#!/usr/bin/env python3
"""Recipe and checks for the per-project chat memory feature.

Companion script to the "Proving ground" paragraph in docs/project-spec.md's "Chat memory
(per-project digest)" section (Alternative A: projects as first-class containers —
chat_sessions.project_id, the digest rendered over a project's sessions and frozen per
session, an unfiled session gets no memory).

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
cluster and are never printed. No address read back from the database is ever printed —
users appear as the same 12-hex pseudonym the memory log line carries.

CLEANUP. Every check subcommand is expected to leave synthetic state behind (sessions,
projects and the chat_memory setting). Each check appends the ids it created to a state
file, and the `cleanup` subcommand deletes exactly those ids — nothing is ever enumerated
from the server, so a mistyped --user cannot reach anything this script did not create.
--user and --user-2 are additionally constrained to the synthetic
memory-check*@broadinstitute.org family, and cleanup refuses to delete anything without
--yes.

RUNNING. `check all` runs 1-7 in order and lets the later checks reuse the project and
seeded sessions the earlier ones created (recorded as "fixtures" in the state file); each
numbered check is also runnable alone and creates whatever it is missing. Reuse is scoped to
one run: the warm session checks 3 and 4 share is rebuilt whenever it was not driven by this
process or no longer sits in the project asked for, since check 4 moves it out and leaves it
there. A check that
cannot be evaluated is a FAIL, not a pass — nothing here reports SKIP.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

NAMESPACE = "genetics"
DEPLOYMENT = "deploy/chat-backend"
POD_SELECTOR = "app=chat-backend"
API_BASE = "http://127.0.0.1:8000/chat/v1"

# the synthetic addresses this epic has used; cleanup deletes, so the blast radius of a typo
# is bounded by the pattern rather than by the operator's attention
SYNTHETIC_USER_RE = re.compile(r"^memory-check[-\w]*@broadinstitute\.org$")

# seeding turns exist only to give a project some content to index; the cache checks (3 and
# 4) are the ones that must run on the deployment's default model, so everything else uses
# the cheapest model and no tools
SEED_MODEL = "claude-haiku-4-5"

# when the script started, so log windows always span the whole run rather than a guess
_STARTED = time.time()


def _fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def _verdict(ok, name, evidence):
    print(f"{'PASS' if ok else 'FAIL'}: {name} — {evidence}")
    return 0 if ok else 1


def _pseudonym(value):
    # mirrors memory_gate.user_log_hash / project_log_hash exactly (strip().lower()), so
    # evidence printed here can be matched against `kubectl logs` output without ever
    # printing the address or the project id itself
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()[:12]


def _fingerprint(text):
    # unlike _pseudonym this normalises nothing: a digest that differs only in whitespace or
    # case is a real byte-inequality, and the evidence has to show it
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def require_synthetic_user(user, flag="--user"):
    if not SYNTHETIC_USER_RE.match(user):
        _fail(
            f"{flag} {user!r} is not a synthetic proving-ground address. Only "
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


def run_kubectl(context, *args, input_text=None, check=True, timeout=None):
    cmd = ["kubectl", "--context", context, "-n", NAMESPACE, *args]
    try:
        return subprocess.run(
            cmd, input=input_text, capture_output=True, text=True, check=check, timeout=timeout
        )
    except subprocess.TimeoutExpired as e:
        _fail(
            f"kubectl {' '.join(args[:2])} exceeded its {timeout}s wall-clock budget; "
            f"stderr: {(e.stderr or '')[-400:]}"
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


# a chat turn is a long SSE stream, so it gets its own program: it drops the content chunks
# (which carry model output, not evidence) and keeps only the memory/usage/error events plus
# the concatenated assistant text needed to build the next turn's message list
_POD_CHAT_PROGRAM = '''
import json, os, sys, time, urllib.error, urllib.request

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
req = urllib.request.Request(
    {base!r} + "/chat", data=json.dumps({body!r}).encode(), headers=headers, method="POST"
)
events, text = [], []
# urlopen's timeout is per read and every SSE ping resets it, so the only cap that bounds a
# turn is this one
deadline = time.monotonic() + {timeout}
try:
    with urllib.request.urlopen(req, timeout={timeout}) as resp:
        status = resp.status
        for raw in resp:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    "wall-clock budget of {timeout}s exceeded after %d event(s), %d chars"
                    % (len(events), sum(len(t) for t in text))
                )
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            try:
                obj = json.loads(line[6:])
            except ValueError:
                continue
            kind = obj.get("type")
            if kind == "content":
                text.append(obj.get("content") or "")
            elif kind == "done":
                events.append({{"type": "done"}})
            elif kind in ("memory", "usage", "error"):
                events.append(obj)
except urllib.error.HTTPError as e:
    print(json.dumps({{"status": e.code, "body": e.read().decode()[:400]}}))
    sys.exit(0)
except Exception as e:
    print(json.dumps({{"status": 0, "body": "%s: %s" % (type(e).__name__, e)}}))
    sys.exit(0)
print(json.dumps({{"status": status, "events": events, "text": "".join(text)}}))
'''


def _exec_json(context, program, what, timeout=None):
    r = run_kubectl(
        context, "exec", "-i", DEPLOYMENT, "--", "python3", "-",
        input_text=program, check=False, timeout=timeout,
    )
    try:
        payload = json.loads(r.stdout)
    except ValueError:
        _fail(f"in-pod {what} produced no JSON (exit {r.returncode}): {r.stderr.strip()[:400]}")
    if "error" in payload:
        _fail(payload["error"])
    return payload


def pod_http(context, user, calls):
    """Run HTTP calls against chat-backend from inside its own pod.

    `calls` is a list of (method, path, body-or-None); the return is a list of
    {"status", "body"} in the same order. The two auth secrets are read from the container's
    environment and never cross the exec boundary.
    """
    program = _POD_HTTP_PROGRAM.format(user=user, calls=calls, base=API_BASE)
    return _exec_json(context, program, "request program")["results"]


def api(context, user, method, path, body=None, expect=(200,)):
    """One call, decoded. `expect` is a hard requirement: a status outside it is a bug in the
    fixture setup rather than a check result, so it aborts instead of printing a verdict."""
    r = pod_http(context, user, [(method, path, body)])[0]
    try:
        decoded = json.loads(r["body"])
    except ValueError:
        decoded = r["body"]
    if expect is not None and r["status"] not in expect:
        _fail(f"{method} {path} returned {r['status']} (expected {expect}): {str(decoded)[:300]}")
    return r["status"], decoded


def pod_chat(context, user, body, timeout=600):
    """Drive one chat turn in-pod and return {"status", "events", "text"}.

    `timeout` is the turn's wall-clock budget; the exec gets a longer one so the in-pod
    message — which says how far the stream got — is what surfaces rather than a bare kill.
    """
    program = _POD_CHAT_PROGRAM.format(user=user, body=body, base=API_BASE, timeout=timeout)
    payload = _exec_json(context, program, "chat program", timeout=timeout + 60)
    if payload["status"] != 200:
        _fail(f"chat turn failed: status={payload['status']} {str(payload.get('body'))[:300]}")
    err = next((e for e in payload["events"] if e.get("type") == "error"), None)
    if err:
        _fail(f"chat turn streamed an error: {str(err.get('error'))[:300]}")
    # a truncated stream must fail here, where the evidence is, rather than downstream as a
    # missing chat_turn_metrics row
    if not any(e.get("type") == "done" for e in payload["events"]):
        _fail(
            "chat turn ended without a done event: events="
            f"{[e.get('type') for e in payload['events']]}, {len(payload['text'])} chars of text"
        )
    return payload


_SQLITE_PROGRAM = '''
import sqlite3, json
conn = sqlite3.connect("file:/data/chat_history.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
rows = [dict(r) for r in conn.execute({query!r}, {params!r})]
print(json.dumps(rows, default=str))
'''


def sqlite_read_only(context, query, params=()):
    """Run a read-only query against chat-backend's chat_history.db via stdout-only exec.

    Writing a file in the pod is blocked, so stdout is the only channel back; open with
    ?mode=ro and print JSON to stdout only. Ids travel as bound parameters, not as text
    spliced into the statement.
    """
    script = _SQLITE_PROGRAM.format(query=query, params=tuple(params))
    r = run_kubectl(
        context, "exec", "-i", DEPLOYMENT, "--", "python3", "-", input_text=script, check=False
    )
    try:
        return json.loads(r.stdout)
    except ValueError:
        _fail(f"in-pod sqlite read produced no JSON (exit {r.returncode}): {r.stderr.strip()[:400]}")


def tail_logs(context, since=None):
    """chat-backend's logs across every replica.

    A label selector rather than the deployment, because `kubectl logs deploy/...` reads one
    pod while `kubectl exec` may land on another; with a selector kubectl defaults to the
    last 10 lines, hence the explicit --tail=-1.
    """
    since = since or f"{int(time.time() - _STARTED) + 120}s"
    r = run_kubectl(
        context, "logs", "-l", POD_SELECTOR, "--tail=-1", f"--since={since}", check=False
    )
    if r.returncode != 0:
        _fail(f"kubectl logs failed (exit {r.returncode}): {r.stderr.strip()[:400]}")
    return r.stdout


def log_count(context, needle):
    return sum(1 for line in tail_logs(context).splitlines() if needle in line)


# ---------------------------------------------------------------------------
# State: what this run created, so cleanup deletes that and only that
# ---------------------------------------------------------------------------

KINDS = ("sessions", "projects", "settings")


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
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    for kind in KINDS:
        # an entry is {"id", "user"}; a bare string from an older run means the primary user,
        # which cleanup fills in from --user
        state[kind] = [
            e if isinstance(e, dict) else {"id": e, "user": None} for e in state.get(kind, [])
        ]
    state.setdefault("fixtures", {})
    return state


def _state_write(path, state):
    path.write_text(json.dumps(state, indent=2))


def state_record(path, kind, obj_id, user):
    # the checks call this as they create sessions/projects/settings; cleanup deletes
    # from this file only, so an id that never got here is out of cleanup's reach
    state = state_load(path)
    entry = {"id": obj_id, "user": user}
    if entry not in state[kind]:
        state[kind].append(entry)
    _state_write(path, state)


def fixture_get(path, name):
    return state_load(path)["fixtures"].get(name)


def fixture_set(path, name, value):
    state = state_load(path)
    state["fixtures"][name] = value
    _state_write(path, state)


# ---------------------------------------------------------------------------
# Fixtures: objects the checks share when they run as `check all`, and create
# for themselves when a check is run alone
# ---------------------------------------------------------------------------

def enable_memory(context, user, state):
    api(context, user, "PUT", "/llm-config/user/settings/chat_memory", {"setting_value": "on"})
    state_record(state, "settings", "chat_memory", user)


def create_session(context, user, state, project_id=None):
    body = {"project_id": project_id} if project_id else {}
    _st, session = api(context, user, "POST", "/chat/sessions", body)
    state_record(state, "sessions", session["id"], user)
    return session["id"]


def chat_turn(context, user, session_id, messages, model=SEED_MODEL, tools=False, persist=True):
    """One turn, optionally persisting the exchange so the session has content to index.

    Returns the pod_chat payload plus the message_id, which is the key chat_turn_metrics
    rows carry — that is how a cache measurement is tied to exactly this turn.
    """
    message_id = str(uuid.uuid4())
    body = {
        "messages": messages,
        "session_id": session_id,
        "enable_tools": tools,
        "secret": False,
        "message_id": message_id,
    }
    if model:
        body["model"] = model
    result = pod_chat(context, user, body)
    result["message_id"] = message_id
    if persist:
        api(context, user, "POST", f"/chat/sessions/{session_id}/messages",
            {"id": str(uuid.uuid4()), "role": "user", "content": messages[-1]["content"]})
        api(context, user, "POST", f"/chat/sessions/{session_id}/messages",
            {"id": str(uuid.uuid4()), "role": "assistant", "content": result["text"]})
    return result


def memory_event(result):
    return next((e for e in result["events"] if e.get("type") == "memory"), None)


def turn_metrics(context, message_id, attempts=5):
    """The chat_turn_metrics row for one turn; the row is written as the turn closes, so a
    read straight after the stream ends can be a beat early."""
    for attempt in range(attempts):
        rows = sqlite_read_only(
            context,
            "SELECT cache_read_tokens, cache_create_tokens, input_tokens, model "
            "FROM chat_turn_metrics WHERE message_id = ?",
            (message_id,),
        )
        if rows:
            return rows[0]
        if attempt < attempts - 1:
            time.sleep(2)
    _fail(f"no chat_turn_metrics row for message_id {message_id} after {attempts} reads")


def session_digest(context, session_id):
    rows = sqlite_read_only(
        context,
        "SELECT context_digest FROM chat_sessions WHERE id = ?",
        (session_id,),
    )
    if not rows:
        _fail(f"session {session_id} has no chat_sessions row")
    return rows[0]["context_digest"]


def ensure_project(context, user, state, key, label):
    """The fixture project, re-created if a previous cleanup deleted it. Returns
    {"id", "name"} read back from the server, so check 5 compares against the live name."""
    want = (fixture_get(state, key) or {}).get("id")
    _st, projects = api(context, user, "GET", "/projects")
    for project in projects:
        if project["id"] == want:
            return {"id": project["id"], "name": project["name"]}
    name = f"proving-ground-{label}-{int(time.time())}"
    _st, project = api(context, user, "POST", "/projects", {"name": name})
    state_record(state, "projects", project["id"], user)
    live = {"id": project["id"], "name": project["name"]}
    fixture_set(state, key, live)
    return live


def _session_project(context, user, session_id):
    """(exists, project_id) for one session, read through the same API the checks act on."""
    status, body = api(context, user, "GET", f"/chat/sessions/{session_id}", expect=None)
    if status != 200 or not isinstance(body, dict):
        return False, None
    return True, body.get("project_id")


def _session_exists(context, user, session_id):
    return _session_project(context, user, session_id)[0]


def ensure_seed_session(context, user, state, project_id, key, prompt):
    """A session filed in the project with one persisted exchange, so the project's digest
    has something to say. Without it checks 2 and 5 would compare empty renders."""
    want = fixture_get(state, key)
    if want and _session_exists(context, user, want):
        return want
    session_id = create_session(context, user, state, project_id=project_id)
    chat_turn(context, user, session_id, [{"role": "user", "content": prompt}])
    api(context, user, "PUT", f"/chat/sessions/{session_id}",
        {"title": f"proving-ground seed {int(time.time())}"})
    fixture_set(state, key, session_id)
    return session_id


def ensure_warm_session(context, user, state, project_id):
    """A session in the project with two default-model turns already run — check 3 measures
    them, check 4 continues the same conversation after a move.

    Existence alone is not enough to reuse it. Check 4 moves this session to another project
    and leaves it there, so a fixture from an earlier process would hand check 3 the previous
    run's chat_turn_metrics rows and hand check 4 a move that has already happened. The run
    marker and the session's live project_id both have to match, or the turns are re-driven in
    a new session.
    """
    warm = fixture_get(state, "warm_session")
    if warm and warm.get("run") == _STARTED:
        exists, current = _session_project(context, user, warm["id"])
        if exists and current == project_id:
            return warm
    session_id = create_session(context, user, state, project_id=project_id)
    first = [{"role": "user", "content": "Reply with the single word PCSK9."}]
    t1 = chat_turn(context, user, session_id, first, model=None, tools=True)
    second = first + [
        {"role": "assistant", "content": t1["text"]},
        {"role": "user", "content": "Reply with the single word LDLR."},
    ]
    t2 = chat_turn(context, user, session_id, second, model=None, tools=True)
    warm = {
        "id": session_id,
        "run": _STARTED,
        "messages": second + [{"role": "assistant", "content": t2["text"]}],
        "turn1": t1["message_id"],
        "turn2": t2["message_id"],
    }
    fixture_set(state, "warm_session", warm)
    return warm


# ---------------------------------------------------------------------------
# Checks (numbered as in the bead description)
# ---------------------------------------------------------------------------

def check1_log_line_project_only(context, user, user2, state):
    """Log line 'memory digest: user=<hash> project=<hash> sessions=N chars=M' fires only
    for a session inside a project — never for an unfiled session or a user without the
    chat_memory setting."""
    enable_memory(context, user, state)
    project = ensure_project(context, user, state, "project_a", "a")
    ensure_seed_session(context, user, state, project["id"], "seed_a",
                        "Reply with the single word APOE.")

    user_line = f"memory digest: user={_pseudonym(user)}"
    project_line = f"{user_line} project={_pseudonym(project['id'])}"

    before_project = log_count(context, project_line)
    filed = create_session(context, user, state, project_id=project["id"])
    chat_turn(context, user, filed, [{"role": "user", "content": "Reply with the single word TP53."}])
    after_filed_project = log_count(context, project_line)
    after_filed_user = log_count(context, user_line)

    unfiled = create_session(context, user, state)
    chat_turn(context, user, unfiled, [{"role": "user", "content": "Reply with the single word MYC."}])
    after_unfiled_user = log_count(context, user_line)

    # the second user never gets the setting; the setting is what this arm isolates, so a
    # stale 'on' from an earlier run would make it prove nothing
    status, setting = api(context, user2, "GET", "/llm-config/user/settings/chat_memory", expect=None)
    if status == 200 and setting is not None:
        _fail("--user-2 already has chat_memory set; run `--yes cleanup` first — the "
              "no-setting arm of check 1 cannot be evaluated otherwise")
    project2 = ensure_project(context, user2, state, "project_u2", "u2")
    u2_line = f"memory digest: user={_pseudonym(user2)}"
    before_u2 = log_count(context, u2_line)
    session2 = create_session(context, user2, state, project_id=project2["id"])
    chat_turn(context, user2, session2, [{"role": "user", "content": "Reply with the single word EGFR."}])
    after_u2 = log_count(context, u2_line)

    filed_fired = after_filed_project - before_project
    unfiled_fired = after_unfiled_user - after_filed_user
    no_setting_fired = after_u2 - before_u2
    ok = filed_fired == 1 and unfiled_fired == 0 and no_setting_fired == 0
    return _verdict(
        ok, "check1 project-scoped log line",
        f"filed session: {filed_fired} line(s) with project={_pseudonym(project['id'])} "
        f"(want 1); unfiled session: {unfiled_fired} (want 0); "
        f"user without the setting: {no_setting_fired} (want 0)",
    )


def check2_digest_byte_equality(context, user, user2, state):
    """GET /chat/v1/projects/{id}/memory digest byte-equals the digest the next new session
    in that project is rendered with (frozen in chat_sessions.context_digest).

    The two differ in exclude_session_id, so only the `digest` field — not the whole
    response — is the comparable quantity. The endpoint is read FIRST: it excludes nothing,
    so a session created before the read would be inside its window and outside the new
    session's."""
    enable_memory(context, user, state)
    project = ensure_project(context, user, state, "project_a", "a")
    ensure_seed_session(context, user, state, project["id"], "seed_a",
                        "Reply with the single word APOE.")

    _st, memory = api(context, user, "GET", f"/projects/{project['id']}/memory")
    endpoint_digest = memory["digest"]

    session_id = create_session(context, user, state, project_id=project["id"])
    chat_turn(context, user, session_id,
              [{"role": "user", "content": "Reply with the single word SORT1."}])
    rendered = session_digest(context, session_id) or ""

    if not endpoint_digest:
        return _verdict(False, "check2 digest byte-equality",
                        "the project rendered an empty digest, so the comparison proves "
                        "nothing — the project needs at least one session with content")
    ok = rendered == endpoint_digest
    return _verdict(
        ok, "check2 digest byte-equality",
        f"endpoint digest {len(endpoint_digest)} chars, session {session_id} froze "
        f"{len(rendered)} chars, {'identical' if ok else 'DIFFERENT'} "
        f"(sha {_fingerprint(endpoint_digest)} vs {_fingerprint(rendered)})",
    )


