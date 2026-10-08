"""Tests for get_hpa_summaries (issue #14): HorizontalPodAutoscalers, autoscaling/v2.

No cluster needed: fake API objects shaped like the client's V2 models.
"""

import datetime
import json
from types import SimpleNamespace as NS

import pytest

from k8stools import k8s_tools, mock_tools
from k8stools.mock_state import CAPTURE_VERSION, MockState, encode_model

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC).replace(microsecond=0)


def _ago(**kw):
    return datetime.datetime.now(UTC) - datetime.timedelta(**kw)


def _target(utilization=None, average=None, value=None, type_="Utilization"):
    return NS(type=type_, average_utilization=utilization, average_value=average, value=value)


def _hpa(name="cart", target="Deployment/cart", min_=1, max_=10, current=3, desired=3,
         metrics=(), current_metrics=(), conditions=(), last_scale=None, namespace="default"):
    kind, _, ref = target.partition("/")
    return NS(
        metadata=NS(name=name, namespace=namespace, creation_timestamp=_ago(days=8)),
        spec=NS(scale_target_ref=NS(kind=kind, name=ref), min_replicas=min_, max_replicas=max_,
                metrics=list(metrics)),
        status=NS(current_replicas=current, desired_replicas=desired,
                  current_metrics=list(current_metrics), conditions=list(conditions),
                  last_scale_time=last_scale))


def _resource(name, target=None, current=None):
    spec = NS(type="Resource", resource=NS(name=name, target=target))
    status = NS(type="Resource", resource=NS(name=name, current=current))
    return spec, status


def _condition(type_, status, reason, message="", ago=None):
    return NS(type=type_, status=status, reason=reason, message=message,
              last_transition_time=_ago(**ago) if ago else None)


class _FakeAutoscaling:
    def __init__(self, hpas):
        self.hpas, self.calls = hpas, []

    def list_horizontal_pod_autoscaler_for_all_namespaces(self):
        self.calls.append(None)
        return NS(items=self.hpas)

    def list_namespaced_horizontal_pod_autoscaler(self, namespace):
        self.calls.append(namespace)
        return NS(items=[h for h in self.hpas if h.metadata.namespace == namespace])


@pytest.fixture
def serve(monkeypatch):
    def install(*hpas):
        fake = _FakeAutoscaling(list(hpas))
        monkeypatch.setattr(k8s_tools, "AUTOSCALING_V2_API", fake)
        return fake
    return install


def test_an_hpa_at_its_maximum(serve):
    """ScalingLimited at max is often the whole answer to "why didn't it scale"."""
    cpu_spec, cpu_status = _resource("cpu", _target(80), _target(92))
    serve(_hpa(max_=3, current=3, desired=3, metrics=[cpu_spec], current_metrics=[cpu_status],
               conditions=[_condition("ScalingLimited", "True", "TooManyReplicas",
                                      "the desired replica count is more than the maximum "
                                      "replica count", ago={"minutes": 90})],
               last_scale=_ago(minutes=90)))
    [h] = k8s_tools.get_hpa_summaries()
    assert (h.scale_target, h.min_replicas, h.max_replicas, h.current_replicas) == \
        ("Deployment/cart", 1, 3, 3)
    assert [(m.type, m.name, m.target, m.current) for m in h.metrics] == \
        [("Resource", "cpu", "80%", "92%")]
    [c] = h.conditions
    assert (c.type, c.status, c.reason) == ("ScalingLimited", "True", "TooManyReplicas")
    assert abs(c.since - datetime.timedelta(minutes=90)) < datetime.timedelta(seconds=5)
    assert abs(h.last_scale - datetime.timedelta(minutes=90)) < datetime.timedelta(seconds=5)
    assert h.notes == [k8s_tools.NOTE_HPA_AT_MAX]


