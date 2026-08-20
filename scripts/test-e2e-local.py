#!/usr/bin/env python3
"""End-to-end verification of run_analysis against the LOCAL stack (4h6.49).

Run:  scripts/test-e2e-local.py [--retention-s N]

WHY THIS IS NOT scripts/test-supervisor.py. That harness is deliberately self-contained: no
cluster, no credentials, no backends, no network beyond loopback, and its fast path needs no
Docker at all. Everything here is the opposite — it needs the whole dev stack up (db-api on
:8080, results-api on :2000), the sandbox container running, a signing key shared between the
minter and both verifiers, and BigQuery behind db-api. Folding this into test-supervisor.py
would make its fast path unrunnable, and a harness that only runs on one developer's machine
must not be the one that gates the properties everyone else re-checks. So: same style, same
check()/skip() reporting, separate file, separate preconditions.

WHAT IT PROVES THAT A 200 DOES NOT. The path is chat-backend mints -> POSTs -> the supervisor
forks -> the SDK reaches db-api and results-api with the per-execution token -> stdout comes
back capped -> artifacts land in the manifest -> audit records reach the container's stdout.
Every hop of that answers 200 in a broken configuration too, so nothing here asserts on a
status code alone:

  * The SDK's results-api request must appear in `sandbox_budget`'s per-execution map keyed on
    the token's `jti`. A caller presenting INTERNAL_API_SECRET is served 200 with the map
    EMPTY — that is the shape of genetics-results-suite-0lf — so the check reads results-api's
    own log for the `jti` and, separately, drives the per-execution CONCURRENCY limit until it
    answers 429, which only the admit path can produce. The negative control is measured in
    the same run rather than argued WHENEVER THE SECRET IS AVAILABLE AND AUTHENTICATES; when
    it is not, the control SKIPS and the run says so rather than resting silently on the
    concurrency evidence alone.
  * The audit records must carry the token's real sub/sid/jti, and a script writing forged
    `[user=...]` text on the audit fd must not produce a record that parses as genuine.
  * `execution_id`, the `/scratch/<id>` directory name and BOTH tokens' `jti` must be one
    value, and the join must close in chat-backend's log, the audit stream, db-api's log and
    results-api's log.
  * Each limit must return a clean structured result TO CHAT-BACKEND'S OWN CLIENT — not a
    hang, not an exception. test-supervisor.py already watches every limit fire on the wire;
    what is new here is that `SandboxClient.execute` turns each into a result dict.
  * An unset SANDBOX_TOKEN_SIGNING_KEY must fail the execution, and a WRONG key must be
    refused by both backends rather than served.
  * Artifacts must survive to the retention deadline and be gone after it.

A CHECK THAT CANNOT GO RED IS NOT A CHECK, and three shapes of that are guarded against here
by construction rather than by care:

  * NOTHING IS ASSERTED ABOUT A CONTAINER THAT IS NOT THE SOURCE UNDER TEST. `genetics-sandbox
    -local` can be left running from a build made before the change being verified, and every
    check below then passes about a different program. The run refuses to start unless the
    container's own /genetics/supervisor.py and /genetics/prewarm.py are byte-identical to
    sandbox/'s (see _verify_container_source, which also states what that does NOT cover).
  * AN ABSENCE IS ONLY EVIDENCE IF THE THING WAS THERE TO BEGIN WITH. Every "X is gone"
    assertion is preceded by a positive observation that X existed, and the positive
    observation is of the OBJECT (a marker read back out of /proc, a genuine audit record
    parsed), never of a call that returns before the object exists.
  * A SKIP IS NOT A PASS. Every skip is listed under a NOT MEASURED heading at the end and
    named in the exit banner. The check count in the docs is quoted next to the invocation
    that produces it, because a count without its command is not a claim.

WHAT THIS RUN CANNOT PROVE, and does not claim: gVisor syscall behaviour, the NetworkPolicy
egress allow-list, the kubelet's pod_pids_limit, RuntimeDefault seccomp, and whether
oom_score_adj and /proc process-group inspection behave under runsc. Docker gives none of
those a local form (scripts/run-sandbox-local.sh prints the full list at startup). Nor does
anything here establish a cross-user isolation property: genetics-results-suite-4h6.55 has
MEASURED that a child can read other executions' tokens out of inherited memory, read and
overwrite other executions' artifacts, and leave a resident setsid() process. This harness
measures that last one rather than asserting the comfortable answer — and it REPRODUCES it
(see the process-group group).
"""

import argparse
import asyncio
import ast
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RUN_DIR = os.path.join(os.path.expanduser("~"), ".cache", "genetics-dev-stack")

FAILURES = []
SKIPPED = []
CHECKS = 0


def check(name, condition, detail=""):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(f"{name}: {detail}" if detail else name)
        print(f"  FAIL  {name} {detail}")
    else:
        print(f"  ok    {name}")


def skip(name, reason):
    SKIPPED.append(f"{name}: {reason}")
    print(f"  skip  {name} ({reason})")


def die(message):
    print(f"HARNESS: {message}", file=sys.stderr)
    raise SystemExit(2)


# --------------------------------------------------------------------------------------
# interpreter: chat-backend's own minting and client code, from the sibling checkout
# --------------------------------------------------------------------------------------

def _mcp_dir():
    """The genetics-mcp-server checkout that matches this one — the sibling's worktree of the
    same name first, then its main checkout. Same resolution as run-sandbox-local.sh, and for
    the same reason: a worktree run must not silently test master."""
    parts = ROOT.split(os.sep)
    candidates = []
    if len(parts) >= 3 and parts[-3:-1] == [".claude", "worktrees"]:
        main = os.sep.join(parts[:-3])
        sibling = os.path.join(os.path.dirname(main), "genetics-mcp-server")
        candidates.append(os.path.join(sibling, ".claude", "worktrees", parts[-1]))
        candidates.append(sibling)
    candidates.append(os.path.join(os.path.dirname(ROOT), "genetics-mcp-server"))
    for path in candidates:
        if os.path.isdir(os.path.join(path, "src", "genetics_mcp_server")):
            return path
    return None


