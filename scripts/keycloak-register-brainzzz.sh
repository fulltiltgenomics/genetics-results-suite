#!/bin/bash
set -euo pipefail

# Register (or reconcile) brainzzz on the RUNNING Keycloak "genetics" realm: the confidential
# OAuth client, and — when BRAINZZZ_STS_ISSUER is set — brainzzz's STS as an identity provider for
# the RFC 7523 jwt-bearer grant plus the client policy that gates its assertions.
#
# The realm import only runs on a fresh DB, so the templates under keycloak/ do NOT affect an
# already-imported realm — this reconciles the live one via the admin API from the same templates
# deploy.sh renders, so the two cannot drift. Idempotent; re-run to rotate the secret, change
# redirect URIs, or move the STS issuer or its keys.
#
# The grant never creates users: Keycloak resolves the assertion's `sub` through an existing
# federated-identity link to the `brainzzz` provider and answers `invalid_grant` "User not found"
# otherwise. Linking is scripts/keycloak-link-brainzzz-users.sh, and it is the access control on
# this path — brainzzz can only act for the people linked.
#
# Inputs (from .env.<DEPLOY_ENV>, kept out of version control):
#   BRAINZZZ_CLIENT_SECRET   confidential client secret; share this with the brainzzz developer
#   BRAINZZZ_STS_ISSUER      the `iss` of brainzzz's assertions; unset leaves the grant unconfigured
#   BRAINZZZ_STS_JWKS_URL    signing keys (default <issuer>/jwks.json); must be reachable unauthenticated
#   BRAINZZZ_STS_PUBLIC_KEY  static verifying key instead of the URL (scripts/lib/keycloak-brainzzz.sh)
#   OAUTH_RESOURCE_URL       the /mcp audience (default: derived from the deployment's KC_HOSTNAME)
#   DEPLOY_ENV               selects the deployment (scripts/lib/env.sh); the kubectl context must
#                            be the one its tfvars names, as for rollout.sh and create-secrets.sh

NAMESPACE="${NAMESPACE:-genetics}"
REALM="${KC_REALM:-genetics}"
CLIENT_ID="brainzzz"
IDP_ALIAS="brainzzz"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

. "${SCRIPT_DIR}/lib/env.sh"
. "${SCRIPT_DIR}/lib/keycloak-brainzzz.sh"
resolve_deploy_env
load_deploy_env
require_kube_context \
  "keycloak-register-brainzzz.sh" \
  "client registration" \
  "Registering brainzzz here" \
  "Keycloak" \
  "    That is one deployment's brainzzz credential and trust root being wired into another cluster's realm."

: "${BRAINZZZ_CLIENT_SECRET:?Set BRAINZZZ_CLIENT_SECRET (confidential client secret) in ${ENV_FILE}}"
OAUTH_EMAIL_DOMAIN="${OAUTH_EMAIL_DOMAIN:-$(tfvar oauth_email_domain)}"
: "${OAUTH_EMAIL_DOMAIN:?oauth_email_domain missing from ${TFVARS}}"

kube() { kubectl --context "${ACTING_CONTEXT}" -n "${NAMESPACE}" "$@"; }
POD="$(kube get pods -l app=keycloak -o jsonpath='{.items[0].metadata.name}')"
AU="$(kube get secret keycloak-secrets -o jsonpath='{.data.admin-user}' | base64 -d)"
AP="$(kube get secret keycloak-secrets -o jsonpath='{.data.admin-password}' | base64 -d)"
KC_HOSTNAME="$(kube get deploy keycloak -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="KC_HOSTNAME")].value}')"
REALM_ISSUER="${KC_HOSTNAME%/}/realms/${REALM}"
OAUTH_RESOURCE_URL="${OAUTH_RESOURCE_URL:-${KC_HOSTNAME%/auth}/mcp}"
export BRAINZZZ_CLIENT_SECRET OAUTH_RESOURCE_URL OAUTH_EMAIL_DOMAIN

# the pod's root filesystem is read-only, so kcadm's config file has to go under /tmp
KC_CONFIG="--config /tmp/kcadm-brainzzz.config"
kc() { kube exec "${POD}" -- /opt/keycloak/bin/kcadm.sh "$@" ${KC_CONFIG}; }
# bodies carry the client secret, so they travel over stdin, never on a command line
kc_body() { kube exec -i "${POD}" -- /opt/keycloak/bin/kcadm.sh "$@" -r "${REALM}" -f - ${KC_CONFIG}; }
jget() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

echo "Authenticating kcadm against ${POD} (${ACTING_CONTEXT})..."
kc config credentials --server http://localhost:8080 --realm master --user "${AU}" --password "${AP}" >/dev/null

