#!/usr/bin/env python3
"""The process the sandbox pod runs: HTTP front door, one-at-a-time queue, fork/reap.

Design of record: docs/code-execution-security.md. The wire shape this file implements is
section 2's "The HTTP contract between chat-backend and the supervisor" (4h6.38) and that
subsection is the ONLY definition of it — chat-backend's client (4h6.47) cannot import
anything from here, because the image pip-installs only the genetics SDK's import closure
and prune_venv.py deletes the rest. Every constant below that names a wire value carries a
pointer to the row it comes from; do not change one without changing the other.

This file started as genetics-results-suite-4h6.39 (the skeleton). Its five holes are filled:

  4h6.41  wall clock, pid budget, group kill (parent)  -> _apply_limits, _watchdog, _kill_group
          RLIMIT_AS (child)                            -> _apply_child_limits
  4h6.42  the 8 MiB pipe cap and the 64 KiB head+tail return  -> _drain, _cap_output
  4h6.43  per-execution token delivery by read-once file      -> _deliver_tokens
  4h6.45  the SDK audit stream: capped, re-framed, stamped, forwarded -> _AuditForwarder
  4h6.46  /scratch sub-quotas, artifact retention, the reaper -> _watchdog, _retain, reap_expired
          the budget arithmetic is stated ONCE, above ARTIFACT_QUOTA_BYTES, and is mirrored in
          docs/code-execution-security.md's "4h6.46" table; do not restate it a second time

WHAT 4h6.45 DOES AND DOES NOT CHANGE ABOUT THE AUDIT TRAIL. The records now reach the pod's
own stdout — the only stream the cluster's logging agent collects — attributed from the
TOKENS' sub/sid/jti and framed by this process, with the rate, byte and per-line caps applied
on the read end where the child cannot reach them. That makes the ATTRIBUTION and the FRAMING
trustworthy: a child cannot name another user, cannot break the bracket framing, and cannot
put text outside printable ASCII or the admitted record shapes on an operator's stream. It
does NOT make the records a true account of what the script did — a script can emit
well-formed records for calls it never made, `client._executor.<m>()` reads data with no
record at all (4h6.33), and a child can still lose its own records by flooding its own pipe
(bounded and counted here) or by suppressing them INSIDE its own process, where nothing on
this end can see it happen (4h6.12's four mechanisms all still work; 4h6.55's territory). A
zero-record summary therefore means "this supervisor read no records", not "this script made
no SDK calls". Do not cite these lines as evidence
of what happened under an assumption of compromise; db-api's and results-api's own
`endpoint_access` lines, written outside this pod, are what hold there.

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
* THE PROCESS THAT FORKS IS NOT THIS ONE. genetics-results-suite-4h6.55 option (b): a FORK
  SERVER is forked out of this process at startup, BEFORE the first request is parsed, and
  every execution child is forked from THAT pristine address space. See the "fork server"
  section below for what that buys and what it does not. The rule it exists to enforce is
  one line long: nothing that ever holds a token, a request body or a user's source code
  may call os.fork() to make an execution child.
"""

import array
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
import urllib.parse
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

# -- 4h6.45, THE AUDIT STREAM'S READ-END BOUNDS. Every one of them is applied HERE, by the
# process the child cannot reach into, and none of them is keyed on anything the child writes.
# 4h6.12 measured what the other arrangement costs: a ceiling keyed on SANDBOX_EXECUTION_ID
# was reset by rewriting that variable (19,622 lines/s after rotation, higher than the
# 18,088 lines/s before the ceiling existed), and, being shared with the records that mattered,
# it doubled as a suppression primitive — ~50ms of cheap refusals bought silence for every
# genuine read afterwards.
#
# WHAT THESE BOUNDS DO AND DO NOT BUY. They bound the stream. They do NOT stop a child
# denying attribution of its OWN later calls by flooding its own pipe — nothing on the read
# end can, because the flood and the records share one channel. What they guarantee instead:
# the loss is COUNTED and ANNOUNCED in supervisor framing on the pod's stdout (a notice the
# first time each cap fires, and a summary line at the end of every execution, emitted whether
# or not anything was dropped), and it is charged to the FLOODER ALONE — the pipe, the byte
# budget and the token bucket are per execution, so one execution cannot silence another's
# records. A process-global budget here would reintroduce exactly that, one level up.
CHILD_AUDIT_FD = 4                    # the number the SDK is told to write on; see _child_main

# A record longer than this is DROPPED, never truncated: a truncated line's tail is where the
# `rows:` field lives, so a truncation either produces something that no longer parses (and is
# then dropped anyway, one step later and after buying the memory) or, worse, a prefix that
# parses as a DIFFERENT record than the child wrote. The house rule for identity-bearing text
# is replace-don't-truncate (_sanitise_error_type, the SDK's `<invalid>`); this is the same
# rule applied to a whole record. Sized well above a real record: the SDK's line is a ~120-byte
# prefix plus a function name and an argument summary whose values are individually capped at
# 64 characters.
AUDIT_LINE_MAX_BYTES = 4096

# The per-execution byte budget, counted over EVERYTHING read off the fd including bytes that
# are then dropped. ~5,000 records at a typical ~200 bytes. Past it the reader KEEPS READING
# AND DISCARDS, exactly as the status pipe does and deliberately unlike the output pipe: a
# reader that stopped would block the child's next audit write, and blocking the child inside
# a successful data call would turn an observability bound into an execution failure.
AUDIT_STREAM_MAX_BYTES = 1024 * 1024

# The rate cap, as a token bucket over LINES. A record that reached the executor cost an HTTP
# round-trip, so a genuine sustained rate above this needs ~100 concurrent in-flight SDK calls
# per second for the whole execution; the burst covers an `asyncio.gather` of a few dozen.
# A script that exceeds it loses records — visibly, with a count, which is the difference
# between this and the in-SDK ceiling it replaces.
AUDIT_RATE_PER_S = 100.0
AUDIT_RATE_BURST = 200

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
#
# THE HEADROOM NOW COVERS TWO PROCESSES, NOT ONE: 4h6.55's fork server lives here too. It is
# forked from the supervisor and does nothing but block on a socket, so its pages are shared
# copy-on-write and its incremental RSS is a few MiB rather than a second interpreter's worth —
# which is why this number is unchanged. If it ever starts allocating, this is what it spends.
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

# THE TTL IS A FLOOR, NOT AN INSTANT. Deletion happens on a reaper tick, so a retained
# directory is there until RETENTION_S and is gone by RETENTION_S +
# REAPER_POLL_S; anywhere in that window it may be either. The until-RETENTION_S half is not
# unconditional: _enforce_retained_ceiling evicts oldest-first when a LATER completion pushes
# the retained aggregate over RETAINED_ARTIFACTS_CEILING_BYTES, so a directory can go BEFORE
# its deadline. A presence assertion at t < RETENTION_S is therefore only sound while nothing
# else is retaining concurrently. Tightening the poll would only
# narrow the window, not close it, and polling is what makes the reaper cover orphans the
# registry has no row for — so the window is stated rather than engineered away. A test that
# asserts "gone at RETENTION_S" is flaky by construction; assert presence at any t < RETENTION_S
# and absence only at t >= RETENTION_S + REAPER_POLL_S (plus its own margin), against the
# SANDBOX_RETENTION_S override rather than fifteen real minutes.

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

