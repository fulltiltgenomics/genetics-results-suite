"""Build-time assertions for the sandbox image. Failing any one fails the build.

These run in the BUILDER stage but assert properties of the FINAL image, which is distroless
and has no shell — so each inspects the exact artefacts the final stage copies (the base
rootfs at /dl, the venv at /opt/venv, the passwd/group files at /out) rather than the
builder's own filesystem. Each docstring names the control it stands for, so a check that
starts failing can be judged rather than deleted.
"""

import ast
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from prune_venv import SDK_ALLOWLIST  # noqa: E402  single source of truth for the closure

DL = "/dl"
VENV = "/opt/venv"
SITE = os.path.join(VENV, "lib/python3.11/site-packages")
OUT = "/out"
DOCKERFILE = "/build/Dockerfile"

# The final image has no /opt/venv/bin: it runs the distroless interpreter against the venv's
# site-packages, so the probes must do the same or they test a configuration that is not
# shipped. -S keeps the BUILDER's own site-packages (which does have pip) off sys.path.
PY = ["python3", "-S"]
PY_ENV = dict(os.environ, PYTHONPATH=SITE)

failures = []


def check(name):
    def wrap(fn):
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            print(f"  FAIL {name}: {exc}")
            failures.append(name)

    return wrap


@check("nsswitch.conf lists files before dns")
def _nsswitch():
    """hostAliases only work if glibc consults `files` first. With no /etc/nsswitch.conf glibc
    defaults to `dns [!UNAVAIL=return] files`, and against an egress policy that drops 53/UDP
    every lookup stalls the full resolver timeout — a hang, not an error, inside the wall
    clock, and unfixable at runtime because the rootfs is read-only."""
    path = os.path.join(DL, "etc/nsswitch.conf")
    assert os.path.exists(path), "absent from the base image"
    line = None
    for raw in open(path):
        if raw.strip().startswith("hosts:"):
            line = raw.strip()
    assert line, "no hosts: line"
    sources = line.split(":", 1)[1].split()
    sources = [re.sub(r"\[.*", "", s) for s in sources if not s.startswith("[")]
    assert "files" in sources, f"`files` not listed: {line!r}"
    if "dns" in sources:
        assert sources.index("files") < sources.index("dns"), f"dns before files: {line!r}"


@check("no shell or package manager in the final rootfs")
def _no_shell():
    """No shell and no package manager on the filesystem, not merely un-allow-listed.

    Walks every tree the final stage copies, not just the distroless base: checking only /dl
    is what let `python3 -m pip install` work inside the sandbox unnoticed."""
    banned = {
        "sh", "bash", "dash", "ash", "busybox", "zsh", "ksh",
        "apt", "apt-get", "dpkg", "rpm", "yum", "apk",
        "curl", "wget", "nc", "ncat", "ssh", "scp",
        "pip", "pip3", "easy_install",
    }
    banned_re = re.compile(r"^(pip3(\.\d+)?|easy_install(-\d+(\.\d+)?)?)$")
    found = []
    for tree in (DL, VENV, OUT):
        for root, _dirs, files in os.walk(tree):
            if root.startswith(os.path.join(DL, "proc")):
                continue
            for f in files:
                if f in banned or banned_re.match(f):
                    found.append(os.path.join(root, f))
    assert not found, f"found {found}"


@check("no packaging tooling importable from the venv")
def _no_pip():
    """Same control as above, from the other side: `pip` and `setuptools` are
    importable as modules even with the console scripts gone, and `python3 -m pip`
    is the form a model-authored script would actually use."""
    for mod in ("pip", "setuptools", "pkg_resources"):
        r = subprocess.run(PY + ["-c", f"import {mod}"], capture_output=True, env=PY_ENV)
        assert r.returncode != 0, f"{mod} is importable from the venv"
    leftovers = [d for d in os.listdir(SITE)
                 if d.lower().split("-", 1)[0] in ("pip", "setuptools", "wheel")]
    assert not leftovers, f"dist-info/packages left in site-packages: {leftovers}"


@check("no google-auth-based client in the venv")
def _no_google_auth():
    """Section 3(c). Anything reaching google.auth.default() probes
    metadata.google.internal *by name*; with no DNS that is a multi-second stall, not
    the fast failure the egress design assumes."""
    banned = ("google-auth", "google_auth", "google-cloud", "google_cloud", "google-api")
    site = os.path.join(VENV, "lib")
    found = []
    for root, dirs, _files in os.walk(site):
        for d in dirs:
            low = d.lower()
            if any(low.startswith(b) for b in banned):
                found.append(os.path.join(root, d))
    assert not found, f"found {found}"
    probe = subprocess.run(PY + ["-c", "import google.auth"], capture_output=True, env=PY_ENV)
    assert probe.returncode != 0, "google.auth is importable"


