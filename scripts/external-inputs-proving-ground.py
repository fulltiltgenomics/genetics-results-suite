#!/usr/bin/env python3
"""Proving ground for external files reaching an analysis: fetch, deliver, run — offline.

This is the ACCEPTANCE TEST for the external-inputs feature, written before the feature. It
is RED against a tree that does not have it, and that is the deliverable: a green run means
the whole path works, and the checks below are the definition of "works".

WHY IT CAN BE OFFLINE AT ALL. The file the feature exists for is already in the tree:
genetics-mcp-server/tests/fixtures/linemodels/covid_hgi_v6_B2_C2_common.tsv, the R-reference
fixture for sdk/linemodels.py, whose GitHub source is named in the fixture README. So the
motivating case is rehearsable against the real bytes with no internet, no cluster and no
credentials. This harness starts a throwaway HTTP server on loopback serving exactly that
file, stops it when it is done, and never opens a socket to anything else.

  scripts/external-inputs-proving-ground.py --fetcher-url http://127.0.0.1:8090
  scripts/external-inputs-proving-ground.py --fetcher-url ... --fetcher-url-guarded ...
  scripts/external-inputs-proving-ground.py --groups ground          # the harness itself

Exit 0 every check passed; 1 at least one check failed (the expected state today); 2 the
harness could not run — a missing precondition, never a verdict about the feature. Same
0/1/2 convention as gen-sandbox-docs.py and test-sandbox-docs.py.

PRECONDITIONS, which are about the stack and not about the feature:
  * the sandbox container from `scripts/run-sandbox-local.sh --no-build`, running the
    working tree's sandbox/supervisor.py byte for byte. A container left over from an older
    build passes or fails these checks about a different program, so it is refused.
  * SANDBOX_TOKEN_SIGNING_KEY, or `scripts/dev-stack.sh` having generated one into its run
    directory. Nothing else from dev-stack.sh is required: the model-driven half of the
    premise cannot run offline (see NOT MEASURED).

PROVISIONAL NAMES. Every interface below is named here before it exists, so that the driver
goes green when the subtasks land instead of being rewritten. The names are the harness's
guess at the chosen architecture (one optional field on /execute, a read-only inputs
directory, one env var, nothing persisted); the subtask that builds each one owns the final
spelling and must fix it here in the same change:

  `inputs` on POST /execute, a list of {"name", "content_b64"}   -> supervisor subtask
  SANDBOX_INPUTS_DIR, the child env var naming a 0500 directory  -> supervisor subtask
  POST /fetch {"url"} -> {"name", "size_bytes", "content_b64"}   -> URL fetcher subtask
  a refusal as non-2xx with {"error": {"type", "message"}}       -> URL fetcher subtask
  the fetcher's health path, probed as /healthz then /health     -> URL fetcher subtask
  `genetics.open_input(name)`                                    -> SDK helper subtask
  `inputs=` on SandboxClient.execute                             -> client subtask

THE LOOPBACK PROBLEM, and why there are two fetcher URLs. The fetcher's job is to refuse
loopback, and this harness serves its fixture over loopback. Both cannot hold at once, so
the local rehearsal runs a fetcher whose guard has been told to permit loopback — that
allowance is a development affordance and must never be settable in the deployed manifest,
which is the fetcher subtask's problem to enforce and not something this file can check.
`--fetcher-url` is that permissive instance and is what the delivery checks fetch through.
`--fetcher-url-guarded` is an instance running the DEPLOYED configuration; given one, the
harness measures that it refuses the very same loopback URL. Without one that check FAILS
rather than skipping: loopback is named in the epic's kill criterion (b) beside metadata and
RFC1918, so a green run with loopback unmeasured would discharge a criterion it never
touched. The address classes a loopback allowance must never
unlock — 169.254.169.254, RFC1918, and a redirect from a permitted host into either — are
measured against the permissive instance, because they are exactly the ones no development
affordance may reach.

NOT MEASURED HERE, and not claimed:
  * the premise end to end, i.e. the model asked the question once and the run happened with
    no manual step. That needs a model API call and a real conversation; it is a staging
    measurement, and it is the epic's success criterion rather than this file's.
  * the fetcher's NetworkPolicy, its service account, and that its pod holds none of
    chat-backend's secrets. Docker gives none of those a local form.
  * the per-user fetch cache in chat-backend: it is in-memory and invisible on the wire, so
    nothing observable from here distinguishes a hit from a second fetch.
  * a large file against the body cap. The fixture is ~5.6 KiB; the cap conversation is the
    fetcher subtask's, measured where the cap lives.
"""

