# Copyright (c) 2025 Benedat LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
#
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Function definitions for tools to interat with kubernetes.
"""

import sys
import os
import logging
import datetime
from typing import Optional, Union, Literal, Any

from pydantic import BaseModel, Field
import yaml

from kubernetes import client, config
from kubernetes.client import V1PodSpec, ApiException
from kubernetes.client.models.v1_container_status import V1ContainerStatus

K8S:Optional[client.CoreV1Api] = None
APPS_V1_API:Optional[client.AppsV1Api] = None
BATCH_V1_API:Optional[client.BatchV1Api] = None

class K8sConfigError(Exception):
    """This is thrown when atempting to load the config or initializing the API fails."""
    pass

class K8sApiError(Exception):
    """This is thrown when one of the kubernetes calls (other than initial API load) fails."""
    pass

def _get_api_client() -> client.CoreV1Api:
    try:
        config.load_kube_config()
        return client.CoreV1Api()
    except config.ConfigException:
        logging.warning("Could not load kube config. Ensure you have a valid Kubernetes configuration.")
        logging.warning("Attempting to load in-cluster config...")
        try:
            config.load_incluster_config()
            return client.CoreV1Api()
        except config.ConfigException as e:
            raise K8sConfigError("Could not load in-cluster config. No Kubernetes config found.") from e
        except Exception as e:
            raise K8sConfigError(f"Unexpected error: {e}") from e


def _get_apps_v1_api_client() -> client.AppsV1Api:
    try:
        config.load_kube_config()
        return client.AppsV1Api()
    except config.ConfigException:
        logging.warning("Could not load kube config. Ensure you have a valid Kubernetes configuration.")
        logging.warning("Attempting to load in-cluster config...")
        try:
            config.load_incluster_config()
            return client.AppsV1Api()
        except config.ConfigException as e:
            raise K8sConfigError("Could not load in-cluster config. No Kubernetes config found.") from e
        except Exception as e:
            raise K8sConfigError(f"Unexpected error: {e}") from e


def _get_batch_v1_api_client() -> client.BatchV1Api:
    try:
        config.load_kube_config()
        return client.BatchV1Api()
    except config.ConfigException:
        logging.warning("Could not load kube config. Ensure you have a valid Kubernetes configuration.")
        logging.warning("Attempting to load in-cluster config...")
        try:
            config.load_incluster_config()
            return client.BatchV1Api()
        except config.ConfigException as e:
            raise K8sConfigError("Could not load in-cluster config. No Kubernetes config found.") from e
        except Exception as e:
            raise K8sConfigError(f"Unexpected error: {e}") from e



class NamespaceSummary(BaseModel):
    """Summary information about a namespace, like returned by `kubectl get namespace`"""
    name: str
    status: str
    age: datetime.timedelta


def get_namespaces() -> list[NamespaceSummary]:
    """Return a summary of the namespaces for this Kubernetes cluster, similar to that
    returned by `kubectl get namespace`.

    Parameters
    ----------
    None
        This function does not take any parameters.

    Returns
    -------
    list of NamespaceSummary
        List of namespace summary objects. Each NamespaceSummary has the following fields:

        name : str
            Name of the namespace.
        status : str
            Status phase of the namespace.
        age : datetime.timedelta
            Age of the namespace (current time minus creation timestamp).
    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call to list namespaces fails.
    """
    global K8S
    if K8S is None:
        K8S = _get_api_client()
    logging.info(f"get_namespaces()")
    namespaces = K8S.list_namespace().items
    now = datetime.datetime.now(datetime.timezone.utc)
    return [
        NamespaceSummary(name=namespace.metadata.name,
                        status=namespace.status.phase,
                        age=now-namespace.metadata.creation_timestamp)
        for namespace in namespaces
    ]


class NodeSummary(BaseModel):
    """A summary of a node's status like returned by `kubectl get nodes -o wide`,
    augmented with the capacity/allocatable/conditions/taints/labels detail that
    `kubectl describe node` shows (useful for reconstructing a pods-per-node
    capacity model)."""
    name: str
    status: str
    roles: list[str]
    age: datetime.timedelta
    version: str
    internal_ip: Optional[str] = None
    external_ip: Optional[str] = None
    os_image: Optional[str] = None
    kernel_version: Optional[str] = None
    container_runtime: Optional[str] = None
    capacity: dict[str, str] = Field(default_factory=dict)
    allocatable: dict[str, str] = Field(default_factory=dict)
    conditions: dict[str, str] = Field(default_factory=dict)
    taints: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)

def get_node_summaries() -> list[NodeSummary]:
    """Return a summary of the nodes for this Kubernetes cluster, similar to that
    returned by `kubectl get nodes -o wide`.

    Parameters
    ----------
    None
        This function does not take any parameters.

    Returns
    -------
    list of NodeSummary
        List of node summary objects. Each NodeSummary has the following fields:

        name : str
            Name of the node.
        status : str
            Status of the node (Ready, NotReady, etc.).
        roles : list[str]
            List of roles for the node (e.g., ['control-plane', 'master']).
        age : datetime.timedelta
            Age of the node (current time minus creation timestamp).
        version : str
            Kubernetes version running on the node.
        internal_ip : Optional[str]
            Internal IP address of the node.
        external_ip : Optional[str]
            External IP address of the node (if available).
        os_image : Optional[str]
            Operating system image running on the node.
        kernel_version : Optional[str]
            Kernel version of the node.
        container_runtime : Optional[str]
            Container runtime version on the node.
        capacity : dict[str, str]
            Total capacity of the node keyed by resource name (e.g. "cpu",
            "memory", "ephemeral-storage", "pods"). Empty if unavailable.
        allocatable : dict[str, str]
            Resources allocatable to pods (capacity minus system-reserved),
            keyed by resource name. Empty if unavailable.
        conditions : dict[str, str]
            Node conditions keyed by type with their status, e.g.
            {"Ready": "True", "MemoryPressure": "False", "DiskPressure": "False"}.
        taints : list[str]
            Taints on the node formatted as "key=value:effect" (value omitted
            when empty).
        labels : dict[str, str]
            All labels on the node. Useful for identifying node pool / instance
            type and spotting version skew across pools.

    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call to list nodes fails.
    """
    global K8S
    if K8S is None:
        K8S = _get_api_client()
    logging.info(f"get_node_summaries()")
    
    try:
        nodes = K8S.list_node().items
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching nodes: {e}") from e
    
    current_time_utc = datetime.datetime.now(datetime.timezone.utc)
    node_summaries: list[NodeSummary] = []
    
    for node in nodes:
        node_name = node.metadata.name
        
        # Determine node status
        status = "Unknown"
        if node.status and node.status.conditions:
            for condition in node.status.conditions:
                if condition.type == "Ready":
                    status = "Ready" if condition.status == "True" else "NotReady"
                    break
        
        # Extract roles from labels
        roles = []
        if node.metadata.labels:
            for label_key in node.metadata.labels:
                if label_key.startswith("node-role.kubernetes.io/"):
                    role = label_key.replace("node-role.kubernetes.io/", "")
                    if role:  # Skip empty roles
                        roles.append(role)
                # Also check for older master label
                elif label_key == "kubernetes.io/role" and node.metadata.labels[label_key]:
                    roles.append(node.metadata.labels[label_key])
        
        if not roles:
            roles = ["<none>"]
        
        # Calculate age
        age = datetime.timedelta(0)
        if node.metadata.creation_timestamp:
            age = current_time_utc - node.metadata.creation_timestamp
        
        # Extract version and system info
        version = node.status.node_info.kubelet_version if node.status and node.status.node_info else "Unknown"
        os_image = node.status.node_info.os_image if node.status and node.status.node_info else None
        kernel_version = node.status.node_info.kernel_version if node.status and node.status.node_info else None
        container_runtime = node.status.node_info.container_runtime_version if node.status and node.status.node_info else None
        
        # Extract IP addresses
        internal_ip = None
        external_ip = None
        if node.status and node.status.addresses:
            for address in node.status.addresses:
                if address.type == "InternalIP":
                    internal_ip = address.address
                elif address.type == "ExternalIP":
                    external_ip = address.address

        # Extract capacity / allocatable / conditions (all live on node.status)
        capacity = dict(node.status.capacity) \
            if node.status and getattr(node.status, "capacity", None) else {}
        allocatable = dict(node.status.allocatable) \
            if node.status and getattr(node.status, "allocatable", None) else {}
        conditions = {c.type: c.status for c in node.status.conditions} \
            if node.status and node.status.conditions else {}

        # Extract taints (on node.spec) and labels (on node.metadata)
        taints: list[str] = []
        if getattr(node, "spec", None) and getattr(node.spec, "taints", None):
            for taint in node.spec.taints:
                value = f"={taint.value}" if getattr(taint, "value", None) else "="
                taints.append(f"{taint.key}{value}:{taint.effect}")
        labels = dict(node.metadata.labels) if node.metadata.labels else {}

        node_summary = NodeSummary(
            name=node_name,
            status=status,
            roles=roles,
            age=age,
            version=version,
            internal_ip=internal_ip,
            external_ip=external_ip,
            os_image=os_image,
            kernel_version=kernel_version,
            container_runtime=container_runtime,
            capacity=capacity,
            allocatable=allocatable,
            conditions=conditions,
            taints=taints,
            labels=labels,
        )
        node_summaries.append(node_summary)
    
    return node_summaries

def print_node_summaries() -> None:
    """
    Calls get_node_summaries and prints the output to stdout, using
    the same format as `kubectl get nodes -o wide`.
    """
    nodes = get_node_summaries()
    print(f"{'NAME':<32} {'STATUS':<12} {'ROLES':<20} {'AGE':<12} {'VERSION':<16} {'INTERNAL-IP':<16} {'EXTERNAL-IP':<16} {'OS-IMAGE':<32} {'KERNEL-VERSION':<16} {'CONTAINER-RUNTIME':<20}")
    for node in nodes:
        age = _format_timedelta(node.age)
        roles_str = ",".join(node.roles) if node.roles and node.roles != ["<none>"] else "<none>"
        internal_ip = node.internal_ip if node.internal_ip else "<none>"
        external_ip = node.external_ip if node.external_ip else "<none>"
        os_image = node.os_image if node.os_image else "<unknown>"
        kernel_version = node.kernel_version if node.kernel_version else "<unknown>"
        container_runtime = node.container_runtime if node.container_runtime else "<unknown>"
        
        print(f"{node.name:<32} {node.status:<12} {roles_str:<20} {age:<12} {node.version:<16} {internal_ip:<16} {external_ip:<16} {os_image:<32} {kernel_version:<16} {container_runtime:<20}")
    

def print_namespaces() -> None:
    """
    Calls get_namespaces and prints the output to stdout, using
    the same format as `kubectl get namespace`.
    """
    namespaces = get_namespaces()
    print(f"{'NAME':<32} {'STATUS':<12} {'AGE':<12}")
    for ns in namespaces:
        age = _format_timedelta(ns.age)
        print(f"{ns.name:<32} {ns.status:<12} {age:<12}")


class PodSummary(BaseModel):
    """A summary of a pod's status like returned by `kubectl get pods -o wide`"""
    name: str
    namespace: str
    total_containers: int
    ready_containers: int
    restarts: int
    last_restart: Optional[datetime.timedelta]
    age: datetime.timedelta
    ip: Optional[str] = None
    node: Optional[str] = None

   

