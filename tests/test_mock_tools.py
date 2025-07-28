"""Tests for mock_tools.py - verifies that all mock functions work correctly."""

import datetime
import pytest
from k8stools import mock_tools, k8s_tools


class TestMockNamespaces:
    """Test mock get_namespaces function."""
    
    def test_get_namespaces_returns_list(self):
        """Test that get_namespaces returns a list of NamespaceSummary objects."""
        namespaces = mock_tools.get_namespaces()
        assert isinstance(namespaces, list)
        assert len(namespaces) > 0
        
    def test_get_namespaces_returns_correct_types(self):
        """Test that get_namespaces returns correct types."""
        namespaces = mock_tools.get_namespaces()
        for ns in namespaces:
            assert isinstance(ns, k8s_tools.NamespaceSummary)
            assert isinstance(ns.name, str)
            assert isinstance(ns.status, str)
            assert isinstance(ns.age, datetime.timedelta)
            
    def test_get_namespaces_has_expected_namespaces(self):
        """Test that get_namespaces includes expected namespaces."""
        namespaces = mock_tools.get_namespaces()
        namespace_names = [ns.name for ns in namespaces]
        assert "default" in namespace_names
        assert "kube-system" in namespace_names


class TestMockNodes:
    """Test mock get_node_summaries function."""
    
    def test_get_node_summaries_returns_list(self):
        """Test that get_node_summaries returns a list of NodeSummary objects."""
        nodes = mock_tools.get_node_summaries()
        assert isinstance(nodes, list)
        assert len(nodes) > 0
        
    def test_get_node_summaries_returns_correct_types(self):
        """Test that get_node_summaries returns correct types."""
        nodes = mock_tools.get_node_summaries()
        for node in nodes:
            assert isinstance(node, k8s_tools.NodeSummary)
            assert isinstance(node.name, str)
            assert isinstance(node.status, str)
            assert isinstance(node.roles, list)
            assert isinstance(node.age, datetime.timedelta)
            assert isinstance(node.version, str)
            
    def test_get_node_summaries_has_minikube(self):
        """Test that get_node_summaries includes minikube node."""
        nodes = mock_tools.get_node_summaries()
        node_names = [node.name for node in nodes]
        assert "minikube" in node_names


class TestMockPods:
    """Test mock get_pod_summaries function."""
    
    def test_get_pod_summaries_returns_list(self):
        """Test that get_pod_summaries returns a list of PodSummary objects."""
        pods = mock_tools.get_pod_summaries()
        assert isinstance(pods, list)
        assert len(pods) > 0
        
    def test_get_pod_summaries_returns_correct_types(self):
        """Test that get_pod_summaries returns correct types."""
        pods = mock_tools.get_pod_summaries()
        for pod in pods:
            assert isinstance(pod, k8s_tools.PodSummary)
            assert isinstance(pod.name, str)
            assert isinstance(pod.namespace, str)
            assert isinstance(pod.total_containers, int)
            assert isinstance(pod.ready_containers, int)
            assert isinstance(pod.restarts, int)
            assert isinstance(pod.age, datetime.timedelta)
            
    def test_get_pod_summaries_namespace_filtering(self):
        """Test that get_pod_summaries filters by namespace correctly."""
        all_pods = mock_tools.get_pod_summaries()
        default_pods = mock_tools.get_pod_summaries("default")
        kube_system_pods = mock_tools.get_pod_summaries("kube-system")
        
        # All pods should be more than or equal to default pods
        assert len(all_pods) >= len(default_pods)
        
        # Default pods should only contain default namespace pods
        for pod in default_pods:
            assert pod.namespace == "default"
            
        # Kube-system pods should only contain kube-system namespace pods  
        for pod in kube_system_pods:
            assert pod.namespace == "kube-system"
            
    def test_get_pod_summaries_has_ad_pod(self):
        """Test that get_pod_summaries includes the ad pod."""
        pods = mock_tools.get_pod_summaries()
        pod_names = [pod.name for pod in pods]
        assert "ad-647b4947cc-s5mpm" in pod_names


