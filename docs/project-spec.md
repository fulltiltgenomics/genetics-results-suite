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
                     │                 (FastAPI, port 4000) directly. /api/v1/ld instead proxies out
                     │                 to the external LD API (see "Frontend CSP and the LD proxy")
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
| bff | genetics-results-browser (`bff/Dockerfile`) | 5000 | Backend-for-frontend: assembles browser `POST /v1/results` from the results-api fan-out, proxies the external LD API as `GET /api/v1/ld`, and passes other `/api/*` calls through. Shares the frontend repo and image tag; image `genetics-results-browser-bff` |
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

### Frontend CSP and the LD proxy

The frontend nginx (`genetics-results-browser/nginx.prod.conf`) serves a Content-Security-Policy
with `connect-src 'self'`, so the SPA can only issue XHR/fetch to its own origin. The LD lookup
view used to call `https://api.finngen.fi/api/ld` straight from the browser; once the CSP shipped,
every lookup failed with `TypeError: Failed to fetch`.

The call is therefore proxied server-side: the browser requests `GET /api/v1/ld` (bff
`ldRoute.ts`), which validates `variant`/`window`/`panel`/`r2_thresh` and forwards them to
`LD_API_URL` (default `https://api.finngen.fi/api/ld`, set explicitly in
`k8s/deployments/bff.yaml`). The route is mounted ahead of the generic passthrough so it never
reaches results-api. `window` is bounded to the LD server's own 100,000–5,000,000 bp range and
`panel` to `[A-Za-z0-9_-]+`; the query string is assembled with `URLSearchParams`, so a variant id
is encoded rather than spliced in; upstream 400/404 pass through, everything else becomes 502 (60s
timeout → 504). The bff has no egress NetworkPolicy, so this outbound call is permitted.

That window is the **total** width, centred on the query variant (`window=1000000` returns
±500 kb), so a pairwise lookup must ask for twice the distance between the two variants, and no
setting reaches a partner more than 2.5 Mb away. The UI clamps its request at 5 Mb and refuses a
pair further apart than 2.5 Mb up front (both derived from one `MAX_LD_WINDOW` constant in
`LDContainer.tsx`). Before that, pairs 2.5-5 Mb apart asked for a window the server rejects and
failed with a bare 400.

Note that `/api/v1/ld` is browser-only: auth-gateway sends `Authorization: Bearer` requests under
`/api/` straight to results-api (`@api_bearer`), which has no such route, so a programmatic client
gets a 404 there and should call the LD API itself.

Two consequences worth keeping in mind when editing either side:

- Any **new external host the browser must reach directly** needs an explicit CSP source added in
  `nginx.prod.conf` — the alternative (and the default choice here) is to proxy it through the bff.
  `media-src 'self' https://sound.peal.io` is the one such exception, for the remote mp3s
  `Header.tsx` plays; without it they fall back to `default-src 'self'` and are blocked.
- The CSP lives in the frontend image, the proxy in the bff image. They share a repo and tag, so
  ship them together (`build.sh frontend` + `build.sh bff`, then `rollout.sh` both).

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
│   ├── lib/env.sh            # resolves DEPLOY_ENV -> tfvars/tfbackend/.env; sourced by
│   │                         #   deploy.sh, create-secrets.sh, build*.sh and rollout.sh
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
│   ├── {daly,daly-staging,finngen}.tfbackend  # per-environment GCS state backends
│   ├── terraform.tfvars.example
│   └── terraform.tfvars.{daly,daly-staging,finngen}  # per-environment values (not committed);
│                             #   selected by DEPLOY_ENV. A bare terraform.tfvars is the legacy
│                             #   single-deployment form and must not coexist with these.
└── docs/
    ├── adding-datasets.md    # how to add a new dataset across repos/profiles
    ├── environments.md       # the three deployments, DEPLOY_ENV, and the staging runbook
    ├── datasets-yaml-schema.md  # schema reference for shared datasets.yaml config
    ├── keycloak-apple-signin.md # Keycloak broker setup, MCP OAuth clients, backup/restore
    ├── mcp-oauth-onboarding.md  # runbook for onboarding an external customer app to /mcp
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

## Authentication

