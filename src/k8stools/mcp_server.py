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
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools import Tool
import argparse
import logging
import sys
from pathlib import Path

from .redaction import redaction_enabled, wrap_with_redaction


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
    parser.add_argument('--kubeconfig', metavar='PATH', default=None,
                        help="Kubeconfig file to use [default: KUBECONFIG, then ~/.kube/config]")
    parser.add_argument('--context', metavar='NAME', default=None,
                        help="Kubeconfig context to use [default: K8STOOLS_CONTEXT, then the "
                             "kubeconfig's current-context]. A kubeconfig or context that is "
                             "given but cannot be loaded stops the server rather than falling "
                             "back to another cluster.")
    parser.add_argument('--mock', action='store_true', default=False,
                        help="If specified, just run mock versions of the tools that don't need a cluster")
    parser.add_argument('--state-file', metavar='FILE', default=None,
                        help="Serve a captured cluster snapshot from FILE (implies --mock). "
                             "Captures are written by k8s-capture-state.")
    parser.add_argument('--state-time', choices=['advancing', 'frozen'], default='advancing',
                        help="Replay clock for a captured snapshot [default: advancing]. "
                             "'frozen' pins every age at its captured value, so repeated runs "
                             "are identical - what an automated suite wants.")
    parser.add_argument('--no-redact', action='store_true', default=False,
                        help="Disable secret redaction of tool output (redaction is on by default; "
                             "can also be disabled with K8STOOLS_REDACT=0)")

    args = parser.parse_args()

    # --state-time only means something when a state file is being replayed. A
    # suite that believed it had frozen the clock and had not would produce flaky
    # results with no visible cause, so this is rejected rather than ignored.
    if args.state_time != 'advancing' and not args.state_file:
        parser.error("--state-time requires --state-file")

    # Likewise a cluster selection: mock tools serve a capture, not a cluster, so
    # a server started with one would look pinned to a cluster it never contacts.
    if (args.kubeconfig or args.context) and (args.mock or args.state_file):
        parser.error("--kubeconfig and --context cannot be combined with --mock or --state-file")

    if args.state_file or args.mock:
        from .mock_tools import TOOLS, load_mock_state
        from .mock_state import CaptureFormatError
        frozen = args.state_time == 'frozen'
        try:
            state = load_mock_state(Path(args.state_file) if args.state_file else None,
                                    frozen=frozen)
        except CaptureFormatError as e:
            print(f"Could not load state: {e}", file=sys.stderr)
            return 1
        source = args.state_file if args.state_file else "built-in snapshot"
        logging.warning(f"Using mock tools serving {source} "
                        f"(captured_at={state.captured_at}, clock={args.state_time})")
        if state.redacted:
            # Redaction is one-way, so --no-redact cannot restore anything here.
            logging.info("This capture was written with redaction applied; secret values "
                         "in it are already [REDACTED] and cannot be recovered.")
    else:
        from . import k8s_tools
        from .k8s_tools import TOOLS
        # Bind now rather than on the first tool call, so the log says which
        # cluster this server answers from before anything asks it.
        try:
            logging.warning(f"Bound to cluster: "
                            f"{k8s_tools.configure(args.kubeconfig, args.context)}")
        except k8s_tools.K8sClusterSelectionError as e:
            print(f"Could not bind to the selected cluster: {e}", file=sys.stderr)
            return 1
        except k8s_tools.K8sConfigError as e:
            # No selection was made, so keep the long-standing behavior: start
            # anyway, and let each tool call report the problem (and retry, in
            # case a cluster becomes available).
            logging.warning(f"No cluster configuration could be loaded; tool calls will "
                            f"fail until one can: {e}")

    # Apply secret redaction at the output boundary (default-on, opt-out). See redaction.py.
    redact = redaction_enabled(no_redact_flag=args.no_redact)
    if redact:
        logging.info("Secret redaction of tool output is ENABLED (use --no-redact or "
                     "K8STOOLS_REDACT=0 to disable)")
        fns = [wrap_with_redaction(fn) for fn in TOOLS]
    else:
        logging.warning("Secret redaction of tool output is DISABLED")
        fns = list(TOOLS)
    wrapped_tools = [get_tool_for_function(fn) for fn in fns]

    mcp = MCPServer(
        name="k8stools-"+args.transport,
        tools=wrapped_tools,
        log_level=args.log_level,
        debug=args.debug,
    )
    logging.info(f"Starting with {len(wrapped_tools)} tools on transport {args.transport}")
    # this starts the uvicorn server
    if args.transport == 'streamable-http':
        mcp.run(
            transport='streamable-http',
            host=args.host,
            port=args.port,
            streamable_http_path="/mcp",
            stateless_http=True,
        )
    else:
        mcp.run(transport=args.transport)
    return 0


if __name__=='__main__':
    sys.exit(main())
