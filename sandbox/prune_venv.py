"""Prune the builder's venv down to what the final image is allowed to contain.

Two independent removals, both of which the final image would copy verbatim:

1. Packaging tooling. `python -m venv` seeds pip and setuptools, and until this ran
   `python3 -m pip install` worked inside the sandbox against whatever egress the
   NetworkPolicy allows. Nothing in the final image needs them: the entrypoint is the
   distroless interpreter with PYTHONPATH at site-packages, never /opt/venv/bin/python3.

2. genetics_mcp_server modules outside the SDK's import closure. `pip install --no-deps`
   installs the whole distribution — 40-odd modules the sandbox never imports. They are
   unimportable there for want of fastapi and anthropic, but SOURCE is the asset:
   auth/core.py is the identity model of every service in the suite. A prompt-injected
   script reads files; it does not need them to import.

SDK_ALLOWLIST is the closure, determined empirically (import the SDK, enumerate sys.modules)
rather than by reading imports. build-checks.py asserts the surviving set equals it exactly,
so the list grows deliberately.
"""

import os
import shutil
import sys

VENV = "/opt/venv"
SITE = os.path.join(VENV, "lib/python3.11/site-packages")
PKG = os.path.join(SITE, "genetics_mcp_server")

# The import closure of `genetics_mcp_server.sdk`, as module paths relative to the
# genetics_mcp_server package root. The closure is what decides what this image ships, so a
# module that discloses more than an SDK caller needs — the suite's internal env vars, its
# whole tool catalogue, the run_analysis gateway with the identity it refuses to dispatch
# without and its artifact authorization model — is CUT OUT OF THE CLOSURE on the other side
# of the wire rather than shipped here and trusted. The cut is `if TYPE_CHECKING` plus a
# module `__getattr__` where a shipped module imported it eagerly, and a subclass where a
# shipped module was the base; either way nothing that ships names what was cut.
# genetics-mcp-server's tests/test_sdk_import_closure.py pins the closure so it cannot
# regrow silently.
#
# tools/executor.py remains: sdk/client.py imports ToolExecutor directly and every SDK method
# delegates to it, so its SQL-building methods ship, guarded by tools/sql_safety.py.
#
# The absences are asserted, not merely arranged. build-checks.py compares the surviving set
# against this one in BOTH directions, so a module that starts shipping fails the build; and
# because nothing in the closure names the cut modules, one re-merged into it fails the same
# build's `import genetics_mcp_server.sdk` instead.
SDK_ALLOWLIST = frozenset(
    {
        "__init__.py",
        "sdk/__init__.py",
        "sdk/_runner.py",
        "sdk/client.py",
        "sdk/errors.py",
        # NOT in the import closure — sdk/__init__.py resolves it through a module
        # __getattr__ so the servers never import matplotlib — so it survives on this list
        # alone. That is the one entry here whose absence would be a missing FEATURE rather
        # than a broken import: without it `genetics.plots` raises ModuleNotFoundError inside
        # the child, where the standard plots are the whole point of shipping it.
        "sdk/plots.py",
        "tools/__init__.py",
        "tools/executor.py",
        "tools/sql_safety.py",
        "tools/chembl.py",
        "tools/uniprot.py",
    }
)

# seeded by `python -m venv` and by the wheel build; nothing in the final image imports them
PACKAGING_DIRS = ("pip", "setuptools", "pkg_resources", "_distutils_hack", "wheel")
PACKAGING_FILES = ("distutils-precedence.pth",)


def prune_packaging():
    removed = []
    for name in os.listdir(SITE):
        low = name.lower()
        base = low.split("-", 1)[0]
        if base in PACKAGING_DIRS or low in PACKAGING_FILES:
            path = os.path.join(SITE, name)
            (shutil.rmtree if os.path.isdir(path) else os.remove)(path)
            removed.append(name)
    # console scripts, including pip; the final image runs /usr/bin/python3 against
    # site-packages and never enters this directory
    binp = os.path.join(VENV, "bin")
    if os.path.isdir(binp):
        removed.append("bin/ (%s)" % ", ".join(sorted(os.listdir(binp))))
        shutil.rmtree(binp)
    return removed


def prune_sdk():
    removed = []
    for root, dirs, files in os.walk(PKG, topdown=False):
        rel_root = os.path.relpath(root, PKG)
        for f in files:
            rel = f if rel_root == "." else os.path.join(rel_root, f)
            if rel not in SDK_ALLOWLIST:
                os.remove(os.path.join(root, f))
                removed.append(rel)
        if root != PKG and not os.listdir(root):
            os.rmdir(root)
    return removed


if __name__ == "__main__":
    assert os.path.isdir(PKG), f"{PKG} missing — the SDK install did not land"
    pkg_removed = prune_packaging()
    sdk_removed = prune_sdk()
    print(f"pruned packaging tooling: {', '.join(pkg_removed) or 'nothing'}")
    print(f"pruned {len(sdk_removed)} genetics_mcp_server file(s) outside the allow-list")
    survivors = set()
    for root, _dirs, files in os.walk(PKG):
        rel_root = os.path.relpath(root, PKG)
        for f in files:
            survivors.add(f if rel_root == "." else os.path.join(rel_root, f))
    missing = SDK_ALLOWLIST - survivors
    if missing:
        print(f"ERROR: allow-listed module(s) absent from the install: {sorted(missing)}")
        sys.exit(1)
