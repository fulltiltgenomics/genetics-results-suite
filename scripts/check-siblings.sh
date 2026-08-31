#!/bin/bash
# Run every sibling repo's own test lane from one place.
#
# WHY. The lanes already exist and are green; nothing triggers them. There is no CI
# anywhere in the suite, so several hundred test files across the sibling repos run only
# when somebody remembers to cd into that repo. This is the trigger, not a new lane: it
# discovers what each checkout already declares and runs that.
#
# The suite's own gates are deliberately not run here — scripts/build-all.sh already runs
# them, and a second caller would be one more copy of that list.
#
# Lanes are DISCOVERED, never listed: a Python repo with a tests/ directory gets pytest
# and a package.json gets whichever of its test/bff:test/typecheck scripts exist. Nothing
# else is executed: running a sibling's scripts by name would execute unreviewed code from
# another repo.
#
# This is NOT restricted to offline tests. A repo that declares an `offline` marker is run
# with `-m offline`; a repo that declares none runs its DEFAULT lane, whatever that
# includes — network, credentials and all. The runtime and flakiness of an unrestricted
# lane belong to the repo that owns it: the fix is a marker there, not a filter here.
#
# Exit 0 = every discovered lane passed, 1 = a lane failed, 2 = something could not run
# (a repo not checked out, no lane, no venv, no node_modules), 3 = a lane ran but reported
# setup/teardown errors with no outright failure. A could-not-run is never folded into a
# pass: a missing checkout is exactly how this stops being a gate.
#
# Errors and failures are kept apart because they are different observations, and neither
# is allowed to swallow the other: a summary that says "N failed" is a failure whatever
# else it also says, and errors with zero failures get their own outcome rather than being
# explained away as a broken environment — the cause is not established here.
#
# A lane that could not start at all is a could-not-run — see the pytest exit-code mapping
# below. scripts/test-network-policies.py is the local example of the trap: bare, it
# decides one check against ENABLE_SANDBOX and reports a failure that is really a missing
# variable.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SIBLINGS_PY="${SCRIPT_DIR}/lib/siblings.py"

QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

failed=0
errored=0
could_not_run=0
passed=0

note() { [ "$QUIET" = 1 ] || echo "$@"; }

report_pytest() {  # repo label rc output
    local repo="$1" label="$2" rc="$3" out="$4"
    # pytest's own summary line, not whatever the app logged last
    local summary
    summary="$(printf '%s\n' "$out" | grep -E '^(=+ )?[0-9]+ (passed|failed|error)|no tests ran' | tail -1)"
    summary="${summary:-pytest exit $rc}"
    local errs
    errs="$(printf '%s' "$summary" | grep -oE '[0-9]+ errors?' | head -1)"
    # the first ERROR heading, which names one thing that actually went wrong; the summary
    # only counts them
    first_error() { printf '%s\n' "$out" | grep -m1 -A2 '^ERROR ' | sed 's/^/      /'; }

    if printf '%s' "$summary" | grep -qE '(^|[^0-9])[0-9]+ failed'; then
        # failures are the observation, whatever else the run also reported
        failed=$((failed + 1))
        echo "FAIL  ${repo}  ${label}  -- ${summary}"
        [ -n "$errs" ] && first_error
        [ "$QUIET" = 1 ] || printf '%s\n' "$out" | tail -25
        return
    fi
    if [ "$rc" -ne 0 ] && [ -n "$errs" ]; then
        errored=$((errored + 1))
        echo "ERRORS ${repo}  ${label}  -- ${summary} (errors are setup/teardown or collection; cause not determined)"
        first_error
        return
    fi
    case "$rc" in
        0) passed=$((passed + 1)); echo "PASS  ${repo}  ${label}  -- ${summary}" ;;
        1) failed=$((failed + 1)); echo "FAIL  ${repo}  ${label}  -- ${summary}"
           [ "$QUIET" = 1 ] || printf '%s\n' "$out" | tail -25 ;;
        # 5 = nothing collected; 2/3/4 = interrupted (a collection error lands here),
        # internal error, usage error. None of those is a test telling us something about
        # the code, so none is a failure.
        *) could_not_run=$((could_not_run + 1))
           echo "SKIP  ${repo}  ${label}  -- pytest exit ${rc} (lane could not run)"
           [ "$QUIET" = 1 ] || printf '%s\n' "$out" | tail -15 ;;
    esac
}

