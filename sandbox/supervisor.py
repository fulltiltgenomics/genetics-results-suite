#!/usr/bin/env python3
"""The sandbox pod's main process: HTTP front door, one-at-a-time queue, fork and reap.

Wire contract: docs/code-execution-security.md. chat-backend's client cannot import from
here — prune_venv.py strips the image down to the genetics SDK's import closure — so both
sides restate the shape independently.

Structural constraints, each of which fails at runtime rather than at review:

* Stdlib only, plus sandbox/requirements.txt. A new third-party import has to be added to
  requirements.txt and to prune_venv.py's allow-list deliberately.
* No setuid and no chown: the pod drops CAP_SETUID/CAP_SETGID/CAP_CHOWN, so supervisor and
  child share uid 65532 and file modes separate nothing between them.
* The child is forked and not exec'd, which is what makes prewarm() worth anything: the
  pre-imported numpy/scipy/polars/matplotlib pages are inherited copy-on-write.
* The process that forks is not this one. A fork server is forked out at startup, before the
  first request is parsed, and every execution child comes from that pristine address space.
  Nothing that ever holds a token, a request body or user source code may call os.fork() to
  make an execution child.

Two limits of the controls here, stated because the optimistic reading is the dangerous one:

* Signals reach the child's process group, and a descendant that calls setsid() leaves it.
  What still reaches such a process is parentage: the fork server is a PR_SET_CHILD_SUBREAPER
  and kills whatever reparents to it at the end of every execution. That bounds how long a
  survivor outlives its own execution. It does not bound the intra-execution window, and it
  is unverified under gVisor, which implements prctl in the sentry.
* Forwarded audit records have trustworthy attribution and framing — a child cannot name
  another user or break the framing — but they are not an account of what a script did. A
  child can emit records for calls it never made, suppress its own records in-process, or
  flood its pipe. A zero-record summary means "this supervisor read no records"; db-api's and
  results-api's own endpoint_access lines are what hold under an assumption of compromise.
"""

import array
import base64
import ctypes
import errno
import hashlib
import http.server
import io
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
import urllib.parse
from collections import deque

# --------------------------------------------------------------------------------------
# Contract constants. docs/code-execution-security.md owns every wire value here.
# --------------------------------------------------------------------------------------

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8080

MAX_BODY_BYTES = 1024 * 1024          # raw bytes on the wire -> 413
MAX_CODE_BYTES = 256 * 1024           # len(code.encode("utf-8")) -> 413
MAX_HEADER_BYTES = 64 * 1024          # request line + headers, whole block -> 431
HEADER_PEEK_BYTES = 512               # see _HeaderBoundedReader: the over-read bound
HEAD_READ_TIMEOUT_S = 10.0            # first byte of the head to the blank line -> 408
IDLE_READ_TIMEOUT_S = 65.0            # connection open, no head started -> closed, no response
BODY_READ_TIMEOUT_S = 10.0            # end of head to last body byte -> 408
DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 120                   # rejected, never clamped
QUEUE_DEPTH = 2                       # WAITING requests, not counting the one executing
MAX_QUEUED_WAIT_S = 120.0             # the number the 300s token TTL constrains
RETRY_AFTER_S = 60
KILL_GRACE_S = 2.0                    # SIGTERM -> SIGKILL, timeout and output-cap paths

# The ceiling on the SIGTERM drain, which is what makes _shutdown_when_idle bounded. It sits
# between MAX_TIMEOUT_S + KILL_GRACE_S = 122s (what an execution already running when the
# SIGTERM landed may legitimately need, plus write time) and the manifest's
# terminationGracePeriodSeconds of 130 (shutdown, server_close and forkserver.close all run
# after this returns). A ceiling is needed at all because _send_json's sendall blocks on a
# connection deliberately left at settimeout(None): one peer that stops reading would
# otherwise hold the drain for tcp_retries2, ~15 minutes on this host.
DRAIN_DEADLINE_S = 125.0

# How long the supervisor keeps reading the pipes AFTER the child is reaped. A setsid()ed
# descendant inherits the write ends, so EOF is not something waiting longer can produce.
DRAIN_GRACE_S = 2.0

# The two output bounds. They are different limits and conflating them loses one.
PIPE_CAP_BYTES = 8 * 1024 * 1024      # stop reading the child's pipe here AND kill the group
RETURN_HEAD_BYTES = 32 * 1024         # first 32 KiB returned to the model
RETURN_TAIL_BYTES = 32 * 1024         # last 32 KiB — the traceback lives here
ELISION_MARKER = "\n...[{} bytes elided]...\n"   # fixed text; the 64 KiB budget EXCLUDES it

# The audit stream's read-end bounds. Every one is applied here, by the process the child
# cannot reach into, and none is keyed on anything the child writes: a ceiling keyed on
# SANDBOX_EXECUTION_ID was measured being reset by rewriting that variable, and, shared with
# the records that mattered, doubled as a suppression primitive.
#
# They bound the stream. They cannot stop a child denying attribution of its OWN later calls
# by flooding its own pipe, because the flood and the records share one channel. What they
# guarantee instead: the loss is counted, announced in supervisor framing on the pod's
# stdout, and charged to the flooder alone — every budget here is per execution, so one
# execution cannot silence another's records.
CHILD_AUDIT_FD = 4                    # the number the SDK is told to write on; see _child_main

# A record longer than this is dropped, never truncated: the tail is where the `rows:` field
# lives, so a truncation either no longer parses or parses as a DIFFERENT record than the
# child wrote. Replace-don't-truncate is this file's rule for identity-bearing text. Sized
# well above a real record: a ~120-byte prefix plus a function name and an argument summary
# whose values are individually capped at 64 characters.
AUDIT_LINE_MAX_BYTES = 4096

# The per-execution byte budget, counted over everything read off the fd including bytes
# then dropped (~5,000 records at a typical ~200 bytes). Past it the reader keeps reading and
# discards, deliberately unlike the output pipe: a reader that stopped would block the
# child's next audit write, turning an observability bound into an execution failure.
AUDIT_STREAM_MAX_BYTES = 1024 * 1024

# The rate cap, a token bucket over lines. Every record cost an HTTP round trip, so a genuine
# sustained rate above this needs ~100 concurrent in-flight SDK calls per second for the whole
# execution; the burst covers an asyncio.gather of a few dozen. Exceeding it loses records
# visibly, with a count.
AUDIT_RATE_PER_S = 100.0
AUDIT_RATE_BURST = 200

# The memory bound, sized against the POD's 3Gi limit and deliberately NOT read from
# /sys/fs/cgroup. Under the local Docker backend /scratch is a tmpfs whose page cache is
# charged to the same memory cgroup; in the pod it is a node-disk emptyDir charged to
# ephemeral-storage and never to limits.memory. Tuning to the local behaviour would be up to
# 512 MiB more conservative than the pod needs. The divergence is real: a script holding
# ~2.4 GiB while /scratch holds 400 MiB can be cgroup-OOM-killed locally and run fine in the
# pod. The headroom covers the fork server too, which is copy-on-write off the supervisor and
# costs a few MiB unless it starts allocating.
POD_MEMORY_LIMIT_BYTES = 3 * 1024 * 1024 * 1024
SUPERVISOR_MEMORY_HEADROOM_BYTES = 512 * 1024 * 1024
CHILD_RLIMIT_AS_BYTES = POD_MEMORY_LIMIT_BYTES - SUPERVISOR_MEMORY_HEADROOM_BYTES  # 2560 MiB

# RLIMIT_AS bounds virtual address space, not RSS, and a prewarmed child starts at VmSize
# ~1358 MiB against VmRSS ~113 MiB because BLAS reserves far more than it touches — so a
# script's own headroom under this limit is ~1.2 GiB, not ~2.5 GiB. Raising the limit to
# "fix" that spends the supervisor's headroom, which is the one thing keeping the cgroup OOM
# killer from choosing between them.
CHILD_OOM_SCORE_ADJ = 500

# A supervisor-side watch, not RLIMIT_NPROC: that limit is per real uid across the pid
# namespace and supervisor and child share uid 65532, so a child forking to its RLIMIT_NPROC
# would also stop the SUPERVISOR forking — the fork bomb takes out the supervisor instead of
# being contained. Sized from what a legitimate script needs and far below the kubelet's
# pod_pids_limit of 1024, which is the outer backstop and not a substitute.
PID_BUDGET = 32

# One thread polls the wall clock, the group size, the /scratch quotas and the aggregate.
# 0.2s is chosen against the wall clock: it is the one bound a client is told the exact value
# of, so the deadline is checked on its own timer, before and after the filesystem scan and
# never behind it. Quota overshoot is not what sizes this — at ~1 GiB/s to tmpfs a poll can
# miss ~200 MiB and no interval anybody would run makes that small. What bounds the retained
# footprint is _retain's trim; what keeps the volume under its sizeLimit is the aggregate.
WATCHDOG_POLL_S = 0.2

# The /scratch budget. Stated once, here, and nowhere else in this file.
#
# The emptyDir sizeLimit is 512Mi COMBINED artifact-plus-temp for every live and retained
# execution, and exceeding it EVICTS THE POD rather than failing the write.
#
#     RETAINED_ARTIFACTS_CEILING   256Mi   steady state, exact: every retained artifacts/ has
#                                          been trimmed to ARTIFACT_QUOTA before it counts
#   + EXECUTION_TOTAL_QUOTA        192Mi   the one live execution
#   = 448Mi
#   <= SCRATCH_AGGREGATE_CEILING   480Mi   = 512Mi - 32Mi for .supervisor and for filesystem
#                                          overhead the per-tree walks do not see
#
# 448 <= 480 is what makes the aggregate check a backstop rather than a second quota: the two
# per-part budgets cannot together reach it, so it only fires on overshoot.
#
# Sealing runs AFTER the trim and adds 28 bytes per file, but the quota is charged in
# st_blocks*512 + DIRENT_COST rather than apparent size, so the overshoot is at most one more
# 4 KiB block per file: ARTIFACT_ENTRY_BUDGET * 4096 = 4 MiB over the per-execution quota.
# _seal_retained adds the measured growth back into the cached row, so the 256Mi ceiling
# stays exact over sizes that are true.
#
# What this does not prove: the 32Mi reserve is a margin, not a bound. A poll can miss
# ~200 MiB of writes and a child that traps SIGTERM keeps writing for KILL_GRACE_S. What
# bounds those is how fast the writer is stopped and _retain deleting what the overshoot
# produced. The steady state is exact and sits 64Mi under the cliff; the transient peak
# during a hostile burst is not, and the aggregate check is what fires 32Mi early instead of
# letting the kubelet be the thing that notices.
ARTIFACT_QUOTA_BYTES = 64 * 1024 * 1024
EXECUTION_TOTAL_QUOTA_BYTES = 192 * 1024 * 1024
RETAINED_ARTIFACTS_CEILING_BYTES = 256 * 1024 * 1024
SCRATCH_SIZE_LIMIT_BYTES = 512 * 1024 * 1024   # the emptyDir sizeLimit; the cliff itself
SCRATCH_SUPERVISOR_RESERVE_BYTES = 32 * 1024 * 1024
SCRATCH_AGGREGATE_CEILING_BYTES = SCRATCH_SIZE_LIMIT_BYTES - SCRATCH_SUPERVISOR_RESERVE_BYTES
RETENTION_S = 5 * 60
REAPER_POLL_S = 30.0

# The TTL is a floor, not an instant. Deletion happens on a reaper tick, so a retained
# directory is present until RETENTION_S and gone by RETENTION_S + REAPER_POLL_S; in between
# it may be either. It can also go EARLIER: _enforce_retained_ceiling evicts oldest-first
# when a later completion pushes the retained aggregate over its ceiling. So a presence
# assertion at t < RETENTION_S is sound only while nothing else is retaining concurrently.
# A test should assert absence only at t >= RETENTION_S + REAPER_POLL_S plus its own margin,
# against the SANDBOX_RETENTION_S override rather than five real minutes.

# Zero-length files are not free, and charging only st_blocks says they are: 300,000 empty
# files under artifacts/ charged 8.6 MB against the 192 MiB quota, so no limit fired, while
# producing a 19.8 MB response and taking the supervisor's RSS from 22 MB to 166 MB. The real
# cost is an inode plus a directory entry — on the volume, in the manifest, in the response
# and in every later scan — so every entry is charged a floor, and a separate entry budget
# bounds the walk. The two are not redundant: the floor makes the byte quota honest, the
# entry budget is what keeps a scan from costing seconds.
DIRENT_COST_BYTES = 512
ARTIFACT_ENTRY_BUDGET = 1024          # entries directly under artifacts/ AND the manifest cap

# What retention costs in RAM, which the artifact ceiling does not bound because it charges
# st_size and 1024 zero-byte files measure 0. The per-execution digest map is bounded, but the
# NUMBER of retained executions was not, so an authenticated caller submitting fast executions
# that each create 1024 long-named empty artifacts accumulated ~0.5 MB apiece for the whole
# retention window with nothing able to evict it. Retention is therefore charged a memory cost
# as well as a disk one, and _enforce_retained_ceiling evicts oldest-first on whichever binds.
# The per-entry figures are deliberately generous: over-charging evicts earlier, under-charging
# is the failure being fixed.
RETAINED_DIGEST_ENTRY_COST_BYTES = 320
RETAINED_ROW_COST_BYTES = 512         # the row, the id string, the 32-byte artifact key and
                                      # the per-execution dict slots
RETAINED_STATE_CEILING_BYTES = 4 * 1024 * 1024

# The largest artifact GET /artifact will hand back, chosen against MAX_RESPONSE_BYTES rather
# than against what a plot needs: the body is base64 (+33%) inside a JSON envelope, so 512 KiB
# of file is ~700 KiB of body and stays clear of the 1 MiB cap. Reaching that cap instead
# would make _cap_response answer "response too large", which reads as a supervisor fault; a
# 413 here names the actual reason.
ARTIFACT_READ_MAX_BYTES = 512 * 1024
EXECUTION_ENTRY_BUDGET = 20000        # entries anywhere under /scratch/<id>; also the scan cap

# Cleanup and accounting must not be bounded by the budget they exist to restore. A live scan
# stops at EXECUTION_ENTRY_BUDGET because past that the exact count changes no decision — the
# tree is over either way. The trim and the retained-size measurement are the opposite case:
# they run after the kill, on a tree already over, and their job is to make the number true.
# Bounding them at 20,000 made them report a truncated sample as fact. Their ceiling exists
# only so a hostile or corrupted tree cannot make them unbounded, and it sits far above what
# a 512 MiB emptyDir can physically hold.
TRIM_SCAN_CHUNK = EXECUTION_ENTRY_BUDGET   # names one drain pass materialises
TRIM_ENTRY_CEILING = 4000000               # hard stop for the drain and for post-hoc sizing

# MAX_BODY_BYTES bounds what comes in; this bounds what goes out. Every component is
# separately capped, so this is a backstop that should never fire; _cap_response degrades
# rather than sending an unbounded body.
MAX_RESPONSE_BYTES = 1024 * 1024

MESSAGE_MAX_BYTES = 2048              # error.message
TRACEBACK_MAX_BYTES = 8192            # error.traceback, tail-capped

# `\Z` and fullmatch, not `$` and match: `$` also matches immediately before a final newline,
# so `^...$` accepts a trailing "\n" — which then names a directory and is echoed back in the
# response. Both anchors are kept so a later switch to .search() cannot quietly widen this.
EXECUTION_ID_RE = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
TOKEN_AUDIENCES = ("db-api", "results-api")

_EXECUTE_FIELDS = frozenset(
    {"code", "execution_id", "tokens", "user", "session_id", "timeout_s"}
)

# Reserved error.type names. error.type is an OPEN string — the child's exception class name
# is the other half of its range — and these are the reserved minimum a client may branch on.
#
# ERR_MEMORY_LIMIT is reserved and never emitted. The memory ceiling is RLIMIT_AS, applied by
# the child to itself and enforced by the kernel inside it, so what comes back is the child's
# own MemoryError with status "error". Re-labelling that would mean trusting the child to
# distinguish "the ceiling stopped me" from `raise MemoryError`, and _sanitise_error_type
# exists precisely not to trust the child with a reserved name. The name stays reserved so a
# script cannot forge it; clients detect memory exhaustion by matching MemoryError.
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

# error.type's other half is the child's exception class name, and the child is forked
# without exec, so the script writes that string. It needs the treatment `message` and
# `traceback` already get: a 60,000-character type reached the response, bypassing the 64 KiB
# output window, and a child writing {"type": "Timeout"} produced a forged reserved name with
# error.limit == null. A dotted qualname is the widest legitimate shape; anything else is
# reported as NonZeroExit rather than echoed, because the response is rendered to a model and
# this field is otherwise a free text channel out of the sandbox.
ERROR_TYPE_MAX_BYTES = 64
_ERROR_TYPE_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\Z")

# --------------------------------------------------------------------------------------
# Local-vs-pod knobs. sandbox/Dockerfile sets all of these in the image, so an unset value
# means "not running in the sandbox image" and is answered with a loud warning rather than a
# silent behaviour change. They exist for running supervisor.py out of a checkout.
# --------------------------------------------------------------------------------------

ENV_SCRATCH_ROOT = "SANDBOX_SCRATCH_ROOT"   # NOT set by the image; test-only override
ENV_MPLCACHE = "GENETICS_MPLCACHE"          # /genetics/mplcache
ENV_PREWARM = "GENETICS_PREWARM"            # /genetics/prewarm.py

# Test-only, set by neither the image nor the manifest: it lets the retention reaper be
# observed firing inside a test run instead of five minutes later. It can only SHORTEN
# retention — a value above RETENTION_S is refused rather than silently clamped, because
# artifacts outliving what read_artifact was told is worse than a startup error.
ENV_RETENTION_S = "SANDBOX_RETENTION_S"

# Where the supervisor writes the per-execution token file's path, in the CHILD's environment
# only. The SDK reads this variable, opens the file once and unlinks it.
ENV_TOKEN_FILE = "SANDBOX_TOKEN_FILE"
TOKEN_FILE_NAME = "tokens.json"

# Where the SDK looks up the audit fd's number. The number has to be in the child's
# environment for the SDK to find it, so the script finds it too and can write whatever it
# likes there — which is why everything that arrives is re-parsed and re-framed rather than
# believed.
ENV_AUDIT_FD = "GENETICS_SDK_AUDIT_FD"

DEFAULT_SCRATCH_ROOT = "/scratch"
NSSWITCH_PATH = "/etc/nsswitch.conf"

# The one /scratch entry the startup wipe keeps. After a restart the supervisor holds no
# record of which executions were live or retained, so nothing else under the root belongs to
# one, and a crash mid-execution must not leave a readable directory behind.
SUPERVISOR_DIR_NAME = ".supervisor"

# The child writes at most one JSON object here and nothing else. See _child_main.
CHILD_STATUS_FD = 3

# The status pipe carries one small JSON object. Unlike the output pipe this bound does not
# kill: past it the reader keeps reading and discards, so a child cannot block on a full
# status pipe. A longer record is a child misbehaving, not a limit worth reporting.
_STATUS_READ_LIMIT_BYTES = 64 * 1024

# The fork server's control socket carries only these four ops, and none of them carries a
# byte of user data — see _forkserver_main.
FS_OP_FORK = "fork"     # + 4 descriptors; answers {"pid": n}
FS_OP_WAIT = "wait"     # block until pid exits, WITHOUT consuming the zombie
FS_OP_REAP = "reap"     # consume the zombie; answers {"status": n} or {"running": true}
FS_OP_SWEEP = "sweep"   # kill+reap everything reparented here; answers
                        # {"swept": [live pids killed], "reaped": [zombie pids]}

# prctl(2). Marking the fork server a child subreaper is what gives the pod a handle on a
# descendant that called setsid() — see _fs_become_subreaper.
PR_SET_CHILD_SUBREAPER = 36

# Control messages are a fixed handful of ASCII bytes. The cap is a framing sanity bound, not
# a budget: anything larger on this socket is a bug or an attempt, and both should fail loudly.
FS_MSG_MAX_BYTES = 4096
# How long a control round trip may take before the supervisor concludes the fork server is
# wedged. FS_OP_WAIT is exempt — it blocks for the child's whole lifetime by design.
FS_CONTROL_TIMEOUT_S = 30.0
# The sweep re-enumerates, and these two bound how often. A stray reparents to the fork server
# only when its own parent exits, so a chain of depth n needs n passes — a grandchild that
# setsid()'d under a parent that also setsid()'d survived a single-pass sweep entirely.
# FS_SWEEP_MAX_ROUNDS is therefore the chain depth one execution clears, bounded rather than
# "loop until clean" so a stray forking a fresh decoy cannot spin the sweep. FS_SWEEP_BUDGET_S
# is the load-bearing bound: the sweep runs inside an FS_OP_SWEEP round trip, so exceeding
# FS_CONTROL_TIMEOUT_S would poison the control socket and take the fork server — and the
# pod's ability to execute anything — down with it.
FS_SWEEP_MAX_ROUNDS = 4
FS_SWEEP_BUDGET_S = 10.0

# How many zombies one SIGCHLD delivery may reap. The supervisor is PID 1, so anything that
# reparents PAST the fork server — which happens when the fork server is dead or when
# PR_SET_CHILD_SUBREAPER did not take — lands here, and unreaped each holds a slot against
# pod_pids_limit for the pod's lifetime. The cap is the point: this runs inside a signal
# handler, where an unbounded waitpid loop is its own hazard. It is safe because deliveries
# are not coalesced in practice — each child death raises the pending flag again, so a
# remainder is taken by the next delivery; the residual is a remainder with no later child
# death at all. Note 64 is not a pid bound: PID_BUDGET is enforced over process-group
# membership only, and a setsid() descendant leaves that group, so one execution can strand
# more than this, bounded only by pod_pids_limit.
ORPHAN_REAP_MAX_ROUNDS = 64
# The execution payload (code + env + cwd, as JSON) travels as an anonymous descriptor the
# fork server never reads. MAX_CODE_BYTES bounds the code; the rest is env and JSON overhead.
PAYLOAD_MAX_BYTES = MAX_CODE_BYTES + 64 * 1024

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
            # The path, never the tokens: /proc/<pid>/environ is readable by any process at
            # the same uid and supervisor and child share uid 65532, so a helper the script
            # spawns could read a token out of a sibling's environment. A path is not secret.
            ENV_TOKEN_FILE: self.tokens,
            # The number, not a path: _child_main dups the audit pipe's write end onto
            # exactly this fd before the script runs.
            ENV_AUDIT_FD: str(CHILD_AUDIT_FD),
            # The SDK renders these three into the line it writes, but they are NOT what
            # attributes a record: the supervisor discards the arriving prefix and re-stamps
            # from job.req.claims, because the child owns its own environment and can rewrite
            # all three between two calls. They are set for the SDK's line shape, for the
            # stubs that document them, and for in-process hosts that have no supervisor.
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

    Called at startup, where by construction there are none: what is live and what is retained
    lives in memory and does not survive the process. A crash mid-execution must not leave a
    readable directory behind, and after a restart nothing could resolve an artifact from
    before it anyway.
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
# Artifact encryption at rest
# --------------------------------------------------------------------------------------
#
# What this closes: /scratch is fully enumerable and readable by any process at the shared
# uid 65532 — measured from inside a second execution's child, which listed /scratch and read
# a previous execution's artifacts. Sealing every RETAINED artifact under a key that exists
# only in this process's memory closes that cross-execution read of a COMPLETED execution.
#
# What it does not close: the live window. The child writes plaintext with a raw open() into
# its own artifacts/ and there is no SDK chokepoint to intercept, so a same-uid resident
# reading DURING an execution is untouched for up to timeout_s + KILL_GRACE_S plus the
# reap-to-seal tail. Nor does it make the key unreachable: the key inherits exactly what
# _artifact_digests has — YAMA ptrace_scope=1 plus the supervisor being the child's ANCESTOR,
# not seccomp, since RuntimeDefault allows ptrace. What it adds is that the key is minted per
# execution and dies with the retained entry it belongs to.
#
# The key must never reach the fork server. ForkServer.start() runs once in bring_up(), before
# any key exists, so no key is in the snapshot every child is forked from. That holds only
# while _forkserver_main's rule holds: nothing per-execution travels the control socket.
#
# ctypes rather than `cryptography`, which would drag a bundled OpenSSL and a large Rust
# object into an image whose whole job is to be a security boundary, for a primitive the
# libcrypto already linked into this interpreter provides. The soname is hardcoded because
# ctypes.util.find_library returns None in a distroless image (no ldconfig, no gcc).
LIBCRYPTO_SONAME = "libcrypto.so.3"
ARTIFACT_KEY_BYTES = 32               # AES-256
GCM_NONCE_BYTES = 12                  # what GCM is defined over; another length costs a GHASH
GCM_TAG_BYTES = 16
ARTIFACT_ENVELOPE_BYTES = GCM_NONCE_BYTES + GCM_TAG_BYTES
CRYPT_CHUNK_BYTES = 1 << 20           # what the streaming seal holds in RAM at once
_ARTIFACT_KEY_ZEROS = bytes(ARTIFACT_KEY_BYTES)

