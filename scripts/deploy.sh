#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${SCRIPT_DIR}/.."
TAG="${TAG:-latest}"
NAMESPACE="${NAMESPACE:-genetics}"
ENABLE_RAG="${ENABLE_RAG:-false}"
SKIP_TERRAFORM="${SKIP_TERRAFORM:-false}"

# load deploy-time config that must stay out of version control (.env is gitignored)
if [ -f "${ROOT_DIR}/.env" ]; then
  set -a; . "${ROOT_DIR}/.env"; set +a
fi

echo "Deploying genetics-results-suite (tag: ${TAG})"

# determine config profile for backend selection
cd "${ROOT_DIR}/terraform"
if [ -n "${CONFIG_PROFILE:-}" ]; then
  PROFILE="${CONFIG_PROFILE}"
elif [ -f terraform.tfvars ]; then
  PROFILE="$(grep -E '^\s*config_profile\s*=' terraform.tfvars | sed 's/.*=\s*"\(.*\)"/\1/')"
else
  echo "ERROR: terraform/terraform.tfvars not found. Copy terraform.tfvars.example and edit it (or set CONFIG_PROFILE)."
  exit 1
fi
BACKEND_FILE="${ROOT_DIR}/terraform/${PROFILE}.tfbackend"
if [ ! -f "${BACKEND_FILE}" ]; then
  echo "ERROR: Backend config not found: ${BACKEND_FILE}"
  echo "Expected one of: daly.tfbackend, finngen.tfbackend"
  exit 1
fi
echo "Using backend config: ${PROFILE}.tfbackend"

# apply terraform
if [ "${SKIP_TERRAFORM}" = "true" ]; then
  echo "=== Skipping Terraform apply (SKIP_TERRAFORM=true) ==="
  terraform init -input=false -backend-config="${BACKEND_FILE}" -reconfigure > /dev/null
else
  echo "=== Applying Terraform ==="
  terraform init -backend-config="${BACKEND_FILE}" -reconfigure
  terraform apply -auto-approve
fi

# configure kubectl
echo "=== Configuring kubectl ==="
CLUSTER_NAME=$(terraform output -raw cluster_name)
eval "$(terraform output -raw kubectl_command)"

# derive variables from terraform (all overridable via env vars)
TF_PROJECT_ID=$(terraform output -raw project_id)
TF_REGION=$(terraform output -raw region)
TF_DOMAIN=$(terraform output -raw domain)
TF_DOMAINS=$(terraform output -raw domains)
TF_STATIC_IP_NAME=$(terraform output -raw static_ip_name)
export GCP_PROJECT="${GCP_PROJECT:-${TF_PROJECT_ID}}"
export GCP_REGION="${GCP_REGION:-${TF_REGION}}"
export DOMAIN="${DOMAIN:-${TF_DOMAIN}}"
DOMAINS="${DOMAINS:-${TF_DOMAINS}}"
export STATIC_IP_NAME="${STATIC_IP_NAME:-${TF_STATIC_IP_NAME}}"
export REGISTRY="${REGISTRY:-${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/genetics-results}"
export LOG_SOURCE="${LOG_SOURCE:-${DOMAIN%%.*}_prod}"
export BQ_DATASET="${BQ_DATASET:-genetics_results}"
TF_CONFIG_PROFILE=$(terraform output -raw config_profile)
export CONFIG_PROFILE="${CONFIG_PROFILE:-${TF_CONFIG_PROFILE}}"
TF_OAUTH_EMAIL_DOMAIN=$(terraform output -raw oauth_email_domain)
export OAUTH_EMAIL_DOMAIN="${OAUTH_EMAIL_DOMAIN:-${TF_OAUTH_EMAIL_DOMAIN}}"
TF_OAUTH_ALLOWED_EMAILS=$(terraform output -raw oauth_allowed_emails 2>/dev/null || true)
export OAUTH_ALLOWED_EMAILS="${OAUTH_ALLOWED_EMAILS:-${TF_OAUTH_ALLOWED_EMAILS}}"
TF_KEYCLOAK_BACKUP_BUCKET=$(terraform output -raw keycloak_backup_bucket 2>/dev/null || true)
export KEYCLOAK_BACKUP_BUCKET="${KEYCLOAK_BACKUP_BUCKET:-${TF_KEYCLOAK_BACKUP_BUCKET}}"
TF_APP_NAME=$(terraform output -raw app_name)
export APP_NAME="${APP_NAME:-${TF_APP_NAME}}"
TF_REDIRECT_FROM_HOST=$(terraform output -raw redirect_from_host 2>/dev/null || true)
TF_REDIRECT_TO_HOST=$(terraform output -raw redirect_to_host 2>/dev/null || true)
REDIRECT_FROM_HOST="${REDIRECT_FROM_HOST:-${TF_REDIRECT_FROM_HOST}}"
REDIRECT_TO_HOST="${REDIRECT_TO_HOST:-${TF_REDIRECT_TO_HOST}}"

