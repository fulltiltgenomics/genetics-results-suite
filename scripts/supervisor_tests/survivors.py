import os

from .harness import Server, check, make_body, skip, sup


def _proc_state(pid):
    """The state letter for `pid`, or None when the pid is gone.

    A zombie is not a survivor, and os.kill(pid, 0) cannot tell the two apart — it succeeds for
    both. "We contained it" and "it is still running the attacker's code" must not be the same
    observation.
    """
    fields = sup._proc_stat_fields(pid)
    if not fields:
        return None
    return fields[0].decode("ascii", "replace")


_SURVIVOR_SLEEP_S = 30


def _survivor_code(detach):
    """A script that leaves ONE process behind and prints its pid, then exits normally.

    It holds none of the supervisor's descriptors, deliberately: 0/1/2 go to /dev/null and the
    status and audit pipes are closed, so all three drains reach EOF when the direct child exits.
    Without that the test would be measuring the drain deadline rather than containment.

    `detach` is the only difference between the two cases: with setsid() the survivor leaves the
    process group and no killpg can reach it; without it, it stays in the group and a
    normally-completing execution simply never signalled that group. Both were measured
    surviving.
    """
    return (
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        + ("    os.setsid()\n" if detach else "")
        + "    for fd in (3, 4):\n"
        "        try:\n"
        "            os.close(fd)\n"
        "        except OSError:\n"
        "            pass\n"
        "    null = os.open('/dev/null', os.O_RDWR)\n"
        "    for fd in (0, 1, 2):\n"
        "        os.dup2(null, fd)\n"
        f"    time.sleep({_SURVIVOR_SLEEP_S})\n"
        "    os._exit(0)\n"
        "print('SURVIVOR', pid, flush=True)\n"
    )


def _run_survivor_probe(server, detach):
    """(pid of the process left behind, response body). pid is None if the probe misfired."""
    status, _, body = server.request(
        "POST", "/execute", body=make_body(code=_survivor_code(detach)))
    body = body or {}
    if status != 200:
        return None, body
    for line in (body.get("output") or "").splitlines():
        if line.startswith("SURVIVOR "):
            return int(line.split()[1]), body
    return None, body


def test_survivors(server):
    """A depth-1 survivor does not outlive its own execution.

    Measured before this landed, after a status-ok execution, both of these were still running
    and would have been for the pod's lifetime: a plain fork that stayed in the process group,
    and a setsid() escapee. The first is the completion path having two _kill_group call sites
    that a reaped job reaches neither of. The second is not reachable by any process-group kill
    at all; what reaches it is PARENTAGE — the fork server is a subreaper, so an escapee whose
    parent exits reparents to it and FS_OP_SWEEP kills and reaps it. Nothing here separates the
    two mechanisms: the in-group probe's assertion is satisfied by the sweep alone, and the
    negative control disables _kill_survivors and _sweep_strays together.

    The negative control is the second half of this test: a probe whose fork silently failed
    would make the positive assertions pass over nothing, so the same two probes run again with
    both disabled and the survivors must then BE there.

    What it does not prove: that the survivor was harmless while its own execution ran (it was
    alive for all of it, by construction), and anything at all about gVisor.
    """
    if server.container or server.supervisor is None:
        skip("survivors: a depth-1 survivor does not outlive its own execution",
             "the probe reads /proc for a pid in the supervisor's own namespace")
        return
    try:
        for detach, label in ((True, "a setsid() escapee"), (False, "an in-group grandchild")):
            pid, body = _run_survivor_probe(server, detach)
            check(f"survivors: the {label} probe ran and named the process it left behind",
                  pid is not None, f"got {body}")
            if pid is None:
                continue
            check(f"survivors: the execution that left {label} still answers ok",
                  body.get("status") == "ok", f"got {body.get('status')} {body.get('error')}")
            state = _proc_state(pid)
            check(f"survivors: {label} does not survive a normally-completing execution",
                  state is None, f"pid {pid} is still there in state {state!r}")

        leftovers = []
        real_kill = sup._kill_survivors
        real_sweep = sup.Supervisor._sweep_strays
        sup._kill_survivors = lambda job: False
        sup.Supervisor._sweep_strays = lambda self, job: None
        try:
            for detach, label in ((True, "a setsid() escapee"), (False, "an in-group grandchild")):
                pid, body = _run_survivor_probe(server, detach)
                state = _proc_state(pid) if pid is not None else None
                check(f"survivors: NEGATIVE CONTROL — with the group kill and the sweep "
                      f"disabled, {label} IS still running after the execution completes",
                      state is not None and state != "Z",
                      f"pid {pid} state {state!r} (the probe proves nothing if this passes "
                      f"only because the fix ran)")
                if state is not None:
                    leftovers.append(pid)
        finally:
            sup._kill_survivors = real_kill
            sup.Supervisor._sweep_strays = real_sweep

        # (killed, reaped-as-zombies): a zombie is not a survivor and the two are reported
        # separately, so this assertion must read the killed half.
        swept, _reaped = server.supervisor.forkserver.sweep()
        check("survivors: an explicit sweep kills and reaps exactly what the negative control "
              "left behind, so the sweep is what does the work",
              leftovers and set(leftovers) <= set(swept)
              and all(_proc_state(p) is None for p in leftovers),
              f"left {leftovers}, swept {swept}")
    finally:
        server.close()


