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
  ├── keycloak-postgres (Keycloak DB, port 5432)     — backed up daily to GCS
  └── sandbox           (code execution, port 8080)  — reachable from chat-backend ONLY; egress
                                                       limited to db-api + results-api; not applied
                                                       unless ENABLE_SANDBOX=true
```

## Services

<!-- BEGIN GENERATED: services -->

| workload | kind | container port | uid | container hardening |
|---|---|---|---|---|
| `analyze-conversations` | CronJob | — | root (unset) | no-priv-esc, drop-ALL, seccomp |
| `auth-gateway / nginx` | Deployment | 8080 | 101 | nonroot, ro-rootfs, no-priv-esc, drop-ALL, seccomp |
| `bff` | Deployment | 5000 | 1000 | nonroot, no-priv-esc, drop-ALL, seccomp |
| `chat-backend` | Deployment | 8000 | root (unset) | no-priv-esc, drop-ALL, seccomp |
| `db-api` | Deployment | 8080 | 10001 | nonroot, no-priv-esc, drop-ALL, seccomp |
| `frontend` | Deployment | 3000 | 101 | nonroot, ro-rootfs, no-priv-esc, drop-ALL, seccomp |
| `keycloak` | Deployment | 8080, 9000 | 1000 | nonroot, ro-rootfs, no-priv-esc, drop-ALL, seccomp |
| `mcp-server` | Deployment | 8080 | root (unset) | no-priv-esc, drop-ALL, seccomp |
| `monitor` | CronJob | — | 1000 | nonroot, ro-rootfs, no-priv-esc, drop-ALL, seccomp |
| `oauth2-proxy` | Deployment | 4180 | 65532 | nonroot, ro-rootfs, no-priv-esc, drop-ALL, seccomp |
| `keycloak-postgres / postgres` | Deployment | 5432 | 70 | nonroot, ro-rootfs, no-priv-esc, drop-ALL, seccomp |
| `rag-service` | Deployment | 8000 | root (unset) | ro-rootfs, no-priv-esc, drop-ALL, seccomp |
| `results-api` | Deployment | 4000 | root (unset) | no-priv-esc, drop-ALL, seccomp |
| `sandbox` | Deployment | 8080 | 65532 | nonroot, ro-rootfs, no-priv-esc, drop-ALL, seccomp |
| `keycloak-postgres-backup / backup` | CronJob | — | root (unset) | no-priv-esc, drop-ALL, seccomp |

`uid` is `runAsUser` on the container, falling back to the pod; `root (unset)` means neither sets one. The hardening column is the container's own `securityContext` — a pod-level `runAsNonRoot` is counted, the rest are not, because they have no pod-level form.

<!-- END GENERATED: services -->

The image each one is built from: `frontend` and `bff` from genetics-results-browser,
`results-api` from genetics-results-api, `chat-backend`, `mcp-server` and
`analyze-conversations` from genetics-mcp-server (one image), `db-api` from
genetics-results-db, `rag-service` from genetics-rag-service, `keycloak` and `sandbox` from
build contexts in this repo, `monitor` from `scripts/monitor/`, and `oauth2-proxy`,
`keycloak-postgres` and `keycloak-postgres-backup` from upstream images. `auth-gateway` is
nginx configured by `deploy.sh`.

`sandbox` is applied only when `ENABLE_SANDBOX=true`, which `scripts/deploy.sh` derives from
`sandbox_pool_enabled` in `terraform.tfvars` (default false); see "The sandbox Deployment".

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

The manifest additionally declares the seven sandbox per-execution limits
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
| `SANDBOX_MAX_CONCURRENT_REQUESTS` | `4` | 4 | In-flight requests per execution. The one limit with a **memory** failure mode: 4 × 16 MiB of buffered bodies. Reachable in full only by a **lone** execution: the reserve below is paid for out of the incumbents' allowance (3 other executions parked on a slot each cut a tenant to 3 concurrent, 4 parked to 2, 6 parked to 1). |
| `SANDBOX_MAX_CONCURRENT_REQUESTS_TOTAL` | `8` | 8 | In-flight sandbox requests pod-wide. Must be ≥ the per-execution value or the pod refuses to start. |
| `SANDBOX_MAX_TRACKED_EXECUTIONS` | `4096` | 4096 | Bound on the counter map itself. At the bound a *new* execution is refused, never a running one evicted. |
| `SANDBOX_RESERVED_POD_SLOTS` | `2` | 2 | Of the pod-wide slots above, how many only an execution with **nothing in flight** may take. Without it, two executions at their per-execution allowance occupy all 8 and deny every newcomer its first request; with it, filling all 8 takes four. Must leave `TOTAL − per-execution` ≥ its own value, so at the default of 2 a `TOTAL` below `per-execution + 2` fails to start. |
| `SANDBOX_REQUEST_TIMEOUT_SECONDS` | `120` | 120 | Wall clock one sandbox request may hold a slot for, matching the sandbox's own hard ceiling. Armed only for a request carrying an execution token. |

The first five are ceilings compared with `>=`, so a value below 1 would silently mean "reject
every sandbox request"; results-api raises at import instead, and likewise when
`SANDBOX_MAX_CONCURRENT_REQUESTS_TOTAL < SANDBOX_MAX_CONCURRENT_REQUESTS` or when
`SANDBOX_RESERVED_POD_SLOTS` exceeds the headroom between the two (a reserve that large would
refuse a lone execution its own documented allowance — the same lie, from the other side).
Raising any of the concurrency values raises peak buffered response memory against the pod's
8Gi limit.

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

<!-- BEGIN GENERATED: structure -->

```
CLAUDE.md                            the coding and documentation-ownership rules for this repo
LICENSE
README.md                            deployment and operations guide
benchmarks/                          inputs for the paired A/B replay benchmark; the harness itself lives in genetics-mcp-server
configs/                             canonical dataset and resource definitions consumed by results-api and db-api, and the registry of the suite's declared duplicates
  covid_hgi_pheno.json               per-phenotype metadata for external GWAS
  datasets-schema-example.yaml       schema reference with example datasets
  datasets.yaml                      the single source of truth for datasets, resources and views
  ibd_gwas_pheno.json                per-phenotype metadata for external GWAS
  rag/                               RAG experiment configs (not k8s manifests)
  twins.yaml                         the duplicates the suite keeps on purpose, netted out of check-duplication.py's counts
docs/                                everything below, and nothing else
  adding-datasets.md                 how to add a dataset across the repos and profiles
  bigquery-dev-dataset.md            the BigQuery rehearsal dataset
  chat-tool-reference.md             verbatim transcription of what the LLM receives: tool names, descriptions and schemas, the profiles, the system prompt, and the chat surface versus /mcp
  code-execution-security.md         threat model and security design for the sandbox
  datasets-yaml-schema.md            the schema of configs/datasets.yaml
  duplication-baseline.json          the duplication ratchet's last-written snapshot, read by check-duplication.py --check
  environments.md                    the three deployments, DEPLOY_ENV, and the staging runbook
  genegenie-migration.md             record of the legacy-hostname redirect
  keycloak-apple-signin.md           Keycloak broker setup, MCP OAuth clients, backup/restore
  local-dev-vm.md                    running the whole suite from source on a VM, no docker or k8s
  mcp-oauth-onboarding.md            runbook for onboarding an external app to /mcp
  nginx-setup.md                     notes for the legacy VM nginx setup
  postmortem-code-execution-epic.md  why the sandbox epic took as long as it did
  project-spec.md                    this file
k8s/                                 manifests, applied by deploy.sh
  configs/                           bearer-auth allow-list; the oauth2-proxy allow-list ConfigMap is generated by deploy.sh and has no manifest
  cronjobs/                          applied only when Keycloak is enabled
  deployments/                       one file per workload, CronJobs included
  disruption-budgets/                PodDisruptionBudgets
  ingress/                           backend/frontend configs only — the Ingress and ManagedCertificate are generated by deploy.sh from the terraform `domains` list
  namespace.yaml                     the `genetics` namespace
  network-policies/                  network isolation rules, including the sandbox's
  volumes/                           PersistentVolumeClaims
