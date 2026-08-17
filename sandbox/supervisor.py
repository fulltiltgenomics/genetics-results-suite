#!/usr/bin/env python3
"""The process the sandbox pod runs: HTTP front door, one-at-a-time queue, fork/reap.

Design of record: docs/code-execution-security.md. The wire shape this file implements is
section 2's "The HTTP contract between chat-backend and the supervisor" (4h6.38) and that
subsection is the ONLY definition of it — chat-backend's client (4h6.47) cannot import
anything from here, because the image pip-installs only the genetics SDK's import closure
and prune_venv.py deletes the rest. Every constant below that names a wire value carries a
pointer to the row it comes from; do not change one without changing the other.

This file is genetics-results-suite-4h6.39 and is deliberately INCOMPLETE. Five siblings own
the rest, each marked in place with `STUB (<bead>)`:

  4h6.41  wall clock and pid policing (parent)      -> _apply_limits
          RLIMIT_AS and the child's oom_score_adj   -> _apply_child_limits
  4h6.42  stdout capture, the 8 MiB pipe cap, the 64 KiB head+tail return   -> _drain, _cap_output
  4h6.43  per-execution token delivery by read-once file                    -> _deliver_tokens
  4h6.45  reading/capping/re-framing/stamping the SDK audit stream          -> _audit_pipe
  4h6.46  /scratch sub-quotas, artifact retention and the reaper            -> _retain

Consequences of those gaps, stated because they are runtime behaviours and not TODOs:
NOTHING KILLS A RUNAWAY CHILD until 4h6.41 lands — `status: "timeout"` is unreachable and a
non-terminating script holds the only slot forever. `status: "limit"` is likewise
unreachable until 4h6.42. Nothing is ever deleted after an execution completes until 4h6.46
lands, so /scratch grows until the pod restarts. A descendant that outlives the child by
`setsid()`ing away keeps the output pipe open; the drain abandons it DRAIN_GRACE_S after the
child is reaped and the slot is freed then, so the escaped process still runs but no longer
blocks the queue. Killing it is 4h6.41's and 4h6.46's business, not this file's.

STRUCTURAL CONSTRAINTS, each of which fails at runtime rather than at review:

* No setuid and no chown. Supervisor and child share uid 65532 — option (b), forced by the
  pod dropping CAP_SETUID/CAP_SETGID/CAP_CHOWN (section 2, "The uid choice"). SANDBOX_CHILD_UID
  is advertised by the image and names a uid nothing here can switch to. Section 2's
  "Permission contract" is NOT IN EFFECT.
* Stdlib only, plus what sandbox/requirements.txt already ships. A new third-party import
  has to be added to requirements.txt and prune_venv.py's allow-list deliberately.
* The child is FORKED AND NOT EXEC'D. That is what makes prewarm() worth anything: the
  pre-imported numpy/scipy/polars/matplotlib pages are inherited copy-on-write. An exec
  would pay the cold-import cost on every execution.
"""

import base64
import errno
import http.server
import json
import logging
import mimetypes
import os
import re
import select
import shutil
import signal
import socket
import stat
import sys
import threading
import time
import traceback as _traceback
from collections import deque

# --------------------------------------------------------------------------------------
# Contract constants. Section 2 of docs/code-execution-security.md owns every one of these.
# --------------------------------------------------------------------------------------

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8080

MAX_BODY_BYTES = 1024 * 1024          # raw bytes on the wire -> 413
MAX_CODE_BYTES = 256 * 1024           # len(code.encode("utf-8")) -> 413
BODY_READ_TIMEOUT_S = 10.0            # request line to last byte -> 408
DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 120                   # rejected, never clamped
QUEUE_DEPTH = 2                       # WAITING requests, not counting the one executing
MAX_QUEUED_WAIT_S = 120.0             # the number the 300s token TTL constrains
RETRY_AFTER_S = 60
KILL_GRACE_S = 2.0                    # SIGTERM -> SIGKILL, timeout and output-cap paths

# Not a wire value and not in section 2: how long the supervisor keeps reading the pipes
# AFTER the child has been reaped. A `setsid()`ed descendant inherits the write ends, so EOF
# is not something waiting longer can produce — see _drain.
DRAIN_GRACE_S = 2.0

MESSAGE_MAX_BYTES = 2048              # error.message
TRACEBACK_MAX_BYTES = 8192            # error.traceback, tail-capped

# `\Z`, not `$`, and matched with fullmatch: `$` also matches immediately before a final
# newline, so `^...$` with re.match accepts a trailing "\n" — which then names a directory
# and is echoed back in the response. Both anchors are kept so a later switch to .search()
# cannot quietly widen this.
EXECUTION_ID_RE = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
TOKEN_AUDIENCES = ("db-api", "results-api")

_EXECUTE_FIELDS = frozenset(
    {"code", "execution_id", "tokens", "user", "session_id", "timeout_s"}
)

# Reserved error.type names. error.type is an OPEN string — the child's exception class name
# is the other half of its range — and these are the reserved minimum a client may branch on.
ERR_TIMEOUT = "Timeout"
ERR_MEMORY_LIMIT = "MemoryLimit"
ERR_PID_LIMIT = "PidLimit"
ERR_ARTIFACT_QUOTA = "ArtifactQuota"
ERR_SCRATCH_QUOTA = "ScratchQuota"
ERR_OUTPUT_LIMIT = "OutputLimit"
ERR_NON_ZERO_EXIT = "NonZeroExit"
ERR_KILLED = "Killed"
ERR_STARTUP_FAILURE = "StartupFailure"

# --------------------------------------------------------------------------------------
# Local-vs-pod knobs. Every one of these is set by sandbox/Dockerfile in the image, so an
# unset value means "not running in the sandbox image" and is answered with a loud warning
# rather than a silent behaviour change. 4h6.40 runs the real image in plain Docker; these
# exist for running supervisor.py straight out of a checkout, which the tests do.
# --------------------------------------------------------------------------------------

ENV_SCRATCH_ROOT = "SANDBOX_SCRATCH_ROOT"   # NOT set by the image; test-only override
ENV_MPLCACHE = "GENETICS_MPLCACHE"          # /genetics/mplcache
ENV_PREWARM = "GENETICS_PREWARM"            # /genetics/prewarm.py

DEFAULT_SCRATCH_ROOT = "/scratch"
NSSWITCH_PATH = "/etc/nsswitch.conf"

# /scratch entries the startup wipe must not delete. Everything else under the root is
# removed: after a restart the supervisor holds no record of which executions were live or
# still retained, so nothing under /scratch belongs to one. A crash mid-execution must not
# leave a readable directory behind, and that is the whole point of the wipe.
SUPERVISOR_DIR_NAME = ".supervisor"

# The child writes at most one JSON object here and nothing else. See _child_main.
CHILD_STATUS_FD = 3

