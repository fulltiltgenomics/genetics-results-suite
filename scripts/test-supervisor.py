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

CROSS-EXECUTION MEMORY ISOLATION (4h6.55, option (b)) IS TESTED AS THE PROPERTY, NOT THE
PLUMBING. The `isolation` group is the bead's own probe, run as a real execution in a real
forked child: it hunts one victim's token, source code and session id after that execution has
COMPLETED AND BEEN RELEASED, and a second victim's while that request is QUEUED behind the
probe, by all four demonstrated routes — module global, frame walk, gc, and a raw
/proc/self/mem scan. A clean result means nothing without a positive control, so the group
carries two and fails loudly if the primary one goes quiet. A test that the fork server starts
would prove none of this and is not what is here.

THE SAME PROBE, ONE LAYER LOWER (4h6.87), is `test_pre_ready_body_bytes`: a POST /execute whose
head and body share one TCP segment, refused 503 while the supervisor is still starting, with
ForkServer.start() gated to run milliseconds later — because under a realistic multi-second
prewarm the same probe recovers nothing and that is arena REUSE, not exclusion. Two details are
what make it a measurement rather than a ritual. The sender is a SUBPROCESS: the harness process
is the supervisor process, so a body built here with make_body() is in the fork snapshot however
the socket behaves, and the first version of this test failed for that reason with a correct fix
in place. And the three needles are asserted absent INDEPENDENTLY and UNCONDITIONALLY, with the
pre-fix rfile restorable by SUPERVISOR_TEST_BUFFERED_RFILE=1 as a negative control that is run,
not described. test_header_reader_units is the mechanics half and makes NO leak claim, so it may
build heads in-process; it carries two controls of its own. SUPERVISOR_TEST_DROP_LF_CRLF=1 drops
b"\n\r\n" from the terminator set and turns the \n\r\n case red. SUPERVISOR_TEST_STATIC_SEAM=1
restores the per-round seam window, which turns the one-byte-drip case red and takes the
terminator/chunk matrix from 28/28 to 26/28 — the same fail-open hang reached through peek size
rather than terminator shape.

WHAT AN EXECUTION LEAVES BEHIND (4h6.66, 4h6.83) AND WHAT A RETENTION WINDOW SERVES (4h6.82)
are both tested as the failure, not as the plumbing, and both carry negative controls that are
run rather than described. `test_survivors` leaves a real process behind a real
normally-completing execution — once inside the process group and once setsid()'d out of it —
and asserts it is gone AND REAPED afterwards; then it disables the group kill and the sweep and
asserts the same probes DO survive, which is the state both beads measured.
`test_artifact_integrity` overwrites a retained artifact with the same number of bytes and
plants a second one beside it, asserts both are refused, and then asserts that the same reads
with the digest binding disabled hand the attacker's content back. Neither group runs in
container mode and both say so by name.

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

