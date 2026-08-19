# Onboarding a customer to the MCP server over OAuth

How to give an external company's application access to the MCP server (`/mcp`) using a
standard OAuth 2.1 flow instead of a shared API key. Keycloak is the authorization server;
the mcp-server is the resource server.

Only applies to deployments where the Keycloak broker is enabled and `OAUTH_ISSUER` +
`OAUTH_RESOURCE_URL` are set (daly/genegenie — the path is inert on finngen). Background on
the broker itself is in [keycloak-apple-signin.md](keycloak-apple-signin.md); the
authentication paths are summarised in [project-spec.md](project-spec.md) § Authentication.

## The two gates

Onboarding is manual, and it is **two independent steps**. Doing only the first is the usual
failure mode: the customer completes the login flow, receives a perfectly valid token, and
still gets `401` from `/mcp`.

| Gate | What it controls | Where it lives |
|---|---|---|
| **Keycloak client** | whether the app can run the OAuth flow at all, and whether its tokens carry the right `aud` | the running `genetics` realm |
| **Email allow-list** | whose tokens the mcp-server accepts, by `email` claim | `ALLOWED_EMAILS` / `ALLOWED_EMAIL_DOMAINS` in the `bearer-auth-allowed` ConfigMap, plus the realm attributes used by the first-broker-login authenticator |

Registration is deliberately manual: Dynamic Client Registration is **not** enabled, so
arbitrary parties cannot register clients on the realm.

## What to collect from the customer first

- **Redirect URI(s)** — every environment their app will use (dev/staging/prod). Exact URLs;
  Keycloak matches them literally.