# STUB (genetics-results-suite-4h6.42). The real bounds are 8 MiB off the pipe with the
# child killed at the cap, and 64 KiB head+tail returned. Neither is implemented here. This
# is a memory backstop so the skeleton cannot be turned into an OOM by a `while True: print`
# while 4h6.42 is outstanding; it is deliberately a different number from both contract
# values so nothing mistakes it for either.
_STUB_DRAIN_LIMIT_BYTES = 1 * 1024 * 1024
_STATUS_READ_LIMIT_BYTES = 64 * 1024

LOG = logging.getLogger("sandbox.supervisor")


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------


class RequestError(Exception):
    """A non-2xx answer. Carries the contract's uniform error shape."""

    def __init__(self, status, type_, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.type = type_
        self.message = message
        self.retry_after = retry_after


class ClientGone(Exception):
    """A queued request whose connection closed before it was dequeued. Never forked."""


class StartupAssertionError(RuntimeError):
    """A startup assertion failed. The pod must crash-loop rather than serve."""


# --------------------------------------------------------------------------------------
# Startup assertions — both required by the handoff table in docs/code-execution-security.md
# --------------------------------------------------------------------------------------


def assert_nsswitch_hosts_files_first(path=NSSWITCH_PATH):
    """`/etc/nsswitch.conf` exists and lists `files` before `dns` for the hosts database.

    Section 3(b) wants this as a cheap runtime backstop to build-checks.py's build-time
    check, and no other task owns it. Absent the file glibc defaults to
    `dns [!UNAVAIL=return] files`, so every lookup stalls the full resolver timeout against
    a dropping egress policy before it ever reaches hostAliases — and readOnlyRootFilesystem
    makes that unfixable at runtime.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        raise StartupAssertionError(f"{path} unreadable: {exc}") from exc

    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line.lower().startswith("hosts:"):
            continue
        # Strip glibc action blocks: `files [!UNAVAIL=return] dns` -> `files dns`.
        body = re.sub(r"\[[^\]]*\]", " ", line.split(":", 1)[1])
        sources = body.split()
        if "files" not in sources:
            raise StartupAssertionError(
                f"{path}: hosts database does not list `files`: {line!r}"
            )
        if "dns" in sources and sources.index("dns") < sources.index("files"):
            raise StartupAssertionError(
                f"{path}: hosts database lists `dns` before `files`: {line!r}"
            )
        return
    raise StartupAssertionError(f"{path}: no `hosts:` line")


def load_prewarm():
    """Import sandbox/prewarm.py from $GENETICS_PREWARM.

    Returns the module, or None when the variable is unset — which means this is not the
    sandbox image, because sandbox/Dockerfile always sets it. Callers must treat None as a
    development degradation and say so out loud.
    """
    path = os.environ.get(ENV_PREWARM)
    if not path:
        return None
    import importlib.util

    spec = importlib.util.spec_from_file_location("sandbox_prewarm", path)
    if spec is None or spec.loader is None:
        raise StartupAssertionError(f"{ENV_PREWARM}={path} is not importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------------------
# Request parsing and the token consistency checks
# --------------------------------------------------------------------------------------


def _bad(message, subtype="InvalidRequest"):
    return RequestError(400, subtype, message)


def _decode_jwt_payload(token, key):
    """Decode a compact JWS payload WITHOUT verifying the signature.

    The supervisor holds no signing key, deliberately and permanently. This reads `sub`,
    `sid`, `jti` and `exp` to check the CALLER'S OWN CONSISTENCY. It is not authentication —
    db-api's and results-api's verification is (section 4) — and nothing here may be
    mistaken for it.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise _bad(f"tokens.{key} is not a compact JWS", "InvalidToken")
    seg = parts[1]
    try:
        raw = base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        raise _bad(f"tokens.{key} payload is not decodable JSON", "InvalidToken")
    if not isinstance(payload, dict):
        raise _bad(f"tokens.{key} payload is not a JSON object", "InvalidToken")
    return payload


def _aud_matches(aud, key):
    if isinstance(aud, str):
        return aud == key
    # Some minters emit `aud` as a list. The contract says "a token's aud != the key it was
    # sent under -> 400"; a single-element list carrying the right value is the same claim.
    if isinstance(aud, list) and len(aud) == 1:
        return aud[0] == key
    return False


class ExecuteRequest:
    __slots__ = (
        "code",
        "execution_id",
        "tokens",
        "user",
        "session_id",
        "timeout_s",
        "claims",
    )

    def __init__(self, code, execution_id, tokens, user, session_id, timeout_s, claims):
        self.code = code
        self.execution_id = execution_id
        self.tokens = tokens
        self.user = user
        self.session_id = session_id
        self.timeout_s = timeout_s
        self.claims = claims

    @property
    def exp(self):
        """The earliest `exp` across both tokens, or None if neither carries one."""
        exps = [c.get("exp") for c in self.claims.values() if isinstance(c.get("exp"), (int, float))]
        return min(exps) if exps else None


def parse_execute_request(raw):
    """Bytes on the wire -> ExecuteRequest. Raises RequestError for every rejection."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        raise _bad("body is not valid UTF-8 JSON")
    if not isinstance(body, dict):
        raise _bad("body is not a JSON object")

    unknown = sorted(set(body) - _EXECUTE_FIELDS)
    if unknown:
        # Rejected rather than ignored: a field added on one side and not the other must
        # fail on the first call, which is the entire reason the contract exists.
        raise _bad(f"unknown field(s): {', '.join(unknown)}", "UnknownField")

    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        raise _bad("code is required and must be a non-empty string")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise RequestError(413, "PayloadTooLarge", "code exceeds 256 KiB")

    execution_id = body.get("execution_id")
    if not isinstance(execution_id, str) or not EXECUTION_ID_RE.fullmatch(execution_id):
        # Strict because this value names a filesystem path. Any laxer rule re-opens path
        # traversal on the one request field that becomes a directory name.
        raise _bad("execution_id is required and must be a lowercase uuid4 string")

    tokens = body.get("tokens")
    if not isinstance(tokens, dict) or set(tokens) != set(TOKEN_AUDIENCES):
        raise _bad("tokens must be an object with exactly db-api and results-api")
    for key in TOKEN_AUDIENCES:
        if not isinstance(tokens[key], str) or not tokens[key]:
            raise _bad(f"tokens.{key} must be a non-empty string")

    user = body.get("user")
    if not isinstance(user, str) or not user:
        raise _bad("user is required and must be a non-empty string")

    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise _bad("session_id is required and must be a non-empty string")

    timeout_s = body.get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int):
        raise _bad("timeout_s must be an integer")
    if timeout_s < 1 or timeout_s > MAX_TIMEOUT_S:
        # Rejected, NOT clamped. Clamping is a silent behaviour change on a path fed from a
        # model-influenceable direction, and it desyncs the client's own deadline.
        raise _bad(f"timeout_s must be between 1 and {MAX_TIMEOUT_S}")

    claims = {key: _decode_jwt_payload(tokens[key], key) for key in TOKEN_AUDIENCES}

    jtis = {claims[key].get("jti") for key in TOKEN_AUDIENCES}
    if len(jtis) != 1:
        raise _bad("the two tokens carry different jti", "TokenMismatch")
    if jtis.pop() != execution_id:
        # Refuse, do not pick a winner. Preferring the jti names the directory one thing and
        # stamps the audit another; preferring the body hands the child credentials whose
        # jti joins to no directory. Either way a downstream record keys on a value some
        # other record does not carry.
        raise _bad("token jti does not equal execution_id", "TokenMismatch")
    for key in TOKEN_AUDIENCES:
        c = claims[key]
        if not _aud_matches(c.get("aud"), key):
            raise _bad(f"tokens.{key} aud does not equal {key}", "TokenMismatch")
        if c.get("sub") != user:
            raise _bad(f"tokens.{key} sub does not equal user", "TokenMismatch")
        if c.get("sid") != session_id:
            raise _bad(f"tokens.{key} sid does not equal session_id", "TokenMismatch")

    return ExecuteRequest(code, execution_id, tokens, user, session_id, timeout_s, claims)


# --------------------------------------------------------------------------------------
# The per-execution directory
# --------------------------------------------------------------------------------------


class ExecutionDirs:
    """/scratch/<execution-id>/ and the child environment that points into it.

    Every one of these variables is deliberately UNSET in sandbox/Dockerfile: a fixed
    pod-wide value recreates the cross-execution shared directory that removing the
    pod-level /tmp was meant to prevent (section 2, Writable paths).
    """

    def __init__(self, root, execution_id):
        self.base = os.path.join(root, execution_id)
        self.artifacts = os.path.join(self.base, "artifacts")
        self.tmp = os.path.join(self.base, "tmp")
        self.home = os.path.join(self.base, "home")
        self.mplconfig = os.path.join(self.base, "mplconfig")
        self.cache = os.path.join(self.base, "cache")
        self.pycache = os.path.join(self.base, "pycache")

    def create(self):
        # 0o700 throughout. The child shares the supervisor's uid, so modes protect nothing
        # between them; they keep anything else out. mkdir on self.base doubles as the
        # last-moment duplicate check — O_EXCL semantics, so two racing dequeues cannot both
        # win the id.
        os.mkdir(self.base, 0o700)
        for path in (
            self.artifacts,
            self.tmp,
            self.home,
            self.mplconfig,
            self.cache,
            self.pycache,
        ):
            os.mkdir(path, 0o700)

    def child_env(self, claims_any):
        env = {
            "TMPDIR": self.tmp,
            "HOME": self.home,
            "MPLCONFIGDIR": self.mplconfig,
            "XDG_CACHE_HOME": self.cache,
            "PYTHONPYCACHEPREFIX": self.pycache,
            "SANDBOX_ARTIFACTS_DIR": self.artifacts,
            # The SDK's audit prefix reads these three per call (section 6). They are the
            # token's own sub/sid/jti, which the supervisor has already checked against the
            # body. Setting them makes the child's line render; it does NOT make it
            # trustworthy — the child can write anything to its own audit fd, which is why
            # 4h6.45 re-stamps on the read end from the tokens rather than believing these.
            "SANDBOX_USER": str(claims_any.get("sub", "")),
            "SANDBOX_SESSION_ID": str(claims_any.get("sid", "")),
            "SANDBOX_EXECUTION_ID": str(claims_any.get("jti", "")),
        }
        return env


def seed_mplconfig(mplconfig_dir):
    """Copy the baked font cache from $GENETICS_MPLCACHE into the per-execution MPLCONFIGDIR.

    The root filesystem is read-only and there is no pod-level /tmp, so on matplotlib 3.10 an
    unwritable MPLCONFIGDIR does not fall back to a temporary directory — `import matplotlib`
    raises outright (see sandbox/prewarm.py). Building the cache instead of copying it costs
    seconds per execution.
    """
    src = os.environ.get(ENV_MPLCACHE)
    if not src or not os.path.isdir(src):
        LOG.warning(
            "%s is unset or not a directory (%r): the per-execution MPLCONFIGDIR starts "
            "empty and matplotlib will rebuild its font cache. This is a development "
            "degradation; in the image the Dockerfile always bakes and sets it.",
            ENV_MPLCACHE,
            src,
        )
        return False
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(mplconfig_dir, name)
        if os.path.isdir(s):
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)
    return True


def wipe_unrecognised_scratch(root, keep=()):
    """Remove every /scratch entry that does not belong to a live or still-retained execution.

    Called at startup, where by construction there are none: the supervisor's record of what
    is live and what is retained is in memory and does not survive the process. So this wipes
    everything except its own reserved directory. That is the intended reading — a crash
    mid-execution must not leave a readable directory behind, and after a restart nothing can
    resolve an artifact from before it anyway (chat-backend's manifest record would point at
    an execution this process never ran).
    """
    keep = set(keep) | {SUPERVISOR_DIR_NAME}
    removed = []
    for name in sorted(os.listdir(root)):
        if name in keep:
            continue
        path = os.path.join(root, name)
        try:
            if os.path.islink(path) or not os.path.isdir(path):
                os.unlink(path)
            else:
                shutil.rmtree(path)
            removed.append(name)
        except OSError as exc:
            LOG.error("could not wipe stale /scratch entry %s: %s", name, exc)
    if removed:
        LOG.warning("wiped %d unrecognised /scratch entries at startup", len(removed))
    return removed


# --------------------------------------------------------------------------------------
# The artifact manifest
# --------------------------------------------------------------------------------------

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _name_is_retrievable(name):
    """Would read_artifact (4h6.15) be able to open a file with this name?

    A manifest must never advertise a name the read cannot open: executor.py does
    `name = name.strip()` BEFORE validating, so "plot.png " passes every other rule, gets
    listed, and is then unretrievable behind the same indistinguishable "Artifact not found"
    the model gets for a name that was never there.
    """
    if not name or name != name.strip():
        return False
    if name in (".", ".."):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    if os.path.basename(name) != name:
        return False
    if _CONTROL_CHARS.search(name):
        return False
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        # os.listdir surrogate-escapes undecodable bytes; such a name cannot be rendered to
        # the model and so cannot be asked for.
        return False
    return True


def build_manifest(artifacts_dir):
    """(entries, omitted). Lists a file only if it would survive read_artifact's checks."""
    entries = []
    omitted = 0
    try:
        dfd = os.open(artifacts_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return [], 0
    try:
        for name in os.listdir(dfd):
            if not _name_is_retrievable(name):
                omitted += 1
                continue
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except OSError:
                omitted += 1
                continue
            # Regular files directly in the directory only: no recursion (a bare name cannot
            # address a subdirectory's contents), no symlinks, no FIFOs, sockets or devices,
            # and st_nlink == 1.
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                omitted += 1
                continue
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            entries.append({"name": name, "size": st.st_size, "content_type": ctype})
    finally:
        os.close(dfd)
    entries.sort(key=lambda e: e["name"])
    return entries, omitted


# --------------------------------------------------------------------------------------
# The child
# --------------------------------------------------------------------------------------


def _close_inherited_fds(keep):
    """Close every descriptor the child has no business holding.

    The child is forked and not exec'd, so PEP 446's non-inheritable default does nothing
    here: without this the script inherits the listening socket and every other in-flight
    client connection, and could read or write another user's HTTP conversation.
    """
    keep = set(keep) | {0, 1, 2}
    try:
        names = os.listdir("/proc/self/fd")
    except OSError:
        names = None
    if names is not None:
        for name in names:
            try:
                fd = int(name)
            except ValueError:
                continue
            if fd in keep:
                continue
            try:
                os.close(fd)
            except OSError:
                pass
        return
    soft = 4096
    try:
        import resource

        value = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
        if value not in (-1, resource.RLIM_INFINITY):
            soft = value
    except Exception:
        pass
    limit = min(soft, 65536)
    for fd in range(3, limit):
        if fd in keep:
            continue
        try:
            os.close(fd)
        except OSError:
            pass


def _child_status(type_, message, tb):
    return json.dumps(
        {
            "type": type_,
            "message": (message or "")[:MESSAGE_MAX_BYTES],
            "traceback": tb[-TRACEBACK_MAX_BYTES:] if tb else None,
        }
    ).encode("utf-8", "replace")


def _child_main(code, env, cwd, out_w, status_w):
    """Runs in the forked child. Never returns.

    HOW THE CHILD REPORTS ITS EXCEPTION was left unsettled by the contract and is settled
    here: a DEDICATED STATUS PIPE carrying exactly one JSON object, not parsing the tail of
    `output`. Two reasons. The tail of output is subject to the 64 KiB head-and-tail cap
    (4h6.42), so a traceback in a chatty script is exactly what gets elided; and a script
    can print anything it likes to stdout, so parsing it lets the script forge its own
    error object. The status pipe narrows that but does NOT close it: the child is forked
    and not exec'd, so the script runs with this fd open and can write a record of its own.
    The supervisor therefore treats what arrives here as untrusted input — re-capping it, and
    discarding it outright when the child exited 0 (see _response). A child that is killed
    writes nothing, which the contract already anticipates: type "Killed", null traceback.
    """
    exit_code = 0
    try:
        os.setsid()  # own session and process group, so 4h6.41 can signal the whole group
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGPIPE):
            try:
                signal.signal(sig, signal.SIG_DFL)
            except (OSError, ValueError):
                pass

        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        if devnull > 2:
            os.close(devnull)
        # One pipe for stdout and stderr together: section 2 budgets ONE 64 KiB window for
        # what reaches the model, and splitting it across two streams either halves the
        # window or doubles the budget.
        os.dup2(out_w, 1)
        os.dup2(out_w, 2)
        if status_w != CHILD_STATUS_FD:
            os.dup2(status_w, CHILD_STATUS_FD)
        os.set_inheritable(CHILD_STATUS_FD, True)
        _close_inherited_fds({CHILD_STATUS_FD})

        os.environ.update(env)
        os.umask(0o077)
        os.chdir(cwd)
        _apply_child_limits()

        # STUB (genetics-results-suite-4h6.43): the per-execution tokens are NOT delivered to
        # the child. Nothing writes the read-once token file and nothing points the SDK at
        # it, so a script's data calls carry whatever credential the image already has.
        # _deliver_tokens() in the supervisor is the seam.

        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(line_buffering=True, errors="replace")
            except Exception:
                pass

        try:
            compiled = compile(code, "<execution>", "exec")
            exec(compiled, {"__name__": "__main__"})
        except SystemExit as exc:
            # sys.exit(3) and friends. No uncaught exception, so no status record: the
            # supervisor reports NonZeroExit with the code, per the contract's reserved name.
            exit_code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        except BaseException as exc:
            tb = "".join(_traceback.format_exception(type(exc), exc, exc.__traceback__))
            os.write(CHILD_STATUS_FD, _child_status(type(exc).__name__, str(exc), tb))
            exit_code = 1
    except BaseException as exc:  # a failure in the child's own setup, not in the script
        try:
            os.write(CHILD_STATUS_FD, _child_status(ERR_STARTUP_FAILURE, str(exc), None))
        except OSError:
            pass
        exit_code = 70
    finally:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(exit_code if isinstance(exit_code, int) and 0 <= exit_code < 256 else 1)


def _drain(fd, limit, reaped=None, grace=DRAIN_GRACE_S, poll=0.2):
    """Read a pipe until EOF or until `grace` seconds after `reaped` is set.

    Past `limit` bytes it KEEPS READING and discards, so `total` stays an accurate count and
    the child is never blocked on a full pipe by this function. That is deliberate and is the
    safer of the two behaviours; 4h6.42 replaces the discarding with an 8 MiB stop that KILLS
    the child and a 64 KiB head-and-tail window in the response, and only then does status
    "limit" become reachable.

    THE DEADLINE IS NOT A DETAIL. The write ends are inherited by every descendant of the
    child, so a grandchild that `setsid()`s away holds the pipe open after the child is
    reaped and no amount of waiting produces EOF. Without a deadline the execution slot is
    held by a pipe read rather than by a process, which no kill-the-child bead can fix.
    `reaped` is set by the caller once waitpid has returned; after that this gives the
    already-buffered bytes `grace` seconds to arrive and then abandons the fd.

    Returns (bytes, total_seen, stopped_at_limit, abandoned).
    """
    chunks = []
    total = 0
    stopped = False
    deadline = None
    abandoned = False
    while True:
        if deadline is None and reaped is not None and reaped.is_set():
            deadline = time.monotonic() + grace
        wait = poll if deadline is None else max(0.0, min(poll, deadline - time.monotonic()))
        try:
            ready, _, _ = select.select([fd], [], [], wait)
        except InterruptedError:
            continue
        except OSError:
            break
        if not ready:
            if deadline is not None and time.monotonic() >= deadline:
                abandoned = True
                break
            continue
        try:
            block = os.read(fd, 65536)
        except OSError as exc:
            if exc.errno in (errno.EINTR, errno.EAGAIN):
                continue
            break
        if not block:
            break
        total += len(block)
        if not stopped:
            chunks.append(block)
            if sum(len(c) for c in chunks) >= limit:
                stopped = True
                chunks = [b"".join(chunks)[:limit]]
    return b"".join(chunks), total, stopped, abandoned


def _cap_output(raw):
    """STUB (genetics-results-suite-4h6.42). The contract wants the first 32 KiB, the marker
    `\\n...[<N> bytes elided]...\\n`, and the last 32 KiB. This returns the drained bytes
    decoded lossily and nothing else. The lossy decode IS contract behaviour and stays:
    invalid bytes become U+FFFD, there is no alternate encoding and no `encoding` field."""
    return raw.decode("utf-8", "replace")


# --------------------------------------------------------------------------------------
# The scheduler: one execution at a time, two waiting, bounded wait
# --------------------------------------------------------------------------------------


class Job:
    __slots__ = ("req", "conn", "enqueued_at", "pid", "pgid", "deadline", "dirs")

    def __init__(self, req, conn):
        self.req = req
        self.conn = conn
        self.enqueued_at = time.monotonic()
        self.pid = None
        self.pgid = None
        self.deadline = None
        self.dirs = None


def peer_gone(sock):
    """True when the client's connection has closed.

    Only ever consulted at dequeue, for a request that has NOT been forked. A RUNNING child
    is never killed on disconnect: it completes, is reaped, its manifest is written and its
    artifacts are retained, because killing it would destroy artifacts the retention window
    promises and because disconnect detection while nobody is reading the socket is
    unreliable enough that a false positive would kill live executions.
    """
    if sock is None:
        return False
    try:
        ready, _, _ = select.select([sock], [], [], 0)
        if not ready:
            return False
        return sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True


class Supervisor:
    """Owns the queue, the /scratch root and the fork. One instance per process."""

    def __init__(self, scratch_root, ready=False):
        self.scratch_root = scratch_root
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._waiting = deque()
        self._running = None
        self._pending_ids = set()
        self._retained_ids = set()
        self.ready = ready
        self.draining = False

    # -- state a request handler asks about -------------------------------------------

    def health(self):
        with self._lock:
            queued = len(self._waiting)
            busy = self._running is not None
        if self.draining:
            status = "draining"
        elif not self.ready:
            status = "starting"
        else:
            status = "ok"
        # A busy supervisor is healthy: 503 here would drop the pod out of the Service
        # endpoints mid-execution, and with one replica every retry then fails against no
        # endpoint at all.
        code = 200 if status == "ok" else 503
        return code, {"status": status, "busy": busy, "queued": queued}

    def begin_drain(self):
        self.draining = True
        with self._cv:
            self._cv.notify_all()

    def idle(self):
        with self._lock:
            return self._running is None and not self._waiting

    # -- the queue ---------------------------------------------------------------------

    def _dir_exists(self, execution_id):
        return os.path.exists(os.path.join(self.scratch_root, execution_id))

    def _admit(self, job):
        """Enqueue, or raise. Depth is WAITING requests and does not count the running one."""
        eid = job.req.execution_id
        with self._cv:
            if self.draining or not self.ready:
                raise RequestError(503, "NotReady", "supervisor is not accepting executions")
            if eid in self._pending_ids or eid in self._retained_ids or self._dir_exists(eid):
                # One execution_id names exactly one directory, one manifest and one audit
                # trail. Reusing merges two runs into a manifest chat-backend already
                # recorded; wiping deletes artifacts read_artifact may still be serving.
                raise RequestError(
                    409, "DuplicateExecutionId", "execution_id names a live or retained execution"
                )
            if len(self._waiting) >= QUEUE_DEPTH:
                raise RequestError(
                    429, "Busy", "queue is full", retry_after=RETRY_AFTER_S
                )
            self._pending_ids.add(eid)
            self._waiting.append(job)
            self._cv.notify_all()

    def _await_slot(self, job):
        """Block until this job owns the only execution slot, or raise 429 / 503."""
        deadline = job.enqueued_at + MAX_QUEUED_WAIT_S
        with self._cv:
            while True:
                if self._running is None and self._waiting and self._waiting[0] is job:
                    self._waiting.popleft()
                    self._running = job
                    return
                if self.draining:
                    self._forget(job)
                    raise RequestError(503, "NotReady", "supervisor is draining")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._forget(job)
                    raise RequestError(
                        429, "Busy", "maximum queued wait exceeded", retry_after=RETRY_AFTER_S
                    )
                self._cv.wait(remaining)

    def _forget(self, job):
        """Caller holds the lock. Drop a job that never reached a fork."""
        try:
            self._waiting.remove(job)
        except ValueError:
            pass
        self._pending_ids.discard(job.req.execution_id)
        self._cv.notify_all()

    def _release(self, job, retain):
        with self._cv:
            if self._running is job:
                self._running = None
            self._pending_ids.discard(job.req.execution_id)
            if retain:
                self._retained_ids.add(job.req.execution_id)
            self._cv.notify_all()

    # -- the whole request path --------------------------------------------------------

    def run(self, job):
        """Queue, dequeue, fork, reap, answer. Raises RequestError or ClientGone."""
        self._admit(job)
        self._await_slot(job)
        retain = False
        try:
            # The directory is created at DEQUEUE, not at accept, so a 400/429/409 leaves
            # nothing behind and the 429 retry may safely carry a fresh id.
            if peer_gone(job.conn):
                LOG.info("dropping queued execution %s: client gone", job.req.execution_id)
                raise ClientGone()
            exp = job.req.exp
            if exp is not None and exp <= time.time():
                raise RequestError(409, "TokenExpired", "tokens expired while queued")
            job.dirs = ExecutionDirs(self.scratch_root, job.req.execution_id)
            try:
                job.dirs.create()
            except FileExistsError:
                raise RequestError(
                    409, "DuplicateExecutionId", "execution_id names a live or retained execution"
                )
            retain = True
            return self._execute(job)
        finally:
            self._release(job, retain)

    def _execute(self, job):
        dirs = job.dirs
        seed_mplconfig(dirs.mplconfig)
        _deliver_tokens(job)

        out_r, out_w = os.pipe()
        st_r, st_w = os.pipe()
        env = dirs.child_env(job.req.claims[TOKEN_AUDIENCES[0]])
        code = job.req.code

        sys.stdout.flush()
        sys.stderr.flush()
        started = time.monotonic()
        pid = os.fork()
        if pid == 0:
            _child_main(code, env, dirs.tmp, out_w, st_w)
            os._exit(70)  # unreachable; _child_main never returns
        os.close(out_w)
        os.close(st_w)
        job.pid = pid
        job.pgid = pid  # the child calls setsid(), so its pgid is its own pid
        job.deadline = started + job.req.timeout_s
        _apply_limits(job)

        out_box = {}
        st_box = {}
        reaped = threading.Event()
        fields = ("raw", "total", "stopped", "abandoned")
        t_out = threading.Thread(
            target=lambda: out_box.update(
                zip(fields, _drain(out_r, _STUB_DRAIN_LIMIT_BYTES, reaped))),
            daemon=True,
        )
        t_st = threading.Thread(
            target=lambda: st_box.update(
                zip(fields, _drain(st_r, _STATUS_READ_LIMIT_BYTES, reaped))),
            daemon=True,
        )
        t_out.start()
        t_st.start()
        try:
            _, wait_status = os.waitpid(pid, 0)
            # The child's own lifetime, measured before the drain. Timing the drain instead
            # reports a number no process spent running whenever a descendant escapes.
            duration_ms = int((time.monotonic() - started) * 1000)
        finally:
            reaped.set()
            t_out.join(DRAIN_GRACE_S + 5.0)
            t_st.join(DRAIN_GRACE_S + 5.0)
            # Closing an fd another thread is blocked on is undefined, so a thread that
            # somehow outlived its own deadline costs two leaked descriptors rather than a
            # read against a reused number. _drain always returns within `grace`, so this
            # branch is a backstop and is logged loudly if it ever fires.
            for name, thread, fd in (("stdout", t_out, out_r), ("status", t_st, st_r)):
                if thread.is_alive():
                    LOG.error("%s drain thread for %s did not stop; leaking its read end",
                              name, job.req.execution_id)
                else:
                    os.close(fd)
        if out_box.get("abandoned") or st_box.get("abandoned"):
            # Not an error for this response: the child is reaped and its answer is complete.
            # It does mean something escaped the child's process group and still holds the
            # write end. Nothing here kills it (4h6.41 owns that); freeing the slot is what
            # matters, and by this point it is free.
            LOG.warning(
                "execution %s: a descendant outlived the child and still holds the output "
                "pipe; drain abandoned after %.1fs", job.req.execution_id, DRAIN_GRACE_S)

        exit_code = os.WEXITSTATUS(wait_status) if os.WIFEXITED(wait_status) else None
        sig = os.WTERMSIG(wait_status) if os.WIFSIGNALED(wait_status) else None

        artifacts, omitted = build_manifest(dirs.artifacts)
        _retain(job)

        return self._response(
            job,
            exit_code=exit_code,
            signal_=sig,
            duration_ms=duration_ms,
            out_raw=out_box.get("raw", b""),
            out_total=out_box.get("total", 0),
            out_stopped=out_box.get("stopped", False),
            status_raw=st_box.get("raw", b""),
            artifacts=artifacts,
            artifacts_omitted=omitted,
        )

    def _response(
        self,
        job,
        exit_code,
        signal_,
        duration_ms,
        out_raw,
        out_total,
        out_stopped,
        status_raw,
        artifacts,
        artifacts_omitted,
    ):
        child_error = None
        if status_raw:
            try:
                parsed = json.loads(status_raw.decode("utf-8", "replace"))
                if isinstance(parsed, dict) and isinstance(parsed.get("type"), str):
                    child_error = parsed
            except Exception:
                child_error = None  # untrusted input; a malformed record is simply absent

        if exit_code == 0 and signal_ is None:
            # THE SUPERVISOR'S OWN OBSERVATION WINS. The status pipe is fd 3 in a child that
            # is forked and not exec'd, so the script can write to it: without this a script
            # can forge {"type": ...} and then exit 0, turning a successful run into
            # status "error" with exit_code 0 — a row the contract's status table says cannot
            # exist, and a lie the model is told about its own analysis. An uncaught exception
            # always leaves a non-zero exit, so no legitimate record is lost here. The record
            # stays untrusted input everywhere else and is still re-capped below.
            status = "ok"
            error = None
            if child_error is not None:
                LOG.warning(
                    "execution %s wrote a status record and exited 0; ignoring it",
                    job.req.execution_id)
        elif child_error is not None:
            status = "error"
            error = {
                "type": child_error["type"],
                "message": str(child_error.get("message") or "")[:MESSAGE_MAX_BYTES],
                "traceback": (str(child_error["traceback"])[-TRACEBACK_MAX_BYTES:]
                              if child_error.get("traceback") else None),
                "limit": None,
            }
        elif signal_ is not None:
            # STUB (4h6.41 / 4h6.42): with no wall clock and no pipe cap in this build, a
            # signalled child is never the supervisor's own doing, so it is an "error" and
            # never "timeout" or "limit". Both of those become reachable when those beads
            # land, and each must set status and error.limit itself.
            status = "error"
            error = {
                "type": ERR_KILLED,
                "message": f"child killed by signal {signal_}",
                "traceback": None,
                "limit": None,
            }
        else:
            status = "error"
            error = {
                "type": ERR_NON_ZERO_EXIT,
                "message": f"child exited with status {exit_code}",
                "traceback": None,
                "limit": None,
            }

        return {
            "execution_id": job.req.execution_id,
            "status": status,
            "exit_code": exit_code,
            "signal": signal_,
            "duration_ms": duration_ms,
            "output": _cap_output(out_raw),
            "output_bytes": out_total,
            "output_truncated": bool(out_stopped),
            "error": error,
            "artifacts": artifacts,
            "artifacts_omitted": artifacts_omitted,
        }


# --------------------------------------------------------------------------------------
# Seams for the sibling beads. Each is a no-op today. All but one are already CALLED from
# the place their owner needs them, so landing those beads is an edit inside the seam and
# not a rewrite of _execute. `_audit_pipe` is the exception and says so in its own docstring:
# it is a placeholder for a shape, not a drop-in, because an fd cannot be handed to a child
# from a function that runs after the fork.
# --------------------------------------------------------------------------------------


def _apply_limits(job):
    """STUB (genetics-results-suite-4h6.41), PARENT SIDE. Runs after the fork.

    Owed here: the wall clock (SIGTERM job.pgid at `job.deadline`, SIGKILL after
    KILL_GRACE_S, answer 200 with status "timeout"), pid policing by watching job.pgid —
    RLIMIT_NPROC is per real uid and the two processes share one, so it is advisory only —
    and RAISING the child's /proc/<pid>/oom_score_adj to +500, which a parent may do because
    raising is unprivileged.

    NOT owed here, and not possible here: RLIMIT_AS on the child (setrlimit on another
    process needs CAP_SYS_RESOURCE, which the pod drops) and LOWERING the supervisor's own
    oom_score_adj to -500 (same capability). The first belongs in _apply_child_limits; the
    second is a pod-spec change, not a runtime one.

    This must not block: _execute is inside the only execution slot and the caller returns
    straight into waitpid, so the wall clock is a timer or a watchdog thread that signals
    job.pgid and lets waitpid observe the result. `killpg` on an already-exited group raises
    ESRCH and that is the normal case, not an error. The reap does not end the execution by
    itself — _drain's deadline does — so a group that escapes still gets signalled and the
    slot is freed either way.

    job.pid, job.pgid and job.deadline are already set when this is called.
    """
    return None


def _apply_child_limits():
    """STUB (genetics-results-suite-4h6.41), CHILD SIDE. Runs in the child before the script.

    Owed: RLIMIT_AS leaving the supervisor headroom under the 3Gi cgroup limit, and the
    child's own oom_score_adj (+500 — a raise, so no capability is needed). Both have to be
    set from inside the process they apply to, which is why this is a separate seam from
    _apply_limits and why it takes no `job`: nothing in the parent's Job object is safe to
    reach for across the fork.
    """
    return None


def _deliver_tokens(job):
    """STUB (genetics-results-suite-4h6.43).

    Owed: write job.req.tokens into a read-once file inside job.dirs.base and point the SDK
    at it, then never keep them after the child is reaped. Under the shared-uid model their
    protection is LIFETIME, not permissions — the SDK reads once and unlinks — because
    mode 0600 does not exclude a same-uid child. No chown: the pod holds no CAP_CHOWN.
    """
    return None


def _audit_pipe(job):
    """STUB (genetics-results-suite-4h6.45). NOT CALLED, and not a drop-in — read this first.

    Owed: a second pipe whose read end the supervisor holds, its number in the child's
    GENETICS_SDK_AUDIT_FD; rate, byte and per-line caps applied ON THE READ END; the child's
    framing re-parsed and re-framed as untrusted input; `[user=…] [session=…] [execution=…]`
    stamped from the TOKENS' sub/sid/jti and never from the body or from the child; and the
    result forwarded to the pod's own stdout, the only stream the logging agent collects.

    Unlike the other seams this one cannot be filled in place. An fd reaches the child only
    by existing before the fork, so 4h6.45 must edit code outside this function:

      * _execute: create the pipe BEFORE `os.fork()`, pass the write end into _child_main
        alongside out_w/st_w, close it in the parent immediately after the fork, and drain
        the read end on a third thread that shares the `reaped` event and DRAIN_GRACE_S
        deadline the other two use — the audit pipe is inherited by escaped descendants
        exactly as the output pipe is.
      * _child_main: dup the write end onto a fixed number the way status_w is dup'd onto
        CHILD_STATUS_FD, and add that number to the `_close_inherited_fds({...})` keep-set.
        A number missing from that set is closed a few lines later and the SDK writes to a
        closed fd.
      * ExecutionDirs.child_env: export the fd number as GENETICS_SDK_AUDIT_FD, next to the
        three SANDBOX_* identity variables the SDK's prefix already reads.

    Keeping this function means keeping the parent-side reader/re-stamper here; the fd
    plumbing above is not something it can own.
    """
    return None


def _retain(job):
    """STUB (genetics-results-suite-4h6.46).

    Owed: per-execution artifact (64Mi) and total /scratch quotas polled during the run, an
    aggregate retained ceiling with oldest-first eviction, deletion of everything under
    /scratch/<id> EXCEPT artifacts/ on completion, and the 15-minute reaper. Until it lands
    nothing is ever deleted after completion, so /scratch grows until the pod restarts and
    the emptyDir sizeLimit is defended by nothing.
    """
    return None


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "genetics-sandbox-supervisor"
    sys_version = ""  # do not disclose the interpreter version to a caller

    # -- plumbing ----------------------------------------------------------------------

    def log_message(self, fmt, *args):
        LOG.info("%s %s", self.address_string(), fmt % args)

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler's default is an HTML page; every non-2xx here is the
        # contract's uniform JSON object instead. This catches 501 (unknown method) and the
        # request-line failures too.
        self._send_json(
            code,
            {"execution_id": None, "error": {"type": _default_error_type(code), "message": message or ""}},
        )

    def _send_json(self, code, payload, extra_headers=()):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra_headers:
            self.send_header(key, value)
        self.end_headers()
        if self.command == "HEAD":
            return  # a HEAD response carries the headers and no body, or keep-alive desyncs
        try:
            self.wfile.write(body)
        except OSError:
            pass  # the client went away between the fork and the answer; nothing to do

    def _send_request_error(self, exc, execution_id=None):
        headers = ()
        if exc.retry_after is not None:
            headers = (("Retry-After", str(exc.retry_after)),)
        self._send_json(
            exc.status,
            {"execution_id": execution_id, "error": {"type": exc.type, "message": exc.message}},
            headers,
        )

    # -- routing -----------------------------------------------------------------------

    def _route(self):
        return self.path.split("?", 1)[0]

    def do_GET(self):
        path = self._route()
        if path == "/health":
            code, payload = SUPERVISOR.health()
            # The single route exempt from the uniform error shape: the probe reads only the
            # status code, and a client polling for recovery wants busy/queued in the 503 as
            # much as in the 200.
            self._send_json(code, payload)
        elif path == "/execute":
            self._send_request_error(RequestError(405, "MethodNotAllowed", "use POST"))
        else:
            self._send_request_error(RequestError(404, "NotFound", "no such route"))

    def do_POST(self):
        path = self._route()
        if path == "/health":
            self._send_request_error(RequestError(405, "MethodNotAllowed", "use GET"))
            return
        if path != "/execute":
            self._send_request_error(RequestError(404, "NotFound", "no such route"))
            return
        self._execute()

    def _method_not_allowed(self):
        path = self._route()
        if path in ("/health", "/execute"):
            self._send_request_error(RequestError(405, "MethodNotAllowed", "unsupported method"))
        else:
            self._send_request_error(RequestError(404, "NotFound", "no such route"))

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _method_not_allowed

    # -- POST /execute ------------------------------------------------------------------

    def _read_body(self, started):
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self.close_connection = True
            raise RequestError(415, "UnsupportedMediaType", "expected application/json")

        encoding = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if encoding and encoding != "identity":
            # Not in the contract because the contract assumes a Content-Length. Refusing is
            # the safe reading: a chunked body cannot be size-capped before it is read, which
            # is the one thing the 1 MiB cap exists to do.
            self.close_connection = True
            raise _bad("chunked request bodies are not accepted")

        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            self.close_connection = True
            raise _bad("Content-Length is required")
        try:
            length = int(raw_len)
        except ValueError:
            self.close_connection = True
            raise _bad("Content-Length is not an integer")
        if length < 0:
            self.close_connection = True
            raise _bad("Content-Length is negative")
        if length > MAX_BODY_BYTES:
            # Stop at the cap rather than buffering past it: the bytes are never read.
            self.close_connection = True
            raise RequestError(413, "PayloadTooLarge", "body exceeds 1 MiB")

        chunks = []
        remaining = length
        while remaining > 0:
            budget = BODY_READ_TIMEOUT_S - (time.monotonic() - started)
            if budget <= 0:
                self.close_connection = True
                raise RequestError(408, "RequestTimeout", "request body not received in time")
            self.connection.settimeout(budget)
            try:
                block = self.rfile.read(min(remaining, 65536))
            except (socket.timeout, TimeoutError):
                self.close_connection = True
                raise RequestError(408, "RequestTimeout", "request body not received in time")
            finally:
                self.connection.settimeout(None)
            if not block:
                self.close_connection = True
                raise _bad("request body shorter than Content-Length")
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)

    def _execute(self):
        started = time.monotonic()
        execution_id = None
        try:
            raw = self._read_body(started)
            req = parse_execute_request(raw)
            execution_id = req.execution_id
            job = Job(req, self.connection)
            result = SUPERVISOR.run(job)
        except RequestError as exc:
            # 400 and 500 bodies never echo the payload and never carry a path or traceback.
            self._send_request_error(exc, execution_id)
            return
        except ClientGone:
            self.close_connection = True
            return
        except Exception:
            LOG.exception("supervisor failure handling /execute")
            self._send_request_error(
                RequestError(500, "InternalError", "supervisor failure"), execution_id
            )
            return
        self._send_json(200, result)


