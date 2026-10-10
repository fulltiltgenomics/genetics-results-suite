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
            instruments: the premise proper. A set's lead is its max-pip variant with
            maf >= --maf-floor; a set with none contributes no instrument, so a rare pip-1.0
            coding variant, which carries no outcome power, is never the instrument.
  zhou      Zhou 2021's OAS1 anchor: cis credible-set leads of the eQTL Catalogue SomaScan
            plasma set qtd000584 through COVID-19 HGI R7 A2/B2/C2, Wald per SOMAmer against
            Supplementary Table 2 (A2). Ruled on OAS1's direction alone; r is reported only.
  d13       Zhang's deCODE replication (Supplementary Data 13): deCODE pseudo credible sets,
            whose maf is NULL, so the floor reads the annotation AF (FinnGen, else gnomAD
            AF_nfe), through IIBDGC IBD, ruled like b.
  c         published anchors through genetics.mr.run(): LDL-C -> CHD (OR 1.3-1.7 per SD,
            genome-wide), the cis anchors PCSK9, IL6R and OAS1's A2 and B2 ruled on direction
            with |z| >= 1.96, BMI -> T2D on direction; IL12B -> psoriasis, IL23R -> Crohn's,
            OAS1 -> C2 and CRP -> CHD reported, not ruled. A refusal of a ruled anchor fails
            its check. A published magnitude is ruled only within one cohort family, so
            OAS1's against Zhou (HGI R4; ours R7) is reported beside ours, not ruled.
  b2        b1's comparison through mr.run() in cis mode (ivw_correlated where K > 1) on a
            deterministic subset of Data 9 (tier-1 plus every 10th protein), reported beside
            b1's lead-only Wald on the same subset, which is what the check rules on (like b);
            Steiger stands in for the reverse run.
  a         estimator identity against the R MendelianRandomization/TwoSampleMR reference
            fixtures: the checkout's tests/test_sdk_mr_estimators.py passes.
  envelope  one exposure to many outcomes, timed: decides whether a precomputed instrument
            table is in scope.

RULING FOR b (the kill criterion): per-protein log-OR estimates against Data 9's IIBDGC IBD
column reach r >= ceiling - SE(r), SE(r) = (1 - r^2) / sqrt(n - 1), and every tier-1 protein (IL12B, CD6, MXRA8, CXCL9, IFNG, RSPO3,
IL18) whose estimate here has |z| >= 1.96 has Data 9's direction; a tier-1 protein below that
is listed as underpowered and one absent on either side is listed, and neither is counted.
Otherwise STOP: once the allele-frequency checks have verified harmonisation, a miss means
credible sets as instruments are unfit for MR, and the module is descoped rather than tuned.
The one-SE tolerance is there because the ceiling is an upper bound (below) and r's own
sampling error is of the same size as the margins measured against it, so r below the ceiling
by less than its SE does not distinguish the instruments from the ceiling. b0's ruled
estimate is the fixed-effect IVW over every harmonised Data 8 instrument, because Data 9 is
itself IVW over those instruments; the lead-only Wald is printed beside it, not ruled on.

The ceiling is sqrt(rel_ours * rel_theirs) + rho * mean(se_ours * se_theirs) /
sqrt(var_ours * var_theirs). The first term is the attenuation both sides' sampling error
imposes; the second is the error correlation the two sides share because their outcome samples
are nested (assumed: Zhang's IIBDGC release is inside ours; the rho > 1 warning is the only
test of it) and a first-order Wald SE carries only outcome error. The two errors correlate at
rho = sqrt(n_eff_theirs / n_eff_ours), n_eff = 4 / (1/cases + 1/controls), only when both sides
instrument the same variant; where the leads differ the correlation is rho times the LD
between the two variants, so the shared term is an upper bound and the ceiling is an upper
bound, conservative toward STOP (most b1 leads are not Zhang's Data 8 instruments). The
case/control split of Zhang's release was not found in any file on this machine, so the
ieu-a-31 row uses n_eff = N, which overstates n_eff and is also conservative toward STOP. The bare r > 0.9 criterion was withdrawn on 2026-10-10:
Data 9's own reliability (about 0.47) holds r far below 0.9 even with Zhang's own instruments,
and the shared outcome samples correlate the errors, so without the second term the
reliability product is not a ceiling at all (r exceeded it in every ruled IBD comparison).

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
import csv
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import checks
import siblings
from checks import check, die, skip
from paths import DEV_STACK_RUN_DIR

PANEL = "finngen_r12"
DATASET = "genetics_results"
FIXTURE_DIR_REL = os.path.join("tests", "fixtures", "mr")
GROUPS = ("ground", "b0", "b1", "zhou", "d13", "c", "b2", "a", "envelope")
NOT_YET = {
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
    env = sdk_env(args, mcp, secret)
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


# the b groups' data comes from one child per batch of SDK calls, for the same reason the
# ground probe is a child; it reads [name, kwargs] pairs on stdin and answers each frame as
# rows, so the arithmetic below runs in the system python3 and nothing here imports polars
SDK_CALLS = """
import json, sys, time
from genetics_mcp_server import sdk as genetics
out = []
for name, kwargs in json.load(sys.stdin):
    started = time.monotonic()
    try:
        result = getattr(genetics, name)(**kwargs)
    except Exception as exc:
        print(json.dumps({"error": f"{name}: {type(exc).__name__}: {exc}"}))
        sys.exit(3)
    rows = result.to_dicts() if hasattr(result, "to_dicts") else result
    out.append({"call": name, "seconds": round(time.monotonic() - started, 2), "rows": rows})
print(json.dumps({"results": out}))
"""

OUTCOMES = {"ibd": "Inflammatory bowel disease", "cd": "Crohn's disease",
            "uc": "Ulcerative colitis"}
OUTCOME_RESOURCE = "ibd_gwas"
TIER1 = ("IL12B", "CD6", "MXRA8", "CXCL9", "IFNG", "RSPO3", "IL18")
AF_TOLERANCE = 0.2
PALINDROME_MAF = 0.42
MHC = (6, 25_000_000, 34_000_000)
CIS_WINDOW = 1_000_000
GWS_MLOG10P = 7.3
MAF_FLOOR = 0.01
Z95 = 1.959963984540054
CONFOUNDERS = (
    "Zhang's IIBDGC release is smaller than our 2026 IIBDGC file",
    "our UKB_PPP betas are a FinnGen reanalysis (n 42,853) vs Sun 2023's discovery subset "
    "(n 33,995) in Zhang: expect a slope away from 1, not direction changes",
    "Data 9's ORs are rounded to two decimals, which is noise on their side the "
    "reliability below does not see",
)
# every set's lead under both rules, 'all' (max pip) and 'floor' (max pip among maf >= the
# floor, absent when no variant reaches it), so the floor's effect is measured, not assumed.
# The floor reads maf, never aaf: on UKB_PPP aaf equals maf on every row; a NULL maf fails it
UKB_PPP_SQL_MAX_ROWS = 50000
UKB_PPP_SQL = """
WITH ranked AS (
  SELECT trait, cell_type, cs_id, chr, pos, variant, ref, alt, beta, se, mlog10p, pip, aaf, maf,
         ROW_NUMBER() OVER (PARTITION BY resource, dataset, trait, cell_type, cs_id
                            ORDER BY pip DESC, mlog10p DESC, variant) AS rn_all,
         ROW_NUMBER() OVER (PARTITION BY resource, dataset, trait, cell_type, cs_id
                            ORDER BY IF(COALESCE(maf >= {floor}, FALSE), 0, 1), pip DESC,
                                     mlog10p DESC, variant) AS rn_floor
  FROM credible_sets_v
  WHERE resource = 'ukbb' AND dataset = 'UKB_PPP'
),
leads AS (
  SELECT 'all' AS rule, * FROM ranked WHERE rn_all = 1
  UNION ALL
  SELECT 'floor' AS rule, * FROM ranked WHERE rn_floor = 1 AND maf >= {floor}
),
genes AS (
  SELECT symbol, chr, gene_start, gene_end, COUNT(*) OVER (PARTITION BY symbol) AS n_rows
  FROM gene_annotations_v
),
traits AS (
  SELECT l.trait, COUNTIF(l.rule = 'all') AS n_cs, COUNTIF(l.rule = 'floor') AS n_cs_floor,
         (SELECT COUNT(*) FROM genes g WHERE g.symbol = l.trait) AS n_gene_rows
  FROM leads l GROUP BY l.trait
)
SELECT 'trait' AS kind, CAST(NULL AS STRING) AS rule, trait, n_cs, n_cs_floor, n_gene_rows,
       CAST(NULL AS STRING) AS cell_type,
       CAST(NULL AS STRING) AS cs_id, CAST(NULL AS INT64) AS chr, CAST(NULL AS INT64) AS pos,
       CAST(NULL AS STRING) AS variant, CAST(NULL AS STRING) AS ref,
       CAST(NULL AS STRING) AS alt, CAST(NULL AS FLOAT64) AS beta,
       CAST(NULL AS FLOAT64) AS se, CAST(NULL AS FLOAT64) AS mlog10p,
       CAST(NULL AS FLOAT64) AS pip, CAST(NULL AS FLOAT64) AS aaf, CAST(NULL AS FLOAT64) AS maf
FROM traits
UNION ALL
SELECT 'cis_lead', l.rule, l.trait, NULL, NULL, NULL, l.cell_type, l.cs_id, l.chr, l.pos,
       l.variant, l.ref, l.alt, l.beta, l.se, l.mlog10p, l.pip, l.aaf, l.maf
FROM leads l JOIN genes g ON g.symbol = l.trait AND g.n_rows = 1 AND g.chr = l.chr
WHERE l.pos BETWEEN g.gene_start - {window} AND g.gene_end + {window}
"""

# Zhang's IIBDGC outcome sample. Neither Data 9 nor the MOESM2 description states the release
# or its case/control counts; the same workbook's Supplementary Data 12 (reverse MR) gives the
# IIBDGC IBD GWAS as "id:ieu-a-31" with samplesize.exposure 34652 and no split. With only a
# total N, n_eff = 4ab/(a+b) <= a+b = N, so that row is an upper bound on rho for that release (conservative toward STOP).
# The two named releases' counts were supplied with the task and are read from no file here
ZHANG_IIBDGC = (
    ("ieu-a-31 per Zhang Data 12, N 34,652, split not stated: n_eff <= N, rho upper bound, conservative toward STOP",
     None, None, 34652, True),
    ("de Lange 2017 IIBDGC, 25,042 cases / 34,915 controls, UNVERIFIED", 25042, 34915, None,
     False),
    ("Liu 2023 IIBDGC multi-ancestry, 59,957 cases / 1,062,226 controls, UNVERIFIED",
     59957, 1062226, None, False),
)


def sdk_env(args, mcp, secret):
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": os.path.join(mcp, "src"),
        "GENETICS_API_URL": args.results_api_url + "/api",
        "BIGQUERY_API_URL": args.db_api_url,
        "INTERNAL_API_SECRET": secret,
    })
    return env


