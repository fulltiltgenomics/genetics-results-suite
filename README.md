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
  ├── db-api            (BigQuery proxy, port 8080) — from chat-backend, mcp-server + sandbox
  ├── rag-service       (RAG retrieval, port 8000)  — only from chat-backend + mcp-server
  ├── keycloak-postgres (Keycloak DB, port 5432)    — from keycloak + keycloak-postgres-backup
  └── sandbox           (code execution, port 8080) — from chat-backend ONLY; egress limited to
                                                      db-api + results-api; the namespace's only
                                                      Egress policy. Its NetworkPolicies are
                                                      applied unconditionally (inert while no
                                                      pod matches); only the Deployment is
                                                      gated on ENABLE_SANDBOX=true
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

(`deploy.sh` does all of this for you from `DEPLOY_ENV` — see `docs/environments.md`. For a
single-deployment instance you may instead keep a bare `terraform.tfvars` and leave `DEPLOY_ENV`
unset.)

> The tfvars files are gitignored and live only in your main checkout. Terraform refuses to plan
> or apply when none of them is present (`require_tfvars`, default `true`) — otherwise a run from
> a git worktree or a fresh clone would use variable defaults and destroy the log sinks and
> replace the node pool. `apply -target=...` bypasses the guard entirely, and `destroy` does too
> (deliberately). If you keep values elsewhere, pass `-var-file=... -var require_tfvars=false`.

> **Which state a run writes to** is fixed by `terraform init -backend-config=<env>.tfbackend`,
> independently of the values in place. Apply one environment's project, region and domains into
> another's state and the plan will not look wrong. `scripts/lib/env.sh` derives both from the
> same `DEPLOY_ENV` so they cannot disagree, and a terraform `precondition` compares the
> initialized state bucket against the one `${config_profile}.tfbackend` names as a backstop for
> a bare `terraform apply`.

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

Terraform can create a **second** node pool, `<cluster_name>-sandbox-pool` — one pinned
`e2-standard-2` running gVisor (GKE Sandbox) for the code-execution sandbox. It is **off by
default** (`sandbox_pool_enabled = false`), because `scripts/deploy.sh` runs
`terraform apply -auto-approve` on every full deploy: terraform changes here are *not* opt-in,
so a pool gated on nothing would appear on a routine deploy nobody asked for.