def _default_error_type(code):
    return {
        400: "InvalidRequest",
        404: "NotFound",
        405: "MethodNotAllowed",
        408: "RequestTimeout",
        413: "PayloadTooLarge",
        414: "PayloadTooLarge",
        415: "UnsupportedMediaType",
        429: "Busy",
        431: "PayloadTooLarge",
        501: "MethodNotAllowed",
        503: "NotReady",
    }.get(code, "InternalError")


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # Queued requests park a handler thread on a condition variable, and /health must answer
    # while an execution is in flight, so the server has to be threaded. Concurrency 1 is
    # enforced by the Supervisor's slot, never by the HTTP layer.


SUPERVISOR = None  # set by main(); the handler class has no other way to reach it


# --------------------------------------------------------------------------------------
# Startup
# --------------------------------------------------------------------------------------


def _scratch_root():
    override = os.environ.get(ENV_SCRATCH_ROOT)
    if override:
        # Loud on purpose. read_artifact (4h6.15) refuses any artifacts directory that does
        # not resolve under a HARDCODED /scratch/ prefix, so an override makes every artifact
        # unretrievable through the real path. It exists for tests, not for deployment.
        LOG.warning(
            "%s=%s overrides the /scratch root. read_artifact's hardcoded /scratch/ prefix "
            "makes artifacts written here unretrievable; this is a TEST-ONLY setting.",
            ENV_SCRATCH_ROOT,
            override,
        )
        return override
    return DEFAULT_SCRATCH_ROOT


