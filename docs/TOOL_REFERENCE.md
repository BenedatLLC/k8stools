# Tool reference

The full description of every tool's parameters and output, field by field.

The tools' own descriptions, which an MCP client sends to the model on every
turn, are kept short (a test holds each to 700 characters). The detail lives here,
and in two places in the results themselves:

## Notes in results

Warnings that apply to a particular result come back with it, so an agent sees
them when they matter instead of carrying them on every turn. Ten models have a
`notes` list, empty unless a warning applies. Notes are derived from the item's
own fields, so live calls and replayed captures (including captures made before
notes existed) give the same notes; captures don't store them.

| Model | When | Note |
|---|---|---|
| `NodeSummary` | it has a `Ready` time | Ready's time changes only when its status does: a restart that never went NotReady doesn't reset it, so it isn't uptime. For the node's last start, see its Starting/Rebooted events or kube-proxy's started_at. |
| `EventSummary` | `count` > 1 | count covers first_seen to last_seen. Updates are rate-limited (after 25, at most one per 5 minutes per object and type), so both can lag; first_seen is where this record starts, not necessarily when the problem did. |
| `EventSummary` | `reason` is `BackOff` | BackOff repeats while a container waits, so this count is several times the number of restarts; count restarts from restart_count or Created events. |
| `PodSummary` | it has restarts and isn't fully ready | last_restart is when a container last terminated (in CrashLoopBackOff, its last crash), not when it was restarted. |
| `ContainerStatus` | waiting after a restart | Waiting with no running instance: its logs, with previous=False or True, are the last terminated instance's. |
| `ContainerStatus` | `last_state` has `ran_for` | last_state.ran_for is how long that instance ran; restarts are further apart, by the back-off between its finished_at and the next start. |
| `DaemonSetSummary` | no `node_selector` | No nodeSelector, but affinity or tolerations may still limit its nodes; desired_number_scheduled is the actual count. |
| `HpaSummary` | at `max_replicas` | At maxReplicas: it can't add replicas, whatever its metrics say. See the ScalingLimited condition. |
| `EndpointSummary` | no ready endpoints | No ready endpoints: traffic sent to this Service has no pod to reach. |
| `IngressSummary` | a backend doesn't resolve | A backend doesn't resolve: see the rule's backend_problem. Requests matching that rule can't reach a pod. |
| `IngressSummary` | a backend Service has no ready endpoints | A backend Service has no ready endpoints (backend_ready 0): requests matching that rule can't reach a pod. |
| `CustomResourceStatus` | `observed_generation` < `generation` | observed_generation < generation: its controller hasn't yet acted on the latest spec change, so the conditions may describe the previous spec. |
| `CustomResourceStatus` | no conditions | No status.conditions: this resource reports its state some other way, which this tool doesn't read. |
| `ContainerUsage` | no reading | No reading: the container isn't running, or started within about one metrics-server scrape interval. |
| `ContainerUsage` | the last instance was OOM-killed, or ended abnormally and the current one started within 10 minutes | "The last instance ended OOMKilled 6m ago; this sample is from the current one, started 58s ago." followed by: A sample averages a short window: a spike that ends in an OOM kill usually never appears in one. |
| `ContainerUsage` | memory at 90% or more of its limit | Memory is the working set: memory in use plus recently used file cache, which the kernel reclaims before an OOM kill. Near the limit is common; it isn't proof a kill is coming. |

`get_workload_history` returns its caveats in the result's `limits` list.

**Logs** are plain text, so the log tools (`get_logs_for_pod_and_container`,
`get_logs_for_job`, `get_logs_for_cronjob`) put a note on a line of its own at
the top, starting `[k8stools] note: `, which is not the container's output. A
log gets a note when:

- the container is waiting (e.g. in CrashLoopBackOff) after a restart: there is
  no running instance, so `previous=False` and `previous=True` both return the
  last terminated instance's log;
- the text is the kubelet's `unable to retrieve container logs for ...` message
  rather than a log: that instance's log file is gone.

Captures store logs without these lines, and replay adds them for the replayed
state. Strip lines starting with `[k8stools] note: ` (the constant
`k8s_tools.LOG_NOTE_PREFIX`) to get the container's output alone.

## Tools

The sections below began as the tools' long descriptions (2.4.0) and are
maintained by hand. Tools are listed in the order of `k8s_tools.TOOLS`.

### `get_cluster_info`

```python
get_cluster_info() -> ClusterInfo
```

Report which Kubernetes cluster these tools are bound to. Call this first
when more than one cluster could be involved, and before drawing conclusions
that depend on which cluster the data came from.

The binding is made once (at server startup, or on the first tool call) and
shared by every tool, so all tools answer from this cluster.

#### Returns

ClusterInfo
An object with the following fields:

- **`source`** (`str`): "kubeconfig" (bound through a kubeconfig context), "in-cluster" (the service account of the pod the server runs in), or "capture" (a recorded snapshot of a cluster being replayed, not a live cluster).
- **`context`** (`Optional[str]`): The kubeconfig context name. None for in-cluster config, and for a capture that did not record one.
- **`server`** (`Optional[str]`): URL of the cluster's API server.
- **`kubeconfig`** (`Optional[str]`): Path of the kubeconfig file (or ``:``-separated list of files) the binding was read from. None unless source is "kubeconfig".
- **`server_version`** (`Optional[str]`): Kubernetes version reported by the API server, e.g. "v1.31.2". None if the server could not be reached, or the capture did not record it.
- **`captured_at`** (`Optional[datetime.datetime]`): For a capture, when it was taken; every age and timestamp the tools return is relative to that moment. None for a live cluster.

### `get_namespace_health`

```python
get_namespace_health(namespace: str = 'default') -> NamespaceHealth
```

What is wrong in a namespace, and where, in one call: a full entry for each
unhealthy workload (Deployment, StatefulSet, DaemonSet, Job, or a pod nothing
owns), and one line for each healthy one. Built from the other tools, so it
answers the same way from a replayed capture. It states facts and fixed
Kubernetes semantics, never a diagnosis.

Pods are linked to workloads by their controlling owner (`PodSummary.owner`).
Captures taken before 2.3.0 recorded no owners, so a pod without one is matched
by its generated name instead (`<replicaset>-<5 chars>`, `<statefulset>-<n>`,
`<job>-<5 chars>`, `<daemonset>-<5 chars>`), and a note says so. A workload none
of whose pods can be found reports `restarts` as unknown (absent), not 0.

Restart cadence comes from container status, not event counts: event records lag
what they count (see `get_events`), so an event-derived rate is an average over
the record, not the current rhythm.

#### Returns

- **`namespace`** (`str`)
- **`workloads`** (`list[WorkloadHealth]`): the unhealthy workloads, by name. A workload is healthy when all desired pods are ready, no container is waiting, and (for a Job) no exit failed. Each has:
  - **`workload`** (`str`): "Kind/name", e.g. "Deployment/ad", or "Pod/name" for a pod nothing owns.
  - **`ready`**, **`desired`** (`int`): ready and desired pods; for a Job, 1/1 once it succeeded.
  - **`restarts`** (`int`): container restarts across the workload's pods; absent when none of its pods could be found.
  - **`last_termination`** (`Termination`): the most recent termination across its pods and containers:
    - **`pod`**, **`container`**
    - **`exit_code`** and **`exit_meaning`**: the code's fixed meaning, e.g. 137 is "killed by SIGKILL (128+9)".
    - **`reason`**: Kubernetes' recorded reason (OOMKilled, Error, Completed, ...), shown beside the exit code; a note says when the two disagree (137 without OOMKilled).
    - **`instance_lifetime`**: how long that instance ran.
    - **`finished`**: time since it finished.
    - **`restart_gap`**: the back-off from finishing to the next start, or, while the container is still waiting, the wait so far. Restarts are lifetime + gap apart.
    - **`waiting`**: the current waiting reason, e.g. CrashLoopBackOff.
  - **`autoscaler`** (`str`): its HPA in a line, e.g. "HPA cart: 3 replicas (min 1, max 3), at max; cpu 92%/80%".
  - **`services`** (`list[str]`): Services sending traffic to its pods, e.g. "Service/ad: 0 ready, 1 not ready". Linked through their endpoints, which name the pods they selected, ready or not.
  - **`usage`** (`list[str]`): each container's current usage, e.g. "ad: memory 140Mi of 300Mi (46.7%), cpu 850m", or "ad: no reading". The readings' notes (e.g. that the sample is from a fresh instance after an OOM kill) are added to the workload's `notes`.
  - **`memory`** (`str`): the memory limit against the request, e.g. "limit 300Mi = request", "limit 512Mi, request 256Mi", "no limit"; per container when they differ.
  - **`template_changed`** (`timedelta`): time since the pod template last changed (the current revision's age, from `get_workload_history`). Not how long the workload has been healthy.
  - **`notes`** (`list[str]`)
- **`services_without_ready_endpoints`** (`list[str]`): Services with nowhere to send traffic, e.g. "Service/ad: 0 ready, 1 not ready".
- **`ingress_problems`** (`list[str]`): Ingress rules that can't reach a pod: a backend that doesn't resolve, or one with no ready endpoints, e.g. "Ingress/shop: shop.example.com/old -> legacy:80: Service 'legacy' not found".
- **`healthy`** (`list[str]`): one line per healthy workload, e.g. "Deployment/cart 3/3", with what's worth knowing about it added: restarts, "HPA at max 3", or "memory at 99.8% of limit" when a container is at 90% or more of its memory limit (with the working-set note in `notes`).
- **`common_failures`** (`list[FailureGroup]`): two or more unhealthy workloads whose last terminations share an exit code, reason and memory shape. The signature also gives their instance lifetimes ("lifetimes 2s-20s"), which are shown rather than required to match: one workload's instances can live 7s, 20s and 2m on successive restarts. A shared signature is a fact worth checking for a common cause, not a conclusion.
- **`notes`** (`list[str]`)

Empty fields are left out of the result.

### `get_workload_report`

```python
get_workload_report(name: str, namespace: str = 'default', kind: Optional[str] = None, log_lines: int = 20, grep: Optional[str] = None) -> WorkloadReport
```

Everything about one workload in one call: what an agent otherwise gathers from
container statuses, events, logs, the pod spec and workload history. Built from
those tools, so it answers the same way from a replayed capture.

#### Parameters

- **`name`**, **`namespace`**: the workload.
- **`kind`**: Deployment, StatefulSet, DaemonSet or Job. Found by name if omitted; an error if the name is ambiguous.
- **`log_lines`**: lines per log tail, at most 100. Lines longer than 300 characters are cut.
- **`grep`**: keep only log lines matching this regular expression (case-insensitive; a plain substring if it isn't a valid regex), then take the last `log_lines`.

#### Returns

- **`workload`**, **`ready`**, **`desired`**
- **`containers`** (`list[ContainerEssentials]`): per container of the focus pod: `image`, `requests`, `limits`, and `probes` (e.g. "liveness: httpGet :8080/healthz every 10s, fails after 3", or "none configured").
- **`instances`** (`list[Instance]`): every pod's containers now: `state`, `started` (time since the current instance started), `ready`, `restarts`, and `last_termination` as in `get_namespace_health`.
- **`events`** (`list[EventLine]`): events for the workload, its ReplicaSets and its pods, merged where they differ only in which object they name (`objects` counts them, and `count` sums them), newest first, at most 20. Events for other kinds that share the name (a Service called like the Deployment) are left out.
- **`logs`** (`list[LogTail]`): current and previous log tails of the focus pod, the one most in trouble: a waiting container first, then most restarts. A note line the log tool added (e.g. that the text is the kubelet's message, not container output) is moved into the tail's `notes`.
- **`last_change`** (`LastChange`): the current revision from `get_workload_history`: `revision`, `age`, `reused`, `rollout_restart`, and `changes` as "field: before -> after".
- **`config`** (`list[ConfigReference]`): the ConfigMaps and Secrets it uses, as in `get_workload_history`.
- **`autoscaler`** (`HpaSummary`): its HPA, if one scales it.
- **`services`** (`list[EndpointSummary]`): Services sending traffic to its pods, with each backend's state.
- **`usage`** (`list[ContainerUsage]`): its containers' current usage, as from `get_container_metrics`. When metrics aren't available, `notes` says why.
- **`ingresses`** (`list[str]`): Ingress rules routing to its Services, e.g. "Ingress/shop: shop.example.com/ -> test-service:80 (3 ready)".
- **`notes`** (`list[str]`): including where the event records start ("not necessarily when the problem did"), when the template last changed ("not how long the workload has been healthy"), which pod the containers and logs come from, and an exit code / reason disagreement.

Empty fields are left out of the result.

### `get_namespaces`

```python
get_namespaces() -> list[NamespaceSummary]
```

Return a summary of the namespaces for this Kubernetes cluster, similar to that
returned by `kubectl get namespace`.

#### Returns

list of NamespaceSummary
List of namespace summary objects. Each NamespaceSummary has the following fields:

- **`name`** (`str`): Name of the namespace.
- **`status`** (`str`): Status phase of the namespace.
- **`age`** (`datetime.timedelta`): Age of the namespace (current time minus creation timestamp).

### `get_node_summaries`

```python
get_node_summaries() -> list[NodeSummary]
```

Return a summary of the nodes for this Kubernetes cluster, similar to that
returned by `kubectl get nodes -o wide`.

#### Returns

list of NodeSummary
List of node summary objects. Each NodeSummary has the following fields:

- **`name`** (`str`): Name of the node.
- **`status`** (`str`): Status of the node (Ready, NotReady, etc.).
- **`roles`** (`list[str]`): List of roles for the node (e.g., ['control-plane', 'master']).
- **`age`** (`datetime.timedelta`): Age of the node (current time minus creation timestamp).
- **`version`** (`str`): Kubernetes version running on the node.
- **`internal_ip`** (`Optional[str]`): Internal IP address of the node.
- **`external_ip`** (`Optional[str]`): External IP address of the node (if available).
- **`os_image`** (`Optional[str]`): Operating system image running on the node.
- **`kernel_version`** (`Optional[str]`): Kernel version of the node.
- **`container_runtime`** (`Optional[str]`): Container runtime version on the node.
- **`capacity`** (`dict[str, str]`): Total capacity of the node keyed by resource name (e.g. "cpu", "memory", "ephemeral-storage", "pods"). Empty if unavailable.
- **`allocatable`** (`dict[str, str]`): Resources allocatable to pods (capacity minus system-reserved), keyed by resource name. Empty if unavailable.
- **`conditions`** (`dict[str, str]`): Node conditions keyed by type with their status, e.g. {"Ready": "True", "MemoryPressure": "False", "DiskPressure": "False"}.
- **`conditions_since`** (`dict[str, datetime.timedelta]`): For each condition in `conditions`, the time since its status last changed (the API's lastTransitionTime), e.g. how long the node has been Ready, or how long ago a MemoryPressure episode ended. A condition with no transition time is left out.

  It changes only when the status does. A node or kubelet restart in which the node comes back Ready without having been marked NotReady or Unknown in between does not reset it: on a minikube cluster stopped and started together with its control plane, Ready stayed "True since the node was created" across a reboot. So a long Ready age does not mean the node or its pods have been running that long, and a short one can come from a brief NotReady flap rather than a reboot. To date a node's last start, use its "Starting" or "Rebooted" events (`get_events(involved_kind="Node")`, while they last - events usually expire after an hour), or the `started_at` of containers that start with the node, such as kube-proxy and the kube-system control-plane containers (`get_pod_container_statuses`).
- **`taints`** (`list[str]`): Taints on the node formatted as "key=value:effect" (value omitted when empty).
- **`labels`** (`dict[str, str]`): All labels on the node. Useful for identifying node pool / instance type and spotting version skew across pools.
- **`notes`** (`list[str]`): Warnings that apply to this item; empty when none do. See [Notes in results](#notes-in-results).

### `get_pod_summaries`

```python
get_pod_summaries(namespace: Optional[str] = None) -> list[PodSummary]
```

Retrieves a list of PodSummary objects for pods in a given namespace or all namespaces.

#### Parameters

- **`namespace`** (`Optional[str], default=None`): The specific namespace to list pods from. If None, lists pods from all namespaces.

#### Returns

list of PodSummary
A list of PodSummary objects, each providing a summary of a pod's status with the following fields:

- **`name`** (`str`): Name of the pod.
- **`namespace`** (`str`): Namespace in which the pod is running.
- **`total_containers`** (`int`): Total number of containers in the pod.
- **`ready_containers`** (`int`): Number of containers currently in ready state.
- **`restarts`** (`int`): Total number of restarts for all containers in the pod.
- **`last_restart`** (`Optional[datetime.timedelta]`): Time since a container of this pod last terminated - the most recent last_state.finished_at across its containers - or None if none has. For a container waiting in CrashLoopBackOff this is its last crash, not a restart: it has not been started again yet. Taken from the kubelet's status, so it is current, unlike event records.
- **`age`** (`datetime.timedelta`): Age of the pod (current time minus creation timestamp).
- **`ip`** (`Optional[str]`): Pod IP address (None if not assigned).
- **`node`** (`Optional[str]`): Name of the node where the pod is running (None if not scheduled).
- **`owner`** (`Optional[str]`): The pod's controlling owner, as "Kind/name" - the object that created and manages it, e.g. "DaemonSet/otel-collector-agent", "StatefulSet/valkey-cart", "Job/backup-29318400". None for a bare pod nobody manages. This is the direct owner only: a Deployment's pods are owned by one of its replica sets ("ReplicaSet/ad-7d9f8c6b5"), whose `owner_deployment` (`get_replicaset_summaries`) names the Deployment; likewise a Job's `owner` names its CronJob. Static (mirror) pods such as the control plane's report "Node/<node name>". Group pods into workloads by this field rather than by stripping name suffixes.
- **`notes`** (`list[str]`): Warnings that apply to this item; empty when none do. See [Notes in results](#notes-in-results).

### `get_pod_container_statuses`

```python
get_pod_container_statuses(pod_name: str, namespace: str = 'default') -> list[ContainerStatus]
```

Get the status for all containers in a specified Kubernetes pod.

#### Parameters

- **`pod_name`** (`str`): Name of the pod to retrieve container statuses for.
- **`namespace`** (`str, optional`): Namespace of the pod (default is "default").

#### Returns

list of ContainerStatus
List of container status objects for the specified pod. Each ContainerStatus has the following fields:

- **`pod_name`** (`str`): Name of the pod.
- **`namespace`** (`str`): Namespace of the pod.
- **`container_name`** (`str`): Name of the container.
- **`image`** (`str`): Image name.
- **`ready`** (`bool`): Whether the container is currently passing its readiness check. The value will change as readiness probes keep executing.
- **`restart_count`** (`int`): Number of times the container has restarted.
- **`started`** (`Optional[bool]`): Started indicates whether the container has finished its postStart lifecycle hook and passed its startup probe.
- **`stop_signal`** (`Optional[str]`): Stop signal for the container.
- **`state`** (`Optional[ContainerState]`): Current state of the container.
- **`last_state`** (`Optional[ContainerState]`): Last state of the container. When Terminated, ``ran_for`` is how long that instance ran (``finished_at - started_at``). It is not the restart interval, which adds the back-off named in a Waiting ``state``'s message, and it is not how much time the container's log covers: a process can stop logging long before it is killed.
- **`volume_mounts`** (`list[VolumeMountStatus]`): Status of volume mounts for the container
- **`resource_requests`** (`dict[str, str]`): Describes the minimum amount of compute resources required. If Requests is omitted for a container, it defaults to Limits if that is explicitly specified, otherwise to an implementation-defined value. Requests cannot exceed Limits.
- **`resource_limits`** (`dict[str, str]`): Describes the maximum amount of compute resources allowed.
- **`allocated_resources`** (`dict[str, str]`): Compute resources allocated for this container by the node.
- **`notes`** (`list[str]`): Warnings that apply to this item; empty when none do. See [Notes in results](#notes-in-results).

### `get_pod_events`

```python
get_pod_events(pod_name: str, namespace: str = 'default') -> list[EventSummary]
```

Get events for a specific Kubernetes pod. This is equivalent to the kubectl command:
`kubectl get events -n NAMESPACE --field-selector involvedObject.name=POD_NAME,involvedObject.kind=Pod`

#### Parameters

- **`pod_name`** (`str`): Name of the pod to retrieve events for.
- **`namespace`** (`str, optional`): Namespace of the pod (default is "default").

#### Returns

list of EventSummary
List of events associated with the specified pod. Each EventSummary has the following fields:

- **`last_seen`** (`Optional[datetime.timedelta]`): Time since the most recent occurrence of the event (if available).
- **`first_seen`** (`Optional[datetime.timedelta]`): Time since the first occurrence combined into this record (if available).
- **`count`** (`Optional[int]`): How many occurrences Kubernetes combined into this record: repeats of the same event on the same object are counted in one record rather than listed separately. The count covers first_seen to last_seen, not the object's lifetime - a record that stops repeating expires (after 1h by default), and a later repeat starts a new one. For a crash-looping container, count the "Created" or "Started" events to get restarts. "BackOff" is emitted repeatedly while the kubelet waits to restart, so its count is several times the number of restarts. count divided by (first_seen - last_seen) is the average rate over that window, not the current back-off.

  A record can lag behind what it counts. By default the kubelet writes at most one event update per object and event type ("Normal" or "Warning") every 5 minutes, once a burst of 25 is used up; occurrences in between are counted but only written with the next update, which then jumps by several at once. So count and last_seen describe the most recent *written* occurrence and can trail reality by several occurrences and tens of minutes. They lag together, so the rate above still holds, but last_seen is not the time of the last restart: for that, use the container status (last_state.finished_at, state.started_at from get_pod_container_statuses) or PodSummary.last_restart, which come from the kubelet's status rather than from events. "Pulled", "Created" and "Started" share one write budget, so their counts for the same restarts can differ by a few; don't compare counts across reasons.
- **`type`** (`str`): Type of the event.
- **`reason`** (`str`): Reason for the event.
- **`object`** (`str`): The object this event applies to.
- **`message`** (`str`): Message describing the event.
- **`notes`** (`list[str]`): Warnings that apply to this item; empty when none do. See [Notes in results](#notes-in-results).

### `get_pod_spec`

```python
get_pod_spec(pod_name: str, namespace: str = 'default') -> dict[str, Any]
```

Retrieves the spec for a given pod in a specific namespace.

#### Parameters

- **`pod_name`** (`str`): The name of the pod.
- **`namespace`** (`str`): The namespace the pod belongs to (defaults to "default").

#### Returns

dict[str, Any]
The pod's spec object, containing its desired state. It is converted
from a V1PodSpec to a dictionary. Key fields include:

- **`containers`** (`list of kubernetes.client.V1Container`): List of containers belonging to the pod. Each container defines its image, ports, environment variables, resource requests/limits, etc.
- **`init_containers`** (`list of kubernetes.client.V1Container, optional`): List of initialization containers belonging to the pod.
- **`volumes`** (`list of kubernetes.client.V1Volume, optional`): List of volumes mounted in the pod and the sources available for the containers.
- **`node_selector`** (`dict, optional`): A selector which must be true for the pod to fit on a node. Keys and values are strings.
- **`restart_policy`** (`str`): Restart policy for all containers within the pod. Common values are "Always", "OnFailure", "Never".
- **`service_account_name`** (`str, optional`): Service account name in the namespace that the pod will use to access the Kubernetes API.
- **`dns_policy`** (`str`): DNS policy for the pod. Common values are "ClusterFirst", "Default".
- **`priority_class_name`** (`str, optional`): If specified, indicates the pod's priority_class via its name.
- **`node_name`** (`str, optional`): NodeName is a request to schedule this pod onto a specific node.

### `get_logs_for_pod_and_container`

```python
get_logs_for_pod_and_container(pod_name: str, namespace: str = 'default', container_name: Optional[str] = None, tail: Optional[int] = None, since_seconds: Optional[int] = None, previous: bool = False) -> Optional[str]
```

Retrieves logs from a Kubernetes pod and container.

The log may begin with lines starting `[k8stools] note: `, added by the tool rather than the container; see [Notes in results](#notes-in-results).

#### Parameters

- **`pod_name`** (`str`): The name of the pod.
- **`namespace`** (`str`): The namespace of the pod.
- **`container_name`** (`str, optional`): The name of the container within the pod. If None, defaults to the first container.
- **`tail`** (`int, optional`): Number of lines to return from the end of the log. If None, defaults to the last 1000 lines. Pass a larger value to retrieve more history, or a small value for a bounded recent slice.
- **`since_seconds`** (`int, optional`): If set, only return logs newer than this many seconds. Combines with `tail` (both limits apply).
- **`previous`** (`bool, default False`): If True, return logs from the *previous* terminated instance of the container instead of the current one. Indispensable for crashloop analysis, where the current instance's logs are empty or post-restart. Fails if there is no previous instance.

  Three Kubernetes behaviors make `previous=True` look as though it were ignored or broken; none is a fault in this tool, and all are worth knowing before drawing a conclusion from the output:

  - While a container sits in CrashLoopBackOff there is no running instance, so the kubelet serves the most recently *terminated* one for `previous=False` as well - both calls then return the same text. Check the container's state with `get_pod_container_statuses` before concluding the flag had no effect.
  - A restart between two calls shifts the window: the instance that was current becomes the previous one, so a container crash-looping every few seconds can legitimately return identical bytes for both. Compare the log timestamps, not just the flag.
  - Once the previous instance's log file has been reclaimed, the API answers 200 with the text `unable to retrieve container logs for <id>` - a successful call whose body is an error message, not a raised error.

  Only the single most recent terminated instance is retained by the kubelet; there is no way to reach further back than one.

#### Returns

str: The log content as text, with real newlines. An empty string if the
container has produced no log output (never None).

### `get_deployment_summaries`

```python
get_deployment_summaries(namespace: Optional[str] = None) -> list[DeploymentSummary]
```

Retrieves a list of DeploymentSummary objects for deployments in a given namespace or all namespaces.
Similar to `kubectl get deployements`.

#### Parameters

- **`namespace`** (`Optional[str], default=None`): The specific namespace to list deployments from. If None, lists deployments from all namespaces.

#### Returns

list of DeploymentSummary
A list of DeploymentSummary objects, each providing a summary of a deployment's status with the following fields:

- **`name`** (`str`): Name of the deployment.
- **`namespace`** (`str`): Namespace in which the deployment is running.
- **`total_replicas`** (`int`): Total number of replicas desired for this deployment.
- **`ready_replicas`** (`int`): Number of replicas that are currently ready.
- **`up_to_date_replicas`** (`int`): Number of replicas that are up to date.
- **`available_replicas`** (`int`): Number of replicas that are available.
- **`age`** (`datetime.timedelta`): Age of the deployment (current time minus creation timestamp).

### `get_replicaset_summaries`

```python
get_replicaset_summaries(namespace: Optional[str] = None, deployment: Optional[str] = None) -> list[ReplicaSetSummary]
```

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

Replica sets with no revision (see `revision` below) sort *first*, ahead of
revision 1, rather than being dropped. They have no place in any deployment's
history, so they are listed before it rather than appended to it. In practice
they are only visible in an unfiltered listing: a replica set without a
revision has no owning deployment either, so passing `deployment` filters them
out, and the "last entry is the current revision" guarantee above is unaffected.

#### Parameters

- **`namespace`** (`Optional[str], default=None`): The specific namespace to list replica sets from. If None, lists from all namespaces.
- **`deployment`** (`Optional[str], default=None`): If given, return only replica sets owned by this deployment. This is the common case: one deployment's revision history.

#### Returns

list of ReplicaSetSummary
A list of ReplicaSetSummary objects with the following fields:

- **`name`** (`str`): Name of the replica set.
- **`namespace`** (`str`): Namespace in which the replica set is defined.
- **`owner_deployment`** (`Optional[str]`): Name of the Deployment that owns this replica set, or None if it is standalone (not managed by a Deployment).
- **`revision`** (`Optional[int]`): The deployment revision this replica set represents, taken from the `deployment.kubernetes.io/revision` annotation. None when that annotation is absent, which means no Deployment created this replica set - only the Deployment controller writes it. A hand-written replica set, or one created by another controller, therefore has no revision (and no `owner_deployment`), and appears in neither `kubectl rollout history` nor a `deployment`-filtered call here. Also None if the annotation is present but not an integer.
- **`desired_replicas`** (`int`): Replicas desired for this replica set. Old revisions are scaled to 0.
- **`current_replicas`** (`int`): Replicas currently running.
- **`ready_replicas`** (`int`): Replicas currently ready.
- **`images`** (`list[str]`): Container images in this revision's pod template, in container order. Comparing this across revisions shows what an upgrade changed.
- **`age`** (`datetime.timedelta`): Age of the replica set (current time minus creation timestamp). For the newest revision this is usually how long ago the deployment last changed - but not after a rollback, or a return to an identical earlier template, which re-activate an existing replica set under a new revision number; `get_workload_history` flags those as reused.

### `get_workload_history`

```python
get_workload_history(name: str, namespace: str = 'default', kind: str = 'Deployment') -> WorkloadHistory
```

What changed in a workload, and when: its retained pod-template revisions,
newest first, each compared with the one before it, plus the ConfigMaps and
Secrets its pod template refers to. Ask early in an investigation: a recent
change is the first suspect, and a workload unchanged for months is a
finding too.

#### Parameters

- **`name`** (`str`): Name of the workload.
- **`namespace`** (`str, default="default"`): Namespace of the workload.
- **`kind`** (`str, default="Deployment"`): "Deployment", "StatefulSet" or "DaemonSet". A Deployment's revisions are its ReplicaSets; a StatefulSet's or DaemonSet's are its ControllerRevisions.

#### Returns

WorkloadHistory

- **`kind, name, namespace`** (`str`): The workload.
- **`revisions`** (`list[WorkloadRevision]`): Retained revisions, newest first. Each has
  - **`revision`** (`Optional[int]`): Revision number.
  - **`source`** (`str`): "ReplicaSet/<name>" or "ControllerRevision/<name>".
  - **`age`** (`datetime.timedelta`): Time since that ReplicaSet or ControllerRevision was created. This is when the revision first went live, unless `reused` is true.
  - **`current`** (`bool`): True for the revision the workload runs now (the highest number).
  - **`reused`** (`bool`): True if a rollback, or a return to an identical earlier template, re-activated this ReplicaSet under a new revision number. It went live later than its age says. Deployments only.
  - **`images`** (`list[str]`): Container images, in container order.
  - **`compared_with`** (`Optional[int]`): The previous retained revision, which `changes` are relative to; None for the oldest retained revision.
  - **`changes`** (`list[TemplateChange]`): What differs from that revision, each with `field`, `change` ("added", "removed", "changed") and, where shown, `before`/`after`. Compared per container (matched by name): image, resource requests and limits, command, args, probes, env vars (by name; values are never shown), envFrom, volume mounts; and for the pod: volumes, node selector, tolerations. Any other field that changed is named, with values only if they are plain scalars.
  - **`metadata_changes`** (`list[TemplateChange]`): Pod-template label and annotation changes, kept apart because tools like Helm change them on every upgrade (e.g. a chart version label). A changed "checksum/config"-style annotation often means the chart's config changed.
  - **`rollout_restart`** (`bool`): True if the kubectl.kubernetes.io/restartedAt annotation changed: a `kubectl rollout restart`, which changes nothing else.
- **`config`** (`list[ConfigReference]`): Each ConfigMap and Secret the current template names: kind, name, used_as ("env", "envFrom", "volume", "volume (subPath)", "imagePullSecrets"). For ConfigMaps also exists, age and last_written (time since its latest write by anyone, from managedFields - including label-only writes, so it does not prove the data changed). Secrets are never read: no time, and their changes are not visible here.

  How a ConfigMap change reaches a running pod: env and envFrom values are read only when a container starts, so a later write takes effect at the next restart; a mounted volume is refreshed within about a minute, except a subPath mount, which never is. Compare last_written with the containers' started_at (`get_pod_container_statuses`).
- **`complete`** (`bool`): False when replayed from a capture that predates this tool: revisions then come from its ReplicaSet records and show only image changes.
- **`limits`** (`list[str]`): What this history cannot show. It never sees changes made outside the pod template, such as replica counts or a feature-flag toggle.

### `get_service_summaries`

```python
get_service_summaries(namespace: Optional[str] = None) -> list[ServiceSummary]
```

Retrieves a list of ServiceSummary objects for services in a given namespace or all namespaces.
Similar to `kubectl get services`.

#### Parameters

- **`namespace`** (`Optional[str], default=None`): The specific namespace to list services from. If None, lists services from all namespaces.

#### Returns

list of ServiceSummary
A list of ServiceSummary objects, each providing a summary of a service's status with the following fields:

- **`name`** (`str`): Name of the service.
- **`namespace`** (`str`): Namespace in which the service is running.
- **`type`** (`str`): Type of the service (ClusterIP, NodePort, LoadBalancer, ExternalName).
- **`cluster_ip`** (`Optional[str]`): Cluster IP address assigned to the service (None for ExternalName services).
- **`external_ip`** (`Optional[str]`): External IP address if applicable (for LoadBalancer services).
- **`ports`** (`list[PortInfo]`): List of ports (and their protocols) exposed by the service.
- **`age`** (`datetime.timedelta`): Age of the service (current time minus creation timestamp).
- **`selector`** (`dict[str, str]`): The label selector the service uses to choose backing pods. Empty for services without a selector (e.g. ExternalName, or manually managed Endpoints).
- **`labels`** (`dict[str, str]`): Labels on the service object.
- **`annotations`** (`dict[str, str]`): Annotations on the service object.
- **`ready_endpoints`**, **`not_ready_endpoints`** (`Optional[int]`): its ready and not-ready backends, from `get_endpoint_summaries`. 0 for a Service whose selector matches no pods. None when unknown: an ExternalName Service, EndpointSlices not readable with the server's permissions, or a capture taken before 3.0.0.


### `get_endpoint_summaries`

```python
get_endpoint_summaries(namespace: Optional[str] = None) -> list[EndpointSummary]
```

Each Service's backends, from its EndpointSlices (`discovery.k8s.io/v1`, the
current API; core `v1 Endpoints` is deprecated and has no per-address state).
Answers "does this Service have anywhere to send traffic?", which the selector in
`get_service_summaries` can't on its own.

Slices are grouped by their `kubernetes.io/service-name` label (a slice without
one is listed under its own name), and a pod listed in two slices, as for a
dual-stack Service with one slice per IP family, is counted once.

#### Parameters

- **`namespace`** (`Optional[str]`): one namespace, or all if omitted.

#### Returns

list of EndpointSummary, each with:

- **`service`**, **`namespace`** (`str`)
- **`ports`** (`list[PortInfo]`): the endpoint ports, the pods' target ports rather than the Service's own.
- **`addresses`** (`list[EndpointAddress]`): one per backend:
  - **`ip`**: its first address.
  - **`ready`**: ready to receive traffic. An unset condition means ready, as the API defines.
  - **`serving`**: serving even if terminating; unset takes `ready`'s value.
  - **`terminating`**
  - **`pod`**: the backing pod's name, when the endpoint targets a pod.
  - **`node`**
- **`ready`**, **`not_ready`**, **`terminating`** (`int`): counts. A terminating endpoint counts as terminating, not as not ready.
- **`notes`** (`list[str]`): see [Notes in results](#notes-in-results).

### `get_ingress_summaries`

```python
get_ingress_summaries(namespace: Optional[str] = None) -> list[IngressSummary]
```

Ingresses (`networking.k8s.io/v1`), like `kubectl get ingress` and `describe`,
with each rule's backend resolved against the namespace's Services. A rule whose
Service or port doesn't exist is the most common Ingress mistake, and this shows
it directly instead of leaving it to be inferred from 503s. Gateway API routes
(`HTTPRoute`) aren't covered.

#### Parameters

- **`namespace`** (`Optional[str]`): one namespace, or all if omitted.

#### Returns

list of IngressSummary, each with:

- **`name`**, **`namespace`**
- **`ingress_class`** (`Optional[str]`): `spec.ingressClassName`, or the older `kubernetes.io/ingress.class` annotation.
- **`rules`** (`list[IngressRule]`): one per host and path:
  - **`host`**: None means any host.
  - **`path`**, **`path_type`**
  - **`service`**, **`port`**: the backend Service, and its port as written, a number or a port name.
  - **`resource`**: a resource backend, as "Kind/name", instead of a Service.
  - **`backend_problem`**: why the backend doesn't resolve, e.g. "Service 'legacy' not found" or "Service 'ad' has no port 9555"; None when it resolves.
  - **`backend_ready`**: the backend Service's ready endpoints, from `get_endpoint_summaries`; None when unknown.
- **`default_backend`** (`Optional[IngressRule]`): the backend for requests no rule matches, resolved the same way.
- **`tls`** (`list[IngressTLS]`): `hosts`, and `secret_name`: the certificate's Secret by name only; its contents are never read.
- **`load_balancer`** (`list[str]`): load-balancer IPs or hostnames.
- **`labels`**, **`annotations`**, **`age`**
- **`notes`** (`list[str]`): see [Notes in results](#notes-in-results).

### `get_configmap_summaries`

```python
get_configmap_summaries(namespace: Optional[str] = None) -> list[ConfigMapSummary]
```

Retrieves a list of ConfigMapSummary objects for ConfigMaps in a given namespace
or all namespaces, similar to `kubectl get configmaps`.

Note that this returns only summary metadata (not the ConfigMap contents); use
`get_configmap` to read the actual data map.

#### Parameters

- **`namespace`** (`Optional[str], default=None`): The specific namespace to list ConfigMaps from. If None, lists ConfigMaps from all namespaces.

#### Returns

list of ConfigMapSummary
A list of ConfigMapSummary objects, each with the following fields:

- **`name`** (`str`): Name of the ConfigMap.
- **`namespace`** (`str`): Namespace in which the ConfigMap lives.
- **`key_count`** (`int`): Number of keys across `data` and `binary_data`.
- **`data_size`** (`int`): Approximate total size in bytes of all values.
- **`age`** (`datetime.timedelta`): Age of the ConfigMap (current time minus creation timestamp).

### `get_configmap`

```python
get_configmap(name: str, namespace: str = 'default') -> dict[str, Any]
```

Retrieves the full contents of a single ConfigMap.

WARNING: ConfigMaps can contain secret-shaped values (e.g. credentials stored
as plain config). When this tool is served through the k8stools MCP server, the
output passes through a redaction step by default (see the `redaction` module
and the server's `--no-redact` flag). Callers using this function directly get
the raw values and are responsible for their own redaction.

#### Parameters

- **`name`** (`str`): Name of the ConfigMap.
- **`namespace`** (`str, optional`): Namespace of the ConfigMap (default is "default").

#### Returns

dict[str, Any]
A dictionary describing the ConfigMap with the following keys:

- **`name`** (`str`): Name of the ConfigMap.
- **`namespace`** (`str`): Namespace of the ConfigMap.
- **`data`** (`dict[str, str]`): The string key/value data map (empty dict if none).
- **`binary_data_keys`** (`list[str]`): Names of any binary keys. The binary values themselves are not returned.

### `get_statefulset_summaries`

```python
get_statefulset_summaries(namespace: Optional[str] = None) -> list[StatefulSetSummary]
```

Retrieves a list of StatefulSetSummary objects for StatefulSets in a given
namespace or all namespaces, similar to `kubectl get statefulsets`.

#### Parameters

- **`namespace`** (`Optional[str], default=None`): The specific namespace to list StatefulSets from. If None, lists from all namespaces.

#### Returns

list of StatefulSetSummary
A list of StatefulSetSummary objects, each with the following fields:

- **`name`** (`str`): Name of the StatefulSet.
- **`namespace`** (`str`): Namespace in which the StatefulSet runs.
- **`total_replicas`** (`int`): Desired number of replicas.
- **`ready_replicas`** (`int`): Number of replicas currently ready.
- **`current_replicas`** (`int`): Number of replicas created by the current revision.
- **`update_strategy`** (`str`): Update strategy type (e.g. "RollingUpdate", "OnDelete").
- **`service_name`** (`Optional[str]`): Name of the governing (headless) service.
- **`age`** (`datetime.timedelta`): Age of the StatefulSet (current time minus creation timestamp).

### `get_daemonset_summaries`

```python
get_daemonset_summaries(namespace: Optional[str] = None) -> list[DaemonSetSummary]
```

Retrieves a list of DaemonSetSummary objects for DaemonSets in a given
namespace or all namespaces, similar to `kubectl get daemonsets -o wide`.

A DaemonSet runs one pod on each node it targets; its pods are named
"<daemonset>-<5 characters>" and have `owner` "DaemonSet/<daemonset>" in
`get_pod_summaries`. A shortfall between the counts below ("desired 5,
ready 4") is often the first sign of a problem with a particular node.

#### Parameters

- **`namespace`** (`Optional[str], default=None`): The specific namespace to list DaemonSets from. If None, lists from all namespaces.

#### Returns

list of DaemonSetSummary
A list of DaemonSetSummary objects, each with the following fields:

- **`name`** (`str`): Name of the DaemonSet.
- **`namespace`** (`str`): Namespace in which the DaemonSet runs.
- **`desired_number_scheduled`** (`int`): Number of nodes that should be running the DaemonSet's pod - the actual number of nodes it targets.
- **`current_number_scheduled`** (`int`): Number of nodes running at least one of its pods that should.
- **`number_ready`** (`int`): Number of nodes whose pod is ready.
- **`updated_number_scheduled`** (`int`): Number of nodes running a pod from the current pod template. Less than desired means a rollout is in progress or stuck.
- **`number_available`** (`int`): Number of nodes whose pod has been ready for at least minReadySeconds.
- **`number_misscheduled`** (`int`): Number of nodes running its pod that should not be.
- **`node_selector`** (`dict[str, str]`): The pod template's nodeSelector (kubectl's NODE SELECTOR column). Many DaemonSets choose their nodes with node affinity and tolerations instead, which this does not show, so an empty selector does not mean "every node"; desired_number_scheduled is the count.
- **`update_strategy`** (`str`): Update strategy type ("RollingUpdate" or "OnDelete").
- **`images`** (`list[str]`): Container images of the current pod template, in container order. A pod running a different image is from an earlier revision.
- **`age`** (`datetime.timedelta`): Age of the DaemonSet (current time minus creation timestamp).
- **`notes`** (`list[str]`): Warnings that apply to this item; empty when none do. See [Notes in results](#notes-in-results).

### `get_hpa_summaries`

```python
get_hpa_summaries(namespace: Optional[str] = None) -> list[HpaSummary]
```

HorizontalPodAutoscalers, from `autoscaling/v2` (the v1 API has no per-metric
status): like `kubectl get hpa` and the condition and metric detail of `kubectl
describe hpa`. Answers "why did this scale, or not?", which workload history
can't: replica counts and autoscaling aren't part of a pod template.

When it scaled is in its events: `get_events(involved_kind="HorizontalPodAutoscaler")`
returns its `SuccessfulRescale` events, with counts.

#### Parameters

- **`namespace`** (`Optional[str]`): one namespace, or all if omitted.

#### Returns

list of HpaSummary, each with:

- **`name`**, **`namespace`** (`str`)
- **`scale_target`** (`str`): what it scales, as "Kind/name" (e.g. "Deployment/cart"), the same form as `PodSummary.owner`, so it joins with `get_workload_history`.
- **`min_replicas`** (`Optional[int]`): None means the API default, 1.
- **`max_replicas`**, **`current_replicas`**, **`desired_replicas`** (`int`)
- **`metrics`** (`list[HpaMetric]`): one per metric it scales on:
  - **`type`**: Resource, ContainerResource, Pods, Object or External.
  - **`name`**: e.g. "cpu", "memory (container app)", "requests_per_second on Ingress/main".
  - **`target`**: "80%" (average utilization, as a percentage of the pods' requests), "500m (average)" (average value per pod) or "10k" (total value).
  - **`current`**: the current reading in the target's own terms; None when the HPA has none (e.g. metrics unavailable, see ScalingActive).
- **`conditions`** (`list[HpaCondition]`): `type`, `status`, `reason`, `message`, and `since` (time since its status last changed). `AbleToScale`, `ScalingActive` (False: it can't compute a scale, often missing metrics) and `ScalingLimited` (True: the desired count was capped at min or max; the reason says which).
- **`last_scale`** (`Optional[timedelta]`): time since it last changed the replica count.
- **`age`** (`timedelta`)
- **`notes`** (`list[str]`): see [Notes in results](#notes-in-results).

### `get_container_metrics`

```python
get_container_metrics(namespace: Optional[str] = None) -> list[ContainerUsage]
```

Each running container's current CPU and memory use, from metrics-server
(`metrics.k8s.io/v1beta1`, like `kubectl top pod --containers`), beside its
requests and limits. Answers "how close is this container to its limit?". It's
per container, not per pod, because limits apply per container: a pod total
hides the container that's near its limit.

**What a reading is, and isn't.** metrics-server reports a recent average over a
short window (`window`, typically 15s to 1m), sampled every scrape interval.
Several things follow:

- **A spike can be missed.** A spike that ends in an OOM kill usually never
  appears in a sample.
- **A fresh instance reads low.** A container that just restarted reports its
  new instance's low usage, so "140Mi of 300Mi" right after an OOM kill doesn't
  mean memory wasn't the problem.
- **Memory is the working set:** memory in use plus recently used file cache,
  which the kernel reclaims before an OOM kill. Near the limit is common.

Each reading's `notes` say which of these apply to it.

**Without metrics-server**, the tool raises `K8sMetricsUnavailable` (a
`K8sApiError`), a normal setup rather than a fault. A permission error on the
metrics API is an ordinary `K8sApiError`.

**Replayed from a capture**, readings keep their values while `sampled`
grows, so a capture replayed a day later honestly reports "sampled 1d ago". A
capture made where metrics-server wasn't available raises the same
`K8sMetricsUnavailable` as live. One made before 3.0.0 raises it with
"predates resource metrics".

#### Parameters

- **`namespace`** (`Optional[str]`): one namespace, or all if omitted.

#### Returns

list of ContainerUsage, one per container of each running or pending pod
(finished pods have nothing to measure), each with:

- **`pod`**, **`namespace`**, **`container`**
- **`cpu_millicores`**, **`memory_bytes`** (`Optional[int]`): whole numbers for calculating; None when there's no reading (not running, or started within about one scrape interval).
- **`cpu`**, **`memory`** (`Optional[str]`): `kubectl top`-style display, e.g. "150m", "140Mi".
- **`cpu_request_millicores`**, **`cpu_limit_millicores`**, **`memory_request_bytes`**, **`memory_limit_bytes`** (`Optional[int]`): the container's requests and limits in the same units.
- **`memory_percent_of_limit`**, **`cpu_percent_of_request`**, **`cpu_percent_of_limit`** (`Optional[float]`): one decimal. CPU against its request is what an HPA's utilization target measures.
- **`sampled`** (`Optional[timedelta]`): time since the sample was taken.
- **`window`** (`Optional[timedelta]`): the span the sample averages; a fixed span, which doesn't grow on replay.
- **`started`** (`Optional[timedelta]`): time since the current instance started. Compare it with `sampled` to see which instance a reading is from.
- **`restarts`** (`int`), **`last_termination_reason`** (`Optional[str]`), **`last_terminated`** (`Optional[timedelta]`): how the last instance ended, and when.
- **`notes`** (`list[str]`): see [Notes in results](#notes-in-results).

### `get_node_metrics`

```python
get_node_metrics() -> list[NodeUsage]
```

Each node's current CPU and memory use, from metrics-server (like `kubectl top
node`), against its allocatable. The same sampling caveats and
`K8sMetricsUnavailable` behavior as `get_container_metrics` apply.

#### Returns

list of NodeUsage, each with:

- **`node`**
- **`cpu_millicores`**, **`memory_bytes`** (`int`), and display strings **`cpu`**, **`memory`**.
- **`cpu_allocatable_millicores`**, **`memory_allocatable_bytes`** (`Optional[int]`), and **`cpu_percent`**, **`memory_percent`** of them.
- **`sampled`**, **`window`**: as for containers.

### `get_cronjob_summaries`

```python
get_cronjob_summaries(namespace: Optional[str] = None) -> list[CronJobSummary]
```

Retrieves a list of CronJobSummary objects for CronJobs in a given namespace
or all namespaces, similar to `kubectl get cronjobs`.

#### Parameters

- **`namespace`** (`Optional[str], default=None`): The specific namespace to list CronJobs from. If None, lists from all namespaces.

#### Returns

list of CronJobSummary
A list of CronJobSummary objects, each with the following fields:

- **`name`** (`str`): Name of the CronJob.
- **`namespace`** (`str`): Namespace of the CronJob.
- **`schedule`** (`str`): Cron schedule expression.
- **`suspend`** (`bool`): Whether the CronJob is suspended.
- **`active`** (`int`): Number of currently active (running) jobs.
- **`last_schedule_time`** (`Optional[datetime.timedelta]`): Time since the CronJob was last scheduled (None if never).
- **`last_successful_time`** (`Optional[datetime.timedelta]`): Time since the CronJob last completed successfully (None if never).
- **`age`** (`datetime.timedelta`): Age of the CronJob (current time minus creation timestamp).
- **`containers`** (`list[ContainerTemplateSummary]`): The job pod template's containers, each with name, image, and literal environment values.

### `get_job_summaries`

```python
get_job_summaries(namespace: Optional[str] = None) -> list[JobSummary]
```

Retrieves a list of JobSummary objects for Jobs in a given namespace or all
namespaces, similar to `kubectl get jobs`.

#### Parameters

- **`namespace`** (`Optional[str], default=None`): The specific namespace to list Jobs from. If None, lists from all namespaces.

#### Returns

list of JobSummary
A list of JobSummary objects, each with the following fields:

- **`name`** (`str`): Name of the Job.
- **`namespace`** (`str`): Namespace of the Job.
- **`owner`** (`Optional[str]`): Name of the owning CronJob, if this Job was created by one.
- **`active`** (`int`): Number of actively running pods.
- **`succeeded`** (`int`): Number of pods that completed successfully.
- **`failed`** (`int`): Number of pods that terminated in failure.
- **`start_time`** (`Optional[datetime.timedelta]`): Time since the Job started (None if not started).
- **`completion_time`** (`Optional[datetime.timedelta]`): Time since the Job completed (None if not complete).
- **`conditions`** (`list[str]`): Status condition types currently True (e.g. ["Complete"], ["Failed"]).
- **`age`** (`datetime.timedelta`): Age of the Job (current time minus creation timestamp).
- **`containers`** (`list[ContainerTemplateSummary]`): The Job pod template's containers, each with name, image, and literal environment values.

### `get_custom_resource_definitions`

```python
get_custom_resource_definitions() -> list[CrdSummary]
```

The custom kinds the cluster serves: the discovery step before
`get_custom_resource_status`.

#### Returns

list of CrdSummary, sorted by name, each with **`name`** ("certificates.cert-manager.io"),
**`group`**, **`kind`**, **`plural`**, **`scope`** ("Namespaced" or "Cluster"),
**`versions`** (served ones), **`storage_version`**, **`short_names`** and **`age`**.

### `get_custom_resource_status`

```python
get_custom_resource_status(group: str, plural: str, version: Optional[str] = None, namespace: Optional[str] = None) -> list[CustomResourceStatus]
```

Custom resources' health, the part of an operator's objects that matters for
root-cause analysis: their `status.conditions` (Ready, Synced, ...) and whether
the controller has caught up with the spec. Deliberately not the objects
themselves. Raw custom resources can be thousands of lines each (Argo, Crossplane,
Flux), some operators keep credentials in spec, and reading them generally needs
wildcard permissions.

#### Parameters

- **`group`**, **`plural`**: from `get_custom_resource_definitions`, e.g. "cert-manager.io", "certificates".
- **`version`**: defaults to the CRD's stored version.
- **`namespace`**: one namespace, or all if omitted.

#### Returns

list of CustomResourceStatus, each with:

- **`kind`**, **`name`**, **`namespace`** (None for a cluster-scoped resource), **`age`**
- **`generation`**, **`observed_generation`** (`Optional[int]`): `status.observedGeneration`, or the highest a condition reports, as many operators record it per condition. Below `generation`, the controller hasn't acted on the latest spec change yet.
- **`conditions`** (`list[ResourceCondition]`): `type`, `status`, `reason`, `message`, and `since` (time since its status last changed).
- **`notes`** (`list[str]`): see [Notes in results](#notes-in-results).

A type the server can't read raises `K8sApiError`, as does one that doesn't exist.
Captures store each type's instances (up to 500) or, when a type wasn't readable
with the capture's permissions, the reason, which replay raises as a live call
would.

### `get_logs_for_job`

```python
get_logs_for_job(job_name: str, namespace: str = 'default', container_name: Optional[str] = None, tail: Optional[int] = None, since_seconds: Optional[int] = None, previous: bool = False) -> Optional[str]
```

Retrieves logs from the most-recently-created pod of a Job.

Job pods are short-lived, so this convenience finds the newest pod belonging to
the Job (via its `job-name` label) and returns its logs, saving the caller from
listing pods and sorting by creation time.

#### Parameters

- **`job_name`** (`str`): Name of the Job.
- **`namespace`** (`str, optional`): Namespace of the Job (default is "default").
- **`container_name`** (`str, optional`): Container within the pod. If None, defaults to the first container.
- **`tail`** (`int, optional`): Number of lines from the end of the log (default: last 1000).
- **`since_seconds`** (`int, optional`): If set, only return logs newer than this many seconds.
- **`previous`** (`bool, default False`): If True, return logs from the previous terminated container instance. See `get_logs_for_pod_and_container` for the cases - CrashLoopBackOff, a restart racing the call, a reclaimed log file - where that legitimately returns the same text as `previous=False`, or a 200 whose body is an error message.

#### Returns

str, optional
Log content, or None if the Job has no pods.

### `get_logs_for_cronjob`

```python
get_logs_for_cronjob(cronjob_name: str, namespace: str = 'default', container_name: Optional[str] = None, tail: Optional[int] = None, since_seconds: Optional[int] = None, previous: bool = False) -> Optional[str]
```

Retrieves logs from the most-recent run of a CronJob.

Finds the newest Job owned by the CronJob, then returns the logs of that Job's
most-recently-created pod. Removes the need to manually locate the right
short-lived pod for a CronJob's last tick.

#### Parameters

- **`cronjob_name`** (`str`): Name of the CronJob.
- **`namespace`** (`str, optional`): Namespace of the CronJob (default is "default").
- **`container_name`** (`str, optional`): Container within the pod. If None, defaults to the first container.
- **`tail`** (`int, optional`): Number of lines from the end of the log (default: last 1000).
- **`since_seconds`** (`int, optional`): If set, only return logs newer than this many seconds.
- **`previous`** (`bool, default False`): If True, return logs from the previous terminated container instance. See `get_logs_for_pod_and_container` for the cases - CrashLoopBackOff, a restart racing the call, a reclaimed log file - where that legitimately returns the same text as `previous=False`, or a 200 whose body is an error message.

#### Returns

str, optional
Log content, or None if the CronJob has no jobs/pods yet.

### `get_pvc_summaries`

```python
get_pvc_summaries(namespace: Optional[str] = None) -> list[PVCSummary]
```

Retrieves a list of PVCSummary objects for PersistentVolumeClaims in a given
namespace or all namespaces, similar to `kubectl get pvc`.

For each PVC, this also resolves which pods currently mount it (by scanning pod
volumes in the same scope), which is useful for spotting orphaned PVCs — claims
with an empty `mounted_by` and no owning pod.

#### Parameters

- **`namespace`** (`Optional[str], default=None`): The specific namespace to list PVCs from. If None, lists from all namespaces.

#### Returns

list of PVCSummary
A list of PVCSummary objects, each with the following fields:

- **`name`** (`str`): Name of the PVC.
- **`namespace`** (`str`): Namespace of the PVC.
- **`status`** (`str`): Phase of the PVC ("Bound", "Pending", or "Lost").
- **`volume_name`** (`Optional[str]`): Name of the bound PersistentVolume (None if unbound).
- **`capacity`** (`Optional[str]`): Storage capacity (e.g. "10Gi"); falls back to the requested size when the claim is not yet bound.
- **`access_modes`** (`list[str]`): Access modes (e.g. ["ReadWriteOnce"]).
- **`storage_class`** (`Optional[str]`): StorageClass backing the claim.
- **`mounted_by`** (`list[str]`): Names of pods (in the same scope) currently mounting this PVC. Empty for orphaned/unmounted claims.
- **`age`** (`datetime.timedelta`): Age of the PVC (current time minus creation timestamp).

### `get_events`

```python
get_events(namespace: Optional[str] = None, reason: Optional[str] = None, involved_kind: Optional[str] = None, involved_name: Optional[str] = None, event_type: Optional[str] = None) -> list[EventSummary]
```

Lists cluster- or namespace-wide events with optional server-side filtering.

Unlike `get_pod_events` (which is scoped to a single named pod), this supports
the sweep queries common in capacity/storage runbooks — e.g. all "Evicted"
events, or all "FailedScheduling" events — where the affected pods often have no
stable name to look up. Note Kubernetes expires an event record about an hour
(by default) after its last occurrence, so events that stopped repeating
earlier than that are gone.

#### Parameters

- **`namespace`** (`Optional[str], default=None`): Namespace to list events from. If None, lists across all namespaces.
- **`reason`** (`Optional[str], default=None`): If set, only return events with this reason (e.g. "Evicted", "FailedScheduling", "BackOff").
- **`involved_kind`** (`Optional[str], default=None`): If set, only return events whose involved object is of this kind (e.g. "Pod", "Node", "PersistentVolumeClaim").
- **`involved_name`** (`Optional[str], default=None`): If set, only return events whose involved object has this name.
- **`event_type`** (`Optional[str], default=None`): If set, only return events of this type ("Normal" or "Warning").

#### Returns

list of EventSummary
Matching events. Each EventSummary has the following fields:

- **`last_seen`** (`Optional[datetime.timedelta]`): Time since the event was last seen (if available).
- **`first_seen`** (`Optional[datetime.timedelta]`): Time since the first occurrence combined into this record (if available).
- **`count`** (`Optional[int]`): How many occurrences Kubernetes combined into this record: repeats of the same event on the same object are counted in one record rather than listed separately. The count covers first_seen to last_seen, not the object's lifetime - a record that stops repeating expires (after 1h by default), and a later repeat starts a new one. For a crash-looping container, count the "Created" or "Started" events to get restarts. "BackOff" is emitted repeatedly while the kubelet waits to restart, so its count is several times the number of restarts. count divided by (first_seen - last_seen) is the average rate over that window, not the current back-off.

  A record can lag behind what it counts. By default the kubelet writes at most one event update per object and event type ("Normal" or "Warning") every 5 minutes, once a burst of 25 is used up; occurrences in between are counted but only written with the next update, which then jumps by several at once. So count and last_seen describe the most recent *written* occurrence and can trail reality by several occurrences and tens of minutes. They lag together, so the rate above still holds, but last_seen is not the time of the last restart: for that, use the container status (last_state.finished_at, state.started_at from get_pod_container_statuses) or PodSummary.last_restart, which come from the kubelet's status rather than from events. "Pulled", "Created" and "Started" share one write budget, so their counts for the same restarts can differ by a few; don't compare counts across reasons.
- **`type`** (`str`): Type of the event ("Normal" or "Warning").
- **`reason`** (`str`): Reason for the event.
- **`object`** (`str`): The involved object as "Kind/name" (or just the name when the kind is unavailable).
- **`message`** (`str`): Message describing the event.
- **`notes`** (`list[str]`): Warnings that apply to this item; empty when none do. See [Notes in results](#notes-in-results).

