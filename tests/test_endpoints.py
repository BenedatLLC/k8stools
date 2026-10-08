"""Tests for get_endpoint_summaries and ServiceSummary endpoint counts (issue #15).

Built from the kubernetes client's own EndpointSlice models, so a renamed
attribute fails here rather than on a cluster.
"""

import datetime
import json
from types import SimpleNamespace as NS

import pytest
from kubernetes import client as c

from k8stools import k8s_tools, mock_tools
from k8stools.mock_state import CAPTURE_VERSION, MockState, encode_model

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC).replace(microsecond=0)


def _ep(ip, pod=None, ready=True, serving=None, terminating=None, node="minikube", conditions=True):
    return c.V1Endpoint(
        addresses=[ip], node_name=node,
        conditions=c.V1EndpointConditions(ready=ready, serving=serving, terminating=terminating)
        if conditions else None,
        target_ref=c.V1ObjectReference(kind="Pod", name=pod) if pod else None)


def _slice(name, service, endpoints, ports=((8080, "TCP"),), namespace="default", family="IPv4"):
    return c.V1EndpointSlice(
        address_type=family,
        metadata=c.V1ObjectMeta(name=name, namespace=namespace,
                                labels={"kubernetes.io/service-name": service} if service else {}),
        endpoints=endpoints,
        ports=[c.DiscoveryV1EndpointPort(port=p, protocol=proto) for p, proto in ports])


class _FakeDiscovery:
    def __init__(self, slices):
        self.slices = slices

    def list_endpoint_slice_for_all_namespaces(self):
        return NS(items=self.slices)

    def list_namespaced_endpoint_slice(self, namespace):
        return NS(items=[s for s in self.slices if s.metadata.namespace == namespace])


@pytest.fixture
def serve(monkeypatch):
    def install(*slices):
        monkeypatch.setattr(k8s_tools, "DISCOVERY_V1_API", _FakeDiscovery(list(slices)))
    return install


def test_a_service_with_ready_and_not_ready_backends(serve):
    serve(_slice("cart-abc", "cart", [_ep("10.0.0.1", "cart-1"), _ep("10.0.0.2", "cart-2", ready=False)]))
    [e] = k8s_tools.get_endpoint_summaries()
    assert (e.service, e.ready, e.not_ready, e.terminating) == ("cart", 1, 1, 0)
    assert [(a.ip, a.pod, a.ready, a.serving, a.node) for a in e.addresses] == [
        ("10.0.0.1", "cart-1", True, True, "minikube"),
        ("10.0.0.2", "cart-2", False, False, "minikube")]
    assert [(p.port, p.protocol) for p in e.ports] == [(8080, "TCP")]
    assert e.notes == []


def test_no_ready_backends_is_noted(serve):
    serve(_slice("ad-x", "ad", [_ep("10.0.0.6", "ad-1", ready=False)]),
          _slice("empty-x", "empty", []))
    ad, empty = k8s_tools.get_endpoint_summaries()
    assert ad.notes == empty.notes == [k8s_tools.NOTE_NO_READY_ENDPOINTS]
    assert (empty.ready, empty.not_ready, empty.addresses) == (0, 0, [])


def test_unset_conditions_follow_the_api_defaults(serve):
    """ready unset means ready; serving unset takes ready's value."""
    serve(_slice("s-x", "s", [_ep("10.0.0.1", "p1", ready=None), _ep("10.0.0.2", "p2", conditions=False)]))
    [e] = k8s_tools.get_endpoint_summaries()
    assert [(a.ready, a.serving, a.terminating) for a in e.addresses] == [
        (True, True, False), (True, True, False)]


def test_a_terminating_pod_is_counted_as_terminating_not_not_ready(serve):
    serve(_slice("s-x", "s", [_ep("10.0.0.1", "p1"),
                              _ep("10.0.0.2", "p2", ready=False, serving=True, terminating=True)]))
    [e] = k8s_tools.get_endpoint_summaries()
    assert (e.ready, e.not_ready, e.terminating) == (1, 0, 1)
    assert e.addresses[1].serving is True


def test_slices_merge_and_dual_stack_counts_each_pod_once(serve):
    serve(_slice("web-a", "web", [_ep("10.0.0.1", "web-1")]),
          _slice("web-b", "web", [_ep("10.0.0.2", "web-2")]),
          _slice("web-v6", "web", [_ep("fd00::1", "web-1")], family="IPv6"))
    [e] = k8s_tools.get_endpoint_summaries()
    assert e.ready == 2 and [a.pod for a in e.addresses] == ["web-1", "web-2"]


def test_a_slice_without_a_service_label_is_listed_under_its_own_name(serve):
    serve(_slice("manual-slice", None, [_ep("10.0.0.9")]))
    [e] = k8s_tools.get_endpoint_summaries()
    assert e.service == "manual-slice" and e.addresses[0].pod is None


