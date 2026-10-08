"""Tests for the capture serializer/deserializer round-trip.

No cluster is needed: these build tool-return objects directly, encode them the
way ``k8s-capture-state`` would, and read them back through ``MockState``.
"""

import datetime
import json
from typing import Optional

import pytest
from pydantic import BaseModel

from k8stools import k8s_tools
from k8stools.capture import (_capture_events, _Redactor, capture_state,
                              CaptureStats)
from k8stools.mock_state import (CAPTURE_VERSION, CaptureFormatError, MockState,
                                 _Clock, decode_model, encode_model)


UTC = datetime.timezone.utc


def _state(**lists) -> dict:
    """An otherwise-empty capture holding the given top-level lists."""
    base = {
        "version": CAPTURE_VERSION,
        "captured_at": datetime.datetime.now(UTC).isoformat(),
        "redacted": False,
    }
    base.update(lists)
    return base


def _reload(state: dict, frozen: bool = True) -> MockState:
    """Round-trip a capture through JSON, as writing and reading a file would."""
    return MockState(json.loads(json.dumps(state)), frozen=frozen)


# --- format -----------------------------------------------------------------

def test_unknown_version_is_rejected():
    # A capture from a future format would otherwise be half-read and served as
    # a cluster with mysteriously missing resources.
    with pytest.raises(CaptureFormatError, match="version"):
        MockState({"version": "999"})


def test_missing_file_is_reported_clearly(tmp_path):
    with pytest.raises(CaptureFormatError, match="not found"):
        MockState.from_file(tmp_path / "nope.json")


