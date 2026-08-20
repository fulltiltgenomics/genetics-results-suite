#!/bin/bash
set -euo pipefail

# build and push a single service's Docker image
# usage: build.sh <service-name>

SERVICE="${1:?Usage: build.sh <service-name>}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# resolve the target deployment (DEPLOY_ENV) and load its .env — that is where per-deployment
# branch overrides (e.g. FRONTEND_BRANCH=staging) live
. "${SCRIPT_DIR}/lib/env.sh"
resolve_deploy_env
load_deploy_env

# registry: derived from the resolved tfvars. A REGISTRY inherited from the shell must agree
# with DEPLOY_ENV, or the run stops — see resolve_registry in lib/env.sh.
resolve_registry

GITHUB_ORG="${GITHUB_ORG:-https://github.com/fulltiltgenomics}"
RAG_SERVICE_ORG="${RAG_SERVICE_ORG:-https://github.com/ykjain}"

# APP_NAME below is read from the deployment's tfvars, which exists only in the main checkout
# — warn before the fallback to FinnGenie happens silently (same preflight as build-all.sh)
"${SCRIPT_DIR}/check-worktree-paths.sh" --check || true

# product/brand name: explicit APP_NAME env > app_name in the deployment's tfvars > FinnGenie
# tfvar() swallows a missing file and a missing key (it is gitignored and main-checkout-only,
# so in a worktree grep exits 2 — pipefail would otherwise kill the build with no output before
# the FinnGenie fallback below was ever reached, genetics-results-suite-1xp)
APP_NAME="${APP_NAME:-$(tfvar app_name)}"
APP_NAME="${APP_NAME:-FinnGenie}"

echo "Building for ${DEPLOY_ENV:-default} -> ${REGISTRY}"

# sandbox: a local build context in this repo (like monitor and keycloak in
# build-all.sh), but one that still needs a clone — the genetics SDK lives in
# genetics-mcp-server and is pip-installed at build time rather than vendored here.
# See docs/code-execution-security.md, "Where the image lives".
if [ "${SERVICE}" = "sandbox" ]; then
  SANDBOX_DIR="${SCRIPT_DIR}/../sandbox"
  WORK_DIR=$(mktemp -d)
  trap 'rm -rf "${WORK_DIR}" "${SANDBOX_DIR}/.sdk-src"' EXIT

  MCP_BRANCH="${MCP_SERVER_BRANCH:-master}"
  MCP_DIR="${WORK_DIR}/genetics-mcp-server"
  echo "--- Cloning genetics-mcp-server (branch: ${MCP_BRANCH}) for the SDK"
  git clone --depth 1 --branch "${MCP_BRANCH}" \
    "${GITHUB_ORG}/genetics-mcp-server.git" "${MCP_DIR}"

  if [ ! -d "${MCP_DIR}/src/genetics_mcp_server/sdk" ]; then
    echo "ERROR: branch ${MCP_BRANCH} of genetics-mcp-server has no"
    echo "       src/genetics_mcp_server/sdk. The sandbox image is not shippable"
    echo "       without the genetics SDK (genetics-results-suite-4h6.11)."
    exit 1
  fi

  rm -rf "${SANDBOX_DIR}/.sdk-src"
  mkdir -p "${SANDBOX_DIR}/.sdk-src"
  cp "${MCP_DIR}/pyproject.toml" "${MCP_DIR}/README.md" "${SANDBOX_DIR}/.sdk-src/"
  cp -r "${MCP_DIR}/src" "${SANDBOX_DIR}/.sdk-src/src"

  # /genetics/schema and /genetics/sdk (genetics-results-suite-4h6.13). Regenerated from
  # configs/datasets.yaml and from the SDK clone staged above on every build, not read
  # from the committed copies, so the image cannot ship documentation older than the
  # canonical file it is derived from. Under `set -e` a failure aborts the build, which is
  # the intended behaviour here: this script builds the sandbox BY NAME, and an image whose
  # schema docs did not regenerate is exactly the silent degradation build-checks.py's
  # placeholder gate exists to prevent. build-all.sh skips instead, so a suite build stays
  # green.
  echo "--- Generating sandbox schema docs and SDK stubs"
  python3 "${SCRIPT_DIR}/gen-sandbox-docs.py" --sdk-src "${SANDBOX_DIR}/.sdk-src"

  # Regeneration alone only proves the generator ran. The properties that matter — every
  # view covered, the correctness rules still in configs/datasets.yaml, the stubs matching
  # the SDK's exported surface exactly — have no runtime symptom: an image that lost them
  # builds, deploys and serves confidently wrong SQL. Gated here for the same reason
  # deploy.sh gates on test-network-policies.py. Non-zero (1 = a property broke,
  # 2 = the harness could not run) aborts under `set -e`.
  echo "--- Checking sandbox schema docs and SDK stubs"
  python3 "${SCRIPT_DIR}/test-sandbox-docs.py" --sdk-src "${SANDBOX_DIR}/.sdk-src"

  TAG="$(date +%Y%m%d).$(git -C "${SCRIPT_DIR}/.." rev-parse --short HEAD)"
  echo "=== Building sandbox (tag: ${TAG}) ==="
  docker build --build-arg SDK_REF="$(git -C "${MCP_DIR}" rev-parse --short HEAD)" \
    -t "${REGISTRY}/sandbox:${TAG}" \
    -t "${REGISTRY}/sandbox:latest" \
    "${SANDBOX_DIR}"
  docker push "${REGISTRY}/sandbox:${TAG}"
  docker push "${REGISTRY}/sandbox:latest"

  echo ""
  echo "Image pushed: ${REGISTRY}/sandbox:${TAG}"
  echo ""
  echo "To roll out: REGISTRY=${REGISTRY} ./scripts/rollout.sh sandbox ${TAG}"
  exit 0
fi

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
  echo "Available services: ${!IMAGE_MAP[*]} sandbox"
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
