import ast
import json
import os
import select
import signal
import socket
import threading
import time
import types

from .harness import ROOT, _LogCapture, check, make_body, skip, sup


ENV_REAPER_UNBOUNDED = "SUPERVISOR_TEST_REAPER_UNBOUNDED"


ENV_REAPER_NO_FS_SLOT = "SUPERVISOR_TEST_REAPER_NO_FS_SLOT"


ENV_REAPER_IGNORES_CLOSING = "SUPERVISOR_TEST_REAPER_IGNORES_CLOSING"


ENV_REAPER_NO_JOB_SLOT = "SUPERVISOR_TEST_REAPER_NO_JOB_SLOT"


ENV_REAPER_LOGS = "SUPERVISOR_TEST_REAPER_LOGS"


ENV_REAPER_SIG_IGN = "SUPERVISOR_TEST_REAPER_SIG_IGN"


ENV_CLOSING_LAST = "SUPERVISOR_TEST_CLOSE_SETS_CLOSING_LAST"


ENV_DRAIN_READY_ONLY = "SUPERVISOR_TEST_DRAIN_DEADLINE_IN_READY"


ENV_PUBLISH_KEEPS_PGID = "SUPERVISOR_TEST_PUBLISH_KEEPS_PGID"


ENV_PUBLISH_NO_REAPED_GUARD = "SUPERVISOR_TEST_PUBLISH_NO_REAPED_GUARD"


def _publish_real(supervisor, pid, status):
    """Supervisor.note_child_reaped itself. The publisher checks route through this so a control
    differs from production in exactly the one line it is named for."""
    return supervisor.note_child_reaped(pid, status)


def _publish_keeps_pgid(supervisor, pid, status):
    """note_child_reaped that clears `pid` but LEAVES `reaped_pgid` stamped. The control for the
    completion-path half of the collision: _reap stamps reaped_pgid BEFORE the waitpid that can
    raise, so a fork server dying in between leaves the pgid set and `reaped` False, the stranded
    branch is not taken, and _execute_inner's else branch calls _kill_survivors unconditionally
    on a pgid whose pid the reaper just made recyclable."""
    job = supervisor._running
    if job is None or job.reaped or pid != job.pid:
        return False
    job.reaped_status = status
    job.reaped = True
    job.pid = None
    return True


def _publish_no_reaped_guard(supervisor, pid, status):
    """note_child_reaped without the `job.reaped` half of its match. The negative control for a
    NORMALLY reaped job: _reap never clears job.pid and _release does not clear _running until
    the whole response is built, so that pid number, once recycled, still matches here."""
    job = supervisor._running
    if job is None or pid != job.pid:
        return False
    job.reaped_pgid = None
    job.reaped_status = status
    job.reaped = True
    job.pid = None
    return True


def _reaper_unbounded(fs=None, max_rounds=None, supervisor=None):
    """_reap_orphans with the round cap removed. The control for the bound.

    max_rounds is accepted and ignored on purpose. In PID 1 this is driven from a signal handler,
    so a peer forking and killing faster than it reaps pins the main thread inside the handler.
    """
    reaped = []
    if fs is not None and getattr(fs, "_closing", False):
        return reaped
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except OSError:
            break
        if pid == 0:
            break
        _publish(fs, supervisor, pid, status)
        reaped.append(pid)
    return reaped


def _reaper_no_fs_slot(fs=None, max_rounds=sup.ORPHAN_REAP_MAX_ROUNDS, supervisor=None):
    """_reap_orphans that reaps fs.pid and DROPS the status. The negative control for the
    collision resolution: this is the shape a blind waitpid(-1) reaper has, and it leaves
    ForkServer.close() polling a pid the kernel is free to have given to somebody else."""
    reaped = []
    for _ in range(max_rounds):
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except OSError:
            break
        if pid == 0:
            break
        if supervisor is not None:
            supervisor.note_child_reaped(pid, status)
        reaped.append(pid)
    return reaped


