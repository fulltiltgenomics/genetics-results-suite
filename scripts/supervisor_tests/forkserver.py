import ast
import os
import signal
import socket
import threading
import time

from .harness import ROOT, Server, check, make_body, sup


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

    # -- A failed round trip loses message alignment permanently. SOCK_SEQPACKET cannot lose
    # framing, but a send that succeeded followed by a receive that did not leaves the peer's
    # reply queued: after an FS_OP_WAIT timed out, the next FS_OP_REAP returned that WAIT's
    # {'ok': True}. The ordering that matters is a fork whose reply is lost — the child WAS
    # forked, so the next execution adopts a stale pid and watchdogs, killpgs and reaps the
    # previous user's child. The socket must fail closed.
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

    This is the hole the fork server opened and the only one poisoning does not close. The fork
    server forked the child and answered; the supervisor's round trip failed before reading it,
    so job.pid stays None and nothing has a pid to kill. Poisoning stops that pid being
    misattributed to the next execution, which was the dangerous half, but the child itself keeps
    running user code at uid 65532 with write access to /scratch for the pod's lifetime. The fork
    server tracks what it forked and kills it when the control channel ends; this steals the pid
    the supervisor never sees and watches it die.
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
    500s and frees the slot while the user's code runs on for the pod's lifetime. _kill_group
    signals with os.killpg directly and never through the control socket, so it works here.
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
