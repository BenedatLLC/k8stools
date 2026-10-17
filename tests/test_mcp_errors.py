"""Anticipated failures reach an MCP client with their reason (issue #25).

The MCP library passes an exception's text to the client only for a
``ToolError``; anything else is reported as a crash, "Error executing tool
<name>", with the text left in the server log. So every K8sApiError, including
4.0's K8sLogReclaimed whose message exists so an agent doesn't misread a missing
log, reached clients without its reason. These tests go through a real MCP
client connected in-process to the tools the server builds.
"""

import asyncio
import json

import pytest
import urllib3
from kubernetes.client import ApiException
from mcp.client.client import Client
from mcp.server.mcpserver import MCPServer

from k8stools import k8s_tools, mock_tools
from k8stools.k8s_tools import K8sApiError, K8sConfigError, K8sLogReclaimed, _api_error_text
from k8stools.mcp_server import build_tools


def _call(fns, name, arguments=None, redact=True):
    server = MCPServer(name="test", tools=build_tools(fns, redact))

    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, arguments or {})
    result = asyncio.run(go())
    return result.is_error, " ".join(getattr(c, "text", "") for c in result.content)


@pytest.fixture(autouse=True)
def builtin_mock():
    mock_tools.load_mock_state()


def test_a_reclaimed_log_reaches_the_client_with_its_explanation():
    is_error, text = _call(mock_tools.TOOLS, "get_logs_for_pod_and_container", {
        "pod_name": "accounting-6c9f8b7d4-x2k8p", "namespace": "default",
        "container_name": "accounting", "previous": True})
    assert is_error
    assert "No log exists for its previous instance of container 'accounting'" in text
    assert "not a tool failure" in text and "get_pod_container_statuses" in text


def test_a_missing_pod_says_not_found():
    is_error, text = _call(mock_tools.TOOLS, "get_pod_container_statuses",
                           {"pod_name": "no-such-pod", "namespace": "default"})
    assert is_error and "no-such-pod" in text and "not found" in text


def test_a_crash_stays_generic():
    """An unexpected exception is the library's to report: no internals leak."""
    def crash() -> str:
        """Always crashes."""
        raise RuntimeError("internal detail that must not reach the client")
    is_error, text = _call([crash], "crash")
    assert is_error and text == "Error executing tool crash"


def test_an_unconfigured_cluster_is_reported_with_its_reason():
    """With no explicit selection the server starts anyway, and each call says why it fails."""
    def unconfigured() -> str:
        """No kubeconfig."""
        raise K8sConfigError("Could not load kube config: no configuration found")
    is_error, text = _call([unconfigured], "unconfigured")
    assert is_error and "Could not load kube config" in text


def test_an_unreachable_api_server_is_named_not_a_crash():
    pool = urllib3.HTTPSConnectionPool("10.0.0.1", 6443)

    def unreachable() -> str:
        """Connection refused."""
        raise urllib3.exceptions.MaxRetryError(pool, "/api/v1/pods", reason=Exception(
            "<urllib3.connection.HTTPSConnection object at 0x7fd21d7ee1b0>: "
            "Failed to establish a new connection: [Errno 111] Connection refused"))
    is_error, text = _call([unreachable], "unreachable")
    assert is_error
    assert text.endswith("can't reach the Kubernetes API server at https://10.0.0.1:6443: "
                         "Failed to establish a new connection: [Errno 111] Connection refused")


@pytest.mark.parametrize("redact, shown", [(True, "[REDACTED]"), (False, "hunter2")])
def test_the_message_is_redacted_like_a_result(redact, shown):
    def leaky() -> str:
        """An API error body that carries a credential."""
        raise K8sApiError("Error fetching pods: 500 Internal Server Error: "
                          "dial postgres://otelu:hunter2@db:5432 failed")
    _, text = _call([leaky], "leaky", redact=redact)
    assert shown in text
    if redact:
        assert "hunter2" not in text


def test_python_callers_still_get_the_exceptions():
    with pytest.raises(K8sLogReclaimed):
        mock_tools.get_logs_for_pod_and_container("accounting-6c9f8b7d4-x2k8p", "default",
                                                  "accounting", previous=True)


# --- one line for a Kubernetes client failure --------------------------------------

def test_an_api_error_is_its_status_and_the_servers_message():
    """str(ApiException) is ~700 characters of headers and raw JSON around one sentence."""
    e = ApiException(status=404, reason="Not Found")
    e.headers = {"Audit-Id": "8444395a", "X-Kubernetes-Pf-Flowschema-Uid": "2718cc8f"}
    e.body = json.dumps({"kind": "Status", "status": "Failure", "reason": "NotFound",
                         "message": 'pods "no-such-pod" not found', "code": 404})
    assert _api_error_text(e) == '404 Not Found: pods "no-such-pod" not found'
    assert "Audit-Id" in str(e)  # what used to be passed through


def test_an_api_error_without_a_json_body_keeps_what_there_is():
    e = ApiException(status=503, reason="Service Unavailable")
    e.body = b"upstream connect error"
    assert _api_error_text(e) == "503 Service Unavailable: upstream connect error"
    assert _api_error_text(ApiException(status=401, reason="Unauthorized")) == "401 Unauthorized"


def test_the_live_tools_use_the_one_line_form(monkeypatch):
    e = ApiException(status=403, reason="Forbidden")
    e.body = json.dumps({"message": 'pods is forbidden: User "agent" cannot list resource "pods"'})

    class Forbidden:
        def list_namespaced_pod(self, **kw):
            raise e
    monkeypatch.setattr(k8s_tools, "K8S", Forbidden())
    with pytest.raises(K8sApiError) as raised:
        k8s_tools.get_pod_summaries("default")
    assert str(raised.value) == ('Error fetching pods: 403 Forbidden: pods is forbidden: '
                                 'User "agent" cannot list resource "pods"')