@check("GCE metadata clients compiled into native code are pinned to a literal IP")
def _metadata_pinned():
    """The branch the distribution-name check above cannot see. polars links `object_store`, a
    Rust GCS/S3/Azure client that performs the metadata token request with no google-auth —
    no Python at all — in the path, resolving metadata.google.internal by name. The control
    actually needed is "nothing in this image resolves the metadata server by name", so scan
    the native objects and, where the capability is present, require GCE_METADATA_HOST pinned
    to a literal address in the FINAL stage's ENV (which is why the Dockerfile is staged)."""
    needles = (b"metadata.google.internal", b"computeMetadata")
    carriers = []
    for root, _dirs, files in os.walk(SITE):
        for f in files:
            if not f.endswith(".so"):
                continue
            path = os.path.join(root, f)
            with open(path, "rb") as fh:
                blob = fh.read()
            if any(n in blob for n in needles):
                carriers.append(os.path.relpath(path, SITE))
    if not carriers:
        return  # 3(c)'s preferred branch: the capability is simply absent
    lines = open(DOCKERFILE).read().splitlines()
    last_from = max(i for i, l in enumerate(lines) if l.startswith("FROM "))
    final = "\n".join(lines[last_from:])
    m = re.search(r"^\s*(ENV\s+)?GCE_METADATA_HOST=(\S+)", final, re.M)
    assert m, (
        f"{carriers} carry a GCE metadata client but the final stage does not set "
        "GCE_METADATA_HOST (docs/code-execution-security.md section 3(c))"
    )
    host = m.group(2).strip('"').strip("'").split(":")[0]
    assert re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host), (
        f"GCE_METADATA_HOST={host!r} is not a literal IPv4 — a name here is the "
        "resolver stall 3(c) exists to prevent"
    )


@check("only the SDK's import closure of genetics_mcp_server ships")
def _sdk_surface():
    """prune_venv.py cuts the installed distribution to the closure of
    `genetics_mcp_server.sdk`. The rest is unimportable in the sandbox for want of fastapi,
    but a prompt-injected script reads source, it does not import it. This asserts the
    surviving set EQUALS the allow-list, so a new SDK dependency has to be added deliberately
    rather than arriving with the next `pip install`."""
    pkg = os.path.join(SITE, "genetics_mcp_server")
    survivors = set()
    for root, _dirs, files in os.walk(pkg):
        rel_root = os.path.relpath(root, pkg)
        if "__pycache__" in rel_root.split(os.sep):
            continue
        for f in files:
            survivors.add(f if rel_root == "." else os.path.join(rel_root, f))
    extra = sorted(survivors - set(SDK_ALLOWLIST))
    missing = sorted(set(SDK_ALLOWLIST) - survivors)
    assert not extra, f"outside the allow-list: {extra}"
    assert not missing, f"allow-listed but absent: {missing}"


def _image_top_level_names():
    """Every top-level name importable from the venv, read off the install itself.

    Reading the directory rather than resolving requirements is what makes this answer for the
    IMAGE: pip has already evaluated the environment markers and the transitive pins by the time
    this runs, so nothing here has to re-derive `python_version < "3.11"` or guess what a
    developer's machine happens to have. It reads the venv the final stage copies, so anything
    an earlier step deleted — pip and setuptools among them — is absent from the answer too.
    """
    names = set()
    for entry in os.listdir(SITE):
        if entry.endswith((".dist-info", ".egg-info", ".pth", ".egg-link")):
            continue
        base = entry if os.path.isdir(os.path.join(SITE, entry)) else entry.split(".")[0]
        if base.isidentifier():
            names.add(base)
    return names


