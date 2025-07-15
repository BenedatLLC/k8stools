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

    def read_namespaced_pod_log(self, name, namespace, container=None, follow=False, _preload_content=True, timestamps=True):
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

# overwrite the real k8s client with a mock
k8s_tools.K8S = MockK8S()

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

@pytest.fixture(scope="module", autouse=True)
def reset_k8s_tools_K8S():
    yield
    k8s_tools.K8S = None
