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
import json
import re
from typing import Optional, Union, Literal, Any, Annotated

from pydantic import BaseModel, Field, AfterValidator, model_serializer, model_validator
import yaml

from kubernetes import client, config
from kubernetes.client import V1PodSpec, ApiException
from kubernetes.client.models.v1_container_status import V1ContainerStatus

K8S:Optional[client.CoreV1Api] = None
APPS_V1_API:Optional[client.AppsV1Api] = None
BATCH_V1_API:Optional[client.BatchV1Api] = None
AUTOSCALING_V2_API:Optional[client.AutoscalingV2Api] = None
DISCOVERY_V1_API:Optional[client.DiscoveryV1Api] = None

class K8sConfigError(Exception):
    """This is thrown when atempting to load the config or initializing the API fails."""
    pass

class K8sClusterSelectionError(K8sConfigError):
    """An explicitly selected kubeconfig or context could not be loaded.

    Separate from :class:`K8sConfigError` because the two call for different
    handling: with no explicit selection, the MCP server starts anyway and every
    call reports the problem (as it always has), but a selection that failed has
    to stop the server - carrying on would serve some other cluster.
    """
    pass

class K8sApiError(Exception):
    """This is thrown when one of the kubernetes calls (other than initial API load) fails."""
    pass


class K8sMetricsUnavailable(K8sApiError):
    """The metrics API (metrics.k8s.io, served by metrics-server) isn't available:
    a normal cluster setup, not a fault. Also raised when replaying a capture
    taken without metrics."""


#: Environment variable naming the kubeconfig context to use, for deployments that
#: pin a server through its environment rather than its command line. There is no
#: counterpart for the file: ``KUBECONFIG`` already is that variable.
CONTEXT_ENV_VAR = "K8STOOLS_CONTEXT"


class _Binding:
    """The cluster this process is bound to: one ApiClient plus where it came from."""

    def __init__(self, api_client: client.ApiClient, source: str,
                 context: Optional[str], server: Optional[str],
                 kubeconfig: Optional[str]):
        self.api_client = api_client
        self.source = source
        self.context = context
        self.server = server
        self.kubeconfig = kubeconfig

    def describe(self) -> str:
        if self.source == "in-cluster":
            return f"in-cluster service account at {self.server}"
        return (f"context '{self.context}' at {self.server} "
                f"(kubeconfig: {self.kubeconfig})")


_BINDING: Optional[_Binding] = None


def _resolve_binding(kubeconfig: Optional[str], context: Optional[str]) -> _Binding:
    """Load the cluster configuration once, into a client of its own.

    An explicit selection (a kubeconfig path, or a context from the argument or
    ``K8STOOLS_CONTEXT``) either loads or raises :class:`K8sClusterSelectionError`.
    It never falls back to in-cluster config: the client raises the same
    ``ConfigException`` for a misspelled context as for a missing kubeconfig, so a
    fallback would bind a server running in a pod to that pod's cluster whenever a
    context name had a typo.

    With no explicit selection the behavior is the one this module always had:
    ``KUBECONFIG`` (or ``~/.kube/config``) and its current context, then in-cluster
    config if there is no usable kubeconfig.
    """
    from_env = context is None and bool(os.environ.get(CONTEXT_ENV_VAR))
    context = context or os.environ.get(CONTEXT_ENV_VAR) or None
    explicit = kubeconfig is not None or context is not None
    path = os.path.expanduser(kubeconfig) if kubeconfig else None
    reported_path = path or os.path.expanduser(config.kube_config.KUBE_CONFIG_DEFAULT_LOCATION)
    try:
        # Name the context before loading it, and load exactly that one, so the
        # binding and its description cannot disagree even if someone runs
        # `kubectl config use-context` between the two reads.
        if context is None:
            _, current = config.list_kube_config_contexts(config_file=path)
            context = current["name"] if current else None
        configuration = client.Configuration()
        config.load_kube_config(config_file=path, context=context,
                                client_configuration=configuration)
        return _Binding(client.ApiClient(configuration), "kubeconfig", context,
                        configuration.host, reported_path)
    except config.ConfigException as e:
        if explicit:
            what = []
            if kubeconfig is not None:
                what.append(f"kubeconfig '{kubeconfig}'")
            if context is not None:
                what.append(f"context '{context}'"
                            + (f" (from {CONTEXT_ENV_VAR})" if from_env else ""))
            raise K8sClusterSelectionError(
                f"Could not load the selected {' and '.join(what)}: {e}") from e
        logging.warning("Could not load kube config. Ensure you have a valid Kubernetes configuration.")
        logging.warning("Attempting to load in-cluster config...")
    try:
        configuration = client.Configuration()
        config.load_incluster_config(client_configuration=configuration)
        return _Binding(client.ApiClient(configuration), "in-cluster", None,
                        configuration.host, None)
    except config.ConfigException as e:
        raise K8sConfigError("Could not load in-cluster config. No Kubernetes config found.") from e
    except Exception as e:
        raise K8sConfigError(f"Unexpected error: {e}") from e


def configure(kubeconfig: Optional[str] = None, context: Optional[str] = None) -> str:
    """Bind the tools to a cluster, replacing any earlier binding.

    Optional: with no call, the tools bind on first use exactly as ``configure()``
    with no arguments would. Every API group (core, apps, batch) shares the one
    binding, so the tools cannot end up answering from two clusters.

    Parameters
    ----------
    kubeconfig
        Path of the kubeconfig file. If None, ``KUBECONFIG`` or ``~/.kube/config``.
    context
        Context to use from it. If None, ``K8STOOLS_CONTEXT``, then the file's
        ``current-context``.

    Returns
    -------
    str
        A one-line description of the binding, for logging.

    Raises
    ------
    K8sClusterSelectionError
        If an explicitly selected kubeconfig or context cannot be loaded.
    K8sConfigError
        If there is no selection and neither a kubeconfig nor in-cluster config
        can be loaded.
    """
    global _BINDING, K8S, APPS_V1_API, BATCH_V1_API, AUTOSCALING_V2_API, DISCOVERY_V1_API
    binding = _resolve_binding(kubeconfig, context)
    _BINDING = binding
    K8S = APPS_V1_API = BATCH_V1_API = AUTOSCALING_V2_API = DISCOVERY_V1_API = None
    return binding.describe()


def _binding() -> _Binding:
    global _BINDING
    if _BINDING is None:
        _BINDING = _resolve_binding(None, None)
    return _BINDING


# All three API groups are built on the one bound ApiClient. They used to load the
# kubeconfig separately, each on its first use, and each snapshots the global
# default configuration when constructed - so a `kubectl config use-context`
# between a server's first pod query and its first deployment query left it
# reading pods from one cluster and deployments from another.

def _get_api_client() -> client.CoreV1Api:
    return client.CoreV1Api(_binding().api_client)


def _get_apps_v1_api_client() -> client.AppsV1Api:
    return client.AppsV1Api(_binding().api_client)


def _get_batch_v1_api_client() -> client.BatchV1Api:
    return client.BatchV1Api(_binding().api_client)


def _get_autoscaling_v2_api_client() -> client.AutoscalingV2Api:
    return client.AutoscalingV2Api(_binding().api_client)


def _get_discovery_v1_api_client() -> client.DiscoveryV1Api:
    return client.DiscoveryV1Api(_binding().api_client)


def _to_whole_seconds(td: datetime.timedelta) -> datetime.timedelta:
    """Truncate a duration to whole seconds, flooring at zero.

    Both adjustments keep the value inside the grammar our own JSON Schema
    declares for it (see :data:`Duration`).

    Truncation: every age here is ``now() - <k8s timestamp>``, and Kubernetes
    timestamps are whole-second, so the sub-second part of an age is nothing but
    the microseconds of the ``now()`` that happened to compute it - noise, and
    identical across every row of a single response. This matches the rule
    ``MockState``'s replay clock already follows for the same reason.

    Flooring: a cluster whose clock is ahead of ours yields a small negative age,
    which pydantic renders as ``-PT5S``. A duration has no sign in this grammar,
    so clamp it; at this granularity "just created" is the honest reading anyway.
    """
    return datetime.timedelta(seconds=max(int(td.total_seconds()), 0))


#: A ``timedelta`` field on a response model.
#:
#: Pydantic's default JSON Schema for ``timedelta`` is
#: ``{"type": "string", "format": "duration"}``, but its default *serialization*
#: emits fractional seconds (``"P2DT5M27.978616S"``) whenever the value carries
#: microseconds. JSON Schema's ``duration`` format references RFC 3339 Appendix A,
#: whose grammar admits only integer components - so pydantic's own schema and its
#: own serializer disagree, and an MCP client that validates structured output
#: against the declared schema (ajv does; Python's ``jsonschema`` skips ``duration``
#: unless the optional ``isoduration`` extra is installed) rejects the tool call
#: outright, on every row, before the caller sees any data.
#:
#: Normalizing the value rather than the schema keeps ``format: "duration"``
#: accurate and keeps direct Python callers' fields real ``timedelta`` objects.
#:
#: A ``Duration`` is an *age*: time from some moment until now. Replay of a
#: capture relies on that - it advances every age by the time since the server
#: started. A fixed span between two moments must be an :data:`Interval` instead.
Duration = Annotated[datetime.timedelta, AfterValidator(_to_whole_seconds)]


class _IntervalMarker:
    """``Annotated`` metadata that tells the capture codec a duration is an interval."""

    def __repr__(self) -> str:
        return "INTERVAL"


INTERVAL = _IntervalMarker()

#: A ``timedelta`` field that is a fixed span between two moments (how long a
#: container ran), as opposed to an age (:data:`Duration`).
#:
#: Same value normalization and schema as ``Duration``; the difference matters only
#: to capture replay. ``MockState`` advances an age by the time since the server
#: started, so an 8-day-old deployment is 8 days and 3 hours old three hours into
#: a replay. An interval must not move: a container that ran 60s ran 60s however
#: long the replay has been up. Declaring a span as a plain ``Duration`` made it
#: grow on every replay, beside timestamps that still said otherwise.
Interval = Annotated[datetime.timedelta, AfterValidator(_to_whole_seconds), INTERVAL]


class NamespaceSummary(BaseModel):
    """Summary information about a namespace, like returned by `kubectl get namespace`"""
    name: str
    status: str
    age: Duration


def get_namespaces() -> list[NamespaceSummary]:
    """Namespaces with status and age, like `kubectl get namespace`."""
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


# ---------------------------------------------------------------------------
# Notes (issue #20)
# ---------------------------------------------------------------------------
#
# A tool's description is context on every turn, so it carries at most one
# warning. Warnings that apply to particular results travel with them instead,
# in a `notes` list that is empty unless one applies. Each model derives its own
# notes from its fields in a validator, so a replayed capture - including one
# recorded before notes existed - gets the same notes as a live call; captures
# don't store them (`mock_state.DERIVED_FIELDS`).

NOTE_NODE_READY = (
    "Ready's time changes only when its status does: a restart that never went "
    "NotReady doesn't reset it, so it isn't uptime. For the node's last start, see "
    "its Starting/Rebooted events or kube-proxy's started_at.")
NOTE_EVENT_COUNT = (
    "count covers first_seen to last_seen. Updates are rate-limited (after 25, at "
    "most one per 5 minutes per object and type), so both can lag; first_seen is "
    "where this record starts, not necessarily when the problem did.")
NOTE_EVENT_BACKOFF = (
    "BackOff repeats while a container waits, so this count is several times the "
    "number of restarts; count restarts from restart_count or Created events.")
NOTE_POD_LAST_RESTART = (
    "last_restart is when a container last terminated (in CrashLoopBackOff, its "
    "last crash), not when it was restarted.")