def test_every_metric_type_is_named_and_shown_in_its_targets_terms(serve):
    mem = NS(type="ContainerResource",
             container_resource=NS(name="memory", container="app", target=_target(average="200Mi")))
    mem_now = NS(type="ContainerResource",
                 container_resource=NS(name="memory", container="app", current=_target(average="150Mi")))
    pods = NS(type="Pods", pods=NS(metric=NS(name="queue_depth"), target=_target(average="30")))
    obj = NS(type="Object", object=NS(metric=NS(name="requests_per_second"),
                                      described_object=NS(kind="Ingress", name="main"),
                                      target=_target(value="10k")))
    ext = NS(type="External", external=NS(metric=NS(name="sqs_messages"), target=_target(value="100")))
    serve(_hpa(metrics=[mem, pods, obj, ext], current_metrics=[mem_now]))
    [h] = k8s_tools.get_hpa_summaries()
    assert [(m.type, m.name, m.target, m.current) for m in h.metrics] == [
        ("ContainerResource", "memory (container app)", "200Mi (average)", "150Mi (average)"),
        ("Pods", "queue_depth", "30 (average)", None),
        ("Object", "requests_per_second on Ingress/main", "10k", None),
        ("External", "sqs_messages", "100", None)]
    assert h.notes == []  # 3 of 10


def test_missing_status_and_min_are_safe(serve):
    hpa = _hpa(min_=None, current=None, desired=None)
    hpa.status = NS(current_replicas=None, desired_replicas=None)
    serve(hpa)
    [h] = k8s_tools.get_hpa_summaries()
    assert (h.min_replicas, h.current_replicas, h.desired_replicas) == (None, 0, 0)
    assert h.metrics == [] and h.conditions == [] and h.last_scale is None


def test_namespace_uses_the_namespaced_call_and_errors_are_wrapped(serve, monkeypatch):
    fake = serve(_hpa(), _hpa(name="other", namespace="kube-system"))
    assert [h.name for h in k8s_tools.get_hpa_summaries("kube-system")] == ["other"]
    assert fake.calls == ["kube-system"]

    def fail():
        raise k8s_tools.client.ApiException(status=403, reason="Forbidden")
    monkeypatch.setattr(k8s_tools, "AUTOSCALING_V2_API",
                        NS(list_horizontal_pod_autoscaler_for_all_namespaces=fail))
    with pytest.raises(k8s_tools.K8sApiError, match="horizontal pod autoscalers"):
        k8s_tools.get_hpa_summaries()


def test_registered_everywhere():
    assert k8s_tools.get_hpa_summaries in k8s_tools.TOOLS
    assert mock_tools.get_hpa_summaries in mock_tools.TOOLS
    assert mock_tools.get_hpa_summaries.__doc__ == k8s_tools.get_hpa_summaries.__doc__
    assert "get_hpa_summaries" in k8s_tools.TOOLSET_NAMES["investigate"]


# --- capture and replay --------------------------------------------------------------

def _capture(**keys):
    return {"version": CAPTURE_VERSION, "captured_at": NOW.isoformat(), "redacted": False, **keys}


def test_hpas_round_trip_and_their_ages_advance():
    hpa = k8s_tools.HpaSummary(
        name="cart", namespace="default", scale_target="Deployment/cart", min_replicas=1,
        max_replicas=3, current_replicas=3, desired_replicas=3,
        metrics=[k8s_tools.HpaMetric(type="Resource", name="cpu", target="80%", current="92%")],
        conditions=[k8s_tools.HpaCondition(type="ScalingLimited", status="True",
                                           reason="TooManyReplicas",
                                           since=datetime.timedelta(minutes=90))],
        last_scale=datetime.timedelta(minutes=90), age=datetime.timedelta(days=8))
    record = encode_model(hpa, NOW)
    assert "notes" not in record
    later = datetime.datetime.now(UTC) - datetime.timedelta(hours=1)
    state = MockState(json.loads(json.dumps(_capture(hpas=[record]))), frozen=False,
                      server_start_time=later)
    [back] = state.get_hpa_summaries("default")
    assert back.last_scale == datetime.timedelta(minutes=150)
    assert back.conditions[0].since == datetime.timedelta(minutes=150)
    assert back.metrics == hpa.metrics and back.notes == hpa.notes


def test_a_capture_without_hpas_replays_none():
    assert MockState(_capture(), frozen=True).get_hpa_summaries() == []


# --- in the composites ------------------------------------------------------------------