THE AUDIT STREAM (4h6.45) IS THE ONE CONTROL WHOSE OUTPUT IS NOT ON THE WIRE. It leaves by the
pod's stdout, so the `audit stream` group reads that stream back — in process by capturing
sys.stdout, in container mode with `docker logs` (its own stdout only, which is what proves
the records went to stdout rather than merely somewhere visible), which needs
`--container-name` and skips by name without it. Every cap is watched firing there and every
forgery is watched failing: a child that rewrites SANDBOX_USER and writes its own `[user=…]`
prefix, one that appends a second record to a real one, one that writes a megabyte line, and
one that writes far above the rate and byte caps.
"""

import ast
import base64
import gc
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import signal
import socket
import socketserver
import subprocess
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


class _AliveForkServer:
    """A stand-in for tests that build a Supervisor directly and are not about the fork server.

    health() asks the fork server whether it is alive (a ready supervisor with none is a pod
    that cannot execute anything and must leave the endpoints), so a bare Supervisor needs one
    to be asked about."""

    pid = -1

    @staticmethod
    def alive():
        return True


def test_queue(tmp):
    root = os.path.join(tmp, "queue-root")
    os.makedirs(root)
    s = sup.Supervisor(root, ready=True)
    s.forkserver = _AliveForkServer()

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

    entries, omitted, digests = sup.build_manifest(d)
    names = [e["name"] for e in entries]
    check("manifest: lists plain regular files", names == ["plot.png"], f"got {names}")
    check("manifest: content_type from the name",
          entries and entries[0]["content_type"] == "image/png")
    check("manifest: size from fstat", entries and entries[0]["size"] == 16)
    check("manifest: omits and counts the rest", omitted == 6, f"got {omitted}")
    check("manifest: no path, no execution id, no url",
          all(set(e) == {"name", "size", "content_type"} for e in entries))

    # Every name build_manifest withheld must also be unreadable, and for the same reason:
    # the two run the same checks so the manifest never advertises what the read refuses,
    # and the read never serves what the manifest hid.
    data, ctype = sup.read_artifact_bytes(d, "plot.png", expected_digests=None)
    check("artifact read: returns the bytes and the name's content type",
          data == b"\x89PNG" * 4 and ctype == "image/png", f"got {len(data)} {ctype}")
    for name, status in (
        ("link.txt", 404),          # symlink
        ("hardlink.csv", 404),      # st_nlink != 1
        ("subdir", 404),            # not a regular file
        ("absent.png", 404),
        ("trailing.png ", 400),     # _name_is_retrievable
        ("../../etc/passwd", 400),
        ("new\nline.txt", 400),
        ("", 400),
    ):
        expect_request_error(f"artifact read: refuses {name!r}",
                             lambda n=name: sup.read_artifact_bytes(d, n, expected_digests=None),
                             status, "NotFound" if status == 404 else "InvalidRequest")

    check("manifest: only the listed name is hashed, and it is hashed",
          set(digests) == {"plot.png"} and digests["plot.png"] ==
          hashlib.sha256(b"\x89PNG" * 4).hexdigest(), f"got {digests}")

    with open(os.path.join(d, "big.bin"), "wb") as fh:
        fh.write(b"x" * 100)
    expect_request_error("artifact read: an oversize artifact is 413, not a truncated body",
                         lambda: sup.read_artifact_bytes(d, "big.bin", max_bytes=99,
                                                        expected_digests=None),
                         413, "ArtifactTooLarge")
    # Over the REAL read cap, not a test-local one: build_manifest hashes with the default, so
    # a 100-byte file with max_bytes=99 would not exercise the branch.
    with open(os.path.join(d, "huge.bin"), "wb") as fh:
        fh.write(b"x" * (sup.ARTIFACT_READ_MAX_BYTES + 1))
    over = sup.build_manifest(d, max_entries=10)[2]
    check("manifest: a file over the read cap is listed but has no digest, because it can "
          "never be served and a truncation must not make it servable",
          "huge.bin" in over and over["huge.bin"] is None, f"got {over.get('huge.bin')!r}")
    expect_request_error(
        "artifact read: a listed-but-unhashable file is refused, not served",
        lambda: sup.read_artifact_bytes(d, "big.bin", expected_digests={"big.bin": None}),
        409, "ArtifactModified")

    # The fail-open case must not be reachable by FORGETTING the argument: a future caller that
    # omits it would otherwise serve unverified bytes with no log line to show for it.
    try:
        sup.read_artifact_bytes(d, "plot.png")
    except TypeError:
        omitted_raises = True
    except Exception as exc:
        omitted_raises = f"raised {type(exc).__name__}"
    else:
        omitted_raises = "served the bytes"
    check("artifact read: omitting expected_digests raises rather than disabling the integrity "
          "binding — None disables it and has to be written",
          omitted_raises is True, f"got {omitted_raises}")


def test_artifact_integrity(tmp):
    """genetics-results-suite-4h6.82: the retention window must not serve bytes that moved.

    THE TAMPERING IS DONE BY THE HARNESS, AT THE HARNESS'S OWN UID, AND THAT IS THE THREAT
    MODEL RATHER THAN A SHORTCUT. The finding is that /scratch is writable by ANY process at
    the shared uid 65532 — measured from inside a second execution's child, which listed
    /scratch, read a previous execution's artifacts/private.csv, overwrote it and planted a new
    file beside it. Nothing about the primitive depends on the writer being a forked child, and
    routing the write through one would test the fork rather than the control. What is under
    test is what the SUPERVISOR does with a directory that has been altered behind it.

    Every assertion here carries its negative control in the same breath: the same read with
    the binding disabled must SERVE the attacker's bytes. Without that, an accidentally-empty
    artifacts directory would make the whole group pass.
    """
    root = os.path.join(tmp, "integrity")
    os.makedirs(root)
    eid = "22222222-2222-4222-8222-222222222222"
    dirs = sup.ExecutionDirs(root, eid)
    dirs.create()
    victim = b"SECRET-VICTIM-DATA"
    with open(os.path.join(dirs.artifacts, "private.csv"), "wb") as fh:
        fh.write(victim)

    s = sup.Supervisor(root, ready=True)
    entries, _, digests = sup.build_manifest(dirs.artifacts)
    s._retention[eid] = [time.monotonic() + 900, 0]
    s._record_digests(eid, digests)
    s._retained_ids.add(eid)

    data, ctype = s.read_artifact(eid, "private.csv")
    check("artifact integrity: an untouched artifact is still served",
          data == victim and ctype == "text/csv", f"got {data!r} {ctype}")

    # SAME LENGTH, so that nothing can pass by comparing sizes: st_size is unchanged and the
    # manifest chat-backend already holds still says 18 bytes.
    forged = b"ATTACKER-OWNED-YOU"
    check("artifact integrity: the probe's overwrite keeps the size identical",
          len(forged) == len(victim))
    with open(os.path.join(dirs.artifacts, "private.csv"), "wb") as fh:
        fh.write(forged)
    expect_request_error("artifact integrity: an OVERWRITTEN artifact is refused, not served",
                         lambda: s.read_artifact(eid, "private.csv"), 409, "ArtifactModified")
    served, _ = sup.read_artifact_bytes(dirs.artifacts, "private.csv", expected_digests=None)
    check("artifact integrity: NEGATIVE CONTROL — with the binding disabled the same read "
          "hands back the attacker's bytes",
          served == forged, f"got {served!r}")

    with open(os.path.join(dirs.artifacts, "planted.csv"), "wb") as fh:
        fh.write(b"PLANTED")
    entries_now, _, _ = sup.build_manifest(dirs.artifacts)
    check("artifact integrity: the planted file really is on disk and would be listed by a "
          "manifest built now",
          "planted.csv" in {e["name"] for e in entries_now})
    expect_request_error("artifact integrity: a PLANTED artifact — one no manifest ever "
                         "listed — is refused",
                         lambda: s.read_artifact(eid, "planted.csv"), 404, "NotFound")
    served, _ = sup.read_artifact_bytes(dirs.artifacts, "planted.csv", expected_digests=None)
    check("artifact integrity: NEGATIVE CONTROL — with the binding disabled the planted file "
          "is served",
          served == b"PLANTED", f"got {served!r}")

    # An execution retained by _register_retention rather than by _retain — an exception before
    # build_manifest ever ran — advertised nothing, so it serves nothing.
    other = "33333333-3333-4333-8333-333333333333"
    odirs = sup.ExecutionDirs(root, other)
    odirs.create()
    with open(os.path.join(odirs.artifacts, "orphan.csv"), "wb") as fh:
        fh.write(b"x")
    s._register_retention(other, odirs)
    s._retained_ids.add(other)
    expect_request_error("artifact integrity: an execution retained without a manifest serves "
                         "nothing at all",
                         lambda: s.read_artifact(other, "orphan.csv"), 404, "NotFound")
    expect_request_error("artifact integrity: and it still answers the name rules first, so "
                         "the digest map cannot mask a 400",
                         lambda: s.read_artifact(other, "../orphan.csv"), 400, "InvalidRequest")

    s._forget_retained(eid)
    check("artifact integrity: forgetting a retained execution drops its digest map too",
          eid not in s._artifact_digests, f"{sorted(s._artifact_digests)}")


def test_artifact_scoping(tmp):
    """The id is the authorisation, and only a RETAINED execution has one."""
    root = os.path.join(tmp, "scoping")
    os.makedirs(root)
    eid = "11111111-1111-4111-8111-111111111111"
    dirs = sup.ExecutionDirs(root, eid)
    dirs.create()
    with open(os.path.join(dirs.artifacts, "plot.png"), "wb") as fh:
        fh.write(b"\x89PNG")

    s = sup.Supervisor(root, ready=True)
    # What a completed execution leaves behind: a retention row and the digest map its manifest
    # was built from. Both are what _retain and _execute_inner write; see test_artifact_integrity
    # for what the map is FOR.
    s._retention[eid] = [time.monotonic() + 900, 0]
    s._record_digests(eid, sup.build_manifest(dirs.artifacts)[2])
    expect_request_error("artifact scoping: a directory that exists but is not retained is 404",
                         lambda: s.read_artifact(eid, "plot.png"), 404, "NotFound")
    expect_request_error("artifact scoping: a malformed execution id is 400",
                         lambda: s.read_artifact("../other", "plot.png"), 400, "InvalidRequest")

    s._retained_ids.add(eid)
    data, ctype = s.read_artifact(eid, "plot.png")
    check("artifact scoping: a retained execution serves its artifact",
          data == b"\x89PNG" and ctype == "image/png", f"got {data!r} {ctype}")
    expect_request_error("artifact scoping: retention does not widen the name rules",
                         lambda: s.read_artifact(eid, "../plot.png"), 400, "InvalidRequest")


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
        # Each Server brings up its own supervisor and therefore its own fork server; without
        # this the run accumulates one idle fork server per group.
        if self.supervisor is not None and self.supervisor.forkserver is not None:
            self.supervisor.forkserver.close()


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
        # GET /artifact is what makes this observable in container mode too, where there is
        # no host view of /scratch at all
        status, _, art = server.request(
            "GET", f"/artifact?execution_id={payload['execution_id']}&name=out.csv")
        check("http: the first execution's artifacts survive the refusal",
              status == 200 and base64.b64decode(art["content_base64"]) == b"a,b\n",
              f"got {status} {art}")
        check("http: /artifact reports the size and the name's content type",
              art["size"] == 4 and art["content_type"] == "text/csv", f"got {art}")

        status, _, body = server.request(
            "GET", f"/artifact?execution_id={payload['execution_id']}&name=absent.csv")
        check("http: /artifact 404s for a name the execution did not write",
              status == 404 and body["error"]["type"] == "NotFound", f"got {status} {body}")
        status, _, body = server.request(
            "GET", f"/artifact?execution_id={payload['execution_id']}"
                   "&name=..%2F..%2Fetc%2Fpasswd")
        check("http: /artifact refuses a name that is a path",
              status == 400 and body["error"]["type"] == "InvalidRequest", f"got {status} {body}")
        status, _, body = server.request(
            "GET", "/artifact?execution_id=00000000-0000-4000-8000-000000000000&name=out.csv")
        check("http: /artifact 404s for an execution id it does not hold",
              status == 404 and body["error"]["type"] == "NotFound", f"got {status} {body}")
        status, _, body = server.request("GET", "/artifact?execution_id=not-a-uuid&name=out.csv")
        check("http: /artifact rejects a malformed execution id",
              status == 400 and body["error"]["type"] == "InvalidRequest", f"got {status} {body}")
        status, _, body = server.request("POST", "/artifact", body={})
        check("http: POST /artifact -> 405", status == 405, f"got {status}")

        # 4h6.82 over the wire, against an artifact a REAL execution wrote and a REAL manifest
        # listed: the tamper is a plain write at the shared uid, which is the whole primitive.
        # Container mode has no host view of /scratch, so it cannot do the write.
        if server.host_scratch is None:
            skip("http: /artifact refuses a tampered artifact",
                 "no host view of /scratch in container mode")
        else:
            out_csv = os.path.join(server.host_scratch, payload["execution_id"],
                                   "artifacts", "out.csv")
            with open(out_csv, "wb") as fh:
                fh.write(b"x,y\n")     # same four bytes' worth of shape, different content
            status, _, body = server.request(
                "GET", f"/artifact?execution_id={payload['execution_id']}&name=out.csv")
            check("http: /artifact refuses an artifact that was overwritten after its manifest "
                  "was built",
                  status == 409 and body["error"]["type"] == "ArtifactModified",
                  f"got {status} {body}")
            planted = os.path.join(server.host_scratch, payload["execution_id"],
                                   "artifacts", "planted.csv")
            with open(planted, "wb") as fh:
                fh.write(b"p\n")
            status, _, body = server.request(
                "GET", f"/artifact?execution_id={payload['execution_id']}&name=planted.csv")
            check("http: /artifact refuses a file planted into a retained execution",
                  status == 404 and body["error"]["type"] == "NotFound", f"got {status} {body}")
            os.unlink(planted)
            with open(out_csv, "wb") as fh:
                fh.write(b"a,b\n")     # restore, so later checks see the execution's own bytes
            status, _, art = server.request(
                "GET", f"/artifact?execution_id={payload['execution_id']}&name=out.csv")
            check("http: NEGATIVE CONTROL — restoring the original bytes makes /artifact serve "
                  "it again, so the refusal is the digest and not the write",
                  status == 200 and base64.b64decode(art["content_base64"]) == b"a,b\n",
                  f"got {status} {art}")

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
            # 0-2, the status pipe (3), the audit pipe (4, genetics-results-suite-4h6.45) and
            # the listdir handle the print itself opened. The bound is what matters: a number
            # above that is a supervisor descriptor — the listening socket, another client's
            # connection — that _close_inherited_fds failed to sweep.
            check("child: inherits no descriptor beyond 0-4 and its own listdir handle",
                  all(int(f) <= 5 for f in fds), f"got {fds}")
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


def _all_retained_with_ceiling(tmp, ids, digests, ceiling=1 << 40):
    """Retain the same ids and maps with the memory ceiling raised, and count what survives.

    The negative control for the memory ceiling: without it, "everything was evicted" and "the
    supervisor evicts on some unrelated ground" look identical.
    """
    real = sup.RETAINED_STATE_CEILING_BYTES
    sup.RETAINED_STATE_CEILING_BYTES = ceiling
    try:
        sv = sup.Supervisor(tmp)
        for eid in ids:
            with sv._lock:
                sv._retention[eid] = [time.monotonic() + 900, 0]
            sv._retained_ids.add(eid)
            sv._record_digests(eid, dict(digests))
        return len(sv._retention)
    finally:
        sup.RETAINED_STATE_CEILING_BYTES = real


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
    entries, omitted, digests = sup.build_manifest(many)
    check("manifest: the entry count is capped",
          len(entries) == sup.ARTIFACT_ENTRY_BUDGET, f"got {len(entries)}")
    check("manifest: what it did not list is reported in artifacts_omitted",
          omitted == 200, f"got {omitted}")
    check("manifest: the digest map covers exactly what was listed, so the cap cannot leave "
          "a listed name unverifiable",
          set(digests) == {e["name"] for e in entries}, f"{len(digests)} vs {len(entries)}")
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

    # -- ZERO BYTES ON DISK IS NOT ZERO COST. 1024 empty artifacts with long names measure 0
    # against the disk ceiling and cost ~0.5 MB of digest map each, and the number of retained
    # executions has no count cap — so before RETAINED_STATE_CEILING_BYTES this accumulated for
    # the whole retention window with nothing able to evict it (pod OOM at 512 Mi).
    sv = sup.Supervisor(tmp)
    fat = {("a" * 200) + str(i): "0" * 64 for i in range(sup.ARTIFACT_ENTRY_BUDGET)}
    ids = []
    for i in range(24):
        eid = f"{i:08d}-0000-4000-8000-000000000000"
        ids.append(eid)
        with sv._lock:
            sv._retention[eid] = [time.monotonic() + 900, 0]   # zero BYTES on disk
        sv._retained_ids.add(eid)
        sv._record_digests(eid, dict(fat))
    held = sum(sv._retained_memory_costs().values())
    check("ceiling: the digest maps of executions that are free on disk are still bounded, "
          "oldest-first, by the memory ceiling",
          held <= sup.RETAINED_STATE_CEILING_BYTES and len(sv._retention) < len(ids),
          f"{held} bytes over {len(sv._retention)} retained rows")
    check("ceiling: NEGATIVE CONTROL — the same maps with the memory ceiling raised out of "
          "the way are all retained, so the bound above is the thing doing the work",
          _all_retained_with_ceiling(tmp, ids, fat) == len(ids),
          "eviction happened for some other reason")
    check("ceiling: eviction FAILS CLOSED — an evicted id is gone from the digest map and "
          "from _retained_ids, so it cannot serve unverified bytes",
          all((eid in sv._artifact_digests) == (eid in sv._retention)
              and (eid in sv._retained_ids) == (eid in sv._retention) for eid in ids),
          f"retained={len(sv._retention)} digests={len(sv._artifact_digests)} "
          f"ids={len(sv._retained_ids)}")

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


# --------------------------------------------------------------------------------------
# the SDK audit stream (genetics-results-suite-4h6.45)
# --------------------------------------------------------------------------------------

# The analyzer's own regex, COPIED and not imported: genetics-mcp-server
# src/genetics_mcp_server/scripts/analyze_conversations.py, SDK_CALL_RE. The two repos cannot
# share a module — the sandbox image installs only the SDK's import closure and prune_venv.py
# deletes the rest — so a divergence between what the supervisor emits and what the shipped
# parser reads has no other way to surface. A copy allowed to drift silently would be worse
# than none: if this stops matching, one of the two sides moved and the analyzer's reports are
# wrong, not this file.
ANALYZER_SDK_CALL_RE = re.compile(
    r"\[user=(?P<user>[^\]]*)\] \[session=(?P<session>[^\]]*)\] \[execution=(?P<execution>[^\]]*)\] "
    r"Executing SDK function: (?P<function>\S+) with input: (?P<arguments>.*?) "
    r"rows: (?P<rows>\d+)(?: error: (?P<error>\S+))?(?P<cancelled> cancelled)?$"
)

ANALYZER_REL_PATH = os.path.join(
    "src", "genetics_mcp_server", "scripts", "analyze_conversations.py")


def _sibling_analyzer_path():
    """genetics-mcp-server's analyze_conversations.py, if a checkout of it sits beside this
    one — plain sibling layout, and the `.claude/worktrees/<branch>` layout both repos use,
    where the sibling's matching worktree is preferred over its main checkout."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parts = repo.split(os.sep)
    candidates = []
    if len(parts) >= 3 and parts[-3:-1] == [".claude", "worktrees"]:
        main = os.sep.join(parts[:-3])
        sibling = os.path.join(os.path.dirname(main), "genetics-mcp-server")
        candidates.append(os.path.join(sibling, ".claude", "worktrees", parts[-1],
                                       ANALYZER_REL_PATH))
        candidates.append(os.path.join(sibling, ANALYZER_REL_PATH))
    candidates.append(os.path.join(os.path.dirname(repo), "genetics-mcp-server",
                                   ANALYZER_REL_PATH))
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _analyzer_sdk_call_pattern(path):
    """SDK_CALL_RE's pattern as the analyzer spells it, read from source rather than imported:
    importing it would pull in the whole analyzer's dependencies, which this harness has no
    business needing."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "SDK_CALL_RE" for t in node.targets):
            continue
        if isinstance(node.value, ast.Call) and node.value.args:
            try:
                return ast.literal_eval(node.value.args[0])
            except ValueError:
                return None
    return None


def check_analyzer_regex_copy():
    """The copy above is only useful if it is still the analyzer's regex.

    Watching the supervisor side alone is half a check: if the ANALYZER moves, every
    assertion built on the copy keeps passing against a pattern nothing ships. The two repos
    genuinely cannot import each other — the sandbox image installs only the SDK's import
    closure — so this reads the literal off disk when a sibling checkout is there and says by
    name when there is not, rather than passing quietly on a copy nobody compared."""
    name = "audit: the copied analyzer regex still matches the shipped analyzer's own"
    path = _sibling_analyzer_path()
    if path is None:
        skip(name, "no genetics-mcp-server checkout beside this one to read SDK_CALL_RE from")
        return
    pattern = _analyzer_sdk_call_pattern(path)
    check(name, pattern == ANALYZER_SDK_CALL_RE.pattern,
          f"{path}: {pattern!r} != {ANALYZER_SDK_CALL_RE.pattern!r}")

SDK_LINE = (
    "2026-01-01 00:00:00,000 - genetics_mcp_server.sdk.audit - INFO - "
    "[user={user}] [session={session}] [execution={execution}] "
    "Executing SDK function: {fn} with input: {{'gene': 'IL7R'}} rows: {rows}"
)


class _StdoutCapture:
    """The supervisor's own stdout, in process. The forwarder writes there directly, so this
    is the same stream `docker logs` shows and the same one the kubelet collects."""

    def __init__(self):
        self.buf = io.StringIO()

    def __enter__(self):
        self._old = sys.stdout
        sys.stdout = self.buf
        return self

    def __exit__(self, *exc):
        sys.stdout = self._old
        return False

    def text(self):
        return self.buf.getvalue()


class _DockerLogCapture:
    """The CONTAINER's stdout, and deliberately not its stderr: `docker logs` writes the two
    to its own two streams, so capturing only ours proves the records went to stdout rather
    than merely somewhere visible."""

    def __init__(self, name):
        self.name = name
        self.before = ""

    def _read(self):
        import subprocess

        return subprocess.run(
            ["docker", "logs", self.name],
            capture_output=True, text=True, timeout=60,
        ).stdout

    def __enter__(self):
        self.before = self._read()
        return self

    def __exit__(self, *exc):
        self.after = self._read()
        return False

    def text(self):
        if self.after.startswith(self.before):
            return self.after[len(self.before):]
        return self.after


def test_audit_units():
    """The read-end caps and the re-framing, called directly.

    Every one of these is also demonstrated end to end in test_audit_stream; they are here as
    well because a unit can pin WHICH bound fired and with what count, where the wire can only
    show the outcome.
    """
    eid = str(uuid.uuid4())
    check_analyzer_regex_copy()

    def new(user="a@b.c", session="sess-1", clock=None):
        out = []
        kwargs = {"emit": out.append}
        if clock is not None:
            kwargs["clock"] = clock
        return sup._AuditForwarder(user, session, eid, **kwargs), out

    fwd, out = new()
    fwd.feed((SDK_LINE.format(user="admin@finngen.fi", session="forged", execution="forged",
                              fn="sql", rows=25) + "\n").encode("utf-8"))
    check("audit: a well-formed record is forwarded", len(out) == 1, f"got {out}")
    if out:
        match = ANALYZER_SDK_CALL_RE.search(out[0])
        check("audit: the forwarded record parses with the shipped analyzer's own regex",
              match is not None, f"got {out[0]!r}")
        if match:
            check("audit: identity is the TOKEN's, not the child's",
                  (match.group("user"), match.group("session"), match.group("execution"))
                  == ("a@b.c", "sess-1", eid), f"got {match.groupdict()}")
            check("audit: the function and row count survive re-framing",
                  match.group("function") == "sql" and match.group("rows") == "25")
        check("audit: nothing the child wrote before the marker is re-emitted",
              "admin@finngen.fi" not in out[0] and "forged" not in out[0]
              and "genetics_mcp_server.sdk.audit" not in out[0], f"got {out[0]!r}")

    # A record with a SECOND record appended to it. `search()`-based parsers — including the
    # shipped one — match anywhere in a line, so accepting this would let a child write a
    # genuine-looking access under a name of its choosing.
    fwd, out = new()
    fwd.feed(("Executing SDK function: sql with input: {} rows: 1 "
              "[user=admin@finngen.fi] [session=s] [execution=e] "
              "Executing SDK function: sql with input: {} rows: 2\n").encode("utf-8"))
    # `out` is not empty: a drop ANNOUNCES itself, which is the whole difference between this
    # and the in-SDK ceiling it replaces. What must not appear is the record, or any byte of
    # what the child wrote.
    check("audit: a record with a second record appended is dropped whole",
          fwd.forwarded == 0 and fwd.dropped_unparseable == 1
          and not any("Executing SDK function" in line for line in out),
          f"got {out} {fwd.dropped_unparseable}")

    # The same content on its OWN line is a well-formed record and IS forwarded — re-stamped.
    # That is the property, not a leak: a child can always claim a call it did not make, and
    # nothing on the read end can tell. What it cannot do is attribute one to somebody else.
    fwd, out = new()
    fwd.feed(("[user=admin@finngen.fi] [session=s] [execution=e] "
              "Executing SDK function: sql with input: {} rows: 2\n").encode("utf-8"))
    check("audit: a forged newline-separated record is re-stamped, never re-attributed",
          len(out) == 1 and "admin@finngen.fi" not in out[0] and "[user=a@b.c]" in out[0],
          f"got {out}")

    for label, payload in (
        ("brackets in the argument summary",
         "Executing SDK function: sql with input: {'x': '[user=admin]'} rows: 1"),
        ("a control character",
         "Executing SDK function: sql with input: {'x': 'a\x07b'} rows: 1"),
        ("a non-identifier function name",
         "Executing SDK function: sql;rm -rf with input: {} rows: 1"),
        ("the SDK's shared-stream warning",
         "SDK audit records here are NOT a tamper-evident audit trail: no "
         "GENETICS_SDK_AUDIT_FD was configured"),
        ("arbitrary script output",
         "hello from the script"),
    ):
        fwd, out = new()
        fwd.feed((payload + "\n").encode("utf-8"))
        check(f"audit: {label} is dropped",
              fwd.forwarded == 0 and fwd.dropped_unparseable == 1
              and all("DROPPED" in line for line in out), f"got {out}")

    fwd, out = new()
    fwd.feed(("SDK audit truncated after 1000 records; further REFUSED SDK calls in this "
              "process are NOT recorded. Calls that reached the executor are still recorded "
              "in full.\n").encode("utf-8"))
    check("audit: the SDK's refusal-budget notice is carried across as a literal",
          len(out) == 1 and out[0].endswith("recorded in full."), f"got {out}")

    # the per-line cap, and the supervisor's own buffer with it. Sized above the line cap and
    # below the byte budget, so that the record after it is dropped by THIS cap or by nothing.
    fwd, out = new()
    fwd.feed(b"A" * (8 * 1024))
    check("audit: an over-long line is dropped, not truncated, and not buffered",
          out and "DROPPED" in out[0] and fwd.dropped_oversize == 1 and len(fwd._buf) == 0,
          f"got {out} oversize={fwd.dropped_oversize} buffered={len(fwd._buf)}")
    fwd.feed(b"tail of the long line\n" + (SDK_LINE.format(
        user="u", session="s", execution="e", fn="coloc", rows=3) + "\n").encode("utf-8"))
    check("audit: a genuine record after an over-long one is still forwarded",
          any("Executing SDK function: coloc" in line for line in out), f"got {out}")

    # the rate cap, on a frozen clock so the bucket cannot refill
    fwd, out = new(clock=lambda: 1000.0)
    record = (SDK_LINE.format(user="u", session="s", execution="e", fn="sql", rows=1)
              + "\n").encode("utf-8")
    fwd.feed(record * (sup.AUDIT_RATE_BURST + 50))
    forwarded = [line for line in out if "Executing SDK function:" in line]
    check("audit: the rate cap drops past the burst and announces itself once",
          len(forwarded) == sup.AUDIT_RATE_BURST and fwd.dropped_rate == 50
          and sum("records/s" in line for line in out) == 1,
          f"got forwarded={len(forwarded)} dropped={fwd.dropped_rate}")

    # the byte budget, on a clock that advances so the rate cap cannot be what fires
    ticks = iter(range(10 ** 6))
    fwd, out = new(clock=lambda: float(next(ticks)))
    while fwd.bytes_seen <= sup.AUDIT_STREAM_MAX_BYTES + 4096:
        fwd.feed(record * 64)
    check("audit: the per-execution byte budget drops past its cap and announces itself",
          fwd.dropped_over_budget > 0 and sum("byte per-execution budget" in line
                                              for line in out) == 1,
          f"got over_budget={fwd.dropped_over_budget}")

    # THE BUDGET MUST NOT TRUNCATE A RECORD AND FORWARD THE FRAGMENT. The child owns every
    # byte on the fd, so it owns where the boundary falls: padding to just under the budget
    # and then writing one record makes the cut land mid-record, and a forwarded prefix parses
    # as a DIFFERENT record than the child wrote (`rows: 999999999` -> `rows: 9`) under the
    # real user's stamp, counted as forwarded so nothing downstream can tell. The pad is
    # oversize lines deliberately: they are dropped whole and spend no rate token, which is
    # what makes the setup cost the child nothing.
    ticks = iter(range(10 ** 6))
    fwd, out = new(clock=lambda: float(next(ticks)))
    trap = b"Executing SDK function: sql with input: {} rows: 999999999\n"
    room = trap.index(b"rows: ") + len("rows: ") + 1     # the cut keeps one digit: `rows: 9`
    fwd.feed(b"x" * (sup.AUDIT_STREAM_MAX_BYTES - room - 1) + b"\n")
    fwd.feed(trap)
    fwd.close()
    check("audit: the byte budget drops a cut record rather than forwarding the fragment",
          fwd.forwarded == 0 and not any("Executing SDK function" in line for line in out)
          and fwd.dropped_over_budget == 1,
          f"got forwarded={fwd.forwarded} over_budget={fwd.dropped_over_budget} {out}")

    # A flood past the budget carrying NO newline at all: counting newlines alone reported
    # dropped_over_budget=0 and left `bytes=` as the only evidence anything was lost.
    ticks = iter(range(10 ** 6))
    fwd, out = new(clock=lambda: float(next(ticks)))
    while fwd.bytes_seen <= sup.AUDIT_STREAM_MAX_BYTES + 65536:
        fwd.feed(b"z" * 65536)
    fwd.close()
    check("audit: a newline-free flood past the budget is counted, not only weighed",
          fwd.dropped_over_budget >= 1, f"got over_budget={fwd.dropped_over_budget}")

    # `<unavailable>` — the BARE STRING, no braces — is what _summarize_arguments returns when
    # signature.bind_partial raises TypeError, i.e. whenever a script passes one extra
    # positional or one unknown keyword. An ordinary buggy script, not an attack: dropping it
    # put a genuine record into dropped_unparseable, where an operator reads it as tampering.
    for label, payload in (
        ("an executed call", "Executing SDK function: gene with input: <unavailable> "
                             "rows: 0 error: TypeError"),
        ("a rejected call", "Rejected SDK function: gene with input: <unavailable> "
                            "error: TypeError"),
    ):
        fwd, out = new()
        fwd.feed((payload + "\n").encode("utf-8"))
        check(f"audit: the SDK's <unavailable> argument summary is a record on {label}",
              fwd.forwarded == 1 and fwd.dropped_unparseable == 0
              and out and out[0].endswith(payload), f"got {out}")

    # The argument charset is the SDK's, which is pure ASCII (_AUDIT_SAFE_VALUE_RE), and the
    # read end is where that has to be held: `<type>` renders type(value).__name__, which a
    # script owns. U+2028/U+2029/U+0085 each split the emitted record into TWO lines under
    # str.splitlines() — what this harness and plenty of log tooling read records with.
    for label, payload in (
        ("a U+2028 line separator", "\u2028"),
        ("a U+2029 paragraph separator", "\u2029"),
        ("a U+0085 next-line", "\u0085"),
        ("a U+202E right-to-left override", "\u202e"),
        ("a non-breaking space", "\u00a0"),
        ("fullwidth brackets", "\uff3b\uff3d"),
    ):
        fwd, out = new()
        fwd.feed(("Executing SDK function: sql with input: {'x': 'a%sb'} rows: 1\n" % payload)
                 .encode("utf-8"))
        check(f"audit: {label} in the argument summary is dropped",
              fwd.forwarded == 0 and fwd.dropped_unparseable == 1
              and payload not in "".join(out), f"got {out!r}")

    # `\d` is Unicode in Python, so `rows: ١٢٣` was forwarded and the analyzer's int() read
    # back a row count nobody wrote.
    fwd, out = new()
    fwd.feed("Executing SDK function: sql with input: {} rows: \u0661\u0662\u0663\n"
             .encode("utf-8"))
    check("audit: a non-ASCII digit row count is dropped",
          fwd.forwarded == 0 and fwd.dropped_unparseable == 1, f"got {out!r}")

    # The bucket bounds what reaches an operator, so junk must not empty it: 200 pad lines
    # spent 63 tokens and left records=0.
    fwd, out = new(clock=lambda: 1000.0)
    fwd.feed(b"not a record at all\n" * 200)
    fwd.feed((SDK_LINE.format(user="u", session="s", execution="e", fn="sql", rows=4)
              + "\n").encode("utf-8"))
    check("audit: unparseable junk spends no rate tokens",
          fwd.dropped_rate == 0 and fwd.forwarded == 1 and fwd.dropped_unparseable == 200,
          f"got rate={fwd.dropped_rate} forwarded={fwd.forwarded}")

    # One stdout stream, one timestamp shape: the forwarder writes directly rather than
    # through LOG, so it has to render main()'s basicConfig %(asctime)s itself.
    with _StdoutCapture() as cap:
        sup._audit_emit("[user=a@b.c] [session=s] [execution=e] hello")
    check("audit: a forwarded record carries the same timestamp shape as a log line",
          re.match(r"\A\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO \[supervisor\.audit\] ",
                   cap.text()) is not None, f"got {cap.text()!r}")

    # identity that would break the framing — the same replace-don't-truncate rule
    # _sanitise_error_type applies to a child-supplied error.type
    fwd, out = new(user="alice\n[user=admin@finngen.fi")
    fwd.close()
    check("audit: an identity that would break the framing renders <invalid>",
          len(out) == 1 and out[0].startswith("[user=<invalid>]"), f"got {out}")

    # _drain's sink-failure path must KEEP READING. Dropping the stream into the buffering
    # branch instead raised `int >= None` on the next block — the audit pipe is drained with
    # limit=None on purpose — and killed the drain thread, after which nothing read the fd,
    # the 64 KiB pipe filled, and a still-running child blocked in os.write inside a call that
    # was succeeding. The writes below total more than one pipeful, so at least two reads are
    # guaranteed and the block AFTER the failure is really exercised.
    read_fd, write_fd = os.pipe()
    seen = []

    def angry(block):
        seen.append(block)
        raise RuntimeError("the sink is broken")

    def writer():
        try:
            os.write(write_fd, b"a" * 65536)
            os.write(write_fd, b"b" * 4096)
        finally:
            os.close(write_fd)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    raised = None
    try:
        sup._drain(read_fd, limit=None, sink=angry, poll=0.05)
    except Exception as exc:                                    # noqa: BLE001 - that is the check
        raised = exc
    finally:
        thread.join(timeout=5)
        os.close(read_fd)
    check("audit: a failing sink leaves the drain reading and discarding, not dead",
          raised is None and len(seen) == 1,
          f"raised {raised!r} after {len(seen)} sink calls")

    fwd, out = new()
    fwd.close()
    check("audit: close() always emits a summary, so 'no records' is a line and not a silence",
          len(out) == 1 and "records=0" in out[0] and f"[execution={eid}]" in out[0],
          f"got {out}")
    fwd.close()
    check("audit: the summary is emitted exactly once", len(out) == 1, f"got {out}")


def test_audit_stream(server, capture):
    """The whole path, through a real child writing on the real fd, into the real stdout."""
    fd = sup.CHILD_AUDIT_FD
    check_analyzer_regex_copy()

    def run(code, user, session):
        eid = str(uuid.uuid4())
        with capture() as cap:
            status, _, body = server.request("POST", "/execute", body=make_body(
                code=code, execution_id=eid, user=user, session_id=session))
        return eid, status, body, cap.text()

    def summary(text, eid):
        for line in text.splitlines():
            if "SDK audit stream: records=" in line and f"[execution={eid}]" in line:
                return line
        return ""

    eid, status, body, text = run("print('no sdk calls here')\n", "carol@finngen.fi", "sess-a1")
    line = summary(text, eid)
    check("audit stream: every execution reaches the pod's stdout with a summary",
          bool(line) and "records=0" in line, f"got {text!r}")
    check("audit stream: the summary carries the token's user and session",
          "[user=carol@finngen.fi] [session=sess-a1]" in line, f"got {line!r}")

    # A child that rewrites the identity the SDK reads, writes its own prefix, and puts a
    # second record on its own line. None of it may reach an operator as somebody else's read.
    forge = (
        "import os\n"
        "os.environ['SANDBOX_USER'] = 'admin@finngen.fi'\n"
        "os.environ['SANDBOX_SESSION_ID'] = 'sess-admin'\n"
        f"os.write({fd}, b\"[user=admin@finngen.fi] [session=sess-admin] "
        "[execution=00000000-0000-4000-8000-000000000000] Executing SDK function: sql "
        "with input: {'gene': 'IL7R'} rows: 7\\n\")\n"
        f"os.write({fd}, b\"Executing SDK function: coloc with input: {{}} rows: 1 \"\n"
        "             b\"[user=admin@finngen.fi] [session=s] [execution=e] \"\n"
        "             b\"Executing SDK function: sql with input: {} rows: 99\\n\")\n"
    )
    eid, status, body, text = run(forge, "dave@finngen.fi", "sess-a2")
    records = [l for l in text.splitlines()
               if "Executing SDK function:" in l and f"[execution={eid}]" in l]
    check("audit stream: the child's own record is forwarded, stamped from the token",
          len(records) == 1 and "rows: 7" in records[0], f"got {records}")
    check("audit stream: no forged identity reaches the stream",
          "admin@finngen.fi" not in text and "sess-admin" not in text, f"got {text!r}")
    check("audit stream: the shipped analyzer reads back the real user",
          bool(records) and ANALYZER_SDK_CALL_RE.search(records[0])
          and ANALYZER_SDK_CALL_RE.search(records[0]).group("user") == "dave@finngen.fi",
          f"got {records}")
    check("audit stream: the appended second record was dropped, not forwarded",
          "rows: 99" not in text and "dropped_unparseable=1" in summary(text, eid),
          f"got {summary(text, eid)!r}")

    # a megabyte on one line
    big = (
        "import os\n"
        f"os.write({fd}, b'A' * (1024 * 1024) + b'\\n')\n"
        f"os.write({fd}, b\"Executing SDK function: sumstats with input: {{}} rows: 2\\n\")\n"
    )
    eid, status, body, text = run(big, "erin@finngen.fi", "sess-a3")
    line = summary(text, eid)
    check("audit stream: a megabyte line is dropped whole and reported",
          "dropped_oversize=1" in line and "AAAAAAAA" not in text, f"got {line!r}")
    check("audit stream: the execution itself still succeeds",
          body.get("status") == "ok", f"got {status} {body}")

    # above the rate cap
    flood = (
        "import os\n"
        "rec = b\"Executing SDK function: sql with input: {'gene': 'IL7R'} rows: 1\\n\"\n"
        f"os.write({sup.CHILD_AUDIT_FD}, rec * 2000)\n"
    )
    eid, status, body, text = run(flood, "frank@finngen.fi", "sess-a4")
    line = summary(text, eid)
    dropped = int(line.split("dropped_rate=")[1].split()[0]) if "dropped_rate=" in line else 0
    forwarded = int(line.split("records=")[1].split()[0]) if "records=" in line else -1
    check("audit stream: a flood above the rate cap is bounded and counted",
          dropped > 0 and 0 < forwarded <= sup.AUDIT_RATE_BURST + sup.AUDIT_RATE_PER_S,
          f"got {line!r}")
    check("audit stream: the rate cap announces itself in the stream",
          any("records/s" in l and f"[execution={eid}]" in l for l in text.splitlines()),
          f"got {text!r}")

    # past the per-execution byte budget
    over = (
        "import os\n"
        "rec = b\"Executing SDK function: sql with input: {'gene': 'IL7R'} rows: 1\\n\"\n"
        f"os.write({sup.CHILD_AUDIT_FD}, rec * 20000)\n"
    )
    eid, status, body, text = run(over, "gina@finngen.fi", "sess-a5")
    line = summary(text, eid)
    over_budget = (int(line.split("dropped_over_budget=")[1].split()[0])
                   if "dropped_over_budget=" in line else 0)
    bytes_seen = int(line.split("bytes=")[1].split()[0]) if "bytes=" in line else 0
    check("audit stream: the per-execution byte budget fires and the reader keeps reading",
          over_budget > 0 and bytes_seen > sup.AUDIT_STREAM_MAX_BYTES, f"got {line!r}")
    check("audit stream: a child past the budget is not blocked and still exits cleanly",
          body.get("status") == "ok", f"got {status} {body}")


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
# 11. cross-execution memory isolation (genetics-results-suite-4h6.55, option (b))
# --------------------------------------------------------------------------------------
#
# THIS GROUP TESTS THE PROPERTY, NOT THE PLUMBING. 4h6.55 demonstrated a child recovering
# another user's tokens, source code and session id by four routes; a test that the fork
# server starts would prove none of them closed. So the probe below IS the bead's probe: it
# runs as a real execution, in a real forked child, and goes looking.
#
# THE POSITIVE CONTROLS ARE THE LOAD-BEARING PART. A search that finds nothing proves nothing
# unless the same search finds something it should, so the probe carries two:
#   * a string planted in the supervisor module BEFORE the fork server is forked. It is in the
#     fork server's inherited pages by construction, so every route that can read inherited
#     memory MUST report it. If /proc/self/mem stops working (gVisor, a hardened /proc), this
#     control goes red and the group fails loudly instead of passing vacuously.
#   * the probe's own token, read from its own token file. It proves the needle shape and the
#     matcher are capable of finding a credential in this address space.
#
# NEEDLES ARE CARRIED AS SPLIT HALVES and never concatenated in the probe. A probe that held
# the whole needle would find it in its own code object and report a hit against itself.

_ISOLATION_PROBE = r'''
import gc, json, os, sys, time
import collections

PAIRS = __PAIRS__          # [[label, first_half, second_half], ...]
SLEEP_S = __SLEEP_S__

def _hit(text, a, b):
    i = text.find(a)
    while i != -1:
        if text[i + len(a): i + len(a) + len(b)] == b:
            return True
        i = text.find(a, i + 1)
    return False

def _hit_bytes(blob, a, b):
    a = a.encode(); b = b.encode()
    i = blob.find(a)
    while i != -1:
        if blob[i + len(a): i + len(a) + len(b)] == b:
            return True
        i = blob.find(a, i + 1)
    return False

_SEQS = (list, tuple, set, frozenset, collections.deque)

def _harvest(roots, depth=8, budget=2000000):
    """Every string reachable from `roots`. Covers module globals, frame dicts and instance
    attributes including __slots__, which is what the bead's routes 1-3 walked by hand."""
    seen = set()
    stack = [(r, 0) for r in roots]
    n = 0
    while stack and n < budget:
        obj, d = stack.pop()
        if d > depth:
            continue
        oid = id(obj)
        if oid in seen:
            continue
        seen.add(oid)
        if isinstance(obj, str):
            n += 1
            yield obj
            continue
        if isinstance(obj, (bytes, bytearray)):
            n += 1
            yield bytes(obj).decode('utf-8', 'replace')
            continue
        if isinstance(obj, dict):
            for k, v in list(obj.items())[:50000]:
                stack.append((k, d + 1))
                stack.append((v, d + 1))
            continue
        if isinstance(obj, _SEQS):
            for v in list(obj)[:50000]:
                stack.append((v, d + 1))
            continue
        dd = getattr(obj, '__dict__', None)
        if isinstance(dd, dict):
            stack.append((dd, d + 1))
        for slot in getattr(type(obj), '__slots__', ()) or ():
            try:
                stack.append((getattr(obj, slot), d + 1))
            except Exception:
                pass