# The largest artifact GET /artifact will hand back, chosen against MAX_RESPONSE_BYTES rather
# than against what a plot needs: the body is base64 (+33%) inside a JSON envelope, so 512 KiB
# of file is ~700 KiB of body and stays clear of the 1 MiB cap. Reaching that cap instead would
# make _cap_response answer "response too large", which reads as a supervisor fault; a 413 here
# names the actual reason. A matplotlib PNG at the SDK's default dpi is a few tens of KiB, so
# this bounds a pathological artifact, not an ordinary figure.
ARTIFACT_READ_MAX_BYTES = 512 * 1024
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
#
# ERR_MEMORY_LIMIT IS RESERVED AND NEVER EMITTED, which is not an oversight and is why it is
# called out here rather than left to be rediscovered. The memory ceiling is RLIMIT_AS, applied
# by the CHILD to itself (_apply_child_limits) and enforced by the kernel inside the child, so
# the supervisor never observes it as a limit firing: what comes back is the child's own
# exception class, `MemoryError`, with status "error". The supervisor could only re-label that
# by trusting the child to distinguish "the ceiling stopped me" from `raise MemoryError`, and
# the whole point of _sanitise_error_type is that it does not trust the child with a reserved
# name. So the name stays in RESERVED_ERROR_TYPES — where its only job is to keep a script from
# forging it — and the doc no longer claims the supervisor emits it. A client wanting to detect
# memory exhaustion must match the child's `MemoryError`, on the open half of the range.
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

# The name the SDK looks the audit fd's NUMBER up under (4h6.45). Read by
# genetics-mcp-server src/genetics_mcp_server/sdk/client.py `_AUDIT_FD_ENV`, which installs a
# handler on it and switches propagation off. The number has to be in the child's environment
# for the SDK to find it, so the script finds it too and can write whatever it likes there —
# which is why everything that arrives is re-parsed and re-framed rather than believed.
ENV_AUDIT_FD = "GENETICS_SDK_AUDIT_FD"

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

# 4h6.55 option (b). The fork server's control socket carries only these three ops, and none
# of them carries a byte of user data — see _forkserver_main.
FS_OP_FORK = "fork"     # + 4 descriptors; answers {"pid": n}
FS_OP_WAIT = "wait"     # block until pid exits, WITHOUT consuming the zombie
FS_OP_REAP = "reap"     # consume the zombie; answers {"status": n} or {"running": true}

# Control messages are a fixed handful of ASCII bytes. The cap is a framing sanity bound, not
# a budget: anything larger on this socket is a bug or an attempt, and both should fail loudly.
FS_MSG_MAX_BYTES = 4096
# How long a control round trip may take before the supervisor concludes the fork server is
# wedged. FS_OP_WAIT is exempt — it blocks for the child's whole lifetime by design.
FS_CONTROL_TIMEOUT_S = 30.0
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
            # 4h6.43. The PATH, never the tokens themselves: /proc/<pid>/environ is readable
            # by any process with the same uid and supervisor and child share uid 65532, so a
            # helper the script spawns could read a token straight out of a sibling's
            # environment. The path is not a secret. See _deliver_tokens for what this does
            # and — more importantly — what it does not bound.
            ENV_TOKEN_FILE: self.tokens,
            # 4h6.45. The number, not a path: the fd itself is set up by _child_main, which
            # dups the audit pipe's write end onto exactly this number before the script runs.
            ENV_AUDIT_FD: str(CHILD_AUDIT_FD),
            # The SDK's audit prefix reads these three per call (section 6). They are the
            # token's own sub/sid/jti, which the supervisor has already checked against the
            # body. THEY ARE NOT WHAT ATTRIBUTES THE RECORD. The supervisor DISCARDS whatever
            # prefix arrives on the audit fd and re-stamps from job.req.claims, because the
            # child owns its own environment and can rewrite all three between two SDK calls.
            # They are still set, for the shape below and for nothing else: the SDK renders
            # them into the line it writes, the sandbox's own stubs document them, and an
            # in-process (non-sandbox) host has no supervisor to stamp anything.
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


