#!/usr/bin/env bash
# Bring the five local dev servers up from ONE tree — the main checkouts or the
# matching git worktrees — and point db-api at one dataset (genetics-results-suite-r9e).
#
# Usage:
#   scripts/dev-stack.sh up                    worktree trees + genetics_dev  (the default)
#   scripts/dev-stack.sh up --tree main        main checkouts + genetics_results (production)
#   scripts/dev-stack.sh up --dataset genetics_results
#   scripts/dev-stack.sh up db-api chat-api    only those services
#   scripts/dev-stack.sh down [svc...]         stop this suite's servers on those ports
#   scripts/dev-stack.sh down --force          ... even if the port holder is NOT this suite's
#   scripts/dev-stack.sh status                what is listening, from which tree, on which dataset
#   scripts/dev-stack.sh logs chat-api         tail -f a service log
#
# A port is only freed when its holder belongs to this suite — its working directory is
# inside the service's repo (either tree) or its command line names that repo. Anything
# else is printed (pid, cwd, argv) and left alone: :3000 and :8080 are the two most
# squatted ports on a dev box, and killing a stranger's work is worse than not starting.
#
# SWITCHING BACK TO MASTER is two commands and nothing else:
#   scripts/dev-stack.sh down
#   scripts/dev-stack.sh up --tree main
#
# Environment:
#   SUITE_SIBLING_ROOT   directory holding the sibling checkouts (default: resolved from
#                        this repo's git common dir, i.e. ~/suite)
#   DEV_WORKTREE         worktree directory name under <repo>/.claude/worktrees
#                        (default: this checkout's own directory name when it is a worktree)
#   DEV_STACK_RUN_DIR    logs and pidfiles (default: ~/.cache/genetics-dev-stack).
#                        Deliberately OUTSIDE every repo — a log file inside a worktree
#                        would show up in `git status` of a tree someone is reviewing.
#   MCP_ENV_FILE         file holding ANTHROPIC_API_KEY and the ANALYZE_* models
#                        (default: <sibling root>/genetics-mcp-server/.env). It is
#                        gitignored and exists ONLY in the main checkout, so a worktree run
#                        reads the main checkout's copy rather than getting a copy of the
#                        secrets. Nothing here prints or copies its contents.
#   PROJECT_ID           GCP project for db-api (default: $GCP_PROJECT, else phewas-development)
#   SANDBOX_TOKEN_SIGNING_KEY, INTERNAL_API_SECRET, SANDBOX_ENABLED
#                        the sandbox's per-execution credential configuration. Generated
#                        once into DEV_STACK_RUN_DIR and reused; override to pin a value.
#                        WITHOUT THEM db-api and results-api resolve no sandbox principal
#                        and serve the SDK with no per-execution accounting at all, which
#                        is indistinguishable from the bug the tokens exist to fix.
#   SANDBOX_URL          code-execution sandbox (default: http://127.0.0.1:8081, what
#                        scripts/run-sandbox-local.sh publishes). MUST be set explicitly:
#                        the client's own default is 127.0.0.1:8080, which is db-api here
#                        (genetics-results-suite-6um).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SUITE_DIR="$(dirname "$SCRIPT_DIR")"

TREE=worktree
DATASET=""
COMMAND=""
FORCE=0
SERVICES=()

RUN_DIR="${DEV_STACK_RUN_DIR:-$HOME/.cache/genetics-dev-stack}"

# port | repo | health path. The ports are the ones the whole local setup already assumes:
# the frontend's VITE_* URLs, the vite /api proxy, and the sandbox container's
# host.docker.internal targets all name them, so a second copy on other ports needs all of
# those changed too (docs/local-dev-vm.md).
ALL_SERVICES=(db-api results-api chat-api bff frontend)

svc_port() {
    case "$1" in
        db-api) echo 8080 ;;
        results-api) echo 2000 ;;
        chat-api) echo 4000 ;;
        bff) echo 5000 ;;
        frontend) echo 3000 ;;
    esac
}

