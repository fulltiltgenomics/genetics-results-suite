#!/usr/bin/env python3
"""The process the sandbox pod runs: HTTP front door, one-at-a-time queue, fork/reap.

Design of record: docs/code-execution-security.md. The wire shape this file implements is
section 2's "The HTTP contract between chat-backend and the supervisor" (4h6.38) and that
subsection is the ONLY definition of it — chat-backend's client (4h6.47) cannot import
anything from here, because the image pip-installs only the genetics SDK's import closure
and prune_venv.py deletes the rest. Every constant below that names a wire value carries a
pointer to the row it comes from; do not change one without changing the other.

This file started as genetics-results-suite-4h6.39 (the skeleton). 4h6.41, 4h6.42, 4h6.43 and
4h6.46 have since filled four of its five holes:

  4h6.41  wall clock, pid budget, group kill (parent)  -> _apply_limits, _watchdog, _kill_group
          RLIMIT_AS (child)                            -> _apply_child_limits
  4h6.42  the 8 MiB pipe cap and the 64 KiB head+tail return  -> _drain, _cap_output
  4h6.43  per-execution token delivery by read-once file      -> _deliver_tokens
  4h6.46  /scratch sub-quotas, artifact retention, the reaper -> _watchdog, _retain, reap_expired
          the budget arithmetic is stated ONCE, above ARTIFACT_QUOTA_BYTES, and is mirrored in
          docs/code-execution-security.md's "4h6.46" table; do not restate it a second time

ONE HOLE REMAINS, marked in place with `STUB (genetics-results-suite-4h6.45)`: the SDK audit
stream is neither read, capped, re-framed, stamped nor forwarded, so the SDK's records reach
the child's own stdout pipe (where they are indistinguishable from script output and are
subject to the same 64 KiB window) and nothing reaches the pod's stdout, which is the only
stream the cluster's logging agent collects.

WHAT THE CONTROLS IN THIS FILE DO NOT CONTAIN, stated because the opposite reading is the
dangerous one. Every kill path signals the child's PROCESS GROUP. A descendant that calls
`setsid()` LEAVES that group and no signal here reaches it: it was MEASURED surviving both
`killpg(SIGTERM)` and `killpg(SIGKILL)`, which returned ESRCH while it kept running. The
group kill handles the ordinary case — a script's `subprocess` children — and nothing more.
Containing an escapee is genetics-results-suite-4h6.55's (a PID namespace per execution), and
until it lands an escaped process shares the pod with the next user. The drain deadline
(4h6.39, DRAIN_GRACE_S) is what keeps such a process from also holding the execution slot;
do not remove it, and do not read "the child was reaped" as "the execution is over".

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

# -- 4h6.42, the two output bounds. They are DIFFERENT limits and conflating them loses one.
PIPE_CAP_BYTES = 8 * 1024 * 1024      # stop reading the child's pipe here AND kill the group
RETURN_HEAD_BYTES = 32 * 1024         # first 32 KiB returned to the model
RETURN_TAIL_BYTES = 32 * 1024         # last 32 KiB — the traceback lives here
ELISION_MARKER = "\n...[{} bytes elided]...\n"   # fixed text; the 64 KiB budget EXCLUDES it

# -- 4h6.41, the memory bound.
#
# SIZED AGAINST THE POD'S BUDGET, NOT THIS CONTAINER'S. k8s/deployments/sandbox.yaml sets
# `limits.memory: 3Gi`, and that number is deliberately NOT read from /sys/fs/cgroup: under
# the local Docker backend /scratch is a tmpfs whose page cache is charged to the SAME memory
# cgroup (measured by 4h6.40: 113 MiB -> 414 MiB after a 300 MiB write), while in the pod
# /scratch is a node-disk-backed emptyDir charged to ephemeral-storage (1Gi/2Gi) and NEVER to
# limits.memory. Tuning this to the local behaviour would be up to 512 MiB more conservative
# than the pod needs. The consequence, stated because it is a real local/pod divergence and
# not a rounding error: a script holding ~2.4 GiB while /scratch holds 400 MiB can be
# cgroup-OOM-killed HERE and run fine in the pod.
POD_MEMORY_LIMIT_BYTES = 3 * 1024 * 1024 * 1024
SUPERVISOR_MEMORY_HEADROOM_BYTES = 512 * 1024 * 1024
CHILD_RLIMIT_AS_BYTES = POD_MEMORY_LIMIT_BYTES - SUPERVISOR_MEMORY_HEADROOM_BYTES  # 2560 MiB

# RLIMIT_AS bounds VIRTUAL address space, not RSS, and the prewarmed child does not start
# from zero: MEASURED inside the image, a child that has inherited prewarm()'s numpy/scipy/
# polars/matplotlib mappings already has VmSize ~1358 MiB against VmRSS ~113 MiB, because BLAS
# reserves far more than it touches. So the script's own allocation headroom under this limit
# is ~1.2 GiB, not ~2.5 GiB. Raising the limit to "fix" that would spend the supervisor's
# headroom, which is the one thing keeping the cgroup OOM killer from choosing between them.
CHILD_OOM_SCORE_ADJ = 500

# The pid budget is a SUPERVISOR-SIDE WATCH, not RLIMIT_NPROC: that limit is per real uid
# across the pid namespace and supervisor and child share uid 65532 (option (b), 4h6.7), so a
# child forking to its RLIMIT_NPROC also stops the SUPERVISOR forking — the fork bomb takes
# out the supervisor instead of being contained. Sized from what a legitimate script needs
# (tens of processes) and far below the kubelet's pod_pids_limit of 1024, which is the outer
# backstop and not a substitute.
PID_BUDGET = 32

# One thread polls the wall clock, the group size, the /scratch quotas and the aggregate.
# 0.2s is chosen against the WALL CLOCK, which is the tightest of the four in the only sense
# that matters: it is the one bound a client is told the exact value of, and MAX_QUEUED_WAIT_S
# (120s) means every second the slot is held past it is a second the next two callers spend
# queued or 429ing. So the deadline is checked on its own timer, before and after the
# filesystem scan and never behind it, and the wait shrinks as the deadline approaches — see
# _watchdog. The quota overshoot is the LOOSEST of the four and is not what sizes this: at
# ~1 GiB/s to tmpfs a poll can miss ~200 MiB, and no poll interval anybody would run makes
# that small. What bounds the retained footprint is _retain's trim, not this number; what
# keeps the volume under its sizeLimit during a run is the aggregate check below.
WATCHDOG_POLL_S = 0.2

# -- 4h6.46, THE /scratch BUDGET. Stated once, here, and nowhere else in this file.
#
# The emptyDir sizeLimit is 512Mi COMBINED artifact-plus-temp for every live and retained
# execution, and exceeding it EVICTS THE POD rather than failing the write.
#
#     RETAINED_ARTIFACTS_CEILING   256Mi   steady state, EXACT: every retained artifacts/ has
#                                          been trimmed to ARTIFACT_QUOTA by _retain, so the
#                                          ceiling is enforced over measured, bounded sizes
#   + EXECUTION_TOTAL_QUOTA        192Mi   the one live execution
#   = 448Mi
#   <= SCRATCH_AGGREGATE_CEILING   480Mi   = 512Mi - 32Mi reserved for .supervisor and for
#                                          filesystem overhead the per-tree walks do not see
#
# 448 <= 480 is what makes the aggregate check a BACKSTOP rather than a second quota: the two
# per-part budgets cannot together reach it, so it only ever fires on overshoot.
#
# WHAT THIS ARITHMETIC DOES NOT PROVE, stated because the opposite reading is the dangerous
# one. The 32Mi reserve is a margin, not a proof. A poll can miss ~200 MiB of writes, and a
# child that traps SIGTERM keeps writing for KILL_GRACE_S (2s) after a quota fires. Neither is
# bounded by 32Mi and no arrangement of these constants would bound them; what bounds them is
# how fast the writer is stopped (SIGTERM immediately, SIGKILL 2s later) and, afterwards,
# _retain deleting or trimming everything the overshoot produced. The honest claim is
# therefore: the STEADY STATE is exact and sits 64Mi under the cliff; the TRANSIENT PEAK
# during a hostile burst is not, and the aggregate check is what fires 32Mi before the cliff
# instead of letting the kubelet be the thing that notices.
ARTIFACT_QUOTA_BYTES = 64 * 1024 * 1024
EXECUTION_TOTAL_QUOTA_BYTES = 192 * 1024 * 1024
RETAINED_ARTIFACTS_CEILING_BYTES = 256 * 1024 * 1024
SCRATCH_SIZE_LIMIT_BYTES = 512 * 1024 * 1024   # the emptyDir sizeLimit; the cliff itself
SCRATCH_SUPERVISOR_RESERVE_BYTES = 32 * 1024 * 1024
SCRATCH_AGGREGATE_CEILING_BYTES = SCRATCH_SIZE_LIMIT_BYTES - SCRATCH_SUPERVISOR_RESERVE_BYTES
RETENTION_S = 15 * 60
REAPER_POLL_S = 30.0

# ZERO-LENGTH FILES ARE NOT FREE, and charging only st_blocks says they are. MEASURED: 300,000
# empty files under artifacts/ charged 8.6 MB against the 192 MiB quota, so no limit fired,
# while producing a 19.8 MB response and taking the supervisor's RSS from 22 MB to 166 MB. The
# cost an empty file really imposes is an inode plus a directory entry — on the volume, in the
# manifest, in the response and in every subsequent scan — so every entry is charged a floor
# whether or not it holds a byte, and a SEPARATE entry budget bounds the walk itself. The two
# are not redundant: the floor makes the byte quota honest, the entry budget is what keeps a
# scan from costing seconds.
DIRENT_COST_BYTES = 512
ARTIFACT_ENTRY_BUDGET = 1024          # entries directly under artifacts/ AND the manifest cap
EXECUTION_ENTRY_BUDGET = 20000        # entries anywhere under /scratch/<id>; also the scan cap

# CLEANUP AND ACCOUNTING MUST NOT BE BOUNDED BY THE BUDGET THEY EXIST TO RESTORE. A live
# scan stops at EXECUTION_ENTRY_BUDGET because past that point the exact count changes no
# decision — the tree is over the budget either way. The trim and the retained-size
# measurement are the opposite case: they run AFTER the kill, on a tree that is already over,
# and their whole job is to make the number true. Bounding them at 20 000 made them report a
# truncated sample as fact (see _trim_artifacts). They get a ceiling four million entries
# high instead: it exists only so a hostile or corrupted tree cannot make them unbounded, and
# it sits far above what a 512 MiB emptyDir can physically hold (an empty tmpfs file costs an
# inode plus a dentry, several hundred bytes, so ~1M entries fills the volume).
TRIM_SCAN_CHUNK = EXECUTION_ENTRY_BUDGET   # names one drain pass materialises
TRIM_ENTRY_CEILING = 4000000               # hard stop for the drain and for post-hoc sizing

# The response side of the same problem. MAX_BODY_BYTES bounds what comes IN and nothing
# bounded what goes out. Every component is separately capped (64 KiB output, 1024 manifest
# entries, a short error.type, 2 KiB message, 8 KiB traceback), so this is a backstop that
# should never fire; _cap_response degrades rather than sending an unbounded body.
MAX_RESPONSE_BYTES = 1024 * 1024

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

RESERVED_ERROR_TYPES = frozenset({
    ERR_TIMEOUT, ERR_MEMORY_LIMIT, ERR_PID_LIMIT, ERR_ARTIFACT_QUOTA, ERR_SCRATCH_QUOTA,
    ERR_OUTPUT_LIMIT, ERR_NON_ZERO_EXIT, ERR_KILLED, ERR_STARTUP_FAILURE,
})

# error.type's OTHER half is the child's exception class name, and the child is forked without
# exec, so the script writes that string. It therefore needs the same treatment `message` and
# `traceback` already get, and it was not getting any: MEASURED, a 60,000-character type
# reached the response, bypassing the 64 KiB output window entirely, and a child writing
# {"type": "Timeout"} produced error.type == "Timeout" with error.limit == null — a forged
# supervisor-reserved name a client is invited to branch on.
#
# THIS IS THE SAME DEFECT 4h6.47 FIXED ON THE OTHER SIDE OF THIS WIRE, where chat-backend's
# client applied _redact to `message` and not to `error_type`. Both halves of the wire had the
# same blind spot about the same field, which is why it is worth naming here rather than
# treating as a local oversight.
#
# A Python class name is an identifier; a dotted qualname is the widest legitimate shape. The
# pattern is deliberately narrower than "any string the child could name a class": a class
# whose __name__ is emoji or 300 characters long is reported as NonZeroExit rather than
# echoed, because the response is rendered to a model and this field is a free text channel
# out of the sandbox otherwise.
ERROR_TYPE_MAX_BYTES = 64
_ERROR_TYPE_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\Z")

# --------------------------------------------------------------------------------------
# Local-vs-pod knobs. Every one of these is set by sandbox/Dockerfile in the image, so an
# unset value means "not running in the sandbox image" and is answered with a loud warning
# rather than a silent behaviour change. 4h6.40 runs the real image in plain Docker; these
# exist for running supervisor.py straight out of a checkout, which the tests do.
# --------------------------------------------------------------------------------------

ENV_SCRATCH_ROOT = "SANDBOX_SCRATCH_ROOT"   # NOT set by the image; test-only override
ENV_MPLCACHE = "GENETICS_MPLCACHE"          # /genetics/mplcache
ENV_PREWARM = "GENETICS_PREWARM"            # /genetics/prewarm.py

# NOT set by the image and NOT set by k8s/deployments/sandbox.yaml. It exists so that the
# retention reaper can be OBSERVED firing in the real image inside a test run instead of
# fifteen minutes later, and it can only ever SHORTEN retention — a value above RETENTION_S is
# refused rather than clamped silently, because the failure it would cause (artifacts outliving
# what read_artifact was told) is worse than a startup error. Same standing as
# SANDBOX_SCRATCH_ROOT: loud on every start, test-only, never deployment configuration.
ENV_RETENTION_S = "SANDBOX_RETENTION_S"

# The name the supervisor writes the per-execution token file's path under, in the CHILD's
# environment only. genetics-results-suite-4h6.44 (the SDK side, a different repo) reads this
# variable, opens the file ONCE and unlinks it. Defined here because the supervisor writes it
# first; docs/code-execution-security.md section 4 is where the two halves are recorded.
ENV_TOKEN_FILE = "SANDBOX_TOKEN_FILE"
TOKEN_FILE_NAME = "tokens.json"

DEFAULT_SCRATCH_ROOT = "/scratch"
NSSWITCH_PATH = "/etc/nsswitch.conf"

# /scratch entries the startup wipe must not delete. Everything else under the root is
# removed: after a restart the supervisor holds no record of which executions were live or
# still retained, so nothing under /scratch belongs to one. A crash mid-execution must not
# leave a readable directory behind, and that is the whole point of the wipe.
SUPERVISOR_DIR_NAME = ".supervisor"

# The child writes at most one JSON object here and nothing else. See _child_main.
CHILD_STATUS_FD = 3

# The status pipe carries one small JSON object. Unlike the output pipe this bound does NOT
# kill: past it the reader keeps reading and discards, so a child cannot block on a full
# status pipe. A record longer than this is a child misbehaving, not a limit worth reporting.
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
        self.tokens = os.path.join(self.base, TOKEN_FILE_NAME)

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
            # 4h6.43. The PATH, never the tokens themselves: /proc/<pid>/environ is readable
            # by any process with the same uid and supervisor and child share uid 65532, so a
            # helper the script spawns could read a token straight out of a sibling's
            # environment. The path is not a secret. See _deliver_tokens for what this does
            # and — more importantly — what it does not bound.
            ENV_TOKEN_FILE: self.tokens,
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


def build_manifest(artifacts_dir, max_entries=ARTIFACT_ENTRY_BUDGET,
                   scan_limit=EXECUTION_ENTRY_BUDGET):
    """(entries, omitted). Lists a file only if it would survive read_artifact's checks.

    BOTH BOUNDS ARE LOAD-BEARING and they are different. `max_entries` bounds the RESPONSE:
    300,000 zero-length files produced a 19.8 MB JSON body for chat-backend to parse, from a
    /scratch tree that tripped no quota (MEASURED). `scan_limit` bounds THIS FUNCTION: it runs
    after the child is reaped, holding the execution slot, and os.listdir on that directory
    materialises every name before the first one is examined. Neither the count nor the walk
    may be a function of how many files the child chose to create.

    Past `scan_limit` the directory is not enumerated further and `omitted` becomes a FLOOR
    rather than an exact count. That is logged, because a silently-wrong count is worse than a
    number a reader knows is a lower bound. The watchdog's entry budget is what normally stops
    a tree ever reaching this size; getting here means it was starved.
    """
    entries = []
    omitted = 0
    try:
        dfd = os.open(artifacts_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return [], 0
    try:
        seen = 0
        # The DIRECTORY FD, not the path: os.listdir(dfd) was symlink-safe and a path-based
        # scandir would not be, so the O_NOFOLLOW open above would stop meaning anything.
        for name in _iter_dir_names(dfd, scan_limit):
            seen += 1
            if len(entries) >= max_entries:
                omitted += 1
                continue
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
    if seen >= scan_limit:
        LOG.error("artifacts/ holds at least %d entries; the manifest stopped enumerating and "
                  "artifacts_omitted is a floor, not a count", seen)
    entries.sort(key=lambda e: e["name"])
    return entries, omitted


def _iter_dir_names(path, limit):
    """At most `limit` names from `path` (a path or a directory fd), streamed.

    os.listdir builds the whole list before the caller sees the first name, which is the
    part that does not survive a child that made 300,000 of them.
    """
    try:
        it = os.scandir(path)
    except OSError:
        return
    with it:
        for count, entry in enumerate(it):
            if count >= limit:
                return
            yield entry.name


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


def _drain(fd, limit, reaped=None, grace=DRAIN_GRACE_S, poll=0.2, on_limit=None):
    """Read a pipe until EOF or until `grace` seconds after `reaped` is set.

    TWO BEHAVIOURS AT `limit`, selected by `on_limit`, and they are not interchangeable.

    * `on_limit` given (the OUTPUT pipe, 8 MiB — 4h6.42): reading STOPS at the cap and
      `on_limit()` is called, which kills the child's process group. Stopping is the point:
      the cap exists so that `while True: print(...)` cannot consume the SUPERVISOR'S memory
      or the pod's CPU before the wall clock fires, and a reader that drained and discarded
      the excess would answer `200 status:"ok"` while doing neither. `total` therefore stops
      at the cap too, which is what `output_bytes` means on the wire. A child blocked writing
      to the now-unread pipe still dies: a pipe write is an interruptible sleep, so SIGTERM's
      default disposition ends it, and SIGKILL follows KILL_GRACE_S later regardless.
    * `on_limit` absent (the STATUS pipe, 64 KiB): past the limit it KEEPS READING and
      discards, so a child writing a huge status record blocks on nothing and `total` stays
      an accurate count. Nothing is killed for it.

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
                if on_limit is not None:
                    total = min(total, limit)
                    try:
                        on_limit()
                    except Exception:
                        LOG.exception("output cap handler failed")
                    break
    return b"".join(chunks), total, stopped, abandoned


