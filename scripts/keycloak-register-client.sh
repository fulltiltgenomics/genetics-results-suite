#!/bin/bash
set -euo pipefail

# Register (or reconcile) a confidential OAuth client on the running Keycloak "genetics" realm,
# with the MCP audience mapper so its access tokens are accepted by the mcp-server (/mcp).
#
# Registration stays MANUAL (no DCR / no open endpoint) but this makes it one command: Keycloak
# GENERATES the client secret — you never invent or store one — and this prints it for handoff.
# Idempotent: re-run to change redirect URIs (the existing secret is preserved unless you pass
# --rotate-secret). Requires kubectl access to the cluster and python3.
#
# Clients created this way live only in the running realm, not realm-genetics.json.template, so a
# fresh-DB reimport would not recreate them — re-run this script after a realm reset (same caveat
# as scripts/keycloak-bind-allowlist.sh).
#
# Usage:
#   ./scripts/keycloak-register-client.sh <clientId> <redirect-uri> [redirect-uri ...]
#   ./scripts/keycloak-register-client.sh --rotate-secret <clientId> <redirect-uri> [...]
#   ./scripts/keycloak-register-client.sh --delete <clientId>   # remove a client (e.g. a test one)
#
# Config (from .env / environment):
#   OAUTH_RESOURCE_URL   MCP resource audience (default: https://genegenie.broadinstitute.org/mcp)
#   OAUTH_ISSUER         issuer shown in the handoff (default: the genegenie path-based issuer)

NAMESPACE="${NAMESPACE:-genetics}"
REALM="${KC_REALM:-genetics}"

ROTATE_SECRET=false
DELETE=false
case "${1:-}" in
  --delete) DELETE=true; shift ;;
  --rotate-secret) ROTATE_SECRET=true; shift ;;
esac

CLIENT_ID="${1:-}"
shift || true
REDIRECT_URIS=("$@")

usage() {
  echo "Usage: $0 [--rotate-secret] <clientId> <redirect-uri> [redirect-uri ...]" >&2
  echo "       $0 --delete <clientId>" >&2
  exit 2
}
[ -n "${CLIENT_ID}" ] || usage
# delete needs only a clientId; registration needs at least one redirect URI
if [ "${DELETE}" != true ] && [ "${#REDIRECT_URIS[@]}" -eq 0 ]; then usage; fi

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "${ROOT_DIR}/.env" ]; then set -a; . "${ROOT_DIR}/.env"; set +a; fi
OAUTH_RESOURCE_URL="${OAUTH_RESOURCE_URL:-https://genegenie.broadinstitute.org/mcp}"
OAUTH_ISSUER="${OAUTH_ISSUER:-https://genegenie.broadinstitute.org/auth/realms/genetics}"

# build the client representation (register mode only). no "secret" field → Keycloak generates one
# on create; on update, omitting it preserves the existing secret. webOrigins "+" = the registered
# redirect URIs' origins.
if [ "${DELETE}" != true ]; then
CLIENT_JSON="$(python3 - "${CLIENT_ID}" "${REDIRECT_URIS[@]}" <<'PY'
import json, sys
client_id, redirects = sys.argv[1], sys.argv[2:]
print(json.dumps({
    "clientId": client_id,
    "enabled": True,
    "protocol": "openid-connect",
    "publicClient": False,
    "standardFlowEnabled": True,
    "directAccessGrantsEnabled": False,
    "serviceAccountsEnabled": False,
    "redirectUris": redirects,
    "webOrigins": ["+"],
    "attributes": {"pkce.code.challenge.method": "S256"},
}))
PY
)"

MAPPER_JSON="$(python3 - "${OAUTH_RESOURCE_URL}" <<'PY'
import json, sys
print(json.dumps({
    "name": "mcp-audience",
    "protocol": "openid-connect",
    "protocolMapper": "oidc-audience-mapper",
    "config": {"included.custom.audience": sys.argv[1], "access.token.claim": "true", "id.token.claim": "false"},
}))
PY
)"
fi

POD="$(kubectl get pods -n "${NAMESPACE}" -l app=keycloak -o jsonpath='{.items[0].metadata.name}')"
AU="$(kubectl get secret keycloak-secrets -n "${NAMESPACE}" -o jsonpath='{.data.admin-user}' | base64 -d)"
AP="$(kubectl get secret keycloak-secrets -n "${NAMESPACE}" -o jsonpath='{.data.admin-password}' | base64 -d)"

kc() { kubectl exec -n "${NAMESPACE}" "${POD}" -- /opt/keycloak/bin/kcadm.sh "$@"; }

echo "Authenticating kcadm against ${POD}..."
kc config credentials --server http://localhost:8080 --realm master --user "${AU}" --password "${AP}" >/dev/null

CID="$(kc get clients -r "${REALM}" -q clientId="${CLIENT_ID}" 2>/dev/null \
  | python3 -c 'import json,sys; a=json.load(sys.stdin); print(a[0]["id"] if a else "")')"

if [ "${DELETE}" = true ]; then
  if [ -z "${CID}" ]; then
    echo "Client '${CLIENT_ID}' not found in realm '${REALM}'; nothing to delete."
    exit 0
  fi
  echo "Deleting client '${CLIENT_ID}' (${CID})..."
  kc delete "clients/${CID}" -r "${REALM}"
  echo "Deleted '${CLIENT_ID}'."
  exit 0
fi

if [ -z "${CID}" ]; then
  echo "Creating client '${CLIENT_ID}'..."
  kc create clients -r "${REALM}" -b "${CLIENT_JSON}"
  CID="$(kc get clients -r "${REALM}" -q clientId="${CLIENT_ID}" \
    | python3 -c 'import json,sys; a=json.load(sys.stdin); print(a[0]["id"] if a else "")')"
else
  echo "Client '${CLIENT_ID}' exists (${CID}); updating config (secret preserved)..."
  kc update "clients/${CID}" -r "${REALM}" -b "${CLIENT_JSON}"
  if [ "${ROTATE_SECRET}" = true ]; then
    echo "Rotating client secret..."
    kc create "clients/${CID}/client-secret" -r "${REALM}" >/dev/null
  fi
fi

# reconcile the mcp-audience mapper (subresource; not managed by the client update above).
# delete any existing one then create fresh — an in-place update needs the id inside the body
# (Keycloak reads rep.id, not the URL path), which kcadm -b does not supply.
MID="$(kc get "clients/${CID}/protocol-mappers/models" -r "${REALM}" 2>/dev/null \
  | python3 -c 'import json,sys; a=json.load(sys.stdin); print(next((m["id"] for m in a if m.get("name")=="mcp-audience"), ""))')"
[ -n "${MID}" ] && kc delete "clients/${CID}/protocol-mappers/models/${MID}" -r "${REALM}"
kc create "clients/${CID}/protocol-mappers/models" -r "${REALM}" -b "${MAPPER_JSON}"

SECRET="$(kc get "clients/${CID}/client-secret" -r "${REALM}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("value",""))')"

cat <<EOF

Client registered. Hand these to the app developer (send the secret over a secure channel):
  client_id     = ${CLIENT_ID}
  client_secret = ${SECRET}
  issuer        = ${OAUTH_ISSUER}
  mcp_url       = ${OAUTH_RESOURCE_URL}
  scopes        = openid email profile
  flow          = authorization code + PKCE (S256)
  redirect_uris = ${REDIRECT_URIS[*]}
Audience is stamped server-side (mcp-audience mapper), so no RFC 8707 resource param is needed.
The user signing in must have an allow-listed (e.g. broadinstitute.org) email.
EOF
