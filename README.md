# Kubernetes Tools

![Unit Tests](https://github.com/BenedatLLC/k8stools/actions/workflows/unit-tests.yml/badge.svg)

This package provides a collection of Kubernetes functions to be used by Agents. They can be passed
directly to an agent as tools or placed behind an MCP server (included). Some use cases include:
* Chat with your kubernetes cluster via GitHub CoPilot or Cursor.
* Build [agents](https://github.com/BenedatLLC/orca-agent/) to monitor your cluster or perform root cause analysis.
* Vibe-code a custom chat UI.
* Use in non-agentic automations.

Here's an example, click to see video:

[![Demo of a live chat](chat-with-cluster.png)](https://youtu.be/huN20MlHPiA)

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

## Current tools

These are the tools we define:

* `get_namespaces` - get a list of namespaces, like `kubectl get namespace`
* `get_node_summaries` - get a list of nodes, like `kubectl get nodes -o wide`
* `get_pod_summaries` - get a list of pods, like `kubectl get pods -o wide`
* `get_pod_container_statuses` - return the status for each of the container in a pod
* `get_pod_events` - return the events for a pod
* `get_pod_spec` - retrieves the spec for a given pod
* `get_logs_for_pod_and_container` - retrieves logs from a pod and container
* `get_deployment_summaries` - get a list of deployments, like `kubectl get deployments`
* `get_service_summaries` - get a list of services, like `kubectl get services`

We also define a set of associated "print_" functions that are helpful in debugging:

* `print_namespaces`
* `print_node_summaries`
* `print_pod_summaries`
* `print_pod_container_statuses`
* `print_pod_events`
* `print_pod_spec`
* `print_deployment_summaries`
* `print_service_summaries`

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
                      [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}] [--debug]

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
[07/21/25 19:55:13] INFO     Starting with 6 tools on transport streamable-http           mcp_server.py:59
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
not go against a real cluster, but return static (but realistic) values. The module
`k8stools.mock_tools` does just that. The data values were captured when running
against a real Minikube instance running the
(Open Telemetry Demo)[https://github.com/open-telemetry/opentelemetry-demo]
application. When running the MCP server, this may be enabled by using the
`--mock` command line option.

## Instruction files
GitHub CoPilot supports *instruction* files that can provide additional context to the CoPilot
Coding Agent. It can even analyze your project and create one for you. By default, this gets
saved to `.github/copilot-instructions.md`. You can manually add instructions to customize
your agent for using your MCP tools. As an example, here's the additional content included
in this repository's `copilot-instructions.md`:

> ### MCP Integration
> Run server: `k8s-mcp-server [--transport stdio|streamable-http]`
> Tools auto-registered via `Tool.from_function()` in `mcp_server.py`
> 
> When answering questions about the user's kubernetes cluster, use the
> tools provided by this server, which is configured in `mcp.json` as
> `k8stools-stdio`. Some other considerations when answering these
> questions:
> * If the answer includes multiple, similar entries, format as a table
>   if possible.
> * When providing pod statuses, be sure to include the state of the pod.
> * When providing a status, use an icon show quickly show if it is good or bad.
> * If you are asked for the current status, and you haven't run a request in
>   more than an minute, be sure to run the tool again to get the latest status.

