#!/usr/bin/env python3
"""Stage the pinned gene-set bundle for the sandbox image, in two steps.

`fetch` runs on the build host, which has PyYAML but not polars, so it never imports polars:

  - refuses `gene_sets.release: null` in configs/datasets.yaml — there is no "latest";
  - refuses a source with no manifest.json. munge uploads it last, so its absence means a
    partial upload, not an empty bundle;
  - refuses a bundle whose manifest names another release than the pinned one;
  - verifies every file the manifest lists, bytes and sha256, on the copy it publishes;
  - writes the verified, UNFILTERED bundle to --out-dir together with policy.json: the
    release, the profile and its profiles.<p>.gene_set_licences.

`filter` runs where polars is (the sandbox image's builder stage). It re-verifies the bundle
against the policy's release before reading any of it, then DROPS each collection whose SPDX
licence is not in the allowlist: sets.parquet keeps only rows of allowed collections, and
members.parquet only rows whose set_id survived. What was dropped, and why, is recorded in
the rewritten manifest. Dropping rather than failing is deliberate: every bundle carries
c5.hpo, which some deployments may not, so failing would refuse every bundle there. The image
builder's own assertion is what fails if a disallowed licence survives this step.

genes.parquet is kept whole. It is the gene universe and coordinates (HGNC/GENCODE), not
licensed set content, and enrichment needs the same universe whatever collections remain.
A file no row was dropped from is copied byte-for-byte, so its sha256 still matches munge's;
a rewritten file gets a new sha256/bytes/rows in the output manifest.

Both steps build their output beside the target and rename it into place, so the target is
either the previous complete output, absent for the instant between two renames, or the new
complete output — never half-written.

WHERE THE BUNDLE COMES FROM. gs://<bucket>/gene_sets/<release>/. --bucket (or
GENE_SETS_BUCKET) names the bucket; only daly has a default, because munge stages there. No
other profile's bucket is derivable on this host, so it is required rather than guessed.
--source-dir reads a local copy of that prefix instead and needs no GCS at all. The profile
is --profile, else config_profile from terraform/terraform.tfvars.<DEPLOY_ENV>, the same file
scripts/lib/env.sh resolves for the build scripts.

Usage:
    scripts/stage-gene-sets.py fetch [--profile P] [--bucket B | --source-dir DIR] [--out-dir DIR]
    scripts/stage-gene-sets.py filter [--bundle-dir DIR] [--out-dir DIR]
                                      [--policy FILE | --profile P --release R --allowed L1,L2]

Exit 0 = staged, 1 = the bundle or the pin was refused, 2 = staging could not run.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from paths import DATASETS_YAML, ROOT, SANDBOX_DIR  # noqa: E402

DEFAULT_FETCH_DIR = os.path.join(SANDBOX_DIR, ".genesets-src")
DEFAULT_OUT_DIR = os.path.join(SANDBOX_DIR, ".genesets")
DEFAULT_BUCKETS = {"daly": "daly-genetics-results"}
BUNDLE_FILES = ("sets.parquet", "members.parquet", "genes.parquet")
MANIFEST = "manifest.json"
POLICY = "policy.json"
# a manifest file name is joined onto local paths, so anything but a plain name is refused
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class Refused(Exception):
    """The bundle or the pin is wrong: exit 1."""


class CannotRun(Exception):
    """A prerequisite is missing: exit 2."""


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_json(path, what):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise Refused(f"{os.path.dirname(path) or '.'} has no {what}")
    except ValueError as exc:
        raise Refused(f"{path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        raise Refused(f"{path} is not a JSON object")
    return data


def read_manifest(src):
    if not os.path.isfile(os.path.join(src, MANIFEST)):
        raise Refused(f"{src} has no {MANIFEST}: a partial upload, not a bundle")
    return read_json(os.path.join(src, MANIFEST), MANIFEST)


def manifest_files(manifest):
    bundle = manifest.get("bundle")
    files = bundle.get("files") if isinstance(bundle, dict) else None
    if not isinstance(files, dict):
        raise Refused("manifest has no bundle.files object")
    for name, meta in files.items():
        if not SAFE_NAME.fullmatch(name) or name in (MANIFEST, POLICY):
            raise Refused(f"manifest lists a file named {name!r}")
        if not isinstance(meta, dict):
            raise Refused(f"manifest entry for {name} is not an object")
    missing = [f for f in BUNDLE_FILES if f not in files]
    if missing:
        raise Refused(f"manifest lists no {', '.join(missing)}")
    return files


def verify(src, release):
    """Every check here is hashlib and json only, and runs before any parquet is read."""
    manifest = read_manifest(src)
    files = manifest_files(manifest)
    if manifest["bundle"].get("release_id") != release:
        raise Refused(f"bundle is release {manifest['bundle'].get('release_id')!r}, "
                      f"pinned is {release!r}")
    for name, meta in files.items():
        path = os.path.join(src, name)
        if not os.path.isfile(path):
            raise Refused(f"{name} is in the manifest but not in the bundle")
        if os.path.getsize(path) != meta.get("bytes"):
            raise Refused(f"{name}: {os.path.getsize(path)} bytes, manifest says {meta.get('bytes')}")
        if sha256_of(path) != meta.get("sha256"):
            raise Refused(f"{name}: sha256 does not match the manifest")
    return manifest


def manifest_licences(manifest):
    collections = manifest.get("collections")
    if not isinstance(collections, list):
        raise Refused("manifest has no collections list")
    licences = {}
    for c in collections:
        name = c.get("collection") if isinstance(c, dict) else None
        if not isinstance(name, str):
            raise Refused(f"manifest collection entry without a collection name: {c!r}")
        if name in licences:
            # two entries for one collection: whichever wins, the other licence is ignored
            raise Refused(f"manifest lists collection {name!r} more than once")
        lic = c.get("spdx_license")
        licences[name] = lic if isinstance(lic, str) else None
    return licences


def publish(out_dir, populate):
    parent = os.path.dirname(os.path.abspath(out_dir))
    os.makedirs(parent, exist_ok=True)
    base = os.path.basename(os.path.abspath(out_dir))
    tmp = tempfile.mkdtemp(prefix=f"{base}.tmp-", dir=parent)
    # mkdtemp's 0700 would survive a Docker COPY and hide the bundle from the sandbox's uid
    os.chmod(tmp, 0o755)
    try:
        result = populate(tmp)
        old = None
        if os.path.exists(out_dir):
            old = tempfile.mkdtemp(prefix=f"{base}.old-", dir=parent)
            os.rename(out_dir, os.path.join(old, base))
        try:
            os.rename(tmp, out_dir)
        except OSError:
            if old:
                os.rename(os.path.join(old, base), out_dir)
                shutil.rmtree(old)
            raise
        tmp = None
        if old:
            shutil.rmtree(old)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return result


def resolve_profile(explicit):
    if explicit:
        return explicit
    env = os.environ.get("DEPLOY_ENV")
    if not env:
        raise CannotRun("no --profile and DEPLOY_ENV is unset")
    tfvars = os.path.join(ROOT, "terraform", f"terraform.tfvars.{env}")
    try:
        with open(tfvars) as fh:
            for line in fh:
                m = re.match(r'^config_profile\s*=\s*"([^"]*)"', line)
                if m:
                    return m.group(1)
    except OSError as exc:
        raise CannotRun(f"DEPLOY_ENV={env}: cannot read {tfvars}: {exc}")
    raise CannotRun(f"{tfvars} has no config_profile")


def read_pin(datasets_yaml, profile):
    try:
        import yaml
    except ImportError:
        raise CannotRun("PyYAML is missing (pip install pyyaml)")
    try:
        with open(datasets_yaml) as fh:
            config = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise CannotRun(f"{datasets_yaml}: {exc}")
    release = (config.get("gene_sets") or {}).get("release")
    if not release:
        raise Refused(f"gene_sets.release is null in {datasets_yaml}: pin a release first")
    prof = (config.get("profiles") or {}).get(profile)
    if prof is None:
        raise CannotRun(f"profile {profile!r} is not in {datasets_yaml}")
    allowed = prof.get("gene_set_licences")
    if not allowed:
        raise Refused(f"profiles.{profile}.gene_set_licences is empty or absent")
    return str(release), [str(a) for a in allowed]


def gcloud(*args):
    try:
        return subprocess.run(["gcloud", "storage", *args], check=True,
                              capture_output=True, text=True).stdout
    except FileNotFoundError:
        raise CannotRun("gcloud is not on PATH")
    except subprocess.CalledProcessError as exc:
        raise CannotRun(f"gcloud storage {' '.join(args)}: {exc.stderr.strip()}")


def fetch_into(dest, release, bucket=None, source_dir=None):
    """Copy the bundle into dest, then verify the copy, so what is published is what was checked."""
    if source_dir:
        if not os.path.isfile(os.path.join(source_dir, MANIFEST)):
            raise Refused(f"{source_dir} has no {MANIFEST}: a partial upload, not a bundle")
        shutil.copyfile(os.path.join(source_dir, MANIFEST), os.path.join(dest, MANIFEST))
        for name in manifest_files(read_manifest(dest)):
            if not os.path.isfile(os.path.join(source_dir, name)):
                raise Refused(f"{name} is in the manifest but not in the bundle")
            shutil.copyfile(os.path.join(source_dir, name), os.path.join(dest, name))
        label = os.path.abspath(source_dir)
    else:
        label = f"gs://{bucket}/gene_sets/{release}/"
        listed = {line.rsplit("/", 1)[-1] for line in gcloud("ls", label).split()}
        if MANIFEST not in listed:
            raise Refused(f"{label} has no {MANIFEST}: a partial upload, not a bundle")
        gcloud("cp", label + MANIFEST, dest)
        gcloud("cp", *[label + n for n in manifest_files(read_manifest(dest))], dest)
    return verify(dest, release), label


def cmd_fetch(args):
    profile = resolve_profile(args.profile)
    release, allowed = read_pin(args.datasets_yaml, profile)
    bucket = None
    if not args.source_dir:
        bucket = args.bucket or DEFAULT_BUCKETS.get(profile)
        if not bucket:
            raise CannotRun(f"no gene-set bucket known for profile {profile!r}: "
                            "pass --bucket or set GENE_SETS_BUCKET")

    def populate(tmp):
        _, label = fetch_into(tmp, release, bucket, args.source_dir)
        with open(os.path.join(tmp, POLICY), "w") as fh:
            json.dump({"release": release, "profile": profile, "allowed_licences": allowed,
                       "source": label}, fh, indent=2, sort_keys=True)
            fh.write("\n")
        return label

    label = publish(args.out_dir, populate)
    print(f"gene sets {release} fetched and verified for {profile} into {args.out_dir} "
          f"from {label}; allowed licences: {', '.join(allowed)}")


def load_policy(args):
    explicit = (args.profile, args.release, args.allowed)
    if any(v is not None for v in explicit):
        if any(v is None for v in explicit):
            raise CannotRun("--profile, --release and --allowed go together")
        policy = {"profile": args.profile, "release": args.release,
                  "allowed_licences": [a for a in args.allowed.split(",") if a],
                  "source": os.path.abspath(args.bundle_dir)}
    else:
        path = args.policy or os.path.join(args.bundle_dir, POLICY)
        if not os.path.isfile(path):
            raise CannotRun(f"no policy: {path} does not exist (run fetch, or pass --policy)")
        policy = read_json(path, POLICY)
    allowed = policy.get("allowed_licences")
    if not isinstance(policy.get("release"), str) or not policy["release"]:
        raise Refused("policy names no release")
    if not isinstance(policy.get("profile"), str):
        raise Refused("policy names no profile")
    if not isinstance(allowed, list) or not all(isinstance(a, str) for a in allowed):
        raise Refused("policy allowed_licences is not a list of strings")
    if not allowed:
        raise Refused(f"profile {policy['profile']!r} allows no licence: nothing could be staged")
    return policy


def import_polars():
    try:
        import polars
    except ImportError:
        raise CannotRun("polars is missing (pip install polars; pyarrow is not needed)")
    return polars


def select(pl, src, manifest, profile, allowed):
    licences = manifest_licences(manifest)
    sets = pl.read_parquet(os.path.join(src, "sets.parquet"))
    members = pl.read_parquet(os.path.join(src, "members.parquet"))
    for frame, name, cols in ((sets, "sets.parquet", ("set_id", "collection")),
                              (members, "members.parquet", ("set_id",))):
        for col in cols:
            if frame.schema.get(col) != pl.String:
                raise Refused(f"{name}.{col} is {frame.schema.get(col)}, not a string column")
            if frame[col].null_count():
                raise Refused(f"{name}.{col} has {frame[col].null_count()} null value(s)")
    if sets["set_id"].n_unique() != sets.height:
        # members.parquet carries no collection, only set_id; a set_id shared across
        # collections would make dropping one collection's members ambiguous
        raise Refused("sets.parquet set_id is not unique, so members cannot be filtered by collection")

    dropped, keep = [], []
    for coll in sorted(set(sets["collection"].unique().to_list()) | set(licences)):
        lic = licences.get(coll)
        if lic is None:
            dropped.append({"collection": coll, "spdx_license": None,
                            "reason": "no licence recorded in the manifest"})
        elif lic not in allowed:
            dropped.append({"collection": coll, "spdx_license": lic,
                            "reason": f"licence not in profiles.{profile}.gene_set_licences"})
        else:
            keep.append(coll)
    kept_sets = sets.filter(pl.col("collection").is_in(pl.Series(keep, dtype=pl.String).implode()))
    kept_members = members.filter(pl.col("set_id").is_in(kept_sets["set_id"].implode()))
    return sets, kept_sets, members, kept_members, keep, dropped


def cmd_filter(args):
    policy = load_policy(args)
    profile, allowed = policy["profile"], policy["allowed_licences"]
    manifest = verify(args.bundle_dir, policy["release"])
    pl = import_polars()
    src = args.bundle_dir

    def populate(tmp):
        try:
            sets, kept_sets, members, kept_members, keep, dropped = select(
                pl, src, manifest, profile, allowed)
        except pl.exceptions.PolarsError as exc:
            raise Refused(f"cannot read the bundle's parquet: {type(exc).__name__}: {exc}")
        files = {}
        for name, before, after in (("sets.parquet", sets, kept_sets),
                                    ("members.parquet", members, kept_members),
                                    ("genes.parquet", None, None)):
            dest = os.path.join(tmp, name)
            if before is None or after.height == before.height:
                shutil.copyfile(os.path.join(src, name), dest)
                files[name] = dict(manifest["bundle"]["files"][name])
            else:
                after.write_parquet(dest)
                files[name] = {"bytes": os.path.getsize(dest), "rows": after.height,
                               "sha256": sha256_of(dest)}
        out = dict(manifest)
        out["bundle"] = dict(manifest["bundle"], files=files)
        if isinstance(out["bundle"].get("mapping"), dict):
            out["bundle"]["mapping"] = {k: v for k, v in out["bundle"]["mapping"].items()
                                        if k in keep}
        out["collections"] = [c for c in manifest["collections"] if c["collection"] in keep]
        out["staging"] = {
            "profile": profile,
            "allowed_licences": allowed,
            "source": policy.get("source"),
            "dropped": dropped,
            "source_files": manifest["bundle"]["files"],
        }
        with open(os.path.join(tmp, MANIFEST), "w") as fh:
            json.dump(out, fh, indent=2, sort_keys=True)
            fh.write("\n")
        return out

    out = publish(args.out_dir, populate)
    kept = [c["collection"] for c in out["collections"]]
    dropped = out["staging"]["dropped"]
    print(f"gene sets {policy['release']} filtered for {profile} into {args.out_dir}: "
          f"kept {len(kept)} ({', '.join(kept)}), dropped {len(dropped)}"
          + "".join(f"\n  dropped {d['collection']} ({d['spdx_license']}): {d['reason']}"
                    for d in dropped))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="host: fetch and verify the unfiltered bundle (no polars)")
    f.add_argument("--profile")
    f.add_argument("--bucket", default=os.environ.get("GENE_SETS_BUCKET"))
    f.add_argument("--source-dir", help="a local copy of gs://<bucket>/gene_sets/<release>/")
    f.add_argument("--out-dir", default=DEFAULT_FETCH_DIR)
    f.add_argument("--datasets-yaml", default=DATASETS_YAML)
    f.set_defaults(run=cmd_fetch)
    g = sub.add_parser("filter", help="builder: drop collections the licence allowlist excludes")
    g.add_argument("--bundle-dir", default=DEFAULT_FETCH_DIR)
    g.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    g.add_argument("--policy", help=f"default: <bundle-dir>/{POLICY}")
    g.add_argument("--profile")
    g.add_argument("--release")
    g.add_argument("--allowed", help="comma-separated SPDX ids")
    g.set_defaults(run=cmd_filter)
    args = parser.parse_args(argv)

    try:
        args.run(args)
    except Refused as exc:
        print(f"gene sets refused: {exc}", file=sys.stderr)
        return 1
    except (CannotRun, OSError) as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
