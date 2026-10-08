# pytest configuration to ensure 'k8stools' is importable
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

import pytest

#: Test files allowed to resolve a real cluster binding: the integration tests,
#: and the binding tests, which load kubeconfig files of their own (no network).
_MAY_BIND = {"test_k8s_tools_realk8s.py", "test_cluster_binding.py", "test_mcp_client.py"}


@pytest.fixture(autouse=True)
def _unit_tests_never_reach_a_cluster(request, monkeypatch):
    """Fail a unit test that would bind to a real cluster.

    Locally a kubeconfig usually points somewhere reachable, so a unit test that
    forgot to fake one API client quietly queried a live cluster and passed; in
    CI, with no kubeconfig, the same test failed (get_service_summaries'
    EndpointSlice call, 3.0.0). This makes both behave like CI.
    """
    if request.node.path.name in _MAY_BIND:
        return
    from k8stools import k8s_tools

    def refuse(*args, **kwargs):
        raise AssertionError(
            f"{request.node.nodeid} tried to bind to a real cluster: fake the API client "
            "it uses (e.g. k8s_tools.DISCOVERY_V1_API), or stub the tool function.")
    monkeypatch.setattr(k8s_tools, "_resolve_binding", refuse)
    monkeypatch.setattr(k8s_tools, "_BINDING", None)