# --- client -------------------------------------------------------------------------------------
CLIENT_JSON="$(envsubst '${BRAINZZZ_CLIENT_SECRET} ${OAUTH_RESOURCE_URL}' < "${ROOT_DIR}/keycloak/brainzzz-client.json.template")"
CID="$(kc get clients -r "${REALM}" -q clientId="${CLIENT_ID}" | jget 'd[0]["id"] if d else ""')"
if [ -z "${CID}" ]; then
  echo "Creating client '${CLIENT_ID}'..."
  printf '%s' "${CLIENT_JSON}" | kc_body create clients
  CID="$(kc get clients -r "${REALM}" -q clientId="${CLIENT_ID}" | jget 'd[0]["id"]')"
else
  echo "Client '${CLIENT_ID}' exists (${CID}); updating..."
  # a client update manages neither mappers nor scopes, so those keys are dropped from the body
  # and reconciled below; attributes present in the body are set, absent ones left alone
  printf '%s' "${CLIENT_JSON}" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); [d.pop(k,None) for k in ("protocolMappers","defaultClientScopes","optionalClientScopes")]; print(json.dumps(d))' \
    | kc_body update "clients/${CID}"
fi

# the mcp-audience mapper: access tokens must carry aud=${OAUTH_RESOURCE_URL} or mcp-server rejects
# them. Delete-then-create, because an in-place mapper update needs the id inside the body
MAPPER_JSON="$(printf '%s' "${CLIENT_JSON}" | jget 'json.dumps(next(m for m in d["protocolMappers"] if m["name"]=="mcp-audience"))')"
MID="$(kc get "clients/${CID}/protocol-mappers/models" -r "${REALM}" | jget 'next((m["id"] for m in d if m.get("name")=="mcp-audience"), "")')"
[ -n "${MID}" ] && kc delete "clients/${CID}/protocol-mappers/models/${MID}" -r "${REALM}"
echo "Setting mcp-audience mapper (aud=${OAUTH_RESOURCE_URL})..."
printf '%s' "${MAPPER_JSON}" | kc_body create "clients/${CID}/protocol-mappers/models"

# offline_access as an OPTIONAL client scope: without it Keycloak silently drops the scope from
# the authorize request and the refresh token cannot outlive the SSO session
SCOPE_ID="$(kc get client-scopes -r "${REALM}" | jget 'next((c["id"] for c in d if c.get("name")=="offline_access"), "")')"
if [ -z "${SCOPE_ID}" ]; then
  echo "WARNING: no 'offline_access' client scope on realm '${REALM}' — skipping" >&2
elif [ -z "$(kc get "clients/${CID}/optional-client-scopes" -r "${REALM}" | jget 'next((c["id"] for c in d if c.get("name")=="offline_access"), "")')" ]; then
  echo "Assigning offline_access as an optional client scope..."
  # the endpoint takes no body, but kcadm refuses to send a PUT without one
  kc update "clients/${CID}/optional-client-scopes/${SCOPE_ID}" -r "${REALM}" -b '{}'
fi

# --- STS identity provider + client policy -------------------------------------------------------
if ! brainzzz_sts_enabled; then
  echo "BRAINZZZ_STS_ISSUER is not set in ${ENV_FILE}: client reconciled, jwt-bearer grant left unconfigured."
  echo "Give the brainzzz developer: client_id=${CLIENT_ID}, client_secret=<BRAINZZZ_CLIENT_SECRET>, issuer=${REALM_ISSUER}, mcp_url=${OAUTH_RESOURCE_URL}, scopes=openid email profile offline_access"
  exit 0
fi

# the jwt-authorization-grant provider type exists from Keycloak 26.6; on an older server the
# create below fails with an opaque 400, so say what is actually missing
KC_VERSION="$(kc get serverinfo | jget 'd["systemInfo"]["version"]')"
case "${KC_VERSION}" in
  26.[0-5].*|2[0-5].*) echo "ERROR: Keycloak ${KC_VERSION} predates the JWT Authorization Grant (26.6). Roll the keycloak image first." >&2; exit 1 ;;
esac

