#!/bin/bash
set -euo pipefail

# Link realm users to the `brainzzz` identity provider so brainzzz's jwt-bearer assertions can
# resolve them — and unlink them to take that away again.
#
# The grant looks the assertion's `sub` up as a federated identity on the provider and never
# creates or links anyone, so a person brainzzz vouches for still gets `invalid_grant` "User not
# found" until this script has run for them. That makes the link the per-person access control
# on the brainzzz path: brainzzz can only act for the people linked here, and unlinking one
# person revokes brainzzz for them without touching their own GeneGenie login. The link key is
# the e-mail, lower-cased, because that is what brainzzz sends as `sub`.
#
# A linked user still has to clear mcp-server's e-mail allow-list; the realm-wide
# `oauth_email_domain` is what --all-existing filters on.
#
#   keycloak-link-brainzzz-users.sh <email>...              link existing realm users
#   keycloak-link-brainzzz-users.sh --create <email>...     create them first when missing (no
#                                                           GeneGenie login needed, ever)
#   keycloak-link-brainzzz-users.sh --all-existing          link every enabled user on the realm's
#                                                           e-mail domain; re-run after people
#                                                           sign in to GeneGenie for the first time
#   keycloak-link-brainzzz-users.sh --unlink <email>...     revoke brainzzz for these people
#   keycloak-link-brainzzz-users.sh --list                  who is linked
#
# Each kcadm call is a JVM start inside the pod (~2 s); --all-existing costs one call per
# unlinked user, so the first run on a realm with hundreds of users takes minutes.
#
# DEPLOY_ENV selects the deployment (scripts/lib/env.sh); the kubectl context must be the one its
# tfvars names, as for rollout.sh and create-secrets.sh.

NAMESPACE="${NAMESPACE:-genetics}"
REALM="${KC_REALM:-genetics}"
IDP_ALIAS="brainzzz"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() { sed -n '/^#   keycloak-link/,/^#                                                           sign in/p' "$0" | cut -c3-; exit 1; }
MODE="link"; CREATE=false; EMAILS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --create) CREATE=true ;;
    --all-existing) MODE="all" ;;
    --unlink) MODE="unlink" ;;
    --list) MODE="list" ;;
    -h|--help) usage ;;
    --*) echo "unknown option $1" >&2; usage ;;
    *) EMAILS+=("$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')") ;;
  esac
  shift
done
case "${MODE}" in
  link|unlink) [ ${#EMAILS[@]} -gt 0 ] || usage ;;
  all|list) [ ${#EMAILS[@]} -eq 0 ] || usage ;;
esac

. "${SCRIPT_DIR}/lib/env.sh"
resolve_deploy_env
require_kube_context \
  "keycloak-link-brainzzz-users.sh" \
  "user linking" \
  "Linking users here" \
  "Keycloak" \
  "    That is one deployment's people being made reachable through brainzzz on another cluster's realm."
OAUTH_EMAIL_DOMAIN="${OAUTH_EMAIL_DOMAIN:-$(tfvar oauth_email_domain)}"

kube() { kubectl --context "${ACTING_CONTEXT}" -n "${NAMESPACE}" "$@"; }
POD="$(kube get pods -l app=keycloak -o jsonpath='{.items[0].metadata.name}')"
AU="$(kube get secret keycloak-secrets -o jsonpath='{.data.admin-user}' | base64 -d)"
AP="$(kube get secret keycloak-secrets -o jsonpath='{.data.admin-password}' | base64 -d)"
KC_CONFIG="--config /tmp/kcadm-brainzzz-link.config"
kc() { kube exec "${POD}" -- /opt/keycloak/bin/kcadm.sh "$@" -r "${REALM}" ${KC_CONFIG}; }
# the expression travels in the environment, so it can quote freely and the shell never re-parses it
jget() { EXPR="$1" python3 -c 'import json,os,sys; d=json.load(sys.stdin); print(eval(os.environ["EXPR"]))'; }

kube exec "${POD}" -- /opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 \
  --realm master --user "${AU}" --password "${AP}" ${KC_CONFIG} >/dev/null

kc get identity-provider/instances/${IDP_ALIAS} >/dev/null 2>&1 \
  || { echo "ERROR: no '${IDP_ALIAS}' identity provider on realm '${REALM}' — run keycloak-register-brainzzz.sh first." >&2; exit 1; }

# every user already linked, as "email id" lines; one call instead of one per user
linked_users() { kc get users -q idpAlias="${IDP_ALIAS}" -q max=5000 | jget '"\n".join((u.get("email") or "").lower() + " " + u["id"] for u in d)'; }
user_id() {  # exact e-mail match; the query is a prefix search unless told otherwise
  kc get users -q email="$1" -q exact=true -q max=10 | EMAIL="$1" jget 'next((u["id"] for u in d if (u.get("email") or "").lower() == os.environ["EMAIL"]), "")'
}
link() {  # $1 email, $2 user id
  kc create "users/$2/federated-identity/${IDP_ALIAS}" \
    -s identityProvider="${IDP_ALIAS}" -s userId="$1" -s userName="$1" >/dev/null
  echo "linked   $1"
}

case "${MODE}" in
  list)
    LINKED="$(linked_users)"
    printf '%s\n' "${LINKED}" | awk 'NF{print $1}' | sort
    echo "$(printf '%s\n' "${LINKED}" | awk 'NF' | wc -l) linked to '${IDP_ALIAS}' on ${ACTING_CONTEXT}"
    ;;
  all)
    LINKED="$(linked_users)"
    kc get users -q enabled=true -q max=5000 \
      | DOMAIN="${OAUTH_EMAIL_DOMAIN}" jget '"\n".join(u["email"].lower() + " " + u["id"] for u in d if (u.get("email") or "").lower().endswith("@" + os.environ["DOMAIN"]))' \
      | while read -r email uid; do
          [ -n "${email}" ] || continue
          if printf '%s\n' "${LINKED}" | grep -q "^${email} "; then echo "already  ${email}"; else link "${email}" "${uid}"; fi
        done
    ;;
  link)
    for email in "${EMAILS[@]}"; do
      UID_="$(user_id "${email}")"
      if [ -z "${UID_}" ]; then
        if [ "${CREATE}" = true ]; then
          kc create users -s username="${email}" -s email="${email}" -s enabled=true -s emailVerified=true >/dev/null
          UID_="$(user_id "${email}")"
          echo "created  ${email}"
        else
          echo "missing  ${email}  (no realm user; they have not signed in to GeneGenie — pass --create to make one)"
          continue
        fi
      fi
      if kc get "users/${UID_}/federated-identity" | ALIAS="${IDP_ALIAS}" jget 'any(f["identityProvider"] == os.environ["ALIAS"] for f in d)' | grep -q True; then
        echo "already  ${email}"
      else
        link "${email}" "${UID_}"
      fi
    done
    ;;
  unlink)
    for email in "${EMAILS[@]}"; do
      UID_="$(user_id "${email}")"
      [ -n "${UID_}" ] || { echo "missing  ${email}"; continue; }
      if kc delete "users/${UID_}/federated-identity/${IDP_ALIAS}" >/dev/null 2>&1; then echo "unlinked ${email}"; else echo "notlinked ${email}"; fi
    done
    ;;
esac
