"""This module provides mocks for the tool functions. For each tool in k8s_tools.TOOLS, it provides
an equivalent mock that has the same function signature and returns mock data of the same type.
This is useful in writing tests for clients of this package (e.g. your agent).

The data comes from a *capture* - a JSON snapshot of a real cluster - served by
:class:`k8stools.mock_state.MockState`. By default that is the built-in OTel Demo
on Minikube snapshot; call :func:`load_mock_state` with a path to replay a capture
of your own, taken with ``k8s-capture-state``.

Because the state is a snapshot of one cluster rather than a set of independently
synthesized answers, the tools agree with each other: a pod that is not in the
capture does not exist for *any* tool, and a pod that is has consistent statuses,
events, spec and logs.
"""
from pathlib import Path
from typing import Optional, Any

from . import k8s_tools
from .mock_state import MockState

_STATE: Optional[MockState] = None


def load_mock_state(path: Optional[Path] = None, frozen: bool = False) -> MockState:
    """Load the state the mock tools serve, replacing whatever was loaded before.

    Parameters
    ----------
    path
        A capture file written by ``k8s-capture-state``. If None, loads the
        built-in OTel Demo snapshot.
    frozen
        Freeze the replay clock, so every age is the one recorded at capture time
        and repeated runs are identical. Off by default, because an advancing
        clock is what makes an interactive session feel like a cluster; on is what
        an automated suite wants. See :class:`k8stools.mock_state.MockState`.
    """
    global _STATE
    _STATE = MockState.from_file(path, frozen=frozen) if path \
        else MockState.from_builtin(frozen=frozen)
    return _STATE


def _state() -> MockState:
    """The loaded state, loading the built-in capture on first use.

    Lazy so that importing this module and calling a tool works with no setup, as
    it did when the data was hardcoded.
    """
    global _STATE
    if _STATE is None:
        _STATE = MockState.from_builtin()
    return _STATE


def get_namespaces() -> list[k8s_tools.NamespaceSummary]:
    return _state().get_namespaces()

get_namespaces.__doc__ = k8s_tools.get_namespaces.__doc__


def get_node_summaries() -> list[k8s_tools.NodeSummary]:
    return _state().get_node_summaries()

get_node_summaries.__doc__ = k8s_tools.get_node_summaries.__doc__


def get_pod_summaries(namespace: Optional[str] = None) -> list[k8s_tools.PodSummary]:
    return _state().get_pod_summaries(namespace)

get_pod_summaries.__doc__ = k8s_tools.get_pod_summaries.__doc__


def get_pod_container_statuses(pod_name: str, namespace: str = "default") -> list[k8s_tools.ContainerStatus]:
    return _state().get_pod_container_statuses(pod_name, namespace)

get_pod_container_statuses.__doc__ = k8s_tools.get_pod_container_statuses.__doc__


def get_pod_events(pod_name: str, namespace: str = "default") -> list[k8s_tools.EventSummary]:
    return _state().get_pod_events(pod_name, namespace)

get_pod_events.__doc__ = k8s_tools.get_pod_events.__doc__


def get_pod_spec(pod_name: str, namespace: str = "default") -> dict[str, Any]:
    return _state().get_pod_spec(pod_name, namespace)

get_pod_spec.__doc__ = k8s_tools.get_pod_spec.__doc__


def get_logs_for_pod_and_container(pod_name: str, namespace: str = "default",
                                   container_name: Optional[str] = None,
                                   tail: Optional[int] = None,
                                   since_seconds: Optional[int] = None,
                                   previous: bool = False) -> Optional[str]:
    return _state().get_logs_for_pod_and_container(
        pod_name, namespace, container_name, tail=tail,
        since_seconds=since_seconds, previous=previous)

get_logs_for_pod_and_container.__doc__ = k8s_tools.get_logs_for_pod_and_container.__doc__


def get_deployment_summaries(namespace: Optional[str] = None) -> list[k8s_tools.DeploymentSummary]:
    return _state().get_deployment_summaries(namespace)

get_deployment_summaries.__doc__ = k8s_tools.get_deployment_summaries.__doc__