python_lane() {
    local repo="$1" dir="$2"
    [ -d "$dir/tests" ] || return 1
    local py="$dir/.venv/bin/python"
    if [ ! -x "$py" ]; then
        could_not_run=$((could_not_run + 1))
        echo "SKIP  ${repo}  pytest  -- no .venv on this machine (${dir}/.venv)"
        return 0
    fi
    local -a marker=()
    grep -q '"offline:' "$dir/pyproject.toml" 2>/dev/null && marker=(-m offline)
    note ""
    note "--- ${repo}: pytest ${marker[*]:-(repo default)}"
    local out rc
    out="$(cd "$dir" && "$py" -m pytest -q "${marker[@]}" 2>&1)"; rc=$?
    report_pytest "$repo" "pytest ${marker[*]:-(repo default)}" "$rc" "$out"
    return 0
}

node_lane() {
    local repo="$1" dir="$2"
    [ -f "$dir/package.json" ] || return 1
    if [ ! -d "$dir/node_modules" ]; then
        could_not_run=$((could_not_run + 1))
        echo "SKIP  ${repo}  npm  -- node_modules is not installed (${dir})"
        return 0
    fi
    local script out rc ran=0
    for script in test bff:test typecheck; do
        python3 -c "import json,sys;sys.exit(0 if '$script' in json.load(open('$dir/package.json')).get('scripts',{}) else 1)" || continue
        ran=1
        note ""
        note "--- ${repo}: npm run ${script}"
        out="$(cd "$dir" && npm run --silent "$script" 2>&1)"; rc=$?
        case "$rc" in
            0) passed=$((passed + 1)); echo "PASS  ${repo}  npm run ${script}" ;;
            1) failed=$((failed + 1)); echo "FAIL  ${repo}  npm run ${script}"
               [ "$QUIET" = 1 ] || printf '%s\n' "$out" | tail -25 ;;
            *) could_not_run=$((could_not_run + 1))
               echo "SKIP  ${repo}  npm run ${script}  -- exit ${rc} (lane could not run)"
               [ "$QUIET" = 1 ] || printf '%s\n' "$out" | tail -15 ;;
        esac
    done
    [ "$ran" = 1 ] || return 1
    return 0
}

echo "=== Sibling gates (each repo's own lane; the suite's own gates are build-all.sh's)"

mapfile -t REPOS < <(python3 "$SIBLINGS_PY" --json | python3 -c '
import json,sys
for r in json.load(sys.stdin):
    if r != "genetics-results-suite":
        print(r)
' | sort)

if [ "${#REPOS[@]}" -eq 0 ]; then
    echo "cannot run: ${SIBLINGS_PY} listed no sibling repos" >&2
    exit 2
fi

for repo in "${REPOS[@]}"; do
    # stderr, not a canned message: siblings.py distinguishes "not on this machine" from
    # an override that names an unusable path, and only it knows which happened
    dir="$(python3 "$SIBLINGS_PY" --path "$repo" 2>&1)" || {
        could_not_run=$((could_not_run + 1))
        echo "SKIP  ${repo}  -- ${dir}"
        continue
    }
    if ! python_lane "$repo" "$dir" && ! node_lane "$repo" "$dir"; then
        could_not_run=$((could_not_run + 1))
        echo "SKIP  ${repo}  -- no test lane found in ${dir}"
    fi
done

echo ""
echo "=== ${passed} passed, ${failed} failed, ${errored} errored (setup/collection), ${could_not_run} could not run"
if [ "$failed" -gt 0 ]; then exit 1; fi
if [ "$errored" -gt 0 ]; then
    echo "=== NOT A PASS: ${errored} lane(s) reported setup/collection errors. Exiting 3." >&2
    exit 3
fi
if [ "$could_not_run" -gt 0 ]; then
    echo "=== NOT A PASS: ${could_not_run} lane(s) did not run. Exiting 2." >&2
    exit 2
fi
exit 0
