"""Build-time assertions for the sandbox image. Failing any one fails the build.

These run in the *builder* stage but assert properties of the *final* image: the
final stage is distroless and has no shell, so nothing can be verified after it is
assembled. Each check therefore inspects the exact artefacts the final stage copies —
the distroless rootfs at /dl, the venv at /opt/venv, and the passwd/group files at
/out — rather than the builder's own filesystem.

Every check corresponds to a control in docs/code-execution-security.md; the
docstrings name which, so a check that starts failing can be judged rather than
deleted.
"""

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

# the final image has no /opt/venv/bin: it runs the distroless interpreter against the
# venv's site-packages, so the probes below must do the same or they test a
# configuration that is not shipped
# -S keeps the *builder's* own site-packages (which does have pip and setuptools) off
# sys.path, so what the probes see is stdlib + the venv — the final image's path exactly
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
    """Section 3(b). hostAliases only work if glibc consults `files` first. With no
    /etc/nsswitch.conf glibc defaults to `dns [!UNAVAIL=return] files`, and against an
    egress policy that drops 53/UDP every lookup stalls the full resolver timeout —
    a hang, not an error, inside a 60s wall clock, unfixable at runtime because the
    root filesystem is read-only."""
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
    """Section 2, base image. execute_script's `bash` interpreter must be absent from
    the filesystem, not merely un-allow-listed — and so must pip, which the security
    doc claims does not ship.

    Walks every tree the final stage copies, not just the distroless base: /dl is the
    base rootfs, /opt/venv and /out are `COPY --from=builder` sources. Checking only
    /dl is what let `python3 -m pip install` work inside the sandbox unnoticed."""
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
    """Section 3(c), the branch the distribution-name check above cannot see. polars
    links `object_store`, a Rust GCS/S3/Azure client: `pl.scan_parquet("gs://...")`
    performs the metadata token request with no google-auth, indeed no Python, in the
    path, and resolves `metadata.google.internal` by name on at least one of them. The
    control 3(c) actually needs is "nothing in this image resolves the metadata server
    by name", not "no python distribution called google-auth" — so scan the native
    objects, and where the capability is present require 3(c)'s second branch,
    `GCE_METADATA_HOST` pinned to a literal address in the *final* stage (the ENV a
    builder-stage check cannot otherwise observe; the Dockerfile is staged at /build
    for exactly this)."""
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
    """`pip install --no-deps` installs the whole distribution; prune_venv.py cuts it
    to the closure of `genetics_mcp_server.sdk`. The rest — auth/core.py's identity
    model, tools/executor.py's SQL, config/defaults.py, the routers and the db layer —
    is unimportable in the sandbox for want of fastapi, but a prompt-injected script
    reads source, it does not import it. This asserts the surviving set *equals* the
    allow-list so a new SDK dependency has to be added deliberately rather than
    arriving with the next `pip install`."""
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


@check("no 4h6.13 placeholders in the staged schema and stubs")
def _no_placeholders():
    """`schema/` and `stubs/` are the model's description of the data and of the SDK.
    Shipping the placeholders degrades silently — run_analysis works, the pod is
    healthy, and the model reads a file that says it is not the real documentation.
    Same reasoning as refusing to build without the SDK: an image asked for by name
    that quietly lacks its schema is worse than a build error. Couples this image to
    genetics-results-suite-4h6.13."""
    found = []
    for tree in ("/build/schema", "/build/stubs"):
        for root, _dirs, files in os.walk(tree):
            for f in files:
                if f.startswith("PLACEHOLDER"):
                    found.append(os.path.join(root, f))
    assert not found, (
        f"{found} — genetics-results-suite-4h6.13 (schema docs and SDK stubs) has not "
        "landed; the image is not shippable with placeholder documentation"
    )


@check("analysis libraries and the genetics SDK import")
def _imports():
    """Section 2 (the image must actually run the analysis stack as shipped) and the
    4h6.11 handoff. A missing genetics_mcp_server.sdk means the SDK task has not
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
    """genetics-results-suite-706. Every doc a script's author can read names the package
    `genetics`; only the import path said `genetics_mcp_server.sdk`, and nothing reachable
    from inside an execution disclosed that, so sessions opened by probing for it.

    IDENTITY is what is asserted, not merely importability. A `from ... import *` alias
    would import fine while being a second module object with its own copy of the SDK's
    client state, so `genetics.configure(...)` would configure something that the
    per-execution credential path never sees."""
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
    """Section 2's "The uid choice", DECIDED as option (b): ONE SHARED UID. The supervisor
    and the child both run as 65532. Option (a)'s distinct child uid needs
    CAP_SETUID/CAP_SETGID/CAP_CHOWN, which this pod drops — setuid(65533) and chown(65533)
    were measured to return EPERM — so RLIMIT_NPROC is not a per-execution control (the
    supervisor polices the child's process group instead) and the token file stays within the
    child's same-uid reach. The `sandboxchild` 65533 entry is therefore ADVERTISED AND
    UNREACHABLE: the image sets SANDBOX_CHILD_UID=65533 and this check keeps /out/passwd
    consistent with it, but nothing can switch to that uid and the supervisor must not fork
    against it. The gid both entries carry is the pod's single, shared gid 65532."""
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


print(f"sandbox build checks: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
