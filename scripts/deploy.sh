#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TAG="${TAG:-latest}"
NAMESPACE="${NAMESPACE:-genetics}"
ENABLE_RAG="${ENABLE_RAG:-false}"
SKIP_TERRAFORM="${SKIP_TERRAFORM:-false}"

# resolve which deployment this is (DEPLOY_ENV) and load its gitignored .env
. "${SCRIPT_DIR}/lib/env.sh"
resolve_deploy_env
load_deploy_env

# the hook FILES are tracked (.beads/hooks/*) but core.hooksPath is local config a
# clone does not carry, so an unwired checkout commits with no doc-drift warning and
# no beads export, silently. warn here; never block a deploy over it.
"${SCRIPT_DIR}/install-git-hooks.sh" --check || true

# same class, one level up: several paths this script depends on (the tfvars file,
# the sibling repos sync-datasets.sh copies into, the beads export) resolve into the
# MAIN checkout when this runs from a worktree, and degrade without erroring.
"${SCRIPT_DIR}/check-worktree-paths.sh" --check || true

echo "Deploying genetics-results-suite (env: ${DEPLOY_ENV:-default}, tag: ${TAG})"

cd "${ROOT_DIR}/terraform"
# The code-execution sandbox is gated on the node pool that hosts it, DERIVED rather than a
# separate switch: k8s/deployments/sandbox.yaml tolerates a taint only the gVisor pool carries,
# and that pool exists only when sandbox_pool_enabled = true (terraform/gke.tf, default false).
# Two independent switches would let a deploy apply the manifest with no pool, which is a pod
# Pending forever — not a visible failure, since kubectl apply succeeds and the rollout restart
# below does not wait on this Deployment.
# An explicit ENABLE_SANDBOX in the environment still wins, for SKIP_TERRAFORM=true re-applies
# where this file cannot see the state terraform holds; the live gVisor-node check in the sandbox
# preflight (right after kubectl is configured) is what catches an override that is simply wrong.
TFVARS_SANDBOX_POOL="false"
if [ -f "${TFVARS}" ] && grep -Eq '^[[:space:]]*sandbox_pool_enabled[[:space:]]*=[[:space:]]*true' "${TFVARS}"; then
  TFVARS_SANDBOX_POOL="true"
fi
ENABLE_SANDBOX="${ENABLE_SANDBOX:-${TFVARS_SANDBOX_POOL}}"
# exported because scripts/test-network-policies.py reads it: this run will not APPLY the
# sandbox, so db-api's and results-api's SANDBOX_ENABLED may legitimately still be "false" and
# that harness must not abort the deploy over it. NOT APPLYING IS NOT THE SAME AS NOT RUNNING —
# this script skips sandbox.yaml when the gate is off, it never deletes it, so a later deploy
# from a worktree or with the tfvars file unreadable runs gate-off against a sandbox that is
# still serving. The harness therefore probes the CLUSTER as well as reading this variable, and
# refuses when the two disagree; see its "SANDBOX_ENABLED is true..." check.
export ENABLE_SANDBOX

echo "Using tfvars:  ${TFVARS##*/}"
echo "Using backend: ${BACKEND_FILE##*/}"

# MANIFEST-RENDER PREFLIGHT — offline, and first, deliberately.
# Every manifest applied below is piped through `envsubst '<whitelist>'` in full, not
# field-wise, so a whitelisted name spelled ${...} anywhere in a file is substituted — comments
# included. LEGACY_REDIRECT and KEYCLOAK_SERVER are multi-line, so such an expansion inside a
# `#` line breaks out of the comment and the render stops being valid YAML; kubectl apply then
# fails partway through a deploy. This needs no terraform,
# no cluster and no credentials, so it runs before either, and it derives the whitelists from
# this script rather than carrying a copy of them.
# exit 1 = a manifest would misrender, and the deploy aborts before anything is applied.
# exit 2 = the harness itself could not tell (no PyYAML, no envsubst, or ANY drift in this
# script it cannot follow — a renamed render-loop variable, an envsubst wrapped over two lines,
# a `printf -v` it cannot read, a rendered directory that no longer exists). It cross-checks
# what it parsed out of this script against a looser survey of the same loops and refuses on
# any disagreement, rather than quietly checking fewer files. That is not evidence of a broken
# manifest, so it only warns.
set +e
python3 "${SCRIPT_DIR}/test-manifest-render.py"
render_check=$?
set -e
if [ "${render_check}" -eq 1 ]; then
  echo "ERROR: a manifest does not survive deploy.sh's own envsubst; refusing to deploy."
  exit 1
