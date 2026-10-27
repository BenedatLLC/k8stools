"""The clock and client hooks (#30): pinned_clock and use_api_client.

An importer runs the real tools and capture against something that isn't a
cluster, as of a time that isn't now. These pin what that needs: every age is
computed from one clock, every API group (and the readers that build their own
client per call) goes through the supplied ApiClient, and both are restored.
"""

import datetime
import json
from urllib.parse import urlparse

import pytest
from kubernetes import client

from k8stools import k8s_tools
from k8stools.capture import capture_state
from k8stools.k8s_tools import K8sMetricsUnavailable, pinned_clock, use_api_client

AT = datetime.datetime(2025, 12, 15, 18, 4, 39, tzinfo=datetime.timezone.utc)


class _Response:
    def __init__(self, status: int, body):
        self.status = status
        self.reason = "OK" if status == 200 else "Not Found"
        self.data = (body if isinstance(body, str) else json.dumps(body)).encode()

    def getheader(self, name, default=None):
        return "application/json" if name.lower() == "content-type" else default

    def getheaders(self):
        return {"content-type": "application/json"}


class FakeRest:
    """Answers GETs by path from ``routes``; anything else is a 404, or with
    ``empty_lists`` an empty list (except metrics, which stay unavailable)."""

    def __init__(self, routes: dict, empty_lists: bool = False):
        self.routes = routes
        self.empty_lists = empty_lists
        self.paths: list[str] = []

    def GET(self, url, **kwargs):
        path = urlparse(url).path.rstrip("/")
        self.paths.append(path)
        if path not in self.routes and self.empty_lists and "metrics.k8s.io" not in path:
            return _Response(200, {"items": []})
        if path not in self.routes:
            raise client.ApiException(http_resp=_Response(404, {"message": f"{path} not found"}))
        return _Response(200, self.routes[path])


def _api_client(routes: dict, empty_lists: bool = False) -> client.ApiClient:
    api = client.ApiClient(client.Configuration(host="https://snapshot.invalid"))
    api.rest_client = FakeRest(routes, empty_lists)
    return api


def _pod(name: str, created: datetime.datetime) -> dict:
    return {"metadata": {"name": name, "namespace": "shop",
                         "creationTimestamp": created.isoformat().replace("+00:00", "Z")},
            "spec": {"containers": [{"name": "app", "image": "app:1"}], "nodeName": "n1"},
            "status": {"phase": "Running", "containerStatuses": []}}


ROUTES = {
    "/version": {"major": "1", "minor": "31", "gitVersion": "v1.31.0", "gitCommit": "x",
                 "gitTreeState": "clean", "buildDate": "2025-01-01T00:00:00Z",
                 "goVersion": "go1.23", "compiler": "gc", "platform": "linux/amd64"},
    "/api/v1/namespaces/shop/pods": {"items": [_pod("cart-1", AT - datetime.timedelta(hours=3))]},
}


def test_ages_are_as_of_the_pinned_clock():
    with use_api_client(_api_client(ROUTES)), pinned_clock(AT):
        [pod] = k8s_tools.get_pod_summaries("shop")
    assert pod.age == datetime.timedelta(hours=3)


def test_the_clock_is_restored_and_needs_a_zone():
    with pinned_clock(AT):
        assert k8s_tools._now() == AT
        with pinned_clock(AT + datetime.timedelta(minutes=5)):
            assert k8s_tools._now() == AT + datetime.timedelta(minutes=5)
        assert k8s_tools._now() == AT
    assert k8s_tools._now() > AT + datetime.timedelta(days=300)
    with pytest.raises(ValueError, match="timezone-aware"):
        with pinned_clock(AT.replace(tzinfo=None)):
            pass


def test_cluster_info_reports_the_supplied_client():
    with use_api_client(_api_client(ROUTES), context="itbench-33", server="snapshot"):
        info = k8s_tools.get_cluster_info()
    assert (info.source, info.context, info.server, info.server_version) == \
        ("supplied", "itbench-33", "snapshot", "v1.31.0")


def test_readers_that_build_their_own_client_go_through_it_too():
    """The metrics reader makes a CustomObjectsApi per call, not a global."""
    api = _api_client(ROUTES)
    with use_api_client(api), pytest.raises(K8sMetricsUnavailable, match="404"):
        k8s_tools.get_container_metrics("shop")
    assert any(p.startswith("/apis/metrics.k8s.io/") for p in api.rest_client.paths)


def test_the_binding_and_group_clients_are_restored_even_after_an_error():
    sentinel = object()
    saved = k8s_tools._BINDING, k8s_tools.K8S
    k8s_tools.K8S = sentinel
    try:
        with pytest.raises(RuntimeError):
            with use_api_client(_api_client(ROUTES)):
                k8s_tools.get_pod_summaries("shop")
                assert k8s_tools.K8S is not sentinel
                raise RuntimeError
        assert k8s_tools.K8S is sentinel
        assert k8s_tools._BINDING is saved[0]
    finally:
        k8s_tools._BINDING, k8s_tools.K8S = saved


def test_a_capture_is_stamped_and_aged_by_the_pinned_clock():
    """capture_state takes captured_at from the clock and pins it for the whole
    capture, so a pod read at any point is aged as of that timestamp."""
    routes = dict(ROUTES)
    routes["/api/v1/namespaces"] = {"items": [{"metadata": {
        "name": "shop", "creationTimestamp": "2025-12-01T00:00:00Z"}, "status": {"phase": "Active"}}]}
    with use_api_client(_api_client(routes, empty_lists=True)), pinned_clock(AT):
        state = capture_state(["shop"], include_logs=False)
    assert state["captured_at"] == AT.isoformat()
    [pod] = state["pods"]
    assert pod["summary"]["age_seconds"] == 3 * 3600
