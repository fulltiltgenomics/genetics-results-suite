#!/usr/bin/env bash
# stands up a throwaway BigQuery dataset that mirrors the live genetics_results, so the
# open DDL beads (94c, eyg, 4h6.20, 4h6.21, 4ci) can be rehearsed before they touch
# production. See docs/bigquery-dev-dataset.md for the expand/verify/contract cycle.
#
# The suite spans three deployments in two projects (docs/environments.md), but none of them
# is a development environment for the data: phewas-development IS production despite its
# name, and daly-staging is not BigQuery-isolated from daly production. This script is the
# whole of it.
#
# WHY CLONES. `CREATE TABLE ... CLONE` is zero-copy and writable: the dev dataset starts
# at no additional storage cost and bills only for blocks that stop being shared with the
# base table - which a rehearsal writing to the clone causes, but so does production
# REPLACEing or DROPping the base table, so drop a clone once its production counterpart is
# gone (docs/bigquery-dev-dataset.md, Cost). Snapshots are read-only (useless for
# rehearsing DDL) and CTAS copies
# would bill all 224 GB. Verified against the real dataset with
#   bq query --dry_run --use_legacy_sql=false \
#     'CREATE TABLE `phewas-development.genetics_results.zz_probe` CLONE `phewas-development.genetics_results.datasets`'
#   -> "Query successfully validated ... upper bound of 0 bytes"
#
# WHY THE VIEWS ARE REWRITTEN, NOT COPIED. Every view in genetics_results embeds a
# fully-qualified `phewas-development.genetics_results.<table>` reference. Copied
# verbatim into a dev dataset they would keep reading PRODUCTION tables while the tables
# beside them are dev clones - a rehearsal that looks right and proves nothing. This
# script rewrites each view's SQL to the dev dataset and `verify` re-reads every dev view
# back out of BigQuery and fails if any of them still names the source dataset.
set -euo pipefail

SRC_DATASET="${SRC_DATASET:-genetics_results}"
DEV_DATASET="${DEV_DATASET:-genetics_results_dev}"
PROJECT_ID="${PROJECT_ID:-}"

# datasets that must never be the TARGET, whatever the flags say
PROTECTED_DATASETS=(genetics_results genetics_api_logs genetics_chat_logs)

APPLY=false
ASSUME_YES=false
REFRESH=false
ONLY_TABLES=""
EXCLUDE_TABLES=""
ACTION=""

usage() {
    cat <<'EOF'
Usage: scripts/bq-dev-dataset.sh <command> [options]

Commands:
  check          Read-only preflight. Touches nothing. Reports source location, object
                 inventory, what already exists in the dev dataset, and every guard.
  create         Create the dev dataset, CLONE the source tables into it and create the
                 views with their table references rewritten to the dev dataset.
  verify         Assert the dev dataset is self-contained: every source view exists in
                 dev, and NO dev view still references the source dataset. Read-only.
  rewrite        Read SQL on stdin, print it with source-dataset references rewritten to
                 the dev dataset. Use it to rehearse the datasets.yaml worked examples,
                 which name the dataset in the SQL text and so cannot be redirected by a
                 default-dataset setting:
                   scripts/bq-dev-dataset.sh rewrite < ex.sql > ex_dev.sql
                   bq query --dry_run --use_legacy_sql=false --location=europe-west1 < ex_dev.sql
  teardown       Delete the dev dataset and everything in it.

Options:
  --apply              Actually run the statements. WITHOUT THIS NOTHING IS EXECUTED -
                       create/teardown print the exact statements and exit. Dry run is
                       the default on purpose.
  --yes                Required in addition to --apply for teardown and --refresh.
  --refresh            With `create`: drop and re-clone/replace objects that already
                       exist, discarding any rehearsal state in them. Requires --yes.
                       Without it, existing dev objects are left alone (idempotent).
  --tables a,b,c       Only these base tables (views are always handled wholesale).
  --exclude a,b,c      Skip these base tables.
  --dataset NAME       Dev dataset name (default genetics_results_dev, env DEV_DATASET).
  --source NAME        Source dataset (default genetics_results, env SRC_DATASET).
  --project ID         GCP project (default: env PROJECT_ID, else `gcloud config`).
  -h, --help           This text.

Guards that cannot be overridden:
  - the target must not be one of the protected production datasets
  - the target name must carry a `dev` segment (^dev_, _dev_, _dev$, or exactly `dev`)
  - the target must not equal the source
  - source and target must be in the same project and the same location; the dataset is
    created in the source's location because a CLONE cannot cross regions

First run, in order:
  scripts/bq-dev-dataset.sh check
  scripts/bq-dev-dataset.sh create            # prints the plan, executes nothing
  scripts/bq-dev-dataset.sh create --apply
  scripts/bq-dev-dataset.sh verify
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }

# without this a missing option value makes `shift 2` fail and set -e exit 1 silently
need_value() { [ "$1" -ge 2 ] || die "option $2 needs a value"; }

while [ $# -gt 0 ]; do
    case "$1" in
        check|create|verify|teardown|rewrite)
            [ -z "$ACTION" ] || die "two commands given: $ACTION and $1"
            ACTION="$1"; shift ;;
        --apply)   APPLY=true; shift ;;
        --yes)     ASSUME_YES=true; shift ;;
        --refresh) REFRESH=true; shift ;;
        --tables)  need_value $# "$1"; ONLY_TABLES="$2"; shift 2 ;;
        --exclude) need_value $# "$1"; EXCLUDE_TABLES="$2"; shift 2 ;;
        --dataset) need_value $# "$1"; DEV_DATASET="$2"; shift 2 ;;
        --source)  need_value $# "$1"; SRC_DATASET="$2"; shift 2 ;;
        --project) need_value $# "$1"; PROJECT_ID="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown argument: $1" ;;
    esac
done

[ -n "$ACTION" ] || { usage >&2; exit 1; }

command -v bq >/dev/null 2>&1 || die "bq is not on PATH (install the Google Cloud SDK)"
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH"

if [ -z "$PROJECT_ID" ]; then
    PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
    [ -n "$PROJECT_ID" ] && [ "$PROJECT_ID" != "(unset)" ] \
        || die "no project: pass --project or set PROJECT_ID"
fi

# ---------------------------------------------------------------------------
# name guards. These run before anything else and are deliberately not overridable:
# the entire point of this script is that the thing it points at is NOT production.
# ---------------------------------------------------------------------------
guard_target_name() {
    [ -n "$DEV_DATASET" ] || die "empty target dataset name"
    # =~ and not `grep -qE`: grep matches per LINE, so a name containing a newline would
    # clear every guard below on the strength of one innocent-looking line
    [[ $DEV_DATASET =~ ^[A-Za-z0-9_]+$ ]] \
        || die "target dataset '$DEV_DATASET' is not a legal BigQuery dataset id"

    for p in "${PROTECTED_DATASETS[@]}"; do
        if [ "$DEV_DATASET" = "$p" ]; then
            echo "REFUSING: '$DEV_DATASET' is a PRODUCTION dataset in $PROJECT_ID." >&2
            echo "          This script clones over, replaces views in and can DELETE the" >&2
            echo "          dataset it is pointed at. It will not point at production." >&2
            exit 1
        fi
    done

    [[ $DEV_DATASET =~ (^dev$|^dev_|_dev_|_dev$) ]] \
        || die "target dataset '$DEV_DATASET' has no 'dev' segment. Rehearsal datasets must be
       named so that a mistyped name cannot resolve to something real: use e.g.
       ${SRC_DATASET}_dev, or dev_${SRC_DATASET}."

    [ "$DEV_DATASET" != "$SRC_DATASET" ] || die "target and source are the same dataset"
}

# ---------------------------------------------------------------------------
# source inspection
# ---------------------------------------------------------------------------
SRC_LOCATION=""
# absence is a normal answer for both of these ("has the dev dataset been created yet?"),
# so the `|| true` is load-bearing: under `set -o pipefail` a failing bq would otherwise
# take the whole script down through `set -e` at the assignment
dataset_location() {  # $1 = dataset id; prints location, empty if the dataset is absent
    bq show --project_id="$PROJECT_ID" --format=prettyjson "${PROJECT_ID}:${1}" 2>/dev/null \
        | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("location",""))
except Exception: pass' || true
}

