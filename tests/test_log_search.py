"""Searching a log over its whole retained span (#34).

The default tail is 1000 lines, about 90 seconds of a busy proxy, and
`get_workload_report(grep=...)` searched only that tail, so evidence earlier in
the log was invisible and a grep that found nothing read as nothing in the log.
The case is k8srca's itbench-31: frontend-proxy's 10,000 lines from 17:42:19 to
17:57:16 hold 15 `503 UC`, about one a minute, with none between 17:48:41 and
17:54:11, where a NetworkPolicy was created at 17:51:21. Any short tail shows
503s only after the policy.
"""

import datetime
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from k8stools import capture, k8s_tools
from k8stools.capture import capture_state
from k8stools.composites import workload_report
from k8stools.k8s_tools import (ContainerStateRunning, ContainerStatus, K8sLogReclaimed,
                                K8sNotRecorded, PodContainerStatuses)
from k8stools.mock_state import CAPTURE_VERSION, NOTE_LOGS_FROM_OTEL, MockState, encode_model

from test_capture import _FakeCluster

UTC = datetime.timezone.utc
EXCERPT = Path(__file__).parent / "fixtures" / "itbench-33-excerpt"
FIRST = datetime.datetime(2025, 12, 15, 17, 42, 19, tzinfo=UTC)
LAST = datetime.datetime(2025, 12, 15, 17, 57, 16, tzinfo=UTC)
CAPTURED = datetime.datetime(2025, 12, 15, 17, 57, 20, tzinfo=UTC)
STARTED = datetime.datetime(2025, 12, 15, 17, 42, 10, tzinfo=UTC)
ERRORS = ["17:42:25", "17:43:30", "17:44:31", "17:45:40", "17:46:33", "17:47:35",
          "17:48:10", "17:48:41", "17:54:11", "17:55:00", "17:55:50", "17:56:20",
          "17:56:45", "17:57:00", "17:57:10"]


