#!/bin/bash
set -euo pipefail

# update a single service's container image
# usage: rollout.sh <service-name> [tag]
#
# ORDERING: roll out `bff` before `results-api`. results-api honours the
# X-Goog-Authenticated-User-Email header only from a caller that also presents
# INTERNAL_API_SECRET, and bff is what attaches it — a new results-api in front of an old bff
# 401s every browser request. The reverse order is safe to sit in. Rollback reverses it
# (results-api first). See README "Deploying the trusted-proxy marker".

SERVICE="${1:?Usage: rollout.sh <service-name> [tag]}"
TAG="${2:-latest}"
NAMESPACE="${NAMESPACE:-genetics}"
if [ -z "${REGISTRY:-}" ]; then
  echo "ERROR: REGISTRY must be set (e.g. \$GCP_REGION-docker.pkg.dev/\$GCP_PROJECT/genetics-results)"
  exit 1
fi

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