def _reaper_ignores_closing(fs=None, max_rounds=sup.ORPHAN_REAP_MAX_ROUNDS,
                            supervisor=None):
    """_reap_orphans that reaps while close() owns fs.pid. The negative control for `_closing`."""
    reaped = []
    for _ in range(max_rounds):
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except OSError:
            break
        if pid == 0:
            break
        _publish(fs, supervisor, pid, status)
        reaped.append(pid)
    return reaped


def _reaper_no_job_slot(fs=None, max_rounds=sup.ORPHAN_REAP_MAX_ROUNDS, supervisor=None):
    """_reap_orphans that publishes fs.pid but NOT the running execution's child — the shape
    before the second publisher existed, and the shape the old docstring described as safe on
    the grounds that "execution children are the supervisor's grandchildren". They are, until
    the fork server dies mid-execution and they reparent to PID 1."""
    reaped = []
    if fs is not None and getattr(fs, "_closing", False):
        return reaped
    for _ in range(max_rounds):
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except OSError:
            break
        if pid == 0:
            break
        if fs is not None:
            fs.note_reaped(pid, status)
        reaped.append(pid)
    return reaped


def _reaper_logging_publisher(fs=None, max_rounds=sup.ORPHAN_REAP_MAX_ROUNDS, supervisor=None):
    """_reap_orphans whose fork-server publisher LOGS from inside the handler — note_reaped as
    it stood, reaching LOG.error through _mark_broken's default. The negative control for "the
    handler is silent": against a congested stdout that call raised `reentrant call inside
    <_io.BufferedWriter>` INSIDE the handler and abandoned the rest of the delivery."""
    reaped = []
    if fs is not None and getattr(fs, "_closing", False):
        return reaped
    for _ in range(max_rounds):
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except OSError:
            break
        if pid == 0:
            break
        if fs is not None and pid == fs.pid:
            fs.exit_status = status
            fs.pid = None
            fs._mark_broken("the fork server exited (reaped by the PID 1 orphan reaper)")
        if supervisor is not None:
            supervisor.note_child_reaped(pid, status)
        reaped.append(pid)
    return reaped


def _publish(fs, supervisor, pid, status):
    """Both publishers, as _reap_orphans runs them. Shared by the controls so that each one
    differs from the real function in exactly the one way it is named for."""
    if fs is not None:
        fs.note_reaped(pid, status)
    if supervisor is not None:
        supervisor.note_child_reaped(pid, status)


def _close_closing_last(fs, grace=2.0):
    """ForkServer.close with `_closing` set LAST instead of first. The negative control for the
    ordering close()'s own docstring calls LOAD-BEARING; every other line is copied verbatim, so
    the single difference is where the flag is set."""
    with fs._lock:
        try:
            fs._sock.close()
        except OSError:
            pass
        pid, fs.pid = fs.pid, None
    if pid is None:
        fs._closing = True
        return
    deadline = time.monotonic() + grace
    while True:
        try:
            got, _ = os.waitpid(pid, os.WNOHANG)
        except OSError:
            fs._closing = True
            return
        if got:
            fs._closing = True
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    except OSError:
        pass
    fs._closing = True


def _install_sig_ign(_supervisor):
    """install_orphan_reaper replaced by `signal(SIGCHLD, SIG_IGN)`. The negative control for
    the end-to-end wiring: the kernel then auto-reaps, so a check that only asserts the zombie
    is gone stays green while _reap_orphans, note_reaped, note_child_reaped and `_closing` are
    all unreachable from production."""
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    return True


class _ProbeSock:
    """A stand-in for the control socket whose close() runs a callback. ForkServer touches the
    socket only through _sock.close() on this path, which makes it a probe for the exact instant
    close() first does anything at all."""

    def __init__(self, on_close):
        self._on_close = on_close

    def close(self):
        self._on_close()


