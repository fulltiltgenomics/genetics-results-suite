#!/usr/bin/env bash
# syncs the canonical datasets.yaml to sibling service repos for local dev
#
# Usage:
#   scripts/sync-datasets.sh                       -> the sibling MAIN checkouts (the default)
#   scripts/sync-datasets.sh --tree worktree       -> <sibling>/.claude/worktrees/<name>
#   scripts/sync-datasets.sh --tree worktree --worktree db-only-architecture
#
# The worktree name defaults to $DEV_WORKTREE, else this checkout's own directory name,
# which is what dev-stack.sh passes. The SOURCE is always this script's own tree, so the
# copy a tree receives is the canonical file of the tree the script was run from: the
# suite's own datasets.yaml differs substantially between branches, and syncing one
# branch's config into a tree running another's is the failure this flag exists to avoid.
# Run the sync from the same tree as the services that will read it — `dev-stack.sh up`
# does exactly that, invoking the sync-datasets.sh of the tree it is bringing up.
#
# The siblings sit next to the MAIN checkout (~/suite/genetics-results-db and so on),
# never next to a git worktree. Resolving them as "$SUITE_DIR/.." was therefore wrong
# from a worktree in two ways at once: it found nothing and skipped silently, and if a
# directory of the sibling's name happened to exist next to the worktree it would have copied
# into that unrelated tree instead.
#
# Failure modes are deliberately split:
#   - a sibling that is simply not checked out here      -> SKIP, exit 0
#   - the sibling root cannot be resolved, or a resolved -> ERROR, exit 1
#     path is not actually that repo
# deploy.sh calls this best-effort (`|| echo WARN ... continuing`), so a nonzero exit
# is loud without turning a missing optional sibling into a failed deploy.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SUITE_DIR="$(dirname "$SCRIPT_DIR")"
SOURCE="$SUITE_DIR/configs/datasets.yaml"

TREE=main
WORKTREE_NAME="${DEV_WORKTREE:-$(basename "$SUITE_DIR")}"

while [ $# -gt 0 ]; do
    case "$1" in
        --tree) shift; TREE="${1:-}" ;;
        --worktree) shift; WORKTREE_NAME="${1:-}" ;;
        -h | --help) sed -n '2,/^set -euo pipefail/p' "$0" | sed -n 's/^# \{0,1\}//p'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

case "$TREE" in
    main) ;;
    worktree) [ -n "$WORKTREE_NAME" ] || { echo "ERROR: --worktree needs a name" >&2; exit 2; } ;;
    *) echo "--tree must be 'main' or 'worktree', got '$TREE'" >&2; exit 2 ;;
esac

if [ ! -f "$SOURCE" ]; then
    echo "ERROR: source file not found: $SOURCE" >&2
    exit 1
fi

SIBLINGS=(
    "genetics-results-db"
    "genetics-results-api"
)

if [ -n "${SUITE_SIBLING_ROOT:-}" ]; then
    if [ ! -d "$SUITE_SIBLING_ROOT" ]; then
        echo "ERROR: SUITE_SIBLING_ROOT is set to '$SUITE_SIBLING_ROOT', which is not a directory" >&2
        exit 1
    fi
    sibling_root="$(cd "$SUITE_SIBLING_ROOT" && pwd)"
else
    # --git-common-dir is the MAIN checkout's .git even when this runs from a worktree,
    # but git may answer with a path relative to the directory it ran in, so absolutise
    # it before taking its parent (same incantation as scripts/install-git-hooks.sh)
    common="$(git -C "$SUITE_DIR" rev-parse --git-common-dir 2>/dev/null)" || {
        echo "ERROR: cannot resolve where the sibling repos live: '$SUITE_DIR' is not a git checkout." >&2
        echo "       Set SUITE_SIBLING_ROOT to the directory that holds ${SIBLINGS[*]} and re-run." >&2
        exit 1
    }
    common="$(cd "$SUITE_DIR" && cd "$common" && pwd)" || {
        echo "ERROR: cannot resolve the git common dir of '$SUITE_DIR' to an absolute path." >&2
        exit 1
    }
    # assumes the git dir is named .git directly under the main checkout root, the same
    # assumption install-git-hooks.sh and check-worktree-paths.sh make; a
    # --separate-git-dir or bare-main layout needs SUITE_SIBLING_ROOT
    main_root="$(dirname "$common")"
    sibling_root="$(dirname "$main_root")"
fi

echo "Sibling repos resolve under: $sibling_root"

here="$(git -C "$SUITE_DIR" rev-parse --show-toplevel 2>/dev/null || echo "$SUITE_DIR")"
here_parent="$(dirname "$here")"

failed=0

for sib in "${SIBLINGS[@]}"; do
    main_checkout="$sibling_root/$sib"
    if [ "$TREE" = worktree ]; then
        target_repo="$main_checkout/.claude/worktrees/$WORKTREE_NAME"
    else
        target_repo="$main_checkout"
    fi
    target_dir="$target_repo/configs"
    target_file="$target_dir/datasets.yaml"

    # the inverse of e47: a same-named directory beside the worktree is NOT the repo,
    # and the old code would have copied into it
    decoy="$here_parent/$sib"
    if [ "$decoy" != "$target_repo" ] && [ -d "$decoy" ]; then
        echo "NOTE: ignoring $decoy (next to this checkout); siblings resolve next to the main checkout"
    fi

    if [ ! -d "$main_checkout" ]; then
        echo "SKIP: $sib is not checked out on this machine ($main_checkout)"
        continue
    fi

    if [ ! -d "$target_repo" ]; then
        echo "SKIP: $sib has no '$WORKTREE_NAME' worktree ($target_repo)"
        continue
    fi

    # a directory of the right name is not necessarily the right repo
    # tolerant of TOML spelling (spacing, single or double quotes) so a reformat of a
    # legitimate sibling cannot fail the sync, but still anchored to the project name
    if ! grep -qE "^name *= *[\"']$sib[\"']" "$target_repo/pyproject.toml" 2>/dev/null; then
        echo "ERROR: $target_repo exists but is not the $sib repo" >&2
        echo "       (its pyproject.toml has no 'name = \"$sib\"') — refusing to copy into it." >&2
        failed=1
        continue
    fi

    mkdir -p "$target_dir"
    cp "$SOURCE" "$target_file"
    echo "OK: copied to $target_file"
done

exit "$failed"
