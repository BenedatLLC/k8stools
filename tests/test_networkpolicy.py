"""Tests for get_networkpolicy_summaries and the composites' policy lines (#31).

A policy's name says nothing reliable about what it does (ITBench Scenario-31's
"frontend-block-all-ports" allows everything), so these check the stated effect
against the API's rules, including the ones most often misread: `[{}]` against
`[{podSelector: {}}]`, a peer with two selectors (both) against two peers
(either), and the default policyTypes. Built from the kubernetes client's own
models.
"""

import datetime
import json
from types import SimpleNamespace as NS

import pytest
from kubernetes import client as c

from k8stools import k8s_tools, mock_tools
from k8stools.composites import namespace_health, workload_report
from k8stools.mock_state import BUILTIN_STATE_FILE, CAPTURE_VERSION, MockState, encode_model

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC).replace(microsecond=0)
FRONTEND = {"app.kubernetes.io/component": "frontend", "app.kubernetes.io/name": "frontend",
            "opentelemetry.io/name": "frontend"}


def _pod(name, labels, host_network=None, namespace="otel-demo"):
    return c.V1Pod(metadata=c.V1ObjectMeta(name=name, namespace=namespace, labels=labels),
                   spec=c.V1PodSpec(containers=[], host_network=host_network))


PODS = [_pod("frontend-675fd7b5c5-8qrfh", FRONTEND | {"pod-template-hash": "675fd7b5c5"}),
        _pod("frontend-proxy-6b4d584985-p6mgt", {"app.kubernetes.io/name": "frontend-proxy"}),
        _pod("node-agent", {"app.kubernetes.io/name": "frontend"}, host_network=True)]


def _policy(spec, name="p"):
    return c.V1NetworkPolicy(
        metadata=c.V1ObjectMeta(name=name, namespace="otel-demo",
                                creation_timestamp=NOW - datetime.timedelta(minutes=5)),
        spec=spec)


def _selector(labels=None, expressions=()):
    return c.V1LabelSelector(match_labels=labels, match_expressions=[
        c.V1LabelSelectorRequirement(key=k, operator=o, values=v) for k, o, v in expressions] or None)


@pytest.fixture
def serve(monkeypatch):
    def install(*policies, pods=PODS):
        monkeypatch.setattr(k8s_tools, "NETWORKING_V1_API", NS(
            list_network_policy_for_all_namespaces=lambda: NS(items=list(policies)),
            list_namespaced_network_policy=lambda namespace: NS(items=list(policies))))
        monkeypatch.setattr(k8s_tools, "K8S", NS(
            list_pod_for_all_namespaces=lambda: NS(items=list(pods)),
            list_namespaced_pod=lambda namespace: NS(items=list(pods))))
        return k8s_tools.get_networkpolicy_summaries("otel-demo")
    return install


# --- what a policy allows ---------------------------------------------------------------

def test_scenario_31s_block_all_ports_allows_all_ingress(serve):
    # The policy as ITBench created it: one rule with no from and no ports.
    [p] = serve(_policy(c.V1NetworkPolicySpec(
        pod_selector=_selector(FRONTEND), policy_types=["Ingress"],
        ingress=[c.V1NetworkPolicyIngressRule()]), name="frontend-block-all-ports"))
    assert p.ingress_effect == "allows all ingress"
    assert p.egress_effect is None
    assert p.selected_pods == ["frontend-675fd7b5c5-8qrfh"]
    assert p.notes == []


def test_no_rules_denies_and_policy_types_default(serve):
    deny, both = serve(
        # No policyTypes and no rules: Ingress is restricted, by default.
        _policy(c.V1NetworkPolicySpec(pod_selector=_selector()), name="deny"),
        # Egress rules make Egress restricted too.
        _policy(c.V1NetworkPolicySpec(pod_selector=_selector(), egress=[
            c.V1NetworkPolicyEgressRule(ports=[c.V1NetworkPolicyPort(port=443)])]), name="both"))
    assert (deny.policy_types, deny.ingress_effect, deny.egress_effect) == (
        ["Ingress"], "denies all ingress", None)
    assert both.policy_types == ["Ingress", "Egress"]
    assert both.ingress_effect == "denies all ingress"
    assert both.egress_effect == "allows egress only to any destination on TCP 443"
    # An empty pod selector selects every pod, except those on the host network.
    assert deny.pod_selector == ""
    assert deny.selected_pods == ["frontend-675fd7b5c5-8qrfh", "frontend-proxy-6b4d584985-p6mgt"]


