#!/bin/bash
set -euo pipefail

# Add or update the Microsoft identity provider in the live realm from MICROSOFT_* in .env.<env>.
#
# deploy.sh renders keycloak/microsoft-idp.json.template into the realm import, but the import
# only runs on a fresh DB; this is the path onto an already-imported realm, and it is idempotent.
# The provider gets the same first-broker-login flow as the google IdP, so once
# keycloak-bind-allowlist.sh has run, the e-mail allow-list gate applies to Microsoft logins too.
#
# Keycloak's built-in Microsoft provider is used on the "common" endpoints unless
# MICROSOFT_TENANT_ID pins a tenant: personal accounts (outlook.com, hotmail.com) and any work or
# school account can sign in, and the allow-list decides who gets in. The Entra app registration
# must therefore allow "any organizational directory and personal Microsoft accounts" and list the
# redirect URI this script prints (Web platform).
#
# Inputs (from .env.<DEPLOY_ENV>):
#   MICROSOFT_CLIENT_ID      Application (client) ID of the Entra app registration
#   MICROSOFT_CLIENT_SECRET  a client secret of that app; they expire, re-run after rotating one
#   MICROSOFT_TENANT_ID      optional Directory (tenant) ID to accept only that tenant
#   DEPLOY_ENV               selects the deployment (scripts/lib/env.sh); the kubectl context must
#                            be the one its tfvars names, as for rollout.sh and create-secrets.sh

NAMESPACE="${NAMESPACE:-genetics}"
REALM="${KC_REALM:-genetics}"
ALIAS="microsoft"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/../keycloak/microsoft-idp.json.template"

. "${SCRIPT_DIR}/lib/env.sh"
resolve_deploy_env
load_deploy_env
require_kube_context \
  "keycloak-register-microsoft-idp.sh" \
  "identity provider update" \
  "Updating the realm here" \
  "Keycloak" \
  "    That is one deployment's Microsoft app registration being wired into another cluster's realm."

: "${MICROSOFT_CLIENT_ID:?Set MICROSOFT_CLIENT_ID in ${ENV_FILE}}"
: "${MICROSOFT_CLIENT_SECRET:?Set MICROSOFT_CLIENT_SECRET in ${ENV_FILE}}"
MICROSOFT_TENANT_ID="${MICROSOFT_TENANT_ID:-common}"
export MICROSOFT_CLIENT_ID MICROSOFT_CLIENT_SECRET MICROSOFT_TENANT_ID
IDP_JSON="$(envsubst '${MICROSOFT_CLIENT_ID} ${MICROSOFT_CLIENT_SECRET} ${MICROSOFT_TENANT_ID}' < "${TEMPLATE}")"

POD="$(kubectl --context "${ACTING_CONTEXT}" get pods -n "${NAMESPACE}" -l app=keycloak -o jsonpath='{.items[0].metadata.name}')"
AU="$(kubectl --context "${ACTING_CONTEXT}" get secret keycloak-secrets -n "${NAMESPACE}" -o jsonpath='{.data.admin-user}' | base64 -d)"
AP="$(kubectl --context "${ACTING_CONTEXT}" get secret keycloak-secrets -n "${NAMESPACE}" -o jsonpath='{.data.admin-password}' | base64 -d)"

# the pod's root filesystem is read-only, so kcadm's config file has to go under /tmp
KC_CONFIG="--config /tmp/kcadm-microsoft-idp.config"
kc() { kubectl --context "${ACTING_CONTEXT}" exec -n "${NAMESPACE}" "${POD}" -- /opt/keycloak/bin/kcadm.sh "$@" ${KC_CONFIG}; }
# the IdP body carries the client secret, so it travels over stdin, never on a command line
kc_body() { printf '%s' "${IDP_JSON}" | kubectl --context "${ACTING_CONTEXT}" exec -i -n "${NAMESPACE}" "${POD}" -- /opt/keycloak/bin/kcadm.sh "$@" -r "${REALM}" -f - ${KC_CONFIG}; }

echo "Authenticating kcadm against ${POD}..."
kc config credentials --server http://localhost:8080 --realm master --user "${AU}" --password "${AP}" >/dev/null

if kc get "identity-provider/instances/${ALIAS}" -r "${REALM}" >/dev/null 2>&1; then
  echo "IdP '${ALIAS}' exists; updating its config..."
  # -m: kcadm turns merging off when the body comes from a file, and a full PUT would drop
  # whatever the console or bind-allowlist.sh set on the provider since
  kc_body update "identity-provider/instances/${ALIAS}" -m
else
  echo "Creating IdP '${ALIAS}'..."
  kc_body create identity-provider/instances
fi

FLOW="$(kc get identity-provider/instances/google -r "${REALM}" 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("firstBrokerLoginFlowAlias",""))' || true)"
if [ -n "${FLOW}" ]; then
  kc update "identity-provider/instances/${ALIAS}" -r "${REALM}" -s "firstBrokerLoginFlowAlias=${FLOW}"
  echo "IdP '${ALIAS}' -> first broker login flow '${FLOW}' (same as google)."
fi

KC_HOSTNAME="$(kubectl --context "${ACTING_CONTEXT}" get deploy keycloak -n "${NAMESPACE}" \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="KC_HOSTNAME")].value}')"
cat <<EOF

Microsoft IdP '${ALIAS}' is in realm '${REALM}' (tenant: ${MICROSOFT_TENANT_ID}). The Entra app
registration must list this redirect URI under Authentication -> Web:
  ${KC_HOSTNAME}/realms/${REALM}/broker/${ALIAS}/endpoint
The login page lists the new button at once; its helper text is baked into the keycloak image
(keycloak/themes/genetics/login/messages/messages_en.properties), so rebuild and roll keycloak if
that text changed.
EOF