svc_repo() {
    case "$1" in
        db-api) echo genetics-results-db ;;
        results-api) echo genetics-results-api ;;
        chat-api) echo genetics-mcp-server ;;
        bff | frontend) echo genetics-results-browser ;;
    esac
}

svc_health() {
    case "$1" in
        db-api) echo /health ;;
        results-api | chat-api | bff) echo /healthz ;;
        frontend) echo / ;;
    esac
}

# the whole header, up to the first line of code — a hardcoded last line silently truncated
# --help mid-sentence, dropping the SANDBOX_URL warning, as soon as the header grew
usage() { sed -n '2,/^set -euo pipefail/p' "$0" | sed -n 's/^# \{0,1\}//p'; }

while [ $# -gt 0 ]; do
    case "$1" in
        up | down | status | logs) [ -z "$COMMAND" ] && COMMAND="$1" || SERVICES+=("$1") ;;
        --tree) shift; TREE="${1:-}" ;;
        --dataset) shift; DATASET="${1:-}" ;;
        --force) FORCE=1 ;;
        -h | --help) usage; exit 0 ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *) SERVICES+=("$1") ;;
    esac
    shift
done

[ -n "$COMMAND" ] || { usage; exit 2; }

case "$TREE" in
    worktree | main) ;;
    *) echo "--tree must be 'worktree' or 'main', got '$TREE'" >&2; exit 2 ;;
esac

if [ ${#SERVICES[@]} -eq 0 ]; then
    [ "$COMMAND" = logs ] && { echo "usage: $0 logs <service>" >&2; exit 2; }
    SERVICES=("${ALL_SERVICES[@]}")
else
    for s in "${SERVICES[@]}"; do
        case " ${ALL_SERVICES[*]} " in
            *" $s "*) ;;
            *) echo "unknown service '$s' (known: ${ALL_SERVICES[*]})" >&2; exit 2 ;;
        esac
    done
fi

# The dataset default follows the tree, because that pairing is the whole point: the
# worktree branches are developed against the small chr22 dev dataset, and master is what
# the user runs against production. Either can be overridden with --dataset.
if [ -z "$DATASET" ]; then
    if [ "$TREE" = worktree ]; then DATASET=genetics_dev; else DATASET=genetics_results; fi
fi

# --------------------------------------------------------------------------------------
# Where the trees are. Same resolution as scripts/sync-datasets.sh: the siblings sit next
# to the MAIN checkout, never next to a worktree.
# --------------------------------------------------------------------------------------
if [ -n "${SUITE_SIBLING_ROOT:-}" ]; then
    [ -d "$SUITE_SIBLING_ROOT" ] || { echo "ERROR: SUITE_SIBLING_ROOT '$SUITE_SIBLING_ROOT' is not a directory" >&2; exit 1; }
    SIBLING_ROOT="$(cd "$SUITE_SIBLING_ROOT" && pwd)"
else
    common="$(git -C "$SUITE_DIR" rev-parse --git-common-dir 2>/dev/null)" || {
        echo "ERROR: '$SUITE_DIR' is not a git checkout; set SUITE_SIBLING_ROOT" >&2; exit 1; }
    common="$(cd "$SUITE_DIR" && cd "$common" && pwd)"
    SIBLING_ROOT="$(dirname "$(dirname "$common")")"
fi

WORKTREE_NAME="${DEV_WORKTREE:-$(basename "$SUITE_DIR")}"

repo_dir() {
    local repo="$1"
    if [ "$TREE" = main ]; then
        echo "$SIBLING_ROOT/$repo"
    else
        echo "$SIBLING_ROOT/$repo/.claude/worktrees/$WORKTREE_NAME"
    fi
}

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-phewas-development}}"
MCP_ENV_FILE="${MCP_ENV_FILE:-$SIBLING_ROOT/genetics-mcp-server/.env}"
SANDBOX_URL="${SANDBOX_URL:-http://127.0.0.1:8081}"

