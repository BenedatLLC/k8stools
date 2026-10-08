"""Tests for get_workload_history (issue #11): revisions, template diffs, config refs.

No cluster needed: fake API objects, with pod templates as the API's JSON dicts
(what `_api_dict` produces from a real client object).
"""

import datetime
import json
from types import SimpleNamespace

import pytest

from k8stools import k8s_tools, mock_tools
from k8stools.k8s_tools import _template_changes
from k8stools.mock_state import CAPTURE_VERSION, MockState, encode_model
from k8stools.redaction import redact_object

UTC = datetime.timezone.utc
NOW = datetime.datetime.now(UTC).replace(microsecond=0)


def _ago(**kw):
    return datetime.datetime.now(UTC) - datetime.timedelta(**kw)


def _template(image="demo:2.0.2-ad", env=None, limits=None, labels=None,
              annotations=None, **spec):
    container = {"name": "ad", "image": image,
                 "resources": {"limits": limits or {"memory": "300Mi"}},
                 "env": env if env is not None else [
                     {"name": "LOG_LEVEL", "value": "info"},
                     {"name": "DB_PASSWORD", "value": "hunter2"}]}
    container.update(spec.pop("container", {}))
    return {"metadata": {"labels": {"app": "ad", "pod-template-hash": "abc",
                                    **(labels or {})},
                         "annotations": annotations or {}},
            "spec": {"containers": [container], **spec}}


def _changes(before, after):
    changes, metadata = _template_changes(before, after)
    return {(c.field, c.change): (c.before, c.after) for c in changes + metadata}


# --- comparing templates -------------------------------------------------------

def test_image_and_limit_changes_show_both_values():
    got = _changes(_template(), _template(image="demo:2.2.0-ad", limits={"memory": "512Mi"}))
    assert got[("containers[ad].image", "changed")] == ("demo:2.0.2-ad", "demo:2.2.0-ad")
    assert got[("containers[ad].resources.limits[memory]", "changed")] == ("300Mi", "512Mi")


def test_env_is_compared_by_name_and_never_shows_values():
    """Removing one variable shifts every later index; matched by position, all
    of them would read as changed."""
    before = _template(env=[{"name": "A", "value": "1"}, {"name": "GONE", "value": "x"},
                            {"name": "DB_PASSWORD", "value": "hunter2"}])
    after = _template(env=[{"name": "A", "value": "1"},
                           {"name": "DB_PASSWORD", "value": "s3cret!"},
                           {"name": "NEW", "valueFrom": {"configMapKeyRef": {"name": "c", "key": "k"}}}])
    got = _changes(before, after)
    assert got == {("containers[ad].env[GONE]", "removed"): (None, None),
                   ("containers[ad].env[DB_PASSWORD]", "changed"): (None, None),
                   ("containers[ad].env[NEW]", "added"): (None, None)}


def test_pod_template_hash_is_not_a_change_and_metadata_is_kept_apart():
    changes, metadata = _template_changes(
        _template(), _template(labels={"pod-template-hash": "def",
                                       "app.kubernetes.io/version": "2.2.0"}))
    assert changes == []
    assert [(c.field, c.after) for c in metadata] == \
        [("metadata.labels[app.kubernetes.io/version]", "2.2.0")]