def _utf8_head(raw):
    """Trim up to 3 trailing bytes so `raw` does not end part-way through a character."""
    for i in range(len(raw) - 1, max(-1, len(raw) - 5), -1):
        byte = raw[i]
        if byte < 0x80:
            return raw                       # ends on ASCII: nothing is pending
        if byte >= 0xC0:                     # a lead byte: is its sequence complete?
            need = 4 if byte >= 0xF0 else 3 if byte >= 0xE0 else 2
            return raw if len(raw) - i >= need else raw[:i]
        # a continuation byte: step back looking for its lead
    return raw                               # no lead within reach: already invalid, leave it


def _utf8_tail(raw):
    """Drop leading continuation bytes so `raw` does not start mid-character."""
    i = 0
    while i < len(raw) and i < 3 and 0x80 <= raw[i] < 0xC0:
        i += 1
    return raw[i:]


def _cap_output(raw):
    """(text, truncated). The contract's 64 KiB head-and-tail window, applied to BYTES.

    Head AND tail, never head alone: the model needs the traceback and the traceback is at the
    tail, so a head-only truncation makes it debug against output it cannot see and burns
    another roundtrip. The marker between them is fixed text (`\\n...[<N> bytes elided]...\\n`,
    N in bytes) so a client recognises truncation without heuristics, and the 64 KiB budget is
    the head and tail ONLY — the marker is additional.

    THE CUT IS ON BYTE BOUNDARIES BUT NOT THROUGH A CHARACTER. The contract fixes the head and
    tail at exactly 32 KiB each rather than at 32 KiB minus half a marker; this trims at most
    3 bytes off each side so the split never bisects a multi-byte sequence, and counts what it
    trimmed into N. Whole bytes remain elided-and-counted, never silently dropped.

    The lossy decode is contract behaviour and stays: invalid bytes become U+FFFD, there is no
    alternate encoding and no `encoding` field. A script with binary to return writes an
    artifact.
    """
    if len(raw) <= RETURN_HEAD_BYTES + RETURN_TAIL_BYTES:
        return raw.decode("utf-8", "replace"), False
    head = _utf8_head(raw[:RETURN_HEAD_BYTES])
    tail = _utf8_tail(raw[len(raw) - RETURN_TAIL_BYTES:])
    elided = len(raw) - len(head) - len(tail)
    return (head.decode("utf-8", "replace")
            + ELISION_MARKER.format(elided)
            + tail.decode("utf-8", "replace")), True


