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
import functools
import re
import signal
from typing import Any, Optional

from .k8s_tools import (ContainerEssentials, ContainerUsage, EndpointSummary, HpaSummary,
                        IngressSummary, NetworkPolicySummary, NOTE_NETWORK_POLICIES,
                        allows_port, network_policy_effect,
                        K8sMetricsUnavailable, NOTE_WORKING_SET, ContainerStateRunning, ContainerStateTerminated,
                        ContainerStateWaiting, EventLine, FailureGroup, Instance, K8sApiError,
                        K8sForbidden, K8sLogReclaimed, K8sNotRecorded, LastChange, LOG_NOTE_PREFIX, LOG_RECLAIMED_REASON,
                        LOG_LINE_CHARS, LogTail, NamespaceHealth, NOTE_INIT_NOT_RECORDED, NOTE_INIT_PENDING,
                        NOTE_RECOVERING, NOTE_RESTART_HISTORY, PVCSummary,
                        Termination, WorkloadHealth, WorkloadReport, _clip, _init_completed, _line_time,
                        _now, _to_whole_seconds)


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


#: The kind each reader returns, for naming one the account may not read.
_READER_KINDS = {
    "get_deployment_summaries": "Deployments", "get_statefulset_summaries": "StatefulSets",
    "get_daemonset_summaries": "DaemonSets", "get_job_summaries": "Jobs",
    "get_replicaset_summaries": "ReplicaSets", "get_pvc_summaries": "PersistentVolumeClaims",
    "get_hpa_summaries": "HorizontalPodAutoscalers", "get_endpoint_summaries": "Service endpoints",
    "get_ingress_summaries": "Ingresses", "get_networkpolicy_summaries": "NetworkPolicies",
    "get_events": "events", "get_workload_history": "workload history",
}


class _Unknown(str):
    """A kind in ``unrecorded``: equal to its name, shown with why it's unknown.
    ``forbidden`` marks a live read the account isn't allowed (#33); the rest
    are a replayed capture's."""

    def __new__(cls, what: str, shown: Optional[str] = None, forbidden: bool = False):
        self = super().__new__(cls, what)
        self.shown, self.forbidden = shown or what, forbidden
        return self


def _recorded(unrecorded: Optional[list[str]], read, *args, default=()):
    """read(*args), or ``default`` when that kind is unknown, which is then
    named in ``unrecorded`` (for the notes): a replayed capture doesn't record
    it, or the account may not read it (a 403, live). Unknown is never none."""
    try:
        return read(*args)
    except K8sNotRecorded as e:
        if unrecorded is not None and e.what and e.what not in unrecorded:
            unrecorded.append(_Unknown(e.what, f"{e.what} ({e.reason})" if e.reason else None))
        return default
    except K8sForbidden:
        what = _READER_KINDS.get(getattr(read, "__name__", ""))
        if what is None:
            raise
        if unrecorded is not None and what not in unrecorded:
            unrecorded.append(_Unknown(what, forbidden=True))
        return default


def _not_recorded_notes(unrecorded: list[str]) -> list[str]:
    forbidden = [u for u in unrecorded if getattr(u, "forbidden", False)]
    missing = [getattr(u, "shown", u) for u in unrecorded if u not in forbidden]
    notes = []
    if missing:
        notes.append("Not recorded in this capture, so unknown rather than absent: "
                     + ", ".join(missing) + ".")
    if forbidden:
        notes.append("Not readable with these credentials (403 Forbidden), so unknown "
                     "rather than absent: " + ", ".join(forbidden) + ".")
    return notes


def _workloads(api, namespace: str, unrecorded: Optional[list[str]] = None
               ) -> list[tuple[str, str, int, int, list[str]]]:
    """(kind, name, ready, desired, failing conditions) for each workload in the
    namespace."""
    out = []
    for d in _recorded(unrecorded, api.get_deployment_summaries, namespace):
        out.append(("Deployment", d.name, d.ready_replicas, d.total_replicas,
                     _failing_conditions(d.conditions or [])))
    for s in _recorded(unrecorded, api.get_statefulset_summaries, namespace):
        out.append(("StatefulSet", s.name, s.ready_replicas, s.total_replicas, []))
    for d in _recorded(unrecorded, api.get_daemonset_summaries, namespace):
        out.append(("DaemonSet", d.name, d.number_ready, d.desired_number_scheduled, []))
    for j in _recorded(unrecorded, api.get_job_summaries, namespace):
        # A Job is "ready" when it has succeeded; desired is 1 run.
        out.append(("Job", j.name, 1 if j.succeeded else 0, 1, []))
    return out


#: Deployment conditions in the state that means a rollout can't make progress:
#: past its progress deadline, or unable to create pods (quota, admission).
_FAILING_CONDITIONS = {("Progressing", "False"), ("ReplicaFailure", "True")}


def _failing_conditions(conditions) -> list[str]:
    """A Deployment's failing conditions in its controller's words, e.g.
    'Progressing=False (ProgressDeadlineExceeded): ReplicaSet "x" has timed out
    progressing.'"""
    return [f"{c.type}={c.status}" + (f" ({c.reason})" if c.reason else "")
            + (f": {c.message[:200]}" if c.message else "")
            for c in conditions if (c.type, c.status) in _FAILING_CONDITIONS]


#: WorkloadHealth.unscheduled when a capture recorded no reason (before 4.1.0).
_REASON_NOT_RECORDED = "reason not recorded (the capture predates k8stools 4.1.0)"


