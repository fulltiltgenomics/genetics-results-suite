#!/usr/bin/env python3
"""Offline assertions about sandbox/supervisor.py (genetics-results-suite-4h6.39).

Run: python3 scripts/test-supervisor.py                       in-process (the fast path)
     python3 scripts/test-supervisor.py --container URL       against a running container
Exit 0 = pass, 1 = a property is broken, 2 = the harness could not run.

No cluster, no credentials, no Docker image, no network beyond loopback. It runs the real
supervisor in this interpreter with SANDBOX_SCRATCH_ROOT pointed at a temporary directory
and forks real children, so the fork/reap path, the per-execution environment and the
artifact manifest are exercised rather than mocked.

CONTAINER MODE (genetics-results-suite-4h6.40) drives the SAME wire checks over HTTP
against a sandbox image started by scripts/run-sandbox-local.sh, plus a group that only
exists there: the read-only root filesystem, the pruned venv, the seeded matplotlib font
cache and the absence of credentials in the child's environment are all properties OF THE
IMAGE, and the in-process run has no image.

THE TWO COUNTS ARE NOT COMPARABLE, and the summary says so rather than leaving a reader to
assume it. Container mode runs the two wire groups and the image group only. Every group
that reaches into the supervisor's own objects — the startup assertions, request parsing,
the queue, the artifact manifest, the startup wipe — is NOT RUN AT ALL over HTTP, because
there is no route that reaches them; that is most of the in-process checks. They are named
in the run's closing "not run in this mode" list. skip() is the narrower mechanism: it
covers a check INSIDE a group that did run, and those are still counted and printed
individually. The in-process mode remains the fast path — it needs no build and no daemon —
and neither mode needs a cluster or a credential.

The properties under test are the ones where the two ends of the contract in
docs/code-execution-security.md section 2 could silently diverge — the queue-depth
definition, the duplicate-id refusal, reject-don't-clamp on timeout_s, the token
consistency rules, which names reach the manifest, and the Retry-After the client's retry
policy reads off a 429. A wire shape that is merely plausible is the failure mode this file
exists to catch.

Two checks are about the fork-without-exec model rather than the wire, because both were
reachable from a script: a forged status record on fd 3 must not turn exit 0 into
status "error", and a descendant that setsid()s away with the output pipe must not hold the
execution slot after the child is reaped.

EVERY SUPERVISOR LIMIT IS DEMONSTRATED FIRING, not reasoned about (4h6.41, 4h6.42, 4h6.46).
The `limits` group runs over the wire in BOTH modes, so each one is proved in the real image
as well as in-process: the wall clock, the 8 MiB pipe cap, the 64 KiB head-and-tail return
window (including that the cut never bisects a multi-byte character), RLIMIT_AS, the pid
budget's process-group kill, the per-execution artifact quota, the per-execution total quota,
and the aggregate retained ceiling's oldest-first eviction. A limit that is only asserted
about in a unit test is a limit nobody has watched fire.

RETENTION EXPIRY needs a clock nobody wants to wait on. In-process the harness starts a
supervisor with a shortened retention directly. In container mode it can only be run against
a container started with SANDBOX_RETENTION_S, so it takes `--retention-s N` to say so and
skips by name when that is absent rather than quietly proving less.

What is NOT covered, because it is not implemented: audit forwarding (4h6.45), which is a
comment block in supervisor.py naming its bead.
"""

import base64
import http.client
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sandbox"))

try:
    import supervisor as sup
except Exception as exc:  # pragma: no cover - harness failure
    print(f"HARNESS: cannot import sandbox/supervisor.py: {exc}", file=sys.stderr)
    sys.exit(2)

FAILURES = []
SKIPPED = []
NOT_RUN = []
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
    """A check that cannot run in this mode. Counted and printed separately: a skipped
    assertion silently omitted is how a mode ends up proving less than its output claims."""
    SKIPPED.append(f"{name}: {reason}")
    print(f"  skip  {name} ({reason})")


def expect_request_error(name, fn, status, type_):
    try:
        fn()
    except sup.RequestError as exc:
        check(name, exc.status == status and exc.type == type_, f"got {exc.status} {exc.type}")
    except Exception as exc:
        check(name, False, f"raised {type(exc).__name__}: {exc}")
    else:
        check(name, False, "no error raised")


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _b64(obj):
    raw = json.dumps(obj).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_token(aud, jti, sub, sid, exp=None):
    payload = {"aud": aud, "jti": jti, "sub": sub, "sid": sid}
    if exp is not None:
        payload["exp"] = exp
    return f"{_b64({'alg': 'HS256'})}.{_b64(payload)}.signature"