def _reexec_under_mcp_venv():
    """chat-backend's client needs httpx and PyJWT. Re-exec under the checkout's own venv
    rather than asking the caller to remember which interpreter has them — and ASSERT the
    module resolves out of THAT tree afterwards, so a globally installed copy cannot silently
    stand in for the branch under test."""
    mcp = _mcp_dir()
    if mcp is None:
        die("no genetics-mcp-server checkout found beside this one")
    python = os.path.join(mcp, ".venv", "bin", "python")
    if not os.path.exists(python):
        die(f"{python} is missing; create the checkout's venv first")
    # Unconditional, not "only if the import fails": an editable install of the MAIN checkout
    # in the ambient interpreter imports perfectly and would have this harness testing master
    # while reporting on the worktree. PYTHONPATH goes ahead of site-packages so the venv's own
    # editable install cannot win either.
    if os.environ.get("_E2E_REEXEC") != "1":
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(mcp, "src") + os.pathsep + env.get("PYTHONPATH", "")
        env["_E2E_REEXEC"] = "1"
        os.execve(python, [python, os.path.abspath(__file__)] + sys.argv[1:], env)
    import genetics_mcp_server
    resolved = os.path.realpath(os.path.dirname(genetics_mcp_server.__file__))
    expected = os.path.realpath(os.path.join(mcp, "src", "genetics_mcp_server"))
    if resolved != expected:
        die(f"genetics_mcp_server resolves to {resolved}, not the tree under test {expected}")
    return mcp


# --------------------------------------------------------------------------------------
# reading what the services and the container actually recorded
# --------------------------------------------------------------------------------------

class LogTail:
    """Lines a service appended while the block ran. Opened at __enter__ and read at text(),
    so a check only ever sees records its own execution produced."""

    def __init__(self, path):
        self.path = path
        self._offset = 0

    def __enter__(self):
        try:
            self._offset = os.path.getsize(self.path)
        except OSError:
            self._offset = 0
        return self

    def __exit__(self, *exc):
        return False

    def lines(self):
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                fh.seek(self._offset)
                return fh.read().splitlines()
        except OSError:
            return []

    def records(self):
        out = []
        for line in self.lines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def _container_file(name, path):
    """One file out of the RUNNING container. `docker cp` streams a tar to stdout and works on
    a distroless image, where `docker exec` has no shell to run."""
    proc = subprocess.run(["docker", "cp", f"{name}:{path}", "-"],
                          capture_output=True, timeout=120)
    if proc.returncode != 0 or not proc.stdout:
        return None
    try:
        with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tf:
            member = tf.next()
            if member is None or not member.isfile():
                return None
            handle = tf.extractfile(member)
            return None if handle is None else handle.read()
    except tarfile.TarError:
        return None


def _verify_container_source(name):
    """The container must be running THE SOURCE UNDER TEST, or nothing below means anything.

    Every other precondition here is about the stack being up. This one is about it being the
    RIGHT stack: `genetics-sandbox-local` survives a rebuild, a rebase and a branch switch, so
    a container started before the change under test passes every check in this file while
    proving them about a different program. scripts/run-sandbox-local.sh is itself modified by
    the change this harness verifies, which makes a stale container the LIKELY state rather
    than a hypothetical one.

    Compared: the two files the image ships from sandbox/ that this harness exercises. NOT
    covered, and deliberately not claimed — sandbox/requirements.txt, prune_venv.py and the
    genetics-mcp-server checkout the SDK is pip-installed from all shape the image without
    appearing under /genetics, so a change to those is invisible here. Everything this file
    asserts is supervisor behaviour, and the supervisor is compared byte for byte."""
    for local, inside in (("supervisor.py", "/genetics/supervisor.py"),
                          ("prewarm.py", "/genetics/prewarm.py")):
        with open(os.path.join(ROOT, "sandbox", local), "rb") as fh:
            want = hashlib.sha256(fh.read()).hexdigest()
        got_bytes = _container_file(name, inside)
        if got_bytes is None:
            die(f"cannot read {inside} out of container {name}; without it there is no "
                "evidence the container is running the source under test")
        got = hashlib.sha256(got_bytes).hexdigest()
        if got != want:
            die(f"container {name} is running a DIFFERENT {inside} ({got[:12]}) from "
                f"sandbox/{local} ({want[:12]}). It was built before the change under test; "
                "rebuild it with scripts/run-sandbox-local.sh")
    print(f"precondition: {name} runs sandbox/supervisor.py and sandbox/prewarm.py byte for "
          "byte (requirements.txt and the SDK's checkout are NOT covered)")


def _container_retention(name):
    """(effective_retention_s, shipped_default_s) as the CONTAINER is actually running them.

    Read from the container rather than from the caller's --retention-s, because the flag
    asserts what the container was STARTED with and a wrong flag makes both sides of the
    retention group pass while the claimed boundary is never tested: a 409 probed at t~=0 is
    satisfied by any positive TTL, including one second.

    Two independent sources that must agree. `docker inspect` reports the env the container was
    created with; the supervisor's own startup warning reports the value it ACCEPTED and, in
    the same line, the shipped constant it shortened. Either alone could be stale or absent —
    an override refused by _retention_s() never starts, and an env var the supervisor ignored
    would show in inspect and not in the log. Returns (None, None) when they disagree or when
    the value cannot be established, and the caller then skips by name."""
    try:
        raw_env = subprocess.run(["docker", "inspect", "--format", "{{json .Config.Env}}", name],
                                 capture_output=True, text=True, timeout=60)
        env = dict(e.split("=", 1) for e in json.loads(raw_env.stdout) if "=" in e)
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True,
                              timeout=120)
    except Exception:
        return None, None
    stream = logs.stdout + logs.stderr
    announced = re.search(r"SANDBOX_RETENTION_S=(\d+) overrides the (\d+)-second artifact "
                          r"retention", stream)
    declared = env.get("SANDBOX_RETENTION_S")
    if declared is None:
        # No override: the supervisor runs its shipped constant and must NOT have announced one.
        return (None, None) if announced else ("default", None)
    if announced is None or announced.group(1) != declared.strip():
        return None, None
    return int(announced.group(1)), int(announced.group(2))


def _source_constant(path, name):
    """A module-level literal read out of the source under test, so the harness follows the
    code rather than a number remembered in a comment. Only used for constants whose value the
    container has already been proved to share, byte for byte."""
    with open(path, encoding="utf-8") as fh:
        match = re.search(rf"^{name} = ([0-9.]+)$", fh.read(), re.M)
    return None if match is None else ast.literal_eval(match.group(1))


class DockerLogTail:
    """The CONTAINER's stdout only, deliberately not its stderr: the audit stream's contract is
    that the records leave by stdout, which is what the cluster's logging agent collects."""

    def __init__(self, name):
        self.name = name
        self._before = ""

    def _read(self):
        return subprocess.run(["docker", "logs", self.name], capture_output=True,
                              text=True, timeout=60).stdout

    def __enter__(self):
        self._before = self._read()
        return self

    def __exit__(self, *exc):
        return False

    def text(self):
        after = self._read()
        return after[len(self._before):] if after.startswith(self._before) else after


