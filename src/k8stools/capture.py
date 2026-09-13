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
"""``k8s-capture-state`` - snapshot a live cluster to a replayable JSON file.

The capture calls the same ``get_*`` functions an agent would, so what it records
is exactly what an agent would have seen, and writes them to a single JSON file
that :class:`k8stools.mock_state.MockState` can serve back. See that module for
the file format and the temporal encoding.

**Redaction is on by default and has to be applied here explicitly.** The
redaction pass lives at the MCP server's *output boundary*; this CLI is a library
consumer of the tool functions and library consumers bypass that boundary. Without
a deliberate :func:`~k8stools.redaction.redact_object` pass a capture would
therefore write unredacted secrets to disk while the server in front of the same
cluster was redacting them. ``--no-redact`` (or ``K8STOOLS_REDACT=0``) opts out,
using the same helper the server uses so there is one rule rather than two.
Redaction is one-way: a capture taken with it on cannot be un-redacted later, only
re-captured.
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from . import k8s_tools
from .mock_state import CAPTURE_VERSION, encode_model
from .redaction import redact_object, redaction_enabled


#: Default number of log lines captured per container.
DEFAULT_MAX_LOG_LINES = 1000


class CaptureStats:
    """Counts worth reporting when a capture finishes.

    ``missing_previous_logs`` matters more than it looks: a crash-loop capture
    that lost its previous-instance logs is indistinguishable, once written, from
    one that never had any - and the previous instance is usually where the
    diagnosis is. Reporting the count is what makes that loss visible.
    """

    def __init__(self) -> None:
        self.pods = 0
        self.containers = 0
        self.logs_captured = 0
        self.previous_logs_captured = 0
        self.missing_previous_logs: list[str] = []
        self.redactions = 0

    def report(self) -> str:
        lines = [f"Captured {self.pods} pod(s), {self.containers} container(s)."]
        lines.append(f"  logs captured:          {self.logs_captured}")
        lines.append(f"  previous logs captured: {self.previous_logs_captured}")
        if self.missing_previous_logs:
            lines.append(
                f"  previous logs MISSING:  {len(self.missing_previous_logs)} "
                f"(restarted container(s) whose previous instance could not be read)")
            for ref in self.missing_previous_logs:
                lines.append(f"      {ref}")
        if self.redactions:
            lines.append(f"  values redacted:        {self.redactions}")
        return "\n".join(lines)


class _Redactor:
    """Applies the redaction pass to each tool's return value as it is captured.

    Deliberately applied per *tool result*, not once to the assembled capture.
    The rule this implements is "a capture holds whatever the tools would have
    returned", and the tools return models, dicts and strings - never the capture
    envelope. Redacting the envelope instead would subject capture-internal
    structure to the redaction pass's key-name heuristic, which reaches things the
    MCP output boundary never sees. Concretely: captured logs are stored in a dict
    keyed by container name, and a container named `valkey-cart` matches the
    heuristic's `key` pattern, so an envelope-wide pass replaced that container's
    entire log with the marker - a whole log destroyed by its own name, which the
    live server does not do because it returns the log as a bare string.
    """

    def __init__(self, enabled: bool, stats: "CaptureStats"):
        self.enabled = enabled
        self.stats = stats

    def __call__(self, value):
        if not self.enabled:
            return value
        redacted, count = redact_object(value)
        self.stats.redactions += count
        return redacted


def _encode_all(models: list[BaseModel], captured_at: datetime.datetime) -> list[dict[str, Any]]:
    return [encode_model(m, captured_at) for m in models]


def _pod_labels(namespace: str) -> dict[str, dict[str, str]]:
    """Labels of every pod in a namespace, keyed by pod name.

    Labels are not part of any tool's return value, but ``get_logs_for_job``
    resolves a Job to its pod through the ``job-name`` label, so a capture that
    dropped them could not answer that call on replay.
    """
    if k8s_tools.K8S is None:
        k8s_tools.K8S = k8s_tools._get_api_client()
    try:
        pods = k8s_tools.K8S.list_namespaced_pod(namespace=namespace).items
    except Exception as e:
        logging.warning(f"Could not read pod labels in namespace '{namespace}': {e}")
        return {}
    return {p.metadata.name: dict(p.metadata.labels or {}) for p in pods}


def capture_state(namespaces: Optional[list[str]] = None,
                  include_logs: bool = True,
                  max_log_lines: int = DEFAULT_MAX_LOG_LINES,
                  include_previous_logs: bool = True,
                  redact: bool = True,
                  stats: Optional[CaptureStats] = None) -> dict[str, Any]:
    """Snapshot the cluster and return the capture as a JSON-ready dict.

    Parameters
    ----------
    namespaces
        Namespaces to capture namespaced resources from. If None, captures all of
        them. Namespaces and nodes are always captured in full regardless.
    include_logs
        Capture each container's current logs.
    max_log_lines
        Cap on log lines per container.
    include_previous_logs
        Also capture the logs of each restarted container's previous instance.
        Ignored when `include_logs` is False - previous-instance logs are logs.
    redact
        Apply the secret-redaction pass to the assembled state.
    stats
        Optional :class:`CaptureStats` to accumulate counts into.
    """
    stats = stats if stats is not None else CaptureStats()
    # Previous-instance logs are logs: --no-logs means no logs at all. Without
    # this, --no-logs skipped the current instance and kept the previous one,
    # which made a "structure-only" capture *larger* than a normal one.
    include_previous_logs = include_previous_logs and include_logs
    redactor = _Redactor(redact, stats)
    captured_at = datetime.datetime.now(datetime.timezone.utc)

    all_namespaces = redactor(k8s_tools.get_namespaces())
    target_namespaces = namespaces if namespaces else [ns.name for ns in all_namespaces]

    state: dict[str, Any] = {
        "version": CAPTURE_VERSION,
        "captured_at": captured_at.isoformat(),
        "redacted": redact,
        "namespaces": _encode_all(all_namespaces, captured_at),
        "nodes": _encode_all(redactor(k8s_tools.get_node_summaries()), captured_at),
        "pods": [],
        "deployments": [],
        "replicasets": [],
        "services": [],
        "configmaps": [],
        "statefulsets": [],
        "cronjobs": [],
        "jobs": [],
        "pvcs": [],
        "events": [],
    }

    for ns in target_namespaces:
        state["deployments"] += _encode_all(redactor(k8s_tools.get_deployment_summaries(ns)), captured_at)
        state["replicasets"] += _encode_all(redactor(k8s_tools.get_replicaset_summaries(ns)), captured_at)
        state["services"] += _encode_all(redactor(k8s_tools.get_service_summaries(ns)), captured_at)
        state["statefulsets"] += _encode_all(redactor(k8s_tools.get_statefulset_summaries(ns)), captured_at)
        state["cronjobs"] += _encode_all(redactor(k8s_tools.get_cronjob_summaries(ns)), captured_at)
        state["jobs"] += _encode_all(redactor(k8s_tools.get_job_summaries(ns)), captured_at)
        state["pvcs"] += _encode_all(redactor(k8s_tools.get_pvc_summaries(ns)), captured_at)
        state["configmaps"] += _capture_configmaps(ns, captured_at, redactor)
        state["events"] += _capture_events(ns, captured_at, redactor)
        state["pods"] += _capture_pods(ns, captured_at, include_logs, max_log_lines,
                                       include_previous_logs, stats, redactor)
    return state


def _capture_configmaps(namespace: str, captured_at: datetime.datetime,
                        redactor: "_Redactor") -> list[dict[str, Any]]:
    """ConfigMap summaries plus their full contents, so both ConfigMap tools replay.

    ``key_count`` / ``data_size`` are stored alongside the content rather than
    re-derived on replay: ``data_size`` counts the bytes of binary values, and
    ``get_configmap`` returns only their key names.
    """
    records = []
    for summary in redactor(k8s_tools.get_configmap_summaries(namespace)):
        record = {
            "name": summary.name,
            "namespace": summary.namespace,
            "key_count": summary.key_count,
            "data_size": summary.data_size,
            "age_seconds": summary.age.total_seconds(),
            "data": {},
            "binary_data_keys": [],
        }
        try:
            full = redactor(k8s_tools.get_configmap(summary.name, summary.namespace))
            record["data"] = full.get("data", {})
            record["binary_data_keys"] = full.get("binary_data_keys", [])
        except k8s_tools.K8sApiError as e:
            logging.warning(f"Could not read ConfigMap "
                            f"'{summary.namespace}/{summary.name}': {e}")
        records.append(record)
    return records


def _capture_events(namespace: str, captured_at: datetime.datetime,
                    redactor: "_Redactor") -> list[dict[str, Any]]:
    """All events in a namespace, as one flat list.

    Events are captured per-namespace (rather than cluster-wide in one call) so
    each record can carry the namespace it came from, which ``EventSummary`` does
    not itself hold but both event tools filter on. The involved object's kind and
    name are split out of the summary's ``Kind/name`` form for the same reason:
    ``get_events`` filters on them, and ``get_pod_events`` reports a bare name.
    """
    records = []
    for event in redactor(k8s_tools.get_events(namespace=namespace)):
        kind, sep, name = event.object.partition("/")
        if not sep:  # no kind available; the whole string is the name
            kind, name = None, event.object
        records.append({
            "last_seen_seconds": event.last_seen.total_seconds() if event.last_seen else None,
            "type": event.type,
            "reason": event.reason,
            "namespace": namespace,
            "involved_kind": kind,
            "involved_name": name,
            "object": event.object,
            "message": event.message,
        })
    return records


def _capture_pods(namespace: str, captured_at: datetime.datetime,
                  include_logs: bool, max_log_lines: int,
                  include_previous_logs: bool, stats: CaptureStats,
                  redactor: "_Redactor") -> list[dict[str, Any]]:
    """One self-contained record per pod: summary, labels, statuses, spec and logs."""
    labels_by_pod = _pod_labels(namespace)
    records = []
    for summary in redactor(k8s_tools.get_pod_summaries(namespace)):
        stats.pods += 1
        statuses = redactor(_safe(k8s_tools.get_pod_container_statuses, summary.name,
                                  namespace, default=[]))
        record: dict[str, Any] = {
            "summary": encode_model(summary, captured_at),
            "labels": labels_by_pod.get(summary.name, {}),
            "container_statuses": _encode_all(statuses, captured_at),
            "spec": redactor(_safe(k8s_tools.get_pod_spec, summary.name, namespace,
                                   default={})),
            "logs": {},
        }
        previous_logs: dict[str, str] = {}
        for status in statuses:
            stats.containers += 1
            if include_logs:
                logs = _safe(k8s_tools.get_logs_for_pod_and_container, summary.name,
                             namespace, status.container_name, tail=max_log_lines,
                             default=None)
                if logs is not None:
                    record["logs"][status.container_name] = redactor(logs)
                    stats.logs_captured += 1
            if include_previous_logs and status.restart_count > 0:
                # A missing previous instance is expected - it may have been
                # garbage-collected - so this is non-fatal, but it is counted.
                previous = _safe(k8s_tools.get_logs_for_pod_and_container, summary.name,
                                 namespace, status.container_name, tail=max_log_lines,
                                 previous=True, default=None)
                if previous is not None:
                    previous_logs[status.container_name] = redactor(previous)
                    stats.previous_logs_captured += 1
                else:
                    stats.missing_previous_logs.append(
                        f"{namespace}/{summary.name}/{status.container_name}")
        if previous_logs:
            record["previous_logs"] = previous_logs
        records.append(record)
    return records


def _safe(fn, *args, default=None, **kwargs):
    """Call a tool, logging and swallowing its failure so one bad pod cannot end the capture."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logging.warning(f"{fn.__name__}{args} failed: {e}")
        return default