def get_pod_summaries(namespace: Optional[str] = None) -> list[PodSummary]:
    """
    Retrieves a list of PodSummary objects for pods in a given namespace or all namespaces.

    Parameters
    ----------
    namespace : Optional[str], default=None
        The specific namespace to list pods from. If None, lists pods from all namespaces.

    Returns
    -------
    list of PodSummary
        A list of PodSummary objects, each providing a summary of a pod's status with the following fields:

        name : str
            Name of the pod.
        namespace : str
            Namespace in which the pod is running.
        total_containers : int
            Total number of containers in the pod.
        ready_containers : int
            Number of containers currently in ready state.
        restarts : int
            Total number of restarts for all containers in the pod.
        last_restart : Optional[datetime.timedelta]
            Time since the container last restart (None if never restarted).
        age : datetime.timedelta
            Age of the pod (current time minus creation timestamp).
        ip : Optional[str]
            Pod IP address (None if not assigned).
        node : Optional[str]
            Name of the node where the pod is running (None if not scheduled).
    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call to list pods fails.
    """
    global K8S
    
    # Load Kubernetes configuration and initialize client only once
    if K8S is None:
        K8S = _get_api_client()

    logging.info(f"get_pod_summaries(namespace={namespace})")
    pod_summaries: list[PodSummary] = []
    
    try:
        if namespace:
            # List pods in a specific namespace
            pods = K8S.list_namespaced_pod(namespace=namespace).items
        else:
            # List pods across all namespaces
            pods = K8S.list_pod_for_all_namespaces().items
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching pods: {e}") from e

    current_time_utc = datetime.datetime.now(datetime.timezone.utc)

    for pod in pods:
        pod_name = pod.metadata.name
        pod_namespace = pod.metadata.namespace
        
        total_containers = len(pod.spec.containers)
        ready_containers = 0
        total_restarts = 0
        latest_restart_time: Optional[datetime.datetime] = None

        if pod.status and pod.status.container_statuses:
            for container_status in pod.status.container_statuses:
                if container_status.ready:
                    ready_containers += 1
                
                total_restarts += container_status.restart_count
                
                # Check for last restart time
                if container_status.last_state and container_status.last_state.terminated:
                    terminated_at = container_status.last_state.terminated.finished_at
                    if terminated_at:
                        if latest_restart_time is None or terminated_at > latest_restart_time:
                            latest_restart_time = terminated_at

        # Calculate age
        age = datetime.timedelta(0) # Default to 0 if creation_timestamp is missing
        if pod.metadata.creation_timestamp:
            age = current_time_utc - pod.metadata.creation_timestamp

        # Calculate last_restart timedelta if a latest_restart_time was found
        last_restart_timedelta: Optional[datetime.timedelta] = None
        if latest_restart_time:
            last_restart_timedelta = current_time_utc - latest_restart_time

        # Extract IP and node information
        pod_ip = pod.status.pod_ip if pod.status and pod.status.pod_ip else None
        node_name = pod.spec.node_name if pod.spec and pod.spec.node_name else None

        pod_summary = PodSummary(
            name=pod_name,
            namespace=pod_namespace,
            total_containers=total_containers,
            ready_containers=ready_containers,
            restarts=total_restarts,
            last_restart=last_restart_timedelta,
            age=age,
            ip=pod_ip,
            node=node_name
        )
        pod_summaries.append(pod_summary)
    
    return pod_summaries

def print_pod_summaries(namespace: Optional[str] = None) -> None:
    """
    Calls get_pod_summaries and prints the output to stdout, using
    the same format as `kubectl get pods -o wide`.
    """
    pod_summaries = get_pod_summaries(namespace)
    # Print header
    print(f"{'NAME':<32} {'NAMESPACE':<20} {'READY':<10} {'RESTARTS':<10} {'AGE':<12} {'IP':<16} {'NODE':<24}")
    for pod in pod_summaries:
        ready = f"{pod.ready_containers}/{pod.total_containers}"
        restarts = str(pod.restarts)
        age = _format_timedelta(pod.age)
        ip = pod.ip if pod.ip else "<none>"
        node = pod.node if pod.node else "<none>"
        print(f"{pod.name:<32} {pod.namespace:<20} {ready:<10} {restarts:<10} {age:<12} {ip:<16} {node:<24}")

