# genetics-results-suite - Project specification

## Introduction

genetics-results-suite is the Terraform and Kubernetes deployment configuration for the genetics results platform. It orchestrates multiple microservices behind a single GKE Ingress with Google-managed SSL certificates and OAuth2-based authentication.

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

## Services

| Service | Source Repo | Port | Description |
|---------|-----------|------|-------------|
| frontend | genetics-results-browser | 3000 | React SPA via nginx |
| auth-gateway | — (nginx config) | 8080 | Auth gateway with oauth2-proxy integration |
| oauth2-proxy | — (upstream image) | 4180 | Google OAuth login |
| results-api | genetics-results-api | 4000 | FastAPI genetics results API |
| chat-backend | genetics-mcp-server | 8000 | LLM chat with MCP tools |
| mcp-server | genetics-mcp-server | 8080 | Standalone MCP server (streamable HTTP) |
| db-api | genetics-results-db | 8080 | BigQuery query proxy (internal only) |
| rag-service | genetics-rag-service | 8000 | RAG document retrieval (internal only) |

## Project structure

```
├── configs/
│   ├── rag/                  # RAG experiment configs (not k8s manifests)
│   ├── datasets.yaml              # canonical dataset/resource definitions (single source of truth)
│   └── datasets-schema-example.yaml  # YAML schema reference with example datasets
├── k8s/
│   ├── namespace.yaml
│   ├── deployments/          # deployment manifests for all services
│   ├── ingress/              # ingress, managed certs, backend/frontend configs
│   ├── configs/              # k8s config manifests (e.g., allowed emails)
│   ├── network-policies/     # network isolation rules
│   ├── secrets/              # non-sensitive secret templates
│   └── volumes/              # persistent volume claims
├── scripts/
│   ├── build-all.sh          # build and push all Docker images
│   ├── create-secrets.sh     # create k8s secrets from env vars
│   ├── deploy.sh             # full deploy (terraform + k8s)
│   ├── rollout.sh            # single-service image update
│   └── sync-datasets.sh      # copy datasets.yaml to sibling service repos for local dev
├── terraform/
│   ├── main.tf               # provider config, GCS backend
│   ├── gke.tf                # GKE cluster and node pool
│   ├── network.tf            # VPC, subnets, static IP, DNS
│   ├── registry.tf           # Artifact Registry for Docker images
│   ├── backups.tf            # daily disk snapshot schedule for chat-data PVC
│   ├── iam.tf                # service accounts, Workload Identity
│   ├── kubernetes.tf         # namespace, k8s service account
│   ├── variables.tf          # input variables
│   ├── outputs.tf            # output values
│   └── terraform.tfvars      # variable values (not committed)
└── docs/
    ├── datasets-yaml-schema.md  # schema reference for shared datasets.yaml config
    ├── nginx-setup.md        # notes for local VM nginx setup
    └── project-spec.md       # this file
```

## Shared Dataset Configuration

`configs/datasets.yaml` is the single source of truth for dataset and resource definitions consumed by both results-api and db-api. At deploy time, `deploy.sh` creates a Kubernetes ConfigMap (`datasets-config`) from this file and volume-mounts it into both service pods at `/app/configs/datasets.yaml`. Each service reads the path from the `DATASETS_CONFIG_PATH` environment variable.

For local development, `scripts/sync-datasets.sh` copies the canonical file to sibling service repos so they can run standalone. Each service's YAML loader defaults to `./configs/datasets.yaml` when `DATASETS_CONFIG_PATH` is not set.

Both services load all dataset/resource metadata exclusively from the YAML -- there are no hardcoded fallback dicts. In genetics-results-api, the profile `datasets.py` files are empty placeholders (datasets come from YAML via `app.config.yaml_loader`). The `dataset_to_resource` mapping in `profiles/*/common.py` is still hardcoded as the YAML schema does not yet support exact BQ dataset name to (resource, version) tuples.

## Authentication

- **oauth2-proxy** handles browser-based auth via Google OAuth, restricted by `oauth_email_domain` terraform variable (default: `finngen.fi`)
- **auth-gateway** (nginx) uses `auth_request` to validate requests against oauth2-proxy before proxying
- **results-api** also accepts `Authorization: Bearer` tokens (Google Identity Tokens or internal shared secret)
- **mcp-server** uses bearer token auth via `MCP_API_KEY` (not behind oauth2-proxy)
- **db-api** is internal-only, protected by NetworkPolicy (no auth needed)
- **Internal calls**: chat-backend authenticates to results-api via `INTERNAL_API_SECRET`
- **External MCP servers**: chat-backend proxies tools from external MCP servers (gnomAD, Open Targets) configured via `EXTERNAL_MCP_SERVERS` secret; `EXTERNAL_MCP_EXCLUDE_TOOLS` excludes specific tools by name (comma-separated)

## Infrastructure

- **GCP Project**: Configured via `project_id` in `terraform/terraform.tfvars`
- **Region**: Configured via `region` in `terraform/terraform.tfvars`
- **GKE Cluster**: Single cluster with Workload Identity for GCP API access
- **Networking**: VPC with private subnet, static IP for ingress
- **SSL**: Google-managed certificates for the domain configured in `terraform/terraform.tfvars`
- **Storage**: 10Gi PVC for chat-backend SQLite databases, file attachments, and tool result downloads; 50Gi PV/PVC for rag-service embedding stores
- **Backups**: Daily GCE disk snapshots of the chat-data PVC (14-day retention, configurable via `snapshot_retention_days`)
- **Terraform state**: GCS bucket `genetics-results-terraform`

## Monitoring

- **Google Managed Prometheus** is enabled on the GKE cluster, collecting system and workload metrics
- Metrics are stored in Cloud Monitoring (Monarch) and queryable via PromQL in Cloud Monitoring or Grafana
- Access metrics via GCP Console → Monitoring → Metrics Explorer (PromQL tab) or by deploying a Grafana instance

## Security

- Network policies enforce db-api is only reachable from chat-backend, and rag-service only from chat-backend and mcp-server
- Workload Identity provides read-only GCP access (BigQuery + GCS) without key files
- HTTPS enforced via FrontendConfig redirect
- All services output structured JSON logs, captured by GKE fluentbit and sent to Cloud Logging

## Operational procedures

- **Full deploy**: `./scripts/deploy.sh` — runs terraform apply, configures kubectl, deploys all k8s manifests; derives the container registry from terraform `project_id` (overridable via `REGISTRY` env var) and substitutes it in k8s manifests at deploy time; `CONFIG_PROFILE` (terraform variable, default `daly`) selects the data profile for results-api (`daly` or `finngen`); creates a `datasets-config` ConfigMap from `configs/datasets.yaml` and mounts it into results-api and db-api pods at `/app/configs/datasets.yaml` (env var `DATASETS_CONFIG_PATH`); rag-service is skipped by default (set `ENABLE_RAG=true` to include it)
- **Single service update**: `./scripts/rollout.sh <service> <tag>` — updates one deployment image (requires `REGISTRY` env var)
- **Build all images**: `./scripts/build-all.sh` — builds and pushes all Docker images to Artifact Registry (requires `REGISTRY` env var)
- **Build single image**: `./scripts/build.sh <service>` — clones, builds, and pushes one service's image (requires `REGISTRY` env var; branch overridable via same env vars as build-all.sh)
- **Create secrets**: `./scripts/create-secrets.sh` — creates k8s secrets from environment variables