def sdk_calls(args, mcp, calls):
    """Run SDK calls in the checkout's venv; a call that raises is a data-route failure (exit 2)."""
    python = os.path.join(mcp, ".venv", "bin", "python")
    env = sdk_env(args, mcp, internal_secret(args.run_dir))
    proc = subprocess.run([python, "-c", SDK_CALLS], env=env, cwd=mcp, input=json.dumps(calls),
                          capture_output=True, text=True, timeout=1800)
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        die(f"SDK child exited {proc.returncode} without an answer; "
            f"stderr: {proc.stderr.strip()[-500:]}")
    if "error" in payload:
        die(f"data route failed: {payload['error'][:800]}")
    for res in payload["results"]:
        print(f"  sdk   {res['call']}: {len(res['rows'])} rows in {res['seconds']} s")
    return [res["rows"] for res in payload["results"]]


def read_tsv(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


def write_tsv(args, name, rows):
    if not args.tsv_dir or not rows:
        return
    os.makedirs(args.tsv_dir, exist_ok=True)
    path = os.path.join(args.tsv_dir, name)
    columns = list(dict.fromkeys(k for row in rows for k in row))
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, columns, delimiter="\t", restval="", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  tsv   {path}")


def as_float(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def outcome_codes(args, mcp):
    """IIBDGC trait codes by name from phenotypes_v, never spelled here, and the IBD row's
    n_cases/n_controls there, which the shared-sample term prints beside the summary rows'."""
    (rows,) = sdk_calls(args, mcp, [["phenotypes", {"resource": OUTCOME_RESOURCE}]])
    by_name = {r["trait_name"]: r for r in rows}
    missing = [name for name in OUTCOMES.values() if name not in by_name]
    if missing:
        die(f"phenotypes_v has no {OUTCOME_RESOURCE} trait named {missing}; it has {list(by_name)}")
    ibd = by_name[OUTCOMES["ibd"]]
    return ({key: by_name[name]["trait_original"] for key, name in OUTCOMES.items()},
            (ibd.get("n_cases"), ibd.get("n_controls")))


def n_eff(cases, controls):
    return 4 / (1 / cases + 1 / controls)


def median(xs):
    xs = sorted(xs)
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def shared_sample_rhos(ibd_rows, phenotypes_counts):
    """rho per Zhang IIBDGC assumption, from our IBD summary rows' case/control counts.

    The counts vary by variant (not every cohort carries every variant), so ours is the median
    over the rows this group looked up."""
    cases = [as_float(r.get("n_cases")) for r in ibd_rows]
    controls = [as_float(r.get("n_controls")) for r in ibd_rows]
    cases, controls = [c for c in cases if c], [c for c in controls if c]
    if not cases or not controls:
        die("the IIBDGC IBD summary rows carry no n_cases/n_controls; the ceiling needs them")
    ours_cases, ours_controls = median(cases), median(controls)
    ours = n_eff(ours_cases, ours_controls)
    row_neff = [as_float(r.get("n_eff")) for r in ibd_rows]
    row_neff = [v for v in row_neff if v]
    print(f"  shared outcome samples: our IBD summary rows ({len(ibd_rows)}) n_cases "
          f"{min(cases):.0f}/{ours_cases:.0f}/{max(cases):.0f} and n_controls "
          f"{min(controls):.0f}/{ours_controls:.0f}/{max(controls):.0f} (min/median/max); "
          f"phenotypes_v IBD n_cases {phenotypes_counts[0]} n_controls {phenotypes_counts[1]}")
    if (ours_cases, ours_controls) != tuple(as_float(v) for v in phenotypes_counts):
        print("    they differ: the summary rows' medians are used, they are the samples the "
              "estimates were computed on")
    print(f"    n_eff ours {ours:.0f} = 4/(1/{ours_cases:.0f} + 1/{ours_controls:.0f})"
          + (f"; the rows' own n_eff column, median {median(row_neff):.0f}, is not the ruled "
             "formula and is not used" if row_neff else ""))
    rhos = []
    for label, a, b, total, is_ruled in ZHANG_IIBDGC:
        theirs = n_eff(a, b) if total is None else total
        rho = math.sqrt(theirs / ours)
        print(f"    Zhang [{label}]: n_eff {theirs:.0f}, rho {rho:.3f}"
              + ("  WARNING rho > 1: this n_eff is not nested inside ours" if rho > 1 else ""))
        rhos.append((label.split(",")[0], rho, is_ruled))
    return rhos


def published(mcp):
    """Data 9's IIBDGC column per outcome: protein -> (log OR, SE from the 95% CI, p)."""
    rows = read_tsv(os.path.join(mcp, FIXTURE_DIR_REL, "zhang2025_data9_mr.tsv"))
    out = {key: {} for key in OUTCOMES}
    for row in rows:
        for key in OUTCOMES:
            odds, low, high = (as_float(row[f"{key}_iibdgc_{c}"]) for c in ("or", "ci_low", "ci_high"))
            if odds and low and high:
                out[key][row["protein"]] = (math.log(odds), (math.log(high) - math.log(low)) / (2 * Z95),
                                            as_float(row[f"{key}_iibdgc_p"]))
    return out


def outcome_index(rows):
    return {(r["phenotype"], f"{r['chr']}:{r['pos']}:{r['ref']}:{r['alt']}"): r for r in rows}


def harmonise(ea, oa, eaf, beta, row, eaf_is_maf=False):
    """Orient an exposure (beta and eaf on `ea`) to the outcome row's alt.

    Returns (beta_on_alt, eaf_on_alt, flags) or None when the alleles do not match. With
    `eaf_is_maf` the exposure frequency names no allele, so it can only be compared with the
    outcome's minor-allele frequency: that catches a wrong variant, never a flipped one.
    """
    if (ea, oa) == (row["alt"], row["ref"]):
        oriented, freq = beta, eaf
    elif (ea, oa) == (row["ref"], row["alt"]):
        oriented, freq = -beta, (None if eaf is None or eaf_is_maf else 1 - eaf)
    else:
        return None
    flags = []
    af_out = row.get("af")
    if eaf_is_maf:
        if eaf is not None and af_out is not None and abs(eaf - min(af_out, 1 - af_out)) > AF_TOLERANCE:
            flags.append("maf_discordant")
        freq = None
    elif freq is not None and af_out is not None and abs(freq - af_out) > AF_TOLERANCE:
        flags.append("af_discordant")
    freq_for_maf = eaf if eaf_is_maf and eaf is not None else (freq if freq is not None else af_out)
    if ({row["ref"], row["alt"]} in ({"A", "T"}, {"C", "G"}) and freq_for_maf is not None
            and min(freq_for_maf, 1 - freq_for_maf) > PALINDROME_MAF):
        flags.append("palindromic")
    return oriented, freq, flags


def wald(beta_exp, row):
    ratio = row["beta"] / beta_exp
    se = row["se"] / abs(beta_exp)
    return ratio, se


def ivw(pairs):
    weights = [1 / se ** 2 for _, se in pairs]
    total = sum(weights)
    return sum(w * r for w, (r, _) in zip(weights, pairs)) / total, 1 / math.sqrt(total)


def z_to_p(z):
    return math.erfc(abs(z) / math.sqrt(2))


def variance(xs):
    mean = sum(xs) / len(xs)
    return sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)


def pearson(xs, ys):
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy), sxy / sxx


def reliability(estimates, ses):
    var = variance(estimates)
    return (var - sum(s ** 2 for s in ses) / len(ses)) / var


def se_r(r, n):
    return (1 - r * r) / math.sqrt(n - 1)


def compare(label, ours, theirs, verbose, rhos, ruled=True, tier1=TIER1, ref="Data 9"):
    """Print the ruling's numbers for one estimate set against `ref`; return (passes, summary).

    With several Zhang sample-size assumptions there is a ceiling and a RULING line for each,
    and it passes only if the ruled row says continue. The ruled row is the one grounded in
    Zhang's own supplementary data (ieu-a-31 per Data 12); the other rows come from no file on
    this machine, so they are printed with the same verdict text as sensitivity rows and do
    not gate.
    `verbose` adds the tier-1 table; without `ruled` the ceilings are only reported."""
    common = sorted(set(ours) & set(theirs))
    print(f"  -- {label}: {len(ours)} ours, {len(theirs)} {ref}, {len(common)} on both sides")
    if len(common) < 3:
        print("     too few proteins on both sides for a correlation")
        return False, {"n": len(common)}
    xs = [theirs[p][0] for p in common]
    ys = [ours[p][0] for p in common]
    r, slope_on_theirs = pearson(xs, ys)
    ses_y, ses_x = [ours[p][1] for p in common], [theirs[p][1] for p in common]
    rel_ours, rel_theirs = reliability(ys, ses_y), reliability(xs, ses_x)
    base = (math.sqrt(rel_ours * rel_theirs) if rel_ours > 0 and rel_theirs > 0 else None)
    shared = (sum(a * b for a, b in zip(ses_x, ses_y)) / len(common)
              / math.sqrt(variance(xs) * variance(ys)))
    ser = se_r(r, len(common))
    print(f"     n {len(common)}  r {r:.3f}  SE(r) {ser:.3f}  slope(ours on theirs) "
          f"{slope_on_theirs:.3f}")
    print(f"     reliability ours {rel_ours:.3f}  theirs {rel_theirs:.3f}  "
          + (f"old ceiling sqrt(rel*rel) {base:.3f}" if base is not None
             else "ceiling undefined: a reliability is not positive"))
    ceilings = [] if base is None else [(name, rho, base + rho * shared, ruled_row) for name, rho, ruled_row in rhos]
    if ceilings:
        print(f"     shared-sample term mean(se_x se_y)/sqrt(var_x var_y) {shared:.3f}; new ceiling "
              + "; ".join(f"[{name}] rho {rho:.3f} -> {c:.3f}" for name, rho, c, _ in ceilings))
        print("     each ceiling is an upper bound conservative toward STOP: rho applies in full "
              "only where both sides instrument the same variant, elsewhere the error "
              "correlation is rho times the LD between the two variants")
    summary = {"n": len(common), "r": r, "se_r": ser, "ceiling_old": base,
               "slope": slope_on_theirs, "ceilings": ceilings}
    if not verbose:
        return None, summary
    print("     tier-1   ours logOR (se)        z        theirs logOR (se)      dir")
    flipped, compared, underpowered, absent = [], [], [], []
    for protein in tier1:
        mine, pub = ours.get(protein), theirs.get(protein)
        if mine is None or pub is None:
            absent.append(protein)
            print(f"     {protein:<8} {'absent' if mine is None else 'present':<22} "
                  f"{'':<8} {'absent' if pub is None else 'present':<22} not counted")
            continue
        z = mine[0] / mine[1]
        same = (mine[0] > 0) == (pub[0] > 0)
        if abs(z) < Z95:
            underpowered.append(protein)
            verdict = f"underpowered ({'same' if same else 'opposite'}), not counted"
        else:
            compared.append(protein)
            if not same:
                flipped.append(protein)
            verdict = "same" if same else "FLIPPED"
        print(f"     {protein:<8} {mine[0]:+.3f} ({mine[1]:.3f}){'':<6} {z:<+8.2f} "
              f"{pub[0]:+.3f} ({pub[1]:.3f}){'':<6} {verdict}")
    print(f"     tier-1: {len(compared)} compared {compared}, {len(flipped)} flipped {flipped}; "
          f"underpowered |z| < 1.96 {underpowered}; absent {absent}")
    if not ruled:
        for name, _, ceiling, _ in ceilings:
            print(f"     reported, not ruled [{name}]: r {r:.3f} "
                  f"{'>=' if r >= ceiling - ser else '<'} ceiling - SE(r) {ceiling - ser:.3f} "
                  f"(ceiling {ceiling:.3f}, SE(r) {ser:.3f}, r {(r - ceiling) / ser:+.2f} SE "
                  "from it)")
        return None, summary
    passes = bool(ceilings)
    passes = passes and any(row[3] for row in ceilings)
    for name, _, ceiling, ruled_row in ceilings:
        ok = r >= ceiling - ser and not flipped
        if ruled_row:
            passes &= ok
        print(f"     RULING ({'ruled' if ruled_row else 'sensitivity'}) [{name}]: r >= ceiling - SE(r) AND every compared tier-1 direction "
              f"the same -> continue; otherwise STOP.  r {r:.3f} "
              f"{'>=' if r >= ceiling - ser else '<'} {ceiling - ser:.3f} (ceiling {ceiling:.3f}, "
              f"SE(r) {ser:.3f}, r {(r - ceiling) / ser:+.2f} SE from it); tier-1 "
              f"{len(compared) - len(flipped)}/{len(compared)} compared the same -> "
              f"{'continue' if ok else 'STOP'}")
    if not ceilings:
        print("     RULING: no ceiling -> STOP")
    return passes, summary


