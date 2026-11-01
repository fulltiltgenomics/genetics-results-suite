#!/bin/bash
set -euo pipefail

# reconcile the RUNNING "genetics" realm with keycloak/mcp-dcr.json.template: the realm state that
# lets an MCP client register itself anonymously (RFC 7591) and get tokens /mcp accepts.
#
# The realm import only runs on a fresh DB, so a template edit never reaches an already-imported
# realm on its own; this is that path. It renders the template through scripts/lib/keycloak-dcr.sh,
# the same source deploy.sh's import uses, so the two cannot drift. Idempotent: everything is
# matched by name, and only what differs is written.
#
#   1. the ClientRegistrationPolicy components: each template entry is updated in place or created;
#      the anonymous "Trusted Hosts" policy, which refuses every anonymous registration, is deleted
#   2. the mcp-pkce client profile and policy, merged by name into the realm-wide lists
#   3. the mcp-audience mapper on the built-in basic scope. The import cannot carry it (declaring
#      any client scope there suppresses the built-in ones), so this script is its only owner
#   4. a realm client scope named mcp-audience, a scope-based duplicate of the same audience, is
#      detached from the realm defaults and from every client, then deleted. Left in place it would
#      be a second audience path that only some clients have
#
# Usage: DEPLOY_ENV=<env> scripts/keycloak-sync-dcr-policy.sh [--dry-run]
#   --dry-run  read the live realm, print the kcadm writes it would make, write nothing
#
# Inputs (from .env.<DEPLOY_ENV>):
#   MCP_DCR_MAX_CLIENTS  cap on anonymously registered clients (default in scripts/lib/keycloak-dcr.sh)
#   OAUTH_RESOURCE_URL   the /mcp audience, used only when the live mcp-server Deployment has none
#   DEPLOY_ENV           selects the deployment (scripts/lib/env.sh); the kubectl context must be the
#                        one its tfvars names, as for rollout.sh and create-secrets.sh

DRY_RUN=false
for arg in "$@"; do
  case "${arg}" in
    --dry-run) DRY_RUN=true ;;
    *) echo "Usage: $0 [--dry-run]" >&2; exit 2 ;;
  esac
done

NAMESPACE="${NAMESPACE:-genetics}"
REALM="${KC_REALM:-genetics}"
CRP_TYPE="org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

. "${SCRIPT_DIR}/lib/env.sh"
. "${SCRIPT_DIR}/lib/keycloak-dcr.sh"
resolve_deploy_env
load_deploy_env
require_kube_context \
  "keycloak-sync-dcr-policy.sh" \
  "realm update" \
  "Updating the realm here" \
  "Keycloak" \
  "    That is one deployment's client-registration policy and /mcp audience being written into another cluster's realm."

kube() { kubectl --context "${ACTING_CONTEXT}" -n "${NAMESPACE}" "$@"; }
POD="$(kube get pods -l app=keycloak -o jsonpath='{.items[0].metadata.name}')"
AU="$(kube get secret keycloak-secrets -o jsonpath='{.data.admin-user}' | base64 -d)"
AP="$(kube get secret keycloak-secrets -o jsonpath='{.data.admin-password}' | base64 -d)"
KC_HOSTNAME="$(kube get deploy keycloak -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="KC_HOSTNAME")].value}')"
# the mapper must stamp exactly the audience the mcp-server checks, so read what it was deployed
# with; the env override only covers a cluster where it is not deployed (yet)
LIVE_AUD="$(kube get deploy mcp-server -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="OAUTH_RESOURCE_URL")].value}' 2>/dev/null || true)"
OAUTH_RESOURCE_URL="${LIVE_AUD:-${OAUTH_RESOURCE_URL:-}}"
if [ -z "${OAUTH_RESOURCE_URL}" ]; then
  echo "ERROR: no OAUTH_RESOURCE_URL on the live mcp-server Deployment and none in the environment." >&2
  exit 1
fi
export OAUTH_RESOURCE_URL

