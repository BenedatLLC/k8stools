"""Rollouts that can't make progress (issue #27).

A Deployment whose new pod can't be scheduled kept `ready >= desired` on its old
pods, and the unscheduled pod has no container statuses, so nothing waited:
get_namespace_health listed it healthy. A pod with no node now makes its
workload unhealthy, with the scheduler's reason, and so do a Deployment's
failing conditions (Progressing=False, ReplicaFailure=True), which also cover a
rollout whose pods were never created. The ready count stays that of the pods
serving.
"""

import datetime
import json
from types import SimpleNamespace

from k8stools import k8s_tools
from k8stools.composites import namespace_health, workload_report
from k8stools.k8s_tools import ContainerStateRunning, ContainerStatus
from k8stools.mock_state import CAPTURE_VERSION, MockState, encode_model

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC).replace(microsecond=0)

NODE_SELECTOR = ("Unschedulable: 0/3 nodes are available: 3 node(s) didn't match Pod's "
                 "node affinity/selector.")
DEADLINE = {"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded",
            "message": 'ReplicaSet "ad-2" has timed out progressing.', "since_seconds": 600.0}


def _deployment(name, ready=1, desired=1, conditions=None):
    d = {"name": name, "namespace": "default", "total_replicas": desired,
         "ready_replicas": ready, "up_to_date_relicas": desired, "available_replicas": ready,
         "age_seconds": 8 * 86400.0}
    if conditions is not None:
        d["conditions"] = conditions
    return d


def _replicaset(name, deployment, revision):
    return {"name": name, "namespace": "default", "owner_deployment": deployment,
            "revision": revision, "desired_replicas": 1, "current_replicas": 1,
            "ready_replicas": 1 if revision == 1 else 0, "images": [f"{deployment}:1"],
            "age_seconds": 3600.0 * (100 if revision == 1 else 2)}


def _serving(pod, rs):
    status = encode_model(ContainerStatus(
        pod_name=pod, namespace="default", container_name="app", image="app:1", ready=True,
        restart_count=0, started=True, stop_signal=None,
        state=ContainerStateRunning(started_at=NOW - datetime.timedelta(days=4)),
        last_state=None, volume_mounts=[], resource_requests={}, resource_limits={},
        allocated_resources={}), NOW)
    return {"summary": {"name": pod, "namespace": "default", "total_containers": 1,
                        "ready_containers": 1, "restarts": 0, "last_restart_seconds": None,
                        "age_seconds": 4 * 86400.0, "node": "node-1",
                        "owner": f"ReplicaSet/{rs}"},
            "labels": {}, "container_statuses": [status], "init_container_statuses": [],
            "spec": {"containers": [{"name": "app", "image": "app:1"}]},
            "logs": {"app": ""}, "previous_logs": {}}


def _unscheduled(pod, rs, reason=NODE_SELECTOR):
    summary = {"name": pod, "namespace": "default", "total_containers": 1,
               "ready_containers": 0, "restarts": 0, "last_restart_seconds": None,
               "age_seconds": 7200.0, "node": None, "owner": f"ReplicaSet/{rs}"}
    if reason is not None:
        summary["unscheduled"] = reason
    return {"summary": summary, "labels": {}, "container_statuses": [],
            "init_container_statuses": [],
            "spec": {"containers": [{"name": "app", "image": "app:1"}],
                     "node_selector": {"disktype": "ssd"}},
            "logs": {}, "previous_logs": {}}


def _stuck(name, reason=NODE_SELECTOR, conditions=None):
    """A Deployment mid-rollout: revision 1's pod serving, revision 2's unscheduled."""
    return dict(deployments=[_deployment(name, conditions=conditions)],
                replicasets=[_replicaset(f"{name}-1", name, 1), _replicaset(f"{name}-2", name, 2)],
                pods=[_serving(f"{name}-1-aaaaa", f"{name}-1"),
                      _unscheduled(f"{name}-2-bbbbb", f"{name}-2", reason)])


def _state(*parts):
    keys = {"deployments": [], "replicasets": [], "pods": []}
    for part in parts:
        for k, v in part.items():
            keys[k] = keys[k] + v
    return MockState(json.loads(json.dumps({
        "version": CAPTURE_VERSION, "captured_at": NOW.isoformat(), "redacted": False,
        **keys})), frozen=True)


def test_a_rollout_whose_new_pod_cant_be_scheduled_is_not_healthy():
    h = namespace_health(_state(_stuck("ad")), "default")
    assert h.healthy == []
    [ad] = h.workloads
    assert (ad.workload, ad.ready, ad.desired) == ("Deployment/ad", 1, 1)  # still served
    assert ad.unscheduled == [f"ad-2-bbbbb: not scheduled for 2h0m: {NODE_SELECTOR}"]


def test_workloads_unscheduled_for_the_same_reason_are_one_failure():
    """The ITBench case: one invalid nodeSelector on ad and cart."""
    h = namespace_health(_state(_stuck("ad"), _stuck("cart")), "default")
    [group] = h.common_failures
    assert group.signature == f"not scheduled: {NODE_SELECTOR}"
    assert group.workloads == ["Deployment/ad", "Deployment/cart"]


def test_a_capture_without_the_reason_still_finds_the_pod():
    """Before 4.1.0 no reason was recorded; no node and no containers is enough."""
    [ad] = namespace_health(_state(_stuck("ad", reason=None)), "default").workloads
    assert ad.unscheduled == ["ad-2-bbbbb: not scheduled for 2h0m: reason not recorded "
                              "(the capture predates k8stools 4.1.0)"]


