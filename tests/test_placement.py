"""Which nodes an unscheduled pod's nodeSelector and required affinity admit (issue #29).

The scheduler's FailedScheduling message names only the first filter that
rejected each node, so "1 node(s) had untolerated taint" can name a node the
pod's selector doesn't match either. The composites now count, for each pod
with no node, the nodes its nodeSelector and required node affinity admit, and
how many of those repel it with an untolerated NoSchedule/NoExecute taint.
"""

import datetime
import json

import pytest

from k8stools import composites, mock_tools
from k8stools.composites import NOTE_PLACEMENT, namespace_health, workload_report
from k8stools.k8s_tools import K8sApiError, NodeSummary
from k8stools.mock_state import CAPTURE_VERSION, MockState

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC).replace(microsecond=0)

REASON = ("Unschedulable: 0/3 nodes are available: 1 node(s) had untolerated taint "
          "{node-role.kubernetes.io/control-plane: }, 2 node(s) didn't match Pod's node "
          "affinity/selector.")
CONTROL_PLANE_TAINT = "node-role.kubernetes.io/control-plane=:NoSchedule"


def _node(name, labels=None, taints=()):
    return {"name": name, "status": "Ready", "roles": [], "age_seconds": 86400.0,
            "version": "v1.30.0", "labels": {"kubernetes.io/hostname": name, **(labels or {})},
            "taints": list(taints)}


NODES = [_node("cp-1", {"node-role.kubernetes.io/control-plane": ""}, [CONTROL_PLANE_TAINT]),
         _node("worker-1", {"disktype": "hdd", "cores": "8"}),
         _node("worker-2", {"disktype": "ssd", "cores": "16"})]


def _pending(name, **spec):
    return {"summary": {"name": f"{name}-6d8f9-bbbbb", "namespace": "default",
                        "total_containers": 1, "ready_containers": 0, "restarts": 0,
                        "last_restart_seconds": None, "age_seconds": 600.0, "node": None,
                        "owner": f"ReplicaSet/{name}-6d8f9", "unscheduled": REASON},
            "labels": {}, "container_statuses": [], "init_container_statuses": [],
            "spec": {"containers": [{"name": "app", "image": "app:1"}], **spec},
            "logs": {}, "previous_logs": {}}


def _state(*pods, nodes=NODES):
    names = [p["summary"]["owner"].split("/")[1].rsplit("-", 1)[0] for p in pods]
    data = {"version": CAPTURE_VERSION, "captured_at": NOW.isoformat(), "redacted": False,
            "nodes": nodes, "pods": list(pods),
            "deployments": [{"name": n, "namespace": "default", "total_replicas": 1,
                             "ready_replicas": 0, "up_to_date_relicas": 1,
                             "available_replicas": 0, "age_seconds": 600.0} for n in names],
            "replicasets": [{"name": f"{n}-6d8f9", "namespace": "default",
                             "owner_deployment": n, "revision": 1, "desired_replicas": 1,
                             "current_replicas": 1, "ready_replicas": 0, "images": ["app:1"],
                             "age_seconds": 600.0} for n in names]}
    return MockState(json.loads(json.dumps(data)), frozen=True)


def _placement(pod):
    [w] = namespace_health(_state(pod), "default").workloads
    return w.placement


def test_a_selector_that_matches_no_node_says_so_the_tainted_one_included():
    """The itbench-33 case: the selector names a host that doesn't exist, and
    the scheduler's message cites the control-plane taint."""
    assert _placement(_pending("ad", node_selector={"kubernetes.io/hostname": "invalid-node"})) == [
        "ad-6d8f9-bbbbb: 0 of 3 nodes match its nodeSelector and required node affinity"]


def test_admitted_nodes_that_repel_the_pod_with_a_taint_are_counted():
    pod = _pending("ad", affinity={"node_affinity": {
        "required_during_scheduling_ignored_during_execution": {"node_selector_terms": [
            {"match_expressions": [{"key": "node-role.kubernetes.io/control-plane",
                                    "operator": "Exists"}]}]}}})
    assert _placement(pod) == [
        "ad-6d8f9-bbbbb: 1 of 3 nodes match its nodeSelector and required node affinity; "
        "of those, 1 has a NoSchedule or NoExecute taint it doesn't tolerate"]
    pod["spec"]["tolerations"] = [{"key": "node-role.kubernetes.io/control-plane",
                                   "operator": "Exists", "effect": "NoSchedule"}]
    assert _placement(pod)[0].endswith("of those, 0 have a NoSchedule or NoExecute "
                                       "taint it doesn't tolerate")


def test_a_pod_with_no_constraint_says_every_node_matches():
    assert _placement(_pending("ad")) == [
        "ad-6d8f9-bbbbb: no nodeSelector or required node affinity, so all nodes match (3); "
        "of those, 1 has a NoSchedule or NoExecute taint it doesn't tolerate"]


def _admitted(**spec) -> list[str]:
    nodes = [NodeSummary.model_validate({**n, "age": n["age_seconds"]}) for n in NODES]
    return [n.name for n in nodes if composites._admits(spec, n)]


def _terms(*terms):
    return {"affinity": {"node_affinity": {
        "required_during_scheduling_ignored_during_execution": {"node_selector_terms": list(terms)}}}}


