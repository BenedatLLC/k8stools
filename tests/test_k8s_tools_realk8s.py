"""These tests call the tool apis against a real k8s cluster. They just do very basic santity
testing of the results. The tests are skipped if we can't reach the cluster.
"""
import datetime
import pytest
from k8stools import k8s_tools

@pytest.fixture(scope="module", autouse=True)
def skip_if_no_k8s():
    try:
        # Try to initialize the client and list namespaces as a basic connectivity check
        client = k8s_tools._get_api_client()
        client.list_namespace()
    except Exception:
        pytest.skip("Could not establish connection to Kubernetes cluster.")

@pytest.fixture(scope="module", autouse=True)
def reset_global_state():
    """Ensure clean global state before and after real K8s tests."""
    # Reset state before tests
    k8s_tools.K8S = None
    k8s_tools.APPS_V1_API = None
    
    yield
    
    # Reset state after tests to avoid contaminating other test files
    k8s_tools.K8S = None
    k8s_tools.APPS_V1_API = None

def test_get_namespaces():
    namespaces = k8s_tools.get_namespaces()
    assert isinstance(namespaces, list)

def test_get_node_summaries():
    nodes = k8s_tools.get_node_summaries()
    assert isinstance(nodes, list)
    # If we have nodes, verify the structure of all fields
    if nodes:
        node = nodes[0]
        assert hasattr(node, 'name')
        assert hasattr(node, 'status')
        assert hasattr(node, 'roles')
        assert hasattr(node, 'age')
        assert hasattr(node, 'version')
        assert hasattr(node, 'internal_ip')
        assert hasattr(node, 'external_ip')
        assert hasattr(node, 'os_image')
        assert hasattr(node, 'kernel_version')
        assert hasattr(node, 'container_runtime')
        
        # Verify field types
        assert isinstance(node.name, str)
        assert isinstance(node.status, str)
        assert isinstance(node.roles, list)
        assert isinstance(node.age, datetime.timedelta)
        assert isinstance(node.version, str)
        assert node.internal_ip is None or isinstance(node.internal_ip, str)
        assert node.external_ip is None or isinstance(node.external_ip, str)
        assert node.os_image is None or isinstance(node.os_image, str)
        assert node.kernel_version is None or isinstance(node.kernel_version, str)
        assert node.container_runtime is None or isinstance(node.container_runtime, str)
        
        # Verify logical constraints
        assert node.status in ["Ready", "NotReady", "Unknown"]
        assert len(node.roles) > 0  # Should have at least one role or ["<none>"]
        if node.roles != ["<none>"]:
            for role in node.roles:
                assert isinstance(role, str)
                assert len(role) > 0

def test_get_pod_summaries():
    pods = k8s_tools.get_pod_summaries()
    assert isinstance(pods, list)
    # If we have pods, verify the structure of all fields
    if pods:
        pod = pods[0]
        assert hasattr(pod, 'name')
        assert hasattr(pod, 'namespace')
        assert hasattr(pod, 'total_containers')
        assert hasattr(pod, 'ready_containers')
        assert hasattr(pod, 'restarts')
        assert hasattr(pod, 'last_restart')
        assert hasattr(pod, 'age')
        assert hasattr(pod, 'ip')
        assert hasattr(pod, 'node')
        
        # Verify field types
        assert isinstance(pod.name, str)
        assert isinstance(pod.namespace, str)
        assert isinstance(pod.total_containers, int)
        assert isinstance(pod.ready_containers, int)
        assert isinstance(pod.restarts, int)
        assert pod.last_restart is None or isinstance(pod.last_restart, datetime.timedelta)
        assert isinstance(pod.age, datetime.timedelta)
        assert pod.ip is None or isinstance(pod.ip, str)
        assert pod.node is None or isinstance(pod.node, str)
        
        # Verify logical constraints
        assert pod.ready_containers <= pod.total_containers
        assert pod.restarts >= 0
        assert pod.total_containers > 0
    
    # Test namespace-specific pods
    default_pods = k8s_tools.get_pod_summaries("default")
    assert isinstance(default_pods, list)

