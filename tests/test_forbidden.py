"""A kind the account may not read is unknown, never fatal or "none" (#33).

A capture recorded it as not readable and carries on; replay raises
K8sNotRecorded with the reason; the composites name it in a note, live (a 403)
and replayed. Pods stay required: a capture without them is no capture.
"""

import datetime
import json
from types import SimpleNamespace as NS

import pytest
from kubernetes.client import ApiException

from k8stools import k8s_tools
from k8stools.capture import CaptureStats, capture_state
from k8stools.composites import namespace_health, workload_report
from k8stools.k8s_tools import K8sApiError, K8sForbidden, K8sMetricsUnavailable, K8sNotRecorded
from k8stools.mock_state import (BUILTIN_STATE_FILE, CAPTURE_VERSION, CAPTURE_VERSION_WITH_GAPS,
                                 CaptureFormatError, MockState)

from test_capture import _FakeCluster

POLICIES_403 = ('Error fetching network policies: 403 Forbidden: networkpolicies.networking.k8s.io '
                'is forbidden: User "system:serviceaccount:kube-system:reader" cannot list '
                'resource "networkpolicies" in API group "networking.k8s.io" in the namespace '
                '"default"')


def _api_exception(status: int, reason: str, message: str) -> ApiException:
    e = ApiException(status=status, reason=reason)
    e.body = json.dumps({"kind": "Status", "message": message})
    return e


def _raise(e):
    def read(*args, **kwargs):
        raise e
    return read


def _replay(state: dict) -> MockState:
    return MockState(json.loads(json.dumps(state)), frozen=True)


# --- live tools ---------------------------------------------------------------

@pytest.mark.parametrize("status,forbidden", [(403, True), (500, False)])
def test_a_403_raises_k8sforbidden(monkeypatch, status, forbidden):
    monkeypatch.setattr(k8s_tools, "K8S", NS())
    monkeypatch.setattr(k8s_tools, "NETWORKING_V1_API", NS(list_namespaced_network_policy=_raise(
        _api_exception(status, "Forbidden" if forbidden else "Internal Server Error",
                       "networkpolicies.networking.k8s.io is forbidden"))))
    with pytest.raises(K8sApiError) as e:
        k8s_tools.get_networkpolicy_summaries("default")
    assert isinstance(e.value, K8sForbidden) is forbidden
    assert str(e.value).startswith(f"Error fetching network policies: {status} ")


@pytest.mark.parametrize("call", [lambda: k8s_tools.get_namespaces(),
                                  lambda: k8s_tools.get_pod_events("p", "default")])
def test_readers_that_let_the_client_error_out_now_say_forbidden(monkeypatch, call):
    # Found by a real namespace-scoped role: these two raised the client's own
    # exception, which neither the capture nor the MCP server could read.
    error = _raise(_api_exception(403, "Forbidden", "namespaces is forbidden"))
    monkeypatch.setattr(k8s_tools, "K8S", NS(list_namespace=error, list_namespaced_event=error))
    with pytest.raises(K8sForbidden, match="403 Forbidden: namespaces is forbidden"):
        call()


# --- capture ------------------------------------------------------------------

@pytest.fixture
def cluster(monkeypatch):
    _FakeCluster().install(monkeypatch)
    monkeypatch.setattr(k8s_tools, "get_namespaces", lambda: [
        k8s_tools.NamespaceSummary(name=n, status="Active", age=datetime.timedelta(days=1))
        for n in ("default", "other")])
    return monkeypatch


def _policies_forbidden_in_default(ns=None):
    if ns == "default":
        raise K8sForbidden(POLICIES_403)
    return []


def test_a_forbidden_kind_is_recorded_and_the_capture_carries_on(cluster):
    cluster.setattr(k8s_tools, "get_networkpolicy_summaries", _policies_forbidden_in_default)
    stats = CaptureStats()
    state = capture_state(redact=False, stats=stats)
    # Stored without the account the 403 names: captures get published.
    assert state["unreadable"] == {"networkpolicies": {"default": (
        'Error fetching network policies: 403 Forbidden: networkpolicies.networking.k8s.io is '
        'forbidden: cannot list resource "networkpolicies" in API group "networking.k8s.io" in '
        'the namespace "default"')}}
    assert state["configmaps"]  # the rest was read
    report = stats.report()
    assert "NOT READABLE:           1 kind(s); replay says not recorded" in report
    assert "networkpolicies: list networkpolicies in API group networking.k8s.io (default)" in report


