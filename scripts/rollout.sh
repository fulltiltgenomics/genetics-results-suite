#!/bin/bash
set -euo pipefail

# update a single service's container image
# usage: rollout.sh <service-name> [tag]

SERVICE="${1:?Usage: rollout.sh <service-name> [tag]}"
TAG="${2:-latest}"
NAMESPACE="${NAMESPACE:-genetics}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# resolve the target deployment (DEPLOY_ENV) so REGISTRY defaults to that deployment's own
# repository. This only sets the image reference — the cluster acted on is whatever kubectl's
# current context points at, which is why it is echoed below.
. "${SCRIPT_DIR}/lib/env.sh"
resolve_deploy_env
resolve_registry
echo "Context: $(kubectl config current-context 2>/dev/null || echo unknown) (env: ${DEPLOY_ENV:-default})"

declare -A IMAGE_MAP=(
  [frontend]=genetics-results-browser
  [bff]=genetics-results-browser-bff
  [results-api]=genetics-results-api
  [chat-backend]=genetics-mcp-server
  [mcp-server]=genetics-mcp-server
  [db-api]=genetics-results-db
  [rag-service]=genetics-rag-service
)

IMAGE="${IMAGE_MAP[$SERVICE]:-}"

if [ -z "${IMAGE}" ]; then
  echo "Unknown service: ${SERVICE}"
  echo "Available services: ${!IMAGE_MAP[*]}"
  exit 1
fi

CONTAINER_NAME="${SERVICE}"

echo "Updating ${SERVICE} to ${REGISTRY}/${IMAGE}:${TAG}"
kubectl set image deployment/"${SERVICE}" \
  "${CONTAINER_NAME}=${REGISTRY}/${IMAGE}:${TAG}" \
  -n "${NAMESPACE}"

kubectl rollout status deployment/"${SERVICE}" -n "${NAMESPACE}" --timeout=300s
echo "Rollout complete for ${SERVICE}."
