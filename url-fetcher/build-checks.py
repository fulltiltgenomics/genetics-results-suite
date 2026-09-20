"""Build-time assertions for the url-fetcher image. Failing any one fails the build.

These run in the BUILDER stage but assert properties of the FINAL image, which is distroless
and has no shell: they read the build context at /build/context and the final stage's rootfs
at /dl, and they parse the Dockerfile rather than trusting a second copy of what it says. Same
pattern, and the same reasons, as sandbox/build-checks.py.

Two of the checks are the whole reason this file exists. `docs/code-execution-security.md`
states that the permissive guard construction is unreachable from a manifest, and that rests on
devserver.py not being in the image and on the entrypoint naming the production module rather
than being a bare interpreter. Both were sentences in a docstring until this ran.
"""

import ast
import json
import os
import re
import shlex
import ssl
import subprocess
import sys

CONTEXT = "/build/context"
DL = "/dl"
DOCKERFILE = os.path.join(CONTEXT, "Dockerfile")
APP = "/app"
# the modules the image is allowed to carry, and the one it must not
PRODUCTION_MODULES = {"guard.py", "fetch.py", "server.py", "main.py"}
TEST_ENTRYPOINT = "devserver.py"
ENTRYPOINT = ["/usr/bin/python3", "/app/main.py"]

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


def instructions(stage="final"):
    """[(verb, argument)] for the Dockerfile, continuations joined.

    `stage="final"` keeps only what follows the last FROM, which is what decides the shipped
    image; an earlier stage's COPY or ENTRYPOINT says nothing about it.
    """
    joined, buf = [], ""
    for raw in open(DOCKERFILE):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        buf += line[:-1] + " " if line.rstrip().endswith("\\") else line
        if not line.rstrip().endswith("\\"):
            joined.append(buf.strip())
            buf = ""
    out = []
    for line in joined:
        verb, _, arg = line.partition(" ")
        out.append((verb.upper(), arg.strip()))
    if stage == "final":
        last = max(i for i, (v, _) in enumerate(out) if v == "FROM")
        out = out[last:]
    return out


def source_files():
    """Every .py in the build context, as {name: parsed module}."""
    out = {}
    for root, dirs, files in os.walk(CONTEXT):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                out[os.path.relpath(path, CONTEXT)] = ast.parse(open(path).read(), path)
    return out


@check("the test entrypoint is not in the build context")
def _no_devserver():
    """url-fetcher/devserver.py is the only construction of a guard that permits loopback
    origins. .dockerignore excludes it; this is what makes that exclusion a property.

    Checked against the CONTEXT rather than the final rootfs on purpose: a file that reaches
    the builder is one COPY away from the image, and 'the Dockerfile happens not to copy it
    today' is not the claim the design document makes."""
    found = [p for p in source_files() if os.path.basename(p) == TEST_ENTRYPOINT]
    assert not found, (
        f"{found} reached the build context. url-fetcher/.dockerignore must exclude "
        f"{TEST_ENTRYPOINT}: it constructs the guard that permits loopback origins, and "
        "docs/code-execution-security.md states that construction is unreachable from a pod"
    )


@check("nothing in the context constructs a permissive guard")
def _no_permissive_construction():
    """The allowance is a constructor argument and the default is False. This asserts both
    halves for the code that ships: no call passes it as anything but a literal False, and
    Guard's own default has not been flipped — a default of True would make every check above
    beside the point."""
    for name, tree in source_files().items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "allow_loopback_origin":
                    continue
                assert isinstance(kw.value, ast.Constant) and kw.value.value is False, (
                    f"{name}:{node.lineno} passes allow_loopback_origin="
                    f"{ast.unparse(kw.value)}; only the test entrypoint may do that, and it "
                    "is not in the image"
                )
    guard = source_files()["guard.py"]
    for node in ast.walk(guard):
        if isinstance(node, ast.FunctionDef) and node.name == "__init__":
            for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
                if arg.arg == "allow_loopback_origin":
                    assert isinstance(default, ast.Constant) and default.value is False, (
                        f"guard.py:{node.lineno} defaults allow_loopback_origin to "
                        f"{ast.unparse(default)}"
                    )
                    return
    raise AssertionError("guard.py has no allow_loopback_origin keyword-only argument; the "
                         "allowance moved and this check no longer asserts anything")


@check("the final stage ships exactly the production modules")
def _shipped_set():
    """Read off the Dockerfile's own COPY lines, so adding a module to the image without
    adding it here fails the build instead of arriving unexamined."""
    shipped = {}
    for verb, arg in instructions():
        if verb != "COPY":
            continue
        parts = shlex.split(arg)
        flags = [p for p in parts if p.startswith("--")]
        parts = [p for p in parts if not p.startswith("--")]
        dest, srcs = parts[-1], parts[:-1]
        assert flags == ["--from=builder"], (
            f"COPY {arg}: the final stage must take every file from the builder stage. A COPY "
            "straight from the context is not a dependency, and BuildKit prunes a stage "
            "nothing depends on — the checks in this file would then be skipped in silence"
        )
        for src in srcs:
            assert src.startswith(CONTEXT), f"COPY {arg}: {src} is not the checked context"
            shipped[os.path.basename(src)] = dest
    assert set(shipped) == PRODUCTION_MODULES, (
        f"the final stage copies {sorted(shipped)}, not {sorted(PRODUCTION_MODULES)}"
    )
    stray = {n: d for n, d in shipped.items() if not d.startswith(APP)}
    assert not stray, f"copied outside {APP}: {stray}"


