# Local development on a Google Cloud VM

How to run the whole suite from source on a single Ubuntu VM, in reload mode, and reach it
from a laptop over an SSH tunnel. Nothing here goes through Docker, Kubernetes or the
auth-gateway — services talk to each other over `localhost` and there is no oauth2-proxy,
so no login is needed. That also means this setup does **not** exercise any of the auth chain;
if that is what you are changing, see [step 8](#8-testing-an-auth-change-the-setup-above-does-not-exercise-it),
which runs the real gateway image locally.

## What runs where

| Port | Service | Repo | Reached by |
|------|---------|------|-----------|
| 2000 | results-api | `genetics-results-api` | BFF, chat-backend |
| 3000 | frontend (vite dev server) | `genetics-results-browser` | your laptop's browser |
| 4000 | chat-backend | `genetics-mcp-server` | browser (`VITE_CHAT_URL`) |
| 5000 | BFF | `genetics-results-browser` (`bff/`) | browser (`VITE_API_URL`) |
| 8080 | db-api (optional, BigQuery proxy) | `genetics-results-db` | chat-backend only |

Data flow: browser → vite `:3000` → BFF `:5000` → results-api `:2000`; the chat views bypass
the BFF and call `:4000` directly. db-api is only used server-side, so it needs no tunnel.

## Already set up? `scripts/dev-stack.sh` drives all five

Steps 1-6 are the from-scratch build-out. On a machine where the repos, venvs and
`node_modules` already exist, one script starts, stops and switches the whole stack
(`genetics-results-suite-r9e`):

```bash
./scripts/dev-stack.sh up                 # the worktree trees, db-api on genetics_dev
./scripts/dev-stack.sh up --tree main     # the main checkouts, db-api on genetics_results
./scripts/dev-stack.sh status             # port, health code, and WHICH TREE each pid runs from
./scripts/dev-stack.sh down               # stop all five
./scripts/dev-stack.sh logs chat-api      # tail -f
```

Switching back to the main checkouts on `master` is `down` then `up --tree main`, and
nothing else. The two trees are mutually exclusive by construction: both use the same five
ports, so `up` frees each port before it starts anything on it.

What the script is doing on your behalf, and why each piece matters:

- **It resolves the trees the way `sync-datasets.sh` does** — the siblings sit next to the
  **main** checkout (`~/suite/genetics-results-db`, …), never next to a worktree, so
  `--tree worktree` means `~/suite/<repo>/.claude/worktrees/<name>` for all four repos at
  once. The worktree name defaults to this checkout's own directory name (`DEV_WORKTREE`
  overrides; `SUITE_SIBLING_ROOT` overrides the root).
- **It stops whatever holds the port *if the holder is this suite's*, not what it started.**
  Resolution is from the listening socket (`ss`) to the process group, so it takes over
  servers started by hand in the tmux windows of step 6 — which is what the takeover
  requires — and one `kill` reaches the whole `npm` → `sh` → `node` tree rather than leaving
  `tsx watch` to respawn its child. But the socket only says *something* answers on `:3000`,
  and on a dev box that is as likely to be an unrelated vite or an unrelated `:8080`. So
  before signalling anything it checks the holder's `/proc/<pid>/cwd` and command line, and
  frees the port only when the process runs from this suite's copy of that service's repo
  (main checkout or any worktree under it) or names that repo on its command line. Anything
  else is printed — pid, cwd, argv — and left running; `up` then skips that service and
  exits non-zero rather than starting a second copy. `down --force` overrides the check when
  you really do mean "kill whatever is there".
- **It validates every selected tree before it frees the first port.** A missing `.venv` or
  `node_modules` discovered while starting service four would leave the first three on the
  new tree, one killed, and the last two still serving the old one — half on each, silently.
  All five directory, venv and `node_modules` checks run first; a failure starts and stops
  nothing.
- **`up` exits non-zero if any service failed** to answer its health endpoint or had its
  port refused, so a script can tell a good stack from a broken one.
- **The gitignored config stays in the main checkout.** `genetics-mcp-server/.env` (the
  `ANTHROPIC_API_KEY` and the `ANALYZE_*` models) is read from the main checkout by path —
  `MCP_ENV_FILE` — and exported into the chat-backend subshell only. Nothing is copied into
  a worktree, nothing lands on a command line where `ps` would show it, and nothing is
  echoed. The frontend's three `VITE_*` values are passed as environment variables instead
  of a file, because vite merges prefixed `process.env` over whatever `.env` files it
  loaded; a worktree therefore needs no `.env.local` of its own.
- **It sets `SANDBOX_URL=http://127.0.0.1:8081` explicitly.** The client's own default is
  `127.0.0.1:8080`, which on this machine is **db-api** — chat-backend would post code
  executions at the BigQuery proxy (`genetics-results-suite-6um`). 8081 is what
  `scripts/run-sandbox-local.sh` publishes.
- **`status` reads `DATASET_ID` out of `/proc/<pid>/environ`**, because `/health` does not
  report it and an **unset** `DATASET_ID` silently means production (`api/main.py` defaults
  it to `genetics_results`). That default is the failure the dev dataset exists to remove,
  so it is reported as `PRODUCTION` rather than as a blank.

**`genetics-mcp-server/.env` is where every other chat-backend variable belongs.** The
script sets only `BIGQUERY_API_URL`, `GENETICS_API_URL`, `DEFAULT_MODEL`,
`EXTERNAL_MCP_SERVERS`, `REQUIRE_AUTH` and `SANDBOX_URL`, and each as a `${VAR:-default}` —
so a value already in `MCP_ENV_FILE` **wins**, because the file is sourced first. Anything
the script does not name is passed through untouched. Put these there rather than in the
`~/genie.env` of [step 5](#5-environment-variables-that-are-not-in-the-repos), which
`dev-stack.sh` never reads:

| Variable | Why it matters under `dev-stack.sh` |
|---|---|
| `EXTERNAL_MCP_SERVERS` | the script's default is **opentargets only**. A hand-started stack that also had, say, a private gnomAD Cloud Run URL loses it unless the full comma-separated list is in the `.env` |
| `PERPLEXITY_API_KEY`, `TAVILY_API_KEY` | literature and web search are simply absent without them; nothing warns |
| `CHAT_HISTORY_DB`, `LLM_CONFIG_DB`, `DOWNLOAD_STORAGE_PATH`, `ATTACHMENT_STORAGE_PATH` | defaults are `/mnt/disks/data/{chat_history.db,llm_config.db,downloads,attachments}`. On a machine that has that disk they are correct and hold the real chat history; on a fresh VM the directory does not exist. If you followed step 5's `$HOME/data` block and then switch to `dev-stack.sh`, the backend opens the **`/mnt/disks/data` database instead** and every past conversation appears to have vanished — put the same four `$HOME/data` paths in the `.env` and they do not |

`SANDBOX_TOKEN_SIGNING_KEY` and `INTERNAL_API_SECRET` are the exception to "put it in the
`.env`": the script **generates** them once into `DEV_STACK_RUN_DIR`
(`~/.cache/genetics-dev-stack`) and exports them to db-api, results-api and chat-backend —
unconditionally, whatever `SANDBOX_ENABLED` is set to — so the minter and both verifiers agree.
Without them both verifiers resolve **no sandbox principal at all** and serve the SDK with no
per-execution accounting (`genetics-results-suite-0lf`): that follows from the two credentials
and from nothing else, since neither verifier reads `SANDBOX_ENABLED`. They have to be
stable across restarts — rotating the key invalidates a token minted seconds earlier — and they
must not land in a repo. Setting either variable yourself still wins.

`SANDBOX_ENABLED` itself defaults to **`false`**: `dev-stack.sh` starts no sandbox
supervisor, so a `true` default would offer `run_analysis` with nothing behind it and its
failure would misreport as a transient `SandboxUnavailable`
(genetics-results-suite-4h6.86). Start a supervisor with `scripts/run-sandbox-local.sh`
and set `SANDBOX_ENABLED=true` yourself (in the environment or in `MCP_ENV_FILE`) to
exercise the sandboxed path locally; if you do, `dev-stack.sh up` probes `SANDBOX_URL/health`
once and warns loudly if nothing answers `"status": "ok"` there.

**Provisioning `INTERNAL_API_SECRET` — not the `SANDBOX_ENABLED` default above — changes
what an unauthenticated local request can do, and it is not a subtlety.**
db-api's auth middleware is fail-open on an **empty** `INTERNAL_API_SECRET` (`api/main.py`
warns `every endpoint is reachable without authentication` and lets everything through), so
simply having the variable set flips it to **enforcing**. Measured 2026-08-17 against the local
stack:

