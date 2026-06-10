# Keycloak image for the genetics suite: official Keycloak + the "Sign in with Apple"
# identity-provider extension. Apple's OIDC needs a JWT client secret (signed with the
# .p8 key, regenerated periodically) and form_post — an extension handles that for us.
#
# Drop the verified extension JAR(s) into keycloak/providers/ before building. Recommended:
# klausbetz/apple-identity-provider (generates the Apple client-secret JWT automatically).
# Pin the version you vet; do not pull an unpinned latest.
ARG KEYCLOAK_VERSION=26.5

FROM quay.io/keycloak/keycloak:${KEYCLOAK_VERSION} AS builder
ENV KC_DB=postgres
# health must be baked in at build time: the deployment runs `start --optimized` (which ignores
# build-time options set at runtime) and probes /health on :9000, so enabling it here is required.
ENV KC_HEALTH_ENABLED=true
# bundled provider JARs: the Apple IdP extension and the email allow-list script authenticator
# (keycloak/providers/email-allowlist-authenticator.jar). The latter needs the "scripts" feature.
COPY providers/ /opt/keycloak/providers/
RUN /opt/keycloak/bin/kc.sh build --features=scripts

FROM quay.io/keycloak/keycloak:${KEYCLOAK_VERSION}
COPY --from=builder /opt/keycloak/ /opt/keycloak/
ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