When you set `sandbox_pool_enabled = true`, `sandbox_node_service_account` becomes **required**
in every mode, including `manage_iam = false`. The variable carries `default = ""` only so
terraform does not stop to prompt; `""` fails the resource's precondition. The SA must be in
this project, must not be `genetics-suite`, and must not equal `node_service_account` (the
primary pool's SA — that pool grants the `cloud-platform` scope, so sharing it puts the whole
suite's credential on the node running untrusted code). Those are **format and identity checks
only**: terraform does not create the SA and does not verify what roles it holds, so the recipe
below is the only thing bounding them. Create it before enabling the pool:

```bash
PROJECT=$(terraform output -raw project_id)
gcloud iam service-accounts create finngenie-sandbox-node --project="$PROJECT"
SA="finngenie-sandbox-node@${PROJECT}.iam.gserviceaccount.com"
for ROLE in roles/logging.logWriter roles/monitoring.metricWriter roles/monitoring.viewer \
            roles/stackdriver.resourceMetadata.writer roles/artifactregistry.reader; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA}" --role="$ROLE"
done
```

Note this pool is created with `workload_metadata_config { mode = "GKE_METADATA" }`
unconditionally, which is why `google_container_cluster.primary`'s `workload_identity_config`
is also unconditional — GKE rejects the former without the latter **at apply, not at plan**.
See "The sandbox pool" in `docs/project-spec.md` before changing either.

**One step the flag does not do for you**, hit on the first `daly-staging` bring-up: **the pool
comes up with zero nodes.** It is declared `min == max == 1` with no `initial_node_count`, so GKE
creates it empty and the autoscaler never scales it to its own minimum — nothing is pending on it,
because `scripts/deploy.sh` refuses to apply `sandbox.yaml` until a node carries
`workload=sandbox`. Break the deadlock once:

```bash
gcloud container clusters resize "$CLUSTER" \
  --node-pool="${CLUSTER}-sandbox-pool" --num-nodes=1 --zone="$ZONE"
```

> **What the next apply does whether or not you enable the pool.** `workload_identity_config`
> on the cluster is unconditional now, and the live cluster currently has an **empty**
> `workloadPool` (verified with `gcloud container clusters list`) because `manage_iam = false`.
> So the next apply **enables Workload Identity on the live cluster** — an in-place update, very
> likely inert (no pool is in `GKE_METADATA` mode and no KSA has a WI binding), but it reaches
> production without anyone opting into it. Two consequences worth knowing before you run it:
> `terraform.tfvars` is gitignored and lives only in the **main checkout**, so adding
> `sandbox_pool_enabled` / `sandbox_node_service_account` there is a manual step this repo cannot
> make for you; and `scripts/deploy.sh` applies terraform on every full deploy, so
> `SKIP_TERRAFORM=true` is the escape hatch if you want manifests only.
>
> **Known risk, recorded not fixed:** once WI is on, the primary pool has no explicit
> `workload_metadata_config` under `manage_iam = false`. If that pool is ever *recreated*
> (`terraform/main.tf` already warns pool replacement is a live hazard), its metadata mode would
> come from GKE's cluster-derived default rather than the `GCE_METADATA` all 8 workloads depend
> on. Whether to pin the primary pool explicitly is tracked separately; nothing here changes its
> behaviour.

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
export ADMIN_USERS="a@example.com,b@example.com"  # optional, emails allowed on the chat admin page
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."  # optional, for the monitor CronJob
export EXTERNAL_MCP_SERVERS="https://..."  # optional, external MCP servers proxied by chat-backend
# INTERNAL_API_SECRET for results API is auto-generated if not set
# GATEWAY_IDENTITY_SECRET (auth-gateway -> chat-backend provenance, gates code execution) is
# auto-generated too; it must stay DIFFERENT from INTERNAL_API_SECRET
# MCP_API_KEY (bearer token mcp-server requires for its sse/streamable-http transports; it
# refuses to start without one) is auto-generated too if not set; export it yourself
# (comma-separated for multiple keys) to pin a specific value instead

# oauth2-proxy credentials (YOUR_CLIENT_ID/SECRET from the OAuth client created in step 2).
# only needed on first install — afterwards they're reused from the cluster if unset.
export OAUTH2_PROXY_CLIENT_ID='YOUR_CLIENT_ID'
export OAUTH2_PROXY_CLIENT_SECRET='YOUR_CLIENT_SECRET'
# OAUTH2_PROXY_COOKIE_SECRET is generated on first install and reused thereafter (never rotated).

./scripts/create-secrets.sh
```

> **Run it from the main checkout.** It derives the config profile (which decides whether
> `keycloak-secrets` is written) from the tfvars `DEPLOY_ENV` selects, and those are gitignored
> and live only there. From a git worktree `resolve_deploy_env` refuses with exit 1 rather than
> guessing, and `CONFIG_PROFILE` does **not** rescue it — that variable overrides the profile
> *read from* the file, it does not stand in for a missing one.

### 4. Build and push Docker images

Authenticate docker:

```bash
gcloud auth configure-docker ${GCP_REGION}-docker.pkg.dev
```

Build and push images:

```bash
./scripts/build-all.sh
```

### 5. Wire up git hooks (once per clone)

```bash
./scripts/install-git-hooks.sh
```

The hook files under `.beads/hooks/` are tracked, but `core.hooksPath` — the local
git config that points git at them — is not, so a fresh clone runs **no** hooks:
no `check-doc-drift.sh` warning on commits and no beads export, silently. This
script sets it and repairs the doc-drift block if it has gone missing; it is
idempotent and safe to re-run, and works from a worktree. `deploy.sh` and
`build-all.sh` run it with `--check` and warn (never block) if it was skipped.

### Working from a git worktree

```bash
./scripts/check-worktree-paths.sh
```

Several things resolve to the **main checkout** even when you run them from a worktree,
and they degrade silently rather than erroring: `terraform.tfvars` (gitignored, main
checkout only), `core.hooksPath`, and bd's `.beads/issues.jsonl` export. The
`.beads/issues.jsonl` case is the most misleading — the worktree's
copy is tracked but nothing writes it, so `git add .beads/issues.jsonl && git commit`
stages nothing and git replies "nothing to commit, working tree clean". Bead state
itself is safe: the shared Dolt database is authoritative, and the git hooks never
import from the jsonl. You can refresh the committed export **from the worktree** —
one Dolt database is shared by every worktree, so `bd export -o .beads/issues.jsonl`
run from here writes the same current snapshot the main checkout would produce
(verified byte-identical). Running it in the main checkout works equally well; what
does not work is assuming a `git add` from here refreshed anything.

`sync-datasets.sh` used to be a fourth case, but it now resolves the sibling repos from
the git common dir, so it works from a worktree and fails loudly when it cannot resolve
them — `check-worktree-paths.sh` no longer reports it.

To **run** the suite from a worktree rather than build from it, use `scripts/dev-stack.sh`
(below): the local dev servers otherwise keep serving the main checkouts on `master` while
you edit a branch, and the config that makes them work — `genetics-mcp-server/.env`, the
browser's `.env.local` — is gitignored and exists only in the main checkout.

This script reports only the paths that actually diverge; it is silent in the main
checkout. `deploy.sh`, `build-all.sh` and `build.sh` run it with `--check`, warn, and
never block — a single-service build falls back to `APP_NAME=FinnGenie` from a worktree
just like a full build does, so it gets the same warning.
See `docs/project-spec.md`, "Worktree path resolution".

### 6. Deploy

```bash
./scripts/deploy.sh
```

This applies any terraform changes, configures kubectl, and deploys all k8s manifests. Use it for both the initial deployment and subsequent updates.

> **Note:** The k8s YAMLs use variable placeholders (`${REGISTRY}`, `${GCP_PROJECT}`, `${DOMAIN}`, etc.) — `deploy.sh` substitutes these automatically from terraform output. Do not `kubectl apply -f` the YAMLs directly; always use `deploy.sh` or `rollout.sh`.

> **Note:** the substitution is over the **whole document**, not over selected fields —
> `deploy.sh` pipes each file in `k8s/configs/`, `k8s/deployments/` and `k8s/cronjobs/` through
> `envsubst '<whitelist>'` in full. A whitelisted name spelled `${...}` in a *comment* is
> therefore substituted too, and two of the values (`LEGACY_REDIRECT`, `KEYCLOAK_SERVER`) are
> multi-line nginx fragments, so such an expansion breaks out of the `#` and the render stops
> being valid YAML. `scripts/test-manifest-render.py` is the guard: it is the first thing
> `deploy.sh` runs — before terraform, before kubectl, so a refusal costs nothing and cannot
> strand a half-finished deploy — and it aborts the deploy when a manifest would misrender.
> Names deploy.sh deliberately leaves out of the whitelist (`${INTERNAL_API_SECRET}`,
> `${GATEWAY_IDENTITY_SECRET}`, which a later initContainer renders from a Secret) and nginx's
> own `$host`/`$scheme` are asserted to survive verbatim rather than flagged. It catches the
> comment form and structural breakage, not every possible mangling — a multi-line fragment
> expanded into a scalar position still parses as YAML and is not flagged.

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

`rollout.sh` knows nine services — `frontend`, `bff`, `results-api`, `chat-backend`,
`mcp-server`, `db-api`, `rag-service`, `sandbox`, `keycloak` — and only swaps container images,
so ConfigMap-driven pods (auth-gateway) and the CronJobs still need `deploy.sh`. `monitor` is
deliberately not in that list, for the only reason that actually discriminates: it is a CronJob,
not a Deployment, so `kubectl set image deployment/monitor` cannot address it. A service with no
Deployment on the current context gets a "Not deployed" message and exit 1 — the ordinary case
for `sandbox` and `keycloak`, which are applied only when their gates are on. A query that
*failed* rather than answered (no context, expired credentials, unreachable API server) is
reported as "could not ask", with kubectl's own error, instead of as a missing service.

### Deploying the trusted-proxy marker

**Roll out `bff` before `results-api`, and never both in the same `deploy.sh`.** results-api
only honours `X-Goog-Authenticated-User-Email` from a caller that also presents
`INTERNAL_API_SECRET`, and it is the BFF that attaches that bearer.

```bash
./scripts/build.sh bff && ./scripts/rollout.sh bff            # 1. must land first
./scripts/build.sh results-api && ./scripts/rollout.sh results-api  # 2. only once bff is Running
```

- **bff new, results-api old** — safe, and safe to sit in indefinitely: the old API accepts the
  bearer as an internal service call, and the usage log still names the real user.
- **results-api new, bff old** — **every browser request 401s.** This is the order to avoid.
- Rolling back reverses it: results-api first, then bff.

`deploy.sh` restarts everything at once and gives no ordering, so use `rollout.sh` for the bff
step first. Details and the full state table are in `docs/project-spec.md`, "Ordered rollout:
the trusted-proxy marker".

The same rule reaches chat-backend — auth-gateway first:

```bash
./scripts/deploy.sh                                                    # 1. gateway ConfigMap
./scripts/build.sh chat-backend && ./scripts/rollout.sh chat-backend   # 2. after
```

- **auth-gateway new, chat-backend old** — safe, and safe to sit in: the gateway carries the
  marker on its own `X-Internal-Auth` header, which the old chat-backend does not recognise, so
  it behaves exactly as it did before. The fix simply is not in force yet.
- Both headers the gateway adds here (`X-Internal-Auth` and, since
  `genetics-results-suite-4h6.84`, `X-Gateway-Auth` carrying the separate
  `gateway-identity-secret`) behave that way. A **new chat-backend ahead of the gateway** loses
  only code execution — `run_analysis` refuses while chat itself keeps working — and both
  Deployments mount `gateway-identity-secret` non-optionally, so `create-secrets.sh` (or the
  `kubectl patch` `deploy.sh` prints) must run before either manifest is applied.
- **chat-backend new, auth-gateway old** — every browser chat request 401s. This is the order to
  avoid.
- Rolling back reverses it: chat-backend first, then auth-gateway; the state in between is the
  safe one, so a half-finished rollback costs nothing.

auth-gateway is ConfigMap-driven, so it needs `deploy.sh`; `rollout.sh` only swaps images. Full
state table in `docs/project-spec.md`, "Ordered rollout: the trusted-proxy marker (auth-gateway
before chat-backend)".

