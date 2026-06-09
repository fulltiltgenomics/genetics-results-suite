#!/bin/bash
set -euo pipefail

# create Kubernetes secrets for the genetics results suite
# set these environment variables before running:
#   ANTHROPIC_API_KEY     - Anthropic API key for chat backend
#   OPENAI_API_KEY        - OpenAI API key (optional for chat backend, required for rag-service)
#   TAVILY_API_KEY        - Tavily API key (optional)
#   PERPLEXITY_API_KEY    - Perplexity API key (optional)
#   MCP_API_KEY           - bearer token for MCP server auth (optional)
#   COHERE_API_KEY        - Cohere API key for RAG service embeddings
#   EXTERNAL_MCP_SERVERS  - comma-separated external MCP server URLs for chat-backend (optional)
#   ADMIN_USERS           - comma-separated admin email addresses (optional)
#   INTERNAL_API_SECRET   - shared secret for internal service-to-service auth (reused from the existing secret if not set, generated on first install)
#   SLACK_WEBHOOK_URL     - Slack webhook URL for alerting (optional)
#
# oauth2-proxy secrets are created separately — see README.md

NAMESPACE="${NAMESPACE:-genetics}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# the keycloak broker (and its secrets) is per-profile: on for daly, off otherwise.
# derive from terraform.tfvars (like build.sh does for app_name); override with ENABLE_KEYCLOAK.
TFVARS="${SCRIPT_DIR}/../terraform/terraform.tfvars"
PROFILE="$(grep -E '^\s*config_profile\s*=' "${TFVARS}" 2>/dev/null | sed 's/.*=\s*"\(.*\)"/\1/')"
ENABLE_KEYCLOAK="${ENABLE_KEYCLOAK:-$([ "${PROFILE}" = "daly" ] && echo true || echo false)}"

echo "Creating genetics-secrets in namespace ${NAMESPACE}..."

# required
: "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY}"
: "${COHERE_API_KEY:?Set COHERE_API_KEY}"

# reuse the internal API secret to avoid breaking service-to-service auth for
# already-running pods: an explicit env value wins, otherwise reuse the value
# already in the cluster, otherwise generate a fresh one (first install).
if [ -z "${INTERNAL_API_SECRET:-}" ]; then
  INTERNAL_API_SECRET="$(kubectl get secret genetics-secrets \
    --namespace="${NAMESPACE}" \
    -o jsonpath='{.data.internal-api-secret}' 2>/dev/null | base64 -d || true)"
fi
INTERNAL_API_SECRET="${INTERNAL_API_SECRET:-$(openssl rand -base64 32)}"

kubectl create secret generic genetics-secrets \
  --namespace="${NAMESPACE}" \
  --from-literal=anthropic-api-key="${ANTHROPIC_API_KEY}" \
  --from-literal=openai-api-key="${OPENAI_API_KEY:-}" \
  --from-literal=tavily-api-key="${TAVILY_API_KEY:-}" \
  --from-literal=perplexity-api-key="${PERPLEXITY_API_KEY:-}" \
  --from-literal=mcp-api-key="${MCP_API_KEY:-}" \
  --from-literal=cohere-api-key="${COHERE_API_KEY}" \
  --from-literal=external-mcp-servers="${EXTERNAL_MCP_SERVERS:-}" \
  --from-literal=admin-users="${ADMIN_USERS:-}" \
  --from-literal=internal-api-secret="${INTERNAL_API_SECRET}" \
  --from-literal=slack-webhook-url="${SLACK_WEBHOOK_URL:-}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "genetics-secrets created/updated."

# keycloak-secrets: Postgres + Keycloak bootstrap admin credentials. Only for deployments
# that run the broker (daly). Passwords are reused from the cluster if already present (so the
# existing DB keeps working), else generated.
if [ "${ENABLE_KEYCLOAK}" = "true" ]; then
  reuse_kc() {
    kubectl get secret keycloak-secrets --namespace="${NAMESPACE}" \
      -o jsonpath="{.data.$1}" 2>/dev/null | base64 -d || true
  }
  KC_DB_USER="${KC_DB_USER:-keycloak}"
  KC_DB_PASSWORD="${KC_DB_PASSWORD:-$(reuse_kc db-password)}"
  KC_DB_PASSWORD="${KC_DB_PASSWORD:-$(openssl rand -base64 24)}"
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
