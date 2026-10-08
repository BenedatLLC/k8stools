"""Tests for get_container_metrics and get_node_metrics (issue #16): metrics.k8s.io.

Pods come from the kubernetes client's own models; metrics-server's response is
a plain dict, as CustomObjectsApi returns it.
"""

import datetime
import json
from types import SimpleNamespace as NS

import pytest
from kubernetes import client as c

from k8stools import capture, k8s_tools, mock_tools
from k8stools.k8s_tools import (ContainerUsage, K8sApiError, K8sMetricsUnavailable,
                                NOTE_NO_READING, NOTE_SHORT_WINDOW, NOTE_WORKING_SET,
                                parse_quantity)
from k8stools.mock_state import CAPTURE_VERSION, MockState, encode_model

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC).replace(microsecond=0)
MI = 2**20


def _ago(**kw):
    return datetime.datetime.now(UTC) - datetime.timedelta(**kw)


@pytest.mark.parametrize("quantity,value", [
    ("12345678n", 0.012345678), ("250m", 0.25), ("2", 2), ("1.5", 1.5), ("500u", 0.0005),
    ("128Mi", 128 * MI), ("122848Ki", 122848 * 1024), ("1Gi", 2**30), ("1G", 1e9),
    ("1e3", 1000), (None, None), ("bogus!", None)])
def test_quantities(quantity, value):
    assert parse_quantity(quantity) == pytest.approx(value) if value is not None else \
        parse_quantity(quantity) is None


@pytest.mark.parametrize("text,seconds", [
    ("15.018s", 15.018), ("1m0.002s", 60.002), ("1h2m3s", 3723), ("500ms", 0.5), (None, None)])
def test_go_durations(text, seconds):
    d = k8s_tools._go_duration(text)
    assert (d.total_seconds() == pytest.approx(seconds)) if seconds is not None else d is None


# --- the live tools, faked at the API -------------------------------------------------

def _pod(name, containers, phase="Running", namespace="default"):
    specs, statuses = [], []
    for cname, requests, limits, state, last, restarts in containers:
        specs.append(c.V1Container(name=cname, image="x:1", resources=c.V1ResourceRequirements(
            requests=requests, limits=limits)))
        statuses.append(c.V1ContainerStatus(
            name=cname, image="x:1", image_id="", ready=True, restart_count=restarts,
            state=state, last_state=last))
    return c.V1Pod(metadata=c.V1ObjectMeta(name=name, namespace=namespace),
                   spec=c.V1PodSpec(containers=specs),
                   status=c.V1PodStatus(phase=phase, container_statuses=statuses))


def _running(**ago):
    return c.V1ContainerState(running=c.V1ContainerStateRunning(started_at=_ago(**ago)))


def _oom(**ago):
    return c.V1ContainerState(terminated=c.V1ContainerStateTerminated(
        exit_code=137, reason="OOMKilled", finished_at=_ago(**ago), started_at=_ago(minutes=60)))


def _item(name, containers, namespace="default", ago=30, window="15.018s"):
    # Sampled at call time: NOW is fixed at import, long before a test in a full run.
    return {"metadata": {"name": name, "namespace": namespace},
            "timestamp": _ago(seconds=ago).isoformat().replace("+00:00", "Z"),
            "window": window,
            "containers": [{"name": n, "usage": {"cpu": cpu, "memory": mem}} for n, cpu, mem in containers]}


class _FakeMetrics:
    def __init__(self, pods=(), nodes=(), error=None):
        self.pods, self.nodes, self.error = list(pods), list(nodes), error

    def _answer(self, plural):
        if self.error:
            raise self.error
        return {"items": self.pods if plural == "pods" else self.nodes}

    def list_namespaced_custom_object(self, group, version, namespace, plural):
        assert (group, version) == ("metrics.k8s.io", "v1beta1")
        return self._answer(plural)

    def list_cluster_custom_object(self, group, version, plural):
        return self._answer(plural)


@pytest.fixture
def serve(monkeypatch):
    def install(pods=(), items=(), nodes=(), node_items=(), error=None):
        monkeypatch.setattr(k8s_tools, "_metrics_api",
                            lambda: _FakeMetrics(items, node_items, error))
        monkeypatch.setattr(k8s_tools, "K8S", NS(
            list_namespaced_pod=lambda namespace: NS(items=list(pods)),
            list_pod_for_all_namespaces=lambda: NS(items=list(pods)),
            list_node=lambda: NS(items=list(nodes))))
    return install


