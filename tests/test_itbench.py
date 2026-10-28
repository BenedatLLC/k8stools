"""Importing ITBench-Lite snapshots as captures (#30).

The importer serves one pull of a snapshot to the real tools and capture, so
these check what it must get right on the way: what a pull is, which kinds it
records (the rest replay as not recorded), the events that existed at the pull,
which log records are the current instance's, and the facts it states about
what ITBench never collected (metrics, custom resource instances, revisions).
The excerpt of Scenario-33 is real snapshot data; the small snapshots built here
cover the cases it doesn't have.
"""

import csv
import datetime
import json
from pathlib import Path

import pytest

from k8stools import itbench
from k8stools.capture import main as capture_main
from k8stools.composites import namespace_health, workload_report
from k8stools.k8s_tools import ContainerStateTerminated, K8sMetricsUnavailable, K8sNotRecorded
from k8stools.mock_state import NOTE_LOGS_FROM_OTEL, MockState

EXCERPT = Path(__file__).parent / "fixtures" / "itbench-33-excerpt"
UTC = datetime.timezone.utc


def _replay(state: dict) -> MockState:
    return MockState(json.loads(json.dumps(state)), frozen=True)


@pytest.fixture(scope="module")
def scenario_33():
    return itbench.import_snapshot(EXCERPT)


# --- the Scenario-33 excerpt ------------------------------------------------------------

def test_pulls_are_batches_five_minutes_apart():
    pulls = itbench.read_pulls(EXCERPT)
    assert [p.number for p in pulls] == [1, 2]
    assert pulls[0].started.replace(microsecond=0) == datetime.datetime(2025, 12, 15, 17, 59, 37,
                                                                        tzinfo=UTC)
    assert abs(pulls[1].started - pulls[0].started - datetime.timedelta(minutes=5)) \
        < datetime.timedelta(seconds=1)
    assert all(p.ended - p.started < datetime.timedelta(seconds=5) for p in pulls)
    assert set(pulls[1].kinds) == {"namespaces", "nodes", "pods", "deployments",
                                   "replicasets", "services", "endpoints"}


def test_the_capture_is_as_of_the_pull_and_says_where_it_came_from(scenario_33):
    assert scenario_33["captured_at"] == "2025-12-15T18:04:40+00:00"
    source = dict(scenario_33["source"])
    assert source.pop("pulled_from").startswith("2025-12-15T18:04:37")
    assert source == {"kind": "itbench", "snapshot": "itbench-33-excerpt", "pull": 2,
                      "pulls": 2, "logs": "otel"}
    info = _replay(scenario_33).get_cluster_info()
    assert (info.source, info.context, info.server) == \
        ("capture", "itbench/itbench-33-excerpt", "ITBench snapshot")


def test_the_regression_picture_of_scenario_33(scenario_33):
    """What k8srca's earlier conversion showed, and what it couldn't: the two
    Deployments whose new pods no node admits, the scheduler's reason, and the
    template change that did it."""
    state = _replay(scenario_33)
    health = namespace_health(state, "otel-demo")
    assert health.healthy == ["Deployment/checkout 1/1"]
    by_name = {w.workload: w for w in health.workloads}
    assert set(by_name) == {"Deployment/ad", "Deployment/cart"}
    cart = by_name["Deployment/cart"]
    assert cart.ready == 1 and not cart.healthy
    [unscheduled] = cart.unscheduled
    assert "didn't match Pod's node affinity/selector" in unscheduled
    assert cart.placement == ["cart-5b597c6db5-pkzkh: 0 of 5 nodes match its nodeSelector "
                              "and required node affinity"]
    assert cart.services == ["Service/cart: 1 ready, 0 not ready"]  # from synthesized slices
    [change] = state.get_workload_history("cart", "otel-demo").revisions[0].changes
    assert (change.field, change.after) == ("node_selector[kubernetes.io/hostname]", "invalid-node")