elif [ "${render_check}" -ne 0 ]; then
  echo "WARNING: scripts/test-manifest-render.py could not run (exit ${render_check}); deploying unverified."
fi

# apply terraform
if [ "${SKIP_TERRAFORM}" = "true" ]; then
  echo "=== Skipping Terraform apply (SKIP_TERRAFORM=true) ==="
  terraform init -input=false -backend-config="${BACKEND_FILE}" -reconfigure > /dev/null
else
  # the file-exists and tfvars/backend-agreement guards this branch used to carry now live in
  # scripts/lib/env.sh: resolve_deploy_env() refuses when the resolved tfvars is missing, and
  # derives TFVARS and BACKEND_FILE from the same DEPLOY_ENV (or, in legacy mode, the backend
  # from the tfvars' own config_profile), so the two identities cannot disagree by construction.
  echo "=== Applying Terraform ==="
  terraform init -backend-config="${BACKEND_FILE}" -reconfigure
  terraform apply -auto-approve "${TF_VAR_FILE_ARGS[@]}"
fi

# configure kubectl
echo "=== Configuring kubectl ==="
CLUSTER_NAME=$(terraform output -raw cluster_name)
export CLUSTER_NAME
eval "$(terraform output -raw kubectl_command)"

# SANDBOX PREFLIGHT — before anything is applied, deliberately.
# Both checks below used to live inside the `for f in deployments/*.yaml` loop, where
# sandbox.yaml sorts second-to-last: `exit 1` there fired only after every other manifest had
# been applied and skipped the cronjob apply, every rollout restart and every rollout-status
# wait, leaving a half-finished deploy whose freshly built :latest images were never rolled and
# no summary line saying so. A precondition that can be judged before the first apply belongs
# before the first apply. The ClusterIP resolution stays in the loop: it genuinely depends on
# db-api.yaml and results-api.yaml having been applied.
if [ "${ENABLE_SANDBOX}" = "true" ]; then
  # The pod tolerates sandbox.gke.io/runtime=gvisor:NoSchedule and selects workload=sandbox, so
  # with no such node it goes Pending and STAYS Pending: kubectl apply returns 0 and the deploy
  # reports success over a feature that is simply absent. terraform ran above, so if the pool
  # were being created it exists by now.
  if [ -z "$(kubectl get nodes -l workload=sandbox -o name 2>/dev/null)" ]; then
    echo "ERROR: ENABLE_SANDBOX=true but no node carries workload=sandbox."
    echo "       Either the gVisor pool is not up — set sandbox_pool_enabled = true (and"
    echo "       sandbox_node_service_account) in ${TFVARS}, which lives in the"
    echo "       MAIN checkout and is gitignored, and apply terraform before this deploy — or the"
    echo "       pool was just created and its node has not finished registering the label yet, in"
    echo "       which case 'kubectl get nodes -l workload=sandbox' answers within a few minutes"
    echo "       and this deploy can simply be re-run."
    echo "       Applying sandbox.yaml now would leave a permanently Pending pod."
    exit 1
  fi
  # The sandbox image's ENTRYPOINT is the bare interpreter and it ships no CMD, so a manifest
  # with neither `command:` nor `args:` starts python3 with no script and CrashLoopBackOffs. It
  # SCHEDULES — "Pending forever" above is only the no-pool case — and nothing downstream waits
  # on this rollout, so the deploy would exit 0 and print success over a pod that can never
  # serve. That is the same silent success the gate exists to prevent, so refuse it too. Keyed
  # on the manifest: the check clears itself once the manifest carries `args:`.
  # PARSED, NOT GREPPED, and scoped to the one container it is about. The indentation-anchored
  # grep this replaced was wrong in both directions, measured end to end: an initContainer or a
  # second document carrying a `command:`/`args:` key at the same column CLEARED it with the
  # sandbox container's own `args:` deleted, and a semantically identical reformat that moved
  # the real `args:` two columns TRIPPED it with a message claiming the key was absent. So ask
  # the question directly — does the container named `sandbox` in the Deployment named `sandbox`
  # run something — the way scripts/test-network-policies.py already reads this directory.
  # Fails closed on an unparseable file or a missing PyYAML (exit 2), because "cannot tell"
  # must not read as "fine"; each exit code below names the cause it actually detected.
  SANDBOX_ARGV_RC=0
  SANDBOX_ARGV_ERR=$(python3 - "${ROOT_DIR}/k8s/deployments/sandbox.yaml" 2>&1 >/dev/null <<'PY'
import sys

try:
    import yaml
except ImportError:
    print("PyYAML is not installed (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

path = sys.argv[1]
try:
    with open(path) as fh:
        docs = list(yaml.safe_load_all(fh))
except (OSError, yaml.YAMLError) as exc:
    print(str(exc).replace("\n", " ")[:300], file=sys.stderr)
    sys.exit(2)

deployment = None
for doc in docs:
    if not isinstance(doc, dict):
        continue
    if doc.get("kind") == "Deployment" and (doc.get("metadata") or {}).get("name") == "sandbox":
        deployment = doc
        break
if deployment is None:
    print("no Deployment named 'sandbox' in the file", file=sys.stderr)
    sys.exit(3)

# containers: only. initContainers and ephemeralContainers are deliberately NOT consulted —
# an init container that runs something says nothing about what the serving container runs.
spec = (((deployment.get("spec") or {}).get("template") or {}).get("spec") or {})
containers = spec.get("containers") or []
container = None
for c in containers:
    if isinstance(c, dict) and c.get("name") == "sandbox":
        container = c
        break
if container is None:
    names = ", ".join(sorted(str(c.get("name")) for c in containers if isinstance(c, dict))) or "none"
    print("the sandbox Deployment has no container named 'sandbox' (containers: %s)" % names,
          file=sys.stderr)
    sys.exit(3)

# a present-but-empty `args: []` runs the bare ENTRYPOINT, which is the failure being guarded
# against, so emptiness counts as absence.
for key in ("command", "args"):
    value = container.get(key)
    if isinstance(value, (list, tuple)) and any(str(v).strip() for v in value):
        sys.exit(0)
    if isinstance(value, str) and value.strip():
        sys.exit(0)
print("the sandbox container declares no non-empty command/args", file=sys.stderr)
sys.exit(4)
PY
  ) || SANDBOX_ARGV_RC=$?
  if [ "${SANDBOX_ARGV_RC}" != "0" ]; then
    case "${SANDBOX_ARGV_RC}" in
      3) echo "ERROR: k8s/deployments/sandbox.yaml no longer describes the workload this gate checks" ;;
      4) echo "ERROR: ENABLE_SANDBOX=true but k8s/deployments/sandbox.yaml's sandbox container"
         echo "       declares no command/args." ;;
      *) echo "ERROR: could not determine what k8s/deployments/sandbox.yaml's sandbox container runs" ;;
    esac
    echo "       cause: ${SANDBOX_ARGV_ERR:-(no detail)}"
    echo "       The check is scoped to the container named 'sandbox' in the Deployment named"
    echo "       'sandbox', and refuses when it cannot tell."
    echo "       The image's ENTRYPOINT is the bare interpreter and it ships no CMD, so a pod with"
    echo "       no command/args starts python3 with no script and CrashLoopBackOffs while the"
    echo "       deploy reports success — nothing here waits on that rollout. Restore"
    echo "       'args: [\"/genetics/supervisor.py\"]' on the sandbox container, or set"
    echo "       sandbox_pool_enabled = false."
    exit 1
  fi
