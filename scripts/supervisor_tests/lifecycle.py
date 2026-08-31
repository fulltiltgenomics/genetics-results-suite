import errno
import os
import socketserver
import sys
import threading
import time

from .harness import Server, _LogCapture, check, make_body, sup


ENV_PIPES_OUTSIDE_TRY = "SUPERVISOR_TEST_PIPES_OUTSIDE_TRY"


_real_pipe = os.pipe


_real_execute_inner = sup.Supervisor._execute_inner


_control_leaks = []


class _PipeFailsOn:
    """os.pipe() that raises EMFILE on its Nth call and works otherwise.

    EMFILE rather than a synthetic error because fd exhaustion is the only state that reaches
    this code, which is why the leak compounds exactly when the process can least afford it.
    """

    def __init__(self, fail_on):
        self.fail_on = fail_on
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls == self.fail_on:
            raise OSError(errno.EMFILE, "Too many open files")
        return _real_pipe()


def _pipes_outside_try(self, job):
    """_execute_inner as it was with a pipe pair created OUTSIDE the try. The negative control.

    The pre-fix prologue made all three pairs above `try:`, so a later os.pipe() raising EMFILE
    never reached the `except BaseException` that closes them. This makes exactly one such
    unowned pair — with the real os.pipe, so the arming counter still fails on the same call —
    and then runs the unchanged body.
    """
    _control_leaks.append(_real_pipe())
    return _real_execute_inner(self, job)


def _open_pipe_fds():
    """How many of this process's descriptors are pipe ends, from /proc.

    Counting pipes, not descriptors: the census has to survive the accepted socket and the client
    connection the request opens and closes on their own schedule.
    """
    total = 0
    for name in os.listdir("/proc/self/fd"):
        try:
            if os.readlink("/proc/self/fd/" + name).startswith("pipe:"):
                total += 1
        except OSError:
            pass  # the entry was the listdir's own descriptor, already gone
    return total


