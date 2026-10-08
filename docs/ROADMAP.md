# k8stools roadmap

This tracks planned and deferred work. It has two parts:

1. **Deferred feature requests**: asked for, not yet planned.
2. **Completed internal work** (mock state capture).

3.0.0 delivered the plan in [#21](https://github.com/BenedatLLC/k8stools/issues/21);
see the changelog.

What each release shipped is in [CHANGELOG.md](../CHANGELOG.md).

The guiding principles from the feature-request intake still hold: **read-only
tools only**, strongly typed with Pydantic return models, first-class `get_*` tools
over generic passthroughs. 3.0.0 adds two: **short tool descriptions**, with
caveats attached to the results they apply to, and **toolsets** so an agent sees
only the tools it needs.

---

## Deferred feature requests

Kept from the original requester's priority list.

**Node allocated-resources breakdown.** The 1.1.0 node enhancement also asked for
summed requests/limits ("allocated resources") per node, marked "if feasible".
Everything else on the node summary shipped. This piece needs every pod listed per
node and Kubernetes quantity strings (`100m`, `128Mi`, …) summed correctly. 3.0.0's
metrics work added that parsing (`k8s_tools.parse_quantity`), so this is now cheaper.
- *Effort:* medium. *Value:* high for pods-per-node capacity modeling.

**`get_pod_summaries`: label / field selectors.** Optional `label_selector` /
`field_selector` parameters (e.g. `status.phase=Pending`, or all pods of one
workload), making "find the newest pod for workload X" one call. Partly covered
already: `PodSummary.owner` (2.3.0) groups pods by workload, and
`get_logs_for_job` / `get_logs_for_cronjob` handle the batch case.
- *Effort:* low. *Value:* medium.

### Out of scope

- Kubelet `/stats/summary` passthrough for live per-pod ephemeral-storage bytes.
- Raw custom-resource objects and Gateway API routes: possible later, on request.
  #18 covers custom resources' `status.conditions` only.
- Any reader for Kubernetes `Secret` objects — **deliberately never.** Tools that
  refer to a Secret (workload history, Ingress TLS) give its name only. The
  redaction work is about secret-shaped values leaking through *non-Secret*
  resources.

---

## Completed internal work: mock state capture & replay

**Implemented 2026-09-13**, to the design in
[`designs/mock-state-capture.md`](../designs/mock-state-capture.md). The hardcoded
single-scenario `mock_tools.py` fixture is replaced by a capture-and-replay system:
`k8s-capture-state` CLI → JSON snapshot → `MockState` loader → MCP `--state-file`,
covering every tool. New tools since then (cluster info, DaemonSets, workload
history) are captured too.

The old hardcoded data was migrated to `src/k8stools/fixtures/otel-demo.json`, which
is now what `--mock` serves. It is hand-maintained and kept internally consistent by
`tests/test_builtin_fixture.py` (see `CLAUDE.md`). See the design doc's status
section for the deviations from the written design and for the behavior changes in
`mock_tools`.
