# Sandbox image for model-authored Python. Design of record:
# docs/code-execution-security.md (this image is genetics-results-suite-4h6.6).
#
# Build context is this directory, but the build additionally needs a staged
# genetics-mcp-server checkout at ./.sdk-src — the genetics SDK is pip-installed from
# that repo at build time and is not vendored here. scripts/build-all.sh and
# scripts/build.sh stage it from the clone they already make; a bare
# `docker build sandbox/` will fail without it, by design.

FROM gcr.io/distroless/python3-debian12:nonroot AS distroless

# DEVIATION from docs/code-execution-security.md section 2, which specifies a
# `python:3.12-slim` builder. distroless/python3-debian12 ships CPython **3.11**
# (bookworm's system python), and the final stage runs the distroless interpreter
# against the venv's site-packages. A 3.12 venv would put cp312-tagged native
# wheels — scipy, numpy, polars, matplotlib all ship them — in front of a 3.11
# interpreter, which fails at import. The builder must track the base image's minor
# version; the doc's intent (venv built in a slim stage, no build tooling in the
# final image) is preserved exactly.
FROM python:3.11-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python3 -m venv /opt/venv

COPY requirements.txt /build/requirements.txt
RUN /opt/venv/bin/pip install -r /build/requirements.txt

# The genetics SDK. Installed --no-deps deliberately: genetics-mcp-server's own
# dependency set contains google-auth (plus anthropic, openai, fastapi and the rest of
# the server), and section 3(c) forbids a google-auth-based client in this image
# because google.auth.default() resolves metadata.google.internal by name and the
# sandbox has no DNS. The SDK's real runtime needs — httpx, polars, matplotlib — are
# pinned in requirements.txt instead, so the closure is declared here rather than
# inherited. build-checks.py fails the build if google-auth reappears by any route.
COPY .sdk-src/ /build/sdk-src/
RUN /opt/venv/bin/pip install --no-deps /build/sdk-src

# Everything below runs the *system* interpreter with PYTHONPATH at the venv's
# site-packages rather than /opt/venv/bin/python3, because the next step deletes
# /opt/venv/bin. That is also exactly how the final image runs (same CPython minor
# version, same PYTHONPATH), so the compile, the font cache bake and the assertions all
# exercise the shipped configuration rather than a venv-only one.
ENV PYTHONPATH=/opt/venv/lib/python3.11/site-packages

# Reduce the venv to what the final image may contain: no pip/setuptools (the security
# doc claims no package manager ships, and until this ran `python3 -m pip install`
# worked inside the sandbox), and no genetics_mcp_server module outside the SDK's
# import closure (the rest is unimportable there but readable, and includes the auth
# model and the executor's SQL). The allow-list lives in prune_venv.py and is asserted
# by build-checks.py.
COPY prune_venv.py /build/prune_venv.py
RUN python3 /build/prune_venv.py

# Bytecode is baked at build time because the runtime root filesystem is read-only:
# without this every import in every execution recompiles from source. Runs after the
# prune so no .pyc outlives the source it came from.
RUN python3 -m compileall -q /opt/venv/lib >/dev/null

# The matplotlib font cache costs seconds to build. Baked here and shipped read-only;
# the supervisor copies it into a writable MPLCONFIGDIR under /scratch before it
# pre-imports matplotlib (see prewarm.py) so no execution ever rebuilds it.
RUN mkdir -p /out/mplcache \
    && MPLBACKEND=Agg MPLCONFIGDIR=/out/mplcache \
       python3 -c "import matplotlib.font_manager"

# A second non-root uid for the forked child (section 2, pids row option (a) and the
# Permission contract): distinct uid so RLIMIT_NPROC bounds the child rather than the
# supervisor and so the child cannot read the supervisor's /proc/<pid>/environ, on the
# shared gid 65532 that carries artifact and token access in both directions. Built by
# appending to the base image's own passwd/group so nothing distroless ships is lost.
COPY --from=distroless /etc/passwd /out/passwd
COPY --from=distroless /etc/group /out/group
RUN printf 'sandboxchild:x:65533:65532:sandbox child:/nonexistent:/sbin/nologin\n' >> /out/passwd

# Assert the final image's properties from here: the final stage has no shell, so
# nothing can be checked after it is assembled. /dl is the base rootfs verbatim; the
# Dockerfile itself is staged so the checks can assert on the *final* stage's ENV, and
# schema/ and stubs/ so the placeholder gate sees what the final stage will copy.
COPY --from=distroless / /dl/
COPY schema/ /build/schema/
COPY stubs/ /build/stubs/
COPY Dockerfile /build/Dockerfile
COPY build-checks.py /build/build-checks.py
RUN python3 /build/build-checks.py


FROM gcr.io/distroless/python3-debian12:nonroot

# The image tag follows this repo's HEAD like the other local build contexts, but the
# SDK comes from genetics-mcp-server, so the tag alone does not identify the contents.
# The build scripts pass that repo's short sha here.
ARG SDK_REF=unknown
LABEL com.fulltiltgenomics.genetics-mcp-server-ref="${SDK_REF}"

COPY --from=builder /out/passwd /etc/passwd
COPY --from=builder /out/group /etc/group
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder --chown=65532:65532 /out/mplcache /genetics/mplcache
COPY --chown=65532:65532 prewarm.py /genetics/prewarm.py
COPY --chown=65532:65532 schema/ /genetics/schema/
COPY --chown=65532:65532 stubs/ /genetics/sdk/

# The venv's bin/python3 symlinks the *builder's* interpreter, which does not exist
# here. The distroless interpreter is used directly against the venv's site-packages
# instead — same CPython minor version, so the cp311 wheels and the baked .pyc files
# both match.
# GCE_METADATA_HOST is section 3(c)'s named fallback, and it is required rather than
# optional: polars links object_store, a Rust GCS/S3/Azure client that mints metadata
# tokens with no Python google-auth involved, so "no google-auth in the image" is true
# of the Python package name only. Verified against the built image with a fake
# metadata listener: object_store sends
# `GET /computeMetadata/v1/instance/service-accounts/default/token` to whatever this
# names, so pinning it to the literal link-local IP keeps `metadata.google.internal`
# out of the resolver — which matters because the sandbox has no DNS and a name lookup
# there is a multi-second stall inside the wall clock, not a fast failure. It is not a
# confinement control: reaching the address at all is the NetworkPolicy's job
# (genetics-results-suite-4h6.8).
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
# Deliberately NOT set here, and owned by genetics-results-suite-4h6.14 because they
# are per-execution: TMPDIR, HOME, MPLCONFIGDIR, XDG_CACHE_HOME and PYTHONPYCACHEPREFIX
# all point inside /scratch/<execution-id> (section 2, writable paths). Setting them to
# a fixed path in the image would create exactly the cross-execution shared directory
# that removing the pod-level /tmp was meant to prevent.

USER 65532:65532
WORKDIR /genetics

# ENTRYPOINT is the bare interpreter so the image is inspectable (`docker run <img> -c
# ...`) and so 4h6.14 can supply the supervisor as the command without rebuilding.
# There is no CMD: the supervisor module is genetics-results-suite-4h6.14's to add, and
# a placeholder default here would be indistinguishable from a real one at runtime.
ENTRYPOINT ["/usr/bin/python3"]
