#!/usr/bin/env python3
"""Offline assertions about the manifests scripts/deploy.sh renders with envsubst.

Run: python3 scripts/test-manifest-render.py
Exit 0 = pass, 1 = a rendered manifest is broken, 2 = the harness could not run.

WHAT THIS DEFENDS. deploy.sh does not render a *field*,
it pipes the WHOLE manifest through `envsubst '<whitelist>'`. So a whitelisted name spelled
`${NAME}` anywhere in the file is substituted — including inside a `#` comment. Two of the
values are multi-line nginx fragments (LEGACY_REDIRECT, KEYCLOAK_SERVER), and a multi-line
expansion inside a comment breaks out of the `#` and the render stops being valid YAML;
`kubectl apply` then fails mid-deploy. That happened, in two comment regions of
k8s/deployments/auth-gateway.yaml, and went unnoticed because on the profiles anyone had
deployed BOTH fragments are empty — the file only misrenders once a redirect or the Keycloak
broker is configured. The fix was a rewording, held in place by nothing but an invariant
comment in the file. This is the mechanical half.

WHY IT IS NOT auth-gateway-ONLY. deploy.sh applies the same whole-document envsubst to every
file in k8s/configs/, k8s/deployments/ and k8s/cronjobs/. auth-gateway.yaml is where the
hazard was first hit, not where it can only occur, so the harness sweeps every manifest
deploy.sh renders and derives each file's whitelist from the loop that renders it.

NOTHING IS RE-TYPED FROM deploy.sh. The whitelists, the `:latest` rewrite, and the multi-line
values are all parsed out of scripts/deploy.sh (see read_render_plan / multiline_values). A
second hand-maintained copy of the whitelist would be exactly the "second list to rot" this
repo's CLAUDE.md warns about — and it would rot silently, because a name dropped from the
copy simply stops being checked.

DRIFT MUST NOT LOOK LIKE A PASS. Reading deploy.sh instead of copying it moves the rot from a
stale whitelist to a stale parser, and a parser that matches PART of the script is the worse
failure: the files it no longer accounts for get an empty whitelist, every check on them passes
vacuously, and the summary line reads like a clean bill of health. So everything parsed is
cross-checked against a deliberately loose survey of the same script (render_loop_survey,
multiline_values), and any disagreement is exit 2 — see main(). Wrapping the deployments
envsubst over two lines, or renaming the loop variable, warns instead of passing.

A WHITELIST IS ALSO CHECKED AGAINST THE FILES IT GOVERNS, in both directions
(check_whitelist_coverage). deploy.sh holds one hand-maintained whitelist per envsubst call and
nothing ever compared them to the manifests: ${DOMAIN} sat in the deployments whitelist while
appearing in zero files under k8s/, because an unused substitution renders nothing and so goes
stale in silence. The other direction is louder but just as unnoticed until a deploy — a
`${NAME}` a manifest spells that its own loop's whitelist omits reaches the cluster as the
literal text `${NAME}`. Which whitelist a file gets is decided by the directory it sits in, so
this is where a manifest copied between k8s/deployments/ and k8s/cronjobs/ is caught. Both
sides are derived: the whitelists are parsed out of deploy.sh, the placeholders are read out of
the files, and neither is re-typed here.

NOT EVERY ${...} IN A COMMENT IS A DEFECT, and the harness must not say it is. Names that are
deliberately absent from the whitelist survive verbatim by design — auth-gateway.yaml's
${INTERNAL_API_SECRET} is a Secret that a later initContainer substitutes, and baking it into
a ConfigMap is the thing its absence prevents. Whitelisted names whose values are single-line
(${KEYCLOAK_HOST} in k8s/deployments/keycloak.yaml:2) expand inside a comment harmlessly. So
the comment rule below fires only for names deploy.sh can give a MULTI-LINE value, and
everything else is judged by whether the render actually parses.
"""

import collections
import glob as globmod
import os
import re
import subprocess
import sys

