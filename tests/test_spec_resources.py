"""Requests and limits a container status doesn't report come from the pod spec (#32).

A status carries resources only with in-place resize (on by default from
Kubernetes 1.33), so on older clusters, and in every ITBench-Lite import, each
container read as having no limit, beside OOM kills. Without resize the spec's
resources are the applied ones; where the status reports them, they stand.
"""

import datetime
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from kubernetes import client as c

from k8stools import itbench, k8s_tools
from k8stools.composites import namespace_health
from k8stools.mock_state import BUILTIN_STATE_FILE, MockState

EXCERPT = Path(__file__).parent / "fixtures" / "itbench-33-excerpt"
SPEC_30MI = {"limits": {"cpu": "15m", "memory": "30Mi"}, "requests": {"cpu": "7m", "memory": "15Mi"}}


def _ad(status_resources: dict, spec_resources) -> MockState:
    """The built-in fixture, with the ad pod's status and spec resources replaced."""
    data = json.loads(BUILTIN_STATE_FILE.read_text())
    [pod] = [p for p in data["pods"] if p["summary"]["name"].startswith("ad-")]
    pod["container_statuses"][0]["resource_requests"] = status_resources.get("requests", {})
    pod["container_statuses"][0]["resource_limits"] = status_resources.get("limits", {})
    pod["spec"]["containers"][0]["resources"] = spec_resources
    return MockState(data, frozen=True)


def _memory(state: MockState) -> str:
    [ad] = [w for w in namespace_health(state, "default").workloads if w.workload == "Deployment/ad"]
    return ad.memory


def test_a_status_without_resources_reads_the_spec():
    assert _memory(_ad({}, SPEC_30MI)) == "limit 30Mi, request 15Mi"


def test_a_status_with_resources_wins():
    # After an in-place resize the status is what's applied; the spec what was asked for.
    resized = {"limits": {"memory": "64Mi"}, "requests": {"memory": "64Mi"}}
    assert _memory(_ad(resized, SPEC_30MI)) == "limit 64Mi = request"


def test_neither_is_no_limit():
    assert _memory(_ad({}, None)) == "no limit"


def test_replay_fills_the_status_tool_too():
    state = _ad({}, SPEC_30MI)
    [pod] = [p.name for p in state.get_pod_summaries("default") if p.name.startswith("ad-")]
    [status] = state.get_pod_container_statuses(pod, "default").containers
    assert (status.resource_limits, status.resource_requests) == (SPEC_30MI["limits"],
                                                                  SPEC_30MI["requests"])


def _container_status(name, resources=None):
    return c.V1ContainerStatus(name=name, image=f"{name}:1", image_id="", ready=True,
                               restart_count=0, resources=resources,
                               state=c.V1ContainerState(running=c.V1ContainerStateRunning(
                                   started_at=datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc))))


def _container(name, resources=None, restart_policy=None):
    return c.V1Container(name=name, image=f"{name}:1", restart_policy=restart_policy,
                         resources=c.V1ResourceRequirements(**resources) if resources else None)


@pytest.fixture
def pod(monkeypatch):
    def serve(spec, status):
        monkeypatch.setattr(k8s_tools, "K8S", NS(read_namespaced_pod=lambda name, namespace: c.V1Pod(
            metadata=c.V1ObjectMeta(name=name, namespace=namespace), spec=spec, status=status)))
        return k8s_tools.get_pod_container_statuses("p", "default")
    return serve


def test_the_live_tool_reads_the_spec_for_every_kind_of_container(pod):
    statuses = pod(
        c.V1PodSpec(containers=[_container("app", SPEC_30MI)],
                    init_containers=[_container("init", {"limits": {"memory": "8Mi"}}),
                                     _container("proxy", {"limits": {"memory": "16Mi"}},
                                                restart_policy="Always")]),
        c.V1PodStatus(container_statuses=[_container_status("app")],
                      init_container_statuses=[_container_status("init"),
                                               _container_status("proxy")]))
    assert {s.container_name: s.resource_limits.get("memory")
            for s in statuses.containers + statuses.init_containers} == {
        "app": "30Mi", "proxy": "16Mi", "init": "8Mi"}
    assert statuses.containers[0].resource_requests == SPEC_30MI["requests"]


def test_the_live_tool_keeps_the_status_resources(pod):
    statuses = pod(c.V1PodSpec(containers=[_container("app", SPEC_30MI)]),
                   c.V1PodStatus(container_statuses=[_container_status(
                       "app", c.V1ResourceRequirements(limits={"memory": "64Mi"}))]))
    assert statuses.containers[0].resource_limits == {"memory": "64Mi"}
    assert statuses.containers[0].resource_requests == {}


def test_an_itbench_import_from_a_1_32_cluster_has_its_limits():
    # Scenario-33's ad: the status reports no resources, the spec 450Mi.
    state = MockState(json.loads(json.dumps(itbench.import_snapshot(EXCERPT))), frozen=True)
    [ad] = [w for w in namespace_health(state, "otel-demo").workloads
            if w.workload == "Deployment/ad"]
    assert ad.memory == "limit 450Mi, request 225Mi"
