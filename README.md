# Genetics Results Suite - Kubernetes Deployment

Terraform/Kubernetes deployment for the genetics results suite.

## Architecture

```
Internet → GKE Ingress (HTTPS, Google-managed certs)
           └── /*  → auth-gateway (nginx, port 8080)
                     ├── /oauth2/*  → oauth2-proxy (OIDC/Google login, port 4180)
                     ├── /auth/*    → keycloak      (login UI + OIDC, port 8080) — only where the
                     │                 identity broker is enabled (see docs/keycloak-apple-signin.md)
                     ├── /api/*     → bff           (port 5000) → results-api — browser oauth2 traffic;
                     │                 bearer-token requests bypass the BFF and hit results-api
                     │                 (FastAPI, port 4000) directly
                     ├── /chat/v1/* → chat-backend  (FastAPI, port 8000); also exact /status
                     ├── /mcp       → mcp-server    (MCP streamable HTTP, port 8080) — bearer token auth
                     └── /*         → frontend      (nginx, port 3000)

Internal only (ClusterIP + NetworkPolicy):
  ├── db-api            (BigQuery proxy, port 8080) — only from chat-backend + mcp-server
  ├── rag-service       (RAG retrieval, port 8000)  — only from chat-backend + mcp-server
  └── keycloak-postgres (Keycloak DB, port 5432)    — only from keycloak
```

## Prerequisites

- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.14
- [gcloud CLI](https://cloud.google.com/sdk/docs/install) authenticated to your GCP project
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Docker](https://docs.docker.com/get-docker/)

Set these environment variables (used in examples below):
```bash
export GCP_PROJECT="your-gcp-project-id"
export GCP_REGION="europe-west1"
export REGISTRY="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT}/genetics-results"
```

## Setup

### 0. Create a bucket for terraform and a terraform service account

Create a bucket:

```
gcloud storage buckets create gs://genetics-results-terraform \
--project=$GCP_PROJECT \
--location=$GCP_REGION \
--uniform-bucket-level-access
```

Or set a different bucket name in [terraform/main.tf](terraform/main.tf)

Create a service account:

```
gcloud iam service-accounts create terraform \
  --display-name="Terraform"
```

Add editor role to it (or granular ones) and give serviceAccountTokenCreator role:

```
gcloud projects add-iam-policy-binding $GCP_PROJECT \
--member="serviceAccount:terraform@$GCP_PROJECT.iam.gserviceaccount.com" \
--role="roles/editor"

# change you@your.org
gcloud iam service-accounts add-iam-policy-binding \
terraform@$GCP_PROJECT.iam.gserviceaccount.com \
--member="user:you@your.org" \
--role="roles/iam.serviceAccountTokenCreator"

# When manage_iam=true, terraform creates a GSA, grants it project-level
# roles, and binds the Workload Identity KSA→GSA relationship. These need
# resourcemanager.projects.setIamPolicy and iam.serviceAccounts.setIamPolicy,
# neither of which is in roles/editor. Grant them explicitly:
gcloud projects add-iam-policy-binding $GCP_PROJECT \
--member="serviceAccount:terraform@$GCP_PROJECT.iam.gserviceaccount.com" \
--role="roles/resourcemanager.projectIamAdmin"

gcloud projects add-iam-policy-binding $GCP_PROJECT \
--member="serviceAccount:terraform@$GCP_PROJECT.iam.gserviceaccount.com" \
--role="roles/iam.serviceAccountAdmin"
```

Impersonate the service account:

```
gcloud auth application-default login --impersonate-service-account=terraform@$GCP_PROJECT.iam.gserviceaccount.com
```

### 1. Initial infrastructure setup

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars  # set project_id and other values
terraform init
terraform apply                               # review the plan before confirming
```

If `manage_iam` is `false` in your tfvars, grant the node pool service account access to Artifact Registry so it can pull images:

```bash
NODE_SA=$(gcloud container node-pools describe $(terraform output -raw cluster_name)-pool \
  --cluster=$(terraform output -raw cluster_name) \
  --zone=$(terraform output -raw zone) \
  --project=$(terraform output -raw project_id) \
  --format='value(config.serviceAccount)')
gcloud projects add-iam-policy-binding $(terraform output -raw project_id) \
  --member="serviceAccount:${NODE_SA}" \
  --role="roles/artifactregistry.reader"
```

Configure kubectl to connect to the new cluster:

```bash
eval "$(terraform output -raw kubectl_command)"
```

Install kubectl credential plugin - e.g.:

```bash
sudo apt-get update && sudo apt-get install -y apt-transport-https ca-certificates gnupg curl
curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list
sudo apt-get update && sudo apt-get install -y google-cloud-cli-gke-gcloud-auth-plugin
```

Go back to root dir:

```bash
cd ..
```

### 2. Set up OAuth credentials

Create Google OAuth credentials for oauth2-proxy:

1. Go to [APIs & Services > Credentials](https://console.cloud.google.com/apis/credentials?project=$GCP_PROJECT)
2. Create an **OAuth client ID** (Web application)
3. Add authorized JavaScript origin(s): `https://your-domain.example.com`
4. Add authorized redirect URI(s): `https://your-domain.example.com/oauth2/callback`

> With the Keycloak identity broker enabled, oauth2-proxy talks to Keycloak instead and the Google
> client belongs to Keycloak — its redirect URI is
> `https://your-domain.example.com/auth/realms/genetics/broker/google/endpoint`. See
> [docs/keycloak-apple-signin.md](docs/keycloak-apple-signin.md).

### 3. Create secrets

`create-secrets.sh` creates `genetics-secrets` (chat-backend API keys) and
`oauth2-proxy-secrets` (the OAuth client creds + session cookie secret), plus `keycloak-secrets`
(DB + bootstrap admin passwords, generated) where the identity broker is enabled. Set the relevant
env vars, then run it once:
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."           # optional
export TAVILY_API_KEY="tvly-..."         # optional
export PERPLEXITY_API_KEY="pplx-..."     # optional
export COHERE_API_KEY="..."              # optional, for rag-service embeddings (required when ENABLE_RAG=true)
export MCP_API_KEY="$(openssl rand -hex 32)"  # optional for bearer token MCP and API access, comma-separated for multiple keys
export ADMIN_USERS="a@example.com,b@example.com"  # optional, emails allowed on the chat admin page
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."  # optional, for the monitor CronJob
export EXTERNAL_MCP_SERVERS="https://..."  # optional, external MCP servers proxied by chat-backend
# INTERNAL_API_SECRET for results API is auto-generated if not set

# oauth2-proxy credentials (YOUR_CLIENT_ID/SECRET from the OAuth client created in step 2).
# only needed on first install — afterwards they're reused from the cluster if unset.
export OAUTH2_PROXY_CLIENT_ID='YOUR_CLIENT_ID'
export OAUTH2_PROXY_CLIENT_SECRET='YOUR_CLIENT_SECRET'
# OAUTH2_PROXY_COOKIE_SECRET is generated on first install and reused thereafter (never rotated).

./scripts/create-secrets.sh
```

### 4. Build and push Docker images

Authenticate docker:

```bash
gcloud auth configure-docker ${GCP_REGION}-docker.pkg.dev
```

Build and push images:

```bash
./scripts/build-all.sh
```

### 5. Deploy

```bash
./scripts/deploy.sh
```

This applies any terraform changes, configures kubectl, and deploys all k8s manifests. Use it for both the initial deployment and subsequent updates.

> **Note:** The k8s YAMLs use variable placeholders (`${REGISTRY}`, `${GCP_PROJECT}`, `${DOMAIN}`, etc.) — `deploy.sh` substitutes these automatically from terraform output. Do not `kubectl apply -f` the YAMLs directly; always use `deploy.sh` or `rollout.sh`.

### Multiple domains

There is no checked-in `ingress.yaml` or `managed-certs.yaml` — `deploy.sh` generates both from the
terraform `domains` list, emitting one `ManagedCertificate` (`managed-cert`) covering every domain as
a SAN and one Ingress host rule per domain, all pointing at `auth-gateway`. So serving several
hostnames only means listing them in `terraform.tfvars` and redeploying:

```hcl
domains = ["primary.example.com", "secondary.example.com"]
```

```bash
./scripts/deploy.sh
```

The first entry is the primary domain (terraform output `domain`). The oauth2-proxy callback, the
Keycloak `/auth` path and the MCP resource URL are built on the *canonical* web host — the primary
domain, or `redirect_to_host` when a legacy-hostname redirect is configured (see below). A managed
cert only goes `Active` once
every domain's DNS A record already resolves to the ingress static IP, so create the DNS records
first and expect 15–60 min of provisioning:

```bash
kubectl describe managedcertificate managed-cert -n genetics
```

To 301-redirect an old hostname to a new one (keeping the old one in `domains` so its cert stays
valid), set `redirect_from_host`/`redirect_to_host` — see [docs/genegenie-migration.md](docs/genegenie-migration.md).

## Updating Services

```bash
# build new images
./scripts/build-all.sh

# roll out one service
./scripts/rollout.sh results-api 20260305.abc1234
```

### Deploying the trusted-proxy marker

**Roll out `bff` before `results-api`, and never both in the same `deploy.sh`.** results-api
only honours `X-Goog-Authenticated-User-Email` from a caller that also presents
`INTERNAL_API_SECRET`, and it is the BFF that attaches that bearer.

```bash
./scripts/build.sh bff && ./scripts/rollout.sh bff            # 1. must land first
./scripts/build.sh results-api && ./scripts/rollout.sh results-api  # 2. only once bff is Running
```

- **bff new, results-api old** — safe, and safe to sit in indefinitely: the old API accepts the
  bearer as an internal service call, and the usage log still names the real user.
- **results-api new, bff old** — **every browser request 401s.** This is the order to avoid.
- Rolling back reverses it: results-api first, then bff.

`deploy.sh` restarts everything at once and gives no ordering, so use `rollout.sh` for the bff
step first. Details and the full state table are in `docs/project-spec.md`, "Ordered rollout:
the trusted-proxy marker".

The same rule reaches chat-backend — auth-gateway first:

```bash
./scripts/deploy.sh                                                    # 1. gateway ConfigMap
./scripts/build.sh chat-backend && ./scripts/rollout.sh chat-backend   # 2. after
```

- **auth-gateway new, chat-backend old** — safe, and safe to sit in: the gateway carries the
  marker on its own `X-Internal-Auth` header, which the old chat-backend does not recognise, so
  it behaves exactly as it did before. The fix simply is not in force yet.
- **chat-backend new, auth-gateway old** — every browser chat request 401s. This is the order to
  avoid.
- Rolling back reverses it: chat-backend first, then auth-gateway; the state in between is the
  safe one, so a half-finished rollback costs nothing.

auth-gateway is ConfigMap-driven, so it needs `deploy.sh`; `rollout.sh` only swaps images. Full
state table in `docs/project-spec.md`, "Ordered rollout: the trusted-proxy marker (auth-gateway
before chat-backend)".

Rolling out `chat-backend` can block for up to ~5 minutes: it waits for any in-flight chat
stream to finish rather than cutting it off mid-answer. See "chat-backend shutdown and stream
draining" in `docs/project-spec.md`.

`build-all.sh` also builds the local `monitor`, `keycloak` and `sandbox` contexts.
`./scripts/build.sh sandbox` builds the sandbox alone. The sandbox image (distroless, no
shell, no pip, uid 65532) runs model-authored Python and pip-installs the genetics SDK
from genetics-mcp-server at build time, pruned to the SDK's import closure; **it is skipped
by `build-all.sh`, with a loud message, while that repo has no
`src/genetics_mcp_server/sdk/`** — which is the case on `master` today. Both build scripts
first run `./scripts/gen-sandbox-docs.py`, which regenerates the on-demand schema markdown
(`sandbox/schema/`, one file per BigQuery view in `configs/datasets.yaml`) and the SDK
signature stubs (`sandbox/stubs/`) the image carries at `/genetics/schema` and
`/genetics/sdk`, and then `./scripts/test-sandbox-docs.py`, which checks the committed
copies are current, that every view and column reaches a file, and that the stubs cover
exactly the SDK's exported surface. `build.sh sandbox` fails on a non-zero exit; `build-all.sh`
folds it into the same skip branch as the generator. Exit 1 = a property broke, 2 = the
harness could not run (no staged SDK source).
The build still fails while `sandbox/schema/` and `sandbox/stubs/` hold placeholders. There
is no sandbox Deployment yet. See
[docs/code-execution-security.md](docs/code-execution-security.md).

## Services

| Service | Source Repo | Image | Port | Notes |
|---------|-----------|-------|------|-------|
| frontend | genetics-results-browser | genetics-results-browser | 3000 | React SPA via nginx |
| bff | genetics-results-browser (`bff/Dockerfile`) | genetics-results-browser-bff | 5000 | Backend-for-frontend: assembles the browser's `POST /v1/results` from the results-api fan-out, passes other `/api/*` calls through |
| auth-gateway | — | nginx:1.27-alpine | 8080 | Auth gateway (oauth2-proxy + routing) |
| oauth2-proxy | — | oauth2-proxy:v7.14.3 | 4180 | Browser login — OIDC against Keycloak, or Google directly where the broker is disabled |
| keycloak | keycloak/ (local build) | keycloak | 8080 | Identity broker (Google + Apple), served at `<domain>/auth`; only when `ENABLE_KEYCLOAK=true` |
| keycloak-postgres | — | postgres:16-alpine | 5432 | Keycloak database (PVC + daily `pg_dump` CronJob to GCS) |
| results-api | genetics-results-api | genetics-results-api | 4000 | FastAPI |
| chat-backend | genetics-mcp-server | genetics-mcp-server | 8000 | FastAPI, LLM chat with MCP tools |
| mcp-server | genetics-mcp-server | genetics-mcp-server | 8080 | Standalone MCP server (streamable HTTP) |
| db-api | genetics-results-db | genetics-results-db | 8080 | BigQuery query proxy (internal only) |
| rag-service | genetics-rag-service | genetics-rag-service | 8000 | RAG document retrieval (internal only; skipped unless `ENABLE_RAG=true`) |
| monitor | — (scripts/monitor/) | monitor | — | CronJob (every 8h): health checks, BQ coverage, log alerts → Slack |

The chat-backend and mcp-server share the same Docker image but run different commands; the frontend
and bff share the same source repo but build different Dockerfiles.

## Authentication

Authentication is handled by [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) behind an nginx auth-gateway. The auth-gateway uses nginx `auth_request` to validate each request against oauth2-proxy before proxying to backend services. Authenticated user email is passed via the `X-Goog-Authenticated-User-Email` header. That header is not itself a credential — results-api honours it only when the request also carries `Authorization: Bearer $INTERNAL_API_SECRET`, proving it came from an in-cluster proxy, and then still checks the address against the shared email allow-list; without the bearer the header is ignored and the request is unauthenticated. The BFF attaches that bearer on every upstream call, including the generic `/api` passthrough that forwards the identity header, so the two always arrive together on the browser path. **Shipping the two halves of that change in the wrong order locks all browser users out** — see [Deploying the trusted-proxy marker](#deploying-the-trusted-proxy-marker) under Updating Services.

Where the Keycloak identity broker is enabled, oauth2-proxy uses its `oidc` provider against Keycloak
(which in turn federates Google and Apple); otherwise it talks to Google directly. See
[docs/keycloak-apple-signin.md](docs/keycloak-apple-signin.md).

- **Frontend, Chat backend**: Protected by oauth2-proxy. Unauthenticated users are redirected to the sign-in page (Google directly, or the Keycloak provider chooser).
- **Results API**: Protected by oauth2-proxy for browser access, which routes through the BFF. Also supports `Authorization: Bearer` tokens for programmatic access — requests with a bearer token bypass both oauth2-proxy and the BFF and are validated directly by the backend (user-created tokens — the recommended path — Google Identity Tokens (deprecated) or internal shared secret).
- **MCP server**: Not behind oauth2-proxy. Uses bearer token auth via `MCP_API_KEY`, user-created tokens (recommended), Google Identity Tokens (deprecated), or — where the broker is enabled — Keycloak OAuth 2.1 access tokens.
- **DB API**: Internal only (NetworkPolicy restricts access to chat-backend and mcp-server), and additionally requires the `INTERNAL_API_SECRET` bearer token on every endpoint except `/health` — the NetworkPolicy alone is not a sufficient boundary, since mcp-server is allowed through it and is itself reachable from outside.
- **Internal service calls**: The chat-backend authenticates to results-api using a shared secret (`INTERNAL_API_SECRET`), auto-generated by `create-secrets.sh`.
- **Code-execution sandbox**: never holds `INTERNAL_API_SECRET`. chat-backend mints a short-lived (5 minute), audience-bound HS256 token per script execution, signed with a separate key (`SANDBOX_TOKEN_SIGNING_KEY`, also auto-generated by `create-secrets.sh`); db-api and results-api verify it, fail closed when the key is missing, and refuse to start at all when `SANDBOX_ENABLED=true` and either secret is unset. See `docs/code-execution-security.md` §4.

### Programmatic API access (per-user API key)

Create a key in the browser: the user menu → **MCP and API keys** → *Create key*. The value is shown
once; the same dialog lists and revokes your keys. A key expires after 90 days without use and every
use extends it by another 90. The same key works for both `/api` and `/mcp`.

```bash
curl -H "Authorization: Bearer <TOKEN>" https://your-domain.example.com/api/v1/...
```

This is the recommended path for scripts and pipelines: the key is issued by this deployment, is
revocable per user, and is attributable in the token store — none of which is true of a Google
Identity Token.

**A key can only be created in the browser.** `POST /chat/v1/tokens` requires an oauth2-proxy
browser session, so no bearer token — not an existing API key, not a Google Identity Token — can
mint one. A CI job or service account therefore cannot self-serve: a human signs in once and
creates the key for it. Once created the key works headlessly and does not expire while it is in
use (every use extends it by 90 days), so this is a one-time step, but plan for it before
migrating a fully headless caller off the deprecated Identity Token path.

#### Google Identity Token (deprecated)

```bash
TOKEN=$(gcloud auth print-identity-token)   # deprecated — prefer a per-user API key
curl -H "Authorization: Bearer $TOKEN" https://your-domain.example.com/api/v1/...
```

Still accepted, and it will not be switched off without notice, but do not build anything new on it
and migrate existing callers to a per-user API key. Reasons: the token expires after 1 hour; this
deployment does not issue it and therefore cannot revoke it for one person; and authorization on it
rests entirely on the email allow-list (`ALLOWED_EMAIL_DOMAINS` / `ALLOWED_EMAILS` from the
`bearer-auth-allowed` ConfigMap — see [Managing access](#managing-access) below). `GOOGLE_TOKEN_AUDIENCE`
does **not** narrow that: it defaults to the gcloud CLI's *public* OAuth client id, which anyone's
`gcloud auth print-identity-token` mints, so it buys cross-*OAuth-client* replay protection and
nothing more — it rejects a token minted for a different client id (ADC's `764086051850-…`, a
project-owned client), but not one the same user handed to another service that documents this same
`gcloud auth print-identity-token` flow, since that token carries the identical `aud`. See
"Programmatic credentials: why the per-user API key, not the Google id_token" in
[docs/project-spec.md](docs/project-spec.md).

### MCP server access (e.g. Claude Desktop or Claude Code)

```json
{
  "mcpServers": {
    "genetics": {
      "type": "streamable-http",
      "url": "https://your-domain.example.com/mcp",
      "headers": {
        "Authorization": "Bearer <MCP_API_KEY>"
      }
    }
  }
}
```

`MCP_API_KEY` is the deployment-wide *shared* secret: one value for everyone, not attributable to a
person and not revocable for one. Prefer your own key from
[Programmatic API access](#programmatic-api-access-per-user-api-key) above — the same per-user key
works here, in exactly this header, and covers both `/api` and `/mcp`.

### Managing access

The allow-list has a single source of truth in terraform, so both the browser and bearer-token paths
stay in step — `deploy.sh` renders it into the oauth2-proxy `--authenticated-emails-file` ConfigMap
and into the `bearer-auth-allowed` ConfigMap that results-api, mcp-server and chat-backend consume
via `envFrom`:

- `oauth_email_domain` — comma-separated domains (e.g. `broadinstitute.org,finngen.fi`) whose Google
  accounts have access by default.
- `oauth_allowed_emails` — comma-separated individual addresses allowed in addition to those domains
  (e.g. Apple users on `me.com`/`icloud.com`/`privaterelay.appleid.com`).

Set them in `terraform.tfvars` and re-run `./scripts/deploy.sh`. Where the Keycloak broker is
enabled the same two values are also enforced at first-broker-login, so a non-allowlisted federated
user never gets an account — re-run `scripts/keycloak-bind-allowlist.sh` after changing them (see
[docs/keycloak-apple-signin.md](docs/keycloak-apple-signin.md)).

## Data Storage

- **Chat backend**: 10Gi PVC (`chat-data`) for SQLite databases and file attachments. Data persists across deployments, and is snapshotted daily (see `terraform/backups.tf`).
- **RAG service**: 50Gi PV/PVC (`rag-stores`) for document embedding stores. Uses a pre-provisioned GCE persistent disk.
- **Monitor**: 1Gi PVC (`monitor-data`) for the alert-deduplication SQLite database.
- **Keycloak**: 5Gi PVC (`keycloak-postgres-data`) for the Keycloak Postgres database, dumped daily to GCS.
- **All other services**: Stateless, no local storage.

## Logging

All services output structured JSON to stdout, automatically captured by GKE's fluentbit agent and sent to Cloud Logging. With `enable_log_sinks = true`, `terraform/logging.tf` also creates two Cloud Logging → BigQuery sinks: the `endpoint_access` usage logs of both results-api and db-api (stripped of variant, gene, phenotype etc. information), scoped to `k8s_container` resources in the `genetics` namespace, into `genetics_api_logs` — both services share the table `genetics_api_logs.stdout`, see `docs/project-spec.md` → Log sinks — and chat-backend container logs at severity ≥ INFO into `genetics_chat_logs`. `scripts/chat_usage_stats.sh` reports chat usage counts from the latter.

## Security

- Network policies source-scope **every** service: db-api and rag-service only from chat-backend and mcp-server; results-api (4000) from auth-gateway, bff, chat-backend and mcp-server; bff (5000), frontend (3000) and mcp-server (8080) only from auth-gateway; chat-backend (8000) from auth-gateway, results-api and mcp-server. The monitor CronJob is admitted separately and additively by `monitor-policy.yaml`. auth-gateway (8080) is the only service reached from outside and the only one using an `ipBlock` — Google's LB/health-check ranges `35.191.0.0/16` and `130.211.0.0/22`; no node CIDR, because it is fronted by a NEG so the load balancer talks to pod IPs directly. The source nginx sees is always the GFE's own address in `35.191.0.0/16`, never the client's (that survives only in `X-Forwarded-For`), so client IPs cannot be filtered at this layer. See `docs/project-spec.md` → Security.
- Application containers run with `allowPrivilegeEscalation: false`, all capabilities dropped and the `RuntimeDefault` seccomp profile; db-api and bff additionally run as non-root
- Workload Identity provides read-only GCP access (BigQuery + GCS) without key files
- HTTPS enforced via FrontendConfig redirect
- Google-managed SSL certificates
