# k8stools

Kubernetes monitoring tools for AI agents, exposed as direct Python functions or via an MCP server.

## Project layout

```
src/k8stools/
  k8s_tools.py   - Core tools: Kubernetes API wrappers with Pydantic return types
  mock_tools.py  - Static mock versions of all tools (captured from real Minikube + OTel Demo)
  mcp_server.py  - MCP server (stdio or streamable-http transport)
  mcp_client.py  - MCP client used for manual testing
tests/
  test_k8s_tools.py          - Unit tests (mocked K8s API)
  test_k8s_tools_realk8s.py  - Integration tests (real cluster, auto-skipped if unreachable)
  test_mock_tools.py         - Tests for mock_tools module
  test_mcp_client.py         - MCP client tests
```

## Environment setup

```bash
cp envrc.template .envrc
# Set KUBECONFIG in .envrc to point to your kubeconfig file
direnv allow   # or source .envrc manually
uv sync
```

The `.envrc` sets `KUBECONFIG` and adds `.venv/bin` to `PATH`. The MCP server's `mcp.json` config also reads from `.envrc` via `envFile`.

## Running tests

```bash
# Unit tests only (no cluster needed)
uv run pytest tests/test_k8s_tools.py tests/test_mock_tools.py -v

# Integration tests against a real cluster (skipped automatically if unreachable)
KUBECONFIG=~/.kube/your-config.yaml uv run pytest tests/test_k8s_tools_realk8s.py -v

# All tests
uv run pytest -v
```

## Tool design patterns

Tools in `k8s_tools.py` follow three patterns:

1. **kubectl-like** — Pydantic models mimicking `kubectl` output (e.g. `get_pod_summaries` ≈ `kubectl get pods -o wide`)
2. **API-typed** — Pydantic models that match K8s client types with lesser-used fields omitted (e.g. `get_pod_container_statuses`)
3. **Raw dict** — `to_dict()` on the K8s client object; return type is `dict[str, Any]` with fields documented in the docstring (e.g. `get_pod_spec`)

Use `datetime.timedelta` for age/duration fields. Use snake_case field names. Include `pod_name`/`namespace` in container-level models for context.

All tools are collected in the `TOOLS` list in `k8s_tools.py` for agent/MCP registration.

`print_*` companion functions exist for each `get_*` function — for human-readable debugging output only.

## Error handling

```python
raise K8sConfigError("Could not load kube config")   # config/connection problems
raise K8sApiError(f"Error fetching pods: {e}")        # API operation failures
```

## Global API client

Lazy singleton initialization pattern used throughout:

```python
global K8S
if K8S is None:
    K8S = _get_api_client()
```

`_get_api_client()` calls `config.load_kube_config()`, which respects the `KUBECONFIG` environment variable.

## Using tools directly in an agent

```python
from k8stools.k8s_tools import TOOLS

agent = Agent(model="openai:gpt-4.1", system_prompt=SYSTEM_PROMPT, tools=TOOLS)
```

## MCP server

```bash
# stdio (default) — for local coding agents (Copilot, Cursor)
k8s-mcp-server

# HTTP — for remote access
k8s-mcp-server --transport=streamable-http --port=8000

# Mock mode (no real cluster needed)
k8s-mcp-server --mock
```

Example `mcp.json`:
```json
{
  "servers": {
    "k8stools-stdio": {
      "command": "${workspaceFolder}/.venv/bin/k8s-mcp-server",
      "envFile": "${workspaceFolder}/.envrc"
    }
  }
}
```

## Design goals

- Read-only tools first; state-modifying tools will be kept in a separate module when added
- Quality over quantity: well-documented, strongly typed, useful for real monitoring/RCA agents
- Mock data captured from a real Minikube instance running the OpenTelemetry Demo app
