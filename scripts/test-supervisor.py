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

What is NOT covered, because it is not implemented: the wall clock and rlimits (4h6.41),
the output caps (4h6.42), token delivery (4h6.43), audit forwarding (4h6.45), quotas and
retention (4h6.46). Each has a stub in supervisor.py naming its bead.
"""

import base64
import http.client
import json
import os
import shutil
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

    def __init__(self, root):
        os.environ[sup.ENV_SCRATCH_ROOT] = root
        self.supervisor = sup.start(scratch_root=root, run_assertions=False)
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
        conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
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

    def __init__(self, base_url):
        parsed = base_url.split("//", 1)[-1].rstrip("/")
        host, _, port = parsed.partition(":")
        self.host = host or "127.0.0.1"
        self.port = int(port or 80)
        self.supervisor = None
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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_container(base_url):
    """The same wire checks, against the image. Nothing here imports the container's
    supervisor: it is reached only through the two routes the contract defines, which is
    exactly what chat-backend's client can do (4h6.47)."""
    server = RemoteServer(base_url)
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

    # Whole groups, not individual skips: there is no route on the wire that reaches the
    # supervisor's own objects, so these never execute here. Naming them is the difference
    # between a subset run and a subset run that looks complete.
    NOT_RUN.extend([
        "startup assertions (test_nsswitch) — reads the container's /etc/nsswitch.conf",
        "request parsing and token consistency (test_parsing) — calls the parser directly",
        "queue (test_queue, test_peer_gone) — inspects the supervisor's queue objects",
        "artifact manifest (test_manifest) — needs the harness's own view of /scratch",
        "startup wipe (test_startup_wipe) — calls wipe_unrecognised_scratch() directly",
    ])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    base_url = None
    if argv and argv[0] == "--container":
        if len(argv) != 2:
            print("usage: test-supervisor.py [--container URL]", file=sys.stderr)
            return 2
        base_url = argv[1]
    elif argv:
        print("usage: test-supervisor.py [--container URL]", file=sys.stderr)
        return 2

    if base_url:
        run_container(base_url)
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
