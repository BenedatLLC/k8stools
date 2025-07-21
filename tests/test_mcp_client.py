
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

def test_k8s_mcp_client_short():
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