NOTE_CONTAINER_RAN_FOR = (
    "last_state.ran_for is how long that instance ran; restarts are further apart, "
    "by the back-off between its finished_at and the next start.")
NOTE_CONTAINER_WAITING = (
    "Waiting with no running instance: its logs, with previous=False or True, are "
    "the last terminated instance's.")
NOTE_HPA_AT_MAX = (
    "At maxReplicas: it can't add replicas, whatever its metrics say. See the "
    "ScalingLimited condition.")
NOTE_NO_READY_ENDPOINTS = (
    "No ready endpoints: traffic sent to this Service has no pod to reach.")
NOTE_NO_READING = (
    "No reading: the container isn't running, or started within about one "
    "metrics-server scrape interval.")
NOTE_SHORT_WINDOW = (
    "A sample averages a short window: a spike that ends in an OOM kill usually "
    "never appears in one.")
NOTE_WORKING_SET = (
    "Memory is the working set: memory in use plus recently used file cache, which "
    "the kernel reclaims before an OOM kill. Near the limit is common; it isn't "
    "proof a kill is coming.")
NOTE_DAEMONSET_SELECTOR = (
    "No nodeSelector, but affinity or tolerations may still limit its nodes; "
    "desired_number_scheduled is the actual count.")


class NodeSummary(BaseModel):
    """A summary of a node's status like returned by `kubectl get nodes -o wide`,
    augmented with the capacity/allocatable/conditions/taints/labels detail that
    `kubectl describe node` shows (useful for reconstructing a pods-per-node
    capacity model)."""
    name: str
    status: str
    roles: list[str]
    age: Duration
    version: str
    internal_ip: Optional[str] = None
    external_ip: Optional[str] = None
    os_image: Optional[str] = None
    kernel_version: Optional[str] = None
    container_runtime: Optional[str] = None
    capacity: dict[str, str] = Field(default_factory=dict)
    allocatable: dict[str, str] = Field(default_factory=dict)
    conditions: dict[str, str] = Field(default_factory=dict)
    conditions_since: dict[str, Duration] = Field(default_factory=dict)
    taints: list[str] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_notes(self) -> "NodeSummary":
        self.notes = [NOTE_NODE_READY] if "Ready" in self.conditions_since else []
        return self

def get_node_summaries() -> list[NodeSummary]:
    """Nodes, like `kubectl get nodes -o wide`, plus capacity, allocatable,
    conditions and the time since each last changed (conditions_since), taints and
    labels. Each node's notes say what its Ready time does and doesn't show."""
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
        conditions_since = {
            c.type: current_time_utc - c.last_transition_time
            for c in (node.status.conditions or [])
            if getattr(c, "last_transition_time", None)
        } if node.status else {}

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
            conditions_since=conditions_since,
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
    print(f"{'NAME':<32} {'STATUS':<12} {'ROLES':<20} {'AGE':<12} {'READY-SINCE':<12} {'VERSION':<16} {'INTERNAL-IP':<16} {'EXTERNAL-IP':<16} {'OS-IMAGE':<32} {'KERNEL-VERSION':<16} {'CONTAINER-RUNTIME':<20}")
    for node in nodes:
        age = _format_timedelta(node.age)
        ready_since = node.conditions_since.get("Ready")
        ready_since = _format_timedelta(ready_since) if ready_since is not None else "<unknown>"
        roles_str = ",".join(node.roles) if node.roles and node.roles != ["<none>"] else "<none>"
        internal_ip = node.internal_ip if node.internal_ip else "<none>"
        external_ip = node.external_ip if node.external_ip else "<none>"
        os_image = node.os_image if node.os_image else "<unknown>"
        kernel_version = node.kernel_version if node.kernel_version else "<unknown>"
        container_runtime = node.container_runtime if node.container_runtime else "<unknown>"
        
        print(f"{node.name:<32} {node.status:<12} {roles_str:<20} {age:<12} {ready_since:<12} {node.version:<16} {internal_ip:<16} {external_ip:<16} {os_image:<32} {kernel_version:<16} {container_runtime:<20}")
    

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
    last_restart: Optional[Duration]
    age: Duration
    ip: Optional[str] = None
    node: Optional[str] = None
    owner: Optional[str] = None  # controlling owner as "Kind/name"
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_notes(self) -> "PodSummary":
        # Only on a pod that isn't fully ready: after a node restart every pod has
        # restarts, and the note on all of them (38 of 38 on one cluster) is noise.
        failing = self.ready_containers < self.total_containers
        self.notes = [NOTE_POD_LAST_RESTART] if failing and self.restarts and self.last_restart else []
        return self


def _controller_owner(metadata) -> Optional[str]:
    """The object's controlling owner as "Kind/name", or None if it has none."""
    for ref in (getattr(metadata, "owner_references", None) or []):
        if getattr(ref, "controller", False):
            return f"{ref.kind}/{ref.name}"
    return None


def get_pod_summaries(namespace: Optional[str] = None) -> list[PodSummary]:
    """Pods, like `kubectl get pods -o wide`: ready containers, restarts,
    last_restart (time since a container last terminated), age, IP, node, and the
    controlling owner ("ReplicaSet/x", "StatefulSet/x", "DaemonSet/x", "Job/x"; a
    Deployment's pods name its ReplicaSet, whose owner_deployment names the
    Deployment). Group pods by owner, not by name. namespace: one, or all."""
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
            node=node_name,
            owner=_controller_owner(pod.metadata),
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
    print(f"{'NAME':<32} {'NAMESPACE':<20} {'READY':<10} {'RESTARTS':<10} {'AGE':<12} {'IP':<16} {'NODE':<24} {'OWNER':<40}")
    for pod in pod_summaries:
        ready = f"{pod.ready_containers}/{pod.total_containers}"
        restarts = str(pod.restarts)
        age = _format_timedelta(pod.age)
        ip = pod.ip if pod.ip else "<none>"
        node = pod.node if pod.node else "<none>"
        owner = pod.owner if pod.owner else "<none>"
        print(f"{pod.name:<32} {pod.namespace:<20} {ready:<10} {restarts:<10} {age:<12} {ip:<16} {node:<24} {owner:<40}")

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
    last_seen: Optional[Duration]  # Time since event occurred
    #: Time since the first occurrence combined into this record.
    first_seen: Optional[Duration] = None
    #: How many occurrences Kubernetes combined into this record.
    count: Optional[int] = None
    type: str
    reason: str
    object: str
    message: str
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_notes(self) -> "EventSummary":
        notes = []
        if self.count is not None and self.count > 1:
            notes.append(NOTE_EVENT_COUNT)
        if self.reason == "BackOff":
            notes.append(NOTE_EVENT_BACKOFF)
        self.notes = notes
        return self


def _event_summary(event: Any, now: datetime.datetime, obj: str) -> EventSummary:
    """Convert a core/v1 Event, including one recorded through events.k8s.io/v1.

    The events.k8s.io API reaches the core/v1 view with the deprecated
    ``count``/``firstTimestamp``/``lastTimestamp`` unset. Its time is
    ``eventTime`` (the first observation) and any repetition is in ``series``, so
    those fill in when the deprecated fields are empty. A record with
    ``eventTime`` and no ``series`` was observed once.
    """
    series = getattr(event, "series", None)
    event_time = getattr(event, "event_time", None)
    last = (getattr(event, "last_timestamp", None)
            or (series.last_observed_time if series else None)
            or event_time)
    first = getattr(event, "first_timestamp", None) or event_time
    count = (getattr(event, "count", None)
             or (series.count if series else None)
             or (1 if event_time else None))
    return EventSummary(
        last_seen=(now - last) if last else None,
        first_seen=(now - first) if first else None,
        count=count,
        type=event.type or "",
        reason=event.reason or "",
        object=obj,
        message=event.message or "",
    )
 

def get_pod_events(pod_name: str, namespace: str = "default") -> list[EventSummary]:
    """Events for one pod; the same as get_events(involved_name=pod_name).
    Each has count, first_seen and last_seen, and notes on how far to trust them.
    Records expire about an hour after their last update."""
    global K8S
    if K8S is None:
        K8S = _get_api_client()
    logging.info(f"get_pod_events(pod_name={pod_name}, namespace={namespace})")
    field_selector = f"involvedObject.name={pod_name}"
    events = K8S.list_namespaced_event(namespace, field_selector=field_selector)
    now = datetime.datetime.now(datetime.timezone.utc)
    return [_event_summary(event, now, getattr(event.involved_object, 'name', pod_name))
            for event in events.items]


def print_pod_events(pod_name: str, namespace: str = "default") -> None:
    """
    Print the events for the specified pod, in a similar format to `kubectl get events`.
    """
    events = get_pod_events(pod_name, namespace)
    print(f"{'LAST SEEN':<12} {'COUNT':<7} {'TYPE':<10} {'REASON':<20} {'OBJECT':<32} {'MESSAGE':<40}")
    for event in events:
        last_seen = _format_timedelta(event.last_seen) if event.last_seen else "-"
        count = str(event.count) if event.count is not None else "-"
        message = (event.message[:37] + '...') if event.message and len(event.message) > 40 else event.message
        print(f"{last_seen:<12} {count:<7} {event.type:<10} {event.reason:<20} {event.object:<32} {message:<40}")

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
    #: How long this instance of the container ran: ``finished_at - started_at``.
    #:
    #: Derived rather than left to the caller because a crash-looping container
    #: has three durations that are easy to confuse, and an agent reading two
    #: absolute timestamps will reach for whichever number it computed last:
    #: how long an instance ran (this), how long its log covers (often far less
    #: -- a process can go silent long before it is killed), and how often it
    #: restarts (this plus the kubelet's back-off, named in the Waiting state's
    #: message). Only the first is in the API; stating it removes one subtraction
    #: that answers kept getting wrong.
    #:
    #: An :data:`Interval`, not a ``Duration``: it is a span, so capture replay
    #: must not advance it the way it advances ages.
    ran_for: Optional[Interval] = None

    @model_validator(mode="after")
    def _derive_ran_for(self) -> "ContainerStateTerminated":
        # Computed here rather than at the API boundary so a replayed capture
        # recorded before this field existed gets it too. Assigned inside a model
        # validator, so Interval's own validator does not run; apply its
        # normalization explicitly.
        if self.ran_for is None and self.started_at and self.finished_at:
            self.ran_for = _to_whole_seconds(self.finished_at - self.started_at)
        return self

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
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_notes(self) -> "ContainerStatus":
        notes = []
        if isinstance(self.state, ContainerStateWaiting) and self.restart_count:
            notes.append(NOTE_CONTAINER_WAITING)
        if isinstance(self.last_state, ContainerStateTerminated) and self.last_state.ran_for:
            notes.append(NOTE_CONTAINER_RAN_FOR)
        self.notes = notes
        return self


def get_pod_container_statuses(pod_name: str, namespace: str = "default") -> list[ContainerStatus]:
    """Each container's status in one pod: image, ready, restart count, the
    current state and the last terminated instance (last_state: exit code, reason,
    started_at, finished_at, ran_for), resources and mounts. Check this before
    reading logs: it says which instance a log call will return."""   
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
    """A pod's spec as the API returns it (snake_case keys): containers with
    image, command, args, env, resources and probes; volumes; node and scheduling.
    Secret-shaped values are redacted by the MCP server."""
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


