"""Tests for the composite tools (issue #13): get_namespace_health, get_workload_report.

The composites are built from the tool functions, passed in as `api`. A
MockState has the same methods, so these tests hand them small captures built
for each case, plus the --mock fixture. No cluster needed.
"""

import datetime
import json

import pytest

from k8stools import composites, k8s_tools, mock_tools
from k8stools.composites import exit_meaning, namespace_health, workload_report
from k8stools.k8s_tools import (ContainerStateRunning, ContainerStateTerminated,
                                ContainerStateWaiting, ContainerStatus)
from k8stools.mock_state import CAPTURE_VERSION, MockState, encode_model

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC).replace(microsecond=0)

#: Size budgets (characters of JSON), part of the contract (#13).
NAMESPACE_BUDGET_PER_WORKLOAD = 450
REPORT_BUDGET = 8000


def _ago(**kw):
    return NOW - datetime.timedelta(**kw)


# --- fixed semantics -------------------------------------------------------------

@pytest.mark.parametrize("code,meaning", [
    (0, "success"), (1, "general error (application-defined)"),
    (137, "killed by SIGKILL (128+9)"), (143, "killed by SIGTERM (128+15)"),
    (139, "killed by SIGSEGV (128+11)"), (127, "command not found"),
    (3, "application-defined"), (None, None),
])
def test_exit_codes_have_their_fixed_meaning(code, meaning):
    assert exit_meaning(code) == meaning


def test_137_without_oomkilled_is_noted_as_a_fact_not_a_diagnosis():
    note = composites._disagreement(137, "Error")
    assert "did not attribute this kill to the out-of-memory killer" in note
    assert "liveness-probe kill" in note
    assert composites._disagreement(137, "OOMKilled") is None
    assert composites._disagreement(1, "Error") is None


# --- a capture built for the case ---------------------------------------------------

def _status(pod, container, state, last_state=None, restarts=0, ready=False, memory=("300Mi", "300Mi")):
    limit, request = memory
    return encode_model(ContainerStatus(
        pod_name=pod, namespace="default", container_name=container, image=f"{container}:1",
        ready=ready, restart_count=restarts, started=ready, stop_signal=None, state=state,
        last_state=last_state, volume_mounts=[],
        resource_requests={"memory": request} if request else {},
        resource_limits={"memory": limit} if limit else {}, allocated_resources={}), NOW)


def _crashing(pod, container, lifetime=7, gap=None, reason="Error", code=137, restarts=40):
    finished = _ago(minutes=4)
    last = ContainerStateTerminated(exit_code=code, reason=reason, finished_at=finished,
                                    started_at=finished - datetime.timedelta(seconds=lifetime))
    state = ContainerStateRunning(started_at=finished + datetime.timedelta(seconds=gap)) \
        if gap is not None else ContainerStateWaiting(reason="CrashLoopBackOff")
    return _status(pod, container, state, last, restarts)


def _deployment(name, ready, desired, age_days=8):
    return {"name": name, "namespace": "default", "total_replicas": desired,
            "ready_replicas": ready, "up_to_date_relicas": desired,
            "available_replicas": ready, "age_seconds": age_days * 86400.0}


def _replicaset(name, deployment, revision=1, hours=7):
    return {"name": name, "namespace": "default", "owner_deployment": deployment,
            "revision": revision, "desired_replicas": 1, "current_replicas": 1,
            "ready_replicas": 0, "images": [f"{deployment}:1"], "age_seconds": hours * 3600.0}


def _pod(name, owner, status, restarts=0, ready=0, spec=None, logs=None, previous=None):
    return {"summary": {"name": name, "namespace": "default", "total_containers": 1,
                        "ready_containers": ready, "restarts": restarts,
                        "last_restart_seconds": 240.0 if restarts else None,
                        "age_seconds": 86400.0, "owner": owner},
            "labels": {}, "container_statuses": [status],
            "spec": spec or {"containers": [{"name": owner.split("/")[1].rsplit("-", 1)[0] if owner else name}]},
            "logs": logs or {}, "previous_logs": previous or {}}


def _state(**keys):
    return MockState(json.loads(json.dumps({
        "version": CAPTURE_VERSION, "captured_at": NOW.isoformat(), "redacted": False,
        **keys})), frozen=True)


