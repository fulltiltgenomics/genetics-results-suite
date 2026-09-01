"""The runtime-supplied temp directories, and that nothing survives an execution boundary in them.

The channel is real rather than theoretical: gVisor supplies /tmp and /dev/shm whatever the pod
spec declares, and on staging a marker written by one execution was read by the next. These
checks drive that channel end to end against a stand-in pair of directories, and two of them
restore a defect — the wipe swapped for a no-op, and an entry that cannot be unlinked — so a
green result here is not vacuous.

The closing check is not about behaviour at all: the wipe sits one level above ForkServer's
fork_child, so a second fork path added later would skip it without failing anything here. That
one reads the source.
"""

import ast
import contextlib
import io
import os

from .harness import ROOT, Server, check, make_body, skip, sup


def _method_call_sites(tree, attr):
    """(enclosing function, lineno) for every `x.<attr>(...)`, against the innermost def."""
    sites = []

    def walk(node, fname):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call) and getattr(child.func, "attr", None) == attr:
                sites.append((fname, child.lineno))
            inner = (child.name
                     if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fname)
            walk(child, inner)

    walk(tree, "<module>")
    return sites


def _lister(path):
    return f"import os\nprint('SAW:' + ','.join(sorted(os.listdir({path!r}))))\n"


def _writer(path, name):
    return (f"import os\nopen(os.path.join({path!r}, {name!r}), 'w').write('x')\n"
            + _lister(path))


def _saw(body):
    for line in (body or {}).get("output", "").splitlines():
        if line.startswith("SAW:"):
            return [n for n in line[4:].split(",") if n]
    return None