# --------------------------------------------------------------------------------------
# The sandbox's per-execution credentials (genetics-results-suite-4h6.49).
#
# Without these the token path is DEAD LOCALLY AND SILENTLY SO. db-api and results-api read
# SANDBOX_ENABLED and SANDBOX_TOKEN_SIGNING_KEY from the environment; with neither set,
# `sandbox_enabled` is false, `verify_sandbox_token` rejects every sandbox-shaped bearer, no
# `SandboxPrincipal` is ever resolved, and results-api's `sandbox_budget.admit` is never
# called — so an SDK request is answered 200 with NO accounting at all. That is the exact
# shape of genetics-results-suite-0lf, and a local run that does not set these cannot tell
# the fixed path from the broken one.
#
# Generated once and kept in RUN_DIR, which is outside every repo: the key must be STABLE
# across restarts (rotating it invalidates a token minted seconds earlier) and must never
# land in a working tree. INTERNAL_API_SECRET is here because both services refuse to start
# with SANDBOX_ENABLED true and that secret unset — the sandbox itself is NOT given it
# (genetics-results-suite-4h6.7), so nothing in the sandbox path uses it; it exists so the
# services' own fail-closed startup check is satisfied by the same configuration the cluster
# has, rather than by turning the check off locally.
dev_secret() {
    # two statements: `local a=$1 b=$a` expands every word BEFORE the builtin assigns any of
    # them, so `$a` is unbound there and `set -u` kills the script
    local name="$1"
    local path="$RUN_DIR/$name"
    if [ ! -s "$path" ]; then
        mkdir -p "$RUN_DIR"
        ( umask 077; python3 -c 'import secrets;print(secrets.token_urlsafe(32))' >"$path" )
    fi
    cat "$path"
}

if [ "$COMMAND" = up ]; then
    SANDBOX_TOKEN_SIGNING_KEY="${SANDBOX_TOKEN_SIGNING_KEY:-$(dev_secret sandbox-token-signing-key)}"
    INTERNAL_API_SECRET="${INTERNAL_API_SECRET:-$(dev_secret internal-api-secret)}"
    SANDBOX_ENABLED="${SANDBOX_ENABLED:-true}"
    export SANDBOX_TOKEN_SIGNING_KEY INTERNAL_API_SECRET SANDBOX_ENABLED
fi

# Every port decision comes from `ss`. Without it the queries return nothing and the script
# concludes every port is free — it would start a second copy of everything and report the
# stack as fresh, so this is fatal rather than a warning.
case "$COMMAND" in
    up | down | status)
        command -v ss >/dev/null 2>&1 || {
            echo "ERROR: 'ss' not found (iproute2) — without it every port looks free" >&2; exit 1; }
        ;;
esac

# `up`/`down` signal whole process groups. As a normal user that can only reach this user's
# own processes; as root it reaches the machine, and `kill -- -1` means every process the
# caller may signal. The dev stack has no reason to run as root either way.
case "$COMMAND" in
    up | down)
        [ "$(id -u)" -ne 0 ] || {
            echo "ERROR: refusing to run '$COMMAND' as root — it signals process groups" >&2; exit 1; }
        ;;
esac

# --------------------------------------------------------------------------------------
# Process control. Everything is resolved from the LISTENING SOCKET, not from a pidfile,
# so `down` stops the servers whoever started them — including the tmux windows of
# docs/local-dev-vm.md, which is what makes taking the ports over possible at all. What it
# will NOT do is kill a port holder that is not this suite's: the socket says only that
# something answers on :3000, and on a dev box that is as likely to be someone else's vite.
# --------------------------------------------------------------------------------------
port_pids() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -un; }

port_free() { [ -z "$(port_pids "$1")" ]; }

pgid_of() { ps -o pgid= -p "$1" 2>/dev/null | tr -d ' '; }

