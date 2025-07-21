"""Exposes the tools in k8s_tools in an mcp server
"""
from pydantic import BaseModel
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools import Tool
from .k8s_tools import TOOLS
import argparse


def get_tool_for_function(fn) -> Tool:
    tool = Tool.from_function(fn, structured_output=True)
    #return_type = fn.__annotations__['return']
    return tool

def main():
    parser = argparse.ArgumentParser(description="Run the MCP server.")
    parser.add_argument('--transport', choices=['streamable-http', 'stdio'],
                        default='stdio',
                        help="Transport to use for MCP server [default: stdio]")
    args = parser.parse_args()

    wrapped_tools = [get_tool_for_function(fn) for fn in TOOLS]

    mcp = FastMCP(
        name="k8stools",
        tools=wrapped_tools,
        streamable_http_path="/mcp",
        stateless_http=(args.transport == 'streamable-http')
    )
    mcp.run(transport=args.transport)

# To run the MCP server, use:
#   uvicorn k8stools.mcp_server:app --host 0.0.0.0 --port 8000
# This will serve the FastMCP instance as an ASGI app.

if __name__=='__main__':
    main()