def check3_cache_warm_second_turn(context, user, user2, state):
    """Turn 2 cache_read >= turn 1 cache_create, on the default model, tools on, inside a
    project session — confirms the frozen digest is actually part of the cached prefix."""
    enable_memory(context, user, state)
    project = ensure_project(context, user, state, "project_a", "a")
    ensure_seed_session(context, user, state, project["id"], "seed_a",
                        "Reply with the single word APOE.")
    warm = ensure_warm_session(context, user, state, project["id"])

    m1 = turn_metrics(context, warm["turn1"])
    m2 = turn_metrics(context, warm["turn2"])
    ok = m1["cache_create_tokens"] > 0 and m2["cache_read_tokens"] >= m1["cache_create_tokens"]
    return _verdict(
        ok, "check3 turn-2 cache warm",
        f"model={m2['model']} turn1 cache_create={m1['cache_create_tokens']} "
        f"turn2 cache_read={m2['cache_read_tokens']} "
        f"(turn2 cache_create={m2['cache_create_tokens']})",
    )


def check4_move_spike_then_stable(context, user, user2, state):
    """Moving a session into a different project produces exactly one cache_create spike on
    its next turn (the digest re-renders and block 1 changes), then turns stay cached.

    Project B and its seed are built before the warm turns, and a control turn runs
    immediately before the move: the prompt cache expires on its own after a few minutes, so
    without a warm reading taken right before the PUT a spike after it would be evidence of
    elapsed time rather than of the move.
    """
    enable_memory(context, user, state)
    project_a = ensure_project(context, user, state, "project_a", "a")
    ensure_seed_session(context, user, state, project_a["id"], "seed_a",
                        "Reply with the single word APOE.")
    project_b = ensure_project(context, user, state, "project_b", "b")
    ensure_seed_session(context, user, state, project_b["id"], "seed_b",
                        "Reply with the single word LPA.")

    warm = ensure_warm_session(context, user, state, project_a["id"])

    messages = list(warm["messages"])
    messages.append({"role": "user", "content": "Reply with the single word HMGCR."})
    control = chat_turn(context, user, warm["id"], messages, model=None, tools=True)
    messages.append({"role": "assistant", "content": control["text"]})
    mc = turn_metrics(context, control["message_id"])
    if mc["cache_read_tokens"] <= 0:
        return _verdict(
            False, "check4 move spike then stable",
            f"the control turn before the move read {mc['cache_read_tokens']} cached tokens, "
            "so the prefix was already cold and a spike after the move would not be "
            "attributable to the move",
        )

    _exists, before_move = _session_project(context, user, warm["id"])
    if before_move == project_b["id"]:
        return _verdict(
            False, "check4 move spike then stable",
            f"session {warm['id']} is already filed in project B, so the PUT would be a "
            "no-op and the check would measure nothing; run `--yes cleanup` first",
        )

    api(context, user, "PUT", f"/chat/sessions/{warm['id']}/project",
        {"project_id": project_b["id"]})

    messages.append({"role": "user", "content": "Reply with the single word CETP."})
    t3 = chat_turn(context, user, warm["id"], messages, model=None, tools=True)
    messages.append({"role": "assistant", "content": t3["text"]})
    messages.append({"role": "user", "content": "Reply with the single word ABCA1."})
    t4 = chat_turn(context, user, warm["id"], messages, model=None, tools=True)
    messages.append({"role": "assistant", "content": t4["text"]})
    warm["messages"] = messages
    fixture_set(state, "warm_session", warm)

    m3 = turn_metrics(context, t3["message_id"])
    m4 = turn_metrics(context, t4["message_id"])
    spiked = m3["cache_create_tokens"] > mc["cache_create_tokens"]
    settled = (
        m4["cache_read_tokens"] >= m3["cache_create_tokens"]
        and m4["cache_create_tokens"] < m3["cache_create_tokens"]
    )
    return _verdict(
        spiked and settled, "check4 move spike then stable",
        f"control turn before move: cache_create={mc['cache_create_tokens']} "
        f"cache_read={mc['cache_read_tokens']}; "
        f"first turn after move={m3['cache_create_tokens']} "
        f"({'spike' if spiked else 'NO SPIKE'}), next turn "
        f"cache_create={m4['cache_create_tokens']} cache_read={m4['cache_read_tokens']} "
        f"({'settled' if settled else 'STILL RE-CREATING'})",
    )