def test_api_errors_are_wrapped(monkeypatch):
    def fail():
        raise c.ApiException(status=403, reason="Forbidden")
    monkeypatch.setattr(k8s_tools, "DISCOVERY_V1_API", NS(list_endpoint_slice_for_all_namespaces=fail))
    with pytest.raises(k8s_tools.K8sApiError, match="endpoint slices"):
        k8s_tools.get_endpoint_summaries()


# --- ServiceSummary counts --------------------------------------------------------

def _service(name, type_="ClusterIP"):
    return c.V1Service(
        metadata=c.V1ObjectMeta(name=name, namespace="default",
                                creation_timestamp=NOW - datetime.timedelta(days=1)),
        spec=c.V1ServiceSpec(type=type_, cluster_ip="10.96.0.1", selector={"app": name},
                             ports=[c.V1ServicePort(port=80, protocol="TCP")]))


def _install_services(monkeypatch, *services):
    monkeypatch.setattr(k8s_tools, "K8S", NS(list_service_for_all_namespaces=lambda: NS(items=list(services))))


def test_services_carry_their_endpoint_counts(serve, monkeypatch):
    serve(_slice("ad-x", "ad", [_ep("10.0.0.6", "ad-1", ready=False)]),
          _slice("cart-x", "cart", [_ep("10.0.0.1", "cart-1"), _ep("10.0.0.2", "cart-2")]))
    _install_services(monkeypatch, _service("ad"), _service("cart"), _service("orphan"),
                      _service("ext", type_="ExternalName"))
    counts = {s.name: (s.ready_endpoints, s.not_ready_endpoints) for s in k8s_tools.get_service_summaries()}
    # A selector Service with no slices has no backends; ExternalName has none to count.
    assert counts == {"ad": (0, 1), "cart": (2, 0), "orphan": (0, 0), "ext": (None, None)}


def test_service_counts_are_unknown_without_endpointslice_access(monkeypatch):
    """Older read-only roles may not grant EndpointSlices; services still list."""
    def fail():
        raise c.ApiException(status=403, reason="Forbidden")
    monkeypatch.setattr(k8s_tools, "DISCOVERY_V1_API", NS(list_endpoint_slice_for_all_namespaces=fail))
    _install_services(monkeypatch, _service("cart"))
    [s] = k8s_tools.get_service_summaries()
    assert (s.ready_endpoints, s.not_ready_endpoints) == (None, None)


def test_registered_everywhere():
    assert k8s_tools.get_endpoint_summaries in k8s_tools.TOOLS
    assert mock_tools.get_endpoint_summaries in mock_tools.TOOLS
    assert mock_tools.get_endpoint_summaries.__doc__ == k8s_tools.get_endpoint_summaries.__doc__


# --- capture and replay -----------------------------------------------------------------

def _capture(**keys):
    return {"version": CAPTURE_VERSION, "captured_at": NOW.isoformat(), "redacted": False, **keys}


def test_endpoints_round_trip_and_notes_are_derived():
    e = k8s_tools.EndpointSummary(service="ad", namespace="default",
                                  ports=[k8s_tools.PortInfo(port=8080, protocol="TCP")],
                                  addresses=[k8s_tools.EndpointAddress(ip="10.0.0.6", ready=False,
                                                                      serving=False, pod="ad-1")],
                                  not_ready=1)
    record = encode_model(e, NOW)
    assert "notes" not in record
    [back] = MockState(json.loads(json.dumps(_capture(endpoints=[record]))),
                       frozen=True).get_endpoint_summaries("default")
    assert back == e and back.notes == [k8s_tools.NOTE_NO_READY_ENDPOINTS]


def test_a_capture_from_before_endpoints_has_none_and_unknown_counts():
    state = MockState(_capture(services=[{
        "name": "ad", "namespace": "default", "type": "ClusterIP", "cluster_ip": "10.96.1.100",
        "external_ip": None, "ports": [{"port": 8080, "protocol": "TCP"}], "age_seconds": 60.0,
        "selector": {"app": "ad"}, "labels": {}, "annotations": {}}]), frozen=True)
    assert state.get_endpoint_summaries() == []
    [s] = state.get_service_summaries()
    assert (s.ready_endpoints, s.not_ready_endpoints) == (None, None)


# --- in the composites ----------------------------------------------------------------------

def test_the_composites_show_services_and_their_backends():
    health = mock_tools.get_namespace_health("default")
    assert health.services_without_ready_endpoints == ["Service/ad: 0 ready, 1 not ready"]
    [ad] = health.workloads
    assert ad.services == ["Service/ad: 0 ready, 1 not ready"]
    report = mock_tools.get_workload_report("test-deployment")
    assert [(e.service, e.ready) for e in report.services] == [("test-service", 3)]
