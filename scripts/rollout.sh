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

usage() {
  echo "Usage: rollout.sh [--context <kubectl-context>] <service-name> [tag]" >&2
}

# The context override is a FLAG, and that is the fix rather than the style
# (genetics-results-suite-b1r). It used to be the environment variable ROLLOUT_CONTEXT,
# cross-checked only against the CURRENT context and never against the context DEPLOY_ENV
# implies. An `export ROLLOUT_CONTEXT=<prod>` typed alongside one deliberate production rollout
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
# tfvars key here rather than assume this line is safe.
NAMESPACE="${NAMESPACE:-genetics}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# resolve the target deployment (DEPLOY_ENV) so REGISTRY defaults to that deployment's own
# repository, and so the context guard below has a tfvars to read the expected cluster from.
. "${SCRIPT_DIR}/lib/env.sh"
resolve_deploy_env
resolve_registry

# ---------------------------------------------------------------------------------------------
# EVIDENCE: the deployment STATES its cluster, in one line of its own tfvars:
#
#     kube_context = "gke_daly-finngenie_us-central1-a_finngenie-staging"
#
# There is no derivation and no fallback (genetics-results-suite-b1r). The guard once rebuilt
# `gke_<project_id>_<zone>_<cluster_name>` from three tfvars keys, defaulting the two terraform
# gives defaults to — so a key that read as ABSENT produced `zone = europe-west1-b` with
# `cluster_name = finngenie`, and those name a PRODUCTION cluster in BOTH projects. Every way that
# reader could fail pointed at production.
#
# Nor does the reader below MODEL HCL, and that is the second lesson rather than a style choice.
# Four rounds of blind validation broke a reader that tried: each round found a new way for its
# `/* */` and heredoc state machine to mis-classify a LEGAL file, in both directions — a comment
# body read as live code, a `/*` inside a string refusing a good key. The analysis was the defect,
# so there is none. The rule needs no understanding of the language at all: the token
# `kube_context` must appear EXACTLY ONCE in the whole file — comments and strings included — and
# that one line must be a column-0 assignment of a plain quoted string. Everything else refuses.
# One stateless precondition sits in front of both: a file containing `/*` anywhere is refused
# outright, because a block comment is the only form that can present a commented-out key AS the
# one legal line. See read_kube_context.
#
# That is deliberately STRICTER than HCL. A file that mentions kube_context in a comment AND sets
# it is refused; that is one obvious edit, and the message names it. In exchange no construct —
# heredoc, block comment, string, CRLF, nesting — can make the reader return a value the operator
# did not write on that one line, because nothing is being interpreted. A refusal costs one line
# in a tfvars; a wrong answer costs a production cluster. `variable "kube_context"` is declared in
# terraform/variables.tf so terraform does not warn about it; no resource consumes it.
#
# lib/env.sh's tfvar() is deliberately not used and deliberately not changed: it is
# `grep -E "^[[:space:]]*<key>[[:space:]]*=" | head -1`, which matches an indented key and picks
# the first of a duplicate rather than refusing. It is shared with deploy.sh, build.sh,
# build-all.sh and create-secrets.sh, so hardening it is a different change with a different
# blast radius (genetics-results-suite-mrg). The weakness is recorded at that helper.
#
# The reader is not usable in a command substitution: the refusal reason has to survive into the
# message, and a subshell would drop it.
EXPECTED_CONTEXT=""
KUBE_CONTEXT_ERROR=""

