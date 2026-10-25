"""Integration tests that create the failure they check, on a real cluster.

The other live tests (test_k8s_tools_realk8s.py) only read what the cluster
happens to have, so a failure case the cluster doesn't show - an unschedulable
pod, an unbound claim - was only ever tested against fakes and the mock
fixture, whose messages were written from documentation. These create one in a
namespace of their own and delete it afterwards.

They write to the cluster, so they run only when asked:

    K8STOOLS_LIVE_WRITE=1 uv run pytest tests/test_live_writes.py -v
"""

import os
import re
import time
import uuid

import pytest
from kubernetes import client

from k8stools import k8s_tools

pytestmark = pytest.mark.skipif(
    os.environ.get("K8STOOLS_LIVE_WRITE") != "1",
    reason="writes to the cluster; set K8STOOLS_LIVE_WRITE=1 to run")

PAUSE_IMAGE = "registry.k8s.io/pause:3.9"


@pytest.fixture(scope="module")
def apis():
    k8s_tools.K8S = k8s_tools.APPS_V1_API = None
    try:
        api_client = k8s_tools._binding().api_client
        core = client.CoreV1Api(api_client)
        core.list_namespace(limit=1)
    except Exception as e:
        pytest.skip(f"Could not reach a Kubernetes cluster: {e}")
    yield core, client.AppsV1Api(api_client)
    k8s_tools.K8S = k8s_tools.APPS_V1_API = None


@pytest.fixture
def namespace(apis):
    core, _ = apis
    name = f"k8stools-test-{uuid.uuid4().hex[:6]}"
    core.create_namespace(client.V1Namespace(metadata=client.V1ObjectMeta(name=name)))
    yield name
    core.delete_namespace(name)


def _until(check, timeout, what):
    """check()'s first truthy result, polling every 2s."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout}s waiting for {what}")
        time.sleep(2)


def test_an_unbound_claim_says_why_it_waits(apis, namespace):
    """#28, against the PV controller's own event."""
    core, _ = apis
    core.create_namespaced_persistent_volume_claim(namespace, client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name="cache"),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"], storage_class_name="k8stools-no-such-class",
            resources=client.V1VolumeResourceRequirements(requests={"storage": "1Mi"}))))

    def claim_with_events():
        [claim] = k8s_tools.get_pvc_summaries(namespace)
        return claim if claim.events else None

    claim = _until(claim_with_events, 60, "the claim's events")
    assert claim.status == "Pending"
    event = claim.events[0]
    assert (event.type, event.reason) == ("Warning", "ProvisioningFailed")
    assert 'storageclass.storage.k8s.io "k8stools-no-such-class" not found' in event.message
    assert event.object == "PersistentVolumeClaim/cache"


def test_a_pod_waiting_on_an_unbound_claim_names_it(apis, namespace):
    """The #28 follow-up: the scheduler says only that the pod has unbound claims;
    get_namespace_health adds the claim and its event, which say why."""
    core, apps = apis
    core.create_namespaced_persistent_volume_claim(namespace, client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name="cache"),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"], storage_class_name="k8stools-no-such-class",
            resources=client.V1VolumeResourceRequirements(requests={"storage": "1Mi"}))))
    labels = {"app": "db"}
    apps.create_namespaced_deployment(namespace, client.V1Deployment(
        metadata=client.V1ObjectMeta(name="db"),
        spec=client.V1DeploymentSpec(
            replicas=1, selector=client.V1LabelSelector(match_labels=labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=client.V1PodSpec(
                    termination_grace_period_seconds=0,
                    containers=[client.V1Container(
                        name="pause", image=PAUSE_IMAGE,
                        volume_mounts=[client.V1VolumeMount(name="cache", mount_path="/cache")])],
                    volumes=[client.V1Volume(
                        name="cache", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                            claim_name="cache"))])))))

    def waiting():
        h = k8s_tools.get_namespace_health(namespace)
        return next((w for w in h.workloads if w.workload == "Deployment/db"
                     and w.unscheduled and w.claims
                     and "storage class" in w.claims[0] and "ProvisioningFailed" in w.claims[0]),
                    None)

    db = _until(waiting, 90, "the pod to be reported waiting on its claim")
    [line] = db.unscheduled
    [claim] = db.claims
    print(f"\nunscheduled: {line}\nclaims: {claim}")
    assert "unbound immediate PersistentVolumeClaims" in line
    assert claim.startswith("PersistentVolumeClaim/cache: Pending, storage class "
                            "k8stools-no-such-class; ProvisioningFailed: ")
    assert 'storageclass.storage.k8s.io "k8stools-no-such-class" not found' in claim
    assert k8s_tools.get_workload_report("db", namespace).claims == db.claims


def test_a_rollout_whose_new_pod_cant_be_scheduled(apis, namespace):
    """#27, against the scheduler's and the Deployment controller's own words."""
    _, apps = apis
    labels = {"app": "web"}
    apps.create_namespaced_deployment(namespace, client.V1Deployment(
        metadata=client.V1ObjectMeta(name="web"),
        spec=client.V1DeploymentSpec(
            replicas=1, progress_deadline_seconds=15,
            selector=client.V1LabelSelector(match_labels=labels),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(labels=labels),
                spec=client.V1PodSpec(termination_grace_period_seconds=0, containers=[
                    client.V1Container(name="pause", image=PAUSE_IMAGE, resources=client.V1ResourceRequirements(
                        requests={"cpu": "1m", "memory": "8Mi"}))])))))
    _until(lambda: any(d.name == "web" and d.ready_replicas == 1
                       for d in k8s_tools.get_deployment_summaries(namespace)),
           120, "the first revision to be ready")

    apps.patch_namespaced_deployment("web", namespace, {"spec": {"template": {"spec": {
        "nodeSelector": {"k8stools-test/no-such-node": "true"}}}}})

    def stuck():
        h = k8s_tools.get_namespace_health(namespace)
        return next((w for w in h.workloads if w.workload == "Deployment/web"
                     and w.unscheduled and w.conditions), None)

    web = _until(stuck, 90, "the rollout to pass its progress deadline")
    assert (web.ready, web.desired) == (1, 1)  # the first revision still serves
    [line] = web.unscheduled
    assert ": not scheduled for " in line and "Unschedulable: " in line
    assert "didn't match Pod's node affinity/selector" in line
    assert web.conditions[0].startswith("Progressing=False (ProgressDeadlineExceeded): ")

    [pending] = [p for p in k8s_tools.get_pod_summaries(namespace) if p.node is None]
    assert pending.unscheduled.startswith("Unschedulable: ")
    report = k8s_tools.get_workload_report("web", namespace)
    print(f"\nunscheduled: {line}\ncondition: {web.conditions[0]}")

    def without_age(lines):  # "for 12s" can tick between the two calls
        return [re.sub(r" not scheduled for \w+: ", " not scheduled: ", l) for l in lines]

    assert without_age(report.unscheduled) == without_age(web.unscheduled)
    assert report.conditions == web.conditions

    # #29: the selector names a label no node has, so it admits none of them.
    nodes = len(k8s_tools.get_node_summaries())
    assert web.placement == [f"{pending.name}: 0 of {nodes} nodes match its nodeSelector "
                             "and required node affinity"]
    assert report.placement == web.placement
