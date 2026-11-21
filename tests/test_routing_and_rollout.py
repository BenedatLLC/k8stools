"""A Service's routing fields (#40) and a Deployment's rollout strategy (#41):
the summaries, the workload report's lines, capture and replay, and older
captures. Both cases are from k8srca's SREGym runs: internalTrafficPolicy Local
on a single-replica Service, and maxUnavailable 100% with maxSurge 0."""

import datetime
import json
from types import SimpleNamespace as NS

import pytest
from kubernetes import client as c

from k8stools import k8s_tools
from k8stools.composites import _routing_lines, workload_report
from k8stools.k8s_tools import (DeploymentSummary, NOTE_EXTERNAL_TRAFFIC_LOCAL,
                                NOTE_INTERNAL_TRAFFIC_LOCAL, NOTE_ROLLOUT_TAKES_ALL,
                                NOTE_SERVICE_ROUTING_NOT_RECORDED, NOTE_STRATEGY_NOT_RECORDED)
from k8stools.mock_state import BUILTIN_STATE_FILE, MockState, encode_model

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(k8s_tools, "_CLOCK", lambda: NOW)


def _fixture() -> dict:
    return json.loads(BUILTIN_STATE_FILE.read_text())


# --- #41: the rollout strategy -------------------------------------------------------

def _deployment(replicas=1, strategy=None, **spec):
    return c.V1Deployment(
        metadata=c.V1ObjectMeta(name="web", namespace="default",
                                creation_timestamp=NOW - datetime.timedelta(days=3)),
        spec=c.V1DeploymentSpec(replicas=replicas, strategy=strategy,
                                selector=c.V1LabelSelector(match_labels={"app": "web"}),
                                template=c.V1PodTemplateSpec(), **spec),
        status=c.V1DeploymentStatus(ready_replicas=replicas, updated_replicas=replicas,
                                    available_replicas=replicas))


def _rolling(max_unavailable, max_surge):
    return c.V1DeploymentStrategy(type="RollingUpdate", rolling_update=c.V1RollingUpdateDeployment(
        max_unavailable=max_unavailable, max_surge=max_surge))


def _deployments(monkeypatch, *deployments):
    monkeypatch.setattr(k8s_tools, "APPS_V1_API", NS(
        list_namespaced_deployment=lambda namespace: NS(items=list(deployments))))
    return k8s_tools.get_deployment_summaries("default")


def test_the_strategy_as_the_spec_gives_it(monkeypatch):
    [d] = _deployments(monkeypatch, _deployment(
        1, _rolling("100%", 0), progress_deadline_seconds=600, revision_history_limit=10))
    assert (d.strategy_type, d.max_unavailable, d.max_surge, d.min_ready_seconds,
            d.progress_deadline_seconds, d.revision_history_limit) == (
        "RollingUpdate", "100%", 0, 0, 600, 10)
    assert d.notes == [NOTE_ROLLOUT_TAKES_ALL]


def test_recreate_has_no_surge_fields_and_takes_every_replica(monkeypatch):
    [d] = _deployments(monkeypatch, _deployment(
        3, c.V1DeploymentStrategy(type="Recreate"), min_ready_seconds=30))
    assert (d.strategy_type, d.max_unavailable, d.max_surge, d.min_ready_seconds) == (
        "Recreate", None, None, 30)
    assert d.update_limits() == (3, 0) and d.notes == [NOTE_ROLLOUT_TAKES_ALL]


@pytest.mark.parametrize("replicas,unavailable,surge,limits,takes_all", [
    (4, "25%", "25%", (1, 1), False),
    (1, "25%", "25%", (0, 1), False),     # maxUnavailable rounds down, maxSurge up
    (10, "33%", "33%", (3, 4), False),
    (1, "100%", 0, (1, 0), True),         # SREGym's case
    (3, 1, 0, (1, 0), False),
    (1, 0, 0, (1, 0), True),              # both zero: the controller allows one
    (3, "0%", "0%", (1, 0), False),
    (2, "50%", 0, (1, 0), False),
    (2, 2, 1, (2, 1), True),
    (0, "100%", 0, (0, 0), False),        # scaled to zero: nothing to take down
    (0, "25%", "25%", (0, 0), False),     # MaxUnavailable() is 0 with no replicas
    (1, 2, 0, (1, 0), True),              # capped at the replica count, as the controller does
    (3, "200%", 1, (3, 1), True),
])
def test_what_an_update_may_take_down_as_the_controller_resolves_it(
        replicas, unavailable, surge, limits, takes_all):
    d = DeploymentSummary(name="web", namespace="default", total_replicas=replicas,
                          ready_replicas=replicas, up_to_date_relicas=replicas,
                          available_replicas=replicas, age=datetime.timedelta(days=1),
                          strategy_type="RollingUpdate", max_unavailable=unavailable, max_surge=surge)
    assert d.update_limits() == limits
    assert d.takes_every_replica() is takes_all
    assert (NOTE_ROLLOUT_TAKES_ALL in d.notes) is takes_all


