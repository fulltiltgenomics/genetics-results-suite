#!/usr/bin/env python3
"""Hold the shipped worked examples to the sandbox's per-query scan cap, on live BigQuery.

Every `tables.<view>.examples[].sql` in configs/datasets.yaml is copied verbatim into
sandbox/schema/<view>.md under the heading "Queries that run against <view> as written", and
that text is also chat-backend's system prompt — so an example that BigQuery refuses under the
sandbox cap teaches the model a query shape that fails, and it retries blind. Measured on a
production turn: the first gene_burden_results_v example (`WHERE gene = 'TLN1'`) estimated at
58 GB against a 50 GB cap, and the model spent five run_analysis calls finding the predicate
that works.

WHAT IS MEASURED. BigQuery enforces `maximum_bytes_billed` against its pre-execution
ESTIMATE, the same number a dry run reports, and that estimate accounts for partition pruning
only — clustering can cut the bytes actually billed but never lowers the estimate the cap is
checked against. So this gate dry-runs each example exactly as db-api would submit it (bare
view names resolved against the served dataset) and compares the estimate to the cap; nothing
else predicts whether the sandbox will run it.

WHAT ELSE IS CHECKED. A view may declare `partition_column` and `clustering_columns`; the
generator renders them into the view's schema doc as the predicate that prunes the scan. A
declaration is a checkable claim about the base table's storage, so it is compared here to
the live table: a mismatch fails, and a base table that is partitioned or clustered while its
view declares nothing is reported as a warning (the doc is then silent where it should guide).

Needs the `bq` CLI with credentials for the target project. Run from scripts/deploy.sh once
GCP_PROJECT and BQ_DATASET are resolved; also runnable by hand:

  scripts/check-example-scans.py --project daly-finngenie --dataset genetics_results

Exit 0 = every example fits and every declaration matches; 1 = an example is over the cap or a
declaration is wrong; 2 = could not tell (no bq, no credentials, no PyYAML), which is not
evidence of a broken example — the caller warns and continues, the convention the other
preflights share.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from paths import DATASETS_YAML

# db-api's SANDBOX_MAX_BYTES_BILLED (genetics-results-db api/main.py). Not importable from
# here, so the value is repeated and overridable; a drift shows up as this gate passing an
# example the sandbox refuses, which the next production turn reports the expensive way.
DEFAULT_CAP_BYTES = 50 * 1024**3


def _bq(args, *, project):
    cmd = ["bq", "--project_id", project, "--format=json", "--headless", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _cannot_run(message):
    print(f"check-example-scans: cannot run: {message}", file=sys.stderr)
    return 2


def dry_run_bytes(sql, *, project, dataset):
    """The estimate BigQuery checks `maximum_bytes_billed` against, or an error string."""
    code, out, err = _bq(
        ["query", "--dry_run", "--use_legacy_sql=false", f"--dataset_id={project}:{dataset}", sql],
        project=project,
    )
    if code != 0:
        return None, (err or out).strip().splitlines()[-1:] or ["bq query failed"]
    try:
        stats = json.loads(out)["statistics"]
        return int(stats["totalBytesProcessed"]), None
    except (ValueError, KeyError, TypeError):
        return None, ["unparseable dry-run output"]


def live_layout(view, *, project, dataset):
    """(partition column, clustering columns) of the view's base table, or None if unknown."""
    base = view.removesuffix("_v")
    code, out, _err = _bq(["show", f"{project}:{dataset}.{base}"], project=project)
    if code != 0:
        return None
    meta = json.loads(out)
    partition = (meta.get("rangePartitioning") or meta.get("timePartitioning") or {}).get("field")
    clustering = (meta.get("clustering") or {}).get("fields") or []
    return partition, list(clustering)


def _fmt_gb(n):
    return f"{n / 1024**3:.1f} GB"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True, help="GCP project the deployment serves")
    parser.add_argument("--dataset", required=True, help="BigQuery dataset db-api resolves bare names in")
    parser.add_argument(
        "--cap-bytes",
        type=int,
        default=int(os.environ.get("SANDBOX_MAX_BYTES_BILLED", DEFAULT_CAP_BYTES)),
        help="per-query estimate the sandbox is allowed (default: db-api's 50 GB)",
    )
    parser.add_argument("--datasets-yaml", default=DATASETS_YAML)
    args = parser.parse_args(argv)

    try:
        import yaml
    except ImportError:
        return _cannot_run("PyYAML is missing (pip install pyyaml)")
    if shutil.which("bq") is None:
        return _cannot_run("the bq CLI is not on PATH")
    code, _out, err = _bq(["ls", "--max_results=1", f"{args.project}:{args.dataset}"], project=args.project)
    if code != 0:
        return _cannot_run(f"bq cannot list {args.project}:{args.dataset}: {err.strip().splitlines()[-1:]}")

    with open(args.datasets_yaml) as fh:
        tables = yaml.safe_load(fh).get("tables") or {}

    exposed = {view: table for view, table in tables.items() if table.get("exposed")}
    examples = [
        (view, " ".join(str(example.get("description", "")).split())[:70], str(example["sql"]))
        for view, table in exposed.items()
        for example in table.get("examples") or []
    ]
    # every bq invocation is an independent process that spends most of its time starting
    # up, so the ~70 of them run side by side; serially this took four minutes
    with ThreadPoolExecutor(max_workers=8) as pool:
        layouts = dict(zip(exposed, pool.map(
            lambda view: live_layout(view, project=args.project, dataset=args.dataset), exposed
        )))
        estimates = list(pool.map(
            lambda ex: dry_run_bytes(ex[2], project=args.project, dataset=args.dataset), examples
        ))

    failures = []
    warnings = []
    for view, table in exposed.items():
        layout = layouts[view]
        declared_partition = table.get("partition_column")
        declared_clustering = list(table.get("clustering_columns") or [])
        if layout is None:
            if declared_partition or declared_clustering:
                failures.append(f"{view}: declares a layout but the base table could not be read")
        else:
            partition, clustering = layout
            if declared_partition or declared_clustering:
                if (declared_partition, declared_clustering) != (partition, clustering):
                    failures.append(
                        f"{view}: declares partition_column={declared_partition!r} "
                        f"clustering_columns={declared_clustering!r} but the base table has "
                        f"partition {partition!r} clustering {clustering!r}"
                    )
            elif partition or clustering:
                warnings.append(
                    f"{view}: base table is partitioned on {partition!r} and clustered on "
                    f"{clustering!r} but the view declares neither, so its schema doc names no "
                    "pruning predicate"
                )

    for (view, title, _sql), (estimate, error) in zip(examples, estimates):
        if error:
            failures.append(f"{view}: example {title!r} does not dry-run: {error[0]}")
        elif estimate > args.cap_bytes:
            failures.append(
                f"{view}: example {title!r} estimates {_fmt_gb(estimate)}, over the "
                f"{_fmt_gb(args.cap_bytes)} sandbox cap"
            )
        else:
            print(f"ok   {view}: {_fmt_gb(estimate):>9}  {title}")
    checked = len(examples)

    for line in warnings:
        print(f"WARN {line}")
    for line in failures:
        print(f"FAIL {line}")
    print(
        f"check-example-scans: {checked} examples against {args.project}:{args.dataset}, "
        f"{len(failures)} failing, {len(warnings)} warnings"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