def _decode_log_response(resp: Any) -> str:
    """Decode what ``read_namespaced_pod_log`` hands back into usable text.

    The log endpoint serves ``text/plain``, but the generated client declares the
    response type as ``str`` and runs the body through
    ``ApiClient.__deserialize_primitive``, which is ``str(data)``. Since
    kubernetes 36 the body arriving there is ``bytes`` (older clients decoded it
    in ``rest.py``), so that call yields ``repr(bytes)`` - one ``b'...'`` line
    with every newline as the two characters ``\\n``. That is useless as logs and
    silently breaks anything a caller greps for. Requesting the raw response and
    decoding here bypasses that deserialization entirely; status handling is
    unaffected, since the REST client raises ``ApiException`` for non-2xx
    responses whether or not the content was preloaded.

    ``errors="replace"`` rather than strict: container logs are arbitrary bytes,
    and ``limit_bytes`` truncates at a byte offset that can land inside a
    multi-byte character, so a ``UnicodeDecodeError`` here would fail a whole
    investigation over one bad byte.
    """
    # The raw urllib3 response carries the body on `.data`; a preloaded response
    # is already a str, and is passed through rather than decoded twice.
    data = None if resp is None else getattr(resp, "data", resp)
    if data is None:
        return ''
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data if isinstance(data, str) else str(data)


def _read_pod_log(pod_name: str, namespace: str = "default",
                  container_name: Optional[str] = None, tail: Optional[int] = None,
                  since_seconds: Optional[int] = None, previous: bool = False) -> str:
    """The container log as the API returns it, without notes. The capture
    stores this, so replay can add notes for the replayed state."""
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
            _preload_content=False,    # Raw response; see _decode_log_response()
            timestamps=True,           # Optional: Include timestamps
            tail_lines=tail if tail is not None else 1000,  # Default: last 1000 lines
            limit_bytes=1024*1024,     # Limit to 1MB to avoid memory issues
        )
        if since_seconds is not None:
            log_kwargs["since_seconds"] = since_seconds
        if previous:
            log_kwargs["previous"] = True
        resp = K8S.read_namespaced_pod_log(**log_kwargs)

        # A single string containing all logs, with real newlines. An empty log
        # is '', never None - callers treat the two the same.
        return _decode_log_response(resp)
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching logs: {e}") from e
    except Exception as e:
        raise K8sApiError(f"An unexpected error occurred: {e}") from e


#: Marks a line k8stools adds to the top of a log; it is not the container's output.
LOG_NOTE_PREFIX = "[k8stools] note: "

_KUBELET_LOG_ERROR = "unable to retrieve container logs for"


def _log_notes(status: Optional["ContainerStatus"], text: str) -> list[str]:
    """Warnings for a log, from the container's status at the time of the call."""
    notes = []
    if text.lstrip().startswith(_KUBELET_LOG_ERROR):
        notes.append("This is the kubelet's message, not container output: that "
                     "instance's log file is gone.")
    if status is not None and isinstance(status.state, ContainerStateWaiting) \
            and status.restart_count:
        notes.append(f"The container is waiting ({status.state.reason}) with no running "
                     "instance, so this is its last terminated instance's log, for "
                     "previous=False and previous=True alike.")
    return notes


def _with_log_notes(text: str, statuses: list["ContainerStatus"],
                    container_name: Optional[str]) -> str:
    """Prefix ``text`` with a marked line per note (none when nothing applies)."""
    status = next((s for s in statuses if s.container_name == container_name),
                  statuses[0] if statuses and container_name is None else None)
    notes = _log_notes(status, text)
    return "".join(f"{LOG_NOTE_PREFIX}{n}\n" for n in notes) + text


def get_logs_for_pod_and_container(pod_name: str, namespace: str = "default",
                                   container_name: Optional[str] = None,
                                   tail: Optional[int] = None,
                                   since_seconds: Optional[int] = None,
                                   previous: bool = False) -> Optional[str]:
    """A container's log, with timestamps (like `kubectl logs --timestamps`).

    pod_name, namespace: the pod. container_name: defaults to the first container.
    tail: lines from the end (default 1000). since_seconds: only newer lines.
    previous: the previous, terminated instance - where a crash usually shows.
    Only one previous instance is kept.

    Lines starting "[k8stools] note:" are added by this tool, not the container:
    e.g. that a waiting container returns its last instance for both values of
    previous, or that the text is the kubelet's error, not a log.
    """
    text = _read_pod_log(pod_name, namespace, container_name, tail=tail,
                         since_seconds=since_seconds, previous=previous)
    try:
        statuses = get_pod_container_statuses(pod_name, namespace)
    except Exception as e:  # notes are best-effort; the log itself was readable
        logging.debug(f"get_logs_for_pod_and_container: no status for notes: {e}")
        statuses = []
    return _with_log_notes(text, statuses, container_name)


class DeploymentSummary(BaseModel):
    """A summary of a deployment's status like returned by `kubectl get deployments`"""
    name: str
    namespace: str
    total_replicas: int
    ready_replicas: int
    up_to_date_relicas: int
    available_replicas: int
    age: Duration

def get_deployment_summaries(namespace: Optional[str] = None) -> list[DeploymentSummary]:
    """Deployments, like `kubectl get deployments`: desired, ready, up-to-date
    and available replicas, and age. namespace: one, or all if omitted."""
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
    #: None when no Deployment owns this replica set.
    owner_deployment: Optional[str]
    #: From the `deployment.kubernetes.io/revision` annotation, which only the
    #: Deployment controller writes; None for a replica set no Deployment created.
    revision: Optional[int]
    desired_replicas: int
    current_replicas: int
    ready_replicas: int
    images: list[str]
    age: Duration


def get_replicaset_summaries(namespace: Optional[str] = None,
                             deployment: Optional[str] = None) -> list[ReplicaSetSummary]:
    """ReplicaSets with each one's deployment revision and images, oldest
    revision first, so for one deployment the last entry is current. deployment:
    only that deployment's. get_workload_history compares revisions in full."""
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


# ---------------------------------------------------------------------------
# Workload change history (issue #11)
# ---------------------------------------------------------------------------

class TemplateChange(BaseModel):
    """One difference between a revision's pod template and the previous one's."""
    #: Where in the pod template, e.g. "containers[ad].image",
    #: "containers[ad].resources.limits.memory", "containers[ad].env[LOG_LEVEL]",
    #: "volumes[config]", "node_selector[disktype]",
    #: "metadata.annotations[kubectl.kubernetes.io/restartedAt]".
    field: str
    change: Literal["added", "removed", "changed"]
    #: Values are shown only where they are safe and short: never for env vars,
    #: and not for structured fields outside the ones compared explicitly.
    before: Optional[str] = None
    after: Optional[str] = None


class WorkloadRevision(BaseModel):
    """One retained revision of a workload's pod template."""
    revision: Optional[int]
    #: "ReplicaSet/<name>" for a Deployment, "ControllerRevision/<name>" otherwise.
    source: str
    age: Duration
    current: bool
    reused: bool = False
    images: list[str] = Field(default_factory=list)
    #: The revision this one is compared with (the previous retained one), or
    #: None for the oldest retained revision, which has no changes listed.
    compared_with: Optional[int] = None
    changes: list[TemplateChange] = Field(default_factory=list)
    metadata_changes: list[TemplateChange] = Field(default_factory=list)
    rollout_restart: bool = False


class ConfigReference(BaseModel):
    """A ConfigMap or Secret the workload's current pod template refers to."""
    kind: Literal["ConfigMap", "Secret"]
    name: str
    #: How it is used: "env" (configMapKeyRef/secretKeyRef), "envFrom",
    #: "volume", "volume (subPath)", "imagePullSecrets".
    used_as: list[str] = Field(default_factory=list)
    #: ConfigMaps only: whether it exists. None for Secrets, which are not read.
    exists: Optional[bool] = None
    age: Optional[Duration] = None
    #: ConfigMaps only: time since the latest write recorded in its
    #: managedFields, by any client, including label-only changes.
    last_written: Optional[Duration] = None


class WorkloadHistory(BaseModel):
    """A workload's retained pod-template revisions and the config it references."""
    kind: str
    name: str
    namespace: str
    revisions: list[WorkloadRevision] = Field(default_factory=list)
    config: list[ConfigReference] = Field(default_factory=list)
    #: False when this was rebuilt from a capture that predates template
    #: history: revisions then show images only.
    complete: bool = True
    limits: list[str] = Field(default_factory=list)


_WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")

#: Labels the controllers stamp on every template; their change is not news.
_IGNORED_TEMPLATE_LABELS = {"pod-template-hash", "controller-revision-hash",
                            "pod-template-generation"}

_RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"

#: Container fields compared explicitly; anything else that differs is named.
_CONTAINER_FIELDS = {"name", "image", "resources", "command", "args", "env", "envFrom",
                     "volumeMounts", "livenessProbe", "readinessProbe", "startupProbe"}
_POD_FIELDS = {"containers", "initContainers", "volumes", "nodeSelector", "tolerations"}


def _api_dict(obj) -> dict:
    """A kubernetes model object (or a dict already) as the API's JSON dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    return _binding().api_client.sanitize_for_serialization(obj)


def _short(value) -> Optional[str]:
    """A scalar as a string; structured values as compact, key-sorted JSON."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _is_scalar(value) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _compare_maps(prefix: str, before: dict, after: dict, show_values: bool,
                  out: list[TemplateChange]) -> None:
    for key in sorted(set(before) | set(after)):
        b, a = before.get(key), after.get(key)
        if b == a:
            continue
        change = "added" if key not in before else "removed" if key not in after else "changed"
        out.append(TemplateChange(field=f"{prefix}[{key}]", change=change,
                                  before=_short(b) if show_values else None,
                                  after=_short(a) if show_values else None))


def _compare_other_fields(prefix: str, before: dict, after: dict, handled: set,
                          out: list[TemplateChange]) -> None:
    """Name any other changed field; show values only for plain scalars."""
    for key in sorted((set(before) | set(after)) - handled):
        b, a = before.get(key), after.get(key)
        if b == a:
            continue
        change = "added" if key not in before else "removed" if key not in after else "changed"
        scalar = _is_scalar(b) and _is_scalar(a)
        out.append(TemplateChange(field=f"{prefix}.{key}", change=change,
                                  before=_short(b) if scalar else None,
                                  after=_short(a) if scalar else None))


def _env_from_entries(container: dict) -> dict[str, dict]:
    entries = {}
    for item in container.get("envFrom") or []:
        for source, kind in (("configMapRef", "configMap"), ("secretRef", "secret")):
            if item.get(source):
                label = f"{kind}:{item[source].get('name')}"
                if item.get("prefix"):
                    label += f" (prefix {item['prefix']})"
                entries[label] = item
    return entries