_EVP_CTRL_AEAD_SET_IVLEN = 0x9
_EVP_CTRL_AEAD_GET_TAG = 0x10
_EVP_CTRL_AEAD_SET_TAG = 0x11

_LIBCRYPTO = None


class CryptoUnavailable(RuntimeError):
    """libcrypto is missing or does not behave. bring_up() raises rather than becoming ready.

    FAIL CLOSED AT THE POD, NOT AT THE EXECUTION. A supervisor that cannot seal would have to
    either retain plaintext or destroy every artifact it produces, and both are worse than a
    pod that never reports ready: a CrashLoopBackOff is something a deploy notices, and
    nobody's data sits in the clear while it is being noticed.
    """


class ArtifactCryptoError(Exception):
    """One artifact could not be sealed or opened. Carries no key material and no plaintext."""


def _libcrypto():
    """The loaded libcrypto with its argtypes declared. Cached; raises CryptoUnavailable.

    The argtypes are not optional: ctypes treats an unprototyped call as variadic, and the
    five-argument EVP_EncryptInit_ex then fails at the varargs boundary.
    """
    global _LIBCRYPTO
    if _LIBCRYPTO is not None:
        return _LIBCRYPTO
    try:
        lib = ctypes.CDLL(LIBCRYPTO_SONAME)
    except OSError as exc:
        raise CryptoUnavailable(f"cannot load {LIBCRYPTO_SONAME}: {exc}")
    ptr, cint = ctypes.c_void_p, ctypes.c_int
    ubytes = ctypes.POINTER(ctypes.c_ubyte)
    try:
        lib.EVP_CIPHER_CTX_new.restype, lib.EVP_CIPHER_CTX_new.argtypes = ptr, []
        lib.EVP_CIPHER_CTX_free.restype, lib.EVP_CIPHER_CTX_free.argtypes = None, [ptr]
        lib.EVP_aes_256_gcm.restype, lib.EVP_aes_256_gcm.argtypes = ptr, []
        for fname in ("EVP_EncryptInit_ex", "EVP_DecryptInit_ex"):
            fn = getattr(lib, fname)
            fn.restype, fn.argtypes = cint, [ptr, ptr, ptr, ubytes, ubytes]
        for fname in ("EVP_EncryptUpdate", "EVP_DecryptUpdate"):
            fn = getattr(lib, fname)
            fn.restype, fn.argtypes = cint, [ptr, ubytes, ctypes.POINTER(cint), ubytes, cint]
        for fname in ("EVP_EncryptFinal_ex", "EVP_DecryptFinal_ex"):
            fn = getattr(lib, fname)
            fn.restype, fn.argtypes = cint, [ptr, ubytes, ctypes.POINTER(cint)]
        lib.EVP_CIPHER_CTX_ctrl.restype = cint
        lib.EVP_CIPHER_CTX_ctrl.argtypes = [ptr, cint, cint, ptr]
    except AttributeError as exc:
        raise CryptoUnavailable(f"{LIBCRYPTO_SONAME} is missing an EVP symbol: {exc}")
    _LIBCRYPTO = lib
    return lib


def _as_ubytes(buf):
    """A POINTER(c_ubyte) VIEW of a writable buffer. No copy, so no second heap copy of a key
    or of somebody's plaintext outlives the in-place wipe of the buffer it came from.

    The length is the buffer's own, not `max(len(buf), 1)`: from_buffer requires the source to
    be at least as large as the array type, so asking for one byte of a zero-length bytearray
    raises ValueError. Callers that must not hand OpenSSL a zero-length update guard it.
    """
    return (ctypes.c_ubyte * len(buf)).from_buffer(buf)


def _gcm_context(lib, key, nonce, aad, encrypt):
    """An AES-256-GCM context with key, nonce and AAD absorbed. The caller frees it."""
    ctx = lib.EVP_CIPHER_CTX_new()
    if not ctx:
        raise ArtifactCryptoError("EVP_CIPHER_CTX_new failed")
    try:
        init = lib.EVP_EncryptInit_ex if encrypt else lib.EVP_DecryptInit_ex
        update = lib.EVP_EncryptUpdate if encrypt else lib.EVP_DecryptUpdate
        if init(ctx, lib.EVP_aes_256_gcm(), None, None, None) != 1:
            raise ArtifactCryptoError("EVP init (cipher) failed")
        if lib.EVP_CIPHER_CTX_ctrl(ctx, _EVP_CTRL_AEAD_SET_IVLEN, GCM_NONCE_BYTES, None) != 1:
            raise ArtifactCryptoError("EVP set ivlen failed")
        if init(ctx, None, None, _as_ubytes(key), _as_ubytes(nonce)) != 1:
            raise ArtifactCryptoError("EVP init (key) failed")
        if aad:
            aadbuf = bytearray(aad)
            absorbed = ctypes.c_int(0)
            if update(ctx, None, ctypes.byref(absorbed), _as_ubytes(aadbuf), len(aadbuf)) != 1:
                raise ArtifactCryptoError("EVP aad failed")
        return ctx
    except Exception:
        lib.EVP_CIPHER_CTX_free(ctx)
        raise


def artifact_aad(execution_id, name):
    """The associated data an artifact is sealed under: its execution id and its own name.

    Binding both is the point. Without it a sealed file could be moved between names inside one
    execution, or lifted into another execution's directory, and would still open and still
    match a digest. The separator is NUL, which cannot occur in either field.
    """
    return execution_id.encode("utf-8") + b"\x00" + name.encode("utf-8")


def new_artifact_key():
    """A fresh per-execution AES-256 key in a MUTABLE buffer, so it can be wiped in place.

    Read with readinto rather than os.urandom(): the latter returns an immutable `bytes` that
    stays on the heap where wiping the bytearray cannot reach it.
    """
    key = bytearray(ARTIFACT_KEY_BYTES)
    try:
        with open("/dev/urandom", "rb", buffering=0) as fh:
            got = fh.readinto(key)
    except OSError as exc:
        wipe_artifact_key(key)
        raise ArtifactCryptoError(f"cannot read /dev/urandom: {exc}")
    if got != ARTIFACT_KEY_BYTES:
        wipe_artifact_key(key)
        raise ArtifactCryptoError(f"/dev/urandom returned {got} bytes")
    return key


def wipe_artifact_key(key):
    """Zero a key in place — this file's idiom for sensitive buffers. Rebinding a `bytes`
    leaves the old object in an arena, which is why a key is never a `bytes`."""
    if key is not None:
        key[:] = _ARTIFACT_KEY_ZEROS


def _write_all(fd, data):
    """Write every byte of `data` or raise. os.write is allowed to write fewer.

    Every write in seal_artifact goes through this, not only the body's: a short write on the
    nonce, the final block or the tag produces a truncated sealed file that is then renamed
    over the plaintext, leaving the artifact permanently 409 with nothing raised.
    """
    view = memoryview(data)
    written = 0
    while written < len(view):
        written += os.write(fd, view[written:])


def seal_artifact(dfd, name, key, aad, chunk_bytes=CRYPT_CHUNK_BYTES):
    """Replace `name` under `dfd` with nonce || ciphertext || tag. (plaintext_size, digest).

    `digest` is the sha256 of the PLAINTEXT, or None when the plaintext is larger than
    ARTIFACT_READ_MAX_BYTES — the same None with the same meaning `_artifact_digest` returns.
    It is computed here because afterwards the plaintext no longer exists to hash.

    Streamed through a fixed buffer and swapped in by rename(), for three reasons:
      * memory: one artifact may be the whole 64 MiB quota, and holding it and its ciphertext
        would be a ~128 MiB transient in a 3 GiB pod;
      * atomicity: a seal that dies partway leaves the original untouched;
      * it breaks a setsid() escapee's existing write handle, since rename() gives the name a
        new inode. Such a process can open() the name again — same uid — and that write is
        detected by the tag rather than prevented.
    """
    lib = _libcrypto()
    tmp = ".seal-" + os.urandom(8).hex()
    nonce = bytearray(GCM_NONCE_BYTES)
    try:
        with open("/dev/urandom", "rb", buffering=0) as fh:
            if fh.readinto(nonce) != GCM_NONCE_BYTES:
                raise ArtifactCryptoError("short nonce")
    except OSError as exc:
        raise ArtifactCryptoError(f"cannot read /dev/urandom: {exc}")
    inbuf = bytearray(chunk_bytes)
    outbuf = bytearray(chunk_bytes + GCM_TAG_BYTES)
    inview, outview = memoryview(inbuf), memoryview(outbuf)
    tag = bytearray(GCM_TAG_BYTES)
    digest = hashlib.sha256()
    plaintext_size = 0
    ctx = None
    src = dst = None
    try:
        try:
            # O_NONBLOCK because this is a check-then-open window: the caller lstat'd a
            # regular file, and a same-uid peer that unlinks it and mkfifos it back would
            # block O_RDONLY in the kernel forever, on the completion path, holding the
            # execution slot with no timeout above it. A writerless fifo then reads EOF and
            # seals an empty file; a fifo with a queued writer seals what the peer wrote. The
            # digest recorded here is faithfully the digest of what is served either way. That
            # is inside the one-uid trust boundary this section already concedes — a plain
            # O_WRONLY|O_TRUNC by the same peer in the same window has the identical outcome —
            # so the flag destroys nothing that was not already destroyable, and the blocking
            # alternative is the denial it exists to prevent.
            src = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        except OSError as exc:
            raise ArtifactCryptoError(f"cannot open {name!r}: {exc}")
        try:
            dst = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                          dir_fd=dfd)
        except OSError as exc:
            raise ArtifactCryptoError(f"cannot create the sealed copy of {name!r}: {exc}")
        ctx = _gcm_context(lib, key, nonce, aad, encrypt=True)
        in_c, out_c = _as_ubytes(inbuf), _as_ubytes(outbuf)
        _write_all(dst, nonce)
        reader = io.FileIO(src, "r", closefd=False)
        produced = ctypes.c_int(0)
        while True:
            got = reader.readinto(inview)
            if not got:
                break
            digest.update(inview[:got])
            plaintext_size += got
            if lib.EVP_EncryptUpdate(ctx, out_c, ctypes.byref(produced), in_c, got) != 1:
                raise ArtifactCryptoError("EVP_EncryptUpdate failed")
            _write_all(dst, outview[:produced.value])
        if lib.EVP_EncryptFinal_ex(ctx, out_c, ctypes.byref(produced)) != 1:
            raise ArtifactCryptoError("EVP_EncryptFinal_ex failed")
        if produced.value:
            _write_all(dst, outview[:produced.value])
        if lib.EVP_CIPHER_CTX_ctrl(ctx, _EVP_CTRL_AEAD_GET_TAG, GCM_TAG_BYTES,
                                   ctypes.cast(_as_ubytes(tag), ctypes.c_void_p)) != 1:
            raise ArtifactCryptoError("EVP get tag failed")
        _write_all(dst, tag)
        # Checked, not swallowed by the finally: close() is where a deferred write error
        # surfaces, and an unchecked close is how a failed writeback becomes a truncated file
        # that the rename below swaps over the plaintext. Not fsynced, deliberately: the
        # emptyDir dies with the pod and retention is RETENTION_S, so nothing here has a
        # durability requirement, and a per-artifact sync on the completion path would hold
        # the execution slot for nothing.
        try:
            os.close(dst)
        except OSError as exc:
            dst = None
            raise ArtifactCryptoError(f"cannot close the sealed copy of {name!r}: {exc}")
        dst = None
        try:
            os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except OSError as exc:
            raise ArtifactCryptoError(f"cannot swap in the sealed copy of {name!r}: {exc}")
        tmp = None
    except OSError as exc:
        raise ArtifactCryptoError(f"sealing {name!r} failed: {exc}")
    finally:
        if ctx is not None:
            lib.EVP_CIPHER_CTX_free(ctx)
        for fd in (src, dst):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if tmp is not None:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
        # In place on every path including the raise: the chunk buffer holds the user's
        # plaintext and the nonce sits beside the key it was used with.
        inbuf[:] = bytes(len(inbuf))
        outbuf[:] = bytes(len(outbuf))
        nonce[:] = bytes(GCM_NONCE_BYTES)
    if plaintext_size > ARTIFACT_READ_MAX_BYTES:
        return plaintext_size, None
    return plaintext_size, digest.hexdigest()


def open_artifact(blob, key, aad):
    """The plaintext inside nonce || ciphertext || tag, or raise ArtifactCryptoError.

    The tag is an integrity check in its own right, verified before a byte is returned: a
    sealed file that was modified, renamed, moved to another execution or written under a
    different key does not open. read_artifact_bytes checks the manifest's sha256 on top.
    """
    if len(blob) < ARTIFACT_ENVELOPE_BYTES:
        raise ArtifactCryptoError("sealed artifact is shorter than its envelope")
    lib = _libcrypto()
    nonce = bytearray(blob[:GCM_NONCE_BYTES])
    body = bytearray(blob[GCM_NONCE_BYTES:len(blob) - GCM_TAG_BYTES])
    tag = bytearray(blob[len(blob) - GCM_TAG_BYTES:])
    out = bytearray(len(body) + GCM_TAG_BYTES)
    outview = memoryview(out)
    ctx = None
    try:
        ctx = _gcm_context(lib, key, nonce, aad, encrypt=False)
        produced = ctypes.c_int(0)
        total = 0
        # Skipped for an empty body: GCM over zero bytes of plaintext is well defined — the
        # tag is still computed over the AAD — and needs no update call. Making it anyway
        # builds a zero-length view of an empty buffer, where a 0-byte artifact used to die.
        if body:
            if lib.EVP_DecryptUpdate(ctx, _as_ubytes(out), ctypes.byref(produced),
                                     _as_ubytes(body), len(body)) != 1:
                raise ArtifactCryptoError("EVP_DecryptUpdate failed")
            total = produced.value
            produced.value = 0
        if lib.EVP_CIPHER_CTX_ctrl(ctx, _EVP_CTRL_AEAD_SET_TAG, GCM_TAG_BYTES,
                                   ctypes.cast(_as_ubytes(tag), ctypes.c_void_p)) != 1:
            raise ArtifactCryptoError("EVP set tag failed")
        if lib.EVP_DecryptFinal_ex(ctx, _as_ubytes(out), ctypes.byref(produced)) != 1:
            raise ArtifactCryptoError("authentication failed")
        return bytes(outview[:total + produced.value])
    finally:
        if ctx is not None:
            lib.EVP_CIPHER_CTX_free(ctx)
        out[:] = bytes(len(out))
        body[:] = bytes(len(body))
        nonce[:] = bytes(GCM_NONCE_BYTES)


def crypto_selftest(directory):
    """Seal and open a probe in `directory`; prove a flipped byte and a wrong key are refused.

    Run at startup, before ready: the alternative is discovering at the end of somebody's
    execution that artifacts cannot be sealed, at the one moment when every outcome is bad. It
    exercises the real file path — open, stream, rename — so a filesystem that cannot support
    the swap fails here too. Raises CryptoUnavailable, which bring_up() does not catch.
    """
    key = other = None
    name = ".crypto-selftest"
    path = os.path.join(directory, name)
    probe = b"supervisor artifact seal selftest" * 4
    try:
        key = new_artifact_key()
        aad = artifact_aad("00000000-0000-4000-8000-000000000000", name)
        with open(path, "wb") as fh:
            fh.write(probe)
        dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            size, digest = seal_artifact(dfd, name, key, aad)
        finally:
            os.close(dfd)
        with open(path, "rb") as fh:
            sealed = fh.read()
        if size != len(probe) or digest != hashlib.sha256(probe).hexdigest():
            raise CryptoUnavailable("the seal reported the wrong plaintext size or digest")
        if len(sealed) != len(probe) + ARTIFACT_ENVELOPE_BYTES:
            raise CryptoUnavailable(
                f"a sealed artifact is {len(sealed)} bytes for {len(probe)} of plaintext; "
                f"the envelope is supposed to cost exactly {ARTIFACT_ENVELOPE_BYTES}")
        if open_artifact(sealed, key, aad) != probe:
            raise CryptoUnavailable("AES-256-GCM round trip did not return the plaintext")
        flipped = bytearray(sealed)
        flipped[GCM_NONCE_BYTES] ^= 0x01
        tagged = bytearray(sealed)
        tagged[-1] ^= 0x01
        wrong_aad = artifact_aad("00000000-0000-4000-8000-000000000000", "other")
        other = new_artifact_key()
        for blob, use, why in ((bytes(flipped), key, "a flipped ciphertext byte"),
                               (bytes(tagged), key, "a flipped tag byte"),
                               (sealed, other, "a wrong key"),
                               (sealed, key, "a wrong name in the AAD")):
            aad_here = wrong_aad if why.endswith("AAD") else aad
            try:
                open_artifact(blob, use, aad_here)
            except ArtifactCryptoError:
                continue
            raise CryptoUnavailable(f"{why} was accepted; the tag is not being checked")
        # The zero-byte boundary is part of the gate. An empty artifact is ordinary — a
        # result frame with no rows, a log nothing wrote to — and it takes a different path
        # through the ctypes layer than the probe above, where the open used to raise
        # ValueError out of a zero-length buffer view with nothing catching it.
        empty_name = ".crypto-selftest-empty"
        empty_path = os.path.join(directory, empty_name)
        empty_aad = artifact_aad("00000000-0000-4000-8000-000000000000", empty_name)
        try:
            with open(empty_path, "wb"):
                pass
            dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                size, digest = seal_artifact(dfd, empty_name, key, empty_aad)
            finally:
                os.close(dfd)
            with open(empty_path, "rb") as fh:
                empty_sealed = fh.read()
            if size != 0 or digest != hashlib.sha256(b"").hexdigest():
                raise CryptoUnavailable(
                    "a zero-byte artifact reported the wrong plaintext size or digest")
            if len(empty_sealed) != ARTIFACT_ENVELOPE_BYTES:
                raise CryptoUnavailable(
                    f"a sealed zero-byte artifact is {len(empty_sealed)} bytes; the envelope "
                    f"is supposed to cost exactly {ARTIFACT_ENVELOPE_BYTES}")
            if open_artifact(empty_sealed, key, empty_aad) != b"":
                raise CryptoUnavailable("a zero-byte artifact did not open to zero bytes")
        finally:
            try:
                os.unlink(empty_path)
            except OSError:
                pass
    except ArtifactCryptoError as exc:
        raise CryptoUnavailable(f"artifact encryption is not usable: {exc}")
    finally:
        wipe_artifact_key(key)
        wipe_artifact_key(other)
        try:
            os.unlink(path)
        except OSError:
            pass


