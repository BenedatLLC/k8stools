
import subprocess
import sys
import os
import re
import pytest

EXPECTED_TOOLS = [
    'get_namespaces',
    'get_pod_summaries',
    'get_pod_container_statuses',
    'get_pod_events',
    'get_pod_spec',
    'get_logs_for_pod_and_container',
]


import time
import requests
import threading
import socket



def run_server():
    # Start the server with streamable-http transport on port 8000
    proc = subprocess.Popen([
        sys.executable, '-m', 'k8stools.mcp_server',
        '--transport', 'streamable-http',
    ], env={**os.environ, 'PYTHONPATH': 'src'})
    return proc

def wait_for_server(timeout=5):
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.post('http://127.0.0.1:8000/mcp', timeout=0.5)
            return True
        except Exception:
            time.sleep(0.1)
    return False

def test_k8s_mcp_server_http_tools():
    """Test that k8s-mcp-server (streamable-http) returns the expected set of tools via HTTP POST."""
    proc = run_server()
    try:
        assert wait_for_server(), "Server did not start on port 8000"
        url = 'http://127.0.0.1:8000/mcp'
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