fi

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
TF_REGISTRY=$(terraform output -raw registry)
resolve_registry "${TF_REGISTRY}"

# THE SANDBOX IMAGE MUST EXIST BEFORE THE MANIFEST IS APPLIED. scripts/build-all.sh SKIPS the
# sandbox image non-fatally when the resolved genetics-mcp-server branch has no SDK or when the
# schema docs fail to verify — a warning in the middle of a long build log, after which the build
# still exits 0. While the supervisor refusal above was unclearable that could not reach the
# cluster; now that the manifest carries args:, an omitted image becomes a sandbox Deployment
# pointing at a tag nobody pushed — ImagePullBackOff behind a `kubectl apply` that returned 0,
# with nothing rollout-statusing it. Same silent success, different route, so it is refused the
# same way. Not in the preflight above only because REGISTRY is not resolved until here.
# gcloud being unable to ANSWER (not installed, no artifactregistry.reader, no credentials) is
# not evidence of absence and must not block a deploy, so those cases warn; only a definite
# NOT_FOUND is fatal. THE THREE CASES ARE KEPT GENUINELY DISTINCT, because they were not:
# with gcloud off PATH the shell's own "gcloud: command not found" matched a bare `not found`
# substring and the deploy died claiming THE IMAGE was missing, the exact opposite of what this
# comment promises. So the absence of the tool is asked first, and the fatal test is anchored to
# gcloud's own error shape (`ERROR: (gcloud.<command>) ...`, or an explicit NOT_FOUND status)
# rather than to a phrase any program on the system can emit.
if [ "${ENABLE_SANDBOX}" = "true" ]; then
  SANDBOX_IMAGE="${REGISTRY}/sandbox:${TAG}"
  if ! command -v gcloud >/dev/null 2>&1; then
    echo "WARNING: gcloud is not on PATH, so ${SANDBOX_IMAGE} could not be checked."
    echo "         Proceeding; if the image was never pushed the sandbox pod ImagePullBackOffs."
  elif ! AR_ERR=$(gcloud artifacts docker images describe "${SANDBOX_IMAGE}" 2>&1 >/dev/null); then
    if printf '%s' "${AR_ERR}" | grep -qE 'NOT_FOUND' ||
       printf '%s' "${AR_ERR}" | grep -qE '^ERROR: \(gcloud\.[^)]*\).*([Nn]ot found|does not exist)'; then
      echo "ERROR: ENABLE_SANDBOX=true but ${SANDBOX_IMAGE} is not in the registry."
      echo "       scripts/build-all.sh skips the sandbox image non-fatally (no SDK on the"
      echo "       resolved genetics-mcp-server branch, or the schema-doc check failed) and still"
      echo "       exits 0 — re-read the build log for a 'SKIPPING sandbox' line. Applying"
      echo "       sandbox.yaml now would leave an ImagePullBackOff pod nothing waits on."
      echo "       Build it with scripts/build.sh sandbox (which fails hard instead of skipping)."
      exit 1
    fi
    # unanswerable, not absent. Name the likely reason rather than just the query's output: the
    # operator has to decide whether to trust this warning, and "PERMISSION_DENIED" and "could
    # not reach the API" call for different responses.
    case "${AR_ERR}" in
      *PERMISSION_DENIED*|*"does not have permission"*|*Forbidden*)
        AR_WHY="the caller lacks artifactregistry.reader on ${REGISTRY}" ;;
      *UNAUTHENTICATED*|*"gcloud auth"*|*credentials*)
        AR_WHY="gcloud has no usable credentials (try: gcloud auth login)" ;;
      *)
        AR_WHY="the query failed for a reason that is not an absent image" ;;
    esac
    echo "WARNING: could not verify ${SANDBOX_IMAGE} exists — ${AR_WHY}."
    echo "         gcloud said: ${AR_ERR%%$'\n'*}"
    echo "         Proceeding; if the image was never pushed the sandbox pod ImagePullBackOffs."
  fi
