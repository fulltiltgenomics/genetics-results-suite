#!/bin/bash
set -euo pipefail

# Build and run the sandbox image in plain Docker on a developer machine
# (genetics-results-suite-4h6.40). The SAME image, the same entrypoint and the same
# supervisor the pod runs — the local/pod difference is a deployment detail, never a second
# code path, so nothing here changes what the program does. chat-backend's client
# (genetics-results-suite-4h6.47) holds one base URL and behaves identically against this
# container and against the Service.
#
# Usage:
#   scripts/run-sandbox-local.sh              build the image, (re)start the container, wait
#                                             for /health, print the fidelity report
#   scripts/run-sandbox-local.sh --no-build   restart the existing image (seconds, not minutes)
#   scripts/run-sandbox-local.sh --test       ... then run scripts/test-supervisor.py --container
#   scripts/run-sandbox-local.sh --stop       stop and remove the container
#   scripts/run-sandbox-local.sh --logs       follow the container's stdout (the audit stream
#                                             lands here too — see section 2, "The audit stream")
#   scripts/run-sandbox-local.sh --regen      regenerate sandbox/schema and sandbox/stubs from
#                                             the staged SDK instead of only checking them
#
# Environment:
#   MCP_SERVER_DIR   genetics-mcp-server checkout holding src/genetics_mcp_server/sdk.
#                    Auto-resolved; see below. NOT cloned from GitHub the way
#                    scripts/build.sh does, because the point of this script is to run what
#                    is in the working tree.
#   HOST_PORT        published loopback port (default 8081). NOT 8080: the local db-api
#                    already holds that (genetics-results-suite-r9e). The CONTAINER port
#                    stays 8080 so the manifest and the local run agree.
#   GENETICS_API_URL, BIGQUERY_API_URL  defaults point at host.docker.internal, i.e. the
#                    local results-api (:2000) and db-api (:8080) that scripts/dev-stack.sh
#                    starts — NOT the manifest's cluster ports, where results-api is :4000
#                    and a local :4000 is chat-api. The manifest's values are cluster FQDNs
#                    pinned by hostAliases and resolve to nothing here.
#   SANDBOX_RETENTION_S  shorten the artifact retention deadline so a test can watch it
#                    expire. Unset in a normal run, which leaves the supervisor's 300s.
#   SANDBOX_IMAGE    image tag to build/run (default genetics-sandbox:local). Deliberately
#                    not $REGISTRY/sandbox:latest — nothing here pushes, and a local build
#                    must not be mistakable for the image the cluster pulls.
#   SANDBOX_DOCKER_RUNTIME  force a runtime (e.g. runsc). Auto-detected when present.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SANDBOX_DIR="${REPO_ROOT}/sandbox"

IMAGE="${SANDBOX_IMAGE:-genetics-sandbox:local}"
NAME="${SANDBOX_CONTAINER_NAME:-genetics-sandbox-local}"
HOST_PORT="${HOST_PORT:-8081}"
# :2000, NOT the manifest's :4000. In the cluster results-api's Service port is 4000; on a
# developer machine scripts/dev-stack.sh puts results-api on :2000 and CHAT-API on :4000, so
# the cluster number pointed the SDK at chat-backend, which answers 404 on /api and never
# looks like an auth or a data problem (measured 2026-08-17, genetics-results-suite-4h6.49).
GENETICS_API_URL="${GENETICS_API_URL:-http://host.docker.internal:2000/api}"
BIGQUERY_API_URL="${BIGQUERY_API_URL:-http://host.docker.internal:8080}"
# Empty by default, i.e. the supervisor's own 300s. Set only to make the retention deadline
# observable in a test run; scripts/test-e2e-local.py --retention-s must be given the same
# number, because nothing on the wire exposes it.
SANDBOX_RETENTION_S="${SANDBOX_RETENTION_S:-}"