STS_JSON="$(render_brainzzz_sts "${ROOT_DIR}/keycloak/brainzzz-sts.json.template")"
IDP_JSON="$(printf '%s' "${STS_JSON}" | jget 'json.dumps(d["identityProvider"])')"
JWKS_URL="$(printf '%s' "${IDP_JSON}" | jget 'd["config"]["jwksUrl"]')"
if [ -n "${JWKS_URL}" ]; then
  # Keycloak fetches the keys lazily, on the first assertion, so a URL it cannot read fails as
  # "Invalid signature" much later and far from here. Check from this machine now. brainzzz-dev
  # answers these paths with a 302 to Google IAP, which is exactly this failure.
  # a warning, not a refusal: the provider has to exist for users to be linked and provisioned
  # against it before brainzzz has fixed its side, and the grant is unusable either way until
  # the keys can be read
  JWKS_WARNING=""
  if ! curl -sSf -m 15 -o /dev/null -H 'Accept: application/json' "${JWKS_URL}" \
     || ! curl -sS -m 15 -H 'Accept: application/json' "${JWKS_URL}" | python3 -c 'import json,sys; assert json.load(sys.stdin)["keys"]' 2>/dev/null; then
    JWKS_WARNING="WARNING: ${JWKS_URL} is not an unauthenticated JWKS document (redirect, non-200, or no 'keys').
         Every assertion will fail with \"Invalid signature\" until brainzzz publishes it without IAP,
         or BRAINZZZ_STS_PUBLIC_KEY is set to the key and this script re-run."
    echo "${JWKS_WARNING}" >&2
  fi
fi

if kc get "identity-provider/instances/${IDP_ALIAS}" -r "${REALM}" >/dev/null 2>&1; then
  echo "IdP '${IDP_ALIAS}' exists; replacing its config from the template..."
  # a full PUT rather than a merge, so a key source that was switched (URL -> static key) does not
  # leave the old one behind; the internalId has to ride along for Keycloak to accept the body
  INTERNAL_ID="$(kc get "identity-provider/instances/${IDP_ALIAS}" -r "${REALM}" | jget 'd["internalId"]')"
  printf '%s' "${IDP_JSON}" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); d['internalId']='${INTERNAL_ID}'; print(json.dumps(d))" \
    | kc_body update "identity-provider/instances/${IDP_ALIAS}"
else
  echo "Creating IdP '${IDP_ALIAS}' (issuer ${BRAINZZZ_STS_ISSUER})..."
  printf '%s' "${IDP_JSON}" | kc_body create identity-provider/instances
fi

# client policies are one realm-wide document each: merge ours in by name, keep the rest
echo "Setting client profile and policy 'brainzzz-assertion' (sub pinned to @${OAUTH_EMAIL_DOMAIN})..."
for kind in profiles policies; do
  key="clientProfile"; [ "${kind}" = policies ] && key="clientPolicy"
  kc get "client-policies/${kind}" -r "${REALM}" \
    | python3 -c "
import json,sys
doc=json.load(sys.stdin); ours=json.loads(sys.argv[1])['${key}']
items=[x for x in doc.get('${kind}',[]) if x['name']!=ours['name']]+[ours]
print(json.dumps({'${kind}': items}))" "${STS_JSON}" \
    | kc_body update "client-policies/${kind}"
done

LINKED="$(kc get users -r "${REALM}" -q idpAlias="${IDP_ALIAS}" -q max=1000 | jget 'len(d)')"
cat <<EOF

Done. Keycloak ${KC_VERSION}, realm '${REALM}' on ${ACTING_CONTEXT}:
  client            ${CLIENT_ID}  (aud ${OAUTH_RESOURCE_URL}; jwt-bearer grant enabled for IdP '${IDP_ALIAS}')
  identity provider ${IDP_ALIAS}  issuer=${BRAINZZZ_STS_ISSUER}  keys=${JWKS_URL:-<static BRAINZZZ_STS_PUBLIC_KEY>}
  client policy     brainzzz-assertion: sub matches [^@]+@${OAUTH_EMAIL_DOMAIN}, act present
  linked users      ${LINKED}  (scripts/keycloak-link-brainzzz-users.sh; an unlinked sub gets invalid_grant "User not found")

Give the brainzzz developer:
  token endpoint  ${REALM_ISSUER}/protocol/openid-connect/token
  grant_type      urn:ietf:params:oauth:grant-type:jwt-bearer   with client_id=${CLIENT_ID}, client_secret, assertion=<JWT>
  assertion       RS256 by a key at ${JWKS_URL:-<the registered key>}; iss=${BRAINZZZ_STS_ISSUER}; aud=${REALM_ISSUER} (exactly one value);
                  sub=<user email> (the link key: keep it the email); exp within 300 s; unique jti (no reuse); act present
  response        access_token for aud ${OAUTH_RESOURCE_URL}, no refresh token; send it as Bearer to ${OAUTH_RESOURCE_URL}
  provisioning    on invalid_grant "User not found": POST ${KC_HOSTNAME%/auth}/provision/${CLIENT_ID} {"email"} with a
                  client_credentials token of this client (chat-backend; BROKER_PROVISIONING_CLIENTS), then retry
${JWKS_WARNING:+
${JWKS_WARNING}}
EOF