def report_comparisons(label, estimates, theirs, rhos, confounders=CONFOUNDERS, tier1=TIER1,
                       ref="Data 9"):
    if confounders:
        print("  known confounders:")
    for item in confounders:
        print(f"    - {item}")
    passes, _ = compare(f"{label} vs {ref} IIBDGC IBD", estimates["ibd"], theirs["ibd"], True,
                        rhos, tier1=tier1, ref=ref)
    # the rhos are IBD's: on CD and UC the printed ceiling only indicates
    for key in ("cd", "uc"):
        compare(f"{label} vs {ref} IIBDGC {key.upper()} (extra, not ruled on)",
                estimates[key], theirs[key], False, rhos, ref=ref)
    return passes


def print_drops(drops):
    print("  drops:")
    for reason, count in drops.items():
        print(f"    {count:>5}  {reason}")


def group_b0(args, mcp):
    codes, phenotypes_counts = outcome_codes(args, mcp)
    theirs = published(mcp)
    wanted = set().union(*(set(t) for t in theirs.values()))
    instruments = read_tsv(os.path.join(mcp, FIXTURE_DIR_REL, "zhang2025_data8_instruments.tsv"))
    proteins_d8 = {r["protein"] for r in instruments}
    drops = {"Data 9 proteins with an IIBDGC estimate and no Data 8 instruments":
             len(wanted - proteins_d8)}
    used = [r for r in instruments if r["protein"] in wanted]
    print(f"  Data 8: {len(instruments)} instruments for {len(proteins_d8)} proteins; "
          f"{len(used)} for the {len(wanted & proteins_d8)} with a Data 9 IIBDGC estimate")

    rsids = sorted({r["rsid"] for r in used})
    (resolved,) = sdk_calls(args, mcp, [["search", {"rsids": rsids}]])
    by_rsid = {r["rsid"]: r["variants"] or [] for r in resolved}
    pos_mismatch, multi = [], 0
    for inst in used:
        alleles = {inst["ea"], inst["oa"]}
        hits = [v for v in by_rsid.get(inst["rsid"], []) if set(v.split(":")[2:]) == alleles]
        multi += len(by_rsid.get(inst["rsid"], [])) > 1
        if hits:
            inst["resolved"] = "search"
            inst["candidates"] = hits
            if any(h.split(":")[1] != inst["pos_grch38"] for h in hits):
                pos_mismatch.append(f"{inst['rsid']} {inst['chr']}:{inst['pos_grch38']} -> {hits}")
        else:
            # not resolved to these alleles: both spellings, and the outcome rows decide
            inst["resolved"] = "unresolved" if not by_rsid.get(inst["rsid"]) else "allele_mismatch"
            base = f"{inst['chr']}:{inst['pos_grch38']}"
            inst["candidates"] = [f"{base}:{inst['oa']}:{inst['ea']}",
                                  f"{base}:{inst['ea']}:{inst['oa']}"]
    counts = {k: sum(i["resolved"] == k for i in used) for k in ("search", "unresolved", "allele_mismatch")}
    print(f"  rsID resolution: {counts['search']} to an id with the instrument's alleles, "
          f"{counts['unresolved']} not found (chr:pos spellings tried), "
          f"{counts['allele_mismatch']} found but with other alleles (chr:pos spellings tried); "
          f"{multi} rsIDs map to several ids; {len(pos_mismatch)} resolved to another position")
    for line in pos_mismatch[:10]:
        print(f"    position differs: {line}")

    variants = sorted({v for i in used for v in i["candidates"]})
    (rows,) = sdk_calls(args, mcp, [["summary_stats", {
        "phenotypes": list(codes.values()), "variants": variants, "resource": OUTCOME_RESOURCE}]])
    index = outcome_index(rows)
    rhos = shared_sample_rhos([r for r in rows if r["phenotype"] == codes["ibd"]],
                              phenotypes_counts)

    per_instrument, estimates, ivws, af_flags = [], {k: {} for k in OUTCOMES}, {k: {} for k in OUTCOMES}, 0
    status_counts = {}
    for key, code in codes.items():
        by_protein = {}
        for inst in used:
            if inst["protein"] not in theirs[key]:
                continue
            row = next((index[(code, v)] for v in inst["candidates"] if (code, v) in index), None)
            record = {"outcome": key, "protein": inst["protein"], "rsid": inst["rsid"],
                      "resolved": inst["resolved"], "ea": inst["ea"], "oa": inst["oa"],
                      "eaf": inst["eaf"], "beta_exp": inst["beta"], "se_exp": inst["se"],
                      "p_exp": inst["p"]}
            if row is None:
                record["status"] = "no outcome row"
            else:
                record["variant"] = f"{row['chr']}:{row['pos']}:{row['ref']}:{row['alt']}"
                record["af_out"] = row.get("af")
                h = harmonise(inst["ea"], inst["oa"], as_float(inst["eaf"]), float(inst["beta"]), row)
                if h is None:
                    record["status"] = "alleles do not match the outcome row"
                else:
                    oriented, freq, flags = h
                    ratio, se = wald(oriented, row)
                    record.update({"beta_exp_alt": oriented, "eaf_alt": freq,
                                   "beta_out": row["beta"], "se_out": row["se"],
                                   "ratio": ratio, "se_ratio": se, "flags": ",".join(flags)})
                    record["status"] = "flagged" if flags else "used"
                    if flags:
                        af_flags += key == "ibd"
                    else:
                        z_exp = abs(float(inst["beta"]) / float(inst["se"]))
                        by_protein.setdefault(inst["protein"], []).append(
                            (float(inst["p"]), -z_exp, ratio, se))
            if key == "ibd":
                status_counts[record["status"]] = status_counts.get(record["status"], 0) + 1
            per_instrument.append(record)
        for protein, items in by_protein.items():
            # Data 8 prints p = 0 for the strongest instruments, so ties fall to the exposure z
            _, _, ratio, se = min(items)
            estimates[key][protein] = (ratio, se, z_to_p(ratio / se))
            est, se = ivw([(r, s) for _, _, r, s in items])
            ivws[key][protein] = (est, se, z_to_p(est / se))

    print(f"  IBD instruments: {status_counts}; AF/palindrome flags (IBD) {af_flags}, "
          "flagged instruments are left out of both estimates")
    with_estimate = set(theirs["ibd"]) & proteins_d8
    drops["IBD: proteins whose every instrument lacked an outcome row or was flagged"] = \
        len(with_estimate - set(estimates["ibd"]))
    print_drops(drops)
    passes = report_comparisons("b0 fixed-effect IVW over every harmonised Data 8 instrument",
                                ivws, theirs, rhos)
    print("  -- extra, not ruled on: the same instruments, lead-only Wald (the strongest per protein):")
    for key in OUTCOMES:
        compare(f"b0 lead-only Wald vs Data 9 IIBDGC {key.upper()}", estimates[key], theirs[key],
                key == "ibd", rhos, ruled=False)
    write_tsv(args, "b0_instruments.tsv", per_instrument)
    write_tsv(args, "b0_proteins.tsv", protein_table(theirs, estimates, ivws))
    check("b0: Data 8 instruments, IVW vs Data 9 IIBDGC IBD meets the ruling",
          passes, "see the RULING line above")


def protein_table(theirs, estimates, ivws=None):
    rows = []
    for key in OUTCOMES:
        for protein in sorted(set(theirs[key]) | set(estimates[key])):
            row = {"outcome": key, "protein": protein}
            for name, source in (("theirs", theirs[key]), ("wald", estimates[key]),
                                 ("ivw", (ivws or {}).get(key, {}))):
                if protein in source:
                    row.update({f"{name}_logor": source[protein][0], f"{name}_se": source[protein][1],
                                f"{name}_p": source[protein][2]})
            rows.append(row)
    return rows