def _stamp(t: datetime.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z"


def _proxy_log(n: int = 10_000) -> str:
    step = (LAST - FIRST) / (n - 1)
    times = [FIRST + i * step for i in range(n)]
    errors = {min(range(n), key=lambda i: abs(times[i] - datetime.datetime.combine(
        FIRST.date(), datetime.time.fromisoformat(e), UTC))) for e in ERRORS}
    return "\n".join(
        f"{_stamp(t)} [{t.isoformat()}] \"GET /api/cart HTTP/1.1\" "
        + ("503 UC upstream_reset_before_response_started" if i in errors else "200 -")
        for i, t in enumerate(times))


def _status(started=STARTED):
    return encode_model(ContainerStatus(
        pod_name="frontend-proxy-1-a", namespace="otel-demo", container_name="frontend-proxy",
        image="envoy:1", ready=True, restart_count=0, started=True, stop_signal=None,
        state=ContainerStateRunning(started_at=started), last_state=None, volume_mounts=[],
        resource_requests={}, resource_limits={}, allocated_resources={}), CAPTURED)


def _state(log: str, starts=None, started=STARTED) -> MockState:
    """A capture of frontend-proxy; ``starts`` is the recorded log start (None:
    a capture from before 4.6.0, which didn't record one)."""
    pod = {"summary": {"name": "frontend-proxy-1-a", "namespace": "otel-demo",
                       "total_containers": 1, "ready_containers": 1, "restarts": 0,
                       "last_restart_seconds": None, "age_seconds": 900.0,
                       "owner": "ReplicaSet/frontend-proxy-1"},
           "labels": {}, "container_statuses": [_status(started)],
           "init_container_statuses": [], "spec": {"containers": [{"name": "frontend-proxy"}]},
           "logs": {"frontend-proxy": log}}
    if starts is not None:
        pod["log_starts"] = {"frontend-proxy": starts}
    return MockState(json.loads(json.dumps({
        "version": CAPTURE_VERSION, "captured_at": CAPTURED.isoformat(), "redacted": False,
        "pods": [pod],
        "deployments": [{"name": "frontend-proxy", "namespace": "otel-demo",
                         "total_replicas": 1, "ready_replicas": 1, "up_to_date_relicas": 1,
                         "available_replicas": 1, "age_seconds": 900.0}],
        "replicasets": [{"name": "frontend-proxy-1", "namespace": "otel-demo",
                         "owner_deployment": "frontend-proxy", "revision": 1,
                         "desired_replicas": 1, "current_replicas": 1, "ready_replicas": 1,
                         "images": ["envoy:1"], "age_seconds": 900.0}]})),
        frozen=True, server_start_time=CAPTURED)


@pytest.fixture
def proxy():
    log = _proxy_log()
    return _state(log, starts=log.split(" ", 1)[0])


def _summary(state, pattern="503 UC", **kw):
    return state.get_log_pattern_summary("frontend-proxy-1-a", "otel-demo", pattern=pattern, **kw)


# --- the pattern summary -------------------------------------------------------------

def test_the_whole_log_is_searched_and_the_gap_shows(proxy):
    s = _summary(proxy)
    assert (s.lines, s.matches) == (10_000, 15)
    assert (s.first_line.replace(microsecond=0), s.last_line.replace(microsecond=0)) == (FIRST, LAST)
    assert s.first_match.strftime("%H:%M") == "17:42" and s.last_match.strftime("%H:%M") == "17:57"
    assert s.bucket_seconds == 60
    # One entry per minute with a 503, and the gap as one run of empty buckets.
    zero_runs = [b for b in s.buckets if " to " in b]
    assert [b.split(" 0/")[0] for b in zero_runs] == ["17:49 to 17:54"]
    assert s.buckets[0].startswith("17:42 1/")
    assert sum(int(b.rsplit(" ", 1)[1].split("/")[1]) for b in s.buckets) == 10_000
    assert s.notes == []  # the capture kept the log whole, and it began with the instance


def test_samples_are_the_first_the_last_and_spread_between(proxy):
    s = _summary(proxy, max_samples=3)
    assert [line.split()[1][12:17] for line in s.samples] == ["17:42", "17:48", "17:57"]
    assert len(_summary(proxy, max_samples=500).samples) == 15
    assert _summary(proxy, max_samples=0).samples == []


def test_lines_are_cut_like_the_report_s():
    long = f"{_stamp(FIRST)} 503 UC " + "x" * 1000
    [line] = _summary(_state(long, starts=_stamp(FIRST))).samples
    assert len(line) == k8s_tools.LOG_LINE_CHARS + 4 and line.endswith(" ...")


def test_no_pattern_counts_every_line(proxy):
    s = _summary(proxy, pattern=None)
    assert s.matches == s.lines == 10_000 and s.pattern is None
    assert not any(" to " in b for b in s.buckets)


def test_a_bad_regex_is_a_substring(proxy):
    assert _summary(proxy, pattern="503 UC (").matches == 0
    assert _summary(proxy, pattern="503 uc upstream").matches == 15


def test_a_long_log_widens_its_buckets_and_says_so():
    lines = [f"{_stamp(FIRST + datetime.timedelta(minutes=10 * i))} tick" for i in range(144)]
    s = _summary(_state("\n".join(lines), starts=lines[0].split()[0]), pattern="tick")
    assert s.bucket_seconds == 1800 and len(s.buckets) <= k8s_tools.LOG_MAX_BUCKETS
    assert "bucket_seconds widened from 60 to 1800, to keep 60 buckets or fewer." in s.notes
    assert s.buckets[0].startswith("12-15T17:30 ")  # more than one day: the date too


def test_lines_without_timestamps_have_no_buckets():
    s = _summary(_state("503 UC one\nok\n503 UC two", starts=None))
    assert (s.matches, s.buckets, s.first_line) == (2, [], None)
    assert k8s_tools.NOTE_LOG_NO_TIMES in s.notes


# --- what a capture recorded --------------------------------------------------------

def test_a_capture_that_cut_the_log_says_where_it_began(proxy):
    log = _proxy_log()
    cut = _state("\n".join(log.splitlines()[-1000:]), starts=log.split(" ", 1)[0])
    s = _summary(cut)
    assert s.lines == 1000 and s.log_starts.replace(microsecond=0) == FIRST
    assert k8s_tools.NOTE_LOG_CUT_IN_CAPTURE in s.notes
    note = cut.get_logs_for_pod_and_container("frontend-proxy-1-a", "otel-demo").splitlines()[0]
    assert "this capture didn't record the lines before these" in note
    assert "2025-12-15T17:42:19Z" in note


def test_an_older_capture_doesnt_know_where_the_log_began():
    s = _summary(_state("\n".join(_proxy_log().splitlines()[-1000:])))
    assert s.log_starts is None and k8s_tools.NOTE_LOG_EXTENT_UNKNOWN in s.notes


def test_a_log_that_starts_long_after_its_instance_says_so():
    log = _proxy_log(100)
    s = _summary(_state(log, starts=log.split(" ", 1)[0],
                        started=FIRST - datetime.timedelta(hours=2)))
    assert ("The log starts 2h0m after this instance started (instance_started): the "
            "kubelet rotated earlier lines away, or the container wrote none until then; "
            "which, the API doesn't say.") in s.notes


def test_an_itbench_import_says_records_werent_collected():
    from k8stools import itbench
    state = MockState(json.loads(json.dumps(itbench.import_snapshot(EXCERPT))), frozen=True)
    s = state.get_log_pattern_summary("ad-554b849958-42mdm", "otel-demo", "ad")
    assert s.notes[0] == NOTE_LOGS_FROM_OTEL
    assert s.notes[1].endswith(k8s_tools.LATE_NOT_COLLECTED)
    assert k8s_tools.NOTE_LOG_EXTENT_UNKNOWN not in s.notes  # the import records each start


def test_replay_moves_the_times_with_everything_else():
    log = _proxy_log(100)
    later = CAPTURED + datetime.timedelta(days=300)
    state = MockState(json.loads(json.dumps(_state(log, starts=log.split(" ", 1)[0])._data)),
                      frozen=True, server_start_time=later)
    s = _summary(state)
    assert s.first_line.replace(microsecond=0) == FIRST + datetime.timedelta(days=300)
    assert s.log_starts is None and s.notes == []  # the start moved with the lines


def test_unrecorded_and_reclaimed_logs_raise():
    state = _state("unable to retrieve container logs for containerd://abc", starts=None)
    with pytest.raises(K8sLogReclaimed):
        _summary(state)
    state._data["pods"][0]["spec"]["containers"].append({"name": "sidecar"})
    with pytest.raises(K8sNotRecorded):
        state.get_log_pattern_summary("frontend-proxy-1-a", "otel-demo", "sidecar")


# --- the log tool and the workload report -------------------------------------------

def test_the_default_tail_says_where_the_log_starts(proxy):
    text = proxy.get_logs_for_pod_and_container("frontend-proxy-1-a", "otel-demo")
    note, *lines = text.splitlines()
    assert note.startswith(k8s_tools.LOG_NOTE_PREFIX + "These 1000 lines start at 2025-12-15T17:55:")
    assert note.endswith("; the log starts at 2025-12-15T17:42:19Z. "
                         "get_log_pattern_summary searches all of it.")
    assert len(lines) == 1000 and sum("503 UC" in l for l in lines) == 5


def test_no_note_when_nothing_was_left_out(proxy):
    log = _proxy_log(50)
    text = _state(log, starts=log.split(" ", 1)[0]).get_logs_for_pod_and_container(
        "frontend-proxy-1-a", "otel-demo")
    assert not text.startswith(k8s_tools.LOG_NOTE_PREFIX)
    # since_seconds leaves lines out on purpose: no note either
    text = proxy.get_logs_for_pod_and_container("frontend-proxy-1-a", "otel-demo",
                                                since_seconds=10)
    assert not text.startswith(k8s_tools.LOG_NOTE_PREFIX)


def test_the_report_greps_the_whole_log(proxy):
    report = workload_report(proxy, "frontend-proxy", "otel-demo", grep="503 UC")
    [tail] = report.logs
    assert (tail.matches, tail.searched_lines, len(tail.lines)) == (15, 10_000, 15)
    assert tail.searched_from.replace(microsecond=0) == FIRST
    assert "T17:42:2" in tail.lines[0] and "T17:57:" in tail.lines[-1]
    assert not any(n.startswith("bucket_seconds") for n in tail.notes)


def test_the_report_without_grep_tails_log_lines(proxy):
    [tail] = workload_report(proxy, "frontend-proxy", "otel-demo", log_lines=20).logs
    assert len(tail.lines) == 20 and tail.matches is None
    assert tail.notes[0].startswith("These 20 lines start at 2025-12-15T17:57:")


# --- live ---------------------------------------------------------------------------

class _Kubelet:
    """The log endpoint, applying tail_lines then limit_bytes from the start, as
    the kubelet does."""

    def __init__(self, text: str):
        self.text, self.calls = text, []

    def read_namespaced_pod_log(self, name, namespace, container=None, **kw):
        self.calls.append(kw)
        lines = self.text.splitlines(keepends=True)
        if kw.get("tail_lines") is not None:
            lines = lines[-kw["tail_lines"]:]
        body = "".join(lines).encode()
        if kw.get("limit_bytes") is not None:
            body = body[:kw["limit_bytes"]]
        return NS(data=body)


@pytest.fixture
def kubelet(monkeypatch):
    def serve(text, statuses=None):
        server = _Kubelet(text)
        monkeypatch.setattr(k8s_tools, "K8S", server)
        monkeypatch.setattr(k8s_tools, "get_pod_container_statuses", lambda pod, ns: (
            statuses or PodContainerStatuses(pod_name=pod, namespace=ns)))
        return server
    return serve


def test_live_the_summary_reads_the_whole_log(kubelet):
    server = kubelet(_proxy_log() + "\n")
    s = k8s_tools.get_log_pattern_summary("frontend-proxy-1-a", "otel-demo", "frontend-proxy",
                                          pattern="503 UC")
    assert (s.lines, s.matches) == (10_000, 15)
    assert server.calls == [{"follow": False, "_preload_content": False, "timestamps": True,
                             "limit_bytes": k8s_tools.WHOLE_LOG_BYTES}]
    assert s.notes == []


def test_live_a_log_past_the_cap_searches_its_newest_part(kubelet, monkeypatch):
    """limit_bytes keeps the oldest bytes; the newest are the ones that matter."""
    monkeypatch.setattr(k8s_tools, "WHOLE_LOG_BYTES", 500_000)
    server = kubelet(_proxy_log() + "\n")
    s = k8s_tools.get_log_pattern_summary("p", "otel-demo", "frontend-proxy", pattern="503 UC")
    assert 0 < s.lines < 10_000 and s.last_line.replace(microsecond=0) == LAST
    assert s.log_starts.replace(microsecond=0) == FIRST and s.first_line > FIRST
    assert 0 < s.matches < 15 and s.last_match.strftime("%H:%M") == "17:57"
    assert k8s_tools.NOTE_LOG_CAPPED in s.notes
    assert server.calls[1]["tail_lines"] < 10_000


def test_live_the_tail_note_says_where_the_log_starts(kubelet):
    server = kubelet(_proxy_log() + "\n")
    text = k8s_tools.get_logs_for_pod_and_container("p", "otel-demo", "frontend-proxy")
    assert text.startswith(k8s_tools.LOG_NOTE_PREFIX + "These 1000 lines start at 2025-12-15T17:55:")
    assert "the log starts at 2025-12-15T17:42:19Z" in text.splitlines()[0]
    assert server.calls[-1]["limit_bytes"] == k8s_tools._START_BYTES
    # A log shorter than the tail: no probe, no note.
    server = kubelet(_proxy_log(10) + "\n")
    assert not k8s_tools.get_logs_for_pod_and_container("p", "otel-demo").startswith("[k8stools]")
    assert len(server.calls) == 1


def test_live_a_tail_past_the_byte_cap_keeps_its_newest_lines(kubelet):
    """limit_bytes stops reading from the start of the tail, so a 1000-line tail
    over 1 MiB came back without its newest lines."""
    lines = [f"{_stamp(FIRST + datetime.timedelta(seconds=i))} line {i} " + "x" * 2000
             for i in range(1500)]
    kubelet("\n".join(lines) + "\n")
    text = k8s_tools._read_pod_log("p", "otel-demo", "c")
    assert text.splitlines()[-1].split()[2] == "1499"
    assert len(text.encode()) < k8s_tools._TAIL_BYTES


def test_live_the_report_greps_the_whole_log(kubelet, monkeypatch):
    kubelet(_proxy_log() + "\n")
    tail = __import__("k8stools.composites").composites._tail(
        k8s_tools, "p", "otel-demo", "frontend-proxy", False, 20, "503 UC")
    assert (tail.matches, tail.searched_lines) == (15, 10_000)


# --- capture ------------------------------------------------------------------------

def test_capture_records_where_each_log_starts(monkeypatch, kubelet):
    _FakeCluster().install(monkeypatch)
    log = _proxy_log(2000)
    kubelet(log + "\n")
    status = ContainerStatus(
        pod_name="p", namespace="default", container_name="c", image="c:1", ready=True,
        restart_count=0, started=True, stop_signal=None,
        state=ContainerStateRunning(started_at=STARTED), last_state=None, volume_mounts=[],
        resource_requests={}, resource_limits={}, allocated_resources={})
    monkeypatch.setattr(k8s_tools, "get_pod_summaries", lambda ns=None: [k8s_tools.PodSummary(
        name="p", namespace="default", total_containers=1, ready_containers=1, restarts=0,
        last_restart=None, age=datetime.timedelta(minutes=20))])
    monkeypatch.setattr(k8s_tools, "get_pod_container_statuses",
                        lambda pod, ns: PodContainerStatuses(pod_name=pod, namespace=ns,
                                                             containers=[status]))
    monkeypatch.setattr(k8s_tools, "get_pod_spec", lambda pod, ns: {"containers": [{"name": "c"}]})
    state = capture_state(redact=False, max_log_lines=1000)
    [pod] = state["pods"]
    assert pod["log_starts"] == {"c": log.split(" ", 1)[0]}
    replay = MockState(json.loads(json.dumps(state)), frozen=True,
                       server_start_time=datetime.datetime.fromisoformat(state["captured_at"]))
    s = replay.get_log_pattern_summary("p", "default", "c", pattern="503 UC")
    assert s.lines == 1000 and k8s_tools.NOTE_LOG_CUT_IN_CAPTURE in s.notes
