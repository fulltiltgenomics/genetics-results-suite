# Deployment environments

The suite is deployed more than once. Each deployment is a **separate GKE cluster with its
own VPC, node pool, PVCs, Keycloak realm, Keycloak database and secrets** — nothing is shared
between clusters at runtime except read-only source data (GCS/BigQuery).

| `DEPLOY_ENV` | GCP project | cluster | primary host | branch built | state prefix |
|---|---|---|---|---|---|
| `daly` | `daly-finngenie` | `finngenie` | `genegenie.broadinstitute.org` (301 from `finngenie.…`) | `master` | `gs://genetics-results-terraform-daly/genetics-results-suite` |
| `daly-staging` | `daly-finngenie` | `finngenie-staging` | `staging.genegenie.broadinstitute.org` | `staging` | `gs://genetics-results-terraform-daly/genetics-results-suite-staging` |
| `finngen` | `phewas-development` | `finngenie` | `finngenie.finngen.fi` | `master` | `gs://genetics-results-terraform/genetics-results-suite` |

`daly` and `daly-staging` are managed from the same admin instance; `finngen` is managed
elsewhere.

## Selecting an environment

Every entry-point script (`deploy.sh`, `create-secrets.sh`, `build.sh`, `build-all.sh`,
`rollout.sh`) resolves the target through `scripts/lib/env.sh`:

```bash
DEPLOY_ENV=daly-staging ./scripts/build-all.sh
DEPLOY_ENV=daly-staging ./scripts/deploy.sh
```

`DEPLOY_ENV=<name>` selects three files, all gitignored except the backend config:

| file | purpose |
|---|---|
| `terraform/terraform.tfvars.<name>` | terraform variables, passed with `-var-file` |
| `terraform/<name>.tfbackend` | GCS state backend (bucket + prefix) — **committed** |
| `.env.<name>` | secrets and deploy knobs, sourced with `set -a` |

Four guardrails exist because picking the wrong one silently deploys across environments. All
four live in `scripts/lib/env.sh`, but an entry point gets one only by **calling** the function
that holds it, so which apply depends on the script — see `project-spec.md`'s per-call-site table:

- **`.env.<name>` never falls back to `.env`.** A fallback would push one deployment's
  secrets into another's cluster. A missing file warns instead.
- **A bare `terraform/terraform.tfvars` is refused when `DEPLOY_ENV` is set.** Terraform
  auto-loads that file *in addition to* `-var-file`, so any variable the per-environment file
  omits would silently come from it. On this instance the daly values live in
  `terraform.tfvars.daly`; there must be no `terraform.tfvars`. Terraform's own
  `require_tfvars` precondition is therefore satisfied by **any** non-`.example`
  `terraform.tfvars*` file, not by the bare one specifically — see the "tfvars guard" bullet
  in `project-spec.md` for what that costs.
- **A `REGISTRY` that disagrees with `DEPLOY_ENV` is refused.** `REGISTRY` is derived from the
  selected tfvars (`<region>-docker.pkg.dev/<project_id>/genetics-results<resource_suffix>`), so
  it no longer has to be exported by hand. But the README tells you to export it and shells keep
  it, and a stale production value would push staging-branch images over production's `:latest`
  tags — which the next pod restart then pulls. When `DEPLOY_ENV` is set, an inherited
  `REGISTRY` must match the derived one; otherwise the run stops with both values printed.
  `unset REGISTRY` is the usual fix; `REGISTRY_FORCE=1` overrides for a deliberate scratch
  registry.
- **A kubectl context that disagrees with `DEPLOY_ENV` is refused** — by `rollout.sh` and
  `create-secrets.sh`, the two `DEPLOY_ENV`-resolving entry points that mutate a cluster without
  first pointing kubectl at one. They are not the only unguarded cluster mutators in `scripts/`:
  `keycloak-register-client.sh`, `keycloak-bind-allowlist.sh` and `keycloak-register-brainzzz.sh`
  each read the Keycloak admin password out of `keycloak-secrets` on the **ambient** context and
  then `kubectl exec … kcadm.sh` inside the pod to create or rotate OIDC clients, bind the
  allow-list authenticator and set realm attributes. None of them sources `lib/env.sh`, resolves
  `DEPLOY_ENV` or even echoes the context, so none can call this guard as it stands; guarding them
  is separate work. Nothing is derived: the deployment **states** its cluster in a mandatory
  `kube_context = "<context name>"` line in its own tfvars, and `require_kube_context` refuses
  unless `kubectl config current-context` is exactly that string, then freezes that verdict
  (`readonly ACTING_CONTEXT`) and pins it with `--context` on every cluster-contacting call — so
  the cluster acted on cannot be changed after the check, not by another terminal's
  `use-context` and not by a `.env.<env>` line sourced afterwards. The **namespace** is
  deliberately not frozen and not guarded. The override is a per-invocation `--context` flag in
  both, never an environment variable. **The tfvars are gitignored, so the key does not arrive with a clone**:
  both scripts refuse in a checkout that has not added the line. That is fail-closed on purpose —
  two of the three deployments are production and both production clusters are named `finngenie`.
  `deploy.sh` needs no such guard: it *sets* the context from `terraform output` before its first
  apply. Details in `project-spec.md`, "The cluster context guard".

