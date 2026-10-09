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
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
import time
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
Z95 = 1.959963984540054
CONFOUNDERS = (
    "Zhang's IIBDGC release is smaller than our 2026 IIBDGC file",
    "our UKB_PPP betas are a FinnGen reanalysis (n 42,853) vs Sun 2023's discovery subset "
    "(n 33,995) in Zhang: expect a slope away from 1, not direction changes",
    "Data 9's ORs are rounded to two decimals, which is noise on their side the "
    "reliability below does not see",
)
UKB_PPP_SQL = f"""
WITH leads AS (
  SELECT trait, cell_type, cs_id, chr, pos, variant, ref, alt, beta, se, mlog10p, pip, aaf
  FROM credible_sets_v
  WHERE resource = 'ukbb' AND dataset = 'UKB_PPP'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY resource, dataset, trait, cell_type, cs_id
                             ORDER BY pip DESC, mlog10p DESC, variant) = 1
),
genes AS (
  SELECT symbol, chr, gene_start, gene_end, COUNT(*) OVER (PARTITION BY symbol) AS n_rows
  FROM gene_annotations_v
),
traits AS (
  SELECT l.trait, COUNT(*) AS n_cs,
         (SELECT COUNT(*) FROM genes g WHERE g.symbol = l.trait) AS n_gene_rows
  FROM leads l GROUP BY l.trait
)
SELECT 'trait' AS kind, trait, n_cs, n_gene_rows, CAST(NULL AS STRING) AS cell_type,
       CAST(NULL AS STRING) AS cs_id, CAST(NULL AS INT64) AS chr, CAST(NULL AS INT64) AS pos,
       CAST(NULL AS STRING) AS variant, CAST(NULL AS STRING) AS ref,
       CAST(NULL AS STRING) AS alt, CAST(NULL AS FLOAT64) AS beta,
       CAST(NULL AS FLOAT64) AS se, CAST(NULL AS FLOAT64) AS mlog10p,
       CAST(NULL AS FLOAT64) AS pip, CAST(NULL AS FLOAT64) AS aaf
FROM traits
UNION ALL
SELECT 'cis_lead', l.trait, NULL, NULL, l.cell_type, l.cs_id, l.chr, l.pos, l.variant, l.ref,
       l.alt, l.beta, l.se, l.mlog10p, l.pip, l.aaf
FROM leads l JOIN genes g ON g.symbol = l.trait AND g.n_rows = 1 AND g.chr = l.chr
WHERE l.pos BETWEEN g.gene_start - {CIS_WINDOW} AND g.gene_end + {CIS_WINDOW}
"""


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
    """IIBDGC trait codes by name from phenotypes_v, never spelled here."""
    (rows,) = sdk_calls(args, mcp, [["phenotypes", {"resource": OUTCOME_RESOURCE}]])
    by_name = {r["trait_name"]: r["trait_original"] for r in rows}
    missing = [name for name in OUTCOMES.values() if name not in by_name]
    if missing:
        die(f"phenotypes_v has no {OUTCOME_RESOURCE} trait named {missing}; it has {by_name}")
    return {key: by_name[name] for key, name in OUTCOMES.items()}


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


