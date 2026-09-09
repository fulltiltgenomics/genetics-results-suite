#!/usr/bin/env python3
"""Recipe and checks for the per-project chat memory feature.

Companion script to the "Proving ground" paragraph in docs/project-spec.md's "Chat memory
(recent-work digest)" section (bd genetics-results-suite-idt3, Alternative A: projects as
first-class containers — chat_sessions.project_id, the digest rendered over a project's
sessions and frozen per session, an unfiled session gets no memory).

STATUS — every check below references endpoints, columns or log-line shapes that do not
exist yet on the branch this script ships with:
  - chat_projects table, chat_sessions.project_id          lands with idt3.3/idt3.4
  - GET /chat/v1/projects/{id}/memory                      lands with idt3.4/idt3.5
  - the "project=<hash>" memory-digest log line, the
    project name on the SSE memory event                   lands with idt3.5
Each check function calls `_pending(...)` and exits SKIP with the landing bead id until its
dependency is in place; a SKIP is not a PASS. Only `cleanup` and the context/exec plumbing
they share are exercised against the live cluster today.

ENVIRONMENT. Staging only, because staging is where the *deployed build* can be observed:
the pod's .status.startTime says which image is answering, `kubectl logs` carries the
memory-digest log line, and the real auth-gateway asserts the identity the gate keys on.
None of that exists locally. (The local dev-stack is not blocked by the identity check —
auth_required resolves the internal-secret marker plus an allow-listed identity header to
that email — but by `gateway_asserted`: `gateway_identity_secret` defaults to "" and
dev-stack.sh never sets it, so a local run needs GATEWAY_IDENTITY_SECRET exported before
chat-api starts and both the X-Internal-Auth and X-Gateway-Auth headers sent. That proves
the code, not the build.) The kubectl context must be the current context and must end in
"-staging"; the suite's staging context is named
gke_daly-finngenie_us-central1-a_finngenie-staging.

GETTING AN IMAGE TO STAGING. scripts/build-all.sh clones the pushed GitHub staging branches
of the sibling repos (results-api, db-api, mcp-server, browser) and builds+pushes their
images. scripts/rollout.sh --context <ctx> <service> <TAG> then updates one deployment to an
explicit YYYYMMDD.sha tag — a bare `:latest` is a silent no-op on this cluster. Verify a
rollout actually landed by the pod's .status.startTime, not by rollout.sh's own success
message:
  kubectl --context <ctx> -n genetics get pods -l app=chat-backend \
    -o jsonpath='{.items[0].status.startTime}'

HOW THIS SCRIPT TALKS TO STAGING. Every HTTP call runs *inside* the chat-backend pod: a
small program is piped to `kubectl exec -i deploy/chat-backend -- python3 -`, reads
INTERNAL_API_SECRET and GATEWAY_IDENTITY_SECRET from the container's own environment, calls
http://127.0.0.1:8000 with urllib and prints status codes and response bodies as JSON. The
secrets synthesize the X-Internal-Auth / X-Gateway-Auth / X-Goog-Authenticated-User-Email
headers auth-gateway would attach after a real oauth2-proxy session; they never leave the
cluster and are never printed. The only email this script prints is a pseudonym of the
--user value the caller supplied, never one read back from the database.

CLEANUP. Every check subcommand is expected to leave synthetic state behind (sessions, and
once idt3.4 lands, projects). Each check appends the ids it created to a state file, and the
`cleanup` subcommand deletes exactly those ids — nothing is ever enumerated from the server,
so a mistyped --user cannot reach anything this script did not create. --user is additionally
constrained to the synthetic memory-check@broadinstitute.org family, and cleanup refuses to
delete anything without --yes.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

NAMESPACE = "genetics"
DEPLOYMENT = "deploy/chat-backend"
API_BASE = "http://127.0.0.1:8000/chat/v1"

# the synthetic addresses this epic has used; cleanup deletes, so the blast radius of a typo
# is bounded by the pattern rather than by the operator's attention
SYNTHETIC_USER_RE = re.compile(r"^memory-check[-\w]*@broadinstitute\.org$")


def _fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def _pending(check_name, lands_with):
    print(f"SKIP: {check_name} — {lands_with} not implemented yet")
    return 2


def _pseudonym(email):
    # mirrors memory_gate.user_log_hash exactly (strip().lower()), so evidence printed here
    # can be matched against `kubectl logs` output without ever printing the email itself
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:12]


def require_synthetic_user(user):
    if not SYNTHETIC_USER_RE.match(user):
        _fail(
            f"--user {user!r} is not a synthetic proving-ground address. Only "
            "memory-check*@broadinstitute.org is accepted: the checks create sessions and "
            "settings and `cleanup` deletes them, so a typo in a real allow-listed address "
            "would otherwise put a real user's staging history in reach."
        )


def require_staging_context(context):
    if not context.endswith("-staging"):
        _fail(f"--context {context!r} does not end in '-staging'; this script runs on staging only")
    current = subprocess.run(
        ["kubectl", "config", "current-context"], capture_output=True, text=True
    ).stdout.strip()
    if current != context:
        _fail(
            f"--context does not name the context kubectl is actually on.\n"
            f"         --context       = {context}\n"
            f"         current context = {current or '<none>'}\n"
            "       A context name is not an endpoint (scripts/lib/env.sh): an inherited "
            "KUBECONFIG export can bind the expected name to any server, so the flag has to "
            "spell out the cluster you are already on rather than select another one."
        )
    kubeconfig = os.environ.get("KUBECONFIG") or f"{Path.home()}/.kube/config"
    print(f"Context: {context} (kubeconfig {kubeconfig})")


def run_kubectl(context, *args, input_text=None, check=True):
    cmd = ["kubectl", "--context", context, "-n", NAMESPACE, *args]
    return subprocess.run(
        cmd, input=input_text, capture_output=True, text=True, check=check
    )


_POD_HTTP_PROGRAM = '''
import json, os, sys, urllib.error, urllib.request

missing = [k for k in ("INTERNAL_API_SECRET", "GATEWAY_IDENTITY_SECRET") if not os.environ.get(k)]
if missing:
    print(json.dumps({{"error": "chat-backend is missing env: " + ",".join(missing)}}))
    sys.exit(1)
headers = {{
    "X-Goog-Authenticated-User-Email": {user!r},
    "X-Internal-Auth": os.environ["INTERNAL_API_SECRET"],
    "X-Gateway-Auth": os.environ["GATEWAY_IDENTITY_SECRET"],
    "Content-Type": "application/json",
}}
out = []
for method, path, body in {calls!r}:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request({base!r} + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out.append({{"status": resp.status, "body": resp.read().decode()}})
    except urllib.error.HTTPError as e:
        out.append({{"status": e.code, "body": e.read().decode()}})
    except Exception as e:
        out.append({{"status": 0, "body": "%s: %s" % (type(e).__name__, e)}})
print(json.dumps({{"results": out}}))
'''


def pod_http(context, user, calls):
    """Run HTTP calls against chat-backend from inside its own pod.

    `calls` is a list of (method, path, body-or-None); the return is a list of
    {"status", "body"} in the same order. The two auth secrets are read from the container's
    environment and never cross the exec boundary.
    """
    program = _POD_HTTP_PROGRAM.format(user=user, calls=calls, base=API_BASE)
    r = run_kubectl(
        context, "exec", "-i", DEPLOYMENT, "--", "python3", "-",
        input_text=program, check=False,
    )
    try:
        payload = json.loads(r.stdout)
    except ValueError:
        _fail(f"in-pod request program produced no JSON (exit {r.returncode}): {r.stderr.strip()[:400]}")
    if "error" in payload:
        _fail(payload["error"])
    return payload["results"]


def sqlite_read_only(context, query):
    """Run a read-only query against chat-backend's chat_history.db via stdout-only exec.

    Writing a file in the pod is blocked, so stdout is the only channel back; open with
    ?mode=ro and print JSON to stdout only.
    """
    script = f"""