def _format_timedelta(td: Optional[datetime.timedelta]) -> str:
    if td is None:
        return "-"
    total_seconds = int(td.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days > 0:
        return f"{days}d{hours}h"
    elif hours > 0:
        return f"{hours}h{minutes}m"
    elif minutes > 0:
        return f"{minutes}m{seconds}s"
    else:
        return f"{seconds}s"


class EventSummary(BaseModel):
    """This is the representation of a Kubernetes Event"""
    last_seen: Optional[datetime.timedelta]  # Time since event occurred
    type: str
    reason: str
    object: str
    message: str
 

def get_pod_events(pod_name: str, namespace: str = "default") -> list[EventSummary]:
    """
    Get events for a specific Kubernetes pod. This is equivalent to the kubectl command:
    `kubectl get events -n NAMESPACE --field-selector involvedObject.name=POD_NAME,involvedObject.kind=Pod`

    Parameters
    ----------
    pod_name : str
        Name of the pod to retrieve events for.
    namespace : str, optional
        Namespace of the pod (default is "default").

    Returns
    -------
    list of EventSummary
        List of events associated with the specified pod. Each EventSummary has the following fields:

        last_seen : Optional[datetime.datetime]
            Timestamp of the last occurrence of the event (if available).
        type : str
            Type of the event.
        reason : str
            Reason for the event.
        object : str
            The object this event applies to.
        message : str
            Message describing the event.
    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call to list events fails.
    """
    global K8S
    if K8S is None:
        K8S = _get_api_client()
    logging.info(f"get_pod_events(pod_name={pod_name}, namespace={namespace})")
    field_selector = f"involvedObject.name={pod_name}"
    events = K8S.list_namespaced_event(namespace, field_selector=field_selector)
    now = datetime.datetime.now(datetime.timezone.utc)
    return [
        EventSummary(
            last_seen=(now - event.last_timestamp) if event.last_timestamp else None,
            type=event.type,
            reason=event.reason,
            object=getattr(event.involved_object, 'name', pod_name),
            message=event.message,
        )
        for event in events.items
    ]


def print_pod_events(pod_name: str, namespace: str = "default") -> None:
    """
    Print the events for the specified pod, in a similar format to `kubectl get events`.
    """
    events = get_pod_events(pod_name, namespace)
    print(f"{'LAST SEEN':<12} {'TYPE':<10} {'REASON':<20} {'OBJECT':<32} {'MESSAGE':<40}")
    for event in events:
        last_seen = _format_timedelta(event.last_seen) if event.last_seen else "-"
        message = (event.message[:37] + '...') if event.message and len(event.message) > 40 else event.message
        print(f"{last_seen:<12} {event.type:<10} {event.reason:<20} {event.object:<32} {message:<40}")

# see kubernetes.client.models.v1_container_state_running.V1ContainerStateRunning
class ContainerStateRunning(BaseModel):
    state_name: Literal['Running'] = 'Running'
    started_at: datetime.datetime

# see kubernetes.client.models.v1_container_state_waiting.V1ContainerStateWaiting
class ContainerStateWaiting(BaseModel):
    state_name: Literal['Waiting'] = 'Waiting'
    reason: str
    message: Optional[str] = None

# see kubernetes.client.models.v1_container_state_terminated.V1ContainerStateTerminated
class ContainerStateTerminated(BaseModel):
    state_name: Literal['Terminated'] = 'Terminated'
    exit_code: Optional[int] = None
    finished_at: Optional[datetime.datetime] = None
    reason: Optional[str] = None
    message: Optional[str] = None
    started_at: Optional[datetime.datetime] = None

# see kubernetes.client.models.v1_container_state.V1ContainerState
ContainerState = Union[ContainerStateRunning, ContainerStateWaiting, ContainerStateTerminated]

def _v1_container_state_to_container_state(container_state:client.V1ContainerState) -> Optional[ContainerState]:
    if container_state.running:
        return ContainerStateRunning(started_at=container_state.running.started_at)
    elif container_state.waiting:
        return ContainerStateWaiting(reason=container_state.waiting.reason,
                                     message=container_state.waiting.message)
    elif container_state.terminated:
        cst = container_state.terminated
        return ContainerStateTerminated(exit_code=cst.exit_code,
                                        reason=cst.reason,
                                        finished_at=cst.finished_at,
                                        message=cst.message,
                                        started_at=cst.started_at)
    else:
        # All states are None - this is valid (e.g., for last_state when container has never been in a previous state)
        return None
    
# see https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/V1VolumeMountStatus.md
class VolumeMountStatus(BaseModel):
    mount_path: str
    name: str
    read_only: Optional[bool]
    recursive_read_only: Optional[str]

def _v1_volume_mount_status_to_mount_statis(mount_status:client.V1VolumeMountStatus) -> VolumeMountStatus:
    return VolumeMountStatus(mount_path=mount_status.mount_path,
                             name=mount_status.name,
                             read_only=mount_status.read_only,
                             recursive_read_only=mount_status.recursive_read_only)

# and https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/V1ContainerStatus.md
class ContainerStatus(BaseModel):
    """Provides information about a container running in a specific pod. This corresponds to
    kubernetes.client.models.v1_container_tatus.V1ContainerStatus.
    """
    pod_name: str
    namespace: str
    container_name: str
    image: str
    ready: bool
    restart_count: int
    started: Optional[bool]
    stop_signal: Optional[str]
    state: Optional[ContainerState]
    last_state: Optional[ContainerState]
    volume_mounts: list[VolumeMountStatus]
    resource_requests: dict[str, str]
    resource_limits: dict[str, str]
    allocated_resources: dict[str, str]

    
def get_pod_container_statuses(pod_name: str, namespace: str = "default") -> list[ContainerStatus]:
    """
    Get the status for all containers in a specified Kubernetes pod.

    Parameters
    ----------
    pod_name : str
        Name of the pod to retrieve container statuses for.
    namespace : str, optional
        Namespace of the pod (default is "default").

    Returns
    -------
    list of ContainerStatus
        List of container status objects for the specified pod. Each ContainerStatus has the following fields:

        pod_name : str
            Name of the pod.
        namespace : str
            Namespace of the pod.
        container_name : str
            Name of the container.
        image : str
            Image name.
        ready : bool
            Whether the container is currently passing its readiness check.
            The value will change as readiness probes keep executing.
        restart_count : int
            Number of times the container has restarted.
        started : Optional[bool]
            Started indicates whether the container has finished its postStart
            lifecycle hook and passed its startup probe.
        stop_signal : Optional[str]
            Stop signal for the container.
        state : Optional[ContainerState]
            Current state of the container.
        last_state : Optional[ContainerState]
            Last state of the container.
        volume_mounts : list[VolumeMountStatus]
            Status of volume mounts for the container
        resource_requests : dict[str, str]
            Describes the minimum amount of compute resources required. If Requests
            is omitted for a container, it defaults to Limits if that is explicitly specified,
            otherwise to an implementation-defined value. Requests cannot exceed Limits. 
        resource_limits : dict[str, str]
            Describes the maximum amount of compute resources allowed.
        allocated_resources : dict[str, str]
            Compute resources allocated for this container by the node.

    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call to read the pod fails.
    """   
    global K8S
    if K8S is None:
        K8S = _get_api_client()
    logging.info(f"get_pod_container_statuses(pod_name={pod_name}, namespace={namespace})")
    pod = K8S.read_namespaced_pod(name=pod_name, namespace=namespace)
    # Only proceed if pod is a V1Pod instance
    if not isinstance(pod, client.V1Pod):
        raise K8sApiError(f"Unexpected type for pod: {type(pod)}")
    result:list[ContainerStatus] = []
    if not pod.status or not pod.status.container_statuses:
        return result
    for container_status in pod.status.container_statuses:
        container_name = container_status.name
        image = container_status.image
        ready = container_status.ready
        restart_count = container_status.restart_count
        started = container_status.started
        stop_signal = container_status.stop_signal
        state = _v1_container_state_to_container_state(container_status.state) \
                if container_status.state is not None else None
        last_state = _v1_container_state_to_container_state(container_status.last_state) \
                if container_status.last_state is not None else None
        volume_mounts = [_v1_volume_mount_status_to_mount_statis(volume_mount)
                         for volume_mount in container_status.volume_mounts] \
                if container_status.volume_mounts is not None else []
        if container_status.resources:
            resource_requests = container_status.resources.requests \
                                if container_status.resources.requests is not None else {}
            resource_limits = container_status.resources.limits \
                              if container_status.resources.limits is not None else {}
        else:
            resource_requests = {}
            resource_limits = {}
        allocated_resources = container_status.allocated_resources \
                              if container_status.allocated_resources is not None else {}

        result.append(ContainerStatus(
            pod_name=pod_name,
            namespace=namespace,
            container_name=container_name,
            image=image,
            ready=ready,
            restart_count=restart_count,
            started=started,
            stop_signal=stop_signal,
            state=state,
            last_state=last_state,
            volume_mounts=volume_mounts,
            resource_requests=resource_requests,
            resource_limits=resource_limits,
            allocated_resources=allocated_resources
        ))
    return result


def print_pod_container_statuses(pod_name: str, namespace: str = "default") -> None:
    """
    Pretty-print the status for all containers in a specified Kubernetes pod. 
    """
    containers = get_pod_container_statuses(pod_name, namespace)
    print(f"{'NAME':<24} {'READY':<8} {'RESTARTS':<9} {'STATE':<12} {'REASON':<20} {'STARTED':<30} {'FINISHED':<30} {'MEMORY':<12}")
    for cs in containers:
        name = cs.container_name
        ready = str(cs.ready)
        restarts = str(cs.restart_count)
        state = "-"
        reason = "-"
        started = "-"
        finished = "-"
        memory = cs.allocated_resources.get('memory', '-')
        if cs.state:
            if isinstance(cs.state, ContainerStateRunning):
                state = "Running"
                started = cs.state.started_at.isoformat() if cs.state.started_at else "-"
            elif isinstance(cs.state, ContainerStateWaiting):
                state = "Waiting"
                reason = cs.state.reason if cs.state.reason else "-"
            elif isinstance(cs.state, ContainerStateTerminated):
                state = "Terminated"
                reason = cs.state.reason if cs.state.reason else "-"
                started = cs.state.started_at.isoformat() if cs.state.started_at else "-"
                finished = cs.state.finished_at.isoformat() if cs.state.finished_at else "-"
        print(f"{name:<24} {ready:<8} {restarts:<9} {state:<12} {reason:<20} {started:<30} {finished:<30} {memory:<12}")


def get_pod_spec(pod_name: str, namespace: str = "default") -> dict[str,Any]:
    """
    Retrieves the spec for a given pod in a specific namespace.

    Args:
        pod_name (str): The name of the pod.
        namespace (str): The namespace the pod belongs to (defaults to "default").

    Returns
    -------
    dict[str, Any]
        The pod's spec object, containing its desired state. It is converted
        from a V1PodSpec to a dictionary. Key fields include:

        containers : list of kubernetes.client.V1Container
            List of containers belonging to the pod. Each container defines its image,
            ports, environment variables, resource requests/limits, etc.
        init_containers : list of kubernetes.client.V1Container, optional
            List of initialization containers belonging to the pod.
        volumes : list of kubernetes.client.V1Volume, optional
            List of volumes mounted in the pod and the sources available for
            the containers.
        node_selector : dict, optional
            A selector which must be true for the pod to fit on a node.
            Keys and values are strings.
        restart_policy : str
            Restart policy for all containers within the pod.
            Common values are "Always", "OnFailure", "Never".
        service_account_name : str, optional
            Service account name in the namespace that the pod will use to
            access the Kubernetes API.
        dns_policy : str
            DNS policy for the pod. Common values are "ClusterFirst", "Default".
        priority_class_name : str, optional
            If specified, indicates the pod's priority_class via its name.
        node_name : str, optional
            NodeName is a request to schedule this pod onto a specific node.

    Raises
    ------
    K8SConfigError
        If unable to initialize the K8S API
    K8sApiError
        If the pod is not found, configuration fails, or any other API error occurs.
    """
    global K8S
    if K8S is None:
        K8S = _get_api_client()
        logging.info(f"get_pod_spec(pod_name={pod_name}, namespace={namespace})")
    try:
        # Get the pod object
        pod = K8S.read_namespaced_pod(name=pod_name, namespace=namespace)
        # Ensure pod is a V1Pod instance and has a spec
        if not isinstance(pod, client.V1Pod) or not hasattr(pod, "spec") or pod.spec is None:
            raise K8sApiError(f"Pod '{pod_name}' in namespace '{namespace}' did not return a valid spec.")
        return pod.spec.to_dict()
    except ApiException as e:
        if hasattr(e, "status") and e.status == 404:
            raise K8sApiError(
                f"Pod '{pod_name}' not found in namespace '{namespace}'."
            ) from e
        else:
            raise K8sApiError(
                f"Error getting pod '{pod_name}' in namespace '{namespace}': {e}"
            ) from e
    except Exception as e:
        raise K8sApiError(f"Unexpected error getting pod spec: {e}") from e

def print_pod_spec(pod_name: str, namespace: str = "default") -> None:
    """Pretty prints the spec for the specified pod as valid YAML."""
    try:
        spec_dict = get_pod_spec(pod_name, namespace)
        print(yaml.safe_dump(spec_dict, default_flow_style=False, sort_keys=False))
    except Exception as e:
        print(f"Error printing pod spec: {e}")


def get_logs_for_pod_and_container(pod_name:str, namespace:str = "default",
                                    container_name:Optional[str]=None,
                                    tail:Optional[int]=None,
                                    since_seconds:Optional[int]=None,
                                    previous:bool=False) -> Optional[str]:
    """
    Retrieves logs from a Kubernetes pod and container.

    Args:
        pod_name (str): The name of the pod.
        namespace (str): The namespace of the pod.
        container_name (str, optional): The name of the container within the pod.
                                        If None, defaults to the first container.
        tail (int, optional): Number of lines to return from the end of the log.
                              If None, defaults to the last 1000 lines. Pass a
                              larger value to retrieve more history, or a small
                              value for a bounded recent slice.
        since_seconds (int, optional): If set, only return logs newer than this
                              many seconds. Combines with `tail` (both limits
                              apply).
        previous (bool, default False): If True, return logs from the *previous*
                              terminated instance of the container instead of the
                              current one. Indispensable for crashloop analysis,
                              where the current instance's logs are empty or
                              post-restart. Fails if there is no previous instance.

    Returns:
        str, optional: Log content if any found for this pod/container, or None otherwise

    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call to fetch logs fails or an unexpected error occurs.
    """
    global K8S
    if K8S is None:
        K8S = _get_api_client()

    try:
        # read_namespaced_pod_log with reasonable limits to avoid memory issues.
        # Optional args are only passed when set so the common path stays simple.
        log_kwargs: dict[str, Any] = dict(
            name=pod_name,
            namespace=namespace,
            container=container_name,  # Pass container_name if specified
            follow=False,              # Set to False to get all current logs
            _preload_content=True,     # Important: This loads all content into memory
            timestamps=True,           # Optional: Include timestamps
            tail_lines=tail if tail is not None else 1000,  # Default: last 1000 lines
            limit_bytes=1024*1024,     # Limit to 1MB to avoid memory issues
        )
        if since_seconds is not None:
            log_kwargs["since_seconds"] = since_seconds
        if previous:
            log_kwargs["previous"] = True
        resp = K8S.read_namespaced_pod_log(**log_kwargs)

        # The response is a single string containing all logs
        if resp:
            return resp
        else:
            return ''
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching logs: {e}") from e
    except Exception as e:
        raise K8sApiError(f"An unexpected error occurred: {e}") from e


class DeploymentSummary(BaseModel):
    """A summary of a deployment's status like returned by `kubectl get deployments`"""
    name: str
    namespace: str
    total_replicas: int
    ready_replicas: int
    up_to_date_relicas: int
    available_replicas: int
    age: datetime.timedelta

def get_deployment_summaries(namespace: Optional[str] = None) -> list[DeploymentSummary]:
    """
    Retrieves a list of DeploymentSummary objects for deployments in a given namespace or all namespaces.
    Similar to `kubectl get deployements`.

    Parameters
    ----------
    namespace : Optional[str], default=None
        The specific namespace to list deployments from. If None, lists deployments from all namespaces.

    Returns
    -------
    list of DeploymentSummary
        A list of DeploymentSummary objects, each providing a summary of a deployment's status with the following fields:

        name : str
            Name of the deployment.
        namespace : str
            Namespace in which the deployment is running.
        total_replicas : int
            Total number of replicas desired for this deployment.
        ready_replicas : int
            Number of replicas that are currently ready.
        up_to_date_replicas : int
            Number of replicas that are up to date.
        available_replicas : int
            Number of replicas that are available.
        age : datetime.timedelta
            Age of the deployment (current time minus creation timestamp).

    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call to list deployments fails.
    """
    global APPS_V1_API
    
    # Load Kubernetes configuration and initialize client only once
    if APPS_V1_API is None:
        APPS_V1_API = _get_apps_v1_api_client()

    logging.info(f"get_deployment_summaries(namespace={namespace})")
    deployment_summaries: list[DeploymentSummary] = []
    
    try:
        if namespace:
            deployments = APPS_V1_API.list_namespaced_deployment(namespace=namespace)
        else:
            deployments = APPS_V1_API.list_deployment_for_all_namespaces()
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching deployments: {e}") from e
    
    current_time_utc = datetime.datetime.now(datetime.timezone.utc)
    
    for deployment in deployments.items:
        deployment_name = deployment.metadata.name
        deployment_namespace = deployment.metadata.namespace
        
        # Extract replica counts from deployment status
        total_replicas = deployment.spec.replicas if deployment.spec.replicas is not None else 0
        ready_replicas = deployment.status.ready_replicas if deployment.status.ready_replicas is not None else 0
        up_to_date_replicas = deployment.status.updated_replicas if deployment.status.updated_replicas is not None else 0
        available_replicas = deployment.status.available_replicas if deployment.status.available_replicas is not None else 0
        
        # Calculate age
        age = datetime.timedelta(0)  # Default to 0 if creation_timestamp is missing
        if deployment.metadata.creation_timestamp:
            age = current_time_utc - deployment.metadata.creation_timestamp
        
        deployment_summary = DeploymentSummary(
            name=deployment_name,
            namespace=deployment_namespace,
            total_replicas=total_replicas,
            ready_replicas=ready_replicas,
            up_to_date_relicas=up_to_date_replicas,
            available_replicas=available_replicas,
            age=age
        )
        deployment_summaries.append(deployment_summary)
    
    return deployment_summaries


class ReplicaSetSummary(BaseModel):
    """A summary of a replica set, like `kubectl get replicasets` with revision info.

    A Deployment's replica sets are its change history: each update creates a new
    replica set holding that revision's pod template. Comparing the images and
    revisions across a deployment's replica sets answers "what changed, and when".
    """
    name: str
    namespace: str
    owner_deployment: Optional[str]
    revision: Optional[int]
    desired_replicas: int
    current_replicas: int
    ready_replicas: int
    images: list[str]
    age: datetime.timedelta


def get_replicaset_summaries(namespace: Optional[str] = None,
                             deployment: Optional[str] = None) -> list[ReplicaSetSummary]:
    """
    Retrieves a list of ReplicaSetSummary objects, similar to `kubectl get replicasets`
    but including each replica set's deployment revision and container images.

    A Deployment's replica sets are its revision history: every update to a Deployment
    creates a new replica set carrying that revision's pod template, and older replica
    sets are retained (scaled to zero). Listing them for one deployment therefore shows
    when it last changed and what its image was at each revision - which is how you
    answer "did something change recently?" without access to deployment tooling or
    version control.

    Results are grouped by namespace and owning deployment, and within each
    deployment sorted by revision, oldest first. So when filtered to a single
    deployment, the last entry is that deployment's current revision.

    Parameters
    ----------
    namespace : Optional[str], default=None
        The specific namespace to list replica sets from. If None, lists from all namespaces.
    deployment : Optional[str], default=None
        If given, return only replica sets owned by this deployment. This is the common
        case: one deployment's revision history.

    Returns
    -------
    list of ReplicaSetSummary
        A list of ReplicaSetSummary objects with the following fields:

        name : str
            Name of the replica set.
        namespace : str
            Namespace in which the replica set is defined.
        owner_deployment : Optional[str]
            Name of the Deployment that owns this replica set, or None if it is
            standalone (not managed by a Deployment).
        revision : Optional[int]
            The deployment revision this replica set represents, taken from the
            `deployment.kubernetes.io/revision` annotation. None if not set.
        desired_replicas : int
            Replicas desired for this replica set. Old revisions are scaled to 0.
        current_replicas : int
            Replicas currently running.
        ready_replicas : int
            Replicas currently ready.
        images : list[str]
            Container images in this revision's pod template, in container order.
            Comparing this across revisions shows what an upgrade changed.
        age : datetime.timedelta
            Age of the replica set (current time minus creation timestamp). For the
            newest revision this is how long ago the deployment last changed.

    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call to list replica sets fails.
    """
    global APPS_V1_API

    if APPS_V1_API is None:
        APPS_V1_API = _get_apps_v1_api_client()

    logging.info(f"get_replicaset_summaries(namespace={namespace}, deployment={deployment})")
    summaries: list[ReplicaSetSummary] = []

    try:
        if namespace:
            replicasets = APPS_V1_API.list_namespaced_replica_set(namespace=namespace)
        else:
            replicasets = APPS_V1_API.list_replica_set_for_all_namespaces()
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching replica sets: {e}") from e

    current_time_utc = datetime.datetime.now(datetime.timezone.utc)

    for rs in replicasets.items:
        owner = None
        for ref in (rs.metadata.owner_references or []):
            if ref.kind == "Deployment":
                owner = ref.name
                break
        if deployment is not None and owner != deployment:
            continue

        annotations = rs.metadata.annotations or {}
        raw_revision = annotations.get("deployment.kubernetes.io/revision")
        try:
            revision = int(raw_revision) if raw_revision is not None else None
        except ValueError:
            # A malformed annotation should not lose the replica set entirely.
            revision = None

        containers = []
        if rs.spec is not None and rs.spec.template is not None and rs.spec.template.spec is not None:
            containers = rs.spec.template.spec.containers or []

        status = rs.status
        summaries.append(ReplicaSetSummary(
            name=rs.metadata.name,
            namespace=rs.metadata.namespace,
            owner_deployment=owner,
            revision=revision,
            desired_replicas=(rs.spec.replicas if rs.spec is not None and rs.spec.replicas else 0),
            current_replicas=(status.replicas if status is not None and status.replicas else 0),
            ready_replicas=(status.ready_replicas if status is not None and status.ready_replicas else 0),
            images=[c.image for c in containers if c.image is not None],
            age=current_time_utc - rs.metadata.creation_timestamp,
        ))

    # Oldest revision first, so the last entry is the current one. Replica sets
    # without a revision sort first rather than being dropped.
    summaries.sort(key=lambda r: (r.namespace, r.owner_deployment or "",
                                  r.revision if r.revision is not None else -1))
    return summaries


def print_replicaset_summaries(namespace: Optional[str] = None,
                               deployment: Optional[str] = None) -> None:
    """
    Calls get_replicaset_summaries and prints the output to stdout, using a format
    similar to `kubectl get replicasets` with the deployment revision and image added.
    """
    summaries = get_replicaset_summaries(namespace, deployment)
    print(f"{'NAME':<40} {'NAMESPACE':<16} {'REV':<5} {'DESIRED':<8} {'CURRENT':<8} {'READY':<7} {'AGE':<10} IMAGES")
    for rs in summaries:
        revision = str(rs.revision) if rs.revision is not None else "-"
        age = _format_timedelta(rs.age)
        images = ", ".join(rs.images)
        print(f"{rs.name:<40} {rs.namespace:<16} {revision:<5} {rs.desired_replicas:<8} "
              f"{rs.current_replicas:<8} {rs.ready_replicas:<7} {age:<10} {images}")


def print_deployment_summaries(namespace: Optional[str] = None) -> None:
    """
    Calls get_deployment_summaries and prints the output to stdout, using
    the same format as `kubectl get deployments`.
    """
    deployment_summaries = get_deployment_summaries(namespace)
    print(f"{'NAME':<32} {'NAMESPACE':<20} {'READY':<10} {'UP-TO-DATE':<12} {'AVAILABLE':<12} {'AGE':<12}")
    for deployment in deployment_summaries:
        ready = f"{deployment.ready_replicas}/{deployment.total_replicas}"
        up_to_date = str(deployment.up_to_date_relicas)
        available = str(deployment.available_replicas)
        age = _format_timedelta(deployment.age)
        print(f"{deployment.name:<32} {deployment.namespace:<20} {ready:<10} {up_to_date:<12} {available:<12} {age:<12}")


class PortInfo(BaseModel):
    """A representation of a port, to be used in various specs."""
    port: int
    protocol: str

class ServiceSummary(BaseModel):
    """A summary of a service's status like returned by `kubectl get services`,
    plus the pod `selector` and the service's labels/annotations."""
    name: str
    namespace: str
    type: str
    cluster_ip: Optional[str] = None
    external_ip: Optional[str] = None
    ports: list[PortInfo]
    age: datetime.timedelta
    selector: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)

def get_service_summaries(namespace: Optional[str] = None) -> list[ServiceSummary]:
    """Retrieves a list of ServiceSummary objects for services in a given namespace or all namespaces.
    Similar to `kubectl get services`.

    Parameters
    ----------
    namespace : Optional[str], default=None
        The specific namespace to list services from. If None, lists services from all namespaces.

    Returns
    -------
    list of ServiceSummary
        A list of ServiceSummary objects, each providing a summary of a service's status with the following fields:

        name : str
            Name of the service.
        namespace : str
            Namespace in which the service is running.
        type : str
            Type of the service (ClusterIP, NodePort, LoadBalancer, ExternalName).
        cluster_ip : Optional[str]
            Cluster IP address assigned to the service (None for ExternalName services).
        external_ip : Optional[str]
            External IP address if applicable (for LoadBalancer services).
        ports : list[PortInfo]
            List of ports (and their protocols) exposed by the service.
        age : datetime.timedelta
            Age of the service (current time minus creation timestamp).
        selector : dict[str, str]
            The label selector the service uses to choose backing pods. Empty
            for services without a selector (e.g. ExternalName, or manually
            managed Endpoints).
        labels : dict[str, str]
            Labels on the service object.
        annotations : dict[str, str]
            Annotations on the service object.

    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call to list services fails.
    """
    global K8S
    
    # Load Kubernetes configuration and initialize client only once
    if K8S is None:
        K8S = _get_api_client()

    logging.info(f"get_service_summaries(namespace={namespace})")
    service_summaries: list[ServiceSummary] = []
    
    try:
        if namespace:
            # List services in a specific namespace
            services = K8S.list_namespaced_service(namespace=namespace).items
        else:
            # List services across all namespaces
            services = K8S.list_service_for_all_namespaces().items
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching services: {e}") from e

    current_time_utc = datetime.datetime.now(datetime.timezone.utc)

    for service in services:
        service_name = service.metadata.name
        service_namespace = service.metadata.namespace
        service_type = service.spec.type if service.spec.type else "ClusterIP"
        
        # Get cluster IP (None for ExternalName services)
        cluster_ip = service.spec.cluster_ip if service.spec.cluster_ip != "None" else None
        
        # Get external IP for LoadBalancer services
        external_ip = None
        if service.status and service.status.load_balancer and service.status.load_balancer.ingress:
            # Take the first ingress IP or hostname
            ingress = service.status.load_balancer.ingress[0]
            external_ip = ingress.ip or ingress.hostname
        
        # Extract port information
        ports = []
        if service.spec.ports:
            for port in service.spec.ports:
                ports.append(PortInfo(
                    port=port.port,
                    protocol=port.protocol if port.protocol else "TCP"
                ))
        
        # Calculate age
        age = datetime.timedelta(0)  # Default to 0 if creation_timestamp is missing
        if service.metadata.creation_timestamp:
            age = current_time_utc - service.metadata.creation_timestamp

        # Selector (spec) and labels/annotations (metadata)
        selector = dict(service.spec.selector) \
            if getattr(service.spec, "selector", None) else {}
        labels = dict(service.metadata.labels) \
            if getattr(service.metadata, "labels", None) else {}
        annotations = dict(service.metadata.annotations) \
            if getattr(service.metadata, "annotations", None) else {}

        service_summary = ServiceSummary(
            name=service_name,
            namespace=service_namespace,
            type=service_type,
            cluster_ip=cluster_ip,
            external_ip=external_ip,
            ports=ports,
            age=age,
            selector=selector,
            labels=labels,
            annotations=annotations,
        )
        service_summaries.append(service_summary)
    
    return service_summaries


def print_service_summaries(namespace: Optional[str] = None) -> None:
    """
    Calls get_service_summaries and prints the output to stdout, using
    the same format as `kubectl get services`.
    """
    service_summaries = get_service_summaries(namespace)
    print(f"{'NAME':<32} {'NAMESPACE':<20} {'TYPE':<15} {'CLUSTER-IP':<16} {'EXTERNAL-IP':<16} {'PORT(S)':<20} {'AGE':<12}")
    for service in service_summaries:
        service_type = service.type
        cluster_ip = service.cluster_ip if service.cluster_ip else "<none>"
        external_ip = service.external_ip if service.external_ip else "<none>"
        
        # Format ports as "port/protocol,port/protocol"
        ports_str = ",".join([f"{port.port}/{port.protocol}" for port in service.ports]) if service.ports else "<none>"
        
        age = _format_timedelta(service.age)
        print(f"{service.name:<32} {service.namespace:<20} {service_type:<15} {cluster_ip:<16} {external_ip:<16} {age:<12} {ports_str:<20}")




class ConfigMapSummary(BaseModel):
    """A summary of a ConfigMap like returned by `kubectl get configmaps`."""
    name: str
    namespace: str
    key_count: int
    data_size: int  # total bytes across all values (data + binary_data)
    age: datetime.timedelta


def _configmap_key_count_and_size(config_map) -> tuple[int, int]:
    key_count = 0
    data_size = 0
    if getattr(config_map, "data", None):
        key_count += len(config_map.data)
        data_size += sum(len(v) for v in config_map.data.values() if v is not None)
    if getattr(config_map, "binary_data", None):
        key_count += len(config_map.binary_data)
        # binary_data values are base64-encoded strings on the wire
        data_size += sum(len(v) for v in config_map.binary_data.values() if v is not None)
    return key_count, data_size


def get_configmap_summaries(namespace: Optional[str] = None) -> list[ConfigMapSummary]:
    """Retrieves a list of ConfigMapSummary objects for ConfigMaps in a given namespace
    or all namespaces, similar to `kubectl get configmaps`.

    Note that this returns only summary metadata (not the ConfigMap contents); use
    `get_configmap` to read the actual data map.

    Parameters
    ----------
    namespace : Optional[str], default=None
        The specific namespace to list ConfigMaps from. If None, lists ConfigMaps from
        all namespaces.

    Returns
    -------
    list of ConfigMapSummary
        A list of ConfigMapSummary objects, each with the following fields:

        name : str
            Name of the ConfigMap.
        namespace : str
            Namespace in which the ConfigMap lives.
        key_count : int
            Number of keys across `data` and `binary_data`.
        data_size : int
            Approximate total size in bytes of all values.
        age : datetime.timedelta
            Age of the ConfigMap (current time minus creation timestamp).

    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call to list ConfigMaps fails.
    """
    global K8S
    if K8S is None:
        K8S = _get_api_client()
    logging.info(f"get_configmap_summaries(namespace={namespace})")
    try:
        if namespace:
            config_maps = K8S.list_namespaced_config_map(namespace=namespace).items
        else:
            config_maps = K8S.list_config_map_for_all_namespaces().items
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching config maps: {e}") from e

    now = datetime.datetime.now(datetime.timezone.utc)
    summaries: list[ConfigMapSummary] = []
    for config_map in config_maps:
        key_count, data_size = _configmap_key_count_and_size(config_map)
        age = datetime.timedelta(0)
        if config_map.metadata.creation_timestamp:
            age = now - config_map.metadata.creation_timestamp
        summaries.append(ConfigMapSummary(
            name=config_map.metadata.name,
            namespace=config_map.metadata.namespace,
            key_count=key_count,
            data_size=data_size,
            age=age,
        ))
    return summaries


