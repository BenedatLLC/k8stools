"""Init containers and native sidecars (issue #24).

get_pod_container_statuses keeps init containers apart from app containers, so a
completed init container's exit 0 can't be read as an app container's
termination, and reports [] (none) apart from None (not recorded, an older
capture). Native sidecars (restartPolicy Always) are listed with the app
containers, in every tool. The composites treat an init container that hasn't
completed as what holds the workload back, and ignore one that has.
"""

import datetime
import json

import pytest

from k8stools import capture, k8s_tools
from k8stools.capture import CaptureStats
from k8stools.composites import namespace_health, workload_report
from k8stools.k8s_tools import (ContainerStateRunning, ContainerStateTerminated,
                                ContainerStateWaiting, ContainerStatus,
                                NOTE_INIT_NOT_RECORDED, NOTE_INIT_PENDING, PodContainerStatuses)
from k8stools.mock_state import CAPTURE_VERSION, MockState, encode_model

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC).replace(microsecond=0)


def _ago(**kw):
    return NOW - datetime.timedelta(**kw)


def _status(container, state, last_state=None, restarts=0, ready=False, sidecar=False):
    return ContainerStatus(
        pod_name="web-1-a", namespace="default", container_name=container,
        image=f"{container}:1", ready=ready, restart_count=restarts, started=ready,
        stop_signal=None, state=state, last_state=last_state, volume_mounts=[],
        resource_requests={}, resource_limits={}, allocated_resources={}, sidecar=sidecar)


def _completed(container):
    return _status(container, ContainerStateTerminated(
        exit_code=0, reason="Completed", started_at=_ago(days=1, seconds=5),
        finished_at=_ago(days=1, seconds=3)))


def _crashing_init(container):
    return _status(container, ContainerStateWaiting(reason="CrashLoopBackOff"),
                   ContainerStateTerminated(exit_code=1, reason="Error",
                                            started_at=_ago(minutes=3, seconds=2),
                                            finished_at=_ago(minutes=3)), restarts=12)


def _running_app(ready=True):
    return _status("web", ContainerStateRunning(started_at=_ago(days=1)), ready=ready)


def _held_back_app():
    return _status("web", ContainerStateWaiting(reason="PodInitializing"))


def _state(app, init, spec_init=(), sidecars=(), ready=1):
    """One Deployment, web, with one pod. ``init`` None leaves the statuses out,
    as a capture made before they were recorded does."""
    pod = {"summary": {"name": "web-1-a", "namespace": "default",
                       "total_containers": 1 + len(sidecars), "ready_containers": ready,
                       "restarts": sum(s.restart_count for s in init or []),
                       "last_restart_seconds": None, "age_seconds": 86400.0,
                       "owner": "ReplicaSet/web-1"},
           "labels": {},
           "container_statuses": [encode_model(s, NOW) for s in [app, *sidecars]],
           "spec": {"containers": [{"name": "web", "image": "web:1"}],
                    "init_containers": [{"name": n, "image": f"{n}:1"} for n in spec_init]
                    + [{"name": s.container_name, "image": s.image, "restart_policy": "Always"}
                       for s in sidecars]},
           "logs": {"web": "", **{n: "2026-10-08T00:00:00Z init failed: db unreachable\n"
                                  for n in spec_init}},
           "previous_logs": {n: "2026-10-08T00:00:00Z init failed: db unreachable\n"
                             for n in spec_init}}
    if init is not None:
        pod["init_container_statuses"] = [encode_model(s, NOW) for s in init]
    return MockState(json.loads(json.dumps({
        "version": CAPTURE_VERSION, "captured_at": NOW.isoformat(), "redacted": False,
        "deployments": [{"name": "web", "namespace": "default", "total_replicas": 1,
                         "ready_replicas": ready, "up_to_date_relicas": 1,
                         "available_replicas": ready, "age_seconds": 8 * 86400.0}],
        "replicasets": [{"name": "web-1", "namespace": "default", "owner_deployment": "web",
                         "revision": 1, "desired_replicas": 1, "current_replicas": 1,
                         "ready_replicas": ready, "images": ["web:1"],
                         "age_seconds": 86400.0}],
        "pods": [pod]})), frozen=True)


# --- the model ------------------------------------------------------------------

def test_none_and_not_recorded_look_different():
    none = PodContainerStatuses(pod_name="p", namespace="d", init_containers=[])
    unknown = PodContainerStatuses(pod_name="p", namespace="d")
    assert none.notes == [] and unknown.notes == [NOTE_INIT_NOT_RECORDED]
    assert json.loads(none.model_dump_json())["init_containers"] == []
    assert json.loads(unknown.model_dump_json())["init_containers"] is None


def test_an_init_container_that_hasnt_completed_is_noted():
    pod = PodContainerStatuses(pod_name="p", namespace="d", containers=[_held_back_app()],
                               init_containers=[_crashing_init("migrate")])
    assert pod.notes == [NOTE_INIT_PENDING]
    done = PodContainerStatuses(pod_name="p", namespace="d", containers=[_running_app()],
                                init_containers=[_completed("migrate")])
    assert done.notes == []


