# Sandbox image for model-authored Python. Design of record: docs/code-execution-security.md.
#
# The build needs a staged genetics-mcp-server checkout at ./.sdk-src — the genetics SDK is
# pip-installed from that repo and is not vendored here. scripts/build-all.sh and
# scripts/build.sh stage it; a bare `docker build sandbox/` fails without it, by design.

FROM gcr.io/distroless/python3-debian12:nonroot AS distroless

# The builder must track the base image's CPython MINOR version: the final stage runs the
# distroless interpreter (3.11) against the venv's site-packages, so a 3.12 venv would put
# cp312-tagged native wheels in front of a 3.11 interpreter and fail at import.
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python3 -m venv /opt/venv

COPY requirements.txt /build/requirements.txt
RUN /opt/venv/bin/pip install -r /build/requirements.txt

# The genetics SDK, --no-deps deliberately: genetics-mcp-server's dependency set contains
# google-auth (plus anthropic, openai, fastapi and the rest of the server), and
# google.auth.default() resolves metadata.google.internal by name where the sandbox has no
# DNS. The SDK's real runtime needs are pinned in requirements.txt instead, so the closure is
# declared rather than inherited. build-checks.py fails the build if google-auth reappears.
COPY .sdk-src/ /build/sdk-src/
RUN /opt/venv/bin/pip install --no-deps /build/sdk-src

# `import genetics`, the name every doc a script's author can read already uses. Placed
# before the prune and the compileall so it is bytecode-baked; prune_sdk walks only
# site-packages/genetics_mcp_server, so this file is outside its reach by construction.
COPY genetics_alias.py /opt/venv/lib/python3.11/site-packages/genetics.py

# Everything below runs the SYSTEM interpreter with PYTHONPATH at the venv's site-packages,
# because the next step deletes /opt/venv/bin — and because that is exactly how the final
# image runs, so the compile, the cache bake and the assertions exercise the shipped config.
ENV PYTHONPATH=/opt/venv/lib/python3.11/site-packages

# Reduce the venv to what the final image may contain: no pip/setuptools, and no
# genetics_mcp_server module outside the SDK's import closure — the rest is unimportable there
# but READABLE, and includes the suite's auth model. See prune_venv.py.
COPY prune_venv.py /build/prune_venv.py
RUN python3 /build/prune_venv.py

# Baked at build time because the runtime rootfs is read-only, so without this every import in
# every execution recompiles. After the prune, so no .pyc outlives the source it came from.
RUN python3 -m compileall -q /opt/venv/lib >/dev/null

# The matplotlib font cache costs seconds to build. Baked here and shipped read-only; the
# supervisor copies it into a per-execution writable MPLCONFIGDIR (see prewarm.py).
RUN mkdir -p /out/mplcache \
    && MPLBACKEND=Agg MPLCONFIGDIR=/out/mplcache \
       python3 -c "import matplotlib.font_manager"

# A second non-root uid the image ADVERTISES for the forked child. Nothing switches to it —
# the pod drops CAP_SETUID — so supervisor and child share uid 65532; see supervisor.py.
# Appended to the base image's own passwd/group so nothing distroless ships is lost.
COPY --from=distroless /etc/passwd /out/passwd
COPY --from=distroless /etc/group /out/group
RUN printf 'sandboxchild:x:65533:65532:sandbox child:/nonexistent:/sbin/nologin\n' >> /out/passwd

# Assert the final image's properties from HERE: the final stage has no shell, so nothing can
# be checked after it is assembled. /dl is the base rootfs verbatim, the Dockerfile is staged
# so the checks can read the final stage's ENV, and schema/ and stubs/ for the placeholder gate.
COPY --from=distroless / /dl/
COPY schema/ /build/schema/
COPY stubs/ /build/stubs/
COPY Dockerfile /build/Dockerfile
COPY build-checks.py /build/build-checks.py
RUN python3 /build/build-checks.py


FROM gcr.io/distroless/python3-debian12:nonroot

# The tag follows this repo's HEAD, but the SDK comes from genetics-mcp-server, so the tag
# alone does not identify the contents. The build scripts pass that repo's short sha here.
ARG SDK_REF=unknown
LABEL com.fulltiltgenomics.genetics-mcp-server-ref="${SDK_REF}"

COPY --from=builder /out/passwd /etc/passwd
COPY --from=builder /out/group /etc/group
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=65532:65532 /out/mplcache /genetics/mplcache
COPY --chown=65532:65532 prewarm.py /genetics/prewarm.py
# Shipping the supervisor does NOT make the image start one: there is still no CMD.
# k8s/deployments/sandbox.yaml names it in `args:` instead, which keeps a manifest that has
# lost its args a loud CrashLoopBackOff rather than a silently defaulted process.
COPY --chown=65532:65532 supervisor.py /genetics/supervisor.py
COPY --chown=65532:65532 schema/ /genetics/schema/
COPY --chown=65532:65532 stubs/ /genetics/sdk/

# The venv's bin/python3 symlinks the BUILDER's interpreter, which does not exist here, so the
# distroless one runs against the venv's site-packages — same minor version, so the cp311
# wheels and the baked .pyc files both match.
#
# GCE_METADATA_HOST is required rather than optional: polars links object_store, a Rust
# GCS/S3/Azure client that mints metadata tokens with no Python google-auth involved, so "no
# google-auth in the image" is true of the Python package name only. Pinning it to the literal
# link-local IP keeps metadata.google.internal out of the resolver, which matters because the
# sandbox has no DNS and a name lookup there is a multi-second stall inside the wall clock. It
# is not a confinement control: reaching the address at all is the NetworkPolicy's job.
ENV GCE_METADATA_HOST=169.254.169.254 \
    PYTHONPATH=/opt/venv/lib/python3.11/site-packages \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    MPLBACKEND=Agg \
    GENETICS_MPLCACHE=/genetics/mplcache \
    GENETICS_SCHEMA_DIR=/genetics/schema \
    GENETICS_STUBS_DIR=/genetics/sdk \
    GENETICS_PREWARM=/genetics/prewarm.py \
    SANDBOX_SUPERVISOR_UID=65532 \
    SANDBOX_CHILD_UID=65533 \
    SANDBOX_SHARED_GID=65532
# Deliberately NOT set here because they are per-execution: TMPDIR, HOME, MPLCONFIGDIR,
# XDG_CACHE_HOME and PYTHONPYCACHEPREFIX all point inside /scratch/<execution-id>. A fixed path
# would create exactly the cross-execution shared directory removing /tmp was meant to prevent.

USER 65532:65532
WORKDIR /genetics

# The bare interpreter, so the image is inspectable and the supervisor can be supplied as the
# command without rebuilding. Deliberately no CMD: a default would start SOMETHING for a
# manifest that has lost its args, where the intended failure is a loud CrashLoopBackOff.
ENTRYPOINT ["/usr/bin/python3"]