# build the auth-gateway legacy-redirect nginx snippet (empty when not configured).
# indentation in the continuation lines matches the surrounding server { } block.
if [ -n "${REDIRECT_FROM_HOST}" ] && [ -n "${REDIRECT_TO_HOST}" ]; then
  printf -v LEGACY_REDIRECT '# 301 the old hostname to the new one, preserving path + query\n        if ($host = %s) {\n          return 301 https://%s$request_uri;\n        }' \
    "${REDIRECT_FROM_HOST}" "${REDIRECT_TO_HOST}"
  echo "Legacy redirect: ${REDIRECT_FROM_HOST} -> ${REDIRECT_TO_HOST}"
else
  LEGACY_REDIRECT=""
fi
export LEGACY_REDIRECT

# Keycloak identity broker — enabled per profile (default: on for daly). When enabled,
# oauth2-proxy uses OIDC against Keycloak (Google + Apple) and the auth-gateway serves the
# login endpoint; otherwise oauth2-proxy talks to Google directly and no broker is deployed.
ENABLE_KEYCLOAK="${ENABLE_KEYCLOAK:-$([ "${CONFIG_PROFILE}" = "daly" ] && echo true || echo false)}"
# Keycloak is exposed under a PATH on the primary domain (https://<domain>/auth) so it reuses
# the existing DNS record, managed cert and ingress — no dedicated auth.<domain> subdomain (and
# its DNS/cert provisioning) required. Keycloak keeps its default "/" relative path and merely
# advertises the /auth prefix via KC_HOSTNAME; the nginx location below strips the prefix.
# To switch to a dedicated subdomain once DNS exists, see
# docs/keycloak-apple-signin.md ("Switching Keycloak to a dedicated subdomain").
KEYCLOAK_PATH="/auth"
export KEYCLOAK_HOST="${KEYCLOAK_HOST:-${REDIRECT_TO_HOST:-${DOMAIN}}${KEYCLOAK_PATH}}"
if [ "${ENABLE_KEYCLOAK}" = "true" ]; then
  export OAUTH2_PROVIDER="oidc"
  export OIDC_ISSUER_URL="https://${KEYCLOAK_HOST}/realms/genetics"
  # full logout: on sign_out, oauth2-proxy calls this server-side (in-cluster, no hairpin) to end
  # the Keycloak SSO session — otherwise the browser keeps the Keycloak session and the next
  # request silently re-authenticates. {id_token} is filled in by oauth2-proxy from the session.
  export OIDC_BACKEND_LOGOUT_URL="http://keycloak.genetics.svc.cluster.local:8080/realms/genetics/protocol/openid-connect/logout?id_token_hint={id_token}"
  # nginx location for the login path, injected INSIDE the main server block. The trailing slash
  # on proxy_pass strips the ${KEYCLOAK_PATH} prefix so Keycloak (served at "/") receives
  # /realms/...; it advertises the prefix back via KC_HOSTNAME. Served WITHOUT the oauth2-proxy
  # auth_request — these are the auth endpoints themselves.
  printf -v KEYCLOAK_SERVER 'location %s/ {\n          proxy_pass http://keycloak.genetics.svc.cluster.local:8080/;\n          proxy_set_header Host $host;\n          proxy_set_header X-Real-IP $remote_addr;\n          proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n          # TLS terminates at the ingress and this hop is plain http, so $scheme is http here.\n          # Tell Keycloak the public scheme is https (the /auth path is only reached via the\n          # https ingress) so it treats the request as a secure context and sets Secure;\n          # SameSite=None cookies itself (needed for the Apple cross-site form_post callback).\n          proxy_set_header X-Forwarded-Proto https;\n          # Belt-and-suspenders for the Apple form_post: force the login cookies SameSite=None;\n          # Secure even if Keycloak ever decides otherwise (default Lax is dropped on the\n          # cross-site POST, giving "Restart login cookie not found").\n          proxy_cookie_flags ~ secure samesite=none;\n        }' "${KEYCLOAK_PATH}"
  echo "Keycloak broker enabled (login URL: https://${KEYCLOAK_HOST})"