# --------------------------------------------------------------------------------------
# The artifact manifest
# --------------------------------------------------------------------------------------

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _name_is_retrievable(name):
    """Would read_artifact be able to open a file with this name?

    A manifest must never advertise a name the read cannot open: the executor strips the name
    before validating, so "plot.png " would pass every other rule, get listed, and then be
    unretrievable behind the same "Artifact not found" a name that never existed gets.
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


def _artifact_digest(dfd, name, max_bytes=ARTIFACT_READ_MAX_BYTES):
    """sha256 of `name` under `dfd`, or None when it can never be served verified.

    None has one meaning at every call site — "this file cannot be bound to what the manifest
    advertised" — and read_artifact_bytes refuses on it. Two things produce it: a failed open
    or read, and a file larger than the read cap. The second is deliberate: such a file gets
    413 anyway, and if a same-uid process later truncates it under the cap, "unverifiable" is
    the right answer rather than serving whatever it now contains.

    Opened relative to `dfd` with O_NOFOLLOW, or a path-based open would re-admit the symlink
    route the directory fd exists to close, and O_NONBLOCK so a fifo swapped in by a same-uid
    peer cannot block the manifest build forever.

    It does NOT return None for that fifo: a writerless fifo read non-blocking gives EOF, so
    this returns sha256(b""), which would match an empty regular file swapped in before the
    read. What makes that moot is narrow: the `sealed is None` branch of build_manifest has no
    production caller, so this runs only in tests. A caller that reaches it with a live
    artifacts directory has to close that gap here first.
    """
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except OSError:
        return None
    try:
        digest = hashlib.sha256()
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining <= 0:
            return None  # over the read cap: 413 territory, never servable
        return digest.hexdigest()
    except OSError:
        return None
    finally:
        os.close(fd)


def build_manifest(artifacts_dir, max_entries=ARTIFACT_ENTRY_BUDGET,
                   scan_limit=EXECUTION_ENTRY_BUDGET, sealed=None):
    """(entries, omitted, digests). Lists a file only if it would survive read_artifact's checks.

    `sealed` is what seal_retained_artifacts measured BEFORE it encrypted:
    {name: (plaintext size, plaintext sha256 or None)}. Passing it changes two things:

      * `size` and the digest are the PLAINTEXT's, so the manifest goes on being a statement
        about the bytes a caller will receive and nothing downstream changes meaning because
        the file on disk grew an envelope;
      * a name that is not in the map is OMITTED, because after the seal pass every
        retrievable file is in it — so such a name was created afterwards, by a setsid()
        escapee or another process at the shared uid.

    None means the directory is plaintext and is hashed from disk, which is what callers
    outside _execute_inner want. Forgetting it on a sealed directory fails closed: the digests
    would be over ciphertext and every read would answer 409.

    `digests` is a security control, not a cache. It maps each listed name to the sha256 of
    the bytes present when the manifest was built, and it is kept in the SUPERVISOR'S MEMORY,
    never on the filesystem, because the failure it answers is that /scratch is writable by
    the process being defended against. What keeps it out of the child's reach is YAMA
    (ptrace_scope=1) and not the seccomp profile: RuntimeDefault allows ptrace, and Yama
    refuses PTRACE_MODE_ATTACH only because the supervisor is the child's ancestor. Measured
    under both runsc and runc: sibling -> sibling is EPERM, parent -> own child is allowed.
    Under runsc, uid 0 could write ptrace_scope=0 (gVisor does not create the read-only
    /proc/sys mount runc uses), so "no uid 0 in the sandbox" is load-bearing here and for the
    artifact key, not hygiene. This function checks none of it.

    Both bounds are load-bearing and they are different. `max_entries` bounds the RESPONSE:
    300,000 zero-length files produced a 19.8 MB body from a tree that tripped no quota.
    `scan_limit` bounds THIS FUNCTION, which runs after the child is reaped while holding the
    execution slot, and where os.listdir materialises every name before the first is examined.
    Past `scan_limit` the directory is not enumerated further and `omitted` becomes a floor
    rather than a count, which is logged. The hashing itself is bounded by the quota: _retain
    trims artifacts/ first, `max_entries` bounds how many files are hashed, and
    _artifact_digest reads at most ARTIFACT_READ_MAX_BYTES + 1 from each.
    """
    entries = []
    omitted = 0
    digests = {}
    try:
        dfd = os.open(artifacts_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return [], 0, {}
    try:
        seen = 0
        # The directory fd, not the path: a path-based scandir would undo the O_NOFOLLOW open.
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
            # Regular files directly in the directory only: no recursion, no symlinks, no
            # fifos, sockets or devices, and st_nlink == 1.
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                omitted += 1
                continue
            if sealed is None:
                size, digest = st.st_size, _artifact_digest(dfd, name)
            else:
                record = sealed.get(name)
                if record is None:
                    omitted += 1
                    continue
                size, digest = record
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            entries.append({"name": name, "size": size, "content_type": ctype})
            digests[name] = digest
    finally:
        os.close(dfd)
    if seen >= scan_limit:
        LOG.error("artifacts/ holds at least %d entries; the manifest stopped enumerating and "
                  "artifacts_omitted is a floor, not a count", seen)
    entries.sort(key=lambda e: e["name"])
    return entries, omitted, digests


class _DigestsUnset:
    """The default for read_artifact_bytes' `expected_digests`, and it is not a value.

    A default that disables a security check points the wrong way. `None` still means "no
    binding", but it now has to be written, so a caller that forgets the argument gets a
    TypeError instead of silently serving unverified bytes.
    """


def read_artifact_bytes(artifacts_dir, name, max_bytes=ARTIFACT_READ_MAX_BYTES,
                        expected_digests=_DigestsUnset, key=None, execution_id=None):
    """(bytes, content_type) for one artifact, or raise RequestError.

    The checks run here, inside the sandbox, against the directory the child actually wrote
    to — which is the point of serving this over HTTP rather than letting chat-backend open a
    path. They are build_manifest's checks in the same order, so nothing the manifest
    advertised is unretrievable and nothing it withheld becomes reachable by asking directly:
    a retrievable bare name; a directory fd opened O_NOFOLLOW with the file opened relative to
    it, so neither can be a symlink out of /scratch/<id>; O_NONBLOCK so a fifo left where a
    regular file was listed cannot hang the open before any check runs; regular file with
    st_nlink == 1.

    Not-found is deliberately indistinguishable across "no such name", "not a regular file"
    and "the open failed": a caller learns only whether the artifact it was told about is
    there, which is all a probe should get.

    `expected_digests` is the integrity binding: the name must be one build_manifest listed,
    and the bytes must still hash to what they hashed to then. A file PLANTED by a same-uid
    process after the execution finished is not in the map and is refused as not-found before
    it is opened; a file whose CONTENT was replaced is refused as 409 ArtifactModified, which
    is a different answer on purpose — a caller holding a legitimate execution id is entitled
    to know the answer is "this is no longer what you were told about" rather than a 404 it
    would read as an expired retention window. None disables the binding and is not a
    production setting; it has to be passed explicitly (see _DigestsUnset).

    `key` and `execution_id` are the seal, both or neither. With them the file on disk is
    nonce || ciphertext || tag and is opened here under the per-execution key that never left
    this address space. A missing key is not a fail-open: if the pass did run, the bytes on
    disk are ciphertext whose sha256 is not the plaintext digest the manifest recorded, so the
    read answers 409.

    The cap applies to the plaintext, not to the file. ARTIFACT_READ_MAX_BYTES bounds the
    RESPONSE, so charging the envelope against it would newly 413 an artifact that fitted
    before encryption. A sealed file is allowed ARTIFACT_ENVELOPE_BYTES more on disk and the
    plaintext is re-checked after it is opened.
    """
    if (key is None) != (execution_id is None):
        raise TypeError("read_artifact_bytes: key and execution_id go together; one without "
                        "the other cannot build the AAD the artifact was sealed under")
    if expected_digests is _DigestsUnset:
        raise TypeError("read_artifact_bytes: pass expected_digests explicitly; None disables "
                        "the integrity binding and must be chosen, not defaulted into")
    if not _name_is_retrievable(name):
        raise RequestError(400, "InvalidRequest", "not a retrievable artifact name")
    if expected_digests is not None and name not in expected_digests:
        # Not merely "unknown": a name on disk but not in the manifest was put there by
        # something that is not this execution's child, which was reaped before it was built.
        raise RequestError(404, "NotFound", "no such artifact")
    try:
        dfd = os.open(artifacts_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise RequestError(404, "NotFound", "no such artifact")
    try:
        try:
            # O_NONBLOCK is a control, not tidiness. Without it this open blocks forever on a
            # fifo with no writer — O_RDONLY on a fifo waits for one, and the fstat that
            # rejects non-regular files runs after the open — so a same-uid process that
            # replaces a listed name with a fifo during the retention window hangs the serving
            # thread and the chat turn behind it. expected_digests narrows the hazard but does
            # not close it, because the replacement happens after the manifest was built. No
            # re-open is needed: the flag only has to let the open return, after which S_ISREG
            # refuses it in the ordinary way, and it is inert for regular files.
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        except OSError:
            raise RequestError(404, "NotFound", "no such artifact")
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise RequestError(404, "NotFound", "no such artifact")
            envelope = 0 if key is None else ARTIFACT_ENVELOPE_BYTES
            if st.st_size > max_bytes + envelope:
                raise RequestError(
                    413,
                    "ArtifactTooLarge",
                    f"artifact is {st.st_size - envelope} bytes; the limit is {max_bytes}",
                )
            # Bounded by max_bytes and not by st_size: a setsid() escapee still holding a
            # write handle can grow the file between the stat and the read.
            chunks = []
            remaining = max_bytes + envelope + 1
            while remaining > 0:
                chunk = os.read(fd, min(remaining, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)
    data = b"".join(chunks)
    if len(data) > max_bytes + (0 if key is None else ARTIFACT_ENVELOPE_BYTES):
        raise RequestError(
            413, "ArtifactTooLarge", f"artifact exceeds the {max_bytes} byte limit"
        )
    if key is not None:
        try:
            data = open_artifact(data, key, artifact_aad(execution_id, name))
        except Exception as exc:
            # The tag failing means what a digest mismatch means: these are not the bytes the
            # manifest described. It is the stronger of the two checks — it also catches a
            # whole sealed file lifted from another name or execution — and is reported as the
            # same 409 so a caller need not distinguish two flavours of "this moved".
            #
            # The catch is a property, not an enumeration: naming (ArtifactCryptoError,
            # CryptoUnavailable) let a ValueError out of the ctypes layer run past every arm
            # into socketserver.handle_error, which closes the socket with no status line. The
            # property is "the crypto layer cannot open this", whatever it raised.
            LOG.error("artifact %r for execution %s did not authenticate; refusing to serve "
                      "it: %s", name, execution_id, exc)
            raise RequestError(
                409, "ArtifactModified",
                "artifact no longer matches the manifest and will not be served",
            )
        if len(data) > max_bytes:
            raise RequestError(
                413, "ArtifactTooLarge", f"artifact exceeds the {max_bytes} byte limit"
            )
    if expected_digests is not None:
        # Over the bytes that would be RETURNED, not over the file: the two differ once
        # something has grown it, and it is the returned bytes a caller would trust.
        want = expected_digests.get(name)
        if want is None or hashlib.sha256(data).hexdigest() != want:
            LOG.error("artifact %r no longer matches the manifest it was listed in; refusing "
                      "to serve it", name)
            raise RequestError(
                409, "ArtifactModified",
                "artifact no longer matches the manifest and will not be served",
            )
    return data, mimetypes.guess_type(name)[0] or "application/octet-stream"


def _purge_artifacts(artifacts_dir):
    """Delete every entry directly under artifacts/. Returns (removed, emptied).

    `emptied` is the part a caller must not ignore: a count alone cannot tell "destroyed
    everything" from "destroyed nothing", and a same-uid peer chmod 0500 on artifacts/ between
    the retain and the seal produced "destroyed 0" over two files still in the clear. False
    means "plaintext may still be on disk", and every caller has to answer it.

    This is the fail-closed arm of the seal pass: if sealing could not complete, the outcomes
    not allowed are "plaintext left on disk and retained" and "the caller is told nothing".
    The count returned is added to artifacts_omitted, which already means "present but not
    listed".

    It drains in bounded passes rather than deleting as it streams. Materialising the whole
    directory does not survive a child that made 300,000 entries, and deleting entries while a
    readdir is in progress may skip entries POSIX never promised to return — which here means
    leaving plaintext behind on the one path whose job is to remove it.
    """
    removed = 0
    seen = 0
    while True:
        names = list(_iter_dir_names(artifacts_dir, TRIM_SCAN_CHUNK))
        if not names:
            return removed, True
        seen += len(names)
        dropped = 0
        for name in names:
            path = os.path.join(artifacts_dir, name)
            try:
                is_dir = stat.S_ISDIR(os.stat(path, follow_symlinks=False).st_mode)
            except OSError:
                continue
            if _remove_entry(path, is_dir):
                dropped += 1
        removed += dropped
        if dropped == 0 or seen > TRIM_ENTRY_CEILING:
            LOG.error("could not empty %s: %d entries seen, %d deleted, and the last pass "
                      "removed %d — artifacts may still be in the clear", artifacts_dir,
                      seen, removed, dropped)
            return removed, False


def seal_retained_artifacts(artifacts_dir, execution_id, key,
                            scan_limit=EXECUTION_ENTRY_BUDGET):
    """Encrypt everything retained under artifacts/. (sealed, purged, growth, stranded).

    Runs between _retain and build_manifest on a quiescent tree: the child is reaped, the
    drains are joined, the non-artifact directories are gone and the quota trim has run. It is
    the only moment at which the supervisor knows the artifacts are complete and still holds
    the execution slot.

    What is not sealed is DELETED, and that is what makes the property true rather than nearly
    true. A subdirectory's contents, a name with a control character, a hard link and a symlink
    are all things no caller can retrieve, so retaining them buys nobody anything and leaves
    exactly the plaintext this pass exists to remove. Their count reaches the response through
    artifacts_omitted.

    A failure is localised to one file wherever it can be: an unsealable file is removed and
    counted into `purged`, and the pass carries on. Raising instead made one chmod 000 file
    destroy three readable artifacts behind a 200 with an empty manifest — and ENOSPC is a
    realistic version of the same thing, since the seal writes a full temporary copy into an
    emptyDir that may already hold 256Mi of retained trees. What still raises is what cannot be
    localised: the directory not opening, more entries than `scan_limit` (everything past the
    bound is unexamined and therefore possibly plaintext), and CryptoUnavailable.

    `stranded` counts entries that are neither sealed nor removed — an unsealable file whose
    unlink also failed. It is the one outcome that leaves plaintext on disk, so it is returned
    rather than logged and the caller decides what the wire says.

    `growth` is what the envelopes cost in the same st_blocks accounting _dir_usage uses, so
    the caller can correct the retained size it cached before this ran.
    """
    sealed = {}
    purged = 0
    growth = 0
    stranded = 0
    dfd = os.open(artifacts_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Materialised first, unlike build_manifest: this pass unlinks and renames over
        # entries, and a readdir running across those changes may skip entries — which here
        # means leaving plaintext. Bounded by scan_limit.
        names = list(_iter_dir_names(dfd, scan_limit + 1))
        if len(names) > scan_limit:
            # Not a partial success: everything past the bound is unexamined and therefore
            # possibly plaintext.
            raise ArtifactCryptoError(
                f"artifacts/ still holds more than {scan_limit} entries after the trim; "
                f"the seal pass cannot account for all of them")
        for name in names:
            path = os.path.join(artifacts_dir, name)
            try:
                st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            except OSError as exc:
                # A name that would not stat is still an entry. `continue` here left it
                # outside both halves of "what is not sealed is deleted" and outside
                # artifacts_omitted — neither sealed, purged, nor counted.
                LOG.warning("execution %s: %r could not be examined for sealing (%s); "
                            "removing it", execution_id, name, exc)
                if _remove_entry(path, False):
                    purged += 1
                else:
                    stranded += 1
                continue
            if (not _name_is_retrievable(name) or not stat.S_ISREG(st.st_mode)
                    or st.st_nlink != 1):
                if _remove_entry(path, stat.S_ISDIR(st.st_mode)):
                    purged += 1
                else:
                    stranded += 1
                continue
            before = st.st_blocks * 512
            try:
                size, digest = seal_artifact(dfd, name, key, artifact_aad(execution_id, name))
            except (ArtifactCryptoError, OSError) as exc:
                LOG.error("execution %s: could not seal %r (%s); deleting it rather than "
                          "retaining it in the clear, and the rest of the execution's "
                          "artifacts are unaffected",
                          execution_id, name, exc)
                if _remove_entry(path, False):
                    purged += 1
                else:
                    stranded += 1
                continue
            sealed[name] = (size, digest)
            try:
                growth += os.stat(name, dir_fd=dfd,
                                  follow_symlinks=False).st_blocks * 512 - before
            except OSError:
                growth += ARTIFACT_ENVELOPE_BYTES
    finally:
        os.close(dfd)
    return sealed, purged, growth, stranded


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


def _relocate_above(fd, ceiling):
    """A copy of `fd` numbered above `ceiling`, or `fd` itself if it already is.

    The kernel picks the pipe numbers, so status_w or audit_w can legitimately BE 3 or 4 —
    in which case `dup2(status_w, CHILD_STATUS_FD)` silently closes the other pipe's write
    end and the SDK writes its audit records into a closed descriptor. `os.dup` returns the
    LOWEST free number, so it can hand back another fd inside the fixed range; the loop keeps
    each intermediate open (which is what makes the next dup pick a higher one) and closes
    them once a number above the range is reached. The original is left to
    _close_inherited_fds a few lines later, along with everything else not in the keep-set.
    """
    walked = []
    while fd <= ceiling:
        walked.append(fd)
        fd = os.dup(fd)
    for spare in walked[1:]:
        os.close(spare)
    return fd


def _read_payload(fd):
    """(code, env, cwd) out of the anonymous descriptor the supervisor filled.

    The child reads this; the fork server never does. Passing the code through the forking
    process as a Python string would leave it in arenas that copy-on-write hands to the next
    user's child — a /proc/self/mem scan recovered strings from executions already completed.
    A descriptor means the bytes are never in that address space at all.
    """
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    total = 0
    while True:
        block = os.read(fd, 65536)
        if not block:
            break
        total += len(block)
        if total > PAYLOAD_MAX_BYTES:
            raise ValueError("execution payload is over its cap")
        chunks.append(block)
    payload = json.loads(b"".join(chunks).decode("utf-8"))
    return payload["code"], payload["env"], payload["cwd"]


def _payload_fd(payload, fallback_dir):
    """An anonymous, seekable descriptor holding `payload` as JSON, positioned anywhere.

    memfd_create is the wanted shape: no name, no filesystem, nothing for a resident process
    from an earlier execution to open. The fallback creates and immediately unlinks a 0600
    file in the execution's own directory, reaching the same anonymous-inode end state through
    a name that exists for microseconds. It exists so a host without memfd_create degrades
    rather than fails; the image has it.
    """
    raw = json.dumps(payload).encode("utf-8")
    fd = None
    try:
        fd = os.memfd_create("sandbox-execution", getattr(os, "MFD_CLOEXEC", 0))
    except (AttributeError, OSError):
        fd = None
    if fd is None:
        path = os.path.join(fallback_dir, ".payload-%s" % base64.urlsafe_b64encode(
            os.urandom(9)).decode("ascii"))
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.unlink(path)
        except OSError:
            os.close(fd)
            raise
    try:
        os.write(fd, raw)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _child_main(payload_fd, out_w, status_w, audit_w):
    """Runs in the forked child. Never returns.

    It takes a descriptor, not the code: the fork server is forbidden to hold user data of any
    kind, so `code`, `env` and `cwd` arrive as JSON on an anonymous descriptor passed through
    without being read.

    The child reports its exception on a DEDICATED STATUS PIPE carrying exactly one JSON
    object, rather than by having the supervisor parse the tail of `output`. That tail is
    subject to the 64 KiB head-and-tail cap, so a traceback in a chatty script is exactly what
    gets elided; and a script can print anything, so parsing stdout lets it forge its own error
    object. The status pipe narrows that but does not close it — the child is forked and not
    exec'd, so the script runs with this fd open — so the supervisor treats what arrives as
    untrusted: it re-caps it, and discards it outright when the child exited 0. A child that is
    killed writes nothing, which the contract anticipates: type "Killed", null traceback.

    The audit fd is the same arrangement for the same reason: the script can write anything to
    it, so the supervisor re-parses and re-frames every record on the read end.
    """
    exit_code = 0
    try:
        os.setsid()  # own session and process group, so the watchdog can signal the whole group
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGPIPE):
            try:
                signal.signal(sig, signal.SIG_DFL)
            except (OSError, ValueError):
                pass

        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, 0)
        if devnull > 2:
            os.close(devnull)
        # One pipe for stdout and stderr together: the contract budgets ONE 64 KiB window for
        # what reaches the model, and splitting it either halves the window or doubles it.
        os.dup2(out_w, 1)
        os.dup2(out_w, 2)
        fixed = max(CHILD_STATUS_FD, CHILD_AUDIT_FD)
        status_w = _relocate_above(status_w, fixed)
        audit_w = _relocate_above(audit_w, fixed)
        # Same collision as the pipes: the kernel may have numbered the payload 3 or 4, in
        # which case the dup2 below would close the execution's own code out from under it.
        payload_fd = _relocate_above(payload_fd, fixed)
        os.dup2(status_w, CHILD_STATUS_FD)
        # A second fixed number, for the SDK's audit records only. It must be in the keep-set
        # below or every record the SDK emits raises inside a successful data call.
        os.dup2(audit_w, CHILD_AUDIT_FD)
        os.set_inheritable(CHILD_STATUS_FD, True)
        os.set_inheritable(CHILD_AUDIT_FD, True)
        _close_inherited_fds({CHILD_STATUS_FD, CHILD_AUDIT_FD, payload_fd})

        # After the status fd is wired, so a malformed or over-cap payload is reported as a
        # StartupFailure the caller can see rather than a silent exit 70.
        code, env, cwd = _read_payload(payload_fd)
        os.close(payload_fd)

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
            # sys.exit(3) and friends: no uncaught exception, so no status record. The
            # supervisor reports NonZeroExit with the code.
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


# --------------------------------------------------------------------------------------
# The fork server
#
# One property, not a bundle: THE PROCESS THAT CALLS os.fork() TO MAKE AN EXECUTION CHILD MUST
# NEVER HAVE HELD A TOKEN, A REQUEST BODY OR ANOTHER USER'S SOURCE CODE. Four routes were
# demonstrated by which a forked child read exactly those out of the supervisor's inherited
# address space — a module global, a frame walk to `job.req.tokens`, gc.get_objects(), and a
# raw scan of /proc/self/mem that recovered a token from an execution already completed and
# released. The fourth is why nothing reference-shaped can fix this: Python strings are
# immutable and freed objects stay in arenas that copy-on-write hands to the child, so `del`,
# __slots__ and overwriting all fail. The only thing that works is never letting the bytes
# into the process that forks.
#
# So this process is forked out of the supervisor at startup, after prewarm() and before the
# first byte of the first request body is read. The second half of that takes two mechanisms,
# because the HTTP server is already serving during bring_up(): _Handler._execute refuses on
# `not SUPERVISOR.accepting()` before _read_body, so no Python object is built, and
# _HeaderBoundedReader consumes only the request head, so the body never leaves the kernel
# receive queue. Its address space is a snapshot of a supervisor that has never seen a user.
# Per execution it receives exactly one control message plus four descriptors and reads none
# of them: it does not learn the execution id, the user, the session, the code or the paths.
#
# Why prewarm survives, which is why this was chosen over exec-after-fork: the fork server
# inherits the prewarmed numpy/scipy/polars/matplotlib pages and passes them to every child
# copy-on-write. The cost is one long-lived process whose pages are shared, not copied.
#
# What it does not do on its own. Cross-execution artifact access: the integrity half is
# closed (build_manifest hashes, read_artifact re-verifies), the READING half is not and is
# not closable under one uid. A setsid() resident: the fork server is a child subreaper and
# sweeps what reparents to it, so a resident no longer survives into the NEXT execution, but
# it is alive during its own and the sweep is unverified under gVisor.
# --------------------------------------------------------------------------------------


def _fs_send(sock, obj, fds=()):
    raw = json.dumps(obj).encode("utf-8")
    if len(raw) > FS_MSG_MAX_BYTES:
        raise ValueError("fork server control message is over its cap")
    if fds:
        sock.sendmsg([raw], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", list(fds)))])
    else:
        sock.sendmsg([raw])


def _fs_recv(sock, maxfds=0):
    """(message, fds) or (None, []) at EOF.

    SOCK_SEQPACKET, so one sendmsg is one recvmsg and there is no framing to get wrong. A
    truncated control message (MSG_CTRUNC) closes whatever descriptors did arrive and raises:
    a half-delivered fd set is not something to carry on from.
    """
    space = socket.CMSG_SPACE(maxfds * array.array("i").itemsize) if maxfds else 0
    raw, ancdata, flags, _ = sock.recvmsg(FS_MSG_MAX_BYTES, space)
    fds = []
    for level, type_, data in ancdata:
        if level == socket.SOL_SOCKET and type_ == socket.SCM_RIGHTS:
            got = array.array("i")
            got.frombytes(data[: len(data) - (len(data) % got.itemsize)])
            fds.extend(got)
    if flags & getattr(socket, "MSG_CTRUNC", 0):
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        raise ValueError("fork server control message was truncated")
    if not raw:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
        return None, []
    return json.loads(raw.decode("utf-8")), fds


def _fs_close_all(fds):
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def _forkserver_main(sock):
    """Runs in the fork server. Never returns.

    Nothing in this loop may start holding user data. The control message is a fixed op name;
    the payload arrives as a descriptor and is passed straight to the child. If a future change
    needs the fork server to know something about the execution, put it in the payload.

    SIGTERM and SIGINT are ignored here deliberately. Kubernetes sends SIGTERM to PID 1 and the
    supervisor's handler drains rather than exiting: an in-flight child must be allowed to
    finish inside terminationGracePeriodSeconds, and the fork server is the only process that
    can reap it. Its lifetime is tied to the control socket — EOF is what ends it. SIGCHLD is
    reset to SIG_DFL for the opposite reason: SIG_IGN makes the kernel auto-reap, racing the
    supervisor's wait/reap split and losing exit statuses.

    `pending` is why the control channel's death is not a leak. The fork server is the only
    process that knows the pid of a child whose `{"pid": n}` reply never reached the supervisor,
    and exiting on EOF then orphaned a process running user code, at the same uid, with write
    access to /scratch, for the pod's lifetime. Every forked pid is held until a reap consumes
    it, and whatever is left when the loop ends is killed by _fs_kill_pending.
    """
    pending = set()
    try:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (OSError, ValueError):
                pass
        _fs_become_subreaper()
        # The fork server needs its control socket and the pod's stdout and nothing else; a
        # child that inherited the listener could read another user's HTTP conversation.
        _close_inherited_fds({sock.fileno()})
        sock.settimeout(None)
        while True:
            try:
                msg, fds = _fs_recv(sock, maxfds=4)
            except (OSError, ValueError) as exc:
                LOG.error("fork server: control channel failed: %s", exc)
                break
            if msg is None:
                break  # EOF: the supervisor is gone
            op = msg.get("op")
            try:
                if op == FS_OP_FORK:
                    _fs_do_fork(sock, fds, pending)
                    continue
                _fs_close_all(fds)
                if op == FS_OP_WAIT:
                    # ECHILD is not "waitid is unavailable": reporting it as such sent the
                    # supervisor into _reap's WNOHANG polling loop under job.kill_lock, whose
                    # first FS_OP_REAP then raised from inside that lock.
                    try:
                        os.waitid(os.P_PID, msg["pid"], os.WEXITED | os.WNOWAIT)
                    except AttributeError as exc:
                        _fs_send(sock, {"unsupported": str(exc)})
                    except OSError as exc:
                        if exc.errno == errno.ECHILD:
                            _fs_send(sock, {"nochild": str(exc)})
                        else:
                            _fs_send(sock, {"unsupported": str(exc)})
                    else:
                        _fs_send(sock, {"ok": True})
                elif op == FS_OP_REAP:
                    try:
                        got, status = os.waitpid(
                            msg["pid"], os.WNOHANG if msg.get("nohang") else 0)
                    except OSError:
                        # ECHILD and friends: not (or no longer) ours, so drop it before the
                        # outer handler answers — the cleanup below would otherwise signal a
                        # number that may since have been recycled.
                        pending.discard(msg.get("pid"))
                        raise
                    if got:
                        pending.discard(msg["pid"])
                    _fs_send(sock, {"running": True} if got == 0 else {"status": status})
                elif op == FS_OP_SWEEP:
                    killed, reaped = _fs_sweep(pending)
                    _fs_send(sock, {"swept": killed, "reaped": reaped})
                else:
                    _fs_send(sock, {"error": f"unknown op {op!r}"})
            except OSError as exc:
                try:
                    _fs_send(sock, {"error": f"{type(exc).__name__}: {exc}"})
                except OSError:
                    break
    except BaseException as exc:  # pragma: no cover - the loop above is the whole function
        try:
            LOG.exception("fork server: aborting: %s", exc)
        except Exception:
            pass
    try:
        _fs_kill_pending(pending)
    except BaseException as exc:  # never let cleanup turn an exit into a hang
        try:
            LOG.error("fork server: cleaning up unreaped children failed: %s", exc)
        except Exception:
            pass
    os._exit(0)


def _fs_do_fork(sock, fds, pending):
    if len(fds) != 4:
        _fs_close_all(fds)
        _fs_send(sock, {"error": f"expected 4 descriptors, got {len(fds)}"})
        return
    payload_fd, out_w, status_w, audit_w = fds
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        pid = os.fork()
    except OSError as exc:
        _fs_close_all(fds)
        _fs_send(sock, {"error": f"fork failed: {exc}"})
        return
    if pid == 0:
        try:
            sock.close()
        except Exception:
            pass
        _child_main(payload_fd, out_w, status_w, audit_w)
        os._exit(70)  # unreachable; _child_main never returns
    # Before the reply, not after: the send is the step that can fail, and a child whose pid
    # was never recorded here is a child nobody in the pod can name.
    pending.add(pid)
    _fs_close_all(fds)
    _fs_send(sock, {"pid": pid})


def _fs_become_subreaper():
    """Mark the fork server a child subreaper. True if it took.

    This is the only handle the pod has on a setsid() escapee, and it works because setsid()
    changes the session and the process group and NOT the parentage. Without it, both a plain
    fork that stayed in the group and a setsid() escapee survived a normally-completing
    execution for the pod's lifetime: the only /proc scan in the supervisor is _group_members,
    which structurally cannot see the second one and is not a sweep in any case. With it, an
    escapee whose parent exits reparents to the fork server rather than to PID 1, so FS_OP_SWEEP
    can enumerate it by parentage — a relation no descendant can leave.

    Reparenting is not instant: it happens only when the escapee's own parent exits, so a chain
    of setsid()'d processes surfaces one level per sweep round. It does not bound the
    intra-execution window — a resident forked by execution A is alive for the whole of A — nor
    a process that has escaped the pod's pid namespace.

    Unverified under gVisor, which implements prctl in the sentry. A refusal degrades rather
    than breaks: it is logged and the escapee reparents to PID 1 as before, which is why this
    is a warning and not a refusal to start.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        rc = libc.prctl(PR_SET_CHILD_SUBREAPER, ctypes.c_ulong(1),
                        ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))
    except Exception as exc:  # no libc, no prctl symbol, a platform without it
        LOG.warning("fork server: PR_SET_CHILD_SUBREAPER is unavailable (%s); a descendant "
                    "that calls setsid() will survive its execution", exc)
        return False
    if rc != 0:
        LOG.warning("fork server: PR_SET_CHILD_SUBREAPER failed (errno %d); a descendant that "
                    "calls setsid() will survive its execution", ctypes.get_errno())
        return False
    return True