def make_body(code="print('hi')", execution_id=None, user="a@b.c", session_id="sess-1",
              exp=None, **overrides):
    eid = execution_id or str(uuid.uuid4())
    body = {
        "code": code,
        "execution_id": eid,
        "tokens": {
            aud: make_token(aud, eid, user, session_id, exp) for aud in sup.TOKEN_AUDIENCES
        },
        "user": user,
        "session_id": session_id,
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------------------
# 1. startup assertions
# --------------------------------------------------------------------------------------


def test_nsswitch(tmp):
    def write(text):
        path = os.path.join(tmp, f"nsswitch-{abs(hash(text))}.conf")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    ok = write("passwd: files\nhosts: files [!UNAVAIL=return] dns\n")
    try:
        sup.assert_nsswitch_hosts_files_first(ok)
        check("nsswitch: files before dns passes", True)
    except sup.StartupAssertionError as exc:
        check("nsswitch: files before dns passes", False, str(exc))

    for label, text in (
        ("dns before files", "hosts: dns files\n"),
        ("no files source", "hosts: myhostname dns\n"),
        ("no hosts line", "passwd: files\n"),
    ):
        try:
            sup.assert_nsswitch_hosts_files_first(write(text))
            check(f"nsswitch: {label} rejected", False, "accepted")
        except sup.StartupAssertionError:
            check(f"nsswitch: {label} rejected", True)

    try:
        sup.assert_nsswitch_hosts_files_first(os.path.join(tmp, "absent.conf"))
        check("nsswitch: missing file rejected", False, "accepted")
    except sup.StartupAssertionError:
        check("nsswitch: missing file rejected", True)


# --------------------------------------------------------------------------------------
# 2. request parsing and token consistency
# --------------------------------------------------------------------------------------


def test_parsing():
    raw = json.dumps(make_body()).encode()
    req = sup.parse_execute_request(raw)
    check("parse: defaults timeout_s to 60", req.timeout_s == sup.DEFAULT_TIMEOUT_S,
          f"got {req.timeout_s}")

    def parse(**kw):
        return lambda: sup.parse_execute_request(json.dumps(make_body(**kw)).encode())

    expect_request_error("parse: unknown field -> 400", parse(nonsense=1), 400, "UnknownField")
    expect_request_error("parse: empty code -> 400", parse(code="   "), 400, "InvalidRequest")
    expect_request_error(
        "parse: oversized code -> 413", parse(code="#" * (sup.MAX_CODE_BYTES + 1)),
        413, "PayloadTooLarge")
    expect_request_error(
        "parse: non-uuid execution_id -> 400",
        lambda: sup.parse_execute_request(json.dumps(
            dict(make_body(), execution_id="../etc")).encode()),
        400, "InvalidRequest")
    # `^...$` with re.match accepts a trailing newline, and this field becomes a directory
    # name and is echoed in the response, so it is a log-injection primitive as well as a
    # path one.
    for label, eid in (
        ("trailing newline", "11111111-1111-4111-8111-111111111111\n"),
        ("leading newline", "\n11111111-1111-4111-8111-111111111111"),
        ("trailing CRLF", "11111111-1111-4111-8111-111111111111\r\n"),
    ):
        body = make_body(execution_id=eid)
        body["tokens"] = {a: make_token(a, eid, "a@b.c", "sess-1") for a in sup.TOKEN_AUDIENCES}
        expect_request_error(
            f"parse: execution_id with a {label} -> 400",
            lambda b=body: sup.parse_execute_request(json.dumps(b).encode()),
            400, "InvalidRequest")

    expect_request_error("parse: timeout_s 121 rejected not clamped", parse(timeout_s=121),
                         400, "InvalidRequest")
    expect_request_error("parse: timeout_s 0 rejected", parse(timeout_s=0), 400, "InvalidRequest")

    # code measured on the UTF-8 encoding of the decoded string, not the JSON escaping
    multibyte = "x = '" + ("é" * (sup.MAX_CODE_BYTES // 2 - 10)) + "'"
    check("parse: code cap measured on decoded UTF-8",
          len(multibyte.encode()) <= sup.MAX_CODE_BYTES
          and sup.parse_execute_request(json.dumps(make_body(code=multibyte)).encode()) is not None)

    eid = str(uuid.uuid4())
    body = make_body(execution_id=eid)
    body["tokens"]["db-api"] = make_token("db-api", str(uuid.uuid4()), "a@b.c", "sess-1")
    expect_request_error("parse: differing jti -> 400",
                         lambda: sup.parse_execute_request(json.dumps(body).encode()),
                         400, "TokenMismatch")

    body = make_body()
    body["tokens"]["db-api"] = make_token("results-api", body["execution_id"], "a@b.c", "sess-1")
    expect_request_error("parse: aud not the key it was sent under -> 400",
                         lambda: sup.parse_execute_request(json.dumps(body).encode()),
                         400, "TokenMismatch")

    body = make_body()
    body["user"] = "someone-else@b.c"
    expect_request_error("parse: sub != user -> 400",
                         lambda: sup.parse_execute_request(json.dumps(body).encode()),
                         400, "TokenMismatch")

    body = make_body()
    body["session_id"] = "other"
    expect_request_error("parse: sid != session_id -> 400",
                         lambda: sup.parse_execute_request(json.dumps(body).encode()),
                         400, "TokenMismatch")

    body = make_body()
    body["tokens"]["extra"] = "x"
    expect_request_error("parse: extra token key -> 400",
                         lambda: sup.parse_execute_request(json.dumps(body).encode()),
                         400, "InvalidRequest")

    body = make_body()
    body["tokens"]["db-api"] = "not-a-jws"
    expect_request_error("parse: malformed token -> 400",
                         lambda: sup.parse_execute_request(json.dumps(body).encode()),
                         400, "InvalidToken")

    # jti must equal the body's execution_id, in both directions
    eid = str(uuid.uuid4())
    other = str(uuid.uuid4())
    body = make_body(execution_id=eid)
    body["tokens"] = {a: make_token(a, other, "a@b.c", "sess-1") for a in sup.TOKEN_AUDIENCES}
    expect_request_error("parse: jti != execution_id -> 400",
                         lambda: sup.parse_execute_request(json.dumps(body).encode()),
                         400, "TokenMismatch")


# --------------------------------------------------------------------------------------
# 3. the queue: depth is WAITING requests, not counting the one executing
# --------------------------------------------------------------------------------------


def test_queue(tmp):
    root = os.path.join(tmp, "queue-root")
    os.makedirs(root)
    s = sup.Supervisor(root, ready=True)

    def job():
        return sup.Job(sup.parse_execute_request(json.dumps(make_body()).encode()), None)

    running = job()
    s._admit(running)
    s._await_slot(running)
    waiting = [job(), job()]
    for j in waiting:
        s._admit(j)

    code, health = s.health()
    check("queue: /health busy while running", health["busy"] is True)
    check("queue: /health queued counts only waiting", health["queued"] == 2,
          f"got {health['queued']}")
    check("queue: a busy supervisor is healthy (200)", code == 200, f"got {code}")

    expect_request_error("queue: fourth request -> 429 Busy", lambda: s._admit(job()), 429, "Busy")

    dup = sup.Job(sup.parse_execute_request(
        json.dumps(make_body(execution_id=running.req.execution_id)).encode()), None)
    expect_request_error("queue: repeated execution_id (live) -> 409",
                         lambda: s._admit(dup), 409, "DuplicateExecutionId")

    s._release(running, retain=True)
    dup2 = sup.Job(sup.parse_execute_request(
        json.dumps(make_body(execution_id=running.req.execution_id)).encode()), None)
    expect_request_error("queue: repeated execution_id (retained) -> 409",
                         lambda: s._admit(dup2), 409, "DuplicateExecutionId")

    order = []
    for _ in waiting:
        nxt = s._waiting[0]
        s._await_slot(nxt)
        order.append(nxt)
        s._release(nxt, retain=False)
    check("queue: dequeued first in, first out", order == waiting)

    # bounded wait: the 429 arrives from waiting too long, not only from depth
    slow = job()
    s._admit(slow)
    s._await_slot(slow)
    late = job()
    s._admit(late)
    original = sup.MAX_QUEUED_WAIT_S
    sup.MAX_QUEUED_WAIT_S = 0.2
    late.enqueued_at = time.monotonic()
    try:
        expect_request_error("queue: maximum wait exceeded -> 429",
                             lambda: s._await_slot(late), 429, "Busy")
    finally:
        sup.MAX_QUEUED_WAIT_S = original
    s._release(slow, retain=False)

    # a not-ready supervisor is observable: the socket binds before prewarm runs
    starting = sup.Supervisor(root, ready=False)
    code, health = starting.health()
    check("queue: not ready -> 503 status starting",
          code == 503 and health["status"] == "starting", f"got {code} {health}")
    expect_request_error("queue: not ready -> 503 NotReady on /execute",
                         lambda: starting._admit(job()), 503, "NotReady")
    starting.draining = True
    check("queue: draining -> status draining", starting.health()[1]["status"] == "draining")

    # a draining supervisor refuses with 503 NotReady
    s.draining = True
    expect_request_error("queue: draining -> 503 NotReady", lambda: s._admit(job()), 503, "NotReady")
    s.draining = False

    # expired tokens are refused at dequeue, not at accept
    expired = sup.Job(sup.parse_execute_request(
        json.dumps(make_body(exp=int(time.time()) - 5)).encode()), None)
    expect_request_error("queue: expired tokens -> 409 TokenExpired",
                         lambda: s.run(expired), 409, "TokenExpired")
    check("queue: a refused dequeue leaves no directory",
          not os.path.exists(os.path.join(root, expired.req.execution_id)))


def test_peer_gone():
    a, b = socket.socketpair()
    check("disconnect: live peer is not gone", sup.peer_gone(a) is False)
    b.close()
    check("disconnect: closed peer is detected", sup.peer_gone(a) is True)
    a.close()


# --------------------------------------------------------------------------------------
# 4. the artifact manifest
# --------------------------------------------------------------------------------------


def test_manifest(tmp):
    d = os.path.join(tmp, "artifacts")
    os.makedirs(d)
    with open(os.path.join(d, "plot.png"), "wb") as fh:
        fh.write(b"\x89PNG" * 4)
    with open(os.path.join(d, "table.csv"), "w") as fh:
        fh.write("a,b\n")
    with open(os.path.join(d, "trailing.png "), "w") as fh:
        fh.write("x")
    with open(os.path.join(d, "new\nline.txt"), "w") as fh:
        fh.write("x")
    os.makedirs(os.path.join(d, "subdir"))
    with open(os.path.join(d, "subdir", "hidden.txt"), "w") as fh:
        fh.write("x")
    os.symlink("/etc/passwd", os.path.join(d, "link.txt"))
    os.link(os.path.join(d, "table.csv"), os.path.join(d, "hardlink.csv"))

    entries, omitted = sup.build_manifest(d)
    names = [e["name"] for e in entries]
    check("manifest: lists plain regular files", names == ["plot.png"], f"got {names}")
    check("manifest: content_type from the name",
          entries and entries[0]["content_type"] == "image/png")
    check("manifest: size from fstat", entries and entries[0]["size"] == 16)
    check("manifest: omits and counts the rest", omitted == 6, f"got {omitted}")
    check("manifest: no path, no execution id, no url",
          all(set(e) == {"name", "size", "content_type"} for e in entries))


# --------------------------------------------------------------------------------------
# 5. end to end over HTTP, with real forks
# --------------------------------------------------------------------------------------


class Server:
    """The supervisor running in THIS interpreter."""

    container = False

    def __init__(self, root, retention_s=None):
        os.environ[sup.ENV_SCRATCH_ROOT] = root
        self.retention_s = retention_s
        self.supervisor = sup.start(scratch_root=root, run_assertions=False,
                                    retention_s=retention_s)
        self.httpd = sup._Server(("127.0.0.1", 0), sup._Handler)
        self.host = "127.0.0.1"
        self.port = self.httpd.server_address[1]
        # What the supervisor calls /scratch, and where the harness can see it. They are the
        # same path here and they are NOT in a container, which is the whole difference
        # between the two modes.
        self.scratch_root = root
        self.host_scratch = root
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                       daemon=True)
        self.thread.start()

    def request(self, method, path, body=None, ctype="application/json"):
        # The contract's own client deadline is `max queued wait + timeout_s + margin` (255s at
        # timeout_s 120). A 30s socket timeout here would report "timed out" for a supervisor
        # that is about to answer — the exact misreading 4h6.47 was corrected for.
        conn = http.client.HTTPConnection(self.host, self.port, timeout=300)
        headers = {}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers = {"Content-Type": ctype, "Content-Length": str(len(payload))}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            parsed = json.loads(raw.decode())
        except Exception:
            parsed = None
        return resp.status, resp.getheader("Retry-After"), parsed

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class RemoteServer(Server):
    """A supervisor in a container, reached over the published loopback port.

    It inherits request() and nothing else: there is no in-process supervisor object and no
    host view of /scratch, so every check that needs either is skipped by name.
    """

    container = True

    def __init__(self, base_url, retention_s=None):
        parsed = base_url.split("//", 1)[-1].rstrip("/")
        host, _, port = parsed.partition(":")
        self.host = host or "127.0.0.1"
        self.port = int(port or 80)
        self.supervisor = None
        self.retention_s = retention_s
        self.scratch_root = sup.DEFAULT_SCRATCH_ROOT
        self.host_scratch = None

    def close(self):
        pass


def test_http(server):
    root = server.scratch_root
    try:
        status, _, body = server.request("GET", "/health")
        check("http: GET /health -> 200 ok", status == 200 and body["status"] == "ok",
              f"got {status} {body}")
        check("http: /health shape is exactly status/busy/queued",
              set(body) == {"status", "busy", "queued"}, f"got {sorted(body)}")

        status, _, body = server.request("GET", "/nope")
        check("http: unknown route -> 404 NotFound",
              status == 404 and body["error"]["type"] == "NotFound", f"got {status} {body}")
        check("http: error shape is execution_id + error",
              set(body) == {"execution_id", "error"} and set(body["error"]) == {"type", "message"})

        status, _, body = server.request("POST", "/health", body={})
        check("http: POST /health -> 405", status == 405 and body["error"]["type"] == "MethodNotAllowed")
        status, _, body = server.request("GET", "/execute")
        check("http: GET /execute -> 405", status == 405)
        status, _, body = server.request("PUT", "/execute", body={})
        check("http: PUT /execute -> 405", status == 405, f"got {status}")
        status, _, body = server.request("HEAD", "/health")
        check("http: HEAD /health -> 405 with no body", status == 405 and body is None,
              f"got {status} {body}")

        status, _, body = server.request("POST", "/execute", body=make_body(), ctype="text/plain")
        check("http: wrong content type -> 415",
              status == 415 and body["error"]["type"] == "UnsupportedMediaType", f"got {status}")

        # The media type is parsed, not string-compared: the Transport paragraph writes it
        # with a charset and the client (4h6.47) sends it bare. Both must be accepted, or
        # every request 415s.
        for label, ctype in (
            ("bare", "application/json"),
            ("with charset", "application/json; charset=utf-8"),
            ("odd case and spacing", "Application/JSON ;charset=UTF-8"),
        ):
            status, _, body = server.request("POST", "/execute",
                                             body=make_body(code="pass"), ctype=ctype)
            check(f"http: content type accepted, {label}", status == 200,
                  f"got {status} {body}")

        status, _, body = _raw_oversized(server.host, server.port)
        check("http: body over 1 MiB -> 413 without reading it",
              status == 413 and body["error"]["type"] == "PayloadTooLarge", f"got {status} {body}")

        # a working script: stdout, an artifact, and the per-execution environment
        code = (
            "import os, json\n"
            "print('hello from the child')\n"
            "keys = ['TMPDIR','HOME','MPLCONFIGDIR','XDG_CACHE_HOME','PYTHONPYCACHEPREFIX',"
            "'SANDBOX_ARTIFACTS_DIR','SANDBOX_USER','SANDBOX_SESSION_ID','SANDBOX_EXECUTION_ID']\n"
            "print(json.dumps({k: os.environ.get(k) for k in keys}))\n"
            "open(os.path.join(os.environ['SANDBOX_ARTIFACTS_DIR'], 'out.csv'),'w').write('a,b\\n')\n"
        )
        payload = make_body(code=code)
        status, _, body = server.request("POST", "/execute", body=payload)
        check("http: successful execution -> 200 ok",
              status == 200 and body["status"] == "ok" and body["error"] is None,
              f"got {status} {body}")
        check("http: echoes execution_id", body.get("execution_id") == payload["execution_id"])
        check("http: exit_code 0 and no signal",
              body["exit_code"] == 0 and body["signal"] is None)
        check("http: output carries the child's stdout", "hello from the child" in body["output"])
        check("http: manifest lists the artifact",
              [e["name"] for e in body["artifacts"]] == ["out.csv"], f"got {body['artifacts']}")
        # `output` is whatever the child printed, so it is excluded: the property is that
        # the SUPERVISOR adds no token, path, environment or host name of its own.
        envelope = json.dumps({k: v for k, v in body.items() if k != "output"})
        check("http: response envelope carries no token and no path",
              "token" not in envelope.lower() and root not in envelope, envelope[:200])

        env = json.loads(body["output"].splitlines()[1])
        base = os.path.join(root, payload["execution_id"])
        check("env: every writable path is inside /scratch/<execution-id>",
              all(env[k] and env[k].startswith(base + os.sep) for k in
                  ("TMPDIR", "HOME", "MPLCONFIGDIR", "XDG_CACHE_HOME", "PYTHONPYCACHEPREFIX",
                   "SANDBOX_ARTIFACTS_DIR")), f"got {env}")
        check("env: artifacts dir is <base>/artifacts",
              env["SANDBOX_ARTIFACTS_DIR"] == os.path.join(base, "artifacts"))
        check("env: audit identity comes from the token claims",
              (env["SANDBOX_USER"], env["SANDBOX_SESSION_ID"], env["SANDBOX_EXECUTION_ID"])
              == (payload["user"], payload["session_id"], payload["execution_id"]))

        # a duplicate id is refused rather than reused or wiped
        status, _, dup = server.request("POST", "/execute", body=make_body(
            execution_id=payload["execution_id"]))
        check("http: duplicate execution_id -> 409 DuplicateExecutionId",
              status == 409 and dup["error"]["type"] == "DuplicateExecutionId", f"got {status} {dup}")
        if server.host_scratch is None:
            # There is no artifact-retrieval route in this contract (4h6.52 owns it), so a
            # container's retained artifacts are not observable from outside it at all.
            skip("http: the first execution's artifacts survive the refusal",
                 "container mode: no host view of /scratch and no artifact route")
        else:
            check("http: the first execution's artifacts survive the refusal",
                  os.path.exists(os.path.join(server.host_scratch, payload["execution_id"],
                                              "artifacts", "out.csv")))

        # an uncaught exception
        status, _, body = server.request("POST", "/execute", body=make_body(
            code="def f():\n    raise ValueError('boom')\nf()\n"))
        check("http: raising script -> 200 status error",
              status == 200 and body["status"] == "error", f"got {status} {body}")
        check("http: error.type is the child's exception class",
              body["error"]["type"] == "ValueError", f"got {body['error']}")
        check("http: traceback is returned and names the frame",
              body["error"]["traceback"] and "ValueError: boom" in body["error"]["traceback"])
        check("http: exit_code 1, signal null", body["exit_code"] == 1 and body["signal"] is None)

        # sys.exit(3): no exception, so the reserved NonZeroExit name
        status, _, body = server.request("POST", "/execute", body=make_body(
            code="import sys\nsys.exit(3)\n"))
        check("http: sys.exit(3) -> NonZeroExit with exit_code 3",
              body["status"] == "error" and body["error"]["type"] == "NonZeroExit"
              and body["exit_code"] == 3 and body["error"]["traceback"] is None,
              f"got {body}")

        # a syntax error is reported as an exception, not as a supervisor failure
        status, _, body = server.request("POST", "/execute", body=make_body(code="def (\n"))
        check("http: syntax error -> SyntaxError",
              body["status"] == "error" and body["error"]["type"] == "SyntaxError", f"got {body}")

        # the child cannot see the supervisor's descriptors
        status, _, body = server.request("POST", "/execute", body=make_body(
            code="import os\nprint('FDS', sorted(os.listdir('/proc/self/fd')))\n"))
        if body["status"] == "ok" and "FDS" in body["output"]:
            fds = json.loads(body["output"].split("FDS ", 1)[1].replace("'", '"'))
            check("child: inherits no descriptor beyond 0-3 and its own listdir handle",
                  all(int(f) <= 4 for f in fds), f"got {fds}")
        else:
            check("child: /proc/self/fd readable", True, "skipped: no /proc")

        # The status pipe is fd 3 in a child that is forked and not exec'd, so the script can
        # write to it. The supervisor's own observation must win: exit 0 and no signal is
        # "ok" whatever the record says, or a script can report its own success as a failure.
        forge = (
            "import os\n"
            "os.write(3, b'{\"type\": \"ValueError\", \"message\": \"forged\", "
            "\"traceback\": \"fake\"}')\n"
            "print('the script actually succeeded')\n"
        )
        status, _, body = server.request("POST", "/execute", body=make_body(code=forge))
        check("child: a forged status record cannot flip exit 0 to error",
              body["status"] == "ok" and body["error"] is None and body["exit_code"] == 0,
              f"got {body['status']} {body['error']} exit={body['exit_code']}")
        check("child: no impossible status/exit_code pair reaches the wire",
              not (body["status"] == "error" and body["exit_code"] == 0))

        # A descendant that setsid()s away inherits the write end of the output pipe, so EOF
        # never arrives. The slot must be freed when the CHILD is reaped, not when the pipe
        # closes, or one escaped process blocks the queue for as long as it lives.
        escape = (
            "import os, time\n"
            "if os.fork() == 0:\n"
            "    os.setsid()\n"
            "    time.sleep(20)\n"
            "    os._exit(0)\n"
            "print('direct child done')\n"
        )
        t0 = time.monotonic()
        status, _, body = server.request("POST", "/execute", body=make_body(code=escape))
        elapsed = time.monotonic() - t0
        check("drain: an escaped descendant does not hold the response",
              elapsed < 10.0, f"took {elapsed:.1f}s")
        check("drain: the escaped execution still answers ok",
              status == 200 and body["status"] == "ok", f"got {status} {body.get('status')}")
        check("drain: duration_ms measures the child, not the drain",
              body["duration_ms"] < 3000, f"got {body['duration_ms']}")
        _, _, health = server.request("GET", "/health")
        check("drain: the slot is free once the child is reaped",
              health["busy"] is False and health["queued"] == 0, f"got {health}")

        status, _, body = server.request("GET", "/health")
        check("http: idle again after the executions", body["busy"] is False and body["queued"] == 0)
    finally:
        server.close()


def test_backpressure(server):
    """429 over the wire, with Retry-After: the client's (4h6.47) retry policy reads it."""
    results = []
    threads = []
    try:
        for _ in range(3):
            t = threading.Thread(
                target=lambda: results.append(
                    server.request("POST", "/execute",
                                   body=make_body(code="import time\ntime.sleep(2)\n"))),
                daemon=True)
            t.start()
            threads.append(t)

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _, _, health = server.request("GET", "/health")
            if health["busy"] and health["queued"] == 2:
                break
            time.sleep(0.05)
        check("backpressure: one running and two queued", health["busy"] and health["queued"] == 2,
              f"got {health}")

        status, retry_after, body = server.request("POST", "/execute", body=make_body())
        check("backpressure: a fourth request -> 429 Busy",
              status == 429 and body["error"]["type"] == "Busy", f"got {status} {body}")
        check("backpressure: 429 carries Retry-After on the wire",
              retry_after == str(sup.RETRY_AFTER_S), f"got {retry_after!r}")
    finally:
        for t in threads:
            t.join(30)
        server.close()
    check("backpressure: all three admitted requests answered 200",
          [r[0] for r in results] == [200, 200, 200], f"got {[r[0] for r in results]}")


def _raw_oversized(host, port):
    """Announce a 2 MiB body and send none of it. The cap must fire on the header alone."""
    s = socket.create_connection((host, port), timeout=10)
    s.sendall(
        b"POST /execute HTTP/1.1\r\nHost: localhost\r\n"
        b"Content-Type: application/json\r\nContent-Length: 2097152\r\n\r\n"
    )
    raw = b""
    s.settimeout(10)
    try:
        while b"\r\n\r\n" not in raw:
            block = s.recv(4096)
            if not block:
                break
            raw += block
        while True:
            block = s.recv(4096)
            if not block:
                break
            raw += block
    except socket.timeout:
        pass
    s.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1]) if head else 0
    try:
        parsed = json.loads(body.decode())
    except Exception:
        parsed = None
    return status, None, parsed


