#!/bin/bash
set -euo pipefail

# Register (or reconcile) the brainzzz OAuth client on the RUNNING Keycloak "genetics" realm.
#
# The realm import only runs on a fresh DB, so adding the client to realm-genetics.json.template
# does NOT affect an already-imported realm — this reconciles the live one via the admin API.
# Idempotent and safe to re-run (e.g. to change redirect URIs or rotate the secret). Requires
# kubectl access to the cluster and python3.
#
# Config (from .env / environment, kept out of version control):
#   BRAINZZZ_CLIENT_SECRET   confidential client secret; share this with the brainzzz developer
#   OAUTH_RESOURCE_URL       MCP resource audience the mcp-server checks
#                            (default: https://genegenie.broadinstitute.org/mcp)

NAMESPACE="${NAMESPACE:-genetics}"
REALM="${KC_REALM:-genetics}"
CLIENT_ID="brainzzz"

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "${ROOT_DIR}/.env" ]; then set -a; . "${ROOT_DIR}/.env"; set +a; fi

: "${BRAINZZZ_CLIENT_SECRET:?Set BRAINZZZ_CLIENT_SECRET (confidential client secret) in .env or the environment}"
OAUTH_RESOURCE_URL="${OAUTH_RESOURCE_URL:-https://genegenie.broadinstitute.org/mcp}"

POD="$(kubectl get pods -n "${NAMESPACE}" -l app=keycloak -o jsonpath='{.items[0].metadata.name}')"
AU="$(kubectl get secret keycloak-secrets -n "${NAMESPACE}" -o jsonpath='{.data.admin-user}' | base64 -d)"
AP="$(kubectl get secret keycloak-secrets -n "${NAMESPACE}" -o jsonpath='{.data.admin-password}' | base64 -d)"

kc() { kubectl exec -n "${NAMESPACE}" "${POD}" -- /opt/keycloak/bin/kcadm.sh "$@"; }

echo "Authenticating kcadm against ${POD}..."
kc config credentials --server http://localhost:8080 --realm master --user "${AU}" --password "${AP}" >/dev/null

# client representation. mappers are reconciled separately below because a client update does not
# manage the protocol-mappers subresource.
CLIENT_JSON="$(cat <<EOF
{
  "clientId": "${CLIENT_ID}",
  "name": "brainzzz (GeneGenie MCP)",
  "enabled": true,
  "protocol": "openid-connect",
  "publicClient": false,
  "standardFlowEnabled": true,
  "directAccessGrantsEnabled": false,
  "serviceAccountsEnabled": false,
  "secret": "${BRAINZZZ_CLIENT_SECRET}",
  "redirectUris": [
    "http://localhost:3000/api/auth/genegenie/callback",
    "https://brainzzz-dev.dsp-eng-tools.broadinstitute.org/api/auth/genegenie/callback",
    "https://brainzzz-staging.dsp-eng-tools.broadinstitute.org/api/auth/genegenie/callback",
    "https://brainzzz-prod.dsp-eng-tools.broadinstitute.org/api/auth/genegenie/callback"
  ],
  "webOrigins": [
    "http://localhost:3000",
    "https://brainzzz-dev.dsp-eng-tools.broadinstitute.org",
    "https://brainzzz-staging.dsp-eng-tools.broadinstitute.org",
    "https://brainzzz-prod.dsp-eng-tools.broadinstitute.org"
  ],
  "attributes": { "pkce.code.challenge.method": "S256" }
}
EOF
)"

MAPPER_JSON="$(cat <<EOF
{
  "name": "mcp-audience",
  "protocol": "openid-connect",
  "protocolMapper": "oidc-audience-mapper",
  "config": { "included.custom.audience": "${OAUTH_RESOURCE_URL}", "access.token.claim": "true", "id.token.claim": "false" }
}
EOF
)"

# upsert the client
CID="$(kc get clients -r "${REALM}" -q clientId="${CLIENT_ID}" 2>/dev/null \
  | python3 -c 'import json,sys; a=json.load(sys.stdin); print(a[0]["id"] if a else "")')"

if [ -z "${CID}" ]; then
  echo "Creating client '${CLIENT_ID}'..."
  kc create clients -r "${REALM}" -b "${CLIENT_JSON}"
  CID="$(kc get clients -r "${REALM}" -q clientId="${CLIENT_ID}" \
    | python3 -c 'import json,sys; a=json.load(sys.stdin); print(a[0]["id"] if a else "")')"
else
  echo "Client '${CLIENT_ID}' exists (${CID}); updating..."
  kc update "clients/${CID}" -r "${REALM}" -b "${CLIENT_JSON}"
fi

# upsert the mcp-audience mapper so access tokens carry aud=${OAUTH_RESOURCE_URL}
# delete any existing mcp-audience mapper, then create fresh — updating a mapper in place needs
# the id inside the body (Keycloak reads rep.id, not the URL), which kcadm -b does not supply
MID="$(kc get "clients/${CID}/protocol-mappers/models" -r "${REALM}" 2>/dev/null \
  | python3 -c 'import json,sys; a=json.load(sys.stdin); print(next((m["id"] for m in a if m.get("name")=="mcp-audience"), ""))')"
[ -n "${MID}" ] && kc delete "clients/${CID}/protocol-mappers/models/${MID}" -r "${REALM}"
echo "Setting mcp-audience mapper (aud=${OAUTH_RESOURCE_URL})..."
kc create "clients/${CID}/protocol-mappers/models" -r "${REALM}" -b "${MAPPER_JSON}"

echo "Done. clientId=${CLIENT_ID}, audience=${OAUTH_RESOURCE_URL}"
echo "Give the brainzzz developer: client_id=${CLIENT_ID}, client_secret=<BRAINZZZ_CLIENT_SECRET>, issuer=https://genegenie.broadinstitute.org/auth/realms/genetics, mcp_url=https://genegenie.broadinstitute.org/mcp"