DO_BUILD=1
DO_TEST=0
REGEN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) DO_BUILD=0 ;;
    --test) DO_TEST=1 ;;
    --regen) REGEN=1 ;;
    --stop)
      # `docker stop`, not `rm -f`: the container is started with --stop-timeout 130, so this
      # is the local form of terminationGracePeriodSeconds — SIGTERM, the supervisor drains,
      # and the in-flight execution is reaped and its directory wiped before SIGKILL. `rm -f`
      # SIGKILLs immediately and would silently skip exactly the sequence the 130s buys.
      if docker stop "${NAME}" >/dev/null 2>&1; then
        docker rm "${NAME}" >/dev/null 2>&1 || true
        echo "stopped and removed container ${NAME}"
      else
        docker rm -f "${NAME}" >/dev/null 2>&1 && echo "removed container ${NAME}" || echo "no container ${NAME}"
      fi
      exit 0
      ;;
    --logs) exec docker logs -f "${NAME}" ;;
    -h | --help) sed -n '3,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

# --------------------------------------------------------------------------------------
# The SDK. sandbox/Dockerfile pip-installs it from ./.sdk-src and a bare `docker build
# sandbox/` fails without it, by design.
# --------------------------------------------------------------------------------------

resolve_mcp_dir() {
  if [ -n "${MCP_SERVER_DIR:-}" ]; then
    echo "${MCP_SERVER_DIR}"
    return
  fi
  # In a worktree the sibling repo is usually checked out under the SAME worktree name —
  # this is the resolve-into-the-main-checkout class scripts/check-worktree-paths.sh
  # exists for, so try the matching worktree BEFORE the main checkout rather than
  # silently building against master (which does not carry the SDK at all).
  local common main_root suite name
  # --path-format=absolute: without it a plain checkout answers the RELATIVE string `.git`,
  # which the `cd` below then resolves against the CALLER'S cwd — so running this from
  # anywhere but the repo root failed with "no checkout found" and blamed MCP_SERVER_DIR.
  common="$(git -C "${REPO_ROOT}" rev-parse --path-format=absolute --git-common-dir 2>/dev/null \
            || git -C "${REPO_ROOT}" rev-parse --git-common-dir)"
  case "${common}" in /*) ;; *) common="${REPO_ROOT}/${common}" ;; esac
  common="$(cd "${common}" && pwd)"
  main_root="$(dirname "${common}")"
  suite="$(dirname "${main_root}")"
  name="$(basename "${REPO_ROOT}")"
  for candidate in \
    "${suite}/genetics-mcp-server/.claude/worktrees/${name}" \
    "${suite}/genetics-mcp-server"; do
    if [ -d "${candidate}/src/genetics_mcp_server/sdk" ]; then
      echo "${candidate}"
      return
    fi
  done
  echo ""
}

if [ "${REGEN}" = "1" ] && [ "${DO_BUILD}" = "0" ]; then
  echo "ERROR: --regen has nothing to do with --no-build. Regeneration reads the SDK staged"
  echo "       during the build; skipping the build skips the staging." >&2
  exit 2
fi

if [ "${DO_BUILD}" = "1" ]; then
  MCP_DIR="$(resolve_mcp_dir)"
  if [ -z "${MCP_DIR}" ]; then
    echo "ERROR: no genetics-mcp-server checkout with src/genetics_mcp_server/sdk was found."
    echo "       Set MCP_SERVER_DIR to one. The sandbox image is not buildable without the"
    echo "       genetics SDK (genetics-results-suite-4h6.11)."
    exit 1
  fi
  echo "--- SDK source: ${MCP_DIR}"

  trap 'rm -rf "${SANDBOX_DIR}/.sdk-src"' EXIT
  rm -rf "${SANDBOX_DIR}/.sdk-src"
  mkdir -p "${SANDBOX_DIR}/.sdk-src"
  cp "${MCP_DIR}/pyproject.toml" "${MCP_DIR}/README.md" "${SANDBOX_DIR}/.sdk-src/"
  cp -r "${MCP_DIR}/src" "${SANDBOX_DIR}/.sdk-src/src"

  # scripts/build.sh REGENERATES sandbox/schema and sandbox/stubs on every build so a
  # pushed image cannot ship documentation older than configs/datasets.yaml. This script
  # only CHECKS by default, because regenerating writes into tracked files in the working
  # tree and a developer starting a container did not ask for that diff. Drift is a
  # warning, not a failure: what it means is the image documents a stale SDK, which is a
  # real defect in a pushed image and a survivable one locally. --regen writes them.
  if [ "${REGEN}" = "1" ]; then
    echo "--- Regenerating sandbox schema docs and SDK stubs"
    python3 "${SCRIPT_DIR}/gen-sandbox-docs.py" --sdk-src "${SANDBOX_DIR}/.sdk-src"
  else
    echo "--- Checking sandbox schema docs and SDK stubs"
    python3 "${SCRIPT_DIR}/gen-sandbox-docs.py" --sdk-src "${SANDBOX_DIR}/.sdk-src" --check || {
      echo "WARNING: sandbox/schema or sandbox/stubs is out of date with configs/datasets.yaml"
      echo "         or with the staged SDK. The image will ship what is committed. Re-run"
      echo "         with --regen to update them."
    }
  fi
  python3 "${SCRIPT_DIR}/test-sandbox-docs.py" --sdk-src "${SANDBOX_DIR}/.sdk-src" || {
    echo "WARNING: scripts/test-sandbox-docs.py failed; the image's on-demand docs are wrong"
    echo "         in a way that has no runtime symptom. scripts/build.sh treats this as fatal."
  }

  SDK_REF="$(git -C "${MCP_DIR}" rev-parse --short HEAD 2>/dev/null || echo local)"
  echo "=== Building ${IMAGE} (SDK ${SDK_REF})"
  docker build --build-arg SDK_REF="${SDK_REF}" -t "${IMAGE}" "${SANDBOX_DIR}"
fi

# --------------------------------------------------------------------------------------
# Run. Every flag below is the local form of a line in k8s/deployments/sandbox.yaml; the
# fidelity report at the end enumerates what has NO local form.
# --------------------------------------------------------------------------------------

RETENTION_FLAGS=()
[ -n "${SANDBOX_RETENTION_S}" ] && RETENTION_FLAGS=(--env SANDBOX_RETENTION_S="${SANDBOX_RETENTION_S}")

RUNTIME_FLAGS=()
RUNTIME="${SANDBOX_DOCKER_RUNTIME:-}"
if [ -z "${RUNTIME}" ] && docker info --format '{{range $k, $v := .Runtimes}}{{$k}} {{end}}' 2>/dev/null | grep -qw runsc; then
  RUNTIME=runsc
fi
[ -n "${RUNTIME}" ] && RUNTIME_FLAGS=(--runtime "${RUNTIME}")

docker rm -f "${NAME}" >/dev/null 2>&1 || true

# NO CMD IN THE IMAGE. The ENTRYPOINT is the bare interpreter and the supervisor is supplied
# HERE, at run time — the same argv k8s/deployments/sandbox.yaml now passes as
# `args: ["/genetics/supervisor.py"]` (genetics-results-suite-4h6.50), so this run exercises
# the deployed invocation. Baking a CMD into the image instead would make a manifest that has
# lost its args: start a supervisor anyway, which is what deploy.sh's container-level
# command:/args: refusal relies on being loud.
docker run -d --name "${NAME}" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --user 65532:65532 \
  --pids-limit 1024 \
  --memory 3g --memory-swap 3g --cpus 1.5 \
  --tmpfs "/scratch:rw,exec,size=512m,mode=0700,uid=65532,gid=65532" \
  --stop-timeout 130 \
  --publish "127.0.0.1:${HOST_PORT}:8080" \
  --add-host host.docker.internal:host-gateway \
  --env GENETICS_API_URL="${GENETICS_API_URL}" \
  --env BIGQUERY_API_URL="${BIGQUERY_API_URL}" \
  --restart no \
  "${RETENTION_FLAGS[@]}" \
  "${RUNTIME_FLAGS[@]}" \
  "${IMAGE}" /genetics/supervisor.py >/dev/null

BASE_URL="http://127.0.0.1:${HOST_PORT}"
echo "--- Waiting for ${BASE_URL}/health"
deadline=$(( $(date +%s) + 120 ))
while :; do
  if ! docker inspect -f '{{.State.Running}}' "${NAME}" 2>/dev/null | grep -q true; then
    echo "ERROR: the container exited. Logs:"
    docker logs "${NAME}" 2>&1 | tail -40
    exit 1
  fi
  # `status` is "starting" until the startup assertions and prewarm() finish; the supervisor
  # binds first so that state is observable rather than a connection refusal.
  if curl -fsS "${BASE_URL}/health" 2>/dev/null | grep -q '"status": "ok"'; then
    break
  fi
  if [ "$(date +%s)" -ge "${deadline}" ]; then
    echo "ERROR: /health did not reach ok within 120s. Logs:"
    docker logs "${NAME}" 2>&1 | tail -40
    exit 1
  fi
  sleep 1
done

cat <<EOF

=== sandbox up: ${BASE_URL}  (container ${NAME}, image ${IMAGE})

  health   curl -s ${BASE_URL}/health
  logs     scripts/run-sandbox-local.sh --logs
  tests    python3 scripts/test-supervisor.py --container ${BASE_URL} --container-name ${NAME}
  stop     scripts/run-sandbox-local.sh --stop

  chat-backend's client needs SANDBOX_URL=${BASE_URL} (there is no default: it refuses to
  build without one, genetics-results-suite-6um).

WHAT THIS RUN DOES NOT REPRODUCE — every control whose ONLY enforcement is one of these is
unexercised here and must be verified at deploy time:
EOF
if [ "${RUNTIME}" = "runsc" ]; then
  echo "  gVisor          RUNNING under runsc (--runtime runsc), so the sentry IS in the path."
else
  echo "  gVisor          runc, not runsc: no userspace syscall boundary. The host kernel is"
  echo "                  directly reachable from model-authored code."
fi
cat <<'EOF'
  NetworkPolicy   there is none. Egress is UNRESTRICTED — the container reaches the whole
                  host network and the internet, including 169.254.169.254. The egress
                  allow-list (section 3) is the only thing that confines it in the pod.
  seccomp         Docker's default profile, not RuntimeDefault via containerd. Close, not
                  identical.
  DNS             normal Docker DNS. The pod has dnsPolicy: None and resolves db-api /
                  results-api out of /etc/hosts via hostAliases; here they are ordinary
                  names. /etc/nsswitch.conf is still asserted at startup.
  /scratch        tmpfs sized 512m. Over-budget writes get ENOSPC; under the emptyDir
                  sizeLimit the KUBELET EVICTS THE POD instead, which is the failure the
                  supervisor's sub-quotas (4h6.46) exist to prevent and it cannot happen here.
                  WORSE, AND IT CHANGES HOW LIMITS MUST BE SIZED: a tmpfs is page cache in
                  THIS CONTAINER'S OWN memory cgroup, so every byte under /scratch is charged
                  against the SAME 3 GiB as the child's RSS (measured: memory.current 113 MiB
                  -> 414 MiB after writing 300 MiB to /scratch). The pod's emptyDir has no
                  `medium: Memory`, so it is node-disk-backed and charged to a SEPARATE
                  budget, ephemeral-storage (requests 1Gi / limits 2Gi) — never to
                  limits.memory: 3Gi. Size RLIMIT_AS (4h6.41) or a /scratch quota (4h6.46)
                  against this container and it is up to 512 MiB more conservative than the
                  pod needs; a script holding 2.6 GiB RSS beside a 400 MiB /scratch is
                  cgroup-OOM-killed here and fine in the pod.
  ephemeral-      NO LOCAL FORM AT ALL. requests 1Gi / limits 2Gi in the manifest; Docker has
  storage         no equivalent knob, so exceeding it (eviction by the kubelet) is
                  unobservable and untestable here in either direction.
  pids            --pids-limit 1024 approximates the kubelet's pod_pids_limit, but it is a
                  per-CONTAINER cgroup rather than a per-POD one.
  restartPolicy   --restart no. A Deployment restarts the pod, so a crash-loop bug presents
                  here as a dead container and in the cluster as CrashLoopBackOff. Deliberate:
                  a dead container keeps its logs still and is easier to debug.
EOF
echo "  grace period    --stop-timeout 130 IS set, so \`--stop\` reproduces"
echo "                  terminationGracePeriodSeconds: 130 (SIGTERM, drain, reap, wipe, then"
echo "                  SIGKILL). \`docker rm -f\` or \`docker kill\` bypasses it — use --stop."

if [ "${DO_TEST}" = "1" ]; then
  echo
  # --container-name: the audit stream (4h6.45) leaves by the container's STDOUT, not by the
  # wire, so the harness reads it back with `docker logs`. Without the name that whole group
  # skips by name instead of proving nothing quietly.
  exec python3 "${SCRIPT_DIR}/test-supervisor.py" --container "${BASE_URL}" \
    --container-name "${NAME}"
fi