def b1_measure(args, mcp, inside_b2=False):
    """b1's measurement and report; returns what group_b1 rules on and b2 compares beside."""
    codes, phenotypes_counts = outcome_codes(args, mcp)
    theirs = published(mcp)
    floor = args.maf_floor
    query = UKB_PPP_SQL.format(floor=repr(floor), window=CIS_WINDOW)
    (rows,) = sdk_calls(args, mcp, [["sql", {"query": query, "max_rows": UKB_PPP_SQL_MAX_ROWS}]])
    if len(rows) >= UKB_PPP_SQL_MAX_ROWS:
        die(f"the UKB_PPP query returned {len(rows)} rows, its max_rows: the answer is truncated")
    traits = {r["trait"]: r for r in rows if r["kind"] == "trait"}
    all_leads = [r for r in rows if r["kind"] == "cis_lead" and r["rule"] == "all"]
    leads = [r for r in rows if r["kind"] == "cis_lead" and r["rule"] == "floor"]
    cell_types = sorted({str(r["cell_type"]) for r in all_leads})
    n_cs = sum(t["n_cs"] for t in traits.values())
    n_cs_floor = sum(t["n_cs_floor"] for t in traits.values())
    print(f"  UKB_PPP: {len(traits)} traits, {n_cs} credible sets, {len(all_leads)} cis "
          f"credible-set leads (cell_type values {cell_types})")
    lost_all = sorted(p for p, t in traits.items() if t["n_cs"] and not t["n_cs_floor"])
    print(f"  lead rule: per set the max-pip variant with maf >= {floor} (--maf-floor); a set "
          f"with none contributes no instrument. Removed by the floor: {n_cs - n_cs_floor} of "
          f"{n_cs} sets, {len(all_leads) - len(leads)} of {len(all_leads)} cis sets; "
          f"{len(lost_all)} proteins lost every set, of them in Data 9 IBD: "
          f"{sorted(set(lost_all) & set(theirs['ibd']))}")
    # the column is named for the alt allele, but if no lead has it above 0.5 it is a minor
    # allele frequency, which cannot tell a flipped allele from a correct one
    aafs = [r["aaf"] for r in leads if r["aaf"] is not None]
    aaf_is_maf = bool(aafs) and max(aafs) <= 0.5
    if aaf_is_maf:
        print(f"  UKB_PPP aaf is a minor-allele frequency (max {max(aafs)} over {len(aafs)} cis "
              "leads), not the alt's: the AF check compares it with the outcome's MAF, which "
              "catches a wrong variant but cannot verify orientation")

    wanted = sorted(set(theirs["ibd"]))
    drops, stage, tier1_drops = {}, wanted, []
    def narrow(reason, keep):
        nonlocal stage
        kept = [p for p in stage if keep(p)]
        drops[reason] = len(stage) - len(kept)
        tier1_drops.extend(f"{p}: {reason}" for p in stage if p in TIER1 and p not in kept)
        stage = kept

    cis, cis_all = {}, {}
    for lead in leads:
        cis.setdefault(lead["trait"], []).append(lead)
    for lead in all_leads:
        cis_all.setdefault(lead["trait"], []).append(lead)
    narrow("not a UKB_PPP credible-set trait", lambda p: p in traits)
    narrow("trait symbol not in gene_annotations_v", lambda p: traits[p]["n_gene_rows"] > 0)
    narrow("trait symbol has several gene_annotations_v rows", lambda p: traits[p]["n_gene_rows"] == 1)
    narrow("no credible-set lead within 1 Mb of the gene", lambda p: p in cis_all)
    narrow(f"no cis credible set has a variant with maf >= {floor}", lambda p: p in cis)
    narrow("no cis lead with mlog10p >= 7.3",
           lambda p: any((lead["mlog10p"] or 0) >= GWS_MLOG10P for lead in cis[p]))
    def outside_mhc(lead):
        return not (lead["chr"] == MHC[0] and MHC[1] <= lead["pos"] <= MHC[2])
    narrow("every significant cis lead is in the MHC",
           lambda p: any((lead["mlog10p"] or 0) >= GWS_MLOG10P and outside_mhc(lead) for lead in cis[p]))
    def strongest(protein_leads):
        candidates = [lead for lead in protein_leads
                      if (lead["mlog10p"] or 0) >= GWS_MLOG10P and outside_mhc(lead)]
        return max(candidates, key=lambda lead: (lead["mlog10p"], lead["pip"]), default=None)
    instrument = {protein: strongest(cis[protein]) for protein in stage}

    def describe(lead):
        return ("none" if lead is None else
                f"{lead['variant']} (maf {lead['maf']}, pip {lead['pip']}, "
                f"mlog10p {lead['mlog10p']}, {lead['cs_id']})")
    print(f"  tier-1 leads, max pip -> max pip with maf >= {floor}:")
    for protein in TIER1:
        before, after = strongest(cis_all.get(protein, [])), instrument.get(protein)
        changed = (before or {}).get("variant") != (after or {}).get("variant")
        print(f"    {protein:<6} {'CHANGED' if changed else 'unchanged'}: {describe(before)}"
              + (f" -> {describe(after)}" if changed else ""))

    variants = sorted({v for lead in instrument.values()
                       for v in (lead["variant"],
                                 f"{lead['chr']}:{lead['pos']}:{lead['alt']}:{lead['ref']}")})
    (out_rows,) = sdk_calls(args, mcp, [["summary_stats", {
        "phenotypes": list(codes.values()), "variants": variants, "resource": OUTCOME_RESOURCE}]])
    index = outcome_index(out_rows)
    rhos = shared_sample_rhos([r for r in out_rows if r["phenotype"] == codes["ibd"]],
                              phenotypes_counts)

    estimates, per_protein, status_counts, af_flags = lead_estimates(
        instrument, codes, index, aaf_is_maf, "ibd")
    tier1_drops += [f"{r['protein']}: lead {r['variant']} {r['status']} {r.get('flags', '')}".rstrip()
                    for r in per_protein
                    if r["outcome"] == "ibd" and r["protein"] in TIER1 and r["status"] != "used"]
    for status in ("no outcome row", "alleles do not match the outcome row", "flagged"):
        drops[f"IBD: lead cis-pQTL {status}"] = status_counts.get(status, 0)
    print(f"  IBD lead instruments: {status_counts}; AF/palindrome flags (IBD) {af_flags}")
    print_drops(drops)
    for line in tier1_drops:
        print(f"    tier-1 dropped  {line}")
    if inside_b2:
        # b2 prints the known confounders once, beside its own comparison
        print("  b1 full set (group b1's ruling, not b2's):")
    passes = report_comparisons("b1 own UKB_PPP cis leads, Wald", estimates, theirs, rhos,
                                confounders=() if inside_b2 else CONFOUNDERS)
    write_tsv(args, "b1_instruments.tsv", per_protein)
    write_tsv(args, "b1_proteins.tsv", protein_table(theirs, estimates))
    return {"passes": passes, "estimates": estimates, "theirs": theirs, "rhos": rhos,
            "codes": codes}


def group_b1(args, mcp):
    passes = b1_measure(args, mcp)["passes"]
    check("b1: own UKB_PPP cis leads, Wald vs Data 9 IIBDGC IBD meets the ruling",
          passes, "see the RULING line above")


def lead_estimates(instrument, codes, index, eaf_is_maf, counted):
    """Wald ratio per protein and outcome from one lead each: (estimates, records, the
    `counted` outcome's status counts, its AF/palindrome flags). A flagged lead is left out."""
    estimates, records, status_counts, af_flags = {k: {} for k in codes}, [], {}, 0
    for key, code in codes.items():
        for protein, lead in sorted(instrument.items()):
            swapped = f"{lead['chr']}:{lead['pos']}:{lead['alt']}:{lead['ref']}"
            row = index.get((code, lead["variant"])) or index.get((code, swapped))
            record = {"outcome": key, "protein": protein, "variant": lead["variant"],
                      "cell_type": lead["cell_type"], "cs_id": lead["cs_id"], "pip": lead["pip"],
                      "mlog10p_exp": lead["mlog10p"], "beta_exp": lead["beta"],
                      "se_exp": lead["se"], "aaf_exp": lead["aaf"], "maf_exp": lead["maf"],
                      "af_source": lead.get("af_source")}
            if row is None:
                record["status"] = "no outcome row"
            else:
                record.update({"outcome_variant": f"{row['chr']}:{row['pos']}:{row['ref']}:{row['alt']}",
                               "af_out": row.get("af")})
                h = harmonise(lead["alt"], lead["ref"], lead["aaf"], lead["beta"], row,
                              eaf_is_maf=eaf_is_maf)
                if h is None:
                    record["status"] = "alleles do not match the outcome row"
                else:
                    oriented, _, flags = h
                    ratio, se = wald(oriented, row)
                    record.update({"beta_out": row["beta"], "se_out": row["se"], "ratio": ratio,
                                   "se_ratio": se, "flags": ",".join(flags)})
                    record["status"] = "flagged" if flags else "used"
                    if flags:
                        af_flags += key == counted
                    else:
                        estimates[key][protein] = (ratio, se, z_to_p(ratio / se))
            if key == counted:
                status_counts[record["status"]] = status_counts.get(record["status"], 0) + 1
            records.append(record)
    return estimates, records, status_counts, af_flags


# every row of every cis credible set, not only the leads, because the floor may have to read
# an annotation AF where the set's maf is NULL; `{key}` names the protein the reference joins on
CIS_ROWS_SQL = """
WITH genes AS (
  SELECT symbol, chr, gene_start, gene_end, COUNT(*) OVER (PARTITION BY symbol) AS n_rows
  FROM gene_annotations_v
)
SELECT {key} AS key, c.trait, c.trait_original, c.cell_type, c.cs_id, c.chr, c.pos, c.variant,
       c.ref, c.alt, c.beta, c.se, c.mlog10p, c.pip, c.aaf, c.maf
FROM credible_sets_v c JOIN genes g ON g.symbol = c.trait AND g.n_rows = 1 AND g.chr = c.chr
WHERE c.resource = '{resource}' AND c.dataset = '{dataset}' AND {key} IN ({keys})
  AND c.pos BETWEEN g.gene_start - {window} AND g.gene_end + {window}
"""
SETS_SQL = """
SELECT {key} AS key, COUNT(DISTINCT CONCAT(c.trait_original, '|', c.cell_type, '|', c.cs_id)) AS n_sets
FROM credible_sets_v c
WHERE c.resource = '{resource}' AND c.dataset = '{dataset}' AND {key} IN ({keys})
GROUP BY 1
"""
CIS_ROWS_MAX = 50000
# results-api refuses a larger variant_annotation batch with a 422
ANNOTATION_BATCH = 2000
SAFE_KEY = re.compile(r"^[A-Za-z0-9._-]+$")
CS_ID_VARIANT = re.compile(r"^chr([0-9XY]+)_([0-9]+)_([A-Z]+)_([A-Z]+)$")


def annotation_af(args, mcp, variants):
    """Alt-allele AF per variant: FinnGen's annotation first, gnomAD AF_nfe for the rest.

    A variant found under the swapped spelling takes 1 - AF. Returns {variant: (af, source)}."""
    found = {}
    def take(rows, column, source, wanted):
        by_key = {f"{r['chr']}:{r['pos']}:{r['ref']}:{r['alt']}": as_float(r.get(column))
                  for r in rows}
        for v in wanted:
            chrom, pos, ref, alt = v.split(":")
            af, flip = by_key.get(v), by_key.get(f"{chrom}:{pos}:{alt}:{ref}")
            if af is not None and not math.isnan(af):
                found[v] = (af, source)
            elif flip is not None and not math.isnan(flip):
                found[v] = (1 - flip, source)
    for source, column in (("finngen", "AF"), ("gnomad", "AF_nfe")):
        wanted = [v for v in variants if v not in found]
        if not wanted:
            break
        batches = sdk_calls(args, mcp, [
            ["variant_annotation", {"variants": wanted[i:i + ANNOTATION_BATCH], "source": source}]
            for i in range(0, len(wanted), ANNOTATION_BATCH)])
        take([r for rows in batches for r in rows], column, f"{source} {column}", wanted)
    return found


