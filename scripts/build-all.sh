#!/bin/bash
set -euo pipefail

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

# branch overrides (set per deployment in .env.<DEPLOY_ENV>, e.g. staging)
FRONTEND_BRANCH="${FRONTEND_BRANCH:-master}"
RESULTS_API_BRANCH="${RESULTS_API_BRANCH:-master}"
MCP_SERVER_BRANCH="${MCP_SERVER_BRANCH:-master}"
DB_API_BRANCH="${DB_API_BRANCH:-master}"
RAG_SERVICE_BRANCH="${RAG_SERVICE_BRANCH:-deploy_jk}"

# see deploy.sh: core.hooksPath is not tracked, so warn if this checkout is unwired
"${SCRIPT_DIR}/install-git-hooks.sh" --check || true

# APP_NAME below is read from the deployment's tfvars, which exists only in the main checkout
# — warn before the fallback to FinnGenie happens silently
"${SCRIPT_DIR}/check-worktree-paths.sh" --check || true

# The generated tables in docs/code-execution-security.md are derived from supervisor.py, the
# sandbox manifest and the network policies. FATAL rather than warn-only, and here rather than
# in the sandbox branch below: it is offline, it costs milliseconds, and a security document
# whose limits table no longer matches the code is the failure the generator exists to prevent.
echo "--- Checking the generated tables in docs/code-execution-security.md"
python3 "${SCRIPT_DIR}/gen-doc-blocks.py" --check

# The N-copy count against docs/duplication-baseline.json. Warn-only, unlike the block check
# above: the count is taken over the SIBLING checkouts, whose state this build does not
# control, so a regrowth has to be loud without being able to stop an image build. Exit 2 is
# "could not count" (a repo is not checked out) and is reported as such rather than as clean.
echo "--- Checking duplication against the baseline snapshot"
DUP_RC=0
python3 "${SCRIPT_DIR}/check-duplication.py" --check || DUP_RC=$?
if [ "${DUP_RC}" -ne 0 ]; then
  echo "!!! duplication check exit ${DUP_RC} (1 = grew past docs/duplication-baseline.json,"
  echo "!!!  2 = could not count: a repo is not checked out, configs/twins.yaml does not say"
  echo "!!!  what an entry must, the baseline is missing/malformed or was measured over a"
  echo "!!!  different set of repos, or the passes now cover less of the trees than the"
  echo "!!!  baseline recorded). Not blocking the build."
fi

# product/brand name: explicit APP_NAME env > app_name in the deployment's tfvars > FinnGenie
# see build.sh: tfvar() swallows a missing tfvars rather than killing the build under pipefail,
# which is what the check-worktree-paths warning above already claims happens
#
APP_NAME="${APP_NAME:-$(tfvar app_name)}"
APP_NAME="${APP_NAME:-FinnGenie}"

echo "Building for ${DEPLOY_ENV:-default} -> ${REGISTRY}"