def test_kinds_the_pull_has_no_batch_of_are_not_recorded(scenario_33):
    state = _replay(scenario_33)
    for read in (state.get_daemonset_summaries, state.get_hpa_summaries,
                 state.get_job_summaries, state.get_configmap_summaries):
        with pytest.raises(K8sNotRecorded):
            read()
    assert "Not recorded in this capture, so unknown rather than absent: StatefulSets, " \
        "DaemonSets, Jobs, HorizontalPodAutoscalers, PersistentVolumeClaims, Ingresses." \
        in namespace_health(state, "otel-demo").notes


def test_metrics_say_why_they_are_unknown(scenario_33):
    with pytest.raises(K8sMetricsUnavailable, match="records no metrics-server readings"):
        _replay(scenario_33).get_container_metrics("otel-demo")


def test_events_keep_one_copy_and_only_those_that_existed(scenario_33):
    events = _replay(scenario_33).get_events(namespace="otel-demo")
    assert len(events) == len({(e.object, e.reason, e.message) for e in events}) == 6
    assert any(e.reason == "FailedScheduling" and e.object == "Pod/cart-5b597c6db5-pkzkh"
               for e in events)


def test_logs_are_otel_records_with_a_note_and_kubelet_timestamps(scenario_33):
    state = _replay(scenario_33)
    log = state.get_logs_for_pod_and_container("checkout-5dccddf8bb-dt94w", "otel-demo")
    note, *lines = log.splitlines()
    assert note == f"[k8stools] note: {NOTE_LOGS_FROM_OTEL}"
    assert lines and all(itbench_ts(line) for line in lines)
    assert "INFO payment went through transaction_id=" in log


def itbench_ts(line: str) -> bool:
    stamp = line.split(" ", 1)[0]
    return stamp.endswith("Z") and len(stamp.split(".")[1]) == 10  # 9 digits and Z


def test_the_report_carries_the_note_with_its_log_tail(scenario_33):
    [tail] = workload_report(_replay(scenario_33), "checkout", "otel-demo").logs
    assert tail.available is not False and tail.lines
    assert NOTE_LOGS_FROM_OTEL in tail.notes


def test_the_cli_lists_pulls_and_imports(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["k8s-capture-state", "--from-itbench", str(EXCERPT),
                                     "--list-pulls"])
    assert capture_main() == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2 and out[0].startswith("  1  2025-12-15 17:59:37-17:59:40Z  7 kinds")
    output = tmp_path / "s33.json"
    monkeypatch.setattr("sys.argv", ["k8s-capture-state", "--from-itbench", str(EXCERPT),
                                     "--at", "1", "-o", str(output)])
    assert capture_main() == 0
    assert "Imported pull 1 of 2" in capsys.readouterr().out
    assert json.loads(output.read_text())["captured_at"] == "2025-12-15T17:59:40+00:00"


def test_no_such_pull_is_an_error():
    with pytest.raises(itbench.SnapshotImportError, match="pulls 1-2"):
        itbench.import_snapshot(EXCERPT, pull=3)


# --- built snapshots, for what the excerpt doesn't have --------------------------------

T0 = datetime.datetime(2025, 12, 15, 17, 0, 0, tzinfo=UTC)
COLUMNS = ["Timestamp", "TimestampTime", "TraceId", "SpanId", "TraceFlags", "SeverityText",
           "SeverityNumber", "ServiceName", "Body", "ResourceSchemaUrl", "ResourceAttributes",
           "ScopeSchemaUrl", "ScopeName", "ScopeVersion", "ScopeAttributes", "LogAttributes"]