# The analyzer's regex, copied from genetics-mcp-server
# src/genetics_mcp_server/scripts/analyze_conversations.py (SDK_CALL_RE). Held identical to
# test-supervisor.py's copy, which checks itself against the shipped analyzer.
AUDIT_RE = re.compile(
    r"\[user=(?P<user>[^\]]*)\] \[session=(?P<session>[^\]]*)\] \[execution=(?P<execution>[^\]]*)\] "
    r"Executing SDK function: (?P<function>\S+) with input: (?P<arguments>.*?) "
    r"rows: (?P<rows>\d+)(?: error: (?P<error>\S+))?(?P<cancelled> cancelled)?$")


def audit_records(text):
    return [m.groupdict() for m in (AUDIT_RE.search(line) for line in text.splitlines()) if m]


# --------------------------------------------------------------------------------------
# the scripts the sandbox runs
# --------------------------------------------------------------------------------------

IDENTITY_CODE = r"""
import asyncio, json, os
from genetics_mcp_server.sdk import GeneticsClient

report = {
    "artifacts_dir": os.environ.get("SANDBOX_ARTIFACTS_DIR"),
    "user": os.environ.get("SANDBOX_USER"),
    "session": os.environ.get("SANDBOX_SESSION_ID"),
    "execution": os.environ.get("SANDBOX_EXECUTION_ID"),
    "scratch": sorted(p for p in os.listdir("/scratch") if not p.startswith(".")),
}

async def main():
    g = GeneticsClient()
    try:
        report["db_rows"] = len(await g.sql("SELECT 1 AS one"))
    except Exception as exc:
        report["db_error"] = f"{type(exc).__name__}: {exc}"[:300]
    try:
        report["results_rows"] = len(await g.search("APOE"))
    except Exception as exc:
        report["results_error"] = f"{type(exc).__name__}: {exc}"[:300]
    await g.close()

asyncio.run(main())
with open(os.path.join(os.environ["SANDBOX_ARTIFACTS_DIR"], "e2e.csv"), "w") as fh:
    fh.write("a,b\n1,2\n")
print("REPORT " + json.dumps(report))
"""

# 12 concurrent results-api calls against a per-execution ceiling of 4. asyncio.gather issues
# them together, so the overlap is not a race the check depends on winning: at most 4 can be
# in flight, and every rejection is a 429 carrying `code: sandbox_concurrency`.
CONCURRENCY_CODE = r"""
import asyncio, json, os
from genetics_mcp_server.sdk import GeneticsClient

async def main():
    g = GeneticsClient()
    async def one(term):
        try:
            return {"ok": len(await g.search(term))}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"[:400]}
    out = await asyncio.gather(*(one(t) for t in
                                 ["APOE", "TP53", "BRCA1", "IL7R", "PCSK9", "LDLR",
                                  "APOB", "MTHFR", "CFTR", "HBB", "F5", "VWF"]))
    await g.close()
    print("CONCURRENCY " + json.dumps(out))

asyncio.run(main())
"""

WRONG_KEY_CODE = r"""
import asyncio, json
from genetics_mcp_server.sdk import GeneticsClient

async def main():
    g = GeneticsClient()
    out = {}
    for label, call in [("db", lambda: g.sql("SELECT 1 AS one")),
                        ("results", lambda: g.search("APOE"))]:
        try:
            await call()
            out[label] = "served"
        except Exception as exc:
            out[label] = f"{type(exc).__name__}: {exc}"[:300]
    await g.close()
    print("WRONGKEY " + json.dumps(out))

asyncio.run(main())
"""

# A grandchild in the execution's own process group, and a second one that setsid()s away from
# it, so a LATER execution can look for both in /proc.
#
# NO exec, AND THAT IS THE WHOLE POINT. The image is gcr.io/distroless/python3-debian12:nonroot
# (sandbox/Dockerfile): there is no /bin/sleep, no shell, no coreutils at all. An
# os.execv("/bin/sleep", ...) here raises FileNotFoundError INSIDE THE FORKED CHILD, which then
# dies immediately and lingers only as an unreaped zombie carrying no marker — while the PARENT
# still reports a successful spawn, because fork() returned a pid before the exec was ever
# attempted. A guard written on the parent's report is then blind to exactly the failure it
# exists to catch, and the survivor scan finds nothing whichever way the kill behaved.
#
# So: fork + optional setsid + time.sleep, no exec — the shape scripts/test-supervisor.py
# already uses (its `escape` script and its reap-fallback fork) — and the marker goes on the
# process NAME via /proc/self/comm, i.e. prctl(PR_SET_NAME), which is what a fork without exec
# can still change; argv[0] cannot be used because the child inherits the parent's cmdline
# verbatim. PR_SET_NAME truncates at 15 bytes, hence the short tag. The parent READS THE NAME
# BACK out of /proc/<pid>/comm before it goes on, so what it reports is the existence of a
# marked process rather than the return of a syscall.
GRANDCHILD_CODE = r"""
import json, os, time

TAG = "%(tag)s"
SPIN = %(spin)s

spawned = []
for kind in ("G", "D"):
    pid = os.fork()
    if pid == 0:
        try:
            if kind == "D":
                os.setsid()
            with open("/proc/self/comm", "w") as fh:
                fh.write("E2E-%%s-%%s" %% (TAG, kind))
            time.sleep(%(sleep)d)
        finally:
            os._exit(0)
    comm = ""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with open("/proc/%%d/comm" %% pid) as fh:
                comm = fh.read().strip()
        except OSError:
            comm = ""
        if comm.startswith("E2E-"):
            break
        time.sleep(0.01)
    spawned.append({"kind": kind, "pid": pid, "comm": comm})
print("SPAWNED " + json.dumps(spawned), flush=True)
if SPIN:
    # Hold the wall clock open. sandbox/supervisor.py signals the process group from
    # _fire_limit and NOWHERE ELSE, so this is what makes the group kill happen at all.
    while True:
        pass
time.sleep(1)
print("PARENT DONE", flush=True)
"""

