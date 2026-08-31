#!/usr/bin/env python3
"""Count, from the trees themselves, the one-fact-in-N-copies shape across the suite.

WHY IT MEASURES INSTEAD OF LISTING. The thing being counted is hand-maintained copies of
one fact. A detector holding its own hand-maintained list of known copies would be another
instance of it, and would go quietly wrong the moment a copy moved. So nothing here names a
file, a function or a constant: every group below is discovered by structure, and the
instances anybody can name are the test of whether the discovery works, not its input.

WHAT IT SEES, and therefore what it can be trusted to notice regrowing:

  bodies-exact      byte-equal bodies (Python function bodies and module-level constant
                    expressions, compared as ASTs so formatting and comments do not hide a
                    copy; shell function bodies, compared with comments stripped) appearing
                    in two or more files.
  bodies-near       same-named bodies in two or more files that are at least NEAR_RATIO
                    similar. This is where a copy that was edited on one side shows up —
                    the shape that produces the same fix twice.
  enumerations      the same set of four or more strings written out as a literal in two or
                    more files (Python list/tuple/set/dict keys, YAML sequences and mapping
                    keys). One set of names, N places that have to agree.

WHAT IT DOES NOT SEE. It is a unit-level detector: a duplicated block that is not a
function body and not a literal set is invisible to it, as is anything in TypeScript. A
count it cannot produce is a target this epic descopes rather than a target it forgets.

WHAT IT MUST NEVER DO IS STOP SEEING SOMETHING QUIETLY. Everything above is found by
parsing, and a parser that covers PART of a tree is worse than one that covers none of it:
the sites it dropped are reported by nobody, the totals still print, and the summary reads
like a clean bill of health. Finding those same sites a second way would need a second
parser, so this counts COVERAGE rather than duplicates: per repo and extension, how many
files were read, how many the owning pass parsed, and how many yielded a unit or an
enumeration. A file that stopped parsing, or an extractor that stopped matching a
construct, moves one of those numbers without moving the one above it, and --check refuses
rather than counting fewer sites. Nothing in the census depends on a duplicate existing, so
a suite with every copy consolidated away still passes.

DISCOVERY IS THE LINK THE CENSUS CANNOT CHECK FROM WITHIN. read, parsed and units all count
what walk() handed over, so SKIP_DIRS growing lowers all three together and reads as
deletion. The fourth number is therefore taken from `git ls-files`, whose reach depends on
neither SKIP_DIRS nor on walk() being able to descend to a file: files read dropping further
than the tracked count is exit 2. Untracked files sit outside it by construction; they can
only make files read the larger number, and growth is not a fail condition.

EXTS is the one input both sides do share, so dropping an extension from it takes the cell
out of walk() and out of the tracked count at once, and no pairing of one number against
another can see that. A baseline cell with no counterpart in the current run at all is
therefore drift in its own right. An extension that keeps its place in EXTS but loses its
dispatch branch in collect() is the other case, and the units rule already catches it.

INTRA VERSUS CROSS. A group whose files all live in one repo is intra-repo; a group
spanning repos is cross-repo. The two are reported separately because the choice of what to
consolidate turns on which of them the work actually lands in, and they are weighted by
COMMIT EVIDENCE rather than by lines: an intra-repo group scores the number of commits that
touched two or more of its files at once, and a cross-repo group scores the number of
calendar days on which two or more repos committed to it. Both are the lockstep edit the
consolidation is supposed to remove; neither is impressed by a long file.

WHAT THE RATCHET COUNTS IS UNDECLARED DUPLICATION. Two kinds of copy are netted out first,
and the report always shows the split rather than one smaller number:

  generated   a member ignored by a TRACKED .gitignore of its own repo and byte-identical
              to a tracked file in another. No list of these is maintained anywhere — git
              is asked. The ignore has to come from a file every clone has: .git/info/exclude
              and a global excludesFile are not tracked, so a rule in either would net a copy
              out here and leave it counted in the next clone. The direction it fails in is
              the one that matters: a consumer that commits its copy stops being ignored, and
              the copy comes back as undeclared.
  declared    a group whose remaining files are covered by an entry in configs/twins.yaml,
              which names the sites, the property that must hold between them and WHY they
              are two things. A registry is itself a hand-maintained list — the shape this
              script exists to measure — so an entry without a reason is a hard error, and
              the declared count is ratcheted too: declaring a twin takes a --write-baseline
              --reason naming it.

Netting happens per MEMBER: a group can lose its generated edges and stay in the count for
the hand-maintained pair underneath, which is what the sync-datasets.sh copies of
configs/datasets.yaml do to the intra-suite copy in configs/datasets-schema-example.yaml.
A member struck this way leaves the declared row as well as the undeclared ones, so the four
rows do not count one file twice.

Usage:
    scripts/check-duplication.py                    report
    scripts/check-duplication.py --json
    scripts/check-duplication.py --write-baseline --reason "..."
                                                      record docs/duplication-baseline.json
    scripts/check-duplication.py --check            fail if the counts grew past it

Exit 0 = counted (and within the baseline under --check), 1 = a count grew, 2 = could not
run: a repo of the suite is not checked out, configs/twins.yaml does not say what it must,
the baseline is missing, unreadable or structurally malformed, it was measured over a
different set of repos and so cannot be compared, or the coverage census fell below it.
A missing checkout and a stale parser both lower every count, so treating either as a pass
is the one outcome that would make this useless. Note which one 2 is NOT: a detector that
has stopped seeing a site has not measured growth, it has stopped being able to measure.
"""