def _ch(ts: datetime.datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S.%f") + "000"


def _api(ts: datetime.datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path: Path, rows: list[dict]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(COLUMNS)
        for row in rows:
            w.writerow([row.get(c, "") for c in COLUMNS])


def _object(ts, kind, obj):
    return {"Timestamp": _ch(ts), "Body": json.dumps(obj),
            "LogAttributes": repr({"k8s.resource.name": kind})}


def _event(ts, type_, uid, last, reason="Started", api_version="v1"):
    obj = {"apiVersion": api_version, "kind": "Event", "count": 1, "type": "Normal",
           "metadata": {"name": f"web-1.{uid}", "namespace": "shop", "uid": uid,
                        "creationTimestamp": _api(last)},
           "involvedObject": {"kind": "Pod", "name": "web-1", "namespace": "shop"},
           "reason": reason, "message": f"event {uid}",
           "firstTimestamp": _api(last), "lastTimestamp": _api(last)}
    return {"Timestamp": _ch(ts), "Body": json.dumps({"type": type_, "object": obj}),
            "LogAttributes": repr({"k8s.resource.name": "events"})}


def _log(ts, pod_uid, body, service="web", attrs=None, severity="INFO"):
    return {"Timestamp": _ch(ts), "Body": body, "SeverityText": severity,
            "ResourceAttributes": repr({"k8s.pod.uid": pod_uid, "k8s.pod.name": "web-1",
                                        "k8s.namespace.name": "shop", "service.name": service}),
            "LogAttributes": repr(attrs or {})}


def _pod(containers=("web",), started=T0, state=None, name="web-1", uid="u-web"):
    statuses = [{"name": c, "containerID": f"containerd://{c}-id", "image": f"{c}:1",
                 "imageID": f"{c}@sha256:0",
                 "ready": True, "restartCount": 0, "started": True,
                 "state": state or {"running": {"startedAt": _api(started)}}}
                for c in containers]
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": "shop", "uid": uid,
                         "creationTimestamp": _api(T0 - datetime.timedelta(hours=1))},
            "spec": {"nodeName": "n1", "containers": [{"name": c, "image": f"{c}:1"}
                                                     for c in containers]},
            "status": {"phase": "Running", "containerStatuses": statuses}}


BASE = [("namespaces", {"apiVersion": "v1", "kind": "Namespace",
                        "metadata": {"name": "shop", "creationTimestamp": _api(T0 - datetime.timedelta(days=1))},
                        "status": {"phase": "Active"}}),
        ("nodes", {"apiVersion": "v1", "kind": "Node",
                   "metadata": {"name": "n1", "creationTimestamp": _api(T0 - datetime.timedelta(days=1))},
                   "status": {"conditions": [{"type": "Ready", "status": "True"}]}})]


def _snapshot(tmp_path, pod=None, events=(), logs=(), pulls=(T0 + datetime.timedelta(minutes=30),),
              kinds=None) -> Path:
    rows = []
    for at in pulls:
        for kind, obj in BASE + [("pods", pod or _pod())]:
            if kinds is None or kind in kinds:
                rows.append(_object(at, kind, obj))
    _write(tmp_path / itbench.OBJECTS_FILE, rows)
    _write(tmp_path / itbench.EVENTS_FILE, list(events))
    _write(tmp_path / itbench.LOGS_FILE, list(logs))
    return tmp_path


PULL = T0 + datetime.timedelta(minutes=30)


def test_the_events_that_existed_at_the_pull(tmp_path):
    m = datetime.timedelta(minutes=1)
    events = [
        # Before the watch: seen only when it expires after the pull, unchanged.
        _event(PULL + 40 * m, "DELETED", "old", last=T0 - 20 * m),
        # Added, and the core and events.k8s.io copies of it.
        _event(T0 + m, "ADDED", "added", last=T0 + m),
        _event(T0 + m, "ADDED", "added", last=T0 + m, api_version="events.k8s.io/v1"),
        # Added and expired before the pull.
        _event(T0 + m, "ADDED", "gone", last=T0 + m), _event(T0 + 20 * m, "DELETED", "gone", last=T0 + m),
        # Created after the pull.
        _event(PULL + m, "ADDED", "later", last=PULL + m),
        # Changed after the pull, from a state that wasn't recorded.
        _event(PULL + m, "MODIFIED", "changed", last=PULL + m),
    ]
    stats = itbench.ImportStats()
    state = itbench.import_snapshot(_snapshot(tmp_path, events=events), stats=stats)
    found = sorted(e.message for e in _replay(state).get_events(namespace="shop"))
    assert found == ["event added", "event old"]
    assert stats.events_unknown == 1


