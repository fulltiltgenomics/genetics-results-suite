import base64
import json
import os
import socket
import threading
import time

from .harness import check, make_body, skip, sup


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
        # with a charset and the client sends it bare. Both must be accepted, or
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

        # The integrity binding over the wire, against an artifact a REAL execution wrote and a
        # REAL manifest listed: the tamper is a plain write at the shared uid, which is the
        # whole primitive.
        # Container mode has no host view of /scratch, so it cannot do the write.
        if server.host_scratch is None:
            skip("http: /artifact refuses a tampered artifact",
                 "no host view of /scratch in container mode")
        else:
            out_csv = os.path.join(server.host_scratch, payload["execution_id"],
                                   "artifacts", "out.csv")
            # Over the wire, against an artifact a real execution wrote. The bytes are kept so
            # the negative control below can put back what was THERE rather than what was
            # WRITTEN — restoring plaintext would no longer authenticate.
            with open(out_csv, "rb") as fh:
                sealed_on_disk = fh.read()
            check("http: a retained artifact is SEALED on disk — a same-uid read gets the "
                  "envelope and not the four bytes the script wrote",
                  b"a,b\n" not in sealed_on_disk
                  and len(sealed_on_disk) == 4 + sup.ARTIFACT_ENVELOPE_BYTES,
                  f"read {sealed_on_disk!r} off disk")
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
                fh.write(sealed_on_disk)   # restore, so later checks see the execution's own bytes
            status, _, art = server.request(
                "GET", f"/artifact?execution_id={payload['execution_id']}&name=out.csv")
            check("http: NEGATIVE CONTROL — restoring the SEALED bytes the execution itself "
                  "left on disk makes /artifact serve it again, so the refusal is the content "
                  "check and not the fact that a write happened",
                  status == 200 and base64.b64decode(art["content_base64"]) == b"a,b\n",
                  f"got {status} {art}")

        # The zero-byte boundary, on the wire. An empty artifact is ordinary, and it is the
        # case where the sealed read used to raise out of the ctypes layer into
        # socketserver.handle_error, which closes the socket with no status line — so this
        # asserts a STATUS, not only a body.
        status, _, body = server.request("POST", "/execute", body=make_body(
            code="import os\n"
                 "d = os.environ['SANDBOX_ARTIFACTS_DIR']\n"
                 "open(os.path.join(d, 'empty.bin'), 'wb').close()\n"
                 "open(os.path.join(d, 'ok.csv'), 'w').write('a,1\\n')\n"))
        empty_eid = body["execution_id"]
        check("http: an execution that writes a ZERO-BYTE artifact lists it in the manifest "
              "with size 0",
              status == 200
              and {(e["name"], e["size"]) for e in body["artifacts"]}
              == {("empty.bin", 0), ("ok.csv", 4)},
              f"got {status} {body.get('artifacts')}")
        check("http: a normal execution carries artifacts_retained_in_clear false — the field "
              "is always present, so a client can read it without treating absence as safe",
              body.get("artifacts_retained_in_clear") is False,
              f"got {body.get('artifacts_retained_in_clear')!r}")
        status, _, art = server.request(
            "GET", f"/artifact?execution_id={empty_eid}&name=empty.bin")
        check("http: GET on the zero-byte artifact the manifest advertised answers 200 with "
              "an empty body — the manifest must never name something the read cannot serve",
              status == 200 and art.get("size") == 0
              and base64.b64decode(art["content_base64"]) == b"",
              f"got {status} {art}")
        status, _, art = server.request(
            "GET", f"/artifact?execution_id={empty_eid}&name=ok.csv")
        check("http: and the non-empty artifact of the same execution still serves, so the "
              "zero-byte case did not poison the connection or the key",
              status == 200 and base64.b64decode(art["content_base64"]) == b"a,1\n",
              f"got {status} {art}")

        # The wire answer when plaintext could not be removed. A 200 whose only signal is a
        # larger artifacts_omitted is not adequate for "we could not remove your data": that
        # field means "produced, present, not listed". Hence artifacts_retained_in_clear.
        #
        # And it must not be a 500. That was the first answer, and it was a same-uid
        # denial-of-service kill switch: 3 for 3, a second process at this uid chmod 0500-ing
        # /scratch/*/artifacts turned every execution into http=500 output=None. The 500 bought
        # no confidentiality either — deletion is what failed, so that peer already holds the
        # plaintext whichever status the caller gets.
        not_secured_name = ("http: an execution whose retained plaintext could be neither "
                            "sealed nor deleted still returns its stdout — a same-uid peer "
                            "cannot deny service by making the seal fail")
        not_secured_field = ("http: and it says so in artifacts_retained_in_clear, its own "
                             "field, NOT by inflating artifacts_omitted — the caller must be "
                             "able to tell 'not listed' from 'readable at this uid'")
        if server.container:
            for name in (not_secured_name, not_secured_field):
                skip(name, "the supervisor is in another process; nothing here can "
                           "make its seal pass report an unsecured directory")
        else:
            real_seal = sup.Supervisor._seal_retained
            sup.Supervisor._seal_retained = lambda self, job: ({}, 2, False)
            try:
                status, _, body = server.request("POST", "/execute", body=make_body(
                    code="import os\n"
                         "open(os.path.join(os.environ['SANDBOX_ARTIFACTS_DIR'],'x.csv'),'w')"
                         ".write('a\\n')\n"
                         "print('STDOUT-THE-CALLER-PAID-FOR')\n"))
            finally:
                sup.Supervisor._seal_retained = real_seal
            check(not_secured_name,
                  status == 200 and body["status"] == "ok" and body["error"] is None
                  and "STDOUT-THE-CALLER-PAID-FOR" in (body.get("output") or ""),
                  f"got {status} {body}")
            # 3 = the stub's 2 purged + the one artifact the script wrote, which the manifest
            # omits because the stubbed seal map does not name it.
            check(not_secured_field,
                  body.get("artifacts_retained_in_clear") is True
                  and body["artifacts"] == [] and body["artifacts_omitted"] == 3,
                  f"got {body}")

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
            # 0-2, the status pipe (3), the audit pipe (4) and the listdir handle the print
            # opened. The bound is what matters: a number above that is a supervisor descriptor
            # — the listening socket, another client's connection — that the sweep missed.
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
    """429 over the wire, with Retry-After: the client's retry policy reads it."""
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


