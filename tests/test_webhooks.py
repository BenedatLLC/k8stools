"""Admission webhooks (#39): get_webhook_configurations and the workload report.

SREGym's fault: a cluster-scoped MutatingWebhookConfiguration rewrites the
first container's memory on pod create, so the live pod has 16Mi where the
template says 128Mi/256Mi, and no tool could show why. These check what decides
whether a webhook is called (rules, namespace and object selectors), whether
its backend can answer, and that replay and older captures answer as live does.
"""

import datetime
import json
from types import SimpleNamespace as NS

import pytest
from kubernetes import client as c
from kubernetes.client import ApiException

from k8stools import itbench, k8s_tools
from k8stools.composites import _webhook_lines, workload_report
from k8stools.k8s_tools import (K8sApiError, K8sForbidden, K8sNotRecorded,
                                NOTE_MUTATING_FORBIDDEN, NOTE_MUTATING_NOT_RECORDED,
                                NOTE_VALIDATING_NOT_RECORDED, NOTE_WEBHOOK_FAILS_CLOSED,
                                NOTE_WEBHOOK_FAILS_OPEN, NOTE_WEBHOOK_LABELS_UNKNOWN,
                                NOTE_WEBHOOK_MATCH_CONDITIONS, NOTE_WEBHOOK_PATCH,
                                NOTE_WEBHOOK_SELECTS_NO_POD, NOTE_WEBHOOK_UNSERVED,
                                PortInfo, ServiceSummary, WebhookConfigurations, WebhookRule,
                                WebhookSummary, _selector_matches, _selector_text,
                                selector_text_matches, webhook_rule_text)
from k8stools.mock_state import BUILTIN_STATE_FILE, MockState, encode_model

from test_itbench import BASE, _api, _object, _pod, _write

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
DAY = datetime.timedelta(days=1)


# --- client objects --------------------------------------------------------------------

def _rule(resources=("pods",), operations=("CREATE",), groups=("",), scope=None):
    return c.V1RuleWithOperations(operations=list(operations), api_groups=list(groups),
                                  api_versions=["v1"], resources=list(resources), scope=scope)


def _hook(name="memory.example.com", rules=None, service=("webhooks", "mem-svc", None),
          url=None, namespace_selector=None, object_selector=None, failure_policy=None,
          conditions=None, mutating=True, **kw):
    ref = None if service is None else c.AdmissionregistrationV1ServiceReference(
        namespace=service[0], name=service[1], port=service[2], path="/mutate")
    cls = c.V1MutatingWebhook if mutating else c.V1ValidatingWebhook
    return cls(name=name, rules=[_rule()] if rules is None else rules,
               client_config=c.AdmissionregistrationV1WebhookClientConfig(service=ref, url=url),
               namespace_selector=namespace_selector, object_selector=object_selector,
               failure_policy=failure_policy, match_conditions=conditions,
               side_effects="None", admission_review_versions=["v1"], **kw)


def _config(name, *hooks):
    return NS(metadata=c.V1ObjectMeta(name=name, creation_timestamp=NOW - DAY),
              webhooks=list(hooks))


def _service(name, ready=1, ports=(443,), type_="ClusterIP"):
    return ServiceSummary(name=name, namespace="webhooks", type=type_,
                          ports=[PortInfo(port=p, protocol="TCP") for p in ports],
                          age=DAY, ready_endpoints=ready, not_ready_endpoints=0,
                          publish_not_ready_addresses=False)


class _Cluster:
    """Fakes for every API the webhook tool reads: the admission API, the
    backend Services, discovery, the namespace and its pods."""

    def __init__(self, monkeypatch, mutating=(), validating=(), services=None,
                 served=None, ns_labels=None, pods=None, refuse=()):
        self.pod_reads = 0
        services = {"mem-svc": _service("mem-svc")} if services is None else services
        served = {"": {"pods", "pods/status", "configmaps"}} if served is None else served

        def lister(kind, configs):
            def list_():
                if kind in refuse:
                    raise ApiException(status=403, reason="Forbidden")
                return NS(items=list(configs))
            return list_
        monkeypatch.setattr(k8s_tools, "ADMISSION_V1_API", NS(
            list_mutating_webhook_configuration=lister("mutating", mutating),
            list_validating_webhook_configuration=lister("validating", validating)))
        monkeypatch.setattr(k8s_tools, "get_service_summaries",
                            lambda ns=None: list(services.values()))
        monkeypatch.setattr(k8s_tools, "_served_resources",
                            lambda groups: {g: served.get(g, set()) for g in groups})
        monkeypatch.setattr(k8s_tools, "_now", lambda: NOW)

        def read_namespace(name):
            if name != "shop":
                raise ApiException(status=404, reason="Not Found")
            if ns_labels == 403:
                raise ApiException(status=403, reason="Forbidden")
            return NS(metadata=NS(labels=ns_labels or {"kubernetes.io/metadata.name": "shop"}))

        def list_pods(namespace):
            self.pod_reads += 1
            return NS(items=[NS(metadata=NS(name=n, labels=ls)) for n, ls in (pods or {}).items()])
        monkeypatch.setattr(k8s_tools, "K8S", NS(read_namespace=read_namespace,
                                                 list_namespaced_pod=list_pods))