else
  export OAUTH2_PROVIDER="google"
  export OIDC_ISSUER_URL=""
  export OIDC_BACKEND_LOGOUT_URL=""
  KEYCLOAK_SERVER=""
  echo "Keycloak broker disabled (oauth2-proxy uses google provider directly)"
fi
export OAUTH2_PROVIDER OIDC_ISSUER_URL OIDC_BACKEND_LOGOUT_URL KEYCLOAK_SERVER

# slack member id(s) to @mention on monitor failures; space/comma-separated for multiple.
# kept out of version control — set via .env or the shell environment.
export SLACK_ALERT_USER_ID="${SLACK_ALERT_USER_ID:-}"

# LLM model
if [ "${CONFIG_PROFILE}" = "daly" ]; then
  export DEFAULT_MODEL="${DEFAULT_MODEL:-claude-opus-4-8}"
else
  export DEFAULT_MODEL="${DEFAULT_MODEL:-claude-opus-4-8}"
fi

# apply kubernetes manifests
echo "=== Applying Kubernetes manifests ==="
cd "${ROOT_DIR}/k8s"

# check secrets exist
kubectl get secret genetics-secrets -n "${NAMESPACE}" > /dev/null 2>&1 || {
  echo "ERROR: genetics-secrets not found. Run create-secrets.sh first."
  exit 1
}