import argparse
import base64
import hashlib
import http.server
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import threading
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_REL = os.path.join("tests", "fixtures", "linemodels", "covid_hgi_v6_B2_C2_common.tsv")
# the premise's own numbers, asserted rather than quoted: a fixture that has been regenerated
# is a different rehearsal and the run must say so instead of proving something about it
FIXTURE_BYTES = 5759
FIXTURE_LINES = 25
# must match dev-stack.sh's own default, so a developer who sets DEV_STACK_RUN_DIR does not
# also have to remember --run-dir here
DEFAULT_RUN_DIR = os.environ.get("DEV_STACK_RUN_DIR") or os.path.join(
    os.path.expanduser("~"), ".cache", "genetics-dev-stack")

INPUTS_FIELD = "inputs"
INPUTS_ENV = "SANDBOX_INPUTS_DIR"
LOOPBACK_CHECK = "a deployed-configuration fetcher refuses loopback"
DIR_MODE_CHECK = "the inputs directory refuses a plain write by the child"

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
# the throwaway origin server
# --------------------------------------------------------------------------------------

class _Origin(http.server.BaseHTTPRequestHandler):
    """Three paths, no state, loopback only. The redirect target is a literal rather than a
    name so that resolving it cannot reach a resolver."""

    payload = b""

    def do_GET(self):
        if self.path == "/covid_hgi_v6_B2_C2_common.tsv":
            self.send_response(200)
            self.send_header("Content-Type", "text/tab-separated-values")
            self.send_header("Content-Length", str(len(self.payload)))
            self.end_headers()
            self.wfile.write(self.payload)
        elif self.path == "/redirect-to-metadata":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/computeMetadata/v1/")
            self.end_headers()
        elif self.path == "/redirect-to-rfc1918":
            self.send_response(302)
            self.send_header("Location", "http://10.0.0.1/")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


class Origin:
    def __init__(self, payload):
        _Origin.payload = payload
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
        self.host, self.port = self.server.server_address[:2]

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)

    def url(self, path):
        return f"http://{self.host}:{self.port}{path}"


