#!/bin/bash
set -euo pipefail

# create Kubernetes secrets for the genetics results suite
# Every key below EXCEPT anthropic-api-key is reused from the cluster when its env var is
# not set, so a re-run with only some of them exported never blanks the rest. ANTHROPIC_API_KEY
# is the one exception: it is never read back from the cluster and must ALWAYS be exported,
# otherwise the script aborts before writing anything. Keys absent both in the environment and
# in the cluster get a fresh random value where marked "generated" — that covers the secrets
# this deployment mints for itself; the optional THIRD-PARTY API keys stay EMPTY rather than
# being invented (a random string is a plausible-looking key that fails only at call time), and
# the keycloak usernames fall back to their literal defaults.
# set these environment variables before running:
#   ANTHROPIC_API_KEY     - Anthropic API key for chat backend (ALWAYS required, including on
#                           re-runs: it is the one key never read back from the cluster)
#   OPENAI_API_KEY        - OpenAI API key (optional for chat backend, required for rag-service)
#   TAVILY_API_KEY        - Tavily API key (optional)
#   PERPLEXITY_API_KEY    - Perplexity API key (optional)
#   COHERE_API_KEY        - Cohere API key for RAG service embeddings (required only when ENABLE_RAG=true)
#   EXTERNAL_MCP_SERVERS  - comma-separated external MCP server URLs for chat-backend (optional)
#   ADMIN_USERS           - comma-separated admin email addresses (optional)
#   INTERNAL_API_SECRET   - shared secret for internal service-to-service auth (reused from the existing secret if not set, generated on first install)
#   SANDBOX_TOKEN_SIGNING_KEY  - signing key for per-execution sandbox tokens (reused if not set, generated on first install; see docs/code-execution-security.md §4)
#   GATEWAY_IDENTITY_SECRET    - auth-gateway -> chat-backend provenance secret gating sandbox dispatch
#                           (reused if not set, generated on first install; MUST stay distinct from INTERNAL_API_SECRET)
#   MCP_API_KEY           - bearer token mcp-server requires for its sse/streamable-http transports;
#                           it refuses to start without one. Reused from the existing secret if not
#                           set, generated on first install — it's a secret this deployment mints
#                           for itself, not a third-party credential, so a generated value is valid.
#   SLACK_WEBHOOK_URL     - Slack webhook URL for alerting (optional)
#   OAUTH2_PROXY_CLIENT_ID     - oauth2-proxy OAuth client id (Google client id on finngen; Keycloak OIDC client id on daly). reused from the cluster if not set; required on first install
#   OAUTH2_PROXY_CLIENT_SECRET - matching OAuth client secret (reused if not set; required on first install)
#   OAUTH2_PROXY_COOKIE_SECRET - oauth2-proxy session cookie secret (reused from the cluster if not set, generated on first install — never rotated on re-run, so sessions survive)

usage() {
  echo "Usage: create-secrets.sh [--context <kubectl-context>]" >&2
}

# The context override is a FLAG and deliberately NOT an environment variable
# (genetics-results-suite-mrg, carried over from -b1r where the difference was measured). An
# `export` typed alongside one deliberate off-target run outlives that invocation and
# re-authorises every later one from the same shell; in rollout.sh's case that was driven to a
# real `kubectl set image` on production. Two things make the override per-invocation HERE, and
# both are properties of this file rather than of flags in general: the assignment below resets
# OVERRIDE_CONTEXT to "" before parsing, so an inherited export is discarded rather than obeyed;
# and the guard runs before load_deploy_env, so a line in `.env.<env>` cannot set it either. That
# second one was a real bypass while the two calls were the other way round — a file outlives a
# shell, so it is worse than the export this comment was written about. It is the same `--context` spelling
# rollout.sh uses, and it obeys the same rule: it must name the context kubectl is ALREADY on, so
# it can confirm where you are and never redirect you.
#
# This script takes no positional arguments — every input is an environment variable (see the
# header) — so anything left over is a mistake worth stopping for rather than ignoring.
OVERRIDE_CONTEXT=""
_override_given=0
while [ $# -gt 0 ]; do
  case "$1" in
    --context)
      [ $# -ge 2 ] || { echo "ERROR: --context requires a kubectl context name." >&2; usage; exit 1; }
      OVERRIDE_CONTEXT="$2"; _override_given=1; shift 2 ;;
    --context=*)
      OVERRIDE_CONTEXT="${1#--context=}"; _override_given=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "ERROR: unexpected argument: $1" >&2
      echo "       create-secrets.sh takes no positional arguments; its inputs are the environment" >&2
      echo "       variables listed at the top of this script." >&2
      usage; exit 1 ;;
  esac
