"""Module list the sandbox supervisor imports before it accepts any execution.

The latency budget assumes the marginal cost of a script is a fork, not a pod schedule and
not a cold import of scipy. Importing these in the supervisor puts them in pages the forked
child inherits copy-on-write.

The supervisor must copy the baked font cache out of $GENETICS_MPLCACHE into a writable
directory and point MPLCONFIGDIR at it BEFORE calling prewarm(). That ordering is a hard
requirement: on matplotlib 3.10 an unwritable MPLCONFIGDIR does not fall back to a temporary
directory, and with a read-only rootfs and no pod-level /tmp `import matplotlib` raises.
"""

PREWARM_MODULES = (
    "numpy",
    "scipy",
    "scipy.stats",
    "polars",
    "matplotlib",
    "matplotlib.pyplot",
    # registers the scienceplots stylesheets in plt.style.library. This is the ONLY route by
    # which scienceplots reaches a figure — the baked matplotlibrc carries density and no
    # style — so the import is what makes `plt.style.use("science")`, written from memory,
    # resolve in a child instead of raising OSError. Registered once in the supervisor; every
    # fork inherits it.
    "scienceplots",
    "httpx",
    "genetics_mcp_server.sdk",
)


class PrewarmError(RuntimeError):
    """A module the sandbox is contracted to provide could not be imported."""


def prewarm(modules=PREWARM_MODULES):
    """Import each module. Raises PrewarmError naming every failure.

    None of these is optional: every one is a module the sandbox contract says a script may
    import, so a failure is not a latency regression that degrades gracefully — it is a pod
    that answers health checks and then fails every data script inside the child, where the
    error surfaces as the script's ImportError rather than as a broken image.
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