def test_shared_tmp_wipe(tmp):
    fake_tmp = os.path.join(tmp, "fake-tmp")
    fake_shm = os.path.join(tmp, "fake-shm")
    absent = os.path.join(tmp, "no-such-mount")
    os.makedirs(fake_tmp)
    os.makedirs(fake_shm)

    # every entry shape the mount can hold: a plain file, a populated directory, a dangling
    # symlink (which os.path.isdir answers False for and rmtree would refuse)
    open(os.path.join(fake_tmp, "leftover"), "w").write("previous tenant")
    os.makedirs(os.path.join(fake_tmp, "matplotlib-ni8dz5ng"))
    open(os.path.join(fake_tmp, "matplotlib-ni8dz5ng", "fontlist.json"), "w").write("{}")
    os.symlink(os.path.join(tmp, "gone"), os.path.join(fake_tmp, "dangling"))
    open(os.path.join(fake_shm, "sem.mp-abcdef"), "w").write("")

    removed = sup.wipe_shared_tmpfs((fake_tmp, fake_shm, absent))
    check("tmp wipe: empties both mounts",
          os.listdir(fake_tmp) == [] and os.listdir(fake_shm) == [], f"removed {removed}")
    check("tmp wipe: keeps the mount points themselves",
          os.path.isdir(fake_tmp) and os.path.isdir(fake_shm))
    check("tmp wipe: a path the runtime did not supply is not an error",
          not os.path.exists(absent))
    check("tmp wipe: reports what it removed", len(removed) == 4, f"got {removed}")

    # the refusal: a root holding the supervisor's own executions is skipped whole, because a
    # wipe that deletes live and retained executions is worse than no wipe at all
    open(os.path.join(fake_tmp, "keep-me"), "w").write("")
    under = os.path.join(fake_tmp, "scratch-lives-here")
    os.makedirs(under)
    removed = sup.wipe_shared_tmpfs((fake_tmp, fake_shm), protect=under)
    check("tmp wipe: a root containing the scratch root is skipped, not narrowed",
          removed == [] and sorted(os.listdir(fake_tmp)) == ["keep-me", "scratch-lives-here"],
          f"got {removed} / {sorted(os.listdir(fake_tmp))}")
    sup.wipe_shared_tmpfs((fake_tmp, fake_shm))

    # the failure path, driven as the failure: this is the one condition here whose policy is to
    # CONTINUE, so the record is the whole of the mitigation and it has to land on a stream the
    # alerter fetches. A read-only parent makes unlink fail without making the directory
    # unlistable, which is the shape a real undeletable entry has.
    if os.geteuid() == 0:
        skip("tmp wipe: a survivor is reported on stderr",
             "running as root, which unlinks straight through a read-only parent")
    else:
        open(os.path.join(fake_tmp, "undeletable"), "w").write("")
        os.chmod(fake_tmp, 0o555)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                removed = sup.wipe_shared_tmpfs((fake_tmp,))
        finally:
            os.chmod(fake_tmp, 0o755)
        check("tmp wipe: a survivor is reported on STDERR, the only stream GKE grades at or "
              "above the alerter's WARNING floor — the same line through LOG would be filed "
              "INFO on stdout and never fetched, which is what makes continuing defensible",
              removed == [] and "survived the wipe" in err.getvalue()
              and "undeletable" in err.getvalue()
              and "survived the wipe" not in out.getvalue(),
              f"removed={removed} stderr={err.getvalue()!r} stdout={out.getvalue()!r}")
        os.unlink(os.path.join(fake_tmp, "undeletable"))

    # -- the same property across a real execution boundary ----------------------------------
    root = os.path.join(tmp, "tmpwipe-scratch")
    os.makedirs(root)
    real_paths = sup.SHARED_TMPFS_PATHS
    sup.SHARED_TMPFS_PATHS = (fake_tmp, fake_shm)
    server = Server(root)
    try:
        open(os.path.join(fake_tmp, "planted"), "w").write("from before the pod served anyone")
        status, _, body = server.request(
            "POST", "/execute", make_body(code=_writer(fake_tmp, "written-by-a")))
        check("tmp wipe: an execution starts with the mount empty",
              status == 200 and _saw(body) == ["written-by-a"], f"got {status} {_saw(body)}")

        status, _, body = server.request(
            "POST", "/execute", make_body(code=_lister(fake_tmp)))
        check("tmp wipe: the next execution cannot see what the last one wrote",
              status == 200 and _saw(body) == [], f"got {status} {_saw(body)}")

        # the control: without the wipe the same probe must go red, or it proves nothing
        server.request("POST", "/execute", make_body(code=_writer(fake_tmp, "written-by-c")))
        real_wipe = sup.wipe_shared_tmpfs
        sup.wipe_shared_tmpfs = lambda *a, **kw: []
        try:
            status, _, body = server.request(
                "POST", "/execute", make_body(code=_lister(fake_tmp)))
        finally:
            sup.wipe_shared_tmpfs = real_wipe
        check("tmp wipe: CONTROL — with the wipe removed the leak is observable",
              status == 200 and _saw(body) == ["written-by-c"], f"got {status} {_saw(body)}")
    finally:
        server.close()
        sup.SHARED_TMPFS_PATHS = real_paths

    # The wipe is in _execute_inner, one level ABOVE the structural chokepoint:
    # ForkServer.fork_child is the single sender of FS_OP_FORK and carries neither the wipe nor
    # an assertion that one ran. Everything above drives the single fork path that exists today,
    # so none of it can see a SECOND one being added — this is what goes red instead of the leak
    # coming back silently. Same shape and same reason as the reaper group's assertion that
    # main() installs the orphan reaper.
    tree = ast.parse(
        open(os.path.join(ROOT, "sandbox", "supervisor.py"), encoding="utf-8").read())
    callers = _method_call_sites(tree, "fork_child")
    check("tmp wipe: fork_child has exactly one caller and it is _execute_inner, so there is no "
          "second fork path for the wipe to miss",
          [name for name, _ in callers] == ["_execute_inner"], f"callers: {callers}")
    inner = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_execute_inner")
    check("tmp wipe: and _execute_inner is the function that calls the wipe",
          any(isinstance(n, ast.Call) and getattr(n.func, "id", None) == "wipe_shared_tmpfs"
              for n in ast.walk(inner)),
          "_execute_inner never calls wipe_shared_tmpfs")