def test_log_records_before_the_current_instance_started_are_dropped(tmp_path):
    started = T0 + datetime.timedelta(minutes=10)
    logs = [_log(started - datetime.timedelta(minutes=5), "u-web", "before the restart"),
            _log(started + datetime.timedelta(minutes=1), "u-web", "Order {id} placed",
                 attrs={"id": "o-1", "code.line.number": "7", "user": "u-2"}),
            _log(PULL + datetime.timedelta(minutes=1), "u-web", "after the pull")]
    stats = itbench.ImportStats()
    state = itbench.import_snapshot(_snapshot(tmp_path, pod=_pod(started=started), logs=logs),
                                    stats=stats)
    log = _replay(state).get_logs_for_pod_and_container("web-1", "shop")
    [line] = [l for l in log.splitlines() if not l.startswith("[k8stools]")]
    assert line.endswith(" INFO Order o-1 placed user=u-2")  # template filled, code.* left out
    assert (stats.logs.records_used, stats.logs.records_other_instances) == (1, 1)


def test_a_record_goes_to_the_container_named_like_its_service_or_none(tmp_path):
    pod = _pod(containers=("web", "proxy"))
    logs = [_log(PULL - datetime.timedelta(minutes=1), "u-web", "from web", service="web"),
            _log(PULL - datetime.timedelta(minutes=1), "u-web", "from who?", service="other")]
    stats = itbench.ImportStats()
    state = _replay(itbench.import_snapshot(_snapshot(tmp_path, pod=pod, logs=logs), stats=stats))
    assert "from web" in state.get_logs_for_pod_and_container("web-1", "shop", "web")
    with pytest.raises(K8sNotRecorded):
        state.get_logs_for_pod_and_container("web-1", "shop", "proxy")
    assert stats.logs.records_unassigned == 1 and stats.logs.unassigned_pods == {"shop/web-1"}


def test_a_container_that_never_started_has_no_start_time(tmp_path):
    """StartError reports Go's zero time; it is not a 55-year run, live or imported."""
    terminated = ContainerStateTerminated(
        exit_code=128, reason="StartError", finished_at=T0,
        started_at=datetime.datetime(1970, 1, 1, tzinfo=UTC))
    assert terminated.started_at is None and terminated.ran_for is None

    state = {"waiting": {"reason": "CrashLoopBackOff"}}
    pod = _pod(state=state)
    pod["status"]["containerStatuses"][0]["restartCount"] = 5
    pod["status"]["containerStatuses"][0]["lastState"] = {"terminated": {
        "exitCode": 128, "reason": "StartError", "startedAt": "1970-01-01T00:00:00Z",
        "finishedAt": _api(PULL - datetime.timedelta(minutes=2))}}
    logs = [_log(PULL - datetime.timedelta(minutes=10), "u-web", "an earlier instance")]
    replayed = _replay(itbench.import_snapshot(_snapshot(tmp_path, pod=pod, logs=logs)))
    [status] = replayed.get_pod_container_statuses("web-1", "shop").containers
    assert status.last_state.ran_for is None
    log = replayed.get_logs_for_pod_and_container("web-1", "shop")
    assert "an earlier instance" not in log
    with pytest.raises(K8sNotRecorded, match="previous instance"):  # never imported
        replayed.get_logs_for_pod_and_container("web-1", "shop", previous=True)


def test_a_pull_without_pods_cant_be_imported(tmp_path):
    with pytest.raises(itbench.SnapshotImportError, match="no batch of pods"):
        itbench.import_snapshot(_snapshot(tmp_path, kinds={"namespaces", "nodes"}))


def test_a_directory_that_isnt_a_snapshot(tmp_path):
    with pytest.raises(itbench.SnapshotImportError, match="not an ITBench snapshot"):
        itbench.read_pulls(tmp_path)


def test_an_import_reaches_no_real_cluster(tmp_path, monkeypatch):
    """Everything goes through the snapshot's client: a binding to a real cluster
    would fail loudly here."""
    from k8stools import k8s_tools
    monkeypatch.setattr(k8s_tools, "_resolve_binding",
                        lambda *a: pytest.fail("an import tried to bind to a cluster"))
    monkeypatch.setattr(k8s_tools, "_BINDING", None)
    itbench.import_snapshot(_snapshot(tmp_path))
    assert k8s_tools._BINDING is None
