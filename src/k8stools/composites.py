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
"""Composite investigation tools (issue #13): one call for the picture an agent
otherwise assembles from five or six.

Both tools are built only from the existing tool functions, passed in as ``api``
(``k8s_tools`` or ``mock_tools``), so they answer the same way live and from a
replayed capture, and need nothing new in captures. They state facts and fixed
Kubernetes semantics - exit-code meanings, back-off, what an event window is -
and never a diagnosis: no ranked causes and no "OOM killed" where Kubernetes
didn't record one.

Restart cadence comes from container status, never from event counts: event
records lag what they count (client-go writes at most one update per object and
type every 5 minutes after a burst of 25), so ``count / window`` is an average
over the record, not the current rhythm.
"""

import datetime
import re
import signal
from typing import Any, Optional

from .k8s_tools import (ContainerEssentials, ContainerStateRunning, ContainerStateTerminated,
                        ContainerStateWaiting, EventLine, FailureGroup, Instance, K8sApiError,
                        LastChange, LOG_NOTE_PREFIX, LogTail, NamespaceHealth, Termination,
                        WorkloadHealth, WorkloadReport, _to_whole_seconds)


# --- fixed semantics ------------------------------------------------------------

_EXIT_MEANINGS = {
    0: "success",
    1: "general error (application-defined)",
    2: "misuse of a shell builtin / invalid arguments",
    126: "command found but not executable",
    127: "command not found",
}


def exit_meaning(code: Optional[int]) -> Optional[str]:
    """The fixed meaning of a container exit code: 128+n is "killed by signal n"."""
    if code is None:
        return None
    if code in _EXIT_MEANINGS:
        return _EXIT_MEANINGS[code]
    if 128 < code < 128 + 65:
        n = code - 128
        try:
            name = signal.Signals(n).name
        except ValueError:
            name = f"signal {n}"
        return f"killed by {name} (128+{n})"
    return "application-defined"


def _disagreement(code: Optional[int], reason: Optional[str]) -> Optional[str]:
    """A note when the exit code and Kubernetes' recorded reason tell different stories."""
    if code == 137 and reason and reason != "OOMKilled":
        return (f"Exit 137 is SIGKILL (128+9), but the recorded reason is {reason}, not "
                "OOMKilled: the runtime did not attribute this kill to the out-of-memory "
                "killer. A liveness-probe kill or an external SIGKILL also exits 137.")
    if reason == "OOMKilled" and code not in (None, 137):
        return f"Recorded reason OOMKilled, with exit code {code} rather than 137."
    return None


# --- gathering ----------------------------------------------------------------------

_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "Job")


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _workloads(api, namespace: str) -> list[tuple[str, str, int, int]]:
    """(kind, name, ready, desired) for each workload in the namespace."""
    out = []
    for d in api.get_deployment_summaries(namespace):
        out.append(("Deployment", d.name, d.ready_replicas, d.total_replicas))
    for s in api.get_statefulset_summaries(namespace):
        out.append(("StatefulSet", s.name, s.ready_replicas, s.total_replicas))
    for d in api.get_daemonset_summaries(namespace):
        out.append(("DaemonSet", d.name, d.number_ready, d.desired_number_scheduled))
    for j in api.get_job_summaries(namespace):
        # A Job is "ready" when it has succeeded; desired is 1 run.
        out.append(("Job", j.name, 1 if j.succeeded else 0, 1))
    return out


#: Generated pod-name suffixes: a ReplicaSet's, Job's or DaemonSet's pods are
#: "<name>-<5 chars>"; a StatefulSet's are "<name>-<ordinal>".
_RANDOM_SUFFIX = r"-[a-z0-9]{5}"
_ORDINAL_SUFFIX = r"-\d+"

NOTE_MATCHED_BY_NAME = (
    "Some pods were matched to their workloads by name (\"<replicaset>-<5 chars>\", "
    "\"<statefulset>-<n>\"), because they carry no owner: a capture taken before "
    "k8stools 2.3.0 recorded none.")


