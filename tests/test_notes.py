"""Short tool descriptions and result notes (issue #20).

A tool's description is sent to the model on every turn, so descriptions are
short and warnings that apply to particular results travel in those results:
a `notes` list on the models, and marked lines at the top of a log. These tests
hold the descriptions to a budget and check that each warning the long
descriptions used to carry still reaches the agent where it applies.
"""

import datetime
import json
from types import SimpleNamespace

import pytest

from k8stools import k8s_tools, mock_tools
from k8stools.k8s_tools import (ContainerStateRunning, ContainerStateTerminated,
                                ContainerStateWaiting, ContainerStatus, LOG_NOTE_PREFIX)
from k8stools.mock_state import CAPTURE_VERSION, MockState, encode_model

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC).replace(microsecond=0)

#: Per-tool and total budgets for descriptions, in characters. 2.4.0's were
#: 61-4461 per tool and 47,946 in all, about 12,000 tokens on every turn.
DESCRIPTION_BUDGET = 700
TOTAL_DESCRIPTION_BUDGET = 6000


# --- descriptions --------------------------------------------------------------

@pytest.mark.parametrize("tool", k8s_tools.TOOLS, ids=lambda f: f.__name__)
def test_each_description_is_within_budget(tool):
    assert len(tool.__doc__) <= DESCRIPTION_BUDGET, \
        f"{tool.__name__}: {len(tool.__doc__)} chars; move detail to " \
        f"docs/TOOL_REFERENCE.md or into the result's notes"


def test_all_descriptions_together_are_within_budget():
    assert sum(len(t.__doc__) for t in k8s_tools.TOOLS) <= TOTAL_DESCRIPTION_BUDGET


def test_every_tool_is_in_the_reference():
    from pathlib import Path
    reference = (Path(__file__).parent.parent / "docs" / "TOOL_REFERENCE.md").read_text()
    for tool in k8s_tools.TOOLS:
        assert f"## `{tool.__name__}`" in reference, tool.__name__


# --- notes on models -------------------------------------------------------------

def _status(state=None, last_state=None, restart_count=0):
    return ContainerStatus(
        pod_name="ad-1", namespace="default", container_name="ad", image="ad:2",
        ready=False, restart_count=restart_count, started=False, stop_signal=None,
        state=state, last_state=last_state, volume_mounts=[], resource_requests={},
        resource_limits={}, allocated_resources={})


def _terminated(ran_for_seconds=123):
    finished = NOW - datetime.timedelta(minutes=6)
    return ContainerStateTerminated(exit_code=137, reason="OOMKilled", finished_at=finished,
                                    started_at=finished - datetime.timedelta(seconds=ran_for_seconds))


def test_a_restarted_container_says_ran_for_is_not_the_cadence():
    """2.1.0: an instance's lifetime is easily confused with the restart cadence."""
    s = _status(ContainerStateRunning(started_at=NOW), _terminated(), restart_count=67)
    assert s.notes == [k8s_tools.NOTE_CONTAINER_RAN_FOR]


def test_a_waiting_container_says_both_log_calls_return_the_same_instance():
    """2.0.4: during CrashLoopBackOff, previous=True looks ignored."""
    s = _status(ContainerStateWaiting(reason="CrashLoopBackOff"), _terminated(), restart_count=67)
    assert k8s_tools.NOTE_CONTAINER_WAITING in s.notes


def test_a_container_that_never_restarted_has_no_notes():
    assert _status(ContainerStateRunning(started_at=NOW)).notes == []
    # Waiting for its first start (ContainerCreating): no previous instance to confuse.
    assert _status(ContainerStateWaiting(reason="ContainerCreating")).notes == []


def test_a_failing_pod_with_restarts_says_what_last_restart_is():
    """2.2.1: last_restart is the last termination, not a restart. Only on a pod
    that isn't fully ready: after a node restart every pod has restarts, and the
    note on all 38 of a cluster's pods was noise."""
    fields = dict(name="p", namespace="d", total_containers=1, ready_containers=0,
                  restarts=67, last_restart=datetime.timedelta(minutes=6),
                  age=datetime.timedelta(hours=7))
    assert k8s_tools.PodSummary(**fields).notes == [k8s_tools.NOTE_POD_LAST_RESTART]
    assert k8s_tools.PodSummary(**fields | {"ready_containers": 1}).notes == []
    assert k8s_tools.PodSummary(**fields | {"restarts": 0, "last_restart": None}).notes == []


def test_notes_are_recomputed_not_taken_from_input():
    """A stale note (old wording, or one copied from another object) can't stick."""
    node = k8s_tools.NodeSummary(name="n", status="Ready", roles=[], age=datetime.timedelta(days=1),
                                 version="v1", notes=["stale"])
    assert node.notes == []


# --- notes in logs ----------------------------------------------------------------

