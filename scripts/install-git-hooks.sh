#!/usr/bin/env sh
# Wires this checkout up to the tracked hooks in .beads/hooks.
#
# Two things have to be true for scripts/check-doc-drift.sh to run on a commit,
# and only one of them survives `git clone`:
#
#   1. .beads/hooks/pre-commit exists and carries the doc-drift block. The file is
#      tracked, so a clone gets it. Beads owns the top of that file (the
#      "BEADS INTEGRATION" markers) and patches between the markers rather than
#      rewriting the file — measured against bd 1.0.3 with `bd hooks install`,
#      `--force`, and a forced version-marker bump — so the appended block does
#      survive a beads upgrade. This script re-appends it anyway, because that
#      guarantee is beads' implementation detail and not ours to rely on.
#   2. core.hooksPath points at .beads/hooks. That is LOCAL git config, stored in
#      the git dir, and is not tracked by anything. A fresh clone has no hooks at
#      all until beads or this script sets it, and the resulting silence looks
#      exactly like a clean commit.
#
# Usage:
#   scripts/install-git-hooks.sh          repair both, print what changed
#   scripts/install-git-hooks.sh --check  report only, exit 1 if anything is missing

set -u

mode=install
case "${1:-}" in
    --check) mode=check ;;
    "") ;;
    *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

# hooks live in the MAIN checkout even when this runs from a worktree, and
# core.hooksPath is shared across worktrees, so resolve via the common git dir
common=$(git rev-parse --git-common-dir 2>/dev/null) || {
    echo "install-git-hooks: not a git repository" >&2
    exit 2
}
common=$(cd "$common" && pwd) || exit 2
# assumes the git dir is named .git directly under the worktree root; a
# --separate-git-dir checkout or a bare-main layout would resolve this wrongly
main_root=$(dirname "$common")
hooks_dir="$main_root/.beads/hooks"
pre_commit="$hooks_dir/pre-commit"

MARKER='# --- doc-drift warning (not managed by beads) ---'

problems=0
messages=""
note() {
    problems=$((problems + 1))
    messages="${messages}  $1
"
}

current=$(git config --get core.hooksPath 2>/dev/null || true)
path_ok=0
[ "$current" = "$hooks_dir" ] && path_ok=1

block_ok=0
[ -f "$pre_commit" ] && grep -qF "$MARKER" "$pre_commit" && block_ok=1

if [ "$mode" = check ]; then
    [ "$path_ok" -eq 1 ] || note "core.hooksPath is '${current:-unset}', expected '$hooks_dir' — no git hooks run in this checkout"
    [ "$block_ok" -eq 1 ] || note "$pre_commit is missing the doc-drift block — commits will not be checked against the documentation-ownership table"
    if [ "$problems" -gt 0 ]; then
        printf '\ngit hooks are not wired up in this checkout:\n\n' >&2
        printf '%s' "$messages" >&2
        printf '\n  Fix: %s\n\n' "scripts/install-git-hooks.sh" >&2
        exit 1
    fi
    exit 0
fi

changed=0

if [ "$path_ok" -eq 0 ]; then
    git config core.hooksPath "$hooks_dir" || exit 2
    echo "install-git-hooks: core.hooksPath -> $hooks_dir"
    changed=1
fi

if [ "$block_ok" -eq 0 ]; then
    mkdir -p "$hooks_dir"
    if [ ! -f "$pre_commit" ]; then
        printf '#!/usr/bin/env sh\n' > "$pre_commit"
    fi
    # this block must stay byte-identical to .beads/hooks/pre-commit's doc-drift
    # block — if that file's block is ever edited without updating this one, this
    # re-append becomes a second source of truth and silently restores the old text
    cat >> "$pre_commit" <<EOF

$MARKER
# never blocks: || true keeps a broken check from stopping a commit
if [ -x ./scripts/check-doc-drift.sh ]; then
  ./scripts/check-doc-drift.sh || true
fi
EOF
    chmod +x "$pre_commit"
    echo "install-git-hooks: re-appended the doc-drift block to $pre_commit"
    changed=1
fi

[ "$changed" -eq 0 ] && echo "install-git-hooks: already wired up ($hooks_dir)"
exit 0