def _await_zero_responses(supervisor, timeout=5.0):
    """The in-flight count, polled to zero. The decrement is in the handler's `finally`, which
    runs after the bytes are on the socket — so the client can be back here first by a hair.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if supervisor.responses_in_flight() == 0:
            return 0
        time.sleep(0.01)
    return supervisor.responses_in_flight()


def test_pipe_fd_ownership(tmp):
    """An os.pipe() that raises must leak nothing, including the pairs made before it.

    _execute_inner made its three pairs above the try that owns every other descriptor, so an
    EMFILE on the second or third call lost the two or four already made — permanently, in the
    one state where they matter.
    """
    root = os.path.join(tmp, "pipe-ownership")
    os.makedirs(root)
    server = Server(root)
    installed = os.environ.get(ENV_PIPES_OUTSIDE_TRY) == "1"
    if installed:
        sup.Supervisor._execute_inner = _pipes_outside_try
    suffix = (" (SUPERVISOR_TEST_PIPES_OUTSIDE_TRY=1 is installed: this is the control)"
              if installed else "")
    try:
        for ordinal, label in ((2, "second"), (3, "third")):
            armed = _PipeFailsOn(ordinal)
            before = _open_pipe_fds()
            os.pipe = armed
            try:
                status, _, body = server.request("POST", "/execute", make_body())
            finally:
                os.pipe = _real_pipe
            after = _open_pipe_fds()
            check(f"pipe ownership: an EMFILE from the {label} os.pipe() is answered, not "
                  "swallowed", status == 500 and armed.calls >= ordinal,
                  f"got {status} after {armed.calls} os.pipe() call(s): {str(body)[:160]}")
            check(f"pipe ownership: an EMFILE from the {label} os.pipe() leaks no descriptor — "
                  "the pairs already made are inside the try that closes them",
                  after == before, f"{after - before} pipe fd(s) leaked "
                  f"({before} -> {after}){suffix}")
            check(f"pipe ownership: and the response owed for that failure is given back "
                  f"({label} os.pipe())",
                  _await_zero_responses(server.supervisor) == 0,
                  f"{server.supervisor.responses_in_flight()} still in flight")
    finally:
        os.pipe = _real_pipe
        sup.Supervisor._execute_inner = _real_execute_inner
        while _control_leaks:
            for fd in _control_leaks.pop():
                try:
                    os.close(fd)
                except OSError:
                    pass
        server.close()


ENV_SHUTDOWN_ON_IDLE = "SUPERVISOR_TEST_SHUTDOWN_ON_IDLE"


def _shutdown_on_idle(httpd, supervisor, poll=0.02):
    """_shutdown_when_idle as it was when it waited on idle() and nothing else. The control.

    idle() is true the instant run()'s finally gives the execution slot back, which is before the
    handler writes the 200 — so this returns, main() would exit, and the answer to a COMPLETED
    execution never reaches the socket.
    """
    while not supervisor.idle():
        time.sleep(poll)
    httpd.shutdown()


def test_shutdown_race(tmp):
    """A SIGTERM landing after the slot release still yields a COMPLETE response.

    Constructed, not raced: _release is wrapped so the drain and the shutdown thread start at the
    exact instant the slot is freed, and the window is held open for a second. The property is an
    ORDERING, so the check reads what the gate saw when it returned, not how long anything took.
    """
    root = os.path.join(tmp, "shutdown-race")
    os.makedirs(root)
    server = Server(root)
    real_release = sup.Supervisor._release
    real_send = sup._Handler._send_json
    real_health = sup.Supervisor.health
    gate = (_shutdown_on_idle if os.environ.get(ENV_SHUTDOWN_ON_IDLE) == "1"
            else sup._shutdown_when_idle)
    installed = gate is not sup._shutdown_when_idle
    suffix = (" (SUPERVISOR_TEST_SHUTDOWN_ON_IDLE=1 is installed: this is the control)"
              if installed else "")
    try:
        # Nothing is executing, so the gate is open — and it must stay open for a readiness
        # probe. If /health were counted, a kubelet polling every few seconds could hold the
        # drain past terminationGracePeriodSeconds and lose the wipe as well as the answer.
        probe = {}

        def watched_health(self):
            probe["quiescent"] = self.quiescent()
            return real_health(self)

        sup.Supervisor.health = watched_health
        try:
            status, _, _ = server.request("GET", "/health")
        finally:
            sup.Supervisor.health = real_health
        check("shutdown race: a /health in flight does not hold the shutdown gate — the probe "
              "is not counted", status == 200 and probe.get("quiescent") is True,
              f"health {status}, quiescent inside the handler {probe.get('quiescent')!r}")

        seen = {}
        written = threading.Event()
        exited = threading.Event()

        def racing_release(self, job, retain):
            real_release(self, job, retain)
            if not seen:
                # SIGTERM, delivered exactly here: begin_drain() then the shutdown thread, which
                # is all main()'s handler does.
                seen["idle"] = self.idle()
                seen["quiescent"] = self.quiescent()
                self.begin_drain()

                def _gate():
                    gate(server.httpd, self, 0.02)
                    seen["written_at_exit"] = written.is_set()
                    exited.set()

                threading.Thread(target=_gate, daemon=True).start()
                time.sleep(1.0)  # hold the window open so the race has a decided winner

        def watched_send(self, code, payload, extra_headers=()):
            real_send(self, code, payload, extra_headers)
            if code == 200:
                written.set()

        sup.Supervisor._release = racing_release
        sup._Handler._send_json = watched_send
        try:
            status, _, body = server.request("POST", "/execute", make_body())
        finally:
            sup.Supervisor._release = real_release
            sup._Handler._send_json = real_send
        exited.wait(60)

        check("shutdown race: the execution completed and was answered in full",
              status == 200 and (body or {}).get("output", "").startswith("hi"),
              f"got {status} {str(body)[:200]}")
        # The reason the shutdown path needs its own predicate rather than idle(): measured
        # inside the window, idle() already says the process may exit.
        check("shutdown race: inside the window idle() is TRUE — the execution slot is free — "
              "while quiescent() is FALSE, because the answer is still owed",
              seen.get("idle") is True and seen.get("quiescent") is False,
              f"idle={seen.get('idle')!r} quiescent={seen.get('quiescent')!r}")
        check("shutdown race: a SIGTERM in that window does not open the shutdown gate until "
              "the response has been written",
              exited.is_set() and seen.get("written_at_exit") is True,
              f"gate returned={exited.is_set()} with the 200 written={seen.get('written_at_exit')!r}"
              + suffix)
        check("shutdown race: and the count is given back, so the drain finishes rather than "
              "running out the 130s grace",
              _await_zero_responses(server.supervisor) == 0,
              f"{server.supervisor.responses_in_flight()} still in flight")
    finally:
        sup.Supervisor._release = real_release
        sup._Handler._send_json = real_send
        sup.Supervisor.health = real_health
        server.close()


def test_shutdown_count_units(tmp):
    """The count must come back on the ERROR exits too, not only the 200.

    A count that leaks on a refusal is worse than the race it replaces: the drain never reaches
    zero and the kubelet SIGKILLs at terminationGracePeriodSeconds. These are the exits reachable
    over the wire without a fork.
    """
    root = os.path.join(tmp, "shutdown-count")
    os.makedirs(root)
    server = Server(root)
    try:
        cases = (
            ("a body the parser refuses", lambda: server.request(
                "POST", "/execute", {"code": 1})),
            ("the wrong content type", lambda: server.request(
                "POST", "/execute", make_body(), ctype="text/plain")),
        )
        for label, call in cases:
            call()
            check(f"shutdown count: {label} leaves nothing in flight",
                  _await_zero_responses(server.supervisor) == 0,
                  f"{server.supervisor.responses_in_flight()} still in flight")
        # ...and the 409 path, which is the one that gets past the parser and into run().
        body = make_body()
        status, _, _ = server.request("POST", "/execute", body)
        dup, _, _ = server.request("POST", "/execute", body)
        check("shutdown count: a duplicate execution_id (409, raised inside run()) leaves "
              "nothing in flight",
              status == 200 and dup == 409 and _await_zero_responses(server.supervisor) == 0,
              f"first {status}, duplicate {dup}, "
              f"{server.supervisor.responses_in_flight()} in flight")

        # Why the count has to exist at all: nothing in the shutdown joins a handler thread.
        # daemon_threads = True makes socketserver drop the thread, so server_close() joins an
        # empty list and main()'s serving.join() waits only for serve_forever.
        recorder = socketserver._Threads()
        daemon = threading.Thread(target=lambda: None, daemon=True)
        recorder.append(daemon)
        daemon.start()
        daemon.join(10)
        check("shutdown count: the server does not join its handler threads, so the count is "
              "the only thing that can wait for a response",
              sup._Server.daemon_threads is True and list(recorder) == [],
              f"daemon_threads={sup._Server.daemon_threads}, recorded {list(recorder)!r}")
    finally:
        server.close()


ENV_COUNT_NO_FINALLY = "SUPERVISOR_TEST_COUNT_NO_FINALLY"


ENV_SHUTDOWN_NO_CEILING = "SUPERVISOR_TEST_SHUTDOWN_NO_CEILING"


_real_handler_execute = sup._Handler._execute


def _count_without_finally(self):
    """_execute with end_response() moved OUT of the finally. The negative control.

    Every counted exit the other checks drive returns normally, so none of them needs the finally
    and this control leaves them all green. The one exit that does need it is an exception
    ESCAPING the counted region, which is not hypothetical: _send_json calls
    send_response()/end_headers() outside its own `except OSError`, so a client resetting
    mid-execution raises ConnectionResetError straight out of the handler body.
    """
    sup.SUPERVISOR.begin_response()
    self._execute_and_answer()
    sup.SUPERVISOR.end_response()


def _shutdown_no_ceiling(httpd, supervisor, poll=0.02, deadline_s=None):
    """_shutdown_when_idle with the ceiling removed. The control.

    It polls quiescent() and nothing else, so one handler parked in sendall holds it for as long
    as the peer likes; measured at 115s and still going with 20,000 pipelined 400s on a socket
    the client never read. deadline_s is accepted and ignored on purpose.
    """
    while not supervisor.quiescent():
        time.sleep(poll)
    httpd.shutdown()


class _RecordingHttpd:
    """Stands in for the real server so the gate can be driven without tearing it down."""

    def __init__(self):
        self.shutdowns = 0

    def shutdown(self):
        self.shutdowns += 1


def test_shutdown_count_escapes(tmp):
    """An exception ESCAPING the counted region must still leave the count at zero.

    This is the only exit the finally exists for — every other counted exit returns normally —
    and it is reachable over the wire: a client that resets mid-execution makes
    send_response()/end_headers() raise out of _send_json, which is outside its own
    `except OSError`. Measured with SO_LINGER(1,0) against a 3s execution: with the finally the
    count came back to 0, without it the supervisor never became quiescent again. Simulated here
    at the same point rather than raced with a real reset.
    """
    root = os.path.join(tmp, "shutdown-escape")
    os.makedirs(root)
    server = Server(root)
    real_send = sup._Handler._send_json
    installed = os.environ.get(ENV_COUNT_NO_FINALLY) == "1"
    suffix = (" (SUPERVISOR_TEST_COUNT_NO_FINALLY=1 is installed: this is the control)"
              if installed else "")
    escaped = []

    def resetting_send(self, code, payload, extra_headers=()):
        if code == 200:
            raise ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
        real_send(self, code, payload, extra_headers)

    def record_error(request, client_address):
        escaped.append(sys.exc_info()[1])

    if installed:
        sup._Handler._execute = _count_without_finally
    sup._Handler._send_json = resetting_send
    server.httpd.handle_error = record_error
    try:
        try:
            server.request("POST", "/execute", make_body())
        except Exception:
            pass  # nothing was written, so the client sees the connection close
        check("shutdown count: a client reset mid-response really does escape the handler "
              "body — send_response()/end_headers() are outside _send_json's except OSError",
              any(isinstance(exc, ConnectionResetError) for exc in escaped),
              f"the server recorded {escaped!r}")
        check("shutdown count: an exception ESCAPING the counted region still gives the count "
              "back, so the drain reaches zero rather than hanging until the SIGKILL",
              _await_zero_responses(server.supervisor) == 0,
              f"{server.supervisor.responses_in_flight()} still in flight" + suffix)
    finally:
        sup._Handler._send_json = real_send
        sup._Handler._execute = _real_handler_execute
        try:
            del server.httpd.handle_error
        except AttributeError:
            pass
        server.close()


def test_shutdown_ceiling(tmp):
    """The drain gate must have a ceiling: one response can otherwise hold it forever.

    _send_json's write is a blocking sendall on a connection left at settimeout(None), so a peer
    that stops reading parks a COUNTED handler with no deadline of its own. The count coming back
    on every exit does not help when the exit never happens, which is why the finally and
    DRAIN_DEADLINE_S close different routes to the same hang. Driven by holding a count directly,
    against a short deadline rather than 125 real seconds.
    """
    root = os.path.join(tmp, "shutdown-ceiling")
    os.makedirs(root)
    server = Server(root)
    gate = (_shutdown_no_ceiling if os.environ.get(ENV_SHUTDOWN_NO_CEILING) == "1"
            else sup._shutdown_when_idle)
    installed = gate is not sup._shutdown_when_idle
    suffix = (" (SUPERVISOR_TEST_SHUTDOWN_NO_CEILING=1 is installed: this is the control)"
              if installed else "")
    held = False
    try:
        check("shutdown ceiling: DRAIN_DEADLINE_S leaves room for a full-length execution "
              "(MAX_TIMEOUT_S + KILL_GRACE_S) and still exits inside the 130s grace",
              sup.MAX_TIMEOUT_S + sup.KILL_GRACE_S < sup.DRAIN_DEADLINE_S < 130,
              f"{sup.MAX_TIMEOUT_S} + {sup.KILL_GRACE_S} < {sup.DRAIN_DEADLINE_S} < 130")

        server.supervisor.begin_response()
        held = True
        httpd = _RecordingHttpd()
        returned = threading.Event()
        with _LogCapture() as capture:
            def _gate():
                gate(httpd, server.supervisor, 0.02, 0.3)
                returned.set()

            threading.Thread(target=_gate, daemon=True).start()
            returned.wait(5)
        check("shutdown ceiling: a response that never finishes writing does NOT hold the "
              "drain open — the gate proceeds to shutdown() at the deadline",
              returned.is_set() and httpd.shutdowns == 1,
              f"gate returned={returned.is_set()}, shutdown() calls={httpd.shutdowns}, "
              f"{server.supervisor.responses_in_flight()} in flight" + suffix)
        check("shutdown ceiling: and it is logged loudly, naming the in-flight count, because "
              "an operator seeing this needs to know the answer was abandoned",
              any("drain deadline reached" in line and "1 response(s) still in flight" in line
                  for line in capture.lines),
              f"logged {capture.lines!r}" + suffix)
    finally:
        if held:
            server.supervisor.end_response()
        server.close()
