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
"""What changed recently (#38): write records from managedFields.

Kubernetes keeps no history of an object, but each object's ``managedFields``
holds one entry per client (manager), operation and subresource, with the time
of that client's latest such write. :func:`object_writes` reads them for every
object of every listable resource type, through discovery and metadata-only
lists, so it never receives an object's contents: a Secret's values never reach
this process, only its name and write times, and only if the role may list
Secrets at all. The capture stores the result; :func:`recent_changes` turns it
into the writes within a window, live and in replay alike.

What it can't show, and says: what a write changed, a client's earlier writes,
and deletions.
"""

from __future__ import annotations

import collections
import concurrent.futures
import datetime
import fnmatch
import json
import re
from dataclasses import dataclass
from typing import Callable, Optional

from kubernetes.client.exceptions import ApiException

from . import k8s_tools
from .k8s_tools import (ChangeRecord, ObjectRecord, ObjectWrites, RecentChanges,
                        UnreadableKind, WriteEntry, K8sApiError)

#: Asks for each object's metadata only (name, creation, owners, managedFields),
#: never its spec or data. No fallback to a full list: a server that can't
#: serve it (406) has that type named as not readable, rather than send Secrets.
METADATA_ONLY = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"

#: Kubernetes' own controllers, by managedFields manager name (fnmatch
#: patterns). Their writes are counted, not listed. Operators and tools (helm,
#: kops, Argo CD, cilium-operator) are clients like any other, and are listed.
CONTROLLER_MANAGERS = (
    "kube-controller-manager",
    "kube-scheduler",
    "kubelet",
    "kube-apiserver",
    "kube-proxy",
    "clusterrole-aggregation-controller",
    "*cloud-controller-manager",
    "csi-*",                   # CSI sidecars: csi-attacher, csi-provisioner, ...
    "external-attacher",
    "external-provisioner",
    "external-resizer",
    "external-snapshotter",
    "snapshot-controller",
)

#: Resource types never listed: written continually, so noise in any window.
#: Events have their own tool. ("*" is every type in the group.)
SKIPPED = {("", "events"), ("events.k8s.io", "events"),
           ("coordination.k8s.io", "leases"), ("metrics.k8s.io", "*")}

#: Common kinds whose absence from a capture's write records replay names.
EXPECTED_KINDS = (
    "pods", "services", "configmaps", "secrets", "serviceaccounts",
    "persistentvolumeclaims", "resourcequotas", "limitranges", "namespaces", "nodes",
    "persistentvolumes", "deployments.apps", "replicasets.apps", "statefulsets.apps",
    "daemonsets.apps", "jobs.batch", "cronjobs.batch",
    "horizontalpodautoscalers.autoscaling", "networkpolicies.networking.k8s.io",
    "ingresses.networking.k8s.io", "roles.rbac.authorization.k8s.io",
    "rolebindings.rbac.authorization.k8s.io", "clusterroles.rbac.authorization.k8s.io",
    "clusterrolebindings.rbac.authorization.k8s.io", "poddisruptionbudgets.policy",
    "customresourcedefinitions.apiextensions.k8s.io",
)

#: Records listed one by one; the rest are counted in changes_more.
MAX_CHANGES = 50
#: More objects than this written by one client, kind and subresource in the
#: window become one line in ``bulk``.
BULK_OVER = 10
#: A write this close to an object's creation is its creation.
CREATED_WITHIN = datetime.timedelta(seconds=1)
#: A revision created this soon after a spec write is reported with it.
REVISION_WITHIN = datetime.timedelta(seconds=5)

_REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
_WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet")
_LIST_THREADS = 8


# --- reading -----------------------------------------------------------------

