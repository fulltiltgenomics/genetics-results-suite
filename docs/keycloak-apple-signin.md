# Keycloak identity broker — Google + Apple sign-in

> **Per-profile**: the broker is enabled by `ENABLE_KEYCLOAK` in `deploy.sh` (default: on for
> `config_profile=daly`, off for `finngen`). Where it's off, oauth2-proxy talks to Google
> directly and none of the Keycloak/Postgres resources are deployed. Each deployment keeps its
> own gitignored tfvars (`terraform/terraform.tfvars.daly`, `terraform/terraform.tfvars.finngen`);
> copy the right one to `terraform/terraform.tfvars` before deploying.

This suite authenticates browser users through **oauth2-proxy → Keycloak → {Google, Apple}**.
Keycloak is the identity broker: it presents the provider chooser, federates Google and
Apple, and exposes a single OIDC issuer that oauth2-proxy trusts. Backends are unchanged —
they read the user's email from the `X-Goog-Authenticated-User-Email` header (the
`accounts.google.com:` prefix is legacy/provider-agnostic; only the address after the colon
is used) and authorize it against the allow-list.

```
browser → GKE ingress → auth-gateway (nginx)
            ├── <domain>/...  → oauth2-proxy (OIDC) ──▶ Keycloak realm "genetics"
            └── <domain>/auth ──────────────────────▶ Keycloak (login UI + OIDC endpoints)
                                                         ├── Google IdP
                                                         └── Apple IdP (bundled extension)
Keycloak DB: in-cluster Postgres (PVC) → daily pg_dump to GCS
```

