# Rendering of keycloak/brainzzz-sts.json.template, shared by deploy.sh (realm import) and
# keycloak-register-brainzzz.sh (live realm) so the two cannot drift.
#
# Knobs, from .env.<DEPLOY_ENV>:
#   BRAINZZZ_STS_ISSUER      the `iss` of brainzzz's assertions; unset means the feature is off
#   BRAINZZZ_STS_JWKS_URL    where Keycloak fetches the signing keys (default: <issuer>/jwks.json)
#   BRAINZZZ_STS_PUBLIC_KEY  a static verifying key (PEM body or JWKS) used INSTEAD of the JWKS
#                            URL. For when the URL is not reachable from the cluster — brainzzz-dev
#                            publishes its discovery documents behind Google IAP — and for the
#                            staging proving ground, whose issuer has no server at all.
# OAUTH_EMAIL_DOMAIN must be set by the caller (deploy.sh exports it from terraform; the scripts
# read it from the tfvars): the `sub` claim is pinned to that domain by client policy.

brainzzz_sts_enabled() { [ -n "${BRAINZZZ_STS_ISSUER:-}" ]; }

# echo the rendered template; the caller picks the pieces out with jq
render_brainzzz_sts() {
  local template="$1"
  : "${BRAINZZZ_STS_ISSUER:?}" "${OAUTH_EMAIL_DOMAIN:?}"
  local use_jwks="true" jwks_url="${BRAINZZZ_STS_JWKS_URL:-${BRAINZZZ_STS_ISSUER%/}/jwks.json}"
  if [ -n "${BRAINZZZ_STS_PUBLIC_KEY:-}" ]; then
    use_jwks="false"
    jwks_url=""
  fi
  # a Java regex inside a JSON string: the dot is escaped once for the regex and once for JSON
  local domain_regex="${OAUTH_EMAIL_DOMAIN//./\\\\.}"
  BRAINZZZ_STS_USE_JWKS="${use_jwks}" BRAINZZZ_STS_JWKS_URL="${jwks_url}" \
  BRAINZZZ_STS_PUBLIC_KEY="${BRAINZZZ_STS_PUBLIC_KEY:-}" \
  BRAINZZZ_SUB_REGEX="[^@]+@${domain_regex}" \
    envsubst '${BRAINZZZ_STS_ISSUER} ${BRAINZZZ_STS_USE_JWKS} ${BRAINZZZ_STS_JWKS_URL} ${BRAINZZZ_STS_PUBLIC_KEY} ${BRAINZZZ_SUB_REGEX}' \
    < "${template}"
}