- **Email domain(s)** their users will sign in with, or the specific addresses.
- **Which identity provider** their users have: Google, Apple, or Microsoft — see
  [Identity providers](#identity-providers) below. This is the step most likely to need lead
  time, so ask early.
- **Whether their app holds a long-lived background session** rather than re-authenticating the
  user per interaction. If it does, it needs `offline_access` — see
  [Sessions longer than 10 hours](#sessions-longer-than-10-hours).

What you hand back: `client_id`, `client_secret`, `issuer`, `mcp_url`, scopes, and the flow
(authorization code + PKCE S256). The registration script prints this block for you.

## Step 1 — register the Keycloak client

```sh
cd genetics-results-suite
./scripts/keycloak-register-client.sh <clientId> <redirect-uri> [more-redirect-uris...]
```

This upserts a confidential client (auth-code + PKCE `S256`, direct-access grants and service
accounts off) **and** an `mcp-audience` protocol mapper that stamps
`aud = $OAUTH_RESOURCE_URL` into its access tokens. The mapper is not optional — the
mcp-server verifies `aud` and rejects the token without it. Because the audience is stamped
server-side, the client does not need to send an RFC 8707 `resource` parameter.

Keycloak generates the secret, so you never invent or store one; the script reads it back for
the handoff. It is idempotent:

```sh
./scripts/keycloak-register-client.sh <clientId> <uri>...           # re-run to change redirect URIs
./scripts/keycloak-register-client.sh --rotate-secret <clientId> <uri>...
./scripts/keycloak-register-client.sh --delete <clientId>           # e.g. remove a test client
```

Send the secret over a secure channel, not email or Slack DM.

> `scripts/keycloak-register-brainzzz.sh` is the original one-off for the brainzzz client and
> is kept only for that client's declarative template. **Do not copy it** for new customers —
> use the generic script above.

### Sessions longer than 10 hours

The realm keeps Keycloak's default **SSO Session Max of 10 hours**, and a normal refresh token
cannot outlive the SSO session — so a customer app that holds a background session (rather than
re-authenticating each user interaction) hits a hard 10h wall and has to send the user through a
browser login again. Customers report this as "the session dies after 10 hours".

The fix is the `offline_access` scope, which gets them an offline token instead: a refresh token
that survives logout and SSO-session expiry, bounded by the offline-session idle timeout (30 days
by default). The generic registration script does **not** assign it — it is a deliberate per
customer decision, because an offline token is a credential that outlives logout. Assign it as an
*optional* scope when a customer needs it:

```sh
NAMESPACE=genetics
POD="$(kubectl get pods -n $NAMESPACE -l app=keycloak -o jsonpath='{.items[0].metadata.name}')"
kc() { kubectl exec -n $NAMESPACE "$POD" -- /opt/keycloak/bin/kcadm.sh "$@"; }
# (authenticate kcadm first — see the Microsoft section for the credentials block)
CID="$(kc get clients -r genetics -q clientId=<clientId> \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')"
# GET /client-scopes takes no query filter, so pick the id out of the full list
SID="$(kc get client-scopes -r genetics \
  | python3 -c 'import json,sys; print(next(c["id"] for c in json.load(sys.stdin) if c["name"]=="offline_access"))')"
kc update "clients/$CID/optional-client-scopes/$SID" -r genetics -b '{}'   # endpoint ignores the body
```

For **brainzzz** this is already wired into `keycloak/brainzzz-client.json.template` and
`scripts/keycloak-register-brainzzz.sh`, so re-running that script is enough.

Then tell the customer to add `offline_access` to the `scope` they send on the authorize request —
without that, nothing changes. Keycloak does not error on a scope it will not grant; it silently
omits it, so an unchanged 10h expiry is the only symptom. Full mechanics, including the
`offline_access` realm role users need, are in
[keycloak-apple-signin.md](keycloak-apple-signin.md) § Sessions longer than 10 hours.

## Step 2 — allow-list their emails

Skip only if the customer's users are already on an allow-listed domain.

The allow-list is a single source of truth in terraform, fanned out by `deploy.sh` to
oauth2-proxy, the results-api and the mcp-server:

```hcl
# terraform/terraform.tfvars.<profile>
oauth_email_domain   = "broadinstitute.org,customer.com"   # comma-separated
oauth_allowed_emails = "someone@gmail.com"                 # individual addresses
```

```sh
cd terraform && terraform apply     # the values are exposed as outputs deploy.sh reads
cd .. && ./scripts/deploy.sh        # re-renders bearer-auth-allowed and restarts mcp-server
```

Then sync Keycloak's **own** copy of the allow-list, which the first-broker-login authenticator
reads from realm attributes. Without this, the customer's users are refused at sign-in with a
Forbidden page before a token is ever issued:

```sh
OAUTH_EMAIL_DOMAIN="broadinstitute.org,customer.com" \
OAUTH_ALLOWED_EMAILS="" \
  ./scripts/keycloak-bind-allowlist.sh
```

Prefer adding a **domain** over a list of addresses. A domain onboards their whole staff and
survives turnover; per-address entries drift and nobody prunes them.

## Step 3 — verify before handing over

```sh
./scripts/keycloak-get-token.sh          # browser auth-code + PKCE, prints an access token
```

Run this on a machine with a browser, then paste the token into MCP Inspector's bearer field
or use it with `curl` against `/mcp`. Generic MCP tooling expects Dynamic Client Registration
(off here) and therefore cannot self-register — this helper sidesteps that. A real customer app
uses its own pre-registered client and needs no DCR.

Sanity checks on a token you are unsure about (decode the payload at jwt.io or with
`python3 -c 'import base64,json,sys; ...'`):

- `iss` equals `OAUTH_ISSUER` exactly, including the `/auth` path segment
- `aud` contains `OAUTH_RESOURCE_URL` (`https://<host>/mcp`) — if absent, the `mcp-audience`
  mapper is missing on that client
- `email` is present and allow-listed — if absent, see the identity-provider notes below

## Identity providers

The customer's users authenticate to **Keycloak**, which brokers to a social/enterprise IdP.
Whatever the customer's app does internally, the token presented to `/mcp` must be
Keycloak-issued. The realm federates **Google** and **Apple** out of the box
(`keycloak/realm-genetics.json.template`, `keycloak/apple-idp.json.template`).

### Microsoft / Entra ID

Not configured today; it has to be added to the realm. Nothing in the mcp-server changes —
once Keycloak issues a token with an allow-listed `email` claim, the resource-server path
treats it identically.

**1. The customer registers an application in their Entra tenant** and gives you:

- Application (client) ID
- a client secret
- Directory (tenant) ID

They must register this redirect URI on their side (path-based issuer — note the `/auth`):

```
https://<host>/auth/realms/genetics/broker/microsoft/endpoint
```

**2. Add the identity provider to the running realm.** The realm import only runs on a fresh
DB, so editing the template does not affect a live realm — use `kcadm` in the Keycloak pod,
the same pattern as the other `keycloak-*.sh` scripts:

```sh
NAMESPACE=genetics
POD="$(kubectl get pods -n $NAMESPACE -l app=keycloak -o jsonpath='{.items[0].metadata.name}')"
AU="$(kubectl get secret keycloak-secrets -n $NAMESPACE -o jsonpath='{.data.admin-user}' | base64 -d)"
AP="$(kubectl get secret keycloak-secrets -n $NAMESPACE -o jsonpath='{.data.admin-password}' | base64 -d)"
kc() { kubectl exec -n $NAMESPACE "$POD" -- /opt/keycloak/bin/kcadm.sh "$@"; }
kc config credentials --server http://localhost:8080 --realm master --user "$AU" --password "$AP"

kc create identity-provider/instances -r genetics -b '{
  "alias": "microsoft",
  "displayName": "Microsoft",
  "providerId": "oidc",
  "enabled": true,
  "trustEmail": true,
  "config": {
    "clientId": "<application-client-id>",
    "clientSecret": "<client-secret>",
    "issuer": "https://login.microsoftonline.com/<tenant-id>/v2.0",
    "authorizationUrl": "https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/authorize",
    "tokenUrl": "https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token",
    "jwksUrl": "https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys",
    "useJwksUrl": "true",
    "validateSignature": "true",
    "clientAuthMethod": "client_secret_post",
    "defaultScope": "openid email profile",
    "syncMode": "IMPORT"
  }
}'
```

Use the **generic OIDC provider pinned to the tenant id**, as above, rather than Keycloak's
built-in `microsoft` social provider. The built-in one targets the multi-tenant `common`
endpoint; pinning the tenant means only that company's directory can produce a login here. (If
you do prefer the built-in provider, confirm its tenant handling in the admin console first —
`providerId: "microsoft"` with `common` endpoints accepts any Microsoft account, which is not
what you want for a per-customer onboarding.)

`trustEmail: true` matters: the realm has no SMTP configured, so without it users are stuck on
a "verify your email" step.

**3. Make sure an `email` claim actually arrives.** Entra only emits `email` when the user has
a mail attribute populated; otherwise the address lives in `preferred_username` / `upn`. The
first-broker-login allow-list authenticator reads the brokered email and denies an empty one,
so a tenant without mail attributes fails closed at sign-in. If that is the case, add an
attribute mapper:

```sh
kc create identity-provider/instances/microsoft/mappers -r genetics -b '{
  "name": "email-from-upn",
  "identityProviderAlias": "microsoft",
  "identityProviderMapper": "oidc-user-attribute-idp-mapper",
  "config": {"claim": "preferred_username", "user.attribute": "email", "syncMode": "INHERIT"}
}'
```

Only add this when the `email` claim is genuinely missing — otherwise it overwrites a good
email with the UPN, which is not always the same address.

**4. Bind the allow-list flow to the new IdP.** `keycloak-bind-allowlist.sh` only rebinds the
aliases it is told about, and defaults to `google apple`:

```sh
IDPS="google apple microsoft" \
OAUTH_EMAIL_DOMAIN="broadinstitute.org,customer.com" \
  ./scripts/keycloak-bind-allowlist.sh
```

Verify afterwards that the provider shows `firstBrokerLoginFlowAlias = first broker login
allowlist`. If it does not, brokered users bypass the allow-list check at account creation and
are only stopped later, at the mcp-server.

**5. Login page wording.** The `genetics` login theme renders social buttons generically, so a
Microsoft button appears with no theme change. But the helper text above them is hardcoded in
`keycloak/themes/genetics/login/messages/messages_en.properties`
("Sign in with Google with your broadinstitute.org account") and will read wrong. Update it
when a second organisation is onboarded.

**Known gap:** the Google Identity Token bearer path (`gcloud auth print-identity-token`) is
Google-only, so Microsoft-identity users have no equivalent CLI shortcut. They use the OAuth
flow via their app, or a per-user API token issued from the chat API.

### Testing Entra without a customer tenant

You need a real Entra directory to test against. A free `outlook.com`/`hotmail.com` address is
**not** one: personal Microsoft accounts live in Microsoft's shared *consumers* tenant
(`9188040d-6c67-4c5b-b112-36a304b66dad`), so against a tenant-pinned provider they cannot sign
in at all. Making them work means pointing the provider at `common`/`consumers`, which is
exactly the configuration we do not ship — you would be testing something we never deploy.

Create a free tenant instead. An Azure free account gives you one at no charge (the Entra ID
**Free** tier does not expire); signup needs a card for identity verification — a ~$1
authorization hold that is reversed — and a phone number. Users you create in it are
`someone@<yourtenant>.onmicrosoft.com`: real work/school accounts producing the same token
shape a customer's users produce. The Microsoft 365 Developer Program sandbox — the old free E5
route — is no longer open to the public and now requires a Visual Studio Professional or
Enterprise *standard* subscription (monthly subscriptions do not qualify).

Then follow the Microsoft steps above against your own tenant id, registering
`https://<host>/auth/realms/genetics/broker/microsoft/endpoint` as the redirect URI in your app
registration, and drive the flow with `scripts/keycloak-get-token.sh`.

Two things a `.onmicrosoft.com` tenant will surface — both are the gates from
[The two gates](#the-two-gates), and hitting them here is the point:

- **The `email` claim is probably missing.** A freshly created tenant user has no mail
  attribute (no Exchange licence), so the address arrives only in `preferred_username`/`upn`
  and the allow-list authenticator refuses the empty email at sign-in. This is the failure mode
  step 3 above exists for, so you get to validate the attribute mapper rather than discover the
  problem during a customer's rollout.
- **Allow-list the single address, not the domain.** Put the test user in
  `oauth_allowed_emails`, never `onmicrosoft.com` in `oauth_email_domain` — that domain is
  shared by every Entra tenant in existence, so allow-listing it would admit users from any
  tenant anywhere.

Nuance worth knowing before a customer hits it: invite a personal account as a **guest** (B2B)
rather than creating a native user, and its UPN becomes
`user_outlook.com#EXT#@<tenant>.onmicrosoft.com` — not an email address. The UPN→email fallback
mapper then writes that mangled string as the user's email and it fails the allow-list.
Customers with contractors or cross-tenant staff will run into this; native tenant users are
unaffected.

When you are done, remove the test address from `oauth_allowed_emails` (re-run `deploy.sh` and
`keycloak-bind-allowlist.sh`) and `--delete` any throwaway client. A permanently allow-listed
test identity is a standing hole in the only access control on this path.

## Caveats

- **Realm reimport wipes live-only objects.** Clients created by
  `keycloak-register-client.sh`, an IdP added with `kcadm`, and the allow-list flow binding all
  live only in the running realm — the realm import runs on a fresh DB only. After a Keycloak
  DB reset, re-run `keycloak-bind-allowlist.sh` and every `keycloak-register-client.sh`
  invocation. Keep a record of which clients exist.
- **No template parity for new IdPs.** Google and Apple have `.json.template` entries wired
  into `deploy.sh`; a Microsoft IdP added by hand does not, so a fresh-DB deploy comes back
  without it. Adding `keycloak/microsoft-idp.json.template` plus a `MICROSOFT_IDP_ENTRY`
  placeholder (mirroring `APPLE_IDP_ENTRY`) is the fix, and is worth doing before the second
  Microsoft customer.
- **The email allow-list is realm-wide, not per-client.** Allow-listing `customer.com` lets
  those users reach every allow-list-gated surface, including the web app through
  oauth2-proxy — not just `/mcp`. There is no per-client scoping today.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `401 {"error":"Invalid or missing bearer token"}` from `/mcp` | token `aud` missing → `mcp-audience` mapper absent on the client; or `iss` mismatch (the `/auth` path segment) |
| Token validates but still `401` | email not allow-listed in `bearer-auth-allowed`, or the mcp-server pods predate the ConfigMap change — check `kubectl rollout restart deployment/mcp-server -n genetics` |
| Forbidden page at sign-in, "not authorized to use this application" | realm allow-list attributes not synced — re-run `keycloak-bind-allowlist.sh` |
| Sign-in succeeds but Keycloak has no email for the user | IdP returns no `email` claim; add the attribute mapper (Microsoft section, step 3) |
| Customer's MCP client tries to self-register and fails | expected — DCR is off; they must use the pre-registered `client_id`/`client_secret` |
| Session dies after ~10 hours, user must log in again | `offline_access` not requested by the client, or not assigned to it — see [Sessions longer than 10 hours](#sessions-longer-than-10-hours) |
