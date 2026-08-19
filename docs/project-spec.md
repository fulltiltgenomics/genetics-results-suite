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
  ├── keycloak-postgres (Keycloak DB, port 5432)     — backed up daily to GCS
  └── sandbox           (code execution, port 8080)  — reachable from chat-backend ONLY; egress
                                                       limited to db-api + results-api; not applied
                                                       unless ENABLE_SANDBOX=true
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
| sandbox | sandbox/ (local build context; SDK from genetics-mcp-server) | 8080 | Code-execution sandbox for model-authored Python. gVisor node pool, dedicated KSA, one `emptyDir` and no other mount. Applied only when `ENABLE_SANDBOX=true`, which `scripts/deploy.sh` derives from `sandbox_pool_enabled` in `terraform.tfvars` (default false); see "The sandbox Deployment" below |
| rag-service | genetics-rag-service | 8000 | RAG document retrieval (internal only) |
| monitor | — (scripts/monitor/) | — | CronJob: health checks, BQ summary, log alerts → Slack |
| analyze-conversations | genetics-mcp-server (same image) | — | Nightly CronJob: LLM scoring of chat conversations (see below) |
| keycloak-postgres-backup | — (upstream `google/cloud-sdk`) | — | Daily CronJob: `pg_dump` of the Keycloak DB to GCS (only when the broker is enabled) |

### results-api deployment tuning

`k8s/deployments/results-api.yaml` sets two env vars that bound the resources its
range-query path (in-process bgzf/tabix reads over GCS) consumes; both have safe
in-code defaults, so they are tuning knobs, not requirements. The sandbox
per-execution limits below are declared the same way, for the same reason:

| Env var | Value (manifest) | Default in code | Purpose |
|---------|------------------|-----------------|---------|
| `TABIX_FILTER_WORKERS` | `2` | `min(4, cpu-1)` | Size of the decompress/filter `ProcessPoolExecutor`. The default reads `os.cpu_count()` (host cores, **not** the cgroup CPU quota), so on a large node an unbounded count would spawn many idle, FD-holding workers. Set to match the container's CPU limit (currently `2`). |
| `GCS_MAX_CONNECTIONS` | `128` | `128` | Process-wide cap on concurrent GCS range-fetch sockets. A single all-resources variant batch fans out across ~12-15 data files; without a cap the simultaneously-open sockets exhausted the file-descriptor limit ("Too many open files"). Lower if the pod's `NOFILE` limit is tight, raise for more fetch parallelism. |

