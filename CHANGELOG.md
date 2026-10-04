# Changelog

All notable changes to k8stools. Versions follow [semantic versioning](https://semver.org/);
dates are release-tag dates.

## 2.3.0 — 2026-10-03

### Added
- **`conditions_since` on nodes** (issue #9). `get_node_summaries` now reports,
  for each node condition, the time since its status last changed (the API's
  `lastTransitionTime`, which was dropped), next to the unchanged `conditions`
  map. A condition with no transition time is left out. `print_node_summaries`
  gains a READY-SINCE column.
- The tool description says what it does not show: a restart in which the node
  comes back Ready without being marked NotReady or Unknown in between does not
  reset it. On a minikube cluster stopped and started with its control plane,
  `Ready` stayed "True since the node was created", 167 days, across a reboot.
  It points to the node's `Starting`/`Rebooted` events and the start times of
  kube-proxy and the control-plane containers for dating a node's last start.
- Captures store it as `conditions_since_seconds` and replay advances it like
  every other age; captures from before 2.3.0 replay it as an empty map. (The
  capture format had no handling for a map of durations: without it the values
  would have been replayed as fixed spans that never advanced.)

## 2.2.1 — 2026-09-29

### Documentation
- **Event records can lag behind what they count** (follow-up to issue #8). The
  kubelet counts every occurrence but, by default, writes at most one update per
  object and event type every 5 minutes once a burst of 25 is used up (client-go's
  event spam filter). A crash-looping pod's `Created` record read `last_seen`
  29 minutes while the container had restarted 46 seconds earlier, then its count
  jumped by six in one update. The `get_pod_events` and `get_events` descriptions
  now say so: `count / (first_seen - last_seen)` still gives the rate, since both
  lag together, but `last_seen` is not the time of the last restart (use the
  container status or `PodSummary.last_restart`), and counts for different reasons
  can differ by a few for the same restarts.
- `PodSummary.last_restart` is described as what it is: time since a container
  last *terminated*. For a container waiting in CrashLoopBackOff, that is its last
  crash, not a restart.

## 2.2.0 — 2026-09-28

### Added
- **`count` and `first_seen` on events** (issue #8). `get_pod_events` and
  `get_events` now report how many occurrences Kubernetes combined into each
  event record, and how long ago the first one was, next to `last_seen`. For a
  crash-looping container this gives the restart rate directly: on one cluster,
  a `Created` count of 579 over 2 days 8 hours is a restart every ~5.8 minutes,
  a number agents kept getting wrong from the other durations. The tool
  descriptions explain the window the count covers and warn that `BackOff`
  counts are several times the number of restarts. `print_pod_events` and
  `print_events` gain a COUNT column.
- Captures store both fields; captures from before 2.2.0 replay them as `None`.

### Fixed
- **Replayed `captured_at` was on a different clock** (issue #7).
  `get_cluster_info` returned the date in the capture file while every other
  replayed timestamp was re-anchored to the server's start, so a pod killed
  "5 minutes ago" appeared beside a capture taken a month ago. `captured_at` is
  now the replay's anchor (the server's start time); the recorded date is in the
  startup log and on `MockState.captured_at`.
- **Replayed logs kept their original timestamps.** Log line timestamps were not
  re-anchored, so they contradicted every other replayed time, and
  `since_seconds` (compared against the replay's "now") returned an empty log for
  any capture older than the window, as if the container had logged nothing.
  The kubelet's timestamp prefix on each line is now moved like every other
  datetime, keeping its original precision and format. Timestamps an application
  writes inside its messages are left as recorded.
- **Events recorded through `events.k8s.io/v1` had no time.** Their core/v1 view
  leaves `lastTimestamp` empty and records the time in `eventTime` (and
  repetition in `series`), so `last_seen` came back `None`. Both are now read
  when the older fields are empty.

## 2.1.0 — 2026-09-28

### Added
- **Cluster selection.** `k8s-mcp-server` and `k8s-capture-state` take
  `--kubeconfig PATH` and `--context NAME`; the context can also be pinned with
  the `K8STOOLS_CONTEXT` environment variable. The order is flag, then
  `KUBECONFIG` / `K8STOOLS_CONTEXT`, then `~/.kube/config` and its
  `current-context`. The MCP server binds at startup and logs which cluster it is
  answering from. Direct Python callers can call `k8s_tools.configure(kubeconfig,
  context)`. See README, "Selecting a cluster".
- **`get_cluster_info` tool.** Reports which cluster the tools answer from: source
  (`kubeconfig`, `in-cluster` or `capture`), context, API server URL, kubeconfig
  path and server version. It never returns credentials. Replayed, it reports
  `source="capture"` and when the capture was taken, so an agent can tell replayed
  evidence from a live cluster.
- **Captures record their cluster**: the context, server URL and server version
  are stored in a new optional `cluster` key, which `get_cluster_info` reads on
  replay. The kubeconfig path is left out: it names a file on the capturing
  machine. Older captures still load; older readers ignore the key.
- **`ContainerStateTerminated.ran_for`**: how long a terminated container instance
  ran (`finished_at - started_at`), as a whole-second duration. It gives the
  number an agent otherwise has to subtract, and often mixes up with how much
  time the container's log covers or how often it restarts.
  `get_pod_container_statuses` documents what it is not.
- `k8s_tools.Interval`, a duration type for a fixed span between two moments, next
  to `Duration` (an age). Capture replay advances ages with the replay clock but
  reads intervals back unchanged.

### Changed
- **A cluster selection that cannot be loaded is an error.** A given
  `--kubeconfig`, `--context` or `K8STOOLS_CONTEXT` that fails to load (missing
  file, misspelled context) stops the MCP server and fails a capture, with the new
  `K8sClusterSelectionError`. Previously every load failure fell back to in-cluster
  config, which inside a pod meant silently binding to the pod's own cluster. With
  no selection, the in-cluster fallback is unchanged, and the server still starts
  when no configuration can be loaded.
- **All tools share one cluster binding, made once.** The core, apps and batch API
  clients used to load the kubeconfig separately, each on first use, so a
  `kubectl config use-context` between a server's first pod query and its first
  deployment query left it answering from two clusters. A running server now
  ignores later changes to the kubeconfig's current context.
- **k8stools no longer sets the kubernetes client's process-wide default
  configuration.** Its binding uses a configuration of its own. Python code that
  relied on k8stools having loaded the kubeconfig for its own kubernetes calls
  must now load it itself (`kubernetes.config.load_kube_config()`).
- `--kubeconfig` and `--context` are rejected with `--mock` or `--state-file`,
  which serve a capture rather than a cluster.
- `k8s-mcp-client` passes `K8STOOLS_CONTEXT` through to the server it starts, as
  it already did `KUBECONFIG`.

## 2.0.4 — 2026-09-19

### Fixed
- **Pod logs arrived as a single unusable `b'...'` line** (issue #6). Since
  kubernetes 36 the log endpoint's body reaches the client as `bytes`, and the
  generated client turned it into `repr(bytes)`. Logs are now decoded where the
  API is called, with invalid bytes replaced rather than failing the call, which
  fixes the log tools, the Job/CronJob log readers and `k8s-capture-state` at once.
- Redaction handles `bytes` values explicitly.

### Documentation
- `previous=True` has three Kubernetes behaviors that look like bugs (identical
  output during CrashLoopBackOff, a restart shifting the window between calls, and
  a reclaimed log answering 200 with an error message). They are documented in the
  tool's description and the README rather than changed.

## 2.0.3 — 2026-09-17

### Fixed
- **Duration fields failed strict schema validation.** Ages serialized with
  fractional seconds (`"P2DT5M27.978616S"`), which the `format: "duration"` grammar
  their own schema declares does not allow, so an MCP client that validates
  structured output (ajv does) rejected every list tool on every row. Durations are
  now whole, non-negative seconds (`k8s_tools.Duration`); a cluster clock ahead of
  ours no longer yields a negative age.
- **Redaction blacked out whole values.** One secret-shaped substring replaced an
  entire ConfigMap value, making config-as-JSON unreadable. Tokens (AWS keys, JWTs)
  are now replaced where they sit. PEM private keys are still removed whole. A value
  that parses as JSON is searched field by field, which also catches a
  `{"password": ...}` inside it that was previously missed.

## 2.0.2 — 2026-09-12

Includes the 2.0.1 packaging fix, which was tagged but not published.

### Changed
- `k8s-mcp-client` always starts the server with the current interpreter
  (`python -m k8stools.mcp_server`) instead of looking for `k8s-mcp-server` next
  to `argv[0]`, and no longer prints a leftover debug line.
- `twine` and `build` dropped from the dev dependencies (uv replaces both).

### Fixed
- `rich` and `pydantic` are declared as runtime dependencies. `k8s-mcp-client`
  imported `rich`, which was only a dev dependency, so it failed with
  `ModuleNotFoundError` on any install from PyPI (2.0.0 and all earlier releases).

## 2.0.1 — 2026-09-12 (tagged, not published)

- The packaging fix listed under 2.0.2.

## 2.0.0 — 2026-09-12

### Added
- **Mock state capture and replay.** `k8s-capture-state` snapshots a live cluster
  into one JSON file, and `k8s-mcp-server --state-file FILE` replays it through all
  19 tools, so agents can be tested against realistic scenarios with no cluster.
  `--state-time frozen` pins every age for repeatable test suites.
  Previous-instance logs are captured, and captures can be written and read
  gzipped. Design: `designs/mock-state-capture.md`.

### Changed (breaking)
- `mock_tools` serves one captured cluster (the built-in OTel Demo snapshot by
  default): a pod not in the capture does not exist for any tool, instead of
  getting a synthesized answer.
- Redaction's key-name rule matches whole words and exempts a field named exactly
  `key`, which in the Kubernetes API is always structural. On a real 38-pod cluster
  this went from 139 redactions to 5, still catching all 3 real secrets.

## 1.2.0 — 2026-09-11

### Added
- `get_replicaset_summaries`: a Deployment's replica sets, which are its change
  history, sorted oldest revision first. Filtered to one deployment, the last entry
  is its current revision, so "when did this last change, and what changed" is one
  call.

## 1.1.0 — 2026-09-06

Delivered the Priority-1 batch from a heavy RCA user's feature request, plus the
cross-cutting redaction ask, and upgraded core dependencies.

### Added
- `get_configmap_summaries` / `get_configmap`: ConfigMap listing and full-content
  read.
- `get_cronjob_summaries` / `get_job_summaries`: CronJob and Job spec and status,
  including the pod template's container images and env; `owner` links a Job back
  to its CronJob.
- `get_logs_for_job` / `get_logs_for_cronjob`: find the most recent pod of a Job,
  or the most recent run of a CronJob, and return its logs.
- `get_pvc_summaries`: PVC listing with `mounted_by` pod resolution, which surfaces
  orphaned PVCs.
- `get_events`: cluster- or namespace-wide events with server-side filtering
  (`reason`, `involved_kind`, `involved_name`, `event_type`).
- `get_statefulset_summaries`: StatefulSet summaries mirroring
  `DeploymentSummary`.
- `get_logs_for_pod_and_container`: `tail`, `since_seconds` and `previous`
  (previous-instance logs, for crash-loop analysis).
- `get_node_summaries`: `capacity`, `allocatable`, `conditions`, `taints`, `labels`.
- `get_service_summaries`: `selector`, `labels`, `annotations`.
- **Secret redaction**: one pass at the MCP server's output boundary, on by
  default, opt-out with `--no-redact` or `K8STOOLS_REDACT=0`. It matches by value
  shape (AWS keys, JWTs, PEM private keys) and by key/env-var name
  (`key|secret|token|password|credential`), replacing matches with a visible
  `[REDACTED]` marker. Direct Python callers get raw values and can call
  `redaction.redact_object` themselves.

### Changed
- `mcp` 1.12 → 2.1.1 (FastMCP → MCPServer), `kubernetes` 33.1 → 36.0.3.

## 1.0.1 — 2025-07-29

- README fixes.

## 1.0.0 — 2025-07-28

- First stable release: kubectl-style read-only tools for namespaces, nodes, pods,
  container statuses, pod events, pod specs, logs, deployments and services, with
  mock versions, as Python functions or through an MCP server.

## 0.1.0, 0.1.2 — 2025-07-21, 2025-07-23

- Initial releases (0.1.1 was tagged but not published).