# --- the summary -----------------------------------------------------------------------

def test_a_webhook_as_its_configuration_gives_it(monkeypatch):
    _Cluster(monkeypatch, mutating=[_config("mem-override", _hook(
        object_selector=c.V1LabelSelector(match_labels={"app": "cart"}),
        namespace_selector=c.V1LabelSelector(match_expressions=[c.V1LabelSelectorRequirement(
            key="env", operator="In", values=["prod", "stage"])]),
        timeout_seconds=5))])
    [w] = k8s_tools.get_webhook_configurations().mutating
    assert (w.configuration, w.name, w.age) == ("mem-override", "memory.example.com", DAY)
    assert [webhook_rule_text(r) for r in w.rules] == ["CREATE pods (core/v1)"]
    assert (w.namespace_selector, w.object_selector) == ("env in (prod,stage)", "app=cart")
    # The API's defaults where the object leaves them out.
    assert (w.failure_policy, w.reinvocation_policy, w.timeout_seconds) == ("Fail", "Never", 5)
    assert (w.service, w.backend_ready, w.backend_problem) == ("webhooks/mem-svc:443/mutate", 1, None)
    assert w.unserved_resources == [] and w.notes == []


def test_a_validating_webhook_has_no_reinvocation_policy(monkeypatch):
    _Cluster(monkeypatch, validating=[_config("deny", _hook(mutating=False))])
    result = k8s_tools.get_webhook_configurations()
    assert result.mutating == [] and result.validating[0].reinvocation_policy is None
    assert result.notes == []  # no mutating webhook, so nothing to say about patches


@pytest.mark.parametrize("services,service,problem,ready", [
    ({}, ("webhooks", "mem-svc", None), "Service webhooks/mem-svc not found", None),
    ({"mem-svc": _service("mem-svc", ports=(8443,))}, ("webhooks", "mem-svc", None),
     "Service webhooks/mem-svc has no port 443", None),
    ({"mem-svc": _service("mem-svc", ports=(8443,))}, ("webhooks", "mem-svc", 8443), None, 1),
    ({"mem-svc": _service("mem-svc", ready=0)}, ("webhooks", "mem-svc", None),
     "no ready endpoints", 0),
])
def test_whether_the_backend_can_answer(monkeypatch, services, service, problem, ready):
    _Cluster(monkeypatch, services=services, mutating=[_config("m", _hook(service=service))])
    [w] = k8s_tools.get_webhook_configurations().mutating
    assert (w.backend_problem, w.backend_ready) == (problem, ready)
    assert (NOTE_WEBHOOK_FAILS_CLOSED in w.notes) is (problem is not None)


def test_a_backend_that_cant_answer_refuses_or_lets_through_by_failure_policy(monkeypatch):
    _Cluster(monkeypatch, services={}, mutating=[_config(
        "m", _hook("a.example.com"), _hook("b.example.com", failure_policy="Ignore"))])
    a, b = k8s_tools.get_webhook_configurations().mutating
    assert a.notes == [NOTE_WEBHOOK_FAILS_CLOSED] and b.notes == [NOTE_WEBHOOK_FAILS_OPEN]


def test_a_url_backend_and_unreadable_services_are_unknown_not_down(monkeypatch):
    _Cluster(monkeypatch, mutating=[_config("m", _hook(service=None, url="https://hooks.example/m"))])
    [w] = k8s_tools.get_webhook_configurations().mutating
    assert (w.url, w.service, w.backend_ready, w.backend_problem) == (
        "https://hooks.example/m", None, None, None)

    _Cluster(monkeypatch, mutating=[_config("m", _hook())])

    def refused(ns=None):
        raise K8sForbidden("403 Forbidden: services")
    monkeypatch.setattr(k8s_tools, "get_service_summaries", refused)
    [w] = k8s_tools.get_webhook_configurations().mutating
    assert (w.backend_ready, w.backend_problem, w.notes) == (None, None, [])