- **Keycloak** is the identity broker, **enabled per deployment profile** (`ENABLE_KEYCLOAK` in `deploy.sh`, defaulting on for `daly`, off for `finngen`). When enabled it presents the provider chooser and federates **Google** and **Apple** (Sign in with Apple), exposing one OIDC issuer at `https://${KEYCLOAK_HOST}/realms/genetics`. It runs in-cluster (`k8s/deployments/keycloak.yaml`) behind the auth-gateway under the **`/auth` path on the primary domain** (`KEYCLOAK_HOST` defaults to `<domain>/auth`), so it reuses the existing DNS record, managed cert and ingress rather than needing an `auth.<domain>` subdomain; it is backed by an in-cluster Postgres (`keycloak-postgres`) with daily `pg_dump` backups to GCS. The image (`keycloak/`) is the official Keycloak plus a bundled Apple identity-provider extension. Setup, Apple Developer prerequisites, secret rotation and restore are documented in `docs/keycloak-apple-signin.md`.
- **oauth2-proxy** handles browser sessions. Its provider is profile-driven (`OAUTH2_PROXY_PROVIDER`): `oidc` against Keycloak where the broker is enabled (daly), or `google` directly otherwise (finngen). Either way it authorizes against an allow-list: one or more **domains** (`OAUTH2_PROXY_EMAIL_DOMAINS`, comma-separated) **or** specific **addresses** (the `--authenticated-emails-file`). Both lists come from terraform — `oauth_email_domain` (comma-separated, e.g. `broadinstitute.org,finngen.fi`) and `oauth_allowed_emails` (specific addresses, e.g. Apple users on `me.com`/`icloud.com`/`privaterelay.appleid.com`).
- **auth-gateway** (nginx) uses `auth_request` to validate requests against oauth2-proxy before proxying; a `location /auth/` block (injected by `deploy.sh` via `${KEYCLOAK_SERVER}`, and served without the `auth_request` since it *is* the auth endpoint) strips the prefix and proxies to Keycloak. The email returned by oauth2-proxy is passed to backends in the `X-Goog-Authenticated-User-Email` header (the `accounts.google.com:` prefix is legacy and provider-agnostic — backends read only the address after the colon).
- **results-api** also accepts `Authorization: Bearer` tokens (Google Identity Tokens or internal shared secret)
- **mcp-server** is not behind oauth2-proxy; it accepts `Authorization: Bearer` tokens via **four** paths: the `MCP_API_KEY` shared secret(s); Google Identity Tokens (JWT validated against `email_verified` plus the configured email/domain allow-list); per-user API tokens issued via the chat API; and **Keycloak OAuth 2.1 access tokens** (see below). NOTE: the Google JWT path validates **Google** Identity Tokens only — programmatic access for Apple-only identities is a known follow-up.
- **MCP OAuth (resource-server) path**: when `OAUTH_ISSUER` + `OAUTH_RESOURCE_URL` are set (daly/genegenie; empty for finngen, so the path is inert there), the mcp-server acts as an OAuth 2.1 **resource server**. It validates Keycloak-issued JWT access tokens (RS256 signature via the realm JWKS, `iss`/`aud`/`exp`, then the same email/domain allow-list), and advertises RFC 9728 discovery at `/.well-known/oauth-protected-resource` (routed unauthenticated through auth-gateway; returns `WWW-Authenticate: Bearer resource_metadata=…` on 401), so MCP clients auto-discover the Keycloak authorization server. The Keycloak issuer is **path-based** (`https://<host>/auth/realms/genetics`); tokens must carry `aud` = `OAUTH_RESOURCE_URL` (`https://<host>/mcp`), enforced per client via an audience mapper. Each external app is its own Keycloak client (registered manually — no open Dynamic Client Registration); onboard one with `scripts/keycloak-register-client.sh <clientId> <redirect-uri>…` (brainzzz is the first, via `keycloak/brainzzz-client.json.template` + `scripts/keycloak-register-brainzzz.sh`). Setup is documented in `docs/keycloak-apple-signin.md`; the end-to-end customer onboarding runbook — client registration, the email allow-list, and adding a Microsoft/Entra IdP — is `docs/mcp-oauth-onboarding.md`.
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