done
if [ "${_override_given}" = "1" ] && [ -z "${OVERRIDE_CONTEXT}" ]; then
  echo "ERROR: --context was given an empty value; it must name the cluster you are about to write to." >&2
  usage; exit 1
fi

# HAZARD, recorded here rather than guarded, identically to rollout.sh's copy of this line: the
# context guard below does NOT cover NAMESPACE. A stale `export NAMESPACE=...` survives into later
# invocations exactly the way an exported context override would, and writes these Secrets into a
# different namespace on the right cluster — the same failure one level down from the one the guard
# closes, and here it means the running pods keep the old values while the new ones sit somewhere
# nothing mounts them. Unguarded only because every deployment's tfvars sets `namespace =
# "genetics"`, so there is nothing to disagree about; whoever gives a deployment a different
# namespace should compare against that tfvars key here rather than assume this line is safe.
# Note also that the OFF-TARGET banner prints this value as resolved HERE: `.env` is sourced after
# the guard (deliberately), so a NAMESPACE set in `.env` is not what that line shows.
#
# BE PRECISE ABOUT THE SPLIT, because the two halves of `--context ... --namespace=...` below are
# now protected differently. The CLUSTER is decided by the guard and frozen readonly, so nothing
# sourced afterwards can move it. The NAMESPACE is neither guarded nor frozen: this assignment
# happens before `.env` is sourced, but NAMESPACE is a plain global, so a `NAMESPACE=` line in
# `.env` DOES change where these Secrets land — deliberately, since `.env` is this deployment's own
# config file and setting a namespace there is a legitimate thing to do. So: right cluster,
# guaranteed; right namespace, on trust. That is a half-guarantee on purpose, not an oversight.
NAMESPACE="${NAMESPACE:-genetics}"
ENABLE_RAG="${ENABLE_RAG:-false}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# resolve the target deployment (DEPLOY_ENV), which is all the guard below needs: it sets TFVARS.
. "${SCRIPT_DIR}/lib/env.sh"
resolve_deploy_env

# GUARD: refuse to write Secrets into a cluster this deployment's tfvars does not name
# (genetics-results-suite-mrg). It sits here, immediately after the deployment is resolved and
# ahead of EVERY cluster-contacting call below — including the `kubectl get secret` reads in
# secret_key(), which run long before the first write.
#
# IT RUNS BEFORE load_deploy_env, AND THAT ORDER IS THE GUARD'S OWN INTEGRITY RATHER THAN TIDINESS.
# load_deploy_env does `set -a; . .env.<env>; set +a` — arbitrary shell from a file this script does
# not control — and TFVARS is already exported by then. Sourcing first was driven two ways: a
# `TFVARS=` line in .env re-pointed the guard at ANOTHER deployment's tfvars, so it validated the
# current context against a cluster nobody selected and printed an ordinary green success line
# before reading and writing Secrets on production; and an `OVERRIDE_CONTEXT=` line set the very
# variable the --context flag exists to keep per-invocation, turning the hard refusal into the
# off-target-and-proceed path. Both are closed by deciding before the file is read. Nothing between
# the two calls is needed here: the guard reads TFVARS, DEPLOY_ENV, OVERRIDE_CONTEXT and kubectl's
# current context, none of which come from .env.
#
# RUNNING FIRST IS NOT SUFFICIENT ON ITS OWN, and an earlier revision of this comment reasoned as
# if it were: it argued only about the guard's INPUTS and about when its OUTPUT is first READ
# ("CURRENT_CONTEXT is first used by secret_key() further down"), which left the case of .env
# WRITING that output unexamined. A `CURRENT_CONTEXT=` line in .env is exactly that case, and it
# sent all three Secret writes below to a production cluster behind a success line that was
# truthful about what the guard had checked. Deciding first is therefore paired with FREEZING the
# decision: require_kube_context ends by making the verdict `readonly ACTING_CONTEXT`, which no
# later-sourced shell can reassign or unset, and every kubectl call below pins THAT. See the
# freeze block at the end of require_kube_context in lib/env.sh.
#
# The implementation is shared with rollout.sh: lib/env.sh's require_kube_context, sourced above,
# holds the kube_context reader, the compare-and-refuse and the reasoning behind both. One copy on
# purpose — two guards over two blast radii would drift, and this one's is the worse of them.
#
# It replaced an echo. This script used to print `kubectl config current-context` one line above
# writing genetics-secrets, which is not a guard: rotating internal-api-secret breaks every
# running pod, the daly production context differs from staging's by a trailing `-staging` alone,
# and both production clusters are literally named finngenie.
require_kube_context \
  "create-secrets.sh" \
  "secret write" \
  "Writing secrets here" \
  "3. Create secrets" \
  "    That is one deployment's secrets being written into a cluster its own tfvars does not
    name. Proceeding, because you named the target; stop here if you did not mean this." \
  "      namespace = ${NAMESPACE}"

