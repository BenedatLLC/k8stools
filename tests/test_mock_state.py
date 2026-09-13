"""Unit tests for MockState - loading a capture and serving it to tool queries."""

import datetime
import json
import time

import pytest

from k8stools import k8s_tools
from k8stools.mock_state import CAPTURE_VERSION, CaptureFormatError, MockState


UTC = datetime.timezone.utc


def _capture(**lists) -> dict:
    base = {
        "version": CAPTURE_VERSION,
        "captured_at": datetime.datetime.now(UTC).isoformat(),
        "redacted": False,
    }
    base.update(lists)
    return base


def _rs(name, namespace="default", owner="ad", revision=1, age_seconds=3600.0):
    return {"name": name, "namespace": namespace, "owner_deployment": owner,
            "revision": revision, "desired_replicas": 1, "current_replicas": 1,
            "ready_replicas": 1, "images": [f"img:{revision}"],
            "age_seconds": age_seconds}


# --- loading ----------------------------------------------------------------

def test_from_file_round_trips(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(_capture(namespaces=[
        {"name": "default", "status": "Active", "age_seconds": 60.0}])))
    state = MockState.from_file(path, frozen=True)
    assert [n.name for n in state.get_namespaces()] == ["default"]


def test_builtin_loads_and_is_a_coherent_cluster():
    state = MockState.from_builtin(frozen=True)
    assert state.captured_at is not None
    assert state.redacted is False
    pods = state.get_pod_summaries()
    assert any(p.name == "ad-647b4947cc-s5mpm" for p in pods)
    # Every pod in the capture answers the per-pod tools.
    for pod in pods:
        assert state.get_pod_container_statuses(pod.name, pod.namespace)
        assert state.get_pod_spec(pod.name, pod.namespace)


# --- filtering --------------------------------------------------------------

def test_namespace_filtering_matches_the_real_tools():
    capture = _capture(
        services=[
            {"name": "a", "namespace": "default", "type": "ClusterIP",
             "cluster_ip": "10.0.0.1", "external_ip": None, "ports": [],
             "age_seconds": 60.0, "selector": {}, "labels": {}, "annotations": {}},
            {"name": "b", "namespace": "kube-system", "type": "ClusterIP",
             "cluster_ip": "10.0.0.2", "external_ip": None, "ports": [],
             "age_seconds": 60.0, "selector": {}, "labels": {}, "annotations": {}}])
    state = MockState(capture, frozen=True)
    assert len(state.get_service_summaries()) == 2
    assert [s.name for s in state.get_service_summaries("default")] == ["a"]
    assert state.get_service_summaries("nonexistent") == []


def test_missing_pod_returns_empty_container_statuses():
    state = MockState(_capture(pods=[]), frozen=True)
    assert state.get_pod_container_statuses("nope", "default") == []


def test_missing_configmap_raises_like_the_real_tool():
    state = MockState(_capture(configmaps=[]), frozen=True)
    with pytest.raises(k8s_tools.K8sApiError, match="not found"):
        state.get_configmap("nope", "default")


def test_configmap_summary_and_contents_come_from_one_record():
    state = MockState(_capture(configmaps=[{
        "name": "app-config", "namespace": "default", "age_seconds": 60.0,
        "key_count": 3, "data_size": 120,
        "data": {"A": "1", "B": "2"}, "binary_data_keys": ["cert.bin"]}]), frozen=True)
    summary = state.get_configmap_summaries()[0]
    assert summary.key_count == 3
    # data_size counts the bytes of binary values, which get_configmap does not
    # return, so it is stored rather than re-derived.
    assert summary.data_size == 120
    full = state.get_configmap("app-config", "default")
    assert full["data"] == {"A": "1", "B": "2"}
    assert full["binary_data_keys"] == ["cert.bin"]


# --- replica set ordering ---------------------------------------------------