def _unscheduled(pod, statuses) -> Optional[str]:
    """Why a pod isn't scheduled, or None if it is.

    A pod is unscheduled when it has no node. Captures before 4.1.0 recorded no
    reason; there, a pod with no node and no container statuses at all hasn't
    been scheduled either (a scheduled pod's kubelet reports its containers).
    """
    if pod.node is not None or (pod.unscheduled is None
                                and (statuses.containers or statuses.init_containers)):
        return None
    return pod.unscheduled or _REASON_NOT_RECORDED


def _unscheduled_line(pod, reason: str) -> str:
    return f"{pod.name}: not scheduled for {_human(pod.age)}: {reason}"


def _claim_line(c: PVCSummary) -> str:
    """An unbound claim and why it waits, in its newest event's words, e.g.
    'PersistentVolumeClaim/cache: Pending, storage class fast-ssd; ProvisioningFailed:
    storageclass.storage.k8s.io "fast-ssd" not found'."""
    line = f"PersistentVolumeClaim/{c.name}: {c.status}" + (
        f", storage class {c.storage_class}" if c.storage_class else "")
    if not c.events:
        return line + "; no events recorded (events expire, by default after an hour)"
    e = c.events[0]
    return line + f"; {e.reason}: {e.message[:200]}"


def _unbound_claims(api, namespace: str, unrecorded: list[str]) -> dict[str, list[str]]:
    """The unbound claims each pod mounts, as _claim_line()s, by pod name.

    The scheduler says only that a pod has unbound claims; why a claim is
    unbound is in the claim's events. Best-effort: the claims are context for
    a pod already reported unscheduled.
    """
    try:
        claims = _recorded(unrecorded, api.get_pvc_summaries, namespace)
    except K8sApiError:
        return {}
    by_pod: dict[str, list[str]] = {}
    for c in claims:
        if c.status != "Bound":
            for pod in c.mounted_by:
                by_pod.setdefault(pod, []).append(_claim_line(c))
    return by_pod


NOTE_PLACEMENT = (
    "placement is k8stools' own evaluation of each pod's nodeSelector, required node "
    "affinity and tolerations against the nodes as read, not the scheduler's. The "
    "scheduler names only the first filter that rejected each node; resources, ports, "
    "volumes, topology spread and pod (anti-)affinity aren't in the count.")

#: Taint effects that keep a pod off a node (PreferNoSchedule only discourages).
_REPELLING_EFFECTS = ("NoSchedule", "NoExecute")


def _matches_requirement(req: dict, value: Optional[str]) -> bool:
    """A node selector requirement against one label (or field) value, None if
    the node lacks it. Gt and Lt compare integers; a value that isn't one
    doesn't match, as in the scheduler."""
    op, values = req.get("operator"), req.get("values") or []
    if op == "In":
        return value is not None and value in values
    if op == "NotIn":
        return value is None or value not in values
    if op == "Exists":
        return value is not None
    if op == "DoesNotExist":
        return value is None
    if op in ("Gt", "Lt"):
        try:
            have, bound = int(value), int(values[0])
        except (TypeError, ValueError, IndexError):
            return False
        return have > bound if op == "Gt" else have < bound
    return False


def _matches_term(term: dict, node) -> bool:
    """A nodeSelectorTerm: its expressions and fields ANDed. An empty term
    matches no node."""
    exprs = term.get("match_expressions") or []
    fields = term.get("match_fields") or []
    if not exprs and not fields:
        return False
    return (all(_matches_requirement(r, node.labels.get(r.get("key"))) for r in exprs)
            and all(_matches_requirement(
                r, node.name if r.get("key") == "metadata.name" else None) for r in fields))


def _required_affinity(spec: dict) -> Optional[dict]:
    return (((spec.get("affinity") or {}).get("node_affinity") or {})
            .get("required_during_scheduling_ignored_during_execution"))


def _admits(spec: dict, node) -> bool:
    """Whether a pod's nodeSelector and required node affinity admit a node."""
    if any(node.labels.get(k) != v for k, v in (spec.get("node_selector") or {}).items()):
        return False
    required = _required_affinity(spec)
    if required is None:
        return True
    return any(_matches_term(t, node) for t in required.get("node_selector_terms") or [])


def _tolerates(tolerations: list[dict], taint: str) -> bool:
    """Whether a toleration matches a taint as NodeSummary writes it ("key=value:Effect")."""
    rest, _, effect = taint.rpartition(":")
    key, _, value = rest.partition("=")
    for t in tolerations:
        if t.get("effect") and t["effect"] != effect:
            continue
        if t.get("key") and t["key"] != key:
            continue
        if t.get("operator") == "Exists" or (t.get("value") or "") == value:
            return True
    return False


def _repelled(spec: dict, node) -> bool:
    """Whether a node has a NoSchedule or NoExecute taint the pod doesn't tolerate."""
    tolerations = spec.get("tolerations") or []
    return any(t.rpartition(":")[2] in _REPELLING_EFFECTS and not _tolerates(tolerations, t)
               for t in node.taints)


def _nodes(api):
    """The nodes, or the reason they can't be read. Best-effort: placement is
    context for a pod already reported unscheduled."""
    try:
        return api.get_node_summaries(), None
    except K8sApiError as e:
        return None, str(e)[:200]