# load this deployment's .env, so the secrets written here come from the same file the matching
# deploy.sh run will use. Deliberately AFTER the guard — see the block above.
load_deploy_env

# RE-ASSERT THE CONTEXT AFTER SOURCING .env, AND BE HONEST ABOUT WHAT THIS IS.
# The freeze above stops `.env` REWRITING the guard's verdict. It does nothing about `.env`
# changing what that verdict MEANS, and three ways were driven (genetics-results-suite-mrg):
#   - a `kubectl() { ... }` shell function in `.env` rewrites the `--context` after the pin has
#     already expanded faithfully;
#   - a `PATH=` line puts a different `kubectl` binary in front;
#   - a `KUBECONFIG=` line resolves the same context NAME through a different file, i.e. a
#     different cluster.
# All three leave the guard's green line above entirely truthful and send every write elsewhere.
#
# THIS IS AN ACCIDENT DETECTOR, NOT A SECURITY BOUNDARY — see the threat model at
# require_kube_context in lib/env.sh. Asking kubectl the same question a second time catches the
# two vectors an operator can plausibly cause by accident (`PATH=` genuinely so, `KUBECONFIG=`
# possibly), because both change what the second question RESOLVES TO while the frozen answer
# stays put. It does NOT catch a `kubectl()` shell function: that function answers this question
# too, and lies consistently. Nobody writes such a function into a deployment `.env` by accident,
# and anyone who writes one deliberately owns the process already.
#
# It lives here and NOT in require_kube_context: rollout.sh never calls load_deploy_env, so it has
# no window to re-check and must not pay for a second `kubectl config current-context` call.
_post_env_context="$(kubectl config current-context 2>/dev/null || true)"
if [ "${_post_env_context}" != "${ACTING_CONTEXT}" ]; then
  echo "ERROR: kubectl's current context changed while ${ENV_FILE} was being sourced." >&2
  echo "         checked and frozen = ${ACTING_CONTEXT}" >&2
  echo "         now resolves to    = ${_post_env_context:-<none>}" >&2
  echo "       Nothing ran in between except that file, which is sourced as arbitrary shell." >&2
  echo "       A PATH= or KUBECONFIG= line in it is the usual cause: the guard above verified one" >&2
  echo "       cluster and every Secret write below would have gone to another, behind a success" >&2
  echo "       line that was truthful about what was checked. Remove that line from ${ENV_FILE}," >&2
  echo "       or set it outside the file so the guard sees it before it decides." >&2
  exit 1
fi
unset _post_env_context

# the keycloak broker (and its secrets) is per-profile: on for daly, off otherwise.
# derive from the tfvars resolve_deploy_env picked (it has already refused if that file is
# missing, so the "profile is unknowable" case cannot reach here); override with
# ENABLE_KEYCLOAK, or CONFIG_PROFILE to name the profile outright.
PROFILE="${CONFIG_PROFILE:-$(tfvar config_profile)}"

# enforce the "(daly|finngen)" the message below advertises. Anything else — a typo, a case
# slip, or a tfvars with no config_profile line (tfvar yields empty rather than aborting) —
# would otherwise fall straight through to ENABLE_KEYCLOAK=false and skip keycloak-secrets
# silently, leaving a daly deploy with a keycloak pod that cannot start.
# Same allowed set terraform/variables.tf validates.
case "${PROFILE}" in
  daly|finngen) ;;
  *)
    echo "ERROR: unrecognised config profile: ${PROFILE:-<empty>}"
    echo "Expected one of: daly, finngen."
    echo "An unrecognised profile would silently skip keycloak-secrets, which only daly needs,"
    echo "and the deploy would then start keycloak with no secret to mount."
    echo "Set CONFIG_PROFILE (daly|finngen), or fix config_profile in: ${TFVARS}"
    exit 1
    ;;
