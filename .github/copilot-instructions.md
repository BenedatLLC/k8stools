# K8stools Development Guide

## Architecture Overview

This project provides Kubernetes monitoring tools accessible via two interfaces:
- **Direct function calls** (`k8stools.k8s_tools.TOOLS`) for agent integration
- **MCP (Model Context Protocol) server** for AI assistant integration via stdio or HTTP

### Core Components

- `src/k8stools/k8s_tools.py` - Core Kubernetes API wrappers with strongly-typed Pydantic models
- `src/k8stools/mcp_server.py` - MCP server implementation using FastMCP
- `src/k8stools/mcp_client.py` - MCP client for testing

## Development Patterns

### Tool Design Philosophy
Tools follow three patterns documented in `k8s_tools.py`:
1. **kubectl-like**: Return Pydantic models mimicking kubectl output (e.g., `get_pod_summaries`)
2. **API-typed**: Return Pydantic models matching K8s client types (e.g., `get_pod_container_statuses`) 
3. **Raw dict**: Call `to_dict()` on K8s client objects with documented fields (e.g., `get_pod_spec`)

### Error Handling Pattern
All tools use consistent custom exceptions:
```python
# Configuration/connection errors
raise K8sConfigError("Could not load kube config")
# API operation errors  
raise K8sApiError(f"Error fetching pods: {e}")
```

### Global API Client Pattern
Uses singleton pattern with lazy initialization:
```python
global K8S
if K8S is None:
    K8S = _get_api_client()
```

## Development Workflow

### Environment Setup
1. Copy `envrc.template` to `.envrc` and set `KUBECONFIG` path
2. Install with `uv add k8stools` or `pip install k8stools`
3. Virtual environment expected at `.venv/` (see `mcp.json` config)

### Testing Strategy
- `tests/test_k8s_tools.py` - Unit tests with mocked K8s API (`MockK8S` class)
- `tests/test_k8s_tools_realk8s.py` - Integration tests requiring real cluster
- Mock pattern uses `SimpleNamespace` objects to simulate K8s API responses

### MCP Server Usage
Two transport modes in `mcp_server.py`:
- **stdio**: Default for local AI assistants (GitHub Copilot, Cursor)
- **streamable-http**: For remote access via HTTP on localhost:8000

Example `mcp.json` configuration:
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

## Key Conventions

### Pydantic Model Patterns
- Use `datetime.timedelta` for age/duration fields (not raw timestamps)
- Include pod_name/namespace in container-level models for context
- Follow K8s API field naming but use snake_case

### Function Naming
- `get_*` functions return typed data
- `print_*` functions format output for human consumption (debugging)
- All functions collected in `TOOLS` list for MCP/agent registration

### Documentation Standards
- Comprehensive docstrings with Parameters/Returns/Raises sections
- Document Pydantic model fields inline when they map to K8s concepts
- Reference equivalent kubectl commands where applicable

## Integration Points

### Agent Integration
Import and use tools directly:
```python
from k8stools.k8s_tools import TOOLS
# Register TOOLS with your agent framework
```

### MCP Integration
Run server: `k8s-mcp-server [--transport stdio|streamable-http]`
Tools auto-registered via `Tool.from_function()` in `mcp_server.py`

When answering questions about the user's kubernetes cluster, use the
tools provided by this server, which is configured in `mcp.json` as
`k8stools-stdio`. Some other considerations when answering these
questions:
* If the answer includes multiple, similar entries, format as a table
  if possible.
* When providing pod statuses, be sure to include the state of the pod.
* When providing a status, use an icon show quickly show if it is good or bad.
* If you are asked for the current status, and you haven't run a request in
  more than an minute, be sure to run the tool again to get the latest status.