def create(scratch_root=None):
    """A Supervisor that is bound but NOT ready: /health answers 503 "starting" and
    /execute answers 503 NotReady. Nothing is forked and no directory is touched yet."""
    global SUPERVISOR
    SUPERVISOR = Supervisor(scratch_root or _scratch_root(), ready=False)
    return SUPERVISOR


def bring_up(supervisor, run_assertions=True):
    """Assertions, scratch wipe, prewarm; then mark the supervisor ready.

    Order matters and is contractual: the assertions and prewarm() happen BEFORE the first
    fork and before anything is accepted, and prewarm needs a writable MPLCONFIGDIR to exist
    first because on matplotlib 3.10 an unwritable one raises rather than falling back.
    """
    root = supervisor.scratch_root

    if run_assertions:
        assert_nsswitch_hosts_files_first()

    os.makedirs(root, mode=0o700, exist_ok=True)
    wipe_unrecognised_scratch(root)

    # The supervisor's own writable MPLCONFIGDIR, needed only so that prewarm() can import
    # matplotlib.pyplot. Every EXECUTION gets its own, seeded the same way; this one is not
    # shared with any child and is not an execution directory, which is why the startup wipe
    # keeps it by name.
    sup_dir = os.path.join(root, SUPERVISOR_DIR_NAME)
    mpl_dir = os.path.join(sup_dir, "mplconfig")
    shutil.rmtree(sup_dir, ignore_errors=True)
    os.makedirs(mpl_dir, mode=0o700)
    seed_mplconfig(mpl_dir)
    os.environ["MPLCONFIGDIR"] = mpl_dir

    module = load_prewarm()
    if module is None:
        LOG.warning(
            "%s is unset: prewarm() SKIPPED. sandbox/Dockerfile always sets it, so this is a "
            "development run outside the image — the analysis modules are not pre-imported "
            "and their absence will surface inside the child instead of crashing the pod.",
            ENV_PREWARM,
        )
    else:
        # PrewarmError is deliberately NOT caught. A pod that answers health checks and then
        # fails every plotting script is worse than one that crash-loops visibly.
        module.prewarm()
        LOG.info("prewarm complete")

    supervisor.ready = True
    return supervisor