- **GCP Project**: Configured via `project_id` in the deployment's tfvars (`terraform/terraform.tfvars.<DEPLOY_ENV>`)
- **Region**: Configured via `region` in the deployment's tfvars
- **GKE Cluster**: Single cluster with Workload Identity for GCP API access
- **Node pool**: pinned at `min_node_count == max_node_count == 2` on `e2-standard-4` (see "Node pool sizing" below) — the autoscaler is deliberately given no room to move
- **Networking**: VPC with private subnet, static IP for ingress
- **SSL**: Google-managed certificates for the domains configured in the deployment's tfvars
- **Storage**: 10Gi PVC (`chat-data`) for chat-backend SQLite databases (`chat_history.db` and `llm_config.db` — the latter now holds **user-authored prompt text**, see "Chat instructions" below), file attachments, and tool result downloads; 50Gi PV/PVC (`rag-stores`) for rag-service embedding stores; 1Gi PVC (`monitor-data`) for the monitor's alert-dedup SQLite DB; 5Gi PVC (`keycloak-postgres-data`) for the Keycloak database
- **Log sinks**: `terraform/logging.tf` optionally creates two Cloud Logging → BigQuery sinks (results-api `endpoint_access` records → `genetics_api_logs`, chat-backend container logs at severity ≥ INFO → `genetics_chat_logs`), gated by `enable_log_sinks` (default `false`). Both filters are scoped to `resource.labels.cluster_name` — a project can host more than one deployment of the suite, and a namespace/container-only filter would route both clusters into the same dataset
- **Backups**: Daily GCE disk snapshots of the chat-data PVC (14-day retention, configurable via `snapshot_retention_days`)
- **Terraform state**: Per-environment GCS backends selected by `DEPLOY_ENV` — `daly.tfbackend` and `daly-staging.tfbackend` → `genetics-results-terraform-daly` (prefixes `genetics-results-suite` and `genetics-results-suite-staging`), `finngen.tfbackend` → `genetics-results-terraform`. With `DEPLOY_ENV` unset the legacy path applies: bare `terraform.tfvars`, backend derived from its `config_profile`
- **Deployment environments**: `daly` (production), `daly-staging` and `finngen`. `daly` and `daly-staging` are separate clusters **inside the same GCP project**, so every project-scoped resource name carries `resource_suffix` (empty vs `-staging`): the Artifact Registry repo, the Workload Identity GSA, the chat-data snapshot policy, the Keycloak backup bucket, and the log sinks with their BigQuery datasets. Cluster-scoped names (VPC, subnet, firewall, node pool) already derive from `cluster_name`. See `docs/environments.md`

### Node pool sizing

`min_node_count` and `max_node_count` in each deployment's tfvars are **pinned equal** (both `2`).
This is not a cost-tuning choice — an autoscaler with room to move breaks chat.

A full `deploy.sh` rolls every deployment at once. All of them except chat-backend, postgres
and rag-service (which are `strategy: Recreate`) surge by one extra pod, adding 1300m CPU and
5.69 GiB on top of the ~2651m / 7.91 GiB steady-state requests:

| | steady state | + rollout surge | one e2-standard-4 allocatable |
|---|---|---|---|
| CPU | 2651m | **3951m** | 3920m |
| Memory | 7.91 GiB | **13.60 GiB** | 12.97 GiB |

So a full deploy overshoots a single node on *both* axes (`results-api` at 500m / 4Gi, doubling
to 8Gi mid-roll, dominates the memory term). With `min < max` the autoscaler reliably added a
second node for the rollout, the scheduler placed pods on it, and ~15 minutes later the now-idle
node was scaled down — evicting those pods with `ScaleDown: deleting pod for node scale down`.
For chat-backend that kills an in-flight SSE response mid-answer. Pinning removes the scale-down
event entirely.

Consequences to keep in mind:

- **Raising any deployment's requests, or adding a service, can re-break this.** Re-derive the
  surge total against 2 × 3920m / 12.97 GiB before merging such a change.
- The `chat-data` PVC is `ReadWriteOnce`, so with two nodes a `Recreate` rollout can land
  chat-backend on the *other* node and stall ~20s on `Multi-Attach error` while the volume
  detaches. That is a slower deploy, not a dropped request.
- The analyze-conversations CronJob already carries a podAffinity onto chat-backend's node for
  the same PVC reason (see "Conversation analysis pipeline"); that keeps working with two nodes.
