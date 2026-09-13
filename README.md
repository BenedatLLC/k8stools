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

## Current tools

These are the tools we define:

* `get_namespaces` - get a list of namespaces, like `kubectl get namespace`
* `get_node_summaries` - get a list of nodes, like `kubectl get nodes -o wide` (includes capacity/allocatable/conditions/taints/labels)
* `get_pod_summaries` - get a list of pods, like `kubectl get pods -o wide`
* `get_pod_container_statuses` - return the status for each of the container in a pod
* `get_pod_events` - return the events for a pod
* `get_pod_spec` - retrieves the spec for a given pod
* `get_logs_for_pod_and_container` - retrieves logs from a pod and container (supports `tail`, `since_seconds`, and `previous`)
* `get_deployment_summaries` - get a list of deployments, like `kubectl get deployments`
* `get_replicaset_summaries` - get a deployment's replica sets with their revision numbers and images. A deployment's replica sets are its change history: use this to see when a workload last changed and what the change was.
* `get_service_summaries` - get a list of services, like `kubectl get services` (includes `selector`/labels/annotations)
* `get_configmap_summaries` - get a list of ConfigMaps, like `kubectl get configmaps`
* `get_configmap` - retrieve the full contents of a single ConfigMap
* `get_statefulset_summaries` - get a list of StatefulSets, like `kubectl get statefulsets`
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
* `print_service_summaries`
* `print_configmap_summaries`
* `print_configmap`
* `print_statefulset_summaries`
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

agent = Agent(
        model="openai:gpt-4.1",
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS
)

result = agent.run_sync("What is the status of the pods in my cluster?")
print(result.output)
```

### Using via MCP
The script `k8s-mcp-server` provides an MCP server for the same set of tools.
Here are the command line arguments for the server:
```
usage: k8s-mcp-server [-h] [--transport {streamable-http,stdio}] [--host HOST] [--port PORT]
                      [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}] [--debug] [--mock]
                      [--state-file FILE] [--state-time {advancing,frozen}] [--no-redact]

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
  --mock                Run mock versions of the tools that don't need a cluster
  --state-file FILE     Serve a captured cluster snapshot from FILE (implies --mock)
  --state-time {advancing,frozen}
                        Replay clock for a captured snapshot [default: advancing]
  --no-redact           Disable secret redaction of tool output (on by default)
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
   set `KUBECONFIG` to point to your `kubectl` config file.

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
`k8stools.mock_tools` does just that. The data values were captured when running
against a real Minikube instance running the
[Open Telemetry Demo](https://github.com/open-telemetry/opentelemetry-demo)
application. When running the MCP server, this may be enabled by using the
`--mock` command line option.

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
usage: k8s-capture-state [-h] [--namespace NS [NS ...]] [-o FILE] [--no-logs]
                         [--max-log-lines MAX_LOG_LINES] [--no-previous-logs]
                         [--no-redact]
                         [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}]

options:
  -h, --help            show this help message and exit
  --namespace NS [NS ...]
                        Namespace(s) to capture namespaced resources from
                        [default: all]. Namespaces and nodes are always
                        captured in full.
  -o FILE, --output FILE
                        Output file [default: k8s-state-<timestamp>.json]
  --no-logs             Skip container logs
  --max-log-lines MAX_LOG_LINES
                        Log lines to capture per container [default: 1000]
  --no-previous-logs    Skip the previous-instance logs of restarted containers
  --no-redact           Disable secret redaction of captured values (redaction
                        is on by default; can also be disabled with
                        K8STOOLS_REDACT=0)
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
| Cluster-wide | namespaces, nodes |
| Per namespace | deployments, replica sets, services, statefulsets, cronjobs, jobs, PVCs, events |
| ConfigMaps | summary **and** full contents, so `get_configmap` works too |
| Per pod | summary, labels, container statuses, spec, and per-container logs |

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

## Secret redaction
Some read-only resources can carry secret-shaped values even though they are not
Kubernetes `Secret` objects — `ConfigMap` data and the `env` blocks in a pod spec
are the common cases. When you run the k8stools MCP server directly against an
agent (with no wrapping service to scrub output), those values would otherwise flow
straight into the model's context.

To prevent that, the MCP server applies a redaction pass to every tool's output.
It is **on by default** and can be disabled with `--no-redact` or by setting
`K8STOOLS_REDACT=0`. Redaction matches two ways, replacing each match with a
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
   a variable someone named `KEY` plausibly does hold one.

If you call the tool functions directly in Python (rather than through the MCP
server), you get raw, un-redacted values; you can apply the same pass yourself via
`k8stools.redaction.redact_object`.