def _raw_get(path: str, query: Optional[dict[str, str]] = None,
             accept: str = "application/json") -> dict:
    """GET a path from the bound API server as JSON, for paths the generated
    client has no method for (discovery, metadata-only lists). Raises
    ApiException for a non-2xx answer, as the generated methods do."""
    api = k8s_tools._binding().api_client
    params = list((query or {}).items())
    headers = {"Accept": accept}
    if hasattr(api, "param_serialize"):  # kubernetes 37 and later
        method, url, header_params, body, post_params = api.param_serialize(
            "GET", path, query_params=params, header_params=headers,
            auth_settings=["BearerToken"])
        resp = api.call_api(method, url, header_params=header_params, body=body,
                            post_params=post_params)
        resp.read()
        if not 200 <= resp.status <= 299:  # as the generated methods raise
            raise ApiException.from_response(http_resp=resp, body=None, data=None)
    else:
        resp = api.call_api(path, "GET", query_params=params, header_params=headers,
                            auth_settings=["BearerToken"], _preload_content=False,
                            _return_http_data_only=True)
    data = k8s_tools._log_body(resp)
    return json.loads(data.decode() if isinstance(data, bytes) else data or "{}")


@dataclass(frozen=True)
class _Resource:
    group: str
    version: str
    plural: str
    kind: str
    namespaced: bool

    @property
    def key(self) -> str:
        """kubectl's name for the type: "pods", "deployments.apps"."""
        return f"{self.plural}.{self.group}" if self.group else self.plural

    @property
    def base(self) -> str:
        return f"/apis/{self.group}/{self.version}" if self.group else "/api/v1"


def _skipped(group: str, plural: str) -> bool:
    return (group, plural) in SKIPPED or (group, "*") in SKIPPED


def _resources(group: str, version: str, document: dict) -> list[_Resource]:
    return [_Resource(group, version, r["name"], r.get("kind", ""), bool(r.get("namespaced")))
            for r in document.get("resources") or []
            if "/" not in r["name"] and "list" in (r.get("verbs") or [])
            and not _skipped(group, r["name"])]


def _discover(not_readable: dict[str, str]) -> list[_Resource]:
    """Every listable resource type, at its group's preferred version. A group
    whose discovery fails (an aggregated API that is down) is named, not fatal."""
    try:
        found = _resources("", "v1", _raw_get("/api/v1"))
        groups = _raw_get("/apis").get("groups") or []
    except (ApiException, OSError) as e:
        raise k8s_tools._api_error("Error discovering the API's resource types", e) from e
    for g in groups:
        name = g.get("name", "")
        preferred = g.get("preferredVersion") or (g.get("versions") or [{}])[0]
        if not preferred.get("version") or _skipped(name, "*"):
            continue
        try:
            found += _resources(name, preferred["version"],
                                _raw_get(f"/apis/{preferred['groupVersion']}"))
        except (ApiException, OSError) as e:
            not_readable[f"*.{name}"] = k8s_tools._api_error_text(e)
    return found


def _list(resource: _Resource, namespace: Optional[str]) -> list[dict]:
    path = (f"{resource.base}/namespaces/{namespace}/{resource.plural}"
            if resource.namespaced and namespace else f"{resource.base}/{resource.plural}")
    items, query = [], {"limit": "500"}
    while True:
        document = _raw_get(path, query, accept=METADATA_ONLY)
        items += document.get("items") or []
        cont = (document.get("metadata") or {}).get("continue")
        if not cont:
            return items
        query = {"limit": "500", "continue": cont}


def _time(text: Optional[str]) -> Optional[datetime.datetime]:
    if not text:
        return None
    return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))


def _record(resource: _Resource, item: dict) -> ObjectRecord:
    meta = item.get("metadata") or {}
    owner = next((f"{o.get('kind')}/{o.get('name')}" for o in meta.get("ownerReferences") or []
                  if o.get("controller")), None)
    revision = (meta.get("annotations") or {}).get(_REVISION_ANNOTATION) \
        if resource.kind == "ReplicaSet" else None
    return ObjectRecord(
        kind=resource.kind, group=resource.group, namespace=meta.get("namespace"),
        name=meta.get("name", ""), created=_time(meta.get("creationTimestamp")), owner=owner,
        writes=[WriteEntry(manager=e.get("manager"), operation=e.get("operation"),
                           subresource=e.get("subresource"), time=_time(e.get("time")))
                for e in meta.get("managedFields") or []],
        revision=int(revision) if revision and str(revision).isdigit() else None)