def start(scratch_root=None, run_assertions=True):
    """create() + bring_up(). The one-call form, used by tests."""
    return bring_up(create(scratch_root), run_assertions=run_assertions)


def main(argv=None):
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [supervisor] %(message)s",
    )
    # Bind and serve BEFORE the startup work, so that `status: "starting"` is observable
    # rather than theoretical: a probe arriving during prewarm gets the contract's 503 with a
    # health body instead of a connection refusal. Nothing can be executed while not ready —
    # /execute answers 503 NotReady — so this widens what is visible, not what is allowed.
    supervisor = create()
    httpd = _Server((LISTEN_HOST, LISTEN_PORT), _Handler)

    def _terminate(_signum, _frame):
        # SIGTERM: stop accepting (503 NotReady) and let the in-flight child finish inside
        # terminationGracePeriodSeconds. Never kill it — its artifacts are promised for 15
        # minutes and its response may still be deliverable.
        LOG.warning("SIGTERM: draining")
        supervisor.begin_drain()
        threading.Thread(target=_shutdown_when_idle, args=(httpd, supervisor), daemon=True).start()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    LOG.info("listening on %s:%d, scratch root %s", LISTEN_HOST, LISTEN_PORT, supervisor.scratch_root)
    serving = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2},
                               daemon=True)
    serving.start()
    try:
        bring_up(supervisor)
    except BaseException:
        # A failed assertion or a PrewarmError must crash the pod, visibly, rather than leave
        # it answering health checks and failing every script inside the child.
        LOG.exception("startup failed; refusing to serve")
        httpd.shutdown()
        httpd.server_close()
        return 1
    LOG.info("ready")
    serving.join()
    httpd.server_close()
    return 0


def _shutdown_when_idle(httpd, supervisor, poll=0.25):
    while not supervisor.idle():
        time.sleep(poll)
    httpd.shutdown()


if __name__ == "__main__":
    sys.exit(main())
