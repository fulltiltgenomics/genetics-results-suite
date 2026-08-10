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
│                             #   genetics-mcp-server at build time. See
│                             #   docs/code-execution-security.md
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

For local development, `scripts/sync-datasets.sh` copies the canonical file to sibling service repos so they can run standalone. Each service's YAML loader defaults to `./configs/datasets.yaml` when `DATASETS_CONFIG_PATH` is not set. `deploy.sh` also runs `sync-datasets.sh` (best-effort) before building the ConfigMap, so the committed copies in the sibling repos don't drift from the canonical file. Note that this only keeps `datasets.yaml` in sync — the results-api product configs (`app/config/profiles/*/credible_sets.py`, `summary_stats.py`, `common.py`, etc., which hold the actual GCS file paths and the `dataset_to_resource` map) live only in genetics-results-api and are baked into its image at build time, so changes there require rebuilding and rolling out the results-api image.

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
for the strongest signals (coeliac `DQB1*02:01` is mlogp 1596), so ranking must use
`mlogp`; and each row carries the allele's imputation `info`, because rare alleles imputed
below ~0.5 produce enormous unstable betas that read as spectacular findings but are
artifacts.

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
- **`X-Goog-Authenticated-User-Email` is not a credential on its own.** It is trivially settable by anything with network reach, so results-api trusts it only when the request *also* carries `Authorization: Bearer $INTERNAL_API_SECRET` — the trusted-proxy marker. bff attaches it on both of its upstream paths: `bff/upstream.ts` for the three assembled variants routes, and `bff/passthrough.ts` for everything else under `/api`, which is where the browser's identity header is forwarded, so the header and the marker now always travel together on that hop. (The passthrough never overwrites an `Authorization` the caller already sent; in practice nginx diverts anything carrying one to `@api_bearer` before it reaches bff, and that location blanks the identity header before proxying.) Without the marker the header is ignored outright (fail closed → 401), and the asserted address is additionally held to the same `ALLOWED_EMAILS`/`ALLOWED_EMAIL_DOMAINS` allow-list as the Google-JWT path. The same rule — marker **and** allow-list — gates the `endpoint_access` usage log, so a forged header cannot mis-attribute a request and an address the auth path refuses is not logged as the requester either. The marker is only as strong as `INTERNAL_API_SECRET`, which four workloads mount (bff, chat-backend, mcp-server, results-api); it is not a boundary against those four, it is a boundary against everything else in the namespace — notably the code-execution sandbox, which has egress to results-api but no secret.
- **Identity precedence in results-api** (`app/core/auth.py:get_verified_user`), in the order it is evaluated. The asserted identity is checked *before* the bearer is mapped to a service identity, so a browser request keeps naming its real user rather than collapsing to the generic `mcp-tool`:
  1. marker **+** allow-listed identity header → **that email** (browser traffic via bff)
  2. marker **+** identity header that is *not* allow-listed → **401**, deliberately *not* downgraded to `mcp-tool` — a downgrade would let anything holding the shared secret launder a refused identity into a working request, i.e. the weaker credential would silently rescue what the stronger claim just failed
  3. marker alone, no identity header → **`mcp-tool`** (auth-gateway `@api_bearer`, chat-backend, mcp-server)
  4. Google Identity Token or user API token → that identity (unchanged)
  5. identity header alone, no marker → **401** — the hole this closes

  An empty oauth2-proxy `$email` does **not** land in case 3. nginx drops a header whose value is the empty string (that is how `@api_bearer` blanks it), so `proxy_set_header X-Goog-Authenticated-User-Email "accounts.google.com:$email"` cannot emit an empty value — it emits the bare prefix `accounts.google.com:`, which is truthy and therefore asserts the empty address: case 2, **401**, not a downgrade to `mcp-tool`. It is unreachable in production because oauth2-proxy cannot return 200 for `/oauth2/auth` without an email (its own domain check needs one).