class TestMockPodContainerStatuses:
    """Test mock get_pod_container_statuses function."""
    
    def test_get_pod_container_statuses_returns_list(self):
        """Test that get_pod_container_statuses returns a list."""
        statuses = mock_tools.get_pod_container_statuses("ad-647b4947cc-s5mpm", "default")
        assert isinstance(statuses, list)
        
    def test_get_pod_container_statuses_ad_pod(self):
        """Test container statuses for the specific ad pod."""
        statuses = mock_tools.get_pod_container_statuses("ad-647b4947cc-s5mpm", "default")
        assert len(statuses) == 1
        
        status = statuses[0]
        assert isinstance(status, k8s_tools.ContainerStatus)
        assert status.pod_name == "ad-647b4947cc-s5mpm"
        assert status.namespace == "default"
        assert status.container_name == "ad"
        assert status.image == "ghcr.io/open-telemetry/demo:2.0.2-ad"
        assert status.ready is False
        assert status.restart_count == 93
        
    def test_get_pod_container_statuses_generic_pod(self):
        """Test container statuses for a generic pod."""
        statuses = mock_tools.get_pod_container_statuses("test-pod-123", "default")
        assert len(statuses) == 2  # test-pod-123 has 2 containers
        
        for status in statuses:
            assert isinstance(status, k8s_tools.ContainerStatus)
            assert status.pod_name == "test-pod-123"
            assert status.namespace == "default"
            
    def test_get_pod_container_statuses_nonexistent_pod(self):
        """Test container statuses for a non-existent pod."""
        statuses = mock_tools.get_pod_container_statuses("nonexistent-pod", "default")
        assert statuses == []


class TestMockPodEvents:
    """Test mock get_pod_events function."""
    
    def test_get_pod_events_returns_list(self):
        """Test that get_pod_events returns a list."""
        events = mock_tools.get_pod_events("ad-647b4947cc-s5mpm", "default")
        assert isinstance(events, list)
        
    def test_get_pod_events_ad_pod(self):
        """Test events for the specific ad pod."""
        events = mock_tools.get_pod_events("ad-647b4947cc-s5mpm", "default")
        assert len(events) == 2
        
        for event in events:
            assert isinstance(event, k8s_tools.EventSummary)
            assert isinstance(event.last_seen, datetime.timedelta)
            assert isinstance(event.type, str)
            assert isinstance(event.reason, str)
            assert event.object == "ad-647b4947cc-s5mpm"
            
    def test_get_pod_events_generic_pod(self):
        """Test events for a generic pod."""
        events = mock_tools.get_pod_events("test-pod", "default")
        assert len(events) == 2
        
        for event in events:
            assert isinstance(event, k8s_tools.EventSummary)
            assert event.object == "test-pod"
            assert event.type == "Normal"


class TestMockPodSpec:
    """Test mock get_pod_spec function."""
    
    def test_get_pod_spec_returns_dict(self):
        """Test that get_pod_spec returns a dictionary."""
        spec = mock_tools.get_pod_spec("ad-647b4947cc-s5mpm", "default")
        assert isinstance(spec, dict)
        
    def test_get_pod_spec_ad_pod(self):
        """Test spec for the specific ad pod."""
        spec = mock_tools.get_pod_spec("ad-647b4947cc-s5mpm", "default")
        assert "containers" in spec
        assert "restart_policy" in spec
        assert "node_name" in spec
        
        containers = spec["containers"]
        assert len(containers) == 1
        assert containers[0]["name"] == "ad"
        assert containers[0]["image"] == "ghcr.io/open-telemetry/demo:2.0.2-ad"
        assert spec["restart_policy"] == "Always"
        assert spec["node_name"] == "minikube"
        
    def test_get_pod_spec_generic_pod(self):
        """Test spec for a generic pod."""
        spec = mock_tools.get_pod_spec("test-pod", "default")
        assert "containers" in spec
        assert "restart_policy" in spec
        assert "node_name" in spec
        
        containers = spec["containers"]
        assert len(containers) == 1
        assert containers[0]["name"] == "test"
        assert containers[0]["image"] == "nginx:latest"


class TestMockPodLogs:
    """Test mock get_logs_for_pod_and_container function."""
    
    def test_get_logs_returns_string(self):
        """Test that get_logs_for_pod_and_container returns a string."""
        logs = mock_tools.get_logs_for_pod_and_container("ad-647b4947cc-s5mpm", "default")
        assert logs is not None
        assert isinstance(logs, str)
        
    def test_get_logs_ad_pod(self):
        """Test logs for the specific ad pod."""
        logs = mock_tools.get_logs_for_pod_and_container("ad-647b4947cc-s5mpm", "default")
        assert logs is not None
        assert "JAVA_TOOL_OPTIONS" in logs
        assert "opentelemetry-javaagent.jar" in logs
        
    def test_get_logs_generic_pod(self):
        """Test logs for a generic pod."""
        logs = mock_tools.get_logs_for_pod_and_container("test-pod", "default")
        assert logs is not None
        assert "Starting test container" in logs
        assert "Ready to serve traffic" in logs
        
    def test_get_logs_with_container_name(self):
        """Test logs with specific container name."""
        logs = mock_tools.get_logs_for_pod_and_container("test-pod", "default", "mycontainer")
        assert logs is not None
        assert "Starting mycontainer container" in logs


class TestMockDeployments:
    """Test mock get_deployment_summaries function."""
    
    def test_get_deployment_summaries_returns_list(self):
        """Test that get_deployment_summaries returns a list."""
        deployments = mock_tools.get_deployment_summaries()
        assert isinstance(deployments, list)
        assert len(deployments) > 0
        
    def test_get_deployment_summaries_returns_correct_types(self):
        """Test that get_deployment_summaries returns correct types."""
        deployments = mock_tools.get_deployment_summaries()
        for deployment in deployments:
            assert isinstance(deployment, k8s_tools.DeploymentSummary)
            assert isinstance(deployment.name, str)
            assert isinstance(deployment.namespace, str)
            assert isinstance(deployment.total_replicas, int)
            assert isinstance(deployment.ready_replicas, int)
            assert isinstance(deployment.age, datetime.timedelta)
            
    def test_get_deployment_summaries_namespace_filtering(self):
        """Test that get_deployment_summaries filters by namespace correctly."""
        all_deployments = mock_tools.get_deployment_summaries()
        default_deployments = mock_tools.get_deployment_summaries("default")
        nonexistent_deployments = mock_tools.get_deployment_summaries("nonexistent")
        
        # All deployments should be more than or equal to default deployments
        assert len(all_deployments) >= len(default_deployments)
        
        # Default deployments should only contain default namespace deployments
        for deployment in default_deployments:
            assert deployment.namespace == "default"
            
        # Non-existent namespace should return empty list
        assert nonexistent_deployments == []


class TestMockServices:
    """Test mock get_service_summaries function."""
    
    def test_get_service_summaries_returns_list(self):
        """Test that get_service_summaries returns a list."""
        services = mock_tools.get_service_summaries()
        assert isinstance(services, list)
        assert len(services) > 0
        
    def test_get_service_summaries_returns_correct_types(self):
        """Test that get_service_summaries returns correct types."""
        services = mock_tools.get_service_summaries()
        for service in services:
            assert isinstance(service, k8s_tools.ServiceSummary)
            assert isinstance(service.name, str)
            assert isinstance(service.namespace, str)
            assert isinstance(service.type, str)
            assert isinstance(service.ports, list)
            assert isinstance(service.age, datetime.timedelta)
            
            for port in service.ports:
                assert isinstance(port, k8s_tools.PortInfo)
                assert isinstance(port.port, int)
                assert isinstance(port.protocol, str)
                
    def test_get_service_summaries_namespace_filtering(self):
        """Test that get_service_summaries filters by namespace correctly."""
        all_services = mock_tools.get_service_summaries()
        default_services = mock_tools.get_service_summaries("default")
        nonexistent_services = mock_tools.get_service_summaries("nonexistent")
        
        # All services should be more than or equal to default services
        assert len(all_services) >= len(default_services)
        
        # Default services should only contain default namespace services
        for service in default_services:
            assert service.namespace == "default"
            
        # Non-existent namespace should return empty list
        assert nonexistent_services == []


class TestMockToolsConsistency:
    """Test consistency between mock tools and real tools."""
    
    def test_tools_list_matches(self):
        """Test that mock_tools.TOOLS matches k8s_tools.TOOLS in function names."""
        mock_tool_names = [tool.__name__ for tool in mock_tools.TOOLS]
        real_tool_names = [tool.__name__ for tool in k8s_tools.TOOLS]
        
        assert set(mock_tool_names) == set(real_tool_names)
        
    def test_function_signatures_match(self):
        """Test that mock functions have the same signatures as real functions."""
        import inspect
        
        for mock_tool in mock_tools.TOOLS:
            real_tool = getattr(k8s_tools, mock_tool.__name__)
            
            mock_sig = inspect.signature(mock_tool)
            real_sig = inspect.signature(real_tool)
            
            assert mock_sig == real_sig, f"Signature mismatch for {mock_tool.__name__}"
            
    def test_docstrings_copied(self):
        """Test that mock functions have the same docstrings as real functions."""
        for mock_tool in mock_tools.TOOLS:
            real_tool = getattr(k8s_tools, mock_tool.__name__)
            
            # The mock functions should have the same docstring as the real functions
            assert mock_tool.__doc__ == real_tool.__doc__, f"Docstring mismatch for {mock_tool.__name__}"
