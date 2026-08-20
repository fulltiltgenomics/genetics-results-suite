#!/bin/bash
set -euo pipefail

# create Kubernetes secrets for the genetics results suite
# Every key below EXCEPT anthropic-api-key is reused from the cluster when its env var is
# not set, so a re-run with only some of them exported never blanks the rest. ANTHROPIC_API_KEY
# is the one exception: it is never read back from the cluster and must ALWAYS be exported,
# otherwise the script aborts before writing anything. Keys absent both in the environment and
# in the cluster get a fresh random value only where marked "generated"; the optional API keys
# stay EMPTY rather than being invented (a random string is a plausible-looking key
# that fails only at call time), and the keycloak usernames fall back to their literal defaults.
# set these environment variables before running:
#   ANTHROPIC_API_KEY     - Anthropic API key for chat backend (ALWAYS required, including on
#                           re-runs: it is the one key never read back from the cluster)
#   OPENAI_API_KEY        - OpenAI API key (optional for chat backend, required for rag-service)
#   TAVILY_API_KEY        - Tavily API key (optional)
#   PERPLEXITY_API_KEY    - Perplexity API key (optional)
#   MCP_API_KEY           - bearer token for MCP server auth (optional)
#   COHERE_API_KEY        - Cohere API key for RAG service embeddings (required only when ENABLE_RAG=true)
#   EXTERNAL_MCP_SERVERS  - comma-separated external MCP server URLs for chat-backend (optional)
#   ADMIN_USERS           - comma-separated admin email addresses (optional)
#   INTERNAL_API_SECRET   - shared secret for internal service-to-service auth (reused from the existing secret if not set, generated on first install)
#   SANDBOX_TOKEN_SIGNING_KEY  - signing key for per-execution sandbox tokens (reused if not set, generated on first install; see docs/code-execution-security.md §4)
#   SLACK_WEBHOOK_URL     - Slack webhook URL for alerting (optional)
#   OAUTH2_PROXY_CLIENT_ID     - oauth2-proxy OAuth client id (Google client id on finngen; Keycloak OIDC client id on daly). reused from the cluster if not set; required on first install
#   OAUTH2_PROXY_CLIENT_SECRET - matching OAuth client secret (reused if not set; required on first install)
#   OAUTH2_PROXY_COOKIE_SECRET - oauth2-proxy session cookie secret (reused from the cluster if not set, generated on first install — never rotated on re-run, so sessions survive)

NAMESPACE="${NAMESPACE:-genetics}"
ENABLE_RAG="${ENABLE_RAG:-false}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# resolve the target deployment (DEPLOY_ENV) and load its .env, so the secrets written here
# come from the same file the matching deploy.sh run will use
. "${SCRIPT_DIR}/lib/env.sh"
resolve_deploy_env
load_deploy_env

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
echo "Target cluster: $(kubectl config current-context 2>/dev/null || echo 'NONE — run deploy.sh or gcloud container clusters get-credentials first')"

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
  out="$(kubectl get secret "${secret}" --namespace="${NAMESPACE}" \
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
# leave it empty. Deliberately reuse-then-EMPTY, not the reuse-then-generate used for the
# two shared secrets below: a random value for an API key would look valid and fail only at
# call time, and admin-users / external-mcp-servers have no meaningful random value at all.
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
reuse_optional MCP_API_KEY          mcp-api-key
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

kubectl create secret generic genetics-secrets \
  --namespace="${NAMESPACE}" \
  --from-literal=anthropic-api-key="${ANTHROPIC_API_KEY}" \
  --from-literal=openai-api-key="${OPENAI_API_KEY:-}" \
  --from-literal=tavily-api-key="${TAVILY_API_KEY:-}" \
  --from-literal=perplexity-api-key="${PERPLEXITY_API_KEY:-}" \
  --from-literal=mcp-api-key="${MCP_API_KEY:-}" \
  --from-literal=cohere-api-key="${COHERE_API_KEY:-}" \
  --from-literal=external-mcp-servers="${EXTERNAL_MCP_SERVERS:-}" \
  --from-literal=admin-users="${ADMIN_USERS:-}" \
  --from-literal=internal-api-secret="${INTERNAL_API_SECRET}" \
  --from-literal=sandbox-token-signing-key="${SANDBOX_TOKEN_SIGNING_KEY}" \
  --from-literal=slack-webhook-url="${SLACK_WEBHOOK_URL:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

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

kubectl create secret generic oauth2-proxy-secrets \
  --namespace="${NAMESPACE}" \
  --from-literal=client-id="${OAUTH2_PROXY_CLIENT_ID}" \
  --from-literal=client-secret="${OAUTH2_PROXY_CLIENT_SECRET}" \
  --from-literal=cookie-secret="${OAUTH2_PROXY_COOKIE_SECRET}" \
  --dry-run=client -o yaml | kubectl apply -f -

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

  kubectl create secret generic keycloak-secrets \
    --namespace="${NAMESPACE}" \
    --from-literal=db-user="${KC_DB_USER}" \
    --from-literal=db-password="${KC_DB_PASSWORD}" \
    --from-literal=admin-user="${KC_ADMIN_USER}" \
    --from-literal=admin-password="${KC_ADMIN_PASSWORD}" \
    --dry-run=client -o yaml | kubectl apply -f -

  echo "keycloak-secrets created/updated."
else
  echo "Skipping keycloak-secrets (ENABLE_KEYCLOAK=${ENABLE_KEYCLOAK})."
fi
