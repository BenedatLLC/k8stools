"""What a capture didn't record replays as "not recorded", never as "none" (#30).

A missing top-level list used to replay as [], so a capture made before a kind
was collected, or one that didn't collect it, told the agent none existed: on
k8srca's Scenario-33 capture, both Services have a ready backend, and replay
said there were no endpoints. A container missing from a pod's logs replayed
as '' (it wrote nothing). The rule is the same for every capture, live or
imported: replay raises K8sNotRecorded, and the composites name what wasn't
recorded in a note, or report the log as unavailable.
"""

import json

import pytest

from k8stools import k8s_tools
from k8stools.composites import namespace_health, workload_report
from k8stools.k8s_tools import K8sApiError, K8sNotRecorded
from k8stools.mock_state import _RECORDED_KINDS, BUILTIN_STATE_FILE, CAPTURE_VERSION, MockState

NOW = "2026-10-01T12:00:00+00:00"

#: The tool that reads each top-level list.
READERS = {
    "namespaces": lambda s: s.get_namespaces(),
    "nodes": lambda s: s.get_node_summaries(),
    "pods": lambda s: s.get_pod_summaries(),
    "deployments": lambda s: s.get_deployment_summaries(),
    "replicasets": lambda s: s.get_replicaset_summaries(),
    "services": lambda s: s.get_service_summaries(),
    "endpoints": lambda s: s.get_endpoint_summaries(),
    "ingresses": lambda s: s.get_ingress_summaries(),
    "networkpolicies": lambda s: s.get_networkpolicy_summaries(),
    "configmaps": lambda s: s.get_configmap_summaries(),
    "statefulsets": lambda s: s.get_statefulset_summaries(),
    "daemonsets": lambda s: s.get_daemonset_summaries(),
    "hpas": lambda s: s.get_hpa_summaries(),
    "cronjobs": lambda s: s.get_cronjob_summaries(),
    "jobs": lambda s: s.get_job_summaries(),
    "pvcs": lambda s: s.get_pvc_summaries(),
    "events": lambda s: s.get_events(),
}


def _state(**keys):
    return MockState(json.loads(json.dumps({
        "version": CAPTURE_VERSION, "captured_at": NOW, "redacted": False, **keys})),
        frozen=True)


def test_every_recorded_kind_has_a_reader():
    assert set(READERS) == set(_RECORDED_KINDS)


@pytest.mark.parametrize("key", sorted(READERS))
def test_a_missing_list_is_not_recorded_and_an_empty_one_is_none(key):
    with pytest.raises(K8sNotRecorded, match=f"no '{key}' list") as e:
        READERS[key](_state())
    assert e.value.what == _RECORDED_KINDS[key]
    assert READERS[key](_state(**{key: []})) == []


def test_the_builtin_fixture_records_every_kind():
    """--mock never says "not recorded": the fixture has every list."""
    data = json.loads(BUILTIN_STATE_FILE.read_text())
    assert set(_RECORDED_KINDS) <= set(data)


def test_a_configmap_in_a_capture_without_configmaps_is_unknown_not_missing():
    with pytest.raises(K8sNotRecorded):
        _state().get_configmap("app-config", "default")
    with pytest.raises(K8sApiError, match="not found") as e:
        _state(configmaps=[]).get_configmap("app-config", "default")
    assert not isinstance(e.value, K8sNotRecorded)


# --- logs ---------------------------------------------------------------------------

def _pod(logs=None, previous_logs=None, restarts=0):
    record = {"summary": {"name": "ad-1", "namespace": "default", "total_containers": 1,
                          "ready_containers": 1, "restarts": restarts,
                          "last_restart_seconds": None, "age_seconds": 600.0, "node": "n1",
                          "owner": "ReplicaSet/ad-6d8f9"},
              "labels": {}, "init_container_statuses": [],
              "container_statuses": [{
                  "pod_name": "ad-1", "namespace": "default", "container_name": "ad",
                  "image": "ad:1", "ready": True, "restart_count": restarts, "started": True,
                  "stop_signal": None, "state": {"started_at_offset_seconds": -300.0},
                  "last_state": None, "volume_mounts": [], "resource_requests": {},
                  "resource_limits": {}, "allocated_resources": {}}],
              "spec": {"containers": [{"name": "ad", "image": "ad:1"}]},
              "logs": logs if logs is not None else {}}
    if previous_logs is not None:
        record["previous_logs"] = previous_logs
    return record