Keycloak is exposed under the **`/auth` path on the primary domain** (e.g.
`https://genegenie.broadinstitute.org/auth`) rather than a dedicated `auth.<domain>` subdomain,
so it reuses the existing DNS record, managed cert and ingress with no extra provisioning. It
keeps its default `/` relative path and advertises the `/auth` prefix via `KC_HOSTNAME`; the
auth-gateway `location /auth/` strips the prefix when proxying. To move to a dedicated subdomain
once its DNS exists, see [Switching Keycloak to a dedicated subdomain](#switching-keycloak-to-a-dedicated-subdomain).

## Components added

| Piece | Where |
|-------|-------|
| Keycloak image (official + Apple extension) | `keycloak/Dockerfile`, JARs in `keycloak/providers/` |
| Realm import template | `keycloak/realm-genetics.json.template` (rendered to the `keycloak-realm` Secret at deploy) |
| Keycloak deployment | `k8s/deployments/keycloak.yaml` |
| Keycloak Postgres + PVC | `k8s/deployments/postgres.yaml`, `k8s/volumes/pvc-keycloak-postgres.yaml` |
| Daily backup CronJob | `k8s/cronjobs/keycloak-postgres-backup.yaml` |
| Backup bucket + IAM | `terraform/backups.tf` (`google_storage_bucket.keycloak_backups`) |
| Path routing | `k8s/deployments/auth-gateway.yaml` (`location /auth/` injected via `${KEYCLOAK_SERVER}`) |
| oauth2-proxy repoint | `k8s/deployments/oauth2-proxy.yaml` (`--provider=oidc`) |
| Allow-list (domains + addresses) | `oauth_email_domain`, `oauth_allowed_emails` → `bearer-auth-allowed` + oauth2-proxy |

## One-time prerequisites

### 1. Keycloak host
Keycloak's issuer must be browser-reachable over TLS. Add the host to terraform `domains`
so the GKE managed cert covers it and the ingress routes it to the auth-gateway:

```hcl
domains = ["finngenie.finngen.fi", "finngenie.fi", "auth.finngenie.finngen.fi"]
```

`KEYCLOAK_HOST` defaults to `auth.${DOMAIN}` in `deploy.sh`; override via env if needed.
Point a DNS record for the host at the ingress static IP.

### 2. Apple Developer (Sign in with Apple)
Requires a paid Apple Developer Program membership. In the portal:
1. **Identifiers → App ID**: create one with the "Sign in with Apple" capability enabled.
2. **Identifiers → Services ID** (switch the Identifiers list filter to "Services IDs" — it
   defaults to App IDs): create one (this string is the OAuth `client_id` =
   `APPLE_SERVICES_ID`) and enable Sign in with Apple on it.
3. On the **Services ID** (not the App ID), open **Sign in with Apple → Configure →
   Web Authentication Configuration**: set **Primary App ID** = the App ID from step 1,
   **Domains and Subdomains** = `<KEYCLOAK_HOST>`, **Return URLs** =
   `https://<KEYCLOAK_HOST>/realms/genetics/broker/apple/endpoint` (confirm the exact callback
   path against the bundled extension's docs) → **Save**. NOTE: the current Apple portal does
   **not** do domain-verification for the web sign-in flow — there is no
   `apple-developer-domain-association.txt` / Verify step here, just Domains + Return URLs. That
   file only exists under the separate "Sign in with Apple for Email Communication" feature
   (private-relay email *sending*), which this setup does not use.
4. **Keys → new Key** with Sign in with Apple enabled; download the **.p8 once** (can't be
   re-downloaded). Note the **Key ID** and your **Team ID**.

### 3. Google OAuth client
Reuse or create a Google OAuth 2.0 client. Authorized redirect URI:
`https://<KEYCLOAK_HOST>/realms/genetics/broker/google/endpoint`.

### 4. Apple Keycloak extension
Drop the vetted Apple identity-provider JAR into `keycloak/providers/` (e.g.
`klausbetz/apple-identity-provider`, pinned). It generates Apple's short-lived ES256 JWT
client secret from the .p8 key and handles `response_mode=form_post`. **Validate the
`providerId` and config keys in `keycloak/realm-genetics.json.template` against the version
you bundle before the first deploy.**

## Secrets / `.env`

`scripts/create-secrets.sh` creates `keycloak-secrets` (DB + bootstrap admin; passwords are
generated and reused from the cluster if already present). The realm import gets its
provider secrets from `.env` (gitignored), rendered by `deploy.sh` into the `keycloak-realm`
Secret. Set in `.env`:

```sh
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
OAUTH2_PROXY_CLIENT_SECRET=... # shared with the oauth2-proxy client in the realm
```

Apple is **wired in but auto-optional**: `deploy.sh` injects the Apple IdP
(`keycloak/apple-idp.json.template`) into the realm only when `APPLE_SERVICES_ID` is set in
`.env` — until then the realm is Google-only, so you can ship Google now and add Apple once
you've registered for the Apple Developer Program and dropped the extension JAR into
`keycloak/providers/`. The Apple vars:

```sh
APPLE_SERVICES_ID=...          # the Services ID (client_id)
APPLE_TEAM_ID=...
APPLE_KEY_ID=...
APPLE_P8_KEY='-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----'   # newlines as \n (valid JSON)
```

The oauth2-proxy↔Keycloak client credentials also go in the separately-created
`oauth2-proxy-secrets` Secret (see README): `client-id=oauth2-proxy`,
`client-secret=${OAUTH2_PROXY_CLIENT_SECRET}` (must match the realm), plus the existing
`cookie-secret`.

## Deploy

```sh
# build images (includes the local keycloak build) and create secrets
REGISTRY=... ./scripts/build-all.sh
ANTHROPIC_API_KEY=... COHERE_API_KEY=... ./scripts/create-secrets.sh
# full deploy: terraform (bucket, domains), renders keycloak-realm, applies all manifests
./scripts/deploy.sh
```

The realm is imported only on Keycloak's **first** start (empty DB). Later changes are made
in the admin console (`https://<KEYCLOAK_HOST>/admin`, user/pass from `keycloak-secrets`).

**Issuer hairpin**: oauth2-proxy fetches OIDC discovery from `https://<KEYCLOAK_HOST>/realms/genetics/...`,
i.e. the public URL routed back through the ingress from inside the cluster. This is verified
working on GKE for the daly deployment. If hairpin NAT is blocked in your network, either allow
it, or split discovery from the issuer: set `OAUTH2_PROXY_SKIP_OIDC_DISCOVERY=true` and point
`OAUTH2_PROXY_LOGIN_URL` at the public URL (browser-facing) while pointing
`OAUTH2_PROXY_REDEEM_URL` / `OAUTH2_PROXY_OIDC_JWKS_URL` at the in-cluster
`http://keycloak.genetics.svc.cluster.local:8080/realms/genetics/...` endpoints — Keycloak still
serves those at `/realms/...` internally (only the advertised URLs carry the `/auth` prefix), and
the issuer string keeps matching token `iss`.

## Switching Keycloak to a dedicated subdomain

Keycloak currently lives at the **`/auth` path on the primary domain** (no separate DNS/cert
needed). To move it to a dedicated `auth.<domain>` subdomain once that DNS record exists, make
these changes (all driven from `deploy.sh`):

1. **DNS** — create an A record `auth.<domain>` → the ingress static IP (`terraform output
   static_ip` / the value behind `kubernetes.io/ingress.global-static-ip-name`).
2. **`scripts/deploy.sh`** (Keycloak broker block): set `KEYCLOAK_PATH=""` and
   `KEYCLOAK_HOST="auth.${REDIRECT_TO_HOST:-${DOMAIN}}"`, and change the `KEYCLOAK_SERVER`
   snippet from the `location /auth/ { … proxy_pass …:8080/; }` form back to a dedicated
   `server { listen 8080; server_name <host>; location / { proxy_pass …:8080; } }` block.
   With no path prefix, `proxy_pass` must **not** have a trailing slash (no prefix to strip).
3. **`k8s/deployments/auth-gateway.yaml`** — move the `${KEYCLOAK_SERVER}` placeholder back out
   of the main `server { }` block to the `http { }` level (a sibling server block), since it
   now carries its own `server_name`.
4. **Cert + ingress** — add `KEYCLOAK_HOST` to `generate_cert`'s `DOMAIN_LIST` (managed-cert
   SAN) and to `generate_ingress`'s host rules so `auth.<domain>` gets a cert and routes to
   `auth-gateway`. Wait for the managed cert to re-provision (DNS must resolve first; 15–60 min).
5. **Provider consoles** — update the Google/Apple authorized redirect URIs to
   `https://auth.<domain>/realms/genetics/broker/{google,apple}/endpoint` (the `/auth` segment
   drops out of the path when the prefix moves to a subdomain).

`KC_HOSTNAME` (`https://${KEYCLOAK_HOST}`) and `OIDC_ISSUER_URL`
(`https://${KEYCLOAK_HOST}/realms/genetics`) follow `KEYCLOAK_HOST` automatically, so no edits
to `keycloak.yaml`/`oauth2-proxy.yaml` are needed. Because the issuer string changes, existing
sessions are invalidated (users re-login once).

## Allow-list (who may log in)

Two layers, both enforced on the email Keycloak returns:
- **Domains** — `oauth_email_domain`, comma-separated (`broadinstitute.org,finngen.fi`).
- **Specific addresses** — `oauth_allowed_emails` (`a@apple.com,b@me.com,c@privaterelay.appleid.com`).

Both flow to oauth2-proxy (`OAUTH2_PROXY_EMAIL_DOMAINS` + the generated `allowed-emails.txt`)
and to the backends (`bearer-auth-allowed` ConfigMap). "Hide My Email" Apple users arrive as
`@privaterelay.appleid.com` — allow them by adding their specific relay address to
`oauth_allowed_emails`.

They are **also** enforced at Keycloak during first-broker-login, *before* an account is
created, so a non-allowlisted federated user gets a clean Forbidden page (not a Keycloak account
plus a downstream oauth2-proxy 403). This is a small script authenticator
(`keycloak/email-allowlist-authenticator/`, packaged as
`keycloak/providers/email-allowlist-authenticator.jar`, needs the `scripts` build feature — see
`keycloak/Dockerfile`). It reads the realm attributes `allowedEmailDomains` / `allowedEmails`,
which `deploy.sh` renders from `oauth_email_domain` / `oauth_allowed_emails`.

The realm import only runs on a fresh DB, so on an already-imported realm bind it (and re-sync
the attributes after changing the allow-list) with:

```sh
OAUTH_EMAIL_DOMAIN=... OAUTH_ALLOWED_EMAILS=... ./scripts/keycloak-bind-allowlist.sh
```

This is idempotent: it sets the realm attributes and inserts the `script-email-allowlist.js`
authenticator as the first REQUIRED step of the **first broker login** flow.

## Apple client-secret rotation

Apple's client secret is a JWT signed with the .p8 key, max ~6-month lifetime. The bundled
extension regenerates it from the key automatically, so **rotate the .p8 key before it
expires** (Apple keys don't expire, but generate a new one if compromised) and keep
`APPLE_*` in `.env` current. If you switch to manually-managed client secrets instead of the
extension, add a CronJob to regenerate and patch it.

## Backup & restore (Keycloak Postgres)

- **Backup**: `keycloak-postgres-backup` CronJob runs `pg_dump | gzip` daily at 02:00 UTC to
  `gs://<project>-keycloak-backups/keycloak/keycloak-<ts>.sql.gz`. Retention is enforced by
  the bucket lifecycle rule (`keycloak_backup_retention_days`, default 14). GCS auth is via
  Workload Identity; with `manage_iam=false`, grant the workload SA `roles/storage.objectAdmin`
  on the bucket manually.
- **Restore**:
  ```sh
  gsutil cp gs://<project>-keycloak-backups/keycloak/keycloak-<ts>.sql.gz - | gunzip | \
    kubectl exec -i -n genetics deploy/keycloak-postgres -- psql -U keycloak -d keycloak
  ```
  Restore into an empty DB; restart the `keycloak` deployment afterward.

## Adding more providers later

The broker design makes this cheap — oauth2-proxy, the auth-gateway, the backends, and the
frontend never change. Adding a provider is purely a Keycloak realm change:

1. In the realm, add an **identity provider** (admin console → Identity Providers, or another
   entry in `keycloak/realm-genetics.json.template`). **Apple** is already wired —
   `deploy.sh` injects `keycloak/apple-idp.json.template` automatically once `APPLE_*` is set
   in `.env` (and the extension JAR is in `keycloak/providers/`); no manual step needed.
2. In that provider's own console, register the redirect URI
   `https://<KEYCLOAK_HOST>/realms/genetics/broker/<alias>/endpoint`.
3. Put its client id/secret in `.env` and re-render the realm Secret (or set it in the admin
   console). The Keycloak login screen lists the new provider automatically.

Keycloak ships **built-in** connectors for Microsoft / Entra ID, GitHub, GitLab, Google,
Facebook, LinkedIn, plus **generic OpenID Connect** and **generic SAML 2.0** (and LDAP/Kerberos
for user federation). Any standards-compliant OIDC or SAML IdP — an institutional login,
ORCID, etc. — needs no custom image: just the generic OIDC/SAML connector. A custom extension
(like the bundled Apple one) is only needed for providers that deviate from the standards, as
Apple does. Whichever provider a user comes in through, their email still has to pass the same
domain/address allow-list.

## Known follow-up

The backend **bearer-token** path (`genetics-results-api/app/core/auth.py`,
`get_bearer_token_user`) validates **Google** Identity Tokens only. Programmatic/API access
for Apple-only identities would need a generic OIDC verifier (validate against Keycloak's
JWKS / issuer). Browser login does not use this path, so it does not block Apple sign-in.
