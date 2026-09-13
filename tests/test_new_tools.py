"""Tests for the resource tools added in 1.1.0 (ConfigMaps, StatefulSets,
CronJobs/Jobs + latest-run logs, PVCs, cluster-wide events) and the log-tool
enhancements. The Kubernetes API is mocked with SimpleNamespace objects.
"""

import datetime
from types import SimpleNamespace

import pytest

from k8stools import k8s_tools, mock_tools

NOW = datetime.datetime.now(datetime.timezone.utc)


def _meta(name, namespace="default", days=1, labels=None, owner_refs=None):
    return SimpleNamespace(
        name=name,
        namespace=namespace,
        creation_timestamp=NOW - datetime.timedelta(days=days),
        labels=labels,
        annotations=None,
        owner_references=owner_refs,
    )


def _env(name, value=None, value_from=None):
    return SimpleNamespace(name=name, value=value, value_from=value_from)


def _container(name, image, env=None):
    return SimpleNamespace(name=name, image=image, env=env)


class MockCoreV1:
    # --- config maps ---
    def list_config_map_for_all_namespaces(self):
        return SimpleNamespace(items=self._config_maps())

    def list_namespaced_config_map(self, namespace):
        return SimpleNamespace(items=[c for c in self._config_maps() if c.metadata.namespace == namespace])

    def read_namespaced_config_map(self, name, namespace):
        for cm in self._config_maps():
            if cm.metadata.name == name and cm.metadata.namespace == namespace:
                return cm
        from kubernetes.client import ApiException
        raise ApiException(status=404, reason="Not Found")

    def _config_maps(self):
        return [
            SimpleNamespace(
                metadata=_meta("app-config", "default", days=1),
                data={"LOG_LEVEL": "info", "AWS_SECRET_ACCESS_KEY": "supersecretvalue"},
                binary_data={"cert.bin": "aGVsbG8="},
            ),
        ]

    # --- pvcs ---
    def list_persistent_volume_claim_for_all_namespaces(self):
        return SimpleNamespace(items=self._pvcs())

    def list_namespaced_persistent_volume_claim(self, namespace):
        return SimpleNamespace(items=[p for p in self._pvcs() if p.metadata.namespace == namespace])

    def _pvcs(self):
        bound = SimpleNamespace(
            metadata=_meta("data-postgres-0", "default", days=3),
            spec=SimpleNamespace(volume_name="pvc-111", access_modes=["ReadWriteOnce"],
                                 storage_class_name="standard", resources=None),
            status=SimpleNamespace(phase="Bound", capacity={"storage": "10Gi"}),
        )
        orphan = SimpleNamespace(
            metadata=_meta("orphan-xyz", "default", days=2),
            spec=SimpleNamespace(volume_name="pvc-222", access_modes=["ReadWriteOnce"],
                                 storage_class_name="standard", resources=None),
            status=SimpleNamespace(phase="Bound", capacity={"storage": "1Gi"}),
        )
        return [bound, orphan]

    # --- pods (for pvc mount resolution and latest-pod-for-selector) ---
    def list_pod_for_all_namespaces(self):
        return SimpleNamespace(items=self._pods())

    def list_namespaced_pod(self, namespace, label_selector=None):
        pods = [p for p in self._pods() if p.metadata.namespace == namespace]
        if label_selector and label_selector.startswith("job-name="):
            want = label_selector.split("=", 1)[1]
            pods = [p for p in pods if (p.metadata.labels or {}).get("job-name") == want]
        return SimpleNamespace(items=pods)

    def _pods(self):
        # postgres-0 mounts the bound PVC
        vol = SimpleNamespace(persistent_volume_claim=SimpleNamespace(claim_name="data-postgres-0"))
        postgres = SimpleNamespace(
            metadata=_meta("postgres-0", "default"),
            spec=SimpleNamespace(volumes=[vol]),
        )
        # two job pods with different creation times; newest should win
        old_pod = SimpleNamespace(
            metadata=SimpleNamespace(name="cleanup-old", namespace="default",
                                     creation_timestamp=NOW - datetime.timedelta(minutes=30),
                                     labels={"job-name": "cleanup-123"}),
            spec=SimpleNamespace(volumes=None),
        )
        new_pod = SimpleNamespace(
            metadata=SimpleNamespace(name="cleanup-new", namespace="default",
                                     creation_timestamp=NOW - datetime.timedelta(minutes=1),
                                     labels={"job-name": "cleanup-123"}),
            spec=SimpleNamespace(volumes=None),
        )
        return [postgres, old_pod, new_pod]

    def read_namespaced_pod_log(self, name, namespace, container=None, **kwargs):
        self.last_log_kwargs = dict(name=name, namespace=namespace, container=container, **kwargs)
        return f"logs for {name}"

    # --- events ---
    def list_event_for_all_namespaces(self, field_selector=None):
        self.last_event_field_selector = field_selector
        return SimpleNamespace(items=self._events())

    def list_namespaced_event(self, namespace, field_selector=None):
        self.last_event_field_selector = field_selector
        return SimpleNamespace(items=self._events())

    def _events(self):
        return [
            SimpleNamespace(
                last_timestamp=NOW - datetime.timedelta(minutes=5),
                type="Warning", reason="Evicted",
                involved_object=SimpleNamespace(kind="Pod", name="session-abc"),
                message="node low on ephemeral-storage",
            ),
        ]


