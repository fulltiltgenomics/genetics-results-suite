#!/usr/bin/env python3
"""Regenerate the enumerated blocks of docs/*.md from the code.

Everything this script owns is a list or a number the code already computes: the sandbox's
per-execution bounds and pod spec, the egress and ingress allow-lists, the image environment,
the reserved `error.type` names, the workload table, the repository layout, and the two
tool surfaces read out of genetics-mcp-server's tool definitions. Those are the
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
import ast
import difflib
import json
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
    "configs/rcnv_pheno.json": "per-phenotype metadata for the rare-CNV association study",
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
    "scripts/chat-memory-proving-ground.py": "staging recipe and check runner for the "
                                             "per-project chat memory feature; `check all` "
                                             "or one numbered check, each PASS/FAIL with its "
                                             "numbers; cleanup reports DONE or FAILED",
    "scripts/keycloak-register-client.sh": "register or update an MCP OAuth client in the live "
                                           "realm",
    "scripts/keycloak-register-brainzzz.sh": "the brainzzz client specifically",
    "scripts/keycloak-bind-allowlist.sh": "bind the email allow-list authenticator and realm "
                                          "attributes",
    "scripts/keycloak-sync-login-policy.sh": "set the live realm's SSO session lifespans from "
                                             "the template and the default IdP redirect",
    "scripts/keycloak-get-token.sh": "browser auth-code+PKCE flow; prints an access token",
    "scripts/gen-sandbox-docs.py": (
        "generate sandbox/schema/*.md, sandbox/stubs/*.pyi, and the same schema "
        "markdown into genetics-mcp-server's prompt copy"
    ),
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


def _monitor_views():
    """`VIEWS` / `_CONFIG_VIEWS` / `_API_VIEWS` out of scripts/monitor/bq_summary.py, by AST.

    Read rather than imported: the module pulls in google.cloud.bigquery, which the build
    gate has no reason to install.
    """
    path = os.path.join(ROOT, "scripts", "monitor", "bq_summary.py")
    with open(path) as fh:
        tree = ast.parse(fh.read(), path)
    wanted = {"VIEWS", "_CONFIG_VIEWS", "_API_VIEWS"}
    found = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in wanted):
            try:
                found[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                pass
    missing = wanted - set(found)
    if missing:
        print(f"HARNESS: {path} no longer assigns {sorted(missing)} at module level",
              file=sys.stderr)
        raise SystemExit(2)
    return found


def block_monitored_views():
    v = _monitor_views()
    rows = ["| view | expected resources come from |", "|---|---|"]
    for name in v["VIEWS"]:
        if name in v["_CONFIG_VIEWS"]:
            source = "`dataset_to_resource_rules` in `configs/datasets.yaml`"
        elif name in v["_API_VIEWS"]:
            source = "the results-api's coloc pairs"
        else:
            source = "nothing — row counts and distinct resources only"
        rows.append(f"| `{name}` | {source} |")
    return "\n".join(rows)


def block_phenotype_join_views():
    """The views whose phenotype code is called `phenotype`, not `trait_original`.

    Derived from the shape that makes the join different rather than from a list: a
    `tables.<view>` block with a `phenotype` column type and no `trait_original` one.
    """
    tables = load_yaml("configs/datasets.yaml")[0]["tables"]
    odd = [name for name, t in sorted(tables.items())
           if "phenotype" in (t.get("column_types") or {})
           and "trait_original" not in (t.get("column_types") or {})]
    if not odd:
        print("HARNESS: no view in configs/datasets.yaml has a phenotype column without "
              "trait_original — the paragraph this block sits in has no subject",
              file=sys.stderr)
        raise SystemExit(2)
    return "\n".join(f"- `{name}`" for name in odd)



# ---------------------------------------------------------------------------------------
# the two tool surfaces, read out of genetics-mcp-server by AST
# ---------------------------------------------------------------------------------------
#
# THE RULES MIRRORED HERE, and what would make each false — `resolve_tools` in that repo's
# tools/definitions.py is the original, and this is a second implementation of it:
#   1. the data tools are TOOL_DEFINITIONS + BIGQUERY_TOOL_DEFINITIONS. False if a fifth
#      list appears, or if either stops feeding `resolve_tools`.
#   2. code_execution=False is every data tool. False if the no-code branch grows a filter.
#   3. code_execution=True is CODE_EXECUTION_TOOL_DEFINITIONS plus each data tool whose
#      `sdk_replaceable` is false. False if membership moves off that field.
#   4. SUBAGENT_TOOL_DEFINITIONS reaches neither surface. False the moment it is named in
#      `resolve_tools`.
#   5. `disabled` is applied afterwards and is a deployment's choice, not a property of the
#      definitions — so it is deliberately NOT reflected in these counts.
# Three checks stand behind those rules, because a shape check alone did not: a no-code
# branch that grows a filter, an inverted `sdk_replaceable` polarity and a module-level
# `TOOL_DEFINITIONS.extend(...)` all satisfy one and emit a wrong table.
#   - _read_definitions asserts the SHAPE the rules read: the four list names bound to
#     literal lists, a dict per entry, `name`/`category` strings and a bool
#     `sdk_replaceable`; and that nothing at module level mutates the four lists after
#     their literal, which is the one way a literal read can be complete and still wrong.
#   - _assert_resolver_shape pins the normalised source of `resolve_tools`' body against
#     _RESOLVE_TOOLS_BODY below, so ANY edit there stops this generator rather than only
#     the edits someone thought to test for.
#   - _check_against_golden compares the code surface derived here against the server's own
#     tests/golden/tool_surface.json, in the same checkout the definitions came from. It is
#     the only check that compares an ANSWER rather than a shape; it is skipped, loudly, on
#     a branch that has no golden file.
# The file is read from disk rather than imported: importing it would need that repo's venv,
# and a doc gate that only runs where the server's dependencies are installed is not a gate.

MCP_SRC = None  # set from --mcp-src; otherwise resolved like gen-sandbox-docs.py's SDK source
_DEF_LISTS = ("TOOL_DEFINITIONS", "CODE_EXECUTION_TOOL_DEFINITIONS",
              "BIGQUERY_TOOL_DEFINITIONS", "SUBAGENT_TOOL_DEFINITIONS")
_defs_cache = {}

# `resolve_tools`' body, normalised through ast.unparse with the docstring dropped. A diff
# here means the server's resolution changed: re-read THE RULES MIRRORED ABOVE against the
# new source, fix them where they no longer hold, and only then update this pin — replacing
# it to make the check pass is how the mirror silently stops being one.
_RESOLVE_TOOLS_BODY = """\
data_tools = list(TOOL_DEFINITIONS) + list(BIGQUERY_TOOL_DEFINITIONS)
if code_execution:
    tools = list(CODE_EXECUTION_TOOL_DEFINITIONS) + [t for t in data_tools if not t['sdk_replaceable']]
