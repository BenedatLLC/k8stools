"""Tests for cluster selection: configure(), the shared binding, get_cluster_info.

No cluster is needed. The kubeconfig here names two fake API servers; loading a
kubeconfig never contacts the server, and the one tool call that would
(get_cluster_info's version read) is faked.
"""

import sys
from types import SimpleNamespace

import pytest
import yaml
from kubernetes import client, config

from k8stools import k8s_tools, mcp_server
from k8stools.mock_state import MockState


def _kubeconfig(current="ctx-a") -> dict:
    return {
        "apiVersion": "v1", "kind": "Config", "current-context": current,
        "clusters": [
            {"name": "a", "cluster": {"server": "https://a.example:6443"}},
            {"name": "b", "cluster": {"server": "https://b.example:6443"}},
        ],
        "users": [{"name": "u", "user": {"token": "not-a-real-token"}}],
        "contexts": [
            {"name": "ctx-a", "context": {"cluster": "a", "user": "u"}},
            {"name": "ctx-b", "context": {"cluster": "b", "user": "u"}},
        ],
    }


@pytest.fixture
def kubeconfig(tmp_path):
    path = tmp_path / "kubeconfig.yaml"
    path.write_text(yaml.safe_dump(_kubeconfig()))
    return path


@pytest.fixture(autouse=True)
def unbound(monkeypatch):
    """Start every test unbound, with no ambient selection, and restore afterwards."""
    for name in ("_BINDING", "K8S", "APPS_V1_API", "BATCH_V1_API"):
        monkeypatch.setattr(k8s_tools, name, None)
    monkeypatch.delenv(k8s_tools.CONTEXT_ENV_VAR, raising=False)


@pytest.fixture
def incluster_calls(monkeypatch):
    """Record (and make succeed) any attempt to fall back to in-cluster config."""
    calls = []

    def fake(client_configuration=None, **kw):
        calls.append(1)
        client_configuration.host = "https://in-cluster.example:443"
    monkeypatch.setattr(config, "load_incluster_config", fake)
    return calls


def _host(api) -> str:
    return api.api_client.configuration.host


# --- selection ---------------------------------------------------------------

def test_current_context_is_used_when_none_is_selected(kubeconfig):
    described = k8s_tools.configure(str(kubeconfig))
    assert "ctx-a" in described and "https://a.example:6443" in described
    assert _host(k8s_tools._get_api_client()) == "https://a.example:6443"


def test_context_argument_selects_the_cluster(kubeconfig):
    k8s_tools.configure(str(kubeconfig), "ctx-b")
    assert _host(k8s_tools._get_api_client()) == "https://b.example:6443"


def test_context_env_var_selects_the_cluster(kubeconfig, monkeypatch):
    monkeypatch.setenv(k8s_tools.CONTEXT_ENV_VAR, "ctx-b")
    k8s_tools.configure(str(kubeconfig))
    assert _host(k8s_tools._get_api_client()) == "https://b.example:6443"


def test_context_argument_wins_over_the_env_var(kubeconfig, monkeypatch):
    monkeypatch.setenv(k8s_tools.CONTEXT_ENV_VAR, "ctx-b")
    k8s_tools.configure(str(kubeconfig), "ctx-a")
    assert _host(k8s_tools._get_api_client()) == "https://a.example:6443"


# --- an explicit selection never falls back ----------------------------------

def test_misspelled_context_fails_instead_of_falling_back_to_in_cluster(
        kubeconfig, incluster_calls):
    """The client raises the same ConfigException for a bad context as for a
    missing kubeconfig. Falling back on it would bind a server running in a pod
    to that pod's own cluster whenever a context name had a typo."""
    with pytest.raises(k8s_tools.K8sClusterSelectionError, match="ctx-typo"):
        k8s_tools.configure(str(kubeconfig), "ctx-typo")
    assert incluster_calls == []
    assert k8s_tools._BINDING is None


def test_missing_kubeconfig_fails_instead_of_falling_back(tmp_path, incluster_calls):
    with pytest.raises(k8s_tools.K8sClusterSelectionError, match="nope.yaml"):
        k8s_tools.configure(str(tmp_path / "nope.yaml"))
    assert incluster_calls == []


