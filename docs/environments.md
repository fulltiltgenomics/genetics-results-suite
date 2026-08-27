# Deployment environments

The suite is deployed more than once. Each deployment is a **separate GKE cluster with its
own VPC, node pool, PVCs, Keycloak realm, Keycloak database and secrets** — nothing is shared
between clusters at runtime except read-only source data (GCS/BigQuery).

| `DEPLOY_ENV` | GCP project | cluster | primary host | branch built | state prefix |
|---|---|---|---|---|---|
| `daly` | `daly-finngenie` | `finngenie` | `genegenie.broadinstitute.org` (301 from `finngenie.…`) | `master` | `gs://genetics-results-terraform-daly/genetics-results-suite` |
| `daly-staging` | `daly-finngenie` | `finngenie-staging` | `staging.genegenie.broadinstitute.org` | `staging` | `gs://genetics-results-terraform-daly/genetics-results-suite-staging` |
| `finngen` | (separate project) | `finngenie` | `finngenie.finngen.fi` | `master` | `gs://genetics-results-terraform/genetics-results-suite` |

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

Three guardrails exist because picking the wrong one silently deploys across environments:

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
  `--cookie-secure` / `--cookie-httponly` / `--cookie-samesite=lax` and no `--cookie-domain`.
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
- [ ] **`staging` branches exist.** `build-all.sh` clones with `--branch staging` and fails
      outright if the branch is missing. Needed in `genetics-results-browser`,
      `genetics-results-api`, `genetics-mcp-server`, `genetics-results-db`.
      (`monitor` and `keycloak` build from this repo's working tree — no branch involved.)
- [ ] **Google OAuth client.** Add the staging broker callback to the existing client's
      authorized redirect URIs (or create a separate client):
      `https://staging.genegenie.broadinstitute.org/auth/realms/genetics/broker/google/endpoint`
- [ ] **Fill in `.env.daly-staging`** (created, gitignored, currently blank where it matters):
      `ANTHROPIC_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and
      `OAUTH2_PROXY_CLIENT_SECRET` (`openssl rand -base64 32` — it is written to
      `oauth2-proxy-secrets` *and* rendered into the realm import, so both sides agree).
      Use a **different `SLACK_WEBHOOK_URL`** than production, or leave it empty; otherwise
      staging's daily monitor report lands in the production alert channel.
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
before `create-secrets.sh` has anywhere to write. Confirm the context first:

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
      staging containers, and `genetics_chat_logs` should contain no staging rows.
      `SELECT DISTINCT resource.labels.cluster_name FROM genetics_chat_logs.*` after a day.
- [ ] **Snapshot policy.** `deploy.sh` attaches `chat-data-daily-snapshot-staging` to the
      chat-data disk on the *second* run — the PVC is unbound on the first. It prints
      `PVC chat-data not yet bound, skipping…` when that happens; re-running is the fix.

## Ongoing use

```bash
DEPLOY_ENV=daly-staging ./scripts/build.sh chat-backend      # rebuild one service from `staging`
DEPLOY_ENV=daly-staging ./scripts/rollout.sh chat-backend    # roll it out
```

`rollout.sh` only sets the image reference; the cluster it acts on is whatever kubectl's
current context points at, which it now echoes. Switch contexts deliberately:

```bash
gcloud container clusters get-credentials finngenie-staging --zone us-central1-a --project daly-finngenie
gcloud container clusters get-credentials finngenie         --zone us-central1-a --project daly-finngenie
```

## Not carried over to staging

- **Apple Sign-In** — needs the staging domain and return URL registered in the Apple
  Developer portal. `APPLE_SERVICES_ID` is empty, so `deploy.sh` renders a Google-only realm.
- **The `brainzzz` MCP OAuth client** — its redirect URIs point at production. Leave
  `BRAINZZZ_CLIENT_SECRET` unset so the staging realm has no such client.
- **Log sinks** — `enable_log_sinks = false`. Set it true to get `genetics_api_logs_staging`
  and `genetics_chat_logs_staging`.
- **rag-service** — `ENABLE_RAG=false`, as in production.