def test_get_pod_container_statuses():
    if k8s_tools.K8S is None:
        k8s_tools.K8S = k8s_tools._get_api_client()
    pod_list = k8s_tools.K8S.list_namespaced_pod(namespace="default").items
    if not pod_list:
        pytest.skip("No pods found in namespace 'default'.")
    pod_name = pod_list[0].metadata.name
    statuses = k8s_tools.get_pod_container_statuses(pod_name, "default")
    assert isinstance(statuses, list)

def test_get_pod_events():
    if k8s_tools.K8S is None:
        k8s_tools.K8S = k8s_tools._get_api_client()
    pod_list = k8s_tools.K8S.list_namespaced_pod(namespace="default").items
    if not pod_list:
        pytest.skip("No pods found in namespace 'default'.")
    pod_name = pod_list[0].metadata.name
    events = k8s_tools.get_pod_events(pod_name, "default")
    assert isinstance(events, list)

def test_get_pod_spec():
    if k8s_tools.K8S is None:
        k8s_tools.K8S = k8s_tools._get_api_client()
    pod_list = k8s_tools.K8S.list_namespaced_pod(namespace="default").items
    if not pod_list:
        pytest.skip("No pods found in namespace 'default'.")
    pod_name = pod_list[0].metadata.name
    spec = k8s_tools.get_pod_spec(pod_name, "default")
    assert isinstance(spec, dict)

def test_retrieve_logs_for_pod_and_container():
    if k8s_tools.K8S is None:
        k8s_tools.K8S = k8s_tools._get_api_client()
    pod_list = k8s_tools.K8S.list_namespaced_pod(namespace="default").items
    if not pod_list:
        pytest.skip("No pods found in namespace 'default'.")
    
    # Find a running pod (not in PodInitializing state)
    running_pod = None
    for pod in pod_list:
        if (pod.status.phase == "Running" and 
            pod.status.container_statuses and 
            any(cs.ready for cs in pod.status.container_statuses)):
            running_pod = pod
            break
    
    if running_pod is None:
        pytest.skip("No running pods found in namespace 'default'.")
    
    pod_name = running_pod.metadata.name
    try:
        logs = k8s_tools.get_logs_for_pod_and_container(pod_name, "default")
        assert isinstance(logs, str)
    except k8s_tools.K8sApiError as e:
        # If we still get an error (e.g., no logs available yet), that's acceptable for this test
        # We just want to make sure the function doesn't crash unexpectedly
        assert "Error fetching logs" in str(e)

def test_deployment_summaries():
    deployments = k8s_tools.get_deployment_summaries()
    assert isinstance(deployments, list)
    # If we have deployments, verify the structure
    if deployments:
        deployment = deployments[0]
        assert hasattr(deployment, 'name')
        assert hasattr(deployment, 'namespace')
        assert hasattr(deployment, 'total_replicas')
        assert hasattr(deployment, 'ready_replicas')
        assert hasattr(deployment, 'up_to_date_relicas')
        assert hasattr(deployment, 'available_replicas')
        assert hasattr(deployment, 'age')
        assert isinstance(deployment.total_replicas, int)
        assert isinstance(deployment.ready_replicas, int)
        assert isinstance(deployment.up_to_date_relicas, int)
        assert isinstance(deployment.available_replicas, int)
    
    # Test namespace-specific deployments
    default_deployments = k8s_tools.get_deployment_summaries("default")
    assert isinstance(default_deployments, list)

def test_service_summaries():
    services = k8s_tools.get_service_summaries()
    assert isinstance(services, list)
    # If we have services, verify the structure
    if services:
        service = services[0]
        assert hasattr(service, 'name')
        assert hasattr(service, 'namespace')
        assert hasattr(service, 'type')
        assert hasattr(service, 'cluster_ip')
        assert hasattr(service, 'external_ip')
        assert hasattr(service, 'ports')
        assert hasattr(service, 'age')
        assert isinstance(service.ports, list)
        # If there are ports, verify their structure
        if service.ports:
            port = service.ports[0]
            assert hasattr(port, 'port')
            assert hasattr(port, 'protocol')
            assert isinstance(port.port, int)
            assert isinstance(port.protocol, str)
    
    # Test namespace-specific services
    default_services = k8s_tools.get_service_summaries("default")
    assert isinstance(default_services, list)