# Sets EXPECTED_CONTEXT from the file's one and only kube_context, or returns 1 with
# KUBE_CONTEXT_ERROR. `\b` around the token is what keeps kube_context_old and old_kube_context
# from inflating the count: `_` is a word character, so neither has a boundary there.
read_kube_context() {
  local hits n hit lineno line re block
  [ -r "${TFVARS}" ] || { KUBE_CONTEXT_ERROR="${TFVARS} could not be read."; return 1; }
  # `/*` ANYWHERE refuses, before the count is even taken. `#` and `//` commented-out keys are
  # already handled: the token still counts and the line fails the column-0 form, so the guard
  # refuses. `/* */` was the one comment form that could smuggle a value past BOTH checks — a file
  # whose only kube_context sits inside a block comment presents one occurrence on a column-0 line
  # in the strict form, and is accepted. An earlier round called that harmless on the grounds that
  # it "never widens what is allowed"; that was wrong, and the error is worth stating. Without
  # reading the commented-out line the count would be ZERO and the guard would refuse outright, so
  # reading it converts "refuse always" into "act on the cluster that line names" — from nothing to
  # a production cluster. Commenting a cluster line out while switching targets is exactly what
  # operators do, so it is a realistic input rather than a contrived one. This is stateless on
  # purpose: no pairing, no nesting, no tracking of which construct a `/*` opens — that modelling
  # is what four rounds of validation broke. The cost is a rare false refusal (a `/*` inside a
  # string, or a genuine block comment elsewhere in the file), fixed by one edit the message names,
  # and it buys the removal of the last input that can hand this guard a cluster nobody meant.
  # It runs FIRST because the count is taken over text this guard has just said it cannot read: on
  # a file with both faults, "delete the other mentions" could send the operator to delete the live
  # key and keep the commented one.
  block="$(grep -nF -m1 -- '/*' "${TFVARS}")" || block=""
  if [ -n "${block}" ]; then
    KUBE_CONTEXT_ERROR="${TFVARS} line ${block%%:*} contains a /* block comment, and this guard refuses to reason about block comments at all rather than risk reading a commented-out cluster as the live one. Remove it or convert it; # and // comments are fine and need no change."
    return 1
  fi
  hits="$(grep -oE '\bkube_context\b' "${TFVARS}")" || hits=""
  n=0
  [ -z "${hits}" ] || n="$(printf '%s\n' "${hits}" | wc -l | tr -d '[:space:]')"
  if [ "${n}" = "0" ]; then
    KUBE_CONTEXT_ERROR="${TFVARS} does not mention kube_context anywhere."
    return 1
  fi
  if [ "${n}" != "1" ]; then
    KUBE_CONTEXT_ERROR="${TFVARS} mentions kube_context ${n} times; it must appear exactly once in the file, inside comments and strings included, because this guard deliberately does not parse HCL and will not guess which occurrence is the real one. Delete the other mentions."
    return 1
  fi
  hit="$(grep -nE '\bkube_context\b' "${TFVARS}")"
  lineno="${hit%%:*}"
  line="${hit#*:}"
  re='^kube_context[[:blank:]]*=[[:blank:]]*"([^"]*)"[[:blank:]]*(#.*)?$'
  if [[ ! "${line}" =~ $re ]]; then
    KUBE_CONTEXT_ERROR="${TFVARS} line ${lineno} is the only kube_context and it is not a column-0 assignment of a plain quoted string, the one form this guard reads: ${line}"
    return 1
  fi
  EXPECTED_CONTEXT="${BASH_REMATCH[1]}"
}

CURRENT_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
read_kube_context || EXPECTED_CONTEXT=""

# GUARD: refuse when the cluster kubectl is pointed at is not the cluster DEPLOY_ENV names
# (genetics-results-suite-b1r). It runs here, before every cluster-contacting call below, so
# nothing has touched a cluster by the time it decides.
#
# deploy.sh does not need this because it OVERWRITES the context from terraform output before its
# first apply, so the acting cluster cannot disagree with the resolved state backend. This script
# deliberately does NOT copy that: silently retargeting the operator's shell is its own hazard in
# a tool people run one service at a time without re-reading their prompt. Refusing leaves the
# shell exactly as it was and puts the decision back on the human.
#
# The echoed context this replaces was not a guard. There are three deployments across two
# projects and two of them are production; BOTH production clusters are named `finngenie` so only
# the project tells them apart, one of those projects is called `phewas-development`, and the daly
# production context differs from staging's by a trailing `-staging` alone. A string printed one
# line above a mutation does not survive that.
#
# HAZARD, recorded rather than fixed: this compares context NAMES. A kubeconfig entry named
# `..._finngenie-staging` whose `cluster.server` had been hand-edited to the production endpoint
# would pass and mutate production. Closing it means comparing the resolved `cluster.server`
# against terraform's endpoint output, which costs state access. Related: ROOT_DIR is honoured
# from the environment (lib/env.sh), so an inherited value relocates the tfvars this guard reads —
# that re-points the guard's EVIDENCE rather than its code, leaving it green while describing
# another checkout's cluster. Neither is reachable as configured here; both are notes for whoever
# meets one.
if [ -n "${OVERRIDE_CONTEXT}" ]; then
  if [ "${OVERRIDE_CONTEXT}" != "${CURRENT_CONTEXT}" ]; then
    echo "ERROR: --context does not name the context kubectl is actually on." >&2
    echo "         --context       = ${OVERRIDE_CONTEXT}" >&2
    echo "         current context = ${CURRENT_CONTEXT:-<none>}" >&2
    echo "       The flag exists to make an off-target rollout deliberate, so it has to spell out" >&2
    echo "       the cluster you are about to mutate. It is not a bypass switch." >&2
    exit 1
  fi
  if [ -z "${EXPECTED_CONTEXT}" ]; then
    echo "OVERRIDE: acting on ${CURRENT_CONTEXT} by explicit --context (env: ${DEPLOY_ENV:-default})."
    echo "  WARNING: nothing cross-checked that against the deployment. ${KUBE_CONTEXT_ERROR}"
  elif [ "${OVERRIDE_CONTEXT}" != "${EXPECTED_CONTEXT}" ]; then
    echo "*** OFF-TARGET ROLLOUT: the cluster and the deployment DISAGREE, and --context asked for it."
    echo "      acting on = ${CURRENT_CONTEXT}"
    echo "      expected  = ${EXPECTED_CONTEXT}  (DEPLOY_ENV=${DEPLOY_ENV:-<unset>}, kube_context in ${TFVARS})"
    echo "      registry  = ${REGISTRY}"
    echo "    That is one deployment's images being pushed onto a cluster its own tfvars does not"
    echo "    name. Proceeding, because you named the target; stop here if you did not mean this."
  else
    echo "OVERRIDE: acting on ${CURRENT_CONTEXT} by explicit --context (env: ${DEPLOY_ENV:-default})."
    echo "  It is the context this deployment expects, so the flag changed nothing."
  fi
