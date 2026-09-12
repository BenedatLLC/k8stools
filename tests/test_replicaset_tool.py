"""Tests for get_replicaset_summaries.

A Deployment's replica sets are its revision history, so this tool exists to
answer "what changed, and when" without access to deployment tooling or version
control. The Kubernetes API is mocked with SimpleNamespace objects, following
tests/test_new_tools.py.
"""

import datetime
from types import SimpleNamespace

import pytest

from k8stools import k8s_tools, mock_tools

NOW = datetime.datetime.now(datetime.timezone.utc)


def _rs(name, *, namespace="default", owner="ad", revision=1, days=1,
        desired=1, current=1, ready=1, images=("demo:1",), bad_revision=False):
    owner_refs = [SimpleNamespace(kind="Deployment", name=owner)] if owner else []
    annotations = None
    if revision is not None or bad_revision:
        annotations = {"deployment.kubernetes.io/revision":
                       "not-a-number" if bad_revision else str(revision)}
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name, namespace=namespace,
            creation_timestamp=NOW - datetime.timedelta(days=days),
            annotations=annotations, owner_references=owner_refs,
        ),
        spec=SimpleNamespace(
            replicas=desired,
            template=SimpleNamespace(
                spec=SimpleNamespace(
                    containers=[SimpleNamespace(name="c", image=i) for i in images])),
        ),
        status=SimpleNamespace(replicas=current, ready_replicas=ready),
    )


class MockAppsV1:
    def __init__(self, items):
        self._items = items

    def list_replica_set_for_all_namespaces(self):
        return SimpleNamespace(items=self._items)

    def list_namespaced_replica_set(self, namespace):
        return SimpleNamespace(items=[r for r in self._items
                                      if r.metadata.namespace == namespace])


@pytest.fixture
def apps(monkeypatch):
    def install(items):
        monkeypatch.setattr(k8s_tools, "APPS_V1_API", MockAppsV1(items))
        return items
    return install


class TestBasics:
    def test_summarises_a_replica_set(self, apps):
        apps([_rs("ad-abc", revision=2, images=("demo:2.2.0-ad",))])
        rs = k8s_tools.get_replicaset_summaries()[0]
        assert rs.name == "ad-abc"
        assert rs.owner_deployment == "ad"
        assert rs.revision == 2
        assert rs.images == ["demo:2.2.0-ad"]

    def test_filters_by_namespace(self, apps):
        apps([_rs("a", namespace="default"), _rs("b", namespace="other")])
        assert [r.name for r in k8s_tools.get_replicaset_summaries(namespace="other")] == ["b"]

    def test_filters_by_deployment(self, apps):
        # The common case: one deployment's revision history.
        apps([_rs("ad-1", owner="ad"), _rs("cart-1", owner="cart")])
        out = k8s_tools.get_replicaset_summaries(deployment="cart")
        assert [r.name for r in out] == ["cart-1"]


class TestRevisionHistory:
    def test_sorted_oldest_first_so_the_last_is_current(self, apps):
        # Callers read the history in order; the API does not guarantee one.
        apps([_rs("ad-2", revision=2, days=1), _rs("ad-1", revision=1, days=30)])
        out = k8s_tools.get_replicaset_summaries(deployment="ad")
        assert [r.revision for r in out] == [1, 2]

    def test_age_of_the_newest_is_when_the_deployment_last_changed(self, apps):
        apps([_rs("ad-1", revision=1, days=200), _rs("ad-2", revision=2, days=145)])
        newest = k8s_tools.get_replicaset_summaries(deployment="ad")[-1]
        assert 144 <= newest.age.days <= 146

    def test_old_revisions_are_scaled_to_zero_and_still_listed(self, apps):
        # The scaled-down revision is the history; dropping it loses the "before".
        apps([_rs("ad-1", revision=1, desired=0, current=0, ready=0, images=("demo:2.0.2-ad",)),
              _rs("ad-2", revision=2, images=("demo:2.2.0-ad",))])
        out = k8s_tools.get_replicaset_summaries(deployment="ad")
        assert out[0].desired_replicas == 0
        assert out[0].images == ["demo:2.0.2-ad"]

    def test_image_change_is_visible_across_revisions(self, apps):
        apps([_rs("ad-1", revision=1, images=("demo:2.0.2-ad",)),
              _rs("ad-2", revision=2, images=("demo:2.2.0-ad",))])
        images = [r.images[0] for r in k8s_tools.get_replicaset_summaries(deployment="ad")]
        assert images == ["demo:2.0.2-ad", "demo:2.2.0-ad"]