def _drain_deadline_in_ready_branch(fd, limit, reaped=None, grace=sup.DRAIN_GRACE_S, poll=0.2,
                                    on_limit=None, sink=None):
    """_drain with the deadline evaluated ONLY when select reports the
    fd went quiet. Reduced to the loop shape that matters — the output cap and the sink-failure
    recovery are untouched by this check and copying them would only invite them to drift — so
    the single difference from the real function is which branch the deadline check sits in."""
    total = 0
    deadline = None
    abandoned = False
    while True:
        if deadline is None and reaped is not None and reaped.is_set():
            deadline = time.monotonic() + grace
        wait = poll if deadline is None else max(0.0, min(poll, deadline - time.monotonic()))
        try:
            ready, _, _ = select.select([fd], [], [], wait)
        except (InterruptedError, OSError):
            break
        if not ready:
            if deadline is not None and time.monotonic() >= deadline:
                abandoned = True
                break
            continue
        try:
            block = os.read(fd, 65536)
        except OSError:
            break
        if not block:
            break
        total += len(block)
        if sink is not None:
            sink(block)
    return b"", total, False, abandoned


def _fork_zombie(code=0):
    """A child of THIS process that exits immediately. Returns its pid once it is state 'Z'."""
    pid = os.fork()
    if pid == 0:                                        # pragma: no cover - the child
        os._exit(code)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if _state(pid) == "Z":
            return pid
        time.sleep(0.005)
    return pid


def _state(pid):
    """The /proc state letter, or None once the pid is gone. _proc_stat_fields yields BYTES."""
    fields = sup._proc_stat_fields(pid)
    return None if fields is None else fields[0].decode("ascii", "replace")