def test_usage_beside_requests_and_limits(serve):
    serve(pods=[_pod("ad-1", [("ad", {"memory": "300Mi", "cpu": "200m"}, {"memory": "300Mi"},
                               _running(minutes=30), None, 0)])],
          items=[_item("ad-1", [("ad", "150000000n", "153600Ki")])])
    [u] = k8s_tools.get_container_metrics("default")
    assert (u.cpu_millicores, u.cpu, u.memory_bytes, u.memory) == (150, "150m", 153600 * 1024, "150Mi")
    assert (u.memory_limit_bytes, u.memory_percent_of_limit) == (300 * MI, 50.0)
    assert (u.cpu_request_millicores, u.cpu_percent_of_request, u.cpu_limit_millicores) == (200, 75.0, None)
    assert abs(u.sampled - datetime.timedelta(seconds=30)) < datetime.timedelta(seconds=5)
    assert u.window == datetime.timedelta(seconds=15)  # whole seconds
    assert u.notes == []


def test_a_fresh_instance_after_an_oom_kill_is_the_trap(serve):
    """#16: 140Mi of 300Mi from the instance that started 58s ago, after the
    last one was OOM-killed, reads as "it can't be memory" without the note."""
    serve(pods=[_pod("ad-1", [("ad", {"memory": "300Mi"}, {"memory": "300Mi"},
                               _running(seconds=58), _oom(minutes=6), 67)])],
          items=[_item("ad-1", [("ad", "850m", "140Mi")])])
    [u] = k8s_tools.get_container_metrics()
    [note] = u.notes
    assert note.startswith("The last instance ended OOMKilled 6m")
    assert "started 58s ago" in note and NOTE_SHORT_WINDOW in note


def test_no_reading_is_a_reading_with_a_note_not_a_missing_container(serve):
    waiting = c.V1ContainerState(waiting=c.V1ContainerStateWaiting(reason="CrashLoopBackOff"))
    serve(pods=[_pod("ad-1", [("ad", {}, {"memory": "300Mi"}, waiting, _oom(minutes=1), 9)])],
          items=[])
    [u] = k8s_tools.get_container_metrics()
    assert u.memory_bytes is None and u.notes == [NOTE_NO_READING]


def test_finished_pods_are_left_out(serve):
    done = c.V1ContainerState(terminated=c.V1ContainerStateTerminated(exit_code=0, reason="Completed"))
    serve(pods=[_pod("job-1", [("job", {}, {}, done, None, 0)], phase="Succeeded")], items=[])
    assert k8s_tools.get_container_metrics() == []


def test_notes_after_an_old_ordinary_restart_and_near_the_limit(serve):
    """A node restart days ago (Error, long-running instance) gets no restart
    note; a reading at 90% or more of its limit gets the working-set note."""
    old_error = c.V1ContainerState(terminated=c.V1ContainerStateTerminated(
        exit_code=137, reason="Error", finished_at=_ago(days=2)))
    serve(pods=[_pod("kafka-1", [("kafka", {}, {"memory": "600Mi"}, _running(days=1), old_error, 15)])],
          items=[_item("kafka-1", [("kafka", "40m", "590Mi")])])
    [u] = k8s_tools.get_container_metrics()
    assert u.notes == [NOTE_WORKING_SET]


@pytest.mark.parametrize("status", [404, 503])
def test_no_metrics_server_is_a_typed_error(serve, status):
    serve(error=c.ApiException(status=status, reason="Not Found"))
    with pytest.raises(K8sMetricsUnavailable, match="metrics-server"):
        k8s_tools.get_container_metrics()
    with pytest.raises(K8sMetricsUnavailable):
        k8s_tools.get_node_metrics()


def test_a_permission_error_is_not_metrics_unavailable(serve):
    serve(error=c.ApiException(status=403, reason="Forbidden"))
    with pytest.raises(K8sApiError) as excinfo:
        k8s_tools.get_container_metrics()
    assert not isinstance(excinfo.value, K8sMetricsUnavailable)


def test_node_usage_against_allocatable(serve):
    node = c.V1Node(metadata=c.V1ObjectMeta(name="n1"),
                    status=c.V1NodeStatus(allocatable={"cpu": "4", "memory": "8Gi"}))
    serve(nodes=[node], node_items=[{"metadata": {"name": "n1"}, "window": "20s",
                                     "timestamp": NOW.isoformat().replace("+00:00", "Z"),
                                     "usage": {"cpu": "1500m", "memory": "2Gi"}}])
    [n] = k8s_tools.get_node_metrics()
    assert (n.cpu, n.cpu_percent, n.memory, n.memory_percent) == ("1500m", 37.5, "2048Mi", 25.0)


