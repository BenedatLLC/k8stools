"""ResourceQuotas and LimitRanges (#38): the tool, the namespace view's line,
capture and replay. ITBench Scenario 102's fault is a quota its capture didn't
record; these check it now is, and reads as at its limit. Built from the
kubernetes client's own models."""

import datetime
import json
from types import SimpleNamespace as NS

import pytest
from kubernetes import client as c

from k8stools import k8s_tools
from k8stools.composites import namespace_health
from k8stools.k8s_tools import K8sForbidden, K8sNotRecorded, NOTE_QUOTA_AT_LIMIT
from k8stools.mock_state import BUILTIN_STATE_FILE, CAPTURE_VERSION, MockState, encode_model

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC).replace(microsecond=0)


def _quota(name="otel-demo-memory", hard=None, used=None, scopes=None, selector=None):
    return c.V1ResourceQuota(
        metadata=c.V1ObjectMeta(name=name, namespace="default",
                                creation_timestamp=NOW - datetime.timedelta(minutes=4, seconds=45)),
        spec=c.V1ResourceQuotaSpec(hard=hard or {"limits.memory": "2Gi", "pods": "10"},
                                   scopes=scopes, scope_selector=selector),
        status=c.V1ResourceQuotaStatus(hard=hard, used=used))


def _limit_range():
    return c.V1LimitRange(
        metadata=c.V1ObjectMeta(name="limits", namespace="default",
                                creation_timestamp=NOW - datetime.timedelta(days=4)),
        spec=c.V1LimitRangeSpec(limits=[c.V1LimitRangeItem(
            type="Container", default={"cpu": "500m"}, default_request={"cpu": "100m"},
            max={"memory": "1Gi"})]))


@pytest.fixture
def serve(monkeypatch):
    monkeypatch.setattr(k8s_tools, "_CLOCK", lambda: NOW)  # ages as of NOW, however late

    def install(quotas=(), limit_ranges=()):
        monkeypatch.setattr(k8s_tools, "K8S", NS(
            list_namespaced_resource_quota=lambda namespace: NS(items=list(quotas)),
            list_resource_quota_for_all_namespaces=lambda: NS(items=list(quotas)),
            list_namespaced_limit_range=lambda namespace: NS(items=list(limit_ranges)),
            list_limit_range_for_all_namespaces=lambda: NS(items=list(limit_ranges))))
        return k8s_tools.get_quota_summaries("default")
    return install


def test_each_resource_with_used_hard_and_percent(serve):
    result = serve([_quota(used={"limits.memory": "1536Mi", "pods": "3"})], [_limit_range()])
    [q] = result.resource_quotas
    assert [(r.resource, r.used, r.hard, r.percent) for r in q.resources] == [
        ("limits.memory", "1536Mi", "2Gi", 75), ("pods", "3", "10", 30)]
    assert q.notes == [] and q.age == datetime.timedelta(minutes=4, seconds=45)
    [lr] = result.limit_ranges
    assert (lr.limits[0].type, lr.limits[0].default, lr.limits[0].default_request,
            lr.limits[0].max) == ("Container", {"cpu": "500m"}, {"cpu": "100m"}, {"memory": "1Gi"})
    assert result.notes == []


def test_a_quota_at_its_limit_says_what_the_api_then_refuses(serve):
    # Scenario 102's shape: hard written in milli-units, used past it.
    [q] = serve([_quota(hard={"memory": "1503238553600m"}, used={"memory": "1901Mi"})]).resource_quotas
    assert q.resources[0].percent == 132
    assert q.notes == [NOTE_QUOTA_AT_LIMIT]


def test_not_counted_yet_and_scopes(serve):
    selector = c.V1ScopeSelector(match_expressions=[c.V1ScopedResourceSelectorRequirement(
        scope_name="PriorityClass", operator="In", values=["high"])])
    [q] = serve([_quota(scopes=["NotBestEffort"], selector=selector)]).resource_quotas
    assert [(r.used, r.percent) for r in q.resources] == [(None, None), (None, None)]
    assert (q.scopes, q.scope_selector) == (["NotBestEffort"], "PriorityClass In (high)")


def test_a_hard_limit_of_zero_has_no_percent_but_is_at_its_limit(serve):
    [q] = serve([_quota(hard={"pods": "0"}, used={"pods": "0"})]).resource_quotas
    assert q.resources[0].percent is None and q.notes == [NOTE_QUOTA_AT_LIMIT]


def _refuse(**kwargs):
    raise c.ApiException(status=403, reason="Forbidden")


def test_both_halves_refused_is_forbidden(monkeypatch):
    monkeypatch.setattr(k8s_tools, "K8S", NS(list_namespaced_resource_quota=_refuse,
                                             list_namespaced_limit_range=_refuse))
    with pytest.raises(K8sForbidden, match="Error fetching resource quotas"):
        k8s_tools.get_quota_summaries("default")


