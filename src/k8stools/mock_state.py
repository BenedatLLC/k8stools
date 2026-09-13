# Copyright (c) 2025 Benedat LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
#
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Load a captured cluster snapshot and serve it to the tool queries.

A *capture* is a single JSON file written by :mod:`k8stools.capture` holding the
state of a real cluster at one instant. :class:`MockState` loads such a file and
answers the same queries as the functions in :mod:`k8stools.k8s_tools`, so an
agent can be exercised against a realistic multi-resource cluster with no cluster
present. :mod:`k8stools.mock_tools` delegates to it, and the MCP server's
``--state-file`` option points it at an arbitrary capture.

**Temporal encoding.** Everything time-related is stored relative to the capture's
``captured_at``: a ``timedelta`` becomes a ``<field>_seconds`` float and a
``datetime`` becomes a ``<field>_offset_seconds`` float counted *backwards* from
``captured_at``. At load time :class:`MockState` records a ``server_start_time``
and every query reconstructs those fields against it, so a pod that was 10
minutes old at capture time is 10 minutes old at server start and ages forward
from there. Because every age in a capture is stored against the same instant and
advanced by the same offset, the *intervals between* resources survive replay
exactly - which is what lets an agent conclude "this deployment is 8 days old but
its current replica set is 7h34m old, so it was upgraded 7h34m ago".

**The replay clock has two modes** (see :class:`MockState`'s ``frozen``
argument). Advancing (the default) is right for interactive use: ages move the way
someone poking at a cluster expects. Frozen is right for an automated suite, where
an advancing clock makes the same scenario yield different ages on every run and
lets a long session see ages drift between its first tool call and its last.
"""

from __future__ import annotations

import contextlib
import datetime
import functools
import gzip
import json
import types
import typing
import zlib
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import BaseModel

from . import k8s_tools


#: Version of the capture file format this module reads and :mod:`k8stools.capture` writes.
CAPTURE_VERSION = "1"

#: Path of the built-in capture (the OTel Demo on Minikube snapshot).
BUILTIN_STATE_FILE = Path(__file__).parent / "fixtures" / "otel-demo.json"


class CaptureFormatError(Exception):
    """Raised when a capture file is missing, malformed, or of an unknown version."""
    pass


# ---------------------------------------------------------------------------
# Temporal field codec
#
# The suffix a field gets is decided by its *declared* type, not its runtime
# value, so an Optional field that happens to be None still round-trips under the
# same key. These two functions are the only place that mapping lives; capture.py
# imports the encoder so the writer and the reader cannot drift apart.
# ---------------------------------------------------------------------------

def _annotation_contains(annotation: Any, target: type) -> bool:
    """True if ``target`` appears anywhere in ``annotation`` (unwrapping Optional/Union)."""
    if annotation is target:
        return True
    origin = typing.get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        return any(_annotation_contains(arg, target) for arg in typing.get_args(annotation))
    return False


def _encoded_name(field_name: str, annotation: Any) -> str:
    """The JSON key a model field is stored under."""
    if _annotation_contains(annotation, datetime.timedelta):
        return f"{field_name}_seconds"
    if _annotation_contains(annotation, datetime.datetime):
        return f"{field_name}_offset_seconds"
    return field_name


def _model_classes(annotation: Any) -> list[type[BaseModel]]:
    """Every Pydantic model class reachable from ``annotation``."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [cls
            for arg in typing.get_args(annotation)
            for cls in _model_classes(arg)]


def encode_model(model: BaseModel, captured_at: datetime.datetime) -> dict[str, Any]:
    """Serialize a tool return model to its capture-file representation.

    ``timedelta`` fields become ``<name>_seconds`` floats and ``datetime`` fields
    become ``<name>_offset_seconds`` floats measured backwards from
    ``captured_at``. Nested models are encoded recursively; every other field is
    stored verbatim.
    """
    out: dict[str, Any] = {}
    for name, field in type(model).model_fields.items():
        value = getattr(model, name)
        key = _encoded_name(name, field.annotation)
        out[key] = _encode_value(value, captured_at)
    return out


