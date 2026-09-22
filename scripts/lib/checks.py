"""The pass/fail/skip recorder the host-side harnesses share.

One process, one tally: a harness calls check() per assertion and reads FAILURES, SKIPPED
and CHECKS back for its summary. The exit code stays the caller's — the convention (0 every
property held, 1 one broke, 2 the harness could not run) is stated in each harness's own
docstring, and die() is its 2.
"""

import sys

FAILURES = []
SKIPPED = []
CHECKS = 0


def check(name, condition, detail=""):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(f"{name}: {detail}" if detail else name)
        print(f"  FAIL  {name} {detail}")
    else:
        print(f"  ok    {name}")


def skip(name, reason):
    """A check that cannot run in this mode. Counted and printed separately: a skipped
    assertion silently omitted is how a mode ends up proving less than its output claims."""
    SKIPPED.append(f"{name}: {reason}")
    print(f"  skip  {name} ({reason})")


def die(message):
    """The harness itself cannot run. Exit 2, never 1, so a caller that gates on 1 does not
    read a missing prerequisite as a broken property."""
    print(f"HARNESS: {message}", file=sys.stderr)
    raise SystemExit(2)


def case(name):
    """check() as a decorator, for a harness whose checks are functions that assert."""
    def wrap(fn):
        try:
            fn()
        except AssertionError as exc:
            check(name, False, str(exc))
        else:
            check(name, True)

    return wrap
