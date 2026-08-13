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
#
# ORDERING (the same constraint, now three services — genetics-results-suite-rhh):
#   bff  ->  mcp-server  ->  results-api
# results-api's ANONYMOUS_SURFACE_MINIMAL defaults ON, which stops /api/v1/auth,
# /api/v1/variant_sets, /api/v1/variant_sets/{name} and /api/v1/rsid/variants (GET+POST) from
# answering a caller that presents nothing at all. Two callers presently present nothing:
#   * the browser, on exactly those routes. bff attaches the secret only on its TYPED upstream
#     routes (bff/upstream.ts); these six go through the GENERIC passthrough
#     (bff/passthrough.ts), which does not. Measured through the deployed bff with no headers:
#     /api/v1/auth still answers 200. The passthrough fix is UNDEPLOYED — it lives only in
#     genetics-results-browser's db-only-architecture worktree. Ship that bff first or the
#     browser 401s on its login-state probe.
#   * an mcp-server pod whose INTERNAL_API_SECRET is unset (its secretKeyRef is optional: true,
#     so it starts anyway). genetics-results-suite-618 turned that into a startup failure. Note
#     what shipping mcp-server first buys: NOT continued service — that pod crash-loops with a
#     message naming the variable instead of 401ing every tool call with nothing local saying
#     why. Diagnosability, not availability.
# Nothing enforces this: `rollout.sh` takes one service, and `deploy.sh` restarts all of them in
# one unordered loop. It is a procedure, not a guard.

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
