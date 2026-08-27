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
  # The image repo and the container are both literally `sandbox`, so CONTAINER_NAME below
  # resolves with no special-casing. Two things make this workload unlike the others, both
  # accommodated rather than special-cased: its Deployment is `strategy: Recreate` with
  # terminationGracePeriodSeconds: 130, so this rollout KILLS AN IN-FLIGHT EXECUTION and leaves
  # no sandbox for up to ~130s before the replacement is even scheduled (chat-backend surfaces
  # that as a tool error, not a wrong answer) — the 300s rollout-status timeout below is what
  # has to exceed that, and does, with room for the supervisor's prewarm and the readiness
  # probe's 5s + 10s. And it is GATED: deploy.sh applies sandbox.yaml only when ENABLE_SANDBOX
  # is on, so on most clusters there is nothing to roll; the existence check below says so in
  # words instead of letting kubectl emit a bare NotFound.
  [sandbox]=sandbox
  # Also gated (deploy.sh applies keycloak.yaml only when ENABLE_KEYCLOAK is true), and the
  # existence check below says so when it is off. It is here because the reason it used to be
  # excluded did not survive being stated: "built from THIS repo's working tree rather than a
  # cloned sibling" is true of `sandbox` and `monitor` too, so it never discriminated — and
  # keycloak is a Deployment named `keycloak`, whose container is named `keycloak`, running
  # ${REGISTRY}/keycloak:latest, which is exactly the shape this script handles.
  [keycloak]=keycloak
)
# `monitor` is the one deliberate absence, for a reason that does discriminate: it is a CronJob,
# so `kubectl set image deployment/monitor` cannot address it at all. Update it with deploy.sh.

IMAGE="${IMAGE_MAP[$SERVICE]:-}"

if [ -z "${IMAGE}" ]; then
  echo "Unknown service: ${SERVICE}"
  echo "Available services: ${!IMAGE_MAP[*]}"
  exit 1
fi

CONTAINER_NAME="${SERVICE}"

# A missing Deployment is an ordinary state for `sandbox` (deploy.sh applies it only when
# ENABLE_SANDBOX is on) and a surprise for everything else, but in both cases `kubectl set image`
# answers with a bare NotFound that reads like a broken cluster. Say which it is.
# THE TWO FAILURES ARE KEPT APART, and kubectl's own words are kept. The `2>&1` this replaced
# discarded them, so an expired credential, a wrong or absent context and an unreachable API
# server all printed "no Deployment '<service>' in namespace genetics on this context" — an
# assertion about the cluster made by a command that never reached one, for every service in the
# map rather than only the gated ones. Same three-way answer scripts/test-network-policies.py's
# live_sandbox_deployment() gives: found / definitely absent / could not ask.
GET_RC=0
GET_ERR=$(kubectl get deployment "${SERVICE}" -n "${NAMESPACE}" -o name 2>&1 >/dev/null) || GET_RC=$?
if [ "${GET_RC}" != "0" ]; then
  case "${GET_ERR}" in
    *"Error from server (NotFound)"*|*"(NotFound)"*)
      echo "Not deployed: no Deployment '${SERVICE}' in namespace ${NAMESPACE} on this context."
      if [ "${SERVICE}" = "keycloak" ]; then
        echo "  Keycloak is gated: scripts/deploy.sh applies k8s/deployments/keycloak.yaml only for"
        echo "  a deployment whose config_profile enables the identity broker (ENABLE_KEYCLOAK)."
      fi
      if [ "${SERVICE}" = "sandbox" ]; then
        echo "  The sandbox is gated: scripts/deploy.sh applies k8s/deployments/sandbox.yaml only when"
        echo "  ENABLE_SANDBOX is true, derived from sandbox_pool_enabled = true in the deployment's"
        echo "  tfvars. Run a full scripts/deploy.sh with the gate on to create it; there is nothing"
        echo "  to roll out until then."
      fi
      ;;
    *)
      echo "Could not ask the cluster whether Deployment '${SERVICE}' exists in namespace ${NAMESPACE}."
      echo "  This is NOT evidence that the service is missing — the query itself failed, so the"
      echo "  context, the credentials or the API server is the thing to look at first."
      echo "  kubectl said: ${GET_ERR}"
      ;;
  esac
  exit 1
fi

echo "Updating ${SERVICE} to ${REGISTRY}/${IMAGE}:${TAG}"
kubectl set image deployment/"${SERVICE}" \
  "${CONTAINER_NAME}=${REGISTRY}/${IMAGE}:${TAG}" \
  -n "${NAMESPACE}"

kubectl rollout status deployment/"${SERVICE}" -n "${NAMESPACE}" --timeout=300s
echo "Rollout complete for ${SERVICE}."
