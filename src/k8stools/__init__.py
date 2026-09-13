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
* get_node_summaries - get a list of nodes, like `kubectl get nodes -o wide`
* get_pod_summaries - get a list of pods, like `kubectl get pods -o wide`
* get_pod_container_statuses - return the status for each of the container in a pod
* get_pod_events - return the events for a pod
* get_pod_spec - retrieves the spec for a given pod
* get_logs_for_pod_and_container - retrieves logs from a pod and container (supports tail/since/previous)
* get_deployment_summaries - get a list of deployments, like `kubectl get deployments`
* get_replicaset_summaries - get a deployment's revision history, like `kubectl get replicasets`
* get_service_summaries - get a list of services, like `kubectl get services`
* get_configmap_summaries - get a list of ConfigMaps, like `kubectl get configmaps`
* get_configmap - retrieve the full contents of a single ConfigMap
* get_statefulset_summaries - get a list of StatefulSets, like `kubectl get statefulsets`
* get_cronjob_summaries - get a list of CronJobs, like `kubectl get cronjobs`
* get_job_summaries - get a list of Jobs, like `kubectl get jobs`
* get_logs_for_job - retrieve logs from a Job's most-recent pod
* get_logs_for_cronjob - retrieve logs from a CronJob's most-recent run
* get_pvc_summaries - get a list of PersistentVolumeClaims, like `kubectl get pvc`
* get_events - list cluster/namespace-wide events with server-side filtering

We also define a set of associated "print_" functions that are helpful in debugging:

* print_namespaces
* print_node_summaries
* print_pod_summaries
* print_pod_container_statuses
* print_pod_events
* print_pod_spec
* print_deployment_summaries
* print_replicaset_summaries
* print_service_summaries
* print_configmap_summaries
* print_configmap
* print_statefulset_summaries
* print_cronjob_summaries
* print_job_summaries
* print_pvc_summaries
* print_events

Secret redaction of tool output (on by default in the MCP server) is provided by
k8stools.redaction. The tool functions are defined in k8stools.k8s_tools.
k8stools.mcp_server can be run to start an MCP server based on these
tools. It can be called directly through the script k8s-mcp-server.
k8stools.mcp_client is a test client that starts the server and makes
a list_tools request through the stdio transport.

For testing without a cluster, k8stools.capture (the k8s-capture-state script)
snapshots a live cluster to a JSON file and k8stools.mock_state.MockState replays
it through the same tool surface. k8stools.mock_tools serves such a capture -
the built-in OTel Demo snapshot by default, or your own via load_mock_state() or
the server's --state-file option.
"""

__version__ = "2.0.1"
