"""Tests for toolsets (issue #20): named subsets of the tools, and the server flags."""

import sys

import pytest

from k8stools import k8s_tools, mcp_server, mock_tools
from k8stools.k8s_tools import OVERLAPPING_TOOLS, TOOLSET_NAMES, select_tools


def _names(tools):
    return [fn.__name__ for fn in tools]


def test_every_tool_is_in_all_and_toolsets_only_name_real_tools():
    """A new tool lands in "all" automatically; a toolset can't name a tool that
    doesn't exist (a renamed tool would otherwise silently drop out)."""
    assert _names(k8s_tools.TOOLSETS["all"]) == _names(k8s_tools.TOOLS)
    every = set(_names(k8s_tools.TOOLS))
    for name, members in TOOLSET_NAMES.items():
        assert set(members) <= every, name


def test_triage_is_the_composites_plus_events_nodes_and_cluster_info():
    """#13's composites answer "what is wrong, and where" in one or two calls."""
    assert set(TOOLSET_NAMES["triage"]) == {"get_cluster_info", "get_namespace_health",
                                            "get_workload_report", "get_events",
                                            "get_node_summaries"}


def test_toolsets_nest_and_investigate_drops_only_the_overlapping_tools():
    triage, investigate, everything = (set(TOOLSET_NAMES[n]) for n in ("triage", "investigate", "all"))
    assert triage <= investigate <= everything
    assert everything - investigate == set(OVERLAPPING_TOOLS)


def test_overlapping_tools_are_still_in_the_library():
    for name in OVERLAPPING_TOOLS:
        assert callable(getattr(k8s_tools, name)) and callable(getattr(mock_tools, name))


def test_mock_toolsets_mirror_the_real_ones():
    for name in TOOLSET_NAMES:
        assert _names(mock_tools.TOOLSETS[name]) == _names(k8s_tools.TOOLSETS[name])
        assert all(fn.__module__ == "k8stools.mock_tools" for fn in mock_tools.TOOLSETS[name])


def test_include_and_exclude_adjust_a_toolset_in_tool_order():
    tools = select_tools(k8s_tools.TOOLS, "triage", include=("get_pod_spec",),
                         exclude=("get_events",))
    assert _names(tools) == ["get_cluster_info", "get_namespace_health", "get_workload_report",
                             "get_node_summaries", "get_pod_spec"]


@pytest.mark.parametrize("kwargs,match", [
    ({"toolset": "everything"}, "Unknown toolset"),
    ({"include": ("get_pods",)}, "get_pods"),
    ({"exclude": ("get_podz",)}, "get_podz"),
])
def test_unknown_names_fail_loudly(kwargs, match):
    with pytest.raises(ValueError, match=match):
        select_tools(k8s_tools.TOOLS, **kwargs)


def test_the_default_is_still_all():
    """Changing it removes tools from existing MCP users; that's for 3.0.0's
    release decision (#20, #21), not a side effect of adding toolsets."""
    assert k8s_tools.DEFAULT_TOOLSET == "all"


# --- the server --------------------------------------------------------------

def _serve(monkeypatch, *argv):
    """Run the server's startup with these flags; return the tool names it would serve."""
    served = {}

    class _Server:
        def __init__(self, name, tools, **kw):
            served["names"] = [t.name for t in tools]

        def run(self, **kw):
            pass

    monkeypatch.setattr(mcp_server, "MCPServer", _Server)
    monkeypatch.setattr(sys, "argv", ["k8s-mcp-server", "--mock", *argv])
    mcp_server.main()
    return served["names"]


def test_server_serves_the_chosen_toolset(monkeypatch):
    assert _serve(monkeypatch) == _names(k8s_tools.TOOLS)
    assert _serve(monkeypatch, "--toolset", "triage") == list(
        n for n in _names(k8s_tools.TOOLS) if n in TOOLSET_NAMES["triage"])
    assert _serve(monkeypatch, "--toolset", "triage", "--include", "get_pod_spec",
                  "--exclude", "get_events", "--exclude", "get_cluster_info") == [
        "get_namespace_health", "get_workload_report", "get_node_summaries", "get_pod_spec"]


@pytest.mark.parametrize("argv", [["--toolset", "nope"], ["--include", "get_pods"]])
def test_server_rejects_unknown_names_at_startup(monkeypatch, argv):
    with pytest.raises(SystemExit) as excinfo:
        _serve(monkeypatch, *argv)
    assert excinfo.value.code == 2
