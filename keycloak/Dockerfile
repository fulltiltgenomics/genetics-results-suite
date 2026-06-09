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
# bundled provider JARs (empty providers/ just builds vanilla Keycloak)
COPY providers/ /opt/keycloak/providers/
RUN /opt/keycloak/bin/kc.sh build

FROM quay.io/keycloak/keycloak:${KEYCLOAK_VERSION}
COPY --from=builder /opt/keycloak/ /opt/keycloak/
ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