def print_configmap_summaries(namespace: Optional[str] = None) -> None:
    """Calls get_configmap_summaries and prints the output to stdout."""
    summaries = get_configmap_summaries(namespace)
    print(f"{'NAME':<40} {'NAMESPACE':<20} {'KEYS':<6} {'SIZE':<10} {'AGE':<12}")
    for cm in summaries:
        age = _format_timedelta(cm.age)
        print(f"{cm.name:<40} {cm.namespace:<20} {cm.key_count:<6} {cm.data_size:<10} {age:<12}")


def get_configmap(name: str, namespace: str = "default") -> dict[str, Any]:
    """Retrieves the full contents of a single ConfigMap.

    WARNING: ConfigMaps can contain secret-shaped values (e.g. credentials stored
    as plain config). When this tool is served through the k8stools MCP server, the
    output passes through a redaction step by default (see the `redaction` module
    and the server's `--no-redact` flag). Callers using this function directly get
    the raw values and are responsible for their own redaction.

    Parameters
    ----------
    name : str
        Name of the ConfigMap.
    namespace : str, optional
        Namespace of the ConfigMap (default is "default").

    Returns
    -------
    dict[str, Any]
        A dictionary describing the ConfigMap with the following keys:

        name : str
            Name of the ConfigMap.
        namespace : str
            Namespace of the ConfigMap.
        data : dict[str, str]
            The string key/value data map (empty dict if none).
        binary_data_keys : list[str]
            Names of any binary keys. The binary values themselves are not
            returned.

    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the ConfigMap is not found or the API call fails.
    """
    global K8S
    if K8S is None:
        K8S = _get_api_client()
    logging.info(f"get_configmap(name={name}, namespace={namespace})")
    try:
        config_map = K8S.read_namespaced_config_map(name=name, namespace=namespace)
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            raise K8sApiError(
                f"ConfigMap '{name}' not found in namespace '{namespace}'."
            ) from e
        raise K8sApiError(
            f"Error getting ConfigMap '{name}' in namespace '{namespace}': {e}"
        ) from e
    return {
        "name": config_map.metadata.name,
        "namespace": config_map.metadata.namespace,
        "data": dict(config_map.data) if getattr(config_map, "data", None) else {},
        "binary_data_keys": list(config_map.binary_data.keys())
        if getattr(config_map, "binary_data", None) else [],
    }


def print_configmap(name: str, namespace: str = "default") -> None:
    """Pretty prints the contents of the specified ConfigMap as YAML."""
    try:
        print(yaml.safe_dump(get_configmap(name, namespace), default_flow_style=False, sort_keys=False))
    except Exception as e:
        print(f"Error printing config map: {e}")


class StatefulSetSummary(BaseModel):
    """A summary of a StatefulSet like returned by `kubectl get statefulsets`,
    mirroring DeploymentSummary for the stateful workloads (databases, queues,
    etc.) that deployments don't cover."""
    name: str
    namespace: str
    total_replicas: int
    ready_replicas: int
    current_replicas: int
    update_strategy: str
    service_name: Optional[str] = None
    age: datetime.timedelta


def get_statefulset_summaries(namespace: Optional[str] = None) -> list[StatefulSetSummary]:
    """Retrieves a list of StatefulSetSummary objects for StatefulSets in a given
    namespace or all namespaces, similar to `kubectl get statefulsets`.

    Parameters
    ----------
    namespace : Optional[str], default=None
        The specific namespace to list StatefulSets from. If None, lists from all
        namespaces.

    Returns
    -------
    list of StatefulSetSummary
        A list of StatefulSetSummary objects, each with the following fields:

        name : str
            Name of the StatefulSet.
        namespace : str
            Namespace in which the StatefulSet runs.
        total_replicas : int
            Desired number of replicas.
        ready_replicas : int
            Number of replicas currently ready.
        current_replicas : int
            Number of replicas created by the current revision.
        update_strategy : str
            Update strategy type (e.g. "RollingUpdate", "OnDelete").
        service_name : Optional[str]
            Name of the governing (headless) service.
        age : datetime.timedelta
            Age of the StatefulSet (current time minus creation timestamp).

    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call to list StatefulSets fails.
    """
    global APPS_V1_API
    if APPS_V1_API is None:
        APPS_V1_API = _get_apps_v1_api_client()
    logging.info(f"get_statefulset_summaries(namespace={namespace})")
    try:
        if namespace:
            stateful_sets = APPS_V1_API.list_namespaced_stateful_set(namespace=namespace).items
        else:
            stateful_sets = APPS_V1_API.list_stateful_set_for_all_namespaces().items
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching stateful sets: {e}") from e

    now = datetime.datetime.now(datetime.timezone.utc)
    summaries: list[StatefulSetSummary] = []
    for sts in stateful_sets:
        spec = sts.spec
        status = sts.status
        total_replicas = spec.replicas if spec and spec.replicas is not None else 0
        ready_replicas = status.ready_replicas if status and status.ready_replicas is not None else 0
        current_replicas = status.current_replicas if status and status.current_replicas is not None else 0
        update_strategy = "Unknown"
        if spec and getattr(spec, "update_strategy", None) and spec.update_strategy.type:
            update_strategy = spec.update_strategy.type
        service_name = spec.service_name if spec and getattr(spec, "service_name", None) else None
        age = datetime.timedelta(0)
        if sts.metadata.creation_timestamp:
            age = now - sts.metadata.creation_timestamp
        summaries.append(StatefulSetSummary(
            name=sts.metadata.name,
            namespace=sts.metadata.namespace,
            total_replicas=total_replicas,
            ready_replicas=ready_replicas,
            current_replicas=current_replicas,
            update_strategy=update_strategy,
            service_name=service_name,
            age=age,
        ))
    return summaries


def print_statefulset_summaries(namespace: Optional[str] = None) -> None:
    """Calls get_statefulset_summaries and prints the output to stdout."""
    summaries = get_statefulset_summaries(namespace)
    print(f"{'NAME':<32} {'NAMESPACE':<20} {'READY':<10} {'SERVICE':<24} {'AGE':<12}")
    for sts in summaries:
        ready = f"{sts.ready_replicas}/{sts.total_replicas}"
        service_name = sts.service_name if sts.service_name else "<none>"
        age = _format_timedelta(sts.age)
        print(f"{sts.name:<32} {sts.namespace:<20} {ready:<10} {service_name:<24} {age:<12}")


class ContainerTemplateSummary(BaseModel):
    """A container as declared in a pod template (e.g. inside a CronJob or Job),
    with just the image and literal environment values that matter for RCA."""
    name: str
    image: str
    env: dict[str, str] = Field(default_factory=dict)


def _container_templates(pod_spec) -> list[ContainerTemplateSummary]:
    """Extract ContainerTemplateSummary list from a V1PodSpec-like object."""
    result: list[ContainerTemplateSummary] = []
    if not pod_spec or not getattr(pod_spec, "containers", None):
        return result
    for container in pod_spec.containers:
        env: dict[str, str] = {}
        if getattr(container, "env", None):
            for env_var in container.env:
                if getattr(env_var, "value", None) is not None:
                    env[env_var.name] = env_var.value
                elif getattr(env_var, "value_from", None) is not None:
                    env[env_var.name] = "<valueFrom>"
        result.append(ContainerTemplateSummary(
            name=container.name,
            image=container.image,
            env=env,
        ))
    return result


class CronJobSummary(BaseModel):
    """A summary of a CronJob like returned by `kubectl get cronjobs`, plus the
    pod template's container images/env (schedules and env-configured timeouts are
    common root-cause material for cleanup / warm-pool jobs)."""
    name: str
    namespace: str
    schedule: str
    suspend: bool
    active: int
    last_schedule_time: Optional[datetime.timedelta] = None
    last_successful_time: Optional[datetime.timedelta] = None
    age: datetime.timedelta
    containers: list[ContainerTemplateSummary] = Field(default_factory=list)


def get_cronjob_summaries(namespace: Optional[str] = None) -> list[CronJobSummary]:
    """Retrieves a list of CronJobSummary objects for CronJobs in a given namespace
    or all namespaces, similar to `kubectl get cronjobs`.

    Parameters
    ----------
    namespace : Optional[str], default=None
        The specific namespace to list CronJobs from. If None, lists from all
        namespaces.

    Returns
    -------
    list of CronJobSummary
        A list of CronJobSummary objects, each with the following fields:

        name : str
            Name of the CronJob.
        namespace : str
            Namespace of the CronJob.
        schedule : str
            Cron schedule expression.
        suspend : bool
            Whether the CronJob is suspended.
        active : int
            Number of currently active (running) jobs.
        last_schedule_time : Optional[datetime.timedelta]
            Time since the CronJob was last scheduled (None if never).
        last_successful_time : Optional[datetime.timedelta]
            Time since the CronJob last completed successfully (None if never).
        age : datetime.timedelta
            Age of the CronJob (current time minus creation timestamp).
        containers : list[ContainerTemplateSummary]
            The job pod template's containers, each with name, image, and literal
            environment values.

    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call to list CronJobs fails.
    """
    global BATCH_V1_API
    if BATCH_V1_API is None:
        BATCH_V1_API = _get_batch_v1_api_client()
    logging.info(f"get_cronjob_summaries(namespace={namespace})")
    try:
        if namespace:
            cron_jobs = BATCH_V1_API.list_namespaced_cron_job(namespace=namespace).items
        else:
            cron_jobs = BATCH_V1_API.list_cron_job_for_all_namespaces().items
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching cron jobs: {e}") from e

    now = datetime.datetime.now(datetime.timezone.utc)
    summaries: list[CronJobSummary] = []
    for cron_job in cron_jobs:
        spec = cron_job.spec
        status = cron_job.status
        active = len(status.active) if status and status.active else 0
        last_schedule_time = None
        if status and getattr(status, "last_schedule_time", None):
            last_schedule_time = now - status.last_schedule_time
        last_successful_time = None
        if status and getattr(status, "last_successful_time", None):
            last_successful_time = now - status.last_successful_time
        age = datetime.timedelta(0)
        if cron_job.metadata.creation_timestamp:
            age = now - cron_job.metadata.creation_timestamp
        pod_spec = None
        if spec and getattr(spec, "job_template", None) and spec.job_template.spec \
                and getattr(spec.job_template.spec, "template", None):
            pod_spec = spec.job_template.spec.template.spec
        summaries.append(CronJobSummary(
            name=cron_job.metadata.name,
            namespace=cron_job.metadata.namespace,
            schedule=spec.schedule if spec else "",
            suspend=bool(spec.suspend) if spec and spec.suspend is not None else False,
            active=active,
            last_schedule_time=last_schedule_time,
            last_successful_time=last_successful_time,
            age=age,
            containers=_container_templates(pod_spec),
        ))
    return summaries