def get_replicaset_summaries(namespace: Optional[str] = None,
                             deployment: Optional[str] = None) -> list[k8s_tools.ReplicaSetSummary]:
    return _state().get_replicaset_summaries(namespace, deployment)

get_replicaset_summaries.__doc__ = k8s_tools.get_replicaset_summaries.__doc__


def get_service_summaries(namespace: Optional[str] = None) -> list[k8s_tools.ServiceSummary]:
    return _state().get_service_summaries(namespace)

get_service_summaries.__doc__ = k8s_tools.get_service_summaries.__doc__


def get_configmap_summaries(namespace: Optional[str] = None) -> list[k8s_tools.ConfigMapSummary]:
    return _state().get_configmap_summaries(namespace)

get_configmap_summaries.__doc__ = k8s_tools.get_configmap_summaries.__doc__


def get_configmap(name: str, namespace: str = "default") -> dict[str, Any]:
    return _state().get_configmap(name, namespace)

get_configmap.__doc__ = k8s_tools.get_configmap.__doc__


def get_statefulset_summaries(namespace: Optional[str] = None) -> list[k8s_tools.StatefulSetSummary]:
    return _state().get_statefulset_summaries(namespace)

get_statefulset_summaries.__doc__ = k8s_tools.get_statefulset_summaries.__doc__


def get_cronjob_summaries(namespace: Optional[str] = None) -> list[k8s_tools.CronJobSummary]:
    return _state().get_cronjob_summaries(namespace)

get_cronjob_summaries.__doc__ = k8s_tools.get_cronjob_summaries.__doc__


def get_job_summaries(namespace: Optional[str] = None) -> list[k8s_tools.JobSummary]:
    return _state().get_job_summaries(namespace)

get_job_summaries.__doc__ = k8s_tools.get_job_summaries.__doc__


def get_logs_for_job(job_name: str, namespace: str = "default",
                     container_name: Optional[str] = None,
                     tail: Optional[int] = None,
                     since_seconds: Optional[int] = None,
                     previous: bool = False) -> Optional[str]:
    return _state().get_logs_for_job(job_name, namespace, container_name, tail=tail,
                                     since_seconds=since_seconds, previous=previous)

get_logs_for_job.__doc__ = k8s_tools.get_logs_for_job.__doc__


def get_logs_for_cronjob(cronjob_name: str, namespace: str = "default",
                         container_name: Optional[str] = None,
                         tail: Optional[int] = None,
                         since_seconds: Optional[int] = None,
                         previous: bool = False) -> Optional[str]:
    return _state().get_logs_for_cronjob(cronjob_name, namespace, container_name, tail=tail,
                                         since_seconds=since_seconds, previous=previous)

get_logs_for_cronjob.__doc__ = k8s_tools.get_logs_for_cronjob.__doc__


def get_pvc_summaries(namespace: Optional[str] = None) -> list[k8s_tools.PVCSummary]:
    return _state().get_pvc_summaries(namespace)

get_pvc_summaries.__doc__ = k8s_tools.get_pvc_summaries.__doc__


def get_events(namespace: Optional[str] = None,
               reason: Optional[str] = None,
               involved_kind: Optional[str] = None,
               involved_name: Optional[str] = None,
               event_type: Optional[str] = None) -> list[k8s_tools.EventSummary]:
    return _state().get_events(namespace, reason, involved_kind, involved_name, event_type)

get_events.__doc__ = k8s_tools.get_events.__doc__


TOOLS = [
    get_namespaces,
    get_node_summaries,
    get_pod_summaries,
    get_pod_container_statuses,
    get_pod_events,
    get_pod_spec,
    get_logs_for_pod_and_container,
    get_deployment_summaries,
    get_replicaset_summaries,
    get_service_summaries,
    get_configmap_summaries,
    get_configmap,
    get_statefulset_summaries,
    get_cronjob_summaries,
    get_job_summaries,
    get_logs_for_job,
    get_logs_for_cronjob,
    get_pvc_summaries,
    get_events,
]