# --------------------------------------------------------------------------------------
# 5c. every supervisor-enforced limit, watched firing over the wire (4h6.41/.42/.43/.46)
# --------------------------------------------------------------------------------------


# Writes are PACED rather than blasted, and that is not politeness. /scratch is 512Mi in both
# the pod (emptyDir sizeLimit) and the local container (tmpfs size), the watchdog polls every
# 0.2s, and an unpaced writer reaches ~1 GiB/s — so an unpaced quota test can hit ENOSPC (or,
# in the pod, an eviction) before the poll it is trying to demonstrate ever runs.
_FILL = """
import os, time
target = os.path.join({dir!r}, 'fill.bin')
block = b'\\0' * (4 * 1024 * 1024)
with open(target, 'wb') as fh:
    for _ in range({blocks}):
        fh.write(block)
        fh.flush()
        os.fsync(fh.fileno())
        time.sleep(0.03)
print('WROTE', os.path.getsize(target))
"""


def test_limits(server):
    """The wall clock, both output bounds, RLIMIT_AS, the pid budget and both quotas."""

    # -- 4h6.41 wall clock. Not overridable upward (parse rejects >120); this proves the
    # timer actually fires and that the response is the contract's `timeout`, not `error`.
    t0 = time.monotonic()
    status, _, body = server.request("POST", "/execute", body=make_body(
        code="while True:\n    pass\n", timeout_s=2))
    elapsed = time.monotonic() - t0
    check("wall clock: a non-terminating script is killed",
          status == 200 and body["status"] == "timeout", f"got {status} {body.get('status')}")
    check("wall clock: error.type Timeout, error.limit null",
          body["error"]["type"] == "Timeout" and body["error"]["limit"] is None,
          f"got {body['error']}")
    check("wall clock: killed by a signal, not a clean exit",
          body["signal"] in (9, 15) and body["exit_code"] is None, f"got {body}")
    check("wall clock: fires at the deadline, not at the 120s ceiling",
          2.0 <= elapsed < 12.0, f"took {elapsed:.1f}s")

    # -- 4h6.41 RLIMIT_AS. The clean MemoryError inside the child is the whole point: an OOM
    # kill would be the kernel choosing between the child and the supervisor by RSS heuristic.
    status, _, body = server.request("POST", "/execute", body=make_body(
        code="import resource\n"
             "print('AS', resource.getrlimit(resource.RLIMIT_AS)[0])\n"
             "buf = bytearray(3 * 1024 * 1024 * 1024)\n"))
    check("rlimit: an over-large allocation is a MemoryError, not an OOM kill",
          body["status"] == "error" and body["error"]["type"] == "MemoryError",
          f"got {body['status']} {body.get('error')}")
    check("rlimit: RLIMIT_AS is the pod-sized value, not unlimited",
          f"AS {sup.CHILD_RLIMIT_AS_BYTES}" in body["output"], body["output"][:120])

    # A LIMIT THE CHILD CAN RAISE BACK IS NOT A LIMIT. Setting only the soft limit left the
    # hard limit at RLIM_INFINITY, and raising a soft limit up to the hard limit needs no
    # privilege — measured in the real image, after which a 2900 MiB allocation produced the
    # cgroup OOM kill the limit exists to prevent.
    undo = (
        "import resource\n"
        "inf = resource.RLIM_INFINITY\n"
        "try:\n"
        "    resource.setrlimit(resource.RLIMIT_AS, (inf, inf))\n"
        "    print('UNDO raised')\n"
        "except (ValueError, OSError) as exc:\n"
        "    print('UNDO refused', type(exc).__name__)\n"
        "print('UNDO hard', resource.getrlimit(resource.RLIMIT_AS)[1])\n"
    )
    status, _, body = server.request("POST", "/execute", body=make_body(code=undo))
    check("rlimit: the child CANNOT raise RLIMIT_AS back — the hard limit is lowered too",
          "UNDO refused" in body.get("output", ""), body.get("output", "")[:200])
    check("rlimit: the hard limit is the pod-sized value, not RLIM_INFINITY",
          f"UNDO hard {sup.CHILD_RLIMIT_AS_BYTES}" in body.get("output", ""),
          body.get("output", "")[:200])

    # oom_score_adj: the child is RAISED to +500 so the cgroup OOM killer aims at it. The
    # child can write 0 back (measured) but not below it, so this is "never a better victim
    # than the supervisor", not a durable +500 — see _apply_limits.
    status, _, body = server.request("POST", "/execute", body=make_body(
        code="print('OOM', open('/proc/self/oom_score_adj').read().strip())\n"))
    if body["status"] == "ok" and "OOM" in body["output"]:
        check("oom_score_adj: the child is raised above the supervisor's",
              f"OOM {sup.CHILD_OOM_SCORE_ADJ}" in body["output"], body["output"][:80])
    else:
        skip("oom_score_adj: the child is raised above the supervisor's", "no /proc")

    # -- 4h6.41 pid budget. RLIMIT_NPROC cannot do this job under one shared uid, so the
    # supervisor watches the process group. Every member must die, not just the direct child.
    fork_bomb = (
        "import os, time\n"
        f"for _ in range({sup.PID_BUDGET * 2}):\n"
        "    if os.fork() == 0:\n"
        "        time.sleep(60)\n"
        "        os._exit(0)\n"
        "time.sleep(60)\n"
    )
    # timeout_s is bounded so that a pid budget that does NOT fire fails as a timeout rather
    # than hanging the harness for the full default minute.
    status, _, body = server.request("POST", "/execute",
                                     body=make_body(code=fork_bomb, timeout_s=20))
    check("pid budget: an over-budget process group is killed",
          body["status"] == "limit" and body["error"]["type"] == "PidLimit",
          f"got {body['status']} {body.get('error')}")
    check("pid budget: error.limit names the limit that fired",
          body["error"]["limit"] == "PidLimit", f"got {body['error']}")
    _, _, health = server.request("GET", "/health")
    check("pid budget: the slot is free afterwards",
          health["busy"] is False and health["queued"] == 0, f"got {health}")

    # -- 4h6.42 the 8 MiB pipe cap. A different limit from the 64 KiB return window, and
    # conflating them loses this one: the cap is what stops a printing loop consuming the
    # SUPERVISOR's memory before the wall clock fires.
    status, _, body = server.request("POST", "/execute", body=make_body(
        code="import sys\n"
             "block = 'x' * 65536\n"
             "for _ in range(256):\n"
             "    sys.stdout.write(block)\n"
             "sys.stdout.flush()\n", timeout_s=60))
    check("pipe cap: 16 MiB of output is a limit, not an ok",
          body["status"] == "limit" and body["error"]["type"] == "OutputLimit",
          f"got {body['status']} {body.get('error')}")
    check("pipe cap: output_bytes stops at the cap",
          body["output_bytes"] == sup.PIPE_CAP_BYTES, f"got {body['output_bytes']}")
    check("pipe cap: output_truncated is set", body["output_truncated"] is True)
    check("pipe cap: the child was killed at the cap",
          body["signal"] in (9, 15) or body["exit_code"] not in (0, None),
          f"got exit={body['exit_code']} signal={body['signal']}")

    # -- 4h6.42 the 64 KiB return window: head AND tail, with a visible marker between.
    chatty = (
        "print('HEADMARK')\n"
        "line = 'y' * 1023\n"
        "for _ in range(200):\n"
        "    print(line)\n"
        "print('TAILMARK')\n"
    )
    status, _, body = server.request("POST", "/execute", body=make_body(code=chatty))
    out = body["output"]
    check("return cap: the run itself is ok — 200 KiB is not a limit",
          body["status"] == "ok", f"got {body['status']} {body.get('error')}")
    check("return cap: the head survives", out.startswith("HEADMARK"), out[:40])
    check("return cap: the TAIL survives, which is where a traceback lives",
          out.rstrip().endswith("TAILMARK"), out[-40:])
    check("return cap: the elision is visible and counted",
          "...[" in out and " bytes elided]..." in out,
          out[sup.RETURN_HEAD_BYTES - 20:sup.RETURN_HEAD_BYTES + 60])
    marker_len = len(sup.ELISION_MARKER.format(10 ** 9))
    check("return cap: the payload is 64 KiB plus the marker, not more",
          len(out.encode()) <= sup.RETURN_HEAD_BYTES + sup.RETURN_TAIL_BYTES + marker_len,
          f"got {len(out.encode())}")
    check("return cap: output_truncated is set when the middle is elided",
          body["output_truncated"] is True)
    check("return cap: output_bytes is the true pre-cap total",
          body["output_bytes"] > sup.RETURN_HEAD_BYTES + sup.RETURN_TAIL_BYTES,
          f"got {body['output_bytes']}")

    # The cut is on BYTES, and both ends must land on a character boundary. The leading 'x'
    # makes the head cut fall in the middle of a two-byte sequence rather than between two.
    status, _, body = server.request("POST", "/execute", body=make_body(
        code="print('x' + '\\u00e9' * 200000)\n"))
    out = body["output"]
    check("return cap: the head/tail cut does not split a multi-byte character",
          "�" not in out, repr(out[:60]) + " ... " + repr(out[-60:]))
    check("return cap: it really did elide (so the check above was exercised)",
          body["output_truncated"] is True and "bytes elided" in out)

    # -- 4h6.46 the per-execution artifact quota: a clean error, never a pod eviction.
    blocks = (sup.ARTIFACT_QUOTA_BYTES // (4 * 1024 * 1024)) + 8
    status, _, body = server.request("POST", "/execute", body=make_body(
        code=_FILL.format(dir="__ARTIFACTS__", blocks=blocks).replace(
            "'__ARTIFACTS__'", "os.environ['SANDBOX_ARTIFACTS_DIR']"), timeout_s=60))
    check("artifact quota: over 64Mi in artifacts/ is a limit",
          body["status"] == "limit" and body["error"]["type"] == "ArtifactQuota",
          f"got {body['status']} {body.get('error')}")

    # -- 4h6.46 the per-execution total quota, over ALL of /scratch/<id>, not just artifacts.
    blocks = (sup.EXECUTION_TOTAL_QUOTA_BYTES // (4 * 1024 * 1024)) + 8
    status, _, body = server.request("POST", "/execute", body=make_body(
        code=_FILL.format(dir="__TMP__", blocks=blocks).replace(
            "'__TMP__'", "os.environ['TMPDIR']"), timeout_s=90))
    check("scratch quota: over the per-execution total is a limit",
          body["status"] == "limit" and body["error"]["type"] == "ScratchQuota",
          f"got {body['status']} {body.get('error')}")
    _, _, health = server.request("GET", "/health")
    check("scratch quota: the supervisor is still serving afterwards",
          health["status"] == "ok", f"got {health}")

    # -- ZERO-LENGTH FILES ARE NOT FREE. Charging st_blocks alone said they were: 300,000 empty
    # files charged 8.6 MB against the 192 MiB quota, so nothing fired, while the response
    # reached 19.8 MB and the slot was held 58.7s for a 34.9s child. The entry budget is what
    # makes the byte quota unbypassable and — because the watchdog's scan is bounded by the
    # same number — what keeps the wall clock from being child-controlled.
    t0 = time.monotonic()
    empties = (
        "import os\n"
        "d = os.environ['TMPDIR']\n"
        f"for i in range({sup.EXECUTION_ENTRY_BUDGET + 10000}):\n"
        "    open(os.path.join(d, 'e%06d' % i), 'w').close()\n"
        "import time; time.sleep(60)\n"
    )
    status, _, body = server.request("POST", "/execute",
                                     body=make_body(code=empties, timeout_s=30))
    elapsed = time.monotonic() - t0
    check("entry budget: 30k zero-length files are a limit, not a free pass",
          body["status"] == "limit" and body["error"]["type"] == "ScratchQuota",
          f"got {body['status']} {body.get('error')} in {elapsed:.1f}s")
    check("entry budget: it fires long before the 30s wall clock it used to starve",
          elapsed < 25.0, f"took {elapsed:.1f}s")

    many = (
        "import os\n"
        "d = os.environ['SANDBOX_ARTIFACTS_DIR']\n"
        f"for i in range({sup.ARTIFACT_ENTRY_BUDGET + 500}):\n"
        "    open(os.path.join(d, 'a%06d.txt' % i), 'w').close()\n"
        "import time; time.sleep(60)\n"
    )
    status, _, body = server.request("POST", "/execute",
                                     body=make_body(code=many, timeout_s=30))
    check("entry budget: too many artifacts is an ArtifactQuota limit",
          body["status"] == "limit" and body["error"]["type"] == "ArtifactQuota",
          f"got {body['status']} {body.get('error')}")
    check("entry budget: the manifest never carries more than its cap",
          len(body["artifacts"]) <= sup.ARTIFACT_ENTRY_BUDGET, f"got {len(body['artifacts'])}")

    # -- THE OVERSHOOT IS TRIMMED, NOT RETAINED. An unpaced writer killed at the 64 MiB quota
    # left 93 MiB on disk (46% over, measured); retaining that makes the 256 MiB aggregate
    # ceiling a ceiling over unbounded terms. The trim is visible on the wire: the over-quota
    # file is gone from the manifest and counted in artifacts_omitted.
    #
    # PACED, like every other quota test here, and the pacing is what makes the check a check.
    # Unpaced this raced the watchdog's first scan: the scan is gated to one filesystem walk per
    # WATCHDOG_POLL_S, so the first one lands at t~200ms, and on a fast tmpfs all 192 MiB
    # completed inside that window — the child exited, job.done fired, and the watchdog returned
    # having never scanned, so the execution answered `ok`. REPRODUCED on an unedited tree: one
    # run FAILED, the next passed. A control assertion that flips on filesystem speed is not
    # evidence the control works, and 4h6.49 reads this harness's output. Pacing 12 x 8 MiB at
    # 100ms puts >= 12 scans inside the write, and still exercises the OVER-quota trim path: the
    # kill lands on the first scan past 64 MiB, the file on disk is then over the quota by at
    # least its dirent cost, and one file over the quota is trimmed to nothing.
    burst = (
        "import os, time\n"
        "p = os.path.join(os.environ['SANDBOX_ARTIFACTS_DIR'], 'burst.bin')\n"
        "block = b'\\0' * (8 * 1024 * 1024)\n"
        "with open(p, 'wb') as fh:\n"
        "    for _ in range(12):\n"
        "        fh.write(block)\n"
        "        fh.flush()\n"
        "        os.fsync(fh.fileno())\n"
        "        time.sleep(0.1)\n"
        "print('BURST', os.path.getsize(p))\n"
    )
    status, _, body = server.request("POST", "/execute",
                                     body=make_body(code=burst, timeout_s=60))
    check("retained trim: the burst is an ArtifactQuota limit",
          body["status"] == "limit" and body["error"]["type"] == "ArtifactQuota",
          f"got {body['status']} {body.get('error')}")
    check("retained trim: the over-quota artifact is NOT retained",
          [e["name"] for e in body["artifacts"]] == [], f"got {body['artifacts']}")
    check("retained trim: the deleted entry is reported in artifacts_omitted",
          body["artifacts_omitted"] >= 1, f"got {body['artifacts_omitted']}")
    # -- error.type is untrusted input like message and traceback, and was the one field
    # getting neither a cap nor a validator (the same defect 4h6.47 fixed on the client side,
    # where _redact reached `message` and not `error_type`).
    forge = (
        "import json, os\n"
        "os.write(3, json.dumps({'type': %r, 'message': 'm', 'traceback': None}).encode())\n"
        "os._exit(1)\n"
    )
    status, _, body = server.request("POST", "/execute",
                                     body=make_body(code=forge % ("A" * 60000)))
    check("error.type: a 60,000-character type never reaches the response",
          body["error"]["type"] == "NonZeroExit", f"got {body['error']['type'][:60]!r}")
    status, _, body = server.request("POST", "/execute",
                                     body=make_body(code=forge % "Timeout"))
    check("error.type: a child cannot forge a supervisor-reserved name",
          body["status"] == "error" and body["error"]["type"] == "NonZeroExit",
          f"got {body['status']} {body['error']}")
    status, _, body = server.request("POST", "/execute", body=make_body(
        code="raise ValueError('genuine')\n"))
    check("error.type: a genuine exception class name still passes through",
          body["error"]["type"] == "ValueError", f"got {body['error']}")


def test_tokens(server):
    """4h6.43: the read-once token file, seen from inside the child that has to use it."""
    probe = (
        "import json, os\n"
        "path = os.environ.get('SANDBOX_TOKEN_FILE')\n"
        "st = os.stat(path)\n"
        "data = json.load(open(path))\n"
        "print('TOKENS ' + json.dumps({\n"
        "  'path': path, 'mode': oct(st.st_mode & 0o777),\n"
        "  'keys': sorted(data), 'uid': st.st_uid,\n"
        "  'in_env': any(v in os.environ.values() for v in data.values()),\n"
        "  'under_base': path.startswith(os.path.dirname(os.environ['SANDBOX_ARTIFACTS_DIR'])),\n"
        "}))\n"
    )
    payload = make_body(code=probe)
    status, _, body = server.request("POST", "/execute", body=payload)
    if body.get("status") != "ok" or "TOKENS " not in body.get("output", ""):
        check("tokens: the child can read its token file", False,
              f"got {body.get('status')} {body.get('error')}")
        return
    got = json.loads(body["output"].split("TOKENS ", 1)[1].splitlines()[0])
    check("tokens: both audiences are delivered",
          got["keys"] == sorted(sup.TOKEN_AUDIENCES), f"got {got['keys']}")
    check("tokens: the file is mode 0600", got["mode"] == "0o600", got["mode"])
    check("tokens: it is NOT chowned away from the shared uid (no CAP_CHOWN here)",
          got["uid"] == os.getuid() if not server.container else got["uid"] == 65532,
          f"got {got['uid']}")
    check("tokens: the file lives inside the per-execution directory", got["under_base"] is True)
    check("tokens: no token is in the child's environment — only the path is",
          got["in_env"] is False)
    check("tokens: the response envelope carries no token",
          all(t not in json.dumps(body) for t in payload["tokens"].values()))
    if server.host_scratch is None:
        skip("tokens: the file is gone once the child is reaped",
             "container mode: no host view of /scratch")
    else:
        check("tokens: the file is gone once the child is reaped",
              not os.path.exists(os.path.join(server.host_scratch,
                                              payload["execution_id"], "tokens.json")))


def test_retained_ceiling(server):
    """4h6.46: the aggregate retained ceiling evicts oldest-first.

    Observable on the wire without any host view: an evicted execution's id stops being a
    `409 DuplicateExecutionId` and becomes usable again, because its directory is gone.
    """
    per = 60 * 1024 * 1024
    blocks = per // (4 * 1024 * 1024)
    count = (sup.RETAINED_ARTIFACTS_CEILING_BYTES // per) + 2
    ids = []
    for _ in range(count):
        payload = make_body(code=_FILL.format(dir="__ARTIFACTS__", blocks=blocks).replace(
            "'__ARTIFACTS__'", "os.environ['SANDBOX_ARTIFACTS_DIR']"), timeout_s=90)
        status, _, body = server.request("POST", "/execute", body=payload)
        if body.get("status") != "ok":
            check("retained ceiling: each filler execution completes", False,
                  f"got {body.get('status')} {body.get('error')}")
            return
        ids.append(payload["execution_id"])
    check("retained ceiling: each filler execution completes", True)

    status, _, body = server.request("POST", "/execute",
                                     body=make_body(execution_id=ids[0], code="pass"))
    check("retained ceiling: the OLDEST execution was evicted, so its id is free again",
          status == 200, f"got {status} {body.get('error')}")
    status, _, body = server.request("POST", "/execute",
                                     body=make_body(execution_id=ids[-1], code="pass"))
    check("retained ceiling: the NEWEST execution is still retained, so its id is not",
          status == 409 and body["error"]["type"] == "DuplicateExecutionId",
          f"got {status} {body.get('error')}")


def test_retention_expiry(server):
    """4h6.46: artifacts are deleted at the retention deadline whether or not read."""
    if not server.retention_s:
        skip("retention: artifacts are deleted at the deadline",
             "needs a supervisor started with a shortened SANDBOX_RETENTION_S "
             "(--retention-s N in container mode)")
        return
    payload = make_body(code=(
        "import os\n"
        "open(os.path.join(os.environ['SANDBOX_ARTIFACTS_DIR'], 'a.csv'), 'w').write('x')\n"))
    status, _, body = server.request("POST", "/execute", body=payload)
    check("retention: the execution completed with an artifact",
          body["status"] == "ok" and [e["name"] for e in body["artifacts"]] == ["a.csv"],
          f"got {body.get('status')} {body.get('artifacts')}")
    status, _, dup = server.request("POST", "/execute",
                                    body=make_body(execution_id=payload["execution_id"]))
    check("retention: inside the window the id is still taken", status == 409, f"got {status}")

    time.sleep(server.retention_s + sup.REAPER_POLL_S + 2
               if server.container else server.retention_s + 1)
    if not server.container:
        server.supervisor.reap_expired()
    if server.host_scratch is not None:
        check("retention: the artifacts directory is gone after the deadline",
              not os.path.exists(os.path.join(server.host_scratch, payload["execution_id"])))
    else:
        skip("retention: the artifacts directory is gone after the deadline",
             "container mode: no host view of /scratch")
    status, _, body = server.request("POST", "/execute",
                                     body=make_body(execution_id=payload["execution_id"]))
    check("retention: after the deadline the id is reusable", status == 200,
          f"got {status} {body.get('error')}")


# --------------------------------------------------------------------------------------
# 5d. unit-level properties of the capping and accounting helpers (in-process only)
# --------------------------------------------------------------------------------------


def test_cap_units(tmp):
    head, tail = sup.RETURN_HEAD_BYTES, sup.RETURN_TAIL_BYTES

    text, truncated = sup._cap_output(b"short")
    check("cap: output under the window is returned whole and untruncated",
          text == "short" and truncated is False)

    exact = b"a" * (head + tail)
    text, truncated = sup._cap_output(exact)
    check("cap: exactly 64 KiB is not truncated",
          text == exact.decode() and truncated is False, f"{len(text)} {truncated}")

    raw = b"H" * head + b"M" * 1000 + b"T" * tail
    text, truncated = sup._cap_output(raw)
    check("cap: one byte over elides the middle and says so",
          truncated is True and text.startswith("H") and text.endswith("T")
          and sup.ELISION_MARKER.format(1000) in text, text[head - 5:head + 40])
    check("cap: the elided count is bytes dropped, not bytes kept",
          "[1000 bytes elided]" in text)

    # A multi-byte character straddling each cut. The single leading 'x' is what puts the
    # boundary mid-sequence instead of neatly between two two-byte characters.
    raw = ("x" + "é" * (head + tail)).encode("utf-8")
    text, truncated = sup._cap_output(raw)
    check("cap: neither cut bisects a multi-byte character (no U+FFFD introduced)",
          truncated is True and "�" not in text, repr(text[:40]))
    kept_head, _, rest = text.partition("\n...[")
    elided = int(rest.split(" bytes elided]", 1)[0])
    kept_tail = rest.split("]...\n", 1)[1]
    check("cap: head + tail + elided accounts for every byte — trimming is never silent",
          len(kept_head.encode()) + len(kept_tail.encode()) + elided == len(raw),
          f"{len(kept_head.encode())} + {len(kept_tail.encode())} + {elided} vs {len(raw)}")

    check("cap: a lone continuation byte is not treated as a lead",
          sup._utf8_tail(b"\x80\x80abc") == b"abc")
    check("cap: an already-complete head is left alone",
          sup._utf8_head("abé".encode()) == "abé".encode())

    # 4h6.46 accounting is on BLOCKS, so a sparse file cannot be used to fake a quota breach.
    d = os.path.join(tmp, "sparse")
    os.makedirs(d)
    with open(os.path.join(d, "sparse.bin"), "wb") as fh:
        fh.seek(256 * 1024 * 1024)
        fh.write(b"x")
    check("quota accounting: a 256 MiB sparse file charges its blocks, not its size",
          sup._dir_bytes(d) < 8 * 1024 * 1024, f"got {sup._dir_bytes(d)}")
    with open(os.path.join(d, "real.bin"), "wb") as fh:
        fh.write(b"\0" * (4 * 1024 * 1024))
    check("quota accounting: a real 4 MiB file is charged",
          sup._dir_bytes(d) >= 4 * 1024 * 1024, f"got {sup._dir_bytes(d)}")

    # An empty file charges its dirent floor. Without this the 192 MiB quota was reachable
    # only with 393,216 files' worth of real blocks and never with 300,000 empty ones.
    e = os.path.join(tmp, "empties")
    os.makedirs(e)
    for i in range(2000):
        open(os.path.join(e, "e%04d" % i), "w").close()
    check("quota accounting: zero-length files are charged a per-entry floor",
          sup._dir_bytes(e) >= 2000 * sup.DIRENT_COST_BYTES, f"got {sup._dir_bytes(e)}")

    # THE SCAN IS BOUNDED BY THE BUDGET, not by how many files the child made. This is what
    # keeps the watchdog's tick — and therefore the wall clock — off the child's control path.
    t0 = time.monotonic()
    cost, entries, _ = sup._dir_usage(e, 100)
    bounded = time.monotonic() - t0
    check("quota accounting: the scan stops at the entry limit instead of walking the tree",
          entries <= 101, f"visited {entries}")
    t0 = time.monotonic()
    _, full_entries, _ = sup._dir_usage(e, 10 ** 9)
    full = time.monotonic() - t0
    check("quota accounting: the unbounded walk really is the more expensive one",
          full_entries == 2000 and bounded <= full + 0.01,
          f"{entries} in {bounded:.4f}s vs {full_entries} in {full:.4f}s")

    # One pass, two answers: artifacts/ lives under base and used to be walked twice per tick.
    base = os.path.join(tmp, "usage-base")
    art = os.path.join(base, "artifacts")
    os.makedirs(art)
    with open(os.path.join(art, "a.bin"), "wb") as fh:
        fh.write(b"\0" * (1024 * 1024))
    with open(os.path.join(base, "t.bin"), "wb") as fh:
        fh.write(b"\0" * (2 * 1024 * 1024))
    cost, entries, (sub_cost, sub_entries) = sup._dir_usage(base, 1000, sub=art)
    check("quota accounting: one pass measures the subtree and the whole tree together",
          sub_cost >= 1024 * 1024 and cost >= 3 * 1024 * 1024 and cost > sub_cost
          and sub_entries == 1 and entries == 3,
          f"cost={cost} entries={entries} sub={sub_cost}/{sub_entries}")

    members = sup._group_members(os.getpgid(0))
    check("pid accounting: the harness's own process group contains the harness",
          members is not None and os.getpid() in members, f"got {members}")
    check("pid accounting: an unused process group is empty, not None",
          sup._group_members(0x7FFFFFF0) == [])


# --------------------------------------------------------------------------------------
# 5e. the hardening properties that have no wire shape: the budget arithmetic, the kill
# path's two races and the two response bounds. Each of these was a real defect; each check
# below fails against the code as it was.
# --------------------------------------------------------------------------------------


def test_hardening_units(tmp):
    # -- the /scratch budget has to CLOSE, and it is stated in exactly one place now.
    check("budget: retained + one live execution fits under the aggregate ceiling",
          sup.RETAINED_ARTIFACTS_CEILING_BYTES + sup.EXECUTION_TOTAL_QUOTA_BYTES
          <= sup.SCRATCH_AGGREGATE_CEILING_BYTES,
          f"{sup.RETAINED_ARTIFACTS_CEILING_BYTES} + {sup.EXECUTION_TOTAL_QUOTA_BYTES} "
          f"vs {sup.SCRATCH_AGGREGATE_CEILING_BYTES}")
    check("budget: the aggregate ceiling leaves the supervisor's reserve under the sizeLimit",
          sup.SCRATCH_AGGREGATE_CEILING_BYTES + sup.SCRATCH_SUPERVISOR_RESERVE_BYTES
          == sup.SCRATCH_SIZE_LIMIT_BYTES)
    check("budget: a single trimmed execution can never breach the retained ceiling alone",
          sup.ARTIFACT_QUOTA_BYTES < sup.RETAINED_ARTIFACTS_CEILING_BYTES)

    # -- the trim itself, newest-first, against both budgets.
    art = os.path.join(tmp, "trim", "artifacts")
    os.makedirs(art)
    for i, size in enumerate((40, 40, 40)):
        p = os.path.join(art, "f%d.bin" % i)
        with open(p, "wb") as fh:
            fh.write(b"\0" * (size * 1024 * 1024))
        os.utime(p, (1000 + i, 1000 + i))
    deleted, total = sup._trim_artifacts(art)
    check("trim: an over-quota artifacts/ is brought back under the quota",
          total <= sup.ARTIFACT_QUOTA_BYTES and deleted >= 1,
          f"deleted {deleted}, {total} bytes left")
    check("trim: the NEWEST entries go first — the deliberate early ones survive",
          os.path.exists(os.path.join(art, "f0.bin"))
          and not os.path.exists(os.path.join(art, "f2.bin")),
          f"left {sorted(os.listdir(art))}")

    art2 = os.path.join(tmp, "trim-entries", "artifacts")
    os.makedirs(art2)
    for i in range(sup.ARTIFACT_ENTRY_BUDGET + 50):
        open(os.path.join(art2, "e%05d" % i), "w").close()
    deleted, _ = sup._trim_artifacts(art2)
    check("trim: the entry budget is trimmed to as well as the byte quota",
          len(os.listdir(art2)) <= sup.ARTIFACT_ENTRY_BUDGET and deleted >= 50,
          f"{len(os.listdir(art2))} left, deleted {deleted}")

    # -- THE TRIM MUST NOT BE BOUNDED BY THE BUDGET IT RESTORES. Its enumeration used to stop at
    # EXECUTION_ENTRY_BUDGET, so it sorted a truncated sample and derived both the surviving
    # count and the returned size from it: MEASURED on 25,000 zero-length files, 6,024 entries
    # survived a 1,024 budget and it reported 0.5 MiB where _dir_usage measured 2.9 MiB. That
    # returned number is what _retain caches, so _retained_total() undercounted by the same
    # factor and both the watchdog's aggregate check and the ceiling eviction ran on fiction.
    # A directory this size is reachable inside KILL_GRACE_S alone at measured creation rates.
    art3 = os.path.join(tmp, "trim-oversized", "artifacts")
    os.makedirs(art3)
    for i in range(sup.TRIM_SCAN_CHUNK + 5000):
        open(os.path.join(art3, "o%06d" % i), "w").close()
    deleted, total = sup._trim_artifacts(art3)
    left = len(os.listdir(art3))
    real, real_entries, _ = sup._dir_usage(art3, sup.TRIM_ENTRY_CEILING)
    check("trim: a tree bigger than one scan pass is still trimmed to the entry budget",
          left <= sup.ARTIFACT_ENTRY_BUDGET, f"{left} entries left after deleting {deleted}")
    check("trim: the size it reports is the size really there, not a sampled fraction",
          total == real and real_entries == left,
          f"reported {total} for {left} entries, measured {real} for {real_entries}")

    # -- the manifest's own caps. 300,000 files produced a 19.8 MB response body.
    many = os.path.join(tmp, "manifest-many")
    os.makedirs(many)
    for i in range(sup.ARTIFACT_ENTRY_BUDGET + 200):
        open(os.path.join(many, "m%05d.txt" % i), "w").close()
    entries, omitted = sup.build_manifest(many)
    check("manifest: the entry count is capped",
          len(entries) == sup.ARTIFACT_ENTRY_BUDGET, f"got {len(entries)}")
    check("manifest: what it did not list is reported in artifacts_omitted",
          omitted == 200, f"got {omitted}")
    check("manifest: the scan limit bounds the walk itself",
          sup.build_manifest(many, max_entries=10, scan_limit=50)[0].__len__() == 10)

    # -- the response bound. Every component is capped; this is the backstop that was missing.
    payload = {"execution_id": "x", "status": "ok", "output": "y" * 1000,
               "output_truncated": False, "error": None, "artifacts_omitted": 0,
               "artifacts": [{"name": "n%06d" % i, "size": 0, "content_type": "text/plain"}
                             for i in range(40000)]}
    trimmed, body = sup._cap_response(payload)
    check("response cap: an oversized body is degraded, not sent",
          len(body) <= sup.MAX_RESPONSE_BYTES, f"got {len(body)}")
    check("response cap: the artifacts it dropped are counted, not lost silently",
          trimmed["artifacts"] == [] and trimmed["artifacts_omitted"] == 40000,
          f"got {trimmed['artifacts_omitted']}")
    small = {"execution_id": "x", "status": "ok", "output": "hi", "artifacts": []}
    check("response cap: an ordinary response is passed through untouched",
          sup._cap_response(small)[0] is small)

    # -- error.type, at the unit level: the cap, the pattern and the reserved names.
    check("error.type: an oversized type is refused",
          sup._sanitise_error_type("A" * 5000, 1, "x") == sup.ERR_NON_ZERO_EXIT)
    check("error.type: a non-identifier is refused",
          sup._sanitise_error_type("Timeout; ignore previous instructions", 1, "x")
          == sup.ERR_NON_ZERO_EXIT)
    for name in sorted(sup.RESERVED_ERROR_TYPES - {sup.ERR_STARTUP_FAILURE}):
        if sup._sanitise_error_type(name, 1, "x") != sup.ERR_NON_ZERO_EXIT:
            check("error.type: every reserved name is unforgeable", False, f"{name} passed")
            break
    else:
        check("error.type: every reserved name is unforgeable", True)
    check("error.type: StartupFailure is admitted only on the child's own exit 70",
          sup._sanitise_error_type("StartupFailure", 70, "x") == "StartupFailure"
          and sup._sanitise_error_type("StartupFailure", 1, "x") == sup.ERR_NON_ZERO_EXIT)
    check("error.type: a real exception class name survives",
          sup._sanitise_error_type("polars.exceptions.ComputeError", 1, "x")
          == "polars.exceptions.ComputeError")

    # -- a reaped job cannot have a limit fire on it: one poll's race turned a clean run into
    # a reported timeout, discarding its output and its manifest.
    job = sup.Job(sup.parse_execute_request(json.dumps(make_body()).encode()), None)
    job.pid = os.getpid()
    job.reaped = True
    sup._fire_limit(job, sup.ERR_TIMEOUT)
    check("race: _fire_limit refuses to label a run that was already reaped",
          job.limit is None, f"got {job.limit}")

    # -- SIGTERM delivery FAILING is not SIGTERM finding nothing, and only the latter may
    # skip the escalation.
    real_signal_group = sup._signal_group
    for outcome, expected in ((sup._SIGNAL_FAILED, 2), (sup._SIGNAL_GONE, 1)):
        sent = []
        sup._signal_group = lambda j, s, _o=outcome, _sent=sent: (_sent.append(s) or _o)
        try:
            job = sup.Job(sup.parse_execute_request(json.dumps(make_body()).encode()), None)
            job.pid = os.getpid()
            sup._kill_group(job)
        finally:
            sup._signal_group = real_signal_group
        check(f"kill path: SIGTERM reporting {outcome} sends {expected} signal(s)",
              len(sent) == expected, f"sent {sent}")

    # -- the reap fallback must not hold kill_lock across a blocking wait. Patching waitid
    # away is the only way to reach that branch, and before the fix it deadlocked every kill
    # path: _signal_group below never returns.
    real_waitid = sup.os.waitid
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child
        os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(10)
        os._exit(0)
    job = sup.Job(sup.parse_execute_request(json.dumps(make_body()).encode()), None)
    job.pid = pid
    box = {}

    def _boom(*_a, **_k):
        raise OSError("waitid unavailable")

    try:
        sup.os.waitid = _boom
        reaper = threading.Thread(target=lambda: box.update(st=sup._reap(job)), daemon=True)
        reaper.start()
        time.sleep(0.3)
        signaller = threading.Thread(
            target=lambda: box.update(sig=sup._signal_group(job, signal.SIGTERM)), daemon=True)
        signaller.start()
        signaller.join(3.0)
        check("reap fallback: a signal can still be delivered while the fallback waits",
              not signaller.is_alive() and box.get("sig") == sup._SIGNAL_DELIVERED,
              f"alive={signaller.is_alive()} got {box.get('sig')}")
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        reaper.join(5.0)
        check("reap fallback: it still reaps the child",
              not reaper.is_alive() and "st" in box, f"box={box}")
    finally:
        sup.os.waitid = real_waitid

    # -- the ceiling has to be able to evict the only thing it holds.
    sv = sup.Supervisor(tmp)
    with sv._lock:
        sv._retention["only-one"] = [time.monotonic() + 900,
                                     sup.RETAINED_ARTIFACTS_CEILING_BYTES * 2]
    sv._retained_ids.add("only-one")
    evicted = sv._enforce_retained_ceiling()
    check("ceiling: a single over-ceiling execution is evicted, not left sitting above it",
          evicted == ["only-one"] and not sv._retention, f"got {evicted} {list(sv._retention)}")

    # -- a directory that was created must be registered for reaping whether or not the
    # execution reached _retain: _retained_ids alone made the id answer 409 with nothing
    # counting its bytes and nothing but the mtime sweep deleting it.
    root = os.path.join(tmp, "release-root")
    os.makedirs(root)
    sv = sup.Supervisor(root)
    job = sup.Job(sup.parse_execute_request(json.dumps(make_body()).encode()), None)
    job.dirs = sup.ExecutionDirs(root, job.req.execution_id)
    job.dirs.create()
    sv._release(job, retain=True)
    check("release: a created directory is registered for reaping even without _retain",
          job.req.execution_id in sv._retention, f"got {list(sv._retention)}")


# --------------------------------------------------------------------------------------
# 5b. container-only: properties of the IMAGE, which the in-process mode has no way to test
# --------------------------------------------------------------------------------------


PROBE = r"""
import json, os, sys
out = {"uid": os.getuid(), "gid": os.getgid(), "cwd": os.getcwd(),
       "env": {k: os.environ.get(k) for k in
               ("GENETICS_PREWARM", "GENETICS_MPLCACHE", "GENETICS_SCHEMA_DIR",
                "GENETICS_STUBS_DIR", "SANDBOX_SCRATCH_ROOT", "GENETICS_API_URL",
                "BIGQUERY_API_URL", "INTERNAL_API_SECRET", "SANDBOX_TOKEN_SIGNING_KEY",
                "TMPDIR")}}
def writable(path):
    try:
        with open(path, "w") as fh:
            fh.write("x")
        os.unlink(path)
        return None
    except OSError as exc:
        return exc.errno
out["write_rootfs"] = writable("/genetics/probe")
out["write_tmp"] = writable("/tmp/probe")
out["write_tmpdir"] = writable(os.path.join(os.environ["TMPDIR"], "probe"))
for mod in ("pip", "setuptools", "google.auth", "genetics_mcp_server.sdk", "matplotlib"):
    try:
        __import__(mod)
        out["import_" + mod.replace(".", "_")] = True
    except Exception:
        out["import_" + mod.replace(".", "_")] = False
out["sdk_pkg_dirs"] = sorted(os.listdir(os.path.dirname(os.path.dirname(
    sys.modules["genetics_mcp_server.sdk"].__file__)))) if "genetics_mcp_server.sdk" in sys.modules else []
print("PROBE " + json.dumps(out))
"""


def test_container(server):
    """Everything here needs the image: the read-only rootfs, the pruned venv, the baked
    font cache and the absence of credentials are all shipped properties."""
    status, _, body = server.request("POST", "/execute", body=make_body(code=PROBE))
    if status != 200 or body.get("status") != "ok":
        check("container: probe script ran", False, f"got {status} {body}")
        return
    probe = json.loads(body["output"].split("PROBE ", 1)[1].splitlines()[0])
    env = probe["env"]

    check("container: child runs as uid 65532, the SUPERVISOR's uid",
          probe["uid"] == 65532 and probe["gid"] == 65532, f"got {probe['uid']}:{probe['gid']}")
    # SANDBOX_CHILD_UID=65533 is advertised by the image and names a uid nothing can switch
    # to: option (a) needs CAP_SETUID and drop-ALL leaves none (section 2, "The uid choice").
    check("container: the child did NOT become the advertised 65533", probe["uid"] != 65533)

    check("container: root filesystem is read-only", probe["write_rootfs"] == 30,
          f"errno {probe['write_rootfs']}")
    check("container: there is no writable /tmp", probe["write_tmp"] is not None,
          f"errno {probe['write_tmp']}")
    check("container: TMPDIR is writable and under /scratch",
          probe["write_tmpdir"] is None and env["TMPDIR"].startswith("/scratch/"),
          f"{env['TMPDIR']} errno {probe['write_tmpdir']}")
    check("container: cwd is the per-execution tmp, not artifacts/",
          probe["cwd"].startswith("/scratch/") and probe["cwd"].endswith("/tmp"), probe["cwd"])

    # prune_venv.py deletes pip and everything outside the SDK's import closure. Asserted at
    # build time by build-checks.py; asserted here from INSIDE a real execution, which is the
    # position an attacker actually occupies.
    check("container: no package manager (pip)", probe["import_pip"] is False)
    check("container: no setuptools", probe["import_setuptools"] is False)
    check("container: no google-auth (section 3(c))", probe["import_google_auth"] is False)
    check("container: the genetics SDK imports", probe["import_genetics_mcp_server_sdk"] is True)
    check("container: matplotlib imports under the read-only rootfs",
          probe["import_matplotlib"] is True)

    check("container: no credential is in the child's environment",
          env["INTERNAL_API_SECRET"] is None and env["SANDBOX_TOKEN_SIGNING_KEY"] is None,
          f"got {env}")
    check("container: the image's env markers are set, so degradations are off",
          env["GENETICS_PREWARM"] and env["GENETICS_MPLCACHE"], f"got {env}")
    check("container: SANDBOX_SCRATCH_ROOT is unset, so /scratch is the real /scratch",
          env["SANDBOX_SCRATCH_ROOT"] is None, f"got {env['SANDBOX_SCRATCH_ROOT']!r}")
    check("container: the two API endpoints are configured and are not cluster FQDNs",
          env["GENETICS_API_URL"] and env["BIGQUERY_API_URL"]
          and "svc.cluster.local" not in (env["GENETICS_API_URL"] + env["BIGQUERY_API_URL"]),
          f"got {env['GENETICS_API_URL']} {env['BIGQUERY_API_URL']}")

    # The font cache is baked at build time and copied into the per-execution MPLCONFIGDIR
    # because the rootfs is read-only. On matplotlib 3.10 an unwritable MPLCONFIGDIR raises
    # rather than falling back, so this is the one path where "it imports" is not enough:
    # the plot has to be produced and land in the manifest.
    plot = (
        "import os, matplotlib\n"
        "matplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1, 2, 3], [3, 1, 2])\n"
        "plt.savefig(os.path.join(os.environ['SANDBOX_ARTIFACTS_DIR'], 'plot.png'))\n"
        "print('MPLCONFIGDIR', os.environ['MPLCONFIGDIR'])\n"
    )
    status, _, body = server.request("POST", "/execute", body=make_body(code=plot))
    check("container: a plot is written under the read-only rootfs",
          status == 200 and body["status"] == "ok", f"got {status} {body.get('error')}")
    check("container: the plot reaches the artifact manifest",
          [e["name"] for e in body.get("artifacts", [])] == ["plot.png"],
          f"got {body.get('artifacts')}")
    check("container: the plot's size is non-zero",
          body.get("artifacts") and body["artifacts"][0]["size"] > 0,
          f"got {body.get('artifacts')}")


# --------------------------------------------------------------------------------------
# 6. the startup wipe
# --------------------------------------------------------------------------------------


def test_startup_wipe(tmp):
    root = os.path.join(tmp, "wipe-root")
    os.makedirs(os.path.join(root, "11111111-1111-4111-8111-111111111111", "artifacts"))
    with open(os.path.join(root, "11111111-1111-4111-8111-111111111111", "artifacts", "x"), "w") as fh:
        fh.write("secret")
    os.makedirs(os.path.join(root, sup.SUPERVISOR_DIR_NAME))
    with open(os.path.join(root, "stray-file"), "w") as fh:
        fh.write("x")

    removed = sup.wipe_unrecognised_scratch(root)
    check("wipe: removes an orphaned execution directory",
          "11111111-1111-4111-8111-111111111111" in removed)
    check("wipe: removes stray files too", "stray-file" in removed)
    check("wipe: keeps the supervisor's own directory",
          os.path.isdir(os.path.join(root, sup.SUPERVISOR_DIR_NAME)))
    check("wipe: nothing readable is left behind", os.listdir(root) == [sup.SUPERVISOR_DIR_NAME])


# --------------------------------------------------------------------------------------


def run_in_process():
    tmp = tempfile.mkdtemp(prefix="supervisor-test-")
    try:
        print("startup assertions")
        test_nsswitch(tmp)
        print("request parsing and token consistency")
        test_parsing()
        print("queue")
        test_queue(tmp)
        test_peer_gone()
        print("artifact manifest")
        test_manifest(tmp)
        print("startup wipe")
        test_startup_wipe(tmp)
        print("end to end over HTTP")
        root = os.path.join(tmp, "scratch")
        os.makedirs(root)
        test_http(Server(root))
        print("backpressure over HTTP")
        root = os.path.join(tmp, "backpressure")
        os.makedirs(root)
        test_backpressure(Server(root))
        print("capping and accounting units")
        test_cap_units(tmp)
        print("hardening units: budget, kill-path races, response bounds")
        test_hardening_units(tmp)
        print("supervisor limits, over HTTP")
        root = os.path.join(tmp, "limits")
        os.makedirs(root)
        server = Server(root)
        try:
            test_limits(server)
            test_tokens(server)
            test_retained_ceiling(server)
        finally:
            server.close()
        print("artifact retention")
        root = os.path.join(tmp, "retention")
        os.makedirs(root)
        server = Server(root, retention_s=2)
        try:
            test_retention_expiry(server)
        finally:
            server.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_container(base_url, retention_s=None):
    """The same wire checks, against the image. Nothing here imports the container's
    supervisor: it is reached only through the two routes the contract defines, which is
    exactly what chat-backend's client can do (4h6.47)."""
    server = RemoteServer(base_url, retention_s=retention_s)
    try:
        status, _, body = server.request("GET", "/health")
    except OSError as exc:
        print(f"HARNESS: cannot reach {base_url}: {exc}", file=sys.stderr)
        print("         Start one with scripts/run-sandbox-local.sh", file=sys.stderr)
        raise SystemExit(2)
    if status != 200 or (body or {}).get("status") != "ok":
        print(f"HARNESS: {base_url}/health is not ok yet: {status} {body}", file=sys.stderr)
        raise SystemExit(2)
    if body["busy"] or body["queued"]:
        # Concurrency is 1 cluster-wide, so a second driver would take 429s that the
        # backpressure test attributes to its own three requests.
        print(f"HARNESS: {base_url} is already executing something: {body}", file=sys.stderr)
        raise SystemExit(2)
    print(f"end to end over HTTP against the container at {base_url}")
    test_http(server)
    print("backpressure over HTTP against the container")
    test_backpressure(server)
    print("image properties, from inside a real execution")
    test_container(server)
    print("supervisor limits, against the container")
    test_limits(server)
    test_tokens(server)
    print("artifact retention, against the container")
    test_retention_expiry(server)
    # LAST, deliberately: it leaves the retained set near its ceiling, and /scratch is 512Mi.
    print("retained-artifact ceiling, against the container")
    test_retained_ceiling(server)

    # Whole groups, not individual skips: there is no route on the wire that reaches the
    # supervisor's own objects, so these never execute here. Naming them is the difference
    # between a subset run and a subset run that looks complete.
    NOT_RUN.extend([
        "startup assertions (test_nsswitch) — reads the container's /etc/nsswitch.conf",
        "request parsing and token consistency (test_parsing) — calls the parser directly",
        "queue (test_queue, test_peer_gone) — inspects the supervisor's queue objects",
        "artifact manifest (test_manifest) — needs the harness's own view of /scratch",
        "startup wipe (test_startup_wipe) — calls wipe_unrecognised_scratch() directly",
        "capping and accounting units (test_cap_units) — calls _cap_output/_dir_usage directly",
        "hardening units (test_hardening_units) — calls _trim_artifacts/_cap_response/_reap directly",
    ])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: test-supervisor.py [--container URL [--retention-s N]]"
    base_url = None
    retention_s = None
    if argv and argv[0] == "--container":
        if len(argv) not in (2, 4) or (len(argv) == 4 and argv[2] != "--retention-s"):
            print(usage, file=sys.stderr)
            return 2
        base_url = argv[1]
        if len(argv) == 4:
            # Asserted by the caller, never discovered: the wire contract exposes no retention
            # field and inventing one to make a test easier would be inventing a contract.
            try:
                retention_s = int(argv[3])
            except ValueError:
                print(usage, file=sys.stderr)
                return 2
    elif argv:
        print(usage, file=sys.stderr)
        return 2

    if base_url:
        run_container(base_url, retention_s=retention_s)
    else:
        run_in_process()

    print()
    if SKIPPED:
        print(f"{len(SKIPPED)} skipped:")
        for line in SKIPPED:
            print(f"  - {line}")
    if NOT_RUN:
        print(f"{len(NOT_RUN)} check groups NOT RUN in this mode (not skips — never invoked):")
        for line in NOT_RUN:
            print(f"  - {line}")
    if FAILURES:
        print(f"FAILED {len(FAILURES)}/{CHECKS} checks:")
        for line in FAILURES:
            print(f"  - {line}")
        return 1
    print(f"OK: {CHECKS} checks passed"
          + (f", {len(SKIPPED)} skipped" if SKIPPED else ""))
    if NOT_RUN:
        print(f"     PARTIAL COVERAGE: {CHECKS} is the container-mode total, not a fraction of"
              " the in-process run.")
        print(f"     {len(NOT_RUN)} groups above never executed. Run scripts/test-supervisor.py"
              " with no arguments for those.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