def read_artifact_bytes(artifacts_dir, name, max_bytes=ARTIFACT_READ_MAX_BYTES):
    """(bytes, content_type) for one artifact, or raise RequestError.

    THE CHECKS RUN HERE, INSIDE THE SANDBOX, against the directory the child actually wrote
    to — which is the whole point of serving this over HTTP rather than letting chat-backend
    open a path (docs/code-execution-security.md §6). They are `build_manifest`'s checks in
    the same order and for the same reasons, so nothing the manifest advertised is
    unretrievable and nothing it withheld becomes reachable by asking directly:

      * `_name_is_retrievable` first — a bare name, no separators, no control characters.
      * the directory fd is opened O_NOFOLLOW and the file is opened *relative to it*, so
        neither the artifacts directory nor the file can be a symlink out of /scratch/<id>.
      * regular file with st_nlink == 1 — no FIFO to block the read on, no device, and no
        hard link to something outside the tree.

    Not-found is deliberately indistinguishable across "no such name", "not a regular file"
    and "the open failed": the caller learns only whether the artifact it was told about is
    there, which is all it needs and all a probe should get.
    """
    if not _name_is_retrievable(name):
        raise RequestError(400, "InvalidRequest", "not a retrievable artifact name")
    try:
        dfd = os.open(artifacts_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise RequestError(404, "NotFound", "no such artifact")
    try:
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
        except OSError:
            raise RequestError(404, "NotFound", "no such artifact")
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise RequestError(404, "NotFound", "no such artifact")
            if st.st_size > max_bytes:
                raise RequestError(
                    413,
                    "ArtifactTooLarge",
                    f"artifact is {st.st_size} bytes; the limit is {max_bytes}",
                )
            # Bounded by max_bytes and not by st_size: the size was read before the read, and
            # a setsid() escapee still holding a write handle can grow the file in between
            # (see this module's docstring on what the kill path does not contain).
            chunks = []
            remaining = max_bytes + 1
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
    if len(data) > max_bytes:
        raise RequestError(
            413, "ArtifactTooLarge", f"artifact exceeds the {max_bytes} byte limit"
        )
    return data, mimetypes.guess_type(name)[0] or "application/octet-stream"


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

    THE CHILD READS THIS, THE FORK SERVER NEVER DOES, and that split is the point of the whole
    arrangement (4h6.55 option (b)). The bead's finding 1 named the victim's SOURCE CODE
    alongside their tokens, and a /proc/self/mem scan recovered strings from executions that
    had already completed — so passing the code through the forking process as a Python string
    would leave it in arenas that copy-on-write hands to the NEXT user's child. Passing a
    descriptor instead means the bytes are never in that address space at all.
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
    from an earlier execution to open (finding 3's shape — see 4h6.83 — is not addressed here
    but must not be WIDENED by adding a new named file under /scratch). The fallback creates
    and immediately unlinks a 0600 file in the execution's own directory, which reaches the
    same anonymous-inode end state through a name that exists for microseconds. It exists so
    that a host without memfd_create degrades rather than fails; the image has it.
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

    IT TAKES A DESCRIPTOR, NOT THE CODE. The child is forked by the fork server, which is
    forbidden to hold user data of any kind, so `code`, `env` and `cwd` arrive as JSON on an
    anonymous descriptor that is passed through the fork server without being read. Changing
    this back to arguments puts the source code in the forking process and re-opens 4h6.55.

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

    THE AUDIT FD (4h6.45) IS THE SAME ARRANGEMENT for the same reason. `audit_w` is dup'd onto
    CHILD_AUDIT_FD and named to the SDK by GENETICS_SDK_AUDIT_FD; the script runs with that fd
    open and can write anything it likes there, so the supervisor re-parses and re-frames every
    record on the read end (_AuditForwarder) rather than believing the framing that arrives.
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
        fixed = max(CHILD_STATUS_FD, CHILD_AUDIT_FD)
        status_w = _relocate_above(status_w, fixed)
        audit_w = _relocate_above(audit_w, fixed)
        # The payload descriptor is subject to the same collision as the pipes: the kernel may
        # legitimately have numbered it 3 or 4, in which case the dup2 below would close the
        # execution's own code out from under it.
        payload_fd = _relocate_above(payload_fd, fixed)
        os.dup2(status_w, CHILD_STATUS_FD)
        # 4h6.45. A SECOND fixed number, for the SDK's audit records only. It must be in the
        # keep-set below or it is closed a few lines later and every record the SDK emits
        # raises inside a successful data call.
        os.dup2(audit_w, CHILD_AUDIT_FD)
        os.set_inheritable(CHILD_STATUS_FD, True)
        os.set_inheritable(CHILD_AUDIT_FD, True)
        _close_inherited_fds({CHILD_STATUS_FD, CHILD_AUDIT_FD, payload_fd})

        # AFTER the status fd is wired, so that a malformed or over-cap payload is reported as
        # a StartupFailure the caller can see rather than a silent exit 70.
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


# --------------------------------------------------------------------------------------
# The fork server (genetics-results-suite-4h6.55, option (b))
#
# WHAT IT IS FOR, and it is one property, not a bundle: THE PROCESS THAT CALLS os.fork() TO
# MAKE AN EXECUTION CHILD MUST NEVER HAVE HELD A TOKEN, A REQUEST BODY OR ANOTHER USER'S
# SOURCE CODE. 4h6.55 demonstrated four routes by which a forked child read exactly those out
# of the supervisor's inherited address space — a module global, a frame walk to
# `job.req.tokens`, gc.get_objects(), and a raw scan of /proc/self/mem that recovered a token
# from an execution ALREADY COMPLETED AND RELEASED. The fourth is why nothing reference-shaped
# can fix this: Python strings are immutable and freed objects stay in arenas that
# copy-on-write hands to the child, so `del`, __slots__ and overwriting all fail. The only
# thing that works is never letting the bytes into the process that forks.
#
# So this process is forked out of the supervisor at startup, AFTER prewarm() and BEFORE the
# first byte of the first request body is read — the second half of that is enforced by
# _Handler._execute, which refuses on `not SUPERVISOR.accepting()` before _read_body, because
# the HTTP server is already serving during bring_up(). Its address space is a snapshot of a
# supervisor that has never seen a user. It receives, per execution, exactly one control message —
# `{"op": "fork"}` — plus four descriptors, and it reads none of them. It does not learn the
# execution id, the user, the session, the code or the directory paths. It forks, hands the
# descriptors to _child_main, and reports the pid.
#
# WHY PREWARM SURVIVES, which is the entire reason (b) was chosen over (a) exec-after-fork:
# the fork server inherits the prewarmed numpy/scipy/polars/matplotlib pages from the
# supervisor and passes them to every child copy-on-write, exactly as before. The child still
# never execs. The cost is one extra long-lived process whose pages are shared, not copied.
#
# WHAT IT DOES NOT DO. It does not touch finding 2 (cross-execution artifact read/overwrite on
# the flat /scratch — genetics-results-suite-4h6.82) or finding 3 (a setsid() resident that
# reads the next execution's token file and survives killpg —
# genetics-results-suite-4h6.83). Both remain open and both still block a multi-user deploy.
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

    NOTHING IN THIS LOOP MAY START HOLDING USER DATA. The control message is a fixed op name;
    the payload arrives as a descriptor and is passed straight to the child. If a future change
    needs the fork server to know something about the execution, that is the moment the bead
    this exists for re-opens — put it in the payload instead.

    SIGTERM AND SIGINT ARE IGNORED HERE, deliberately. Kubernetes sends SIGTERM to PID 1, and
    the supervisor's own handler drains rather than exiting: an in-flight child must be allowed
    to finish inside terminationGracePeriodSeconds, and the fork server is the only process
    that can reap it. So the fork server's lifetime is tied to the control socket, not to a
    signal: EOF (the supervisor is gone) is what ends it.

    SIGCHLD IS RESET TO SIG_DFL for the opposite reason. SIG_IGN makes the kernel auto-reap,
    which would race the supervisor's own wait/reap split and lose exit statuses.

    `pending` IS WHY THE CONTROL CHANNEL'S DEATH IS NOT A LEAK. The fork server is the only
    process that knows the pid of a child whose `{"pid": n}` reply never reached the supervisor
    — a fork round trip that times out leaves job.pid None, so neither _execute_inner's finally
    nor the watchdog has anything to kill, and before 4h6.55 that could not happen because the
    supervisor forked the child itself. Exiting on EOF then orphaned a process running user
    code, at the same uid, with write access to /scratch, for the pod's lifetime. So every
    forked pid is held here until a REAP consumes it, and whatever is left when the loop ends
    is killed and reaped by _fs_kill_pending before this process exits.
    """
    pending = set()
    try:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (OSError, ValueError):
                pass
        # The listening socket, every in-flight client connection and anything else the
        # supervisor happened to hold. The fork server needs its control socket and the pod's
        # stdout, and nothing else; a child that inherited the listener could read another
        # user's HTTP conversation (the same reason _close_inherited_fds exists for the child).
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
                    # ECHILD IS NOT "waitid IS UNAVAILABLE" and reporting it as such sent the
                    # supervisor into _reap's WNOHANG polling loop — under job.kill_lock —
                    # whose first FS_OP_REAP then raised from inside that lock. "This pid is
                    # not my child" is a fact the caller has to be told as itself.
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
                        # ECHILD and friends: this pid is not (or no longer) ours, so drop it
                        # before the outer handler answers. Keeping it would have the cleanup
                        # below signal a number that may since have been recycled.
                        pending.discard(msg.get("pid"))
                        raise
                    if got:
                        pending.discard(msg["pid"])
                    _fs_send(sock, {"running": True} if got == 0 else {"status": status})
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
    # BEFORE the reply, not after: the send is the step that can fail, and a child whose pid
    # was never recorded here is a child nobody in the pod can name.
    pending.add(pid)
    _fs_close_all(fds)
    _fs_send(sock, {"pid": pid})


def _fs_kill_pending(pending):
    """Kill and reap every child the supervisor can no longer ask about. Bounded.

    ONLY EVER CALLED AFTER THE CONTROL LOOP HAS ENDED, which is what keeps it clear of the
    supervisor's own reap path: once the socket is at EOF or broken no further FS_OP_REAP can
    arrive, so nothing here can consume a zombie the supervisor is waiting for. Anything still
    in `pending` at that point is, by construction, a child the supervisor either never learned
    the pid of or can no longer reap.

    The shape is _kill_group's — SIGTERM, then SIGKILL after KILL_GRACE_S — flattened over a
    set: one grace for the whole batch rather than one each, because the pod is going away and
    holding its exit open per child buys nothing. Delivery is os.killpg on the child's OWN
    group for the same reason and with the same guard as _resolve_pgid; see _fs_signal_pending.
    """
    if not pending:
        return
    LOG.error("fork server: the control channel ended with %d unreaped child(ren) %s; "
              "killing their process groups rather than orphaning them",
              len(pending), sorted(pending))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        _fs_signal_pending(pending, sig)
        deadline = time.monotonic() + KILL_GRACE_S
        while pending:
            _fs_reap_pending(pending)
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        if not pending:
            return
    LOG.error("fork server: %s survived SIGKILL; exiting anyway", sorted(pending))


def _fs_signal_pending(pending, sig):
    for pid in sorted(pending):
        pgid = _own_pgid(pid)
        try:
            if pgid is None:
                # No group of its own: either it has not reached _child_main's setsid() yet or
                # it never will. killpg on the group it reports would signal the fork server
                # and the supervisor with it, so signal the child alone — exactly _signal_group.
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

    THE CHILD IS A GRANDCHILD OF THE SUPERVISOR, so the supervisor cannot waitpid() it. The
    wait is split across the socket in exactly the two steps _reap already used for its own
    children — a blocking waitid(WNOWAIT) that does not consume the zombie, then a waitpid
    under job.kill_lock that does — because that split is what keeps a pid from being recycled
    between the watchdog deciding to kill and the killpg landing. Collapsing it into one round
    trip re-opens that race across a process boundary, where it is harder to see.

    SIGNALLING IS UNCHANGED AND DOES NOT GO THROUGH HERE: supervisor and child share uid
    65532, so os.killpg from the supervisor reaches the child's group directly.
    """

    def __init__(self, pid, sock):
        self.pid = pid
        self._sock = sock
        # The control socket is a single stream shared by fork/wait/reap. Concurrency is 1, so
        # this is uncontended in practice; it is here so that a future second caller blocks
        # rather than interleaving two round trips on one socket.
        self._lock = threading.Lock()
        # Why a socket that has failed once is never used again: see _poison.
        self._broken = None

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

    def _mark_broken(self, reason):
        """Record, once, that this handle is finished. Safe from any thread: `_broken` is a
        plain attribute so that alive() can read and set it WITHOUT self._lock, which
        wait_nowait holds for the entire lifetime of an execution (timeout=None). Taking that
        lock in alive() made every /health during an execution block until the child exited."""
        if self._broken is None:
            self._broken = reason
            LOG.error("fork server control socket is unusable and will not be reused: %s", reason)

    def _poison(self, reason):
        """Caller holds the lock. Mark the control socket unusable and close it.

        A ROUND TRIP THAT FAILED HALFWAY LEAVES THE PEER'S REPLY QUEUED, and there is no way to
        tell later how many replies are outstanding. Reusing the socket then reads the PREVIOUS
        request's answer: MEASURED — after an FS_OP_WAIT timed out at 0.5s, the next FS_OP_REAP
        returned that WAIT's `{"ok": true}`. The dangerous ordering is a fork whose reply is
        lost: the fork server DID fork the child and DID send its pid, so the next execution
        reads that stale pid, applies its limits to, watchdogs, killpgs and reaps ANOTHER
        USER'S CHILD, while its own child runs with no wall clock and no reaper. Message
        alignment cannot be re-established, so it is never attempted: every later call fails
        immediately, /health goes non-ok (see Supervisor.health) and Kubernetes replaces the
        pod. Restarting the fork server here would be worse than doing nothing — it would be
        forked from a supervisor that has served requests, which is finding 1 exactly.
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
        # An `error` reply is IN BAND: the message was received and answered, so the socket is
        # still aligned and is not poisoned. Only a failed round trip loses alignment.
        if "error" in reply:
            raise ForkServerError(str(reply["error"]))
        return reply

    def alive(self):
        """False once the control socket is poisoned or the fork server process is gone.

        /health asks this. A dead fork server used to be invisible: every /execute answered 500
        forever while /health answered 200 ok, and k8s/deployments/sandbox.yaml has only a
        readinessProbe, so nothing took the pod out of the Service endpoints or replaced it.
        The fork server is a plausible cgroup-OOM victim — it shares the supervisor's pages, so
        its RSS reads high, and it keeps the inherited oom_score_adj of 0 while only the child
        is raised.

        DELIBERATELY LOCK-FREE. self._lock is held for the whole of an execution by
        wait_nowait's timeout=None round trip, so taking it here would make /health block until
        the child exited — a readiness probe that stalls for the length of every execution is
        worse than the failure it was added to detect. It touches only the flag and the pid,
        never the socket, so it cannot close an fd another thread is blocked on.
        """
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

    def close(self, grace=2.0):
        """Close the control socket and reap the fork server. Idempotent."""
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
        # It was blocked in FS_OP_WAIT on a child that outlived the supervisor, or wedged.
        # Either way the pod is going away; do not hold the shutdown open for it.
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except OSError:
            pass


class _SelfWaiter:
    """The waiter for a child of THIS process. Not used in production — see _reap."""

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


def _drain(fd, limit, reaped=None, grace=DRAIN_GRACE_S, poll=0.2, on_limit=None, sink=None):
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

    `sink` (4h6.45, the AUDIT pipe) hands every block STRAIGHT to a consumer and buffers
    nothing, so `limit` does not apply and the returned bytes are empty. It is used where the
    consumer owns its own bounds and needs them applied AS THE BYTES ARRIVE — a rate cap
    cannot be enforced on a buffer handed over at EOF — and where holding the stream in the
    supervisor's memory to re-emit it later would be the flooding primitive the caps exist to
    remove. The deadline, the EINTR handling and the abandon path are shared with the other
    two pipes deliberately: the audit write end is inherited by an escaped descendant exactly
    as the output pipe's is, so EOF is not something waiting longer can produce. If the
    consumer ever raises, this keeps reading and DISCARDS: it cannot fall back to buffering
    (there is no `limit` on this pipe to buffer against) and it must not stop reading, or the
    child blocks in `os.write` on a full pipe inside a call that was succeeding.

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
        if sink is not None:
            try:
                sink(block)
            except Exception:
                # KEEP DRAINING AND DISCARDING. Setting `sink = None` here would drop this
                # stream into the buffering branch below, which is reached with `limit=None`
                # on the audit pipe (deliberately: the forwarder does the capping) and raises
                # TypeError on the next block, killing this thread — after which nothing reads
                # the fd, the 64 KiB pipe fills, and a still-running child BLOCKS in os.write
                # inside a successful data call until the wall clock kills it. That is the
                # exact failure the keep-reading-and-discard design exists to prevent, so the
                # recovery must not be worse than no recovery at all.
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
        # minutes of executions after it (MEASURED). Nothing the supervisor knows about writes
        # to a retained directory — the child is reaped and _retain has already trimmed it —
        # and _forget_retained is the only thing that removes bytes, so the cached value cannot
        # drift for any process the kill path actually reaches.
        #
        # IT CAN DRIFT FOR A setsid() ESCAPEE, and the earlier wording here said flatly that it
        # could not. A descendant that left the process group is not signalled by _kill_group
        # (see its comment), keeps its handles and its write access to /scratch/<id>/artifacts,
        # and can grow a retained tree after its size was cached — so the retained total, the
        # ceiling eviction and the watchdog's aggregate check all read low, and the emptyDir
        # sizeLimit is what would notice. Re-measuring here would not fix it either (the write
        # continues after any measurement); what fixes it is the containment boundary 4h6.55
        # owns. The claim this comment is allowed to make is the conditional one.
        self._retention = {}
        self.retention_s = RETENTION_S if retention_s is None else retention_s
        self._stop_reaper = threading.Event()
        # Set by bring_up(), which forks it before the supervisor is ready and therefore before
        # any request has been parsed. None here means "nothing can be executed yet", which is
        # what a Supervisor built directly by a unit test is.
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
            # THE ONE UNRECOVERABLE STATE, and it used to be invisible: with the fork server
            # dead or its control socket poisoned, every /execute answers 500 forever while
            # this answered 200 ok. k8s/deployments/sandbox.yaml has a readinessProbe and
            # deliberately no livenessProbe, so a pod in that state stayed in the Service
            # endpoints and was never replaced. Answering non-ok takes it out of endpoints,
            # which is the only recovery available: the fork server is NOT restarted in
            # process, because one re-forked from a supervisor that has served requests is
            # 4h6.55 finding 1 again (see ForkServer._poison).
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

        WHY IT IS NOT ENOUGH FOR _admit TO CHECK. _admit runs after parse_execute_request, so a
        POST /execute arriving while bring_up() is still in prewarm() had already materialised
        both JWTs and the user's source as Python strings in this process — and ForkServer.start()
        then snapshots that address space. MEASURED: a request refused with 503 during startup
        was still recovered from the child by the /proc/self/mem route, while a needle minted
        after bring_up() was not, so the finding was specific. main() binds and serves before
        bring_up() on purpose (so `status: "starting"` is observable rather than a connection
        refusal), which makes this the enforcement point for the fork server's whole property.
        _admit re-checks under the lock, where the queue decision is actually made.

        WHAT IT DOES NOT CLOSE. The property this enforces is "no Python object holding a token,
        a request body or anybody's source code is constructed before the fork" — _read_body and
        parse_execute_request never run, so the module-global, frame-walk and gc routes stay
        clean. It is NOT "those bytes are never in this address space": _Handler inherits
        rbufsize = -1, so BaseHTTPRequestHandler's request-line/header parse does an 8 KiB
        buffered recv before _execute is entered, and a body sharing that TCP segment with its
        headers (anything under ~8 KiB from a normal client; http.client concatenates them) is
        already raw in this heap. MEASURED: one segment -> token, source and session id recovered
        from a child forked promptly after the 503; separate segments -> nothing. A realistic
        startup window (prewarm ~3.06s, request at +10ms) recovered nothing, but that is arena
        reuse, not exclusion, and nothing enforces it. genetics-results-suite-4h6.87 owns closing
        it: rbufsize = 0 on the handler (closes it at the source, costs unbuffered header reads
        on the hot path and wants measuring), or restating the bar to the enforced property.
        """
        return self.ready and not self.draining

    def read_artifact(self, execution_id, name):
        """(bytes, content_type) for an artifact of a RETAINED execution, or raise.

        RETAINED ONLY, not running. The caller that has an execution_id is the one that
        submitted it and has already been answered, so by the time it can ask, the execution
        is over and `_retain` has trimmed the directory. Serving a running one would hand
        back a file mid-write, and would do it for the only execution whose bytes are still
        moving — a half-written PNG is worse than a 404.

        The id is the authorisation. It is a uuid4 minted per execution by chat-backend and
        never shown to the model (`parse_execute_request` requires it to equal the tokens'
        jti), so it cannot be guessed and cannot be walked; combined with the NetworkPolicy
        that decides who reaches this port at all, that is the same standing /execute has.
        There is no per-session check here — the sid-scoped resolution
        genetics-results-suite-4h6.52 specifies belongs in chat-backend, which is the only
        side that knows which session owns which execution.
        """
        if not isinstance(execution_id, str) or not EXECUTION_ID_RE.fullmatch(execution_id):
            raise RequestError(400, "InvalidRequest", "execution_id must be a lowercase uuid4")
        with self._lock:
            retained = execution_id in self._retained_ids
        if not retained:
            # One shape for "never existed", "still running" and "reaped": which of the three
            # it is would tell a caller holding a guessed id something about the pod's state.
            raise RequestError(404, "NotFound", "no such execution")
        dirs = ExecutionDirs(self.scratch_root, execution_id)
        return read_artifact_bytes(dirs.artifacts, name)

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
        """[(execution_id, bytes)] in completion order — which is oldest-first.

        THE SIZES CAN DOUBLE-COUNT, and it is recorded here so it is not re-found as new. The
        cached number comes from _dir_usage/_dir_bytes over /scratch/<id> and its artifacts
        subtree, and os.scandir follows the path it is handed: a child that replaces its own
        artifacts/ with a symlink to ANOTHER execution's directory gets that directory's bytes
        charged twice, once to each row. The consequence is misaccounting in the conservative
        direction — the total reads HIGH, so the ceiling evicts sooner than it needs to — and
        deletion stays correct, because _forget_retained's shutil.rmtree does not traverse a
        symlink and _remove_entry unlinks one rather than descending it. NOT FIXED HERE: a
        child that can plant it is already outside the boundary 4h6.55 owns, where it can do
        worse than skew a number. The cheap guard, if it is ever wanted, is one lstat —
        refusing to measure <id>/artifacts when os.path.islink says it is a symlink and
        charging the row 0 — which costs a syscall per measurement and buys nothing until
        4h6.55 lands.
        """
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
        if self.forkserver is None:
            # bring_up() starts it before `ready`, and /execute answers 503 NotReady until
            # then, so this is unreachable through the wire contract. It is here so that a
            # Supervisor built directly (unit tests) fails saying what is missing rather than
            # with an AttributeError three frames down.
            raise RequestError(503, "NotReady", "the fork server is not running")
        dirs = job.dirs
        seed_mplconfig(dirs.mplconfig)
        _deliver_tokens(job)

        out_r, out_w = os.pipe()
        st_r, st_w = os.pipe()
        # 4h6.45. Created BEFORE the fork because that is the only way a descriptor reaches a
        # forked child, and read by this process alone.
        audit_r, audit_w = os.pipe()
        # EVERYTHING FROM HERE TO THE FORK IS INSIDE THE try, and the try used to start five
        # lines lower. _payload_fd raises OSError for real reasons — memfd_create ENOMEM, or
        # os.open/os.write ENOSPC/EDQUOT on the fallback against the 512Mi emptyDir — and
        # dirs.child_env can raise too; either one leaked all six pipe descriptors, which is
        # the leak this change set out to remove, moved five lines up.
        payload_fd = None
        try:
            claims = job.req.claims[TOKEN_AUDIENCES[0]]
            env = dirs.child_env(claims)
            # THE STAMP COMES FROM THE CLAIMS, NOT FROM THE BODY AND NOT FROM THE CHILD.
            # parse_execute_request has already refused the request unless both tokens agree on
            # jti/sub/sid and those match execution_id/user/session_id, so either audience's
            # claims will do; taking them from the token is what makes this evidence rather
            # than an echo.
            audit = _AuditForwarder(
                str(claims.get("sub", "")), str(claims.get("sid", "")), str(claims.get("jti", ""))
            )
            # 4h6.55 option (b). THE CODE AND THE ENVIRONMENT GO OUT AS A DESCRIPTOR, not as
            # arguments, and this process does not fork. The fork server receives four numbers
            # and the word "fork"; it never learns the user, the session, the execution id or
            # the code, and it has never held a token. See the fork-server section.
            payload_fd = _payload_fd(
                {"code": job.req.code, "env": env, "cwd": dirs.tmp}, dirs.base)
            started = time.monotonic()
            pid = self.forkserver.fork_child(payload_fd, out_w, st_w, audit_w)
        except BaseException:
            # The fork server owns nothing yet, so every descriptor is still this process's to
            # close. Leaking the write ends here would leave all three drains blocked on a pipe
            # that never reaches EOF.
            _fs_close_all([fd for fd in (payload_fd, out_w, st_w, audit_w, out_r, st_r, audit_r)
                           if fd is not None])
            raise
        os.close(payload_fd)
        os.close(out_w)
        os.close(st_w)
        os.close(audit_w)
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
        # `limit=None`: with a sink, _drain buffers nothing and applies no bound of its own —
        # the byte, rate and per-line caps are the forwarder's, applied as the bytes arrive.
        # Passing AUDIT_STREAM_MAX_BYTES here would read as a second enforcement point that
        # does not exist.
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
            # The child's own lifetime, measured before the drain. Timing the drain instead
            # reports a number no process spent running whenever a descendant escapes.
            duration_ms = int((time.monotonic() - started) * 1000)
        finally:
            # A JOB THAT WAS FORKED BUT NOT REAPED HAS A CHILD NOBODY WILL EVER KILL, and
            # setting job.done first is what made that permanent: _watchdog's first statement is
            # `if job.done.wait(...): return`, so it exits without firing a limit and without
            # killing the group, and neither _execute nor run kills on its error path. _reap
            # raises ForkServerError (a RuntimeError) whenever the fork server dies or its
            # control socket is poisoned mid-execution — so the fork server dying at t+1s of a
            # 120s execution left the user's code running for the pod's lifetime, holding CPU,
            # memory and same-uid write access to /scratch while later users executed.
            # _kill_group goes through os.killpg/os.kill directly, never the control socket, so
            # it still works with a dead fork server.
            with job.kill_lock:
                stranded = job.pid is not None and not job.reaped
            if stranded:
                LOG.error("execution %s: the reap did not complete; killing the child's group",
                          job.req.execution_id)
                _kill_group(job)
            job.done.set()
            reaped.set()
            t_out.join(DRAIN_GRACE_S + 5.0)
            t_st.join(DRAIN_GRACE_S + 5.0)
            t_audit.join(DRAIN_GRACE_S + 5.0)
            # Closing an fd another thread is blocked on is undefined, so a thread that
            # somehow outlived its own deadline costs two leaked descriptors rather than a
            # read against a reused number. _drain always returns within `grace`, so this
            # branch is a backstop and is logged loudly if it ever fires.
            for name, thread, fd in (("stdout", t_out, out_r), ("status", t_st, st_r),
                                     ("audit", t_audit, audit_r)):
                if thread.is_alive():
                    LOG.error("%s drain thread for %s did not stop; leaking its read end",
                              name, job.req.execution_id)
                else:
                    os.close(fd)
            # AFTER the join, so nothing is still feeding it, and in the finally so that an
            # execution which failed anywhere above still accounts for its own audit stream.
            # The summary is emitted unconditionally: "this script made no SDK calls" and
            # "this script's records were dropped" are then different lines on the pod's
            # stdout rather than the same silence.
            audit.close()
        if out_box.get("abandoned") or st_box.get("abandoned") or audit_box.get("abandoned"):
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

# One row per reason _fire_limit is ever called with, EXCEPT ERR_TIMEOUT, which _response
# answers in its own branch (status "timeout", error.limit null) and so never looks up here.
# _response indexes this dict directly, so a new _fire_limit reason added without a row is a
# KeyError there rather than a bad message. ERR_MEMORY_LIMIT has no row on purpose: nothing
# fires it (see the reserved-names block above), and a message for a limit that cannot fire is
# the stub this file has already had removed from it twice.
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
    statement.

    THE FORK SERVER DELIBERATELY DOES NOT setsid(), and this guard is why. It stays in the
    supervisor's process group, so a child that has not yet reached its own setsid() reports
    the supervisor's pgid and is caught here exactly as before. A fork server in a group of its
    own would report a pgid this test does not recognise, and the first killpg would take out
    the fork server and with it every future execution. Callers treat None as "no group to signal or count", never as "the group is
    empty". The caller holds job.kill_lock, so job.pid cannot be reaped and recycled while
    this reads it.
    """
    if job.pid is None:
        return None
    return _own_pgid(job.pid)


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


def _reap(job, waiter=None):
    """Block until the child exits, then reap it under job.kill_lock. Returns wait status.

    `waiter` IS THE PROCESS THAT OWNS THE CHILD. In production that is the ForkServer, because
    4h6.55 moved the fork out of this process and the child is now a GRANDchild here — waitpid
    from the supervisor would raise ECHILD. The default reaps a child of this process and is
    used only by tests that fork their own; the two-step structure below is identical either
    way, which is the point of routing it through an object rather than branching.

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
    waiter = SELF_WAITER if waiter is None else waiter
    if not waiter.wait_nowait(job.pid):
        while True:
            with job.kill_lock:
                wait_status = waiter.reap(job.pid, nohang=True)
                if wait_status is not None:
                    job.reaped = True
                    return wait_status
            time.sleep(0.02)
    with job.kill_lock:
        wait_status = waiter.reap(job.pid)
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

    THE POLICY IS BLIND TO SIZE, and the case it loses badly is one an ordinary user writes:
    a 100 MiB CSV FIRST, then fifty small plots. The CSV is the oldest, so it is the LAST
    candidate — the fifty plots go first, and since no number of plots brings a tree under a
    quota one file alone exceeds, the CSV goes too. Everything is lost, and the plots were lost
    for nothing. A size-aware pass would keep them. This is left as it is deliberately: recency
    is the right signal for the hostile case the trim exists for (the entry being written when
    the kill landed is the culprit), and the alternative deletes the large output a script was
    asked to produce while keeping incidental ones. It is not a budget violation — the tree
    still ends up under ARTIFACT_QUOTA_BYTES, or the give-up path returns a measured size — so
    it is a behaviour, and it is stated in read_artifact's contract (the read_artifact
    subsection of docs/code-execution-security.md) where a user reads what may be missing.

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

    WHAT THIS FILE IS AND IS NOT AN EXPOSURE BOUND AGAINST, restated after 4h6.55 option (b)
    landed, because the earlier wording is now half wrong in a way that would be read
    generously. Three routes were MEASURED against the ORIGINAL shape, in which the process
    holding tokens was also the process that forked:
      * a raw /proc/self/mem scan in the child recovered tokens out of the inherited address
        space — including from an execution that had already completed and been released;
      * so did every reference route (module globals, a frame walk to job.req.tokens,
        gc.get_objects());
      * and a detached setsid() grandchild of an EARLIER execution read THIS execution's
        mode-0600 file from inside the read-once window.
    THE FIRST TWO ARE CLOSED, and by the fork server rather than by anything here: the child is
    forked from a process that has never held a token, a request body or a line of anyone's
    source code, so there is nothing of another user's in the address space it inherits. THE
    THIRD IS NOT CLOSED — it does not depend on the fork at all, only on one shared uid and a
    file with a name — and it is genetics-results-suite-4h6.83. So this function's mode 0600
    still bounds nothing against a same-uid resident, and the read-once unlink still narrows
    only the window, not the reachable set.

    IT IS ALSO THE ROUTE THAT MAKES OPTION (b) POSSIBLE, which is the one thing the earlier
    wording missed. The child needs its credential and the fork server must not carry it; a
    file the SUPERVISOR writes and the CHILD opens is a route from one to the other that does
    not pass through the process in between. That is now this file's load-bearing property.

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
# The SDK audit stream (4h6.45)
#
# The child writes SDK audit records on CHILD_AUDIT_FD; the supervisor holds the read end and
# is the only thing that decides what is recorded. Three sites make that true and all three
# are load-bearing: the fd exists BEFORE the fork (_execute_inner), it is dup'd onto a fixed
# number and kept out of the close sweep (_child_main), and its number is named to the SDK in
# the child's environment (ExecutionDirs.child_env).
#
# WHY THE READ END AND NOT THE SDK. 4h6.12 defeated every in-process control by running it:
# records were forged with `logging.getLogger("genetics_mcp_server.sdk.audit").info(...)` and
# with `os.write` to the fd number the script reads from its own environment, and silenced
# with `logger.disabled`, the level, a filter and handler removal. The audited code and the
# emitter share an address space, so that is a property of where the code runs. Nothing below
# tries to defend the child's side of the fd — it assumes the child owns it completely.
# --------------------------------------------------------------------------------------


