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
                     │                 (FastAPI, port 4000) directly. /api/v1/ld is the exception:
                     │                 the BFF proxies it out to the external LD API (LD_API_URL),
                     │                 because the frontend CSP forbids off-origin fetches
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

`REGISTRY` is optional once terraform is configured — the scripts derive it from the selected
deployment's tfvars. If you do export it and it disagrees with `DEPLOY_ENV` (below), the scripts
stop rather than push across deployments; `unset REGISTRY` or set `REGISTRY_FORCE=1`.

## Deployment environments

This repo deploys the suite more than once (`daly`, `daly-staging`, `finngen`). Pick one with
`DEPLOY_ENV`, which selects `terraform/terraform.tfvars.<env>`, `terraform/<env>.tfbackend` and
`.env.<env>`:

```bash
DEPLOY_ENV=daly-staging ./scripts/build-all.sh
DEPLOY_ENV=daly-staging ./scripts/deploy.sh
```

`daly` and `daly-staging` are separate clusters in the *same* GCP project, so project-scoped
resource names carry `resource_suffix`. The setup below describes a single deployment; see
[docs/environments.md](docs/environments.md) for the multi-environment rules, the guardrails
against deploying across environments, and the staging bring-up runbook.

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
cp terraform.tfvars.example terraform.tfvars.<env>  # set project_id and other values
terraform init -backend-config=<env>.tfbackend
terraform apply -var-file=terraform.tfvars.<env>    # review the plan before confirming
```

(`deploy.sh` does all of this for you from `DEPLOY_ENV`. For a single-deployment instance you
may instead keep a bare `terraform.tfvars` and leave `DEPLOY_ENV` unset.)

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
hostnames only means listing them in the deployment's tfvars and redeploying:

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

Rolling out `chat-backend` can block for up to ~5 minutes: it waits for any in-flight chat
stream to finish rather than cutting it off mid-answer. See "chat-backend shutdown and stream
draining" in `docs/project-spec.md`.

## Services

| Service | Source Repo | Image | Port | Notes |
|---------|-----------|-------|------|-------|
| frontend | genetics-results-browser | genetics-results-browser | 3000 | React SPA via nginx |
| bff | genetics-results-browser (`bff/Dockerfile`) | genetics-results-browser-bff | 5000 | Backend-for-frontend: assembles the browser's `POST /v1/results` from the results-api fan-out, proxies the external LD API as `GET /api/v1/ld` (`LD_API_URL`), passes other `/api/*` calls through |
| auth-gateway | — | nginx:1.27-alpine | 8080 | Auth gateway (oauth2-proxy + routing) |
| oauth2-proxy | — | oauth2-proxy:v7.14.3 | 4180 | Browser login — OIDC against Keycloak, or Google directly where the broker is disabled |
| keycloak | keycloak/ (local build) | keycloak | 8080 | Identity broker (Google + Apple), served at `<domain>/auth`; only when `ENABLE_KEYCLOAK=true` |
| keycloak-postgres | — | postgres:16-alpine | 5432 | Keycloak database (PVC + daily `pg_dump` CronJob to GCS) |
| results-api | genetics-results-api | genetics-results-api | 4000 | FastAPI |
| chat-backend | genetics-mcp-server | genetics-mcp-server | 8000 | FastAPI, LLM chat with MCP tools |
| mcp-server | genetics-mcp-server | genetics-mcp-server | 8080 | Standalone MCP server (streamable HTTP) |
| db-api | genetics-results-db | genetics-results-db | 8080 | BigQuery query proxy (internal only) |
| rag-service | genetics-rag-service | genetics-rag-service | 8000 | RAG document retrieval (internal only; skipped unless `ENABLE_RAG=true`) |
| monitor | — (scripts/monitor/) | monitor | — | CronJob (daily, 08:00 UTC): health checks, BQ coverage, log alerts → Slack |

The chat-backend and mcp-server share the same Docker image but run different commands; the frontend
and bff share the same source repo but build different Dockerfiles.

## Authentication

Authentication is handled by [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) behind an nginx auth-gateway. The auth-gateway uses nginx `auth_request` to validate each request against oauth2-proxy before proxying to backend services. Authenticated user email is passed via the `X-Goog-Authenticated-User-Email` header.

Where the Keycloak identity broker is enabled, oauth2-proxy uses its `oidc` provider against Keycloak
(which in turn federates Google and Apple); otherwise it talks to Google directly. See
[docs/keycloak-apple-signin.md](docs/keycloak-apple-signin.md).

- **Frontend, Chat backend**: Protected by oauth2-proxy. Unauthenticated users are redirected to the sign-in page (Google directly, or the Keycloak provider chooser).
- **Results API**: Protected by oauth2-proxy for browser access, which routes through the BFF. Also supports `Authorization: Bearer` tokens for programmatic access — requests with a bearer token bypass both oauth2-proxy and the BFF and are validated directly by the backend (user-created tokens, Google Identity Tokens or internal shared secret).
- **MCP server**: Not behind oauth2-proxy. Uses bearer token auth via `MCP_API_KEY`, user-created tokens, Google Identity Tokens, or — where the broker is enabled — Keycloak OAuth 2.1 access tokens.
- **DB API**: Internal only (NetworkPolicy restricts access to chat-backend and mcp-server), and additionally requires the `INTERNAL_API_SECRET` bearer token on every endpoint except `/health` — the NetworkPolicy alone is not a sufficient boundary, since mcp-server is allowed through it and is itself reachable from outside.
- **Internal service calls**: The chat-backend authenticates to results-api using a shared secret (`INTERNAL_API_SECRET`), auto-generated by `create-secrets.sh`.

### Programmatic API access with Google Identity Token

```bash
TOKEN=$(gcloud auth print-identity-token)
curl -H "Authorization: Bearer $TOKEN" https://your-domain.example.com/api/v1/...
```

Requires a Google account on the allow-list (`ALLOWED_EMAIL_DOMAINS` / `ALLOWED_EMAILS` from the
`bearer-auth-allowed` ConfigMap — see [Managing access](#managing-access) below). The token's `aud`
must also match `GOOGLE_TOKEN_AUDIENCE`, which defaults to the gcloud CLI's client id — i.e. exactly
what `gcloud auth print-identity-token` mints — so an id_token issued for some other application
cannot be replayed here. Google Identity Tokens expire after 1 hour.

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

### Managing access

The allow-list has a single source of truth in terraform, so both the browser and bearer-token paths
stay in step — `deploy.sh` renders it into the oauth2-proxy `--authenticated-emails-file` ConfigMap
and into the `bearer-auth-allowed` ConfigMap that results-api and mcp-server consume via `envFrom`:

- `oauth_email_domain` — comma-separated domains (e.g. `broadinstitute.org,finngen.fi`) whose Google
  accounts have access by default.
- `oauth_allowed_emails` — comma-separated individual addresses allowed in addition to those domains
  (e.g. Apple users on `me.com`/`icloud.com`/`privaterelay.appleid.com`).

Set them in the deployment's tfvars and re-run `./scripts/deploy.sh`. Where the Keycloak broker is
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

All services output structured JSON to stdout, automatically captured by GKE's fluentbit agent and sent to Cloud Logging. With `enable_log_sinks = true`, `terraform/logging.tf` also creates two Cloud Logging → BigQuery sinks: the results API's usage logs (stripped of variant, gene, phenotype etc. information) into `genetics_api_logs`, and chat-backend container logs at severity ≥ INFO into `genetics_chat_logs`. `scripts/chat_usage_stats.sh` reports chat usage counts from the latter.

## Security

- Network policies enforce that db-api and rag-service are only reachable from chat-backend and mcp-server
- Application containers run with `allowPrivilegeEscalation: false`, all capabilities dropped and the `RuntimeDefault` seccomp profile; db-api and bff additionally run as non-root
- Workload Identity provides read-only GCP access (BigQuery + GCS) without key files
- HTTPS enforced via FrontendConfig redirect
- Google-managed SSL certificates