SELF_PGID="$(pgid_of $$)"
PARENT_PGID="$(pgid_of "${PPID:-0}")"

pids_in_pgid() { ps -eo pid=,pgid= | awk -v g="$1" '$2 == g { print $1 }'; }

proc_cwd() { readlink "/proc/$1/cwd" 2>/dev/null || true; }
proc_argv() { tr '\0' ' ' < "/proc/$1/cmdline" 2>/dev/null | sed 's/[[:space:]]*$//'; }

# A pid is ours to signal when it runs from this suite's copy of the service's repo — which
# covers the main checkout and every worktree under it — or when its command line names that
# repo, for a server that has chdir'd away or whose /proc we cannot read. Nothing else
# matches: an unrelated app on :3000 or :8080 has neither.
proc_is_ours() {
    local pid="$1" svc="$2" root cwd argv repo
    repo="$(svc_repo "$svc")"
    root="$SIBLING_ROOT/$repo"
    cwd="$(proc_cwd "$pid")"
    case "$cwd" in "$root" | "$root"/*) return 0 ;; esac
    argv="$(proc_argv "$pid")"
    case "$argv" in *"$repo"*) return 0 ;; esac
    case "$svc:$argv" in chat-api:*genetics_mcp_server.chat_api*) return 0 ;; esac
    return 1
}

signallable_pgid() {
    local pgid="$1"
    [ -n "$pgid" ] || return 1
    # kill -- -0 and -- -1 mean "this group" and "every process we may signal"; and the
    # launcher's own group, or the group it inherited from the terminal that started it,
    # would take the caller (and that terminal) down with the target
    [ "$pgid" -ge 2 ] 2>/dev/null || return 1
    [ "$pgid" = "$SELF_PGID" ] && return 1
    [ "$pgid" = "$PARENT_PGID" ] && return 1
    return 0
}

report_foreign() {
    local pid="$1" pgid="$2" svc="$3" port="$4"
    {
        echo "  REFUSING to free :$port — its holder is not this suite's $svc:"
        echo "      pid  $pid  (pgid ${pgid:-?})"
        echo "      cwd  $(proc_cwd "$pid")"
        echo "      argv $(proc_argv "$pid")"
        echo "    stop it yourself, or re-run with --force to kill it anyway"
    } >&2
}

# Returns 0 only when the port ends up free. The pid->pgid map is snapshotted ONCE, before
# any signal: re-resolving it before the SIGKILL would run exactly while the target is
# dying, and a recycled pid would hand an unrelated group the SIGKILL.
free_port() {
    local port="$1" name="$2" pid pgid i entry
    local -a targets=() described=()
    port_free "$port" && return 0
    for pid in $(port_pids "$port"); do
        pgid="$(pgid_of "$pid")"
        if [ "$FORCE" -eq 0 ] && ! proc_is_ours "$pid" "$name"; then
            report_foreign "$pid" "$pgid" "$name" "$port"
            continue
        fi
        if ! signallable_pgid "$pgid"; then
            echo "  WARN: :$port held by pid $pid in process group ${pgid:-?} — not signalling it" >&2
            continue
        fi
        targets+=("$pgid")
        described+=("pid $pid $(proc_argv "$pid")")
    done
    [ ${#targets[@]} -gt 0 ] || return 1

    for pgid in "${targets[@]}"; do kill -TERM -- "-$pgid" 2>/dev/null || true; done
    for i in $(seq 1 20); do port_free "$port" && break; sleep 0.5; done
    if ! port_free "$port"; then
        for pgid in "${targets[@]}"; do kill -KILL -- "-$pgid" 2>/dev/null || true; done
        sleep 1
    fi

    if port_free "$port"; then
        for entry in "${described[@]}"; do echo "  stopped $name (:$port): $entry"; done
        return 0
    fi
    echo "  WARN: :$port still held by $(port_pids "$port" | tr '\n' ' ')" >&2
    return 1
}

# `up` records the process GROUP it started, and `down` uses it only when the port is free:
# a server that came up but never listened is invisible to `ss`, so without this it could
# never be stopped and every later `up` would leave another one behind.
reap_recorded() {
    local svc="$1" file="$RUN_DIR/$svc.pgid" pgid pid
    [ -f "$file" ] || return 1
    pgid="$(tr -dc '0-9' < "$file")"
    rm -f "$file"
    signallable_pgid "$pgid" || return 1
    local -a live=()
    for pid in $(pids_in_pgid "$pgid"); do
        if [ "$FORCE" -eq 1 ] || proc_is_ours "$pid" "$svc"; then live+=("$pid"); fi
    done
    [ ${#live[@]} -gt 0 ] || return 1
    echo "  stopped orphaned $svc (pgid $pgid, never listened): ${live[*]}"
    kill -TERM -- "-$pgid" 2>/dev/null || true
    return 0
}

wait_health() {
    local port="$1" path="$2" name="$3" tries="${4:-120}" i code
    for i in $(seq 1 "$tries"); do
        code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://localhost:$port$path" 2>/dev/null || true)"
        [ "$code" = "200" ] && { echo "  $name ready on :$port ($path 200)"; return 0; }
        sleep 1
    done
    echo "  ERROR: $name did not answer $path on :$port — see $RUN_DIR/$name.log" >&2
    return 1
}

check_dir() { [ -d "$1" ] || { echo "ERROR: $2 tree not found: $1" >&2; return 1; }; }

check_venv() {
    [ -x "$1/.venv/bin/python" ] || {
        echo "ERROR: no .venv in $1 — run 'uv sync' (or 'uv venv && uv pip install -r pyproject.toml') there first" >&2
        return 1
    }
}

check_node_modules() {
    [ -d "$1/node_modules" ] || { echo "ERROR: no node_modules in $1 — run 'npm install' there first" >&2; return 1; }
}

# Checked for EVERY selected service before the first port is freed. Discovering a missing
# .venv while starting service four means the first three have already taken their ports
# from the running stack and the last two are still serving the old tree — half on each,
# with no warning. Nothing here starts or stops anything.
preflight_svc() {
    local svc="$1" dir; dir="$(repo_dir "$(svc_repo "$svc")")"
    check_dir "$dir" "$svc" || return 1
    case "$svc" in
        db-api | results-api | chat-api) check_venv "$dir" || return 1 ;;
        bff | frontend) check_node_modules "$dir" || return 1 ;;
    esac
    case "$svc" in
        db-api | results-api)
            [ -f "$dir/configs/datasets.yaml" ] || echo "  WARN: $dir/configs/datasets.yaml missing — run scripts/sync-datasets.sh" >&2 ;;
        chat-api)
            [ -f "$MCP_ENV_FILE" ] || echo "  WARN: $MCP_ENV_FILE not found — chat will start and then fail on the first message (no ANTHROPIC_API_KEY)" >&2 ;;
    esac
    return 0
}

# --------------------------------------------------------------------------------------
# The services. Each runs under setsid so it owns its process group and `down` can stop
# the whole tree of it (npm -> sh -> node) without reaching the launcher. Their trees are
# validated by preflight_svc before any of this runs. Each sets LAST_CHILD so `up` can
# record the group even when the service never manages to listen.
# --------------------------------------------------------------------------------------
start_db_api() {
    local dir; dir="$(repo_dir genetics-results-db)"
    (
        cd "$dir"
        export PROJECT_ID="$PROJECT_ID" DATASET_ID="$DATASET" PORT=8080
        exec setsid .venv/bin/python api/main.py
    ) >"$RUN_DIR/db-api.log" 2>&1 &
    LAST_CHILD=$!
}

start_results_api() {
    local dir; dir="$(repo_dir genetics-results-api)"
    (
        cd "$dir"
        export CONFIG_PROFILE="${CONFIG_PROFILE:-finngen}" DEPLOY_ENV="${DEPLOY_ENV:-local}" RELOAD="${RELOAD:-1}"
        exec setsid .venv/bin/python run_server.py 2000
    ) >"$RUN_DIR/results-api.log" 2>&1 &
    LAST_CHILD=$!
}

start_chat_api() {
    local dir; dir="$(repo_dir genetics-mcp-server)"
    (
        cd "$dir"
        # the secrets enter as exported variables of this subshell only: never on a
        # command line (ps), never in the log, never copied into the worktree
        if [ -f "$MCP_ENV_FILE" ]; then set -a; . "$MCP_ENV_FILE"; set +a; fi
        export BIGQUERY_API_URL="${BIGQUERY_API_URL:-http://localhost:8080}"
        export GENETICS_API_URL="${GENETICS_API_URL:-http://localhost:2000/api}"
        export DEFAULT_MODEL="${DEFAULT_MODEL:-claude-opus-5}"
        export EXTERNAL_MCP_SERVERS="${EXTERNAL_MCP_SERVERS:-https://mcp.platform.opentargets.org}"
        export REQUIRE_AUTH="${REQUIRE_AUTH:-false}"
        export SANDBOX_URL="$SANDBOX_URL"
        exec setsid .venv/bin/python -m genetics_mcp_server.chat_api --port 4000
    ) >"$RUN_DIR/chat-api.log" 2>&1 &
    LAST_CHILD=$!
}

start_bff() {
    local dir; dir="$(repo_dir genetics-results-browser)"
    (
        cd "$dir"
        export GENETICS_API_URL="${GENETICS_API_URL:-http://localhost:2000/api}" BFF_PORT=5000
        exec setsid npm run bff:dev
    ) >"$RUN_DIR/bff.log" 2>&1 &
    LAST_CHILD=$!
}

start_frontend() {
    local dir; dir="$(repo_dir genetics-results-browser)"
    (
        cd "$dir"
        # vite's default mode is "development", which loads .env.local and NOT .env.dev —
        # and .env.local is gitignored, so it exists only in the main checkout. These
        # exports are the same three values and take precedence over any file (vite merges
        # prefixed process.env over the loaded files), so the worktree needs no env file.
        export VITE_TARGET="${VITE_TARGET:-public}"
        export VITE_API_URL="${VITE_API_URL:-http://localhost:5000/api}"
        export VITE_CHAT_URL="${VITE_CHAT_URL:-http://localhost:4000/chat}"
        exec setsid npm run dev
    ) >"$RUN_DIR/frontend.log" 2>&1 &
    LAST_CHILD=$!
}

# The process GROUP, not the socket holder: the group is what `down` signals, and it is
# knowable even for a service that never listens. Prefer the child we started; fall back to
# whatever now holds the port for the case where setsid forked instead of exec'ing.
record_pgid() {
    local name="$1" port="$2" child="$3" pid pgid
    pgid="$(pgid_of "$child")"
    if [ -z "$pgid" ] || [ "$pgid" = "$SELF_PGID" ]; then
        pid="$(port_pids "$port" | head -1)"
        pgid="${pid:+$(pgid_of "$pid")}"
    fi
    if signallable_pgid "${pgid:-}"; then printf '%s\n' "$pgid" >"$RUN_DIR/$name.pgid"; else rm -f "$RUN_DIR/$name.pgid"; fi
}

cmd_up() {
    mkdir -p "$RUN_DIR"
    echo "tree:    $TREE  ($(repo_dir genetics-results-db))"
    echo "dataset: $PROJECT_ID.$DATASET"
    echo "logs:    $RUN_DIR"
    echo

    local svc port failed=0
    for svc in "${ALL_SERVICES[@]}"; do
        case " ${SERVICES[*]} " in *" $svc "*) ;; *) continue ;; esac
        preflight_svc "$svc" || failed=1
    done
    [ "$failed" -eq 0 ] || { echo "ERROR: nothing was started or stopped — fix the above first" >&2; return 1; }

    for svc in "${ALL_SERVICES[@]}"; do
        case " ${SERVICES[*]} " in *" $svc "*) ;; *) continue ;; esac
        port="$(svc_port "$svc")"
        if ! free_port "$port" "$svc"; then
            echo "  SKIPPING $svc — :$port is not free" >&2
            failed=1
            continue
        fi
        echo "starting $svc on :$port"
        LAST_CHILD=""
        case "$svc" in
            db-api) start_db_api ;;
            results-api) start_results_api ;;
            chat-api) start_chat_api ;;
            bff) start_bff ;;
            frontend) start_frontend ;;
        esac
        # results-api verifies every configured tabix file against GCS before it serves,
        # which takes minutes on a cold run; the others answer in seconds
        if [ "$svc" = results-api ]; then
            wait_health "$port" "$(svc_health "$svc")" "$svc" 600 || failed=1
        else
            wait_health "$port" "$(svc_health "$svc")" "$svc" || failed=1
        fi
        record_pgid "$svc" "$port" "$LAST_CHILD"
    done

    echo
    cmd_status
    echo
    if [ "$TREE" = worktree ]; then
        echo "back to master:  $0 down && $0 up --tree main"
    else
        echo "back to the worktree branches:  $0 down && $0 up"
    fi
    [ "$failed" -eq 0 ] || { echo "ERROR: one or more services did not come up — see above and $RUN_DIR" >&2; return 1; }
    return 0
}

cmd_down() {
    local svc port failed=0
    for svc in "${SERVICES[@]}"; do
        port="$(svc_port "$svc")"
        # .pid is from an earlier revision of this script and recorded the socket holder,
        # not the group: leaving it around invites someone to signal the wrong process
        rm -f "$RUN_DIR/$svc.pid"
        if port_free "$port"; then
            reap_recorded "$svc" || echo "  $svc (:$port) not running"
        else
            free_port "$port" "$svc" || failed=1
            rm -f "$RUN_DIR/$svc.pgid"
        fi
    done
    [ "$failed" -eq 0 ] || return 1
    return 0
}

cmd_status() {
    local svc port pid cwd code
    printf '%-12s %-6s %-8s %s\n' SERVICE PORT HEALTH TREE
    for svc in "${ALL_SERVICES[@]}"; do
        port="$(svc_port "$svc")"
        pid="$(port_pids "$port" | head -1)"
        if [ -z "$pid" ]; then
            printf '%-12s %-6s %-8s %s\n' "$svc" "$port" "-" "not running"
            continue
        fi
        # curl has already written 000 when it fails, so appending "---" to its output would
        # render a bound-but-silent service as "000---"
        code="$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://localhost:$port$(svc_health "$svc")" 2>/dev/null || true)"
        case "$code" in '' | 000) code='---' ;; esac
        cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || echo "?")"
        printf '%-12s %-6s %-8s %s\n' "$svc" "$port" "$code" "$cwd"
    done
    pid="$(port_pids 8080 | head -1)"
    if [ -n "$pid" ]; then
        # db-api's /health does not name the dataset, and getting this wrong is the whole
        # hazard the dev dataset exists to remove — so read it from the process itself.
        # An UNSET DATASET_ID means production (api/main.py defaults it to genetics_results).
        echo
        echo "db-api reads:   $(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep '^DATASET_ID=' || echo 'DATASET_ID unset -> genetics_results (PRODUCTION)')"
    fi
}

case "$COMMAND" in
    up) cmd_up ;;
    down) cmd_down ;;
    status) cmd_status ;;
    logs)
        svc="${SERVICES[0]:-}"
        [ -n "$svc" ] || { echo "usage: $0 logs <service>" >&2; exit 2; }
        exec tail -f "$RUN_DIR/$svc.log"
        ;;
esac