class MockBatchV1:
    def list_cron_job_for_all_namespaces(self):
        return SimpleNamespace(items=self._cronjobs())

    def list_namespaced_cron_job(self, namespace):
        return SimpleNamespace(items=[c for c in self._cronjobs() if c.metadata.namespace == namespace])

    def _cronjobs(self):
        pod_spec = SimpleNamespace(containers=[_container("cleanup", "busybox:latest",
                                                          env=[_env("TIMEOUT", "30")])])
        template = SimpleNamespace(spec=pod_spec)
        job_spec = SimpleNamespace(template=template)
        job_template = SimpleNamespace(spec=job_spec)
        spec = SimpleNamespace(schedule="*/5 * * * *", suspend=False, job_template=job_template)
        status = SimpleNamespace(active=None,
                                 last_schedule_time=NOW - datetime.timedelta(minutes=2),
                                 last_successful_time=NOW - datetime.timedelta(minutes=2))
        return [SimpleNamespace(metadata=_meta("cleanup", "default"), spec=spec, status=status)]

    def list_job_for_all_namespaces(self):
        return SimpleNamespace(items=self._jobs())

    def list_namespaced_job(self, namespace):
        return SimpleNamespace(items=[j for j in self._jobs() if j.metadata.namespace == namespace])

    def _jobs(self):
        pod_spec = SimpleNamespace(containers=[_container("cleanup", "busybox:latest",
                                                          env=[_env("TIMEOUT", "30")])])
        template = SimpleNamespace(spec=pod_spec)
        spec = SimpleNamespace(template=template)
        status = SimpleNamespace(active=None, succeeded=1, failed=0,
                                 start_time=NOW - datetime.timedelta(minutes=2),
                                 completion_time=NOW - datetime.timedelta(minutes=1),
                                 conditions=[SimpleNamespace(type="Complete", status="True")])
        owner = [SimpleNamespace(kind="CronJob", name="cleanup")]
        old = SimpleNamespace(
            metadata=_meta("cleanup-123", "default", days=0, owner_refs=owner),
            spec=spec, status=status)
        old.metadata.creation_timestamp = NOW - datetime.timedelta(minutes=40)
        new = SimpleNamespace(
            metadata=_meta("cleanup-456", "default", days=0, owner_refs=owner),
            spec=spec, status=status)
        new.metadata.creation_timestamp = NOW - datetime.timedelta(minutes=2)
        return [old, new]


class MockAppsV1:
    def list_stateful_set_for_all_namespaces(self):
        return SimpleNamespace(items=self._statefulsets())

    def list_namespaced_stateful_set(self, namespace):
        return SimpleNamespace(items=[s for s in self._statefulsets() if s.metadata.namespace == namespace])

    def _statefulsets(self):
        spec = SimpleNamespace(replicas=3, service_name="postgres",
                               update_strategy=SimpleNamespace(type="RollingUpdate"))
        status = SimpleNamespace(ready_replicas=2, current_replicas=3)
        return [SimpleNamespace(metadata=_meta("postgres", "default", days=3), spec=spec, status=status)]


@pytest.fixture(autouse=True)
def mock_clients():
    orig = (k8s_tools.K8S, k8s_tools.APPS_V1_API, k8s_tools.BATCH_V1_API)
    core = MockCoreV1()
    k8s_tools.K8S = core
    k8s_tools.APPS_V1_API = MockAppsV1()
    k8s_tools.BATCH_V1_API = MockBatchV1()
    yield core
    k8s_tools.K8S, k8s_tools.APPS_V1_API, k8s_tools.BATCH_V1_API = orig


# --- ConfigMaps -------------------------------------------------------------

def test_configmap_summaries():
    cms = k8s_tools.get_configmap_summaries()
    assert len(cms) == 1
    cm = cms[0]
    assert cm.name == "app-config"
    assert cm.key_count == 3  # 2 data keys + 1 binary key
    assert cm.data_size > 0


def test_get_configmap_returns_data():
    cm = k8s_tools.get_configmap("app-config", "default")
    assert cm["name"] == "app-config"
    assert cm["data"]["LOG_LEVEL"] == "info"
    assert cm["data"]["AWS_SECRET_ACCESS_KEY"] == "supersecretvalue"  # raw (redaction is at MCP boundary)
    assert cm["binary_data_keys"] == ["cert.bin"]