Rolling out `chat-backend` can block for up to ~5 minutes: it waits for any in-flight chat
stream to finish rather than cutting it off mid-answer. See "chat-backend shutdown and stream
draining" in `docs/project-spec.md`.

`build-all.sh` also builds the local `monitor`, `keycloak` and `sandbox` contexts.
`./scripts/build.sh sandbox` builds the sandbox alone. The sandbox image (distroless, no
shell, no pip, uid 65532) runs model-authored Python and pip-installs the genetics SDK
from genetics-mcp-server at build time, pruned to the SDK's import closure; **it is skipped
by `build-all.sh`, with a loud message, while that repo has no
`src/genetics_mcp_server/sdk/`** — which is the case on `master` today. Both build scripts
first run `./scripts/gen-sandbox-docs.py`, which regenerates the on-demand schema markdown
(`sandbox/schema/`, one file per BigQuery view in `configs/datasets.yaml`) and the SDK
signature stubs (`sandbox/stubs/`) the image carries at `/genetics/schema` and
`/genetics/sdk`, and then `./scripts/test-sandbox-docs.py`, which checks the committed
copies are current, that every view and column reaches a file **with its BigQuery type**,
and that the stubs cover exactly the SDK's exported surface. `build.sh sandbox` fails on
a non-zero exit; `build-all.sh` folds it into the same skip branch as the generator.
Exit 1 = a property broke, 2 = the harness could not run (no SDK source). Run either script
by hand with no `--sdk-src` and it resolves `GENETICS_SDK_SRC`, then `MCP_SERVER_DIR`, then the
live sibling genetics-mcp-server checkout, and prints which one it used — it never falls back to
the gitignored `sandbox/.sdk-src`, which only exists after an *interrupted* build and would
silently regenerate the shipped stubs from an old SDK (`genetics-results-suite-4h6.60`).
The build still fails while `sandbox/schema/` and `sandbox/stubs/` hold placeholders.