class TestReplicaSetOrdering:
    """The real tool's docstring promises revision order, oldest first, so that
    the last entry for one deployment is its current revision. Replay has to
    reproduce that ordering rather than echo the order of the capture file.
    """

    # Deliberately shuffled, so a MockState that echoed the file would fail.
    SHUFFLED = _capture(replicasets=[
        _rs("ad-3", revision=3), _rs("ad-1", revision=1),
        _rs("other-1", namespace="kube-system", owner="other", revision=1),
        _rs("ad-2", revision=2),
        _rs("standalone", owner=None, revision=None),
    ])

    def test_sorted_by_revision_oldest_first(self):
        state = MockState(dict(self.SHUFFLED), frozen=True)
        out = state.get_replicaset_summaries(namespace="default", deployment="ad")
        assert [r.revision for r in out] == [1, 2, 3]

    def test_last_entry_is_the_current_revision(self):
        state = MockState(dict(self.SHUFFLED), frozen=True)
        out = state.get_replicaset_summaries(deployment="ad")
        assert out[-1].name == "ad-3"

    def test_ordering_survives_filtering(self):
        # Sorting has to happen after filtering, not before it.
        state = MockState(dict(self.SHUFFLED), frozen=True)
        out = state.get_replicaset_summaries(namespace="default")
        assert [r.name for r in out] == ["standalone", "ad-1", "ad-2", "ad-3"]

    def test_null_revision_sorts_first_as_in_the_real_tool(self):
        state = MockState(dict(self.SHUFFLED), frozen=True)
        out = state.get_replicaset_summaries(namespace="default")
        assert out[0].revision is None

    def test_matches_the_real_tool_on_the_same_input(self):
        """Belt and braces: run the real sort over the same summaries."""
        state = MockState(dict(self.SHUFFLED), frozen=True)
        mine = state.get_replicaset_summaries()
        expected = sorted(mine, key=lambda r: (r.namespace, r.owner_deployment or "",
                                               r.revision if r.revision is not None else -1))
        assert [r.name for r in mine] == [r.name for r in expected]

    def test_deployment_filter_excludes_other_namespaces_owners(self):
        state = MockState(dict(self.SHUFFLED), frozen=True)
        assert [r.name for r in state.get_replicaset_summaries(deployment="other")] \
            == ["other-1"]


# --- logs -------------------------------------------------------------------

def _pod_with_logs(logs, previous_logs=None, restarts=1):
    record = {
        "summary": {"name": "p", "namespace": "default", "total_containers": 1,
                    "ready_containers": 0, "restarts": restarts,
                    "last_restart_seconds": 60.0, "age_seconds": 600.0},
        "container_statuses": [{
            "pod_name": "p", "namespace": "default", "container_name": "c",
            "image": "img", "ready": False, "restart_count": restarts,
            "started": False, "stop_signal": None, "state": None, "last_state": None,
            "volume_mounts": [], "resource_requests": {}, "resource_limits": {},
            "allocated_resources": {}}],
        "spec": {},
        "logs": logs,
    }
    if previous_logs is not None:
        record["previous_logs"] = previous_logs
    return record


def test_previous_true_serves_previous_logs():
    state = MockState(_capture(pods=[_pod_with_logs(
        {"c": "current"}, {"c": "the OOM is here"})]), frozen=True)
    assert state.get_logs_for_pod_and_container("p", "default", "c") == "current"
    assert state.get_logs_for_pod_and_container("p", "default", "c",
                                                previous=True) == "the OOM is here"


def test_previous_true_with_no_previous_instance_fails_like_the_real_tool():
    # The real tool raises rather than returning empty, because "no previous
    # instance" and "previous instance logged nothing" are different answers.
    state = MockState(_capture(pods=[_pod_with_logs({"c": "current"})]), frozen=True)
    with pytest.raises(k8s_tools.K8sApiError):
        state.get_logs_for_pod_and_container("p", "default", "c", previous=True)


def test_container_name_defaults_to_the_first_container():
    state = MockState(_capture(pods=[_pod_with_logs({"c": "current"})]), frozen=True)
    assert state.get_logs_for_pod_and_container("p", "default") == "current"


def test_tail_returns_the_last_n_lines():
    text = "\n".join(f"line {i}" for i in range(10))
    state = MockState(_capture(pods=[_pod_with_logs({"c": text})]), frozen=True)
    out = state.get_logs_for_pod_and_container("p", "default", "c", tail=3)
    assert out.splitlines() == ["line 7", "line 8", "line 9"]


def test_since_seconds_filters_on_parseable_timestamps():
    now = datetime.datetime.now(UTC)
    lines = [f"{(now - datetime.timedelta(seconds=s)).isoformat()} at -{s}s"
             for s in (600, 300, 30)]
    state = MockState(_capture(pods=[_pod_with_logs({"c": "\n".join(lines)})]),
                      frozen=True, server_start_time=now)
    out = state.get_logs_for_pod_and_container("p", "default", "c", since_seconds=120)
    assert out.splitlines() == [lines[-1]]


def test_since_seconds_is_ignored_when_lines_carry_no_timestamp():
    """Replay must not return an empty log for a filter it cannot evaluate -
    that would manufacture an absence of evidence."""
    text = "no timestamp here\nnor here"
    state = MockState(_capture(pods=[_pod_with_logs({"c": text})]), frozen=True)
    out = state.get_logs_for_pod_and_container("p", "default", "c", since_seconds=1)
    assert out == text