def _encode_value(value: Any, captured_at: datetime.datetime) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, datetime.datetime):
        return (captured_at - value).total_seconds()
    if isinstance(value, BaseModel):
        return encode_model(value, captured_at)
    if isinstance(value, list):
        return [_encode_value(v, captured_at) for v in value]
    if isinstance(value, dict):
        return {k: _encode_value(v, captured_at) for k, v in value.items()}
    return value


class _Clock:
    """The replay clock. Reconstructs temporal fields against the server's start time.

    ``frozen`` pins elapsed time at zero, so every query returns precisely the
    ages recorded at capture time and every interval derived from them is stable
    across runs and across a long session. Datetime reconstruction is anchored to
    ``server_start_time`` and so is internally consistent in either mode.

    Two details keep the *intervals between* resources intact, which is what an
    agent actually reasons over ("this deployment is 8 days old, its current
    replica set 7h34m, so it was upgraded 7h34m ago"):

    * Elapsed time is quantized to whole seconds. Kubernetes ages are meaningful
      at second granularity - ``kubectl`` prints ``8d`` and ``7h34m`` - so
      advancing them by a microsecond-precise offset would add false precision,
      and false precision is what turns two resources captured at the same instant
      into two resources of provably different age.
    * It is sampled once per query and reused for every field in the result, so a
      list of resources can never be internally inconsistent with itself.

    Across *separate* queries an advancing clock can still tick between calls;
    that is inherent to advancing time, and is the reason frozen mode exists.
    """

    def __init__(self, server_start_time: datetime.datetime, frozen: bool = False):
        self.server_start_time = server_start_time
        self.frozen = frozen
        self._pinned: Optional[float] = None

    def elapsed(self) -> float:
        if self.frozen:
            return 0.0
        if self._pinned is not None:
            return self._pinned
        return self._sample()

    def _sample(self) -> float:
        delta = (datetime.datetime.now(datetime.timezone.utc) - self.server_start_time)
        return float(int(delta.total_seconds()))

    @contextlib.contextmanager
    def pinned(self):
        """Sample elapsed time once for the duration of one query."""
        if self.frozen or self._pinned is not None:
            yield  # already stable, or an outer query already pinned it
            return
        self._pinned = self._sample()
        try:
            yield
        finally:
            self._pinned = None

    def age(self, seconds: float) -> datetime.timedelta:
        return datetime.timedelta(seconds=seconds + self.elapsed())

    def dt(self, offset_seconds: float) -> datetime.datetime:
        return self.server_start_time - datetime.timedelta(seconds=offset_seconds)


