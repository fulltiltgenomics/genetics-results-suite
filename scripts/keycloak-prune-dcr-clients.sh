#!/bin/bash
set -euo pipefail

# list, and with --delete prune, the clients that anonymous Dynamic Client Registration (RFC 7591)
# left in the RUNNING "genetics" realm. Nothing else removes them: auth-gateway 404s the RFC 7592
# management path, so a registrant cannot delete its own client, and Keycloak never expires one.
#
# Which clients count as DCR: consentRequired=true and a clientId outside the keep-set. The flag is
# set by the anonymous "Consent Required" registration policy (keycloak/mcp-dcr.json.template) on
# every anonymous registration, and nothing committed sets it — the realm template, the brainzzz
# template and keycloak-register-client.sh all leave it false. The keep-set (clients[] of
# keycloak/realm-genetics.json.template, the hand-registered brainzzz and access-grants, Keycloak's
# built-ins) is a second lock, so a by-hand consent toggle on a real client cannot get it pruned.
#
# Keycloak records no creation time on a client, so age is measured from the first run that saw
# it: a write mode stamps the attribute suite.first-seen (ISO 8601 UTC) on each DCR client lacking
# it. --delete then removes a DCR client only when that stamp is at least --older-than full days
# old AND it has no active and no offline session — a client whose refresh token is still in use keeps an
# offline session, so a connector someone is using survives regardless of age.
#
# Usage: DEPLOY_ENV=<env> scripts/keycloak-prune-dcr-clients.sh [--delete [--older-than DAYS]]
#                                                               [--max-clients-headroom]
#   (no flag)               dry run: list every DCR client with name, redirect URIs, first-seen and
#                           session counts, and what --delete would do to it; writes nothing, not
#                           even the first-seen stamp
#   --delete                stamp first-seen where missing, then delete the eligible clients.
#                           The first run stamps everything and so deletes nothing at the default
#                           age; at a "Max Clients" 403, --older-than 0 deletes every session-less
#                           DCR client at once, including one registered seconds ago by someone
#                           still mid-login, so use 0 only to clear that 403 and expect anyone
#                           mid-flow to register afresh
#   --older-than DAYS       age threshold for --delete (default 30)
#   --max-clients-headroom  print only the realm's client count against the anonymous Max Clients
#                           value; the dry run prints the same line after its listing
#
# Max Clients (MCP_DCR_MAX_CLIENTS, default 200) counts EVERY client in the realm, not only DCR
# ones, so the built-ins and the hand-registered clients come off the top. Measured
# on daly-staging: Claude Code registers a new client on every authenticate attempt, named
# "Claude Code (<server name>)" with one loopback redirect each, and claude.ai one per connector.
# One person therefore costs about one client per re-login plus one per claude.ai account, so with
# a monthly --delete at the default age, 200 carries roughly 40-50 regular users at a few
# re-logins a month. Raise MCP_DCR_MAX_CLIENTS when the dry run shows the headroom below about a
# month's intake — not as a substitute for pruning: the cap is also the only bound on how many
# clients an anonymous caller can create, so every slot added is a slot a flood can fill.
#
# Inputs: DEPLOY_ENV selects the deployment (scripts/lib/env.sh); the kubectl context must be the
# one its tfvars names, as for rollout.sh and create-secrets.sh.

MODE=dry-run
OLDER_THAN=30
usage() { echo "Usage: $0 [--delete [--older-than DAYS]] [--max-clients-headroom]" >&2; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --delete) MODE=delete ;;
    --older-than) [ $# -ge 2 ] || usage; OLDER_THAN="$2"; shift ;;
    --max-clients-headroom) MODE=headroom ;;
    *) usage ;;
  esac
  shift
done
case "${OLDER_THAN}" in ''|*[!0-9]*) echo "ERROR: --older-than takes a whole number of days." >&2; exit 2 ;; esac

NAMESPACE="${NAMESPACE:-genetics}"
REALM="${KC_REALM:-genetics}"
CRP_TYPE="org.keycloak.services.clientregistration.policy.ClientRegistrationPolicy"
FIRST_SEEN="suite.first-seen"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

. "${SCRIPT_DIR}/lib/env.sh"
resolve_deploy_env
load_deploy_env
require_kube_context \
  "keycloak-prune-dcr-clients.sh" \
  "client prune" \
  "Pruning clients here" \
  "Keycloak" \
  "    That is one deployment's registered MCP clients being judged, and deleted, by another cluster's sessions."

kube() { kubectl --context "${ACTING_CONTEXT}" -n "${NAMESPACE}" "$@"; }
POD="$(kube get pods -l app=keycloak -o jsonpath='{.items[0].metadata.name}')"
AU="$(kube get secret keycloak-secrets -o jsonpath='{.data.admin-user}' | base64 -d)"
AP="$(kube get secret keycloak-secrets -o jsonpath='{.data.admin-password}' | base64 -d)"

# the pod's root filesystem is read-only, so kcadm's config file has to go under /tmp
KC_CONFIG="--config /tmp/kcadm-prune.config"
kc() { kube exec "${POD}" -- /opt/keycloak/bin/kcadm.sh "$@" ${KC_CONFIG}; }
jget() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

echo "Authenticating kcadm against ${POD} (${ACTING_CONTEXT})..."
kc config credentials --server http://localhost:8080 --realm master --user "${AU}" --password "${AP}" >/dev/null
unset AP

