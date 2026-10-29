# Changelog

All notable changes to k8stools. Versions follow [semantic versioning](https://semver.org/);
dates are release-tag dates.

## 4.4.0 — unreleased

### Issues fixed
- [#30](https://github.com/BenedatLLC/k8stools/issues/30): Import path: an
  ITBench-Lite snapshot to a capture, at a chosen pull.
- [#31](https://github.com/BenedatLLC/k8stools/issues/31): A NetworkPolicy tool
  that states what each policy allows.

### Added
- **`get_networkpolicy_summaries(namespace)`**: each NetworkPolicy's pod
  selector and the pods it selects, its rules, and what it allows per
  direction in plain words ("allows all ingress", "denies all egress",
  "allows ingress only from pods with app=web in this namespace on TCP
  8080"), by the API's rules rather than the policy's name: ITBench-Lite
  Scenario-31's `frontend-block-all-ports` allows everything. Default
  `policyTypes`, `[{}]` against `[{podSelector: {}}]`, and one peer with two
  selectors (both) against two peers (either) are each stated distinctly;
  notes say when a selector matches no pod and when no egress rule allows
  DNS. In the `investigate` toolset. Captured and replayed; a capture without
  them says they're not recorded. **Needs `list` on
  `networking.k8s.io/networkpolicies`** (added to the README's ClusterRole);
  without it the composites say so and carry on.
- `get_workload_report` gains `network_policies`: the policies selecting the
  workload's pods, combined per direction ("ingress: allows all ingress
  (NetworkPolicy/frontend-block-all-ports)"), or "no NetworkPolicy
  (networking.k8s.io) selects these pods", with policy kinds installed as
  custom resources (Cilium, Calico, AdminNetworkPolicy) named in a note.
  `get_namespace_health` gains `network_policies`, one line per policy.
- **`k8s-capture-state --from-itbench DIR [--at PULL]`** imports one pull of an
  ITBench-Lite scenario directory (default: the last) as a capture, and
  `--list-pulls` lists them with the kinds each has. The pull is served to the
  real tools and capture code (`k8stools.itbench`), so it replays under the
  same rules as a live capture, and what the snapshot doesn't hold is not
  recorded: kinds a pull has no batch of, HPAs, CronJobs, StatefulSet and
  DaemonSet revisions, previous-instance logs, and the logs of containers with
  no records. Metrics and custom resource instances say ITBench didn't collect
  them. Logs are the services' OpenTelemetry records, each line with its
  time, severity, body (templates filled in) and attributes, and every
  replayed log says in a note that they aren't the container's output. Events
  are those that existed at the pull; Endpoints are served as EndpointSlices.
  All 11 scenarios at hand import (7-18 s each); Scenario-33's matches k8srca's
  earlier conversion on everything that had, and adds owners, the scheduler's
  reasons, Deployment conditions, endpoints and the template change that
  caused it.
- `K8sNotRecorded`, a `K8sApiError` raised only on replay, when the capture
  doesn't record what was asked for. Like any `K8sApiError`, an MCP client
  receives its message.
- `k8s_tools.pinned_clock(at)`: every tool computes ages as of `at` within the
  block. Every age the tools compute now reads one clock (`_now()`).
- `k8s_tools.use_api_client(api_client, context=, server=)`: bind every tool to
  an `ApiClient` of your own within the block, then restore the binding. All
  API groups, the metrics and custom-resource readers and `get_cluster_info`
  (which reports `source: "supplied"`) go through it, so a converter can serve
  a recorded snapshot to the real tools and capture by giving the client a
  `rest_client` that answers from it.

### Changed
- **A kind a capture has no list of is now "not recorded" on replay, not
  none.** Its tool raises `K8sNotRecorded`, saying the kind is unknown rather
  than absent; it used to return `[]`, telling the agent none existed. This
  covers namespaces, nodes, pods, Deployments, ReplicaSets, Services,
  endpoints, Ingresses, ConfigMaps, StatefulSets, DaemonSets, HPAs, CronJobs,
  Jobs, PVCs and events (`get_configmap` too), and custom resource definitions
  in a capture made before 3.0.0. `get_namespace_health` and
  `get_workload_report` don't fail: they name those kinds in one note, "Not
  recorded in this capture, so unknown rather than absent: ...", and use
  nothing from them. A report asked for a workload of a kind that wasn't
  recorded raises `K8sNotRecorded`.
- **A container log a capture didn't record is now "not recorded", not
  empty.** `get_logs_for_pod_and_container` raised nothing and returned `''`
  for a container missing from the pod's logs (unreadable when captured, or
  `--no-logs`); it now raises `K8sNotRecorded`, and in `get_workload_report`
  that tail has `available: false` with the reason. A log recorded as an empty
  string still replays as empty. With `previous=True`, a container that
  restarted but whose previous log wasn't captured is "not recorded"; one that
  never restarted is "not found", as live. A container the pod doesn't have
  is an error, as live, rather than an empty log.
- In k8srca's captures this changes (the rest replay as before):
  - `itbench-33-invalid-node-selector` and `jvm-oom-on-startup`: DaemonSets,
    HPAs, endpoints and Ingresses read "not recorded", in the composites' note
    and from their own tools. Neither has custom resources, now "not
    recorded" rather than none. Scenario-33 also has no workload histories
    (rebuilt from its ReplicaSets with a note, as before), and neither has
    metrics ("predates resource metrics", as before).
  - `image-pull-typo`: `email-754859f94c-4x46m`'s `email` log is not recorded
    (the capture has no entry for it), so it is "not recorded", not empty.
  - In seven of them (all but `checkout-gc-pressure`, `itbench-33` and
    `jvm-oom-on-startup`), completed init containers that restarted (e.g.
    `accounting-...-9pczq/wait-for-kafka`, 16 restarts) have no captured
    previous log, by design: `previous=True` on them reads "not recorded",
    not "not found".
  - Scenario-33's 8 containers stored as `''` still replay as empty; only
    re-importing changes them.
- **A container that never started has no start time.** One that failed with
  `StartError` (the runtime couldn't run it) reports its start as Go's zero
  time, 1970-01-01, which `ContainerStateTerminated` took literally:
  `ran_for` was 55 years, and `get_workload_report` gave that as the instance's
  lifetime. `started_at` and `ran_for` are now None for it. (Found in
  ITBench-Lite Scenario-105, a CrashLoopBackOff on an invalid command.)
- **A capture's ages are as of its `captured_at`.** `k8s-capture-state` pins
  the clock for the whole capture, so an object read a minute in is no longer
  aged a minute more than the file's timestamp says (replay shifted it by that
  much). `captured_at` is now whole seconds.

## 4.3.0 — 2026-10-24

### Issues fixed
- [#29](https://github.com/BenedatLLC/k8stools/issues/29): Composites: say how
  many nodes an unscheduled pod's nodeSelector and required affinity admit.

### Added
- `WorkloadHealth.placement` and `WorkloadReport.placement`: for each pod with
  no node, how many nodes its nodeSelector and required node affinity admit,
  out of all nodes, and how many of those have a NoSchedule or NoExecute taint
  it doesn't tolerate, e.g. "ad-x: 0 of 5 nodes match its nodeSelector and
  required node affinity". The scheduler's FailedScheduling message names only
  the first filter that rejected each node, so a node it reports as tainted
  may not match the selector either; answering that took `get_pod_spec` and
  `get_node_summaries` together. The count is k8stools' own evaluation, and a
  note says so. The composites read the nodes once per call, only when some
  pod is unscheduled; when the nodes can't be read (a namespace-scoped role),
  the line says "not evaluated" and why. Replays compute it from the
  captured node labels and pod specs, so existing captures have it.

### Changed
- `get_namespace_health` and `get_workload_report` on the `--mock` fixture
  have a `placement` line for each of the two unscheduled pods (`checkout`,
  `quote`), and the namespace view a note. Tests that pin the mock's output
  will see them.

### Fixed
- `get_pod_spec`'s error for a failure other than 404 carried the client
  exception's whole text (headers and raw JSON) instead of its one sentence.

## 4.2.0 — 2026-10-24

### Issues fixed
- [#28](https://github.com/BenedatLLC/k8stools/issues/28): get_pvc_summaries:
  an unbound claim's events are not in its summary.

### Added
- `PVCSummary.events`: for a claim that is not Bound, its events, newest
  first, at most five (issue #28). Why a claim waits (`ProvisioningFailed:
  storageclass ... not found`, `WaitForFirstConsumer`) was in `get_events`,
  and the pods that mount it in `get_pvc_summaries`, so no one result tied the
  reason to the pod. The tool makes one extra events call when any claim is
  unbound, and none when all are bound. Replays fill it from the capture's
  events, so older captures replay it too; captures don't store it.
- The `--mock` fixture has an unbound claim, `quote-cache`, whose storage
  class doesn't exist, and a new `quote` Deployment whose only pod waits on
  it. The scheduler's, PV controller's and Deployment controller's messages
  for it were recorded from a real cluster with the same objects.
- `WorkloadHealth.claims` and `WorkloadReport.claims`: the unbound claims an
  unscheduled pod mounts, each with its storage class and newest event, as
  'PersistentVolumeClaim/cache: Pending, storage class fast-ssd;
  ProvisioningFailed: storageclass.storage.k8s.io "fast-ssd" not found'. The
  scheduler's reason, "pod has unbound immediate PersistentVolumeClaims", said
  the pod waited on a claim but not why; the claim's events do. The composites
  read claims only when some pod is unscheduled.

### Changed
- `get_namespace_health` on the `--mock` fixture lists three unhealthy
  workloads, not two: `Deployment/quote` (0/1) is new. Tests that pin the
  mock's output will see it.

### Fixed
- The live test of replica set ordering failed on a cluster where a
  Deployment had been rolled back: a rollback reuses an older replica set
  under the newest revision number. It now accepts that when
  `get_workload_history` marks the revision `reused`.

### Tests
- `tests/test_live_writes.py` creates the failures #27 and #28 describe (a
  rollout whose new pod can't be scheduled, a claim on a missing storage
  class) in a namespace of its own on a real cluster, and checks the tools
  against the scheduler's and controllers' own messages. It writes to the
  cluster, so it runs only with `K8STOOLS_LIVE_WRITE=1`. A third test has a
  Deployment's pod mount a claim on a missing storage class, and checks that
  the composites name the claim and the PV controller's event.

## 4.1.0 — 2026-10-21

### Issues fixed
- [#27](https://github.com/BenedatLLC/k8stools/issues/27): get_namespace_health
  counts a workload with a stuck rollout as healthy.

### Changed
- **A rollout that can't progress is not healthy** (issue #27). A Deployment
  whose new pod couldn't be scheduled (an invalid nodeSelector on the ITBench
  otel-demo) was listed healthy: its old pod was still ready, and a pod with no
  node has no containers to be waiting. `get_namespace_health` now counts a
  workload with a pod that has no node as unhealthy, and so is a Deployment
  whose `Progressing` condition is `False` (e.g. `ProgressDeadlineExceeded`)
  or whose `ReplicaFailure` is `True` (e.g. a ResourceQuota rejected its pods,
  so there's no pod to find). `ready` still counts the pods serving.
  Workloads not scheduled for the same reason are grouped in `common_failures`.

### Added
- `PodSummary.unscheduled`: why a pod has no node, from its `PodScheduled`
  condition.
- `DeploymentSummary.conditions`: `Available`, `Progressing` and
  `ReplicaFailure`, with reason, message and how long ago each changed.
- `WorkloadHealth` and `WorkloadReport` gain `unscheduled` (one line per pod:
  how long, and the scheduler's reason) and `conditions` (a Deployment's
  failing conditions, in its controller's words). `get_workload_report`
  previously showed an unscheduled pod nowhere, since it has no instances.
- The `--mock` fixture has a `checkout` Deployment whose new pod can't be
  scheduled, past its progress deadline, and every Deployment has conditions.

Captures made before 4.1.0 have neither field: an unscheduled pod is still
found (no node and no container statuses), with its reason "not recorded", and
`DeploymentSummary.conditions` is None.

## 4.0.2 — 2026-10-17

### Issues fixed
- [#26](https://github.com/BenedatLLC/k8stools/issues/26): restarts_per_day is
  read as a restart history.

### Changed
- **`WorkloadHealth.restarts_per_day` is removed** (issue #26). Restarts over
  pod age, added in 4.0.0, was read as a steady rate and so as a timeline:
  "3200 restarts at 19.5/day: crash-looping continuously for 163 days since the
  rollout". A restart count has no times in it (a container looping
  continuously restarts ~288 times a day at the 5-minute back-off cap, so that
  figure itself showed it wasn't). `restarts` and `pod_age` remain. The
  restart-history note now says the restarts' times aren't recorded and that a
  count over the pod's age is not a rate.

## 4.0.1 — 2026-10-17

### Issues fixed
- [#25](https://github.com/BenedatLLC/k8stools/issues/25): MCP server hides every
  K8sApiError's message: clients see only 'Error executing tool <name>'.

### Fixed
- **MCP clients see why a tool call failed** (issue #25). The MCP library passes
  an exception's text to the client only for its `ToolError`, and reports
  anything else as a crash, so every anticipated k8stools failure reached
  agents as `Error executing tool <name>`: a missing pod, a forbidden list,
  metrics-server absent, a capture that predates a field, and 4.0's reclaimed
  log, whose explanation exists for agents. The server now reports
  `K8sApiError`, `K8sConfigError` and failures to reach the API server with
  their message, redacted like a result. Unexpected exceptions stay generic.
  This was true of every earlier release; Python callers are unaffected.
- **Kubernetes API errors are one line.** Messages built from the client's
  exception carried its whole text, ~500-700 characters of response headers
  and raw JSON around one sentence. They now read e.g. `Error fetching pod
  'x' in namespace 'default': 404 Not Found: pods "x" not found`.
- **An unreachable API server is named**: `can't reach the Kubernetes API
  server at https://...: Failed to establish a new connection: [Errno 111]
  Connection refused`, instead of a connection object's repr. The log readers
  reported it as "An unexpected error occurred"; it's now "Error fetching logs".

### Changed
- `urllib3` is a declared dependency (it was already installed by `kubernetes`),
  since k8stools now imports it directly.

## 4.0.0 — 2026-10-17

### Issues fixed
- [#22](https://github.com/BenedatLLC/k8stools/issues/22): Redaction misses a SQL
  password literal (CREATE USER ... WITH PASSWORD '...').
- [#23](https://github.com/BenedatLLC/k8stools/issues/23): Redaction false
  positive: query parameter contextKeys=… redacted (plural of 'key').
- [#24](https://github.com/BenedatLLC/k8stools/issues/24): Triage composites:
  state the facts agents keep misreading (logs gone, timing, recovering
  workloads, init containers).

### Upgrading from 3.x
- **A log whose file is gone raises `K8sLogReclaimed`** (a `K8sApiError`) from
  `get_logs_for_pod_and_container`, `get_logs_for_job` and `get_logs_for_cronjob`,
  where 3.x returned the kubelet's `unable to retrieve container logs for ...`
  text with a note above it. Code that already catches `K8sApiError` needs no
  change.
- **`get_pod_container_statuses` returns a `PodContainerStatuses`**, not a list:
  `containers` (app containers, then native sidecars) and `init_containers`. Use
  `.containers` where you iterated the list. It raises `K8sApiError` for a pod
  that doesn't exist (live, it leaked the client's `ApiException`; replay
  returned `[]`).

### Changed
- **Reclaimed logs are absent, not text** (issue #24). Agents read the kubelet's
  `unable to retrieve container logs for ...` message as the program's output,
  note or not, and offered "no exception in the previous log" as evidence. The
  log tools now raise `K8sLogReclaimed`, saying which instance's log is gone and
  that this is not a tool failure. In `get_workload_report`, that log tail has
  `available: false`, a `reason`, and no lines. Captures still record the
  kubelet's text, and replay raises the same error.
- **"Ready" is no longer "healthy"** (issue #24). `get_namespace_health`
  listed a workload that restarts and recovers as healthy, e.g. "Deployment/
  accounting 1/1, 578 restarts", without its last termination, and "every other
  workload is healthy" became the largest source of unsupported claims. A ready
  workload with a restart in the last 24 hours now goes in a new `recovering`
  group, with the same fields as an unhealthy one (last termination, memory,
  usage), most recent first, at most 10 in full. `WorkloadHealth` gains
  `restarts_per_day` (restarts over the summed age of its pods). The `--mock`
  fixture has such a workload, `accounting`, whose previous log is reclaimed.
- **Init containers are reported** (issue #24). Answers said "no init
  containers" when there was one, because no tool showed them.
  - `get_pod_container_statuses` lists them in `init_containers`, apart from the
    app containers, so a completed init container's exit 0 can't be read as an
    app container's termination. `[]` means none; None (with a note) means a
    replayed capture didn't record them. Native sidecars (`restartPolicy:
    Always`) are listed with the app containers, with `sidecar: true`, in every
    tool.
  - `get_namespace_health` treats an init container that hasn't completed as
    unhealthy and names it, and ignores completed ones.
  - `get_workload_report` adds `init_containers`, always present (`[]` when
    none), and `init_instances` for those that haven't completed, whose logs it
    includes.
  - Captures record init container statuses and logs (previous logs only for
    init containers that haven't completed: a completed one's is rarely kept,
    and asking for it filled the capture summary with missing logs); older captures replay
    them as not recorded. The `--mock` fixture's `postgres-0` has a completed
    init container.
- **Restart history is said to be missing, not estimated** (issue #24). Answers
  read the event window as when a crash loop began. Kubernetes keeps only each
  container's restart count and last termination, so `get_namespace_health` and
  `get_workload_report` now say so when anything has restarted, and point to
  metrics or centralized logs for history. `get_workload_report`'s instances
  gain `pod_age`, the span a restart count covers. The issue's proposed derived
  timings (a restart interval from event counts, failures-observed-since,
  restarts vs. event span) were left out: event counts aren't restarts, and
  the API doesn't keep the history they'd need.
- **Nothing claims how long event records last** (issue #24). `get_events`,
  `get_pod_events` and `get_workload_report`'s event note said records expire
  about an hour after their last update. That is the API server's default
  `--event-ttl`, not something the API reports, and answers quoted it as this
  cluster's retention. They now say only where the records start, and that the
  retention period isn't reported.
- **Redaction catches SQL password literals** (issue #22): `CREATE/ALTER
  ROLE|USER ... PASSWORD '...'` and MySQL's `IDENTIFIED [WITH plugin] BY '...'`,
  replacing only the literal's contents. 3.0.0 caught the OTel demo's database
  password in three connection-string formats but returned its Postgres init
  script's `CREATE USER otelu WITH PASSWORD 'otelp'` in full. Only a quoted
  literal counts, so prose such as "password reset" is untouched. Measured on a
  live OTel demo capture: one new redaction, that password, and no other change.
  Re-redact kept captures with `k8s-capture-state --redact-file`.
- **Redaction no longer hides `...Keys=` settings** (issue #23). In setting
  names, the plural `keys` no longer marks a value sensitive: an Envoy access
  log's `?contextKeys=telescopes` came back `contextKeys=[REDACTED]`, 41 times in
  one capture, none a credential. Plural `tokens` and `credentials` still count,
  and so does `keys` in env-var and field names (`API_KEYS`).
- **`PodSummary` counts native sidecars**, as `kubectl get pods` does:
  `total_containers`, `ready_containers`, `restarts` and `last_restart` include
  init containers with `restartPolicy: Always`. Before, a crash-looping sidecar
  left the pod's `restarts` at 0 and `last_restart` empty, so
  `get_namespace_health` could show a workload with `restarts: 0` beside a last
  termination from that sidecar, and a pod nothing owns looked ready with an
  unready sidecar. Captures keep the counts as recorded.
- `get_service_summaries` and `get_ingress_summaries` treat an EndpointSlice
  client that can't be set up (`K8sConfigError`) like an unreadable EndpointSlice
  API: endpoint counts and backend readiness are None (unknown), rather than the
  call failing. With one cluster binding shared by every API group, a live
  server reaches this only when the core query would have failed first anyway.

### Fixed
- **`test_service_summaries` failed in CI.** Since #15, `get_service_summaries`
  lists EndpointSlices for its endpoint counts, and the test faked only the core
  API client: with no kubeconfig it raised `K8sConfigError`, while locally it
  passed by querying whatever cluster the kubeconfig named. The test now fakes
  the discovery API and checks the counts, and `tests/conftest.py` fails any
  unit test that tries to resolve a cluster binding (integration, binding and
  MCP client tests are exempt), so local runs behave like CI.

### Documentation
- **The README is reorganized for someone new to the project.** It starts with
  a quick start (install, try `--mock`, point at a cluster, add to Claude Code or
  an `mcp.json`) and explains toolsets (`--toolset`, `--include`, `--exclude`) in
  their own section. The tools are grouped by area, with the toolsets each is
  in, and the server's options are grouped by purpose. New sections cover
  choosing toolsets in Python, `k8s-mcp-client`, and development. The rules for
  cluster selection were under toolsets by mistake and moved back, and links to
  `docs/` are absolute so they work on PyPI.

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