def _placement_line(api, pod, namespace: str, nodes) -> str:
    """How many nodes an unscheduled pod's nodeSelector and required node affinity
    admit, and how many of those repel it with a taint, e.g. "ad-x: 0 of 5 nodes
    match its nodeSelector and required node affinity"."""
    nodes, error = nodes
    if error:
        return f"{pod.name}: not evaluated, nodes could not be read: {error}"
    if not nodes:
        return f"{pod.name}: not evaluated, no nodes were read"
    try:
        spec = api.get_pod_spec(pod.name, namespace)
    except K8sApiError as e:
        return f"{pod.name}: not evaluated, its spec could not be read: {str(e)[:200]}"
    admitted = [n for n in nodes if _admits(spec, n)]
    if spec.get("node_selector") or _required_affinity(spec) is not None:
        line = (f"{pod.name}: {len(admitted)} of {len(nodes)} nodes match its "
                "nodeSelector and required node affinity")
    else:
        line = f"{pod.name}: no nodeSelector or required node affinity, so all nodes match ({len(nodes)})"
    if admitted:
        repelled = sum(_repelled(spec, n) for n in admitted)
        line += (f"; of those, {repelled} {'has' if repelled == 1 else 'have'} a "
                 "NoSchedule or NoExecute taint it doesn't tolerate")
    return line


#: Generated pod-name suffixes: a ReplicaSet's, Job's or DaemonSet's pods are
#: "<name>-<5 chars>"; a StatefulSet's are "<name>-<ordinal>".
_RANDOM_SUFFIX = r"-[a-z0-9]{5}"
_ORDINAL_SUFFIX = r"-\d+"

NOTE_MATCHED_BY_NAME = (
    "Some pods were matched to their workloads by name (\"<replicaset>-<5 chars>\", "
    "\"<statefulset>-<n>\"), because they carry no owner: a capture taken before "
    "k8stools 2.3.0 recorded none.")


def _match_by_name(pod_name: str, rs_owner: dict[str, str],
                   workloads: list[tuple]) -> Optional[tuple[str, str]]:
    """The workload a pod's generated name points to, for a pod with no owner."""
    for rs, deployment in rs_owner.items():
        if deployment and re.fullmatch(re.escape(rs) + _RANDOM_SUFFIX, pod_name):
            return ("Deployment", deployment)
    for kind, suffix in (("StatefulSet", _ORDINAL_SUFFIX), ("Job", _RANDOM_SUFFIX),
                         ("DaemonSet", _RANDOM_SUFFIX)):
        for k, name, *_ in workloads:
            if k == kind and re.fullmatch(re.escape(name) + suffix, pod_name):
                return (kind, name)
    return None


def _pods_by_workload(api, namespace: str, workloads: list[tuple],
                      unrecorded: Optional[list[str]] = None
                      ) -> tuple[dict[tuple[str, str], list], list, bool]:
    """Pods grouped by their workload; pods no workload owns; and whether any
    pod was matched by name.

    A pod's controlling owner (`PodSummary.owner`) links it. Captures taken
    before 2.3.0 have no owners, so a pod without one is matched by its
    generated name instead - otherwise every pod of an older capture would look
    ownerless and every workload would read zero restarts, which is wrong, not
    unknown.
    """
    rs_owner = {r.name: r.owner_deployment
                for r in _recorded(unrecorded, api.get_replicaset_summaries, namespace)}
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


def _autoscalers(api, namespace: str, unrecorded: list[str]) -> dict[str, HpaSummary]:
    """HPAs by the "Kind/name" they scale."""
    return {h.scale_target: h for h in _recorded(unrecorded, api.get_hpa_summaries, namespace)}


def _endpoint_line(e: EndpointSummary) -> str:
    return (f"Service/{e.service}: {e.ready} ready, {e.not_ready} not ready"
            + (f", {e.terminating} terminating" if e.terminating else ""))


def _services_for(endpoints: list[EndpointSummary], pod_names: set[str]) -> list[EndpointSummary]:
    """Services whose endpoints include any of these pods. Pods carry no labels
    in these tools, so a Service's selector can't be matched directly; its
    endpoints name the pods it selected, ready or not."""
    return [e for e in endpoints if any(a.pod in pod_names for a in e.addresses)]


#: A healthy workload this close to a memory limit is worth a mention.
_NEAR_LIMIT_PERCENT = 90.0
#: A ready workload with a restart more recent than this is recovering, not healthy.
_RECOVERING_WINDOW = datetime.timedelta(hours=24)
_MAX_RECOVERING = 10


def _usage(api, namespace: str) -> tuple[Optional[list[ContainerUsage]], Optional[str]]:
    """Container usage, or (None, why not) when metrics aren't available or
    the account may not read them."""
    try:
        return api.get_container_metrics(namespace), None
    except (K8sMetricsUnavailable, K8sForbidden) as e:
        return None, str(e)


def _usage_line(u: ContainerUsage) -> str:
    if u.memory_bytes is None and u.cpu_millicores is None:
        return f"{u.container}: no reading"
    memory = f"memory {u.memory}"
    if u.memory_limit_bytes:
        memory += f" of {_show_memory(u.memory_limit_bytes)} ({u.memory_percent_of_limit}%)"
    return f"{u.container}: {memory}, cpu {u.cpu}"


