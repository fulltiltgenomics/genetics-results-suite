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
violate this; it runs from the `pre-commit` hook. It reads `git diff --cached`, so it
only ever sees **staged** changes — run by hand with an empty index it checks nothing
and says so; that message is not a pass.

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
| `scripts/deploy.sh`, `rollout.sh`, `build*.sh`, `sync-datasets.sh`, `install-git-hooks.sh`, `check-worktree-paths.sh`, `check-siblings.sh`, `check-duplication.py`, `scripts/lib/**` | `docs/project-spec.md`, `README.md` | operational procedures, which manifests are generated vs committed, what the preflights check and when they stay silent |
| `scripts/run-sandbox-local.sh` | `docs/project-spec.md`, `README.md` | how the sandbox image is built and staged locally, what the local supervisor run does and does not reproduce |
| `scripts/dev-stack.sh` | `docs/local-dev-vm.md`, `docs/code-execution-security.md` | the generated-and-persisted `SANDBOX_TOKEN_SIGNING_KEY` / `INTERNAL_API_SECRET`, where they are stored, and what an unauthenticated local caller can reach once they are set |
| `scripts/lib/env.sh`, `terraform/*.tfbackend` | `docs/environments.md` | the environment table, `DEPLOY_ENV` selection rules, shared-project resource suffixes |
| `k8s/deployments/oauth2-proxy.yaml`, `k8s/deployments/keycloak.yaml`, `scripts/deploy.sh` | `docs/environments.md` | the cookie surface of the shared host: that no component sets an explicit cookie `Domain`, the `--cookie-*` flags, the deliberate `proxy_cookie_flags ~ secure samesite=none` rewrite in the gateway block, `KEYCLOAK_HOST` |
| `scripts/monitor/**` | `docs/project-spec.md` | monitored views, alert ignore patterns |
| `keycloak/**` **except static branding assets under `keycloak/themes/**`** (`.css`, `.properties`, images, fonts), `scripts/keycloak-*.sh` | `docs/keycloak-apple-signin.md`, `docs/mcp-oauth-onboarding.md` | client setup, allowlist, backup and restore paths; the onboarding runbook's commands and IdP list |
| `sandbox/**` (**including** `sandbox/schema/**` and `sandbox/stubs/**`), `k8s/deployments/sandbox.yaml`, `k8s/network-policies/sandbox-policy.yaml` | `docs/code-execution-security.md` | isolation boundary (uid, rootfs, seccomp, runtime class), egress/ingress allow-lists, resource and timeout caps, the sandbox token's claims and validation rules, the three MCP-exclusion layers, what the shipped schema docs and stubs disclose |
| `sandbox/**` (**including** the generated trees), `k8s/deployments/sandbox.yaml`, `k8s/network-policies/sandbox-policy.yaml` | `docs/project-spec.md` | services table, isolation-boundary summary, sandbox network policy, what the sandbox exposes |
| `scripts/gen-sandbox-docs.py`, `scripts/test-sandbox-docs.py` | `docs/code-execution-security.md` | the schema-doc contract the generator *owns*: that neither generated tree is empty, that no `PLACEHOLDER` file survives the build gate, that each view's file carries its description, columns and worked-example SQL, and that the stubs cover exactly the SDK's exported surface |
| `scripts/gen-sandbox-docs.py`, `scripts/test-sandbox-docs.py` | `docs/project-spec.md` | the build-step spec: what the generator emits (one `sandbox/schema/*.md` per view in `configs/datasets.yaml` plus an index, `sandbox/stubs/*.pyi` read out of the staged SDK), what the test asserts (every view, column, enumerable column and worked example reaches a file; every documented column carries a well-formed BigQuery type; the stubs cover exactly the SDK's exported surface), the `--sdk-src` resolution order, the `PLACEHOLDER` build gate, and the shared 0/1/2 exit-code convention |
| `scripts/test-network-policies.py` | `docs/code-execution-security.md` | the controls this harness is cited as enforcing — the sandbox's ingress/egress allow-lists, the three MCP-exclusion layers, the `SANDBOX_ENABLED` pairing, which pod-spec fields are still treated as sandbox tells |
| `scripts/test-network-policies.py` | `docs/project-spec.md` | the harness's own enumerated spec: the checks it runs, its discovery tells and both locks, the workload kinds it sweeps, and its three-way answer on the live-sandbox probe |
| `scripts/gen-doc-blocks.py` | `docs/code-execution-security.md`, `docs/project-spec.md` | which blocks are generated, what each derives from, and the build gate that runs it. The blocks themselves need no row: `--check` fails the build when they are stale |

The four rows just added — `gen-sandbox-docs.py`/`test-sandbox-docs.py` and
`test-network-policies.py`, two docs each — close a gap that ran the other way: the *generated* trees
(`sandbox/schema/**`, `sandbox/stubs/**`) were mapped to `docs/code-execution-security.md`
while the *generator* was not, so a change to `gen-sandbox-docs.py` could falsify every
claim that doc makes about the shipped schema docs with no path-based warning at all
(`genetics-results-suite-8vn`). The `gen-sandbox-docs.py`/`test-sandbox-docs.py` pair and
`test-network-policies.py` each get two rows for the same reason `sandbox/**` does —
satisfying one of the two docs does not satisfy the other, and folding them into one
alternation would let a change that touches the easier doc mask an unexamined claim in the
harder one.

The **except** clause in the `keycloak/**` row is not tidiness, and its narrowness is the point. A rule whose
path pattern is broader than the doc concern it names fires where it can never apply, and
that trains people to ignore it (`genetics-results-suite-dqa`) — but the exclusion has to
be justified against what the doc actually says, not against how the files were produced.
A stylesheet or message bundle cannot change how Keycloak authenticates anybody, so it is
exempt; a FreeMarker override or a script under the same directory can, so it is not. The
generated `sandbox/schema/**` and `sandbox/stubs/**` are **not** exempt for the same
reason in reverse: `docs/code-execution-security.md` reasons about their shipped content —
the stubs are where `INTERNAL_API_SECRET` enters its secrets-in-image analysis, and the
`PLACEHOLDER` build gate is stated over both staged trees — so a regeneration can falsify
it. When adding a row, check the path pattern is no broader than the doc concern it names,
and mirror any exclusion into `scripts/check-doc-drift.sh`'s optional 4th `check`
argument — the table and the script are supposed to match.

A doc is stale the moment it *enumerates* something the code no longer matches.
Counts and lists rot silently — view lists, endpoint tables, env-var tables,
service inventories — so re-derive them from the code rather than trusting them.


# Comments and documentation

**The code is the source of truth for HOW; a comment or a doc is for WHY.** Both were allowed
to grow until one document was longer than the code it described, and the postmortem's finding
was that the prose rotted while the code stayed correct. So:

1. **Say why, briefly.** A comment earns its place by explaining something the code cannot: a
   constraint, a measurement, a rejected alternative, a hazard at the site. Restating what the
   line does is noise, and so is a paragraph where a sentence works.
2. **No history in comments.** "This used to be X", "an earlier version did Y", "fixed in Z" —
   delete it. Keep the *consequence* if it still binds ("the hard limit is lowered too, or the
   child can raise the soft one back"), drop the narrative. Git holds the history.
3. **No tracker ids in code, and none in a doc except where the doc is about the tracker.** A
   bead id in a comment tells a future reader nothing they can act on and rots the moment the
   bead closes.
4. **Anything that ENUMERATES is generated, with a build gate.** Counts, lists, tables of
   limits, service inventories, env-var tables, directory trees. `scripts/gen-doc-blocks.py`
   owns the marked blocks in `docs/*.md` and `--check` fails the build when they are stale;
   `scripts/gen-sandbox-docs.py` owns the shipped schema docs and stubs the same way. Adding a
   hand-maintained list is adding something nothing will notice going false.
5. **A doc points at the code rather than restating it.** The one exception is a contract that
   genuinely cannot be shared — the sandbox wire shape is defined in prose because the two ends
   cannot import one module — and that exception is stated where it applies.
6. **Delete rather than annotate.** A section that has become a diary of how the code got here
   is not improved by a note saying so.

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


# Findings that are not work

This repo's review cycles are adversarial and productive, and that is exactly why the
backlog grows faster than it closes: every round surfaces real observations, and filing each
one as a task turns an epic into an open-ended loop. Measured, 2026-08-19: 75 open beads, of
which at least seven say **in their own description** that nothing triggers them today —
`LATENT`, `not reachable`, `explicitly not a defect`, `nothing is broken today`. Those are
hazard notes that were filed as work, and each one will cost a future session a full
implement-review-fix cycle to close as "no change needed".

Before filing, apply the filing gate in the global CLAUDE.md: **blocks committed work → file;
real but unblocking → file and `bd defer` it; latent or nit → not a bead at all.**

Repo-specific consequences:

- **Enumeration and doc-wording findings append; they never get their own bead.** This is
  the single largest class here — the doc-ownership section above exists because these rot
  silently, and a thorough validator turns up one or two per cycle. `genetics-results-suite-e8e`
  is the standing batch bead for that; add to its `notes`.
- **A latent hazard belongs at the site, not in the tracker.** If the code has a line where
  the hazard lives, a comment there reaches the next person who touches it. A bead does not.
- **"Found but deliberately not fixed" is a complete outcome.** It does not need a bead to
  be a legitimate stopping point, provided the finding is written down somewhere it will be
  read. Say so in the close reason.


# Software Development Behavior Guidelines

1. Don't guess and do things which you are not certain about. Ask the user instead.
2. Don't add or modify code unrelated to the specific request and context at the moment.
3. In interactive mode: only use git when asked, stage changes and propose a commit message for user review. In autonomous/orchestrator mode (e.g. ralph wiggum): commit after each completed task with a descriptive message.
4. **Always** prior to finishing a task and considering it completed, revise all the changes and update Project Specification files.
5. When trying to fix any bug or error **ALWAYS** think carefully and analyze in detail what happened and WHY? Explain and confirm with user.


# Code Conventions

1. The root layout is enumerated in `docs/project-spec.md` ("Project structure") and
   **nowhere else** — a second copy here is a second list to rot, which is the exact
   failure the doc-ownership section above warns about, and this line was that failure:
   it named four directories for a tree that has had `configs/`, `keycloak/` and
   `sandbox/` for some time. Adding a root directory means adding it to that tree in the
   same commit.
2. Code should be self-descriptive
   - Only add comments for tricky or complex parts of the code (explaining WHY something is done)
   - NO redundant and trivial comments that simply restate what the code does
3. Kubernetes manifests use standard YAML formatting
4. Terraform follows HCL best practices with variables defined in `variables.tf`
5. Shell scripts should be POSIX-compatible where possible


# Project-specific conventions

1. All of a deployment's services run in one GKE cluster, in the `genetics` namespace — one cluster per deployment, three today (`docs/environments.md`)
2. Docker images are stored in Google Artifact Registry (registry path derived from `project_id` in terraform, overridable via `REGISTRY` env var)
3. Configuration uses Kubernetes secrets and environment variables — never hardcode credentials
4. Terraform state is stored in a GCS backend bucket
5. Infrastructure changes go through `terraform plan` review before applying
6. Kubernetes deployments use `scripts/deploy.sh` for full deploys and `scripts/rollout.sh` for single-service updates


====

**Don't forget any of the 'QUALITY CODING RULES' above!!!**