def test_rules_for_resources_the_server_doesnt_serve(monkeypatch):
    """A rule on a CRD that isn't installed is inert."""
    _Cluster(monkeypatch, served={"": {"pods", "pods/status"}, "apps": {"deployments"}},
             validating=[_config("v", _hook(mutating=False, rules=[
                 _rule(["pods", "pods/status", "pods/ephemeralcontainers"]),
                 _rule(["widgets", "widgets/*"], groups=["example.com"]),
                 _rule(["deployments", "*"], groups=["apps", "*"]),
                 _rule(["*/*"], groups=["*"])]))])
    [w] = k8s_tools.get_webhook_configurations().validating
    assert w.unserved_resources == ["pods/ephemeralcontainers", "widgets.example.com",
                                    "widgets/*.example.com"]
    assert NOTE_WEBHOOK_UNSERVED in w.notes


def test_discovery_that_fails_leaves_served_unknown(monkeypatch):
    _Cluster(monkeypatch, mutating=[_config("m", _hook())])
    monkeypatch.setattr(k8s_tools, "_served_resources", lambda groups: {g: None for g in groups})
    [w] = k8s_tools.get_webhook_configurations().mutating
    assert w.unserved_resources is None and NOTE_WEBHOOK_UNSERVED not in w.notes


def test_served_resources_from_discovery(monkeypatch):
    from k8stools import changes
    documents = {"/api/v1": {"resources": [{"name": "pods"}, {"name": "pods/status"}]},
                 "/apis/apps": {"versions": [{"groupVersion": "apps/v1"}]},
                 "/apis/apps/v1": {"resources": [{"name": "deployments"}]}}

    def raw_get(path, *args, **kw):
        if path == "/apis/broken.example.com":
            raise ApiException(status=503, reason="Service Unavailable")
        if path not in documents:
            raise ApiException(status=404, reason="Not Found")
        return documents[path]
    monkeypatch.setattr(changes, "_raw_get", raw_get)
    assert k8s_tools._served_resources({"", "apps", "example.com", "broken.example.com"}) == {
        "": {"pods", "pods/status"}, "apps": {"deployments"}, "example.com": set(),
        "broken.example.com": None}


def test_match_conditions_are_shown_and_not_evaluated(monkeypatch):
    _Cluster(monkeypatch, mutating=[_config("m", _hook(conditions=[
        c.V1MatchCondition(name="not-system", expression="!request.userInfo.username.startsWith('system:')")]))])
    [w] = k8s_tools.get_webhook_configurations().mutating
    assert w.match_conditions == ["not-system: !request.userInfo.username.startsWith('system:')"]
    assert w.notes == [NOTE_WEBHOOK_MATCH_CONDITIONS]


@pytest.mark.parametrize("rule,covers", [
    (WebhookRule(operations=["CREATE"], api_groups=[""], api_versions=["v1"], resources=["pods"]), True),
    (WebhookRule(operations=["*"], api_groups=["*"], api_versions=["*"], resources=["*"]), True),
    (WebhookRule(operations=["CREATE"], api_groups=[""], api_versions=["v1"], resources=["*/*"]), True),
    (WebhookRule(operations=["UPDATE"], api_groups=[""], api_versions=["v1"], resources=["pods"]), False),
    (WebhookRule(operations=["CREATE"], api_groups=["apps"], api_versions=["v1"], resources=["pods"]), False),
    (WebhookRule(operations=["CREATE"], api_groups=[""], api_versions=["v1"], resources=["pods/*"]), False),
    (WebhookRule(operations=["CREATE"], api_groups=[""], api_versions=["v1"], resources=["pods"],
                 scope="Cluster"), False),
])
def test_which_rules_cover_creating_a_pod(rule, covers):
    assert rule.covers("", "pods", "CREATE") is covers


