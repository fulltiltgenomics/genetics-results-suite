#!/usr/bin/env python3
"""Proving ground for the protein structure layer: the rehearsal ground and the measurements.

Every check group is declared before it exists and prints NOT MEASURED until something fills
it in, so a run always states what it did not measure rather than looking greener than it is.

  ground    the rehearsal ground the rest stands on: the rehearsal release root in the daly
            bucket lists, the rehearsal BigQuery dataset exists beside genetics_results in
            the same location with a `dev` segment in its name, and every suite repo has its
            worktree on the feature branch. The rehearsal pointer is reported, not ruled: it
            exists only once a release has been published to the rehearsal root.
  coverage  how densely the suite's own findings land on structures: the share of daly's
            fine-mapped credible-set and exome missense variants the variant -> residue map
            assigns to a residue with a model, split into "not in the map" and "mapped, no
            model", and the genes holding two or more such variants (pip >= 0.1). Needs a
            release with its residue map in the rehearsal root and the residue table loaded
            into the rehearsal dataset, so the file results-api serves and the BigQuery view
            are measured against each other.
  fidelity  genetics.structure's distances, PAE lookups and adjacency equal the lab's own
            utils.py on the oracle fixtures, multi-file proteins included, and a pair sharing
            no model window gets no number. Needs the SDK module and its fixtures.
  envelope  the same module under the sandbox limits (120 s, 2560 MiB): k-variant distances
            and one full-protein neighbourhood, timed and sized. Needs the SDK module and a
            release served by results-api.
  figure    genetics.plots.protein_track renders a reference protein from the release:
            SETD1A (O15047) shows its RRM and SET domains and the disordered middle. Needs
            the plot function and features in the release.
  view      the browser's structure viewer loads a model through /api and renders the
            highlighted residues, run against the BUILT frontend image because the vite dev
            server does not apply the production CSP. Needs the viewer component.

THE GROUND is fixed by the defaults below and overridable by flag: the rehearsal root
gs://daly-genetics-results/protein_structure_staging/, whose releases/CURRENT is the rehearsal
pointer, beside the production root protein_structure/ that a deployment's pointer will name;
and daly-finngenie:genetics_results_protein_structure_dev, which holds only this feature's
table. A rehearsal never moves the production pointer and never loads genetics_results.

Exit 0 every measured check passed; 1 at least one failed; 2 the harness could not run — a
missing precondition, never a verdict. Same 0/1/2 convention as
external-inputs-proving-ground.py and mr-proving-ground.py, and as there a NOT MEASURED group
is listed in the summary but does not change the exit code, even when selected explicitly.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import checks
import siblings
from checks import check, die, skip

PROJECT = "daly-finngenie"
SOURCE_DATASET = "genetics_results"
REHEARSAL_DATASET = "genetics_results_protein_structure_dev"
REHEARSAL_ROOT = "gs://daly-genetics-results/protein_structure_staging"
WORKTREE = "protein-structure"
# the same two rules scripts/bq-dev-dataset.sh enforces on its target (a dev segment, and
# only [A-Za-z0-9_]), so a dataset that passes here is one that script will also accept
DEV_SEGMENT = re.compile(r"(^dev$|^dev_|_dev_|_dev$)")
PRODUCTION_DATASETS = {"genetics_results", "genetics_api_logs", "genetics_chat_logs"}

GROUPS = ("ground", "coverage", "fidelity", "envelope", "figure", "view")
NOT_YET = {
    "coverage": "needs a release with its residue map and the residue table in the rehearsal "
                "dataset",
    "fidelity": "needs genetics.structure and its oracle fixtures",
    "envelope": "needs genetics.structure and a release served by results-api",
    "figure": "needs genetics.plots.protein_track and features in a release",
    "view": "needs the structure viewer in a built frontend image",
}


def run(cmd, timeout_s=60):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        die(f"{cmd[0]} timed out after {timeout_s} s")
    return out.returncode, out.stdout, out.stderr


def last_line(text):
    lines = [ln for ln in text.strip().splitlines() if "warning" not in ln.lower() and ln.strip()]
    return lines[-1] if lines else ""


def bq_dataset(project, dataset):
    """The dataset's metadata, or None when BigQuery says it does not exist."""
    code, out, err = run(["bq", "show", "--format=json", f"{project}:{dataset}"])
    if code == 0:
        return json.loads(out)
    if "Not found" in out + err:
        return None
    die(f"bq show {project}:{dataset} failed: {last_line(out + err)}")


