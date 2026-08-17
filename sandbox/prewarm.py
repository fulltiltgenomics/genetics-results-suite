"""Module list the sandbox supervisor imports before it accepts any execution.

The latency budget in docs/code-execution-security.md assumes the marginal cost of a
script is a fork, not a pod schedule and not a cold import of scipy. Importing these
in the supervisor puts them in pages the forked child inherits copy-on-write, so the
child pays neither the disk read nor the module-init cost.

Owned by the image (genetics-results-suite-4h6.6); *called* by the supervisor
(genetics-results-suite-4h6.39), which must call prewarm() once at startup, before the
first fork and before it drops any privilege.

matplotlib is the expensive one and the reason the ordering matters: importing
matplotlib.pyplot builds the font cache, which takes seconds on a cold MPLCONFIGDIR.
The supervisor must therefore copy the baked cache out of $GENETICS_MPLCACHE into a
writable directory and point MPLCONFIGDIR at it *before* calling prewarm().

That ordering is a hard requirement, not an optimisation. On matplotlib 3.10 an
unwritable MPLCONFIGDIR does **not** fall back to a temporary directory: with the root
filesystem read-only and no pod-level /tmp, `import matplotlib` raises
`OSError: Matplotlib requires access to a writable cache directory`. Verified in the
built image, not assumed; see docs/code-execution-security.md.
"""

PREWARM_MODULES = (
    "numpy",
    "scipy",
    "scipy.stats",
    "polars",
    "matplotlib",
    "matplotlib.pyplot",
    "httpx",
    "genetics_mcp_server.sdk",
)


class PrewarmError(RuntimeError):
    """A module the sandbox is contracted to provide could not be imported."""


def prewarm(modules=PREWARM_MODULES):
    """Import each module. Raises PrewarmError naming every failure.

    None of these is optional. Every one is a module the sandbox contract says a
    model-authored script may import, so a failure here is not a latency regression
    that degrades gracefully — it is a pod that answers health checks and then fails
    every plotting or data script inside the forked child, where the error surfaces as
    the *script's* ImportError rather than as a broken image. Failing at startup makes
    the pod crash-loop, which is visible; returning the failures made it depend on the
    caller bothering to look.
    """
    import importlib

    failed = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:
            failed.append(f"{name}: {type(exc).__name__}: {exc}")
    if failed:
        raise PrewarmError("prewarm imports failed: " + "; ".join(failed))


if __name__ == "__main__":
    import sys

    try:
        prewarm()
    except PrewarmError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