With `DEPLOY_ENV` unset the scripts keep the original single-deployment behaviour (bare
`terraform.tfvars`, backend derived from its `config_profile`), which is what the `finngen`
instance uses.

## Two deployments in one GCP project

`daly` and `daly-staging` share the `daly-finngenie` project, so anything **project**-scoped
would collide by name. `resource_suffix` (empty for daly, `-staging` for daly-staging) is
appended to exactly those names:

| resource | daly | daly-staging |
|---|---|---|
| Artifact Registry repo | `genetics-results` | `genetics-results-staging` |
| Workload Identity GSA | `genetics-suite-gke` | `genetics-suite-gke-staging` |
| chat-data snapshot policy | `chat-data-daily-snapshot` | `chat-data-daily-snapshot-staging` |
| Keycloak backup bucket | `daly-finngenie-keycloak-backups` | `daly-finngenie-keycloak-backups-staging` |
| log sinks / BQ datasets | `genetics_{api,chat}_logs` | `genetics_{api,chat}_logs_staging` |

**Cluster**-scoped names (VPC, subnet, firewall, node pool) already derive from
`cluster_name` and need no suffix. PVCs live inside the cluster and are separate by
construction.

Two shared-project hazards are handled explicitly:

- **The dataset the cluster serves.** `bq_dataset` in the tfvars (default `genetics_results`)
  is rendered by `deploy.sh` into db-api's `DATASET_ID` *and* the monitor CronJob's
  `BQ_DATASET`, so both readers of a deployment see the same data. daly-staging sets
  `genetics_results_dev`, the rehearsal clone (`docs/bigquery-dev-dataset.md`). `deploy.sh`
  refuses an empty value rather than defaulting it: db-api's own fallback is
  `genetics_results`, so an empty render serves production data from staging without failing.
- **Log sinks.** `terraform/logging.tf` filters both sinks on
  `resource.labels.cluster_name`. Without it a namespace/container-only filter routes both
  clusters into the same BigQuery dataset, mixing staging traffic into the production chat and
  endpoint-access tables (and therefore into `chat_usage_stats.sh`).
- **Monitor alerts.** `scripts/monitor/alerter.py` reads Cloud Logging project-wide and both
  clusters use the `genetics` namespace, so it also filters on `K8S_CLUSTER` (injected into
  `monitor-cronjob.yaml` from the terraform `cluster_name` output). Unset, the variable is
  omitted from the filter and the old project-wide behaviour returns.