def test_log_notes_for_a_waiting_container_and_a_kubelet_error():
    waiting = _status(ContainerStateWaiting(reason="CrashLoopBackOff"), _terminated(), 67)
    text = k8s_tools._with_log_notes("2026-10-07T00:00:00Z boom\n", [waiting], None)
    assert text.startswith(LOG_NOTE_PREFIX + "The container is waiting (CrashLoopBackOff)")
    assert text.endswith("2026-10-07T00:00:00Z boom\n")
    gone = k8s_tools._with_log_notes("unable to retrieve container logs for docker://abc",
                                     [_status(ContainerStateRunning(started_at=NOW))], "ad")
    assert gone.startswith(LOG_NOTE_PREFIX + "This is the kubelet's message")


def test_a_log_with_nothing_to_note_is_unchanged():
    running = _status(ContainerStateRunning(started_at=NOW), _terminated(), 3)
    assert k8s_tools._with_log_notes("line\n", [running], "ad") == "line\n"
    assert k8s_tools._with_log_notes("line\n", [], None) == "line\n"


class _FakeCore:
    def read_namespaced_pod_log(self, **kw):
        return SimpleNamespace(data=b"2026-10-07T00:00:00Z Terminating due to OOM\n")


@pytest.mark.parametrize("waiting", [True, False])
def test_the_live_log_tool_adds_notes_and_capture_reads_raw(monkeypatch, waiting):
    monkeypatch.setattr(k8s_tools, "K8S", _FakeCore())
    monkeypatch.setattr(k8s_tools, "_decode_log_response", lambda resp: resp.data.decode())
    state = ContainerStateWaiting(reason="CrashLoopBackOff") if waiting \
        else ContainerStateRunning(started_at=NOW)
    monkeypatch.setattr(k8s_tools, "get_pod_container_statuses",
                        lambda pod, ns: [_status(state, _terminated(), 67)])
    text = k8s_tools.get_logs_for_pod_and_container("ad-1", "default", previous=True)
    assert text.startswith(LOG_NOTE_PREFIX) == waiting
    assert text.endswith("Terminating due to OOM\n")
    # The capture stores the raw log, so replay can note the replayed state.
    assert not k8s_tools._read_pod_log("ad-1", "default", previous=True).startswith(LOG_NOTE_PREFIX)


def test_a_failed_status_lookup_still_returns_the_log(monkeypatch):
    monkeypatch.setattr(k8s_tools, "K8S", _FakeCore())
    monkeypatch.setattr(k8s_tools, "_decode_log_response", lambda resp: resp.data.decode())

    def fail(pod, ns):
        raise k8s_tools.K8sApiError("forbidden")
    monkeypatch.setattr(k8s_tools, "get_pod_container_statuses", fail)
    assert k8s_tools.get_logs_for_pod_and_container("ad-1") == \
        "2026-10-07T00:00:00Z Terminating due to OOM\n"


def test_replay_adds_notes_for_the_replayed_state():
    status = encode_model(_status(ContainerStateWaiting(reason="CrashLoopBackOff"),
                                  _terminated(), 67), NOW)
    state = MockState(json.loads(json.dumps({
        "version": CAPTURE_VERSION, "captured_at": NOW.isoformat(), "redacted": False,
        "pods": [{"summary": {"name": "ad-1", "namespace": "default"},
                  "container_statuses": [status],
                  "logs": {"ad": "2026-10-07T00:00:00Z boom\n"},
                  "previous_logs": {"ad": "2026-10-07T00:00:00Z boom\n"}}]})), frozen=True)
    for previous in (False, True):
        text = state.get_logs_for_pod_and_container("ad-1", "default", previous=previous)
        assert text.startswith(LOG_NOTE_PREFIX + "The container is waiting")


# --- captures --------------------------------------------------------------------

def test_captures_do_not_store_notes_and_old_captures_get_them():
    event = k8s_tools.EventSummary(last_seen=datetime.timedelta(minutes=1),
                                   first_seen=datetime.timedelta(days=2), count=2561,
                                   type="Warning", reason="BackOff", object="Pod/ad-1",
                                   message="Back-off restarting failed container")
    assert event.notes and "notes" not in encode_model(event, NOW)
    # A record written before notes existed replays with them.
    state = MockState({"version": CAPTURE_VERSION, "captured_at": NOW.isoformat(),
                       "redacted": False, "events": [{
                           "last_seen_seconds": 60.0, "first_seen_seconds": 172800.0,
                           "count": 2561, "type": "Warning", "reason": "BackOff",
                           "namespace": "default", "involved_kind": "Pod",
                           "involved_name": "ad-1", "object": "Pod/ad-1",
                           "message": "Back-off restarting failed container"}]}, frozen=True)
    assert state.get_events()[0].notes == event.notes


def test_the_mock_fixture_shows_the_notes():
    """--mock is where agent authors look first."""
    mock_tools.load_mock_state()
    assert mock_tools.get_node_summaries()[0].notes
    backoff = [e for e in mock_tools.get_events() if e.reason == "BackOff"][0]
    assert k8s_tools.NOTE_EVENT_BACKOFF in backoff.notes
    [ad] = mock_tools.get_pod_container_statuses("ad-647b4947cc-s5mpm", "default")
    assert ad.notes == [k8s_tools.NOTE_CONTAINER_RAN_FOR]