headroom() {
  local total max
  total="$(kc get clients -r "${REALM}" --fields id | jget 'len(d)')"
  max="$(kc get components -r "${REALM}" -q type="${CRP_TYPE}" \
    | jget 'next((c["config"].get("max-clients", ["?"])[0] for c in d if c.get("subType") == "anonymous" and c["providerId"] == "max-clients"), "none")')"
  if [ "${max}" = none ]; then
    echo "Max Clients: realm '${REALM}' has ${total} clients and NO anonymous Max Clients policy — registration is unbounded."
  else
    echo "Max Clients: realm '${REALM}' has ${total} of ${max} clients; anonymous registration is refused (403) at ${max}, $((max - total)) to go."
  fi
}

if [ "${MODE}" = headroom ]; then
  headroom
  exit 0
fi

# the slots are emptied rather than rendered: every client the template can produce is either a
# literal entry or the brainzzz client, which the keep-set names outright
KEEP="$(python3 -c '
import json, re, sys
t = open(sys.argv[1]).read().replace("${MCP_DCR_REGISTRATION_POLICIES}", "[]")
t = re.sub(r"\$\{[A-Z0-9_]+\}", "", t)
ids = {c["clientId"] for c in json.loads(t).get("clients", [])}
ids |= {"brainzzz", "access-grants", "account", "account-console", "admin-cli", "broker",
        "realm-management", "security-admin-console"}
print(json.dumps(sorted(ids)))' "${ROOT_DIR}/keycloak/realm-genetics.json.template")"

NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# kcadm's --fields returns no attributes at all unless the key is named
CLIENTS="$(kc get clients -r "${REALM}" --fields "id,clientId,name,consentRequired,redirectUris,attributes(${FIRST_SEEN})")"
# name and redirect URIs are chosen by the anonymous registrant, so nothing they wrote is ever
# parsed by the shell: only validated ids cross over, and the display fields come back with
# control characters (tab and newline among them) replaced
DCR_IDS="$(python3 -c '
import json, re, sys
keep = set(json.loads(sys.argv[1]))
for c in json.load(sys.stdin):
    if c.get("consentRequired") is True and c["clientId"] not in keep:
        if not re.fullmatch(r"[0-9a-f-]{36}", c["id"]):
            sys.exit("ERROR: DCR client with a malformed internal id %r" % c["id"])
        print(c["id"])
' "${KEEP}" <<< "${CLIENTS}")"
# one "clientId<TAB>name<TAB>redirects<TAB>first-seen" line, safe to split on tabs; a first-seen
# that is not our own timestamp format reads as missing, so it is restamped rather than trusted
show() { python3 -c '
import json, re, sys
c = next(c for c in json.load(sys.stdin) if c["id"] == sys.argv[1])
clean = lambda s: re.sub(r"[\x00-\x1f\x7f]", "?", s)
seen = (c.get("attributes") or {}).get(sys.argv[2], "")
if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", seen):
    seen = ""
print(clean(c["clientId"]), clean(c.get("name") or "-"),
      clean(" ".join(c.get("redirectUris") or []) or "-"), seen, sep="\t")
' "$1" "${FIRST_SEEN}" <<< "${CLIENTS}"; }

if [ "${MODE}" = dry-run ]; then
  echo "DRY RUN: nothing is written, including the first-seen stamp. --delete --older-than ${OLDER_THAN} would:"
else
  echo "Pruning DCR clients at least ${OLDER_THAN} full days old with no sessions:"
fi

n=0 deleted=0
while read -r id; do
  [ -n "${id}" ] || continue
  [[ "${id}" =~ ^[0-9a-f-]{36}$ ]] || { echo "ERROR: refusing malformed client id." >&2; exit 1; }
  row="$(show "${id}")"
  IFS=$'\t' read -r client_id name redirects seen <<< "${row}"
  n=$((n + 1))
  stamped=""
  if [ -z "${seen}" ]; then
    seen="${NOW}"
    if [ "${MODE}" = delete ]; then
      kc update "clients/${id}" -r "${REALM}" -s "attributes.\"${FIRST_SEEN}\"=${seen}"
      stamped=" (stamped now)"
    else
      stamped=" (unstamped; --delete stamps it ${NOW})"
    fi
  fi
  active="$(kc get "clients/${id}/session-count" -r "${REALM}" | jget 'd["count"]')"
  offline="$(kc get "clients/${id}/offline-session-count" -r "${REALM}" | jget 'd["count"]')"
  age="$(python3 -c '
import sys
from datetime import datetime, timezone
seen = datetime.strptime(sys.argv[1], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
print((datetime.now(timezone.utc) - seen).days)' "${seen}")"
  if [ "${active}" != 0 ] || [ "${offline}" != 0 ]; then
    verdict="keep: has sessions"
  elif [ "${age}" -lt "${OLDER_THAN}" ]; then
    verdict="keep: ${age} days old"
  else
    verdict="DELETE"
  fi
  printf '  %s\n    clientId    %s\n    redirects   %s\n    first-seen  %s%s\n    sessions    %s active, %s offline\n    --delete    %s\n' \
    "${name}" "${client_id}" "${redirects}" "${seen}" "${stamped}" "${active}" "${offline}" "${verdict}"
  if [ "${verdict}" = DELETE ] && [ "${MODE}" = delete ]; then
    kc delete "clients/${id}" -r "${REALM}"
    deleted=$((deleted + 1))
    echo "    deleted"
  fi
done <<< "${DCR_IDS}"

echo
if [ "${MODE}" = delete ]; then
  echo "${n} DCR clients, ${deleted} deleted."
else
  echo "${n} DCR clients; none touched."
fi
headroom