keycloak/                            Keycloak image build context, realm/client/IdP templates and the login theme
sandbox/                             sandbox image build context for model-authored Python; the SDK is pip-installed from genetics-mcp-server at build time
scripts/                             build, deploy and verification scripts
  bq-dev-dataset.sh                  stand up, verify or tear down the BigQuery rehearsal dataset (docs/bigquery-dev-dataset.md)
  build-all.sh                       build and push every image
  build.sh                           build and push one service's image
  chat_usage_stats.sh                chat usage counts from the BigQuery chat-log sink
  check-doc-drift.sh                 warn when a commit changes code the docs describe
  check-duplication.py               ratchet on the suite's UNDECLARED duplication count (and on the declared one), measured from the trees themselves
  check-siblings.sh                  run each sibling repo's own discovered test lane from one place
  check-worktree-paths.sh            warn when a tool would resolve a path into the main checkout
  create-secrets.sh                  create the k8s Secrets from environment variables
  deploy.sh                          full deploy: terraform apply, then every manifest
  dev-stack.sh                       start/stop the local dev servers from one tree (docs/local-dev-vm.md)
  gen-doc-blocks.py                  generate the marked blocks in docs/*.md; `--check` is the build gate
  gen-sandbox-docs.py                generate sandbox/schema/*.md and sandbox/stubs/*.pyi
  install-git-hooks.sh               wire core.hooksPath; run once per clone
  keycloak-bind-allowlist.sh         bind the email allow-list authenticator and realm attributes
  keycloak-get-token.sh              browser auth-code+PKCE flow; prints an access token
  keycloak-register-brainzzz.sh      the brainzzz client specifically
  keycloak-register-client.sh        register or update an MCP OAuth client in the live realm
  lib/                               shared library: DEPLOY_ENV resolution, the kubectl context guard, sibling-repo resolution
  monitor/                           the monitoring CronJob's Python package
  rollout.sh                         single-service image update
  run-sandbox-local.sh               build and run the sandbox image in plain Docker
  supervisor_tests/                  the check groups scripts/test-supervisor.py runs
  sync-datasets.sh                   copy datasets.yaml to the sibling repos for local dev
  test-e2e-local.py                  end-to-end run_analysis against the live local stack
  test-manifest-render.py            offline: render every manifest deploy.sh renders, and hold each envsubst whitelist to the files it governs
  test-network-policies.py           offline: the namespace's policies as a whole
  test-sandbox-docs.py               offline: the generated schema docs and SDK stubs
  test-supervisor.py                 offline: the sandbox supervisor, in process or against a container
terraform/                           infrastructure
  backups.tf                         disk snapshot schedule and the Keycloak backup bucket
  daly-staging.tfbackend             per-environment GCS state backends, selected by DEPLOY_ENV
  daly.tfbackend                     per-environment GCS state backends, selected by DEPLOY_ENV
  finngen.tfbackend                  per-environment GCS state backends, selected by DEPLOY_ENV
  gke.tf                             the GKE cluster and its node pools, the sandbox pool included
  iam.tf                             service accounts and Workload Identity
  kubernetes.tf                      namespace and Kubernetes service accounts
  logging.tf                         Cloud Logging to BigQuery sinks, gated by `enable_log_sinks`
  main.tf                            provider config and the GCS backend
  network.tf                         VPC, subnets, static IP, DNS
  outputs.tf                         output values
  registry.tf                        Artifact Registry
  terraform.tfvars.example           per-environment values; **not committed** except the `.example`. A bare `terraform.tfvars` is the legacy single-deployment form and must not coexist with these
  variables.tf                       input variables
```

<!-- END GENERATED: structure -->

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
- **Allow-list comparison** is case-insensitive and whitespace-tolerant on both sides (oauth2-proxy lower-cases the address before its own domain check, so a mixed-case `User@FinnGen.fi` it admits must not be rejected downstream), and reproduces oauth2-proxy v7.14.3's matching forms in full: an **exact address** in `ALLOWED_EMAILS`, an **exact domain** in `ALLOWED_EMAIL_DOMAINS`, a **leading-dot** `.example.com`, which matches subdomains and *not* the bare domain, `*.example.com` as an **exact synonym** for that leading-dot form (`genetics-results-suite-zl2` — oauth2-proxy strips the star and runs the same suffix test, so the two spellings must decide alike), and a literal **`*`** meaning "any domain" — without that last one, setting `oauth_email_domain = "*"` would authenticate everyone at the proxy and reject everyone at results-api. **Since `genetics-results-suite-g8i` the bare `*` is honoured only where a gateway has already decided who gets in.** It remains allow-all on the marker-gated proxied identity-header path, and is **refused** on the token paths no proxy fronts — results-api's Google id_token path, and mcp-server's Keycloak and Google id_token paths — in both services. The reason is that one terraform variable (`oauth_email_domain`) configures oauth2-proxy *and* fills the shared `bearer-auth-allowed` ConfigMap, so a `*` written with the gateway in scope would otherwise silently widen a path no gateway fronts to any Google-verified account (and, on the Keycloak path, any realm account) with `GOOGLE_TOKEN_AUDIENCE` — inert while unset, the *public* gcloud client id when set — as no backstop. The divergence is the bare `*` and nothing else: every other form above, `*.example.com` included, keeps full oauth2-proxy parity on those paths, because it is a different value. Both services log a startup **warning** when a literal `*` is configured (results-api at import of `app/server.py`; mcp-server on the remote transports only, the sole shape in which those paths are reachable), because the operator's intent is then half-honoured rather than broken and silence about that is the whole problem repeating. Because matching tolerates case and surrounding whitespace, the resolved identity is **returned trimmed and lower-cased** by both `get_authenticated_user` and the usage-log extractor, so `User@FinnGen.fi` and `" user@finngen.fi "` attribute to one identity in `endpoint_access` rather than three.
- **mcp-server** is not behind oauth2-proxy; it accepts `Authorization: Bearer` tokens via **four** paths: the `MCP_API_KEY` shared secret(s); Google Identity Tokens (JWT validated against `email_verified` plus the configured email/domain allow-list); per-user API tokens issued via the chat API; and **Keycloak OAuth 2.1 access tokens** (see below). NOTE: the Google JWT path validates **Google** Identity Tokens only, so an Apple-only identity cannot use *that* (deprecated) branch. It does **not** block programmatic access: the per-user API token path is identity-provider agnostic — the browser dialog mints a key for whoever the oauth2-proxy session says you are (Keycloak/Apple included), and `/api` and `/mcp` validate it by token-store lookup, not by email domain. A generic OIDC verifier is a follow-up for the deprecated Google JWT branch only.
- **MCP OAuth (resource-server) path**: when `OAUTH_ISSUER` + `OAUTH_RESOURCE_URL` are set (daly/genegenie; empty for finngen, so the path is inert there), the mcp-server acts as an OAuth 2.1 **resource server**. It validates Keycloak-issued JWT access tokens (RS256 signature via the realm JWKS, `iss`/`aud`/`exp`, then the same email/domain allow-list), and advertises RFC 9728 discovery at `/.well-known/oauth-protected-resource` (routed unauthenticated through auth-gateway; returns `WWW-Authenticate: Bearer resource_metadata=…` on 401), so MCP clients auto-discover the Keycloak authorization server. The Keycloak issuer is **path-based** (`https://<host>/auth/realms/genetics`); tokens must carry `aud` = `OAUTH_RESOURCE_URL` (`https://<host>/mcp`), enforced per client via an audience mapper. Each external app is its own Keycloak client (registered manually — no open Dynamic Client Registration); onboard one with `scripts/keycloak-register-client.sh <clientId> <redirect-uri>…` (brainzzz is the first, via `keycloak/brainzzz-client.json.template` + `scripts/keycloak-register-brainzzz.sh`). Setup is documented in `docs/keycloak-apple-signin.md`; the end-to-end customer onboarding runbook — client registration, the email allow-list, and adding a Microsoft/Entra IdP — is `docs/mcp-oauth-onboarding.md`.
- **Shared bearer-auth allow-list**: `ALLOWED_EMAILS`, `ALLOWED_EMAIL_DOMAINS` and `GOOGLE_TOKEN_AUDIENCE` (used for Google Identity Token JWT validation in results-api and mcp-server, and for chat-backend's own allow-list check on the identity header) are sourced from a single Kubernetes ConfigMap `bearer-auth-allowed` (manifest: `k8s/configs/bearer-auth-allowed.yaml`), populated from `oauth_allowed_emails`/`oauth_email_domain` plus the `GOOGLE_TOKEN_AUDIENCE` export in `deploy.sh`, consumed by all three deployments (`results-api`, `mcp-server`, `chat-backend`) via `envFrom: configMapRef` to prevent config drift
- **Google token audience**: `GOOGLE_TOKEN_AUDIENCE` is the `aud` claim a Google Identity Token must carry. `id_token.verify_oauth2_token` skips the audience check when none is supplied, so while it is unset **any** Google-signed id_token with an allow-listed email is accepted — including one minted for a different OAuth client, which that client's operator could replay here. It defaults to the gcloud CLI's **public** OAuth client id (`32555940559.apps.googleusercontent.com`) — not a client id belonging to this project — because the flow it was documented for is `gcloud auth print-identity-token` and user credentials cannot request a custom audience. **What it buys is exactly cross-OAuth-client replay protection, and nothing else**: it rejects a token addressed to another client id (ADC's `764086051850-…`, a project-owned client), but every `gcloud auth print-identity-token` on earth carries that same `aud`, so a token the same user handed to any other service documenting that flow still passes. It is not an identity gate (see "Programmatic credentials" under Security), and the email allow-list remains the whole of the access control. Add further client ids, comma-separated, if service accounts call the API with audience-scoped tokens.
- **db-api** is internal-only (NetworkPolicy) **and** requires `Authorization: Bearer $INTERNAL_API_SECRET` on every endpoint except `/health`. The NetworkPolicy is not a boundary on its own: mcp-server is permitted through it and is itself reachable from outside, so anything that could drive mcp-server could reach BigQuery behind it. That path fails open (with a startup warning) if the env var is unset, so local runs and mid-rollout clusters keep working — **the sandbox token path below does not inherit that**.
- **Sandbox execution tokens** (`genetics-results-suite-4h6.9`, design: `docs/code-execution-security.md` §4). The code-execution sandbox must never hold `INTERNAL_API_SECRET`, which authenticates the *service*, never expires, and would let a model-authored script reach both backends forever. Instead chat-backend mints a **short-lived HS256 JWT per audience per execution**, signed with a *separate* key `SANDBOX_TOKEN_SIGNING_KEY` (`genetics-secrets` key `sandbox-token-signing-key`, generated by `create-secrets.sh`) that chat-backend, db-api and results-api mount and the sandbox does not. Claims: `iss=chat-backend`, `aud` = `db-api` **or** `results-api` (so a token captured from one cannot be replayed at the other), `sub` = the authenticated user, `sid` = the chat session id (this is what makes `endpoint_access` lines attributable to a conversation), `jti` = the execution id (also the `/scratch/<id>` directory name, joining logs across chat-backend, the sandbox SDK and db-api), `iat`/`exp` 5 minutes apart, and a `scope` whose presence is required and whose value is not yet interpreted. Both validators **discriminate on the JOSE `alg` header, never on dot count** — three-segment JWTs are also what every Google Identity Token looks like, and routing on dots would 401 that entire class of results-api caller. A sandbox-shaped bearer is validated only as a sandbox token: hard 401 on failure, never a fallthrough to the shared-secret comparison (which would degrade a malformed token into "is this string equal to the secret") and never on to `verify_oauth2_token`. Reading the unverified header is safe because it only *selects* a validator — each branch pins its own algorithm and key. In db-api the branch sits **ahead of** the fail-open early return; in results-api it is a new case 0 ahead of the four `genetics-results-suite-fad` precedence cases, and reports the caller as `sandbox:<user>` so a script is never mistaken for a verified human. `SANDBOX_ENABLED` (a separate required input, true once the sandbox Deployment exists) makes both services `sys.exit(1)` rather than warn when either secret is unset — without it, a script could simply omit the `Authorization` header and be served by the fail-open branch with nothing to attribute it to. The same startup gate also `sys.exit(1)`s when `SANDBOX_TOKEN_SIGNING_KEY` is **shorter than 32 bytes** ignoring surrounding whitespace: `"   "`, `"\n"`, `"x"` and `"0"` are all truthy, so the presence check passed them and they became guessable HMAC keys that mint valid principals. 32 is RFC 7518 §3.2's HS256 minimum and the threshold PyJWT's own `InsecureKeyLengthWarning` names; `create-secrets.sh`'s `openssl rand -base64 32` (44 chars) and `dev-stack.sh`'s `secrets.token_urlsafe(32)` (43) clear it. The check measures the *stripped* key and discards it — **nothing normalises the value used for signing or verifying**, because chat-backend mints with its own copy of the secret and a `.strip()` at a verifier would 401 every legitimate token minted from a key deployed with a trailing newline; such a key is logged as a startup **warning** instead. Both validators also assert the minter's invariants rather than trusting them: an **empty** `sub`, `sid` or `jti` is rejected (PyJWT's `require` catches only missing/null, and a blank one attributes the query to nobody), and `aud` must be a **string**, because PyJWT reads a list `aud` as membership and `["db-api","results-api"]` would otherwise validate at both services. Both pass `leeway=5` to `jwt.decode` for minter/verifier clock skew — the 300s ttl covers skew only in the past direction, while PyJWT ≥ 2.10 rejects `iat > now` outright — and the separate 300s `iat` age check stays exact. The principal each validator resolves is left on `request.state` (`request.state.principal` in db-api — a `SandboxPrincipal`, the string `"internal"`, or `None`; `request.state.sandbox_principal` in results-api), which is the hook the caps below key on.
- **Per-credential row and byte caps** (`genetics-results-suite-4h6.28`, design: `docs/code-execution-security.md` §4). The tight limits are the **default**, relaxed only for a *verified non-sandbox* credential — the inverse of keying them on the sandbox audience, which would let a caller widen its limits by presenting a weaker credential or none at all. **db-api**: `maximum_bytes_billed` 50 GB per query (vs the operator's `MAX_BYTES_BILLED`, 100 GB), a 25 000-row response cap (vs `MAX_ROWS`, 100 000), and an aggregate **200 GB per `jti`** enforced by a bounded in-process LRU counter — over budget is a **429, never a truncated result**. The budget spans **all four** of db-api's BigQuery paths: `/query` charges the dry run's estimate *before* the bytes are spent and reconciles afterwards, while `/schema`'s distinct-value scans, `/stats` and `/tables/{t}/sample` — none of them cached at the HTTP layer, and none with a dry run to price them — go through one shared helper that refuses to start a job once the budget is spent and charges what the job processed once it finishes, so the budget can be overshot by at most one query's `maximum_bytes_billed`. `/schema`'s scans run at the **triggering** caller's ceiling and are charged to it; previously they passed no request and so ran at the relaxed 100 GB ceiling, twice the sandbox per-query cap, for free. That does not contaminate the shared `_get_categorical_values` cache across callers, because a job over the triggering caller's ceiling fails and leaves the cache unpopulated for the next caller to retry. Charge and reconcile are in `total_bytes_processed`, not `total_bytes_billed`: a dry run reports only the former, so it is the one figure available on both sides of the correction. A query that raises between the charge and the reconcile is refunded in a `finally`, so syntax errors do not consume a budget they never spent. The relax condition here is exactly one thing, a successful `hmac.compare_digest` against `INTERNAL_API_SECRET`; the fail-open branch's `None` principal stays tight. The row cap is clamped **in the handler**: `QueryRequest.max_rows` carries a class-level `le=MAX_ROWS` evaluated once at model-definition time, so it cannot vary per credential, and tightening the module-level `MAX_ROWS` would move that bound for every caller in the process. The `/query` response **reports the ceiling it actually applied** as `max_rows_applied`, because `truncated` alone says the rows are a positional prefix without saying where the cut fell, and the two candidate ceilings differ by 4x — mcp-server's SDK quotes that number in the error it raises instead of hardcoding one. Additive: no existing `QueryResponse` field changed, since chat-backend and mcp-server both parse it. The counter is in-process and db-api runs `replicas: 1` with no HPA, so today it is exact — **at more than one replica it would bound spend per replica, not globally**; `k8s/deployments/db-api.yaml` carries a comment on `replicas: 1` saying so, and a cross-replica budget needs shared state and is deliberately not in v1. **results-api** carries its own response-**byte** cap (16 MiB) and **no row cap**, enforced by `SandboxResponseCapMiddleware` innermost of GZip so it measures the payload the caller decodes; a capped response is buffered precisely so the answer can be a 429 rather than a truncated stream, the buffer is handed downstream without a copy, and a relaxed response is never buffered or inspected. The row cap was removed deliberately: counting rows meant `json.loads` over the whole body on the event loop — a memory amplifier only a sandbox caller could trigger, on a `replicas: 1` pod — and it never bound TSV, the default `format` of every bulk range endpoint, while the byte cap was already the binding one. Exceeding the cap now **tears the producer down** by raising out of `send`, rather than discarding chunks a generator keeps producing; that generator is GCS range reads plus the tabix filter pool on the real endpoints. Its relax condition is **broader** — *any* verified non-sandbox principal (shared secret, Google id_token, or per-user chat API token) — because auth-gateway's `@api_bearer` location routes programmatic clients straight here with their own token and deliberately no shared secret, so an hmac-only rule would put verified humans on the sandbox caps on the bulkiest endpoints in the suite. Two cases reach a handler with no principal resolved and are decided on their own terms rather than by defaulting: an `@is_public` route — re-derive the set with `grep -rn "@is_public" app/`; today **seven**: `/api/v1/rsid/variants` GET and POST, `/api/v1/variant_sets`, `/api/v1/variant_sets/{name}`, `/api/v1/auth` (the route the code registers; this doc previously called it `/auth/status`), plus `/api/v1` and `/healthz` in `app/server.py` — where `auth_required` returns before `get_verified_user`, and `REQUIRE_AUTH=false` (dev only; the shipped `results-api.yaml` sets `"true"`). Both are **relaxed**. The `@is_public` case exists only while `SANDBOX_ENABLED` is `"false"`: with the sandbox deployed the anonymous surface collapses to `/healthz`, so six of the seven get whatever their caller's principal earns them instead. Measured, tight caps there would truncate nothing today — the largest possible public response is 888 rows / 18.6 KB (`variant_sets/FinnGen_enriched_202505`) against a 16 MiB cap. What makes that exception carry **zero security delta** is not the caps but that every public route bounds its own response **for every caller**: `POST /rsid/variants` used to read an unbounded body and answer one object per id, so a script omitting its sandbox token got a strictly looser limit than the same script presenting it — the core invariant, broken — and it now enforces `MAX_RSIDS` (5 000) uniformly, with no sandbox special case, plus a bounded body read. 5 000 comes from the GET's own ceiling: h11 caps the request line and headers at 16 KiB and the shortest id costs 4 bytes in the query string, so no working GET carries more than 4 096. The measurements are in `docs/code-execution-security.md` §4. The sandbox principal is also resolved **before** both short circuits in `app/dependencies.py:auth_required`, so a sandbox token is capped on a public route and under `REQUIRE_AUTH=false` too — necessary, but not sufficient on its own, since it only tightens the caller that chose to identify itself.
- **results-api per-execution limits** (`genetics-results-suite-4h6.29`, design: `docs/code-execution-security.md` §4). The 16 MiB cap above bounds **one** response; a script has ~120 s of wall clock and nothing bounded how many responses it asked for, at what concurrency, or how many bytes it accumulated. `app/core/sandbox_budget.py` was shaped on db-api's `_jti_bytes` byte-budget map — one in-process map keyed on `jti`, checked **before** the handler runs, 429 rather than truncation — and it is no longer an analogy in one direction only: db-api has since gained a direct port of *this* module beside `_jti_bytes` (`api/sandbox_budget.py`, next bullet), so the two are siblings — with four limits, all env-configurable (db-api's BigQuery byte and row caps are module constants; its request-count and concurrency limits, added later by `genetics-results-suite-4h6.61`, are env-configurable under the same names as here): aggregate response bytes per `jti` **1 GiB** (`SANDBOX_AGGREGATE_RESPONSE_BYTES_BUDGET`), requests per `jti` **1000** (`SANDBOX_MAX_REQUESTS_PER_EXECUTION`), concurrent requests per `jti` **4** (`SANDBOX_MAX_CONCURRENT_REQUESTS`) and pod-wide **8** (`SANDBOX_MAX_CONCURRENT_REQUESTS_TOTAL`). Concurrency is the one with a **memory** failure mode rather than a cost one — each in-flight capped request buffers up to 16 MiB on a `replicas: 1` pod that preloads the gene maps and the search index — which is why it exists at all and why the pod-wide bound is there even though the sandbox's own `concurrency: 1` makes it unreachable today. All seven env vars are declared at their defaults in `k8s/deployments/results-api.yaml` (table under "results-api deployment tuning"), and each is validated at import: below 1 turns a `>=` ceiling into "reject everything", `..._TOTAL` below the per-execution value makes the per-execution number a lie, and a `SANDBOX_RESERVED_POD_SLOTS` above the headroom between the two would refuse a lone execution its own allowance, so all three refuse to start. Admitted and released inside `SandboxResponseCapMiddleware`, whose `finally` the ASGI contract puts after the last byte of the response, a `StreamingResponse` included; the middleware verifies the bearer itself, non-raising, because `request.state.sandbox_principal` is set later by `auth_required` and a request-count bound has to be admitted before the handler runs. The reason the release cannot move to a dependency teardown is **not** that a streaming generator outlives it — measured on FastAPI 0.136.1 a `yield` dependency's exit code runs *after* the response body, so for a matched route the two are indistinguishable — but that `admit` runs for every request while a dependency is solved only for a **matched route**: an unmatched path 404s out of the router with no dependency entered, stranding the slot permanently, since `_sweep_locked` will not evict an entry with `in_flight > 0`. Bytes are charged from what was **sent**, taken from the cap middleware's own buffer, so the two cannot diverge or double-count. **Every status is buffered, capped and charged, not only 2xx**: "an error body is small" was false, because FastAPI's 422 handler echoes the offending input (measured: a 100 000-char query param produced a 100 144-byte body, and a 200 014-byte body was delivered under a 500-byte cap uncharged), which made the real egress bound the request count × whatever fits in a URI. An over-cap 2xx still becomes a 429 while an over-cap non-2xx keeps its own status with the same bounded stub body, because rewriting a 404 into a 429 loses the answer; only that stub case is uncharged. **Reject, never queue**: queueing burns the sandbox's clock on a wait the script cannot see and can admit work that finishes after its execution is dead, the same waste `4h6.28` removed. Every 429 carries `code`, `limit` and `observed` (`sandbox_response_bytes`, `sandbox_aggregate_bytes`, `sandbox_request_count`, `sandbox_concurrency`, `sandbox_concurrency_pod`, `sandbox_concurrency_pod_share`, `sandbox_execution_tracker_full`), and the two non-429 rejections are the deadline's: `sandbox_request_timeout` answers **504**, and `sandbox_request_timeout_after_send` answers nothing at all, because the response had already begun — counted rather than swallowed, since the slot was pinned for the full deadline either way. Eight codes in all. **Cleanup cannot evict a live execution** — the deliberate departure from db-api's LRU, which can drop a running counter and silently reset its budget: an entry is evictable only once its token is past the point `verify_sandbox_token` would accept it *and* nothing is in flight under it (covering a stream that outlives its own token); the map is hard-bounded at `SANDBOX_MAX_TRACKED_EXECUTIONS` (4096) and at the bound refuses the *new* execution rather than evicting a running one. In-process, so `replicas: 1` is load-bearing exactly as on db-api and `k8s/deployments/results-api.yaml` now carries the matching comment. **Two of the cross-tenant denial surfaces are now closed and one is not** (`genetics-results-suite-yv4`, `docs/code-execution-security.md` §4). (a) A **request deadline**, `SANDBOX_REQUEST_TIMEOUT_SECONDS` (120s, the sandbox's own hard ceiling), armed in the same `try` whose `finally` releases the slot — so the release on the timeout path is the same line as on the happy path — and only for a request carrying an execution token. Nothing bounded this before: a request wedged in a GCS read held a per-execution slot, a pod-wide slot **and** a map entry `_sweep_locked` may never evict, so the rule that closes the fail-open direction had no counterpart bounding how long an entry may stay unevictable, and a handful of hung requests reached the pod-wide bound with no attacker. uvicorn has no per-request timeout to set (`timeout_keep_alive` bounds an idle connection, not a request in progress), and an outer middleware would make deadline and release two separately-ordered layers. (b) **Pod-wide fairness**, `SANDBOX_RESERVED_POD_SLOTS` (2 of 8): an execution already holding a slot cannot take the last two, so two executions can no longer occupy every slot and deny a newcomer its first request; nothing is preempted and nothing is queued. The reserve cannot bind before a lone execution reaches its own allowance — the import check guarantees it — so today's `concurrency: 1` behaviour is unchanged. (c) **`sandbox_execution_tracker_full` remains cross-tenant and deliberately so**: refusing the newcomer is the direct cost of the fail-closed eviction rule, and shedding the oldest idle entry would trade a bounded denial for an unbounded one, since a shed entry's token still authenticates and would return with its budget reset. **Denials are observable** (item 4): a pod-wide denial logs at **ERROR**, a self-inflicted per-execution one stays a WARNING, every line carries process-lifetime admission/rejection counters and high-water marks (`sandbox_budget.stats()`), and one INFO line per *execution* (not per request) records admissions, so a denied hour and a quiet hour no longer produce the same log. `tests/test_sandbox_budget.py` (44 tests, offline lane) is the only thing that will report a regression here.
- **db-api per-execution request count and concurrency** (`genetics-results-suite-4h6.61`, design: `docs/code-execution-security.md` §4). The 200 GB aggregate byte budget above bounds **spend** and nothing else: db-api's paths that run no BigQuery job — `/health`, `/docs`, `/redoc`, `/openapi.json`, an unmatched path, `/schema` on a categorical-value cache hit, `/stats`' `get_table` metadata loop — charge it nothing and were reachable in a loop at unbounded concurrency for the execution's whole 60-120 s wall clock. db-api is `replicas: 1` at `cpu: 500m` / `memory: 512Mi` and is called by chat-backend and mcp-server as well as by the sandbox, so that is an availability problem on the browser's chat path rather than a spend one. `api/sandbox_budget.py` ports results-api's rule rather than inventing one: same constant names and defaults (1000 requests per `jti`, 4 in flight per execution, 8 pod-wide, 4096 tracked executions), same env-var handling with the same import-time refusals (below 1, or pod-wide below per-execution) and the same declaration of all four at their defaults in `k8s/deployments/db-api.yaml`, same `detail`/`code`/`limit`/`observed` 429 payload and the same four `code` labels, and the same expiry-based sweep that refuses to evict an execution whose token can still authenticate or that has something in flight. Enforced by `SandboxBudgetMiddleware`, registered **inner of CORS** but still outside the router, because db-api's `require_auth` is an app-level `Depends` solved only for a **matched** route — an unmatched path 404s out of the router with no dependency entered, so a dependency placement would neither count it nor release its slot, and `_sweep_locked` would never reclaim the entry. For the same reason the middleware resolves the sandbox principal off the raw ASGI headers itself instead of reading `request.state.principal`, which is set later. **A request carrying no sandbox token is untouched**: chat-backend and mcp-server authenticate with `INTERNAL_API_SECRET`, the kubelet probes `/health` with nothing, and neither creates an entry or can ever be rejected — the property that keeps production chat working, pinned first in `tests/test_sandbox_budget.py`. The older `_jti_bytes` byte-budget map is deliberately untouched and still a 1024-entry LRU, so db-api now has two per-`jti` maps with different eviction policies; consolidating them is not part of this change.
- **The no-credential path into the counters is closed; the internal-secret path is not** (`genetics-results-suite-0lf`, design: `docs/code-execution-security.md` §4). The four counters above are admitted from the `Authorization` header, so a request carrying none is counted against **nothing** — and the sandbox's NetworkPolicy egress reaches `results-api:4000` directly, bypassing auth-gateway, so a script could shed all four by omitting the header on any of the seven `@is_public` routes (measured: 20/20 header-less requests served with the counter map empty). That half is closed by **shrinking the anonymous surface, not by identifying the caller**: `app/dependencies.is_public_endpoint` treats only `ALWAYS_ANONYMOUS_PATHS` — `/healthz` — as servable with no principal whenever `ANONYMOUS_SURFACE_MINIMAL` is on, so every route touching a data path answers 401 to a request carrying nothing. results-api still cannot tell a sandbox request from a browser request — both arrive on `:4000` in-cluster — and for this half it does not have to. **It is *not* true that the only way into a handler is to present a credential whose presentation calls `admit`**, and earlier drafts of this bullet said so wrongly: `admit` is reached only from `_sandbox_principal`, which accepts an HS256 sandbox token and nothing else, while `INTERNAL_API_SECRET` satisfies `is_internal_caller` — measured against the real ASGI app with `SANDBOX_ENABLED=true`, `Authorization: Bearer $INTERNAL_API_SECRET` gets **200** on `/api/v1/rsid/variants` and `/api/v1/variant_sets` as `user_email=mcp-tool` with the counter map still empty. **The sandbox's half of that residue is now closed in the transport**: genetics-mcp-server's `tools/executor.py` builds its client from the per-execution tokens whenever `SANDBOX_TOKEN_FILE` names them, attaches the audience-bound token per destination, and **never** attaches `INTERNAL_API_SECRET` alongside or instead — the two paths are mutually exclusive in `_build_client`, because preferring the secret would silently re-open this. An unusable token file raises rather than falling back to the secret or to no header. **`genetics-results-suite-4h6.7`** keeps the Deployment half: the sandbox is never given the secret, which matters independently of what the SDK prefers, since a script that can read `os.environ` can build its own client. **What remains is intentional and is not the sandbox's**: results-api still serves an internal-secret caller unaccounted, because chat-backend, mcp-server and bff legitimately authenticate that way and none of them is a per-execution tenant — pinned by `tests/test_anonymous_surface.py::test_the_internal_secret_path_survives_but_the_sdk_no_longer_takes_it`, which asserts both halves. The rollout hazard an earlier draft warned about — flipping the flag early leaves the SDK working while the counters bind nothing — is gone with the transport: a sandbox request now resolves a principal, so `admit` runs. **Requiring a principal does not yet cost the browser nothing.** The BFF attaches the shared secret only on its **typed** upstream routes (`bff/upstream.ts`); the browser reaches all six narrowed routes through the BFF's **generic passthrough** (`bff/passthrough.ts`), which attaches no credential — measured against the live cluster, a header-less request through the *deployed* BFF still gets **200** from `/api/v1/auth`, and the passthrough fix exists only in genetics-results-browser's un-deployed `db-only-architecture` worktree. Usage logging cannot settle this either way: it cannot attribute callers on `@is_public` routes at all, because `state.authenticated_user` is never set there. **The control is `ANONYMOUS_SURFACE_MINIMAL`, not `SANDBOX_ENABLED`, and it defaults to on**. It was gated directly on `SANDBOX_ENABLED` at first, which made one switch both the incident lever and the security lever with the security side failing **open**: `SANDBOX_ENABLED=false`, the routine action for killing the sandbox under pressure, silently re-opened all six routes. `SANDBOX_ENABLED=true` now merely *forces* the minimal surface, and widening it is an explicit `ANONYMOUS_SURFACE_MINIMAL=false` that the sandbox overrides. Defaulting it on **does** change behaviour at the next results-api deploy — those six routes stop answering anonymous callers now rather than at sandbox rollout. Most in-cluster callers admitted to `results-api:4000` by `k8s/network-policies/policies.yaml` already present a credential (auth-gateway forwards the client's own bearer, chat-backend and mcp-server send `INTERNAL_API_SECRET`), and nothing from outside the cluster reaches results-api without going through auth-gateway — but **two callers do not, so this is a three-service ordering constraint: `bff` → `mcp-server` → `results-api`.** (1) The **browser**: the BFF's credential-less generic passthrough serves all six of these routes and the fix is un-deployed (above), so results-api first means a 401 on the login-state probe, variant sets and rsid lookups. (2) An **mcp-server pod with `INTERNAL_API_SECRET` unset**, whose tool executor fell back to sending **no** `Authorization` header; `genetics-results-suite-618` turned that into a startup failure. Deploying 618 first does **not** keep that pod working — it converts a bare 401 with no local signal into a CrashLoopBackOff naming the variable. Diagnosability, not availability. **Nothing enforces the order**: `scripts/rollout.sh` documents it in its `ORDERING:` header, while `scripts/deploy.sh` restarts every Deployment in one unordered loop with results-api ahead of chat-backend and mcp-server (a warning now sits next to its `DEPLOYS` list). **Rejected**: removing `results-api:4000` from the sandbox's egress allow-list, because the SDK genuinely calls a public route (`search(rsids=...)` → `GET /v1/rsid/variants`) and 16 of its 25 functions are results-api-only; requiring the *sandbox* token specifically, which results-api cannot ask for without identifying the caller and which `/healthz` cannot satisfy for the kubelet; and a pod-wide anonymous-request bucket, which is a rate limiter that would 429 browser traffic. Enforced by `tests/test_anonymous_surface.py`, which reads the **live route table** so a new `@is_public` decorator fails a test rather than silently reopening the hole; `scripts/test-network-policies.py` cannot see route decorators and is not the right home for it.
- **Internal calls**: chat-backend authenticates to results-api via `INTERNAL_API_SECRET`
- **A deployed service never falls back to no credential** (`genetics-results-suite-618`, the same contract as `4h6.9`). genetics-mcp-server's `tools/executor.py` built its client header as "bearer if `INTERNAL_API_SECRET` is set, **no header at all** if it is not", so an unset variable made every call to results-api and db-api anonymous — silently, at request time, and invisibly at the far end, since results-api's usage log attributes callers by the secret and never sees a principal on a route that resolves none (measured: 246/246 NULL `user_email` on `GET /api/v1/rsid/variants` over 90 days, which distinguishes an anonymous caller from an internal one not at all). **Only `k8s/deployments/mcp-server.yaml` marks that `secretKeyRef` `optional: true`**, so a missing key in `genetics-secrets` leaves the variable unset and that pod starts anyway — mcp-server is the only one of the two that can reach the silently-anonymous state. `k8s/deployments/chat-backend.yaml` sets no `optional` on that key, so a missing key stops it at `CreateContainerConfigError` instead of starting it credential-less. Both entrypoints still call the guard, because an **empty** value satisfies the kubelet in either Deployment and reaches the process. The two deployed entrypoints now call `config.settings.require_internal_api_secret()` — `mcp_server.main()` for the remote transports, beside the existing `MCP_API_KEY` check, and `chat_api`'s lifespan when `REQUIRE_AUTH` is true — so a pod in that state crash-loops with a message naming the variable instead of issuing anonymous requests. Deliberately **not** enforced at import, in `Settings`, or in `ToolExecutor.__init__`: a local run against an unauthenticated results-api needs no secret, and the **sandbox image holds no internal credential by design** (`_PrunedInstallSettings` — it ships only the SDK's import closure and gets a per-execution token instead, `4h6.9`/`4h6.44`). A full install that builds the client with no secret now also logs a warning naming the variable, which is the only local signal on a developer's machine. This is one leg of the ordering constraint on `genetics-results-suite-rhh` — mcp-server ships before results-api — but not the whole of it: the browser's BFF passthrough is the other credential-less caller and ships first of the three. And 618 does not keep a secret-less pod working; it makes the failure legible (CrashLoopBackOff naming the variable) instead of a bare 401.
- **`create-secrets.sh` generates `MCP_API_KEY` instead of leaving it empty**. mcp-server hard-requires the key for the sse/streamable-http transports (see 618 above, and `mcp_server.py`'s startup check) with no escape hatch, so treating it as an optional value that stays blank when unset produced a present-but-zero-byte `mcp-api-key` in `genetics-secrets` and a rollout that could not converge on a first deploy that never exported it. `k8s/deployments/mcp-server.yaml` is `replicas: 1` and nothing scales it (no HPA, and neither `deploy.sh` nor `rollout.sh` touches the replica count), so what was observed on daly-staging was **two ReplicaSets each holding one pod**: the new pod crash-looping and never reaching Ready, the old one therefore never scaled away — a stuck rollout, not two replicas down. It now gets the same reuse-or-generate treatment as `internal-api-secret`, `sandbox-token-signing-key` and `gateway-identity-secret`: an explicit `MCP_API_KEY` wins, else the value already in the cluster, else a fresh `openssl rand -hex 32` — unlike the third-party API keys it sits beside in the script, this one is a secret the deployment mints for itself, so a generated value is valid by construction rather than a guess that only fails at call time. `k8s/deployments/mcp-server.yaml`'s `secretKeyRef` for `mcp-api-key` still marks `optional: true`; that flag is now provably dead for any Secret this script creates, since `--from-literal=mcp-api-key=...` always writes the key (`optional` governs only an *absent* key, and the failure mode was always an *empty* one) — left as-is and tracked separately, not fixed here.
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
- **GKE Cluster**: **one cluster per deployment, three today** — `finngenie` and `finngenie-staging` in project `daly-finngenie`, and `finngenie` in project `phewas-development` (`docs/environments.md`). **Two of the three are production** (`daly` and `finngen`); only `daly-staging` is not, and it is a rehearsal ground for manifests and images rather than for data — see "There is no development environment" below. Each has Workload Identity available for GCP API access
- **Node pool**: `e2-standard-4`, **autoscaling** `min_node_count = 1` / `max_node_count = 3` (`terraform/terraform.tfvars.daly-staging` pins `min = max = 2` in this working tree — that file is gitignored and untracked, not part of the repo, so no clone carries it; it is the **daly-staging** profile. No tfvars for any deployment is committed, so node counts for **no** deployment — production included — are repo-derivable). Measured 2026-08-30, and **only for the two clusters this checkout can reach**: `finngenie` (daly production, project `daly-finngenie`) runs **two** general nodes, and `finngenie-staging` runs **two** general nodes plus the single-node gVisor sandbox pool. The **finngen** production cluster lives in a separate project (`phewas-development`, `europe-west1-b`), has no kubeconfig context here and 403s on `container.clusters.list`, so its node count is **not observable from this machine** — do not assert one. This line previously said "one node is running today", and `k8s/deployments/auth-gateway.yaml` reasoned from that. A full deploy can surge past a single node; nothing prevents the subsequent scale-down from evicting chat-backend — what keeps that eviction from truncating an in-flight stream is its graceful-shutdown configuration, not the PodDisruptionBudgets in `k8s/disruption-budgets/` (which are declarative only at `replicas: 1`) — see "Node pool sizing" below
- **Networking**: VPC with private subnet, static IP for ingress
- **SSL**: Google-managed certificates for the domains configured in the deployment's tfvars
- **Storage**: 10Gi PVC (`chat-data`) for chat-backend SQLite databases (`chat_history.db` and `llm_config.db` — the latter now holds **user-authored prompt text**, see "Chat instructions" below), file attachments, and tool result downloads; 50Gi PV/PVC (`rag-stores`) for rag-service embedding stores; 1Gi PVC (`monitor-data`) for the monitor's alert-dedup SQLite DB; 5Gi PVC (`keycloak-postgres-data`) for the Keycloak database
- **Log sinks**: `terraform/logging.tf` optionally creates two Cloud Logging → BigQuery sinks (`endpoint_access` records from the in-cluster services → `genetics_api_logs`, chat-backend container logs at severity ≥ INFO → `genetics_chat_logs`), gated by `enable_log_sinks` (default `false`). **A BigQuery sink names its destination table after the log ID, not after the service**, and every GKE container logs to stdout, so all `endpoint_access` rows from results-api *and* db-api land in one table, `genetics_api_logs.stdout` — that is the table to query for API usage. **Identity is available for results-api rows only**: db-api's `endpoint_access` payload (`genetics-results-db/api/main.py`) carries no `user_email` and never will — db-api sits behind results-api and the internal secret rather than in front of users, so its caller is a *service*, not a person. It now emits `principal` (`internal` / `sandbox` / `unauthenticated`) naming the credential that authorized the call, which is the only principal that exists there. **Split the two services on `jsonPayload.service` — three eras, and a query spanning them needs all three.** Both services now emit a constant, non-env-derived `service` on every `endpoint_access` line: `"db-api"` (`genetics-results-db/api/main.py`, `SERVICE`) and `"results-api"` (`genetics-results-api/app/middleware_usage_logging.py`, `SERVICE`). It is a module constant on purpose — both earlier discriminators moved underneath the queries built on them:
    1. **From 2026-08-12 (`service` exists): `jsonPayload.service = 'db-api' | 'results-api'`.** The only stable split. Prefer it whenever the window is entirely inside this era.
    2. **2026-08-12 back to the start of the table (2026-03-06): enumerate `log_source` values.** db-api rows are `log_source='genetics_db_api_prod'`; results-api rows are `'finngenie_prod'` **or** `'genetics-results-api-prod'` (renamed 2026-06-03 — see the query hazard below). `log_source` is *not* a service discriminator and is retained only as the environment axis: it is derived from `DEPLOY_ENV`, contains no service name, is asymmetric between the two services (`genetics_db_api_prod` vs `genetics-results-api-dev1` do not even share a separator), and has already been renamed once in production. Enumerate the values you find; do not assume one, and do not parse the string.
    3. **Before db-api emitted anything (up to 2026-08-12): `endpoint_path IS NULL`** identifies db-api. This worked by accident — db-api emitted neither `log_source` nor `endpoint_path`, so the discriminator was the *absence* of a field, and it silently stopped meaning "db-api" the moment db-api started emitting `endpoint_path`. Use it only for rows that predate that change.

    A query crossing an era boundary must OR the relevant tests together; a query keyed on any single one returns a silently wrong subset outside its era rather than an error. The sibling table `genetics_api_logs.genetics_results_api` is the `genetics-results-api-dev1` **GCE VM**, which reached the sink because the filter used to be project-wide. It is not decommissioned and not usage: it is a developer machine running the results-api **test suite** (`sourceLocation.file` points inside a checkout under `/home/jkarjala/suite/genetics-results-api`, and it emitted 1,638 entries within a single second), still producing rows today — 1,377 on 2026-08-11. Narrowing the sink filter to `resource.type="k8s_container"` in this cluster (`resource.labels.cluster_name`) and namespace `genetics` **stops that feed deliberately and with no replacement**; that is the point, since it is test noise. Both sinks pin `cluster_name` for a second reason: a GCP project can host more than one deployment of the suite (`daly` and `daly-staging` do), and a namespace/container-only filter would route both clusters into the same dataset. Neither table carries `httpRequest` on any row, before or since — the middleware emits a `jsonPayload`-only record, so `httpRequest.responseSize` is structurally NULL across all 278,757 rows of the pre-2026-08-27 period and stays NULL after it; `httpRequest IS NULL` therefore selects everything and is **not** an era test. No sink configuration could have changed that, since Cloud Logging fills the struct only when the caller sets it. `genetics-results-api`'s `app/middleware_usage_logging.py` instead emits the size from 2026-08-27 as a plain `jsonPayload` key, **`response_body_bytes`**, on the single stdout path (`config.use_cloud_logging_api` is dev-only); the sink grows the payload schema when a new key first appears — the same mechanism that leaves `sid`/`sub`/`jti` without columns below — so the column exists from the first row that carries it, and `response_body_bytes IS NULL` is the test for "written before the change" (a zero-byte body records `0`, not NULL). The standard field was rejected deliberately: `httpRequest.responseSize` means wire bytes *including* headers, and auth-gateway's `$body_bytes_sent` and the GCLB request logs already populate it with genuine wire bytes elsewhere in the suite, so a third source with different semantics under that name would mix silently in a join. The number is **uncompressed response body bytes, excluding headers** — the usage middleware is registered inside `GZipMiddleware`, so it measures the body the caller decodes, which is also the quantity `SandboxResponseCapMiddleware` caps — and it is therefore **not comparable with `$body_bytes_sent`**
  - **Query hazard — `log_source` was renamed.** Inside `genetics_api_logs.stdout`, results-api rows carried `log_source='genetics-results-api-prod'` (40,958 rows, only 95 non-null `user_email`) up to 2026-06-03; after that results-api emits `log_source='finngenie_prod'` (12,258 rows, 12,026 non-null `user_email` — the current value). db-api rows carried `log_source` NULL until it started emitting `genetics_db_api_prod` (2026-08-12). A query still filtering on the old value returns **nothing after 2026-06-03 and no error**, which is the same silent-empty-result trap as reading the wrong table
  - **The sink's `jsonPayload` schema has no `sid`, `sub` or `jti` column**, so the sandbox-attribution fields db-api logs on a sandbox-authorized request (`api/main.py`, `require_auth`) are **not queryable in BigQuery** — they exist only in Cloud Logging / container stdout. That is expected today, since no sandbox Deployment is applied — the manifest exists since `4h6.7` but is gated off — and `SANDBOX_ENABLED` is `"false"` on both services, so no such row has ever reached the sink to grow the schema. Do not cite BigQuery for per-execution sandbox attribution without checking the schema again
- **Backups**: Daily GCE disk snapshots of the chat-data PVC (14-day retention, configurable via `snapshot_retention_days`)
- **Terraform state**: Per-environment GCS backends selected by `DEPLOY_ENV` — `daly.tfbackend` and `daly-staging.tfbackend` → `genetics-results-terraform-daly` (prefixes `genetics-results-suite` and `genetics-results-suite-staging`), `finngen.tfbackend` → `genetics-results-terraform`. With `DEPLOY_ENV` unset the legacy path applies: bare `terraform.tfvars`, backend derived from its `config_profile`
- **Deployment environments**: `daly` (production), `daly-staging` and `finngen`. `daly` and `daly-staging` are separate clusters **inside the same GCP project**, so every project-scoped resource name carries `resource_suffix` (empty vs `-staging`): the Artifact Registry repo, the Workload Identity GSA, the chat-data snapshot policy, the Keycloak backup bucket, and the log sinks with their BigQuery datasets. Cluster-scoped names (VPC, subnet, firewall, node pool) already derive from `cluster_name`. See `docs/environments.md`
- **tfvars guard (`require_tfvars`, default `true`)**: `terraform.tfvars` and every `terraform.tfvars.<env>` are gitignored and exist only in the main checkout, so terraform run from a git worktree (`.claude/worktrees/*`) or a fresh clone would fall back to variable **defaults** — and those defaults are not a no-op subset of the live config: `enable_log_sinks=false` destroys both log sinks and their BigQuery dataset IAM members, `manage_iam=true` with an empty `node_service_account` **replaces the GKE node pool**, and `config_profile`/`oauth_email_domain` revert to the daly/Broad values. A `precondition` on `data.google_compute_global_address.static_ip` in `terraform/main.tf` asserts that **at least one** such file is present (`local.tfvars_present`, the `terraform.tfvars*` fileset minus the committed `.example`) — it cannot assert the bare `terraform.tfvars` specifically, because `DEPLOY_ENV` mode passes `terraform.tfvars.<env>` with `-var-file` and `scripts/lib/env.sh` **refuses** to run when a bare one exists alongside those. **That is a real weakening in the main checkout**: with per-environment files present, a bare `terraform apply` there satisfies the guard while still using defaults. In a worktree or a fresh clone — the case the guard was written for — the set is empty and it still fails closed. Measured behavior is that Terraform still renders the complete plan first — every resource diff, including the alarming-looking `Plan: N to add, N to change, N to destroy` — and only afterward prints `Terraform planned the following actions, but then encountered a problem:` followed by the precondition error, exiting non-zero with nothing applied. The operator will see that full destroy/replace plan scroll past above the error, which is worth knowing since it is never actually applied. `scripts/deploy.sh` no longer carries its own copy of this check: `scripts/lib/env.sh` refuses **earlier and for every entry point**, at `resolve_deploy_env`. **That is stricter than before, and it removes the worktree escape hatch**: `SKIP_TERRAFORM=true` only reads outputs from state and used to work from a worktree with `CONFIG_PROFILE` exported, but `resolve_deploy_env` runs before the `SKIP_TERRAFORM` branch is reached and exits 1 when the resolved tfvars is missing. Deploy from the main checkout. Supplying values another way requires `-var require_tfvars=false` alongside the `-var-file`. Note `project_id` and `domains` have no defaults, so a worktree run stops to prompt for them first — everything else defaults silently once they are answered. **The guard does not cover every entry point.** `terraform apply -target=<resource>` prunes the graph to the target and its dependencies; the guarded data source is a dependency of nothing (only a root output references it), so its precondition never evaluates and a targeted apply from a worktree still runs with the destructive defaults. `terraform destroy` also never evaluates it, because destroy plans are driven by state, not data sources — that gap is deliberate, not an oversight, since blocking teardown on `terraform.tfvars` presence would break legitimate destroys for no safety benefit
- **State-identity guard**: existence is not enough, because which state a run writes to is fixed by `terraform init -backend-config=`, independently of the values in place. The environments differ on `project_id`, `region`/`zone`, `domains`, `static_ip_name`, `manage_iam` and `oauth_email_domain`, so applying one environment's values into another's state writes values that are all real and plausible — the plan does not look wrong. Two checks, in order of usefulness:
  1. **`scripts/lib/env.sh`** derives `TFVARS` and `BACKEND_FILE` from the same `DEPLOY_ENV`, so they cannot disagree; in legacy mode it derives the backend from the tfvars' own `config_profile`, which is the same guarantee by construction. It additionally refuses to run when a bare `terraform.tfvars` sits beside the per-environment files, because terraform auto-loads it on top of `-var-file` and any variable the per-environment file omits would silently come from the other deployment. This is the path that matters — `deploy.sh`, `build.sh`, `build-all.sh` and `create-secrets.sh` all source it, **before `terraform init` runs at all**
  2. **A `precondition`** on `data.google_compute_global_address.static_ip` compares the bucket recorded in `.terraform/terraform.tfstate` (written by `terraform init -backend-config=`, the only in-config signal of which state this directory is bound to, and independent of every variable) against the bucket parsed out of `${var.config_profile}.tfbackend`. Backstop for a bare `terraform apply`; it inherits every limitation of the existence precondition above — full plan rendered first, silent under `-target`, never evaluated on `destroy` — and additionally passes when either side cannot be determined (never initialized, or no `.tfbackend` for that profile), both cases where terraform fails on its own. It compares **buckets only**, so it cannot separate `daly` from `daly-staging`, which share a bucket and differ only by prefix. Gated on the same `require_tfvars` — so `require_tfvars=false` disables this bucket-identity check along with the existence check above; there is no separate flag to silence one without the other

### Node pool sizing

There are **two** pools, and they are sized on different grounds.

The **primary** pool **autoscales**: `min_node_count = 1`, `max_node_count = 3` in every live
`terraform.tfvars` profile (`terraform.tfvars.daly-staging` pins `min = max = 2`; it is the staging profile, and no production tfvars is committed), on `e2-standard-4`. Two general nodes are running on each of the two clusters this checkout can reach — see the Node pool bullet above for what is and is not measurable.

The **sandbox** pool (`<cluster>-sandbox-pool`, `terraform/gke.tf`) is **pinned at one node**
and exists for isolation, not capacity — see "The sandbox pool" below. It contributes **0m and
0 GiB** to everything in the surge table that follows.

> An earlier version of this section claimed the pool was pinned at
> `min_node_count == max_node_count == 2`. That pinning was written into
> `terraform.tfvars.example` by commit 6db94e8 but **never applied to any live profile**
>. The decision has since been to keep autoscaling and handle
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
> work.** The cluster in question runs the **finngen** profile in a separate project this checkout cannot reach (see the Node pool bullet), so its node count is unverified — the premise this paragraph was written on, "on one node", is **not** established. By the re-derived
> table finngen fits (12.20 GiB peak vs 12.96 GiB allocatable). If it fits, no surge node
> should be created, and the reap-the-surge-node mechanism narrated above should not fire on
> the live profile at all. **That step rests entirely on the unverified node count**: at two or
> more nodes the autoscaler's scale-down path is available with no surge required, which would
> make the observed eviction ordinary rather than anomalous and dissolve most of this question.
> So checking that cluster's node count — from an environment that can reach `phewas-development`
> — comes before any of the candidates below. The original narrative is kept because the eviction
> was observed; what is missing is a verified trigger. Plausible candidates, none confirmed: a transient
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
`e2-standard-2`, `sandbox_config { type = "GVISOR" }`, `min_node_count == max_node_count == 1`,
created only when `sandbox_pool_enabled = true` (default false). It hosts nothing but the
code-execution sandbox.

**Why a second pool at all.** Not capacity — isolation. GKE Sandbox is a per-pool property, so
gVisor's userspace syscall boundary around untrusted LLM-authored code can only be bought by
creating a pool for it, and the consequence is that chat-backend can never be co-scheduled with
that code. GKE taints gVisor nodes `sandbox.gke.io/runtime=gvisor:NoSchedule` automatically, so
nothing else drifts onto it. Two further properties need the pool rather than the pod spec:
`pod_pids_limit` is a **kubelet** setting, and the node service account is node-scoped, so a
sandbox node can carry a minimal identity instead of the suite's.

**Why pinned when the primary pool is not.** The primary pool autoscales because its pods are
restartable request-servers with graceful shutdown. A scale-down here would kill an in-flight
script, and there is no second replica. The accepted cost is one permanently-running
`e2-standard-2` — chosen on isolation grounds alone. Do not repeat the "nearly free" framing an
earlier revision used: it rested on a belief that the primary pool was pinned at two nodes,
which measurement contradicted.

**Budget on that node.** CPU allocatable is arithmetic (2 vCPU, GKE reserves 70m → 1930m).
**Memory allocatable cannot be derived offline**: the usual capacity-minus-reservation method
provably overstates — applied to a measured `e2-standard-4` it yields 13622 Mi against a
measured 13273 Mi — so 6249 Mi is an upper bound, not a value. Against 1930m of CPU the sandbox
requests 500m and the DaemonSets that tolerate the taint measure ~383m, which leaves the 1500m
*limit* as a burst ceiling a co-scheduled workload would contend with rather than a
reservation. If the machine type changes, revisit the request and the limit together.

Two things that do **not** appear in that arithmetic. The primary node's measured system load
mostly cannot land here — kube-dns, metrics-server, konnectivity and the autoscalers do not
tolerate the gVisor taint — so quoting it for this node overstates. And the runsc sentry's
memory is charged to the **pod's cgroup**, so it eats the 3Gi limit rather than the node
headroom: it constrains the `RLIMIT_AS` budget, not this table.

`workload_metadata_config { mode = "GKE_METADATA" }` on the pool is unconditional, which
requires the cluster's `workload_identity_config` to be unconditional too — without it the pool
is rejected **at apply, not at plan**. The pool's own service account is dedicated and minimal
(logging, monitoring, resource metadata, Artifact Registry read) with explicit `oauth_scopes`,
and is a **mandatory input under `manage_iam = false` with no null fallback**. That, plus the
sandbox KSA having no Workload Identity binding, is what makes the metadata server useless from
inside the sandbox — see `docs/code-execution-security.md`.

### The sandbox Deployment

`k8s/deployments/sandbox.yaml`. Every field it sets is tabulated, generated from the manifest,
in `docs/code-execution-security.md` → "What the pod declares"; that is the enumeration, and it
is not duplicated here. What belongs here is how a deploy handles it.

The manifest is applied only when `ENABLE_SANDBOX=true`, which `deploy.sh` derives from
`sandbox_pool_enabled` in the resolved tfvars rather than being a second switch. Three refusals
sit in a **preflight that runs before the first apply of the deploy**, not in the manifest loop
where an `exit 1` would leave every other manifest applied and every rollout unrolled:

1. no node carries `workload=sandbox` — the pod tolerates a taint only the gVisor pool has, so
   applying it without the pool leaves a permanently Pending pod behind a `kubectl apply` that
   returned 0;
2. the container named `sandbox`, in the Deployment named `sandbox`, declares no `command:` or
   `args:` — the image has no `CMD` on purpose, so a manifest that lost its args would start
   `python3` with no script. Parsed with PyYAML rather than grepped, and failing **closed** on a
   file it cannot read or a missing PyYAML;
3. `${REGISTRY}/sandbox:${TAG}` is not in Artifact Registry. A definite `NOT_FOUND` is fatal; an
   unanswerable query (no `gcloud`, no `artifactregistry.reader`) is a warning, because
   inability to answer is not evidence of absence.

`build-all.sh` closes the other end: it skips the sandbox image non-fatally when the resolved
genetics-mcp-server branch has no SDK or when the generated schema docs fail to verify, but it
restates the skip as its last line and **exits non-zero when the deployment's tfvars enables the
sandbox** — otherwise a build that omitted the image followed by a deploy that applies the
Deployment is an ImagePullBackOff behind two commands that both returned 0.

`SANDBOX_ENABLED` on db-api, results-api and chat-backend is a separate, deliberate step and
stays `"false"` until it. `scripts/test-network-policies.py` reports the pairing as a note
rather than a failure **only when it has confirmed against the cluster that no sandbox
Deployment is live**: `ENABLE_SANDBOX` means "this run will not apply it", and deploy.sh
*skips* the manifest rather than deleting it, so a later gate-off deploy can run against a
sandbox that is still serving. A live sandbox with the gate off is a hard failure; an
undeterminable cluster fails closed.

`SANDBOX_URL` on chat-backend is a second variable and has no default — it ships
unconditionally, even while the gate is false, so flipping the flag is the only change the
enabling deploy makes. Confirmed on the `daly-staging` bring-up: with the address absent every
`run_analysis` failed while the sandbox pod stayed healthy and answered its probes.

### The sandbox HTTP contract

`docs/code-execution-security.md` → "The HTTP contract between chat-backend and the supervisor"
is the **definition**, and it is the only one: chat-backend's client and the supervisor cannot
import a shared module, because the image installs only the genetics SDK's import closure. Do
not restate the field list here — two copies of a wire shape is the failure that subsection
exists to prevent.

What belongs in this file is the deployment shape around it: plain HTTP on 8080, pod-to-pod,
with **no HTTP-layer authentication** (the pod holds no credential it could verify a caller
against; the ingress allow-list is the control), three routes and no fourth, and a client that
holds exactly one configuration value — a base URL — so the same image serves the cluster and a
plain Docker container identically.

### The supervisor process

`sandbox/supervisor.py` is the pod's main process and PID 1. It is deliberately one file with
no third-party imports: the image installs only the genetics SDK's import closure, so the
supervisor can use nothing the SDK does not already need. Its shape, in the order the file is
laid out — contract constants, the request parser, the per-execution directory, artifact
encryption, the manifest, the child, the fork server, the scheduler, the per-execution limits,
token delivery, the audit forwarder, HTTP, startup.

What it does per execution: admit or refuse the request against the queue bounds and the
duplicate-id rule; create `/scratch/<execution-id>`; write the token file; ask the **fork
server** for a child; watch the wall clock, the process group and the `/scratch` quotas; drain
three pipes; reap; kill whatever the child left behind; trim, seal and list the artifacts; and
answer. Every bound it enforces is a constant at the top of the file with its reasoning beside
it, and the same numbers are tabulated in `docs/code-execution-security.md`, generated from
that file.

Three structural facts are worth knowing before reading it:

- **The child is forked and not exec'd**, which is what makes `prewarm()` worth anything: the
  pre-imported numpy/scipy/polars/matplotlib pages are inherited copy-on-write.
- **The process that forks is not the supervisor.** A fork server is forked out at startup,
  before the first request body is read, and every execution child comes from that pristine
  address space. Nothing that has held a token, a request body or user source code may fork an
  execution child.
- **Supervisor and child share uid 65532**, because the pod drops the capabilities a second uid
  would need. File modes separate nothing between them; lifetime does.

**Verification.** `scripts/test-supervisor.py` is the offline harness — no cluster, no
credentials, no image — and runs the real supervisor in-process against a temporary scratch
root, forking real children. `--container URL` drives the same wire checks against the image
started by `scripts/run-sandbox-local.sh`, plus the properties only the image has. Its check
groups live in `scripts/supervisor_tests/`. Two conventions make it evidence rather than ritual:
every group that asserts a hazard is closed also **restores the defect** and asserts the same
probe goes red, and anything about a hang is driven on a thread with a deadline.


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
  - `allow-ingress-sandbox` — **chat-backend only**, 8080/TCP. This is **the network layer of the MCP-exclusion argument** (the layers, and how many of them there are, are enumerated once in `docs/code-execution-security.md` § 5 and deliberately not counted here): the user's requirement is that code execution must not be reachable via MCP, and the registration-layer exclusion of `run_analysis`/`read_artifact` is partly a runtime-assembled set that a refactor could undo, so mcp-server is denied at the network layer too. The precedent is in this very table — mcp-server sits on both sides of the db-api rule, so "anything that can drive mcp-server can reach BigQuery behind it"; the same shape applied to code execution would mean anything that can drive mcp-server can run code. `monitor-policy.yaml` is deliberately **not** extended to the sandbox.
  - `sandbox-egress` — **db-api:8080 and results-api:4000 only**, as two separate rules so the destinations and ports do not cross-product. No `ipBlock` of any kind: no internet, so no `pip install`, no mining payload, and no `pl.write_parquet('s3://…')` — polars ships the Rust `object_store` with its own HTTP/TLS stack, so this policy is the *only* control that closes s3. Also denied: keycloak, keycloak-postgres, chat-backend and rag-service (the sandbox is a leaf — it is called, it does not call back, so a script cannot re-enter the chat API with the caller's session), mcp-server, and the Kubernetes API server.
  - **No DNS rule, deliberately.** Egress to CoreDNS sustains ~200 KB/s of exfiltration through query names alone (~10³ queries/s × ~200 usable base32 bytes), needing no POST and no response — tens of megabytes inside the 120 s wall clock, and a ~1 KB stolen token in five queries. Name resolution is done with `hostAliases` pinning db-api and results-api to their ClusterIPs in all four name forms instead. Two hard consequences: the image must ship `/etc/nsswitch.conf` with `files` before `dns` (otherwise glibc defaults to DNS-first and every lookup *hangs* through the resolver timeout budget, unrepairable under `readOnlyRootFilesystem`), and `GCE_METADATA_HOST` is pinned to a literal IP so any `google.auth.default()` probe fails fast instead of stalling.
  - **The metadata server (169.254.169.254) is NOT demonstrably covered by this policy.** No rule permits it, but link-local/node-local traffic is exactly the class already proven *exempt* from NetworkPolicy on this dataplane in the ingress direction (kubelet probes), and the egress direction has not been tested here. The load-bearing metadata defence is the node pool's `GKE_METADATA` mode with no Workload Identity binding for the sandbox KSA, not this file.
  - **Label contract, owned by this policy:** pod label `app: sandbox`, container port **8080/TCP**, Service `sandbox` (8080 → targetPort 8080). NetworkPolicy `ports` are pod ports, not Service ports. A podSelector matching no pod is not an error — it is silent no-coverage, and here it would yield a sandbox with *unrestricted* egress.
  - **Reverse direction, and why it is currently a dead path.** An egress allow-list is necessary but not sufficient: `default-deny-ingress` drops the connection at the *receiving* end, so db-api's and results-api's own ingress rules both gained an explicit `app: sandbox` entry (that is why both rows above changed). The sandbox holds neither shared secret **by design**. Since `genetics-results-suite-4h6.9` the path is opened by a *credential*, not by widening the network rule: both services now also accept a short-lived, audience-bound sandbox token minted per execution by chat-backend (see the sandbox-execution-token bullet under Authentication). Without one, db-api still 401s a request lacking `Authorization: Bearer $INTERNAL_API_SECRET`, and results-api still 401s a request carrying neither the trusted-proxy marker nor a valid bearer.
  - **`scripts/test-network-policies.py`** asserts all of the above from the repo alone, with one deliberate exception noted below (the live sandbox probe): it parses every file in `k8s/network-policies/` (union semantics — the mcp-server property cannot be read off one file), and checks that no rule selecting the sandbox admits mcp-server or the monitor, that the only pod admitted at all is chat-backend — swept over the **whole** inventory of apps the suite runs (the harness's `KNOWN_APPS` list, its pod-label table, *and* the `app` labels it finds in `k8s/deployments/`), because a sweep over the pod-label table alone silently passed a sandbox rule admitting `app: frontend`, `keycloak`, `keycloak-postgres` or `oauth2-proxy` — that no sandbox rule is `from:`-less, that the egress allow-list is exactly the two destinations with no `ipBlock` and no port 53, and that db-api and results-api admit the sandbox on the right ports. It also **discovers** the sandbox workload anywhere in `k8s/deployments/` by a *union* of independent tells — file name, object name, the pod's `app` label value, and the label contract itself — so `sandbox.yaml`, `sandbox-deployment.yaml`, a Deployment/Service split across two files, and a renamed file whose pod still carries `app: sandbox` all activate it, while a pod labelled `app: sandbox-runner` is still found by name so the contract check can *fail* on it rather than not see it (the label branch reads the `app` value only, never the stringified label dict — matching that adopted any pod carrying an unrelated key such as `sandbox-client: "true"`, which turned a working control into cascading false failures). Because no set of tells is exhaustive, two **locks** catch a workload that evades all of them: `SANDBOX_ENABLED` on with no discoverable sandbox is a contradiction (the deploy-ordering contract flips the flag in the same commit that lands the workload), and any workload in `k8s/deployments/` that carries one of the sandbox's **forced** pod-spec tells while being recognised by no discovery tell is refused until it is classified. Those tells are `runtimeClassName` set at all, a toleration for `sandbox.gke.io/runtime` (GKE taints the gVisor pool, and the sandbox is the only pod that may tolerate it), and a `serviceAccountName` that is present and is not `genetics-suite` (absent is *not* a tell — bff, frontend, keycloak, postgres and oauth2-proxy declare none). `automountServiceAccountToken: false` **was** a fourth tell and was removed in `genetics-results-suite-o5i`: auth-gateway adopted it as ordinary hardening, which made the lock abort the deploy over a pod that had merely been improved — and it is a control other workloads should adopt too, so keeping it would tax exactly the change the suite wants. Keying the lock on tells rather than on unknown `app` labels is deliberate: the tells are forced by the node pool's taint and by the credential guarantee, so nothing ordinary declares one, whereas the earlier `KNOWN_APPS` form made every added service arrive as `ERROR: network-policy checks failed; refusing to apply network-policies/` — a check that taxes routine work is deleted rather than fixed. Discovery and both locks cover every kind that carries a pod template — Deployment, StatefulSet, DaemonSet, ReplicaSet, **Job, CronJob and bare Pod** — with the CronJob's template read at `spec.jobTemplate.spec.template`, since a sandbox landing as a Job or a Pod would otherwise be invisible to discovery *and* to the lock that backstops it. It cross-checks the discovered workload against the label contract, and — once a workload exists — that **`db-api.yaml`, `results-api.yaml` and `chat-backend.yaml` all set `SANDBOX_ENABLED: "true"`**: nothing else couples the flag to the sandbox existing, and with the sandbox deployed and the flag still `"false"` the `sys.exit(1)` assertion never fires and a script that simply omits `Authorization` lands in db-api's fail-open branch. Both workload-dependent checks are inert with a printed note until a sandbox Deployment or Service lands. Since `4h6.7` landed `k8s/deployments/sandbox.yaml`, "the file exists" and "the pod runs" are no longer the same thing — deploy.sh applies it only when `ENABLE_SANDBOX=true` — so that last check (and **only** that one; every static check runs against the manifest either way) is relaxed when the sandbox is not running. **It is keyed on the cluster, not on `ENABLE_SANDBOX`.** The env var says only that *this run will not apply* the sandbox — deploy.sh **skips** `sandbox.yaml` when the gate is off, it never deletes it — so after one gate-on deploy, any later deploy from a worktree or with `terraform.tfvars` unreadable (`SKIP_TERRAFORM=true`, the documented path for exactly that) runs gate-off against a live, serving sandbox, and relaxing on the variable alone would silence the one check that stops db-api shipping `SANDBOX_ENABLED=false` underneath it — the fail-open branch where a script that omits `Authorization` is authorized with no `sub`/`sid`/`jti`. So with the gate off and a sandbox workload in the directory the harness runs `kubectl get deployments -n genetics -o name` (its **only** cluster call; everything else stays offline) and: a live sandbox Deployment → **fail**, naming the mismatch; none → relax with a note; `kubectl` present but unable to answer → **fail closed**, because the live state is then a guess; `kubectl` absent from `PATH` → relax, saying so explicitly in the note, since deploy.sh does everything through kubectl and cannot have reached the harness without it, so that case is a manual offline run by construction. `ENABLE_SANDBOX=true` is not a way around any of it — it makes the check strict rather than relaxed. Four properties of the parser are load-bearing: `policyTypes` is **inferred** when a policy omits it (the API server does the same — an `egress:` section implies Egress, and every spec implies Ingress), otherwise a policy admitting mcp-server with the field left off would be enforced by the cluster and invisible to the harness; any peer that is not `podSelector`-only is **refused rather than interpreted**, because `- namespaceSelector: {}` admits every pod in every namespace and cannot be resolved without live Namespace objects; **every** peer of a `from:`/`to:` list is evaluated rather than short-circuited on the first match, so a refused peer sitting behind a matching one (`from: [podSelector chat-backend, ipBlock 0.0.0.0/0]`) still raises instead of passing unseen; whether a selector is widened to a **superset** match or narrowed to an exact one is chosen per call site by the **polarity of the assertion**, not per function — a must-NOT-reach check (mcp-server, the monitor) widens, so a peer such as `{app: mcp-server, role: tools}` counts as reaching and the check fails closed, while a must-REACH check (db-api/results-api admitting the sandbox) narrows, because widening there would report a dead path as live once `4h6.7` makes the pod's label set fully known; and a policy selector is matched against the sandbox as a **superset** — until `4h6.7` lands a manifest the harness knows only the contract labels, not the pod's full label set, so `matchLabels: {app: sandbox, tier: untrusted}` is treated as selecting the sandbox (it would, if the pod carries `tier`) rather than skipped, which would take that policy out of every check below in silence. `scripts/deploy.sh` runs the harness immediately before `kubectl apply -f network-policies/` and **aborts the deploy on exit 1** (a broken control), warning only on exit 2 (harness could not run — no PyYAML, or a manifest directory that is missing or does not parse, which is routed to 2 rather than 1 so an unreadable file is never reported as a broken control) — this is the only place it runs, as the repo has no CI and the pre-commit hook runs only the doc-drift check (see "Documentation-drift hook" below). The **live** test — opening a connection from the mcp-server pod to the sandbox Service and confirming it fails — is deferred to the deploy window and has not been run (`genetics-results-suite-4h6.26`, which also covers the metadata-server and ClusterIP-translation questions).
- **Why auth-gateway takes an `ipBlock` and no node CIDR.** It is the only Ingress backend (both `genetics-suite` rules point at it) and the only NodePort Service, but it is fronted by a **NEG** (`cloud.google.com/neg: {"ingress":true}`, NEG `k8s1-35278419-genetics-auth-gateway-8080-ec38d214` reported HEALTHY on the Ingress). Container-native load balancing means the GFE connects straight to the pod IP, so there is no NodePort hop and nothing is SNAT'd to the node address — the intuitive "add the node CIDR for NodePort SNAT" is wrong here. Confirmed from the nginx access log: health checks *and* real user traffic both arrive from 35.191.0.0/16, kube-probe arrives from the link-local 169.254.4.6, and nothing else appears at all. Note that this is the GFE's own address, not the client's — the GFE does not preserve the client IP (an external scanner logged as 35.191.151.104), and the real client is only in `X-Forwarded-For`, so no client IP can be source-filtered at this layer. The NEG path is what keeps the source out of the node CIDR, not source preservation. `130.211.0.0/22` is Google's other documented LB/health-check range and is admitted defensively. The node subnet is `finngenie-subnet` **10.0.0.0/20** (pods 10.16.0.0/14, Services 10.20.0.0/20); it is recorded only because losing the NEG annotation would make it suddenly required, and losing the site is the failure mode.
- **chat-backend applies the same trusted-proxy marker rule** (`genetics-results-suite-th2`, was a P1 hole). `auth/core.py:get_authenticated_user` honours `X-Goog-Authenticated-User-Email` only when the request also carries the marker, and holds the asserted address to `ALLOWED_EMAILS`/`ALLOWED_EMAIL_DOMAINS`; `auth/dependencies.py:auth_required` follows results-api's precedence exactly (marker + allow-listed header → that user; marker + non-allow-listed header → 401, never a downgrade to `mcp-tool`; marker alone → `mcp-tool`; header alone → 401). `auth/core.py:is_internal_caller` is the single place the secret is compared, and it accepts the marker in either transport: `X-Internal-Auth: $INTERNAL_API_SECRET` (auth-gateway's, on the only two locations that proxy to chat-backend — `location /chat/v1/` and `location = /status`) or `Authorization: Bearer $INTERNAL_API_SECRET` (results-api's and mcp-server's, unchanged). **The two transports are equivalent for `is_internal_caller`, and neither of them is what gates sandbox dispatch**. Sandbox dispatch keys on a **second, distinct secret**, `GATEWAY_IDENTITY_SECRET` (the `gateway-identity-secret` key of `genetics-secrets`), mounted into **auth-gateway and chat-backend only** — never into mcp-server or results-api. auth-gateway sets `X-Gateway-Auth: $GATEWAY_IDENTITY_SECRET` on both chat locations behind `auth_request /oauth2/auth` (`proxy_set_header` redefines, so a caller-supplied value cannot survive the hop); `auth/core.py:is_gateway_caller` compares it constant-time; `auth/dependencies.py:gateway_asserted_identity` reduces "that secret **and** an identity header" to one boolean; and `POST /chat/v1/chat` passes it into `ToolExecutor.run_analysis`, which refuses to dispatch without it. An earlier draft gated on the marker's *transport* (`X-Internal-Auth` versus `Authorization: Bearer`) and was measurably bypassable — mcp-server and results-api hold `INTERNAL_API_SECRET` by design and can write it under any header name, so a header name is not a secret; do not reintroduce that shape. `auth_required`'s own precedence below is untouched — every other route stays reachable by any marker holder — and the property bought is narrow and stated with its bound in `docs/code-execution-security.md` §5 "Layer 2c": it closes the transitive `mcp-server → chat-backend → sandbox` path with a check on something mcp-server cannot produce, while a compromised auth-gateway, a leak of `GATEWAY_IDENTITY_SECRET` to any pod with reach to chat-backend:8000, and `REQUIRE_AUTH=false` (production sets it `true`) all still reach the dispatch. Unset or empty, the secret refuses every dispatch rather than admitting one — a dispatch-time failure, not a startup one, because it gates a single tool; `deploy.sh` refuses to apply while the key is missing, both `secretKeyRef`s are non-optional, and chat-backend logs an `ERROR` naming the variable at startup. Both compare as bytes, since `hmac.compare_digest` on `str` raises `TypeError` — a 500 — for a non-ASCII value; the presented value is re-encoded **latin-1** and the configured secret **UTF-8**, over a secret the service now refuses to start with unless it is ASCII — see `genetics-results-suite-ctq` below. `POST /chat/v1/tokens/validate` is the one route with no auth dependency; it calls the same helper and additionally refuses any request that carries an identity header at all, since its genuine callers are service-to-service and never assert one. Before this, forging the header granted admin (`ENABLE_ADMIN_PAGE=true`, membership tested against `ADMIN_USERS` on the forged string), read every user's chat transcripts, and minted a plaintext per-user API token via `POST /chat/v1/tokens` — which mcp-server and results-api both accept, so it pivoted into both. `GET /chat/v1/auth` is `@is_public` and reflects the identity, so it was an unauthenticated admin-membership oracle; it needed no change of its own because it resolves through the same `get_authenticated_user`. **mcp-server does not share the identity-header half of this bug**: it reads no identity header on any path and its ASGI gate fails closed to 401 without a `Bearer`. It did carry the `compare_digest`-on-`str` half, in its own bearer gate rather than in `auth/core.py` — see the mcp-server bullet below. Two deliberate deviations from results-api:
  - `REQUIRE_AUTH=false` (local dev only; prod sets `true`) still honours the header as-is. That mode already authenticated everyone as `anonymous`, so a marker would protect nothing and only break developing as a named user.
  - The allow-list **fails open when neither `ALLOWED_EMAILS` nor `ALLOWED_EMAIL_DOMAINS` is set**, warning as it does. chat-backend only started reading them here, and the code default is `finngen.fi`; enforcing that default on a pod that had not yet picked up the `bearer-auth-allowed` ConfigMap would lock out every user of any other deployment. `k8s/deployments/chat-backend.yaml` now `envFrom`s that ConfigMap (the same terraform-rendered values oauth2-proxy itself uses, so it cannot refuse anyone oauth2-proxy admitted), which is what makes the check live in production. The marker is the half that closes the hole; the allow-list is defence in depth against a compromised holder of `INTERNAL_API_SECRET`.
- **db-api compares the shared secret as bytes**. `api/main.py:require_auth` ran `hmac.compare_digest` on two `str`s, which raises `TypeError` for a non-ASCII value and surfaced as a **500 rather than a 401**. This was the **third of four** instances in the suite (`genetics-results-suite-fad` fixed results-api, `genetics-results-suite-th2` chat-backend, and the fourth — mcp-server's own ASGI bearer gate — is the bullet below; the zyi bead's "last known instance" wording predates finding it), and unlike the bead's own note it was **reachable, not latent**: the sandbox branch runs first but *declines* a non-ASCII bearer (it is not `alg: HS256`-shaped), which is exactly what let it fall through to the comparison — measured at HTTP 500 pre-fix, 401 after. httpx refuses to encode a non-ASCII header value, so the regression test in `tests/test_api_auth.py` sends the bearer as raw bytes, which is what a client that is not httpx puts on the wire and what starlette latin-1-decodes back into a non-ASCII `str`. `pyproject.toml` also pins `PyJWT==2.13.0` exactly (was `>=2.10`), matching the exact-pin convention of `genetics-mcp-server` (`2.11.0`) and `genetics-results-api` (`2.12.1`) on the one dependency that validates credentials. The ≥ 2.10 floor is still satisfied, and what it buys is the rejection of a **post-dated** token — 2.10 is where `iat > now` became `ImmatureSignatureError`, which is why `sandbox_auth.LEEWAY_SECONDS` exists to absorb minter/verifier clock skew. The opposite property, **anti-backdating**, does not come from PyJWT at all: PyJWT accepts an arbitrarily old `iat` as long as `exp` is in the future, and `sandbox_auth.verify_sandbox_token`'s own `iat < now - MAX_TOKEN_AGE_SECONDS` check (exact, no leeway) is what enforces it, at every PyJWT version.
- **mcp-server's ASGI bearer gate compares the shared secret as bytes** (`genetics-results-suite-zyi`, the **fourth** instance). `mcp_server.py:_wrap_with_bearer_auth._token_is_valid` ran `hmac.compare_digest` on two `str`s against every configured `MCP_API_KEY`, and `api_keys` is non-empty whenever the wrapper is installed, so a non-ASCII bearer always reached at least one comparison and raised `TypeError`. `genetics-results-suite-th2` had already converted chat-backend's `auth/core.py` to bytes; this call site was missed because it is the SSE/HTTP transport gate, not the FastAPI dependency. It is raw ASGI middleware with no exception handler above it, so the failure was a bare 500, not a 401. The same line's `.decode()` of the raw `authorization` header bytes was a second 500 on the same path — an `Authorization` value that is not valid UTF-8 (`b"Bearer \xff"`) raised `UnicodeDecodeError` before the comparison — and is now caught and treated as an absent credential. Both are pinned by tests in `tests/test_mcp_server.py` that build the ASGI scope directly with raw header bytes, since httpx refuses to encode a non-ASCII header value and a test written the obvious way fails in the client instead of the server. The same branch's `?token=` fallback carried the *identical* undecodable-bytes 500 one line further down — `scope["query_string"].decode()`, also unguarded — fixed the same way by `genetics-results-suite-tzi` and pinned by its own scope-built test. That one really was latent, unlike zyi's: nothing **sets** `MCP_ALLOW_QUERY_TOKEN` anywhere. Re-derived by grepping the whole of `genetics-mcp-server` and this repo, it appears in **seven files**, none of them a live assignment — `mcp_server.py` (three occurrences: the `os.environ.get`, the warning logged when it is on, the comment on the fallback), a **commented-out** `.env.example` line, `tests/test_mcp_server.py` (four occurrences: two `monkeypatch.setenv` calls — the `?token=` acceptance test and `tzi`'s own undecodable-query-string test — plus two docstrings), `genetics-mcp-server/docs/project-spec.md` (env-var table and branch 1), that repo's `CLAUDE.md` doc-ownership row, its `scripts/check-doc-drift.sh`, and these docs (here plus the monitor ignore-list note above). The earlier wording — "nowhere but the code, a commented-out `.env.example` line and these docs" — missed the tests and all three doc-ownership references. The deployed `mcp-server` Deployment's only `envFrom` is the three-key `bearer-auth-allowed` ConfigMap (verified against the live cluster, not just the manifests). This gate's comparison still encodes **UTF-8 on both sides**, deliberately and unlike the three below, and **no starlette is involved**: it decodes the raw scope header bytes itself, with UTF-8, so re-encoding with UTF-8 reproduces the wire bytes exactly and an undecodable value never reaches the comparison at all. Switching it would not tighten or broaden the accepted set — switching only the encode would raise `UnicodeEncodeError`, the very 500 the bytes comparison exists to prevent, and switching decode and encode together accepts exactly the same tokens (the expected side is valid UTF-8 by construction) and only moves where an undecodable value is rejected.
- **The secret must be ASCII, and now the services enforce it; the presented credential is re-encoded latin-1** (`genetics-results-suite-ctq`, all three shared-secret comparisons at once — db-api `api/main.py:require_auth`, results-api `app/core/auth.py:is_internal_caller`, chat-backend `auth/core.py:is_internal_caller`). Starlette decodes raw header bytes as **latin-1**, so re-encoding the presented credential with UTF-8 compared *mojibake* rather than the str starlette had produced: a wire bearer of `b"Bearer s\xc3\xa9cret"` arrived as `"Bearer sÃ©cret"` and re-encoded to `b"s\xc3\x83\xc2\xa9cret"`. Verified empirically against each repo's pinned starlette — 1.6.0 (db-api), 0.47.3 (results-api), 0.50.0 (mcp-server) — all three decode latin-1. The presented side is now `.encode("latin-1")`, which undoes that decode exactly; the *expected* side stays `.encode("utf-8")`. **The two codecs differ on purpose** and each site carries a comment saying so.
  - **The original justification was false and is corrected here and in all three source comments.** "Callers transmit it UTF-8-encoded" is not true of this fleet. Measured off a real socket: **node fetch/undici — which is the browser BFF — and python `requests`/`http.client` put latin-1 on the wire; `aiohttp` puts UTF-8; and httpx 0.28 (mcp-server's and chat-backend's client) refuses a non-ASCII header value outright with a client-side `UnicodeEncodeError`, so it cannot send one in any encoding.** Because the clients disagree with *each other*, byte-exactness across all callers is **unachievable** — there is no codec choice that is correct for all of them, which makes the codec question moot and the ASCII invariant the real answer.
  - **Stated plainly, because it is the honest record**: under a hypothetical non-ASCII secret this change *flips which wire bytes authenticate*. Confirmed end to end against db-api's real app: `b"Bearer s\xe9cret"` was OLD=authenticated, NEW=401; `b"Bearer s\xc3\xa9cret"` was OLD=401, NEW=authenticated. The callers that would have worked before are exactly the BFF-shaped ones that would 401 after, and the encoding the new code accepts is the aiohttp-shaped one that no client in this fleet emits. That is not a regression anywhere today only because no such secret exists.
  - **So the ASCII invariant is enforced, not documented.** The bead's own suggestion — "document that the secret MUST be ASCII and add a startup assertion" — was initially rejected as a new crash path for a configuration nobody has; the measurements above reverse that. All three services now refuse a non-ASCII `INTERNAL_API_SECRET` at startup with a message naming the variable and saying that HTTP clients disagree on encoding non-ASCII header values: results-api `app/config/common.py:require_ascii_internal_secret` (called where the variable is already read, at import), db-api `api/main.py:_require_ascii_secret` (at import, beside the existing unset warning), and chat-backend/mcp-server inside the **existing** `config.require_internal_api_secret(component)` from `genetics-results-suite-618` rather than a parallel mechanism. **The guard never fires on an absent or empty secret** — that is the dev/test configuration and several suites depend on it (mcp-server's unset case still gets 618's own message, and the paths that legitimately hold no credential, a local run and the sandbox image, never call the helper). The failure mode is deliberately the good one: a bad secret **stalls the rollout with the old pods still serving** — the new pod never passes readiness — instead of every internal call 401ing mysteriously at request time.
    - **In db-api the invariant is upheld on every request too**, since `genetics-results-suite-xi6` moved the secret out of a module global into `_internal_api_secret()`, which reads the environment and checks it in the same accessor, so the value `require_auth` compares against is always one that has been checked. **The two times fail differently on purpose**: the import-time call raises, which is the rollout-stalling fail-fast described above; the request-time check *fails closed with a 401 and never raises*, because a RuntimeError out of a FastAPI dependency would 500 every call — including one presenting the correct credential — on a pod that stays Ready, since `/health` returns before the read. A non-ASCII value can only reach the request-time read via an in-process `os.environ` mutation, which no deployment performs.
    - **That accessor is tri-state**, and the empty case is not simply fail-open: a non-empty ASCII secret means compare against it, `""` means no secret is configured and none ever was in this process (the documented fail-open dev shape), and `None` means refuse, which `require_auth` turns into a 401. **Which of the latter two an empty variable yields is decided by a one-way latch** (`_authentication_was_configured`, seeded at import from the startup read, set on any non-empty ASCII read and never cleared): a runtime environment change may **enable** authentication but can never **disable** it, so a process that has once observed a secret answers 401 — not fail-open — if the variable is later emptied or deleted.
  - Every deployed secret is ASCII (`scripts/create-secrets.sh` generates with `openssl rand -base64 32`, except `mcp-api-key` which uses `openssl rand -hex 32`; both alphabets are ASCII), where all codecs coincide, so nothing observable changes and no new crash path opens for any existing deployment. The bug originally filed was fail-**closed** (a 401 for the legitimate holder of a non-ASCII secret, never a bypass).
  - results-api needed one extra change, because it has a second producer of the string: `middleware_usage_logging._extract_user_from_header` decoded the raw header bytes itself as `utf-8, errors="ignore"` and now decodes latin-1 like starlette and like `app/middleware.py`'s sandbox peek. The justification is **not** a new 500 — the same commit's `try/except UnicodeEncodeError` in `is_internal_caller` makes that unreachable — but (i) the two producers of the string `is_internal_caller` receives must **agree**, or the usage log and the auth path answer differently about who called, and (ii) `errors="ignore"` **silently dropped undecodable bytes**, so the old path matched `b"Bearer <secret>\xff"` as internal and attributed a user while starlette's latin-1 path rejected the identical request. It is a strict tightening.
  - **The `try/except UnicodeEncodeError` asymmetry is deliberate and now commented.** Only results-api guards the re-encode, because only its `is_internal_caller` takes a `str`, so a direct caller can hand it anything; db-api's and chat-backend's take a starlette `Request` and can only see a value starlette itself latin-1-decoded, which re-encodes by construction. Both of those now carry a comment saying so and telling the next person to add the guard if a str-taking entry point appears.
  - Pinned in each repo by a test with a **non-ASCII secret**, by a test that the startup guard fires on non-ASCII and stays silent on ASCII and on absent, and — the case that actually changed — by a test built on a **hand-built ASGI scope** that pins which raw wire bytes authenticate. That last one **cannot** be written with `TestClient`: `starlette/testclient.py` does `value.encode()` (UTF-8) on httpx's already-decoded header str, so latin-1 wire bytes are silently rewritten before reaching the app and every case collapses into the UTF-8 one — a validator was briefly fooled by exactly this. Each such test carries that note.
- **`GET /api/v1/auth` on results-api is not an identity oracle** (`genetics-results-suite-r0l`, measured, no code change). The route is one of the seven `@is_public` routes above, so it answers a caller with no credential at all — the shape that made chat-backend's `GET /chat/v1/auth` an unauthenticated `ADMIN_USERS` oracle before `genetics-results-suite-th2`. Probed against the real app (full middleware stack, and again over a real socket): no headers → `{"authenticated": false, "user": null}`; **forged identity header alone → the same, the address is not reflected**; marker + allow-listed identity → that address, normalized; marker alone → `authenticated: false`, since the handler asks `get_authenticated_user`, which only ever answers about the proxied *person*, not the service. `tests/test_auth_endpoint_identity.py` pins all four offline so the property stays a test rather than a re-measurement.
- **Container privileges**: `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]` and the `RuntimeDefault` seccomp profile are set on **the containers of the suite's own services** — `results-api`, `chat-backend`, `mcp-server`, `bff`, `db-api`, and (since `genetics-results-suite-eau`) both containers of `auth-gateway` — which since `genetics-results-suite-a7n` also add `runAsNonRoot` (uid 101) and `readOnlyRootFilesystem`. Outside the sandbox, `readOnlyRootFilesystem` is set by auth-gateway's two containers and — since `genetics-results-suite-d6n` — by the `monitor` CronJob, `frontend`, `oauth2-proxy`, `keycloak`, `keycloak-postgres` and `rag-service`; the **seven** workloads that still do **not** set it are `results-api`, `chat-backend`, `mcp-server`, `bff`, `db-api`, `analyze-conversations` and `keycloak-postgres-backup`. `runAsNonRoot` is **not** exclusive to them — `db-api` and `bff` set it too (next bullet); only the read-only rootfs is. The **sandbox** is hardened further still — `runAsNonRoot`, uid 65532, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem`, a dedicated KSA, `automountServiceAccountToken: false` and `enableServiceLinks: false`, with exactly one writable path (a 512Mi `emptyDir` at `/scratch`) and no other mount of any kind (see `docs/code-execution-security.md` § 2 and "The sandbox Deployment" above). Since `genetics-results-suite-4h6.7` it **does** have a manifest, `k8s/deployments/sandbox.yaml`, so it is grepped like everything else — but it is applied only when `ENABLE_SANDBOX=true`, so its presence in this directory does not mean a hardened pod is running. Since `genetics-results-suite-d6n` closed, **every** pod-template workload under `k8s/` sets those three fields — all fifteen, so the count still running with the container defaults is now **zero**. That bead ran in two tranches: the three CronJobs first (sub-bullet below), then the third-party and support five — `frontend`, `oauth2-proxy`, `keycloak`, `keycloak-postgres` and `rag-service` (sub-bullet after it). "Every container" is now *true* for those three fields and only those three; it is still false for `readOnlyRootFilesystem`, for `runAsNonRoot`, and for the capability set (`grep -rn 'add:' k8s/` returns exactly **one** hit — `keycloak-postgres-backup`, the only workload that adds any capability back). `grep -rn allowPrivilegeEscalation k8s/` (**`k8s/`, not `k8s/deployments/`**: the backup CronJob lives in `k8s/cronjobs/`, and a `k8s/deployments/`-scoped grep is exactly how it got left off this list once already) re-derives only the **hardened** side. The non-hardened side is that set's **complement**, so it cannot be grepped for at all — derive it by listing every workload manifest in `k8s/` and subtracting the hardened ones.
  - `db-api`, `bff` and `auth-gateway` additionally run `runAsNonRoot` (uid 10001 / 1000 / 101) since they write nothing outside their image (auth-gateway writes only into two `emptyDir`s); results-api, chat-backend and mcp-server still run as root because they raise `ulimit`, shell out to `gcloud`, cache tabix indexes, or own root-owned files on the `chat-data` PVC. chat-backend sets `fsGroup: 1032` so the pre-existing SQLite files stay writable once `CAP_DAC_OVERRIDE` is dropped; the `analyze-conversations` CronJob, which is the same image writing the same files, now sets the same value for the same reason.
  - **The three CronJobs were hardened one image at a time**, deliberately not as one uniform block: `genetics-results-suite-eau` had already shown that a bare drop-`ALL` breaks an image for reasons the manifest did not anticipate, so each was run locally under the constraint before the field was written down.
    - **`monitor`** takes the full baseline and then some: `allowPrivilegeEscalation: false`, drop-`ALL`, `RuntimeDefault`, `runAsNonRoot` with uid/gid **1000**, and `readOnlyRootFilesystem`. The uid **pins what the image already does** (`scripts/monitor/Dockerfile` ends in `USER monitor`, uid 1000 gid 1000) rather than changing it, which matters because the `monitor-data` PVC's existing files were written by that uid; the pre-existing pod-level `fsGroup: 1000` is what keeps that volume writable with `CAP_DAC_OVERRIDE` gone, and is the reason the drop is safe. `readOnlyRootFilesystem` costs one `emptyDir` at `/tmp`: under `--read-only` the image has **no** usable temp directory at all (`tempfile` raises "No usable temporary directory found in ['/tmp', '/var/tmp', '/usr/tmp', '/app']`"), and while `scripts/monitor/` opens no tempfile itself once `MONITOR_DB_PATH` points at the PVC, the BigQuery client, `google.auth`'s mTLS helper and `requests.utils` in that image all import `tempfile` on paths a credential-less local run never reaches.
    - **`analyze-conversations`** takes `allowPrivilegeEscalation: false`, drop-`ALL` and `RuntimeDefault`, and **no uid change and no read-only rootfs**. The image ships no `USER` (it runs as uid 0) and the `chat-data` SQLite files are owned by uid 1031, so the write survives the capability drop only through the group — hence the `fsGroup: 1032` above; measured against the image with a `/data` owned `1031:1032` mode `2775`, `--cap-drop=ALL` alone fails every write with "attempt to write a readonly database" and the supplementary gid fixes it, WAL switch and sidecar creation included. `readOnlyRootFilesystem` is left off as **unproven rather than known-broken**: the output directory defaults onto the PVC (`Path(--db).parent / "analysis_output"`) and `--help` loads every module read-only, but the real LLM run cannot be exercised without an API key and live conversation data.
    - **`keycloak-postgres-backup`** is the one that cannot take the baseline. Its command `apt-get install`s `postgresql-client` at run time, so it needs uid 0 and a writable rootfs, and under a bare `--cap-drop=ALL` the install fails outright — apt drops to `_apt` to download and dpkg chowns what it unpacks, so the run produces `setgroups 65534 failed`, `seteuid 42 failed`, denied `SetupAPTPartialDirectory` chowns and then "no longer has a Release file" from every repository. What it does take is drop-`ALL` **plus** exactly `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID` added back, with `allowPrivilegeEscalation: false` and `RuntimeDefault` — verified installing cleanly under that set. `NET_RAW`, `NET_BIND_SERVICE`, `SYS_CHROOT`, `MKNOD`, `KILL`, `AUDIT_WRITE`, `SETFCAP` and `SETPCAP` are gone. The backup output is not what blocks the stricter fields: `pg_dump` is piped through `gzip` straight into `gsutil cp -` and nothing is staged on disk. Baking `postgresql-client` into an image is what would let this workload reach the baseline.
  - **The third-party and support five went the same way, one image at a time** (`genetics-results-suite-d6n`, tranche 2), each run locally under the candidate constraint against the real image before the field was written down.
    - **`frontend`**, **`oauth2-proxy`** and **`keycloak`** take the **full** baseline — `allowPrivilegeEscalation: false`, drop-`ALL`, `RuntimeDefault`, `runAsNonRoot` and `readOnlyRootFilesystem` — because all three images already declare a non-root `USER` (101, 65532 and 1000) and already run with `CapPrm`/`CapEff` 0 unconstrained, so the drop takes away nothing they had. `oauth2-proxy` needs **no** writable path at all, not even `/tmp`, because its session store is the cookie default; `frontend` needs two `emptyDir`s (`/var/cache/nginx` for nginx's five compiled-in temp paths, and `/tmp/nginx`, because that image's `nginx.conf` sets `pid /tmp/nginx/nginx.pid`) — and the reason is `readOnlyRootFilesystem` and nothing else: **both directories already exist in the image**, owned `101:101` `0755`, so this is the cost of the read-only rootfs rather than a fix for a path nginx fails to create; `keycloak` needs two (`/opt/keycloak/data`, which it fills with `transaction-logs` on every start, and `/tmp` for the JVM and Quarkus) with `runAsGroup` deliberately left unset because the image is uid 1000 with primary **gid 0** and its files are group-root and group-writable. Both also carry a pod `fsGroup` (101 and 1000), and it is worth being precise about why, because the manifests used to claim otherwise: an `emptyDir` root is created **0777 root:root**, so a non-root uid can write one with no `fsGroup` at all — measured, for both images. `fsGroup` buys determinism, not access, which is exactly the reason `sandbox.yaml` gives for its own. Verified end to end: the frontend serves `/`, an SPA deep link and hashed assets with the CSP intact; oauth2-proxy answers `/ping`, `/ready`, an unauthenticated `/oauth2/auth` (401), `/oauth2/start` (302 to the provider) and `/oauth2/sign_out`; Keycloak imports its realm against a real Postgres and answers `/health/ready`, `/health/live`, the realm's `.well-known/openid-configuration` and the admin console.
    - **`keycloak-postgres`** takes the **full** baseline, `readOnlyRootFilesystem` included, and runs as **uid 70** rather than root. That is not a new identity: the image's entrypoint is root only long enough to `chown` `PGDATA` before `exec gosu postgres`, so the live server is already uid 70 with no capabilities, and that the PVC's files really are uid 70 is *entailed* — Postgres refuses to start on a data directory it does not own. Starting as 70 is what makes drop-`ALL` free: as root, a bare `--cap-drop=ALL` dies at `chmod: /var/lib/postgresql/data/pgdata: Operation not permitted` and would need `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID` and `SETUID` back. It is the one workload here with a pod-level **`fsGroup: 70`** (plus `fsGroupChangePolicy: OnRootMismatch`, as the sandbox uses), and it has exactly **one** justification: letting a **fresh** PVC bootstrap at all — `docker_create_db_directories()` does an unguarded `mkdir -p "$PGDATA"` and `$PGDATA` is a *subdirectory* of the volume root, which uid 70 cannot create in a root-owned root. It is **not** what makes the single `emptyDir` at `/var/run/postgresql` (the unix socket and its lock file) writable — an emptyDir is created 0777 and uid 70 writes it either way; `OnRootMismatch` is likewise a cost optimisation, not a correctness requirement. Applying it to the **existing** PVC was the open question, since the kubelet takes `PGDATA` from `70:0 0700` to `70:70 2770` and Postgres rejects any data directory that is not `u=rwx` or `u=rwx,g=rx`; it was reproduced against `postgres:16-alpine` rather than reasoned about — a PGDATA initialised through the normal root+`gosu` path, re-grouped exactly as the kubelet does, starts and serves under `--user 70:70 --cap-drop=ALL --security-opt no-new-privileges --read-only`, because the entrypoint's `chmod 00700 "$PGDATA"` runs *before* Postgres's permission check and leaves the directory back at 0700. `OnRootMismatch` then keeps the recursive walk over a realm-sized database to the first start only. That first pass is a **one-way change to the volume's contents** — subdirectories under PGDATA are left `70:70 2770` and pre-existing files `0660`, and only the top-level directory is ever restored — which is harmless here (gid 70 is postgres, RWO volume, single process) and does not drift, since new files are still created `0600` under `data_directory_mode = 0700`, but should be known before the first apply rather than after. Rollback to the pre-change manifest shape was verified against a re-grouped volume.
    - **`rag-service`** takes `allowPrivilegeEscalation: false`, drop-`ALL`, `RuntimeDefault` and `readOnlyRootFilesystem` with one `emptyDir` at `/tmp`, and **no uid change** — same call as `analyze-conversations`: the image ships no `USER`, runs as uid 0, and writes the `rag-stores` PVC whose live ownership cannot be seen from outside the cluster. Under the read-only constraint the FastMCP/uvicorn server starts, `/health` and an MCP `initialize` both answer, every module under `src/` imports, and the OpenAI/Cohere/Anthropic clients construct; the untested part is a real retrieval, which needs a populated store and live API keys. Nothing in that path should want the rootfs — embeddings and reranking are remote HTTP and the image carries no local embedding model to cache under `$HOME`.
  - **auth-gateway runs its whole pod as uid 101**, which is what removes the capability exception rather than justifying it. `nginx:1.27-alpine`'s master normally starts as root and its build defaults to `--user=nginx` (`nginx -V` → `--user=nginx --group=nginx`), which the mounted config does not override, so a root master chowns `/var/cache/nginx/*_temp` to uid 101 and `setgid`/`setuid`s each worker to it — the reason `genetics-results-suite-eau` had to add `CHOWN`, `SETUID`, `SETGID` back on top of drop-ALL. A pod-level `runAsUser/runAsGroup: 101` with `runAsNonRoot: true` and `fsGroup: 101` does none of those things: nginx skips the chown entirely when it is not root, and there is no privileged master to setuid from. All three capabilities are gone from **both** containers, which now drop `ALL` and add nothing. `NET_BIND_SERVICE` remains unnecessary for the same reason as before: the only `listen` is 8080, and neither fragment `deploy.sh` injects (`${LEGACY_REDIRECT}`, `${KEYCLOAK_SERVER}`) adds a listener.
    - It costs two `emptyDir`s. `/var/cache/nginx` is `root:root 0755` in the image with none of the `*_temp` directories created, so uid 101 dies at `mkdir("/var/cache/nginx/client_temp") failed (13: Permission denied)` without one; it is mounted into the initContainer too, because `nginx -t` is not a dry run and creates those same paths (as 101, chowning nothing). It is deliberately **not** `medium: Memory` — a 50M request body or a buffered upstream response would then be charged against the container's 128Mi memory limit. `/tmp` is the second, needed because both containers now also set `readOnlyRootFilesystem: true`; the config already puts the pid file at `/tmp/nginx.pid`, and `/var/run` turns out **not** to be needed at all.
    - **`proxy_max_temp_file_size` is bounded at `64m`, and neither emptyDir has a `sizeLimit`** (`genetics-results-suite-o5i`, revisited by `genetics-results-suite-3zi`; the full arithmetic is in the manifest so it is not rediscovered). nginx's default is `1024m` **per in-flight request** on every buffered location — `proxy_buffering off` appears only on `= /mcp` and `/chat/v1/` — which made `proxy_temp` the dominant term in this pod's disk exposure at ~1 TiB. It is set at **http level**, for the same reason `client_max_body_size` is: it must cover every buffered location, present and future. That is a **behaviour** change rather than a limit, so it was measured against the real rendered config with one slow client (~5 MB/s) pulling a **100 MB** response through `/api/`: unset → HTTP 200, all 104 857 600 bytes, sha256 match, **95.6 MiB** peak on disk; `64m` → identical response, **64.0 MiB** peak; `0` → identical response, **4 KiB** peak. Past the ceiling nginx stops extending the temp file and falls back to **client-paced streaming** — it does not truncate and does not 502. Nor does it 504: with `proxy_read_timeout` scaled **60× down to 5s** and the same 100 MB / ~5 MB/s client the response is still 200, all bytes, sha256 match, clean error log — nginx does not arm the upstream read timeout while the pipe is blocked on a full temp file. `nginx -t` accepts the directive down to `32k`, so `64m` clears the floor by 2048×. `64m` rather than `0` because buffering is what frees the upstream worker early, and `/api/` reaches results-api through the BFF with 300s timeouts. The remaining ceiling is `1024 × 50 MiB` of `client_temp` (**50 GiB**) plus `1024 × 64 MiB` of `proxy_temp` (**64 GiB**) = **114 GiB** against ~43.83 GiB allocatable ephemeral storage (47 060 071 478 B, re-measured 2026-08-29) — down from ~24× the disk to ~2.6×, but **still not closed**, because the half the bound does not touch is the reachable one. A 50 MB POST to `/oauth2/` with **no credential** spools the full 50.0 MiB into `client_temp` (measured). The same POST to `/api/` spools **zero** — but *only* with no `Authorization` header at all, where `auth_request` denies at the access phase and nginx answers 302 without reading the body; add **any** bearer prefix and it spools 50.0 MiB again (measured: `Authorization: Bearer not-a-real-token` → 200, 50.0 MiB), because `/api/`'s `if ($http_authorization ~* "^Bearer ")` runs in the **rewrite** phase, ahead of `auth_request`, and the gateway does **not** validate the token — `@api_bearer` proxies straight to results-api, so the body is on disk before any upstream can reject it. Machine-derived from the rendered config, **9 of the 13** locations carry no `auth_request /oauth2/auth`: `= /healthz` and `@oauth2_login` (a `return`, no body read), `= /oauth2/auth` (internal, `proxy_pass_request_body off`), and the seven that spool — `/oauth2/`, `= /mcp`, **`@api_bearer`**, both `.well-known` ones, and the injected Keycloak login location where enabled. The four with `auth_request` are `/api/`, `= /status`, `/chat/v1/` and `/`. Re-derive this list rather than trusting it: `@api_bearer` was omitted once already, and it is the member that makes `/api/` anonymously reachable. What did become derivable is the **per-connection** worst case, ~64 MiB after the bound against ~1 GiB before, so a number is now constructible as *p99 concurrent buffered requests × 64 MiB × safety factor*; the missing input is the concurrency figure, which needs the gateway's own access log. Until it exists a guess on the internet-facing `replicas: 1` pod trades an attacker-reachable disk-fill for an attacker-reachable **eviction** ~10× cheaper to trigger, and both end in the same whole-site outage. For `tmp` there is nothing to bound at all: `/tmp/nginx.pid` (~6 bytes) and one ~9 KB placeholder config on the initContainer's error path. **No `ephemeral-storage` request or limit either, and the two halves are decided separately.** A *limit* is the **worse** instrument, not the better one — it evicts exactly as a `sizeLimit` does but also counts the container's stdout, and the config `access_log`s every request to `/dev/stdout`, so it would be crossed by traffic **volume** rather than by the temp files it is meant to bound. The absent *request* is the **correct** setting: `DiskPressure` eviction ranks by usage **above request**, so naming one would move this pod *down* the ranking and protect the pod that filled the disk. **The "single node" premise recorded here previously is false**: measured 2026-08-30 for the two clusters this checkout can reach — `finngenie-staging` runs two general nodes plus a gVisor sandbox node, `finngenie` (daly production) runs two, and the finngen production cluster in `phewas-development` is unreachable from here so no count is claimed for it. The weakened form is what holds — on staging, 8 of the 11 pods running in the namespace, this one included, share one general node, so `DiskPressure` there reaches nearly everything, but an evicted pod **can** reschedule onto the other node.
    - **The image stays a tag, with the tested digest recorded in the manifest**. `readOnlyRootFilesystem` made the moving tag newly load-bearing: `docker-entrypoint.sh` runs under `set -e`, so a future `1.27-alpine` push adding a `/docker-entrypoint.d` script without a read-only guard **crash-loops the internet-facing gateway** rather than writing a stray file (all four current scripts were read out of the image and are safe). It is not pinned because nothing in this repo bumps image references — no Renovate, no Dependabot, no `.github/` at all — and every other image in `k8s/` is a tag, so a pin here would freeze an internet-facing HTTP parser at a fixed build for good. Note the tag buys less than it appears to: with the default `IfNotPresent`, a node keeps whatever it already pulled, so the tag is re-resolved only when the pod lands on a node without the image. `nginx@sha256:65645c7b…2f2a10` (built 2025-04-16) is what both `a7n`'s harness and `o5i`'s tested, and `kubectl get pod` confirms it is what the live gateway is running — so recording it changes nothing at the next rollout and gives the next reader something to diff the entrypoint against.
    - Measured end to end against `nginx:1.27-alpine` under `--user 101:101 --cap-drop ALL --security-opt no-new-privileges --read-only`, driving the **real rendered config** (ConfigMap → `deploy.sh`'s envsubst with both fragments populated → the initContainer's envsubst) with all eight cluster-local names stubbed: master **and** worker both `Uid 101`, `CapPrm`/`CapEff`/`CapBnd` all `0000000000000000`, `NoNewPrivs 1`. Traffic: `/healthz` 200, `/` authorized 200 and unauthenticated 302 to `/oauth2/start`, the `Authorization: Bearer` → `@api_bearer` bypass 200, `/api/` → BFF 200, `/status` and `/chat/v1/` 200 with the rendered `X-Internal-Auth` intact, the `${KEYCLOAK_SERVER}` `/auth/` fragment 200, the `${LEGACY_REDIRECT}` 301, a 20 MB POST (forcing a `client_body_temp` write) 200 on both the auth_request and Bearer paths, and a 20 MB response to a rate-limited client 200 with `proxy_temp/3/00` created by the worker. Error log clean.
  - **What the auth-gateway hardening buys, measured — and what it does not.** The nginx *worker*, the process that parses internet traffic, was **already** fully unprivileged before any of this: `setuid(101)` without `PR_SET_KEEPCAPS` clears the capability sets, so the pre-`eau` pod with an empty `securityContext` showed Uid 101, CapPrm 0, CapEff 0. `eau` improved only the **master**, which never touches attacker input, from CapEff `0xa80425fb` (the 14-cap runtime default) to `0xc1` with NoNewPrivs 0 → 1 — real, but it left the master uid 0 on a writable rootfs, so the gateway was not meaningfully harder to exploit, only less useful once exploited. `a7n` is the step that changes the landing privileges themselves: the master is now uid 101 with an empty capability bounding set and an unwritable rootfs, so an nginx RCE lands with no capability to reclaim through `execve` and no way to persist into the image layer. **The mounted credential went in the follow-up**: the pod now sets `automountServiceAccountToken: false`, so no ServiceAccount token is projected into the internet-facing pod at all. **How much that is worth, measured rather than assumed** — the honest answer is *defence-in-depth, not the closing of a live escalation path*. No RoleBinding or ClusterRoleBinding in the cluster names `system:serviceaccount:genetics:default` (checked over all 151 bindings), and the SA carries no `iam.gke.io/gcp-service-account` annotation, so it reaches GCP not at all. Its entire authorisation is what every authenticated identity inherits — `system:basic-user`, `system:discovery`, `system:public-info-viewer`, `system:service-account-issuer-discovery`: self-reviews, `/version`, `/healthz`, the OpenAPI/discovery documents, the OIDC JWKS. So what the change denies an attacker today is a valid cluster credential for reconnaissance, and what it denies tomorrow is the blast radius of any binding someone later grants the `default` SA — which is the failure mode worth pre-empting, because such a binding would arrive without anyone rechecking this pod. Verified functionally inert by re-running `a7n`'s traffic matrix against the rendered config with the token directory absent: identical status codes on all twelve cases.
    - **Now done for the other five too**. `bff`, `frontend`, `keycloak`, `oauth2-proxy` and `postgres` set neither field and got the `default` token; all five now set `automountServiceAccountToken: false`, so **no** pod in the namespace mounts it. Same honest framing as auth-gateway above — reconnaissance-grade denial plus the blast radius of a future binding, not a live escalation path. Each was checked for API-server use before the field was set, from the images and config rather than from vendor docs: the BFF and frontend images carry no Kubernetes client (frontend is nginx over a static bundle); the Keycloak image ships **no** `jgroups-kubernetes` jar and `conf/cache-ispn.xml` names no transport stack, so KUBE_PING discovery is unavailable, not merely unconfigured; the `oauth2-proxy` v7.14.3 binary links only `k8s.io/apimachinery`'s `util/sets` and `util/errors` helpers — no client-go, no in-cluster config — and its session store is left at the cookie default; `postgres:16-alpine` carries no Patroni/stolon-style HA agent. The eight that set `serviceAccountName: genetics-suite` automount that KSA's token instead (`genetics-suite` has no RBAC binding either — its value is GCP-side through Workload Identity, and that path runs through the metadata server, not the mounted token), so they are the remaining place a `default`-style grant could land.
    - **This is why `automountServiceAccountToken: false` is no longer a sandbox tell** in `scripts/test-network-policies.py`. It was one of that harness's four forced tells; auth-gateway setting it made the lock fire (`refusing to apply network-policies/`) on a pod that had merely been hardened, which is the exact shape of check that gets deleted instead of fixed. Removed from `sandbox_tells()`; the two gVisor tells, which the node pool's taint genuinely forces, are untouched.
  - **The rollout is low-risk by construction.** auth-gateway is `replicas: 1` with no `strategy` block, so the default RollingUpdate gives `maxUnavailable = floor(0.25 × 1) = 0` and `maxSurge = 1`: the new pod must reach Ready before the old one is deleted. A CrashLoop or a failing `render-config` initContainer therefore leaves the **current** gateway serving rather than causing an outage.
- Workload Identity provides read-only GCP access (BigQuery + GCS) without key files
- HTTPS enforced via FrontendConfig redirect
- All services output structured JSON logs, captured by GKE fluentbit and sent to Cloud Logging

## Operational procedures

**Choosing the deployment:** every entry-point script (`deploy.sh`, `create-secrets.sh`, `build.sh`, `build-all.sh`, `rollout.sh`) resolves its target through `scripts/lib/env.sh`. `DEPLOY_ENV=<name>` selects `terraform/terraform.tfvars.<name>` (passed with `-var-file`), `terraform/<name>.tfbackend` and `.env.<name>`; `REGISTRY` defaults to that environment's own Artifact Registry repo. Known names: `daly`, `daly-staging`, `finngen`. Four guardrails exist because a mis-selection deploys across environments: `.env.<name>` **never** falls back to a bare `.env` (that would push one deployment's secrets into another's cluster), a bare `terraform/terraform.tfvars` is **refused** while `DEPLOY_ENV` is set (terraform auto-loads it *in addition to* `-var-file`, so variables the per-environment file omits would silently come from it); an inherited `REGISTRY` that disagrees with the selected environment is **refused** (`unset REGISTRY`, or `REGISTRY_FORCE=1`) rather than allowed to push one deployment's images over another's `:latest` tags; and `rollout.sh` **and `create-secrets.sh` refuse a kubectl context that is not the one the selected deployment's mandatory `kube_context` tfvars key names** (overridable only with each script's own `--context` flag, below). **All four live in `lib/env.sh`** — the fourth was lifted there out of `rollout.sh` by `genetics-results-suite-mrg` precisely so `create-secrets.sh` could *call* it rather than carry a second copy that could drift from it. But *living* in `lib/env.sh` is not the same as applying everywhere: an entry point gets a guardrail only by **calling** the function that holds it, so the guardrails hold per call site rather than per file. Re-derived from the five call sites:

| entry point | calls | guardrails in force |
|---|---|---|
| `deploy.sh` | `resolve_deploy_env`, `load_deploy_env`, `resolve_registry` | 1, 2, 3 |
| `build.sh` | same three | 1, 2, 3 |
| `build-all.sh` | same three | 1, 2, 3 |
| `rollout.sh` | `resolve_deploy_env`, `resolve_registry`, `require_kube_context` | 2, 3, 4 |
| `create-secrets.sh` | `resolve_deploy_env`, `load_deploy_env`, `require_kube_context` | 1, 2, 4 |

Guardrail 1 lives inside `load_deploy_env`, which `rollout.sh` never calls — vacuous for it, since it sources no `.env` at all. Guardrail 3 lives inside `resolve_registry`, which `create-secrets.sh` never calls — vacuous for it too, since it pushes no images. **`deploy.sh` is the one of these five entry points with no context guard and no need of one**: `deploy.sh:99`'s `eval "$(terraform output -raw kubectl_command)"` is the only line in `scripts/` that *sets* a context at all, and it runs on both branches (`SKIP_TERRAFORM=true` included) after `terraform init -backend-config` against the resolved backend, so the cluster it acts on cannot disagree with the resolved state backend. It is **not** the only unguarded cluster mutator in `scripts/`, and the difference is that the other three do not resolve a deployment at all: `keycloak-register-client.sh`, `keycloak-bind-allowlist.sh` and `keycloak-register-brainzzz.sh` read the Keycloak **admin password** out of `keycloak-secrets` on whatever context is ambient and then `kubectl exec … kcadm.sh` in the pod to create/rotate OIDC clients, bind the allow-list authenticator and set realm attributes. They source no `lib/env.sh`, read no `DEPLOY_ENV` and do not so much as echo the context, so `require_kube_context` has no tfvars to compare against and cannot simply be called from them; giving them a guard is its own change and is tracked separately. (They were missed by the earlier derivation because it grepped for `apply|create|set|patch|delete|scale|rollout` and not `exec`.) With `DEPLOY_ENV` unset the original single-deployment behaviour applies. See `docs/environments.md`.

**Important:** `deploy.sh` does NOT build images. To ship new service code you must build first, then deploy. The typical workflow is:

1. `./scripts/build-all.sh` (or `./scripts/build.sh <service>` for one service) — builds and pushes new `:latest` images to Artifact Registry
2. `./scripts/deploy.sh` (or `./scripts/rollout.sh <service>` for one service) — applies manifests and force-restarts pods so they pull the freshly-built `:latest` images

If you only run `deploy.sh` without building, the rollout restart will re-pull whatever `:latest` currently points to in the registry (i.e. the last build), so no code changes from upstream service repos will be picked up.

- **Full deploy**: `./scripts/deploy.sh` — runs terraform apply, configures kubectl, deploys all k8s manifests; derives the container registry from the terraform `registry` output (overridable via `REGISTRY` env var, which must agree with `DEPLOY_ENV` unless `REGISTRY_FORCE=1`) and substitutes it in k8s manifests at deploy time; `CONFIG_PROFILE` (terraform variable, default `daly`) selects the data profile for results-api (`daly` or `finngen`); creates a `datasets-config` ConfigMap from `configs/datasets.yaml` and mounts it into results-api and db-api pods at `/app/configs/datasets.yaml` (env var `DATASETS_CONFIG_PATH`); rag-service is skipped by default (set `ENABLE_RAG=true` to include it); after applying manifests, force-restarts all app deployments so pods pick up `:latest` images and ConfigMap changes (subPath mounts don't propagate; oauth2-proxy doesn't hot-reload). Does **not** build images — run `build-all.sh` or `build.sh` first if you need new code.
- **Branding (product name)**: the displayed product name is configurable per deployment via the `app_name` terraform variable in `terraform.tfvars` (single source of truth; default `FinnGenie`, e.g. `GeneGenie` for the daly profile). Resolution order everywhere is **`APP_NAME` env override → `app_name` in `terraform.tfvars` → `FinnGenie`**. `deploy.sh` reads it from terraform output and injects `APP_NAME` into the chat-backend pod (used by the MCP server's assistant persona in `default_system_prompt`). The frontend bakes it in at build time: `build.sh`/`build-all.sh` resolve `APP_NAME` (via `tfvar app_name` from `scripts/lib/env.sh`, reading the tfvars `DEPLOY_ENV` selected) and pass `--build-arg APP_NAME` → Dockerfile writes `VITE_APP_NAME` into `.env` → `import.meta.env.VITE_APP_NAME` (read via `src/config/appName.ts`). So setting `app_name` once in the deployment's tfvars covers both the frontend build and the backend deploy. Logos and the `finngen.fi` CORS/domain identifiers are unchanged.
### Manifest-render preflight (`scripts/test-manifest-render.py`)

`deploy.sh` does not substitute *fields*; it pipes each file in `k8s/configs/`,
`k8s/deployments/` and `k8s/cronjobs/` through `envsubst '<whitelist>'` **in full** and applies
the result. So a whitelisted name spelled `${...}` anywhere in a manifest is substituted,
comments included — and two of the values are multi-line nginx fragments built by `printf -v`
(`LEGACY_REDIRECT` from `redirect_from_host`/`redirect_to_host`, `KEYCLOAK_SERVER` when the
broker is enabled), so an expansion inside a `#` line breaks out of the comment and the render
stops being valid YAML, failing `kubectl apply` partway through a deploy.

That is not hypothetical: two comment regions of `k8s/deployments/auth-gateway.yaml` spelled
both names literally for a long time. It stayed invisible because
**nobody had ever driven that file through `deploy.sh` with either fragment populated** — on the
deployed profiles both are empty and the render is well-formed. It is not `daly`-only either:
`LEGACY_REDIRECT` comes from `redirect_from_host`/`redirect_to_host`, which have no profile
coupling. The fix was a rewording, held in place only by an invariant comment in the file;
`genetics-results-suite-puv` is the mechanical half.

The harness (`scripts/test-manifest-render.py`, exit 0 pass / 1 broken / 2 could-not-run, the
same three-way answer `test-network-policies.py` gives) checks **every** manifest `deploy.sh`
renders, not just `auth-gateway.yaml` — the whole-document `envsubst` is the same in all three
loops, so auth-gateway is where the hazard was first hit, not where it can only occur. For each
file it:

- **derives** the whitelist by parsing `deploy.sh`'s own `envsubst '...' < "$f"` invocations,
  including the `[ "${base}" = "sandbox.yaml" ]` branch that narrows `sandbox.yaml` to its own
  three names, and derives the multi-line values by parsing the `printf -v` lines. Nothing is
  re-typed: a hand-kept copy of a whitelist is a second list to rot, and it would rot
  *silently*, since a name missing from the copy simply stops being checked;
- **renders** it exactly as `deploy.sh` does (same whitelist, same `:latest` → `:${TAG}` `sed`)
  with **both** multi-line fragments populated, and requires the result to parse as YAML and to
  hold the same number and kinds of documents the file declares. Deliberately *not* the same
  resource *names*: `${APP_NAME}` is whitelisted and already spelled inside a name
  (`k8s/deployments/chat-backend.yaml`), so a parameterised `metadata.name` is a legitimate
  manifest whose name is supposed to change under the render, and failing it would abort every
  deploy with what reads like a corruption report. Names that hold no placeholder are still
  compared, so content moving between documents is still caught;
- **refuses** a whitelisted, multi-line-valued name spelled `${...}` in a comment — the
  invariant `auth-gateway.yaml`'s header states, mechanised. This one is deliberately narrower
  than "any `${...}` in a comment": `${INTERNAL_API_SECRET}` is *supposed* to sit in that file's
  comments (it is not whitelisted, because the render-config initContainer substitutes it from a
  Secret later and baking a Secret into a ConfigMap is what its absence prevents), and
  `${KEYCLOAK_HOST}` in `k8s/deployments/keycloak.yaml`'s header comment is whitelisted but
  single-line and expands harmlessly. A check that flagged either would be deleted rather than
  obeyed;
- **asserts** that every `$NAME`/`${NAME}` the file spells that is *not* in its whitelist
  survives the render verbatim — which covers both later-substituted secrets and nginx's own
  `$host`, `$remote_addr`, `$scheme`, `$request_uri` without naming any of them;
- **holds each whitelist to the files it governs, in both directions.** A whitelisted name no
  governed file spells substitutes nothing, so it goes stale in silence — `${DOMAIN}` sat in the
  deployments whitelist while appearing in zero files under `k8s/`, and this check is what found
  it. The other way round, a `${...}` a file spells that its own loop's whitelist omits reaches
  the cluster as literal text; since the whitelist a file gets is decided by the directory it
  sits in, this is where a manifest copied between `k8s/deployments/` and `k8s/cronjobs/` is
  caught. Only braced spellings count (nginx's runtime variables are bare), and a name the file
  itself defines — a container `env` entry, a shell assignment in an embedded script, or an
  in-manifest `envsubst` such as `auth-gateway.yaml`'s render-config initContainer — is excused
  unless some *other* whitelist substitutes it, which is what stops an `env` entry from
  laundering a genuine placeholder. Both sides are derived; nothing here is a list.

What it does **not** prove: a multi-line fragment expanded into a *scalar* position — an
annotation value, say — still parses as valid YAML, so the render check passes it. The guard
covers the comment form (mechanically) and structural breakage (by parsing the render); it is
not a diff of every rendered document body against the original.

Because nothing is re-typed, the rot moves from a stale whitelist to a stale parser — and a
parser that matches only *part* of `deploy.sh` is the dangerous case, not one that matches none
of it: every file it stops accounting for is checked against an empty whitelist, passes
vacuously, and is counted in a reassuring summary line. So the harness cross-checks what it
parsed (globs, number of `envsubst` calls, every `printf -v`) against a deliberately loose
survey of the same script, and treats any disagreement — or an empty whitelist for a file under
a rendered glob, or an `envsubst '...'` call in the script that the coverage check does not
govern — as could-not-run. That last one is why the coverage check reaches the `keycloak/`
template renders as well as the three manifest loops: the number of whitelists is counted from
the script rather than known, so a render the parser cannot follow refuses instead of leaving a
whitelist quietly unchecked. Wrapping the deployments `envsubst` over two lines,
renaming the loop variable, or rebuilding a fragment with a double-quoted `printf -v` all warn
instead of passing.

It runs **first** in `deploy.sh`, before `terraform apply` and before kubectl is configured: it
needs no cluster, no credentials and no terraform output, so there is no reason to pay for those
before finding out a manifest cannot be applied. Exit 1 aborts; exit 2 (no PyYAML, no `envsubst`,
a rendered directory that no longer exists, or any of the parser drift above) warns and proceeds,
because "cannot tell" is not "broken". An *empty* rendered directory is not drift — `deploy.sh`
tolerates an empty `k8s/cronjobs/` by design (`[ -e "$f" ] || continue`), so the harness does
too, and only a missing directory is exit 2. The repo has no CI and the pre-commit hook only
runs the doc-drift check, so this is the only place it runs.

### Sibling repos: resolution, and the one command that runs their gates

`scripts/lib/siblings.py` is the shared answer to "where is repo X checked out". It exists
because four scripts here answered that privately and none generally — two share
`SUITE_SIBLING_ROOT` but cover different repo sets, two resolve only genetics-mcp-server
through `MCP_SERVER_DIR`, and none can find a repo checked out under a different root from
the rest. Its docstring holds the resolution order; the short version is per-repo
`SUITE_REPO_<NAME>` override, then `SUITE_SIBLING_ROOT`, then the parent of the **main**
checkout, then that parent's own siblings. An auto-discovered candidate is accepted if its
`origin` names the repo, or — for a checkout with no origin at all — on its directory name
plus `rev-parse --is-inside-work-tree`. So a same-named *plain* directory cannot become the
answer, but a same-named originless git checkout can. A `SUITE_REPO_<NAME>` override skips
the origin test entirely (a fork's origin names something else, and that is what the
override is for) but must still be a git checkout; when it is not, that is an error naming
the path and the reason, never the "not checked out on this machine" message.
The four existing resolvers are deliberately **not** retrofitted onto it yet.

The repos it knows about:

<!-- BEGIN GENERATED: suite-repos -->

- `genetics-results-suite`
- `genetics-results-api`
- `genetics-results-db`
- `genetics-results-browser`
- `genetics-mcp-server`
- `genetics-results-munge`

<!-- END GENERATED: suite-repos -->

`scripts/check-siblings.sh` runs each sibling's own test lane. It is a trigger, not a new
lane: there is no CI anywhere in the suite, so those lanes run only when somebody remembers
to. Lanes are discovered from each checkout (a `tests/` directory gets pytest, a
`package.json` gets whichever of its `test`/`bff:test`/`typecheck` scripts exist) rather
than listed here, and nothing else in a sibling is executed.

It is **not** restricted to offline tests. A repo that declares an `offline` pytest marker
is run with `-m offline`; a repo that declares none runs its default lane, whatever that
includes — network, credentials and all. The runtime and flakiness of an unrestricted lane
are that repo's to fix, by declaring a marker there rather than filtering here.

**When it stays silent, it never exits 0.** A repo that is not checked out, a missing
`.venv` or `node_modules`, no discoverable lane, or pytest exiting 2/3/4/5 are all
could-not-run: the lane never got as far as reporting on the code. Failures and errors are
kept apart from that and from each other — any `N failed` in the summary is a failure
(exit 1) however many errors accompany it, and setup/collection errors with zero failures
are their own outcome (exit 3), reported as what was observed rather than as a diagnosed
cause. The suite's own gates are not run here — `build-all.sh` already runs them.

### Duplication baseline (`scripts/check-duplication.py`)

Counts the one-fact-in-N-copies shape across all six repos, split intra-repo versus
cross-repo and weighted by lockstep commits rather than by lines. The detector names
nothing: one holding a list of known copies would be another copy to maintain, so every
group is found by structure (equal or near-equal function bodies and module-level constant
expressions, and the same set of four or more strings written out as a literal in two
files). It is a unit-level detector — a duplicated block that is neither a body nor a
literal set is outside its reach, TypeScript entirely so.

What it ratchets is **undeclared** duplication, and the report always shows the split
rather than one smaller number — a count that fell for an unstated reason is the failure
this gate exists to prevent. A member **ignored by a tracked `.gitignore` of its own repo
and byte-identical to a file committed in another** is generated, and no list of them
exists anywhere because git is asked — which members were struck on a given run is in
`--json` as each group's `generated_files`, and is not written down here. The ignore has to
come from a file the clone carries: a rule in `.git/info/exclude` or in the user's global
excludes file is not accepted, or the same file would be netted out on one machine and
counted on another with nothing in review able to see it. It fails in the right direction:
the day a consumer commits its copy, `git check-ignore` stops covering it and it is counted
again. A group whose remaining files fall inside one entry of **`configs/twins.yaml`** is
declared. Netting is per member, and a struck member leaves the declared row as well as the
undeclared ones, so no file is counted in two rows; a group can lose its generated edges and
stay in the count for the hand-maintained pair underneath — which is what the synced copies
do to the suite-internal copy in `configs/datasets-schema-example.yaml`, cross-repo groups
that were really intra-repo groups all along.

`configs/twins.yaml` names the sites of each deliberate duplicate, the property that must
hold between them, and why they are two things; `merge: never` marks the ones with
counter-evidence against merging. It is itself a hand-maintained list — the shape this gate
measures — and netting an entry out is exactly how a real finding would be silenced, so:
`reason` is mandatory and an entry without one, carrying an unreadable field, or naming a
site that no longer exists is **exit 2** rather than a quieter count; an entry may pin
parity to named symbols per site, which is how the auth allow-list matcher is declared
without declaring the neighbouring function whose fail-open preamble must not be; and the
**declared count is ratcheted alongside the undeclared ones**, so adding an entry takes a
`--write-baseline --reason` — non-empty is all the code enforces, and naming the entry in it
is the convention. The generated count is not ratcheted — it moves when a generator gains a
consumer, which costs nobody anything. What that argument leaves open is written down at the
site in `check-duplication.py`: untracking a hand-maintained duplicate takes it out of the
ratchet with no reason recorded at all, and whole-file byte-identity is what bounds the
exposure rather than eliminating it.

`docs/duplication-baseline.json` is a **dated snapshot**, not a live claim, and carries the
date and commit it was taken at. `--check` ratchets today's counts against it and is wired
into `build-all.sh` warn-only, because the counts are taken over sibling checkouts the
build does not control. A missing checkout is exit 2, not a pass: it lowers every count.

Everything it counts is found by parsing, and a parser that has fallen behind on part of a
tree is the one failure that would look exactly like success — the sites it dropped are
reported by nobody while the totals still print. Catching that by finding the same
duplicates a second way would take a second parser, so the baseline records **coverage**
instead: per repo and file extension, how many files were read, how many the owning pass
parsed, how many yielded a unit or an enumeration, and how many `git ls-files` says are
tracked. `--check` pairs each number against the one above it in the chain tracked → read →
parsed → units and refuses when the lower one has fallen *further* than the one above it —
a real deletion moves both by the same amount. The tracked count is never compared on its
own, only as a pairing partner, and a cell whose baseline has none (git could not answer on
the machine that recorded it) skips that one pairing rather than reading as zero. Parsed
dropping further than read is a file that stopped parsing; units dropping further than
parsed is an extractor that stopped matching; read dropping further than the tracked count
is discovery that stopped reaching files still on disk — the last is why the tracked count
comes from git and not from the same walk. A baseline cell with no counterpart in the
current run at all is drift too, since an extension leaving the scan takes both sides of
every pairing with it. Each is **exit 2**, not 1: a detector that has stopped seeing a site has not
measured growth, it has stopped being able to measure. The census measures the detector,
not the trees, so it says nothing about which duplicates exist and passes a suite where
every copy has been consolidated away. What it does not cover: a construct no extractor
ever matched is not a drop from anything; for `.sh`/`.bash` the parse check has no failure
mode, so parsed equals read by construction and shell drift rests on the units rule alone;
and TypeScript, which no pass reads, has no census at all.

### There is no development environment — and the BigQuery rehearsal dataset

Established read-only on 2026-08-13 and worth stating
plainly because the project's name argues the opposite: **`phewas-development` IS
production.** In that project: one GKE cluster and one kubeconfig context
(`gke_phewas-development_europe-west1-b_finngenie`), no companion
`phewas-production`, one application namespace (`genetics`), and **four** BigQuery
datasets — `genetics_api_logs`, `genetics_chat_logs`, `genetics_results`, and since
2026-08-14 `genetics_dev`, which `--tree worktree` points
db-api at. It was created as a **chr22-only subset** and has been a **persistent
full-size copy of `genetics_results`** only since the 2026-08-18 widening, which took it
from 0.57 GB to all **755,813,602 rows / 136.69 GB**. It is not a clone and shares no
blocks with production: the widening was a `TRUNCATE` + `INSERT … SELECT` reload, so it
is a storage line of its own (`docs/bigquery-dev-dataset.md`, "Why this exists";
`docs/local-dev-vm.md`, "The dev dataset"). So the results data *does* have a standing
dev copy; the two log datasets do not, and neither does the cluster. The survey line
this paragraph used to carry — "exactly three … with no dev copy of any of them" — is
the enumeration that rotted. The live results-api carries `DEPLOY_ENV=prod` and
`LOG_SOURCE=finngenie_prod`. The `daly` profile is a **second production brand** with its
own project, region, domain and real users, not a staging copy, so it is not a canary.
(`log_source` `genetics-results-api-dev1` in the monitoring section is historical; nothing
answers to it now.)

The `daly-staging` deployment added later does **not** change this for `phewas-development`.
It is a second GKE cluster inside the **daly** project (`docs/environments.md`), so it is a
rehearsal ground for the daly brand's manifests and images — not for this project, and not
for BigQuery, whose datasets it does not duplicate.

**It does change the suite-wide framing, and this section used to state it globally**
(`genetics-results-suite-8vn`, corrected 2026-08-30). There are **three clusters in two
projects**, and **two of them are production**: `finngenie` in `daly-finngenie` (daly
brand, real Broad users) alongside `finngenie` in `phewas-development`. Any statement of
the form "there is only one cluster and one project" is now false, and acting on it is
dangerous in one specific way — it invites treating the daly `finngenie` cluster as
non-production and therefore safe to mutate. It is production.

Two further facts, measured 2026-08-30 from db-api's and results-api's environments on the
two clusters this admin instance can reach:

- **`daly-staging` is not data-isolated from `daly` production.** Both clusters' db-api
  runs with `PROJECT_ID=daly-finngenie` and `DATASET_ID=genetics_results` — one dataset,
  two clusters. Staging rehearses manifests and images; it rehearses nothing about
  BigQuery. Filed as `genetics-results-suite-zaw` (deferred).
- **`DEPLOY_ENV` does not discriminate them at runtime.** results-api reports
  `DEPLOY_ENV=prod` and `CONFIG_PROFILE=daly` on *both* daly clusters; the only
  distinguishing environment value is `LOG_SOURCE` (`finngenie_prod` vs `staging_prod`).
  Do not use `DEPLOY_ENV` read off a running pod to tell staging from production.

`phewas-development` was **not** re-measured and cannot be from this checkout — it has no
kubeconfig context here and `gcloud container clusters list --project phewas-development`
returns 403. Its node count, cluster state and dataset contents are therefore unverifiable
from this repo. The one-cluster / one-context / one-namespace facts above rest on that
2026-08-13 survey; the **fourth dataset** does not — the
`genetics_dev` creation date, the chr22 origin and the row/byte figures come from the
later work recorded in `docs/bigquery-dev-dataset.md` and `docs/local-dev-vm.md`
(created 2026-08-14, measured after the 2026-08-18 widening).

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
optimiser has processed it (measured: 4,492,232,401 B dry-run against 517,406,337 B
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
- **`SANDBOX_URL` is set explicitly to `http://127.0.0.1:8081`**, what
  `scripts/run-sandbox-local.sh` publishes. The client has **no default** — it raises
  `SandboxNotConfigured` when the variable is unset, because
  the default it used to carry was `127.0.0.1:8080`, which locally is db-api rather than the
  sandbox. In the cluster the value comes from `k8s/deployments/chat-backend.yaml`.
- **It provisions the sandbox's per-execution credentials**:
  `SANDBOX_TOKEN_SIGNING_KEY` and `INTERNAL_API_SECRET` are generated once into
  `DEV_STACK_RUN_DIR` — stable across restarts, outside every repo, never in a working tree —
  and exported to the services it starts unconditionally, whatever `SANDBOX_ENABLED` is set to.
  Without them db-api and results-api resolve **no sandbox principal at all** and serve the
  sandbox SDK with no per-execution accounting, which is locally indistinguishable from the bug
  the tokens exist to fix. That hazard belongs to the two
  credentials and to nothing else: both verifiers raise `SANDBOX_TOKEN_SIGNING_KEY is not set`
  before they look at a token (`app/core/sandbox_token.py`, `api/sandbox_auth.py`), so `admit()`
  is never reached and the execution map stays empty, and with `INTERNAL_API_SECRET` empty
  db-api's auth middleware is fail-open on top of that. Neither verifier consults
  `SANDBOX_ENABLED` — it is read only by the startup guard `require_sandbox_config`, which is
  inert while it is false, and by results-api's collapse of its anonymous surface. Override any
  of the three to pin a value.
- **`SANDBOX_ENABLED` defaults to `false`**, which is a
  separate fact about what the stack can back rather than about the credentials above: the
  script starts no sandbox supervisor, so a `true` default would offer `run_analysis` with
  nothing behind it and its failure would misreport as a transient `SandboxUnavailable`. Set it
  to `true` only after starting a supervisor with `scripts/run-sandbox-local.sh`; the script's
  `up` command then probes `SANDBOX_URL/health` once and warns if nothing answers
  `"status": "ok"` there.
- **`status` reads `DATASET_ID` from `/proc/<pid>/environ`**, because `/health` does not
  report it and an unset `DATASET_ID` means production — `api/main.py` defaults it to
  `genetics_results`. It prints that case as `PRODUCTION` rather than as a blank.
- **`--tree worktree` defaults db-api to `genetics_dev`**, the persistent full-size copy
 : all 15 tables and views, and since 2026-08-18 every
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
 . `HOST_PORT` overrides it. It publishes on `127.0.0.1` only.
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
  rather than missing coverage, and anyone tuning the memory or /scratch budgets must read it:
  local `/scratch` is a tmpfs, i.e. page cache in the container's **own memory cgroup**, so
  its bytes come out of the same 3 GiB as the child's RSS (measured 113 MiB → 414 MiB after a
  300 MiB write), whereas the pod's `emptyDir` has no `medium: Memory`, is node-disk-backed
  and is charged to `ephemeral-storage` instead — never to `limits.memory`. Headroom sized
  locally is therefore up to 512 MiB more conservative than the pod needs.
  `docs/code-execution-security.md` → "As built" enumerates each and what it costs;
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
rule uses it — the `docs/keycloak-apple-signin.md`
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
like a full build, so it warns exactly like one.

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
| `genetics-mcp-server` | **editable install** — `.venv/…/_editable_impl_genetics_mcp_server.pth` points at ONE tree's `src/` | in a fresh worktree `uv run pytest` falls through to the pyenv shim, whose interpreter has the MAIN checkout installed; worktree tests then run main-checkout source and report green | `pytest_configure` aborts unless `genetics_mcp_server.__file__` is under the pytest rootdir |
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

By contrast with the two rollouts above, the sandbox credential
has **no code ordering hazard**: nothing sent an HS256 bearer until the sandbox and the client
that calls the minter both existed, so minter-first and validators-first are both safe, and every
existing credential type is untouched in either single-sided state.

| state | chat-backend mints | db-api / results-api verify | result |
|---|---|---|---|
| neither shipped | no | no | current behaviour |
| **validators only** | no | yes | **safe** — no caller produces an HS256 bearer, the branch never fires |
| **minter only** | yes | no | **safe today** — nothing calls the minter yet; a token sent to an old validator would 401, never authorize |
| both shipped | yes | yes | the sandbox path works once the sandbox exists |

The ordering that *is* load-bearing is the **secret**: chat-backend, db-api and results-api all
mount `sandbox-token-signing-key`, so `create-secrets.sh` must run before the manifests are
applied or the pods sit in `CreateContainerConfigError`. `deploy.sh` now verifies that key,
`internal-api-secret` and `gateway-identity-secret` are non-empty before applying anything, and
on a miss offers both a targeted `kubectl patch secret genetics-secrets --type=merge` for
**that one key** and a re-run of `create-secrets.sh`. That re-run used to be the trap — the script reused-or-generated only a
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
`SANDBOX_ENABLED=true` with either secret missing — both verifiers `sys.exit(1)` by design, so
that crash-loops db-api and results-api. **Three** manifests ship `SANDBOX_ENABLED: "false"` —
`db-api.yaml`, `results-api.yaml` and `chat-backend.yaml`; the
deploy that creates the sandbox Deployment flips all three, after `create-secrets.sh`.

**What that deploy does with the sandbox itself**, now that `k8s/deployments/sandbox.yaml`
carries `args:` and `deploy.sh` will apply it: with
`ENABLE_SANDBOX=true` the preflight checks the gVisor node, the container-level `args:` and the
sandbox image's presence in Artifact Registry *before the first apply*; the manifest loop
resolves the db-api and results-api ClusterIPs from the live cluster into `hostAliases` and
applies the file; and `sandbox` is appended **last** to the `kubectl rollout restart` and
`rollout status` lists — last deliberately, because `strategy: Recreate` on one pinned node makes
that restart a brief outage of code execution and a restart mid-execution kills the running
script. With the gate off the file is skipped, never deleted, which is why the network-policy
harness probes the cluster rather than trusting `ENABLE_SANDBOX`. **Rollback:
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

`genetics-results-suite-4h6.84` adds a **second** secret on the same two locations,
`X-Gateway-Auth: $GATEWAY_IDENTITY_SECRET` (`gateway-identity-secret` in `genetics-secrets`,
held by auth-gateway and chat-backend only), and it has the same benign leading state for the
same reason: an old chat-backend ignores the header, and a new chat-backend that leads the
gateway refuses **only code execution** (`run_analysis` returns `SandboxNotConfigured`) while
chat, history, downloads and tokens keep working. Because both Deployments mount the key
non-optionally, `create-secrets.sh` — or the targeted `kubectl patch` `deploy.sh` prints — must
run before either manifest is applied; `deploy.sh` refuses to apply at all while the key is
absent or empty, which is what keeps that ordering from being discovered at pod-start.

### SDK empty-result contract

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

The four SDK functions outside the `range_response` family — `gene_burden`,
`gene_annotations`, `gene_disease` and `search`, which compute their JSON rather than
streaming a TSV — were left degrading to a bare `pl.DataFrame()` by `6uk` and are covered by
`genetics-results-suite-8a1`. Having no file header to read, each **declares** its columns,
and because a hand-maintained list per router is exactly the thing that rots, no declaration
is trusted:

- Where the rows are projected in results-api, the declaration **is** the object the
  projection uses — the `.select()` list for gene annotations, the row builder for gene-group
  members, the profile's `output_columns` for gene-disease — so it cannot drift from the rows.
- `verified_columns_header` (`app/core/responses.py`) **refuses rather than degrading**:
  a non-empty response compares its declaration against `rows[0]` — names and order — and
  raises before any byte is written. The four direct callers check that first row only; the
  search path loops every row, because a search result array is heterogeneous by
  construction. Search is why this is on the
  response path and not only in a test — its result dicts are assembled from a live index,
  so no offline fixture can produce a real one; its declaration is additionally checked
  against the dict literals in its own module source.
- `gene_burden(gene=...)` needed no declaration at all: it serves TSV, whose header line
  `tabix -h` prints even for a locus with no hits, and the SDK was simply dropping it —
  `ToolExecutor` now takes those names from the reader it already built.
  `gene_burden(phenotype=...)` reads a file, so it advertises that file's real header via
  `json_phenotype_with_header`.
- `gene_disease` expresses "no associations" as a **404** that the SDK reads as an empty
  frame, so its header rides on that 404.

`tests/test_declared_columns.py` (results-api) mutation-proves the refusal: a projection that
stops using the declaration, a row builder that grows a key, a search item or ranking splice
that grows a key, and a declaration that over-claims are each caught. The refusal compares
`rows[0]` for the four direct callers, whose rows come from one projection, and every row on
the search path, whose result array is heterogeneous by construction.

**`6uk`'s coverage claim was too wide, and re-deriving it turned up three more.**
`credible_sets(phenotype=...)`, `credible_sets(phenotype=..., leads_only=True)` and
`exome(phenotype=...)` are `json_phenotype` callers, not `range_response` callers, so they
were sending no header while being documented as covered. They read a file, so they now
advertise its real header line (`json_phenotype_with_header` /
`lead_variants_phenotype_with_header`) and declare nothing. Re-derived from the routers,
what still hands the SDK a nameless empty frame is `search(rsids=...)` (`/rsid/variants`)
and `ld()` (a different service); `credible_sets(cs_id=...)` 404s rather than returning an
empty array.

The test that hid this is fixed too: `test_sdk_empty_result_schema.py`'s fake transport
attached `X-Columns` to *every* response, so it passed against a server that advertised for
none. It now attaches the header only for paths in an explicit `_ADVERTISING_PATHS` list
re-derived from results-api, which is what makes an uncovered endpoint fail the suite.

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
into a ConfigMap, so `auth-gateway.yaml` keeps literal `${INTERNAL_API_SECRET}` and
`${GATEWAY_IDENTITY_SECRET}` placeholders in the ConfigMap — `deploy.sh`'s `envsubst` whitelist
deliberately omits both names, so they survive verbatim — and a `render-config` initContainer
substitutes them from `genetics-secrets` into a `medium: Memory` emptyDir that nginx mounts as
`/etc/nginx/nginx.conf`. It substitutes those two names only, which is what keeps `envsubst`
away from nginx's own `$host`/`$email`/`$request_uri`; the same explicit list is repeated on the
placeholder re-render used to tell "nginx rejected the secret" from "nginx rejected the
template", so the two runs render the same document. The initContainer also refuses to start
when **either** value is empty or holds a character nginx would reinterpret inside a header
value (`A-Za-z0-9+/=_.-` only) — an empty `gateway-identity-secret` would render a header nginx
drops, leaving chat working with code execution silently refused at every dispatch.

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

- **Single service update**: `./scripts/rollout.sh [--context <ctx>] <service> [tag]` — updates one deployment image (`REGISTRY` defaults to the selected environment's repo; `tag` defaults to `latest`). **The cluster acted on can no longer disagree with `DEPLOY_ENV`**: before it touches the cluster, the script reads the mandatory `kube_context` key out of the resolved tfvars and refuses if `kubectl config current-context` is not that exact string — and refuses too unless the token `kube_context` occurs **exactly once in the whole file** on a column-0 `kube_context = "..."` line, in a file containing no `/*` anywhere. Nothing is derived and nothing falls back, so a checkout whose tfvars lacks the key cannot roll out until it is added. The three cluster-contacting calls then run pinned to that verified context (`kubectl --context "${ACTING_CONTEXT}"`, the guard's `readonly` freeze of its own verdict), so neither a `kubectl config use-context` from another terminal nor any shell sourced after the guard can retarget them between the check and the call. The implementation is **not** `rollout.sh`'s own: `genetics-results-suite-mrg` lifted it into `scripts/lib/env.sh` (`read_kube_context` plus `require_kube_context`) so `create-secrets.sh` calls the same code, and `rollout.sh` keeps only the call and the wording it passes in. See "The cluster context guard" below. Known services, re-derived from `IMAGE_MAP`: `frontend`, `bff`, `results-api`, `chat-backend`, `mcp-server`, `db-api`, `rag-service`, `sandbox`, `keycloak` (9). It only swaps container images, so ConfigMap-driven pods (auth-gateway) and the CronJobs need `deploy.sh`. A service whose Deployment does not exist on the current context gets a "Not deployed" message and exit 1 instead of a bare `kubectl` `NotFound` — the ordinary case for `sandbox` and for `keycloak`, both of which `deploy.sh` applies only when their gate is on — while a query that *failed* (no context, expired credentials, unreachable API server) is reported as "could not ask", with kubectl's own error kept, rather than as a missing service. **`monitor` is the one deliberate absence**, and for the only reason that discriminates: it is a CronJob, so `kubectl set image deployment/monitor` cannot address it. `keycloak` used to be excluded alongside it on the grounds that it is "built from *this* repo's working tree rather than from a cloned sibling" — which is equally true of `sandbox` and `monitor`, so it never discriminated; it is a Deployment named `keycloak` whose container is named `keycloak` and whose image is `${REGISTRY}/keycloak:latest`, so it is now in the map.
  - **`rollout.sh sandbox` is not an ordinary rollout**. The sandbox Deployment is `strategy: Recreate` with `terminationGracePeriodSeconds: 130`, so the restart **kills an in-flight execution** and leaves no sandbox for up to ~130 s before the replacement is even scheduled; chat-backend surfaces that as a tool error, not a wrong answer. The script's `kubectl rollout status --timeout=300s` tolerates it — 300 s exceeds the 130 s grace period with room for the supervisor's prewarm and the readiness probe's 5 s delay + 10 s period.
  - **The cluster context guard** (`genetics-results-suite-b1r`; lifted into `scripts/lib/env.sh` and given to `create-secrets.sh` too by `genetics-results-suite-mrg`, so there is **one** implementation and not two that can drift — `require_kube_context` takes the caller's own wording for its messages, because a generic "this script" refusal is worst at the moment someone is one paste away from production). `deploy.sh` does not need one: it *overwrites* the context from `terraform output -raw kubectl_command` before its first apply, so the acting cluster cannot disagree with the resolved state backend. `rollout.sh` used to have no equivalent — it resolved `REGISTRY` from `DEPLOY_ENV` and then acted on whatever context happened to be current, merely **echoing** it. That mattered because there are three deployments in two projects, **two of them production**; both production clusters are named `finngenie` so only the project tells them apart, one of those projects is called `phewas-development`, and the daly production context differs from staging's by a trailing `-staging` alone. An echoed string one line above a mutation does not survive that.
    - **The deployment STATES its cluster; nothing is derived.** The expected context is one mandatory column-0 key in the resolved tfvars — `kube_context = "gke_daly-finngenie_us-central1-a_finngenie-staging"` — read verbatim and compared with `kubectl config current-context`. An earlier version *derived* `gke_<project_id>_<zone>_<cluster_name>` from three tfvars keys, falling back to `terraform/variables.tf`'s defaults for the two that have them. Three rounds of blind validation each broke that reader in a **new** way — a legitimately indented top-level attribute read as absent, a column-0 line inside a heredoc body or a `/* */` block comment read as real, a legal `cluster_name = join("-", [...])` read as empty — and every break degraded in the **same direction**, because a key that reads as absent falls back to `zone = europe-west1-b` and `cluster_name = finngenie`, and those name a **production** cluster in *both* projects. The inference was the defect, not the parser: text-matching over HCL cannot be made certain, and its failures all pointed at production. `terraform output` was never an option either — it needs state access and credentials, turning a fast local command into a slow one that fails for unrelated reasons.
    - **The reader does not model HCL, and that is the second half of the lesson.** It used to: an `awk` state machine tracked `/* */` block comments and heredoc bodies so a copy of the key inside one could be refused rather than mistaken for the real attribute. A **fourth** round of blind validation broke that machinery in both directions — a line closing one block comment and opening another set no flag, so the next comment body was read as live code and a genuine attribute written after a `*/` was skipped without an error (terraform read *staging*, the guard read *production* and did not refuse); and, the other way, `note = "glob pattern /* here"` or `note = "x" # heredocs look like <<EOT` set the flags from inside a string and falsely refused a good key below. So the modelling is gone. The rule now needs no understanding of the language: the token `kube_context` must occur **exactly once in the whole file** — comments and strings included — and that one line must match `^kube_context[ \t]*=[ \t]*"[^"]*"[ \t]*(#.*)?$`. A second, equally stateless precondition runs **first**: a file containing `/*` anywhere is refused outright. That is the one comment form that could smuggle a value past *both* other checks — a lone key inside a `/* */` block presents as one occurrence on a column-0 line in the strict form and was **accepted**, which widens the guard from "refuse always" (the count would otherwise be zero) to "act on the cluster that commented-out line names", and commenting a cluster line out while switching targets is exactly what operators do. `#` and `//` commented-out keys never needed this: the token still counts and the line fails the column-0 form. The cost is a rare false refusal — a `/*` inside a string, or a genuine block comment elsewhere — fixed by one edit the message names. It runs before the count because on a file with both faults "delete the other mentions" could send the operator to delete the live key and keep the commented one. Anything else refuses, naming the file, the line and the count. The reader is ~20 lines of `grep` plus one `[[ =~ ]]`, and it is deliberately **stricter than HCL**: a file that mentions the key in a comment *and* sets it is refused, which is one obvious edit that the message names. In exchange no construct — heredoc, block comment, string, CRLF, nesting — can make it return a value the operator did not write on that one line. Refusing is the cheap outcome: one line in a tfvars, against a production cluster. `lib/env.sh`'s `tfvar()` is **not** used by the reader and remains **unchanged**, now as a settled decision rather than a deferral — it is `grep -E "^[[:space:]]*<key>[[:space:]]*=" | head -1`, which matches an indented key and silently picks the first of a duplicate. `genetics-results-suite-mrg` revisited it when the guard moved into the same file and left it alone: `tfvar()` is shared with `deploy.sh`, `build.sh`, `build-all.sh` and `create-secrets.sh`, where the same shadowed-key defect misdirects the **registry** and the **config profile** rather than the cluster, so hardening it is a fail-closed change to four scripts at once and wants its own validation. Both readers now sit in `lib/env.sh` with the strict one immediately below the loose one, and the comment at `tfvar()` says which is which and why. The weakness is recorded at the helper.
    - **The key is mandatory, the tfvars are gitignored, and that combination is deliberate.** `.gitignore` ignores `terraform.tfvars.*` and un-ignores only `terraform.tfvars.example`, so `kube_context` does **not** arrive with a clone or a pull: `rollout.sh` **refuses in any checkout that has not added the line**, the production checkout included, until someone pastes it in from `kubectl config get-contexts -o name`. That is the intended fail-closed behaviour rather than a regression — the alternative is a script guessing between two identically named production clusters. `terraform/variables.tf` declares `variable "kube_context"` (type `string`, default `""`, description saying rollout.sh consumes it and no resource does) purely so terraform stays quiet: measured on Terraform **v1.14.8**, a key in a `-var-file` that the root module does not declare is a **warning** ("Value for undeclared variable"), exit 0, not an error — so declaring it is correctness of hygiene, not of function. `terraform.tfvars.example` carries the key with a comment explaining it.
    - **It refuses; it does not switch.** `deploy.sh` may retarget the shell because it owns the whole run. Silently retargeting the shell of a low-ceremony single-service tool is its own hazard, so this one stops and prints the `kubectl config use-context` to run.
    - **The override is a flag, not an environment variable, and that is the fix rather than the style.** It was `ROLLOUT_CONTEXT`, cross-checked against the current context but never against the expected one — so an `export ROLLOUT_CONTEXT=<prod>` typed for one deliberate production rollout outlived that invocation and re-authorised itself on every later run from the same shell. Driven: `DEPLOY_ENV=daly-staging ./scripts/rollout.sh bff`, still on the production cluster, was accepted and pushed the **staging registry's image onto the production cluster** — worse than either half alone. `--context <ctx>` cannot be exported and is not inherited, so it is per-invocation by construction. It still must name the context kubectl is actually on; and when it *also* differs from what `DEPLOY_ENV` expects, the acceptance message says so loudly rather than silently, because that is the deliberate off-target case. `ROLLOUT_CONTEXT` is read nowhere, so a stale export of it now does nothing.
    - **The refusal does not hand over a route to the wrong cluster.** Both refusals used to print a complete, runnable override command with the wrong cluster already filled in — a one-line copy-paste onto production for someone who was accidentally on it. Now the *correct* path is the paste-ready one (`kubectl config use-context <expected>` on a mismatch; the `kube_context = "..."` line to add when the key is missing, with an explicit instruction to check the name first), and the override is named and pointed at the README rather than spelled out as a runnable command containing the context the operator is being warned away from.
    - **The verdict is frozen, so nothing sourced afterwards can re-aim the pinned calls.** `require_kube_context` ends by doing `readonly ACTING_CONTEXT="${CURRENT_CONTEXT}"`, and *that* is what both callers pin (`kubectl --context "${ACTING_CONTEXT}"`). Without it the pin carried a plain global that `create-secrets.sh`'s own `load_deploy_env` could overwrite: the guard correctly runs **first**, but `set -a; . .env.<env>; set +a` is arbitrary shell, and a one-line `CURRENT_CONTEXT=<production context>` in that file redirected all three Secret writes to a cluster nothing had checked while the success line above stayed *truthful* about what was checked — driven, and silent, since nothing downstream reprints the context. It also nullified the TOCTOU pinning that is the pin's entire purpose. Running the guard before the file is read is therefore necessary but **not sufficient**: ordering protects the guard's *inputs*, the freeze protects its *output*. Bash refuses both reassignment and `unset` on a readonly global, so the attempt now aborts the script with a `readonly variable` error instead of quietly succeeding. The freeze lives in `lib/env.sh` rather than in `create-secrets.sh` because a property both callers depend on should not depend on each caller remembering it; `rollout.sh` never sources `.env` at all, and its observable output is unchanged. Re-invoking the guard in one shell is defined rather than left to trip the `readonly`: the same context re-verifies as a no-op, a different one refuses, since calls already made cannot be re-aimed.
    - **Five hazards are recorded rather than guarded, one is deliberately half-closed, and one is half-caught by a re-check.** (Re-derived from the code comments, not incremented: `NAMESPACE` at both call sites, and `.env.<env>`, the name-vs-endpoint/`KUBECONFIG`, `ROOT_DIR` and `BASH_ENV` blocks in `lib/env.sh`.) `NAMESPACE` is taken from the environment, the guard does not cover it, and — unlike the context — it is **not** frozen either, so a stale `export NAMESPACE` or a `NAMESPACE=` line in `.env.<env>` still acts on a different namespace of the right cluster; that split is intentional and stated at both call sites in those words (right cluster guaranteed, right namespace on trust), because `.env` is the deployment's own config file and setting a namespace there is legitimate, while every deployment's tfvars sets `namespace = "genetics"` so there is nothing to disagree about. The other four re-point the guard's evidence, or re-interpret its verdict, from outside the file. `ROOT_DIR` is honoured from the environment (`lib/env.sh`), so an inherited value relocates the tfvars the guard reads, leaving it green while describing another checkout's cluster. `KUBECONFIG` is honoured from the environment by kubectl itself, so an inherited export selects a different kubeconfig entirely, in which the expected context *name* may be bound to any `cluster.server` at all — the guard compares names, not endpoints. `BASH_ENV` is sourced by bash before line 1 of a non-interactive script, so a `kubectl()` shell function defined there answers the guard and every pinned call after it while the real binary is never run. And **`.env.<env>` is itself a `BASH_ENV`-class vector** — the one this list previously omitted, even though the ordering and the freeze both exist because of it: those two close `.env` rewriting the guard's *inputs* (`TFVARS=`, `OVERRIDE_CONTEXT=`) and its *output* (`CURRENT_CONTEXT=`), and neither touches `.env` changing what the frozen verdict **means**. A `kubectl()` function, a `PATH=` line or a `KUBECONFIG=` line each lets the pin expand faithfully and then be reinterpreted, sending every Secret write to another cluster behind a green line that is truthful about what was checked. All three were driven. It differs from `BASH_ENV` in reachability, not mechanism: `BASH_ENV` needs someone in the operator's environment, whereas `.env.<env>` is an ordinary config file the operator edits on purpose. **`ROOT_DIR`, `CURRENT_CONTEXT`-via-`.env` and all three `.env` reinterpretation vectors were driven** during `genetics-results-suite-mrg`'s validation; `CURRENT_CONTEXT` is fixed by the freeze above, and two of the three `.env` vectors are now caught by `create-secrets.sh`'s **re-assert** (below). An earlier revision dismissed the name-vs-endpoint hazard as a hand-edited kubeconfig, "not reachable as configured" — that was wrong, since `KUBECONFIG` needs no edit to any file and is exactly as reachable as `ROOT_DIR`. `BASH_ENV` is not defensible from inside the script it subverts and is deliberately left unchecked. What `ROOT_DIR` and `KUBECONFIG` get instead is visibility: the guard's **accepting** path prints both environment-supplied inputs (`Context: <ctx> (env: <env>, kube_context in <tfvars>, kubeconfig <path>)`), so a guard reading another tree's evidence, or resolving the name out of another kubeconfig, no longer produces a success line indistinguishable from a correct run — the refusals and the OFF-TARGET banner already named the tfvars. **That claim needs one correction, and it is the third overclaiming hazard note in this file.** The printed kubeconfig covers the **inherited export** only, because the value is in scope when the line prints; it does *not* cover a `KUBECONFIG=` line in `.env.<env>`, which takes effect *after* the line is printed at `lib/env.sh`'s accepting path and before the writes at `create-secrets.sh`. So for that vector the printed path is accurate about what the **guard** read and obsolete about what the **writes** use, and the re-assert rather than the printed line is what catches it. The pattern is the warning — `ROOT_DIR` was called "not reachable as configured" and was then driven, the same words were used of `KUBECONFIG` and were equally wrong, and this line claimed a mitigation that does not reach the vector it sat next to — so a hazard comment asserting a bound is a claim to be tested, and should name both the vector it covers and the one it does not. All five are commented where they live (four in `lib/env.sh` with the guard, the `NAMESPACE` one at *both* call sites), per `CLAUDE.md`'s "a latent hazard belongs at the site".
    - **The threat model is ACCIDENT, not ATTACK, and it is now stated in the code.** `.env.<env>` holds the operator's own API keys and is written by the same person who runs these scripts; it is gitignored because it is *secret*, not because it is *hostile*. `create-secrets.sh` sources it with `set -a; . file; set +a`, and sourcing arbitrary shell in your own process ends the argument — anything in that file can redefine any command, rewrite `PATH`, or replace the script's own functions, so a check written inside a script is worth nothing against it, exactly as with `BASH_ENV`. Every "refuses", "cannot" and "closed" above therefore means *bounds a mistake an operator can realistically make*. The mistakes are ranked, and the ranking is the point: a stray `PATH=` line in a deployment `.env` is genuinely plausible, a `KUBECONFIG=` line is possible, and a `kubectl()` shell function is not something anyone writes by accident. The statement lives at `require_kube_context` in `lib/env.sh`, because the code around it implied a stronger model than it can deliver.
