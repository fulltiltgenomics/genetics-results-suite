import http.client
import json
import os
import socketserver
import subprocess
import sys
import threading
import time

from .harness import check, make_body, sup
from .isolation import _ISOLATION_PROBE, _pair


def test_pre_ready_execute(tmp):
    """A POST /execute arriving before the supervisor is ready must be refused BEFORE its body
    is read.

    main() binds and serves before bring_up() on purpose, so requests do arrive during the
    multi-second prewarm(), and ForkServer.start() snapshots the address space at the end of it.
    The readiness check used to sit in _admit, after _read_body and parse_execute_request had
    already made both JWTs and the user's source into Python strings: an early request answered
    503 was still recovered from a later child by the /proc/self/mem route. A 503 does not take
    the bytes back out of the arenas.
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


# The sender has to be another process, and it was measured: the harness process IS the
# supervisor process here, so a body built with make_body() is in the heap ForkServer.start()
# snapshots no matter what the socket read buffer does — the first version of
# test_pre_ready_body_bytes recovered all three needles WITH the fix in place, for that reason
# alone. The needles are minted here, written to a file the parent reads only after the fork.
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
    """_Handler.setup as it was before the bounded header read: an 8 KiB BufferedReader as rfile.

    The negative control for test_pre_ready_body_bytes, and a whole restoration of the defect
    rather than a flag the fixed code reads. It asserts AT LEAST ONE needle recovers, and which
    is arena-dependent: measured 2 of 3. A run seeing 2 red and 1 green is the control working,
    not a flake.
    """
    socketserver.StreamRequestHandler.setup(self)
    self.rfile = self.connection.makefile("rb", -1)


def test_pre_ready_body_bytes(tmp):
    """A body that shares a TCP segment with its headers must not be in the supervisor when the
    fork server is forked.

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