def object_writes(namespaces: Optional[list[str]] = None) -> ObjectWrites:
    """Write records of every object: namespaced types in each of
    ``namespaces`` (in every namespace when None), cluster-scoped types once.
    A type that can't be listed (a 403, a 406, a 503) is named in
    ``not_readable`` and the rest carry on."""
    not_readable: dict[str, str] = {}
    resources = _discover(not_readable)
    jobs = [(r, ns) for r in resources
            for ns in ((namespaces or [None]) if r.namespaced else [None])]

    def read(job):
        resource, ns = job
        try:
            return job, [_record(resource, item) for item in _list(resource, ns)], None
        except (ApiException, OSError) as e:
            return job, [], k8s_tools._api_error_text(e)

    objects: list[ObjectRecord] = []
    kinds: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(_LIST_THREADS) as pool:
        for (resource, ns), records, error in pool.map(read, jobs):
            if error is not None:
                not_readable[resource.key] = error + (f" (namespace {ns})" if ns else "")
                continue
            if resource.key not in kinds:
                kinds.append(resource.key)
            objects += records
    return ObjectWrites(kinds=sorted(kinds), objects=objects,
                        not_readable=[UnreadableKind(kind=k, reason=why)
                                      for k, why in sorted(not_readable.items())])


# --- the window ----------------------------------------------------------------

_WINDOW_RE = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def parse_window(within: str) -> datetime.timedelta:
    """ "30m", "2h", "1d", "90s", or combined ("1h30m")."""
    m = _WINDOW_RE.fullmatch((within or "").strip().lower())
    if not m or not any(m.groups()):
        raise K8sApiError(f"within must be a duration like 30m, 2h, 1d or 1h30m, not "
                          f"{within!r}")
    days, hours, minutes, seconds = (int(g or 0) for g in m.groups())
    return datetime.timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def is_controller(manager: Optional[str]) -> bool:
    return manager is not None and any(fnmatch.fnmatchcase(manager, p)
                                       for p in CONTROLLER_MANAGERS)


# --- revisions -----------------------------------------------------------------

#: (kind, namespace, name) -> [(revision, created)]
Births = Callable[[str, str, str], list[tuple[int, datetime.datetime]]]


def deployment_births(objects: list[ObjectRecord]) -> dict[tuple, list]:
    """Each Deployment's revisions, from its ReplicaSets' write records."""
    births: dict[tuple, list] = collections.defaultdict(list)
    for o in objects:
        if o.kind == "ReplicaSet" and o.revision is not None and o.created and \
                o.owner and o.owner.startswith("Deployment/"):
            births[("Deployment", o.namespace, o.owner.split("/", 1)[1])].append(
                (o.revision, o.created))
    return births


def _revision(births: Births, o: ObjectRecord, w: WriteEntry) -> Optional[int]:
    if o.kind not in _WORKLOADS or w.subresource or w.time is None:
        return None
    made = [rev for rev, created in births(o.kind, o.namespace, o.name)
            if datetime.timedelta(0) <= created - w.time <= REVISION_WITHIN]
    return min(made) if made else None


# --- the result ----------------------------------------------------------------

