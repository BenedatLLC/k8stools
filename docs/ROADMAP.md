# k8stools roadmap

This tracks planned and deferred work. It has three parts:

1. What shipped in **1.1.0** (for context).
2. **Deferred feature requests**, kept in the original requester's priority order.
3. **Completed internal work** (mock state capture).

The guiding principles from the feature-request intake still hold: **read-only
tools only**, strongly typed with Pydantic return models and full docstrings,
first-class `get_*` tools over generic passthroughs.

---

## Shipped in 1.1.0

Delivered the Priority-1 batch from a heavy RCA user's feature-request intake
(tracked internally, not in this repo) plus the cross-cutting redaction ask, and
upgraded core dependencies.

**New tools**
- `get_configmap_summaries` / `get_configmap` — ConfigMap listing and full-content read.
- `get_cronjob_summaries` / `get_job_summaries` — CronJob & Job spec+status, including
  the pod template's container images/env; `owner` links a Job back to its CronJob.
- `get_logs_for_job` / `get_logs_for_cronjob` — convenience readers that locate the
  most-recent pod of a Job / most-recent run of a CronJob (no manual pod hunting).
- `get_pvc_summaries` — PVC listing with `mounted_by` pod resolution (surfaces orphaned PVCs).
- `get_events` — cluster/namespace-wide events with server-side filtering
  (`reason`, `involved_kind`, `involved_name`, `event_type`); reuses `EventSummary`.
- `get_statefulset_summaries` — StatefulSet summaries mirroring `DeploymentSummary`.

**Enhancements to existing tools**
- `get_logs_for_pod_and_container` — added `tail`, `since_seconds`, and `previous`
  (previous-instance logs for crashloop RCA).
- `get_node_summaries` — added `capacity`, `allocatable`, `conditions`, `taints`, `labels`.
- `get_service_summaries` — added `selector`, `labels`, `annotations`.

**Secret redaction** (`redaction.py`) — a single redaction pass at the MCP server's
output boundary, **on by default**, opt-out via `--no-redact` or `K8STOOLS_REDACT=0`.
Matches by value shape (AWS keys, JWTs, PEM private keys) and by key/env-var name
(`key|secret|token|password|credential`), replacing matches with a visible
`[REDACTED]` marker and logging a count. Direct (non-MCP) callers get raw values and
can call `redaction.redact_object` themselves.

**Dependencies** — `mcp` `1.12` → `2.1.1` (FastMCP → MCPServer migration in
`mcp_server.py`), `kubernetes` `33.1` → `36.0.3`.

---

## Deferred feature requests (in priority order)

### From Priority-1 — partially deferred

**7b. `get_node_summaries`: allocated-resources breakdown.** The requester's node
enhancement (#7) also asked for a summed requests/limits ("allocated resources")
breakdown per node, marked "if feasible". Shipped everything else on the node
summary; this piece was deferred because it requires listing all pods per node and
correctly summing Kubernetes quantity strings (`100m`, `128Mi`, …). Worth doing with
a small, well-tested quantity parser.
- *Effort:* medium. *Value:* high for pods-per-node capacity modeling.

### Priority 2 — nice to have

**9. Live metrics (`kubectl top` equivalent).** `get_pod_metrics` / `get_node_metrics`
from `metrics.k8s.io` (via `CustomObjectsApi` or the metrics client). Depends on
metrics-server being installed; tools should degrade gracefully when it isn't.
- *Effort:* medium. *Value:* medium (idle-fleet / live-usage RCAs).

**10. `get_pod_summaries`: label / field selectors.** Optional `label_selector` /
`field_selector` params on the existing tool (e.g. `status.phase=Pending`, or all
pods of one workload) to make "find the newest pod for workload X" a one-call op and
cut client-side grepping. Note `get_logs_for_job` / `get_logs_for_cronjob` already
cover the most common "newest pod for a workload" case for batch workloads.
- *Effort:* low. *Value:* medium.

**11. Core Ingress read.** `get_ingress_summaries` — host/path routing, backend
service, TLS secret *names* (not contents). Core `networking.k8s.io` Ingress only;
vendor CRDs remain the caller's problem.
- *Effort:* low–medium. *Value:* low.

### Out of scope (requester handles these locally — recorded for the line we drew)

- HorizontalPodAutoscaler / autoscaler status.
- Vendor-specific CRDs (e.g. an ingress controller's route CRD).
- Kubelet `/stats/summary` passthrough for live per-pod ephemeral-storage bytes.
- Any reader for Kubernetes `Secret` objects — **deliberately never.** (The redaction
  work is only about secret-shaped values leaking through *non-Secret* resources.)

---

## Completed internal work: mock state capture & replay

**Implemented 2026-09-13**, to the design in
[`designs/mock-state-capture.md`](../designs/mock-state-capture.md). The hardcoded
single-scenario `mock_tools.py` fixture is replaced by a capture-and-replay system:
`k8s-capture-state` CLI → JSON snapshot → `MockState` loader → MCP `--state-file`,
covering all 19 tools including the 1.1.0 (ConfigMaps, CronJobs/Jobs, PVCs,
StatefulSets, cluster-wide events) and 1.2.0 (ReplicaSets) batches.

The old hardcoded data was migrated to `src/k8stools/fixtures/otel-demo.json`, which
is now what `--mock` serves. See the design doc's status section for the deviations
from the written design and for the behavior changes in `mock_tools`.
