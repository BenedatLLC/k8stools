"""Tests for NodeSummary.conditions_since: node condition transition times (issue #9).

No cluster needed: these are fake API objects.
"""

import datetime
import json
from types import SimpleNamespace

import pytest

from k8stools import k8s_tools
from k8stools.mock_state import CAPTURE_VERSION, MockState, encode_model

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC).replace(microsecond=0)


def _ago(**kw):
    """Sampled per call: NOW is fixed at import, long before a test in a full run."""
    return datetime.datetime.now(UTC) - datetime.timedelta(**kw)


def _condition(type_, status, transition=None):
    return SimpleNamespace(type=type_, status=status, last_transition_time=transition)


def _node(*conditions):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="minikube", labels={},
                                 creation_timestamp=NOW - datetime.timedelta(days=167)),
        spec=SimpleNamespace(taints=None),
        status=SimpleNamespace(conditions=list(conditions), addresses=[],
                               node_info=None, capacity=None, allocatable=None))


@pytest.fixture
def serve(monkeypatch):
    def install(*nodes):
        monkeypatch.setattr(k8s_tools, "K8S", SimpleNamespace(
            list_node=lambda: SimpleNamespace(items=list(nodes))))
        return k8s_tools.get_node_summaries()
    return install


def _close(td, **kw):
    """Within a few seconds: the test and the tool each sample their own now()."""
    return abs(td - datetime.timedelta(**kw)) < datetime.timedelta(seconds=5)


def test_each_condition_reports_the_time_since_it_changed(serve):
    [node] = serve(_node(
        _condition("Ready", "True", _ago(days=3, hours=20)),
        _condition("MemoryPressure", "False", _ago(minutes=12))))
    assert node.conditions == {"Ready": "True", "MemoryPressure": "False"}  # unchanged
    assert set(node.conditions_since) == {"Ready", "MemoryPressure"}
    assert _close(node.conditions_since["Ready"], days=3, hours=20)
    assert _close(node.conditions_since["MemoryPressure"], minutes=12)


def test_a_condition_without_a_transition_time_is_left_out(serve):
    [node] = serve(_node(_condition("Ready", "True", _ago(hours=1)),
                         _condition("DiskPressure", "False")))
    assert "DiskPressure" in node.conditions
    assert set(node.conditions_since) == {"Ready"}


def test_a_node_without_conditions_has_an_empty_map(serve):
    [node] = serve(_node())
    assert node.conditions == {} and node.conditions_since == {}


def test_values_are_whole_second_durations(serve):
    [node] = serve(_node(_condition(
        "Ready", "True", _ago(seconds=90, microseconds=400000))))
    assert node.conditions_since["Ready"].microseconds == 0


def test_a_node_with_a_ready_time_says_it_is_not_uptime(serve):
    """On benben, a minikube stop/start (Rebooted event, every container
    restarted) left Ready's lastTransitionTime at the node's creation, 167 days
    earlier. Read naively, "Ready for 167 days" supports exactly the "broken
    since it was deployed" misreading issue #9 set out to prevent, so the tool
    description has to say so and point at what does date a node's start."""
    [node] = serve(_node(_condition("Ready", "True", _ago(days=167))))
    assert node.notes == [k8s_tools.NOTE_NODE_READY]
    assert "isn't uptime" in node.notes[0] and "Rebooted" in node.notes[0]
    [no_times] = serve(_node(_condition("Ready", "True")))
    assert no_times.notes == []


# --- capture and replay --------------------------------------------------------

def _capture(nodes):
    return {"version": CAPTURE_VERSION, "captured_at": NOW.isoformat(),
            "redacted": False, "nodes": nodes}


def _summary(**conditions_since):
    return k8s_tools.NodeSummary(
        name="minikube", status="Ready", roles=["control-plane"],
        age=datetime.timedelta(days=167), version="v1.34.0",
        conditions={k: "True" for k in conditions_since},
        conditions_since=conditions_since)


def test_conditions_since_is_stored_in_seconds():
    record = encode_model(_summary(Ready=datetime.timedelta(days=3)), NOW)
    assert record["conditions_since_seconds"] == {"Ready": 3 * 86400}
    assert "conditions_since" not in record


def test_conditions_since_advances_on_replay_like_age():
    """An age, so it moves with the replay clock. A frozen clock can't tell an
    age from a fixed span (elapsed time is zero), so replay an hour in."""
    record = encode_model(_summary(Ready=datetime.timedelta(days=3)), NOW)
    later = datetime.datetime.now(UTC) - datetime.timedelta(hours=1)
    state = MockState(json.loads(json.dumps(_capture([record]))), frozen=False,
                      server_start_time=later)
    [node] = state.get_node_summaries()
    assert node.conditions_since["Ready"] == datetime.timedelta(days=3, hours=1)
    assert node.age - node.conditions_since["Ready"] == \
        datetime.timedelta(days=167) - datetime.timedelta(days=3)


def test_a_capture_from_before_2_3_0_replays_an_empty_map():
    state = MockState(_capture([{
        "name": "minikube", "status": "Ready", "roles": ["control-plane"],
        "age_seconds": 86400.0, "version": "v1.34.0",
        "conditions": {"Ready": "True"}}]), frozen=True)
    [node] = state.get_node_summaries()
    assert node.conditions == {"Ready": "True"}
    assert node.conditions_since == {}