def _fs_sweep(pending):
    """Kill and reap what has REPARENTED to the fork server, for up to FS_SWEEP_MAX_ROUNDS
    passes. Returns the pids it consumed.

    The `pending` set is what keeps this off the validated reap path: the fork server's own live
    execution child is its child by parentage exactly as a stray is. Every pid it forked and has
    not been asked to reap is in `pending`, so "my children minus pending" is the set that had
    reparented here at the instant of the scan, and it cannot steal a zombie the supervisor is
    about to reap because the supervisor only names pids in `pending`. The exclusion is by pid
    but its effect is a whole subtree: descendants reparent here only when their parent exits.

    One scan is not the whole set. A depth-2 chain is invisible to the first scan — at scan time
    the grandchild's parent is still alive — so re-enumeration is the fix and FS_SWEEP_MAX_ROUNDS
    is the chain depth cleared in one execution. A deeper chain, or one that forks a fresh decoy
    each time it is reparented, leaves a remainder for the next execution's sweep, logged so the
    bound is observable rather than silent.

    Re-enumeration is also what reaps the collateral: delivery is killpg on each stray's own
    group, so a group member not itself in the scanned set becomes a zombie here. A round before
    the last one enumerates it; collateral from the LAST round waits for the next execution.

    A zombie stray is not an escapee and is not logged as one. An ordinary subprocess.Popen a
    script never waits on reparents here like any orphan and is not running anybody's code, so
    live strays and reparented zombies are counted and reported separately.

    Called at the END of an execution, after the supervisor has reaped its child, so that
    execution's escapees have already reparented. /proc is the only way to enumerate live
    children — waitpid finds only the ones that have exited — and an unreadable /proc degrades
    to "no strays found".
    """
    killed = set()
    reaped = set()
    deadline = time.monotonic() + FS_SWEEP_BUDGET_S
    for round_no in range(1, FS_SWEEP_MAX_ROUNDS + 1):
        kids = _child_pids(os.getpid())
        if kids is None:
            LOG.error("fork server: /proc is unreadable, so reparented strays cannot be swept")
            break
        strays = {pid for pid in kids if pid not in pending}
        if not strays:
            break
        # An already-exited pid is grouped with the zombies: nothing to kill, and waitpid is
        # the only thing that can still be owed to it.
        zombies = {pid for pid in strays if not _pid_is_live(pid)}
        live = strays - zombies
        if zombies:
            unreaped = set(zombies)
            _fs_reap_pending(unreaped)
            LOG.info("fork server: reaped %d reparented zombie(s) in sweep round %d: %s",
                     len(zombies) - len(unreaped), round_no, sorted(zombies - unreaped))
            reaped |= zombies - unreaped
        if live:
            LOG.error("fork server: %d process(es) escaped an execution's process group and "
                      "reparented here (sweep round %d): %s; killing and reaping them",
                      len(live), round_no, sorted(live))
            survivors = set(live)
            if not _fs_kill_set(survivors):
                LOG.error("fork server: %s survived SIGKILL and are still running",
                          sorted(survivors))
            killed |= live - survivors
        if time.monotonic() >= deadline:
            LOG.error("fork server: the sweep hit its %.0fs budget after %d round(s); anything "
                      "that reparents from here waits for the next execution's sweep",
                      FS_SWEEP_BUDGET_S, round_no)
            break
    else:
        LOG.error("fork server: the sweep used all %d rounds without a pass that found "
                  "nothing, so it cannot claim the pod is clear; a chain deeper than that is "
                  "cleared one execution at a time", FS_SWEEP_MAX_ROUNDS)
    return sorted(killed), sorted(reaped)


