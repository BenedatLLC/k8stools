"""The built-in fixture (what `--mock` serves) must describe a cluster that could exist.

It is hand-maintained, not a raw capture, so nothing but these tests keeps its
parts agreeing with each other. An agent tested against contradictory mock data
learns to reason about a cluster that cannot exist - the ad pod used to run its
previous revision's image, log a 2-minute run for an instance whose status said
2 seconds, claim more restarts than its age allowed at the backoff cap, and
print log timestamps 14 months older than the capture.
(`test_replicaset_tool.TestMockConsistency` covers the replica sets.)
"""

import datetime
import json
import re

import pytest

from k8stools.mock_state import BUILTIN_STATE_FILE, MockState

_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z", re.M)


@pytest.fixture(scope="module")
def state():
    return MockState.from_builtin(frozen=True)


def _log_times(text):
    return [datetime.datetime.strptime(whole, "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=datetime.timezone.utc)
            + datetime.timedelta(microseconds=int((frac or "0")[:6].ljust(6, "0")))
            for whole, frac in _TS.findall(text)]


def _owned_images(state, pod):
    kind, _, name = pod.owner.partition("/")
    if kind == "ReplicaSet":
        [rs] = [r for r in state.get_replicaset_summaries(pod.namespace) if r.name == name]
        return rs.images
    if kind == "DaemonSet":
        [ds] = [d for d in state.get_daemonset_summaries(pod.namespace) if d.name == name]
        return ds.images
    if kind == "Job":
        [job] = [j for j in state.get_job_summaries(pod.namespace) if j.name == name]
        return [c.image for c in job.containers]
    if kind == "StatefulSet":
        # StatefulSetSummary carries no images; it has to exist, though.
        assert name in {s.name for s in state.get_statefulset_summaries(pod.namespace)}
        return None
    pytest.fail(f"{pod.name}: no check for owner kind {kind}")


def test_every_owner_is_in_the_fixture_and_runs_the_pods_images(state):
    owned = [p for p in state.get_pod_summaries() if p.owner]
    assert {p.owner.partition("/")[0] for p in owned} >= {"ReplicaSet", "DaemonSet", "Job"}
    for pod in owned:
        images = [s.image for s in state.get_pod_container_statuses(pod.name, pod.namespace)]
        expected = _owned_images(state, pod)
        assert expected is None or images == expected, pod.name


def _ready_pods(state, namespace, owner):
    return sum(p.ready_containers == p.total_containers
               for p in state.get_pod_summaries(namespace) if p.owner == owner)


def test_every_workload_has_its_pods(state):
    for deployment in state.get_deployment_summaries():
        current = state.get_replicaset_summaries(
            deployment.namespace, deployment=deployment.name)[-1]
        owner = f"ReplicaSet/{current.name}"
        assert len([p for p in state.get_pod_summaries(deployment.namespace)
                    if p.owner == owner]) == current.current_replicas, deployment.name
        assert _ready_pods(state, deployment.namespace, owner) == \
            deployment.ready_replicas == current.ready_replicas, deployment.name
    for sts in state.get_statefulset_summaries():
        assert _ready_pods(state, sts.namespace, f"StatefulSet/{sts.name}") == \
            sts.ready_replicas, sts.name
    pods = {(p.namespace, p.name) for p in state.get_pod_summaries()}
    for pvc in state.get_pvc_summaries():
        assert all((pvc.namespace, name) in pods for name in pvc.mounted_by), pvc.name


def test_spec_and_status_name_the_same_containers(state):
    for pod in state.get_pod_summaries():
        spec = [c["name"] for c in state.get_pod_spec(pod.name, pod.namespace)["containers"]]
        statuses = [s.container_name
                    for s in state.get_pod_container_statuses(pod.name, pod.namespace)]
        assert spec == statuses and len(spec) == pod.total_containers, pod.name


def test_services_select_pods_and_statefulsets_have_theirs(state):
    labels = {(p["summary"]["namespace"], p["summary"]["name"]): p["labels"]
              for p in json.loads(BUILTIN_STATE_FILE.read_text())["pods"]}
    for svc in state.get_service_summaries():
        assert svc.selector, svc.name
        assert any(ns == svc.namespace and svc.selector.items() <= pod_labels.items()
                   for (ns, _), pod_labels in labels.items()), svc.name
    services = {(s.namespace, s.name) for s in state.get_service_summaries()}
    for sts in state.get_statefulset_summaries():
        assert (sts.namespace, sts.service_name) in services, sts.name


def test_daemonset_counts_match_its_pods(state):
    for ds in state.get_daemonset_summaries():
        pods = [p for p in state.get_pod_summaries(ds.namespace)
                if p.owner == f"DaemonSet/{ds.name}"]
        assert len(pods) == ds.current_number_scheduled
        assert sum(p.ready_containers == p.total_containers for p in pods) == ds.number_ready


def test_nothing_is_older_than_its_namespace(state):
    namespaces = {n.name: n.age for n in state.get_namespaces()}
    resources = [*state.get_pod_summaries(), *state.get_deployment_summaries(),
                 *state.get_replicaset_summaries(), *state.get_service_summaries(),
                 *state.get_configmap_summaries(), *state.get_statefulset_summaries(),
                 *state.get_daemonset_summaries(), *state.get_cronjob_summaries(),
                 *state.get_job_summaries(), *state.get_pvc_summaries()]
    for r in resources:
        assert r.age <= namespaces[r.namespace], f"{type(r).__name__} {r.name}"


def test_node_conditions_have_times_and_none_predates_the_node(state):
    [node] = state.get_node_summaries()
    assert set(node.conditions_since) == set(node.conditions) and node.conditions
    assert all(since <= node.age for since in node.conditions_since.values())


def test_last_restart_is_the_latest_termination(state):
    for pod in state.get_pod_summaries():
        finished = [s.last_state.finished_at
                    for s in state.get_pod_container_statuses(pod.name, pod.namespace)
                    if s.last_state is not None and s.last_state.state_name == "Terminated"]
        if not finished:
            assert pod.last_restart is None, pod.name
        else:
            assert pod.last_restart == state.get_cluster_info().captured_at - max(finished)


def test_logs_fall_inside_the_instance_that_wrote_them(state):
    """Including the CrashLoopBackOff case: with no running instance, the
    kubelet serves the last terminated one for previous=False too."""
    now = state.get_cluster_info().captured_at
    checked = 0
    for pod in state.get_pod_summaries():
        for s in state.get_pod_container_statuses(pod.name, pod.namespace):
            for previous in (False, True):
                instance = s.last_state if previous or s.state.state_name == "Waiting" \
                    else s.state
                if instance is None:
                    continue  # no previous instance: the call raises, as live
                text = state.get_logs_for_pod_and_container(
                    pod.name, pod.namespace, s.container_name, previous=previous)
                if not text:
                    continue
                times = _log_times(text)
                end = getattr(instance, "finished_at", None) or now
                assert times == sorted(times), (pod.name, previous)
                assert instance.started_at <= times[0] and times[-1] <= end, \
                    (pod.name, s.container_name, previous)
                checked += 1
    assert checked >= 6


def test_restart_count_fits_the_pods_age(state):
    """Kubernetes caps the crash-loop backoff at 5 minutes, so each restart
    costs at least the instance's run time plus (after the first few) 300s.
    Agents divide restarts by age to get a rate; it has to be possible."""
    for pod in state.get_pod_summaries():
        for s in state.get_pod_container_statuses(pod.name, pod.namespace):
            if s.last_state is None or s.last_state.ran_for is None:
                continue
            cycle = s.last_state.ran_for + datetime.timedelta(minutes=5)
            assert s.restart_count <= pod.age / cycle + 6, pod.name


def test_event_windows_are_ordered(state):
    for e in state.get_events():
        assert e.count is not None and e.count >= 1
        assert e.first_seen >= e.last_seen