fi
export LOG_SOURCE="${LOG_SOURCE:-${DOMAIN%%.*}_prod}"
export BQ_DATASET="${BQ_DATASET:-genetics_results}"
TF_CONFIG_PROFILE=$(terraform output -raw config_profile)
export CONFIG_PROFILE="${CONFIG_PROFILE:-${TF_CONFIG_PROFILE}}"
TF_OAUTH_EMAIL_DOMAIN=$(terraform output -raw oauth_email_domain)
export OAUTH_EMAIL_DOMAIN="${OAUTH_EMAIL_DOMAIN:-${TF_OAUTH_EMAIL_DOMAIN}}"
TF_OAUTH_ALLOWED_EMAILS=$(terraform output -raw oauth_allowed_emails 2>/dev/null || true)
export OAUTH_ALLOWED_EMAILS="${OAUTH_ALLOWED_EMAILS:-${TF_OAUTH_ALLOWED_EMAILS}}"
# audience accepted on Google Identity Tokens — a deprecated access path; per-user API keys are the
# documented programmatic flow. Defaults to the gcloud CLI's *public* OAuth client id, which is what
# `gcloud auth print-identity-token` mints; user credentials cannot request a custom audience, so a
# project-owned client id here would reject every human caller. Worth exactly cross-OAuth-client replay
# protection, not identity: it rejects a token minted for a different client id, but NOT one the same
# user handed to another service documenting this same `gcloud auth print-identity-token` flow, since
# that token carries the identical aud. The email allow-list is the access control. Override to add
# service-account clients.
export GOOGLE_TOKEN_AUDIENCE="${GOOGLE_TOKEN_AUDIENCE:-32555940559.apps.googleusercontent.com}"
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

