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
    pod_name = pod_list[0].metadata.name
    logs = k8s_tools.get_logs_for_pod_and_container(pod_name, "default")
    assert isinstance(logs, str)
