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

INTRA VERSUS CROSS. A group whose files all live in one repo is intra-repo; a group
spanning repos is cross-repo. The two are reported separately because the choice of what to
consolidate turns on which of them the work actually lands in, and they are weighted by
COMMIT EVIDENCE rather than by lines: an intra-repo group scores the number of commits that
touched two or more of its files at once, and a cross-repo group scores the number of
calendar days on which two or more repos committed to it. Both are the lockstep edit the
consolidation is supposed to remove; neither is impressed by a long file.

Usage:
    scripts/check-duplication.py                    report
    scripts/check-duplication.py --json
    scripts/check-duplication.py --write-baseline   record docs/duplication-baseline.json
    scripts/check-duplication.py --check            fail if the counts grew past it

Exit 0 = counted (and within the baseline under --check), 1 = a count grew, 2 = could not
run: a repo of the suite is not checked out, or the baseline was measured over a different
set of repos and so cannot be compared. A missing checkout lowers every count, so treating
it as a pass is the one outcome that would make this useless.
"""

import argparse
import ast
import difflib
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


def walk(repo_dir):
    for base, dirs, files in os.walk(repo_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".sdk"))
        for f in sorted(files):
            yield os.path.join(base, f)


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


def collect(repos):
    """units[(kind, key)] -> [(repo, relpath, name, src)]; enums[set] -> [(repo, rel, name)]"""
    units = defaultdict(list)
    enums = defaultdict(list)
    unreadable = 0
    for repo, path in sorted(repos.items()):
        for f in walk(path):
            ext = os.path.splitext(f)[1]
            if ext not in (".py", ".sh", ".bash", ".yaml", ".yml"):
                continue
            text = read(f)
            if text is None:
                unreadable += 1
                continue
            rel = os.path.relpath(f, path)
            if ext == ".py":
                for name, key, src, size in py_units(text):
                    units[key].append((repo, rel, name, src, size))
                for name, s in py_enums(text):
                    enums[s].append((repo, rel, name))
            elif ext in (".sh", ".bash"):
                for name, key, src, size in sh_units(text):
                    units[key].append((repo, rel, name, src, size))
            else:
                for name, s in yaml_enums(text):
                    enums[s].append((repo, rel, name))
    return units, enums, unreadable


def _group(members, sized=False):
    """members: [(repo, rel, name, ...)] -> a group dict, or None if it does not qualify."""
    files = sorted({(m[0], m[1]) for m in members})
    if len(files) < 2:
        return None
    if sized and len(files) < WIDE_FILES and max(m[4] for m in members) < FLOOR_NODES:
        return None
    return {
        "files": [f"{r}:{p}" for r, p in files],
        "repos": sorted({r for r, _ in files}),
        "names": sorted({m[2] for m in members if m[2]}),
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


def measure(repos, window):
    units, enums, unreadable = collect(repos)
    exact = exact_groups(units)
    groups = exact + near_groups(units, exact) + enum_groups(enums)
    history = {r: commit_index(p, window) for r, p in repos.items()}
    weigh(groups, history)
    groups.sort(key=lambda g: (-g["lockstep"], -len(g["files"]), g["kind"], g["files"]))

    def tally(scope):
        sel = [g for g in groups if (len(g["repos"]) == 1) == (scope == "intra")]
        return {
            "groups": len(sel),
            "files": len({f for g in sel for f in g["files"]}),
            "lockstep": sum(g["lockstep"] for g in sel),
            "by_kind": {k: sum(1 for g in sel if g["kind"] == k)
                        for k in ("bodies-exact", "bodies-near", "enumerations")},
        }

    return {
        "repos_present": sorted(repos),
        "window_days": window,
        "unreadable_files": unreadable,
        "intra": tally("intra"),
        "cross": tally("cross"),
        "groups": groups,
    }


def render(m, top=12):
    lines = [
        f"repos scanned: {len(m['repos_present'])} ({', '.join(m['repos_present'])})",
        f"commit window: {m['window_days']} days",
        "",
        f"{'':10} {'groups':>7} {'files':>7} {'lockstep':>9}   by kind",
    ]
    for scope in ("intra", "cross"):
        t = m[scope]
        kinds = " ".join(f"{k.split('-')[-1]}={v}" for k, v in t["by_kind"].items())
        lines.append(f"{scope + '-repo':10} {t['groups']:>7} {t['files']:>7} "
                     f"{t['lockstep']:>9}   {kinds}")
    lines += ["",
              "lockstep = commits touching >=2 files of one intra-repo group; for cross-repo",
              "groups, days on which >=2 repos committed to the group.", "",
              f"top {top} groups by lockstep evidence:"]
    for g in m["groups"][:top]:
        scope = "intra" if len(g["repos"]) == 1 else "CROSS"
        names = ", ".join(g["names"][:3]) or "-"
        lines.append(f"  [{scope}] {g['kind']:16} lockstep={g['lockstep']:<3} "
                     f"{len(g['files'])} files  {names}")
        for f in g["files"][:6]:
            lines.append(f"        {f}")
        if len(g["files"]) > 6:
            lines.append(f"        ... {len(g['files']) - 6} more")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--window", type=int, default=WINDOW_DAYS)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()

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

    m = measure({r: p for r, p in found.items() if p}, args.window)

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
            "repos_present": m["repos_present"],
            "window_days": m["window_days"],
            "intra": m["intra"],
            "cross": m["cross"],
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
        if base["repos_present"] != m["repos_present"]:
            print("cannot run: the baseline was measured over a different set of repos "
                  f"({', '.join(base['repos_present'])}); the counts are not comparable.",
                  file=sys.stderr)
            return 2
        grew = [
            f"{scope}-repo {field}: {m[scope][field]} > {base[scope][field]}"
            for scope in ("intra", "cross") for field in ("groups", "files")
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
              f"{m['cross']['groups']} cross-repo groups)")
        return 0

    print(json.dumps(m, indent=2) if args.json else render(m, args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