def test_a_container_missing_from_logs_is_not_recorded():
    state = _state(pods=[_pod()])
    with pytest.raises(K8sNotRecorded, match="log of container 'ad' in pod 'ad-1' wasn't recorded"):
        state.get_logs_for_pod_and_container("ad-1", "default", "ad")
    with pytest.raises(K8sNotRecorded):
        state.get_logs_for_pod_and_container("ad-1", "default")  # the first container


def test_a_recorded_empty_log_stays_empty():
    """An empty string is what the container wrote; only re-capturing changes it."""
    assert _state(pods=[_pod(logs={"ad": ""})]).get_logs_for_pod_and_container(
        "ad-1", "default", "ad") == ""


def test_a_container_the_pod_doesnt_have_is_an_error_not_unrecorded():
    with pytest.raises(K8sApiError, match="not valid for pod") as e:
        _state(pods=[_pod()]).get_logs_for_pod_and_container("ad-1", "default", "nope")
    assert not isinstance(e.value, K8sNotRecorded)


def test_a_previous_instance_is_missing_only_when_the_container_never_restarted():
    never = _state(pods=[_pod(logs={"ad": ""})])
    with pytest.raises(K8sApiError, match="previous terminated instance .* not found") as e:
        never.get_logs_for_pod_and_container("ad-1", "default", "ad", previous=True)
    assert not isinstance(e.value, K8sNotRecorded)

    restarted = _state(pods=[_pod(logs={"ad": ""}, restarts=3)])
    with pytest.raises(K8sNotRecorded, match="previous instance of container 'ad'"):
        restarted.get_logs_for_pod_and_container("ad-1", "default", "ad", previous=True)


# --- the composites -----------------------------------------------------------------

def _scenario_33_shape(**extra):
    """One Deployment and its pod, in a capture without the kinds Scenario-33 lacks."""
    return _state(
        pods=[_pod(**extra)],
        deployments=[{"name": "ad", "namespace": "default", "total_replicas": 1,
                      "ready_replicas": 1, "up_to_date_relicas": 1, "available_replicas": 1,
                      "age_seconds": 600.0}],
        replicasets=[{"name": "ad-6d8f9", "namespace": "default", "owner_deployment": "ad",
                      "revision": 1, "desired_replicas": 1, "current_replicas": 1,
                      "ready_replicas": 1, "images": ["ad:1"], "age_seconds": 600.0}],
        statefulsets=[], jobs=[], pvcs=[], events=[])


NOTE = ("Not recorded in this capture, so unknown rather than absent: DaemonSets, "
        "HorizontalPodAutoscalers, Service endpoints, Ingresses, NetworkPolicies.")


def test_the_namespace_view_names_what_wasnt_recorded_in_one_note():
    h = namespace_health(_scenario_33_shape(logs={"ad": ""}), "default")
    assert h.healthy == ["Deployment/ad 1/1"]
    assert NOTE in h.notes


def test_the_report_names_it_too_and_an_unrecorded_log_is_unavailable():
    r = workload_report(_scenario_33_shape(), "ad", "default")
    assert NOTE in r.notes
    [tail] = r.logs
    assert (tail.container, tail.available, tail.lines) == ("ad", False, [])
    assert "wasn't recorded in this capture" in tail.reason


def test_a_report_on_a_kind_that_wasnt_recorded_says_so():
    with pytest.raises(K8sNotRecorded, match="doesn't record DaemonSets, so DaemonSet 'x'"):
        workload_report(_scenario_33_shape(), "x", "default", kind="DaemonSet")
    with pytest.raises(K8sApiError, match="StatefulSet 'x' not found"):
        workload_report(_scenario_33_shape(), "x", "default", kind="StatefulSet")


def test_live_tools_never_raise_it():
    """K8sNotRecorded is replay's: nothing in k8s_tools raises it."""
    source = open(k8s_tools.__file__).read()
    assert "raise K8sNotRecorded" not in source