SELECTORS = [
    None,
    c.V1LabelSelector(match_labels={"app": "cart"}),
    c.V1LabelSelector(match_labels={"app": "cart", "tier": "web"}),
    c.V1LabelSelector(match_expressions=[c.V1LabelSelectorRequirement(
        key="env", operator="In", values=["prod", "stage"])]),
    c.V1LabelSelector(match_expressions=[c.V1LabelSelectorRequirement(
        key="env", operator="NotIn", values=["prod", "stage"])]),
    c.V1LabelSelector(match_expressions=[
        c.V1LabelSelectorRequirement(key="webhooks.example.com/skip", operator="DoesNotExist"),
        c.V1LabelSelectorRequirement(key="app", operator="Exists")]),
    c.V1LabelSelector(match_expressions=[c.V1LabelSelectorRequirement(
        key="env", operator="Gt", values=["1"])]),
]
LABELS = [{}, {"app": "cart"}, {"app": "cart", "tier": "web"}, {"env": "prod"},
          {"env": "dev", "app": "x"}, {"app": "cart", "webhooks.example.com/skip": "true"}]


@pytest.mark.parametrize("selector", SELECTORS)
def test_a_selector_as_text_matches_as_the_selector_does(selector):
    """Captures store selectors as text; matching the text agrees with the API's rules."""
    for labels in LABELS:
        assert selector_text_matches(_selector_text(selector), labels) is \
            _selector_matches(selector, labels), (selector, labels)


# --- one namespace ---------------------------------------------------------------------

def _shop_cluster(monkeypatch, **kw):
    hooks = [_hook("all.example.com"),
             _hook("prod-only.example.com", namespace_selector=c.V1LabelSelector(
                 match_labels={"env": "prod"})),
             _hook("cart.example.com", object_selector=c.V1LabelSelector(
                 match_labels={"app": "cart"})),
             _hook("nobody.example.com", object_selector=c.V1LabelSelector(
                 match_labels={"app": "nobody"})),
             _hook("configmaps.example.com", rules=[_rule(["configmaps"])],
                   object_selector=c.V1LabelSelector(match_labels={"app": "cart"}))]
    return _Cluster(monkeypatch, mutating=[_config("mem-override", *hooks)],
                    pods={"cart-1": {"app": "cart"}, "web-1": {"app": "web"}}, **kw)


def test_a_namespace_keeps_the_webhooks_its_labels_select(monkeypatch):
    _shop_cluster(monkeypatch, ns_labels={"kubernetes.io/metadata.name": "shop", "env": "dev"})
    r = k8s_tools.get_webhook_configurations("shop")
    assert (r.namespace, r.namespace_labels) == ("shop", {"kubernetes.io/metadata.name": "shop",
                                                          "env": "dev"})
    by_name = {w.name: w for w in r.mutating}
    assert set(by_name) == {"all.example.com", "cart.example.com", "nobody.example.com",
                            "configmaps.example.com"}
    assert r.left_out == ["MutatingWebhookConfiguration/mem-override: webhook "
                          "prod-only.example.com (namespaceSelector env=prod)"]
    # objectSelectors matched to the namespace's pods, for rules covering pods only.
    assert by_name["all.example.com"].object_selector_pods is None  # every pod
    assert by_name["cart.example.com"].object_selector_pods == ["cart-1"]
    assert by_name["nobody.example.com"].object_selector_pods == []
    assert by_name["nobody.example.com"].notes == [NOTE_WEBHOOK_SELECTS_NO_POD]
    assert by_name["configmaps.example.com"].object_selector_pods is None
    assert r.notes == [NOTE_WEBHOOK_PATCH]


def test_pods_are_read_only_when_an_object_selector_needs_them(monkeypatch):
    cluster = _Cluster(monkeypatch, mutating=[_config("m", _hook())])
    k8s_tools.get_webhook_configurations("shop")
    assert cluster.pod_reads == 0
    cluster = _shop_cluster(monkeypatch)
    k8s_tools.get_webhook_configurations("shop")
    assert cluster.pod_reads == 1


def test_a_namespace_that_doesnt_exist_is_an_error(monkeypatch):
    _shop_cluster(monkeypatch)
    with pytest.raises(K8sApiError, match="Namespace 'shp' not found"):
        k8s_tools.get_webhook_configurations("shp")


def test_unreadable_namespace_labels_filter_nothing_and_say_so(monkeypatch):
    _shop_cluster(monkeypatch, ns_labels=403)
    r = k8s_tools.get_webhook_configurations("shop")
    assert len(r.mutating) == 5 and r.left_out == [] and r.namespace_labels is None
    assert NOTE_WEBHOOK_LABELS_UNKNOWN in r.notes