- There is still **no PodDisruptionBudget** in the namespace, and every deployment is
  `replicas: 1`. Node auto-upgrade or repair will therefore still interrupt chat. Pinning
  addresses the autoscaler, not voluntary drains.

To consolidate onto one larger node instead, note that `node_config.machine_type` is ForceNew on
`google_container_node_pool` — changing it in place destroys and recreates the pool. Do it as a
new pool plus cordon/drain migration, not a tfvars edit.

## Monitoring

### Metrics (Prometheus)

- **Google Managed Prometheus** is enabled on the GKE cluster, collecting system and workload metrics
- Metrics are stored in Cloud Monitoring (Monarch) and queryable via PromQL in Cloud Monitoring or Grafana
- Access metrics via GCP Console → Monitoring → Metrics Explorer (PromQL tab) or by deploying a Grafana instance

### Monitor CronJob

A Python-based monitoring CronJob (`scripts/monitor/`) runs once a day (schedule `0 8 * * *`, i.e. 08:00 UTC — after the 02:00 Keycloak backup and 02:30 conversation analysis, so their output falls inside the same report) and sends results to Slack. Deployed as a Kubernetes CronJob in the `genetics` namespace. The report interval, `ALERT_LOOKBACK_HOURS` and `ALERT_DEDUP_TTL_HOURS` are coupled and must be changed together — see the env-var table below.

**What it checks:**

- **Service health** (`health.py`): HTTP liveness checks against results-api `/healthz`, chat-backend `/healthz`, frontend `/`, mcp-server `/healthz`, and db-api `/health`. Then loads `datasets.yaml` and verifies each API-served dataset is present in the results-api `/api/v1/datasets` response.
- **BigQuery data coverage** (`bq_summary.py`): Queries BQ views (`credible_sets_v`, `colocalization_v`, `coloc_credsets_v`, `exome_variant_results_v`, `gene_burden_results_v`, `asm_qtl_v`, `mpra_v`) for row counts and distinct resources. For credible_sets/exome/gene_based/asm_qtl/mpra views, compares actual resources against expected from `dataset_to_resource_rules`. For colocalization views, derives expected resources from the results-api's dataset products (coloc pairs). Collection sub-resources (eQTL Catalogue `qtd*`) are collapsed to their parent. API resource names are mapped to BQ resource names via `dataset_to_resource_rules` patterns.
- **Log alerts** (`alerter.py`): Queries Cloud Logging for `severity >= WARNING` entries from `k8s_container` resources in the `genetics` namespace **of its own cluster** over the last check interval (default 24h). Groups by container, deduplicates via SQLite, and only reports new alerts. The cluster clause matters because Cloud Logging is project-wide and two deployments in one project (daly + daly-staging) both use the `genetics` namespace — without `K8S_CLUSTER` each would alert on the other's errors.

**Severity reclassification:** GKE's logging agent tags *everything a container writes to stderr* as `severity=ERROR` regardless of content, so the Cloud Logging severity is meaningless for the many services that log normally to stderr (uvicorn, postgres, batch scripts). The alerter therefore recovers the level the application itself reported by matching the message text against `_LEVEL_PATTERNS` (python/uvicorn `INFO:`, nginx `[error]`, postgres `[27] LOG:`), ranks it via `_LEVEL_RANK`, and drops anything below WARNING. Messages carrying no recognizable level fall back to the Cloud Logging severity (fail open, so unknown formats still alert). The count of dropped entries is logged to the CronJob's stdout so the suppression is never silent. Services that log progress to stderr must prefix it with a level (see `analyze_conversations.py`) or it will be reported as an error. Third-party tools whose output cannot be prefixed must be silenced at the source instead of added to the ignore list, so that their *real* errors still alert: `keycloak-postgres-backup` sets `DEBIAN_FRONTEND=noninteractive` (else debconf warns about the missing TTY on every run), buffers apt output to a temp file and replays it to stderr only when the install fails, and passes `gsutil -q` to drop the upload progress meter. Before that, one nightly backup in two surfaced ~10 phantom `[ERROR]` lines in Slack — intermittently, because the 02:00 UTC job is only inside the lookback window of the 08:00 monitor run and the 24h dedup TTL expires at almost exactly the backup's own 24h cadence.

