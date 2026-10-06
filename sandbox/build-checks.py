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
from prune_venv import (
    SDK_ALLOWLIST,  # noqa: E402  single source of truth for the closure
)

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
    """Section 2: the image must actually run the analysis stack it ships, as shipped. A
    missing genetics_mcp_server.sdk means the SDK install has not landed in the branch being
    built — the image is not shippable without it."""
    mods = ["numpy", "scipy.stats", "polars", "matplotlib.pyplot", "httpx", "pypdf",
            "PIL.Image", "genetics_mcp_server.sdk"]
    env = dict(PY_ENV, MPLBACKEND="Agg", MPLCONFIGDIR="/tmp/mplcheck")
    for m in mods:
        r = subprocess.run(
            PY + ["-c", f"import {m}"], capture_output=True, env=env, text=True,
        )
        assert r.returncode == 0, f"import {m} failed: {r.stderr.strip().splitlines()[-1:]}"


@check("a wide frame prints every column under the image's polars settings")
def _polars_display():
    """polars renders for a console it cannot see: stdout here is the supervisor's pipe, and
    the default POLARS_FMT_MAX_COLS=8 drops the MIDDLE columns of a wider frame into an
    ellipsis column. The failure is silent in the only way that matters — the value is in the
    frame, absent from the print — and the knobs a script reaches for first
    (tbl_width_chars, fmt_str_lengths) do not govern column count, so a script that notices
    pays for a second full run to see what it already fetched.

    Asserted against the FINAL stage's ENV (which is why the Dockerfile is staged) and then
    exercised, because a variable that is set but misspelled is worth nothing: POLARS_TABLE_WIDTH
    is the width knob, and POLARS_FMT_TABLE_WIDTH_CHARS — the name that matches the Python
    setter — is silently ignored."""
    lines = open(DOCKERFILE).read().splitlines()
    last_from = max(i for i, l in enumerate(lines) if l.startswith("FROM "))
    final = "\n".join(lines[last_from:])
    settings = {}
    for var in ("POLARS_FMT_MAX_COLS", "POLARS_FMT_MAX_ROWS", "POLARS_FMT_STR_LEN",
                "POLARS_TABLE_WIDTH"):
        m = re.search(rf"^\s*(ENV\s+)?{var}=(\S+)", final, re.M)
        assert m, f"the final stage does not set {var}"
        settings[var] = m.group(2).strip('"').strip("'").rstrip("\\")
    assert settings["POLARS_FMT_MAX_COLS"] == "-1", (
        f"POLARS_FMT_MAX_COLS={settings['POLARS_FMT_MAX_COLS']!r}; anything but -1 elides "
        "the middle columns of a wide frame"
    )
    rows = int(settings["POLARS_FMT_MAX_ROWS"])
    assert 0 < rows <= 200, (
        f"POLARS_FMT_MAX_ROWS={rows}; unbounded or very large trades the column elision this "
        "check exists for against the supervisor's 64 KiB stdout window"
    )
    probe = (
        "import polars as pl;"
        "df = pl.DataFrame({f'c{i}': ['x'] for i in range(12)});"
        "out = str(df);"
        "assert all(f'c{i}' in out for i in range(12)), out;"
        "assert '\u2026' not in out, out;"
        "print('ok')"
    )
    r = subprocess.run(
        PY + ["-c", probe], capture_output=True, text=True,
        env=dict(PY_ENV, **settings),
    )
    assert r.returncode == 0, (
        "a 12-column frame did not print in full under the image's polars settings: "
        f"{r.stderr.strip().splitlines()[-1:] or r.stdout.strip()}"
    )


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