def _match_by_name(pod_name: str, rs_owner: dict[str, str],
                   workloads: list[tuple[str, str, int, int]]) -> Optional[tuple[str, str]]:
    """The workload a pod's generated name points to, for a pod with no owner."""
    for rs, deployment in rs_owner.items():
        if deployment and re.fullmatch(re.escape(rs) + _RANDOM_SUFFIX, pod_name):
            return ("Deployment", deployment)
    for kind, suffix in (("StatefulSet", _ORDINAL_SUFFIX), ("Job", _RANDOM_SUFFIX),
                         ("DaemonSet", _RANDOM_SUFFIX)):
        for k, name, _, _ in workloads:
            if k == kind and re.fullmatch(re.escape(name) + suffix, pod_name):
                return (kind, name)
    return None


def _pods_by_workload(api, namespace: str, workloads: list[tuple[str, str, int, int]]
                      ) -> tuple[dict[tuple[str, str], list], list, bool]:
    """Pods grouped by their workload; pods no workload owns; and whether any
    pod was matched by name.

    A pod's controlling owner (`PodSummary.owner`) links it. Captures taken
    before 2.3.0 have no owners, so a pod without one is matched by its
    generated name instead - otherwise every pod of an older capture would look
    ownerless and every workload would read zero restarts, which is wrong, not
    unknown.
    """
    rs_owner = {r.name: r.owner_deployment for r in api.get_replicaset_summaries(namespace)}
    grouped: dict[tuple[str, str], list] = {}
    standalone, by_name = [], False
    for pod in api.get_pod_summaries(namespace):
        kind, _, name = (pod.owner or "").partition("/")
        if kind == "ReplicaSet" and rs_owner.get(name):
            key = ("Deployment", rs_owner[name])
        elif kind in _KINDS:
            key = (kind, name)
        elif pod.owner is None and (key := _match_by_name(pod.name, rs_owner, workloads)):
            by_name = True
        else:
            standalone.append(pod)
            continue
        grouped.setdefault(key, []).append(pod)
    return grouped, standalone, by_name


def _termination(pod: str, status, now: datetime.datetime) -> Optional[Termination]:
    """The container's last termination: the current state if it's terminated
    (a Job's container), else last_state."""
    term = status.state if isinstance(status.state, ContainerStateTerminated) \
        else status.last_state if isinstance(status.last_state, ContainerStateTerminated) \
        else None
    if term is None:
        return None
    t = Termination(pod=pod, container=status.container_name, exit_code=term.exit_code,
                    exit_meaning=exit_meaning(term.exit_code), reason=term.reason,
                    instance_lifetime=term.ran_for)
    if term.finished_at:
        t.finished = _to_whole_seconds(now - term.finished_at)
        if isinstance(status.state, ContainerStateRunning) and term is status.last_state:
            t.restart_gap = _to_whole_seconds(status.state.started_at - term.finished_at)
        elif isinstance(status.state, ContainerStateWaiting):
            t.restart_gap = t.finished
    if isinstance(status.state, ContainerStateWaiting):
        t.waiting = status.state.reason
    note = _disagreement(term.exit_code, term.reason)
    if note:
        t.notes.append(note)
    return t


def _latest(terminations: list[Termination]) -> Optional[Termination]:
    done = [t for t in terminations if t.finished is not None]
    return min(done, key=lambda t: t.finished) if done else None


def _memory_shape(statuses) -> Optional[str]:
    shapes = []
    for s in statuses:
        limit, request = s.resource_limits.get("memory"), s.resource_requests.get("memory")
        if limit is None:
            shape = "no limit"
        elif limit == request:
            shape = f"limit {limit} = request"
        else:
            shape = f"limit {limit}, request {request or 'none'}"
        shapes.append((s.container_name, shape))
    if len({shape for _, shape in shapes}) == 1:
        return shapes[0][1]  # every container the same: say it once
    return "; ".join(f"{name}: {shape}" for name, shape in shapes) or None


def _template_changed(api, kind: str, name: str, namespace: str) -> Optional[datetime.timedelta]:
    if kind == "Job":
        return None
    try:
        history = api.get_workload_history(name, namespace, kind)
    except K8sApiError:
        return None
    current = [r for r in history.revisions if r.current]
    return current[0].age if current else None


def _lifetimes(terminations: list[Termination]) -> str:
    """The range of instance lifetimes in a group, e.g. "lifetimes 2s-20s"."""
    known = sorted(t.instance_lifetime for t in terminations if t.instance_lifetime is not None)
    if not known:
        return "lifetimes unknown"
    low, high = _human(known[0]), _human(known[-1])
    return f"lifetime {low}" if low == high else f"lifetimes {low}-{high}"