def test_replay_says_why_in_that_namespace_only(cluster):
    cluster.setattr(k8s_tools, "get_networkpolicy_summaries", _policies_forbidden_in_default)
    state = _replay(capture_state(redact=False))
    with pytest.raises(K8sNotRecorded) as e:
        state.get_networkpolicy_summaries("default")
    assert e.value.what == "NetworkPolicies"
    assert e.value.reason == "not readable when captured"
    assert "in namespace 'default': they weren't readable when captured (Error fetching " \
           "network policies: 403 Forbidden" in str(e.value)
    assert state.get_networkpolicy_summaries("other") == []
    with pytest.raises(K8sNotRecorded):  # every namespace includes the one it couldn't read
        state.get_networkpolicy_summaries()


def test_a_capture_with_nothing_unreadable_has_no_unreadable_key(cluster):
    state = capture_state(redact=False)
    assert "unreadable" not in state
    assert state["version"] == CAPTURE_VERSION  # readable by every k8stools


def test_a_capture_with_gaps_is_one_older_readers_refuse(cluster):
    # A reader before 4.5.0 ignores `unreadable` and would answer "none".
    cluster.setattr(k8s_tools, "get_networkpolicy_summaries", _policies_forbidden_in_default)
    state = capture_state(redact=False)
    assert state["version"] == CAPTURE_VERSION_WITH_GAPS == "2"
    _replay(state)  # this one reads both
    with pytest.raises(CaptureFormatError, match="reads versions '1' and '2'"):
        MockState({**state, "version": "3"})


def test_forbidden_metrics_and_custom_resources_are_stored_without_the_account(cluster):
    forbidden = K8sForbidden('Error fetching x: 403 Forbidden: x is forbidden: User '
                             '"system:serviceaccount:kube-system:reader" cannot list resource "x"')
    cluster.setattr(k8s_tools, "get_container_metrics", _raise(forbidden))
    cluster.setattr(k8s_tools, "get_custom_resource_definitions", _raise(forbidden))
    stats = CaptureStats()
    state = capture_state(redact=False, stats=stats)
    assert "serviceaccount" not in json.dumps(state)
    assert state["metrics"]["reason"].endswith('x is forbidden: cannot list resource "x"')
    assert set(stats.forbidden_other) == {"metrics", "custom resources"}


def test_pods_are_required(cluster):
    cluster.setattr(k8s_tools, "get_pod_summaries", _raise(K8sForbidden("pods forbidden")))
    with pytest.raises(K8sForbidden):
        capture_state(redact=False)


def test_namespaces_are_required_only_to_walk_them(cluster):
    cluster.setattr(k8s_tools, "get_namespaces", _raise(K8sForbidden("namespaces forbidden")))
    with pytest.raises(K8sForbidden):
        capture_state(redact=False)
    state = capture_state(namespaces=["default"], redact=False)
    assert state["unreadable"] == {"namespaces": {"": "namespaces forbidden"}}
    with pytest.raises(K8sNotRecorded, match="namespaces: they weren't readable"):
        _replay(state).get_namespaces()


def test_nodes_are_optional(cluster):
    cluster.setattr(k8s_tools, "get_node_summaries", _raise(K8sForbidden("nodes forbidden")))
    state = capture_state(redact=False)
    assert state["unreadable"] == {"nodes": {"": "nodes forbidden"}}
    with pytest.raises(K8sNotRecorded):
        _replay(state).get_node_summaries()


def test_a_404_for_a_kind_the_cluster_does_not_serve_is_unreadable(cluster):
    error = K8sApiError("Error fetching horizontal pod autoscalers: 404 Not Found")
    error.__cause__ = ApiException(status=404, reason="Not Found")
    cluster.setattr(k8s_tools, "get_hpa_summaries", _raise(error))
    assert set(capture_state(redact=False)["unreadable"]["hpas"]) == {"default", "other"}


def test_any_other_failure_still_fails_the_capture(cluster):
    cluster.setattr(k8s_tools, "get_ingress_summaries",
                    _raise(K8sApiError("Error fetching ingresses: 500 Internal Server Error")))
    with pytest.raises(K8sApiError, match="500"):
        capture_state(redact=False)


def test_forbidden_events_replay_as_not_recorded(cluster):
    cluster.setattr(k8s_tools, "get_events", _raise(K8sForbidden("events forbidden")))
    state = _replay(capture_state(redact=False))
    with pytest.raises(K8sNotRecorded, match="events in namespace"):
        state.get_events("default")
    with pytest.raises(K8sNotRecorded):
        state.get_pod_events("p", "other")