def _imported_top_level_names(source):
    """Top-level module names an import in `source` names, at ANY nesting depth.

    Relative imports resolve inside the package the prune already governs, so they are not this
    check's question.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [(alias.name.split(".")[0], node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            found.append(((node.module or "").split(".")[0], node.lineno))
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name in ("__import__", "import_module") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append((arg.value.split(".")[0], node.lineno))
    return [(n, line) for n, line in found if n]


@check("no shipped source names a module the image does not install")
def _shipped_imports():
    """The half of the closure that `import genetics_mcp_server.sdk` cannot reach.

    That import runs module bodies and nothing else, so a function-level `from ddgs import
    DDGS` in a shipped file survives every runtime check and raises ModuleNotFoundError at CALL
    time instead — inside a container with no shell and no package manager, where it is
    expensive rather than cheap. Deferring an import is also the house style for adding
    capability to these files, so the blind spot sits exactly where new code lands. Reading the
    source with `ast` is what closes it.

    `if TYPE_CHECKING` and `try/except ImportError` guards count too, for the reason the
    assertion below states; `typing.get_type_hints()` would resolve a TYPE_CHECKING import for
    real regardless of the guard.
    """
    allowed = set(sys.stdlib_module_names) | _image_top_level_names()
    # the alias is shipped source too; its presence is the alias check's assertion, not this one
    alias = os.path.join(SITE, "genetics.py")
    sources = [alias] if os.path.exists(alias) else []
    for root, _dirs, files in os.walk(os.path.join(SITE, "genetics_mcp_server")):
        if "__pycache__" in root.split(os.sep):
            continue
        sources += [os.path.join(root, f) for f in files if f.endswith(".py")]
    offenders = []
    for path in sorted(sources):
        with open(path) as fh:
            for name, line in _imported_top_level_names(fh.read()):
                if name not in allowed:
                    offenders.append(f"{os.path.relpath(path, SITE)}:{line} imports {name!r}")
    assert not offenders, (
        "the image ships these files, and they name modules it does not install. A "
        "`TYPE_CHECKING` or `try/except ImportError` guard does not exempt an import — the "
        "name is in the shipped source whether or not that line runs. Cut the code out of "
        "the SDK's import closure, or add the pin to requirements.txt deliberately:\n  "
        + "\n  ".join(offenders)
    )


@check("no placeholders in the staged schema and stubs")
def _no_placeholders():
    """`schema/` and `stubs/` are the model's description of the data and of the SDK, and
    shipping the placeholders degrades silently: run_analysis works, the pod is healthy, and
    the model reads a file that says it is not the real documentation."""
    found = []
    for tree in ("/build/schema", "/build/stubs"):
        for root, _dirs, files in os.walk(tree):
            for f in files:
                if f.startswith("PLACEHOLDER"):
                    found.append(os.path.join(root, f))
    assert not found, (
        f"{found} (schema docs and SDK stubs) has not "
        "landed; the image is not shippable with placeholder documentation"
    )


@check("analysis libraries and the genetics SDK import")
def _imports():
    """Section 2 (the image must actually run the analysis stack as shipped) and the
    A missing genetics_mcp_server.sdk means the SDK install has not
    landed in the branch being built — the image is not shippable without it."""
    mods = ["numpy", "scipy.stats", "polars", "matplotlib.pyplot", "httpx",
            "genetics_mcp_server.sdk"]
    env = dict(PY_ENV, MPLBACKEND="Agg", MPLCONFIGDIR="/tmp/mplcheck")
    for m in mods:
        r = subprocess.run(
            PY + ["-c", f"import {m}"], capture_output=True, env=env, text=True,
        )
        assert r.returncode == 0, f"import {m} failed: {r.stderr.strip().splitlines()[-1:]}"


@check("`import genetics` resolves to the SDK itself, not a copy of it")
def _sdk_alias():
    """Every doc a script's author can read names the package `genetics`; only the import path
    said `genetics_mcp_server.sdk`, and nothing reachable from inside an execution disclosed
    that, so sessions opened by probing for it.

    IDENTITY is what is asserted, not importability: a `from ... import *` alias would import
    fine while being a second module object with its own copy of the SDK's client state."""
    r = subprocess.run(
        PY + ["-c", "import genetics, genetics_mcp_server.sdk as s; "
                    "assert genetics is s, genetics; "
                    "from genetics import summary_stats; print(summary_stats.__module__)"],
        capture_output=True, env=PY_ENV, text=True,
    )
    assert r.returncode == 0, f"import genetics failed: {r.stderr.strip().splitlines()[-1:]}"
    assert r.stdout.strip().startswith("genetics_mcp_server.sdk"), r.stdout.strip()