def test_command_args_probes_mounts_volumes_selector_tolerations():
    probe = {"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 10}
    before = _template(volumes=[{"name": "cfg", "configMap": {"name": "ad-config"}}],
                       nodeSelector={"disktype": "ssd"},
                       container={"args": ["--port=8080"], "livenessProbe": probe,
                                  "volumeMounts": [{"name": "cfg", "mountPath": "/etc/ad"}]})
    after = _template(volumes=[{"name": "cfg", "configMap": {"name": "ad-config-v2"}}],
                      tolerations=[{"key": "dedicated", "operator": "Equal",
                                    "value": "ad", "effect": "NoSchedule"}],
                      container={"args": ["--port=9555"],
                                 "livenessProbe": {**probe, "periodSeconds": 5},
                                 "volumeMounts": [{"name": "cfg", "mountPath": "/etc/ad",
                                                   "subPath": "ad.yaml"}]})
    got = _changes(before, after)
    assert got[("containers[ad].args", "changed")] == ('["--port=8080"]', '["--port=9555"]')
    assert ("containers[ad].livenessProbe", "changed") in got
    assert got[("containers[ad].volumeMounts[/etc/ad]", "changed")] == \
        ("cfg", "cfg subPath=ad.yaml")
    assert got[("volumes[cfg]", "changed")] == ("configMap:ad-config", "configMap:ad-config-v2")
    assert got[("node_selector[disktype]", "removed")] == ("ssd", None)
    assert got[("tolerations", "added")] == (None, "dedicated=ad:NoSchedule")


def test_containers_are_matched_by_name():
    sidecar = {"name": "proxy", "image": "envoy:1.30"}
    before = _template()
    after = _template()
    after["spec"]["containers"].insert(0, sidecar)
    assert _changes(before, after) == {("containers[proxy]", "added"): (None, "envoy:1.30")}


def test_other_fields_are_named_with_values_only_for_scalars():
    got = _changes(_template(serviceAccountName="ad", container={"securityContext": {"runAsUser": 1}}),
                   _template(serviceAccountName="ad-v2", container={"securityContext": {"runAsUser": 2}}))
    assert got[("spec.serviceAccountName", "changed")] == ("ad", "ad-v2")
    assert got[("containers[ad].securityContext", "changed")] == (None, None)


def test_init_containers_are_compared():
    before = _template(initContainers=[{"name": "migrate", "image": "flyway:9"}])
    after = _template(initContainers=[{"name": "migrate", "image": "flyway:10"}])
    assert _changes(before, after) == {
        ("init_containers[migrate].image", "changed"): ("flyway:9", "flyway:10")}


# --- the tool, with fake API objects ---------------------------------------------

def _meta(name, created, owner=None, annotations=None):
    refs = [SimpleNamespace(kind=owner[0], name=owner[1], controller=True)] if owner else None
    return SimpleNamespace(name=name, namespace="default", creation_timestamp=created,
                           owner_references=refs, annotations=annotations or {},
                           managed_fields=None)


def _rs(name, revision, template, created, owner="ad", history=None):
    annotations = {"deployment.kubernetes.io/revision": str(revision)}
    if history:
        annotations["deployment.kubernetes.io/revision-history"] = history
    return SimpleNamespace(metadata=_meta(name, created, ("Deployment", owner), annotations),
                           spec=SimpleNamespace(template=template))


def _cr(name, revision, template, created, owner):
    return SimpleNamespace(metadata=_meta(name, created, owner), revision=revision,
                           data={"spec": {"template": {"$patch": "replace", **template}}})


class _Fake:
    def __init__(self, workloads=None, replicasets=(), revisions=(), configmaps=None):
        self.workloads = workloads or {}
        self.replicasets = list(replicasets)
        self.revisions = list(revisions)
        self.configmaps = configmaps or {}

    def _read(self, kind, name, namespace):
        if (kind, name) not in self.workloads:
            raise k8s_tools.client.ApiException(status=404, reason="Not Found")
        return self.workloads[(kind, name)]

    def read_namespaced_deployment(self, name, namespace):
        return self._read("Deployment", name, namespace)

    def read_namespaced_stateful_set(self, name, namespace):
        return self._read("StatefulSet", name, namespace)

    def read_namespaced_daemon_set(self, name, namespace):
        return self._read("DaemonSet", name, namespace)

    def list_namespaced_replica_set(self, namespace):
        return SimpleNamespace(items=self.replicasets)

    def list_namespaced_controller_revision(self, namespace):
        return SimpleNamespace(items=self.revisions)

    def read_namespaced_config_map(self, name, namespace):
        if name not in self.configmaps:
            raise k8s_tools.client.ApiException(status=404, reason="Not Found")
        return self.configmaps[name]

    def read_namespaced_secret(self, *a, **kw):
        pytest.fail("get_workload_history must never read a Secret")


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        f = _Fake(**kw)
        monkeypatch.setattr(k8s_tools, "APPS_V1_API", f)
        monkeypatch.setattr(k8s_tools, "K8S", f)
        return f
    return install


def _workload(template, history_limit=None):
    return SimpleNamespace(spec=SimpleNamespace(template=template,
                                                revision_history_limit=history_limit))


def test_deployment_revisions_newest_first_each_compared_with_the_last(fake):
    v1, v2 = _template(), _template(image="demo:2.2.0-ad")
    v3 = _template(image="demo:2.2.0-ad",
                   annotations={"kubectl.kubernetes.io/restartedAt": "2026-10-04T10:00:00Z"})
    fake(workloads={("Deployment", "ad"): _workload(v3, history_limit=3)},
         replicasets=[_rs("ad-3", 3, v3, _ago(hours=1)), _rs("ad-1", 1, v1, _ago(days=8)),
                      _rs("ad-2", 2, v2, _ago(hours=7)),
                      _rs("cart-1", 1, v1, _ago(days=8), owner="cart")])
    h = k8s_tools.get_workload_history("ad")
    assert [(r.revision, r.source, r.current, r.compared_with) for r in h.revisions] == [
        (3, "ReplicaSet/ad-3", True, 2), (2, "ReplicaSet/ad-2", False, 1),
        (1, "ReplicaSet/ad-1", False, None)]
    newest, upgrade, oldest = h.revisions
    assert newest.rollout_restart and newest.changes == []
    assert [c.field for c in upgrade.changes] == ["containers[ad].image"]
    assert not upgrade.rollout_restart and oldest.changes == []
    assert abs(upgrade.age - datetime.timedelta(hours=7)) < datetime.timedelta(seconds=5)
    assert h.complete and any("keeps 3 old revisions" in line for line in h.limits)


def test_a_reused_replica_set_is_flagged(fake):
    """kubectl rollout undo re-activates the old ReplicaSet under a new number,
    so its age is when it was first created, not when it went live."""
    v1, v2 = _template(), _template(image="demo:2.2.0-ad")
    fake(workloads={("Deployment", "ad"): _workload(v1)},
         replicasets=[_rs("ad-1", 3, v1, _ago(days=8), history="1"),
                      _rs("ad-2", 2, v2, _ago(hours=7))])
    newest = k8s_tools.get_workload_history("ad").revisions[0]
    assert newest.reused and newest.current and newest.revision == 3
    assert [(c.before, c.after) for c in newest.changes] == [("demo:2.2.0-ad", "demo:2.0.2-ad")]


def test_statefulsets_and_daemonsets_use_controller_revisions(fake):
    v1, v2 = _template(image="opensearch:2.19.0"), _template(image="opensearch:3.4.0")
    fake(workloads={("StatefulSet", "opensearch"): _workload(v2),
                    ("DaemonSet", "agent"): _workload(v1)},
         revisions=[_cr("opensearch-b", 2, v2, _ago(days=1), ("StatefulSet", "opensearch")),
                    _cr("opensearch-a", 1, v1, _ago(days=2), ("StatefulSet", "opensearch")),
                    _cr("agent-a", 1, v1, _ago(days=2), ("DaemonSet", "agent"))])
    sts = k8s_tools.get_workload_history("opensearch", kind="StatefulSet")
    assert [r.source for r in sts.revisions] == ["ControllerRevision/opensearch-b",
                                                 "ControllerRevision/opensearch-a"]
    assert [(c.field, c.after) for c in sts.revisions[0].changes] == \
        [("containers[ad].image", "opensearch:3.4.0")]
    ds = k8s_tools.get_workload_history("agent", kind="DaemonSet")
    assert len(ds.revisions) == 1 and ds.revisions[0].current


def test_config_references_and_configmap_write_times(fake):
    template = _template(
        env=[{"name": "LEVEL", "valueFrom": {"configMapKeyRef": {"name": "flags", "key": "l"}}},
             {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db", "key": "p"}}}],
        volumes=[{"name": "cfg", "configMap": {"name": "ad-config"}},
                 {"name": "certs", "secret": {"secretName": "tls"}},
                 {"name": "proj", "projected": {"sources": [{"configMap": {"name": "extra"}}]}}],
        imagePullSecrets=[{"name": "regcred"}],
        container={"envFrom": [{"configMapRef": {"name": "flags"}}],
                   "volumeMounts": [{"name": "cfg", "mountPath": "/etc/ad/ad.yaml",
                                     "subPath": "ad.yaml"}]})
    written = _ago(hours=3)
    cm = SimpleNamespace(metadata=SimpleNamespace(
        creation_timestamp=_ago(days=8),
        managed_fields=[SimpleNamespace(time=_ago(days=8)), SimpleNamespace(time=written)]))
    fake(workloads={("Deployment", "ad"): _workload(template)},
         replicasets=[_rs("ad-1", 1, template, _ago(days=8))],
         configmaps={"flags": cm, "ad-config": cm})
    refs = {(r.kind, r.name): r for r in k8s_tools.get_workload_history("ad").config}
    assert refs[("ConfigMap", "flags")].used_as == ["env", "envFrom"]
    assert refs[("ConfigMap", "ad-config")].used_as == ["volume (subPath)"]
    assert refs[("ConfigMap", "extra")].used_as == ["volume"]
    assert refs[("ConfigMap", "extra")].exists is False
    flags = refs[("ConfigMap", "flags")]
    assert flags.exists and abs(flags.last_written - datetime.timedelta(hours=3)) < \
        datetime.timedelta(seconds=5)
    for secret in ("db", "tls", "regcred"):
        ref = refs[("Secret", secret)]
        assert (ref.exists, ref.age, ref.last_written) == (None, None, None)
    assert refs[("Secret", "regcred")].used_as == ["imagePullSecrets"]


def test_env_values_never_appear_in_the_result(fake):
    v1 = _template()
    v2 = _template(env=[{"name": "LOG_LEVEL", "value": "debug"},
                        {"name": "DB_PASSWORD", "value": "s3cret!"}])
    fake(workloads={("Deployment", "ad"): _workload(v2)},
         replicasets=[_rs("ad-1", 1, v1, _ago(days=8)), _rs("ad-2", 2, v2, _ago(hours=1))])
    dumped = k8s_tools.get_workload_history("ad").model_dump_json()
    for value in ("hunter2", "s3cret!", '"info"', '"debug"'):
        assert value not in dumped
    assert "env[LOG_LEVEL]" in dumped and "env[DB_PASSWORD]" in dumped


def test_redaction_leaves_env_names_readable(fake):
    """The output must not put env names where redaction treats them as keys,
    or every change to a *_PASSWORD variable would read [REDACTED]."""
    v1 = _template()
    v2 = _template(env=[{"name": "DB_PASSWORD", "value": "other"}])
    fake(workloads={("Deployment", "ad"): _workload(v2)},
         replicasets=[_rs("ad-1", 1, v1, _ago(days=8)), _rs("ad-2", 2, v2, _ago(hours=1))])
    redacted, count = redact_object(k8s_tools.get_workload_history("ad"))
    assert count == 0
    assert {c.field for c in redacted.revisions[0].changes} == \
        {"containers[ad].env[LOG_LEVEL]", "containers[ad].env[DB_PASSWORD]"}


def test_unknown_workload_and_kind_raise(fake):
    fake()
    with pytest.raises(k8s_tools.K8sApiError, match="Deployment 'nope' not found"):
        k8s_tools.get_workload_history("nope")
    with pytest.raises(k8s_tools.K8sApiError, match="Unsupported kind"):
        k8s_tools.get_workload_history("ad", kind="Job")


def test_registered_with_shared_docstring():
    assert k8s_tools.get_workload_history in k8s_tools.TOOLS
    assert mock_tools.get_workload_history in mock_tools.TOOLS
    assert mock_tools.get_workload_history.__doc__ == k8s_tools.get_workload_history.__doc__


def test_the_result_states_what_history_cannot_show(fake):
    """Since #20 these travel in the result's limits, not the description."""
    v1 = _template()
    fake(workloads={("Deployment", "ad"): _workload(v1)},
         replicasets=[_rs("ad-1", 1, v1, _ago(days=8))])
    limits = " ".join(k8s_tools.get_workload_history("ad").limits)
    assert "subPath" in limits and "started_at" in limits
    assert "Secret contents and change times are not read" in limits
    assert "reused" in limits and "label-only" in limits


# --- capture and replay --------------------------------------------------------------

def _capture(**keys):
    return {"version": CAPTURE_VERSION, "captured_at": NOW.isoformat(),
            "redacted": False, **keys}


def _history():
    return k8s_tools.WorkloadHistory(
        kind="Deployment", name="ad", namespace="default",
        revisions=[k8s_tools.WorkloadRevision(
            revision=2, source="ReplicaSet/ad-2", age=datetime.timedelta(hours=7),
            current=True, images=["demo:2.2.0-ad"], compared_with=1,
            changes=[k8s_tools.TemplateChange(field="containers[ad].image", change="changed",
                                              before="demo:2.0.2-ad", after="demo:2.2.0-ad")])],
        config=[k8s_tools.ConfigReference(kind="ConfigMap", name="flags", used_as=["envFrom"],
                                          exists=True, age=datetime.timedelta(days=8),
                                          last_written=datetime.timedelta(hours=3))],
        limits=["..."])


def test_history_round_trips_and_its_ages_advance():
    record = encode_model(_history(), NOW)
    later = datetime.datetime.now(UTC) - datetime.timedelta(hours=1)
    state = MockState(json.loads(json.dumps(_capture(workload_histories=[record]))),
                      frozen=False, server_start_time=later)
    h = state.get_workload_history("ad")
    assert h.revisions[0].age == datetime.timedelta(hours=8)
    assert h.config[0].last_written == datetime.timedelta(hours=4)
    assert h.revisions[0].changes == _history().revisions[0].changes


def _rs_record(name, revision, image, hours):
    return {"name": name, "namespace": "default", "owner_deployment": "ad",
            "revision": revision, "desired_replicas": 1, "current_replicas": 1,
            "ready_replicas": 1, "images": [image], "age_seconds": hours * 3600.0}


def test_a_capture_from_before_2_4_0_falls_back_to_images_only():
    state = MockState(_capture(
        deployments=[{"name": "ad", "namespace": "default", "total_replicas": 1,
                      "ready_replicas": 1, "up_to_date_relicas": 1,
                      "available_replicas": 1, "age_seconds": 691200.0}],
        statefulsets=[{"name": "pg", "namespace": "default", "total_replicas": 1,
                       "ready_replicas": 1, "current_replicas": 1,
                       "update_strategy": "RollingUpdate", "age_seconds": 60.0}],
        replicasets=[_rs_record("ad-2", 2, "demo:2.2.0-ad", 7),
                     _rs_record("ad-1", 1, "demo:2.0.2-ad", 192)]), frozen=True)
    h = state.get_workload_history("ad")
    assert not h.complete and "predates workload history" in h.limits[-1]
    assert [(r.revision, r.current) for r in h.revisions] == [(2, True), (1, False)]
    assert [(c.field, c.before, c.after) for c in h.revisions[0].changes] == \
        [("images", "demo:2.0.2-ad", "demo:2.2.0-ad")]
    sts = state.get_workload_history("pg", kind="StatefulSet")
    assert not sts.complete and sts.revisions == []
    with pytest.raises(k8s_tools.K8sApiError, match="not found"):
        state.get_workload_history("nope")
