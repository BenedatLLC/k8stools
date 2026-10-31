"""Which pods the workload report reads logs from, and how much of each line (#35).

The case is k8srca's itbench-80: Kafka runs two replicas, each a single-node
KRaft controller, and both are healthy, so the report showed one pod's log
(skk8d). Its 9,024 `TOPIC_ALREADY_EXISTS` lines are 447 characters with the
word at character 382, past the 300-character cut; the other pod (xclnk) has
the 83 lines that matter, about the `orders` topic, and wasn't searched.
"""

import datetime
import json
from types import SimpleNamespace as NS

import pytest

from k8stools import k8s_tools, mock_tools
from k8stools.composites import _MAX_GREP_PODS, _OTHER_LOG_LINES, _OTHER_LOG_PODS, workload_report
from k8stools.k8s_tools import (LOG_LINE_CHARS, ContainerStateRunning, ContainerStatus,
                                PodContainerStatuses, _clip)
from k8stools.mock_state import CAPTURE_VERSION, MockState, encode_model

UTC = datetime.timezone.utc
CAPTURED = datetime.datetime(2025, 12, 15, 18, 0, 0, tzinfo=UTC)
STARTED = CAPTURED - datetime.timedelta(hours=1)
NOISE = ("INFO [QuorumController id=1] CreateTopics result(s): CreatableTopic(name="
         "'__consumer_offsets', numPartitions=50, replicationFactor=1, assignments=[], configs=["
         "CreatableTopicConfig(name='compression.type', value='producer'), CreatableTopicConfig("
         "name='cleanup.policy', value='compact'), CreatableTopicConfig(name='segment.bytes', "
         "value='104857600')]): TOPIC_ALREADY_EXISTS (Topic '__consumer_offsets' already exists.)")
ORDERS = ("INFO [QuorumController id=1] CreateTopics result(s): CreatableTopic(name='orders', "
          "numPartitions=1, replicationFactor=1, assignments=[], configs=[]): "
          "TOPIC_ALREADY_EXISTS (Topic 'orders' already exists.)")
SENT = "INFO Sent auto-creation request for Set(__consumer_offsets) to the active controller."


