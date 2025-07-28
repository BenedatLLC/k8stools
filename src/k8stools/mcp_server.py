# Copyright (c) 2025 Benedat LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
#
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Exposes the tools in k8s_tools in an mcp server

This will run with either the stdio transport or the streamable http transport.
"""
from pydantic import BaseModel
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools import Tool
import argparse
import logging


def get_tool_for_function(fn) -> Tool:
    tool = Tool.from_function(fn, structured_output=True)
    #return_type = fn.__annotations__['return']
    return tool

def main():
    parser = argparse.ArgumentParser(description="Run the MCP server.")
    parser.add_argument('--transport', choices=['streamable-http', 'stdio'],
                        default='stdio',
                        help="Transport to use for MCP server [default: stdio]")
    parser.add_argument('--host', default='127.0.0.1',
                        help="Hostname for HTTP service [default: 127.0.0.1]")
    parser.add_argument('--port', type=int, default=8000,
                        help="Port for HTTP service [default: 8000]")
    parser.add_argument('--log-level', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        default='INFO',
                        help="Log level [default: INFO]")
    parser.add_argument('--debug', action='store_true',
                        help="Enable debug mode [default: False]")
    parser.add_argument('--mock', action='store_true', default=False,
                        help="If specified, just run mock versions of the tools that don't need a cluster")

    args = parser.parse_args()
    if not args.mock:
        from .k8s_tools import TOOLS
    else:
        from .mock_tools import TOOLS
        logging.warning(f"Using mock versions of the tools")
    wrapped_tools = [get_tool_for_function(fn) for fn in TOOLS]

    mcp = FastMCP(
        name="k8stools-"+args.transport,
        tools=wrapped_tools,
        streamable_http_path="/mcp",
        stateless_http=(args.transport == 'streamable-http'),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        debug=args.debug
    )
    logging.debug(f"Settings are: {mcp.settings}")
    logging.info(f"Starting with {len(wrapped_tools)} tools on transport {args.transport}")
    # this starts the uvicorn server
    mcp.run(transport=args.transport)


if __name__=='__main__':
    main()