def test_get_configmap_not_found():
    with pytest.raises(k8s_tools.K8sApiError):
        k8s_tools.get_configmap("nope", "default")


# --- StatefulSets -----------------------------------------------------------

def test_statefulset_summaries():
    sts = k8s_tools.get_statefulset_summaries()
    assert len(sts) == 1
    s = sts[0]
    assert s.name == "postgres"
    assert s.total_replicas == 3
    assert s.ready_replicas == 2
    assert s.current_replicas == 3
    assert s.update_strategy == "RollingUpdate"
    assert s.service_name == "postgres"


# --- CronJobs / Jobs --------------------------------------------------------

def test_cronjob_summaries():
    cjs = k8s_tools.get_cronjob_summaries()
    assert len(cjs) == 1
    cj = cjs[0]
    assert cj.schedule == "*/5 * * * *"
    assert cj.suspend is False
    assert cj.active == 0
    assert cj.last_schedule_time is not None
    assert len(cj.containers) == 1
    assert cj.containers[0].image == "busybox:latest"
    assert cj.containers[0].env == {"TIMEOUT": "30"}


def test_job_summaries():
    jobs = k8s_tools.get_job_summaries()
    assert len(jobs) == 2
    job = jobs[0]
    assert job.owner == "cleanup"
    assert job.succeeded == 1
    assert job.conditions == ["Complete"]
    assert job.containers[0].env == {"TIMEOUT": "30"}


def test_get_logs_for_job_picks_newest_pod(mock_clients):
    logs = k8s_tools.get_logs_for_job("cleanup-123", "default")
    assert logs == "logs for cleanup-new"  # newest pod by creation timestamp


def test_get_logs_for_cronjob_uses_latest_job(mock_clients):
    # latest owned job is cleanup-456; its pods have no matching job-name label here,
    # so the selector finds nothing and we get None (exercises the owner-selection path)
    logs = k8s_tools.get_logs_for_cronjob("cleanup", "default")
    assert logs is None


# --- PVCs -------------------------------------------------------------------

def test_pvc_summaries_resolves_mounts():
    pvcs = k8s_tools.get_pvc_summaries()
    by_name = {p.name: p for p in pvcs}
    assert by_name["data-postgres-0"].status == "Bound"
    assert by_name["data-postgres-0"].capacity == "10Gi"
    assert by_name["data-postgres-0"].mounted_by == ["postgres-0"]
    # the orphan PVC is mounted by nobody
    assert by_name["orphan-xyz"].mounted_by == []


# --- Events -----------------------------------------------------------------

def test_get_events_formats_object_and_builds_selector(mock_clients):
    events = k8s_tools.get_events(reason="Evicted", involved_kind="Pod")
    assert len(events) == 1
    assert events[0].object == "Pod/session-abc"
    assert events[0].reason == "Evicted"
    # field selector is assembled from the filters
    fs = mock_clients.last_event_field_selector
    assert "reason=Evicted" in fs
    assert "involvedObject.kind=Pod" in fs


# --- log enhancement plumbing ----------------------------------------------

def test_log_kwargs_plumbing(mock_clients):
    k8s_tools.get_logs_for_pod_and_container("pod-x", "default", "c", tail=50,
                                             since_seconds=120, previous=True)
    kw = mock_clients.last_log_kwargs
    assert kw["tail_lines"] == 50
    assert kw["since_seconds"] == 120
    assert kw["previous"] is True


def test_log_kwargs_defaults_omit_optional(mock_clients):
    k8s_tools.get_logs_for_pod_and_container("pod-x", "default", "c")
    kw = mock_clients.last_log_kwargs
    assert kw["tail_lines"] == 1000
    assert "since_seconds" not in kw
    assert "previous" not in kw


# --- mock_tools parity smoke tests -----------------------------------------

def test_mock_tools_new_functions():
    assert mock_tools.get_configmap_summaries()[0].name == "app-config"
    assert mock_tools.get_configmap("app-config")["data"]["AWS_ACCESS_KEY_ID"].startswith("AKIA")
    assert mock_tools.get_statefulset_summaries()[0].name == "postgres"
    assert mock_tools.get_cronjob_summaries()[0].name == "cleanup"
    assert mock_tools.get_job_summaries()[0].owner == "cleanup"
    assert mock_tools.get_pvc_summaries(namespace="default")
    evicted = mock_tools.get_events(reason="Evicted")
    assert len(evicted) == 1 and evicted[0].reason == "Evicted"
    # The capture keeps one flat event list, so a cluster-wide sweep also sees the
    # pod-scoped events that get_pod_events returns - including Normal ones.
    normal = mock_tools.get_events(event_type="Normal")
    assert [e.reason for e in normal] == ["Pulled"]