@check("the baked rc sets render density and imposes no style")
def _house_style():
    """Both halves are asserted, and the second is the one that would regress silently.

    The density has to be in effect for a script that never mentions it, so this imports
    matplotlib the way the supervisor does — MPLCONFIGDIR at the baked cache — and asks the
    resolved rcParams rather than reading the file back, which would only prove it exists.

    The style has to be ABSENT. scienceplots stays installed and a script may ask for it, but
    imposing it made some figures worse; two keys it would have set are checked against
    matplotlib's own defaults so that re-baking a style into the image fails here rather than
    quietly changing every figure. text.usetex is checked for the separate reason that this
    image is distroless: a True raises at draw time in every execution that plots."""
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
    assert got["dpi"] == 200, f"savefig.dpi {got['dpi']} is not the baked density"
    # matplotlib's own defaults. science.mplstyle sets these to 0.5 and "in", so a style baked
    # back into the rc fails here instead of silently restyling every figure in the image.
    assert got["linewidth"] == 0.8, (
        f"axes.linewidth {got['linewidth']} is not matplotlib's default — a style is baked in"
    )
    assert got["direction"] == "out", (
        f"xtick.direction {got['direction']!r} is not matplotlib's default — a style is baked in"
    )


@check("the scienceplots style names resolve for a script that asks for them by name")
def _style_names_registered():
    """This is now the ONLY way scienceplots reaches a figure, which makes it load-bearing
    rather than a convenience. `plt.style.use("science")` is what a model writes from memory
    and it raises OSError unless scienceplots has been imported; prewarm.py imports it in the
    supervisor before the first fork so every child inherits the registration. Assert the
    import works at all in the final layout, since prewarm treats a failure as a crash-loop."""
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


# The gene-set bundle as the final stage copies it: stage-gene-sets.py filter's output. The
# filter drops what the allowlist excludes; these assert the RESULT, against the build's own
# datasets.yaml and build args, so a filter bug or a leftover bundle from another profile fails
# here rather than shipping. Every check re-reads, so a bundle that fails to load fails each.
GENESETS = os.path.join(OUT, "genesets")
GENESETS_YAML = "/build/genesets-tools/datasets.yaml"
GENESETS_PYYAML = "/build/pyyaml"
GENESETS_BANNED = ("KEGG", "BIOCARTA")


def _genesets():
    try:
        import polars as pl
        if GENESETS_PYYAML not in sys.path:
            sys.path.insert(0, GENESETS_PYYAML)
        import yaml
        with open(os.path.join(GENESETS, "manifest.json")) as fh:
            manifest = json.load(fh)
        with open(GENESETS_YAML) as fh:
            config = yaml.safe_load(fh)
        sets = pl.read_parquet(os.path.join(GENESETS, "sets.parquet"))
        members = pl.read_parquet(os.path.join(GENESETS, "members.parquet"))
    except Exception as exc:
        raise AssertionError(f"the staged gene-set bundle does not load: {type(exc).__name__}: {exc}")
    return manifest, config, sets, members


@check("the gene-set bundle was staged for this build's profile and release")
def _genesets_profile():
    """The profile the filter recorded has to be the one this build was invoked for, and the
    release the one this build's datasets.yaml pins and the image's LABEL names. Both sides
    come from the same build arg in the same stage, so this only catches a filter bug or a
    stale build context; the real guard against shipping unlicensed sets is the in-builder
    filter plus _genesets_licences."""
    manifest, config, _sets, _members = _genesets()
    profile = os.environ.get("GENESETS_PROFILE", "")
    assert profile, "build arg GENESETS_PROFILE is empty"
    staged = (manifest.get("staging") or {}).get("profile")
    assert staged == profile, f"bundle was filtered for profile {staged!r}, the build is {profile!r}"
    pin = (config.get("gene_sets") or {}).get("release")
    got = (manifest.get("bundle") or {}).get("release_id")
    label = os.environ.get("GENESETS_RELEASE", "")
    assert got == pin == label, (
        f"bundle release {got!r}, datasets.yaml pins {pin!r}, LABEL build arg {label!r}"
    )


@check("every gene-set collection is licensed for this profile, once")
def _genesets_licences():
    """The licence gate itself: each collection the image serves carries an SPDX id in
    profiles.<profile>.gene_set_licences of this build's datasets.yaml. A collection in the
    data without a manifest entry has no licence on record at all, and one listed twice has
    two, of which only one would have been checked."""
    manifest, config, sets, _members = _genesets()
    profile = os.environ.get("GENESETS_PROFILE", "")
    allowed = ((config.get("profiles") or {}).get(profile) or {}).get("gene_set_licences") or []
    assert allowed, f"profiles.{profile}.gene_set_licences is empty or absent"
    names = [c.get("collection") for c in manifest.get("collections") or []]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"listed more than once in the manifest: {dupes}"
    bad = [(c.get("collection"), c.get("spdx_license")) for c in manifest["collections"]
           if c.get("spdx_license") not in allowed]
    assert not bad, f"licence not in profiles.{profile}.gene_set_licences {allowed}: {bad}"
    unlisted = sorted(set(sets["collection"].to_list()) - set(names))
    assert not unlisted, f"collections in sets.parquet with no manifest entry: {unlisted}"


