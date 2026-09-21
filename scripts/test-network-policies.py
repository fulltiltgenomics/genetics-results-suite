#!/usr/bin/env python3
"""Assertions about k8s/network-policies/*.yaml, and optionally about what a cluster enforces.

Every check is decided from the repo alone, with two exceptions, both of which reach a
cluster through kubectl_get() and nothing else:

  * the SANDBOX_ENABLED check, to find out whether a sandbox is actually running, because
    ENABLE_SANDBOX only says whether this deploy will APPLY one — see
    live_sandbox_deployment();
  * the drift check, which is opt-in on LIVE_POLICY_CHECK and compares the policies the
    cluster is enforcing against the committed union — see live_policies().

With no kubectl on PATH the harness still runs end to end and says so; a kubectl that is
present but cannot answer is refused rather than guessed at.

Validating the committed union says nothing about what is enforced: measured 2026-09-01, a
checkout that passes every offline check below had six policies in production whose ingress
rule carried no `from:` at all. That is why the drift check exists and why it reports every
policy rather than aborting on the first — the drift spanned six objects.

The property under test is the one that cannot be read off a single file: NetworkPolicies
in a namespace are ADDITIVE (union), so "mcp-server cannot reach the sandbox" is a
statement about *every* policy in the directory at once, and a single new rule anywhere
undoes it silently. Each check below names the control it defends in
docs/code-execution-security.md so a failure can be judged rather than deleted.

TWO pods are the subject rather than one. The sandbox runs untrusted code and may reach two
in-cluster services with no IP range and no DNS at all; url-fetcher dials addresses a model
chose and may reach every PUBLIC address on 443 — the private ranges excepted — plus DNS,
and holds no credential to lose. Their policies are opposite shapes, so no check here
generalises from one to the other, and a rule satisfying either would fail the other. What
they share is that the NetworkPolicy is the reason the pod is safe rather than hardening on
top of something already safe, which is why both get a label-contract check: a podSelector
matching no pod is not an error, it is silent no-coverage.

Run: python3 scripts/test-network-policies.py
Live drift check: LIVE_POLICY_CHECK=true [KUBE_CONTEXT=...] python3 scripts/test-network-policies.py
Exit 0 = pass, 1 = a control is broken, 2 = the harness could not run.

Not covered here, because it needs a live cluster — see the deploy-window verification
bead: whether Dataplane V2 actually enforces egress to the link-local metadata server,
whether ClusterIP->pod translation happens before egress policy evaluation, and a real
connection attempt from the mcp-server pod to the sandbox Service.
"""

import json
import os
import shutil
import subprocess
import sys

try:
    import yaml
except ImportError:
    # exit 2, not sys.exit(<str>)'s 1: deploy.sh treats 1 as "a control is broken" and aborts
    print("harness cannot run: PyYAML is missing (pip install pyyaml)", file=sys.stderr)
    sys.exit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY_DIR = os.path.join(ROOT, "k8s", "network-policies")
DEPLOY_DIR = os.path.join(ROOT, "k8s", "deployments")

# the label contract declared by k8s/network-policies/sandbox-policy.yaml. This is the
# subset the policies rely on, NOT the sandbox pod's full label set — see sandbox_pod_labels()
SANDBOX_LABELS = {"app": "sandbox"}
SANDBOX_PORT = 8080

# kinds that carry a pod template and could therefore be the sandbox workload. Job, CronJob
# and bare Pod are here because a sandbox landing in one of those shapes would otherwise be
# invisible to BOTH sandbox discovery and the tell-based catch-all that backstops it
WORKLOAD_KINDS = {
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "ReplicaSet",
    "Job",
    "CronJob",
    "Pod",
}

# the label contract declared by k8s/network-policies/url-fetcher-policy.yaml
FETCHER_LABELS = {"app": "url-fetcher"}
FETCHER_PORT = 8090

# the address classes url-fetcher-policy.yaml's `except:` list must carve out of 0.0.0.0/0.
# Without them a 443 rule to the whole internet includes every pod in the cluster, the node,
# the kubelet and 169.254.169.254 — i.e. the server-side request forgery the fetcher pod was
# split out of chat-backend to contain, with the network layer contributing nothing
FETCHER_EGRESS_EXCEPT = {
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "100.64.0.0/10",
}
FETCHER_EGRESS_PORT = 443
DNS_PORT = 53

# the two resolver workloads the fetcher may reach on 53, and the whole DNS surface it gets.
# kube-dns alone is not enough: with NodeLocal DNSCache on, a query addressed to the kube-dns
# ClusterIP is redirected to the node-local-dns pod and is judged against THAT pod's identity
FETCHER_DNS_PODS = {"kube-dns", "node-local-dns"}

