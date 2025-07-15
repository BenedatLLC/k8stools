# Kubernetes Tools

This package provides a collection of functions to be used as tools for an Agent. They can be called
directly as functions or placed behind an MCP server (included). Some use cases
include:
* Chat with your kubernetes cluster via GitHub CoPilot or Cursor.
* Build agents to monitor your cluster or perform root cause analysis.
* Use in non-agentic automations.

Currently, the priority is on functions that do not modify the state of the cluster.
We want to focus first on the monitoring / RCA use cases. When we do add tools to address
other use cases, they will be kept separate from the read-only tools so you can still build
"safe" agents. In general, our goal is to focus on quality over quantity -- providing
well-documented and strongly typed tools. We believe that this is a critical in enabling
agents to make effective use of tools, beyond simple demos.

These are built on top of the kubernetes Python API (https://github.com/kubernetes-client/python).
There are three styles of tools provided here:
1. There are tools that mimic the output of kubectl commands (e.g. `get_pod_summaries`, which is equivalent
   to `kubectl get pods`).  Strongly-typed Pydantic models are used for the return values of these tools.
2. There are tools that return strongly typed Pydantic models that attempt to match the associated Kubernetes
   client types (see https://github.com/kubernetes-client/python/tree/master/kubernetes/docs).
   Lesser used fields may be omitted from these models. An example of this case is `get_pod_container_statuses`.
3. In some cases we simply call `to_dict()` on the class returned by the API (defined in 
   https://github.com/kubernetes-client/python/tree/master/kubernetes/client/models).
   The return type is `dict[str,Any]`, but we document the fields in the function's docstring.
   `get_pod_spec` is an example of this type of tool.

## Current tools

These are the tools we define:

* `get_namespaces` - get a list of namespaces, like `kubectl get namespace`
* `get_pod_summar`ies` - get a list of pods, like `kubectl get pods`
* `get_pod_container_statuses` - return the status for each of the container in a pod
* `get_pod_events` - return the events for a pod
* `get_pod_spec` - retrieves the spec for a given pod
* `get_logs_for_pod_and_container` - retrieves logs from a pod and container

We also define a set of associated "print_" functions that are helpful in debugging:

* `print_namespaces`
* `print_pod_summaries`
* `print_pod_container_statuses`
* `print_pod_events`
* `print_pod_spec`