elif [ -z "${EXPECTED_CONTEXT}" ]; then
  echo "ERROR: this deployment does not state which cluster it is, so rollout.sh refuses to act." >&2
  echo "       ${KUBE_CONTEXT_ERROR}" >&2
  echo "         current context = ${CURRENT_CONTEXT:-<none>}" >&2
  echo "       Two of the three deployments are production and both production clusters are named" >&2
  echo "       finngenie, so a guess here is a guess about production. THE FIX is one line in" >&2
  echo "       ${TFVARS}, at the start of a line, taken verbatim, and the file's ONLY mention of" >&2
  echo "       kube_context — this guard counts the token rather than parsing HCL:" >&2
  echo "" >&2
  echo "         kube_context = \"${CURRENT_CONTEXT:-gke_<project>_<zone>_<cluster>}\"" >&2
  echo "" >&2
  echo "       Check that name IS the cluster this deployment owns before pasting it —" >&2
  echo "       'kubectl config get-contexts -o name' lists them. rollout.sh's --context flag is" >&2
  echo "       not the fix: it authorises one deliberately off-target rollout, it does not tell" >&2
  echo "       the guard what this deployment is. See README, \"Updating Services\"." >&2
  exit 1
elif [ "${CURRENT_CONTEXT}" != "${EXPECTED_CONTEXT}" ]; then
  echo "ERROR: kubectl's current context is not the cluster this deployment names." >&2
  echo "         current  = ${CURRENT_CONTEXT:-<none: no current context, or kubectl unavailable>}" >&2
  echo "         expected = ${EXPECTED_CONTEXT}" >&2
  echo "         from     = kube_context in ${TFVARS} (DEPLOY_ENV=${DEPLOY_ENV:-<unset>})" >&2
  echo "       Rolling out here would update a different cluster than the one you selected, and" >&2
  echo "       two of the three deployments are production. The fix, ready to run:" >&2
  echo "" >&2
  echo "         kubectl config use-context ${EXPECTED_CONTEXT}" >&2
  echo "" >&2
  echo "       An off-target rollout is possible on purpose, with the --context flag, which must" >&2
  echo "       name the cluster being mutated; see README, \"Updating Services\". This message" >&2
  echo "       deliberately does not spell that command out: the only context it could fill in is" >&2
  echo "       the one you are being warned away from, and a paste-ready line is not a decision." >&2
  exit 1
else
  echo "Context: ${CURRENT_CONTEXT} (env: ${DEPLOY_ENV:-default})"
fi

# Every path that reaches here leaves CURRENT_CONTEXT non-empty: an empty current context either
# fails the --context equality test above or mismatches a non-empty EXPECTED_CONTEXT. So pinning
# it on the calls below cannot degrade into an empty --context= argument.

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
GET_ERR=$(kubectl --context "${CURRENT_CONTEXT}" get deployment "${SERVICE}" -n "${NAMESPACE}" -o name 2>&1 >/dev/null) || GET_RC=$?
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
kubectl --context "${CURRENT_CONTEXT}" set image deployment/"${SERVICE}" \
  "${CONTAINER_NAME}=${REGISTRY}/${IMAGE}:${TAG}" \
  -n "${NAMESPACE}"

kubectl --context "${CURRENT_CONTEXT}" rollout status deployment/"${SERVICE}" -n "${NAMESPACE}" --timeout=300s
echo "Rollout complete for ${SERVICE}."
