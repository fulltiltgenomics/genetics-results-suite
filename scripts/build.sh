#!/bin/bash
set -euo pipefail

# build and push a single service's Docker image
# usage: build.sh <service-name>

SERVICE="${1:?Usage: build.sh <service-name>}"

if [ -z "${REGISTRY:-}" ]; then
  echo "ERROR: REGISTRY must be set (e.g. \$GCP_REGION-docker.pkg.dev/\$GCP_PROJECT/genetics-results)"
  exit 1
fi

GITHUB_ORG="${GITHUB_ORG:-https://github.com/fulltiltgenomics}"
RAG_SERVICE_ORG="${RAG_SERVICE_ORG:-https://github.com/ykjain}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# product/brand name: explicit APP_NAME env > app_name in terraform.tfvars > FinnGenie
APP_NAME="${APP_NAME:-$(grep -E '^\s*app_name\s*=' "${SCRIPT_DIR}/../terraform/terraform.tfvars" 2>/dev/null | sed 's/.*=\s*"\(.*\)"/\1/')}"
APP_NAME="${APP_NAME:-FinnGenie}"

# service → image name
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

# branch overrides (same env vars as build-all.sh)
declare -A BRANCH_MAP=(
  [frontend]="${FRONTEND_BRANCH:-master}"
  [bff]="${FRONTEND_BRANCH:-master}"
  [results-api]="${RESULTS_API_BRANCH:-master}"
  [chat-backend]="${MCP_SERVER_BRANCH:-master}"
  [mcp-server]="${MCP_SERVER_BRANCH:-master}"
  [db-api]="${DB_API_BRANCH:-master}"
  [rag-service]="${RAG_SERVICE_BRANCH:-deploy_jk}"
)

BRANCH="${BRANCH_MAP[$SERVICE]}"

# clone
WORK_DIR=$(mktemp -d)
trap 'rm -rf "${WORK_DIR}"' EXIT

REPO_ORG="${GITHUB_ORG}"
if [ "${SERVICE}" = "rag-service" ]; then
  REPO_ORG="${RAG_SERVICE_ORG}"
fi

# git repo to clone — usually the image name, but the bff shares the frontend repo
# (separate Dockerfile at bff/Dockerfile) so it clones genetics-results-browser instead.
REPO="${IMAGE}"
if [ "${SERVICE}" = "bff" ]; then
  REPO="genetics-results-browser"
fi

echo "--- Cloning ${REPO} (branch: ${BRANCH})"
git clone --depth 1 --branch "${BRANCH}" "${REPO_ORG}/${REPO}.git" "${WORK_DIR}/${REPO}"

# tag: date + short SHA
TAG="$(date +%Y%m%d).$(git -C "${WORK_DIR}/${REPO}" rev-parse --short HEAD)"

# per-service build args
BUILD_ARGS=()
case "${SERVICE}" in
  frontend)
    BUILD_ARGS+=(--build-arg DEPLOY_ENV=prod --build-arg DATA_SOURCE=finngen --build-arg APP_NAME="${APP_NAME}")
    ;;
  bff)
    BUILD_ARGS+=(-f "${WORK_DIR}/${REPO}/bff/Dockerfile")
    ;;
  results-api)
    BUILD_ARGS+=(--build-arg DEPLOY_ENV=prod)
    ;;
esac

# build and push
echo "=== Building ${IMAGE} (tag: ${TAG}) ==="
docker build "${BUILD_ARGS[@]}" \
  -t "${REGISTRY}/${IMAGE}:${TAG}" \
  -t "${REGISTRY}/${IMAGE}:latest" \
  "${WORK_DIR}/${REPO}"
docker push "${REGISTRY}/${IMAGE}:${TAG}"
docker push "${REGISTRY}/${IMAGE}:latest"

echo ""
echo "Image pushed: ${REGISTRY}/${IMAGE}:${TAG}"
echo ""
echo "To roll out: REGISTRY=${REGISTRY} ./scripts/rollout.sh ${SERVICE} ${TAG}"