@check("the entrypoint is the production entrypoint, in exec form, with no CMD")
def _entrypoint():
    """`args:` in a manifest replaces CMD and leaves ENTRYPOINT alone. With the program named
    here, an `args:` can only append argv that main.py defines no flags for; with a bare
    interpreter (sandbox/Dockerfile's shape, for its own reasons) an `args:` would choose what
    runs, which is precisely the choice this design takes away from the manifest.

    Exec form matters too: a shell-form ENTRYPOINT is run through /bin/sh, which this image
    does not have."""
    entries = [arg for verb, arg in instructions() if verb == "ENTRYPOINT"]
    cmds = [arg for verb, arg in instructions() if verb == "CMD"]
    assert not cmds, f"the final stage declares CMD {cmds}; only ENTRYPOINT decides here"
    assert len(entries) == 1, f"expected one ENTRYPOINT in the final stage, found {entries}"
    try:
        argv = json.loads(entries[0])
    except ValueError:
        raise AssertionError(f"ENTRYPOINT {entries[0]} is not exec form (JSON array); shell "
                             "form needs a /bin/sh this image does not ship") from None
    assert argv == ENTRYPOINT, f"ENTRYPOINT is {argv}, expected {ENTRYPOINT}"


@check("the shipped entrypoint's startup tripwire passes")
def _tripwire():
    """main.py refuses to start unless the ADDRESS-CLASS policy is what rejects 127.0.0.1,
    169.254.169.254 and 10.0.0.1. Running it here turns 'the pod CrashLoopBackOffs' into 'the
    image does not build', against the same files the final stage copies. It dials nothing:
    every probe is an IP literal, refused before any resolution."""
    probe = (
        "import sys; sys.path.insert(0, %r);"
        "import main;"
        "main.assert_deployed_configuration(main.Guard(main.allowed_hosts()))" % CONTEXT
    )
    r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout + r.stderr).strip()


@check("the base image carries a CA bundle the standard library will load")
def _ca_bundle():
    """The fetcher is pure stdlib, so TLS verification is ssl.create_default_context() against
    the base image's bundle. A distroless base WITHOUT ca-certificates builds, starts, serves
    /healthz and fails every fetch at the handshake — the one failure shape no offline check
    would ever see. The path is derived from the interpreter rather than typed here, and the
    bundle is actually loaded rather than merely existing, because an empty or truncated file
    passes an existence check and verifies nothing."""
    env = {}
    for verb, arg in instructions():
        if verb == "ENV":
            for token in shlex.split(arg):
                name, _, value = token.partition("=")
                env[name] = value
    cafile = env.get("SSL_CERT_FILE")
    assert cafile, (
        "the final stage sets no SSL_CERT_FILE. Deriving the path from the BUILDER's "
        "interpreter is wrong on both counts — a different rootfs and a different environment "
        "— so the Dockerfile has to name the path this check and the shipped interpreter both "
        "read"
    )
    staged = os.path.join(DL, cafile.lstrip("/"))
    assert os.path.exists(staged), (
        f"the base image has no {cafile}: it ships no ca-certificates, so every https fetch "
        "fails at the TLS handshake. Pick a base that carries one, or point SSL_CERT_FILE at "
        "where this one keeps it"
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=staged)
    loaded = ctx.cert_store_stats()["x509"]
    assert loaded > 50, f"{cafile} loaded only {loaded} certificates"
    print(f"       {cafile}: {loaded} CA certificates, {os.path.getsize(staged)} bytes")


@check("no shell or package manager in the final rootfs")
def _no_shell():
    """docs/code-execution-security.md says this pod has 'no shell' in the same breath as 'no
    secret'. Both are claims about what an RCE in the route gets to work with."""
    banned = {
        "sh", "bash", "dash", "ash", "busybox", "zsh", "ksh",
        "apt", "apt-get", "dpkg", "rpm", "yum", "apk",
        "curl", "wget", "nc", "ncat", "ssh", "scp",
        "pip", "easy_install",
    }
    banned_re = re.compile(r"^(pip3(\.\d+)?|easy_install(-\d+(\.\d+)?)?)$")
    found = []
    for root, _dirs, files in os.walk(DL):
        if root.startswith(os.path.join(DL, "proc")):
            continue
        for f in files:
            if f in banned or banned_re.match(f):
                found.append(os.path.join(root, f))
    assert not found, f"found {found}"


print(f"url-fetcher build checks: {len(failures)} failed" if failures
      else "url-fetcher build checks: all passed")
sys.exit(1 if failures else 0)