def test_a_pod_with_containers_reported_is_scheduled_even_without_a_node():
    """Old test captures and fakes record no node for pods that are running."""
    part = _stuck("ad")
    part["pods"] = part["pods"][:1]
    part["pods"][0]["summary"]["node"] = None
    part["replicasets"] = part["replicasets"][:1]
    h = namespace_health(_state(part), "default")
    assert h.workloads == [] and h.healthy == ["Deployment/ad 1/1"]


def test_failing_deployment_conditions_make_it_unhealthy_in_the_controllers_words():
    """ReplicaFailure covers a rollout whose pods were never created (quota,
    admission), so there is no pod to find."""
    quota = {"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate",
             "message": 'pods "web-2-x" is forbidden: exceeded quota: compute', "since_seconds": 60.0}
    ok = [{"type": "Available", "status": "True", "reason": "MinimumReplicasAvailable",
           "message": "Deployment has minimum availability."},
          {"type": "Progressing", "status": "True", "reason": "ReplicaSetUpdated",
           "message": 'ReplicaSet "web-2" is progressing.'}]
    part = {"deployments": [_deployment("web", conditions=ok + [quota]),
                            _deployment("api", conditions=ok)],
            "replicasets": [_replicaset("web-1", "web", 1), _replicaset("api-1", "api", 1)],
            "pods": [_serving("web-1-aaaaa", "web-1"), _serving("api-1-aaaaa", "api-1")]}
    h = namespace_health(_state(part), "default")
    [web] = h.workloads
    assert web.conditions == ['ReplicaFailure=True (FailedCreate): pods "web-2-x" is '
                              'forbidden: exceeded quota: compute']
    assert h.healthy == ["Deployment/api 1/1"]  # Progressing=True mid-rollout is fine


def test_a_capture_without_deployment_conditions_reads_as_before():
    state = _state(_stuck("ad"))
    assert all(d.conditions is None for d in state.get_deployment_summaries("default"))
    [ad] = namespace_health(state, "default").workloads
    assert ad.conditions == []


def test_the_workload_report_names_the_unscheduled_pod_and_the_condition():
    r = workload_report(_state(_stuck("ad", conditions=[DEADLINE])), "ad", "default")
    assert (r.ready, r.desired) == (1, 1)
    assert r.unscheduled == [f"ad-2-bbbbb: not scheduled for 2h0m: {NODE_SELECTOR}"]
    assert r.conditions == ['Progressing=False (ProgressDeadlineExceeded): ReplicaSet "ad-2" '
                            'has timed out progressing.']
    assert [i.pod for i in r.instances] == ["ad-1-aaaaa"]  # the unscheduled pod has none


def test_the_mock_fixture_has_a_stuck_rollout():
    from k8stools import mock_tools
    mock_tools.load_mock_state()
    h = mock_tools.get_namespace_health("default")
    [checkout] = [w for w in h.workloads if w.workload == "Deployment/checkout"]
    assert (checkout.ready, checkout.desired) == (1, 1)
    [line] = checkout.unscheduled
    assert line.startswith("checkout-5f6d8c9b7-h4n7t: not scheduled for ")
    assert "didn't match Pod's node affinity/selector" in line
    assert checkout.conditions[0].startswith("Progressing=False (ProgressDeadlineExceeded)")


# --- the live tools -------------------------------------------------------------------

def _condition(type_, status, reason=None, message=None, ago=None):
    return SimpleNamespace(type=type_, status=status, reason=reason, message=message,
                           last_transition_time=NOW - ago if ago else None)


def _live_pod(name, node, conditions):
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace="default", owner_references=None,
                                 creation_timestamp=NOW - datetime.timedelta(hours=2)),
        spec=SimpleNamespace(containers=[SimpleNamespace(name="app")], init_containers=None,
                             node_name=node),
        status=SimpleNamespace(container_statuses=None, init_container_statuses=None,
                               pod_ip=None, conditions=conditions))


def test_pod_summaries_say_why_a_pod_has_no_node(monkeypatch):
    pods = [_live_pod("a", None, [_condition("PodScheduled", "False", "Unschedulable",
                                             "0/3 nodes are available: ...")]),
            _live_pod("b", None, None),
            _live_pod("c", "node-1", [_condition("PodScheduled", "True")])]
    api = SimpleNamespace(list_namespaced_pod=lambda namespace: SimpleNamespace(items=pods))
    monkeypatch.setattr(k8s_tools, "K8S", api)
    a, b, c = k8s_tools.get_pod_summaries("default")
    assert a.unscheduled == "Unschedulable: 0/3 nodes are available: ..."
    assert b.unscheduled == k8s_tools.NOT_YET_SCHEDULED
    assert c.unscheduled is None


def test_deployment_summaries_carry_their_conditions(monkeypatch):
    d = SimpleNamespace(
        metadata=SimpleNamespace(name="ad", namespace="default",
                                 creation_timestamp=NOW - datetime.timedelta(days=8)),
        spec=SimpleNamespace(replicas=1),
        status=SimpleNamespace(ready_replicas=1, updated_replicas=1, available_replicas=1,
                               conditions=[_condition("Progressing", "False",
                                                      "ProgressDeadlineExceeded", "timed out",
                                                      datetime.timedelta(minutes=10))]))
    api = SimpleNamespace(list_namespaced_deployment=lambda namespace: SimpleNamespace(items=[d]))
    monkeypatch.setattr(k8s_tools, "APPS_V1_API", api)
    [summary] = k8s_tools.get_deployment_summaries("default")
    [c] = summary.conditions
    assert (c.type, c.status, c.reason, c.message) == ("Progressing", "False",
                                                       "ProgressDeadlineExceeded", "timed out")
    assert datetime.timedelta(minutes=10) <= c.since < datetime.timedelta(minutes=11)
