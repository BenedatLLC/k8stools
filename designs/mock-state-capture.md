# Mock State Capture & Replay

## Status — PAUSED (2026-08-29)

**Implementation has not started.** This design is complete and has no open
blockers, but none of the new/changed files below exist yet — `mock_tools.py`
still ships the hardcoded `_get_static_mock_data()` from a single OTel-Demo
snapshot. The full build (new `MockState`, capture CLI, fixture migration,
`mock_tools.py`/MCP-server refactor, two test modules) is several hours of work,
so it is intentionally paused rather than half-started.

When resumed, follow the plan below verbatim. Suggested first step:
`src/k8stools/mock_state.py` + the serialization round-trip (`tests/test_capture.py`),
then the capture CLI, then wire `mock_tools.py` and the MCP server. This design was
updated (2026-08-30) to cover the 1.1.0 tool batch — the JSON capture format,
`MockState` query methods, and capture CLI below all include ConfigMaps,
StatefulSets, CronJobs/Jobs, PVCs, and the unified flat events list.

**Updated 2026-09-12 for 1.2.0 and for its first real consumer.** Three changes,
all driven by k8srca's scenario suite (`k8srca/designs/004-scenario-testing.md`),
which replays captures as graded root-cause test fixtures:

1. **ReplicaSets** (the 1.2.0 tool batch) — a `replicasets` top-level list, a
   `MockState.get_replicaset_summaries` with the same filters and *the same
   ordering guarantee* as the real tool, and capture support. Without this a
   replayed capture cannot answer "did something change recently?", which is
   the question a deploy-caused incident turns on.
2. **Previous-instance logs** are now captured, not deferred. For a
   crash-looping container the current instance's logs are usually empty or
   post-restart; `previous=True` is the call that carries the diagnosis. A
   capture without them silently omits the best evidence in the scenario.