@check("passwd carries the supervisor and child uids on a shared gid")
def _uids():
    """One shared uid: supervisor and child both run as 65532. A distinct child uid needs
    CAP_SETUID/CAP_SETGID/CAP_CHOWN, which this pod drops (setuid and chown were measured
    EPERM), so RLIMIT_NPROC is not a per-execution control and the token file stays within
    the child's same-uid reach. The `sandboxchild` 65533 entry is therefore ADVERTISED AND
    UNREACHABLE — this check keeps /out/passwd consistent with SANDBOX_CHILD_UID, but nothing
    can switch to that uid and the supervisor must not fork against it."""
    entries = {}
    for line in open("/out/passwd"):
        parts = line.strip().split(":")
        if len(parts) >= 4:
            entries[parts[0]] = (parts[2], parts[3])
    assert entries.get("nonroot") == ("65532", "65532"), f"nonroot: {entries.get('nonroot')}"
    assert entries.get("sandboxchild") == ("65533", "65532"), (
        f"sandboxchild: {entries.get('sandboxchild')}"
    )
    assert "root" in entries, "root entry lost"


@check("baked matplotlib font cache is present")
def _fontcache():
    """The font cache costs seconds to build. MPLCONFIGDIR is per-execution and
    writable-only under /scratch, so a cache baked at build time is the only way a
    cold pod's first execution does not pay for it. See prewarm.py."""
    files = os.listdir("/out/mplcache")
    assert any(f.startswith("fontlist-") for f in files), f"no fontlist in {files}"


@check("the house plot style is the resolved default, and needs no LaTeX")
def _house_style():
    """The style is REQUIRED, not offered, so what has to be asserted is that a figure drawn
    by a script that never mentions it comes out styled. Reading the rc file back would only
    prove the file exists; this imports matplotlib the way the supervisor does — with
    MPLCONFIGDIR at the baked cache — and asks the resolved rcParams.

    text.usetex is checked separately from the rest because it is the one key whose wrong
    value is not a cosmetic difference: this image is distroless, so a True here raises at
    draw time in every execution that plots."""
    probe = (
        "import matplotlib, json; "
        "print(json.dumps({"
        "'usetex': matplotlib.rcParams['text.usetex'],"
        "'linewidth': matplotlib.rcParams['axes.linewidth'],"
        "'direction': matplotlib.rcParams['xtick.direction'],"
        "'dpi': matplotlib.rcParams['savefig.dpi'],"
        "'rcfile': matplotlib.matplotlib_fname(),"
        "}))"
    )
    env = dict(PY_ENV, MPLCONFIGDIR="/out/mplcache", MPLBACKEND="Agg")
    r = subprocess.run(PY + ["-c", probe], capture_output=True, env=env, text=True)
    assert r.returncode == 0, f"matplotlib would not import under the baked config: {r.stderr}"
    got = json.loads(r.stdout)
    assert got["rcfile"] == "/out/mplcache/matplotlibrc", (
        f"matplotlib read {got['rcfile']!r}, not the generated one — the style is not in effect"
    )
    assert got["usetex"] is False, "text.usetex is on in an image with no LaTeX"
    # values that come from science.mplstyle and from nowhere else, so they fail if the rc is
    # present but empty or parsed to defaults
    assert got["linewidth"] == 0.5, f"axes.linewidth {got['linewidth']} is not the style's"
    assert got["direction"] == "in", f"xtick.direction {got['direction']!r} is not the style's"
    assert got["dpi"] == 200, f"savefig.dpi {got['dpi']} is not the local override's"


@check("the scienceplots style names resolve for a script that asks for them by name")
def _style_names_registered():
    """The rc above styles a figure with no cooperation from the script. This is the other
    half: `plt.style.use("science")` is what a model writes from memory, and it raises OSError
    unless scienceplots has been imported. prewarm.py imports it in the supervisor before the
    first fork so every child inherits the registration — assert the import works at all in
    the final layout, since prewarm treats a failure as a crash-loop."""
    probe = (
        "import matplotlib; matplotlib.use('Agg'); "
        "import scienceplots, matplotlib.pyplot as plt; "
        "plt.style.use(['science', 'no-latex']); "
        "assert plt.rcParams['text.usetex'] is False; "
        "print('ok')"
    )
    env = dict(PY_ENV, MPLCONFIGDIR="/out/mplcache", MPLBACKEND="Agg")
    r = subprocess.run(PY + ["-c", probe], capture_output=True, env=env, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "ok", (
        f"scienceplots styles do not resolve in the shipped layout: {r.stderr}"
    )


print(f"sandbox build checks: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