# ---------------------------------------------------------------------------
# Tests for the 1.1.0 tools (ConfigMaps, StatefulSets, CronJobs/Jobs, PVCs,
# cluster-wide events) and the node/service/log field enhancements.
#
# These scan all namespaces rather than just "default", since the resource
# types below are often only present in system namespaces. Where a cluster has
# none of a given resource, the test verifies the empty-result contract and
# skips the per-object field checks.
# ---------------------------------------------------------------------------

def _find_running_pod(namespace: str = "default"):
    """Returns a (pod_name, container_name) for a pod with a ready container, or None."""
    if k8s_tools.K8S is None:
        k8s_tools.K8S = k8s_tools._get_api_client()
    for pod in k8s_tools.K8S.list_namespaced_pod(namespace=namespace).items:
        if (pod.status.phase == "Running" and
                pod.status.container_statuses and
                any(cs.ready for cs in pod.status.container_statuses)):
            ready = [cs.name for cs in pod.status.container_statuses if cs.ready]
            return pod.metadata.name, ready[0]
    return None


def test_get_configmap_summaries():
    configmaps = k8s_tools.get_configmap_summaries()
    assert isinstance(configmaps, list)
    if configmaps:
        cm = configmaps[0]
        assert isinstance(cm.name, str) and len(cm.name) > 0
        assert isinstance(cm.namespace, str) and len(cm.namespace) > 0
        assert isinstance(cm.key_count, int)
        assert isinstance(cm.data_size, int)
        assert isinstance(cm.age, datetime.timedelta)

        # Logical constraints
        assert cm.key_count >= 0
        assert cm.data_size >= 0
        if cm.key_count == 0:
            assert cm.data_size == 0

    # Namespace scoping should be a subset of the all-namespaces result
    default_configmaps = k8s_tools.get_configmap_summaries("default")
    assert isinstance(default_configmaps, list)
    assert all(cm.namespace == "default" for cm in default_configmaps)
    assert len(default_configmaps) <= len(configmaps)


def test_get_configmap():
    configmaps = k8s_tools.get_configmap_summaries()
    if not configmaps:
        pytest.skip("No ConfigMaps found in the cluster.")
    summary = configmaps[0]
    cm = k8s_tools.get_configmap(summary.name, summary.namespace)
    assert isinstance(cm, dict)
    assert set(cm.keys()) == {"name", "namespace", "data", "binary_data_keys"}
    assert cm["name"] == summary.name
    assert cm["namespace"] == summary.namespace
    assert isinstance(cm["data"], dict)
    assert isinstance(cm["binary_data_keys"], list)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in cm["data"].items())
    assert all(isinstance(k, str) for k in cm["binary_data_keys"])

    # The summary's key count should agree with the full read
    assert summary.key_count == len(cm["data"]) + len(cm["binary_data_keys"])


def test_get_configmap_not_found():
    with pytest.raises(k8s_tools.K8sApiError):
        k8s_tools.get_configmap("no-such-configmap-xyzzy", "default")


def test_get_statefulset_summaries():
    statefulsets = k8s_tools.get_statefulset_summaries()
    assert isinstance(statefulsets, list)
    if not statefulsets:
        pytest.skip("No StatefulSets found in the cluster.")
    ss = statefulsets[0]
    assert isinstance(ss.name, str) and len(ss.name) > 0
    assert isinstance(ss.namespace, str) and len(ss.namespace) > 0
    assert isinstance(ss.total_replicas, int)
    assert isinstance(ss.ready_replicas, int)
    assert isinstance(ss.current_replicas, int)
    assert isinstance(ss.update_strategy, str)
    assert ss.service_name is None or isinstance(ss.service_name, str)
    assert isinstance(ss.age, datetime.timedelta)

    # Logical constraints
    assert ss.total_replicas >= 0
    assert ss.ready_replicas >= 0
    assert ss.ready_replicas <= ss.total_replicas
    assert ss.update_strategy in ["RollingUpdate", "OnDelete", ""]

    scoped = k8s_tools.get_statefulset_summaries(ss.namespace)
    assert all(s.namespace == ss.namespace for s in scoped)
    assert ss.name in [s.name for s in scoped]