# the pod's root filesystem is read-only, so kcadm's config file has to go under /tmp
KC_CONFIG="--config /tmp/kcadm-dcr.config"
kc() { kube exec "${POD}" -- /opt/keycloak/bin/kcadm.sh "$@" ${KC_CONFIG}; }
# every write goes through here, so --dry-run is one switch; a body (the one optional 3rd
# argument) travels over stdin
kc_write() {
  local verb="$1" path="$2" body="${3:-}"
  if [ "${DRY_RUN}" = true ]; then
    echo "  [dry-run] kcadm ${verb} ${path} -r ${REALM}${body:+ -f - <<< ${body}}"
  elif [ -n "${body}" ]; then
    printf '%s' "${body}" | kube exec -i "${POD}" -- /opt/keycloak/bin/kcadm.sh "${verb}" "${path}" -r "${REALM}" -f - ${KC_CONFIG}
  else
    kc "${verb}" "${path}" -r "${REALM}"
  fi
}
jget() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

echo "Authenticating kcadm against ${POD} (${ACTING_CONTEXT})..."
DRY_NOTE=""
if [ "${DRY_RUN}" = true ]; then
  echo "DRY RUN: reads are live, writes are printed and not made."
  DRY_NOTE="DRY RUN: the state below is from BEFORE the writes printed above. "
fi
kc config credentials --server http://localhost:8080 --realm master --user "${AU}" --password "${AP}" >/dev/null
unset AP

DCR_JSON="$(render_mcp_dcr "${ROOT_DIR}/keycloak/mcp-dcr.json.template")"

# --- 1. client-registration policy components -----------------------------------------------------
# the plan is computed in one place and emitted as "verb<TAB>path<TAB>body" lines
REALM_ID="$(kc get "realms/${REALM}" | jget 'd["id"]')"
CRP_PLAN="$(kc get components -r "${REALM}" -q type="${CRP_TYPE}" | python3 -c '
import json, sys
current = json.load(sys.stdin)
wanted, realm_id, crp_type = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3]
# keycloak returns config values as lists in no fixed order, so compare them as sets; it also
# returns an unset key as an empty list where the template omits it
norm = lambda cfg: {k: sorted(v) if isinstance(v, list) else v for k, v in (cfg or {}).items() if v != []}
by_key = {(c.get("subType"), c["name"]): c for c in current}
for w in wanted:
    body = {k: v for k, v in w.items() if k != "subComponents"}
    body.update(providerType=crp_type, parentId=realm_id)
    have = by_key.get((w["subType"], w["name"]))
    if have is None:
        print("create", "components", json.dumps(body), sep="\t")
    elif have["providerId"] != w["providerId"] or norm(have.get("config")) != norm(w["config"]):
        body["id"] = have["id"]
        print("update", "components/" + have["id"], json.dumps(body), sep="\t")
stale = by_key.get(("anonymous", "Trusted Hosts"))
if stale:
    print("delete", "components/" + stale["id"], "", sep="\t")
' "$(printf '%s' "${DCR_JSON}" | mcp_dcr_registration_policies)" "${REALM_ID}" "${CRP_TYPE}")"
if [ -z "${CRP_PLAN}" ]; then
  echo "Client-registration policies already match the template."
else
  echo "Reconciling client-registration policies..."
  while IFS=$'\t' read -r verb path body; do
    kc_write "${verb}" "${path}" "${body}"
  done <<< "${CRP_PLAN}"
fi

# --- 2. mcp-pkce client profile and policy ------------------------------------------------------
# client policies are one realm-wide document each: merge ours in by name, keep the rest. Profiles
# first, because a policy that names a missing profile is refused
for kind in profiles policies; do
  if [ "${kind}" = profiles ]; then label=profile; ours="$(printf '%s' "${DCR_JSON}" | mcp_dcr_client_profile)"
  else label=policy; ours="$(printf '%s' "${DCR_JSON}" | mcp_dcr_client_policy)"; fi
  merged="$(kc get "client-policies/${kind}" -r "${REALM}" | python3 -c '
import json, sys
kind, ours = sys.argv[1], json.loads(sys.argv[2])
items = json.load(sys.stdin).get(kind, [])
if ours in items:
    sys.exit(0)
print(json.dumps({kind: [x for x in items if x["name"] != ours["name"]] + [ours]}))' "${kind}" "${ours}")"
  if [ -z "${merged}" ]; then
    echo "Client ${label} '$(printf '%s' "${ours}" | jget 'd["name"]')' already matches the template."
  else
    echo "Setting client ${label} '$(printf '%s' "${ours}" | jget 'd["name"]')'..."
    kc_write update "client-policies/${kind}" "${merged}"
  fi
done