- **Cookie domain.** `staging.genegenie.broadinstitute.org` is a **subdomain of** the
  production host `genegenie.broadinstitute.org`, not a sibling. The rename did not create a
  cross-host cookie channel — `broadinstitute.org` is not a public suffix, so the old
  `staging-genegenie.broadinstitute.org` could equally have set `Domain=broadinstitute.org`,
  and production would have sent it. What the rename narrowed is the *value* needed to open
  the channel: under the hyphen that value was `Domain=broadinstitute.org`, which also leaks
  to every other Broad host and reads as obviously over-broad in review; under the dot it is
  `Domain=genegenie.broadinstitute.org` — literally the correct value for the production
  deployment, so a tfvars or manifest copied from production produces it verbatim and
  survives review.
  Two components set cookies on this host. `k8s/deployments/oauth2-proxy.yaml` passes
  `--cookie-secure` / `--cookie-httponly` / `--cookie-samesite=lax` / `--cookie-refresh=2m`
  and no `--cookie-domain`. `--cookie-refresh` is coupled to the realm rather than to the
  cookie: it must stay below the realm's `accessTokenLifespan`, or oauth2-proxy stops
  refreshing and `/oauth2/auth` answers 401 once the access token expires. That lifespan is
  not in `realm-genetics.json.template` — it is Keycloak's own default, so it is whatever the
  running realm says (`kcadm.sh get realms/genetics --fields accessTokenLifespan`) and a realm
  edit in the admin console can change it with nothing in this repo moving. The gateway then 302s to `/oauth2/start`, which a GET survives invisibly and a
  POST does not — the redirect strips the body. Lowering the realm's lifespan without
  lowering this flag reintroduces that.
  Keycloak is served on the *same* host under `/auth` (`deploy.sh` builds `KEYCLOAK_HOST` from
  `DOMAIN` + `KEYCLOAK_PATH`); `k8s/deployments/keycloak.yaml` sets no cookie attributes at
  all, and the gateway nginx block rewrites them with `proxy_cookie_flags ~ secure
  samesite=none`, deliberately, for the Apple `form_post` callback — so `AUTH_SESSION_ID` /
  `KEYCLOAK_IDENTITY` / `KEYCLOAK_SESSION` / `KC_RESTART` are the `SameSite=None` half, the
  fully cross-site-usable half if a `Domain` is ever introduced.
  What is actually verified is narrow: **no component sets an explicit cookie `Domain`, so
  cookies are host-only.** That is a default, not a control — nothing asserts it. Two edits
  would open the channel: a cookie-domain setting on oauth2-proxy, or nginx's
  `proxy_cookie_domain` — the sibling directive to the `proxy_cookie_flags` already in use,
  inside a shell `printf` in `deploy.sh`, which is the site least likely to be caught by
  anyone grepping manifests for `--cookie-domain`.

### Known limitation: the Workload Identity principal is shared

Workload Identity principals are project-wide, not cluster-wide. Both clusters use namespace
`genetics` and KSA `genetics-suite`, so both map to the identical principal
`daly-finngenie.svc.id.goog[genetics/genetics-suite]`, and that principal holds
`roles/iam.workloadIdentityUser` on *both* GSAs.

In practice each cluster's pods get only their own GSA, because the GKE metadata server issues
tokens based on the **KSA's annotation**, which terraform sets per cluster. But a
cluster-admin on staging could re-annotate the KSA to `genetics-suite-gke@…` and obtain
production's service account. Both clusters are administered by the same operator, so this is
accepted rather than fixed; separating it would mean giving staging a different namespace,
which is hardcoded in ~40 manifests.

---

# Bringing up `daly-staging`

## Before deploying

- [ ] **DNS.** Create an A record for `staging.genegenie.broadinstitute.org` → `34.36.39.82`
      (the reserved global IP `staging-finngenie-broadinstitute-org-ip`). Do this **first** —
      the Google-managed certificate cannot provision until the record resolves, and a
      ManagedCertificate that starts in `FAILED_NOT_VISIBLE` takes a delete/recreate to retry.
      Verify: `dig +short staging.genegenie.broadinstitute.org`.
- [ ] **`staging` branches exist *and* `.env.daly-staging` selects them.** `build-all.sh`
      does not assume `staging`: each cloned repo takes its branch from its own variable,
      **defaulting to `master`** (`scripts/build-all.sh`) — `FRONTEND_BRANCH`
      (`genetics-results-browser`), `RESULTS_API_BRANCH` (`genetics-results-api`),
      `MCP_SERVER_BRANCH` (`genetics-mcp-server`) and `DB_API_BRANCH`
      (`genetics-results-db`). All four must be **set to `staging` in `.env.<DEPLOY_ENV>`**
      (here `.env.daly-staging`); a `staging` branch that exists on GitHub but is not named
      in that file is simply not built — the run silently builds `master` instead. Once a
      variable is set, `git clone --depth 1 --branch` fails the build if that branch is
      missing. (`monitor` and `keycloak` build from this repo's working tree — no branch
      involved. `genetics-rag-service` has a fifth variable, `RAG_SERVICE_BRANCH`, default
      `deploy_jk` and not a `staging` branch; it is cloned and built unconditionally.)
      One absence is deliberately **not** fatal: if the `MCP_SERVER_BRANCH` clone has no
      `src/genetics_mcp_server/sdk`, or the schema-doc generation fails, `build-all.sh` skips
      the sandbox image, says so, and still exits 0 — it fails only when the sandbox is
      enabled for this deployment (`sandbox_pool_enabled = true` in the tfvars, or
      `ENABLE_SANDBOX=true`).