list_objects() {  # $1 = dataset, $2 = TABLE|VIEW; prints ids one per line
    bq ls --project_id="$PROJECT_ID" --max_results=10000 --format=prettyjson "${PROJECT_ID}:${1}" 2>/dev/null \
        | python3 -c 'import json,sys
want=sys.argv[1]
try: rows=json.load(sys.stdin)
except Exception: rows=[]
for r in rows:
    if r.get("type")==want: print(r["tableReference"]["tableId"])' "$2" || true
}

view_query() {  # $1 = dataset, $2 = view id
    bq show --project_id="$PROJECT_ID" --format=prettyjson "${PROJECT_ID}:${1}.${2}" \
        | python3 -c 'import json,sys
d=json.load(sys.stdin)
v=d.get("view") or {}
if v.get("useLegacySql"):
    sys.stderr.write("legacy SQL view, cannot be rewritten safely\n"); sys.exit(2)
sys.stdout.write(v.get("query",""))'
}

# One program, two modes, so that what the rewriter rewrites and what the checks look for
# can never drift apart - the earlier split between a python rewrite and a `grep -E` check
# is exactly how per-part-backticked references got rewritten by neither and caught by
# neither. Both modes read SQL on stdin.
#
#   rewrite  prints the SQL with source-dataset references pointed at the dev dataset.
#            Handles the three-part `project.dataset.object` form (what all 15 views in
#            genetics_results use) and the two-part `dataset.object` form (the
#            datasets.yaml worked examples), with backticks around the whole path, around
#            each part, or absent, and whitespace or newlines around the dots.
#   residue  prints the lines that still reference the source dataset, and exits 0 when it
#            found any / 1 when the SQL is clean - `grep -q` semantics, so callers read
#            `if residue="$(... residue)"; then <it is dirty>`.
#
# `python3 -c` and not `python3 - <<HEREDOC`: with a heredoc the SCRIPT is python's stdin,
# so the piped SQL is discarded and the rewrite silently emits nothing.
sql_tool() {  # $1 = rewrite|residue; SQL on stdin
    python3 -c '
import re, sys
mode, project, src, dev = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
sql = sys.stdin.read()
q = chr(39)
# comments and string literals are left alone; backtick-quoted identifiers are NOT (they
# are where the references live) and are matched here only so a quote character inside one
# cannot be mistaken for the start of a string literal
#
# a raw prefix (r/R, optionally with b/B) turns backslash escapes OFF in BigQuery, so a raw
# literal ending in a backslash is COMPLETE. An escape-aware body would read that trailing
# backslash-quote as an escaped quote, run on to the NEXT quote, and swallow whatever lies
# between - a real table reference in there is then neither rewritten nor reported, which is
# the exact silent failure this tokenizer exists to prevent. So the escape-aware body is used
# only when the prefix is not raw, for every quote form.
RAW, NRAW = "(?:[rR][bB]?|[bB][rR])", "[bB]?"
def lits(d):  # d = quote character; triple forms first so they win over the single ones
    esc3, esc1 = "(?:\\\\.|[^\\\\])*?", "(?:\\\\.|[^" + d + "\\\\\n])*"
    return (RAW + d * 3 + ".*?" + d * 3 + "|" + NRAW + d * 3 + esc3 + d * 3
            + "|" + RAW + d + "[^" + d + "\n]*" + d
            + "|" + NRAW + d + esc1 + d)
TOKEN = re.compile(
    "`(?:[^`]|``)*`"
    "|--[^\n]*|#[^\n]*|/\\*.*?\\*/"
    "|" + lits(q) + "|" + lits(chr(34)), re.S)
P, S = re.escape(project), re.escape(src)
THREE = re.compile("(?<![\\w.-])(`?)" + P + "\\1(\\s*\\.\\s*)(`?)" + S + "\\3(?=\\s*\\.)")
TWO = re.compile("(?<![\\w.-])(`?)" + S + "\\1(?=\\s*\\.)")
FIND = re.compile("(?<![A-Za-z0-9_])`?" + S + "`?\\s*\\.")

parts, last = [], 0
for m in TOKEN.finditer(sql):
    if m.group(0)[0] == "`":
        continue
    parts.append((sql[last:m.start()], False))
    parts.append((m.group(0), True))
    last = m.end()
parts.append((sql[last:], False))

if mode == "rewrite":
    def rw(t):
        t = THREE.sub(lambda m: m.group(1) + project + m.group(1) + m.group(2)
                      + m.group(3) + dev + m.group(3), t)
        return TWO.sub(lambda m: m.group(1) + dev + m.group(1), t)
    sys.stdout.write("".join(t if skip else rw(t) for t, skip in parts))
    sys.exit(0)

masked = "".join(re.sub("[^\n]", " ", t) if skip else t for t, skip in parts)
lines = sql.split("\n")
seen = []
for m in FIND.finditer(masked):
    n = masked.count("\n", 0, m.start()) + 1
    if n not in seen:
        seen.append(n)
        sys.stdout.write("%d: %s\n" % (n, lines[n - 1].strip()))
if any(skip and FIND.search(t) for t, skip in parts):
    sys.stderr.write("note: a comment or string literal mentions " + src
                     + "; left as-is, not counted as a residue\n")
sys.exit(0 if seen else 1)
' "$1" "$PROJECT_ID" "$SRC_DATASET" "$DEV_DATASET"
}