def write_state(state: dict[str, Any], output: Path) -> None:
    """Write a capture, gzipping it when the filename ends in ``.gz``.

    Indented rather than compact on purpose. A capture checked into git is
    re-captured and re-committed over time, and git stores blobs zlib-compressed
    *and* deltas successive versions against each other - which works far better
    on indented JSON than on a compact line, and cannot work at all on a gzipped
    blob. Measured on two captures of the same 38-pod cluster: updating a
    committed `.json` fixture cost 61 KB, the same update as `.gz` cost 358 KB.
    So prefer plain `.json` for anything under source control, and reach for `.gz`
    when the file travels on its own - attached to an issue, or simply too large
    to keep expanded in a working tree.
    """
    if output.suffix == ".gz":
        with gzip.open(output, "wt", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=False)
    else:
        with open(output, "w") as f:
            json.dump(state, f, indent=2, sort_keys=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="k8s-capture-state",
        description="Snapshot a Kubernetes cluster to a JSON file replayable by the "
                    "k8stools MCP server (--state-file) or MockState.")
    parser.add_argument('--namespace', nargs='+', metavar='NS', default=None,
                        help="Namespace(s) to capture namespaced resources from "
                             "[default: all]. Namespaces and nodes are always captured in full.")
    parser.add_argument('-o', '--output', metavar='FILE', default=None,
                        help="Output file [default: k8s-state-<timestamp>.json]. "
                             "A name ending in .gz is written gzipped; captures are "
                             "read back compressed or not either way.")
    parser.add_argument('--no-logs', action='store_true', default=False,
                        help="Skip container logs entirely, including previous-instance "
                             "logs, for a structure-only snapshot")
    parser.add_argument('--max-log-lines', type=int, default=DEFAULT_MAX_LOG_LINES,
                        help=f"Log lines to capture per container [default: {DEFAULT_MAX_LOG_LINES}]")
    parser.add_argument('--no-previous-logs', action='store_true', default=False,
                        help="Skip the previous-instance logs of restarted containers. "
                             "These usually carry the diagnosis for a crash loop, so "
                             "dropping them is rarely what you want.")
    parser.add_argument('--no-redact', action='store_true', default=False,
                        help="Disable secret redaction of captured values (redaction is on "
                             "by default; can also be disabled with K8STOOLS_REDACT=0)")
    parser.add_argument('--log-level', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        default='INFO', help="Log level [default: INFO]")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level),
                        format="%(levelname)s: %(message)s")

    redact = redaction_enabled(no_redact_flag=args.no_redact)
    if redact:
        logging.info("Secret redaction is ENABLED (use --no-redact or K8STOOLS_REDACT=0 "
                     "to capture raw values). Redaction cannot be undone in the capture file.")
    else:
        logging.warning("Secret redaction is DISABLED - this capture may contain credentials")

    output = Path(args.output) if args.output else Path(
        f"k8s-state-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}.json")

    stats = CaptureStats()
    try:
        state = capture_state(namespaces=args.namespace,
                              include_logs=not args.no_logs,
                              max_log_lines=args.max_log_lines,
                              include_previous_logs=not args.no_previous_logs,
                              redact=redact,
                              stats=stats)
    except (k8s_tools.K8sConfigError, k8s_tools.K8sApiError) as e:
        print(f"Capture failed: {e}", file=sys.stderr)
        return 1

    write_state(state, output)

    print(stats.report())
    print(f"Wrote {output} ({output.stat().st_size:,} bytes, "
          f"{'redacted' if redact else 'NOT redacted'}).")
    return 0


if __name__ == '__main__':
    sys.exit(main())