def test_an_unreadable_history_is_not_rebuilt_on_replay(cluster):
    cluster.setattr(k8s_tools, "get_deployment_summaries", lambda ns=None: [
        k8s_tools.DeploymentSummary(name="web", namespace=ns, ready_replicas=1, total_replicas=1,
                                    up_to_date_relicas=1, available_replicas=1,
                                    age=datetime.timedelta(days=1))])
    cluster.setattr(k8s_tools, "get_workload_history",
                    _raise(K8sForbidden("controllerrevisions forbidden")))
    state = capture_state(namespaces=["default"], redact=False)
    assert state["unreadable"] == {"workload_histories": {"default": "controllerrevisions forbidden"}}
    assert state["workload_histories"] == []
    with pytest.raises(K8sNotRecorded, match="workload histories in namespace 'default'"):
        _replay(state).get_workload_history("web", "default")


def test_a_workload_deleted_mid_capture_is_skipped_not_unreadable(cluster):
    # Listed, then gone before its history was read: the 404 is that object's,
    # not its kind's, so nothing is recorded as unreadable and nothing to grant.
    cluster.setattr(k8s_tools, "get_deployment_summaries", lambda ns=None: [
        k8s_tools.DeploymentSummary(name=n, namespace=ns, ready_replicas=1, total_replicas=1,
                                    up_to_date_relicas=1, available_replicas=1,
                                    age=datetime.timedelta(days=1)) for n in ("gone", "web")])

    def history(name, namespace, kind):
        if name == "gone":
            error = K8sApiError(f"Deployment 'gone' not found in namespace '{namespace}'.")
            error.__cause__ = ApiException(status=404, reason="Not Found")
            raise error
        return k8s_tools.WorkloadHistory(kind=kind, name=name, namespace=namespace,
                                         revisions=[], config=[], complete=True, limits=[])
    cluster.setattr(k8s_tools, "get_workload_history", history)
    stats = CaptureStats()
    state = capture_state(namespaces=["default"], redact=False, stats=stats)
    assert "unreadable" not in state and "NOT READABLE" not in stats.report()
    assert [h["name"] for h in state["workload_histories"]] == ["web"]


def test_forbidden_metrics_are_recorded_unavailable(cluster):
    cluster.setattr(k8s_tools, "get_container_metrics", _raise(K8sForbidden("metrics forbidden")))
    state = capture_state(redact=False)
    assert state["metrics"] == {"available": False, "reason": "metrics forbidden"}
    with pytest.raises(K8sMetricsUnavailable, match="metrics forbidden"):
        _replay(state).get_container_metrics("default")


# --- composites -----------------------------------------------------------------

def test_replayed_composites_name_what_was_not_readable():
    data = json.loads(BUILTIN_STATE_FILE.read_text())
    data["unreadable"] = {"hpas": {"default": "403 Forbidden"}}
    notes = namespace_health(MockState(data, frozen=True), "default").notes
    assert ("Not recorded in this capture, so unknown rather than absent: "
            "HorizontalPodAutoscalers (not readable when captured).") in notes


def _live(**forbidden) -> MockState:
    """The built-in fixture standing in for a live cluster whose role can't
    read the named tools' kinds."""
    state = MockState.from_builtin(frozen=True)
    for name, message in forbidden.items():
        def read(*args, _message=message, **kwargs):
            raise K8sForbidden(_message)
        read.__name__ = name
        setattr(state, name, read)
    return state


def test_live_composites_carry_on_past_a_403():
    api = _live(get_hpa_summaries="hpas forbidden", get_endpoint_summaries="endpoints forbidden",
                get_container_metrics="metrics forbidden")
    health = namespace_health(api, "default")
    assert "Usage unavailable: metrics forbidden" in health.notes
    assert ("Not readable with these credentials (403 Forbidden), so unknown rather than "
            "absent: HorizontalPodAutoscalers, Service endpoints.") in health.notes
    assert not any(n.startswith("Not recorded") for n in health.notes)
    report = workload_report(api, "ad", "default", "Deployment")
    assert any(n.startswith("Not readable with these credentials") for n in report.notes)


def test_a_workload_whose_kind_is_forbidden_is_unknown_not_missing():
    api = _live(get_deployment_summaries="deployments forbidden")
    with pytest.raises(K8sForbidden, match="Deployment 'ad' is unknown, not absent"):
        workload_report(api, "ad", "default", "Deployment")
    with pytest.raises(K8sApiError, match=r"\(Deployments unknown: not recorded or not readable\)"):
        workload_report(api, "ad", "default")
