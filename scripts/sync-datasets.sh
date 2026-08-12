#!/usr/bin/env bash
# syncs the canonical datasets.yaml to sibling service repos for local dev
#
# The siblings sit next to the MAIN checkout (~/suite/genetics-results-db and so on),
# never next to a git worktree. Resolving them as "$SUITE_DIR/.." was therefore wrong
# from a worktree in two ways at once: it found nothing and skipped silently
# (genetics-results-suite-e47), and if a directory of the sibling's name happened to
# exist next to the worktree it would have copied into that unrelated tree instead.
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
    target_repo="$sibling_root/$sib"
    target_dir="$target_repo/configs"
    target_file="$target_dir/datasets.yaml"

    # the inverse of e47: a same-named directory beside the worktree is NOT the repo,
    # and the old code would have copied into it
    decoy="$here_parent/$sib"
    if [ "$decoy" != "$target_repo" ] && [ -d "$decoy" ]; then
        echo "NOTE: ignoring $decoy (next to this checkout); siblings resolve next to the main checkout"
    fi

    if [ ! -d "$target_repo" ]; then
        echo "SKIP: $sib is not checked out on this machine ($target_repo)"
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