def check5_sse_memory_event_once(context, user, user2, state):
    """The SSE stream's memory event — which already ships — carries the project's name,
    exactly once per session."""
    enable_memory(context, user, state)
    project = ensure_project(context, user, state, "project_a", "a")
    ensure_seed_session(context, user, state, project["id"], "seed_a",
                        "Reply with the single word APOE.")

    session_id = create_session(context, user, state, project_id=project["id"])
    first = [{"role": "user", "content": "Reply with the single word VEGFA."}]
    t1 = chat_turn(context, user, session_id, first)
    second = first + [
        {"role": "assistant", "content": t1["text"]},
        {"role": "user", "content": "Reply with the single word KRAS."},
    ]
    t2 = chat_turn(context, user, session_id, second)

    ev1, ev2 = memory_event(t1), memory_event(t2)
    named = ev1 is not None and ev1.get("project") == project["name"]
    once = ev2 is None
    return _verdict(
        named and once, "check5 project name on the SSE memory event",
        f"turn 1 event={json.dumps(ev1)} (want project={project['name']!r}); "
        f"turn 2 event={'none' if once else json.dumps(ev2)} (want none)",
    )


def check6_share_read_no_project_id(context, user, user2, state):
    """A non-owner reading a shared session link never sees project_id (mirrors the existing
    rule that a non-owner read clears context_digest)."""
    project = ensure_project(context, user, state, "project_a", "a")
    seed = ensure_seed_session(context, user, state, project["id"], "seed_a",
                               "Reply with the single word APOE.")
    api(context, user, "PUT", f"/chat/sessions/{seed}/share", {"shared": True})

    _st, owner_view = api(context, user, "GET", f"/chat/sessions/{seed}")
    _st, guest_view = api(context, user2, "GET", f"/chat/sessions/{seed}")
    ok = (
        owner_view.get("project_id") == project["id"]
        and guest_view.get("is_owner") is False
        and guest_view.get("project_id") is None
    )
    return _verdict(
        ok, "check6 non-owner share read",
        f"owner sees project_id={_pseudonym(owner_view.get('project_id') or '')} "
        f"(want the project's), non-owner sees project_id="
        f"{guest_view.get('project_id')!r} is_owner={guest_view.get('is_owner')!r}",
    )


