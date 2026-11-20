"""get_recent_changes and write records (#38).

Each object's managedFields keeps the time of each client's latest write. These
check the reader (discovery, metadata-only lists, nothing fatal), the logic
(window, created vs updated, controllers counted apart, revisions by timing,
bulk and cap), replay on the capture's clock, the fallback for captures without
write records, the capture itself, and an ITBench import that carries them.
"""

import datetime
import json
from urllib.parse import parse_qsl, urlparse

import pytest
from kubernetes import client

from k8stools import changes, itbench, k8s_tools
from k8stools.capture import capture_state
from k8stools.changes import METADATA_ONLY, is_controller, parse_window, recent_changes
from k8stools.k8s_tools import (K8sApiError, ObjectRecord, ObjectWrites, WriteEntry,
                                pinned_clock, use_api_client)
from k8stools.mock_state import BUILTIN_STATE_FILE, CAPTURE_VERSION, MockState, encode_model

from test_itbench import BASE, _api, _object, _pod, _write

UTC = datetime.timezone.utc
AT = datetime.datetime(2025, 12, 15, 17, 57, 16, tzinfo=UTC)
M = datetime.timedelta(minutes=1)
S = datetime.timedelta(seconds=1)


def _ts(t: datetime.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry(manager, t, operation="Update", subresource=None):
    e = {"manager": manager, "operation": operation, "time": _ts(t),
         "fieldsType": "FieldsV1", "fieldsV1": {"f:spec": {}}}
    if subresource:
        e["subresource"] = subresource
    return e


def _meta(name, created, entries=(), namespace="shop", owner=None, annotations=None):
    meta = {"name": name, "creationTimestamp": _ts(created), "managedFields": list(entries)}
    if namespace:
        meta["namespace"] = namespace
    if owner:
        kind, owner_name = owner.split("/")
        meta["ownerReferences"] = [{"kind": kind, "name": owner_name, "controller": True}]
    if annotations:
        meta["annotations"] = annotations
    return {"apiVersion": "meta.k8s.io/v1", "kind": "PartialObjectMetadata", "metadata": meta}


def _resource(name, kind, namespaced=True, verbs=("get", "list", "watch")):
    return {"name": name, "kind": kind, "namespaced": namespaced, "verbs": list(verbs)}


def _group(name, version="v1"):
    gv = {"groupVersion": f"{name}/{version}", "version": version}
    return {"name": name, "versions": [gv], "preferredVersion": gv}


def _list(*items):
    return {"kind": "PartialObjectMetadataList", "metadata": {}, "items": list(items)}


class _Response:
    def __init__(self, status, body):
        self.status = status
        self.reason = {200: "OK", 403: "Forbidden", 503: "Service Unavailable"}.get(status, "")
        self.data = json.dumps(body).encode()
        self.headers = {"content-type": "application/json"}

    @property
    def response(self):
        return self

    def read(self):
        return self.data

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def getheaders(self):
        return self.headers


class FakeApi:
    """Answers GETs by path, records each with its query and Accept header.
    ``errors`` maps a path to (status, message); an unknown path is an empty
    list, so the capture's other readers find nothing."""

    def __init__(self, routes, errors=None):
        self.routes, self.errors = routes, errors or {}
        self.requests: list[tuple[str, dict, str]] = []

    def GET(self, url, headers=None, query_params=None, **kwargs):  # kubernetes 36
        return self._get(urlparse(url).path, dict(query_params or []), headers or {})

    def request(self, method, url, headers=None, **kwargs):  # kubernetes 37
        parsed = urlparse(url)
        return self._get(parsed.path, dict(parse_qsl(parsed.query)), headers or {})

    def _get(self, path, query, headers):
        path = path.rstrip("/")
        self.requests.append((path, query, headers.get("Accept", "")))
        if path in self.errors:
            status, message = self.errors[path]
            raise client.ApiException(http_resp=_Response(status, {"message": message}))
        if path == "/version":
            raise client.ApiException(http_resp=_Response(404, {"message": "no version"}))
        body = self.routes.get(path, {"items": []})
        if path == "/api/v1/namespaces" and headers.get("Accept") != METADATA_ONLY:
            # A cluster always has namespaces: typed reads see these, Active.
            body = {"items": [{**i, "status": {"phase": "Active"}} for i in body["items"]]}
        elif "items" in body and headers.get("Accept") != METADATA_ONLY:
            body = {"items": []}  # these lists hold metadata only; typed reads see none
        return _Response(200, body)

    def client(self):
        api = client.ApiClient(client.Configuration(host="https://fake.invalid"))
        api.rest_client = self
        return api


ROUTES = {
    "/api/v1": {"resources": [
        _resource("pods", "Pod"), _resource("pods/log", "Pod"),
        _resource("secrets", "Secret"), _resource("events", "Event"),
        _resource("namespaces", "Namespace", namespaced=False),
        _resource("bindings", "Binding", verbs=("create",))]},
    "/apis": {"groups": [_group("apps"), _group("networking.k8s.io"),
                         _group("coordination.k8s.io"), _group("metrics.k8s.io", "v1beta1"),
                         _group("broken.example.com")]},
    "/apis/apps/v1": {"resources": [_resource("deployments", "Deployment"),
                                    _resource("replicasets", "ReplicaSet")]},
    "/apis/networking.k8s.io/v1": {"resources": [_resource("networkpolicies", "NetworkPolicy")]},
    "/apis/coordination.k8s.io/v1": {"resources": [_resource("leases", "Lease")]},
    "/api/v1/namespaces/shop/pods": _list(
        _meta("web-2-abc", AT - 3 * M + 2 * S, [_entry("kube-controller-manager", AT - 3 * M + 2 * S),
                                               _entry("kubelet", AT - M, subresource="status")],
              owner="ReplicaSet/web-2")),
    "/api/v1/namespaces": _list(
        _meta("shop", AT - datetime.timedelta(days=9), [_entry("helm", AT - datetime.timedelta(days=9))],
              namespace=None),
        _meta("agent", AT - 7 * M, [_entry("OpenAPI-Generator", AT - 7 * M)], namespace=None)),
    "/apis/apps/v1/namespaces/shop/deployments": _list(
        _meta("web", AT - datetime.timedelta(days=9),
              [_entry("helm", AT - datetime.timedelta(days=9)), _entry("kubectl-edit", AT - 3 * M),
               _entry("kube-controller-manager", AT - 2 * M, subresource="status")])),
    "/apis/apps/v1/namespaces/shop/replicasets": _list(
        _meta("web-1", AT - datetime.timedelta(days=9), [], owner="Deployment/web",
              annotations={"deployment.kubernetes.io/revision": "1"}),
        _meta("web-2", AT - 3 * M + S, [_entry("kube-controller-manager", AT - 3 * M + S)],
              owner="Deployment/web", annotations={"deployment.kubernetes.io/revision": "2"})),
    "/apis/networking.k8s.io/v1/namespaces/shop/networkpolicies": _list(
        _meta("deny-all", AT - 5 * M, [_entry("kubectl-create", AT - 5 * M)]),
        _meta("old", AT - 2 * 60 * M, [_entry("kubectl-create", AT - 2 * 60 * M)])),
}
ERRORS = {"/api/v1/namespaces/shop/secrets": (403, 'secrets is forbidden: User "system:serviceaccount:'
                                               'k8s:reader" cannot list resource "secrets" in API '
                                               'group "" in the namespace "shop"'),
          "/apis/broken.example.com/v1": (503, "the server is currently unable to handle the request")}


@pytest.fixture
def fake():
    api = FakeApi(ROUTES, ERRORS)
    with use_api_client(api.client()), pinned_clock(AT):
        yield api


# --- the live tool ------------------------------------------------------------------

def test_the_writes_by_clients_newest_first_with_controllers_counted(fake):
    result = k8s_tools.get_recent_changes("shop")
    assert [(c.kind, c.name, c.change, c.manager, c.age, c.revision) for c in result.changes] == [
        ("Deployment", "web", "updated", "kubectl-edit", 3 * M, 2),
        ("NetworkPolicy", "deny-all", "created", "kubectl-create", 5 * M, None),
        ("Namespace", "agent", "created", "OpenAPI-Generator", 7 * M, None)]
    assert result.changes[2].namespace is None  # cluster-scoped
    assert result.left_out == ["kube-controller-manager: 3 writes", "kubelet: 1 write"]
    assert result.within == 30 * M and result.source == "write records"
    assert result.notes == [k8s_tools.NOTE_WRITE_RECORDS, k8s_tools.NOTE_CHANGE_REVISION]


def test_lists_ask_for_metadata_only_and_skip_what_is_written_continually(fake):
    k8s_tools.get_recent_changes("shop")
    lists = [(p, accept) for p, _, accept in fake.requests if p not in ROUTES or "/namespaces" in p]
    assert lists and all(accept == METADATA_ONLY for p, accept in lists if p.count("/") > 3)
    paths = [p for p, _, _ in fake.requests]
    assert not any(p.endswith(("/events", "/leases", "/log", "/bindings")) for p in paths)
    assert not any("metrics.k8s.io" in p for p in paths)


def test_what_cant_be_read_is_named_and_the_rest_carry_on(fake):
    result = k8s_tools.get_recent_changes("shop")
    assert result.not_readable == [
        "*.broken.example.com: 503 Service Unavailable: the server is currently unable to "
        "handle the request",
        'secrets: 403 Forbidden: secrets is forbidden: User "system:serviceaccount:k8s:reader" '
        'cannot list resource "secrets" in API group "" in the namespace "shop" (namespace shop)']


def test_the_window_is_a_parameter(fake):
    assert [c.name for c in k8s_tools.get_recent_changes("shop", "4m").changes] == ["web"]
    assert "old" in [c.name for c in k8s_tools.get_recent_changes("shop", "1d").changes]
    with pytest.raises(K8sApiError, match="within must be a duration like 30m"):
        k8s_tools.get_recent_changes("shop", "half an hour")


def test_a_namespace_that_doesnt_exist_is_an_error_not_no_changes(fake):
    """Listing in a misspelt namespace finds nothing, which read as "nothing
    changed": live, replayed, and from an older capture's creation times."""
    with pytest.raises(K8sApiError, match=r"Namespace 'shpo' not found \(namespaces: agent, shop\)"):
        k8s_tools.get_recent_changes("shpo")
    with pytest.raises(K8sApiError, match="Namespace 'shpo' not found"):
        MockState(json.loads(json.dumps(_captured())), frozen=True).get_recent_changes("shpo")
    data = json.loads(BUILTIN_STATE_FILE.read_text())
    data.pop("writes", None)
    with pytest.raises(K8sApiError, match="Namespace 'otel-demo' not found"):
        MockState(data, frozen=True).get_recent_changes("otel-demo")


def test_without_namespace_records_the_namespace_isnt_checked():
    """A role that can't list Namespaces says nothing of which exist."""
    writes = ObjectWrites(kinds=["pods"], objects=[])
    assert recent_changes(writes, "shop", parse_window("30m"), AT, lambda *a: []).changes == []


def test_a_failed_discovery_is_an_error_an_agent_can_read():
    api = FakeApi(ROUTES, {"/apis": (403, "forbidden")})
    with use_api_client(api.client()), pinned_clock(AT):
        with pytest.raises(k8s_tools.K8sForbidden, match="Error discovering the API's resource types"):
            k8s_tools.get_recent_changes("shop")


def test_records_keep_no_field_contents(fake):
    writes = k8s_tools._object_writes(["shop"])
    assert "fieldsV1" not in json.dumps(encode_model(writes, AT))
    assert "secrets" not in writes.kinds and "pods" in writes.kinds


# --- the logic ---------------------------------------------------------------------

def _obj(name, created, *writes, kind="ConfigMap", owner=None, namespace="shop"):
    return ObjectRecord(kind=kind, namespace=namespace, name=name, created=created, owner=owner,
                        writes=[WriteEntry(manager=m, operation="Update", subresource=s, time=t)
                                for m, t, s in writes])


def _changes(*objects, window=30 * M, births=lambda kind, ns, name: []):
    return recent_changes(ObjectWrites(objects=list(objects)), "shop", window, AT, births)


def test_a_creator_that_wrote_again_leaves_the_creation_without_a_client():
    r = _changes(_obj("cm", AT - 10 * M, ("kubectl", AT - 2 * M, None)))
    assert [(c.change, c.manager, c.age) for c in r.changes] == [
        ("updated", "kubectl", 2 * M), ("created", None, 10 * M)]


def test_controllers_own_what_only_they_wrote_or_own():
    r = _changes(_obj("pod", AT - 5 * M, kind="Pod", owner="ReplicaSet/x"),
                 _obj("ep", AT - 5 * M, ("kube-controller-manager", AT - 4 * M, None), kind="Endpoints"))
    assert r.changes == [] and r.left_out == ["kube-controller-manager: 1 write"]
    assert r.model_dump()["changes"] == []  # [] means none, kept when serialized


def test_the_window_includes_its_edge_and_other_namespaces_are_left_out():
    r = _changes(_obj("edge", AT - 60 * M, ("helm", AT - 30 * M, None)),
                 _obj("older", AT - 60 * M, ("helm", AT - 30 * M - S, None)),
                 _obj("elsewhere", AT - 60 * M, ("helm", AT - M, None), namespace="other"))
    assert [c.name for c in r.changes] == ["edge"]


def test_a_revision_is_reported_only_when_it_followed_a_spec_write_within_5s():
    def births(kind, ns, name):
        return {"a": [(4, AT - 10 * M + 3 * S)], "b": [(7, AT - 10 * M + 6 * S)],
                "c": [(2, AT - 10 * M + S)]}[name]
    old = AT - datetime.timedelta(days=1)
    r = _changes(_obj("a", old, ("kubectl-edit", AT - 10 * M, None), kind="Deployment"),
                 _obj("b", old, ("kubectl-edit", AT - 10 * M, None), kind="Deployment"),
                 _obj("c", old, ("kubectl", AT - 10 * M, "scale"), kind="Deployment"),
                 births=births)
    assert {c.name: c.revision for c in r.changes} == {"a": 4, "b": None, "c": None}


def test_one_client_writing_many_objects_of_a_kind_is_one_line():
    many = [_obj(f"cm{i}", AT - 60 * M, ("helm", AT - 20 * M, None)) for i in range(11)]
    ten = [_obj(f"svc{i}", AT - 60 * M, ("helm", AT - 20 * M, None), kind="Service")
           for i in range(10)]
    r = _changes(*many, *ten)
    assert r.bulk == ["helm updated 11 ConfigMap, newest 20m0s ago"]
    assert [c.kind for c in r.changes] == ["Service"] * 10


def test_at_most_50_records_and_the_rest_counted():
    r = _changes(*[_obj(f"cm{i}", AT - 60 * M, (f"client-{i}", AT - i * S, None))
                   for i in range(60)])
    assert len(r.changes) == 50 and r.changes_more == 10
    assert r.changes[0].name == "cm0"


@pytest.mark.parametrize("text,seconds", [("30m", 1800), ("2h", 7200), ("1d", 86400),
                                          ("90s", 90), ("1h30m", 5400), (" 15M ", 900)])
def test_windows_parse(text, seconds):
    assert parse_window(text) == datetime.timedelta(seconds=seconds)


@pytest.mark.parametrize("text", ["", "30", "m", "1.5h", "30 minutes", "-5m"])
def test_other_windows_are_refused(text):
    with pytest.raises(K8sApiError):
        parse_window(text)


@pytest.mark.parametrize("manager,controller", [
    ("kube-controller-manager", True), ("aws-cloud-controller-manager", True),
    ("csi-attacher", True), ("kubelet", True), ("helm", False), ("kops", False),
    ("cilium-operator", False), ("OpenAPI-Generator", False), ("kubectl-rollout", False),
    ("api-priority-and-fairness-config-producer-v1", True), ("service-cidr-controller", True),
    ("storage-provisioner", True), ("kubeadm", False), ("kubectl-client-side-apply", False),
    (None, False)])
def test_which_clients_are_kubernetes_own_controllers(manager, controller):
    assert is_controller(manager) is controller


# --- replay --------------------------------------------------------------------------

def _captured(**extra):
    with pinned_clock(AT), use_api_client(FakeApi(ROUTES, ERRORS).client()):
        writes = k8s_tools._object_writes(["shop"])
    return {"version": CAPTURE_VERSION, "captured_at": AT.isoformat(), "redacted": False,
            "writes": encode_model(writes, AT), **extra}


def test_replay_answers_as_live_did_at_the_capture(fake):
    live = k8s_tools.get_recent_changes("shop")
    replayed = MockState(json.loads(json.dumps(_captured())), frozen=True).get_recent_changes("shop")
    assert [(c.kind, c.name, c.change, c.manager, c.age, c.revision) for c in replayed.changes] == \
        [(c.kind, c.name, c.change, c.manager, c.age, c.revision) for c in live.changes]
    assert replayed.left_out == live.left_out and replayed.not_readable == live.not_readable


def test_replay_ages_advance_on_the_replay_clock():
    started = datetime.datetime.now(UTC).replace(microsecond=0) - datetime.timedelta(minutes=2)
    state = MockState(json.loads(json.dumps(_captured())), server_start_time=started)
    ages = {c.name: c.age for c in state.get_recent_changes("shop").changes}
    assert 5 * M <= ages["web"] <= 5 * M + 10 * S  # 3m at capture, +2m replayed
    assert "agent" not in {c.name for c in state.get_recent_changes("shop", "8m").changes}


def test_replay_names_common_kinds_it_has_no_records_of():
    r = MockState(json.loads(json.dumps(_captured())), frozen=True).get_recent_changes("shop")
    assert "configmaps" in r.not_recorded and "pods" not in r.not_recorded
    assert "secrets" not in r.not_recorded  # not readable, said so there instead


def test_a_capture_without_write_records_lists_creations_and_says_updates_arent_recorded():
    data = json.loads(BUILTIN_STATE_FILE.read_text())
    data.pop("writes", None)
    r = MockState(data, frozen=True).get_recent_changes("default", "1h")
    assert r.source == "creation times"
    assert r.notes == [k8s_tools.NOTE_CHANGES_FROM_CREATION]
    assert [(c.kind, c.name, c.change, c.manager) for c in r.changes] == [
        ("Deployment", "quote", "created", None), ("PersistentVolumeClaim", "quote-cache", "created", None)]
    pods = MockState(data, frozen=True).get_pod_summaries("default")
    assert not ({p.name for p in pods if p.owner} & {c.name for c in r.changes})


# --- the capture ---------------------------------------------------------------------

def test_a_capture_stores_every_write_record_on_its_clock(fake):
    state = capture_state(namespaces=["shop"], include_logs=False)
    records = {o["name"]: o for o in state["writes"]["objects"]}
    assert records["deny-all"]["created_offset_seconds"] == 300
    assert records["web"]["writes"][1] == {"manager": "kubectl-edit", "operation": "Update",
                                           "subresource": None, "time_offset_seconds": 180.0}
    [secrets] = [u for u in state["writes"]["not_readable"] if u["kind"] == "secrets"]
    assert secrets["reason"].startswith(  # not redacted, and no User "..."
        '403 Forbidden: secrets is forbidden: cannot list resource "secrets"')


def test_a_capture_whose_discovery_fails_records_none():
    api = FakeApi(ROUTES, {"/api/v1": (500, "boom")})
    with use_api_client(api.client()), pinned_clock(AT):
        state = capture_state(namespaces=["shop"], include_logs=False)
    assert "writes" not in state
    r = MockState(json.loads(json.dumps(state)), frozen=True).get_recent_changes("shop")
    assert r.source == "creation times"


# --- an ITBench import -----------------------------------------------------------------

def test_an_itbench_import_carries_the_write_records(tmp_path):
    """Scenario 31's shape: the harness's Role and a NetworkPolicy by
    OpenAPI-Generator, a status write by kube-controller-manager."""
    pull = AT
    policy = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
              "metadata": {"name": "frontend-block-all-ports", "namespace": "shop",
                           "creationTimestamp": _api(pull - 6 * M),
                           "managedFields": [_entry("OpenAPI-Generator", pull - 6 * M)]},
              "spec": {"ingress": [{}], "podSelector": {}, "policyTypes": ["Ingress"]}}
    role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
            "metadata": {"name": "agent-access", "namespace": "shop",
                         "creationTimestamp": _api(pull - 8 * M),
                         "managedFields": [_entry("OpenAPI-Generator", pull - 8 * M)]},
            "rules": []}
    quota = {"apiVersion": "v1", "kind": "ResourceQuota",
             "metadata": {"name": "mem", "namespace": "shop",
                          "creationTimestamp": _api(pull - 4 * M),
                          "managedFields": [_entry("OpenAPI-Generator", pull - 4 * M),
                                            _entry("kube-controller-manager", pull - 4 * M + 16 * S,
                                                   subresource="status")]},
             "spec": {"hard": {"memory": "1Gi"}}, "status": {"hard": {"memory": "1Gi"},
                                                           "used": {"memory": "1200Mi"}}}
    rows = [_object(pull, kind, obj) for kind, obj in BASE + [
        ("pods", _pod()), ("networkpolicies", policy), ("roles", role), ("resourcequotas", quota)]]
    _write(tmp_path / itbench.OBJECTS_FILE, rows)
    _write(tmp_path / itbench.EVENTS_FILE, [])
    _write(tmp_path / itbench.LOGS_FILE, [])
    state = MockState(json.loads(json.dumps(itbench.import_snapshot(tmp_path))), frozen=True)

    r = state.get_recent_changes("shop", "20m")
    assert [(c.kind, c.name, c.manager, c.age) for c in r.changes] == [
        ("ResourceQuota", "mem", "OpenAPI-Generator", 4 * M),
        ("NetworkPolicy", "frontend-block-all-ports", "OpenAPI-Generator", 6 * M),
        ("Role", "agent-access", "OpenAPI-Generator", 8 * M)]
    assert r.left_out == ["kube-controller-manager: 1 write"]
    assert "secrets" in r.not_recorded and "roles.rbac.authorization.k8s.io" not in r.not_recorded
    [q] = state.get_quota_summaries("shop").resource_quotas
    assert (q.resources[0].percent, q.notes) == (117, [k8s_tools.NOTE_QUOTA_AT_LIMIT])