def _expr(key, operator, *values):
    return {"key": key, "operator": operator, "values": list(values) or None}


@pytest.mark.parametrize("spec, nodes", [
    ({"node_selector": {"disktype": "ssd"}}, ["worker-2"]),
    ({"node_selector": {"disktype": "ssd", "cores": "8"}}, []),
    (_terms({"match_expressions": [_expr("disktype", "In", "ssd", "hdd")]}), ["worker-1", "worker-2"]),
    (_terms({"match_expressions": [_expr("disktype", "NotIn", "ssd")]}), ["cp-1", "worker-1"]),
    (_terms({"match_expressions": [_expr("disktype", "DoesNotExist")]}), ["cp-1"]),
    (_terms({"match_expressions": [_expr("cores", "Gt", "8")]}), ["worker-2"]),
    (_terms({"match_expressions": [_expr("cores", "Lt", "16")]}), ["worker-1"]),
    # Gt on a label that isn't an integer doesn't match, as in the scheduler.
    (_terms({"match_expressions": [_expr("disktype", "Gt", "1")]}), []),
    # Terms are ORed; expressions and fields within one are ANDed.
    (_terms({"match_expressions": [_expr("disktype", "In", "ssd")]},
            {"match_fields": [_expr("metadata.name", "In", "cp-1")]}), ["cp-1", "worker-2"]),
    (_terms({"match_expressions": [_expr("disktype", "Exists")],
             "match_fields": [_expr("metadata.name", "NotIn", "worker-2")]}), ["worker-1"]),
    # An empty term matches no node.
    (_terms({}), []),
    # nodeSelector and affinity must both admit the node.
    ({"node_selector": {"disktype": "hdd"},
      **_terms({"match_expressions": [_expr("cores", "Gt", "8")]})}, []),
])
def test_selector_and_affinity_follow_the_api(spec, nodes):
    assert _admitted(**spec) == nodes


@pytest.mark.parametrize("toleration, tolerated", [
    ({"key": "node-role.kubernetes.io/control-plane", "operator": "Exists"}, True),
    ({"key": "node-role.kubernetes.io/control-plane", "operator": "Equal", "value": ""}, True),
    ({"key": "node-role.kubernetes.io/control-plane", "value": "x"}, False),
    ({"key": "node-role.kubernetes.io/control-plane", "operator": "Exists",
      "effect": "NoExecute"}, False),
    ({"operator": "Exists"}, True),  # an empty key with Exists tolerates every taint
    ({"key": "other", "operator": "Exists"}, False),
])
def test_tolerations_follow_the_api(toleration, tolerated):
    assert composites._tolerates([toleration], CONTROL_PLANE_TAINT) is tolerated


def test_prefer_no_schedule_doesnt_repel():
    node = NodeSummary(name="n", status="Ready", roles=[], age=0, version="v1",
                       taints=["dedicated=gpu:PreferNoSchedule"])
    assert not composites._repelled({}, node)


def test_the_workload_report_has_the_same_lines_and_the_note():
    state = _state(_pending("ad", node_selector={"disktype": "ssd"}))
    [w] = namespace_health(state, "default").workloads
    r = workload_report(state, "ad", "default")
    assert r.placement == w.placement == [
        "ad-6d8f9-bbbbb: 1 of 3 nodes match its nodeSelector and required node affinity; "
        "of those, 0 have a NoSchedule or NoExecute taint it doesn't tolerate"]
    assert NOTE_PLACEMENT in r.notes
    assert NOTE_PLACEMENT in namespace_health(state, "default").notes


def test_nodes_are_read_once_and_only_when_a_pod_is_unscheduled(monkeypatch):
    state = _state(_pending("ad"), _pending("cart"))
    calls, real = [], state.get_node_summaries
    monkeypatch.setattr(state, "get_node_summaries", lambda: calls.append(1) or real())
    assert len(namespace_health(state, "default").workloads) == 2
    assert calls == [1]


def test_unreadable_nodes_say_so_instead_of_dropping_the_line(monkeypatch):
    state = _state(_pending("ad"))

    def forbidden():
        raise K8sApiError("Error fetching nodes: 403 Forbidden: nodes is forbidden")
    monkeypatch.setattr(state, "get_node_summaries", forbidden)
    assert _placement_of(state) == ["ad-6d8f9-bbbbb: not evaluated, nodes could not be read: "
                                    "Error fetching nodes: 403 Forbidden: nodes is forbidden"]
    assert _placement_of(_state(_pending("ad"), nodes=[])) == [
        "ad-6d8f9-bbbbb: not evaluated, no nodes were read"]


def _placement_of(state):
    [w] = namespace_health(state, "default").workloads
    return w.placement


def test_the_mock_fixture_has_a_pod_whose_selector_matches_no_node():
    h = mock_tools.get_namespace_health("default")
    checkout = next(w for w in h.workloads if w.workload == "Deployment/checkout")
    assert checkout.placement == [
        "checkout-5f6d8c9b7-h4n7t: 0 of 1 nodes match its nodeSelector and required node affinity"]
    assert NOTE_PLACEMENT in h.notes