def cis_instruments(args, mcp, resource, dataset, key_sql, keys, floor):
    """Per key, the lead of its strongest cis credible set under the ruled lead rule.

    A set's lead is its max-pip variant (ties by mlog10p) whose MAF reaches `floor`; the MAF
    is the set's own where it has one, the annotation AF's where it is NULL. The strongest set
    is the one whose lead has the largest mlog10p, among leads at genome-wide significance
    outside the MHC. Returns (instrument, drops, report) for the caller to print."""
    bad = [k for k in keys if not SAFE_KEY.match(k)]
    if bad:
        die(f"protein keys unsafe to quote into SQL: {bad[:5]}")
    fmt = {"key": key_sql, "resource": resource, "dataset": dataset,
           "keys": ", ".join(f"'{k}'" for k in sorted(keys)), "window": CIS_WINDOW}
    rows, set_counts = sdk_calls(args, mcp, [
        ["sql", {"query": CIS_ROWS_SQL.format(**fmt), "max_rows": CIS_ROWS_MAX}],
        ["sql", {"query": SETS_SQL.format(**fmt), "max_rows": CIS_ROWS_MAX}]])
    if len(rows) >= CIS_ROWS_MAX:
        die(f"the {dataset} cis query returned {len(rows)} rows, its max_rows: truncated")
    n_sets = {r["key"]: r["n_sets"] for r in set_counts}

    need = sorted({r["variant"] for r in rows if r["maf"] is None or r["aaf"] is None})
    ann = annotation_af(args, mcp, need) if need else {}
    sources = Counter()
    for r in rows:
        if r["maf"] is not None and r["aaf"] is not None:
            r["af_source"] = "credible set"
        elif r["variant"] in ann:
            af, r["af_source"] = ann[r["variant"]]
            r["aaf"] = af if r["aaf"] is None else r["aaf"]
            r["maf"] = min(af, 1 - af) if r["maf"] is None else r["maf"]
        else:
            r["af_source"] = "none (fails the floor)"
        sources[r["af_source"]] += 1

    sets = {}
    for r in rows:
        sets.setdefault((r["key"], r["trait_original"], r["cell_type"], r["cs_id"]), []).append(r)
    leads, no_floor, pip_ties, one_pip = {}, 0, 0, 0
    for set_key, members in sets.items():
        one_pip += len({m["pip"] for m in members}) == 1 and len(members) > 1
        eligible = sorted((m for m in members if m["maf"] is not None and m["maf"] >= floor),
                          key=lambda m: (-(m["pip"] or 0), -(m["mlog10p"] or 0), m["variant"]))
        if not eligible:
            no_floor += 1
            continue
        pip_ties += len(eligible) > 1 and eligible[1]["pip"] == eligible[0]["pip"]
        leads.setdefault(set_key[0], []).append(eligible[0])

    def outside_mhc(lead):
        return not (lead["chr"] == MHC[0] and MHC[1] <= lead["pos"] <= MHC[2])
    cis_keys = {k for k, *_ in sets}
    drops, stage = {}, sorted(keys)
    def narrow(reason, keep):
        nonlocal stage
        kept = [k for k in stage if keep(k)]
        drops[reason] = len(stage) - len(kept)
        stage = kept
    narrow(f"no {dataset} credible set at all", lambda k: n_sets.get(k, 0) > 0)
    narrow("credible sets, none within 1 Mb of the gene (trans only, or no single "
           "gene_annotations_v row)", lambda k: k in cis_keys)
    narrow(f"no cis set has a variant with MAF >= {floor}", lambda k: k in leads)
    narrow("no cis lead with mlog10p >= 7.3",
           lambda k: any((ld["mlog10p"] or 0) >= GWS_MLOG10P for ld in leads[k]))
    narrow("every significant cis lead is in the MHC",
           lambda k: any((ld["mlog10p"] or 0) >= GWS_MLOG10P and outside_mhc(ld) for ld in leads[k]))
    instrument = {k: max((ld for ld in leads[k] if (ld["mlog10p"] or 0) >= GWS_MLOG10P
                          and outside_mhc(ld)), key=lambda ld: (ld["mlog10p"], ld["pip"]))
                  for k in stage}

    per_key = Counter(k for k, *_ in sets)
    named = 0
    for k, lead in instrument.items():
        m = CS_ID_VARIANT.match(lead["cs_id"] or "")
        named += bool(m) and ":".join(m.groups()) == lead["variant"]
    report = {"rows": len(rows), "sets": len(sets), "keys_cis": len(cis_keys),
              "sets_per_key": sorted(per_key.values()), "sources": dict(sources),
              "no_floor": no_floor, "pip_ties": pip_ties, "one_pip": one_pip,
              "lead_is_cs_id": named,
              "cs_id_names_variant": sum(bool(CS_ID_VARIANT.match(ld["cs_id"] or ""))
                                         for ld in instrument.values()),
              "n_sets": n_sets, "cis_sets": dict(per_key)}
    return instrument, drops, report


def print_cis_report(dataset, report, floor):
    spk = report["sets_per_key"]
    print(f"  {dataset}: {report['rows']} cis rows in {report['sets']} cis sets for "
          f"{report['keys_cis']} proteins; sets per protein median {median(spk) if spk else 0} "
          f"max {max(spk, default=0)}")
    print(f"  AF for the MAF floor, by row: {report['sources']}")
    print(f"  lead rule: max pip, ties by mlog10p, at MAF >= {floor}; {report['no_floor']} sets "
          f"have no variant there and drop; {report['pip_ties']} chosen leads tie on pip with "
          f"the next eligible variant; {report['one_pip']} sets of more than one variant give "
          "every variant the same pip")
    print(f"  chosen leads that are the variant the cs_id names: {report['lead_is_cs_id']} of "
          f"{report['cs_id_names_variant']} whose cs_id names one")


COVID_RESOURCE = "covid_hgi"
COVID_OUTCOMES = {"a2": "COVID_A2", "b2": "COVID_B2", "c2": "COVID_C2"}
ZHOU_SOMASCAN = ("qtd000584", "QTD000584")
ZHOU_ANCHOR = "OAS1.10361.25.3"


def group_zhou(args, mcp):
    resource, dataset = ZHOU_SOMASCAN
    (meta,) = sdk_calls(args, mcp, [["sql", {"query":
        "SELECT dataset, dataset_id, resource, subdataset_of, author, n_samples, data_type, "
        "description FROM datasets_v WHERE dataset_id IN ('QTD000584', 'interval') "
        "OR dataset = 'QTD000584'"}]])
    for m in meta:
        print(f"  datasets_v: {m['dataset']} ({m['resource']}, subdataset_of "
              f"{m['subdataset_of']}): author {m['author']}, n_samples {m['n_samples']}, "
              f"{m['data_type']}, {m['description'][:120]}")
    qtd = next((m for m in meta if m["dataset"] == dataset), None)
    interval = next((m for m in meta if m["dataset_id"] == "interval"), None)
    if qtd is None:
        die(f"datasets_v has no {dataset}")
    is_interval = (qtd["author"] == "Sun_2018" and interval is not None
                   and qtd["n_samples"] == interval["n_samples"])
    print(f"  cohort: {dataset} is "
          + ("INTERVAL SomaScan (author Sun_2018, the INTERVAL pQTL paper, and n_samples equal "
             "to the registry's INTERVAL row), the cohort Zhou used" if is_interval else
             "NOT shown to be INTERVAL by the registry; Zhou used INTERVAL SomaScan (Sun 2018). "
             "Running anyway"))

    fixture = read_tsv(os.path.join(mcp, FIXTURE_DIR_REL, "zhou2021_st2_somamer_a2.tsv"))
    fixture = [r for r in fixture if r["method"] in ("Wald ratio", "Inverse variance weighted")]
    if len({r["somamer"] for r in fixture}) != len(fixture):
        die("zhou2021_st2_somamer_a2.tsv: a SOMAmer has several Wald/IVW rows")
    theirs = {r["somamer"]: (float(r["beta"]), float(r["se"]), as_float(r["p"])) for r in fixture}
    print(f"  Zhou ST2 (A2 only; ST4/ST6 for B2/C2 are not extracted): {len(theirs)} SOMAmers; "
          "joined on the SOMAmer id, which qtd000584's trait_original carries before '..', "
          "finer than the symbol")
    instrument, drops, report = cis_instruments(
        args, mcp, resource, dataset, "SPLIT(c.trait_original, '..')[OFFSET(0)]", list(theirs),
        args.maf_floor)
    print_cis_report(dataset, report, args.maf_floor)
    print_drops(drops)
    symbols = {k.split(".")[0] for k in theirs}
    print(f"  ({len({ld['trait'] for ld in instrument.values()} & symbols)} of the instrumented "
          "SOMAmers' traits equal the symbol in Zhou's id)")

    variants = sorted({v for ld in instrument.values()
                       for v in (ld["variant"], f"{ld['chr']}:{ld['pos']}:{ld['alt']}:{ld['ref']}")})
    (out_rows,) = sdk_calls(args, mcp, [["summary_stats", {
        "phenotypes": list(COVID_OUTCOMES.values()), "variants": variants,
        "resource": COVID_RESOURCE}]])
    missing = [c for c in COVID_OUTCOMES.values() if not any(r["phenotype"] == c for r in out_rows)]
    if missing:
        die(f"summary_stats returned no {COVID_RESOURCE} rows for {missing}")
    aafs = [ld["aaf"] for ld in instrument.values() if ld["aaf"] is not None]
    eaf_is_maf = bool(aafs) and max(aafs) <= 0.5
    print(f"  exposure eaf: alt AF (max {max(aafs, default=None)}), so the AF check verifies "
          "orientation" if not eaf_is_maf else "  exposure aaf is a MAF: orientation unverified")
    estimates, records, status_counts, af_flags = lead_estimates(
        instrument, COVID_OUTCOMES, outcome_index(out_rows), eaf_is_maf, "a2")
    print(f"  A2 lead instruments: {status_counts}; AF/palindrome flags (A2) {af_flags}")

    common = sorted(set(estimates["a2"]) & set(theirs))
    print(f"  per SOMAmer, ours (Wald on the lead) vs Zhou ST2 A2, n {len(common)}:")
    print("    somamer              lead               A2 ours (se)     z      Zhou (se)        "
          "dir   B2 ours (se)     C2 ours (se)")
    for k in common:
        mine, pub = estimates["a2"][k], theirs[k]
        extra = "  ".join(f"{estimates[o][k][0]:+.3f} ({estimates[o][k][1]:.3f})"
                          if k in estimates[o] else f"{'-':<15}" for o in ("b2", "c2"))
        print(f"    {k:<20} {instrument[k]['variant']:<18} {mine[0]:+.3f} ({mine[1]:.3f})  "
              f"{mine[0] / mine[1]:+6.2f}  {pub[0]:+.3f} ({pub[1]:.3f})  "
              f"{'same' if (mine[0] > 0) == (pub[0] > 0) else 'opp':<5} {extra}")
    _, summary = compare("zhou A2 vs Zhou ST2 (reported only)", estimates["a2"], theirs, False, [],
                         ref="Zhou ST2")
    if summary.get("ceiling_old") is not None:
        base, ser = summary["ceiling_old"], summary["se_r"]
        print(f"     information only: HGI R7 is not Zhou's R4 and the overlap term has no stated "
              f"n, so only the base ceiling {base:.3f}; r {summary['r']:.3f} "
              f"{'>=' if summary['r'] >= base - ser else '<'} ceiling - SE(r) {base - ser:.3f}")
    write_tsv(args, "zhou_instruments.tsv", records)

    ok = True
    for o in ("a2", "b2"):
        est = estimates[o].get(ZHOU_ANCHOR)
        good = est is not None and est[0] < 0 and abs(est[0] / est[1]) >= Z95
        ok &= good
        print(f"  RULING {ZHOU_ANCHOR} on {COVID_OUTCOMES[o]}: protective per SD (log OR < 0) with "
              f"|z| >= 1.96 -> "
              + ("no estimate" if est is None else
                 f"{est[0]:+.3f} (se {est[1]:.3f}, z {est[0] / est[1]:+.2f}, OR "
                 f"{math.exp(est[0]):.3f})") + f" -> {'ok' if good else 'FAIL'}")
    if ZHOU_ANCHOR in theirs:
        print(f"    Zhou ST2 A2 {ZHOU_ANCHOR}: log OR {theirs[ZHOU_ANCHOR][0]:+.3f} (OR "
              f"{math.exp(theirs[ZHOU_ANCHOR][0]):.3f})")
    check(f"zhou: {ZHOU_ANCHOR} protective with |z| >= 1.96 on COVID_A2 and COVID_B2", ok,
          "see the RULING lines above")