# --- namespace health -------------------------------------------------------------

def test_a_completed_init_container_is_not_a_termination():
    health = namespace_health(_state(_running_app(), [_completed("migrate")], ["migrate"]))
    assert health.workloads == [] and health.healthy == ["Deployment/web 1/1"]


def test_a_crash_looping_init_container_makes_the_workload_unhealthy_and_is_named():
    health = namespace_health(_state(_held_back_app(), [_crashing_init("migrate")],
                                     ["migrate"], ready=0))
    [w] = health.workloads
    assert w.last_termination.container == "migrate" and w.last_termination.exit_code == 1
    assert NOTE_INIT_PENDING in w.notes
    assert "Init container 'migrate': Waiting (CrashLoopBackOff)" in w.notes


# --- workload report --------------------------------------------------------------

def test_no_init_containers_is_stated_not_dropped():
    report = workload_report(_state(_running_app(), []), "web", "default")
    assert report.init_containers == []
    assert json.loads(report.model_dump_json())["init_containers"] == []
    assert NOTE_INIT_NOT_RECORDED not in report.notes


def test_a_completed_init_container_is_listed_but_not_as_an_instance():
    report = workload_report(_state(_running_app(), [_completed("migrate")], ["migrate"]),
                             "web", "default")
    assert [c.container for c in report.init_containers] == ["migrate"]
    assert report.init_containers[0].probes == []  # they can't have any
    assert report.init_instances == []
    assert [i.container for i in report.instances] == ["web"]


def test_a_crash_looping_init_container_is_an_instance_with_its_logs():
    report = workload_report(_state(_held_back_app(), [_crashing_init("migrate")],
                                    ["migrate"], ready=0), "web", "default")
    [i] = report.init_instances
    assert (i.container, i.state, i.restarts) == ("migrate", "Waiting (CrashLoopBackOff)", 12)
    assert NOTE_INIT_PENDING in report.notes
    assert {(t.container, t.previous) for t in report.logs} >= {("migrate", False), ("migrate", True)}


def test_a_native_sidecar_is_with_the_app_containers():
    proxy = _status("proxy", ContainerStateRunning(started_at=_ago(days=1)), ready=True,
                    sidecar=True)
    state = _state(_running_app(), [], sidecars=[proxy], ready=1)
    statuses = state.get_pod_container_statuses("web-1-a", "default")
    assert [(c.container_name, c.sidecar) for c in statuses.containers] == \
        [("web", False), ("proxy", True)]
    report = workload_report(state, "web", "default")
    assert [(c.container, c.sidecar) for c in report.containers] == [("web", None), ("proxy", True)]
    assert report.init_containers == []


def test_an_older_capture_says_init_statuses_werent_recorded():
    state = _state(_running_app(), None, ["migrate"])
    statuses = state.get_pod_container_statuses("web-1-a", "default")
    assert statuses.init_containers is None and statuses.notes == [NOTE_INIT_NOT_RECORDED]
    report = workload_report(state, "web", "default")
    assert NOTE_INIT_NOT_RECORDED in report.notes
    # The spec is in every capture, so the definitions are still there.
    assert [c.container for c in report.init_containers] == ["migrate"]


# --- capture ----------------------------------------------------------------------

def _capture_one_pod(monkeypatch, statuses):
    monkeypatch.setattr(k8s_tools, "get_pod_summaries", lambda ns=None: [
        k8s_tools.PodSummary(name="web-1-a", namespace="default", total_containers=1,
                             ready_containers=1, restarts=12, last_restart=None,
                             age=datetime.timedelta(days=1))])
    monkeypatch.setattr(k8s_tools, "get_pod_container_statuses", statuses)
    monkeypatch.setattr(k8s_tools, "get_pod_spec", lambda p, ns: {})
    monkeypatch.setattr(capture, "_pod_labels", lambda ns: {})
    monkeypatch.setattr(k8s_tools, "_read_pod_log",
                        lambda pod, ns, c, tail=None, previous=False: f"{c} previous={previous}\n")
    return capture._capture_pods("default", NOW, include_logs=True, max_log_lines=10,
                                 include_previous_logs=True, stats=CaptureStats(),
                                 redactor=lambda v: v)[0]


def test_capture_records_init_statuses_and_their_logs(monkeypatch):
    record = _capture_one_pod(monkeypatch, lambda p, ns: PodContainerStatuses(
        pod_name=p, namespace=ns, containers=[_running_app()],
        init_containers=[_crashing_init("migrate")]))
    assert [s["container_name"] for s in record["init_container_statuses"]] == ["migrate"]
    assert record["logs"]["migrate"] and record["previous_logs"]["migrate"]


def test_an_unreadable_pod_records_no_init_statuses_rather_than_none(monkeypatch):
    def fail(p, ns):
        raise k8s_tools.K8sApiError("forbidden")
    record = _capture_one_pod(monkeypatch, fail)
    assert "init_container_statuses" not in record and record["container_statuses"] == []
