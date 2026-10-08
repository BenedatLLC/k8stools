"""Tests for get_custom_resource_definitions / get_custom_resource_status (issue #18).

CRDs come from the kubernetes client's own models; instances are plain dicts,
as CustomObjectsApi returns them.
"""

import datetime
import json
from types import SimpleNamespace as NS

import pytest
from kubernetes import client as c

from k8stools import capture, k8s_tools, mock_tools
from k8stools.k8s_tools import NOTE_CR_NO_CONDITIONS, NOTE_CR_NOT_OBSERVED, K8sApiError
from k8stools.mock_state import CAPTURE_VERSION, MockState

UTC = datetime.timezone.utc


def _iso(**ago):
    return (datetime.datetime.now(UTC) - datetime.timedelta(**ago)).isoformat().replace("+00:00", "Z")


def _crd(group="cert-manager.io", plural="certificates", kind="Certificate", scope="Namespaced",
         versions=(("v1", True, True), ("v1alpha2", False, False))):
    return c.V1CustomResourceDefinition(
        metadata=c.V1ObjectMeta(name=f"{plural}.{group}",
                                creation_timestamp=datetime.datetime.now(UTC) - datetime.timedelta(days=8)),
        spec=c.V1CustomResourceDefinitionSpec(
            group=group, scope=scope,
            names=c.V1CustomResourceDefinitionNames(kind=kind, plural=plural, short_names=["cert"]),
            versions=[c.V1CustomResourceDefinitionVersion(name=n, served=served, storage=storage)
                      for n, served, storage in versions]))


def _item(name, conditions=None, generation=1, observed=None, namespace="default", kind="Certificate"):
    status = {}
    if conditions is not None:
        status["conditions"] = conditions
    if observed is not None:
        status["observedGeneration"] = observed
    return {"kind": kind, "metadata": {"name": name, "namespace": namespace, "generation": generation,
                                       "creationTimestamp": _iso(hours=2)},
            "spec": {"secretName": "never-returned"}, "status": status}


def _cond(type_, status, reason=None, message=None, ago=None, observed=None):
    out = {"type": type_, "status": status, "reason": reason, "message": message}
    if ago is not None:
        out["lastTransitionTime"] = _iso(**ago)
    if observed is not None:
        out["observedGeneration"] = observed
    return out


class _FakeCustom:
    def __init__(self, items, error=None):
        self.items, self.error, self.calls = items, error, []

    def list_namespaced_custom_object(self, group, version, namespace, plural):
        self.calls.append((group, version, namespace, plural))
        if self.error:
            raise self.error
        return {"items": [i for i in self.items if i["metadata"].get("namespace") == namespace]}

    def list_cluster_custom_object(self, group, version, plural):
        self.calls.append((group, version, None, plural))
        if self.error:
            raise self.error
        return {"items": self.items}


@pytest.fixture
def serve(monkeypatch):
    def install(crds=(), items=(), error=None):
        fake = _FakeCustom(list(items), error)
        monkeypatch.setattr(k8s_tools.client, "CustomObjectsApi", lambda api_client=None: fake)
        monkeypatch.setattr(k8s_tools, "_binding", lambda: NS(api_client=None))
        by_name = {crd.metadata.name: crd for crd in crds}

        def read(name):
            if name not in by_name:
                raise c.ApiException(status=404, reason="Not Found")
            return by_name[name]
        monkeypatch.setattr(k8s_tools, "APIEXTENSIONS_V1_API", NS(
            list_custom_resource_definition=lambda: NS(items=list(crds)),
            read_custom_resource_definition=read))
        return fake
    return install


def test_definitions_list_served_versions_and_storage(serve):
    serve(crds=[_crd()])
    [d] = k8s_tools.get_custom_resource_definitions()
    assert (d.name, d.group, d.kind, d.plural, d.scope) == (
        "certificates.cert-manager.io", "cert-manager.io", "Certificate", "certificates", "Namespaced")
    assert (d.versions, d.storage_version, d.short_names) == (["v1"], "v1", ["cert"])


def test_status_is_conditions_and_generations_never_spec(serve):
    fake = serve(crds=[_crd()], items=[_item("shop-tls", [
        _cond("Ready", "False", "DoesNotExist", "Issuing certificate as Secret does not exist",
              ago={"hours": 2})], generation=1, observed=1)])
    [r] = k8s_tools.get_custom_resource_status("cert-manager.io", "certificates")
    assert fake.calls == [("cert-manager.io", "v1", None, "certificates")]   # stored version
    assert (r.kind, r.name, r.namespace, r.generation, r.observed_generation) == (
        "Certificate", "shop-tls", "default", 1, 1)
    [cond] = r.conditions
    assert (cond.type, cond.status, cond.reason) == ("Ready", "False", "DoesNotExist")
    assert abs(cond.since - datetime.timedelta(hours=2)) < datetime.timedelta(seconds=5)
    assert r.notes == []
    assert "never-returned" not in r.model_dump_json()


def test_a_controller_behind_the_spec_is_noted(serve):
    serve(crds=[_crd()], items=[_item("a", [_cond("Ready", "True")], generation=3, observed=2)])
    [r] = k8s_tools.get_custom_resource_status("cert-manager.io", "certificates", "v1")
    assert r.notes == [NOTE_CR_NOT_OBSERVED]


def test_observed_generation_falls_back_to_the_conditions(serve):
    """Many operators (and metav1.Condition) record it per condition."""
    serve(items=[_item("a", [_cond("Ready", "True", observed=4), _cond("Synced", "True", observed=5)],
                       generation=5)])
    [r] = k8s_tools.get_custom_resource_status("cert-manager.io", "certificates", "v1")
    assert r.observed_generation == 5 and r.notes == []