def recent_changes(writes: ObjectWrites, namespace: str, window: datetime.timedelta,
                   now: datetime.datetime, births: Births,
                   source: str = "write records",
                   not_recorded: Optional[list[str]] = None) -> RecentChanges:
    """The writes within ``window`` before ``now`` to objects in ``namespace``
    and to cluster-scoped objects, Kubernetes' own controllers' counted apart."""
    since = now - window
    records: list[ChangeRecord] = []
    left_out: collections.Counter = collections.Counter()

    def add(o: ObjectRecord, change: str, time: datetime.datetime,
            w: Optional[WriteEntry] = None) -> None:
        records.append(ChangeRecord(
            kind=o.kind, namespace=o.namespace, name=o.name, change=change, time=time,
            age=now - time, manager=w.manager if w else None,
            operation=w.operation if w else None, subresource=w.subresource if w else None,
            revision=_revision(births, o, w) if w else None))

    for o in writes.objects:
        if o.namespace not in (None, namespace):
            continue
        creation_entry = False
        for w in o.writes:
            is_creation = (o.created is not None and w.time is not None
                           and abs(w.time - o.created) <= CREATED_WITHIN)
            creation_entry = creation_entry or is_creation
            if w.time is None or w.time < since:
                continue
            if is_controller(w.manager):
                left_out[w.manager] += 1
                continue
            add(o, "created" if is_creation else "updated", w.time, w)
        # Created in the window, and whoever created it has written again since
        # (or nothing is recorded): the creation is a fact, the client isn't.
        # An owned object was created by its owner's controller, and so is one
        # whose recorded writes are all controllers' (Endpoints the endpoints
        # controller has rewritten since): a client's entry stays unless another
        # client takes over every field it set, which controllers don't.
        controllers_only = bool(o.writes) and all(is_controller(w.manager) for w in o.writes)
        if o.created and o.created >= since and not creation_entry and o.owner is None \
                and not controllers_only:
            add(o, "created", o.created)

    records.sort(key=lambda r: (-r.time.timestamp(), r.kind, r.name))
    groups: dict[tuple, list[ChangeRecord]] = collections.defaultdict(list)
    for r in records:
        groups[(r.manager, r.kind, r.change, r.subresource)].append(r)
    bulk, collapsed = [], set()
    for (manager, kind, change, sub), members in groups.items():
        if len(members) > BULK_OVER:
            bulk.append(f"{manager or 'an unrecorded client'} {change} {len(members)} "
                        f"{kind}{f' ({sub})' if sub else ''}, newest "
                        f"{k8s_tools._format_timedelta(members[0].age)} ago")
            collapsed.update(id(r) for r in members)
    records = [r for r in records if id(r) not in collapsed]
    return RecentChanges(
        namespace=namespace, within=window, source=source,
        changes=records[:MAX_CHANGES],
        changes_more=len(records) - MAX_CHANGES if len(records) > MAX_CHANGES else None,
        bulk=bulk,
        left_out=[f"{m}: {n} write{'s' if n != 1 else ''}" for m, n in left_out.most_common()],
        not_readable=[f"{u.kind}: {u.reason}" for u in writes.not_readable],
        not_recorded=list(not_recorded or []))


def recent_changes_live(namespace: str, within: str) -> RecentChanges:
    window = parse_window(within)
    writes = object_writes([namespace])
    now = k8s_tools._now()
    deployments = deployment_births(writes.objects)
    revisions: dict[tuple, list] = {}

    def births(kind: str, ns: str, name: str) -> list[tuple[int, datetime.datetime]]:
        if kind == "Deployment":
            return deployments.get((kind, ns, name), [])
        if ns not in revisions:  # StatefulSets and DaemonSets: read only when asked
            revisions[ns] = _controller_revisions(ns)
        return revisions[ns].get((kind, name), [])

    return recent_changes(writes, namespace, window, now, births)


def _controller_revisions(namespace: str) -> dict[tuple, list]:
    """(kind, name) -> [(revision, created)] from ControllerRevisions, which
    keep the revision number outside their metadata."""
    if k8s_tools.APPS_V1_API is None:
        k8s_tools.APPS_V1_API = k8s_tools._get_apps_v1_api_client()
    try:
        items = k8s_tools.APPS_V1_API.list_namespaced_controller_revision(namespace=namespace).items
    except ApiException:
        return {}  # the revision is then unknown, and the write still listed
    found: dict[tuple, list] = collections.defaultdict(list)
    for cr in items:
        owner = next((o for o in cr.metadata.owner_references or [] if o.controller), None)
        if owner is not None and cr.metadata.creation_timestamp is not None:
            found[(owner.kind, owner.name)].append((cr.revision, cr.metadata.creation_timestamp))
    return found