- **Allow-list comparison** is case-insensitive on both sides (oauth2-proxy lower-cases the address before its own domain check, so a mixed-case `User@FinnGen.fi` it admits must not be rejected downstream), and a literal `*` in `ALLOWED_EMAIL_DOMAINS` means "any domain", matching oauth2-proxy's reading of the same value — without that, setting `oauth_email_domain = "*"` would authenticate everyone at the proxy and reject everyone at results-api. `*` also opens the Google-JWT path to any verified Google account, leaving `GOOGLE_TOKEN_AUDIENCE` as the only narrowing; it is a deliberate "open deployment" switch, not a default. Because matching tolerates case and surrounding whitespace, the resolved identity is **returned trimmed and lower-cased** by both `get_authenticated_user` and the usage-log extractor, so `User@FinnGen.fi` and `" user@finngen.fi "` attribute to one identity in `endpoint_access` rather than three.
- **mcp-server** is not behind oauth2-proxy; it accepts `Authorization: Bearer` tokens via **four** paths: the `MCP_API_KEY` shared secret(s); Google Identity Tokens (JWT validated against `email_verified` plus the configured email/domain allow-list); per-user API tokens issued via the chat API; and **Keycloak OAuth 2.1 access tokens** (see below). NOTE: the Google JWT path validates **Google** Identity Tokens only — programmatic access for Apple-only identities is a known follow-up.
- **MCP OAuth (resource-server) path**: when `OAUTH_ISSUER` + `OAUTH_RESOURCE_URL` are set (daly/genegenie; empty for finngen, so the path is inert there), the mcp-server acts as an OAuth 2.1 **resource server**. It validates Keycloak-issued JWT access tokens (RS256 signature via the realm JWKS, `iss`/`aud`/`exp`, then the same email/domain allow-list), and advertises RFC 9728 discovery at `/.well-known/oauth-protected-resource` (routed unauthenticated through auth-gateway; returns `WWW-Authenticate: Bearer resource_metadata=…` on 401), so MCP clients auto-discover the Keycloak authorization server. The Keycloak issuer is **path-based** (`https://<host>/auth/realms/genetics`); tokens must carry `aud` = `OAUTH_RESOURCE_URL` (`https://<host>/mcp`), enforced per client via an audience mapper. Each external app is its own Keycloak client (registered manually — no open Dynamic Client Registration); onboard one with `scripts/keycloak-register-client.sh <clientId> <redirect-uri>…` (brainzzz is the first, via `keycloak/brainzzz-client.json.template` + `scripts/keycloak-register-brainzzz.sh`). Setup is documented in `docs/keycloak-apple-signin.md`.
- **Shared bearer-auth allow-list**: `ALLOWED_EMAILS`, `ALLOWED_EMAIL_DOMAINS` and `GOOGLE_TOKEN_AUDIENCE` (used for Google Identity Token JWT validation in results-api and mcp-server, and for chat-backend's own allow-list check on the identity header) are sourced from a single Kubernetes ConfigMap `bearer-auth-allowed` (manifest: `k8s/configs/bearer-auth-allowed.yaml`), populated from `oauth_allowed_emails`/`oauth_email_domain` plus the `GOOGLE_TOKEN_AUDIENCE` export in `deploy.sh`, consumed by all three deployments (`results-api`, `mcp-server`, `chat-backend`) via `envFrom: configMapRef` to prevent config drift
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
- **Node pool**: `e2-standard-4`, **autoscaling** `min_node_count = 1` / `max_node_count = 3` (`max = 2` on the daly profile); one node is running today. A full deploy can surge past a single node; nothing prevents the subsequent scale-down from evicting chat-backend — what keeps that eviction from truncating an in-flight stream is its graceful-shutdown configuration, not the PodDisruptionBudgets in `k8s/disruption-budgets/` (which are declarative only at `replicas: 1`) — see "Node pool sizing" below
- **Networking**: VPC with private subnet, static IP for ingress
- **SSL**: Google-managed certificates for the domain configured in `terraform/terraform.tfvars`
- **Storage**: 10Gi PVC (`chat-data`) for chat-backend SQLite databases (`chat_history.db` and `llm_config.db` — the latter now holds **user-authored prompt text**, see "Chat instructions" below), file attachments, and tool result downloads; 50Gi PV/PVC (`rag-stores`) for rag-service embedding stores; 1Gi PVC (`monitor-data`) for the monitor's alert-dedup SQLite DB; 5Gi PVC (`keycloak-postgres-data`) for the Keycloak database
- **Log sinks**: `terraform/logging.tf` optionally creates two Cloud Logging → BigQuery sinks (results-api `endpoint_access` records → `genetics_api_logs`, chat-backend container logs at severity ≥ INFO → `genetics_chat_logs`), gated by `enable_log_sinks` (default `false`)
- **Backups**: Daily GCE disk snapshots of the chat-data PVC (14-day retention, configurable via `snapshot_retention_days`)
- **Terraform state**: Per-profile GCS backends (`daly.tfbackend` → `genetics-results-terraform-daly`, `finngen.tfbackend` → `genetics-results-terraform`); `deploy.sh` auto-selects based on `config_profile`

### Node pool sizing

The pool **autoscales**: `min_node_count = 1`, `max_node_count = 3` in every live
`terraform.tfvars` profile (`max = 2` on daly), on `e2-standard-4`. One node is running today.

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
are re-derived from `k8s/deployments/*.yaml` and from the live node (2026-08-07); "system"
is the per-node GKE overhead (`kube-system`, `gmp-system`, `gke-managed-cim`) measured at
876m / 1.33 GiB.

RAG is **not** profile-derived: `scripts/deploy.sh` sets `ENABLE_RAG="${ENABLE_RAG:-false}"`
unconditionally, so rag-service is off on *every* profile unless the operator exports it. Only
Keycloak is profile-derived (on for daly). A default daly deploy is therefore **10**
deployments, not 11.

| | CPU | Memory |
|---|---|---|
| one `e2-standard-4` allocatable | **3920m** | **12.96 GiB** |
| app requests, daly **as deployed by default** (Keycloak on, RAG off — 10 deployments) | 1650m | 6.44 GiB |
| app requests, daly **with `ENABLE_RAG=true`** (11 deployments) | 1900m | 6.94 GiB |
| app requests, finngen profile (no Keycloak, no RAG) | 1300m | 5.69 GiB |
| + per-node GKE system overhead | 876m | 1.33 GiB |
| rollout surge, daly (either variant — rag-service is `Recreate`, so it never surges) | +1300m | +5.69 GiB |
| rollout surge, finngen (no Keycloak) | +1050m | +5.19 GiB |
| **peak during a full deploy — daly, default** | **3826m** | **13.45 GiB** |
| **peak during a full deploy — daly, RAG enabled** | **4076m** | **13.95 GiB** |
| **peak during a full deploy — finngen** | 3226m | 12.20 GiB |

So the **daly** profile as actually deployed overshoots a single node on **memory only** —
13.45 GiB against 12.96 GiB, while its 3826m CPU peak stays under the 3920m allocatable. It
still must get a second node; only the reason is narrower than "both axes". Turning RAG on
pushes CPU over as well. The **finngen** profile fits, but with under 1 GiB of memory
headroom — and that margin disappears if the analyze-conversations (512Mi) or monitor (256Mi)
CronJob overlaps the rollout. `results-api` at 500m / 4Gi, doubling to 8Gi mid-roll, dominates
the memory term either way.

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
new pool plus cordon/drain migration, not a tfvars edit.

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
- **Every ingress rule is source-scoped; none is `from`-less.** A rule carrying `ports` with no `from:` admits *every* pod in the namespace, and six of them did. `genetics-results-suite-fad` fixed results-api and bff, `genetics-results-suite-k4t` fixed the other four. Current admitted sources (`k8s/network-policies/policies.yaml`):

  | target | port | admitted by this file |
  |---|---|---|
  | frontend | 3000 | auth-gateway (`location /`) |
  | bff | 5000 | auth-gateway (`location /api/`) |
  | results-api | 4000 | auth-gateway (`@api_bearer`), bff, chat-backend, mcp-server |
  | chat-backend | 8000 | auth-gateway (`/chat/v1/`, `= /status`), results-api, mcp-server (both POST `/chat/v1/tokens/validate`) |
  | mcp-server | 8080 | auth-gateway (`/mcp`, the two `/.well-known/oauth-protected-resource` paths) |
  | auth-gateway | 8080 | `ipBlock` 35.191.0.0/16 and 130.211.0.0/22 — **not** a podSelector |
  | oauth2-proxy | 4180 | auth-gateway |
  | db-api | 8080 | chat-backend, mcp-server |
  | rag-service | 8000 | chat-backend, mcp-server |

  The monitor CronJob is admitted **additively** by `monitor-policy.yaml` for results-api, chat-backend, mcp-server, db-api and frontend. NetworkPolicies union, so those rules are load-bearing: they are not redundant with the table above and deleting them locks the monitor out. The monitor never dials auth-gateway, which is why that one has no monitor rule.

  Only auth-gateway needs an `ipBlock`. Everything else is ClusterIP with no Ingress backend, and kubelet probes are exempt from NetworkPolicy on this cluster's ADVANCED_DATAPATH (Dataplane V2) — proven by db-api and oauth2-proxy, which have httpGet probes, podSelector-only policies and no restarts.
- **Why auth-gateway takes an `ipBlock` and no node CIDR.** It is the only Ingress backend (both `genetics-suite` rules point at it) and the only NodePort Service, but it is fronted by a **NEG** (`cloud.google.com/neg: {"ingress":true}`, NEG `k8s1-35278419-genetics-auth-gateway-8080-ec38d214` reported HEALTHY on the Ingress). Container-native load balancing means the GFE connects straight to the pod IP, so there is no NodePort hop and nothing is SNAT'd to the node address — the intuitive "add the node CIDR for NodePort SNAT" is wrong here. Confirmed from the nginx access log: health checks *and* real user traffic both arrive from 35.191.0.0/16, kube-probe arrives from the link-local 169.254.4.6, and nothing else appears at all. Note that this is the GFE's own address, not the client's — the GFE does not preserve the client IP (an external scanner logged as 35.191.151.104), and the real client is only in `X-Forwarded-For`, so no client IP can be source-filtered at this layer. The NEG path is what keeps the source out of the node CIDR, not source preservation. `130.211.0.0/22` is Google's other documented LB/health-check range and is admitted defensively. The node subnet is `finngenie-subnet` **10.0.0.0/20** (pods 10.16.0.0/14, Services 10.20.0.0/20); it is recorded only because losing the NEG annotation would make it suddenly required, and losing the site is the failure mode.
- **chat-backend applies the same trusted-proxy marker rule** (`genetics-results-suite-th2`, was a P1 hole). `auth/core.py:get_authenticated_user` honours `X-Goog-Authenticated-User-Email` only when the request also carries the marker, and holds the asserted address to `ALLOWED_EMAILS`/`ALLOWED_EMAIL_DOMAINS`; `auth/dependencies.py:auth_required` follows results-api's precedence exactly (marker + allow-listed header → that user; marker + non-allow-listed header → 401, never a downgrade to `mcp-tool`; marker alone → `mcp-tool`; header alone → 401). `auth/core.py:is_internal_caller` is the single place the secret is compared, and it accepts the marker in either transport: `X-Internal-Auth: $INTERNAL_API_SECRET` (auth-gateway's, on the only two locations that proxy to chat-backend — `location /chat/v1/` and `location = /status`) or `Authorization: Bearer $INTERNAL_API_SECRET` (results-api's and mcp-server's, unchanged). Both compare as bytes, since `hmac.compare_digest` on `str` raises `TypeError` — a 500 — for a non-ASCII value. `POST /chat/v1/tokens/validate` is the one route with no auth dependency; it calls the same helper and additionally refuses any request that carries an identity header at all, since its genuine callers are service-to-service and never assert one. Before this, forging the header granted admin (`ENABLE_ADMIN_PAGE=true`, membership tested against `ADMIN_USERS` on the forged string), read every user's chat transcripts, and minted a plaintext per-user API token via `POST /chat/v1/tokens` — which mcp-server and results-api both accept, so it pivoted into both. `GET /chat/v1/auth` is `@is_public` and reflects the identity, so it was an unauthenticated admin-membership oracle; it needed no change of its own because it resolves through the same `get_authenticated_user`. **mcp-server does not share this bug and is untouched**: it reads no identity header on any path and its ASGI gate fails closed to 401 without a `Bearer`. Two deliberate deviations from results-api:
  - `REQUIRE_AUTH=false` (local dev only; prod sets `true`) still honours the header as-is. That mode already authenticated everyone as `anonymous`, so a marker would protect nothing and only break developing as a named user.
  - The allow-list **fails open when neither `ALLOWED_EMAILS` nor `ALLOWED_EMAIL_DOMAINS` is set**, warning as it does. chat-backend only started reading them here, and the code default is `finngen.fi`; enforcing that default on a pod that had not yet picked up the `bearer-auth-allowed` ConfigMap would lock out every user of any other deployment. `k8s/deployments/chat-backend.yaml` now `envFrom`s that ConfigMap (the same terraform-rendered values oauth2-proxy itself uses, so it cannot refuse anyone oauth2-proxy admitted), which is what makes the check live in production. The marker is the half that closes the hole; the allow-list is defence in depth against a compromised holder of `INTERNAL_API_SECRET`.
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