def test_one_half_refused_gives_the_other_and_says_so(monkeypatch):
    """#43: a role with ResourceQuotas and not LimitRanges, or the reverse,
    gets the half it can read, as replay already gave."""
    monkeypatch.setattr(k8s_tools, "_CLOCK", lambda: NOW)
    monkeypatch.setattr(k8s_tools, "K8S", NS(
        list_namespaced_resource_quota=lambda namespace: NS(items=[_quota()]),
        list_namespaced_limit_range=_refuse))
    result = k8s_tools.get_quota_summaries("default")
    assert [q.name for q in result.resource_quotas] == ["otel-demo-memory"]
    assert result.limit_ranges is None and result.notes == [k8s_tools.NOTE_LIMIT_RANGES_FORBIDDEN]
    monkeypatch.setattr(k8s_tools, "K8S", NS(
        list_namespaced_resource_quota=_refuse,
        list_namespaced_limit_range=lambda namespace: NS(items=[_limit_range()])))
    result = k8s_tools.get_quota_summaries("default")
    assert result.resource_quotas is None and [lr.name for lr in result.limit_ranges] == ["limits"]
    assert result.notes == [k8s_tools.NOTE_QUOTAS_FORBIDDEN]


# --- capture and replay -------------------------------------------------------------

def _state(**keys):
    data = json.loads(BUILTIN_STATE_FILE.read_text())
    data.update({"captured_at": NOW.isoformat(), **keys})
    return MockState(json.loads(json.dumps(data)), frozen=True)


def _records(serve):
    result = serve([_quota(used={"limits.memory": "2Gi", "pods": "4"})], [_limit_range()])
    return ([encode_model(q, NOW) for q in result.resource_quotas],
            [encode_model(lr, NOW) for lr in result.limit_ranges])


def test_replay_gives_what_live_gave_and_derives_percent(serve):
    quotas, limit_ranges = _records(serve)
    assert "percent" not in json.dumps(quotas) and "notes" not in quotas[0]
    replayed = _state(resourcequotas=quotas, limitranges=limit_ranges).get_quota_summaries("default")
    assert replayed == serve([_quota(used={"limits.memory": "2Gi", "pods": "4"})], [_limit_range()])
    assert replayed.resource_quotas[0].notes == [NOTE_QUOTA_AT_LIMIT]


def test_replay_says_which_half_isnt_recorded(serve):
    quotas, _ = _records(serve)
    data = json.loads(BUILTIN_STATE_FILE.read_text())
    del data["limitranges"]
    half = MockState(data | {"resourcequotas": quotas}, frozen=True).get_quota_summaries("default")
    assert half.limit_ranges is None and half.notes == [k8s_tools.NOTE_LIMIT_RANGES_NOT_RECORDED]
    del data["resourcequotas"]
    with pytest.raises(K8sNotRecorded, match="doesn't record ResourceQuotas"):
        MockState(data, frozen=True).get_quota_summaries("default")


# --- the namespace view ------------------------------------------------------------

def test_the_namespace_view_has_a_line_per_quota(serve):
    quotas, _ = _records(serve)
    health = namespace_health(_state(resourcequotas=quotas), "default")
    assert health.resource_quotas == [
        "ResourceQuota/otel-demo-memory (created 4m45s ago): limits.memory 2Gi/2Gi (100%), "
        "pods 4/10 (40%)"]
    assert NOTE_QUOTA_AT_LIMIT in health.notes


def test_a_hard_limit_not_in_a_binary_unit_is_shown_in_useds(serve):
    """#43, Scenario 102: 1503238553600m against 1901Mi."""
    quotas = [encode_model(q, NOW) for q in serve([
        _quota(hard={"memory": "1503238553600m", "pods": "10", "limits.memory": "2Gi"},
               used={"memory": "1901Mi", "pods": "3", "limits.memory": "1536Mi"})]).resource_quotas]
    [line] = namespace_health(_state(resourcequotas=quotas), "default").resource_quotas
    assert line == ("ResourceQuota/otel-demo-memory (created 4m45s ago): "
                    "limits.memory 1536Mi/2Gi (75%), "
                    "memory 1901Mi/1433.6Mi (132%; hard written as 1503238553600m), pods 3/10 (30%)")


def test_no_quota_no_line_and_unrecorded_quotas_are_named():
    assert namespace_health(_state(), "default").resource_quotas == []
    data = json.loads(BUILTIN_STATE_FILE.read_text())
    del data["resourcequotas"]
    health = namespace_health(MockState(data, frozen=True), "default")
    assert health.resource_quotas == []
    assert any(n.startswith("Not recorded") and "ResourceQuotas" in n for n in health.notes)


def test_a_role_without_quotas_doesnt_fail_the_namespace_view():
    state = _state()

    class Forbidden:
        def __getattr__(self, name):
            return getattr(state, name)

        def get_resourcequota_summaries(self, namespace=None):
            raise K8sForbidden("Error fetching resource quotas: 403 Forbidden")

    health = namespace_health(Forbidden(), "default")
    assert ("Not readable with these credentials (403 Forbidden), so unknown rather than "
            "absent: ResourceQuotas." in health.notes)