def _ingress_lines(ingresses: list[IngressSummary], services: Optional[set[str]] = None,
                   problems_only: bool = False) -> list[str]:
    """One line per rule: "Ingress/shop: host/path -> service:port (3 ready)", with
    its problem; only rules routing to `services`, or only rules that can't
    reach a pod."""
    lines = []
    for ing in ingresses:
        for r in ing.rules + ([ing.default_backend] if ing.default_backend else []):
            if services is not None and r.service not in services:
                continue
            broken = r.backend_problem or r.backend_ready == 0
            if problems_only and not broken:
                continue
            where = f"{r.host or '*'}{r.path or ''}" if (r.host or r.path) else "(default)"
            target = f"{r.service}:{r.port}" if r.service else (r.resource or "-")
            state = (f": {r.backend_problem}" if r.backend_problem
                     else f" ({r.backend_ready} ready)" if r.backend_ready is not None else "")
            lines.append(f"Ingress/{ing.name}: {where} -> {target}{state}")
    return lines


_MAX_POLICY_LINES = 10


def _pods_text(names: list[str]) -> str:
    if not names:
        return "no pod"
    shown = ", ".join(names[:3]) + (f", +{len(names) - 3}" if len(names) > 3 else "")
    return f"{len(names)} pod{'' if len(names) == 1 else 's'} ({shown})"


def _policy_lines(policies: list[NetworkPolicySummary]) -> list[str]:
    """One line per policy: what it selects and what it allows."""
    lines = [f"NetworkPolicy/{p.name} selects {_pods_text(p.selected_pods)}: "
             + "; ".join(filter(None, (p.ingress_effect, p.egress_effect)))
             for p in sorted(policies, key=lambda p: p.name)]
    if len(lines) > _MAX_POLICY_LINES:
        lines[_MAX_POLICY_LINES:] = [f"and {len(lines) - _MAX_POLICY_LINES} more: "
                                     "see get_networkpolicy_summaries"]
    return lines


def _workload_policy_lines(policies: list[NetworkPolicySummary], pod_names: set[str]) -> list[str]:
    """The policies selecting these pods, combined per direction: a pod's allowed
    traffic is the union of every policy selecting it for that direction."""
    selecting = [p for p in policies if pod_names & set(p.selected_pods)]
    if not selecting:
        return ["no NetworkPolicy (networking.k8s.io) selects these pods"]
    lines = []
    for direction in ("Ingress", "Egress"):
        word = direction.lower()
        these = [p for p in selecting if direction in p.policy_types]
        if not these:
            lines.append(f"{word}: not restricted (no NetworkPolicy selects these pods for {word})")
            continue
        rules = [r for p in these for r in (p.ingress if direction == "Ingress" else p.egress)]
        effect = network_policy_effect(direction, rules)
        if direction == "Egress" and allows_port(rules, 53) is False:
            effect += "; none allows port 53, so these pods can't look up names (DNS)"
        lines.append(f"{word}: {effect} ("
                     + ", ".join(f"NetworkPolicy/{p.name}" for p in these) + ")")
    for p in selecting:
        n = len(pod_names & set(p.selected_pods))
        if n < len(pod_names):
            lines.append(f"NetworkPolicy/{p.name} selects {n} of these {len(pod_names)} pods")
    return lines


def _policy_kinds(api) -> list[str]:
    """Policy kinds installed as custom resources (Cilium, Calico, Antrea,
    AdminNetworkPolicy): they apply too, and no tool here reads them."""
    try:
        crds = api.get_custom_resource_definitions()
    except K8sApiError:  # including not recorded: the note is only ever additive
        return []
    return [f"{c.kind} ({c.group})" for c in crds if c.plural.endswith("networkpolicies")]


def _network_policies(api, namespace: str, unrecorded: list[str]
                      ) -> tuple[Optional[list[NetworkPolicySummary]], Optional[str]]:
    """The namespace's policies, or None and why: not recorded (named in
    ``unrecorded``) or not readable (e.g. a role without the permission)."""
    try:
        return _recorded(unrecorded, api.get_networkpolicy_summaries, namespace, default=None), None
    except K8sApiError as e:
        return None, f"NetworkPolicies unavailable: {e}"


def _autoscaler_line(h: HpaSummary) -> str:
    at_max = h.current_replicas >= h.max_replicas
    line = (f"HPA {h.name}: {h.current_replicas} replicas (min "
            f"{h.min_replicas if h.min_replicas is not None else 1}, max {h.max_replicas})"
            + (", at max" if at_max else ""))
    readings = [f"{m.name} {m.current or 'unknown'}/{m.target}" for m in h.metrics]
    inactive = [c for c in h.conditions if c.type == "ScalingActive" and c.status == "False"]
    if inactive:
        readings.append(f"not scaling: {inactive[0].reason}")
    return line + ("; " + ", ".join(readings) if readings else "")


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
    unrecorded: list[str] = []
    workloads = _workloads(api, namespace, unrecorded)
    grouped, standalone, by_name = _pods_by_workload(api, namespace, workloads, unrecorded)
    autoscalers = _autoscalers(api, namespace, unrecorded)
    endpoints = _recorded(unrecorded, api.get_endpoint_summaries, namespace)
    usage, usage_unavailable = _usage(api, namespace)
    entries: list[WorkloadHealth] = []
    recovering: list[WorkloadHealth] = []
    healthy_lines: list[str] = []
    unschedulable: dict[str, list[str]] = {}  # workloads by why their pods aren't scheduled
    # Read only when some pod is unscheduled, and then once.
    unbound_claims = functools.cache(lambda: _unbound_claims(api, namespace, unrecorded))
    nodes = functools.cache(lambda: _nodes(api))

    def assess(label, kind, name, ready, desired, pods, conditions):
        terminations, first_pod, waiting, init_pending, unscheduled = [], [], False, [], {}
        claims: dict[str, None] = {}
        placement = []
        for i, pod in enumerate(pods):
            statuses = api.get_pod_container_statuses(pod.name, namespace)
            # A pod with no node has no containers to wait on: without this, the
            # old pods of a rollout that can't schedule its new one read healthy.
            if reason := _unscheduled(pod, statuses):
                unscheduled[_unscheduled_line(pod, reason)] = reason
                claims.update(dict.fromkeys(unbound_claims().get(pod.name, [])))
                placement.append(_placement_line(api, pod, namespace, nodes()))
            for s in statuses.containers:
                if i == 0:
                    first_pod.append(s)
                waiting |= isinstance(s.state, ContainerStateWaiting)
                t = _termination(pod.name, s, now)
                if t:
                    terminations.append(t)
            # A completed init container's exit 0 is not a termination to report;
            # one that hasn't completed holds the app containers back.
            for s in statuses.init_containers or []:
                if _init_completed(s):
                    continue
                waiting = True
                init_pending.append(f"Init container '{s.container_name}': {_state_text(s, now)}")
                t = _termination(pod.name, s, now)
                if t:
                    terminations.append(t)
        last = _latest(terminations)
        failed_job = kind == "Job" and last is not None and last.exit_code not in (0, None)
        restarts = sum(p.restarts for p in pods) if pods or not desired else None
        hpa = autoscalers.get(label)
        pod_names = {p.name for p in pods}
        readings = [u for u in usage or [] if u.pod in pod_names]
        if ready >= desired and not (waiting or failed_job or unscheduled or conditions):
            # A restart: a container running again after a termination (not a
            # Job's container that finished, nor a completed init container).
            restart = _latest([t for t in terminations if t.restart_gap is not None])
            if restart is not None and restart.finished <= _RECOVERING_WINDOW:
                recovering.append(entry(label, kind, name, ready, desired, pods, restarts,
                                        restart, first_pod, hpa, readings, []))
                return
            at_max = hpa is not None and hpa.current_replicas >= hpa.max_replicas
            peak = max((u.memory_percent_of_limit for u in readings
                        if u.memory_percent_of_limit is not None), default=None)
            healthy_lines.append(f"{label} {ready}/{desired}"
                                 + (f", {restarts} restarts" if restarts else "")
                                 + (f", HPA at max {hpa.max_replicas}" if at_max else "")
                                 + (f", memory at {peak}% of limit"
                                    if peak is not None and peak >= _NEAR_LIMIT_PERCENT else ""))
            return
        w = entry(label, kind, name, ready, desired, pods, restarts, last, first_pod, hpa,
                  readings, init_pending)
        w.unscheduled, w.claims, w.conditions = list(unscheduled), list(claims), conditions
        w.placement = placement
        for reason in set(unscheduled.values()):
            unschedulable.setdefault(reason, []).append(label)
        entries.append(w)

    def entry(label, kind, name, ready, desired, pods, restarts, last, first_pod, hpa,
              readings, init_pending) -> WorkloadHealth:
        pod_names = {p.name for p in pods}
        w = WorkloadHealth(
            workload=label, ready=ready, desired=desired, restarts=restarts,
            last_termination=last, memory=_memory_shape(first_pod),
            template_changed=_template_changed(api, kind, name, namespace) if kind else None,
            autoscaler=_autoscaler_line(hpa) if hpa else None,
            services=[_endpoint_line(e) for e in _services_for(endpoints, pod_names)],
            usage=list(dict.fromkeys(_usage_line(u) for u in readings)))
        for note in (n for u in readings for n in u.notes):
            if note not in w.notes:
                w.notes.append(note)
        if init_pending:
            w.notes.extend([NOTE_INIT_PENDING, *dict.fromkeys(init_pending)])
        if restarts is None:
            w.notes.append("None of its pods could be found, so its restarts and "
                           "terminations are unknown.")
        if last:
            w.notes.extend(last.notes)
            last.notes = []
        return w

    for kind, name, ready, desired, conditions in workloads:
        assess(f"{kind}/{name}", kind, name, ready, desired, grouped.get((kind, name), []),
               conditions)
    for pod in standalone:
        assess(f"Pod/{pod.name}", None, pod.name, pod.ready_containers,
               pod.total_containers, [pod], [])

    entries.sort(key=lambda w: w.workload)
    recovering.sort(key=lambda w: w.last_termination.finished)
    recovering_more = [_recovering_line(w) for w in recovering[_MAX_RECOVERING:]]
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
    # Pods that can't be scheduled for the same reason (a node selector no node
    # matches, set on several workloads at once) are one failure too.
    failures += [FailureGroup(signature=f"not scheduled: {reason}", workloads=sorted(members))
                 for reason, members in unschedulable.items()
                 if len(members) > 1 and reason != _REASON_NOT_RECORDED]
    result = NamespaceHealth(
        namespace=namespace, workloads=entries, recovering=recovering[:_MAX_RECOVERING],
        recovering_more=recovering_more, healthy=healthy_lines,
        common_failures=failures,
        services_without_ready_endpoints=[_endpoint_line(e) for e in endpoints if not e.ready],
        ingress_problems=_ingress_lines(
            _recorded(unrecorded, api.get_ingress_summaries, namespace), problems_only=True))
    policies, unavailable = _network_policies(api, namespace, unrecorded)
    if policies:
        result.network_policies = _policy_lines(policies)
        result.notes.append(NOTE_NETWORK_POLICIES)
    if unavailable:
        result.notes.append(unavailable)
    if unrecorded:
        result.notes += _not_recorded_notes(unrecorded)
    if by_name:
        result.notes.append(NOTE_MATCHED_BY_NAME)
    if any(w.placement for w in entries):
        result.notes.append(NOTE_PLACEMENT)
    if recovering:
        result.notes.append(NOTE_RECOVERING)
    if usage_unavailable:
        result.notes.append(f"Usage unavailable: {usage_unavailable}")
    if any("of limit" in line for line in healthy_lines):
        result.notes.append(NOTE_WORKING_SET)
    if any(w.last_termination for w in entries + recovering):
        result.notes.append(
            "instance_lifetime is how long the last instance ran; restart_gap is the "
            "back-off before the next start (or the wait so far). Restarts are "
            "lifetime + gap apart.")
    if any(w.restarts for w in entries + recovering):
        result.notes.append(NOTE_RESTART_HISTORY)
    return result


