#!/usr/bin/env sh
# Reports the tools that, when run from a git worktree, resolve a path into the
# MAIN CHECKOUT and then degrade without erroring.
#
# This is a recurring class, not a list of unrelated bugs. Four instances have been
# found so far, each by accident, each after the silent degradation had already
# happened:
#
#   genetics-results-suite-e47  sync-datasets.sh resolved ../genetics-results-{db,api}
#                               from the checkout it ran in, found nothing in a worktree,
#                               warned and exited 0 having copied nothing. FIXED in the
#                               script itself — it resolves from the git common dir now,
#                               so this file no longer checks it
#   genetics-results-suite-82s  terraform.tfvars is gitignored and exists only in the
#                               main checkout, so terraform from a worktree falls back
#                               to destructive variable defaults
#   genetics-results-suite-rxw  core.hooksPath is local git config shared across
#                               worktrees, so it points at the main checkout's hooks
#   genetics-results-suite-0xs  bd exports .beads/issues.jsonl next to the Dolt store,
#                               which lives in the main checkout, so the worktree's
#                               tracked copy is never written
#
# What they share: the tool succeeds, prints nothing alarming, and the operator reads
# the absence of an error as success. This script makes the divergence explicit before
# a build or deploy relies on it. It only ever WARNS — call it with `|| true`.
#
# It is silent in the main checkout, where every path below resolves locally by
# definition.
#
# Adding a case: state the path the tool will actually use, compare it to the path
# inside this worktree, and report only when they differ. Do not report a general
# "you are in a worktree" warning — an alert that fires every time is an alert nobody
# reads.
#
# Usage:
#   scripts/check-worktree-paths.sh          report, exit 1 if anything diverges
#   scripts/check-worktree-paths.sh --check  identical; accepted so call sites read
#                                            the same as install-git-hooks.sh --check

set -u

case "${1:-}" in
    --check | "") ;;
    *)
        echo "usage: $0 [--check]" >&2
        exit 2
        ;;
esac

toplevel=$(git rev-parse --show-toplevel 2>/dev/null) || {
    echo "check-worktree-paths: not a git repository" >&2
    exit 2
}

common=$(git rev-parse --git-common-dir 2>/dev/null) || exit 2
common=$(cd "$common" && pwd) || exit 2
# assumes the git dir is named .git directly under the main worktree root, the same
# assumption scripts/install-git-hooks.sh makes; a --separate-git-dir or bare-main
# layout would resolve this wrongly
main_root=$(dirname "$common")

# in the main checkout main_root == toplevel and there is nothing to diverge
[ "$toplevel" = "$main_root" ] && exit 0

problems=0
messages=""
note() {
    problems=$((problems + 1))
    messages="${messages}  - $1
"
}

# --- bd: the Dolt store, and therefore the jsonl export, live in the main checkout ---
# One Dolt database is shared by every worktree (a worktree has no .beads/embeddeddolt
# of its own), so a single canonical export next to it is correct. What is misleading
# is that the worktree carries a TRACKED .beads/issues.jsonl that nothing here writes.
# The structural facts (no local embeddeddolt, a tracked jsonl) hold in EVERY worktree
# permanently, so they cannot be the trigger — only an actual content difference can.
if [ -f "$toplevel/.beads/issues.jsonl" ] &&
    [ ! -d "$toplevel/.beads/embeddeddolt" ] &&
    [ -d "$main_root/.beads/embeddeddolt" ] &&
    [ -f "$main_root/.beads/issues.jsonl" ] &&
    ! cmp -s "$toplevel/.beads/issues.jsonl" "$main_root/.beads/issues.jsonl"; then
    here=$(wc -l <"$toplevel/.beads/issues.jsonl" 2>/dev/null | tr -d ' ')
    there=$(wc -l <"$main_root/.beads/issues.jsonl" 2>/dev/null | tr -d ' ')
    note "bd writes .beads/issues.jsonl to $main_root/.beads/, next to the shared Dolt store.
    This worktree's tracked copy holds ${here:-?} issues; the one bd actually writes holds ${there:-?}.
    'git add .beads/issues.jsonl' from here stages nothing and git then reports a clean
    tree, which reads as 'already up to date'. Bead state is safe (Dolt is authoritative).
    Refresh this worktree's copy in place — the Dolt store is shared, so an export taken
    from here is the same current snapshot:
        bd export -o .beads/issues.jsonl"
fi

# --- sync-datasets.sh: fixed at the source, nothing to check ---
# It now resolves the siblings from the git COMMON dir, so from a worktree it finds the
# same repos the main checkout would, ignores a same-named directory sitting next to the
# worktree, and exits nonzero if it cannot resolve the sibling root at all. A check here
# would have nothing left to compare.

# --- terraform.tfvars: gitignored, so it exists only in the main checkout ---
if [ ! -f "$toplevel/terraform/terraform.tfvars" ] &&
    [ -f "$main_root/terraform/terraform.tfvars" ]; then
    note "terraform/terraform.tfvars exists only in $main_root (it is gitignored).
    terraform run from here falls back to variable DEFAULTS, which are not a no-op subset
    of the live config. The require_tfvars precondition and deploy.sh block an apply, but
    'terraform apply -target=' and 'terraform destroy' bypass it, and build.sh/build-all.sh
    just silently fall back to APP_NAME=FinnGenie."
fi

# --- core.hooksPath: local git config, shared across worktrees ---
# Pointing at the main checkout is intended (install-git-hooks.sh sets it that way and
# the hook files are tracked, so they normally match). It only matters when this branch
# has edited the hooks: the edits are inert here.
hooks_path=$(git config --get core.hooksPath 2>/dev/null || true)
if [ -n "$hooks_path" ]; then
    case "$hooks_path" in
    # a relative value is resolved against the working tree git runs the hook from,
    # so it is already per-worktree; nothing can diverge
    "$toplevel"/* | [!/]*) ;;
    *)
        # Not handled: only hooks present on BOTH sides are compared, so a hook added
        # on this branch is missed; and when hooks_path is not the main checkout's
        # .beads/hooks the message blames "your edit" for an unrelated difference.
        for hook in pre-commit post-merge post-checkout pre-push prepare-commit-msg; do
            [ -f "$toplevel/.beads/hooks/$hook" ] || continue
            [ -f "$hooks_path/$hook" ] || continue
            if ! cmp -s "$toplevel/.beads/hooks/$hook" "$hooks_path/$hook"; then
                note "git runs hooks from '$hooks_path', outside this worktree, and this branch's
    .beads/hooks/$hook differs from the copy that actually runs. Your edit to that hook has
    no effect on commits made here."
            fi
        done
        ;;
    esac
fi

if [ "$problems" -gt 0 ]; then
    printf '\nthis is a git worktree, and %d path(s) resolve into the main checkout instead:\n\n' "$problems" >&2
    printf '%s' "$messages" >&2
    printf '  main checkout: %s\n  this worktree: %s\n\n' "$main_root" "$toplevel" >&2
    exit 1
fi

exit 0
