#!/usr/bin/env sh
# Warns when a commit changes something the docs describe but leaves the doc
# untouched. Mappings mirror the "Documentation ownership" table in CLAUDE.md.
#
# This never blocks. A warning that is occasionally ignored beats a gate that
# gets bypassed with --no-verify, because a bypassed gate is both absent and
# assumed present.

set -u

root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$root" || exit 0

staged=$(git diff --cached --name-only --diff-filter=ACMRD)
[ -n "$staged" ] || exit 0

hit() {
    printf '%s\n' "$staged" | grep -qE "$1"
}

found=0
check() {
    if hit "$1" && ! hit "$2"; then
        if [ "$found" -eq 0 ]; then
            printf '\ndoc-drift warning — this commit changes code the docs describe:\n\n' >&2
            found=1
        fi
        printf '  %s\n' "$3" >&2
    fi
}

DOCS_SPEC='^(docs/project-spec\.md|README\.md)$'

check '^configs/datasets\.yaml$' \
    '^docs/(datasets-yaml-schema|adding-datasets)\.md$' \
    'configs/datasets.yaml -> docs/datasets-yaml-schema.md (data_type enum, field lists), docs/adding-datasets.md (ALL_VIEWS list)'

check '^k8s/' "$DOCS_SPEC" \
    'k8s/ -> docs/project-spec.md + README.md (services table, request routing, PVCs, hardening)'

check '^terraform/' "$DOCS_SPEC" \
    'terraform/ -> docs/project-spec.md + README.md (infrastructure, log sinks, tfvars)'

check '^scripts/(deploy|rollout|build|build-all|sync-datasets|install-git-hooks|check-worktree-paths)\.sh$' "$DOCS_SPEC" \
    'deploy/rollout/build/preflight scripts -> docs/project-spec.md + README.md (operational procedures, generated manifests)'

check '^scripts/monitor/' '^docs/project-spec\.md$' \
    'scripts/monitor/ -> docs/project-spec.md (monitored VIEWS, alert ignore patterns)'

# the CLAUDE.md sandbox row owns two docs, and satisfying one does not satisfy the
# other — so they are two checks, not one alternation
SANDBOX_PATHS='^(sandbox/|k8s/deployments/sandbox\.yaml$|k8s/network-policies/sandbox-policy\.yaml$)'

check "$SANDBOX_PATHS" \
    '^docs/code-execution-security\.md$' \
    'sandbox image/manifests/policy -> docs/code-execution-security.md (isolation boundary, egress+ingress allow-lists, the three MCP-exclusion layers, sandbox token claims)'

check "$SANDBOX_PATHS" \
    '^docs/project-spec\.md$' \
    'sandbox image/manifests/policy -> docs/project-spec.md (services table, isolation boundary summary, sandbox network policy)'

check '^(keycloak/|scripts/keycloak-)' '^docs/keycloak-apple-signin\.md$' \
    'keycloak config/scripts -> docs/keycloak-apple-signin.md (client setup, allowlist, backup paths)'

if [ "$found" -eq 1 ]; then
    printf '\n  Update the doc in this commit, or note why it does not apply.\n' >&2
    printf '  Not blocking. Mappings live in CLAUDE.md > Documentation ownership.\n\n' >&2
fi

exit 0