def _recovering_line(w: WorkloadHealth) -> str:
    t = w.last_termination
    return (f"{w.workload} {w.ready}/{w.desired}, {w.restarts} restarts, last terminated "
            f"{_human(t.finished)} ago: exit {t.exit_code} ({t.reason})")


# --- get_workload_report -----------------------------------------------------------------

_MAX_LOG_LINES = 100
_MAX_LINE_CHARS = LOG_LINE_CHARS
#: With grep, how many pods' logs are searched; without, how many pods besides
#: the focus pod get a tail, and of how many lines.
_MAX_GREP_PODS = 10
_OTHER_LOG_PODS = 5
_OTHER_LOG_LINES = 5
_MAX_EVENTS = 20


def _find_kind(api, name: str, namespace: str) -> str:
    unrecorded: list[str] = []
    found = [kind for kind, n, *_ in _workloads(api, namespace, unrecorded) if n == name]
    if not found:
        unknown = (f" ({', '.join(getattr(u, 'shown', u) for u in unrecorded)} unknown: "
                   "not recorded or not readable)" if unrecorded else "")
        raise K8sApiError(f"No Deployment, StatefulSet, DaemonSet or Job named '{name}' "
                          f"in namespace '{namespace}'{unknown}.")
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


def _essentials(spec: dict, statuses) -> tuple[list[ContainerEssentials],
                                               list[ContainerEssentials]]:
    """The app containers (with native sidecars) and the init containers."""
    init = _get(spec, "init_containers", "initContainers") or []
    sidecars = [c for c in init if _get(c, "restart_policy", "restartPolicy") == "Always"]
    app = [_container_essentials(c, statuses) for c in spec.get("containers") or []]
    for c in sidecars:
        app.append(_container_essentials(c, statuses))
        app[-1].sidecar = True
    init_only = [_container_essentials(c, statuses) for c in init if c not in sidecars]
    for e in init_only:
        e.probes = []  # init containers can't have probes; "none configured" misleads
    return app, init_only


def _container_essentials(c: dict, statuses) -> ContainerEssentials:
    status = next((s for s in statuses if s.container_name == c.get("name")), None)
    resources = c.get("resources") or {}
    probes = []
    for kind in ("liveness", "readiness", "startup"):
        probe = _get(c, f"{kind}_probe", f"{kind}Probe")
        if probe:
            probes.append(f"{kind}: {_probe_summary(probe)}")
    return ContainerEssentials(
        container=c.get("name"), image=c.get("image") or (status.image if status else ""),
        requests=dict(resources.get("requests") or {}),
        limits=dict(resources.get("limits") or {}),
        probes=probes or ["none configured"])


def _state_text(status, now) -> str:
    st = status.state
    if isinstance(st, ContainerStateRunning):
        return "Running"
    if isinstance(st, ContainerStateWaiting):
        return f"Waiting ({st.reason})"
    if isinstance(st, ContainerStateTerminated):
        return f"Terminated ({st.reason})"
    return "Unknown"


def _events(api, namespace: str, names: list[str], kinds: set[str],
            unrecorded: list[str]) -> list[EventLine]:
    """Events for the named objects of the given kinds, merged where they differ
    only in which object they name (the same event recorded for each pod)."""
    merged: dict[tuple, EventLine] = {}
    name_rx = re.compile(r"(?<![\w.-])(" + "|".join(map(re.escape, sorted(names, key=len, reverse=True)))
                         + r")(?![\w.-])")
    for name in names:
        for e in _recorded(unrecorded, api.get_events, namespace, None, None, name):
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


def _log_start(api, pod: str, namespace: str, container: str, previous: bool):
    try:
        return api.get_log_start(pod, namespace, container, previous)
    except K8sApiError:
        return None  # unknown