rewrite_sql() { sql_tool rewrite; }

# the required verification: read every dev view back OUT of BigQuery and fail if any of
# them still names the source dataset. A view that slipped through would silently read
# production tables while the tables around it are dev clones.
verify_dataset() {
    local rc=0 src_views dev_views v q
    src_views="$(list_objects "$SRC_DATASET" VIEW)"
    dev_views="$(list_objects "$DEV_DATASET" VIEW)"

    [ -n "$(dataset_location "$DEV_DATASET")" ] || die "dev dataset ${PROJECT_ID}:${DEV_DATASET} does not exist"

    local src_loc dev_loc
    src_loc="$(dataset_location "$SRC_DATASET")"
    dev_loc="$(dataset_location "$DEV_DATASET")"
    if [ "$src_loc" != "$dev_loc" ]; then
        echo "FAIL: location mismatch - source $src_loc, dev $dev_loc. Nothing will join." >&2
        rc=1
    else
        echo "OK: both datasets are in $dev_loc"
    fi

    for v in $src_views; do
        if ! printf '%s\n' "$dev_views" | grep -qxF "$v"; then
            echo "FAIL: view $v exists in $SRC_DATASET but not in $DEV_DATASET" >&2
            rc=1
        fi
    done

    local hits
    for v in $dev_views; do
        q="$(view_query "$DEV_DATASET" "$v")" || { echo "FAIL: cannot read DDL of $v" >&2; rc=1; continue; }
        if hits="$(printf '%s' "$q" | sql_tool residue)"; then
            echo "FAIL: dev view ${DEV_DATASET}.${v} still references ${SRC_DATASET}:" >&2
            printf '%s\n' "$hits" | sed 's/^/      /' >&2
            rc=1
        else
            echo "OK: ${DEV_DATASET}.${v} references only ${DEV_DATASET}"
        fi
    done

    # base tables every dev view needs must be present, or the view is a time bomb that
    # only fails when someone queries it
    local t
    for t in $(list_objects "$SRC_DATASET" TABLE); do
        if printf '%s\n' "$dev_views" | grep -qxF "${t}_v" \
           && ! list_objects "$DEV_DATASET" TABLE | grep -qxF "$t"; then
            echo "FAIL: dev view ${t}_v exists but its base table $t was not cloned" >&2
            rc=1
        fi
    done

    if [ "$rc" -eq 0 ]; then
        echo
        echo "VERIFIED: ${PROJECT_ID}:${DEV_DATASET} is self-contained."
    else
        echo >&2
        echo "VERIFICATION FAILED - do NOT rehearse anything in this dataset." >&2
    fi
    return "$rc"
}

# ---------------------------------------------------------------------------
# statement execution. In dry-run mode (the default) statements are printed, never run.
# ---------------------------------------------------------------------------
# --location is not optional: with a dataset the CLI cannot yet resolve, bq falls back to
# US and the statement fails with a confusing "not found in location US"
run_sql() {  # $1 = statement
    if [ "$APPLY" = true ]; then
        printf '%s\n' "$1" | bq query --project_id="$PROJECT_ID" --location="$SRC_LOCATION" \
            --use_legacy_sql=false --format=none --quiet
    else
        printf '%s;\n' "${1%;}"
    fi
}