SURVIVOR_SCAN_CODE = r"""
import json, os
# /proc/<pid>/stat, not /proc/<pid>/cmdline: a fork without exec inherits the parent's argv, so
# the NAME is the only per-process marker a grandchild can carry. proc(5) fields after the
# closing paren are state, ppid, pgrp — reported so the caller can see whether a survivor is
# still IN the execution's process group, and so an unreaped zombie is distinguishable from a
# live process (the supervisor is pid 1 and never waits on orphans, so a grandchild it KILLED
# stays visible in /proc with its name intact and state 'Z').
found = []
for entry in os.listdir("/proc"):
    if not entry.isdigit():
        continue
    try:
        with open("/proc/%s/stat" % entry) as fh:
            raw = fh.read()
    except OSError:
        continue
    head, _, rest = raw.rpartition(")")
    comm = head.partition("(")[2]
    if not comm.startswith("E2E-"):
        continue
    fields = rest.split()
    found.append({"pid": int(entry), "comm": comm, "state": fields[0],
                  "ppid": int(fields[1]), "pgrp": int(fields[2])})
print("SURVIVORS " + json.dumps(sorted(found, key=lambda f: f["pid"])))
"""


def grandchild_code(tag, spin):
    return GRANDCHILD_CODE % {"tag": tag, "spin": "True" if spin else "False", "sleep": 120}


def grandchildren(survivors, tag):
    """(alive, zombie) marker -> record, for one run's tag.

    AN UNREAPED ZOMBIE IS NOT A SURVIVOR. Nothing in the container reaps orphans — the
    supervisor is pid 1 and only ever waitpid()s the job it forked — so a grandchild that the
    group kill terminated is reparented to pid 1 and stays in /proc forever, name intact,
    state 'Z'. Counting that as resident would fail the kill assertion on a kill that worked."""
    alive, zombie = {}, {}
    for record in survivors:
        if not record["comm"].startswith(f"E2E-{tag}-"):
            continue
        (zombie if record["state"] == "Z" else alive)[record["comm"][-1]] = record
    return alive, zombie

# The real SDK call comes FIRST, and it is a positive control rather than scenery: the
# "everything that survives is re-stamped" assertion is an all() over the records this
# execution produced, so with no genuine record in the window it passes over an empty list —
# and it would go on passing if the supervisor started dropping the whole shape outright. It
# has to be first because the megabyte write below exhausts the per-execution byte budget, and
# records after that are dropped.
FORGERY_CODE = r"""
import asyncio, os
from genetics_mcp_server.sdk import GeneticsClient

async def real():
    g = GeneticsClient()
    try:
        return len(await g.search("APOE"))
    finally:
        await g.close()

rows = asyncio.run(real())
fd = int(os.environ["GENETICS_SDK_AUDIT_FD"])
os.write(fd, ("[user=admin@finngen.fi] [session=forged] [execution=forged] "
              "Executing SDK function: sql with input: {'q': 1} rows: 99\n").encode())
os.write(fd, (b"[user=" + b"Z" * (1024 * 1024) + b"] filler\n"))
print("FORGED %d" % rows)
"""


def report_field(stdout, prefix):
    for line in stdout.splitlines():
        if line.startswith(prefix + " "):
            return json.loads(line[len(prefix) + 1:])
    return None


