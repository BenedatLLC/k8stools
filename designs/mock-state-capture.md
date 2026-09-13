# Mock State Capture & Replay

## Status — IMPLEMENTED (2026-09-13)

Built to this design. All nine files in [New and changed files](#new-and-changed-files)
exist, the whole tool surface (19 tools) replays from a capture, and `--mock` now
serves the migrated OTel-Demo snapshot rather than `_get_static_mock_data()`.

The design below is kept as written; this section records where the build
deviated from it and why, and what changed for existing callers.

### Deviations from the design as written

1. **The built-in fixture lives at `src/k8stools/fixtures/otel-demo.json`, not
   `tests/fixtures/`.** `MockState.from_builtin()` is production code — it is what
   `k8s-mcp-server --mock` serves — so the fixture has to ship in the wheel, and
   `tests/` does not. (Verified: it is present in the built wheel.)

2. **Null revisions sort *first*, not last** — *resolved 2026-09-13: keep this
   behavior.* An earlier draft of this design said "last";
   `k8s_tools.get_replicaset_summaries` has always sorted them first (keying on
   `revision if revision is not None else -1`). Since the point of that section is
   that replay must not violate a guarantee the real tool makes, the real tool
   wins and `MockState` matches it. The design body above is corrected, and both
   docstrings now state the ordering and what a missing revision means.

3. **Pod records carry a `labels` map**, which the design's pod record does not
   mention. `get_logs_for_job` resolves a Job to its pod through the `job-name`
   label; labels are not part of any tool's return value, so a capture built only
   from tool output could not answer that call on replay. The capture reads them
   from the pod list directly.

4. **ConfigMap records store `key_count` and `data_size`** alongside the content
   rather than deriving both from it, as the design suggests. `data_size` counts
   the bytes of *binary* values and `get_configmap` returns only their key names,
   so the derivation is not possible from captured data. The derivation is kept as
   a fallback for hand-written fixtures.

5. **The replay clock quantizes elapsed time to whole seconds and samples it once
   per query.** The design's `_elapsed()` returns microsecond-precise time sampled
   per call, which breaks the interval guarantee the design leans on: two
   resources captured at the same instant come back tens of microseconds apart,
   and a deployment can be reported as younger than its own replica set. Kubernetes
   ages are meaningful at second granularity (`kubectl` prints `8d`, `7h34m`), so
   the extra precision was false precision with a real cost. Frozen mode is
   unaffected, and cross-query drift in advancing mode is unchanged — that is
   inherent to advancing time and is what frozen mode is for.

6. **`scripts/migrate_mock_data.py` was run and then deleted**, as the design
   permits. It could not survive its own migration: its input,
   `mock_tools._get_static_mock_data()`, is removed by the change it performs, so
   keeping it would leave a script in the tree that raises `AttributeError` on
   sight. What it did is recorded below instead.

### Added during implementation, not in the original design

These are capabilities the design did not call for, added because implementing or
using the thing made the need obvious:

7. **Captures can be gzipped.** An output name ending in `.gz` is written
   compressed (3.3 MB → 364 KB on a real 38-pod cluster), and reads detect gzip by
   magic bytes rather than file name, so a renamed capture still loads.

   **Plain `.json` remains the default, and is the right choice for anything
   checked into git.** Git already stores blobs compressed *and* deltas successive
   versions of a text file against each other, which it cannot do for a gzip blob.
   Measured by committing two captures of the same cluster in sequence: the first
   commit costs about the same either way (404 KB vs 388 KB), but updating the
   fixture later costs **+61 KB as `.json` and +358 KB as `.gz`** — so a `.gz`
   fixture grows the repo roughly 6x faster per re-capture. This is also why
   captures are written indented rather than compact: it gives git something to
   delta. And why git-lfs is likely the wrong tool here — LFS stores every version
   whole, giving up exactly that delta compression. Reach for `.gz` when the file
   travels on its own: attached to an issue, or in object storage.

8. **Redaction is applied per tool result, not once to the assembled capture.**
   See [Redaction](#5-redaction) — this turned out to be a correctness matter, not
   a stylistic one.

9. **`--no-logs` skips previous-instance logs too.** As first built it skipped only
   the current instance and still captured the previous one, uncapped, which made a
   "structure-only" capture *larger* than a normal one (3.6 MB vs 1.4 MB).
   Previous-instance logs are logs; `--no-previous-logs` remains the finer-grained
   option for keeping the current instance and dropping the previous.

### What the migration did to the mock data

Beyond re-encoding, four deliberate changes, all of which made the fixture a more
coherent cluster than the hardcoded data was:

- **Events were unified into one flat list.** The old mock kept a per-pod list and
  a separate cluster-wide one, and they disagreed: the ad pod's `BackOff` event was
  4 minutes old through `get_pod_events` and 1 minute old through `get_events`. One
  list, one age. The visible consequence is that a cluster-wide `get_events` sweep
  now also returns the ad pod's `Normal`/`Pulled` event, which it previously could
  not see.
- **The ad pod gained previous-instance logs.** It is `OOMKilled` with 93 restarts
  and its captured logs show only JVM startup — precisely the case `previous=True`
  exists for. Without a `previous_logs` entry the built-in fixture could not
  exercise the tool at all.
- **The cleanup Job gained its pod.** The old data had a Job that completed a
  minute ago but no pod for it, so `get_logs_for_job` / `get_logs_for_cronjob`
  would have had nothing to resolve to on replay.
- **Deployments now strictly predate their own replica sets.** The old data gave
  each deployment and its revision-1 replica set the *identical* age, which held
  together only because both were the same literal `timedelta`. Any clock that
  samples time twice orders them arbitrarily. The revision-1 replica sets are aged
  down by a second so the relationship lives in the data rather than in a tie.

The fixture is committed **unredacted** (`"redacted": false`). Its one
secret-shaped value is AWS's own documented example key, present so that redaction
can be seen working end to end through the mock server — `--mock` with redaction on
returns `[REDACTED]` for it, which a test asserts.

### Behavior changes for existing callers of `mock_tools`

`mock_tools` used to synthesize a plausible answer for any pod name it was handed.
It now serves one captured cluster, so **a pod that is not in the capture does not
exist for any tool**: `get_pod_container_statuses` returns `[]`, `get_pod_events`
returns `[]`, and `get_pod_spec` / `get_logs_for_pod_and_container` raise
`K8sApiError` the way the real tools do on a 404. This is the point of the change —
the old behavior let an agent "discover" pods that appeared in no listing — but it
is a breaking change for any caller that relied on the synthesis. Four tests in
`tests/test_mock_tools.py` asserted the old behavior and were rewritten.

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
  "redacted": true,
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

`redacted` records which mode the capture was taken in — see
[Redaction](#redaction). It exists so a consumer can tell without guessing;
redaction is lossy and one-way, so this cannot be inferred from the content.

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
  "labels": {"app": "ad", "pod-template-hash": "647b4947cc"},
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

  Only the Deployment controller writes that annotation, so the two nulls travel
  together: a ReplicaSet with no revision has no owning deployment either, and
  appears in neither `kubectl rollout history` nor a `deployment`-filtered call.

**Ordering is part of the contract, not a presentation detail.** The real tool
documents that results are grouped by namespace and owning deployment and,
within each deployment, **sorted by revision, oldest first** — so that when
filtered to one deployment "the last entry is that deployment's current
revision." Callers rely on that sentence. `MockState.get_replicaset_summaries`
must reproduce the ordering rather than echo capture order, or a replayed
capture quietly violates a guarantee the tool's own docstring makes. Sort at
query time, after filtering, on `(namespace, owner_deployment, revision)`.

**`null` revisions sort first**, ahead of revision 1, matching what
`k8s_tools.get_replicaset_summaries` already does (it keys on `revision if
revision is not None else -1`). An earlier draft of this design said "last";
that was decided in favor of the real tool's existing behavior on 2026-09-13,
and both docstrings now state it. Since a null revision implies a null
`owner_deployment`, these are only ever visible in an unfiltered listing.

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
    def from_file(cls, path: Path, frozen: bool = False) -> MockState: ...
    @classmethod
    def from_builtin(cls, frozen: bool = False) -> MockState: ...
    # loads src/k8stools/fixtures/otel-demo.json; gzipped captures are read
    # transparently, detected by magic bytes rather than by file name

    # Query methods — same signatures as k8s_tools counterparts
    def get_namespaces(self) -> list[NamespaceSummary]: ...
    def get_node_summaries(self) -> list[NodeSummary]: ...
    def get_pod_summaries(self, namespace: str | None = None) -> list[PodSummary]: ...
    def get_pod_container_statuses(self, pod_name: str, namespace: str) -> list[ContainerStatus]: ...
    def get_pod_events(self, pod_name: str, namespace: str) -> list[EventSummary]: ...
    def get_pod_spec(self, pod_name: str, namespace: str) -> dict[str, Any]: ...
    def get_logs_for_pod_and_container(
        self, pod_name: str, namespace: str, container_name: str | None = None,
        tail: int | None = None, since_seconds: int | None = None,
        previous: bool = False
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
usage: k8s-capture-state [-h] [--namespace NS [NS ...]] [-o FILE] [--no-logs]
                         [--max-log-lines MAX_LOG_LINES] [--no-previous-logs]
                         [--no-redact]
                         [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}]
```

Behavior:
1. Initializes the K8s client (respects `KUBECONFIG`) and resolves the redaction
   mode with `redaction_enabled(no_redact_flag=args.no_redact)` — the same helper
   the MCP server uses, so `--no-redact` and `K8STOOLS_REDACT=0` behave identically
   in both. See [Redaction](#redaction).
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
5. Applies `redact_object` to the assembled state unless redaction is disabled,
   and records the mode in the file's `redacted` field.
6. Records `captured_at = datetime.now(UTC)`.
7. Serializes to JSON using a custom Pydantic serializer (see below) and writes to `--output` (default: `k8s-state-<timestamp>.json`).

**Serialization**: Pydantic v2 custom serializer registered on `timedelta` fields converts them to `float` seconds. `datetime` fields on `ContainerStateRunning` / `ContainerStateTerminated` are converted to offset seconds from `captured_at`. The capture function owns this transformation — `MockState.from_file` is the inverse.

New entry point in `pyproject.toml`:
```toml
k8s-capture-state = "k8stools.capture:main"
```

### 3. Refactored `mock_tools.py`

Replace the module-level `_MOCK_DATA` dict and parallel function bodies with a `MockState` delegate:

```python
_STATE: Optional[MockState] = None

def load_mock_state(path: Optional[Path] = None, frozen: bool = False) -> MockState:
    global _STATE
    _STATE = MockState.from_file(path, frozen=frozen) if path \
        else MockState.from_builtin(frozen=frozen)
    return _STATE

def _state() -> MockState:
    """The loaded state, loading the built-in capture on first use."""
    global _STATE
    if _STATE is None:
        _STATE = MockState.from_builtin()
    return _STATE

def get_namespaces() -> list[NamespaceSummary]:
    return _state().get_namespaces()

# ... same pattern for all other tools
```

`_STATE` is initialized lazily on first call if `load_mock_state` hasn't been called
(uses `from_builtin()`), so existing callers that import `mock_tools` directly
continue to work without changes. As built, `load_mock_state` takes a `frozen` flag
and returns the state, so a test suite can load a capture and assert against it
without reaching for the module global.

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

### 5. Redaction

**Capture applies whatever redaction the tools would have applied.** Redaction is
on by default, so a capture is redacted by default; `--no-redact` (or
`K8STOOLS_REDACT=0`) captures exactly what the tools return. Same flag, same env
var, same `redaction_enabled()` helper as the MCP server, so there is one rule to
remember rather than two.

**Capture has to apply it explicitly — it does not get it for free.** Redaction
lives in `redaction.py` and is applied by `mcp_server.py` at the *MCP output
boundary*, by wrapping each tool with `wrap_with_redaction`. The capture CLI calls
the `get_*` functions directly, as a library consumer, and `redaction.py`'s own
docstring is explicit that library consumers bypass the redaction layer and get
raw values. So without a deliberate `redact_object` pass, `k8s-capture-state`
would write unredacted secrets to disk **while the server in front of the same
cluster was redacting them** — the one combination nobody would expect.

Three consequences worth knowing before the first capture is committed anywhere:

- **Redaction is one-way.** `REDACTED` replaces the value; the original is gone.
  Running a replay server with `--no-redact` over a redacted capture does *not*
  restore anything, and should not be read as doing so. If a scenario genuinely
  needs the real values, it has to be re-captured with `--no-redact`.
- **Double redaction is a no-op, but the count is not.** A redacted capture
  served by a redacting server passes through the pass twice. `[REDACTED]`
  matches no value-shape pattern and sits under the same key names, so nothing
  further changes — but the server reports *0* new redactions where the live
  cluster reported *N*. Only relevant if something ever asserts on that count.
- **Structure survives, so replay fidelity does not suffer.** Redaction never
  drops a field; it substitutes a marker, which is what lets an agent tell
  "absent" from "hidden". A redacted capture therefore has the same shape as the
  cluster it came from, and an agent reasoning over it sees the same fields.

**Two redaction bugs this work surfaced** (both fixed 2026-09-13, found by
capturing a real 38-pod cluster and reading every redaction):

- *Redaction is applied per tool result, not once to the assembled capture.* An
  envelope-wide pass subjects capture-internal structure to the key-name
  heuristic, which the MCP output boundary never sees. Captured logs are stored in
  a dict keyed by container name, so a container named `valkey-cart` matched the
  heuristic's `key` pattern and its **entire log — current and previous — was
  replaced by the marker**. The live server does not do this, because it returns
  the log as a bare string. Per-result redaction is also what this design's own
  words ask for: a capture holds whatever the tools would have returned, and the
  tools never return the envelope. Pod labels, likewise capture-internal, are not
  redacted at all.
- *`redaction.py`'s key-name rule matched substrings and treated a bare `key` as
  sensitive.* Of 139 redactions in that capture, 3 were secrets. The fix (whole-word
  matching, plus exempting a field named exactly `key` as Kubernetes structural)
  brought it to 5 while keeping all 3. See the module docstring; the residual two
  are `topology_key`.

**In practice this rarely bites**, because the clusters worth capturing are test
clusters — the OTel demo has no real credentials to leak. The default is chosen
for the case where that assumption is wrong, which is the only case where it
matters: a capture file is far more portable than cluster access. It gets
committed to git, attached to issues, and passed around, long after whoever made
it remembers which cluster it came from.

The cost of the default is that captures cannot be used to test redaction
itself. That is fine — redaction has its own unit tests, and testing it through a
capture would test the capture path, not the redaction path.

---

## Migration of existing mock data

The current hardcoded data in `mock_tools.py` was captured from a Minikube instance running the OpenTelemetry Demo. Migration steps:

1. Write a one-off script `scripts/migrate_mock_data.py` that instantiates the existing Pydantic objects from `_get_static_mock_data()` and serializes them to the new JSON format using the capture serializer.
2. Save the output as `src/k8stools/fixtures/otel-demo.json` (see deviation 1 —
   the design originally said `tests/`, but `--mock` serves this file so it must
   ship in the wheel).
3. Update `MockState.from_builtin()` to load this file.
4. Remove `_get_static_mock_data()` and the hardcoded data from `mock_tools.py`.

The migration script is a one-shot tool; it can be deleted after the fixture file is
committed. It was — see deviation 6, and "What the migration did to the mock data"
above for what it changed along the way.

---

## Test fixture organization

- `src/k8stools/fixtures/otel-demo.json` — migrated builtin snapshot (OTel Demo on
  Minikube), shipped in the wheel because `--mock` serves it.
- Additional fixtures can be added by any consumer of the library in their own repo; only `otel-demo.json` lives here.

Tests that currently use `MockK8S` / `MockAppsV1Api` in `test_k8s_tools.py` are unaffected — they test `k8s_tools.py` directly and bypass `mock_tools` entirely.

New tests to add:
- `tests/test_mock_state.py` — unit tests for `MockState`: load from file, namespace filtering, time adjustment math, missing pod returns empty list, etc. Add:
  - `get_replicaset_summaries` ordering — revision ascending within a deployment,
    after both filters, `null` revisions first. Assert against a fixture whose
    capture order is deliberately shuffled, so that echoing the file fails.
  - `previous=True` serves `previous_logs`, and behaves like no-previous-instance
    when the key is absent.
  - frozen mode returns identical ages from two queries separated by a slept
    interval; advancing mode does not.
- `tests/test_capture.py` — unit tests for the serializer/deserializer round-trip (no cluster needed). Add:
  - a capture assembled from objects carrying a secret-shaped value (a JWT, an
    `AWS_SECRET_ACCESS_KEY` env var) is written redacted by default, and raw under
    `--no-redact`, with `redacted` set to match in both cases. This is the test
    that would catch the library-consumer bypass described under
    [Redaction](#redaction), which no existing test covers because no existing
    caller has this shape.
  - a
  round-trip for `replicasets` (including `owner_deployment: null` / `revision: null`)
  and one asserting that **relative intervals survive**: a deployment and its newest
  replica set captured 8 days and 7h34m old must still be 8 days and 7h34m apart
  after a reload at an arbitrary later time.

---

## Open decisions

**Log capture scope**: Capturing logs for every container in a large cluster can produce very large JSON files. The default cap of 1000 lines per container is a reasonable starting point. If this proves too large or too small in practice, it can be tuned. Agents that need to test log-based RCA should use `--max-log-lines` explicitly.
Previous-instance logs roughly double the log payload for a cluster in which many
containers are restarting — which is exactly the cluster worth capturing.

*(The redaction question raised here on 2026-09-12 was decided the same day; see
[Redaction](#redaction) under Components.)*

---

## New and changed files

As built:

| File | Change |
|---|---|
| `src/k8stools/mock_state.py` | New — `MockState`, the temporal codec, and the replay clock |
| `src/k8stools/capture.py` | New — `k8s-capture-state` CLI, incl. `replicasets`, previous-instance logs, and an explicit `redact_object` pass |
| `src/k8stools/fixtures/otel-demo.json` | New — migrated builtin mock data (moved out of `tests/`; see deviation 1) |
| `scripts/migrate_mock_data.py` | Added, run, and deleted (see deviation 6) |
| `src/k8stools/mock_tools.py` | Rewritten — delegates to `MockState` |
| `src/k8stools/mcp_server.py` | Added `--state-file` and `--state-time` arguments |
| `pyproject.toml` | Added `k8s-capture-state` entry point |
| `tests/test_mock_state.py` | New — 29 `MockState` unit tests |
| `tests/test_capture.py` | New — 16 serialization, redaction and compression tests |
| `src/k8stools/redaction.py` | Fixed — whole-word name matching and a structural `key` exemption (see [Redaction](#5-redaction)) |
| `tests/test_redaction.py` | Updated — 23 → 51 tests for the tightened name rule |
| `tests/test_mock_tools.py` | Updated — four tests asserted the removed synthesis |
| `tests/test_new_tools.py` | Updated — one test asserted the split event lists |
| `README.md`, `docs/ROADMAP.md`, `CLAUDE.md` | Documented the CLI, the flags, and the new layout |