import sqlite3, json
conn = sqlite3.connect("file:/data/chat_history.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
rows = [dict(r) for r in conn.execute({query!r})]
print(json.dumps(rows, default=str))
"""
    r = run_kubectl(
        context, "exec", "-i", DEPLOYMENT, "--", "python3", "-", input_text=script
    )
    return json.loads(r.stdout)


def tail_logs(context, deployment=DEPLOYMENT, since="10m"):
    r = run_kubectl(context, "logs", deployment, f"--since={since}")
    return r.stdout


# ---------------------------------------------------------------------------
# State: what this run created, so cleanup deletes that and only that
# ---------------------------------------------------------------------------

def default_state_path(user):
    cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache) if cache else Path.home() / ".cache"
    try:
        target = base / "genetics-results-suite"
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        target = Path.cwd()
    return target / f"chat-memory-proving-ground.{_pseudonym(user)}.json"


def state_load(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {"sessions": [], "projects": [], "settings": []}


def state_record(path, kind, value):
    # the checks call this as they create sessions/projects/settings; cleanup deletes
    # from this file only, so an id that never got here is out of cleanup's reach
    state = state_load(path)
    if value not in state.setdefault(kind, []):
        state[kind].append(value)
    path.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Checks (numbered as in the bead description)
# ---------------------------------------------------------------------------

def check1_log_line_project_only(context, user, state):
    """Log line 'memory digest: user=<hash> project=<hash> sessions=N chars=M' fires only
    for a session inside a project — never for an unfiled session or a user without the
    chat_memory setting."""
    return _pending("check1 project-scoped log line", "idt3.5 (memory_gate/memory_digest per-project rewrite)")


def check2_digest_byte_equality(context, user, state):
    """GET /chat/v1/projects/{id}/memory digest byte-equals the digest the next new session
    in that project is rendered with (frozen in chat_sessions.context_digest).

    The two differ in exclude_session_id, so only the `digest` field — not the whole
    response — is the comparable quantity."""
    return _pending("check2 digest byte-equality", "idt3.4 (GET /chat/v1/projects/{id}/memory)")


def check3_cache_warm_second_turn(context, user, state):
    """Turn 2 cache_read >= turn 1 cache_create, on the default model, tools on, inside a
    project session — confirms the frozen digest is actually part of the cached prefix."""
    return _pending("check3 turn-2 cache warm", "idt3.4/idt3.5 (project sessions + digest envelope)")


def check4_move_spike_then_stable(context, user, state):
    """Moving a session into a different project produces exactly one cache_create spike on
    its next turn (the digest re-renders and block 1 changes), then turns stay cached."""
    return _pending("check4 move spike then stable", "idt3.4 (project membership + move endpoint)")


def check5_sse_memory_event_once(context, user, state):
    """The SSE stream's memory event — which already ships — carries the project's name,
    exactly once per session."""
    return _pending("check5 project name on the SSE memory event", "idt3.5 (project name on the memory event)")


def check6_share_read_no_project_id(context, user, state):
    """A non-owner reading a shared session link never sees project_id (mirrors the existing
    rule that a non-owner read clears context_digest)."""
    return _pending("check6 non-owner share read", "idt3.4 (project_id on chat_sessions + share path)")


def check7_fork_lands_unfiled(context, user, state):
    """Forking a session lands the fork with no project — D5: an unfiled conversation gets no
    memory, and a fork does not inherit the source session's project."""
    return _pending("check7 fork lands unfiled", "idt3.4 (fork_session + project_id semantics)")


CHECKS = {
    1: ("log-line", check1_log_line_project_only),
    2: ("digest-match", check2_digest_byte_equality),
    3: ("cache-warm", check3_cache_warm_second_turn),
    4: ("move-spike", check4_move_spike_then_stable),
    5: ("sse-event", check5_sse_memory_event_once),
    6: ("share-read", check6_share_read_no_project_id),
    7: ("fork-unfiled", check7_fork_lands_unfiled),
}


def cmd_cleanup(context, user, state, assume_yes):
    """Delete exactly the ids the checks recorded in the state file — sessions, the
    chat_memory user_settings row, and (once the endpoints land) projects."""
    recorded = state_load(state)
    targets = [("sessions", sid, "DELETE", f"/chat/sessions/{sid}") for sid in recorded.get("sessions", [])]
    targets += [
        ("settings", k, "DELETE", f"/llm-config/user/settings/{k}") for k in recorded.get("settings", [])
    ]
    if recorded.get("projects"):
        print(
            "note: project ids are recorded but no project delete endpoint exists yet "
            "(idt3.4); they are left in the state file for a later cleanup run"
        )

    print(f"cleanup for user={_pseudonym(user)}: {len(targets)} recorded object(s) in {state}")
    if not targets:
        print("DONE: nothing recorded to delete")
        return 0
    if not assume_yes:
        print("DONE: dry run — re-run with --yes to delete")
        return 0

    calls = [(method, path, None) for _kind, _id, method, path in targets]
    results = pod_http(context, user, calls)
    failed = 0
    for (kind, obj_id, _method, path), r in zip(targets, results):
        # a 404 means the object is already gone — re-running cleanup after a partial
        # failure must not treat that as a new failure, or the state file never clears
        ok = 200 <= r["status"] < 300 or r["status"] == 404
        print(f"  {'deleted' if ok else 'FAILED '} {path} (status={r['status']})")
        if ok:
            recorded[kind].remove(obj_id)
        else:
            failed += 1

    state.write_text(json.dumps(recorded, indent=2))
    if failed:
        print(f"FAILED: cleanup left {failed} of {len(targets)} object(s) behind")
        return 1

    print(f"DONE: deleted {len(targets)} object(s)")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--context", required=True, help="kubectl context; must be the current context and end in -staging")
    p.add_argument("--user", required=True, help="synthetic user email, e.g. memory-check@broadinstitute.org")
    p.add_argument("--state", help="state file of ids created by the checks (default: under ~/.cache)")
    p.add_argument("--yes", action="store_true", help="cleanup: actually delete the recorded ids")
    sub = p.add_subparsers(dest="command", required=True)

    check_cmd = sub.add_parser("check", help="run one numbered check (1-7)")
    check_cmd.add_argument("n", type=int, choices=sorted(CHECKS))

    for n, (slug, _fn) in CHECKS.items():
        sub.add_parser(f"check{n}-{slug}", help=f"check {n}: {slug}")

    sub.add_parser("cleanup", help="delete the ids this script recorded for --user")

    args = p.parse_args()
    require_synthetic_user(args.user)
    require_staging_context(args.context)
    state = Path(args.state) if args.state else default_state_path(args.user)

    if args.command == "cleanup":
        sys.exit(cmd_cleanup(args.context, args.user, state, args.yes))

    if args.command == "check":
        n = args.n
    else:
        n = int(args.command[len("check"):].split("-", 1)[0])

    _slug, fn = CHECKS[n]
    sys.exit(fn(args.context, args.user, state) or 0)


if __name__ == "__main__":
    main()