def _tail(api, pod: str, namespace: str, container: str, previous: bool,
          lines: int, grep: Optional[str]) -> Optional[LogTail]:
    try:
        if grep:  # the whole log, not its tail (#34)
            found = api.get_log_pattern_summary(pod, namespace, container, pattern=grep,
                                                previous=previous, max_samples=lines)
        else:
            text = api.get_logs_for_pod_and_container(pod, namespace, container,
                                                      tail=lines, previous=previous)
    except K8sLogReclaimed:
        return LogTail(pod=pod, container=container, previous=previous, available=False,
                       reason=LOG_RECLAIMED_REASON)
    except K8sNotRecorded as e:
        return LogTail(pod=pod, container=container, previous=previous, available=False,
                       reason=str(e))
    except K8sApiError:
        return None  # e.g. no previous instance
    began = _log_start(api, pod, namespace, container, previous)
    if grep:
        notes = [n for n in found.notes if not n.startswith("bucket_seconds")]
        if found.matches > len(found.samples):
            notes.append(f"{found.matches} lines match; shown: the first, the last, and "
                         "evenly spaced between." if found.samples else
                         f"{found.matches} lines match; none shown: the report's "
                         f"{_MAX_LOG_LINES}-line limit was reached.")
        return LogTail(pod=pod, container=container, previous=previous, lines=found.samples,
                       log_starts=began, matches=found.matches, searched_lines=found.lines,
                       searched_from=found.first_line, notes=notes)
    notes, body = [], []
    for line in (text or "").splitlines():
        if line.startswith(LOG_NOTE_PREFIX):
            notes.append(line[len(LOG_NOTE_PREFIX):])
        else:
            body.append(line)
    body = body[-lines:]
    return LogTail(pod=pod, container=container, previous=previous,
                   lines=[_clip(l) for l in body],
                   lines_from=next((t for t in map(_line_time, body) if t), None),
                   log_starts=began, notes=notes)


def _logs(api, namespace: str, pods: list, focus, statuses_by_pod: dict, pending_init,
          lines: int, grep: Optional[str], notes: list[str]) -> list[LogTail]:
    """The focus pod's logs in full (current and previous instances), then the
    other pods' (#35): with grep, every log of up to _MAX_GREP_PODS pods is
    searched, so each has its match count; without, up to _OTHER_LOG_PODS get a
    short tail of each current instance. Lines past the focus pod's share the
    report's _MAX_LOG_LINES."""
    others = sorted((p for p in pods if p is not focus), key=lambda p: p.name)
    budget = _MAX_LOG_LINES
    logs: list[LogTail] = []

    def read(pod, previous_too: bool, want: int) -> None:
        nonlocal budget
        for s in statuses_by_pod[pod.name].containers + pending_init(pod):
            for previous in (False, True):
                if previous and not (previous_too and s.restart_count):
                    continue
                n = want if pod is focus else min(want, budget)
                if n <= 0 and not grep:
                    return
                tail = _tail(api, pod.name, namespace, s.container_name, previous,
                             max(n, 0), grep)
                if tail:
                    logs.append(tail)
                    budget -= len(tail.lines)

    read(focus, True, lines)
    if grep:
        searched, skipped = others[:_MAX_GREP_PODS - 1], others[_MAX_GREP_PODS - 1:]
        for pod in searched:
            read(pod, True, lines)
        if skipped:
            notes.append(f"Logs of {_pods_text([p.name for p in skipped])} weren't "
                         f"searched: the report searches {_MAX_GREP_PODS} pods. "
                         "get_log_pattern_summary searches one.")
    else:
        shown = []
        for pod in others[:_OTHER_LOG_PODS]:
            if budget <= 0:
                break
            read(pod, False, min(lines, _OTHER_LOG_LINES))
            shown.append(pod.name)
        if skipped := [p.name for p in others if p.name not in shown]:
            notes.append(f"No log lines from {_pods_text(skipped)}: the report shows "
                         f"{_OTHER_LOG_LINES} from each of {_OTHER_LOG_PODS} other pods at "
                         "most. get_logs_for_pod_and_container reads them.")
    return logs