@check("every gene-set member belongs to a kept set")
def _genesets_members():
    """members.parquet has no collection column, so a member row of a dropped set is
    unlicensed content that no collection-level check can see."""
    _manifest, _config, sets, members = _genesets()
    orphans = set(members["set_id"].to_list()) - set(sets["set_id"].to_list())
    assert not orphans, f"{len(orphans)} member set_id(s) not in sets.parquet: {sorted(orphans)[:5]}"


@check("no KEGG or BioCarta gene set")
def _genesets_no_kegg():
    """KEGG needs a commercial licence for non-academic use and BioCarta's permission is
    qualified, so neither may ship whatever licence a manifest claims for its collection.
    Matched after dropping case and punctuation, on set_id, name and collection alike."""
    _manifest, _config, sets, _members = _genesets()
    hits = []
    for col in ("set_id", "name", "collection"):
        for value in sets[col].unique().to_list():
            norm = re.sub(r"[^A-Z0-9]", "", str(value).upper())
            if any(b in norm for b in GENESETS_BANNED):
                hits.append(f"{col}={value!r}")
    assert not hits, f"{len(hits)} match(es): {hits[:5]}"


@check("the staged gene-set files are exactly those the manifest lists, unmodified")
def _genesets_checksums():
    """The filter rewrites the manifest for what it rewrote; anything else in the directory,
    or any file that differs from its entry, is content no check above has vouched for."""
    import hashlib
    manifest, _config, _sets, _members = _genesets()
    files = (manifest.get("bundle") or {}).get("files") or {}
    present = set(os.listdir(GENESETS)) - {"manifest.json"}
    assert present == set(files), f"on disk {sorted(present)}, in the manifest {sorted(files)}"
    for name, meta in sorted(files.items()):
        path = os.path.join(GENESETS, name)
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        assert os.path.getsize(path) == meta.get("bytes"), f"{name}: size differs from the manifest"
        assert h.hexdigest() == meta.get("sha256"), f"{name}: sha256 differs from the manifest"


@check("the gene-set bundle is non-empty and readable by the sandbox uid")
def _genesets_nonempty():
    """A licence filter that dropped everything exits 0 with an empty bundle, and the image
    would ship enrichment with nothing to enrich against. The final stage copies the tree
    root-owned, so the sandbox uid reads it through the world bits alone."""
    manifest, _config, sets, members = _genesets()
    assert manifest.get("collections"), "no collection survived the licence filter"
    assert sets.height and members.height, f"{sets.height} sets, {members.height} member rows"
    unreadable = []
    for root, dirs, files in os.walk(GENESETS):
        for d in dirs + [""]:
            if os.stat(os.path.join(root, d)).st_mode & 0o005 != 0o005:
                unreadable.append(os.path.join(root, d))
        for f in files:
            if not os.stat(os.path.join(root, f)).st_mode & 0o004:
                unreadable.append(os.path.join(root, f))
    assert not unreadable, f"not world-readable: {unreadable}"


@check("no test-fixture gene-set bundle")
def _genesets_not_fixture():
    """The placeholder gate's counterpart. The local fixture bundle carries `"fixture": true`
    at the top of its manifest; an image built from it works, deploys healthy and answers every
    enrichment against toy sets. Only a local build opts in, with GENESETS_ALLOW_FIXTURE=1."""
    manifest, _config, _sets, _members = _genesets()
    if manifest.get("fixture") in (None, False):
        return
    assert os.environ.get("GENESETS_ALLOW_FIXTURE") == "1", (
        f"the manifest says fixture={manifest['fixture']!r}; a test-fixture bundle is not "
        "shippable (GENESETS_ALLOW_FIXTURE=1 is for local builds only)"
    )


print(f"sandbox build checks: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