def compare(label, ours, theirs, verbose):
    """Print the ruling's numbers for one estimate set against Data 9; return (passes, summary)."""
    common = sorted(set(ours) & set(theirs))
    print(f"  -- {label}: {len(ours)} ours, {len(theirs)} Data 9, {len(common)} on both sides")
    if len(common) < 3:
        print("     too few proteins on both sides for a correlation")
        return False, {"n": len(common)}
    xs = [theirs[p][0] for p in common]
    ys = [ours[p][0] for p in common]
    r, slope_on_theirs = pearson(xs, ys)
    rel_ours = reliability(ys, [ours[p][1] for p in common])
    rel_theirs = reliability(xs, [theirs[p][1] for p in common])
    ceiling = (math.sqrt(rel_ours * rel_theirs) if rel_ours > 0 and rel_theirs > 0 else None)
    print(f"     n {len(common)}  r {r:.3f}  slope(ours on theirs) {slope_on_theirs:.3f}")
    print(f"     reliability ours {rel_ours:.3f}  theirs {rel_theirs:.3f}  "
          + ("ceiling " + f"{ceiling:.3f}" if ceiling is not None
             else "ceiling undefined: a reliability is not positive"))
    if not verbose:
        return None, {"n": len(common), "r": r, "ceiling": ceiling, "slope": slope_on_theirs}
    print("     tier-1   ours logOR (se)        p         theirs logOR (se)      dir")
    directions_ok = True
    for protein in TIER1:
        mine, pub = ours.get(protein), theirs.get(protein)
        if mine is None or pub is None:
            print(f"     {protein:<8} {'absent' if mine is None else 'present':<22} "
                  f"{'':<9} {'absent' if pub is None else 'present'}")
            continue
        same = (mine[0] > 0) == (pub[0] > 0)
        directions_ok &= same
        print(f"     {protein:<8} {mine[0]:+.3f} ({mine[1]:.3f}){'':<6} {mine[2]:<9.2g} "
              f"{pub[0]:+.3f} ({pub[1]:.3f}){'':<6} {'same' if same else 'FLIPPED'}")
    at_ceiling = ceiling is not None and r >= 0.95 * ceiling
    passes = (r > 0.9 or at_ceiling) and directions_ok
    print(f"     RULING: r > 0.9 (or at the SE-implied ceiling) with tier-1 directions "
          f"reproduced -> continue; otherwise STOP.  r {r:.3f} "
          f"{'>' if r > 0.9 else '<='} 0.9; "
          + (f"r/ceiling {r / ceiling:.3f} ({'>=' if at_ceiling else '<'} 0.95); "
             if ceiling is not None else "no ceiling; ")
          + f"tier-1 directions {'reproduced' if directions_ok else 'NOT reproduced'} "
          f"-> {'continue' if passes else 'STOP'}")
    return passes, {"n": len(common), "r": r, "ceiling": ceiling, "slope": slope_on_theirs}


def report_comparisons(label, estimates, theirs):
    print("  known confounders:")
    for item in CONFOUNDERS:
        print(f"    - {item}")
    passes, _ = compare(f"{label} vs Data 9 IIBDGC IBD", estimates["ibd"], theirs["ibd"], True)
    for key in ("cd", "uc"):
        compare(f"{label} vs Data 9 IIBDGC {key.upper()} (extra, not ruled on)",
                estimates[key], theirs[key], False)
    return passes


def print_drops(drops):
    print("  drops:")
    for reason, count in drops.items():
        print(f"    {count:>5}  {reason}")