def _pinned_query(method):
    """Sample the replay clock once for the whole of a query method."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._clock.pinned():
            return method(self, *args, **kwargs)
    return wrapper


def decode_model(cls: type[BaseModel], record: dict[str, Any], clock: _Clock) -> BaseModel:
    """Rebuild a tool return model from its capture-file representation.

    The inverse of :func:`encode_model`, with the stored offsets resolved against
    ``clock``.
    """
    kwargs: dict[str, Any] = {}
    for name, field in cls.model_fields.items():
        key = _encoded_name(name, field.annotation)
        if key not in record:
            continue  # let the model's own default apply
        kwargs[name] = _decode_value(record[key], field.annotation, clock)
    return cls(**kwargs)


def _decode_value(value: Any, annotation: Any, clock: _Clock) -> Any:
    if value is None:
        return None
    if _annotation_contains(annotation, datetime.timedelta):
        return clock.age(value)
    if _annotation_contains(annotation, datetime.datetime):
        return clock.dt(value)

    candidates = _model_classes(annotation)
    if candidates and isinstance(value, dict):
        return _decode_submodel(candidates, value, clock)
    if isinstance(value, list):
        args = typing.get_args(annotation)
        item_annotation = args[0] if args else Any
        return [_decode_value(v, item_annotation, clock) for v in value]
    return value


def _decode_submodel(candidates: list[type[BaseModel]], value: dict[str, Any],
                     clock: _Clock) -> BaseModel:
    """Pick the right model from a union and decode into it.

    ``ContainerState`` is a union of Running/Waiting/Terminated discriminated by
    the literal ``state_name`` field, which is how the state a container was in at
    capture time is recovered.
    """
    if len(candidates) > 1:
        discriminator = value.get("state_name")
        for cls in candidates:
            field = cls.model_fields.get("state_name")
            if field is not None and typing.get_args(field.annotation)[:1] == (discriminator,):
                return decode_model(cls, value, clock)
        # No match: fall through to the first candidate so a malformed record
        # produces a validation error naming the field, not a silent drop.
    return decode_model(candidates[0], value, clock)


# ---------------------------------------------------------------------------
# MockState
# ---------------------------------------------------------------------------

class MockState:
    """A captured cluster snapshot, queryable through the k8stools tool surface.

    Every ``get_*`` method mirrors the signature *and the documented behavior* of
    its :mod:`k8stools.k8s_tools` counterpart, including filter semantics and
    result ordering. Where the real tool guarantees an ordering, this class
    reproduces it at query time rather than echoing the order the capture happened
    to be written in.
    """

    def __init__(self, data: dict[str, Any], frozen: bool = False,
                 server_start_time: Optional[datetime.datetime] = None):
        version = data.get("version")
        if version != CAPTURE_VERSION:
            raise CaptureFormatError(
                f"Unsupported capture version {version!r}; this build reads version "
                f"{CAPTURE_VERSION!r}.")
        self._data = data
        self._clock = _Clock(
            server_start_time or datetime.datetime.now(datetime.timezone.utc),
            frozen=frozen)

        self.captured_at: Optional[str] = data.get("captured_at")
        self.redacted: bool = bool(data.get("redacted", False))

        # Index the pod records for O(1) lookup; every other list is small enough
        # to filter linearly, which keeps the filtering identical to the real tools'.
        self._pods: dict[tuple[str, str], dict[str, Any]] = {}
        for record in data.get("pods", []):
            summary = record.get("summary", {})
            self._pods[(summary.get("namespace", ""), summary.get("name", ""))] = record

    # -- construction -------------------------------------------------------

    @classmethod
    def from_file(cls, path: Path, frozen: bool = False) -> "MockState":
        """Load a capture written by ``k8s-capture-state``.

        Gzipped captures are read transparently. Detection is by content (the
        gzip magic bytes), not by file name, so a capture still loads after being
        renamed or downloaded without its extension.
        """
        path = Path(path)
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except FileNotFoundError as e:
            raise CaptureFormatError(f"Capture file not found: {path}") from e
        if raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
            except (OSError, EOFError, zlib.error) as e:
                # BadGzipFile is an OSError, but a truncated or header-corrupt
                # file raises EOFError instead.
                raise CaptureFormatError(
                    f"Capture file {path} looks gzipped but could not be "
                    f"decompressed: {e}") from e
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise CaptureFormatError(f"Capture file {path} is not valid JSON: {e}") from e
        return cls(data, frozen=frozen)

    @classmethod
    def from_builtin(cls, frozen: bool = False) -> "MockState":
        """Load the built-in capture (the OTel Demo on Minikube snapshot)."""
        return cls.from_file(BUILTIN_STATE_FILE, frozen=frozen)

    # -- helpers ------------------------------------------------------------

    def _decode_all(self, key: str, model: type[BaseModel],
                    namespace: Optional[str]) -> list[Any]:
        """Decode a top-level list, filtered by namespace the way the real tools do."""
        records = self._data.get(key, [])
        if namespace is not None:
            records = [r for r in records if r.get("namespace") == namespace]
        return [decode_model(model, r, self._clock) for r in records]

    # -- queries ------------------------------------------------------------

    @_pinned_query
    def get_namespaces(self) -> list[k8s_tools.NamespaceSummary]:
        return self._decode_all("namespaces", k8s_tools.NamespaceSummary, None)

    @_pinned_query
    def get_node_summaries(self) -> list[k8s_tools.NodeSummary]:
        return self._decode_all("nodes", k8s_tools.NodeSummary, None)

    @_pinned_query
    def get_pod_summaries(self, namespace: Optional[str] = None) -> list[k8s_tools.PodSummary]:
        records = [r.get("summary", {}) for r in self._data.get("pods", [])]
        if namespace is not None:
            records = [r for r in records if r.get("namespace") == namespace]
        return [decode_model(k8s_tools.PodSummary, r, self._clock) for r in records]

    @_pinned_query
    def get_pod_container_statuses(self, pod_name: str,
                                   namespace: str = "default") -> list[k8s_tools.ContainerStatus]:
        record = self._pods.get((namespace, pod_name))
        if record is None:
            return []
        return [decode_model(k8s_tools.ContainerStatus, cs, self._clock)
                for cs in record.get("container_statuses", [])]

    @_pinned_query
    def get_pod_spec(self, pod_name: str, namespace: str = "default") -> dict[str, Any]:
        record = self._pods.get((namespace, pod_name))
        if record is None:
            # The real tool turns a 404 into K8sApiError with this wording.
            raise k8s_tools.K8sApiError(
                f"Pod '{pod_name}' not found in namespace '{namespace}'.")
        return record.get("spec", {})

    @_pinned_query
    def get_pod_events(self, pod_name: str,
                       namespace: str = "default") -> list[k8s_tools.EventSummary]:
        """Events for one pod, filtered out of the capture's flat event list.

        Matches the real tool, which selects on the involved object's *name*
        within the namespace and reports the object as a bare name rather than
        the ``Kind/name`` form ``get_events`` uses.
        """
        results = []
        for record in self._data.get("events", []):
            if record.get("namespace") != namespace:
                continue
            if record.get("involved_name") != pod_name:
                continue
            results.append(self._event_summary(record, object_name=pod_name))
        return results

    @_pinned_query
    def get_events(self, namespace: Optional[str] = None,
                   reason: Optional[str] = None,
                   involved_kind: Optional[str] = None,
                   involved_name: Optional[str] = None,
                   event_type: Optional[str] = None) -> list[k8s_tools.EventSummary]:
        results = []
        for record in self._data.get("events", []):
            if namespace is not None and record.get("namespace") != namespace:
                continue
            if reason is not None and record.get("reason") != reason:
                continue
            if involved_kind is not None and record.get("involved_kind") != involved_kind:
                continue
            if involved_name is not None and record.get("involved_name") != involved_name:
                continue
            if event_type is not None and record.get("type") != event_type:
                continue
            results.append(self._event_summary(record))
        return results

    def _event_summary(self, record: dict[str, Any],
                       object_name: Optional[str] = None) -> k8s_tools.EventSummary:
        last_seen = record.get("last_seen_seconds")
        if object_name is None:
            kind = record.get("involved_kind")
            name = record.get("involved_name") or ""
            object_name = record.get("object") or (f"{kind}/{name}" if kind else name)
        return k8s_tools.EventSummary(
            last_seen=self._clock.age(last_seen) if last_seen is not None else None,
            type=record.get("type", ""),
            reason=record.get("reason", ""),
            object=object_name,
            message=record.get("message", ""),
        )

    @_pinned_query
    def get_logs_for_pod_and_container(self, pod_name: str, namespace: str = "default",
                                       container_name: Optional[str] = None,
                                       tail: Optional[int] = None,
                                       since_seconds: Optional[int] = None,
                                       previous: bool = False) -> Optional[str]:
        record = self._pods.get((namespace, pod_name))
        if record is None:
            raise k8s_tools.K8sApiError(
                f"Error fetching logs: pod '{pod_name}' not found in namespace '{namespace}'.")
        logs = record.get("previous_logs" if previous else "logs", {}) or {}
        if container_name is None:
            # The real tool defaults to the pod's first container.
            statuses = record.get("container_statuses", [])
            if statuses:
                container_name = statuses[0].get("container_name")
            elif logs:
                container_name = next(iter(logs))
        if container_name not in logs:
            if previous:
                # No previous instance was captured for this container, which is
                # what the real tool reports when there is none to read.
                raise k8s_tools.K8sApiError(
                    f"Error fetching logs: previous terminated instance of container "
                    f"'{container_name}' in pod '{pod_name}' not found.")
            return ''
        return _slice_log(logs[container_name], tail=tail, since_seconds=since_seconds,
                          now=self._clock.server_start_time + datetime.timedelta(
                              seconds=self._clock.elapsed()))

    @_pinned_query
    def get_deployment_summaries(self,
                                 namespace: Optional[str] = None) -> list[k8s_tools.DeploymentSummary]:
        return self._decode_all("deployments", k8s_tools.DeploymentSummary, namespace)

    @_pinned_query
    def get_replicaset_summaries(self, namespace: Optional[str] = None,
                                 deployment: Optional[str] = None) -> list[k8s_tools.ReplicaSetSummary]:
        """Replica sets, filtered and *then* ordered the way the real tool orders them.

        The real tool's docstring promises results grouped by namespace and owning
        deployment and sorted by revision oldest-first, so that filtered to one
        deployment "the last entry is that deployment's current revision". Callers
        rely on that sentence, so the ordering is recomputed here rather than taken
        from the order the capture was written in.

        Replica sets with no revision sort first, ahead of revision 1, matching
        `k8s_tools.get_replicaset_summaries`. See that function's docstring for
        what a missing revision means.
        """
        records = self._data.get("replicasets", [])
        if namespace is not None:
            records = [r for r in records if r.get("namespace") == namespace]
        if deployment is not None:
            records = [r for r in records if r.get("owner_deployment") == deployment]
        summaries = [decode_model(k8s_tools.ReplicaSetSummary, r, self._clock) for r in records]
        # Replica sets without a revision sort first rather than being dropped,
        # matching k8s_tools.get_replicaset_summaries.
        summaries.sort(key=lambda r: (r.namespace, r.owner_deployment or "",
                                      r.revision if r.revision is not None else -1))
        return summaries

    @_pinned_query
    def get_service_summaries(self,
                              namespace: Optional[str] = None) -> list[k8s_tools.ServiceSummary]:
        return self._decode_all("services", k8s_tools.ServiceSummary, namespace)

    @_pinned_query
    def get_configmap_summaries(self,
                                namespace: Optional[str] = None) -> list[k8s_tools.ConfigMapSummary]:
        records = self._data.get("configmaps", [])
        if namespace is not None:
            records = [r for r in records if r.get("namespace") == namespace]
        summaries = []
        for r in records:
            key_count, data_size = _configmap_counts(r)
            summaries.append(k8s_tools.ConfigMapSummary(
                name=r.get("name", ""),
                namespace=r.get("namespace", ""),
                key_count=key_count,
                data_size=data_size,
                age=self._clock.age(r.get("age_seconds", 0.0)),
            ))
        return summaries

    @_pinned_query
    def get_configmap(self, name: str, namespace: str = "default") -> dict[str, Any]:
        for r in self._data.get("configmaps", []):
            if r.get("name") == name and r.get("namespace") == namespace:
                return {
                    "name": r.get("name"),
                    "namespace": r.get("namespace"),
                    "data": dict(r.get("data") or {}),
                    "binary_data_keys": list(r.get("binary_data_keys") or []),
                }
        raise k8s_tools.K8sApiError(
            f"ConfigMap '{name}' not found in namespace '{namespace}'.")

    @_pinned_query
    def get_statefulset_summaries(self,
                                  namespace: Optional[str] = None) -> list[k8s_tools.StatefulSetSummary]:
        return self._decode_all("statefulsets", k8s_tools.StatefulSetSummary, namespace)

    @_pinned_query
    def get_cronjob_summaries(self,
                              namespace: Optional[str] = None) -> list[k8s_tools.CronJobSummary]:
        return self._decode_all("cronjobs", k8s_tools.CronJobSummary, namespace)

    @_pinned_query
    def get_job_summaries(self, namespace: Optional[str] = None) -> list[k8s_tools.JobSummary]:
        return self._decode_all("jobs", k8s_tools.JobSummary, namespace)

    @_pinned_query
    def get_pvc_summaries(self, namespace: Optional[str] = None) -> list[k8s_tools.PVCSummary]:
        return self._decode_all("pvcs", k8s_tools.PVCSummary, namespace)

    @_pinned_query
    def get_logs_for_job(self, job_name: str, namespace: str = "default",
                         container_name: Optional[str] = None,
                         tail: Optional[int] = None,
                         since_seconds: Optional[int] = None,
                         previous: bool = False) -> Optional[str]:
        """Logs of the Job's most-recently-created pod, resolved the way the real tool does.

        The real tool selects pods by the ``job-name`` label and takes the newest;
        captured pod records carry their labels so the same selection is possible
        here, with the smallest age standing in for the newest creation timestamp.
        """
        pod_name = self._latest_pod_for_label(namespace, "job-name", job_name)
        if pod_name is None:
            return None
        return self.get_logs_for_pod_and_container(pod_name, namespace, container_name,
                                                   tail=tail, since_seconds=since_seconds,
                                                   previous=previous)

    @_pinned_query
    def get_logs_for_cronjob(self, cronjob_name: str, namespace: str = "default",
                             container_name: Optional[str] = None,
                             tail: Optional[int] = None,
                             since_seconds: Optional[int] = None,
                             previous: bool = False) -> Optional[str]:
        """Logs of the CronJob's newest Job's newest pod, as the real tool resolves them."""
        owned = [r for r in self._data.get("jobs", [])
                 if r.get("namespace") == namespace and r.get("owner") == cronjob_name]
        if not owned:
            return None
        newest = min(owned, key=lambda r: r.get("age_seconds", 0.0))
        return self.get_logs_for_job(newest.get("name", ""), namespace, container_name,
                                     tail=tail, since_seconds=since_seconds, previous=previous)

    def _latest_pod_for_label(self, namespace: str, label: str, value: str) -> Optional[str]:
        matching = [r for r in self._data.get("pods", [])
                    if r.get("summary", {}).get("namespace") == namespace
                    and (r.get("labels") or {}).get(label) == value]
        if not matching:
            return None
        newest = min(matching, key=lambda r: r.get("summary", {}).get("age_seconds", 0.0))
        return newest.get("summary", {}).get("name")


def _configmap_counts(record: dict[str, Any]) -> tuple[int, int]:
    """Key count and total value size for a captured ConfigMap.

    Both are stored by the capture because ``data_size`` counts the *bytes* of
    binary values, which ``get_configmap`` does not return - it yields only the
    binary key names - so a capture built from tool output cannot re-derive it.
    The derivation is kept as a fallback for hand-written fixtures.
    """
    if "key_count" in record and "data_size" in record:
        return int(record["key_count"]), int(record["data_size"])
    data = record.get("data") or {}
    binary_keys = record.get("binary_data_keys") or []
    return (len(data) + len(binary_keys),
            sum(len(v) for v in data.values() if v is not None))


def _slice_log(text: str, tail: Optional[int], since_seconds: Optional[int],
               now: datetime.datetime) -> str:
    """Apply the real log tool's ``tail`` / ``since_seconds`` limits to captured text.

    ``since_seconds`` is honoured only when the stored lines carry a parseable
    leading RFC3339 timestamp (which they do when captured, since the tool always
    passes ``timestamps=True``). When they do not, the filter is *skipped* rather
    than applied approximately: returning an empty log for a filter that cannot
    actually be evaluated would invent an absence of evidence.
    """
    lines = text.splitlines()
    if since_seconds is not None:
        cutoff = now - datetime.timedelta(seconds=since_seconds)
        filtered = []
        parseable = False
        for line in lines:
            ts = _leading_timestamp(line)
            if ts is None:
                filtered.append(line)
                continue
            parseable = True
            if ts >= cutoff:
                filtered.append(line)
        if parseable:
            lines = filtered
    if tail is not None:
        lines = lines[-tail:] if tail > 0 else []
    elif len(lines) > 1000:
        # The real tool defaults to the last 1000 lines.
        lines = lines[-1000:]
    return "\n".join(lines)


def _leading_timestamp(line: str) -> Optional[datetime.datetime]:
    """Parse the RFC3339 timestamp kubectl prefixes to each log line, if present."""
    token = line.split(" ", 1)[0]
    if not token:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(token.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed
