"""This module provides mocks for the tool functions. For each tool in k8s_tools.TOOLS, it provides
an equivalent mock that has the same function signature and returns mock data of the same type.
This is useful in writing tests for clients of this package (e.g. your agent).
"""
import datetime
from typing import Optional, Any
from . import k8s_tools

def _get_static_mock_data():
    """Static mock data for testing"""
    now = datetime.datetime.now(datetime.timezone.utc)
    
    return {
        'namespaces': [
            k8s_tools.NamespaceSummary(
                name="default", 
                status="Active", 
                age=now - datetime.datetime(2025, 7, 20, tzinfo=datetime.timezone.utc)
            ),
            k8s_tools.NamespaceSummary(
                name="kube-system", 
                status="Active", 
                age=now - datetime.datetime(2025, 7, 20, tzinfo=datetime.timezone.utc)
            ),
        ],
        'nodes': [
            k8s_tools.NodeSummary(
                name="minikube",
                status="Ready",
                roles=["control-plane"],
                age=datetime.timedelta(days=7),
                version="v1.30.0",
                internal_ip="192.168.49.2",
                external_ip=None,
                os_image="Ubuntu 22.04.4 LTS",
                kernel_version="5.15.0-112-generic",
                container_runtime="docker://26.1.1"
            )
        ],
        'pods': [
            k8s_tools.PodSummary(
                name="ad-647b4947cc-s5mpm",
                namespace="default",
                total_containers=1,
                ready_containers=0,
                restarts=93,
                last_restart=datetime.timedelta(minutes=1),
                age=datetime.timedelta(hours=7, minutes=34),
                ip="10.244.0.6",
                node="minikube"
            ),
            k8s_tools.PodSummary(
                name="test-pod-123",
                namespace="default",
                total_containers=2,
                ready_containers=2,
                restarts=0,
                last_restart=None,
                age=datetime.timedelta(hours=2),
                ip="10.244.0.10",
                node="minikube"
            ),
            k8s_tools.PodSummary(
                name="kube-system-pod",
                namespace="kube-system",
                total_containers=1,
                ready_containers=1,
                restarts=0,
                last_restart=None,
                age=datetime.timedelta(days=1),
                ip="10.244.0.5",
                node="minikube"
            )
        ],
        'deployments': [
            k8s_tools.DeploymentSummary(
                name="ad",
                namespace="default",
                total_replicas=1,
                ready_replicas=0,
                up_to_date_relicas=1,
                available_replicas=0,
                age=datetime.timedelta(hours=7, minutes=34)
            ),
            k8s_tools.DeploymentSummary(
                name="test-deployment",
                namespace="default",
                total_replicas=3,
                ready_replicas=3,
                up_to_date_relicas=3,
                available_replicas=3,
                age=datetime.timedelta(hours=2)
            )
        ],
        'services': [
            k8s_tools.ServiceSummary(
                name="ad",
                namespace="default",
                type="ClusterIP",
                cluster_ip="10.96.1.100",
                external_ip=None,
                ports=[k8s_tools.PortInfo(port=8080, protocol="TCP")],
                age=datetime.timedelta(hours=7, minutes=34)
            ),
            k8s_tools.ServiceSummary(
                name="test-service",
                namespace="default",
                type="LoadBalancer",
                cluster_ip="10.96.1.200",
                external_ip="192.168.1.100",
                ports=[
                    k8s_tools.PortInfo(port=80, protocol="TCP"),
                    k8s_tools.PortInfo(port=443, protocol="TCP")
                ],
                age=datetime.timedelta(hours=2)
            )
        ],
        'ad_pod_container_statuses': [
            k8s_tools.ContainerStatus(
                pod_name="ad-647b4947cc-s5mpm",
                namespace="default",
                container_name="ad",
                image="ghcr.io/open-telemetry/demo:2.0.2-ad",
                ready=False,
                restart_count=93,
                started=False,
                stop_signal=None,
                state=k8s_tools.ContainerStateWaiting(
                    reason="CrashLoopBackOff",
                    message="back-off 5m0s restarting failed container"
                ),
                last_state=k8s_tools.ContainerStateTerminated(
                    exit_code=137,
                    reason="OOMKilled",
                    finished_at=now - datetime.timedelta(minutes=2),
                    started_at=now - datetime.timedelta(minutes=2, seconds=2)
                ),
                volume_mounts=[
                    k8s_tools.VolumeMountStatus(
                        mount_path="/var/run/secrets/kubernetes.io/serviceaccount",
                        name="kube-api-access-zwmhp",
                        read_only=True,
                        recursive_read_only="Disabled"
                    )
                ],
                resource_requests={"memory": "300Mi"},
                resource_limits={"memory": "300Mi"},
                allocated_resources={"memory": "300Mi"}
            )
        ],
        'ad_pod_events': [
            k8s_tools.EventSummary(
                last_seen=datetime.timedelta(minutes=1),
                type="Normal",
                reason="Pulled",
                object="ad-647b4947cc-s5mpm",
                message="Container image already present on machine"
            ),
            k8s_tools.EventSummary(
                last_seen=datetime.timedelta(minutes=4),
                type="Warning",
                reason="BackOff",
                object="ad-647b4947cc-s5mpm",
                message="Back-off restarting failed container"
            )
        ],
        'ad_pod_spec': {
            "containers": [{
                "name": "ad",
                "image": "ghcr.io/open-telemetry/demo:2.0.2-ad",
                "ports": [{"containerPort": 8080, "protocol": "TCP"}],
                "resources": {
                    "limits": {"memory": "300Mi"},
                    "requests": {"memory": "300Mi"}
                }
            }],
            "restart_policy": "Always",
            "node_name": "minikube"
        },
        'ad_pod_logs': "2025-07-28T01:50:52.678740495Z Picked up JAVA_TOOL_OPTIONS: -javaagent:/usr/src/app/opentelemetry-javaagent.jar\n2025-07-28T01:50:52.758344990Z OpenJDK 64-Bit Server VM warning: Sharing is only supported for boot loader classes"
    }

# Initialize the mock data
_MOCK_DATA = _get_static_mock_data()


def get_namespaces() -> list[k8s_tools.NamespaceSummary]:
    """Mock implementation that returns static namespace data"""
    return _MOCK_DATA['namespaces']

get_namespaces.__doc__ = k8s_tools.get_namespaces.__doc__


def get_node_summaries() -> list[k8s_tools.NodeSummary]:
    """Mock implementation that returns static node data"""
    return _MOCK_DATA['nodes']

get_node_summaries.__doc__ = k8s_tools.get_node_summaries.__doc__


def get_pod_summaries(namespace: Optional[str] = None) -> list[k8s_tools.PodSummary]:
    """Mock implementation that returns static pod data, filtered by namespace if specified"""
    pods = _MOCK_DATA['pods']
    
    if namespace is not None:
        return [pod for pod in pods if pod.namespace == namespace]
    return pods

get_pod_summaries.__doc__ = k8s_tools.get_pod_summaries.__doc__


def get_pod_container_statuses(pod_name: str, namespace: str = "default") -> list[k8s_tools.ContainerStatus]:
    """Mock implementation that returns static container status data for the specified pod"""
    
    # For the specific ad pod, return cached data
    if pod_name == "ad-647b4947cc-s5mpm" and namespace == "default":
        return _MOCK_DATA['ad_pod_container_statuses']
    
    # For other pods, filter from all pods and create mock container statuses
    pods = _MOCK_DATA['pods']
    matching_pods = [pod for pod in pods if pod.name == pod_name and pod.namespace == namespace]
    
    if not matching_pods:
        return []
    
    pod = matching_pods[0]
    # Create mock container statuses based on pod summary
    statuses = []
    for i in range(pod.total_containers):
        container_name = f"container-{i+1}" if pod.total_containers > 1 else pod_name.split('-')[0]
        statuses.append(
            k8s_tools.ContainerStatus(
                pod_name=pod_name,
                namespace=namespace,
                container_name=container_name,
                image="nginx:latest",
                ready=i < pod.ready_containers,
                restart_count=pod.restarts // pod.total_containers,
                started=i < pod.ready_containers,
                stop_signal=None,
                state=k8s_tools.ContainerStateRunning(
                    started_at=datetime.datetime.now(datetime.timezone.utc) - pod.age
                ) if i < pod.ready_containers else k8s_tools.ContainerStateWaiting(
                    reason="ImagePullBackOff",
                    message="Unable to pull image"
                ),
                last_state=None,
                volume_mounts=[],
                resource_requests={},
                resource_limits={},
                allocated_resources={}
            )
        )
    return statuses

get_pod_container_statuses.__doc__ = k8s_tools.get_pod_container_statuses.__doc__


def get_pod_events(pod_name: str, namespace: str = "default") -> list[k8s_tools.EventSummary]:
    """Mock implementation that returns static event data for the specified pod"""
    
    # For the specific ad pod, return cached data
    if pod_name == "ad-647b4947cc-s5mpm" and namespace == "default":
        return _MOCK_DATA['ad_pod_events']
    
    # For other pods, return generic mock events
    return [
        k8s_tools.EventSummary(
            last_seen=datetime.timedelta(minutes=5),
            type="Normal",
            reason="Scheduled",
            object=pod_name,
            message=f"Successfully assigned {namespace}/{pod_name} to minikube"
        ),
        k8s_tools.EventSummary(
            last_seen=datetime.timedelta(minutes=3),
            type="Normal",
            reason="Pulled",
            object=pod_name,
            message="Container image pulled successfully"
        )
    ]

get_pod_events.__doc__ = k8s_tools.get_pod_events.__doc__


def get_pod_spec(pod_name: str, namespace: str = "default") -> dict[str, Any]:
    """Mock implementation that returns static pod spec data for the specified pod"""
    
    # For the specific ad pod, return cached data
    if pod_name == "ad-647b4947cc-s5mpm" and namespace == "default":
        return _MOCK_DATA['ad_pod_spec']
    
    # For other pods, return generic mock spec
    return {
        "containers": [{
            "name": pod_name.split('-')[0],
            "image": "nginx:latest",
            "ports": [{"containerPort": 80, "protocol": "TCP"}],
            "resources": {}
        }],
        "restart_policy": "Always",
        "node_name": "minikube"
    }

get_pod_spec.__doc__ = k8s_tools.get_pod_spec.__doc__


def get_logs_for_pod_and_container(pod_name: str, namespace: str = "default", container_name: Optional[str] = None) -> Optional[str]:
    """Mock implementation that returns static log data for the specified pod and container"""
    
    # For the specific ad pod, return cached data
    if pod_name == "ad-647b4947cc-s5mpm" and namespace == "default":
        return _MOCK_DATA['ad_pod_logs']
    
    # For other pods, return generic mock logs
    container_ref = container_name or pod_name.split('-')[0]
    return f"""2025-07-28T01:30:00.000000000Z Starting {container_ref} container
2025-07-28T01:30:01.000000000Z {container_ref} container started successfully
2025-07-28T01:30:02.000000000Z Processing requests...
2025-07-28T01:30:03.000000000Z Ready to serve traffic"""

get_logs_for_pod_and_container.__doc__ = k8s_tools.get_logs_for_pod_and_container.__doc__


def get_deployment_summaries(namespace: Optional[str] = None) -> list[k8s_tools.DeploymentSummary]:
    """Mock implementation that returns static deployment data, filtered by namespace if specified"""
    deployments = _MOCK_DATA['deployments']
    
    if namespace is not None:
        return [deployment for deployment in deployments if deployment.namespace == namespace]
    return deployments

get_deployment_summaries.__doc__ = k8s_tools.get_deployment_summaries.__doc__


def get_service_summaries(namespace: Optional[str] = None) -> list[k8s_tools.ServiceSummary]:
    """Mock implementation that returns static service data, filtered by namespace if specified"""
    services = _MOCK_DATA['services']
    
    if namespace is not None:
        return [service for service in services if service.namespace == namespace]
    return services

get_service_summaries.__doc__ = k8s_tools.get_service_summaries.__doc__


TOOLS = [
    get_namespaces,
    get_node_summaries,
    get_pod_summaries,
    get_pod_container_statuses,
    get_pod_events,
    get_pod_spec,
    get_logs_for_pod_and_container,
    get_deployment_summaries,
    get_service_summaries
]