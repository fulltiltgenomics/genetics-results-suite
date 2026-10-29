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
# the allow-list check as the first REQUIRED step, and points the brokered IdPs (IDPS, those of
# them that exist in the realm) at the copy.
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
IDPS="${IDPS:-google apple microsoft}"

POD="$(kubectl get pods -n "${NAMESPACE}" -l app=keycloak -o jsonpath='{.items[0].metadata.name}')"
AU="$(kubectl get secret keycloak-secrets -n "${NAMESPACE}" -o jsonpath='{.data.admin-user}' | base64 -d)"
AP="$(kubectl get secret keycloak-secrets -n "${NAMESPACE}" -o jsonpath='{.data.admin-password}' | base64 -d)"

# the pod's root filesystem is read-only, so kcadm's config file has to go under /tmp
KC_CONFIG="--config /tmp/kcadm-allowlist.config"
kc() { kubectl exec -n "${NAMESPACE}" "${POD}" -- /opt/keycloak/bin/kcadm.sh "$@" ${KC_CONFIG}; }

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

# 3b) disable the "Review Profile" step — the brokered IdPs are trusted, so don't prompt
# federated users with a profile form on first login.
REVIEW_ID="$(kc get "authentication/flows/${FLOW_ENC}/executions" -r "${REALM}" | python3 -c '
import json,sys
for e in json.load(sys.stdin):
    if e.get("providerId")=="idp-review-profile": print(e["id"]); break')"
if [ -n "${REVIEW_ID}" ]; then
  kc update "authentication/flows/${FLOW_ENC}/executions" -r "${REALM}" \
    -b "{\"id\":\"${REVIEW_ID}\",\"requirement\":\"DISABLED\"}"
  echo "Disabled 'Review Profile' in '${FLOW}'."
fi

# 3c) an existing account with the same e-mail is linked automatically instead of asking the
# person to prove it. The built-in "Handle Existing Account" subflow confirms by e-mail (the realm
# has no SMTP) or by password (the realm has none), so a user created before their first login —
# chat-backend's POST /provision/{alias} and keycloak-link-brainzzz-users.sh --create both do that
# — would be stuck at that page. Every brokered IdP here is trust-email, which is the condition
# Keycloak's own docs put on "Automatically set existing user".
EXECS="$(kc get "authentication/flows/${FLOW_ENC}/executions" -r "${REALM}")"
# the copy prefixed every subflow's alias (and so its displayName) with the new flow's name
HANDLE_ID="$(printf '%s' "${EXECS}" | python3 -c '
import json,sys
for e in json.load(sys.stdin):
    if e.get("displayName","").endswith("Handle Existing Account") and e.get("authenticationFlow"): print(e["id"]); break')"
if [ -n "${HANDLE_ID}" ]; then
  kc update "authentication/flows/${FLOW_ENC}/executions" -r "${REALM}" \
    -b "{\"id\":\"${HANDLE_ID}\",\"requirement\":\"DISABLED\"}"
  echo "Disabled 'Handle Existing Account' in '${FLOW}'."
fi
if ! printf '%s' "${EXECS}" | python3 -c '
import json,sys; sys.exit(0 if any(e.get("providerId")=="idp-auto-link" for e in json.load(sys.stdin)) else 1)'; then
  # the execution goes into the "User creation or linking" subflow, after "Create User If Unique"
  # (both ALTERNATIVE: the first one defers to the next when the e-mail already exists); a
  # subflow is addressed by its alias, which for a subflow is what the executions list shows as
  # its displayName
  SUBFLOW_ALIAS="$(printf '%s' "${EXECS}" | python3 -c '
import json,sys,urllib.parse
for e in json.load(sys.stdin):
    if e.get("displayName","").endswith("User creation or linking") and e.get("authenticationFlow"): print(urllib.parse.quote(e["displayName"])); break')"
  [ -n "${SUBFLOW_ALIAS}" ] || { echo "ERROR: no 'User creation or linking' subflow in '${FLOW}'" >&2; exit 1; }
  kc create "authentication/flows/${SUBFLOW_ALIAS}/executions/execution" -r "${REALM}" -b '{"provider":"idp-auto-link"}' >/dev/null
  AUTO_ID="$(kc get "authentication/flows/${FLOW_ENC}/executions" -r "${REALM}" | python3 -c '
