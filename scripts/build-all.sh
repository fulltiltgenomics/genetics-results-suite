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

WORK_DIR=$(mktemp -d)
trap 'rm -rf "${WORK_DIR}"' EXIT

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

echo ""
echo "All images built and pushed."