@pytest.fixture
def two_alike():
    """Two deployments crashing the same way, one healthy, one bare pod."""
    return _state(
        deployments=[_deployment("ad", 0, 1), _deployment("fraud", 0, 1), _deployment("cart", 1, 1)],
        replicasets=[_replicaset("ad-1", "ad"), _replicaset("fraud-1", "fraud"),
                     _replicaset("cart-1", "cart")],
        pods=[_pod("ad-1-a", "ReplicaSet/ad-1", _crashing("ad-1-a", "ad", lifetime=7), 40),
              _pod("fraud-1-a", "ReplicaSet/fraud-1", _crashing("fraud-1-a", "fraud", lifetime=2), 50),
              _pod("cart-1-a", "ReplicaSet/cart-1",
                   _status("cart-1-a", "cart", ContainerStateRunning(started_at=_ago(days=1)),
                           ready=True), ready=1),
              _pod("debug", None, _status("debug", "debug", ContainerStateRunning(started_at=_ago(hours=1)),
                                          ready=True, memory=(None, None)), ready=1)])


# --- get_namespace_health --------------------------------------------------------------

def test_unhealthy_workloads_come_first_with_their_last_termination(two_alike):
    h = namespace_health(two_alike, "default")
    assert [w.workload for w in h.workloads] == [
        "Deployment/ad", "Deployment/fraud", "Deployment/cart", "Pod/debug"]
    ad = h.workloads[0]
    assert (ad.healthy, ad.ready, ad.desired, ad.restarts) == (False, 0, 1, 40)
    t = ad.last_termination
    assert (t.exit_code, t.exit_meaning, t.reason) == (137, "killed by SIGKILL (128+9)", "Error")
    assert t.instance_lifetime == datetime.timedelta(seconds=7)
    assert t.waiting == "CrashLoopBackOff" and t.restart_gap == t.finished
    assert ad.memory == "limit 300Mi = request"
    assert any("did not attribute" in n for n in ad.notes)


def test_workloads_failing_the_same_way_are_grouped(two_alike):
    """The common cause agents missed: two workloads with one signature."""
    [group] = namespace_health(two_alike, "default").common_failures
    assert group.workloads == ["Deployment/ad", "Deployment/fraud"]
    assert "exit 137" in group.signature and "under 10s" in group.signature
    assert "limit 300Mi = request" in group.signature


def test_healthy_workloads_are_compact(two_alike):
    cart = [w for w in namespace_health(two_alike, "default").workloads
            if w.workload == "Deployment/cart"][0]
    dumped = cart.model_dump(mode="json")
    assert "last_termination" not in dumped and "notes" not in dumped
    assert dumped["healthy"] is True


def test_restart_gap_of_a_running_container_is_the_back_off():
    """Cadence comes from status: finished -> next start, never event counts."""
    state = _state(deployments=[_deployment("ad", 0, 1)], replicasets=[_replicaset("ad-1", "ad")],
                   pods=[_pod("ad-1-a", "ReplicaSet/ad-1",
                              _crashing("ad-1-a", "ad", lifetime=123, gap=300), 67)])
    t = namespace_health(state, "default").workloads[0].last_termination
    assert t.instance_lifetime == datetime.timedelta(seconds=123)
    assert t.restart_gap == datetime.timedelta(seconds=300) and t.waiting is None


def test_namespace_health_is_bounded():
    h = namespace_health(MockState.from_builtin(frozen=True), "default")
    size = len(json.dumps(h.model_dump(mode="json")))
    assert size <= NAMESPACE_BUDGET_PER_WORKLOAD * len(h.workloads)


def test_namespace_health_on_the_mock_fixture():
    h = mock_tools.get_namespace_health("default")
    first = h.workloads[0]
    assert first.workload == "Deployment/ad" and not first.healthy
    assert first.last_termination.reason == "OOMKilled"
    assert first.template_changed == datetime.timedelta(hours=7, minutes=34)
    assert all(w.healthy for w in h.workloads[1:])
    assert h.common_failures == []  # only one workload is failing


# --- get_workload_report ----------------------------------------------------------------

def test_report_on_the_mock_fixture():
    r = mock_tools.get_workload_report("ad")
    assert r.workload == "Deployment/ad" and (r.ready, r.desired) == (0, 1)
    [c] = r.containers
    assert c.limits == {"memory": "300Mi"} and c.probes == ["none configured"]
    [i] = r.instances
    assert i.last_termination.reason == "OOMKilled"
    assert i.last_termination.pod is None  # the instance names it
    assert [(t.previous, bool(t.lines)) for t in r.logs] == [(False, True), (True, True)]
    assert "OutOfMemoryError" in r.logs[1].lines[-1]
    assert r.last_change.revision == 2 and r.last_change.changes[0].startswith("containers[ad].image")
    assert any("not necessarily when the problem did" in n for n in r.notes)
    assert any("not how long the workload has been healthy" in n for n in r.notes)