| request | before `dev-stack.sh` provisioned the secret | now |
|---|---|---|
| `curl -X POST 127.0.0.1:8080/query -d '{"query":"SELECT 1"}'` | served | **401** |
| `curl 127.0.0.1:8080/openapi.json` | served | **401** |
| `curl 127.0.0.1:8080/health` | 200 | 200 (unauthenticated by design) |

Any curl one-liner, notebook or script that talked to the local db-api without a credential
stops working the first time you bring the stack up after this change. That is the cluster's
behaviour arriving locally rather than a regression — the point of provisioning the secrets is
that the local stack authenticates the way the deployed one does — but it is a real change to
what a developer's existing tooling can do, so: send
`Authorization: Bearer $(cat ~/.cache/genetics-dev-stack/internal-api-secret)`. Note that
`SANDBOX_ENABLED=false` does **not** get the old behaviour back — it is `INTERNAL_API_SECRET`
alone that db-api gates on. Bringing the stack up with `INTERNAL_API_SECRET= SANDBOX_ENABLED=`
does, at the cost of the whole sandbox token path (see `docs/code-execution-security.md` §2).

It starts services in dependency order and waits for each health endpoint. results-api gets
a 10-minute budget because it verifies every configured tabix file against GCS before it
serves (~90 s warm, longer cold); everything else answers in seconds.

### The dev dataset

`--tree worktree` defaults db-api to `DATASET_ID=genetics_dev` — `phewas-development.genetics_dev`,
region `europe-west1`, built by `genetics-results-suite-g08`. Since **2026-08-18 it is
FULL SIZE**: all 15 tables carry every production row — 755,813,602 rows / 136.69 GB,
each table's count identical to its `genetics_results` counterpart. (Production's 1.1 B
total is larger only because it also holds the three `credible_sets_exp_*` experiment
tables, 115 M rows each, which dev deliberately does not have — they are `4h6.18`'s and
`genetics-results-suite-4ci` proposes dropping them.)

- **Any gene smoke-tests now**, on any chromosome — all 23 are present. `APOE` (chr19)
  returns 2,758 credible sets and 17,059 gene-burden rows. Before the widening it
  returned zero, and the old advice to smoke-test only with a chr22 gene like `SMARCB1`
  no longer applies. Zero rows for a common gene now means a broken stack, not a subset.
- Two views differ from production on purpose: `hla_associations_v` has different column
  names (`genetics-results-suite-94c`) and `credible_sets` has a different storage layout
  (`genetics-results-suite-eyg`, consumer-transparent through `credible_sets_v`). The other
  13 are byte-identical.