def test_every_pod_in_this_namespace_is_not_everyone(serve):
    [p] = serve(_policy(c.V1NetworkPolicySpec(pod_selector=_selector(FRONTEND), ingress=[
        c.V1NetworkPolicyIngressRule(_from=[c.V1NetworkPolicyPeer(pod_selector=_selector())])])))
    assert p.ingress_effect == "allows ingress only from every pod in this namespace on any port"


def test_one_peer_with_two_selectors_is_both_and_two_peers_is_either(serve):
    ns = _selector({"team": "shop"})
    pods = _selector({"app": "proxy"})
    both, either = serve(
        _policy(c.V1NetworkPolicySpec(pod_selector=_selector(), ingress=[
            c.V1NetworkPolicyIngressRule(_from=[
                c.V1NetworkPolicyPeer(namespace_selector=ns, pod_selector=pods)])]), name="and"),
        _policy(c.V1NetworkPolicySpec(pod_selector=_selector(), ingress=[
            c.V1NetworkPolicyIngressRule(_from=[c.V1NetworkPolicyPeer(namespace_selector=ns),
                                                c.V1NetworkPolicyPeer(pod_selector=pods)])]),
                name="or"))
    assert both.ingress_effect == \
        "allows ingress only from pods with app=proxy in namespaces with team=shop on any port"
    assert either.ingress_effect == ("allows ingress only from every pod in namespaces with "
                                     "team=shop or pods with app=proxy in this namespace on any port")


def test_namespaces_by_name_ip_blocks_and_ports(serve):
    [p] = serve(_policy(c.V1NetworkPolicySpec(pod_selector=_selector(), ingress=[
        c.V1NetworkPolicyIngressRule(
            _from=[c.V1NetworkPolicyPeer(namespace_selector=_selector(
                {"kubernetes.io/metadata.name": "ingress-nginx"}))],
            ports=[c.V1NetworkPolicyPort(port="http"),
                   c.V1NetworkPolicyPort(port=8000, end_port=8100, protocol="TCP")]),
        c.V1NetworkPolicyIngressRule(
            _from=[c.V1NetworkPolicyPeer(ip_block=c.V1IPBlock(cidr="10.0.0.0/8",
                                                              _except=["10.1.0.0/16"]))],
            ports=[c.V1NetworkPolicyPort(protocol="UDP")])])))
    assert p.ingress_effect == (
        "allows ingress only from every pod in namespace ingress-nginx on TCP port 'http', "
        "TCP 8000-8100; or from addresses in 10.0.0.0/8 except 10.1.0.0/16 on every UDP port")


def test_label_selector_expressions(serve):
    [p] = serve(_policy(c.V1NetworkPolicySpec(pod_selector=_selector(expressions=[
        ("app.kubernetes.io/name", "In", ["frontend", "cart"]), ("pod-template-hash", "Exists", None),
        ("canary", "DoesNotExist", None)]))))
    assert p.pod_selector == ("app.kubernetes.io/name in (frontend,cart),pod-template-hash,"
                              "!canary")
    assert p.selected_pods == ["frontend-675fd7b5c5-8qrfh"]