# MCP OAuth resource-server env. Non-secret PUBLIC values rendered
# into the mcp-server Deployment via envsubst; the mcp-server treats them as optional and the
# whole OAuth path stays inert when they are empty — so profiles without the Keycloak broker
# (finngen) get no resource-server behaviour. OAUTH_ISSUER reuses OIDC_ISSUER_URL so it exactly
# matches the `iss` Keycloak advertises (KC_HOSTNAME/realms/genetics); OAUTH_RESOURCE_URL is the
# MCP canonical URL / expected token audience on the canonical web host. JWKS is left to the
# mcp-server (derives <issuer>/protocol/openid-connect/certs).
export OAUTH_ISSUER="${OIDC_ISSUER_URL}"
if [ "${ENABLE_KEYCLOAK}" = "true" ]; then
  export OAUTH_RESOURCE_URL="https://${REDIRECT_TO_HOST:-${DOMAIN}}/mcp"
else
  export OAUTH_RESOURCE_URL=""
fi

# slack member id(s) to @mention on monitor failures; space/comma-separated for multiple.
# kept out of version control — set via .env or the shell environment.
export SLACK_ALERT_USER_ID="${SLACK_ALERT_USER_ID:-}"

# LLM model
if [ "${CONFIG_PROFILE}" = "daly" ]; then
  export DEFAULT_MODEL="${DEFAULT_MODEL:-claude-opus-5}"
else
  export DEFAULT_MODEL="${DEFAULT_MODEL:-claude-opus-5}"
fi

# apply kubernetes manifests
echo "=== Applying Kubernetes manifests ==="
cd "${ROOT_DIR}/k8s"

# check secrets exist
kubectl get secret genetics-secrets -n "${NAMESPACE}" > /dev/null 2>&1 || {
  echo "ERROR: genetics-secrets not found. Run create-secrets.sh first."
  exit 1
}

# Every deployment below mounts sandbox-token-signing-key. Missing, the pods stay in
# CreateContainerConfigError rather than starting degraded, so catch it here instead: an older
# genetics-secrets predating the sandbox work simply has no such key.
#
# create-secrets.sh now reuses every key it does not get from the environment, so re-running it
# is a safe fix here and no longer blanks the optional keys. Its one requirement is an exported
# ANTHROPIC_API_KEY — that key alone is never read back from the cluster, and without it the
# script aborts before writing anything. The targeted patch below stays as the alternative for
# operators who do not have that key to hand.
# gateway-identity-secret is checked the same way and for a sharper reason: auth-gateway's
# render-config initContainer refuses to start without it, and chat-backend gates code
# execution on it, so an older genetics-secrets would take the gateway down at rollout.
for key in internal-api-secret sandbox-token-signing-key gateway-identity-secret; do
  if [ -z "$(kubectl get secret genetics-secrets -n "${NAMESPACE}" \
       -o jsonpath="{.data.${key}}" 2>/dev/null)" ]; then
    cat >&2 <<EOF
ERROR: genetics-secrets is missing '${key}'.

Add ONLY that key, leaving every other key in the secret untouched:

  kubectl patch secret genetics-secrets -n ${NAMESPACE} --type=merge \\
    -p "{\"stringData\":{\"${key}\":\"\$(openssl rand -base64 32)\"}}"

