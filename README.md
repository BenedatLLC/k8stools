# Kubernetes Tools

![Unit Tests](https://github.com/BenedatLLC/k8stools/actions/workflows/unit-tests.yml/badge.svg)

This package provides a collection of Kubernetes functions to be used by Agents. They can be passed
directly to an agent as tools or placed behind an MCP server (included). Some use cases include:
* Chat with your kubernetes cluster via GitHub CoPilot or Cursor.
* Build [agents](https://github.com/BenedatLLC/orca-agent/) to monitor your cluster or perform root cause analysis.
* Vibe-code a custom chat UI.
* Use in non-agentic automations.
* Snapshot a live cluster to a file and replay it as a test fixture, with no cluster
  needed — see [Capturing and replaying a cluster](#capturing-and-replaying-a-cluster).

<a href="https://glama.ai/mcp/servers/@BenedatLLC/k8stools">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/@BenedatLLC/k8stools/badge" alt="Kubernetes Tools Server MCP server" />
</a>

## Methodology

Our goal is to focus on quality over quantity -- providing
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

Currently, the priority is on functions that do not modify the state of the cluster.
We want to focus first on the monitoring / RCA use cases. When we do add tools to address
other use cases, they will be kept separate from the read-only tools so you can still build
"safe" agents.

## Installation
Via `pip`:

```sh
pip install k8stools
```

Via `uv`:
```sh
uv add k8stools
```

This installs three commands: `k8s-mcp-server` (the MCP server), `k8s-mcp-client`
(a client for manual testing), and `k8s-capture-state` (snapshot a cluster to a
replayable file).

What changed in each release is in the
[changelog](https://github.com/BenedatLLC/k8stools/blob/main/CHANGELOG.md).

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
```

`get_cluster_info` also reads the server version (`/version`), which every
authenticated user can read by default. No tool reads Secrets: `get_workload_history`
names the Secrets a workload uses, from its pod template, without reading them.

## Current tools

Each tool's description, which an MCP client sends to the model on every turn,
is a few lines. [docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md) documents every
parameter and field. Warnings that apply to a particular result come back in that
result: a `notes` list on nodes, pods, container statuses, events and DaemonSets,
and `[k8stools] note:` lines at the top of a log (see
[Notes in results](docs/TOOL_REFERENCE.md#notes-in-results)).

These are the tools we define:

* `get_cluster_info` - which cluster the tools are answering from: kubeconfig context, API server URL and version, or the capture being replayed
* `get_namespaces` - get a list of namespaces, like `kubectl get namespace`
* `get_node_summaries` - get a list of nodes, like `kubectl get nodes -o wide` (includes capacity/allocatable/conditions/taints/labels)
* `get_pod_summaries` - get a list of pods, like `kubectl get pods -o wide`, with each pod's controlling owner (e.g. `DaemonSet/otel-collector-agent`)
* `get_pod_container_statuses` - return the status for each of the container in a pod
* `get_pod_events` - return the events for a pod
* `get_pod_spec` - retrieves the spec for a given pod
* `get_logs_for_pod_and_container` - retrieves logs from a pod and container (supports `tail`, `since_seconds`, and `previous`). See [Previous-instance log semantics](#previous-instance-log-semantics) for when `previous=True` returns the same text as `previous=False` — it is Kubernetes behavior, not a bug.
* `get_deployment_summaries` - get a list of deployments, like `kubectl get deployments`
* `get_replicaset_summaries` - get a deployment's replica sets with their revision numbers and images. A deployment's replica sets are its change history: use this to see when a workload last changed and what the change was.
* `get_workload_history` - what changed in a Deployment, StatefulSet or DaemonSet, and when: each retained revision of its pod template compared with the one before (images, resources, probes, command/args, env var *names*, volumes, scheduling), plus the ConfigMaps and Secrets it references. It states what history cannot show.
* `get_service_summaries` - get a list of services, like `kubectl get services` (includes `selector`/labels/annotations)
* `get_configmap_summaries` - get a list of ConfigMaps, like `kubectl get configmaps`
* `get_configmap` - retrieve the full contents of a single ConfigMap
* `get_statefulset_summaries` - get a list of StatefulSets, like `kubectl get statefulsets`
* `get_daemonset_summaries` - get a list of DaemonSets, like `kubectl get daemonsets -o wide` (includes node selector and images)
* `get_cronjob_summaries` - get a list of CronJobs, like `kubectl get cronjobs`
* `get_job_summaries` - get a list of Jobs, like `kubectl get jobs`
* `get_logs_for_job` - retrieve logs from a Job's most-recent pod
* `get_logs_for_cronjob` - retrieve logs from a CronJob's most-recent run
* `get_pvc_summaries` - get a list of PersistentVolumeClaims, like `kubectl get pvc` (resolves mounting pods)
* `get_events` - list cluster/namespace-wide events with server-side filtering

We also define a set of associated "print_" functions that are helpful in debugging:

* `print_namespaces`
* `print_node_summaries`
* `print_pod_summaries`
* `print_pod_container_statuses`
* `print_pod_events`
* `print_pod_spec`
* `print_deployment_summaries`
* `print_workload_history`
* `print_service_summaries`
* `print_configmap_summaries`
* `print_configmap`
* `print_statefulset_summaries`
* `print_daemonset_summaries`
* `print_cronjob_summaries`
* `print_job_summaries`
* `print_pvc_summaries`
* `print_events`

## Using the tools
### Directly use in an agent
The core tools are in `k8stools.k8s_tools`. Here's an example usage in an agent:

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

The tools use `KUBECONFIG` (or `~/.kube/config`) and its current context. To pin a
cluster instead, call `configure` before the first tool call:

```python
from k8stools import k8s_tools

k8s_tools.configure(kubeconfig="~/.kube/prod.yaml", context="prod-eu")
```

See [Selecting a cluster](#selecting-a-cluster) for how the selection is resolved.

> **⚠️ Redaction is not automatic outside the k8stools MCP server.** The functions
> in `TOOLS` (and `mock_tools.TOOLS`) return raw values: ConfigMap contents, env
> values in pod specs, container logs and command-line args, exactly as the API
> returns them. Only `k8s-mcp-server` and `k8s-capture-state` apply redaction for
> you. If you hand `TOOLS` to an agent, or build your own MCP server from them,
> wrap each one with `wrap_with_redaction` as above, or anything secret-shaped in
> your cluster goes straight into the model's context. See
> [Where redaction applies](#where-redaction-applies).

### Using via MCP
The script `k8s-mcp-server` provides an MCP server for the same set of tools.
Here are the command line arguments for the server:
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
                        Which tools to serve [default: all]. 'triage' is the few needed to find
                        where to look, 'investigate' is every tool but those another tool covers,
                        'all' is everything.
  --include TOOL        Also serve TOOL (repeatable)
  --exclude TOOL        Don't serve TOOL (repeatable)
```

#### Selecting a cluster

Each server answers from exactly one cluster, chosen at startup:

| | Kubeconfig file | Context |
|---|---|---|
| 1st | `--kubeconfig PATH` | `--context NAME` |
| 2nd | `KUBECONFIG` | `K8STOOLS_CONTEXT` |
| 3rd | `~/.kube/config` | the file's `current-context` |

With none of these given and no usable kubeconfig, the server falls back to the
in-cluster service account, as when it runs in a pod.

The server logs the binding when it starts:

```
WARNING:root:Bound to cluster: context 'prod-eu' at https://prod-eu.example:6443 (kubeconfig: /home/me/.kube/prod.yaml)
```

and the `get_cluster_info` tool reports it to an agent, so an agent can check which
cluster it is talking to without leaving MCP.

#### Choosing which tools to serve

Every tool's description is sent to the model on every turn, and agents choose
less reliably among many similar tools. `--toolset` serves a named subset:

| Toolset | Tools | Use it for |
|---|---|---|
| `triage` | `get_cluster_info`, `get_node_summaries`, `get_pod_summaries`, `get_events`, `get_workload_history` | finding where to look |
| `investigate` | every tool except those another tool already covers | a full investigation |
| `all` (default) | every tool | compatibility; the default until 3.0.0 |

`investigate` leaves out `get_pod_events` (use `get_events(involved_name=...)`),
`get_replicaset_summaries` (use `get_workload_history`), and `get_logs_for_job` /
`get_logs_for_cronjob` (use the pod log tool; `PodSummary.owner` names each pod's
Job). They stay available in `all` and in Python.

Adjust a toolset with `--include TOOL` and `--exclude TOOL`, both repeatable:

```sh
k8s-mcp-server --toolset triage --include get_pod_spec
```

A misspelled toolset or tool name stops the server at startup.

In Python, `k8s_tools.TOOLSETS["triage"]` (and `mock_tools.TOOLSETS`) hold the same
sets, and `k8s_tools.select_tools(TOOLS, toolset, include, exclude)` builds an
adjusted one. Wrap them for redaction as above.

A few rules keep a server from quietly answering from the wrong cluster:

* **An explicit selection never falls back.** If a `--kubeconfig`, `--context` or
  `K8STOOLS_CONTEXT` is given but cannot be loaded (a missing file, a misspelled
  context), the server exits with an error. It does not fall back to in-cluster
  config, which in a pod would silently mean *that pod's* cluster.
* **The binding is made once.** Every tool shares it, and it does not follow later
  changes to the kubeconfig: running `kubectl config use-context` does not
  repoint a server that is already running.
* **`--kubeconfig` and `--context` are rejected with `--mock` or `--state-file`**,
  which serve a capture rather than a cluster.

To serve two clusters, run two servers:

```sh
k8s-mcp-server --transport=streamable-http --port=8001 --context prod-eu
k8s-mcp-server --transport=streamable-http --port=8002 \
    --kubeconfig ~/.kube/eks.yaml --context staging
```

#### Use MCP with the stdio transport
The *stdio* transport is best for use with local Coding Agents, like GitHub CoPilot or Cursor.
It is the default, so you can run the `k8s-mcp-server` script without arguments. Here's an example
`mcp.json` configuration:

```json
{
   "servers": {
      "k8stools-stdio": {
         "command": "${workspaceFolder}/.venv/bin/k8s-mcp-server",
         "args": [
         ],
         "envFile": "${workspaceFolder}/.envrc"
      }
   }
}
```

This assumes the following:
1. The Python virtual environment is expected to be in `.venv` under the root of your VSCode workspace
2. You have installed the k8stools package into your workspace
3. The environment file `.envrc` contains any variables you need defined. In particular, you may need to
   set `KUBECONFIG` to point to your `kubectl` config file, and `K8STOOLS_CONTEXT` to pin a context
   (or pass `--kubeconfig` / `--context` in `args`).

#### Use MCP with the streamable HTTP transport
The *streamable http* transport is enabled with the command line option `--transport=streamable-http`. It will
start an HTTP server which listens on the specified address and port (defaulting to 127.0.0.1 and 8000, respectively).
This transport is best for cases where you want remote access to your MCP server.

Here's a short example that starts the server and then does a sanity test using `curl` to get the tool information:
```sh
# start the server
 $ k8s-mcp-server --transport=streamable-http
[07/21/25 19:55:13] INFO     Starting with 18 tools on transport streamable-http          mcp_server.py:59
INFO:     Started server process [6649]
INFO:     Waiting for application startup.
INFO     StreamableHTTP session manager started         streamable_http_manager.py:111
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)

# Now, open another terminal window and test it
$ curl -v \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{
           "jsonrpc": "2.0",
           "id": 1,
           "method": "tools/list",
           "params": {}
         }' \
     http://127.0.0.1:8000/mcp
*   Trying 127.0.0.1:8000...
* Connected to 127.0.0.1 (127.0.0.1) port 8000
> POST /mcp HTTP/1.1
> Host: 127.0.0.1:8000
> User-Agent: curl/8.7.1
> Content-Type: application/json
> Accept: application/json, text/event-stream
> Content-Length: 120
>
* upload completely sent off: 120 bytes
< HTTP/1.1 200 OK
< date: Tue, 22 Jul 2025 02:56:25 GMT
< server: uvicorn
< cache-control: no-cache, no-transform
< connection: keep-alive
< content-type: text/event-stream
< x-accel-buffering: no
< Transfer-Encoding: chunked
<
event: message
data: {"jsonrpc":"2.0","id":1,"result":{"tools":[.... long text elided ...]}}
```

## Mock tools
When building agents, it can be helpful to test them against *mock* versions that do
not go against a real cluster, but return realistic values. The module
`k8stools.mock_tools` does just that. The data is a small, hand-maintained
cluster modeled on a Minikube instance running the
[Open Telemetry Demo](https://github.com/open-telemetry/opentelemetry-demo)
application: a crash-looping `ad` service mid-upgrade, a DaemonSet, a CronJob and
its Job, a StatefulSet with PVCs, and a few events. When running the MCP server,
this may be enabled by using the `--mock` command line option.

The mock serves a *capture* — a JSON snapshot of one cluster — rather than a set of
per-tool canned answers, so the tools agree with each other: a pod that is not in the
capture does not exist for any tool, and one that is has consistent statuses, events,
spec and logs. You can snapshot your own cluster the same way — see
[Capturing and replaying a cluster](#capturing-and-replaying-a-cluster).

## Capturing and replaying a cluster

`k8s-capture-state` snapshots a live cluster into a single JSON file, and the MCP
server replays that file as if it were the cluster. A whole scenario — a crash
loop, a full volume, a bad rollout — becomes a fixture you can re-run, commit, or
attach to an issue, with no cluster needed to reproduce it.

```bash
# Snapshot the current cluster (respects KUBECONFIG)
k8s-capture-state -o incident-1234.json

# Replay it
k8s-mcp-server --state-file incident-1234.json
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

It finishes with a summary of what it got:

```
$ k8s-capture-state --namespace default -o incident-1234.json
Captured 27 pod(s), 31 container(s).
  logs captured:          31
  previous logs captured: 31
  values redacted:        4
Wrote incident-1234.json (1,435,335 bytes, redacted).
```

### What gets captured

Everything the tools can read, so that every tool answers on replay:

| | |
|---|---|
| Cluster | context name, API server URL and version, so `get_cluster_info` answers on replay (not the kubeconfig path, which names a file on the capturing machine) |
| Cluster-wide | namespaces, nodes |
| Per namespace | deployments, replica sets, services, statefulsets, daemonsets, cronjobs, jobs, PVCs, events, and each workload's change history (`get_workload_history`'s result, so no env values are written) |
| ConfigMaps | summary **and** full contents, so `get_configmap` works too |
| Per pod | summary, labels, container statuses, spec, and per-container logs |

A cluster is selected exactly as for the MCP server (see [Selecting a
cluster](#selecting-a-cluster)), and a selection that cannot be loaded fails the
capture.

`--namespace` restricts only the namespaced resources; namespaces and nodes are
always captured in full so the cluster still makes sense.

Logs dominate the file size — expect a few MB for a few dozen pods (the 27-pod
capture above was 1.4 MB with only 50 lines per container; a 38-pod one at 200
lines was 3.3 MB). Use `--max-log-lines` to trade size against history, or
`--no-logs` for a structure-only snapshot. Captures compress about 10x, and can be
written and read gzipped — see [Compression, and what to check
in](#compression-and-what-to-check-in).

### Compression, and what to check in

A name ending in `.gz` is written gzipped, and captures are read back compressed
or not either way — detection is by content, so a capture still loads after being
renamed:

```bash
k8s-capture-state -o incident-1234.json.gz     # 3.3 MB -> 364 KB
k8s-mcp-server --state-file incident-1234.json.gz
```

**For a fixture you check into git, use plain `.json` anyway.** Git already stores
blobs compressed, so you do not pay the uncompressed size — and it *deltas*
successive versions of a text file against each other, which it cannot do for a
gzip blob. Measured on two captures of the same 38-pod cluster:

| | first commit | updating the fixture later |
|---|---|---|
| `.json` (3.3 MB) | 404 KB | **+61 KB** |
| `.json.gz` (364 KB) | 388 KB | **+358 KB** |

The two cost about the same once, but every re-capture of a `.gz` adds a whole
fresh blob, so the repo grows roughly 6x faster. Captures are also indented rather
than compact for the same reason — it gives git something to delta. This is also
why you probably do *not* need git-lfs here: LFS stores every version whole, which
gives up exactly the delta compression that makes these files cheap.

Reach for `.gz` when the file travels on its own — attached to an issue, dropped
in object storage, or simply too large to keep expanded in a working tree.

### Previous-instance logs

The capture includes the **previous-instance logs** of every restarted container —
what `get_logs_for_pod_and_container(..., previous=True)` returns. For a
crash-looping container the current instance's logs are usually empty or
post-restart, and the stack trace or OOM message that explains the crash lives in
the previous instance, so a capture without them would be missing the evidence
that matters most.

They roughly double the log payload for a cluster where a lot is restarting —
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
  whose content is an error message.

Only the single most recent terminated instance is retained; there is no way to
reach further back than one.

The first and third are detected for you: the log then starts with a
`[k8stools] note:` line saying so. The second can't be seen from a single call.

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
a capture file is far more portable than cluster access — it gets committed to
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
Kubernetes `Secret` objects — `ConfigMap` data and the `env` blocks in a pod spec
are the common cases. When you run the k8stools MCP server directly against an
agent (with no wrapping service to scrub output), those values would otherwise flow
straight into the model's context.

To prevent that, the MCP server applies a redaction pass to every tool's output.
It is **on by default** and can be disabled with `--no-redact` or by setting
`K8STOOLS_REDACT=0`. Redaction matches three ways, replacing each match with a
visible `[REDACTED]` marker so the agent can tell "hidden" from "absent". We never
provide a reader for Kubernetes `Secret` objects.

1. **By value shape** — AWS access keys, JWT / bearer tokens, PEM private-key blocks.
2. **By key / env-var name** — the name contains `key`, `secret`, `token`,
   `password`, `passwd`, `credential` or `cred` **as a whole word**. Names are split
   on separators and camelCase, so `AWS_SECRET_ACCESS_KEY`, `apiKey` and
   `x-auth-token` all match while `VALKEY_ADDR` does not.

   A field named *exactly* `key` is exempt: in the Kubernetes API that is always a
   map entry, a taint, a label selector or a projected-volume item, never a
   credential. This matters more than it sounds — on a real 38-pod cluster it was
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
3. **By context** — a credential inside a longer string, found by what surrounds
   it, with only the credential replaced:

   | Where | Example |
   |---|---|
   | URL userinfo | `postgres://otelu:[REDACTED]@postgresql/otel` |
   | Sensitive flags and system properties | `--password=[REDACTED]`, `--token [REDACTED]`, `-Dapi.key=[REDACTED]` |
   | Argv lists | `["--token", "[REDACTED]"]` |
   | Connection strings | `Password=[REDACTED];`, `password=[REDACTED] dbname=otel` |
   | Query strings and presigned URLs | `?access_token=[REDACTED]&`, `X-Amz-Signature=[REDACTED]`, `sig=[REDACTED]` |
   | Properties files | `spring.datasource.password=[REDACTED]` |

   In running text (`sh -c "..."`, logs, `--help` output), a `--flag value` pair
   counts only when the value looks like a credential (a digit or symbol, or 16+
   characters), because prose and help text put words and type names after flags.

**Nothing that only locates a secret is redacted.** Like `secretKeyRef.key`, these
are references, and hiding them hides where a secret comes from and protects
nothing: `$(DB_PASSWORD)`, `${token}`, `$API_KEY`, templates, file paths
(`--tls-key /etc/certs/tls.key`), and flags named for a location
(`--password-file`, `--secret-name`, `--token-url`). Booleans and masks
(`skip-secret: "true"`, `password=****`) are left alone too.

On a real 38-pod cluster, these rules redact 6 values, all real credentials,
where the 2.x rules redacted 6 of which 3 were not secrets.

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