def test_get_cronjob_summaries():
    cronjobs = k8s_tools.get_cronjob_summaries()
    assert isinstance(cronjobs, list)
    if not cronjobs:
        pytest.skip("No CronJobs found in the cluster.")
    cj = cronjobs[0]
    assert isinstance(cj.name, str) and len(cj.name) > 0
    assert isinstance(cj.namespace, str) and len(cj.namespace) > 0
    assert isinstance(cj.schedule, str) and len(cj.schedule) > 0
    assert isinstance(cj.suspend, bool)
    assert isinstance(cj.active, int) and cj.active >= 0
    assert cj.last_schedule_time is None or isinstance(cj.last_schedule_time, datetime.timedelta)
    assert cj.last_successful_time is None or isinstance(cj.last_successful_time, datetime.timedelta)
    assert isinstance(cj.age, datetime.timedelta)
    assert isinstance(cj.containers, list)
    for container in cj.containers:
        assert isinstance(container.name, str) and len(container.name) > 0
        assert isinstance(container.image, str) and len(container.image) > 0
        assert isinstance(container.env, dict)
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in container.env.items())

    scoped = k8s_tools.get_cronjob_summaries(cj.namespace)
    assert all(c.namespace == cj.namespace for c in scoped)


def test_get_job_summaries():
    jobs = k8s_tools.get_job_summaries()
    assert isinstance(jobs, list)
    if not jobs:
        pytest.skip("No Jobs found in the cluster.")
    job = jobs[0]
    assert isinstance(job.name, str) and len(job.name) > 0
    assert isinstance(job.namespace, str) and len(job.namespace) > 0
    assert job.owner is None or isinstance(job.owner, str)
    assert isinstance(job.active, int) and job.active >= 0
    assert isinstance(job.succeeded, int) and job.succeeded >= 0
    assert isinstance(job.failed, int) and job.failed >= 0
    assert job.start_time is None or isinstance(job.start_time, datetime.timedelta)
    assert job.completion_time is None or isinstance(job.completion_time, datetime.timedelta)
    assert isinstance(job.conditions, list)
    assert all(isinstance(c, str) for c in job.conditions)
    assert isinstance(job.age, datetime.timedelta)
    assert isinstance(job.containers, list)
    for container in job.containers:
        assert isinstance(container.name, str)
        assert isinstance(container.image, str)
        assert isinstance(container.env, dict)

    # A completed job should have finished no earlier than it started. The fields
    # are ages, so the completion age is the *smaller* of the two.
    if job.start_time is not None and job.completion_time is not None:
        assert job.completion_time <= job.start_time

    scoped = k8s_tools.get_job_summaries(job.namespace)
    assert all(j.namespace == job.namespace for j in scoped)


def test_get_logs_for_job():
    jobs = k8s_tools.get_job_summaries()
    if not jobs:
        pytest.skip("No Jobs found in the cluster.")
    job = jobs[0]
    try:
        logs = k8s_tools.get_logs_for_job(job.name, job.namespace)
    except k8s_tools.K8sApiError as e:
        # A job's pods may have been garbage collected or not be ready yet
        assert "Error fetching logs" in str(e)
        return
    assert logs is None or isinstance(logs, str)


def test_get_logs_for_job_no_such_job():
    # A job with no pods returns None rather than raising
    assert k8s_tools.get_logs_for_job("no-such-job-xyzzy", "default") is None


def test_get_logs_for_cronjob():
    cronjobs = k8s_tools.get_cronjob_summaries()
    if not cronjobs:
        pytest.skip("No CronJobs found in the cluster.")
    cj = cronjobs[0]
    try:
        logs = k8s_tools.get_logs_for_cronjob(cj.name, cj.namespace)
    except k8s_tools.K8sApiError as e:
        assert "Error fetching logs" in str(e)
        return
    assert logs is None or isinstance(logs, str)


def test_get_logs_for_cronjob_no_such_cronjob():
    # A cronjob that has never run (or does not exist) returns None
    assert k8s_tools.get_logs_for_cronjob("no-such-cronjob-xyzzy", "default") is None