import argparse
import ast
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import siblings  # noqa: E402

ROOT = siblings.ROOT
BASELINE = os.path.join(ROOT, "docs", "duplication-baseline.json")
TWINS = os.path.join(ROOT, "configs", "twins.yaml")

try:
    import yaml
except ImportError:
    print("cannot run: PyYAML is missing (pip install pyyaml)", file=sys.stderr)
    raise SystemExit(2)

# The size floor is what keeps idioms out. It is waived at WIDE_FILES copies, because a
# body repeated that widely is a fact somebody is maintaining by hand however short it is —
# which is how a fifteen-copy one-liner stays visible without lowering the floor for
# everything. NEAR_RATIO runs on unparsed bodies, so it is stable across formatting.
MIN_STMTS = 1
MIN_NODES = 8
MIN_SH_LINES = 1
FLOOR_NODES = 25
FLOOR_SH_LINES = 4
WIDE_FILES = 5
MIN_ENUM = 4
NEAR_RATIO = 0.80
WINDOW_DAYS = 30

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".claude", ".beads", ".sdk-src",
    "dist", "build", "htmlcov", "site-packages", ".next", "coverage",
}

SH_FUNC = re.compile(r"^(?:function\s+)?([A-Za-z_][A-Za-z0-9_-]*)\s*\(\)\s*\{\s*$")

EXTS = (".py", ".sh", ".bash", ".yaml", ".yml")
# on_disk is deliberately not among the fields a cell starts with: absent means "git could
# not say", which a 0 would spend the rest of the baseline's life claiming was a real count
WALK_FIELDS = ("read", "parsed", "units")
CENSUS_FIELDS = WALK_FIELDS + ("on_disk",)
# each number paired with the one above it in tracked -> read -> parsed -> units
CHAIN = (("read", "on_disk"), ("parsed", "read"), ("units", "parsed"))
DRIFT_MESSAGE = {
    "read": ("{repo} {ext}: read {low} < {base_low} while {high} such files are still "
             "tracked on disk — discovery stopped reaching files that are still there"),
    "parsed": ("{repo} {ext}: parsed {low} < {base_low} with {high} still read — a file "
               "stopped parsing, and every unit in it was dropped without a word"),
    "units": ("{repo} {ext}: yielding a unit {low} < {base_low} with {high} still parsed — "
              "an extractor stopped matching a construct it used to see"),
}


def walk(repo_dir):
    for base, dirs, files in os.walk(repo_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".sdk"))
        for f in sorted(files):
            yield os.path.join(base, f)


def parses(text, ext):
    """Did the pass that OWNS this extension get through the file at all?

    Mirrors each extractor's own exception handling exactly, because a file this calls
    parsed while py_units or yaml_enums silently drops it would make the census agree with
    nothing. The shell scanner is a line scanner with no failure mode, so for .sh/.bash
    parsed necessarily equals read and only the units count carries drift there.
    """
    if ext == ".py":
        try:
            ast.parse(text)
        except SyntaxError:
            return False
    elif ext in (".yaml", ".yml"):
        try:
            list(yaml.safe_load_all(text))
        except Exception:
            return False
    return True