- **Build all images**: `./scripts/build-all.sh` — clones the service repos and builds/pushes all Docker images to Artifact Registry, including the local `monitor`, `keycloak` and `sandbox` build contexts (those build from this repo's working tree and so have no branch). Per-service branches come from `FRONTEND_BRANCH`, `RESULTS_API_BRANCH`, `MCP_SERVER_BRANCH`, `DB_API_BRANCH`, `RAG_SERVICE_BRANCH` — all default `master` except rag-service (`deploy_jk`), and they are set per deployment in `.env.<DEPLOY_ENV>` (daly-staging sets them all to `staging`). `REGISTRY` defaults to the selected environment's repo
- **Build single image**: `./scripts/build.sh <service>` — clones, builds, and pushes one service's image (same `DEPLOY_ENV`, `REGISTRY` and branch env vars as build-all.sh). `sandbox` is also accepted: it builds the local `sandbox/` context rather than a clone, but still clones genetics-mcp-server for the SDK.
- **Build sandbox image**: included in `./scripts/build-all.sh`; builds `sandbox/` as the `sandbox` image. It stages genetics-mcp-server's `src/` and `pyproject.toml` into `sandbox/.sdk-src/` (gitignored, removed on exit) and pip-installs the SDK `--no-deps` — the SDK is never vendored into this repo. The installed package is then pruned to the SDK's import closure (`sandbox/prune_venv.py`), and pip/setuptools are removed from the venv, before the final stage copies it. **The sandbox is skipped, loudly, when the genetics-mcp-server branch has no `src/genetics_mcp_server/sdk/`** (`master` does not today; `genetics-results-suite-4h6.11` has landed only on `worktree-db-only-architecture`). That skip keeps a suite build green **only where the sandbox is not being deployed**: `build-all.sh` restates the skip as its last line instead of printing "All images built and pushed.", and **exits 1 when this deployment's tfvars sets `sandbox_pool_enabled = true`** (the same derivation `deploy.sh` uses for `ENABLE_SANDBOX`, `ENABLE_SANDBOX` in the environment still winning) — so on exactly the staging bring-up that turns the sandbox on, an unshippable sandbox fails the build rather than being carried past it. See "How the sandbox is turned on" above. `./scripts/build.sh sandbox` fails hard in the same situation instead of skipping. Both scripts first run `./scripts/gen-sandbox-docs.py` **with an explicit `--sdk-src` pointing at the copy just staged**, which regenerates `sandbox/schema/*.md` (one file per view in `configs/datasets.yaml`, plus an index) and `sandbox/stubs/*.pyi` (signature stubs read out of the staged SDK source with `ast`) — the Dockerfile copies them verbatim to `/genetics/schema` and `/genetics/sdk`. Those files are **committed and regenerated**: committed so the directories are never empty and a `datasets.yaml` change shows up in review, regenerated so the image cannot document a schema older than the canonical file. `./scripts/test-sandbox-docs.py` runs next in both scripts and gates the image: it asserts the committed copies match a fresh generation, that every view, column, enumerable column and worked example reaches a file, that every documented column carries a well-formed BigQuery type from `tables.<view>.column_types` and that a column missing one is **refused** rather than rendered with a blank type cell, that the stubs cover **exactly** the SDK's exported surface (plus the four lifecycle helpers the generator adds), and that the correctness rules live in `datasets.yaml` rather than in the generator. Exit 1 = a property broke, 2 = the harness could not run because no SDK source could be found — it never skips silently. Neither script defaults to `sandbox/.sdk-src` any more: that copy exists only after an *interrupted* build, so the old default was reachable only when stale and would silently regenerate the shipped stubs from an old SDK. Run by hand with no `--sdk-src`, both resolve `GENETICS_SDK_SRC`, then `MCP_SERVER_DIR`, then the live sibling genetics-mcp-server checkout (worktree-matching one first, each gated on `src/genetics_mcp_server/sdk`, the same resolution `run-sandbox-local.sh` uses), print the source they chose, and report a leftover staged copy rather than reading it. `build.sh sandbox` fails hard on either; `build-all.sh` folds both into the existing skip branch. Worked example SQL in `datasets.yaml` names views **bare** (`FROM credible_sets_v`), with no project or dataset prefix and no backticks. db-api no longer rewrites the SQL to achieve that: `genetics-results-suite-4h6.53` deleted `_qualify_tables` and set `default_dataset` on both the dry-run and execution job configs instead, so **BigQuery** resolves a bare name against db-api's own dataset. Qualifying is the failure mode rather than the fix — db-api owns the dataset identity, so the same emitted SQL serves dev and production — while backticks are now merely a style deviation rather than the correctness hazard the rewrite made them (BigQuery resolves a backtick-quoted bare name like any other identifier). See `docs/datasets-yaml-schema.md`, "Field details for `tables.<table>.examples`". The build **also** fails while `sandbox/schema/` or `sandbox/stubs/` still hold `PLACEHOLDER*` files (`genetics-results-suite-4h6.13`, now landed). See `docs/code-execution-security.md`, "Where the image lives".
- **Create secrets**: `./scripts/create-secrets.sh [--context <ctx>]` — creates k8s secrets from environment variables (includes `SLACK_WEBHOOK_URL` for the monitor). **It gets the same cluster context guard `rollout.sh` has**, from the same `lib/env.sh` implementation: immediately after `resolve_deploy_env`, **before `load_deploy_env`**, and ahead of *every* cluster-contacting call — including the `kubectl get secret` reads in `secret_key()`, which run long before the first write — it refuses unless `kubectl config current-context` is the exact string the resolved tfvars' mandatory `kube_context` key names, and all **seven** kubectl invocations below then run pinned with `--context "${ACTING_CONTEXT}"` — the guard's `readonly` freeze of its verdict, not the plain `CURRENT_CONTEXT` it also leaves behind, so that the `.env` sourced immediately afterwards cannot rewrite the target the pins carry — the four that actually reach a cluster (`secret_key()`'s `get secret`, plus the three `apply -f -` halves of the create/apply pipelines) and, uniformly, the three `create secret ... --dry-run=client` halves that do not, so that a later edit dropping a `--dry-run` cannot silently unpin the call it turns into a write. **The `load_deploy_env` ordering is part of the guard, not housekeeping**: `load_deploy_env` sources `.env.<env>` with `set -a`, i.e. arbitrary shell from a file the script does not control, and `TFVARS` is exported by then — so while it ran first, a `TFVARS=` line in that file re-pointed the guard at another deployment's tfvars (green success line, then reads and writes on production) and an `OVERRIDE_CONTEXT=` line set the very variable the `--context` **flag** exists to keep per-invocation, converting a hard refusal into the off-target-and-proceed path. Deciding before the file is read closes both; the guard needs nothing from `.env`. **Ordering alone is not enough**, and the comment here once reasoned only about the guard's inputs and about when its output is first *read*: a `CURRENT_CONTEXT=` line in `.env` *wrote* that output, and drove all three Secret writes onto production behind a truthful green staging line. The freeze (`readonly ACTING_CONTEXT`, above) is the other half — and **not the last half**: ordering protects the guard's inputs and the freeze protects its output, but neither touches `.env` changing what the frozen verdict *means*. A `kubectl() { ... }` function, a `PATH=` line or a `KUBECONFIG=` line in that file each lets the pin expand faithfully and then be **reinterpreted** — all three driven, all three silent, all three leaving the green staging line above truthful while every write goes to production. So immediately after `load_deploy_env` returns and before the first cluster contact, the script **re-asks `kubectl config current-context` and refuses unless it still equals `ACTING_CONTEXT`**, naming `.env.<env>` as the only thing that ran in between. That is an **accident detector, not a security boundary**, and the code says so: it catches the `PATH=` and `KUBECONFIG=` lines, because both change what the second question *resolves to* while the frozen answer stays put; it does **not** catch a `kubectl()` shell function, which answers the re-check too and lies consistently (verified). It lives in `create-secrets.sh` and not in `require_kube_context` because `rollout.sh` never calls `load_deploy_env`, so it has no window to re-check and must not gain a second `kubectl config current-context` call for one. Cost on the normal path: exactly one extra `kubectl config current-context`. It was the last unguarded cluster mutator *of the five `DEPLOY_ENV`-resolving entry points* (the three `keycloak-*.sh` scripts remain unguarded, above), and its blast radius is the worst of them: it used to print `kubectl config current-context` one line above writing `genetics-secrets`, and rotating `internal-api-secret` against the wrong cluster breaks every running pod there, while the daly production context differs from staging's by a trailing `-staging` alone. The override is a per-invocation `--context` **flag**, never an environment variable, for the reason measured in `rollout.sh`: an `export` outlives the run it was typed for and re-authorises every later one from the same shell. `create-secrets.sh` takes **no positional arguments** — every other input is an environment variable — so anything else on the command line is rejected rather than ignored. It also needs the **config profile** to know whether to write `keycloak-secrets` (daly only), and reads it with `tfvar config_profile` out of the tfvars `scripts/lib/env.sh` resolved. That file is gitignored and exists only in the main checkout; `resolve_deploy_env` **refuses with exit 1** when it is missing, rather than letting a guessed profile write the wrong per-profile secrets. `CONFIG_PROFILE=daly|finngen` overrides the value read from the file, and that `daly|finngen` is **enforced**, not advertised: any other value (a typo, a case slip like `Daly`, or a tfvars with no `config_profile` line, which parses to empty) also exits 1, because an unrecognised profile would otherwise fall through to `ENABLE_KEYCLOAK=false` and skip `keycloak-secrets` silently. Before that guard existed it died with exit 2 and no output at all, because the `grep` on the missing file tripped `pipefail`.
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
since `stream_chat` is reachable with raw dicts. **Anthropic is now the only selectable
provider**: `stream_chat` refuses `provider="openai"`, any other
non-`anthropic` provider, and any model whose name does not start with `claude-` with a **400
before the stream opens** — refused at the request boundary rather than removed, so the
`_stream_openai` path and the `OPENAI_API_KEY` plumbing stay in place for a future
reinstatement. Full behaviour is documented in `../genetics-mcp-server/docs/project-spec.md`
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

### Tool profiles, and the `code` profile

The **Tools** option above is the `tool_profile` field, and it is resolved by **two** mechanisms in
`genetics-mcp-server/src/genetics_mcp_server/tools/definitions.py`. `TOOL_PROFILES` maps a profile
to whole tool **categories** (`api`, `bigquery`, `rag`, `nocode`); `TOOL_PROFILE_TOOLS` maps a
profile to an explicit set of tool **names** and takes precedence over it. `null` — the default — is *no
filtering at all*, not a union of the profiles, and an unrecognised string degrades to
general-only rather than raising, because the value is read back from `chat_messages` rows written
by older clients. The degrade is unchanged but no longer silent to an operator: since
`genetics-results-suite-4h6.74` the `None` branch logs one WARNING per **distinct** unknown value
(bounded at 64, because a stored profile is re-sent on every turn), and
`GET /chat/v1/tools/resolved?tool_profile=<v>` answers `known_profile: false` for the same input.

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

**Shipping dark is the settled position, not a holding pattern.** It was to be revisited by the
paired A/B, which was **descoped on 2026-08-30** by user
decision — initial benchmarking was done by hand, and further benchmarking moves outside this
epic. That bead's kill criterion read *"if the code arm does not beat the baseline on cost AND does
not regress quality, keep it behind the profile rather than defaulting it on"*, and its
conservative branch **is** the status quo, so the absence of a measurement has a defined outcome:
**code execution stays opt-in and `null` remains the default profile.** Read that precisely — the
code arm was never run against the baseline, so nothing here records it losing; the decision was
simply not taken on numbers, and the documented default therefore stands. No A/B result exists
to look up, and none is coming from this epic.

`nocode` is the fourth category-union profile, added for the code-versus-tools A/B and,
like `rag`, **server-side only and deliberately never user-facing** — the browser's control does not
offer it, and its own list does not even contain the name. That is not an oversight to be corrected:
it is the comparator arm, and a user must not be able to pick it. A value already sitting in
`user_settings` (written by a benchmark harness or by hand) is no longer discarded, though — the
browser probes the server for it and keeps it if the server confirms it, which is what makes a
stored `nocode` behave as stored without ever being advertised (see below).
It resolves to `{general, api, bigquery}`: `null` minus exactly `run_analysis`,
`list_capabilities` and `read_artifact` under the deployed flags (65 → 62, measured 2026-08-19).
It exists because `null` **is not** a pre-code-execution baseline — `null` contains `run_analysis`,
so an arm meant to represent the old surface could reach for the mechanism under test. Note the
equivalence rides on a runtime flag, not on the category: excluding `orchestration` also excludes
`launch_subagents`, which only stays out because `enable_subagents` defaults to false. Turn it on
and `nocode` is no longer "the old surface".

#### Selecting a profile from the browser

The **Tools** control offers **All** (`null`), **API**, **Database** (`bigquery`) and **Code
execution** (`code`). The server knows **five** profiles and the browser's own list names **four**:
`rag` is in the browser's list but carries a `null` label, so it is resolvable and never rendered;
`nocode` is not in the browser's list at all. Both omissions are deliberate — do not read the two
lists as copies of each other. The control had been commented out of `LLMChat.tsx` entirely, so the stored profile
rode along with every request while nothing could change it — which is why the row above described
a **Tools** option no one could see. It is back, with `code` added, so the small surface can be
A/B'd against the full one. The default is unchanged: **All** — and, since that A/B was descoped
without running (see "It **ships dark**" above), that default is settled rather than provisional.

The browser's own hazard is the mirror image of the server's, and is worth stating because it reads
backwards. Every narrower — `coerceToolProfile`, the store's `resolveCurrent`, the control — maps
an **unrecognised** profile to `null`, and `null` is the **largest** surface, not the smallest. So a
list left behind by a new server-side profile does not fail, it runs the maximal arm — a benchmark
driven through the browser would be invalid with no visible symptom — unless the adoption path
below rescues the value first, which it can only do when the server answers. The server makes the opposite
call for the same input (unknown → general-only). Both are deliberate — the value comes back from
`user_settings` and from `chat_messages` rows written by older clients, so neither side may raise.

`genetics-results-suite-4h6.74` pins the two lists together rather than leaving the drift merely
recorded. Three mechanisms, and it is worth knowing which one catches which direction:

- **A profile the server added that this browser build predates** is caught at runtime, on the
  browser side. `getStoredChatOptions` keeps the raw value as `unknownToolProfile` when
  `coerceToolProfile` rejects it, and `adoptServerKnownProfile` (`useChatOptions.ts`) asks
  `GET /chat/v1/tools/resolved?tool_profile=<v>`. Only `known_profile: true` changes anything: the
  value is kept, labelled from the raw key by `toolProfileLabel` (`LLMChat.tsx`), offered as an
  extra radio and sent on the next message. Nothing is persisted — the settings row already holds
  it. The stored setting is not the only place such a name arrives from: `tool_profile` is
  persisted **per message**, so reopening a conversation that ran under a server-only profile
  narrowed it the same way, and that is the likelier path — `applyFromConversation` probes it too.
  The answer stays in the layer it came from: a conversation's name is adopted into the control
  only while that conversation is the one on screen, and never becomes the user's default for new
  chats. One probe per name per page either way — a settled answer is re-applied from the store's
  cache, so reopening the same conversation does not re-ask. The value must first pass `isPlausibleToolProfile` (non-empty, ≤ 32 chars,
  `^[a-z][a-z0-9_-]*$`, not the `all` sentinel) or it is never asked about and never rendered;
  corruption in a settings row is not drift.
- **A profile this browser offers that the server no longer knows** is caught at runtime too, by
  the same endpoint, called whenever a profile is selected and whenever one is restored at load.
  An explicit `known_profile: false` puts an amber "not recognised by the server" beside the
  **Tools** control; `true` shows the resolved local-tool count. **A failed or unanswerable probe
  shows nothing at all** — offline, a 5xx, or a backend predating the endpoint is not evidence of
  drift, so the check is fire-and-forget, never gates sending, and is left out of the store rather
  than recorded as "unknown".
- **A profile added or renamed on the server** is caught at build time, on the server side, by
  `tests/test_unknown_profile_warning.py::test_the_profile_key_set_is_pinned_against_the_browsers_copy`,
  which asserts `TOOL_PROFILES | TOOL_PROFILE_TOOLS == {api, bigquery, rag, nocode, code}` against a
  literal and names the browser file to update. The two repos cannot import each other, so a literal
  on each side is the only thing that can pin them.

What keeps the browser side safe underneath all of that is unchanged: `TOOL_PROFILES`
in `src/features/chat/chat.types.ts` is the single list every narrower reads, `TOOL_PROFILE_LABELS`
in `LLMChat.tsx` is a `Record<ToolProfile, …>` so a new profile is a **type error** until the UI has
decided about it (`null` there means "deliberately not offered", which is `rag`), and
`useChatOptions.test.ts` / `LLMChat.options.test.tsx` / `useChatOptions.profileCheck.test.ts` drive
their cases off that list and pin the unknown-value and probe behaviour explicitly.

**Hazard, unresolved**: `run_analysis` is the primary tool of the
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
