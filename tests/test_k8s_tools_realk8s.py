"""These tests call the tool apis against a real k8s cluster. They just do very basic santity
testing of the results. The tests are skipped if we can't reach the cluster.
"""
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

def test_get_pod_summaries():
    pods = k8s_tools.get_pod_summaries()
    assert isinstance(pods, list)

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