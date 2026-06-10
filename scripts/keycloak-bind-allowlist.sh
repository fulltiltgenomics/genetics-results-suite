#!/bin/bash
set -euo pipefail

# Bind the email allow-list script authenticator into Keycloak's first-broker-login and sync the
# allow-list realm attributes.
#
# Run this AFTER deploying a keycloak image that bundles the scripts feature + the
# email-allowlist-authenticator.jar provider. The realm import only runs on a fresh DB, so this
# reconciles an already-imported realm; it is idempotent and safe to re-run (e.g. after changing
# the allow-list). Requires kubectl access and python3 on the machine running it.
#
# Built-in flows can't be edited, so this copies "first broker login" to a custom flow, inserts
# the allow-list check as the first REQUIRED step, and points the Google/Apple IdPs at the copy.
#
# Allow-list source (same as deploy.sh / oauth2-proxy):
#   OAUTH_EMAIL_DOMAIN    comma/space separated domains   (e.g. "broadinstitute.org")
#   OAUTH_ALLOWED_EMAILS  comma/space separated addresses (e.g. Apple privaterelay aliases)

NAMESPACE="${NAMESPACE:-genetics}"
REALM="${KC_REALM:-genetics}"
BUILTIN_FLOW="first broker login"
FLOW="first broker login allowlist"
FLOW_ENC="${FLOW// /%20}"
BUILTIN_ENC="${BUILTIN_FLOW// /%20}"
PROVIDER="script-email-allowlist.js"
IDPS="${IDPS:-google apple}"

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

# 1) custom flow = copy of the built-in (built-in flows are not editable)
if kc get authentication/flows -r "${REALM}" | python3 -c '
import json,sys; sys.exit(0 if any(f.get("alias")=="'"${FLOW}"'" for f in json.load(sys.stdin)) else 1)'; then
  echo "Custom flow '${FLOW}' already exists."
else
  echo "Copying '${BUILTIN_FLOW}' -> '${FLOW}'..."
  kc create "authentication/flows/${BUILTIN_ENC}/copy" -r "${REALM}" -b "{\"newName\":\"${FLOW}\"}"
fi

# 2) ensure the allow-list authenticator is present in the custom flow.
# Match on displayName: Keycloak stores a script authenticator's providerId base32-encoded in the
# executions list, so it won't equal "${PROVIDER}". "Email allow-list" is the name from
# keycloak-scripts.json.
get_exec_id() {
  for _ in 1 2 3; do
    local out
    if out="$(kc get "authentication/flows/${FLOW_ENC}/executions" -r "${REALM}" 2>/dev/null)"; then
      printf '%s' "${out}" | python3 -c '
import json,sys
try: execs=json.load(sys.stdin)
except Exception: sys.exit(0)
for e in execs:
    if e.get("displayName")=="Email allow-list": print(e["id"]); break'
      return
    fi
    sleep 2
  done
}
EXEC_ID="$(get_exec_id)"
if [ -z "${EXEC_ID}" ]; then
  echo "Adding '${PROVIDER}' to '${FLOW}'..."
  # capture the new id from the create output; re-fetching immediately can 404 (write-then-read)
  CREATE_OUT="$(kc create "authentication/flows/${FLOW_ENC}/executions/execution" -r "${REALM}" -b "{\"provider\":\"${PROVIDER}\"}" 2>&1)"
  EXEC_ID="$(printf '%s' "${CREATE_OUT}" | sed -n "s/.*id '\\([0-9a-f-]\\{8\\}[0-9a-f-]*\\)'.*/\\1/p")"
  [ -z "${EXEC_ID}" ] && sleep 2 && EXEC_ID="$(get_exec_id)"
else
  echo "Authenticator already present in '${FLOW}'."
fi
if [ -z "${EXEC_ID}" ]; then echo "ERROR: could not determine execution id" >&2; exit 1; fi

# 3) make it REQUIRED and move it to the top (raise-priority is a no-op once first)
kc update "authentication/flows/${FLOW_ENC}/executions" -r "${REALM}" \
  -b "{\"id\":\"${EXEC_ID}\",\"requirement\":\"REQUIRED\"}"
TOP_COUNT="$(kc get "authentication/flows/${FLOW_ENC}/executions" -r "${REALM}" \
  | python3 -c 'import json,sys; print(sum(1 for e in json.load(sys.stdin) if e.get("level",0)==0))')"
for _ in $(seq 1 "${TOP_COUNT:-8}"); do
  kc create "authentication/executions/${EXEC_ID}/raise-priority" -r "${REALM}" >/dev/null 2>&1 || true
done

# 4) point the brokered IdPs at the custom flow
for idp in ${IDPS}; do
  if kc get "identity-provider/instances/${idp}" -r "${REALM}" >/dev/null 2>&1; then
    kc update "identity-provider/instances/${idp}" -r "${REALM}" -s "firstBrokerLoginFlowAlias=${FLOW}"
    echo "IdP '${idp}' -> first broker login flow '${FLOW}'."
  fi
done

echo "Done."