import json,sys
for e in json.load(sys.stdin):
    if e.get("providerId")=="idp-auto-link": print(e["id"]); break')"
  kc update "authentication/flows/${FLOW_ENC}/executions" -r "${REALM}" \
    -b "{\"id\":\"${AUTO_ID}\",\"requirement\":\"ALTERNATIVE\"}"
  echo "Added 'Automatically set existing user' to '${FLOW}'."
else
  echo "'Automatically set existing user' already in '${FLOW}'."
fi
# order inside the subflow decides everything: auto-link only has a user to link once "Create
# User If Unique" has run and recorded the duplicate, and run first it fails the login with
# "Invalid username or password" (measured). Keycloak gave the new execution a lower index than
# the existing one, so push it down until it is after
order() {
  kc get "authentication/flows/${FLOW_ENC}/executions" -r "${REALM}" | python3 -c '
import json,sys
ix={e.get("providerId"): e["index"] for e in json.load(sys.stdin) if e.get("providerId") in ("idp-auto-link","idp-create-user-if-unique")}
print(ix.get("idp-auto-link",-1), ix.get("idp-create-user-if-unique",-1))'
}
AUTO_ID="$(kc get "authentication/flows/${FLOW_ENC}/executions" -r "${REALM}" | python3 -c '
import json,sys
for e in json.load(sys.stdin):
    if e.get("providerId")=="idp-auto-link": print(e["id"]); break')"
for _ in 1 2 3 4; do
  read -r AUTO_IX CREATE_IX <<< "$(order)"
  [ "${AUTO_IX}" -gt "${CREATE_IX}" ] && break
  kc create "authentication/executions/${AUTO_ID}/lower-priority" -r "${REALM}" >/dev/null
done
read -r AUTO_IX CREATE_IX <<< "$(order)"
if [ "${AUTO_IX}" -gt "${CREATE_IX}" ]; then
  echo "'Automatically set existing user' runs after 'Create User If Unique'."
else
  echo "ERROR: could not order 'Automatically set existing user' after 'Create User If Unique'" >&2; exit 1
fi

# 4) point the brokered IdPs at the custom flow
for idp in ${IDPS}; do
  if kc get "identity-provider/instances/${idp}" -r "${REALM}" >/dev/null 2>&1; then
    kc update "identity-provider/instances/${idp}" -r "${REALM}" -s "firstBrokerLoginFlowAlias=${FLOW}"
    echo "IdP '${idp}' -> first broker login flow '${FLOW}'."
  fi
done

# 5) make firstName/lastName optional in the user profile. Apple only returns the name on the
# very first authorization and not at all for repeat/Hide-My-Email logins, so requiring them
# forces a "complete your profile" form on federated sign-in. Email stays required.
NEW_PROFILE="$(kc get users/profile -r "${REALM}" | python3 -c '
import json,sys
p=json.load(sys.stdin)
for a in p.get("attributes",[]):
    if a.get("name") in ("firstName","lastName"): a.pop("required",None)
print(json.dumps(p))')"
if [ -n "${NEW_PROFILE}" ]; then
  B64="$(printf '%s' "${NEW_PROFILE}" | base64 | tr -d '\n')"
  kubectl exec -n "${NAMESPACE}" "${POD}" -- bash -c \
    "echo '${B64}' | base64 -d > /tmp/kc-userprofile.json && /opt/keycloak/bin/kcadm.sh update users/profile -r ${REALM} -f /tmp/kc-userprofile.json ${KC_CONFIG} && rm -f /tmp/kc-userprofile.json"
  echo "Made firstName/lastName optional in the user profile."
fi

echo "Done."