def _compare_containers(group: str, before: list, after: list,
                        out: list[TemplateChange]) -> None:
    old = {c.get("name"): c for c in before or []}
    new = {c.get("name"): c for c in after or []}
    for name in [n for n in old if n not in new]:
        out.append(TemplateChange(field=f"{group}[{name}]", change="removed",
                                  before=old[name].get("image")))
    for name in new:
        prefix = f"{group}[{name}]"
        if name not in old:
            out.append(TemplateChange(field=prefix, change="added",
                                      after=new[name].get("image")))
            continue
        b, a = old[name], new[name]
        if b.get("image") != a.get("image"):
            out.append(TemplateChange(field=f"{prefix}.image", change="changed",
                                      before=b.get("image"), after=a.get("image")))
        for part in ("requests", "limits"):
            _compare_maps(f"{prefix}.resources.{part}",
                          (b.get("resources") or {}).get(part) or {},
                          (a.get("resources") or {}).get(part) or {}, True, out)
        for key in ("command", "args", "livenessProbe", "readinessProbe", "startupProbe"):
            if b.get(key) != a.get(key):
                change = "added" if b.get(key) is None else \
                    "removed" if a.get(key) is None else "changed"
                out.append(TemplateChange(field=f"{prefix}.{key}", change=change,
                                          before=_short(b.get(key)), after=_short(a.get(key))))
        # Env vars by name, never by position; names only, never values.
        _compare_maps(f"{prefix}.env",
                      {e.get("name"): e for e in b.get("env") or []},
                      {e.get("name"): e for e in a.get("env") or []}, False, out)
        before_from, after_from = _env_from_entries(b), _env_from_entries(a)
        for label in sorted(set(before_from) ^ set(after_from)):
            out.append(TemplateChange(field=f"{prefix}.envFrom", change=(
                "added" if label in after_from else "removed"),
                before=label if label in before_from else None,
                after=label if label in after_from else None))
        _compare_maps(f"{prefix}.volumeMounts",
                      {m.get("mountPath"): _mount_summary(m) for m in b.get("volumeMounts") or []},
                      {m.get("mountPath"): _mount_summary(m) for m in a.get("volumeMounts") or []},
                      True, out)
        _compare_other_fields(prefix, b, a, _CONTAINER_FIELDS, out)


def _mount_summary(mount: dict) -> str:
    text = mount.get("name") or ""
    if mount.get("subPath"):
        text += f" subPath={mount['subPath']}"
    if mount.get("readOnly"):
        text += " (read-only)"
    return text


def _volume_summary(volume: dict) -> str:
    for source, value in volume.items():
        if source == "name" or not isinstance(value, dict):
            continue
        name = value.get("name") or value.get("secretName") or value.get("claimName") \
            or value.get("path")
        if source == "projected":
            parts = [f"{k}:{(v or {}).get('name')}" for s in value.get("sources") or []
                     for k, v in s.items() if isinstance(v, dict) and v.get("name")]
            return "projected" + (f" ({', '.join(parts)})" if parts else "")
        return f"{source}:{name}" if name else source
    return "unknown"


def _toleration_summary(t: dict) -> str:
    text = t.get("key") or "*"
    if t.get("operator") == "Exists":
        text += " exists"
    elif t.get("value") is not None:
        text += f"={t['value']}"
    if t.get("effect"):
        text += f":{t['effect']}"
    return text


def _template_changes(before: dict, after: dict) -> tuple[list[TemplateChange],
                                                          list[TemplateChange]]:
    """Compare two pod templates (API JSON dicts): (spec changes, metadata changes)."""
    changes: list[TemplateChange] = []
    b_spec, a_spec = before.get("spec") or {}, after.get("spec") or {}
    _compare_containers("containers", b_spec.get("containers"), a_spec.get("containers"), changes)
    _compare_containers("init_containers", b_spec.get("initContainers"),
                        a_spec.get("initContainers"), changes)
    _compare_maps("volumes",
                  {v.get("name"): _volume_summary(v) for v in b_spec.get("volumes") or []},
                  {v.get("name"): _volume_summary(v) for v in a_spec.get("volumes") or []},
                  True, changes)
    _compare_maps("node_selector", b_spec.get("nodeSelector") or {},
                  a_spec.get("nodeSelector") or {}, True, changes)
    b_tol = {_toleration_summary(t) for t in b_spec.get("tolerations") or []}
    a_tol = {_toleration_summary(t) for t in a_spec.get("tolerations") or []}
    for t in sorted(b_tol ^ a_tol):
        changes.append(TemplateChange(field="tolerations",
                                      change="added" if t in a_tol else "removed",
                                      before=t if t in b_tol else None,
                                      after=t if t in a_tol else None))
    _compare_other_fields("spec", b_spec, a_spec, _POD_FIELDS, changes)

    metadata: list[TemplateChange] = []
    b_meta, a_meta = before.get("metadata") or {}, after.get("metadata") or {}
    _compare_maps("metadata.labels",
                  {k: v for k, v in (b_meta.get("labels") or {}).items()
                   if k not in _IGNORED_TEMPLATE_LABELS},
                  {k: v for k, v in (a_meta.get("labels") or {}).items()
                   if k not in _IGNORED_TEMPLATE_LABELS}, True, metadata)
    _compare_maps("metadata.annotations", b_meta.get("annotations") or {},
                  a_meta.get("annotations") or {}, True, metadata)
    return changes, metadata


def _template_images(template: dict) -> list[str]:
    return [c.get("image") for c in (template.get("spec") or {}).get("containers") or []
            if c.get("image")]


def _config_references(template: dict) -> list[ConfigReference]:
    """Every ConfigMap and Secret a pod template names, and how it uses each."""
    uses: dict[tuple[str, str], list[str]] = {}

    def use(kind, name, how):
        if name:
            seen = uses.setdefault((kind, name), [])
            if how not in seen:
                seen.append(how)

    spec = template.get("spec") or {}
    containers = (spec.get("containers") or []) + (spec.get("initContainers") or [])
    sub_path_volumes = {m.get("name") for c in containers for m in c.get("volumeMounts") or []
                        if m.get("subPath") or m.get("subPathExpr")}
    for c in containers:
        for e in c.get("env") or []:
            source = e.get("valueFrom") or {}
            if source.get("configMapKeyRef"):
                use("ConfigMap", source["configMapKeyRef"].get("name"), "env")
            if source.get("secretKeyRef"):
                use("Secret", source["secretKeyRef"].get("name"), "env")
        for item in c.get("envFrom") or []:
            if item.get("configMapRef"):
                use("ConfigMap", item["configMapRef"].get("name"), "envFrom")
            if item.get("secretRef"):
                use("Secret", item["secretRef"].get("name"), "envFrom")
    for v in spec.get("volumes") or []:
        how = "volume (subPath)" if v.get("name") in sub_path_volumes else "volume"
        if v.get("configMap"):
            use("ConfigMap", v["configMap"].get("name"), how)
        if v.get("secret"):
            use("Secret", v["secret"].get("secretName"), how)
        for source in (v.get("projected") or {}).get("sources") or []:
            if source.get("configMap"):
                use("ConfigMap", source["configMap"].get("name"), how)
            if source.get("secret"):
                use("Secret", source["secret"].get("name"), how)
    for ref in spec.get("imagePullSecrets") or []:
        use("Secret", ref.get("name"), "imagePullSecrets")
    return [ConfigReference(kind=kind, name=name, used_as=how)
            for (kind, name), how in sorted(uses.items())]


def _history_limits(kind: str, history_limit: Optional[int]) -> list[str]:
    source = "ReplicaSet" if kind == "Deployment" else "ControllerRevision"
    return [
        "Only pod-template changes are recorded. Replica counts, autoscaling and the "
        "update strategy are not, and neither are changes outside the cluster "
        "(feature flags, traffic, dependencies).",
        f"Kubernetes keeps {history_limit if history_limit is not None else 10} old "
        "revisions (revisionHistoryLimit); older ones are deleted, so the oldest listed "
        "revision is not necessarily the first.",
        f"A revision's age is when its {source} was created. A rollback reuses an "
        "older one (reused=true), which then went live later than its age says.",
        "Env var values are never shown, only names. Secret contents and change "
        "times are not read.",
        "A ConfigMap's last_written is its latest write of any kind, including "
        "label-only changes such as a Helm upgrade: it does not show that the data "
        "changed.",
        "A ConfigMap write reaches a running pod by how it is used: env and envFrom "
        "only at the container's next start; a mounted volume within about a minute; "
        "a subPath mount never. Compare last_written with the containers' started_at.",
    ]


def _read_workload(kind: str, name: str, namespace: str):
    readers = {"Deployment": APPS_V1_API.read_namespaced_deployment,
               "StatefulSet": APPS_V1_API.read_namespaced_stateful_set,
               "DaemonSet": APPS_V1_API.read_namespaced_daemon_set}
    try:
        return readers[kind](name=name, namespace=namespace)
    except client.ApiException as e:
        if e.status == 404:
            raise K8sApiError(
                f"{kind} '{name}' not found in namespace '{namespace}'.") from e
        raise K8sApiError(f"Error fetching {kind} '{name}': {e}") from e


def _owned_by(obj, kind: str, name: str) -> bool:
    return any(getattr(ref, "controller", False) and ref.kind == kind and ref.name == name
               for ref in (obj.metadata.owner_references or []))


def _raw_revisions(kind: str, name: str, namespace: str):
    """(revision, source, created, reused, template dict) for each retained revision."""
    try:
        if kind == "Deployment":
            items = APPS_V1_API.list_namespaced_replica_set(namespace=namespace).items
        else:
            items = APPS_V1_API.list_namespaced_controller_revision(namespace=namespace).items
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching revisions of {kind} '{name}': {e}") from e
    revisions = []
    for item in items:
        if not _owned_by(item, kind, name):
            continue
        if kind == "Deployment":
            annotations = item.metadata.annotations or {}
            try:
                revision = int(annotations.get("deployment.kubernetes.io/revision"))
            except (TypeError, ValueError):
                revision = None
            reused = "deployment.kubernetes.io/revision-history" in annotations
            template = _api_dict(item.spec.template if item.spec else None)
            source = f"ReplicaSet/{item.metadata.name}"
        else:
            revision = item.revision
            reused = False
            data = item.data if isinstance(item.data, dict) else {}
            template = dict((data.get("spec") or {}).get("template") or {})
            template.pop("$patch", None)
            source = f"ControllerRevision/{item.metadata.name}"
        revisions.append((revision, source, item.metadata.creation_timestamp, reused, template))
    revisions.sort(key=lambda r: r[0] if r[0] is not None else -1)
    return revisions


def _configmap_reference(ref: ConfigReference, namespace: str,
                         now: datetime.datetime) -> ConfigReference:
    if ref.kind != "ConfigMap":
        return ref
    try:
        cm = K8S.read_namespaced_config_map(name=ref.name, namespace=namespace)
    except client.ApiException as e:
        if e.status == 404:
            return ref.model_copy(update={"exists": False})
        raise K8sApiError(f"Error fetching ConfigMap '{ref.name}': {e}") from e
    times = [m.time for m in (cm.metadata.managed_fields or []) if getattr(m, "time", None)]
    created = cm.metadata.creation_timestamp
    return ref.model_copy(update={
        "exists": True,
        "age": now - created if created else None,
        "last_written": now - max(times) if times else None,
    })


def get_workload_history(name: str, namespace: str = "default",
                         kind: str = "Deployment") -> WorkloadHistory:
    """What changed in a workload, and when: its retained revisions, newest
    first, each compared with the one before (images, resources, probes,
    command/args, env var names, volumes, scheduling), plus the ConfigMaps and
    Secrets it references. kind: "Deployment" (default), "StatefulSet" or
    "DaemonSet". Ask early: a recent change is the first suspect, and a long
    unchanged workload is a finding too. The result's limits list what history
    can't show."""
    global K8S, APPS_V1_API
    if kind not in _WORKLOAD_KINDS:
        raise K8sApiError(f"Unsupported kind '{kind}': expected one of {', '.join(_WORKLOAD_KINDS)}.")
    if APPS_V1_API is None:
        APPS_V1_API = _get_apps_v1_api_client()
    if K8S is None:
        K8S = _get_api_client()
    logging.info(f"get_workload_history(name={name}, namespace={namespace}, kind={kind})")

    workload = _read_workload(kind, name, namespace)
    raw = _raw_revisions(kind, name, namespace)
    now = datetime.datetime.now(datetime.timezone.utc)

    revisions: list[WorkloadRevision] = []
    for i, (number, source, created, reused, template) in enumerate(raw):
        changes: list[TemplateChange] = []
        metadata: list[TemplateChange] = []
        compared_with = None
        if i > 0:
            compared_with = raw[i - 1][0]
            changes, metadata = _template_changes(raw[i - 1][4], template)
        revisions.append(WorkloadRevision(
            revision=number, source=source,
            age=now - created if created else datetime.timedelta(0),
            current=(i == len(raw) - 1), reused=reused,
            images=_template_images(template), compared_with=compared_with,
            changes=changes, metadata_changes=metadata,
            rollout_restart=any(c.field == f"metadata.annotations[{_RESTARTED_AT}]"
                                for c in metadata),
        ))
    revisions.reverse()

    spec = workload.spec
    current_template = _api_dict(spec.template if spec else None)
    config = [_configmap_reference(ref, namespace, now)
              for ref in _config_references(current_template)]
    history_limit = getattr(spec, "revision_history_limit", None) if spec else None
    return WorkloadHistory(kind=kind, name=name, namespace=namespace,
                           revisions=revisions, config=config, complete=True,
                           limits=_history_limits(kind, history_limit))


def print_workload_history(name: str, namespace: str = "default",
                           kind: str = "Deployment") -> None:
    """Calls get_workload_history and prints the revisions and config references."""
    history = get_workload_history(name, namespace, kind)
    print(f"{history.kind}/{history.name} in {history.namespace}"
          + ("" if history.complete else " (images only: capture predates template history)"))
    print(f"{'REV':<5} {'AGE':<10} {'SOURCE':<48} IMAGES")
    for r in history.revisions:
        marks = (" (current)" if r.current else "") + (" (reused)" if r.reused else "") \
            + (" (rollout restart)" if r.rollout_restart else "")
        revision = str(r.revision) if r.revision is not None else "-"
        print(f"{revision:<5} {_format_timedelta(r.age):<10} {r.source:<48} "
              f"{', '.join(r.images)}{marks}")
        for c in r.changes + r.metadata_changes:
            values = f": {c.before or '-'} -> {c.after or '-'}" if c.before or c.after else ""
            print(f"      {c.change:<8} {c.field}{values}")
    if history.config:
        print(f"{'CONFIG':<36} {'USED AS':<28} {'AGE':<10} LAST WRITTEN")
        for ref in history.config:
            if ref.kind == "Secret":
                age, written = "-", "(not read)"
            elif ref.exists is False:
                age, written = "-", "(missing)"
            else:
                age, written = _format_timedelta(ref.age), _format_timedelta(ref.last_written)
            print(f"{ref.kind + '/' + ref.name:<36} {', '.join(ref.used_as):<28} {age:<10} {written}")


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
    age: Duration
    selector: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    #: Ready and not-ready backends, from its EndpointSlices. None when unknown:
    #: an ExternalName Service, EndpointSlices not readable, or a capture taken
    #: before these were recorded.
    ready_endpoints: Optional[int] = None
    not_ready_endpoints: Optional[int] = None

def get_service_summaries(namespace: Optional[str] = None) -> list[ServiceSummary]:
    """Services, like `kubectl get services`: type, cluster and external IP,
    ports, selector, labels, annotations, age, and how many ready and not-ready
    endpoints (backends) each has. namespace: one, or all if omitted."""
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
    # One more list call, so "does this Service have backends?" needs no second
    # tool. Older roles may not grant EndpointSlices: counts are then unknown.
    try:
        endpoint_counts = {(e.namespace, e.service): (e.ready, e.not_ready)
                           for e in get_endpoint_summaries(namespace)}
    except K8sApiError as e:
        logging.warning(f"get_service_summaries: endpoint counts unavailable: {e}")
        endpoint_counts = None

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

        counts = endpoint_counts.get((service_namespace, service_name), (0, 0)) \
            if endpoint_counts is not None and service_type != "ExternalName" else (None, None)
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
            ready_endpoints=counts[0],
            not_ready_endpoints=counts[1],
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


class EndpointAddress(BaseModel):
    """One backend of a Service, from its EndpointSlices."""
    ip: str
    #: Ready to receive traffic (an unset condition means ready).
    ready: bool
    #: Serving, even if terminating (an unset condition takes ready's value).
    serving: bool
    terminating: bool = False
    #: The backing pod's name, when the endpoint targets a pod.
    pod: Optional[str] = None
    node: Optional[str] = None


class EndpointSummary(BaseModel):
    """A Service's backends, merged from its EndpointSlices."""
    service: str
    namespace: str
    ports: list[PortInfo] = Field(default_factory=list)
    addresses: list[EndpointAddress] = Field(default_factory=list)
    ready: int = 0
    not_ready: int = 0
    terminating: int = 0
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_notes(self) -> "EndpointSummary":
        self.notes = [] if self.ready else [NOTE_NO_READY_ENDPOINTS]
        return self


_SERVICE_NAME_LABEL = "kubernetes.io/service-name"


def get_endpoint_summaries(namespace: Optional[str] = None) -> list[EndpointSummary]:
    """Each Service's backends, from its EndpointSlices: every address with its
    ready, serving and terminating state, pod and node, and counts of ready,
    not-ready and terminating ones. A Service with no ready endpoints has
    nowhere to send traffic. namespace: one, or all if omitted."""
    global DISCOVERY_V1_API
    if DISCOVERY_V1_API is None:
        DISCOVERY_V1_API = _get_discovery_v1_api_client()
    logging.info(f"get_endpoint_summaries(namespace={namespace})")
    try:
        if namespace:
            slices = DISCOVERY_V1_API.list_namespaced_endpoint_slice(namespace=namespace).items
        else:
            slices = DISCOVERY_V1_API.list_endpoint_slice_for_all_namespaces().items
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching endpoint slices: {e}") from e

    merged: dict[tuple[str, str], EndpointSummary] = {}
    seen: dict[tuple[str, str], set] = {}
    for sl in slices:
        labels = sl.metadata.labels or {}
        key = (sl.metadata.namespace, labels.get(_SERVICE_NAME_LABEL) or sl.metadata.name)
        summary = merged.setdefault(key, EndpointSummary(service=key[1], namespace=key[0]))
        for port in sl.ports or []:
            info = PortInfo(port=port.port or 0, protocol=port.protocol or "TCP")
            if info not in summary.ports:
                summary.ports.append(info)
        for ep in sl.endpoints or []:
            if not ep.addresses:
                continue
            ref = ep.target_ref
            pod = ref.name if ref is not None and ref.kind == "Pod" else None
            # A dual-stack Service has one slice per IP family: count a pod once.
            identity = pod or ep.addresses[0]
            if identity in seen.setdefault(key, set()):
                continue
            seen[key].add(identity)
            cond = ep.conditions
            ready = cond.ready if cond is not None and cond.ready is not None else True
            serving = cond.serving if cond is not None and cond.serving is not None else ready
            terminating = bool(cond.terminating) if cond is not None else False
            summary.addresses.append(EndpointAddress(
                ip=ep.addresses[0], ready=ready, serving=serving, terminating=terminating,
                pod=pod, node=ep.node_name))
    summaries = []
    for summary in merged.values():
        summary.ready = sum(a.ready for a in summary.addresses)
        summary.terminating = sum(a.terminating for a in summary.addresses)
        summary.not_ready = len(summary.addresses) - summary.ready - sum(
            a.terminating and not a.ready for a in summary.addresses)
        # Revalidate so notes see the final counts.
        summaries.append(EndpointSummary.model_validate(summary.model_dump()))
    summaries.sort(key=lambda e: (e.namespace, e.service))
    return summaries


def print_endpoint_summaries(namespace: Optional[str] = None) -> None:
    """Calls get_endpoint_summaries and prints the output to stdout, like
    `kubectl get endpoints`."""
    summaries = get_endpoint_summaries(namespace)
    print(f"{'SERVICE':<32} {'NAMESPACE':<16} {'READY':<6} {'NOT-READY':<10} "
          f"{'TERMINATING':<12} ENDPOINTS")
    for e in summaries:
        ports = ",".join(str(p.port) for p in e.ports)
        shown = ", ".join(f"{a.ip}:{ports}" for a in e.addresses[:3]) or "<none>"
        if len(e.addresses) > 3:
            shown += f" + {len(e.addresses) - 3} more"
        print(f"{e.service:<32} {e.namespace:<16} {e.ready:<6} {e.not_ready:<10} "
              f"{e.terminating:<12} {shown}")




class ConfigMapSummary(BaseModel):
    """A summary of a ConfigMap like returned by `kubectl get configmaps`."""
    name: str
    namespace: str
    key_count: int
    data_size: int  # total bytes across all values (data + binary_data)
    age: Duration


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
    """ConfigMaps with key count, data size and age, like `kubectl get
    configmaps`. get_configmap reads one's contents."""
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
    """One ConfigMap's contents: its data, and the names of its binary keys.
    Secret-shaped values are redacted by the MCP server."""
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
    age: Duration


def get_statefulset_summaries(namespace: Optional[str] = None) -> list[StatefulSetSummary]:
    """StatefulSets, like `kubectl get statefulsets`: desired, ready and
    current replicas, update strategy, governing service, age."""
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


class DaemonSetSummary(BaseModel):
    """A summary of a DaemonSet like returned by `kubectl get daemonsets -o wide`:
    the node agents (log shippers, CNI, kube-proxy, collectors) that run one pod
    per eligible node."""
    name: str
    namespace: str
    desired_number_scheduled: int
    current_number_scheduled: int
    number_ready: int
    updated_number_scheduled: int
    number_available: int
    number_misscheduled: int
    node_selector: dict[str, str] = Field(default_factory=dict)
    update_strategy: str
    images: list[str] = Field(default_factory=list)
    age: Duration
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_notes(self) -> "DaemonSetSummary":
        self.notes = [] if self.node_selector else [NOTE_DAEMONSET_SELECTOR]
        return self


def get_daemonset_summaries(namespace: Optional[str] = None) -> list[DaemonSetSummary]:
    """DaemonSets, like `kubectl get daemonsets -o wide`: desired, current,
    ready, up-to-date, available and misscheduled counts (one pod per targeted
    node), node selector, images, update strategy, age. A shortfall ("desired 5,
    ready 4") often points at one node."""
    global APPS_V1_API
    if APPS_V1_API is None:
        APPS_V1_API = _get_apps_v1_api_client()
    logging.info(f"get_daemonset_summaries(namespace={namespace})")
    try:
        if namespace:
            daemon_sets = APPS_V1_API.list_namespaced_daemon_set(namespace=namespace).items
        else:
            daemon_sets = APPS_V1_API.list_daemon_set_for_all_namespaces().items
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching daemon sets: {e}") from e

    now = datetime.datetime.now(datetime.timezone.utc)
    summaries: list[DaemonSetSummary] = []
    for ds in daemon_sets:
        spec = ds.spec
        status = ds.status

        def count(field: str) -> int:
            # The API omits a count that is zero.
            value = getattr(status, field, None) if status else None
            return value if value is not None else 0

        update_strategy = "Unknown"
        if spec and getattr(spec, "update_strategy", None) and spec.update_strategy.type:
            update_strategy = spec.update_strategy.type
        pod_spec = spec.template.spec if spec and spec.template else None
        node_selector = dict(pod_spec.node_selector) \
            if pod_spec and getattr(pod_spec, "node_selector", None) else {}
        images = [c.image for c in (pod_spec.containers or [])] if pod_spec else []
        age = datetime.timedelta(0)
        if ds.metadata.creation_timestamp:
            age = now - ds.metadata.creation_timestamp
        summaries.append(DaemonSetSummary(
            name=ds.metadata.name,
            namespace=ds.metadata.namespace,
            desired_number_scheduled=count("desired_number_scheduled"),
            current_number_scheduled=count("current_number_scheduled"),
            number_ready=count("number_ready"),
            updated_number_scheduled=count("updated_number_scheduled"),
            number_available=count("number_available"),
            number_misscheduled=count("number_misscheduled"),
            node_selector=node_selector,
            update_strategy=update_strategy,
            images=images,
            age=age,
        ))
    return summaries


def print_daemonset_summaries(namespace: Optional[str] = None) -> None:
    """Calls get_daemonset_summaries and prints the output to stdout, like
    `kubectl get daemonsets`."""
    summaries = get_daemonset_summaries(namespace)
    print(f"{'NAME':<32} {'NAMESPACE':<20} {'DESIRED':<8} {'CURRENT':<8} {'READY':<8} "
          f"{'UP-TO-DATE':<11} {'AVAILABLE':<10} {'NODE SELECTOR':<28} {'AGE':<12}")
    for ds in summaries:
        selector = ",".join(f"{k}={v}" for k, v in ds.node_selector.items()) or "<none>"
        age = _format_timedelta(ds.age)
        print(f"{ds.name:<32} {ds.namespace:<20} {ds.desired_number_scheduled:<8} "
              f"{ds.current_number_scheduled:<8} {ds.number_ready:<8} "
              f"{ds.updated_number_scheduled:<11} {ds.number_available:<10} "
              f"{selector:<28} {age:<12}")


class HpaMetric(BaseModel):
    """One metric an autoscaler scales on, with its target and current value."""
    #: Resource, ContainerResource, Pods, Object or External.
    type: str
    #: e.g. "cpu", "memory (container app)", "requests_per_second on Ingress/main".
    name: str
    #: "80%" (average utilization of requests), "500m (average)" or "10k".
    target: Optional[str] = None
    #: In the target's terms; None when the HPA has no current reading.
    current: Optional[str] = None


class HpaCondition(BaseModel):
    """An HPA condition: AbleToScale, ScalingActive or ScalingLimited."""
    type: str
    status: str
    reason: Optional[str] = None
    message: Optional[str] = None
    #: Time since its status last changed.
    since: Optional[Duration] = None


class HpaSummary(BaseModel):
    """A HorizontalPodAutoscaler, like `kubectl get hpa` and `kubectl describe hpa`."""
    name: str
    namespace: str
    #: "Kind/name", the same form as PodSummary.owner, e.g. "Deployment/cart".
    scale_target: str
    min_replicas: Optional[int] = None
    max_replicas: int
    current_replicas: int
    desired_replicas: int
    metrics: list[HpaMetric] = Field(default_factory=list)
    conditions: list[HpaCondition] = Field(default_factory=list)
    #: Time since it last changed the replica count.
    last_scale: Optional[Duration] = None
    age: Duration
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_notes(self) -> "HpaSummary":
        self.notes = [NOTE_HPA_AT_MAX] if self.current_replicas >= self.max_replicas else []
        return self


def _hpa_target(target) -> Optional[str]:
    if target is None:
        return None
    if getattr(target, "average_utilization", None) is not None:
        return f"{target.average_utilization}%"
    if getattr(target, "average_value", None) is not None:
        return f"{target.average_value} (average)"
    if getattr(target, "value", None) is not None:
        return str(target.value)
    return None


def _hpa_current(reading, target) -> Optional[str]:
    """A current reading in its target's own terms: utilization against a
    utilization target, average value against an average-value target."""
    if reading is None:
        return None
    if target is not None:
        for field, shown in (("average_utilization", "{}%"), ("average_value", "{} (average)"),
                             ("value", "{}")):
            if getattr(target, field, None) is not None and getattr(reading, field, None) is not None:
                return shown.format(getattr(reading, field))
    return _hpa_target(reading)


def _hpa_metric_key(metric) -> tuple[str, str]:
    """(type, display name) for a metric spec or status entry."""
    kind = metric.type
    if kind == "Resource" and metric.resource:
        return kind, metric.resource.name
    if kind == "ContainerResource" and metric.container_resource:
        return kind, f"{metric.container_resource.name} (container {metric.container_resource.container})"
    if kind == "Pods" and metric.pods:
        return kind, metric.pods.metric.name
    if kind == "Object" and metric.object:
        ref = metric.object.described_object
        return kind, f"{metric.object.metric.name} on {ref.kind}/{ref.name}"
    if kind == "External" and metric.external:
        return kind, metric.external.metric.name
    return kind, kind


def _hpa_source(metric):
    attr = {"Resource": "resource", "ContainerResource": "container_resource", "Pods": "pods",
            "Object": "object", "External": "external"}.get(metric.type)
    return getattr(metric, attr, None) if attr else None


def get_hpa_summaries(namespace: Optional[str] = None) -> list[HpaSummary]:
    """HorizontalPodAutoscalers (autoscaling/v2): scale target, min/max/current/
    desired replicas, each metric's current value against its target,
    conditions (AbleToScale, ScalingActive, ScalingLimited) with time since each
    changed, and time since it last scaled. namespace: one, or all if omitted.
    When it scaled: get_events(involved_kind="HorizontalPodAutoscaler")."""
    global AUTOSCALING_V2_API
    if AUTOSCALING_V2_API is None:
        AUTOSCALING_V2_API = _get_autoscaling_v2_api_client()
    logging.info(f"get_hpa_summaries(namespace={namespace})")
    try:
        if namespace:
            hpas = AUTOSCALING_V2_API.list_namespaced_horizontal_pod_autoscaler(namespace=namespace).items
        else:
            hpas = AUTOSCALING_V2_API.list_horizontal_pod_autoscaler_for_all_namespaces().items
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching horizontal pod autoscalers: {e}") from e

    now = datetime.datetime.now(datetime.timezone.utc)
    summaries: list[HpaSummary] = []
    for hpa in hpas:
        spec, status = hpa.spec, hpa.status
        current = {}
        for m in (getattr(status, "current_metrics", None) or []):
            source = _hpa_source(m)
            current[_hpa_metric_key(m)] = getattr(source, "current", None) if source else None
        metrics = []
        for m in (spec.metrics or []):
            key = _hpa_metric_key(m)
            source = _hpa_source(m)
            target = getattr(source, "target", None) if source else None
            metrics.append(HpaMetric(type=key[0], name=key[1], target=_hpa_target(target),
                                     current=_hpa_current(current.get(key), target)))
        conditions = [HpaCondition(
            type=c.type, status=c.status, reason=c.reason, message=c.message,
            since=now - c.last_transition_time if c.last_transition_time else None)
            for c in (getattr(status, "conditions", None) or [])]
        ref = spec.scale_target_ref
        last_scale = getattr(status, "last_scale_time", None)
        summaries.append(HpaSummary(
            name=hpa.metadata.name, namespace=hpa.metadata.namespace,
            scale_target=f"{ref.kind}/{ref.name}",
            min_replicas=spec.min_replicas, max_replicas=spec.max_replicas,
            current_replicas=getattr(status, "current_replicas", None) or 0,
            desired_replicas=getattr(status, "desired_replicas", None) or 0,
            metrics=metrics, conditions=conditions,
            last_scale=now - last_scale if last_scale else None,
            age=now - hpa.metadata.creation_timestamp if hpa.metadata.creation_timestamp
            else datetime.timedelta(0)))
    return summaries


def print_hpa_summaries(namespace: Optional[str] = None) -> None:
    """Calls get_hpa_summaries and prints the output to stdout, like `kubectl get hpa`."""
    summaries = get_hpa_summaries(namespace)
    print(f"{'NAME':<28} {'NAMESPACE':<16} {'REFERENCE':<32} {'TARGETS':<28} "
          f"{'MIN':<4} {'MAX':<4} {'REPLICAS':<9} {'AGE':<10}")
    for h in summaries:
        targets = ", ".join(f"{m.current or '<unknown>'}/{m.target}" for m in h.metrics) or "<none>"
        print(f"{h.name:<28} {h.namespace:<16} {h.scale_target:<32} {targets:<28} "
              f"{h.min_replicas if h.min_replicas is not None else 1:<4} {h.max_replicas:<4} "
              f"{h.current_replicas:<9} {_format_timedelta(h.age):<10}")


# ---------------------------------------------------------------------------
# Resource usage: metrics.k8s.io (issue #16)
# ---------------------------------------------------------------------------

_BINARY_SUFFIXES = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
_DECIMAL_SUFFIXES = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1, "k": 1e3, "K": 1e3, "M": 1e6,
                     "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}


def parse_quantity(quantity: Optional[str]) -> Optional[float]:
    """A Kubernetes quantity ("250m", "1.5", "128Mi", "12345678n", "1e3") as a number."""
    if quantity is None:
        return None
    q = str(quantity).strip()
    match = re.fullmatch(r"([+-]?[0-9.]+(?:[eE][+-]?[0-9]+)?)([a-zA-Z]*)", q)
    if not match:
        return None
    number, suffix = float(match.group(1)), match.group(2)
    if suffix in _BINARY_SUFFIXES:
        return number * _BINARY_SUFFIXES[suffix]
    if suffix in _DECIMAL_SUFFIXES:
        return number * _DECIMAL_SUFFIXES[suffix]
    return None


def _millicores(q: Optional[str]) -> Optional[int]:
    v = parse_quantity(q)
    return None if v is None else round(v * 1000)


def _bytes(q: Optional[str]) -> Optional[int]:
    v = parse_quantity(q)
    return None if v is None else round(v)


def _show_cpu(m: Optional[int]) -> Optional[str]:
    return None if m is None else f"{m}m"


def _show_memory(b: Optional[int]) -> Optional[str]:
    return None if b is None else f"{round(b / 2**20)}Mi"


def _percent(part: Optional[int], whole: Optional[int]) -> Optional[float]:
    return round(100 * part / whole, 1) if part is not None and whole else None


def _go_duration(text: Optional[str]) -> Optional[datetime.timedelta]:
    """A Go duration as metrics-server writes windows: "15.018s", "1m0.002s"."""
    if not text:
        return None
    units = {"h": 3600, "m": 60, "s": 1, "ms": 1e-3, "us": 1e-6, "µs": 1e-6, "ns": 1e-9}
    total, found = 0.0, False
    for number, unit in re.findall(r"([0-9.]+)(h|ms|us|µs|ns|m|s)", text):
        total += float(number) * units[unit]
        found = True
    return datetime.timedelta(seconds=total) if found else None


#: A current instance younger than this is "just restarted" for usage notes.
_RECENT_START = datetime.timedelta(minutes=10)


class ContainerUsage(BaseModel):
    """One container's resource usage from metrics-server, beside its requests
    and limits. Whole millicores and bytes for calculating; display strings
    like `kubectl top`."""
    pod: str
    namespace: str
    container: str
    #: None when there's no reading (see notes).
    cpu_millicores: Optional[int] = None
    memory_bytes: Optional[int] = None
    cpu: Optional[str] = None
    memory: Optional[str] = None
    cpu_request_millicores: Optional[int] = None
    cpu_limit_millicores: Optional[int] = None
    memory_request_bytes: Optional[int] = None
    memory_limit_bytes: Optional[int] = None
    #: Usage as a percentage of the limit (memory) and of the request and limit (CPU).
    memory_percent_of_limit: Optional[float] = None
    cpu_percent_of_request: Optional[float] = None
    cpu_percent_of_limit: Optional[float] = None
    #: Time since the sample was taken. A replayed sample keeps its values; this grows.
    sampled: Optional[Duration] = None
    #: The span the sample averages.
    window: Optional[Interval] = None
    #: Time since the current instance started, its restarts, and how the last
    #: one ended: which instance the reading is from.
    started: Optional[Duration] = None
    restarts: int = 0
    last_termination_reason: Optional[str] = None
    last_terminated: Optional[Duration] = None
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _derive_notes(self) -> "ContainerUsage":
        notes = []
        if self.memory_bytes is None and self.cpu_millicores is None:
            notes.append(NOTE_NO_READING)
        elif self.last_termination_reason == "OOMKilled" or (
                self.last_termination_reason not in (None, "Completed")
                and self.started is not None and self.started < _RECENT_START):
            # When it was OOM-killed (a sample can't show that spike), or ended
            # abnormally and the current instance is young (its low reading reads as
            # "it can't be memory"). Not after every restart: a node restart leaves
            # most containers with an Error or Completed ending, and the note on all
            # of them (14 of 31 on one cluster) was noise.
            started = f"started {_format_timedelta(self.started)} ago" if self.started else "restarted"
            ended = (f" {_format_timedelta(self.last_terminated)} ago"
                     if self.last_terminated is not None else "")
            notes.append(f"The last instance ended {self.last_termination_reason}{ended}; this "
                         f"sample is from the current one, {started}. {NOTE_SHORT_WINDOW}")
        if self.memory_percent_of_limit is not None and self.memory_percent_of_limit >= 90:
            notes.append(NOTE_WORKING_SET)
        self.notes = notes
        return self


class NodeUsage(BaseModel):
    """A node's resource usage from metrics-server, against its allocatable."""
    node: str
    cpu_millicores: int
    memory_bytes: int
    cpu: str
    memory: str
    cpu_allocatable_millicores: Optional[int] = None
    memory_allocatable_bytes: Optional[int] = None
    cpu_percent: Optional[float] = None
    memory_percent: Optional[float] = None
    sampled: Optional[Duration] = None
    window: Optional[Interval] = None


def _metrics_api():
    return client.CustomObjectsApi(_binding().api_client)


def _list_metrics(plural: str, namespace: Optional[str] = None) -> list[dict]:
    """metrics.k8s.io items; K8sMetricsUnavailable when the API isn't served."""
    api = _metrics_api()
    try:
        if namespace:
            body = api.list_namespaced_custom_object("metrics.k8s.io", "v1beta1", namespace, plural)
        else:
            body = api.list_cluster_custom_object("metrics.k8s.io", "v1beta1", plural)
    except client.ApiException as e:
        if e.status in (404, 503):
            raise K8sMetricsUnavailable(
                f"The metrics API (metrics.k8s.io) isn't available ({e.status} {e.reason}): "
                "metrics-server is not installed or not running.") from e
        raise K8sApiError(f"Error fetching {plural} metrics: {e}") from e
    return body.get("items", []) if isinstance(body, dict) else []


def _sample_age(item: dict, now: datetime.datetime) -> Optional[datetime.timedelta]:
    ts = item.get("timestamp")
    if not ts:
        return None
    return now - datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def get_container_metrics(namespace: Optional[str] = None) -> list[ContainerUsage]:
    """Each running container's current CPU and memory use (metrics-server, like
    `kubectl top pod --containers`) beside its requests and limits, with
    percentages. A reading is a recent short-window sample; each reading's notes
    say which instance it's from and what it can't show. Raises
    K8sMetricsUnavailable when metrics-server isn't installed. namespace: one,
    or all if omitted."""
    global K8S
    if K8S is None:
        K8S = _get_api_client()
    logging.info(f"get_container_metrics(namespace={namespace})")
    items = _list_metrics("pods", namespace)
    try:
        pods = (K8S.list_namespaced_pod(namespace=namespace) if namespace
                else K8S.list_pod_for_all_namespaces()).items
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching pods: {e}") from e

    now = datetime.datetime.now(datetime.timezone.utc)
    readings = {(i["metadata"]["namespace"], i["metadata"]["name"]): i for i in items}
    out: list[ContainerUsage] = []
    for pod in pods:
        if pod.status is None or pod.status.phase not in ("Running", "Pending"):
            continue  # a finished pod has nothing to measure
        key = (pod.metadata.namespace, pod.metadata.name)
        item = readings.get(key, {})
        usage = {c["name"]: c.get("usage", {}) for c in item.get("containers", [])}
        statuses = {cs.name: cs for cs in (pod.status.container_statuses or [])}
        for c in pod.spec.containers:
            resources = c.resources
            requests = dict(resources.requests or {}) if resources else {}
            limits = dict(resources.limits or {}) if resources else {}
            u = usage.get(c.name)
            cpu = _millicores(u.get("cpu")) if u else None
            memory = _bytes(u.get("memory")) if u else None
            cs = statuses.get(c.name)
            running = cs.state.running if cs and cs.state else None
            last = cs.last_state.terminated if cs and cs.last_state else None
            out.append(ContainerUsage(
                pod=pod.metadata.name, namespace=pod.metadata.namespace, container=c.name,
                cpu_millicores=cpu, memory_bytes=memory,
                cpu=_show_cpu(cpu), memory=_show_memory(memory),
                cpu_request_millicores=_millicores(requests.get("cpu")),
                cpu_limit_millicores=_millicores(limits.get("cpu")),
                memory_request_bytes=_bytes(requests.get("memory")),
                memory_limit_bytes=_bytes(limits.get("memory")),
                memory_percent_of_limit=_percent(memory, _bytes(limits.get("memory"))),
                cpu_percent_of_request=_percent(cpu, _millicores(requests.get("cpu"))),
                cpu_percent_of_limit=_percent(cpu, _millicores(limits.get("cpu"))),
                sampled=_sample_age(item, now) if u else None,
                window=_go_duration(item.get("window")) if u else None,
                started=now - running.started_at if running and running.started_at else None,
                restarts=cs.restart_count if cs else 0,
                last_termination_reason=last.reason if last else None,
                last_terminated=now - last.finished_at if last and last.finished_at else None))
    return out


def get_node_metrics() -> list[NodeUsage]:
    """Each node's current CPU and memory use (metrics-server, like `kubectl top
    node`) against its allocatable, with percentages. Raises
    K8sMetricsUnavailable when metrics-server isn't installed."""
    global K8S
    if K8S is None:
        K8S = _get_api_client()
    logging.info("get_node_metrics()")
    items = _list_metrics("nodes")
    try:
        nodes = {n.metadata.name: n for n in K8S.list_node().items}
    except client.ApiException as e:
        raise K8sApiError(f"Error fetching nodes: {e}") from e
    now = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for item in items:
        name = item["metadata"]["name"]
        usage = item.get("usage", {})
        cpu, memory = _millicores(usage.get("cpu")) or 0, _bytes(usage.get("memory")) or 0
        node = nodes.get(name)
        allocatable = dict(node.status.allocatable or {}) if node and node.status else {}
        cpu_alloc, mem_alloc = _millicores(allocatable.get("cpu")), _bytes(allocatable.get("memory"))
        out.append(NodeUsage(
            node=name, cpu_millicores=cpu, memory_bytes=memory,
            cpu=_show_cpu(cpu), memory=_show_memory(memory),
            cpu_allocatable_millicores=cpu_alloc, memory_allocatable_bytes=mem_alloc,
            cpu_percent=_percent(cpu, cpu_alloc), memory_percent=_percent(memory, mem_alloc),
            sampled=_sample_age(item, now), window=_go_duration(item.get("window"))))
    return out


def print_container_metrics(namespace: Optional[str] = None) -> None:
    """Calls get_container_metrics and prints it, like `kubectl top pod --containers`."""
    print(f"{'POD':<40} {'CONTAINER':<24} {'CPU':<8} {'MEMORY':<9} {'MEM/LIMIT':<16} {'SAMPLED':<8}")
    for u in get_container_metrics(namespace):
        limit = (f"{u.memory}/{_show_memory(u.memory_limit_bytes)} ({u.memory_percent_of_limit}%)"
                 if u.memory_limit_bytes and u.memory else "-")
        print(f"{u.pod:<40} {u.container:<24} {u.cpu or '-':<8} {u.memory or '-':<9} "
              f"{limit:<16} {_format_timedelta(u.sampled) if u.sampled else '-':<8}")


def print_node_metrics() -> None:
    """Calls get_node_metrics and prints it, like `kubectl top node`."""
    print(f"{'NODE':<32} {'CPU':<10} {'CPU%':<6} {'MEMORY':<10} {'MEMORY%':<8}")
    for n in get_node_metrics():
        print(f"{n.node:<32} {n.cpu:<10} {n.cpu_percent if n.cpu_percent is not None else '-':<6} "
              f"{n.memory:<10} {n.memory_percent if n.memory_percent is not None else '-':<8}")


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
    last_schedule_time: Optional[Duration] = None
    last_successful_time: Optional[Duration] = None
    age: Duration
    containers: list[ContainerTemplateSummary] = Field(default_factory=list)


def get_cronjob_summaries(namespace: Optional[str] = None) -> list[CronJobSummary]:
    """CronJobs, like `kubectl get cronjobs`: schedule, suspended, active
    jobs, time since the last schedule and last success, the pod template's
    containers (image, literal env), age."""
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
    start_time: Optional[Duration] = None
    completion_time: Optional[Duration] = None
    conditions: list[str] = Field(default_factory=list)
    age: Duration
    containers: list[ContainerTemplateSummary] = Field(default_factory=list)


def _cronjob_owner(metadata) -> Optional[str]:
    for owner_ref in (getattr(metadata, "owner_references", None) or []):
        if owner_ref.kind == "CronJob":
            return owner_ref.name
    return None


def get_job_summaries(namespace: Optional[str] = None) -> list[JobSummary]:
    """Jobs, like `kubectl get jobs`: active, succeeded and failed pods,
    start and completion times, conditions, owning CronJob, containers, age."""
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
    """Logs of a Job's newest pod; parameters and notes as in
    get_logs_for_pod_and_container. None if the Job has no pods."""
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
    """Logs of a CronJob's latest run (its newest Job's newest pod);
    parameters and notes as in get_logs_for_pod_and_container. None if it has run
    no Jobs."""
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
    age: Duration


def get_pvc_summaries(namespace: Optional[str] = None) -> list[PVCSummary]:
    """PersistentVolumeClaims, like `kubectl get pvc`: status, volume,
    capacity, access modes, storage class, age, and which pods mount each (an
    empty mounted_by may be an orphaned claim)."""
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
    """Events in a namespace or the whole cluster, filtered server-side by
    reason (e.g. "Evicted", "FailedScheduling"), involved_kind ("Pod", "Node",
    ...), involved_name or event_type ("Normal", "Warning"). Each has count,
    first_seen and last_seen, and notes on how far to trust them. Records expire
    about an hour after their last update."""
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
        results.append(_event_summary(event, now, obj))
    return results


def print_events(namespace: Optional[str] = None,
                 reason: Optional[str] = None,
                 involved_kind: Optional[str] = None,
                 involved_name: Optional[str] = None,
                 event_type: Optional[str] = None) -> None:
    """Calls get_events and prints the output to stdout, similar to
    `kubectl get events`."""
    events = get_events(namespace, reason, involved_kind, involved_name, event_type)
    print(f"{'LAST SEEN':<12} {'COUNT':<7} {'TYPE':<10} {'REASON':<20} {'OBJECT':<40} {'MESSAGE':<40}")
    for event in events:
        last_seen = _format_timedelta(event.last_seen) if event.last_seen else "-"
        count = str(event.count) if event.count is not None else "-"
        message = (event.message[:37] + '...') if event.message and len(event.message) > 40 else event.message
        print(f"{last_seen:<12} {count:<7} {event.type:<10} {event.reason:<20} {event.object:<40} {message:<40}")



class ClusterInfo(BaseModel):
    """Which cluster these tools are answering from."""
    source: Literal["kubeconfig", "in-cluster", "capture"]
    context: Optional[str] = None
    server: Optional[str] = None
    kubeconfig: Optional[str] = None
    server_version: Optional[str] = None
    captured_at: Optional[datetime.datetime] = None


def get_cluster_info() -> ClusterInfo:
    """Which cluster these tools answer from: source ("kubeconfig", "in-cluster",
    or "capture" for a replayed snapshot), context, API server, server version and,
    for a capture, when it was taken. Call it first when more than one cluster
    could be involved. Never returns credentials."""
    binding = _binding()
    logging.info("get_cluster_info()")
    try:
        server_version = client.VersionApi(binding.api_client).get_code().git_version
    except Exception as e:
        # Still worth answering: which cluster the tools are bound to is the
        # question, and an unreachable server is part of the answer.
        logging.warning(f"Could not read the API server version: {e}")
        server_version = None
    return ClusterInfo(source=binding.source, context=binding.context,
                       server=binding.server, kubeconfig=binding.kubeconfig,
                       server_version=server_version)


def print_cluster_info() -> None:
    """Calls get_cluster_info and prints the output to stdout."""
    info = get_cluster_info()
    print(f"{'SOURCE':<12} {'CONTEXT':<24} {'SERVER':<40} {'VERSION':<12}")
    print(f"{info.source:<12} {info.context or '-':<24} {info.server or '-':<40} "
          f"{info.server_version or '-':<12}")


# ---------------------------------------------------------------------------
# Composite investigation tools (issue #13), built from the tools above. The
# models are here with the others; the logic is in composites.py, imported when
# a tool is called so the two modules don't import each other at load time.
# ---------------------------------------------------------------------------

class _Compact(BaseModel):
    """Serializes without empty fields: these results are one line per workload,
    and the MCP server sends a result twice (as text and as structured content)."""

    @model_serializer(mode="wrap")
    def _drop_empty(self, handler):
        return {k: v for k, v in handler(self).items() if v not in (None, [], {})}



class Termination(_Compact):
    """A container's last termination, and how long until it started again."""
    #: Omitted where the termination sits under an instance that names them.
    pod: Optional[str] = None
    container: Optional[str] = None
    exit_code: Optional[int] = None
    exit_meaning: Optional[str] = None
    #: Kubernetes' recorded reason (OOMKilled, Error, Completed, ...).
    reason: Optional[str] = None
    #: How long that instance ran. Not the restart cadence: see restart_gap.
    instance_lifetime: Optional[Interval] = None
    #: Time since it finished.
    finished: Optional[Duration] = None
    #: The back-off: from finishing to the next start. While the container is
    #: still waiting, the time waited so far (and `waiting` is set).
    restart_gap: Optional[Duration] = None
    #: The current waiting reason, e.g. CrashLoopBackOff, if not running.
    waiting: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


class WorkloadHealth(_Compact):
    """One unhealthy workload's health."""
    workload: str
    ready: int
    desired: int
    #: None when none of its pods could be found (see notes), rather than 0.
    restarts: Optional[int] = None
    healthy: bool = False
    last_termination: Optional[Termination] = None
    #: e.g. "limit 300Mi = request", "limit 512Mi, request 256Mi", "no limit".
    memory: Optional[str] = None
    #: Time since the pod template last changed (the current revision's age).
    template_changed: Optional[Duration] = None
    #: Its HorizontalPodAutoscaler, in a line, e.g. "HPA cart: 3 replicas (min 1,
    #: max 3), at max; cpu 92%/80%".
    autoscaler: Optional[str] = None
    #: Services sending traffic to its pods, e.g. "Service/ad: 0 ready, 1 not ready".
    services: list[str] = Field(default_factory=list)
    #: Each container's current usage, e.g. "ad: memory 140Mi of 300Mi (46.7%), cpu
    #: 850m"; "ad: no reading" when there is none. See notes on what a sample shows.
    usage: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class FailureGroup(_Compact):
    """Unhealthy workloads failing the same way."""
    signature: str
    workloads: list[str]


class NamespaceHealth(_Compact):
    namespace: str
    #: Unhealthy workloads, in full, by name.
    workloads: list[WorkloadHealth] = Field(default_factory=list)
    #: Healthy workloads, one line each: "Deployment/cart 3/3", with restarts if any.
    healthy: list[str] = Field(default_factory=list)
    #: Two or more unhealthy workloads with the same failure signature.
    common_failures: list[FailureGroup] = Field(default_factory=list)
    #: Services with no ready endpoints, e.g. "Service/ad: 0 ready, 1 not ready".
    services_without_ready_endpoints: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ContainerEssentials(_Compact):
    container: str
    image: str
    requests: dict[str, str] = Field(default_factory=dict)
    limits: dict[str, str] = Field(default_factory=dict)
    #: e.g. "liveness: httpGet :8080/healthz every 10s, fails after 3", or
    #: "none configured".
    probes: list[str] = Field(default_factory=list)


class Instance(_Compact):
    """One container instance of one pod, now."""
    pod: str
    container: str
    state: str
    #: Time since the current instance started (if running).
    started: Optional[Duration] = None
    ready: bool = False
    restarts: int = 0
    last_termination: Optional[Termination] = None


class EventLine(_Compact):
    """Events deduplicated across the workload's objects."""
    type: str
    reason: str
    message: str
    #: Occurrences, summed over the objects it was recorded for.
    count: Optional[int] = None
    first_seen: Optional[Duration] = None
    last_seen: Optional[Duration] = None
    objects: int = 1


class LogTail(_Compact):
    pod: str
    container: str
    previous: bool
    lines: list[str] = Field(default_factory=list)
    #: Lines the tool added about this log ("[k8stools] note: ..."), not output.
    notes: list[str] = Field(default_factory=list)


class LastChange(_Compact):
    revision: Optional[int] = None
    #: Time since the current revision went live (its object was created).
    age: Optional[Duration] = None
    reused: bool = False
    rollout_restart: bool = False
    #: "field: before -> after", or "field: added/removed/changed".
    changes: list[str] = Field(default_factory=list)


class WorkloadReport(_Compact):
    workload: str
    ready: int
    desired: int
    containers: list[ContainerEssentials] = Field(default_factory=list)
    instances: list[Instance] = Field(default_factory=list)
    events: list[EventLine] = Field(default_factory=list)
    logs: list[LogTail] = Field(default_factory=list)
    last_change: Optional[LastChange] = None
    config: list[ConfigReference] = Field(default_factory=list)
    #: Its HorizontalPodAutoscaler, if one scales it.
    autoscaler: Optional[HpaSummary] = None
    #: Services sending traffic to its pods, with each pod's endpoint state.
    services: list[EndpointSummary] = Field(default_factory=list)
    #: Its containers' current usage against requests and limits.
    usage: list[ContainerUsage] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def get_namespace_health(namespace: str = "default") -> NamespaceHealth:
    """What is wrong in a namespace, and where. Each unhealthy workload
    (Deployment, StatefulSet, DaemonSet, Job, or a pod nothing owns) in full:
    ready/desired, restarts, the last termination (exit code and its meaning
    beside Kubernetes' recorded reason), instance lifetime and restart gap,
    memory limit vs request, when the pod template last changed. Healthy ones
    in a line each. Workloads failing the same way are grouped. Facts only."""
    from .composites import namespace_health
    return namespace_health(sys.modules[__name__], namespace)


def get_workload_report(name: str, namespace: str = "default", kind: Optional[str] = None,
                        log_lines: int = 20, grep: Optional[str] = None) -> WorkloadReport:
    """Everything about one workload in one call: container images, resources
    and probes; each instance's state and last termination; its events,
    deduplicated; current and previous log tails of its most troubled pod; the
    last pod-template change; the ConfigMaps and Secrets it uses. kind:
    Deployment, StatefulSet, DaemonSet or Job (found by name if omitted).
    log_lines: per log (max 100). grep: keep only matching log lines."""
    from .composites import workload_report
    return workload_report(sys.modules[__name__], name, namespace, kind, log_lines, grep)

TOOLS = [
    get_cluster_info,
    get_namespace_health,
    get_workload_report,
    get_namespaces,
    get_node_summaries,
    get_pod_summaries,
    get_pod_container_statuses,
    get_pod_events,
    get_pod_spec,
    get_logs_for_pod_and_container,
    get_deployment_summaries,
    get_replicaset_summaries,
    get_workload_history,
    get_service_summaries,
    get_endpoint_summaries,
    get_configmap_summaries,
    get_configmap,
    get_statefulset_summaries,
    get_daemonset_summaries,
    get_hpa_summaries,
    get_container_metrics,
    get_node_metrics,
    get_cronjob_summaries,
    get_job_summaries,
    get_logs_for_job,
    get_logs_for_cronjob,
    get_pvc_summaries,
    get_events,
]


# ---------------------------------------------------------------------------
# Toolsets (issue #20)
# ---------------------------------------------------------------------------
#
# Every tool's description is standing context on every turn of every agent
# that has it, and agents choose less reliably among similar tools. A toolset
# names the subset an agent needs. These are names rather than functions so
# that `mock_tools` and callers composing tools in Python select the same sets.

#: Tools another tool already covers. They stay in the library and in "all",
#: and leave the smaller toolsets.
OVERLAPPING_TOOLS = (
    "get_pod_events",            # get_events(involved_name=...)
    "get_replicaset_summaries",  # get_workload_history
    "get_logs_for_job",          # get_logs_for_pod_and_container + PodSummary.owner
    "get_logs_for_cronjob",      # likewise
)

#: Tool names in each toolset. "investigate" is every tool but the overlapping
#: ones; "triage" is the few an agent needs to find where to look.
TOOLSET_NAMES: dict[str, tuple[str, ...]] = {
    "triage": (
        "get_cluster_info",
        "get_namespace_health",
        "get_workload_report",
        "get_events",
        "get_node_summaries",
    ),
    "investigate": tuple(fn.__name__ for fn in TOOLS
                         if fn.__name__ not in OVERLAPPING_TOOLS),
    "all": tuple(fn.__name__ for fn in TOOLS),
}

#: The toolset k8s-mcp-server serves unless told otherwise.
DEFAULT_TOOLSET = "all"


def select_tools(tools: list, toolset: str = DEFAULT_TOOLSET,
                 include: tuple[str, ...] = (), exclude: tuple[str, ...] = ()) -> list:
    """The functions in ``tools`` that make up ``toolset``, plus ``include`` and
    minus ``exclude`` (tool names), in ``tools``' order.

    ``tools`` is ``k8s_tools.TOOLS`` or ``mock_tools.TOOLS``. Unknown toolset or
    tool names raise ``ValueError``, so a misspelled name in a server's
    configuration fails at startup rather than silently serving a different set.
    """
    if toolset not in TOOLSET_NAMES:
        raise ValueError(f"Unknown toolset '{toolset}': expected one of "
                         f"{', '.join(TOOLSET_NAMES)}.")
    known = {fn.__name__ for fn in tools}
    unknown = [n for n in (*include, *exclude) if n not in known]
    if unknown:
        raise ValueError(f"Unknown tool name(s): {', '.join(unknown)}.")
    chosen = (set(TOOLSET_NAMES[toolset]) | set(include)) - set(exclude)
    return [fn for fn in tools if fn.__name__ in chosen]


#: The functions in each toolset, for composing tools in Python. Wrap them with
#: `redaction.wrap_with_redaction` before handing them to an agent.
TOOLSETS: dict[str, list] = {name: select_tools(TOOLS, name) for name in TOOLSET_NAMES}