def workload_report(api, name: str, namespace: str = "default", kind: Optional[str] = None,
                    log_lines: int = 20, grep: Optional[str] = None) -> WorkloadReport:
    now = _now()
    if kind is not None and kind not in _KINDS:
        raise K8sApiError(f"Unsupported kind '{kind}': expected one of {', '.join(_KINDS)}.")
    kind = kind or _find_kind(api, name, namespace)
    unrecorded: list[str] = []
    workloads = _workloads(api, namespace, unrecorded)
    matches = [(r, d, c) for k, n, r, d, c in workloads if (k, n) == (kind, name)]
    if not matches:
        unknown = next((u for u in unrecorded if u == f"{kind}s"), None)
        if getattr(unknown, "forbidden", False):
            raise K8sForbidden(f"These credentials can't read {kind}s (403 Forbidden), so "
                               f"{kind} '{name}' is unknown, not absent.")
        if unknown is not None:
            raise K8sNotRecorded(f"This capture doesn't record {kind}s, so {kind} "
                                 f"'{name}' is unknown, not absent.", what=f"{kind}s")
        raise K8sApiError(f"{kind} '{name}' not found in namespace '{namespace}'.")
    ready, desired, conditions = matches[0]
    grouped, _, by_name = _pods_by_workload(api, namespace, workloads, unrecorded)
    pods = grouped.get((kind, name), [])
    report = WorkloadReport(workload=f"{kind}/{name}", ready=ready, desired=desired,
                            conditions=conditions)
    if by_name and pods and all(p.owner is None for p in pods):
        report.notes.append(NOTE_MATCHED_BY_NAME)

    statuses_by_pod = {p.name: api.get_pod_container_statuses(p.name, namespace) for p in pods}

    def instance(pod, s):
        started = s.state.started_at if isinstance(s.state, ContainerStateRunning) else None
        termination = _termination(pod.name, s, now)
        if termination:
            termination.pod = termination.container = None  # the instance names them
        return Instance(pod=pod.name, container=s.container_name, pod_age=pod.age,
                        state=_state_text(s, now),
                        started=_to_whole_seconds(now - started) if started else None,
                        ready=s.ready, restarts=s.restart_count, last_termination=termination)

    def pending_init(pod):
        return [s for s in statuses_by_pod[pod.name].init_containers or []
                if not _init_completed(s)]

    unbound_claims = functools.cache(lambda: _unbound_claims(api, namespace, unrecorded))
    nodes = functools.cache(lambda: _nodes(api))
    for pod in pods:
        if reason := _unscheduled(pod, statuses_by_pod[pod.name]):
            report.unscheduled.append(_unscheduled_line(pod, reason))
            report.claims.extend(c for c in unbound_claims().get(pod.name, [])
                                 if c not in report.claims)
            report.placement.append(_placement_line(api, pod, namespace, nodes()))
        report.instances.extend(instance(pod, s) for s in statuses_by_pod[pod.name].containers)
        report.init_instances.extend(instance(pod, s) for s in pending_init(pod))
    if report.placement:
        report.notes.append(NOTE_PLACEMENT)
    if any(st.init_containers is None for st in statuses_by_pod.values()):
        report.notes.append(NOTE_INIT_NOT_RECORDED)
    elif report.init_instances:
        report.notes.append(NOTE_INIT_PENDING)
    if pods:
        # The pod most in trouble speaks for the workload: waiting (or held back by
        # an init container), then most restarts.
        focus = max(pods, key=lambda p: (
            bool(pending_init(p)) or any(isinstance(s.state, ContainerStateWaiting)
                                         for s in statuses_by_pod[p.name].containers),
            p.restarts))
        focus_statuses = statuses_by_pod[focus.name]
        report.containers, report.init_containers = _essentials(
            api.get_pod_spec(focus.name, namespace),
            focus_statuses.containers + (focus_statuses.init_containers or []))
        lines = max(1, min(log_lines, _MAX_LOG_LINES))
        if len(pods) > 1:
            report.notes.append(f"containers are from {focus.name}, of {len(pods)} pods; "
                                "logs are from it first, then the others.")
        report.logs = _logs(api, namespace, pods, focus, statuses_by_pod, pending_init,
                            lines, grep, report.notes)
    else:
        report.notes.append("No pods: spec, instances and logs are unavailable.")

    replicasets = [r.name for r in _recorded(unrecorded, api.get_replicaset_summaries, namespace)
                   if r.owner_deployment == name] if kind == "Deployment" else []
    report.events = _events(api, namespace, [name] + replicasets + [p.name for p in pods],
                            {kind, "ReplicaSet", "Pod"}, unrecorded)
    oldest = max((e.first_seen for e in report.events if e.first_seen is not None), default=None)
    if oldest is not None:
        report.notes.append(
            f"Event records start {_human(oldest)} ago: that is where the records begin, "
            "not necessarily when the problem did. Records expire after a retention "
            "period the API doesn't report.")

    history = None
    if kind != "Job":
        history = _recorded(unrecorded, api.get_workload_history, name, namespace, kind,
                            default=None)
    if history is not None:
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
    report.autoscaler = _autoscalers(api, namespace, unrecorded).get(f"{kind}/{name}")
    report.services = _services_for(_recorded(unrecorded, api.get_endpoint_summaries, namespace),
                                    {p.name for p in pods})
    report.ingresses = _ingress_lines(_recorded(unrecorded, api.get_ingress_summaries, namespace),
                                      services={e.service for e in report.services})
    policies, unavailable = _network_policies(api, namespace, unrecorded)
    if policies is not None and pods:
        report.network_policies = _workload_policy_lines(policies, {p.name for p in pods})
        if any(set(p.selected_pods) & {q.name for q in pods} for p in policies):
            report.notes.append(NOTE_NETWORK_POLICIES)
        if kinds := _policy_kinds(api):
            report.notes.append("Policy kinds from custom resources are installed: "
                                + ", ".join(kinds) + ". Their policies can also apply to "
                                "these pods, and aren't shown.")
    if unavailable:
        report.notes.append(unavailable)
    if unrecorded:
        report.notes += _not_recorded_notes(unrecorded)
    usage, usage_unavailable = _usage(api, namespace)
    if usage_unavailable:
        report.notes.append(f"Usage unavailable: {usage_unavailable}")
    report.usage = [u for u in usage or [] if u.pod in {p.name for p in pods}]

    if any(i.last_termination for i in report.instances + report.init_instances):
        report.notes.append(
            "instance_lifetime is how long an instance ran; restart_gap is the back-off "
            "before the next start (or the wait so far). Restarts are lifetime + gap apart.")
    if any(i.restarts for i in report.instances + report.init_instances):
        report.notes.append(NOTE_RESTART_HISTORY)
    for i in report.instances + report.init_instances:
        if i.last_termination:
            report.notes.extend(n for n in i.last_termination.notes if n not in report.notes)
            i.last_termination.notes = []
    return report


def _show_memory(b: Optional[int]) -> Optional[str]:
    return None if b is None else f"{round(b / 2**20)}Mi"


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
