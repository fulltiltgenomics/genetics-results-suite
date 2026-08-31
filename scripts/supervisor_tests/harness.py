"""Shared plumbing for the supervisor check groups: the counters, the assertion helpers,
the request builders, and the two Server flavours (in-process and container)."""

import base64
import http.client
import io
import json
import logging
import os
import sys
import threading
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


def expect_request_error(name, fn, status, type_, suffix=""):
    try:
        fn()
    except sup.RequestError as exc:
        check(name, exc.status == status and exc.type == type_,
              f"got {exc.status} {exc.type}" + suffix)
    except Exception as exc:
        check(name, False, f"raised {type(exc).__name__}: {exc}" + suffix)
    else:
        check(name, False, "no error raised" + suffix)


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


class _AliveForkServer:
    """A stand-in for tests that build a Supervisor directly and are not about the fork server.

    health() asks the fork server whether it is alive, so a bare Supervisor needs one to be
    asked about."""

    pid = -1

    @staticmethod
    def alive():
        return True


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
        # that is about to answer — the exact misreading the client was corrected for.
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

    It inherits request() and nothing else: there is no in-process supervisor object and no host
    view of /scratch, so every check that needs either is skipped by name.
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


class _LogCapture(logging.Handler):
    """The supervisor's own LOG, which is the audit stream: main() points logging at stdout."""

    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def __enter__(self):
        # The harness never calls basicConfig, so this logger sits at the root's WARNING and
        # LOG.info() returns before any handler sees it. In the image main() puts it at INFO on
        # stdout, which is the stream this test is about.
        self._level = sup.LOG.level
        sup.LOG.setLevel(logging.INFO)
        sup.LOG.addHandler(self)
        return self

    def __exit__(self, *exc):
        sup.LOG.removeHandler(self)
        sup.LOG.setLevel(self._level)
        return False