**Ignore list:** `_IGNORE_PATTERNS` drops known-benign `(container, message regex)` pairs outright. Two sources are on it. **oauth2-proxy** probe/callback noise: invalid redirects, unparseable OAuth2 state, and `Error while parsing OAuth2 callback` (the IdP returned `?error=...`; observed as `invalid_scope`/`unsupported_response_type` from crawlers and scanners hitting `/oauth2/callback`, and `temporarily_unavailable` when a real user leaves the Keycloak login page open too long). **nginx** `upstream prematurely closed connection` on `/mcp` only: mcp-server's streamable-HTTP SSE streams are long-lived, so each pod restart kills the open ones mid-response and nginx logs one line per connection — a crash-looping mcp-server is caught by the `/healthz` check instead, and the same error on any other route (e.g. a chat-backend stream dying mid-answer) still alerts. The mcp-server query-parameter-token warning was deliberately removed from the list: the query-token fallback is off by default in genetics-mcp-server, so if it fires again someone enabled `MCP_ALLOW_QUERY_TOKEN` and credentials are travelling in URLs — that must reach Slack.

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
| `K8S_CLUSTER` | CronJob manifest (envsubst); exported by `deploy.sh` from the terraform `cluster_name` output | — (unset = no cluster clause) | Restricts log queries to this cluster. Required when a GCP project hosts more than one deployment |
| `MONITOR_DB_PATH` | CronJob manifest | `/data/monitor.db` | SQLite dedup database path (on PVC) |
| `RESULTS_API_URL` | — | `http://results-api....:4000` | Override results-api URL |
| `ALERT_LOOKBACK_HOURS` | CronJob manifest | `24` | How far back to query logs. Must equal the schedule interval: shorter leaves a blind spot between runs, longer re-reports what the previous run covered |
| `ALERT_DEDUP_TTL_HOURS` | CronJob manifest | `24` (manifest sets `23`) | How long to suppress duplicate alerts. Set just under the schedule interval so an ongoing problem re-alerts once per run; at exactly the interval, whether yesterday's dedup row has expired races cron start jitter |

**Manual trigger:** `kubectl create job --from=cronjob/monitor monitor-$(date +%s) -n genetics`

**Network policies:** `k8s/network-policies/monitor-policy.yaml` allows the monitor pod (label `app: monitor`) to reach results-api (4000), chat-backend (8000), frontend (3000), mcp-server (8080), and db-api (8080). The service account has `roles/logging.viewer` for Cloud Logging access (configured in `terraform/iam.tf`).

## Security

- Network policies enforce db-api is only reachable from chat-backend and mcp-server, and rag-service only from chat-backend and mcp-server
- **Container privileges**: every application container sets `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]` and the `RuntimeDefault` seccomp profile. `db-api` and `bff` additionally run `runAsNonRoot` (uid 10001 / 1000) since they write nothing outside their image; results-api, chat-backend and mcp-server still run as root because they raise `ulimit`, shell out to `gcloud`, cache tabix indexes, or own root-owned files on the `chat-data` PVC. chat-backend sets `fsGroup: 1032` so the pre-existing SQLite files stay writable once `CAP_DAC_OVERRIDE` is dropped.
- Workload Identity provides read-only GCP access (BigQuery + GCS) without key files
- HTTPS enforced via FrontendConfig redirect
- All services output structured JSON logs, captured by GKE fluentbit and sent to Cloud Logging

## Operational procedures

**Choosing the deployment:** every entry-point script (`deploy.sh`, `create-secrets.sh`, `build.sh`, `build-all.sh`, `rollout.sh`) resolves its target through `scripts/lib/env.sh`. `DEPLOY_ENV=<name>` selects `terraform/terraform.tfvars.<name>` (passed with `-var-file`), `terraform/<name>.tfbackend` and `.env.<name>`; `REGISTRY` defaults to that environment's own Artifact Registry repo. Known names: `daly`, `daly-staging`, `finngen`. Three guardrails exist because a mis-selection deploys across environments: `.env.<name>` **never** falls back to a bare `.env` (that would push one deployment's secrets into another's cluster), a bare `terraform/terraform.tfvars` is **refused** while `DEPLOY_ENV` is set (terraform auto-loads it *in addition to* `-var-file`, so variables the per-environment file omits would silently come from it); and an inherited `REGISTRY` that disagrees with the selected environment is **refused** (`unset REGISTRY`, or `REGISTRY_FORCE=1`) rather than allowed to push one deployment's images over another's `:latest` tags. With `DEPLOY_ENV` unset the original single-deployment behaviour applies. See `docs/environments.md`.

