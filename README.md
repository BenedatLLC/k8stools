# Kubernetes Tools

![Unit Tests](https://github.com/BenedatLLC/k8stools/actions/workflows/unit-tests.yml/badge.svg)

k8stools gives AI agents read-only access to a Kubernetes cluster. It provides
31 tools: from "what is wrong in this namespace?" down to a single pod's spec or
logs. You can use them in two ways:

* **As an MCP server** (`k8s-mcp-server`, included), for Claude Code, Cursor,
  GitHub Copilot or any other MCP client.
* **As Python functions**, passed straight to an agent framework such as
  pydantic-ai, or called from ordinary scripts.

Some use cases:
* Chat with your Kubernetes cluster from your coding agent.
* Build [agents](https://github.com/BenedatLLC/orca-agent/) that monitor your cluster or perform root cause analysis.
* Build a custom chat UI, or use the tools in non-agentic automations.
* Snapshot a live cluster to a file and replay it later as a test fixture, with no
  cluster needed. See [Capturing and replaying a cluster](#capturing-and-replaying-a-cluster).

Every tool only reads. None changes the cluster, and none reads Kubernetes
`Secret` objects. Secret-shaped values in other objects (ConfigMaps, env vars,
command lines, logs) are [redacted](#secret-redaction) by default.

<a href="https://glama.ai/mcp/servers/@BenedatLLC/k8stools">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/@BenedatLLC/k8stools/badge" alt="Kubernetes Tools Server MCP server" />
</a>

## Contents

* [Quick start](#quick-start)
* [Toolsets: choosing which tools an agent sees](#toolsets-choosing-which-tools-an-agent-sees)
* [The tools](#the-tools)
* [Running the MCP server](#running-the-mcp-server)
* [Using the tools from Python](#using-the-tools-from-python)
* [Mock data](#mock-data)
* [Capturing and replaying a cluster](#capturing-and-replaying-a-cluster)
* [Secret redaction](#secret-redaction)
* [Design approach](#design-approach)
* [Development](#development)

What changed in each release is in the
[changelog](https://github.com/BenedatLLC/k8stools/blob/main/CHANGELOG.md).

## Quick start

### 1. Install

k8stools needs Python 3.12 or newer.

```sh
pip install k8stools      # or: uv add k8stools
```

This installs three commands:

| Command | What it does |
|---|---|
| `k8s-mcp-server` | The MCP server |
| `k8s-capture-state` | Snapshots a cluster to a file that the server can replay |
| `k8s-mcp-client` | Lists the server's tools and their schemas, for manual checking |

### 2. Try it without a cluster

`--mock` serves a built-in snapshot of a small cluster that has a few things wrong
with it (see [Mock data](#mock-data)), so you can see what the tools do before
pointing them at anything real. To add it to Claude Code:

```sh
claude mcp add k8stools-mock -- k8s-mcp-server --mock
```

Then ask something like *"What's wrong in the default namespace?"*

For VS Code, Cursor or another client that reads an `mcp.json`, see
[Connecting an MCP client](#connecting-an-mcp-client).

### 3. Point it at your cluster

Without `--mock`, the server uses the same cluster as `kubectl`: the file in
`KUBECONFIG` (or `~/.kube/config`) and its current context. To choose a different
one, pass `--kubeconfig` and/or `--context`:

```sh
claude mcp add k8stools -- k8s-mcp-server --context my-cluster
```

The account it runs as needs only `get` and `list` permissions. See
[Permissions](#permissions) for a ClusterRole that covers every tool, and
[Selecting a cluster](#selecting-a-cluster) for the details.

## Toolsets: choosing which tools an agent sees

An MCP client sends every tool's description to the model on every turn, and
agents choose less reliably when they have many similar tools to pick from. So the
server doesn't have to offer all 31 tools: it serves a named **toolset**, chosen
with `--toolset`:

| Toolset | What it contains | Use it when |
|---|---|---|
| `triage` | 5 tools: `get_cluster_info`, `get_namespace_health`, `get_workload_report`, `get_events`, `get_node_summaries` | The agent's job is to find what's wrong and where. The two *composite* tools answer in one call what otherwise takes five or six. |
| `investigate` **(default)** | 27 tools: every tool except the four that another tool already covers | The agent needs to dig into details: specs, history, logs, networking, storage. |
| `all` | All 31 tools | You want the full set, as k8stools 2.x served. |

The four tools that `investigate` leaves out, and what to use instead:

| Left out | Use instead |
|---|---|
| `get_pod_events` | `get_events(involved_name=...)` |
| `get_replicaset_summaries` | `get_workload_history` |
| `get_logs_for_job`, `get_logs_for_cronjob` | `get_logs_for_pod_and_container`; each pod's `owner` names its Job |

You can adjust any toolset with `--include TOOL` and `--exclude TOOL`. Both can be
repeated:

```sh
# The triage tools, plus pod specs and container logs
k8s-mcp-server --toolset triage --include get_pod_spec --include get_logs_for_pod_and_container

# Everything except ConfigMap contents
k8s-mcp-server --toolset all --exclude get_configmap
```

A misspelled toolset or tool name stops the server at startup, rather than
quietly serving a different set. The [table of tools](#the-tools) below shows which
toolsets each tool is in. In Python, the same sets are in `k8s_tools.TOOLSETS`
(see [Choosing tools in Python](#choosing-tools-in-python)).

## The tools

Each tool's description, which is what the model sees, is kept to a few lines.
[docs/TOOL_REFERENCE.md](https://github.com/BenedatLLC/k8stools/blob/main/docs/TOOL_REFERENCE.md)
documents every parameter and field.

Warnings that apply to a particular result come back *in* that result, rather than
in the description: nodes, pods, container statuses, events and DaemonSets have a
`notes` list (for example, that a node's Ready time is not its uptime), and a log
can begin with `[k8stools] note:` lines (for example, that the "previous" log is
the same as the current one). See
[Notes in results](https://github.com/BenedatLLC/k8stools/blob/main/docs/TOOL_REFERENCE.md#notes-in-results).

In the tables below, **T** means the tool is in the `triage` toolset and **I** that
it is in `investigate`. Every tool is in `all`.

### Start here: overview tools

| Tool | What it answers | T | I |
|---|---|:-:|:-:|
| `get_namespace_health` | What is wrong in a namespace, and where. A full entry for each unhealthy workload (last termination with the exit code's meaning, how long instances live, the restart back-off, memory limit vs request, when the template last changed), pods that can't be scheduled and a Deployment's failing rollout conditions, the same for each *recovering* one (ready, but restarted within the last 24 hours), and one line for each healthy one. Workloads failing the same way are grouped. | ✓ | ✓ |
| `get_workload_report` | Everything about one workload in one call: images, resources and probes; each instance's state and last termination; deduplicated events; current and previous log tails; the last template change; the ConfigMaps and Secrets it uses; its Services, Ingress rules, HPA and resource usage. | ✓ | ✓ |
| `get_cluster_info` | Which cluster the tools are answering from: kubeconfig context, API server URL and version, or the capture being replayed. | ✓ | ✓ |

These *composite* tools are built from the other tools, and report facts rather
than diagnoses: they say "exit code 137, without OOMKilled", not "this was an
out-of-memory kill".

### Cluster and nodes

| Tool | What it answers | T | I |
|---|---|:-:|:-:|
| `get_namespaces` | The namespaces, like `kubectl get namespace` | | ✓ |
| `get_node_summaries` | The nodes, like `kubectl get nodes -o wide`, with capacity, allocatable, conditions, taints and labels | ✓ | ✓ |
| `get_node_metrics` | Each node's current CPU and memory use against its allocatable, like `kubectl top node` (needs metrics-server) | | ✓ |
| `get_events` | Cluster- or namespace-wide events, filtered on the server (by object, reason, type) | ✓ | ✓ |

### Pods, containers and logs

| Tool | What it answers | T | I |
|---|---|:-:|:-:|
| `get_pod_summaries` | The pods, like `kubectl get pods -o wide`, with each pod's controlling owner (e.g. `DaemonSet/otel-collector-agent`) | | ✓ |
| `get_pod_container_statuses` | Each container's state in a pod: running, waiting or terminated, restarts, last termination. Init containers are listed separately (`[]` when there are none); native sidecars are listed with the app containers. | | ✓ |
| `get_pod_spec` | A pod's full spec | | ✓ |
| `get_pod_events` | The events for one pod | | |
| `get_logs_for_pod_and_container` | A container's logs, with `tail`, `since_seconds` and `previous` (the instance before the last restart). See [Previous-instance log semantics](#previous-instance-log-semantics). | | ✓ |
| `get_container_metrics` | Each running container's current CPU and memory use beside its requests and limits, like `kubectl top pod --containers` (needs metrics-server) | | ✓ |

### Workloads

| Tool | What it answers | T | I |
|---|---|:-:|:-:|
| `get_deployment_summaries` | Deployments, like `kubectl get deployments` | | ✓ |
| `get_statefulset_summaries` | StatefulSets, like `kubectl get statefulsets` | | ✓ |
| `get_daemonset_summaries` | DaemonSets, like `kubectl get daemonsets -o wide`, with node selector and images | | ✓ |
| `get_workload_history` | What changed in a Deployment, StatefulSet or DaemonSet, and when: each retained revision compared with the one before (images, resources, probes, command and args, env var *names*, volumes, scheduling), plus the ConfigMaps and Secrets it references. It also says what the history cannot show. | | ✓ |
| `get_replicaset_summaries` | A Deployment's ReplicaSets with their revision numbers and images | | |
| `get_hpa_summaries` | HorizontalPodAutoscalers, like `kubectl get hpa` plus `describe`: target, min/max/current/desired replicas, each metric against its target, and the scaling conditions | | ✓ |
| `get_cronjob_summaries` | CronJobs, like `kubectl get cronjobs` | | ✓ |
| `get_job_summaries` | Jobs, like `kubectl get jobs` | | ✓ |
| `get_logs_for_job` | Logs from a Job's most recent pod | | |
| `get_logs_for_cronjob` | Logs from a CronJob's most recent run | | |

### Networking

| Tool | What it answers | T | I |
|---|---|:-:|:-:|
| `get_service_summaries` | Services, like `kubectl get services`, with selector, labels, annotations, and ready / not-ready endpoint counts | | ✓ |
| `get_endpoint_summaries` | Each Service's backends, from its EndpointSlices: every address's ready / serving / terminating state, pod and node | | ✓ |
| `get_ingress_summaries` | Ingresses, like `kubectl get ingress` plus `describe`: each host/path rule with its backend Service and port resolved (flagged when the Service or port doesn't exist), TLS hosts and Secret names, load-balancer addresses | | ✓ |

### Configuration and storage

| Tool | What it answers | T | I |
|---|---|:-:|:-:|
| `get_configmap_summaries` | ConfigMaps, like `kubectl get configmaps` | | ✓ |
| `get_configmap` | The full contents of one ConfigMap (redacted by the server) | | ✓ |
| `get_pvc_summaries` | PersistentVolumeClaims, like `kubectl get pvc`, with the pods that mount each one, and an unbound claim's events | | ✓ |

### Custom resources

| Tool | What it answers | T | I |
|---|---|:-:|:-:|
| `get_custom_resource_definitions` | The custom kinds the cluster serves (CRDs): group, kind, plural, scope, versions | | ✓ |
| `get_custom_resource_status` | Custom resources' health: each instance's conditions (Ready, Synced, ...) and whether its controller has caught up with its spec. Not the objects themselves, which can be huge and can hold credentials. | | ✓ |

## Running the MCP server

### Connecting an MCP client

The server uses the **stdio** transport by default, which is what local agents
such as Claude Code, Cursor and VS Code expect: the client starts the server
itself.

**Claude Code:**

```sh
claude mcp add k8stools -- k8s-mcp-server
# with options:
claude mcp add k8stools -- k8s-mcp-server --context prod-eu --toolset triage
```

**VS Code, Cursor and other clients with an `mcp.json`:**

```json
{
   "servers": {
      "k8stools": {
         "command": "${workspaceFolder}/.venv/bin/k8s-mcp-server",
         "args": ["--toolset", "investigate"],
         "envFile": "${workspaceFolder}/.envrc"
      }
   }
}
```

This assumes that:
1. k8stools is installed in a virtual environment in `.venv`, at the root of your workspace.
2. The environment file `.envrc` sets any variables you need: for example
   `KUBECONFIG` to point to your kubeconfig file and `K8STOOLS_CONTEXT` to pin a
   context. You can pass `--kubeconfig` and `--context` in `args` instead.

### Command-line options

The options, grouped by what they're for:

| Purpose | Options |
|---|---|
| Which tools to serve | `--toolset {triage,investigate,all}` (default `investigate`), `--include TOOL`, `--exclude TOOL`. See [Toolsets](#toolsets-choosing-which-tools-an-agent-sees). |
| Which cluster to read | `--kubeconfig PATH`, `--context NAME`. See [Selecting a cluster](#selecting-a-cluster). |
| Serve a snapshot instead of a cluster | `--mock` (the built-in snapshot), `--state-file FILE` (one of your own), `--state-time {advancing,frozen}`. See [Mock data](#mock-data) and [Capturing and replaying a cluster](#capturing-and-replaying-a-cluster). |
| Secret redaction | `--no-redact` turns it off (it is on by default). See [Secret redaction](#secret-redaction). |
| Transport | `--transport {stdio,streamable-http}` (default `stdio`), `--host` (default `127.0.0.1`), `--port` (default `8000`) |
| Logging | `--log-level`, `--debug` |

Some common combinations:

```sh
k8s-mcp-server                                         # stdio, current kubeconfig context, investigate toolset
k8s-mcp-server --toolset triage                        # just the five triage tools
k8s-mcp-server --context prod-eu                       # a specific cluster
k8s-mcp-server --mock                                  # the built-in snapshot, no cluster needed
k8s-mcp-server --state-file incident-1234.json         # replay your own capture
k8s-mcp-server --transport=streamable-http --port=8000 # HTTP, for remote access
```

<details>
<summary>Full <code>k8s-mcp-server --help</code></summary>

```
usage: k8s-mcp-server [-h] [--transport {streamable-http,stdio}] [--host HOST] [--port PORT]
                      [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}] [--debug]
                      [--kubeconfig PATH] [--context NAME] [--mock] [--state-file FILE]
                      [--state-time {advancing,frozen}] [--no-redact]
                      [--toolset {triage,investigate,all}] [--include TOOL] [--exclude TOOL]

Run the MCP server.

options:
  -h, --help            show this help message and exit
  --transport {streamable-http,stdio}
                        Transport to use for MCP server [default: stdio]
  --host HOST           Hostname for HTTP service [default: 127.0.0.1]
  --port PORT           Port for HTTP service [default: 8000]
  --log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}
                        Log level [default: INFO]
  --debug               Enable debug mode [default: False]
  --kubeconfig PATH     Kubeconfig file to use [default: KUBECONFIG, then ~/.kube/config]
  --context NAME        Kubeconfig context to use [default: K8STOOLS_CONTEXT, then the
                        kubeconfig's current-context]. A kubeconfig or context that is given but
                        cannot be loaded stops the server rather than falling back to another
                        cluster.
  --mock                If specified, just run mock versions of the tools that don't need a
                        cluster
  --state-file FILE     Serve a captured cluster snapshot from FILE (implies --mock). Captures are
                        written by k8s-capture-state.
  --state-time {advancing,frozen}
                        Replay clock for a captured snapshot [default: advancing]. 'frozen' pins
                        every age at its captured value, so repeated runs are identical - what an
                        automated suite wants.
  --no-redact           Disable secret redaction of tool output (redaction is on by default; can
                        also be disabled with K8STOOLS_REDACT=0)
  --toolset {triage,investigate,all}
                        Which tools to serve [default: investigate]. 'triage' is the few needed to find
                        where to look, 'investigate' is every tool but those another tool covers,
                        'all' is everything.
  --include TOOL        Also serve TOOL (repeatable)
  --exclude TOOL        Don't serve TOOL (repeatable)
```

</details>

Environment variables:

| Variable | Effect |
|---|---|
| `KUBECONFIG` | Kubeconfig file, when `--kubeconfig` isn't given |
| `K8STOOLS_CONTEXT` | Kubeconfig context, when `--context` isn't given |
| `K8STOOLS_REDACT=0` | Turns redaction off, like `--no-redact` |

### When a tool call fails

A failed call comes back to the client as an error result with the reason, for
example `404 Not Found: pods "x" not found`, a permission error naming what the
account can't list, `can't reach the Kubernetes API server at ...`, or why a
log can't be read, so an agent can correct itself instead of retrying blindly.
The reason is redacted like any result. An unexpected failure (a bug) shows
only `Error executing tool <name>`, and its details go to the server log.

### Selecting a cluster

Each server answers from exactly one cluster, chosen at startup:

| | Kubeconfig file | Context |
|---|---|---|
| 1st | `--kubeconfig PATH` | `--context NAME` |
| 2nd | `KUBECONFIG` | `K8STOOLS_CONTEXT` |
| 3rd | `~/.kube/config` | the file's `current-context` |

With none of these given and no usable kubeconfig, the server falls back to the
in-cluster service account, as when it runs in a pod.

The server logs the cluster it chose when it starts:

```
WARNING:root:Bound to cluster: context 'prod-eu' at https://prod-eu.example:6443 (kubeconfig: /home/me/.kube/prod.yaml)
```

and the `get_cluster_info` tool reports it, so an agent can check which cluster it
is talking to.

A few rules keep a server from quietly answering from the wrong cluster:

* **An explicit selection never falls back.** If a `--kubeconfig`, `--context` or
  `K8STOOLS_CONTEXT` is given but cannot be loaded (a missing file, a misspelled
  context), the server exits with an error. It does not fall back to in-cluster
  config, which in a pod would silently mean *that pod's* cluster.
* **The choice is made once.** Every tool shares it, and it does not follow later
  changes to the kubeconfig: running `kubectl config use-context` does not
  repoint a server that is already running.
* **`--kubeconfig` and `--context` are rejected with `--mock` or `--state-file`**,
  which serve a snapshot rather than a cluster.

To serve two clusters, run two servers:

```sh
k8s-mcp-server --transport=streamable-http --port=8001 --context prod-eu
k8s-mcp-server --transport=streamable-http --port=8002 \
    --kubeconfig ~/.kube/eks.yaml --context staging
```

### Permissions

The tools only read, with the `get` and `list` verbs. A read-only ClusterRole
that covers every tool:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: k8stools-reader
rules:
- apiGroups: [""]
  resources: [namespaces, nodes, pods, pods/log, events, services, configmaps,
              persistentvolumeclaims]
  verbs: [get, list]
- apiGroups: [apps]
  resources: [deployments, replicasets, statefulsets, daemonsets, controllerrevisions]
  verbs: [get, list]
- apiGroups: [batch]
  resources: [jobs, cronjobs]
  verbs: [get, list]
- apiGroups: [autoscaling]
  resources: [horizontalpodautoscalers]
  verbs: [get, list]
- apiGroups: [discovery.k8s.io]
  resources: [endpointslices]
  verbs: [get, list]
- apiGroups: [metrics.k8s.io]
  resources: [pods, nodes]
  verbs: [get, list]
- apiGroups: [networking.k8s.io]
  resources: [ingresses]
  verbs: [get, list]
- apiGroups: [apiextensions.k8s.io]
  resources: [customresourcedefinitions]
  verbs: [get, list]
```

`get_custom_resource_status` also needs read access to each custom resource you
want it to see, granted per group. Add a rule like this for each operator you
use, rather than a wildcard:

```yaml
- apiGroups: [cert-manager.io]
  resources: [certificates, issuers, clusterissuers]
  verbs: [get, list]
```

`get_cluster_info` also reads the server version (`/version`), which every
authenticated user can read by default. No tool reads Secrets: `get_workload_history`
names the Secrets a workload uses, from its pod template, without reading them.

### The HTTP transport

`--transport=streamable-http` starts an HTTP server that listens on `--host` and
`--port` (by default 127.0.0.1 and 8000). Use it when the agent is not on the
same machine as the server.

Here's how to start the server and check it with `curl`:

```sh
# start the server
$ k8s-mcp-server --transport=streamable-http
INFO:     Started server process [6649]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)

# in another terminal, list the tools
$ curl -H "Content-Type: application/json" \
       -H "Accept: application/json, text/event-stream" \
       -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}' \
       http://127.0.0.1:8000/mcp
event: message
data: {"jsonrpc":"2.0","id":1,"result":{"tools":[.... long text elided ...]}}
```

### Checking the tools with `k8s-mcp-client`

`k8s-mcp-client` starts the server over stdio and prints each tool's description
and input schema as Markdown. `--short` prints only the descriptions, and
`--tools NAME ...` limits it to the tools named. It passes `KUBECONFIG` and
`K8STOOLS_CONTEXT` through to the server.

## Using the tools from Python

### In an agent

The tools are in `k8stools.k8s_tools`. Here's an example with pydantic-ai:

```python
from pydantic_ai.agent import Agent
from k8stools.k8s_tools import TOOLS
from k8stools.redaction import wrap_with_redaction

agent = Agent(
        model="openai:gpt-4.1",
        system_prompt=SYSTEM_PROMPT,
        tools=[wrap_with_redaction(fn) for fn in TOOLS],
)

result = agent.run_sync("What is the status of the pods in my cluster?")
print(result.output)
```

> **⚠️ Redaction is not automatic outside the k8stools MCP server.** The functions
> in `TOOLS` (and `mock_tools.TOOLS`) return raw values: ConfigMap contents, env
> values in pod specs, container logs and command-line args, exactly as the API
> returns them. Only `k8s-mcp-server` and `k8s-capture-state` apply redaction for
> you. If you hand `TOOLS` to an agent, or build your own MCP server from them,
> wrap each one with `wrap_with_redaction` as above, or anything secret-shaped in
> your cluster goes straight into the model's context. See
> [Where redaction applies](#where-redaction-applies).

### Choosing a cluster

The tools use `KUBECONFIG` (or `~/.kube/config`) and its current context. To pin a
cluster instead, call `configure` before the first tool call:

```python
from k8stools import k8s_tools

k8s_tools.configure(kubeconfig="~/.kube/prod.yaml", context="prod-eu")
```

The selection is resolved as described in [Selecting a cluster](#selecting-a-cluster).

To answer from something that isn't a cluster (a converter serving a recorded
snapshot, say), bind an `ApiClient` whose `rest_client` answers the requests, and
pin the clock to the snapshot's time. Both apply to every tool and to
`capture_state`, and are restored at the end of the block:

```python
with k8s_tools.use_api_client(api, context="snapshot-33"), \
        k8s_tools.pinned_clock(snapshot_time):
    state = capture_state()
```

### Choosing tools in Python

`TOOLS` is every tool. The toolsets are in `TOOLSETS`, and `select_tools` builds an
adjusted one, just like `--toolset`, `--include` and `--exclude`:

```python
from k8stools.k8s_tools import TOOLS, TOOLSETS, select_tools

triage = TOOLSETS["triage"]
mine = select_tools(TOOLS, "triage", include=("get_pod_spec",), exclude=("get_events",))

tools = [wrap_with_redaction(fn) for fn in mine]   # wrap them, as above
```

`mock_tools.TOOLSETS` holds the same sets of [mock tools](#mock-data).

### Printing results

Most `get_*` tools have a `print_*` companion that prints the result as a table,
which is handy when debugging (for example `print_pod_summaries`,
`print_workload_history`, `print_hpa_summaries`). The log readers have none, since
they already return text.

```python
from k8stools.k8s_tools import print_pod_summaries
print_pod_summaries("default")
```

## Mock data

When building agents, it can be helpful to test them against *mock* versions of the
tools that don't need a real cluster but return realistic values. The module
`k8stools.mock_tools` does just that, and so does the server's `--mock` option.

The data is a small, hand-maintained cluster modeled on a Minikube instance running
the [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo)
application, with problems for an agent to find:

* a crash-looping `ad` service, mid-upgrade, whose Service has no ready backends;
* an `accounting` service that is ready but was OOM-killed 40 minutes ago, after
  578 restarts, and whose previous log file is gone;
* a `checkout` rollout that can't finish: its new pod can't be scheduled (a node
  selector no node matches), while the old pod still serves;
* a Deployment whose HPA is at its maximum;
* a new `quote` Deployment whose pod can't be scheduled, because the
  PersistentVolumeClaim it mounts can't bind: its storage class doesn't exist;
* an Ingress with one rule to a missing Service;
* a cert-manager `Certificate` that isn't Ready;
* a DaemonSet, a CronJob and its Job, a StatefulSet with PVCs and a completed
  init container, and events.

The mock serves a *capture* (a JSON snapshot of one cluster) rather than a set of
canned answers per tool, so the tools agree with each other: a pod that is not in the
capture does not exist for any tool, and one that is has consistent statuses, events,
spec and logs. You can snapshot your own cluster the same way.

## Capturing and replaying a cluster

`k8s-capture-state` snapshots a live cluster into a single JSON file, and the MCP
server replays that file as if it were the cluster. A whole scenario, such as a
crash loop, a full volume or a bad rollout, becomes a fixture you can re-run,
commit, or attach to an issue, with no cluster needed to reproduce it.

```bash
# Snapshot the current cluster (respects KUBECONFIG)
k8s-capture-state -o incident-1234.json

# Replay it
k8s-mcp-server --state-file incident-1234.json
```

It finishes with a summary of what it got:

```
$ k8s-capture-state --namespace default -o incident-1234.json
Captured 27 pod(s), 31 container(s).
  logs captured:          31
  previous logs captured: 31
  values redacted:        4
Wrote incident-1234.json (1,435,335 bytes, redacted).
```

### `k8s-capture-state` options

```
usage: k8s-capture-state [-h] [--namespace NS [NS ...]] [-o FILE]
                         [--kubeconfig PATH] [--context NAME] [--no-logs]
                         [--max-log-lines MAX_LOG_LINES] [--no-previous-logs]
                         [--no-redact] [--redact-file FILE]
                         [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}]

Snapshot a Kubernetes cluster to a JSON file replayable by the k8stools MCP
server (--state-file) or MockState.

options:
  -h, --help            show this help message and exit
  --namespace NS [NS ...]
                        Namespace(s) to capture namespaced resources from
                        [default: all]. Namespaces and nodes are always
                        captured in full.
  -o FILE, --output FILE
                        Output file [default: k8s-state-<timestamp>.json]. A
                        name ending in .gz is written gzipped; captures are
                        read back compressed or not either way.
  --kubeconfig PATH     Kubeconfig file to capture from [default: KUBECONFIG,
                        then ~/.kube/config]
  --context NAME        Kubeconfig context to capture from [default:
                        K8STOOLS_CONTEXT, then the kubeconfig's current-
                        context]
  --no-logs             Skip container logs entirely, including previous-
                        instance logs, for a structure-only snapshot
  --max-log-lines MAX_LOG_LINES
                        Log lines to capture per container [default: 1000]
  --no-previous-logs    Skip the previous-instance logs of restarted
                        containers. These usually carry the diagnosis for a
                        crash loop, so dropping them is rarely what you want.
  --no-redact           Disable secret redaction of captured values (redaction
                        is on by default; can also be disabled with
                        K8STOOLS_REDACT=0)
  --redact-file FILE    Instead of capturing, re-run redaction over an
                        existing capture FILE and rewrite it in place (keeping
                        its compression). Use on captures taken with --no-
                        redact, or before a newer k8stools added redaction
                        rules.
  --log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}
                        Log level [default: INFO]
```

### What gets captured

Everything the tools can read, so that every tool answers on replay:

| | |
|---|---|
| Cluster | context name, API server URL and version, so `get_cluster_info` answers on replay (not the kubeconfig path, which names a file on the capturing machine) |
| Cluster-wide | namespaces, nodes, custom resource definitions and their instances' conditions (up to 500 per type), and resource usage (one metrics-server sample of containers and nodes, or why metrics weren't available) |
| Per namespace | deployments, replica sets, services and their endpoints, ingresses, statefulsets, daemonsets, HPAs, cronjobs, jobs, PVCs, events, and each workload's change history (`get_workload_history`'s result, so no env values are written) |
| ConfigMaps | summary **and** full contents, so `get_configmap` works too |
| Per pod | summary, labels, container and init container statuses, spec, and per-container logs (init containers included) |

A cluster is selected exactly as for the MCP server (see [Selecting a
cluster](#selecting-a-cluster)), and a selection that cannot be loaded fails the
capture. The capture needs the permissions in [Permissions](#permissions); a
custom-resource type it can't read is recorded as such rather than failing the
capture, and so is a cluster without metrics-server.

`--namespace` restricts only the namespaced resources; namespaces and nodes are
always captured in full so the cluster still makes sense.

Logs dominate the file size: expect a few MB for a few dozen pods (the 27-pod
capture above was 1.4 MB with only 50 lines per container; a 38-pod one at 200
lines was 3.3 MB). Use `--max-log-lines` to trade size against history, or
`--no-logs` for a structure-only snapshot. Captures compress about 10x, and can be
written and read gzipped; see [Compression, and what to check
in](#compression-and-what-to-check-in).

Captures made by older versions of k8stools still load. **What a capture didn't
record replays as not recorded, never as none**, whether the capture predates it
or didn't collect it:

- A kind the capture has no list of (an older capture has no `endpoints`, say)
  makes its tool raise `K8sNotRecorded`, an error saying the kind is unknown
  rather than absent. `get_namespace_health` and `get_workload_report` name such
  kinds in one note instead of failing.
- A container log that wasn't recorded (unreadable when captured, or
  `--no-logs`) raises `K8sNotRecorded` too, and so does a previous-instance log
  of a container that restarted, when its previous log wasn't captured. A
  container that never restarted has no previous instance, and says so, as
  live. A log recorded as empty is replayed as empty.
- A field the capture predates is unknown (`None`, never 0).

### Compression, and what to check in

A name ending in `.gz` is written gzipped, and captures are read back compressed
or not either way. Detection is by content, so a capture still loads after being
renamed:

```bash
k8s-capture-state -o incident-1234.json.gz     # 3.3 MB -> 364 KB
k8s-mcp-server --state-file incident-1234.json.gz
```

**For a fixture you check into git, use plain `.json` anyway.** Git already stores
blobs compressed, so you do not pay the uncompressed size, and it *deltas*
successive versions of a text file against each other, which it cannot do for a
gzip blob. Measured on two captures of the same 38-pod cluster:

| | first commit | updating the fixture later |
|---|---|---|
| `.json` (3.3 MB) | 404 KB | **+61 KB** |
| `.json.gz` (364 KB) | 388 KB | **+358 KB** |

The two cost about the same once, but every re-capture of a `.gz` adds a whole
fresh blob, so the repo grows roughly 6x faster. Captures are also indented rather
than compact for the same reason: it gives git something to delta. This is also
why you probably do *not* need git-lfs here: LFS stores every version whole, which
gives up exactly the delta compression that makes these files cheap.

Reach for `.gz` when the file travels on its own: attached to an issue, dropped
in object storage, or simply too large to keep expanded in a working tree.

### Previous-instance logs

The capture includes the **previous-instance logs** of every restarted container,
which is what `get_logs_for_pod_and_container(..., previous=True)` returns. For a
crash-looping container the current instance's logs are usually empty or
post-restart, and the stack trace or OOM message that explains the crash lives in
the previous instance, so a capture without them would be missing the evidence
that matters most.

They roughly double the log payload for a cluster where a lot is restarting,
which is exactly the cluster worth capturing. `--no-previous-logs` skips them. The
summary reports how many could not be read (the previous instance may have been
garbage-collected), because a crash-loop capture that lost them looks identical to
one that never had any.

### Previous-instance log semantics

`previous=True` asks the kubelet for the container's last *terminated* instance.
Three of its behaviors read as bugs and are not, and all three show up in captures
as well as in live queries:

- **In CrashLoopBackOff, both calls return the same text.** There is no running
  instance during the backoff, so the kubelet serves the most recently terminated
  one for `previous=False` too. Check `get_pod_container_statuses` before
  concluding the flag was ignored.
- **A restart between two calls shifts the window.** The instance that was current
  becomes the previous one, so a container crash-looping every few seconds can
  return byte-identical output for both. Compare the log timestamps, not the flag.
- **A reclaimed log file returns 200, not an error**, with the body
  `unable to retrieve container logs for <container-id>`. It is a successful call
  whose content is an error message. The log tools don't return that text, which
  agents kept reading as the program's output: they raise `K8sLogReclaimed`, and
  `get_workload_report` shows the log tail as `available: false`, with no lines.

Only the single most recent terminated instance is retained; there is no way to
reach further back than one.

The first is detected for you: the log then starts with a `[k8stools] note:`
line saying so. The second can't be seen from a single call.

### Replay clock

Every age in a capture is stored relative to the moment of capture, so the
*intervals* between resources survive replay exactly: a deployment aged 8 days
whose newest replica set is aged 7h34m still says "upgraded 7h34m ago" whenever it
is replayed.

By default those ages advance in real time from when the server starts, which is
what makes an interactive session feel like a live cluster. For an automated test
suite pass `--state-time frozen`:

```bash
k8s-mcp-server --state-file incident-1234.json --state-time frozen
```

which pins every age at its captured value, so repeated runs are identical and a
long session cannot see ages drift between its first tool call and its last.
(`--state-time` requires `--state-file`; it is rejected rather than ignored, since
a suite that believed it had frozen the clock and had not would be flaky with no
visible cause.)

Absolute times are replayed the same way: every timestamp a tool returns is moved
so the moment of capture becomes the moment the server started. A container killed
5 minutes before the capture reads as killed 5 minutes before server start, and
`get_cluster_info`'s `captured_at` is that start time, not the date in the file,
so the agent sees one consistent clock. Log line timestamps (the kubelet's
prefix) are moved too, so `since_seconds` works on a capture of any age;
timestamps an application writes inside its own log messages are left as
recorded. The file's own capture date is in the server's startup log.

### Replaying from Python

For a test suite, skip the server and load the capture directly. `MockState` has
the same query methods as the tools:

```python
from pathlib import Path
from k8stools.mock_state import MockState

state = MockState.from_file(Path("incident-1234.json"), frozen=True)

pods = state.get_pod_summaries("default")
logs = state.get_logs_for_pod_and_container("ad-647b4947cc-s5mpm", "default",
                                            previous=True)
```

Or point the `mock_tools` module at it, so anything already calling
`k8stools.mock_tools` serves your capture instead of the built-in one:

```python
from k8stools import mock_tools

mock_tools.load_mock_state(Path("incident-1234.json"), frozen=True)
agent = Agent(model="openai:gpt-4.1", tools=mock_tools.TOOLS)
```

### Captures and secrets

`k8s-capture-state` applies the same redaction pass as the MCP server, **on by
default**, opt out with `--no-redact` or `K8STOOLS_REDACT=0`. This matters because
a capture file is far more portable than cluster access: it gets committed to
git, attached to issues, and passed around long after anyone remembers which
cluster it came from. Redaction is one-way: replaying a redacted capture with
`--no-redact` restores nothing, so a scenario that genuinely needs real values has
to be re-captured.

A capture taken with `--no-redact`, or by an older k8stools before a redaction
rule existed, can be redacted afterwards. This rewrites the file in place, keeping
its compression; running it again finds nothing more:

```sh
k8s-capture-state --redact-file incident-1234.json
```

Captures taken before 3.0.0 may hold credentials embedded in args, URLs and
connection strings (see below). Re-redact any you've committed or shared.

## Secret redaction

Some read-only resources can carry secret-shaped values even though they are not
Kubernetes `Secret` objects: `ConfigMap` data and the `env` blocks in a pod spec
are the common cases. When you run the k8stools MCP server directly against an
agent (with no wrapping service to scrub output), those values would otherwise flow
straight into the model's context.

To prevent that, the MCP server applies a redaction pass to every tool's output.
It is **on by default** and can be disabled with `--no-redact` or by setting
`K8STOOLS_REDACT=0`. Redaction matches three ways, replacing each match with a
visible `[REDACTED]` marker so the agent can tell "hidden" from "absent". We never
provide a reader for Kubernetes `Secret` objects.

1. **By value shape**: AWS access keys, JWT / bearer tokens, PEM private-key blocks.
2. **By key / env-var name**: the name contains `key`, `secret`, `token`,
   `password`, `passwd`, `credential` or `cred` **as a whole word**. Names are split
   on separators and camelCase, so `AWS_SECRET_ACCESS_KEY`, `apiKey` and
   `x-auth-token` all match while `VALKEY_ADDR` does not.

   A field named *exactly* `key` is exempt: in the Kubernetes API that is always a
   map entry, a taint, a label selector or a projected-volume item, never a
   credential. This matters more than it sounds: on a real 38-pod cluster it was
   130 of 139 redactions, none of them secrets: toleration keys such as
   `node.kubernetes.io/not-ready`, the filename `ca.crt`, and `secretKeyRef.key`.
   That last is worth stating plainly: **a reference to a secret is not a secret.**
   The value lives in the `Secret` object and is resolved by the kubelet, never
   appearing in tool output, so redacting the pointer hides which key feeds an env
   var and protects nothing. The exemption does not apply to env-var names, where
   a variable someone named `KEY` plausibly does hold one. Schema fields that
   compound `key` are exempt too, in both spellings: an affinity term's
   `topologyKey` / `topology_key` and label-key lists, and `get_configmap`'s
   `binary_data_keys`.
3. **By context**: a credential inside a longer string, found by what surrounds
   it, with only the credential replaced:

   | Where | Example |
   |---|---|
   | URL userinfo | `postgres://otelu:[REDACTED]@postgresql/otel` |
   | Sensitive flags and system properties | `--password=[REDACTED]`, `--token [REDACTED]`, `-Dapi.key=[REDACTED]` |
   | Argv lists | `["--token", "[REDACTED]"]` |
   | Connection strings | `Password=[REDACTED];`, `password=[REDACTED] dbname=otel` |
   | Query strings and presigned URLs | `?access_token=[REDACTED]&`, `X-Amz-Signature=[REDACTED]`, `sig=[REDACTED]` |
   | Properties files | `spring.datasource.password=[REDACTED]` |
   | SQL password literals | `CREATE USER otelu WITH PASSWORD '[REDACTED]'`, `IDENTIFIED BY '[REDACTED]'` |

   In running text (`sh -c "..."`, logs, `--help` output), a `--flag value` pair
   counts only when the value looks like a credential (a digit or symbol, or 16+
   characters), because prose and help text put words and type names after flags.

**Nothing that only locates a secret is redacted.** Like `secretKeyRef.key`, these
are references, and hiding them hides where a secret comes from and protects
nothing: `$(DB_PASSWORD)`, `${token}`, `$API_KEY`, templates, file paths
(`--tls-key /etc/certs/tls.key`), and flags named for a location
(`--password-file`, `--secret-name`, `--token-url`). Booleans and masks
(`skip-secret: "true"`, `password=****`) are left alone too.

On a real 38-pod cluster, these rules redact 7 values, all real credentials (the
seventh is the SQL password literal), where the 2.x rules redacted 6 of which 3
were not secrets.

### Where redaction applies

| How the tools are used | Redacted? |
|---|---|
| `k8s-mcp-server` | **Yes**, on by default; `--no-redact` or `K8STOOLS_REDACT=0` turns it off |
| `k8s-capture-state` | **Yes**: the capture applies the pass itself before writing the file |
| `k8s_tools.TOOLS` / `mock_tools.TOOLS` given to an agent framework, or composed into your own MCP server | **No.** Wrap each function: `[wrap_with_redaction(fn) for fn in TOOLS]` |
| Calling a tool function directly in Python | **No.** Apply the pass to the result: `redact_object(result)` returns `(redacted, count)` |

`wrap_with_redaction` keeps each function's name, signature and docstring, so a
framework that builds tool schemas from them (pydantic-ai, MCP) sees the same tool.
Redaction cannot catch everything. A secret with nothing naming it, such as a bare
positional argument (`app hunter2`), can't be told apart from any other value; nor
can an unusual format none of the rules above recognise.

## Design approach

Our goal is quality over quantity: well-documented, strongly typed tools. We
believe this is critical for agents to use tools effectively, beyond simple demos.

The tools are built on the [Kubernetes Python client](https://github.com/kubernetes-client/python),
and return one of three kinds of result:
1. **kubectl-like**: Pydantic models that mirror a `kubectl` command's output.
   For example, `get_pod_summaries` is equivalent to `kubectl get pods -o wide`.
2. **API-typed**: Pydantic models that match the Kubernetes client's types
   (see https://github.com/kubernetes-client/python/tree/master/kubernetes/docs),
   with lesser-used fields omitted. For example, `get_pod_container_statuses`.
3. **Raw dict**: the result of `to_dict()` on the API's object (defined in
   https://github.com/kubernetes-client/python/tree/master/kubernetes/client/models),
   with its fields documented in the tool's docstring. For example, `get_pod_spec`.

Read-only tools come first, because we are focusing on monitoring and root cause
analysis. If we add tools that change the cluster, they will be kept separate
from the read-only ones, so you can still build "safe" agents.

## Development

```sh
git clone https://github.com/BenedatLLC/k8stools.git
cd k8stools
uv sync

# Unit tests (no cluster needed)
uv run pytest -v

# Integration tests against a real cluster (skipped automatically if unreachable)
KUBECONFIG=~/.kube/your-config.yaml uv run pytest tests/test_k8s_tools_realk8s.py -v

# Integration tests that write to the cluster (skipped unless asked)
K8STOOLS_LIVE_WRITE=1 KUBECONFIG=~/.kube/your-config.yaml uv run pytest tests/test_live_writes.py -v
```

The integration tests above only read what the cluster has. The ones in
`tests/test_live_writes.py` create the failures they check, such as a claim on a
storage class that doesn't exist or a rollout whose new pod can't be scheduled,
and compare the tools' output with the cluster's own messages. Each test creates
a `k8stools-test-<random>` namespace and deletes it afterwards, so it needs
permission to create and delete namespaces, Deployments and
PersistentVolumeClaims. The tests pull `registry.k8s.io/pause`. Use a test
cluster such as Minikube or kind. They take about 30 seconds. If a run is
interrupted, remove what it left with
`kubectl get ns -o name | grep k8stools-test- | xargs -r kubectl delete`.

Planned and deferred work is in
[docs/ROADMAP.md](https://github.com/BenedatLLC/k8stools/blob/main/docs/ROADMAP.md).