try:
    import yaml
except ImportError:
    # exit 2, not 1: deploy.sh treats 1 as "a manifest is broken" and aborts the deploy.
    print("harness cannot run: PyYAML is missing (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY_SH = os.path.join(ROOT, "scripts", "deploy.sh")
K8S_DIR = os.path.join(ROOT, "k8s")

# deploy.sh renders inside `cd "${ROOT_DIR}/k8s"`, so its globs are relative to k8s/.
FOR_RE = re.compile(r'^(\s*)for\s+f\s+in\s+([^;]+);\s*do\s*$')
ENVSUBST_RE = re.compile(r"""envsubst\s+'([^']*)'\s*<\s*"\$f\"""")
BASE_GUARD_RE = re.compile(r'\[\s*"\$\{base\}"\s*=\s*"([^"]+)"\s*\]')
PRINTF_RE = re.compile(r"""printf\s+-v\s+([A-Za-z_][A-Za-z0-9_]*)\s+'((?:[^'])*)'""")
# the same three constructs matched with (almost) nothing pinned down, for the drift
# cross-check in render_loop_survey / multiline_values.
FOR_ANY_RE = re.compile(r'^(\s*)for\s+[A-Za-z_][A-Za-z0-9_]*\s+in\s+([^;]+);\s*do\s*$')
ENVSUBST_WORD_RE = re.compile(r'\benvsubst\b')
PRINTF_ANY_RE = re.compile(r'printf\s+-v\s+([A-Za-z_][A-Za-z0-9_]*)')
TAG_SED_RE = re.compile(r"""sed\s+"s/:latest/:\$\{TAG\}/g\"""")
NAME_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
# the keycloak realm/IdP renders read a literal path instead of "$f", and the coverage check
# below covers them too — a dead name rots the same in either shape.
LITERAL_ENVSUBST_RE = re.compile(r"""envsubst\s+'([^']*)'\s*<\s*"([^"]+)\"""")
ENVSUBST_CALL_RE = re.compile(r"envsubst[ \t]+'")
ROOT_DIR_PREFIX_RE = re.compile(r"^\$\{ROOT_DIR\}/")
# only braced spellings are build-time placeholders; nginx's runtime variables are bare ($host,
# $request_uri) and policing those here would flag every proxy directive in auth-gateway.yaml.
BRACED_NAME_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
ENV_ENTRY_RE = re.compile(r"^\s*-\s*name:\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", re.M)
SHELL_ASSIGN_RE = re.compile(r"^\s*(?:export\s+|local\s+)?([A-Za-z_][A-Za-z0-9_]*)=", re.M)
INNER_ENVSUBST_RE = re.compile(r"envsubst\s+'([^']*)'")

# stand-in values. Deliberately inert tokens: the property under test is what the DOCUMENT
# does with a substitution, not what any particular value is.
SINGLE_LINE_VALUE = "envsubst-render-check"
PRINTF_ARG = "render-check.example.org"
TAG = "render-check-tag"


class HarnessError(Exception):
    """The harness could not decide the question (exit 2), as distinct from a failure."""


Render = collections.namedtuple("Render", "glob guard names lineno")
# one envsubst call, resolved to the files it governs. `label` and `lineno` exist only so a
# coverage failure can point at the whitelist a reader has to edit.
Whitelist = collections.namedtuple("Whitelist", "label lineno names paths")


def logical_lines(lines):
    """[(lineno, text)] with backslash continuations joined and comment-only lines dropped.

    The keycloak renders wrap their whitelist onto a second line, and prose above the render
    loops uses the word envsubst; both would otherwise miscount in envsubst_call_count.
    """
    out, pending, start = [], None, None
    for lineno, raw in enumerate(lines, 1):
        text = raw.rstrip("\n")
        if pending is None and text.lstrip().startswith("#"):
            continue
        if pending is None:
            pending, start = text, lineno
        else:
            pending += " " + text.strip()
        if pending.endswith("\\"):
            pending = pending[:-1]
            continue
        out.append((start, pending))
        pending = None
    if pending is not None:
        out.append((start, pending))
    return out


def envsubst_call_count(lines):
    """How many `envsubst '<names>'` calls deploy.sh makes, counted as loosely as possible.

    The coverage check has to account for every one of them. It is the same anti-vacuity
    property render_loop_survey defends for the render loops: a parser that recovers only some
    of the calls checks fewer whitelists and still prints a pass. This epic's own evidence
    enumerated the whitelists by hand and came up one short, so the count is derived here
    rather than written down.
    """
    return sum(len(ENVSUBST_CALL_RE.findall(text)) for _, text in logical_lines(lines))


def literal_renders(lines):
    """[(lineno, names, path)] for the envsubst calls that read a literal path, not "$f"."""
    out = []
    for lineno, text in logical_lines(lines):
        for names, target in LITERAL_ENVSUBST_RE.findall(text):
            if target == "$f":
                continue
            rel = ROOT_DIR_PREFIX_RE.sub("", target)
            if "$" in rel:
                raise HarnessError(
                    "scripts/deploy.sh:%d renders %r, whose path this harness cannot resolve "
                    "without running the script, so it cannot check that whitelist"
                    % (lineno, target))
            if not os.path.isfile(os.path.join(ROOT, rel)):
                raise HarnessError(
                    "scripts/deploy.sh:%d renders %s, which does not exist — the tree moved "
                    "out from under the whitelist" % (lineno, rel))
            out.append((lineno, NAME_RE.findall(names), os.path.join(ROOT, rel)))
    return out


def claimed_bases(plan_for_glob):
    return {b for entry in plan_for_glob if entry.guard for b in entry.guard}


def governs(entry, base, claimed):
    """Whether this envsubst renders that basename — the directory-and-guard mapping deploy.sh
    already has, read rather than restated."""
    if entry.guard is not None:
        return base in entry.guard
    return base not in claimed


def substitution_whitelists(lines, plan):
    """Every envsubst whitelist in deploy.sh, paired with the files that call renders.

    A whitelist is identified by its deploy.sh line: two of them share one glob, because the
    ${base} guard splits k8s/deployments/*.yaml into sandbox.yaml and everything else.

    Two shapes: the render loops (`< "$f"`, narrowed further by a ${base} guard) and the
    keycloak template renders (`< "<literal path>"`). Both are covered, because here the
    whitelist itself is under test rather than the YAML it produces.
    """
    whitelists = []
    for pattern in sorted({entry.glob for entry in plan}):
        plan_for_glob = [entry for entry in plan if entry.glob == pattern]
        claimed = claimed_bases(plan_for_glob)
        for entry in plan_for_glob:
            paths = [path for path in sorted(globmod.glob(os.path.join(K8S_DIR, pattern)))
                     if governs(entry, os.path.basename(path), claimed)]
            label = "k8s/" + pattern
            if entry.guard:
                label += " (%s)" % ", ".join(sorted(entry.guard))
            whitelists.append(Whitelist(label, entry.lineno, set(entry.names), paths))
    for lineno, names, path in literal_renders(lines):
        whitelists.append(Whitelist(os.path.relpath(path, ROOT), lineno, set(names), [path]))
    return whitelists


def locally_defined(source):
    """Names the file itself supplies, so deploy.sh is right not to substitute them.

    Three shapes occur: a container env entry (`- name: X`), a shell assignment inside an
    embedded script (`X=...`), and an in-file envsubst that renders the name later from a
    Secret — k8s/deployments/auth-gateway.yaml's render-config initContainer is why
    ${INTERNAL_API_SECRET} must stay OUT of deploy.sh's whitelist. Derived from the file rather
    than listed here, because a hand-kept exemption list is the rot this check exists to catch.
    """
    names = set(ENV_ENTRY_RE.findall(source)) | set(SHELL_ASSIGN_RE.findall(source))
    for whitelist in INNER_ENVSUBST_RE.findall(source):
        names.update(NAME_RE.findall(whitelist))
    return names


def check_whitelist_coverage(whitelists, failures):
    """Both directions between each envsubst whitelist and the files that call renders.

    Forward: a whitelisted name no governed file spells substitutes nothing, so nothing ever
    notices it going stale.

    Reverse: a `${NAME}` a governed file spells that its whitelist omits reaches the cluster as
    the literal text `${NAME}`. A name the file defines itself is excused — unless some OTHER
    whitelist substitutes it, which makes it a build-time placeholder sitting in a directory
    whose loop will not render it, and an env entry of the same name does not save it.
    """
    substituted_somewhere = set()
    for w in whitelists:
        substituted_somewhere |= w.names
    for w in whitelists:
        if not w.paths:
            # an empty rendered directory is a state deploy.sh handles by design
            # (`[ -e "$f" ] || continue`); with no files there is nothing to be dead against.
            continue
        used = set()
        for path in w.paths:
            with open(path) as fh:
                source = fh.read()
            spelled = set(BRACED_NAME_RE.findall(source))
            used |= spelled
            local = locally_defined(source)
            for name in sorted(spelled - w.names):
                if name in substituted_somewhere or name not in local:
                    failures.append(
                        "%s spells ${%s}, which the whitelist at scripts/deploy.sh:%d does not "
                        "substitute for this file, so it reaches the cluster as the literal "
                        "text ${%s}. Add it to that whitelist, or define the name in the file."
                        % (os.path.relpath(path, ROOT), name, w.lineno, name))
        for name in sorted(w.names - used):
            failures.append(
                "scripts/deploy.sh:%d whitelists ${%s} for %s, but no file it renders spells "
                "it. The substitution does nothing, so nothing notices it going stale — drop "
                "it from the whitelist, or spell it in one of those files."
                % (w.lineno, name, w.label))


def unescape(fmt):
    """Interpret the backslash escapes bash's printf would, for the escapes deploy.sh uses."""
    out, i = [], 0
    while i < len(fmt):
        c = fmt[i]
        if c == "\\" and i + 1 < len(fmt):
            nxt = fmt[i + 1]
            if nxt in "nt\\":
                out.append({"n": "\n", "t": "\t", "\\": "\\"}[nxt])
                i += 2
                continue
        out.append(c)
        i += 1
    return "".join(out)


def multiline_values(lines):
    """Names deploy.sh can assign a MULTI-LINE value, with that value, read out of deploy.sh.

    Only `printf -v NAME '...\\n...'` produces one today. A name assigned no embedded newline
    anywhere cannot break a comment open, so it is not one of these and is not policed as one.

    The comment rule fires only for names that appear here, so a `printf -v` this parser cannot
    read is not a smaller check, it is no check: the name drops out and its comments stop being
    policed silently. Rather than pass quietly, refuse (exit 2) when deploy.sh builds a fragment
    some other way (double quotes, a heredoc, concatenation) or when no multi-line fragment is
    found at all.
    """
    values, parsed = {}, set()
    for line in lines:
        for name, fmt in PRINTF_RE.findall(line):
            parsed.add(name)
            value = unescape(fmt).replace("%%", "%").replace("%s", PRINTF_ARG)
            if "\n" in value:
                values[name] = value
    unreadable = {n for line in lines for n in PRINTF_ANY_RE.findall(line)} - parsed
    if unreadable:
        raise HarnessError(
            "deploy.sh builds %s with a `printf -v` this harness cannot read (it understands "
            "only a single-quoted format string), so it cannot tell whether that value is "
            "multi-line, and the comment rule would stop policing the name silently"
            % ", ".join(sorted(unreadable)))
    if not values:
        raise HarnessError(
            "found no multi-line `printf -v` fragment in deploy.sh (LEGACY_REDIRECT, "
            "KEYCLOAK_SERVER). With none found the comment rule below matches nothing and "
            "would report a pass while checking nothing")
    return values


def read_render_plan(lines):
    """[Render] for every manifest envsubst in deploy.sh.

    `guard` is the set of basenames a `[ "${base}" = "x.yaml" ]` branch narrows the
    envsubst to (k8s/deployments/sandbox.yaml gets its own, narrower whitelist that way).
    `lineno` is carried so a coverage failure can name the whitelist that has to change.
    """
    plan = []
    loop_glob = None
    loop_indent = None
    guard = None
    guard_indent = None
    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        m = FOR_RE.match(line.rstrip("\n"))
        if m:
            loop_indent, loop_glob = len(m.group(1)), m.group(2).strip()
            guard = guard_indent = None
            continue
        if loop_glob is None:
            continue
        if stripped == "done" and indent <= loop_indent:
            loop_glob = guard = guard_indent = None
            continue
        if guard is not None and stripped in ("fi", "else") and indent <= guard_indent:
            guard = guard_indent = None
        found = BASE_GUARD_RE.findall(line)
        if found and re.match(r"^(if|elif)\b", stripped):
            guard, guard_indent = set(found), indent
            continue
        for names in ENVSUBST_RE.findall(line):
            plan.append(Render(loop_glob, guard, NAME_RE.findall(names), lineno))
    return plan


def render_loop_survey(lines):
    """({glob}, envsubst_call_count) for deploy.sh's manifest render loops, read loosely.

    read_render_plan is deliberately literal — it insists on `for f in`, and on an
    `envsubst '<names>' < "$f"` that fits on one physical line. That literalness is what lets it
    recover the exact whitelist, and it is also what makes it brittle: rename the loop variable,
    or wrap the 400-character deployments envsubst, and it parses FEWER renders while still
    parsing some. Every file it then fails to account for gets an EMPTY whitelist, every check
    on it passes trivially, and the harness prints a reassuring pass — worse than no guard.

    This survey pins down almost nothing (any loop variable, any glob, the bare word `envsubst`
    anywhere in the body), so it keeps counting across such an edit. main() refuses (exit 2)
    when the two disagree, which turns parser drift into a warning instead of a false all-clear.
    """
    globs, calls = set(), 0
    stack = []
    for line in lines:
        stripped = line.strip()
        m = FOR_ANY_RE.match(line.rstrip("\n"))
        if m:
            stack.append([m.group(2).strip(), 0])
            continue
        if stripped == "done" and stack:
            pattern, count = stack.pop()
            if count:
                globs.add(pattern)
                calls += count
            continue
        if stack:
            # comments are stripped so that prose mentioning envsubst does not inflate the count
            stack[-1][1] += len(ENVSUBST_WORD_RE.findall(line.split("#", 1)[0]))
    return globs, calls


def whitelist_for(path, plan_for_glob):
    """The exact set of names deploy.sh substitutes into THIS file."""
    base = os.path.basename(path)
    claimed = claimed_bases(plan_for_glob)
    names = set()
    for entry in plan_for_glob:
        if governs(entry, base, claimed):
            names.update(entry.names)
    return names


def render(path, whitelist, values, rewrite_tag):
    env = dict(os.environ)
    for name in whitelist:
        env[name] = values.get(name, SINGLE_LINE_VALUE)
    shell_format = " ".join("${%s}" % n for n in sorted(whitelist))
    try:
        out = subprocess.run(["envsubst", shell_format], stdin=open(path, "rb"),
                             capture_output=True, env=env, check=True).stdout.decode()
    except FileNotFoundError:
        raise HarnessError("envsubst is not on PATH (install gettext)")
    except subprocess.CalledProcessError as exc:
        raise HarnessError("envsubst failed on %s: %s" % (path, exc.stderr.decode()[:200]))
    return out.replace(":latest", ":" + TAG) if rewrite_tag else out


def doc_ids(text):
    """(kind, name) per document, with a placeholder-derived name normalised away.

    The property wanted is "the render produces the same documents", and a resource name is not
    part of it: `${APP_NAME}` is whitelisted and already spelled inside a name
    (k8s/deployments/chat-backend.yaml), so `name: ${APP_NAME}-frontend` is a legitimate
    manifest whose name is SUPPOSED to change under the render. Comparing names literally makes
    that ordinary edit abort every deploy with what reads like a corruption report. So the
    comparison is over the count and the kinds, plus the names that were literal to begin with.
    """
    ids = []
    for d in yaml.safe_load_all(text):
        if isinstance(d, dict):
            name = (d.get("metadata") or {}).get("name")
            ids.append((d.get("kind"), None if isinstance(name, str) and "$" in name else name))
    return ids


def check_file(path, whitelist, values, rewrite_tag, failures):
    rel = os.path.relpath(path, ROOT)
    with open(path) as fh:
        source = fh.read()

    # 1. the invariant auth-gateway.yaml's header comment states, mechanised. Scoped to the
    #    names that can carry a newline, because only those break a comment open.
    for lineno, line in enumerate(source.splitlines(), 1):
        if "#" not in line:
            continue
        after_hash = line.split("#", 1)[1]
        for name in NAME_RE.findall(after_hash):
            if name in whitelist and name in values:
                failures.append(
                    "%s:%d spells ${%s} in a comment. deploy.sh envsubsts the whole document "
                    "and gives that name a MULTI-LINE value, which breaks out of the `#` and "
                    "invalidates the YAML. Name it without the ${...}." % (rel, lineno, name))

    # 2. the render deploy.sh would produce, with every multi-line fragment populated, has to
    #    still be the same YAML documents. This is what actually fails when (1) is violated
    #    anywhere the comment rule cannot see, and it needs no list of hazardous places.
    rendered = render(path, whitelist, values, rewrite_tag)
    try:
        before = doc_ids(source)
    except yaml.YAMLError:
        before = None  # unrendered file is not parseable on its own; only judge the render
    try:
        after = doc_ids(rendered)
    except yaml.YAMLError as exc:
        failures.append("%s does not parse as YAML once deploy.sh renders it: %s"
                        % (rel, str(exc).replace("\n", " ")[:300]))
        return
    if before is not None:
        if [k for k, _ in before] != [k for k, _ in after]:
            failures.append("%s renders to a different set of documents than it declares: "
                            "%s -> %s" % (rel, before, after))
        else:
            for (_, src_name), (kind, out_name) in zip(before, after):
                if src_name is not None and src_name != out_name:
                    failures.append("%s: the %s named %r is named %r after the render, though "
                                    "its name holds no placeholder — the render moved content "
                                    "between documents" % (rel, kind, src_name, out_name))

    # 3. anything the file spells as a placeholder that is NOT whitelisted must survive
    #    verbatim — auth-gateway.yaml's ${INTERNAL_API_SECRET}/${GATEWAY_IDENTITY_SECRET} are
    #    rendered later by an initContainer from a Secret, and nginx's own $host,
    #    $remote_addr, $scheme, $request_uri are runtime variables, not build-time ones.
    for name in sorted(set(NAME_RE.findall(source)) - whitelist):
        if source.count("$" + name) + source.count("${%s}" % name) > \
                rendered.count("$" + name) + rendered.count("${%s}" % name):
            failures.append("%s: $%s is not in deploy.sh's whitelist for this file but did "
                            "not survive the render verbatim" % (rel, name))


def main():
    try:
        with open(DEPLOY_SH) as fh:
            lines = fh.readlines()
    except OSError as exc:
        print("harness cannot run: %s" % exc, file=sys.stderr)
        return 2

    plan = read_render_plan(lines)
    plan_globs = {entry.glob for entry in plan}
    survey_globs, survey_calls = render_loop_survey(lines)
    if not plan:
        print("harness cannot run: found no `envsubst '...' < \"$f\"` render in %s — the "
              "render loop moved and this check is no longer reading it" % DEPLOY_SH,
              file=sys.stderr)
        return 2
    # PARTIAL drift is the dangerous case, not total drift: a plan that lost only some of its
    # renders is still non-empty, and every file it no longer accounts for is then checked
    # against an empty whitelist and passes. Compare what was parsed against the loose survey.
    if plan_globs != survey_globs or len(plan) != survey_calls:
        print("harness cannot run: parsed %d `envsubst '...' < \"$f\"` render(s) over %s from "
              "%s, but its render loops hold %d envsubst call(s) over %s. The render loop moved "
              "out from under the parser (a renamed loop variable, or an envsubst wrapped over "
              "more than one line); refusing rather than checking fewer files."
              % (len(plan), sorted(plan_globs) or "nothing", DEPLOY_SH,
                 survey_calls, sorted(survey_globs) or "nothing"), file=sys.stderr)
        return 2
    try:
        values = multiline_values(lines)
        whitelists = substitution_whitelists(lines, plan)
    except HarnessError as exc:
        print("harness cannot run: %s" % exc, file=sys.stderr)
        return 2
    # every envsubst call deploy.sh makes has to be one this check governs. Recovering only
    # some of them would check fewer whitelists and still report a pass.
    calls = envsubst_call_count(lines)
    if len(whitelists) != calls:
        print("harness cannot run: scripts/deploy.sh makes %d `envsubst '...'` call(s) but the "
              "whitelist-coverage check accounts for %d. A render moved out from under the "
              "parser; refusing rather than checking fewer whitelists." % (calls, len(whitelists)),
              file=sys.stderr)
        return 2
    rewrite_tag = any(TAG_SED_RE.search(line) for line in lines)

    expected = {p for g in plan_globs for p in globmod.glob(os.path.join(K8S_DIR, g))}
    failures = []
    checked = 0
    for pattern in sorted(plan_globs):
        plan_for_glob = [entry for entry in plan if entry.glob == pattern]
        paths = sorted(globmod.glob(os.path.join(K8S_DIR, pattern)))
        if not paths:
            # an EMPTY rendered directory is a state deploy.sh handles by design
            # (`[ -e "$f" ] || continue` in the cronjobs loop); a MISSING one means the tree
            # moved and the glob no longer names what deploy.sh renders.
            directory = os.path.dirname(os.path.join(K8S_DIR, pattern))
            if os.path.isdir(directory):
                continue
            print("harness cannot run: k8s/%s does not exist" % os.path.dirname(pattern),
                  file=sys.stderr)
            return 2
        for path in paths:
            whitelist = whitelist_for(path, plan_for_glob)
            if not whitelist:
                print("harness cannot run: deploy.sh renders k8s/%s through envsubst, but the "
                      "parser derived an EMPTY whitelist for %s — with no names to substitute "
                      "every check below passes vacuously, so this is drift, not a pass."
                      % (pattern, os.path.relpath(path, ROOT)), file=sys.stderr)
                return 2
            try:
                check_file(path, whitelist, values, rewrite_tag, failures)
            except HarnessError as exc:
                print("harness cannot run: %s" % exc, file=sys.stderr)
                return 2
            checked += 1

    if checked != len(expected):
        print("harness cannot run: checked %d manifest(s) but deploy.sh's globs match %d — "
              "some rendered file was skipped" % (checked, len(expected)), file=sys.stderr)
        return 2

    check_whitelist_coverage(whitelists, failures)

    if failures:
        for f in failures:
            print("FAIL: %s" % f, file=sys.stderr)
        return 1
    print("manifest render OK: %d manifests, %d envsubst whitelist(s) covered both ways, "
          "whitelist derived from scripts/deploy.sh, multi-line fragments populated (%s)"
          % (checked, len(whitelists), ", ".join(sorted(values)) or "none found"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