def test_malformed_file_is_reported_clearly(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(CaptureFormatError, match="valid JSON"):
        MockState.from_file(path)


# --- temporal round-trip ----------------------------------------------------

def test_timedelta_round_trips_as_seconds():
    captured_at = datetime.datetime.now(UTC)
    ns = k8s_tools.NamespaceSummary(name="default", status="Active",
                                    age=datetime.timedelta(days=5))
    record = encode_model(ns, captured_at)
    assert record["age_seconds"] == 5 * 86400
    assert "age" not in record

    back = _reload(_state(namespaces=[record])).get_namespaces()[0]
    assert back.age == datetime.timedelta(days=5)


def test_optional_timedelta_none_round_trips():
    captured_at = datetime.datetime.now(UTC)
    pod = k8s_tools.PodSummary(name="p", namespace="default", total_containers=1,
                               ready_containers=1, restarts=0, last_restart=None,
                               age=datetime.timedelta(hours=2))
    record = encode_model(pod, captured_at)
    # The key is chosen from the declared type, so an absent value still lands
    # under the name the reader looks for.
    assert record["last_restart_seconds"] is None

    back = _reload(_state(pods=[{"summary": record}])).get_pod_summaries()[0]
    assert back.last_restart is None
    assert back.age == datetime.timedelta(hours=2)


def test_container_state_datetimes_round_trip_and_discriminate():
    captured_at = datetime.datetime.now(UTC)
    status = k8s_tools.ContainerStatus(
        pod_name="p", namespace="default", container_name="c", image="img",
        ready=False, restart_count=3, started=False, stop_signal=None,
        state=k8s_tools.ContainerStateWaiting(reason="CrashLoopBackOff", message="m"),
        last_state=k8s_tools.ContainerStateTerminated(
            exit_code=137, reason="OOMKilled",
            started_at=captured_at - datetime.timedelta(seconds=360),
            finished_at=captured_at - datetime.timedelta(seconds=300)),
        volume_mounts=[], resource_requests={}, resource_limits={},
        allocated_resources={})
    record = encode_model(status, captured_at)
    assert record["last_state"]["started_at_offset_seconds"] == pytest.approx(360)
    assert record["last_state"]["finished_at_offset_seconds"] == pytest.approx(300)

    state = _reload(_state(pods=[{
        "summary": {"name": "p", "namespace": "default", "total_containers": 1,
                    "ready_containers": 0, "restarts": 3, "last_restart_seconds": None,
                    "age_seconds": 100.0},
        "container_statuses": [record]}]))
    back = state.get_pod_container_statuses("p", "default")[0]
    # The union member is recovered from state_name, not guessed from field shape.
    assert isinstance(back.state, k8s_tools.ContainerStateWaiting)
    assert back.state.reason == "CrashLoopBackOff"
    assert isinstance(back.last_state, k8s_tools.ContainerStateTerminated)
    assert back.last_state.exit_code == 137
    # The 60s between the previous instance starting and being killed survives.
    assert (back.last_state.finished_at - back.last_state.started_at
            == datetime.timedelta(seconds=60))
    assert back.last_state.ran_for == datetime.timedelta(seconds=60)


def test_a_capture_recorded_before_ran_for_existed_still_gets_it():
    """Old captures have no ran_for; replay derives it rather than dropping it."""
    captured_at = datetime.datetime.now(UTC)
    status = k8s_tools.ContainerStatus(
        pod_name="p", namespace="default", container_name="c", image="img",
        ready=False, restart_count=3, started=False, stop_signal=None,
        state=None,
        last_state=k8s_tools.ContainerStateTerminated(
            exit_code=137,
            started_at=captured_at - datetime.timedelta(seconds=360),
            finished_at=captured_at - datetime.timedelta(seconds=297)),
        volume_mounts=[], resource_requests={}, resource_limits={},
        allocated_resources={})
    record = encode_model(status, captured_at)
    record["last_state"].pop("ran_for_seconds", None)
    state = _reload(_state(pods=[{
        "summary": {"name": "p", "namespace": "default", "total_containers": 1,
                    "ready_containers": 0, "restarts": 3, "last_restart_seconds": None,
                    "age_seconds": 100.0},
        "container_statuses": [record]}]))
    back = state.get_pod_container_statuses("p", "default")[0]
    assert back.last_state.ran_for == datetime.timedelta(seconds=63)


def test_ran_for_does_not_grow_on_an_advancing_replay_clock():
    """Regression: a capture stores ran_for as ``ran_for_seconds``, and the replay
    codec decodes every timedelta as an *age* - captured value plus time since the
    server started. Taken at its word, a container that ran 60s reported 1d+60s a
    day into a replay, beside timestamps still 60s apart. The frozen-clock tests
    above cannot see this: frozen elapsed time is zero."""
    captured_at = datetime.datetime.now(UTC)
    status = k8s_tools.ContainerStatus(
        pod_name="p", namespace="default", container_name="c", image="img",
        ready=False, restart_count=3, started=False, stop_signal=None,
        state=None,
        last_state=k8s_tools.ContainerStateTerminated(
            exit_code=137, reason="OOMKilled",
            started_at=captured_at - datetime.timedelta(seconds=360),
            finished_at=captured_at - datetime.timedelta(seconds=300)),
        volume_mounts=[], resource_requests={}, resource_limits={},
        allocated_resources={})
    state = _state(pods=[{
        "summary": {"name": "p", "namespace": "default", "total_containers": 1,
                    "ready_containers": 0, "restarts": 3, "last_restart_seconds": None,
                    "age_seconds": 100.0},
        "container_statuses": [encode_model(status, captured_at)]}])

    later = datetime.datetime.now(UTC) - datetime.timedelta(days=1)
    reloaded = MockState(json.loads(json.dumps(state)), frozen=False,
                         server_start_time=later)
    back = reloaded.get_pod_container_statuses("p", "default")[0].last_state
    assert back.ran_for == datetime.timedelta(seconds=60)
    assert back.ran_for == back.finished_at - back.started_at


class _Spans(BaseModel):
    """One of each kind of duration, bare and optional, for the codec tests.

    A bare field's Annotated metadata is moved onto FieldInfo.metadata by
    pydantic while an Optional one keeps it inline, so both shapes are covered.
    """
    name: str = "s"
    namespace: str = "default"
    age: k8s_tools.Duration
    span: k8s_tools.Interval
    maybe_span: Optional[k8s_tools.Interval] = None


def test_intervals_are_not_advanced_by_the_replay_clock():
    captured_at = datetime.datetime.now(UTC)
    record = encode_model(_Spans(age=datetime.timedelta(hours=1),
                                 span=datetime.timedelta(seconds=60),
                                 maybe_span=datetime.timedelta(seconds=90)), captured_at)
    # Same key convention as an age: the file format does not change.
    assert record["span_seconds"] == 60 and record["maybe_span_seconds"] == 90

    later = datetime.datetime.now(UTC) - datetime.timedelta(days=1)
    clock = _Clock(later, frozen=False)
    back = decode_model(_Spans, json.loads(json.dumps(record)), clock)
    assert back.age == datetime.timedelta(days=1, hours=1)   # ages advance
    assert back.span == datetime.timedelta(seconds=60)        # spans do not
    assert back.maybe_span == datetime.timedelta(seconds=90)


def test_a_hand_edited_ran_for_takes_effect_on_replay():
    """The stored value is live data, not a dead copy of the timestamps."""
    captured_at = datetime.datetime.now(UTC)
    status = k8s_tools.ContainerStatus(
        pod_name="p", namespace="default", container_name="c", image="img",
        ready=False, restart_count=3, started=False, stop_signal=None, state=None,
        last_state=k8s_tools.ContainerStateTerminated(
            started_at=captured_at - datetime.timedelta(seconds=360),
            finished_at=captured_at - datetime.timedelta(seconds=300)),
        volume_mounts=[], resource_requests={}, resource_limits={},
        allocated_resources={})
    record = encode_model(status, captured_at)
    record["last_state"]["ran_for_seconds"] = 3600.0
    state = _reload(_state(pods=[{
        "summary": {"name": "p", "namespace": "default", "total_containers": 1,
                    "ready_containers": 0, "restarts": 3, "last_restart_seconds": None,
                    "age_seconds": 100.0},
        "container_statuses": [record]}]), frozen=False)
    back = state.get_pod_container_statuses("p", "default")[0]
    assert back.last_state.ran_for == datetime.timedelta(hours=1)


def test_relative_intervals_survive_a_later_reload():
    """The inference a capture exists to support: deployment 8d old, its newest
    replica set 7h34m old, so it was upgraded 7h34m ago. That subtraction is only
    valid if both ages advance by the same offset on replay."""
    captured_at = datetime.datetime.now(UTC)
    deployment = k8s_tools.DeploymentSummary(
        name="ad", namespace="default", total_replicas=1, ready_replicas=0,
        up_to_date_relicas=1, available_replicas=0, age=datetime.timedelta(days=8))
    replicaset = k8s_tools.ReplicaSetSummary(
        name="ad-647b4947cc", namespace="default", owner_deployment="ad", revision=2,
        desired_replicas=1, current_replicas=1, ready_replicas=0, images=["img:2"],
        age=datetime.timedelta(hours=7, minutes=34))
    state = _state(deployments=[encode_model(deployment, captured_at)],
                   replicasets=[encode_model(replicaset, captured_at)])

    # Reload at an arbitrary later time, with the clock advancing.
    later = datetime.datetime.now(UTC) - datetime.timedelta(days=3)
    reloaded = MockState(json.loads(json.dumps(state)), frozen=False,
                         server_start_time=later)
    d = reloaded.get_deployment_summaries()[0]
    r = reloaded.get_replicaset_summaries()[0]
    assert d.age - r.age == datetime.timedelta(days=8) - datetime.timedelta(hours=7, minutes=34)
    # Both advanced by the same whole-second offset, so the sum is exact too.
    assert d.age == datetime.timedelta(days=11)


def test_replicasets_round_trip_with_null_owner_and_revision():
    captured_at = datetime.datetime.now(UTC)
    standalone = k8s_tools.ReplicaSetSummary(
        name="orphan", namespace="default", owner_deployment=None, revision=None,
        desired_replicas=1, current_replicas=1, ready_replicas=1, images=["img"],
        age=datetime.timedelta(hours=1))
    record = encode_model(standalone, captured_at)
    assert record["owner_deployment"] is None and record["revision"] is None

    back = _reload(_state(replicasets=[record])).get_replicaset_summaries()[0]
    assert back.owner_deployment is None
    assert back.revision is None
    assert back.images == ["img"]


def test_events_capture_kind_and_name_for_both_event_tools(monkeypatch):
    """get_events reports 'Kind/name' and get_pod_events reports a bare name, so
    the capture stores the parts rather than only the rendered string - and the
    namespace, which EventSummary does not carry but both tools filter on."""
    monkeypatch.setattr(k8s_tools, "get_events", lambda namespace=None: [
        k8s_tools.EventSummary(last_seen=datetime.timedelta(seconds=120),
                               type="Warning", reason="BackOff",
                               object="Pod/ad-1", message="Back-off restarting"),
        k8s_tools.EventSummary(last_seen=None, type="Normal", reason="Starting",
                               object="minikube", message="no kind available"),
    ])
    records = _capture_events("default", datetime.datetime.now(UTC),
                              _Redactor(False, CaptureStats()))
    assert records[0]["involved_kind"] == "Pod"
    assert records[0]["involved_name"] == "ad-1"
    assert records[0]["namespace"] == "default"
    # An object with no kind keeps the whole string as the name rather than
    # inventing one.
    assert records[1]["involved_kind"] is None
    assert records[1]["involved_name"] == "minikube"
    assert records[1]["last_seen_seconds"] is None

    state = _reload(_state(events=records))
    assert [e.object for e in state.get_events()] == ["Pod/ad-1", "minikube"]
    assert [e.object for e in state.get_pod_events("ad-1", "default")] == ["ad-1"]
    assert state.get_events(involved_kind="Pod")[0].reason == "BackOff"
    assert state.get_events(namespace="other") == []


# --- redaction --------------------------------------------------------------

class _FakeCluster:
    """The smallest cluster that carries a secret through every path a capture takes."""

    JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    AWS_KEY = "AKIAIOSFODNN7EXAMPLE"

    def install(self, monkeypatch):
        import k8stools.capture as capture
        monkeypatch.setattr(k8s_tools, "get_cluster_info", lambda: k8s_tools.ClusterInfo(
            source="kubeconfig", context="prod", server="https://prod.example:6443",
            kubeconfig="/home/someone/.kube/prod.yaml", server_version="v1.31.2"))
        monkeypatch.setattr(k8s_tools, "get_namespaces", lambda: [
            k8s_tools.NamespaceSummary(name="default", status="Active",
                                       age=datetime.timedelta(days=1))])
        monkeypatch.setattr(k8s_tools, "get_node_summaries", lambda: [])
        for name in ("get_deployment_summaries", "get_replicaset_summaries",
                     "get_service_summaries", "get_statefulset_summaries",
                     "get_daemonset_summaries",
                     "get_cronjob_summaries", "get_job_summaries",
                     "get_pvc_summaries", "get_pod_summaries"):
            monkeypatch.setattr(k8s_tools, name, lambda ns=None: [])
        monkeypatch.setattr(k8s_tools, "get_events", lambda **kw: [])
        monkeypatch.setattr(k8s_tools, "get_configmap_summaries", lambda ns=None: [
            k8s_tools.ConfigMapSummary(name="app-config", namespace="default",
                                       key_count=2, data_size=100,
                                       age=datetime.timedelta(days=1))])
        monkeypatch.setattr(k8s_tools, "get_configmap", lambda name, ns="default": {
            "name": name, "namespace": ns,
            "data": {"AWS_SECRET_ACCESS_KEY": self.AWS_KEY, "SESSION_JWT": self.JWT,
                     "LOG_LEVEL": "info"},
            "binary_data_keys": []})
        monkeypatch.setattr(capture, "_pod_labels", lambda ns: {})


def test_capture_redacts_by_default(monkeypatch):
    """The library-consumer bypass this guards against: capture calls the tool
    functions directly, so it misses the MCP server's redaction boundary and has
    to apply the pass itself. Without it a capture would write raw secrets to disk
    while the server in front of the same cluster was redacting them."""
    _FakeCluster().install(monkeypatch)
    stats = CaptureStats()
    state = capture_state(redact=True, stats=stats)

    assert state["redacted"] is True
    data = state["configmaps"][0]["data"]
    assert data["AWS_SECRET_ACCESS_KEY"] == "[REDACTED]"
    assert data["SESSION_JWT"] == "[REDACTED]"
    assert data["LOG_LEVEL"] == "info"  # redaction never drops a field
    assert stats.redactions >= 2

    # Structure survives, so an agent replaying it still sees the key exists.
    served = _reload(state).get_configmap("app-config", "default")
    assert set(served["data"]) == {"AWS_SECRET_ACCESS_KEY", "SESSION_JWT", "LOG_LEVEL"}


def test_capture_keeps_raw_values_with_no_redact(monkeypatch):
    _FakeCluster().install(monkeypatch)
    state = capture_state(redact=False)

    assert state["redacted"] is False
    data = state["configmaps"][0]["data"]
    assert data["AWS_SECRET_ACCESS_KEY"] == _FakeCluster.AWS_KEY
    assert data["SESSION_JWT"] == _FakeCluster.JWT


def test_redacted_flag_travels_with_the_capture(monkeypatch):
    """Redaction is lossy and one-way, so whether it was applied cannot be
    inferred from the content - the file has to say."""
    _FakeCluster().install(monkeypatch)
    assert _reload(capture_state(redact=True)).redacted is True
    assert _reload(capture_state(redact=False)).redacted is False


def test_a_containers_name_cannot_redact_its_own_logs(monkeypatch):
    """Regression: captured logs live in a dict keyed by container name, and
    `valkey-cart` matches the redaction pass's `key` heuristic. An envelope-wide
    pass replaced that container's entire log with the marker - evidence destroyed
    by the container's own name. The live server does not do this, because it
    returns the log as a bare string, so neither may the capture."""
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    log = "2026-01-01T00:00:00Z valkey ready to accept connections"
    monkeypatch.setattr(k8s_tools, "get_pod_summaries", lambda ns=None: [
        k8s_tools.PodSummary(name="valkey-cart-1", namespace="default",
                             total_containers=1, ready_containers=1, restarts=1,
                             last_restart=datetime.timedelta(seconds=60),
                             age=datetime.timedelta(hours=1))])
    monkeypatch.setattr(k8s_tools, "get_pod_container_statuses", lambda p, ns: [
        k8s_tools.ContainerStatus(
            pod_name=p, namespace=ns, container_name="valkey-cart", image="valkey:8",
            ready=True, restart_count=1, started=True, stop_signal=None, state=None,
            last_state=None, volume_mounts=[], resource_requests={},
            resource_limits={}, allocated_resources={})])
    monkeypatch.setattr(k8s_tools, "get_pod_spec", lambda p, ns: {})
    monkeypatch.setattr(k8s_tools, "get_logs_for_pod_and_container",
                        lambda *a, **kw: log)

    state = capture_state(redact=True)
    pod = state["pods"][0]
    assert pod["logs"]["valkey-cart"] == log
    assert pod["previous_logs"]["valkey-cart"] == log

    # A secret-shaped value inside a log is still caught, as on the live server.
    # Only the token's own span goes: a log line is mostly diagnostics, and the
    # surrounding text is the part an agent is reading the log for.
    monkeypatch.setattr(k8s_tools, "get_logs_for_pod_and_container",
                        lambda *a, **kw: f"token={_FakeCluster.JWT}")
    state = capture_state(redact=True)
    captured_log = state["pods"][0]["logs"]["valkey-cart"]
    assert _FakeCluster.JWT not in captured_log
    assert captured_log == "token=[REDACTED]"


def test_pod_labels_are_not_subject_to_the_key_name_heuristic(monkeypatch):
    """Labels are capture-internal - no tool returns them - and Kubernetes label
    values cannot hold a credential (63 chars, restricted charset). Redacting
    `gcp-auth-skip-secret: "true"` on the strength of its name is pure noise."""
    import k8stools.capture as capture
    cluster = _FakeCluster()
    cluster.install(monkeypatch)
    monkeypatch.setattr(k8s_tools, "get_pod_summaries", lambda ns=None: [
        k8s_tools.PodSummary(name="p", namespace="default", total_containers=1,
                             ready_containers=1, restarts=0, last_restart=None,
                             age=datetime.timedelta(hours=1))])
    monkeypatch.setattr(k8s_tools, "get_pod_container_statuses", lambda p, ns: [])
    monkeypatch.setattr(k8s_tools, "get_pod_spec", lambda p, ns: {})
    monkeypatch.setattr(capture, "_pod_labels",
                        lambda ns: {"p": {"gcp-auth-skip-secret": "true"}})

    state = capture_state(redact=True)
    assert state["pods"][0]["labels"] == {"gcp-auth-skip-secret": "true"}


def test_no_logs_skips_previous_logs_too(monkeypatch):
    """`--no-logs` means no logs. Previous-instance logs are logs, and leaving
    them in made a structure-only capture larger than a normal one."""
    _FakeCluster().install(monkeypatch)
    monkeypatch.setattr(k8s_tools, "get_pod_summaries", lambda ns=None: [
        k8s_tools.PodSummary(name="p", namespace="default", total_containers=1,
                             ready_containers=0, restarts=3,
                             last_restart=datetime.timedelta(seconds=60),
                             age=datetime.timedelta(hours=1))])
    monkeypatch.setattr(k8s_tools, "get_pod_container_statuses", lambda p, ns: [
        k8s_tools.ContainerStatus(
            pod_name=p, namespace=ns, container_name="c", image="img", ready=False,
            restart_count=3, started=False, stop_signal=None, state=None,
            last_state=None, volume_mounts=[], resource_requests={},
            resource_limits={}, allocated_resources={})])
    monkeypatch.setattr(k8s_tools, "get_pod_spec", lambda p, ns: {})
    monkeypatch.setattr(k8s_tools, "get_logs_for_pod_and_container",
                        lambda *a, **kw: "some log text")

    stats = CaptureStats()
    state = capture_state(include_logs=False, redact=False, stats=stats)
    pod = state["pods"][0]
    assert pod["logs"] == {}
    assert "previous_logs" not in pod
    assert stats.logs_captured == 0 and stats.previous_logs_captured == 0
    assert stats.missing_previous_logs == []  # not "missing" - never asked for

    # The finer-grained flag still keeps the current instance.
    state = capture_state(include_previous_logs=False, redact=False)
    assert state["pods"][0]["logs"] == {"c": "some log text"}
    assert "previous_logs" not in state["pods"][0]


def test_gz_output_round_trips(tmp_path, monkeypatch):
    """A .gz output name writes gzipped, and MockState reads it back."""
    import gzip
    from k8stools.capture import write_state
    _FakeCluster().install(monkeypatch)
    state = capture_state(redact=False)

    path = tmp_path / "cap.json.gz"
    write_state(state, path)
    assert path.read_bytes()[:2] == b"\x1f\x8b"
    with gzip.open(path, "rt") as f:
        assert json.load(f)["version"] == CAPTURE_VERSION
    assert MockState.from_file(path, frozen=True).redacted is False

    # Plain output is still plain, and stays indented so git can delta it.
    plain = tmp_path / "cap.json"
    write_state(state, plain)
    assert plain.read_text().startswith("{\n  ")


def test_capture_records_which_cluster_it_came_from(monkeypatch):
    """Replay answers get_cluster_info from this record, so a capture can be told
    apart from a live cluster and from captures of other clusters."""
    _FakeCluster().install(monkeypatch)
    state = capture_state(redact=True)
    assert state["cluster"] == {"context": "prod", "server": "https://prod.example:6443",
                                "server_version": "v1.31.2"}
    # The kubeconfig path names a file on the capturing machine: meaningless on
    # replay, and not something to hand to whoever the capture is shared with.
    assert "/home/someone" not in json.dumps(state)

    info = _reload(state).get_cluster_info()
    assert (info.source, info.context, info.server_version) == ("capture", "prod", "v1.31.2")


def test_capture_survives_an_unreadable_cluster_record(monkeypatch):
    _FakeCluster().install(monkeypatch)

    def fail():
        raise k8s_tools.K8sApiError("version endpoint forbidden")
    monkeypatch.setattr(k8s_tools, "get_cluster_info", fail)
    state = capture_state(redact=False)
    assert state["cluster"] is None
    assert _reload(state).get_cluster_info().context is None


# --- re-redacting an existing capture (issue #12) ------------------------------

def _old_capture(path, gzipped=False):
    import gzip as _gzip
    state = {"version": CAPTURE_VERSION, "captured_at": "2026-09-13T01:37:32+00:00",
             "redacted": False,
             "pods": [{"summary": {"name": "p", "namespace": "default"},
                       "spec": {"containers": [{"name": "c", "env": [
                           {"name": "DATABASE_URL",
                            "value": "postgres://app:hunter2@db/app"}],
                           "args": ["--token", "ghp_abcdefghij0123456789"]}]}}]}
    data = json.dumps(state, indent=2).encode()
    path.write_bytes(_gzip.compress(data) if gzipped else data)


@pytest.mark.parametrize("gzipped", [False, True])
def test_redact_file_rewrites_an_existing_capture_in_place(tmp_path, gzipped):
    from k8stools.capture import redact_capture_file
    from k8stools.mock_state import read_capture
    path = tmp_path / "old.json"
    _old_capture(path, gzipped)
    assert redact_capture_file(path) == 2
    state, still_gzipped = read_capture(path)
    assert still_gzipped == gzipped
    assert state["redacted"] is True
    container = state["pods"][0]["spec"]["containers"][0]
    assert container["env"][0]["value"] == "postgres://app:[REDACTED]@db/app"
    assert container["args"] == ["--token", "[REDACTED]"]
    assert state["captured_at"] == "2026-09-13T01:37:32+00:00"
    assert redact_capture_file(path) == 0          # idempotent


def test_redact_file_cli_needs_no_cluster(tmp_path, monkeypatch, capsys):
    import sys as _sys
    from k8stools import capture
    path = tmp_path / "old.json"
    _old_capture(path)
    monkeypatch.setattr(k8s_tools, "configure", lambda *a, **k: pytest.fail("must not bind"))
    monkeypatch.setattr(_sys, "argv", ["k8s-capture-state", "--redact-file", str(path)])
    assert capture.main() == 0
    assert "Redacted 2 value(s)" in capsys.readouterr().out
    monkeypatch.setattr(_sys, "argv", ["k8s-capture-state", "--redact-file", str(tmp_path / "nope")])
    assert capture.main() == 1


def test_pod_labels_are_redacted_at_capture_time(monkeypatch):
    """Labels are served on replay, so they get the same pass as tool output -
    which is also what --redact-file applies to a whole file."""
    import k8stools.capture as capture
    monkeypatch.setattr(capture, "_pod_labels",
                        lambda ns: {"p": {"app": "x", "auth-token": "ghp_abcdefghij0123456789"}})
    monkeypatch.setattr(k8s_tools, "get_pod_summaries", lambda ns=None: [
        k8s_tools.PodSummary(name="p", namespace="default", total_containers=1,
                             ready_containers=1, restarts=0, last_restart=None,
                             age=datetime.timedelta(days=1))])
    monkeypatch.setattr(k8s_tools, "get_pod_container_statuses", lambda *a, **k: [])
    monkeypatch.setattr(k8s_tools, "get_pod_spec", lambda *a, **k: {})
    stats = CaptureStats()
    records = capture._capture_pods("default", datetime.datetime.now(datetime.timezone.utc),
                                    False, 0, False, stats, _Redactor(True, stats))
    assert records[0]["labels"] == {"app": "x", "auth-token": "[REDACTED]"}