def print_cronjob_summaries(namespace: Optional[str] = None) -> None:
    """Calls get_cronjob_summaries and prints the output to stdout."""
    summaries = get_cronjob_summaries(namespace)
    print(f"{'NAME':<32} {'NAMESPACE':<20} {'SCHEDULE':<16} {'SUSPEND':<8} {'ACTIVE':<7} {'LAST SCHEDULE':<14} {'AGE':<12}")
    for cj in summaries:
        last_schedule = _format_timedelta(cj.last_schedule_time) if cj.last_schedule_time else "<none>"
        age = _format_timedelta(cj.age)
        print(f"{cj.name:<32} {cj.namespace:<20} {cj.schedule:<16} {str(cj.suspend):<8} {cj.active:<7} {last_schedule:<14} {age:<12}")


class JobSummary(BaseModel):
    """A summary of a Job like returned by `kubectl get jobs`, plus its owning
    CronJob (if any) and pod template containers."""
    name: str
    namespace: str
    owner: Optional[str] = None  # owning CronJob name, if created by one
    active: int
    succeeded: int
    failed: int
    start_time: Optional[datetime.timedelta] = None
    completion_time: Optional[datetime.timedelta] = None
    conditions: list[str] = Field(default_factory=list)
    age: datetime.timedelta
    containers: list[ContainerTemplateSummary] = Field(default_factory=list)


def _cronjob_owner(metadata) -> Optional[str]:
    for owner_ref in (getattr(metadata, "owner_references", None) or []):
        if owner_ref.kind == "CronJob":
            return owner_ref.name
    return None


def get_job_summaries(namespace: Optional[str] = None) -> list[JobSummary]:
    """Retrieves a list of JobSummary objects for Jobs in a given namespace or all
    namespaces, similar to `kubectl get jobs`.

    Parameters
    ----------
    namespace : Optional[str], default=None
        The specific namespace to list Jobs from. If None, lists from all namespaces.

    Returns
    -------
    list of JobSummary
        A list of JobSummary objects, each with the following fields:

        name : str
            Name of the Job.
        namespace : str
            Namespace of the Job.
        owner : Optional[str]
            Name of the owning CronJob, if this Job was created by one.
        active : int
            Number of actively running pods.
        succeeded : int
            Number of pods that completed successfully.
        failed : int
            Number of pods that terminated in failure.
        start_time : Optional[datetime.timedelta]
            Time since the Job started (None if not started).
        completion_time : Optional[datetime.timedelta]
            Time since the Job completed (None if not complete).
        conditions : list[str]
            Status condition types currently True (e.g. ["Complete"], ["Failed"]).
        age : datetime.timedelta
            Age of the Job (current time minus creation timestamp).
        containers : list[ContainerTemplateSummary]
            The Job pod template's containers, each with name, image, and literal
            environment values.

    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call to list Jobs fails.
    """
    global BATCH_V1_API
    if BATCH_V1_API is None:
        BATCH_V1_API = _get_batch_v1_api_client()
    logging.info(f"get_job_summaries(namespace={namespace})")
    try:
        if namespace:
            jobs = BATCH_V1_API.list_namespaced_job(namespace=namespace).items
        else:
            jobs = BATCH_V1_API.list_job_for_all_namespaces().items
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching jobs: {e}") from e

    now = datetime.datetime.now(datetime.timezone.utc)
    summaries: list[JobSummary] = []
    for job in jobs:
        status = job.status
        active = status.active if status and status.active is not None else 0
        succeeded = status.succeeded if status and status.succeeded is not None else 0
        failed = status.failed if status and status.failed is not None else 0
        start_time = None
        if status and getattr(status, "start_time", None):
            start_time = now - status.start_time
        completion_time = None
        if status and getattr(status, "completion_time", None):
            completion_time = now - status.completion_time
        conditions = []
        if status and getattr(status, "conditions", None):
            conditions = [c.type for c in status.conditions if c.status == "True"]
        age = datetime.timedelta(0)
        if job.metadata.creation_timestamp:
            age = now - job.metadata.creation_timestamp
        pod_spec = job.spec.template.spec if job.spec and getattr(job.spec, "template", None) else None
        summaries.append(JobSummary(
            name=job.metadata.name,
            namespace=job.metadata.namespace,
            owner=_cronjob_owner(job.metadata),
            active=active,
            succeeded=succeeded,
            failed=failed,
            start_time=start_time,
            completion_time=completion_time,
            conditions=conditions,
            age=age,
            containers=_container_templates(pod_spec),
        ))
    return summaries


def print_job_summaries(namespace: Optional[str] = None) -> None:
    """Calls get_job_summaries and prints the output to stdout."""
    summaries = get_job_summaries(namespace)
    print(f"{'NAME':<40} {'NAMESPACE':<20} {'OWNER':<24} {'COMPLETIONS':<12} {'CONDITIONS':<16} {'AGE':<12}")
    for job in summaries:
        owner = job.owner if job.owner else "<none>"
        completions = f"{job.succeeded} ok / {job.failed} fail"
        conditions = ",".join(job.conditions) if job.conditions else "<none>"
        age = _format_timedelta(job.age)
        print(f"{job.name:<40} {job.namespace:<20} {owner:<24} {completions:<12} {conditions:<16} {age:<12}")


def _latest_pod_name_for_selector(namespace: str, label_selector: str) -> Optional[str]:
    """Return the name of the most-recently-created pod matching a label selector,
    or None if there are no matching pods."""
    global K8S
    if K8S is None:
        K8S = _get_api_client()
    try:
        pods = K8S.list_namespaced_pod(namespace=namespace, label_selector=label_selector).items
    except client.ApiException as e:
        raise K8sApiError(f"Error listing pods for selector '{label_selector}': {e}") from e
    if not pods:
        return None
    pods_with_ts = [p for p in pods if p.metadata.creation_timestamp]
    if not pods_with_ts:
        return pods[0].metadata.name
    latest = max(pods_with_ts, key=lambda p: p.metadata.creation_timestamp)
    return latest.metadata.name


def get_logs_for_job(job_name: str, namespace: str = "default",
                     container_name: Optional[str] = None,
                     tail: Optional[int] = None,
                     since_seconds: Optional[int] = None,
                     previous: bool = False) -> Optional[str]:
    """Retrieves logs from the most-recently-created pod of a Job.

    Job pods are short-lived, so this convenience finds the newest pod belonging to
    the Job (via its `job-name` label) and returns its logs, saving the caller from
    listing pods and sorting by creation time.

    Parameters
    ----------
    job_name : str
        Name of the Job.
    namespace : str, optional
        Namespace of the Job (default is "default").
    container_name : str, optional
        Container within the pod. If None, defaults to the first container.
    tail : int, optional
        Number of lines from the end of the log (default: last 1000).
    since_seconds : int, optional
        If set, only return logs newer than this many seconds.
    previous : bool, default False
        If True, return logs from the previous terminated container instance.

    Returns
    -------
    str, optional
        Log content, or None if the Job has no pods.

    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call fails.
    """
    logging.info(f"get_logs_for_job(job_name={job_name}, namespace={namespace})")
    pod_name = _latest_pod_name_for_selector(namespace, f"job-name={job_name}")
    if pod_name is None:
        return None
    return get_logs_for_pod_and_container(pod_name, namespace, container_name,
                                          tail=tail, since_seconds=since_seconds,
                                          previous=previous)


def get_logs_for_cronjob(cronjob_name: str, namespace: str = "default",
                         container_name: Optional[str] = None,
                         tail: Optional[int] = None,
                         since_seconds: Optional[int] = None,
                         previous: bool = False) -> Optional[str]:
    """Retrieves logs from the most-recent run of a CronJob.

    Finds the newest Job owned by the CronJob, then returns the logs of that Job's
    most-recently-created pod. Removes the need to manually locate the right
    short-lived pod for a CronJob's last tick.

    Parameters
    ----------
    cronjob_name : str
        Name of the CronJob.
    namespace : str, optional
        Namespace of the CronJob (default is "default").
    container_name : str, optional
        Container within the pod. If None, defaults to the first container.
    tail : int, optional
        Number of lines from the end of the log (default: last 1000).
    since_seconds : int, optional
        If set, only return logs newer than this many seconds.
    previous : bool, default False
        If True, return logs from the previous terminated container instance.

    Returns
    -------
    str, optional
        Log content, or None if the CronJob has no jobs/pods yet.

    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call fails.
    """
    global BATCH_V1_API
    if BATCH_V1_API is None:
        BATCH_V1_API = _get_batch_v1_api_client()
    logging.info(f"get_logs_for_cronjob(cronjob_name={cronjob_name}, namespace={namespace})")
    try:
        jobs = BATCH_V1_API.list_namespaced_job(namespace=namespace).items
    except client.ApiException as e:
        raise K8sApiError(f"Error listing jobs for cronjob '{cronjob_name}': {e}") from e
    owned = [j for j in jobs if _cronjob_owner(j.metadata) == cronjob_name and j.metadata.creation_timestamp]
    if not owned:
        return None
    latest_job = max(owned, key=lambda j: j.metadata.creation_timestamp)
    return get_logs_for_job(latest_job.metadata.name, namespace, container_name,
                            tail=tail, since_seconds=since_seconds, previous=previous)


