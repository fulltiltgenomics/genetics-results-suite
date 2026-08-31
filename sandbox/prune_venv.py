"""Prune the builder's venv down to what the final image is allowed to contain.

Two independent removals, both of which the final image copies verbatim if they are
left in place (`COPY --from=builder /opt/venv /opt/venv`):

1. **Packaging tooling.** `python -m venv` seeds pip and setuptools; the SDK wheel
   build seeds nothing else. `docs/code-execution-security.md` claims no package
   manager in the final image, and until this ran that claim was false —
   `python3 -m pip install` worked inside the sandbox against whatever egress the
   NetworkPolicy allows. Nothing in the final image needs them: the entrypoint is the
   distroless interpreter with PYTHONPATH pointing at site-packages, never
   `/opt/venv/bin/python3` (a symlink to the builder's interpreter, which does not
   exist there anyway).

2. **genetics_mcp_server modules outside the SDK's import closure.** `pip install
   --no-deps` installs the whole distribution — chat_api, llm_service, mcp_server,
   auth/, routers/, db/, skills/, scripts/ — 40-odd modules the sandbox never imports.
   They are unimportable there for want of fastapi and anthropic, but *source* is the
   asset: `auth/core.py` is the identity model of every service in the suite and
   `tools/executor.py` is a map of the SQL interpolation sites in the one backend the
   sandbox may talk to. A prompt-injected script reads files; it does not need them to
   import.

SDK_ALLOWLIST is the closure, determined empirically (import the SDK, enumerate
`sys.modules`) rather than by reading imports — see build-checks.py, which asserts the
surviving set equals it exactly so the list grows deliberately.
"""

import os
import shutil
import sys

VENV = "/opt/venv"
SITE = os.path.join(VENV, "lib/python3.11/site-packages")
PKG = os.path.join(SITE, "genetics_mcp_server")

# The import closure of `genetics_mcp_server.sdk`, as module paths relative to the
# genetics_mcp_server package root. config/settings.py used to be in it — sdk/client.py
# imports tools.executor, whose module-level `from genetics_mcp_server.tools.uniprot
# import UniProtClient` pulled `from genetics_mcp_server.config.settings import
# Settings` — so the image shipped a file naming every internal env var of the suite.
# genetics-results-suite-l41 cut that in genetics-mcp-server: the Settings import in
# uniprot.py is now behind `if TYPE_CHECKING`, and ToolExecutor resolves settings at
# first use (falling back to Settings' defaults when the module is absent, which is
# exactly this install) rather than in __init__. That repo's
# tests/test_sdk_import_closure.py pins the closure so it cannot regrow silently.
#
# tools/definitions.py went the same way in genetics-results-suite-6bv, and for the same
# reason one module along: `tools/__init__.py` re-exported it eagerly, so importing
# tools.executor imported it, and its module-level `from pydantic import Field` broke the
# image's own `import genetics_mcp_server.sdk` check the moment definitions.py grew that
# import. The re-export is now a module `__getattr__`. Cutting it also stops the image
# shipping the 2600-line catalogue of every tool the suite exposes.
#
# tools/executor.py remains: sdk/client.py imports ToolExecutor directly and every SDK
# method delegates to it. Its SQL-building methods therefore still ship, guarded by
# tools/sql_safety.py — see docs/code-execution-security.md, "Handoffs to other tasks".
SDK_ALLOWLIST = frozenset(
    {
        "__init__.py",
        "sdk/__init__.py",
        "sdk/_runner.py",
        "sdk/client.py",
        "sdk/errors.py",
        "tools/__init__.py",
        "tools/executor.py",
        "tools/phewas_categories.py",
        "tools/sql_safety.py",
        "tools/uniprot.py",
    }
)

# seeded by `python -m venv` and by the wheel build; nothing in the final image imports
# them, and `pip` in particular is a package manager the security doc says is absent
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
    # console scripts, including pip/pip3/pip3.11 and the wheels' own CLIs; the final
    # image runs /usr/bin/python3 against site-packages and never enters this directory
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