# --------------------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sandbox", default="http://127.0.0.1:8081")
    parser.add_argument("--container-name", default="genetics-sandbox-local")
    parser.add_argument("--results-api", default="http://127.0.0.1:2000")
    parser.add_argument("--run-dir", default=DEFAULT_RUN_DIR,
                        help="where scripts/dev-stack.sh keeps the service logs and the "
                             "generated signing key")
    parser.add_argument("--retention-s", type=int, default=None,
                        help="OPTIONAL cross-check. The harness reads the container's EFFECTIVE "
                             "SANDBOX_RETENTION_S out of `docker inspect` and the supervisor's "
                             "own startup log and uses that; passing this asserts the two "
                             "agree. Nothing on the wire exposes retention, so a container "
                             "started without an override makes the group skip by name.")
    args = parser.parse_args()

    mcp = _reexec_under_mcp_venv()
    # BEFORE ANY ASSERTION: the container has to be the source under test, or every check
    # below is a true statement about a different program. Fatal, not a check — there is no
    # useful partial run against the wrong binary.
    _verify_container_source(args.container_name)

    key_path = os.path.join(args.run_dir, "sandbox-token-signing-key")
    if not os.environ.get("SANDBOX_TOKEN_SIGNING_KEY"):
        if not os.path.exists(key_path):
            die(f"no signing key at {key_path}; start the stack with scripts/dev-stack.sh up")
        with open(key_path, encoding="utf-8") as fh:
            os.environ["SANDBOX_TOKEN_SIGNING_KEY"] = fh.read().strip()
    os.environ["SANDBOX_URL"] = args.sandbox

    import httpx
    import jwt
    from genetics_mcp_server import sandbox_token as st
    from genetics_mcp_server.config.settings import get_settings
    from genetics_mcp_server.sandbox_client import SandboxClient

    signing_key = os.environ["SANDBOX_TOKEN_SIGNING_KEY"]
    db_log = LogTail(os.path.join(args.run_dir, "db-api.log"))
    api_log = LogTail(os.path.join(args.run_dir, "results-api.log"))

    client = SandboxClient(args.sandbox)
    try:
        health = asyncio.run(client.health())
    except Exception as exc:
        die(f"sandbox at {args.sandbox} is not reachable: {exc}. "
            "Start one with scripts/run-sandbox-local.sh")
    if health.busy or health.queued:
        die(f"{args.sandbox} is already executing something: busy={health.busy} "
            f"queued={health.queued}. Concurrency is 1, so a second driver would steal slots.")
    for name, url in (("db-api", "http://127.0.0.1:8080/health"),
                      ("results-api", args.results_api + "/healthz")):
        try:
            if httpx.get(url, timeout=10).status_code != 200:
                die(f"{name} at {url} is not healthy; start it with scripts/dev-stack.sh up")
        except Exception as exc:
            die(f"{name} at {url} is not reachable: {exc}")

    user = "e2e-verify@example.org"
    session = f"sess-{uuid.uuid4().hex[:8]}"

    run = lambda **kw: asyncio.run(client.execute(user=user, session_id=session, **kw))

    # ----------------------------------------------------------------------------------
    print("identity: one value across chat-backend, /scratch, both tokens and every log")
    # ----------------------------------------------------------------------------------
    tokens = st.mint_execution_tokens(user=user, session_id=session)
    claims = {aud: jwt.decode(tok, signing_key, algorithms=["HS256"], audience=aud)
              for aud, tok in tokens.tokens.items()}
    check("identity: both audiences are minted, and they are different tokens",
          set(claims) == {"db-api", "results-api"}
          and tokens.tokens["db-api"] != tokens.tokens["results-api"])
    check("identity: both tokens' jti IS the execution id",
          all(c["jti"] == tokens.execution_id for c in claims.values()),
          f"{[c['jti'] for c in claims.values()]} vs {tokens.execution_id}")
    check("identity: both tokens carry the real sub and sid",
          all(c["sub"] == user and c["sid"] == session for c in claims.values()),
          str([(c["sub"], c["sid"]) for c in claims.values()]))

    with db_log, api_log, DockerLogTail(args.container_name) as dlog:
        result = run(code=IDENTITY_CODE, timeout_s=110)
    eid = result["execution_id"]
    report = report_field(result.get("output", ""), "REPORT") or {}

    check("identity: the execution completed ok",
          result["status"] == "ok", f"got {result['status']} {result.get('error')} "
                                    f"{result.get('output','')[-400:]}")
    check("identity: the SDK reached db-api and got rows back",
          report.get("db_rows") == 1, f"got {report.get('db_rows')} {report.get('db_error')}")
    check("identity: the SDK reached results-api and got rows back",
          (report.get("results_rows") or 0) > 0,
          f"got {report.get('results_rows')} {report.get('results_error')}")
    # `eid in scratch`, not `scratch == [eid]`. The latter asserts /scratch holds EXACTLY one
    # directory, which is a property of the CONTAINER — of what earlier executions left behind
    # inside the retention window — and not of this execution at all. It made a real run fail
    # with 21 retained directories present, and it makes the harness non-idempotent against
    # itself.
    check("identity: /scratch/<id> IS the execution id",
          report.get("artifacts_dir") == f"/scratch/{eid}/artifacts"
          and eid in (report.get("scratch") or []),
          f"got {report.get('artifacts_dir')} {report.get('scratch')}")
    check("identity: the child's environment carries the TOKEN's sub/sid/jti",
          (report.get("user"), report.get("session"), report.get("execution"))
          == (user, session, eid),
          f"got {report.get('user')} {report.get('session')} {report.get('execution')}")
    check("identity: the artifact reaches the manifest",
          [e["name"] for e in result["artifacts"]] == ["e2e.csv"], f"got {result['artifacts']}")

    db_hits = [r for r in db_log.records()
               if r.get("message") == "sandbox request authorized" and r.get("jti") == eid]
    check("join: db-api authorized the request AS A SANDBOX PRINCIPAL with this jti",
          bool(db_hits), f"no db-api record with jti {eid}")
    check("join: db-api's record carries the same sub and sid",
          bool(db_hits) and db_hits[0]["sub"] == user and db_hits[0]["sid"] == session,
          str(db_hits[:1]))
    api_hits = [r for r in api_log.records() if r.get("jti") == eid]
    check("join: results-api recorded the same jti and sid",
          bool(api_hits) and all(r.get("sid") == session for r in api_hits),
          f"{len(api_hits)} records with jti {eid}")

    audits = audit_records(dlog.text())
    mine = [a for a in audits if a["execution"] == eid]
    check("audit: the SDK's calls reach the CONTAINER's stdout in the analyzer's own shape",
          len(mine) >= 2, f"got {len(mine)} of {len(audits)} parsed records")
    check("audit: every record carries the real sub/sid/jti, never 'unknown'",
          bool(mine) and all(a["user"] == user and a["session"] == session for a in mine),
          str(mine[:1]))
    check("audit: the function names are the SDK calls that were actually made",
          {"sql", "search"} <= {a["function"] for a in mine}, str({a["function"] for a in mine}))

    # ----------------------------------------------------------------------------------
    print("results-api accounting: the request is IN the per-execution map, not merely served")
    # ----------------------------------------------------------------------------------
    with api_log:
        conc = run(code=CONCURRENCY_CODE, timeout_s=110)
    ceid = conc["execution_id"]
    outcomes = report_field(conc.get("output", ""), "CONCURRENCY") or []
    records = api_log.records()
    rejections = [r for r in records
                  if r.get("jti") == ceid
                  and r.get("message") == "sandbox per-execution limit exceeded"]
    admitted = [r for r in records
                if r.get("jti") == ceid and r.get("log_type") == "endpoint_access"]
    check("accounting: the concurrent run completed",
          conc["status"] == "ok", f"got {conc['status']} {conc.get('error')} "
                                  f"{conc.get('output','')[-300:]}")
    check("accounting: the SDK's requests are IN the map — results-api recorded this jti",
          bool(admitted), f"no endpoint_access record carried jti {ceid}")
    check("accounting: results-api REFUSED some of them FROM the per-execution map",
          bool(rejections),
          "no `sandbox per-execution limit exceeded` record; a caller that is never admitted "
          "is never rejected either — that is the 0lf shape")
    check("accounting: the script saw the 429s rather than silent success",
          any("error" in o for o in outcomes), str(outcomes)[:300])
    check("accounting: the admitted ones still succeeded",
          any("ok" in o for o in outcomes), str(outcomes)[:300])

    # WHICH limit fired is not in results-api's log — its JSON formatter carries `sid` and
    # `jti` from the `extra` and drops `code`, `limit` and `observed`, so `Rejection.code`
    # never reaches an operator (measured 2026-08-17; filed as a finding on results-api, not
    # worked around here). It IS on the wire, in the 429 body, so read it there. A token minted
    # for a fresh execution id and driven directly is not the SDK's request — the check above
    # is what covers the SDK — but it is the only place the code is observable.
    probe = st.mint_execution_tokens(user=user, session_id=session)

    async def _hammer():
        async with httpx.AsyncClient(base_url=args.results_api, timeout=60) as c:
            async def one(term):
                return await c.get("/api/v1/search",
                                   params={"q": term, "types": "phenotypes", "limit": 5},
                                   headers={"Authorization": f"Bearer {probe.results_api}"})
            return await asyncio.gather(*(one(t) for t in
                                          ["APOE", "TP53", "BRCA1", "IL7R", "PCSK9", "LDLR",
                                           "APOB", "MTHFR", "CFTR", "HBB", "F5", "VWF"]))

    bodies = [r.json() for r in asyncio.run(_hammer()) if r.status_code == 429]
    check("accounting: the 429 names WHICH per-execution limit fired",
          bool(bodies) and any(b.get("code") == "sandbox_concurrency" for b in bodies),
          str(bodies[:1]) or "no 429 at all")

    # The negative control, measured in the same run: the credential that is served with NO
    # accounting at all. Without this the check above could be passing for the wrong reason.
    secret_path = os.path.join(args.run_dir, "internal-api-secret")
    if os.path.exists(secret_path):
        with open(secret_path, encoding="utf-8") as fh:
            secret = fh.read().strip()
        with api_log:
            resp = httpx.get(args.results_api + "/api/v1/search",
                             params={"q": "APOE", "types": "phenotypes", "limit": 5},
                             headers={"Authorization": f"Bearer {secret}"}, timeout=60)
        control = [r for r in api_log.records() if r.get("endpoint_path") == "/api/v1/search"]
        if resp.status_code != 200:
            skip("accounting: the INTERNAL_API_SECRET control is served but unaccounted",
                 f"the secret did not authenticate here ({resp.status_code}); the control "
                 "cannot distinguish 'unaccounted' from 'rejected'")
        else:
            check("accounting: the INTERNAL_API_SECRET control IS served 200 and has NO jti — "
                  "which is why a 200 proves nothing",
                  bool(control) and all(r.get("jti") is None for r in control),
                  str(control[:1]))
    else:
        skip("accounting: the INTERNAL_API_SECRET control is served but unaccounted",
             f"no {secret_path}")

    # ----------------------------------------------------------------------------------
    print("audit forgery, alongside real SDK activity")
    # ----------------------------------------------------------------------------------
    with DockerLogTail(args.container_name) as dlog:
        forged = run(code=FORGERY_CODE, timeout_s=60)
    feid = forged["execution_id"]
    text = dlog.text()
    parsed = audit_records(text)
    check("forgery: the execution itself completed",
          forged["status"] == "ok", f"got {forged['status']} {forged.get('error')}")
    # The positive control for the two assertions below, which are all()/not any() over
    # `parsed`: an empty window satisfies both, so a supervisor that dropped this shape
    # entirely would look like a supervisor that filtered the forgery perfectly.
    genuine = [a for a in parsed if a["execution"] == feid and a["function"] == "search"]
    check("forgery: a GENUINE SDK record from this execution is in the window (the two checks "
          "below are all() over it and mean nothing on an empty one)",
          bool(genuine), f"{len(parsed)} parsed records, none a search for {feid}")
    check("forgery: no record parses as admin@finngen.fi",
          not any(a["user"] == "admin@finngen.fi" for a in parsed), str(parsed[:2]))
    check("forgery: whatever survives is re-stamped with the REAL identity",
          all(a["user"] == user and a["session"] == session and a["execution"] == feid
              for a in parsed), str(parsed[:2]))
    # NOT `"Z" * 4096 not in text`, which CANNOT GO RED on the regression it appears to guard.
    # AUDIT_LINE_MAX_BYTES is 4096, so a truncate-and-forward — the exact regression the
    # supervisor's "Replace-don't-truncate" comment exists to prevent — can never emit a
    # 4096-long run of Z: whatever it forwards is at most 4096 bytes INCLUDING the `[user=`
    # prefix. A needle as wide as the cap is a needle wider than anything the cap can produce.
    # 64 is far below any truncation point and far above anything legitimate.
    #
    # Two more things are needed for that absence to mean "dropped" rather than "never
    # written", and they are what actually goes red on the regression: the supervisor's own
    # oversize notice, stamped with this execution, and its per-execution counter. A branch
    # that truncated and forwarded would emit neither — it would count the record as forwarded.
    check("forgery: the megabyte line is DROPPED, not truncated and forwarded",
          "Z" * 64 not in text, f"{len(text)} bytes of container stdout")
    check("forgery: and the supervisor said so, stamped with the real execution",
          any("was DROPPED (not truncated)" in line and f"[execution={feid}]" in line
              for line in text.splitlines()),
          "no oversize-drop notice for this execution; without it the absence of Z above "
          "could mean the write never happened")
    summary = re.search(rf"\[execution={re.escape(feid)}\] SDK audit stream: records=(\d+) "
                        r"dropped_rate=\d+ dropped_oversize=(\d+)", text)
    check("forgery: the supervisor COUNTED it as dropped-oversize, not as forwarded",
          bool(summary) and int(summary.group(2)) >= 1 and int(summary.group(1)) >= 1,
          f"summary line: {summary.group(0) if summary else None}")

    # ----------------------------------------------------------------------------------
    print("limits: what chat-backend's own client receives when each one fires")
    # ----------------------------------------------------------------------------------
    def limited(label, code, timeout_s, want_status, want_type, max_wall):
        t0 = time.monotonic()
        try:
            body = run(code=code, timeout_s=timeout_s)
        except Exception as exc:  # a limit must never surface as a transport failure
            check(f"limit {label}: the client receives a result, not an exception", False,
                  f"{type(exc).__name__}: {exc}")
            return None
        elapsed = time.monotonic() - t0
        got_type = (body.get("error") or {}).get("type")
        check(f"limit {label}: status {want_status}, error.type {want_type}",
              body["status"] == want_status and got_type == want_type,
              f"got {body['status']} {got_type}")
        check(f"limit {label}: it returns rather than hangs (< {max_wall:.0f}s)",
              elapsed < max_wall, f"took {elapsed:.1f}s")
        return body

    limited("wall clock", "while True:\n    pass\n", 5, "timeout", "Timeout", 40)
    limited("8 MiB pipe cap",
            "import sys\nblock = 'x' * 65536\nfor _ in range(256):\n"
            "    sys.stdout.write(block)\nsys.stdout.flush()\n",
            60, "limit", "OutputLimit", 90)
    limited("artifact quota",
            "import os\nd = os.environ['SANDBOX_ARTIFACTS_DIR']\n"
            "block = b'\\0' * (4 * 1024 * 1024)\n"
            "for i in range(24):\n"
            "    with open(os.path.join(d, 'f%02d.bin' % i), 'wb') as fh:\n"
            "        fh.write(block)\n"
            "        fh.flush()\n"
            "        os.fsync(fh.fileno())\n"
            "    import time; time.sleep(0.1)\n",
            90, "limit", "ArtifactQuota", 120)

    chatty = run(code="print('HEADMARK')\nline = 'y' * 1023\n"
                      "for _ in range(200):\n    print(line)\nprint('TAILMARK')\n",
                 timeout_s=60)
    out = chatty.get("output", "")
    check("limit 64 KiB return cap: the run is ok and the client gets head AND tail",
          chatty["status"] == "ok" and out.startswith("HEADMARK")
          and out.rstrip().endswith("TAILMARK"), f"{out[:30]!r} ... {out[-30:]!r}")
    check("limit 64 KiB return cap: the middle is elided, visibly and counted",
          chatty["output_truncated"] is True and "bytes elided" in out
          and chatty["output_bytes"] > len(out.encode()),
          f"{chatty['output_bytes']} pre-cap, {len(out.encode())} returned")

    # ----------------------------------------------------------------------------------
    print("process-group kill, and what a setsid() grandchild does about it")
    # ----------------------------------------------------------------------------------
    # TWO RUNS. The assertion below is on the LIMIT path, which is where this bead's
    # process-group kill runs. The completing path is RECORDED rather than asserted here, and
    # the reason has changed: it used to be that a normal completion signalled nothing at all
    # (there were two _kill_group call sites and a reaped job reached neither), so asserting a
    # grandchild was gone would have been asserting a wish. Since 4h6.66 and 4h6.83 it DOES
    # signal — _kill_survivors on the group, then the fork server's subreaper sweep for
    # whatever setsid()'d out of it — and scripts/test-supervisor.py's `survivors` group
    # asserts exactly that, with a negative control. What is not established is the behaviour
    # under this harness's runtime, so the note stays a note until somebody runs it here.
    limit_tag = uuid.uuid4().hex[:4]
    done_tag = uuid.uuid4().hex[:4]
    killed = run(code=grandchild_code(limit_tag, spin=True), timeout_s=5)
    completed = run(code=grandchild_code(done_tag, spin=False), timeout_s=30)
    scan = run(code=SURVIVOR_SCAN_CODE, timeout_s=30)
    survivors = report_field(scan.get("output", ""), "SURVIVORS") or []
    killed_alive, killed_dead = grandchildren(survivors, limit_tag)
    done_alive, done_dead = grandchildren(survivors, done_tag)

    check("process group: the limit path did fire the wall clock — the only thing that "
          "signals the group at all", killed["status"] == "timeout",
          f"got {killed['status']}; _kill_group is called from _fire_limit and nowhere else, "
          "so without a limit nothing below is being tested")
    # THE GUARD. An absence observed after nothing happened is not evidence, and a guard on
    # "did fork() return" is not a guard: it reports success for a child that died before it
    # was ever anything. What is asserted here is that a process CARRYING THE MARKER EXISTED —
    # the spawner read the name back out of /proc/<pid>/comm, so this is an observation of the
    # object the scan then looks for, in the same namespace, by the same key.
    marked = [s for s in (report_field(killed.get("output", ""), "SPAWNED") or [])
              if s.get("comm") == f"E2E-{limit_tag}-{s.get('kind')}"]
    check("process group: both grandchildren EXISTED AND CARRIED THE MARKER before the kill "
          "(read back out of /proc, not inferred from fork())",
          len(marked) == 2,
          f"{len(marked)} marked of {report_field(killed.get('output',''), 'SPAWNED')}")
    if len(marked) != 2:
        skip("process group: a grandchild IN the group does not outlive a limit kill",
             "no marked grandchild was ever observed, so its absence proves nothing")
    else:
        check("process group: a grandchild IN the group does not outlive a limit kill",
              "G" not in killed_alive,
              f"still alive: {killed_alive.get('G')} (reaped-away: {sorted(killed_dead)})")

    # NOT PASS/FAIL, EITHER OF THEM. 4h6.55 has measured a setsid() descendant escaping the
    # group kill; asserting it does not would assert the comfortable answer, and asserting it
    # does would fail this harness on a property nobody has claimed. The completing path is
    # here for the same reason in reverse: what it shows is a consequence of _kill_group's only
    # call site, not a defect this harness gets to decide about.
    print(f"  note  NOT AN ASSERTION — after the LIMIT kill the setsid() grandchild is "
          f"{'RESIDENT' if 'D' in killed_alive else 'gone'}: alive={sorted(killed_alive)} "
          f"zombie={sorted(killed_dead)}. A killpg cannot reach it by construction; what is "
          "supposed to reach it now is the fork server's subreaper sweep at the END of the "
          "execution (genetics-results-suite-4h6.83), so RESIDENT here means the sweep did "
          "not run or did not take under this runtime — worth chasing, not a pass/fail.")
    print(f"  note  NOT AN ASSERTION — the NORMALLY-COMPLETING execution left "
          f"alive={sorted(done_alive)} zombie={sorted(done_dead)}; status={completed['status']}. "
          "Since 4h6.66 and 4h6.83 both grandchildren are supposed to be GONE here, which is "
          "asserted with a negative control in scripts/test-supervisor.py's `survivors` group; "
          "this stays a note only because that behaviour is unverified under this runtime.")

    # ----------------------------------------------------------------------------------
    print("the signing key: unset must fail the execution, wrong must be refused")
    # ----------------------------------------------------------------------------------
    class _CountingTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.calls = 0

        async def handle_async_request(self, request):
            self.calls += 1
            raise AssertionError("a request left chat-backend with no signing key configured")

    spy = _CountingTransport()
    get_settings.cache_clear()
    saved = os.environ.pop("SANDBOX_TOKEN_SIGNING_KEY")
    try:
        unkeyed = SandboxClient(args.sandbox, transport=spy)
        try:
            asyncio.run(unkeyed.execute(code="print(1)", user=user, session_id=session))
        except st.SandboxTokenUnavailable as exc:
            check("signing key: an unset key FAILS the execution by name",
                  "SANDBOX_TOKEN_SIGNING_KEY" in str(exc), str(exc))
        except Exception as exc:
            check("signing key: an unset key FAILS the execution by name", False,
                  f"raised {type(exc).__name__}: {exc}")
        else:
            check("signing key: an unset key FAILS the execution by name", False,
                  "the execution was submitted anyway")
        check("signing key: nothing was sent — no fallback to the shared secret, and no "
              "uncredentialed request", spy.calls == 0, f"{spy.calls} requests left the client")

        os.environ["SANDBOX_TOKEN_SIGNING_KEY"] = "wrong-key-" + uuid.uuid4().hex
        get_settings.cache_clear()
        with db_log, api_log:
            bad = SandboxClient(args.sandbox)
            wrong = asyncio.run(bad.execute(code=WRONG_KEY_CODE, user=user,
                                            session_id=session, timeout_s=60))
    finally:
        os.environ["SANDBOX_TOKEN_SIGNING_KEY"] = saved
        get_settings.cache_clear()

    weid = wrong["execution_id"]
    verdict = report_field(wrong.get("output", ""), "WRONGKEY") or {}
    check("signing key: a token signed with the WRONG key is refused by db-api",
          "401" in str(verdict.get("db", "")), f"got {verdict.get('db')!r}")
    check("signing key: a token signed with the WRONG key is refused by results-api",
          "401" in str(verdict.get("results", "")), f"got {verdict.get('results')!r}")
    check("signing key: neither backend recorded a principal for that execution",
          not any(r.get("jti") == weid for r in db_log.records() + api_log.records()),
          f"a record carried jti {weid}")

    # ----------------------------------------------------------------------------------
    print("artifact retention")
    # ----------------------------------------------------------------------------------
    # The boundary is [TTL, TTL + REAPER_POLL_S], because SANDBOX_RETENTION_S shortens the
    # deadline and NOT the reaper's poll interval. Absence is asserted only after TTL + one
    # full poll; anything tighter is a flaky test dressed up as a control. Same shape as
    # test-supervisor.py::test_retention_expiry, deliberately.
    #
    # WHAT IS AND IS NOT ESTABLISHED BY THE PRESENCE SIDE. A 409 probed the instant the
    # execution returns is satisfied by ANY positive retention — one second would do — so on
    # its own it pins nothing about the deadline, and combined with a --retention-s that does
    # not match the container both sides go green while the claimed boundary is never tested.
    # Hence: the TTL is READ OFF THE CONTAINER (never the flag), the probe waits until half of
    # it has elapsed, and if the TTL cannot be established the group skips by name instead of
    # asserting. Even so this shows "still there at TTL/2", not "still there at TTL-epsilon" —
    # the interval between the last probe and the deadline is unmeasured, and it is the reaper,
    # not this harness, that owns it.
    retention_s, shipped_s = _container_retention(args.container_name)
    reaper_poll_s = _source_constant(os.path.join(ROOT, "sandbox", "supervisor.py"),
                                     "REAPER_POLL_S")
    if args.retention_s is not None:
        check("retention: the container's EFFECTIVE retention is the one --retention-s claims",
              retention_s == args.retention_s,
              f"the container reports {retention_s!r}, the caller asserted "
              f"{args.retention_s}; every retention claim below would be about the wrong number")
    if reaper_poll_s is None:
        skip("retention: artifacts survive to the deadline and are gone after it",
             "REAPER_POLL_S could not be read out of sandbox/supervisor.py, so the absence "
             "side has no sound deadline to wait for")
    elif retention_s is None:
        skip("retention: artifacts survive to the deadline and are gone after it",
             f"the effective SANDBOX_RETENTION_S of {args.container_name} could not be "
             "established from `docker inspect` and the supervisor's own startup log, and "
             "asserting a boundary whose value is unknown would measure nothing")
    elif retention_s == "default":
        skip("retention: artifacts survive to the deadline and are gone after it",
             f"{args.container_name} runs the shipped retention (SANDBOX_RETENTION_S unset); "
             "restart it with SANDBOX_RETENTION_S=45 scripts/run-sandbox-local.sh --no-build")
    elif retention_s < 20:
        skip("retention: artifacts survive to the deadline and are gone after it",
             f"the container's SANDBOX_RETENTION_S={retention_s} leaves no room to probe "
             "presence at a point that discriminates it from a one-second TTL")
    else:
        print(f"  note  the container's own SANDBOX_RETENTION_S is {retention_s}s, which it "
              f"logged as shortening the shipped {shipped_s}s; reaper poll {reaper_poll_s}s")
        keep = run(code="import os\n"
                        "open(os.path.join(os.environ['SANDBOX_ARTIFACTS_DIR'], 'r.csv'), "
                        "'w').write('x')\n", timeout_s=30)
        written_at = time.monotonic()
        reid = keep["execution_id"]
        check("retention: the execution completed with an artifact",
              keep["status"] == "ok" and [e["name"] for e in keep["artifacts"]] == ["r.csv"],
              f"got {keep['status']} {keep.get('artifacts')}")
        # Presence, from the wire: a retained directory keeps its id taken, and the supervisor
        # answers 409 DuplicateExecutionId. Sound only while nothing else is retaining
        # concurrently — the aggregate ceiling can evict early — which is why this harness
        # refuses to start against a busy sandbox.
        time.sleep(max(0.0, written_at + retention_s / 2 - time.monotonic()))
        held = asyncio.run(_raw_status(httpx, args.sandbox, tokens_for(st, user, session, reid)))
        check(f"retention: {retention_s / 2:.0f}s in — half the container's own TTL — the "
              "artifacts are still there (id still taken)", held == 409, f"got {held}")
        time.sleep(max(0.0, written_at + retention_s + reaper_poll_s + 5 - time.monotonic()))
        gone = asyncio.run(_raw_status(httpx, args.sandbox, tokens_for(st, user, session, reid)))
        check("retention: past the deadline plus one reaper poll they are gone (id reusable)",
              gone == 200, f"got {gone}")

    print()
    print(f"invocation: {' '.join(sys.argv)}")
    if SKIPPED:
        # A SKIP IS NOT A PASS, and a count of passing checks quoted without this list is a
        # claim about a run that did not happen. Named this loudly so a green exit cannot be
        # read as "everything was measured".
        print(f"NOT MEASURED ({len(SKIPPED)}) — this run did not verify:")
        for line in SKIPPED:
            print(f"  - {line}")
    if FAILURES:
        print(f"FAILED {len(FAILURES)}/{CHECKS} checks:")
        for line in FAILURES:
            print(f"  - {line}")
        if SKIPPED:
            print(f"...and {_properties(SKIPPED)} above NOT MEASURED AT ALL.")
        return 1
    if SKIPPED:
        print(f"PARTIAL: {CHECKS} checks passed, but {_properties(SKIPPED)} above "
              "NOT MEASURED (listed by name). This is not a full run.")
    else:
        print(f"OK: {CHECKS} checks passed, nothing skipped.")
    return 0


def _properties(items):
    return f"{len(items)} propert{'y was' if len(items) == 1 else 'ies were'}"


def tokens_for(st, user, session, execution_id):
    return st.mint_execution_tokens(user=user, session_id=session, execution_id=execution_id)


async def _raw_status(httpx, base_url, tokens):
    """Submit a trivial script under an EXISTING execution id and return only the status code.

    Deliberately not through SandboxClient: its retry policy re-mints a fresh id on some
    failures, and re-using the id is the entire point of this probe."""
    body = {"code": "pass", "execution_id": tokens.execution_id, "tokens": tokens.tokens,
            "user": tokens.user, "session_id": tokens.session_id, "timeout_s": 20}
    async with httpx.AsyncClient(base_url=base_url, timeout=60) as client:
        return (await client.post("/execute", json=body)).status_code


if __name__ == "__main__":
    sys.exit(main())