def group_ground(args):
    root = args.rehearsal_root.rstrip("/")
    code, out, err = run(["gcloud", "storage", "ls", f"{root}/"])
    if code != 0 and "matched no objects" not in err:
        die(f"gcloud storage ls {root}/ failed: {last_line(err)}")
    objects = out.split() if code == 0 else []
    check("rehearsal root lists", bool(objects), f"{root}/: {'listed' if objects else 'empty'}")
    pointer = f"{root}/releases/CURRENT"
    code, out, err = run(["gcloud", "storage", "cat", pointer])
    if code == 0:
        state = "present: " + out.strip()[:200]
    elif "matched no objects" in err:
        state = "absent (no release published)"
    else:
        state = f"unknown ({last_line(err)})"
    print(f"  info  rehearsal pointer {pointer}: {state}")

    name = args.dataset
    check("rehearsal dataset name has a dev segment and is not production",
          bool(DEV_SEGMENT.search(name)) and re.fullmatch(r"[A-Za-z0-9_]+", name) is not None
          and name not in PRODUCTION_DATASETS, name)
    meta = bq_dataset(args.project, name)
    check("rehearsal dataset exists", meta is not None, f"{args.project}:{name}")
    source = bq_dataset(args.project, SOURCE_DATASET)
    if meta is None or source is None:
        skip("rehearsal dataset is in the source dataset's location",
             "the rehearsal dataset or the source dataset was not found")
    else:
        # a view joining the rehearsal table to production tables must run in one location
        check("rehearsal dataset is in the source dataset's location",
              meta.get("location") == source.get("location"),
              f"{meta.get('location')} vs {SOURCE_DATASET} {source.get('location')}")

    for repo in siblings.SUITE_REPOS:
        try:
            main = siblings.resolve(repo)
        except siblings.SiblingError as exc:
            die(str(exc))
        if main is None:
            check(f"{repo} worktree on {args.worktree}", False, "no checkout of the repo found")
            continue
        tree = os.path.join(main, ".claude", "worktrees", args.worktree)
        _, branch, _ = run(["git", "-C", tree, "branch", "--show-current"]) \
            if os.path.isdir(tree) else (1, "", "")
        check(f"{repo} worktree on {args.worktree}", branch.strip() == args.worktree,
              f"{tree}: {'missing' if not os.path.isdir(tree) else 'on ' + repr(branch.strip())}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", default=PROJECT, help="GCP project of both datasets")
    parser.add_argument("--dataset", default=REHEARSAL_DATASET,
                        help="the rehearsal BigQuery dataset")
    parser.add_argument("--rehearsal-root", default=REHEARSAL_ROOT,
                        help="gs:// release root a rehearsal publishes to")
    parser.add_argument("--worktree", default=WORKTREE,
                        help="worktree directory name under <repo>/.claude/worktrees, and the "
                             "branch it must be on")
    parser.add_argument("--groups", default=",".join(GROUPS),
                        help=f"comma-separated subset of {', '.join(GROUPS)}")
    args = parser.parse_args()
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        die(f"unknown group(s) {', '.join(unknown)}; choose from {', '.join(GROUPS)}")
    if "ground" in groups:
        for tool in ("gcloud", "bq", "git"):
            if shutil.which(tool) is None:
                die(f"{tool} is not on PATH")

    for group in groups:
        print(f"[{group}]")
        started = time.monotonic()
        if group == "ground":
            group_ground(args)
        else:
            skip(group, f"{NOT_YET[group]}: not implemented yet")
        print(f"  [{group}] {time.monotonic() - started:.1f} s")

    print(f"\n{checks.CHECKS - len(checks.FAILURES)}/{checks.CHECKS} checks passed")
    if checks.SKIPPED:
        print("NOT MEASURED:")
        for item in checks.SKIPPED:
            print(f"  - {item}")
    if checks.FAILURES:
        print("FAILED:")
        for item in checks.FAILURES:
            print(f"  - {item}")
        return 1
    return 0


if __name__ == "__main__":
    # exit 1 is reserved for a red check; anything unexpected is the harness failing to run
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"HARNESS: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