# --- get_namespace_health -------------------------------------------------------------

def namespace_health(api, namespace: str = "default") -> NamespaceHealth:
    now = _now()
    workloads = _workloads(api, namespace)
    grouped, standalone, by_name = _pods_by_workload(api, namespace, workloads)
    entries: list[WorkloadHealth] = []
    healthy_lines: list[str] = []

    def assess(label, kind, name, ready, desired, pods):
        terminations, first_pod, waiting = [], [], False
        for i, pod in enumerate(pods):
            for s in api.get_pod_container_statuses(pod.name, namespace):
                if i == 0:
                    first_pod.append(s)
                waiting |= isinstance(s.state, ContainerStateWaiting)
                t = _termination(pod.name, s, now)
                if t:
                    terminations.append(t)
        last = _latest(terminations)
        failed_job = kind == "Job" and last is not None and last.exit_code not in (0, None)
        restarts = sum(p.restarts for p in pods) if pods or not desired else None
        if ready >= desired and not waiting and not failed_job:
            healthy_lines.append(f"{label} {ready}/{desired}"
                                 + (f", {restarts} restarts" if restarts else ""))
            return
        w = WorkloadHealth(
            workload=label, ready=ready, desired=desired, restarts=restarts,
            last_termination=last, memory=_memory_shape(first_pod),
            template_changed=_template_changed(api, kind, name, namespace) if kind else None)
        if restarts is None:
            w.notes.append("None of its pods could be found, so its restarts and "
                           "terminations are unknown.")
        if last:
            w.notes.extend(last.notes)
            last.notes = []
        entries.append(w)

    for kind, name, ready, desired in workloads:
        assess(f"{kind}/{name}", kind, name, ready, desired, grouped.get((kind, name), []))
    for pod in standalone:
        assess(f"Pod/{pod.name}", None, pod.name, pod.ready_containers,
               pod.total_containers, [pod])

    entries.sort(key=lambda w: w.workload)
    healthy_lines.sort()
    # Grouped by what a termination reliably repeats: exit code, reason and memory
    # shape. Not by lifetime: one workload's instances live 7s, 20s and 2m on
    # successive restarts, so fixed bands split the same failure arbitrarily. The
    # lifetimes are shown instead.
    groups: dict[tuple, list[WorkloadHealth]] = {}
    for w in entries:
        t = w.last_termination
        if t is None:
            continue
        groups.setdefault((t.exit_code, t.exit_meaning, t.reason, w.memory), []).append(w)
    failures = []
    for (code, meaning, reason, memory), members in groups.items():
        if len(members) > 1:
            failures.append(FailureGroup(
                signature=(f"exit {code} ({meaning}), reason {reason}, memory "
                           f"{memory or 'unknown'}, "
                           f"{_lifetimes([m.last_termination for m in members])}"),
                workloads=[m.workload for m in members]))
    result = NamespaceHealth(
        namespace=namespace, workloads=entries, healthy=healthy_lines,
        common_failures=failures)
    if by_name:
        result.notes.append(NOTE_MATCHED_BY_NAME)
    if any(w.last_termination for w in entries):
        result.notes.append(
            "instance_lifetime is how long the last instance ran; restart_gap is the "
            "back-off before the next start (or the wait so far). Restarts are "
            "lifetime + gap apart.")
    return result


# --- get_workload_report -----------------------------------------------------------------

_MAX_LOG_LINES = 100
_MAX_LINE_CHARS = 300
_MAX_EVENTS = 20


def _find_kind(api, name: str, namespace: str) -> str:
    found = [kind for kind, n, _, _ in _workloads(api, namespace) if n == name]
    if not found:
        raise K8sApiError(f"No Deployment, StatefulSet, DaemonSet or Job named '{name}' "
                          f"in namespace '{namespace}'.")
    if len(found) > 1:
        raise K8sApiError(f"'{name}' names a {' and a '.join(found)} in namespace "
                          f"'{namespace}'; pass kind.")
    return found[0]


def _get(d: dict, *keys):
    for k in keys:
        if d.get(k) is not None:
            return d[k]
    return None