# volumes
for f in volumes/*.yaml; do
  if [ "${ENABLE_RAG}" != "true" ] && [ "$(basename "$f")" = "pvc-rag-stores.yaml" ]; then
    echo "Skipping rag-stores volume (ENABLE_RAG=${ENABLE_RAG})"
    continue
  fi
  if [ "${ENABLE_KEYCLOAK}" != "true" ] && [ "$(basename "$f")" = "pvc-keycloak-postgres.yaml" ]; then
    echo "Skipping keycloak-postgres volume (ENABLE_KEYCLOAK=${ENABLE_KEYCLOAK})"
    continue
  fi
  kubectl apply -f "$f"
done

# attach snapshot policy to chat-data PVC disk (idempotent)
echo "=== Attaching snapshot policy to chat-data disk ==="
SNAPSHOT_POLICY=$(cd "${ROOT_DIR}/terraform" && terraform output -raw snapshot_policy_name 2>/dev/null || true)
if [ -n "${SNAPSHOT_POLICY}" ]; then
  ZONE=$(cd "${ROOT_DIR}/terraform" && terraform output -raw zone)
  PV_NAME=$(kubectl get pvc chat-data -n "${NAMESPACE}" -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)
  if [ -n "${PV_NAME}" ]; then
    DISK_NAME=$(kubectl get pv "${PV_NAME}" -o jsonpath='{.spec.csi.volumeHandle}' 2>/dev/null | awk -F'/' '{print $NF}')
    if [ -n "${DISK_NAME}" ]; then
      gcloud compute disks add-resource-policies "${DISK_NAME}" \
        --resource-policies="${SNAPSHOT_POLICY}" \
        --zone="${ZONE}" \
        --project="${GCP_PROJECT}" \
        2>/dev/null || echo "  Snapshot policy already attached or disk not yet provisioned"
    fi
  else
    echo "  PVC chat-data not yet bound, skipping snapshot policy attachment"
  fi
fi

# configs (envsubst for profile-aware values like OAUTH_EMAIL_DOMAIN).
# oauth2-allowed-emails is generated below from OAUTH_ALLOWED_EMAILS, so skip the static file.
for f in configs/*.yaml; do
  [ "$(basename "$f")" = "oauth2-allowed-emails.yaml" ] && continue
  envsubst '${OAUTH_EMAIL_DOMAIN} ${OAUTH_ALLOWED_EMAILS}' < "$f" | kubectl apply -f -
done

# oauth2-proxy per-address allowlist (one email per line) — single source of truth is
# OAUTH_ALLOWED_EMAILS (comma-separated); these addresses are allowed in addition to the domains.
OAUTH2_ALLOWED_EMAILS_TXT="$(printf '%s' "${OAUTH_ALLOWED_EMAILS}" | tr ',' '\n' | sed '/^$/d')"
kubectl create configmap oauth2-allowed-emails \
  --from-literal=allowed-emails.txt="${OAUTH2_ALLOWED_EMAILS_TXT}" \
  -n "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

# Keycloak realm import: render the template (secrets from .env) into the keycloak-realm
# Secret. Imported by Keycloak only on first start (empty DB); later edits go via the admin
# console. The Apple IdP is injected automatically once APPLE_SERVICES_ID is set in .env
# (register for the Apple Developer Program first); until then the realm is Google-only.
# Skipped when the broker is disabled, or if GOOGLE_CLIENT_ID isn't set.
if [ "${ENABLE_KEYCLOAK}" = "true" ] && [ -n "${GOOGLE_CLIENT_ID:-}" ]; then
  APPLE_IDP_ENTRY=""
  if [ -n "${APPLE_SERVICES_ID:-}" ]; then
    APPLE_JSON="$(envsubst '${APPLE_SERVICES_ID} ${APPLE_TEAM_ID} ${APPLE_KEY_ID} ${APPLE_P8_KEY}' \
      < "${ROOT_DIR}/keycloak/apple-idp.json.template")"
    APPLE_IDP_ENTRY=",${APPLE_JSON}"
  fi
  export APPLE_IDP_ENTRY
  # the oauth2-proxy callback lives on the canonical web host (redirect_to_host when migrating,
  # else the primary domain) — NOT a legacy host that 301-redirects away.
  REALM_DOMAIN="${REDIRECT_TO_HOST:-${DOMAIN}}"
  REALM_RENDERED="$(DOMAIN="${REALM_DOMAIN}" envsubst '${DOMAIN} ${APP_NAME} ${OAUTH_EMAIL_DOMAIN} ${OAUTH_ALLOWED_EMAILS} ${OAUTH2_PROXY_CLIENT_SECRET} ${GOOGLE_CLIENT_ID} ${GOOGLE_CLIENT_SECRET} ${APPLE_IDP_ENTRY}' \
    < "${ROOT_DIR}/keycloak/realm-genetics.json.template")"
  kubectl create secret generic keycloak-realm \
    --from-literal=realm.json="${REALM_RENDERED}" \
    -n "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
  echo "keycloak-realm rendered (apple IdP: ${APPLE_SERVICES_ID:+enabled}${APPLE_SERVICES_ID:-google-only})"
elif [ "${ENABLE_KEYCLOAK}" = "true" ]; then
  echo "Skipping keycloak-realm render (GOOGLE_CLIENT_ID not set in .env)"
fi

# keycloak login theme CSS: mounted into the keycloak pod from this ConfigMap so visual tweaks
# apply with a ConfigMap update + restart, no image rebuild (theme caching is disabled in the
# deployment). The same file is also baked into the image as a fallback.
if [ "${ENABLE_KEYCLOAK}" = "true" ]; then
  kubectl create configmap keycloak-login-css \
    --from-file=genetics.css="${ROOT_DIR}/keycloak/themes/genetics/login/resources/css/genetics.css" \
    -n "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
fi

# keep the sibling service repos' committed datasets.yaml in sync with the canonical
# copy (best-effort: skips repos that aren't checked out next to this one). The pods
# below get the canonical file via the ConfigMap regardless, but this prevents the
# committed copies (used for local dev and baked into freshly built images) from drifting.
if [ -x "${SCRIPT_DIR}/sync-datasets.sh" ]; then
  echo "=== Syncing datasets.yaml to sibling service repos ==="
  "${SCRIPT_DIR}/sync-datasets.sh" || echo "  WARN: dataset sync reported an issue (continuing)"
fi

# datasets ConfigMap (single source of truth for dataset definitions)
kubectl create configmap datasets-config \
  --from-file=datasets.yaml="${ROOT_DIR}/configs/datasets.yaml" \
  -n "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

# ingress resources
# managed-certs and ingress are generated to support multiple domains;
# other ingress files (backend-configs, frontend-config) are applied as-is
for f in ingress/*.yaml; do
  case "$(basename "$f")" in
    managed-certs.yaml|ingress.yaml) continue ;;
    *) kubectl apply -f "$f" ;;
  esac
done

# build managed certificate and ingress with all domains
IFS=',' read -ra DOMAIN_LIST <<< "${DOMAINS}"

generate_cert() {
  echo "apiVersion: networking.gke.io/v1"
  echo "kind: ManagedCertificate"
  echo "metadata:"
  echo "  name: managed-cert"
  echo "  namespace: ${NAMESPACE}"
  echo "spec:"
  echo "  domains:"
  for d in "${DOMAIN_LIST[@]}"; do
    echo "    - ${d}"
  done
}
generate_cert | kubectl apply -f -

generate_ingress() {
  echo "apiVersion: networking.k8s.io/v1"
  echo "kind: Ingress"
  echo "metadata:"
  echo "  name: genetics-suite"
  echo "  namespace: ${NAMESPACE}"
  echo "  annotations:"
  echo "    networking.gke.io/v1beta1.FrontendConfig: \"https-redirect\""
  echo "    kubernetes.io/ingress.global-static-ip-name: ${STATIC_IP_NAME}"
  echo "    networking.gke.io/managed-certificates: \"managed-cert\""
  echo "    kubernetes.io/ingress.allow-http: \"true\""
  echo "spec:"
  echo "  rules:"
  for d in "${DOMAIN_LIST[@]}"; do
    echo "  - host: ${d}"
    echo "    http:"
    echo "      paths:"
    echo "      - path: /*"
    echo "        pathType: ImplementationSpecific"
    echo "        backend:"
    echo "          service:"
    echo "            name: auth-gateway"
    echo "            port:"
    echo "              number: 8080"
  done
}
generate_ingress | kubectl apply -f -

# network policies
kubectl apply -f network-policies/

# deployments (substitute variables and image tags)
for f in deployments/*.yaml; do
  base="$(basename "$f")"
  if [ "${ENABLE_RAG}" != "true" ] && [ "${base}" = "rag-service.yaml" ]; then
    echo "Skipping rag-service (ENABLE_RAG=${ENABLE_RAG})"
    continue
  fi
  if [ "${ENABLE_KEYCLOAK}" != "true" ] && { [ "${base}" = "keycloak.yaml" ] || [ "${base}" = "postgres.yaml" ]; }; then
    echo "Skipping ${base} (ENABLE_KEYCLOAK=${ENABLE_KEYCLOAK})"
    continue
  fi
  envsubst '${REGISTRY} ${GCP_PROJECT} ${BQ_DATASET} ${LOG_SOURCE} ${CONFIG_PROFILE} ${OAUTH_EMAIL_DOMAIN} ${DOMAIN} ${KEYCLOAK_HOST} ${OAUTH2_PROVIDER} ${OIDC_ISSUER_URL} ${OIDC_BACKEND_LOGOUT_URL} ${KEYCLOAK_SERVER} ${DEFAULT_MODEL} ${APP_NAME} ${SLACK_ALERT_USER_ID} ${LEGACY_REDIRECT}' < "$f" | \
    sed "s/:latest/:${TAG}/g" | kubectl apply -f -
done

# cronjobs (e.g. keycloak postgres backup) — only when the broker is enabled
if [ "${ENABLE_KEYCLOAK}" = "true" ]; then
  for f in cronjobs/*.yaml; do
    [ -e "$f" ] || continue
    envsubst '${KEYCLOAK_BACKUP_BUCKET}' < "$f" | kubectl apply -f -
  done
fi

echo ""
echo "=== Forcing rollout restarts ==="
# Always restart so pods pick up: (a) freshly-built :latest images,
# (b) ConfigMap changes (subPath mounts don't propagate; oauth2-proxy doesn't hot-reload).
DEPLOYS="frontend results-api db-api chat-backend mcp-server auth-gateway oauth2-proxy"
if [ "${ENABLE_RAG}" = "true" ]; then
  DEPLOYS="${DEPLOYS} rag-service"
fi
if [ "${ENABLE_KEYCLOAK}" = "true" ]; then
  DEPLOYS="${DEPLOYS} keycloak"
fi
for deploy in ${DEPLOYS}; do
  kubectl rollout restart deployment/"${deploy}" -n "${NAMESPACE}"
done

echo ""
echo "=== Checking rollout status ==="
for deploy in ${DEPLOYS}; do
  echo "Waiting for ${deploy}..."
  kubectl rollout status deployment/"${deploy}" -n "${NAMESPACE}" --timeout=300s || true
done

echo ""
echo "Deployment complete."
echo "Ingress IP: $(kubectl get ingress genetics-suite -n ${NAMESPACE} -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo 'pending')"
