#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${SCRIPT_DIR}/.."
TAG="${TAG:-latest}"
NAMESPACE="${NAMESPACE:-genetics}"
ENABLE_RAG="${ENABLE_RAG:-false}"
SKIP_TERRAFORM="${SKIP_TERRAFORM:-false}"

echo "Deploying genetics-results-suite (tag: ${TAG})"

# apply terraform
cd "${ROOT_DIR}/terraform"
if [ "${SKIP_TERRAFORM}" = "true" ]; then
  echo "=== Skipping Terraform apply (SKIP_TERRAFORM=true) ==="
  terraform init -input=false > /dev/null
else
  echo "=== Applying Terraform ==="
  terraform init
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
TF_STATIC_IP_NAME=$(terraform output -raw static_ip_name)
export GCP_PROJECT="${GCP_PROJECT:-${TF_PROJECT_ID}}"
export GCP_REGION="${GCP_REGION:-${TF_REGION}}"
export DOMAIN="${DOMAIN:-${TF_DOMAIN}}"
export STATIC_IP_NAME="${STATIC_IP_NAME:-${TF_STATIC_IP_NAME}}"
export REGISTRY="${REGISTRY:-${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/genetics-results}"
export LOG_SOURCE="${LOG_SOURCE:-${DOMAIN%%.*}_prod}"
TF_CONFIG_PROFILE=$(terraform output -raw config_profile)
export CONFIG_PROFILE="${CONFIG_PROFILE:-${TF_CONFIG_PROFILE}}"
TF_OAUTH_EMAIL_DOMAIN=$(terraform output -raw oauth_email_domain)
export OAUTH_EMAIL_DOMAIN="${OAUTH_EMAIL_DOMAIN:-${TF_OAUTH_EMAIL_DOMAIN}}"

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

# configs
kubectl apply -f configs/

# ingress resources (substitute domain and static IP)
for f in ingress/*.yaml; do
  envsubst '${DOMAIN} ${STATIC_IP_NAME}' < "$f" | kubectl apply -f -
done

# network policies
kubectl apply -f network-policies/

# deployments (substitute variables and image tags)
for f in deployments/*.yaml; do
  if [ "${ENABLE_RAG}" != "true" ] && [ "$(basename "$f")" = "rag-service.yaml" ]; then
    echo "Skipping rag-service (ENABLE_RAG=${ENABLE_RAG})"
    continue
  fi
  envsubst '${REGISTRY} ${GCP_PROJECT} ${LOG_SOURCE} ${CONFIG_PROFILE} ${OAUTH_EMAIL_DOMAIN} ${DOMAIN}' < "$f" | \
    sed "s/:latest/:${TAG}/g" | kubectl apply -f -
done

echo ""
echo "=== Checking rollout status ==="
DEPLOYS="frontend results-api db-api chat-backend mcp-server"
if [ "${ENABLE_RAG}" = "true" ]; then
  DEPLOYS="${DEPLOYS} rag-service"
fi
for deploy in ${DEPLOYS}; do
  echo "Waiting for ${deploy}..."
  kubectl rollout status deployment/"${deploy}" -n "${NAMESPACE}" --timeout=300s || true
done

echo ""
echo "Deployment complete."
echo "Ingress IP: $(kubectl get ingress genetics-suite -n ${NAMESPACE} -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo 'pending')"
