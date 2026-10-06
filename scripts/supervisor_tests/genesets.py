import json

from .harness import check, make_body, sup

# Runs as model-authored code would: the bundle, its permissions and the thread cap are
# shipped properties, so they are read from inside an execution rather than off the image.
PROBE = r"""
import errno, json, os, resource

def status_kb(field):
    with open("/proc/self/status") as fh:
        for line in fh:
            if line.startswith(field + ":"):
                return int(line.split()[1])

def write_errno(path):
    try:
        with open(path, "a"):
            return None
    except OSError as exc:
        return exc.errno

out = {"env_dir": os.environ.get("GENETICS_GENESETS_DIR"),
       "polars_max_threads": os.environ.get("POLARS_MAX_THREADS")}
d = out["env_dir"] or ""
out["listing"] = sorted(os.listdir(d)) if os.path.isdir(d) else None
out["manifest_fixture"] = json.load(open(os.path.join(d, "manifest.json"))).get("fixture") if out["listing"] else None
out["write_manifest"] = write_errno(os.path.join(d, "manifest.json"))
out["create_in_dir"] = write_errno(os.path.join(d, "planted"))
out["write_tmpdir"] = write_errno(os.path.join(os.environ["TMPDIR"], "control"))
out["access_w"] = [p for p in [d] + [os.path.join(d, n) for n in out["listing"] or []]
                   if os.access(p, os.W_OK)]
out["stat"] = {os.path.basename(q) or q: [os.stat(q).st_uid, os.stat(q).st_mode]
               for q in [d] + [os.path.join(d, n) for n in out["listing"] or []]}
out["rlimit_as"] = resource.getrlimit(resource.RLIMIT_AS)[0]

import polars
out["polars_threads"] = polars.thread_pool_size()
from genetics import enrichment
cols = enrichment.collections()
out["collections"] = cols.select("collection", "n_sets", "licence").to_dicts()
res = enrichment.ora([f"GENE{i}" for i in range(1, 9)], "coll.a")
out["ora_results"] = res["results"].select("set_id", "overlap", "pval").to_dicts()
out["ora_parameters"] = {k: res["parameters"][k] for k in ("universe_size", "list_size", "sets_tested")}
out["vmsize_kb"] = status_kb("VmSize")
out["vmpeak_kb"] = status_kb("VmPeak")
out["vmrss_kb"] = status_kb("VmRSS")
print("GENESETS " + json.dumps(out))
"""


def test_genesets(server):
    """Only meaningful against an image run-sandbox-local.sh built: it stages the FIXTURE
    bundle, and the ora() expectations below are that fixture's sets. The expected collections
    assume the daly profile (run-sandbox-local.sh's default): with GENESETS_PROFILE=finngen the
    HPO-licensed fixture collection coll.b is dropped by design."""
    status, _, body = server.request("POST", "/execute", body=make_body(code=PROBE))
    if status != 200 or body.get("status") != "ok" or "GENESETS " not in body.get("output", ""):
        check("gene sets: probe script ran", False,
              f"got {status} {body.get('status')} {body.get('error')} {body.get('output', '')[-800:]}")
        return
    p = json.loads(body["output"].split("GENESETS ", 1)[1].splitlines()[0])
    print(f"  gene sets: VmSize {p['vmsize_kb'] // 1024} MiB, VmPeak {p['vmpeak_kb'] // 1024} MiB, "
          f"VmRSS {p['vmrss_kb'] // 1024} MiB, RLIMIT_AS {p['rlimit_as'] // (1 << 20)} MiB; "
          f"collections {p['collections']}; ora {p['ora_parameters']}")

    check("gene sets: GENETICS_GENESETS_DIR is /genetics/genesets",
          p["env_dir"] == "/genetics/genesets", f"got {p['env_dir']!r}")
    check("gene sets: the bundle is present",
          p["listing"] == ["genes.parquet", "manifest.json", "members.parquet", "sets.parquet"],
          f"got {p['listing']}")
    check("gene sets: the local image carries the fixture bundle, marked as such",
          p["manifest_fixture"] is True, f"got {p['manifest_fixture']!r}")

    # the local container runs --read-only, so every write probe below fails with EROFS whoever
    # owns the bundle; they show only that it cannot be written at runtime. Ownership and mode
    # are what the stat check asserts. The TMPDIR write is the control that the probe can see
    # a write succeed at all.
    check("gene sets: write probe control — TMPDIR accepts a write", p["write_tmpdir"] is None,
          f"errno {p['write_tmpdir']}")
    check("gene sets: the bundle directory and every file are root-owned with no group/world write",
          bool(p["stat"]) and all(uid == 0 and (mode & 0o022) == 0 for uid, mode in p["stat"].values()),
          "(uid, mode) per path: " + str({k: (u, oct(m)) for k, (u, m) in p["stat"].items()}))
    check("gene sets: the manifest cannot be written at runtime",
          p["write_manifest"] in (13, 30), f"errno {p['write_manifest']}")
    check("gene sets: nothing can be created in the bundle directory at runtime",
          p["create_in_dir"] in (13, 30), f"errno {p['create_in_dir']}")
    check("gene sets: access(W_OK) is false on the directory and every file at runtime",
          not p["access_w"], f"writable: {p['access_w']}")

    check("gene sets: POLARS_MAX_THREADS=2 and polars honours it",
          p["polars_max_threads"] == "2" and p["polars_threads"] == 2,
          f"env {p['polars_max_threads']!r}, thread_pool_size {p['polars_threads']}")

    names = sorted(c["collection"] for c in p["collections"])
    check("gene sets: collections() lists the fixture's collections (daly profile)",
          names == ["coll.a", "coll.b"],
          f"got {p['collections']}; expects the daly profile, GENESETS_PROFILE=finngen drops coll.b")
    top = p["ora_results"][0]["set_id"] if p["ora_results"] else None
    check("gene sets: ora() over coll.a ranks the set the list was drawn from first",
          top == "A_EARLY" and p["ora_results"][0]["overlap"] == 8, f"got {p['ora_results']}")
    check("gene sets: the child ran under the supervisor's RLIMIT_AS",
          p["rlimit_as"] == sup.CHILD_RLIMIT_AS_BYTES,
          f"got {p['rlimit_as']}, supervisor sets {sup.CHILD_RLIMIT_AS_BYTES}")
    check("gene sets: peak address space stayed under RLIMIT_AS",
          p["vmpeak_kb"] * 1024 < p["rlimit_as"],
          f"VmPeak {p['vmpeak_kb']} kB, RLIMIT_AS {p['rlimit_as']}")