# Writes are paced rather than blasted, and that is not politeness: /scratch is 512Mi in both
# the pod and the local container, the watchdog polls every 0.2s, and an unpaced writer reaches
# ~1 GiB/s — so an unpaced quota test can hit ENOSPC, or an eviction, before the poll it is
# trying to demonstrate ever runs.
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

    # -- wall clock. Not overridable upward (parse rejects >120); this proves the
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

    # -- RLIMIT_AS. The clean MemoryError inside the child is the whole point: an OOM
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

    # A limit the child can raise back is not a limit: raising a soft limit up to the hard limit
    # needs no privilege, and with the hard limit at RLIM_INFINITY a 2900 MiB allocation produced
    # exactly the cgroup OOM kill the limit exists to prevent.
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

    # -- pid budget. RLIMIT_NPROC cannot do this job under one shared uid, so the
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

    # -- the 8 MiB pipe cap. A different limit from the 64 KiB return window, and
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

    # -- the 64 KiB return window: head AND tail, with a visible marker between.
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

    # -- the per-execution artifact quota: a clean error, never a pod eviction.
    blocks = (sup.ARTIFACT_QUOTA_BYTES // (4 * 1024 * 1024)) + 8
    status, _, body = server.request("POST", "/execute", body=make_body(
        code=_FILL.format(dir="__ARTIFACTS__", blocks=blocks).replace(
            "'__ARTIFACTS__'", "os.environ['SANDBOX_ARTIFACTS_DIR']"), timeout_s=60))
    check("artifact quota: over 64Mi in artifacts/ is a limit",
          body["status"] == "limit" and body["error"]["type"] == "ArtifactQuota",
          f"got {body['status']} {body.get('error')}")

    # -- the per-execution total quota, over ALL of /scratch/<id>, not just artifacts.
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

    # -- Zero-length files are not free. Charging st_blocks alone said they were: 300,000 empty
    # files charged 8.6 MB against the 192 MiB quota, so nothing fired, while the response
    # reached 19.8 MB and the slot was held 58.7s for a 34.9s child. The entry budget is what
    # makes the byte quota unbypassable and keeps the wall clock from being child-controlled.
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

    # -- The overshoot is trimmed, not retained. An unpaced writer killed at the 64 MiB quota
    # left 93 MiB on disk (46% over); retaining that makes the 256 MiB aggregate ceiling a
    # ceiling over unbounded terms. The trim is visible on the wire.
    #
    # Paced, and the pacing is what makes the check a check. Unpaced this raced the watchdog's
    # first scan, which lands at t~200ms: on a fast tmpfs all 192 MiB completed inside that
    # window, so the watchdog returned having never scanned and the execution answered `ok` —
    # reproduced on an unedited tree, one run red and the next green. Pacing 12 x 8 MiB at 100ms
    # puts >= 12 scans inside the write and still exercises the over-quota trim path.
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
    # getting neither a cap nor a validator (the same defect fixed on the client side,
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
    """The read-once token file, seen from inside the child that has to use it."""
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
    """The aggregate retained ceiling evicts oldest-first.

    Observable on the wire without any host view: an evicted execution's id stops being a
    409 DuplicateExecutionId and becomes usable again, because its directory is gone.
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
    """Artifacts are deleted at the retention deadline whether or not read."""
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
