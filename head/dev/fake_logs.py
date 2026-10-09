"""Log records for the fake Insights and the filter-tree matcher of the logs search API.

The head's own records (service porthole) are served when the fake runs in process next to the head; tentacle
records from log storms are served only when droplet logs are switched on, because on 2026-10-08 the
Observability agent shipped metrics only (finding A6b)."""
from __future__ import annotations

import zlib
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

SEVERITIES = {"DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17}
MESSAGES = {"DEBUG": "cache lookup k{n}", "INFO": "order k{n} accepted", "WARN": "slow query took {n} ms",
            "ERROR": "payment provider timeout after {n} ms"}
MAX_PER_RUN = 20_000


def _ts(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _head_records(store: Any, region: str, t0: float, t1: float) -> Iterator[dict]:
    head = store.fleet.head
    if head is None or head.region != region:
        return
    resource = {"do.component": "app", "do.app.id": head.app_id}
    now = store.now().timestamp()
    if store.head_logs is not None:
        for rec in store.head_logs():
            t = _parse(rec["timestamp"])
            if t0 <= t <= min(t1, now - 5):  # a few seconds of ingestion delay
                attrs = {k: str(v) for k, v in rec.items()
                         if k not in ("timestamp", "severity_text", "severity_number", "body", "service.name",
                                      "trace_id", "span_id")}
                yield {"timestamp": rec["timestamp"], "severity_number": rec["severity_number"],
                       "severity_text": rec["severity_text"], "body": rec["body"], "trace_id": rec.get("trace_id", ""),
                       "span_id": rec.get("span_id", ""), "service_name": head.service_name, "resource": resource,
                       "attributes": attrs, "_urn": head.urn}
        return
    start = max(t0, t1 - 6 * 3600)
    t = start - start % 20
    while t <= t1:
        if t >= t0:
            n = zlib.crc32(str(int(t)).encode()) % 90 + 5
            yield {"timestamp": _ts(t), "severity_number": 9, "severity_text": "INFO",
                   "body": f"GET /api/fleet 200 in {n} ms", "service_name": head.service_name,
                   "resource": resource, "attributes": {"http.route": "/api/fleet"}, "_urn": head.urn}
        t += 20


def _tentacle_records(store: Any, region: str, t0: float, t1: float) -> Iterator[dict]:
    if not store.droplet_logs or store.world is None:
        return
    now = store.now().timestamp()
    for t in store.fleet.tentacles:
        if t.region != region:
            continue
        resource = {"do.component": "droplet", "do.droplet.id": str(t.id or "")}
        for run in store.world.runs(t.name):
            if run["name"] != "logs":
                continue
            p, start = run["params"], run["started_at"].timestamp()
            end = min(run["ended_at"].timestamp() if run.get("ended_at") else now, now)
            rate = max(1, int(p.get("rate", 1)))
            first = max(0, int((t0 - start) * rate))
            last = min(int((min(end, t1) - start) * rate), first + MAX_PER_RUN)
            for i in range(first, max(first, last)):
                ts = start + i / rate
                h = zlib.crc32(f"{run['id']}:{i}".encode()) % 10_000 / 100
                sev = "ERROR" if h < p.get("error_pct", 0) else ("WARN" if h % 7 < 1.05 else
                                                                 "DEBUG" if h % 7 < 2.1 else "INFO")
                yield {"timestamp": _ts(ts), "severity_number": SEVERITIES[sev], "severity_text": sev,
                       "body": MESSAGES[sev].format(n=int(h * 37) % 4000), "service_name": t.service_name,
                       "trace_id": f"{zlib.crc32(run['id'].encode()):08x}" * 4, "span_id": f"{i:016x}",
                       "resource": resource, "_urn": t.urn,
                       "attributes": {"scenario.name": "logs", "scenario.id": run["id"], "log.seq": str(i)}}


def generate(store: Any, region: str, t0: float, t1: float) -> list[dict]:
    return list(_head_records(store, region, t0, t1)) + list(_tentacle_records(store, region, t0, t1))


def _field_value(rec: dict, field: dict) -> Any:
    name, scope = field.get("name", ""), field.get("scope")
    if name.startswith("ResourceAttributes['"):
        return rec.get("resource", {}).get(name[20:-2])
    if name.startswith("LogAttributes['"):
        return rec.get("attributes", {}).get(name[15:-2])
    if name == "service.name":
        return rec.get("service_name")
    if name == "resource.urn":
        return rec.get("_urn")
    if name == "resource.type":
        return rec.get("resource", {}).get("do.component")
    if scope == "FIELD_SCOPE_RESOURCE":
        return rec.get("resource", {}).get(name)
    if scope == "FIELD_SCOPE_ATTRIBUTES":
        return rec.get("attributes", {}).get(name)
    return rec.get(name)


def _value(v: dict) -> Any:
    for key in ("string_value", "number_value", "bool_value"):
        if key in v:
            return v[key]
    for key in ("string_array_value", "number_array_value"):
        if key in v:
            return list(v[key].get("values") or [])
    return None


def matches(rec: dict, node: Any) -> bool:
    if not node:
        return True
    if "and" in node:
        return all(matches(rec, e) for e in node["and"].get("expressions") or [])
    if "or" in node:
        return any(matches(rec, e) for e in node["or"].get("expressions") or [])
    if "not" in node:
        return not matches(rec, node["not"])
    if "text_search" in node:
        q = str(node["text_search"].get("query") or "").lower()
        return any(q in str(x).lower() for x in (rec.get("body"), rec.get("service_name"), rec.get("_urn")))
    c = node.get("condition") or {}
    have = _field_value(rec, c.get("field") or {})
    op, want = c.get("operator"), _value(c.get("value") or {})
    if op == "FILTER_OPERATOR_EXISTS":
        return have not in (None, "")
    if op == "FILTER_OPERATOR_EQ":
        return have == want
    if op == "FILTER_OPERATOR_NEQ":
        return have != want
    if op == "FILTER_OPERATOR_IN":
        return have in (want or [])
    if have is None:
        return False
    try:
        if op == "FILTER_OPERATOR_GTE":
            return float(have) >= float(want)
        if op == "FILTER_OPERATOR_LTE":
            return float(have) <= float(want)
    except (TypeError, ValueError):
        return False
    return False
