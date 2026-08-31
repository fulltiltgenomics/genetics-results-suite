#!/usr/bin/env python3
"""Resolve where each repo of the suite is checked out on this machine.

WHY THIS EXISTS. Four scripts here already answer this question privately and none of
them answers it generally: sync-datasets.sh and dev-stack.sh share SUITE_SIBLING_ROOT but
cover different repo sets, run-sandbox-local.sh and gen-sandbox-docs.py resolve only
genetics-mcp-server through MCP_SERVER_DIR, and not one of them can find
genetics-results-munge, which on at least one machine is checked out under a different
root from the rest. Anything new that needs a sibling should use this rather than add a
fifth answer. Retrofitting the existing four is separate work and is deliberately not
done here.

RESOLUTION ORDER, per repo:

  1. SUITE_REPO_<NAME>, e.g. SUITE_REPO_GENETICS_RESULTS_MUNGE — an exact path.
  2. $SUITE_SIBLING_ROOT/<name>, when that variable is set.
  3. <derived root>/<name>, where the derived root is the parent of the MAIN checkout, two
     levels up from `git rev-parse --git-common-dir`. Measured: from a plain checkout git
     answers `.git` (relative), from a linked worktree it answers the MAIN checkout's
     absolute `.git`, so absolutising first and then taking two parents is correct for
     both, and siblings resolve next to the main checkout rather than next to a worktree.
  4. <sibling of the derived root>/<name> — the derived root's own siblings, one level up.
     This is what finds a repo checked out under a different root from the others, and is
     why SUITE_SIBLING_ROOT narrows the search rather than ending it: honouring it
     exclusively, as the two shell scripts do, makes such a repo unreachable.

An AUTO-DISCOVERED candidate is accepted only if it is a git checkout whose `origin` URL
names the repo; a checkout with no origin is accepted on its directory name alone. Rule 4
enumerates every directory under the parent, so without that test a same-named directory
beside the real one silently becomes the answer.

A `SUITE_REPO_<NAME>` override skips the origin test: a user naming a path is not a decoy,
and a fork or a rename — whose origin names something else — is exactly what the override
is for. It must still be a git checkout, and when it is not, that is an ERROR naming the
path, not a "repo is not checked out": the caller pointed at something real and deserves
to be told why it was refused.

Usage:
    scripts/lib/siblings.py                 # table of every repo and where it resolved
    scripts/lib/siblings.py --json
    scripts/lib/siblings.py --path REPO     # print one path; exit 2 if not checked out

Exit 0 = every repo resolved, 2 = at least one could not be (never 1: not finding a
checkout is a could-not-run, not a failure).
"""

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# the one place the suite's membership is written down. It is a literal because nothing in
# any tree enumerates it, and without an expected set a missing checkout cannot be
# reported at all — it would just be a repo nobody looked for. docs/project-spec.md renders
# this list through gen-doc-blocks.py rather than repeating it.
SUITE_REPOS = (
    "genetics-results-suite",
    "genetics-results-api",
    "genetics-results-db",
    "genetics-results-browser",
    "genetics-mcp-server",
    "genetics-results-munge",
)


def _git(args, cwd):
    try:
        out = subprocess.run(
            ["git", "-C", cwd] + args,
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def derived_root(suite_dir=ROOT):
    """The directory the sibling checkouts sit in, per rule 3 above."""
    common = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], suite_dir)
    if not common:
        return None
    return os.path.dirname(os.path.dirname(os.path.normpath(common)))


class SiblingError(Exception):
    """An explicit override names a path that cannot be used — distinct from a repo simply
    not being on this machine, which is None and not an error."""


def _override_var(repo):
    return "SUITE_REPO_" + repo.upper().replace("-", "_")


def _is_repo(path, name):
    if not os.path.isdir(path):
        return False
    url = _git(["remote", "get-url", "origin"], path)
    if url:
        return os.path.basename(url.rstrip("/")).removesuffix(".git") == name
    # a clone with no origin (a tarball, a CI fixture) still counts if it is a git
    # checkout under the right name; a plain directory of that name does not
    return os.path.basename(os.path.normpath(path)) == name and (
        _git(["rev-parse", "--is-inside-work-tree"], path) == "true"
    )


def candidate_roots(suite_dir=ROOT):
    roots = []
    env_root = os.environ.get("SUITE_SIBLING_ROOT")
    if env_root:
        roots.append(env_root)
    base = derived_root(suite_dir)
    if base:
        roots.append(base)
        parent = os.path.dirname(base)
        if parent and parent != base:
            try:
                roots.extend(
                    os.path.join(parent, d) for d in sorted(os.listdir(parent))
                    if os.path.isdir(os.path.join(parent, d))
                )
            except OSError:
                pass
    seen, out = set(), []
    for r in roots:
        r = os.path.abspath(r)
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def resolve(repo, suite_dir=ROOT):
    """Absolute path to `repo`'s main checkout, or None if it is not on this machine.

    Raises SiblingError when an override names an unusable path — see the docstring above.
    """
    var = _override_var(repo)
    override = os.environ.get(var)
    if override:
        if not os.path.isdir(override):
            why = "no such directory"
        elif _git(["rev-parse", "--is-inside-work-tree"], override) != "true":
            why = "not a git checkout"
        else:
            return os.path.abspath(override)
        origin = _git(["remote", "get-url", "origin"], override) or "none"
        raise SiblingError(
            f"{var}={override} cannot be used: {why} (origin: {origin}; "
            f"expected a checkout of {repo})")
    for root in candidate_roots(suite_dir):
        cand = os.path.join(root, repo)
        if _is_repo(cand, repo):
            return cand
    return None


def resolve_all(suite_dir=ROOT):
    return {r: resolve(r, suite_dir) for r in SUITE_REPOS}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--path", metavar="REPO")
    args = ap.parse_args()

    if args.path:
        if args.path not in SUITE_REPOS:
            print(f"cannot run: {args.path} is not a repo of this suite", file=sys.stderr)
            return 2
        try:
            p = resolve(args.path)
        except SiblingError as exc:
            print(f"cannot run: {exc}", file=sys.stderr)
            return 2
        if not p:
            print(f"cannot run: {args.path} is not checked out on this machine",
                  file=sys.stderr)
            return 2
        print(p)
        return 0

    try:
        found = resolve_all()
    except SiblingError as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(found, indent=2, sort_keys=True))
    else:
        for repo in SUITE_REPOS:
            print(f"{repo:28} {found[repo] or '-- not checked out --'}")
    return 0 if all(found.values()) else 2


if __name__ == "__main__":
    sys.exit(main())