(Substitute your own value for \$(openssl rand -base64 32) if the key already exists elsewhere
and must match — sandbox-token-signing-key must be identical on chat-backend, db-api and
results-api, internal-api-secret on every internal caller, and gateway-identity-secret on
auth-gateway and chat-backend — those two ONLY, since it is what tells chat-backend a request
came through the gateway rather than from another holder of internal-api-secret.)

Re-running create-secrets.sh also fixes this and is safe for the optional keys: it reuses every
value already in the cluster instead of blanking the ones you have not exported. It does still
require ANTHROPIC_API_KEY to be exported — that is the one key it never reads back from the
cluster, and without it the script aborts (safely, writing nothing).
EOF
    exit 1
  fi
done

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
# note: the oauth2-allowed-emails ConfigMap has no manifest here — it is generated below.
for f in configs/*.yaml; do
  envsubst '${OAUTH_EMAIL_DOMAIN} ${OAUTH_ALLOWED_EMAILS} ${GOOGLE_TOKEN_AUDIENCE}' < "$f" | kubectl apply -f -
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
  # brainzzz OAuth client (GeneGenie MCP integration): injected only when its secret is set in
  # .env, so deployments that don't use it stay unaffected. On an already-imported realm this
  # template change does nothing (import runs on a fresh DB only) — use
  # scripts/keycloak-register-brainzzz.sh to reconcile the live realm.
  BRAINZZZ_CLIENT_ENTRY=""
  if [ -n "${BRAINZZZ_CLIENT_SECRET:-}" ]; then
    BRAINZZZ_JSON="$(envsubst '${BRAINZZZ_CLIENT_SECRET} ${OAUTH_RESOURCE_URL}' \
      < "${ROOT_DIR}/keycloak/brainzzz-client.json.template")"
    BRAINZZZ_CLIENT_ENTRY=",${BRAINZZZ_JSON}"
  fi
  export BRAINZZZ_CLIENT_ENTRY
  # the oauth2-proxy callback lives on the canonical web host (redirect_to_host when migrating,
  # else the primary domain) — NOT a legacy host that 301-redirects away.
  REALM_DOMAIN="${REDIRECT_TO_HOST:-${DOMAIN}}"
  REALM_RENDERED="$(DOMAIN="${REALM_DOMAIN}" envsubst '${DOMAIN} ${APP_NAME} ${OAUTH_EMAIL_DOMAIN} ${OAUTH_ALLOWED_EMAILS} ${OAUTH2_PROXY_CLIENT_SECRET} ${GOOGLE_CLIENT_ID} ${GOOGLE_CLIENT_SECRET} ${APPLE_IDP_ENTRY} ${BRAINZZZ_CLIENT_ENTRY}' \
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

# refresh the sibling service repos' LOCAL datasets.yaml from the canonical copy. Those
# copies are gitignored and untracked in both siblings, so they exist only on a developer's
# machine and never reach an image (build.sh clones from GitHub). Best-effort: prints SKIP
# for a sibling that isn't cloned here and exits 0; it only fails if it cannot resolve the
# sibling root or finds a directory that isn't that repo. The pods below get the canonical
# file via the ConfigMap regardless — this is purely so local dev isn't stale.
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
# The union of every file in network-policies/ is what actually decides "mcp-server cannot
# reach the sandbox" and "the sandbox reaches nothing but db-api and results-api". The
# pre-commit hook only runs the doc-drift check, and there is no CI, so nothing else in
# this repo runs this check — it runs here, before the apply that would put a broken
# union on the cluster.
# exit 1 = a control is broken, and the deploy aborts. exit 2 = the harness itself could
# not run (missing PyYAML); that is not evidence of a broken policy, so it only warns.
set +e
python3 "${SCRIPT_DIR}/test-network-policies.py"
policy_check=$?
set -e
if [ "${policy_check}" -eq 1 ]; then
  echo "ERROR: network-policy checks failed; refusing to apply network-policies/."
  echo "       See docs/code-execution-security.md sections 3 and 5."
  exit 1
elif [ "${policy_check}" -ne 0 ]; then
  echo "WARNING: scripts/test-network-policies.py could not run (exit ${policy_check}); applying unverified."
fi
kubectl apply -f network-policies/

# pod disruption budgets
kubectl apply -f disruption-budgets/

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
  if [ "${base}" = "sandbox.yaml" ]; then
    if [ "${ENABLE_SANDBOX}" != "true" ]; then
      echo "Skipping sandbox (ENABLE_SANDBOX=${ENABLE_SANDBOX}; set sandbox_pool_enabled = true in ${TFVARS##*/})"
      continue
    fi
    # the gVisor-node and supervisor preconditions were judged in the sandbox preflight near the
    # top of this script, before the first apply. What is left here genuinely cannot be:
    # hostAliases replace DNS for this pod (docs/code-execution-security.md, "On DNS"), so the
    # two ClusterIPs are resolved from the live cluster and substituted here. Both Services are
    # applied earlier in this same loop (db-api.yaml, results-api.yaml sort before sandbox.yaml),
    # so they exist by now; an empty value would render `ip: ""` and be rejected by the API
    # server, but failing here says why.
    DB_API_CLUSTER_IP="$(kubectl get svc db-api -n "${NAMESPACE}" -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
    RESULTS_API_CLUSTER_IP="$(kubectl get svc results-api -n "${NAMESPACE}" -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
    if [ -z "${DB_API_CLUSTER_IP}" ] || [ -z "${RESULTS_API_CLUSTER_IP}" ]; then
      echo "ERROR: could not resolve the db-api / results-api ClusterIPs the sandbox pins in"
      echo "       /etc/hosts (db-api='${DB_API_CLUSTER_IP}' results-api='${RESULTS_API_CLUSTER_IP}')."
      echo "       The sandbox has no DNS, so an unpinned name is unresolvable inside it."
      exit 1
    fi
    export DB_API_CLUSTER_IP RESULTS_API_CLUSTER_IP
    envsubst '${REGISTRY} ${DB_API_CLUSTER_IP} ${RESULTS_API_CLUSTER_IP}' < "$f" | \
      sed "s/:latest/:${TAG}/g" | kubectl apply -f -
    continue
  fi
  envsubst '${REGISTRY} ${GCP_PROJECT} ${BQ_DATASET} ${LOG_SOURCE} ${CONFIG_PROFILE} ${OAUTH_EMAIL_DOMAIN} ${DOMAIN} ${KEYCLOAK_HOST} ${OAUTH2_PROVIDER} ${OIDC_ISSUER_URL} ${OIDC_BACKEND_LOGOUT_URL} ${KEYCLOAK_SERVER} ${DEFAULT_MODEL} ${APP_NAME} ${SLACK_ALERT_USER_ID} ${LEGACY_REDIRECT} ${OAUTH_ISSUER} ${OAUTH_RESOURCE_URL} ${CLUSTER_NAME}' < "$f" | \
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
# ORDER WARNING: this list is restarted in one loop with no waiting, and results-api comes
# BEFORE bff's effect lands and before mcp-server — the opposite of the cross-service ordering
# documented in scripts/rollout.sh's ORDERING header (bff -> mcp-server -> results-api, because
# results-api's ANONYMOUS_SURFACE_MINIMAL defaults on and the browser reaches /api/v1/auth,
# /api/v1/variant_sets and /api/v1/rsid/variants through the bff's credential-less generic
# passthrough). A full deploy from a state where the new bff is not yet built therefore takes a
# transient browser 401 on those routes. When that matters, roll the three out individually with
# scripts/rollout.sh in that order instead.
DEPLOYS="frontend bff results-api db-api chat-backend mcp-server auth-gateway oauth2-proxy"
if [ "${ENABLE_RAG}" = "true" ]; then
  DEPLOYS="${DEPLOYS} rag-service"
fi
if [ "${ENABLE_KEYCLOAK}" = "true" ]; then
  DEPLOYS="${DEPLOYS} keycloak"
fi
if [ "${ENABLE_SANDBOX}" = "true" ]; then
  # last on purpose: strategy is Recreate on a single pinned node, so the restart is a brief
  # outage of code execution, and a restart mid-execution kills that script
  DEPLOYS="${DEPLOYS} sandbox"
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