DECODE = ("decode", "deCODE_pQTL_2021")
TIER1_D13 = ("IL12B", "MXRA8", "CXCL9", "IFNG", "IL18")
CONFOUNDERS_D13 = (
    "Zhang's IIBDGC release is smaller than our 2026 IIBDGC file",
    "deCODE credible sets here are pseudo sets, not fine-mapped ones",
    "Data 13 names no instrument SNP, so whether our lead is Zhang's deCODE instrument cannot "
    "be checked",
    "Data 13's ORs are rounded to two decimals",
)


def published_data13(mcp):
    """Data 13's IIBDGC column per outcome: protein -> (log OR, SE from the 95% CI, p)."""
    out = {key: {} for key in OUTCOMES}
    for row in read_tsv(os.path.join(mcp, FIXTURE_DIR_REL, "zhang2025_data13_decode_mr.tsv")):
        odds, low, high = (as_float(row[f"iibdgc_{c}"]) for c in ("or", "ci_low", "ci_high"))
        if odds and low and high:
            out[row["outcome"]][row["protein"]] = (
                math.log(odds), (math.log(high) - math.log(low)) / (2 * Z95),
                as_float(row["iibdgc_p"]))
    return out


def group_d13(args, mcp):
    codes, phenotypes_counts = outcome_codes(args, mcp)
    theirs = published_data13(mcp)
    keys = sorted(set().union(*(set(t) for t in theirs.values())))
    print(f"  Data 13 IIBDGC: {', '.join(f'{k} {len(v)}' for k, v in theirs.items())} proteins "
          f"({len(keys)} distinct)")
    # the tier-1 proteins Data 13 lacks are looked up too, so their absence is measured here
    instrument, drops, report = cis_instruments(args, mcp, *DECODE, "c.trait",
                                                sorted(set(keys) | set(TIER1_D13)), args.maf_floor)
    print_cis_report(DECODE[1], report, args.maf_floor)
    print_drops(drops)
    for protein in TIER1_D13:
        lead = instrument.get(protein)
        print(f"    tier-1 {protein:<6} in Data 13 IBD {protein in theirs['ibd']}, deCODE sets "
              f"{report['n_sets'].get(protein, 0)} (cis {report['cis_sets'].get(protein, 0)}): "
              + ("no cis instrument" if lead is None else
                 f"{lead['variant']} (pip {lead['pip']}, mlog10p {lead['mlog10p']}, AF "
                 f"{lead['aaf']} from {lead['af_source']}, {lead['trait_original']} {lead['cs_id']})"))

    variants = sorted({v for ld in instrument.values()
                       for v in (ld["variant"], f"{ld['chr']}:{ld['pos']}:{ld['alt']}:{ld['ref']}")})
    (out_rows,) = sdk_calls(args, mcp, [["summary_stats", {
        "phenotypes": list(codes.values()), "variants": variants, "resource": OUTCOME_RESOURCE}]])
    rhos = shared_sample_rhos([r for r in out_rows if r["phenotype"] == codes["ibd"]],
                              phenotypes_counts)
    estimates, records, status_counts, af_flags = lead_estimates(
        instrument, codes, outcome_index(out_rows), False, "ibd")
    print(f"  IBD lead instruments: {status_counts}; AF/palindrome flags (IBD) {af_flags} "
          "(the exposure AF is the annotation's alt AF)")
    passes = report_comparisons("d13 deCODE cis leads, Wald", estimates, theirs, rhos,
                                CONFOUNDERS_D13, TIER1_D13, "Data 13")
    write_tsv(args, "d13_instruments.tsv", records)
    write_tsv(args, "d13_proteins.tsv", protein_table(theirs, estimates))
    check("d13: deCODE cis leads, Wald vs Data 13 IIBDGC IBD meets the ruling", passes,
          "see the RULING line above")


def group_a(args, mcp):
    tests = os.path.join(mcp, "tests", "test_sdk_mr_estimators.py")
    if not os.path.exists(tests):
        die(f"{tests} is missing")
    cmd = ["uv", "run", "pytest", "-q", "-p", "no:randomly", "tests/test_sdk_mr_estimators.py"]
    try:
        proc = subprocess.run(cmd, cwd=mcp, capture_output=True, text=True, timeout=900)
    except FileNotFoundError:
        die("uv is not on PATH; it runs the checkout's test suite")
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    tally = lines[-1] if lines else proc.stderr.strip()[-300:]
    print(f"  {' '.join(cmd)} (in {mcp}): exit {proc.returncode}: {tally}")
    if proc.returncode not in (0, 1):
        die(f"pytest could not run the estimator tests (exit {proc.returncode}): "
            f"{proc.stderr.strip()[-500:] or tally}")
    check("a: the estimator tests against the R reference fixtures pass", proc.returncode == 0,
          tally)


# mr.run() per job in the checkout's venv, one child for all of a group's jobs. A
# GeneticsUsageError is the module refusing (D6, pseudo sets, no outcome rows) and is a
# result; another GeneticsError is the data route failing (exit 2: nothing was measured), any
# other exception the module crashing, which is a failed check (exit 1)
MR_RUNS = """
import json, sys, time
from genetics_mcp_server.sdk import mr
from genetics_mcp_server.sdk.errors import GeneticsError, GeneticsUsageError

def scalars(d):
    return {k: v for k, v in (d or {}).items()
            if v is None or isinstance(v, (bool, int, float, str))}

def result(r):
    frame = r.instruments.frame
    cols = [c for c in ("variant", "beta", "se", "maf", "proxy_for") if c in frame.columns]
    return {"exposure": r.exposure, "outcome": r.outcome, "headline": r.headline,
            "estimates": {m: scalars(e) for m, e in r.estimates.items()},
            "checks": [] if r.checks is None else r.checks.to_dicts(),
            "overlap": list(r.overlap), "units": dict(r.harmonised.units),
            "steiger": scalars(r.sensitivity.get("steiger")),
            "instruments": frame.select(cols).to_dicts(), "notes": list(r.notes)}

out = []
for job in json.load(sys.stdin):
    started = time.monotonic()
    rec = {"label": job["label"]}
    try:
        res = mr.run(job["exposure"], outcomes=[tuple(o) for o in job["outcomes"]],
                     **job.get("kwargs", {}))
        rec.update(status="ok", results=[result(r) for r in res.results],
                   failed=res.failed.to_dicts(), requests=dict(res.requests))
    except GeneticsUsageError as exc:
        rec.update(status="refused", error=str(exc))
    except GeneticsError as exc:
        rec.update(status="failed", failure="data route", error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        rec.update(status="failed", failure="module exception",
                   error=f"{type(exc).__name__}: {exc}")
    rec["seconds"] = round(time.monotonic() - started, 2)
    out.append(rec)
print(json.dumps({"results": out}, default=str))
"""


def mr_runs(args, mcp, jobs):
    """Each job is {label, exposure, outcomes, kwargs}; outcomes always go as a list, so every
    answer is an MRBatch and a per-outcome refusal lands in `failed` instead of raising."""
    python = os.path.join(mcp, ".venv", "bin", "python")
    env = sdk_env(args, mcp, internal_secret(args.run_dir))
    proc = subprocess.run([python, "-c", MR_RUNS], env=env, cwd=mcp, input=json.dumps(jobs),
                          capture_output=True, text=True, timeout=3600)
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        die(f"mr.run child exited {proc.returncode} without an answer; "
            f"stderr: {proc.stderr.strip()[-500:]}")
    runs = payload["results"]
    total = sum((r.get("requests") or {}).get("total", 0) for r in runs)
    print(f"  mr.run: {len(runs)} runs, {sum(r['status'] == 'ok' for r in runs)} ran, "
          f"{sum(r['status'] == 'refused' for r in runs)} refused, "
          f"{sum(r['status'] == 'failed' for r in runs)} failed; {total} requests, "
          f"{sum(r['seconds'] for r in runs):.1f} s")
    return runs


def refusal_kind(message):
    if "pseudo credible sets" in message:
        return "pseudo-credible-set exposure"
    if re.search(r"no credible set fine-mapped within the window|trans sets only", message):
        return "D6: no cis credible set in the window"
    if "no credible-set rows" in message:
        return "no credible-set rows for the trait"
    if "no credible set with a lead at MAF" in message:
        return "no lead passes the MAF floor / p threshold / MHC"
    if re.search(r"is in LD panel \S+, so none can be pruned", message):
        return "no lead passing floor / threshold is in the LD panel"
    if re.search(r"gene_annotations_v row carries no", message):
        return "gene_annotations_v row has no position"
    if "gene_annotations_v has" in message:
        return "trait is not one HGNC symbol in gene_annotations_v"
    if re.search(r"no instrument .*(outcome|proxy)|no outcome row", message, re.I):
        return "no outcome rows for any instrument"
    return "other: " + re.sub(r"[0-9]+", "#", message)[:100]