def test_no_conditions_is_noted(serve):
    serve(items=[_item("a")])
    [r] = k8s_tools.get_custom_resource_status("cert-manager.io", "certificates", "v1")
    assert r.conditions == [] and r.notes == [NOTE_CR_NO_CONDITIONS]


def test_namespace_uses_the_namespaced_call(serve):
    fake = serve(items=[_item("a"), _item("b", namespace="other")])
    assert [r.name for r in k8s_tools.get_custom_resource_status(
        "cert-manager.io", "certificates", "v1", "other")] == ["b"]
    assert fake.calls[-1] == ("cert-manager.io", "v1", "other", "certificates")


def test_unknown_types_and_errors(serve):
    serve(crds=[])
    with pytest.raises(K8sApiError, match="No custom resource 'widgets.example.com'"):
        k8s_tools.get_custom_resource_status("example.com", "widgets")
    serve(error=c.ApiException(status=404, reason="Not Found"))
    with pytest.raises(K8sApiError, match="No custom resource"):
        k8s_tools.get_custom_resource_status("example.com", "widgets", "v1")
    serve(error=c.ApiException(status=403, reason="Forbidden"))
    with pytest.raises(K8sApiError, match="Error fetching widgets.example.com"):
        k8s_tools.get_custom_resource_status("example.com", "widgets", "v1")


def test_registered_everywhere():
    for name in ("get_custom_resource_definitions", "get_custom_resource_status"):
        assert getattr(k8s_tools, name) in k8s_tools.TOOLS
        assert getattr(mock_tools, name) in mock_tools.TOOLS
        assert getattr(mock_tools, name).__doc__ == getattr(k8s_tools, name).__doc__


# --- capture --------------------------------------------------------------------------

def test_capture_records_what_it_can_read_and_why_not_the_rest(monkeypatch):
    redactor = capture._Redactor(False, capture.CaptureStats())
    crd_ok = k8s_tools.CrdSummary(name="certificates.cert-manager.io", group="cert-manager.io",
                                  kind="Certificate", plural="certificates", scope="Namespaced",
                                  versions=["v1"], storage_version="v1", age=datetime.timedelta(days=1))
    crd_denied = crd_ok.model_copy(update={"name": "secrets.vault.io", "group": "vault.io",
                                           "plural": "secrets", "kind": "VaultSecret"})
    monkeypatch.setattr(k8s_tools, "get_custom_resource_definitions", lambda: [crd_ok, crd_denied])
    many = [k8s_tools.CustomResourceStatus(kind="Certificate", name=f"c{i}", namespace="default",
                                           age=datetime.timedelta(hours=1))
            for i in range(capture.MAX_CUSTOM_RESOURCES_PER_TYPE + 5)]

    def status(group, plural, version=None, namespace=None):
        if group == "vault.io":
            raise K8sApiError("Error fetching secrets.vault.io: (403) Forbidden")
        return many
    monkeypatch.setattr(k8s_tools, "get_custom_resource_status", status)
    now = datetime.datetime.now(UTC)
    record = capture._capture_custom_resources(None, now, redactor)
    ok, denied = record["instances"]
    assert ok["available"] and ok["truncated"] and len(ok["items"]) == capture.MAX_CUSTOM_RESOURCES_PER_TYPE
    assert denied == {"group": "vault.io", "plural": "secrets", "version": "v1",
                      "available": False, "reason": "Error fetching secrets.vault.io: (403) Forbidden"}

    def no_access():
        raise K8sApiError("Error fetching custom resource definitions: (403) Forbidden")
    monkeypatch.setattr(k8s_tools, "get_custom_resource_definitions", no_access)
    assert capture._capture_custom_resources(None, now, redactor)["available"] is False


# --- replay ------------------------------------------------------------------------------

def _state(custom_resources=None):
    data = {"version": CAPTURE_VERSION, "captured_at": datetime.datetime.now(UTC).isoformat(),
            "redacted": False}
    if custom_resources is not None:
        data["custom_resources"] = custom_resources
    return MockState(json.loads(json.dumps(data)), frozen=True)


def test_replay_cases():
    denied = _state({"available": True, "definitions": [], "instances": [
        {"group": "vault.io", "plural": "secrets", "version": "v1", "available": False,
         "reason": "Error fetching secrets.vault.io: (403) Forbidden"}]})
    with pytest.raises(K8sApiError, match="403"):
        denied.get_custom_resource_status("vault.io", "secrets")
    with pytest.raises(K8sApiError, match="No custom resource 'widgets.example.com'"):
        denied.get_custom_resource_status("example.com", "widgets")
    older = _state()
    assert older.get_custom_resource_definitions() == []
    with pytest.raises(K8sApiError, match="predates custom resources"):
        older.get_custom_resource_status("cert-manager.io", "certificates")
    with pytest.raises(K8sApiError, match="not readable|Forbidden"):
        _state({"available": False, "reason": "Forbidden"}).get_custom_resource_definitions()


def test_the_mock_fixture_has_a_certificate_that_isnt_ready():
    """shop-tls is the Secret the shop Ingress serves TLS from."""
    mock_tools.load_mock_state()
    [crd] = mock_tools.get_custom_resource_definitions()
    assert crd.name == "certificates.cert-manager.io"
    certs = {r.name: r for r in mock_tools.get_custom_resource_status("cert-manager.io", "certificates")}
    ready = {name: next(c.status for c in r.conditions if c.type == "Ready") for name, r in certs.items()}
    assert ready == {"shop-tls": "False", "postgres-tls": "True"}
    [ingress] = mock_tools.get_ingress_summaries("default")
    assert ingress.tls[0].secret_name == "shop-tls"
