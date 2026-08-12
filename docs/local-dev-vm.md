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
# defaults point at /mnt/disks/data, which a fresh VM does not have:
export CHAT_HISTORY_DB=$HOME/data/chat_history.db
export LLM_CONFIG_DB=$HOME/data/llm_config.db
export DOWNLOAD_STORAGE_PATH=$HOME/data/downloads
export ATTACHMENT_STORAGE_PATH=$HOME/data/attachments

# --- db-api (:8080) ---
export PROJECT_ID=$GCP_PROJECT
export DATASET_ID=genetics_results
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
| Chat page errors, rest of app fine | chat-backend down, or `ANTHROPIC_API_KEY` unset |
| Chat answers but BigQuery tools fail | db-api not running or `BIGQUERY_API_URL` unset |
| db-api logs `bigquery.tables.get` / `bigquery.jobs.create` denied | the VM runs as the default compute service account, which has no roles. Attach one with `roles/bigquery.dataViewer` + `roles/bigquery.jobUser` (`gcloud compute instances set-service-account`, VM stopped) and restart the servers |
| Browser CORS errors | the origin must be `http://localhost:3000` (the chat backend's `CORS_ORIGINS` default); a different port needs `CORS_ORIGINS` set |
| `networks/default ... cannot be found` on create | custom-mode VPC — pass `--subnet=<subnet>` |
| `Network interface must specify a subnet` | `--network` was given without `--subnet` |
| `ssh: connect ... timed out` | no firewall rule for tcp:22 on the VPC (see step 1) |