def test_the_composites_show_the_autoscaler():
    health = mock_tools.get_namespace_health("default")
    assert "Deployment/test-deployment 3/3, HPA at max 3" in health.healthy
    report = mock_tools.get_workload_report("test-deployment")
    assert report.autoscaler.scale_target == "Deployment/test-deployment"
    assert report.autoscaler.notes == [k8s_tools.NOTE_HPA_AT_MAX]


def test_an_unhealthy_workloads_autoscaler_line():
    from k8stools.composites import _autoscaler_line
    hpa = k8s_tools.HpaSummary(
        name="cart", namespace="default", scale_target="Deployment/cart", min_replicas=None,
        max_replicas=5, current_replicas=2, desired_replicas=2,
        metrics=[k8s_tools.HpaMetric(type="Resource", name="cpu", target="80%")],
        conditions=[k8s_tools.HpaCondition(type="ScalingActive", status="False",
                                           reason="FailedGetResourceMetric")],
        age=datetime.timedelta(days=1))
    assert _autoscaler_line(hpa) == ("HPA cart: 2 replicas (min 1, max 5); cpu unknown/80%, "
                                     "not scaling: FailedGetResourceMetric")


def test_against_the_clients_own_v2_models(serve):
    """The fakes above are shaped from memory of the client's models; this
    builds the real ones, so a renamed attribute fails here, not on a cluster."""
    from kubernetes import client as c
    hpa = c.V2HorizontalPodAutoscaler(
        metadata=c.V1ObjectMeta(name="cart", namespace="default", creation_timestamp=_ago(days=1)),
        spec=c.V2HorizontalPodAutoscalerSpec(
            scale_target_ref=c.V2CrossVersionObjectReference(kind="Deployment", name="cart",
                                                             api_version="apps/v1"),
            min_replicas=2, max_replicas=4,
            metrics=[
                c.V2MetricSpec(type="Resource", resource=c.V2ResourceMetricSource(
                    name="cpu", target=c.V2MetricTarget(type="Utilization", average_utilization=80))),
                c.V2MetricSpec(type="ContainerResource",
                               container_resource=c.V2ContainerResourceMetricSource(
                                   name="memory", container="app",
                                   target=c.V2MetricTarget(type="AverageValue", average_value="200Mi"))),
                c.V2MetricSpec(type="Object", object=c.V2ObjectMetricSource(
                    metric=c.V2MetricIdentifier(name="rps"),
                    described_object=c.V2CrossVersionObjectReference(kind="Ingress", name="main"),
                    target=c.V2MetricTarget(type="Value", value="10k"))),
                c.V2MetricSpec(type="Pods", pods=c.V2PodsMetricSource(
                    metric=c.V2MetricIdentifier(name="queue_depth"),
                    target=c.V2MetricTarget(type="AverageValue", average_value="30"))),
                c.V2MetricSpec(type="External", external=c.V2ExternalMetricSource(
                    metric=c.V2MetricIdentifier(name="sqs"),
                    target=c.V2MetricTarget(type="Value", value="100")))]),
        status=c.V2HorizontalPodAutoscalerStatus(
            current_replicas=4, desired_replicas=4, last_scale_time=_ago(minutes=30),
            current_metrics=[c.V2MetricStatus(type="Resource", resource=c.V2ResourceMetricStatus(
                name="cpu", current=c.V2MetricValueStatus(average_utilization=95, average_value="190m")))],
            conditions=[c.V2HorizontalPodAutoscalerCondition(
                type="ScalingLimited", status="True", reason="TooManyReplicas",
                last_transition_time=_ago(minutes=30))]))
    serve(hpa)
    [h] = k8s_tools.get_hpa_summaries()
    assert [(m.name, m.target, m.current) for m in h.metrics] == [
        ("cpu", "80%", "95%"), ("memory (container app)", "200Mi (average)", None),
        ("rps on Ingress/main", "10k", None), ("queue_depth", "30 (average)", None),
        ("sqs", "100", None)]
    assert h.scale_target == "Deployment/cart" and h.notes == [k8s_tools.NOTE_HPA_AT_MAX]
