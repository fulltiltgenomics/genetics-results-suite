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
# check <code paths> <doc paths> <message> [paths to ignore inside <code paths>]
#
# The 4th argument exists because a rule whose path pattern is broader than the doc
# concern it names fires where it can never apply, and a warning that fires when it
# cannot apply is how a warn-only check becomes wallpaper (genetics-results-suite-dqa).
# Use it only where the named doc demonstrably does not describe the excluded files, and
# say which property of them makes that true — "generated" is not by itself such a
# property, since a doc can and does reason about generated content.
check() {
    matched=$(printf '%s\n' "$staged" | grep -E "$1")
    [ -n "$matched" ] || return 0
    if [ -n "${4:-}" ]; then
        matched=$(printf '%s\n' "$matched" | grep -vE "$4")
        [ -n "$matched" ] || return 0
    fi
    if ! hit "$2"; then
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
# no exclusion for the generated trees: code-execution-security.md reasons about the
# *content* of sandbox/schema/ and sandbox/stubs/ directly — the shipped stubs are where
# INTERNAL_API_SECRET appears in the secrets-in-image analysis, and the build gate that
# fails on a PLACEHOLDER file in either staged tree is stated in terms of both. A
# regeneration can therefore falsify the doc, which is exactly what this rule is for.
check "$SANDBOX_PATHS" \
    '^docs/code-execution-security\.md$' \
    'sandbox image/manifests/policy/schema/stubs -> docs/code-execution-security.md (isolation boundary, egress+ingress allow-lists, the three MCP-exclusion layers, sandbox token claims, what the shipped schema docs and stubs disclose)'

# project-spec.md summarises what the sandbox *exposes*, so the generated trees are in
# scope for this one — a new view in sandbox/schema/ changes that summary.
check "$SANDBOX_PATHS" \
    '^docs/project-spec\.md$' \
    'sandbox image/manifests/policy/schema -> docs/project-spec.md (services table, isolation boundary summary, sandbox network policy, what the sandbox exposes)'

# Only the *static branding assets* under keycloak/themes/ are exempt: a stylesheet, an
# image, a font or a `.properties` bundle changes how the login page looks and reads and
# has no other effect. The exemption is by extension, not by directory, because
# keycloak/themes/genetics/login/ is exactly where a FreeMarker override (`login.ftl`) or a
# script would go, and those change how the login page *behaves* — they stay covered.
KEYCLOAK_BRANDING='^keycloak/themes/.*\.(css|properties|png|jpg|jpeg|gif|svg|ico|webp|woff|woff2|ttf|eot)$'

check '^(keycloak/|scripts/keycloak-)' '^docs/keycloak-apple-signin\.md$' \
    'keycloak config/scripts -> docs/keycloak-apple-signin.md (client setup, allowlist, backup paths)' \
    "$KEYCLOAK_BRANDING"

if [ "$found" -eq 1 ]; then
    printf '\n  Update the doc in this commit, or note why it does not apply.\n' >&2
    printf '  Not blocking. Mappings live in CLAUDE.md > Documentation ownership.\n\n' >&2
fi

exit 0