def test_events_merge_across_pods_and_skip_other_kinds_with_the_same_name():
    """A Service named like the workload has its own events; they don't belong.
    And replacing pod names must not break words ("ad" inside "already")."""
    def event(obj, reason, message, count):
        kind, _, name = obj.partition("/")
        return {"last_seen_seconds": 60.0, "first_seen_seconds": 3600.0, "count": count,
                "type": "Normal", "reason": reason, "namespace": "default",
                "involved_kind": kind, "involved_name": name, "object": obj, "message": message}
    state = _state(
        deployments=[_deployment("ad", 0, 2)], replicasets=[_replicaset("ad-1", "ad")],
        pods=[_pod(p, "ReplicaSet/ad-1", _crashing(p, "ad"), 40) for p in ("ad-1-a", "ad-1-b")],
        events=[event("Pod/ad-1-a", "Pulled", "Image already present for ad-1-a", 30),
                event("Pod/ad-1-b", "Pulled", "Image already present for ad-1-b", 20),
                event("Service/ad", "UpdatedLoadBalancer", "Updated load balancer", 1)])
    r = workload_report(state, "ad", "default")
    assert [(e.reason, e.message, e.count, e.objects) for e in r.events] == [
        ("Pulled", "Image already present for <name>", 50, 2)]


def test_logs_are_bounded_filtered_and_their_notes_separated():
    line = "2026-10-08T00:00:{:02d}Z line {} " + "x" * 400
    logs = "\n".join(line.format(i, i) for i in range(60)) + "\n"
    state = _state(deployments=[_deployment("ad", 0, 1)], replicasets=[_replicaset("ad-1", "ad")],
                   pods=[_pod("ad-1-a", "ReplicaSet/ad-1", _crashing("ad-1-a", "ad"), 40,
                              logs={"ad": logs}, previous={"ad": logs})])
    r = workload_report(state, "ad", "default", log_lines=5, grep="line 5")
    current = r.logs[0]
    # grep ("line 5" matches 5 and 50-59), then the last 5 matches, each capped
    assert [l.split()[2] for l in current.lines] == ["55", "56", "57", "58", "59"]
    assert all(len(l) <= composites._MAX_LINE_CHARS + 4 for l in current.lines)
    # The container is waiting, so the tool's note line is lifted out of the log.
    assert current.notes and not any(l.startswith("[k8stools]") for l in current.lines)
    # log_lines is capped at 100; all 60 lines fit
    assert len(workload_report(state, "ad", "default", log_lines=10_000).logs[0].lines) == 60


def test_kind_is_found_by_name_and_errors_are_clear():
    state = _state(deployments=[_deployment("x", 1, 1)],
                   statefulsets=[{"name": "x", "namespace": "default", "total_replicas": 1,
                                  "ready_replicas": 1, "current_replicas": 1,
                                  "update_strategy": "RollingUpdate", "age_seconds": 60.0}])
    with pytest.raises(k8s_tools.K8sApiError, match="Deployment and a StatefulSet"):
        workload_report(state, "x", "default")
    assert workload_report(state, "x", "default", kind="StatefulSet").workload == "StatefulSet/x"
    with pytest.raises(k8s_tools.K8sApiError, match="No Deployment"):
        workload_report(state, "nope", "default")
    with pytest.raises(k8s_tools.K8sApiError, match="Unsupported kind"):
        workload_report(state, "x", "default", kind="Pod")


def test_a_workload_with_no_pods_says_so():
    state = _state(deployments=[_deployment("idle", 0, 0)])
    r = workload_report(state, "idle", "default")
    assert r.instances == [] and any("No pods" in n for n in r.notes)


def test_reports_are_bounded():
    for name in ("ad", "postgres", "otel-collector-agent", "cleanup-28999999"):
        r = mock_tools.get_workload_report(name)
        assert len(json.dumps(r.model_dump(mode="json"))) <= REPORT_BUDGET, name


def test_config_references_come_through():
    r = mock_tools.get_workload_report("otel-collector-agent")
    assert [(c.kind, c.name) for c in r.config] == [("ConfigMap", "otel-collector-agent")]


# --- serialization and registration -------------------------------------------------

def test_empty_fields_are_left_out():
    dumped = composites.WorkloadHealth(workload="Deployment/x", ready=1, desired=1).model_dump(mode="json")
    assert dumped == {"workload": "Deployment/x", "ready": 1, "desired": 1, "restarts": 0,
                      "healthy": True}


def test_registered_with_short_descriptions():
    for name in ("get_namespace_health", "get_workload_report"):
        assert getattr(k8s_tools, name) in k8s_tools.TOOLS
        assert getattr(mock_tools, name) in mock_tools.TOOLS
        assert getattr(mock_tools, name).__doc__ == getattr(k8s_tools, name).__doc__
        assert name in k8s_tools.TOOLSET_NAMES["triage"]
