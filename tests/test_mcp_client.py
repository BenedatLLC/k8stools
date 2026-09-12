
import subprocess
import sys
import os
import re
import pytest
import json
import asyncio
import tempfile

from k8stools.k8s_tools import TOOLS
EXPECTED_TOOLS = [tool.__name__ for tool in TOOLS]

import time
import requests
import threading
import socket

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent



def free_port():
    """Ask the OS for an unused localhost port.

    Using an ephemeral port instead of a fixed one keeps the test from
    colliding with -- and worse, silently testing against -- some other
    k8s-mcp-server that happens to be listening on the same machine.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

def run_server(port, logfile):
    # Start the server with streamable-http transport on the given port.
    # Server output goes to a temp file (not a pipe) so it can't deadlock on a
    # full pipe buffer, and can be reported if startup fails.
    proc = subprocess.Popen([
        sys.executable, '-m', 'k8stools.mcp_server',
        '--transport', 'streamable-http',
        '--port', str(port),
    ], env={**os.environ, 'PYTHONPATH': 'src'},
       stdout=logfile, stderr=subprocess.STDOUT)
    return proc

def wait_for_server(port, proc, timeout=10):
    """Wait for our own server subprocess to answer on port.

    Returns False as soon as the subprocess exits (e.g. it could not bind the
    port), so a failure is reported against our server rather than being
    masked by whatever else may be listening.
    """
    start = time.time()
    while time.time() - start < timeout:
        if proc.poll() is not None:
            return False
        try:
            requests.post(f'http://127.0.0.1:{port}/mcp', timeout=0.5)
            return True
        except Exception:
            time.sleep(0.1)
    return False

def server_output(logfile):
    logfile.seek(0)
    return logfile.read()

def test_k8s_mcp_server_http_tools():
    """Test that k8s-mcp-server (streamable-http) returns the expected set of tools via HTTP POST."""
    port = free_port()
    logfile = tempfile.TemporaryFile(mode='w+')
    proc = run_server(port, logfile)
    try:
        assert wait_for_server(port, proc), (
            f"Server did not start on port {port} "
            f"(subprocess exit code {proc.returncode}). Server output:\n"
            f"{server_output(logfile)}")
        url = f'http://127.0.0.1:{port}/mcp'
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream',
        }
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {}
        }
        resp = requests.post(url, headers=headers, json=payload, timeout=5)
        assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
        # Parse event stream: look for 'data: ...' lines and parse the first JSON object
        json_data = None
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith('data: '):
                try:
                    json_data = line[len('data: '):]
                    break
                except Exception:
                    continue
        assert json_data, f"No data: line found in response: {resp.text}"
        import json
        data = json.loads(json_data)
        assert 'result' in data, f"No result in response: {data}"
        # Accept both {"result": {"tools": [...]}} and {"result": [...]}
        result = data['result']
        if isinstance(result, dict) and 'tools' in result:
            tools_list = result['tools']
        else:
            tools_list = result
        found_tools = [tool['name'] for tool in tools_list]
        for tool in EXPECTED_TOOLS:
            assert tool in found_tools, f"Tool '{tool}' not found in output: {found_tools}"
        assert set(found_tools) == set(EXPECTED_TOOLS), f"Unexpected tools: {set(found_tools) - set(EXPECTED_TOOLS)}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
        logfile.close()

def test_k8s_mcp_client_stdio_short():
    """Test that k8s-mcp-client --short returns the expected set of tools."""
    proc = subprocess.run(
        [sys.executable, '-m', 'k8stools.mcp_client', '--short'],
        capture_output=True,
        text=True,
        env={**os.environ, 'PYTHONPATH': 'src'}
    )
    assert proc.returncode == 0, f"Client failed: {proc.stderr}"
    tool_lines = [line for line in proc.stdout.splitlines() if ' - ' in line]
    found_tools = [re.split(r'\s*-\s*', line)[0].strip() for line in tool_lines]
    for tool in EXPECTED_TOOLS:
        assert tool in found_tools, f"Tool '{tool}' not found in output: {found_tools}"
    assert set(found_tools) == set(EXPECTED_TOOLS), f"Unexpected tools: {set(found_tools) - set(EXPECTED_TOOLS)}"


def test_k8s_mcp_server_stdio_mock():
    """Test that k8s-mcp-server with --mock returns mock data via stdio."""
    
    async def run_test():
        # Create server parameters for stdio connection with mock option
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "k8stools.mcp_server", "--mock"],
            env={**os.environ, 'PYTHONPATH': 'src'}
        )
        
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                # Initialize the connection
                await session.initialize()
                
                # List available tools to verify mock tools are loaded
                tools = await session.list_tools()
                found_tools = [tool.name for tool in tools.tools]
                
                # Verify we have the expected tools (from mock_tools.py)
                from k8stools.mock_tools import TOOLS as MOCK_TOOLS
                expected_mock_tools = [tool.__name__ for tool in MOCK_TOOLS]
                for tool in expected_mock_tools:
                    assert tool in found_tools, f"Mock tool '{tool}' not found in output: {found_tools}"
                
                # Call a mock tool - get_namespaces is simple and doesn't require parameters
                result = await session.call_tool("get_namespaces", {})
                
                # Verify the result has the expected structure
                assert result.content is not None, "Tool call returned no content"
                assert len(result.content) > 0, "Tool call returned empty content"
                
                # Parse the result - it should be JSON containing mock namespace data
                content_text = ""
                if result.content:
                    for content_item in result.content:
                        if isinstance(content_item, TextContent):
                            content_text += content_item.text
                
                # Verify the mock data contains the expected namespaces
                assert "default" in content_text, f"Expected 'default' namespace in mock data: {content_text}"
                assert "kube-system" in content_text, f"Expected 'kube-system' namespace in mock data: {content_text}"
                
                # Verify it looks like structured data (should contain status and age info)
                assert "Active" in content_text, f"Expected 'Active' status in mock namespace data: {content_text}"
                
                return True
    
    # Run the async test
    result = asyncio.run(run_test())
    assert result, "Mock stdio test failed"
