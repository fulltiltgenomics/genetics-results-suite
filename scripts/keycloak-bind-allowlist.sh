#!/bin/bash
set -euo pipefail

# Bind the email allow-list script authenticator into Keycloak's "first broker login" flow and
# sync the allow-list realm attributes.
#
# Run this AFTER deploying a keycloak image that bundles the scripts feature + the
# email-allowlist-authenticator.jar provider. The realm import only runs on a fresh DB, so this
# reconciles an already-imported realm; it is idempotent and safe to re-run (e.g. after changing
# the allow-list). Requires kubectl access and python3 on the machine running it.
#
# Allow-list source (same as deploy.sh / oauth2-proxy):
#   OAUTH_EMAIL_DOMAIN    comma/space separated domains   (e.g. "broadinstitute.org")
#   OAUTH_ALLOWED_EMAILS  comma/space separated addresses (e.g. Apple privaterelay aliases)

NAMESPACE="${NAMESPACE:-genetics}"
REALM="${KC_REALM:-genetics}"
FLOW="first broker login"
FLOW_ENC="${FLOW// /%20}"
PROVIDER="script-email-allowlist.js"

POD="$(kubectl get pods -n "${NAMESPACE}" -l app=keycloak -o jsonpath='{.items[0].metadata.name}')"
AU="$(kubectl get secret keycloak-secrets -n "${NAMESPACE}" -o jsonpath='{.data.admin-user}' | base64 -d)"
AP="$(kubectl get secret keycloak-secrets -n "${NAMESPACE}" -o jsonpath='{.data.admin-password}' | base64 -d)"

kc() { kubectl exec -n "${NAMESPACE}" "${POD}" -- /opt/keycloak/bin/kcadm.sh "$@"; }

echo "Authenticating kcadm against ${POD}..."
kc config credentials --server http://localhost:8080 --realm master --user "${AU}" --password "${AP}" >/dev/null

echo "Syncing allow-list realm attributes (domains='${OAUTH_EMAIL_DOMAIN:-}', emails='${OAUTH_ALLOWED_EMAILS:-}')..."
kc update "realms/${REALM}" \
  -s "attributes.allowedEmailDomains=${OAUTH_EMAIL_DOMAIN:-}" \
  -s "attributes.allowedEmails=${OAUTH_ALLOWED_EMAILS:-}"

echo "Ensuring '${PROVIDER}' is the first REQUIRED step of '${FLOW}'..."
EXECS="$(kc get "authentication/flows/${FLOW_ENC}/executions" -r "${REALM}")"

EXEC_ID="$(printf '%s' "${EXECS}" | python3 -c '
import json,sys
execs=json.load(sys.stdin)
for e in execs:
    if e.get("providerId")=="'"${PROVIDER}"'":
        print(e["id"]); break
')"

if [ -z "${EXEC_ID}" ]; then
  echo "  adding execution..."
  kc create "authentication/flows/${FLOW_ENC}/executions/execution" -r "${REALM}" \
    -b "{\"provider\":\"${PROVIDER}\"}"
  EXECS="$(kc get "authentication/flows/${FLOW_ENC}/executions" -r "${REALM}")"
  EXEC_ID="$(printf '%s' "${EXECS}" | python3 -c '
import json,sys
for e in json.load(sys.stdin):
    if e.get("providerId")=="'"${PROVIDER}"'":
        print(e["id"]); break
')"
else
  echo "  execution already present."
fi

# make it REQUIRED
kc update "authentication/flows/${FLOW_ENC}/executions" -r "${REALM}" \
  -b "{\"id\":\"${EXEC_ID}\",\"requirement\":\"REQUIRED\"}"

# move it to the top so it runs before profile review / user creation. raise-priority is a no-op
# once it is already first, so over-calling (by the number of top-level steps) is safe.
TOP_COUNT="$(printf '%s' "${EXECS}" | python3 -c 'import json,sys; print(sum(1 for e in json.load(sys.stdin) if e.get("level",0)==0))')"
for _ in $(seq 1 "${TOP_COUNT:-8}"); do
  kc create "authentication/executions/${EXEC_ID}/raise-priority" -r "${REALM}" >/dev/null 2>&1 || true
done

echo "Done. '${FLOW}' now runs the email allow-list check first."
