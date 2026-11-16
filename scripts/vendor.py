#!/usr/bin/env python3
"""Write the suite's single copy of each shared file into the repos that carry it.

configs/vendored.yaml names each source in this repo and the repos that receive it. A copy
is the source's bytes and mode, nothing added: no header is stamped in, because
check-duplication.py recognises a vendored copy by byte-identity with its source and a
stamped copy would need the stamp parsed back out on both sides.

Usage:
    scripts/vendor.py                 write every copy that differs
    scripts/vendor.py --check         exit 1 if any copy differs or is missing
    scripts/vendor.py --root DIR      resolve the siblings as DIR/<repo> — a build's
                                      clones — instead of this machine's checkouts

Exit 0 = in step (or written), 1 = --check found drift, 2 = could not tell: the manifest
does not say what it must, a source is missing, or a receiving repo is not checked out.
A missing checkout is 2 rather than a pass because a copy nobody looked at has not been
found identical.
"""

import argparse
import os
import shutil
import stat
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import siblings  # noqa: E402

ROOT = siblings.ROOT
MANIFEST = os.path.join(ROOT, "configs", "vendored.yaml")
REQUIRED = ("source", "repos", "reason")
OPTIONAL = ("dest",)


class ManifestError(Exception):
    pass


def load_manifest(path=MANIFEST):
    """[(source_rel, dest_rel, [repo, ...])], refusing anything it would have to guess at."""
    try:
        import yaml
    except ImportError as exc:
        raise ManifestError("PyYAML is not installed") from exc
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestError(f"{path} could not be read: {exc}") from exc
    entries = doc.get("vendored") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ManifestError(f"{path} has no `vendored` list")
    out, seen = [], set()
    for e in entries:
        if not isinstance(e, dict) or set(e) - set(REQUIRED + OPTIONAL):
            raise ManifestError(f"{path}: an entry is not a mapping of "
                                f"{list(REQUIRED + OPTIONAL)}: {e!r}")
        source, repos = e.get("source"), e.get("repos")
        if not isinstance(source, str) or not isinstance(repos, list) or not repos:
            raise ManifestError(f"{path}: an entry needs `source` and a non-empty `repos`: {e!r}")
        if not str(e.get("reason") or "").strip():
            raise ManifestError(f"{path}: {source} has no reason")
        if not os.path.isfile(os.path.join(ROOT, source)):
            raise ManifestError(f"{path}: source {source} does not exist in this repo")
        dest = e.get("dest", source)
        for repo in repos:
            if repo not in siblings.SUITE_REPOS or repo == "genetics-results-suite":
                raise ManifestError(f"{path}: {source} names {repo!r}, not a sibling repo")
            if (repo, dest) in seen:
                raise ManifestError(f"{path}: {repo}:{dest} is written by two entries")
            seen.add((repo, dest))
        out.append((source, dest, list(repos)))
    return out


def copies(manifest):
    """{"<repo>:<dest>": source_rel} — the form check-duplication.py asks for."""
    return {f"{repo}:{dest}": source for source, dest, repos in manifest for repo in repos}


def same(src, dst):
    try:
        with open(src, "rb") as a, open(dst, "rb") as b:
            if a.read() != b.read():
                return False
    except OSError:
        return False
    mode = stat.S_IXUSR
    return (os.stat(src).st_mode & mode) == (os.stat(dst).st_mode & mode)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--root")
    args = ap.parse_args()

    try:
        manifest = load_manifest()
    except ManifestError as exc:
        print(f"vendor: {exc}", file=sys.stderr)
        return 2

    if args.root:
        repos = {r: os.path.join(args.root, r) for r in siblings.SUITE_REPOS}
        repos = {r: p for r, p in repos.items() if os.path.isdir(p)}
    else:
        repos = {r: p for r, p in siblings.resolve_all().items() if p}

    drift, missing, written = [], [], []
    for source, dest, targets in manifest:
        src = os.path.join(ROOT, source)
        for repo in targets:
            if repo not in repos:
                missing.append(repo)
                continue
            dst = os.path.join(repos[repo], dest)
            if same(src, dst):
                continue
            if args.check:
                drift.append(f"{repo}:{dest}  (source: {source})")
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
            shutil.copymode(src, dst)
            written.append(f"{repo}:{dest}")

    for line in written:
        print(f"vendor: wrote {line}")
    if missing:
        print("vendor: not checked out, so not compared: " + ", ".join(sorted(set(missing))),
              file=sys.stderr)
    if drift:
        print("vendor: these copies differ from their source in genetics-results-suite:",
              file=sys.stderr)
        for line in drift:
            print(f"  {line}", file=sys.stderr)
        print("  edit the source and run scripts/vendor.py; never edit a copy.",
              file=sys.stderr)
        return 1
    if missing:
        return 2
    if not written:
        print("vendor: every copy matches its source")
    return 0


if __name__ == "__main__":
    sys.exit(main())