- [ ] **Google OAuth client.** Add the staging broker callback to the existing client's
      authorized redirect URIs (or create a separate client):
      `https://staging.genegenie.broadinstitute.org/auth/realms/genetics/broker/google/endpoint`
- [ ] **Fill in `.env.daly-staging`** (created, gitignored, currently blank where it matters):
      `ANTHROPIC_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and
      `OAUTH2_PROXY_CLIENT_SECRET` (`openssl rand -base64 32` — it is written to
      `oauth2-proxy-secrets` *and* rendered into the realm import, so both sides agree).
      Use a **different `SLACK_WEBHOOK_URL`** than production, or leave it empty; otherwise
      staging's daily monitor report lands in the production alert channel.
- [ ] **`DEFAULT_TOOL_PROFILE=code` in `.env.daly-staging`.** Staging starts its chat users on
      the `code` profile — the seven-tool code-execution surface — while production leaves the
      variable unset and starts them on **All**. It is rendered into chat-backend by
      `scripts/deploy.sh` and served to the browser as the profile of anyone who has not chosen
      one; a user's own choice still wins and persists (`docs/project-spec.md`, "Tool profiles").
      Since it is read from `.env.<name>`, a deploy run without the line silently returns staging
      to All — that is the drift to look for when staging answers with direct tools again.
- [ ] **Confirm there is no `terraform/terraform.tfvars`.** The daly values now live in
      `terraform/terraform.tfvars.daly`; the scripts refuse to run while both exist.
- [ ] **`unset REGISTRY`** if your shell profile exports the production one (it currently does:
      `us-central1-docker.pkg.dev/daly-finngenie/genetics-results`). The scripts stop on the
      mismatch rather than push staging images over production tags, but unsetting it avoids
      the interruption — the right value is derived per environment.
- [ ] **Quota.** The staging node pool adds 2 × `e2-standard-4` (8 vCPU) in `us-central1-a`
      on top of production's. Check `CPUS` / `IN_USE_ADDRESSES` regional quota.
- [ ] **Cost.** Staging roughly doubles the fixed spend: 2 nodes, a second GKE control plane,
      four PVCs (10Gi chat-data, 50Gi rag-stores only if `ENABLE_RAG=true`, 1Gi monitor-data,
      5Gi keycloak-postgres), a second load balancer, and daily chat-data snapshots.

## Deploying

```bash
export DEPLOY_ENV=daly-staging

# 1. infrastructure + cluster (creates 18 resources; ~10 min for the cluster)
./scripts/deploy.sh          # fails at the secrets check below — that is expected on a first run

# 2. secrets, once kubectl points at the new cluster
./scripts/create-secrets.sh

# 3. images from the staging branches
./scripts/build-all.sh

# 4. full deploy
./scripts/deploy.sh
```

Step 1 stops at `ERROR: genetics-secrets not found` because `deploy.sh` checks for secrets
before applying manifests. That is the intended order: terraform has to create the cluster
before `create-secrets.sh` has anywhere to write. Confirming the context first is no longer left
to you — step 2 refuses unless it matches this environment's `kube_context` — but it is still the
fastest way to see where you are:

```bash
kubectl config current-context   # ..._us-central1-a_finngenie-staging
```

## After deploying

- [ ] **Certificate.** `kubectl get managedcertificate -n genetics -w` — `Provisioning` →
      `Active` takes 15–60 min after DNS resolves.
- [ ] **Certificate SAN.** Once `Active`, read the SAN that was actually issued:
      `openssl s_client -connect <ip>:443 -servername staging.genegenie.broadinstitute.org
      </dev/null | openssl x509 -noout -text | grep DNS:` — it must be
      `staging.genegenie.broadinstitute.org` exactly. `kubectl describe managedcertificate -n
      genetics` shows the `spec.domains` this deploy *requested*, generated from the same
      tfvars value you are validating, so it echoes staleness rather than detecting it; only
      the `openssl` branch reads the issued certificate. A stale `domains` normally means the
      certificate never provisions at all (`FAILED_NOT_VISIBLE`, per the DNS step above); it
      reaches `Active` for the wrong name only if the stale host happens to resolve to the
      same IP, which nothing here checks. Either way `domains[0]` also drives `KC_HOSTNAME`
      and the ingress host rules (`terraform/outputs.tf`), so a mismatch surfaces as a
      redirect/login loop.
- [ ] **Keycloak realm.** The realm import runs **only against an empty database**. Check
      `deploy.sh` printed `keycloak-realm rendered (apple IdP: google-only)`; if
      `GOOGLE_CLIENT_ID` was missing at that point, fix `.env.daly-staging` and either delete
      the `keycloak-postgres-data` PVC (staging has nothing to lose) or edit the live realm in
      the admin console.
- [ ] **Sign in** at `https://staging.genegenie.broadinstitute.org` with an allow-listed
      account and confirm Google is the only IdP offered.
