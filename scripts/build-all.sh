#!/bin/bash
set -euo pipefail

if [ -z "${REGISTRY:-}" ]; then
  echo "ERROR: REGISTRY must be set (e.g. \$GCP_REGION-docker.pkg.dev/\$GCP_PROJECT/genetics-results)"
  exit 1
fi
GITHUB_ORG="${GITHUB_ORG:-https://github.com/fulltiltgenomics}"

# branch overrides
FRONTEND_BRANCH="${FRONTEND_BRANCH:-llm}"
RESULTS_API_BRANCH="${RESULTS_API_BRANCH:-master}"
MCP_SERVER_BRANCH="${MCP_SERVER_BRANCH:-master}"
DB_API_BRANCH="${DB_API_BRANCH:-master}"
RAG_SERVICE_BRANCH="${RAG_SERVICE_BRANCH:-master}"

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
clone_repo genetics-rag-service "${RAG_SERVICE_BRANCH}"

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
  --build-arg DEPLOY_ENV=prod --build-arg DATA_SOURCE=finngen

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

echo ""
echo "All images built and pushed."