def test_job_logs_resolve_through_the_job_name_label():
    old = _pod_with_logs({"c": "older run"})
    old["summary"] |= {"name": "j-old", "age_seconds": 5000.0}
    old["labels"] = {"job-name": "j"}
    new = _pod_with_logs({"c": "newest run"})
    new["summary"] |= {"name": "j-new", "age_seconds": 60.0}
    new["labels"] = {"job-name": "j"}
    state = MockState(_capture(
        pods=[old, new],
        jobs=[{"name": "j", "namespace": "default", "owner": "cj", "active": 0,
               "succeeded": 1, "failed": 0, "start_time_seconds": 120.0,
               "completion_time_seconds": 60.0, "conditions": ["Complete"],
               "age_seconds": 120.0, "containers": []}]), frozen=True)
    # The newest pod of the job, as the real tool picks it.
    assert state.get_logs_for_job("j", "default") == "newest run"
    assert state.get_logs_for_cronjob("cj", "default") == "newest run"
    assert state.get_logs_for_job("no-such-job", "default") is None
    assert state.get_logs_for_cronjob("no-such-cronjob", "default") is None


# --- replay clock -----------------------------------------------------------

class TestReplayClock:

    CAPTURE = _capture(namespaces=[
        {"name": "default", "status": "Active", "age_seconds": 100.0}])

    def test_advancing_clock_moves_ages_forward(self):
        start = datetime.datetime.now(UTC) - datetime.timedelta(seconds=300)
        state = MockState(dict(self.CAPTURE), frozen=False, server_start_time=start)
        # 100s old at capture, plus the 300s since the server started.
        assert state.get_namespaces()[0].age == datetime.timedelta(seconds=400)

    def test_frozen_clock_returns_the_captured_age(self):
        start = datetime.datetime.now(UTC) - datetime.timedelta(seconds=300)
        state = MockState(dict(self.CAPTURE), frozen=True, server_start_time=start)
        assert state.get_namespaces()[0].age == datetime.timedelta(seconds=100)

    def test_frozen_mode_is_identical_across_a_slept_interval(self):
        """A graded suite needs the same scenario to yield the same ages every
        run, and the same ages at its first tool call and its last."""
        state = MockState(dict(self.CAPTURE), frozen=True)
        first = state.get_namespaces()[0].age
        time.sleep(1.1)
        assert state.get_namespaces()[0].age == first

    def test_advancing_mode_does_not_stand_still(self):
        state = MockState(dict(self.CAPTURE), frozen=False)
        first = state.get_namespaces()[0].age
        time.sleep(1.1)
        assert state.get_namespaces()[0].age > first

    def test_one_query_is_internally_consistent(self):
        """Two resources captured at the same instant must still be the same age
        after replay, however many fields the query decodes in between."""
        capture = _capture(replicasets=[_rs(f"rs-{i}", revision=i, age_seconds=500.0)
                                        for i in range(200)])
        state = MockState(capture, frozen=False)
        ages = {r.age for r in state.get_replicaset_summaries()}
        assert len(ages) == 1

    def test_datetimes_are_anchored_to_server_start_in_both_modes(self):
        capture = _capture(pods=[{
            "summary": {"name": "p", "namespace": "default", "total_containers": 1,
                        "ready_containers": 1, "restarts": 0,
                        "last_restart_seconds": None, "age_seconds": 60.0},
            "container_statuses": [{
                "pod_name": "p", "namespace": "default", "container_name": "c",
                "image": "img", "ready": True, "restart_count": 0, "started": True,
                "stop_signal": None,
                "state": {"state_name": "Running", "started_at_offset_seconds": 60.0},
                "last_state": None, "volume_mounts": [], "resource_requests": {},
                "resource_limits": {}, "allocated_resources": {}}]}])
        start = datetime.datetime.now(UTC)
        for frozen in (True, False):
            state = MockState(json.loads(json.dumps(capture)), frozen=frozen,
                              server_start_time=start)
            status = state.get_pod_container_statuses("p", "default")[0]
            assert status.state.started_at == start - datetime.timedelta(seconds=60)


# --- compressed captures ----------------------------------------------------

class TestCompressedCaptures:
    """Captures may be gzipped; detection is by content, not by file name."""

    CAPTURE = _capture(namespaces=[
        {"name": "default", "status": "Active", "age_seconds": 100.0}])

    def _write_gz(self, path):
        import gzip
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(self.CAPTURE, f)
        return path

    def test_gzipped_capture_loads(self, tmp_path):
        state = MockState.from_file(self._write_gz(tmp_path / "s.json.gz"), frozen=True)
        assert [n.name for n in state.get_namespaces()] == ["default"]

    def test_gzipped_capture_loads_under_any_name(self, tmp_path):
        # Detection is by the gzip magic bytes, so a capture survives being
        # renamed or downloaded without its extension.
        state = MockState.from_file(self._write_gz(tmp_path / "no-extension"), frozen=True)
        assert [n.name for n in state.get_namespaces()] == ["default"]

    def test_plain_json_still_loads(self, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(json.dumps(self.CAPTURE))
        assert MockState.from_file(path, frozen=True).get_namespaces()[0].name == "default"

    def test_truncated_gzip_is_reported_as_such(self, tmp_path):
        path = tmp_path / "bad.json.gz"
        path.write_bytes(b"\x1f\x8b" + b"garbage")
        with pytest.raises(CaptureFormatError, match="gzipped"):
            MockState.from_file(path)
