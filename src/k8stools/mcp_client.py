"""Test MCP client: prints the tools available from the MCP server in markdown format.


Based on sample client from the MCP python SDK.
"""

import argparse
import asyncio
import os
import sys
from typing import Any


from pydantic import AnyUrl

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.context import RequestContext
from mcp.types import ListToolsResult, Tool

from rich import print
from rich.console import Console
from rich.markdown import Markdown

# Create server parameters for stdio connection
env = {"PYTHONPATH":'src',}
if 'KUBECONFIG' in os.environ:
    env['KUBECONFIG'] = os.environ['KUBECONFIG']
server_params = StdioServerParameters(
    command=sys.executable,
    args=["-m", "k8stools.mcp_server"],
    env=env
)

def json_to_md(schema:dict[str,Any]) -> str:
    import json
    return f"```json\n{json.dumps(schema, indent=2)}\n```\n"

def tool_to_markdown(tool:Tool):
    r = f"## {tool.title if tool.title else tool.name}\n\n"
    r += f"- Name: {tool.name}\n"
    r += f"- Title: {tool.title}\n"
    r += f"\n### Description\n\n{tool.description}\n"
    r += f"\n### Input Schema\n\n{json_to_md(tool.inputSchema)}\n"
    r += f"\n### Output Schema\n\n"
    if tool.outputSchema:
        r += f"{json_to_md(tool.outputSchema)}\n"
    else:
        r += "No output schema provided\n"

    return r


def print_tools(tools:ListToolsResult, filter_names=None):
    console=Console()
    for tool in tools.tools:
        if filter_names and tool.name not in filter_names:
            continue
        md = tool_to_markdown(tool)
        console.print(Markdown(md))

async def run(filter_names=None):
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # Initialize the connection
            await session.initialize()

            # List available tools
            tools = await session.list_tools()
            print(f"Available tools: {[t.name for t in tools.tools]}")
            print_tools(tools, filter_names)

def main():
    """Entry point for the client script."""
    parser = argparse.ArgumentParser(description="MCP Client")
    parser.add_argument('--tools', nargs='*', help='List of tool names to display')
    args = parser.parse_args()
    asyncio.run(run(args.tools))


if __name__ == "__main__":
    main()