# The three fields the supervisor STAMPS. Checked the way _sanitise_error_type checks a
# child-supplied error.type, and for the same reason: a value that breaks the framing produces
# a line an operator's tools read back as something else. These come from the TOKENS, so this
# is not defence against the script — `sub` is whatever the identity provider put in the
# claim, and a `]` in it would close the bracket early while a 100 KB one would put 100 KB on
# the stream per record. REPLACED, never truncated: `<invalid>` keeps the field's position so
# the line still parses and is unmistakably not an identity, where a truncation of
# `admin@finngen.fi.attacker.test` manufactures a different, credible-looking one (4h6.12
# measured exactly that shape on the SDK side).
_AUDIT_IDENTITY_RE = re.compile(r"\A[A-Za-z0-9_.:/@|+-]{1,64}\Z")
_AUDIT_BAD_IDENTITY = "<invalid>"

# WHAT A RECORD IS ALLOWED TO LOOK LIKE. The child's framing is untrusted input, so a line is
# not "cleaned up" — it either matches one of these exactly, from the marker to the end of the
# line, or it is dropped and counted. Anything laxer re-opens the forgery the whole bead
# exists to close: `search()`-based parsers (including this repo's own
# scripts/analyze_conversations.py) match a record ANYWHERE in a line, so a child that appends
# `[user=admin@finngen.fi] [session=s] [execution=e] Executing SDK function: sql with input:
# {} rows: 1` to an otherwise ordinary record would otherwise have written a genuine-looking
# access under someone else's name.
#
# The charsets are bounded by what the SDK can emit AND — where the SDK's own bound turns out
# to be weaker than it looks — by what may go on an operator's stream. Function names are
# Python identifiers and exception types are dotted identifiers. The argument summary is
# `_summarize_arguments`' dict rendering, whose values are a repr of a scalar, a repr of an
# identifier-shaped string (the SDK's `_AUDIT_SAFE_VALUE_RE`) or `<type:N>`/`<type>` — PLUS
# the bare string `<unavailable>`, with no braces at all, which is what that function returns
# when `signature.bind_partial` raises TypeError. That shape is not exotic and is not an
# attack: one extra positional argument or one unknown keyword in an ordinary script produces
# it, so omitting it put a genuine record of a genuine mistake into dropped_unparseable, which
# an operator reads as tampering.
#
# WHY THE CLASS IS TIGHTER THAN THE SDK'S, which is ASCII already. `<type>` renders
# `type(value).__name__`, and a script owns that outright, so the emitting side is not where
# this can be held — the read end is. Printable ASCII minus `[`, `]`, `{`, `}` and a backslash
# costs the SDK nothing, because it cannot emit anything else, and it buys two things: the
# operator's bracket framing stays unforgeable from inside the summary, and a record stays ONE
# line — U+2028, U+2029 and U+0085 each split a line under `str.splitlines()`, which is what
# this repo's own harness and plenty of log tooling read records with.
_AUDIT_FN_RE = r"[A-Za-z_][A-Za-z0-9_]{0,63}"
_AUDIT_ERR_RE = r"[A-Za-z_][A-Za-z0-9_.]{0,63}"
_AUDIT_ARGS_RE = r"(?:\{[^{}\[\]\\\x00-\x1f\x7f-\U0010ffff]{0,1024}\}|<unavailable>)"

