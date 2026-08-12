#!/usr/bin/env python3
"""Offline assertions about k8s/network-policies/*.yaml. No cluster, no network.

The property under test is the one that cannot be read off a single file: NetworkPolicies
in a namespace are ADDITIVE (union), so "mcp-server cannot reach the sandbox" is a
statement about *every* policy in the directory at once, and a single new rule anywhere
undoes it silently. Each check below names the control it defends in
docs/code-execution-security.md so a failure can be judged rather than deleted.

Run: python3 scripts/test-network-policies.py
Exit 0 = pass, 1 = a control is broken, 2 = the harness could not run.

Not covered here, because it needs a live cluster — see the deploy-window verification
bead: whether Dataplane V2 actually enforces egress to the link-local metadata server,
whether ClusterIP->pod translation happens before egress policy evaluation, and a real
connection attempt from the mcp-server pod to the sandbox Service.
"""

import os
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

# every pod in this namespace but the sandbox runs as this KSA (docs/code-execution-security.md
# section 2, "Service account"), which Workload Identity binds to a GSA holding BigQuery and GCS
# reader roles — the sandbox running as it would forfeit the no-usable-credential guarantee
SUITE_SERVICE_ACCOUNT = "genetics-suite"
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

    These are the tells docs/code-execution-security.md contractually obliges the sandbox to
    carry, and they are FORCED rather than conventional: GKE taints the gVisor pool
    (`sandbox.gke.io/runtime=gvisor:NoSchedule`, doc ~303-305, ~1840), so a sandbox without the
    runtimeClass and the toleration does not schedule there at all, and the doc states at ~1668
    that it is the only pod tolerating that taint. Running as `genetics-suite` would hand the
    sandbox the Workload-Identity-bound GSA the whole isolation story rests on (doc section 2).

    An ABSENT serviceAccountName is not a tell: bff, frontend, keycloak, postgres and
    oauth2-proxy declare none, and counting absence would fire on all of them.
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
    automount = spec.get("automountServiceAccountToken")
    if automount is not None and not _is_on(automount):
        tells.append(f"automountServiceAccountToken: {automount!r}")
    sa = spec.get("serviceAccountName")
    if sa is not None and sa != SUITE_SERVICE_ACCOUNT:
        tells.append(f"serviceAccountName: {sa!r}")
    return tells


def _is_sandbox_doc(fname, doc):
    """Is this doc part of the sandbox workload? A UNION of independent tells.

    Each branch is an OR, so adding one can only widen discovery — it can never make a check
    that fires today go inert. Name alone is not enough: a 4h6.7 landing the workload as
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

    The sandbox pod's full label set is not knowable from the policy directory: until 4h6.7
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


def sweep_labels():
    """Every non-sandbox pod this namespace is known to run, as {name: labels}.

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
    labels.pop("sandbox", None)
    return labels


def sandbox_policies():
    return [p for p in POLICIES if may_select(p["spec"].get("podSelector"), sandbox_pod_labels())]


def _is_on(value):
    # the services parse this with .strip().lower() against {1, true, yes}; anything else —
    # including a valueFrom with no literal value — is off
    return str(value).strip().lower() in {"1", "true", "yes"}


def sandbox_enabled_values():
    """The literal SANDBOX_ENABLED values each verifier's Deployment declares, per file."""
    values = {}
    for fname in ("db-api.yaml", "results-api.yaml"):
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
    sandbox). Widening is fail-OPEN there, and increasingly so once 4h6.7 lands and
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
        for name, labels in sweep_labels().items()
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
                    f"'{key}:' — that admits ALL peers (the genetics-results-suite-fad bug class)"
                )


@check("sandbox egress is deny-by-default")
def _():
    assert any("Egress" in policy_types(p["spec"]) for p in sandbox_policies()), (
        "no policy selecting the sandbox lists Egress in policyTypes, so its egress is "
        "unrestricted — this is the only egress policy in the namespace"
    )