def test_capture_keeps_counts_and_percents_apart(monkeypatch):
    [d] = _deployments(monkeypatch, _deployment(2, _rolling(1, "50%"),
                                                progress_deadline_seconds=120))
    record = encode_model(d, NOW)
    assert (record["max_unavailable"], record["max_surge"]) == (1, "50%")
    assert "notes" not in record
    data = _fixture() | {"deployments": [record], "captured_at": NOW.isoformat()}
    assert MockState(json.loads(json.dumps(data)), frozen=True).get_deployment_summaries("default") == [d]


def _fixture_report(data, name, kind="Deployment"):
    return workload_report(MockState(json.loads(json.dumps(data)), frozen=True), name, "default", kind)


def test_the_report_states_the_strategy_and_says_when_it_takes_every_replica():
    data = _fixture()
    assert _fixture_report(data, "test-deployment").rollout_strategy == (
        "RollingUpdate: maxUnavailable 25%, maxSurge 25%; of 3 replicas, at most 0 unavailable "
        "and 1 extra during an update")
    for d in data["deployments"]:
        if d["name"] == "test-deployment":
            d.update(max_unavailable="100%", max_surge=0, min_ready_seconds=10,
                     progress_deadline_seconds=300)
    report = _fixture_report(data, "test-deployment")
    assert report.rollout_strategy == (
        "RollingUpdate: maxUnavailable 100%, maxSurge 0; of 3 replicas, at most 3 unavailable "
        "and 0 extra during an update; minReadySeconds 10; progressDeadlineSeconds 300")
    assert NOTE_ROLLOUT_TAKES_ALL in report.notes


def test_an_older_capture_says_the_strategy_isnt_recorded():
    data = _fixture()
    for d in data["deployments"]:
        for key in ("strategy_type", "max_unavailable", "max_surge", "min_ready_seconds",
                    "progress_deadline_seconds", "revision_history_limit"):
            d.pop(key)
    state = MockState(json.loads(json.dumps(data)), frozen=True)
    assert all(d.notes == [NOTE_STRATEGY_NOT_RECORDED] for d in state.get_deployment_summaries())
    report = _fixture_report(data, "test-deployment")
    assert report.rollout_strategy is None
    assert NOTE_STRATEGY_NOT_RECORDED in report.notes and NOTE_ROLLOUT_TAKES_ALL not in report.notes


def test_a_statefulset_report_has_no_rollout_strategy_line():
    assert _fixture_report(_fixture(), "postgres", "StatefulSet").rollout_strategy is None


# --- #40: the routing fields ---------------------------------------------------------

def _service(name="checkout", type_="ClusterIP", **spec):
    return c.V1Service(
        metadata=c.V1ObjectMeta(name=name, namespace="default",
                                creation_timestamp=NOW - datetime.timedelta(days=1)),
        spec=c.V1ServiceSpec(type=type_, cluster_ip="10.96.0.1", selector={"app": name},
                             ports=[c.V1ServicePort(port=80, protocol="TCP")], **spec))


def _services(monkeypatch, *services):
    monkeypatch.setattr(k8s_tools, "K8S", NS(
        list_namespaced_service=lambda namespace: NS(items=list(services))))
    monkeypatch.setattr(k8s_tools, "get_endpoint_summaries", lambda namespace=None: [])
    return {s.name: s for s in k8s_tools.get_service_summaries("default")}


def test_the_routing_fields_as_the_spec_gives_them(monkeypatch):
    s = _services(monkeypatch, _service(
        internal_traffic_policy="Local", session_affinity="ClientIP",
        traffic_distribution="PreferClose", publish_not_ready_addresses=True,
        external_traffic_policy="Cluster"))["checkout"]
    assert (s.internal_traffic_policy, s.external_traffic_policy, s.session_affinity,
            s.traffic_distribution, s.publish_not_ready_addresses) == (
        "Local", None, "ClientIP", "PreferClose", True)  # external only for NodePort/LoadBalancer
    assert s.notes == [NOTE_INTERNAL_TRAFFIC_LOCAL]
    assert s.routing_changes() == ["internalTrafficPolicy Local", "sessionAffinity ClientIP",
                                   "trafficDistribution PreferClose", "publishNotReadyAddresses true"]