def route_module_global():
    roots = [m for m in list(sys.modules.values()) if getattr(m, 'SUPERVISOR', None) is not None]
    roots = [m.SUPERVISOR for m in roots] + [getattr(m, '__dict__', {}) for m in roots]
    return _harvest(roots)

def route_frames():
    roots = []
    f = sys._getframe()
    while f is not None:
        roots.append(f.f_locals)
        roots.append(f.f_globals)
        f = f.f_back
    return _harvest(roots, depth=6)

def route_gc():
    for obj in gc.get_objects():
        for ref in gc.get_referents(obj):
            if isinstance(ref, str):
                yield ref
            elif isinstance(ref, (bytes, bytearray)):
                yield bytes(ref).decode('utf-8', 'replace')

def scan_refs(route, out):
    for text in route:
        for label, a, b in PAIRS:
            if label in out:
                continue
            if _hit(text, a, b):
                out.add(label)

def scan_mem(out):
    """The route that decided the design: the raw address space, which no amount of dropping
    references can clean because freed strings stay in the arenas COW hands over.

    Its findings are kept SEPARATE from the reference routes'. Folding them together lets a
    dead memory scan hide behind a live reference hit, and this is the route the bead says
    decides the design — it has to be shown working on its own.
    """
    try:
        lines = open('/proc/self/maps').read().splitlines()
        mem = os.open('/proc/self/mem', os.O_RDONLY)
    except OSError as exc:
        return 'unavailable: %s' % exc
    scanned = 0
    try:
        for line in lines:
            parts = line.split()
            if len(parts) < 2 or 'r' not in parts[1]:
                continue
            path = parts[5] if len(parts) > 5 else ''
            if path in ('[vvar]', '[vdso]', '[vsyscall]', '[vvar_vclock]'):
                continue
            lo, _, hi = parts[0].partition('-')
            try:
                lo = int(lo, 16); hi = int(hi, 16)
            except ValueError:
                continue
            size = hi - lo
            if size <= 0 or size > 64 * 1024 * 1024 or scanned > 768 * 1024 * 1024:
                continue
            try:
                os.lseek(mem, lo, os.SEEK_SET)
                blob = os.read(mem, size)
            except OSError:
                continue
            scanned += len(blob)
            for label, a, b in PAIRS:
                if label not in out and _hit_bytes(blob, a, b):
                    out.add(label)
    finally:
        os.close(mem)
    return scanned

