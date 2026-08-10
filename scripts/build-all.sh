#!/bin/bash
set -euo pipefail

if [ -z "${REGISTRY:-}" ]; then
  echo "ERROR: REGISTRY must be set (e.g. \$GCP_REGION-docker.pkg.dev/\$GCP_PROJECT/genetics-results)"
  exit 1
fi
GITHUB_ORG="${GITHUB_ORG:-https://github.com/fulltiltgenomics}"
RAG_SERVICE_ORG="${RAG_SERVICE_ORG:-https://github.com/ykjain}"

# branch overrides
FRONTEND_BRANCH="${FRONTEND_BRANCH:-master}"
RESULTS_API_BRANCH="${RESULTS_API_BRANCH:-master}"
MCP_SERVER_BRANCH="${MCP_SERVER_BRANCH:-master}"
DB_API_BRANCH="${DB_API_BRANCH:-master}"
RAG_SERVICE_BRANCH="${RAG_SERVICE_BRANCH:-deploy_jk}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# product/brand name: explicit APP_NAME env > app_name in terraform.tfvars > FinnGenie
APP_NAME="${APP_NAME:-$(grep -E '^\s*app_name\s*=' "${SCRIPT_DIR}/../terraform/terraform.tfvars" 2>/dev/null | sed 's/.*=\s*"\(.*\)"/\1/')}"
APP_NAME="${APP_NAME:-FinnGenie}"

SANDBOX_DIR="${SCRIPT_DIR}/../sandbox"

WORK_DIR=$(mktemp -d)
trap 'rm -rf "${WORK_DIR}" "${SANDBOX_DIR}/.sdk-src"' EXIT

clone_repo() {
  local name=$1 branch=$2
  echo "--- Cloning ${name} (branch: ${branch})"
  git clone --depth 1 --branch "${branch}" "${GITHUB_ORG}/${name}.git" "${WORK_DIR}/${name}"
}

clone_repo genetics-results-browser "${FRONTEND_BRANCH}"
clone_repo genetics-results-api "${RESULTS_API_BRANCH}"
clone_repo genetics-mcp-server "${MCP_SERVER_BRANCH}"
clone_repo genetics-results-db "${DB_API_BRANCH}"
echo "--- Cloning genetics-rag-service (branch: ${RAG_SERVICE_BRANCH})"
git clone --depth 1 --branch "${RAG_SERVICE_BRANCH}" "${RAG_SERVICE_ORG}/genetics-rag-service.git" "${WORK_DIR}/genetics-rag-service"

# tag includes date + short SHA from each repo's HEAD
tag_for() {
  local dir=$1
  echo "$(date +%Y%m%d).$(git -C "${dir}" rev-parse --short HEAD)"
}

build_and_push() {
  local name=$1 dir=$2 tag=$3
  shift 3
  echo "=== Building ${name} (tag: ${tag}) ==="
  docker build "$@" \
    -t "${REGISTRY}/${name}:${tag}" \
    -t "${REGISTRY}/${name}:latest" \
    "${dir}"
  docker push "${REGISTRY}/${name}:${tag}"
  docker push "${REGISTRY}/${name}:latest"
}

# frontend
TAG=$(tag_for "${WORK_DIR}/genetics-results-browser")
build_and_push genetics-results-browser "${WORK_DIR}/genetics-results-browser" "${TAG}" \
  --build-arg DEPLOY_ENV=prod --build-arg DATA_SOURCE=finngen --build-arg APP_NAME="${APP_NAME}"

# BFF (backend-for-frontend) — same repo as the frontend, separate Dockerfile, shares the frontend tag
build_and_push genetics-results-browser-bff "${WORK_DIR}/genetics-results-browser" "${TAG}" \
  -f "${WORK_DIR}/genetics-results-browser/bff/Dockerfile"

# results API
TAG=$(tag_for "${WORK_DIR}/genetics-results-api")
build_and_push genetics-results-api "${WORK_DIR}/genetics-results-api" "${TAG}" \
  --build-arg DEPLOY_ENV=prod

