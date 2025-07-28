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
    """A summary of a node's status like returned by `kubectl get nodes -o wide`"""
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
            container_runtime=container_runtime
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
                                    container_name:Optional[str]=None) -> Optional[str]:
    """
    Retrieves logs from a Kubernetes pod and container.

    Args:
        namespace (str): The namespace of the pod.
        pod_name (str): The name of the pod.
        container_name (str, optional): The name of the container within the pod.
                                        If None, defaults to the first container.

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
        # read_namespaced_pod_log with reasonable limits to avoid memory issues
        resp = K8S.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container_name,  # Pass container_name if specified
            follow=False,              # Set to False to get all current logs
            _preload_content=True,     # Important: This loads all content into memory
            timestamps=True,           # Optional: Include timestamps
            tail_lines=1000,          # Limit to last 1000 lines to avoid memory issues
            limit_bytes=1024*1024     # Limit to 1MB to avoid memory issues
        )

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
    """A summary of a service's status like returned by `kubectl get servicess`"""
    name: str
    namespace: str
    type: str
    cluster_ip: Optional[str] = None
    external_ip: Optional[str] = None
    ports: list[PortInfo]
    age: datetime.timedelta

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

        service_summary = ServiceSummary(
            name=service_name,
            namespace=service_namespace,
            type=service_type,
            cluster_ip=cluster_ip,
            external_ip=external_ip,
            ports=ports,
            age=age
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