def http_json(url, body=None, timeout=30):
    """(status, decoded-json-or-raw-text). Never raises for a non-2xx: a refusal IS the
    observation in half the checks here, so its body must survive to the assertion."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read(), exc.code
    except (urllib.error.URLError, OSError) as exc:
        return None, str(exc)
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, raw


def reachable(url, path):
    """The spelling is per service, not per suite: db-api and the sandbox supervisor serve
    /health, every FastAPI service here serves /healthz (dev-stack.sh's `svc_health`). One
    hardcoded spelling makes a live service look down and its skip message a false statement."""
    status, _ = http_json(url.rstrip("/") + path)
    return status == 200


# the fetcher does not exist yet, so which spelling it will serve is not knowable here and
# guessing wrong reds all eight fetcher checks for a reason that reads as a missing guard
FETCHER_HEALTH_PATHS = ("/healthz", "/health")


def reachable_any(url, paths):
    return any(reachable(url, path) for path in paths)


# --------------------------------------------------------------------------------------
# the container under test
# --------------------------------------------------------------------------------------

def container_file(name, path):
    """One file out of the RUNNING container, as ("file", bytes) | ("absent", why) |
    ("unknown", why). `docker cp` streams a tar and works on a distroless image, where
    `docker exec` has no shell to run.

    The three-way answer is the point: "the file is not there" and "I could not look" are
    opposite evidence, and collapsing them into None turns a missing docker, a stopped
    container or a mistyped path into a passing absence check."""
    try:
        proc = subprocess.run(["docker", "cp", f"{name}:{path}", "-"],
                              capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return "unknown", f"docker cp failed: {exc}"
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        lowered = err.lower()
        if "no such file or directory" in lowered or "could not find the file" in lowered:
            return "absent", err
        return "unknown", err or f"docker cp exited {proc.returncode}"
    try:
        with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tf:
            member = tf.next()
            if member is None or not member.isfile():
                return "unknown", "docker cp returned no regular file"
            handle = tf.extractfile(member)
            if handle is None:
                return "unknown", "docker cp member could not be extracted"
            return "file", handle.read()
    except tarfile.TarError as exc:
        return "unknown", f"docker cp stream is not a readable tar: {exc}"


def verify_container_source(name):
    """The container must run THE SOURCE UNDER TEST, or nothing below means anything.

    `genetics-sandbox-local` survives a rebuild, a rebase and a branch switch, and this
    harness is RED by design — a stale container turns "the feature is missing" and "the
    container predates it" into the same output, which is the one confusion the whole file
    exists to avoid."""
    local = os.path.join(ROOT, "sandbox", "supervisor.py")
    with open(local, "rb") as fh:
        want = hashlib.sha256(fh.read()).hexdigest()
    kind, got_bytes = container_file(name, "/genetics/supervisor.py")
    if kind != "file":
        die(f"cannot read /genetics/supervisor.py out of container {name} ({kind}: "
            f"{got_bytes}); without it there is no evidence the container runs the source "
            "under test")
    got = hashlib.sha256(got_bytes).hexdigest()
    if got != want:
        die(f"container {name} runs a DIFFERENT supervisor.py ({got[:12]}) from "
            f"sandbox/supervisor.py ({want[:12]}) — rebuild it with "
            "scripts/run-sandbox-local.sh")
    print(f"precondition: {name} runs sandbox/supervisor.py byte for byte")


def mcp_dir():
    """The genetics-mcp-server checkout matching this one — the sibling's worktree of the
    same name first, then its main checkout. Same resolution as run-sandbox-local.sh, and for
    the same reason: a worktree run must not silently rehearse master."""
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


def reexec_under_mcp_venv():
    """The token minter and the client live in the sibling checkout. Re-exec under its venv
    rather than asking the caller which interpreter has PyJWT — and assert the module
    resolves out of THAT tree, so an editable install of the main checkout cannot stand in
    for the branch under test."""
    mcp = mcp_dir()
    if mcp is None:
        die("no genetics-mcp-server checkout found beside this one")
    python = os.path.join(mcp, ".venv", "bin", "python")
    if not os.path.exists(python):
        die(f"{python} is missing; create the checkout's venv first")
    if os.environ.get("_INPUTS_PG_REEXEC") != "1":
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(mcp, "src") + os.pathsep + env.get("PYTHONPATH", "")
        env["_INPUTS_PG_REEXEC"] = "1"
        os.execve(python, [python, os.path.abspath(__file__)] + sys.argv[1:], env)
    import genetics_mcp_server
    resolved = os.path.realpath(os.path.dirname(genetics_mcp_server.__file__))
    expected = os.path.realpath(os.path.join(mcp, "src", "genetics_mcp_server"))
    if resolved != expected:
        die(f"genetics_mcp_server resolves to {resolved}, not the tree under test {expected}")
    return mcp


def execute(sandbox_url, code, inputs=None, timeout_s=60):
    """POST /execute directly, not through SandboxClient: the client re-mints the execution
    id on a retry, and these checks join on the id the supervisor was given."""
    from genetics_mcp_server import sandbox_token as st
    tokens = st.mint_execution_tokens(user="inputs-proving-ground@example.org",
                                      session_id="inputs-pg-" + uuid.uuid4().hex[:8])
    body = {"code": code, "execution_id": tokens.execution_id, "tokens": tokens.tokens,
            "user": tokens.user, "session_id": tokens.session_id, "timeout_s": timeout_s}
    if inputs is not None:
        body[INPUTS_FIELD] = inputs
    status, payload = http_json(sandbox_url.rstrip("/") + "/execute", body, timeout=timeout_s + 30)
    return tokens.execution_id, status, payload


def reported(payload, prefix):
    """The value the child printed after `prefix `, or None. stdout is the only channel out
    of an execution that carries what the script observed."""
    if not isinstance(payload, dict):
        return None
    for line in str(payload.get("stdout", "")).splitlines():
        if line.startswith(prefix + " "):
            return line[len(prefix) + 1:].strip()
    return None


# --------------------------------------------------------------------------------------
# the scripts the sandbox runs
# --------------------------------------------------------------------------------------

READ_VIA_ENV = '''
import hashlib, os
d = os.environ.get({env!r})
print("ENV_SET " + ("yes" if d else "no"))
if d:
    print("DIR " + d)
    b = open(os.path.join(d, {name!r}), "rb").read()
    text = b.decode("utf-8")
    print("SHA " + hashlib.sha256(b).hexdigest())
    print("LINES " + str(len(text.splitlines())))
    print("COLS " + str(len(text.splitlines()[0].split("\\t"))))
    def probe_write():
        open(os.path.join(d, "probe-" + os.urandom(4).hex()), "w").write("x")
    try:
        probe_write()
        print("DIR_WRITABLE yes")
    except OSError:
        print("DIR_WRITABLE no")
    try:
        os.chmod(d, 0o700)
        probe_write()
        print("DIR_WRITABLE_AFTER_CHMOD yes")
    except OSError:
        print("DIR_WRITABLE_AFTER_CHMOD no")
'''

READ_VIA_SDK = '''
import hashlib
from genetics import open_input
with open_input({name!r}) as fh:
    b = fh.read()
if isinstance(b, str):
    b = b.encode("utf-8")
print("SHA " + hashlib.sha256(b).hexdigest())
print("LINES " + str(len(b.decode("utf-8").splitlines())))
'''


# --------------------------------------------------------------------------------------
# check groups
# --------------------------------------------------------------------------------------

def group_ground(origin, payload, digest):
    """The rehearsal ground itself. This group must be GREEN on a tree that has none of the
    feature: if it is not, every red below is about this harness rather than about the
    feature, and the run says nothing."""
    print("\n-- the rehearsal ground --")
    check("fixture is the premise's file", len(payload) == FIXTURE_BYTES
          and len(payload.decode("utf-8").splitlines()) == FIXTURE_LINES,
          f"{len(payload)} bytes / {len(payload.decode('utf-8').splitlines())} lines, "
          f"expected {FIXTURE_BYTES} / {FIXTURE_LINES}")
    check("origin binds loopback only", origin.host == "127.0.0.1", origin.host)
    status, served = http_json(origin.url("/covid_hgi_v6_B2_C2_common.tsv"))
    served_bytes = served if isinstance(served, bytes) else b""
    check("origin serves the fixture byte for byte", status == 200
          and hashlib.sha256(served_bytes).hexdigest() == digest,
          f"status {status}, {len(served_bytes)} bytes")
    status, _ = http_json(origin.url("/nothing-here"))
    check("origin serves nothing it was not given", status == 404, f"status {status}")


def group_fetcher(args, origin, digest):
    """The fetcher surface, and the kill criterion's address classes. RED until the fetcher
    subtasks land."""
    print("\n-- the fetcher --")
    spellings = " or ".join(FETCHER_HEALTH_PATHS)
    if not reachable_any(args.fetcher_url, FETCHER_HEALTH_PATHS):
        for name in ("fetcher answers a health path",
                     "fetcher returns the fixture byte for byte",
                     "fetcher declares the size it returned",
                     "fetcher refuses 169.254.169.254", "fetcher refuses RFC1918",
                     "fetcher refuses a redirect into 169.254.169.254",
                     "fetcher refuses a redirect into RFC1918",
                     "fetcher refuses a non-http scheme"):
            check(name, False, f"nothing answers {spellings} at {args.fetcher_url}")
        # named even here: a check that disappears from the report when its precondition is
        # missing is indistinguishable from one that was never written. And it is a FAILURE
        # rather than a skip, because loopback is named in the epic's kill criterion (b)
        # alongside metadata and RFC1918 — a run that reports success with loopback
        # unmeasured discharges nothing.
        check(LOOPBACK_CHECK, False,
              f"nothing answers {spellings} at {args.fetcher_url} either, so there is "
              "nothing to compare")
        return None

    check("fetcher answers a health path", True)
    status, payload = http_json(args.fetcher_url.rstrip("/") + "/fetch",
                                {"url": origin.url("/covid_hgi_v6_B2_C2_common.tsv")})
    got = b""
    if isinstance(payload, dict) and isinstance(payload.get("content_b64"), str):
        try:
            got = base64.b64decode(payload["content_b64"])
        except ValueError:
            got = b""
    check("fetcher returns the fixture byte for byte",
          status == 200 and hashlib.sha256(got).hexdigest() == digest,
          f"status {status}, {len(got)} bytes")
    check("fetcher declares the size it returned",
          isinstance(payload, dict) and payload.get("size_bytes") == len(got),
          f"size_bytes={payload.get('size_bytes') if isinstance(payload, dict) else payload!r}")

    # A refusal must be a REFUSAL: a non-2xx carrying a type, not a 200 with empty content and
    # not a connection error, which is what an unreachable address looks like anyway and would
    # pass this check while the guard did nothing.
    def refuses(name, url):
        st, body = http_json(args.fetcher_url.rstrip("/") + "/fetch", {"url": url})
        typed = isinstance(body, dict) and isinstance(body.get("error"), dict) \
            and bool(body["error"].get("type"))
        check(name, isinstance(st, int) and 400 <= st < 500 and typed,
              f"status {st!r}, body {str(body)[:120]!r}")

    refuses("fetcher refuses 169.254.169.254",
            "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/")
    refuses("fetcher refuses RFC1918", "http://10.0.0.1/")
    refuses("fetcher refuses a redirect into 169.254.169.254",
            origin.url("/redirect-to-metadata"))
    refuses("fetcher refuses a redirect into RFC1918", origin.url("/redirect-to-rfc1918"))
    refuses("fetcher refuses a non-http scheme", "file:///etc/passwd")

    if not args.fetcher_url_guarded:
        check(LOOPBACK_CHECK, False,
              "no --fetcher-url-guarded, and the permissive local instance is told to allow "
              "loopback — so nothing here measured it")
    elif not reachable_any(args.fetcher_url_guarded, FETCHER_HEALTH_PATHS):
        check(LOOPBACK_CHECK, False,
              f"nothing answers {spellings} at {args.fetcher_url_guarded}")
    else:
        st, body = http_json(args.fetcher_url_guarded.rstrip("/") + "/fetch",
                             {"url": origin.url("/covid_hgi_v6_B2_C2_common.tsv")})
        typed = isinstance(body, dict) and isinstance(body.get("error"), dict)
        check(LOOPBACK_CHECK, isinstance(st, int) and 400 <= st < 500 and typed,
              f"status {st!r}, body {str(body)[:120]!r}")
    return got if got else None


def group_delivery(args, payload, digest, fetched):
    """Delivery into an execution: the /execute field, the directory, the env var, and that
    nothing survives the run. RED until the supervisor subtask lands."""
    print("\n-- delivery into an execution --")
    # The fetcher's bytes when there are any, the fixture's otherwise: the delivery half must
    # stay measurable while the fetcher does not exist, or the two reds mask each other.
    body = fetched if fetched is not None else payload
    name = os.path.basename(FIXTURE_REL)
    inputs = [{"name": name, "content_b64": base64.b64encode(body).decode()}]

    # The negative control, and it must come first. Every check below posts to /execute, and
    # /execute rejects an unknown field before it looks at code, execution_id or tokens — so
    # a stale token, a malformed signing key or clock skew arrives as the same red as "the
    # field is not implemented". This one execution carries no `inputs`, so it fails only for
    # those other reasons, and a red here means the group cannot attribute anything.
    _, control_status, control = execute(args.sandbox_url, 'print("CONTROL ok")')
    control_ok = control_status == 200 and reported(control, "CONTROL") == "ok"
    check("control: an execution with no inputs field runs", control_ok,
          f"status {control_status}, {str(control)[:160]!r}")
    if not control_ok:
        for pending in (f"/execute accepts an {INPUTS_FIELD!r} field",
                        f"the child sees {INPUTS_ENV}",
                        "the delivered bytes are the fetched bytes",
                        "the file reads as the premise's table",
                        DIR_MODE_CHECK,
                        "nothing is persisted after the execution",
                        "genetics.open_input reads the same bytes"):
            skip(pending, "the control execution did not run, so a red here would be about "
                          "the tokens or the container and not about the feature")
        return

    _, status, result = execute(
        args.sandbox_url, READ_VIA_ENV.format(env=INPUTS_ENV, name=name), inputs=inputs)
    check(f"/execute accepts an {INPUTS_FIELD!r} field", status == 200,
          f"status {status}, {str(result)[:160]!r}")
    check(f"the child sees {INPUTS_ENV}", reported(result, "ENV_SET") == "yes",
          f"ENV_SET={reported(result, 'ENV_SET')!r}")
    check("the delivered bytes are the fetched bytes",
          reported(result, "SHA") == digest, f"SHA={reported(result, 'SHA')!r}")
    check("the file reads as the premise's table",
          reported(result, "LINES") == str(FIXTURE_LINES)
          and (reported(result, "COLS") or "0").isdigit()
          and int(reported(result, "COLS") or 0) > 1,
          f"LINES={reported(result, 'LINES')!r} COLS={reported(result, 'COLS')!r}")
    # Deliberately NOT named read-only: ExecutionDirs.create states the governing fact — the
    # child shares the supervisor's uid — so a 0500 directory the child owns is chmod-able by
    # the child, and a mode is not a boundary against a hostile script. The probe measures
    # both: the plain write is what the mode stops, the chmod-then-write is what it does not.
    check(DIR_MODE_CHECK, reported(result, "DIR_WRITABLE") == "no",
          f"DIR_WRITABLE={reported(result, 'DIR_WRITABLE')!r}")
    print(f"  note  a hostile script chmods the directory first: "
          f"DIR_WRITABLE_AFTER_CHMOD={reported(result, 'DIR_WRITABLE_AFTER_CHMOD')!r} "
          "(a mode is not an isolation boundary here)")

    # An absence is only evidence if the thing was there: this runs after an execution that
    # reported reading the file, so the directory demonstrably existed inside the run. The
    # path comes from the child rather than from this file's guess at the layout.
    delivered = reported(result, "SHA") == digest
    child_dir = reported(result, "DIR")
    if not delivered:
        skip("nothing is persisted after the execution",
             "the input never reached the execution, so its absence proves nothing")
    elif not child_dir:
        skip("nothing is persisted after the execution",
             f"the child did not report its {INPUTS_ENV}, so there is no path to look at")
    else:
        kind, detail = container_file(args.container, os.path.join(child_dir, name))
        if kind == "unknown":
            skip("nothing is persisted after the execution",
                 f"could not look inside {args.container}: {detail}")
        else:
            check("nothing is persisted after the execution", kind == "absent",
                  f"{len(detail)} bytes still at {child_dir}/{name}"
                  if kind == "file" else "")

    _, status, result = execute(args.sandbox_url, READ_VIA_SDK.format(name=name), inputs=inputs)
    check("genetics.open_input reads the same bytes",
          status == 200 and reported(result, "SHA") == digest
          and reported(result, "LINES") == str(FIXTURE_LINES),
          f"status {status}, SHA={reported(result, 'SHA')!r}, "
          f"LINES={reported(result, 'LINES')!r}")


def group_client(args):
    """chat-backend's own client. Offline: the surface a conversation reaches the feature
    through must exist, or the path stops one hop short of the model."""
    print("\n-- chat-backend's client --")
    import inspect

    from genetics_mcp_server.sandbox_client import SandboxClient
    params = inspect.signature(SandboxClient.execute).parameters
    check(f"SandboxClient.execute takes {INPUTS_FIELD!r}", INPUTS_FIELD in params,
          f"parameters: {', '.join(params)}")

    # named for what /healthz establishes and nothing more: it returns {"status": "ok"} and
    # says nothing about the sandbox, so a name claiming otherwise is a false reason in the
    # run's own output
    check("chat-api answers /healthz", reachable(args.chat_api_url, "/healthz"),
          f"nothing answers /healthz at {args.chat_api_url}")
    # always stated, up or down: the model-driven half is out of reach either way, and a
    # check that silently vanishes when its precondition IS met is the failure this file
    # condemns elsewhere
    skip("the premise end to end",
         "the model-driven half needs a model API call and a real conversation; it is a "
         "staging measurement this harness will not make")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sandbox-url", default="http://127.0.0.1:8081",
                        help="the supervisor scripts/run-sandbox-local.sh publishes")
    parser.add_argument("--container", default=os.environ.get("SANDBOX_CONTAINER_NAME",
                                                              "genetics-sandbox-local"))
    parser.add_argument("--fetcher-url", default="http://127.0.0.1:8090",
                        help="the local fetcher, guard told to permit loopback")
    parser.add_argument("--fetcher-url-guarded", default=None,
                        help="a second fetcher running the DEPLOYED configuration, which must "
                             "refuse the loopback origin this harness serves")
    parser.add_argument("--chat-api-url", default="http://127.0.0.1:4000",
                        help="chat-api as scripts/dev-stack.sh starts it")
    parser.add_argument("--run-dir", default=DEFAULT_RUN_DIR,
                        help="dev-stack.sh's run directory, for the signing key")
    parser.add_argument("--groups", default="ground,fetcher,delivery,client",
                        help="comma-separated subset of the check groups")
    args = parser.parse_args()
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]

    mcp = reexec_under_mcp_venv()
    fixture = os.path.join(mcp, FIXTURE_REL)
    if not os.path.exists(fixture):
        die(f"{fixture} is missing; it is the premise's own file and the whole rehearsal")
    with open(fixture, "rb") as fh:
        payload = fh.read()
    digest = hashlib.sha256(payload).hexdigest()

    needs_sandbox = "delivery" in groups
    if needs_sandbox:
        if not os.environ.get("SANDBOX_TOKEN_SIGNING_KEY"):
            key_path = os.path.join(args.run_dir, "sandbox-token-signing-key")
            if not os.path.exists(key_path):
                die(f"SANDBOX_TOKEN_SIGNING_KEY is unset and {key_path} does not exist; "
                    "export one or run scripts/dev-stack.sh up once")
            with open(key_path) as fh:
                os.environ["SANDBOX_TOKEN_SIGNING_KEY"] = fh.read().strip()
        if not reachable(args.sandbox_url, "/health"):
            die(f"nothing answers /health at {args.sandbox_url}; start the container with "
                "scripts/run-sandbox-local.sh --no-build")
        verify_container_source(args.container)

    with Origin(payload) as origin:
        print(f"origin: {origin.url('/covid_hgi_v6_B2_C2_common.tsv')} "
              f"({len(payload)} bytes, sha256 {digest[:12]})")
        fetched = None
        if "ground" in groups:
            group_ground(origin, payload, digest)
        if "fetcher" in groups:
            fetched = group_fetcher(args, origin, digest)
        if "delivery" in groups:
            group_delivery(args, payload, digest, fetched)
        if "client" in groups:
            group_client(args)
        host, port = origin.host, origin.port

    # the origin is the one piece of infrastructure this harness owns; a run that leaves it
    # listening has left a socket serving a file on a developer's machine
    sock = socket.socket()
    sock.settimeout(2)
    stopped = sock.connect_ex((host, port)) != 0
    sock.close()
    check("the origin is stopped when the run ends", stopped, f"still listening on :{port}")

    print(f"\n{CHECKS - len(FAILURES)}/{CHECKS} checks passed")
    if SKIPPED:
        print("NOT MEASURED:")
        for item in SKIPPED:
            print(f"  - {item}")
    if FAILURES:
        print("FAILED:")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    # exit 1 is reserved for a red check. An unexpected exception — http.client.BadStatusLine
    # is not an OSError and walks straight out of http_json, for one — is the harness failing
    # to run, which the 0/1/2 convention spells 2.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"HARNESS: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