# MCP server (shared image for chat-backend and mcp-server)
TAG=$(tag_for "${WORK_DIR}/genetics-mcp-server")
build_and_push genetics-mcp-server "${WORK_DIR}/genetics-mcp-server" "${TAG}"

# DB API
TAG=$(tag_for "${WORK_DIR}/genetics-results-db")
build_and_push genetics-results-db "${WORK_DIR}/genetics-results-db" "${TAG}"

# RAG service
TAG=$(tag_for "${WORK_DIR}/genetics-rag-service")
build_and_push genetics-rag-service "${WORK_DIR}/genetics-rag-service" "${TAG}"

# monitor (local, no clone needed)
MONITOR_DIR="${SCRIPT_DIR}/monitor"
TAG="$(date +%Y%m%d).$(git -C "${SCRIPT_DIR}/.." rev-parse --short HEAD)"
build_and_push monitor "${MONITOR_DIR}" "${TAG}"

# keycloak (local build context: official Keycloak + bundled Apple extension JARs)
KEYCLOAK_DIR="${SCRIPT_DIR}/../keycloak"
TAG="$(date +%Y%m%d).$(git -C "${SCRIPT_DIR}/.." rev-parse --short HEAD)"
build_and_push keycloak "${KEYCLOAK_DIR}" "${TAG}"

# sandbox (local build context: distroless image that runs model-authored Python).
# The genetics SDK is not vendored here — it is staged out of the genetics-mcp-server
# clone above and pip-installed at build time, so the sandbox and mcp-server can never
# drift apart. See docs/code-execution-security.md, "Where the image lives".
MCP_DIR="${WORK_DIR}/genetics-mcp-server"
# the schema docs and SDK stubs (genetics-results-suite-4h6.13) gate the image the same
# way the SDK does: build-checks.py refuses to build with them, because a sandbox whose
# /genetics/schema says "PLACEHOLDER — not the real schema documentation" degrades
# silently. Detected here so a suite build still skips rather than fails, as it does for
# the SDK.
SANDBOX_PLACEHOLDERS=$(find "${SANDBOX_DIR}/schema" "${SANDBOX_DIR}/stubs" -name 'PLACEHOLDER*' 2>/dev/null | tr '\n' ' ')
if [ -n "${SANDBOX_PLACEHOLDERS}" ]; then
  echo ""
  echo "!!! SKIPPING sandbox: placeholders still staged (${SANDBOX_PLACEHOLDERS})."
  echo "!!! The image is not shippable with placeholder schema docs or SDK stubs"
  echo "!!! (genetics-results-suite-4h6.13). Every other image was built."
elif [ -d "${MCP_DIR}/src/genetics_mcp_server/sdk" ]; then
  rm -rf "${SANDBOX_DIR}/.sdk-src"
  mkdir -p "${SANDBOX_DIR}/.sdk-src"
  cp "${MCP_DIR}/pyproject.toml" "${MCP_DIR}/README.md" "${SANDBOX_DIR}/.sdk-src/"
  cp -r "${MCP_DIR}/src" "${SANDBOX_DIR}/.sdk-src/src"
  TAG="$(date +%Y%m%d).$(git -C "${SCRIPT_DIR}/.." rev-parse --short HEAD)"
  build_and_push sandbox "${SANDBOX_DIR}" "${TAG}" \
    --build-arg SDK_REF="$(git -C "${MCP_DIR}" rev-parse --short HEAD)"
  rm -rf "${SANDBOX_DIR}/.sdk-src"
else
  echo ""
  echo "!!! SKIPPING sandbox: ${MCP_SERVER_BRANCH} of genetics-mcp-server has no"
  echo "!!! src/genetics_mcp_server/sdk. The sandbox image is not shippable without"
  echo "!!! the SDK (genetics-results-suite-4h6.11). Every other image was built."
fi

echo ""
echo "All images built and pushed."