class TestEdgeCases:
    def test_standalone_replica_set_has_no_owner(self, apps):
        apps([_rs("solo", owner=None)])
        assert k8s_tools.get_replicaset_summaries()[0].owner_deployment is None

    def test_deployment_filter_excludes_standalone_replica_sets(self, apps):
        apps([_rs("solo", owner=None), _rs("ad-1", owner="ad")])
        assert [r.name for r in k8s_tools.get_replicaset_summaries(deployment="ad")] == ["ad-1"]

    def test_missing_revision_annotation_is_none_not_an_error(self, apps):
        apps([_rs("ad-1", revision=None)])
        assert k8s_tools.get_replicaset_summaries()[0].revision is None

    def test_malformed_revision_does_not_lose_the_replica_set(self, apps):
        # A bad annotation should degrade the field, not drop the history entry.
        apps([_rs("ad-1", bad_revision=True)])
        out = k8s_tools.get_replicaset_summaries()
        assert len(out) == 1 and out[0].revision is None

    def test_absent_status_counts_as_zero(self, apps):
        item = _rs("ad-1")
        item.status = SimpleNamespace(replicas=None, ready_replicas=None)
        apps([item])
        out = k8s_tools.get_replicaset_summaries()[0]
        assert out.current_replicas == 0 and out.ready_replicas == 0

    def test_multiple_containers_are_all_reported(self, apps):
        apps([_rs("multi", images=("app:1", "sidecar:2"))])
        assert k8s_tools.get_replicaset_summaries()[0].images == ["app:1", "sidecar:2"]

    def test_api_error_is_wrapped(self, apps, monkeypatch):
        class Failing:
            def list_replica_set_for_all_namespaces(self):
                from kubernetes import client
                raise client.ApiException(status=403, reason="Forbidden")

        monkeypatch.setattr(k8s_tools, "APPS_V1_API", Failing())
        with pytest.raises(k8s_tools.K8sApiError, match="replica sets"):
            k8s_tools.get_replicaset_summaries()


class TestMock:
    def test_mock_is_registered_and_filters(self):
        out = mock_tools.get_replicaset_summaries(namespace="default", deployment="ad")
        assert [r.revision for r in out] == [1, 2]

    def test_mock_shows_an_upgrade(self):
        out = mock_tools.get_replicaset_summaries(deployment="ad")
        assert out[0].images != out[1].images

    def test_registered_in_both_tool_lists(self):
        for tools in (k8s_tools.TOOLS, mock_tools.TOOLS):
            assert any(t.__name__ == "get_replicaset_summaries" for t in tools)


class TestMockConsistency:
    """The mock data is a single pretend cluster, so the replica sets have to
    agree with the deployments and pods an agent sees from the other tools.
    An agent reasoning over contradictory mock data is being tested against a
    cluster that could not exist.
    """

    def test_every_replica_set_is_owned_by_a_mock_deployment(self):
        deployments = {(d.namespace, d.name)
                       for d in mock_tools.get_deployment_summaries()}
        for rs in mock_tools.get_replicaset_summaries():
            assert (rs.namespace, rs.owner_deployment) in deployments, \
                f"{rs.name} is owned by a deployment the mock does not have"

    def test_no_replica_set_is_older_than_its_deployment(self):
        # A replica set is created by its deployment, so it cannot predate it.
        deployments = {(d.namespace, d.name): d
                       for d in mock_tools.get_deployment_summaries()}
        for rs in mock_tools.get_replicaset_summaries():
            owner = deployments[(rs.namespace, rs.owner_deployment)]
            assert rs.age <= owner.age, \
                f"{rs.name} ({rs.age}) is older than deployment {owner.name} ({owner.age})"

    def test_ages_are_fixed_not_computed_from_a_hardcoded_date(self):
        # Ages derived from a fixed calendar date grow without bound as the
        # repo ages; every other mock resource uses a fixed timedelta.
        for rs in mock_tools.get_replicaset_summaries():
            assert rs.age < datetime.timedelta(days=365), \
                f"{rs.name} age {rs.age} looks computed from a hardcoded date"

    def test_running_pods_belong_to_a_listed_replica_set(self):
        # A pod's name is "<replicaset name>-<suffix>", so the replica set a
        # running pod belongs to should appear in the replica set list.
        names = {(rs.namespace, rs.name)
                 for rs in mock_tools.get_replicaset_summaries()}
        for pod in mock_tools.get_pod_summaries():
            prefix = pod.name.rsplit("-", 1)[0]
            if (pod.namespace, prefix) in names:
                return
        pytest.fail("no mock pod maps onto a mock replica set")

    def test_current_revision_agrees_with_its_deployment(self):
        # The newest replica set is the one actually running, so its counts
        # should match what get_deployment_summaries reports.
        for deployment in mock_tools.get_deployment_summaries():
            owned = mock_tools.get_replicaset_summaries(
                namespace=deployment.namespace, deployment=deployment.name)
            if not owned:
                continue
            current = owned[-1]
            assert current.desired_replicas == deployment.total_replicas
            assert current.ready_replicas == deployment.ready_replicas
