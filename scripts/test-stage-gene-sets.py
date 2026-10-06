#!/usr/bin/env python3
"""Offline assertions about scripts/stage-gene-sets.py, against a tiny fake bundle.

Run: python3 scripts/test-stage-gene-sets.py   (needs PyYAML and polars; no network)
Exit 0 = pass, 1 = a property is broken, 2 = the harness could not run.

What is pinned here is the refusal set and the licence filter, because none of it has a
runtime symptom: a staging step that stopped refusing a partial upload, or stopped dropping
a disallowed collection, still produces a perfectly healthy image. The byte count and the
sha256 are each driven alone — a same-size byte flip, and a manifest whose sha256 is right
but whose byte count is not — so dropping either check fails a case of its own.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import checks  # noqa: E402
from checks import check  # noqa: E402
from paths import SCRIPTS_DIR  # noqa: E402

try:
    import polars as pl
    import yaml
except ImportError as exc:
    checks.die(f"{exc.name} is missing")

RELEASE = "20990101.hgnc20990101.gencode99"
SETS = pl.DataFrame({
    "set_id": ["S1", "S2", "H1"],
    "collection": ["go", "go", "hpo"],
    "name": ["a", "b", "c"],
    "url_or_stable_id": ["u1", "u2", "u3"],
    "n_given": [2, 1, 2], "n_mapped": [2, 1, 2], "n_unmapped": [0, 0, 0],
})
MEMBERS = pl.DataFrame({
    "set_id": ["S1", "S1", "S2", "H1", "H1"],
    "ensembl_gene_id": ["E1", "E2", "E1", "E2", "E3"],
    "symbol": ["G1", "G2", "G1", "G2", "G3"],
})
GENES = pl.DataFrame({"ensembl_gene_id": ["E1", "E2", "E3"], "symbol": ["G1", "G2", "G3"]})
COLLECTIONS = [{"collection": "go", "spdx_license": "CC-BY-4.0"},
               {"collection": "hpo", "spdx_license": "LicenseRef-HPO"}]
STRICT = ["CC0-1.0", "CC-BY-4.0"]


def load_module():
    spec = importlib.util.spec_from_file_location(
        "stage_gene_sets", os.path.join(SCRIPTS_DIR, "stage-gene-sets.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def make_bundle(src, sets=SETS, members=MEMBERS, collections=COLLECTIONS, manifest=True,
                raw=None):
    """raw maps a file name to bytes written instead of the frame; the manifest still matches."""
    os.makedirs(src)
    files = {}
    for name, df in (("sets.parquet", sets), ("members.parquet", members),
                     ("genes.parquet", GENES)):
        path = os.path.join(src, name)
        if raw and name in raw:
            with open(path, "wb") as fh:
                fh.write(raw[name])
        else:
            df.write_parquet(path)
        files[name] = {"bytes": os.path.getsize(path), "rows": df.height, "sha256": sha(path)}
    if manifest:
        with open(os.path.join(src, "manifest.json"), "w") as fh:
            json.dump({
                "release_id": "20990101",
                "bundle": {"release_id": RELEASE, "files": files,
                           "mapping": {"go": {"n_sets": 2}, "hpo": {"n_sets": 1}}},
                "collections": collections,
            }, fh)
    return src


def edit_manifest(src, fn):
    path = os.path.join(src, "manifest.json")
    with open(path) as fh:
        m = json.load(fh)
    fn(m)
    with open(path, "w") as fh:
        json.dump(m, fh)


def make_yaml(root, release=RELEASE, strict=STRICT):
    path = os.path.join(root, "datasets.yaml")
    with open(path, "w") as fh:
        yaml.safe_dump({"gene_sets": {"release": release}, "profiles": {
            "strict": {"gene_set_licences": strict},
            "lax": {"gene_set_licences": ["CC0-1.0", "CC-BY-4.0", "LicenseRef-HPO"]},
        }}, fh)
    return path


def run(mod, *argv):
    """A traceback is a failure of its own: every bad input must end in a message and 1 or 2."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            rc = mod.main(list(argv))
        except Exception:
            return "traceback", traceback.format_exc()
    return rc, err.getvalue()


@contextlib.contextmanager
def no_polars():
    saved = {k: v for k, v in sys.modules.items() if k == "polars" or k.startswith("polars.")}
    for k in saved:
        del sys.modules[k]
    sys.modules["polars"] = None  # makes `import polars` raise ImportError
    try:
        yield
    finally:
        del sys.modules["polars"]
        sys.modules.update(saved)


