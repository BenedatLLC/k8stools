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
        'replicasets': [
            # Two revisions of `ad`: an image upgrade with the old revision
            # scaled to zero, which is what a Deployment's history looks like.
            # The `ad` deployment is 8 days old; it was upgraded 7h34m ago,
            # which created revision 2 and the pod ad-647b4947cc-s5mpm.
            k8s_tools.ReplicaSetSummary(
                name="ad-d5c94c49b",
                namespace="default",
                owner_deployment="ad",
                revision=1,
                desired_replicas=0,
                current_replicas=0,
                ready_replicas=0,
                images=["ghcr.io/open-telemetry/demo:2.0.2-ad"],
                age=datetime.timedelta(days=8)
            ),
            # Current revision. Named to match the running pod
            # ad-647b4947cc-s5mpm, and not ready, like the `ad` deployment.
            k8s_tools.ReplicaSetSummary(
                name="ad-647b4947cc",
                namespace="default",
                owner_deployment="ad",
                revision=2,
                desired_replicas=1,
                current_replicas=1,
                ready_replicas=0,
                images=["ghcr.io/open-telemetry/demo:2.2.0-ad"],
                age=datetime.timedelta(hours=7, minutes=34)
            ),
            # A never-upgraded deployment: a single revision, fully ready.
            k8s_tools.ReplicaSetSummary(
                name="test-deployment-7d4b9c85f",
                namespace="default",
                owner_deployment="test-deployment",
                revision=1,
                desired_replicas=3,
                current_replicas=3,
                ready_replicas=3,
                images=["ghcr.io/open-telemetry/demo:2.2.0-cart"],
                age=datetime.timedelta(hours=2)
            ),
        ],
        'deployments': [
            k8s_tools.DeploymentSummary(
                name="ad",
                namespace="default",
                total_replicas=1,
                ready_replicas=0,
                up_to_date_relicas=1,
                available_replicas=0,
                # Older than its newest replica set: created 8 days ago,
                # last upgraded 7h34m ago. See 'replicasets' above.
                age=datetime.timedelta(days=8)
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
                age=datetime.timedelta(days=8)
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
        'ad_pod_logs': "2025-07-28T01:50:52.678740495Z Picked up JAVA_TOOL_OPTIONS: -javaagent:/usr/src/app/opentelemetry-javaagent.jar\n2025-07-28T01:50:52.758344990Z OpenJDK 64-Bit Server VM warning: Sharing is only supported for boot loader classes",
        'configmaps': [
            k8s_tools.ConfigMapSummary(
                name="app-config", namespace="default",
                key_count=3, data_size=120, age=datetime.timedelta(days=1)
            ),
            k8s_tools.ConfigMapSummary(
                name="kube-root-ca.crt", namespace="kube-system",
                key_count=1, data_size=1099, age=datetime.timedelta(days=7)
            ),
        ],
        # The app-config map intentionally carries a secret-shaped value so the
        # MCP server's redaction pass has something to act on in mock mode.
        'app_config_data': {
            "name": "app-config",
            "namespace": "default",
            "data": {
                "LOG_LEVEL": "info",
                "MODEL_ALIAS": "gpt-4o",
                "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            },
            "binary_data_keys": [],
        },
        'statefulsets': [
            k8s_tools.StatefulSetSummary(
                name="postgres", namespace="default",
                total_replicas=1, ready_replicas=1, current_replicas=1,
                update_strategy="RollingUpdate", service_name="postgres",
                age=datetime.timedelta(days=3)
            ),
        ],
        'cronjobs': [
            k8s_tools.CronJobSummary(
                name="cleanup", namespace="default",
                schedule="*/5 * * * *", suspend=False, active=0,
                last_schedule_time=datetime.timedelta(minutes=2),
                last_successful_time=datetime.timedelta(minutes=2),
                age=datetime.timedelta(days=1),
                containers=[k8s_tools.ContainerTemplateSummary(
                    name="cleanup", image="busybox:latest", env={"TIMEOUT": "30"})],
            ),
        ],
        'jobs': [
            k8s_tools.JobSummary(
                name="cleanup-28999999", namespace="default", owner="cleanup",
                active=0, succeeded=1, failed=0,
                start_time=datetime.timedelta(minutes=2),
                completion_time=datetime.timedelta(minutes=1),
                conditions=["Complete"], age=datetime.timedelta(minutes=2),
                containers=[k8s_tools.ContainerTemplateSummary(
                    name="cleanup", image="busybox:latest", env={"TIMEOUT": "30"})],
            ),
        ],
        'pvcs': [
            k8s_tools.PVCSummary(
                name="data-postgres-0", namespace="default", status="Bound",
                volume_name="pvc-11112222", capacity="10Gi",
                access_modes=["ReadWriteOnce"], storage_class="standard",
                mounted_by=["postgres-0"], age=datetime.timedelta(days=3)
            ),
            k8s_tools.PVCSummary(
                name="orphaned-session-xyz", namespace="default", status="Bound",
                volume_name="pvc-33334444", capacity="1Gi",
                access_modes=["ReadWriteOnce"], storage_class="standard",
                mounted_by=[], age=datetime.timedelta(days=2)
            ),
        ],
        'events': [
            k8s_tools.EventSummary(
                last_seen=datetime.timedelta(minutes=1), type="Warning",
                reason="BackOff", object="Pod/ad-647b4947cc-s5mpm",
                message="Back-off restarting failed container"
            ),
            k8s_tools.EventSummary(
                last_seen=datetime.timedelta(minutes=10), type="Warning",
                reason="Evicted", object="Pod/session-abc",
                message="The node was low on resource: ephemeral-storage."
            ),
        ],
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


def get_logs_for_pod_and_container(pod_name: str, namespace: str = "default", container_name: Optional[str] = None, tail: Optional[int] = None, since_seconds: Optional[int] = None, previous: bool = False) -> Optional[str]:
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


def get_configmap_summaries(namespace: Optional[str] = None) -> list[k8s_tools.ConfigMapSummary]:
    """Mock implementation that returns static ConfigMap summary data, filtered by namespace if specified"""
    configmaps = _MOCK_DATA['configmaps']
    if namespace is not None:
        return [cm for cm in configmaps if cm.namespace == namespace]
    return configmaps

get_configmap_summaries.__doc__ = k8s_tools.get_configmap_summaries.__doc__


def get_configmap(name: str, namespace: str = "default") -> dict[str, Any]:
    """Mock implementation that returns static ConfigMap contents for the specified config map"""
    if name == "app-config" and namespace == "default":
        return _MOCK_DATA['app_config_data']
    return {"name": name, "namespace": namespace, "data": {}, "binary_data_keys": []}

get_configmap.__doc__ = k8s_tools.get_configmap.__doc__


def get_statefulset_summaries(namespace: Optional[str] = None) -> list[k8s_tools.StatefulSetSummary]:
    """Mock implementation that returns static StatefulSet data, filtered by namespace if specified"""
    statefulsets = _MOCK_DATA['statefulsets']
    if namespace is not None:
        return [sts for sts in statefulsets if sts.namespace == namespace]
    return statefulsets

get_statefulset_summaries.__doc__ = k8s_tools.get_statefulset_summaries.__doc__


def get_cronjob_summaries(namespace: Optional[str] = None) -> list[k8s_tools.CronJobSummary]:
    """Mock implementation that returns static CronJob data, filtered by namespace if specified"""
    cronjobs = _MOCK_DATA['cronjobs']
    if namespace is not None:
        return [cj for cj in cronjobs if cj.namespace == namespace]
    return cronjobs

get_cronjob_summaries.__doc__ = k8s_tools.get_cronjob_summaries.__doc__


def get_job_summaries(namespace: Optional[str] = None) -> list[k8s_tools.JobSummary]:
    """Mock implementation that returns static Job data, filtered by namespace if specified"""
    jobs = _MOCK_DATA['jobs']
    if namespace is not None:
        return [job for job in jobs if job.namespace == namespace]
    return jobs

get_job_summaries.__doc__ = k8s_tools.get_job_summaries.__doc__


def get_logs_for_job(job_name: str, namespace: str = "default", container_name: Optional[str] = None, tail: Optional[int] = None, since_seconds: Optional[int] = None, previous: bool = False) -> Optional[str]:
    """Mock implementation that returns static log data for a Job's most-recent pod"""
    container_ref = container_name or job_name.split('-')[0]
    return f"""2025-07-28T02:00:00.000000000Z {container_ref} job pod starting
2025-07-28T02:00:01.000000000Z {container_ref} processing batch
2025-07-28T02:00:02.000000000Z {container_ref} job completed successfully"""

get_logs_for_job.__doc__ = k8s_tools.get_logs_for_job.__doc__


def get_logs_for_cronjob(cronjob_name: str, namespace: str = "default", container_name: Optional[str] = None, tail: Optional[int] = None, since_seconds: Optional[int] = None, previous: bool = False) -> Optional[str]:
    """Mock implementation that returns static log data for a CronJob's most-recent run"""
    container_ref = container_name or cronjob_name.split('-')[0]
    return f"""2025-07-28T02:00:00.000000000Z {container_ref} cronjob run starting
2025-07-28T02:00:01.000000000Z {container_ref} tick completed"""

get_logs_for_cronjob.__doc__ = k8s_tools.get_logs_for_cronjob.__doc__


def get_pvc_summaries(namespace: Optional[str] = None) -> list[k8s_tools.PVCSummary]:
    """Mock implementation that returns static PVC data, filtered by namespace if specified"""
    pvcs = _MOCK_DATA['pvcs']
    if namespace is not None:
        return [pvc for pvc in pvcs if pvc.namespace == namespace]
    return pvcs

get_pvc_summaries.__doc__ = k8s_tools.get_pvc_summaries.__doc__


def get_events(namespace: Optional[str] = None, reason: Optional[str] = None, involved_kind: Optional[str] = None, involved_name: Optional[str] = None, event_type: Optional[str] = None) -> list[k8s_tools.EventSummary]:
    """Mock implementation that returns static event data with optional filtering"""
    events = _MOCK_DATA['events']
    result = []
    for event in events:
        # object is formatted "Kind/name"; split for filtering
        kind, _, name = event.object.partition('/')
        if reason is not None and event.reason != reason:
            continue
        if event_type is not None and event.type != event_type:
            continue
        if involved_kind is not None and kind != involved_kind:
            continue
        if involved_name is not None and name != involved_name:
            continue
        result.append(event)
    return result

get_events.__doc__ = k8s_tools.get_events.__doc__


def get_deployment_summaries(namespace: Optional[str] = None) -> list[k8s_tools.DeploymentSummary]:
    """Mock implementation that returns static deployment data, filtered by namespace if specified"""
    deployments = _MOCK_DATA['deployments']
    
    if namespace is not None:
        return [deployment for deployment in deployments if deployment.namespace == namespace]
    return deployments

get_deployment_summaries.__doc__ = k8s_tools.get_deployment_summaries.__doc__


def get_replicaset_summaries(namespace: Optional[str] = None,
                             deployment: Optional[str] = None) -> list[k8s_tools.ReplicaSetSummary]:
    """Mock implementation returning static replica set data, filtered by namespace and deployment"""
    replicasets = _MOCK_DATA['replicasets']

    if namespace is not None:
        replicasets = [rs for rs in replicasets if rs.namespace == namespace]
    if deployment is not None:
        replicasets = [rs for rs in replicasets if rs.owner_deployment == deployment]
    return replicasets

get_replicaset_summaries.__doc__ = k8s_tools.get_replicaset_summaries.__doc__


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
    get_replicaset_summaries,
    get_service_summaries,
    get_configmap_summaries,
    get_configmap,
    get_statefulset_summaries,
    get_cronjob_summaries,
    get_job_summaries,
    get_logs_for_job,
    get_logs_for_cronjob,
    get_pvc_summaries,
    get_events,
]