def _probe_summary(probe: dict) -> str:
    http = _get(probe, "http_get", "httpGet")
    tcp = _get(probe, "tcp_socket", "tcpSocket")
    cmd = _get(probe, "_exec", "exec")
    grpc = _get(probe, "grpc")
    if http:
        how = f"httpGet :{_get(http, 'port')}{_get(http, 'path') or ''}"
    elif tcp:
        how = f"tcpSocket :{_get(tcp, 'port')}"
    elif cmd:
        how = "exec " + " ".join(str(c) for c in (_get(cmd, "command") or []))[:80]
    elif grpc:
        how = f"grpc :{_get(grpc, 'port')}"
    else:
        how = "probe"
    period = _get(probe, "period_seconds", "periodSeconds") or 10
    failures = _get(probe, "failure_threshold", "failureThreshold") or 3
    delay = _get(probe, "initial_delay_seconds", "initialDelaySeconds")
    text = f"{how} every {period}s, fails after {failures}"
    return text + (f", starts after {delay}s" if delay else "")


def _essentials(spec: dict, statuses) -> list[ContainerEssentials]:
    by_name = {s.container_name: s for s in statuses}
    out = []
    for c in spec.get("containers") or []:
        status = by_name.get(c.get("name"))
        resources = c.get("resources") or {}
        probes = []
        for kind in ("liveness", "readiness", "startup"):
            probe = _get(c, f"{kind}_probe", f"{kind}Probe")
            if probe:
                probes.append(f"{kind}: {_probe_summary(probe)}")
        out.append(ContainerEssentials(
            container=c.get("name"), image=c.get("image") or (status.image if status else ""),
            requests=dict(resources.get("requests") or {}),
            limits=dict(resources.get("limits") or {}),
            probes=probes or ["none configured"]))
    return out


def _state_text(status, now) -> str:
    st = status.state
    if isinstance(st, ContainerStateRunning):
        return "Running"
    if isinstance(st, ContainerStateWaiting):
        return f"Waiting ({st.reason})"
    if isinstance(st, ContainerStateTerminated):
        return f"Terminated ({st.reason})"
    return "Unknown"


def _events(api, namespace: str, names: list[str], kinds: set[str]) -> list[EventLine]:
    """Events for the named objects of the given kinds, merged where they differ
    only in which object they name (the same event recorded for each pod)."""
    merged: dict[tuple, EventLine] = {}
    name_rx = re.compile(r"(?<![\w.-])(" + "|".join(map(re.escape, sorted(names, key=len, reverse=True)))
                         + r")(?![\w.-])")
    for name in names:
        for e in api.get_events(namespace=namespace, involved_name=name):
            if e.object.partition("/")[0] not in kinds:
                continue  # e.g. a Service that shares the workload's name
            message = name_rx.sub("<name>", e.message)
            key = (e.type, e.reason, message[:200])
            line = merged.get(key)
            if line is None:
                merged[key] = EventLine(type=e.type, reason=e.reason, message=message[:200],
                                        count=e.count, first_seen=e.first_seen,
                                        last_seen=e.last_seen)
                continue
            line.objects += 1
            if e.count is not None:
                line.count = (line.count or 0) + e.count
            if e.first_seen is not None:
                line.first_seen = max(filter(None, (line.first_seen, e.first_seen)))
            if e.last_seen is not None:
                line.last_seen = min(filter(None, (line.last_seen, e.last_seen)))
    lines = sorted(merged.values(),
                   key=lambda e: e.last_seen if e.last_seen is not None else datetime.timedelta.max)
    return lines[:_MAX_EVENTS]


def _tail(api, pod: str, namespace: str, container: str, previous: bool,
          lines: int, grep: Optional[str]) -> Optional[LogTail]:
    try:
        text = api.get_logs_for_pod_and_container(pod, namespace, container, previous=previous)
    except K8sApiError:
        return None  # e.g. no previous instance
    notes, body = [], []
    for line in (text or "").splitlines():
        if line.startswith(LOG_NOTE_PREFIX):
            notes.append(line[len(LOG_NOTE_PREFIX):])
        else:
            body.append(line)
    if grep:
        try:
            rx = re.compile(grep, re.IGNORECASE)
            body = [l for l in body if rx.search(l)]
        except re.error:
            body = [l for l in body if grep.lower() in l.lower()]
    body = [l if len(l) <= _MAX_LINE_CHARS else l[:_MAX_LINE_CHARS] + " ..."
            for l in body[-lines:]]
    return LogTail(pod=pod, container=container, previous=previous, lines=body, notes=notes)


