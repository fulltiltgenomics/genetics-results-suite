#!/bin/bash
set -euo pipefail

# Register (or reconcile) the confidential client chat-backend's access-grant reconciler uses to
# keep the realm's `allowedEmails` attribute in step with runtime grants and to disable the
# accounts of ended grants (genetics-mcp-server access_grants.py). The client has no login flow
# at all — service account only — and holds exactly the realm-management roles those two writes
# need, so chat-backend never sees the bootstrap admin password that the other keycloak-*.sh
# scripts read.
#
# Keycloak generates the secret; this script stores it straight into genetics-secrets under
# keycloak-access-grants-client-secret (create-secrets.sh reuses the stored value on re-runs) and
# never prints it. chat-backend reads the key at startup, so roll it afterwards:
#   DEPLOY_ENV=<env> scripts/rollout.sh chat-backend
#
# Clients created this way live only in the running realm, not realm-genetics.json.template, so a
# fresh-DB reimport would not recreate them — re-run after a realm reset, like
# keycloak-register-client.sh. Idempotent; pass --rotate-secret to mint a new secret.
#
# Inputs:
#   DEPLOY_ENV   selects the deployment (scripts/lib/env.sh); the kubectl context must be the one
#                its tfvars names, as for rollout.sh and create-secrets.sh

NAMESPACE="${NAMESPACE:-genetics}"
REALM="${KC_REALM:-genetics}"
CLIENT_ID="access-grants"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ROTATE_SECRET=false
[ "${1:-}" = "--rotate-secret" ] && ROTATE_SECRET=true

. "${SCRIPT_DIR}/lib/env.sh"
resolve_deploy_env
require_kube_context \
  "keycloak-register-access-grants-client.sh" \
  "client registration" \
  "Registering the client here" \
  "Keycloak" \
  "    That is one deployment's reconciler credential being minted in another cluster's realm."

POD="$(kubectl --context "${ACTING_CONTEXT}" get pods -n "${NAMESPACE}" -l app=keycloak -o jsonpath='{.items[0].metadata.name}')"
AU="$(kubectl --context "${ACTING_CONTEXT}" get secret keycloak-secrets -n "${NAMESPACE}" -o jsonpath='{.data.admin-user}' | base64 -d)"
AP="$(kubectl --context "${ACTING_CONTEXT}" get secret keycloak-secrets -n "${NAMESPACE}" -o jsonpath='{.data.admin-password}' | base64 -d)"

# the pod's root filesystem is read-only, so kcadm's config file has to go under /tmp
KC_CONFIG="--config /tmp/kcadm-access-grants.config"
kc() { kubectl --context "${ACTING_CONTEXT}" exec -n "${NAMESPACE}" "${POD}" -- /opt/keycloak/bin/kcadm.sh "$@" ${KC_CONFIG}; }

echo "Authenticating kcadm against ${POD}..."
kc config credentials --server http://localhost:8080 --realm master --user "${AU}" --password "${AP}" >/dev/null

CLIENT_JSON='{
  "clientId": "access-grants",
  "name": "access-grants (chat-backend reconciler)",
  "description": "Service account for the runtime access-grant reconciler: realm attribute allowedEmails and user enable/disable.",
  "enabled": true,
  "protocol": "openid-connect",
  "publicClient": false,
  "standardFlowEnabled": false,
  "implicitFlowEnabled": false,
  "directAccessGrantsEnabled": false,
  "serviceAccountsEnabled": true,
  "redirectUris": [],
  "webOrigins": []
}'

CID="$(kc get clients -r "${REALM}" -q clientId="${CLIENT_ID}" 2>/dev/null \
  | python3 -c 'import json,sys; a=json.load(sys.stdin); print(a[0]["id"] if a else "")')"
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

# manage-realm writes the attribute; manage-users flips `enabled`; view-users is what the
# by-email lookup needs. add-roles is idempotent, so reconciling is just asking again
echo "Granting realm-management roles to service-account-${CLIENT_ID}..."
kc add-roles -r "${REALM}" --uusername "service-account-${CLIENT_ID}" --cclientid realm-management \
  --rolename manage-realm --rolename manage-users --rolename view-users

SECRET="$(kc get "clients/${CID}/client-secret" -r "${REALM}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("value",""))')"
[ -n "${SECRET}" ] || { echo "ERROR: Keycloak returned no client secret" >&2; exit 1; }

# stringData so kubectl does the base64; the key name is the one chat-backend.yaml reads
kubectl --context "${ACTING_CONTEXT}" patch secret genetics-secrets -n "${NAMESPACE}" --type merge \
  -p "$(python3 -c 'import json,sys; print(json.dumps({"stringData": {"keycloak-access-grants-client-secret": sys.argv[1]}}))' "${SECRET}")" \
  >/dev/null

cat <<EOF

Client '${CLIENT_ID}' registered in realm '${REALM}' and its secret stored in
genetics-secrets/keycloak-access-grants-client-secret. chat-backend reads it at startup:
  DEPLOY_ENV=${DEPLOY_ENV} ${SCRIPT_DIR}/rollout.sh chat-backend
EOF