`./scripts/run-sandbox-local.sh` builds that same image and runs it in a **plain Docker
container** on the developer machine — no cluster, no credentials, nothing pushed — with the
supervisor passed as the container's command, `--read-only`, `--cap-drop ALL`, uid 65532 and a
writable `/scratch`. It publishes `127.0.0.1:8081` (the container port stays 8080; the local
db-api already holds 8080 on the host) and prints, on every start, the list of controls that
have **no** local form: gVisor, the NetworkPolicy, the kubelet pid limit, the exact seccomp
profile, `emptyDir` `sizeLimit` eviction, `ephemeral-storage` requests/limits, the Deployment's
restart behaviour and DNS — including the one that changes how limits must be sized, that the
local `/scratch` tmpfs is charged to the container's memory cgroup while the pod's disk-backed
`emptyDir` is charged to `ephemeral-storage` instead. `--test` then runs
`python3 scripts/test-supervisor.py --container http://127.0.0.1:8081 --container-name NAME`
against it (the name is what lets the audit-stream group read the container's stdout);
`--stop` removes it. See "Running the sandbox locally" in `docs/project-spec.md`.

The Deployment is `k8s/deployments/sandbox.yaml`. The image ships no `CMD` and its ENTRYPOINT is
the bare interpreter, so the manifest supplies the supervisor itself —
`args: ["/genetics/supervisor.py"]` on the sandbox container. It is applied only when
`ENABLE_SANDBOX=true`, which `scripts/deploy.sh` derives from `sandbox_pool_enabled` in
`terraform.tfvars`, and a preflight in deploy.sh — before the first `kubectl apply` of the run, so
a refusal cannot strand a half-finished deploy — refuses that apply on three preconditions: no
node carries `workload=sandbox` (the pod would be Pending forever); the sandbox **container**
declares neither `command:` nor `args:` (it would schedule and CrashLoopBackOff while the deploy
printed success — the manifest is parsed with PyYAML and the question is scoped to the container
named `sandbox` in the Deployment named `sandbox`, so no initContainer, sidecar, other document or
nested probe `exec.command` can clear it, reformatting cannot trip it, and a file that cannot be
parsed fails closed); or `${REGISTRY}/sandbox:${TAG}` is definitely not in Artifact Registry —
`gcloud` being absent, unauthenticated or unauthorised warns and proceeds instead, naming which
of those it was, because not being able to ask is not evidence the image is missing. That last one exists because `scripts/build-all.sh` skips the sandbox image
non-fatally when the mcp-server branch has no SDK or the generated schema docs fail to verify —
which would otherwise deploy a Deployment pointing at a tag nobody pushed. `build-all.sh` no
longer claims "All images built and pushed." after such a skip, and exits non-zero when the
tfvars actually enables the sandbox. Once applied, `sandbox` is restarted and waited on **last**
in the deploy's rollout list, because `strategy: Recreate` makes that restart a brief outage of
code execution. To update it on its own: `./scripts/rollout.sh sandbox <tag>` (it kills any
in-flight execution and leaves no sandbox for up to ~130 s; the 300 s rollout-status timeout
covers that, and it prints a "Not deployed" message rather than a raw `kubectl` error when the
gate has never been on). See "The sandbox Deployment" in `docs/project-spec.md` and
[docs/code-execution-security.md](docs/code-execution-security.md).

## Running the suite locally

Everything above deploys. To run the five services from source on one machine — results-api
`:2000`, frontend `:3000`, chat-backend `:4000`, BFF `:5000`, db-api `:8080`, one per repo —
see [docs/local-dev-vm.md](docs/local-dev-vm.md) for the from-scratch setup and
`scripts/dev-stack.sh` to drive them:

```bash
./scripts/dev-stack.sh up                 # all five from the worktree trees, db-api on genetics_dev
./scripts/dev-stack.sh up --tree main     # all five from the main checkouts, db-api on genetics_results
./scripts/dev-stack.sh status             # port, health, and which tree each pid is serving
./scripts/dev-stack.sh down
```

Both trees use the same five ports, so `up` frees each port first and one tree serves at a
time; switching back is `down` then `up --tree main`. A port is only freed when its holder
is this suite's — checked against `/proc/<pid>/cwd` and the command line — so an unrelated
app on `:3000` or `:8080` is reported and left alone (`--force` overrides) rather than
killed. `--tree worktree` points db-api at `genetics_dev`, the persistent **full-size**
copy of production's 15 tables (755,813,602 rows / 136.69 GB since 2026-08-18) — any gene
on any chromosome smoke-tests, `APOE` included. Nothing in this script touches the cluster. See "Running the local dev stack" in
`docs/project-spec.md`.

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
| sandbox | sandbox/ (local context; SDK from genetics-mcp-server) | sandbox | 8080 | Code-execution sandbox for model-authored Python: gVisor node pool, dedicated KSA with no GCP identity, one `emptyDir` and no other mount, reachable from chat-backend only. **Not applied unless `ENABLE_SANDBOX=true`**, which `deploy.sh` derives from `sandbox_pool_enabled` in `terraform.tfvars` (default false) |
| rag-service | genetics-rag-service | genetics-rag-service | 8000 | RAG document retrieval (internal only; skipped unless `ENABLE_RAG=true`) |
| monitor | — (scripts/monitor/) | monitor | — | CronJob (daily, 08:00 UTC): health checks, BQ coverage, log alerts → Slack |

The chat-backend and mcp-server share the same Docker image but run different commands; the frontend
and bff share the same source repo but build different Dockerfiles.

## Authentication

Authentication is handled by [oauth2-proxy](https://oauth2-proxy.github.io/oauth2-proxy/) behind an nginx auth-gateway. The auth-gateway uses nginx `auth_request` to validate each request against oauth2-proxy before proxying to backend services. Authenticated user email is passed via the `X-Goog-Authenticated-User-Email` header. That header is not itself a credential — results-api honours it only when the request also carries `Authorization: Bearer $INTERNAL_API_SECRET`, proving it came from an in-cluster proxy, and then still checks the address against the shared email allow-list; without the bearer the header is ignored and the request is unauthenticated. The BFF attaches that bearer on every upstream call, including the generic `/api` passthrough that forwards the identity header, so the two always arrive together on the browser path. **Shipping the two halves of that change in the wrong order locks all browser users out** — see [Deploying the trusted-proxy marker](#deploying-the-trusted-proxy-marker) under Updating Services.

Where the Keycloak identity broker is enabled, oauth2-proxy uses its `oidc` provider against Keycloak
(which in turn federates Google and Apple); otherwise it talks to Google directly. See
[docs/keycloak-apple-signin.md](docs/keycloak-apple-signin.md).

- **Frontend, Chat backend**: Protected by oauth2-proxy. Unauthenticated users are redirected to the sign-in page (Google directly, or the Keycloak provider chooser).
- **Results API**: Protected by oauth2-proxy for browser access, which routes through the BFF. Also supports `Authorization: Bearer` tokens for programmatic access — requests with a bearer token bypass both oauth2-proxy and the BFF and are validated directly by the backend (user-created tokens — the recommended path — Google Identity Tokens (deprecated) or internal shared secret).
- **MCP server**: Not behind oauth2-proxy. Uses bearer token auth via `MCP_API_KEY`, user-created tokens (recommended), Google Identity Tokens (deprecated), or — where the broker is enabled — Keycloak OAuth 2.1 access tokens.
- **DB API**: Internal only (NetworkPolicy restricts access to chat-backend, mcp-server and — where it is deployed — the sandbox), and additionally requires the `INTERNAL_API_SECRET` bearer token on every endpoint except `/health` — the NetworkPolicy alone is not a sufficient boundary, since mcp-server is allowed through it and is itself reachable from outside.
- **Internal service calls**: The chat-backend authenticates to results-api using a shared secret (`INTERNAL_API_SECRET`), auto-generated by `create-secrets.sh`.
- **Code execution requires a browser session, proven by a secret**: `run_analysis` dispatches only when the request carries `GATEWAY_IDENTITY_SECRET` (the `gateway-identity-secret` key of `genetics-secrets`, auto-generated by `create-secrets.sh`) in `X-Gateway-Auth`, which auth-gateway sets on its two chat locations after verifying an oauth2-proxy session. That key is mounted **only** into auth-gateway and chat-backend, so mcp-server and results-api — which hold `INTERNAL_API_SECRET` by design and can reach chat-backend on the pod network — cannot mint the provenance by choosing their own headers. Unset, it refuses every dispatch. Bounds in `docs/code-execution-security.md` §5, "Layer 2c".
- **Code-execution sandbox**: never holds `INTERNAL_API_SECRET`. chat-backend mints a short-lived (5 minute), audience-bound HS256 token per script execution, signed with a separate key (`SANDBOX_TOKEN_SIGNING_KEY`, also auto-generated by `create-secrets.sh`); db-api and results-api verify it, fail closed when the key is missing, and refuse to start at all when `SANDBOX_ENABLED=true` and either secret is unset. Both services then bound the execution in aggregate from in-process counters keyed on the token's `jti` — db-api 200 GB of BigQuery bytes, plus (since `genetics-results-suite-4h6.61`) the same request-count and concurrency rule results-api runs; results-api 1 GiB of response bytes — and both enforce 1000 requests and 4 concurrent requests (8 pod-wide) with a 4096-entry bound on the counter map. That is why the `replicas: 1` on both Deployments carries a comment saying it is load-bearing: scaling either up multiplies every one of those limits until the counters move to shared state. results-api's five and db-api's four are declared at their defaults in `k8s/deployments/results-api.yaml` and `k8s/deployments/db-api.yaml` so an operator can tune them without a rebuild, and either pod refuses to start on a value below 1 or on a pod-wide bound tighter than the per-execution one. **They bind only a request that presents a token** — a header-less request is never admitted — so results-api pairs them with an empty anonymous surface: with `ANONYMOUS_SURFACE_MINIMAL` on — its default, declared `"true"` in `k8s/deployments/results-api.yaml`, and forced by `SANDBOX_ENABLED=true` — `/healthz` is the only route it will answer without a resolved principal, and everything else 401s, so a script cannot shed its per-execution bounds by omitting the header (`genetics-results-suite-0lf`; the browser is unaffected because the BFF authenticates its upstream calls with the shared secret). That flag is deliberately **separate** from `SANDBOX_ENABLED` (`genetics-results-suite-rhh`): while the surface keyed on the sandbox switch, disabling the sandbox during an incident silently re-opened six routes to anonymous callers. Its counterpart in genetics-mcp-server is `genetics-results-suite-618`: the tool executor used to send **no** `Authorization` header when `INTERNAL_API_SECRET` was unset, so the deployed entrypoints now refuse to start without it rather than making anonymous calls that no log can distinguish from authenticated ones. The remaining gap — the two pod-wide bounds are cross-tenant denial surfaces — is documented in `docs/code-execution-security.md` §4.

### Programmatic API access (per-user API key)

Create a key in the browser: the user menu → **MCP and API keys** → *Create key*. The value is shown
once; the same dialog lists and revokes your keys. A key expires after 90 days without use and every
use extends it by another 90. The same key works for both `/api` and `/mcp`.

```bash
curl -H "Authorization: Bearer <TOKEN>" https://your-domain.example.com/api/v1/...
```

This is the recommended path for scripts and pipelines: the key is issued by this deployment, is
revocable per user, and is attributable in the token store — none of which is true of a Google
Identity Token.

**A key can only be created in the browser.** `POST /chat/v1/tokens` requires an oauth2-proxy
browser session, so no bearer token — not an existing API key, not a Google Identity Token — can
mint one. A CI job or service account therefore cannot self-serve: a human signs in once and
creates the key for it. Once created the key works headlessly and does not expire while it is in
use (every use extends it by 90 days), so this is a one-time step, but plan for it before
migrating a fully headless caller off the deprecated Identity Token path.

#### Google Identity Token (deprecated)

```bash
TOKEN=$(gcloud auth print-identity-token)   # deprecated — prefer a per-user API key
curl -H "Authorization: Bearer $TOKEN" https://your-domain.example.com/api/v1/...
```

Still accepted, and it will not be switched off without notice, but do not build anything new on it
and migrate existing callers to a per-user API key. Reasons: the token expires after 1 hour; this
deployment does not issue it and therefore cannot revoke it for one person; and authorization on it
rests entirely on the email allow-list (`ALLOWED_EMAIL_DOMAINS` / `ALLOWED_EMAILS` from the
`bearer-auth-allowed` ConfigMap — see [Managing access](#managing-access) below). `GOOGLE_TOKEN_AUDIENCE`
does **not** narrow that: it defaults to the gcloud CLI's *public* OAuth client id, which anyone's
`gcloud auth print-identity-token` mints, so it buys cross-*OAuth-client* replay protection and
nothing more — it rejects a token minted for a different client id (ADC's `764086051850-…`, a
project-owned client), but not one the same user handed to another service that documents this same
`gcloud auth print-identity-token` flow, since that token carries the identical `aud`. See
"Programmatic credentials: why the per-user API key, not the Google id_token" in
[docs/project-spec.md](docs/project-spec.md).

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

`MCP_API_KEY` is the deployment-wide *shared* secret: one value for everyone, not attributable to a
person and not revocable for one. Prefer your own key from
[Programmatic API access](#programmatic-api-access-per-user-api-key) above — the same per-user key
works here, in exactly this header, and covers both `/api` and `/mcp`.

### Managing access

The allow-list has a single source of truth in terraform, so both the browser and bearer-token paths
stay in step — `deploy.sh` renders it into the oauth2-proxy `--authenticated-emails-file` ConfigMap
and into the `bearer-auth-allowed` ConfigMap that results-api, mcp-server and chat-backend consume
via `envFrom`:

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

All services output structured JSON to stdout, automatically captured by GKE's fluentbit agent and sent to Cloud Logging. With `enable_log_sinks = true`, `terraform/logging.tf` also creates two Cloud Logging → BigQuery sinks: the `endpoint_access` usage logs of both results-api and db-api (stripped of variant, gene, phenotype etc. information), scoped to `k8s_container` resources in the `genetics` namespace, into `genetics_api_logs` — both services share the table `genetics_api_logs.stdout`, see `docs/project-spec.md` → Log sinks — and chat-backend container logs at severity ≥ INFO into `genetics_chat_logs`. `scripts/chat_usage_stats.sh` reports chat usage counts from the latter.

## Security

- Network policies source-scope **every** service. Rather than trusting the list that follows, re-derive it — `k8s/network-policies/` is the whole inventory and `scripts/test-network-policies.py` asserts the sandbox half of it. As it stands: db-api (8080) from chat-backend, mcp-server and sandbox; rag-service (8000) from chat-backend and mcp-server; results-api (4000) from auth-gateway, bff, chat-backend, mcp-server and sandbox; bff (5000), frontend (3000) and mcp-server (8080) only from auth-gateway; chat-backend (8000) from auth-gateway, results-api and mcp-server; sandbox (8080) from chat-backend and nothing else, and it is the one pod in the namespace with an Egress policy at all (db-api:8080 and results-api:4000, no `ipBlock`, no DNS). The monitor CronJob is admitted separately and additively by `monitor-policy.yaml`. auth-gateway (8080) is the only service reached from outside and the only one using an `ipBlock` — Google's LB/health-check ranges `35.191.0.0/16` and `130.211.0.0/22`; no node CIDR, because it is fronted by a NEG so the load balancer talks to pod IPs directly. The source nginx sees is always the GFE's own address in `35.191.0.0/16`, never the client's (that survives only in `X-Forwarded-For`), so client IPs cannot be filtered at this layer. See `docs/project-spec.md` → Security.
- The suite's own service containers — results-api, chat-backend, mcp-server, bff, db-api and both auth-gateway containers — run with `allowPrivilegeEscalation: false`, all capabilities dropped and nothing added back, and the `RuntimeDefault` seccomp profile; db-api, bff and auth-gateway additionally run as non-root (uid 10001 / 1000 / 101), and auth-gateway also sets `readOnlyRootFilesystem` with `emptyDir`s over `/var/cache/nginx` and `/tmp`. Running nginx's *master* as uid 101 rather than root is what let `CHOWN`/`SETUID`/`SETGID` go away — the worker was already unprivileged either way. auth-gateway also sets `automountServiceAccountToken: false`, so the internet-facing pod carries no ServiceAccount token; the namespace `default` SA it would otherwise mount has no RoleBinding anywhere in the cluster and no GCP identity, so this is defence-in-depth against a future grant rather than the closing of a live escalation path. Since `genetics-results-suite-5ho` the other five workloads that name no service account (bff, frontend, keycloak, oauth2-proxy, postgres) set it too, so **no** workload in the namespace now mounts the `default` token. Since `genetics-results-suite-d6n` the three CronJobs are hardened too, but each only to what its own image was measured to tolerate: `monitor` runs non-root (uid 1000, its image's own `USER`) with drop-`ALL`, `readOnlyRootFilesystem` and an `emptyDir` at `/tmp`; `analyze-conversations` drops `ALL` and stays root, with a new pod-level `fsGroup: 1032` — the same value and the same reason as chat-backend, since it writes the same `chat-data` SQLite files; `keycloak-postgres-backup` drops `ALL` and adds back `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID` and `SETUID`, which its run-time `apt-get install postgresql-client` needs and without which the install fails outright. A *different* five — the third-party and support workloads (frontend, oauth2-proxy, keycloak, postgres, rag-service) — are not hardened this way; the sandbox goes further still (non-root uid 65532, read-only rootfs, a dedicated KSA with no GCP identity, `automountServiceAccountToken: false`, `enableServiceLinks: false`, and one 512Mi `emptyDir` at `/scratch` as its only mount) — its manifest is `k8s/deployments/sandbox.yaml`, applied only when `ENABLE_SANDBOX=true`. See `docs/project-spec.md` → Security.
- Workload Identity provides read-only GCP access (BigQuery + GCS) without key files
- HTTPS enforced via FrontendConfig redirect
- Google-managed SSL certificates