esac
ENABLE_KEYCLOAK="${ENABLE_KEYCLOAK:-$([ "${PROFILE}" = "daly" ] && echo true || echo false)}"

echo "Creating genetics-secrets in namespace ${NAMESPACE} (env: ${DEPLOY_ENV:-default})..."

# --context is PINNED on every kubectl invocation from here down (genetics-results-suite-mrg),
# the same way rollout.sh pins its three, and it pins ACTING_CONTEXT — the guard's frozen readonly
# verdict — rather than the plain CURRENT_CONTEXT the guard also leaves behind. The guard above
# verified the current context, but `kubectl config use-context` from another terminal rewrites the
# shared kubeconfig, so between the check and the call an unpinned kubectl would re-read it and
# read — or write — somewhere else; and pinning a variable that `.env` has since been sourced over
# would reintroduce the same gap from the other end. The `create secret ... --dry-run=client` halves of the three pipelines below do not
# contact a cluster at all and are pinned anyway: pinning uniformly means a later edit that drops
# a --dry-run flag cannot silently unpin the call it turns into a write.
#
# read one key out of a secret. The old form (`kubectl ... 2>/dev/null | base64 -d || true`)
# collapsed three different outcomes into "empty", so a wrong kubeconfig context, an RBAC
# denial or a transient API-server error was indistinguishable from "never set" — and the
# caller would then rotate internal-api-secret (breaking every running pod) or blank an
# optional key. Here only a genuine absence yields empty; anything else aborts the script.
# The pipeline is split out because `a | b` reports b's status, hiding kubectl's.
# stderr goes to a scratch file rather than being merged into stdout: kubectl exits 0 while
# still writing to stderr (auth-plugin deprecation notices, kubelogin chatter), and merging
# would splice those lines into the base64 payload — an abort at best, a corrupted value
# written back over the live secret on a base64 that skips non-alphabet bytes.
SECRET_KEY_ERR="$(mktemp)"
trap 'rm -f "${SECRET_KEY_ERR}"' EXIT
secret_key() {
  local secret="$1" key="$2" out err rc=0
  out="$(kubectl --context "${ACTING_CONTEXT}" get secret "${secret}" --namespace="${NAMESPACE}" \
    -o jsonpath="{.data.${key}}" 2>"${SECRET_KEY_ERR}")" || rc=$?
  if [ "${rc}" -ne 0 ]; then
    err="$(cat "${SECRET_KEY_ERR}")"
    # the secret not existing yet is the legitimate first-install case
    case "${err}" in
      *NotFound*) return 0 ;;
    esac
    echo "ERROR: kubectl get secret ${secret} (key ${key}) failed with exit ${rc}:" >&2
    echo "  ${err}" >&2
    echo "Refusing to continue: an unreachable cluster is not the same as an unset key," >&2
    echo "and proceeding would rotate internal-api-secret and blank the optional secrets." >&2
    echo "Check your kubeconfig context and RBAC, then re-run." >&2
    return 1
  fi
  if [ -n "${out}" ]; then
    printf '%s' "${out}" | base64 -d || {
      echo "ERROR: key ${key} of secret ${secret} is not valid base64; refusing to continue." >&2
      return 1
    }
  fi
}

# an explicit env value wins, otherwise keep whatever is already in the cluster, otherwise
# leave it empty. Deliberately reuse-then-EMPTY, not the reuse-then-generate used for the four
# genetics-secrets keys this deployment mints for itself (internal-api-secret,
# sandbox-token-signing-key, gateway-identity-secret, mcp-api-key — and, further down and
# outside this Secret, oauth2-proxy's cookie-secret and keycloak's db-password/admin-password):
# a random value for a third-party API key would look valid and fail only at call time, and
# admin-users / external-mcp-servers have no meaningful random value at all.
reuse_optional() {
  local var="$1" key="$2" val
  [ -z "${!var:-}" ] || return 0
  # assigned in two steps on purpose: a command substitution inside a *command's* arguments
  # (printf -v) would discard secret_key's failure, an assignment propagates it to set -e.
  val="$(secret_key genetics-secrets "${key}")"
  printf -v "${var}" '%s' "${val}"
}
reuse_optional OPENAI_API_KEY       openai-api-key
reuse_optional TAVILY_API_KEY       tavily-api-key
reuse_optional PERPLEXITY_API_KEY   perplexity-api-key
reuse_optional COHERE_API_KEY       cohere-api-key
reuse_optional EXTERNAL_MCP_SERVERS external-mcp-servers
reuse_optional ADMIN_USERS          admin-users
reuse_optional SLACK_WEBHOOK_URL    slack-webhook-url