selected_tables() {
    local all t keep
    all="$(list_objects "$SRC_DATASET" TABLE)"
    if [ -n "$ONLY_TABLES" ]; then
        keep=""
        for t in ${ONLY_TABLES//,/ }; do
            printf '%s\n' "$all" | grep -qxF "$t" || die "--tables names '$t', which is not a base table in $SRC_DATASET"
            keep="${keep}${t}"$'\n'
        done
        all="$keep"
    fi
    # -F: without it `--exclude '.*'` is a regex that removes every table
    for t in ${EXCLUDE_TABLES//,/ }; do
        all="$(printf '%s\n' "$all" | grep -vxF "$t" || true)"
    done
    printf '%s\n' "$all" | grep -v '^$' || true
}

do_check() {
    guard_target_name
    echo "project:        $PROJECT_ID"
    echo "source dataset: $SRC_DATASET"
    echo "dev dataset:    $DEV_DATASET"
    SRC_LOCATION="$(dataset_location "$SRC_DATASET")"
    [ -n "$SRC_LOCATION" ] || die "source dataset ${PROJECT_ID}:${SRC_DATASET} not found or not readable"
    echo "source location: $SRC_LOCATION  (the dev dataset is created here; a CLONE cannot cross regions)"
    echo
    echo "source base tables: $(list_objects "$SRC_DATASET" TABLE | wc -l)"
    echo "source views:       $(list_objects "$SRC_DATASET" VIEW | wc -l)"
    echo
    local dev_loc
    dev_loc="$(dataset_location "$DEV_DATASET")"
    if [ -z "$dev_loc" ]; then
        echo "dev dataset does not exist yet - 'create --apply' will make it in $SRC_LOCATION"
    else
        echo "dev dataset EXISTS in $dev_loc with $(list_objects "$DEV_DATASET" TABLE | wc -l) tables and $(list_objects "$DEV_DATASET" VIEW | wc -l) views"
        [ "$dev_loc" = "$SRC_LOCATION" ] || echo "WARN: dev dataset location $dev_loc != source $SRC_LOCATION - clones into it will fail"
        echo "     'create' without --refresh leaves existing objects untouched"
    fi
    echo
    echo "guards passed."
}

do_create() {
    guard_target_name
    SRC_LOCATION="$(dataset_location "$SRC_DATASET")"
    [ -n "$SRC_LOCATION" ] || die "source dataset ${PROJECT_ID}:${SRC_DATASET} not found or not readable"

    if [ "$REFRESH" = true ] && [ "$ASSUME_YES" != true ]; then
        die "--refresh discards whatever a rehearsal has done to the existing dev objects. Add --yes."
    fi

    # resolved before anything is created: assign, THEN iterate, because `for t in
    # $(selected_tables)` swallows the die() inside the command substitution - it exits
    # only the subshell, and a --tables typo would go on to build 15 views over zero base
    # tables and report success. Assignment failure does trip set -e.
    local selected
    selected="$(selected_tables)"

    if [ "$APPLY" != true ]; then
        echo "=== DRY RUN. Nothing below is executed. Re-run with --apply. ==="
        echo
    fi

    local dev_loc
    dev_loc="$(dataset_location "$DEV_DATASET")"
    if [ -z "$dev_loc" ]; then
        if [ "$APPLY" = true ]; then
            echo "Creating dataset ${PROJECT_ID}:${DEV_DATASET} in $SRC_LOCATION"
            bq mk --project_id="$PROJECT_ID" --dataset --location="$SRC_LOCATION" \
                --description="REHEARSAL COPY of ${SRC_DATASET} (genetics-results-suite-44g). Clones + rewritten views. NOT LIVE. Safe to drop." \
                "${PROJECT_ID}:${DEV_DATASET}"
        else
            echo "bq mk --project_id=$PROJECT_ID --dataset --location=$SRC_LOCATION ${PROJECT_ID}:${DEV_DATASET}"
        fi
    else
        [ "$dev_loc" = "$SRC_LOCATION" ] \
            || die "dev dataset already exists in $dev_loc but the source is in $SRC_LOCATION.
       A CLONE cannot cross regions. Tear the dev dataset down and recreate it."
        echo "dataset ${PROJECT_ID}:${DEV_DATASET} already exists in $dev_loc"
    fi
    echo

    local existing_tables existing_views t v q hits
    existing_tables="$(list_objects "$DEV_DATASET" TABLE)"
    existing_views="$(list_objects "$DEV_DATASET" VIEW)"

    echo "--- base tables (zero-copy clones) ---"
    for t in $selected; do
        if printf '%s\n' "$existing_tables" | grep -qxF "$t"; then
            if [ "$REFRESH" = true ]; then
                run_sql "DROP TABLE \`${PROJECT_ID}.${DEV_DATASET}.${t}\`"
                run_sql "CREATE TABLE \`${PROJECT_ID}.${DEV_DATASET}.${t}\` CLONE \`${PROJECT_ID}.${SRC_DATASET}.${t}\`"
            else
                echo "-- SKIP ${t}: already present in ${DEV_DATASET} (use --refresh --yes to re-clone)"
            fi
        else
            run_sql "CREATE TABLE IF NOT EXISTS \`${PROJECT_ID}.${DEV_DATASET}.${t}\` CLONE \`${PROJECT_ID}.${SRC_DATASET}.${t}\`"
        fi
    done
    echo

    echo "--- views (table references rewritten to ${DEV_DATASET}) ---"
    for v in $(list_objects "$SRC_DATASET" VIEW); do
        if printf '%s\n' "$existing_views" | grep -qxF "$v" && [ "$REFRESH" != true ]; then
            echo "-- SKIP ${v}: already present in ${DEV_DATASET} (use --refresh --yes to re-sync from ${SRC_DATASET})"
            continue
        fi
        q="$(view_query "$SRC_DATASET" "$v" | rewrite_sql)"
        [ -n "$q" ] || die "rewriting ${SRC_DATASET}.${v} produced an empty definition - refusing to create an empty view"
        if hits="$(printf '%s' "$q" | sql_tool residue)"; then
            die "refusing to create ${DEV_DATASET}.${v}: the rewrite left a reference to ${SRC_DATASET}:
$(printf '%s\n' "$hits" | sed 's/^/       /')
       Rewriting this view needs a human. Report it."
        fi
        run_sql "CREATE OR REPLACE VIEW \`${PROJECT_ID}.${DEV_DATASET}.${v}\` AS
${q}"
    done

    echo
    if [ "$APPLY" = true ]; then
        echo "Done. Now run:  $0 verify --dataset $DEV_DATASET"
    else
        echo "=== DRY RUN ended. Nothing was executed. ==="
    fi
}

do_teardown() {
    guard_target_name
    [ -n "$(dataset_location "$DEV_DATASET")" ] || { echo "dev dataset ${PROJECT_ID}:${DEV_DATASET} does not exist - nothing to do"; return 0; }

    echo "About to DELETE ${PROJECT_ID}:${DEV_DATASET} and everything in it:"
    echo "  tables: $(list_objects "$DEV_DATASET" TABLE | wc -l)"
    echo "  views:  $(list_objects "$DEV_DATASET" VIEW | wc -l)"
    echo

    if [ "$APPLY" != true ]; then
        echo "=== DRY RUN. Nothing deleted. Re-run with --apply --yes. ==="
        echo "bq rm -r -f -d ${PROJECT_ID}:${DEV_DATASET}"
        return 0
    fi
    [ "$ASSUME_YES" = true ] || die "teardown needs --yes as well as --apply"

    # same shape of guard as genetics-results-db setup_bigquery.sh --recreate: make the
    # operator type the name, so a wrong --dataset cannot be waved through by muscle memory
    if [ -t 0 ]; then
        read -r -p "Type the dataset name to confirm: " typed
        [ "$typed" = "$DEV_DATASET" ] || die "typed '$typed', expected '$DEV_DATASET' - aborting"
    else
        die "teardown refuses to run non-interactively; it needs the dataset name typed at a tty"
    fi

    bq rm -r -f -d "${PROJECT_ID}:${DEV_DATASET}"
    echo "Deleted ${PROJECT_ID}:${DEV_DATASET}"
}

case "$ACTION" in
    check)    do_check ;;
    create)   do_create ;;
    verify)   guard_target_name; verify_dataset ;;
    teardown) do_teardown ;;
    rewrite)  guard_target_name; rewrite_sql ;;
esac