def group_b0(args, mcp):
    codes = outcome_codes(args, mcp)
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
    passes = report_comparisons("b0 lead-only Wald", estimates, theirs)
    print("  -- the same instruments, fixed-effect IVW over all that harmonised (Data 9 is IVW):")
    for key in OUTCOMES:
        compare(f"b0 IVW vs Data 9 IIBDGC {key.upper()}", ivws[key], theirs[key], key == "ibd")
    write_tsv(args, "b0_instruments.tsv", per_instrument)
    write_tsv(args, "b0_proteins.tsv", protein_table(theirs, estimates, ivws))
    check("b0: Data 8 instruments, lead-only Wald vs Data 9 IIBDGC IBD meets the ruling",
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


def group_b1(args, mcp):
    codes = outcome_codes(args, mcp)
    theirs = published(mcp)
    (rows,) = sdk_calls(args, mcp, [["sql", {"query": UKB_PPP_SQL, "max_rows": 25000}]])
    traits = {r["trait"]: r for r in rows if r["kind"] == "trait"}
    leads = [r for r in rows if r["kind"] == "cis_lead"]
    cell_types = sorted({str(r["cell_type"]) for r in leads})
    print(f"  UKB_PPP: {len(traits)} traits, {sum(t['n_cs'] for t in traits.values())} credible "
          f"sets, {len(leads)} cis credible-set leads (cell_type values {cell_types})")
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

    cis = {}
    for lead in leads:
        cis.setdefault(lead["trait"], []).append(lead)
    narrow("not a UKB_PPP credible-set trait", lambda p: p in traits)
    narrow("trait symbol not in gene_annotations_v", lambda p: traits[p]["n_gene_rows"] > 0)
    narrow("trait symbol has several gene_annotations_v rows", lambda p: traits[p]["n_gene_rows"] == 1)
    narrow("no credible-set lead within 1 Mb of the gene", lambda p: p in cis)
    narrow("no cis lead with mlog10p >= 7.3",
           lambda p: any((lead["mlog10p"] or 0) >= GWS_MLOG10P for lead in cis[p]))
    def outside_mhc(lead):
        return not (lead["chr"] == MHC[0] and MHC[1] <= lead["pos"] <= MHC[2])
    narrow("every significant cis lead is in the MHC",
           lambda p: any((lead["mlog10p"] or 0) >= GWS_MLOG10P and outside_mhc(lead) for lead in cis[p]))
    instrument = {}
    for protein in stage:
        candidates = [lead for lead in cis[protein]
                      if (lead["mlog10p"] or 0) >= GWS_MLOG10P and outside_mhc(lead)]
        instrument[protein] = max(candidates, key=lambda lead: (lead["mlog10p"], lead["pip"]))

    variants = sorted({v for lead in instrument.values()
                       for v in (lead["variant"],
                                 f"{lead['chr']}:{lead['pos']}:{lead['alt']}:{lead['ref']}")})
    (out_rows,) = sdk_calls(args, mcp, [["summary_stats", {
        "phenotypes": list(codes.values()), "variants": variants, "resource": OUTCOME_RESOURCE}]])
    index = outcome_index(out_rows)

    estimates, per_protein = {k: {} for k in OUTCOMES}, []
    status_counts, af_flags = {}, 0
    for key, code in codes.items():
        for protein, lead in sorted(instrument.items()):
            swapped = f"{lead['chr']}:{lead['pos']}:{lead['alt']}:{lead['ref']}"
            row = index.get((code, lead["variant"])) or index.get((code, swapped))
            record = {"outcome": key, "protein": protein, "variant": lead["variant"],
                      "cell_type": lead["cell_type"], "cs_id": lead["cs_id"], "pip": lead["pip"],
                      "mlog10p_exp": lead["mlog10p"], "beta_exp": lead["beta"],
                      "se_exp": lead["se"], "aaf_exp": lead["aaf"]}
            if row is None:
                record["status"] = "no outcome row"
            else:
                record.update({"outcome_variant": f"{row['chr']}:{row['pos']}:{row['ref']}:{row['alt']}",
                               "af_out": row.get("af")})
                h = harmonise(lead["alt"], lead["ref"], lead["aaf"], lead["beta"], row,
                              eaf_is_maf=aaf_is_maf)
                if h is None:
                    record["status"] = "alleles do not match the outcome row"
                else:
                    oriented, _, flags = h
                    ratio, se = wald(oriented, row)
                    record.update({"beta_out": row["beta"], "se_out": row["se"], "ratio": ratio,
                                   "se_ratio": se, "flags": ",".join(flags)})
                    record["status"] = "flagged" if flags else "used"
                    if flags:
                        af_flags += key == "ibd"
                    else:
                        estimates[key][protein] = (ratio, se, z_to_p(ratio / se))
            if key == "ibd":
                status_counts[record["status"]] = status_counts.get(record["status"], 0) + 1
                if protein in TIER1 and record["status"] != "used":
                    tier1_drops.append(f"{protein}: lead {lead['variant']} {record['status']} "
                                       f"{record.get('flags', '')}".rstrip())
            per_protein.append(record)
    for status in ("no outcome row", "alleles do not match the outcome row", "flagged"):
        drops[f"IBD: lead cis-pQTL {status}"] = status_counts.get(status, 0)
    print(f"  IBD lead instruments: {status_counts}; AF/palindrome flags (IBD) {af_flags}")
    print_drops(drops)
    for line in tier1_drops:
        print(f"    tier-1 dropped  {line}")
    passes = report_comparisons("b1 own UKB_PPP cis leads, Wald", estimates, theirs)
    write_tsv(args, "b1_instruments.tsv", per_protein)
    write_tsv(args, "b1_proteins.tsv", protein_table(theirs, estimates))
    check("b1: own UKB_PPP cis leads, Wald vs Data 9 IIBDGC IBD meets the ruling",
          passes, "see the RULING line above")


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