The container entrypoint (`genetics-results-api`'s `start.sh`) also raises `ulimit -n`
to 65536 as defense-in-depth. Keep `TABIX_FILTER_WORKERS` in step with the deployment's
CPU `limits` if you change them.

The manifest additionally declares the five sandbox per-execution limits
(`app/core/sandbox_budget.py`), all at their in-code defaults, so an operator can tune them
without a rebuild — they were code-default-only at first, which made "env-configurable" true
of the code and false in practice. `SANDBOX_MAX_RESPONSE_BYTES` (the 16 MiB per-response cap,
`app/core/limits.py`) is **not** declared and keeps its in-code default.

`ANONYMOUS_SURFACE_MINIMAL` (`"true"` in the manifest, and the in-code default) is declared
there for the same reason but is **not** a tuning knob: it decides whether `/healthz` is the
only route results-api will answer with no credential at all, and its unset value is on, so a
typo or a missing variable fails safe. It is separate from `SANDBOX_ENABLED`
(`genetics-results-suite-rhh`) because that one is the incident lever — while the anonymous
surface keyed on it, `SANDBOX_ENABLED=false` to kill the sandbox also re-opened six routes to
anonymous callers. `SANDBOX_ENABLED=true` forces the minimal surface regardless of this value.

| Env var | Value (manifest) | Default in code | Purpose |
|---------|------------------|-----------------|---------|
| `SANDBOX_AGGREGATE_RESPONSE_BYTES_BUDGET` | `1073741824` | 1 GiB | Response bytes one execution (`jti`) may be sent in total, charged from what actually went on the wire, every status included. |
| `SANDBOX_MAX_REQUESTS_PER_EXECUTION` | `1000` | 1000 | Requests one execution may issue. Bounds a loop of *small* responses, which the byte budget does not. |
| `SANDBOX_MAX_CONCURRENT_REQUESTS` | `4` | 4 | In-flight requests per execution. The one limit with a **memory** failure mode: 4 × 16 MiB of buffered bodies. |
| `SANDBOX_MAX_CONCURRENT_REQUESTS_TOTAL` | `8` | 8 | In-flight sandbox requests pod-wide. Must be ≥ the per-execution value or the pod refuses to start. |
| `SANDBOX_MAX_TRACKED_EXECUTIONS` | `4096` | 4096 | Bound on the counter map itself. At the bound a *new* execution is refused, never a running one evicted. |

Each is a ceiling compared with `>=`, so a value below 1 would silently mean "reject every
sandbox request"; results-api raises at import instead, and likewise when
`SANDBOX_MAX_CONCURRENT_REQUESTS_TOTAL < SANDBOX_MAX_CONCURRENT_REQUESTS`. Raising any of the
concurrency values raises peak buffered response memory against the pod's 8Gi limit.

### chat-backend shutdown and stream draining

A chat turn is a single long-lived SSE response — with tool-calling loops it routinely runs
1-3 minutes. Under the default 30s termination grace period, any pod deletion (deploy, eviction,
drain) SIGKILLed uvicorn mid-stream and truncated the answer on screen. `chat-backend.yaml`
therefore sets three related knobs:

| Setting | Value | Purpose |
|---------|-------|---------|
| `terminationGracePeriodSeconds` (pod spec) | `300` | Window for the in-flight SSE response to finish before SIGKILL. |
| `lifecycle.preStop.sleep` | `10` seconds | Leaves the Service endpoints before uvicorn stops accepting, so a request arriving as SIGTERM lands isn't met with connection refused. Uses the native `sleep` hook (GA since Kubernetes 1.30), so it needs no shell in an image that drops `ALL` capabilities. |
| `--timeout-graceful-shutdown` (uvicorn arg) | `280` | Uvicorn's graceful shutdown is unbounded by default and would sit out the whole grace period, then take SIGKILL. Exiting inside the window closes the `chat_history.db` / `llm_config.db` handles on the `chat-data` PVC cleanly. |

Keep the uvicorn timeout below `terminationGracePeriodSeconds`, with room for the preStop sleep.

The cost is deploy latency: chat-backend is `strategy: Recreate`, so `deploy.sh` blocks on the
old pod for up to ~5 minutes when someone is mid-conversation. That stays inside the
deployment's `progressDeadlineSeconds` (600). This makes shutdown graceful; it does not make it
resumable — a stream cut short by a hard node failure is still lost, since there is no
client-side reconnect and no persistence of partial assistant turns.

## Project structure

```
├── benchmarks/               # inputs for the paired A/B replay benchmark (the harness itself
│   │                         #   is genetics-mcp-server's replay_benchmark.py)
│   ├── eval_dataset_local.json  # hand-authored question set — NOT a production replay; see
│   │                         #   the README for what that costs the comparison
│   └── README.md             # provenance, the two deliberate exclusions, the unfilled
│                             #   script-failure pre-registration, and the run procedure
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
│   ├── disruption-budgets/   # PodDisruptionBudgets (chat-backend, results-api)
│   ├── network-policies/     # network isolation rules
│   └── volumes/              # persistent volume claims
├── keycloak/                 # Keycloak image build context (official image + Apple IdP and
│                             #   email-allowlist extension JARs), realm/client/IdP templates,
│                             #   and the `genetics` login theme
├── sandbox/                  # sandbox image build context (distroless, no shell, uid 65532)
│                             #   for model-authored Python; SDK pip-installed from
│                             #   genetics-mcp-server at build time and importable as
│                             #   `genetics` (genetics_alias.py). See
│                             #   docs/code-execution-security.md
├── scripts/
│   ├── build-all.sh          # build and push all Docker images
│   ├── build.sh              # build and push one service's image
│   ├── create-secrets.sh     # create k8s secrets from env vars
│   ├── deploy.sh             # full deploy (terraform + k8s)
│   ├── rollout.sh            # single-service image update
│   ├── sync-datasets.sh      # copy datasets.yaml to sibling service repos for local dev
│   ├── test-e2e-local.py     # end-to-end run_analysis verification against the live local
│                             #   stack (4h6.49). Needs dev-stack.sh + run-sandbox-local.sh up
│   ├── dev-stack.sh          # start/stop the five local dev servers from one tree
│                             #   (main checkouts or worktrees) against one dataset.
│                             #   See docs/local-dev-vm.md
│   ├── bq-dev-dataset.sh     # stand up / verify / tear down the BigQuery rehearsal
│                             #   dataset. See docs/bigquery-dev-dataset.md
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
    ├── chat-tool-reference.md # verbatim transcription of what the LLM receives: every tool
    │                         #   name/description/schema, the TOOL_PROFILES membership, the
    │                         #   system prompt, verbosity fragments and instruction envelope,
    │                         #   and the chat surface vs the /mcp surface
    ├── datasets-yaml-schema.md  # schema reference for shared datasets.yaml config
    ├── code-execution-security.md # threat model and security design of record for the
    │                         #   model-authored-code sandbox (isolation, egress, credentials,
    │                         #   MCP exposure, residual risk)
    ├── keycloak-apple-signin.md # Keycloak broker setup, MCP OAuth clients, backup/restore
    ├── genegenie-migration.md   # record of the finngenie → genegenie legacy-hostname redirect
    ├── local-dev-vm.md       # running the whole suite from source on a GCE VM (no docker/k8s)
    ├── nginx-setup.md        # notes for the legacy VM nginx setup
    └── project-spec.md       # this file
```

## Shared Dataset Configuration

`configs/datasets.yaml` is the single source of truth for dataset and resource definitions consumed by both results-api and db-api. At deploy time, `deploy.sh` creates a Kubernetes ConfigMap (`datasets-config`) from this file and volume-mounts it into both service pods at `/app/configs/datasets.yaml`. Each service reads the path from the `DATASETS_CONFIG_PATH` environment variable.

For local development, `scripts/sync-datasets.sh` copies the canonical file to sibling service repos so they can run standalone. Each service's YAML loader defaults to `./configs/datasets.yaml` when `DATASETS_CONFIG_PATH` is not set. The sibling copies are **generated and untracked** — `configs/datasets.yaml` is gitignored in both `genetics-results-api` and `genetics-results-db`, and committed only here. Two consequences: a fresh sibling clone has no `configs/datasets.yaml` at all and the service aborts at startup until `sync-datasets.sh` (or an explicit `DATASETS_CONFIG_PATH`) provides one; and because the copies are untracked there is nothing to diff, so no check detects a stale sibling copy — divergence shows up only as a service reading yesterday's config. `deploy.sh` also runs `sync-datasets.sh` (best-effort) before building the ConfigMap. The script resolves the siblings from the **git common dir** (`git rev-parse --git-common-dir`, absolutised, twice up), which is the main checkout's `.git` even from a worktree — so a run from `<repo>/.claude/worktrees/<name>` syncs the same `~/suite/genetics-results-{db,api}` a main-checkout run would, and a same-named directory sitting next to the worktree is reported as ignored rather than written to (genetics-results-suite-e47; it previously derived `SUITE_DIR` as the parent of `scripts/`, warned, and **exited 0 having copied nothing**). Failure modes are split deliberately: a sibling that is simply not checked out prints `SKIP:` and exits 0, while an unresolvable sibling root, or a resolved directory whose `pyproject.toml` does not name that repo, prints `ERROR:` and exits 1. Nonzero is loud without breaking a deploy — `deploy.sh` calls it `|| echo WARN ... (continuing)`. `SUITE_SIBLING_ROOT` overrides the resolution for layouts the `.git`-next-to-the-root assumption does not fit. Only the ConfigMap built from the canonical file governs what the deployed pods read; the sibling copies matter for local runs only. Note that this only keeps `datasets.yaml` in sync — the results-api product configs (`app/config/profiles/*/credible_sets.py`, `summary_stats.py`, `common.py`, etc., which hold the actual GCS file paths and the `dataset_to_resource` map) live only in genetics-results-api and are baked into its image at build time, so changes there require rebuilding and rolling out the results-api image.

Both services load all dataset/resource metadata exclusively from the YAML -- there are no hardcoded fallback dicts. In genetics-results-api, the profile `datasets.py` files are empty placeholders (datasets come from YAML via `app.config.yaml_loader`). The `dataset_to_resource` mapping in `profiles/*/common.py` is still hardcoded as the YAML schema does not yet support exact BQ dataset name to (resource, version) tuples.

The YAML defines two exome dataset resources with different filtering levels: `genebass_exome` (filtered to p < 1e-4) and `ibd_exome` (resource: `ibd_exome_2026`, containing only exome-wide significant variants at p < 3e-7 plus LD-curated variants at p < 5e-6, with 3 phenotypes: IBD, UC, CD). The `dataset_to_resource_rules` map `IBD_exome_2026` to `ibd_exome_2026` for `exome_variant_results_v` and `gene_burden_results_v` views.

Summary statistics are served by results-api from per-phenotype tabix files two ways: `/api/v1/summary_stats/{resource}/{data_type}` (GET/POST) for named variants, and `/api/v1/summary_stats_by_region/{resource}/{data_type}/{region}` for every record in a `chr:start-end` region. Both require `phenotypes=<comma-separated>` — there is no combined file spanning a region across traits, unlike `credible_sets_by_region`. The genetics-mcp-server exposes both as `get_summary_stats` and `get_summary_stats_by_region`.

Every results-api endpoint except the variant-set ones is now reachable from an MCP tool. The tools closing the last gaps are `get_credible_sets_by_region`, `get_credible_set_leads_by_phenotype`, `get_exome_results_by_variant`, `get_exome_results_by_region`, `get_colocalization_by_credible_set`, `get_peak_to_genes` / `get_gene_to_peaks` (Open4Gene peak-to-gene links, the caQTL peak → target gene bridge), `get_open_chromatin_by_peak`, `get_summary_stats_by_region`, `get_hla_by_phenotype`, `get_resource_metadata` and `get_dataset_display_names`. Region-shaped tools cap inline rows at 500 and set `truncated`, leaving the full result behind the download URL. (`get_hla_by_allele` has no results-api counterpart by design — it is BigQuery-only, see "Classical HLA allele associations" below.)

ASM-QTL (allele-specific methylation QTL) data from deCODE is served via both BigQuery (`asm_qtl` table / `asm_qtl_v` view) and the standard sumstats endpoint (`/summary_stats/decode/asmqtl`). Two datasets: `decode_asmqtl_cpg` (CpG methylation, phenotype code `CpG`) and `decode_asmqtl_mds` (MDS methylation, phenotype code `MDS`), both under the `decode` resource. The `dataset_to_resource_rules` map `deCODE%` to `decode` for `asm_qtl_v`.

caQTL results are keyed by **peak**, not gene: `finngen_caqtl` rows in `credible_sets_v` carry a peak id (e.g. `chr5-35482826-35484273`) in the `trait` column, so a gene-based caQTL question is only answerable by joining through the Open4Gene peak-to-gene link table (`peak_to_gene_v`, the BQ face of the `finngen_chromatin_peaks` dataset) on `peak_id = trait` and `cell_type = cell_type`. The `tables.peak_to_gene_v` block in `datasets.yaml` carries the column descriptions, worked join examples, and an explicit warning against approximating the link by coordinates (linked peaks sit up to ~1 Mb away and most nearby peaks are not linked) — without it agents fell back to hand-written coordinate-window SQL that silently answered a different question. The `FinnGen%` resource rule is scoped to include `peak_to_gene_v` since the table is FinnGen ATAC-seq only.

MPRA (Siraj et al. 2026) is a new functional-annotation product — measured intrinsic cis-regulatory allelic activity from a massively parallel reporter assay (221K fine-mapped + 86K control variants tested in 5 cell lines: K562, HepG2, SK-N-SH, HCT116, A549), served for both profiles. Like open_chromatin/variant_effect it is a new vertical rather than a plain dataset: one dataset `siraj_mpra` under resource `siraj_mpra` (`dataset_to_resource_rules` map `siraj_mpra%` -> `siraj_mpra`), `data_type: mpra`, `trait_type: null`. The source WIDE per-variant TSV is munged to LONG (one row per variant × `cell_line`, where `cell_line` ∈ {`meta`, K562, HEPG2, SKNSH, HCT116, A549}) carrying the emVar/active/log2Skew/log2FC calls, bgzip+tabix-indexed on GCS. Served two ways: results-api tabix range endpoints (`/api/v1/mpra` by-variant / by-region / by-gene, its own tabix vertical) and BigQuery (`mpra` base table / `mpra_v` view, which adds `resource`). The genetics-mcp-server exposes `get_mpra_by_variant`, `get_mpra_by_region`, and `get_mpra_by_gene`. Scientifically it is the functional-validation layer for the regulatory-buffering story (Kanai et al.): emVar rates and allelic-effect concordance scale with FinnGen fine-mapping PIP, and MPRA measures intrinsic reporter activity distinct from endogenous eQTL/caQTL and from in-silico variant_effect predictions.

### Classical HLA allele associations

FinnGen R14 imputed **classical HLA allele** results are served for both profiles: 187
alleles at 4-digit (two-field) resolution across 10 genes (HLA-A, -B, -C, -DPB1, -DQA1,
-DQB1, -DRB1, -DRB3, -DRB4, -DRB5), tested with REGENIE against 2,712 core R14 endpoints —
~507k (phenotype, allele) rows in all. One dataset `finngen_hla` under the existing
`finngen` resource, `data_type: hla`, `trait_type: mixed`.

**Why it is a separate data type rather than more GWAS.** The association unit is an
allele, not a nucleotide variant. The source encodes the dosage test as `ref='<absent>'` /
`alt='A*02:01'`, which is faithful to the test run (presence of the allele vs its absence)
but reads as a variant everywhere downstream; the munge rewrites it into explicit
`gene`/`allele` columns. The consequence is deliberate: these rows have **no
chr:pos:ref:alt identity**, so they do not join to credible sets or variant annotations,
and no by-variant path can return one. Every allele of a gene sits at that gene's single
anchor position (HLA-DRB3/4/5 share a placeholder anchor and cannot be separated
positionally). This is why the MHC is worth serving at all: LD across chr6:29-33Mb makes
SNP-level results there effectively uninterpretable, and the classical allele is what the
literature and the clinic use.

Two fields need care and are documented everywhere they surface: `pval` **underflows to 0**
for the strongest signals (coeliac `DQB1*02:01` is mlog10p 1596), so ranking must use
`mlog10p`; and each row carries the allele's imputation `info`, because rare alleles imputed
below ~0.5 produce enormous unstable betas that read as spectacular findings but are
artifacts.

Both serving paths spell the statistics the same way — `mlog10p`, `se`, `af`, `af_cases`,
`af_controls` — so per-column access is uniform and no renaming is ever needed. The column
*sets* still differ, and a script that concatenates the two directions has to account for
it: the by-allele path (`hla_associations_v`, via the MCP SDK) returns the 11 columns common
to both — `phenotype`, `gene`, `allele`, `mlog10p`, `pval`, `beta`, `se`, `af`, `af_cases`,
`af_controls`, `info` — while the by-phenotype path (results-api `_HLA_HEADER_SCHEMA`)
returns those plus `resource`, `version`, `chr` and `pos`, 15 in all. A bare `pl.concat` of
the two frames fails on width; select the shared 11 on both sides first. Both directions
now keep their column names through an **empty** result — see "SDK empty-result contract"
below; the by-phenotype one did not until `genetics-results-suite-6uk`. The staged
file and the `hla_associations` table keep FinnGen's native `mlogp`/`sebeta`/`af_alt`/
`af_alt_cases`/`af_alt_controls`; `hla_associations_v` renames them 1:1 to the house
spelling that results-api's `_HLA_HEADER_SCHEMA` already used. See
[HLA column rename rollout](#hla-column-rename-rollout-hla_associations_v) for the ordering
constraint that rename imposes.

Served two ways, because the data has two query axes and neither storage layout serves both:

- **results-api tabix** (per-phenotype files, its own `/hla` router but the existing
  `SumstatsDataAccess` read path — the files are registered in the profile `summary_stats`
  config under `data_type: hla`): `GET /api/v1/hla/{resource}?phenotypes=…[&genes=…]`
  returns a trait's whole HLA profile, and `GET /api/v1/hla/genes` returns the locus
  registry. Only the merge ordering is new (`SORT_CONFIG_HLA` on chr/pos/allele, selected
  per data_type, since the files have no ref/alt to sort on); `genes` is served by discrete
  point reads at the selected anchors rather than the span between them.
- **BigQuery** (`hla_associations` table / `hla_associations_v` view, added to the db-api
  `VIEWS` allowlist and the monitor's view list): the only place the cross-phenotype
  question is answerable — "which traits is `B*27:05` associated with?" spans all 2,712
  per-phenotype files. The view maps `dataset` to `resource = 'finngen'` explicitly, since
  the lowercase fallback would give `finngen_hla`.

The agent reaches both through `get_hla_by_phenotype` and `get_hla_by_allele`.

Data is produced by `genetics-results-munge`'s `scripts/munge_hla.{py,sh}` and staged to
`gs://finngen-commons/results_api_data/hla/finngen_hla/` and
`gs://daly-genetics-results/hla/finngen_hla/` (per-phenotype tabix files under
`summary_stats/`, plus one combined `finngen_hla.tsv.gz` for the BigQuery load, which
`genetics-results-db`'s `scripts/load_hla.sh` reads). The munge drops the ~96 extra `_WIDE`
endpoints the source ships that have no entry in the R14 phenotype metadata — without
metadata they would appear as traits with no name, case count or category — and 4 core
endpoints have no HLA run. R10-R13 ship the same directory; only R14 is carried, since
R12/R13 HLA would duplicate the rows for a release the suite no longer serves elsewhere.
Decisions are recorded in `genetics-results-munge/docs/hla-allele-associations.md`.

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

### Phenotype and dataset metadata in BigQuery

Trait/phenotype metadata and the dataset registry also exist as BigQuery tables, `phenotypes`
and `datasets`, exposed as `phenotypes_v` / `datasets_v` (both in the db-api `VIEWS`
allowlist) and documented for agents by their `tables.*` blocks in `configs/datasets.yaml`.
Before this, the only way to turn `AB1_ACTINOMYCOSIS` into "Actinomycosis", or to filter
traits by case count or ICD chapter, was a results-api round trip — which is why a phenotype
question cost a search call, a lookup call and then the real query. It is now a JOIN inside
the same query.

- `phenotypes` — one row per `(dataset, trait_original)`, 35,327 rows. **Join on
  `trait_original`, never on `trait`**: in every results view that has both columns
  `trait_original` is the phenotype code and `trait` is a display form for most rows (`HEIGHT_IRN` vs
  `Height,_inverse-rank_normalized`; `continuous_30040_both_sexes__irnt` vs `Mean corpuscular
  volume`), so joining on `trait` returns zero rows silently. `hla_associations_v` is the
  exception with neither column: it spells its phenocode `phenotype`, so its join is
  `p.dataset = h.dataset AND p.trait_original = h.phenotype`. Coverage is partial by design —
  QTL datasets have no rows because their traits are genes, proteins and peaks (resolved via
  `gene_annotations_v` and `peak_to_gene_v`), and datasets whose codes are already readable
  (PGC, GP2, BipEx2, SCHEMA2, IBD_exome) have none either. Use a `LEFT JOIN` when the dataset
  is not known in advance.
- `datasets` — one row per results-view `dataset` value (~890, of which 841 are eQTL
  Catalogue QTD sub-studies), unique on `dataset` so the join never fans results out, plus a
  `dataset IS NULL` row for each registry entry with no BigQuery presence. Carries
  `pseudo_credible_sets`, which must be checked before `pip`/`cs_size` are interpreted.

**Ranked fuzzy phenotype search stays on results-api.** BigQuery cannot replace an in-memory
ranked index, and search is the entry point of most conversations; these tables serve exact
resolution and SQL-expressible filtering only.

Both are rebuilt in full by `genetics-results-db`'s `scripts/load_phenotypes.sh` from
`configs/datasets.yaml` and the `metadata_file` sources it references — see
[adding-datasets.md](adding-datasets.md) for the registry-key → `dataset`-value map that the
builder owns and that a new dataset must be added to.

## Authentication

- **Keycloak** is the identity broker, **enabled per deployment profile** (`ENABLE_KEYCLOAK` in `deploy.sh`, defaulting on for `daly`, off for `finngen`). When enabled it presents the provider chooser and federates **Google** and **Apple** (Sign in with Apple), exposing one OIDC issuer at `https://${KEYCLOAK_HOST}/realms/genetics`. It runs in-cluster (`k8s/deployments/keycloak.yaml`) behind the auth-gateway under the **`/auth` path on the primary domain** (`KEYCLOAK_HOST` defaults to `<domain>/auth`), so it reuses the existing DNS record, managed cert and ingress rather than needing an `auth.<domain>` subdomain; it is backed by an in-cluster Postgres (`keycloak-postgres`) with daily `pg_dump` backups to GCS. The image (`keycloak/`) is the official Keycloak plus a bundled Apple identity-provider extension. Setup, Apple Developer prerequisites, secret rotation and restore are documented in `docs/keycloak-apple-signin.md`.
- **oauth2-proxy** handles browser sessions. Its provider is profile-driven (`OAUTH2_PROXY_PROVIDER`): `oidc` against Keycloak where the broker is enabled (daly), or `google` directly otherwise (finngen). Either way it authorizes against an allow-list: one or more **domains** (`OAUTH2_PROXY_EMAIL_DOMAINS`, comma-separated) **or** specific **addresses** (the `--authenticated-emails-file`). Both lists come from terraform — `oauth_email_domain` (comma-separated, e.g. `broadinstitute.org,finngen.fi`) and `oauth_allowed_emails` (specific addresses, e.g. Apple users on `me.com`/`icloud.com`/`privaterelay.appleid.com`).
- **auth-gateway** (nginx) uses `auth_request` to validate requests against oauth2-proxy before proxying; a `location /auth/` block (injected by `deploy.sh` via `${KEYCLOAK_SERVER}`, and served without the `auth_request` since it *is* the auth endpoint) strips the prefix and proxies to Keycloak. The email returned by oauth2-proxy is passed to backends in the `X-Goog-Authenticated-User-Email` header (the `accounts.google.com:` prefix is legacy and provider-agnostic — backends read only the address after the colon).
- **results-api** also accepts `Authorization: Bearer` tokens (Google Identity Tokens or internal shared secret)
- **`X-Goog-Authenticated-User-Email` is not a credential on its own.** It is trivially settable by anything with network reach, so results-api trusts it only when the request *also* carries `Authorization: Bearer $INTERNAL_API_SECRET` — the trusted-proxy marker. bff attaches it on both of its upstream paths: `bff/upstream.ts` for the three assembled variants routes, and `bff/passthrough.ts` for everything else under `/api`, which is where the browser's identity header is forwarded, so the header and the marker now always travel together on that hop. **The passthrough half is un-deployed** — it exists only in genetics-results-browser's `db-only-architecture` worktree, and the BFF running in the cluster attaches nothing on that path (measured: a header-less request through the deployed BFF gets 200 from `/api/v1/auth`). That is what makes `bff` the first of the three services in the rollout order below. (The passthrough never overwrites an `Authorization` the caller already sent; in practice nginx diverts anything carrying one to `@api_bearer` before it reaches bff, and that location blanks the identity header before proxying.) Without the marker the header is ignored outright (fail closed → 401), and the asserted address is additionally held to the same `ALLOWED_EMAILS`/`ALLOWED_EMAIL_DOMAINS` allow-list as the Google-JWT path. The same rule — marker **and** allow-list — gates the `endpoint_access` usage log, so a forged header cannot mis-attribute a request and an address the auth path refuses is not logged as the requester either. The marker is only as strong as `INTERNAL_API_SECRET`, which four workloads mount (bff, chat-backend, mcp-server, results-api); it is not a boundary against those four, it is a boundary against everything else in the namespace — notably the code-execution sandbox, which has egress to results-api but no secret.
- **Identity precedence in results-api** (`app/core/auth.py:get_verified_user`), in the order it is evaluated. The asserted identity is checked *before* the bearer is mapped to a service identity, so a browser request keeps naming its real user rather than collapsing to the generic `mcp-tool`:
  1. marker **+** allow-listed identity header → **that email** (browser traffic via bff)
  2. marker **+** identity header that is *not* allow-listed → **401**, deliberately *not* downgraded to `mcp-tool` — a downgrade would let anything holding the shared secret launder a refused identity into a working request, i.e. the weaker credential would silently rescue what the stronger claim just failed
  3. marker alone, no identity header → **`mcp-tool`** (auth-gateway `@api_bearer`, chat-backend, mcp-server)
  4. Google Identity Token or user API token → that identity (unchanged)
  5. identity header alone, no marker → **401** — the hole this closes

  An empty oauth2-proxy `$email` does **not** land in case 3. nginx drops a header whose value is the empty string (that is how `@api_bearer` blanks it), so `proxy_set_header X-Goog-Authenticated-User-Email "accounts.google.com:$email"` cannot emit an empty value — it emits the bare prefix `accounts.google.com:`, which is truthy and therefore asserts the empty address: case 2, **401**, not a downgrade to `mcp-tool`. It is unreachable in production because oauth2-proxy cannot return 200 for `/oauth2/auth` without an email (its own domain check needs one).
- **Allow-list comparison** is case-insensitive on both sides (oauth2-proxy lower-cases the address before its own domain check, so a mixed-case `User@FinnGen.fi` it admits must not be rejected downstream), and a literal `*` in `ALLOWED_EMAIL_DOMAINS` means "any domain", matching oauth2-proxy's reading of the same value — without that, setting `oauth_email_domain = "*"` would authenticate everyone at the proxy and reject everyone at results-api. `*` also opens the Google-JWT path to any verified Google account, leaving `GOOGLE_TOKEN_AUDIENCE` as the only narrowing; it is a deliberate "open deployment" switch, not a default. Because matching tolerates case and surrounding whitespace, the resolved identity is **returned trimmed and lower-cased** by both `get_authenticated_user` and the usage-log extractor, so `User@FinnGen.fi` and `" user@finngen.fi "` attribute to one identity in `endpoint_access` rather than three.
- **mcp-server** is not behind oauth2-proxy; it accepts `Authorization: Bearer` tokens via **four** paths: the `MCP_API_KEY` shared secret(s); Google Identity Tokens (JWT validated against `email_verified` plus the configured email/domain allow-list); per-user API tokens issued via the chat API; and **Keycloak OAuth 2.1 access tokens** (see below). NOTE: the Google JWT path validates **Google** Identity Tokens only, so an Apple-only identity cannot use *that* (deprecated) branch. It does **not** block programmatic access: the per-user API token path is identity-provider agnostic — the browser dialog mints a key for whoever the oauth2-proxy session says you are (Keycloak/Apple included), and `/api` and `/mcp` validate it by token-store lookup, not by email domain. A generic OIDC verifier is a follow-up for the deprecated Google JWT branch only.
- **MCP OAuth (resource-server) path**: when `OAUTH_ISSUER` + `OAUTH_RESOURCE_URL` are set (daly/genegenie; empty for finngen, so the path is inert there), the mcp-server acts as an OAuth 2.1 **resource server**. It validates Keycloak-issued JWT access tokens (RS256 signature via the realm JWKS, `iss`/`aud`/`exp`, then the same email/domain allow-list), and advertises RFC 9728 discovery at `/.well-known/oauth-protected-resource` (routed unauthenticated through auth-gateway; returns `WWW-Authenticate: Bearer resource_metadata=…` on 401), so MCP clients auto-discover the Keycloak authorization server. The Keycloak issuer is **path-based** (`https://<host>/auth/realms/genetics`); tokens must carry `aud` = `OAUTH_RESOURCE_URL` (`https://<host>/mcp`), enforced per client via an audience mapper. Each external app is its own Keycloak client (registered manually — no open Dynamic Client Registration); onboard one with `scripts/keycloak-register-client.sh <clientId> <redirect-uri>…` (brainzzz is the first, via `keycloak/brainzzz-client.json.template` + `scripts/keycloak-register-brainzzz.sh`). Setup is documented in `docs/keycloak-apple-signin.md`.
- **Shared bearer-auth allow-list**: `ALLOWED_EMAILS`, `ALLOWED_EMAIL_DOMAINS` and `GOOGLE_TOKEN_AUDIENCE` (used for Google Identity Token JWT validation in results-api and mcp-server, and for chat-backend's own allow-list check on the identity header) are sourced from a single Kubernetes ConfigMap `bearer-auth-allowed` (manifest: `k8s/configs/bearer-auth-allowed.yaml`), populated from `oauth_allowed_emails`/`oauth_email_domain` plus the `GOOGLE_TOKEN_AUDIENCE` export in `deploy.sh`, consumed by all three deployments (`results-api`, `mcp-server`, `chat-backend`) via `envFrom: configMapRef` to prevent config drift
- **Google token audience**: `GOOGLE_TOKEN_AUDIENCE` is the `aud` claim a Google Identity Token must carry. `id_token.verify_oauth2_token` skips the audience check when none is supplied, so while it is unset **any** Google-signed id_token with an allow-listed email is accepted — including one minted for a different OAuth client, which that client's operator could replay here. It defaults to the gcloud CLI's **public** OAuth client id (`32555940559.apps.googleusercontent.com`) — not a client id belonging to this project — because the flow it was documented for is `gcloud auth print-identity-token` and user credentials cannot request a custom audience. **What it buys is exactly cross-OAuth-client replay protection, and nothing else**: it rejects a token addressed to another client id (ADC's `764086051850-…`, a project-owned client), but every `gcloud auth print-identity-token` on earth carries that same `aud`, so a token the same user handed to any other service documenting that flow still passes. It is not an identity gate (see "Programmatic credentials" under Security), and the email allow-list remains the whole of the access control. Add further client ids, comma-separated, if service accounts call the API with audience-scoped tokens.
- **db-api** is internal-only (NetworkPolicy) **and** requires `Authorization: Bearer $INTERNAL_API_SECRET` on every endpoint except `/health`. The NetworkPolicy is not a boundary on its own: mcp-server is permitted through it and is itself reachable from outside, so anything that could drive mcp-server could reach BigQuery behind it. That path fails open (with a startup warning) if the env var is unset, so local runs and mid-rollout clusters keep working — **the sandbox token path below does not inherit that**.
- **Sandbox execution tokens** (`genetics-results-suite-4h6.9`, design: `docs/code-execution-security.md` §4). The code-execution sandbox must never hold `INTERNAL_API_SECRET`, which authenticates the *service*, never expires, and would let a model-authored script reach both backends forever. Instead chat-backend mints a **short-lived HS256 JWT per audience per execution**, signed with a *separate* key `SANDBOX_TOKEN_SIGNING_KEY` (`genetics-secrets` key `sandbox-token-signing-key`, generated by `create-secrets.sh`) that chat-backend, db-api and results-api mount and the sandbox does not. Claims: `iss=chat-backend`, `aud` = `db-api` **or** `results-api` (so a token captured from one cannot be replayed at the other), `sub` = the authenticated user, `sid` = the chat session id (this is what makes `endpoint_access` lines attributable to a conversation), `jti` = the execution id (also the `/scratch/<id>` directory name, joining logs across chat-backend, the sandbox SDK and db-api), `iat`/`exp` 5 minutes apart, and a `scope` whose presence is required and whose value is not yet interpreted. Both validators **discriminate on the JOSE `alg` header, never on dot count** — three-segment JWTs are also what every Google Identity Token looks like, and routing on dots would 401 that entire class of results-api caller. A sandbox-shaped bearer is validated only as a sandbox token: hard 401 on failure, never a fallthrough to the shared-secret comparison (which would degrade a malformed token into "is this string equal to the secret") and never on to `verify_oauth2_token`. Reading the unverified header is safe because it only *selects* a validator — each branch pins its own algorithm and key. In db-api the branch sits **ahead of** the fail-open early return; in results-api it is a new case 0 ahead of the four `genetics-results-suite-fad` precedence cases, and reports the caller as `sandbox:<user>` so a script is never mistaken for a verified human. `SANDBOX_ENABLED` (a separate required input, true once the sandbox Deployment exists) makes both services `sys.exit(1)` rather than warn when either secret is unset — without it, a script could simply omit the `Authorization` header and be served by the fail-open branch with nothing to attribute it to. Both validators also assert the minter's invariants rather than trusting them: an **empty** `sub`, `sid` or `jti` is rejected (PyJWT's `require` catches only missing/null, and a blank one attributes the query to nobody), and `aud` must be a **string**, because PyJWT reads a list `aud` as membership and `["db-api","results-api"]` would otherwise validate at both services. Both pass `leeway=5` to `jwt.decode` for minter/verifier clock skew — the 300s ttl covers skew only in the past direction, while PyJWT ≥ 2.10 rejects `iat > now` outright — and the separate 300s `iat` age check stays exact. The principal each validator resolves is left on `request.state` (`request.state.principal` in db-api — a `SandboxPrincipal`, the string `"internal"`, or `None`; `request.state.sandbox_principal` in results-api), which is the hook the caps below key on.
- **Per-credential row and byte caps** (`genetics-results-suite-4h6.28`, design: `docs/code-execution-security.md` §4). The tight limits are the **default**, relaxed only for a *verified non-sandbox* credential — the inverse of keying them on the sandbox audience, which would let a caller widen its limits by presenting a weaker credential or none at all. **db-api**: `maximum_bytes_billed` 50 GB per query (vs the operator's `MAX_BYTES_BILLED`, 100 GB), a 25 000-row response cap (vs `MAX_ROWS`, 100 000), and an aggregate **200 GB per `jti`** enforced by a bounded in-process LRU counter — over budget is a **429, never a truncated result**. The budget spans **all four** of db-api's BigQuery paths: `/query` charges the dry run's estimate *before* the bytes are spent and reconciles afterwards, while `/schema`'s distinct-value scans, `/stats` and `/tables/{t}/sample` — none of them cached at the HTTP layer, and none with a dry run to price them — go through one shared helper that refuses to start a job once the budget is spent and charges what the job processed once it finishes, so the budget can be overshot by at most one query's `maximum_bytes_billed`. `/schema`'s scans run at the **triggering** caller's ceiling and are charged to it; previously they passed no request and so ran at the relaxed 100 GB ceiling, twice the sandbox per-query cap, for free. That does not contaminate the shared `_get_categorical_values` cache across callers, because a job over the triggering caller's ceiling fails and leaves the cache unpopulated for the next caller to retry. Charge and reconcile are in `total_bytes_processed`, not `total_bytes_billed`: a dry run reports only the former, so it is the one figure available on both sides of the correction. A query that raises between the charge and the reconcile is refunded in a `finally`, so syntax errors do not consume a budget they never spent. The relax condition here is exactly one thing, a successful `hmac.compare_digest` against `INTERNAL_API_SECRET`; the fail-open branch's `None` principal stays tight. The row cap is clamped **in the handler**: `QueryRequest.max_rows` carries a class-level `le=MAX_ROWS` evaluated once at model-definition time, so it cannot vary per credential, and tightening the module-level `MAX_ROWS` would move that bound for every caller in the process. The counter is in-process and db-api runs `replicas: 1` with no HPA, so today it is exact — **at more than one replica it would bound spend per replica, not globally**; `k8s/deployments/db-api.yaml` carries a comment on `replicas: 1` saying so, and a cross-replica budget needs shared state and is deliberately not in v1. **results-api** carries its own response-**byte** cap (16 MiB) and **no row cap**, enforced by `SandboxResponseCapMiddleware` innermost of GZip so it measures the payload the caller decodes; a capped response is buffered precisely so the answer can be a 429 rather than a truncated stream, the buffer is handed downstream without a copy, and a relaxed response is never buffered or inspected. The row cap was removed deliberately: counting rows meant `json.loads` over the whole body on the event loop — a memory amplifier only a sandbox caller could trigger, on a `replicas: 1` pod — and it never bound TSV, the default `format` of every bulk range endpoint, while the byte cap was already the binding one. Exceeding the cap now **tears the producer down** by raising out of `send`, rather than discarding chunks a generator keeps producing; that generator is GCS range reads plus the tabix filter pool on the real endpoints. Its relax condition is **broader** — *any* verified non-sandbox principal (shared secret, Google id_token, or per-user chat API token) — because auth-gateway's `@api_bearer` location routes programmatic clients straight here with their own token and deliberately no shared secret, so an hmac-only rule would put verified humans on the sandbox caps on the bulkiest endpoints in the suite. Two cases reach a handler with no principal resolved and are decided on their own terms rather than by defaulting: an `@is_public` route — re-derive the set with `grep -rn "@is_public" app/`; today **seven**: `/api/v1/rsid/variants` GET and POST, `/api/v1/variant_sets`, `/api/v1/variant_sets/{name}`, `/api/v1/auth` (the route the code registers; this doc previously called it `/auth/status`), plus `/api/v1` and `/healthz` in `app/server.py` — where `auth_required` returns before `get_verified_user`, and `REQUIRE_AUTH=false` (dev only; the shipped `results-api.yaml` sets `"true"`). Both are **relaxed**. The `@is_public` case exists only while `SANDBOX_ENABLED` is `"false"`: with the sandbox deployed the anonymous surface collapses to `/healthz` (`genetics-results-suite-0lf`, below), so six of the seven get whatever their caller's principal earns them instead. Measured, tight caps there would truncate nothing today — the largest possible public response is 888 rows / 18.6 KB (`variant_sets/FinnGen_enriched_202505`) against a 16 MiB cap. What makes that exception carry **zero security delta** is not the caps but that every public route bounds its own response **for every caller**: `POST /rsid/variants` used to read an unbounded body and answer one object per id, so a script omitting its sandbox token got a strictly looser limit than the same script presenting it — the core invariant, broken — and it now enforces `MAX_RSIDS` (5 000) uniformly, with no sandbox special case, plus a bounded body read. 5 000 comes from the GET's own ceiling: h11 caps the request line and headers at 16 KiB and the shortest id costs 4 bytes in the query string, so no working GET carries more than 4 096. The measurements are in `docs/code-execution-security.md` §4. The sandbox principal is also resolved **before** both short circuits in `app/dependencies.py:auth_required`, so a sandbox token is capped on a public route and under `REQUIRE_AUTH=false` too — necessary, but not sufficient on its own, since it only tightens the caller that chose to identify itself.
- **results-api per-execution limits** (`genetics-results-suite-4h6.29`, design: `docs/code-execution-security.md` §4). The 16 MiB cap above bounds **one** response; a script has ~120 s of wall clock and nothing bounded how many responses it asked for, at what concurrency, or how many bytes it accumulated. `app/core/sandbox_budget.py` is the analogue of db-api's `_jti_bytes` and is deliberately shaped like it — one in-process map keyed on `jti`, checked **before** the handler runs, 429 rather than truncation — with four limits, all env-configurable (db-api's are module constants; results-api payload sizes vary by dataset and format in a way BigQuery byte counts do not): aggregate response bytes per `jti` **1 GiB** (`SANDBOX_AGGREGATE_RESPONSE_BYTES_BUDGET`), requests per `jti` **1000** (`SANDBOX_MAX_REQUESTS_PER_EXECUTION`), concurrent requests per `jti` **4** (`SANDBOX_MAX_CONCURRENT_REQUESTS`) and pod-wide **8** (`SANDBOX_MAX_CONCURRENT_REQUESTS_TOTAL`). Concurrency is the one with a **memory** failure mode rather than a cost one — each in-flight capped request buffers up to 16 MiB on a `replicas: 1` pod that preloads the gene maps and the search index — which is why it exists at all and why the pod-wide bound is there even though the sandbox's own `concurrency: 1` makes it unreachable today. All five are declared at their defaults in `k8s/deployments/results-api.yaml` (table under "results-api deployment tuning"), and each is validated at import: below 1 turns a `>=` ceiling into "reject everything", and `..._TOTAL` below the per-execution value makes the per-execution number a lie, so both refuse to start. Admitted and released inside `SandboxResponseCapMiddleware`, whose `finally` the ASGI contract puts after the last byte of the response, a `StreamingResponse` included; the middleware verifies the bearer itself, non-raising, because `request.state.sandbox_principal` is set later by `auth_required` and a request-count bound has to be admitted before the handler runs. The reason the release cannot move to a dependency teardown is **not** that a streaming generator outlives it — measured on FastAPI 0.136.1 a `yield` dependency's exit code runs *after* the response body, so for a matched route the two are indistinguishable — but that `admit` runs for every request while a dependency is solved only for a **matched route**: an unmatched path 404s out of the router with no dependency entered, stranding the slot permanently, since `_sweep_locked` will not evict an entry with `in_flight > 0`. Bytes are charged from what was **sent**, taken from the cap middleware's own buffer, so the two cannot diverge or double-count. **Every status is buffered, capped and charged, not only 2xx**: "an error body is small" was false, because FastAPI's 422 handler echoes the offending input (measured: a 100 000-char query param produced a 100 144-byte body, and a 200 014-byte body was delivered under a 500-byte cap uncharged), which made the real egress bound the request count × whatever fits in a URI. An over-cap 2xx still becomes a 429 while an over-cap non-2xx keeps its own status with the same bounded stub body, because rewriting a 404 into a 429 loses the answer; only that stub case is uncharged. **Reject, never queue**: queueing burns the sandbox's clock on a wait the script cannot see and can admit work that finishes after its execution is dead, the same waste `4h6.28` removed. Every 429 carries `code`, `limit` and `observed` (`sandbox_response_bytes`, `sandbox_aggregate_bytes`, `sandbox_request_count`, `sandbox_concurrency`, `sandbox_concurrency_pod`, `sandbox_execution_tracker_full`). **Cleanup cannot evict a live execution** — the deliberate departure from db-api's LRU, which can drop a running counter and silently reset its budget: an entry is evictable only once its token is past the point `verify_sandbox_token` would accept it *and* nothing is in flight under it (covering a stream that outlives its own token); the map is hard-bounded at `SANDBOX_MAX_TRACKED_EXECUTIONS` (4096) and at the bound refuses the *new* execution rather than evicting a running one. In-process, so `replicas: 1` is load-bearing exactly as on db-api and `k8s/deployments/results-api.yaml` now carries the matching comment. **One limitation remains documented rather than fixed** (`docs/code-execution-security.md` §4): `sandbox_execution_tracker_full` and the pod-wide concurrency limit are pod-wide and therefore **cross-tenant** denial surfaces, sized far above honest use rather than made fair. `tests/test_sandbox_budget.py` (30 tests, offline lane) is the only thing that will report a regression: production impact is nil while `SANDBOX_ENABLED` is `"false"` and no sandbox Deployment is applied (the manifest exists since `4h6.7`, gated off).
- **The no-credential path into the counters is closed; the internal-secret path is not** (`genetics-results-suite-0lf`, design: `docs/code-execution-security.md` §4). The four counters above are admitted from the `Authorization` header, so a request carrying none is counted against **nothing** — and the sandbox's NetworkPolicy egress reaches `results-api:4000` directly, bypassing auth-gateway, so a script could shed all four by omitting the header on any of the seven `@is_public` routes (measured: 20/20 header-less requests served with the counter map empty). That half is closed by **shrinking the anonymous surface, not by identifying the caller**: `app/dependencies.is_public_endpoint` treats only `ALWAYS_ANONYMOUS_PATHS` — `/healthz` — as servable with no principal whenever `ANONYMOUS_SURFACE_MINIMAL` is on, so every route touching a data path answers 401 to a request carrying nothing. results-api still cannot tell a sandbox request from a browser request — both arrive on `:4000` in-cluster — and for this half it does not have to. **It is *not* true that the only way into a handler is to present a credential whose presentation calls `admit`**, and earlier drafts of this bullet said so wrongly: `admit` is reached only from `_sandbox_principal`, which accepts an HS256 sandbox token and nothing else, while `INTERNAL_API_SECRET` satisfies `is_internal_caller` — measured against the real ASGI app with `SANDBOX_ENABLED=true`, `Authorization: Bearer $INTERNAL_API_SECRET` gets **200** on `/api/v1/rsid/variants` and `/api/v1/variant_sets` as `user_email=mcp-tool` with the counter map still empty. **The sandbox's half of that residue is now closed in the transport** (`genetics-results-suite-4h6.44`, landed): genetics-mcp-server's `tools/executor.py` builds its client from the per-execution tokens whenever `SANDBOX_TOKEN_FILE` names them, attaches the audience-bound token per destination, and **never** attaches `INTERNAL_API_SECRET` alongside or instead — the two paths are mutually exclusive in `_build_client`, because preferring the secret would silently re-open this. An unusable token file raises rather than falling back to the secret or to no header. **`genetics-results-suite-4h6.7`** keeps the Deployment half: the sandbox is never given the secret, which matters independently of what the SDK prefers, since a script that can read `os.environ` can build its own client. **What remains is intentional and is not the sandbox's**: results-api still serves an internal-secret caller unaccounted, because chat-backend, mcp-server and bff legitimately authenticate that way and none of them is a per-execution tenant — pinned by `tests/test_anonymous_surface.py::test_the_internal_secret_path_survives_but_the_sdk_no_longer_takes_it`, which asserts both halves. The rollout hazard an earlier draft warned about — flipping the flag early leaves the SDK working while the counters bind nothing — is gone with the transport: a sandbox request now resolves a principal, so `admit` runs. **Requiring a principal does not yet cost the browser nothing.** The BFF attaches the shared secret only on its **typed** upstream routes (`bff/upstream.ts`); the browser reaches all six narrowed routes through the BFF's **generic passthrough** (`bff/passthrough.ts`), which attaches no credential — measured against the live cluster, a header-less request through the *deployed* BFF still gets **200** from `/api/v1/auth`, and the passthrough fix exists only in genetics-results-browser's un-deployed `db-only-architecture` worktree. Usage logging cannot settle this either way: it cannot attribute callers on `@is_public` routes at all, because `state.authenticated_user` is never set there. **The control is `ANONYMOUS_SURFACE_MINIMAL`, not `SANDBOX_ENABLED`, and it defaults to on** (`genetics-results-suite-rhh`). It was gated directly on `SANDBOX_ENABLED` at first, which made one switch both the incident lever and the security lever with the security side failing **open**: `SANDBOX_ENABLED=false`, the routine action for killing the sandbox under pressure, silently re-opened all six routes. `SANDBOX_ENABLED=true` now merely *forces* the minimal surface, and widening it is an explicit `ANONYMOUS_SURFACE_MINIMAL=false` that the sandbox overrides. Defaulting it on **does** change behaviour at the next results-api deploy — those six routes stop answering anonymous callers now rather than at sandbox rollout. Most in-cluster callers admitted to `results-api:4000` by `k8s/network-policies/policies.yaml` already present a credential (auth-gateway forwards the client's own bearer, chat-backend and mcp-server send `INTERNAL_API_SECRET`), and nothing from outside the cluster reaches results-api without going through auth-gateway — but **two callers do not, so this is a three-service ordering constraint: `bff` → `mcp-server` → `results-api`.** (1) The **browser**: the BFF's credential-less generic passthrough serves all six of these routes and the fix is un-deployed (above), so results-api first means a 401 on the login-state probe, variant sets and rsid lookups. (2) An **mcp-server pod with `INTERNAL_API_SECRET` unset**, whose tool executor fell back to sending **no** `Authorization` header; `genetics-results-suite-618` turned that into a startup failure. Deploying 618 first does **not** keep that pod working — it converts a bare 401 with no local signal into a CrashLoopBackOff naming the variable. Diagnosability, not availability. **Nothing enforces the order**: `scripts/rollout.sh` documents it in its `ORDERING:` header, while `scripts/deploy.sh` restarts every Deployment in one unordered loop with results-api ahead of chat-backend and mcp-server (a warning now sits next to its `DEPLOYS` list). **Rejected**: removing `results-api:4000` from the sandbox's egress allow-list, because the SDK genuinely calls a public route (`search(rsids=...)` → `GET /v1/rsid/variants`) and 16 of its 25 functions are results-api-only; requiring the *sandbox* token specifically, which results-api cannot ask for without identifying the caller and which `/healthz` cannot satisfy for the kubelet; and a pod-wide anonymous-request bucket, which is a rate limiter (`genetics-results-suite-8zk`) that would 429 browser traffic. Enforced by `tests/test_anonymous_surface.py`, which reads the **live route table** so a new `@is_public` decorator fails a test rather than silently reopening the hole; `scripts/test-network-policies.py` cannot see route decorators and is not the right home for it.
- **Internal calls**: chat-backend authenticates to results-api via `INTERNAL_API_SECRET`
- **A deployed service never falls back to no credential** (`genetics-results-suite-618`, the same contract as `4h6.9`). genetics-mcp-server's `tools/executor.py` built its client header as "bearer if `INTERNAL_API_SECRET` is set, **no header at all** if it is not", so an unset variable made every call to results-api and db-api anonymous — silently, at request time, and invisibly at the far end, since results-api's usage log attributes callers by the secret and never sees a principal on a route that resolves none (measured: 246/246 NULL `user_email` on `GET /api/v1/rsid/variants` over 90 days, which distinguishes an anonymous caller from an internal one not at all). **Only `k8s/deployments/mcp-server.yaml` marks that `secretKeyRef` `optional: true`**, so a missing key in `genetics-secrets` leaves the variable unset and that pod starts anyway — mcp-server is the only one of the two that can reach the silently-anonymous state. `k8s/deployments/chat-backend.yaml` sets no `optional` on that key, so a missing key stops it at `CreateContainerConfigError` instead of starting it credential-less. Both entrypoints still call the guard, because an **empty** value satisfies the kubelet in either Deployment and reaches the process. The two deployed entrypoints now call `config.settings.require_internal_api_secret()` — `mcp_server.main()` for the remote transports, beside the existing `MCP_API_KEY` check, and `chat_api`'s lifespan when `REQUIRE_AUTH` is true — so a pod in that state crash-loops with a message naming the variable instead of issuing anonymous requests. Deliberately **not** enforced at import, in `Settings`, or in `ToolExecutor.__init__`: a local run against an unauthenticated results-api needs no secret, and the **sandbox image holds no internal credential by design** (`_PrunedInstallSettings` — it ships only the SDK's import closure and gets a per-execution token instead, `4h6.9`/`4h6.44`). A full install that builds the client with no secret now also logs a warning naming the variable, which is the only local signal on a developer's machine. This is one leg of the ordering constraint on `genetics-results-suite-rhh` — mcp-server ships before results-api — but not the whole of it: the browser's BFF passthrough is the other credential-less caller and ships first of the three. And 618 does not keep a secret-less pod working; it makes the failure legible (CrashLoopBackOff naming the variable) instead of a bare 401.
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
- **Node pool**: `e2-standard-4`, **autoscaling** `min_node_count = 1` / `max_node_count = 3` (`max = 2` on the daly profile); one node is running today. A full deploy can surge past a single node; nothing prevents the subsequent scale-down from evicting chat-backend — what keeps that eviction from truncating an in-flight stream is its graceful-shutdown configuration, not the PodDisruptionBudgets in `k8s/disruption-budgets/` (which are declarative only at `replicas: 1`) — see "Node pool sizing" below
- **Networking**: VPC with private subnet, static IP for ingress
- **SSL**: Google-managed certificates for the domain configured in `terraform/terraform.tfvars`
- **Storage**: 10Gi PVC (`chat-data`) for chat-backend SQLite databases (`chat_history.db` and `llm_config.db` — the latter now holds **user-authored prompt text**, see "Chat instructions" below), file attachments, and tool result downloads; 50Gi PV/PVC (`rag-stores`) for rag-service embedding stores; 1Gi PVC (`monitor-data`) for the monitor's alert-dedup SQLite DB; 5Gi PVC (`keycloak-postgres-data`) for the Keycloak database
- **Log sinks**: `terraform/logging.tf` optionally creates two Cloud Logging → BigQuery sinks (`endpoint_access` records from the in-cluster services → `genetics_api_logs`, chat-backend container logs at severity ≥ INFO → `genetics_chat_logs`), gated by `enable_log_sinks` (default `false`). **A BigQuery sink names its destination table after the log ID, not after the service**, and every GKE container logs to stdout, so all `endpoint_access` rows from results-api *and* db-api land in one table, `genetics_api_logs.stdout` — that is the table to query for API usage. **Identity is available for results-api rows only**: db-api's `endpoint_access` payload (`genetics-results-db/api/main.py`) carries no `user_email` and never will — db-api sits behind results-api and the internal secret rather than in front of users, so its caller is a *service*, not a person. It now emits `principal` (`internal` / `sandbox` / `unauthenticated`) naming the credential that authorized the call, which is the only principal that exists there. **Split the two services on `jsonPayload.service` — three eras, and a query spanning them needs all three.** Both services now emit a constant, non-env-derived `service` on every `endpoint_access` line: `"db-api"` (`genetics-results-db/api/main.py`, `SERVICE`) and `"results-api"` (`genetics-results-api/app/middleware_usage_logging.py`, `SERVICE`). It is a module constant on purpose — both earlier discriminators moved underneath the queries built on them:
    1. **From 2026-08-12 (`service` exists): `jsonPayload.service = 'db-api' | 'results-api'`.** The only stable split. Prefer it whenever the window is entirely inside this era.
    2. **2026-08-12 back to the start of the table (2026-03-06): enumerate `log_source` values.** db-api rows are `log_source='genetics_db_api_prod'`; results-api rows are `'finngenie_prod'` **or** `'genetics-results-api-prod'` (renamed 2026-06-03 — see the query hazard below). `log_source` is *not* a service discriminator and is retained only as the environment axis: it is derived from `DEPLOY_ENV`, contains no service name, is asymmetric between the two services (`genetics_db_api_prod` vs `genetics-results-api-dev1` do not even share a separator), and has already been renamed once in production. Enumerate the values you find; do not assume one, and do not parse the string.
    3. **Before db-api emitted anything (up to 2026-08-12): `endpoint_path IS NULL`** identifies db-api. This worked by accident — db-api emitted neither `log_source` nor `endpoint_path`, so the discriminator was the *absence* of a field, and it silently stopped meaning "db-api" the moment db-api started emitting `endpoint_path`. Use it only for rows that predate that change.

    A query crossing an era boundary must OR the relevant tests together; a query keyed on any single one returns a silently wrong subset outside its era rather than an error. The sibling table `genetics_api_logs.genetics_results_api` is the `genetics-results-api-dev1` **GCE VM**, which reached the sink because the filter used to be project-wide. It is not decommissioned and not usage: it is a developer machine running the results-api **test suite** (`sourceLocation.file` points inside a checkout under `/home/jkarjala/suite/genetics-results-api`, and it emitted 1,638 entries within a single second), still producing rows today — 1,377 on 2026-08-11. Narrowing the sink filter to `resource.type="k8s_container"` in namespace `genetics` **stops that feed deliberately and with no replacement**; that is the point, since it is test noise. Neither table ever carries `httpRequest` — the middleware emits a `jsonPayload`-only record, so `httpRequest.responseSize` is structurally NULL and no response-size data exists in this sink at all
  - **Query hazard — `log_source` was renamed.** Inside `genetics_api_logs.stdout`, results-api rows carried `log_source='genetics-results-api-prod'` (40,958 rows, only 95 non-null `user_email`) up to 2026-06-03; after that results-api emits `log_source='finngenie_prod'` (12,258 rows, 12,026 non-null `user_email` — the current value). db-api rows carried `log_source` NULL until it started emitting `genetics_db_api_prod` (2026-08-12, `genetics-results-suite-tcs`). A query still filtering on the old value returns **nothing after 2026-06-03 and no error**, which is the same silent-empty-result trap as reading the wrong table
  - **The sink's `jsonPayload` schema has no `sid`, `sub` or `jti` column**, so the sandbox-attribution fields db-api logs on a sandbox-authorized request (`api/main.py`, `require_auth`) are **not queryable in BigQuery** — they exist only in Cloud Logging / container stdout. That is expected today, since no sandbox Deployment is applied — the manifest exists since `4h6.7` but is gated off — and `SANDBOX_ENABLED` is `"false"` on both services, so no such row has ever reached the sink to grow the schema. Do not cite BigQuery for per-execution sandbox attribution without checking the schema again
- **Backups**: Daily GCE disk snapshots of the chat-data PVC (14-day retention, configurable via `snapshot_retention_days`)
- **Terraform state**: Per-profile GCS backends (`daly.tfbackend` → `genetics-results-terraform-daly`, `finngen.tfbackend` → `genetics-results-terraform`); `deploy.sh` auto-selects based on `config_profile` in `terraform.tfvars` unless `CONFIG_PROFILE` overrides it
- **tfvars guard (`require_tfvars`, default `true`)**: `terraform.tfvars` is gitignored and exists only in the main checkout, so terraform run from a git worktree (`.claude/worktrees/*`) or a fresh clone would fall back to variable **defaults** — and those defaults are not a no-op subset of the live config: `enable_log_sinks=false` destroys both log sinks and their BigQuery dataset IAM members, `manage_iam=true` with an empty `node_service_account` **replaces the GKE node pool**, and `config_profile`/`oauth_email_domain` revert to the daly/Broad values. A `precondition` on `data.google_compute_global_address.static_ip` in `terraform/main.tf` asserts `fileexists("${path.module}/terraform.tfvars")`; measured behavior is that Terraform still renders the complete plan first — every resource diff, including the alarming-looking `Plan: N to add, N to change, N to destroy` — and only afterward prints `Terraform planned the following actions, but then encountered a problem:` followed by the precondition error, exiting non-zero with nothing applied. The operator will see that full destroy/replace plan scroll past above the error, which is worth knowing since it is never actually applied. `scripts/deploy.sh` refuses to `terraform apply` on the same condition (`SKIP_TERRAFORM=true`, which only reads outputs from state, still works from a worktree). Supplying values another way requires `-var require_tfvars=false` alongside the `-var-file`. Note `project_id` and `domains` have no defaults, so a worktree run stops to prompt for them first — everything else defaults silently once they are answered. **The guard does not cover every entry point.** `terraform apply -target=<resource>` prunes the graph to the target and its dependencies; the guarded data source is a dependency of nothing (only a root output references it), so its precondition never evaluates and a targeted apply from a worktree still runs with the destructive defaults. `terraform destroy` also never evaluates it, because destroy plans are driven by state, not data sources — that gap is deliberate, not an oversight, since blocking teardown on `terraform.tfvars` presence would break legitimate destroys for no safety benefit
- **Profile-identity guard**: existence is not enough, because the main checkout keeps `terraform.tfvars.daly` and `terraform.tfvars.finngen` beside the active `terraform.tfvars`, and `CONFIG_PROFILE` selects the **backend** independently of which one is in place. The two profiles differ on `project_id`, `region`/`zone`, `domains`, `static_ip_name`, `manage_iam` and `oauth_email_domain`, so `CONFIG_PROFILE=daly ./scripts/deploy.sh` with the finngen tfvars present would write finngen values into the daly state — every value real and plausible, so the plan does not look wrong. Two checks, in order of usefulness:
  1. **`scripts/deploy.sh`** parses `config_profile` out of `terraform.tfvars` unconditionally and aborts if it differs from the profile that selected the backend, **before `terraform init` runs at all**. This is the path that matters. When `CONFIG_PROFILE` is unset the profile was derived from that same file, so the check is a no-op by construction — the mismatch only exists when something outside the file chose the backend. Only on the apply path: `SKIP_TERRAFORM=true` reads outputs from whichever state the backend selects and never applies variables, so tfvars content is inert there and the worktree flow keeps working
  2. **A second `precondition`** on `data.google_compute_global_address.static_ip` compares the bucket recorded in `.terraform/terraform.tfstate` (written by `terraform init -backend-config=`, the only in-config signal of which state this directory is bound to, and independent of every variable) against the bucket parsed out of `${var.config_profile}.tfbackend`. Backstop for a bare `terraform apply`; it inherits every limitation of the existence precondition above — full plan rendered first, silent under `-target`, never evaluated on `destroy` — and additionally passes when either side cannot be determined (never initialized, or no `.tfbackend` for that profile), both cases where terraform fails on its own. Gated on the same `require_tfvars` — so `require_tfvars=false` disables this bucket-identity check along with the existence check above; there is no separate flag to silence one without the other

### Node pool sizing

There are **two** pools, and they are sized on different grounds.

The **primary** pool **autoscales**: `min_node_count = 1`, `max_node_count = 3` in every live
`terraform.tfvars` profile (`max = 2` on daly), on `e2-standard-4`. One node is running today.

The **sandbox** pool (`<cluster>-sandbox-pool`, `terraform/gke.tf`) is **pinned at one node**
and exists for isolation, not capacity — see "The sandbox pool" below. It contributes **0m and
0 GiB** to everything in the surge table that follows.

> An earlier version of this section claimed the pool was pinned at
> `min_node_count == max_node_count == 2`. That pinning was written into
> `terraform.tfvars.example` by commit 6db94e8 but **never applied to any live profile**
> (`genetics-results-suite-262`). The decision has since been to keep autoscaling and handle
> the eviction case with graceful shutdown, which also covers node auto-upgrade — something
> pinning never did. PodDisruptionBudgets are declared for the two expensive workloads but,
> at `replicas: 1`, do not currently block anything (see below).

**Why the surge matters.** A full `deploy.sh` rolls every deployment at once. All of them
except chat-backend, keycloak-postgres and rag-service (which are `strategy: Recreate`) use
the default `RollingUpdate`, so with `replicas: 1` each surges by one extra pod. Figures below
are re-derived from `k8s/deployments/*.yaml` (2026-08-14) and from the live node (2026-08-07);
"system" is the per-node GKE overhead (`kube-system`, `gmp-system`, `gke-managed-cim`)
measured at 876m / 1.33 GiB.

**How to re-derive: sum pods' *effective* requests, not container counts.** The scheduler
computes a pod's effective request as `max( max(init container requests), sum(regular
container requests) )`, **per resource**. `auth-gateway` is the case that catches people: its
`render-config` (10m/16Mi) is an `initContainer`, and the pod declares no `restartPolicy`
anywhere, so it is a classic init container, not a native sidecar. 10m < 50m and 16Mi < 64Mi
on both axes, so it contributes **exactly zero** to scheduling and auth-gateway is
**50m / 64Mi**, not 60m / 80Mi. A *native sidecar* — an entry under `initContainers:` that
carries `restartPolicy: Always` — **would** be added to the regular sum. So check for that
field; do not count containers. A 2026-08-14 edit of this table applied the container-count
rule instead and inflated every peak by 20m / 32 Mi; it has been reverted.

RAG is **not** profile-derived: `scripts/deploy.sh` sets `ENABLE_RAG="${ENABLE_RAG:-false}"`
unconditionally, so rag-service is off on *every* profile unless the operator exports it. Only
Keycloak is profile-derived (on for daly). A default daly deploy is therefore **10**
deployments, not 11.

| | CPU | Memory |
|---|---|---|
| one `e2-standard-4` allocatable | **3920m** | **12.96 GiB** (13273 Mi) |
| app requests, daly **as deployed by default** (Keycloak on, RAG off — 10 deployments) | 1650m | 6.44 GiB (6592 Mi) |
| app requests, daly **with `ENABLE_RAG=true`** (11 deployments) | 1900m | 6.94 GiB (7104 Mi) |
| app requests, finngen profile (no Keycloak, no RAG — 8 deployments) | 1300m | 5.69 GiB (5824 Mi) |
| + per-node GKE system overhead | 876m | 1.33 GiB (1362 Mi) |
| rollout surge, daly (either variant — rag-service is `Recreate`, so it never surges) | +1300m | +5.69 GiB (+5824 Mi) |
| rollout surge, finngen (no Keycloak) | +1050m | +5.19 GiB (+5312 Mi) |
| **peak during a full deploy — daly, default** | **3826m** | **13.46 GiB (13778 Mi)** |
| **peak during a full deploy — daly, RAG enabled** | **4076m** | **13.96 GiB (14290 Mi)** |
| **peak during a full deploy — finngen** | 3226m | 12.21 GiB (12498 Mi) |
| the sandbox, on **either** primary-pool profile | **0m** | **0 GiB** (separate pool) |

So the **daly** profile as actually deployed overshoots a single node on **memory only** —
13778 Mi against 13273 Mi allocatable, **over by 505 Mi** — while its 3826m CPU peak stays
under the 3920m allocatable. It still must get a second node; only the reason is narrower than
"both axes". Turning RAG on pushes CPU over as well, so daly+RAG is over on **both** axes. The
**finngen** profile fits, with **775 Mi** of memory headroom — and that margin disappears if
the analyze-conversations (512Mi) or monitor (256Mi) CronJob overlaps the rollout. `results-api`
at 500m / 4Gi, doubling to 8Gi mid-roll, dominates the memory term either way.

**Nothing above changed when the sandbox was added, and that is the whole point of giving it
its own pool.** The sandbox contributes 0m / 0 GiB here because it is on a different pool. The
figures are `genetics-results-suite-262`'s, unchanged.

When the autoscaler does add a node for a rollout, the scheduler places pods on it and ~15
minutes later reaps the now-idle node, evicting them with `ScaleDown: deleting pod for node
scale down`. For chat-backend that killed an in-flight SSE response mid-answer.

> **Open question — the arithmetic above does not explain the eviction that prompted this
> work.** The live cluster runs the **finngen** profile on one node, and by the re-derived
> table finngen fits (12.20 GiB peak vs 12.96 GiB allocatable). If it fits, no surge node
> should be created, and the reap-the-surge-node mechanism narrated above should not fire on
> the live profile at all. The original narrative is kept because the eviction was observed;
> what is missing is a verified trigger. Plausible candidates, none confirmed: a transient
> CronJob overlapping the rollout and consuming the sub-1 GiB margin; a node auto-upgrade or
> auto-repair drain rather than an autoscaler scale-down (the log line would differ — check
> which was actually recorded); or a resource request that has been lowered since. This is
> **open**. Do not treat the table as having explained the incident.

**One mitigation is actually in place, and it is not the pinning.**

Graceful shutdown on chat-backend (`k8s/deployments/chat-backend.yaml`):
`terminationGracePeriodSeconds: 300`, a `preStop` sleep of 10s, and uvicorn
`--timeout-graceful-shutdown 280`. An evicted pod finishes the stream it is serving. This is
the whole of the live protection for a mid-stream eviction.

**The PodDisruptionBudgets** (`k8s/disruption-budgets/budgets.yaml`) on **chat-backend** and
**results-api** are set to `maxUnavailable: 1` and are **declarative only today**. Both
deployments are `replicas: 1`, so `desiredHealthy = replicas - maxUnavailable = 0` and
`disruptionsAllowed = 1` whenever the pod is healthy: every eviction is admitted immediately.
The budgets record which two workloads are expensive to interrupt — results-api because its
`startupProbe` allows ~5 minutes for a cold start with no second replica to serve tool calls
meanwhile, chat-backend because of the SSE stream — and they become a real constraint the
moment a second replica exists. They do not prevent the reap-the-surge-node eviction now.

**Why `maxUnavailable: 0` was rejected.** It is the value that would make these budgets bite at
`replicas: 1`, and it is a trap. With one replica `maxUnavailable: 0` and `minAvailable: 1` are
the same constraint and both forbid voluntary eviction outright — the budget can never be
satisfied while the pod exists. The cost is **not** symmetric across the two GKE drain paths,
and the expensive one is the autoscaler:

- **cluster autoscaler — no time bound at all, this is the dominant cost.** The autoscaler
  documents two separate branches. "If there are Pods on a node that cannot move to other
  nodes in the cluster, cluster autoscaler does not attempt to scale down that node." Only
  *separately*, for a node it has already selected: "If Pods can be moved to other nodes, but
  the node cannot be drained gracefully after a timeout period, the node is forcibly
  terminated. This timeout period is one hour for GKE versions 1.32.7-gke.1079000 or later"
  ([cluster autoscaler](https://cloud.google.com/kubernetes-engine/docs/concepts/cluster-autoscaler)).
  A permanently unsatisfiable PDB puts the node in the **first** branch: it is never selected,
  so the one-hour force-termination never applies and there is no bound. The node stays.
- **node upgrade — one hour, but only for the SURGE strategy.** This pool uses surge
  (`terraform/gke.tf` sets no `upgrade_settings`), and surge upgrades drain "respecting
  PodDisruptionBudget and GracefulTerminationPeriod settings for up to one hour", after which
  "any remaining Pods are forcefully evicted so that the upgrade can proceed". Under
  **BLUE_GREEN** the bound is different: pods go away in the delete-blue-pool phase, gated by
  a configurable soak that defaults to 1 hour and can be extended to 7 days
  ([node upgrade strategies](https://cloud.google.com/kubernetes-engine/docs/concepts/node-pool-upgrade-strategies)).
- **node auto-repair — UNCONFIRMED.** Google documents a one-hour drain bound for repair, but
  does not state that PDBs are honoured during it. Do not rely on the one-hour figure holding
  for this path either way.

**The permanent-ratchet consequence that `0` would have produced.** results-api is
`RollingUpdate`, so during a rollout its replacement pod is scheduled onto the surge node
before the old pod dies. The old pod then goes away and node 1 frees up — but the new pod
stays on node 2, and under a blocking budget node 2 would host a pod that can never be
selected for scale-down. **The pool would ratchet to two nodes and stay there** — a permanent
cost arrived at as a side effect, and effectively the `min == max == 2` pinning that was
explicitly rejected. That is the reason for choosing `1` over `0` (bead
`genetics-results-suite-0v5`), accepting that the budgets are protectively inert until a second
replica exists. At `maxUnavailable: 1` this ratchet does not occur.

**Which workload would have driven that ratchet.**

- **results-api** is `RollingUpdate` with no volume or affinity holding it to a node, so it
  migrates to the surge node on every rollout. It would have been the reliable cause.
- **chat-backend** is `strategy: Recreate`, so it never runs alongside its own old pod and
  never *creates* a surge node. Its `chat-data` PVC is `ReadWriteOnce` on `standard-rwo`,
  which is **zonal, not node-scoped** — it does not pin the pod to a node, it only forces a
  detach/reattach (and the ~20s `Multi-Attach error` noted below). So chat-backend *can* be
  rescheduled onto a surge node that some other deployment created, and a blocking budget would
  then pin that node too. Less frequent than results-api, not impossible.

Neither PDB blocks `deploy.sh` itself: the Deployment controller deletes pods directly rather
than going through the Eviction API, so rollouts are unaffected.

Both budgets set `unhealthyPodEvictionPolicy: AlwaysAllow`. At `maxUnavailable: 1` over one
replica the budget blocks nothing, so this field **has no effect today** — it is not fixing an
active deadlock. It is kept because it is the correct setting once a second replica exists, and
because removing it would leave that deadlock unguarded once the budget can actually reach its
limit — every replica unhealthy at `maxUnavailable: 1`, or *any* unhealthy pod under
`minAvailable` or `maxUnavailable: 0`. (At `replicas: 2, maxUnavailable: 1` a single unhealthy
pod beside a healthy one still leaves `currentHealthy >= desiredHealthy`, so `IfHealthyBudget`
admits its eviction; raising `replicas` alone does not recreate the block.) Under the default
`IfHealthyBudget`, a running-but-unhealthy pod
may be evicted only if the guarded application is not disrupted, so under a blocking budget a
**CrashLoopBackOff pod cannot be evicted either** — serving nothing while still blocking any
eviction-API drain (`kubectl drain`, surge upgrade). `AlwaysAllow` lets an unhealthy pod "be
evicted regardless of whether the criteria in a PDB is met"
([configure a PDB](https://kubernetes.io/docs/tasks/run-application/configure-pdb/)). That
relief is confirmed only for the Eviction API path; whether cluster-autoscaler's scale-down
candidate simulation models the field at all is **unconfirmed**, so do not assume a
crashlooping pod releases the scale-down ratchet a blocking budget would create. The field is accepted at `policy/v1` on
this cluster's 1.35.6-gke.1250000, verified with
`kubectl explain pdb.spec.unhealthyPodEvictionPolicy` and a client-side dry-run.

**`AlwaysAllow` would carry a cost once the budgets bite, and it lands on results-api's own
justification.** "Healthy" here means `status.conditions[type=Ready].status == "True"`, so a
pod that is merely *starting* is also unhealthy for this purpose and is freely evictable.
results-api's `startupProbe` is `failureThreshold: 60, periodSeconds: 5` — about five minutes —
so even at `replicas: 2` the budget would give **no** protection for ~5 minutes after every
restart: the pod can be evicted and its node is drainable. That is exactly the cold-start
window the results-api budget is justified by, so any future guarantee begins when the
`startupProbe` passes, not when the pod is created. `AlwaysAllow` stays anyway — the
alternative is an unbounded crashloop deadlock.

So a *blocking* budget buys protection for an in-flight request and costs a node that may never
be reclaimed. That is arguably the right trade for a long-lived SSE stream and a 5-minute cold
start once there are two replicas to make it satisfiable; it is **not** the right trade for a
cheap-to-restart service, and at `replicas: 1` it is not available at all without the ratchet.
Do not blanket the namespace with PDBs — db-api, mcp-server, bff, auth-gateway, frontend and
oauth2-proxy deliberately have none, because eviction already runs their `preStop`/SIGTERM path
and they come back in seconds. Symptom of an over-restrictive PDB: `gke_cluster` log entries
reading `Cannot evict pod as it would violate the pod's disruption budget`.

Other consequences to keep in mind:

- **Raising any deployment's requests, or adding a service, changes the table above.**
  Re-derive the surge total against 3920m / 12.96 GiB per node before merging such a change.
- The `chat-data` PVC is `ReadWriteOnce`, so once a second node exists a `Recreate` rollout can
  land chat-backend on the *other* node and stall ~20s on `Multi-Attach error` while the volume
  detaches. That is a slower deploy, not a dropped request.
- The analyze-conversations CronJob carries a podAffinity onto chat-backend's node for the same
  PVC reason (see "Conversation analysis pipeline"); that keeps working with several nodes.

To consolidate onto one larger node instead, note that `node_config.machine_type` is ForceNew on
`google_container_node_pool` — changing it in place destroys and recreates the pool. Do it as a
new pool plus cordon/drain migration, not a tfvars edit. The same applies to
`var.sandbox_machine_type` below.

### The sandbox pool

`google_container_node_pool.sandbox_nodes` in `terraform/gke.tf`: `<cluster>-sandbox-pool`,
`e2-standard-2` (`var.sandbox_machine_type`), `sandbox_config { type = "gvisor" }`,
`min_node_count == max_node_count == 1`. It hosts only the code-execution sandbox
(`docs/code-execution-security.md`). It is created **only when `sandbox_pool_enabled = true`**
(`count`); the default is `false`. Note also that `type = "gvisor"` is lowercase while the GKE
REST enum is `GVISOR` and the provider does no normalization — whether the API accepts the
lowercase form is unverified until the first real apply; if rejected the value becomes
`"GVISOR"` here, in `terraform/gke.tf` and in `docs/code-execution-security.md`.

**Why a second pool at all.** Not capacity — isolation. GKE Sandbox is a per-pool property, so
gVisor's userspace syscall boundary around untrusted LLM-authored code can only be bought by
creating a pool for it, and the consequence is that chat-backend can never be co-scheduled with
that code. GKE taints gVisor nodes `sandbox.gke.io/runtime=gvisor:NoSchedule` automatically, so
no other workload drifts onto it.

**Why pinned when the primary pool is not.** The primary pool autoscales because its pods are
restartable request-servers with graceful shutdown. A scale-down here would kill an in-flight
script mid-execution, and there is no second replica. Accepted cost: **one permanently-running
`e2-standard-2`**. The cost argument in `docs/code-execution-security.md` was originally written
against a primary pool believed to be pinned at 2 nodes ("the sandbox contributes 0 to a budget
that is already over"); `genetics-results-suite-262` established the primary pool actually runs
**one** node under the live finngen profile. The pool was still chosen, on isolation grounds
alone — do not repeat the "nearly free" framing.

**Budget on that node.** CPU allocatable is arithmetic (2 vCPU, GKE reserves 70m → 1930m).
**Memory allocatable is not known and cannot be derived offline.** The usual method — advertised
capacity 8192 Mi, minus GKE's 1843 Mi reservation, minus the 100 Mi eviction threshold — gives
6249 Mi, but that method provably *overstates*: applied to the measured `e2-standard-4` it
yields 13622 Mi against a measured 13273 Mi, because real capacity is 15996 Mi rather than
16384 Mi. So treat **6249 Mi as an upper bound**, not a value; the true figure is lower by an
amount only a real node reports.

| | CPU | Memory |
|---|---|---|
| one `e2-standard-2` allocatable | **1930m** | **≤ 6249 Mi** (upper bound, see above) |
| sandbox pod **requests** (`replicas: 1`) | 500m | 1024 Mi |
| per-node overhead that can actually land here — measured DaemonSets, see below | ~383m | ~769 Mi |
| plus `gke-metadata-server` and the GKE Sandbox components | **undetermined** | **undetermined** |
| **scheduled total, excluding the undetermined rows** | **~883m** | **~1793 Mi** |
| sandbox pod **limits** (burst ceiling, not a reservation) | 1500m | 3072 Mi |

**Why the primary node's 876m / 1.33 GiB does not apply here.** That measurement is the whole
of the primary node's system load, and most of it is singletons — kube-dns, metrics-server,
konnectivity, the autoscalers, ~487m / ~555 Mi in total — which **cannot** land on the sandbox
node: they do not tolerate `sandbox.gke.io/runtime=gvisor:NoSchedule`. The measured breakdown is
DaemonSets 383m / 769 Mi (these tolerate everything, so they *do* land here), ReplicaSets
382m / 425 Mi and a StatefulSet 105m / 130 Mi (these do not). Quoting 876m for this node is an
**over**-statement, not a lower bound.

Three things that table does *not* settle, all tracked in `genetics-results-suite-5r2`:

1. **`gke-metadata-server`.** It is a DaemonSet that exists only on Workload-Identity clusters,
   and this cluster has WI off today, so its request is **undeterminable offline**. It is not
   guessed here.
2. **The GKE Sandbox components.** One of them is measurable: `runsc-metric-server` already
   exists dormant in `kube-system` at 3m / 12 Mi (`desiredNumberScheduled: 0`). The rest appear
   only once a gVisor node exists. As of 2026-08-13 the project has none anywhere.
3. **The runsc sentry's memory is charged to the pod's cgroup, so it eats the 3Gi *limit*, not
   the node headroom.** It therefore constrains the `RLIMIT_AS` budget the supervisor sets for
   the child, not this table. Unmeasured, and workload-shaped. That budget is now a concrete
   number — `genetics-results-suite-4h6.41` sets `RLIMIT_AS` to **2560 MiB**, i.e. the 3Gi
   `limits.memory` less 512 MiB of supervisor headroom, hard-coded from
   `k8s/deployments/sandbox.yaml` rather than read from `/sys/fs/cgroup` (see
   `docs/code-execution-security.md`, "As built (`4h6.41`, `4h6.42`, `4h6.43`, `4h6.46`)").
   Whatever the sentry holds comes out of that same 512 MiB, so if it turns out to be large
   the headroom is what has to grow.

**On the CPU limit.** An earlier version of this section claimed the pod's 1500m ceiling was
"deliberately not satisfiable alongside the system pods" (876m + 1500m = 2376m > 1930m). That
rests on the wrong base: on the ~383m that can actually schedule here, 383m + 1500m = 1883m,
which is **under** 1930m — so the alarming conclusion is not established, and it is not replaced
with a reassuring one either, because the two undetermined rows above could move it in either
direction. What is true regardless: limits are not reservations, so nothing here blocks at
schedule time; `requests: 500m` is what guarantees schedulability; and a script at its full
ceiling contends with the node's own daemons under CFS shares. If `sandbox_machine_type` ever
changes, revisit the pod's 500m/1500m together with it.

**What the pool spec carries that is not about sizing at all.** Three properties in
`terraform/gke.tf` are load-bearing security controls, and a reviewer should check them by
reading the source rather than a plan diff:

- `workload_metadata_config { mode = "GKE_METADATA" }`, **unconditional**. Unset defaults to
  `GCE_METADATA`, which exposes `169.254.169.254` to every pod on the node — one HTTP GET of
  `/computeMetadata/v1/instance/service-accounts/default/token` then yields a node-level token,
  and Workload Identity becomes irrelevant because the identity served is the node's.
- `google_container_cluster.primary`'s `workload_identity_config` is **unconditional** as a
  direct consequence: GKE rejects a `GKE_METADATA` pool on a cluster with no `workload_pool`,
  and it rejects it **at apply, not at plan**. It used to be gated on `var.manage_iam`. Enabling
  the workload pool is an in-place cluster update — it does not recreate the cluster and does
  not change the primary pool's own (still `manage_iam`-gated) metadata mode.
- `var.sandbox_node_service_account` is **required whenever the pool is created**, enforced by
  `lifecycle { precondition }` blocks on the resource rather than by variable validation — so it
  fires only when the pool actually exists, and the escape hatch is "don't create the pool",
  never "create it with a weaker SA". Three checks: the email must match
  `<name>@<project_id>.iam.gserviceaccount.com` **in this project** (case-folded), it must not be
  `genetics-suite`, and it must not equal `var.node_service_account` — that last one matters most,
  because under the live `manage_iam = false` mode the primary pool grants
  `oauth_scopes = ["cloud-platform"]`, so sharing its SA would put the suite's entire credential
  on the node running untrusted code. **These are format and identity checks, not a privilege
  check.** Terraform neither creates the SA nor reads back the roles bound to it; the `gcloud`
  recipe in `README.md` (`roles/logging.logWriter`, `roles/monitoring.metricWriter`,
  `roles/monitoring.viewer`, `roles/stackdriver.resourceMetadata.writer`,
  `roles/artifactregistry.reader`) is the only thing bounding them, and nothing checks that the
  operator did not grant more. The variable keeps `default = ""` only so terraform does not
  prompt interactively; `""` fails the first precondition.
- `var.sandbox_pool_enabled` (**default `false`**) gates whether the pool resource exists at all,
  via `count`. It must **never** appear inside `node_config` — no `dynamic`, no ternary, no
  count-derived field — so the only two representable states are "no pool" and "a pool in
  `GKE_METADATA` mode". The cluster's `workload_identity_config` stays unconditional and is
  deliberately *not* tied to this flag: tying it would make enabling the pool a two-step apply,
  and the apply-time failure in between invites exactly the re-gating of the metadata mode that
  the design forbids.

**The pool is only half the control, and terraform cannot supply the other half.** `GKE_METADATA`
does not deny credentials — it swaps *node* identity for *KSA* identity. Whether that is worth
anything depends entirely on the KSA the sandbox pod runs as, and nothing in this change
establishes it. The house style is the failure mode: all 8 deployments in `k8s/deployments/*.yaml`
use `serviceAccountName: genetics-suite`, which `terraform/iam.tf` binds to a GSA holding
`bigquery.dataViewer`, `bigquery.jobUser`, `storage.objectViewer` and `logging.viewer`. Whoever
writes `k8s/deployments/sandbox.yaml` **must** give it a dedicated KSA with no
`iam.gke.io/gcp-service-account` annotation and no `google_service_account_iam_member` binding —
explicitly **not** `genetics-suite`, and not the namespace `default` either. Copy the house style
and `GKE_METADATA` buys nothing at all.

The explicit `oauth_scopes` (`devstorage.read_only` — **required** for Artifact Registry pulls,
`roles/artifactregistry.reader` alone is not sufficient — plus `logging.write`, `monitoring`,
`monitoring.write`, `service.management.readonly`, `servicecontrol`, `trace.append`) are **not**
a fourth control of that kind. In `GKE_METADATA` mode pod tokens come from the IAM Credentials
API and are not bounded by node scopes at all. They defend the `GCE_METADATA` misconfiguration
case and the escape-to-the-node case only. Do not cite them as the pod-facing guarantee.

`kubelet_config { pod_pids_limit = 1024 }` is the outer fork-bomb backstop (a per-pod pid
ceiling is a kubelet setting, not a pod-spec field — one more reason the sandbox needs its own
pool). `docs/code-execution-security.md` specifies 256; GKE's documented range for
`podPidsLimit` starts at 1024, so 256 would be rejected at pool creation. **Unconfirmed against
this cluster** (`genetics-results-suite-5r2`); note in particular that `terraform validate` does
*not* establish it — the provider schema is a bare optional number with no range check, so it
passes on 256 as readily as on 1024. The number is not what contains a fork bomb anyway — the
supervisor enforces a child pid budget far below it.

**What an operator is actually walking into.** `scripts/deploy.sh` runs
`terraform apply -auto-approve` on every full deploy, so terraform changes are **not opt-in**;
`SKIP_TERRAFORM=true` applies manifests only and is the escape hatch. `terraform.tfvars` is
gitignored and lives only in the **main checkout**, so setting `sandbox_pool_enabled` or
`sandbox_node_service_account` there is a manual step this repo cannot make. And because
`workload_identity_config` on the cluster is now unconditional while the live cluster's
`workloadPool` is currently **empty** (`manage_iam = false`, verified via
`gcloud container clusters list`), **the next apply enables Workload Identity on the live
cluster whether or not the sandbox pool is enabled**. That is an in-place update and very likely
inert today — no pool runs in `GKE_METADATA` mode and no KSA carries a WI binding — but it
reaches production without anyone opting in, which is more than "an in-place cluster update"
conveys. **Recorded as a known risk, not changed here:** afterwards the primary pool still has no
explicit `workload_metadata_config` under `manage_iam = false`, so if that pool is ever
*recreated* (`terraform/main.tf` already warns pool replacement is a live hazard) its metadata
mode would come from GKE's cluster-derived default rather than the `GCE_METADATA` all 8 workloads
depend on. Whether to pin the primary pool explicitly is tracked separately; nothing in `4h6.10`
alters the primary pool's behaviour.

### The sandbox Deployment

`k8s/deployments/sandbox.yaml` (`genetics-results-suite-4h6.7`) holds three objects: a
ServiceAccount `sandbox`, the Deployment, and a ClusterIP Service on 8080 → 8080. Image
`${REGISTRY}/sandbox:latest`, one replica, `strategy: Recreate`.

**It is not applied by default.** `scripts/deploy.sh` skips the file unless
`ENABLE_SANDBOX=true`, and derives that from `sandbox_pool_enabled` in `terraform.tfvars`
rather than making it a second, independent switch — the pod tolerates
`sandbox.gke.io/runtime=gvisor:NoSchedule` and selects `workload=sandbox`, so applying it
without the pool yields a pod that is Pending forever while `kubectl apply` returns 0 and the
deploy reports success. An explicit `ENABLE_SANDBOX` in the environment still wins (for
`SKIP_TERRAFORM=true` re-applies), and a **sandbox preflight** immediately after kubectl is
configured — *before the first `kubectl apply` of the run* — then refuses on either of two
preconditions: no node carrying `workload=sandbox`, or a `sandbox.yaml` that still declares
neither `command:` nor `args:`. Both used to sit inside the `for f in deployments/*.yaml` loop,
where `sandbox.yaml` sorts second-to-last, so `exit 1` fired only after every other manifest had
been applied and skipped the cronjob apply, all rollout restarts and all rollout-status waits —
a half-finished deploy whose freshly built `:latest` images were never rolled, with no summary
line saying so. The node-check message also names the pool-just-created case: a node that has not
yet registered the label reads identically to no pool at all, and the fix there is to wait and
re-run rather than to change terraform. The `command`/`args` refusal exists because a manifest
with neither **schedules and CrashLoopBackOffs** — "Pending forever" is only the no-pool case —
and nothing downstream waits on the sandbox rollout, so the deploy would exit 0 and print success
over a pod that can never serve; it is keyed on the manifest, not on a ticket number, so it
clears itself the moment `4h6.50` adds `args:` — the last bead of the supervisor chain, which is
why `4h6.39` writes the supervisor without touching this manifest. Turning the sandbox on is
therefore a deliberate two-step: set `sandbox_pool_enabled = true` (plus
`sandbox_node_service_account`) in the **main checkout's** gitignored `terraform.tfvars`, then
deploy — and until the supervisor lands, that deploy is refused with `4h6.39` named.

**The manifest carries no `command`/`args`, deliberately.** The image's ENTRYPOINT is the bare
interpreter and it ships no `CMD`; the supervisor is `genetics-results-suite-4h6.39`'s to
write (and `4h6.50`'s to wire in here), and a placeholder would be indistinguishable from a
real one at runtime — the same reasoning that kept `CMD` out of the image. Until it lands, an
applied pod would start
`python3` with no script and exit, which is the second reason the gate defaults off.

Security-relevant fields, and what each one is for:

| Field | Value | Why |
|---|---|---|
| pod label / port / Service | `app: sandbox`, 8080/TCP, ClusterIP `sandbox` 8080 → 8080 | The contract `k8s/network-policies/sandbox-policy.yaml` declares. Every rule there selects `app: sandbox`; a podSelector matching nothing is silent no-coverage, and that file is the namespace's **only** Egress policy. `scripts/test-network-policies.py` asserts the pairing. |
| `serviceAccountName` | `sandbox`, a dedicated KSA with **no** `iam.gke.io/gcp-service-account` annotation and no IAM binding | `GKE_METADATA` on the pool swaps *node* identity for *KSA* identity; it does not deny credentials. `genetics-suite` (the house style, used by 8 workloads) is bound to a GSA holding BigQuery and GCS reader roles, and the namespace `default` is a shared identity — either would make the isolation design buy nothing. |
| `automountServiceAccountToken` | `false`, on both the SA and the pod | Denies the Kubernetes API server. It defends **nothing** against the GCP metadata server, which is reached over the network and needs no mounted token. It also avoids adding a sixth workload to `genetics-results-suite-5ho`. |
| `enableServiceLinks` | `false` | Kubernetes otherwise injects `<SERVICE>_SERVICE_HOST`/`_PORT` for every Service in the namespace, handing untrusted code the full internal inventory and its ClusterIPs. |
| `runtimeClassName` / `nodeSelector` / toleration | `gvisor` / `workload: sandbox` / `sandbox.gke.io/runtime=gvisor:NoSchedule` | Pins the pod to the gVisor pool and nowhere else. `workload: sandbox` is the node label `terraform/gke.tf` sets; the taint is applied by GKE, not by terraform. |
| `dnsPolicy` / `dnsConfig` | `None`, nameserver `127.0.0.1`, `ndots:1 timeout:1 attempts:1` | The egress policy permits no 53/UDP. Left at `ClusterFirst` a lookup that escapes `/etc/hosts` stalls through the whole resolver timeout budget against dropped packets — a hang inside the wall clock, unrepairable under `readOnlyRootFilesystem`. Against loopback it fails in microseconds instead. Depends on the image's `/etc/nsswitch.conf` listing `files` before `dns` (asserted at build time). |
| `hostAliases` | db-api and results-api ClusterIPs, **all four name forms each** | Replaces DNS. glibc's `files` module does no search-domain expansion, so the bare name does not answer a lookup for the FQDN the rest of the suite uses. deploy.sh resolves both IPs from the live cluster and substitutes `${DB_API_CLUSTER_IP}` / `${RESULTS_API_CLUSTER_IP}`; deleting and recreating either Service requires re-rendering and rolling this Deployment. |
| pod `securityContext` | `runAsNonRoot`, uid/gid 65532, `fsGroup: 65532` (`OnRootMismatch`), `RuntimeDefault` seccomp | 65532 is the distroless `nonroot` identity and deliberately none of the suite's other uids. The uid choice the design left open is **decided here as option (b), one shared uid** (`docs/code-execution-security.md` → "The uid choice"): option (a)'s distinct child uid needs `CAP_SETUID`/`CAP_SETGID`/`CAP_CHOWN` to `setuid` and to `chown` `/scratch/<id>` and the token file, and this container drops all capabilities with `allowPrivilegeEscalation: false`, so both calls return `EPERM`. The costs are real and the supervisor beads inherit them — `RLIMIT_NPROC` stops being a per-execution control (`4h6.41`: the supervisor must police the child's process group instead) and the token file sits within the child's same-uid reach, which read-once-and-unlink (`4h6.43`/`4h6.44`) does **not** bound — `4h6.55` measured a detached grandchild of an earlier execution reading a live token file from inside the read-once window, and recovered tokens from a completed execution by scanning `/proc/self/mem`; the bound is `4h6.55`'s resolution — and `4h6.39` must not assume it can drop privileges. `fsGroup` is therefore simply the pod's single gid, and is what makes the `emptyDir` writable by a non-root uid deterministically. |
| container `securityContext` | `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]`, non-root 65532, `RuntimeDefault` | Exceeds the suite baseline. Bytecode and the matplotlib font cache are baked at build time, so nothing needs to write outside `/scratch`. `procMount` is left at its default: `Unmasked` would weaken the container, and anything hiding `/proc/self/fd` would break `read_artifact`, which verifies the artifacts directory **by descriptor** through `/proc/self/fd/<dirfd>`. |
| volumes | exactly one `emptyDir` at `/scratch`, `sizeLimit: 512Mi` | No PVC ever (`chat-data` holds every conversation), no ConfigMap, no Secret, no pod-level `/tmp`. `/scratch` is where the mount must be: `read_artifact` refuses any `SANDBOX_ARTIFACTS_DIR` whose resolved path is not under a hardcoded `/scratch/` prefix, and refuses a symlinked one. Exceeding `sizeLimit` **evicts the pod**, so the supervisor's sub-quotas must fire first. |
| env | `GENETICS_API_URL`, `BIGQUERY_API_URL` only | **No credentials, and none may be added** — not `INTERNAL_API_SECRET`, not `SANDBOX_TOKEN_SIGNING_KEY`. Per-execution tokens arrive in the body of chat-backend's POST and live 300s; a pod-spec value would make them static pod-lifetime credentials. `SANDBOX_ARTIFACTS_DIR` is likewise **not** set here: it is per-execution (`/scratch/<execution-id>/artifacts`) and a fixed pod-wide value would be exactly the cross-execution shared directory that removing `/tmp` prevented. |
| resources | requests 500m / 1Gi / 1Gi ephemeral, limits 1500m / 3Gi / 2Gi ephemeral | As in "The sandbox pool" above and `docs/code-execution-security.md` § 2. The memory limit is the cgroup the runsc sentry is charged against too. |
| probes | readiness on `/health`, **no liveness probe** | The supervisor enforces its own wall clock; a liveness probe racing a legitimate 120s execution would restart the pod and destroy it for nothing. Kubelet probes are exempt from NetworkPolicy on this dataplane, which is why `sandbox-policy.yaml` carries no probe rule. |
| `strategy` / `terminationGracePeriodSeconds` | `Recreate` / 130 | The pool is one pinned node whose remaining headroom includes two rows this document records as undetermined, so a surge pod is not obviously schedulable; `Recreate` also keeps "one execution at a time" true cluster-wide. 130s is the 120s hard ceiling plus time to reap, answer and wipe. |

`SANDBOX_ENABLED` stays `"false"` on db-api and results-api — flipping it is the separate,
user-gated step that turns the feature on, and `scripts/test-network-policies.py` enforces the
pairing whenever `ENABLE_SANDBOX=true`.

### The sandbox HTTP contract (summary — the definition lives elsewhere)

`docs/code-execution-security.md` § 2, "The HTTP contract between chat-backend and the
supervisor" (`genetics-results-suite-4h6.38`), **owns** the wire shape between chat-backend
and the supervisor: every field, its type, whether it is required, and what happens when it
is absent or malformed. It is written down rather than shared as code because the sandbox
image pip-installs only the genetics SDK's import closure and `sandbox/prune_venv.py`
deletes the rest, so the two ends (`4h6.39` in this repo, `4h6.47` in genetics-mcp-server)
cannot import one definition. Do not restate it here; the essentials only:

- **A small fixed set of routes and no others**: a health endpoint the `readinessProbe`
  reads, one execute endpoint, and one artifact-read endpoint, plain HTTP/1.1 JSON. No
  HTTP-layer authentication — the sandbox holds no credential to verify against, so the
  ingress allow-list is the authentication.
- **Artifacts come back out only as images, and only automatically** (`GET /artifact`,
  `genetics-results-suite-8z1`). A script that saves a figure has it shown to the user:
  chat-backend fetches image artifacts of a completed run itself, streams them as `image` SSE
  chunks, and strips the base64 before the tool result reaches the model. The unguessable
  per-execution `execution_id` — never rendered to the model — is the authorisation, and only
  **retained** (completed) executions are served. Nothing the model calls reaches the route,
  and no other artifact type is retrievable; general artifact reads and the sid-scoped
  resolution remain `genetics-results-suite-4h6.52`.
- **Nothing in the request or response depends on Kubernetes**, so the local Docker backend
  (`4h6.40`) speaks the identical contract; the client holds one base URL. What differs
  (gVisor, NetworkPolicy, `hostAliases`, `emptyDir`) is deployment only.
- **Execute returns once and does not stream.** A `200` covers a script that raised, timed
  out or hit a limit; non-2xx means the supervisor did not run it at all.
- **The wall clock is bounded and the model cannot raise it** — `run_analysis` exposes no
  timeout parameter, and an over-ceiling request is rejected rather than clamped.
- **One execution at a time, with a bounded queue and a bounded wait**, which with
  `replicas: 1` and `strategy: Recreate` is the cluster-wide bound. Every figure involved —
  the port, the timeouts, the queue depth and wait, the output cap, the manifest fields —
  lives in the security doc, which calls the queue depth "the one tunable number here". Read
  them there; a copy here would rot silently.
- **The per-execution tokens travel in the POST body only** — never pod env, ConfigMap or
  Secret, which is why the manifest declares no credentials.
- **What the sandbox puts on the wire, and to which upstream** (`4h6.43` supervisor half,
  `4h6.44` SDK half, both landed). The supervisor writes both tokens to
  `/scratch/<id>/tokens.json` (mode `0600`, `O_EXCL`) and names the **path** to the child in
  `SANDBOX_TOKEN_FILE`. genetics-mcp-server's `tools/executor.py` reads that file **once**
  with `O_NOFOLLOW`, unlinks it in a `finally` whether or not the read succeeded, and attaches
  the token matching the **destination** of each request — `aud: results-api` for
  `GENETICS_API_URL`, `aud: db-api` for `BIGQUERY_API_URL`. Both validators pin `aud` as a
  string, so a token sent to the wrong service is a hard `401`. Any other destination gets no
  credential, and `INTERNAL_API_SECRET` is never sent from the sandbox at all. A missing or
  unusable token file **raises before the request** rather than degrading to an
  uncredentialed run. Two things this is not: read-once-and-unlink is **not** an exposure
  bound (`4h6.55`, above), and the destination binding is **hygiene plus a correctness
  property, not a control over the script** — the child is forked without exec and owns
  `os.environ`, and both URLs are resolved on first SDK use, so a script that repoints
  `GENETICS_API_URL` before its first call sends the results-api token to a host of its
  choosing (reproduced). The egress allow-list in
  `k8s/network-policies/sandbox-policy.yaml` is what stops that in the cluster; the local
  Docker path has no NetworkPolicy and no such stop.
- **The two upstreams meter differently.** results-api's four per-execution counters
  (`app/core/sandbox_budget.py`) are admitted in middleware **before routing**, so every
  request counts, unmatched paths included. db-api has **no request-count or concurrency
  limit at all**; its per-execution control is the 200 GB aggregate byte budget keyed on
  `jti`, checked in the handlers of its four BigQuery paths. So `genetics.sql()` is bounded
  by spend and attributable per execution, but **not** bounded by request count or
  concurrency, and db-api's cheap paths are uncounted. That is a difference in kind — db-api's
  cost is BigQuery bytes, results-api's is egress and pod memory — not an oversight, but "the
  per-execution counters now apply" must not be read as "db-api counts requests".
- **`execution_id` is one value in three roles**: the `/scratch/<id>` directory name, the
  `jti` of both tokens, and the log join key. Any disagreement is a `400`, never a
  best-guess, and an id is spent once — a resubmission with the same one is refused.
- **The response's artifact manifest names files and nothing else** — no paths and no
  execution id — because `read_artifact` takes a bare name and refuses anything else.
- **The supervisor forwards the SDK audit stream to the pod's stdout** (`4h6.45`), the only
  stream the cluster's logging agent collects. It holds the read end of the child's audit fd,
  applies the rate, byte and per-line caps there rather than in the SDK, matches every line
  whole against the record shapes and drops what does not match, and stamps
  `[user=…] [session=…] [execution=…]` from the tokens' `sub`/`sid`/`jti` — the prefix the SDK
  renders from the child's own environment is discarded, because the child owns that
  environment. **The env prefix and the signed claims are not the same evidence.** Every
  execution also emits a summary line, so a drop the *supervisor* made is a different line from
  an execution that produced no records. It is **not** distinguishable from suppression inside
  the child: `logger.disabled`, the level, a filter, handler removal (`4h6.12`) or rewriting
  `GENETICS_SDK_AUDIT_FD` before the first SDK call all leave a `records=0` summary
  byte-identical to a script that genuinely made no SDK calls, and containing that needs the
  child contained rather than read (`4h6.55`). What this does *not* establish is that the records are
  true: a script can emit well-formed records for calls it never made and can read through
  `_executor` with no record at all. For that, upstream attribution is what holds — db-api's
  and results-api's own `endpoint_access` lines carry the same three claims from the signed
  token and are written outside the sandbox.

### The supervisor process (`sandbox/supervisor.py`, `genetics-results-suite-4h6.39`)

The program the sandbox pod runs, and the thing whose absence made
`k8s/deployments/sandbox.yaml` unapplyable. Standard library only, plus what
`sandbox/requirements.txt` already ships — `sandbox/prune_venv.py` deletes pip and
everything outside the SDK's import closure, so a new dependency has to be added there
deliberately. `sandbox/Dockerfile` copies it to `/genetics/supervisor.py`; that does **not**
start it, because the image still ships no `CMD` and the manifest still declares no
`command`/`args`, which is what `scripts/deploy.sh` refuses to apply until
`genetics-results-suite-4h6.50` wires it up as the last bead of the chain.

- **The per-execution limits are built.** The wall clock and rlimits (`4h6.41`), the output
  caps (`4h6.42`), token delivery to the child (`4h6.43`) and the `/scratch` quotas, retention
  and reaper (`4h6.46`) landed together, sharing one poll loop and one kill path: a
  per-execution watchdog thread kills the process group (`_fire_limit` → `_kill_group`) and the
  reaper deletes completed executions on a 30 s tick (`reap_expired` → `_forget_retained`).
  The audit stream (`4h6.45`) followed, on a third drain thread sharing the same reaped-child
  deadline. `docs/code-execution-security.md` → "As built (`4h6.41`, `4h6.42`, `4h6.43`,
  `4h6.45`, `4h6.46`)" tabulates what each one is worth and what it measurably does not do.
  What is still absent is the missing `CMD` (`4h6.50`, above), without which nothing starts the
  supervisor at all.
- **The child is forked, never exec'd** — that is the whole return on `prewarm()`, whose
  pre-imported analysis modules the child inherits copy-on-write — and it therefore closes
  every inherited descriptor before running the script, or it would hold the listening socket
  and other users' in-flight connections. Two consequences of not exec'ing are handled rather
  than assumed away: the script can also write the status pipe it is left holding, so a
  status record is **ignored** when the child exited 0 and was not signalled (otherwise a
  script forges `status: "error"` on its own successful run); and the child's descendants
  inherit the output pipe, so the drain is bounded — see the next point.
- **An execution ends when the child is reaped, not when its pipes close.** A grandchild that
  `setsid()`s away keeps the write ends open and EOF never arrives, which used to hold the
  only execution slot on a pipe read after the child was long gone. The drain gets 2 s past
  `waitpid` and is then abandoned with a log line, and `duration_ms` is taken at the reap.
  Killing the escapee is `4h6.55`'s work and not the watchdog's — a `setsid()` descendant has
  left the process group `_kill_group` signals, which is measured, so a PID namespace per
  execution is what contains it; the slot is freed regardless.
- **Every writable path is per-execution.** `TMPDIR`, `HOME`, `MPLCONFIGDIR`,
  `XDG_CACHE_HOME`, `PYTHONPYCACHEPREFIX` and `SANDBOX_ARTIFACTS_DIR` are set in the child
  and point inside `/scratch/<execution-id>`. The Dockerfile leaves all of them unset on
  purpose: a fixed pod-wide value recreates the cross-execution shared directory that
  removing the pod-level `/tmp` was meant to prevent.
- **No `setuid` and no `chown` anywhere.** Supervisor and child share uid 65532 — forced,
  because the pod drops `CAP_SETUID`/`CAP_SETGID`/`CAP_CHOWN`. The image's advertised
  `SANDBOX_CHILD_UID` names a uid nothing in this pod can switch to.
- **`scripts/test-supervisor.py`** is the offline harness, in two modes:
  `python3 scripts/test-supervisor.py` (in-process, the fast path) and
  `python3 scripts/test-supervisor.py --container URL [--container-name NAME]` against a
  container started by
  `scripts/run-sandbox-local.sh` (see *Running the sandbox locally* under Operational
  procedures). Exit 0 pass / 1 a property broke / 2 could not run, in the same style as
  `test-sandbox-docs.py` and `test-network-policies.py`. It needs no cluster, no credentials
  and no image — it runs the real supervisor in the local interpreter against a temporary
  `/scratch` root and forks real children, so the queue-depth definition, the duplicate-id
  refusal, reject-don't-clamp on the timeout, the token consistency rules, the per-execution
  environment, which names reach the artifact manifest, the `429`'s `Retry-After` header that
  the client's retry policy reads, and the two forgeries the fork-without-exec model makes
  reachable — a status record written by the script, and a descendant holding the output pipe
  open — are exercised rather than asserted about. Container mode drives the same wire checks
  against the image and adds a group that has no in-process equivalent because it is about the
  image (read-only rootfs, no writable `/tmp`, pruned venv, the SDK and matplotlib importing,
  no credential in the child's environment). `--container-name NAME` is what lets the audit-stream
  group run there at all: those records leave by the container's **stdout**, not over the wire, so
  without a name to `docker logs` the group skips by name. `run-sandbox-local.sh --test` passes it.
  **Container mode is a partial run and its total is
  not a fraction of the in-process total**: the groups reaching into the supervisor's own
  objects (startup assertions, request parsing, queue, artifact manifest, startup wipe) have no
  route over HTTP and are **not run at all**, so the harness prints them by name under "check
  groups NOT RUN in this mode" and says so on the summary line. `skip()` is the narrower
  mechanism — a check inside a group that ran, such as one needing the harness's own view of
  `/scratch` — and those stay counted and listed individually. No build or deploy script runs
  either mode yet.
- **`scripts/test-e2e-local.py`** is the *other* harness and deliberately a separate file
  (`genetics-results-suite-4h6.49`): it needs the whole local stack up (db-api `:8080`,
  results-api `:2000`, the sandbox container) plus a signing key shared between chat-backend
  and both verifiers, and BigQuery behind db-api — preconditions that would take away
  `test-supervisor.py`'s "no cluster, no credentials, no image" property if folded in. It
  drives chat-backend's own `sandbox_token`/`sandbox_client` code end to end and asserts on
  what a 200 does not show: that the SDK's request appears in results-api's per-execution map
  under the token's `jti` (with an `INTERNAL_API_SECRET` caller measured in the same run as the
  200-with-no-accounting negative control), that the audit records carry the token's real
  sub/sid/jti while a forged `[user=…]` line does not parse as genuine, that `execution_id`,
  `/scratch/<id>` and both `jti`s are one value that joins across every log, that each limit
  returns a clean structured result to the client, that a grandchild left **in** the process
  group does not outlive the limit kill (the only path on which the supervisor signals the
  group at all), that an unset signing key sends nothing at all, and that artifacts are still
  present at half the container's own TTL and gone by `TTL + REAPER_POLL_S`. It refuses to
  start unless the running container's `/genetics/supervisor.py` and `prewarm.py` are
  byte-identical to `sandbox/`'s, and every skip is listed under `NOT MEASURED` and named in
  the exit banner, because a green exit is not a claim that everything was measured. Measured
  2026-08-17 with a container started as `SANDBOX_RETENTION_S=45`:
  `scripts/test-e2e-local.py --retention-s 45` → **49 checks passed, nothing skipped** (48
  without the flag, which only cross-checks the retention the harness reads off the container
  itself). Against a container with no `SANDBOX_RETENTION_S` the retention group skips and the
  run reports `NOT MEASURED (1)` with a `PARTIAL:` banner, still exiting 0. The
  run also **reproduces `genetics-results-suite-4h6.55`**: a `setsid()` grandchild is still
  resident after the execution is killed. Details in
  [docs/code-execution-security.md](code-execution-security.md) §2, *As built (`4h6.49`)*.
- **Three environment differences are what "not the image" looks like**, each warned about
  loudly at startup rather than silently changed: `GENETICS_PREWARM` unset skips `prewarm()`,
  `GENETICS_MPLCACHE` unset leaves the font cache to be rebuilt, and `SANDBOX_SCRATCH_ROOT`
  moves the scratch root for tests — which makes artifacts unretrievable, since
  `read_artifact` hardcodes the `/scratch/` prefix. The image sets the first two and never
  sets the third.

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

### Programmatic credentials: why the per-user API key, not the Google id_token

Decided in `genetics-results-suite-fdd`; recorded here so it is not re-opened cold.

**What `GOOGLE_TOKEN_AUDIENCE` actually buys.** Its deployed value is
`32555940559.apps.googleusercontent.com`, the *public* gcloud CLI OAuth client id. Every
`gcloud auth print-identity-token`, run by anyone anywhere, mints a token with that `aud`, so the
check binds the token to no application of ours. It is worth exactly **cross-OAuth-client replay
protection**, and the distinction matters: without it (`verify_oauth2_token` skips `aud` when none
is passed) any Google-signed id_token with an allow-listed email is accepted, including one minted
for a *different* OAuth client — ADC's `764086051850-…`, a project-owned client id, an unrelated
app's own client. With it, those are rejected. What it does **not** cover is the likeliest replay
case: every other homegrown service that documents the same `gcloud auth print-identity-token`
flow receives a token carrying this exact `aud`, so a token a finngen.fi user handed to one of
those services passes the check here unchanged and is replayable by that service's operator. It is
*not* an identity gate. All authorization on the id_token path rests on the `finngen.fi` domain
allow-list. The hazard being corrected is the belief that it does more — a control that appears to
bind and does not is worse than an absent one, because people reason about the system's safety
assuming it works. Measured 2026-08-10 from Cloud Logging: in the preceding 90 days it had
rejected nothing (zero `token audience not allowed` log lines).

**Why re-pointing the audience at a project-owned client id was rejected.** `gcloud auth
print-identity-token` on *user* credentials cannot request a custom audience at all (only service
accounts can). Setting a project-owned client id would therefore 401 every human caller while
changing nothing for a service account, which does not use this path today. `scripts/deploy.sh`
carries the same note next to the export.

**The decision: push programmatic users to per-user API keys.** The intended boundary is now a
credential *this deployment issues and can revoke* — the per-user API token, created from the
browser's "MCP and API keys" dialog, validated by results-api and mcp-server against the token
store, rolling 90-day idle expiry. It is already the dialog's primary path and already works for
both `/api` and `/mcp`.

**This is a migration, not a removal.** The Google-id_token verification branch stays in place and
`GOOGLE_TOKEN_AUDIENCE` keeps its value; only the *guidance* changed. Measured 2026-08-10 from
**Cloud Logging** (an earlier attempt to re-derive these numbers from the BigQuery `endpoint_access`
sink disagreed, but only because it queried the wrong table: `genetics_api_logs.genetics_results_api`
is the dev VM's test suite, where `user_email` is always NULL. The sink neither drops `user_email`
nor lags — `genetics_api_logs.stdout` carries it on 12,121 of 53,216 results-api rows and lands rows
within ~90 seconds of wall clock — so `stdout` is the table to cross-check against, mind the
`log_source` rename noted under "Log sinks"): over the preceding 90 days, non-`mcp-tool` traffic was 1,943 requests
from six `@finngen.fi` people, at least three of them programmatic — and the request logs record no
credential type, so it is impossible to tell which of them are on the id_token path. Deleting it
would strand callers we cannot enumerate. Nothing automated depends on
it: no CI, no cron, and no code in the suite calls `fetch_id_token`, `IDTokenCredentials` or the
metadata `identity?audience=` endpoint (the monitor CronJob uses `INTERNAL_API_SECRET`).
results-api has no Keycloak bearer path and there is no Keycloak in this deployment, so Keycloak is
not the alternative here.

**Known gap: a headless caller cannot mint its own key.** `POST /chat/v1/tokens` is
`Depends(auth_required)` (`genetics-mcp-server/src/genetics_mcp_server/routers/api_tokens.py`),
which in production requires the internal-secret marker **plus** an allow-listed oauth2-proxy
identity header; a request carrying only a bearer token lands in case 4 of `auth_required` and gets
401, and the auth-gateway's `location /chat/v1/` fronts it with `auth_request /oauth2/auth` anyway.
So a bearer token — API key or id_token — cannot mint an API key, and a CI job or service account
with no interactive Google session has **no path to the recommended credential on its own**: a
human must sign in through the browser once and create the key for it. After that the key works
headlessly and indefinitely under the rolling 90-day idle expiry. Anyone still on the deprecated
id_token path who is fully headless should plan for that one human step.

Sites that document the deprecation: `README.md` ("Programmatic API access"), `scripts/deploy.sh`,
the `GOOGLE_TOKEN_AUDIENCE` bullet under Authentication above, `genetics-results-browser`'s
`src/features/page/McpTokenDialog.tsx` (both the MCP-access and API-access boxes),
`genetics-results-api`'s `docs/project-spec.md` (Tech Stack and Authentication), and
`genetics-mcp-server`'s `.env.example` and `docs/project-spec.md`.

- Network policies enforce db-api is only reachable from chat-backend, mcp-server and the sandbox, and rag-service only from chat-backend and mcp-server
- **Every ingress rule is source-scoped; none is `from`-less.** A rule carrying `ports` with no `from:` admits *every* pod in the namespace, and six of them did. `genetics-results-suite-fad` fixed results-api and bff, `genetics-results-suite-k4t` fixed the other four. Current admitted sources (`k8s/network-policies/policies.yaml`):

  | target | port | admitted by this file |
  |---|---|---|
  | frontend | 3000 | auth-gateway (`location /`) |
  | bff | 5000 | auth-gateway (`location /api/`) |
  | results-api | 4000 | auth-gateway (`@api_bearer`), bff, chat-backend, mcp-server, sandbox |
  | chat-backend | 8000 | auth-gateway (`/chat/v1/`, `= /status`), results-api, mcp-server (both POST `/chat/v1/tokens/validate`) |
  | mcp-server | 8080 | auth-gateway (`/mcp`, the two `/.well-known/oauth-protected-resource` paths) |
  | auth-gateway | 8080 | `ipBlock` 35.191.0.0/16 and 130.211.0.0/22 — **not** a podSelector |
  | oauth2-proxy | 4180 | auth-gateway |
  | db-api | 8080 | chat-backend, mcp-server, sandbox |
  | rag-service | 8000 | chat-backend, mcp-server |
  | sandbox | 8080 | chat-backend — **and nothing else**; see the sandbox bullet below (`sandbox-policy.yaml`) |

  The monitor CronJob is admitted **additively** by `monitor-policy.yaml` for results-api, chat-backend, mcp-server, db-api and frontend. NetworkPolicies union, so those rules are load-bearing: they are not redundant with the table above and deleting them locks the monitor out. The monitor never dials auth-gateway, which is why that one has no monitor rule.

  Only auth-gateway needs an `ipBlock`. Everything else is ClusterIP with no Ingress backend, and kubelet probes are exempt from NetworkPolicy on this cluster's ADVANCED_DATAPATH (Dataplane V2) — proven by db-api and oauth2-proxy, which have httpGet probes, podSelector-only policies and no restarts.
- **The sandbox: the namespace's only egress policy** (`k8s/network-policies/sandbox-policy.yaml`, `genetics-results-suite-4h6.8`; full rationale in `docs/code-execution-security.md` sections 3 and 5). Every other pod here has unrestricted egress — `default-deny-ingress` has no egress counterpart, and egress becomes deny-by-default for a pod only once a policy selecting it lists `Egress` in `policyTypes`. Two objects, both selecting `app: sandbox`:
  - `allow-ingress-sandbox` — **chat-backend only**, 8080/TCP. This is **layer 2 of the three MCP-exclusion layers**: the user's requirement is that code execution must not be reachable via MCP, and omitting `run_analysis`/`read_artifact` from mcp-server's tool registration (layer 1) is a runtime-assembled set one refactor away from being undone, so mcp-server is denied at the network layer too. The precedent is in this very table — mcp-server sits on both sides of the db-api rule, so "anything that can drive mcp-server can reach BigQuery behind it"; the same shape applied to code execution would mean anything that can drive mcp-server can run code. `monitor-policy.yaml` is deliberately **not** extended to the sandbox.
  - `sandbox-egress` — **db-api:8080 and results-api:4000 only**, as two separate rules so the destinations and ports do not cross-product. No `ipBlock` of any kind: no internet, so no `pip install`, no mining payload, and no `pl.write_parquet('s3://…')` — polars ships the Rust `object_store` with its own HTTP/TLS stack, so this policy is the *only* control that closes s3. Also denied: keycloak, keycloak-postgres, chat-backend and rag-service (the sandbox is a leaf — it is called, it does not call back, so a script cannot re-enter the chat API with the caller's session), mcp-server, and the Kubernetes API server.
  - **No DNS rule, deliberately.** Egress to CoreDNS sustains ~200 KB/s of exfiltration through query names alone (~10³ queries/s × ~200 usable base32 bytes), needing no POST and no response — tens of megabytes inside the 120 s wall clock, and a ~1 KB stolen token in five queries. Name resolution is done with `hostAliases` pinning db-api and results-api to their ClusterIPs in all four name forms instead. Two hard consequences: the image must ship `/etc/nsswitch.conf` with `files` before `dns` (otherwise glibc defaults to DNS-first and every lookup *hangs* through the resolver timeout budget, unrepairable under `readOnlyRootFilesystem`), and `GCE_METADATA_HOST` is pinned to a literal IP so any `google.auth.default()` probe fails fast instead of stalling.
  - **The metadata server (169.254.169.254) is NOT demonstrably covered by this policy.** No rule permits it, but link-local/node-local traffic is exactly the class already proven *exempt* from NetworkPolicy on this dataplane in the ingress direction (kubelet probes), and the egress direction has not been tested here. The load-bearing metadata defence is the node pool's `GKE_METADATA` mode with no Workload Identity binding for the sandbox KSA, not this file.
  - **Label contract, owned by this policy:** pod label `app: sandbox`, container port **8080/TCP**, Service `sandbox` (8080 → targetPort 8080). NetworkPolicy `ports` are pod ports, not Service ports. A podSelector matching no pod is not an error — it is silent no-coverage, and here it would yield a sandbox with *unrestricted* egress.
  - **Reverse direction, and why it is currently a dead path.** An egress allow-list is necessary but not sufficient: `default-deny-ingress` drops the connection at the *receiving* end, so db-api's and results-api's own ingress rules both gained an explicit `app: sandbox` entry (that is why both rows above changed). The sandbox holds neither shared secret **by design**. Since `genetics-results-suite-4h6.9` the path is opened by a *credential*, not by widening the network rule: both services now also accept a short-lived, audience-bound sandbox token minted per execution by chat-backend (see the sandbox-execution-token bullet under Authentication). Without one, db-api still 401s a request lacking `Authorization: Bearer $INTERNAL_API_SECRET`, and results-api still 401s a request carrying neither the trusted-proxy marker nor a valid bearer.
  - **`scripts/test-network-policies.py`** asserts all of the above from the repo alone, with one deliberate exception noted below (the live sandbox probe): it parses every file in `k8s/network-policies/` (union semantics — the mcp-server property cannot be read off one file), and checks that no rule selecting the sandbox admits mcp-server or the monitor, that the only pod admitted at all is chat-backend — swept over the **whole** inventory of apps the suite runs (the harness's `KNOWN_APPS` list, its pod-label table, *and* the `app` labels it finds in `k8s/deployments/`), because a sweep over the pod-label table alone silently passed a sandbox rule admitting `app: frontend`, `keycloak`, `keycloak-postgres` or `oauth2-proxy` — that no sandbox rule is `from:`-less, that the egress allow-list is exactly the two destinations with no `ipBlock` and no port 53, and that db-api and results-api admit the sandbox on the right ports. It also **discovers** the sandbox workload anywhere in `k8s/deployments/` by a *union* of independent tells — file name, object name, the pod's `app` label value, and the label contract itself — so `sandbox.yaml`, `sandbox-deployment.yaml`, a Deployment/Service split across two files, and a renamed file whose pod still carries `app: sandbox` all activate it, while a pod labelled `app: sandbox-runner` is still found by name so the contract check can *fail* on it rather than not see it (the label branch reads the `app` value only, never the stringified label dict — matching that adopted any pod carrying an unrelated key such as `sandbox-client: "true"`, which turned a working control into cascading false failures). Because no set of tells is exhaustive, two **locks** catch a workload that evades all of them: `SANDBOX_ENABLED` on with no discoverable sandbox is a contradiction (the deploy-ordering contract flips the flag in the same commit that lands the workload), and any workload in `k8s/deployments/` that carries one of the sandbox's **forced** pod-spec tells while being recognised by no discovery tell is refused until it is classified. Those tells are `runtimeClassName` set at all, a toleration for `sandbox.gke.io/runtime` (GKE taints the gVisor pool, and the sandbox is the only pod that may tolerate it), and a `serviceAccountName` that is present and is not `genetics-suite` (absent is *not* a tell — bff, frontend, keycloak, postgres and oauth2-proxy declare none). `automountServiceAccountToken: false` **was** a fourth tell and was removed in `genetics-results-suite-o5i`: auth-gateway adopted it as ordinary hardening, which made the lock abort the deploy over a pod that had merely been improved — and it is a control other workloads should adopt too, so keeping it would tax exactly the change the suite wants. Keying the lock on tells rather than on unknown `app` labels is deliberate: the tells are forced by the node pool's taint and by the credential guarantee, so nothing ordinary declares one, whereas the earlier `KNOWN_APPS` form made every added service arrive as `ERROR: network-policy checks failed; refusing to apply network-policies/` — a check that taxes routine work is deleted rather than fixed. Discovery and both locks cover every kind that carries a pod template — Deployment, StatefulSet, DaemonSet, ReplicaSet, **Job, CronJob and bare Pod** — with the CronJob's template read at `spec.jobTemplate.spec.template`, since a sandbox landing as a Job or a Pod would otherwise be invisible to discovery *and* to the lock that backstops it. It cross-checks the discovered workload against the label contract, and — once a workload exists — that **both `db-api.yaml` and `results-api.yaml` set `SANDBOX_ENABLED: "true"`**: nothing else couples the flag to the sandbox existing, and with the sandbox deployed and the flag still `"false"` the `sys.exit(1)` assertion never fires and a script that simply omits `Authorization` lands in db-api's fail-open branch. Both workload-dependent checks are inert with a printed note until a sandbox Deployment or Service lands. Since `4h6.7` landed `k8s/deployments/sandbox.yaml`, "the file exists" and "the pod runs" are no longer the same thing — deploy.sh applies it only when `ENABLE_SANDBOX=true` — so that last check (and **only** that one; every static check runs against the manifest either way) is relaxed when the sandbox is not running. **It is keyed on the cluster, not on `ENABLE_SANDBOX`.** The env var says only that *this run will not apply* the sandbox — deploy.sh **skips** `sandbox.yaml` when the gate is off, it never deletes it — so after one gate-on deploy, any later deploy from a worktree or with `terraform.tfvars` unreadable (`SKIP_TERRAFORM=true`, the documented path for exactly that) runs gate-off against a live, serving sandbox, and relaxing on the variable alone would silence the one check that stops db-api shipping `SANDBOX_ENABLED=false` underneath it — the fail-open branch where a script that omits `Authorization` is authorized with no `sub`/`sid`/`jti`. So with the gate off and a sandbox workload in the directory the harness runs `kubectl get deployments -n genetics -o name` (its **only** cluster call; everything else stays offline) and: a live sandbox Deployment → **fail**, naming the mismatch; none → relax with a note; `kubectl` present but unable to answer → **fail closed**, because the live state is then a guess; `kubectl` absent from `PATH` → relax, saying so explicitly in the note, since deploy.sh does everything through kubectl and cannot have reached the harness without it, so that case is a manual offline run by construction. `ENABLE_SANDBOX=true` is not a way around any of it — it makes the check strict rather than relaxed. Four properties of the parser are load-bearing: `policyTypes` is **inferred** when a policy omits it (the API server does the same — an `egress:` section implies Egress, and every spec implies Ingress), otherwise a policy admitting mcp-server with the field left off would be enforced by the cluster and invisible to the harness; any peer that is not `podSelector`-only is **refused rather than interpreted**, because `- namespaceSelector: {}` admits every pod in every namespace and cannot be resolved without live Namespace objects; **every** peer of a `from:`/`to:` list is evaluated rather than short-circuited on the first match, so a refused peer sitting behind a matching one (`from: [podSelector chat-backend, ipBlock 0.0.0.0/0]`) still raises instead of passing unseen; whether a selector is widened to a **superset** match or narrowed to an exact one is chosen per call site by the **polarity of the assertion**, not per function — a must-NOT-reach check (mcp-server, the monitor) widens, so a peer such as `{app: mcp-server, role: tools}` counts as reaching and the check fails closed, while a must-REACH check (db-api/results-api admitting the sandbox) narrows, because widening there would report a dead path as live once `4h6.7` makes the pod's label set fully known; and a policy selector is matched against the sandbox as a **superset** — until `4h6.7` lands a manifest the harness knows only the contract labels, not the pod's full label set, so `matchLabels: {app: sandbox, tier: untrusted}` is treated as selecting the sandbox (it would, if the pod carries `tier`) rather than skipped, which would take that policy out of every check below in silence. `scripts/deploy.sh` runs the harness immediately before `kubectl apply -f network-policies/` and **aborts the deploy on exit 1** (a broken control), warning only on exit 2 (harness could not run — no PyYAML, or a manifest directory that is missing or does not parse, which is routed to 2 rather than 1 so an unreadable file is never reported as a broken control) — this is the only place it runs, as the repo has no CI and the pre-commit hook runs only the doc-drift check (see "Documentation-drift hook" below). The **live** test — opening a connection from the mcp-server pod to the sandbox Service and confirming it fails — is deferred to the deploy window and has not been run (`genetics-results-suite-4h6.26`, which also covers the metadata-server and ClusterIP-translation questions).
- **Why auth-gateway takes an `ipBlock` and no node CIDR.** It is the only Ingress backend (both `genetics-suite` rules point at it) and the only NodePort Service, but it is fronted by a **NEG** (`cloud.google.com/neg: {"ingress":true}`, NEG `k8s1-35278419-genetics-auth-gateway-8080-ec38d214` reported HEALTHY on the Ingress). Container-native load balancing means the GFE connects straight to the pod IP, so there is no NodePort hop and nothing is SNAT'd to the node address — the intuitive "add the node CIDR for NodePort SNAT" is wrong here. Confirmed from the nginx access log: health checks *and* real user traffic both arrive from 35.191.0.0/16, kube-probe arrives from the link-local 169.254.4.6, and nothing else appears at all. Note that this is the GFE's own address, not the client's — the GFE does not preserve the client IP (an external scanner logged as 35.191.151.104), and the real client is only in `X-Forwarded-For`, so no client IP can be source-filtered at this layer. The NEG path is what keeps the source out of the node CIDR, not source preservation. `130.211.0.0/22` is Google's other documented LB/health-check range and is admitted defensively. The node subnet is `finngenie-subnet` **10.0.0.0/20** (pods 10.16.0.0/14, Services 10.20.0.0/20); it is recorded only because losing the NEG annotation would make it suddenly required, and losing the site is the failure mode.
- **chat-backend applies the same trusted-proxy marker rule** (`genetics-results-suite-th2`, was a P1 hole). `auth/core.py:get_authenticated_user` honours `X-Goog-Authenticated-User-Email` only when the request also carries the marker, and holds the asserted address to `ALLOWED_EMAILS`/`ALLOWED_EMAIL_DOMAINS`; `auth/dependencies.py:auth_required` follows results-api's precedence exactly (marker + allow-listed header → that user; marker + non-allow-listed header → 401, never a downgrade to `mcp-tool`; marker alone → `mcp-tool`; header alone → 401). `auth/core.py:is_internal_caller` is the single place the secret is compared, and it accepts the marker in either transport: `X-Internal-Auth: $INTERNAL_API_SECRET` (auth-gateway's, on the only two locations that proxy to chat-backend — `location /chat/v1/` and `location = /status`) or `Authorization: Bearer $INTERNAL_API_SECRET` (results-api's and mcp-server's, unchanged). Both compare as bytes, since `hmac.compare_digest` on `str` raises `TypeError` — a 500 — for a non-ASCII value; the presented value is re-encoded **latin-1** and the configured secret **UTF-8**, over a secret the service now refuses to start with unless it is ASCII — see `genetics-results-suite-ctq` below. `POST /chat/v1/tokens/validate` is the one route with no auth dependency; it calls the same helper and additionally refuses any request that carries an identity header at all, since its genuine callers are service-to-service and never assert one. Before this, forging the header granted admin (`ENABLE_ADMIN_PAGE=true`, membership tested against `ADMIN_USERS` on the forged string), read every user's chat transcripts, and minted a plaintext per-user API token via `POST /chat/v1/tokens` — which mcp-server and results-api both accept, so it pivoted into both. `GET /chat/v1/auth` is `@is_public` and reflects the identity, so it was an unauthenticated admin-membership oracle; it needed no change of its own because it resolves through the same `get_authenticated_user`. **mcp-server does not share the identity-header half of this bug**: it reads no identity header on any path and its ASGI gate fails closed to 401 without a `Bearer`. It did carry the `compare_digest`-on-`str` half, in its own bearer gate rather than in `auth/core.py` — see the mcp-server bullet below. Two deliberate deviations from results-api:
  - `REQUIRE_AUTH=false` (local dev only; prod sets `true`) still honours the header as-is. That mode already authenticated everyone as `anonymous`, so a marker would protect nothing and only break developing as a named user.
  - The allow-list **fails open when neither `ALLOWED_EMAILS` nor `ALLOWED_EMAIL_DOMAINS` is set**, warning as it does. chat-backend only started reading them here, and the code default is `finngen.fi`; enforcing that default on a pod that had not yet picked up the `bearer-auth-allowed` ConfigMap would lock out every user of any other deployment. `k8s/deployments/chat-backend.yaml` now `envFrom`s that ConfigMap (the same terraform-rendered values oauth2-proxy itself uses, so it cannot refuse anyone oauth2-proxy admitted), which is what makes the check live in production. The marker is the half that closes the hole; the allow-list is defence in depth against a compromised holder of `INTERNAL_API_SECRET`.
- **db-api compares the shared secret as bytes** (`genetics-results-suite-zyi`). `api/main.py:require_auth` ran `hmac.compare_digest` on two `str`s, which raises `TypeError` for a non-ASCII value and surfaced as a **500 rather than a 401**. This was the **third of four** instances in the suite (`genetics-results-suite-fad` fixed results-api, `genetics-results-suite-th2` chat-backend, and the fourth — mcp-server's own ASGI bearer gate — is the bullet below; the zyi bead's "last known instance" wording predates finding it), and unlike the bead's own note it was **reachable, not latent**: the sandbox branch runs first but *declines* a non-ASCII bearer (it is not `alg: HS256`-shaped), which is exactly what let it fall through to the comparison — measured at HTTP 500 pre-fix, 401 after. httpx refuses to encode a non-ASCII header value, so the regression test in `tests/test_api_auth.py` sends the bearer as raw bytes, which is what a client that is not httpx puts on the wire and what starlette latin-1-decodes back into a non-ASCII `str`. `pyproject.toml` also pins `PyJWT==2.13.0` exactly (was `>=2.10`), matching the exact-pin convention of `genetics-mcp-server` (`2.11.0`) and `genetics-results-api` (`2.12.1`) on the one dependency that validates credentials. The ≥ 2.10 floor is still satisfied, and what it buys is the rejection of a **post-dated** token — 2.10 is where `iat > now` became `ImmatureSignatureError`, which is why `sandbox_auth.LEEWAY_SECONDS` exists to absorb minter/verifier clock skew. The opposite property, **anti-backdating**, does not come from PyJWT at all: PyJWT accepts an arbitrarily old `iat` as long as `exp` is in the future, and `sandbox_auth.verify_sandbox_token`'s own `iat < now - MAX_TOKEN_AGE_SECONDS` check (exact, no leeway) is what enforces it, at every PyJWT version.
- **mcp-server's ASGI bearer gate compares the shared secret as bytes** (`genetics-results-suite-zyi`, the **fourth** instance). `mcp_server.py:_wrap_with_bearer_auth._token_is_valid` ran `hmac.compare_digest` on two `str`s against every configured `MCP_API_KEY`, and `api_keys` is non-empty whenever the wrapper is installed, so a non-ASCII bearer always reached at least one comparison and raised `TypeError`. `genetics-results-suite-th2` had already converted chat-backend's `auth/core.py` to bytes; this call site was missed because it is the SSE/HTTP transport gate, not the FastAPI dependency. It is raw ASGI middleware with no exception handler above it, so the failure was a bare 500, not a 401. The same line's `.decode()` of the raw `authorization` header bytes was a second 500 on the same path — an `Authorization` value that is not valid UTF-8 (`b"Bearer \xff"`) raised `UnicodeDecodeError` before the comparison — and is now caught and treated as an absent credential. Both are pinned by tests in `tests/test_mcp_server.py` that build the ASGI scope directly with raw header bytes, since httpx refuses to encode a non-ASCII header value and a test written the obvious way fails in the client instead of the server. The same branch's `?token=` fallback carried the *identical* undecodable-bytes 500 one line further down — `scope["query_string"].decode()`, also unguarded — fixed the same way by `genetics-results-suite-tzi` and pinned by its own scope-built test. That one really was latent, unlike zyi's: nothing **sets** `MCP_ALLOW_QUERY_TOKEN` anywhere. Re-derived by grepping the whole of `genetics-mcp-server` and this repo, it appears in **seven files**, none of them a live assignment — `mcp_server.py` (three occurrences: the `os.environ.get`, the warning logged when it is on, the comment on the fallback), a **commented-out** `.env.example` line, `tests/test_mcp_server.py` (four occurrences: two `monkeypatch.setenv` calls — the `?token=` acceptance test and `tzi`'s own undecodable-query-string test — plus two docstrings), `genetics-mcp-server/docs/project-spec.md` (env-var table and branch 1), that repo's `CLAUDE.md` doc-ownership row, its `scripts/check-doc-drift.sh`, and these docs (here plus the monitor ignore-list note above). The earlier wording — "nowhere but the code, a commented-out `.env.example` line and these docs" — missed the tests and all three doc-ownership references. The deployed `mcp-server` Deployment's only `envFrom` is the three-key `bearer-auth-allowed` ConfigMap (verified against the live cluster, not just the manifests). This gate's comparison still encodes **UTF-8 on both sides**, deliberately and unlike the three below, and **no starlette is involved**: it decodes the raw scope header bytes itself, with UTF-8, so re-encoding with UTF-8 reproduces the wire bytes exactly and an undecodable value never reaches the comparison at all. Switching it would not tighten or broaden the accepted set — switching only the encode would raise `UnicodeEncodeError`, the very 500 the bytes comparison exists to prevent, and switching decode and encode together accepts exactly the same tokens (the expected side is valid UTF-8 by construction) and only moves where an undecodable value is rejected.
- **The secret must be ASCII, and now the services enforce it; the presented credential is re-encoded latin-1** (`genetics-results-suite-ctq`, all three shared-secret comparisons at once — db-api `api/main.py:require_auth`, results-api `app/core/auth.py:is_internal_caller`, chat-backend `auth/core.py:is_internal_caller`). Starlette decodes raw header bytes as **latin-1**, so re-encoding the presented credential with UTF-8 compared *mojibake* rather than the str starlette had produced: a wire bearer of `b"Bearer s\xc3\xa9cret"` arrived as `"Bearer sÃ©cret"` and re-encoded to `b"s\xc3\x83\xc2\xa9cret"`. Verified empirically against each repo's pinned starlette — 1.6.0 (db-api), 0.47.3 (results-api), 0.50.0 (mcp-server) — all three decode latin-1. The presented side is now `.encode("latin-1")`, which undoes that decode exactly; the *expected* side stays `.encode("utf-8")`. **The two codecs differ on purpose** and each site carries a comment saying so.
  - **The original justification was false and is corrected here and in all three source comments.** "Callers transmit it UTF-8-encoded" is not true of this fleet. Measured off a real socket: **node fetch/undici — which is the browser BFF — and python `requests`/`http.client` put latin-1 on the wire; `aiohttp` puts UTF-8; and httpx 0.28 (mcp-server's and chat-backend's client) refuses a non-ASCII header value outright with a client-side `UnicodeEncodeError`, so it cannot send one in any encoding.** Because the clients disagree with *each other*, byte-exactness across all callers is **unachievable** — there is no codec choice that is correct for all of them, which makes the codec question moot and the ASCII invariant the real answer.
  - **Stated plainly, because it is the honest record**: under a hypothetical non-ASCII secret this change *flips which wire bytes authenticate*. Confirmed end to end against db-api's real app: `b"Bearer s\xe9cret"` was OLD=authenticated, NEW=401; `b"Bearer s\xc3\xa9cret"` was OLD=401, NEW=authenticated. The callers that would have worked before are exactly the BFF-shaped ones that would 401 after, and the encoding the new code accepts is the aiohttp-shaped one that no client in this fleet emits. That is not a regression anywhere today only because no such secret exists.
  - **So the ASCII invariant is enforced, not documented.** The bead's own suggestion — "document that the secret MUST be ASCII and add a startup assertion" — was initially rejected as a new crash path for a configuration nobody has; the measurements above reverse that. All three services now refuse a non-ASCII `INTERNAL_API_SECRET` at startup with a message naming the variable and saying that HTTP clients disagree on encoding non-ASCII header values: results-api `app/config/common.py:require_ascii_internal_secret` (called where the variable is already read, at import), db-api `api/main.py:_require_ascii_secret` (at import, beside the existing unset warning), and chat-backend/mcp-server inside the **existing** `config.require_internal_api_secret(component)` from `genetics-results-suite-618` rather than a parallel mechanism. **The guard never fires on an absent or empty secret** — that is the dev/test configuration and several suites depend on it (mcp-server's unset case still gets 618's own message, and the paths that legitimately hold no credential, a local run and the sandbox image, never call the helper). The failure mode is deliberately the good one: a bad secret **stalls the rollout with the old pods still serving** — the new pod never passes readiness — instead of every internal call 401ing mysteriously at request time.
    - **In db-api the invariant is upheld on every request too**, since `genetics-results-suite-xi6` moved the secret out of a module global into `_internal_api_secret()`, which reads the environment and checks it in the same accessor, so the value `require_auth` compares against is always one that has been checked. **The two times fail differently on purpose**: the import-time call raises, which is the rollout-stalling fail-fast described above; the request-time check *fails closed with a 401 and never raises*, because a RuntimeError out of a FastAPI dependency would 500 every call — including one presenting the correct credential — on a pod that stays Ready, since `/health` returns before the read. A non-ASCII value can only reach the request-time read via an in-process `os.environ` mutation, which no deployment performs.
    - **That accessor is tri-state**, and the empty case is not simply fail-open: a non-empty ASCII secret means compare against it, `""` means no secret is configured and none ever was in this process (the documented fail-open dev shape), and `None` means refuse, which `require_auth` turns into a 401. **Which of the latter two an empty variable yields is decided by a one-way latch** (`_authentication_was_configured`, seeded at import from the startup read, set on any non-empty ASCII read and never cleared): a runtime environment change may **enable** authentication but can never **disable** it, so a process that has once observed a secret answers 401 — not fail-open — if the variable is later emptied or deleted.
  - Every deployed secret is ASCII (`scripts/create-secrets.sh` uses `openssl rand -base64 32`), where all codecs coincide, so nothing observable changes and no new crash path opens for any existing deployment. The bug originally filed was fail-**closed** (a 401 for the legitimate holder of a non-ASCII secret, never a bypass).
  - results-api needed one extra change, because it has a second producer of the string: `middleware_usage_logging._extract_user_from_header` decoded the raw header bytes itself as `utf-8, errors="ignore"` and now decodes latin-1 like starlette and like `app/middleware.py`'s sandbox peek. The justification is **not** a new 500 — the same commit's `try/except UnicodeEncodeError` in `is_internal_caller` makes that unreachable — but (i) the two producers of the string `is_internal_caller` receives must **agree**, or the usage log and the auth path answer differently about who called, and (ii) `errors="ignore"` **silently dropped undecodable bytes**, so the old path matched `b"Bearer <secret>\xff"` as internal and attributed a user while starlette's latin-1 path rejected the identical request. It is a strict tightening.
  - **The `try/except UnicodeEncodeError` asymmetry is deliberate and now commented.** Only results-api guards the re-encode, because only its `is_internal_caller` takes a `str`, so a direct caller can hand it anything; db-api's and chat-backend's take a starlette `Request` and can only see a value starlette itself latin-1-decoded, which re-encodes by construction. Both of those now carry a comment saying so and telling the next person to add the guard if a str-taking entry point appears.
  - Pinned in each repo by a test with a **non-ASCII secret**, by a test that the startup guard fires on non-ASCII and stays silent on ASCII and on absent, and — the case that actually changed — by a test built on a **hand-built ASGI scope** that pins which raw wire bytes authenticate. That last one **cannot** be written with `TestClient`: `starlette/testclient.py` does `value.encode()` (UTF-8) on httpx's already-decoded header str, so latin-1 wire bytes are silently rewritten before reaching the app and every case collapses into the UTF-8 one — a validator was briefly fooled by exactly this. Each such test carries that note.
- **`GET /api/v1/auth` on results-api is not an identity oracle** (`genetics-results-suite-r0l`, measured, no code change). The route is one of the seven `@is_public` routes above, so it answers a caller with no credential at all — the shape that made chat-backend's `GET /chat/v1/auth` an unauthenticated `ADMIN_USERS` oracle before `genetics-results-suite-th2`. Probed against the real app (full middleware stack, and again over a real socket): no headers → `{"authenticated": false, "user": null}`; **forged identity header alone → the same, the address is not reflected**; marker + allow-listed identity → that address, normalized; marker alone → `authenticated: false`, since the handler asks `get_authenticated_user`, which only ever answers about the proxied *person*, not the service. `tests/test_auth_endpoint_identity.py` pins all four offline so the property stays a test rather than a re-measurement.
- **Container privileges**: `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]` and the `RuntimeDefault` seccomp profile are set on **the containers of the suite's own services** — `results-api`, `chat-backend`, `mcp-server`, `bff`, `db-api`, and (since `genetics-results-suite-eau`) both containers of `auth-gateway` — which since `genetics-results-suite-a7n` also add `runAsNonRoot` (uid 101) and `readOnlyRootFilesystem`, and are the only suite containers outside the sandbox to set `readOnlyRootFilesystem` at all. `runAsNonRoot` is **not** exclusive to them — `db-api` and `bff` set it too (next bullet); only the read-only rootfs is. The **sandbox** is hardened further still — `runAsNonRoot`, uid 65532, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem`, a dedicated KSA, `automountServiceAccountToken: false` and `enableServiceLinks: false`, with exactly one writable path (a 512Mi `emptyDir` at `/scratch`) and no other mount of any kind (see `docs/code-execution-security.md` § 2 and "The sandbox Deployment" above). Since `genetics-results-suite-4h6.7` it **does** have a manifest, `k8s/deployments/sandbox.yaml`, so it is grepped like everything else — but it is applied only when `ENABLE_SANDBOX=true`, so its presence in this directory does not mean a hardened pod is running. The controls are **not** set on the third-party and support workloads: `frontend`, `oauth2-proxy`, `keycloak`, `postgres`, `rag-service`, and the `monitor`, `analyze-conversations` and `keycloak-postgres-backup` CronJobs — **eight** workloads — all still run with the container defaults. Do not restate this as "every container". `grep -rn allowPrivilegeEscalation k8s/` (**`k8s/`, not `k8s/deployments/`**: the backup CronJob lives in `k8s/cronjobs/`, and a `k8s/deployments/`-scoped grep is exactly how it got left off this list once already) re-derives only the **hardened** side. The non-hardened side is that set's **complement**, so it cannot be grepped for at all — derive it by listing every workload manifest in `k8s/` and subtracting the hardened ones.
  - `db-api`, `bff` and `auth-gateway` additionally run `runAsNonRoot` (uid 10001 / 1000 / 101) since they write nothing outside their image (auth-gateway writes only into two `emptyDir`s); results-api, chat-backend and mcp-server still run as root because they raise `ulimit`, shell out to `gcloud`, cache tabix indexes, or own root-owned files on the `chat-data` PVC. chat-backend sets `fsGroup: 1032` so the pre-existing SQLite files stay writable once `CAP_DAC_OVERRIDE` is dropped.
  - **auth-gateway runs its whole pod as uid 101** (`genetics-results-suite-a7n`), which is what removes the capability exception rather than justifying it. `nginx:1.27-alpine`'s master normally starts as root and its build defaults to `--user=nginx` (`nginx -V` → `--user=nginx --group=nginx`), which the mounted config does not override, so a root master chowns `/var/cache/nginx/*_temp` to uid 101 and `setgid`/`setuid`s each worker to it — the reason `genetics-results-suite-eau` had to add `CHOWN`, `SETUID`, `SETGID` back on top of drop-ALL. A pod-level `runAsUser/runAsGroup: 101` with `runAsNonRoot: true` and `fsGroup: 101` does none of those things: nginx skips the chown entirely when it is not root, and there is no privileged master to setuid from. All three capabilities are gone from **both** containers, which now drop `ALL` and add nothing. `NET_BIND_SERVICE` remains unnecessary for the same reason as before: the only `listen` is 8080, and neither fragment `deploy.sh` injects (`${LEGACY_REDIRECT}`, `${KEYCLOAK_SERVER}`) adds a listener.
    - It costs two `emptyDir`s. `/var/cache/nginx` is `root:root 0755` in the image with none of the `*_temp` directories created, so uid 101 dies at `mkdir("/var/cache/nginx/client_temp") failed (13: Permission denied)` without one; it is mounted into the initContainer too, because `nginx -t` is not a dry run and creates those same paths (as 101, chowning nothing). It is deliberately **not** `medium: Memory` — a 50M request body or a buffered upstream response would then be charged against the container's 128Mi memory limit. `/tmp` is the second, needed because both containers now also set `readOnlyRootFilesystem: true`; the config already puts the pid file at `/tmp/nginx.pid`, and `/var/run` turns out **not** to be needed at all.
    - **Neither emptyDir has a `sizeLimit`, and that is a decision, not an omission** (`genetics-results-suite-o5i`; the arithmetic is repeated in the manifest so it is not rediscovered). A `sizeLimit` does not surface as `ENOSPC` inside nginx — the kubelet **evicts the pod**, and this Deployment is `replicas: 1` and internet-facing, so every candidate number buys a disk-fill defence at the price of a new whole-site outage path. For `cache` the config's own ceiling is ~1 TiB: `worker_processes 1` × `worker_connections 1024` in flight, each able to spool a 50M body (`client_max_body_size`, and anything over the ~16k `client_body_buffer_size` default goes to `client_temp`) **plus** `proxy_max_temp_file_size`, left at its **1024m default**, on every buffered location (`proxy_buffering off` appears only on `/mcp` and `/chat/v1/`). The node offers ~43.8 GiB allocatable ephemeral storage, so no limit derived from the config bounds the adversarial case — it is ~24× the whole disk. Nor is the reviewer's suggested 1Gi safe for *legitimate* traffic: `/api/` proxies results-api through the BFF with buffering on, so one slow client pulling a large summary-stats response spools up to 1024m into `proxy_temp` unaided. A defensible number requires bounding `proxy_max_temp_file_size` in the config first, which is an nginx behaviour change needing its own traffic test. For `tmp` there is nothing to bound: the main container writes only `/tmp/nginx.pid` (~6 bytes) and the initContainer one ~9 KB placeholder config on its error path, none of it attacker-reachable. What the pod has instead, by omission: **no ephemeral-storage request**, so under node `DiskPressure` the kubelet's eviction ranking (usage above request, request 0) already puts the pod that filled the disk at the top of the list, without naming a threshold normal traffic can cross.
    - **The image stays a tag, with the tested digest recorded in the manifest** (`genetics-results-suite-o5i`). `readOnlyRootFilesystem` made the moving tag newly load-bearing: `docker-entrypoint.sh` runs under `set -e`, so a future `1.27-alpine` push adding a `/docker-entrypoint.d` script without a read-only guard **crash-loops the internet-facing gateway** rather than writing a stray file (all four current scripts were read out of the image and are safe). It is not pinned because nothing in this repo bumps image references — no Renovate, no Dependabot, no `.github/` at all — and every other image in `k8s/` is a tag, so a pin here would freeze an internet-facing HTTP parser at a fixed build for good. Note the tag buys less than it appears to: with the default `IfNotPresent`, a node keeps whatever it already pulled, so the tag is re-resolved only when the pod lands on a node without the image. `nginx@sha256:65645c7b…2f2a10` (built 2025-04-16) is what both `a7n`'s harness and `o5i`'s tested, and `kubectl get pod` confirms it is what the live gateway is running — so recording it changes nothing at the next rollout and gives the next reader something to diff the entrypoint against.
    - Measured end to end against `nginx:1.27-alpine` under `--user 101:101 --cap-drop ALL --security-opt no-new-privileges --read-only`, driving the **real rendered config** (ConfigMap → `deploy.sh`'s envsubst with both fragments populated → the initContainer's envsubst) with all eight cluster-local names stubbed: master **and** worker both `Uid 101`, `CapPrm`/`CapEff`/`CapBnd` all `0000000000000000`, `NoNewPrivs 1`. Traffic: `/healthz` 200, `/` authorized 200 and unauthenticated 302 to `/oauth2/start`, the `Authorization: Bearer` → `@api_bearer` bypass 200, `/api/` → BFF 200, `/status` and `/chat/v1/` 200 with the rendered `X-Internal-Auth` intact, the `${KEYCLOAK_SERVER}` `/auth/` fragment 200, the `${LEGACY_REDIRECT}` 301, a 20 MB POST (forcing a `client_body_temp` write) 200 on both the auth_request and Bearer paths, and a 20 MB response to a rate-limited client 200 with `proxy_temp/3/00` created by the worker. Error log clean.
  - **What the auth-gateway hardening buys, measured — and what it does not.** The nginx *worker*, the process that parses internet traffic, was **already** fully unprivileged before any of this: `setuid(101)` without `PR_SET_KEEPCAPS` clears the capability sets, so the pre-`eau` pod with an empty `securityContext` showed Uid 101, CapPrm 0, CapEff 0. `eau` improved only the **master**, which never touches attacker input, from CapEff `0xa80425fb` (the 14-cap runtime default) to `0xc1` with NoNewPrivs 0 → 1 — real, but it left the master uid 0 on a writable rootfs, so the gateway was not meaningfully harder to exploit, only less useful once exploited. `a7n` is the step that changes the landing privileges themselves: the master is now uid 101 with an empty capability bounding set and an unwritable rootfs, so an nginx RCE lands with no capability to reclaim through `execve` and no way to persist into the image layer. **The mounted credential went in the follow-up** (`genetics-results-suite-o5i`): the pod now sets `automountServiceAccountToken: false`, so no ServiceAccount token is projected into the internet-facing pod at all. **How much that is worth, measured rather than assumed** — the honest answer is *defence-in-depth, not the closing of a live escalation path*. No RoleBinding or ClusterRoleBinding in the cluster names `system:serviceaccount:genetics:default` (checked over all 151 bindings), and the SA carries no `iam.gke.io/gcp-service-account` annotation, so it reaches GCP not at all. Its entire authorisation is what every authenticated identity inherits — `system:basic-user`, `system:discovery`, `system:public-info-viewer`, `system:service-account-issuer-discovery`: self-reviews, `/version`, `/healthz`, the OpenAPI/discovery documents, the OIDC JWKS. So what the change denies an attacker today is a valid cluster credential for reconnaissance, and what it denies tomorrow is the blast radius of any binding someone later grants the `default` SA — which is the failure mode worth pre-empting, because such a binding would arrive without anyone rechecking this pod. Verified functionally inert by re-running `a7n`'s traffic matrix against the rendered config with the token directory absent: identical status codes on all twelve cases.
    - **Still true of the other five.** `bff`, `frontend`, `keycloak`, `oauth2-proxy` and `postgres` set neither field and still get the `default` token; the eight that set `serviceAccountName: genetics-suite` automount that KSA's token instead (`genetics-suite` has no RBAC binding either — its value is GCP-side through Workload Identity, and that path runs through the metadata server, not the mounted token). Filed separately rather than changed here.
    - **This is why `automountServiceAccountToken: false` is no longer a sandbox tell** in `scripts/test-network-policies.py`. It was one of that harness's four forced tells; auth-gateway setting it made the lock fire (`refusing to apply network-policies/`) on a pod that had merely been hardened, which is the exact shape of check that gets deleted instead of fixed. Removed from `sandbox_tells()`; the two gVisor tells, which the node pool's taint genuinely forces, are untouched.
  - **The rollout is low-risk by construction.** auth-gateway is `replicas: 1` with no `strategy` block, so the default RollingUpdate gives `maxUnavailable = floor(0.25 × 1) = 0` and `maxSurge = 1`: the new pod must reach Ready before the old one is deleted. A CrashLoop or a failing `render-config` initContainer therefore leaves the **current** gateway serving rather than causing an outage.
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
### There is no development environment — and the BigQuery rehearsal dataset

Established read-only on 2026-08-13 (`genetics-results-suite-44g`) and worth stating
plainly because the project's name argues the opposite: **`phewas-development` IS
production.** One GKE cluster and one kubeconfig context
(`gke_phewas-development_europe-west1-b_finngenie`), one GCP project with no companion
`phewas-production`, one application namespace (`genetics`), and exactly three BigQuery
datasets — `genetics_api_logs`, `genetics_chat_logs`, `genetics_results` — with no dev
copy of any of them. The live results-api carries `DEPLOY_ENV=prod` and
`LOG_SOURCE=finngenie_prod`. The `daly` profile is a **second production brand** with its
own project, region, domain and real users, not a staging copy, so it is not a canary.
(`log_source` `genetics-results-api-dev1` in the monitoring section is historical; nothing
answers to it now.)

Consequently every BigQuery DDL change is a change to live data by default. The
rehearsal ground is `scripts/bq-dev-dataset.sh`, which builds `genetics_results_dev` next
to `genetics_results` in the **same project and the same location** (`europe-west1`, per
`bq show --format=prettyjson phewas-development:genetics_results`) out of zero-copy
`CREATE TABLE … CLONE` statements, then creates the views with their table references
**rewritten** to the dev dataset.

The rewrite is the correctness crux, not a detail: all 15 views in `genetics_results`
embed a fully-qualified `` `phewas-development.genetics_results.<table>` `` reference, so a
view copied verbatim into a dev dataset silently reads **production** tables while the
tables beside it are dev clones — a rehearsal that looks right and proves nothing.
`bq-dev-dataset.sh verify` therefore reads every dev view back out of BigQuery and fails
loudly on any surviving reference to the source dataset; run it after every `create` and
after any rehearsal step that replaced a view. Rewrite and detection are one
implementation used in two modes, because splitting them is how references backticked per
part (`` `p`.`d`.`t` ``, ordinary BigQuery and the form a hand-written rehearsal view is
likely to use) got handled by neither.

Clone storage is not free forever: a clone bills for every block it stops sharing with its
base table, which happens when **production** replaces or drops that table just as much as
when the rehearsal writes to the clone. Drop a table's dev clone once its production
counterpart has been promoted away — for this batch that is ~105 GB across `eyg` and `4ci`
— rather than waiting for the batch teardown.

Dry run is the default (`--apply` executes), `teardown` additionally needs `--yes` and the
dataset name typed at a tty, and the target dataset name is refused outright if it is one
of the three production datasets or lacks a `dev` segment — there is no override flag.

`docs/bigquery-dev-dataset.md` is the runbook: the expand/verify/contract cycle and the
promotion path for each of the five open BigQuery beads (`94c`, `eyg`, `4h6.20`,
`4h6.21`, `4ci`), and the ordering constraints between them — `4ci`'s irreversible
~27 GB `DROP`s only after `eyg` is verified, `94c`'s contract phase only after all three
code artifacts ship (see *HLA column rename rollout* below), and `5p5`/`4h6.30` held back
because their `datasets.yaml` guidance is correct today and becomes wrong only once `eyg`
lands.

Note for anyone measuring: `--dry_run` proves syntax and reference resolution only. On a
freshly created table BigQuery's dry-run estimate **ignores clustering** until the storage
optimiser has processed it (4h6.18 observed 4,492,232,401 B dry-run against 517,406,337 B
actual), so scan-byte claims need real execution with `use_query_cache=False`, and the
clustering itself should be asserted from
`INFORMATION_SCHEMA.COLUMNS.clustering_ordinal_position`.

### Running the local dev stack (`scripts/dev-stack.sh`)

Five servers run from source on a dev machine — results-api `:2000`, frontend `:3000`,
chat-backend `:4000`, BFF `:5000`, db-api `:8080` — and each lives in a different repo.
`scripts/dev-stack.sh` starts, stops and switches all five as one unit; the full
from-scratch setup is [docs/local-dev-vm.md](local-dev-vm.md).

```
./scripts/dev-stack.sh up                 # worktree trees, db-api on genetics_dev
./scripts/dev-stack.sh up --tree main     # main checkouts, db-api on genetics_results
./scripts/dev-stack.sh status             # port, health, and which tree each pid runs from
./scripts/dev-stack.sh down
```

What is load-bearing about it:

- **One tree at a time, by construction.** Both trees want the same five ports (the
  frontend's `VITE_*` URLs, vite's `/api` proxy and the sandbox container's
  `host.docker.internal` targets all name them), so `up` frees each port before starting on
  it. Going back to `master` is `down` then `up --tree main`.
- **It stops what holds the port, not what it started** — `ss` to the listening pid to its
  process group — so it takes over hand-started tmux servers, and one signal reaches a whole
  `npm` → `sh` → `node` tree instead of letting `tsx watch` respawn its child. **But only
  when the holder is this suite's**: the socket says nothing about whose process it is, and
  `:3000`/`:8080` are the ports an unrelated dev server is most likely to be sitting on. Each
  holder's `/proc/<pid>/cwd` and command line are checked against the service's repo (main
  checkout or any worktree beneath it) first; a stranger is reported — pid, cwd, argv — and
  left alone, that service is skipped, and `up` exits non-zero. `--force` overrides. It also
  refuses to run as root and never signals process group 0, 1, its own, or the one it
  inherited from the launching terminal.
- **Every selected tree is validated before the first port is freed**, so a missing `.venv`
  cannot leave two services on the new tree, one killed and two still serving the old one.
  `up` returns non-zero if any service fails preflight, has its port refused, or never
  answers its health endpoint.
- **The gitignored config is referenced, never copied.** `genetics-mcp-server/.env` exists
  only in the main checkout; the script sources it by path (`MCP_ENV_FILE`) into the
  chat-backend subshell, so no secret reaches a worktree, a command line or a log. The
  frontend's `VITE_*` values are passed as environment variables — vite merges prefixed
  `process.env` over its `.env` files — so a worktree needs no `.env.local` either.
- **`SANDBOX_URL` is set explicitly to `http://127.0.0.1:8081`.** The client's default is
  `127.0.0.1:8080`, which locally is db-api, not the sandbox (`genetics-results-suite-6um`).
- **It provisions the sandbox's per-execution credentials** (`genetics-results-suite-4h6.49`):
  `SANDBOX_TOKEN_SIGNING_KEY` and `INTERNAL_API_SECRET` are generated once into
  `DEV_STACK_RUN_DIR` — stable across restarts, outside every repo, never in a working tree —
  and exported with `SANDBOX_ENABLED=true`. Without them db-api and results-api resolve **no
  sandbox principal at all** and serve the sandbox SDK with no per-execution accounting, which
  is locally indistinguishable from the bug the tokens exist to fix
  (`genetics-results-suite-0lf`). Override any of the three to pin a value.
- **`status` reads `DATASET_ID` from `/proc/<pid>/environ`**, because `/health` does not
  report it and an unset `DATASET_ID` means production — `api/main.py` defaults it to
  `genetics_results`. It prints that case as `PRODUCTION` rather than as a blank.
- **`--tree worktree` defaults db-api to `genetics_dev`**, the persistent full-size copy
  (`genetics-results-suite-g08`): all 15 tables and views, and since 2026-08-18 every
  production row — 755,813,602 rows / 136.69 GB, each table matching its `genetics_results`
  counterpart exactly. Production's 1.1 B is larger only by the three `credible_sets_exp_*`
  tables dev deliberately omits. Any gene smoke-tests, on any of the 23 chromosomes;
  `APOE` no longer returns zero. Reload it with `TRUNCATE` + `INSERT … SELECT` and an
  explicit column list, never a CTAS or clone — no dev table's schema equals production's
  (all 15 carry descriptions and `REQUIRED` modes that production, uniformly `NULLABLE`
  and undescribed, does not), and a CTAS inherits neither those, nor the `chr` range
  partitioning, nor the clustering. It is a
  different object from the `genetics_results_dev` *rehearsal* clone of
  [docs/bigquery-dev-dataset.md](bigquery-dev-dataset.md), which is created and torn down
  around one DDL change.

Nothing here touches a cluster: it starts local processes and reads BigQuery.

### Running the sandbox locally (`scripts/run-sandbox-local.sh`)

The sandbox is the one service with a genuine local backend, and it is deliberately not an
exception to the section above: it runs the **same image** in a plain Docker container
instead of a gVisor pod, with the supervisor supplied as the container's command the way
`k8s/deployments/sandbox.yaml` will supply it as `args:`. There is no local/production fork
in the code and none in the request flow — chat-backend's client holds one base URL either
way.

```
./scripts/run-sandbox-local.sh            # build, (re)start, wait for /health, print the fidelity report
./scripts/run-sandbox-local.sh --test     # ... then run test-supervisor.py --container --container-name against it
./scripts/run-sandbox-local.sh --no-build # restart without rebuilding
./scripts/run-sandbox-local.sh --logs     # container stdout, which is where the audit stream lands
./scripts/run-sandbox-local.sh --stop
```

Operational specifics:

- **The host port is 8081, the container port is 8080.** The container port matches the
  manifest and the Service; the host port cannot, because the local db-api already holds 8080
  (`genetics-results-suite-r9e`). `HOST_PORT` overrides it. It publishes on `127.0.0.1` only.
- **It does not clone genetics-mcp-server the way `scripts/build.sh` does.** The point is to
  run the working tree, so the SDK is staged from a local checkout: `MCP_SERVER_DIR` if set,
  otherwise the sibling repo's worktree **of the same name** first and its main checkout
  second — the resolve-into-the-main-checkout class `scripts/check-worktree-paths.sh` exists
  for, and here it would silently build against a branch that carries no SDK at all.
- **It checks `sandbox/schema` and `sandbox/stubs` rather than regenerating them**, because
  regenerating writes tracked files a developer starting a container did not ask to change.
  Drift warns and the build continues (`--regen` writes them). `scripts/build.sh` still
  regenerates and still treats a failure as fatal — a pushed image documenting a stale SDK is
  a defect; a local one is survivable.
- **It never touches a cluster and pushes nothing.** The tag is `genetics-sandbox:local` so a
  local build cannot be mistaken for the image the cluster pulls.
- **The fidelity gap is printed on every start**, not buried: gVisor, the NetworkPolicy, the
  kubelet pid limit, the seccomp profile, `emptyDir` `sizeLimit` eviction,
  `ephemeral-storage` requests/limits (`1Gi`/`2Gi`, with **no** local analogue at all), the
  Deployment's restart behaviour (`--restart no` here) and DNS have no local form.
  `terminationGracePeriodSeconds: 130` **is** reproduced, by `--stop-timeout 130` plus a
  `--stop` that calls `docker stop` rather than `docker rm -f`. One gap is a sizing trap
  rather than missing coverage and `4h6.41`/`4h6.46` must read it before tuning anything: the
  local `/scratch` is a tmpfs, i.e. page cache in the container's **own memory cgroup**, so
  its bytes come out of the same 3 GiB as the child's RSS (measured 113 MiB → 414 MiB after a
  300 MiB write), whereas the pod's `emptyDir` has no `medium: Memory`, is node-disk-backed
  and is charged to `ephemeral-storage` instead — never to `limits.memory`. Headroom sized
  locally is therefore up to 512 MiB more conservative than the pod needs.
  `docs/code-execution-security.md` → "As built (`4h6.40`)" enumerates each and what it costs;
  any control whose only enforcement is one of them is unexercised locally and must be
  verified at deploy time.

### Documentation-drift hook

`scripts/check-doc-drift.sh` warns (never blocks) when a commit changes a path the
docs describe without touching the doc — the mappings are the "Documentation
ownership" table in `CLAUDE.md`. It runs from `pre-commit`, and it fires from
worktrees as well as the main checkout, because git runs hooks with the working
tree's top as cwd so the `./scripts/` path resolves per worktree.

Two pieces have to be in place, and **only one of them survives a clone**:

- **The hook file.** `.beads/hooks/pre-commit` is tracked. Beads owns the section
  between its `BEGIN/END BEADS INTEGRATION` markers; the doc-drift block is appended
  below it. Beads **patches between the markers rather than rewriting the file** —
  measured against bd 1.0.3 with `bd hooks install`, `bd hooks install --force`, and
  a forced downgrade of the version marker followed by a reinstall; the appended
  block survived all three, and `bd hooks uninstall` strips only the beads section.
- **`core.hooksPath`.** Beads points it at `<main checkout>/.beads/hooks`. This is
  local git config living in the git dir, so **a fresh clone runs no hooks at all** —
  no doc-drift warning and no beads export, with no error to notice.

`scripts/install-git-hooks.sh` closes both. Run once after cloning: it sets
`core.hooksPath` (resolved via `git rev-parse --git-common-dir`, so it is correct
when run from a worktree) and re-appends the doc-drift block if it is missing. It is
idempotent and prints only what it changed. `--check` reports without repairing and
exits 1; `scripts/deploy.sh` and `scripts/build-all.sh` call it that way and warn
without blocking, so an unwired checkout surfaces the first time anyone builds or
deploys rather than staying silent.

`check()` takes an **optional 4th argument**: a regex of paths to ignore *inside* the
rule's own path pattern, for files the named doc demonstrably cannot describe. One
rule uses it (`genetics-results-suite-dqa`) — the `docs/keycloak-apple-signin.md`
rule ignores the static branding assets under `keycloak/themes/**` (stylesheets,
`.properties` bundles, images, fonts), which change how the login page looks and reads
and nothing else. The exemption is written by file extension rather than by directory
on purpose: `keycloak/themes/genetics/login/` is where a FreeMarker override such as
`login.ftl` would go, and a template or script there changes how the login page
behaves, so it stays covered. The sandbox's generated trees are deliberately **not**
excluded from anything: `docs/code-execution-security.md` reasons about the content
`sandbox/stubs/**` and `sandbox/schema/**` ship (the stubs are where
`INTERNAL_API_SECRET` enters its secrets-in-image analysis, and the `PLACEHOLDER`
build gate is stated over both staged trees), and `docs/project-spec.md` summarises
what the sandbox exposes, so either doc can be falsified by a regeneration. A rule
whose path pattern is broader than the doc concern it names fires where it can never
apply, and a warn-only check that fires when it cannot apply becomes wallpaper — but
"generated" is not on its own a reason to exclude a path, only "the doc makes no claim
this file can break" is. Exclusions are recorded in `CLAUDE.md`'s ownership table as
**except** clauses; the table and the script must match.

**Known limits** (deliberate): the check only warns, and it only knows path → doc
mappings. It cannot detect a doc that *enumerates* something the code no longer
matches — a stale view list, row count or endpoint table — which is what most of the
drift found in this repo has actually been. All five sibling repos
(`genetics-results-api`, `-db`, `-browser`, `-munge`, `genetics-mcp-server`) carry
their own `scripts/check-doc-drift.sh` wired into `.beads/hooks/pre-commit` the same
way, and share the same `core.hooksPath`-not-tracked gap; none of them has the
installer.

### Worktree path resolution: `scripts/check-worktree-paths.sh`

A recurring failure class in this repo: **a tool run from a git worktree resolves a
path into the MAIN checkout, then degrades without erroring**. The tool exits 0, prints
nothing alarming, and the absence of an error is read as success. Four instances have
been found, each by accident and each after the silent degradation had already happened.
Three are still only *detected* here; the fourth was fixed in the offending script:

| what resolves to the main checkout | how it degrades from a worktree |
|---|---|
| `.beads/issues.jsonl` — bd writes the export next to the Dolt store, and the store is in the main checkout | the worktree's copy is **tracked but never written**, so `git add .beads/issues.jsonl && git commit` stages nothing and git answers "nothing to commit, working tree clean" |
| ~~sibling repos for `scripts/sync-datasets.sh`~~ — **fixed**: the script resolves them from the git common dir, so a worktree run syncs the real siblings, refuses a directory whose `pyproject.toml` does not name that repo, and exits nonzero when it cannot resolve them at all. `check-worktree-paths.sh` no longer checks this case | (was: printed `WARN: repo not found, skipping` for both and **exited 0 having copied nothing**) |
| `terraform/terraform.tfvars` — gitignored, so it exists only in the main checkout | terraform falls back to destructive variable **defaults**; `build.sh`/`build-all.sh` fall back to `APP_NAME=FinnGenie` (both run this preflight first, so the fallback is warned about rather than silent) |
| `core.hooksPath` — local git config, shared by every worktree | hooks run from the main checkout's copies, so an edit to `.beads/hooks/*` on a branch is inert for commits made there |

`scripts/check-worktree-paths.sh` is one preflight for the three still-detected cases
(the sync-datasets one was fixed at source and dropped from it). It compares the path
each tool will actually use against the path inside the current worktree and reports
**only where they differ** — an alert that fires unconditionally is one nobody reads.
It exits 0 in silence from the main checkout (where `git rev-parse --git-common-dir`'s
parent equals the top level, so nothing can diverge), 1 from a worktree with
divergences, and 2 outside a git repository. `scripts/deploy.sh`, `scripts/build-all.sh`
and `scripts/build.sh` call it `--check || true`, the same warn-never-block pattern as
`install-git-hooks.sh` — a single-service build degrades to `APP_NAME=FinnGenie` exactly
like a full build, so it warns exactly like one (`genetics-results-suite-8wh`).

Three refinements worth keeping: the `core.hooksPath` case fires **only when the hook
files actually differ** (pointing at the main checkout is intended, and the files are
tracked so they normally match); a *relative* `core.hooksPath` is skipped outright,
because git resolves relative values against the working tree the hook runs from and
they are therefore already per-worktree; and the beads case likewise fires **only when
the two `issues.jsonl` files actually differ** (`cmp -s`). The structural facts it used
to key on — a worktree has a tracked `issues.jsonl` and no `.beads/embeddeddolt` of its
own — are true in every worktree permanently, so keying on them made it fire on every
deploy and every build regardless of whether the export was stale.

**On the beads case specifically — bd's behaviour is correct and must not be
"fixed".** One Dolt database is shared by every worktree (a worktree gets no
`.beads/embeddeddolt` of its own; a bead created from a worktree lands in the main
store), so a single canonical export next to that store is right. Making bd export per
worktree would put N snapshots of one shared database into N branches — each one
*complete*, since they all read the same store, and differing only in **when** it was
taken. That is worse than it sounds: N committed files that look authoritative, diverge
purely by staleness, and conflict on every merge.
**Measured** against bd 1.0.3: the `post-checkout` and `post-merge` hooks do **not**
import from `issues.jsonl` — after closing an issue and then restoring an older
committed jsonl, neither hook nor a real `git merge` that changed the file reverted the
live state. But an explicit `bd import` upserts blindly and **did** revert a closed
issue to open. So a stale committed jsonl is inert until someone runs `bd import`, at
which point it silently rewinds live state.

Refreshing it does **not** require the main checkout. Because the Dolt store is shared,
`bd export -o .beads/issues.jsonl` run from inside a worktree writes that worktree's
tracked copy from the same live database — measured byte-identical to the main
checkout's export. That is the command the preflight now prints, so its warning is
actionable rather than merely informative. What is never true is that a worktree's
`git add .beads/issues.jsonl` did anything on its own.

### The same class in Python: which tree the tests actually import

`check-worktree-paths.sh` covers shell tools. The service repos have their own instance of
the failure, and it is enforced in each repo's `tests/conftest.py` rather than here, because
only the interpreter that runs the tests can see it. The three repos differ in install mode,
so the guard differs in shape:

| repo | install mode | what can silently import another tree | guard |
|---|---|---|---|
| `genetics-mcp-server` | **editable install** — `.venv/…/_editable_impl_genetics_mcp_server.pth` points at ONE tree's `src/` | in a fresh worktree `uv run pytest` falls through to the pyenv shim, whose interpreter has the MAIN checkout installed; worktree tests then run main-checkout source and report green (genetics-results-suite-6o3) | `pytest_configure` aborts unless `genetics_mcp_server.__file__` is under the pytest rootdir |
| `genetics-results-api` | **not installed** — `app` is a namespace package, `tests/conftest.py` puts the repo root first on `sys.path` | the foreign-interpreter case cannot happen (the insert is anchored to the conftest's own location), but a namespace package **merges** every matching `sys.path` entry, so `PYTHONPATH=<other checkout>` — what a bare `python scripts/…` run needs — adds that tree's `app/` to `app.__path__` | `pytest_configure` aborts unless every `app.__path__` entry is under the rootdir |
| `genetics-results-db` | **not installed** — no `[build-system]`, so `uv sync` installs dependencies only; `api` is a namespace package each test module puts on `sys.path` | same namespace-merge exposure as results-api | `tests/conftest.py` (added for this) makes the same `api.__path__` assertion |

All three fail in `pytest_configure` — before collection, with the resolved paths and the
fix in the message — not as a test failure inside a run. The fix for the mcp-server case is
`uv sync --extra dev` **in the worktree** followed by the resolution one-liner the message
prints; for the other two it is unsetting or repointing `PYTHONPATH`.

### Ordered rollout: the trusted-proxy marker (bff before results-api)

The identity-header trust rule spans two repos and **the rollout order is load-bearing**. bff
must ship first.

| state | bff sends the bearer on `/api` passthrough? | results-api honours the identity header over the bearer? | browser result |
|---|---|---|---|
| neither shipped (old) | no | no (bearer wins → `mcp-tool`) | works, but the header alone authenticates — the vulnerability |
| **bff only** (transitional) | yes | no | **works.** Old results-api checks the bearer first, matches `INTERNAL_API_SECRET` and authenticates as `mcp-tool`; the usage log reads the header directly, so attribution is still the real user |
| **results-api only** | no | yes | **total lockout.** Every non-variants browser request arrives with the identity header and no bearer → 401 |
| both shipped | yes | yes | works, and the real user is the authenticated identity again |

So:

```bash
./scripts/build.sh bff       && ./scripts/rollout.sh bff        # first, and let it settle
./scripts/build.sh results-api && ./scripts/rollout.sh results-api  # only after bff is serving
```

### Ordered rollout: the sandbox execution token (no code ordering, one config lockout)

By contrast with the two rollouts above, the sandbox credential (`genetics-results-suite-4h6.9`)
has **no code ordering hazard**: nothing sends an HS256 bearer until `4h6.7` and `4h6.47` land
the sandbox and the client that calls the minter, so minter-first and validators-first are both safe, and every
existing credential type is untouched in either single-sided state.

| state | chat-backend mints | db-api / results-api verify | result |
|---|---|---|---|
| neither shipped | no | no | current behaviour |
| **validators only** | no | yes | **safe** — no caller produces an HS256 bearer, the branch never fires |
| **minter only** | yes | no | **safe today** — nothing calls the minter yet; a token sent to an old validator would 401, never authorize |
| both shipped | yes | yes | the sandbox path works once the sandbox exists |

The ordering that *is* load-bearing is the **secret**: chat-backend, db-api and results-api all
mount `sandbox-token-signing-key`, so `create-secrets.sh` must run before the manifests are
applied or the pods sit in `CreateContainerConfigError`. `deploy.sh` now verifies that key and
`internal-api-secret` are non-empty before applying anything, and on a miss offers both a
targeted `kubectl patch secret genetics-secrets --type=merge` for **that one key** and a re-run
of `create-secrets.sh`. That re-run used to be the trap — the script reused-or-generated only a
handful of keys and wrote `--from-literal=x="${X:-}"` for the rest, so re-running it without
every optional secret exported blanked the OpenAI/Tavily/Perplexity/Cohere/MCP keys,
`external-mcp-servers`, `admin-users` and `slack-webhook-url`. Fixed in
`genetics-results-suite-4pj`: every key the script writes **except `anthropic-api-key`** is now
reused from the cluster when its env var is unset. The three fallbacks differ by what an absent
value would mean: the shared secrets and passwords are reuse-then-*generate*; the eight optional
keys are reuse-then-*empty*, because a random value for an API key is a plausible-looking string
that fails only at call time and `admin-users`/`external-mcp-servers` have no random value at
all; the keycloak usernames are reuse-then-*default* (`keycloak`/`admin`), since an empty
username is useless and overwriting a customised `db-user` while faithfully reusing `db-password`
would leave Keycloak unable to reach Postgres. `ANTHROPIC_API_KEY` is the deliberate exception —
never read back, always required, and the script aborts before writing anything without it, which
is the one caveat the `deploy.sh` and `docs/code-execution-security.md` remediation text carries.
The reuse read also separates "kubectl worked, key absent" from "kubectl failed" and aborts
loudly on the latter — previously `kubectl get … 2>/dev/null | base64 -d || true` made an RBAC
denial or a wrong kubeconfig context indistinguishable from "never set", which would silently
rotate `internal-api-secret` (breaking every service) against a perfectly healthy cluster.
kubectl's stderr is captured to a scratch file and read **only when it exits non-zero**, never
merged into stdout: kubectl exits 0 while printing auth-plugin deprecation warnings, and merging
those into the base64 payload would abort the script on GNU coreutils and, on a busybox `base64`
that skips non-alphabet bytes, write a corrupted value back over the live secret.
The single lockout is
`SANDBOX_ENABLED=true` with either secret missing — both services `sys.exit(1)` by design, so
that crash-loops db-api and results-api. Both manifests ship `SANDBOX_ENABLED: "false"`; the
deploy that creates the sandbox Deployment flips it, after `create-secrets.sh`. **Rollback:
set `SANDBOX_ENABLED=false` and restart** — it disables only the startup assertion, never the
token validation, and since `genetics-results-suite-rhh` it no longer widens results-api's
anonymous surface either (that is `ANONYMOUS_SURFACE_MINIMAL`, which defaults to on and is
merely *forced* by `SANDBOX_ENABLED`).

`deploy.sh` restarts every deployment at once, so a **full deploy carrying both images is not
safe on its own** — it gives no ordering between the two rollouts and can leave results-api
new while bff pods are still terminating. Roll bff out on its own first, confirm
`kubectl get pods -n genetics -l app=bff` is fully `Running` on the new image, then deploy the
rest.

Rolling **back** reverses the constraint: results-api first, bff second. Reverting bff alone
while the new results-api is live reproduces the lockout row.

The transitional bff-only state is genuinely safe rather than merely brief — it is a valid
resting state, so a rollout can stop there indefinitely. Its only cost is that during the
window `request.state.authenticated_user` is `mcp-tool`; that field is a *fallback* for the
`endpoint_access` log, which prefers the header, so logs still name the real user. No
transitional feature flag or dual-accept mode is needed.

### Ordered rollout: the trusted-proxy marker (auth-gateway before chat-backend)

The same rule reaches chat-backend in `genetics-results-suite-th2`. auth-gateway must ship
**first**, but unlike the bff pair the leading state is a safe resting state — the marker rides
its own header, `X-Internal-Auth`, precisely so that a chat-backend which has not yet rolled
simply does not recognise it.

| state | auth-gateway sends `X-Internal-Auth` on the chat locations? | chat-backend requires the marker for the identity header? | browser result |
|---|---|---|---|
| neither shipped (old) | no | no | works, but the header alone authenticates — the vulnerability |
| **auth-gateway only** (transitional) | yes | no | **works, byte for byte as before.** The old chat-backend reads no such header and its `auth_required` finds no `Authorization` to match, so it takes the plain identity-header path. Safe to sit in: the pre-fix vulnerability is still open, but nothing is mis-attributed and no rollback is needed to leave it |
| **chat-backend only** | no | yes | **total lockout.** Every browser chat request arrives with the identity header and no marker → 401. This is the order to avoid |
| both shipped | yes | yes | works, and the real user is the authenticated identity |

Only one of the four states is harmful, and it is the one gateway-first never enters. Rolling
**back** is the mirror image: revert chat-backend first, auth-gateway second, and the state in
between is the safe "auth-gateway only" row again — a rollback that stalls half-done costs
nothing. Reverting the gateway alone while the new chat-backend is live is the one move that
reproduces the lockout row.

auth-gateway is ConfigMap-driven, so `rollout.sh` (image swap only) cannot ship it — it needs
`deploy.sh`:

```bash
./scripts/deploy.sh                                                    # 1. gateway ConfigMap
./scripts/build.sh chat-backend && ./scripts/rollout.sh chat-backend   # 2. after
```

Collapsing this into a single `deploy.sh` carrying a freshly built chat-backend image is no
longer a data hazard, but it is still not ordered: `deploy.sh` fires every `rollout restart`
before waiting on any, so which service reaches its new pod first is a race, not a guarantee.
chat-backend's `Recreate` strategy plus its 300s termination grace *usually* makes it the slower
of the two — but that grace is an upper bound, and a chat-backend with no in-flight streams
terminates in seconds, while the gateway now has an extra initContainer in front of it. The
worst case is therefore a short "chat-backend only" window: browser chat 401s until the gateway
lands, inside a deploy that is already interrupting chat. Use the two-step above, which removes
the race rather than betting on it.

The transport deliberately differs between the two services: results-api takes the marker on
`Authorization: Bearer $INTERNAL_API_SECRET` (its callers are service-to-service and have no
`Authorization` of their own), while the browser-facing chat locations use `X-Internal-Auth`.
chat-backend's `is_internal_caller` accepts either, so results-api and mcp-server keep calling
it with the bearer unchanged. Two conventions is the accepted price of a rollout with no window
in which browser sessions, downloads and API tokens could be written under the shared `mcp-tool`
identity — which is exactly what an `Authorization`-borne marker would have caused, since the
old chat-backend checks the bearer *before* it looks at the identity header.

### SDK empty-result contract (`genetics-results-suite-6uk`)

A script in the code-execution sandbox filters whatever frame the SDK hands it, so an empty
result must keep its **column names** — otherwise a perfectly ordinary no-hit query raises
`ColumnNotFoundError` and costs the agent a retry iteration. The two backends reach that
guarantee by different routes, and the difference is a property of the wire format, not of
the SDK:

- **db-api** returns `{"columns": [...], "rows": [[...]]}`. `columns` comes from the
  BigQuery job schema, which is populated for a zero-row result, so the SDK has always been
  able to build a named empty frame. `columns` is also **required** here: the rows are
  positional and handing them to a dict constructor silently produces a transposed,
  stringified frame.
- **results-api** returns a **bare JSON array** of named row objects. An empty one is `[]`
  and carries no schema at all. `range_response` now advertises the file's own header line
  in an **`X-Columns` response header** (`genetics-results-api app/core/responses.py`),
  which covers all 11 routers that serve JSON range responses in one place — credible sets,
  colocalization, exome, expression, HLA, MPRA, open chromatin, chromatin peaks, summary
  stats, variant annotation, variant effect. The names come from the TSV header, **not**
  from the router's `header_schema`: that schema is a validating superset (see
  `genetics-results-suite-7yg`) and would over-claim on files that carry fewer columns.

Three properties are load-bearing and each is pinned by a test:

1. **The change is additive.** The JSON body is byte-identical — the browser and the MCP
   server parse the array and neither can observe an added response header. Wrapping the
   array in an envelope would have been a breaking change for both. The header is JSON-only:
   on the TSV path the header line is already the first line of the body, and `7yg`
   established that path must not buffer the stream to inspect it.
2. **MCP tool output is unchanged.** A `ToolExecutor` result dict *is* the MCP tool payload
   and the chat backend's model input, which this epic freezes. The executor therefore takes
   `expose_columns=False` by default and only the SDK's own executor asks for it; when it is
   off — or the endpoint does not advertise — the result dict is byte-identical to before.
3. **The two keys stay separate.** results-api's list arrives as `column_names`, never as
   db-api's `columns`, because those rows are already named dicts: routing them through the
   positional constructor would give up `pl.from_dicts`' `strict=False` fallback for the
   mixed-type columns upstream does produce. `column_names` is consulted **only** when the
   result is empty.

What this does *not* cover: results-api endpoints outside the `range_response` family —
fuzzy search, gene annotations, gene groups, rsID lookup, LD, gene–disease, and gene-based /
gene-burden results (`gene_based.py`, which returns `StreamingResponse`/`TimedJSONResponse`
directly) — which compute their JSON rather than streaming a TSV and so have no header to
advertise. Those degrade to the previous behaviour (a bare `pl.DataFrame()`), they do not break.

### HLA column rename rollout (`hla_associations_v`)

`genetics-results-suite-5wm` renamed the statistic columns of `hla_associations_v` to the
suite's house spelling — `mlogp`→`mlog10p`, `sebeta`→`se`, `af_alt`→`af`,
`af_alt_cases`→`af_cases`, `af_alt_controls`→`af_controls` — so that both branches of the SDK's
`hla()` and both HLA MCP tools return one set of names. The rename lives entirely in the view;
the staged file and the `hla_associations` table keep FinnGen's native spelling, and the values
are byte-identical.

**This is not two commands. It is one view plus THREE code artifacts, each with its own
apply mechanism — and the third is `deploy.sh`, which the other two never invoke.**

| part | artifact | what applies it |
|---|---|---|
| the view | `genetics-results-db/schemas/hla_associations_v.sql` | `genetics-results-db/scripts/setup_bigquery.sh`, run by hand against `PROJECT_ID`/`DATASET_ID`. It sed-substitutes the project into every `schemas/*_v.sql` and issues the `CREATE OR REPLACE VIEW`. Nothing in `deploy.sh`, `build.sh` or `rollout.sh` touches a BigQuery object |
| code 1/3 | mcp-server's `get_hla_by_allele` SQL (`tools/executor.py`), the tool descriptions, the SDK docstring | `scripts/build.sh mcp-server && scripts/rollout.sh mcp-server` |
| code 2/3 | the sandbox image's generated `sandbox/schema/hla_associations_v.md` and `sandbox/stubs/*.pyi` | a **separate image** from mcp-server: `scripts/build.sh sandbox && scripts/rollout.sh sandbox <tag>`. Rolling mcp-server does not roll it |
| code 3/3 | `configs/datasets.yaml`, which reaches the pods as the `datasets-config` ConfigMap | **only `scripts/deploy.sh` refreshes it** (`kubectl create configmap datasets-config --from-file=…`). Neither `build.sh` nor `rollout.sh` recreates that ConfigMap at all |

Code 3/3 is the one that gets forgotten and the one that matters most for this rename:
`configs/datasets.yaml` is the schema the model reads when it composes **ad-hoc** SQL, so a
rollout that ships only the mcp-server image leaves the LLM confidently writing `mlogp`
against a view that no longer has it. The tool's own hardcoded SQL is fixed by code 1/3; the
model's improvised SQL is only fixed by code 3/3.

| state | view emits house names? | mcp-server SQL asks for house names? | result |
|---|---|---|---|
| neither applied (old) | no | no | works — the divergence `5wm` describes, but functional |
| **view only** | yes | no | **broken.** Every `get_hla_by_allele` call and every `hla(allele=)` script selects `mlogp`/`sebeta`/`af_alt` from a view that no longer has them → BigQuery `Unrecognized name` on every request. So does any hand-written `query_bigquery` SQL the model composed from the old schema doc |
| **code only** | no | yes | **broken, in mirror image.** The new SQL selects `mlog10p`/`se`/`af` from a view that still emits the native names → the same error on every request |
| both applied | yes | yes | works, and the two `hla()` branches agree |

**Neither single-sided state is safe** — unlike the `bff`/`results-api` and
`auth-gateway`/`chat-backend` pairs, where one leading order has a benign transitional row.
A rename is not additive on either side, so applying the committed view directly gives no
order that avoids a broken window; there is only a choice of which half is broken first.

**Use expand/contract. Do not apply the committed view directly.** It costs one extra
BigQuery application and removes the broken window entirely:

1. **Expand** — replace the view with one emitting **both** spellings (SQL below). Every
   state from here on serves old and new readers simultaneously.
2. **Ship the code** — all three artifacts above, in any order, at any pace.
3. **Contract** — apply the committed `schemas/hla_associations_v.sql` as-is.

The expanded view is a safe resting state, verified against every consumer: the TSV header
is built from the query's own `columns` so extra columns cannot desync it; the only
`SELECT *` consumer is model-composed sandbox SQL, whose schema doc names the house spelling
only; `scripts/monitor/` issues just `COUNT(*)` and `DISTINCT resource`;
`generate_resource_sql.py --lint` reads only the `CASE` block; and `gen-sandbox-docs.py
--check` compares against `configs/datasets.yaml`, not the live view.

What settles it is **rollback**. While expanded, the view serves both generations, so the
code half rolls back with a plain `kubectl rollout undo` and **no BigQuery action at all**.
Applying the committed view directly makes every rollback a two-sided, time-critical
operation under exactly the pressure that causes mistakes.

The expand-phase statement, in full — do not improvise it. The `CASE … AS resource` block is
the reason this view exists (`resource` would otherwise be the wrong `'finngen_hla'`), and it
is what an improvised `SELECT *` plus five aliases silently drops:

```sql
CREATE OR REPLACE VIEW `genetics_results.hla_associations_v` AS
SELECT
  *,
  mlogp AS mlog10p,
  sebeta AS se,
  af_alt AS af,
  af_alt_cases AS af_cases,
  af_alt_controls AS af_controls,
  CASE
    WHEN LOWER(dataset) LIKE 'finngen_hla%' THEN 'finngen'
    ELSE LOWER(dataset)
  END AS resource
FROM `genetics_results.hla_associations`;
```

This emits the base table's 14 columns, the 5 aliases and `resource` — 20 in all — so both
spellings resolve. Note it is `SELECT *`, which is precisely what the committed contracted
definition deliberately abandons; the expand phase is the one time that is wanted, because
it is what makes the state transitional rather than a new resting shape.

If the broken window is accepted anyway (a rename applied in one shot), apply the view and
roll all three code artifacts back to back, and treat the gap as an outage of the
HLA-by-allele path rather than as a resting state. Rolling **back** from that state is the
mirror image: revert the view and all three code artifacts, in either order, as close
together as possible.

**This changes the live MCP tool output shape.** `get_hla_by_allele` returns named rows built
from the view's column list, and its TSV download carries the same header, so any external
consumer — a saved script, a notebook, a downstream agent reading `mlogp`/`sebeta`/`af_alt` out
of the tool result — breaks at the moment the view is replaced, not at some later opt-in. The
`min_mlogp` **parameter** name is deliberately unchanged: it is part of the tool's public input
schema, renaming it would break callers for no gain, and it is not a column.

nginx cannot read environment variables from config directives, and a Secret must not be baked
into a ConfigMap, so `auth-gateway.yaml` keeps a literal `${INTERNAL_API_SECRET}` placeholder in
the ConfigMap — `deploy.sh`'s `envsubst` whitelist deliberately omits that name, so it survives
verbatim — and a `render-config` initContainer substitutes it from `genetics-secrets` into a
`medium: Memory` emptyDir that nginx mounts as `/etc/nginx/nginx.conf`. It substitutes that one
name only, which is what keeps `envsubst` away from nginx's own `$host`/`$email`/`$request_uri`.

Both passes use `envsubst`'s **shell-format argument** (an explicit name whitelist), never a bare
`envsubst`. That is load-bearing twice over: a bare run would expand every nginx `$variable` that
happens to collide with a set environment variable, and it would also expand `${INTERNAL_API_SECRET}`
in `deploy.sh`'s pass, baking a Secret into a ConfigMap. The whitelist is not the whole story though
— `deploy.sh` applies it to the **entire YAML document**, not just the `nginx.conf` value, so a
whitelisted name written as `${...}` anywhere in that file, *including in a `#` comment*, is
expanded. Both injected fragments are multi-line, so an expansion inside a comment breaks out of it
and the render stops being valid YAML. That is why `auth-gateway.yaml`'s own comments name
`LEGACY_REDIRECT`/`KEYCLOAK_SERVER` bare, without `${}` (`genetics-results-suite-8wh`'s sibling
`genetics-results-suite-i5v`). **Either fragment alone triggers it**, and only one of the two is
profile-gated: `KEYCLOAK_SERVER` is populated when `ENABLE_KEYCLOAK` is on (default on for `daly`,
off for `finngen`), but `LEGACY_REDIRECT` comes from the `redirect_from_host`/`redirect_to_host`
terraform variables, which have no profile coupling at all — a `finngen` deploy that configures a
legacy redirect hits the same breakage. Verify any change here by rendering with both fragments
populated and parsing the result as YAML; the bug is invisible only when *both* are empty.

That initContainer then **validates what it rendered**, because the secret lands inside an nginx
string literal and `create-secrets.sh` lets an operator supply their own value:

- the secret is checked against a **whitelist** before it is rendered: non-empty and
  `[A-Za-z0-9+/=_.-]` only, which is a superset of the `openssl rand -base64 32` alphabet
  `create-secrets.sh` generates, so it rejects nothing the tooling produces. This is the check
  that actually holds, because `nginx -t` on the rendered file catches much less than it looks
  like it does — measured against `nginx:1.27-alpine`:
  - `\` and a bare newline in the secret both render a **valid** config. nginx unescapes `\t`,
    `\r`, `\n`, `\"`, `\'` and `\\` inside a quoted string, so `abc\tdef` ships a marker with a
    literal tab in it and `nginx -t` is happy.
  - a `;` inside the quoted string is not syntax at all — `ab"; #` simply renders a header value
    that ends at the quote, i.e. a silently truncated marker from a config that validates.
  - an empty secret renders an empty header value, which nginx drops from the request entirely.
  - `nginx -t` *does* reject most `$` values as an unknown variable (`abc$zzz`, `abc${FOO}`), but
    misses any whose suffix happens to spell a real nginx variable: `abc$http_foo` and `abc$arg_x`
    both validate and both deliver `abc`. The open-ended `$http_*`/`$arg_*`/`$cookie_*` families
    make that impossible to enumerate, which is the other reason the guard exists.

  Every one of those cases ends the same way: the initContainer reports success and every browser
  chat request 401s, because the marker chat-backend receives is not the marker it holds.
- `nginx -t` against the rendered file is still run, as a check on the **template** (and on the
  few secret values it does catch) rather than as the secret's guard.
- on failure it re-renders with a placeholder secret and re-tests, to say whether the secret or
  the template is at fault. nginx's own message is printed **only** for the placeholder run:
  `[emerg]` output quotes the offending token, which for the rendered file would put a fragment
  of the secret into Cloud Logging.

`nginx -t` resolves the upstream names and the `resolver` host, both of which the main container
already resolves at startup, so this adds no dependency the pod did not have. A failing
initContainer stalls the rollout; without the check the pod becomes a crash-looping nginx, which
slips past `deploy.sh`'s `kubectl rollout status ... || true` and still prints "Deployment
complete".

- **Single service update**: `./scripts/rollout.sh <service> [tag]` — updates one deployment image (requires `REGISTRY` env var; `tag` defaults to `latest`). Known services: `frontend`, `bff`, `results-api`, `chat-backend`, `mcp-server`, `db-api`, `rag-service`. It only swaps container images, so ConfigMap-driven pods (auth-gateway) and the CronJobs need `deploy.sh`.
- **Build all images**: `./scripts/build-all.sh` — clones the service repos (branch overridable per service, all default `master` except rag-service) and builds/pushes all Docker images to Artifact Registry, including the local `monitor`, `keycloak` and `sandbox` build contexts (requires `REGISTRY` env var)
- **Build single image**: `./scripts/build.sh <service>` — clones, builds, and pushes one service's image (requires `REGISTRY` env var; branch overridable via same env vars as build-all.sh). `sandbox` is also accepted: it builds the local `sandbox/` context rather than a clone, but still clones genetics-mcp-server for the SDK.
- **Build sandbox image**: included in `./scripts/build-all.sh`; builds `sandbox/` as the `sandbox` image. It stages genetics-mcp-server's `src/` and `pyproject.toml` into `sandbox/.sdk-src/` (gitignored, removed on exit) and pip-installs the SDK `--no-deps` — the SDK is never vendored into this repo. The installed package is then pruned to the SDK's import closure (`sandbox/prune_venv.py`), and pip/setuptools are removed from the venv, before the final stage copies it. **The sandbox is skipped, loudly, when the genetics-mcp-server branch has no `src/genetics_mcp_server/sdk/`** (`master` does not today; `genetics-results-suite-4h6.11` has landed only on `worktree-db-only-architecture`), so a suite build stays green while the sandbox is unshippable. `./scripts/build.sh sandbox` fails hard in the same situation instead of skipping. Both scripts first run `./scripts/gen-sandbox-docs.py`, which regenerates `sandbox/schema/*.md` (one file per view in `configs/datasets.yaml`, plus an index) and `sandbox/stubs/*.pyi` (signature stubs read out of the staged SDK source with `ast`) — the Dockerfile copies them verbatim to `/genetics/schema` and `/genetics/sdk`. Those files are **committed and regenerated**: committed so the directories are never empty and a `datasets.yaml` change shows up in review, regenerated so the image cannot document a schema older than the canonical file. `./scripts/test-sandbox-docs.py` runs next in both scripts and gates the image: it asserts the committed copies match a fresh generation, that every view, column, enumerable column and worked example reaches a file, that every documented column carries a well-formed BigQuery type from `tables.<view>.column_types` and that a column missing one is **refused** rather than rendered with a blank type cell (`genetics-results-suite-4h6.31`), that the stubs cover **exactly** the SDK's exported surface (plus the four lifecycle helpers the generator adds), and that the correctness rules live in `datasets.yaml` rather than in the generator. Exit 1 = a property broke, 2 = the harness could not run because no SDK source is staged — it never skips silently. `build.sh sandbox` fails hard on either; `build-all.sh` folds both into the existing skip branch. Worked example SQL in `datasets.yaml` names views **bare** (`FROM credible_sets_v`), with no project or dataset prefix and no backticks (`genetics-results-suite-bee`). db-api's `_qualify_tables` rewrites a bare name to `` `{PROJECT_ID}.{DATASET_ID}.{view}` `` and only in a genuine table position — the regex anchors on `FROM`/`JOIN`, so it no longer mistakes ordinary words in string literals for tables, which is what the earlier fully-qualified convention existed to avoid. Qualifying is now the failure mode rather than the fix: db-api owns the dataset identity, so the same emitted SQL serves dev and production, and the backticks must come off with the prefix because the rewrite does not match a backtick after the whitespace. The build **also** fails while `sandbox/schema/` or `sandbox/stubs/` still hold `PLACEHOLDER*` files (`genetics-results-suite-4h6.13`, now landed). See `docs/code-execution-security.md`, "Where the image lives".
- **Create secrets**: `./scripts/create-secrets.sh` — creates k8s secrets from environment variables (includes `SLACK_WEBHOOK_URL` for the monitor). It needs the **config profile** to know whether to write `keycloak-secrets` (daly only), and reads it from `terraform/terraform.tfvars`, which is gitignored and exists only in the main checkout. Without that file it **refuses with exit 1** and the same main-checkout/worktree message `deploy.sh` prints, rather than guessing a profile and writing the wrong per-profile secrets (`genetics-results-suite-1xp`); set `CONFIG_PROFILE=daly|finngen` to run it from a worktree anyway — and that `daly|finngen` is **enforced**, not advertised: any other value (a typo, a case slip like `Daly`, or a `terraform.tfvars` with no `config_profile` line, which parses to empty) also exits 1, because an unrecognised profile would otherwise fall through to `ENABLE_KEYCLOAK=false` and skip `keycloak-secrets` silently. Before that guard existed it died with exit 2 and no output at all, because the `grep` on the missing file tripped `pipefail`.
- **Build monitor image**: included in `./scripts/build-all.sh`; builds `scripts/monitor/` as the `monitor` image
- **Deploy monitor**: included in `./scripts/deploy.sh`; applies `k8s/deployments/monitor-cronjob.yaml` with `REGISTRY` envsubst
- **Manual monitor run**: `kubectl create job --from=cronjob/monitor monitor-manual -n genetics`

## Chat instructions (user-authored prompt text)

Users store named sets of their own instructions ("I'm a statistician", "answer in Finnish") and
select one per chat; the selected set's text is appended to the chat system prompt as a second,
separately cached block. The feature spans two service repos and needed **no manifest or
infrastructure change in this one** — it lands entirely inside the chat-backend image and the
frontend bundle, on storage that already exists.

| Piece | Repo | Where |
|-------|------|-------|
| Tables, accessors and caps; envelope; per-turn resolution; two-block cached system prompt | `../genetics-mcp-server` | `db/llm_config_db.py`, `config/defaults.py`, `chat_api.py`, `llm_service.py` |
| CRUD + history API; per-message persistence; admin visibility; nightly report breakdown | `../genetics-mcp-server` | `routers/llm_config.py`, `routers/chat_history.py`, `routers/admin.py`, `scripts/analyze_conversations.py` |
| Account-menu dialog, options-row selector, API client, per-message persistence | `../genetics-results-browser` | `src/features/chat/InstructionsDialog.tsx`, `instructionSetsApi.ts`, `useInstructionSets.ts`, `LLMChat.tsx`, `ChatPage.tsx` |

New routes on the chat backend (all behind `/chat/v1/*` → chat-backend, all authenticated and
scoped to the caller):

- `GET /chat/v1/llm-config/user/instruction-sets` — the caller's non-archived sets
- `POST /chat/v1/llm-config/user/instruction-sets` — create (400 empty, 413 over the 4000-char body cap, 409 over the 20-sets-per-user cap)
- `PUT /chat/v1/llm-config/user/instruction-sets/{id}` — update (404 if not the caller's, or archived)
- `DELETE /chat/v1/llm-config/user/instruction-sets/{id}` — archive (soft delete), 204
- `GET /chat/v1/llm-config/user/instruction-sets/{id}/history?limit=` — version history (`limit` bounded 1..100)

Which set is selected reuses the existing user-settings key `selected_instruction_set` — no new
endpoint. `POST /chat/v1/chat` gains an `instruction_set_id` field: **only the id travels**, the
body is loaded server-side scoped to the authenticated user, and an id that does not resolve for
that user is ignored rather than rejected. `chat_messages.instruction_set_id` records the set in
force per message, so the admin sessions list and the nightly analyzer can both attribute an
answer to it; both resolve "the last set wins" from a total order on `(created_at, rowid)`, since
`created_at` has one-second resolution and they would otherwise be free to disagree.

Two other changes to the same request shape belong here rather than only in the edited repo's
docs. `system_prompt` is **gone** — it was used verbatim with no gate, so any authenticated user
could discard the server prompt for their turn; nothing in the suite ever sent it, and pydantic
ignores the unknown key rather than 422ing an old caller. `ChatMessage.role` is now
`Literal["user", "assistant"]`, because the same capability had a second form: a `role: "system"`
message, which the OpenAI path forwarded into a real system slot *after* the server's, where
recency favours it. Both provider paths still filter system-role messages as defence in depth,
since `stream_chat` is reachable with raw dicts. Full behaviour is documented in `../genetics-mcp-server/docs/project-spec.md`
("Instructions"); note that instructions apply to the chat path only — the standalone `mcp-server`
pod has no server-side system prompt and mounts no `chat-data` volume, so `llm_config.db` is not
reachable from `/mcp` at all.

**Storage and backup.** The sets live in `llm_config.db` on the `chat-data` PVC, alongside tool
descriptions, user settings, hashed API tokens and user feedback comments. It is the first user
text in that file that is *fed back into the model* rather than only read by admins, and it
persists there even for users who only ever use secret chat (the dialog copy says so). No new backup infrastructure is needed: the daily GCE disk snapshot of that PVC
(`terraform/backups.tf`, 14-day retention) already covers it, and restoring a `chat-data` snapshot
restores the sets alongside the conversations that reference them. Deletion is a soft archive
rather than a row removal, precisely so a restored `chat_messages` row keeps resolving. The
`analyze-conversations` CronJob already mounts the same PVC and the analyzer derives
`--llm-config-db` as `llm_config.db` beside `--db`, so its report names sets without any manifest
change.

## Chat option persistence

The four chat options (**Answer** detail, **Instructions**, **Literature search**, **Tools**) are
persisted in two layers, so that a preference follows the user across browsers while an old
conversation still reopens the way it was held.

| Layer | Storage | Written by | Read by |
|-------|---------|-----------|---------|
| User default | `user_settings` in `llm_config.db` — keys `chat_verbosity`, `chat_literature_backend`, `chat_tool_profile`, `selected_instruction_set` | only an explicit control interaction | page load, and every new chat |
| Per conversation | `chat_messages` columns `verbosity`, `literature_backend`, `tool_profile`, `instruction_set_id` | every message save | opening an existing conversation |

Opening a conversation applies **its last message's** options to the controls and deliberately does
*not* touch the user default: reading an old detailed chat must not make "detailed" the setting for
the next new chat. Starting a new chat (including secret chat) returns the controls to the default.
A conversation that predates a column reads NULL there and falls through to the user's default
rather than to the built-in one.

No new endpoints — the defaults ride on the generic `GET/PUT /chat/v1/llm-config/user/settings*`,
and the per-message values on the existing message save. Because that save is an
`ON CONFLICT DO UPDATE` full-row replace, **every** save path must carry all four fields; omitting
one clears it. Both stores live at module scope in the browser
(`src/features/chat/useChatOptions.ts`, `useInstructionSets.ts`) because `ChatPage` remounts
`LLMChat` on every conversation switch, and each keeps the current value separate from the default
for the reason above.

### Tool profiles, and the `code` profile (`genetics-results-suite-4h6.16`)

The **Tools** option above is the `tool_profile` field, and it is resolved by **two** mechanisms in
`genetics-mcp-server/src/genetics_mcp_server/tools/definitions.py`. `TOOL_PROFILES` maps a profile
to whole tool **categories** (`api`, `bigquery`, `rag`); `TOOL_PROFILE_TOOLS` maps a profile to an
explicit set of tool **names** and takes precedence over it. `null` — the default — is *no
filtering at all*, not a union of the profiles, and an unrecognised string degrades silently to
general-only rather than raising, because the value is read back from `chat_messages` rows written
by older clients.

The second mechanism exists for `code`, the minimal code-execution surface: `run_analysis`,
`list_capabilities`, `read_artifact`, `search_genes`, `search_phenotypes`,
`search_scientific_literature`, `lookup_variants_by_rsid` — **seven tools against the default 68**,
and no external (gnomAD / Open Targets) or RAG tools either. That set is not expressible as
categories: its three orchestration tools share a category with `launch_subagents`, which must stay
out, and its four search tools share `general` with 14 others. Recategorising tools to make it fit
was **ruled out** — a tool's `category` also decides what the `api` chat profile advertises and what
subagent skills declaring `tool_categories={"general","api"}` may call — so the profile layer grew
the ability to name tools instead. No existing profile's resolved set changed.

It **ships dark**: no server-side default moved, so `profile=null` still yields the full surface;
selection is per request for local A/B work, and rollback is deleting one dict entry. The bead's
`search_entities` / `search_literature` names do not exist anywhere in the codebase — the
consolidation that would create them is deferred, and revisiting it is what would change this
profile's membership. Per-profile resolved counts, including under the deployed feature flags, are
in `docs/chat-tool-reference.md` § 3.

#### Selecting a profile from the browser

The **Tools** control offers **All** (`null`), **API**, **Database** (`bigquery`) and **Code
execution** (`code`); `rag` is a real server-side profile that has deliberately never been
user-facing. The control had been commented out of `LLMChat.tsx` entirely, so the stored profile
rode along with every request while nothing could change it — which is why the row above described
a **Tools** option no one could see. It is back, with `code` added, so the small surface can be
A/B'd against the full one. The default is unchanged: **All**.

The browser's own hazard is the mirror image of the server's, and is worth stating because it reads
backwards. Every narrower — `coerceToolProfile`, the store's `resolveCurrent`, the control — maps
an **unrecognised** profile to `null`, and `null` is the **largest** surface, not the smallest. So a
list left behind by a new profile does not fail, it silently runs the maximal arm; a benchmark
driven through the browser would be invalid with no visible symptom. The server makes the opposite
call for the same input (unknown → general-only). Both are deliberate — the value comes back from
`user_settings` and from `chat_messages` rows written by older clients, so neither side may raise —
and the inconsistency is recorded rather than resolved. What keeps it safe is that `TOOL_PROFILES`
in `src/features/chat/chat.types.ts` is the single list every narrower reads, `TOOL_PROFILE_LABELS`
in `LLMChat.tsx` is a `Record<ToolProfile, …>` so a new profile is a **type error** until the UI has
decided about it (`null` there means "deliberately not offered", which is `rag`), and
`useChatOptions.test.ts` / `LLMChat.options.test.tsx` drive their cases off that list and pin the
unknown-value behaviour explicitly.

**Hazard, unresolved** (`genetics-results-suite-4h6.56`): `run_analysis` is the primary tool of the
`code` profile and has no feature flag, so on any cluster **without a deployed sandbox** a user can
now pick a profile whose main tool cannot work at all. Locally the sandbox is running and this is
fine; it is a deployment-ordering constraint, not a browser bug.

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
