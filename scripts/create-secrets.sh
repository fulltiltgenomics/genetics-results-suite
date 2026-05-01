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
#   INTERNAL_API_SECRET   - shared secret for internal service-to-service auth (auto-generated if not set)
#
# oauth2-proxy secrets are created separately — see README.md

NAMESPACE="${NAMESPACE:-genetics}"

echo "Creating genetics-secrets in namespace ${NAMESPACE}..."

# required
: "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY}"
: "${COHERE_API_KEY:?Set COHERE_API_KEY}"

# auto-generate internal API secret if not provided
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
  --dry-run=client -o yaml | kubectl apply -f -

echo "genetics-secrets created/updated."