# `[0-9]`, never `\d`: Python's `\d` matches every Unicode decimal digit, so `rows: ١٢٣` was
# forwarded and the analyzer's `int()` read it back as 123 — a row count nobody wrote.
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
    # It is the one statement about the channel worth carrying across — scripts/
    # analyze_conversations.py scans for it, and lands the number in
    # `notices["truncated_at"]`. THE NUMBER IS CHILD-SUPPLIED and nothing here can check it:
    # the supervisor does not count the SDK's refusals, so a child that writes this line
    # itself picks the figure (999999999 was measured going through). The literal text around
    # it is what is bounded — the notice cannot become a channel for chosen PROSE, and it
    # carries no `rows:` field so it can never be read as a data access. The cross-check for
    # the number is the supervisor's own per-execution summary, which the child cannot write.
    # The SDK's other meta record, the shared-stream warning, is deliberately NOT admitted: it
    # says the records may be forged because no dedicated fd was configured, which on this
    # path is false and would make the analyzer distrust a stream the supervisor stamped.
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

# Fixed text, one per cap, emitted at most once per execution. NO CHILD-CHOSEN BYTES, for the
# reason the SDK's own meta channel documents: a notice that quotes what it is complaining
# about hands the thing being bounded a way to write into the operator's log. None of them can
# parse as a data access — no `Executing SDK function:` marker and no trailing `rows:` field.
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

    Everything before the marker — the SDK's asctime, its logger name, its level, and the
    `[user=…] [session=…] [execution=…]` prefix it renders from the child's own environment —
    is DISCARDED rather than parsed. The prefix in particular is exactly the field this bead
    exists to stop believing, and re-emitting any of it would put child bytes on the pod's
    stdout under supervisor framing.
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
    """Put one already-framed record on the POD'S OWN STDOUT, and flush it.

    Written to the stream rather than through LOG, for two reasons that are both about
    delivery rather than style. The logging configuration belongs to main(), so a supervisor
    embedded differently — the test harness does exactly this — would discard the whole
    control at the root logger's default level, silently. And stdout is BLOCK-buffered when it
    is a pipe, which is what it is under both `docker logs` and the kubelet, so an unflushed
    record arrives minutes late or, if the process is killed, never.
    """
    # The same shape logging's %(asctime)s renders in main()'s basicConfig, milliseconds and
    # all: one stdout stream carrying two timestamp formats makes a reader parse two.
    now = time.time()
    stamp = "%s,%03d" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                         int((now % 1) * 1000))
    with _AUDIT_EMIT_LOCK:
        try:
            sys.stdout.write(f"{stamp} INFO [supervisor.audit] {text}\n")
            sys.stdout.flush()
        except Exception:
            # A failing stdout must not turn a successful execution into a failed one; the
            # same contract _emit has on the SDK side.
            LOG.exception("could not write an audit record to stdout")