def _sanitise_error_type(raw, exit_code, execution_id):
    """A child-supplied error.type the response may carry, or ERR_NON_ZERO_EXIT.

    THREE THINGS ARE REFUSED, and each was reachable from a script. A type longer than
    ERROR_TYPE_MAX_BYTES — 60,000 characters reached the response, a text channel out of the
    sandbox that bypasses the 64 KiB output window entirely and lands in a model's context. A
    type that is not an identifier or dotted qualname, which no real class name fails and no
    prose passes. And a SUPERVISOR-RESERVED name, which the contract invites clients to branch
    on: `{"type": "Timeout"}` from a child produced error.type "Timeout" with error.limit null,
    a shape only the supervisor is supposed to be able to emit.

    StartupFailure is the one reserved name a child legitimately writes, from _child_main's own
    setup handler — which exits 70 and cannot reach the script. So it is admitted on that exit
    code and refused on every other, rather than being lost or left forgeable.
    """
    if not isinstance(raw, str):
        return ERR_NON_ZERO_EXIT
    reason = None
    if len(raw.encode("utf-8", "replace")) > ERROR_TYPE_MAX_BYTES:
        reason = "over %d bytes" % ERROR_TYPE_MAX_BYTES
    elif not _ERROR_TYPE_RE.fullmatch(raw):
        reason = "not an identifier"
    elif raw in RESERVED_ERROR_TYPES and not (
            raw == ERR_STARTUP_FAILURE and exit_code == 70):
        reason = "a supervisor-reserved name"
    if reason is None:
        return raw
    LOG.warning("execution %s: refusing a child-supplied error.type (%s); reporting %s",
                execution_id, reason, ERR_NON_ZERO_EXIT)
    return ERR_NON_ZERO_EXIT


def _cap_response(payload):
    """Keep a response body under MAX_RESPONSE_BYTES, degrading in the order that loses least.

    A BACKSTOP, NOT A BOUND ANYTHING SHOULD REACH: output is capped at 64 KiB, the manifest at
    ARTIFACT_ENTRY_BUDGET entries, error.type/message/traceback at 64 B / 2 KiB / 8 KiB, so a
    well-formed response is ~100 KiB at most. It exists because MAX_BODY_BYTES bounded what
    came IN and nothing bounded what went out, and every component cap above was added after
    something was measured getting past the ones before it.

    Artifacts go first and their count survives in artifacts_omitted, because a name the model
    cannot see is recoverable (it can list again) while output it never sees is not.
    """
    body = json.dumps(payload).encode("utf-8")
    if len(body) <= MAX_RESPONSE_BYTES:
        return payload, body
    trimmed = dict(payload)
    dropped = len(trimmed.get("artifacts") or [])
    if dropped:
        trimmed["artifacts"] = []
        trimmed["artifacts_omitted"] = trimmed.get("artifacts_omitted", 0) + dropped
        body = json.dumps(trimmed).encode("utf-8")
    if len(body) > MAX_RESPONSE_BYTES and "output" in trimmed:
        trimmed["output"] = ""
        trimmed["output_truncated"] = True
        if isinstance(trimmed.get("error"), dict):
            trimmed["error"] = dict(trimmed["error"], traceback=None, message="")
        body = json.dumps(trimmed).encode("utf-8")
    if len(body) > MAX_RESPONSE_BYTES:
        # Nothing left to give: this route's payload is not the execution shape, so send the
        # uniform error object rather than a body of unknown size.
        trimmed = {"execution_id": payload.get("execution_id"),
                   "error": {"type": "InternalError", "message": "response too large"}}
        body = json.dumps(trimmed).encode("utf-8")
    LOG.error("response for %s exceeded %d bytes; degraded to %d",
              payload.get("execution_id"), MAX_RESPONSE_BYTES, len(body))
    return trimmed, body


# --------------------------------------------------------------------------------------
# The scheduler: one execution at a time, two waiting, bounded wait
# --------------------------------------------------------------------------------------


