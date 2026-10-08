"""Tests for get_ingress_summaries (issue #17): rules resolved against Services.

Built from the kubernetes client's own Ingress models.
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


def _backend(service=None, number=None, name=None, resource=None):
    if resource:
        kind, _, rname = resource.partition("/")
        return c.V1IngressBackend(resource=c.V1TypedLocalObjectReference(kind=kind, name=rname))
    return c.V1IngressBackend(service=c.V1IngressServiceBackend(
        name=service, port=c.V1ServiceBackendPort(number=number, name=name)))


def _ingress(name="shop", paths=(), default=None, tls=(), cls="nginx", annotations=None,
             lb=(), namespace="default", host="shop.example.com"):
    rules = [c.V1IngressRule(host=host, http=c.V1HTTPIngressRuleValue(paths=[
        c.V1HTTPIngressPath(path=p, path_type="Prefix", backend=b) for p, b in paths]))] if paths else None
    return c.V1Ingress(
        metadata=c.V1ObjectMeta(name=name, namespace=namespace, annotations=annotations,
                                creation_timestamp=NOW - datetime.timedelta(hours=2)),
        spec=c.V1IngressSpec(ingress_class_name=cls, rules=rules, default_backend=default,
                             tls=[c.V1IngressTLS(hosts=h, secret_name=s) for h, s in tls] or None),
        status=c.V1IngressStatus(load_balancer=c.V1IngressLoadBalancerStatus(
            ingress=[c.V1IngressLoadBalancerIngress(ip=a) for a in lb] or None)))


def _service(name, ports=((80, "http"),), type_="ClusterIP"):
    return c.V1Service(metadata=c.V1ObjectMeta(name=name, namespace="default"),
                       spec=c.V1ServiceSpec(type=type_, ports=[
                           c.V1ServicePort(port=p, name=n) for p, n in ports]))


@pytest.fixture
def serve(monkeypatch):
    def install(ingresses, services, ready=None):
        monkeypatch.setattr(k8s_tools, "NETWORKING_V1_API", NS(
            list_ingress_for_all_namespaces=lambda: NS(items=list(ingresses)),
            list_namespaced_ingress=lambda namespace: NS(items=list(ingresses))))
        monkeypatch.setattr(k8s_tools, "K8S", NS(
            list_service_for_all_namespaces=lambda: NS(items=list(services)),
            list_namespaced_service=lambda namespace: NS(items=list(services))))

        def endpoints(namespace=None):
            if ready is None:
                raise k8s_tools.K8sApiError("forbidden")
            return [k8s_tools.EndpointSummary(service=s, namespace="default", ready=n)
                    for s, n in ready.items()]
        monkeypatch.setattr(k8s_tools, "get_endpoint_summaries", endpoints)
    return install


def test_rules_resolve_by_port_number_and_name(serve):
    serve([_ingress(paths=[("/", _backend("cart", number=80)), ("/api", _backend("cart", name="http"))])],
          [_service("cart")], ready={"cart": 3})
    [ing] = k8s_tools.get_ingress_summaries()
    assert [(r.host, r.path, r.service, r.port, r.backend_problem, r.backend_ready) for r in ing.rules] == [
        ("shop.example.com", "/", "cart", "80", None, 3),
        ("shop.example.com", "/api", "cart", "http", None, 3)]
    assert ing.notes == []


def test_a_missing_service_or_port_is_flagged(serve):
    """The most common Ingress mistake, and visible directly."""
    serve([_ingress(paths=[("/old", _backend("legacy", number=80)),
                           ("/ads", _backend("ad", number=9555)),
                           ("/admin", _backend("ad", name="admin"))])],
          [_service("ad", ports=((8080, "grpc"),))], ready={"ad": 1})
    [ing] = k8s_tools.get_ingress_summaries()
    assert [r.backend_problem for r in ing.rules] == [
        "Service 'legacy' not found", "Service 'ad' has no port 9555", "Service 'ad' has no port admin"]
    assert all(r.backend_ready is None for r in ing.rules)
    assert ing.notes == [k8s_tools.NOTE_INGRESS_BACKEND]


def test_a_backend_with_no_ready_endpoints_is_noted(serve):
    serve([_ingress(paths=[("/ads", _backend("ad", number=80))])], [_service("ad")], ready={"ad": 0})
    [ing] = k8s_tools.get_ingress_summaries()
    assert ing.rules[0].backend_ready == 0 and ing.notes == [k8s_tools.NOTE_INGRESS_NO_READY]


def test_readiness_is_unknown_without_endpointslice_access(serve):
    serve([_ingress(paths=[("/", _backend("cart", number=80))])], [_service("cart")], ready=None)
    [ing] = k8s_tools.get_ingress_summaries()
    assert ing.rules[0].backend_ready is None and ing.rules[0].backend_problem is None


def test_default_and_resource_backends_class_tls_and_load_balancer(serve):
    serve([_ingress(default=_backend("cart", number=80), cls=None,
                    annotations={"kubernetes.io/ingress.class": "traefik"},
                    paths=[("/static", _backend(resource="StorageBucket/assets"))],
                    tls=[(["shop.example.com"], "shop-tls")], lb=["203.0.113.7"])],
          [_service("cart")], ready={"cart": 2})
    [ing] = k8s_tools.get_ingress_summaries()
    assert ing.ingress_class == "traefik"
    assert ing.default_backend.service == "cart" and ing.default_backend.backend_ready == 2
    assert ing.rules[0].resource == "StorageBucket/assets" and ing.rules[0].service is None
    assert [(t.hosts, t.secret_name) for t in ing.tls] == [(["shop.example.com"], "shop-tls")]
    assert ing.load_balancer == ["203.0.113.7"]


def test_api_errors_are_wrapped(monkeypatch):
    def fail():
        raise c.ApiException(status=403, reason="Forbidden")
    monkeypatch.setattr(k8s_tools, "NETWORKING_V1_API", NS(list_ingress_for_all_namespaces=fail))
    monkeypatch.setattr(k8s_tools, "K8S", NS(list_service_for_all_namespaces=lambda: NS(items=[])))
    with pytest.raises(k8s_tools.K8sApiError, match="ingresses"):
        k8s_tools.get_ingress_summaries()


def test_registered_everywhere():
    assert k8s_tools.get_ingress_summaries in k8s_tools.TOOLS
    assert mock_tools.get_ingress_summaries in mock_tools.TOOLS
    assert mock_tools.get_ingress_summaries.__doc__ == k8s_tools.get_ingress_summaries.__doc__


def test_round_trip_and_older_captures():
    ing = k8s_tools.IngressSummary(
        name="shop", namespace="default", rules=[k8s_tools.IngressRule(
            host="h", path="/", service="legacy", port="80", backend_problem="Service 'legacy' not found")],
        age=datetime.timedelta(hours=2))
    record = encode_model(ing, NOW)
    assert "notes" not in record
    capture = {"version": CAPTURE_VERSION, "captured_at": NOW.isoformat(), "redacted": False}
    [back] = MockState(json.loads(json.dumps(capture | {"ingresses": [record]})),
                       frozen=True).get_ingress_summaries("default")
    assert back == ing and back.notes == [k8s_tools.NOTE_INGRESS_BACKEND]
    assert MockState(capture, frozen=True).get_ingress_summaries() == []


def test_the_composites_show_rules_that_cant_reach_a_pod():
    mock_tools.load_mock_state()
    health = mock_tools.get_namespace_health("default")
    assert health.ingress_problems == [
        "Ingress/shop: shop.example.com/ads -> ad:8080 (0 ready)",
        "Ingress/shop: shop.example.com/old -> legacy:80: Service 'legacy' not found"]
    assert mock_tools.get_workload_report("test-deployment").ingresses == [
        "Ingress/shop: shop.example.com/ -> test-service:80 (3 ready)"]