def test_orphan_reaper():
    """PID 1 must reap what reparents PAST the fork server to it, and stay bounded doing it.

    The fork server is a subreaper with a bounded re-enumerating FS_OP_SWEEP, so on every
    ordinary path a stray reparents there and is killed and reaped. This is the residual: a dead
    fork server, or a PR_SET_CHILD_SUBREAPER that never took, sends survivors past it to PID 1,
    where nothing ever waited on them — measured state 'Z', still 'Z' a second later, never
    waitpid()ed, one pid slot gone against pod_pids_limit for the pod's lifetime.

    The zombies here are real children of this process, forked and left unwaited, which is the
    same relationship a reparented orphan has to PID 1 — the kernel does not distinguish them.
    """
    reaper = sup._reap_orphans
    if os.environ.get(ENV_REAPER_UNBOUNDED) == "1":
        reaper = _reaper_unbounded
    elif os.environ.get(ENV_REAPER_NO_FS_SLOT) == "1":
        reaper = _reaper_no_fs_slot
    elif os.environ.get(ENV_REAPER_IGNORES_CLOSING) == "1":
        reaper = _reaper_ignores_closing
    elif os.environ.get(ENV_REAPER_NO_JOB_SLOT) == "1":
        reaper = _reaper_no_job_slot
    elif os.environ.get(ENV_REAPER_LOGS) == "1":
        reaper = _reaper_logging_publisher
    installed = reaper is not sup._reap_orphans
    suffix = (f" ({reaper.__name__} is installed: this is the control)" if installed else "")

    # --- it reaps at all, and the thing it reaped really was a zombie first ---
    pid = _fork_zombie(code=3)
    was_zombie = _state(pid) == "Z"
    reaped = reaper(None, 8)
    check("orphan reaper: an unwaited child of PID 1 really is a permanent zombie until "
          "something reaps it, so this drives the state the bead measured",
          was_zombie, f"state was {_state(pid)!r}" + suffix)
    check("orphan reaper: a bounded waitpid(-1, WNOHANG) sweep reaps it, giving the pid slot "
          "back to a replicas-1 pod that serves every later user",
          pid in reaped and _state(pid) is None,
          f"reaped={reaped}, state now {_state(pid)!r}" + suffix)

    # --- and it STOPS. The cap is not decoration: this runs inside a signal handler. ---
    zombies = [_fork_zombie() for _ in range(8)]
    took = reaper(None, 3)
    left = [z for z in zombies if _state(z) == "Z"]
    check("orphan reaper: ONE delivery reaps at most max_rounds children and then returns — an "
          "unbounded waitpid loop in a signal handler pins PID 1's main thread",
          len(took) == 3 and len(left) >= 5,
          f"reaped {len(took)} in one call, {len(left)}/8 still zombies" + suffix)
    for _ in range(8):
        if not any(_state(z) == "Z" for z in zombies):
            break
        reaper(None, 8)
    check("orphan reaper: and the cap only defers — later deliveries clear the rest",
          all(_state(z) is None for z in zombies),
          f"{[z for z in zombies if _state(z) is not None]} left" + suffix)

    # --- THE ONE GENUINE COLLISION: fs.pid. waitpid(-1) cannot skip a pid, so it publishes. ---
    left_sock, right_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        fs_pid = _fork_zombie(code=7)
        fs = sup.ForkServer(fs_pid, left_sock)
        reaper(fs, 8)
        check("orphan reaper / fs.pid: the fork server's wait status is PUBLISHED through the "
              "handle rather than dropped — waitpid(-1) reports which child it took only after "
              "taking it, so the reaper cannot skip fs.pid and must hand it over",
              fs.exit_status is not None and os.WEXITSTATUS(fs.exit_status) == 7,
              f"exit_status={fs.exit_status!r}" + suffix)
        check("orphan reaper / fs.pid: publishing clears fs.pid and marks the handle broken, so "
              "close()'s grace loop has nothing left to poll and never SIGKILLs a pid the "
              "kernel may already have recycled",
              fs.pid is None and fs._broken is not None,
              f"pid={fs.pid!r}, broken={fs._broken!r}" + suffix)
        check("orphan reaper / fs.pid: and /health sees it dead without a syscall of its own",
              fs.alive() is False, f"alive()={fs.alive()!r}" + suffix)
    finally:
        left_sock.close()
        right_sock.close()

    # --- AND IT DOES ALL OF THAT WITHOUT LOGGING. The handler is claimed silent; it was not. ---
    left_sock, right_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        silent_pid = _fork_zombie(code=13)
        fs4 = sup.ForkServer(silent_pid, left_sock)
        with _LogCapture() as cap:
            reaper(fs4, 8)
            during = list(cap.lines)
            fs4.alive()
            after = cap.lines[len(during):]
        check("orphan reaper: the handler's whole path emits NO log record. It used to reach "
              "LOG.error through note_reaped -> _mark_broken, and MEASURED against main()'s own "
              "logging setup with a stalled stdout consumer that raised `reentrant call inside "
              "<_io.BufferedWriter>` INSIDE the handler, aborting the delivery with 4 of 5 "
              "zombies unreaped",
              during == [], f"the handler path logged {during}" + suffix)
        check("orphan reaper: and the reason is not lost with the log call — alive(), on a "
              "normal thread, prints the line the handler could not. _mark_broken sets `_broken` "
              "BEFORE it logs, so a log call that failed there lost this line for good",
              any("unusable and will not be reused" in line for line in after),
              f"lines after the reap: {after}" + suffix)
    finally:
        left_sock.close()
        right_sock.close()

    # --- THE COLLISION THE OLD DOCSTRING DENIED: an execution child stranded by a dead fork
    # server is not a grandchild any more. It reparents to PID 1 and this reaper takes it. ---
    supervisor = sup.Supervisor("/nonexistent/orphan-reaper-check")
    job = sup.Job(sup.parse_execute_request(json.dumps(make_body()).encode()), None)
    stranded_pid = os.fork()
    if stranded_pid == 0:                               # pragma: no cover - the child
        os.setsid()          # its OWN process group, exactly as a real execution child does
        os._exit(6)
    deadline = time.monotonic() + 5
    while _state(stranded_pid) != "Z" and time.monotonic() < deadline:
        time.sleep(0.005)
    job.pid = stranded_pid
    supervisor._running = job
    reaper(None, 8, supervisor=supervisor)
    check("orphan reaper / stranded execution: the child's wait status is published to the "
          "running job. test_forkserver_death_mid_execution already drives the case that "
          "produces it — a fork server that dies mid-execution leaves its child a DIRECT child "
          "of PID 1, and _reap raised, so nothing else will ever mark that job reaped",
          job.reaped and job.pid is None and job.reaped_status is not None
          and os.WEXITSTATUS(job.reaped_status) == 6,
          f"reaped={job.reaped}, pid={job.pid!r}, status={job.reaped_status!r}" + suffix)

    kills = []
    real_kill, real_killpg = os.kill, os.killpg
    os.kill = lambda pid, sig: (kills.append(("kill", pid, sig)), real_kill(pid, sig))[1]
    os.killpg = lambda pgid, sig: (kills.append(("killpg", pgid, sig)), real_killpg(pgid, sig))[1]
    try:
        with _LogCapture() as cap:
            started_kill = time.monotonic()
            answer = sup._signal_group(job, signal.SIGKILL)
            sup._kill_group(job)
            kill_elapsed = time.monotonic() - started_kill
            kill_lines = list(cap.lines)
    finally:
        os.kill, os.killpg = real_kill, real_killpg
    check("orphan reaper / stranded execution: NOTHING SIGNALS THAT PID AFTERWARDS. The reaper "
          "freed it, so the number is the kernel's to hand out again — measured as real PID 1 "
          "with ns_last_pid forcing reuse, the pre-fix code killed an unrelated bystander that "
          "had been forked onto it",
          answer == sup._SIGNAL_GONE and kills == [],
          f"_signal_group said {answer}; syscalls issued: {kills}" + suffix)
    check("orphan reaper / stranded execution: and _kill_group returns at once rather than "
          "spending the whole of KILL_GRACE_S polling a job that can never go reaped and then "
          "escalating onto that pid",
          kill_elapsed < sup.KILL_GRACE_S / 2,
          f"took {kill_elapsed:.2f}s against a {sup.KILL_GRACE_S}s grace" + suffix)
    check("orphan reaper / stranded execution: the false diagnostic is unreachable too — the "
          "child DID setsid() into its own group, and 'no process group of its own' was only "
          "_resolve_pgid reading a pid the reaper had already freed",
          not any("no process group of its own" in line for line in kill_lines),
          f"logged {kill_lines}" + suffix)

    # --- The same collision on the completion path, which the stranded checks above cannot
    # reach. _reap stamps job.reaped_pgid BEFORE the waitpid that can raise ForkServerError, so a
    # fork server dying between the two replies leaves the pgid stamped and `reaped` False: the
    # stranded branch is not taken and the else branch calls _kill_survivors unconditionally.
    # For a setsid() child that pgid IS the pid. ---
    publisher = _publish_real
    if os.environ.get(ENV_PUBLISH_KEEPS_PGID) == "1":
        publisher = _publish_keeps_pgid
    elif os.environ.get(ENV_PUBLISH_NO_REAPED_GUARD) == "1":
        publisher = _publish_no_reaped_guard
    pub_suffix = ("" if publisher is _publish_real
                  else f" ({publisher.__name__} is installed: this is the control)")

    # A LIVE process in its own group, standing in for whoever holds that number after the
    # reaper frees it. It has to be live: a group with only zombies in it is one _kill_survivors
    # walks away from anyway, which would leave the control green.
    hold2_r, hold2_w = os.pipe()
    bystander = os.fork()
    if bystander == 0:                                  # pragma: no cover - the child
        os.setsid()          # its OWN process group, so its pgid IS its pid
        os.close(hold2_w)
        try:
            os.read(hold2_r, 1)
        finally:
            os._exit(0)
    os.close(hold2_r)
    try:
        job2 = sup.Job(sup.parse_execute_request(json.dumps(make_body()).encode()), None)
        job2.pid = bystander
        deadline = time.monotonic() + 5
        while sup._resolve_pgid(job2) is None and time.monotonic() < deadline:
            time.sleep(0.005)
        job2.reaped_pgid = sup._resolve_pgid(job2)       # exactly _reap's stamp
        stamped = job2.reaped_pgid
        supervisor._running = job2
        took2 = publisher(supervisor, bystander, 0)      # ...and then the reap does NOT happen
        kills2 = []
        real_kill, real_killpg = os.kill, os.killpg
        os.kill = lambda pid, sig: (kills2.append(("kill", pid, sig)), real_kill(pid, sig))[1]
        os.killpg = lambda pgid, sig: (kills2.append(("killpg", pgid, sig)),
                                       real_killpg(pgid, sig))[1]
        try:
            with _LogCapture() as cap:
                survivors = sup._kill_survivors(job2)
                surv_lines = list(cap.lines)
        finally:
            os.kill, os.killpg = real_kill, real_killpg
        still_alive = sup._pid_is_live(bystander)
        check("orphan reaper / completed execution: publishing clears the RECORDED PGID as well "
              "as the pid, so _kill_survivors signals nothing. It is the one reader of "
              "reaped_pgid and it runs on the else branch UNCONDITIONALLY, so a stamp that "
              "outlived the reap sent SIGTERM to a group whose number the reaper had just made "
              "recyclable — for a setsid() child that number is the child's own pid",
              took2 and job2.reaped_pgid is None and survivors is False and kills2 == [],
              f"published={took2}, reaped_pgid stamped {stamped!r} now {job2.reaped_pgid!r}, "
              f"_kill_survivors said {survivors!r}, syscalls issued: {kills2}" + pub_suffix)
        check("orphan reaper / completed execution: and the process still holding that group "
              "number is untouched, INCLUDING the announcement. MEASURED under `unshare -Urpf "
              "--mount-proc` with ns_last_pid: the supervisor logged 'process group 117 still "
              "has members; killing them' about an unrelated process and killed it",
              still_alive and not any("still has members" in line for line in surv_lines),
              f"bystander {bystander} live={still_alive}, logged {surv_lines}" + pub_suffix)
    finally:
        try:
            os.write(hold2_w, b"x")
        except OSError:
            pass
        os.close(hold2_w)
        try:
            os.kill(bystander, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.waitpid(bystander, 0)
        except OSError:
            pass

    # --- AND A JOB THAT WAS REAPED NORMALLY REFUSES A FOREIGN STATUS. _reap never clears
    # job.pid and _release does not clear _running until the whole response is built. ---
    sup_src = open(os.path.join(ROOT, "sandbox", "supervisor.py"), encoding="utf-8").read()
    reap_node = next(n for n in ast.parse(sup_src).body
                     if isinstance(n, ast.FunctionDef) and n.name == "_reap")
    reap_src = ast.get_source_segment(sup_src, reap_node) or ""
    job3 = sup.Job(sup.parse_execute_request(json.dumps(make_body()).encode()), None)
    job3.pid = 0x7FFFFFF0        # a number this process never forked: the recycled pid's stand-in
    job3.reaped = True           # ...reaped by _reap itself, which leaves job.pid naming it
    supervisor._running = job3
    took3 = publisher(supervisor, job3.pid, 1337)
    check("orphan reaper / already-reaped job: `job.reaped` is part of the match, so a pid the "
          "kernel recycled after an ORDINARY reap cannot stamp a foreign wait status onto a "
          "healthy execution. reaped_status has exactly one reader — _execute_inner's `is not "
          "None` check — so the harm is bounded to a spurious 'the fork server died "
          "mid-execution' ERROR, and the docstring's _running/pid-turnover argument is now true "
          "as written rather than true by luck",
          took3 is False and job3.reaped_status is None,
          f"published={took3}, reaped_status={job3.reaped_status!r}; the case is reachable "
          f"because _reap clears job.pid: {'job.pid = None' in reap_src}" + pub_suffix)
    supervisor._running = None

    # --- and close() OWNS fs.pid for the whole of its grace loop ---
    left_sock, right_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        closing_pid = _fork_zombie(code=5)
        fs2 = sup.ForkServer(closing_pid, left_sock)
        fs2._closing = True
        took2 = reaper(fs2, 8)
        check("orphan reaper / close(): with `_closing` set the reaper stands down entirely, so "
              "no reap can land between close()'s last poll and its SIGKILL",
              took2 == [] and _state(closing_pid) == "Z",
              f"reaped {took2}, pid state {_state(closing_pid)!r}" + suffix)
        fs2.close(grace=1.0)
        check("orphan reaper / close(): and close() still reaps it itself, so standing down "
              "costs no zombie",
              _state(closing_pid) is None, f"state {_state(closing_pid)!r}" + suffix)
    finally:
        left_sock.close()
        right_sock.close()

    # --- and close() SETS THE FLAG FIRST. The check above hands it the flag; this one makes
    # close() produce it, which is the ordering its docstring calls LOAD-BEARING. ---
    closer = sup.ForkServer.close
    if os.environ.get(ENV_CLOSING_LAST) == "1":
        closer = _close_closing_last
    closing_ctl = ("" if closer is sup.ForkServer.close else
                   f" ({ENV_CLOSING_LAST}=1 is installed: this is the control)")
    ordering_pid = _fork_zombie(code=8)
    observed = []
    # The probe runs at the FIRST thing close()'s body touches after the flag should be set.
    fs6 = sup.ForkServer(ordering_pid, _ProbeSock(lambda: observed.append(reaper(fs6, 8))))
    closer(fs6, grace=0.5)
    check("orphan reaper / close(): `_closing` is set BEFORE close() touches anything else — a "
          "reaper delivered at the first instruction of close()'s body finds the flag already "
          "set and stands down, which is what keeps the SIGKILL at the end of the grace loop "
          "off a pid the reaper freed. Driven THROUGH close(), not by setting the flag by hand",
          observed == [[]] and fs6.exit_status is None,
          f"the reaper saw {observed} from inside close(), exit_status={fs6.exit_status!r}"
          + closing_ctl + suffix)
    check("orphan reaper / close(): and the pid it stood down over is still close()'s to reap",
          _state(ordering_pid) is None,
          f"state {_state(ordering_pid)!r}" + closing_ctl + suffix)

    # --- the production wiring: a SIGCHLD handler, in the main thread, that cannot raise ---
    if installed:
        skip("orphan reaper: the SIGCHLD handler end to end",
             f"{reaper.__name__} is installed; the handler calls the real _reap_orphans")
    else:
        install = sup.install_orphan_reaper
        if os.environ.get(ENV_REAPER_SIG_IGN) == "1":
            install = _install_sig_ign
        install_ctl = ("" if install is sup.install_orphan_reaper else
                       f" ({ENV_REAPER_SIG_IGN}=1 is installed: this is the control)")
        previous = signal.getsignal(signal.SIGCHLD)
        published = []
        left_sock, right_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        # The child must not die before the handle it publishes into exists, so it waits for a
        # byte rather than racing the parent.
        hold_r, hold_w = os.pipe()
        try:
            handler_pid = os.fork()
            if handler_pid == 0:                        # pragma: no cover - the child
                os.close(hold_w)
                os.read(hold_r, 1)
                os._exit(11)
            os.close(hold_r)
            fs5 = sup.ForkServer(handler_pid, left_sock)
            server_stub = types.SimpleNamespace(forkserver=fs5)
            server_stub.note_child_reaped = (
                lambda pid, status: published.append((pid, status)) or False)
            ok = install(server_stub)
            os.write(hold_w, b"x")
            deadline = time.monotonic() + 5
            while _state(handler_pid) is not None and time.monotonic() < deadline:
                time.sleep(0.01)
            check("orphan reaper: main()'s SIGCHLD handler reaps without anybody calling "
                  "waitpid — the zombie is gone at the moment it appears, not a poll later",
                  ok and _state(handler_pid) is None,
                  f"installed={ok}, state {_state(handler_pid)!r}" + install_ctl)
            check("orphan reaper: and it is OUR reaper that took it, not the kernel. The status "
                  "reached BOTH publishers, which is a side effect nothing else produces: "
                  "signal(SIGCHLD, SIG_IGN) leaves the pid just as gone while _reap_orphans, "
                  "note_reaped, note_child_reaped and `_closing` are all unreachable from "
                  "production",
                  fs5.exit_status is not None and os.WEXITSTATUS(fs5.exit_status) == 11
                  and published == [(handler_pid, fs5.exit_status)],
                  f"fs.exit_status={fs5.exit_status!r}, published={published}" + install_ctl)
        finally:
            signal.signal(signal.SIGCHLD, previous)
            try:
                os.close(hold_w)
            except OSError:
                pass
            left_sock.close()
            right_sock.close()

    tree = ast.parse(open(os.path.join(ROOT, "sandbox", "supervisor.py"), encoding="utf-8").read())
    main_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    wired = any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "install_orphan_reaper"
                for n in ast.walk(main_fn))
    check("orphan reaper: main() actually installs it — the checks above drive the reaper "
          "directly, and nothing else ties it to the process that runs as PID 1",
          wired, "main() never calls install_orphan_reaper")