def test_a_role_that_can_read_one_kind_gets_it(monkeypatch):
    _Cluster(monkeypatch, validating=[_config("v", _hook(mutating=False))], refuse=("mutating",))
    r = k8s_tools.get_webhook_configurations()
    assert r.mutating is None and len(r.validating) == 1
    assert r.forbidden == ["MutatingWebhookConfigurations"] and r.notes == [NOTE_MUTATING_FORBIDDEN]
    _Cluster(monkeypatch, refuse=("mutating", "validating"))
    with pytest.raises(K8sForbidden):
        k8s_tools.get_webhook_configurations()


# --- capture and replay ----------------------------------------------------------------

def _captured(monkeypatch, labels=True):
    """The built-in fixture with the shop cluster's webhooks, as a capture stores them."""
    _shop_cluster(monkeypatch, services={})
    data = json.loads(BUILTIN_STATE_FILE.read_text())
    captured_at = datetime.datetime.fromisoformat(data["captured_at"])
    data["mutatingwebhooks"] = [encode_model(w, captured_at)
                                for w in k8s_tools.get_mutating_webhook_summaries()]
    data["validatingwebhooks"] = []
    if not labels:
        for ns in data["namespaces"]:
            ns.pop("labels")
    return data


def _replay(data) -> MockState:
    return MockState(json.loads(json.dumps(data)), frozen=True)


def test_replay_matches_namespaces_and_pods_from_the_capture(monkeypatch):
    data = _captured(monkeypatch)
    for record in data["mutatingwebhooks"]:
        assert "object_selector_pods" not in record and "notes" not in record
    r = _replay(data).get_webhook_configurations("default")
    by_name = {w.name: w for w in r.mutating}
    assert "prod-only.example.com" not in by_name and len(r.left_out) == 1
    # test-deployment's pods carry app=cart in the fixture.
    assert by_name["cart.example.com"].object_selector_pods == [
        "test-deployment-7d4b9c85f-4kq9z", "test-deployment-7d4b9c85f-m7xpw",
        "test-deployment-7d4b9c85f-tb2ns"]
    assert by_name["all.example.com"].backend_problem == "Service webhooks/mem-svc not found"
    assert by_name["all.example.com"].notes == [NOTE_WEBHOOK_FAILS_CLOSED]
    assert len(_replay(data).get_webhook_configurations().mutating) == 5
    with pytest.raises(K8sApiError, match="Namespace 'shop' not found"):
        _replay(data).get_webhook_configurations("shop")


def test_a_capture_without_namespace_labels_filters_nothing(monkeypatch):
    r = _replay(_captured(monkeypatch, labels=False)).get_webhook_configurations("default")
    assert len(r.mutating) == 5 and r.namespace_labels is None
    assert NOTE_WEBHOOK_LABELS_UNKNOWN in r.notes
    assert all(ns.labels is None for ns in
               _replay(_captured(monkeypatch, labels=False)).get_namespaces())


def test_a_capture_before_webhooks_says_not_recorded():
    data = json.loads(BUILTIN_STATE_FILE.read_text())
    del data["mutatingwebhooks"], data["validatingwebhooks"]
    with pytest.raises(K8sNotRecorded) as e:
        _replay(data).get_webhook_configurations("default")
    assert e.value.what == "admission webhooks"
    data["validatingwebhooks"] = []
    r = _replay(data).get_webhook_configurations()
    assert r.mutating is None and r.validating == [] and r.notes == [NOTE_MUTATING_NOT_RECORDED]


def test_the_capture_records_webhooks_and_namespace_labels(monkeypatch):
    from test_capture import _FakeCluster
    from k8stools.capture import capture_state
    _FakeCluster().install(monkeypatch)
    hook = WebhookSummary(configuration="m", name="memory.example.com", age=DAY,
                          object_selector="app=cart", object_selector_pods=["x"])
    monkeypatch.setattr(k8s_tools, "get_mutating_webhook_summaries", lambda: [hook])
    monkeypatch.setattr(k8s_tools, "get_namespaces", lambda: [k8s_tools.NamespaceSummary(
        name="default", status="Active", age=DAY, labels={"kubernetes.io/metadata.name": "default"})])
    state = capture_state()
    assert [r["name"] for r in state["mutatingwebhooks"]] == ["memory.example.com"]
    assert "object_selector_pods" not in state["mutatingwebhooks"][0]
    assert state["validatingwebhooks"] == []
    assert state["namespaces"][0]["labels"] == {"kubernetes.io/metadata.name": "default"}