def test_egress_without_dns(serve):
    def egress(*ports):
        return _policy(c.V1NetworkPolicySpec(pod_selector=_selector(), policy_types=["Egress"],
                                             egress=[c.V1NetworkPolicyEgressRule(ports=list(ports))]))
    [no_dns] = serve(egress(c.V1NetworkPolicyPort(port=443)))
    assert k8s_tools.NOTE_NETPOL_NO_DNS in no_dns.notes
    [dns] = serve(egress(c.V1NetworkPolicyPort(port=53, protocol="UDP")))
    [named] = serve(egress(c.V1NetworkPolicyPort(port="dns", protocol="UDP")))
    [denied] = serve(_policy(c.V1NetworkPolicySpec(pod_selector=_selector(), policy_types=["Egress"])))
    assert k8s_tools.NOTE_NETPOL_NO_DNS not in dns.notes + named.notes  # a name could be 53
    assert denied.egress_effect == "denies all egress"
    assert k8s_tools.NOTE_NETPOL_NO_DNS in denied.notes


def test_a_policy_that_selects_no_pod_says_so(serve):
    [p] = serve(_policy(c.V1NetworkPolicySpec(pod_selector=_selector({"app": "gone"}))))
    assert p.selected_pods == [] and p.notes == [k8s_tools.NOTE_NETPOL_SELECTS_NONE]


def test_api_errors_are_wrapped(monkeypatch):
    def fail(namespace):
        raise c.ApiException(status=403, reason="Forbidden")
    monkeypatch.setattr(k8s_tools, "NETWORKING_V1_API", NS(list_namespaced_network_policy=fail))
    monkeypatch.setattr(k8s_tools, "K8S", NS())
    with pytest.raises(k8s_tools.K8sApiError, match="network policies"):
        k8s_tools.get_networkpolicy_summaries("otel-demo")


def test_registered_everywhere():
    assert k8s_tools.get_networkpolicy_summaries in k8s_tools.TOOLS
    assert mock_tools.get_networkpolicy_summaries in mock_tools.TOOLS
    assert mock_tools.get_networkpolicy_summaries.__doc__ == \
        k8s_tools.get_networkpolicy_summaries.__doc__


# --- capture and replay -----------------------------------------------------------------

def _summary(name, selected, ingress=None, egress=None, types=("Ingress",)):
    return k8s_tools.NetworkPolicySummary(
        name=name, namespace="default", pod_selector="app=x", policy_types=list(types),
        ingress=ingress or [], egress=egress or [], selected_pods=selected,
        age=datetime.timedelta(minutes=5))


ALL = k8s_tools.NetworkPolicyRule()


def test_round_trip_derives_the_effects_and_older_captures_are_not_recorded():
    policy = _summary("allow", ["a"], ingress=[ALL])
    record = encode_model(policy, NOW)
    assert not {"notes", "ingress_effect", "egress_effect"} & set(record)
    capture = {"version": CAPTURE_VERSION, "captured_at": NOW.isoformat(), "redacted": False}
    [back] = MockState(json.loads(json.dumps(capture | {"networkpolicies": [record]})),
                       frozen=True).get_networkpolicy_summaries("default")
    assert back == policy and back.ingress_effect == "allows all ingress"
    with pytest.raises(k8s_tools.K8sNotRecorded, match="doesn't record NetworkPolicies"):
        MockState(capture, frozen=True).get_networkpolicy_summaries()


# --- the composites ---------------------------------------------------------------------

TEST_PODS = ["test-deployment-7d4b9c85f-4kq9z", "test-deployment-7d4b9c85f-m7xpw",
             "test-deployment-7d4b9c85f-tb2ns"]


def _fixture(*policies, crds=()):
    data = json.loads(BUILTIN_STATE_FILE.read_text())
    data["networkpolicies"] = [encode_model(p, NOW) for p in policies]
    data["captured_at"] = NOW.isoformat()
    for plural, group, kind in crds:
        data["custom_resources"]["definitions"].append(encode_model(k8s_tools.CrdSummary(
            name=f"{plural}.{group}", group=group, kind=kind, plural=plural, scope="Namespaced",
            age=datetime.timedelta(days=1)), NOW))
    return MockState(json.loads(json.dumps(data)), frozen=True)


def test_no_policy_selecting_a_workload_rules_them_out_in_one_line():
    report = workload_report(_fixture(), "test-deployment", "default")
    assert report.network_policies == ["no NetworkPolicy (networking.k8s.io) selects these pods"]
    assert k8s_tools.NOTE_NETWORK_POLICIES not in report.notes
    assert namespace_health(_fixture(), "default").network_policies == []


