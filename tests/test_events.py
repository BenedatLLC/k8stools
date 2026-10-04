"""Tests for event aggregation: EventSummary.count and first_seen (issue #8).

Kubernetes combines repeats of an event into one record. core/v1 events carry
that in count / firstTimestamp / lastTimestamp; events recorded through
events.k8s.io/v1 reach the core/v1 view with those unset, and carry it in
eventTime and series instead. No cluster needed: these are fake API objects.
"""

import datetime
import json
from types import SimpleNamespace

import pytest

from k8stools import k8s_tools
from k8stools.capture import _capture_events, _Redactor, CaptureStats
from k8stools.mock_state import CAPTURE_VERSION, MockState

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC).replace(microsecond=0)


def _event(reason="Created", *, count=None, first=None, last=None, event_time=None,
           series=None, name="ad-1", kind="Pod"):
    return SimpleNamespace(
        type="Normal", reason=reason, message=f"{reason} message",
        involved_object=SimpleNamespace(name=name, kind=kind),
        count=count, first_timestamp=first, last_timestamp=last,
        event_time=event_time, series=series)


def _ago(**kw):
    """Sampled per call: NOW is fixed at import, long before a test in a full run."""
    return datetime.datetime.now(UTC) - datetime.timedelta(**kw)


class _FakeCore:
    def __init__(self, events):
        self.events = events

    def list_namespaced_event(self, namespace, field_selector=None):
        return SimpleNamespace(items=self.events)

    def list_event_for_all_namespaces(self, field_selector=None):
        return SimpleNamespace(items=self.events)


@pytest.fixture
def serve(monkeypatch):
    """Serve fake events to both event tools; returns what each tool made of them."""
    def install(*events):
        monkeypatch.setattr(k8s_tools, "K8S", _FakeCore(list(events)))
        return (k8s_tools.get_pod_events("ad-1", "default"),
                k8s_tools.get_events(namespace="default"))
    return install


def _only(results):
    """Both tools must agree; return the one event."""
    pod_events, events = results
    assert len(pod_events) == len(events) == 1
    a, b = pod_events[0], events[0]
    assert (a.count, a.first_seen, a.last_seen) == (b.count, b.first_seen, b.last_seen)
    return b


def _close(td, **kw):
    """Within a few seconds: the test and the tool each sample their own now()."""
    return abs(td - datetime.timedelta(**kw)) < datetime.timedelta(seconds=5)


# --- core/v1 aggregation -------------------------------------------------------

def test_aggregated_event_keeps_count_and_window(serve):
    e = _only(serve(_event(count=579, first=_ago(days=2, hours=8), last=_ago(minutes=2))))
    assert e.count == 579
    assert _close(e.first_seen, days=2, hours=8)
    assert _close(e.last_seen, minutes=2)


def test_restart_rate_is_readable_from_one_record(serve):
    """The motivating case from the issue, with benben's numbers: ad restarted
    579 times in 2d8h15m, i.e. every ~5.8 minutes - not "every minute"."""
    e = _only(serve(_event(count=579, first=_ago(days=2, hours=8, minutes=17),
                           last=_ago(minutes=2))))
    per_restart = (e.first_seen - e.last_seen) / e.count
    assert datetime.timedelta(minutes=5, seconds=30) < per_restart < datetime.timedelta(minutes=6)


# --- events.k8s.io/v1 in the core/v1 view --------------------------------------

def test_series_fills_in_when_the_deprecated_fields_are_empty(serve):
    series = SimpleNamespace(count=12, last_observed_time=_ago(minutes=1))
    e = _only(serve(_event(event_time=_ago(hours=1), series=series)))
    assert e.count == 12
    assert _close(e.first_seen, hours=1)
    assert _close(e.last_seen, minutes=1)


def test_a_single_events_k8s_io_event_counts_once_and_has_a_time(serve):
    """No series means observed once. last_seen used to be None here, although
    the event's time was right there in eventTime."""
    e = _only(serve(_event(reason="Scheduled", event_time=_ago(minutes=10))))
    assert e.count == 1
    assert _close(e.first_seen, minutes=10) and _close(e.last_seen, minutes=10)