class PVCSummary(BaseModel):
    """A summary of a PersistentVolumeClaim like returned by `kubectl get pvc`,
    with the pods currently mounting it resolved where possible."""
    name: str
    namespace: str
    status: str  # Bound / Pending / Lost
    volume_name: Optional[str] = None
    capacity: Optional[str] = None
    access_modes: list[str] = Field(default_factory=list)
    storage_class: Optional[str] = None
    mounted_by: list[str] = Field(default_factory=list)
    age: datetime.timedelta


def get_pvc_summaries(namespace: Optional[str] = None) -> list[PVCSummary]:
    """Retrieves a list of PVCSummary objects for PersistentVolumeClaims in a given
    namespace or all namespaces, similar to `kubectl get pvc`.

    For each PVC, this also resolves which pods currently mount it (by scanning pod
    volumes in the same scope), which is useful for spotting orphaned PVCs — claims
    with an empty `mounted_by` and no owning pod.

    Parameters
    ----------
    namespace : Optional[str], default=None
        The specific namespace to list PVCs from. If None, lists from all namespaces.

    Returns
    -------
    list of PVCSummary
        A list of PVCSummary objects, each with the following fields:

        name : str
            Name of the PVC.
        namespace : str
            Namespace of the PVC.
        status : str
            Phase of the PVC ("Bound", "Pending", or "Lost").
        volume_name : Optional[str]
            Name of the bound PersistentVolume (None if unbound).
        capacity : Optional[str]
            Storage capacity (e.g. "10Gi"); falls back to the requested size when
            the claim is not yet bound.
        access_modes : list[str]
            Access modes (e.g. ["ReadWriteOnce"]).
        storage_class : Optional[str]
            StorageClass backing the claim.
        mounted_by : list[str]
            Names of pods (in the same scope) currently mounting this PVC. Empty
            for orphaned/unmounted claims.
        age : datetime.timedelta
            Age of the PVC (current time minus creation timestamp).

    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call to list PVCs fails.
    """
    global K8S
    if K8S is None:
        K8S = _get_api_client()
    logging.info(f"get_pvc_summaries(namespace={namespace})")
    try:
        if namespace:
            claims = K8S.list_namespaced_persistent_volume_claim(namespace=namespace).items
        else:
            claims = K8S.list_persistent_volume_claim_for_all_namespaces().items
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching persistent volume claims: {e}") from e

    # Build a map of (namespace, claim_name) -> [pod names] by scanning pod volumes.
    claim_to_pods: dict[tuple[str, str], list[str]] = {}
    try:
        if namespace:
            pods = K8S.list_namespaced_pod(namespace=namespace).items
        else:
            pods = K8S.list_pod_for_all_namespaces().items
        for pod in pods:
            if not pod.spec or not getattr(pod.spec, "volumes", None):
                continue
            for volume in pod.spec.volumes:
                pvc_ref = getattr(volume, "persistent_volume_claim", None)
                if pvc_ref and getattr(pvc_ref, "claim_name", None):
                    key = (pod.metadata.namespace, pvc_ref.claim_name)
                    claim_to_pods.setdefault(key, []).append(pod.metadata.name)
    except client.ApiException:
        # Pod resolution is best-effort; leave mounted_by empty on failure.
        claim_to_pods = {}

    now = datetime.datetime.now(datetime.timezone.utc)
    summaries: list[PVCSummary] = []
    for claim in claims:
        spec = claim.spec
        status = claim.status
        capacity = None
        if status and getattr(status, "capacity", None) and status.capacity.get("storage"):
            capacity = status.capacity["storage"]
        elif spec and getattr(spec, "resources", None) and getattr(spec.resources, "requests", None):
            capacity = spec.resources.requests.get("storage")
        age = datetime.timedelta(0)
        if claim.metadata.creation_timestamp:
            age = now - claim.metadata.creation_timestamp
        key = (claim.metadata.namespace, claim.metadata.name)
        summaries.append(PVCSummary(
            name=claim.metadata.name,
            namespace=claim.metadata.namespace,
            status=status.phase if status and status.phase else "Unknown",
            volume_name=spec.volume_name if spec and getattr(spec, "volume_name", None) else None,
            capacity=capacity,
            access_modes=list(spec.access_modes) if spec and getattr(spec, "access_modes", None) else [],
            storage_class=spec.storage_class_name if spec and getattr(spec, "storage_class_name", None) else None,
            mounted_by=claim_to_pods.get(key, []),
            age=age,
        ))
    return summaries


def print_pvc_summaries(namespace: Optional[str] = None) -> None:
    """Calls get_pvc_summaries and prints the output to stdout."""
    summaries = get_pvc_summaries(namespace)
    print(f"{'NAME':<40} {'NAMESPACE':<20} {'STATUS':<10} {'CAPACITY':<10} {'STORAGECLASS':<20} {'MOUNTED-BY':<24} {'AGE':<12}")
    for pvc in summaries:
        capacity = pvc.capacity if pvc.capacity else "<none>"
        storage_class = pvc.storage_class if pvc.storage_class else "<none>"
        mounted_by = ",".join(pvc.mounted_by) if pvc.mounted_by else "<none>"
        age = _format_timedelta(pvc.age)
        print(f"{pvc.name:<40} {pvc.namespace:<20} {pvc.status:<10} {capacity:<10} {storage_class:<20} {mounted_by:<24} {age:<12}")


def get_events(namespace: Optional[str] = None,
               reason: Optional[str] = None,
               involved_kind: Optional[str] = None,
               involved_name: Optional[str] = None,
               event_type: Optional[str] = None) -> list[EventSummary]:
    """Lists cluster- or namespace-wide events with optional server-side filtering.

    Unlike `get_pod_events` (which is scoped to a single named pod), this supports
    the sweep queries common in capacity/storage runbooks — e.g. all "Evicted"
    events, or all "FailedScheduling" events — where the affected pods often have no
    stable name to look up. Note the Kubernetes API only retains roughly the last
    hour of events.

    Parameters
    ----------
    namespace : Optional[str], default=None
        Namespace to list events from. If None, lists across all namespaces.
    reason : Optional[str], default=None
        If set, only return events with this reason (e.g. "Evicted",
        "FailedScheduling", "BackOff").
    involved_kind : Optional[str], default=None
        If set, only return events whose involved object is of this kind (e.g.
        "Pod", "Node", "PersistentVolumeClaim").
    involved_name : Optional[str], default=None
        If set, only return events whose involved object has this name.
    event_type : Optional[str], default=None
        If set, only return events of this type ("Normal" or "Warning").

    Returns
    -------
    list of EventSummary
        Matching events. Each EventSummary has the following fields:

        last_seen : Optional[datetime.timedelta]
            Time since the event was last seen (if available).
        type : str
            Type of the event ("Normal" or "Warning").
        reason : str
            Reason for the event.
        object : str
            The involved object as "Kind/name" (or just the name when the kind is
            unavailable).
        message : str
            Message describing the event.

    Raises
    ------
    K8sConfigError
        If unable to initialize the K8S API.
    K8sApiError
        If the API call to list events fails.
    """
    global K8S
    if K8S is None:
        K8S = _get_api_client()
    logging.info(f"get_events(namespace={namespace}, reason={reason}, "
                 f"involved_kind={involved_kind}, involved_name={involved_name}, "
                 f"event_type={event_type})")
    selectors = []
    if reason:
        selectors.append(f"reason={reason}")
    if involved_kind:
        selectors.append(f"involvedObject.kind={involved_kind}")
    if involved_name:
        selectors.append(f"involvedObject.name={involved_name}")
    if event_type:
        selectors.append(f"type={event_type}")
    field_selector = ",".join(selectors) if selectors else None

    try:
        if namespace:
            events = K8S.list_namespaced_event(namespace, field_selector=field_selector).items
        else:
            events = K8S.list_event_for_all_namespaces(field_selector=field_selector).items
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching events: {e}") from e

    now = datetime.datetime.now(datetime.timezone.utc)
    results: list[EventSummary] = []
    for event in events:
        involved = getattr(event, "involved_object", None)
        obj_name = getattr(involved, "name", None) or ""
        obj_kind = getattr(involved, "kind", None)
        obj = f"{obj_kind}/{obj_name}" if obj_kind else obj_name
        results.append(EventSummary(
            last_seen=(now - event.last_timestamp) if getattr(event, "last_timestamp", None) else None,
            type=event.type or "",
            reason=event.reason or "",
            object=obj,
            message=event.message or "",
        ))
    return results


def print_events(namespace: Optional[str] = None,
                 reason: Optional[str] = None,
                 involved_kind: Optional[str] = None,
                 involved_name: Optional[str] = None,
                 event_type: Optional[str] = None) -> None:
    """Calls get_events and prints the output to stdout, similar to
    `kubectl get events`."""
    events = get_events(namespace, reason, involved_kind, involved_name, event_type)
    print(f"{'LAST SEEN':<12} {'TYPE':<10} {'REASON':<20} {'OBJECT':<40} {'MESSAGE':<40}")
    for event in events:
        last_seen = _format_timedelta(event.last_seen) if event.last_seen else "-"
        message = (event.message[:37] + '...') if event.message and len(event.message) > 40 else event.message
        print(f"{last_seen:<12} {event.type:<10} {event.reason:<20} {event.object:<40} {message:<40}")


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