# --- the workload report ---------------------------------------------------------------

def test_the_report_names_the_webhooks_called_when_its_pods_are_created(monkeypatch):
    report = workload_report(_replay(_captured(monkeypatch)), "test-deployment", "default")
    assert report.admission_webhooks == [
        "MutatingWebhookConfiguration/mem-override: webhook all.example.com; failurePolicy Fail; "
        "backend webhooks/mem-svc:443/mutate (Service webhooks/mem-svc not found); "
        "the API server refuses these pod creates",
        "MutatingWebhookConfiguration/mem-override: webhook cart.example.com; failurePolicy Fail; "
        "objectSelector app=cart; backend webhooks/mem-svc:443/mutate "
        "(Service webhooks/mem-svc not found); the API server refuses these pod creates"]
    # What a webhook's notes say is on its line: a note's "its" can't name one of several.
    assert NOTE_WEBHOOK_PATCH in report.notes
    assert not {NOTE_WEBHOOK_FAILS_CLOSED, NOTE_WEBHOOK_FAILS_OPEN} & set(report.notes)
    # Another workload's pods don't carry app=cart.
    ad = workload_report(_replay(_captured(monkeypatch)), "ad", "default")
    assert [line.split(";")[0] for line in ad.admission_webhooks] == [
        "MutatingWebhookConfiguration/mem-override: webhook all.example.com"]


def test_the_report_says_when_no_webhook_is_called():
    report = workload_report(MockState.from_builtin(frozen=True), "test-deployment", "default")
    assert report.admission_webhooks == ["no admission webhook is called when these pods are created"]
    assert NOTE_WEBHOOK_PATCH not in report.notes


def test_an_older_capture_gives_no_line_and_says_not_recorded():
    data = json.loads(BUILTIN_STATE_FILE.read_text())
    del data["mutatingwebhooks"], data["validatingwebhooks"]
    report = workload_report(_replay(data), "test-deployment", "default")
    assert report.admission_webhooks == []
    assert any("admission webhooks" in n and n.startswith("Not recorded") for n in report.notes)


def test_a_role_without_webhooks_still_gets_its_report():
    state = MockState.from_builtin(frozen=True)

    class Refusing:
        def __getattr__(self, name):
            return getattr(state, name)

        def get_namespace_webhooks(self, namespace):
            raise K8sForbidden("403 Forbidden: mutatingwebhookconfigurations")
    report = workload_report(Refusing(), "test-deployment", "default")
    assert report.admission_webhooks == []
    assert any("admission webhooks" in n and "403" in n for n in report.notes)


def test_without_pods_a_webhook_with_an_object_selector_is_still_named():
    """A validating webhook refusing every create leaves a workload with no pods."""
    hook = WebhookSummary(configuration="v", name="deny.example.com", age=DAY,
                          rules=[WebhookRule(operations=["CREATE"], api_groups=[""],
                                             api_versions=["v1"], resources=["pods"])],
                          object_selector="app=cart", object_selector_pods=[])
    result = WebhookConfigurations(namespace="default", namespace_labels={}, mutating=[],
                                   validating=[hook])
    assert _webhook_lines(result, set())[0] == [
        "ValidatingWebhookConfiguration/v: webhook deny.example.com; failurePolicy Fail; "
        "objectSelector app=cart"]
    assert _webhook_lines(result, {"cart-1"})[0] == [
        "no admission webhook is called when these pods are created"]