class Job:
    # NO pgid SLOT. The design point is that no pgid is ever cached (see _resolve_pgid); a
    # slot for one is an invitation to start.
    __slots__ = ("req", "conn", "enqueued_at", "pid", "deadline", "dirs", "owner",
                 "kill_lock", "reaped", "limit", "done")

    def __init__(self, req, conn, owner=None):
        self.req = req
        self.conn = conn
        # The Supervisor that owns this job, so the watchdog can read the retained total for
        # the aggregate /scratch check. None outside a real run (the queue tests build Jobs).
        self.owner = owner
        self.enqueued_at = time.monotonic()
        self.pid = None
        self.deadline = None
        self.dirs = None
        # THE REAP AND EVERY SIGNAL ARE SERIALISED BY THIS LOCK, and that is a correctness
        # requirement rather than tidiness. Once waitpid reaps the child the pid is free for
        # the kernel to reuse, and a watchdog that decided to kill a moment earlier would then
        # signal a process group that is plausibly THE NEXT EXECUTION'S CHILD. Reaping and
        # setting `reaped` happen together under this lock; every signal path takes it, checks
        # `reaped`, and re-reads the pgid with os.getpgid() before signalling.
        self.kill_lock = threading.Lock()
        self.reaped = False
        self.limit = None      # the first supervisor limit that fired: a reserved error type
        self.done = threading.Event()   # set once the child is reaped; stops the watchdog


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

    def __init__(self, scratch_root, ready=False, retention_s=None):
        self.scratch_root = scratch_root
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._waiting = deque()
        self._running = None
        self._pending_ids = set()
        self._retained_ids = set()
        # execution_id -> [monotonic deadline, measured bytes], in COMPLETION ORDER. Insertion
        # order is what makes "oldest-first eviction" a property of the structure rather than a
        # sort key somebody has to remember to keep in step with the clock.
        #
        # THE SIZE IS CACHED, NOT RE-MEASURED. It was re-measured by walking every retained
        # tree on every completion, which made a 300,000-file execution a tax on all fifteen
        # minutes of executions after it (MEASURED). Nothing writes to a retained directory —
        # the child is reaped and _retain has already trimmed it — so the value cannot drift,
        # and _forget_retained is the only thing that removes bytes.
        self._retention = {}
        self.retention_s = RETENTION_S if retention_s is None else retention_s
        self._stop_reaper = threading.Event()
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
        # REGISTERING THE DIRECTORY IS NOT OPTIONAL AND IS NOT _retain's ALONE. _retain runs on
        # the success path only; a fork OSError, a manifest failure or any other exception
        # after ExecutionDirs.create() left the id in _retained_ids — so it answered 409 — with
        # NO row in _retention, so its bytes were counted against no ceiling and only the
        # mtime sweep removed it, up to fifteen minutes later. Whatever created the directory,
        # something must own deleting it.
        if retain:
            self._register_retention(job.req.execution_id, job.dirs)
        with self._cv:
            if self._running is job:
                self._running = None
            self._pending_ids.discard(job.req.execution_id)
            if retain:
                self._retained_ids.add(job.req.execution_id)
            self._cv.notify_all()

    def _register_retention(self, execution_id, dirs):
        """Give a created directory a retention deadline and a measured size. Idempotent.

        MEASURES base, NOT artifacts. This runs on the path _retain never reached — an
        exception in _execute_inner — so nothing has deleted tmp/, home/, cache/ or pycache/
        and nothing has trimmed artifacts/. Charging only artifacts/ charged that as ZERO,
        up to a whole 192 MiB execution quota held against no ceiling until the mtime sweep
        found it fifteen minutes later. The ceiling is re-checked here for the same reason:
        nothing else re-checks it until the next completion, which may never come.
        """
        with self._lock:
            if execution_id in self._retention:
                return
        size = 0
        if dirs is not None:
            size, _, _ = _dir_usage(dirs.base, TRIM_ENTRY_CEILING)
        with self._lock:
            self._retention.setdefault(
                execution_id, [time.monotonic() + self.retention_s, size])
        self._enforce_retained_ceiling()

    # -- /scratch lifecycle (genetics-results-suite-4h6.46) -----------------------------

    def _retain(self, job):
        """On completion: delete everything under /scratch/<id> except artifacts/, TRIM
        artifacts/ back inside its own quota, register the retention deadline and size, and
        bring the aggregate retained set back under its ceiling. Returns files deleted.

        artifacts/ is what read_artifact has to return; tmp, home, caches, pycache and the
        token file have no reader after the child is reaped and every byte of them counts
        against the same 512Mi the kubelet evicts the pod for exceeding.

        THE TRIM IS WHY THE BUDGET CLOSES. Without it a quota kill RETAINED its own overshoot:
        MEASURED, a burst write killed by ArtifactQuota at 64 MiB left 93 MiB on disk (46%
        over) in 0.31s, and at the ~1 GiB/s tmpfs sustains that is ~264 MiB. Retaining that
        makes RETAINED_ARTIFACTS_CEILING a ceiling over unbounded terms; trimming makes every
        term <= ARTIFACT_QUOTA_BYTES and the ceiling exact.

        It runs BEFORE build_manifest, which is the only order that works: a manifest built
        first would advertise names the trim then deletes, and the model would be told about an
        artifact read_artifact answers "not found" for. The trimmed count reaches the response
        through artifacts_omitted, the field that already means "present but not listed".
        """
        base = job.dirs.base
        try:
            names = os.listdir(base)
        except OSError as exc:
            LOG.error("execution %s: cannot list its directory to clean it: %s",
                      job.req.execution_id, exc)
            return 0
        for name in names:
            if name == "artifacts":
                continue
            path = os.path.join(base, name)
            try:
                if os.path.islink(path) or not os.path.isdir(path):
                    os.unlink(path)
                else:
                    shutil.rmtree(path)
            except OSError as exc:
                LOG.error("execution %s: could not delete %s: %s",
                          job.req.execution_id, name, exc)
        trimmed, size = _trim_artifacts(job.dirs.artifacts)
        if trimmed:
            LOG.warning("execution %s: artifacts/ was over its quota after the kill; deleted "
                        "%d newest entr%s to bring it back to %d MiB",
                        job.req.execution_id, trimmed, "y" if trimmed == 1 else "ies",
                        ARTIFACT_QUOTA_BYTES // (1024 * 1024))
        with self._lock:
            self._retention[job.req.execution_id] = [
                time.monotonic() + self.retention_s, size]
        self._enforce_retained_ceiling()
        return trimmed

    def _retained_sizes(self):
        """[(execution_id, bytes)] in completion order — which is oldest-first."""
        with self._lock:
            return [(eid, row[1]) for eid, row in self._retention.items()]

    def _retained_total(self):
        with self._lock:
            return sum(row[1] for row in self._retention.values())

    def _enforce_retained_ceiling(self):
        """Oldest-first eviction until the retained artifact set is under its ceiling.

        Retention degrades gracefully instead of accumulating until the kubelet intervenes —
        and the kubelet intervening is a POD EVICTION, which would destroy every retained
        artifact from every execution in the window plus the in-flight script. Losing the
        oldest artifacts is the cheaper failure by a wide margin.

        THE LOOP HAS NO `len(sizes) > 1` GUARD, and removing it is the fix rather than a
        widening. That guard meant a single over-ceiling execution sat above the ceiling
        permanently — there was nothing older to evict, so the loop exited having achieved
        nothing. It only existed to protect the newest execution, the one whose manifest the
        model is about to be handed. Per-execution bounds protect it now: the loop stops the
        moment the sum is under the ceiling, so it can reach the newest row only if that row
        ALONE exceeds 256 MiB. A COMPLETED execution cannot — _trim_artifacts brings it to
        <= ARTIFACT_QUOTA_BYTES (64 MiB) before it is retained. A FAILURE-PATH retention is
        neither cleaned nor trimmed and is measured over the whole base directory, so its
        bound is the 192 MiB execution quota instead; still under the ceiling. Both are bounds
        on a POLLED quota, so both describe the steady state and not a hostile burst's
        transient peak. A guard that cannot fire beats a guard that fires wrongly.
        """
        sizes = self._retained_sizes()
        total = sum(size for _, size in sizes)
        evicted = []
        while total > RETAINED_ARTIFACTS_CEILING_BYTES and sizes:
            eid, size = sizes.pop(0)
            self._forget_retained(eid)
            evicted.append(eid)
            total -= size
        if evicted:
            LOG.warning(
                "retained artifacts exceeded %d MiB; evicted %d oldest execution(s): %s",
                RETAINED_ARTIFACTS_CEILING_BYTES // (1024 * 1024), len(evicted),
                ", ".join(evicted))
        return evicted

    def _forget_retained(self, execution_id):
        """Delete a retained execution's directory and make its id reusable again."""
        shutil.rmtree(os.path.join(self.scratch_root, execution_id), ignore_errors=True)
        with self._cv:
            self._retention.pop(execution_id, None)
            self._retained_ids.discard(execution_id)
            self._cv.notify_all()

    def reap_expired(self):
        """Delete retained artifacts past their TTL, and any directory belonging to no
        execution this process knows about. Returns the ids removed.

        TWO MECHANISMS, because they answer different failures. The registry covers
        executions that COMPLETED: their artifacts are deleted at the deadline whether or not
        anything ever read them, which is what makes "nothing persists beyond 15 minutes"
        true rather than aspirational. The filesystem sweep covers a directory that was
        created and whose job then died on a path that never reached _retain — an orphan the
        registry has no row for and that would otherwise sit there until the pod restarts.
        The sweep uses mtime, so an id that is live or queued is excluded by name first.
        """
        now = time.monotonic()
        with self._lock:
            live = set(self._pending_ids)
            if self._running is not None:
                live.add(self._running.req.execution_id)
            expired = [eid for eid, row in self._retention.items() if row[0] <= now]
            known = set(self._retention)
        removed = []
        for eid in expired:
            self._forget_retained(eid)
            removed.append(eid)
        try:
            names = os.listdir(self.scratch_root)
        except OSError:
            names = []
        for name in names:
            if name == SUPERVISOR_DIR_NAME or name in live or name in known:
                continue
            path = os.path.join(self.scratch_root, name)
            try:
                age = time.time() - os.stat(path).st_mtime
            except OSError:
                continue
            if age >= self.retention_s:
                self._forget_retained(name)
                removed.append(name)
        if removed:
            LOG.info("retention reaper removed %d execution director%s",
                     len(removed), "y" if len(removed) == 1 else "ies")
        return removed

    def _reaper_loop(self, poll=REAPER_POLL_S):
        while not self._stop_reaper.wait(poll):
            try:
                self.reap_expired()
            except Exception:
                LOG.exception("retention reaper failed; it will run again")

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
        # THE UNLINK IS IN A finally BECAUSE EVERY OTHER ARRANGEMENT LEAKS A CREDENTIAL. The
        # unlink that matters is the one below, the moment the child is reaped; this one is
        # for the paths that never get there — a fork OSError, an exception while wiring the
        # pipes — which otherwise leave a mode-0600 token file on disk for the reaper to
        # notice up to fifteen minutes later. _release registers the directory itself for the
        # same reason (see _register_retention).
        try:
            return self._execute_inner(job)
        finally:
            try:
                os.unlink(job.dirs.tokens)
            except OSError:
                pass

    def _execute_inner(self, job):
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
        job.deadline = started + job.req.timeout_s
        # job.pgid IS DELIBERATELY NOT SET HERE, and an earlier version of this line is why.
        # It read `os.getpgid(pid)` immediately after the fork and MEASURED the supervisor's
        # OWN process group: the child's setsid() is its first statement but the parent still
        # wins the race routinely. The parent cannot fix that by calling setpgid(pid, pid)
        # itself either — that makes the child a group leader, and setsid() then fails with
        # EPERM for a group leader. So the pgid is resolved fresh at every use, by
        # _resolve_pgid, which refuses to hand back the supervisor's own group. Caching it
        # here would have pointed every killpg at the supervisor.
        _apply_limits(job)

        out_box = {}
        st_box = {}
        reaped = threading.Event()
        fields = ("raw", "total", "stopped", "abandoned")
        t_out = threading.Thread(
            target=lambda: out_box.update(zip(fields, _drain(
                out_r, PIPE_CAP_BYTES, reaped,
                on_limit=lambda: _fire_limit(job, ERR_OUTPUT_LIMIT)))),
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
            wait_status = _reap(job)
            # The child's own lifetime, measured before the drain. Timing the drain instead
            # reports a number no process spent running whenever a descendant escapes.
            duration_ms = int((time.monotonic() - started) * 1000)
        finally:
            job.done.set()
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
            # write end — and having escaped the group, it is a process no signal here
            # reaches (see _kill_group). Freeing the slot is what this path achieves, and by
            # this point it is free; containing the escapee is 4h6.55's.
            LOG.warning(
                "execution %s: a descendant outlived the child and still holds the output "
                "pipe; drain abandoned after %.1fs", job.req.execution_id, DRAIN_GRACE_S)

        exit_code = os.WEXITSTATUS(wait_status) if os.WIFEXITED(wait_status) else None
        sig = os.WTERMSIG(wait_status) if os.WIFSIGNALED(wait_status) else None

        # 4h6.43: the token file goes the moment the child is reaped, whether or not the SDK
        # ever read it. _retain deletes it too — this is not redundancy for its own sake, it
        # is the case where the script never made a data call and the file would otherwise sit
        # there for as long as the response takes to build.
        try:
            os.unlink(dirs.tokens)
        except OSError:
            pass

        # THE ORDER IS TRIM, THEN LIST. _retain brings artifacts/ back inside its quota; a
        # manifest built before that would name files the trim deletes a moment later.
        trimmed = self._retain(job)
        artifacts, omitted = build_manifest(dirs.artifacts)

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
            artifacts_omitted=omitted + trimmed,
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

        if job.limit is not None:
            # A SUPERVISOR LIMIT WINS OVER HOW THE PROCESS HAPPENED TO DIE, including over a
            # clean exit 0. A child that traps SIGTERM and exits zero, or that finishes in the
            # 2s grace, still ended because the supervisor decided it should; reporting that
            # as "ok" would tell the model its analysis completed when its output was cut off
            # or its directory was over quota. `exit_code` and `signal` still report the truth
            # about the process — normally exit_code null with signal 15 or 9.
            if job.limit == ERR_TIMEOUT:
                # The contract keeps these apart: `timeout` is the wall clock, `limit` is
                # everything else the supervisor enforces, and `error.limit` names which.
                status = "timeout"
                error = {"type": ERR_TIMEOUT,
                         "message": f"execution exceeded its {job.req.timeout_s}s wall clock",
                         "traceback": None, "limit": None}
            else:
                status = "limit"
                error = {"type": job.limit, "message": _LIMIT_MESSAGES[job.limit],
                         "traceback": None, "limit": job.limit}
        elif exit_code == 0 and signal_ is None:
            # THE SUPERVISOR'S OWN OBSERVATION WINS. The status pipe is fd 3 in a child that
            # is forked and not exec'd, so the script can write to it: without this a script
            # can forge {"type": ...} and then exit 0, turning a successful run into
            # status "error" with exit_code 0 — a row the contract's status table says cannot
            # exist, and a lie the model is told about its own analysis. An uncaught exception
            # always leaves a non-zero exit, so no legitimate record is lost here. The record
            # stays untrusted input everywhere else: `message` and `traceback` are re-capped
            # below and `type` goes through _sanitise_error_type, which until this fix it did
            # not — the claim was in this comment before it was in the code.
            status = "ok"
            error = None
            if child_error is not None:
                LOG.warning(
                    "execution %s wrote a status record and exited 0; ignoring it",
                    job.req.execution_id)
        elif child_error is not None:
            status = "error"
            error = {
                "type": _sanitise_error_type(child_error["type"], exit_code,
                                             job.req.execution_id),
                "message": str(child_error.get("message") or "")[:MESSAGE_MAX_BYTES],
                "traceback": (str(child_error["traceback"])[-TRACEBACK_MAX_BYTES:]
                              if child_error.get("traceback") else None),
                "limit": None,
            }
        elif signal_ is not None:
            # A signal with no supervisor limit recorded: the kernel's OOM killer, or
            # something outside this process. The supervisor's own kills all set job.limit
            # first and are answered in the branch above.
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

        output, elided = _cap_output(out_raw)
        return {
            "execution_id": job.req.execution_id,
            "status": status,
            "exit_code": exit_code,
            "signal": signal_,
            "duration_ms": duration_ms,
            "output": output,
            "output_bytes": out_total,
            # Two independent ways output is incomplete, and the contract folds them into one
            # flag: the 8 MiB pipe cap fired, or the 64 KiB return window elided a middle.
            "output_truncated": bool(out_stopped or elided),
            "error": error,
            "artifacts": artifacts,
            "artifacts_omitted": artifacts_omitted,
        }


# --------------------------------------------------------------------------------------
# Per-execution limits: the wall clock, the pid budget, the /scratch quotas and the kill path
# (4h6.41 and 4h6.46). One watchdog thread polls all four, because they share a poll interval
# and a single kill path, and four timers would give four chances to get the reap race wrong.
# --------------------------------------------------------------------------------------

_LIMIT_MESSAGES = {
    ERR_OUTPUT_LIMIT: f"output exceeded the {PIPE_CAP_BYTES // (1024 * 1024)} MiB pipe cap",
    ERR_PID_LIMIT: f"process group exceeded the {PID_BUDGET}-process budget",
    ERR_ARTIFACT_QUOTA:
        f"artifacts/ exceeded the {ARTIFACT_QUOTA_BYTES // (1024 * 1024)} MiB / "
        f"{ARTIFACT_ENTRY_BUDGET}-entry per-execution quota",
    ERR_SCRATCH_QUOTA:
        f"the execution directory exceeded its "
        f"{EXECUTION_TOTAL_QUOTA_BYTES // (1024 * 1024)} MiB / "
        f"{EXECUTION_ENTRY_BUDGET}-entry quota, or /scratch as a whole exceeded "
        f"{SCRATCH_AGGREGATE_CEILING_BYTES // (1024 * 1024)} MiB",
}


def _fire_limit(job, reason):
    """Record the first limit that fired and kill the child's process group.

    First one wins: a timeout that also trips the pid budget while dying should report the
    reason the supervisor acted on, not whichever poll happened to run last.

    A REAPED CHILD CANNOT HAVE A LIMIT FIRE ON IT, and the `reaped` check is what makes that
    true. _watchdog enters a poll body with `job.done` clear and then compares the clock
    against the deadline; if _reap returns inside that body the run is already complete and
    correct, and recording ERR_TIMEOUT anyway made _response — which gives job.limit absolute
    priority — answer `status: "timeout"` for a clean run, discarding its output and its
    manifest. Nothing wrong was ever KILLED (both signal paths refuse a reaped job); the model
    was simply told the wrong thing about its own analysis. The window is one poll wide, and
    checking `reaped` under the same lock the reap sets it under closes it.
    """
    with job.kill_lock:
        if job.reaped:
            return
        if job.limit is None:
            job.limit = reason
        elif job.limit != reason:
            return
    LOG.warning("execution %s: %s; killing the child's process group",
                job.req.execution_id, _LIMIT_MESSAGES.get(reason, reason))
    _kill_group(job)


def _kill_group(job):
    """SIGTERM the child's process group, SIGKILL after KILL_GRACE_S.

    WHAT THIS REACHES, precisely: the child and every descendant that stayed in its process
    group. A descendant that calls setsid() is NOT in the group and is not signalled — that
    was measured, with killpg returning ESRCH while the escapee kept running. This is the best
    available mechanism and it handles the ordinary case; it is not a containment boundary,
    and 4h6.55 owns the one that would be.

    EVERY SIGNAL RE-READS THE PGID UNDER job.kill_lock, and neither half of that is optional.
    A pgid cached at fork time goes stale the instant waitpid reaps the child, and the pid is
    then reusable — signalling a remembered number can hit an unrelated group, plausibly the
    next execution's child. Holding the lock is what makes "not reaped yet" true for the
    duration of the signal: a zombie keeps its pgid and its pid cannot be recycled until it is
    reaped, so re-reading under the lock is sound and re-reading without it is not.

    "GONE" AND "FAILED" ARE DIFFERENT ANSWERS TO SIGTERM and only one of them may skip the
    SIGKILL. ProcessLookupError means the group has already exited: escalating would signal
    nothing, or a recycled pid. A transient OSError out of getpgid/killpg means DELIVERY
    failed against a process that is still running, and treating that as "already gone"
    forfeits the escalation for exactly the child that needed it — the one that ignored or
    could not be reached by SIGTERM.
    """
    if _signal_group(job, signal.SIGTERM) == _SIGNAL_GONE:
        return
    deadline = time.monotonic() + KILL_GRACE_S
    while time.monotonic() < deadline:
        with job.kill_lock:
            if job.reaped:
                return
        time.sleep(0.05)
    _signal_group(job, signal.SIGKILL)


def _resolve_pgid(job):
    """The child's OWN process group, read live, or None if it does not have one yet.

    THE GUARD IS THE FUNCTION. A pgid equal to the supervisor's own group means the child has
    not reached setsid() (or never will), and killpg on that value would signal the SUPERVISOR
    — measured, not hypothesised: reading the pgid immediately after the fork returned the
    supervisor's group routinely, because the parent wins the race against the child's first
    statement. Callers treat None as "no group to signal or count", never as "the group is
    empty". The caller holds job.kill_lock, so job.pid cannot be reaped and recycled while
    this reads it.
    """
    if job.pid is None:
        return None
    try:
        pgid = os.getpgid(job.pid)
    except OSError:
        return None
    if pgid == os.getpgrp():
        return None
    return pgid


# _signal_group's three answers. "gone" and "failed" were one value (False) and had to be
# separated: _kill_group skipped its SIGKILL escalation on both, so a transient OSError from
# getpgid or killpg silently forfeited the escalation.
_SIGNAL_DELIVERED = "delivered"
_SIGNAL_GONE = "gone"
_SIGNAL_FAILED = "failed"


def _signal_group(job, sig):
    """_SIGNAL_DELIVERED, _SIGNAL_GONE (nothing left to signal) or _SIGNAL_FAILED."""
    with job.kill_lock:
        if job.reaped or job.pid is None:
            return _SIGNAL_GONE
        pgid = _resolve_pgid(job)
        try:
            if pgid is None:
                # The child has no group of its own, so there is nothing group-shaped to
                # signal; signal the child alone rather than the supervisor's group.
                LOG.warning("execution %s: child has no process group of its own; "
                            "signalling pid %s alone", job.req.execution_id, job.pid)
                os.kill(job.pid, sig)
            else:
                os.killpg(pgid, sig)
        except ProcessLookupError:
            # The normal case for an already-exited group, not an error.
            return _SIGNAL_GONE
        except OSError as exc:
            LOG.error("execution %s: signalling %s with %s failed: %s",
                      job.req.execution_id, pgid or job.pid, sig, exc)
            return _SIGNAL_FAILED
        return _SIGNAL_DELIVERED


def _reap(job):
    """Block until the child exits, then reap it under job.kill_lock. Returns wait status.

    `waitid(..., WNOWAIT)` blocks without consuming the zombie, so the wait costs nothing and
    the pid stays un-recyclable; the actual `waitpid` and the `reaped` flag are then set
    together under the lock. A plain blocking `waitpid` cannot do this — it reaps before any
    lock can be taken, leaving a window in which the watchdog's killpg targets a recycled pid.

    THE FALLBACK MUST NOT BLOCK UNDER THE LOCK, and an earlier version did: `with kill_lock:
    waitpid(pid, 0)` holds the lock for the child's entire remaining lifetime, and that lock is
    the one _signal_group takes and _kill_group's grace loop polls. A non-terminating child
    could then never be signalled at all — _fire_limit blocked forever on the lock and the slot
    was held until the pod restarted. That is a DEADLOCK ON EVERY KILL PATH, not the "the race
    window is back" the comment there described. So the fallback polls WNOHANG instead: the
    lock is held only for the syscall that reaps and the flag it sets, and the sleep between
    polls happens outside it.
    """
    try:
        os.waitid(os.P_PID, job.pid, os.WEXITED | os.WNOWAIT)
    except (AttributeError, OSError):
        while True:
            with job.kill_lock:
                pid, wait_status = os.waitpid(job.pid, os.WNOHANG)
                if pid != 0:
                    job.reaped = True
                    return wait_status
            time.sleep(0.02)
    with job.kill_lock:
        _, wait_status = os.waitpid(job.pid, 0)
        job.reaped = True
    return wait_status


def _group_members(pgid):
    """The pids in `pgid`, or None if /proc could not be read.

    None is not zero and callers must not treat it as one: an unreadable /proc means the pid
    budget is unenforceable, which is a degradation to report, not a group of size nought.
    Whether /proc process-group inspection behaves the same under gVisor is unverified and is
    the deploy-window bead's to establish (4h6.51).
    """
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    members = []
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue  # exited between listdir and open
        # comm is parenthesised and may itself contain spaces and parens, so split after the
        # LAST ')': the fields that follow are state, ppid, pgrp, ...
        cut = raw.rfind(b")")
        if cut < 0:
            continue
        fields = raw[cut + 2:].split()
        if len(fields) < 3:
            continue
        try:
            if int(fields[2]) == pgid:
                members.append(int(name))
        except ValueError:
            continue
    return members


def _dir_usage(path, entry_limit, sub=None):
    """ONE pass over `path`. Returns (cost, entries, sub) where `sub` is (cost, entries) for
    the `sub` subtree if given and (0, 0) otherwise.

    COST IS st_blocks PLUS A PER-ENTRY FLOOR, and both halves are needed.

    st_blocks is what the kubelet's emptyDir accounting sees for DATA, and the difference from
    st_size is reachable on purpose: `f.seek(512 << 20); f.write(b'x')` makes a file whose
    st_size is 512 MiB and whose blocks are nearly none. Charging apparent size would kill that
    script for using no space.

    DIRENT_COST_BYTES is the other half, and charging blocks alone said an empty file was free.
    MEASURED: 300,000 zero-length files charged 8.6 MB against a 192 MiB quota, so no limit
    fired, while the response reached 19.8 MB and the supervisor's RSS went 22 MB -> 166 MB. An
    empty file is not free anywhere it matters — an inode, a directory entry, a manifest row, a
    scan step — so every entry pays a floor.

    `entry_limit` BOUNDS THE WALK ITSELF, which is the other half of the same bug. The scan
    runs on the watchdog thread between two deadline checks, and one pass over 800,000 empty
    files took 8.47s and reported 0 bytes — with timeout_s=30 the child was killed at 46.74s
    (MEASURED). Stopping at the limit is sound because the limit IS a budget: a tree with more
    entries than the budget allows is over it, and the exact count past that point changes no
    decision. The caller is told by `entries > entry_limit`.
    """
    cost = 0
    entries = 0
    sub_cost = 0
    sub_entries = 0
    stack = [(path, False)]
    while stack:
        current, in_sub = stack.pop()
        if not in_sub and sub is not None and current == sub:
            in_sub = True
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    entry_cost = st.st_blocks * 512 + DIRENT_COST_BYTES
                    cost += entry_cost
                    entries += 1
                    if in_sub:
                        sub_cost += entry_cost
                        sub_entries += 1
                    if stat.S_ISDIR(st.st_mode):
                        stack.append((entry.path, in_sub))
                    if entries > entry_limit:
                        return cost, entries, (sub_cost, sub_entries)
        except OSError:
            continue
    return cost, entries, (sub_cost, sub_entries)


def _dir_bytes(path, entry_limit=EXECUTION_ENTRY_BUDGET):
    """The cost half of _dir_usage, for callers that only want a number."""
    return _dir_usage(path, entry_limit)[0]


def _trim_artifacts(artifacts_dir):
    """Bring artifacts/ inside ARTIFACT_QUOTA_BYTES and ARTIFACT_ENTRY_BUDGET. (deleted, cost).

    NEWEST-FIRST, by mtime with the name as tiebreak. The entry that blew the quota is the one
    being written when the kill landed; the smaller, deliberate artifacts a script produced
    earlier are the ones worth keeping. One policy covers both budgets, so there is no case
    where the two orderings disagree about which file goes.

    Subdirectories are deleted whole and count like any other entry: a bare name cannot address
    their contents, so read_artifact can never return anything from one, but their bytes count
    against the same volume.

    THE ENUMERATION IS NOT CAPPED AT EXECUTION_ENTRY_BUDGET, and it used to be — a bound that
    was circular, because the trim is the thing that MAKES the tree small. Capped, it sorted a
    truncated 20 000-name sample and derived both the surviving entry count and the returned
    size from it: MEASURED on 25 000 zero-length files, it left 6 024 entries against a 1 024
    budget and reported 0.5 MiB where _dir_usage measured 2.9 MiB. That number is what _retain
    caches, so _retained_total() undercounted by the same factor, the watchdog's aggregate
    check could not fire and _enforce_retained_ceiling evicted against a fiction — the pod
    eviction this whole lifecycle exists to prevent. It is reachable: ArtifactQuota fires above
    1 024 entries, KILL_GRACE_S is 2 s, and ~14 000 file creations/s were measured on a slow
    filesystem, so the grace window alone is tens of thousands of entries.

    So it DRAINS in bounded passes instead. A pass that sees the whole directory does the exact
    newest-first trim and returns an exact total. A pass that fills its chunk cannot order its
    sample against the unseen remainder — and a directory that far over the entry budget is one
    where nearly everything must go regardless — so it drops the sample whole and re-scans.
    Neither the loop nor the materialised list is unbounded: one chunk at a time, TRIM_ENTRY_CEILING
    entries in total, and a pass that deletes nothing gives up rather than re-sampling names it
    cannot remove. On the give-up path the size returned is a real measurement, never a sample.
    """
    deleted = 0
    seen = 0
    while True:
        rows = []
        total = 0
        names = 0
        truncated = False
        for name in _iter_dir_names(artifacts_dir, TRIM_SCAN_CHUNK + 1):
            names += 1
            if names > TRIM_SCAN_CHUNK:
                truncated = True
                break
            path = os.path.join(artifacts_dir, name)
            try:
                st = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode):
                cost = (st.st_blocks * 512 + DIRENT_COST_BYTES
                        + _dir_bytes(path, TRIM_ENTRY_CEILING))
            else:
                cost = st.st_blocks * 512 + DIRENT_COST_BYTES
            rows.append((st.st_mtime, name, path, cost, stat.S_ISDIR(st.st_mode)))
            total += cost
        seen += names
        if not truncated:
            count = len(rows)
            for mtime, name, path, cost, is_dir in sorted(rows, key=lambda r: (-r[0], r[1])):
                if total <= ARTIFACT_QUOTA_BYTES and count <= ARTIFACT_ENTRY_BUDGET:
                    break
                if not _remove_entry(path, is_dir):
                    continue
                total -= cost
                count -= 1
                deleted += 1
            return deleted, total
        dropped = 0
        for _, _, path, _, is_dir in rows:
            if _remove_entry(path, is_dir):
                dropped += 1
        deleted += dropped
        if dropped == 0 or seen > TRIM_ENTRY_CEILING:
            LOG.error("could not drain %s: %d entries seen, %d deleted, and the last pass "
                      "removed %d — reporting a measured size, not a sampled one",
                      artifacts_dir, seen, deleted, dropped)
            return deleted, _dir_bytes(artifacts_dir, TRIM_ENTRY_CEILING)


def _remove_entry(path, is_dir):
    """Delete one artifacts/ entry, directory or not. False (logged) if it survives."""
    try:
        if is_dir and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    except OSError as exc:
        LOG.error("could not trim %s: %s", path, exc)
        return False
    return True


def _apply_limits(job):
    """PARENT SIDE (4h6.41). Runs immediately after the fork and must not block.

    Raises the child's oom_score_adj and starts the watchdog thread that owns the wall clock,
    the pid budget, the per-execution /scratch quotas and the aggregate /scratch ceiling.

    WHAT THE oom_score_adj RAISE IS ACTUALLY WORTH, measured in the real image rather than
    assumed: the child starts at 0 (inherited); writing 500 succeeds; writing 0 again ALSO
    succeeds, from inside the child, at any time. Only going BELOW the inherited floor is
    refused — `-500` returned EPERM for the child's own file and for the supervisor's. So a
    script can undo this, and the honest guarantee is not "+500 holds" but "the child can
    never make itself a BETTER OOM candidate than the supervisor": its adj stays in [0, 1000]
    against the supervisor's 0. The supervisor's own -500, which section 2 asks for, is
    unreachable at runtime for the same reason and is a pod-spec change (`4h6.50`), which is
    why nothing here pretends to set it.
    """
    _raise_child_oom_score(job)
    threading.Thread(target=_watchdog, args=(job,), daemon=True,
                     name=f"watchdog-{job.req.execution_id[:8]}").start()


def _raise_child_oom_score(job):
    path = f"/proc/{job.pid}/oom_score_adj"
    try:
        with open(path, "w") as fh:
            fh.write(str(CHILD_OOM_SCORE_ADJ))
    except OSError as exc:
        # Best effort with a visible failure, never a silent one: if this stops working the
        # cgroup OOM killer goes back to choosing between two processes by RSS heuristic, and
        # an operator needs to see that in the log rather than infer it from a dead pod.
        LOG.warning("execution %s: could not raise the child's oom_score_adj (%s): %s",
                    job.req.execution_id, path, exc)


def _watchdog(job):
    """Poll the wall clock, the process-group size and the /scratch budgets until the reap.

    THE WALL CLOCK IS ON ITS OWN TIMER AND NEVER BEHIND THE FILESYSTEM SCAN. It used to be
    checked once per tick, at the top, with two full tree walks after it in the same body —
    and `artifacts/` was walked twice, because it lives under `base`. So the deadline was as
    late as the child chose to make it: MEASURED with timeout_s=30, 0 files -> killed at
    30.23s, 200,000 empty files -> 45.51s, 800,000 -> 46.74s. timeout_s is a number the client
    is given exactly, and MAX_QUEUED_WAIT_S is 120s, so every extra second is one the next two
    callers spend queued or being 429ed. Three things fix it and all three are load-bearing:
    the wait shrinks as the deadline approaches, so the tick cannot straddle it; the scan is
    entry-bounded (see _dir_usage), so it has a worst case at all; and the clock is checked
    again immediately AFTER the scan, so an overrun fires on this tick rather than the next.

    ONE WALK PER TICK, not two: _dir_usage measures base and the artifacts subtree in a single
    pass, which is what the `sub` argument exists for.

    THE AGGREGATE CHECK IS THE INVARIANT THAT MATTERS and nothing measured it during a run.
    The per-execution quota bounds one execution and the retained ceiling bounds the completed
    set, but the emptyDir sizeLimit is charged the SUM, and the sum was measured nowhere.
    Retained sizes are cached (they cannot drift — see Supervisor._retention), so this costs an
    addition rather than a second walk.
    """
    dirs = job.dirs
    proc_unreadable_logged = False
    scan_due = 0.0
    while True:
        now = time.monotonic()
        # Never sleep past the deadline: without this the tick straddles it and the kill lands
        # up to WATCHDOG_POLL_S late even when nothing is slow.
        wait = max(0.0, min(WATCHDOG_POLL_S, job.deadline - now))
        if job.done.wait(wait):
            return
        if job.limit is not None:
            return  # a limit already fired; _fire_limit owns the kill from here
        if time.monotonic() >= job.deadline:
            _fire_limit(job, ERR_TIMEOUT)
            return
        with job.kill_lock:
            pgid = _resolve_pgid(job)
        # None means the child has no group of its own yet (or is gone), NOT that its group
        # is empty. Skipping the check is the only safe reading: the alternative counts the
        # supervisor's own group against the child's budget.
        members = _group_members(pgid) if pgid is not None else None
        if pgid is not None and members is None:
            if not proc_unreadable_logged:
                LOG.error("execution %s: /proc is unreadable; the pid budget is not enforced",
                          job.req.execution_id)
                proc_unreadable_logged = True
        elif members is not None and len(members) > PID_BUDGET:
            _fire_limit(job, ERR_PID_LIMIT)
            return
        if time.monotonic() < scan_due:
            continue
        cost, entries, (art_cost, art_entries) = _dir_usage(
            dirs.base, EXECUTION_ENTRY_BUDGET, sub=dirs.artifacts)
        scan_due = time.monotonic() + WATCHDOG_POLL_S
        # The scan is bounded but not free, and the deadline may have passed inside it.
        if time.monotonic() >= job.deadline:
            _fire_limit(job, ERR_TIMEOUT)
            return
        if art_cost > ARTIFACT_QUOTA_BYTES or art_entries > ARTIFACT_ENTRY_BUDGET:
            _fire_limit(job, ERR_ARTIFACT_QUOTA)
            return
        if cost > EXECUTION_TOTAL_QUOTA_BYTES or entries > EXECUTION_ENTRY_BUDGET:
            _fire_limit(job, ERR_SCRATCH_QUOTA)
            return
        retained = job.owner._retained_total() if job.owner is not None else 0
        if retained + cost > SCRATCH_AGGREGATE_CEILING_BYTES:
            LOG.warning("execution %s: /scratch aggregate %d MiB (retained %d + live %d) is "
                        "over the %d MiB ceiling", job.req.execution_id,
                        (retained + cost) // (1024 * 1024), retained // (1024 * 1024),
                        cost // (1024 * 1024),
                        SCRATCH_AGGREGATE_CEILING_BYTES // (1024 * 1024))
            _fire_limit(job, ERR_SCRATCH_QUOTA)
            return


def _apply_child_limits():
    """CHILD SIDE (4h6.41). Runs in the child, before the script, and applies RLIMIT_AS.

    Separate from _apply_limits because setrlimit on ANOTHER process needs CAP_SYS_RESOURCE,
    which the pod drops — this is not a stylistic split. It takes no `job` for the same reason
    the fork model demands generally: nothing in the parent's object graph is safe to reach
    for from here.

    Hitting RLIMIT_AS gives a clean MemoryError inside the child. That is a better failure
    than a cgroup OOM kill in either direction, because the kernel picks the victim by
    oom_score — a heuristic over RSS — and gVisor changes the accounting again, since the
    sentry holds memory on the application's behalf.

    THE HARD LIMIT IS LOWERED TOO, AND THAT IS THE WHOLE CONTROL. Setting only the soft limit
    made this OPT-OUT: raising a soft limit back up to the hard limit is unprivileged, so
    `setrlimit(RLIMIT_AS, (RLIM_INFINITY, RLIM_INFINITY))` from the script succeeded — MEASURED
    in the real image, after which allocating 2900 MiB produced exactly the cgroup OOM kill
    (sig=9) this docstring says it prevents. Lowering a hard limit is unprivileged and
    IRREVERSIBLE without CAP_SYS_RESOURCE, which the pod drops, so the child cannot undo it.
    This is the only setrlimit call in the file; there is no second one with the same defect.
    """
    try:
        import resource

        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = CHILD_RLIMIT_AS_BYTES
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception as exc:
        # The child's own setup failing is a StartupFailure the caller already reports; this
        # is deliberately not swallowed into a silent unlimited run.
        raise RuntimeError(f"could not apply RLIMIT_AS: {exc}") from exc


# --------------------------------------------------------------------------------------
# Token delivery (4h6.43)
# --------------------------------------------------------------------------------------


def _deliver_tokens(job):
    """Write the per-execution tokens where the child can read them, and nowhere else.

    A mode-0600 JSON file inside /scratch/<execution_id>, whose PATH is named to the child by
    SANDBOX_TOKEN_FILE. The tokens never enter the supervisor's own environment (residual #7),
    never reach the pod spec, a ConfigMap or a Secret, and the supervisor never reads the file
    back. genetics-results-suite-4h6.44 owns the other half: the SDK opens it once, unlinks it
    and picks the token by destination (`aud: db-api` for BIGQUERY_API_URL, `aud: results-api`
    for GENETICS_API_URL — they are audience-bound and a cross-audience token is a hard 401).

    THIS IS NOT AN EXPOSURE BOUND, and every earlier version of this design read as if it
    were. Three things were MEASURED against this exact shape (4h6.55):
      * the child is forked WITHOUT exec from a supervisor holding tokens in its address
        space, and a raw /proc/self/mem scan in the child recovered them — including from an
        execution that had already completed and been released;
      * a detached setsid() grandchild of an EARLIER execution read THIS execution's
        mode-0600 file from inside the read-once window;
      * so did every reference route (module globals, a frame walk, gc.get_objects()).
    The file is still the right thing to build: the child needs some route to the credential
    and this one is no worse than the alternatives, it keeps the token out of
    /proc/<pid>/environ, and it gives the SDK something to unlink. What bounds the exposure is
    4h6.55's resolution and nothing in this function.

    NO CHOWN AND NOT MODE 0400. That is section 2's permission contract for option (a), which
    is NOT IN EFFECT: the pod holds no CAP_CHOWN or CAP_SETUID and both were measured to
    return EPERM. 0400 without the chown would exclude the child, which is the process that
    needs to read it.

    Refusing to run uncredentialed is the other half of this and it lives in
    parse_execute_request, which rejects a token set that is incomplete, carries the wrong
    audience, or disagrees with the body — before any directory is created. It matters
    because db-api's pre-existing fail-open branch (unset INTERNAL_API_SECRET disables auth
    with a startup warning) is exactly what an uncredentialed run would reach.
    """
    raw = json.dumps(job.req.tokens).encode("utf-8")
    fd = os.open(job.dirs.tokens, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    # The sub/sid/jti the audit stamping needs (4h6.45) are already retained on the request as
    # job.req.claims, checked against the body at parse time. The supervisor is the only
    # component that both holds the token and sits outside the child's address space, which is
    # why 4h6.12 put the stamping here rather than in the SDK.


# --------------------------------------------------------------------------------------
# STUB (genetics-results-suite-4h6.45): the SDK audit stream. A COMMENT AND NOT A FUNCTION,
# deliberately. This was a `_audit_pipe(job)` that was defined, never called, and returned
# None while its docstring described a pipe, four caps and a re-stamper — a seam that
# describes itself falsely is worse than an absent one, because a reader greps for the
# mechanism, finds a function, and stops looking. Every word of the handoff is kept; only the
# claim to be code is dropped.
#
# Owed: a second pipe whose read end the supervisor holds, its number in the child's
# GENETICS_SDK_AUDIT_FD; rate, byte and per-line caps applied ON THE READ END, where the child
# cannot reach them; the child's framing re-parsed and re-framed as untrusted input;
# `[user=…] [session=…] [execution=…]` stamped from the TOKENS' sub/sid/jti (job.req.claims —
# retained here for exactly this) and never from the body or from the child; and the result
# forwarded to the pod's own stdout, the only stream the logging agent collects.
#
# It cannot be filled in one place. An fd reaches the child only by existing before the fork,
# so 4h6.45 edits three sites:
#
#   * _execute: create the pipe BEFORE `os.fork()`, pass the write end into _child_main
#     alongside out_w/st_w, close it in the parent immediately after the fork, and drain the
#     read end on a third thread that shares the `reaped` event and DRAIN_GRACE_S deadline the
#     other two use — the audit pipe is inherited by escaped descendants exactly as the output
#     pipe is.
#   * _child_main: dup the write end onto a fixed number the way status_w is dup'd onto
#     CHILD_STATUS_FD, and add that number to the `_close_inherited_fds({...})` keep-set. A
#     number missing from that set is closed a few lines later and the SDK writes to a closed
#     fd.
#   * ExecutionDirs.child_env: export the fd number as GENETICS_SDK_AUDIT_FD, next to the
#     three SANDBOX_* identity variables the SDK's prefix already reads.
#
# UNTIL IT LANDS: the SDK's audit records go to the child's stdout, where they are
# indistinguishable from script output, are subject to the same 64 KiB return window, and
# reach no collector at all.
# --------------------------------------------------------------------------------------


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
        # Every response goes out through here, so this is where the outgoing bound belongs —
        # the incoming one (MAX_BODY_BYTES) has had a single choke point since 4h6.39 and the
        # outgoing one had none.
        _, body = _cap_response(payload)
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
            job = Job(req, self.connection, owner=SUPERVISOR)
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


def _retention_s():
    """RETENTION_S, or a SHORTER value from the test-only override."""
    raw = os.environ.get(ENV_RETENTION_S)
    if not raw:
        return RETENTION_S
    try:
        value = int(raw)
    except ValueError:
        raise StartupAssertionError(f"{ENV_RETENTION_S}={raw!r} is not an integer")
    if value < 1 or value > RETENTION_S:
        # Refused, not clamped. A value above the contract's 15 minutes would leave artifacts
        # alive after chat-backend has been told they are gone, and silently accepting a
        # number the supervisor then ignores is how a knob ends up believed.
        raise StartupAssertionError(
            f"{ENV_RETENTION_S} may only SHORTEN retention: 1..{RETENTION_S}, got {value}")
    LOG.warning(
        "%s=%d overrides the %d-second artifact retention. Artifacts vanish sooner than "
        "read_artifact promises and than chat-backend records; this is a TEST-ONLY setting "
        "and neither the image nor k8s/deployments/sandbox.yaml sets it.",
        ENV_RETENTION_S, value, RETENTION_S)
    return value


def create(scratch_root=None, retention_s=None):
    """A Supervisor that is bound but NOT ready: /health answers 503 "starting" and
    /execute answers 503 NotReady. Nothing is forked and no directory is touched yet."""
    global SUPERVISOR
    SUPERVISOR = Supervisor(scratch_root or _scratch_root(), ready=False,
                            retention_s=_retention_s() if retention_s is None else retention_s)
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

    # The retention reaper (4h6.46). Started after the wipe so its first pass cannot race the
    # startup clean, and before `ready` so no execution can complete without one running.
    threading.Thread(target=supervisor._reaper_loop, daemon=True, name="retention-reaper").start()

    supervisor.ready = True
    return supervisor


def start(scratch_root=None, run_assertions=True, retention_s=None):
    """create() + bring_up(). The one-call form, used by tests."""
    return bring_up(create(scratch_root, retention_s=retention_s),
                    run_assertions=run_assertions)


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
