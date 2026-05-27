#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${SCRIPT_DIR}/.."
TAG="${TAG:-latest}"
NAMESPACE="${NAMESPACE:-genetics}"
ENABLE_RAG="${ENABLE_RAG:-false}"
SKIP_TERRAFORM="${SKIP_TERRAFORM:-false}"

echo "Deploying genetics-results-suite (tag: ${TAG})"

# determine config profile for backend selection
cd "${ROOT_DIR}/terraform"
PROFILE="${CONFIG_PROFILE:-$(grep -E '^\s*config_profile\s*=' terraform.tfvars 2>/dev/null | sed 's/.*=\s*"\(.*\)"/\1/')}"
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

# LLM model
if [ "${CONFIG_PROFILE}" = "daly" ]; then
  export DEFAULT_MODEL="${DEFAULT_MODEL:-claude-opus-4-7}"
else
  export DEFAULT_MODEL="${DEFAULT_MODEL:-claude-opus-4-7}"
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

# configs (envsubst for profile-aware values like OAUTH_EMAIL_DOMAIN)
for f in configs/*.yaml; do
  envsubst '${OAUTH_EMAIL_DOMAIN}' < "$f" | kubectl apply -f -
done

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
  if [ "${ENABLE_RAG}" != "true" ] && [ "$(basename "$f")" = "rag-service.yaml" ]; then
    echo "Skipping rag-service (ENABLE_RAG=${ENABLE_RAG})"
    continue
  fi
  envsubst '${REGISTRY} ${GCP_PROJECT} ${BQ_DATASET} ${LOG_SOURCE} ${CONFIG_PROFILE} ${OAUTH_EMAIL_DOMAIN} ${DOMAIN} ${DEFAULT_MODEL} ${SLACK_ALERT_USER_ID}' < "$f" | \
    sed "s/:latest/:${TAG}/g" | kubectl apply -f -
done

echo ""
echo "=== Forcing rollout restarts ==="
# Always restart so pods pick up: (a) freshly-built :latest images,
# (b) ConfigMap changes (subPath mounts don't propagate; oauth2-proxy doesn't hot-reload).
DEPLOYS="frontend results-api db-api chat-backend mcp-server auth-gateway oauth2-proxy"
if [ "${ENABLE_RAG}" = "true" ]; then
  DEPLOYS="${DEPLOYS} rag-service"
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