def test_get_pvc_summaries():
    pvcs = k8s_tools.get_pvc_summaries()
    assert isinstance(pvcs, list)
    if not pvcs:
        pytest.skip("No PersistentVolumeClaims found in the cluster.")
    pvc = pvcs[0]
    assert isinstance(pvc.name, str) and len(pvc.name) > 0
    assert isinstance(pvc.namespace, str) and len(pvc.namespace) > 0
    assert isinstance(pvc.status, str)
    assert pvc.status in ["Bound", "Pending", "Lost"]
    assert pvc.volume_name is None or isinstance(pvc.volume_name, str)
    assert pvc.capacity is None or isinstance(pvc.capacity, str)
    assert isinstance(pvc.access_modes, list)
    assert all(isinstance(m, str) for m in pvc.access_modes)
    assert pvc.storage_class is None or isinstance(pvc.storage_class, str)
    assert isinstance(pvc.mounted_by, list)
    assert all(isinstance(p, str) for p in pvc.mounted_by)
    assert isinstance(pvc.age, datetime.timedelta)

    # A bound PVC should have a backing volume
    if pvc.status == "Bound":
        assert pvc.volume_name is not None

    scoped = k8s_tools.get_pvc_summaries(pvc.namespace)
    assert all(p.namespace == pvc.namespace for p in scoped)


def test_get_events():
    events = k8s_tools.get_events()
    assert isinstance(events, list)
    if not events:
        # The API only retains roughly the last hour of events, so an idle
        # cluster legitimately returns none.
        pytest.skip("No events currently retained by the cluster.")
    event = events[0]
    assert event.last_seen is None or isinstance(event.last_seen, datetime.timedelta)
    assert isinstance(event.type, str)
    assert event.type in ["Normal", "Warning"]
    assert isinstance(event.reason, str)
    assert isinstance(event.object, str)
    assert isinstance(event.message, str)


def test_get_events_filters():
    events = k8s_tools.get_events()
    if not events:
        pytest.skip("No events currently retained by the cluster.")

    # Filter by type
    for event_type in ["Normal", "Warning"]:
        filtered = k8s_tools.get_events(event_type=event_type)
        assert isinstance(filtered, list)
        assert all(e.type == event_type for e in filtered)
        assert len(filtered) <= len(events)

    # Filter by involved kind: `object` is rendered as "Kind/name"
    by_pod = k8s_tools.get_events(involved_kind="Pod")
    assert isinstance(by_pod, list)
    assert all(e.object.startswith("Pod/") for e in by_pod)

    # Filter by reason, using a reason we know is present
    reason = events[0].reason
    by_reason = k8s_tools.get_events(reason=reason)
    assert len(by_reason) > 0
    assert all(e.reason == reason for e in by_reason)

    # Filter by involved name, using an object we know is present
    kind, _, name = events[0].object.partition("/")
    if name:
        by_name = k8s_tools.get_events(involved_kind=kind, involved_name=name)
        assert len(by_name) > 0
        assert all(e.object == events[0].object for e in by_name)

    # A namespace filter should restrict the result set
    scoped = k8s_tools.get_events(namespace="default")
    assert isinstance(scoped, list)
    assert len(scoped) <= len(events)

    # A reason that cannot match returns an empty list, not an error
    assert k8s_tools.get_events(reason="NoSuchReasonXyzzy") == []


def test_get_logs_tail_and_since_seconds():
    """Covers the 1.1.0 tail/since_seconds/previous log options."""
    found = _find_running_pod("default")
    if found is None:
        pytest.skip("No running pods found in namespace 'default'.")
    pod_name, container_name = found

    full = k8s_tools.get_logs_for_pod_and_container(pod_name, "default", container_name)
    assert isinstance(full, str)

    # tail should never return more lines than the untruncated read
    tailed = k8s_tools.get_logs_for_pod_and_container(
        pod_name, "default", container_name, tail=5)
    assert isinstance(tailed, str)
    assert len(tailed.splitlines()) <= 5
    assert len(tailed.splitlines()) <= len(full.splitlines())

    # since_seconds is a time window, so it is also a subset of the full read
    recent = k8s_tools.get_logs_for_pod_and_container(
        pod_name, "default", container_name, since_seconds=60)
    assert isinstance(recent, str)
    assert len(recent.splitlines()) <= len(full.splitlines())

    # previous=True fails on a container that has not restarted; both the
    # success and the wrapped-error paths are acceptable, a crash is not.
    try:
        previous = k8s_tools.get_logs_for_pod_and_container(
            pod_name, "default", container_name, previous=True)
        assert previous is None or isinstance(previous, str)
    except k8s_tools.K8sApiError as e:
        assert "Error fetching logs" in str(e)