- [ ] **Check isolation.** Confirm production is untouched: its monitor should not report
      staging containers. Staging's own sinks (`enable_log_sinks = true`) write only
      `genetics_{api,chat}_logs_staging`, because both filters pin
      `resource.labels.cluster_name` to `finngenie-staging`. **The reverse does not hold yet**:
      production's live sinks were created before that pin and still carry none, so staging
      rows do reach `genetics_chat_logs`/`genetics_api_logs` — measured 2026-09-01, 553 and 519
      rows. `SELECT DISTINCT resource.labels.cluster_name FROM genetics_chat_logs.*` shows it.
      The fix is a production `terraform apply`, which re-creates production's sinks with the
      pin the config already has; nothing in staging can close it.
- [ ] **Snapshot policy.** `deploy.sh` attaches `chat-data-daily-snapshot-staging` to the
      chat-data disk on the *second* run — the PVC is unbound on the first. It prints
      `PVC chat-data not yet bound, skipping…` when that happens; re-running is the fix.

## Ongoing use

```bash
DEPLOY_ENV=daly-staging ./scripts/build.sh chat-backend      # rebuild one service from `staging`
DEPLOY_ENV=daly-staging ./scripts/rollout.sh chat-backend    # roll it out
```

`rollout.sh` only sets the image reference, and it no longer acts on whatever kubectl's current
context happens to be. Each deployment's tfvars **states** its cluster in a mandatory
`kube_context` key, and `rollout.sh` **refuses** when `kubectl config current-context` is not that
string, naming the `kubectl config use-context` to run (`genetics-results-suite-b1r`; see README,
"Updating Services"). Nothing is derived and no HCL is parsed: the token must appear **exactly
once** in the whole tfvars file, on a column-0 `kube_context = "..."` line, and the file must
contain no `/*` anywhere (`#` and `//` comments are fine; a block comment could otherwise present a
commented-out key as the one legal line). A missing, repeated, indented or non-quoted-string key,
or any `/*`, is a refusal, never a fallback. Because the tfvars files are **gitignored**,
each checkout has to add the key once or `rollout.sh` stops working there — intended, since the
alternative is guessing between two production clusters that share a name. A deliberately
off-target rollout needs the `--context <ctx>` flag, which must spell out the cluster being
mutated. On acceptance the guard prints the two environment-supplied inputs it trusted —
`Context: <ctx> (env: <env>, kube_context in <tfvars>, kubeconfig <path>)` — because `ROOT_DIR`
picks the first and `KUBECONFIG` the second, and an **inherited export** of either (already set
before the script starts) leaves the guard green while pointing it at another checkout's evidence
or another kubeconfig's endpoint. That printed kubeconfig covers the inherited export **only**: it
is printed before `create-secrets.sh` sources `.env.<env>`, so a `KUBECONFIG=` line *in that file*
makes the printed path accurate about what the guard read and obsolete about what the writes use.
`create-secrets.sh` re-asserts the context after sourcing for that case; `rollout.sh` never sources
`.env` and so has no such window. Switch contexts deliberately:

```bash
gcloud container clusters get-credentials finngenie-staging --zone us-central1-a --project daly-finngenie
gcloud container clusters get-credentials finngenie         --zone us-central1-a --project daly-finngenie
```

## Not carried over to staging

- **Apple Sign-In** — needs the staging domain and return URL registered in the Apple
  Developer portal. `APPLE_SERVICES_ID` is empty, so `deploy.sh` renders a Google-only realm.
- **The `brainzzz` MCP OAuth client** — its redirect URIs point at production. Leave
  `BRAINZZZ_CLIENT_SECRET` unset so the staging realm has no such client.
- **rag-service** — `ENABLE_RAG=false`, as in production.
