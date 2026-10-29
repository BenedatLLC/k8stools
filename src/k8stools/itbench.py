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
"""Import an ITBench-Lite snapshot as a capture (#30).

An ITBench-Lite scenario directory holds what an OpenTelemetry collector
exported from the cluster during the incident, as ClickHouse TSVs:

- ``k8s_objects_raw.tsv``: the k8sobjects receiver in pull mode. Every five
  minutes it lists each configured kind (one batch per kind, within a few
  seconds of each other) and writes one record per object, with the object's
  JSON as ``Body`` and the kind's plural in ``LogAttributes['k8s.resource.name']``.
  A kind with no objects writes nothing, so a pull without a kind can't say
  whether there were none or the batch was lost.
- ``k8s_events_raw.tsv``: the same receiver watching events, every event twice
  (core/v1 and events.k8s.io/v1, same uid); ADDED, MODIFIED and DELETED.
- ``otel_logs_raw.tsv``: the OTel SDK log records the services exported. Not
  the containers' stdout, and with no container name.

The importer rebuilds the cluster as of one pull and serves it through a fake
REST client (:func:`k8s_tools.use_api_client`), with the clock pinned to the
pull (:func:`k8s_tools.pinned_clock`), and runs the real
:func:`capture.capture_state` against it. So an imported capture holds what the
tools would have answered, through the same code as a live one, and replays
under the same rules. What the snapshot doesn't hold is left out of the
capture, and replay says "not recorded": the kinds the pull has no batch of,
container logs with no attributable records, every previous-instance log,
ControllerRevisions, custom resource instances and metrics.
"""

from __future__ import annotations

import ast
import csv
import datetime
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlparse

from kubernetes import client

from . import k8s_tools

OBJECTS_FILE = "k8s_objects_raw.tsv"
EVENTS_FILE = "k8s_events_raw.tsv"
LOGS_FILE = "otel_logs_raw.tsv"

#: A gap this long between object records starts a new pull (pulls are five
#: minutes apart; one pull's batches span a few seconds).
PULL_GAP = datetime.timedelta(seconds=60)

#: Each capture key, and the snapshot kind (plural) it is captured from.
CAPTURED_FROM = {
    "namespaces": "namespaces", "nodes": "nodes", "pods": "pods",
    "deployments": "deployments", "replicasets": "replicasets", "services": "services",
    "endpoints": "endpoints", "ingresses": "ingresses", "networkpolicies": "networkpolicies",
    "configmaps": "configmaps", "statefulsets": "statefulsets", "daemonsets": "daemonsets",
    "hpas": "horizontalpodautoscalers", "cronjobs": "cronjobs", "jobs": "jobs",
    "pvcs": "persistentvolumeclaims",
}

#: The API group each snapshot kind is served under ("" for core).
_GROUPS = {"deployments": "apps", "replicasets": "apps", "statefulsets": "apps",
           "daemonsets": "apps", "jobs": "batch", "cronjobs": "batch",
           "ingresses": "networking.k8s.io", "networkpolicies": "networking.k8s.io",
           "customresourcedefinitions": "apiextensions.k8s.io",
           "endpointslices": "discovery.k8s.io", "events": "",
           "horizontalpodautoscalers": "autoscaling"}

#: Kinds an import can't do without: a pull missing one is refused.
REQUIRED_KINDS = ("namespaces", "nodes", "pods")

METRICS_REASON = ("Imported from an ITBench snapshot, which records no metrics-server "
                  "readings: resource usage is unknown.")
CUSTOM_RESOURCES_REASON = ("Imported from an ITBench snapshot, which records custom resource "
                           "definitions but not their instances.")
REVISIONS_LIMIT = ("Imported from an ITBench snapshot, which doesn't record "
                   "ControllerRevisions: this workload's revisions are unknown.")


class SnapshotImportError(Exception):
    """The snapshot can't be imported as asked (missing files, no such pull)."""


# --- reading the TSVs -----------------------------------------------------------------

