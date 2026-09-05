#!/usr/bin/env python3
"""Regenerate the enumerated blocks of docs/*.md from the code.

Everything this script owns is a list or a number the code already computes: the sandbox's
per-execution bounds and pod spec, the egress and ingress allow-lists, the image environment,
the reserved `error.type` names, the workload table and the repository layout. Those are the
parts of a document that rot silently — the prose around them stays plausible while the table
stops matching the thing it describes — so they are derived rather than transcribed.

A block is delimited in the doc by `<!-- BEGIN GENERATED: name -->` / `<!-- END GENERATED:
name -->`; a block with no marker, or a marker with no block, is an error, so the two cannot
drift apart silently either.

    scripts/gen-doc-blocks.py            rewrite the blocks in place
    scripts/gen-doc-blocks.py --check    exit 1 if any block is stale (the build gate)

Exit 0 = up to date (or written), 1 = stale under --check, 2 = a source could not be read.
"""

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.path.insert(0, os.path.join(ROOT, "sandbox"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "lib"))

try:
    import yaml
except ImportError:
    print("HARNESS: PyYAML is required", file=sys.stderr)
    raise SystemExit(2)

try:
    import siblings
except Exception as exc:  # pragma: no cover
    print(f"HARNESS: cannot import scripts/lib/siblings.py: {exc}", file=sys.stderr)
    raise SystemExit(2)

try:
    import supervisor as sup
except Exception as exc:  # pragma: no cover
    print(f"HARNESS: cannot import sandbox/supervisor.py: {exc}", file=sys.stderr)
    raise SystemExit(2)


def mib(n):
    return f"{n // (1024 * 1024)} MiB" if n % (1024 * 1024) == 0 else f"{n} B"


def kib(n):
    return f"{n // 1024} KiB" if n % 1024 == 0 else f"{n} B"