def test_the_report_puts_each_webhooks_consequences_on_its_line():
    """Not in detached notes: with several webhooks, "its backend" names none.
    Rules on unserved resources aren't mentioned: a listed webhook covers pod
    CREATE, which is always served, so the note would be about other rules."""
    rule = WebhookRule(operations=["CREATE"], api_groups=["", "example.com"],
                       api_versions=["v1"], resources=["pods", "widgets"])
    hooks = [WebhookSummary(configuration="m", name=name, age=DAY, rules=[rule],
                            failure_policy=policy, service="hooks/svc:443",
                            backend_problem="no ready endpoints", backend_ready=0,
                            match_conditions=["skip-system: true"],
                            unserved_resources=["widgets.example.com"])
             for name, policy in (("a.example.com", "Fail"), ("b.example.com", "Ignore"))]
    assert all(NOTE_WEBHOOK_UNSERVED in w.notes for w in hooks)
    result = WebhookConfigurations(namespace="default", namespace_labels={}, mutating=hooks,
                                   validating=[])
    lines, notes = _webhook_lines(result, {"cart-1"})
    assert lines == [
        "MutatingWebhookConfiguration/m: webhook a.example.com; failurePolicy Fail; "
        "1 matchConditions (not evaluated); backend hooks/svc:443 (no ready endpoints); "
        "the API server refuses these pod creates",
        "MutatingWebhookConfiguration/m: webhook b.example.com; failurePolicy Ignore; "
        "1 matchConditions (not evaluated); backend hooks/svc:443 (no ready endpoints); "
        "pod creates go ahead without it"]
    assert notes == [NOTE_WEBHOOK_PATCH]


def test_the_reports_read_skips_discovery(monkeypatch):
    """The report doesn't use unserved_resources, so its read doesn't pay for them."""
    _shop_cluster(monkeypatch)

    def no_discovery(groups):
        raise AssertionError("discovery on the report's path")
    monkeypatch.setattr(k8s_tools, "_served_resources", no_discovery)
    r = k8s_tools.get_namespace_webhooks("shop")
    assert len(r.mutating) == 4 and all(w.unserved_resources is None for w in r.mutating)
    assert {w.name: w.object_selector_pods for w in r.mutating}["cart.example.com"] == ["cart-1"]


def test_one_kind_not_recorded_gives_no_none_line():
    result = WebhookConfigurations(namespace="default", namespace_labels={}, mutating=None,
                                   validating=[])
    lines, notes = _webhook_lines(result, {"cart-1"})
    assert lines == [] and notes == [NOTE_MUTATING_NOT_RECORDED]


# --- an ITBench import -----------------------------------------------------------------

def test_an_itbench_pull_without_webhooks_doesnt_record_them(tmp_path):
    pull = datetime.datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    _write(tmp_path / itbench.OBJECTS_FILE,
           [_object(pull, kind, obj) for kind, obj in BASE + [("pods", _pod())]])
    _write(tmp_path / itbench.EVENTS_FILE, [])
    _write(tmp_path / itbench.LOGS_FILE, [])
    state = itbench.import_snapshot(tmp_path)
    assert "mutatingwebhooks" not in state and "validatingwebhooks" not in state
    with pytest.raises(K8sNotRecorded):
        _replay(state).get_webhook_configurations("shop")


def test_an_itbench_pull_with_a_webhook_carries_it(tmp_path):
    pull = datetime.datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
    webhook = {"apiVersion": "admissionregistration.k8s.io/v1",
               "kind": "MutatingWebhookConfiguration",
               "metadata": {"name": "mem-override", "creationTimestamp": _api(pull - DAY)},
               "webhooks": [{"name": "memory.example.com", "admissionReviewVersions": ["v1"],
                             "sideEffects": "None", "failurePolicy": "Ignore",
                             "clientConfig": {"service": {"namespace": "shop", "name": "mem"}},
                             "rules": [{"operations": ["CREATE"], "apiGroups": [""],
                                        "apiVersions": ["v1"], "resources": ["pods"]},
                                       {"operations": ["CREATE"], "apiGroups": ["example.com"],
                                        "apiVersions": ["v1"], "resources": ["widgets"]}]}]}
    _write(tmp_path / itbench.OBJECTS_FILE,
           [_object(pull, kind, obj) for kind, obj in
            BASE + [("pods", _pod()), ("mutatingwebhookconfigurations", webhook)]])
    _write(tmp_path / itbench.EVENTS_FILE, [])
    _write(tmp_path / itbench.LOGS_FILE, [])
    r = _replay(itbench.import_snapshot(tmp_path)).get_webhook_configurations("shop")
    [w] = r.mutating
    assert (w.configuration, w.failure_policy, w.service) == ("mem-override", "Ignore", "shop/mem:443")
    # The pull has no Services, so the backend is unknown, not "not found".
    assert (w.backend_ready, w.backend_problem) == (None, None)
    # The pull's discovery serves only its own kinds, so neither the CRD nor
    # any other resource it lacks can be told unserved: unknown.
    assert w.unserved_resources is None and NOTE_WEBHOOK_UNSERVED not in w.notes
    assert r.validating is None and NOTE_VALIDATING_NOT_RECORDED in r.notes