def test_node_summaries_capacity_and_conditions():
    """Covers the 1.1.0 NodeSummary additions used for capacity modelling."""
    nodes = k8s_tools.get_node_summaries()
    if not nodes:
        pytest.skip("No nodes found in the cluster.")
    node = nodes[0]
    assert isinstance(node.capacity, dict)
    assert isinstance(node.allocatable, dict)
    assert isinstance(node.conditions, dict)
    assert isinstance(node.taints, list)
    assert isinstance(node.labels, dict)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in node.capacity.items())
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in node.allocatable.items())
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in node.conditions.items())
    assert all(isinstance(t, str) for t in node.taints)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in node.labels.items())

    # Every real node reports cpu/memory/pods capacity, and the Ready condition
    for key in ["cpu", "memory", "pods"]:
        assert key in node.capacity
        assert key in node.allocatable
    assert "Ready" in node.conditions
    # The derived `status` field should agree with the Ready condition
    if node.conditions["Ready"] == "True":
        assert node.status == "Ready"

    # kubernetes.io/hostname is set by the kubelet on every node
    assert "kubernetes.io/hostname" in node.labels


def test_service_summaries_selector_and_labels():
    """Covers the 1.1.0 ServiceSummary additions (selector/labels/annotations)."""
    services = k8s_tools.get_service_summaries()
    if not services:
        pytest.skip("No services found in the cluster.")
    for service in services:
        assert isinstance(service.selector, dict)
        assert isinstance(service.labels, dict)
        assert isinstance(service.annotations, dict)
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in service.selector.items())
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in service.labels.items())
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in service.annotations.items())

    # The default/kubernetes service has no selector (its endpoints are managed
    # by the apiserver), which is the case that must not become a None.
    kubernetes_svc = [s for s in services
                      if s.name == "kubernetes" and s.namespace == "default"]
    if kubernetes_svc:
        assert kubernetes_svc[0].selector == {}


def test_selector_matches_pods():
    """A service's selector should actually resolve to pods in the cluster."""
    services = k8s_tools.get_service_summaries("default")
    with_selectors = [s for s in services if s.selector]
    if not with_selectors:
        pytest.skip("No services with selectors in namespace 'default'.")
    if k8s_tools.K8S is None:
        k8s_tools.K8S = k8s_tools._get_api_client()

    # At least one selector in the namespace should match a running pod;
    # this catches a selector that is populated but malformed.
    matched = False
    for service in with_selectors:
        label_selector = ",".join(f"{k}={v}" for k, v in service.selector.items())
        pods = k8s_tools.K8S.list_namespaced_pod(
            namespace="default", label_selector=label_selector).items
        if pods:
            matched = True
            break
    assert matched


def _replicasets_or_skip():
    """Replica sets from the cluster, skipping the test if there are none.

    Every assertion below is written to fail on an empty result rather than
    pass vacuously: a tool that silently returned nothing must not look green.
    """
    replicasets = k8s_tools.get_replicaset_summaries()
    assert isinstance(replicasets, list)
    if not replicasets:
        # A Deployment always has at least one ReplicaSet, so "no replica sets"
        # is only believable when there are no deployments either. Otherwise
        # the tool is broken and must not be excused as an empty cluster.
        assert not k8s_tools.get_deployment_summaries(), \
            "cluster has deployments but get_replicaset_summaries returned nothing"
        pytest.skip("no replica sets in the cluster")
    return replicasets