**Important:** `deploy.sh` does NOT build images. To ship new service code you must build first, then deploy. The typical workflow is:

1. `./scripts/build-all.sh` (or `./scripts/build.sh <service>` for one service) — builds and pushes new `:latest` images to Artifact Registry
2. `./scripts/deploy.sh` (or `./scripts/rollout.sh <service>` for one service) — applies manifests and force-restarts pods so they pull the freshly-built `:latest` images

If you only run `deploy.sh` without building, the rollout restart will re-pull whatever `:latest` currently points to in the registry (i.e. the last build), so no code changes from upstream service repos will be picked up.

- **Full deploy**: `./scripts/deploy.sh` — runs terraform apply, configures kubectl, deploys all k8s manifests; derives the container registry from the terraform `registry` output (overridable via `REGISTRY` env var) and substitutes it in k8s manifests at deploy time; `CONFIG_PROFILE` (terraform variable, default `daly`) selects the data profile for results-api (`daly` or `finngen`); creates a `datasets-config` ConfigMap from `configs/datasets.yaml` and mounts it into results-api and db-api pods at `/app/configs/datasets.yaml` (env var `DATASETS_CONFIG_PATH`); rag-service is skipped by default (set `ENABLE_RAG=true` to include it); after applying manifests, force-restarts all app deployments so pods pick up `:latest` images and ConfigMap changes (subPath mounts don't propagate; oauth2-proxy doesn't hot-reload). Does **not** build images — run `build-all.sh` or `build.sh` first if you need new code.
- **Branding (product name)**: the displayed product name is configurable per deployment via the `app_name` terraform variable in `terraform.tfvars` (single source of truth; default `FinnGenie`, e.g. `GeneGenie` for the daly profile). Resolution order everywhere is **`APP_NAME` env override → `app_name` in `terraform.tfvars` → `FinnGenie`**. `deploy.sh` reads it from terraform output and injects `APP_NAME` into the chat-backend pod (used by the MCP server's assistant persona in `default_system_prompt`). The frontend bakes it in at build time: `build.sh`/`build-all.sh` resolve `APP_NAME` (via `tfvar app_name` from `scripts/lib/env.sh`, reading the tfvars `DEPLOY_ENV` selected) and pass `--build-arg APP_NAME` → Dockerfile writes `VITE_APP_NAME` into `.env` → `import.meta.env.VITE_APP_NAME` (read via `src/config/appName.ts`). So setting `app_name` once in the deployment's tfvars covers both the frontend build and the backend deploy. Logos and the `finngen.fi` CORS/domain identifiers are unchanged.
- **Single service update**: `./scripts/rollout.sh <service> [tag]` — updates one deployment image (`REGISTRY` defaults to the selected environment's repo; `tag` defaults to `latest`). It only sets the image reference — the cluster acted on is whatever kubectl's current context points at, which the script echoes. Known services: `frontend`, `bff`, `results-api`, `chat-backend`, `mcp-server`, `db-api`, `rag-service`. It only swaps container images, so ConfigMap-driven pods (auth-gateway) and the CronJobs need `deploy.sh`.
- **Build all images**: `./scripts/build-all.sh` — clones the service repos and builds/pushes all Docker images to Artifact Registry, including the local `monitor` and `keycloak` build contexts (those two have no branch: they build from this repo's working tree). Per-service branches come from `FRONTEND_BRANCH`, `RESULTS_API_BRANCH`, `MCP_SERVER_BRANCH`, `DB_API_BRANCH`, `RAG_SERVICE_BRANCH` — all default `master` except rag-service (`deploy_jk`), and they are set per deployment in `.env.<DEPLOY_ENV>` (daly-staging sets them all to `staging`). `REGISTRY` defaults to the selected environment's repo
- **Build single image**: `./scripts/build.sh <service>` — clones, builds, and pushes one service's image (same `DEPLOY_ENV`, `REGISTRY` and branch env vars as build-all.sh)
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