# --- capture and replay -------------------------------------------------------------------

def _capture(**keys):
    return {"version": CAPTURE_VERSION, "captured_at": NOW.isoformat(), "redacted": False, **keys}


def _usage(**kw):
    fields = dict(pod="ad-1", namespace="default", container="ad", cpu_millicores=850,
                  memory_bytes=140 * MI, cpu="850m", memory="140Mi", memory_limit_bytes=300 * MI,
                  memory_percent_of_limit=46.7, sampled=datetime.timedelta(seconds=30),
                  window=datetime.timedelta(seconds=15), started=datetime.timedelta(seconds=58),
                  restarts=67, last_termination_reason="OOMKilled",
                  last_terminated=datetime.timedelta(minutes=6))
    return ContainerUsage(**fields | kw)


def test_replay_keeps_values_ages_advance_window_stays_and_notes_follow():
    record = encode_model(_usage(), NOW)
    assert "notes" not in record
    later = datetime.datetime.now(UTC) - datetime.timedelta(days=1)
    state = MockState(json.loads(json.dumps(_capture(metrics={
        "available": True, "containers": [record], "nodes": []}))), frozen=False,
        server_start_time=later)
    [u] = state.get_container_metrics("default")
    assert (u.memory_bytes, u.memory_percent_of_limit) == (140 * MI, 46.7)   # values as sampled
    assert u.sampled == datetime.timedelta(days=1, seconds=30)               # honestly old
    assert u.window == datetime.timedelta(seconds=15)                        # an Interval
    assert "started 1d0h ago" in u.notes[0]                                  # derived on replay


@pytest.mark.parametrize("metrics,match", [
    (None, "predates resource metrics"),
    ({"available": False, "reason": "The metrics API (metrics.k8s.io) isn't available (404 Not Found): ..."},
     "isn't available"),
])
def test_the_three_cases_stay_distinct(metrics, match):
    data = _capture() if metrics is None else _capture(metrics=metrics)
    state = MockState(data, frozen=True)
    with pytest.raises(K8sMetricsUnavailable, match=match):
        state.get_container_metrics()
    with pytest.raises(K8sMetricsUnavailable, match=match):
        state.get_node_metrics()


def test_capture_records_unavailable_metrics_and_fails_on_other_errors(monkeypatch):
    redactor = capture._Redactor(False, capture.CaptureStats())

    def unavailable(ns=None):
        raise K8sMetricsUnavailable("The metrics API (metrics.k8s.io) isn't available (404 Not Found).")
    monkeypatch.setattr(k8s_tools, "get_container_metrics", unavailable)
    assert capture._capture_metrics(["default"], NOW, redactor) == {
        "available": False, "reason": "The metrics API (metrics.k8s.io) isn't available (404 Not Found)."}

    def forbidden(ns=None):
        raise K8sApiError("Error fetching pods metrics: (403)")
    monkeypatch.setattr(k8s_tools, "get_container_metrics", forbidden)
    with pytest.raises(K8sApiError):
        capture._capture_metrics(["default"], NOW, redactor)


# --- in the composites and the fixture -------------------------------------------------------

def test_the_composites_show_usage_and_its_caveats():
    mock_tools.load_mock_state()  # the built-in fixture, whatever an earlier test loaded
    health = mock_tools.get_namespace_health("default")
    [ad] = health.workloads
    assert ad.usage == ["ad: memory 140Mi of 300Mi (46.7%), cpu 850m"]
    assert any("this sample is from the current one, started 58s ago" in n for n in ad.notes)
    report = mock_tools.get_workload_report("test-deployment")
    assert sorted(u.cpu_percent_of_request for u in report.usage) == [90.0, 92.5, 93.5]


def test_composites_say_when_usage_is_unavailable():
    from k8stools.composites import namespace_health, workload_report
    state = MockState(_capture(deployments=[{
        "name": "x", "namespace": "default", "total_replicas": 0, "ready_replicas": 0,
        "up_to_date_relicas": 0, "available_replicas": 0, "age_seconds": 60.0}]), frozen=True)
    assert any("predates resource metrics" in n for n in namespace_health(state, "default").notes)
    assert any("predates resource metrics" in n for n in workload_report(state, "x", "default").notes)


def test_registered_everywhere():
    for name in ("get_container_metrics", "get_node_metrics"):
        assert getattr(k8s_tools, name) in k8s_tools.TOOLS
        assert getattr(mock_tools, name) in mock_tools.TOOLS
        assert getattr(mock_tools, name).__doc__ == getattr(k8s_tools, name).__doc__
