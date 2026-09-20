# The URL fetcher. Design of record: docs/code-execution-security.md, "The URL fetcher: a
# second pod, because the fetch must not happen in the first one".
#
# There is no requirements.txt and no pip step, and that is the design: this pod dials
# attacker-nameable addresses and holds nothing worth stealing, so every dependency it does
# not have is attack surface it does not have. The one thing it needs from the base that the
# standard library cannot supply is a CA BUNDLE — ssl.create_default_context() verifies
# against /etc/ssl/certs/ca-certificates.crt, and a base without one fails at the first TLS
# handshake rather than at build time, which no check here or in CI would ever see.
# build-checks.py loads the bundle out of the base and counts the certificates in it, at the
# path SSL_CERT_FILE names in the final stage below. That variable is the one the check and
# the interpreter agree on: the base sets it too, and pinning it here is what gives the
# check something in the Dockerfile to verify rather than a path it had to guess — a base
# that moved or dropped the bundle then fails the build instead of the first handshake.

FROM gcr.io/distroless/python3-debian12:nonroot AS distroless

FROM python:3.11-slim AS builder

# THE WHOLE BUILD CONTEXT, deliberately. The checks below assert what is NOT in it — chiefly
# devserver.py, excluded by .dockerignore — and a check that inspects only the files the final
# stage copies cannot see the difference between "excluded" and "not copied by this Dockerfile
# today".
COPY . /build/context/
# the final stage's rootfs, so the no-shell and CA-bundle checks read the image that ships
# rather than the builder they run in
COPY --from=distroless / /dl/
RUN python3 /build/context/build-checks.py


FROM gcr.io/distroless/python3-debian12:nonroot

# The four production modules and nothing else. build-checks.py parses these COPY lines and
# holds the shipped set to exactly this — a fifth module, or devserver.py, fails the build.
#
# `--from=builder` IS WHAT MAKES THE CHECKS RUN. BuildKit builds only the stages the target
# depends on, so with the sources copied straight from the context the builder stage above is
# pruned and every assertion in build-checks.py is skipped in silence — measured, on the first
# build of this file, and again when a reviewer rewrote these four lines.
#
# NOTHING IN THIS FILE CAN ENFORCE THAT. build-checks.py asserts the flag, but it asserts it
# from inside the stage the flag is what runs: rewrite the four lines and the assertion is not
# skipped so much as never reached. What enforces it is out of the image —
# assert_fetcher_dockerfile_gated in scripts/lib/env.sh, called by both build.sh and
# build-all.sh before `docker build`, which refuses a final stage whose COPYs do not come from
# a stage that runs build-checks.py. The in-image check is still worth having: it is what a
# hand-run `docker build url-fetcher/` gets, and it catches the one-COPY case.
COPY --from=builder /build/context/guard.py /app/guard.py
COPY --from=builder /build/context/fetch.py /app/fetch.py
COPY --from=builder /build/context/server.py /app/server.py
COPY --from=builder /build/context/main.py /app/main.py

# The rootfs is read-only in the pod; without this, CPython attempts a __pycache__ write per
# import and takes the EACCES silently on every start.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

USER 65532:65532
WORKDIR /app

# NAMED, not a bare interpreter, and no CMD — the opposite of sandbox/Dockerfile's choice and
# for the opposite reason. There the manifest supplies the module in `args:` so a manifest that
# lost them CrashLoopBackOffs; here the whole design rests on the deployed guard being the only
# guard a manifest can produce, so `args:` must not be able to choose what runs. Kubernetes
# `args:` REPLACES CMD and leaves ENTRYPOINT alone, so with the program named here the worst an
# `args:` can do is append argv that main.py defines no flags for and ignores. A manifest
# `command:` does override this, which no image can prevent — scripts/deploy.sh refuses to
# apply k8s/deployments/url-fetcher.yaml if the container declares one.
ENTRYPOINT ["/usr/bin/python3", "/app/main.py"]