def test_policies_selecting_a_workload_add_up_per_direction():
    from_proxy = k8s_tools.NetworkPolicyRule(
        peers=[k8s_tools.NetworkPolicyPeer(pod_selector="app=frontend-proxy")],
        ports=[k8s_tools.NetworkPolicyPort(port="8080")])
    state = _fixture(
        _summary("deny-all", TEST_PODS + ["ad-647b4947cc-s5mpm"], types=("Ingress", "Egress")),
        _summary("from-proxy", TEST_PODS[:2], ingress=[from_proxy]))
    report = workload_report(state, "test-deployment", "default")
    assert report.network_policies == [
        "ingress: allows ingress only from pods with app=frontend-proxy in this namespace on "
        "TCP 8080 (NetworkPolicy/deny-all, NetworkPolicy/from-proxy)",
        "egress: denies all egress; none allows port 53, so these pods can't look up names "
        "(DNS) (NetworkPolicy/deny-all)",
        "NetworkPolicy/from-proxy selects 2 of these 3 pods"]
    assert k8s_tools.NOTE_NETWORK_POLICIES in report.notes
    assert namespace_health(state, "default").network_policies == [
        "NetworkPolicy/deny-all selects 4 pods (test-deployment-7d4b9c85f-4kq9z, "
        "test-deployment-7d4b9c85f-m7xpw, test-deployment-7d4b9c85f-tb2ns, +1): "
        "denies all ingress; denies all egress",
        "NetworkPolicy/from-proxy selects 2 pods (test-deployment-7d4b9c85f-4kq9z, "
        "test-deployment-7d4b9c85f-m7xpw): allows ingress only from pods with "
        "app=frontend-proxy in this namespace on TCP 8080"]


def test_one_allow_all_policy_outweighs_the_rest_and_egress_unrestricted_says_so():
    state = _fixture(_summary("deny", TEST_PODS), _summary("allow", TEST_PODS, ingress=[ALL]))
    assert workload_report(state, "test-deployment", "default").network_policies == [
        "ingress: allows all ingress (NetworkPolicy/deny, NetworkPolicy/allow)",
        "egress: not restricted (no NetworkPolicy selects these pods for egress)"]


def test_policy_kinds_from_custom_resources_are_named():
    state = _fixture(crds=[("ciliumnetworkpolicies", "cilium.io", "CiliumNetworkPolicy")])
    assert ("Policy kinds from custom resources are installed: CiliumNetworkPolicy "
            "(cilium.io). Their policies can also apply to these pods, and aren't shown."
            in workload_report(state, "test-deployment", "default").notes)


def test_not_recorded_or_unreadable_policies_are_never_none():
    data = json.loads(BUILTIN_STATE_FILE.read_text())
    del data["networkpolicies"]
    old = MockState(data, frozen=True)
    report = workload_report(old, "test-deployment", "default")
    assert report.network_policies == []
    assert any(n.startswith("Not recorded") and "NetworkPolicies" in n for n in report.notes)

    class Forbidden:
        def __init__(self, state):
            self._state = state

        def __getattr__(self, name):
            return getattr(self._state, name)

        def get_networkpolicy_summaries(self, namespace=None):
            raise self.error

    # A 403 is a kind these credentials can't read (#33); any other failure says why.
    note = ("Not readable with these credentials (403 Forbidden), so unknown rather "
            "than absent: NetworkPolicies.")
    Forbidden.error = k8s_tools.K8sForbidden("Error fetching network policies: 403 Forbidden")
    report = workload_report(Forbidden(_fixture()), "test-deployment", "default")
    assert report.network_policies == []
    assert note in report.notes
    assert note in namespace_health(Forbidden(_fixture()), "default").notes
    Forbidden.error = k8s_tools.K8sApiError("Error fetching network policies: 500 Internal Server Error")
    assert "NetworkPolicies unavailable: Error fetching network policies: 500 Internal Server " \
        "Error" in workload_report(Forbidden(_fixture()), "test-deployment", "default").notes
