# genetics-results-suite - Project specification

## Introduction

genetics-results-suite is the Terraform and Kubernetes deployment configuration for the genetics results platform. It orchestrates multiple microservices behind a single GKE Ingress with Google-managed SSL certificates and OAuth2-based authentication.

## Architecture

```
Internet → GKE Ingress (HTTPS, Google-managed certs)
           └── /*  → auth-gateway (nginx, port 8080)
                     ├── /oauth2/*  → oauth2-proxy (OIDC → Keycloak, port 4180)
                     ├── /auth/*    → keycloak      (login UI + OIDC, port 8080) — unauthenticated
                     ├── /api/*     → bff           (port 5000) → results-api — browser oauth2 traffic;
                     │                 Authorization: Bearer requests bypass both and hit results-api
                     │                 (FastAPI, port 4000) directly
                     ├── /chat/v1/* → chat-backend  (FastAPI, port 8000); also exact /status
                     ├── /mcp       → mcp-server    (MCP streamable HTTP, port 8080) — bearer token auth;
                     │                 /.well-known/oauth-protected-resource[/mcp] served unauthenticated
                     └── /*         → frontend      (nginx, port 3000)

Login flow: oauth2-proxy → Keycloak (genetics realm) → Google or Apple IdP.

Request-duration ceiling: the Ingress backend service (`k8s/ingress/backend-configs.yaml`,
`timeoutSec: 1800`) caps how long *any* response may take, streaming or not — GCLB's
`timeoutSec` is a total response timeout, not an idle timeout, so it kills a chat SSE
stream mid-answer even while chunks are flowing (the browser reports `TypeError: network
error`). It was 300s, which cut long chat turns; nginx's per-location `proxy_read_timeout`
on `/chat/v1/` is idle-based and is kept at or above this value so the LB is never the
shorter cap. Any turn expected to run longer than this needs both raised together.

Internal only (ClusterIP + NetworkPolicy):
  ├── db-api            (BigQuery proxy, port 8080) — accessible from chat-backend + mcp-server
  ├── rag-service       (RAG retrieval, port 8000)  — only accessible from chat-backend + mcp-server
  ├── keycloak          (identity broker, port 8080) — reached via the /auth path on the primary domain
  └── keycloak-postgres (Keycloak DB, port 5432)     — backed up daily to GCS
```

## Services

| Service | Source Repo | Port | Description |
|---------|-----------|------|-------------|
| frontend | genetics-results-browser | 3000 | React SPA via nginx |
| bff | genetics-results-browser (`bff/Dockerfile`) | 5000 | Backend-for-frontend: assembles browser `POST /v1/results` from the results-api fan-out and passes other `/api/*` calls through. Shares the frontend repo and image tag; image `genetics-results-browser-bff` |
| auth-gateway | — (nginx config) | 8080 | Auth gateway with oauth2-proxy integration; also serves the keycloak login path `<domain>/auth` |
| oauth2-proxy | — (upstream image) | 4180 | OIDC login against Keycloak, or Google directly where the broker is disabled |
| keycloak | keycloak/ (local build) | 8080 | Identity broker: Google + Apple sign-in, single OIDC issuer |
| keycloak-postgres | — (upstream image) | 5432 | Keycloak database (PVC + daily pg_dump to GCS) |
| results-api | genetics-results-api | 4000 | FastAPI genetics results API |
| chat-backend | genetics-mcp-server | 8000 | LLM chat with MCP tools |
| mcp-server | genetics-mcp-server | 8080 | Standalone MCP server (streamable HTTP) |
| db-api | genetics-results-db | 8080 | BigQuery query proxy (internal only) |
| rag-service | genetics-rag-service | 8000 | RAG document retrieval (internal only) |
| monitor | — (scripts/monitor/) | — | CronJob: health checks, BQ summary, log alerts → Slack |
| analyze-conversations | genetics-mcp-server (same image) | — | Nightly CronJob: LLM scoring of chat conversations (see below) |
| keycloak-postgres-backup | — (upstream `google/cloud-sdk`) | — | Daily CronJob: `pg_dump` of the Keycloak DB to GCS (only when the broker is enabled) |

### results-api deployment tuning

`k8s/deployments/results-api.yaml` sets two env vars that bound the resources its
range-query path (in-process bgzf/tabix reads over GCS) consumes; both have safe
in-code defaults, so they are tuning knobs, not requirements:

| Env var | Value (manifest) | Default in code | Purpose |
|---------|------------------|-----------------|---------|
| `TABIX_FILTER_WORKERS` | `2` | `min(4, cpu-1)` | Size of the decompress/filter `ProcessPoolExecutor`. The default reads `os.cpu_count()` (host cores, **not** the cgroup CPU quota), so on a large node an unbounded count would spawn many idle, FD-holding workers. Set to match the container's CPU limit (currently `2`). |
| `GCS_MAX_CONNECTIONS` | `128` | `128` | Process-wide cap on concurrent GCS range-fetch sockets. A single all-resources variant batch fans out across ~12-15 data files; without a cap the simultaneously-open sockets exhausted the file-descriptor limit ("Too many open files"). Lower if the pod's `NOFILE` limit is tight, raise for more fetch parallelism. |