def _chain_code():
    """A script that leaves a DEPTH-2 chain of setsid()'d processes behind, and names both.

    The middle process must stay alive until the sweep runs, which is the entire difference from
    _survivor_code and the reason a single-pass sweep missed this shape: B reparents to the fork
    server only when A exits, so while A is alive no enumeration can see B. Both call setsid(),
    so B is not in A's process group either.
    """
    return (
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    b = os.fork()\n"
        "    if b == 0:\n"
        "        os.setsid()\n"
        "        for fd in (3, 4):\n"
        "            try:\n"
        "                os.close(fd)\n"
        "            except OSError:\n"
        "                pass\n"
        "        null = os.open('/dev/null', os.O_RDWR)\n"
        "        for fd in (0, 1, 2):\n"
        "            os.dup2(null, fd)\n"
        f"        time.sleep({_SURVIVOR_SLEEP_S})\n"
        "        os._exit(0)\n"
        "    print('CHAIN', pid, b, flush=True)\n"
        "    for fd in (3, 4):\n"
        "        try:\n"
        "            os.close(fd)\n"
        "        except OSError:\n"
        "            pass\n"
        "    null = os.open('/dev/null', os.O_RDWR)\n"
        "    for fd in (0, 1, 2):\n"
        "        os.dup2(null, fd)\n"
        f"    time.sleep({_SURVIVOR_SLEEP_S})\n"
        "    os._exit(0)\n"
        # The direct child exits here, which is what starts A's reparenting. The pause only
        # keeps the print ahead of the exit so the harness reads the pids from a live pipe.
        "time.sleep(0.3)\n"
    )


def _run_chain_probe(server):
    """((pid of A, pid of B), body). The pids are None if the probe misfired. A prints the pair
    because the pid the parent gets from fork() is A's, and B's is only knowable inside A."""
    status, _, body = server.request(
        "POST", "/execute", body=make_body(code=_chain_code()))
    body = body or {}
    if status != 200:
        return (None, None), body
    for line in (body.get("output") or "").splitlines():
        if line.startswith("CHAIN "):
            _, a, b = line.split()
            return (int(a), int(b)), body
    return (None, None), body


def test_survivor_chain(root):
    """A DEPTH-2 setsid() chain does not survive its own execution either.

    Measured against the single-pass sweep this replaced: B was in state S, running, for the
    whole of the NEXT execution. test_survivors cannot catch it — both of its probes are depth 1,
    which is exactly the shape a single enumeration sees. A process reparents to the subreaper
    only when its own parent exits, so B is invisible while A lives, becomes the fork server's
    child after the sweep has killed A, and then needs a second enumeration.

    The negative control is a SECOND supervisor with FS_SWEEP_MAX_ROUNDS at 1, and it has to be
    a second one because the fork server is forked at bring_up(): patching the constant
    afterwards would change the harness's copy, not the one the sweep reads.
    """
    servers = []
    try:
        real_rounds = sup.FS_SWEEP_MAX_ROUNDS
        sup.FS_SWEEP_MAX_ROUNDS = 1
        try:
            neg = Server(os.path.join(root, "one-round"))
        finally:
            sup.FS_SWEEP_MAX_ROUNDS = real_rounds
        servers.append(neg)
        if neg.supervisor is None:
            skip("survivor chain: a depth-2 setsid() chain does not outlive its execution",
                 "the probe reads /proc for a pid in the supervisor's own namespace")
            return
        (a, b), body = _run_chain_probe(neg)
        check("survivor chain: the depth-2 probe ran and named both processes it left behind",
              a is not None and b is not None, f"got {body}")
        state_b = _proc_state(b) if b is not None else None
        check("survivor chain: NEGATIVE CONTROL — with the sweep's re-enumeration disabled "
              "(one round) the grandchild of the chain IS still running afterwards",
              state_b is not None and state_b != "Z",
              f"A={_proc_state(a)!r} B={state_b!r} (the positive assertion below proves "
              f"nothing if this passes only because the fix ran)")
        swept, _reaped = neg.supervisor.forkserver.sweep()
        check("survivor chain: and one more sweep round, now that it has reparented, clears it",
              b is None or _proc_state(b) is None, f"swept {swept}, B={_proc_state(b)!r}")

        pos = Server(os.path.join(root, "all-rounds"))
        servers.append(pos)
        (a, b), body = _run_chain_probe(pos)
        check("survivor chain: the depth-2 probe ran under the real sweep too",
              a is not None and b is not None, f"got {body}")
        check("survivor chain: the execution that left the chain still answers ok",
              body.get("status") == "ok", f"got {body.get('status')} {body.get('error')}")
        states = (_proc_state(a) if a else None, _proc_state(b) if b else None)
        check("survivor chain: NEITHER process of a depth-2 setsid() chain survives a "
              "normally-completing execution",
              states == (None, None), f"A={states[0]!r} B={states[1]!r}")
    finally:
        for server in servers:
            server.close()
