#!/bin/bash
set -euo pipefail

# update a single service's container image
# usage: rollout.sh [--context <kubectl-context>] <service-name> [tag]
#
# ORDERING: roll out `bff` before `results-api`. results-api honours the
# X-Goog-Authenticated-User-Email header only from a caller that also presents
# INTERNAL_API_SECRET, and bff is what attaches it — a new results-api in front of an old bff
# 401s every browser request. The reverse order is safe to sit in. Rollback reverses it
# (results-api first). See README "Deploying the trusted-proxy marker".
#
# ORDERING (the same constraint, now three services):
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
#     so it starts anyway); that is now a startup failure instead. Note
#     what shipping mcp-server first buys: NOT continued service — that pod crash-loops with a
#     message naming the variable instead of 401ing every tool call with nothing local saying
#     why. Diagnosability, not availability.
# Nothing enforces this: `rollout.sh` takes one service, and `deploy.sh` restarts all of them in
# one unordered loop. It is a procedure, not a guard.

usage() {
  echo "Usage: rollout.sh [--context <kubectl-context>] <service-name> [tag]" >&2
}

# The context override is a FLAG, and that is the fix rather than the style. It used to be the
# environment variable ROLLOUT_CONTEXT, cross-checked only against the CURRENT context and
# never against the context DEPLOY_ENV implies. An `export ROLLOUT_CONTEXT=<prod>` typed alongside one deliberate production rollout
# therefore outlived that invocation and re-authorised itself on every later run from the same
# shell: a subsequent `DEPLOY_ENV=daly-staging ./scripts/rollout.sh bff`, still on the production
# cluster, was accepted and pushed the STAGING registry's image onto PRODUCTION. A flag cannot be
# exported and is not inherited, so the override is per-invocation by construction rather than by
# convention. ROLLOUT_CONTEXT is read nowhere below, so a stale export of it now does nothing.
OVERRIDE_CONTEXT=""
_override_given=0
_positional=()
while [ $# -gt 0 ]; do
  case "$1" in
    --context)
      [ $# -ge 2 ] || { echo "ERROR: --context requires a kubectl context name." >&2; usage; exit 1; }
      OVERRIDE_CONTEXT="$2"; _override_given=1; shift 2 ;;
    --context=*)
      OVERRIDE_CONTEXT="${1#--context=}"; _override_given=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    --)
      shift; while [ $# -gt 0 ]; do _positional+=("$1"); shift; done ;;
    -*)
      echo "ERROR: unknown option: $1" >&2; usage; exit 1 ;;
    *)
      _positional+=("$1"); shift ;;
  esac
done
if [ "${_override_given}" = "1" ] && [ -z "${OVERRIDE_CONTEXT}" ]; then
  echo "ERROR: --context was given an empty value; it must name the cluster you are about to mutate." >&2
  usage; exit 1
fi
set -- ${_positional[@]+"${_positional[@]}"}
SERVICE="${1:-}"
if [ -z "${SERVICE}" ]; then
  echo "ERROR: a service name is required." >&2
  usage; exit 1
fi
TAG="${2:-latest}"
if [ "$#" -gt 2 ]; then
  echo "ERROR: too many arguments: ${*:3}" >&2
  usage; exit 1
fi

# HAZARD, recorded here rather than guarded: NAMESPACE comes from the environment and the context
# guard below does NOT cover it. A stale `export NAMESPACE=...` survives into later invocations
# exactly the way the old ROLLOUT_CONTEXT export did, and mutates a different namespace on the
# right cluster — the same failure one level down from the one the guard closes. It is unguarded
# only because every deployment's tfvars sets `namespace = "genetics"`, so there is nothing to
# disagree about; whoever gives a deployment a different namespace should compare against the
# tfvars key here rather than assume this line is safe. The split is deliberate and worth stating
# plainly: the CLUSTER the calls below act on is frozen readonly by the guard, the NAMESPACE they
# act in is not, so `--context` is guaranteed and `-n` is taken on trust.
NAMESPACE="${NAMESPACE:-genetics}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# resolve the target deployment (DEPLOY_ENV) so REGISTRY defaults to that deployment's own
# repository, and so the context guard below has a tfvars to read the expected cluster from.
. "${SCRIPT_DIR}/lib/env.sh"
resolve_deploy_env
resolve_registry

# GUARD: refuse to act on a cluster this deployment's tfvars does not name. It runs here,
# before every cluster-contacting call below, so nothing has touched a cluster by the time it
# decides.
#
# The implementation — the kube_context reader and the compare-and-refuse — lives in
# lib/env.sh's require_kube_context, sourced above, because create-secrets.sh needs exactly the
# same guard and two copies of it would drift. The rationale for
# every part of its shape, and the four blind-validation rounds that produced it, are recorded
# there rather than repeated here. It reads OVERRIDE_CONTEXT (the --context flag parsed above),
# freezes its verdict into the readonly ACTING_CONTEXT that the three kubectl calls below pin, and
# exits 1 rather than returning on a refusal. The arguments are this
# script's own wording for the messages: a generic "this script" refusal is worse at the moment
# someone is one paste away from production.
require_kube_context \
  "rollout.sh" \
  "rollout" \
  "Rolling out here" \
  "Updating Services" \
  "    That is one deployment's images being pushed onto a cluster its own tfvars does not
    name. Proceeding, because you named the target; stop here if you did not mean this." \
  "      registry  = ${REGISTRY}"

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

# --context is PINNED on this and on both mutating calls below. The guard above verified the
# current context, but `kubectl config use-context` from another terminal writes the shared
# kubeconfig, so between the check and the call an unpinned kubectl would re-read it and act on
# whatever landed there. Pinning the verified name closes that TOCTOU window at no cost.
#
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
GET_ERR=$(kubectl --context "${ACTING_CONTEXT}" get deployment "${SERVICE}" -n "${NAMESPACE}" -o name 2>&1 >/dev/null) || GET_RC=$?
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
kubectl --context "${ACTING_CONTEXT}" set image deployment/"${SERVICE}" \
  "${CONTAINER_NAME}=${REGISTRY}/${IMAGE}:${TAG}" \
  -n "${NAMESPACE}"

kubectl --context "${ACTING_CONTEXT}" rollout status deployment/"${SERVICE}" -n "${NAMESPACE}" --timeout=300s
echo "Rollout complete for ${SERVICE}."