def _stamp(t: datetime.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z"


def _log(n: int, line, first=STARTED) -> str:
    """``n`` lines a second apart; ``line(i)`` is the message of line i."""
    return "\n".join(f"{_stamp(first + datetime.timedelta(seconds=i))} {line(i)}"
                     for i in range(n))


def _pod(name: str, log: str, starts=True, restarts=0):
    status = encode_model(ContainerStatus(
        pod_name=name, namespace="otel-demo", container_name="kafka", image="kafka:1",
        ready=True, restart_count=restarts, started=True, stop_signal=None,
        state=ContainerStateRunning(started_at=STARTED), last_state=None, volume_mounts=[],
        resource_requests={}, resource_limits={}, allocated_resources={}), CAPTURED)
    record = {"summary": {"name": name, "namespace": "otel-demo", "total_containers": 1,
                          "ready_containers": 1, "restarts": restarts,
                          "last_restart_seconds": None, "age_seconds": 3600.0,
                          "owner": "ReplicaSet/kafka-1"},
              "labels": {}, "container_statuses": [status], "init_container_statuses": [],
              "spec": {"containers": [{"name": "kafka", "image": "kafka:1"}]},
              "logs": {"kafka": log}}
    if starts is True:
        record["log_starts"] = {"kafka": log.split(" ", 1)[0]}
    elif starts:
        record["log_starts"] = {"kafka": starts}
    return record


def _state(*pods) -> MockState:
    n = len(pods)
    return MockState(json.loads(json.dumps({
        "version": CAPTURE_VERSION, "captured_at": CAPTURED.isoformat(), "redacted": False,
        "pods": list(pods),
        "deployments": [{"name": "kafka", "namespace": "otel-demo", "total_replicas": n,
                         "ready_replicas": n, "up_to_date_relicas": n, "available_replicas": n,
                         "age_seconds": 3600.0}],
        "replicasets": [{"name": "kafka-1", "namespace": "otel-demo",
                         "owner_deployment": "kafka", "revision": 1, "desired_replicas": n,
                         "current_replicas": n, "ready_replicas": n, "images": ["kafka:1"],
                         "age_seconds": 3600.0}]})),
        frozen=True, server_start_time=CAPTURED)


@pytest.fixture
def kafka():
    """skk8d: the controller, all noise; xclnk: sends requests, and 83 lines about
    `orders`. Both are healthy, so the tie goes to skk8d, listed first."""
    return _state(
        _pod("kafka-1-skk8d", _log(2000, lambda i: NOISE)),
        _pod("kafka-1-xclnk", _log(2000, lambda i: ORDERS if i % 24 == 5 else SENT)))


# --- the cut keeps the match ------------------------------------------------------------

def test_a_cut_line_keeps_its_timestamp_and_the_match():
    line = f"{_stamp(STARTED)} {NOISE}"
    at = line.index("TOPIC_ALREADY_EXISTS")
    assert len(line) > LOG_LINE_CHARS and at > LOG_LINE_CHARS
    clipped = _clip(line, (at, at + len("TOPIC_ALREADY_EXISTS")))
    assert clipped.startswith(_stamp(STARTED) + " ... ")
    assert "TOPIC_ALREADY_EXISTS (Topic '__consumer_offsets'" in clipped
    assert len(clipped) <= LOG_LINE_CHARS + len(" ...")
    assert clipped.endswith("already exists.)")  # the window reaches the line's end


def test_a_match_in_the_middle_has_text_on_both_sides():
    line = f"{_stamp(STARTED)} " + "a" * 400 + " NEEDLE " + "b" * 400
    at = line.index("NEEDLE")
    clipped = _clip(line, (at, at + 6))
    assert " aaa" in clipped and "NEEDLE bbb" in clipped and clipped.endswith(" ...")
    assert len(clipped) == LOG_LINE_CHARS + len(" ...")


def test_a_match_inside_the_cut_or_no_match_cuts_from_the_start():
    line = f"{_stamp(STARTED)} NEEDLE " + "x" * 1000
    assert _clip(line, (31, 37)) == line[:LOG_LINE_CHARS] + " ..."
    assert _clip(line) == line[:LOG_LINE_CHARS] + " ..."
    assert _clip("short", (0, 5)) == "short"


def test_the_pattern_summary_s_samples_show_the_match(kafka):
    s = kafka.get_log_pattern_summary("kafka-1-skk8d", "otel-demo", pattern="TOPIC_ALREADY_EXISTS")
    assert s.matches == 2000 and s.samples
    assert all("TOPIC_ALREADY_EXISTS" in line for line in s.samples)
    # A bad regex is matched as a substring, and its match is kept too.
    s = kafka.get_log_pattern_summary("kafka-1-skk8d", "otel-demo", pattern="EXISTS (Topic")
    assert all("EXISTS (Topic" in line for line in s.samples)


# --- grep searches every pod ---------------------------------------------------------

def test_grep_searches_every_pod_and_counts_each(kafka):
    report = workload_report(kafka, "kafka", "otel-demo", grep="TOPIC_ALREADY_EXISTS",
                             log_lines=3)
    assert [(t.pod, t.matches, t.searched_lines) for t in report.logs] == [
        ("kafka-1-skk8d", 2000, 2000), ("kafka-1-xclnk", 84, 2000)]
    skk8d, xclnk = report.logs
    assert all("TOPIC_ALREADY_EXISTS" in line for line in skk8d.lines)
    assert len(xclnk.lines) == 3 and all("name='orders'" in line for line in xclnk.lines)
    assert any(n.startswith("containers are from kafka-1-skk8d, of 2 pods") for n in report.notes)


def test_a_pod_without_matches_says_0():
    state = _state(_pod("kafka-1-a", _log(10, lambda i: NOISE)),
                   _pod("kafka-1-b", _log(10, lambda i: SENT)))
    logs = workload_report(state, "kafka", "otel-demo", grep="TOPIC_ALREADY_EXISTS").logs
    assert [(t.pod, t.matches, t.lines) for t in logs][1] == ("kafka-1-b", 0, [])
    assert "matches" in logs[1].model_dump()  # 0 is kept, not dropped as empty


def test_samples_share_the_report_s_line_limit():
    """The focus pod's samples come first; past 100 lines, the others' counts
    remain without samples."""
    state = _state(*[_pod(f"kafka-1-{c}", _log(300, lambda i: ORDERS)) for c in "abc"])
    logs = workload_report(state, "kafka", "otel-demo", grep="orders", log_lines=60).logs
    assert [(t.pod, t.matches, len(t.lines)) for t in logs] == [
        ("kafka-1-a", 300, 60), ("kafka-1-b", 300, 40), ("kafka-1-c", 300, 0)]
    assert any("none shown: the report's 100-line limit" in n for n in logs[2].notes)


def test_pods_past_the_limit_are_named_not_searched():
    n = _MAX_GREP_PODS + 2
    state = _state(*[_pod(f"kafka-1-p{i:02d}", _log(5, lambda j: ORDERS)) for i in range(n)])
    report = workload_report(state, "kafka", "otel-demo", grep="orders", log_lines=1)
    assert len({t.pod for t in report.logs}) == _MAX_GREP_PODS
    [note] = [x for x in report.notes if "weren't searched" in x]
    assert note.startswith("Logs of 2 pods (kafka-1-p10, kafka-1-p11) weren't searched")


def test_the_focus_pod_is_the_one_in_trouble_and_goes_first():
    state = _state(_pod("kafka-1-a", _log(5, lambda i: ORDERS)),
                   _pod("kafka-1-b", _log(5, lambda i: ORDERS), restarts=2))
    logs = workload_report(state, "kafka", "otel-demo", grep="orders").logs
    assert logs[0].pod == "kafka-1-b" and logs[-1].pod == "kafka-1-a"


# --- without grep: a short tail of the others -------------------------------------------

def test_the_other_pods_get_a_short_tail(kafka):
    report = workload_report(kafka, "kafka", "otel-demo", log_lines=20)
    assert [(t.pod, len(t.lines)) for t in report.logs] == [
        ("kafka-1-skk8d", 20), ("kafka-1-xclnk", _OTHER_LOG_LINES)]
    assert not any("numPartitions=50" in line for line in report.logs[1].lines)  # it logs something else
    assert not any("No log lines from" in n for n in report.notes)


def test_pods_past_the_tail_limit_are_named():
    n = _OTHER_LOG_PODS + 3
    state = _state(*[_pod(f"kafka-1-p{i}", _log(10, lambda j: SENT)) for i in range(n)])
    report = workload_report(state, "kafka", "otel-demo", log_lines=20)
    assert len(report.logs) == 1 + _OTHER_LOG_PODS
    assert sum(len(t.lines) for t in report.logs) == 10 + _OTHER_LOG_PODS * _OTHER_LOG_LINES
    [note] = [x for x in report.notes if x.startswith("No log lines from")]
    assert "2 pods (kafka-1-p6, kafka-1-p7)" in note


def test_a_one_pod_workload_is_unchanged():
    report = workload_report(_state(_pod("kafka-1-a", _log(50, lambda i: SENT))),
                             "kafka", "otel-demo")
    assert [(t.pod, len(t.lines)) for t in report.logs] == [("kafka-1-a", 20)]
    assert not any("of 1 pods" in n or "No log lines" in n for n in report.notes)


# --- where a log starts, as fields -------------------------------------------------------

def test_lines_from_and_log_starts(kafka):
    [focus, other] = workload_report(kafka, "kafka", "otel-demo", log_lines=20).logs
    assert focus.lines_from.replace(microsecond=0) == STARTED + datetime.timedelta(seconds=1980)
    assert focus.log_starts.replace(microsecond=0) == STARTED
    # Shown whole: the two are equal.
    whole = workload_report(_state(_pod("kafka-1-a", _log(10, lambda i: SENT))), "kafka",
                            "otel-demo").logs[0]
    assert whole.lines_from == whole.log_starts


def test_a_start_before_the_recorded_lines_is_the_log_s():
    earlier = _stamp(STARTED - datetime.timedelta(days=3))
    [tail] = workload_report(_state(_pod("kafka-1-a", _log(10, lambda i: SENT), starts=earlier)),
                             "kafka", "otel-demo").logs
    assert tail.log_starts.replace(microsecond=0) == STARTED - datetime.timedelta(days=3)


def test_an_unrecorded_start_is_unknown_not_the_first_line():
    state = _state(_pod("kafka-1-a", _log(10, lambda i: SENT), starts=False))
    [tail] = workload_report(state, "kafka", "otel-demo").logs
    assert tail.lines_from is not None and tail.log_starts is None
    assert "log_starts" not in tail.model_dump(exclude_none=True)
    [tail] = workload_report(state, "kafka", "otel-demo", grep="Sent").logs
    assert tail.searched_from is not None and tail.log_starts is None


def test_grep_reports_where_the_log_starts(kafka):
    tail = workload_report(kafka, "kafka", "otel-demo", grep="orders").logs[1]
    assert tail.log_starts.replace(microsecond=0) == STARTED
    assert tail.lines_from is None  # matches are spread, not a run from a line


def test_replay_moves_log_starts_with_the_lines():
    pods = [_pod("kafka-1-a", _log(10, lambda i: SENT))]
    data = json.loads(json.dumps(_state(*pods)._data))
    later = CAPTURED + datetime.timedelta(days=30)
    [tail] = workload_report(MockState(data, frozen=True, server_start_time=later),
                             "kafka", "otel-demo").logs
    assert tail.log_starts.replace(microsecond=0) == STARTED + datetime.timedelta(days=30)
    assert tail.lines_from == tail.log_starts


# --- get_log_start: live, mock_tools ------------------------------------------------------

def test_live_get_log_start_reads_the_first_line(monkeypatch):
    log = _log(1000, lambda i: SENT) + "\n"
    calls = []

    def read(name, namespace, container=None, **kw):
        calls.append(kw)
        return NS(data=log.encode()[:kw.get("limit_bytes") or None])
    monkeypatch.setattr(k8s_tools, "K8S", NS(read_namespaced_pod_log=read))
    start = k8s_tools.get_log_start("p", "otel-demo", "kafka")
    assert start.replace(microsecond=0) == STARTED
    assert calls == [{"follow": False, "_preload_content": False, "timestamps": True,
                      "limit_bytes": k8s_tools._START_BYTES}]
    monkeypatch.setattr(k8s_tools, "K8S", NS(read_namespaced_pod_log=lambda *a, **kw: NS(data=b"")))
    assert k8s_tools.get_log_start("p", "otel-demo", "kafka") is None


def test_get_log_start_is_not_a_tool_but_replays():
    assert k8s_tools.get_log_start not in k8s_tools.TOOLS
    assert mock_tools.get_log_start not in mock_tools.TOOLS
    mock_tools.load_mock_state(frozen=True)
    assert mock_tools.get_log_start("ad-647b4947cc-s5mpm", "default", "ad") is not None