def yv(value):
    """A YAML-ish rendering, so `true` in the manifest does not read back as `True`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def load_yaml(rel):
    with open(os.path.join(ROOT, rel)) as fh:
        return [d for d in yaml.safe_load_all(fh) if d]


def sandbox_container():
    for doc in load_yaml("k8s/deployments/sandbox.yaml"):
        if doc.get("kind") != "Deployment":
            continue
        spec = doc["spec"]["template"]["spec"]
        for c in spec["containers"]:
            if c["name"] == "sandbox":
                return doc, spec, c
    raise SystemExit(2)


# ---------------------------------------------------------------------------------------
# blocks
# ---------------------------------------------------------------------------------------


def block_limits():
    rows = [
        ("wall clock", f"{sup.DEFAULT_TIMEOUT_S}s default, {sup.MAX_TIMEOUT_S}s ceiling",
         "`_watchdog`, per execution; over the ceiling is rejected, never clamped"),
        ("kill grace", f"{sup.KILL_GRACE_S:g}s",
         "SIGTERM to the child's process group, then SIGKILL"),
        ("memory (child)", mib(sup.CHILD_RLIMIT_AS_BYTES),
         f"`RLIMIT_AS`, soft and hard, applied by the child to itself; "
         f"{mib(sup.SUPERVISOR_MEMORY_HEADROOM_BYTES)} of the pod's "
         f"{mib(sup.POD_MEMORY_LIMIT_BYTES)} is left as supervisor headroom"),
        ("pid budget", str(sup.PID_BUDGET),
         "supervisor-side watch on the child's process group, not `RLIMIT_NPROC`"),
        ("pipe cap", mib(sup.PIPE_CAP_BYTES),
         "the reader stops and kills the child's group"),
        ("returned output", f"{kib(sup.RETURN_HEAD_BYTES)} head + {kib(sup.RETURN_TAIL_BYTES)} tail",
         "elision marker between them, additional to the budget"),
        ("artifacts, per execution",
         f"{mib(sup.ARTIFACT_QUOTA_BYTES)} / {sup.ARTIFACT_ENTRY_BUDGET} entries",
         "polled; over it the execution is killed and `_retain` trims back to the quota"),
        ("execution directory",
         f"{mib(sup.EXECUTION_TOTAL_QUOTA_BYTES)} / {sup.EXECUTION_ENTRY_BUDGET} entries",
         "the whole of `/scratch/<id>`, artifacts included"),
        ("retained artifacts", mib(sup.RETAINED_ARTIFACTS_CEILING_BYTES),
         "oldest-first eviction across completed executions"),
        ("retained supervisor state", mib(sup.RETAINED_STATE_CEILING_BYTES),
         "digest maps and retention rows; the second ceiling eviction fires on"),
        ("/scratch aggregate", mib(sup.SCRATCH_AGGREGATE_CEILING_BYTES),
         f"backstop {mib(sup.SCRATCH_SUPERVISOR_RESERVE_BYTES)} under the emptyDir "
         f"`sizeLimit` of {mib(sup.SCRATCH_SIZE_LIMIT_BYTES)}"),
        ("retention", f"{sup.RETENTION_S}s",
         f"a floor, not an instant: the reaper polls every {sup.REAPER_POLL_S:g}s, and the "
         f"ceilings above can evict earlier"),
        ("artifact read", kib(sup.ARTIFACT_READ_MAX_BYTES),
         "plaintext, not the file: a sealed file is allowed "
         f"{sup.ARTIFACT_ENVELOPE_BYTES} bytes more on disk"),
        ("audit stream", f"{kib(sup.AUDIT_LINE_MAX_BYTES)}/record, {mib(sup.AUDIT_STREAM_MAX_BYTES)}"
         f"/execution, {sup.AUDIT_RATE_PER_S:g} records/s (burst {sup.AUDIT_RATE_BURST})",
         "every one applied on the read end, per execution"),
        ("queue", f"depth {sup.QUEUE_DEPTH}, wait {sup.MAX_QUEUED_WAIT_S:g}s",
         f"depth counts requests *waiting*; over either, `429` with "
         f"`Retry-After: {sup.RETRY_AFTER_S}`"),
        ("request body", mib(sup.MAX_BODY_BYTES),
         f"raw bytes on the wire; `code` separately at {kib(sup.MAX_CODE_BYTES)} of UTF-8"),
        ("request head", f"{kib(sup.MAX_HEADER_BYTES)}",
         "request line and headers as one block"),
        ("read deadlines",
         f"head {sup.HEAD_READ_TIMEOUT_S:g}s, body {sup.BODY_READ_TIMEOUT_S:g}s, "
         f"idle {sup.IDLE_READ_TIMEOUT_S:g}s",
         "one deadline for the whole head; the idle bound closes silently"),
        ("response body", mib(sup.MAX_RESPONSE_BYTES),
         "a backstop; every component is separately capped"),
        ("SIGTERM drain", f"{sup.DRAIN_DEADLINE_S:g}s",
         "between max wall clock + grace and the manifest's "
         "`terminationGracePeriodSeconds`"),
    ]
    out = ["| bound | value | enforced by |", "|---|---|---|"]
    out += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    return "\n".join(out)


def block_errors():
    reserved = sorted(sup.RESERVED_ERROR_TYPES)
    never = sorted(sup.RESERVED_ERROR_TYPES - set(sup._LIMIT_MESSAGES) - {sup.ERR_TIMEOUT})
    lines = [
        "Reserved `error.type` names — the supervisor emits no others for these conditions and "
        "a client may branch on them: " + ", ".join(f"`{n}`" for n in reserved) + ".",
        "",
        "The other half of `error.type`'s range is the child's own exception class name, which "
        "is why the field is an open string. `_sanitise_error_type` refuses a child-supplied "
        f"value over {sup.ERROR_TYPE_MAX_BYTES} bytes, one that is not a dotted identifier, and "
        "any reserved name — so a script cannot forge one.",
        "",
        "Reserved but never emitted: " + ", ".join(f"`{n}`" for n in never) +
        ". The memory ceiling is `RLIMIT_AS`, applied by the child to itself, so what comes back "
        "is the child's own `MemoryError` on the open half of the range. The name stays reserved "
        "only so a script cannot forge it.",
    ]
    return "\n".join(lines)


def block_pod():
    doc, spec, c = sandbox_container()
    sc = c.get("securityContext", {})
    psc = spec.get("securityContext", {})
    res = c.get("resources", {})
    caps = sc.get("capabilities", {})
    vols = [v["name"] + " (" + ("emptyDir, sizeLimit " +
            str(v["emptyDir"].get("sizeLimit", "unset")) if "emptyDir" in v else
            ",".join(sorted(v.keys() - {"name"}))) + ")" for v in spec.get("volumes", [])]
    rows = [
        ("runtimeClassName", f"`{yv(spec.get('runtimeClassName', 'unset'))}`"),
        ("replicas / strategy",
         f"{doc['spec'].get('replicas')} / `{doc['spec'].get('strategy', {}).get('type')}`"),
        ("serviceAccountName", f"`{spec.get('serviceAccountName', 'default')}`"),
        ("automountServiceAccountToken", f"`{yv(spec.get('automountServiceAccountToken'))}`"),
        ("enableServiceLinks", f"`{yv(spec.get('enableServiceLinks'))}`"),
        ("dnsPolicy", f"`{yv(spec.get('dnsPolicy'))}`"),
        ("hostAliases", ", ".join(sorted(
            h["ip"] + " → " + " ".join(h["hostnames"]) for h in spec.get("hostAliases", []))) or "none"),
        ("uid / gid",
         f"runAsUser {sc.get('runAsUser', psc.get('runAsUser'))}, "
         f"runAsGroup {sc.get('runAsGroup', psc.get('runAsGroup'))}, "
         f"fsGroup {psc.get('fsGroup')}, runAsNonRoot "
         f"`{yv(sc.get('runAsNonRoot', psc.get('runAsNonRoot')))}`"),
        ("readOnlyRootFilesystem", f"`{yv(sc.get('readOnlyRootFilesystem'))}`"),
        ("allowPrivilegeEscalation", f"`{yv(sc.get('allowPrivilegeEscalation'))}`"),
        ("capabilities", "drop " + ",".join(caps.get("drop", [])) +
         (", add " + ",".join(caps.get("add", [])) if caps.get("add") else ", no add")),
        ("seccompProfile", f"`{sc.get('seccompProfile', psc.get('seccompProfile', {})).get('type')}`"),
        ("resources",
         "requests " + ", ".join(f"{k} {v}" for k, v in sorted(res.get("requests", {}).items())) +
         "; limits " + ", ".join(f"{k} {v}" for k, v in sorted(res.get("limits", {}).items()))),
        ("volumes", ", ".join(vols) or "none"),
        ("probes", ", ".join(sorted(k for k in c if k.endswith("Probe"))) or "none"),
        ("command / args", f"`{c.get('command', c.get('args'))}`"),
        ("terminationGracePeriodSeconds", str(spec.get("terminationGracePeriodSeconds"))),
        ("tolerations", ", ".join(sorted(
            f"{t.get('key')}={t.get('value')}:{t.get('effect')}"
            for t in spec.get("tolerations", []))) or "none"),
        ("nodeSelector", ", ".join(f"{k}={v}" for k, v in
                                   sorted(spec.get("nodeSelector", {}).items())) or "none"),
    ]
    out = ["| field | value |", "|---|---|"]
    out += [f"| {a} | {b} |" for a, b in rows]
    return "\n".join(out)


def _peers(rule, key):
    out = []
    for peer in rule.get(key, []) or []:
        sel = peer.get("podSelector", {}).get("matchLabels")
        if sel:
            out.append(", ".join(f"`{k}: {v}`" for k, v in sorted(sel.items())))
        elif "ipBlock" in peer:
            out.append("ipBlock " + peer["ipBlock"].get("cidr", "?"))
        elif "namespaceSelector" in peer:
            out.append("namespaceSelector " + str(peer["namespaceSelector"]))
    return out


def _ports(rule):
    return ", ".join(f"{p.get('port')}/{p.get('protocol', 'TCP')}"
                     for p in rule.get("ports", []) or []) or "any"


def block_egress():
    lines = []
    for doc in load_yaml("k8s/network-policies/sandbox-policy.yaml"):
        if doc.get("kind") != "NetworkPolicy":
            continue
        spec = doc["spec"]
        sel = spec.get("podSelector", {}).get("matchLabels", {})
        lines.append(f"**`{doc['metadata']['name']}`** — selects "
                     + ", ".join(f"`{k}: {v}`" for k, v in sorted(sel.items()))
                     + f", policyTypes {', '.join(spec.get('policyTypes', []))}")
        lines.append("")
        for direction, key in (("egress", "to"), ("ingress", "from")):
            for rule in spec.get(direction, []) or []:
                peers = _peers(rule, key) or ["**anywhere** (no selector — this rule is open)"]
                for p in peers:
                    lines.append(f"- {direction}: {p} on {_ports(rule)}")
        lines.append("")

    reverse = []
    for doc in load_yaml("k8s/network-policies/policies.yaml"):
        if doc.get("kind") != "NetworkPolicy":
            continue
        for rule in doc["spec"].get("ingress", []) or []:
            for peer in rule.get("from", []) or []:
                if peer.get("podSelector", {}).get("matchLabels", {}).get("app") == "sandbox":
                    reverse.append(f"- `{doc['metadata']['name']}` admits `app: sandbox` "
                                   f"on {_ports(rule)}")
    lines.append("The receiving end has to admit it too — a sandbox egress allow is necessary "
                 "and not sufficient against the namespace's `default-deny-ingress`:")
    lines.append("")
    lines += sorted(set(reverse)) or ["- (nothing in `policies.yaml` admits `app: sandbox`)"]
    return "\n".join(lines)


def block_image():
    env = {}
    with open(os.path.join(ROOT, "sandbox", "Dockerfile")) as fh:
        text = fh.read()
    final = text.rsplit("\nFROM ", 1)[-1]
    for match in re.finditer(r"^ENV (.+?)(?=^\S|\Z)", final, re.M | re.S):
        for pair in re.findall(r"([A-Z_][A-Z0-9_]*)=(\S+)", match.group(1)):
            env[pair[0]] = pair[1]
    import prune_venv

    lines = ["The final stage's environment, all of it:", ""]
    lines += [f"- `{k}={v}`" for k, v in sorted(env.items())]
    lines += [
        "",
        "`TMPDIR`, `HOME`, `MPLCONFIGDIR`, `XDG_CACHE_HOME` and `PYTHONPYCACHEPREFIX` are "
        "deliberately absent: they are per-execution and point inside `/scratch/<id>`, and a "
        "fixed path here would be exactly the cross-execution shared directory the redirect "
        "exists to prevent. The redirect keeps the supervisor's own path out of the "
        "runtime-supplied `/tmp` and `/dev/shm`; it does not remove those, and what keeps them "
        "from carrying bytes between tenants is the wipe before every fork (section 2).",
        "",
        "`prune_venv.py` reduces the installed distribution to the SDK's import closure, and "
        "`build-checks.py` asserts the surviving set is exactly:",
        "",
    ]
    lines += [f"- `genetics_mcp_server/{p}`" for p in sorted(prune_venv.SDK_ALLOWLIST)]
    lines += [
        "",
        "plus `genetics.py`, the `import genetics` alias. Everything else the wheel installed "
        "is deleted — unimportable there for want of fastapi, but a prompt-injected script "
        "reads source, it does not import it. `pip`, `setuptools` and the venv's `bin/` go "
        "with it: " + ", ".join(f"`{d}`" for d in sorted(prune_venv.PACKAGING_DIRS)) + ".",
    ]
    return "\n".join(lines)


def block_services():
    """The workload table, from the manifests themselves."""
    rows = []
    for d in sorted(os.listdir(os.path.join(ROOT, "k8s", "deployments"))) + \
            [os.path.join("..", "cronjobs", f)
             for f in sorted(os.listdir(os.path.join(ROOT, "k8s", "cronjobs")))]:
        if not d.endswith((".yaml", ".yml")):
            continue
        for doc in load_yaml(os.path.join("k8s", "deployments", d)):
            kind = doc.get("kind")
            if kind not in ("Deployment", "CronJob", "StatefulSet", "DaemonSet"):
                continue
            if kind == "CronJob":
                pod = doc["spec"]["jobTemplate"]["spec"]["template"]["spec"]
            else:
                pod = doc["spec"]["template"]["spec"]
            name = doc["metadata"]["name"]
            psc = pod.get("securityContext", {})
            for c in pod.get("containers", []):
                sc = c.get("securityContext", {})
                ports = ", ".join(str(p.get("containerPort"))
                                  for p in c.get("ports", []) or []) or "—"
                uid = sc.get("runAsUser", psc.get("runAsUser"))
                hard = []
                if sc.get("runAsNonRoot", psc.get("runAsNonRoot")):
                    hard.append("nonroot")
                if sc.get("readOnlyRootFilesystem"):
                    hard.append("ro-rootfs")
                if sc.get("allowPrivilegeEscalation") is False:
                    hard.append("no-priv-esc")
                if "ALL" in (sc.get("capabilities", {}).get("drop") or []):
                    hard.append("drop-ALL")
                if (sc.get("seccompProfile", psc.get("seccompProfile", {})) or {}).get("type"):
                    hard.append("seccomp")
                label = name if c["name"] == name else f"{name} / {c['name']}"
                rows.append((label, kind, ports,
                             str(uid) if uid is not None else "root (unset)",
                             ", ".join(hard) or "—"))
    out = ["| workload | kind | container port | uid | container hardening |", "|---|---|---|---|---|"]
    out += [f"| `{a}` | {b} | {c} | {d} | {e} |" for a, b, c, d, e in rows]
    out += ["",
            "`uid` is `runAsUser` on the container, falling back to the pod; "
            "`root (unset)` means neither sets one. The hardening column is the container's own "
            "`securityContext` — a pod-level `runAsNonRoot` is counted, the rest are not, "
            "because they have no pod-level form."]
    return "\n".join(out)


# Every path the repository layout block lists needs a line here, and every line needs a path:
# a directory added without one fails the build gate rather than going undocumented, and a
# description left behind by a deletion fails it too. That is the whole reason the block is
# generated — the old hand-maintained tree named directories the repo had not had for months.
LAYOUT = {
    "benchmarks": "inputs for the paired A/B replay benchmark; the harness itself lives in "
                  "genetics-mcp-server",
    "configs": "canonical dataset and resource definitions consumed by results-api and "
               "db-api, and the registry of the suite's declared duplicates",
    "configs/datasets.yaml": "the single source of truth for datasets, resources and views",
    "configs/datasets-schema-example.yaml": "schema reference with example datasets",
    "configs/twins.yaml": "the duplicates the suite keeps on purpose, netted out of "
                          "check-duplication.py's counts",
    "configs/rag": "RAG experiment configs (not k8s manifests)",
    "configs/*_pheno.json": "per-phenotype metadata for external GWAS",
    "docs": "everything below, and nothing else",
    "docs/project-spec.md": "this file",
    "docs/adding-datasets.md": "how to add a dataset across the repos and profiles",
    "docs/datasets-yaml-schema.md": "the schema of configs/datasets.yaml",
    "docs/chat-tool-reference.md": "verbatim transcription of what the LLM receives: tool "
                                   "names, descriptions and schemas, the profiles, the system "
                                   "prompt, and the chat surface versus /mcp",
    "docs/code-execution-security.md": "threat model and security design for the sandbox",
    "docs/environments.md": "the three deployments, DEPLOY_ENV, and the staging runbook",
    "docs/local-dev-vm.md": "running the whole suite from source on a VM, no docker or k8s",
    "docs/bigquery-dev-dataset.md": "the BigQuery rehearsal dataset",
    "docs/keycloak-apple-signin.md": "Keycloak broker setup, MCP OAuth clients, backup/restore",
    "docs/mcp-oauth-onboarding.md": "runbook for onboarding an external app to /mcp",
    "docs/genegenie-migration.md": "record of the legacy-hostname redirect",
    "docs/nginx-setup.md": "notes for the legacy VM nginx setup",
    "docs/postmortem-code-execution-epic.md": "why the sandbox epic took as long as it did",
    "docs/duplication-baseline.json": "the duplication ratchet's last-written snapshot, "
                                      "read by check-duplication.py --check",
    "k8s": "manifests, applied by deploy.sh",
    "k8s/namespace.yaml": "the `genetics` namespace",
    "k8s/deployments": "one file per workload, CronJobs included",
    "k8s/ingress": "backend/frontend configs only — the Ingress and ManagedCertificate are "
                   "generated by deploy.sh from the terraform `domains` list",
    "k8s/configs": "bearer-auth allow-list; the oauth2-proxy allow-list ConfigMap is generated "
                   "by deploy.sh and has no manifest",
    "k8s/cronjobs": "applied only when Keycloak is enabled",
    "k8s/disruption-budgets": "PodDisruptionBudgets",
    "k8s/network-policies": "network isolation rules, including the sandbox's",
    "k8s/volumes": "PersistentVolumeClaims",
    "keycloak": "Keycloak image build context, realm/client/IdP templates and the login theme",
    "sandbox": "sandbox image build context for model-authored Python; the SDK is "
               "pip-installed from genetics-mcp-server at build time",
    "scripts": "build, deploy and verification scripts",
    "scripts/lib": "shared library: DEPLOY_ENV resolution, the kubectl context guard, sibling-repo resolution",
    "scripts/monitor": "the monitoring CronJob's Python package",
    "scripts/supervisor_tests": "the check groups scripts/test-supervisor.py runs",
    "scripts/deploy.sh": "full deploy: terraform apply, then every manifest",
    "scripts/rollout.sh": "single-service image update",
    "scripts/build.sh": "build and push one service's image",
    "scripts/build-all.sh": "build and push every image",
    "scripts/create-secrets.sh": "create the k8s Secrets from environment variables",
    "scripts/install-git-hooks.sh": "wire core.hooksPath; run once per clone",
    "scripts/sync-datasets.sh": "copy datasets.yaml to the sibling repos for local dev",
    "scripts/dev-stack.sh": "start/stop the local dev servers from one tree "
                            "(docs/local-dev-vm.md)",
    "scripts/run-sandbox-local.sh": "build and run the sandbox image in plain Docker",
    "scripts/bq-dev-dataset.sh": "stand up, verify or tear down the BigQuery rehearsal dataset "
                                 "(docs/bigquery-dev-dataset.md)",
    "scripts/chat_usage_stats.sh": "chat usage counts from the BigQuery chat-log sink",
    "scripts/keycloak-register-client.sh": "register or update an MCP OAuth client in the live "
                                           "realm",
    "scripts/keycloak-register-brainzzz.sh": "the brainzzz client specifically",
    "scripts/keycloak-bind-allowlist.sh": "bind the email allow-list authenticator and realm "
                                          "attributes",
    "scripts/keycloak-get-token.sh": "browser auth-code+PKCE flow; prints an access token",
    "scripts/gen-sandbox-docs.py": "generate sandbox/schema/*.md and sandbox/stubs/*.pyi",
    "scripts/gen-doc-blocks.py": "generate the marked blocks in docs/*.md; `--check` is the "
                                 "build gate",
    "scripts/check-doc-drift.sh": "warn when a commit changes code the docs describe",
    "scripts/check-duplication.py": "ratchet on the suite's UNDECLARED duplication count "
                                    "(and on the declared one), measured from the trees "
                                    "themselves",
    "scripts/check-siblings.sh": "run each sibling repo's own discovered test lane from "
                                 "one place",
    "scripts/check-worktree-paths.sh": "warn when a tool would resolve a path into the main "
                                       "checkout",
    "scripts/test-manifest-render.py": "offline: render every manifest deploy.sh renders, and "
                                       "hold each envsubst whitelist to the files it governs",
    "scripts/test-network-policies.py": "the namespace's policies as a whole: offline by "
                                        "default, plus an opt-in diff against a live cluster",
    "scripts/test-sandbox-docs.py": "offline: the generated schema docs and SDK stubs",
    "scripts/test-supervisor.py": "offline: the sandbox supervisor, in process or against a "
                                  "container",
    "scripts/test-e2e-local.py": "end-to-end run_analysis against the live local stack",
    "terraform": "infrastructure",
    "terraform/main.tf": "provider config and the GCS backend",
    "terraform/gke.tf": "the GKE cluster and its node pools, the sandbox pool included",
    "terraform/network.tf": "VPC, subnets, static IP, DNS",
    "terraform/registry.tf": "Artifact Registry",
    "terraform/backups.tf": "disk snapshot schedule and the Keycloak backup bucket",
    "terraform/logging.tf": "Cloud Logging to BigQuery sinks, gated by `enable_log_sinks`",
    "terraform/iam.tf": "service accounts and Workload Identity",
    "terraform/kubernetes.tf": "namespace and Kubernetes service accounts",
    "terraform/variables.tf": "input variables",
    "terraform/outputs.tf": "output values",
    "terraform/*.tfbackend": "per-environment GCS state backends, selected by DEPLOY_ENV",
    "terraform/terraform.tfvars.*": "per-environment values; **not committed** except the "
                                    "`.example`. A bare `terraform.tfvars` is the legacy "
                                    "single-deployment form and must not coexist with these",
    "CLAUDE.md": "the coding and documentation-ownership rules for this repo",
    "README.md": "deployment and operations guide",
    "LICENSE": "",
}

# expanded one level in the block below; everything else is listed as a single entry
LAYOUT_EXPAND = ("configs", "docs", "k8s", "scripts", "terraform")


def _tracked():
    out = subprocess.run(["git", "-C", ROOT, "ls-files"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(2)
    return out.stdout.split()


def block_structure():
    files = [p for p in _tracked() if not p.startswith(".")]
    top = sorted({p.split("/", 1)[0] for p in files})
    listed, used = [], set()

    def describe(path):
        if path in LAYOUT:
            used.add(path)
            return LAYOUT[path]
        for key in LAYOUT:
            if "*" in key and re.fullmatch(key.replace("*", "[^/]*"), path):
                used.add(key)
                return LAYOUT[key]
        raise SystemExit(f"docs/project-spec.md: {path} has no entry in gen-doc-blocks.py's "
                         f"LAYOUT — add one, or stop tracking the path")

    for entry in top:
        is_dir = any(p.startswith(entry + "/") for p in files)
        listed.append((entry + ("/" if is_dir else ""), describe(entry)))
        if entry in LAYOUT_EXPAND:
            children = sorted({p.split("/")[1] for p in files if p.startswith(entry + "/")})
            for child in children:
                path = f"{entry}/{child}"
                sub_is_dir = any(p.startswith(path + "/") for p in files)
                listed.append(("  " + child + ("/" if sub_is_dir else ""), describe(path)))

    orphans = sorted(set(LAYOUT) - used)
    if orphans:
        raise SystemExit(f"docs/project-spec.md: gen-doc-blocks.py's LAYOUT describes paths "
                         f"that no longer exist: {orphans}")

    width = max(len(name) for name, _ in listed)
    out = ["```"]
    for name, desc in listed:
        out.append(f"{name.ljust(width)}  {desc}" if desc else name)
    out.append("```")
    return "\n".join(out)


def block_suite_repos():
    return "\n".join(f"- `{r}`" for r in siblings.SUITE_REPOS)


SECURITY = "docs/code-execution-security.md"
SPEC = "docs/project-spec.md"

BLOCKS = {
    "limits": (SECURITY, block_limits),
    "error-types": (SECURITY, block_errors),
    "pod": (SECURITY, block_pod),
    "network": (SECURITY, block_egress),
    "image": (SECURITY, block_image),
    "services": (SPEC, block_services),
    "structure": (SPEC, block_structure),
    "suite-repos": (SPEC, block_suite_repos),
}

MARKER = re.compile(
    r"(<!-- BEGIN GENERATED: (?P<name>[a-z-]+) -->\n)(?P<body>.*?)(<!-- END GENERATED: (?P=name) -->)",
    re.S,
)


def render(doc, text):
    seen = set()

    def repl(m):
        name = m.group("name")
        seen.add(name)
        if name not in BLOCKS or BLOCKS[name][0] != doc:
            raise SystemExit(f"{doc}: unknown generated block {name!r}")
        return m.group(1) + "\n" + BLOCKS[name][1]().rstrip() + "\n\n" + m.group(4)

    out = MARKER.sub(repl, text)
    missing = {n for n, (d, _) in BLOCKS.items() if d == doc} - seen
    if missing:
        raise SystemExit(f"{doc}: no marker for generated block(s) {sorted(missing)}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any generated block is stale; write nothing")
    args = ap.parse_args()

    stale = []
    for doc in sorted({d for d, _ in BLOCKS.values()}):
        path = os.path.join(ROOT, doc)
        with open(path) as fh:
            current = fh.read()
        fresh = render(doc, current)
        n = sum(1 for d, _ in BLOCKS.values() if d == doc)
        if current == fresh:
            print(f"{doc}: {n} generated block(s) up to date")
            continue
        if args.check:
            stale.append(doc)
            continue
        with open(path, "w") as fh:
            fh.write(fresh)
        print(f"{doc}: {n} generated block(s) rewritten")
    if stale:
        print("STALE, run scripts/gen-doc-blocks.py: " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
