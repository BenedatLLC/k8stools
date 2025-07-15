"""Tools for interacting with kubernetes.
These are build on top of the kubernetes Python API (https://github.com/kubernetes-client/python).
The goal is to provide well-documented and strongly typed tools. There are three kinds
of tools provided here:
1. There are tools that mimic the output of kubectl commands (e.g. get_pod_summaries).
   strongly-typed Pydantic models are used for the return values of these tools.
2. There are tools that return strongly typed Pydantic models that attempt to match the associated Kubernetes
   client types (see https://github.com/kubernetes-client/python/tree/master/kubernetes/docs).
   Lesser used fields may be omitted from these models. An example of this case is get_pod_container_statuses.
3. In some cases we simply call to_dict() on the class returned by the API (defined in 
   https://github.com/kubernetes-client/python/tree/master/kubernetes/client/models).
   The return type is dict[str,Any], but we document the fields in the function's docstring.
   get_pod_spec is an example of this type of tool.

These are the tools we define:

* get_namespaces - get a list of namespaces, like `kubectl get namespace`
* get_pod_summaries - get a list of pods, like `kubectl get pods`
* get_pod_container_statuses - return the status for each of the container in a pod
* get_pod_events - return the events for a pod
* get_pod_spec - retrieves the spec for a given pod
* get_logs_for_pod_and_container - retrieves logs from a pod and container

We also define a set of associated "print_" functions that are helpful in debugging:

* print_namespaces
* print_pod_summaries
* print_pod_container_statuses
* print_pod_events
* print_pod_spec
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

class K8sConfigError(Exception):
    """This is thrown when ampting to load the config or initializing the API fails."""
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
    """A summary of a pod's status like returned by `kubectl get pods`"""
    name: str
    namespace: str
    total_containers: int
    ready_containers: int
    restarts: int
    last_restart: Optional[datetime.timedelta]
    age: datetime.timedelta

   

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

        pod_summary = PodSummary(
            name=pod_name,
            namespace=pod_namespace,
            total_containers=total_containers,
            ready_containers=ready_containers,
            restarts=total_restarts,
            last_restart=last_restart_timedelta,
            age=age
        )
        pod_summaries.append(pod_summary)
    
    return pod_summaries

def print_pod_summaries(namespace: Optional[str] = None) -> None:
    """
    Calls get_pod_summaries and prints the output to stdout, using
    the same format as `kubectl get pods`.
    """
    pod_summaries = get_pod_summaries(namespace)
    # Print header
    print(f"{'NAME':<32} {'NAMESPACE':<20} {'READY':<10} {'RESTARTS':<10} {'AGE':<12} {'LAST_RESTART':<15}")
    for pod in pod_summaries:
        ready = f"{pod.ready_containers}/{pod.total_containers}"
        restarts = str(pod.restarts)
        age = _format_timedelta(pod.age)
        last_restart = _format_timedelta(pod.last_restart) if pod.last_restart is not None else "-"
        print(f"{pod.name:<32} {pod.namespace:<20} {ready:<10} {restarts:<10} {age:<12} {last_restart:<15}")

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

def _v1_container_state_to_container_state(container_state:client.V1ContainerState) -> ContainerState:
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
        raise K8sApiError(f"Unexpected container state: {container_state}")
    
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
        # read_namespaced_pod_log with follow=False and _preload_content=True (default)
        # returns the entire log content as a string
        resp = K8S.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container_name,  # Pass container_name if specified
            follow=False,              # Set to False to get all current logs
            _preload_content=True,     # Important: This loads all content into memory
            timestamps=True            # Optional: Include timestamps
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


TOOLS = [
    get_namespaces,
    get_pod_summaries,
    get_pod_container_statuses,
    get_pod_events,
    get_pod_spec,
    get_logs_for_pod_and_container
]