def test_drain_continuous_writer():
    """A descendant that writes CONTINUOUSLY must not hold a drain thread for the pod's lifetime.

    The deadline used to be evaluated only inside `if not ready:`, so a setsid()'d escapee writing
    without pause kept `ready` truthy on every pass and the deadline was never reached. The writer
    here never stops and its write end is never closed while the drain runs — a writer that stops,
    or an EOF, is precisely the case the pre-fix code already handled.

    The sink sleeps a few milliseconds per block so the writer stays ahead of the reader and the
    64 KiB pipe is never observed empty; without that the pre-fix loop reaches its deadline too,
    which would make the control flaky rather than red.
    """
    drain = (_drain_deadline_in_ready_branch
             if os.environ.get(ENV_DRAIN_READY_ONLY) == "1" else sup._drain)
    installed = drain is not sup._drain
    suffix = (" (SUPERVISOR_TEST_DRAIN_DEADLINE_IN_READY=1 is installed: this is the control)"
              if installed else "")
    grace = 0.5

    read_fd, write_fd = os.pipe()
    stop = threading.Event()
    reaped = threading.Event()
    written = [0]
    result = []

    def writer():
        while not stop.is_set():
            try:
                written[0] += os.write(write_fd, b"x" * 4096)
            except OSError:
                break

    def sink(block):
        time.sleep(0.005)

    w = threading.Thread(target=writer, daemon=True)
    w.start()
    started = time.monotonic()
    reaped.set()

    def run():
        result.append(drain(read_fd, limit=None, reaped=reaped, grace=grace, poll=0.05,
                            sink=sink))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=grace + 6.0)
    elapsed = time.monotonic() - started
    # The regime, sampled at the moment the drain let go. `written` cannot be used: once nothing
    # reads the pipe the writer blocks in os.write and its counter stops by definition. What
    # proves the continuous case is that the write end was still live and the fd still had unread
    # bytes.
    fd_still_ready = bool(select.select([read_fd], [], [], 0)[0])
    writer_live = w.is_alive() and not stop.is_set()
    stop.set()
    try:
        os.close(write_fd)
    except OSError:
        pass
    w.join(timeout=5)
    t.join(timeout=5)
    try:
        os.close(read_fd)
    except OSError:
        pass

    check("drain deadline: the drain gave up with the write end STILL LIVE and the fd STILL "
          "READY — the continuous-writer regime the deadline exists for, not a writer that "
          "stopped or an EOF",
          fd_still_ready and writer_live and written[0] > 65536,
          f"fd ready={fd_still_ready}, writer live={writer_live}, "
          f"{written[0]} bytes written" + suffix)
    check("drain deadline: a continuously-written pipe does NOT hold the drain thread for the "
          "pod's lifetime — the deadline is evaluated whether or not the fd is ready",
          bool(result) and result[0][3] is True,
          f"returned={bool(result)}, result={result[0] if result else None}" + suffix)
    check("drain deadline: and it gives up ON the deadline rather than merely eventually — "
          "well inside DRAIN_GRACE_S plus the join slack _execute_inner allows",
          bool(result) and elapsed < grace + 2.0,
          f"took {elapsed:.2f}s against a {grace}s grace" + suffix)

    # The regression guard for the fix itself: moving the check out of the `not ready` branch
    # must not abandon a pipe whose writer simply finished. This one DOES close its write end.
    read_fd, write_fd = os.pipe()
    done = threading.Event()
    os.write(write_fd, b"y" * 1000)
    os.close(write_fd)
    done.set()
    body, total, stopped, abandoned = sup._drain(read_fd, limit=1 << 20, reaped=done,
                                                 grace=grace, poll=0.02)
    os.close(read_fd)
    check("drain deadline: a writer that finishes still yields every byte and a clean EOF — the "
          "deadline moving out of the `not ready` branch must not truncate an ordinary result",
          body == b"y" * 1000 and total == 1000 and not stopped and not abandoned,
          f"{len(body)} bytes, total={total}, stopped={stopped}, abandoned={abandoned}")