The container entrypoint (`genetics-results-api`'s `start.sh`) also raises `ulimit -n`
to 65536 as defense-in-depth. Keep `TABIX_FILTER_WORKERS` in step with the deployment's
CPU `limits` if you change them.

## Project structure

```
├── configs/
│   ├── rag/                  # RAG experiment configs (not k8s manifests)
│   ├── datasets.yaml              # canonical dataset/resource definitions (single source of truth)
│   ├── datasets-schema-example.yaml  # YAML schema reference with example datasets
│   └── *_pheno.json          # per-phenotype metadata for external GWAS (covid_hgi, ibd_gwas)
├── k8s/
│   ├── namespace.yaml
│   ├── deployments/          # deployment manifests for all services (+ analyze-conversations
│   │                         #   and monitor CronJobs)
│   ├── ingress/              # backend/frontend configs only — the Ingress and ManagedCertificate
│   │                         #   are generated by deploy.sh from the terraform `domains` list
│   ├── configs/              # k8s config manifests (bearer-auth allow-list; the oauth2-proxy
│   │                         #   allow-list ConfigMap is generated by deploy.sh, no manifest)
│   ├── cronjobs/             # cronjobs applied only when Keycloak is enabled (postgres backup)
│   ├── network-policies/     # network isolation rules
│   └── volumes/              # persistent volume claims
├── keycloak/                 # Keycloak image build context (official image + Apple IdP and
│                             #   email-allowlist extension JARs), realm/client/IdP templates,
│                             #   and the `genetics` login theme
├── scripts/
│   ├── build-all.sh          # build and push all Docker images
│   ├── build.sh              # build and push one service's image
│   ├── create-secrets.sh     # create k8s secrets from env vars
│   ├── deploy.sh             # full deploy (terraform + k8s)
│   ├── rollout.sh            # single-service image update
│   ├── sync-datasets.sh      # copy datasets.yaml to sibling service repos for local dev
│   ├── chat_usage_stats.sh   # chat usage counts from the BigQuery chat-log sink
│   ├── keycloak-register-client.sh    # register/update an MCP OAuth client in the live realm
│   ├── keycloak-register-brainzzz.sh  # the brainzzz client specifically
│   ├── keycloak-bind-allowlist.sh     # bind the email allow-list authenticator + realm attrs
│   ├── keycloak-get-token.sh          # browser auth-code+PKCE flow, prints an access token
│   └── monitor/              # monitoring CronJob (Python)
│       ├── Dockerfile
│       ├── requirements.txt
│       ├── main.py            # CLI entrypoint (--health, --bq-summary, --alerts, --all)
│       ├── health.py          # service liveness + dataset accessibility checks
│       ├── bq_summary.py      # BigQuery view row counts and resource coverage
│       ├── alerter.py         # Cloud Logging alerter with SQLite dedup
│       └── slack.py           # Slack webhook helper
├── terraform/
│   ├── main.tf               # provider config, GCS backend
│   ├── gke.tf                # GKE cluster and node pool
│   ├── network.tf            # VPC, subnets, static IP, DNS
│   ├── registry.tf           # Artifact Registry for Docker images
│   ├── backups.tf            # daily disk snapshot schedule for chat-data PVC, Keycloak
│   │                         #   backup bucket
│   ├── logging.tf            # Cloud Logging → BigQuery sinks (gated by `enable_log_sinks`)
│   ├── iam.tf                # service accounts, Workload Identity
│   ├── kubernetes.tf         # namespace, k8s service account
│   ├── variables.tf          # input variables
│   ├── outputs.tf            # output values
│   ├── {daly,finngen}.tfbackend       # per-profile GCS state backends
│   ├── terraform.tfvars.example
│   ├── terraform.tfvars.{daly,finngen}  # per-deployment values (not committed)
│   └── terraform.tfvars      # active variable values (not committed)
└── docs/
    ├── adding-datasets.md    # how to add a new dataset across repos/profiles
    ├── datasets-yaml-schema.md  # schema reference for shared datasets.yaml config
    ├── keycloak-apple-signin.md # Keycloak broker setup, MCP OAuth clients, backup/restore
    ├── genegenie-migration.md   # record of the finngenie → genegenie legacy-hostname redirect
    ├── local-dev-vm.md       # running the whole suite from source on a GCE VM (no docker/k8s)
    ├── nginx-setup.md        # notes for the legacy VM nginx setup
    └── project-spec.md       # this file
```

## Shared Dataset Configuration

`configs/datasets.yaml` is the single source of truth for dataset and resource definitions consumed by both results-api and db-api. At deploy time, `deploy.sh` creates a Kubernetes ConfigMap (`datasets-config`) from this file and volume-mounts it into both service pods at `/app/configs/datasets.yaml`. Each service reads the path from the `DATASETS_CONFIG_PATH` environment variable.

For local development, `scripts/sync-datasets.sh` copies the canonical file to sibling service repos so they can run standalone. Each service's YAML loader defaults to `./configs/datasets.yaml` when `DATASETS_CONFIG_PATH` is not set. `deploy.sh` also runs `sync-datasets.sh` (best-effort) before building the ConfigMap, so the committed copies in the sibling repos don't drift from the canonical file. Note that this only keeps `datasets.yaml` in sync — the results-api product configs (`app/config/profiles/*/credible_sets.py`, `summary_stats.py`, `common.py`, etc., which hold the actual GCS file paths and the `dataset_to_resource` map) live only in genetics-results-api and are baked into its image at build time, so changes there require rebuilding and rolling out the results-api image.

Both services load all dataset/resource metadata exclusively from the YAML -- there are no hardcoded fallback dicts. In genetics-results-api, the profile `datasets.py` files are empty placeholders (datasets come from YAML via `app.config.yaml_loader`). The `dataset_to_resource` mapping in `profiles/*/common.py` is still hardcoded as the YAML schema does not yet support exact BQ dataset name to (resource, version) tuples.

The YAML defines two exome dataset resources with different filtering levels: `genebass_exome` (filtered to p < 1e-4) and `ibd_exome` (resource: `ibd_exome_2026`, containing only exome-wide significant variants at p < 3e-7 plus LD-curated variants at p < 5e-6, with 3 phenotypes: IBD, UC, CD). The `dataset_to_resource_rules` map `IBD_exome_2026` to `ibd_exome_2026` for `exome_variant_results_v` and `gene_burden_results_v` views.

ASM-QTL (allele-specific methylation QTL) data from deCODE is served via both BigQuery (`asm_qtl` table / `asm_qtl_v` view) and the standard sumstats endpoint (`/summary_stats/decode/asmqtl`). Two datasets: `decode_asmqtl_cpg` (CpG methylation, phenotype code `CpG`) and `decode_asmqtl_mds` (MDS methylation, phenotype code `MDS`), both under the `decode` resource. The `dataset_to_resource_rules` map `deCODE%` to `decode` for `asm_qtl_v`.

caQTL results are keyed by **peak**, not gene: `finngen_caqtl` rows in `credible_sets_v` carry a peak id (e.g. `chr5-35482826-35484273`) in the `trait` column, so a gene-based caQTL question is only answerable by joining through the Open4Gene peak-to-gene link table (`peak_to_gene_v`, the BQ face of the `finngen_chromatin_peaks` dataset) on `peak_id = trait` and `cell_type = cell_type`. The `tables.peak_to_gene_v` block in `datasets.yaml` carries the column descriptions, worked join examples, and an explicit warning against approximating the link by coordinates (linked peaks sit up to ~1 Mb away and most nearby peaks are not linked) — without it agents fell back to hand-written coordinate-window SQL that silently answered a different question. The `FinnGen%` resource rule is scoped to include `peak_to_gene_v` since the table is FinnGen ATAC-seq only.

MPRA (Siraj et al. 2026) is a new functional-annotation product — measured intrinsic cis-regulatory allelic activity from a massively parallel reporter assay (221K fine-mapped + 86K control variants tested in 5 cell lines: K562, HepG2, SK-N-SH, HCT116, A549), served for both profiles. Like open_chromatin/variant_effect it is a new vertical rather than a plain dataset: one dataset `siraj_mpra` under resource `siraj_mpra` (`dataset_to_resource_rules` map `siraj_mpra%` -> `siraj_mpra`), `data_type: mpra`, `trait_type: null`. The source WIDE per-variant TSV is munged to LONG (one row per variant × `cell_line`, where `cell_line` ∈ {`meta`, K562, HEPG2, SKNSH, HCT116, A549}) carrying the emVar/active/log2Skew/log2FC calls, bgzip+tabix-indexed on GCS. Served two ways: results-api tabix range endpoints (`/api/v1/mpra` by-variant / by-region / by-gene, its own tabix vertical) and BigQuery (`mpra` base table / `mpra_v` view, which adds `resource`). The genetics-mcp-server exposes `get_mpra_by_variant`, `get_mpra_by_region`, and `get_mpra_by_gene`. Scientifically it is the functional-validation layer for the regulatory-buffering story (Kanai et al.): emVar rates and allelic-effect concordance scale with FinnGen fine-mapping PIP, and MPRA measures intrinsic reporter activity distinct from endogenous eQTL/caQTL and from in-silico variant_effect predictions.

External GWAS pseudo credible sets (COVID-19 HGI, PGC SCZ, PGC BIP, GP2 Parkinson's, and IIBDGC IBD/UC/CD) live in a single shared file `gs://<bucket>/credible_sets/ext_pseudo/EXT_*_pseudo_credible_sets.*.tsv.gz` referenced by five datasets (`covid_hgi`, `pgc_scz`, `pgc_bip`, `gp2_pd`, `ibd_gwas`). Per-phenotype individual CS files are organized into per-source subdirs `ext_pseudo/individual/<source>/` (e.g. `covid_hgi/`, `pgc_scz/`, `iibdgc/`) so phenotype lookups disambiguate when multiple datasets share the same resource (e.g. `pgc_scz` and `pgc_bip` both belong to resource `pgc`). The `dataset_to_resource_rules` map the combined file's `dataset` column values `COVID19_HGI%`, `PGC`, `GP2`, and `IIBDGC` to resources `covid_hgi`, `pgc`, `gp2`, and `ibd_gwas`. The IIBDGC pseudo CS reuse the existing `ibd_gwas` dataset/resource (the IIBDGC IBD/UC/CD GWAS meta-analysis whose summary statistics are already served); they are not formally fine-mapped, so the `ibd_gwas` dataset carries `pseudo_credible_sets: true`. The results-api dedups range queries by combined-file path (one tabix per shared file) and uses a per-row resource filter (`dataset_to_resource` in each profile's `common.py`) so each resource only sees its own rows — the `dataset` column value must therefore be present in that map (e.g. `IIBDGC → ibd_gwas`) for region/variant credible-set queries to attribute its rows.

PGC schizophrenia additionally has **published** fine-mapping served alongside its pseudo credible sets, under the same `pgc` resource: dataset `pgc_scz_finemap`, `dataset` column value `PGC_SCZ_2022`, from Trubetskoy et al. 2022 supplementary table ST11a (`gs://<bucket>/credible_sets/pgc_scz_finemap/2022/PGC_SCZ_2022_credible_sets.tsv.gz`, munged by `genetics-results-munge/scripts/munge_pgc_scz_finemap.sh`). Two datasets under one resource therefore now carry credible sets for the same trait code `SCZ`, one pseudo (`pgc_scz`, `dataset` = `PGC`) and one fine-mapped (`pgc_scz_finemap`), which is the intended state — queries that want only genuine fine-mapping must filter on `dataset`, not on `resource`. The `PGC` rule is an exact match, so a separate `PGC_SCZ%` rule maps the new value to `pgc`. Unlike the pseudo sets this file is loaded by `load_credsets_coloc.sh` rather than `load_pseudo.sh`, and it is not part of the shared `ext_pseudo` file. Its caveats are documented in `genetics-results-munge/docs/pgc-scz-finemapping.md`: FINEMAP was run with several causal variants allowed per locus (PIPs within a set sum to roughly *k*, not 1), `cs_min_r2` is unavailable, and the X-linked sets carry no `aaf` because the wave 3 summary statistics are autosomes only.

### Open chromatin and variant effect (Products A and B)

Two data products cover chromatin accessibility rather than association statistics. Both carry
`trait_type: null` and their own `data_type` (`open_chromatin` / `variant_effect`), and both are
served the same way as the other tabix verticals: the GCS file paths live in
`genetics-results-api`'s `app/config/profiles/<profile>/{open_chromatin,variant_effect}.py`
(baked into the image), while the dataset registry lives in `configs/datasets.yaml`.

- **Open chromatin (Product A)** — an atlas of accessible/active regions labelled by cell type,
  tissue and condition, answering "which contexts is this variant/region/gene accessible in?".
  Six datasets: `marderstein_open_chromatin` (Marderstein/Kundaje 2026, brain + heart),
  `li_brain_open_chromatin` (Li 2023), `catlas_open_chromatin` (Zhang 2021),
  `epimap_open_chromatin` (EpiMap ChromHMM active states + enhancer-gene links),
  `calderon_open_chromatin` (Calderon 2019 immune stimulation, the only dataset with two
  `condition` values) and `rosmap_open_chromatin` (ROSMAP/Xiong 2023 AD brain). The files are
  **interval-indexed** tabix TSVs.
- **Variant effect (Product B)** — in-silico predicted variant effect on accessibility. Two
  datasets, both under the `marderstein` resource because the resource ships two distinct
  predictors: `marderstein_chrombpnet` (ChromBPNet, per-context, thresholded per the row-scale
  policy) and `marderstein_flare` (FLARE, pan-context). The files are **point-indexed**
  (`-s1 -b2 -e2`) tabix TSVs.

Endpoints (results-api): `/api/v1/open_chromatin/{region/{chrom}/{start}/{end}, variant/{variant},
peak/{peak_id}}` and `/api/v1/variant_effect/{variant/{variant}, region/{chrom}/{start}/{end},
gene/{gene}}`. The `variant` path parameter accepts both `chrom_pos_ref_alt` and `chrom:pos:ref:alt`.

In BigQuery the products land in the `open_chromatin` and `variant_effect` tables with
`open_chromatin_v` / `variant_effect_v` views deriving `resource` from the `dataset` column
(both registered in the db-api `VIEWS` allowlist). Tables are created by
`genetics-results-db`'s `scripts/setup_bigquery.sh`; rows are loaded separately by
`scripts/load_open_chromatin.sh` and `scripts/load_variant_effect.sh` (set
`PROJECT_ID`/`DATASET_ID`/`GCS_BUCKET`/`GCS_PREFIX` per profile — the daly bucket has no prefix,
finngen uses `results_api_data/`).

The agent reaches both products through five MCP tools: `get_open_chromatin_by_{variant,region,gene}`
and `get_variant_effect_by_{variant,gene}`. The three position-based tools go through results-api;
the two **gene-based** tools resolve gene → coordinates via BigQuery (`gene_annotations_v`) because
results-api has no by-gene open-chromatin endpoint, so they work only where the caller can reach
db-api — both chat-backend and the standalone mcp-server (mcp-server sets `BIGQUERY_API_URL`
and is admitted by the `allow-ingress-db-api` NetworkPolicy; fixed in `genetics-results-suite-v1n`).

## Authentication

- **Keycloak** is the identity broker, **enabled per deployment profile** (`ENABLE_KEYCLOAK` in `deploy.sh`, defaulting on for `daly`, off for `finngen`). When enabled it presents the provider chooser and federates **Google** and **Apple** (Sign in with Apple), exposing one OIDC issuer at `https://${KEYCLOAK_HOST}/realms/genetics`. It runs in-cluster (`k8s/deployments/keycloak.yaml`) behind the auth-gateway under the **`/auth` path on the primary domain** (`KEYCLOAK_HOST` defaults to `<domain>/auth`), so it reuses the existing DNS record, managed cert and ingress rather than needing an `auth.<domain>` subdomain; it is backed by an in-cluster Postgres (`keycloak-postgres`) with daily `pg_dump` backups to GCS. The image (`keycloak/`) is the official Keycloak plus a bundled Apple identity-provider extension. Setup, Apple Developer prerequisites, secret rotation and restore are documented in `docs/keycloak-apple-signin.md`.
- **oauth2-proxy** handles browser sessions. Its provider is profile-driven (`OAUTH2_PROXY_PROVIDER`): `oidc` against Keycloak where the broker is enabled (daly), or `google` directly otherwise (finngen). Either way it authorizes against an allow-list: one or more **domains** (`OAUTH2_PROXY_EMAIL_DOMAINS`, comma-separated) **or** specific **addresses** (the `--authenticated-emails-file`). Both lists come from terraform — `oauth_email_domain` (comma-separated, e.g. `broadinstitute.org,finngen.fi`) and `oauth_allowed_emails` (specific addresses, e.g. Apple users on `me.com`/`icloud.com`/`privaterelay.appleid.com`).
- **auth-gateway** (nginx) uses `auth_request` to validate requests against oauth2-proxy before proxying; a `location /auth/` block (injected by `deploy.sh` via `${KEYCLOAK_SERVER}`, and served without the `auth_request` since it *is* the auth endpoint) strips the prefix and proxies to Keycloak. The email returned by oauth2-proxy is passed to backends in the `X-Goog-Authenticated-User-Email` header (the `accounts.google.com:` prefix is legacy and provider-agnostic — backends read only the address after the colon).
- **results-api** also accepts `Authorization: Bearer` tokens (Google Identity Tokens or internal shared secret)
- **mcp-server** is not behind oauth2-proxy; it accepts `Authorization: Bearer` tokens via **four** paths: the `MCP_API_KEY` shared secret(s); Google Identity Tokens (JWT validated against `email_verified` plus the configured email/domain allow-list); per-user API tokens issued via the chat API; and **Keycloak OAuth 2.1 access tokens** (see below). NOTE: the Google JWT path validates **Google** Identity Tokens only — programmatic access for Apple-only identities is a known follow-up.
- **MCP OAuth (resource-server) path**: when `OAUTH_ISSUER` + `OAUTH_RESOURCE_URL` are set (daly/genegenie; empty for finngen, so the path is inert there), the mcp-server acts as an OAuth 2.1 **resource server**. It validates Keycloak-issued JWT access tokens (RS256 signature via the realm JWKS, `iss`/`aud`/`exp`, then the same email/domain allow-list), and advertises RFC 9728 discovery at `/.well-known/oauth-protected-resource` (routed unauthenticated through auth-gateway; returns `WWW-Authenticate: Bearer resource_metadata=…` on 401), so MCP clients auto-discover the Keycloak authorization server. The Keycloak issuer is **path-based** (`https://<host>/auth/realms/genetics`); tokens must carry `aud` = `OAUTH_RESOURCE_URL` (`https://<host>/mcp`), enforced per client via an audience mapper. Each external app is its own Keycloak client (registered manually — no open Dynamic Client Registration); onboard one with `scripts/keycloak-register-client.sh <clientId> <redirect-uri>…` (brainzzz is the first, via `keycloak/brainzzz-client.json.template` + `scripts/keycloak-register-brainzzz.sh`). Setup is documented in `docs/keycloak-apple-signin.md`.
- **Shared bearer-auth allow-list**: `ALLOWED_EMAILS`, `ALLOWED_EMAIL_DOMAINS` and `GOOGLE_TOKEN_AUDIENCE` (used for Google Identity Token JWT validation in both results-api and mcp-server) are sourced from a single Kubernetes ConfigMap `bearer-auth-allowed` (manifest: `k8s/configs/bearer-auth-allowed.yaml`), populated from `oauth_allowed_emails`/`oauth_email_domain` plus the `GOOGLE_TOKEN_AUDIENCE` export in `deploy.sh`, consumed by both deployments via `envFrom: configMapRef` to prevent config drift
- **Google token audience**: `GOOGLE_TOKEN_AUDIENCE` is the `aud` claim a Google Identity Token must carry. `id_token.verify_oauth2_token` skips the audience check when none is supplied, so while it is unset **any** Google-signed id_token with an allow-listed email is accepted — including one minted for an unrelated application, which that application's operator could replay here. It defaults to the gcloud CLI's OAuth client id (`32555940559.apps.googleusercontent.com`), because the documented flow is `gcloud auth print-identity-token` and user credentials cannot request a custom audience. This blocks cross-application replay; it is not an identity gate on its own (anyone with a Google account can mint a token with that audience), so the email allow-list remains the access control. Add further client ids, comma-separated, if service accounts call the API with audience-scoped tokens.
- **db-api** is internal-only (NetworkPolicy) **and** requires `Authorization: Bearer $INTERNAL_API_SECRET` on every endpoint except `/health`. The NetworkPolicy is not a boundary on its own: mcp-server is permitted through it and is itself reachable from outside, so anything that could drive mcp-server could reach BigQuery behind it. Fails open (with a startup warning) if the env var is unset, so local runs and mid-rollout clusters keep working.
- **Internal calls**: chat-backend authenticates to results-api via `INTERNAL_API_SECRET`
- **External MCP servers**: chat-backend proxies tools from external MCP servers (gnomAD, Open Targets) configured via `EXTERNAL_MCP_SERVERS` secret; `EXTERNAL_MCP_EXCLUDE_TOOLS` excludes specific tools by name (comma-separated)
- **Third-party live resources called natively**: separately from the proxied MCP servers, genetics-mcp-server calls several public APIs directly over its own unauthenticated HTTP client (never the internal secret): MouseMine/MGI (`search_mgi`), UniProt + EBI Proteins (`get_protein_annotations`, `map_protein_variants`, `get_variant_protein_effect`, `search_uniprot`), myvariant.info (`get_myvariant_annotations`), cBioPortal (`search_cbioportal`), and the literature/web backends (Europe PMC, Perplexity, Tavily). All are chat-backend only — excluded from the standalone mcp-server via `_mcp_disabled`. None needs an API key except Perplexity and Tavily. Per-tool behaviour is documented in `../genetics-mcp-server/docs/project-spec.md`.

### Genome build across resources

The suite is **GRCh38** throughout, but the third-party resources are not uniform, and this is a correctness boundary rather than a detail:

| Resource | Build | Handling |
|---|---|---|
| Suite datasets, results-api, BigQuery | GRCh38 | native |
| myvariant.info | GRCh38 | assembly pinned to `hg38` on every request |
| UniProt / EBI genomic-HGVS lookups | GRCh38 | pinned per-chromosome RefSeq accessions |
| cBioPortal | **mostly GRCh37** (467 of 539 studies hg19) | never lifted over; matched on gene symbol and protein change only |

cBioPortal is the one resource whose coordinates must not be compared with the
suite's. `search_cbioportal` therefore keys every query type on build-independent
identifiers, returns coordinates grouped under the build they came from without
merging, and carries a `genome_build_note` on every response pointing the agent at
`get_variant_protein_effect` to convert a GRCh38 variant into a protein change
first. Adding any further third-party resource requires making the same decision
explicitly: pin the build, or match on something build-independent.

## Infrastructure

- **GCP Project**: Configured via `project_id` in `terraform/terraform.tfvars`
- **Region**: Configured via `region` in `terraform/terraform.tfvars`
- **GKE Cluster**: Single cluster with Workload Identity for GCP API access
- **Networking**: VPC with private subnet, static IP for ingress
- **SSL**: Google-managed certificates for the domain configured in `terraform/terraform.tfvars`
- **Storage**: 10Gi PVC (`chat-data`) for chat-backend SQLite databases, file attachments, and tool result downloads; 50Gi PV/PVC (`rag-stores`) for rag-service embedding stores; 1Gi PVC (`monitor-data`) for the monitor's alert-dedup SQLite DB; 5Gi PVC (`keycloak-postgres-data`) for the Keycloak database
- **Log sinks**: `terraform/logging.tf` optionally creates two Cloud Logging → BigQuery sinks (results-api `endpoint_access` records → `genetics_api_logs`, chat-backend container logs at severity ≥ INFO → `genetics_chat_logs`), gated by `enable_log_sinks` (default `false`)
- **Backups**: Daily GCE disk snapshots of the chat-data PVC (14-day retention, configurable via `snapshot_retention_days`)
- **Terraform state**: Per-profile GCS backends (`daly.tfbackend` → `genetics-results-terraform-daly`, `finngen.tfbackend` → `genetics-results-terraform`); `deploy.sh` auto-selects based on `config_profile`

## Monitoring

### Metrics (Prometheus)

- **Google Managed Prometheus** is enabled on the GKE cluster, collecting system and workload metrics
- Metrics are stored in Cloud Monitoring (Monarch) and queryable via PromQL in Cloud Monitoring or Grafana
- Access metrics via GCP Console → Monitoring → Metrics Explorer (PromQL tab) or by deploying a Grafana instance

### Monitor CronJob

A Python-based monitoring CronJob (`scripts/monitor/`) runs 3x/day (every 8 hours, schedule `0 */8 * * *`) and sends results to Slack. Deployed as a Kubernetes CronJob in the `genetics` namespace.

**What it checks:**

- **Service health** (`health.py`): HTTP liveness checks against results-api `/healthz`, chat-backend `/healthz`, frontend `/`, mcp-server `/healthz`, and db-api `/health`. Then loads `datasets.yaml` and verifies each API-served dataset is present in the results-api `/api/v1/datasets` response.
- **BigQuery data coverage** (`bq_summary.py`): Queries BQ views (`credible_sets_v`, `colocalization_v`, `coloc_credsets_v`, `exome_variant_results_v`, `gene_burden_results_v`, `asm_qtl_v`, `mpra_v`) for row counts and distinct resources. For credible_sets/exome/gene_based/asm_qtl/mpra views, compares actual resources against expected from `dataset_to_resource_rules`. For colocalization views, derives expected resources from the results-api's dataset products (coloc pairs). Collection sub-resources (eQTL Catalogue `qtd*`) are collapsed to their parent. API resource names are mapped to BQ resource names via `dataset_to_resource_rules` patterns.
- **Log alerts** (`alerter.py`): Queries Cloud Logging for `severity >= WARNING` entries from `k8s_container` resources in the `genetics` namespace over the last check interval (default 8h). Groups by container, deduplicates via SQLite, and only reports new alerts.

**Severity reclassification:** GKE's logging agent tags *everything a container writes to stderr* as `severity=ERROR` regardless of content, so the Cloud Logging severity is meaningless for the many services that log normally to stderr (uvicorn, postgres, batch scripts). The alerter therefore recovers the level the application itself reported by matching the message text against `_LEVEL_PATTERNS` (python/uvicorn `INFO:`, nginx `[error]`, postgres `[27] LOG:`), ranks it via `_LEVEL_RANK`, and drops anything below WARNING. Messages carrying no recognizable level fall back to the Cloud Logging severity (fail open, so unknown formats still alert). The count of dropped entries is logged to the CronJob's stdout so the suppression is never silent. Services that log progress to stderr must prefix it with a level (see `analyze_conversations.py`) or it will be reported as an error.

**Ignore list:** `_IGNORE_PATTERNS` drops known-benign `(container, message regex)` pairs outright — currently only oauth2-proxy probe/callback noise. The mcp-server query-parameter-token warning was deliberately removed from the list: the query-token fallback is off by default in genetics-mcp-server, so if it fires again someone enabled `MCP_ALLOW_QUERY_TOKEN` and credentials are travelling in URLs — that must reach Slack.

**Deduplication:** The alerter normalizes log messages (stripping timestamps, UUIDs, IPs, request IDs) and hashes `container|normalized_message` into a dedup key. Seen keys are stored in a SQLite database on a PVC (`/data/monitor.db`) with a 24-hour TTL. Expired entries are cleaned up at the start of each run.

**Slack notifications:** Results are formatted as Slack Block Kit messages with deployment flag emoji (Finnish flag for finngen, US flag for daly). When failures or alerts are detected, the configured user is @mentioned for notification. Posted via incoming webhook (`SLACK_WEBHOOK_URL` from `genetics-secrets`). Human-readable output is also printed to stdout.

**Configuration (env vars):**

| Variable | Source | Default | Description |
|----------|--------|---------|-------------|
| `GCP_PROJECT` | CronJob manifest (envsubst) | — | GCP project for BQ and Logging clients |
| `CONFIG_PROFILE` | CronJob manifest (envsubst) | `finngen` | Active dataset profile (also selects flag emoji) |
| `BQ_DATASET` | CronJob manifest (envsubst) | `genetics_results` | BigQuery dataset name |
| `SLACK_ALERT_USER_ID` | CronJob manifest (envsubst); exported by `deploy.sh` from `.env`/shell | — | Slack member ID(s) to @mention on failures; space/comma-separated for multiple users. Kept out of version control (`.env` is gitignored). |
| `DATASETS_CONFIG_PATH` | CronJob manifest | `/app/configs/datasets.yaml` | Path to datasets config |
| `INTERNAL_API_SECRET` | `genetics-secrets` | — | Bearer token for results-api |
| `SLACK_WEBHOOK_URL` | `genetics-secrets` | — | Slack incoming webhook URL |
| `K8S_NAMESPACE` | CronJob manifest | `genetics` | Namespace for log queries |
| `MONITOR_DB_PATH` | CronJob manifest | `/data/monitor.db` | SQLite dedup database path (on PVC) |
| `RESULTS_API_URL` | — | `http://results-api....:4000` | Override results-api URL |
| `ALERT_LOOKBACK_HOURS` | — | `8` | How far back to query logs |
| `ALERT_DEDUP_TTL_HOURS` | — | `24` | How long to suppress duplicate alerts |

**Manual trigger:** `kubectl create job --from=cronjob/monitor monitor-$(date +%s) -n genetics`

**Network policies:** `k8s/network-policies/monitor-policy.yaml` allows the monitor pod (label `app: monitor`) to reach results-api (4000), chat-backend (8000), frontend (3000), mcp-server (8080), and db-api (8080). The service account has `roles/logging.viewer` for Cloud Logging access (configured in `terraform/iam.tf`).

## Security

- Network policies enforce db-api is only reachable from chat-backend and mcp-server, and rag-service only from chat-backend and mcp-server
- **Container privileges**: every application container sets `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]` and the `RuntimeDefault` seccomp profile. `db-api` and `bff` additionally run `runAsNonRoot` (uid 10001 / 1000) since they write nothing outside their image; results-api, chat-backend and mcp-server still run as root because they raise `ulimit`, shell out to `gcloud`, cache tabix indexes, or own root-owned files on the `chat-data` PVC. chat-backend sets `fsGroup: 1032` so the pre-existing SQLite files stay writable once `CAP_DAC_OVERRIDE` is dropped.
- Workload Identity provides read-only GCP access (BigQuery + GCS) without key files
- HTTPS enforced via FrontendConfig redirect
- All services output structured JSON logs, captured by GKE fluentbit and sent to Cloud Logging

## Operational procedures

**Important:** `deploy.sh` does NOT build images. To ship new service code you must build first, then deploy. The typical workflow is:

1. `./scripts/build-all.sh` (or `./scripts/build.sh <service>` for one service) — builds and pushes new `:latest` images to Artifact Registry
2. `./scripts/deploy.sh` (or `./scripts/rollout.sh <service>` for one service) — applies manifests and force-restarts pods so they pull the freshly-built `:latest` images

If you only run `deploy.sh` without building, the rollout restart will re-pull whatever `:latest` currently points to in the registry (i.e. the last build), so no code changes from upstream service repos will be picked up.

- **Full deploy**: `./scripts/deploy.sh` — runs terraform apply, configures kubectl, deploys all k8s manifests; derives the container registry from terraform `project_id` (overridable via `REGISTRY` env var) and substitutes it in k8s manifests at deploy time; `CONFIG_PROFILE` (terraform variable, default `daly`) selects the data profile for results-api (`daly` or `finngen`); creates a `datasets-config` ConfigMap from `configs/datasets.yaml` and mounts it into results-api and db-api pods at `/app/configs/datasets.yaml` (env var `DATASETS_CONFIG_PATH`); rag-service is skipped by default (set `ENABLE_RAG=true` to include it); after applying manifests, force-restarts all app deployments so pods pick up `:latest` images and ConfigMap changes (subPath mounts don't propagate; oauth2-proxy doesn't hot-reload). Does **not** build images — run `build-all.sh` or `build.sh` first if you need new code.
- **Branding (product name)**: the displayed product name is configurable per deployment via the `app_name` terraform variable in `terraform.tfvars` (single source of truth; default `FinnGenie`, e.g. `GeneGenie` for the daly profile). Resolution order everywhere is **`APP_NAME` env override → `app_name` in `terraform.tfvars` → `FinnGenie`**. `deploy.sh` reads it from terraform output and injects `APP_NAME` into the chat-backend pod (used by the MCP server's assistant persona in `default_system_prompt`). The frontend bakes it in at build time: `build.sh`/`build-all.sh` resolve `APP_NAME` (grepping `terraform.tfvars` directly, like `deploy.sh` does for `config_profile`) and pass `--build-arg APP_NAME` → Dockerfile writes `VITE_APP_NAME` into `.env` → `import.meta.env.VITE_APP_NAME` (read via `src/config/appName.ts`). So setting `app_name` once in the deployment's tfvars covers both the frontend build and the backend deploy. Logos and the `finngen.fi` CORS/domain identifiers are unchanged.
- **Single service update**: `./scripts/rollout.sh <service> [tag]` — updates one deployment image (requires `REGISTRY` env var; `tag` defaults to `latest`). Known services: `frontend`, `bff`, `results-api`, `chat-backend`, `mcp-server`, `db-api`, `rag-service`. It only swaps container images, so ConfigMap-driven pods (auth-gateway) and the CronJobs need `deploy.sh`.
- **Build all images**: `./scripts/build-all.sh` — clones the service repos (branch overridable per service, all default `master` except rag-service) and builds/pushes all Docker images to Artifact Registry, including the local `monitor` and `keycloak` build contexts (requires `REGISTRY` env var)
- **Build single image**: `./scripts/build.sh <service>` — clones, builds, and pushes one service's image (requires `REGISTRY` env var; branch overridable via same env vars as build-all.sh)
- **Create secrets**: `./scripts/create-secrets.sh` — creates k8s secrets from environment variables (includes `SLACK_WEBHOOK_URL` for the monitor)
- **Build monitor image**: included in `./scripts/build-all.sh`; builds `scripts/monitor/` as the `monitor` image
- **Deploy monitor**: included in `./scripts/deploy.sh`; applies `k8s/deployments/monitor-cronjob.yaml` with `REGISTRY` envsubst
- **Manual monitor run**: `kubectl create job --from=cronjob/monitor monitor-manual -n genetics`

## Conversation analysis pipeline

The chat backend's conversations are scored for topic, quality and disposition by an LLM-based
analyzer (`analyze_conversations`) whose results are cached in SQLite, refreshed nightly in the
cluster, surfaced to admins in the frontend, and backed up for free alongside the conversations
themselves. The pieces span three repos:

| Piece | Repo | Where |
|-------|------|-------|
| Analyzer + SQLite cache + shared time-series aggregation + admin API | `../genetics-mcp-server` | `scripts/analyze_conversations.py`, `scripts/analysis_timeseries.py`, `db/chat_history_db.py`, `routers/admin.py` |
| Nightly CronJob (this repo) | `genetics-results-suite` | `k8s/deployments/analyze-conversations-cronjob.yaml` |
| Admin UI (Conversations columns + Quality plots) | `../genetics-results-browser` | `src/features/admin/AdminPage.tsx`, `adminApi.ts` |

### SQLite analysis cache

Analysis results persist in two tables inside the same `chat_history.db` SQLite database that
holds the conversations (`conversation_analysis`, keyed by `session_id`, plus a normalized
`conversation_issue` table so issue-category filtering and plots are plain SQL). Storing the
cache in the live DB rather than flat files means it is **persistent and backed up for free**:
`chat_history.db` lives on the `chat-data` PVC, which the existing daily GCE disk snapshot
covers (`terraform/backups.tf`, `google_compute_resource_policy.chat_data_snapshots`, 03:00
daily, `snapshot_retention_days` retention). A restore of that snapshot therefore brings back
the conversations **and** their analysis together, since they share one database file. The
analyzer keeps its write transactions short so the nightly run does not block live chat writes.

The human-readable analysis report stays a flat `.md` file; `metrics.json` and the PNG plots
(`plot_conversation_scores.py`) are local-dev outputs only.

### Nightly CronJob

`k8s/deployments/analyze-conversations-cronjob.yaml` is a CronJob (namespace `genetics`,
schedule `30 2 * * *` — 02:30 daily, a low-traffic window, and just before the 03:00 disk
snapshot so each night's snapshot captures that night's fresh analysis) that keeps every
conversation's analysis up to date. It reuses the existing chat-backend image
(`${REGISTRY}/genetics-mcp-server:latest`) and runs
`python -m genetics_mcp_server.scripts.analyze_conversations --db /data/chat_history.db`.
The analyzer is staleness-based: it only (re)analyzes sessions that are missing, have new
messages (continued conversations, `updated_at > analyzed_at`), or were analyzed by an older
`ANALYZER_VERSION` — unchanged conversations are skipped, so the nightly run only spends LLM
budget on what actually changed. Results are written into the `conversation_analysis`/
`conversation_issue` tables in `chat_history.db`.

- **Storage / RWO co-scheduling**: the job mounts the same `chat-data` PVC at `/data` as
  chat-backend so it reads/writes the same `chat_history.db`. Because that PVC is `ReadWriteOnce`,
  the CronJob uses a `podAffinity requiredDuringSchedulingIgnoredDuringExecution` rule
  (`labelSelector app: chat-backend`, `topologyKey kubernetes.io/hostname`) to co-schedule on the
  same node as chat-backend so it can attach the volume.
- **Concurrency / scheduling**: `concurrencyPolicy: Forbid` (never overlap nightly runs),
  `startingDeadlineSeconds: 3600`, `restartPolicy: OnFailure`, `backoffLimit: 1`,
  `successfulJobsHistoryLimit/failedJobsHistoryLimit: 3`. Resources are modest (the work is
  LLM-bound, mostly waiting on the Anthropic API).
- **Secret**: `ANTHROPIC_API_KEY` from `genetics-secrets` (key `anthropic-api-key`), the same
  secret/key chat-backend uses.
- **Deploy**: picked up automatically by `deploy.sh`'s `deployments/*.yaml` loop (`REGISTRY`
  envsubst + `:latest`→tag rewrite); no script change needed.
- **Manual force-reanalyze** (rerun everything from scratch, e.g. after bumping `ANALYZER_VERSION`):
  `kubectl -n genetics create job analyze-conversations-force-$(date +%Y%m%d) --from=cronjob/analyze-conversations -- python -m genetics_mcp_server.scripts.analyze_conversations --db /data/chat_history.db --force`

### Local / dev usage

The analyzer still runs standalone for development (in `../genetics-mcp-server`):
`python -m genetics_mcp_server.scripts.analyze_conversations --db <path>/chat_history.db --output-dir <dir>`.
`--db` points at any copy of the SQLite DB and `--output-dir` collects the local-dev artifacts
(`metrics.json` and, via `plot_conversation_scores.py`, the PNG plots); the analysis report
`.md` is also written there. The same SQLite cache and staleness rules apply locally, so a dev
run only re-analyzes changed sessions unless `--force` is passed. Bumping the `ANALYZER_VERSION`
constant in `analyze_conversations.py` invalidates all cached rows so the next run (local or the
nightly CronJob, with `--force`) re-scores everything.

### Admin UI additions

The admin page in `../genetics-results-browser` (`src/features/admin/AdminPage.tsx`) consumes
the analysis through the chat backend's admin API:

- **Conversations tab** gains 4 analysis-derived columns, each server-side filterable
  (consistent with the existing user/date filters): disposition, issue count (with a tooltip
  listing the issue categories), LLM rating (1-5 or `NA` for unrated), and a
  successful / neutral / unsuccessful icon.
- **Quality plots tab** (added after Feedback) renders 4 interactive Chart.js line charts —
  per-score share, rolling mean + volume, disposition mix, and issue-category mix — that mirror
  the PNG plots. Hovering a line highlights it and dims the others so a single issue category or
  disposition can be followed over time.

These are fed by the chat backend's admin router (`../genetics-mcp-server/routers/admin.py`):
the `/chat/v1/admin/sessions` list LEFT JOINs the analysis fields and accepts the new filter
params (disposition, `min_issues`, success label, rating including `NA`=unrated), and a new
`GET /chat/v1/admin/analytics/quality` endpoint returns raw per-conversation rows that the
frontend aggregates client-side using the same rolling-window logic as `analysis_timeseries.py`,
so the JS and PNG plots cannot drift.

### Backup / restore

No new backup infrastructure is needed: because the analysis tables live in `chat_history.db`
on the `chat-data` PVC, the existing daily disk snapshot (`terraform/backups.tf`) already covers
them. Restoring a `chat-data` snapshot restores the conversations and their cached analysis
together. If analysis was lost or wiped without restoring the disk, re-running the analyzer
(nightly or the manual force job above) regenerates it from the conversations.