def test_the_deprecated_fields_win_when_both_are_set(serve):
    series = SimpleNamespace(count=3, last_observed_time=_ago(hours=5))
    e = _only(serve(_event(count=40, first=_ago(hours=2), last=_ago(minutes=1),
                           event_time=_ago(hours=9), series=series)))
    assert e.count == 40
    assert _close(e.first_seen, hours=2) and _close(e.last_seen, minutes=1)


def test_an_event_with_no_times_or_count_stays_unknown(serve):
    e = _only(serve(_event()))
    assert (e.count, e.first_seen, e.last_seen) == (None, None, None)


def test_docstrings_warn_that_backoff_counts_are_not_restarts():
    """BackOff is emitted repeatedly while waiting (2561 BackOff vs 579 Created
    for one pod on benben), so reading it as restarts overstates the rate ~4x.
    The docstring is the MCP tool description, so the warning has to be there."""
    for tool in (k8s_tools.get_pod_events, k8s_tools.get_events):
        assert '"BackOff"' in tool.__doc__ and '"Created"' in tool.__doc__
        assert "first_seen" in tool.__doc__ and "count" in tool.__doc__


def test_docstrings_warn_that_records_lag_behind_what_they_count():
    """client-go counts an occurrence before its spam filter decides whether to
    write it, and the filter budgets writes per object and event type (burst 25,
    then one per 5 minutes). On a real crash loop a Created record's last_seen
    read 29 minutes while the container had restarted 46s earlier, then its
    count jumped 595 -> 601. The docstring has to send agents to the container
    status for the time of the last restart."""
    for tool in (k8s_tools.get_pod_events, k8s_tools.get_events):
        doc = tool.__doc__
        assert "lag" in doc and "every 5 minutes" in doc
        assert "last_state.finished_at" in doc and "PodSummary.last_restart" in doc
        assert "don't compare counts across" in doc


# --- capture and replay --------------------------------------------------------

def _capture(events):
    return {"version": CAPTURE_VERSION, "captured_at": NOW.isoformat(),
            "redacted": False, "events": events}


def test_count_and_first_seen_round_trip_and_first_seen_ages(monkeypatch):
    monkeypatch.setattr(k8s_tools, "get_events", lambda namespace=None: [
        k8s_tools.EventSummary(last_seen=datetime.timedelta(minutes=2),
                               first_seen=datetime.timedelta(days=2),
                               count=579, type="Normal", reason="Created",
                               object="Pod/ad-1", message="Container created")])
    records = _capture_events("default", NOW, _Redactor(False, CaptureStats()))
    assert records[0]["count"] == 579
    assert records[0]["first_seen_seconds"] == 2 * 86400

    later = datetime.datetime.now(UTC) - datetime.timedelta(hours=1)
    state = MockState(json.loads(json.dumps(_capture(records))), frozen=False,
                      server_start_time=later)
    for e in (state.get_events()[0], state.get_pod_events("ad-1", "default")[0]):
        assert e.count == 579
        # An age, advanced like last_seen - the window between them is unchanged.
        assert e.first_seen == datetime.timedelta(days=2, hours=1)
        assert e.first_seen - e.last_seen == datetime.timedelta(days=2) - datetime.timedelta(minutes=2)


def test_a_capture_from_before_2_2_0_replays_the_new_fields_as_none():
    state = MockState(_capture([{
        "last_seen_seconds": 120.0, "type": "Warning", "reason": "BackOff",
        "namespace": "default", "involved_kind": "Pod", "involved_name": "ad-1",
        "object": "Pod/ad-1", "message": "Back-off restarting failed container"}]),
        frozen=True)
    e = state.get_events()[0]
    assert (e.count, e.first_seen) == (None, None)
    assert e.last_seen == datetime.timedelta(seconds=120)
