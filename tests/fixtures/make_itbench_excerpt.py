"""Trim an ITBench-Lite scenario to a test fixture (how itbench-33-excerpt was made).

    uv run python tests/fixtures/make_itbench_excerpt.py <Scenario-33 dir> tests/fixtures/itbench-33-excerpt

Keeps the last two pulls: every namespace and node, and in otel-demo the cart, ad
and checkout workloads (Deployments, ReplicaSets, pods, Services, Endpoints); the
events about them, in both API versions; and per pod the last 10 log records
before each pull. managedFields are dropped (only ConfigMaps' are read, and none
are kept). The dataset is ITBench-Lite (Apache 2.0).
"""

import collections
import csv
import json
import sys
from pathlib import Path

from k8stools import itbench

APPS = ("cart", "ad", "checkout")
LOG_RECORDS_PER_PULL = 10


def _ours(name: str) -> bool:
    return name.split("-")[0] in APPS


def _keep_object(kind: str, obj: dict) -> bool:
    meta = obj["metadata"]
    if kind in ("namespaces", "nodes"):
        return True
    if meta.get("namespace") != "otel-demo":
        return False
    if kind in ("deployments", "services", "endpoints"):
        return meta["name"] in APPS
    return kind in ("replicasets", "pods") and _ours(meta["name"])


def _copy(src: Path, dst: Path, name: str, keep, limit=None) -> None:
    with open(src / name, newline="") as f, open(dst / name, "w", newline="") as g:
        reader = csv.reader(f, delimiter="\t")
        writer = csv.writer(g, delimiter="\t", lineterminator="\n")
        header = next(reader)
        writer.writerow(header)
        col = {h: i for i, h in enumerate(header)}
        rows = [row for row in reader if keep(row, col)]
        for row in (limit(rows, col) if limit else rows):
            row = list(row)
            if name != itbench.LOGS_FILE:
                body = json.loads(row[col["Body"]])
                (body.get("object") or body)["metadata"].pop("managedFields", None)
                row[col["Body"]] = json.dumps(body, separators=(",", ":"))
            writer.writerow(row)


def main(src: Path, dst: Path) -> None:
    csv.field_size_limit(sys.maxsize)
    dst.mkdir(parents=True, exist_ok=True)
    pulls = itbench.read_pulls(src)[-2:]
    first, last = pulls[0].started, pulls[-1].ended

    def ts(row, col):
        return itbench._parse_ts(row[col["Timestamp"]])

    def keep_object(row, col):
        kind = itbench._attrs(row[col["LogAttributes"]]).get("k8s.resource.name")
        return first <= ts(row, col) <= last and _keep_object(kind, json.loads(row[col["Body"]]))

    def keep_event(row, col):
        event = json.loads(row[col["Body"]])["object"]
        involved = event.get("involvedObject") or event.get("regarding") or {}  # core, events.k8s.io
        return involved.get("namespace") == "otel-demo" and _ours(involved.get("name") or "")

    def keep_log(row, col):
        resource = itbench._attrs(row[col["ResourceAttributes"]])
        return (resource.get("k8s.namespace.name") == "otel-demo"
                and _ours(resource.get("k8s.pod.name", "")) and ts(row, col) <= last)

    def last_records(rows, col):
        by_pod = collections.defaultdict(list)
        for row in rows:
            by_pod[itbench._attrs(row[col["ResourceAttributes"]])["k8s.pod.name"]].append(row)
        kept = set()
        for records in by_pod.values():
            records.sort(key=lambda r: r[col["Timestamp"]])
            for pull in pulls:
                kept.update(tuple(r) for r in [r for r in records if ts(r, col) <= pull.ended]
                            [-LOG_RECORDS_PER_PULL:])
        return sorted(kept, key=lambda r: r[col["Timestamp"]])

    _copy(src, dst, itbench.OBJECTS_FILE, keep_object)
    _copy(src, dst, itbench.EVENTS_FILE, keep_event)
    _copy(src, dst, itbench.LOGS_FILE, keep_log, last_records)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
