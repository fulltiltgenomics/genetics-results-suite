# Rendering of keycloak/mcp-dcr.json.template, shared by deploy.sh (realm import) and
# keycloak-sync-dcr-policy.sh (live realm) so the two cannot drift. The template holds the realm
# state that lets an MCP client register itself anonymously (RFC 7591) and log in:
#
#   registrationPolicies  the whole ClientRegistrationPolicy component list. A realm import that
#                         declares any of these REPLACES Keycloak's default set for both subtypes,
#                         so the authenticated defaults are restated verbatim or authenticated
#                         registration would run with no policy at all. The anonymous set is
#                         Keycloak's default minus Trusted Hosts, which is what refused every
#                         anonymous registration. "openid" is not a Keycloak client scope, but MCP
#                         clients request it and the policy refuses any name it does not list.
#                         Anonymous registrants may attach no protocol mappers: MCP clients need
#                         none, and Keycloak's default list lets one map username into the email
#                         claim, which the mcp-server would trust. An empty list refuses every type.
#   clientProfile/Policy  mcp-pkce: PKCE S256 enforced on every client, registered or not
#   audienceMapper        aud = OAUTH_RESOURCE_URL on the built-in basic scope, so a registered
#                         client's tokens pass the mcp-server's audience check. Not in the realm
#                         import: declaring any clientScopes there suppresses Keycloak's built-in
#                         scopes and their mappers, so the live-realm script adds it after import.
#
# Knobs, from .env.<DEPLOY_ENV>:
#   MCP_DCR_MAX_CLIENTS  realm-wide cap on anonymously registered clients (default 200); the hard
#                        bound while stale-client cleanup is manual
# OAUTH_RESOURCE_URL must be set by the caller (deploy.sh exports it whenever the broker is on).

# echo the rendered template; the caller picks the pieces out with the mcp_dcr_* extractors
render_mcp_dcr() {
  local template="$1"
  : "${OAUTH_RESOURCE_URL:?}"
  MCP_DCR_MAX_CLIENTS="${MCP_DCR_MAX_CLIENTS:-200}" \
    envsubst '${MCP_DCR_MAX_CLIENTS} ${OAUTH_RESOURCE_URL}' < "${template}"
}

# each reads the rendered template on stdin and prints one member as compact JSON
_mcp_dcr_member() {
  python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)[sys.argv[1]]))' "$1"
}
mcp_dcr_registration_policies() { _mcp_dcr_member registrationPolicies; }
mcp_dcr_client_profile() { _mcp_dcr_member clientProfile; }
mcp_dcr_client_policy() { _mcp_dcr_member clientPolicy; }
mcp_dcr_audience_mapper() { _mcp_dcr_member audienceMapper; }