else:
    tools = data_tools
if disabled:
    tools = [t for t in tools if t['name'] not in disabled]
return tools"""



def _load_gen_sandbox_docs():
    """gen-sandbox-docs.py's SDK-source resolution, reused rather than copied.

    Same checkout, same worktree-first order, same GENETICS_SDK_SRC / MCP_SERVER_DIR
    overrides: a build that regenerates the stubs from one mcp-server tree must not
    describe the tool surface of another.
    """
    import importlib.util

    path = os.path.join(ROOT, "scripts", "gen-sandbox-docs.py")
    # without this the traceback exits 1, which this file's convention reads as "stale"
    if not os.path.isfile(path):
        print(f"HARNESS: {path} is missing — the tool blocks resolve their source through "
              f"it", file=sys.stderr)
        raise SystemExit(2)
    spec = importlib.util.spec_from_file_location("gen_sandbox_docs", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _definitions_path():
    gsd = _load_gen_sandbox_docs()
    src = gsd.resolve_sdk_src(MCP_SRC)
    if src is None:
        # gen-sandbox-docs.py's own message names --sdk-src, which this tool does not have
        print("HARNESS: no genetics-mcp-server checkout was found next to this repo.\n"
              "Pass --mcp-src DIR, or set GENETICS_SDK_SRC or MCP_SERVER_DIR. The tool "
              "surfaces cannot be invented from this repo.", file=sys.stderr)
        raise SystemExit(2)
    _defs_cache["src"] = src
    path = os.path.join(src, "src", "genetics_mcp_server", "tools", "definitions.py")
    if not os.path.isfile(path):
        print(f"HARNESS: no tool definitions at {path}", file=sys.stderr)
        raise SystemExit(2)
    return path


def _const(node):
    return node.value if isinstance(node, ast.Constant) else None


def _assert_resolver_shape(tree, path):
    """`resolve_tools` still resolves by the rules this file mirrors, or nothing is emitted."""
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "resolve_tools"), None)
    if fn is None:
        print(f"HARNESS: {path} defines no resolve_tools", file=sys.stderr)
        raise SystemExit(2)
    args = [a.arg for a in fn.args.args]
    body = "\n".join(
        ast.unparse(stmt) for stmt in fn.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
    )
    problems = []
    if args[:2] != ["code_execution", "disabled"]:
        problems.append(f"expects (code_execution, disabled), found {args}")
    if body != _RESOLVE_TOOLS_BODY:
        problems.append("its body no longer matches _RESOLVE_TOOLS_BODY:\n"
                        + "\n".join(difflib.unified_diff(
                            _RESOLVE_TOOLS_BODY.splitlines(), body.splitlines(),
                            "pinned", "found", lineterm="")))
    if problems:
        print(f"HARNESS: {path}: resolve_tools has changed shape — " + "; ".join(problems)
              + ".\nThe rules mirrored in gen-doc-blocks.py no longer describe it; fix them "
                "there before regenerating.", file=sys.stderr)
        raise SystemExit(2)
    # ast.unparse writes an annotated default as `x: T=v`; the doc shows the source form
    sig = ast.unparse(fn).splitlines()[0].rstrip(":")
    return re.sub(r"(:[^,]*?)=", r"\1 = ", sig)


def _assert_no_mutation(tree, path):
    """Nothing at module level adds to the four lists after their literal.

    Reading the literal is only the whole list while the literal IS the list: an
    `extend`/`append`/`+=` further down leaves this generator emitting a table that is
    correct about nothing but the first half of a surface.
    """
    hits = []
    for node in tree.body:
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) \
                and node.target.id in _DEF_LISTS:
            hits.append(f"{node.target.id} += ... (line {node.lineno})")
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Attribute) \
                and node.value.func.attr in ("extend", "append", "insert") \
                and isinstance(node.value.func.value, ast.Name) \
                and node.value.func.value.id in _DEF_LISTS:
            hits.append(f"{node.value.func.value.id}.{node.value.func.attr}(...) "
                        f"(line {node.lineno})")
    if hits:
        print(f"HARNESS: {path}: the definition lists are mutated after their literal — "
              + "; ".join(hits) + ".\nThe literal read in gen-doc-blocks.py is no longer "
              "the whole list; fix the rules mirrored there before regenerating.",
              file=sys.stderr)
        raise SystemExit(2)


def _read_definitions():
    if "lists" in _defs_cache:
        return _defs_cache
    path = _definitions_path()
    with open(path) as fh:
        tree = ast.parse(fh.read(), filename=path)
    _assert_no_mutation(tree, path)
    lists = {}
    for node in tree.body:
        name = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name not in _DEF_LISTS or not isinstance(node.value, ast.List):
            continue
        tools = []
        for entry in node.value.elts:
            if not isinstance(entry, ast.Dict):
                print(f"HARNESS: {path}: {name} holds a non-literal entry", file=sys.stderr)
                raise SystemExit(2)
            fields = {_const(k): v for k, v in zip(entry.keys, entry.values)}
            tool = {k: _const(fields.get(k)) for k in ("name", "category", "sdk_replaceable")}
            if not isinstance(tool["name"], str) or not isinstance(tool["category"], str) \
                    or not isinstance(tool["sdk_replaceable"], bool):
                print(f"HARNESS: {path}: an entry in {name} ({tool['name']!r}) is missing "
                      f"name, category or a boolean sdk_replaceable", file=sys.stderr)
                raise SystemExit(2)
            tools.append(tool)
        if not tools:
            print(f"HARNESS: {path}: {name} is empty", file=sys.stderr)
            raise SystemExit(2)
        lists[name] = tools
    missing = [n for n in _DEF_LISTS if n not in lists]
    if missing:
        print(f"HARNESS: {path}: no literal list assignment for {missing}", file=sys.stderr)
        raise SystemExit(2)
    _defs_cache.update(lists=lists, signature=_assert_resolver_shape(tree, path))
    return _defs_cache


def _check_against_golden(code):
    """The code surface derived here against the server's own golden file.

    Same checkout the definitions came from, so the two cannot describe different trees.
    A branch without the golden file is still buildable, so this says so and returns.
    """
    path = os.path.join(_defs_cache["src"], "tests", "golden", "tool_surface.json")
    if not os.path.isfile(path):
        print(f"note: no {path} — the tool surfaces are emitted from the mirrored rules "
              f"alone, uncross-checked", file=sys.stderr)
        return
    with open(path) as fh:
        golden = json.load(fh)
    try:
        expected = golden["chat_backend"]["profiles"]["code"]["local"]["tools"]
    except (KeyError, TypeError):
        print(f"HARNESS: {path} has no chat_backend.profiles.code.local.tools",
              file=sys.stderr)
        raise SystemExit(2)
    got = [t["name"] for t in code]
    if sorted(got) != sorted(expected):
        missing = sorted(set(expected) - set(got))
        extra = sorted(set(got) - set(expected))
        print(f"HARNESS: the code surface derived here does not match {path}: "
              f"missing {missing}, unexpected {extra}.\nOne of the two is wrong — the rules "
              f"mirrored in gen-doc-blocks.py, or the golden file's own expectation.",
              file=sys.stderr)
        raise SystemExit(2)


def _surfaces():
    """(the no-code surface, the code surface), by the rules above."""
    if "surfaces" in _defs_cache:
        return _defs_cache["surfaces"]
    lists = _read_definitions()["lists"]
    data = lists["TOOL_DEFINITIONS"] + lists["BIGQUERY_TOOL_DEFINITIONS"]
    code = lists["CODE_EXECUTION_TOOL_DEFINITIONS"] + [
        t for t in data if not t["sdk_replaceable"]
    ]
    _check_against_golden(code)
    _defs_cache["surfaces"] = (data, code)
    return data, code


def _names(tools):
    return ", ".join(f"`{t['name']}`" for t in tools)


def _by_category(tools):
    counts = {}
    for t in tools:
        counts[t["category"]] = counts.get(t["category"], 0) + 1
    return ", ".join(f"`{c}` {n}" for c, n in sorted(counts.items()))


def block_tool_lists():
    lists = _read_definitions()["lists"]
    rows = ["| symbol | tools | contents |", "|---|---|---|"]
    for name in _DEF_LISTS:
        tools = lists[name]
        # keyed on the list rather than on its length: a sixth code-execution tool would
        # otherwise cross a length threshold and be labelled "the data tools"
        contents = "the data tools" if name == "TOOL_DEFINITIONS" else _names(tools)
        rows.append(f"| `{name}` | {len(tools)} | {contents} — {_by_category(tools)} |")
    every = [t for name in _DEF_LISTS for t in lists[name]]
    rows += ["", f"**{len(every)} tool definitions in total** across the four lists: "
                 f"{_by_category(every)}."]
    return "\n".join(rows)


def block_tool_surfaces_spec():
    """The same two surfaces, for a spec that points at the reference doc for the rest.

    Deliberately not the same rendering: an identical table in both docs is one table
    someone edits in the wrong place.
    """
    lists = _read_definitions()["lists"]
    execs = lists["CODE_EXECUTION_TOOL_DEFINITIONS"]
    data, code = _surfaces()
    kept = code[len(execs):]
    return "\n".join([
        "| surface | local tools |",
        "|---|---|",
        f"| no-code (`code_execution=False`) | {len(data)} — every data tool |",
        f"| code (`code_execution=True`) | {len(code)} — the {len(execs)} code-execution "
        f"tools, plus the {len(kept)} data tools the SDK cannot stand in for |",
        "",
        f"The code surface: {_names(execs)}, {_names(kept)}.",
    ])


def block_tool_surfaces():
    lists = _read_definitions()["lists"]
    execs = lists["CODE_EXECUTION_TOOL_DEFINITIONS"]
    data, code = _surfaces()
    kept = code[len(execs):]
    return "\n".join([
        "```python",
        _read_definitions()["signature"],
        "```",
        "",
        "| `code_execution` | local tools | membership |",
        "|---|---|---|",
        f"| `False` — the no-code surface | {len(data)} | every data tool: "
        "`TOOL_DEFINITIONS` + `BIGQUERY_TOOL_DEFINITIONS` |",
        f"| `True` — the code surface | {len(code)} | `CODE_EXECUTION_TOOL_DEFINITIONS` "
        f"({len(execs)}) + the {len(kept)} data tools whose `sdk_replaceable` is false |",
        "",
        f"`SUBAGENT_TOOL_DEFINITIONS` ({_names(lists['SUBAGENT_TOOL_DEFINITIONS'])}) reaches "
        "neither surface. `disabled` subtracts from either one afterwards and is a "
        "deployment's choice rather than a property of the definitions, so it is not in "
        "these counts.",
        "",
        f"The code surface, in definition order: {_names(execs)}, then the data tools the "
        f"SDK cannot stand in for — {_names(kept)}.",
    ])

SECURITY = "docs/code-execution-security.md"
SPEC = "docs/project-spec.md"
TOOLS = "docs/chat-tool-reference.md"
DATASETS = "docs/adding-datasets.md"

BLOCKS = {
    "limits": (SECURITY, block_limits),
    "error-types": (SECURITY, block_errors),
    "pod": (SECURITY, block_pod),
    "network": (SECURITY, block_egress),
    "image": (SECURITY, block_image),
    "services": (SPEC, block_services),
    "structure": (SPEC, block_structure),
    "suite-repos": (SPEC, block_suite_repos),
    "monitored-views": (SPEC, block_monitored_views),
    "phenotype-join-views": (DATASETS, block_phenotype_join_views),
    "tool-lists": (TOOLS, block_tool_lists),
    "tool-surfaces": (TOOLS, block_tool_surfaces),
    "tool-surfaces-spec": (SPEC, block_tool_surfaces_spec),
}

# the blocks that need a genetics-mcp-server checkout; --skip-tool-blocks leaves them as they
# stand, so a build can gate everything else before it has cloned that repo
TOOL_BLOCKS = {"tool-lists", "tool-surfaces", "tool-surfaces-spec"}

MARKER = re.compile(
    r"(<!-- BEGIN GENERATED: (?P<name>[a-z-]+) -->\n)(?P<body>.*?)(<!-- END GENERATED: (?P=name) -->)",
    re.S,
)


def render(doc, text, skip=frozenset()):
    seen = set()

    def repl(m):
        name = m.group("name")
        seen.add(name)
        if name not in BLOCKS or BLOCKS[name][0] != doc:
            raise SystemExit(f"{doc}: unknown generated block {name!r}")
        if name in skip:
            return m.group(0)
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
    ap.add_argument("--mcp-src", default=None,
                    help="a genetics-mcp-server checkout to read tools/definitions.py "
                         "from; resolved as scripts/gen-sandbox-docs.py --sdk-src is")
    ap.add_argument("--skip-tool-blocks", action="store_true",
                    help="leave the blocks that need a genetics-mcp-server checkout "
                         "untouched, and neither read nor check them")
    args = ap.parse_args()

    global MCP_SRC
    MCP_SRC = args.mcp_src
    skip = TOOL_BLOCKS if args.skip_tool_blocks else frozenset()

    stale = []
    for doc in sorted({d for d, _ in BLOCKS.values()}):
        path = os.path.join(ROOT, doc)
        with open(path) as fh:
            current = fh.read()
        fresh = render(doc, current, skip)
        n = sum(1 for name, (d, _) in BLOCKS.items() if d == doc and name not in skip)
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
