# Changelog

All notable changes to k8stools. Versions follow [semantic versioning](https://semver.org/);
dates are release-tag dates.

## 3.0.0 — 2026-10-08

### Issues fixed
- [#12](https://github.com/BenedatLLC/k8stools/issues/12): Redaction misses
  credentials embedded in args, flags and URLs.
- [#13](https://github.com/BenedatLLC/k8stools/issues/13): RCA-oriented composite
  tools: namespace health and workload report.
- [#14](https://github.com/BenedatLLC/k8stools/issues/14): Add get_hpa_summaries
  (HorizontalPodAutoscaler, autoscaling/v2).
- [#15](https://github.com/BenedatLLC/k8stools/issues/15): Service backends:
  get_endpoint_summaries (EndpointSlice) and ready counts on ServiceSummary.
- [#16](https://github.com/BenedatLLC/k8stools/issues/16): Container and node
  resource usage (metrics.k8s.io), alongside limits.
- [#17](https://github.com/BenedatLLC/k8stools/issues/17): Add
  get_ingress_summaries, with backend resolution.
- [#18](https://github.com/BenedatLLC/k8stools/issues/18): Custom resources: list
  CRDs and instance conditions (not raw objects).
- [#20](https://github.com/BenedatLLC/k8stools/issues/20): Toolsets and short tool
  descriptions: limit what each agent sees.

The work was sequenced in [#21](https://github.com/BenedatLLC/k8stools/issues/21),
with k8srca measuring each step: short descriptions cut cost per run 14% with no
misreading returned, and the composite tools in the triage group took its trap
score from 6/12 to 10/12 at half the calls and 36% less cost.

### Upgrading from 2.x
- **The MCP server serves the `investigate` toolset by default**, which leaves
  out `get_pod_events`, `get_replicaset_summaries`, `get_logs_for_job` and
  `get_logs_for_cronjob`; other tools cover each. `--toolset all` serves every
  tool, as 2.x did. In Python, `TOOLS` is unchanged.
- **Tool descriptions are short, and warnings come back in results.** Read the
  `notes` lists, and skip log lines starting `[k8stools] note:`
  (`k8s_tools.LOG_NOTE_PREFIX`) if you parse logs.
- **Redaction catches more.** Credentials in args, flags, URLs and connection
  strings that 2.x returned in full come back `[REDACTED]`. Run captures you've
  kept or shared through `k8s-capture-state --redact-file`.
- **A capture needs more read permissions:** `horizontalpodautoscalers`,
  `endpointslices`, `ingresses`, and `metrics.k8s.io` pods and nodes where
  metrics-server runs. Custom-resource types it can't read are recorded, not
  fatal. The README's role has them all.
- **Captures from 2.x still load.** Data they don't have replays as empty, as
  unknown (`None`, never 0), or with an error saying the capture predates it.


### Added
- **`get_custom_resource_definitions` and `get_custom_resource_status`** (issue
  #18).
  - **Definitions:** the custom kinds the cluster serves.
  - **Status:** each instance's `status.conditions` with the time since each
    changed, and whether its controller has acted on the latest spec
    (`observed_generation` against `generation`, read per condition when the
    status doesn't have it).
  - **Deliberately narrower than a raw reader:** no spec and no other status.
    Raw custom resources can be huge, some keep credentials in spec, and
    reading them generally needs wildcard permissions.
  - **Captures** store each type's instances (up to 500), or, where the
    capture's role can't read a type, the reason, which replay raises as live.
    Custom-resource access is granted per group, so this doesn't fail the
    capture.
  - **`--mock`:** the fixture has cert-manager's `Certificate` CRD. `shop-tls`
    isn't Ready because its Secret doesn't exist; it's the Secret the `shop`
    Ingress serves TLS from.
  - `print_custom_resource_definitions` and `print_custom_resource_status` are
    their companions.
- **`get_ingress_summaries`** (issue #17): Ingresses with their class, and each
  host/path rule's backend resolved against the namespace's Services.
  `backend_problem` says when the Service or port doesn't exist, the most common
  Ingress mistake; `backend_ready` gives the backend's ready endpoints. It also
  reports the default backend, TLS hosts with the Secret's name (never its
  contents), and load-balancer addresses. `print_ingress_summaries` is its
  companion.
  - **Composites:** `get_namespace_health` lists rules that can't reach a pod,
    and `get_workload_report` the rules routing to the workload.
  - **Captures** store Ingresses; older captures replay none. The `--mock`
    fixture's `shop` Ingress has a working rule, a rule to `ad` (0 ready) and
    a rule to a missing Service.
- **`get_container_metrics` and `get_node_metrics`** (issue #16): current CPU
  and memory use from metrics-server.
  - **Per container,** because limits apply per container, beside its requests
    and limits, in whole millicores and bytes for calculating, with percentages
    and `kubectl top`-style display strings. Nodes are measured against their
    allocatable.
  - **Each reading says what it can't show.** A note marks a reading taken
    after an OOM kill, or after an abnormal end within 10 minutes: the sample is
    from the fresh instance, and a short-window sample usually misses the spike
    that ends in a kill. Another marks memory at 90% or more of its limit:
    that's the working set, cache included, and isn't proof a kill is coming. A
    container with no reading is listed, with a note, not left out.
  - **Without metrics-server** the tools raise `K8sMetricsUnavailable`, since
    that's a normal setup.
  - **Captures** store one sample, or why metrics weren't available, which
    doesn't fail the capture. Replay keeps the values while `sampled` grows.
    Captures from before 3.0.0 raise "predates resource metrics".
  - **Composites:** each unhealthy workload gets its containers' usage, and a
    healthy workload near a memory limit says so; `get_workload_report`
    includes the readings.
  - **`--mock`:** `ad` reads 140Mi of 300Mi from the instance that started 58s
    after an OOM kill, which is the trap the notes are for.
    `test-deployment`'s CPU averages the 92% its HPA reports.
  - `print_container_metrics` and `print_node_metrics` are their companions.
- **`get_endpoint_summaries`** (issue #15): each Service's backends, from its
  EndpointSlices. Each address has its ready, serving and terminating state, pod
  and node, and the Service gets ready, not-ready and terminating counts. Unset
  conditions follow the API's defaults, and a pod in two slices (dual-stack) is
  counted once. `print_endpoint_summaries` is its companion.
  - **`ServiceSummary`** gains `ready_endpoints` and `not_ready_endpoints`, so
    "does this Service have backends?" is one call. They're None when unknown:
    ExternalName, EndpointSlices not readable, or older captures. They are not 0.
  - **Composites:** `get_namespace_health` lists Services with no ready
    endpoints, and names the Services in front of each unhealthy workload;
    `get_workload_report` includes them with each backend's state.
  - **Captures** store endpoints; older captures replay none. The `--mock`
    fixture's `ad` Service has its one not-ready backend.
- **`get_hpa_summaries`** (issue #14): HorizontalPodAutoscalers from
  `autoscaling/v2`, with:
  - the scale target as `"Kind/name"`, which joins with `owner` and
    `get_workload_history`;
  - min, max, current and desired replicas;
  - each metric's current value against its target, in the target's terms
    (`92%` of `80%`, `150Mi (average)`);
  - the `AbleToScale` / `ScalingActive` / `ScalingLimited` conditions, with the
    time since each changed;
  - the time since it last scaled;
  - a note when it's at `maxReplicas`.

  `print_hpa_summaries` is its companion. Captures store HPAs; older captures
  replay none. The composites show a workload's autoscaler (`get_namespace_health`
  flags a healthy workload "HPA at max"; `get_workload_report` includes the HPA),
  and the `--mock` fixture's `test-deployment` has one, at its maximum.
- **`get_namespace_health` and `get_workload_report`** (issue #13), composite
  tools for investigation.
  - **`get_namespace_health`:** a full entry per unhealthy workload, and one line
    per healthy one:
    - ready/desired and restarts;
    - the last termination, with the exit code's fixed meaning beside Kubernetes'
      recorded reason, noted when they disagree (137 without OOMKilled);
    - the instance lifetime and the restart gap (the back-off), from container
      status;
    - memory limit against request;
    - when the pod template last changed.

    Unhealthy workloads with the same exit code, reason and memory shape are
    grouped, with their instance lifetimes shown.
  - **`get_workload_report`:** one workload in one call:
    - container images, resources and probes;
    - each instance's state and last termination;
    - events, deduplicated across its pods;
    - current and previous log tails of its most troubled pod, with an optional
      grep;
    - the last template change, and the ConfigMaps and Secrets it uses.

  Both are built from the other tools, so they replay from any capture, and they
  state facts, not diagnoses. On captures from before 2.3.0, which have no pod
  owners, pods are matched to their workloads by their generated names, and the
  result says so. Output is bounded and empty fields are omitted.
  They replace `get_pod_summaries` and `get_workload_history` in the `triage`
  toolset, which is now `get_cluster_info`, `get_namespace_health`,
  `get_workload_report`, `get_events` and `get_node_summaries`.
- **Toolsets** (issue #20). `k8s-mcp-server --toolset triage|investigate|all`
  serves a named subset of the tools, adjustable with `--include TOOL` and
  `--exclude TOOL`; `k8s_tools.TOOLSETS` / `select_tools` do the same in Python.
  `investigate` leaves out the four tools another tool already covers
  (`get_pod_events`, `get_replicaset_summaries`, `get_logs_for_job`,
  `get_logs_for_cronjob`), which stay in `all` and in the library. `triage` is
  the composite tools plus cluster info, events and nodes. The default is still
  `all`.
- **`k8s-capture-state --redact-file FILE`** re-runs redaction over an existing
  capture and rewrites it in place, keeping its compression. Use it on captures
  taken with `--no-redact`, or before this release's rules: captures from 2.x can
  hold the credentials below in full.

### Changed
- **`investigate` is the default toolset** (issue #20, step 6 of #21), rather
  than every tool; `--toolset all` restores 2.x's behavior. k8srca's
  measurements on #20 and #13 supported the change.
- **A capture needs `list` on `horizontalpodautoscalers`** (autoscaling) **and
  `endpointslices`** (discovery.k8s.io). The README's role includes both.
  `get_service_summaries` works without the latter, with unknown counts.
- **A capture needs `list` on `ingresses`** (networking.k8s.io).
- **A capture needs `list` on `metrics.k8s.io` pods and nodes** where
  metrics-server runs. A cluster without metrics-server captures fine; a
  permission error fails the capture.
- **Tool descriptions are a few lines each** (issue #20): what the tool answers,
  its parameters, and at most one warning. Together they went from 47,946
  characters to about 5,000, which is roughly 11,000 fewer tokens on every turn
  of an agent that has all the tools. The field-by-field reference moved to
  [docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md).
- **Warnings come back with the results they apply to** (issue #20), rather than
  in descriptions:
  - Nodes, pods, container statuses, events and DaemonSets have a `notes` list,
    empty unless one applies. It covers the Ready time not being uptime, event
    counts that lag and BackOff counts that aren't restarts, `last_restart`,
    `ran_for` vs the restart cadence, a waiting container's logs, and an empty
    node selector.
  - Logs can begin with `[k8stools] note:` lines, which are not container output:
    for a waiting container whose `previous=False` and `previous=True` logs are
    the same, and when the text is the kubelet's error rather than a log. Callers
    that parse logs should skip lines starting with `k8s_tools.LOG_NOTE_PREFIX`.
  - `get_workload_history`'s `limits` also says how a ConfigMap write reaches a
    running pod.

  Captures don't store notes; replay derives them, including for older captures.
- **Redaction catches credentials embedded in longer strings** (issue #12).
  Previously these went through unless the value had a known token shape or sat
  under a sensitive name. Only the credential is replaced:
  - URL userinfo: `postgres://user:[REDACTED]@host`;
  - sensitive flags and system properties: `--password=`, `--token x`,
    `-Dapi.key=`, and `["--token", "x"]` in an argv list;
  - connection strings: `Password=…;`, libpq `password=…`;
  - query strings: `access_token=`, and presigned URLs' `X-Amz-Signature` / `sig`;
  - properties files: `db.password=…`.

  This also applies to the old and new values of changed args in
  `get_workload_history`. On a real cluster it found the same database password
  in three formats that 2.x returned in full.
- **Nothing that only locates a secret is redacted, under any rule.** That covers:
  - variable references (`$(DB_PASSWORD)`, `${token}`, `$API_KEY`) and templates;
  - file paths (`--tls-key /etc/certs/tls.key`);
  - booleans and masks;
  - flags named for a location (`--password-file`, `--secret-name`).

  Under the name rule this un-redacts, for example, a `skip-secret: "true"` label.
- Captures redact pod labels, as they do tool output.

### Fixed
- **Affinity `topology_key` values were redacted.** `get_pod_spec` returns
  snake_case, which the exact-`key` exemption missed, so `"kubernetes.io/hostname"`
  came back `[REDACTED]`. The same was true of label-key lists, and of
  `get_configmap`'s `binary_data_keys`, which hid binary entry names. These
  schema fields are now exempt in both spellings. On the same cluster, 3 of the
  6 redactions 2.x made were these false positives; now all 6 redactions are
  real credentials.

### Documentation
- **The README says plainly where redaction applies.** The functions in `TOOLS`
  return raw values; only `k8s-mcp-server` and `k8s-capture-state` redact. The
  agent example now wraps the tools with `wrap_with_redaction` (it passed them
  unwrapped, and a downstream MCP server built the same way served unredacted
  output), a warning follows it, and a new "Where redaction applies" table covers
  each way of using the tools.

## 2.4.0 — 2026-10-05

### Added
- **`get_workload_history` tool** (issue #11): what changed in a Deployment,
  StatefulSet or DaemonSet, and when. It lists the retained pod-template revisions
  newest first (a Deployment's ReplicaSets; a StatefulSet's or DaemonSet's
  ControllerRevisions), each compared with the one before it:
  - per container, matched by name: image, resource requests and limits,
    command, args, probes, env vars, `envFrom` and volume mounts;
  - for the pod: volumes, node selector and tolerations;
  - any other changed field is named;
  - template label and annotation changes are listed separately, so a Helm
    version-label bump doesn't bury a memory-limit change;
  - a `kubectl rollout restart` is flagged.

  Env vars are compared by name and shown by name only, never by value. The
  result also lists the ConfigMaps and Secrets the current template uses, and
  how. ConfigMaps get their age and `last_written`. Secrets are named but never
  read. Each result carries the limits of what history can show. `print_workload_history`
  is its companion.
- Captures store each workload's history (the tool's result, so no env values are
  written); captures from before 2.4.0 replay an images-only history rebuilt from
  their ReplicaSet records, marked `complete: false`. The `--mock` fixture has a
  history for each of its four workloads.

### Changed
- **A capture needs `list` on `controllerrevisions`** (apps), for StatefulSet and
  DaemonSet histories, and fails without it. The README's role includes it.

### Documentation
- `get_replicaset_summaries` no longer says the newest replica set's age is always
  when the deployment last changed: a rollback re-activates an older replica set
  under a new revision number.

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
- **`get_daemonset_summaries` tool** (issue #10), like `kubectl get daemonsets -o wide`:
  desired, current, ready, up-to-date, available and misscheduled counts, the pod
  template's node selector and images, update strategy, and age. Until now a
  DaemonSet was visible only through its pods, whose `<name>-<5 chars>` names
  can't be told apart from other names. Captures store DaemonSets; captures from
  before 2.3.0 replay none. `print_daemonset_summaries` is its companion.
- **`owner` on pods.** `get_pod_summaries` reports each pod's controlling owner
  as `"Kind/name"` (e.g. `"DaemonSet/otel-collector-agent"`), so pods can be
  grouped into workloads without guessing from name suffixes. It is the direct
  owner: a Deployment's pods name a replica set, whose `owner_deployment` names
  the Deployment. `None` for a pod nobody manages; captures from before 2.3.0
  replay `None`. `print_pod_summaries` gains an OWNER column.
- **The built-in `--mock` fixture shows the 2.3.0 fields** and now describes a
  cluster that could exist. It gains an `otel-collector-agent` DaemonSet and its
  pod, pod owners, node condition times, and event counts. Every workload now has
  its pods (`test-deployment`'s three, and `postgres-0`, which a PVC already
  named as mounting it), services select the pods they front, and the
  `postgres` StatefulSet's headless service exists. It also fixes
  contradictions in the old data that an agent would reason from:
  - the `ad` pod ran its previous revision's image;
  - its last instance's status said it ran 2 seconds while its log covered 2
    minutes;
  - its 93 restarts were more than its age allows at the 5-minute backoff cap
    (now 67);
  - several logs were dated 14 months before the capture;
  - the namespaces were older than anything else in the cluster;
  - `test-pod-123`'s spec declared one container while its status had two.

  The `ad` container now shows the case `previous=True` is for: it restarted 58
  seconds ago, so its current log is JVM startup and the OOM is only in the
  previous one. A new test file keeps the fixture consistent.

### Changed
- **A capture needs `list` on `daemonsets`** (apps). `k8s-capture-state` now
  captures DaemonSets, so it fails where the cluster role does not grant them,
  rather than writing a capture that replays as if there were none. Add
  `daemonsets` to the role's apps resources (see README, "Permissions").

### Documentation
- README lists the read-only RBAC rules that cover every tool.

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
