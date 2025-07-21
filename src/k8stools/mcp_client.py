"""Test MCP client: prints the tools available from the MCP server in markdown format.


Based on sample client from the MCP python SDK.
"""

import argparse
import asyncio
import os
from os.path import dirname, join, abspath, exists
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

print(f"sys.argv[0] = {sys.argv[0]}") # XXX
if sys.argv[0].endswith('ks8-mcp-client'):
    # this was run as an installed script
    COMMAND = join(dirname(abspath(sys.argv[0])), 'k8s-mcp-server')
    if not exists(COMMAND):
        raise Exception(f"Did not find server script at {COMMAND}")
    ENV = {}
    ARGS = []
else:
    COMMAND=sys.executable
    ENV =  {"PYTHONPATH":'src',}
    ARGS = ["-m", "k8stools.mcp_server"]

# Create server parameters for stdio connection
if 'KUBECONFIG' in os.environ:
    ENV['KUBECONFIG'] = os.environ['KUBECONFIG']
server_params = StdioServerParameters(
    command=COMMAND,
    args=ARGS,
    env=ENV
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


def print_tools(tools:ListToolsResult, filter_names=None, short=False):
    console=Console()
    for tool in tools.tools:
        if filter_names and tool.name not in filter_names:
            continue
        if short:
            first_line = tool.description.strip().split('\n')[0]
            console.print(f"{tool.name} - {tool.title if tool.title else first_line}")
        else:
            md = tool_to_markdown(tool)
            console.print(Markdown(md))

async def run(short=False, filter_names=None):
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            # Initialize the connection
            await session.initialize()

            # List available tools
            tools = await session.list_tools()
            print_tools(tools, filter_names, short=short)

def main():
    """Entry point for the client script."""
    parser = argparse.ArgumentParser(description="MCP Client")
    parser.add_argument('--short', action='store_true', default=False,
                        help="If specified, only provide short outputs for the tools")
    parser.add_argument('--tools', nargs='*', help='List of tool names to display')
    args = parser.parse_args()
    asyncio.run(run(args.short, args.tools))


if __name__ == "__main__":
    main()