@check("sandbox egress allow-list is exactly db-api:8080 and results-api:4000")
def _():
    # This pins the allow-list in BOTH directions, and the "not narrower" half is the one that
    # needs explaining. genetics-results-suite-0lf proposed dropping results-api:4000 so that
    # sandbox traffic could be forced down a path that always carries the per-execution token.
    # It cannot be dropped: the SDK's `search(rsids=...)` calls GET /v1/rsid/variants on
    # results-api, 16 of its 25 public functions are results-api-only (census in
    # genetics-results-suite-6uk), and there is no other path — the sandbox is denied
    # auth-gateway by design and auth-gateway would not validate a sandbox HS256 token anyway.
    # What makes keeping this entry safe is NOT in this file and cannot be: results-api shrinks
    # its anonymous surface to /healthz once SANDBOX_ENABLED is true, so an anonymous request
    # from this pod gets a 401 rather than an unaccounted 200. That control is a route
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


@check("no DNS egress: the design eliminates DNS rather than allowing it")
def _():
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
    # discovered, not read from a hard-coded k8s/deployments/sandbox.yaml: 4h6.7 may land the
    # workload as sandbox-deployment.yaml or split the Deployment and Service across files, and
    # a check keyed on one filename would then stay inert forever while printing a reassuring
    # note into deploy output — the exact failure mode this check is supposed to prevent
    if not sandbox_is_deployed():
        notes.append(
            "no sandbox Deployment or Service found in k8s/deployments/ "
            "(genetics-results-suite-4h6.7); the label-contract check is inert until one lands"
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

    A 4h6.7 that renames the file, the object AND the pod label at once is invisible to
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
        "(docs/code-execution-security.md section 2)."
    )


@check("SANDBOX_ENABLED is true on db-api and results-api once the sandbox exists")
def _():
    """The fail-closed rules are opt-in until this flag is on.

    Both verifiers key `require_sandbox_config()` on `SANDBOX_ENABLED`, not on the signing key
    being present, so with the sandbox deployed and the flag still `"false"` the startup
    assertion never fires — and a script that simply omits `Authorization` lands in db-api's
    pre-existing unset-`INTERNAL_API_SECRET` fail-open branch, authorized with no `sub`, `sid`
    or `jti`. docs/code-execution-security.md section 4, rule 6.
    """
    values = sandbox_enabled_values()
    if not sandbox_is_deployed():
        # the discovery lock. Every other sandbox check is skipped by this branch, so a
        # workload this harness FAILED TO DISCOVER — renamed file, renamed object, relabelled
        # pod — looks identical to one that has not landed. The deploy-ordering contract
        # obliges 4h6.7 to flip this flag in the commit that creates the workload, so the flag
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
            "(genetics-results-suite-4h6.7); the SANDBOX_ENABLED check is inert until one lands"
        )
        return
    for fname in ("db-api.yaml", "results-api.yaml"):
        path = os.path.join(DEPLOY_DIR, fname)
        assert os.path.exists(path), f"{fname} is missing from {DEPLOY_DIR}"
        found = values[fname]
        assert found, (
            f"{fname} declares no SANDBOX_ENABLED env var, but a sandbox workload exists in "
            "k8s/deployments/. Unset reads as false in both services, so the startup assertion that "
            "makes the sandbox credential mandatory never fires."
        )
        for value in found:
            assert _is_on(value), (
                f"{fname} sets SANDBOX_ENABLED={value!r} while a sandbox workload exists in "
                "k8s/deployments/. Flip it to \"true\" in the same deploy that creates the sandbox, "
                "after create-secrets.sh has run — see the deploy-ordering table in "
                "docs/code-execution-security.md section 4."
            )


for note in notes:
    print(f"note: {note}")
if failures:
    print(f"\n{len(failures)} network-policy check(s) FAILED:\n")
    for f in failures:
        print(f"  - {f}\n")
    sys.exit(1)
print(f"network-policy checks passed ({len(POLICIES)} policies across {POLICY_DIR})")