nginx cannot read environment variables from config directives, and a Secret must not be baked
into a ConfigMap, so `auth-gateway.yaml` keeps a literal `${INTERNAL_API_SECRET}` placeholder in
the ConfigMap — `deploy.sh`'s `envsubst` whitelist deliberately omits that name, so it survives
verbatim — and a `render-config` initContainer substitutes it from `genetics-secrets` into a
`medium: Memory` emptyDir that nginx mounts as `/etc/nginx/nginx.conf`. It substitutes that one
name only, which is what keeps `envsubst` away from nginx's own `$host`/`$email`/`$request_uri`.

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
- **Build sandbox image**: included in `./scripts/build-all.sh`; builds `sandbox/` as the `sandbox` image. It stages genetics-mcp-server's `src/` and `pyproject.toml` into `sandbox/.sdk-src/` (gitignored, removed on exit) and pip-installs the SDK `--no-deps` — the SDK is never vendored into this repo. The installed package is then pruned to the SDK's import closure (`sandbox/prune_venv.py`), and pip/setuptools are removed from the venv, before the final stage copies it. **The sandbox is skipped, loudly, when the genetics-mcp-server branch has no `src/genetics_mcp_server/sdk/`** (`master` does not today; `genetics-results-suite-4h6.11` has landed only on `worktree-db-only-architecture`), so a suite build stays green while the sandbox is unshippable. `./scripts/build.sh sandbox` fails hard in the same situation instead of skipping. The build **also** fails while `sandbox/schema/` or `sandbox/stubs/` still hold `PLACEHOLDER*` files (`genetics-results-suite-4h6.13`). See `docs/code-execution-security.md`, "Where the image lives".
- **Create secrets**: `./scripts/create-secrets.sh` — creates k8s secrets from environment variables (includes `SLACK_WEBHOOK_URL` for the monitor)
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