def test_a_headless_service_has_no_internal_policy_note(monkeypatch):
    """kube-proxy doesn't proxy a headless Service, so its internalTrafficPolicy
    drops nothing; the field is kept as the spec gives it."""
    svc = _service("kafka", internal_traffic_policy="Local", session_affinity="ClientIP",
                   publish_not_ready_addresses=True)
    svc.spec.cluster_ip = "None"
    s = _services(monkeypatch, svc)["kafka"]
    assert s.headless() and s.cluster_ip is None and s.internal_traffic_policy == "Local"
    assert NOTE_INTERNAL_TRAFFIC_LOCAL not in s.notes
    assert s.routing_changes() == ["sessionAffinity ClientIP", "publishNotReadyAddresses true"]
    lines, notes = _routing_lines([s], [])
    assert lines == ["Service/kafka: sessionAffinity ClientIP; publishNotReadyAddresses true"]
    assert NOTE_INTERNAL_TRAFFIC_LOCAL not in notes


def test_defaults_and_types(monkeypatch):
    services = _services(
        monkeypatch,
        _service("plain", internal_traffic_policy="Cluster", session_affinity="None"),
        _service("lb", "LoadBalancer", internal_traffic_policy="Cluster",
                 external_traffic_policy="Local", session_affinity="None"),
        _service("ext", "ExternalName", external_name="db.example.com", session_affinity="None"))
    plain, lb, ext = services["plain"], services["lb"], services["ext"]
    # The API omits a false publishNotReadyAddresses: live, unset is false.
    assert (plain.publish_not_ready_addresses, plain.routing_changes(), plain.notes) == (False, [], [])
    assert lb.external_traffic_policy == "Local" and lb.notes == [NOTE_EXTERNAL_TRAFFIC_LOCAL]
    assert ext.internal_traffic_policy is None and ext.routing_changes() == []


def test_capture_and_replay_give_the_same_service(monkeypatch):
    s = _services(monkeypatch, _service(internal_traffic_policy="Local",
                                        session_affinity="None"))["checkout"]
    record = encode_model(s, NOW)
    assert "notes" not in record
    data = _fixture() | {"services": [record], "captured_at": NOW.isoformat()}
    assert MockState(json.loads(json.dumps(data)), frozen=True).get_service_summaries("default") == [s]


def test_the_report_names_a_local_policy_with_the_nodes_holding_ready_endpoints():
    """SREGym's case: a caller on another node gets no answer."""
    data = _fixture()
    assert _fixture_report(data, "test-deployment").service_routing == []
    for s in data["services"]:
        if s["name"] == "test-service":
            s.update(internal_traffic_policy="Local", external_traffic_policy="Local",
                     session_affinity="ClientIP")
    for e in data["endpoints"]:
        if e["service"] == "test-service":
            e["addresses"][0]["node"] = "worker-2"
            e["addresses"][1]["ready"] = False
    report = _fixture_report(data, "test-deployment")
    assert report.service_routing == [
        "Service/test-service: internalTrafficPolicy Local; externalTrafficPolicy Local; "
        "sessionAffinity ClientIP; ready endpoints on nodes minikube, worker-2"]
    assert NOTE_INTERNAL_TRAFFIC_LOCAL in report.notes and NOTE_EXTERNAL_TRAFFIC_LOCAL in report.notes


def test_a_local_policy_with_no_ready_endpoint_says_so():
    data = _fixture()
    for s in data["services"]:
        if s["name"] == "ad":
            s["internal_traffic_policy"] = "Local"
    assert _fixture_report(data, "ad").service_routing == [
        "Service/ad: internalTrafficPolicy Local; no ready endpoints"]


def test_an_older_capture_says_the_routing_fields_arent_recorded():
    data = _fixture()
    for s in data["services"]:
        for key in ("internal_traffic_policy", "external_traffic_policy", "session_affinity",
                    "traffic_distribution", "publish_not_ready_addresses"):
            s.pop(key)
    state = MockState(json.loads(json.dumps(data)), frozen=True)
    assert all(s.notes == [NOTE_SERVICE_ROUTING_NOT_RECORDED] for s in state.get_service_summaries())
    report = _fixture_report(data, "test-deployment")
    assert report.service_routing == [] and NOTE_SERVICE_ROUTING_NOT_RECORDED in report.notes


def test_a_role_without_services_doesnt_fail_the_report():
    state = MockState(_fixture(), frozen=True)

    class Forbidden:
        def __getattr__(self, name):
            return getattr(state, name)

        def get_service_summaries(self, namespace=None):
            raise k8s_tools.K8sForbidden("Error fetching services: 403 Forbidden")

    report = workload_report(Forbidden(), "test-deployment", "default", "Deployment")
    assert report.service_routing == []
    assert any("Not readable with these credentials" in n and "Services" in n for n in report.notes)