result = {}
for phase in ('released', 'queued'):
    if phase == 'queued':
        time.sleep(SLEEP_S)
    found = set()
    scan_refs(route_module_global(), found)
    scan_refs(route_frames(), found)
    scan_refs(route_gc(), found)
    in_mem = set()
    mem = scan_mem(in_mem)
    result[phase] = {'found': sorted(found | in_mem), 'ref_found': sorted(found),
                     'mem_found': sorted(in_mem),
                     'mem': mem if isinstance(mem, str) else 'ok'}
print('PROBERESULT ' + json.dumps(result))
'''


def _pair(value):
    """A needle as two halves, so the probe never holds the whole thing."""
    cut = len(value) // 2
    return [value[:cut], value[cut:]]


def test_isolation(tmp):
    root = os.path.join(tmp, "isolation")
    os.makedirs(root)

    # PLANTED BEFORE THE SERVER IS BUILT, and that ordering is the control. bring_up() forks
    # the fork server, so anything set on the module now is in the fork server's inherited
    # pages and every child must be able to see it.
    control = "FORKSRVCTL" + os.urandom(16).hex().upper()
    sup.ISOLATION_TEST_CONTROL = control

    # THE SECOND CONTROL IS THE BEAD'S OWN SANITY PROBE: a string dropped and gc.collect()-ed
    # BEFORE the fork, which the bead measured still recoverable in the child. It is what
    # proves the raw scan reads FREED arenas and not merely live objects, and therefore what
    # makes "reference-based clearing cannot work" a measurement rather than a claim. It is
    # reported rather than required — an arena can legitimately be reused or returned to the
    # OS between the drop and the scan, and a control that is right most of the time must not
    # be allowed to fail a suite.
    # 4 KiB of padding so the allocation goes to malloc rather than a pymalloc pool: a 44-byte
    # string lands in a size class that the supervisor's own startup reuses within microseconds,
    # which makes the control skip for a reason that has nothing to do with the property.
    freed = "FORKSRVFREED" + os.urandom(16).hex().upper() + "." * 4096
    freed_pair = _pair(freed[:44])   # the halves survive; the whole string must not
    sup.ISOLATION_TEST_FREED = freed
    del sup.ISOLATION_TEST_FREED
    del freed
    gc.collect()

    server = Server(root)
    try:
        # -- victim 1: runs to completion and is released before the probe starts.
        v1 = os.urandom(12).hex().upper()
        body1 = make_body(code=f"x = 'VICTIMCODE{v1}'\nprint('victim one')\n",
                          user=f"v1-{v1[:8]}@b.c", session_id=f"sess-{v1}")
        status, _, _ = server.request("POST", "/execute", body1)
        check("isolation: the released victim executed", status == 200, f"got {status}")

        # -- victim 2 will be QUEUED behind the probe. Its markers are known now so the probe
        # can carry them; the request itself is sent after the probe is running.
        v2 = os.urandom(12).hex().upper()
        body2 = make_body(code=f"y = 'VICTIMCODE{v2}'\nprint('victim two')\n",
                          user=f"v2-{v2[:8]}@b.c", session_id=f"sess-{v2}")

        pairs = [
            ["control-forkserver", *_pair(control)],
            ["control-freed", *freed_pair],
            ["released-token", *_pair(body1["tokens"]["db-api"].split(".")[1])],
            ["released-code", *_pair("VICTIMCODE" + v1)],
            ["released-session", *_pair("sess-" + v1)],
            ["queued-token", *_pair(body2["tokens"]["db-api"].split(".")[1])],
            ["queued-code", *_pair("VICTIMCODE" + v2)],
            ["queued-session", *_pair("sess-" + v2)],
        ]
        probe_code = (_ISOLATION_PROBE
                      .replace("__PAIRS__", json.dumps(pairs))
                      .replace("__SLEEP_S__", "2.0"))
        probe_body = make_body(code=probe_code, user="probe@b.c", session_id="sess-probe")
        probe_body["timeout_s"] = 60

        box = {}
        t = threading.Thread(
            target=lambda: box.update(zip(("status", "retry", "body"),
                                          server.request("POST", "/execute", probe_body))),
            daemon=True)
        t.start()
        # Long enough for the probe to be the running execution, short enough that victim 2 is
        # still queued when the probe's second phase scans.
        time.sleep(0.6)
        box2 = {}
        t2 = threading.Thread(
            target=lambda: box2.update(zip(("status", "retry", "body"),
                                           server.request("POST", "/execute", body2))),
            daemon=True)
        t2.start()
        t.join(120)
        t2.join(120)

        body = box.get("body") or {}
        check("isolation: the probe execution completed",
              box.get("status") == 200 and body.get("status") == "ok",
              f"got {box.get('status')} {str(body)[:300]}")
        check("isolation: victim two was queued behind the probe and then ran",
              box2.get("status") == 200, f"got {box2.get('status')}")

        line = ""
        for candidate in (body.get("output") or "").splitlines():
            if candidate.startswith("PROBERESULT "):
                line = candidate[len("PROBERESULT "):]
        try:
            result = json.loads(line)
        except Exception:
            result = None
        check("isolation: the probe reported a result",
              isinstance(result, dict) and set(result) == {"released", "queued"},
              f"output was {str(body.get('output'))[:400]}")
        if not isinstance(result, dict):
            return

        for phase in ("released", "queued"):
            found = set(result[phase]["found"])
            in_mem = set(result[phase]["mem_found"])
            in_refs = set(result[phase].get("ref_found", ()))
            check(f"isolation [{phase}]: the positive control IS reachable, so the search works",
                  "control-forkserver" in found, f"found {sorted(found)}")
            # SEPARATELY FROM THE MEM SCAN, because `found` is the union and the union hid a
            # dead search: SABOTAGED, making scan_refs return immediately — which kills the
            # module-global, frame-walk and gc routes all at once — left this suite GREEN,
            # since the mem hit alone satisfied the check above. Three of the four advertised
            # routes had no positive control at all.
            check(f"isolation [{phase}]: the reference routes (module global, frame walk, gc) "
                  f"reach the positive control on their own",
                  "control-forkserver" in in_refs, f"the reference routes found {sorted(in_refs)}")
            check(f"isolation [{phase}]: the raw /proc/self/mem scan reaches the fork "
                  f"server's inherited pages",
                  result[phase]["mem"] == "ok" and "control-forkserver" in in_mem,
                  f"mem said {result[phase]['mem']}, found {sorted(in_mem)}")
            if "control-freed" in in_mem:
                check(f"isolation [{phase}]: the memory scan recovers a string FREED before "
                      f"the fork, which is why no reference-clearing fix could have worked",
                      True)
            else:
                skip(f"isolation [{phase}]: the freed-string control",
                     "its arena was reused or returned before the scan; the live-object "
                     "control above still proves the scan reads inherited pages")
            leaked = sorted(found - {"control-forkserver", "control-freed"})
            check(f"isolation [{phase}]: no other execution's token, code or session id "
                  f"is reachable from the child", not leaked, f"LEAKED {leaked}")
    finally:
        server.close()


def test_forkserver_units(tmp):
    """The two invariants the fork server exists to hold, checked directly rather than through
    an execution: it never receives user data, and the payload never touches a named file."""
    payload = {"code": "print(1)", "env": {"A": "b"}, "cwd": tmp}
    for name, forced in (("memfd", False), ("fallback", True)):
        real = getattr(sup.os, "memfd_create", None)
        if forced and real is not None:
            sup.os.memfd_create = lambda *a, **k: (_ for _ in ()).throw(OSError("forced"))
        try:
            before = set(os.listdir(tmp))
            fd = sup._payload_fd(payload, tmp)
            try:
                check(f"payload fd ({name}): leaves no name behind",
                      set(os.listdir(tmp)) == before, f"{set(os.listdir(tmp)) - before}")
                check(f"payload fd ({name}): round-trips code, env and cwd",
                      sup._read_payload(fd) == (payload["code"], payload["env"], payload["cwd"]))
            finally:
                os.close(fd)
        finally:
            if forced and real is not None:
                sup.os.memfd_create = real

    big = {"code": "x" * (sup.PAYLOAD_MAX_BYTES + 1), "env": {}, "cwd": tmp}
    fd = sup._payload_fd(big, tmp)
    try:
        try:
            sup._read_payload(fd)
        except ValueError:
            check("payload fd: an over-cap payload is refused rather than read", True)
        else:
            check("payload fd: an over-cap payload is refused rather than read", False,
                  "it was read")
    finally:
        os.close(fd)

    # The control protocol carries an op name and nothing else. This is the check that fails
    # if somebody later "just adds the execution id" to the fork message.
    src = open(os.path.join(ROOT, "sandbox", "supervisor.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    sent = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "_round_trip"):
            continue
        for arg in node.args[:1]:
            if isinstance(arg, ast.Dict):
                sent.append({k.value for k in arg.keys if isinstance(k, ast.Constant)})
    check("fork server protocol: every control message is drawn from a fixed key set",
          sent and all(keys <= {"op", "pid", "nohang"} for keys in sent), f"{sent}")

    fs = sup.ForkServer.start()
    try:
        check("fork server: it is in the supervisor's own process group, so _resolve_pgid's "
              "guard still catches a child that has not reached setsid()",
              os.getpgid(fs.pid) == os.getpgrp(), f"{os.getpgid(fs.pid)} vs {os.getpgrp()}")
        expect = "expected 4 descriptors"
        try:
            fs._round_trip({"op": sup.FS_OP_FORK})
        except sup.ForkServerError as exc:
            check("fork server: a fork without its four descriptors is refused",
                  expect in str(exc), f"said {exc}")
        else:
            check("fork server: a fork without its four descriptors is refused", False, "accepted")
        try:
            fs._round_trip({"op": "nonsense"})
        except sup.ForkServerError as exc:
            check("fork server: an unknown op is refused", "unknown op" in str(exc), str(exc))
        else:
            check("fork server: an unknown op is refused", False, "accepted")
    finally:
        fs.close()
    check("fork server: close() reaps it", fs.pid is None)

    # -- A FAILED ROUND TRIP LOSES MESSAGE ALIGNMENT PERMANENTLY. SOCK_SEQPACKET cannot lose
    # framing, but a send that succeeded followed by a receive that did not leaves the peer's
    # reply queued: MEASURED on the unfixed tree, after an FS_OP_WAIT timed out at 0.5s the next
    # FS_OP_REAP returned that WAIT's {'ok': True}. The ordering that matters is a fork whose
    # reply is lost — the child WAS forked, so the next execution adopts a stale pid and
    # watchdogs, killpgs and reaps the PREVIOUS user's child. The socket must fail closed.
    fs = sup.ForkServer.start()
    try:
        real_recv = sup._fs_recv

        def _timeout(*_a, **_k):
            raise socket.timeout("timed out")

        sup._fs_recv = _timeout
        try:
            fs._round_trip({"op": sup.FS_OP_REAP, "pid": os.getpid(), "nohang": True})
        except sup.ForkServerError:
            check("fork server: a round trip whose reply is lost raises", True)
        else:
            check("fork server: a round trip whose reply is lost raises", False, "it returned")
        finally:
            sup._fs_recv = real_recv
        # The fork server answered that message and the answer is sitting in the socket. A
        # handle that carried on would hand it back as the reply to this next, unrelated call.
        try:
            reply = fs._round_trip({"op": "nonsense"})
        except sup.ForkServerError as exc:
            check("fork server: after a failed round trip the control socket is poisoned and "
                  "every later call refuses rather than reading the previous reply",
                  "unusable" in str(exc), f"said {exc}")
        else:
            check("fork server: after a failed round trip the control socket is poisoned and "
                  "every later call refuses rather than reading the previous reply",
                  False, f"it answered {reply}")
        check("fork server: a poisoned control socket is not alive()", not fs.alive())
        # BLOCKING 2's other half: a supervisor holding a poisoned or dead fork server must
        # report it, because sandbox.yaml has only a readinessProbe and 200 ok would leave a
        # permanently broken pod in the Service endpoints forever.
        sick = sup.Supervisor(os.path.join(tmp, "sick-health"), ready=True)
        sick.forkserver = fs
        code, payload = sick.health()
        check("health: a supervisor whose fork server is unusable answers 503 forkserver-down, "
              "so the readiness probe pulls the pod out of endpoints",
              code == 503 and payload["status"] == "forkserver-down", f"got {code} {payload}")
    finally:
        fs.close()


def test_forkserver_lost_fork_reply(tmp):
    """A fork whose {"pid": n} reply never arrives must not leave the child running.

    THIS IS THE HOLE 4h6.55 OPENED AND THE ONLY ONE POISONING DOES NOT CLOSE. The fork server
    forked the child and answered; the supervisor's round trip failed before reading it, so
    job.pid stays None and neither _execute_inner's finally nor the watchdog has a pid to kill
    — the supervisor cannot name the process at all. Poisoning stops that pid being
    MISATTRIBUTED to the next execution, which was the dangerous half, but the child itself
    keeps running user code at uid 65532 with write access to /scratch for the pod's lifetime.
    Before the fork server the supervisor forked directly and always knew the pid, so nothing
    older covers this. The fork server tracks what it forked and kills it when the control
    channel ends; the test steals the pid the supervisor never sees and watches it die.
    """
    marker = os.path.join(tmp, "lost-reply.started")
    code = f"import os, time\nopen({marker!r}, 'w').write(str(os.getpid()))\ntime.sleep(300)\n"
    fs = sup.ForkServer.start()
    seen = {}
    fds = []
    try:
        payload_fd = sup._payload_fd({"code": code, "env": {}, "cwd": tmp}, tmp)
        out_r, out_w = os.pipe()
        status_r, status_w = os.pipe()
        audit_r, audit_w = os.pipe()
        fds = [payload_fd, out_r, out_w, status_r, status_w, audit_r, audit_w]
        real_recv = sup._fs_recv

        def _lose_the_reply(sock, maxfds=0):
            reply, extra = real_recv(sock, maxfds)
            seen.update(reply or {})
            # Do not raise until the child is demonstrably running the user's code, so this is
            # the real ordering and not a race the fix could win by accident.
            deadline = time.monotonic() + 30
            while not os.path.exists(marker) and time.monotonic() < deadline:
                time.sleep(0.02)
            raise socket.timeout("timed out")

        sup._fs_recv = _lose_the_reply
        try:
            fs.fork_child(payload_fd, out_w, status_w, audit_w)
        except sup.ForkServerError:
            pass
        finally:
            sup._fs_recv = real_recv

        child = seen.get("pid")
        check("lost fork reply: the fork server forked a child and the supervisor did not "
              "learn its pid", isinstance(child, int) and os.path.exists(marker),
              f"reply {seen}, marker {os.path.exists(marker)}")
        if not isinstance(child, int):
            return
        check("lost fork reply: the control socket is poisoned, so the stale pid cannot be "
              "misattributed to the next execution", not fs.alive())

        # _poison already closed the supervisor's end, so the fork server is at EOF and the
        # kill is under way; fs.close() below only reaps the fork server itself.
        gone = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except OSError:
                gone = True
                break
            try:
                with open(f"/proc/{child}/stat", "rb") as fh:
                    raw = fh.read()
                if raw[raw.rfind(b")") + 2: raw.rfind(b")") + 3] == b"Z":
                    gone = True  # reparented zombie: not running user code
                    break
            except OSError:
                gone = True
                break
            time.sleep(0.05)
        check("lost fork reply: the fork server kills the child it forked, so a lost reply "
              "leaves nothing running", gone, f"pid {child} is still running")
        if not gone:
            try:
                os.killpg(os.getpgid(child), signal.SIGKILL)
            except OSError:
                try:
                    os.kill(child, signal.SIGKILL)
                except OSError:
                    pass
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        fs.close()


def test_forkserver_death_mid_execution(tmp):
    """The fork server dying under a running execution must still kill that execution's group.

    _reap raises ForkServerError when the control socket dies, and _execute_inner's finally set
    job.done BEFORE anything killed anything — so _watchdog returned on its first statement
    without firing a limit, and neither _execute nor run kills on its error path. The request
    500s and frees the slot while the user's code runs on for the pod's lifetime, holding CPU,
    memory and same-uid write access to /scratch while later users execute. _kill_group signals
    with os.killpg directly and never through the control socket, so it works here.
    """
    root = os.path.join(tmp, "forkserver-death")
    os.makedirs(root)
    server = Server(root)
    try:
        body = make_body(code="import time\ntime.sleep(120)\n")
        body["timeout_s"] = 120
        box = {}
        t = threading.Thread(
            target=lambda: box.update(zip(("status", "retry", "body"),
                                          server.request("POST", "/execute", body))),
            daemon=True)
        t.start()

        child = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            running = server.supervisor._running
            if running is not None and running.pid is not None:
                child = running.pid
                break
            time.sleep(0.02)
        check("forkserver death: the victim execution reached its fork", child is not None)
        if child is None:
            return
        # Its own session, so killpg on the supervisor's group is not what cleans this up.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and os.getpgid(child) == os.getpgrp():
            time.sleep(0.02)

        os.kill(server.supervisor.forkserver.pid, signal.SIGKILL)
        t.join(90)
        check("forkserver death: the execution is answered 500 rather than hanging",
              box.get("status") == 500, f"got {box.get('status')} {str(box.get('body'))[:200]}")

        gone = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except OSError:
                gone = True
                break
            # A reparented zombie is not still running; /proc says so.
            try:
                with open(f"/proc/{child}/stat", "rb") as fh:
                    raw = fh.read()
                if raw[raw.rfind(b")") + 2: raw.rfind(b")") + 3] == b"Z":
                    gone = True
                    break
            except OSError:
                gone = True
                break
            time.sleep(0.05)
        check("forkserver death: the orphaned execution child is killed, not left running for "
              "the pod's lifetime", gone, f"pid {child} is still running")
        if not gone:
            try:
                os.kill(child, signal.SIGKILL)
            except OSError:
                pass

        status, _, health = server.request("GET", "/health")
        check("forkserver death: /health stops saying ok, so the readiness probe replaces "
              "the pod instead of leaving it dead in the endpoints",
              status == 503 and (health or {}).get("status") == "forkserver-down",
              f"got {status} {health}")
    finally:
        server.close()


def _proc_state(pid):
    """The state letter for `pid`, or None when the pid is gone.

    A ZOMBIE IS NOT A SURVIVOR, and os.kill(pid, 0) cannot tell the two apart — it succeeds
    for both. That is the entire reason this reads /proc instead: "we contained it" and "it is
    still running the attacker's code" must not be the same observation.
    """
    fields = sup._proc_stat_fields(pid)
    if not fields:
        return None
    return fields[0].decode("ascii", "replace")


_SURVIVOR_SLEEP_S = 30


def _survivor_code(detach):
    """A script that leaves ONE process behind and prints its pid, then exits normally.

    IT HOLDS NONE OF THE SUPERVISOR'S DESCRIPTORS, deliberately. 0/1/2 go to /dev/null and the
    status and audit pipes are closed, so all three drains reach EOF when the direct child
    exits. Without that, an execution's response would be held for DRAIN_GRACE_S and the test
    would be measuring the drain deadline (4h6.39) rather than containment. It is the shape
    4h6.55's finding-3 probe used, for the same reason.

    `detach` is the only difference between the two cases the beads name: with setsid() the
    survivor leaves the process group and no killpg can reach it (4h6.83); without it, it
    stays in the group and a normally-completing execution simply never signalled that group
    (4h6.66). Both were MEASURED surviving.
    """
    return (
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        + ("    os.setsid()\n" if detach else "")
        + "    for fd in (3, 4):\n"
        "        try:\n"
        "            os.close(fd)\n"
        "        except OSError:\n"
        "            pass\n"
        "    null = os.open('/dev/null', os.O_RDWR)\n"
        "    for fd in (0, 1, 2):\n"
        "        os.dup2(null, fd)\n"
        f"    time.sleep({_SURVIVOR_SLEEP_S})\n"
        "    os._exit(0)\n"
        "print('SURVIVOR', pid, flush=True)\n"
    )


def _run_survivor_probe(server, detach):
    """(pid of the process left behind, response body). pid is None if the probe misfired."""
    status, _, body = server.request(
        "POST", "/execute", body=make_body(code=_survivor_code(detach)))
    body = body or {}
    if status != 200:
        return None, body
    for line in (body.get("output") or "").splitlines():
        if line.startswith("SURVIVOR "):
            return int(line.split()[1]), body
    return None, body


def test_survivors(server):
    """A depth-1 survivor does not outlive its own execution: 4h6.66 and 4h6.83, watched.

    MEASURED BEFORE THIS LANDED, after a status-ok execution — both of these were still
    running, and would have been for the pod's lifetime:
        1197089 1197084 PRB-INGRP    <- plain fork, stayed in the process group
        1197090 1197090 PRB-DETACH   <- setsid() escapee
    The first is 4h6.66: _kill_group has two call sites and a normal completion has
    reaped=True, so it reached neither. The second is 4h6.83 and is not reachable by any
    process-group kill at all; what reaches it is PARENTAGE — the fork server is a
    PR_SET_CHILD_SUBREAPER, so an escapee whose parent exits reparents to it and FS_OP_SWEEP
    kills and reaps it.

    THE NEGATIVE CONTROL IS THE SECOND HALF OF THIS TEST AND IT IS NOT DECORATION. A probe
    whose fork silently failed, or whose survivor exited on its own, would make the positive
    assertions pass over nothing. So the same two probes are run again with _kill_survivors and
    _sweep_strays disabled, and the survivors must then BE THERE — restoring the exact
    behaviour the beads measured — before an explicit sweep clears them.

    WHAT IT DOES NOT PROVE: that the survivor was harmless while its OWN execution ran (it was
    alive for all of it, by construction), and anything at all about gVisor, which implements
    prctl and /proc in the sentry and is 4h6.51's to measure.
    """
    if server.container or server.supervisor is None:
        skip("survivors: a depth-1 survivor does not outlive its own execution",
             "the probe reads /proc for a pid in the supervisor's own namespace")
        return
    try:
        for detach, label in ((True, "a setsid() escapee"), (False, "an in-group grandchild")):
            pid, body = _run_survivor_probe(server, detach)
            check(f"survivors: the {label} probe ran and named the process it left behind",
                  pid is not None, f"got {body}")
            if pid is None:
                continue
            check(f"survivors: the execution that left {label} still answers ok",
                  body.get("status") == "ok", f"got {body.get('status')} {body.get('error')}")
            state = _proc_state(pid)
            check(f"survivors: {label} does not survive a normally-completing execution",
                  state is None, f"pid {pid} is still there in state {state!r}")

        leftovers = []
        real_kill = sup._kill_survivors
        real_sweep = sup.Supervisor._sweep_strays
        sup._kill_survivors = lambda job: False
        sup.Supervisor._sweep_strays = lambda self, job: None
        try:
            for detach, label in ((True, "a setsid() escapee"), (False, "an in-group grandchild")):
                pid, body = _run_survivor_probe(server, detach)
                state = _proc_state(pid) if pid is not None else None
                check(f"survivors: NEGATIVE CONTROL — with the group kill and the sweep "
                      f"disabled, {label} IS still running after the execution completes",
                      state is not None and state != "Z",
                      f"pid {pid} state {state!r} (the probe proves nothing if this passes "
                      f"only because the fix ran)")
                if state is not None:
                    leftovers.append(pid)
        finally:
            sup._kill_survivors = real_kill
            sup.Supervisor._sweep_strays = real_sweep

        # (killed, reaped-as-zombies): a zombie is not a survivor and the two are reported
        # separately, so this assertion must read the killed half.
        swept, _reaped = server.supervisor.forkserver.sweep()
        check("survivors: an explicit sweep kills and reaps exactly what the negative control "
              "left behind, so the sweep is what does the work",
              leftovers and set(leftovers) <= set(swept)
              and all(_proc_state(p) is None for p in leftovers),
              f"left {leftovers}, swept {swept}")
    finally:
        server.close()


def _chain_code():
    """A script that leaves a DEPTH-2 chain of setsid()'d processes behind, and names both.

    THE MIDDLE PROCESS MUST STAY ALIVE UNTIL THE SWEEP RUNS, which is the entire difference
    from `_survivor_code` and the reason a single-pass sweep missed this shape: B reparents to
    the fork server only when A exits, so while A is alive B is not the fork server's child and
    no enumeration can see it. A is killed by the sweep's first round; B appears afterwards.
    Both call setsid(), so B is not in A's process group either and the collateral killpg that
    catches shallower probes does not reach it.
    """
    return (
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    b = os.fork()\n"
        "    if b == 0:\n"
        "        os.setsid()\n"
        "        for fd in (3, 4):\n"
        "            try:\n"
        "                os.close(fd)\n"
        "            except OSError:\n"
        "                pass\n"
        "        null = os.open('/dev/null', os.O_RDWR)\n"
        "        for fd in (0, 1, 2):\n"
        "            os.dup2(null, fd)\n"
        f"        time.sleep({_SURVIVOR_SLEEP_S})\n"
        "        os._exit(0)\n"
        "    print('CHAIN', pid, b, flush=True)\n"
        "    for fd in (3, 4):\n"
        "        try:\n"
        "            os.close(fd)\n"
        "        except OSError:\n"
        "            pass\n"
        "    null = os.open('/dev/null', os.O_RDWR)\n"
        "    for fd in (0, 1, 2):\n"
        "        os.dup2(null, fd)\n"
        f"    time.sleep({_SURVIVOR_SLEEP_S})\n"
        "    os._exit(0)\n"
        # The direct child exits here, which is what starts A's reparenting. The pause only
        # keeps the print ahead of the exit so the harness reads the pids from a live pipe.
        "time.sleep(0.3)\n"
    )


def _run_chain_probe(server):
    """((pid of A, pid of B), body). The pids are None if the probe misfired.

    A prints the pair, not the direct child: the pid the parent gets from fork() is A's, and B's
    is only knowable inside A.
    """
    status, _, body = server.request(
        "POST", "/execute", body=make_body(code=_chain_code()))
    body = body or {}
    if status != 200:
        return (None, None), body
    for line in (body.get("output") or "").splitlines():
        if line.startswith("CHAIN "):
            _, a, b = line.split()
            return (int(a), int(b)), body
    return (None, None), body


def test_survivor_chain(root):
    """A DEPTH-2 setsid() chain does not survive its own execution either (4h6.83).

    MEASURED against the single-pass sweep this replaced — B was in state S, running, for the
    whole of the NEXT execution:
        PROBE4 double-setsid status=ok pids={'A': 1213980, 'B': 1213981} A=None B='S'
        PROBE4 after ONE more execution: B=None
    `test_survivors` cannot catch it: both of its probes are depth 1, which is exactly the shape
    a single enumeration sees. The mechanism is that a process reparents to the subreaper only
    when ITS OWN parent exits, so B is invisible while A lives, becomes the fork server's child
    after the sweep has killed A, and then needs a SECOND enumeration.

    THE NEGATIVE CONTROL IS A SECOND SUPERVISOR WITH FS_SWEEP_MAX_ROUNDS AT 1, and it has to be
    a second supervisor because the fork server is forked at bring_up(): patching the constant
    afterwards would change the harness's copy and not the one the sweep actually reads. With
    one round B must BE there afterwards — restoring the measurement above — or the positive
    assertion is passing over a probe that never worked.
    """
    servers = []
    try:
        real_rounds = sup.FS_SWEEP_MAX_ROUNDS
        sup.FS_SWEEP_MAX_ROUNDS = 1
        try:
            neg = Server(os.path.join(root, "one-round"))
        finally:
            sup.FS_SWEEP_MAX_ROUNDS = real_rounds
        servers.append(neg)
        if neg.supervisor is None:
            skip("survivor chain: a depth-2 setsid() chain does not outlive its execution",
                 "the probe reads /proc for a pid in the supervisor's own namespace")
            return
        (a, b), body = _run_chain_probe(neg)
        check("survivor chain: the depth-2 probe ran and named both processes it left behind",
              a is not None and b is not None, f"got {body}")
        state_b = _proc_state(b) if b is not None else None
        check("survivor chain: NEGATIVE CONTROL — with the sweep's re-enumeration disabled "
              "(one round) the grandchild of the chain IS still running afterwards",
              state_b is not None and state_b != "Z",
              f"A={_proc_state(a)!r} B={state_b!r} (the positive assertion below proves "
              f"nothing if this passes only because the fix ran)")
        swept, _reaped = neg.supervisor.forkserver.sweep()
        check("survivor chain: and one more sweep round, now that it has reparented, clears it",
              b is None or _proc_state(b) is None, f"swept {swept}, B={_proc_state(b)!r}")

        pos = Server(os.path.join(root, "all-rounds"))
        servers.append(pos)
        (a, b), body = _run_chain_probe(pos)
        check("survivor chain: the depth-2 probe ran under the real sweep too",
              a is not None and b is not None, f"got {body}")
        check("survivor chain: the execution that left the chain still answers ok",
              body.get("status") == "ok", f"got {body.get('status')} {body.get('error')}")
        states = (_proc_state(a) if a else None, _proc_state(b) if b else None)
        check("survivor chain: NEITHER process of a depth-2 setsid() chain survives a "
              "normally-completing execution",
              states == (None, None), f"A={states[0]!r} B={states[1]!r}")
    finally:
        for server in servers:
            server.close()


def test_pre_ready_execute(tmp):
    """A POST /execute arriving before the supervisor is ready must be refused BEFORE its body
    is read.

    main() binds and serves before bring_up() on purpose, so `status: "starting"` is observable
    — which means requests DO arrive during the multi-second prewarm(), and ForkServer.start()
    snapshots the address space at the end of it. The readiness check used to sit in _admit,
    after _read_body and parse_execute_request had already made both JWTs and the user's source
    into Python strings: an early request answered 503 was still recovered from a later
    execution child by the /proc/self/mem route. A 503 does not take the bytes back out of the
    arenas, so the refusal has to happen before they exist.
    """
    root = os.path.join(tmp, "pre-ready")
    os.makedirs(root)
    saved = sup.SUPERVISOR
    real_read_body = sup._Handler._read_body
    supervisor = sup.create(scratch_root=root)  # bound, NOT ready: nothing is forked yet
    httpd = sup._Server(("127.0.0.1", 0), sup._Handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    reads = []
    sup._Handler._read_body = (
        lambda self, started, _real=real_read_body: reads.append(1) or _real(self, started))
    try:
        payload = json.dumps(make_body(code="x = 'PREREADYNEEDLE'\n")).encode()
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=30)
        conn.putrequest("POST", "/execute")
        conn.putheader("Content-Type", "application/json")
        # DELIBERATELY MORE THAN IS SENT. This is the mechanism-independent half: a supervisor
        # that reads the body blocks here until BODY_READ_TIMEOUT_S and answers 408, so an
        # immediate 503 is proof the bytes were never taken in.
        conn.putheader("Content-Length", str(len(payload) + 65536))
        conn.endheaders()
        conn.send(payload)
        started = time.monotonic()
        resp = conn.getresponse()
        raw = resp.read()
        elapsed = time.monotonic() - started
        status = resp.status
        conn.close()
        parsed = json.loads(raw.decode()) if raw else {}
        check("pre-ready: POST /execute during bring_up() is refused 503 NotReady",
              status == 503 and parsed.get("error", {}).get("type") == "NotReady",
              f"got {status} {parsed}")
        check("pre-ready: it is refused BEFORE the body is read, so no token and no source "
              "code ever enters the process the fork server is snapshotted from",
              not reads, f"_read_body ran {len(reads)} time(s)")
        check("pre-ready: the refusal does not wait on the unsent body",
              elapsed < sup.BODY_READ_TIMEOUT_S / 2, f"took {elapsed:.1f}s")

        # The draining half of the same gate. Nothing is ready here, so set both states.
        supervisor.ready = True
        supervisor.begin_drain()
        reads.clear()
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=30)
        conn.putrequest("POST", "/execute")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(payload) + 65536))
        conn.endheaders()
        conn.send(payload)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
        conn.close()
        check("pre-ready: a draining supervisor refuses the same way, before the body",
              status == 503 and not reads, f"got {status}, _read_body ran {len(reads)} time(s)")
    finally:
        sup._Handler._read_body = real_read_body
        httpd.shutdown()
        httpd.server_close()
        sup.SUPERVISOR = saved


ENV_BUFFERED_RFILE = "SUPERVISOR_TEST_BUFFERED_RFILE"

# THE SENDER HAS TO BE ANOTHER PROCESS, and this is not fastidiousness — it was measured. The
# harness process IS the supervisor process here, so a body built with make_body() is in the
# heap that ForkServer.start() snapshots no matter what the socket read buffer does: the first
# version of test_pre_ready_body_bytes recovered all three needles WITH the fix in place, for
# that reason alone. The needles are therefore minted here, written to a file the parent reads
# only AFTER the fork, and never exist in the parent before it.
_EARLY_SENDER = r'''
import base64, json, os, socket, sys, uuid