def workload_report(api, name: str, namespace: str = "default", kind: Optional[str] = None,
                    log_lines: int = 20, grep: Optional[str] = None) -> WorkloadReport:
    now = _now()
    if kind is not None and kind not in _KINDS:
        raise K8sApiError(f"Unsupported kind '{kind}': expected one of {', '.join(_KINDS)}.")
    kind = kind or _find_kind(api, name, namespace)
    workloads = _workloads(api, namespace)
    matches = [(r, d) for k, n, r, d in workloads if (k, n) == (kind, name)]
    if not matches:
        raise K8sApiError(f"{kind} '{name}' not found in namespace '{namespace}'.")
    ready, desired = matches[0]
    grouped, _, by_name = _pods_by_workload(api, namespace, workloads)
    pods = grouped.get((kind, name), [])
    report = WorkloadReport(workload=f"{kind}/{name}", ready=ready, desired=desired)
    if by_name and pods and all(p.owner is None for p in pods):
        report.notes.append(NOTE_MATCHED_BY_NAME)

    statuses_by_pod = {p.name: api.get_pod_container_statuses(p.name, namespace) for p in pods}
    for pod in pods:
        for s in statuses_by_pod[pod.name]:
            started = s.state.started_at if isinstance(s.state, ContainerStateRunning) else None
            termination = _termination(pod.name, s, now)
            if termination:
                termination.pod = termination.container = None  # the instance names them
            report.instances.append(Instance(
                pod=pod.name, container=s.container_name, state=_state_text(s, now),
                started=_to_whole_seconds(now - started) if started else None,
                ready=s.ready, restarts=s.restart_count, last_termination=termination))
    if pods:
        # The pod most in trouble speaks for the workload: waiting, then most restarts.
        focus = max(pods, key=lambda p: (any(isinstance(s.state, ContainerStateWaiting)
                                              for s in statuses_by_pod[p.name]), p.restarts))
        report.containers = _essentials(api.get_pod_spec(focus.name, namespace),
                                        statuses_by_pod[focus.name])
        lines = max(1, min(log_lines, _MAX_LOG_LINES))
        for s in statuses_by_pod[focus.name]:
            for previous in (False, True):
                if previous and not s.restart_count:
                    continue
                tail = _tail(api, focus.name, namespace, s.container_name, previous, lines, grep)
                if tail:
                    report.logs.append(tail)
        if len(pods) > 1:
            report.notes.append(f"containers and logs are from {focus.name}, of {len(pods)} pods.")
    else:
        report.notes.append("No pods: spec, instances and logs are unavailable.")

    replicasets = [r.name for r in api.get_replicaset_summaries(namespace)
                   if r.owner_deployment == name] if kind == "Deployment" else []
    report.events = _events(api, namespace, [name] + replicasets + [p.name for p in pods],
                            {kind, "ReplicaSet", "Pod"})
    oldest = max((e.first_seen for e in report.events if e.first_seen is not None), default=None)
    if oldest is not None:
        report.notes.append(
            f"Event records start {_human(oldest)} ago: that is where the records begin "
            "(they expire about an hour after their last update), not necessarily when "
            "the problem did.")

    if kind != "Job":
        history = api.get_workload_history(name, namespace, kind)
        current = next((r for r in history.revisions if r.current), None)
        if current:
            report.last_change = LastChange(
                revision=current.revision, age=current.age, reused=current.reused,
                rollout_restart=current.rollout_restart,
                changes=[_change_text(c) for c in current.changes])
            report.notes.append(
                f"The pod template last changed {_human(current.age)} ago. That is when "
                "it changed, not how long the workload has been healthy.")
        report.config = history.config

    if any(i.last_termination for i in report.instances):
        report.notes.append(
            "instance_lifetime is how long an instance ran; restart_gap is the back-off "
            "before the next start (or the wait so far). Restarts are lifetime + gap apart.")
    for i in report.instances:
        if i.last_termination:
            report.notes.extend(n for n in i.last_termination.notes if n not in report.notes)
            i.last_termination.notes = []
    return report


def _change_text(c) -> str:
    if c.before is None and c.after is None:
        return f"{c.field}: {c.change}"
    return f"{c.field}: {c.before or '-'} -> {c.after or '-'}"


def _human(td: datetime.timedelta) -> str:
    seconds = int(td.total_seconds())
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{seconds}s"
    return f"{seconds}s"