def _rows(path: Path) -> Iterator[dict[str, str]]:
    csv.field_size_limit(sys.maxsize)
    with open(path, newline="", encoding="utf-8") as f:
        yield from csv.DictReader(f, delimiter="\t")


def _parse_ts(text: str) -> datetime.datetime:
    """A ClickHouse ``DateTime64(9)``, written in UTC with no zone."""
    whole, _, frac = text.partition(".")
    return datetime.datetime.fromisoformat(whole).replace(
        microsecond=int((frac + "000000")[:6]), tzinfo=datetime.timezone.utc)


def _attrs(text: str) -> dict[str, Any]:
    """An attribute map, which ClickHouse writes as a Python dict repr."""
    return ast.literal_eval(text) if text else {}


def _api_time(ts: datetime.datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Pull:
    """One pull of the k8sobjects receiver: its batches, one per kind it found."""
    number: int  # 1-based, in time order
    started: datetime.datetime
    ended: datetime.datetime
    kinds: Counter = field(default_factory=Counter)  # plural -> objects

    def describe(self, all_kinds: set[str]) -> str:
        missing = sorted(all_kinds - set(self.kinds))
        line = (f"{self.number:>3}  {self.started:%Y-%m-%d %H:%M:%S}-{self.ended:%H:%M:%S}Z  "
                f"{len(self.kinds)} kinds, {self.kinds.get('pods', 0)} pods")
        return line + (f"; no batch of: {', '.join(missing)}" if missing else "")


def _check(directory: Path) -> None:
    for name in (OBJECTS_FILE, EVENTS_FILE):
        if not (directory / name).is_file():
            raise SnapshotImportError(f"{directory} is not an ITBench snapshot: no {name}")


def read_pulls(directory: Path) -> list[Pull]:
    """The snapshot's pulls, in time order."""
    _check(directory)
    stamps: dict[datetime.datetime, Counter] = defaultdict(Counter)
    for row in _rows(directory / OBJECTS_FILE):
        kind = _attrs(row["LogAttributes"]).get("k8s.resource.name")
        stamps[_parse_ts(row["Timestamp"])][kind] += 1
    pulls: list[Pull] = []
    for ts in sorted(stamps):
        if not pulls or ts - pulls[-1].ended > PULL_GAP:
            pulls.append(Pull(len(pulls) + 1, ts, ts))
        pulls[-1].ended = ts
        pulls[-1].kinds.update(stamps[ts])
    if not pulls:
        raise SnapshotImportError(f"{directory / OBJECTS_FILE} has no object records")
    return pulls


def all_kinds(pulls: list[Pull]) -> set[str]:
    return {k for p in pulls for k in p.kinds}


def _objects(directory: Path, pull: Pull) -> dict[str, list[dict]]:
    """The pull's objects, by kind (plural)."""
    objects: dict[str, list[dict]] = defaultdict(list)
    for row in _rows(directory / OBJECTS_FILE):
        if pull.started <= _parse_ts(row["Timestamp"]) <= pull.ended:
            kind = _attrs(row["LogAttributes"]).get("k8s.resource.name")
            objects[kind].append(json.loads(row["Body"]))
    return objects


def _events(directory: Path, at: datetime.datetime) -> tuple[list[dict], int]:
    """The core/v1 events that existed at ``at``, and how many are unknown.

    The watch began before the first pull, and an event that existed then
    appears only when it changes or expires (DELETED, about an hour after it last
    occurred, with its final state). So an event's state at ``at`` is its latest
    record up to then; with none, an event first seen later as DELETED existed
    unchanged at ``at`` if it last occurred by then. One first seen later as
    MODIFIED changed after ``at``, from a state that isn't recorded: counted,
    and left out.
    """
    history: dict[str, list[tuple[datetime.datetime, str, dict]]] = defaultdict(list)
    for row in _rows(directory / EVENTS_FILE):
        body = json.loads(row["Body"])
        obj = body.get("object") or {}
        if obj.get("apiVersion") != "v1":  # the events.k8s.io/v1 copy of the same event
            continue
        history[obj["metadata"]["uid"]].append((_parse_ts(row["Timestamp"]), body["type"], obj))
    events, unknown = [], 0
    for records in history.values():
        records.sort(key=lambda r: r[0])
        before = [r for r in records if r[0] <= at]
        if before:
            if before[-1][1] != "DELETED":
                events.append(before[-1][2])
            continue
        first_type, obj = records[0][1], records[0][2]
        if first_type == "ADDED":
            continue  # created after the pull
        last = obj.get("lastTimestamp") or obj.get("eventTime")
        if first_type == "DELETED" and last and _parse_api_time(last) <= at:
            events.append(obj)
        else:
            unknown += 1
    return events, unknown


def _parse_api_time(text: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


# --- logs -----------------------------------------------------------------------------

_TEMPLATE_RE = re.compile(r"\{(@?[A-Za-z_][\w.]*)\}")


def _log_line(row: dict[str, str], attributes: dict[str, Any]) -> str:
    """One record as kubelet-style lines: the record's time, its severity, its body
    with ``{Name}`` templates filled in, then the attributes the body doesn't
    already show (``code.*`` and trace ids left out)."""
    used: set[str] = set()

    def fill(m: re.Match) -> str:
        if m.group(1) in attributes:
            used.add(m.group(1))
            return str(attributes[m.group(1)])
        return m.group(0)

    body = _TEMPLATE_RE.sub(fill, row["Body"]).rstrip("\n")
    extra = [f"{k}={v}" for k, v in attributes.items()
             if k not in used and not k.startswith(("code.", "otel"))
             and str(v) not in body]
    whole, _, frac = row["Timestamp"].partition(".")
    stamp = f"{whole.replace(' ', 'T')}.{(frac + '000000000')[:9]}Z"
    severity = f"{row['SeverityText']} " if row["SeverityText"] else ""
    text = f"{severity}{body}" + (f" {' '.join(extra)}" if extra else "")
    return "\n".join(f"{stamp} {part}" for part in text.split("\n"))


def _instance_started(status: dict) -> Optional[datetime.datetime]:
    """When the container's latest instance started: the running or terminated
    one, or for a waiting container the last that terminated (what the log
    endpoint serves for it)."""
    state, last = status.get("state") or {}, status.get("lastState") or {}
    instance = state.get("running") or state.get("terminated") or last.get("terminated") or {}
    started = _parse_api_time(instance["startedAt"]) if instance.get("startedAt") else None
    # Never started (StartError): Go's zero time. No record can be its output.
    return datetime.datetime.max.replace(tzinfo=datetime.timezone.utc) \
        if started is not None and started.timestamp() <= 0 else started


@dataclass
class LogStats:
    pods_with_records: int = 0
    records_used: int = 0
    records_other_instances: int = 0
    records_unassigned: int = 0
    unassigned_pods: set = field(default_factory=set)


def _logs(directory: Path, at: datetime.datetime, pods: list[dict],
          stats: LogStats) -> dict[tuple[str, str, str], str]:
    """Each container's log: its current instance's OTel records up to ``at``.

    A record names its pod but not its container. It goes to the pod's only
    container, or to the one named like its service; otherwise it is unassigned
    and counted. A record from before the current instance started is an earlier
    instance's and is dropped (previous-instance logs aren't imported). The
    records' ``container.id`` can't tell instances apart: the SDKs' resource
    detectors report an id that matches none of the pod's containers, even one
    that never restarted. A
    container with no records at all gets no log, so replay says it wasn't
    recorded; one whose records are all earlier instances' gets an empty log.
    """
    if not (directory / LOGS_FILE).is_file():
        return {}
    by_uid = {p["metadata"].get("uid"): p for p in pods}
    lines: dict[tuple[str, str, str], list[tuple[datetime.datetime, str]]] = defaultdict(list)
    attributed: set[tuple[str, str, str]] = set()
    seen_pods: set[str] = set()
    for row in _rows(directory / LOGS_FILE):
        ts = _parse_ts(row["Timestamp"])
        if ts > at:
            continue
        resource = _attrs(row["ResourceAttributes"])
        pod = by_uid.get(resource.get("k8s.pod.uid"))
        if pod is None:
            continue
        seen_pods.add(pod["metadata"]["uid"])
        statuses = {s["name"]: s for s in (pod.get("status") or {}).get("containerStatuses") or []}
        names = [c["name"] for c in pod["spec"].get("containers") or []]
        if len(names) == 1:
            name = names[0]
        elif resource.get("service.name") in names:
            name = resource["service.name"]
        else:
            stats.records_unassigned += 1
            stats.unassigned_pods.add(f"{pod['metadata']['namespace']}/{pod['metadata']['name']}")
            continue
        key = (pod["metadata"]["namespace"], pod["metadata"]["name"], name)
        attributed.add(key)
        started = _instance_started(statuses.get(name) or {})
        if started is None or ts < started:
            stats.records_other_instances += 1
            continue
        stats.records_used += 1
        lines[key].append((ts, _log_line(row, _attrs(row["LogAttributes"]))))
    stats.pods_with_records = len(seen_pods)
    return {key: "\n".join(line for _, line in sorted(lines.get(key, []), key=lambda l: l[0]))
            for key in attributed}


# --- the fake API server ----------------------------------------------------------------

class _Response:
    def __init__(self, status: int, body: Any, content_type: str = "application/json"):
        self.status = status
        self.reason = {200: "OK", 404: "Not Found"}.get(status, "")
        self.data = (body if isinstance(body, str) else json.dumps(body)).encode()
        self._content_type = content_type

    def getheader(self, name, default=None):
        return self._content_type if name.lower() == "content-type" else default

    def getheaders(self):
        return {"content-type": self._content_type}


def _not_found(what: str) -> client.ApiException:
    return client.ApiException(http_resp=_Response(
        404, {"kind": "Status", "status": "Failure", "reason": "NotFound", "code": 404,
              "message": f"{what} not found"}))


def _endpoint_slices(endpoints: list[dict]) -> list[dict]:
    """EndpointSlices for the core Endpoints the snapshot has (it pulls no
    slices): one per subset, ready addresses ready, the others not."""
    slices = []
    for ep in endpoints:
        meta = ep.get("metadata") or {}
        for i, subset in enumerate(ep.get("subsets") or []):
            members = [(a, True) for a in subset.get("addresses") or []] + \
                      [(a, False) for a in subset.get("notReadyAddresses") or []]
            slices.append({
                "apiVersion": "discovery.k8s.io/v1", "kind": "EndpointSlice",
                "metadata": {"name": f"{meta.get('name')}-{i}", "namespace": meta.get("namespace"),
                             "creationTimestamp": meta.get("creationTimestamp"),
                             "labels": {"kubernetes.io/service-name": meta.get("name")}},
                "addressType": "IPv4",
                "endpoints": [{"addresses": [a.get("ip")],
                               "conditions": {"ready": ready, "serving": ready},
                               "nodeName": a.get("nodeName"), "targetRef": a.get("targetRef")}
                              for a, ready in members],
                "ports": [{"name": p.get("name"), "port": p.get("port"),
                           "protocol": p.get("protocol", "TCP")} for p in subset.get("ports") or []],
            })
    return slices


def _field(obj: dict, path: str) -> str:
    for part in path.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else None
    return "" if obj is None else str(obj)


def _selected(obj: dict, label_selector: Optional[str], field_selector: Optional[str]) -> bool:
    """Equality selectors (``a=b``, ``a==b``, ``a!=b``), all that the tools use."""
    labels = (obj.get("metadata") or {}).get("labels") or {}
    for selector, value_of in ((label_selector, lambda k: labels.get(k)),
                               (field_selector, lambda k: _field(obj, k))):
        for term in filter(None, (selector or "").split(",")):
            key, op, value = re.match(r"([^!=]+)(!=|==|=)(.*)", term).groups()
            if (value_of(key) == value) != (op != "!="):
                return False
    return True


class SnapshotRest:
    """Answers the Kubernetes API's GETs from one pull of a snapshot.

    A kind the pull has no batch of answers an empty list (the tools then
    capture nothing for it), and :func:`import_snapshot` removes its key, so
    replay says it wasn't recorded rather than that there are none.
    """

    def __init__(self, objects: dict[str, list[dict]], events: list[dict],
                 logs: dict[tuple[str, str, str], str]):
        self.objects = dict(objects)
        self.objects["events"] = events
        self.objects["endpointslices"] = _endpoint_slices(objects.get("endpoints", []))
        self.logs = logs

    def GET(self, url, query_params=None, **kwargs):
        parts = urlparse(url).path.strip("/").split("/")
        query = dict(query_params or [])
        if parts == ["version"]:
            raise _not_found("server version")
        group = "" if parts[0] == "api" else parts[1]
        if group == "metrics.k8s.io":
            raise _not_found("the metrics API")
        parts = parts[2:] if parts[0] == "api" else parts[3:]  # past api/v1, apis/<g>/<v>
        namespace = None
        if len(parts) >= 3 and parts[0] == "namespaces":
            namespace, parts = parts[1], parts[2:]
        kind, name, sub = (parts + [None, None])[:3]
        if sub == "log":
            return self._log(namespace, name, query)
        served = self.objects.get(kind, []) if _GROUPS.get(kind, "") == group else []
        items = [o for o in served
                 if namespace is None or (o.get("metadata") or {}).get("namespace") == namespace]
        if name is not None:
            found = next((o for o in items if o["metadata"].get("name") == name), None)
            if found is None:
                raise _not_found(f'{kind} "{name}"')
            return _Response(200, found)
        items = [o for o in items if _selected(o, query.get("labelSelector"),
                                               query.get("fieldSelector"))]
        return _Response(200, {"apiVersion": "v1", "kind": "List", "metadata": {}, "items": items})

    def _log(self, namespace, pod, query):
        container = query.get("container")
        if str(query.get("previous")).lower() == "true" or (namespace, pod, container) not in self.logs:
            raise _not_found(f'log of container "{container}" in pod "{pod}"')
        text = self.logs[(namespace, pod, container)]
        tail = query.get("tailLines")
        if tail is not None and text:
            text = "\n".join(text.split("\n")[-int(tail):])
        return _Response(200, text + ("\n" if text else ""), "text/plain")


# --- the import -----------------------------------------------------------------------

class _QuietLogReads(logging.Filter):
    """Drops the capture's warning for each log it can't read, and the metrics
    one: in an import those are the containers with no records, counted in
    :class:`LogStats`, and metrics that were never collected."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not (message.startswith("_read_pod_log(")
                    or message.startswith("Resource metrics not captured")
                    or message.startswith("Could not read the API server version"))


@dataclass
class ImportStats:
    pull: Optional[Pull] = None
    pulls: int = 0
    not_recorded: list[str] = field(default_factory=list)
    #: Kinds other pulls have and this one doesn't: none at the pull, or a lost batch.
    missing_from_pull: list[str] = field(default_factory=list)
    events: int = 0
    events_unknown: int = 0
    logs: LogStats = field(default_factory=LogStats)

    def report(self) -> str:
        p = self.pull
        lines = [f"Imported pull {p.number} of {self.pulls} "
                 f"({p.started:%Y-%m-%d %H:%M:%S}-{p.ended:%H:%M:%S}Z)."]
        if self.not_recorded:
            lines.append(f"  not recorded:           {', '.join(self.not_recorded)}")
        if self.missing_from_pull:
            lines.append(f"  in other pulls only:    {', '.join(self.missing_from_pull)} (none "
                         "at this pull, or its batch was lost: not recorded)")
        lines.append(f"  events:                 {self.events}"
                     + (f" ({self.events_unknown} changed after the pull from an "
                        "unrecorded state, left out)" if self.events_unknown else ""))
        lines.append(f"  log records used:       {self.logs.records_used} "
                     f"(from {self.logs.pods_with_records} pod(s))")
        lines.append(f"  earlier instances':     {self.logs.records_other_instances} (dropped)")
        if self.logs.records_unassigned:
            lines.append(f"  no container to assign: {self.logs.records_unassigned}, in "
                         f"{', '.join(sorted(self.logs.unassigned_pods))}")
        return "\n".join(lines)


def import_snapshot(directory: Path, pull: Optional[int] = None,
                    namespaces: Optional[list[str]] = None,
                    include_logs: bool = True,
                    max_log_lines: Optional[int] = None,
                    redact: bool = True,
                    stats: Optional[ImportStats] = None,
                    capture_stats=None) -> dict[str, Any]:
    """Convert one pull of an ITBench-Lite snapshot (default: the last) to a
    capture dict, as :func:`capture.capture_state` returns one."""
    from .capture import DEFAULT_MAX_LOG_LINES, capture_state

    directory = Path(directory)
    stats = stats if stats is not None else ImportStats()
    pulls = read_pulls(directory)
    if pull is None:
        pull = len(pulls)
    if not 1 <= pull <= len(pulls):
        raise SnapshotImportError(f"No pull {pull}: the snapshot has pulls 1-{len(pulls)} (--list-pulls).")
    chosen = pulls[pull - 1]
    stats.pull, stats.pulls = chosen, len(pulls)
    stats.missing_from_pull = sorted(all_kinds(pulls) - set(chosen.kinds))
    missing = [k for k in REQUIRED_KINDS if k not in chosen.kinds]
    if missing:
        raise SnapshotImportError(f"Pull {pull} has no batch of {', '.join(missing)}: "
                           "it can't be imported; choose another (--list-pulls).")
    at = chosen.ended
    objects = _objects(directory, chosen)
    events, stats.events_unknown = _events(directory, at)
    stats.events = len(events)
    logs = _logs(directory, at, objects.get("pods", []), stats.logs) if include_logs else {}

    api = client.ApiClient(client.Configuration(host="https://itbench-snapshot.invalid"))
    api.rest_client = SnapshotRest(objects, events, logs)
    quiet = _QuietLogReads()
    logging.getLogger().addFilter(quiet)
    try:
        with k8s_tools.use_api_client(api, context=f"itbench/{directory.name}",
                                      server="ITBench snapshot"), k8s_tools.pinned_clock(at):
            state = capture_state(namespaces=namespaces, include_logs=include_logs,
                                  max_log_lines=max_log_lines or DEFAULT_MAX_LOG_LINES,
                                  include_previous_logs=False, redact=redact,
                                  stats=capture_stats)
    finally:
        logging.getLogger().removeFilter(quiet)

    recorded = set(chosen.kinds)
    for key, kind in CAPTURED_FROM.items():
        if kind not in recorded:
            del state[key]
            stats.not_recorded.append(key)
    state["workload_histories"] = _histories(state)
    state["metrics"] = {"available": False, "reason": METRICS_REASON}
    if "customresourcedefinitions" not in recorded:
        del state["custom_resources"]
        stats.not_recorded.append("custom_resources")
    else:
        for record in state["custom_resources"].get("instances", []):
            record.pop("items", None)
            record.pop("truncated", None)
            record.update(available=False, reason=CUSTOM_RESOURCES_REASON)
    state["source"] = {"kind": "itbench", "snapshot": directory.name, "pull": pull,
                       "pulls": len(pulls), "pulled_from": chosen.started.isoformat(),
                       "logs": "otel"}
    return state


def _histories(state: dict[str, Any]) -> list[dict]:
    """Deployment histories stand (they come from ReplicaSets) only when the pull
    has ReplicaSets; a StatefulSet's or DaemonSet's revisions are unknown, since
    the snapshot has no ControllerRevisions, though what it references is not."""
    histories = []
    for record in state.get("workload_histories", []):
        if record.get("kind") == "Deployment":
            if "replicasets" in state:
                histories.append(record)
            continue
        histories.append(record | {"revisions": [], "complete": False,
                                   "limits": [REVISIONS_LIMIT] + list(record.get("limits", []))})
    return histories
