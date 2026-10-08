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
NAMESPACE_BUDGET_PER_WORKLOAD = 450   # an unhealthy workload, in full
HEALTHY_LINE_BUDGET = 60              # a healthy one, in a line
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

def test_unhealthy_workloads_in_full_and_healthy_ones_in_a_line(two_alike):
    h = namespace_health(two_alike, "default")
    assert [w.workload for w in h.workloads] == ["Deployment/ad", "Deployment/fraud"]
    assert h.healthy == ["Deployment/cart 1/1", "Pod/debug 1/1"]
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
    assert group.signature == ("exit 137 (killed by SIGKILL (128+9)), reason Error, memory "
                               "limit 300Mi = request, lifetimes 2s-7s")


def test_lifetimes_are_shown_not_required_to_match():
    """On a real cluster ad's last instance lived 20s and fraud-detection's 2s,
    on either side of a 10s band, though the failure is the same."""
    state = _state(
        deployments=[_deployment("ad", 0, 1), _deployment("fraud", 0, 1), _deployment("db", 0, 1)],
        replicasets=[_replicaset("ad-1", "ad"), _replicaset("fraud-1", "fraud"),
                     _replicaset("db-1", "db")],
        pods=[_pod("ad-1-a", "ReplicaSet/ad-1", _crashing("ad-1-a", "ad", lifetime=20), 40),
              _pod("fraud-1-a", "ReplicaSet/fraud-1", _crashing("fraud-1-a", "fraud", lifetime=2), 50),
              _pod("db-1-a", "ReplicaSet/db-1",
                   _crashing("db-1-a", "db", reason="OOMKilled", lifetime=5), 9)])
    [group] = namespace_health(state, "default").common_failures
    assert group.workloads == ["Deployment/ad", "Deployment/fraud"]  # not db: OOMKilled
    assert group.signature.endswith("lifetimes 2s-20s")


def test_a_healthy_workload_line_shows_its_restarts():
    """Healthy now, but a restart count is worth seeing (e.g. after a node restart)."""
    state = _state(deployments=[_deployment("cart", 1, 1)], replicasets=[_replicaset("cart-1", "cart")],
                   pods=[_pod("cart-1-a", "ReplicaSet/cart-1",
                              _status("cart-1-a", "cart", ContainerStateRunning(started_at=_ago(days=1)),
                                      restarts=762, ready=True), restarts=762, ready=1)])
    assert namespace_health(state, "default").healthy == ["Deployment/cart 1/1, 762 restarts"]


def _without_owners(records):
    """As a capture taken before 2.3.0 recorded pods: no owner key at all."""
    for pod in records:
        pod["summary"].pop("owner", None)
    return records


def test_pods_without_owners_are_matched_to_workloads_by_name():
    """k8srca's scenario captures predate PodSummary.owner (2.3.0). Unmatched,
    every pod looked ownerless and Deployment/ad read 0 restarts: wrong, not
    unknown (#13)."""
    def pods(): return [
        _pod("ad-5547bd5bd9-v65gj", "ReplicaSet/ad-5547bd5bd9",
             _crashing("ad-5547bd5bd9-v65gj", "ad"), 3200),
        _pod("db-0", "StatefulSet/db",
             _crashing("db-0", "db", reason="OOMKilled", lifetime=600), 3),
        _pod("nightly-28999999-t4xk9", "Job/nightly-28999999",
             _crashing("nightly-28999999-t4xk9", "nightly", code=1, reason="Error"), 0)]
    keys = dict(
        deployments=[_deployment("ad", 0, 1)],
        replicasets=[_replicaset("ad-5547bd5bd9", "ad")],
        statefulsets=[{"name": "db", "namespace": "default", "total_replicas": 1,
                       "ready_replicas": 0, "current_replicas": 1,
                       "update_strategy": "RollingUpdate", "age_seconds": 86400.0}],
        jobs=[{"name": "nightly-28999999", "namespace": "default", "active": 0, "succeeded": 0,
               "failed": 1, "conditions": ["Failed"], "age_seconds": 600.0, "containers": []}])
    with_owners = namespace_health(_state(pods=pods(), **keys), "default")
    old = namespace_health(_state(pods=_without_owners(pods()), **keys), "default")
    assert [w.workload for w in old.workloads] == [w.workload for w in with_owners.workloads] == [
        "Deployment/ad", "Job/nightly-28999999", "StatefulSet/db"]
    ad = old.workloads[0]
    assert ad.restarts == 3200 and ad.last_termination.exit_code == 137
    assert composites.NOTE_MATCHED_BY_NAME in old.notes
    assert composites.NOTE_MATCHED_BY_NAME not in with_owners.notes
    report = workload_report(_state(pods=_without_owners(pods()), **keys), "ad", "default")
    assert report.instances and report.logs is not None
    assert composites.NOTE_MATCHED_BY_NAME in report.notes


def test_a_workload_whose_pods_cant_be_found_reads_unknown_not_zero():
    state = _state(deployments=[_deployment("ad", 0, 1)], replicasets=[_replicaset("ad-1", "ad")],
                   pods=_without_owners([_pod("something-else", None,
                                               _crashing("something-else", "x"), 5)]))
    [ad] = [w for w in namespace_health(state, "default").workloads if w.workload == "Deployment/ad"]
    assert ad.restarts is None and "restarts" not in ad.model_dump(mode="json")
    assert any("could be found" in n for n in ad.notes)


def test_the_mock_fixture_without_owners_gives_the_same_picture():
    """The --mock fixture is internally consistent, so stripping owners (as a
    pre-2.3.0 capture) must change nothing but the note."""
    from k8stools.mock_state import BUILTIN_STATE_FILE
    data = json.loads(BUILTIN_STATE_FILE.read_text())
    with_owners = namespace_health(MockState(json.loads(json.dumps(data)), frozen=True), "default")
    _without_owners(data["pods"])
    old = namespace_health(MockState(data, frozen=True), "default")
    assert old.workloads == with_owners.workloads
    assert old.healthy == with_owners.healthy
    assert set(old.notes) == set(with_owners.notes) | {composites.NOTE_MATCHED_BY_NAME}


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
    assert size <= NAMESPACE_BUDGET_PER_WORKLOAD * len(h.workloads) + \
        HEALTHY_LINE_BUDGET * len(h.healthy) + 400


def test_namespace_health_on_the_mock_fixture():
    h = mock_tools.get_namespace_health("default")
    [ad] = h.workloads
    assert ad.workload == "Deployment/ad" and not ad.healthy
    assert ad.last_termination.reason == "OOMKilled"
    assert ad.template_changed == datetime.timedelta(hours=7, minutes=34)
    assert h.healthy == ["DaemonSet/otel-collector-agent 1/1", "Deployment/test-deployment 3/3, HPA at max 3",
                         "Job/cleanup-28999999 1/1", "Pod/test-pod-123 2/2", "StatefulSet/postgres 1/1"]
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
    dumped = composites.WorkloadHealth(workload="Deployment/x", ready=0, desired=1).model_dump(mode="json")
    assert dumped == {"workload": "Deployment/x", "ready": 0, "desired": 1, "healthy": False}


def test_registered_with_short_descriptions():
    for name in ("get_namespace_health", "get_workload_report"):
        assert getattr(k8s_tools, name) in k8s_tools.TOOLS
        assert getattr(mock_tools, name) in mock_tools.TOOLS
        assert getattr(mock_tools, name).__doc__ == getattr(k8s_tools, name).__doc__
        assert name in k8s_tools.TOOLSET_NAMES["triage"]
