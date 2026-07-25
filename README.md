# Genetics Results Suite - Kubernetes Deployment

Terraform/Kubernetes deployment for the genetics results suite.

## Architecture

```
Internet → GKE Ingress (HTTPS, Google-managed certs)
           └── /*  → auth-gateway (nginx, port 8080)
                     ├── /oauth2/*  → oauth2-proxy (Google OAuth login, port 4180)
                     ├── /api/*     → results-api  (FastAPI, port 4000) — oauth2 or bearer token
                     ├── /chat/*    → chat-backend  (FastAPI, port 8000)
                     ├── /mcp/*     → mcp-server    (MCP streamable HTTP, port 8080) — bearer token auth
                     └── /*         → frontend      (nginx, port 3000)

Internal only (ClusterIP + NetworkPolicy):
  ├── db-api      (BigQuery proxy, port 8080) — only accessible from chat-backend
  └── rag-service (RAG retrieval, port 8000)  — only accessible from chat-backend + mcp-server
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

### 3. Create secrets

`create-secrets.sh` creates both `genetics-secrets` (chat-backend API keys) and
`oauth2-proxy-secrets` (the OAuth client creds + session cookie secret). Set the relevant
env vars, then run it once:
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."           # optional
export TAVILY_API_KEY="tvly-..."         # optional
export PERPLEXITY_API_KEY="pplx-..."     # optional
export COHERE_API_KEY="..."              # optional, for rag-service embeddings (required when ENABLE_RAG=true)
export MCP_API_KEY="$(openssl rand -hex 32)"  # optional for bearer token MCP and API access, comma-separated for multiple keys
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

The default configuration serves a single domain (the first entry in the terraform `domains` variable). To serve multiple domains, add a host rule per domain to `k8s/ingress/ingress.yaml` and a `ManagedCertificate` per domain to `k8s/ingress/managed-certs.yaml`, then list all cert names in the ingress `networking.gke.io/managed-certificates` annotation. For example, with two domains:

```yaml
# managed-certs.yaml — one ManagedCertificate per domain
apiVersion: networking.gke.io/v1
kind: ManagedCertificate
metadata:
  name: cert-primary
  namespace: genetics
spec:
  domains:
    - primary.example.com
---
apiVersion: networking.gke.io/v1
kind: ManagedCertificate
metadata:
  name: cert-secondary
  namespace: genetics
spec:
  domains:
    - secondary.example.com
```

```yaml
# ingress.yaml — list both certs and add a host rule for each domain
metadata:
  annotations:
    networking.gke.io/managed-certificates: "cert-primary,cert-secondary"
spec:
  rules:
  - host: primary.example.com
    http: ...
  - host: secondary.example.com
    http: ...
```

## Updating Services

```bash
# build new images
./scripts/build-all.sh

# roll out one service
./scripts/rollout.sh results-api 20260305.abc1234
```

## Services

| Service | Source Repo | Image | Port | Notes |
|---------|-----------|-------|------|-------|
| frontend | genetics-results-browser | genetics-results-browser | 3000 | React SPA via nginx |
| auth-gateway | — | nginx:1.27-alpine | 8080 | Auth gateway (oauth2-proxy + routing) |
| oauth2-proxy | — | oauth2-proxy:v7.14.3 | 4180 | Google OAuth login |
| results-api | genetics-results-api | genetics-results-api | 4000 | FastAPI |
| chat-backend | genetics-mcp-server | genetics-mcp-server | 8000 | FastAPI, LLM chat with MCP tools |
| mcp-server | genetics-mcp-server | genetics-mcp-server | 8080 | Standalone MCP server (streamable HTTP) |
| db-api | genetics-results-db | genetics-results-db | 8080 | BigQuery query proxy (internal only) |
| rag-service | genetics-rag-service | genetics-rag-service | 8000 | RAG document retrieval (internal only) |

The chat-backend and mcp-server share the same Docker image but run different commands.

## Authentication

Authentication is handled by [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) behind an nginx auth-gateway. The auth-gateway uses nginx `auth_request` to validate each request against oauth2-proxy before proxying to backend services. Authenticated user email is passed via the `X-Goog-Authenticated-User-Email` header.

- **Frontend, Chat backend**: Protected by oauth2-proxy. Unauthenticated users are redirected to Google sign-in.
- **Results API**: Protected by oauth2-proxy for browser access. Also supports `Authorization: Bearer` tokens for programmatic access — requests with a bearer token bypass oauth2-proxy and are validated directly by the backend (user-created tokens, Google Identity Tokens or internal shared secret).
- **MCP server**: Not behind oauth2-proxy. Uses bearer token auth via `MCP_API_KEY` or user-created tokens.
- **DB API**: Internal only (NetworkPolicy restricts access to chat-backend and mcp-server), and additionally requires the `INTERNAL_API_SECRET` bearer token on every endpoint except `/health` — the NetworkPolicy alone is not a sufficient boundary, since mcp-server is allowed through it and is itself reachable from outside.
- **Internal service calls**: The chat-backend authenticates to results-api using a shared secret (`INTERNAL_API_SECRET`), auto-generated by `create-secrets.sh`.

### Programmatic API access with Google Identity Token

```bash
TOKEN=$(gcloud auth print-identity-token)
curl -H "Authorization: Bearer $TOKEN" https://your-domain.example.com/api/v1/...
```

Requires a Google account in the allowed email domain (configured via `--email-domain` in oauth2-proxy) or an email listed in `ALLOWED_EMAILS` env var for results API. Google Identity Tokens expire after 1 hour.

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

- Google accounts in the allowed email domain have access by default (configured via `--email-domain` in oauth2-proxy)
- Additional email addresses can be added to `k8s/secrets/oauth2-allowed-emails.yaml` and applied with `kubectl apply -f k8s/secrets/`
- For bearer token access, additional domains/emails can be set via `ALLOWED_EMAIL_DOMAINS` and `ALLOWED_EMAILS` env vars on results-api

## Data Storage

- **Chat backend**: 10Gi PVC (`chat-data`) for SQLite databases and file attachments. Data persists across deployments.
- **RAG service**: 50Gi PV/PVC (`rag-stores`) for document embedding stores. Uses a pre-provisioned GCE persistent disk.
- **All other services**: Stateless, no local storage.

## Logging

All services output structured JSON to stdout, automatically captured by GKE's fluentbit agent and sent to Cloud Logging. The results API's usage logs are stripped of variant, gene, phenotype etc. information and exported to BigQuery via an existing Cloud Logging sink.

## Security

- Network policies enforce that db-api is only reachable from chat-backend, and rag-service only from chat-backend and mcp-server
- Workload Identity provides read-only GCP access (BigQuery + GCS) without key files
- HTTPS enforced via FrontendConfig redirect
- Google-managed SSL certificates