def run_failures(runs):
    """(refusals Counter, failure lines) over whole-run refusals and per-outcome ones."""
    refusals, failures = Counter(), []
    for run in runs:
        if run["status"] == "refused":
            refusals[refusal_kind(run["error"])] += 1
        elif run["status"] == "failed":
            failures.append(f"{run['label']} [{run['failure']}]: {run['error'][:400]}")
        for f in run.get("failed", []):
            refusals[f"per outcome: {refusal_kind(f['reason'])}"] += 1
    return refusals, failures


def headline(result):
    e = result["estimates"][result["headline"]]
    return e["estimate"], e["se"], e["p"], e.get("n_instruments")


def failing_checks(result):
    return [f"{c['check']}: {c['detail']}" for c in result["checks"] if c.get("status") == "fail"]


def by_phenotype(run, phenotype):
    # MRResult.outcome is "<resource> <phenotype>"
    return next((r for r in run.get("results", []) if r["outcome"].endswith(" " + phenotype)),
                None)


FINNGEN = "finngen"
UKB_PPP = "UKB_PPP"
LDL_OR_RANGE = (1.3, 1.7)
# the primary papers' numbers, copied from their abstracts (Lyall's SD from Table 2's footnote).
# Not a fixture TSV: tests/fixtures/mr/ is generated by convert_fixtures.py from pinned
# workbooks, and these come from no workbook. The last field is why the magnitude is not ruled
TRAIT_ANCHORS = {
    "BMI -> T2D": ("Lyall 2017 JAMA Cardiol, doi 10.1001/jamacardio.2016.5804, UK Biobank",
                   2.53, 2.04, 3.13, "per 1 SD BMI (4.83 kg/m2)",
                   "another cohort family (FinnGen vs UK Biobank)"),
    "CRP -> CHD": ("CCGC 2011 BMJ 342:d548, doi 10.1136/bmj.d548, 47 studies, CRP-gene SNPs",
                   1.00, 0.90, 1.13, "per 1 SD ln CRP",
                   "another cohort family, and the exposure unit is not known to be SD of "
                   "ln CRP"),
}
# exposure, outcomes as (resource, phenotype, expected sign or 0 for null, ruled, expectation),
# instrument mode. A ruled anchor passes when the sign matches with |z| >= 1.96
C_ANCHORS = (
    ("LDL-C -> CHD", {"dataset": "nmr_ukbb_est", "trait": "LDL_C"}, "genome_wide", 0.01,
     [(FINNGEN, "I9_CHD", +1, True, "OR per SD in 1.3-1.7 (the epic's trait-level range)")]),
    ("PCSK9", {"dataset": UKB_PPP, "trait": "PCSK9"}, "cis", None,
     [(FINNGEN, "I9_CHD", +1, True, "higher PCSK9 level raises LDL-C and CHD risk (R46L "
       "lowers both)"),
      (FINNGEN, "E4_HYPERCHOL", +1, True, "higher PCSK9 level raises LDL-C")]),
    ("IL6R", {"dataset": UKB_PPP, "trait": "IL6R"}, "cis", None,
     [(FINNGEN, "M13_RHEUMA", -1, True, "Asp358Ala raises soluble IL6R and lowers membrane "
       "signalling: protective"),
      (FINNGEN, "I9_CHD", -1, True, "Asp358Ala raises soluble IL6R and is protective for CHD")]),
    ("IL23R", {"dataset": UKB_PPP, "trait": "IL23R"}, "cis", None,
     [(FINNGEN, "K11_CD_STRICT2", +1, False, "R381Q (rs11209026) loses IL23R function and "
       "protects from Crohn's, so more functional IL23R raises risk; whether the measured "
       "soluble level tracks function is not established")]),
    ("IL12B", {"dataset": UKB_PPP, "trait": "IL12B"}, "cis", None,
     [(FINNGEN, "L12_PSORIASIS", +1, False, "ustekinumab (anti-p40) treats psoriasis, so more "
       "functional p40 raises risk; the measured plasma level may not track function at this "
       "cis-pQTL (an epitope or measured-level effect), so reported, not ruled")]),
    # by resource: the registry's eqtl_catalogue row has no `dataset`, so run() refuses the
    # dataset name the views hold, QTD000584
    ("OAS1", {"resource": ZHOU_SOMASCAN[0], "trait": "OAS1"}, "cis", None,
     [(COVID_RESOURCE, "COVID_A2", -1, True, "Zhou 2021 ST2 A2 OR 0.544: protective"),
      (COVID_RESOURCE, "COVID_B2", -1, True, "protective (Zhou 2021)"),
      (COVID_RESOURCE, "COVID_C2", -1, False, "protective, weaker (susceptibility)")]),
    ("BMI -> T2D", {"dataset": "FinnGen_R14", "trait": "Body_mass_index,_inverse-rank_normalized"},
     "genome_wide", 0.01,
     [(FINNGEN, "T2D", +1, True, "higher BMI raises T2D risk (Lyall 2017)")]),
    ("CRP -> CHD", {"dataset": "FinnGen_kanta", "trait":
                    "C_reactive_protein_[Mass/volume]_in_Serum_or_Plasma_[mg/l],_sample-wise_median"},
     "genome_wide", 0.01,
     [(FINNGEN, "I9_CHD", 0, False, "null (CCGC 2011); genome-wide CRP instruments include "
       "non-CRP loci, unlike CCGC's CRP-gene SNPs")]),
)
# ruled anchors whose refusal by run() is the expected answer and stays NOT MEASURED; any
# other ruled anchor's refusal fails its check. Empty: IL23R, the one anchor refused so far,
# is reported rather than ruled
EXPECTED_REFUSALS_C = frozenset()
NOT_MEASURED_C = (
    ("SBP -> stroke", "no fine-mapped SBP exposure: FinnGen has no core blood-pressure "
     "endpoint with credible sets, and Open Targets' SBP studies are pseudo credible sets, "
     "which run() refuses as exposures"),
    ("HDL-C -> CHD pattern", "nmr_ukbb_est HDL_C genome-wide is ~450 requests and ~160 s "
     "(879 credible sets), over this group's time budget; no primary-paper number is pinned "
     "for it"),
)
PUBLISHED_ZHOU = "zhou2021_st2_somamer_a2.tsv"


def zhou_anchor_ci(mcp):
    rows = read_tsv(os.path.join(mcp, FIXTURE_DIR_REL, PUBLISHED_ZHOU))
    row = next((r for r in rows if r["somamer"] == ZHOU_ANCHOR and r["method"] == "Wald ratio"),
               None)
    if row is None:
        die(f"{PUBLISHED_ZHOU} has no Wald-ratio row for {ZHOU_ANCHOR}")
    return float(row["or"]), float(row["ci_low"]), float(row["ci_high"])


def group_c(args, mcp):
    zhou_or = zhou_anchor_ci(mcp)
    jobs = [{"label": label, "exposure": exposure,
             "outcomes": [[res, pheno] for res, pheno, *_ in outcomes],
             "kwargs": {"instruments": mode, **({"r2_prune": r2} if r2 is not None else {})}}
            for label, exposure, mode, r2, outcomes in C_ANCHORS]
    runs = {r["label"]: r for r in mr_runs(args, mcp, jobs)}
    table, failed_routes, ruled_fail = [], [], []
    for label, exposure, mode, r2, outcomes in C_ANCHORS:
        run = runs[label]
        print(f"  -- {label}: {exposure.get('dataset') or exposure.get('resource')} "
              f"{exposure['trait']}, {mode}"
              f"{f', r2_prune {r2}' if r2 is not None else ', default r2_prune'}: {run['status']}"
              f", {(run.get('requests') or {}).get('total', '?')} requests, {run['seconds']} s")
        if run["status"] == "failed":
            failed_routes.append(f"{label} [{run['failure']}]: {run['error'][:400]}")
            print(f"     FAILED ({run['failure']}): {run['error'][:600]}")
            continue
        if run["status"] == "refused":
            print(f"     REFUSED ({refusal_kind(run['error'])}): {run['error'][:600]}")
        for resource, pheno, sign, ruled, expectation in outcomes:
            name = f"{label} -> {pheno}" if "->" not in label else label
            row = {"anchor": name, "outcome": f"{resource}:{pheno}", "expected_sign":
                   {1: "+", -1: "-", 0: "null"}[sign], "ruled": ruled, "expectation": expectation}
            result = by_phenotype(run, pheno) if run["status"] == "ok" else None
            if result is None:
                reason = (run.get("error") if run["status"] == "refused" else next(
                    (f["reason"] for f in run.get("failed", []) if f["phenotype"] == pheno),
                    "no result and no refusal reason"))
                if ruled and name not in EXPECTED_REFUSALS_C:
                    row.update(verdict="REFUSED, ruled: FAIL", detail=reason[:300])
                    check(f"c: {name} refused by run()", False, reason[:300])
                    ruled_fail.append(name)
                else:
                    row.update(verdict="REFUSED, not measured", detail=reason[:300])
                    if ruled:
                        skip(f"c: {name}", f"refused by run() (expected): {reason[:200]}")
                table.append(row)
                continue
            est, se, p, k = headline(result)
            z = est / se
            e = result["estimates"][result["headline"]]
            odds = (math.exp(est), math.exp(e["ci_low"]), math.exp(e["ci_high"]))
            fails = failing_checks(result)
            row.update(K=k, method=result["headline"], estimate=est, se=se, p=p, z=z,
                       OR=odds[0], OR_low=odds[1], OR_high=odds[2],
                       exposure_unit=result["units"].get("exposure"),
                       outcome_unit=result["units"].get("outcome"),
                       overlap=f"{result['overlap'][0]}: {result['overlap'][1]}",
                       failing_checks="; ".join(fails),
                       methods="; ".join(f"{m} {e['estimate']:+.3f} ({e['se']:.3f})"
                                         for m, e in result["estimates"].items()
                                         if e.get("estimate") is not None and e.get("se")))
            sig = abs(z) >= Z95
            if sign == 0:
                verdict = ("null agrees (|z| < 1.96)" if not sig
                           else f"NOT null ({'+' if est > 0 else '-'}, |z| >= 1.96)")
            else:
                same = (est > 0) == (sign > 0)
                verdict = (("direction ok" if same else "DIRECTION WRONG") if sig else
                           f"underpowered (|z| < 1.96, {'same' if same else 'opposite'} sign)")
            ok = sign != 0 and sig and (est > 0) == (sign > 0)
            if label == "LDL-C -> CHD":
                in_range = LDL_OR_RANGE[0] <= odds[0] <= LDL_OR_RANGE[1]
                ok &= in_range
                verdict += (f"; OR {odds[0]:.3f} {'inside' if in_range else 'OUTSIDE'} "
                            f"{LDL_OR_RANGE[0]}-{LDL_OR_RANGE[1]}")
            if pheno == "COVID_A2":
                # the magnitude rule holds only within one cohort family: Zhou's anchor is HGI
                # release 4, ours R7 (a later, larger release without 23andMe)
                inside = zhou_or[1] <= odds[0] <= zhou_or[2]
                verdict += (f"; ours OR {odds[0]:.3f} ({odds[1]:.3f}-{odds[2]:.3f}) beside Zhou "
                            f"ST2 (HGI R4) {zhou_or[0]:.3f} ({zhou_or[1]:.3f}-{zhou_or[2]:.3f}), "
                            f"{'inside' if inside else 'outside'} its CI; release differs "
                            "(HGI R7 here): magnitude reported")
            if label in TRAIT_ANCHORS:
                paper, p_or, lo, hi, unit, why = TRAIT_ANCHORS[label]
                verdict += (f"; published {p_or:.2f} ({lo:.2f}-{hi:.2f}) {unit}, ours "
                            f"{'inside' if lo <= odds[0] <= hi else 'outside'} it (not ruled: "
                            f"{why})")
            row["verdict"] = verdict
            if ruled:
                check(f"c: {name} {verdict}", ok, f"{est:+.3f} (se {se:.3f}, z {z:+.2f})")
                if not ok:
                    ruled_fail.append(name)
            table.append(row)
    for label, reason in NOT_MEASURED_C:
        skip(f"c: {label}", reason)
        table.append({"anchor": label, "verdict": "NOT MEASURED", "detail": reason})

    print("  anchor                    K   method          estimate    se      p         "
          "OR (95% CI)            exp  verdict")
    for row in table:
        if "estimate" not in row:
            print(f"  {row['anchor']:<25} {row['verdict']}: {row.get('detail', '')[:160]}")
            continue
        print(f"  {row['anchor']:<25} {row['K']!s:<3} {row['method']:<15} {row['estimate']:+.4f}"
              f"   {row['se']:.4f}  {row['p']:<9.2e} {row['OR']:.3f} ({row['OR_low']:.3f}-"
              f"{row['OR_high']:.3f})  {row['expected_sign']:<4} "
              f"{'RULED ' if row['ruled'] else 'report'} {row['verdict']}")
        print(f"  {'':<25} units {row['exposure_unit']} -> {row['outcome_unit']}; overlap "
              f"{row['overlap'][:150]}")
        print(f"  {'':<25} failing checks: {row['failing_checks'] or 'none'}")
        print(f"  {'':<25} all methods: {row['methods']}")
        print(f"  {'':<25} expectation: {row['expectation']}")
    write_tsv(args, "c_anchors.tsv", table)
    crashes = [f for f in failed_routes if "[module exception]" in f]
    check("c: no mr.run call crashed (a module exception is a defect, not a refusal)",
          not crashes, "; ".join(crashes))
    routes = [f for f in failed_routes if f not in crashes]
    if routes:
        die("c: mr.run's data route failed for " + "; ".join(routes))