class _AuditForwarder:
    """Cap, re-parse, re-frame, stamp and forward one execution's SDK audit stream.

    ONE INSTANCE PER EXECUTION, and that is a bound in its own right. The byte budget and the
    token bucket live here, so a flooding script spends ITS OWN execution's budget and cannot
    reach the next one's — a process-global budget on the read end would have rebuilt 4h6.12's
    suppression primitive one level up, where the flooder silences somebody else.

    WHAT IS AND IS NOT PROMISED. Every record that leaves here is attributed from the tokens
    the supervisor holds, framed by the supervisor, and made of bytes that matched one of the
    shapes above — so a child cannot name another user, cannot break the framing and cannot
    put text outside those shapes on the operator's stream. It CAN still lose its own records
    by flooding its own pipe: no read-end control can separate the flood from the records when
    they share one channel. What is guaranteed is narrower than "loss is never silent": every
    drop THIS CLASS makes announces itself the first time its cap fires and is counted in the
    per-execution summary close() always emits, so a SUPERVISOR-SIDE drop is distinguishable
    from an execution that produced no records.

    IT IS NOT DISTINGUISHABLE FROM CHILD-SIDE SUPPRESSION, and no read-end control can make it
    so. A script that disables the SDK's logger, drops its level, installs a filter, removes
    the handler (4h6.12 measured all four) or simply rewrites GENETICS_SDK_AUDIT_FD to 1
    before its first SDK call writes nothing to this fd, and the summary it produces is
    BYTE-IDENTICAL to the summary of a script that made no SDK calls: `records=0 dropped_*=0
    bytes=0`. Both are honest statements about what this fd carried. Neither is a statement
    about what the script did — for that, db-api's and results-api's own endpoint_access
    lines, written outside the pod, are what hold. Making in-process suppression observable
    needs the child contained rather than read (4h6.55) and is not attempted here.

    Also not promised, because nothing here can promise it: that the records describe what the
    script actually did. A script that makes no SDK calls at all and writes well-formed
    records by hand produces a clean stream, and `client._executor.<method>()` reads data with
    no record at all (4h6.33). These lines bound WHO a record is attributed to and WHAT SHAPE
    it can take, not whether it happened.
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
                # PAST THE BUDGET THE READER KEEPS READING AND DISCARDS — the status pipe's
                # behaviour, not the output pipe's. Stopping the read would block the child's
                # next audit write, and a child blocked inside a successful data call turns an
                # observability bound into an execution failure.
                #
                # The discarded records are counted by their newlines, PLUS the unterminated
                # one at the end of the discarded stream (tracked across blocks by
                # _over_budget_open and added in close()). Counting newlines alone reported
                # dropped_over_budget=0 for a flood that contained none, leaving only `bytes=`
                # to say anything had been lost at all.
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
                    # The tail of a line already dropped as oversize. If the child never
                    # terminated that line, whatever it wrote next is part of the SAME line
                    # and goes with it: one line in, one drop counted. That is not a lost
                    # record going uncounted — without a newline there was never a second
                    # record to count — but it does mean an unterminated oversize write
                    # swallows what follows it, so the oversize notice is the only signal.
                    self._skipping = False
                    continue
                self._line(line)
            if cut:
                # THE BUDGET CAN FALL MID-RECORD, and what is left in the buffer is then a
                # FRAGMENT the child chose the length of. Forwarding it would put a prefix
                # that parses as a DIFFERENT record than the child wrote under the real user's
                # stamp — `rows: 999999999` shears to `rows: 9`, a trailing ` error: X` or
                # ` cancelled` shears off entirely — and it would be counted as forwarded, so
                # nothing downstream could tell. Replace-don't-truncate, the same rule
                # AUDIT_LINE_MAX_BYTES states: the fragment is dropped, and it was already
                # counted above by the newline that terminated it in the discarded part.
                # Nothing more can be appended either — bytes_seen only grows, so `room` stays
                # 0 for the rest of this execution.
                self._buf.clear()
                self._skipping = False
            elif len(self._buf) > AUDIT_LINE_MAX_BYTES:
                # The cap is on the SUPERVISOR'S buffer as much as on the record: a child
                # writing one megabyte-long line without a newline must not be able to make
                # the supervisor hold it. Counted once for the whole line, not once per block.
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
                # EOF terminates a record as well as a newline does. Only a buffer that was
                # never cut by the byte budget reaches here — feed() clears a truncated one at
                # the cut rather than leaving a fragment for this line to forward.
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
        # PARSE BEFORE SPENDING A TOKEN, and more generally: NOTHING THAT IS DROPPED SPENDS
        # ONE. The bucket bounds how many records reach an operator, so a line that can never
        # reach one must not empty it — 200 lines of pad produced dropped_rate=63 with
        # records=0, junk starving the genuine records the cap was sized for. Parsing first
        # costs a regex match bounded by AUDIT_LINE_MAX_BYTES. The oversize branch above spends
        # no token for the same reason, and the volume of junk is bounded by the byte budget
        # rather than by this bucket.
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

        Base64 rather than the raw bytes with their own content type, so that this route
        answers in the same shape as every other one and `_send_json`'s outgoing cap stays
        the single choke point. The 33% is affordable at ARTIFACT_READ_MAX_BYTES.
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
            if not SUPERVISOR.accepting():
                # BEFORE _read_body, and that order is the fork server's property, not a
                # micro-optimisation: reading the body during bring_up() puts a token and a
                # user's source into the arenas ForkServer.start() is about to snapshot, and no
                # later 503 takes them back out. See Supervisor.accepting.
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

    Order matters and is contractual: the assertions and prewarm() happen BEFORE the first fork
    and before any execution is admitted, and prewarm needs a writable MPLCONFIGDIR to exist
    first because on matplotlib 3.10 an unwritable one raises rather than falling back.

    "BEFORE ANYTHING IS ACCEPTED" IS ENFORCED, NOT ASSUMED. main() is already serving while
    this runs — deliberately, so `status: "starting"` is observable — so requests DO arrive
    here. What holds is that _Handler._execute refuses on `not supervisor.accepting()` before
    it reads a byte of the body, which is the check that keeps a token or a user's source out
    of the pages ForkServer.start() snapshots below.
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

    # 4h6.55 option (b). THE ORDER OF THESE THREE LINES IS THE WHOLE CONTROL.
    #   * AFTER prewarm(), so the fork server inherits the pre-imported analysis modules and
    #     every child still gets them copy-on-write. Forking it earlier would cost exactly what
    #     option (a) costs and buy nothing extra.
    #   * BEFORE `ready`. That is what keeps any Python object holding a token, a request body
    #     or anybody's source code out of the address space this snapshots — but only because
    #     _Handler._execute checks Supervisor.accepting() BEFORE _read_body. main() is already
    #     serving by now, so a POST /execute can and does arrive during this function; when the
    #     readiness check sat after _read_body and parse_execute_request, an early request's
    #     token and source were MEASURED still recoverable from a later child by the
    #     /proc/self/mem route, 503 and all. It does NOT keep the raw bytes out: _Handler
    #     inherits rbufsize = -1, so a body sharing a TCP segment with its headers is in the
    #     socket read buffer before _execute runs, and was MEASURED recoverable from a child
    #     forked promptly after the refusal when the two shared a segment (not when they were
    #     separate). See Supervisor.accepting(); genetics-results-suite-4h6.87 owns that residual.
    #   * BEFORE the reaper thread starts. fork() copies only the calling thread; forking while
    #     another thread runs is how a lock ends up held forever in the child. serve_forever is
    #     ALREADY running on its own thread here (main() binds first, on purpose), so this is
    #     "before any thread that the fork server's own loop depends on", not "single-threaded":
    #     _forkserver_main touches no Supervisor object and no lock a serving thread can hold,
    #     and the one inherited thing that would matter to a child — the listening socket — is
    #     closed by _close_inherited_fds.
    supervisor.forkserver = ForkServer.start()

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
        if supervisor.forkserver is not None:
            supervisor.forkserver.close()
        return 1
    LOG.info("ready")
    serving.join()
    httpd.server_close()
    # After serve_forever returns, so the drain has already let the in-flight child finish and
    # be reaped — the fork server is the only process that can reap it.
    supervisor.forkserver.close()
    return 0


def _shutdown_when_idle(httpd, supervisor, poll=0.25):
    while not supervisor.idle():
        time.sleep(poll)
    httpd.shutdown()


if __name__ == "__main__":
    sys.exit(main())
