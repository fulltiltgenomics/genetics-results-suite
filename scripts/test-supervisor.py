#!/usr/bin/env python3
"""Offline assertions about sandbox/supervisor.py.

Run: python3 scripts/test-supervisor.py                       in-process (the fast path)
     python3 scripts/test-supervisor.py --container URL       against a running container
Exit 0 = pass, 1 = a property is broken, 2 = the harness could not run.

No cluster, no credentials, no image, no network beyond loopback. The in-process mode runs the
real supervisor in this interpreter with SANDBOX_SCRATCH_ROOT pointed at a temporary directory
and forks real children, so the fork/reap path, the per-execution environment and the artifact
manifest are exercised rather than mocked.

Container mode drives the same wire checks over HTTP against an image started by
scripts/run-sandbox-local.sh, plus a group that only exists there: the read-only rootfs, the
pruned venv, the seeded font cache and the absence of credentials in the child's environment
are properties OF THE IMAGE. The two totals are not comparable — every group that reaches into
the supervisor's own objects is not run at all over HTTP, and those are named in the closing
"not run in this mode" list. skip() is the narrower mechanism, for a check inside a group that
did run.

Two conventions run through the whole file and are what make it evidence rather than ritual:

* A control is DRIVEN AS THE FAILURE, not reasoned about. Every group that asserts a hazard is
  closed also restores the defect — usually by swapping in the pre-fix source, selected by a
  SUPERVISOR_TEST_* environment variable — and asserts the same probe then goes red. A clean
  result with no positive control is worth nothing, and several of these checks passed
  vacuously before their control existed.
* Anything about a HANG is driven on a thread with a deadline, so a regression fails the check
  rather than wedging the harness.

The properties under test are the ones where the two ends of the contract in
docs/code-execution-security.md could silently diverge: the queue-depth definition, the
duplicate-id refusal, reject-don't-clamp on timeout_s, the token consistency rules, which names
reach the manifest, and the Retry-After a client's retry policy reads off a 429. Every
supervisor limit is watched FIRING over the wire in both modes.

The SUPERVISOR_TEST_* controls are documented at the function that installs each one.
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from supervisor_tests import harness
from supervisor_tests.parsing import test_nsswitch, test_parsing
from supervisor_tests.queue import test_queue, test_peer_gone
from supervisor_tests.artifacts import test_manifest, test_artifact_integrity, test_artifact_encryption, test_artifact_scoping, test_artifact_fifo_does_not_block, test_seal_fifo_does_not_block
from supervisor_tests.wire import test_http, test_backpressure, test_limits, test_tokens, test_retained_ceiling, test_retention_expiry
from supervisor_tests.units import test_cap_units, test_hardening_units
from supervisor_tests.audit import test_audit_units, test_audit_stream
from supervisor_tests.image import test_container
from supervisor_tests.startup import test_startup_wipe
from supervisor_tests.tmpwipe import test_shared_tmp_wipe
from supervisor_tests.isolation import test_isolation
from supervisor_tests.forkserver import test_forkserver_units, test_forkserver_lost_fork_reply, test_forkserver_death_mid_execution
from supervisor_tests.survivors import test_survivors, test_survivor_chain
from supervisor_tests.preready import test_pre_ready_execute, test_pre_ready_body_bytes
from supervisor_tests.headreader import test_header_reader_units, test_head_timeout
from supervisor_tests.lifecycle import test_pipe_fd_ownership, test_shutdown_race, test_shutdown_count_units, test_shutdown_count_escapes, test_shutdown_ceiling
from supervisor_tests.reaper import test_orphan_reaper, test_drain_continuous_writer


def run_in_process():
    tmp = tempfile.mkdtemp(prefix="supervisor-test-")
    # EVERY group below runs the real _execute_inner, which empties SHARED_TMPFS_PATHS before
    # each fork. Left at its production value that is the INVOKING USER'S OWN /tmp and /dev/shm,
    # several hundred times over: mkdtemp honours TMPDIR, so the only thing standing between a
    # test run and the caller's ssh-agent socket is the `protect` refusal happening to fire
    # because the run's scratch root happened to land under /tmp. Set TMPDIR anywhere else and
    # it does not fire. Repointed here for the WHOLE run rather than per group, so no group can
    # reach the real paths whatever TMPDIR says; the group that is ABOUT the wipe repoints it
    # again to its own pair, and restores it to these.
    shared_tmpfs = (os.path.join(tmp, "shared-tmp"), os.path.join(tmp, "shared-shm"))
    for path in shared_tmpfs:
        os.makedirs(path)
    real_shared_tmpfs = harness.sup.SHARED_TMPFS_PATHS
    harness.sup.SHARED_TMPFS_PATHS = shared_tmpfs
    try:
        print("startup assertions")
        test_nsswitch(tmp)
        print("request parsing and token consistency")
        test_parsing()
        print("queue")
        test_queue(tmp)
        test_peer_gone()
        print("artifact manifest and retrieval")
        test_manifest(tmp)
        test_artifact_scoping(tmp)
        test_artifact_fifo_does_not_block(tmp)
        test_seal_fifo_does_not_block(tmp)
        test_artifact_integrity(tmp)
        test_artifact_encryption(tmp)
        print("startup wipe")
        test_startup_wipe(tmp)
        print("the runtime-supplied temp directories")
        test_shared_tmp_wipe(tmp)
        print("fork server units")
        test_forkserver_units(tmp)
        print("bounded header reads")
        test_header_reader_units()
        print("fork server failure paths")
        test_pre_ready_execute(tmp)
        test_pre_ready_body_bytes(tmp)
        test_forkserver_lost_fork_reply(tmp)
        test_forkserver_death_mid_execution(tmp)
        print("cross-execution memory isolation")
        test_isolation(tmp)
        print("end to end over HTTP")
        root = os.path.join(tmp, "scratch")
        os.makedirs(root)
        test_http(harness.Server(root))
        print("descriptor ownership and the shutdown gate")
        test_pipe_fd_ownership(tmp)
        test_shutdown_count_units(tmp)
        test_shutdown_count_escapes(tmp)
        test_shutdown_ceiling(tmp)
        test_shutdown_race(tmp)
        print("PID 1 orphan reaping and the drain deadline")
        test_orphan_reaper()
        test_drain_continuous_writer()
        print("head-read deadline and log sanitising")
        root = os.path.join(tmp, "headtimeout")
        os.makedirs(root)
        server = harness.Server(root)
        try:
            test_head_timeout(server)
        finally:
            server.close()
        print("what an execution leaves behind")
        root = os.path.join(tmp, "survivors")
        os.makedirs(root)
        test_survivors(harness.Server(root))
        root = os.path.join(tmp, "chain")
        os.makedirs(root)
        test_survivor_chain(root)
        print("backpressure over HTTP")
        root = os.path.join(tmp, "backpressure")
        os.makedirs(root)
        test_backpressure(harness.Server(root))
        print("capping and accounting units")
        test_cap_units(tmp)
        print("hardening units: budget, kill-path races, response bounds")
        test_hardening_units(tmp)
        print("audit stream units: the read-end caps and the re-framing")
        test_audit_units()
        print("audit stream over HTTP, into this process's own stdout")
        root = os.path.join(tmp, "audit")
        os.makedirs(root)
        server = harness.Server(root)
        try:
            test_audit_stream(server, harness._StdoutCapture)
        finally:
            server.close()
        print("supervisor limits, over HTTP")
        root = os.path.join(tmp, "limits")
        os.makedirs(root)
        server = harness.Server(root)
        try:
            test_limits(server)
            test_tokens(server)
            test_retained_ceiling(server)
        finally:
            server.close()
        print("artifact retention")
        root = os.path.join(tmp, "retention")
        os.makedirs(root)
        server = harness.Server(root, retention_s=2)
        try:
            test_retention_expiry(server)
        finally:
            server.close()
    finally:
        harness.sup.SHARED_TMPFS_PATHS = real_shared_tmpfs
        shutil.rmtree(tmp, ignore_errors=True)


def run_container(base_url, retention_s=None, container_name=None):
    """The same wire checks, against the image. Nothing here imports the container's
    supervisor: it is reached only through the two routes the contract defines, which is
    exactly what chat-backend's client can do."""
    server = harness.RemoteServer(base_url, retention_s=retention_s)
    try:
        status, _, body = server.request("GET", "/health")
    except OSError as exc:
        print(f"HARNESS: cannot reach {base_url}: {exc}", file=sys.stderr)
        print("         Start one with scripts/run-sandbox-local.sh", file=sys.stderr)
        raise SystemExit(2)
    if status != 200 or (body or {}).get("status") != "ok":
        print(f"HARNESS: {base_url}/health is not ok yet: {status} {body}", file=sys.stderr)
        raise SystemExit(2)
    if body["busy"] or body["queued"]:
        # Concurrency is 1 cluster-wide, so a second driver would take 429s that the
        # backpressure test attributes to its own three requests.
        print(f"HARNESS: {base_url} is already executing something: {body}", file=sys.stderr)
        raise SystemExit(2)
    print(f"end to end over HTTP against the container at {base_url}")
    test_http(server)
    print("backpressure over HTTP against the container")
    test_backpressure(server)
    print("image properties, from inside a real execution")
    test_container(server)
    print("supervisor limits, against the container")
    test_limits(server)
    test_tokens(server)
    # The audit stream is the one control whose output is not on the wire: it goes to the
    # container's stdout. Reading it needs the container's name, so without one this proves
    # nothing and says so by name rather than passing quietly.
    if container_name:
        print("audit stream, against the container's own stdout")
        test_audit_stream(server, lambda: harness._DockerLogCapture(container_name))
    else:
        harness.skip("audit stream against the container",
             "no --container-name; the records go to the container's stdout, not the wire")
    print("artifact retention, against the container")
    test_retention_expiry(server)
    # LAST, deliberately: it leaves the retained set near its ceiling, and /scratch is 512Mi.
    print("retained-artifact ceiling, against the container")
    test_retained_ceiling(server)

    # Whole groups, not individual skips: there is no route on the wire that reaches the
    # supervisor's own objects, so these never execute here. Naming them is the difference
    # between a subset run and a subset run that looks complete.
    harness.NOT_RUN.extend([
        "startup assertions (test_nsswitch) — reads the container's /etc/nsswitch.conf",
        "request parsing and token consistency (test_parsing) — calls the parser directly",
        "queue (test_queue, test_peer_gone) — inspects the supervisor's queue objects",
        "artifact manifest (test_manifest) — needs the harness's own view of /scratch",
        "artifact integrity (test_artifact_integrity) — tampers with a retained artifact "
        "directly, which needs the harness's own view of /scratch",
        "artifact encryption at rest (test_artifact_encryption) — reads a retained artifact "
        "off disk at the shared uid and drives _seal_retained directly, both of which need "
        "the harness's own view of /scratch",
        "what an execution leaves behind (test_survivors) — reads /proc for a pid in the "
        "supervisor's pid namespace, and disables the kill and the sweep in the module to get "
        "its negative control",
        "startup wipe (test_startup_wipe) — calls wipe_unrecognised_scratch() directly",
        "the runtime-supplied temp directories (test_shared_tmp_wipe) — repoints SHARED_TMPFS_PATHS in the module and swaps the wipe for a no-op to get its control, neither of which is reachable over the wire",
        "capping and accounting units (test_cap_units) — calls _cap_output/_dir_usage directly",
        "hardening units (test_hardening_units) — calls _trim_artifacts/_cap_response/_reap directly",
        "audit stream units (test_audit_units) — calls _AuditForwarder and _drain directly",
        "PID 1 orphan reaping and the drain deadline (test_orphan_reaper, "
        "test_drain_continuous_writer) — forks its own zombies, drives _reap_orphans and "
        "ForkServer.note_reaped directly and installs a SIGCHLD handler in this process, none "
        "of which is reachable over the wire",
        "fork server units (test_forkserver_units) — drives ForkServer and _payload_fd directly",
        "fork server failure paths (test_pre_ready_execute, test_pre_ready_body_bytes, "
        "test_forkserver_lost_fork_reply, test_forkserver_death_mid_execution) — needs to bind "
        "its own pre-ready supervisor and gate ForkServer.start() around a refused request, to "
        "drop a fork reply inside the control protocol and to SIGKILL the fork server, none of "
        "which is reachable over the wire",
        "bounded header reads (test_header_reader_units) — drives _HeaderBoundedReader over a "
        "socketpair, which needs the module",
        "head-read deadline and log sanitising (test_head_timeout) — times a stalled head "
        "against HEAD_READ_TIMEOUT_S and reads the supervisor's own LOG, so it needs the "
        "module and a socket whose latency is the harness's own",
        "cross-execution memory isolation (test_isolation) — plants its positive control in "
        "the supervisor module before the fork server is forked, which needs the module",
    ])


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    usage = ("usage: test-supervisor.py [--container URL [--retention-s N] "
             "[--container-name NAME]]")
    base_url = None
    retention_s = None
    container_name = None
    try:
        while argv:
            flag = argv.pop(0)
            if flag == "--container":
                base_url = argv.pop(0)
            elif flag == "--retention-s":
                # Asserted by the caller, never discovered: the wire contract exposes no
                # retention field and inventing one to make a test easier would be inventing a
                # contract.
                retention_s = int(argv.pop(0))
            elif flag == "--container-name":
                # Same standing: the audit stream leaves by the container's stdout, which is
                # not on the wire, so the harness is TOLD where to read it rather than
                # discovering it.
                container_name = argv.pop(0)
            else:
                raise ValueError(flag)
    except (IndexError, ValueError):
        print(usage, file=sys.stderr)
        return 2
    if (retention_s is not None or container_name is not None) and not base_url:
        print(usage, file=sys.stderr)
        return 2

    if base_url:
        run_container(base_url, retention_s=retention_s, container_name=container_name)
    else:
        run_in_process()

    print()
    if harness.SKIPPED:
        print(f"{len(harness.SKIPPED)} skipped:")
        for line in harness.SKIPPED:
            print(f"  - {line}")
    if harness.NOT_RUN:
        print(f"{len(harness.NOT_RUN)} check groups NOT RUN in this mode (not skips — never invoked):")
        for line in harness.NOT_RUN:
            print(f"  - {line}")
    if harness.FAILURES:
        print(f"FAILED {len(harness.FAILURES)}/{harness.CHECKS} checks:")
        for line in harness.FAILURES:
            print(f"  - {line}")
        return 1
    print(f"OK: {harness.CHECKS} checks passed"
          + (f", {len(harness.SKIPPED)} skipped" if harness.SKIPPED else ""))
    if harness.NOT_RUN:
        print(f"     PARTIAL COVERAGE: {harness.CHECKS} is the container-mode total, not a fraction of"
              " the in-process run.")
        print(f"     {len(harness.NOT_RUN)} groups above never executed. Run scripts/test-supervisor.py"
              " with no arguments for those.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