# an env var name carrying any of these is treated as a credential wherever a credential is
# forbidden, alongside the structural forms (secretKeyRef, envFrom.secretRef, a secret volume)
CREDENTIAL_NAME_MARKERS = ("SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "_KEY", "APIKEY")

# the Workload Identity annotation. Its ABSENCE is the whole of what makes a dedicated KSA
# safe: a KSA with no binding has no GCP identity to steal, mounted token or not
WORKLOAD_IDENTITY_ANNOTATION = "iam.gke.io/gcp-service-account"

# every pod in this namespace but the sandbox runs as this KSA (docs/code-execution-security.md
# section 2, "Service account"), which Workload Identity binds to a GSA holding BigQuery and GCS
# reader roles — the sandbox running as it would forfeit the no-usable-credential guarantee
SUITE_SERVICE_ACCOUNT = "genetics-suite"

# Workloads permitted to name a service account other than `genetics-suite`, as
# {pod `app` label: KSA name}. Naming one is a sandbox tell (see sandbox_tells()), and the
# ONLY way out of that tell is an entry here — which is not an exemption but an enrolment:
# credential_free_workloads() asserts, for every entry, that the KSA carries no Workload
# Identity annotation, that the token is not mounted, that no secret reaches the environment
# and that the container is non-root on a read-only rootfs. A workload whose identity is not
# worth those assertions does not belong here; it belongs on `genetics-suite`.
#
# So a THIRD workload with an identity of its own still fires the tell and still stops the
# deploy, exactly as url-fetcher did before it was enrolled. Adding a line here to silence
# that is not free: it buys the whole check below, and a workload that cannot pass it fails
# louder than it did as an unrecognised tell.
DEDICATED_SA_WORKLOADS = {
    "url-fetcher": "url-fetcher",
}
# GKE taints the gVisor node pool with this key, and the doc (~line 1668) states the sandbox is
# the only pod tolerating it
GVISOR_TAINT_KEY = "sandbox.gke.io/runtime"

# what each service's pods are labelled with, for "would this rule select that pod?"
POD_LABELS = {
    "mcp-server": {"app": "mcp-server"},
    "chat-backend": {"app": "chat-backend"},
    "db-api": {"app": "db-api"},
    "results-api": {"app": "results-api"},
    "monitor": {"app": "monitor"},
    "auth-gateway": {"app": "auth-gateway"},
    "bff": {"app": "bff"},
    "rag-service": {"app": "rag-service"},
    "sandbox": SANDBOX_LABELS,
    "url-fetcher": FETCHER_LABELS,
}

# every pod `app` value k8s/deployments/ is known to carry. This is the inventory the
# "nobody else" half of the sandbox ingress check sweeps: a sandbox ingress rule admitting an
# app that is in the inventory but not in POD_LABELS passed silently while the sweep ran over
# POD_LABELS alone. Adding a new service here is optional — sweep_labels() also derives apps
# from k8s/deployments/ so the sweep does not go narrow when this list rots.
KNOWN_APPS = {
    "auth-gateway",
    "bff",
    "chat-backend",
    "db-api",
    "frontend",
    "keycloak",
    "keycloak-postgres",
    "mcp-server",
    "oauth2-proxy",
    "rag-service",
    "results-api",
}

failures = []
notes = []


class HarnessError(Exception):
    """The repo could not be read or parsed, so no control was ever judged — exit 2.

    Deliberately not an AssertionError. A manifest that parses but is undecidable is a
    broken control and must exit 1; a YAML syntax error, a non-mapping document or a
    missing directory means this harness did not run, and reporting that as 1 tells
    deploy.sh "a security control is broken" about a file it cannot even read.
    """


def check(name):
    def wrap(fn):
        try:
            fn()
        except HarnessError as e:
            print(f"harness cannot run: {name}: {e}", file=sys.stderr)
            sys.exit(2)
        except AssertionError as e:
            failures.append(f"{name}: {e}")
        except Exception as e:  # a harness bug must not read as a pass
            failures.append(f"{name}: harness error: {e!r}")
        return fn
    return wrap


def manifest_names(directory):
    try:
        names = sorted(os.listdir(directory))
    except OSError as e:
        raise HarnessError(f"cannot list {directory}: {e}") from e
    return [n for n in names if n.endswith((".yaml", ".yml"))]


def load_docs(path):
    try:
        with open(path) as fh:
            docs = list(yaml.safe_load_all(fh))
    except (OSError, yaml.YAMLError) as e:
        raise HarnessError(f"cannot parse {path}: {e}") from e
    for doc in docs:
        if doc is not None and not isinstance(doc, dict):
            raise HarnessError(
                f"{path} contains a top-level YAML document that is not a mapping "
                f"({type(doc).__name__}); this harness cannot judge it"
            )
    return [d for d in docs if d]


def load_policies():
    docs = []
    for fname in manifest_names(POLICY_DIR):
        for doc in load_docs(os.path.join(POLICY_DIR, fname)):
            if doc.get("kind") == "NetworkPolicy":
                doc["__file__"] = fname
                docs.append(doc)
    return docs


try:
    POLICIES = load_policies()
except HarnessError as e:
    print(f"harness cannot run: {e}", file=sys.stderr)
    sys.exit(2)

_SANDBOX_DOCS = None
_DEPLOY_DOCS = None


def pod_template(fname, doc):
    """The pod template of a workload doc, as {"metadata": ..., "spec": ...}; {} if it has none.

    A CronJob's template is one level deeper (spec.jobTemplate.spec.template) and a bare Pod
    IS its own template. Reading spec.template on either yields no labels and no pod spec, so
    every label and tell below would silently read as absent.
    """
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        return {}
    kind = doc.get("kind")
    if kind == "Pod":
        return {"metadata": doc.get("metadata") or {}, "spec": spec}
    if kind == "CronJob":
        job_spec = (spec.get("jobTemplate") or {}).get("spec")
        spec = job_spec if isinstance(job_spec, dict) else {}
    template = spec.get("template")
    if template is None:
        return {}
    if not isinstance(template, dict):
        raise HarnessError(
            f"{fname}: the pod template of {doc.get('kind')} "
            f"{(doc.get('metadata') or {}).get('name')!r} is {template!r}, not a mapping"
        )
    return template


def pod_template_labels(fname, doc):
    labels = ((pod_template(fname, doc).get("metadata") or {}).get("labels")) or {}
    if not isinstance(labels, dict):
        raise HarnessError(f"{fname}: pod template labels are {labels!r}, not a mapping")
    return labels


def sandbox_tells(fname, doc):
    """The sandbox-only pod-spec properties this workload declares, as human-readable strings.

    These are the sandbox-forced tells that still DISCRIMINATE — a deliberately narrower set
    than the sandbox contract in docs/code-execution-security.md, which also obliges
    `automountServiceAccountToken: false`. That one left this list because any pod making no
    API-server calls should adopt it, so it no longer distinguishes the sandbox from anything
    else. What remains is FORCED rather than conventional: GKE taints the gVisor pool
    (`sandbox.gke.io/runtime=gvisor:NoSchedule`, doc ~303-305, ~1840), so a sandbox without the
    runtimeClass and the toleration does not schedule there at all, and the doc states at ~1668
    that it is the only pod tolerating that taint. Running as `genetics-suite` would hand the
    sandbox the Workload-Identity-bound GSA the whole isolation story rests on (doc section 2).

    `automountServiceAccountToken: false` used to be in this list and no longer is: it stopped
    being sandbox-only the moment auth-gateway set it. The sandbox needs it, but so does any pod that makes no API-server calls, and it is the
    control we want other workloads to adopt. Leaving it here would have made "I hardened a pod"
    arrive as `refusing to apply network-policies/` — the exact failure mode the KNOWN_APPS
    trigger was deleted for. The THREE remaining tells are unaffected and each is load-bearing:
    the two gVisor ones are forced by the node pool's taint, so nothing else can carry them by
    accident, and the third — serviceAccountName, not gVisor-derived — fires on any pod naming a
    service account other than `genetics-suite`, which is the sandbox alone.

    An ABSENT serviceAccountName is not a tell: auth-gateway, bff, frontend, keycloak, postgres
    and oauth2-proxy declare none, and counting absence would fire on all of them.

    The serviceAccountName tell is NOT relaxed by name. It is relaxed only for a workload
    ENROLLED in DEDICATED_SA_WORKLOADS, whose identity properties credential_free_workloads()
    then asserts in full — so the tell still fires for a workload nothing asserts anything
    about, which is the case it exists for. Exempting the string "url-fetcher" instead would
    have made the next pod that wants its own identity a rename away from invisible.
    """
    spec = pod_template(fname, doc).get("spec") or {}
    if not isinstance(spec, dict):
        raise HarnessError(f"{fname}: pod template spec is {spec!r}, not a mapping")
    tells = []
    if spec.get("runtimeClassName") is not None:
        tells.append(f"runtimeClassName: {spec['runtimeClassName']!r}")
    for tol in spec.get("tolerations") or []:
        if isinstance(tol, dict) and tol.get("key") == GVISOR_TAINT_KEY:
            tells.append(f"tolerates {GVISOR_TAINT_KEY}")
    sa = spec.get("serviceAccountName")
    if sa is not None and sa != SUITE_SERVICE_ACCOUNT:
        app = pod_template_labels(fname, doc).get("app")
        if DEDICATED_SA_WORKLOADS.get(app) != sa:
            tells.append(f"serviceAccountName: {sa!r}")
    return tells


def _is_sandbox_doc(fname, doc):
    """Is this doc part of the sandbox workload? A UNION of independent tells.

    Each branch is an OR, so adding one can only widen discovery — it can never make a check
    that fires today go inert. Name alone is not enough: a manifest landing the workload as
    code-exec.yaml / object `code-exec` / `app: code-exec` would not be found, every sandbox
    check would skip in silence, and the harness would print a reassuring "not landed yet"
    note while the pod ran with unrestricted egress. Labels alone are not enough either: a
    pod labelled `app: sandbox-runner` satisfies no label contract and must still be found so
    the contract check can FAIL on it rather than not see it.

    The label branch reads the `app` VALUE only. Matching the stringified label dict adopted
    any pod carrying an unrelated key that merely mentions the sandbox — `sandbox-client:
    "true"` on chat-backend, exactly what a future sandbox-client policy peer would use —
    which made chat-backend's labels the sandbox's, dropped sandbox-policy.yaml out of
    sandbox_policies(), and cascaded into false failures against a working control.
    """
    if "sandbox" in fname.lower():
        return True
    if "sandbox" in str((doc.get("metadata") or {}).get("name") or "").lower():
        return True
    labels = pod_template_labels(fname, doc)
    if "sandbox" in str(labels.get("app") or "").lower():
        return True
    # the label contract itself: catches "renamed the file and the object, kept app: sandbox"
    return all(labels.get(k) == v for k, v in SANDBOX_LABELS.items())


def sandbox_workload_docs():
    """Every doc in k8s/deployments/ that declares part of the sandbox workload."""
    global _SANDBOX_DOCS
    if _SANDBOX_DOCS is None:
        found = []
        for fname in manifest_names(DEPLOY_DIR):
            for doc in load_docs(os.path.join(DEPLOY_DIR, fname)):
                if _is_sandbox_doc(fname, doc):
                    found.append((fname, doc))
        _SANDBOX_DOCS = found
    return _SANDBOX_DOCS


def sandbox_workloads():
    return [(f, d) for f, d in sandbox_workload_docs() if d.get("kind") in WORKLOAD_KINDS]


def sandbox_services():
    return [(f, d) for f, d in sandbox_workload_docs() if d.get("kind") == "Service"]


def sandbox_is_deployed():
    """Has the sandbox workload landed in k8s/deployments/ in any shape at all?

    A Service with no workload counts: it means the manifests are half-written, which the
    label-contract check must report rather than skip. ConfigMaps and Secrets named
    `sandbox-*` do not — they carry no pod labels and no ports to contradict.
    """
    return bool(sandbox_workloads() or sandbox_services())


def selects(selector, labels):
    """Does a podSelector select a pod whose label set is EXACTLY `labels`? Empty selects all.

    Only sound where the pod's labels are known in full — a manifest this harness has read.
    Where they are not, use may_select(); see its docstring for why the difference is a hole.
    """
    if selector is None:
        return False
    if selector.get("matchExpressions"):
        raise AssertionError("matchExpressions is unhandled by this harness; extend it")
    match = selector.get("matchLabels") or {}
    return all(labels.get(k) == v for k, v in match.items())


def may_select(selector, known_labels):
    """Could this podSelector select a pod carrying AT LEAST `known_labels`?

    The sandbox pod's full label set is not knowable from the policy directory: until a
    lands a workload manifest, all this harness has is the contract subset sandbox-policy.yaml
    declares. `selects()` answers "does this selector match a pod labelled exactly
    {app: sandbox}", so a policy with matchLabels {app: sandbox, tier: untrusted} — which WOULD
    select the real pod if it carries `tier` — is reported as not selecting it, and every
    sandbox check below then skips that policy in silence. That is the fail-open direction.

    So: a selector key this harness knows nothing about is assumed to match, and only a key
    known to hold a DIFFERENT value rules the policy out.
    """
    if selector is None:
        raise AssertionError(
            "a NetworkPolicy in this directory has no spec.podSelector. The field is required "
            "by the API, so the manifest is either invalid or a shape this harness cannot "
            "decide — it will not be assumed to select nothing"
        )
    if selector.get("matchExpressions"):
        raise AssertionError("matchExpressions is unhandled by this harness; extend it")
    match = selector.get("matchLabels") or {}
    return all(known_labels.get(k, v) == v for k, v in match.items())


def sandbox_pod_labels():
    """The sandbox pod's labels: the real set once a manifest declares them, the contract
    subset from sandbox-policy.yaml until then."""
    workloads = sandbox_workloads()
    if not workloads:
        return dict(SANDBOX_LABELS)
    seen = []
    for fname, doc in workloads:
        labels = pod_template_labels(fname, doc)
        assert labels, (
            f"{doc['kind']} {doc['metadata'].get('name')!r} ({fname}) declares no pod template "
            "labels, so no NetworkPolicy podSelector can select its pods and the sandbox would "
            "run with unrestricted egress"
        )
        seen.append((fname, labels))
    first = seen[0][1]
    for fname, labels in seen[1:]:
        assert labels == first, (
            f"sandbox workloads declare differing pod labels ({seen[0][0]}: {first}, "
            f"{fname}: {labels}); this harness cannot decide which set the policies select"
        )
    return first


def sweep_labels(exclude):
    """Every pod this namespace is known to run except `exclude`, as {name: labels}.

    `exclude` has no default: it is the pod the "and nobody else" assertion is ABOUT, and
    there are now two of them. Defaulting it to the sandbox would have let the fetcher's
    sweep quietly omit the sandbox — the one source whose admission would turn this epic
    into option B by the back door.

    The "nobody else" sweep has to cover the whole inventory: it ran over POD_LABELS alone
    while KNOWN_APPS enumerated three more apps, so `from: [podSelector {app: frontend}]` on
    the sandbox passed every check in this file. Sources are unioned, most precise first —
    POD_LABELS where it has the real label set, the manifests themselves so a service added
    without touching this file is still swept, and KNOWN_APPS as a name-only fallback.
    """
    labels = {}
    for app in KNOWN_APPS:
        labels[app] = {"app": app}
    for fname in manifest_names(DEPLOY_DIR):
        for doc in load_docs(os.path.join(DEPLOY_DIR, fname)):
            if doc.get("kind") not in WORKLOAD_KINDS or _is_sandbox_doc(fname, doc):
                continue
            discovered = pod_template_labels(fname, doc)
            app = discovered.get("app")
            if app:
                labels[app] = discovered
    labels.update(POD_LABELS)
    labels.pop(exclude, None)
    return labels


def sandbox_policies():
    return [p for p in POLICIES if may_select(p["spec"].get("podSelector"), sandbox_pod_labels())]


def deploy_docs():
    """Every document in k8s/deployments/, parsed once, as (filename, doc)."""
    global _DEPLOY_DOCS
    if _DEPLOY_DOCS is None:
        _DEPLOY_DOCS = [
            (fname, doc)
            for fname in manifest_names(DEPLOY_DIR)
            for doc in load_docs(os.path.join(DEPLOY_DIR, fname))
        ]
    return _DEPLOY_DOCS


def workloads_labelled(app):
    """Workload docs whose POD TEMPLATE carries `app: <app>` — what a podSelector selects.

    Keyed on the pod label rather than on the object name or the file, because the podSelector
    is: an object renamed to `fetcher` with the label kept is still covered by the policy, and
    an object that keeps the name while the label drifts is not covered by anything.
    """
    return [
        (f, d)
        for f, d in deploy_docs()
        if d.get("kind") in WORKLOAD_KINDS and pod_template_labels(f, d).get("app") == app
    ]


def services_selecting(app):
    return [
        (f, d)
        for f, d in deploy_docs()
        if d.get("kind") == "Service" and ((d.get("spec") or {}).get("selector") or {}).get("app") == app
    ]


def service_account_doc(name):
    for f, d in deploy_docs():
        if d.get("kind") == "ServiceAccount" and (d.get("metadata") or {}).get("name") == name:
            return (f, d)
    return None


def pod_labels_of(app):
    """The full label set of `app`'s pods, or the policy-contract subset if none has landed."""
    workloads = workloads_labelled(app)
    if not workloads:
        return dict(POD_LABELS.get(app) or {"app": app})
    first = pod_template_labels(*workloads[0])
    for fname, doc in workloads[1:]:
        labels = pod_template_labels(fname, doc)
        assert labels == first, (
            f"{app} workloads declare differing pod labels ({workloads[0][0]}: {first}, "
            f"{fname}: {labels}); this harness cannot decide which set the policies select"
        )
    return first


def fetcher_policies():
    return [p for p in POLICIES if may_select(p["spec"].get("podSelector"), pod_labels_of("url-fetcher"))]


def fetcher_named_policies():
    """Policies that NAME the fetcher, i.e. excluding the namespace-wide catch-all."""
    return [p for p in fetcher_policies() if (p["spec"].get("podSelector") or {}).get("matchLabels")]


def containers(fname, doc):
    spec = pod_template(fname, doc).get("spec") or {}
    return (spec.get("containers") or []) + (spec.get("initContainers") or [])


def credential_env(fname, doc):
    """Every way a credential could reach this workload's containers, as readable strings.

    Structural forms first (a secretKeyRef, an envFrom secretRef, a Secret volume, a projected
    serviceAccountToken) and then the name heuristic, because a literal value that is a
    credential is indistinguishable from any other literal — the name is all there is.
    """
    found = []
    for c in containers(fname, doc):
        cname = c.get("name")
        for env in c.get("env") or []:
            name = str(env.get("name") or "")
            if (env.get("valueFrom") or {}).get("secretKeyRef"):
                found.append(f"{cname} env {name} from a secretKeyRef")
            elif any(m in name.upper() for m in CREDENTIAL_NAME_MARKERS):
                found.append(f"{cname} env {name}")
        for src in c.get("envFrom") or []:
            if src.get("secretRef"):
                found.append(f"{cname} envFrom secretRef {src['secretRef'].get('name')}")
    for vol in (pod_template(fname, doc).get("spec") or {}).get("volumes") or []:
        if vol.get("secret"):
            found.append(f"volume {vol.get('name')} is a Secret")
        for src in (vol.get("projected") or {}).get("sources") or []:
            if src.get("serviceAccountToken"):
                found.append(f"volume {vol.get('name')} projects a serviceAccountToken")
    return found


def dockerfile_base_images(path):
    """The image ref of every FROM in a Dockerfile, in order."""
    try:
        with open(path) as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        raise HarnessError(f"cannot read {path}: {e}") from e
    return [
        line.split()[1]
        for line in (ln.strip() for ln in lines)
        if line.upper().startswith("FROM ") and len(line.split()) > 1
    ]


def kubectl_argv():
    """kubectl and the flags every cluster query in this file shares, or None if it is absent.

    KUBE_CONTEXT selects the cluster for the whole harness rather than for one probe, so the
    drift check and the sandbox probe can never end up answering about different clusters in
    the same run. Unset means kubectl's own current context, which is what deploy.sh relies on.
    """
    exe = shutil.which("kubectl")
    if exe is None:
        return None
    context = os.environ.get("KUBE_CONTEXT", "").strip()
    return [exe, *(["--context", context] if context else [])]


def kubectl_get(args):
    """One `kubectl get`, or None if it could not answer (no context, unreachable, forbidden).

    The verb is prepended here rather than passed in: this harness is a check, and a check
    that could reach `apply`, `patch` or `delete` through a caller's argument list is one
    typo away from mutating the cluster it was asked to inspect.
    """
    argv = kubectl_argv()
    if argv is None:
        return None
    try:
        proc = subprocess.run(
            [*argv, "get", *args], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc if proc.returncode == 0 else None


def live_sandbox_deployment():
    """Is a sandbox workload live in the cluster right now?

    The only cluster call in the DEFAULT invocation (live_policies() is the other one, and it
    runs only when asked). It exists because one relaxation cannot be decided from the repo:
    ENABLE_SANDBOX=false means "this run will not apply the sandbox", and deploy.sh SKIPS
    sandbox.yaml when the gate is off rather than deleting it, so the env var says nothing
    about whether a sandbox is serving. Every other check here stays offline.

    Returns one of:
      "live"       a workload whose name mentions the sandbox exists in the namespace
      "absent"     the namespace exists, was read, and holds no such workload
      "no-kubectl" kubectl is not on PATH — necessarily a manual offline run, since deploy.sh
                   does everything through kubectl and could not have reached this harness
      "unknown"    kubectl is there and the query failed (no context, unreachable, forbidden),
                   or the namespace itself does not exist

    The query covers every kind in WORKLOAD_KINDS, derived from it rather than restated, so the
    two cannot drift: a sandbox running as a StatefulSet, Job or bare Pod must not read "absent"
    and relax the check, for exactly the reason WORKLOAD_KINDS covers seven kinds offline.

    The namespace is verified to exist first because an empty stdout is otherwise ambiguous:
    real kubectl exits 0 and prints "No resources found" to stderr for a namespace that is not
    there, so a mistyped NAMESPACE would relax rather than refuse. That the harness default and
    deploy.sh's default happen to be the same string is a coincidence of value, not a guarantee.

    Name matching is a substring, not equality, for the same reason _is_sandbox_doc() unions its
    tells: a live workload called `sandbox-runner` or `code-exec-sandbox` must count.
    """
    if kubectl_argv() is None:
        return "no-kubectl"
    namespace = os.environ.get("NAMESPACE", "genetics")
    if kubectl_get(["namespace", namespace, "-o", "name"]) is None:
        return "unknown"
    resources = ",".join(sorted(k.lower() + "s" for k in WORKLOAD_KINDS))
    proc = kubectl_get([resources, "-n", namespace, "-o", "name"])
    if proc is None:
        return "unknown"
    for line in proc.stdout.splitlines():
        if "sandbox" in line.rsplit("/", 1)[-1].lower():
            return "live"
    return "absent"


def live_policies():
    """The NetworkPolicies the cluster is actually enforcing, as {name: spec}.

    Returns (state, policies) with the same three-way answer live_sandbox_deployment() gives,
    for the same reason: a cluster that cannot be read must never be reported as a cluster
    with nothing wrong.

      ("live", {...})     the namespace was read and its policies returned
      ("no-kubectl", {})  kubectl is not on PATH
      ("unknown", {})     kubectl is there and the query failed, or the namespace is absent

    The namespace is verified first because `kubectl get networkpolicies -n nope` exits 0 with
    empty stdout, which would otherwise read as "the cluster enforces nothing" — a state this
    check is supposed to SHOUT about — rather than as "wrong namespace".
    """
    if kubectl_argv() is None:
        return "no-kubectl", {}
    namespace = os.environ.get("NAMESPACE", "genetics")
    if kubectl_get(["namespace", namespace, "-o", "name"]) is None:
        return "unknown", {}
    proc = kubectl_get(["networkpolicies", "-n", namespace, "-o", "json"])
    if proc is None:
        return "unknown", {}
    try:
        items = json.loads(proc.stdout).get("items") or []
    except (ValueError, AttributeError):
        return "unknown", {}
    live = {}
    for item in items:
        name = ((item.get("metadata") or {}).get("name"))
        spec = item.get("spec")
        if name and isinstance(spec, dict):
            live[name] = spec
    return "live", live


def _canon(obj):
    return json.dumps(obj, sort_keys=True)


def _canon_peer(peer):
    """One from:/to: entry, canonical. Rendered short where it is the ordinary podSelector."""
    labels = (peer.get("podSelector") or {}).get("matchLabels") if isinstance(peer, dict) else None
    if isinstance(peer, dict) and set(peer) == {"podSelector"} and labels and set(labels) == {"app"}:
        return f"app={labels['app']}"
    return _canon(peer)


def _canon_ports(rule):
    """A rule's ports as sortable "8080/TCP" strings.

    protocol is defaulted on BOTH sides: the API server writes `protocol: TCP` into every port
    it stores while the manifests mostly omit it, so comparing them raw makes every policy look
    drifted and hides the ones that really are.
    """
    out = []
    for p in rule.get("ports") or []:
        if not isinstance(p, dict):
            out.append(_canon(p))
            continue
        port = p.get("port")
        span = f"{port}-{p['endPort']}" if p.get("endPort") is not None else f"{port}"
        out.append(f"{span}/{p.get('protocol', 'TCP')}")
    return sorted(out)


def _direction(spec, direction):
    """What one direction of a policy admits, in the terms drift is judged on."""
    key = "from" if direction == "ingress" else "to"
    rules = [r for r in (spec.get(direction) or []) if isinstance(r, dict)]
    return {
        "rules": len(rules),
        # a rule with no from:/to: admits EVERY source, and this is the headline number:
        # measured 2026-09-01, six production policies had drifted into exactly that shape
        "open": sum(1 for r in rules if r.get(key) is None),
        "peers": sorted(_canon_peer(pe) for r in rules for pe in (r.get(key) or [])),
        "ports": sorted(pt for r in rules for pt in _canon_ports(r)),
        "rule_set": sorted(
            "{} {} on {}".format(
                key,
                "ANY (no peer list)" if r.get(key) is None
                else sorted(_canon_peer(pe) for pe in r[key]),
                _canon_ports(r) or "ANY PORT",
            )
            for r in rules
        ),
    }


def policy_shape(spec):
    """Everything about a policy that decides what traffic it admits, and nothing else.

    Rules are compared as a SET: NetworkPolicy rules are additive and unordered, so a reorder
    is not drift and reporting it as such would train people to ignore this check.
    """
    return {
        "podSelector": _canon(spec.get("podSelector") or {}),
        "policyTypes": sorted(policy_types(spec)),
        "ingress": _direction(spec, "ingress"),
        "egress": _direction(spec, "egress"),
    }


def describe_drift(committed, live):
    """Every way `live` differs from `committed`, as one line each. Never stops at the first."""
    lines = []
    if committed["podSelector"] != live["podSelector"]:
        lines.append(
            f"    podSelector: committed {committed['podSelector']}, live {live['podSelector']} "
            "(a changed selector applies the whole policy to different pods)"
        )
    if committed["policyTypes"] != live["policyTypes"]:
        lines.append(
            f"    policyTypes: committed {committed['policyTypes']}, live {live['policyTypes']} "
            "(a direction the cluster does not list is not restricted at all)"
        )
    for direction in ("ingress", "egress"):
        c, v = committed[direction], live[direction]
        if c == v:
            continue
        key = "from" if direction == "ingress" else "to"
        if c["open"] != v["open"]:
            lines.append(
                f"    {direction} rules with no '{key}:': committed {c['open']}, live {v['open']}"
                + (" — the live rule admits EVERY source" if v["open"] > c["open"] else "")
            )
        if c["rules"] != v["rules"]:
            lines.append(
                f"    {direction} rule count: committed {c['rules']}, live {v['rules']}"
            )
        if c["peers"] != v["peers"]:
            lines.append(
                f"    {direction} peer selectors: committed {c['peers']}, live {v['peers']}"
            )
        if c["ports"] != v["ports"]:
            lines.append(
                f"    {direction} ports: committed {c['ports']}, live {v['ports']}"
            )
        if c["rule_set"] != v["rule_set"] and all(
            c[k] == v[k] for k in ("open", "rules", "peers", "ports")
        ):
            # every other dimension agrees, so the peers and ports have been REGROUPED across
            # rules — which changes what is admitted, since a rule pairs its peers with its own
            # ports. Only reported here because the lines above would otherwise repeat it
            lines.append(
                f"    {direction} rules pair peers with ports differently: committed "
                f"{c['rule_set']}, live {v['rule_set']}"
            )
    return lines


def _is_on(value):
    # the services parse this with .strip().lower() against {1, true, yes}; anything else —
    # including a valueFrom with no literal value — is off
    return str(value).strip().lower() in {"1", "true", "yes"}


def sandbox_enabled_values():
    """The literal SANDBOX_ENABLED values each verifier's Deployment declares, per file.

    chat-backend is not a verifier — it mints tokens and gates the run_analysis tool rather
    than authorizing requests — but it must track the other two once the sandbox exists:
    left "false" while db-api/results-api flip true, the sandbox deploys and run_analysis
    stays withheld from every tool list with no signal anywhere.
    """
    values = {}
    for fname in ("db-api.yaml", "results-api.yaml", "chat-backend.yaml"):
        path = os.path.join(DEPLOY_DIR, fname)
        found = []
        if os.path.exists(path):
            for d in load_docs(path):
                if d.get("kind") != "Deployment":
                    continue
                for container in d["spec"]["template"]["spec"].get("containers") or []:
                    for env in container.get("env") or []:
                        if env.get("name") == "SANDBOX_ENABLED":
                            found.append(env.get("value"))
        values[fname] = found
    return values


def policy_types(spec):
    """The rule types a policy actually carries, inferred when `policyTypes:` is omitted.

    `policyTypes` is optional and the API server fills it in: a spec with a NON-EMPTY
    `egress:` affects Egress, and every spec affects Ingress whether or not it contains
    `ingress:`.
    Reading the field as written instead makes a policy that omits it invisible here while
    the cluster enforces it in full — a sandbox rule admitting mcp-server would pass.
    """
    declared = spec.get("policyTypes")
    if declared:
        return set(declared)
    inferred = {"Ingress"}
    # truthiness, not `in`: the defaulter is `len(spec.Egress) != 0`, so `egress: []` and
    # `egress:` (null) add nothing — treating them as Egress would report an unrestricted
    # sandbox as deny-by-default
    if spec.get("egress"):
        inferred.add("Egress")
    return inferred


def peer_selects(peer, labels, widen):
    """Does one entry of a from:/to: list admit a pod carrying `labels`?

    Fail-closed: any peer shape this harness cannot decide offline is refused rather than
    guessed at. `- namespaceSelector: {}` in particular matches EVERY pod in EVERY
    namespace — `genetics` and mcp-server included — so answering "no pod match" for it
    reports a wide-open rule as closed.

    `widen` picks which of selects()/may_select() is the fail-closed answer, and that is a
    property of the CALLER'S ASSERTION, not of this function — see rules_reaching().
    """
    if "ipBlock" in peer:
        raise AssertionError(
            f"peer {peer!r} uses an ipBlock, whose coverage of pod IPs cannot be decided "
            "offline. Rewrite the rule as podSelector-only, or extend this harness to "
            "reason about CIDRs before relying on it again."
        )
    if "namespaceSelector" in peer:
        raise AssertionError(
            f"peer {peer!r} carries a namespaceSelector. `namespaceSelector: {{}}` admits "
            "every pod in every namespace including mcp-server, and a labelled one needs "
            "live Namespace objects to resolve. Rewrite the rule as podSelector-only."
        )
    if peer.get("podSelector") is None:
        raise AssertionError(
            f"peer {peer!r} has no podSelector; this harness only admits podSelector-only "
            "peers, so its verdict on this rule would be meaningless"
        )
    return (may_select if widen else selects)(peer["podSelector"], labels)


def rules_reaching(policies, direction, labels, *, widen):
    """Rules in `policies` whose from:/to: admits `labels` — including from-less rules,
    which admit EVERY source and are the bug class fad/k4t just closed.

    `widen` has no default on purpose: every call site must state its assertion's polarity,
    because the fail-closed direction is the opposite one for each.

    widen=True for a must-NOT-reach assertion (`assert not hits` — mcp-server, monitor, the
    "nobody else" half of the ingress check). A peer this harness cannot fully decide, e.g.
    matchLabels {app: mcp-server, role: tools} where POD_LABELS carries no `role`, must count
    as reaching; narrowing there answers "mcp-server is not admitted" about a rule that would
    admit it in the cluster, and layer 2 of the MCP exclusion goes silently uncovered.

    widen=False for a must-REACH assertion (`assert hits` — db-api/results-api admitting the
    sandbox). Widening is fail-OPEN there, and increasingly so once a sandbox lands and
    sandbox_pod_labels() is the pod's COMPLETE set: an unknown selector key is then genuinely
    absent from the pod, so may_select() would report a dead path as live.
    """
    key = "from" if direction == "ingress" else "to"
    hits = []
    for p in policies:
        if direction.capitalize() not in policy_types(p["spec"]):
            continue
        for rule in p["spec"].get(direction) or []:
            peers = rule.get(key)
            if peers is None:
                hits.append((p["metadata"]["name"], p["__file__"], rule))
                continue
            # every peer is evaluated before the verdict, deliberately: peer_selects() refuses
            # shapes it cannot decide by raising, and `any(... for ...)` would short-circuit
            # past an undecidable peer sitting behind a matching one — `from: [podSelector
            # chat-backend, ipBlock 0.0.0.0/0]` would be answered "reaches chat-backend" with
            # the ipBlock never looked at, which is the fail-closed promise above going unkept
            matches = [peer_selects(peer, labels, widen) for peer in peers]
            if any(matches):
                hits.append((p["metadata"]["name"], p["__file__"], rule))
    return hits


@check("a policy names the sandbox specifically")
def _():
    # not `assert sandbox_policies()`: policies.yaml's namespace-wide default-deny-ingress has
    # `podSelector: {}`, which selects the sandbox along with everything else, so that form of
    # the check could never fail and asserted nothing. What has to hold is that some policy
    # names the sandbox pod — default-deny-ingress declares no Egress and admits nobody, so it
    # is not the coverage this check is about.
    named = [
        p
        for p in sandbox_policies()
        if (p["spec"].get("podSelector") or {}).get("matchLabels")
    ]
    assert named, (
        f"no NetworkPolicy in k8s/network-policies/ has a podSelector naming the sandbox pod "
        f"({sandbox_pod_labels()}); only the namespace-wide catch-all covers it, which declares "
        "no Egress at all. A policy that selects nothing is not an error, it is silent "
        "no-coverage."
    )


@check("layer 2 of MCP exclusion: mcp-server cannot reach the sandbox")
def _():
    hits = rules_reaching(sandbox_policies(), "ingress", POD_LABELS["mcp-server"], widen=True)
    assert not hits, (
        "mcp-server is admitted to the sandbox by "
        + ", ".join(f"{n} ({f})" for n, f, _ in hits)
        + " — docs/code-execution-security.md section 5, layer 2"
    )


@check("sandbox ingress admits chat-backend and nobody else")
def _():
    pols = sandbox_policies()
    # both polarities live in this one equality, so each side gets its own fail-closed
    # direction: an undecidable peer must count as reaching for everyone who must NOT
    # (widen), and must NOT count as reaching for chat-backend, who must (narrow)
    admitted = {
        name
        for name, labels in sweep_labels("sandbox").items()
        if name != "chat-backend" and rules_reaching(pols, "ingress", labels, widen=True)
    }
    if rules_reaching(pols, "ingress", POD_LABELS["chat-backend"], widen=False):
        admitted.add("chat-backend")
    assert admitted == {"chat-backend"}, f"expected {{'chat-backend'}}, got {admitted or set()}"


@check("sandbox ingress is on 8080/TCP only")
def _():
    # widen: this asserts a RESTRICTION on the admitting rules, so the more rules it is made
    # to inspect the tighter it is; missing one would leave an unchecked port open
    for _n, _f, rule in rules_reaching(
        sandbox_policies(), "ingress", POD_LABELS["chat-backend"], widen=True
    ):
        ports = rule.get("ports")
        assert ports, "a portless ingress rule admits every port on the sandbox"
        for port in ports:
            assert port.get("port") == SANDBOX_PORT, f"unexpected sandbox ingress port {port}"


@check("no sandbox rule is from-less or to-less")
def _():
    for p in sandbox_policies():
        for direction, key in (("ingress", "from"), ("egress", "to")):
            for rule in p["spec"].get(direction) or []:
                assert rule.get(key) is not None, (
                    f"{p['metadata']['name']} ({p['__file__']}) has a {direction} rule with no "
                    f"'{key}:' — that admits ALL peers"
                )


@check("sandbox egress is deny-by-default")
def _():
    assert any("Egress" in policy_types(p["spec"]) for p in sandbox_policies()), (
        "no policy selecting the sandbox lists Egress in policyTypes, so its egress is "
        "unrestricted. There is no namespace-wide default-deny-egress: egress is restricted "
        "for a pod only once some policy selecting THAT pod declares Egress, and the "
        "namespace's other egress policy (url-fetcher-egress) selects app: url-fetcher and "
        "does nothing for this one"
    )


@check("sandbox egress allow-list is exactly db-api:8080 and results-api:4000")
def _():
    # This pins the allow-list in BOTH directions, and the "not narrower" half is the one that
    # needs explaining. Dropping results-api:4000 was proposed so that
    # sandbox traffic could be forced down a path that always carries the per-execution token.
    # It cannot be dropped: the SDK's `search(rsids=...)` calls GET /v1/rsid/variants on
    # results-api, 16 of its 25 public functions are results-api-only (census in
    # and there is no other path — the sandbox is denied
    # auth-gateway by design and auth-gateway would not validate a sandbox HS256 token anyway.
    # What makes keeping this entry safe is NOT in this file and cannot be: results-api shrinks
    # its anonymous surface to /healthz whenever ANONYMOUS_SURFACE_MINIMAL is on — which is its
    # default, and which SANDBOX_ENABLED=true forces regardless (so
    # that turning the sandbox off cannot re-widen it) — so an anonymous request from this pod
    # gets a 401 rather than an unaccounted 200. That control is a route
    # decorator, invisible to a manifest reader, and is pinned by results-api's
    # tests/test_anonymous_surface.py. Deleting this entry here would break the SDK; deleting
    # that test there would reopen the hole. Neither is a cleanup.
    allowed = set()
    for p in sandbox_policies():
        for rule in p["spec"].get("egress") or []:
            ports = rule.get("ports")
            assert ports, f"{p['metadata']['name']} has a portless egress rule"
            for peer in rule.get("to") or []:
                assert "ipBlock" not in peer, (
                    f"{p['metadata']['name']} has an egress ipBlock — the sandbox must reach no "
                    "IP range at all; this is what closes pip install, s3:// writes and DNS"
                )
                sel = (peer.get("podSelector") or {}).get("matchLabels") or {}
                for port in ports:
                    # protocol is part of the identity of an allowed pair: a UDP variant of
                    # an allowed (app, port) is a different hole, and TCP is the API default
                    allowed.add((sel.get("app"), port.get("port"), port.get("protocol", "TCP")))
    assert allowed == {("db-api", 8080, "TCP"), ("results-api", 4000, "TCP")}, (
        f"egress allow-list is {allowed}"
    )


@check("no DNS egress FOR THE SANDBOX: the design eliminates DNS rather than allowing it")
def _():
    # scoped to policies selecting the sandbox, and the name says so, because the namespace
    # now contains a DNS rule: url-fetcher-egress has one, deliberately. The reasoning below
    # is about a pod that runs untrusted code and has a token to steal, neither of which is
    # true of the fetcher — so the fetcher is not a precedent, and this check must not be
    # widened into a namespace-wide one that the fetcher would then be exempted from
    for p in sandbox_policies():
        for rule in p["spec"].get("egress") or []:
            for port in rule.get("ports") or []:
                assert port.get("port") != 53, (
                    "a kube-dns rule reappeared. docs/code-execution-security.md 'On DNS' "
                    "measures that path at ~200 KB/s sustained exfiltration and eliminates it "
                    "in favour of hostAliases; re-adding it invalidates section 6.2 control #1 "
                    "and section 6.4 control #2"
                )
            for peer in rule.get("to") or []:
                ns = peer.get("namespaceSelector") or {}
                assert "kube-system" not in str(ns), "egress to kube-system reappeared"


@check("reverse direction: db-api and results-api admit the sandbox")
def _():
    for target, port in (("db-api", 8080), ("results-api", 4000)):
        pols = [p for p in POLICIES if selects(p["spec"].get("podSelector"), POD_LABELS[target])]
        hits = rules_reaching(pols, "ingress", sandbox_pod_labels(), widen=False)
        assert hits, (
            f"{target} does not admit app: sandbox. The sandbox's egress allow-list is "
            f"necessary but not sufficient — default-deny-ingress drops the connection at the "
            f"receiving end, so the path is dead with no error on the sandbox side."
        )
        assert any(
            any(pt.get("port") == port for pt in (rule.get("ports") or []))
            for _n, _f, rule in hits
        ), f"{target} admits the sandbox but not on port {port}"


@check("the monitor was not extended to the sandbox")
def _():
    hits = rules_reaching(sandbox_policies(), "ingress", POD_LABELS["monitor"], widen=True)
    assert not hits, (
        "monitor-policy.yaml (or another file) now admits the monitor to the sandbox. Liveness "
        "is the kubelet's job and kubelet probes are exempt from NetworkPolicy on this cluster."
    )


@check("label contract: the sandbox workload matches the policy selector")
def _():
    # discovered, not read from a hard-coded k8s/deployments/sandbox.yaml: a deploy may land the
    # workload as sandbox-deployment.yaml or split the Deployment and Service across files, and
    # a check keyed on one filename would then stay inert forever while printing a reassuring
    # note into deploy output — the exact failure mode this check is supposed to prevent
    if not sandbox_is_deployed():
        notes.append(
            "no sandbox Deployment or Service found in k8s/deployments/ "
            "; the label-contract check is inert until one lands"
        )
        return
    workloads = sandbox_workloads()
    assert workloads, (
        "k8s/deployments/ declares a sandbox Service ("
        + ", ".join(f"{d['metadata'].get('name')} in {f}" for f, d in sandbox_services())
        + ") but no Deployment/StatefulSet/DaemonSet to carry the pod labels the policies select"
    )
    labels = sandbox_pod_labels()
    assert selects({"matchLabels": SANDBOX_LABELS}, labels), (
        f"sandbox pod labels {labels} ("
        + ", ".join(f"{d['kind']} in {f}" for f, d in workloads)
        + f") are not selected by {SANDBOX_LABELS}; every rule in sandbox-policy.yaml would "
        "then apply to no pod and the sandbox would run with unrestricted egress"
    )
    for fname, d in sandbox_services():
        for port in d["spec"].get("ports") or []:
            tp = port.get("targetPort", port.get("port"))
            assert isinstance(tp, int), (
                f"sandbox Service port {port} uses a named targetPort ({tp!r}), which resolves "
                f"to a pod port only via the container's ports[].name ({fname}). This "
                "harness does not resolve names, so it cannot decide whether the Service "
                "targets "
                f"{SANDBOX_PORT}: extend it to look the name up in the Deployment's "
                "containerPort list, or write the Service with a numeric targetPort"
            )
            assert tp == SANDBOX_PORT, (
                f"sandbox Service targets port {port} but the ingress rule allows "
                f"{SANDBOX_PORT}; NetworkPolicy ports are pod ports, not Service ports"
            )


@check("no undiscovered workload carries the sandbox's pod-spec tells")
def _():
    """The other half of the discovery lock, for the case where the flag is not yet flipped.

    A change that renames the file, the object AND the pod label at once is invisible to
    _is_sandbox_doc(), and an invisible sandbox is indistinguishable from no sandbox: the
    checks above skip, the notes say "inert until one lands", and the pod runs with
    unrestricted egress.

    This keys on the sandbox's TELLS, not on unknown app names. The earlier "app not in
    KNOWN_APPS" trigger taxed every ordinary new service — deploy.sh aborts on exit 1 as
    "refusing to apply network-policies/", so "I added a service" arrived dressed as a policy
    breach, which is how a check gets deleted rather than fixed. The tells in sandbox_tells()
    are not conventions a new service would trip over by accident: they are forced by the
    gVisor node pool's taint and by the credential guarantee, so a workload declaring one is
    either the sandbox or something that needs the same scrutiny.
    """
    unclassified = []
    for fname in manifest_names(DEPLOY_DIR):
        for doc in load_docs(os.path.join(DEPLOY_DIR, fname)):
            if doc.get("kind") not in WORKLOAD_KINDS or _is_sandbox_doc(fname, doc):
                continue
            tells = sandbox_tells(fname, doc)
            if tells:
                unclassified.append(
                    f"{doc.get('kind')} {(doc.get('metadata') or {}).get('name')!r} in {fname} "
                    f"declares {', '.join(tells)}"
                )
    assert not unclassified, (
        "workload(s) in "
        + DEPLOY_DIR
        + " carry sandbox-only pod-spec properties but were not recognised as the sandbox: "
        + "; ".join(unclassified)
        + ". If this IS the sandbox, teach _is_sandbox_doc() its shape — otherwise every "
        "sandbox check in this harness is inert while the pod runs with unrestricted egress. "
        "If it is something else, say so here: these properties are the gVisor node pool and "
        "the non-genetics-suite KSA, and nothing ordinary needs them "
        "(docs/code-execution-security.md section 2). A workload that legitimately needs an "
        "identity of its own — url-fetcher is the one that does — is ENROLLED in "
        "DEDICATED_SA_WORKLOADS, which silences this tell only by subjecting it to the "
        "credential-free assertions below; enrolling one that cannot pass them fails louder "
        "than leaving it here."
    )


# ---------------------------------------------------------------------------
# url-fetcher. The namespace's SECOND egress policy, and the second pod whose
# NetworkPolicy is the whole reason it exists rather than an ordinary service's
# hardening. The two are opposite shapes — the sandbox reaches two in-cluster
# services and no IP range at all, the fetcher reaches every PUBLIC address on 443
# and nothing in-cluster — so nothing below is a generalisation of a sandbox check,
# and a rule that satisfies one would fail the other.
# ---------------------------------------------------------------------------


@check("label contract: the url-fetcher workload matches the policy selector")
def _():
    workloads = workloads_labelled("url-fetcher")
    named = fetcher_named_policies()
    if not workloads and not named:
        notes.append(
            "no url-fetcher workload and no policy naming it; the fetcher checks are inert. "
            "A pod that does not exist dials nothing"
        )
        return
    assert workloads, (
        "k8s/network-policies/ carries policies naming app: url-fetcher ("
        + ", ".join(sorted(p["__file__"] for p in named))
        + ") but no workload in k8s/deployments/ carries that pod label. A podSelector "
        "matching no pod is not an error, it is silent no-coverage — and if the pod is "
        "there under a different label it has unrestricted egress to this cluster"
    )
    assert named, (
        "a url-fetcher workload exists in k8s/deployments/ but no NetworkPolicy names "
        f"{FETCHER_LABELS}. There is no namespace-wide default-deny-egress, so this pod — the "
        "one that dials addresses a model chose — would reach every pod, the node, the kubelet "
        "and 169.254.169.254"
    )
    labels = pod_labels_of("url-fetcher")
    assert selects({"matchLabels": FETCHER_LABELS}, labels), (
        f"url-fetcher pod labels {labels} are not selected by {FETCHER_LABELS}"
    )
    for fname, doc in workloads:
        ports = [p.get("containerPort") for c in containers(fname, doc) for p in c.get("ports") or []]
        assert FETCHER_PORT in ports, (
            f"{fname}: the url-fetcher pod declares containerPorts {ports}, not {FETCHER_PORT}; "
            "the ingress rule allows a port nothing listens on and chat-backend reaches nothing"
        )
    for fname, doc in services_selecting("url-fetcher"):
        for port in doc["spec"].get("ports") or []:
            tp = port.get("targetPort", port.get("port"))
            assert isinstance(tp, int), (
                f"url-fetcher Service port {port} uses a named targetPort ({tp!r}) in {fname}; "
                "this harness does not resolve port names, so write it numerically"
            )
            assert tp == FETCHER_PORT, (
                f"url-fetcher Service targets pod port {tp} but the ingress rule allows "
                f"{FETCHER_PORT}; NetworkPolicy ports are pod ports, not Service ports"
            )


@check("url-fetcher ingress admits chat-backend and nobody else — the sandbox least of all")
def _():
    pols = fetcher_policies()
    if not fetcher_named_policies():
        return  # the label-contract check above owns this failure
    # the sandbox gets its own assertion before the sweep, because it is the one source whose
    # admission would not merely widen the boundary but change what this epic built: a sandbox
    # that can call the fetcher has egress to the internet through a proxy, which is option B
    # by the back door and the single worst outcome available here
    reached = rules_reaching(pols, "ingress", sandbox_pod_labels(), widen=True)
    assert not reached, (
        "the sandbox is admitted to the url-fetcher by "
        + ", ".join(f"{n} ({f})" for n, f, _ in reached)
        + " — that hands a pod with zero egress a proxy to every public address on the "
        "internet, which is exactly the design this epic rejected (epic vxtv, option B)"
    )
    admitted = {
        name
        for name, labels in sweep_labels("url-fetcher").items()
        if name != "chat-backend" and rules_reaching(pols, "ingress", labels, widen=True)
    }
    if rules_reaching(pols, "ingress", POD_LABELS["chat-backend"], widen=False):
        admitted.add("chat-backend")
    assert admitted == {"chat-backend"}, (
        f"expected {{'chat-backend'}}, got {admitted or set()}. The /fetch route carries no "
        "authentication by design — the pod holds no credential it could verify a caller "
        "against — so this ingress rule IS the access control on it"
    )


@check("url-fetcher ingress is on 8090/TCP only")
def _():
    for _n, _f, rule in rules_reaching(
        fetcher_policies(), "ingress", POD_LABELS["chat-backend"], widen=True
    ):
        ports = rule.get("ports")
        assert ports, "a portless ingress rule admits every port on the url-fetcher"
        for port in ports:
            assert port.get("port") == FETCHER_PORT, f"unexpected url-fetcher ingress port {port}"


@check("url-fetcher egress is deny-by-default")
def _():
    if not fetcher_named_policies():
        return  # the label-contract check above owns this failure
    assert any("Egress" in policy_types(p["spec"]) for p in fetcher_named_policies()), (
        "no policy selecting app: url-fetcher lists Egress in policyTypes, so its egress is "
        "unrestricted. Egress is deny-by-default for a pod only once some policy selecting it "
        "declares Egress, and this namespace has no default-deny-egress"
    )


@check("url-fetcher egress: 443 to public addresses only, the private ranges excepted, plus DNS")
def _():
    """The except blocks are the difference between containment and decoration.

    0.0.0.0/0 on 443 with no `except:` admits every pod in the cluster, the node, the kubelet
    and 169.254.169.254 — the SSRF this pod was split out of chat-backend to contain. This
    parses the rules directly rather than going through rules_reaching(), which refuses
    ipBlock peers on purpose: an ipBlock's coverage of POD IPs cannot be decided offline, and
    here the ipBlock is the subject rather than an obstacle.
    """
    if not fetcher_named_policies():
        return  # the label-contract check above owns this failure
    public = []
    dns = []
    for p in fetcher_named_policies():
        if "Egress" not in policy_types(p["spec"]):
            continue
        for rule in p["spec"].get("egress") or []:
            where = f"{p['metadata']['name']} ({p['__file__']})"
            peers = rule.get("to")
            assert peers, f"{where} has an egress rule with no 'to:' — that permits every destination"
            ports = rule.get("ports")
            assert ports, f"{where} has a portless egress rule — that permits every port"
            # HAZARD: `endPort` widens a rule into a range and this set cannot see it, so a
            # 53-or-443 entry carrying one passes every port assertion below as a single port
            portset = {(pt.get("port"), pt.get("protocol", "TCP")) for pt in ports}
            assert portset <= {(FETCHER_EGRESS_PORT, "TCP"), (DNS_PORT, "UDP"),
                               (DNS_PORT, "TCP")}, (
                f"{where}: egress rule opens {sorted(portset)}. 443 for the fetch and 53 for "
                "the name it resolves are the only ports this pod has a use for"
            )
            for peer in peers:
                if portset <= {(DNS_PORT, "UDP"), (DNS_PORT, "TCP")}:
                    dns.append((where, peer, portset))
                elif "ipBlock" in peer:
                    public.append((where, peer["ipBlock"], portset))
                else:
                    raise AssertionError(
                        f"{where}: egress peer {peer!r} on {sorted(portset)} is neither the "
                        "public-internet ipBlock nor the DNS rule. This pod is allowed exactly "
                        "two destinations; anything in-cluster reintroduces the SSRF"
                    )
    assert public, (
        "no ipBlock egress rule selects app: url-fetcher, so the pod cannot reach the public "
        "internet at all and every fetch fails — or, if a rule was removed rather than "
        "narrowed, it can reach everything"
    )
    for where, block, portset in public:
        assert portset == {(FETCHER_EGRESS_PORT, "TCP")}, (
            f"{where}: the public egress rule allows {sorted(portset)}, not 443/TCP alone. "
            "Port 443 only is the network-layer twin of the guard's https-only rule: a "
            "redirect to http then fails at the socket even if the guard were wrong about it"
        )
        assert block.get("cidr") == "0.0.0.0/0", (
            f"{where}: egress ipBlock cidr is {block.get('cidr')!r}; this harness judges the "
            "except list against 0.0.0.0/0 and cannot decide a narrower CIDR offline"
        )
        missing = FETCHER_EGRESS_EXCEPT - set(block.get("except") or [])
        assert not missing, (
            f"{where}: the 0.0.0.0/0 egress rule does not except {sorted(missing)}. Without "
            "every one of them this rule permits the pod, node and Service CIDRs, the kubelet "
            "and 169.254.169.254, and the pod is decoration rather than containment "
            "(docs/code-execution-security.md, 'The URL fetcher')"
        )
    assert dns, (
        "the url-fetcher has no DNS egress rule, so it can resolve no host name and fetches "
        "nothing. THIS IS NOT A PRECEDENT FOR THE SANDBOX: the sandbox's missing DNS rule is "
        "the design of record and the 'no DNS egress' check above still holds it"
    )
    seen = set()
    for where, peer, _ports in dns:
        assert "ipBlock" not in peer, (
            f"{where}: a 53 rule reaches {peer['ipBlock']!r} by address. The resolver is "
            "reachable by pod selector, and any CIDR here is wider than that: measured on "
            "finngenie-staging, even the link-local cache address 169.254.20.10/32 resolved "
            "nothing, because the GKE addon binds no interface on this dataplane"
        )
        ns = (peer.get("namespaceSelector") or {}).get("matchLabels") or {}
        assert ns.get("kubernetes.io/metadata.name") == "kube-system", (
            f"{where}: the DNS rule's namespaceSelector is {ns!r}. An unlabelled or empty "
            "namespaceSelector on a 53 rule admits every pod in every namespace on that port"
        )
        pod_sel = (peer.get("podSelector") or {}).get("matchLabels") or {}
        assert pod_sel.get("k8s-app") in FETCHER_DNS_PODS, (
            f"{where}: the DNS rule selects {pod_sel!r} in kube-system, which is neither of "
            f"{sorted(FETCHER_DNS_PODS)}"
        )
        assert set(pod_sel) == {"k8s-app"}, (
            f"{where}: the DNS rule's podSelector carries {sorted(pod_sel)}; only k8s-app "
            "decides which resolver this is"
        )
        seen.add(pod_sel["k8s-app"])
    assert seen == FETCHER_DNS_PODS, (
        f"the url-fetcher's DNS egress reaches {sorted(seen)}, not {sorted(FETCHER_DNS_PODS)}. "
        "Dropping node-local-dns breaks resolution outright while the addon is enabled; "
        "dropping kube-dns breaks it the moment the addon is turned off"
    )


@check("workloads with a service account of their own carry no credential")
def _():
    """What an entry in DEDICATED_SA_WORKLOADS buys, and why it is not an exemption.

    Naming a KSA other than genetics-suite is a sandbox tell. Being enrolled here silences
    that tell and replaces it with these assertions, which are the properties that made the
    dedicated identity the safe choice in the first place. A workload with an identity of its
    own and none of these properties is a workload that still fires the tell.
    """
    for app, ksa in sorted(DEDICATED_SA_WORKLOADS.items()):
        workloads = workloads_labelled(app)
        if not workloads:
            notes.append(
                f"{app} is enrolled in DEDICATED_SA_WORKLOADS but no workload carries "
                f"app: {app}; the enrolment asserts nothing until one lands"
            )
            continue
        sa_doc = service_account_doc(ksa)
        assert sa_doc is not None, (
            f"{app} names serviceAccountName: {ksa!r} but no ServiceAccount object of that "
            f"name is declared in {DEPLOY_DIR}. Kubernetes will not create it, the pod will "
            "not schedule, and nothing in this repo states what identity it has"
        )
        sa_file, sa = sa_doc
        annotations = (sa.get("metadata") or {}).get("annotations") or {}
        bound = [k for k in annotations if "gcp-service-account" in k]
        assert not bound, (
            f"ServiceAccount {ksa} ({sa_file}) carries {bound} — a Workload Identity binding. "
            f"That is the one annotation that must never appear here: it gives this pod's "
            "outbound connections a GCP identity to steal, which is the whole of what the "
            f"split from chat-backend removed ({WORKLOAD_IDENTITY_ANNOTATION})"
        )
        assert sa.get("automountServiceAccountToken") is False, (
            f"ServiceAccount {ksa} ({sa_file}) does not set automountServiceAccountToken: "
            "false"
        )
        for fname, doc in workloads:
            spec = pod_template(fname, doc).get("spec") or {}
            assert spec.get("serviceAccountName") == ksa, (
                f"{fname}: {app} runs as {spec.get('serviceAccountName')!r}, not {ksa!r}"
            )
            assert spec.get("automountServiceAccountToken") is False, (
                f"{fname}: {app} does not set automountServiceAccountToken: false on the pod "
                "spec, so a projected Kubernetes API token is mounted into a pod that makes "
                "no API calls"
            )
            creds = credential_env(fname, doc)
            assert not creds, (
                f"{fname}: a credential reaches {app}'s containers ({'; '.join(creds)}). This "
                "pod is unauthenticated on purpose — the ingress allow-list is the control — "
                "precisely because there is nothing behind the route to protect; a secret here "
                "ends that and makes the ingress rule the only thing left"
            )


@check("url-fetcher container hardening: distroless, non-root, read-only rootfs")
def _():
    for fname, doc in workloads_labelled("url-fetcher"):
        pod_sc = (pod_template(fname, doc).get("spec") or {}).get("securityContext") or {}
        for c in containers(fname, doc):
            sc = c.get("securityContext") or {}
            effective = {**pod_sc, **sc}
            where = f"{fname}: container {c.get('name')!r}"
            assert effective.get("runAsNonRoot") is True, f"{where} does not set runAsNonRoot"
            assert effective.get("runAsUser") not in (None, 0), (
                f"{where} runs as uid {effective.get('runAsUser')!r}"
            )
            assert sc.get("readOnlyRootFilesystem") is True, (
                f"{where} does not set readOnlyRootFilesystem; this image writes nothing and "
                "the bytes it fetches must never land on a filesystem"
            )
            assert sc.get("allowPrivilegeEscalation") is False, (
                f"{where} does not set allowPrivilegeEscalation: false"
            )
            assert (sc.get("capabilities") or {}).get("drop") == ["ALL"], (
                f"{where} does not drop ALL capabilities"
            )
            assert not sc.get("privileged"), f"{where} is privileged"
    # the base image, from the repo like everything else here. url-fetcher/build-checks.py is
    # the stronger statement — it asserts against the built image that no shell is present —
    # but it runs at build time only, and a base swapped here would ship on the next deploy
    # without the build ever being re-run from this checkout
    dockerfile = os.path.join(ROOT, "url-fetcher", "Dockerfile")
    if not workloads_labelled("url-fetcher"):
        return
    assert os.path.exists(dockerfile), (
        "a url-fetcher workload is deployed but url-fetcher/Dockerfile is missing, so nothing "
        "in this repo says what the image is"
    )
    final = dockerfile_base_images(dockerfile)[-1:]
    assert final and "distroless" in final[0], (
        f"url-fetcher/Dockerfile's final stage builds on {final or ['nothing']}, which is not "
        "a distroless base. A shell in this image turns a bug in the fetch route into command "
        "execution in the one pod allowed to dial the open internet"
    )


@check("SANDBOX_ENABLED is true on db-api, results-api and chat-backend once the sandbox exists")
def _():
    """The fail-closed rules are opt-in until this flag is on.

    db-api and results-api are the verifiers: both key `require_sandbox_config()` on
    `SANDBOX_ENABLED`, not on the signing key being present, so with the sandbox deployed and
    the flag still `"false"` the startup assertion never fires — and a script that simply omits
    `Authorization` lands in db-api's pre-existing unset-`INTERNAL_API_SECRET` fail-open branch,
    authorized with no `sub`, `sid` or `jti`. chat-backend is not a verifier — it mints the
    sandbox token and reads the same flag only to decide whether `run_analysis` belongs in a
    tool list — so leaving its copy `"false"` withholds the tool rather than opening a fail-open
    auth path. docs/code-execution-security.md section 4, rule 6.
    """
    if sandbox_is_deployed() and os.environ.get("ENABLE_SANDBOX", "").strip().lower() != "true":
        # PRESENT IN THE DIRECTORY IS NOT THE SAME AS APPLIED. scripts/deploy.sh skips
        # sandbox.yaml unless ENABLE_SANDBOX=true, which it derives from terraform.tfvars'
        # sandbox_pool_enabled — the same gated shape as rag-service and keycloak. So "false" on
        # the verifiers can be correct rather than drift, and failing over it would abort every
        # deploy (deploy.sh treats exit 1 as "refusing to apply network-policies/") for a
        # workload that is not running.
        #
        # BUT THE GATE DOES NOT SAY THAT. ENABLE_SANDBOX=false means "this run will not apply
        # it", not "it is not running": deploy.sh SKIPS sandbox.yaml when the gate is off, it
        # never deletes it. After one gate-on deploy, any later deploy from a worktree or with
        # terraform.tfvars unreadable (SKIP_TERRAFORM=true is the documented path for exactly
        # that) runs gate-off against a LIVE, SERVING sandbox — and relaxing on the env var
        # alone would silence the one check that stops db-api shipping SANDBOX_ENABLED=false
        # underneath it, which is the fail-open branch where a script that omits Authorization
        # is authorized with no sub/sid/jti. So the relaxation is keyed on the CLUSTER.
        #
        # This only relaxes THIS check. Every other sandbox check above is static — the label
        # contract, the egress allow-list, the tells lock — and still runs against the manifest.
        live = live_sandbox_deployment()
        assert live != "live", (
            "a sandbox Deployment is LIVE in the cluster but ENABLE_SANDBOX is not true. This "
            "deploy would leave db-api, results-api and chat-backend with "
            "SANDBOX_ENABLED=\"false\" while the "
            "sandbox serves, so a script that omits Authorization lands in db-api's unset-"
            "INTERNAL_API_SECRET fail-open branch, authorized with no sub/sid/jti; "
            "chat-backend's copy of the flag would also stay false, withholding run_analysis "
            "from every tool list even though the sandbox is live "
            "(docs/code-execution-security.md section 4, rule 6). Deploy from a checkout whose "
            "terraform.tfvars sets sandbox_pool_enabled = true, or export ENABLE_SANDBOX=true, "
            "or delete the live sandbox Deployment first. Do not relax this check."
        )
        assert live != "unknown", (
            "could not determine from the cluster whether a sandbox workload is live (kubectl "
            "is on PATH but the query failed — no context, unreachable cluster, or no "
            "permission), and ENABLE_SANDBOX is not true. Refusing to relax the SANDBOX_ENABLED "
            "check on a guess: the gate says only that THIS run will not apply the sandbox, "
            "never that one is not already running. Give this harness a working kubectl context "
            "and re-run. Running with ENABLE_SANDBOX=true is not a way around this — it makes "
            "this check strict, not relaxed."
        )
        if live == "no-kubectl":
            # Said out loud rather than passed silently. deploy.sh does everything through
            # kubectl and cannot reach this harness without it, so this is a manual offline run
            # by construction and there is no deploy to fail open.
            notes.append(
                "kubectl is not on PATH, so whether a sandbox Deployment is LIVE was not "
                "determined; the SANDBOX_ENABLED check is relaxed on the ENABLE_SANDBOX gate "
                "alone, which is sound only for an offline run (deploy.sh always has kubectl)"
            )
        else:
            notes.append(
                "a sandbox workload exists in k8s/deployments/ but ENABLE_SANDBOX is not true "
                "and no sandbox workload is live in the cluster, so deploy.sh will not apply "
                "it; the SANDBOX_ENABLED check on db-api, results-api and chat-backend is "
                "inert until the gate is "
                "on"
            )
        return
    values = sandbox_enabled_values()
    if not sandbox_is_deployed():
        # the discovery lock. Every other sandbox check is skipped by this branch, so a
        # workload this harness FAILED TO DISCOVER — renamed file, renamed object, relabelled
        # pod — looks identical to one that has not landed. The deploy-ordering contract
        # obliges a deploy to flip this flag in the commit that creates the workload, so the flag
        # being on with nothing discovered is a contradiction that catches the miss from the
        # other side, and it fails CLOSED.
        on = sorted(f for f, vs in values.items() if any(_is_on(v) for v in vs))
        assert not on, (
            f"{', '.join(on)} sets SANDBOX_ENABLED on, but no sandbox workload was discovered "
            f"in {DEPLOY_DIR}. Either the flag was flipped before the workload landed (the "
            "deploy-ordering table, docs/code-execution-security.md section 4), or the "
            "workload IS there under a file name, object name and pod label this harness does "
            "not recognise — in which case every sandbox check above skipped in silence and "
            "the sandbox has unrestricted egress. Teach _is_sandbox_doc() the new shape; do "
            "not silence this by setting the flag back to false."
        )
        notes.append(
            "no sandbox Deployment or Service found in k8s/deployments/ "
            "; the SANDBOX_ENABLED check is inert until one lands"
        )
        return
    for fname in ("db-api.yaml", "results-api.yaml", "chat-backend.yaml"):
        path = os.path.join(DEPLOY_DIR, fname)
        assert os.path.exists(path), f"{fname} is missing from {DEPLOY_DIR}"
        found = values[fname]
        assert found, (
            f"{fname} declares no SANDBOX_ENABLED env var, but a sandbox workload exists in "
            "k8s/deployments/. Unset reads as false, so " + (
                "the startup assertion that makes the sandbox credential mandatory never fires."
                if fname != "chat-backend.yaml" else
                "run_analysis is withheld from every tool list."
            )
        )
        for value in found:
            assert _is_on(value), (
                f"{fname} sets SANDBOX_ENABLED={value!r} while a sandbox workload exists in "
                "k8s/deployments/. Flip it to \"true\" in the same deploy that creates the sandbox, "
                "after create-secrets.sh has run — see the deploy-ordering table in "
                "docs/code-execution-security.md section 4."
            )


# set by the drift check when it was asked to run and could not; read by the epilogue
live_check_blocked = None


@check("live cluster: the enforced NetworkPolicies are the committed ones")
def _():
    """Opt-in on LIVE_POLICY_CHECK, because the default caller has no cluster.

    deploy.sh and build.sh run this harness on hosts that may hold no kubeconfig at all, so
    the absence of the variable must be a silent pass — a check that fails where it cannot
    apply is a check that gets removed from deploy.sh.

    The committed side is POLICIES, which is every NetworkPolicy in k8s/network-policies/ and
    not one file: `kubectl apply -f network-policies/` applies the whole directory
    unconditionally, so policies.yaml's 10 documents, keycloak-policies.yaml's, the
    per-service *-from-monitor policies in monitor-policy.yaml and sandbox-policy.yaml's two
    are all equally committed. Comparing against a subset would report a correct cluster as
    drifted, which is how this check would come to be disbelieved.
    """
    global live_check_blocked
    if not _is_on(os.environ.get("LIVE_POLICY_CHECK", "")):
        return
    state, live = live_policies()
    if state != "live":
        live_check_blocked = (
            "kubectl is not on PATH" if state == "no-kubectl"
            else "kubectl could not read the namespace's NetworkPolicies (no context, "
                 "unreachable cluster, no permission, or the namespace does not exist)"
        )
        return
    committed = {p["metadata"]["name"]: p for p in POLICIES}
    report = []
    for name in sorted(set(committed) | set(live)):
        if name not in live:
            report.append(
                f"  {name} ({committed[name]['__file__']}) is committed but the cluster is not "
                "enforcing it"
            )
        elif name not in committed:
            report.append(
                f"  {name} is enforced by the cluster but no file in k8s/network-policies/ "
                "declares it; it survives no `kubectl apply` of that directory and nobody can "
                "review what it admits"
            )
        else:
            lines = describe_drift(
                policy_shape(committed[name]["spec"]), policy_shape(live[name])
            )
            if lines:
                report.append(f"  {name} ({committed[name]['__file__']}):")
                report.extend(lines)
    assert not report, (
        "the cluster is not enforcing the committed policies. Every policy is listed, not "
        "just the first — drift is normally spread across objects:\n" + "\n".join(report)
    )


for note in notes:
    print(f"note: {note}")
if failures:
    print(f"\n{len(failures)} network-policy check(s) FAILED:\n")
    for f in failures:
        print(f"  - {f}\n")
    if live_check_blocked:
        print(f"note: the live drift check was requested but could not run: {live_check_blocked}")
    # 1, not 2, even though the live check could not run: a control IS broken, and downgrading
    # that to "harness could not run" would turn deploy.sh's abort into "applying unverified"
    sys.exit(1)
if live_check_blocked:
    # nothing offline is broken, so the only outcome left is the one the live check was asked
    # for and did not produce — and an unread cluster must never be reported as a clean one
    print(
        f"harness cannot run: LIVE_POLICY_CHECK is set but {live_check_blocked}. The committed "
        "policies are not evidence about what a cluster enforces. Give this harness a working "
        "kubectl context (KUBE_CONTEXT selects one) and re-run.",
        file=sys.stderr,
    )
    sys.exit(2)
print(f"network-policy checks passed ({len(POLICIES)} policies across {POLICY_DIR})")