def test_replicaset_summaries():
    replicasets = _replicasets_or_skip()
    for rs in replicasets:
        assert rs.name and isinstance(rs.name, str)
        assert rs.namespace and isinstance(rs.namespace, str)
        assert rs.owner_deployment is None or isinstance(rs.owner_deployment, str)
        assert rs.revision is None or isinstance(rs.revision, int)
        assert rs.desired_replicas >= 0
        assert rs.current_replicas >= 0
        assert rs.ready_replicas >= 0
        # A replica set always carries the revision's pod template, so the
        # image list is the payload of this tool and must never be empty.
        assert rs.images, f"{rs.name} reported no images"
        assert all(isinstance(i, str) and i for i in rs.images)
        assert isinstance(rs.age, datetime.timedelta)
        assert rs.age >= datetime.timedelta(0)


def test_replicaset_summaries_are_owned_by_real_deployments():
    """owner_deployment must name a deployment that actually exists."""
    replicasets = _replicasets_or_skip()
    deployments = {(d.namespace, d.name)
                   for d in k8s_tools.get_deployment_summaries()}
    owned = [rs for rs in replicasets if rs.owner_deployment is not None]
    assert owned, "expected at least one deployment-owned replica set"
    for rs in owned:
        assert (rs.namespace, rs.owner_deployment) in deployments, \
            f"{rs.name} claims owner {rs.owner_deployment}, which does not exist"


def test_replicaset_summaries_filtered_by_deployment():
    """A deployment's replica sets are its revision history, newest last."""
    replicasets = _replicasets_or_skip()
    target = next((rs for rs in replicasets if rs.owner_deployment), None)
    if target is None:
        pytest.skip("no deployment-owned replica sets in the cluster")

    owned = k8s_tools.get_replicaset_summaries(namespace=target.namespace,
                                               deployment=target.owner_deployment)
    # Non-empty is the point: an always-empty filter would satisfy every
    # all(...) check below without ever exercising the tool.
    assert owned, f"deployment {target.owner_deployment} has no replica sets"
    assert all(rs.owner_deployment == target.owner_deployment for rs in owned)
    assert all(rs.namespace == target.namespace for rs in owned)

    # The filtered result must be exactly what filtering the full list gives.
    expected = {rs.name for rs in replicasets
                if rs.namespace == target.namespace
                and rs.owner_deployment == target.owner_deployment}
    assert {rs.name for rs in owned} == expected

    revisions = [rs.revision for rs in owned if rs.revision is not None]
    assert revisions == sorted(revisions), "revisions must be oldest first"


def test_replicaset_revision_history_is_ordered_oldest_first():
    """For a deployment that has been upgraded, check the history reads in order."""
    replicasets = _replicasets_or_skip()
    by_deployment = {}
    for rs in replicasets:
        if rs.owner_deployment and rs.revision is not None:
            by_deployment.setdefault((rs.namespace, rs.owner_deployment), []).append(rs)
    multi = {k: v for k, v in by_deployment.items() if len(v) > 1}
    if not multi:
        pytest.skip("no deployment in the cluster has more than one revision")

    for (namespace, deployment), _ in multi.items():
        history = k8s_tools.get_replicaset_summaries(namespace=namespace,
                                                     deployment=deployment)
        revisions = [rs.revision for rs in history if rs.revision is not None]
        assert revisions == sorted(revisions)
        assert len(set(revisions)) == len(revisions), \
            f"{deployment} has duplicate revision numbers: {revisions}"
        # The newest revision is the most recently created, so it is youngest.
        newest = history[-1]
        assert all(newest.age <= rs.age for rs in history[:-1]), \
            f"{deployment}: newest revision {newest.revision} is not the youngest"


def test_replicaset_summaries_namespace_filter():
    replicasets = _replicasets_or_skip()
    ns = replicasets[0].namespace
    filtered = k8s_tools.get_replicaset_summaries(namespace=ns)
    assert filtered, f"namespace {ns} has replica sets but the filter returned none"
    assert all(rs.namespace == ns for rs in filtered)
    assert {rs.name for rs in filtered} == {rs.name for rs in replicasets
                                            if rs.namespace == ns}


def test_replicaset_summaries_unknown_deployment_is_empty():
    _replicasets_or_skip()
    assert k8s_tools.get_replicaset_summaries(
        deployment="no-such-deployment-xyzzy") == []