def _fs_kill_set(pids):
    """SIGTERM, then SIGKILL after KILL_GRACE_S, over a whole SET, reaping as it goes.

    True when nothing is left. `pids` is consumed: what remains on a False is what survived.
    One grace for the batch rather than one each — a per-child grace turns n stragglers into
    n * KILL_GRACE_S on a path that is either a pod shutdown or a user's response.
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        _fs_signal_pending(pids, sig)
        deadline = time.monotonic() + KILL_GRACE_S
        while pids:
            _fs_reap_pending(pids)
            if not pids or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        if not pids:
            return True
    return False


def _fs_kill_pending(pending):
    """Kill and reap every child the supervisor can no longer ask about. Bounded.

    Only ever called after the control loop has ended, which is what keeps it clear of the
    supervisor's reap path: no further FS_OP_REAP can arrive, so nothing here can consume a
    zombie the supervisor is waiting for. The shape is _kill_group's, flattened over a set,
    because the pod is going away and holding its exit open per child buys nothing.

    The strays go too, and only here: on the way out there is no live child to protect, so
    _fs_sweep's `pending` distinction has nothing left to make.
    """
    kids = _child_pids(os.getpid())
    if kids:
        pending = set(pending) | set(kids)
    if not pending:
        return
    LOG.error("fork server: the control channel ended with %d unreaped child(ren) %s; "
              "killing their process groups rather than orphaning them",
              len(pending), sorted(pending))
    if not _fs_kill_set(pending):
        LOG.error("fork server: %s survived SIGKILL; exiting anyway", sorted(pending))


def _fs_signal_pending(pending, sig):
    for pid in sorted(pending):
        pgid = _own_pgid(pid)
        try:
            if pgid is None:
                # No group of its own: it has not reached _child_main's setsid(), or never
                # will. killpg on the group it reports would signal the fork server and the
                # supervisor with it, so signal the child alone.
                os.kill(pid, sig)
            else:
                os.killpg(pgid, sig)
        except OSError:
            pass  # already gone, or undeliverable; the reap below is the arbiter


def _fs_reap_pending(pending):
    for pid in sorted(pending):
        try:
            got, _ = os.waitpid(pid, os.WNOHANG)
        except OSError:
            pending.discard(pid)  # ECHILD: not ours, so not ours to wait for
        else:
            if got:
                pending.discard(pid)


def _own_pgid(pid):
    """`pid`'s process group, or None when that is the CALLER'S group.

    The guard is the whole point and it holds in both processes that use it: neither the
    supervisor nor the fork server (deliberately) calls setsid(), so a child that has not yet
    reached its own reports the caller's group, and killpg on that value would signal the
    caller. See _resolve_pgid, which is this plus job.pid's locking rules.
    """
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return None
    return None if pgid == os.getpgrp() else pgid


class ForkServerError(RuntimeError):
    """The fork server refused or could not answer. Surfaces as a 500 to the caller."""


class ForkServer:
    """The supervisor's handle on the fork server. One per Supervisor, started by bring_up().

    The child is a GRANDCHILD of the supervisor, so the supervisor cannot waitpid() it. The wait
    is split across the socket in the same two steps _reap uses for its own children — a
    blocking waitid(WNOWAIT) that does not consume the zombie, then a waitpid under
    job.kill_lock that does — because that split is what keeps a pid from being recycled between
    the watchdog deciding to kill and the killpg landing.

    Signalling does not go through here: supervisor and child share uid 65532, so os.killpg from
    the supervisor reaches the child's group directly.
    """

    def __init__(self, pid, sock):
        self.pid = pid
        self._sock = sock
        # One stream shared by fork/wait/reap. Concurrency is 1, so this is uncontended; it is
        # here so a future second caller blocks rather than interleaving two round trips.
        self._lock = threading.Lock()
        # Why a socket that has failed once is never used again: see _poison.
        self._broken = None
        # A reason recorded by a path that must not log — the SIGCHLD orphan reaper's, via
        # note_reaped. Emitted later by _flush_broken_log from a normal thread.
        self._broken_unlogged = None
        # The collision surface with the PID 1 orphan reaper. Plain attributes, never a lock:
        # note_reaped is reached from a SIGCHLD handler, and a handler that blocks on a lock the
        # interrupted thread holds deadlocks PID 1. `exit_status` publishes a status the reaper
        # consumed on this handle's behalf; `_closing` is close()'s claim on self.pid, which
        # makes the reaper stand down for the whole of the grace loop.
        self.exit_status = None
        self._closing = False

    @classmethod
    def start(cls):
        """Fork the fork server out of THIS process. Call before anything is accepted."""
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:
            try:
                parent.close()
            except Exception:
                pass
            _forkserver_main(child)
            os._exit(70)  # unreachable
        child.close()
        parent.settimeout(FS_CONTROL_TIMEOUT_S)
        LOG.info("fork server started as pid %d", pid)
        return cls(pid, parent)

    def _mark_broken(self, reason, log=True):
        """Record, once, that this handle is finished.

        Safe from any thread: `_broken` is a plain attribute so alive() can read and set it
        without self._lock, which wait_nowait holds for the entire lifetime of an execution.
        Taking that lock in alive() made every /health during an execution block until the
        child exited.

        `log=False` is for the SIGCHLD orphan reaper and nothing else. That caller arrives from
        a signal handler in PID 1, and logging there re-enters the logging machinery at an
        arbitrary point in the interrupted thread: with main()'s exact setup and a congested
        stdout pipe it raised `RuntimeError: reentrant call inside <_io.BufferedWriter>`, which
        aborted the delivery with 4 of 5 zombies unreaped — and SIGCHLD is not queued, so they
        were never retried. The reason is not dropped: setting `_broken` before logging used to
        drop it for good, so it is parked in `_broken_unlogged` and printed by
        _flush_broken_log from a serving thread.
        """
        if self._broken is None:
            self._broken = reason
            if log:
                LOG.error("fork server control socket is unusable and will not be reused: %s",
                          reason)
            else:
                self._broken_unlogged = reason

    def _flush_broken_log(self):
        """Print, from a normal thread, a reason the signal handler had to record silently.

        alive() calls it, so the line appears on the next readiness probe rather than not at
        all. Read-then-clear with no lock: two threads racing here at worst print it twice,
        which is the failure this is willing to have. Printing it zero times is not.
        """
        reason = self._broken_unlogged
        if reason is not None:
            self._broken_unlogged = None
            LOG.error("fork server control socket is unusable and will not be reused: %s", reason)

    def _poison(self, reason):
        """Caller holds the lock. Mark the control socket unusable and close it.

        A round trip that failed halfway leaves the peer's reply queued, and there is no way to
        tell later how many replies are outstanding: after an FS_OP_WAIT timed out, the next
        FS_OP_REAP returned that WAIT's `{"ok": true}`. The dangerous ordering is a fork whose
        reply is lost — the child WAS forked and its pid WAS sent, so the next execution reads
        that stale pid and applies its limits to, watchdogs, killpgs and reaps another user's
        child while its own runs with no wall clock and no reaper. Alignment cannot be
        re-established, so it is never attempted: every later call fails, /health goes non-ok
        and Kubernetes replaces the pod. Restarting the fork server here would be worse — it
        would be forked from a supervisor that has served requests.
        """
        self._mark_broken(reason)
        try:
            self._sock.close()
        except OSError:
            pass

    def _round_trip(self, msg, fds=(), timeout=FS_CONTROL_TIMEOUT_S):
        with self._lock:
            if self._broken is not None:
                raise ForkServerError(f"fork server is unusable: {self._broken}")
            try:
                self._sock.settimeout(timeout)
                _fs_send(self._sock, msg, fds)
                reply, extra = _fs_recv(self._sock)
            except (OSError, ValueError) as exc:
                self._poison(f"{type(exc).__name__}: {exc}")
                raise ForkServerError(f"fork server control round trip failed: {exc}") from exc
            if reply is None:
                self._poison("the fork server exited")
            else:
                try:
                    self._sock.settimeout(FS_CONTROL_TIMEOUT_S)
                except OSError as exc:
                    self._poison(f"{type(exc).__name__}: {exc}")
        _fs_close_all(extra)
        if reply is None:
            raise ForkServerError("fork server exited")
        # An `error` reply is in band: the message was received and answered, so the socket is
        # still aligned. Only a failed round trip loses alignment.
        if "error" in reply:
            raise ForkServerError(str(reply["error"]))
        return reply

    def alive(self):
        """False once the control socket is poisoned or the fork server process is gone.

        /health asks this. A dead fork server used to be invisible: every /execute answered 500
        forever while /health answered 200, and the manifest has only a readinessProbe, so
        nothing replaced the pod. The fork server is a plausible cgroup-OOM victim — it shares
        the supervisor's pages, so its RSS reads high, and it keeps oom_score_adj 0 while only
        the child is raised.

        Deliberately lock-free: self._lock is held for the whole of an execution by
        wait_nowait's timeout=None round trip, so taking it here would make /health block until
        the child exited. It touches only the flag and the pid, never the socket, so it cannot
        close an fd another thread is blocked on. It is also where the orphan reaper's silent
        record becomes visible.
        """
        self._flush_broken_log()
        pid = self.pid  # read ONCE: close() may set it to None between two reads
        if self._broken is not None or pid is None:
            return False
        try:
            got, _ = os.waitpid(pid, os.WNOHANG)
        except OSError as exc:
            # ECHILD: something else reaped it, so it is gone either way.
            self._mark_broken(f"waitpid on the fork server failed: {exc}")
            return False
        if got:
            self.pid = None  # reaped here, so close() must not wait for it again
            self._mark_broken("the fork server exited")
            return False
        return True

    def note_reaped(self, pid, status):
        """Publish a wait status the PID 1 orphan reaper consumed instead of this handle.

        `waitpid(-1, WNOHANG)` cannot be told to skip a pid — it reports which child it took
        only after taking it — so the reaper cannot avoid the fork server's zombie. It hands it
        here instead, and the handle treats a published status as what its own waitpid would
        have returned: `pid` goes None, so alive() answers False without a syscall and close()
        has nothing left to wait for or kill. Without that, close()'s grace loop would poll a
        pid the reaper had already consumed and at its deadline SIGKILL a possibly recycled one.

        It runs in a signal handler, so it does not log: logging from here raised `RuntimeError:
        reentrant call inside <_io.BufferedWriter>` against a congested stdout, which aborted
        the whole delivery and left the rest of the zombies unreaped. The reason reaches the log
        from alive(), on a serving thread.

        True when `pid` was this fork server; False means the caller may discard the status.
        """
        if pid != self.pid:
            return False
        self.exit_status = status
        self.pid = None  # read ONCE by alive() and close(), so this is a single publish
        self._mark_broken("the fork server exited (reaped by the PID 1 orphan reaper)", log=False)
        return True

    def fork_child(self, payload_fd, out_w, status_w, audit_w):
        """The pid of a fresh execution child. The four descriptors stay the caller's to close.

        SCM_RIGHTS duplicates them into the fork server rather than moving them, and the reply
        is only sent after the fork server has received them, so closing the originals once
        this returns is safe and is the caller's job.
        """
        reply = self._round_trip({"op": FS_OP_FORK}, (payload_fd, out_w, status_w, audit_w))
        pid = reply.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise ForkServerError(f"fork server returned {pid!r} instead of a pid")
        return pid

    def wait_nowait(self, pid):
        """Block until `pid` exits WITHOUT consuming it. False if waitid is unavailable.

        ECHILD raises instead of returning False: `pid` is not the fork server's child, so the
        WNOHANG fallback _reap would take next cannot ever succeed either — it would spin under
        job.kill_lock until its first reap raised from inside the lock. Raising here reaches
        _execute_inner's finally, which kills the group.
        """
        reply = self._round_trip({"op": FS_OP_WAIT, "pid": pid}, timeout=None)
        if "nochild" in reply:
            raise ForkServerError(
                f"the fork server does not own pid {pid}: {reply['nochild']}")
        return "unsupported" not in reply

    def reap(self, pid, nohang=False):
        """The wait status, or None when `nohang` and the child is still running."""
        reply = self._round_trip({"op": FS_OP_REAP, "pid": pid, "nohang": bool(nohang)})
        if reply.get("running"):
            return None
        return reply["status"]

    def sweep(self):
        """Kill and reap everything that has reparented to the fork server. Returns those pids.

        Call it after the reap and not before: during an execution the fork server's own child
        is indistinguishable from a stray by parentage and is kept out of the sweep only by the
        `pending` set that FS_OP_REAP clears. Concurrency is 1 and the execution slot is still
        held, so there is no other fork in flight either.

        Its cost is per round — 2 * KILL_GRACE_S for a round whose strays ignore SIGTERM, and
        nothing measurable when there is no stray. FS_SWEEP_BUDGET_S plus one round's grace is
        the whole-call bound, sized to stay under FS_CONTROL_TIMEOUT_S because overrunning that
        poisons the socket and kills the fork server.
        """
        reply = self._round_trip({"op": FS_OP_SWEEP})
        swept = reply.get("swept")
        reaped = reply.get("reaped")
        return (swept if isinstance(swept, list) else [],
                reaped if isinstance(reaped, list) else [])

    def close(self, grace=2.0):
        """Close the control socket and reap the fork server. Idempotent.

        `_closing` is set first and is load-bearing: it stops the PID 1 orphan reaper consuming
        self.pid while the grace loop below owns it, which keeps the SIGKILL at the end of that
        loop off a recycled pid. A plain bool rather than a lock because the reaper runs in a
        SIGCHLD handler and CPython runs handlers in the main thread — the same thread this is
        called from — so the flag is set before any later handler invocation can read it, with
        no way for a handler to deadlock against the thread it interrupted. Nothing clears it.
        """
        self._closing = True
        with self._lock:
            try:
                self._sock.close()
            except OSError:
                pass
            pid, self.pid = self.pid, None
        if pid is None:
            return
        deadline = time.monotonic() + grace
        while True:
            try:
                got, _ = os.waitpid(pid, os.WNOHANG)
            except OSError:
                return
            if got:
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        # Blocked in FS_OP_WAIT on a child that outlived the supervisor, or wedged. Either way
        # the pod is going away; do not hold the shutdown open for it.
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except OSError:
            pass


class _SelfWaiter:
    """The waiter for a child of THIS process. Not used in production — see _reap.

    It must not share a process with the PID 1 orphan reaper: reap() calls waitpid on a specific
    pid, so a generic waitpid(-1) that got there first turns it into ECHILD, which _reap
    propagates into _execute_inner's finally. Not a live hazard — in the image every execution
    child is a grandchild forked by the fork server, and this is used only by tests that fork
    their own children. Anything that starts forking execution children out of the supervisor
    again re-opens it for real.
    """

    @staticmethod
    def wait_nowait(pid):
        try:
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
        except (AttributeError, OSError):
            return False
        return True

    @staticmethod
    def reap(pid, nohang=False):
        got, status = os.waitpid(pid, os.WNOHANG if nohang else 0)
        return None if got == 0 else status


SELF_WAITER = _SelfWaiter()


def _reap_orphans(fs=None, max_rounds=ORPHAN_REAP_MAX_ROUNDS, supervisor=None):
    """Reap up to `max_rounds` dead children of THIS process. Returns the pids reaped.

    The class this exists for is narrow and not the ordinary one. On every ordinary path a
    descendant that outlives its parent reparents to the FORK SERVER and the sweep kills and
    reaps it. What is left reparents PAST the fork server to PID 1 — the supervisor — and
    nothing else ever waits on it: either the fork server is dead (the `stranded` path
    _execute_inner handles), or PR_SET_CHILD_SUBREAPER was unavailable, which
    _fs_become_subreaper only warns about. A zombie costs a pid slot against pod_pids_limit for
    the pod's lifetime, in a replicas-1 pod that serves every later user.

    What it cannot do: a LIVE descendant is not a zombie. Reaping does not touch a setsid()'d
    process that is still running and still holding a pipe write end — that one is bounded by
    _drain's deadline and by nothing else.

    Bounded three ways because it is driven from a signal handler: `max_rounds` caps one
    delivery, every waitpid is WNOHANG, and OSError ends the loop rather than propagating.

    It publishes every pid it consumes, because waitpid(-1) has no way to skip one. Two owners
    might have been waiting: `fs` (the fork server's own pid) and `supervisor` (the running
    execution's child, which reparents here when the fork server dies mid-execution). Each
    publisher is called inside its own guard, because note_reaped's own failure mode used to
    escape the OSError guard and abort the delivery — and SIGCHLD is not queued, so what it
    abandoned was never retried.
    """
    reaped = []
    if fs is not None and getattr(fs, "_closing", False):
        # close() owns fs.pid until the process exits. Standing down for that window costs
        # nothing: the only orphans it declines belong to a pod that is shutting down.
        return reaped
    for _ in range(max_rounds):
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except OSError:
            break
        if pid == 0:
            break
        for owner, publish in ((fs, "note_reaped"), (supervisor, "note_child_reaped")):
            if owner is None:
                continue
            try:
                getattr(owner, publish)(pid, status)
            except BaseException:  # noqa: BLE001 - one publisher may not cost the rest their reap
                pass
        reaped.append(pid)
    return reaped


def _drain(fd, limit, reaped=None, grace=DRAIN_GRACE_S, poll=0.2, on_limit=None, sink=None):
    """Read a pipe until EOF or until `grace` seconds after `reaped` is set.

    Two behaviours at `limit`, selected by `on_limit`, and they are not interchangeable.

    * `on_limit` given (the OUTPUT pipe, 8 MiB): reading STOPS at the cap and `on_limit()` kills
      the child's process group. Stopping is the point — the cap exists so `while True: print()`
      cannot consume the supervisor's memory or the pod's CPU before the wall clock fires, and a
      reader that drained and discarded would answer 200 "ok" while doing neither. `total` stops
      at the cap too, which is what `output_bytes` means on the wire. A child blocked writing to
      the now-unread pipe still dies: a pipe write is an interruptible sleep.
    * `on_limit` absent (the STATUS pipe, 64 KiB): past the limit it keeps reading and discards,
      so a child writing a huge record blocks on nothing and `total` stays accurate.

    The deadline is not a detail. The write ends are inherited by every descendant, so a
    grandchild that setsid()s away holds the pipe open after the child is reaped and no amount
    of waiting produces EOF; without a deadline the execution slot is held by a pipe read rather
    than by a process. It abandons whether or not bytes are still arriving: a deadline evaluated
    only when the fd went quiet was no deadline at all against a writer that never lets it go
    quiet. The fd is still leaked in that case, deliberately — see _execute_inner.

    `sink` (the AUDIT pipe) hands every block straight to a consumer and buffers nothing, so
    `limit` does not apply and the returned bytes are empty. The consumer owns its own bounds
    and needs them applied as the bytes arrive — a rate cap cannot be enforced on a buffer
    handed over at EOF, and holding the stream here to re-emit later would be the flooding
    primitive the caps exist to remove. If the consumer raises, this keeps reading and DISCARDS:
    it cannot fall back to buffering, and it must not stop reading or the child blocks in
    os.write inside a call that was succeeding.

    Returns (bytes, total_seen, stopped_at_limit, abandoned).
    """
    chunks = []
    total = 0
    stopped = False
    deadline = None
    abandoned = False
    discarding = False
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
        # Evaluated whether or not the fd is ready. Inside `if not ready:` it was no deadline
        # at all: a descendant that setsid()s away and writes continuously keeps `ready` truthy
        # on every pass, so this thread ran for the pod's lifetime discarding bytes. What it
        # bounds is the thread and the CPU, not the escapee. The read end is closed by
        # _execute_inner's post-join cleanup, so a still-live writer takes EPIPE on its next
        # write; the descriptor is leaked only in the backstop branch there, where closing an fd
        # a thread may be blocked on would be undefined.
        if deadline is not None and time.monotonic() >= deadline:
            abandoned = True
            break
        if not ready:
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
        if sink is not None:
            try:
                sink(block)
            except Exception:
                # Keep draining and discarding. Setting `sink = None` would drop this stream
                # into the buffering branch, which is reached with limit=None on the audit pipe
                # and raises TypeError on the next block, killing this thread — after which
                # nothing reads the fd, the pipe fills, and a running child blocks in os.write
                # inside a successful data call. The recovery must not be worse than none.
                LOG.exception("audit sink failed; this stream is drained and DISCARDED from "
                              "here on, and nothing more from it is forwarded")
                sink = None
                discarding = True
            continue
        if discarding:
            continue
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

    Head and tail, never head alone: the model needs the traceback and the traceback is at the
    tail. The marker between them is fixed text so a client recognises truncation without
    heuristics, and the 64 KiB budget is the head and tail only — the marker is additional.

    The cut is on byte boundaries but not through a character: the head and tail are exactly
    32 KiB each, and this trims at most 3 bytes off each side so the split never bisects a
    multi-byte sequence, counting what it trimmed into N.

    The lossy decode is contract behaviour: invalid bytes become U+FFFD, there is no `encoding`
    field. A script with binary to return writes an artifact.
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

    Three things are refused, each reachable from a script: a type over
    ERROR_TYPE_MAX_BYTES (60,000 characters reached the response, a text channel out of the
    sandbox that bypasses the output window and lands in a model's context); a type that is not
    an identifier or dotted qualname; and a supervisor-reserved name, which the contract invites
    clients to branch on.

    StartupFailure is the one reserved name a child legitimately writes, from _child_main's own
    setup handler, which exits 70 and cannot reach the script — so it is admitted on that exit
    code and refused on every other rather than being lost or left forgeable.
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

    A backstop, not a bound anything should reach: every component is separately capped, so a
    well-formed response is ~100 KiB at most. Artifacts go first and their count survives in
    artifacts_omitted, because a name the model cannot see is recoverable — it can list again —
    while output it never sees is not.
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
        # Nothing left to give: this payload is not the execution shape, so send the uniform
        # error object rather than a body of unknown size.
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
    # No pgid slot for signalling a LIVE child: none is ever cached for that (see
    # _resolve_pgid), and a slot for one is an invitation to start. `reaped_pgid` is not that
    # slot — it is written at exactly one moment, inside _reap under kill_lock while the zombie
    # is still held by waitid(WNOWAIT), and read at exactly one, by _kill_survivors immediately
    # afterwards. It is cleared in Supervisor.note_child_reaped, the one case where the write
    # happens and the reap does not, which is when the number becomes recyclable.
    __slots__ = ("req", "conn", "enqueued_at", "pid", "deadline", "dirs", "owner",
                 "kill_lock", "reaped", "reaped_pgid", "reaped_status", "limit", "done",
                 "sealed")

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
        # The reap and every signal are serialised by this lock. Once waitpid reaps the child
        # the pid is free for the kernel to reuse, and a watchdog that decided to kill a moment
        # earlier would signal a group that is plausibly the NEXT execution's child. Every
        # signal path takes it, checks `reaped`, and re-reads the pgid before signalling.
        self.kill_lock = threading.Lock()
        self.reaped = False
        # The child's OWN process group, read in _reap while its zombie was still held. None
        # when it never had one. Read only by _kill_survivors, only after the reap.
        self.reaped_pgid = None
        # A wait status the PID 1 orphan reaper consumed on this job's behalf, which happens
        # only when the fork server died mid-execution and the child reparented to PID 1.
        # Non-None is how _execute_inner tells that case from a reap it did itself.
        self.reaped_status = None
        self.limit = None      # the first supervisor limit that fired: a reserved error type
        self.done = threading.Event()   # set once the child is reaped; stops the watchdog
        # True once _seal_retained has RUN over this job's artifacts/, whatever it concluded —
        # the flag _release keys "a retained directory is sealed or empty" on. It means "the
        # pass was entered", not "the directory is secured": _seal_retained sets it before doing
        # any work, then catches Exception and purges. A BaseException raised inside the pass
        # would skip both that arm and _secure_unsealed and leave plaintext retained. Not
        # reachable today — KeyboardInterrupt reaches only the main thread and this runs on a
        # handler thread, and MemoryError is caught — but moving the completion path onto the
        # main thread or adding a cancellation mechanism would make it real.
        self.sealed = False


def peer_gone(sock):
    """True when the client's connection has closed.

    Only consulted at dequeue, for a request that has not been forked. A RUNNING child is never
    killed on disconnect: it completes, is reaped, its manifest is written and its artifacts are
    retained, because killing it would destroy artifacts the retention window promises and
    because disconnect detection while nobody is reading the socket is unreliable enough that a
    false positive would kill live executions.
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
        # Handlers that owe a response, which is not the execution slot: the slot is given back
        # in run()'s finally, which runs before the handler writes the 200, so idle() cannot be
        # what tells the shutdown path the process may exit.
        self._responding = 0
        # execution_id -> [monotonic deadline, measured bytes], in COMPLETION ORDER, which is
        # what makes oldest-first eviction a property of the structure rather than a sort key.
        #
        # The size is cached, not re-measured: re-walking every retained tree on each completion
        # made a 300,000-file execution a tax on all five minutes of executions after it.
        # Nothing the supervisor knows about writes to a retained directory, and
        # _forget_retained is the only thing that removes bytes.
        #
        # It CAN drift for a setsid() escapee, which is not signalled by _kill_group and keeps
        # its write access to /scratch/<id>/artifacts: the retained total, the ceiling eviction
        # and the watchdog's aggregate check all then read low, and the emptyDir sizeLimit is
        # what would notice. Re-measuring would not fix it either.
        self._retention = {}
        # execution_id -> {artifact name: sha256 hex or None}, as build_manifest found them.
        # In memory on purpose: what this defends against is a same-uid process writing to
        # /scratch, so a manifest written there would be forged in the same breath as the file
        # it describes. An id with no row here serves no artifact at all. Not unbounded — the
        # aggregate is charged against RETAINED_STATE_CEILING_BYTES and evicted with the
        # execution it belongs to.
        self._artifact_digests = {}
        # execution_id -> bytearray(32), the AES-256-GCM key its retained artifacts are sealed
        # under. Same place and same protection as the digest map — YAMA plus ancestry, not
        # seccomp. What it adds: it is minted per execution, after ForkServer.start() took the
        # snapshot every child is forked from, so no child's address space can contain it, and
        # it dies with the retained entry in _forget_retained, the only eviction path. A
        # bytearray and never `bytes`, so it can be wiped in place.
        self._artifact_keys = {}
        self.retention_s = RETENTION_S if retention_s is None else retention_s
        self._stop_reaper = threading.Event()
        # Set by bring_up(), which forks it before the supervisor is ready and therefore before
        # any request has been parsed. None means "nothing can be executed yet".
        self.forkserver = None
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
        elif self.forkserver is None or not self.forkserver.alive():
            # The one unrecoverable state, and it used to be invisible: with the fork server
            # dead every /execute answered 500 forever while this answered 200, and the
            # manifest has a readinessProbe and deliberately no livenessProbe, so the pod
            # stayed in the Service endpoints. Answering non-ok takes it out, which is the only
            # recovery available: the fork server is not restarted in process, because one
            # re-forked from a supervisor that has served requests defeats its whole purpose.
            status = "forkserver-down"
        else:
            status = "ok"
        # A busy supervisor is healthy: 503 here would drop the pod out of the Service
        # endpoints mid-execution, and with one replica every retry then fails against no
        # endpoint at all.
        code = 200 if status == "ok" else 503
        return code, {"status": status, "busy": busy, "queued": queued}

    def accepting(self):
        """False while starting or draining. Checked BEFORE the request body is read.

        _admit is not early enough: it runs after parse_execute_request, so a POST /execute
        arriving during bring_up() had already materialised both JWTs and the user's source as
        Python strings — and ForkServer.start() then snapshots that address space. A request
        refused with 503 during startup was still recovered from the child through
        /proc/self/mem. main() binds and serves before bring_up() on purpose, so
        `status: "starting"` is observable rather than a connection refusal, which makes this
        the enforcement point. _admit re-checks under the lock.

        On its own this enforces "no Python object holding a token, a request body or source
        code is constructed before the fork". It was never enough for "those bytes are never in
        this address space": with the default rbufsize the header parse did an 8 KiB buffered
        recv before _execute was entered, so a body sharing a TCP segment with its headers was
        already raw in this heap. That half is _HeaderBoundedReader's — rbufsize 0, and rfile
        consumes exactly the request head so the body waits in the kernel receive queue. The
        residue is not zero: the head itself is parsed, and up to HEADER_PEEK_BYTES - 1 body
        bytes sit in two fixed, in-place-zeroed buffers for the length of a single read.
        """
        return self.ready and not self.draining

    def read_artifact(self, execution_id, name):
        """(bytes, content_type) for an artifact of a RETAINED execution, or raise.

        Retained only, not running: the caller holding an execution_id is the one that
        submitted it and has already been answered, so by the time it can ask, _retain has
        trimmed the directory. Serving a running one would hand back a file mid-write.

        The id is the authorisation ON THIS HTTP SURFACE, and the qualifier is the whole
        accuracy of the sentence. It is a uuid4 minted per execution by chat-backend, never
        shown to the model, and there is no route here that lists execution ids — so combined
        with the NetworkPolicy that decides who reaches this port, it has the same standing
        /execute has. It is NOT a filesystem property: /scratch is fully enumerable by any
        process at the shared uid. What the id bounds is who can ask this process for bytes;
        what the digest map bounds is which bytes it hands over. There is no per-session check
        and there is not meant to be one — chat-backend is the only side that knows which
        session owns which execution, and it resolves the model's artifact NAME against its own
        record before an execution_id ever reaches this route.

        The digest map is part of the answer, not an optimisation: an execution retained
        without a manifest ever being built has an empty map and serves nothing, because
        nothing was ever advertised for it. The key is part of the answer too — a retained
        execution that reached the seal pass has one, one that did not is plaintext, and a
        sealed directory whose key is gone answers 409 for everything.
        """
        if not isinstance(execution_id, str) or not EXECUTION_ID_RE.fullmatch(execution_id):
            raise RequestError(400, "InvalidRequest", "execution_id must be a lowercase uuid4")
        with self._lock:
            retained = execution_id in self._retained_ids
            digests = self._artifact_digests.get(execution_id) or {}
            key = self._artifact_keys.get(execution_id)
        if not retained:
            # One shape for "never existed", "still running" and "reaped": which it is would
            # tell a caller holding a guessed id something about the pod's state.
            raise RequestError(404, "NotFound", "no such execution")
        dirs = ExecutionDirs(self.scratch_root, execution_id)
        return read_artifact_bytes(dirs.artifacts, name, expected_digests=digests,
                                   key=key,
                                   execution_id=None if key is None else execution_id)

    def begin_drain(self):
        self.draining = True
        with self._cv:
            self._cv.notify_all()

    def idle(self):
        with self._lock:
            return self._running is None and not self._waiting

    def begin_response(self):
        """A request handler has taken on a response it has not written yet."""
        with self._lock:
            self._responding += 1

    def end_response(self):
        """...and has finished writing it, or has failed in a way that never will."""
        with self._lock:
            self._responding -= 1

    def responses_in_flight(self):
        with self._lock:
            return self._responding

    def quiescent(self):
        """idle(), AND no handler still owes a response. The shutdown path's predicate.

        Deliberately not idle(), which is about the EXECUTION SLOT and goes true in run()'s
        finally, before the handler writes the 200. Widening idle() would change what
        _await_slot, _release and health() see; the one caller that must also wait for the
        answer gets its own predicate.
        """
        with self._lock:
            return self._running is None and not self._waiting and self._responding == 0

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
                # One execution_id names one directory, one manifest and one audit trail.
                # Reusing merges two runs into a manifest chat-backend already recorded; wiping
                # deletes artifacts read_artifact may still be serving.
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
        # Registering the directory is not _retain's alone: _retain runs on the success path
        # only, so a fork OSError or a manifest failure after ExecutionDirs.create() left the id
        # in _retained_ids — answering 409 — with no row in _retention, its bytes counted
        # against no ceiling and only the mtime sweep removing it. Whatever created the
        # directory, something must own deleting it.
        if retain:
            # Sealed or empty, structurally. _seal_retained runs on the completion path only,
            # so any exception out of _execute_inner propagated past the seal and landed here,
            # and this method then retained the directory for the whole of RETENTION_S with the
            # child's plaintext exactly where it wrote it — the original demonstrated attack,
            # reproduced against the sealed build. The read path returning 409 does not answer
            # it; the read path was never the threat. Making it structural means the guarantee
            # does not depend on the happy path reaching a call.
            if not job.sealed:
                self._secure_unsealed(job)
            self._register_retention(job.req.execution_id, job.dirs)
        with self._cv:
            if self._running is job:
                self._running = None
            self._pending_ids.discard(job.req.execution_id)
            if retain:
                self._retained_ids.add(job.req.execution_id)
            self._cv.notify_all()

    def _secure_unsealed(self, job):
        """Empty a directory that is about to be retained without having been sealed.

        There is nothing in it to lose, which is what makes emptying it right rather than
        harsh. This runs only when _execute_inner raised, so no manifest was built, no digest
        map recorded and no key minted — and read_artifact refuses every name of an execution
        with an empty digest map. Every byte under /scratch/<id> is therefore unreachable
        through the API and readable by any process at the shared uid: cost with no benefit.

        The whole base tree, not only artifacts/: _retain is what normally deletes tmp/, home/,
        the caches and the token file, and it runs on the completion path only. The directory
        itself stays, empty, so the execution id remains 409-reserved for the retention window.

        Never raises: it runs from run()'s finally, where an exception would replace the error
        the caller is about to be given.
        """
        eid = job.req.execution_id
        if job.dirs is None:
            return
        removed = 0
        failed = []
        try:
            names = os.listdir(job.dirs.base)
        except OSError as exc:
            LOG.error("execution %s: cannot list its directory to empty it after a failed "
                      "execution (%s); anything it holds stays in the clear until the reaper "
                      "removes it", eid, exc)
            return
        for name in names:
            path = os.path.join(job.dirs.base, name)
            try:
                is_dir = (not os.path.islink(path)) and os.path.isdir(path)
            except OSError:
                is_dir = False
            if _remove_entry(path, is_dir):
                removed += 1
            else:
                failed.append(name)
        if failed:
            LOG.error("execution %s: did not complete, and %d of its %d retained entries "
                      "could not be deleted (%s); they ARE RETAINED IN THE CLEAR until the "
                      "reaper removes the directory",
                      eid, len(failed), len(names), ", ".join(sorted(failed)[:8]))
        elif removed:
            LOG.warning("execution %s: did not complete, so its %d retained entries were "
                        "deleted rather than kept in the clear — nothing was ever advertised "
                        "for it, so nothing could have been served",
                        eid, removed)

    def _register_retention(self, execution_id, dirs):
        """Give a created directory a retention deadline and a measured size. Idempotent.

        Measures base, not artifacts: this runs on the path _retain never reached, so nothing
        has deleted tmp/, home/ or the caches and nothing has trimmed artifacts/. Charging only
        artifacts/ charged that as zero, holding up to a whole 192 MiB execution quota against
        no ceiling until the mtime sweep found it. The ceiling is re-checked here because
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

    # -- /scratch lifecycle ------------------------------------------------------------

    def _retain(self, job):
        """On completion: delete everything under /scratch/<id> except artifacts/, trim
        artifacts/ back inside its quota, register the retention deadline and size, and bring
        the aggregate retained set under its ceiling. Returns files deleted.

        artifacts/ is what read_artifact has to return; tmp, home, caches, pycache and the token
        file have no reader after the child is reaped and every byte counts against the same
        512Mi the kubelet evicts the pod for exceeding.

        The trim is why the budget closes. Without it a quota kill retained its own overshoot: a
        burst killed by ArtifactQuota at 64 MiB left 93 MiB on disk in 0.31s, and at the ~1 GiB/s
        tmpfs sustains that is ~264 MiB. Trimming makes every term <= ARTIFACT_QUOTA_BYTES and
        the ceiling exact.

        It runs BEFORE build_manifest, the only order that works: a manifest built first would
        advertise names the trim then deletes. The trimmed count reaches the response through
        artifacts_omitted.
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

    def _record_digests(self, execution_id, digests):
        """Bind a retained execution to the bytes its manifest described.

        Guarded on _retention rather than _retained_ids: _release adds the id to _retained_ids
        only after _execute returns, so the row _retain wrote is the only evidence the directory
        is still alive — and _enforce_retained_ceiling may already have evicted this execution,
        in which case recording a map for it would leak a dict per eviction.

        The ceiling is re-enforced here, not only in _retain, because this is where the memory
        the map costs appears. Outside the lock, because eviction takes both locks.
        """
        with self._lock:
            recorded = execution_id in self._retention
            if recorded:
                self._artifact_digests[execution_id] = digests
        if recorded:
            self._enforce_retained_ceiling()

    def _seal_retained(self, job):
        """Encrypt the retained artifacts under a fresh per-execution key.

        Returns (sealed, omitted, secured). `secured` is False when plaintext could not be
        removed from artifacts/ and is therefore still on disk; nothing else means that.

        Runs between _retain and build_manifest — after the trim, so nothing is sealed that is
        about to be deleted, and before the manifest, so it can be built over the plaintext
        sizes and digests this pass measured and omit anything that appears afterwards.

        It fails closed. Leaving the artifacts in the clear keeps the response useful and leaves
        the exposure this exists to close; failing the execution 500 throws away stdout the
        caller has already paid for over a failure in the retention path. So what cannot be
        sealed is destroyed and its count is reported through artifacts_omitted, with a
        LOG.error beside it. Destruction is per file wherever it can be —
        seal_retained_artifacts localises a failure to the entry that caused it, so the
        whole-execution purge below runs only for a failure that could not be attributed to one
        file at all.

        The one outcome that is not fail-closed is reported rather than logged over. If the
        plaintext can neither be sealed nor deleted — a same-uid peer chmod 0500 on artifacts/
        between _retain and here — no arrangement of this code removes it. `secured=False` says
        so and _execute_inner turns it into artifacts_retained_in_clear; answering 500 instead
        would be a same-uid denial-of-service kill switch that bought no confidentiality.

        The cached retained size is corrected here: _retain caches a pre-seal number, so the
        envelopes are added back before the ceilings are enforced against it.
        """
        eid = job.req.execution_id
        key = None
        job.sealed = True
        try:
            key = new_artifact_key()
            sealed, purged, growth, stranded = seal_retained_artifacts(
                job.dirs.artifacts, eid, key)
        except Exception as exc:
            # A property, not an enumeration. Naming (ArtifactCryptoError, CryptoUnavailable,
            # OSError) lists the ways the pass was expected to fail rather than stating
            # something about the directory, and any other type escaped past the fail-closed
            # arm and left the plaintext retained. The question is "did the seal complete".
            wipe_artifact_key(key)
            destroyed, emptied = _purge_artifacts(job.dirs.artifacts)
            if emptied:
                LOG.error("execution %s: could not seal its retained artifacts (%s); "
                          "destroyed %d rather than retaining them in the clear",
                          eid, exc, destroyed)
            else:
                LOG.error("execution %s: could not seal its retained artifacts (%s) AND could "
                          "not delete them (%d removed); artifacts ARE RETAINED IN THE CLEAR "
                          "and readable by any process at this uid until the reaper removes "
                          "the directory",
                          eid, exc, destroyed)
            return {}, destroyed, emptied
        if stranded:
            LOG.error("execution %s: %d artifact(s) could neither be sealed nor deleted; they "
                      "ARE RETAINED IN THE CLEAR and readable by any process at this uid "
                      "until the reaper removes the directory",
                      eid, stranded)
        with self._lock:
            recorded = eid in self._retention
            if recorded:
                self._retention[eid][1] += growth
                self._artifact_keys[eid] = key
        if not recorded:
            # Evicted between _retain and here, so the directory is already gone. Keeping the
            # key would leak 32 bytes per eviction and unlock nothing.
            wipe_artifact_key(key)
            return {}, purged + stranded, not stranded
        return sealed, purged + stranded, not stranded

    def _retained_sizes(self):
        """[(execution_id, bytes)] in completion order — which is oldest-first.

        The sizes can double-count: os.scandir follows the path it is handed, so a child that
        replaces its own artifacts/ with a symlink to another execution's directory gets those
        bytes charged twice. The error is conservative — the total reads high, so the ceiling
        evicts sooner — and deletion stays correct, since rmtree does not traverse a symlink and
        _remove_entry unlinks one. Not fixed here: a child that can plant it is already outside
        the containment boundary, where it can do worse than skew a number.
        """
        with self._lock:
            return [(eid, row[1]) for eid, row in self._retention.items()]

    def _retained_total(self):
        with self._lock:
            return sum(row[1] for row in self._retention.values())

    def _retained_memory_costs(self):
        """{execution_id: bytes of supervisor MEMORY the retention of that id holds}.

        Derived rather than stored, so it cannot drift out of step with the two dicts it
        measures. Bounded by the same numbers it enforces: at the ceiling there are at most
        RETAINED_STATE_CEILING_BYTES / RETAINED_ROW_COST_BYTES rows to walk.
        """
        with self._lock:
            costs = {eid: RETAINED_ROW_COST_BYTES for eid in self._retention}
            for eid, digests in self._artifact_digests.items():
                if eid in costs:
                    costs[eid] += sum(len(name) + RETAINED_DIGEST_ENTRY_COST_BYTES
                                      for name in digests)
        return costs

    def _enforce_retained_ceiling(self):
        """Oldest-first eviction until the retained artifact set is under its ceiling.

        Retention degrades gracefully instead of accumulating until the kubelet intervenes — and
        that intervention is a POD EVICTION, destroying every retained artifact in the window
        plus the in-flight script. Losing the oldest artifacts is far cheaper.

        The loop has no `len(sizes) > 1` guard: that guard meant a single over-ceiling execution
        sat above the ceiling permanently, since there was nothing older to evict. It existed to
        protect the newest execution, and per-execution bounds protect it now — the loop stops
        the moment the sum is under the ceiling, so it reaches the newest row only if that row
        alone exceeds 256 MiB. A completed execution cannot (trimmed to 64 MiB); a failure-path
        retention is bounded by the 192 MiB execution quota. Both are bounds on a polled quota,
        so both describe the steady state rather than a hostile burst's transient peak.

        Two ceilings, and the second is the one that bounds RAM. Disk is charged st_size, so
        1024 zero-byte artifacts with 255-byte names measure 0 while costing ~0.5 MB of digest
        map, and the number of retained executions has no count cap.
        RETAINED_STATE_CEILING_BYTES charges retention what it costs in memory and evicts on
        whichever ceiling binds first. It fails closed by construction: eviction is
        _forget_retained, which drops the directory, the id and the digest map together, so
        there is no state in which an execution is readable but no longer verifiable.
        """
        sizes = self._retained_sizes()
        mem = self._retained_memory_costs()
        total = sum(size for _, size in sizes)
        mem_total = sum(mem.values())
        evicted = []
        reasons = set()
        while sizes and (total > RETAINED_ARTIFACTS_CEILING_BYTES
                         or mem_total > RETAINED_STATE_CEILING_BYTES):
            # Which ceiling bound, not just that one did: disk and digest-map memory are
            # different operational facts and want different responses.
            reasons.add("%d MiB of retained artifacts on disk"
                        % (RETAINED_ARTIFACTS_CEILING_BYTES // (1024 * 1024))
                        if total > RETAINED_ARTIFACTS_CEILING_BYTES
                        else "%d MiB of manifest digests in memory"
                        % (RETAINED_STATE_CEILING_BYTES // (1024 * 1024)))
            eid, size = sizes.pop(0)
            self._forget_retained(eid)
            evicted.append(eid)
            total -= size
            mem_total -= mem.get(eid, 0)
        if evicted:
            LOG.warning("retention exceeded %s; evicted %d oldest execution(s): %s",
                        " and ".join(sorted(reasons)), len(evicted), ", ".join(evicted))
        return evicted

    def _forget_retained(self, execution_id):
        """Delete a retained execution's directory and make its id reusable again.

        The key dies here, and this is the only place it can: every route by which a retained
        execution stops existing — the TTL, either ceiling, the orphan sweep — goes through this
        method, so "the key never outlives the entry it belongs to" is a property of the
        structure rather than a rule four call sites have to remember. Wiped in place before it
        is dropped: popping the reference alone leaves the bytes in a freed arena.
        """
        shutil.rmtree(os.path.join(self.scratch_root, execution_id), ignore_errors=True)
        with self._cv:
            self._retention.pop(execution_id, None)
            self._retained_ids.discard(execution_id)
            self._artifact_digests.pop(execution_id, None)
            wipe_artifact_key(self._artifact_keys.pop(execution_id, None))
            self._cv.notify_all()

    def reap_expired(self):
        """Delete retained artifacts past their TTL, and any directory belonging to no execution
        this process knows about. Returns the ids removed.

        Two mechanisms for two failures. The registry covers executions that COMPLETED: their
        artifacts go at the deadline whether or not anything read them, which is what makes
        "nothing persists beyond 5 minutes" true rather than aspirational. The filesystem sweep
        covers a directory whose job died on a path that never reached _retain — an orphan the
        registry has no row for. The sweep uses mtime, so a live or queued id is excluded by
        name first.
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

    def note_child_reaped(self, pid, status):
        """Publish, to the running job, a wait status the PID 1 orphan reaper consumed.

        "Execution children are grandchildren, so a waitpid(-1) here cannot reach them" holds
        only while the fork server is alive — and its death mid-execution is one of the two
        cases the reaper exists for. The child then reparents to PID 1 and a waitpid(-1) takes
        its status, while _reap has already raised ForkServerError, so `job.reaped` is still
        False and `job.pid` still names it.

        That breaks the invariant _kill_group is built on: "a zombie keeps its pgid and its pid
        cannot be recycled until it is reaped". Once the reaper has reaped it the number is the
        kernel's to hand out again, so the stranded path spends KILL_GRACE_S polling a job that
        can never go reaped and then SIGKILLs that number — measured killing a bystander forked
        onto the same pid. The fix is note_reaped's, applied to the execution child: publish the
        status so nothing later polls or signals that pid.

        No lock is taken, for `_closing`'s reason: this runs inside a SIGCHLD handler that
        CPython delivers on the main thread, and a handler that blocks on a lock the interrupted
        thread holds deadlocks PID 1. Every access below is a single attribute load or store,
        `_running` is read once, and both flags a signal path consults are ORed, so no reader
        can observe a half-published state.

        `reaped_pgid` is cleared because _kill_survivors is the second path that could signal a
        freed pid: _reap stamps the pgid BEFORE the waitpid that can fail, so a fork server
        dying between the two leaves the pgid stamped and the else branch calls _kill_survivors
        unconditionally. For a setsid() child that pgid is the child's pid. Clearing it makes
        _kill_survivors' existing `pgid is None` guard the answer here too.

        What remains is one signal, once: a _signal_group that has already passed its
        `job.reaped` check will still deliver that one signal, a window measured at 3.1
        microseconds. It is not closable from here — waitpid reports whose status it took only
        after taking it. The escalation is refused, so the grace loop, the SIGKILL and every
        later signal are gone, which is where the exposure was.

        Returns True when `pid` was the running execution's child and this call marked it
        reaped; False for an already-reaped job.
        """
        job = self._running  # read ONCE: a serving thread clears it in _release
        # `job.reaped` is part of the match, not a redundant re-check: _reap never clears
        # job.pid and _release does not clear _running until the response is built, so a job
        # reaped normally still names its pid here. If the kernel recycled that number this
        # would stamp a foreign status onto a healthy execution.
        if job is None or job.reaped or pid != job.pid:
            return False
        # Cleared before `reaped`, so anything that observes the job reaped also observes the
        # pgid gone.
        job.reaped_pgid = None
        job.reaped_status = status
        job.reaped = True
        job.pid = None
        return True

    def _execute(self, job):
        # In a finally because every other arrangement leaks a credential. The unlink that
        # matters is the one below, the moment the child is reaped; this one is for the paths
        # that never get there — a fork OSError, an exception while wiring the pipes — which
        # otherwise leave a mode-0600 token file on disk until the reaper notices.
        try:
            return self._execute_inner(job)
        finally:
            try:
                os.unlink(job.dirs.tokens)
            except OSError:
                pass

    def _sweep_strays(self, job):
        """Ask the fork server to kill and reap whatever reparented to it.

        Never raises: it runs inside _execute_inner's finally, where an exception would replace
        the one being propagated — and the fork server being dead is precisely one of the cases
        that gets here. A sweep that could not run leaves the strays alive, which is the
        behaviour this replaced; it is logged and the response is still correct.

        The two lists are not the same event. A live stray outlived its execution and is
        evidence a control failed. A reparented zombie is an ordinary orphan the script never
        waited on, which left no running process behind; reporting it at the same level would
        make a routine pattern a standing false alarm.
        """
        fs = self.forkserver
        if fs is None:
            return
        try:
            swept, reaped = fs.sweep()
        except Exception as exc:
            LOG.error("execution %s: could not sweep reparented strays: %s",
                      job.req.execution_id, exc)
            return
        if swept:
            LOG.warning("execution %s: killed %d process(es) that had escaped the child's "
                        "process group and outlived it: %s",
                        job.req.execution_id, len(swept), swept)
        if reaped:
            LOG.info("execution %s: reaped %d orphaned zombie(s) the script never waited on: "
                     "%s", job.req.execution_id, len(reaped), reaped)

    def _execute_inner(self, job):
        if self.forkserver is None:
            # bring_up() starts it before `ready` and /execute answers 503 until then, so this
            # is unreachable through the wire contract. It is here so a Supervisor built
            # directly by a unit test fails saying what is missing.
            raise RequestError(503, "NotReady", "the fork server is not running")
        dirs = job.dirs
        seed_mplconfig(dirs.mplconfig)
        _deliver_tokens(job)

        # Every descriptor an execution creates is made inside this try. _payload_fd raises
        # OSError for real reasons (memfd_create ENOMEM, ENOSPC on the fallback against the
        # 512Mi emptyDir), dirs.child_env can raise, and os.pipe() itself raises EMFILE under
        # the fd exhaustion that is the only realistic driver here — with any of the calls
        # outside the try, a failure leaked the descriptors already made, in exactly the state
        # where the process can least afford them. All seven names are bound to None first so
        # the handler is safe before any descriptor exists.
        payload_fd = out_r = out_w = st_r = st_w = audit_r = audit_w = None
        try:
            out_r, out_w = os.pipe()
            st_r, st_w = os.pipe()
            # Created before the fork because that is the only way a descriptor reaches a
            # forked child, and read by this process alone.
            audit_r, audit_w = os.pipe()
            claims = job.req.claims[TOKEN_AUDIENCES[0]]
            env = dirs.child_env(claims)
            # The stamp comes from the claims, not from the body and not from the child.
            # parse_execute_request has already refused the request unless both tokens agree,
            # so either audience will do; taking them from the token is what makes this
            # evidence rather than an echo.
            audit = _AuditForwarder(
                str(claims.get("sub", "")), str(claims.get("sid", "")), str(claims.get("jti", ""))
            )
            # The code and the environment go out as a descriptor, not as arguments, and this
            # process does not fork. The fork server receives four numbers and the word "fork".
            payload_fd = _payload_fd(
                {"code": job.req.code, "env": env, "cwd": dirs.tmp}, dirs.base)
            started = time.monotonic()
            pid = self.forkserver.fork_child(payload_fd, out_w, st_w, audit_w)
        except BaseException:
            # The fork server owns nothing yet, so every descriptor is still this process's to
            # close. Leaking the write ends would leave all three drains blocked on a pipe that
            # never reaches EOF.
            _fs_close_all([fd for fd in (payload_fd, out_w, st_w, audit_w, out_r, st_r, audit_r)
                           if fd is not None])
            raise
        os.close(payload_fd)
        os.close(out_w)
        os.close(st_w)
        os.close(audit_w)
        job.pid = pid
        job.deadline = started + job.req.timeout_s
        # No pgid is cached here. Reading os.getpgid(pid) after the fork routinely measured the
        # SUPERVISOR'S own group — the child's setsid() is its first statement but the parent
        # still wins the race — and the parent cannot call setpgid(pid, pid) instead, because
        # that makes the child a group leader and setsid() then fails EPERM. _resolve_pgid
        # resolves it fresh at every use and refuses to hand back the supervisor's own group.
        _apply_limits(job)

        out_box = {}
        st_box = {}
        audit_box = {}
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
        # limit=None: with a sink, _drain buffers nothing and applies no bound of its own. The
        # byte, rate and per-line caps are the forwarder's, applied as the bytes arrive.
        t_audit = threading.Thread(
            target=lambda: audit_box.update(
                zip(fields, _drain(audit_r, None, reaped, sink=audit.feed))),
            daemon=True,
        )
        t_out.start()
        t_st.start()
        t_audit.start()
        try:
            wait_status = _reap(job, self.forkserver)
            # The child's own lifetime, measured before the drain. Timing the drain reports a
            # number no process spent running whenever a descendant escapes.
            duration_ms = int((time.monotonic() - started) * 1000)
        finally:
            # A job that was forked but not reaped has a child nobody will ever kill, and
            # setting job.done first made that permanent: _watchdog returns immediately on
            # job.done without firing a limit or killing the group, and neither _execute nor
            # run kills on its error path. _reap raises ForkServerError whenever the fork server
            # dies mid-execution, so that at t+1s of a 120s execution left the user's code
            # running for the pod's lifetime. _kill_group goes through killpg directly, never
            # the control socket, so it still works with a dead fork server.
            with job.kill_lock:
                stranded = job.pid is not None and not job.reaped
            if stranded:
                LOG.error("execution %s: the reap did not complete; killing the child's group",
                          job.req.execution_id)
                _kill_group(job)
            else:
                if job.reaped_status is not None:
                    # Not stranded, but not an ordinary completion: the fork server died and
                    # the PID 1 reaper took the child's status. Without this line the case that
                    # used to log "the reap did not complete" would pass in silence.
                    LOG.error("execution %s: the fork server died mid-execution and the PID 1 "
                              "orphan reaper consumed the child's wait status; its pid was "
                              "published rather than signalled", job.req.execution_id)
                # The ordinary completion, which used to signal nothing at all.
                _kill_survivors(job)
            # Then whatever left the process group entirely. Before the drain joins, so an
            # escapee holding the output pipe's write end is not the reason _drain waits out
            # its grace, and before _retain and build_manifest, so the manifest is hashed over
            # a directory nothing is still writing to.
            self._sweep_strays(job)
            job.done.set()
            reaped.set()
            t_out.join(DRAIN_GRACE_S + 5.0)
            t_st.join(DRAIN_GRACE_S + 5.0)
            t_audit.join(DRAIN_GRACE_S + 5.0)
            # Closing an fd another thread is blocked on is undefined, so a thread that
            # outlived its own deadline costs two leaked descriptors rather than a read against
            # a reused number. _drain always returns within `grace`, so this is a backstop.
            for name, thread, fd in (("stdout", t_out, out_r), ("status", t_st, st_r),
                                     ("audit", t_audit, audit_r)):
                if thread.is_alive():
                    LOG.error("%s drain thread for %s did not stop; leaking its read end",
                              name, job.req.execution_id)
                else:
                    os.close(fd)
            # After the join, so nothing is still feeding it, and in the finally so an
            # execution that failed above still accounts for its own audit stream. Emitted
            # unconditionally, so "made no SDK calls" and "records were dropped" are different
            # lines rather than the same silence.
            audit.close()
        if out_box.get("abandoned") or st_box.get("abandoned") or audit_box.get("abandoned"):
            # Not an error for this response: the child is reaped and its answer is complete.
            # It means something held the write end past the grace DESPITE the kill and the
            # sweep — a dead fork server, PR_SET_CHILD_SUBREAPER not taking, or a process
            # outside this pod's reach. The warning is evidence that a control failed.
            LOG.warning(
                "execution %s: a descendant outlived the child and still holds the output "
                "pipe; drain abandoned after %.1fs", job.req.execution_id, DRAIN_GRACE_S)

        exit_code = os.WEXITSTATUS(wait_status) if os.WIFEXITED(wait_status) else None
        sig = os.WTERMSIG(wait_status) if os.WIFSIGNALED(wait_status) else None

        # The token file goes the moment the child is reaped, whether or not the SDK read it.
        # _retain deletes it too; this covers the script that never made a data call, where the
        # file would otherwise sit there for as long as the response takes to build.
        try:
            os.unlink(dirs.tokens)
        except OSError:
            pass

        # Trim, then seal, then list, and each step depends on the one before. _retain brings
        # artifacts/ back inside its quota, so nothing is encrypted that is about to be deleted.
        # _seal_retained encrypts what is left and measures the plaintext sizes and digests
        # while the plaintext still exists. build_manifest then describes the plaintext — the
        # bytes a caller will receive — and omits anything that turned up after the seal.
        trimmed = self._retain(job)
        sealed, purged, secured = self._seal_retained(job)
        # The one thing artifacts_omitted cannot say, carried in its own field. Every other
        # seal failure ends with the plaintext gone, and a larger omitted count is a truthful
        # account of it. This one ends with the plaintext still on disk at a shared uid, where
        # "produced, present, not listed" would claim a property the code did not achieve.
        #
        # Not a 500, because that would be a same-uid kill switch that buys no confidentiality.
        # Measured 3 for 3: a second process at this uid polling /scratch/*/artifacts and chmod
        # 0500-ing any directory holding a file made every execution answer 500 with output
        # None. And the 500 protects nothing — in the only case that produces it, DELETION is
        # what failed, so the peer already holds the plaintext either way. What the caller keeps
        # by the 200 is the analysis it paid for, plus an explicit statement that its artifacts
        # are readable at this uid.
        artifacts, omitted, digests = build_manifest(dirs.artifacts, sealed=sealed)
        self._record_digests(job.req.execution_id, digests)

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
            artifacts_omitted=omitted + trimmed + purged,
            artifacts_retained_in_clear=not secured,
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
        artifacts_retained_in_clear=False,
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
            # A supervisor limit wins over how the process happened to die, including over a
            # clean exit 0: a child that traps SIGTERM and exits zero still ended because the
            # supervisor decided it should, and reporting "ok" would tell the model its
            # analysis completed when its output was cut off. `exit_code` and `signal` still
            # report the truth about the process.
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
            # The supervisor's own observation wins. The status pipe is fd 3 in a child that
            # is forked and not exec'd, so a script can forge {"type": ...} and then exit 0,
            # turning a successful run into status "error" with exit_code 0 — a row the
            # contract's status table says cannot exist. An uncaught exception always leaves a
            # non-zero exit, so no legitimate record is lost. The record stays untrusted input
            # everywhere else.
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
            # something outside this process. The supervisor's own kills set job.limit first.
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
            # Two independent ways output is incomplete, folded into one flag: the 8 MiB pipe
            # cap fired, or the 64 KiB return window elided a middle.
            "output_truncated": bool(out_stopped or elided),
            "error": error,
            "artifacts": artifacts,
            "artifacts_omitted": artifacts_omitted,
            # Separate from artifacts_omitted on purpose: omission is recoverable and says
            # nothing about exposure, while this says the seal pass could neither encrypt nor
            # delete what the script wrote.
            "artifacts_retained_in_clear": artifacts_retained_in_clear,
        }


# --------------------------------------------------------------------------------------
# Per-execution limits: the wall clock, the pid budget, the /scratch quotas and the kill path.
# One watchdog thread polls all four, because they share a poll interval and a single kill
# path, and four timers would give four chances to get the reap race wrong.
# --------------------------------------------------------------------------------------

# One row per reason _fire_limit is called with, except ERR_TIMEOUT, which _response answers
# in its own branch. _response indexes this directly, so a new reason without a row is a
# KeyError there rather than a bad message. ERR_MEMORY_LIMIT has no row because nothing fires
# it — see the reserved-names block above.
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
    reason the supervisor acted on, not whichever poll ran last.

    A reaped child cannot have a limit fire on it. _watchdog enters a poll body with `job.done`
    clear and then compares the clock against the deadline; if _reap returns inside that body
    the run is already complete, and recording ERR_TIMEOUT anyway made _response — which gives
    job.limit absolute priority — answer "timeout" for a clean run, discarding its output and
    its manifest. Nothing was ever wrongly KILLED; the model was told the wrong thing. Checking
    `reaped` under the same lock the reap sets it under closes the one-poll window.
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

    What this reaches: the child and every descendant that stayed in its process group. A
    descendant that calls setsid() is not in the group and is not signalled — measured, with
    killpg returning ESRCH while the escapee kept running. Reaching that one is the fork
    server's FS_OP_SWEEP, by parentage rather than by group.

    Every signal re-reads the pgid under job.kill_lock, and neither half is optional. A pgid
    cached at fork time goes stale the instant waitpid reaps the child, and the pid is then
    reusable. Holding the lock is what makes "not reaped yet" true for the duration of the
    signal: a zombie keeps its pgid and its pid cannot be recycled until it is reaped.

    "Gone" and "failed" are different answers to SIGTERM and only one may skip the SIGKILL.
    ProcessLookupError means the group has already exited. A transient OSError out of
    getpgid/killpg means delivery failed against a process that is still running, and treating
    that as "already gone" forfeits the escalation for exactly the child that needed it.
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


def _kill_survivors(job):
    """After the reap: SIGTERM the child's process group, SIGKILL after KILL_GRACE_S. Bounded.

    The success path signalled nothing at all before this existed. The two _kill_group call
    sites are _fire_limit and the stranded path, and a normal completion has reaped=True, so it
    hit neither: after a status-ok execution a plain forked grandchild that stayed in the pgid
    was still running, and would have been for the pod's lifetime.

    It cannot route through _signal_group, and that is the guard being read correctly rather
    than dodged. _signal_group answers _SIGNAL_GONE for a reaped job on purpose: after waitpid
    the pid is free for the kernel to reuse. This function signals a reaped child's GROUP, a
    different object with different lifetime rules, and takes three guards instead:

      * the value was read while the zombie was held — _reap resolves it under kill_lock
        between waitid(WNOWAIT) and waitpid, so it named this child's group and the pid was not
        recyclable. The one path where that stops being true is a reap that never completes,
        and Supervisor.note_child_reaped clears the slot for exactly that reason;
      * a process group with live members cannot be recycled: the kernel keeps the number
        allocated while anything has it as its pgrp, so if this kill has anything to reach, the
        number still means what it meant. killpg on an empty group is ESRCH;
      * the own-group guard is re-applied against the live value, because signalling that group
        would kill the supervisor, the fork server and every future execution.

    It reaches descendants that stayed in the child's process group, not a setsid() escapee —
    that is the fork server's FS_OP_SWEEP, which runs immediately after.
    """
    pgid = job.reaped_pgid
    if pgid is None or pgid == os.getpgrp():
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return False  # nothing stayed behind: the ordinary completion
    except OSError as exc:
        LOG.error("execution %s: signalling the completed child's group %s failed: %s",
                  job.req.execution_id, pgid, exc)
        return False
    LOG.warning("execution %s: the execution completed but its process group %d still has "
                "members; killing them", job.req.execution_id, pgid)
    deadline = time.monotonic() + KILL_GRACE_S
    while time.monotonic() < deadline:
        members = _group_members(pgid)
        if members is not None and not any(_pid_is_live(pid) for pid in members):
            return True
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
    # Whether the SIGKILL worked is not assumed: returning True whatever happened made a group
    # that outlived escalation indistinguishable from one that died. Delivery is asynchronous,
    # so give it a short bounded look rather than reading the instant after the signal.
    settle = time.monotonic() + 0.25
    while True:
        members = _group_members(pgid)
        alive = None if members is None else [p for p in members if _pid_is_live(p)]
        if not alive or time.monotonic() >= settle:
            break
        time.sleep(0.02)
    if alive:
        LOG.error("execution %s: process group %d still has live members after SIGKILL: %s; "
                  "the fork server's sweep is the only thing left that can reach them",
                  job.req.execution_id, pgid, sorted(alive))
    return True


def _resolve_pgid(job):
    """The child's OWN process group, read live, or None if it does not have one yet.

    The guard is the function. A pgid equal to the supervisor's own group means the child has
    not reached setsid() (or never will), and killpg on that value would signal the SUPERVISOR —
    measured, not hypothesised: reading the pgid immediately after the fork returned the
    supervisor's group routinely.

    The fork server deliberately does not setsid(), and this guard is why: it stays in the
    supervisor's group, so a child that has not reached its own setsid() reports that group and
    is caught here. A fork server in a group of its own would report a pgid this test does not
    recognise, and the first killpg would take out every future execution.

    Callers treat None as "no group to signal or count", never as "the group is empty". The
    caller holds job.kill_lock, so job.pid cannot be reaped and recycled while this reads it.
    """
    if job.pid is None:
        return None
    return _own_pgid(job.pid)


# _signal_group's three answers. "gone" and "failed" were one value, which meant a transient
# OSError from getpgid or killpg silently forfeited _kill_group's SIGKILL escalation.
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
                # No group of its own, so signal the child alone rather than the supervisor's
                # group.
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


def _reap(job, waiter=None):
    """Block until the child exits, then reap it under job.kill_lock. Returns wait status.

    `waiter` is the process that owns the child: in production the ForkServer, because the child
    is a grandchild here and waitpid from the supervisor would raise ECHILD. The default reaps a
    child of this process and is used only by tests; the two-step structure is identical either
    way, which is why it is routed through an object rather than a branch.

    waitid(WNOWAIT) blocks without consuming the zombie, so the wait costs nothing and the pid
    stays un-recyclable; the waitpid and the `reaped` flag are then set together under the lock.
    A plain blocking waitpid cannot do this — it reaps before any lock can be taken, leaving a
    window in which the watchdog's killpg targets a recycled pid.

    The fallback must not block under the lock: `with kill_lock: waitpid(pid, 0)` holds it for
    the child's entire remaining lifetime, and that is the lock _signal_group takes and
    _kill_group's grace loop polls — a non-terminating child could then never be signalled at
    all. So the fallback polls WNOHANG, holding the lock only for the syscall that reaps.
    """
    waiter = SELF_WAITER if waiter is None else waiter
    if not waiter.wait_nowait(job.pid):
        while True:
            with job.kill_lock:
                # The last value read before the reap that succeeds is the one
                # _kill_survivors uses; taking it here rather than after is why it is sound.
                job.reaped_pgid = _resolve_pgid(job)
                wait_status = waiter.reap(job.pid, nohang=True)
                if wait_status is not None:
                    job.reaped = True
                    return wait_status
            time.sleep(0.02)
    with job.kill_lock:
        job.reaped_pgid = _resolve_pgid(job)
        wait_status = waiter.reap(job.pid)
        job.reaped = True
    return wait_status


def _proc_stat_fields(pid):
    """/proc/<pid>/stat as [state, ppid, pgrp, ...], or None if it could not be read.

    comm is parenthesised and may itself contain spaces and parens, so the split is after the
    LAST ')' — everything before it is pid and comm, everything after is fixed-position.
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None  # exited between the listdir and the open
    cut = raw.rfind(b")")
    if cut < 0:
        return None
    return raw[cut + 2:].split()


def _proc_scan(field, value):
    """The pids whose stat `field` equals `value`, or None if /proc could not be read.

    None is not an empty list and callers must not treat it as one: an unreadable /proc means
    the question is unanswerable, which is a degradation to report rather than a negative
    answer. Whether /proc process inspection behaves the same under gVisor is unverified.
    """
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    found = []
    for name in names:
        if not name.isdigit():
            continue
        fields = _proc_stat_fields(name)
        if fields is None or len(fields) <= field:
            continue
        try:
            if int(fields[field]) == value:
                found.append(int(name))
        except ValueError:
            continue
    return found


def _group_members(pgid):
    """The pids in `pgid`, or None if /proc could not be read. The pid budget's counter."""
    return _proc_scan(2, pgid)


def _pid_is_live(pid):
    """False when `pid` is gone or is a ZOMBIE.

    A zombie keeps its entry and its pgrp until somebody reaps it, so a group kill that waits
    for the group to EMPTY waits out its whole grace on processes that are already dead and
    then escalates to SIGKILL against nothing. The pid budget deliberately does NOT use this:
    a zombie still occupies a pid against the pod's limit, which is what that budget counts.
    """
    fields = _proc_stat_fields(pid)
    return bool(fields) and fields[0] not in (b"Z", b"X", b"x")


def _child_pids(ppid):
    """The pids whose PARENT is `ppid`, or None if /proc could not be read.

    Parentage rather than process group, because that is the relation a descendant cannot
    leave: setsid() moves a process out of every group the supervisor can name, and moves it
    out of nothing here. See _fs_sweep, which is the only caller and runs inside the fork
    server — the subreaper the escapees end up under.
    """
    return _proc_scan(1, ppid)


def _dir_usage(path, entry_limit, sub=None):
    """ONE pass over `path`. Returns (cost, entries, sub) where `sub` is (cost, entries) for the
    `sub` subtree if given and (0, 0) otherwise.

    Cost is st_blocks plus a per-entry floor, and both halves are needed. st_blocks is what the
    kubelet's emptyDir accounting sees for data, and the difference from st_size is reachable on
    purpose: `f.seek(512 << 20); f.write(b"x")` makes a file whose apparent size is 512 MiB and
    whose blocks are nearly none. DIRENT_COST_BYTES is the other half — 300,000 zero-length
    files charged 8.6 MB against a 192 MiB quota, so no limit fired, while the response reached
    19.8 MB and the supervisor's RSS went 22 MB -> 166 MB.

    `entry_limit` bounds the walk itself. The scan runs on the watchdog thread between two
    deadline checks, and one pass over 800,000 empty files took 8.47s and reported 0 bytes —
    with timeout_s=30 the child was killed at 46.74s. Stopping at the limit is sound because the
    limit IS a budget: a tree with more entries than the budget allows is over it either way.
    The caller is told by `entries > entry_limit`.
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

    Newest-first, by mtime with the name as tiebreak: the entry that blew the quota is the one
    being written when the kill landed, and the smaller deliberate artifacts a script produced
    earlier are worth keeping. One policy covers both budgets.

    The policy is blind to size, and the case it loses badly is ordinary: a 100 MiB CSV written
    first, then fifty small plots. The plots go first and the CSV goes too, so everything is
    lost and the plots were lost for nothing. Left as it is deliberately — recency is the right
    signal for the hostile case the trim exists for, and a size-aware pass would delete the
    large output a script was asked to produce while keeping incidental ones. It is a behaviour
    rather than a budget violation, and it is stated in read_artifact's contract where a user
    reads what may be missing.

    Subdirectories are deleted whole and count like any other entry: a bare name cannot address
    their contents, but their bytes count against the same volume.

    The enumeration is not capped at EXECUTION_ENTRY_BUDGET, a bound that was circular because
    the trim is what MAKES the tree small. Capped, it sorted a truncated 20,000-name sample and
    derived both the surviving entry count and the returned size from it: on 25,000 zero-length
    files it left 6,024 entries against a 1,024 budget and reported 0.5 MiB where _dir_usage
    measured 2.9 MiB — and that number is what _retain caches, so the aggregate check could not
    fire and _enforce_retained_ceiling evicted against a fiction.

    So it drains in bounded passes. A pass that sees the whole directory does the exact
    newest-first trim and returns an exact total. A pass that fills its chunk cannot order its
    sample against the unseen remainder, so it drops the sample whole and re-scans. On the
    give-up path the size returned is a real measurement, never a sample.
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
    """Parent side. Runs immediately after the fork and must not block.

    Raises the child's oom_score_adj and starts the watchdog thread that owns the wall clock,
    the pid budget, the per-execution /scratch quotas and the aggregate ceiling.

    What the oom_score_adj raise is worth, measured rather than assumed: the child starts at 0,
    writing 500 succeeds, and writing 0 again also succeeds from inside the child at any time.
    Only going below the inherited floor is refused. So a script can undo this, and the honest
    guarantee is not "+500 holds" but "the child can never make itself a better OOM candidate
    than the supervisor". The supervisor's own -500 is unreachable at runtime for the same
    reason and is a pod-spec change.
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
        # Best effort with a visible failure: if this stops working the cgroup OOM killer goes
        # back to choosing between two processes by RSS heuristic, and an operator needs to see
        # that in the log rather than infer it from a dead pod.
        LOG.warning("execution %s: could not raise the child's oom_score_adj (%s): %s",
                    job.req.execution_id, path, exc)


def _watchdog(job):
    """Poll the wall clock, the process-group size and the /scratch budgets until the reap.

    The wall clock is on its own timer and never behind the filesystem scan. Checked once per
    tick at the top, with two full tree walks after it, the deadline was as late as the child
    chose to make it: with timeout_s=30, 0 files killed at 30.23s, 200,000 empty files at
    45.51s, 800,000 at 46.74s. Three things fix it and all three are load-bearing — the wait
    shrinks as the deadline approaches so the tick cannot straddle it, the scan is entry-bounded
    so it has a worst case at all, and the clock is checked again immediately after the scan so
    an overrun fires on this tick rather than the next.

    One walk per tick, not two: _dir_usage measures base and the artifacts subtree in a single
    pass, which is what the `sub` argument exists for.

    The aggregate check is the invariant that matters and nothing measured it during a run: the
    per-execution quota bounds one execution and the retained ceiling bounds the completed set,
    but the emptyDir sizeLimit is charged the sum. Retained sizes are cached, so this costs an
    addition rather than a second walk.
    """
    dirs = job.dirs
    proc_unreadable_logged = False
    scan_due = 0.0
    while True:
        now = time.monotonic()
        # Never sleep past the deadline, or the tick straddles it and the kill lands up to
        # WATCHDOG_POLL_S late even when nothing is slow.
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
        # None means the child has no group of its own yet (or is gone), not that its group is
        # empty. Skipping the check is the only safe reading: the alternative counts the
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
    """Child side. Runs in the child, before the script, and applies RLIMIT_AS.

    Separate from _apply_limits because setrlimit on another process needs CAP_SYS_RESOURCE,
    which the pod drops. It takes no `job` for the reason the fork model demands generally:
    nothing in the parent's object graph is safe to reach for from here.

    Hitting RLIMIT_AS gives a clean MemoryError inside the child, a better failure than a cgroup
    OOM kill in either direction, because the kernel picks the victim by a heuristic over RSS
    and gVisor changes the accounting again.

    The hard limit is lowered too, and that is the whole control. Setting only the soft limit
    made this opt-out: raising a soft limit back to the hard limit is unprivileged, so
    `setrlimit(RLIMIT_AS, (RLIM_INFINITY, RLIM_INFINITY))` from the script succeeded, after
    which allocating 2900 MiB produced exactly the cgroup OOM kill this prevents. Lowering a
    hard limit is irreversible without CAP_SYS_RESOURCE, which the pod drops.
    """
    try:
        import resource

        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = CHILD_RLIMIT_AS_BYTES
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception as exc:
        # The child's own setup failing is a StartupFailure the caller already reports, rather
        # than a silent unlimited run.
        raise RuntimeError(f"could not apply RLIMIT_AS: {exc}") from exc


# --------------------------------------------------------------------------------------
# Token delivery
# --------------------------------------------------------------------------------------


def _deliver_tokens(job):
    """Write the per-execution tokens where the child can read them, and nowhere else.

    A mode-0600 JSON file inside /scratch/<execution_id>, whose PATH is named to the child by
    SANDBOX_TOKEN_FILE. The tokens never enter the supervisor's own environment, never reach the
    pod spec, a ConfigMap or a Secret, and the supervisor never reads the file back. The SDK
    opens it once, unlinks it, and picks the token by destination — they are audience-bound and
    a cross-audience token is a hard 401.

    What this is and is not an exposure bound against. Three routes were measured against the
    original shape, in which the process holding tokens was also the process that forked: a raw
    /proc/self/mem scan in the child; every reference route (module globals, a frame walk,
    gc.get_objects()); and a detached setsid() grandchild of an EARLIER execution reading this
    execution's mode-0600 file inside the read-once window. The first two are closed, by the
    fork server rather than by anything here. The third is not — it does not depend on the fork
    at all, only on one shared uid and a file with a name — so mode 0600 bounds nothing against
    a same-uid resident, and the read-once unlink narrows only the window.

    It is also the route that makes the fork-server design possible: the child needs its
    credential and the fork server must not carry it, and a file the supervisor writes and the
    child opens is a route from one to the other that does not pass through the process in
    between. That is this file's load-bearing property.

    No chown and not mode 0400: the pod holds no CAP_CHOWN or CAP_SETUID (both measured EPERM),
    and 0400 without the chown would exclude the child, which is the process that needs to read
    it.

    Refusing to run uncredentialed is the other half and it lives in parse_execute_request,
    which rejects an incomplete or disagreeing token set before any directory is created. It
    matters because db-api's fail-open branch — unset INTERNAL_API_SECRET disables auth with a
    startup warning — is what an uncredentialed run would reach.
    """
    raw = json.dumps(job.req.tokens).encode("utf-8")
    fd = os.open(job.dirs.tokens, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, raw)
    finally:
        os.close(fd)
    # The sub/sid/jti the audit stamping needs are already on the request as job.req.claims,
    # checked against the body at parse time. The supervisor is the only component that both
    # holds the token and sits outside the child's address space, which is why the stamping is
    # here rather than in the SDK.


# --------------------------------------------------------------------------------------
# The SDK audit stream
#
# The child writes SDK audit records on CHILD_AUDIT_FD; the supervisor holds the read end and is
# the only thing that decides what is recorded. Three sites make that true and all three are
# load-bearing: the fd exists BEFORE the fork (_execute_inner), it is dup'd onto a fixed number
# and kept out of the close sweep (_child_main), and its number is named to the SDK in the
# child's environment (ExecutionDirs.child_env).
#
# Why the read end and not the SDK: every in-process control was defeated by running it. Records
# were forged with a logger call and with os.write to the fd number the script reads from its
# own environment, and silenced with logger.disabled, the level, a filter and handler removal.
# The audited code and the emitter share an address space. Nothing below tries to defend the
# child's side of the fd — it assumes the child owns it completely.
# --------------------------------------------------------------------------------------


# The three fields the supervisor STAMPS, checked the way _sanitise_error_type checks a
# child-supplied error.type: a value that breaks the framing produces a line an operator's
# tools read back as something else. These come from the tokens, so this is not defence against
# the script — `sub` is whatever the identity provider put in the claim, and a `]` in it would
# close the bracket early. Replaced, never truncated: `<invalid>` keeps the field's position and
# is unmistakably not an identity, where truncating `admin@finngen.fi.attacker.test`
# manufactures a different, credible-looking one.
_AUDIT_IDENTITY_RE = re.compile(r"\A[A-Za-z0-9_.:/@|+-]{1,64}\Z")
_AUDIT_BAD_IDENTITY = "<invalid>"

# What a record is allowed to look like. The child's framing is untrusted input, so a line is
# not cleaned up — it either matches one of these exactly, marker to end of line, or it is
# dropped and counted. Anything laxer re-opens the forgery this exists to close: search()-based
# parsers match a record ANYWHERE in a line, so a child appending a second well-formed record
# to an ordinary one would write a genuine-looking access under someone else's name.
#
# The charsets are bounded by what the SDK can emit and, where that bound is weaker than it
# looks, by what may go on an operator's stream. Function names are Python identifiers and
# exception types dotted identifiers. The argument summary is the SDK's dict rendering — plus
# the bare string `<unavailable>`, with no braces, which is what it returns when
# signature.bind_partial raises: one extra positional argument in an ordinary script produces
# that, so omitting it put a genuine record into dropped_unparseable, which reads as tampering.
#
# The class is tighter than the SDK's, which is ASCII already, because `<type>` renders
# `type(value).__name__` and a script owns that outright. Printable ASCII minus brackets,
# braces and backslash costs the SDK nothing and buys two things: the operator's bracket
# framing stays unforgeable from inside the summary, and a record stays ONE line — U+2028,
# U+2029 and U+0085 each split a line under str.splitlines().
_AUDIT_FN_RE = r"[A-Za-z_][A-Za-z0-9_]{0,63}"
_AUDIT_ERR_RE = r"[A-Za-z_][A-Za-z0-9_.]{0,63}"
_AUDIT_ARGS_RE = r"(?:\{[^{}\[\]\\\x00-\x1f\x7f-\U0010ffff]{0,1024}\}|<unavailable>)"

# `[0-9]`, never `\d`: Python's `\d` matches every Unicode decimal digit, so a record with
# Arabic-Indic digits was forwarded and the analyzer's int() read it back as a row count
# nobody wrote.
_AUDIT_DIGITS = "[0-9]"

_AUDIT_BODY_RES = (
    re.compile(
        rf"\AExecuting SDK function: {_AUDIT_FN_RE} with input: {_AUDIT_ARGS_RE} "
        rf"rows: {_AUDIT_DIGITS}{{1,12}}(?: error: {_AUDIT_ERR_RE})?(?: cancelled)?\Z"
    ),
    re.compile(
        rf"\ARejected SDK function: {_AUDIT_FN_RE} with input: {_AUDIT_ARGS_RE} "
        rf"error: {_AUDIT_ERR_RE}\Z"
    ),
    # The SDK's own refusal-budget notice, admitted as a fixed literal with one number in it.
    # The number is child-supplied and nothing here can check it — the supervisor does not
    # count the SDK's refusals. What is bounded is the literal text around it: the notice
    # cannot become a channel for chosen prose, and it carries no `rows:` field so it can never
    # be read as a data access. The cross-check is the supervisor's own per-execution summary,
    # which the child cannot write. The SDK's other meta record, the shared-stream warning, is
    # deliberately not admitted: it says the records may be forged because no dedicated fd was
    # configured, which on this path is false.
    re.compile(
        rf"\ASDK audit truncated after {_AUDIT_DIGITS}{{1,9}} records; further REFUSED SDK "
        r"calls in this process are NOT recorded\. Calls that reached the executor are still "
        r"recorded in full\.\Z"
    ),
)

_AUDIT_MARKERS = (
    "Executing SDK function: ",
    "Rejected SDK function: ",
    "SDK audit truncated after ",
)

# Fixed text, one per cap, emitted at most once per execution. No child-chosen bytes: a notice
# that quotes what it is complaining about hands the thing being bounded a way to write into
# the operator's log. None of them can parse as a data access.
_AUDIT_NOTICES = {
    "line": "SDK audit stream: a record over %d bytes was DROPPED (not truncated)"
            % AUDIT_LINE_MAX_BYTES,
    "rate": "SDK audit stream: over %d records/s; records are being DROPPED"
            % int(AUDIT_RATE_PER_S),
    "bytes": "SDK audit stream: past the %d-byte per-execution budget; records are being "
             "DROPPED" % AUDIT_STREAM_MAX_BYTES,
    "unparseable": "SDK audit stream: a line that is not a well-formed SDK record was DROPPED",
}

_AUDIT_EMIT_LOCK = threading.Lock()


def _audit_identifier(value):
    return value if _AUDIT_IDENTITY_RE.match(value or "") else _AUDIT_BAD_IDENTITY


def _audit_record_body(text):
    """The re-framable body of one child line, or None if there is not one.

    Everything before the marker — the SDK's asctime, logger name, level, and the
    `[user=…] [session=…] [execution=…]` prefix it renders from the child's own environment —
    is discarded rather than parsed. That prefix is exactly the field this exists to stop
    believing, and re-emitting any of it would put child bytes on stdout under supervisor
    framing.
    """
    if text.endswith("\r"):
        text = text[:-1]
    start = None
    for marker in _AUDIT_MARKERS:
        found = text.find(marker)
        if found >= 0 and (start is None or found < start):
            start = found
    if start is None:
        return None
    body = text[start:]
    for pattern in _AUDIT_BODY_RES:
        if pattern.match(body):
            return body
    return None


def _audit_emit(text):
    """Put one already-framed record on the pod's own stdout, and flush it.

    Written to the stream rather than through LOG, for two delivery reasons. The logging
    configuration belongs to main(), so a supervisor embedded differently would discard the
    whole control at the root logger's default level, silently. And stdout is block-buffered
    when it is a pipe, which it is under both `docker logs` and the kubelet, so an unflushed
    record arrives minutes late or never.
    """
    # The same shape logging's %(asctime)s renders in main()'s basicConfig: one stdout stream
    # carrying two timestamp formats makes a reader parse two.
    now = time.time()
    stamp = "%s,%03d" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                         int((now % 1) * 1000))
    with _AUDIT_EMIT_LOCK:
        try:
            sys.stdout.write(f"{stamp} INFO [supervisor.audit] {text}\n")
            sys.stdout.flush()
        except Exception:
            # A failing stdout must not turn a successful execution into a failed one.
            LOG.exception("could not write an audit record to stdout")


class _AuditForwarder:
    """Cap, re-parse, re-frame, stamp and forward one execution's SDK audit stream.

    One instance per execution, which is a bound in its own right: the byte budget and the token
    bucket live here, so a flooding script spends its own execution's budget. A process-global
    budget on the read end would rebuild the suppression primitive one level up, where the
    flooder silences somebody else.

    Every record that leaves here is attributed from the tokens the supervisor holds, framed by
    the supervisor, and made of bytes that matched one of the shapes above — so a child cannot
    name another user, break the framing, or put text outside those shapes on the operator's
    stream. It can still lose its own records by flooding its own pipe; no read-end control can
    separate the flood from the records when they share one channel. What is guaranteed is that
    every drop THIS CLASS makes announces itself the first time its cap fires and is counted in
    the summary close() always emits, so a supervisor-side drop is distinguishable from an
    execution that produced no records.

    It is not distinguishable from child-side suppression, and no read-end control can make it
    so. A script that disables the SDK's logger, drops its level, installs a filter, removes the
    handler, or rewrites GENETICS_SDK_AUDIT_FD before its first call writes nothing here, and
    its summary is byte-identical to that of a script that made no SDK calls. Both are honest
    statements about what this fd carried; neither is a statement about what the script did.

    Also not promised: that the records describe what the script actually did. A script can
    write well-formed records by hand, and `client._executor.<method>()` reads data with no
    record at all. These lines bound who a record is attributed to and what shape it can take,
    not whether it happened.
    """

    def __init__(self, user, session, execution, emit=None, clock=time.monotonic):
        self.user = _audit_identifier(user)
        self.session = _audit_identifier(session)
        self.execution = _audit_identifier(execution)
        self._emit = emit if emit is not None else _audit_emit
        self._clock = clock
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._skipping = False
        self._over_budget_open = False
        self._tokens = float(AUDIT_RATE_BURST)
        self._refilled = clock()
        self._announced = set()
        self._closed = False
        self.bytes_seen = 0
        self.forwarded = 0
        self.dropped_rate = 0
        self.dropped_oversize = 0
        self.dropped_unparseable = 0
        self.dropped_over_budget = 0

    def feed(self, block):
        """Consume one block off the fd. Never blocks and never raises at the caller."""
        with self._lock:
            if self._closed:
                return
            before = self.bytes_seen
            self.bytes_seen += len(block)
            room = max(0, AUDIT_STREAM_MAX_BYTES - before)
            cut = room < len(block)
            if cut:
                # Past the budget the reader keeps reading and discards — the status pipe's
                # behaviour, not the output pipe's. Stopping would block the child's next audit
                # write inside a successful data call.
                #
                # Discarded records are counted by their newlines, plus the unterminated one at
                # the end of the discarded stream (tracked by _over_budget_open and added in
                # close()). Counting newlines alone reported 0 for a flood that contained none.
                tail = block[room:]
                self.dropped_over_budget += tail.count(b"\n")
                self._over_budget_open = not tail.endswith(b"\n")
                self._announce("bytes")
                block = block[:room]
            self._buf += block
            while True:
                nl = self._buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(self._buf[:nl])
                del self._buf[: nl + 1]
                if self._skipping:
                    # The tail of a line already dropped as oversize. Without a newline there
                    # was never a second record to count, but an unterminated oversize write
                    # does swallow what follows it, so the oversize notice is the only signal.
                    self._skipping = False
                    continue
                self._line(line)
            if cut:
                # The budget can fall mid-record, leaving a fragment whose length the child
                # chose. Forwarding it would put a prefix that parses as a DIFFERENT record
                # under the real user's stamp — `rows: 999999999` shears to `rows: 9` — and it
                # would be counted as forwarded. The fragment is dropped, and it was already
                # counted above by the newline that terminated it in the discarded part.
                self._buf.clear()
                self._skipping = False
            elif len(self._buf) > AUDIT_LINE_MAX_BYTES:
                # The cap is on the supervisor's buffer as much as on the record: a child
                # writing a megabyte-long line with no newline must not be able to make the
                # supervisor hold it. Counted once per line, not once per block.
                if not self._skipping:
                    self.dropped_oversize += 1
                    self._announce("line")
                self._buf.clear()
                self._skipping = True

    def close(self):
        """Flush the last unterminated line and emit the per-execution summary, once."""
        with self._lock:
            if self._closed:
                return
            if self._buf and not self._skipping:
                # EOF terminates a record as well as a newline does. Only a buffer never cut by
                # the byte budget reaches here.
                self._line(bytes(self._buf))
            self._buf.clear()
            if self._over_budget_open:
                # the discarded stream ended mid-record; that record was lost too
                self.dropped_over_budget += 1
            self._closed = True
            self._emit(self._stamp(
                "SDK audit stream: records=%d dropped_rate=%d dropped_oversize=%d "
                "dropped_unparseable=%d dropped_over_budget=%d bytes=%d"
                % (self.forwarded, self.dropped_rate, self.dropped_oversize,
                   self.dropped_unparseable, self.dropped_over_budget, self.bytes_seen)
            ))

    def _line(self, raw):
        if len(raw) > AUDIT_LINE_MAX_BYTES:
            self.dropped_oversize += 1
            self._announce("line")
            return
        # Parse before spending a token: nothing that is dropped spends one. The bucket bounds
        # how many records reach an operator, so a line that can never reach one must not empty
        # it — 200 lines of pad produced dropped_rate=63 with records=0, junk starving the
        # genuine records the cap was sized for.
        body = _audit_record_body(raw.decode("utf-8", "replace"))
        if body is None:
            self.dropped_unparseable += 1
            self._announce("unparseable")
            return
        if not self._take_token():
            self.dropped_rate += 1
            self._announce("rate")
            return
        self.forwarded += 1
        self._emit(self._stamp(body))

    def _take_token(self):
        now = self._clock()
        self._tokens = min(
            float(AUDIT_RATE_BURST), self._tokens + (now - self._refilled) * AUDIT_RATE_PER_S
        )
        self._refilled = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        return True

    def _stamp(self, body):
        return f"[user={self.user}] [session={self.session}] [execution={self.execution}] {body}"

    def _announce(self, kind):
        if kind in self._announced:
            return
        self._announced.add(kind)
        self._emit(self._stamp(_AUDIT_NOTICES[kind]))


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------


class _HeaderTooLarge(Exception):
    """The request line and headers did not terminate within MAX_HEADER_BYTES -> 431."""


class _HeadReadTimeout(Exception):
    """The head began arriving but did not terminate within HEAD_READ_TIMEOUT_S -> 408.

    DELIBERATELY NOT a TimeoutError subclass. BaseHTTPRequestHandler.handle_one_request catches
    socket.timeout itself and returns having sent NOTHING, so a timeout that reached it would
    drop the connection silently instead of answering in the uniform JSON shape.
    """


# All four shapes http.client.parse_headers stops on, not three: it ends the head at a blank
# line that is "\r\n" or "\n", following a line that ended "\r\n" or "\n" — and "\r\n\n"
# contains "\n\n", so the set below covers the fourth by containment. Dropping "\n\r\n" is not
# cosmetic: the terminator would never be found, `take` would become the whole peek, and the
# body would be copied onto the heap in `parts`, where the finally's wipe cannot reach it.
# Adding it cannot change well-formed behaviour, since the loop takes the smallest end anyway.
_HEADER_TERMINATORS = (b"\r\n\r\n", b"\n\r\n", b"\n\n")
_HEADER_PEEK_ZEROS = bytes(HEADER_PEEK_BYTES)
# A terminator split across two peeks has at most len - 1 bytes on either side of the seam, so
# the boundary window is twice that, derived from the set rather than written down again.
_HEADER_TAIL_BYTES = max(len(term) for term in _HEADER_TERMINATORS) - 1
_HEADER_EDGE_BYTES = 2 * _HEADER_TAIL_BYTES
_HEADER_EDGE_ZEROS = bytes(_HEADER_EDGE_BYTES)


class _HeaderBoundedReader:
    """`rfile` for _Handler: reads the request head WITHOUT pulling the body into this process.

    This is what keeps a refused request's body out of the address space the fork server
    snapshots. socketserver's default rfile is an 8 KiB BufferedReader, so the request-line and
    header parse recv()s 8 KiB — which swallows any body sharing the segment with its headers,
    i.e. every normal client's body under ~8 KiB. Those raw bytes were measured recoverable from
    a child forked promptly after _execute's 503, with _read_body and parse_execute_request
    never having run. Refusing earlier cannot help: the bytes arrive underneath the handler.

    How: peek the socket (MSG_PEEK leaves the queue intact), find the blank line, then consume
    exactly the head. The body stays in the kernel receive queue until _read_body asks for it.

    The bound, stated honestly — this does not make the number zero. The peek copies up to
    HEADER_PEEK_BYTES at a time into one fixed bytearray, so up to HEADER_PEEK_BYTES - 1 body
    bytes can be in it transiently, plus a few more in the fixed seam buffer. Both are zeroed in
    place in a finally before the read returns, and reused rather than reallocated, so nothing
    is left in a freed arena; only a fork landing inside that microsecond window could see them.
    And the request line and headers ARE materialised, necessarily — the contract carries tokens
    and code in the BODY, so a caller that puts a secret in a header gets no protection here.
    """

    def __init__(self, sock, raw):
        self._sock = sock
        self._raw = raw          # socketserver's makefile object; kept only so close() works
        self._scratch = bytearray(HEADER_PEEK_BYTES)
        self._view = memoryview(self._scratch)
        # The seam window for a terminator split across two peeks. Fixed and zeroed for the
        # same reason as _scratch: its trailing bytes can be body bytes, and a `bytes` built
        # there would be freed into an arena the fork snapshots.
        self._edge = bytearray(_HEADER_EDGE_BYTES)
        self._head = None        # BytesIO over head bytes ONLY; never holds a body byte

    def _roll_tail(self, tail_len, take):
        """Extend the seam window by the `take` bytes just consumed, keeping the LAST
        _HEADER_TAIL_BYTES of the whole consumed stream. Returns the new window length.

        It rolls across rounds rather than being recomputed from one. Recomputing meant a peek
        returning fewer than N bytes — a peer dripping the head a byte at a time — discarded
        what earlier rounds had seen, so a terminator straddling the seam was never found,
        `take` stayed the whole peek every round, and the WHOLE BODY was consumed off the kernel
        queue into `parts`. `total` never approaches MAX_HEADER_BYTES on that path either, so
        _HeaderTooLarge never fired: fail-open and pre-auth. It is now bounded at
        HEAD_READ_TIMEOUT_S, which caps how long the copy is held without stopping it.
        """
        edge, view = self._edge, self._view
        if take >= _HEADER_TAIL_BYTES:
            edge[:_HEADER_TAIL_BYTES] = view[take - _HEADER_TAIL_BYTES:take]
            return _HEADER_TAIL_BYTES
        keep = min(tail_len, _HEADER_TAIL_BYTES - take)
        drop = tail_len - keep
        for i in range(keep):
            # A byte at a time rather than a slice assignment, whose right side would
            # allocate: nothing here may put these bytes anywhere but the two fixed buffers
            # _read_head's finally wipes. At most 2 iterations.
            edge[i] = edge[drop + i]
        edge[keep:keep + take] = view[:take]
        return keep + take

    def _arm(self, deadline):
        """Bound the next head recv. One deadline for the whole head rather than a per-recv
        timer: a peer dripping a byte at a time would reset a per-recv timer forever.

        `deadline` is None until the first byte of a head arrives, and that distinction is the
        point: a kept-alive connection waiting for its next request has sent nothing and gets
        the long idle bound, while a head that has started gets HEAD_READ_TIMEOUT_S.
        """
        if deadline is None:
            self._sock.settimeout(IDLE_READ_TIMEOUT_S)
            return
        budget = deadline - time.monotonic()
        if budget <= 0:
            # settimeout(0) is non-blocking mode, not "expired", so never arm it.
            raise _HeadReadTimeout()
        self._sock.settimeout(budget)

    def _read_head(self):
        """The request line and headers, consumed exactly. b"" if the peer closed first.

        The socket timeout is armed and disarmed entirely inside this function, on every path
        including both raises. It is not armed in _Handler.setup(), because _read_body's finally
        does settimeout(None) and a head timeout armed once per connection would then be gone
        for every later request on a kept-alive connection — a fix that works once and silently
        stops working.
        """
        parts = []
        total = 0
        tail_len = 0
        view = self._view
        edge = self._edge
        deadline = None
        try:
            while True:
                # Blocks until at least one byte is queued, exactly as a buffered readline does.
                self._arm(deadline)
                try:
                    peeked = self._sock.recv_into(view, HEADER_PEEK_BYTES, socket.MSG_PEEK)
                except (socket.timeout, TimeoutError):
                    if deadline is None:
                        # Idle keep-alive: nothing of a request has arrived, so there is
                        # nothing to answer. Report EOF and let the caller close, as for a peer
                        # that went away — a 408 written into a connection the client believes
                        # is idle is most likely to be read as the answer to its NEXT request.
                        return b""
                    raise _HeadReadTimeout()
                if peeked == 0:
                    return b""  # clean EOF, or a half-open peer: the caller closes
                if deadline is None:
                    deadline = time.monotonic() + HEAD_READ_TIMEOUT_S
                # Searched in place, on the bytearray. `bytes(view[:peeked])` would be correct
                # and would defeat the whole point: it copies the over-read onto the heap, where
                # it outlives the wipe below in a freed arena. Measured — with that copy present
                # the probe recovered the token and the source from the child even though the
                # scratch itself was being zeroed.
                end = -1
                for term in _HEADER_TERMINATORS:
                    found = self._scratch.find(term, 0, peeked)
                    if found != -1 and (end == -1 or found + len(term) < end):
                        end = found + len(term)
                if tail_len:
                    # A terminator split across two peeks, searched in the second fixed buffer
                    # for the reason the main search is done in place. The leading bytes are
                    # head by construction; the trailing ones are the front of the new peek, and
                    # when the head ends 1 or 2 bytes into it those are BODY bytes.
                    lead = min(_HEADER_TAIL_BYTES, peeked)
                    edge[tail_len:tail_len + lead] = view[:lead]
                    span = tail_len + lead
                    for term in _HEADER_TERMINATORS:
                        found = edge.find(term, 0, span)
                        if found != -1:
                            stop = found + len(term) - tail_len
                            if stop > 0 and (end == -1 or stop < end):
                                end = stop
                # Nothing before the terminator can be a body byte, so until it shows up the
                # whole peek is consumable head; the next peek then blocks on new data.
                take = peeked if end < 0 else end
                got = 0
                while got < take:
                    self._arm(deadline)
                    try:
                        read = self._sock.recv_into(view[got:take], take - got)
                    except (socket.timeout, TimeoutError):
                        raise _HeadReadTimeout()
                    if read == 0:
                        return b""  # truncated head: refuse by closing, never guess
                    got += read
                parts.append(bytes(view[:take]))
                total += take
                if end >= 0:
                    return b"".join(parts)
                tail_len = self._roll_tail(tail_len, take)
                if total + HEADER_PEEK_BYTES > MAX_HEADER_BYTES:
                    raise _HeaderTooLarge()
        finally:
            # In place, on both fixed buffers, on every path including the raise: what the peek
            # over-read past the head dies here rather than in an arena.
            self._scratch[:] = _HEADER_PEEK_ZEROS
            self._edge[:] = _HEADER_EDGE_ZEROS
            # Back to blocking: the response write and _read_body's own arming assume it.
            try:
                self._sock.settimeout(None)
            except OSError:
                pass  # the connection is already gone; the caller is about to close it anyway

    # -- the file-like surface BaseHTTPRequestHandler and _read_body use --------------------

    def readline(self, limit=-1):
        # Only ever called for the request line and the header lines: the head buffer runs out
        # exactly at the blank line, so the next call starts a new head read.
        if self._head is not None:
            line = self._head.readline(limit)
            if line:
                return line
        self._head = io.BytesIO(self._read_head())
        return self._head.readline(limit)

    def read(self, size=-1):
        if self._head is not None:
            data = self._head.read(size)
            if data:
                return data  # unreachable for a well-formed request; correctness, not a path
        if size is None or size < 0:
            chunks = []
            while True:
                block = self._sock.recv(65536)
                if not block:
                    return b"".join(chunks)
                chunks.append(block)
        # Short reads are legal here and _read_body loops on them; a BufferedReader would block
        # for the full `size` instead.
        return self._sock.recv(size)

    def readable(self):
        return True

    def close(self):
        if self._raw is not None:
            self._raw.close()


# BaseHTTPRequestHandler._control_char_table is private, so nothing promises it stays. Every
# interpreter this ships on has it, so the branch below is dead code there; it is kept so an
# interpreter that drops it does not make the log path raise AttributeError. The rebuilt table
# is verified dict-equal to the stdlib's.
_CONTROL_CHAR_TABLE = getattr(http.server.BaseHTTPRequestHandler, "_control_char_table", None)
if _CONTROL_CHAR_TABLE is None:
    _CONTROL_CHAR_TABLE = str.maketrans(
        {c: "\\x{:02x}".format(c) for c in list(range(0x20)) + list(range(0x7f, 0xa0))}
    )
    _CONTROL_CHAR_TABLE[ord("\\")] = "\\\\"


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "genetics-sandbox-supervisor"
    sys_version = ""  # do not disclose the interpreter version to a caller
    rbufsize = 0      # no BufferedReader: setup() installs _HeaderBoundedReader as rfile

    def setup(self):
        super().setup()
        # rbufsize = 0 means super() made a raw SocketIO, which is never read from; it is
        # handed over only so close() still does socketserver's bookkeeping.
        self.rfile = _HeaderBoundedReader(self.connection, self.rfile)

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except _HeaderTooLarge:
            # Fail closed: the head never terminated, so nothing was routed and no body was
            # read. Answer and close rather than fall through to a read of anything.
            self._refuse_head(431, "request headers too large")
        except _HeadReadTimeout:
            # Same standing as the 431: the head stalled, so nothing was routed.
            self._refuse_head(408, "request head not received in time")

    def _refuse_head(self, code, message):
        self.close_connection = True
        # The request line is not trustworthy on either path — it may be absent, or half of it
        # may still be in the socket — so it is cleared before send_response logs it.
        self.requestline = ""
        self.request_version = ""
        self.command = ""
        try:
            self.send_error(code, message)
        except OSError:
            pass

    # -- plumbing ----------------------------------------------------------------------

    def log_message(self, fmt, *args):
        # translate() is the stdlib's. The request line reaches this call raw, and this stream
        # is the audit channel — the one an operator reads to answer "who ran what" — so without
        # it a malformed request line puts ANSI escapes and bare CRs into that channel.
        LOG.info("%s %s", self.address_string(), (fmt % args).translate(_CONTROL_CHAR_TABLE))

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler's default is an HTML page; every non-2xx here is the
        # contract's uniform JSON object instead, including 501 and the request-line failures.
        self._send_json(
            code,
            {"execution_id": None, "error": {"type": _default_error_type(code), "message": message or ""}},
        )

    def _send_json(self, code, payload, extra_headers=()):
        # Every response goes out through here, so this is where the outgoing bound belongs.
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
            # The one route exempt from the uniform error shape: the probe reads only the
            # status code, and a client polling for recovery wants busy/queued in the 503 too.
            self._send_json(code, payload)
        elif path == "/artifact":
            self._artifact()
        elif path == "/execute":
            self._send_request_error(RequestError(405, "MethodNotAllowed", "use POST"))
        else:
            self._send_request_error(RequestError(404, "NotFound", "no such route"))

    def do_POST(self):
        path = self._route()
        if path in ("/health", "/artifact"):
            self._send_request_error(RequestError(405, "MethodNotAllowed", "use GET"))
            return
        if path != "/execute":
            self._send_request_error(RequestError(404, "NotFound", "no such route"))
            return
        self._execute()

    def _method_not_allowed(self):
        path = self._route()
        if path in ("/health", "/execute", "/artifact"):
            self._send_request_error(RequestError(405, "MethodNotAllowed", "unsupported method"))
        else:
            self._send_request_error(RequestError(404, "NotFound", "no such route"))

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _method_not_allowed

    # -- GET /artifact ------------------------------------------------------------------

    def _artifact(self):
        """One artifact of a retained execution, base64 in the uniform JSON envelope.

        Base64 rather than raw bytes with their own content type, so this route answers in the
        same shape as every other and _send_json's outgoing cap stays the single choke point.
        The 33% is affordable at ARTIFACT_READ_MAX_BYTES.
        """
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        execution_id = (params.get("execution_id") or [""])[0]
        name = (params.get("name") or [""])[0]
        try:
            data, content_type = SUPERVISOR.read_artifact(execution_id, name)
        except RequestError as exc:
            self._send_request_error(exc, execution_id=execution_id or None)
            return
        except OSError as exc:
            LOG.error("reading artifact %r of %s failed: %s", name, execution_id, exc)
            self._send_request_error(
                RequestError(500, "InternalError", "artifact could not be read"),
                execution_id=execution_id,
            )
            return
        self._send_json(
            200,
            {
                "execution_id": execution_id,
                "name": name,
                "content_type": content_type,
                "size": len(data),
                "content_base64": base64.b64encode(data).decode("ascii"),
            },
        )

    # -- POST /execute ------------------------------------------------------------------

    def _read_body(self, started):
        ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            self.close_connection = True
            raise RequestError(415, "UnsupportedMediaType", "expected application/json")

        encoding = (self.headers.get("Transfer-Encoding") or "").strip().lower()
        if encoding and encoding != "identity":
            # Refusing is the safe reading: a chunked body cannot be size-capped before it is
            # read, which is the one thing the 1 MiB cap exists to do.
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
        # The count is taken before any work and given back only once the answer has been
        # written to the socket, so a SIGTERM cannot let the shutdown path decide the process is
        # finished in the window between run()'s finally releasing the execution slot and
        # _send_json reaching the wire. A client that reads a reset there is told the execution
        # failed, retryably, for an execution that COMPLETED and whose artifacts are retained.
        #
        # A finally rather than a decrement per exit: the body below returns normally from three
        # except clauses and from the fall-through after the 200, and it also lets exceptions
        # escape, since _send_json calls send_response() and end_headers() outside its own
        # `except OSError`. A count that leaked on one exit would turn a truncated response into
        # a drain that never reaches zero, and the kubelet then SIGKILLs. The other route to the
        # same outcome — a write that never returns, with no leak at all — is closed by
        # DRAIN_DEADLINE_S; it takes both.
        SUPERVISOR.begin_response()
        try:
            self._execute_and_answer()
        finally:
            SUPERVISOR.end_response()

    def _execute_and_answer(self):
        started = time.monotonic()
        execution_id = None
        try:
            if not SUPERVISOR.accepting():
                # Before _read_body, and that order is the fork server's property, not a
                # micro-optimisation: reading the body during bring_up() puts a token and a
                # user's source into the arenas ForkServer.start() is about to snapshot, and no
                # later 503 takes them back out.
                #
                # The connection is closed rather than kept alive because the body has NOT been
                # read: leaving those bytes in the socket makes them the next request line.
                self.close_connection = True
                raise RequestError(503, "NotReady", "supervisor is not accepting executions")
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
        # 414 is unreachable as shipped: the stdlib sends it only when rfile.readline(65537)
        # returns more than 65536 bytes, and _HeaderBoundedReader caps the whole head at
        # MAX_HEADER_BYTES first. It re-arms by itself if that is ever raised above 64 KiB.
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
        # Loud on purpose: read_artifact refuses any artifacts directory that does not resolve
        # under a hardcoded /scratch/ prefix, so an override makes every artifact unretrievable
        # through the real path. It exists for tests, not for deployment.
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
        # Refused, not clamped: a longer value would leave artifacts alive after chat-backend
        # has been told they are gone, and silently ignoring a number is how a knob ends up
        # believed.
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

    Order matters and is contractual: the assertions and prewarm() happen before the first fork
    and before any execution is admitted, and prewarm needs a writable MPLCONFIGDIR to exist
    first because on matplotlib 3.10 an unwritable one raises rather than falling back.

    "Before anything is accepted" is enforced, not assumed. main() is already serving while this
    runs — deliberately, so `status: "starting"` is observable — so requests do arrive here.
    What holds is that _Handler._execute refuses on `not supervisor.accepting()` before it reads
    a byte of the body.
    """
    root = supervisor.scratch_root

    if run_assertions:
        assert_nsswitch_hosts_files_first()

    os.makedirs(root, mode=0o700, exist_ok=True)
    wipe_unrecognised_scratch(root)

    # The supervisor's own writable MPLCONFIGDIR, needed only so prewarm() can import
    # matplotlib.pyplot. Every execution gets its own; this one is shared with no child, which
    # is why the startup wipe keeps it by name.
    sup_dir = os.path.join(root, SUPERVISOR_DIR_NAME)
    mpl_dir = os.path.join(sup_dir, "mplconfig")
    shutil.rmtree(sup_dir, ignore_errors=True)
    os.makedirs(mpl_dir, mode=0o700)
    seed_mplconfig(mpl_dir)
    os.environ["MPLCONFIGDIR"] = mpl_dir

    # A startup gate rather than a per-execution try/except: if AES-256-GCM is not usable in
    # this image, every execution's choice is between retaining plaintext and destroying the
    # artifacts it just produced, and refusing to become ready surfaces that as a
    # CrashLoopBackOff the deploy sees. The probe is sealed in the supervisor's own directory so
    # the file path is exercised too, and before ForkServer.start() so nothing it allocates is
    # in the fork snapshot.
    crypto_selftest(sup_dir)

    module = load_prewarm()
    if module is None:
        LOG.warning(
            "%s is unset: prewarm() SKIPPED. sandbox/Dockerfile always sets it, so this is a "
            "development run outside the image — the analysis modules are not pre-imported "
            "and their absence will surface inside the child instead of crashing the pod.",
            ENV_PREWARM,
        )
    else:
        # PrewarmError is deliberately not caught: a pod that answers health checks and then
        # fails every plotting script is worse than one that crash-loops visibly.
        module.prewarm()
        LOG.info("prewarm complete")

    # The order of these three lines is the whole control.
    #   * After prewarm(), so the fork server inherits the pre-imported analysis modules and
    #     every child still gets them copy-on-write.
    #   * Before `ready`, which is what keeps any Python object holding a token, a request body
    #     or source code out of the address space this snapshots — but only because
    #     _Handler._execute checks accepting() BEFORE _read_body. main() is already serving, so
    #     a POST /execute does arrive during this function. The RAW bytes are kept out by a
    #     second mechanism, _HeaderBoundedReader; see Supervisor.accepting() for the residual.
    #   * Before the reaper thread starts. fork() copies only the calling thread, and forking
    #     while another runs is how a lock ends up held forever in the child. serve_forever is
    #     already running on its own thread here, so this is "before any thread the fork
    #     server's loop depends on" rather than "single-threaded": _forkserver_main touches no
    #     Supervisor object and no lock a serving thread can hold, and the one inherited thing
    #     that would matter — the listening socket — is closed by _close_inherited_fds.
    supervisor.forkserver = ForkServer.start()

    # Started after the wipe so its first pass cannot race the startup clean, and before
    # `ready` so no execution can complete without one running.
    threading.Thread(target=supervisor._reaper_loop, daemon=True, name="retention-reaper").start()

    supervisor.ready = True
    return supervisor


def start(scratch_root=None, run_assertions=True, retention_s=None):
    """create() + bring_up(). The one-call form, used by tests."""
    return bring_up(create(scratch_root, retention_s=retention_s),
                    run_assertions=run_assertions)


def install_orphan_reaper(supervisor):
    """Make PID 1 reap what reparents to it. True if the handler was installed.

    See _reap_orphans for which orphans reach PID 1 at all and which are the sweep's to handle.

    SIGCHLD rather than a polling thread, for two reasons and the second decides it: a handler
    reaps the moment a zombie appears, so a pid slot is never held for a poll interval; and
    CPython delivers signals to the main thread only, which is the thread main() runs
    ForkServer.close() on — that is what lets `_closing` be a plain bool the reaper and close()
    cannot interleave over, instead of a lock a handler could deadlock on.

    The handler is silent, and making it so took more than declining to write a log call here:
    it used to reach LOG.error through note_reaped -> _mark_broken, which against a congested
    stdout raised `RuntimeError: reentrant call inside <_io.BufferedWriter>` inside the handler,
    aborting the delivery with 4 of 5 zombies unreaped — permanent, since SIGCHLD is not queued.
    The reaper path now records its reason without emitting it and ForkServer.alive() flushes it
    from a serving thread. The handler cannot raise either: an exception out of a handler
    surfaces at whatever bytecode the main thread happened to be executing.

    Installed after bring_up(), so the fork server exists and its handle is the one note_reaped
    can publish into. `supervisor` is passed as well, because the second publisher is the
    supervisor's — an execution child stranded by a dead fork server reparents to PID 1 and this
    handler is what would otherwise steal its status.
    """

    def _sigchld(_signum, _frame):
        try:
            _reap_orphans(supervisor.forkserver, supervisor=supervisor)
        except BaseException:  # noqa: BLE001 - a handler in PID 1 must never propagate
            pass

    try:
        signal.signal(signal.SIGCHLD, _sigchld)
    except (ValueError, OSError) as exc:
        LOG.warning(
            "could not install the SIGCHLD orphan reaper (%s): a descendant that reparents past "
            "the fork server to PID 1 will stay a zombie and hold a pid slot for the pod's "
            "lifetime", exc)
        return False
    return True


def main(argv=None):
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [supervisor] %(message)s",
    )
    # Bind and serve before the startup work, so `status: "starting"` is observable rather than
    # theoretical: a probe arriving during prewarm gets the contract's 503 with a health body
    # instead of a connection refusal. Nothing can be executed while not ready.
    supervisor = create()
    httpd = _Server((LISTEN_HOST, LISTEN_PORT), _Handler)

    def _terminate(_signum, _frame):
        # SIGTERM: stop accepting and let the in-flight child finish inside
        # terminationGracePeriodSeconds. Never kill it — its artifacts are promised for the
        # retention window and its response may still be deliverable.
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
        # A failed assertion or a PrewarmError must crash the pod visibly rather than leave it
        # answering health checks and failing every script inside the child.
        LOG.exception("startup failed; refusing to serve")
        httpd.shutdown()
        httpd.server_close()
        if supervisor.forkserver is not None:
            supervisor.forkserver.close()
        return 1
    # After bring_up, so supervisor.forkserver is set and fs.pid has a handle to publish into.
    install_orphan_reaper(supervisor)
    LOG.info("ready")
    serving.join()
    httpd.server_close()
    # After serve_forever returns, so the drain has let the in-flight child finish and be
    # reaped — the fork server is the only process that can reap it.
    supervisor.forkserver.close()
    return 0


def _shutdown_when_idle(httpd, supervisor, poll=0.25, deadline_s=None):
    """Stop serving once nothing is queued, nothing is running AND no answer is still owed — or
    once DRAIN_DEADLINE_S has passed, whichever comes first.

    This polled idle(), which goes true in run()'s finally, before the handler writes the 200. A
    SIGTERM landing in that window let httpd.shutdown() run and the process exit with the
    response truncated or unsent; daemon_threads is True, so server_close() joins nothing. The
    client reads the reset as a retryable failure, so the model is told to run again a script
    that already ran to completion.

    Only POST /execute is counted. /health and /artifact are idempotent GETs with no side
    effect, so a reset on one cannot cause the duplicate-execution harm this exists to prevent,
    and artifacts are lost to termination either way. Not counting them also means a readiness
    probe can never hold the drain open.

    The wait has a ceiling because the thing it waits on has none: _send_json's write is a
    blocking sendall on a connection left at settimeout(None), so a peer that never reads parks
    a counted handler indefinitely — converting a truncated response into a process that is
    SIGKILLed at terminationGracePeriodSeconds, losing forkserver.close() and the child reap as
    well. At DRAIN_DEADLINE_S this proceeds regardless and says so at ERROR.
    """
    limit = DRAIN_DEADLINE_S if deadline_s is None else deadline_s
    deadline = time.monotonic() + limit
    while not supervisor.quiescent():
        if time.monotonic() >= deadline:
            LOG.error(
                "drain deadline reached after %.0fs with %d response(s) still in flight and "
                "%s; shutting down anyway. THOSE ANSWERS ARE ABANDONED — the client sees a "
                "truncated or unsent response. Proceeding is deliberate: waiting past "
                "terminationGracePeriodSeconds would cost the clean fork-server shutdown and "
                "child reap on top of the answer.",
                limit, supervisor.responses_in_flight(),
                "an execution still in flight" if not supervisor.idle() else "the slot free")
            break
        time.sleep(poll)
    httpd.shutdown()


if __name__ == "__main__":
    sys.exit(main())