def check7_fork_lands_unfiled(context, user, user2, state):
    """Forking a session lands the fork with no project — D5: an unfiled conversation gets no
    memory, and a fork does not inherit the source session's project."""
    project = ensure_project(context, user, state, "project_a", "a")
    seed = ensure_seed_session(context, user, state, project["id"], "seed_a",
                               "Reply with the single word APOE.")
    api(context, user, "PUT", f"/chat/sessions/{seed}/share", {"shared": True})

    _st, fork = api(context, user2, "POST", f"/chat/sessions/{seed}/fork")
    state_record(state, "sessions", fork["id"], user2)
    stored = sqlite_read_only(
        context, "SELECT project_id FROM chat_sessions WHERE id = ?", (fork["id"],)
    )
    stored_project = stored[0]["project_id"] if stored else "<no row>"
    ok = fork.get("project_id") is None and stored_project is None
    return _verdict(
        ok, "check7 fork lands unfiled",
        f"fork {fork['id']} response project_id={fork.get('project_id')!r}, "
        f"stored project_id={stored_project!r} (want null in both)",
    )


CHECKS = {
    1: ("log-line", check1_log_line_project_only),
    2: ("digest-match", check2_digest_byte_equality),
    3: ("cache-warm", check3_cache_warm_second_turn),
    4: ("move-spike", check4_move_spike_then_stable),
    5: ("sse-event", check5_sse_memory_event_once),
    6: ("share-read", check6_share_read_no_project_id),
    7: ("fork-unfiled", check7_fork_lands_unfiled),
}