# --- 3. the /mcp audience mapper on the basic scope ---------------------------------------------
SCOPES="$(kc get client-scopes -r "${REALM}")"
BASIC_ID="$(printf '%s' "${SCOPES}" | jget 'next((c["id"] for c in d if c["name"]=="basic"), "")')"
if [ -z "${BASIC_ID}" ]; then
  echo "ERROR: realm '${REALM}' has no built-in 'basic' client scope to carry the /mcp audience." >&2
  exit 1
fi
MAPPER="$(printf '%s' "${DCR_JSON}" | mcp_dcr_audience_mapper)"
MAPPER_PLAN="$(kc get "client-scopes/${BASIC_ID}/protocol-mappers/models" -r "${REALM}" | python3 -c '
import json, sys
want, base = json.loads(sys.argv[1]), sys.argv[2]
have = next((m for m in json.load(sys.stdin) if m["name"] == want["name"]), None)
if have is None:
    print("create", base, json.dumps(want), sep="\t")
elif have["protocolMapper"] != want["protocolMapper"] or any(have.get("config", {}).get(k) != v for k, v in want["config"].items()):
    print("update", base + "/" + have["id"], json.dumps(dict(want, id=have["id"])), sep="\t")
' "${MAPPER}" "client-scopes/${BASIC_ID}/protocol-mappers/models")"
if [ -z "${MAPPER_PLAN}" ]; then
  echo "basic scope already carries mcp-audience (aud=${OAUTH_RESOURCE_URL})."
else
  echo "Setting mcp-audience mapper on the basic scope (aud=${OAUTH_RESOURCE_URL})..."
  IFS=$'\t' read -r verb path body <<< "${MAPPER_PLAN}"
  kc_write "${verb}" "${path}" "${body}"
fi

# --- 4. stale realm client scope named mcp-audience ---------------------------------------------
STALE_ID="$(printf '%s' "${SCOPES}" | jget 'next((c["id"] for c in d if c["name"]=="mcp-audience"), "")')"
if [ -n "${STALE_ID}" ]; then
  echo "Removing the stale 'mcp-audience' client scope (${STALE_ID})..."
  # detach first so the delete cannot fail on a lingering attachment
  for kind in default-default-client-scopes default-optional-client-scopes; do
    if kc get "${kind}" -r "${REALM}" | jget "any(c['id']=='${STALE_ID}' for c in d)" | grep -q True; then
      kc_write delete "${kind}/${STALE_ID}"
    fi
  done
  # captured first: a failing command substitution in a for word list does not trip set -e
  client_ids="$(kc get clients -r "${REALM}" --fields id | jget '" ".join(c["id"] for c in d)')" || {
    echo "ERROR: could not list the realm's clients to detach the stale scope." >&2
    exit 1
  }
  for cid in ${client_ids}; do
    for kind in default-client-scopes optional-client-scopes; do
      if kc get "clients/${cid}/${kind}" -r "${REALM}" | jget "any(c['id']=='${STALE_ID}' for c in d)" | grep -q True; then
        kc_write delete "clients/${cid}/${kind}/${STALE_ID}"
      fi
    done
  done
  kc_write delete "client-scopes/${STALE_ID}"
fi

# --- state --------------------------------------------------------------------------------------
STATE="$(kc get components -r "${REALM}" -q type="${CRP_TYPE}" | python3 -c '
import json, sys
anon = sorted((c for c in json.load(sys.stdin) if c.get("subType") == "anonymous"), key=lambda c: c["name"])
cfg = lambda pid, key: next((c["config"].get(key, []) for c in anon if c["providerId"] == pid), [])
print("  anonymous policies  " + ", ".join(c["name"] for c in anon))
print("  max clients         " + ",".join(cfg("max-clients", "max-clients")))
print("  allowed scopes      " + " ".join(sorted(cfg("allowed-client-templates", "allowed-client-scopes"))))')"
BASIC_AUD="$(kc get "client-scopes/${BASIC_ID}/protocol-mappers/models" -r "${REALM}" \
  | jget 'next((m["config"].get("included.custom.audience","") for m in d if m["name"]=="mcp-audience"), "") or "NO"')"
cat <<EOF

${DRY_NOTE}Realm '${REALM}' on ${ACTING_CONTEXT}:
${STATE}
  basic mcp-audience  ${BASIC_AUD}
  registration        ${KC_HOSTNAME%/}/realms/${REALM}/clients-registrations/openid-connect
                      (the only registration path auth-gateway proxies; any other path containing clients-registrations is 404)
EOF