def read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def py_units(text):
    """(name, exact key, comparable source, size) for every non-trivial body."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name, body = node.name, node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
        else:
            continue
        if len(body) < MIN_STMTS:
            continue
        size = sum(1 for b in body for _ in ast.walk(b))
        if size < MIN_NODES:
            continue
        try:
            src = "\n".join(ast.unparse(b) for b in body)
        except Exception:
            continue
        yield name, "py:" + "\n".join(ast.dump(b) for b in body), src, size
    # a module-level constant built by an expression rather than written out as a literal
    # is invisible to the enumeration detector below, but it is the same N-copy fact
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        if not isinstance(node.targets[0], ast.Name) or isinstance(node.value, ast.Constant):
            continue
        size = sum(1 for _ in ast.walk(node.value))
        if size < MIN_NODES:
            continue
        try:
            src = ast.unparse(node.value)
        except Exception:
            continue
        yield node.targets[0].id, "pyconst:" + ast.dump(node.value), src, size


def sh_units(text):
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = SH_FUNC.match(lines[i].strip())
        if not m:
            i += 1
            continue
        body = []
        j = i + 1
        while j < len(lines) and lines[j].rstrip() != "}":
            body.append(lines[j])
            j += 1
        i = j + 1
        kept = [" ".join(l.split()) for l in body]
        kept = [l for l in kept if l and not l.startswith("#")]
        if len(kept) < MIN_SH_LINES:
            continue
        src = "\n".join(kept)
        yield m.group(1), "sh:" + src, src, len(kept) * (FLOOR_NODES // FLOOR_SH_LINES)


def _strset(values):
    if all(isinstance(v, str) for v in values) and len(set(values)) >= MIN_ENUM:
        return frozenset(values)
    return None


def py_enums(text):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        v = node.value
        got = None
        if isinstance(v, (ast.List, ast.Tuple, ast.Set)):
            items = [e.value for e in v.elts if isinstance(e, ast.Constant)]
            if len(items) == len(v.elts):
                got = _strset(items)
        elif isinstance(v, ast.Dict):
            keys = [k.value for k in v.keys if isinstance(k, ast.Constant)]
            if len(keys) == len(v.keys):
                got = _strset(keys)
        if got:
            name = ""
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if targets and isinstance(targets[0], ast.Name):
                name = targets[0].id
            yield name, got


def yaml_enums(text):
    try:
        docs = list(yaml.safe_load_all(text))
    except Exception:
        return
    seen = []

    def visit(obj, key=""):
        if isinstance(obj, dict):
            got = _strset(list(obj.keys()))
            if got:
                seen.append((key, got))
            for k, v in obj.items():
                visit(v, str(k))
        elif isinstance(obj, list):
            got = _strset(obj)
            if got:
                seen.append((key, got))
            for v in obj:
                visit(v, key)

    for d in docs:
        visit(d)
    yield from seen


def _slot(census, repo, ext):
    return census.setdefault(repo, {}).setdefault(ext, dict.fromkeys(WALK_FIELDS, 0))


def collect(repos):
    """units[(kind, key)] -> [(repo, relpath, name, src)]; enums[set] -> [(repo, rel, name)];
    census[repo][ext] -> how far each file got through the pass that owns it."""
    units = defaultdict(list)
    enums = defaultdict(list)
    census = {}
    unreadable = 0
    for repo, path in sorted(repos.items()):
        for f in walk(path):
            ext = os.path.splitext(f)[1]
            if ext not in EXTS:
                continue
            text = read(f)
            if text is None:
                unreadable += 1
                continue
            cell = _slot(census, repo, ext)
            cell["read"] += 1
            if parses(text, ext):
                cell["parsed"] += 1
            rel = os.path.relpath(f, path)
            got = 0
            if ext == ".py":
                for name, key, src, size in py_units(text):
                    units[key].append((repo, rel, name, src, size))
                    got += 1
                for name, st in py_enums(text):
                    enums[st].append((repo, rel, name))
                    got += 1
            elif ext in (".sh", ".bash"):
                for name, key, src, size in sh_units(text):
                    units[key].append((repo, rel, name, src, size))
                    got += 1
            else:
                for name, st in yaml_enums(text):
                    enums[st].append((repo, rel, name))
                    got += 1
            if got:
                cell["units"] += 1
    return units, enums, unreadable, census


def tracked_paths(repo_path):
    """Everything `git ls-files` reports — the census's second opinion, and the generated
    rule's notion of "committed somewhere".

    walk() is the census's own eyes, so a directory it stops entering lowers read, parsed
    and units together and the drop reads as an ordinary deletion. This shares no code and
    no configuration with it. None means git could not answer, which is a could-not-count
    rather than an empty set: an empty set would silently disarm the discovery check.
    """
    try:
        out = subprocess.run(["git", "-C", repo_path, "ls-files", "-z"],
                             capture_output=True, text=True, check=False)
    except OSError:
        return None
    if out.returncode != 0:
        return None
    return {f for f in out.stdout.split("\0") if f}


def tracked_counts(paths):
    counts = defaultdict(int)
    for f in paths:
        ext = os.path.splitext(f)[1]
        if ext in EXTS:
            counts[ext] += 1
    return dict(counts)


def ignored_paths(repo_path, rels, tracked):
    """The subset of `rels` a TRACKED .gitignore of this repo covers.

    `git check-ignore` consults the index, so a path that is TRACKED comes back as not
    ignored however well it matches a pattern. That is the whole failure direction of the
    generated rule: the day a consumer commits its generated copy, this stops covering it
    and the copy is counted again. An unanswerable git is an empty set, which can only
    leave copies in the count.

    The ignore also has to come from a file every clone has. check-ignore honours
    .git/info/exclude and the user's core.excludesFile, and neither is tracked, so a rule
    written in one of them would net a copy out of the ratchet on one machine and leave it
    counted on another with nothing in review able to see the difference. -v names the
    source; only a source `git ls-files` reports for this repo is accepted.
    """
    if not rels or not tracked:
        return set()
    try:
        out = subprocess.run(["git", "-C", repo_path, "check-ignore", "-v", "-z", "--stdin"],
                             input="\0".join(rels), capture_output=True, text=True,
                             check=False)
    except OSError:
        return set()
    if out.returncode not in (0, 1):
        return set()
    # -v -z emits <source>\0<line>\0<pattern>\0<pathname>\0 per match
    fields = out.stdout.split("\0")
    return {fields[i + 3] for i in range(0, len(fields) - 3, 4) if fields[i] in tracked}


def digest(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError:
        return None


def generated_members(groups, repos, tracked):
    """Members that are gitignored where they live and byte-identical to a file committed
    in another repo — the generated copies, recognised without anyone listing them.

    Only files that are already in a group are hashed, and that is not a shortcut: a
    byte-identical copy yields byte-identical units and enumerations, so it shares every
    group with its source and the source is always there to compare against.
    """
    members = defaultdict(set)
    for g in groups:
        for f in g["files"]:
            repo, _, rel = f.partition(":")
            members[repo].add(rel)
    ignored = {r: ignored_paths(repos[r], sorted(rels), tracked.get(r))
               for r, rels in members.items()}
    digests = {}
    for repo, rels in members.items():
        for rel in rels:
            d = digest(os.path.join(repos[repo], rel))
            if d:
                digests[(repo, rel)] = d
    committed = defaultdict(set)
    for (repo, rel), d in digests.items():
        if tracked.get(repo) and rel in tracked[repo]:
            committed[d].add(repo)
    return {f"{repo}:{rel}" for (repo, rel), d in digests.items()
            if rel in ignored[repo] and committed[d] - {repo}}


class TwinError(Exception):
    """configs/twins.yaml does not say what an entry has to say."""


TWIN_REQUIRED = ("id", "property", "reason", "merge")
# derived rather than written out again: this script's own detector flags the schema
# restated as a literal beside the entries in configs/twins.yaml that carry every field
TWIN_FIELDS = set(TWIN_REQUIRED) | {"sites", "symbols"}
TWIN_MERGE = ("never", "open")


def load_twins(repos):
    """The declared twins, validated hard.

    Nothing here defaults. Netting a group out of the ratchet is how a real finding gets
    silenced, so an entry missing its reason, naming a site that no longer exists, or
    carrying a field nobody reads is a refusal to run rather than a quieter count. An
    ABSENT registry is not an error — it nets nothing out, which can only make the counts
    larger — but it is said out loud in the report.
    """
    path = TWINS
    if not os.path.exists(path):
        return []
    try:
        with open(path) as fh:
            doc = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise TwinError(f"{os.path.relpath(path, ROOT)}: {exc}")
    if not isinstance(doc, dict) or not isinstance(doc.get("twins"), list):
        raise TwinError(f"{os.path.relpath(path, ROOT)}: expected a `twins:` list")
    out, seen = [], set()
    for i, entry in enumerate(doc["twins"]):
        where = f"{os.path.relpath(path, ROOT)} entry {i + 1}"
        if not isinstance(entry, dict):
            raise TwinError(f"{where}: not a mapping")
        unknown = sorted(set(entry) - TWIN_FIELDS)
        if unknown:
            raise TwinError(f"{where}: unknown field(s) {unknown} — a misspelt `reason` is "
                            "an entry with no reason")
        for field in TWIN_REQUIRED:
            value = entry.get(field)
            # a string, not something that stringifies: `reason: 1` and `reason: {a: b}`
            # are both an entry with no reason, and both would pass a str() coercion
            if not isinstance(value, str) or not value.strip():
                raise TwinError(f"{where}: `{field}` is required and must be a non-empty "
                                "string")
        if entry["merge"] not in TWIN_MERGE:
            raise TwinError(f"{where}: merge must be one of {list(TWIN_MERGE)}")
        if entry["id"] in seen:
            raise TwinError(f"{where}: duplicate id {entry['id']!r}")
        seen.add(entry["id"])
        sites = entry.get("sites")
        if not isinstance(sites, list) or len(sites) < 2:
            raise TwinError(f"{where}: `sites` must list two or more repo:path sites")
        for site in sites:
            repo, _, rel = str(site).partition(":")
            if repo not in repos:
                raise TwinError(f"{where}: site {site!r} names no repo of the suite")
            if not rel or not os.path.exists(os.path.join(repos[repo], rel)):
                raise TwinError(f"{where}: site {site!r} does not exist — a stale entry "
                                "declares nothing and hides that it declares nothing")
        symbols = entry.get("symbols") or {}
        if not isinstance(symbols, dict) or any(
                s not in sites or not isinstance(n, list) or not n
                for s, n in symbols.items()):
            raise TwinError(f"{where}: `symbols` maps a declared site to a non-empty list "
                            "of names")
        out.append({**entry, "sites": sites, "symbols": symbols})
    return out


def explaining_twin(twins, files, names_by_file):
    """The entry that covers this group, or None.

    The whole group must fall inside one entry's sites: a group with a foot outside stays
    counted, so declaring a file cannot quietly absorb whatever else it duplicates. Where
    an entry carries `symbols`, every file's names must fall inside the list for THAT site
    — the mechanism that lets a parity declaration stop at one function.
    """
    for t in twins:
        if not set(files) <= set(t["sites"]):
            continue
        if t["symbols"] and not all(
                names_by_file.get(f) and set(names_by_file[f]) <= set(t["symbols"].get(f, ()))
                for f in files):
            continue
        return t
    return None


def net_out(groups, generated, twins):
    """Label every group generated, declared or undeclared, stripping generated members.

    The strip runs on every group that survives, declared ones included: a generated copy
    left inside a declared group is counted in two of the four rows at once, and the
    lockstep days of the repo it sits in are charged to work nobody has to do.
    """
    for g in groups:
        gen = [f for f in g["files"] if f in generated]
        left = [f for f in g["files"] if f not in generated]
        if gen:
            g["generated_files"] = gen
        if len(left) < 2:
            g["status"] = "generated"
            continue
        if gen:
            g["files"] = left
            g["repos"] = sorted({f.partition(":")[0] for f in left})
            g["names_by_file"] = {f: n for f, n in g["names_by_file"].items() if f in left}
            g["names"] = sorted({n for names in g["names_by_file"].values() for n in names})
        twin = explaining_twin(twins, left, g["names_by_file"])
        if twin:
            g["status"] = "declared"
            g["twin"] = twin["id"]
            continue
        g["status"] = "undeclared"


def baseline_shape(base):
    """What is wrong with the baseline's own structure, or None.

    Presence is not enough. A block that is there but malformed — `"declared": {}`, a
    repos_present that is not a list — reaches the comparisons below as a KeyError or a
    TypeError, and the process exits 1 saying a count grew when the truth is that nothing
    could be compared. That is the one answer this must never give: 1 is a finding about
    the trees, 2 is "could not run".
    """
    if not isinstance(base.get("repos_present"), list):
        return "`repos_present` is missing or is not a list"
    if not isinstance(base.get("census"), dict):
        return "`census` is not an object"
    for scope in ("intra", "cross", "declared"):
        block = base.get(scope)
        if not isinstance(block, dict):
            return f"`{scope}` is not an object"
        for field in ("groups", "files"):
            if not isinstance(block.get(field), int):
                return f"`{scope}.{field}` is missing or is not a number"
    return None


def census_drift(base, now):
    """The drops that mean the detector stopped seeing, not that the tree shrank.

    Each is paired with the number above it in the chain tracked -> read -> parsed -> units,
    and it is the SIZE of the two drops that decides: a real deletion moves both by the same
    amount, drift moves the lower one further. Asking only whether the number above also fell
    is what a single unrelated deletion needs to disarm the rule for the whole run. max(0)
    keeps growth above from excusing a drop below. That comparison is what makes exact counts
    workable and a tolerance unwanted — a tuned threshold would be one more arbitrary
    constant with nothing to check it against.
    """
    out = []
    for repo in sorted(base):
        for ext in sorted(base[repo]):
            b = base[repo][ext]
            n = now.get(repo, {}).get(ext)
            if n is None:
                out.append(f"{repo} {ext}: counted at the baseline ({b['read']} files read), "
                           "counted nowhere now — the extension left EXTS, or the repo left "
                           "the scan, and no pairing between numbers can see that")
                continue
            for low, high in CHAIN:
                if high not in b or high not in n:
                    continue
                if max(0, b[low] - n[low]) > max(0, b[high] - n[high]):
                    out.append(DRIFT_MESSAGE[low].format(
                        repo=repo, ext=ext, low=n[low], base_low=b[low], high=n[high]))
    return out


def _group(members, sized=False):
    """members: [(repo, rel, name, ...)] -> a group dict, or None if it does not qualify."""
    files = sorted({(m[0], m[1]) for m in members})
    if len(files) < 2:
        return None
    if sized and len(files) < WIDE_FILES and max(m[4] for m in members) < FLOOR_NODES:
        return None
    # per file, not only the union: a twin entry that declares parity on one named function
    # has to be able to say which name belongs to which side. The union cannot — two repos
    # each holding a function of the same name would look like the declared one.
    by_file = defaultdict(set)
    for m in members:
        if m[2]:
            by_file[f"{m[0]}:{m[1]}"].add(m[2])
    return {
        "files": [f"{r}:{p}" for r, p in files],
        "repos": sorted({r for r, _ in files}),
        "names": sorted({m[2] for m in members if m[2]}),
        "names_by_file": {f: sorted(n) for f, n in sorted(by_file.items())},
    }


def exact_groups(units):
    out = []
    for key in sorted(units):
        g = _group(units[key], sized=True)
        if g:
            g["kind"] = "bodies-exact"
            out.append(g)
    return out


def near_groups(units, exact):
    """Same-named function bodies that are similar but not equal, clustered per name."""
    exact_sets = {tuple(g["files"]) for g in exact}
    by_name = defaultdict(list)
    for key, members in units.items():
        for repo, rel, name, src, size in members:
            by_name[name].append((repo, rel, key, src, size))
    out = []
    for name in sorted(by_name):
        # one representative per file: a copy is about files, not about call sites
        per_file = {}
        for repo, rel, key, src, size in by_name[name]:
            per_file.setdefault((repo, rel), (key, src, size))
        if len(per_file) < 2:
            continue
        items = sorted(per_file.items())
        parent = list(range(len(items)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for a in range(len(items)):
            for b in range(a + 1, len(items)):
                ka, sa, _ = items[a][1]
                kb, sb, _ = items[b][1]
                if ka == kb:
                    continue
                if difflib.SequenceMatcher(None, sa, sb).ratio() >= NEAR_RATIO:
                    parent[find(a)] = find(b)
        clusters = defaultdict(list)
        for i, ((repo, rel), (_, _, size)) in enumerate(items):
            clusters[find(i)].append((repo, rel, name, None, size))
        for members in clusters.values():
            g = _group(members, sized=True)
            if g and tuple(g["files"]) not in exact_sets:
                g["kind"] = "bodies-near"
                out.append(g)
    return out


def enum_groups(enums):
    out = []
    for s in sorted(enums, key=lambda x: sorted(x)):
        g = _group(enums[s])
        if g:
            g["kind"] = "enumerations"
            g["size"] = len(s)
            out.append(g)
    return out


def commit_index(path, since):
    """[(sha, date, {files})] for the window, or [] if git cannot answer."""
    try:
        out = subprocess.run(
            ["git", "-C", path, "log", f"--since={since} days ago",
             "--pretty=format:\x01%H %ad", "--date=short", "--name-only"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return []
    if out.returncode != 0:
        return []
    commits = []
    for chunk in out.stdout.split("\x01"):
        lines = [l for l in chunk.splitlines() if l.strip()]
        if not lines:
            continue
        sha, _, date = lines[0].partition(" ")
        commits.append((sha, date.strip(), set(lines[1:])))
    return commits


def weigh(groups, history):
    """Lockstep evidence per group; see the module docstring for why not lines."""
    for g in groups:
        by_repo = defaultdict(set)
        for f in g["files"]:
            repo, _, rel = f.partition(":")
            by_repo[repo].add(rel)
        if len(g["repos"]) == 1:
            repo = g["repos"][0]
            g["lockstep"] = sum(
                1 for _, _, files in history.get(repo, [])
                if len(files & by_repo[repo]) >= 2
            )
        else:
            days = defaultdict(set)
            for repo, rels in by_repo.items():
                for _, date, files in history.get(repo, []):
                    if files & rels:
                        days[date].add(repo)
            g["lockstep"] = sum(1 for r in days.values() if len(r) >= 2)


def measure(repos, window, twins):
    units, enums, unreadable, census = collect(repos)
    blind = []
    tracked = {}
    for repo, path in sorted(repos.items()):
        tracked[repo] = tracked_paths(path)
        if tracked[repo] is None:
            # leave on_disk out rather than writing 0: a baseline recorded here would
            # otherwise turn every later committed deletion into a permanent false red
            blind.append(repo)
            continue
        counts = tracked_counts(tracked[repo])
        for ext in set(census.get(repo, {})) | set(counts):
            _slot(census, repo, ext)["on_disk"] = counts.get(ext, 0)
    exact = exact_groups(units)
    groups = exact + near_groups(units, exact) + enum_groups(enums)
    net_out(groups, generated_members(groups, repos, tracked), twins)
    history = {r: commit_index(p, window) for r, p in repos.items()}
    # after net_out, so an undeclared group is weighed on the files still in it and the
    # generated copies' lockstep days stop counting towards work nobody has to do
    weigh(groups, history)
    groups.sort(key=lambda g: (-g["lockstep"], -len(g["files"]), g["kind"], g["files"]))

    def tally(pick):
        sel = [g for g in groups if pick(g)]
        return {
            "groups": len(sel),
            "files": len({f for g in sel for f in g["files"]}),
            "lockstep": sum(g["lockstep"] for g in sel),
            "by_kind": {k: sum(1 for g in sel if g["kind"] == k)
                        for k in ("bodies-exact", "bodies-near", "enumerations")},
        }

    def undeclared(scope):
        return lambda g: (g["status"] == "undeclared"
                          and (len(g["repos"]) == 1) == (scope == "intra"))

    declared = tally(lambda g: g["status"] == "declared")
    declared["by_twin"] = {t["id"]: sum(1 for g in groups if g.get("twin") == t["id"])
                           for t in twins}
    return {
        "repos_present": sorted(repos),
        "window_days": window,
        "unreadable_files": unreadable,
        "census": census,
        "census_blind": blind,
        "twins_registry": os.path.exists(TWINS),
        "intra": tally(undeclared("intra")),
        "cross": tally(undeclared("cross")),
        "declared": declared,
        "generated": tally(lambda g: g["status"] == "generated"),
        "groups": groups,
    }


def render(m, top=12):
    lines = [
        f"repos scanned: {len(m['repos_present'])} ({', '.join(m['repos_present'])})",
        f"commit window: {m['window_days']} days",
        "",
        f"{'':10} {'groups':>7} {'files':>7} {'lockstep':>9}   by kind",
    ]
    for scope in ("intra", "cross", "declared", "generated"):
        t = m[scope]
        kinds = " ".join(f"{k.split('-')[-1]}={v}" for k, v in t["by_kind"].items())
        label = scope + "-repo" if scope in ("intra", "cross") else scope
        lines.append(f"{label:10} {t['groups']:>7} {t['files']:>7} "
                     f"{t['lockstep']:>9}   {kinds}")
    lines += ["",
              "the two -repo rows are UNDECLARED duplication, and they are what --check "
              "ratchets on.",
              "declared = covered by an entry in configs/twins.yaml"
              + ("" if m["twins_registry"] else " (ABSENT — nothing is netted out)")
              + "; generated = gitignored where",
              "it lives and byte-identical to a file committed in another repo, so no entry "
              "is needed."]
    if m["declared"]["by_twin"]:
        for tid, n in sorted(m["declared"]["by_twin"].items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {tid:28} {n} group(s)"
                         + ("   — declares nothing this run" if not n else ""))
    lines += ["",
              "lockstep = commits touching >=2 files of one intra-repo group; for cross-repo",
              "groups, days on which >=2 repos committed to the group.", "",
              "coverage census — what the passes above actually got through, summed over "
              "repos:",
              f"{'':10} {'tracked':>8} {'read':>8} {'parsed':>8} {'w/ units':>9}"]
    per_ext = defaultdict(lambda: dict.fromkeys(CENSUS_FIELDS, 0))
    for exts in m["census"].values():
        for ext, cell in exts.items():
            for k, v in cell.items():
                per_ext[ext][k] += v
    for ext in sorted(per_ext):
        c = per_ext[ext]
        lines.append(f"{ext:10} {c['on_disk']:>8} {c['read']:>8} {c['parsed']:>8} "
                     f"{c['units']:>9}")
    if m["census_blind"]:
        lines.append("  tracked counts unavailable: " + ", ".join(m["census_blind"]))
    lines += ["", f"top {top} UNDECLARED groups by lockstep evidence:"]
    for g in [g for g in m["groups"] if g["status"] == "undeclared"][:top]:
        scope = "intra" if len(g["repos"]) == 1 else "CROSS"
        names = ", ".join(g["names"][:3]) or "-"
        stripped = len(g.get("generated_files", ()))
        lines.append(f"  [{scope}] {g['kind']:16} lockstep={g['lockstep']:<3} "
                     f"{len(g['files'])} files  {names}"
                     + (f"  (+{stripped} generated)" if stripped else ""))
        for f in g["files"][:6]:
            lines.append(f"        {f}")
        if len(g["files"]) > 6:
            lines.append(f"        ... {len(g['files']) - 6} more")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--reason", help="why the baseline is being raised or lowered; "
                                     "required with --write-baseline, recorded in the "
                                     "snapshot")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--window", type=int, default=WINDOW_DAYS)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

    if args.write_baseline and not args.reason:
        ap.error("--write-baseline requires --reason: a ratchet moved with no reason "
                  "recorded is a ratchet silenced")

    try:
        found = siblings.resolve_all()
    except siblings.SiblingError as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return 2
    missing = sorted(r for r, p in found.items() if not p)
    if missing:
        print("cannot run: not checked out on this machine: " + ", ".join(missing),
              file=sys.stderr)
        print("       every count below a full checkout is an undercount, so this is not "
              "a pass.", file=sys.stderr)
        return 2

    repos = {r: p for r, p in found.items() if p}
    try:
        twins = load_twins(repos)
    except TwinError as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        print("       an entry that does not say why is how a real finding gets netted "
              "out silently, so this refuses rather than counting.", file=sys.stderr)
        return 2

    m = measure(repos, args.window, twins)

    if args.write_baseline:
        head = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        date = subprocess.run(["git", "-C", ROOT, "log", "-1", "--format=%ad",
                               "--date=short"], capture_output=True, text=True).stdout.strip()
        # the measurement is of the WORKING TREE, which is usually not the commit: writing
        # the baseline is itself part of an uncommitted change, so HEAD alone names a
        # commit that does not reproduce these numbers
        dirty = bool(subprocess.run(["git", "-C", ROOT, "status", "--porcelain"],
                                    capture_output=True, text=True).stdout.strip())
        against = (f"commit {head[:12]} of genetics-results-suite plus uncommitted changes"
                   if dirty else f"commit {head[:12]} of genetics-results-suite")
        snap = {
            "SNAPSHOT": f"Measured on {date}, against {against} and whatever the sibling "
                        "checkouts were at that moment. This is a DATED MEASUREMENT, not a "
                        "claim about today: run scripts/check-duplication.py for current "
                        "numbers.",
            "measured_date": date,
            "measured_commit": head,
            "measured_tree_dirty": dirty,
            "reason": args.reason,
            "repos_present": m["repos_present"],
            "window_days": m["window_days"],
            "census": m["census"],
            "intra": m["intra"],
            "cross": m["cross"],
            "declared": m["declared"],
            "generated": m["generated"],
        }
        with open(BASELINE, "w") as fh:
            json.dump(snap, fh, indent=2)
            fh.write("\n")
        print(render(m, args.top))
        print(f"\nbaseline written: {os.path.relpath(BASELINE, ROOT)}")
        return 0

    if args.check:
        try:
            with open(BASELINE) as fh:
                base = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"cannot run: no readable baseline at {BASELINE}: {exc}", file=sys.stderr)
            return 2
        if "census" not in base:
            print("cannot run: the baseline predates the coverage census and cannot say "
                  "whether the passes still reach as much of the trees as they did. "
                  "Rerun --write-baseline --reason to record one.", file=sys.stderr)
            return 2
        if "declared" not in base:
            print("cannot run: the baseline predates the declared/generated split, so its "
                  "intra and cross counts include duplication these do not. Comparing them "
                  "would read as a large improvement nobody made. Rerun --write-baseline "
                  "--reason to record one.", file=sys.stderr)
            return 2
        broken = baseline_shape(base)
        if broken:
            print(f"cannot run: the baseline at {BASELINE} is malformed: {broken}. "
                  "Rerun --write-baseline --reason to record one.", file=sys.stderr)
            return 2
        if base["repos_present"] != m["repos_present"]:
            print("cannot run: the baseline was measured over a different set of repos "
                  f"({', '.join(base['repos_present'])}); the counts are not comparable.",
                  file=sys.stderr)
            return 2
        # coverage before counts: a pass that has gone blind on part of a tree still
        # produces counts, and they compare clean because the sites it dropped are in
        # neither number.
        if m["census_blind"]:
            print("cannot run: git could not list the tracked files of "
                  + ", ".join(m["census_blind"])
                  + ", so a drop in files read cannot be told from a deletion.",
                  file=sys.stderr)
            return 2
        drift = census_drift(base["census"], m["census"])
        if drift:
            print("the duplication passes cover less of the trees than the baseline "
                  "recorded:", file=sys.stderr)
            for d in drift:
                print(f"  {d}", file=sys.stderr)
            print("  a site nobody parses is in no group, so the counts below would read "
                  "as a clean bill of health. Refusing rather than counting fewer sites.",
                  file=sys.stderr)
            print("  if the drop is real — files deleted, a helper consolidated away — "
                  "rerun --write-baseline --reason to record it.", file=sys.stderr)
            return 2
        # declared is ratcheted alongside the undeclared counts: netting a group out is the
        # one move that makes this number fall for a reason nobody can see, so it has to be
        # spent through --write-baseline --reason like any other. generated is not — it
        # moves when a generator gains a consumer, which costs nobody anything.
        #
        # WHAT WOULD MAKE THAT FALSE, since a one-directional rationale is the kind that
        # rots: `git rm --cached` plus a .gitignore line takes a hand-maintained duplicate
        # out of the ratchet with no --write-baseline and no reason recorded anywhere — the
        # accountability an entry in configs/twins.yaml carries in its `reason` field has no
        # counterpart on this side. What bounds it is the precondition: WHOLE-FILE
        # byte-identity to a file committed in another repo, now also requiring the ignore
        # to come from a tracked .gitignore. Measured 2026-08-31 over all six repos with no
        # extension filter, exactly one cross-repo byte-identical pair had a gitignored
        # side, the sync-datasets.sh copies. It is not exact even so: a probe hand-typed a
        # 46-byte colors.yaml, ignored under a local/ dev-overrides rule that has nothing to
        # do with generation and byte-identical to a tracked file in another repo, and it is
        # netted out. That residue is honest and stays — a size threshold to close it would
        # be a tuned constant with nothing to check it against.
        grew = [
            f"{label} {field}: {m[scope][field]} > {base[scope][field]}"
            for scope, label in (("intra", "undeclared intra-repo"),
                                 ("cross", "undeclared cross-repo"),
                                 ("declared", "declared twins"))
            for field in ("groups", "files")
            if m[scope][field] > base[scope][field]
        ]
        if grew:
            print("duplication grew past the baseline snapshot:", file=sys.stderr)
            for g in grew:
                print(f"  {g}", file=sys.stderr)
            print(f"  baseline measured {base['measured_date']} at "
                  f"{base['measured_commit'][:12]}"
                  f"{' plus uncommitted changes' if base.get('measured_tree_dirty') else ''}",
                  file=sys.stderr)
            print("  run: scripts/check-duplication.py   to see which groups",
                  file=sys.stderr)
            return 1
        print(f"duplication within the baseline ({m['intra']['groups']} intra-repo, "
              f"{m['cross']['groups']} cross-repo undeclared groups; "
              f"{m['declared']['groups']} declared in configs/twins.yaml and "
              f"{m['generated']['groups']} generated, netted out)")
        return 0

    print(json.dumps(m, indent=2) if args.json else render(m, args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
