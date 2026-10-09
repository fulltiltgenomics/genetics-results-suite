#!/usr/bin/env python3
"""Proving ground for genetics.mr: the stack, the published anchors, and the measurements.

The epic's premise is that the module reproduces published MR from the suite's own data, and
its kill criterion is measured here. Each check group below is declared before it exists and
prints NOT MEASURED until the subtask that owns it fills it in, so a run always states what it
did not measure rather than looking greener than it is.

  ground    the stack the rest stands on: results-api answers and serves the finngen_r12 LD
            panel, db-api answers and serves the genetics_results dataset, the SDK imports
            from the genetics-mcp-server checkout and its ld_panels() names finngen_r12, and
            the replication fixtures in genetics-mcp-server tests/fixtures/mr/ are exactly
            the ones SHA256SUMS pins and match it.
  b0        Zhang 2025 Supplementary Data 8 instruments taken verbatim, outcome rows looked
            up in IIBDGC, Wald/IVW per protein against Data 9's IIBDGC IBD column: separates
            an outcome-route or harmonisation bug from an instrument-selection one.
  b1        the same comparison with the suite's own UKB_PPP cis credible-set leads as
            instruments: the premise proper.
  c         LDL-C -> I9_CHD (OR 1.3-1.7 per SD) and the PCSK9 and IL6R cis anchors
            (protective per SD on CHD).
  a         estimator identity against the R MendelianRandomization/TwoSampleMR reference
            fixtures, deterministic methods to ~1e-12.
  envelope  one exposure to many outcomes, timed: decides whether a precomputed instrument
            table is in scope.

RULING FOR b (the kill criterion): per-protein log-OR estimates correlate with Data 9's
IIBDGC IBD column at r > 0.9, or at the ceiling the published standard errors imply, with the
tier-1 proteins (IL12B, CD6, MXRA8, CXCL9, IFNG, RSPO3, IL18) reproduced in direction.
Otherwise STOP: once the allele-frequency checks have verified harmonisation, a miss means
credible sets as instruments are unfit for MR, and the module is descoped rather than tuned.

THE STACK is the local one docs/local-dev-vm.md ("MR proving ground") brings up: results-api
on the daly profile and db-api on genetics_results, from the main checkouts. The SDK talks to
them with INTERNAL_API_SECRET, read from the environment or from dev-stack.sh's run
directory, never from an argument, so it never reaches a process list.

Exit 0 every measured check passed; 1 at least one failed; 2 the harness could not run — a
missing precondition, never a verdict. Same 0/1/2 convention as
external-inputs-proving-ground.py, and as there a NOT MEASURED group is listed in the summary
but does not change the exit code, even when selected explicitly: it is a statement about
this harness, not about the module.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import checks
import siblings
from checks import check, die, skip
from paths import DEV_STACK_RUN_DIR

PANEL = "finngen_r12"
DATASET = "genetics_results"
FIXTURE_DIR_REL = os.path.join("tests", "fixtures", "mr")
GROUPS = ("ground", "b0", "b1", "c", "a", "envelope")
NOT_YET = {
    "b0": "Data 8 instruments verbatim vs Data 9 IIBDGC",
    "b1": "own UKB_PPP cis leads vs Data 9 IIBDGC",
    "c": "LDL-C -> I9_CHD and the PCSK9/IL6R cis anchors",
    "a": "estimator identity vs the R reference fixtures",
    "envelope": "one-exposure-to-many-outcomes timing",
}

# the SDK probe runs in the mcp-server venv as a child, not imported here: this harness stays
# runnable by the system python3, and the child's import resolving out of the checkout under
# test is itself one of the things `ground` asserts
SDK_PROBE = """
import json, os, sys
import genetics_mcp_server
from genetics_mcp_server import sdk as genetics
panels = genetics.ld_panels()
print(json.dumps({"module": os.path.realpath(os.path.dirname(genetics_mcp_server.__file__)),
                  "panels": panels["panel"].to_list()}))