# required
: "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY}"
# cohere is only needed by rag-service; require it only when RAG is deployed. Checked after
# the reuse above so a value already in the cluster satisfies it on a re-run.
if [ "${ENABLE_RAG}" = "true" ]; then
  : "${COHERE_API_KEY:?Set COHERE_API_KEY (required when ENABLE_RAG=true)}"
fi

# reuse the internal API secret to avoid breaking service-to-service auth for
# already-running pods: an explicit env value wins, otherwise reuse the value
# already in the cluster, otherwise generate a fresh one (first install).
INTERNAL_API_SECRET="${INTERNAL_API_SECRET:-$(secret_key genetics-secrets internal-api-secret)}"
INTERNAL_API_SECRET="${INTERNAL_API_SECRET:-$(openssl rand -base64 32)}"

# signing key for the per-execution sandbox tokens (docs/code-execution-security.md section 4).
# Deliberately a distinct key from INTERNAL_API_SECRET: chat-backend signs, db-api and
# results-api verify, and the sandbox holds neither. Same reuse-or-generate rule as above —
# regenerating it would invalidate every token in flight, which is at most one execution.
SANDBOX_TOKEN_SIGNING_KEY="${SANDBOX_TOKEN_SIGNING_KEY:-$(secret_key genetics-secrets sandbox-token-signing-key)}"
SANDBOX_TOKEN_SIGNING_KEY="${SANDBOX_TOKEN_SIGNING_KEY:-$(openssl rand -base64 32)}"

# auth-gateway's provenance secret (genetics-results-suite-4h6.84). A THIRD distinct secret,
# and the distinctness is the security property: auth-gateway sends it on the two locations
# that proxy to chat-backend, chat-backend gates sandbox dispatch on it, and mcp-server and
# results-api — which hold internal-api-secret by design and can reach chat-backend:8000 —
# hold this one not at all. Never derive it from INTERNAL_API_SECRET. Same reuse-or-generate
# rule; rotating it costs at most a gateway+backend restart.
GATEWAY_IDENTITY_SECRET="${GATEWAY_IDENTITY_SECRET:-$(secret_key genetics-secrets gateway-identity-secret)}"
GATEWAY_IDENTITY_SECRET="${GATEWAY_IDENTITY_SECRET:-$(openssl rand -base64 32)}"

# bearer token mcp-server requires for its sse/streamable-http transports (it refuses to
# start without one — no escape hatch). Unlike the third-party keys above, this one is not
# a credential issued by an outside service: it's a shared secret this deployment mints for
# itself, so a generated value is authoritative by construction rather than a plausible-
# looking guess. Same reuse-or-generate rule as the three secrets above.
MCP_API_KEY="${MCP_API_KEY:-$(secret_key genetics-secrets mcp-api-key)}"
MCP_API_KEY="${MCP_API_KEY:-$(openssl rand -hex 32)}"

kubectl --context "${ACTING_CONTEXT}" create secret generic genetics-secrets \
  --namespace="${NAMESPACE}" \
  --from-literal=anthropic-api-key="${ANTHROPIC_API_KEY}" \
  --from-literal=openai-api-key="${OPENAI_API_KEY:-}" \
  --from-literal=tavily-api-key="${TAVILY_API_KEY:-}" \
  --from-literal=perplexity-api-key="${PERPLEXITY_API_KEY:-}" \
  --from-literal=mcp-api-key="${MCP_API_KEY}" \
  --from-literal=cohere-api-key="${COHERE_API_KEY:-}" \
  --from-literal=external-mcp-servers="${EXTERNAL_MCP_SERVERS:-}" \
  --from-literal=admin-users="${ADMIN_USERS:-}" \
  --from-literal=internal-api-secret="${INTERNAL_API_SECRET}" \
  --from-literal=sandbox-token-signing-key="${SANDBOX_TOKEN_SIGNING_KEY}" \
  --from-literal=gateway-identity-secret="${GATEWAY_IDENTITY_SECRET}" \
  --from-literal=slack-webhook-url="${SLACK_WEBHOOK_URL:-}" \
  --dry-run=client -o yaml | kubectl --context "${ACTING_CONTEXT}" apply -f -