def run_checks(numbers, context, user, user2, state):
    failed = []
    for n in numbers:
        _slug, fn = CHECKS[n]
        if fn(context, user, user2, state):
            failed.append(n)
    if len(numbers) > 1:
        print(f"{len(numbers) - len(failed)}/{len(numbers)} checks passed"
              + (f"; failed: {failed}" if failed else ""))
    return 1 if failed else 0


def cmd_cleanup(context, user, state, assume_yes):
    """Delete exactly the ids the checks recorded in the state file: sessions, projects (with
    the sessions filed in them) and the chat_memory setting, for each user that created
    them."""
    recorded = state_load(state)
    targets = []
    for entry in recorded["sessions"]:
        targets.append(("sessions", entry, "DELETE", f"/chat/sessions/{entry['id']}"))
    for entry in recorded["projects"]:
        # with_sessions=true so a session created by a check that never recorded it (a crash
        # between create and state_record) still goes with its project
        targets.append(("projects", entry, "DELETE",
                        f"/projects/{entry['id']}?with_sessions=true"))
    for entry in recorded["settings"]:
        targets.append(("settings", entry, "DELETE",
                        f"/llm-config/user/settings/{entry['id']}"))

    print(f"cleanup for state {state}: {len(targets)} recorded object(s)")
    if not targets:
        print("DONE: nothing recorded to delete")
        return 0
    if not assume_yes:
        for kind, entry, _method, path in targets:
            print(f"  would delete {path} (user {_pseudonym(entry.get('user') or user)})")
        print("DONE: dry run — re-run with --yes to delete")
        return 0

    by_user = {}
    for target in targets:
        by_user.setdefault(target[1].get("user") or user, []).append(target)

    failed = 0
    for owner, owned in by_user.items():
        results = pod_http(context, owner, [(m, p, None) for _k, _e, m, p in owned])
        for (kind, entry, _method, path), r in zip(owned, results):
            # a 404 means the object is already gone — re-running cleanup after a partial
            # failure must not treat that as a new failure, or the state file never clears
            ok = 200 <= r["status"] < 300 or r["status"] == 404
            print(f"  {'deleted' if ok else 'FAILED '} {path} (status={r['status']}, "
                  f"user {_pseudonym(owner)})")
            if ok:
                recorded[kind].remove(entry)
            else:
                failed += 1

    if not failed:
        # every recorded id is gone, so the fixture ids the checks would reuse are dangling
        recorded["fixtures"] = {}
    _state_write(state, recorded)
    if failed:
        print(f"FAILED: cleanup left {failed} of {len(targets)} object(s) behind")
        return 1

    print(f"DONE: deleted {len(targets)} object(s)")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--context", required=True, help="kubectl context; must be the current context and end in -staging")
    p.add_argument("--user", required=True, help="synthetic user email, e.g. memory-check@broadinstitute.org")
    p.add_argument("--user-2", default="memory-check-2@broadinstitute.org",
                   help="second synthetic user, for the share-read and fork checks")
    p.add_argument("--state", help="state file of ids created by the checks (default: under ~/.cache)")
    p.add_argument("--yes", action="store_true", help="cleanup: actually delete the recorded ids")
    sub = p.add_subparsers(dest="command", required=True)

    check_cmd = sub.add_parser("check", help="run one numbered check (1-7), or 'all'")
    check_cmd.add_argument("n", help="check number 1-7, or 'all'")

    for n, (slug, _fn) in CHECKS.items():
        sub.add_parser(f"check{n}-{slug}", help=f"check {n}: {slug}")

    sub.add_parser("cleanup", help="delete the ids this script recorded")

    args = p.parse_args()
    require_synthetic_user(args.user)
    require_synthetic_user(args.user_2, "--user-2")
    if args.user_2.strip().lower() == args.user.strip().lower():
        _fail("--user-2 must differ from --user: checks 6 and 7 need a non-owner")
    require_staging_context(args.context)
    state = Path(args.state) if args.state else default_state_path(args.user)

    if args.command == "cleanup":
        sys.exit(cmd_cleanup(args.context, args.user, state, args.yes))

    if args.command == "check":
        if args.n == "all":
            numbers = sorted(CHECKS)
        elif args.n.isdigit() and int(args.n) in CHECKS:
            numbers = [int(args.n)]
        else:
            _fail(f"check takes 1-{max(CHECKS)} or 'all', not {args.n!r}")
    else:
        numbers = [int(args.command[len("check"):].split("-", 1)[0])]

    sys.exit(run_checks(numbers, args.context, args.user, args.user_2, state))


if __name__ == "__main__":
    main()
