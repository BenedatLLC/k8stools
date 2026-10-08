"""Tests for get_daemonset_summaries and PodSummary.owner (issue #10).

A DaemonSet pod is named "<daemonset>-<5 chars>", which is indistinguishable
from many legitimate names, so grouping pods into workloads needs both the list
of DaemonSets and each pod's controlling owner. No cluster needed: these are
fake API objects.
"""

import datetime
import json
from types import SimpleNamespace

import pytest

from k8stools import k8s_tools, mock_tools
from k8stools.mock_state import CAPTURE_VERSION, MockState, encode_model

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC).replace(microsecond=0)


def _ago(**kw):
    return datetime.datetime.now(UTC) - datetime.timedelta(**kw)


# --- DaemonSets ----------------------------------------------------------------

def _daemonset(name, namespace="default", *, node_selector=None, images=("img:1",),
               strategy="RollingUpdate", **status):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace,
                                 creation_timestamp=_ago(days=167)),
        spec=SimpleNamespace(
            update_strategy=SimpleNamespace(type=strategy),
            template=SimpleNamespace(spec=SimpleNamespace(
                node_selector=node_selector,
                containers=[SimpleNamespace(name=f"c{i}", image=img)
                            for i, img in enumerate(images)]))),
        status=SimpleNamespace(**{f: status.get(f) for f in (
            "desired_number_scheduled", "current_number_scheduled", "number_ready",
            "updated_number_scheduled", "number_available", "number_misscheduled")}))


class _FakeApps:
    def __init__(self, daemonsets):
        self.daemonsets = daemonsets
        self.calls = []

    def list_daemon_set_for_all_namespaces(self):
        self.calls.append(None)
        return SimpleNamespace(items=self.daemonsets)

    def list_namespaced_daemon_set(self, namespace):
        self.calls.append(namespace)
        return SimpleNamespace(items=[d for d in self.daemonsets
                                      if d.metadata.namespace == namespace])


@pytest.fixture
def apps(monkeypatch):
    def install(*daemonsets):
        fake = _FakeApps(list(daemonsets))
        monkeypatch.setattr(k8s_tools, "APPS_V1_API", fake)
        return fake
    return install


def test_daemonset_summary_fields(apps):
    apps(_daemonset("kube-proxy", "kube-system",
                    node_selector={"kubernetes.io/os": "linux"},
                    images=("registry.k8s.io/kube-proxy:v1.35.1",),
                    desired_number_scheduled=5, current_number_scheduled=5,
                    number_ready=4, updated_number_scheduled=5,
                    number_available=4, number_misscheduled=1))
    [ds] = k8s_tools.get_daemonset_summaries()
    assert (ds.name, ds.namespace) == ("kube-proxy", "kube-system")
    assert (ds.desired_number_scheduled, ds.current_number_scheduled, ds.number_ready,
            ds.updated_number_scheduled, ds.number_available,
            ds.number_misscheduled) == (5, 5, 4, 5, 4, 1)
    assert ds.node_selector == {"kubernetes.io/os": "linux"}
    assert ds.update_strategy == "RollingUpdate"
    assert ds.images == ["registry.k8s.io/kube-proxy:v1.35.1"]
    assert abs(ds.age - datetime.timedelta(days=167)) < datetime.timedelta(seconds=5)


def test_counts_the_api_omits_are_zero(apps):
    """The API leaves a zero count out of the status entirely."""
    apps(_daemonset("agent", desired_number_scheduled=3, current_number_scheduled=3))
    [ds] = k8s_tools.get_daemonset_summaries()
    assert (ds.number_ready, ds.updated_number_scheduled, ds.number_available,
            ds.number_misscheduled) == (0, 0, 0, 0)


def test_no_node_selector_is_an_empty_map(apps):
    apps(_daemonset("agent", desired_number_scheduled=1))
    assert k8s_tools.get_daemonset_summaries()[0].node_selector == {}


def test_images_are_in_container_order(apps):
    apps(_daemonset("agent", images=("collector:0.142.0", "sidecar:2")))
    assert k8s_tools.get_daemonset_summaries()[0].images == \
        ["collector:0.142.0", "sidecar:2"]


def test_namespace_uses_the_namespaced_call(apps):
    fake = apps(_daemonset("a", "default"), _daemonset("b", "kube-system"))
    assert [d.name for d in k8s_tools.get_daemonset_summaries("kube-system")] == ["b"]
    assert [d.name for d in k8s_tools.get_daemonset_summaries()] == ["a", "b"]
    assert fake.calls == ["kube-system", None]


def test_api_errors_raise_k8s_api_error(monkeypatch):
    def fail():
        raise k8s_tools.client.ApiException(status=403, reason="Forbidden")
    monkeypatch.setattr(k8s_tools, "APPS_V1_API",
                        SimpleNamespace(list_daemon_set_for_all_namespaces=fail))
    with pytest.raises(k8s_tools.K8sApiError, match="daemon sets"):
        k8s_tools.get_daemonset_summaries()


def test_daemonset_tool_is_registered_in_both_tool_lists():
    assert k8s_tools.get_daemonset_summaries in k8s_tools.TOOLS
    assert mock_tools.get_daemonset_summaries in mock_tools.TOOLS
    assert mock_tools.get_daemonset_summaries.__doc__ == \
        k8s_tools.get_daemonset_summaries.__doc__