def test_a_bad_env_context_names_the_env_var(kubeconfig, monkeypatch, incluster_calls):
    """A stale variable in a server's environment is easy to overlook."""
    monkeypatch.setenv(k8s_tools.CONTEXT_ENV_VAR, "ctx-typo")
    with pytest.raises(k8s_tools.K8sClusterSelectionError, match=k8s_tools.CONTEXT_ENV_VAR):
        k8s_tools.configure(str(kubeconfig))
    assert incluster_calls == []


def test_with_no_selection_the_in_cluster_fallback_is_kept(tmp_path, monkeypatch,
                                                          incluster_calls):
    monkeypatch.setattr(config.kube_config, "KUBE_CONFIG_DEFAULT_LOCATION",
                        str(tmp_path / "absent"))
    described = k8s_tools.configure()
    assert incluster_calls == [1]
    assert "in-cluster" in described
    assert _host(k8s_tools._get_api_client()) == "https://in-cluster.example:443"


def test_with_no_selection_and_no_config_the_error_is_not_a_selection_error(
        tmp_path, monkeypatch):
    """The MCP server starts anyway in this case, as it always has; only a failed
    *selection* stops it."""
    monkeypatch.setattr(config.kube_config, "KUBE_CONFIG_DEFAULT_LOCATION",
                        str(tmp_path / "absent"))

    def fail(**kw):
        raise config.ConfigException("not in a pod")
    monkeypatch.setattr(config, "load_incluster_config", fail)
    with pytest.raises(k8s_tools.K8sConfigError) as excinfo:
        k8s_tools.configure()
    assert not isinstance(excinfo.value, k8s_tools.K8sClusterSelectionError)


# --- one binding for every API group -----------------------------------------

def test_api_groups_cannot_split_across_clusters(kubeconfig, monkeypatch):
    """Regression: each API group used to load the kubeconfig on its own first
    use, so switching the current context between a server's first pod query and
    its first deployment query left it answering from two clusters."""
    monkeypatch.setattr(config.kube_config, "KUBE_CONFIG_DEFAULT_LOCATION", str(kubeconfig))
    core = k8s_tools._get_api_client()                     # lazy, ambient binding
    kubeconfig.write_text(yaml.safe_dump(_kubeconfig(current="ctx-b")))  # use-context
    apps = k8s_tools._get_apps_v1_api_client()
    batch = k8s_tools._get_batch_v1_api_client()
    assert _host(core) == _host(apps) == _host(batch) == "https://a.example:6443"
    assert core.api_client is apps.api_client is batch.api_client


def test_configure_rebinds_clients_already_created(kubeconfig):
    k8s_tools.configure(str(kubeconfig), "ctx-a")
    k8s_tools.K8S = k8s_tools._get_api_client()
    k8s_tools.configure(str(kubeconfig), "ctx-b")
    assert k8s_tools.K8S is None and k8s_tools.APPS_V1_API is None
    assert _host(k8s_tools._get_api_client()) == "https://b.example:6443"


def test_binding_leaves_the_process_wide_default_configuration_alone(kubeconfig):
    before = client.Configuration.get_default_copy().host
    k8s_tools.configure(str(kubeconfig), "ctx-b")
    assert client.Configuration.get_default_copy().host == before


# --- get_cluster_info ---------------------------------------------------------

def _fake_version_api(monkeypatch, git_version=None, error=None):
    def get_code():
        if error:
            raise error
        return SimpleNamespace(git_version=git_version)
    monkeypatch.setattr(client, "VersionApi",
                        lambda api_client: SimpleNamespace(get_code=get_code))


def test_cluster_info_reports_the_binding(kubeconfig, monkeypatch):
    _fake_version_api(monkeypatch, git_version="v1.31.2")
    k8s_tools.configure(str(kubeconfig), "ctx-b")
    info = k8s_tools.get_cluster_info()
    assert info.source == "kubeconfig"
    assert info.context == "ctx-b"
    assert info.server == "https://b.example:6443"
    assert info.kubeconfig == str(kubeconfig)
    assert info.server_version == "v1.31.2"
    assert info.captured_at is None