port, auds, out = int(sys.argv[1]), json.loads(sys.argv[2]), sys.argv[3]

def b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode("ascii").rstrip("=")

v = os.urandom(12).hex().upper()
eid, user, sid = str(uuid.uuid4()), "early-%s@b.c" % v[:8], "sess-%s" % v
tokens = {aud: "%s.%s.signature" % (b64({"alg": "HS256"}),
                                    b64({"aud": aud, "jti": eid, "sub": user, "sid": sid}))
          for aud in auds}
body = {"code": "x = 'EARLYCODE%s'\nprint('early')\n" % v, "execution_id": eid,
        "tokens": tokens, "user": user, "session_id": sid}
payload = json.dumps(body).encode()
with open(out, "w") as fh:
    json.dump({"early-token": tokens[auds[0]].split(".")[1],
               "early-code": "EARLYCODE" + v, "early-session": sid}, fh)
head = (b"POST /execute HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n")
sock = socket.create_connection(("127.0.0.1", port), timeout=30)
sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, True)
sock.sendall(head + payload)   # ONE sendall: the head and the body share a segment
answer = sock.recv(4096)
sock.close()
sys.exit(0 if b" 503 " in answer else 3)
'''


def _buffered_setup(self):
    """_Handler.setup as it was BEFORE 4h6.87: an 8 KiB BufferedReader as rfile.

    This is the negative control for test_pre_ready_body_bytes, and it is a whole restoration
    of the defect rather than a flag the fixed code reads: with it installed the header parse
    recv()s 8 KiB and the body rides in with the headers again.

    IT ASSERTS AT LEAST ONE NEEDLE RECOVERS, ARENA-DEPENDENT WHICH: measured 2 of 3, with
    `early-code` the one that comes back only sometimes. A future run seeing 2 red and 1 green
    is the control working, not a flake.
    """
    socketserver.StreamRequestHandler.setup(self)
    self.rfile = self.connection.makefile("rb", -1)


def test_pre_ready_body_bytes(tmp):
    """A body that shares a TCP segment with its headers must not be in the supervisor when the
    fork server is forked (genetics-results-suite-4h6.87).

    THE ORDERING FIX IS NOT ENOUGH ON ITS OWN, which is why this is a second test and not an
    assertion inside test_pre_ready_execute. _execute refuses before _read_body, so no Python
    string is built — but socketserver's default rfile is an 8 KiB BufferedReader, so
    BaseHTTPRequestHandler's request-line and header parse had ALREADY recv()d the body
    underneath the handler. MEASURED before the fix, with the fork gated to land milliseconds
    after the 503: one segment -> the token, the source and the session id all recovered from
    the child; two segments -> nothing. Run this with %s=1 to put that buffer back and watch
    the three checks below go red.

    THE FORK IS GATED DELIBERATELY. ForkServer.start() runs on the next line after the refusal.
    Under a realistic multi-second prewarm the same probe recovers nothing, but that is arena
    REUSE and nothing enforces it; a null result there would prove nothing about the property.
    """ % ENV_BUFFERED_RFILE
    root = os.path.join(tmp, "pre-ready-bytes")
    os.makedirs(root)
    saved_env = os.environ.get(sup.ENV_SCRATCH_ROOT)
    os.environ[sup.ENV_SCRATCH_ROOT] = root
    saved = sup.SUPERVISOR
    real_setup = sup._Handler.setup
    if os.environ.get(ENV_BUFFERED_RFILE) == "1":
        print(f"  !! {ENV_BUFFERED_RFILE}=1: the negative control is installed, "
              f"the checks below MUST fail")
        sup._Handler.setup = _buffered_setup

    # The same positive control test_isolation uses, planted before the fork server exists: if
    # this one is not recovered the search is dead and the absences below mean nothing.
    control = "FORKSRVCTL" + os.urandom(16).hex().upper()
    sup.ISOLATION_TEST_CONTROL = control

    supervisor = sup.create(scratch_root=root, retention_s=60)  # bound, NOT ready, NOT forked
    httpd = sup._Server(("127.0.0.1", 0), sup._Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    try:
        needle_file = os.path.join(root, "needles.json")
        sent = subprocess.run(
            [sys.executable, "-c", _EARLY_SENDER, str(port),
             json.dumps(list(sup.TOKEN_AUDIENCES)), needle_file], timeout=120)
        check("pre-ready bytes: a one-segment POST /execute during bring_up is refused 503",
              sent.returncode == 0, f"sender exited {sent.returncode}")

        # MILLISECONDS after the refusal, which is what makes the leak observable at all.
        supervisor.forkserver = sup.ForkServer.start()
        supervisor.ready = True

        # AFTER the fork, deliberately: see _EARLY_SENDER. Anything read before this line is in
        # the snapshot the probe searches, and would make every arm of the A/B look identical.
        with open(needle_file, encoding="utf-8") as fh:
            early = json.load(fh)
        pairs = [["control-prefork", *_pair(control)]]
        pairs += [[label, *_pair(early[label])] for label in
                  ("early-token", "early-code", "early-session")]
        probe_code = (_ISOLATION_PROBE
                      .replace("__PAIRS__", json.dumps(pairs))
                      .replace("__SLEEP_S__", "0.0"))
        probe = make_body(code=probe_code, user="probe@b.c", session_id="sess-probe")
        probe["timeout_s"] = 120
        raw = json.dumps(probe).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
        conn.request("POST", "/execute", body=raw,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(raw))})
        resp = conn.getresponse()
        answered = json.loads(resp.read().decode())
        conn.close()

        line = ""
        for candidate in (answered.get("output") or "").splitlines():
            if candidate.startswith("PROBERESULT "):
                line = candidate[len("PROBERESULT "):]
        try:
            result = json.loads(line)["released"]
        except Exception:
            result = None
        check("pre-ready bytes: the probe executed and reported",
              isinstance(result, dict), f"got {str(answered)[:300]}")
        if not isinstance(result, dict):
            return
        found = set(result["found"])
        check("pre-ready bytes: the positive control IS recovered, so the search works",
              "control-prefork" in found and result["mem"] == "ok",
              f"mem said {result['mem']}, found {sorted(found)}")
        # THREE INDEPENDENT, UNCONDITIONAL CHECKS. Not one check over a union and not an `or`:
        # this group has already shipped a test that passed because one assertion carried
        # another (see the isolation group's note on `found`).
        for label, what in (("early-token", "token"), ("early-code", "source code"),
                            ("early-session", "session id")):
            check(f"pre-ready bytes: the refused request's {what} is NOT in the child, so the "
                  f"socket read buffer no longer carries it into the fork",
                  label not in found, f"RECOVERED {label}; found {sorted(found)}")
    finally:
        sup._Handler.setup = real_setup
        httpd.shutdown()
        httpd.server_close()
        if supervisor.forkserver is not None:
            supervisor.forkserver.close()
        sup.SUPERVISOR = saved
        if saved_env is None:
            os.environ.pop(sup.ENV_SCRATCH_ROOT, None)
        else:
            os.environ[sup.ENV_SCRATCH_ROOT] = saved_env


ENV_DROP_LF_CRLF = "SUPERVISOR_TEST_DROP_LF_CRLF"
ENV_STATIC_SEAM = "SUPERVISOR_TEST_STATIC_SEAM"


def _static_seam(self, tail_len, take):
    """_read_head's seam update as it was BEFORE the rolling window: recomputed from THIS
    round only.

    The negative control for the drip checks below, and a whole restoration of the defect
    rather than a flag the fixed code reads. With it installed a peek that returns fewer than
    _HEADER_TAIL_BYTES bytes discards what earlier rounds saw, so a blank line straddling it is
    never found: the read consumes the WHOLE body off the kernel queue and blocks in recv_into
    forever. MEASURED, 4 terminator shapes x 7 chunk sizes: 26/28 with this installed, 28/28
    without, and the two failures are exactly the take == 1 cells whose blank line is \\r\\n.
    """
    tail = min(take, sup._HEADER_TAIL_BYTES)
    self._edge[:tail] = self._view[take - tail:take]
    return tail


def _drip(sock, blob, chunk, delay):
    """Feed `blob` `chunk` bytes at a time on a thread, returned so the caller can join it.

    THE JOIN MATTERS: reader.read() is a single recv by design, so a leftover assertion made
    while the feeder is still writing sees a short read and passes or fails for the wrong
    reason. Drain to the expected length after the feeder is done.
    """
    def go():
        try:
            for i in range(0, len(blob), chunk):
                sock.sendall(blob[i:i + chunk])
                time.sleep(delay)
        except OSError:
            pass  # the reader hung (the control is installed) and the socket was closed
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def _drain(reader, n):
    out = b""
    while len(out) < n:
        block = reader.read(n - len(out))
        if not block:
            break
        out += block
    return out


def _read_head_watched(reader, timeout):
    """(result, heap_bytes_seen, still_running) for one _read_head call on its own thread.

    THIS IS A MECHANICS TEST AND MAKES NO LEAK CLAIM — a leak claim has to send from another
    process (see _EARLY_SENDER), because this process is the supervisor. What it does assert is
    narrower and checkable here: which objects _read_head puts a byte into. `heap_bytes_seen`
    is every `bytes` value that appeared as a local of the _read_head frame, so a body byte
    showing up in one means a heap copy was built where a fixed, wiped buffer was required.
    """
    seen, box = [], []

    def tracer(frame, event, arg):
        if frame.f_code.co_name != "_read_head":
            return None
        for value in frame.f_locals.values():
            if type(value) is bytes:                                     # noqa: E721
                seen.append(value)
            elif type(value) is list:                                    # noqa: E721
                seen.extend(item for item in value if type(item) is bytes)  # noqa: E721
        return tracer

    def go():
        sys.settrace(tracer)
        try:
            box.append(reader._read_head())
        except BaseException as exc:                                     # noqa: BLE001
            box.append(exc)
        finally:
            sys.settrace(None)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    return (box[0] if box else None), seen, t.is_alive()


def test_header_reader_units():
    """_HeaderBoundedReader's edges, which the wire tests cannot reach on purpose: a head split
    across segments, a head dripped ONE BYTE AT A TIME, a head that never terminates, a head
    whose blank line is "\\n\\r\\n", and both fixed buffers left clean."""
    pair = socket.socketpair
    saved_terminators = sup._HEADER_TERMINATORS
    saved_roll = sup._HeaderBoundedReader._roll_tail
    if os.environ.get(ENV_DROP_LF_CRLF) == "1":
        print(f"  !! {ENV_DROP_LF_CRLF}=1: the negative control is installed, "
              f"the \\n\\r\\n checks below MUST fail")
        sup._HEADER_TERMINATORS = tuple(t for t in sup._HEADER_TERMINATORS if t != b"\n\r\n")
    if os.environ.get(ENV_STATIC_SEAM) == "1":
        print(f"  !! {ENV_STATIC_SEAM}=1: the negative control is installed, "
              f"the drip checks below MUST fail")
        sup._HeaderBoundedReader._roll_tail = _static_seam
    try:
        _header_reader_cases(pair)
    finally:
        sup._HEADER_TERMINATORS = saved_terminators
        sup._HeaderBoundedReader._roll_tail = saved_roll


def _header_reader_cases(pair):

    # 1. head and body in one write: the head comes back exactly, the body stays in the kernel.
    a, b = pair()
    reader = sup._HeaderBoundedReader(b, None)
    a.sendall(b"POST /x HTTP/1.1\r\nHost: h\r\nContent-Length: 5\r\n\r\nBODY!")
    line = reader.readline(65537)
    check("header reader: the request line is returned",
          line == b"POST /x HTTP/1.1\r\n", f"got {line!r}")
    rest = b"".join(iter(lambda: reader.readline(65537), b"\r\n"))
    check("header reader: the headers are returned and stop at the blank line",
          rest == b"Host: h\r\nContent-Length: 5\r\n", f"got {rest!r}")
    check("header reader: the body is still readable afterwards",
          reader.read(5) == b"BODY!")
    check("header reader: the peek scratch is zeroed, so no body byte is left in it",
          reader._scratch == bytearray(sup.HEADER_PEEK_BYTES))
    a.close(); b.close()

    # 2. a head split across three segments, with the terminator itself bisected — and a body
    # riding in the last segment, so the seam search sees body bytes and must not copy them.
    a, b = pair()
    reader = sup._HeaderBoundedReader(b, None)
    whole = b"GET /y HTTP/1.1\r\nA: " + b"z" * 900 + b"\r\n\r\n"
    def _feed():
        a.sendall(whole[:10]); time.sleep(0.05)
        a.sendall(whole[10:len(whole) - 2]); time.sleep(0.05)
        a.sendall(whole[len(whole) - 2:] + b"\x01\x02BODYBODY")
    threading.Thread(target=_feed, daemon=True).start()
    head, heap, alive = _read_head_watched(reader, 10)
    check("header reader: a head split across segments, terminator bisected, is reassembled",
          not alive and head == whole, f"got {head!r} (still running: {alive})")
    # The seam window straddles the boundary, so its trailing bytes ARE body bytes. Building it
    # as `tail + bytes(view[:n])` put them in a heap `bytes` that is freed into an arena the
    # fork server would snapshot; it is a second fixed bytearray for exactly this reason.
    # THE NEEDLE IS ONE BYTE ON PURPOSE — the pre-fix copy carried only 1 or 2 body bytes here,
    # so a word-sized needle would have made this assertion pass against the defect it exists
    # to catch. \x01 and \x02 cannot occur in the head, and the terminators do not contain them.
    escaped = [chunk for chunk in heap if b"\x01" in chunk or b"\x02" in chunk]
    check("header reader: the bisected-terminator path copied NO body byte onto the heap",
          not escaped, f"heap bytes carrying body: {escaped!r}")
    check("header reader: both fixed buffers are zeroed after the seam read",
          reader._scratch == bytearray(sup.HEADER_PEEK_BYTES)
          and reader._edge == bytearray(sup._HEADER_EDGE_BYTES),
          f"scratch clean: {reader._scratch == bytearray(sup.HEADER_PEEK_BYTES)}, "
          f"edge: {bytes(reader._edge)!r}")
    a.close(); b.close()

    # 2b. THE BLANK LINE IS "\n\r\n" — the fourth shape http.client.parse_headers stops on, and
    # the one a three-member terminator set misses. Not reachable from the deployed path, but
    # with it missing this was MEASURED to copy the WHOLE body into `parts` on the heap, leave
    # it in the un-wiped scratch (the finally never runs), and block in recv_into forever
    # instead of refusing. Run with SUPERVISOR_TEST_DROP_LF_CRLF=1 to drop it from the set and
    # watch these go red.
    a, b = pair()
    reader = sup._HeaderBoundedReader(b, None)
    lf_head = b"POST /execute HTTP/1.1\r\nHost: h\nContent-Length: 15\n\r\n"
    a.sendall(lf_head + b"SECRETBODYNEEDLE"[:15])
    head, heap, alive = _read_head_watched(reader, 5)
    check("header reader: a head whose blank line is \\n\\r\\n terminates and is consumed exactly",
          not alive and head == lf_head, f"got {head!r} (still blocked in recv_into: {alive})")
    check("header reader: that head's body is not copied onto the heap",
          not [chunk for chunk in heap if b"SECRETBODY" in chunk],
          f"heap bytes carrying body: {[c for c in heap if b'SECRETBODY' in c]!r}")
    check("header reader: that head's body is not left in the scratch buffer",
          reader._scratch == bytearray(sup.HEADER_PEEK_BYTES),
          f"scratch: {bytes(reader._scratch)[:80]!r}")
    if not alive:
        check("header reader: that head's body is still in the kernel queue afterwards",
              reader.read(15) == b"SECRETBODYNEEDL")
    a.close(); b.close()

    # 2c. THE SAME FAILURE REACHED THROUGH PEEK SIZE RATHER THAN TERMINATOR SHAPE: an ordinary,
    # entirely valid \r\n\r\n head delivered a byte at a time. The seam window used to be
    # recomputed from the current round, so a 1-byte peek shrank it to 1 byte and the blank line
    # straddling it was never found — take became the whole peek every round, THE WHOLE BODY was
    # consumed off the kernel queue into `parts`, the finally never ran, and the read blocked in
    # recv_into forever instead of refusing. total never approaches MAX_HEADER_BYTES on that
    # path, so _HeaderTooLarge never fires either: fail-OPEN and pre-auth. Run with
    # SUPERVISOR_TEST_STATIC_SEAM=1 to put the per-round window back and watch these go red.
    a, b = pair()
    reader = sup._HeaderBoundedReader(b, None)
    drip_head = b"POST /execute HTTP/1.1\r\nHost: h\r\nContent-Length: 20\r\n\r\n"
    # THE BODY OPENS WITH TWO ONE-BYTE NEEDLES, and that is what makes the heap check bite
    # here: at a byte a drip `parts` fills with 1-byte `bytes`, so a word-sized needle like
    # SECRETBODY can never appear in one and the check would pass against the defect it exists
    # to catch. \x01 and \x02 cannot occur in a head and are in no terminator.
    drip_body = b"\x01\x02SECRETBODYNEEDL" + b"AA"
    feeder = _drip(a, drip_head + drip_body, 1, 0.004)
    head, heap, alive = _read_head_watched(reader, 20)
    feeder.join(20)
    check("header reader: a head dripped one byte at a time terminates and is consumed exactly",
          not alive and head == drip_head,
          f"got {head!r} (still blocked in recv_into: {alive})")
    leaked = [c for c in heap if b"\x01" in c or b"\x02" in c or b"SECRETBODY" in c]
    check("header reader: the dripped head's body is not copied onto the heap",
          not leaked, f"{len(leaked)} heap bytes carrying body, e.g. {leaked[:6]!r}")
    check("header reader: the dripped head's body is still in the kernel queue afterwards",
          not alive and _drain(reader, len(drip_body)) == drip_body)
    check("header reader: both fixed buffers are zeroed after the dripped read",
          reader._scratch == bytearray(sup.HEADER_PEEK_BYTES)
          and reader._edge == bytearray(sup._HEADER_EDGE_BYTES),
          f"scratch clean: {reader._scratch == bytearray(sup.HEADER_PEEK_BYTES)}, "
          f"edge: {bytes(reader._edge)!r}")
    a.close(); b.close()

    # 2d. AND THE WHOLE MATRIX, so the take == 1 class is asserted rather than spot-checked:
    # every blank-line shape http.client.parse_headers stops on (the last header line ends
    # \r\n or \n, the blank line is \r\n or \n) crossed with chunk sizes that make a peek land
    # inside the terminator. MEASURED: 28/28 here, 26/28 with SUPERVISOR_TEST_STATIC_SEAM=1,
    # and the two that fail there are exactly the take == 1 cells whose blank line is \r\n —
    # take == 2 is rescued by the b"\n\r\n" member and \n\n survives on a 2-byte window, which
    # is why one-write clients never saw this and any peer that can connect can trigger it.
    for prev in (b"\r\n", b"\n"):
        for blank in (b"\r\n", b"\n"):
            bad = []
            for chunk in (1, 2, 3, 4, 5, 7, 512):
                m_head = (b"POST /x HTTP/1.1\r\nHost: h\r\nContent-Length: 20"
                          + prev + blank)
                m_body = b"M" * 20
                a, b = pair()
                reader = sup._HeaderBoundedReader(b, None)
                feeder = _drip(a, m_head + m_body, chunk, 0.004 if chunk <= 2 else 0.002)
                head, _, alive = _read_head_watched(reader, 20)
                feeder.join(20)
                left = b"" if alive else _drain(reader, len(m_body))
                if not (not alive and head == m_head and left == m_body
                        and reader._scratch == bytearray(sup.HEADER_PEEK_BYTES)
                        and reader._edge == bytearray(sup._HEADER_EDGE_BYTES)):
                    bad.append(f"chunk={chunk} hung={alive} head={head!r} left={left!r}")
                a.close(); b.close()
            check(f"header reader: a head ending {prev + blank!r} is consumed exactly and "
                  f"leaves its body in the kernel at every chunk size", not bad,
                  "; ".join(bad))

    # 3. a head that never terminates fails CLOSED at MAX_HEADER_BYTES.
    a, b = pair()
    reader = sup._HeaderBoundedReader(b, None)
    box = []
    def _over():
        try:
            reader.readline(65537)
        except sup._HeaderTooLarge:
            box.append("refused")
        except Exception as exc:                       # noqa: BLE001
            box.append(repr(exc))
    t = threading.Thread(target=_over, daemon=True)
    t.start()
    try:
        sent = 0
        while sent <= sup.MAX_HEADER_BYTES + sup.HEADER_PEEK_BYTES and t.is_alive():
            a.sendall(b"X: " + b"q" * 1021 + b"\r\n")
            sent += 1024
        t.join(10)
    finally:
        a.close(); b.close()
    check("header reader: a head that never terminates is refused, not buffered forever",
          box == ["refused"], f"got {box!r}")
    # The wipe is in a `finally`, so it has to survive the raise as well as the return — the
    # refusal path is the one where a caller has most reason to assume nothing was kept.
    check("header reader: both fixed buffers are zeroed on the _HeaderTooLarge path too",
          reader._scratch == bytearray(sup.HEADER_PEEK_BYTES)
          and reader._edge == bytearray(sup._HEADER_EDGE_BYTES),
          f"scratch clean: {reader._scratch == bytearray(sup.HEADER_PEEK_BYTES)}, "
          f"edge: {bytes(reader._edge)!r}")

    # 4. and over the wire that refusal has to be a real answer, not a traceback in the log:
    # _HeaderTooLarge escapes handle_one_request, which is a path socketserver would otherwise
    # turn into a dropped connection with no response.
    httpd = sup._Server(("127.0.0.1", 0), sup._Handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    try:
        wire = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=20)
        wire.sendall(b"GET /health HTTP/1.1\r\nHost: h\r\n")
        sent = 0
        try:
            while sent < sup.MAX_HEADER_BYTES + 4096:
                wire.sendall(b"X: " + b"q" * 1021 + b"\r\n")
                sent += 1024
        except OSError:
            pass  # the supervisor answered and closed while we were still writing
        wire.settimeout(20)
        answer = b""
        try:
            while True:
                block = wire.recv(4096)
                if not block:
                    break
                answer += block
        except OSError:
            pass
        wire.close()
        check("header reader: an over-long head is answered 431 in the uniform JSON shape and "
              "the connection is closed",
              answer.startswith(b"HTTP/1.1 431 ") and b'"PayloadTooLarge"' in answer,
              f"got {answer[:160]!r}")
    finally:
        httpd.shutdown()
        httpd.server_close()


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
        print("artifact manifest and retrieval")
        test_manifest(tmp)
        test_artifact_scoping(tmp)
        test_artifact_integrity(tmp)
        print("startup wipe")
        test_startup_wipe(tmp)
        print("fork server units")
        test_forkserver_units(tmp)
        print("bounded header reads (4h6.87)")
        test_header_reader_units()
        print("fork server failure paths")
        test_pre_ready_execute(tmp)
        test_pre_ready_body_bytes(tmp)
        test_forkserver_lost_fork_reply(tmp)
        test_forkserver_death_mid_execution(tmp)
        print("cross-execution memory isolation (4h6.55 option (b))")
        test_isolation(tmp)
        print("end to end over HTTP")
        root = os.path.join(tmp, "scratch")
        os.makedirs(root)
        test_http(Server(root))
        print("what an execution leaves behind (4h6.66, 4h6.83)")
        root = os.path.join(tmp, "survivors")
        os.makedirs(root)
        test_survivors(Server(root))
        root = os.path.join(tmp, "chain")
        os.makedirs(root)
        test_survivor_chain(root)
        print("backpressure over HTTP")
        root = os.path.join(tmp, "backpressure")
        os.makedirs(root)
        test_backpressure(Server(root))
        print("capping and accounting units")
        test_cap_units(tmp)
        print("hardening units: budget, kill-path races, response bounds")
        test_hardening_units(tmp)
        print("audit stream units: the read-end caps and the re-framing")
        test_audit_units()
        print("audit stream over HTTP, into this process's own stdout")
        root = os.path.join(tmp, "audit")
        os.makedirs(root)
        server = Server(root)
        try:
            test_audit_stream(server, _StdoutCapture)
        finally:
            server.close()
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


def run_container(base_url, retention_s=None, container_name=None):
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
    # The audit stream is the one control whose OUTPUT is not on the wire: it goes to the
    # container's stdout, which is what the cluster's logging agent collects and what
    # `docker logs` shows. Reading it needs the container's name, so without one this proves
    # nothing and says so by name rather than passing quietly.
    if container_name:
        print("audit stream, against the container's own stdout")
        test_audit_stream(server, lambda: _DockerLogCapture(container_name))
    else:
        skip("audit stream against the container",
             "no --container-name; the records go to the container's stdout, not the wire")
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
        "artifact integrity (test_artifact_integrity) — tampers with a retained artifact "
        "directly, which needs the harness's own view of /scratch",
        "what an execution leaves behind (test_survivors) — reads /proc for a pid in the "
        "supervisor's pid namespace, and disables the kill and the sweep in the module to get "
        "its negative control",
        "startup wipe (test_startup_wipe) — calls wipe_unrecognised_scratch() directly",
        "capping and accounting units (test_cap_units) — calls _cap_output/_dir_usage directly",
        "hardening units (test_hardening_units) — calls _trim_artifacts/_cap_response/_reap directly",
        "audit stream units (test_audit_units) — calls _AuditForwarder and _drain directly",
        "fork server units (test_forkserver_units) — drives ForkServer and _payload_fd directly",
        "fork server failure paths (test_pre_ready_execute, test_pre_ready_body_bytes, "
        "test_forkserver_lost_fork_reply, test_forkserver_death_mid_execution) — needs to bind "
        "its own pre-ready supervisor and gate ForkServer.start() around a refused request, to "
        "drop a fork reply inside the control protocol and to SIGKILL the fork server, none of "
        "which is reachable over the wire",
        "bounded header reads (test_header_reader_units) — drives _HeaderBoundedReader over a "
        "socketpair, which needs the module",
        "cross-execution memory isolation (test_isolation) — plants its positive control in "
        "the supervisor module before the fork server is forked, which needs the module",
    ])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    usage = ("usage: test-supervisor.py [--container URL [--retention-s N] "
             "[--container-name NAME]]")
    base_url = None
    retention_s = None
    container_name = None
    try:
        while argv:
            flag = argv.pop(0)
            if flag == "--container":
                base_url = argv.pop(0)
            elif flag == "--retention-s":
                # Asserted by the caller, never discovered: the wire contract exposes no
                # retention field and inventing one to make a test easier would be inventing a
                # contract.
                retention_s = int(argv.pop(0))
            elif flag == "--container-name":
                # Same standing: the audit stream leaves by the container's stdout, which is
                # not on the wire, so the harness is TOLD where to read it rather than
                # discovering it.
                container_name = argv.pop(0)
            else:
                raise ValueError(flag)
    except (IndexError, ValueError):
        print(usage, file=sys.stderr)
        return 2
    if (retention_s is not None or container_name is not None) and not base_url:
        print(usage, file=sys.stderr)
        return 2

    if base_url:
        run_container(base_url, retention_s=retention_s, container_name=container_name)
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