def test_an_empty_node_selector_says_it_is_not_every_node(apps):
    apps(_daemonset("agent", desired_number_scheduled=1),
         _daemonset("proxy", node_selector={"kubernetes.io/os": "linux"}))
    agent, proxy = k8s_tools.get_daemonset_summaries()
    assert agent.notes == [k8s_tools.NOTE_DAEMONSET_SELECTOR]
    assert "affinity" in agent.notes[0]
    assert proxy.notes == []


# --- PodSummary.owner ----------------------------------------------------------

def _ref(kind, name, controller=True):
    return SimpleNamespace(kind=kind, name=name, controller=controller)


def _pod(name, *owner_references):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace="default",
                                 creation_timestamp=_ago(days=1),
                                 owner_references=list(owner_references) or None),
        spec=SimpleNamespace(containers=[SimpleNamespace(name="c")], node_name="minikube"),
        status=SimpleNamespace(container_statuses=None, pod_ip=None))


@pytest.fixture
def pods(monkeypatch):
    def install(*pods):
        monkeypatch.setattr(k8s_tools, "K8S", SimpleNamespace(
            list_pod_for_all_namespaces=lambda: SimpleNamespace(items=list(pods))))
        return {p.name: p.owner for p in k8s_tools.get_pod_summaries()}
    return install


def test_owner_is_the_controlling_owner_as_kind_slash_name(pods):
    owners = pods(
        _pod("otel-collector-agent-fxhxp", _ref("DaemonSet", "otel-collector-agent")),
        _pod("valkey-cart-0", _ref("StatefulSet", "valkey-cart")),
        _pod("ad-7d9f8c6b5-x2k4p", _ref("ReplicaSet", "ad-7d9f8c6b5")),
        _pod("kube-apiserver-minikube", _ref("Node", "minikube")))
    assert owners == {
        "otel-collector-agent-fxhxp": "DaemonSet/otel-collector-agent",
        "valkey-cart-0": "StatefulSet/valkey-cart",
        "ad-7d9f8c6b5-x2k4p": "ReplicaSet/ad-7d9f8c6b5",   # direct owner only
        "kube-apiserver-minikube": "Node/minikube",
    }


def test_a_pod_without_owners_has_none(pods):
    assert pods(_pod("debug")) == {"debug": None}


def test_a_non_controller_owner_is_not_the_owner(pods):
    """Only one ownerReference can be the controller; others (e.g. an object
    that should merely garbage-collect the pod) are not what manages it."""
    assert pods(_pod("p", _ref("ConfigMap", "gc-anchor", controller=False),
                     _ref("Job", "backup-1"))) == {"p": "Job/backup-1"}
    assert pods(_pod("q", _ref("ConfigMap", "gc-anchor", controller=None))) == {"q": None}


def test_owner_docstring_points_from_replica_set_to_deployment():
    doc = k8s_tools.get_pod_summaries.__doc__
    assert "owner_deployment" in doc and "not by name" in doc


# --- capture and replay --------------------------------------------------------

def _capture(**keys):
    return {"version": CAPTURE_VERSION, "captured_at": NOW.isoformat(),
            "redacted": False, **keys}


def _daemonset_summary(name, namespace):
    return k8s_tools.DaemonSetSummary(
        name=name, namespace=namespace, desired_number_scheduled=1,
        current_number_scheduled=1, number_ready=1, updated_number_scheduled=1,
        number_available=1, number_misscheduled=0,
        node_selector={"kubernetes.io/os": "linux"}, update_strategy="RollingUpdate",
        images=["registry.k8s.io/kube-proxy:v1.35.1"], age=datetime.timedelta(days=167))


def test_daemonsets_round_trip_and_filter_by_namespace():
    records = [encode_model(_daemonset_summary("kube-proxy", "kube-system"), NOW),
               encode_model(_daemonset_summary("otel-collector-agent", "default"), NOW)]
    later = datetime.datetime.now(UTC) - datetime.timedelta(hours=1)
    state = MockState(json.loads(json.dumps(_capture(daemonsets=records))),
                      frozen=False, server_start_time=later)
    [ds] = state.get_daemonset_summaries("kube-system")
    assert ds == _daemonset_summary("kube-proxy", "kube-system").model_copy(
        update={"age": datetime.timedelta(days=167, hours=1)})
    assert len(state.get_daemonset_summaries()) == 2


def test_a_capture_from_before_2_3_0_has_no_daemonsets_and_no_owners():
    state = MockState(_capture(pods=[{
        "summary": {"name": "p", "namespace": "default", "total_containers": 1,
                    "ready_containers": 1, "restarts": 0, "last_restart_seconds": None,
                    "age_seconds": 60.0},
        "container_statuses": []}]), frozen=True)
    assert state.get_daemonset_summaries() == []
    assert state.get_pod_summaries()[0].owner is None


def test_pod_owner_round_trips():
    summary = k8s_tools.PodSummary(
        name="otel-collector-agent-fxhxp", namespace="default", total_containers=1,
        ready_containers=1, restarts=0, last_restart=None,
        age=datetime.timedelta(days=1), owner="DaemonSet/otel-collector-agent")
    state = MockState(json.loads(json.dumps(_capture(pods=[
        {"summary": encode_model(summary, NOW), "container_statuses": []}]))), frozen=True)
    assert state.get_pod_summaries()[0].owner == "DaemonSet/otel-collector-agent"