- **`genetics_dev` is the FIXED state for HLA, not the broken one, and the failing
  combination is the mixed one.** `genetics_dev.hla_associations_v` is built from the
  committed schemas and carries the house spelling (`mlog10p`, `se`, `af`, `af_cases`,
  `af_controls`) that the worktree's `get_hla_by_allele` selects;
  `genetics_results.hla_associations_v` still carries FinnGen's native spelling (`mlogp`,
  `sebeta`, `af_alt`, …) that `master`'s MCP executor selects, because
  `genetics-results-suite-94c`'s expand phase has not been applied to production. So `up`
  works, `up --tree main` works, and an HLA query fails only when worktree code runs against
  `genetics_results` — the `--dataset genetics_results` override below. See
  [HLA column rename rollout](project-spec.md#hla-column-rename-rollout-hla_associations_v).
- Only db-api reads it. results-api serves tabix files from GCS and is identical under both
  trees; the BFF and frontend name no dataset at all.
- `--dataset` overrides either way, and `up --tree worktree --dataset genetics_results`
  points the worktree branches at production if that is what you actually want.

Not to be confused with `genetics_results_dev`, the *rehearsal* dataset of
[docs/bigquery-dev-dataset.md](bigquery-dev-dataset.md) — a zero-copy clone created and torn
down around a specific DDL change. `genetics_dev` is a persistent full-size copy for
running the stack.

**Reloading it: `TRUNCATE` + `INSERT … SELECT`, never `CREATE OR REPLACE TABLE`.** Dev is
not a copy of production and a CTAS or a `bq cp`/clone would silently destroy what makes
it dev. Measured on 2026-08-18, **no dev table's schema is identical to its production
counterpart**: all 15 carry column descriptions and `NOT NULL` (`REQUIRED`) modes that
production lacks entirely — production's columns are without exception `NULLABLE` and
undescribed. (Partitioning is *not* a difference: production range-partitions on `chr`
the same way, and clusters identically on every table except `credible_sets`, whose keys
`eyg` swapped to `data_type, resource, variant, pos`.) A CTAS inherits neither the
partitioning nor the clustering nor the descriptions, and flattens every column to
`NULLABLE`, which would flip db-api's `/schema` from `REQUIRED`; a clone would overwrite
the dev schema with production's outright. Truncating the existing table and inserting
into it with an **explicit column list** (read from
`bq show --schema` so column order cannot silently shift) preserves all of it. The
2026-08-18 full load did exactly this for all 15 tables: 180 s wall clock, 133.75 GiB
scanned, USD 0.65, and production verified byte-identical in table membership, row counts
and `size_bytes` before and after.

`credible_sets` is the one table needing a transform rather than a straight column copy:
dev's base table **stores** `variant` and `resource`, production's only computes them in
`credible_sets_v` (`genetics-results-suite-eyg`). Take the two expressions verbatim from
production's view — `CONCAT(chr,':',pos,':',ref,':',alt)` and its 11-branch resource
`CASE` — rather than reinventing them. `hla_associations` needs no transform: its base
table is column-identical to production and the renames live only in dev's view.

**Running a second copy:** a dev VM often already has the suite running from the main clones on
exactly these ports. If you are bringing up a second copy (from a worktree, a branch, a second
checkout), pick a disjoint port for every service and set `GENETICS_API_URL`, `BFF_PORT`,
`BIGQUERY_API_URL`, `CORS_ORIGINS` and `.env.local` to match — the numbers above are the
defaults, not a requirement. One verification run used 12000/12001, 15000-15003, 18000/18001 and
18080; any free block works. Afterwards, confirm the original five ports are still listening
(`ss -ltnp | grep -E ':(2000|3000|4000|5000|8080)'`) so you know you did not disturb the copy
someone else is using.

## 1. Create the VM

```bash
export GCP_PROJECT=your-project-name
export ZONE=us-central1-a
export VM_NAME=genetics-dev
export SA_NAME=genetics-suite-gke
```

```bash
gcloud compute instances create $VM_NAME \
  --project=$GCP_PROJECT --zone=$ZONE \
  --machine-type=e2-standard-2 \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --boot-disk-size=100GB \
  --service-account=$SA_NAME@$GCP_PROJECT.iam.gserviceaccount.com \
  --scopes=cloud-platform
```

Add `--subnet=SUBNETWORK_NAME` to the above command if there's no default network in the Google project or you don't want to use it.

`--scopes=cloud-platform` matters: the VM service account is the credential the servers use
for GCS and BigQuery. That service account still needs read access to the data buckets and
the BigQuery dataset.

## 2. System packages

```bash
gcloud compute ssh $VM_NAME --project=$GCP_PROJECT --zone=$ZONE --tunnel-through-iap

sudo apt-get update && sudo apt-get install -y \
  git curl tmux build-essential autoconf sqlite3 \
  libz-dev libbz2-dev liblzma-dev libcurl4-openssl-dev libssl-dev libdeflate-dev
```

### tabix with GCS support (required by results-api)

results-api shells out to `tabix` on `gs://` paths, so htslib must be built with libcurl and
GCS enabled (same flags as the production image):

```bash
HTSLIB_VER=1.22.1
curl -LO https://github.com/samtools/htslib/releases/download/${HTSLIB_VER}/htslib-${HTSLIB_VER}.tar.bz2
tar -xjf htslib-${HTSLIB_VER}.tar.bz2 && cd htslib-${HTSLIB_VER}
./configure --enable-libcurl --enable-gcs --with-libdeflate && make -j4 && sudo make install
sudo ldconfig && cd .. && tabix --version
```

### uv and node

uv installs the right Python per repo, so no pyenv/system python is needed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
```

Node must be >= 22.12 (vite 8); Ubuntu 24.04's `apt` node is too old
(check nvm docs for the current version - it's different from node version):

```bash
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.6/install.sh | bash
source ~/.nvm/nvm.sh
nvm install 22
```

## 3. Clone the repos

```bash
mkdir -p ~/suite && cd ~/suite
for r in genetics-results-suite genetics-results-api genetics-results-browser \
         genetics-mcp-server genetics-results-db; do
  git clone https://github.com/fulltiltgenomics/$r.git
done
```

### `configs/datasets.yaml` is NOT in a fresh clone

`configs/datasets.yaml` is committed **only in this repo** (`genetics-results-suite`). Both
`genetics-results-api` and `genetics-results-db` gitignore it (see their `.gitignore`), so a
fresh clone of either has no `configs/datasets.yaml` and the server aborts at startup. Populate
it before step 6:

```bash
~/suite/genetics-results-suite/scripts/sync-datasets.sh
```

That script resolves its targets from the **git common dir** — the main checkout's `.git` even
when the script is invoked from `<repo>/.claude/worktrees/<name>` — so it works the same from a
worktree and from a normal clone, and it prints the resolved root as its first line. A sibling
that is not cloned here prints `SKIP:` and the run still exits 0; only an unresolvable sibling
root, or a directory whose `pyproject.toml` does not name that repo, is an `ERROR:` and exit 1.
If your layout does not put the siblings next to the main checkout, point it at them:

```bash
SUITE_SIBLING_ROOT=~/elsewhere ~/suite/genetics-results-suite/scripts/sync-datasets.sh
```

## 4. Install dependencies

```bash
cd ~/suite/genetics-results-api   && uv sync                                # python 3.13
cd ~/suite/genetics-mcp-server    && uv sync                                # python 3.12
cd ~/suite/genetics-results-db    && uv venv && uv pip install -r pyproject.toml
cd ~/suite/genetics-results-browser && npm install
```

The `npm install` is a hard prerequisite, not an optimisation: both the BFF (`bff/`, run with
`tsx watch`) and the vite frontend live in `genetics-results-browser`, and neither start command
bootstraps `node_modules`. Without it both windows in step 6 fail immediately.

## 5. Environment variables that are not in the repos

None of the values below are committed anywhere — set them yourself. Put them in
`~/genie.env` and `source` it in every tmux window.

```bash
cat > ~/genie.env <<'EOF'
# --- GCP ---
export GCP_PROJECT=your-project-name
# Only if the VM service account is not the identity you want. Either a key file:
# export GOOGLE_APPLICATION_CREDENTIALS=$HOME/keys/sa-key.json
# ...or run once: gcloud auth application-default login

# --- results-api (:2000) ---
export CONFIG_PROFILE=daly      # or finngen — must match the data you have access to
export DEPLOY_ENV=local
export RELOAD=1                    # auto-reload

# --- BFF (:5000) ---
export GENETICS_API_URL=http://localhost:2000/api
export BFF_PORT=5000

# --- chat-backend (:4000) ---
export ANTHROPIC_API_KEY=sk-ant-...          # REQUIRED for chat
export PERPLEXITY_API_KEY=pplx-...           # optional, literature search
export TAVILY_API_KEY=tvly-...               # optional, web search
export BIGQUERY_API_URL=http://localhost:8080
export DEFAULT_MODEL=claude-opus-5
export EXTERNAL_MCP_SERVERS=https://mcp.platform.opentargets.org
export REQUIRE_AUTH=false                     # no oauth2-proxy locally
# the client's default is 127.0.0.1:8080, which is db-api here — always set this
# explicitly if you run the sandbox (scripts/run-sandbox-local.sh publishes 8081):
export SANDBOX_URL=http://127.0.0.1:8081
# defaults point at /mnt/disks/data, which a fresh VM does not have:
export CHAT_HISTORY_DB=$HOME/data/chat_history.db
export LLM_CONFIG_DB=$HOME/data/llm_config.db
export DOWNLOAD_STORAGE_PATH=$HOME/data/downloads
export ATTACHMENT_STORAGE_PATH=$HOME/data/attachments

# --- db-api (:8080) ---
export PROJECT_ID=$GCP_PROJECT
export DATASET_ID=genetics_results   # genetics_dev for the full-size dev copy — see "The dev dataset".
                                     # UNSET means genetics_results too, i.e. PRODUCTION
export PORT=8080
EOF

mkdir -p ~/data/downloads ~/data/attachments
```

Notes:

- **Credentials**: the servers use Application Default Credentials. On the VM that is the VM
  service account; results-api mints and refreshes `GCS_OAUTH_TOKEN` from it by itself for the
  `tabix` subprocesses, so do not export it manually.
- `INTERNAL_API_SECRET` / `GENETICS_API_TOKEN` / `MCP_API_KEY` are production-only. Leave them
  unset locally — results-api and db-api then run without bearer auth.

### Frontend env (gitignored, must be created)

Vite always loads `.env.local`, which is not in the repo:

```bash
cat > ~/suite/genetics-results-browser/.env.local <<'EOF'
VITE_TARGET=public
VITE_API_URL=http://localhost:5000/api
VITE_CHAT_URL=http://localhost:4000/chat
EOF
```

Both URLs are `localhost` as seen **by the laptop browser**, which is why the tunnel forwards
4000 and 5000 as well as 3000.

## 6. Run the servers (tmux, all in reload mode)

```bash
tmux new -s suite
# in each window: source ~/genie.env   (Ctrl-b c opens a new window)
```

| Window | Commands |
|---|---|
| results-api | `cd ~/suite/genetics-results-api && uv run python run_server.py 2000` |
| chat-backend | `cd ~/suite/genetics-mcp-server && uv run python -m genetics_mcp_server.chat_api --port 4000` |
| BFF | `cd ~/suite/genetics-results-browser && npm run bff:dev` |
| frontend | `cd ~/suite/genetics-results-browser && npm run dev` |
| db-api (optional) | `cd ~/suite/genetics-results-db && uv run uvicorn api.main:app --host 0.0.0.0 --port 8080 --reload` |

All five reload on source changes (`RELOAD=1` for results-api, `reload=True` in the chat API's
`__main__`, `tsx watch` for the BFF, HMR for vite, `--reload` for db-api). Start results-api
first: it verifies every configured data file is reachable before serving and aborts on failure,
which takes a while on the first run.

Detach with `Ctrl-b d`, reattach with `tmux attach -t suite`.

## 7. Connect from laptop

```bash
gcloud compute ssh VM_NAME --project=YOUR_PROJECT --zone=YOUR_ZONE --tunnel-through-iap \
  -- -L 2000:localhost:2000 -L 3000:localhost:3000 -L 4000:localhost:4000 -L 5000:localhost:5000
```

Then open <http://localhost:3000>. Nothing but port 22 is exposed — the app ports are reached
through the SSH tunnel only. Drop `--tunnel-through-iap` if you allowed 22 from your own IP
instead. The same flags apply to the `gcloud compute ssh` in step 2.

Quick checks, on the VM or through the tunnel:

```bash
curl localhost:2000/healthz          # results-api
curl localhost:5000/healthz          # BFF
curl localhost:4000/healthz          # chat-backend
curl localhost:8080/health           # db-api
```

## 8. Testing an auth change: the setup above does NOT exercise it

Everything above deliberately skips the auth chain — no auth-gateway, no oauth2-proxy, requests
arrive at the BFF, results-api and chat-backend straight from `localhost`. That is fine for data
and UI work, and wrong for anything touching identity, because **`X-Goog-Authenticated-User-Email`
originates in auth-gateway's nginx and nowhere else**: `k8s/deployments/auth-gateway.yaml` takes
`$email` from the `auth_request /oauth2/auth` subrequest's `X-Auth-Request-Email` response header
and sets `X-Goog-Authenticated-User-Email "accounts.google.com:$email"` on the way to the BFF,
chat-backend and frontend, clearing it to `""` on the bearer-token path. The bearer divert itself
(`if ($http_authorization ~* "^Bearer ")` in `location /api/`, which jumps to `@api_bearer` via
`error_page 418`) is likewise a gateway-only behaviour, and it is the partition any backend guard
has to agree with. Run a guard change against the doc's default setup and you have tested the
guard's default branch, not the code under test.

To actually exercise it, run the real gateway locally. This recipe was verified end to end on
this VM:

1. **Real image, host network.** `docker run --network host nginx:1.27-alpine`. Not a
   hand-written nginx.conf — the point is to test the shipped one.
2. **Extract the config with a YAML parser**, not `sed`: it is the `nginx.conf` key of the
   `auth-gateway-config` ConfigMap in `k8s/deployments/auth-gateway.yaml`, a block scalar whose
   body contains `#`, `$` and `{}` that line-based extraction mangles.
3. **Render pass 1 — `deploy.sh`'s whitelist, exactly.** Copy the `envsubst '...'` argument from
   the deployments loop in `scripts/deploy.sh` verbatim. It is a whitelist, and it deliberately
   omits `${INTERNAL_API_SECRET}` — confirmed: that placeholder survives pass 1 untouched, which
   is what keeps the secret out of the ConfigMap.
4. **Render pass 2 — the `render-config` initContainer, verbatim.** Take `args[0]` of that
   initContainer as-is and run it inside the image with `INTERNAL_API_SECRET` set. Do not
   paraphrase it: it carries the character whitelist that rejects a secret nginx would
   reinterpret inside a header string, and the `nginx -t` that catches a broken render. Keep the
   `nginx -t`; it is the step that tells you the template still parses.
5. **The only local edits needed** are the ones that assume a cluster: the cluster-DNS
   `proxy_pass` upstreams (six distinct services — oauth2-proxy, mcp-server, results-api, bff,
   chat-backend, frontend — across ten `proxy_pass` directives) repointed at your local ports,
   the `resolver kube-dns.kube-system.svc.cluster.local` line (no kube-dns locally), and
   `listen 8080` moved to a free port. That was 24 changed lines in the verified run. Everything
   else — the `auth_request` chain, the bearer divert, the `X-Internal-Auth` headers, the log
   redaction map — runs as shipped.
6. **Stub oauth2-proxy** with any responder that answers the `/oauth2/auth` subrequest with `202`
   and an `X-Auth-Request-Email: you@example.com` header. nginx reads only that header, so the
   stub is enough to drive the whole authenticated path; return `401` from it to exercise the
   `@oauth2_login` redirect instead.

Point the browser (or `curl`) at the gateway port rather than at the BFF/chat-backend ports and
the requests now carry the same headers production sends.

## What this VM cannot verify

Two things look testable here and are not. Do not record either as verified from a local run.

- **NetworkPolicy enforcement.** There is no local Kubernetes here, and standing one up does not
  help: kind/minikube's default CNI does not enforce NetworkPolicy at all, so policies appear to
  "pass" while nothing is enforcing them. Worse, the production cluster runs `ADVANCED_DATAPATH`
  (Dataplane V2), and the finding that kubelet probes bypass NetworkPolicy is specific to that
  datapath — a local Calico or Cilium answer would be a different implementation's behaviour and
  would mislead rather than inform. The offline parser `scripts/test-network-policies.py` checks
  the policy *files*; live enforcement is deferred to the deploy window under
  `genetics-results-suite-4h6.26`.
- **gVisor isolation.** `runsc` is not installed on this VM, so the sandbox image runs under
  plain `runc` locally. Building the image and seeing the ten checks in
  `sandbox/build-checks.py` pass is genuine
  verification **of the image**; it says nothing about the isolation boundary, which is the
  `gvisor` RuntimeClass plus seccomp on the cluster.

## Troubleshooting

| Symptom | Cause |
|---|---|
| results-api or db-api aborts complaining about `configs/datasets.yaml` | the file is gitignored in both service repos — sync or copy it (step 3) |
| `sync-datasets.sh` prints `SKIP: <repo> is not checked out on this machine` | that sibling really is not cloned here; clone it or ignore the line (it exits 0) |
| `sync-datasets.sh` exits 1 with `ERROR: cannot resolve where the sibling repos live` | it was run from a directory that is not a git checkout; set `SUITE_SIBLING_ROOT` to the directory holding the sibling repos |
| `sync-datasets.sh` exits 1 with `ERROR: <path> exists but is not the <repo> repo` | a directory of the right name sits where the sibling should be but its `pyproject.toml` does not name that repo — usually a stale or partial clone, or `SUITE_SIBLING_ROOT` pointing one level off. The script refuses to copy into it; point it at the real checkout |
| `sync-datasets.sh` exits 1 with `ERROR: SUITE_SIBLING_ROOT is set to '<path>', which is not a directory` | the override is a typo, a file, or a path that does not exist; unset it to fall back to the git-common-dir resolution |
| BFF or vite window exits instantly | `npm install` never ran in `genetics-results-browser` (step 4) |
| An auth/identity change behaves differently locally than in the cluster | the default setup has no auth-gateway, so `X-Goog-Authenticated-User-Email` is never produced and the bearer divert never runs (step 8) |
| results-api aborts at startup listing `gs://` files | ADC identity has no read access to the data bucket, or `CONFIG_PROFILE` points at data you cannot see |
| `tabix: ... unknown URL scheme` / GCS errors | htslib built without `--enable-libcurl --enable-gcs` |
| Frontend loads but tables are empty | BFF or results-api not running; check `VITE_API_URL` in `.env.local` |
| Frontend ignores `.env.dev` | vite's default mode is `development`, which loads `.env`/`.env.development`/`.env.local` — **not** `.env.dev`, which needs `--mode dev`. `.env.dev` is tracked; `.env.local` is gitignored. Exported `VITE_*` variables beat both |
| Every by-gene query returns zero rows | Not the dataset: `genetics_dev` has been full-size since 2026-08-18, so a common gene returning nothing is a real fault. Confirm the dataset with `dev-stack.sh status`, then look at db-api itself |
| Code execution posts at db-api, or "sandbox" answers look like SQL errors | `SANDBOX_URL` is unset and its default is `127.0.0.1:8080`, which is db-api here; set `http://127.0.0.1:8081` (`genetics-results-suite-6um`) |
| An HLA query fails with "unrecognized name: mlog10p" (or `se`, `af_cases`) | worktree code is pointed at `genetics_results` (`up --dataset genetics_results`). Production's `hla_associations_v` still has FinnGen's native column names — `genetics-results-suite-94c`'s expand phase has not been applied there. `genetics_dev` and `--tree main` both work; only the mixed combination fails |
| Chat page errors, rest of app fine | chat-backend down, or `ANTHROPIC_API_KEY` unset |
| Chat answers but BigQuery tools fail | db-api not running or `BIGQUERY_API_URL` unset |
| db-api logs `bigquery.tables.get` / `bigquery.jobs.create` denied | the VM runs as the default compute service account, which has no roles. Attach one with `roles/bigquery.dataViewer` + `roles/bigquery.jobUser` (`gcloud compute instances set-service-account`, VM stopped) and restart the servers |
| Browser CORS errors | the origin must be `http://localhost:3000` (the chat backend's `CORS_ORIGINS` default); a different port needs `CORS_ORIGINS` set |
| `networks/default ... cannot be found` on create | custom-mode VPC — pass `--subnet=<subnet>` |
| `Network interface must specify a subnet` | `--network` was given without `--subnet` |
| `ssh: connect ... timed out` | no firewall rule for tcp:22 on the VPC (see step 1) |
