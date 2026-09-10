#!/bin/bash
set -euo pipefail

# Reconcile the live realm's login policy with this repo: the SSO session lifespans from
# keycloak/realm-genetics.json.template and the browser flow's Identity Provider Redirector.
#
# The realm import only runs on a fresh DB, so a template edit never reaches an already-imported
# realm on its own; this script is that path, and it is idempotent. Run it after deploy.sh on any
# realm whose lifespans should follow the template, and whenever KEYCLOAK_DEFAULT_IDP changes.
#
# Inputs:
#   DEPLOY_ENV            selects the deployment (scripts/lib/env.sh); the kubectl context must
#                         be the one its tfvars names, as for rollout.sh and create-secrets.sh
#   KEYCLOAK_DEFAULT_IDP  IdP alias the redirector sends every login straight to, skipping the
#                         chooser page. Empty keeps the chooser. Defaults to "google" for the
#                         finngen profile, which offers no other provider, and to empty otherwise:
#                         daly offers Apple too, and a default provider would hide it.
#   CONFIG_PROFILE        overrides the profile read from the tfvars

NAMESPACE="${NAMESPACE:-genetics}"
REALM="${KC_REALM:-genetics}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/../keycloak/realm-genetics.json.template"

. "${SCRIPT_DIR}/lib/env.sh"
resolve_deploy_env
require_kube_context \
  "keycloak-sync-login-policy.sh" \
  "realm update" \
  "Updating the realm here" \
  "Keycloak" \
  "    That is one deployment's login policy being written into another cluster's realm."
CONFIG_PROFILE="${CONFIG_PROFILE:-$(tfvar config_profile)}"
if [ -z "${KEYCLOAK_DEFAULT_IDP+x}" ]; then
  KEYCLOAK_DEFAULT_IDP="$([ "${CONFIG_PROFILE}" = "finngen" ] && echo google || true)"
fi

# the template is the only place the numbers live; it is not valid JSON before envsubst, so
# read the two keys with a regex rather than parsing it
read -r SSO_IDLE SSO_MAX < <(python3 - "${TEMPLATE}" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
vals = [re.search(r'"%s"\s*:\s*(\d+)' % k, text) for k in ("ssoSessionIdleTimeout", "ssoSessionMaxLifespan")]
if not all(vals):
    sys.exit("template does not set both ssoSessionIdleTimeout and ssoSessionMaxLifespan")
print(*(m.group(1) for m in vals))
PY
) || true
if [ -z "${SSO_MAX:-}" ]; then
  echo "ERROR: could not read ssoSessionIdleTimeout/ssoSessionMaxLifespan from ${TEMPLATE}" >&2
  exit 1
fi

POD="$(kubectl --context "${ACTING_CONTEXT}" get pods -n "${NAMESPACE}" -l app=keycloak -o jsonpath='{.items[0].metadata.name}')"
AU="$(kubectl --context "${ACTING_CONTEXT}" get secret keycloak-secrets -n "${NAMESPACE}" -o jsonpath='{.data.admin-user}' | base64 -d)"
AP="$(kubectl --context "${ACTING_CONTEXT}" get secret keycloak-secrets -n "${NAMESPACE}" -o jsonpath='{.data.admin-password}' | base64 -d)"

# the pod's root filesystem is read-only, so kcadm's config file has to go under /tmp
KC_CONFIG="--config /tmp/kcadm-login-policy.config"
kc() { kubectl --context "${ACTING_CONTEXT}" exec -n "${NAMESPACE}" "${POD}" -- /opt/keycloak/bin/kcadm.sh "$@" ${KC_CONFIG}; }

echo "Authenticating kcadm against ${POD}..."
kc config credentials --server http://localhost:8080 --realm master --user "${AU}" --password "${AP}" >/dev/null

echo "Setting SSO session idle=${SSO_IDLE}s max=${SSO_MAX}s..."
kc update "realms/${REALM}" -s "ssoSessionIdleTimeout=${SSO_IDLE}" -s "ssoSessionMaxLifespan=${SSO_MAX}"

# the redirector's config lives on its execution in the built-in browser flow; the execution
# itself is editable even though the flow is not
EXEC_JSON="$(kc get authentication/flows/browser/executions -r "${REALM}" | python3 -c '
import json, sys
for e in json.load(sys.stdin):
    if e.get("providerId") == "identity-provider-redirector":
        print(e["id"], e.get("authenticationConfig", "")); break')"
EXEC_ID="${EXEC_JSON%% *}"
CONFIG_ID="${EXEC_JSON#* }"
[ "${CONFIG_ID}" = "${EXEC_ID}" ] && CONFIG_ID=""
if [ -z "${EXEC_ID}" ]; then
  echo "ERROR: the browser flow has no Identity Provider Redirector execution" >&2
  exit 1
fi

if [ -n "${KEYCLOAK_DEFAULT_IDP}" ]; then
  if ! kc get "identity-provider/instances/${KEYCLOAK_DEFAULT_IDP}" -r "${REALM}" >/dev/null 2>&1; then
    echo "ERROR: KEYCLOAK_DEFAULT_IDP='${KEYCLOAK_DEFAULT_IDP}' is not an identity provider of realm '${REALM}'" >&2
    exit 1
  fi
  BODY="{\"alias\":\"default-idp\",\"config\":{\"defaultProvider\":\"${KEYCLOAK_DEFAULT_IDP}\"}}"
  if [ -n "${CONFIG_ID}" ]; then
    kc update "authentication/config/${CONFIG_ID}" -r "${REALM}" -b "{\"id\":\"${CONFIG_ID}\",${BODY#\{}"
  else
    kc create "authentication/executions/${EXEC_ID}/config" -r "${REALM}" -b "${BODY}"
  fi
  echo "Identity Provider Redirector -> '${KEYCLOAK_DEFAULT_IDP}' (the chooser page is skipped)."
elif [ -n "${CONFIG_ID}" ]; then
  kc delete "authentication/config/${CONFIG_ID}" -r "${REALM}"
  echo "Identity Provider Redirector has no default provider (the chooser page is shown)."
else
  echo "Identity Provider Redirector already has no default provider."
fi

echo "Done."
