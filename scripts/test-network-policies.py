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

# the label contract declared by k8s/network-policies/sandbox-policy.yaml
SANDBOX_LABELS = {"app": "sandbox"}
SANDBOX_PORT = 8080

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

failures = []
notes = []


def check(name):
    def wrap(fn):
        try:
            fn()
        except AssertionError as e:
            failures.append(f"{name}: {e}")
        except Exception as e:  # a harness bug must not read as a pass
            failures.append(f"{name}: harness error: {e!r}")
        return fn
    return wrap


def load_policies():
    docs = []
    for fname in sorted(os.listdir(POLICY_DIR)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        with open(os.path.join(POLICY_DIR, fname)) as fh:
            for doc in yaml.safe_load_all(fh):
                if doc and doc.get("kind") == "NetworkPolicy":
                    doc["__file__"] = fname
                    docs.append(doc)
    return docs


POLICIES = load_policies()


def selects(selector, labels):
    """Does a podSelector select a pod carrying `labels`? Empty selector selects all."""
    if selector is None:
        return False
    if selector.get("matchExpressions"):
        raise AssertionError("matchExpressions is unhandled by this harness; extend it")
    match = selector.get("matchLabels") or {}
    return all(labels.get(k) == v for k, v in match.items())


def sandbox_policies():
    return [p for p in POLICIES if selects(p["spec"].get("podSelector"), SANDBOX_LABELS)]


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


def peer_selects(peer, labels):
    """Does one entry of a from:/to: list admit a pod carrying `labels`?

    Fail-closed: any peer shape this harness cannot decide offline is refused rather than
    guessed at. `- namespaceSelector: {}` in particular matches EVERY pod in EVERY
    namespace — `genetics` and mcp-server included — so answering "no pod match" for it
    reports a wide-open rule as closed.
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
    return selects(peer["podSelector"], labels)


def rules_reaching(policies, direction, labels):
    """Rules in `policies` whose from:/to: admits `labels` — including from-less rules,
    which admit EVERY source and are the bug class fad/k4t just closed."""
    key = "from" if direction == "ingress" else "to"
    hits = []
    for p in policies:
        if direction.capitalize() not in policy_types(p["spec"]):
            continue
        for rule in p["spec"].get(direction) or []:
            peers = rule.get(key)
            if peers is None or any(peer_selects(peer, labels) for peer in peers):
                hits.append((p["metadata"]["name"], p["__file__"], rule))
    return hits


@check("sandbox policy exists at all")
def _():
    assert sandbox_policies(), (
        "no NetworkPolicy in k8s/network-policies/ selects app: sandbox. A policy that "
        "selects nothing is not an error, it is silent no-coverage."
    )


@check("layer 2 of MCP exclusion: mcp-server cannot reach the sandbox")
def _():
    hits = rules_reaching(sandbox_policies(), "ingress", POD_LABELS["mcp-server"])
    assert not hits, (
        "mcp-server is admitted to the sandbox by "
        + ", ".join(f"{n} ({f})" for n, f, _ in hits)
        + " — docs/code-execution-security.md section 5, layer 2"
    )


@check("sandbox ingress admits chat-backend and nobody else")
def _():
    pols = sandbox_policies()
    admitted = {
        name
        for name, labels in POD_LABELS.items()
        if name != "sandbox" and rules_reaching(pols, "ingress", labels)
    }
    assert admitted == {"chat-backend"}, f"expected {{'chat-backend'}}, got {admitted or set()}"


@check("sandbox ingress is on 8080/TCP only")
def _():
    for _n, _f, rule in rules_reaching(sandbox_policies(), "ingress", POD_LABELS["chat-backend"]):
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
        hits = rules_reaching(pols, "ingress", SANDBOX_LABELS)
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
    hits = rules_reaching(sandbox_policies(), "ingress", POD_LABELS["monitor"])
    assert not hits, (
        "monitor-policy.yaml (or another file) now admits the monitor to the sandbox. Liveness "
        "is the kubelet's job and kubelet probes are exempt from NetworkPolicy on this cluster."
    )


@check("label contract: sandbox.yaml matches the policy selector")
def _():
    path = os.path.join(DEPLOY_DIR, "sandbox.yaml")
    if not os.path.exists(path):
        notes.append(
            "k8s/deployments/sandbox.yaml does not exist yet (genetics-results-suite-4h6.7); "
            "the label-contract check is inert until it does"
        )
        return
    with open(path) as fh:
        docs = [d for d in yaml.safe_load_all(fh) if d]
    pod_labels = [
        d["spec"]["template"]["metadata"].get("labels") or {}
        for d in docs
        if d.get("kind") == "Deployment"
    ]
    assert pod_labels, "sandbox.yaml declares no Deployment"
    for labels in pod_labels:
        assert selects({"matchLabels": SANDBOX_LABELS}, labels), (
            f"sandbox pod labels {labels} are not selected by {SANDBOX_LABELS}; every rule in "
            "sandbox-policy.yaml would then apply to no pod and the sandbox would run with "
            "unrestricted egress"
        )
    for d in docs:
        if d.get("kind") != "Service":
            continue
        for port in d["spec"].get("ports") or []:
            tp = port.get("targetPort", port.get("port"))
            assert isinstance(tp, int), (
                f"sandbox Service port {port} uses a named targetPort ({tp!r}), which resolves "
                "to a pod port only via the container's ports[].name in sandbox.yaml. This "
                "harness does not resolve names, so it cannot decide whether the Service "
                "targets "
                f"{SANDBOX_PORT}: extend it to look the name up in the Deployment's "
                "containerPort list, or write the Service with a numeric targetPort"
            )
            assert tp == SANDBOX_PORT, (
                f"sandbox Service targets port {port} but the ingress rule allows "
                f"{SANDBOX_PORT}; NetworkPolicy ports are pod ports, not Service ports"
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
    if not os.path.exists(os.path.join(DEPLOY_DIR, "sandbox.yaml")):
        notes.append(
            "k8s/deployments/sandbox.yaml does not exist yet (genetics-results-suite-4h6.7); "
            "the SANDBOX_ENABLED check is inert until it does"
        )
        return
    for fname in ("db-api.yaml", "results-api.yaml"):
        path = os.path.join(DEPLOY_DIR, fname)
        assert os.path.exists(path), f"{fname} is missing from {DEPLOY_DIR}"
        with open(path) as fh:
            docs = [d for d in yaml.safe_load_all(fh) if d]
        found = []
        for d in docs:
            if d.get("kind") != "Deployment":
                continue
            for container in d["spec"]["template"]["spec"].get("containers") or []:
                for env in container.get("env") or []:
                    if env.get("name") == "SANDBOX_ENABLED":
                        found.append(env.get("value"))
        assert found, (
            f"{fname} declares no SANDBOX_ENABLED env var, but k8s/deployments/sandbox.yaml "
            "exists. Unset reads as false in both services, so the startup assertion that "
            "makes the sandbox credential mandatory never fires."
        )
        for value in found:
            # the services parse this with .strip().lower() against {1, true, yes}; anything
            # else — including a valueFrom with no literal value — is off
            assert str(value).strip().lower() in {"1", "true", "yes"}, (
                f"{fname} sets SANDBOX_ENABLED={value!r} while k8s/deployments/sandbox.yaml "
                "exists. Flip it to \"true\" in the same deploy that creates the sandbox, "
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
