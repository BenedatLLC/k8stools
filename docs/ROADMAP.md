# k8stools roadmap

This tracks planned and deferred work. It has three parts:

1. **Planned: 3.0.0**, sequenced in GitHub issue
   [#21](https://github.com/BenedatLLC/k8stools/issues/21).
2. **Deferred feature requests**: asked for, not yet planned.
3. **Completed internal work** (mock state capture).

What each release shipped is in [CHANGELOG.md](../CHANGELOG.md).

The guiding principles from the feature-request intake still hold: **read-only
tools only**, strongly typed with Pydantic return models, first-class `get_*` tools
over generic passthroughs. 3.0.0 adds two: **short tool descriptions**, with
caveats attached to the results they apply to, and **toolsets** so an agent sees
only the tools it needs.

---

## Planned: 3.0.0

All of this ships together as 3.0.0. The issue,
[#21](https://github.com/BenedatLLC/k8stools/issues/21), holds the build order, the
reasons for it, the k8srca measurement checkpoints, and what makes the release a
major version. In build order:

| Step | Issue | Work |
|---|---|---|
| 1 | [#12](https://github.com/BenedatLLC/k8stools/issues/12) | Redaction: credentials embedded in args, flags and URLs |
| 2 | [#20](https://github.com/BenedatLLC/k8stools/issues/20) | Short tool descriptions (caveats move into results); named toolsets |
| 3 | [#13](https://github.com/BenedatLLC/k8stools/issues/13) | Composite tools: `get_namespace_health`, `get_workload_report` |
| 4 | [#14](https://github.com/BenedatLLC/k8stools/issues/14), [#15](https://github.com/BenedatLLC/k8stools/issues/15), [#16](https://github.com/BenedatLLC/k8stools/issues/16) | HPA; Service endpoints (EndpointSlice); container and node metrics |
| 5 | [#17](https://github.com/BenedatLLC/k8stools/issues/17), [#18](https://github.com/BenedatLLC/k8stools/issues/18) | Ingress; custom resource conditions |
| 6 | [#20](https://github.com/BenedatLLC/k8stools/issues/20) | Default toolset becomes `investigate`, if the measurements support it |

Steps 4–5 came from a downstream coverage request, tracked in
[#19](https://github.com/BenedatLLC/k8stools/issues/19), which also records what was
declined (ReplicationController) and why.

---

## Deferred feature requests

Not part of 3.0.0. Kept from the original requester's priority list.

**Node allocated-resources breakdown.** The 1.1.0 node enhancement also asked for
summed requests/limits ("allocated resources") per node, marked "if feasible".
Everything else on the node summary shipped. This piece needs every pod listed per
node and Kubernetes quantity strings (`100m`, `128Mi`, …) summed correctly. #16's
metrics work needs the same quantity parsing, so this gets cheaper once that lands.
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
