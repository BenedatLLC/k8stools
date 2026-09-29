# k8stools roadmap

This tracks planned and deferred work. It has two parts:

1. **Deferred feature requests**, kept in the original requester's priority order.
2. **Completed internal work** (mock state capture).

What each release shipped is in [CHANGELOG.md](../CHANGELOG.md).

The guiding principles from the feature-request intake still hold: **read-only
tools only**, strongly typed with Pydantic return models and full docstrings,
first-class `get_*` tools over generic passthroughs.

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
