# Local development on a Google Cloud VM

How to run the whole suite from source on a single Ubuntu VM, in reload mode, and reach it
from a laptop over an SSH tunnel. Nothing here goes through Docker, Kubernetes or the
auth-gateway — services talk to each other over `localhost` and there is no oauth2-proxy,
so no login is needed.

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

`configs/datasets.yaml` is committed in `genetics-results-api` and
`genetics-results-db`. If the canonical copy in this repo is newer or
unsure, refresh them:
`./genetics-results-suite/scripts/sync-datasets.sh`.

## 4. Install dependencies

```bash
cd ~/suite/genetics-results-api   && uv sync                                # python 3.13
cd ~/suite/genetics-mcp-server    && uv sync                                # python 3.12
cd ~/suite/genetics-results-db    && uv venv && uv pip install -r pyproject.toml
cd ~/suite/genetics-results-browser && npm install
```

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

## Troubleshooting

| Symptom | Cause |
|---|---|
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