def test_cluster_info_still_answers_when_the_server_is_unreachable(kubeconfig, monkeypatch):
    """Which cluster the tools are bound to is the question; an unreachable
    server is part of the answer, not a reason to give none."""
    _fake_version_api(monkeypatch, error=ConnectionError("refused"))
    k8s_tools.configure(str(kubeconfig))
    info = k8s_tools.get_cluster_info()
    assert info.context == "ctx-a" and info.server_version is None


def test_cluster_info_carries_no_credentials(kubeconfig, monkeypatch):
    _fake_version_api(monkeypatch, git_version="v1.31.2")
    k8s_tools.configure(str(kubeconfig))
    dumped = k8s_tools.get_cluster_info().model_dump_json()
    assert "not-a-real-token" not in dumped
    assert '"u"' not in dumped  # nor the kubeconfig user


def test_replayed_cluster_info_says_it_is_a_capture():
    state = MockState({
        "version": "1", "captured_at": "2026-09-27T21:50:53+00:00", "redacted": True,
        "cluster": {"context": "prod", "server": "https://prod.example:6443",
                    "server_version": "v1.31.2"}})
    info = state.get_cluster_info()
    assert info.source == "capture"
    assert (info.context, info.server, info.server_version) == \
        ("prod", "https://prod.example:6443", "v1.31.2")
    assert info.kubeconfig is None


@pytest.mark.parametrize("frozen", [True, False])
def test_replayed_captured_at_is_on_the_same_clock_as_everything_else(frozen):
    """Issue #7: captured_at was the date in the file while every other datetime
    was re-anchored to the server's start, so a pod killed "5 minutes ago"
    appeared beside a capture taken a month ago."""
    import datetime, json
    recorded = "2026-08-01T12:00:00+00:00"
    start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    status = {"pod_name": "p", "namespace": "default", "container_name": "c",
              "image": "img", "ready": False, "restart_count": 1, "started": False,
              "stop_signal": None, "state": None, "volume_mounts": [],
              "resource_requests": {}, "resource_limits": {}, "allocated_resources": {},
              "last_state": {"state_name": "Terminated", "exit_code": 137,
                             "started_at_offset_seconds": 360.0,
                             "finished_at_offset_seconds": 297.0}}
    state = MockState(json.loads(json.dumps({
        "version": "1", "captured_at": recorded, "redacted": False,
        "pods": [{"summary": {"name": "p", "namespace": "default"},
                  "container_statuses": [status]}]})),
        frozen=frozen, server_start_time=start)
    captured_at = state.get_cluster_info().captured_at
    finished_at = state.get_pod_container_statuses("p", "default")[0].last_state.finished_at
    assert captured_at == start
    assert captured_at - finished_at == datetime.timedelta(seconds=297)
    assert state.captured_at == recorded   # the file's own date stays available


def test_a_capture_without_a_cluster_record_still_answers():
    info = MockState.from_builtin(frozen=True).get_cluster_info()
    assert info.source == "capture"
    assert info.context is None and info.captured_at is not None


# --- the MCP server -----------------------------------------------------------

def _run_server(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["k8s-mcp-server", *argv])
    return mcp_server.main()


@pytest.mark.parametrize("flags", [["--context", "x"], ["--kubeconfig", "k.yaml"]])
@pytest.mark.parametrize("mode", [["--mock"], ["--state-file", "s.json"]])
def test_server_rejects_a_cluster_selection_in_mock_mode(monkeypatch, flags, mode):
    with pytest.raises(SystemExit) as excinfo:
        _run_server(monkeypatch, *mode, *flags)
    assert excinfo.value.code == 2


def test_server_refuses_to_start_on_a_failed_selection(kubeconfig, monkeypatch,
                                                       incluster_calls, capsys):
    assert _run_server(monkeypatch, "--kubeconfig", str(kubeconfig),
                       "--context", "ctx-typo") == 1
    assert "ctx-typo" in capsys.readouterr().err
    assert incluster_calls == []