B2_STRIDE = 10


def group_b2(args, mcp):
    """mr.run()'s cis headline is reported beside b1's lead-only Wald on the same subset, and
    the check rules on the latter. The shared-sample ceiling assumes both sides' errors come
    from the same variant; a pruned multi-instrument IVW instruments variants Zhang did not,
    so it overstates the ceiling further than b1 already does, and the epic's ruled
    measurement is the lead-only form."""
    b1 = b1_measure(args, mcp, inside_b2=True)
    theirs, rhos, codes = b1["theirs"], b1["rhos"], b1["codes"]
    proteins = sorted(theirs["ibd"])
    subset = sorted(set(TIER1) | set(proteins[::B2_STRIDE]))
    print(f"  [b2] subset: the tier-1 seven plus every {B2_STRIDE}th of the {len(proteins)} "
          f"Data 9 IIBDGC IBD proteins in symbol order: {len(subset)} runs (a full pass is "
          f"~{len(proteins) * 6 // 60} min at ~6 s per run)")
    jobs = [{"label": p, "exposure": {"dataset": UKB_PPP, "trait": p},
             "outcomes": [[OUTCOME_RESOURCE, codes[k]] for k in OUTCOMES],
             "kwargs": {"instruments": "cis"}} for p in subset]
    runs = mr_runs(args, mcp, jobs)
    refusals, failures = run_failures(runs)
    estimates, records = {k: {} for k in OUTCOMES}, []
    methods, ks = Counter(), Counter()
    for run in runs:
        rec = {"protein": run["label"], "status": run["status"], "seconds": run["seconds"],
               "requests": (run.get("requests") or {}).get("total"),
               "error": (run.get("error") or "")[:300]}
        for key, code in codes.items():
            result = by_phenotype(run, code)
            if result is None:
                continue
            est, se, p, k = headline(result)
            estimates[key][run["label"]] = (est, se, p)
            rec.update({f"{key}_method": result["headline"], f"{key}_K": k,
                        f"{key}_estimate": est, f"{key}_se": se, f"{key}_p": p})
            if key == "ibd":
                methods[result["headline"]] += 1
                ks[k] += 1
                rec["steiger_correct_direction"] = result["steiger"].get("correct_causal_direction")
                rec["steiger_pval"] = result["steiger"].get("steiger_pval")
                rec["failing_checks"] = "; ".join(failing_checks(result))
        records.append(rec)
    print(f"  IBD headline methods {dict(methods)}; K distribution {dict(sorted(ks.items()))}")
    print("  refusals (counted separately from failures):")
    for kind, n in refusals.most_common():
        print(f"    {n:>4}  {kind}")
    print(f"  failures (data route or module exception): {len(failures)}")
    for line in failures:
        print(f"    {line}")
    print("  tier-1 runs:")
    for rec in records:
        if rec["protein"] in TIER1:
            print(f"    {rec['protein']:<6} {rec['status']:<7} "
                  + (f"{rec.get('ibd_method')} K={rec.get('ibd_K')} "
                     f"{rec.get('ibd_estimate', float('nan')):+.3f} "
                     f"(se {rec.get('ibd_se', float('nan')):.3f})" if 'ibd_estimate' in rec
                     else rec["error"][:200]))

    sub_b1 = {k: {p: v for p, v in b1["estimates"][k].items() if p in subset} for k in OUTCOMES}
    print("  known confounders:")
    for item in CONFOUNDERS:
        print(f"    - {item}")
    passes, b1_sum = compare("b1 lead-only Wald on the b2 subset vs Data 9 IIBDGC IBD (ruled)",
                             sub_b1["ibd"], theirs["ibd"], True, rhos)
    _, b2_sum = compare("b2 mr.run cis headline (ivw_correlated where K > 1) vs Data 9 IIBDGC "
                        "IBD (reported)", estimates["ibd"], theirs["ibd"], True, rhos,
                        ruled=False)
    for key in ("cd", "uc"):
        compare(f"b2 mr.run cis headline vs Data 9 IIBDGC {key.upper()} (extra, not ruled on)",
                estimates[key], theirs[key], False, rhos)
    print("  b2 (reported) beside b1 (ruled), IBD, same subset:")
    for name, summary in (("b1 Wald", b1_sum), ("b2 mr.run", b2_sum)):
        if "r" not in summary:
            print(f"    {name:<10} n {summary['n']}: too few for a correlation")
            continue
        print(f"    {name:<10} n {summary['n']}  r {summary['r']:.3f}  SE(r) "
              f"{summary['se_r']:.3f}  "
              + "; ".join(f"[{c[0]}] ceiling {c[2]:.3f}, r {(summary['r'] - c[2]) / summary['se_r']:+.2f} SE"
                          for c in summary["ceilings"]))
    common = sorted(set(sub_b1["ibd"]) & set(estimates["ibd"]))
    if len(common) >= 3:
        r, slope = pearson([sub_b1["ibd"][p][0] for p in common],
                           [estimates["ibd"][p][0] for p in common])
        print(f"  b2 headline vs b1 Wald, ours against ours: n {len(common)}, r {r:.3f}, "
              f"slope {slope:.3f}")

    print("  reverse direction (IBD -> protein): refused by design in v1. Its exposure would be "
          "IIBDGC credible sets, which are pseudo sets, and run() refuses pseudo-credible-set "
          "exposures; no reverse run is attempted. Steiger from the forward run instead (IBD "
          "is a case-control consortium: the sample prevalence is used, so the outcome's "
          "variance explained is approximate):")
    for rec in records:
        p = rec["protein"]
        if p in TIER1 and "ibd_estimate" in rec and abs(rec["ibd_estimate"] / rec["ibd_se"]) >= Z95:
            print(f"    {p:<6} correct_causal_direction {rec.get('steiger_correct_direction')}, "
                  f"steiger_pval {rec.get('steiger_pval')}")
    write_tsv(args, "b2_runs.tsv", records)
    write_tsv(args, "b2_proteins.tsv", [
        {"outcome": key, "protein": p,
         **{f"{name}_{col}": src[p][i] for name, src in (("theirs", theirs[key]),
                                                         ("b2", estimates[key]),
                                                         ("b1", sub_b1[key])) if p in src
            for i, col in enumerate(("logor", "se", "p"))}}
        for key in OUTCOMES for p in sorted(set(estimates[key]) | (set(theirs[key]) & set(subset)))])
    check("b2: b1's lead-only Wald on the b2 subset vs Data 9 IIBDGC IBD meets the ruling "
          "(mr.run's headline reported beside it)", passes, "see the RULING line above")
    crashes = [f for f in failures if "[module exception]" in f]
    check("b2: no mr.run call crashed (a module exception is a defect, not a refusal)",
          not crashes, "; ".join(crashes))
    routes = [f for f in failures if f not in crashes]
    if routes:
        die(f"b2: {len(routes)} mr.run calls failed on the data route")


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
    parser.add_argument("--tsv-dir",
                        help="write the b groups' per-instrument and per-protein tables here; "
                             "nothing is written without it")
    parser.add_argument("--maf-floor", type=float, default=MAF_FLOOR,
                        help="b1, zhou, d13: a credible set's lead is its max-pip variant with "
                             "maf at or above this; a set with none contributes no instrument")
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
        started = time.monotonic()
        if group == "ground":
            group_ground(args, mcp)
        elif group == "b0":
            group_b0(args, mcp)
        elif group == "b1":
            group_b1(args, mcp)
        elif group == "zhou":
            group_zhou(args, mcp)
        elif group == "d13":
            group_d13(args, mcp)
        elif group == "c":
            group_c(args, mcp)
        elif group == "b2":
            group_b2(args, mcp)
        elif group == "a":
            group_a(args, mcp)
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