3. **A frozen-time mode** for the replay clock. Advancing time is right for
   interactive use and wrong for a graded suite — see
   [Replay clock](#replay-clock).

## Goal

Enable complex, multi-scenario agent testing without a live cluster by:

1. A CLI tool (`k8s-capture-state`) that snapshots a real cluster to a JSON file.
2. A `MockState` class that loads a JSON file and serves subsets of it in response to the same queries the real tools issue.
3. An MCP server option (`--state-file`) to run entirely from a captured snapshot.
4. Migration of the existing hardcoded mock data to the new format.

---

## JSON capture format

A single JSON file per capture. Top-level keys:

```json
{
  "version": "1",
  "captured_at": "2026-05-31T14:00:00Z",
  "namespaces": [ ... ],
  "nodes": [ ... ],
  "pods": [ ... ],
  "deployments": [ ... ],
  "replicasets": [ ... ],
  "services": [ ... ],
  "configmaps": [ ... ],
  "statefulsets": [ ... ],
  "cronjobs": [ ... ],
  "jobs": [ ... ],
  "pvcs": [ ... ],
  "events": [ ... ]
}
```

The `configmaps`..`events` keys were added alongside the 1.1.0 tool batch
(ConfigMaps, StatefulSets, CronJobs/Jobs, PVCs, and cluster-wide events). See
[Resource records added in 1.1.0](#resource-records-added-in-110) below for their
shapes. `replicasets` was added alongside the 1.2.0 tool batch — see
[Resource records added in 1.2.0](#resource-records-added-in-120).

All lists are **flat** (not nested by namespace). Namespace filtering is handled at query time by `MockState`, matching how the real tools work. The `MockState` builds internal dicts for O(1) pod lookups at load time.

### Temporal field encoding

All time-related data is stored relative to `captured_at` so the mock can replay it relative to when the MCP server starts — an event that was 10 minutes old at capture time will appear 10 minutes old at server start and age forward from there.

| Original field type | JSON representation | Notes |
|---|---|---|
| `timedelta` (age, last_restart, last_seen) | `float` seconds | `age_seconds`, `last_restart_seconds`, `last_seen_seconds` |
| `datetime` (started_at, finished_at) | `float` seconds before captured_at | `started_at_offset_seconds`, `finished_at_offset_seconds` |
| `None` for optional fields | `null` | unchanged |

At server start, `MockState` records `server_start_time`. When serving a query at time `now`:

- **timedelta reconstruction**: `timedelta(seconds=stored_seconds + (now - server_start_time).total_seconds())`
- **datetime reconstruction**: `server_start_time - timedelta(seconds=stored_offset_seconds)`

This keeps all ages consistent and advancing in real time, anchored to the moment the server started rather than when the snapshot was taken.

### Namespace / node records

```json
{
  "name": "default",
  "status": "Active",
  "age_seconds": 432000.0
}
```

```json
{
  "name": "minikube",
  "status": "Ready",
  "roles": ["control-plane"],
  "age_seconds": 432000.0,
  "version": "v1.30.0",
  "internal_ip": "192.168.49.2",
  "external_ip": null,
  "os_image": "Ubuntu 22.04.4 LTS",
  "kernel_version": "6.5.0-25-generic",
  "container_runtime": "docker://26.1.1"
}
```

### Pod records

Each element of the `pods` array bundles all per-pod data together so a single capture pass produces a self-contained record:

```json
{
  "summary": {
    "name": "ad-647b4947cc-s5mpm",
    "namespace": "default",
    "total_containers": 1,
    "ready_containers": 0,
    "restarts": 5,
    "last_restart_seconds": 300.0,
    "age_seconds": 43200.0,
    "ip": "10.244.0.5",
    "node": "minikube"
  },
  "container_statuses": [
    {
      "pod_name": "ad-647b4947cc-s5mpm",
      "namespace": "default",
      "container_name": "ad",
      "image": "otel/demo:ad",
      "ready": false,
      "restart_count": 5,
      "started": false,
      "stop_signal": null,
      "state": {
        "state_name": "waiting",
        "reason": "CrashLoopBackOff",
        "message": "back-off 5m0s restarting failed container"
      },
      "last_state": {
        "state_name": "terminated",
        "exit_code": 1,
        "reason": "Error",
        "message": null,
        "started_at_offset_seconds": 360.0,
        "finished_at_offset_seconds": 300.0
      },
      "volume_mounts": [],
      "resource_requests": {"cpu": "100m", "memory": "128Mi"},
      "resource_limits": {"cpu": "200m", "memory": "256Mi"},
      "allocated_resources": null
    }
  ],
  "events": [
    {
      "last_seen_seconds": 120.0,
      "type": "Warning",
      "reason": "BackOff",
      "object": "Pod/ad-647b4947cc-s5mpm",
      "message": "Back-off restarting failed container ad in pod ad-647b4947cc-s5mpm_default"
    }
  ],
  "spec": { },
  "logs": {
    "ad": "2025-07-21 10:00:00 starting up\n..."
  },
  "previous_logs": {
    "ad": "2025-07-21 09:54:00 starting up\n...\nAllocating 512MB heap\n"
  }
}
```

`previous_logs` holds the logs of the container's *previous terminated
instance* — what `get_logs_for_pod_and_container(..., previous=True)` returns.
It is present only for containers that had a previous instance at capture time
(`restart_count > 0`); the key is absent otherwise, and `previous=True` against
an absent key behaves like the real tool does with no previous instance.

**This is not an optional nicety.** In a crash loop the current instance is
typically seconds old and its logs are empty, truncated, or show only startup —
the stack trace, the OOM message and the exit path all live in the previous
instance. A capture that omits them replays a cluster where the decisive
evidence simply does not exist, and any agent tested against it will look worse,
or differently wrong, than against the cluster it was captured from.

`ContainerStateRunning.started_at` → `started_at_offset_seconds` (seconds before `captured_at`).  
`ContainerStateTerminated.started_at` / `finished_at` → same pattern.  
`ContainerStateWaiting` has no datetime fields.

### Deployment / service records

Same as current `DeploymentSummary` / `ServiceSummary` Pydantic fields, with `age` → `age_seconds`.

### Resource records added in 1.1.0

The 1.1.0 tool batch (ConfigMaps, StatefulSets, CronJobs/Jobs, PVCs, cluster-wide
events) adds the corresponding top-level lists. Each follows the same conventions:
lists are flat, `timedelta` fields become `*_seconds` floats, and `datetime` fields
become `*_offset_seconds` before `captured_at`.

- **`configmaps`** — one record per ConfigMap holding the *full* content (so both
  `get_configmap_summaries` and `get_configmap` can be served from it):
  ```json
  {
    "name": "app-config", "namespace": "default", "age_seconds": 86400.0,
    "data": {"LOG_LEVEL": "info", "MODEL_ALIAS": "gpt-4o"},
    "binary_data_keys": ["cert.bin"]
  }
  ```
  `get_configmap_summaries` derives `key_count` / `data_size` from `data` +
  `binary_data_keys`; `get_configmap` returns `{name, namespace, data, binary_data_keys}`.

- **`statefulsets`** — `StatefulSetSummary` fields with `age` → `age_seconds`.

- **`cronjobs`** — `CronJobSummary` fields with `age` → `age_seconds`,
  `last_schedule_time` → `last_schedule_time_seconds`, `last_successful_time` →
  `last_successful_time_seconds`; `containers` is the list of
  `ContainerTemplateSummary` (name/image/env), which has no temporal fields.

- **`jobs`** — `JobSummary` fields with `age` → `age_seconds`,
  `start_time` → `start_time_seconds`, `completion_time` → `completion_time_seconds`;
  `containers` as above; `owner` and `conditions` stored verbatim.

- **`pvcs`** — `PVCSummary` fields with `age` → `age_seconds`. `mounted_by` is
  stored verbatim (captured at snapshot time; it does not advance).

- **`events`** — a single flat list of **all** captured events, replacing the
  per-pod `events` nested in pod records above. Each record carries enough context
  to serve both cluster/namespace-wide `get_events` (with `reason` /
  `involved_kind` / `involved_name` / `event_type` filters) and `get_pod_events`
  (a filtered view where `involved_kind == "Pod"` and `involved_name == pod_name`):
  ```json
  {
    "last_seen_seconds": 120.0, "type": "Warning", "reason": "Evicted",
    "namespace": "default", "involved_kind": "Pod", "involved_name": "session-abc",
    "object": "Pod/session-abc",
    "message": "The node was low on resource: ephemeral-storage."
  }
  ```
  This is the one structural change from the original design: pods no longer embed
  their own `events`; `MockState.get_pod_events` filters the flat list instead. This
  also lets captures include events with no surviving pod (Evicted, FailedScheduling),
  which is precisely the `get_events` use case.

### Resource records added in 1.2.0

- **`replicasets`** — one flat list of every captured ReplicaSet.
  `ReplicaSetSummary` fields verbatim with `age` → `age_seconds`:

  ```json
  {
    "name": "ad-647b4947cc",
    "namespace": "default",
    "owner_deployment": "ad",
    "revision": 2,
    "desired_replicas": 1,
    "current_replicas": 1,
    "ready_replicas": 0,
    "images": ["ghcr.io/open-telemetry/demo:2.2.0-ad"],
    "age_seconds": 27240.0
  }
  ```

  `owner_deployment` and `revision` are `Optional` on the model and may be
  `null` — a ReplicaSet created directly, or one whose
  `deployment.kubernetes.io/revision` annotation is missing. Capture stores
  `null`; it does not invent a revision.

**Ordering is part of the contract, not a presentation detail.** The real tool
documents that results are grouped by namespace and owning deployment and,
within each deployment, **sorted by revision, oldest first** — so that when
filtered to one deployment "the last entry is that deployment's current
revision." Callers rely on that sentence. `MockState.get_replicaset_summaries`
must reproduce the ordering rather than echo capture order, or a replayed
capture quietly violates a guarantee the tool's own docstring makes. Sort at
query time, after filtering, on `(namespace, owner_deployment, revision)` with
`null` revisions last.

**Why this list earns its place in a capture.** ReplicaSets are a Deployment's
change history, and they are the only such history reachable through the tool
surface: no git, no CI, no deployment tooling. Their value in a replayed
scenario is *relational* — the shape that answers a question is a deployment
aged 8 days whose current ReplicaSet is aged 7h34m, meaning it was upgraded
7h34m ago. That inference survives replay only because every age in the capture
is stored against the same `captured_at` and advanced by the same offset, so
the intervals between them are preserved exactly. Capturing ReplicaSets without
that invariant would be worse than not capturing them: it would support
confident arithmetic on drifting numbers.

---

## Components

### 1. `MockState` class (`src/k8stools/mock_state.py`)

Loads a capture file and serves subsets of it in response to tool queries.

```python
class MockState:
    @classmethod
    def from_file(cls, path: Path) -> MockState: ...
    @classmethod
    def from_builtin(cls) -> MockState: ...   # loads tests/fixtures/otel-demo.json

    # Query methods — same signatures as k8s_tools counterparts
    def get_namespaces(self) -> list[NamespaceSummary]: ...
    def get_node_summaries(self) -> list[NodeSummary]: ...
    def get_pod_summaries(self, namespace: str | None = None) -> list[PodSummary]: ...
    def get_pod_container_statuses(self, pod_name: str, namespace: str) -> list[ContainerStatus]: ...
    def get_pod_events(self, pod_name: str, namespace: str) -> list[EventSummary]: ...
    def get_pod_spec(self, pod_name: str, namespace: str) -> dict[str, Any]: ...
    def get_logs_for_pod_and_container(
        self, pod_name: str, namespace: str, container_name: str | None = None
    ) -> str | None: ...
    def get_deployment_summaries(self, namespace: str | None = None) -> list[DeploymentSummary]: ...
    def get_service_summaries(self, namespace: str | None = None) -> list[ServiceSummary]: ...
    # added in 1.2.0
    def get_replicaset_summaries(self, namespace: str | None = None,
                                 deployment: str | None = None) -> list[ReplicaSetSummary]: ...
    # added in 1.1.0
    def get_configmap_summaries(self, namespace: str | None = None) -> list[ConfigMapSummary]: ...
    def get_configmap(self, name: str, namespace: str) -> dict[str, Any]: ...
    def get_statefulset_summaries(self, namespace: str | None = None) -> list[StatefulSetSummary]: ...
    def get_cronjob_summaries(self, namespace: str | None = None) -> list[CronJobSummary]: ...
    def get_job_summaries(self, namespace: str | None = None) -> list[JobSummary]: ...
    def get_logs_for_job(self, job_name: str, namespace: str, container_name: str | None = None,
                         tail: int | None = None, since_seconds: int | None = None,
                         previous: bool = False) -> str | None: ...
    def get_logs_for_cronjob(self, cronjob_name: str, namespace: str, container_name: str | None = None,
                             tail: int | None = None, since_seconds: int | None = None,
                             previous: bool = False) -> str | None: ...
    def get_pvc_summaries(self, namespace: str | None = None) -> list[PVCSummary]: ...
    def get_events(self, namespace: str | None = None, reason: str | None = None,
                   involved_kind: str | None = None, involved_name: str | None = None,
                   event_type: str | None = None) -> list[EventSummary]: ...
```

The log enhancements to `get_logs_for_pod_and_container` map onto replay as
follows. `previous=True` serves the pod record's `previous_logs` entry for the
container, or behaves as the real tool does with no previous instance when the
key is absent. `tail=N` returns the last `N` lines of the stored text.
`since_seconds` is honoured only when the stored lines carry a parseable leading
timestamp, and is otherwise ignored rather than applied approximately — replay
must not return an empty log for a filter it cannot actually evaluate.
`get_logs_for_job` / `get_logs_for_cronjob` resolve to the captured logs of the
relevant pod using the same job/owner lookup the real tools use.

Internal structure built at load time:
- `_pods: dict[tuple[str, str], PodRecord]` — keyed by `(namespace, name)`
- `_deployments: dict[str, list[DeploymentSummary]]` — keyed by namespace, `""` for all
- `_replicasets: list[ReplicaSetSummary]` — flat; filtered and *then* sorted at query
  time, because the `(namespace, owner_deployment, revision)` ordering has to survive
  filtering
- `_services: dict[str, list[ServiceSummary]]` — same
- `_configmaps`, `_statefulsets`, `_cronjobs`, `_jobs`, `_pvcs` — same namespace-keyed
  pattern as `_services`
- `_events: list[EventRecord]` — flat; `get_events` / `get_pod_events` filter it

#### Replay clock

Time helpers are called at query time, not load time, so ages advance correctly:

```python
def _elapsed(self) -> float:
    if self._frozen:
        return 0.0
    return (datetime.now(UTC) - self._server_start_time).total_seconds()

def _age(self, seconds: float) -> timedelta:
    return timedelta(seconds=seconds + self._elapsed())

def _dt(self, offset_seconds: float) -> datetime:
    return self._server_start_time - timedelta(seconds=offset_seconds)
```

`_frozen` is off by default: an advancing clock is what makes an interactive
session feel like a cluster, and it keeps `age` and `last_restart` moving the
way someone poking at the mock expects.

**It is wrong for an automated suite, so it has to be switchable.** Two problems
appear the moment a capture becomes a graded test fixture:

- *Across runs*, the same scenario yields different ages, so an expectation
  written as "restarts began about five minutes ago" passes or fails depending
  on how long the harness took to get there.
- *Within a run*, a session spanning several minutes sees ages drift between its
  first tool call and its last. An agent that reads pod summaries early and
  events late can derive an interval that never existed — a self-inflicted
  version of exactly the inconsistency such a test exists to detect.

Frozen mode pins `elapsed` at zero, so every query returns precisely the ages
recorded at `captured_at` and every derived interval is stable. Datetime
reconstruction is unaffected: it is already anchored to `server_start_time` and
so is internally consistent either way.

### 2. `k8s-capture-state` CLI (`src/k8stools/capture.py`)

```
usage: k8s-capture-state [-h] [--namespace NS [NS ...]] [--output FILE]
                          [--no-logs] [--max-log-lines N] [--no-previous-logs]
```

Behavior:
1. Initializes the K8s client (respects `KUBECONFIG`).
2. Calls all `get_*` tools to collect state — including the 1.1.0 additions
   (`configmaps`, `statefulsets`, `cronjobs`, `jobs`, `pvcs`), the 1.2.0 addition
   (`replicasets`), and a namespace/cluster-wide `get_events` sweep. If
   `--namespace` is given, only captures namespaced resources (pods/deployments/
   replicasets/services/configmaps/statefulsets/cronjobs/jobs/pvcs/events) in those
   namespaces; namespaces and nodes are always captured in full.
3. For each pod and each container, calls `get_logs_for_pod_and_container` unless `--no-logs`. Default log cap: 1000 lines (override with `--max-log-lines`).
4. For each container whose `restart_count > 0`, calls the same tool with
   `previous=True` and stores the result under `previous_logs`, unless
   `--no-previous-logs`. A failure here is expected and non-fatal — the previous
   instance may have been garbage-collected, and the real tool raises rather than
   returning empty — so catch it, omit the key, and carry on. But **count the
   omissions and report them at the end**: a crash-loop capture that lost its
   previous logs looks identical to one that never had any, and the scenario is
   worth little without them.
5. Records `captured_at = datetime.now(UTC)`.
6. Serializes to JSON using a custom Pydantic serializer (see below) and writes to `--output` (default: `k8s-state-<timestamp>.json`).

**Serialization**: Pydantic v2 custom serializer registered on `timedelta` fields converts them to `float` seconds. `datetime` fields on `ContainerStateRunning` / `ContainerStateTerminated` are converted to offset seconds from `captured_at`. The capture function owns this transformation — `MockState.from_file` is the inverse.

New entry point in `pyproject.toml`:
```toml
k8s-capture-state = "k8stools.capture:main"
```

### 3. Refactored `mock_tools.py`

Replace the module-level `_MOCK_DATA` dict and parallel function bodies with a `MockState` delegate:

```python
_STATE: MockState | None = None

def load_mock_state(path: Path | None = None) -> None:
    global _STATE
    _STATE = MockState.from_file(path) if path else MockState.from_builtin()

def get_namespaces() -> list[NamespaceSummary]:
    assert _STATE is not None, "call load_mock_state() first"
    return _STATE.get_namespaces()

# ... same pattern for all other tools
```

`_STATE` is initialized lazily on first call if `load_mock_state` hasn't been called (uses `from_builtin()`), so existing callers that import `mock_tools` directly continue to work without changes.

Docstrings are still mirrored from `k8s_tools` as today.

### 4. MCP server changes (`src/k8stools/mcp_server.py`)

Add `--state-file PATH` argument. When provided, calls `load_mock_state(path)` at startup instead of `load_mock_state()` (builtin). The existing `--mock` flag becomes shorthand for `--state-file` with the builtin fixture path — or equivalently, `--mock` remains and `--state-file` is a new flag that implies mock mode.

```
--mock                 Use built-in mock state (OTel Demo snapshot)
--state-file FILE      Use captured state from FILE (implies mock mode)
--state-time {advancing,frozen}
                       Replay clock (default: advancing). `frozen` pins every age at
                       its captured value so repeated runs are identical; see
                       [Replay clock](#replay-clock).
```

`--state-time` is meaningful only with a state file. Reject it otherwise rather
than ignoring it: a suite that believes it froze the clock and did not would
produce flaky results with no visible cause.

---

## Migration of existing mock data

The current hardcoded data in `mock_tools.py` was captured from a Minikube instance running the OpenTelemetry Demo. Migration steps:

1. Write a one-off script `scripts/migrate_mock_data.py` that instantiates the existing Pydantic objects from `_get_static_mock_data()` and serializes them to the new JSON format using the capture serializer.
2. Save the output as `tests/fixtures/otel-demo.json`.
3. Update `MockState.from_builtin()` to load this file.
4. Remove `_get_static_mock_data()` and the hardcoded data from `mock_tools.py`.

The migration script is a one-shot tool; it can be deleted after the fixture file is committed.

---

## Test fixture organization

- `tests/fixtures/otel-demo.json` — migrated builtin snapshot (OTel Demo on Minikube)
- Additional fixtures can be added by any consumer of the library in their own repo; only `otel-demo.json` lives here.

Tests that currently use `MockK8S` / `MockAppsV1Api` in `test_k8s_tools.py` are unaffected — they test `k8s_tools.py` directly and bypass `mock_tools` entirely.

New tests to add:
- `tests/test_mock_state.py` — unit tests for `MockState`: load from file, namespace filtering, time adjustment math, missing pod returns empty list, etc. Add:
  - `get_replicaset_summaries` ordering — revision ascending within a deployment,
    after both filters, `null` revisions last. Assert against a fixture whose
    capture order is deliberately shuffled, so that echoing the file fails.
  - `previous=True` serves `previous_logs`, and behaves like no-previous-instance
    when the key is absent.
  - frozen mode returns identical ages from two queries separated by a slept
    interval; advancing mode does not.
- `tests/test_capture.py` — unit tests for the serializer/deserializer round-trip (no cluster needed). Add a
  round-trip for `replicasets` (including `owner_deployment: null` / `revision: null`)
  and one asserting that **relative intervals survive**: a deployment and its newest
  replica set captured 8 days and 7h34m old must still be 8 days and 7h34m apart
  after a reload at an arbitrary later time.

---

## Open decisions

**Log capture scope**: Capturing logs for every container in a large cluster can produce very large JSON files. The default cap of 1000 lines per container is a reasonable starting point. If this proves too large or too small in practice, it can be tuned. Agents that need to test log-based RCA should use `--max-log-lines` explicitly.
Previous-instance logs roughly double the log payload for a cluster in which many
containers are restarting — which is exactly the cluster worth capturing.

**Should capture redact?** The MCP server has `--no-redact`, so redaction is
already a live concern, and a capture file is far more portable than a cluster:
it gets committed to git, attached to issues, and shared. Capturing through the
same redaction path the server uses is the safer default, at the cost of making
captures useless for testing redaction itself. Unresolved — worth deciding before
the first capture is committed anywhere.

---

## New and changed files

| File | Change |
|---|---|
| `src/k8stools/mock_state.py` | New — `MockState` class, incl. `get_replicaset_summaries` and the frozen replay clock |
| `src/k8stools/capture.py` | New — `k8s-capture-state` CLI, incl. `replicasets` and previous-instance logs |
| `tests/fixtures/otel-demo.json` | New — migrated builtin mock data |
| `scripts/migrate_mock_data.py` | New (temporary) — one-shot migration script |
| `src/k8stools/mock_tools.py` | Refactor — delegate to `MockState` |
| `src/k8stools/mcp_server.py` | Add `--state-file` and `--state-time` arguments |
| `pyproject.toml` | Add `k8s-capture-state` entry point |
| `tests/test_mock_state.py` | New — `MockState` unit tests |
| `tests/test_capture.py` | New — serialization round-trip tests |
