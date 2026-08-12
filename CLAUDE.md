====


QUALITY CODING RULES


# Code changes

1. If you find errors or suggestions in code which are not DIRECTLY related to user's current request, never change it without asking first.
2. Before suggesting changes to files, always assume user might have changed the file since your last read and consider reading the file again.


# Security

1. Never commit sensitive files
2. Use environment variables for API keys and credentials
3. Keep API keys and credentials out of logs and output


# Project Specifications

1. Project documentation is maintained in files in `docs/` folder.
2. `docs/project-spec.md` is an overview of project purpose, structure and logic.
3. Create other files under `docs/` if necessary.
4. Maintain `docs/project-spec.md` and any other generated files to be up to date with the project.
5. Reread `docs/project-spec.md` often and whenever you need to refresh your context with what the project is about and implementation logic.
6. This should often be your first step in understanding a task.


# Documentation ownership

Changing a path on the left makes the doc on the right wrong until it is updated in
the same commit. `scripts/check-doc-drift.sh` warns (never blocks) on commits that
violate this; it runs from the `pre-commit` hook.

The hook lives in `.beads/hooks/pre-commit`, which is **tracked**, and git finds it
via `core.hooksPath` — **local config that no clone carries**. So a fresh clone has
the hook file and no hooks running. Run `scripts/install-git-hooks.sh` once after
cloning; it sets `core.hooksPath` and re-appends the doc-drift block if it has gone
missing, and is idempotent. `scripts/deploy.sh` and `scripts/build-all.sh` call it
with `--check`, which warns loudly (never blocks) when the checkout is unwired.
Beads owns the top of that hook file between its `BEGIN/END BEADS INTEGRATION`
markers and patches between them rather than rewriting the file, so the appended
doc-drift block survives a beads upgrade (measured against bd 1.0.3); the installer
repairs it anyway rather than trusting that.

| changed path | doc to update | what to check |
|---|---|---|
| `configs/datasets.yaml` | `docs/datasets-yaml-schema.md`, `docs/adding-datasets.md` | `data_type` enum, dataset/rule/table fields, the `ALL_VIEWS` list |
| `k8s/**` | `docs/project-spec.md`, `README.md` | services table, request routing, PVCs, container hardening |
| `terraform/**` | `docs/project-spec.md`, `README.md` | infrastructure, log sinks, tfvars and access control |
| `scripts/deploy.sh`, `rollout.sh`, `build*.sh`, `sync-datasets.sh`, `install-git-hooks.sh`, `check-worktree-paths.sh` | `docs/project-spec.md`, `README.md` | operational procedures, which manifests are generated vs committed, what the preflights check and when they stay silent |
| `scripts/monitor/**` | `docs/project-spec.md` | monitored views, alert ignore patterns |
| `keycloak/**`, `scripts/keycloak-*.sh` | `docs/keycloak-apple-signin.md` | client setup, allowlist, backup and restore paths |
| `sandbox/**`, `k8s/deployments/sandbox.yaml`, `k8s/network-policies/sandbox-policy.yaml` | `docs/code-execution-security.md`, `docs/project-spec.md` | isolation boundary (uid, rootfs, seccomp, runtime class), egress/ingress allow-lists, resource and timeout caps, the sandbox token's claims and validation rules, the three MCP-exclusion layers |

A doc is stale the moment it *enumerates* something the code no longer matches.
Counts and lists rot silently — view lists, endpoint tables, env-var tables,
service inventories — so re-derive them from the code rather than trusting them.


# Cross-repo documentation

This repo is the spec of record for the suite as a whole. The sibling repos
(`genetics-results-api`, `-db`, `-browser`, `-munge`, `genetics-mcp-server`) each
document only themselves, so a feature that spans repos leaves no single repo's
docs wrong in a way that repo can detect.

1. Adding or changing a **dataset, BigQuery view, API route, or MCP tool** anywhere
   in the suite requires updating this repo's `docs/adding-datasets.md` and
   `docs/project-spec.md`, not only the docs of the repo you edited.
2. `configs/datasets.yaml` here is the canonical copy. Sibling repos hold generated
   copies — when you change it, check whether those copies need regenerating.
3. When a change lands in a sibling repo that invalidates a count or list in this
   repo's docs, fix it here in the same session. Do not assume the other repo's
   own docs cover it.


# Software Development Behavior Guidelines

1. Don't guess and do things which you are not certain about. Ask the user instead.
2. Don't add or modify code unrelated to the specific request and context at the moment.
3. In interactive mode: only use git when asked, stage changes and propose a commit message for user review. In autonomous/orchestrator mode (e.g. ralph wiggum): commit after each completed task with a descriptive message.
4. **Always** prior to finishing a task and considering it completed, revise all the changes and update Project Specification files.
5. When trying to fix any bug or error **ALWAYS** think carefully and analyze in detail what happened and WHY? Explain and confirm with user.


# Code Conventions

1. Project structure contains `docs/`, `k8s/`, `scripts/`, and `terraform/` folders at the root
2. Code should be self-descriptive
   - Only add comments for tricky or complex parts of the code (explaining WHY something is done)
   - NO redundant and trivial comments that simply restate what the code does
3. Kubernetes manifests use standard YAML formatting
4. Terraform follows HCL best practices with variables defined in `variables.tf`
5. Shell scripts should be POSIX-compatible where possible


# Project-specific conventions

1. All services are deployed to a single GKE cluster in the `genetics` namespace
2. Docker images are stored in Google Artifact Registry (registry path derived from `project_id` in terraform, overridable via `REGISTRY` env var)
3. Configuration uses Kubernetes secrets and environment variables — never hardcode credentials
4. Terraform state is stored in a GCS backend bucket
5. Infrastructure changes go through `terraform plan` review before applying
6. Kubernetes deployments use `scripts/deploy.sh` for full deploys and `scripts/rollout.sh` for single-service updates


====

**Don't forget any of the 'QUALITY CODING RULES' above!!!**
