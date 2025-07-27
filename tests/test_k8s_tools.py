"""Tests for our k8s tools. we mock out the connection to kubernetes (k8s_tools.K8S).
"""

import datetime
from types import SimpleNamespace
from k8stools import k8s_tools
from unittest.mock import patch
import pytest

class MockK8S:
    def list_namespace(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        ns1 = SimpleNamespace(
            metadata=SimpleNamespace(name="default", creation_timestamp=now - datetime.timedelta(days=5)),
            status=SimpleNamespace(phase="Active")
        )
        ns2 = SimpleNamespace(
            metadata=SimpleNamespace(name="test", creation_timestamp=now - datetime.timedelta(days=2)),
            status=SimpleNamespace(phase="Active")
        )
        return SimpleNamespace(items=[ns1, ns2])

    def list_pod_for_all_namespaces(self):
        return self._mock_pods()

    def list_namespaced_pod(self, namespace):
        pods = [pod for pod in self._mock_pods().items if pod.metadata.namespace == namespace]
        return SimpleNamespace(items=pods)

    def read_namespaced_pod(self, name, namespace):
        for pod in self._mock_pods().items:
            if pod.metadata.name == name and pod.metadata.namespace == namespace:
                return pod
        return None

    def list_namespaced_event(self, namespace, field_selector=None):
        now = datetime.datetime.now(datetime.timezone.utc)
        event1 = SimpleNamespace(
            last_timestamp=now - datetime.timedelta(hours=1),
            type="Normal",
            reason="Started",
            involved_object=SimpleNamespace(name="pod-1"),
            message="Pod started successfully."
        )
        event2 = SimpleNamespace(
            last_timestamp=now - datetime.timedelta(minutes=30),
            type="Warning",
            reason="Failed",
            involved_object=SimpleNamespace(name="pod-1"),
            message="Pod failed to start."
        )
        return SimpleNamespace(items=[event1, event2])

    def read_namespaced_pod_log(self, name, namespace, container=None, follow=False, _preload_content=True, timestamps=True, tail_lines=None, limit_bytes=None):
        # Return a sample log string for testing
        if name == "pod-1" and namespace == "default" and container == "container-1":
            return "2025-07-12T00:00:00Z container-1 log line 1\n2025-07-12T00:01:00Z container-1 log line 2"
        return ""

    def _mock_pods(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        def spec_to_dict(self):
            return {
                "containers": [{"name": "container-1", "image": "nginx:latest"}],
                # Add other fields as needed for your tests
            }
        container_status = SimpleNamespace(
            ready=True,
            restart_count=1,
            last_state=SimpleNamespace(terminated=SimpleNamespace(started_at=now - datetime.timedelta(hours=3),
                                                                  finished_at=now - datetime.timedelta(hours=2),
                                                                  exit_code=137, reason='OOMKilled', message=None),
                                       running=None, waiting=None),
            state=SimpleNamespace(running=SimpleNamespace(started_at=now - datetime.timedelta(days=1)), waiting=None, terminated=None),
            name="container-1",
            image="nginx:latest",
            started=True,
            stop_signal=None,
            volume_mounts=[],
            resources=None,
            resource_requests={},
            resource_limits={},
            allocated_resources={}
        )
        container1 = SimpleNamespace(name="container-1", image="nginx:latest")
        container2 = SimpleNamespace(name="container-2", image="busybox:latest")
        spec1 = SimpleNamespace(containers=[container1])
        spec1.to_dict = spec_to_dict.__get__(spec1)
        pod1 = SimpleNamespace(
            metadata=SimpleNamespace(name="pod-1", namespace="default", creation_timestamp=now - datetime.timedelta(days=1)),
            spec=spec1,
            status=SimpleNamespace(container_statuses=[container_status]),
            to_dict=lambda self: dict(self)
        )
        spec2 = SimpleNamespace(containers=[container1, container2])
        spec2.to_dict = spec_to_dict.__get__(spec2)
        pod2 = SimpleNamespace(
            metadata=SimpleNamespace(name="pod-2", namespace="test", creation_timestamp=now - datetime.timedelta(hours=12)),
            spec=spec2,
            status=SimpleNamespace(container_statuses=[container_status, container_status]),
            to_dict=lambda self: dict(self)
        )
        return SimpleNamespace(items=[pod1, pod2])


class MockAppsV1Api:
    def list_namespaced_deployment(self, namespace):
        deployments = [dep for dep in self._mock_deployments().items if dep.metadata.namespace == namespace]
        return SimpleNamespace(items=deployments)
    
    def list_deployment_for_all_namespaces(self):
        return self._mock_deployments()
    
    def _mock_deployments(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        
        # Create mock deployment 1 in default namespace
        deployment1 = SimpleNamespace(
            metadata=SimpleNamespace(
                name="nginx-deployment",
                namespace="default",
                creation_timestamp=now - datetime.timedelta(days=2)
            ),
            spec=SimpleNamespace(replicas=3),
            status=SimpleNamespace(
                ready_replicas=2,
                updated_replicas=3,
                available_replicas=2
            )
        )
        
        # Create mock deployment 2 in test namespace
        deployment2 = SimpleNamespace(
            metadata=SimpleNamespace(
                name="app-deployment",
                namespace="test",
                creation_timestamp=now - datetime.timedelta(hours=6)
            ),
            spec=SimpleNamespace(replicas=1),
            status=SimpleNamespace(
                ready_replicas=1,
                updated_replicas=1,
                available_replicas=1
            )
        )
        
        return SimpleNamespace(items=[deployment1, deployment2])


@pytest.fixture(scope="module", autouse=True)
def setup_and_teardown_mock():
    """Set up mock K8S client before tests and clean up after."""
    # Store original values
    original_k8s = k8s_tools.K8S
    original_apps_v1 = k8s_tools.APPS_V1_API
    
    # Set up mocks
    k8s_tools.K8S = MockK8S()
    k8s_tools.APPS_V1_API = MockAppsV1Api()
    
    yield
    
    # Clean up - reset to original values
    k8s_tools.K8S = original_k8s
    k8s_tools.APPS_V1_API = original_apps_v1

# Note: We no longer set the global state at module level
# k8s_tools.K8S = MockK8S()  # Removed this line

def test_get_namespaces():
    namespaces = k8s_tools.get_namespaces()
    assert len(namespaces) == 2
    assert namespaces[0].name == "default"
    assert namespaces[1].name == "test"
    assert namespaces[0].status == "Active"
    assert isinstance(namespaces[0].age, datetime.timedelta)

def test_get_pod_summaries():
    pods = k8s_tools.get_pod_summaries()
    assert len(pods) == 2
    assert pods[0].name == "pod-1"
    assert pods[1].name == "pod-2"
    assert pods[0].namespace == "default"
    assert pods[1].namespace == "test"
    assert pods[0].total_containers == 1
    assert pods[1].total_containers == 2
    assert pods[0].ready_containers == 1
    assert pods[1].ready_containers == 2
    assert isinstance(pods[0].age, datetime.timedelta)

def test_get_pod_container_statuses():
    # Adjusted for new return type: should be instance of k8s_tools.ContainerStatus
    with patch.object(k8s_tools.client, "V1Pod", SimpleNamespace):
        statuses = k8s_tools.get_pod_container_statuses("pod-1", "default")
        assert isinstance(statuses, list)
        assert len(statuses) == 1
        cs = statuses[0]
        assert isinstance(cs, k8s_tools.ContainerStatus)
        assert cs.container_name == "container-1"
        assert cs.ready is True
        assert cs.restart_count == 1
        assert hasattr(cs, "last_state")

def test_get_pod_events():
    events = k8s_tools.get_pod_events("pod-1", "default")
    assert len(events) == 2
    assert events[0].type == "Normal"
    assert events[1].type == "Warning"
    assert events[0].reason == "Started"
    assert events[1].reason == "Failed"
    assert events[0].object == "pod-1"
    assert isinstance(events[0].last_seen, datetime.timedelta)

def test_get_pod_spec():
    with patch.object(k8s_tools.client, "V1Pod", SimpleNamespace):
        spec = k8s_tools.get_pod_spec("pod-1", "default")
        assert isinstance(spec, dict)
        assert "containers" in spec
        assert spec["containers"] is not None
        assert isinstance(spec["containers"], list)
        assert len(spec["containers"]) == 1
        assert spec["containers"][0]["name"] == "container-1"
        assert spec["containers"][0]["image"] == "nginx:latest"

def test_retrieve_logs_from_pod_and_container():
    logs = k8s_tools.get_logs_for_pod_and_container("pod-1", "default", "container-1")
    assert isinstance(logs, str)
    assert "container-1 log line 1" in logs
    assert "container-1 log line 2" in logs

def test_deployment_summaries():
    deployments = k8s_tools.get_deployment_summaries()
    assert isinstance(deployments, list)
    assert len(deployments) == 2
    
    # Check first deployment (nginx-deployment in default namespace)
    deployment1 = deployments[0]
    assert isinstance(deployment1, k8s_tools.DeploymentSummary)
    assert deployment1.name == "nginx-deployment"
    assert deployment1.namespace == "default"
    assert deployment1.total_replicas == 3
    assert deployment1.ready_replicas == 2
    assert deployment1.up_to_date_relicas == 3
    assert deployment1.available_replicas == 2
    assert isinstance(deployment1.age, datetime.timedelta)
    
    # Check second deployment (app-deployment in test namespace)
    deployment2 = deployments[1]
    assert isinstance(deployment2, k8s_tools.DeploymentSummary)
    assert deployment2.name == "app-deployment"
    assert deployment2.namespace == "test"
    assert deployment2.total_replicas == 1
    assert deployment2.ready_replicas == 1
    assert deployment2.up_to_date_relicas == 1
    assert deployment2.available_replicas == 1
    assert isinstance(deployment2.age, datetime.timedelta)
    
    # Test namespace-specific deployments
    default_deployments = k8s_tools.get_deployment_summaries("default")
    assert isinstance(default_deployments, list)
    assert len(default_deployments) == 1
    assert default_deployments[0].name == "nginx-deployment"
    assert default_deployments[0].namespace == "default"