def siblings(out):
    parent = os.path.dirname(out)
    return sorted(n for n in os.listdir(parent) if n.startswith(os.path.basename(out) + "."))


def main():
    mod = load_module()
    with tempfile.TemporaryDirectory() as root:
        src = make_bundle(os.path.join(root, "bundle"))
        ds = make_yaml(root)
        n = iter(range(1000))

        def fetch(source, profile="strict", yaml_path=None):
            dest = os.path.join(root, f"fetched{next(n)}", ".genesets-src")
            rc, err = run(mod, "fetch", "--profile", profile, "--source-dir", source,
                          "--out-dir", dest, "--datasets-yaml", yaml_path or ds)
            return rc, err, dest

        def filt(bundle, out, *policy):
            return run(mod, "filter", "--bundle-dir", bundle, "--out-dir", out, *policy)

        def explicit(allowed=STRICT):
            return ("--profile", "strict", "--release", RELEASE, "--allowed", ",".join(allowed))

        def bundle(name, **kw):
            return make_bundle(os.path.join(root, name), **kw)

        # ---- fetch: host side, no polars
        rc, err, _ = fetch(src, yaml_path=make_yaml(os.path.join(root), release=None))
        check("fetch: null release refused with 1", rc == 1 and "null" in err, f"rc={rc} {err}")
        rc, err, _ = fetch(src, yaml_path=make_yaml(root, release="other"))
        check("fetch: release other than the pin refused with 1", rc == 1 and "pinned" in err, err)
        rc, err, _ = fetch(src, yaml_path=make_yaml(root, strict=[]))
        check("fetch: empty allowlist refused with 1", rc == 1 and "empty" in err, err)
        ds = make_yaml(root)
        rc, err, _ = fetch(bundle("partial", manifest=False))
        check("fetch: missing manifest refused with 1", rc == 1 and "manifest.json" in err, err)

        flipped = bundle("flipped")
        path = os.path.join(flipped, "members.parquet")
        with open(path, "r+b") as fh:
            fh.seek(os.path.getsize(path) // 2)
            byte = fh.read(1)
            fh.seek(-1, 1)
            fh.write(bytes([byte[0] ^ 0xFF]))
        rc, err, dest = fetch(flipped)
        check("fetch: same-size byte flip refused on sha256", rc == 1 and "sha256" in err
              and not os.path.exists(dest), err)
        wrong_size = bundle("wrongsize")
        edit_manifest(wrong_size, lambda m: m["bundle"]["files"]["genes.parquet"].update(
            bytes=m["bundle"]["files"]["genes.parquet"]["bytes"] + 1))
        rc, err, _ = fetch(wrong_size)
        check("fetch: right sha256 with wrong byte count refused on bytes",
              rc == 1 and "bytes" in err, err)

        bad_json = bundle("badjson")
        with open(os.path.join(bad_json, "manifest.json"), "w") as fh:
            fh.write("{")
        rc, err, _ = fetch(bad_json)
        check("fetch: unparseable manifest refused with 1", rc == 1 and "JSON" in err, f"{rc} {err}")

        with no_polars():
            rc, err, fetched = fetch(src)
        check("fetch runs with polars unimportable", rc == 0, f"{rc} {err}")
        check("fetch output is the source bundle byte for byte, plus policy.json",
              sorted(os.listdir(fetched)) == sorted([*mod.BUNDLE_FILES, "manifest.json",
                                                     "policy.json"])
              and all(sha(os.path.join(fetched, f)) == sha(os.path.join(src, f))
                      for f in (*mod.BUNDLE_FILES, "manifest.json")))
        with open(os.path.join(fetched, "policy.json")) as fh:
            policy = json.load(fh)
        check("policy.json carries the release, profile and allowlist",
              (policy["release"], policy["profile"], policy["allowed_licences"])
              == (RELEASE, "strict", STRICT), str(policy))
        rc, err, _ = fetch(src, profile="nosuch")
        check("fetch: unknown profile is 2, not 1", rc == 2, err)
        rc, err = run(mod, "fetch", "--profile", "strict", "--out-dir",
                      os.path.join(root, "nobucket"), "--datasets-yaml", ds)
        check("fetch: no bucket for a profile without a default is 2",
              rc == 2 and "--bucket" in err, err)

        # ---- filter: builder side
        out = os.path.join(root, "sandbox", ".genesets")
        with no_polars():
            rc, err = filt(fetched, out)
        check("filter without polars is 2", rc == 2 and "polars" in err, f"{rc} {err}")

        rc, err, fetched_lax = fetch(src, profile="lax")
        rc, err = filt(fetched_lax, out)
        with open(os.path.join(out, "manifest.json")) as fh:
            lax = json.load(fh)
        check("lax profile stages everything", rc == 0 and lax["staging"]["dropped"] == []
              and [c["collection"] for c in lax["collections"]] == ["go", "hpo"], err)
        check("nothing dropped: every file byte-identical to the source",
              all(sha(os.path.join(out, f)) == sha(os.path.join(src, f))
                  for f in mod.BUNDLE_FILES))

        rc, err = filt(fetched, out)
        with open(os.path.join(out, "manifest.json")) as fh:
            m = json.load(fh)
        sets = pl.read_parquet(os.path.join(out, "sets.parquet"))
        members = pl.read_parquet(os.path.join(out, "members.parquet"))
        check("strict policy from fetch's policy.json exits 0", rc == 0, err)
        check("disallowed collection dropped from sets and members",
              sets["collection"].unique().to_list() == ["go"]
              and "H1" not in members["set_id"].to_list())
        check("drop recorded with its licence",
              [(d["collection"], d["spdx_license"]) for d in m["staging"]["dropped"]]
              == [("hpo", "LicenseRef-HPO")]
              and [c["collection"] for c in m["collections"]] == ["go"]
              and list(m["bundle"]["mapping"]) == ["go"])
        check("kept collection's rows untouched",
              sets.equals(SETS.filter(pl.col("collection") == "go"))
              and members.equals(MEMBERS.filter(pl.col("set_id") != "H1")))
        check("genes.parquet kept whole",
              sha(os.path.join(out, "genes.parquet")) == sha(os.path.join(src, "genes.parquet")))
        check("rewritten manifest checksums match the staged files",
              all(m["bundle"]["files"][f]["sha256"] == sha(os.path.join(out, f))
                  and m["bundle"]["files"][f]["bytes"] == os.path.getsize(os.path.join(out, f))
                  for f in mod.BUNDLE_FILES)
              and m["bundle"]["files"]["members.parquet"]["rows"] == members.height)
        check("no temp or old directories left behind", siblings(out) == [], str(siblings(out)))
        before = sha(os.path.join(out, "manifest.json"))

        def unchanged():
            manifest = os.path.join(out, "manifest.json")
            return os.path.isfile(manifest) and sha(manifest) == before and siblings(out) == []

        tampered = os.path.join(root, "tampered")
        shutil.copytree(fetched, tampered)
        with open(os.path.join(tampered, "members.parquet"), "ab") as fh:
            fh.write(b"x")
        rc, err = filt(tampered, out)
        check("filter re-verifies: a bundle changed after fetch is refused with 1",
              rc == 1 and "members.parquet" in err, err)
        check("refusal leaves the previous staging intact", unchanged())

        rc, err = filt(src, out, *explicit([]))
        check("filter: empty allowlist refused with 1", rc == 1 and "allows no licence" in err, err)
        rc, err = filt(src, out, "--profile", "strict", "--allowed", "CC0-1.0")
        check("filter: partial explicit policy is 2", rc == 2 and "together" in err, err)
        rc, err = filt(src, out, "--datasets-yaml", ds, "--profile", "strict",
                       "--allowed", "CC0-1.0")
        check("filter: --datasets-yaml with an explicit allowlist is 2",
              rc == 2 and "--profile only" in err, err)
        rc, err = filt(src, out, "--datasets-yaml", make_yaml(root, release="other"),
                       "--profile", "strict")
        check("filter: --datasets-yaml pin other than the bundle's refused with 1",
              rc == 1 and "pinned" in err, err)
        ds = make_yaml(root)
        yaml_out = os.path.join(root, "o-yaml")
        rc, err = filt(src, yaml_out, "--datasets-yaml", ds, "--profile", "strict")
        with open(os.path.join(yaml_out, "manifest.json")) as fh:
            staged = json.load(fh)
        check("filter: --datasets-yaml takes the profile's allowlist from the file",
              rc == 0 and [c["collection"] for c in staged["collections"]] == ["go"]
              and staged["staging"]["profile"] == "strict", f"{rc} {err}")
        rc, err = filt(src, out, "--profile", "strict", "--release", "other",
                       "--allowed", "CC0-1.0")
        check("filter: release other than the policy's refused with 1",
              rc == 1 and "pinned" in err, err)

        dup = bundle("dupcoll", collections=[
            *COLLECTIONS, {"collection": "hpo", "spdx_license": "CC0-1.0"}])
        rc, err = filt(dup, os.path.join(root, "o-dup"), *explicit())
        check("duplicate manifest collection refused with 1",
              rc == 1 and "more than once" in err, f"{rc} {err}")

        dup_ids = bundle("dupid", sets=SETS.with_columns(pl.Series("set_id", ["S1", "S2", "S1"])))
        rc, err = filt(dup_ids, os.path.join(root, "o-dupid"), *explicit())
        check("duplicate set_id refused with 1", rc == 1 and "not unique" in err, f"{rc} {err}")

        extra = os.path.join(root, "o-extra")
        rc, err = filt(bundle("extra", sets=pl.concat([SETS, pl.DataFrame({
            "set_id": ["X1"], "collection": ["unlisted"], "name": ["x"],
            "url_or_stable_id": ["ux"], "n_given": [1], "n_mapped": [1], "n_unmapped": [0]})]),
            members=pl.concat([MEMBERS, pl.DataFrame({
                "set_id": ["X1", "ORPHAN"], "ensembl_gene_id": ["E1", "E3"],
                "symbol": ["G1", "G3"]})])), extra, *explicit())
        with open(os.path.join(extra, "manifest.json")) as fh:
            m = json.load(fh)
        ex_sets = pl.read_parquet(os.path.join(extra, "sets.parquet"))
        ex_members = pl.read_parquet(os.path.join(extra, "members.parquet"))
        check("collection absent from the manifest dropped and recorded",
              rc == 0 and "unlisted" not in ex_sets["collection"].to_list()
              and {"collection": "unlisted", "spdx_license": None,
                   "reason": "no licence recorded in the manifest"} in m["staging"]["dropped"],
              err)
        check("members of dropped and of unknown set_ids are dropped",
              sorted(set(ex_members["set_id"].to_list())) == ["S1", "S2"])

        for name, kw in (
                ("null collection in sets.parquet", {"sets": SETS.with_columns(
                    pl.Series("collection", ["go", None, "hpo"]))}),
                ("integer set_id in sets.parquet", {"sets": SETS.with_columns(
                    pl.Series("set_id", [1, 2, 3]))}),
                ("sets.parquet that is not parquet", {"raw": {"sets.parquet": b"not parquet"}}),
                ("manifest collection without a name", {"collections": [
                    {"name": "go", "spdx_license": "CC-BY-4.0"}]}),
                ("manifest collections not a list", {"collections": {"go": "CC-BY-4.0"}})):
            rc, err = filt(bundle(name.replace(" ", "_"), **kw),
                           os.path.join(root, "o-" + name.replace(" ", "_")), *explicit())
            check(f"malformed bundle refused with 1, not a traceback: {name}", rc == 1,
                  f"{rc} {err}")

        src2 = bundle("q")
        real_write = pl.DataFrame.write_parquet

        def failing_write(self, path, *a, **kw):
            real_write(self, path, *a, **kw)
            raise OSError("disk full")

        pl.DataFrame.write_parquet = failing_write
        try:
            rc, _ = filt(src2, out, *explicit())
        finally:
            pl.DataFrame.write_parquet = real_write
        check("a failure mid-write leaves the previous staging intact", rc == 2 and unchanged(),
              str(siblings(out)))

        real_rename = os.rename

        def failing_rename(a, b):
            if os.path.basename(a).startswith(".genesets.tmp-"):
                raise OSError("rename refused")
            return real_rename(a, b)

        os.rename = failing_rename
        try:
            rc, _ = filt(src2, out, *explicit())
        finally:
            os.rename = real_rename
        check("a failed final rename restores the previous staging", rc == 2 and unchanged(),
              str(siblings(out)))

        fresh = os.path.join(root, "fresh", ".genesets")
        rc, _ = filt(src2, fresh, *explicit())
        check("stages into a directory that did not exist", rc == 0
              and sorted(os.listdir(fresh)) == ["genes.parquet", "manifest.json",
                                                "members.parquet", "sets.parquet"])

    print(f"stage-gene-sets checks: {checks.CHECKS} run, {len(checks.FAILURES)} failure(s)")
    return 1 if checks.FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