"""


def http_get(url, timeout_s=30):
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        return None, str(exc).encode()


def http_post_json(url, payload, secret, timeout_s=60):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {secret}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        return None, str(exc).encode()


def db_api_dataset_check(db_api_url, secret):
    """Whether db-api serves `genetics_results`, from its own allow-list.

    No db-api route returns the dataset it serves, but its query gate admits a table only if
    it is PROJECT_ID.DATASET_ID.<view>, so a dry run naming genetics_results.credible_sets_v
    succeeds there and is refused "outside the exposed set" on any other dataset. A dry run
    scans nothing. The refusal does not name the dataset the server does serve.
    """
    sql = f"SELECT 1 FROM `{DATASET}.credible_sets_v` LIMIT 0"
    status, body = http_post_json(db_api_url + "/query", {"sql": sql, "dry_run": True}, secret)
    text = body.decode(errors="replace")
    if status == 200:
        return True, f"{db_api_url} serves {DATASET}"
    if status == 403 and "outside the exposed set" in text:
        return False, (f"{db_api_url} does not serve {DATASET}: its allow-list refuses "
                       f"{DATASET}.credible_sets_v, so its DATASET_ID is some other dataset")
    return False, f"{db_api_url}: dry run answered {status}: {text[:300]}"


def internal_secret(run_dir):
    secret = os.environ.get("INTERNAL_API_SECRET")
    if secret:
        return secret
    path = os.path.join(run_dir, "internal-api-secret")
    if not os.path.exists(path):
        die(f"INTERNAL_API_SECRET is unset and {path} does not exist; export one or run "
            "scripts/dev-stack.sh up once")
    with open(path) as fh:
        return fh.read().strip()


def read_sums(path):
    sums = {}
    with open(path) as fh:
        for line in fh:
            digest, name = line.rstrip("\n").split("  ", 1)
            sums[name] = digest
    return sums


def group_ground(args, mcp):
    status, _ = http_get(args.results_api_url + "/healthz")
    check("results-api answers /healthz", status == 200, f"{args.results_api_url}: {status}")

    status, body = http_get(args.results_api_url + "/api/v1/ld/panels")
    panels = []
    if status == 200:
        try:
            panels = [p.get("panel") for p in json.loads(body).get("panels", [])]
        except (ValueError, AttributeError):
            pass
    check(f"results-api /api/v1/ld/panels lists {PANEL}", PANEL in panels,
          f"status {status}, panels {panels}")

    status, _ = http_get(args.db_api_url + "/health")
    check("db-api answers /health", status == 200, f"{args.db_api_url}: {status}")
    secret = internal_secret(args.run_dir)
    ok, detail = db_api_dataset_check(args.db_api_url, secret)
    print(f"  db-api dataset: {detail}")
    check(f"db-api serves the {DATASET} dataset", ok, detail)

    python = os.path.join(mcp, ".venv", "bin", "python")
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": os.path.join(mcp, "src"),
        "GENETICS_API_URL": args.results_api_url + "/api",
        "BIGQUERY_API_URL": args.db_api_url,
        "INTERNAL_API_SECRET": secret,
    })
    proc = subprocess.run([python, "-c", SDK_PROBE], env=env, cwd=mcp, capture_output=True,
                          text=True, timeout=120)
    probe = {}
    if proc.returncode == 0:
        try:
            probe = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            pass
    expected = os.path.realpath(os.path.join(mcp, "src", "genetics_mcp_server"))
    check("the SDK imports from the checkout under test", probe.get("module") == expected,
          f"resolved {probe.get('module')}, expected {expected}; "
          f"stderr: {proc.stderr.strip()[-300:]}")
    check(f"genetics.ld_panels() names {PANEL}", PANEL in probe.get("panels", []),
          f"panels {probe.get('panels')}")

    fixtures = os.path.join(mcp, FIXTURE_DIR_REL)
    sums_path = os.path.join(fixtures, "SHA256SUMS")
    if not os.path.exists(sums_path):
        check("the MR fixtures match their SHA256SUMS", False, f"{sums_path} is missing")
        return
    sums = read_sums(sums_path)
    check("SHA256SUMS lists the fixtures", len(sums) > 0, sums_path)
    # a fixture dropped from the directory, or added without being pinned, would otherwise
    # pass: the loop below only visits names SHA256SUMS already lists
    on_disk = {n for n in os.listdir(fixtures) if n.endswith(".tsv")}
    check("the fixture TSVs are exactly the ones SHA256SUMS pins", on_disk == set(sums),
          f"unpinned {sorted(on_disk - set(sums))}, missing {sorted(set(sums) - on_disk)}")
    for name, want in sorted(sums.items()):
        path = os.path.join(fixtures, name)
        got = None
        if os.path.exists(path):
            with open(path, "rb") as fh:
                got = hashlib.sha256(fh.read()).hexdigest()
        check(f"fixture {name} matches SHA256SUMS", got == want, f"sha256 {got}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results-api-url", default="http://127.0.0.1:2000",
                        help="results-api as scripts/dev-stack.sh starts it")
    parser.add_argument("--db-api-url", default=os.environ.get("MR_DB_API_URL",
                                                               "http://127.0.0.1:8080"),
                        help="db-api serving genetics_results (env MR_DB_API_URL). The "
                             "default is only a guess at the port, which is why `ground` "
                             "asserts the dataset rather than trusting it: on the daly dev VM "
                             ":8080 is a staging db-api on genetics_results_dev and fails "
                             "that check; the main checkout's is on :8085")
    parser.add_argument("--run-dir", default=DEV_STACK_RUN_DIR,
                        help="dev-stack.sh's run directory, for INTERNAL_API_SECRET")
    parser.add_argument("--groups", default=",".join(GROUPS),
                        help=f"comma-separated subset of {', '.join(GROUPS)}")
    args = parser.parse_args()
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        die(f"unknown group(s) {', '.join(unknown)}; choose from {', '.join(GROUPS)}")

    try:
        mcp = siblings.resolve_matching("genetics-mcp-server")
    except siblings.SiblingError as exc:
        die(str(exc))
    if mcp is None:
        die("no genetics-mcp-server checkout found beside this one")
    if not os.path.exists(os.path.join(mcp, ".venv", "bin", "python")):
        die(f"{mcp}/.venv is missing; create the checkout's venv first")

    for group in groups:
        print(f"[{group}]")
        if group == "ground":
            group_ground(args, mcp)
        else:
            skip(group, f"{NOT_YET[group]}: not implemented yet")

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