echo "genetics-secrets created/updated."

# oauth2-proxy-secrets: OAuth client creds + session cookie secret, consumed only by the
# oauth2-proxy deployment (both profiles). client-id/client-secret are the Google OAuth client
# on finngen, or the Keycloak OIDC client on daly. As with the other secrets, an explicit env
# value wins, else we reuse what's already in the cluster. The cookie-secret is reused-or-
# generated (NEVER regenerated on a re-run) — rotating it would invalidate every active session.
reuse_o2p() { secret_key oauth2-proxy-secrets "$1"; }
OAUTH2_PROXY_CLIENT_ID="${OAUTH2_PROXY_CLIENT_ID:-$(reuse_o2p client-id)}"
OAUTH2_PROXY_CLIENT_SECRET="${OAUTH2_PROXY_CLIENT_SECRET:-$(reuse_o2p client-secret)}"
: "${OAUTH2_PROXY_CLIENT_ID:?Set OAUTH2_PROXY_CLIENT_ID (OAuth client id) — required on first install}"
: "${OAUTH2_PROXY_CLIENT_SECRET:?Set OAUTH2_PROXY_CLIENT_SECRET — required on first install}"
OAUTH2_PROXY_COOKIE_SECRET="${OAUTH2_PROXY_COOKIE_SECRET:-$(reuse_o2p cookie-secret)}"
OAUTH2_PROXY_COOKIE_SECRET="${OAUTH2_PROXY_COOKIE_SECRET:-$(openssl rand -base64 32 | head -c 32)}"

kubectl --context "${ACTING_CONTEXT}" create secret generic oauth2-proxy-secrets \
  --namespace="${NAMESPACE}" \
  --from-literal=client-id="${OAUTH2_PROXY_CLIENT_ID}" \
  --from-literal=client-secret="${OAUTH2_PROXY_CLIENT_SECRET}" \
  --from-literal=cookie-secret="${OAUTH2_PROXY_COOKIE_SECRET}" \
  --dry-run=client -o yaml | kubectl --context "${ACTING_CONTEXT}" apply -f -

echo "oauth2-proxy-secrets created/updated."

# keycloak-secrets: Postgres + Keycloak bootstrap admin credentials. Only for deployments
# that run the broker (daly). Passwords are reused from the cluster if already present (so the
# existing DB keeps working), else generated. The usernames are reused too, falling back to the
# literal defaults rather than to empty: overwriting a non-default db-user with `keycloak` while
# faithfully reusing db-password would leave Keycloak unable to authenticate to Postgres.
if [ "${ENABLE_KEYCLOAK}" = "true" ]; then
  reuse_kc() { secret_key keycloak-secrets "$1"; }
  KC_DB_USER="${KC_DB_USER:-$(reuse_kc db-user)}"
  KC_DB_USER="${KC_DB_USER:-keycloak}"
  KC_DB_PASSWORD="${KC_DB_PASSWORD:-$(reuse_kc db-password)}"
  KC_DB_PASSWORD="${KC_DB_PASSWORD:-$(openssl rand -base64 24)}"
  KC_ADMIN_USER="${KC_ADMIN_USER:-$(reuse_kc admin-user)}"
  KC_ADMIN_USER="${KC_ADMIN_USER:-admin}"
  KC_ADMIN_PASSWORD="${KC_ADMIN_PASSWORD:-$(reuse_kc admin-password)}"
  KC_ADMIN_PASSWORD="${KC_ADMIN_PASSWORD:-$(openssl rand -base64 24)}"

  kubectl --context "${ACTING_CONTEXT}" create secret generic keycloak-secrets \
    --namespace="${NAMESPACE}" \
    --from-literal=db-user="${KC_DB_USER}" \
    --from-literal=db-password="${KC_DB_PASSWORD}" \
    --from-literal=admin-user="${KC_ADMIN_USER}" \
    --from-literal=admin-password="${KC_ADMIN_PASSWORD}" \
    --dry-run=client -o yaml | kubectl --context "${ACTING_CONTEXT}" apply -f -

  echo "keycloak-secrets created/updated."
else
  echo "Skipping keycloak-secrets (ENABLE_KEYCLOAK=${ENABLE_KEYCLOAK})."
fi