SANDBOX_DIR="${SCRIPT_DIR}/../sandbox"
# set to the reason by either sandbox skip branch below; read by the final summary
SANDBOX_SKIPPED=""

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
# The schema docs and SDK stubs are generated below rather
# than taken from the working tree, and they gate the image the same way the SDK does:
# build-checks.py refuses to build while a placeholder is staged, because a sandbox whose
# /genetics/schema says "PLACEHOLDER — not the real schema documentation" degrades
# silently. Failures are caught here so a suite build skips rather than fails, as it does
# for the SDK.
if [ -d "${MCP_DIR}/src/genetics_mcp_server/sdk" ]; then
  rm -rf "${SANDBOX_DIR}/.sdk-src"
  mkdir -p "${SANDBOX_DIR}/.sdk-src"
  cp "${MCP_DIR}/pyproject.toml" "${MCP_DIR}/README.md" "${SANDBOX_DIR}/.sdk-src/"
  cp -r "${MCP_DIR}/src" "${SANDBOX_DIR}/.sdk-src/src"
  # regenerate /genetics/schema and /genetics/sdk from configs/datasets.yaml and the SDK
  # clone, so the image never documents a schema older
  # than the canonical file. `|| true` under `set -e`: a suite build must not fail on the
  # sandbox, so the outcome is inspected and the sandbox skipped loudly instead — the same
  # shape as the SDK-missing branch. `build.sh sandbox` fails hard on the same condition.
  echo "--- Generating sandbox schema docs and SDK stubs"
  SANDBOX_DOCS_OK=0
  python3 "${SCRIPT_DIR}/gen-sandbox-docs.py" --sdk-src "${SANDBOX_DIR}/.sdk-src" || SANDBOX_DOCS_OK=$?
  # regeneration only proves the generator ran; test-sandbox-docs.py is what checks the
  # properties that have no runtime symptom (coverage, the correctness rules still being in
  # configs/datasets.yaml, the stubs matching the SDK's exported surface exactly). Folded
  # into the same skip branch, so a suite build stays green and says why.
  if [ "${SANDBOX_DOCS_OK}" -eq 0 ]; then
    echo "--- Checking sandbox schema docs and SDK stubs"
    python3 "${SCRIPT_DIR}/test-sandbox-docs.py" --sdk-src "${SANDBOX_DIR}/.sdk-src" \
      || SANDBOX_DOCS_OK=$?
  fi
  # a placeholder surviving generation means the generator did not own that file; the
  # image build would refuse anyway (build-checks.py), so stop here with a readable reason
  SANDBOX_PLACEHOLDERS=$(find "${SANDBOX_DIR}/schema" "${SANDBOX_DIR}/stubs" -name 'PLACEHOLDER*' 2>/dev/null | tr '\n' ' ')
  if [ "${SANDBOX_DOCS_OK}" -ne 0 ] || [ -n "${SANDBOX_PLACEHOLDERS}" ]; then
    SANDBOX_SKIPPED="the schema docs / SDK stubs could not be generated or verified"
    echo ""
    echo "!!! SKIPPING sandbox: could not generate or verify the schema docs and SDK stubs"
    echo "!!! (gen-sandbox-docs.py / test-sandbox-docs.py exit ${SANDBOX_DOCS_OK};"
    echo "!!!  placeholders: ${SANDBOX_PLACEHOLDERS:-none})."
    echo "!!! The image is not shippable with placeholder or stale schema docs."
    echo "!!! Every other image was built."
    rm -rf "${SANDBOX_DIR}/.sdk-src"
  else
    TAG="$(date +%Y%m%d).$(git -C "${SCRIPT_DIR}/.." rev-parse --short HEAD)"
    build_and_push sandbox "${SANDBOX_DIR}" "${TAG}" \
      --build-arg SDK_REF="$(git -C "${MCP_DIR}" rev-parse --short HEAD)"
    rm -rf "${SANDBOX_DIR}/.sdk-src"
  fi
else
  SANDBOX_SKIPPED="${MCP_SERVER_BRANCH} of genetics-mcp-server has no src/genetics_mcp_server/sdk"
  echo ""
  echo "!!! SKIPPING sandbox: ${MCP_SERVER_BRANCH} of genetics-mcp-server has no"
  echo "!!! src/genetics_mcp_server/sdk. The sandbox image is not shippable without"
  echo "!!! the SDK. Every other image was built."
fi

echo ""
if [ -z "${SANDBOX_SKIPPED}" ]; then
  echo "All images built and pushed."
  exit 0
fi

# THE SUMMARY LINE MUST NOT LIE. Both sandbox skips above are deliberate guards (an image with
# no SDK or with placeholder schema docs is not shippable)
# and they stay non-fatal for a suite build that does not deploy the sandbox. What was wrong is
# that the skip was a warning in the middle of a long build log and the script still signed off
# with "All images built and pushed." That was survivable only while deploy.sh refused to apply
# sandbox.yaml at all; now that the manifest carries args:, the next deploy applies a sandbox
# Deployment pointing at a tag this run never pushed. So: always restate the skip at the end, and
# make it FATAL when this deployment's tfvars actually enables the sandbox — the same
# sandbox_pool_enabled derivation deploy.sh uses for ENABLE_SANDBOX, read from the same file.
# An explicit ENABLE_SANDBOX in the environment wins, as it does there.
echo "!!! Every image EXCEPT the sandbox was built and pushed."
echo "!!! sandbox SKIPPED: ${SANDBOX_SKIPPED}"
TFVARS_SANDBOX_POOL="false"
if [ -f "${TFVARS}" ] && grep -Eq '^[[:space:]]*sandbox_pool_enabled[[:space:]]*=[[:space:]]*true' "${TFVARS}"; then
  TFVARS_SANDBOX_POOL="true"
fi
if [ "${ENABLE_SANDBOX:-${TFVARS_SANDBOX_POOL}}" = "true" ]; then
  if [ -n "${ENABLE_SANDBOX:-}" ]; then
    WHY="ENABLE_SANDBOX=${ENABLE_SANDBOX} in the environment"
  else
    WHY="sandbox_pool_enabled = true in ${TFVARS##*/}"
  fi
  echo "!!! The sandbox is enabled for this deployment (${WHY}), so scripts/deploy.sh WILL"
  echo "!!! apply k8s/deployments/sandbox.yaml and the pod would ImagePullBackOff on a tag"
  echo "!!! that was never pushed. Failing this build rather than handing the deploy a"
  echo "!!! missing image."
  exit 1
fi
echo "!!! The sandbox is not enabled for this deployment, so this is not fatal — but do not"
echo "!!! enable it until the sandbox image builds."
