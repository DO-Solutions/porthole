"""Metric panels built from the harness: range charts, instant values, the catalog, label values, fleet dots.

Every upstream call goes through the panel cache and the upstream budget; when the budget runs out a panel serves
its last value marked stale. In 'both' mode each region is asked separately and its series are tagged with the
region, never summed. Alerts and logs panels live in panels_alerts.py and panels_logs.py."""
from __future__ import annotations

import asyncio
import json
import math
import time
from pathlib import Path
from typing import Any

import httpx

from insights_harness import InsightsError, excerpt
from porthole import promql
from porthole.apitrace import caller, collect_calls
from porthole.cache import BudgetExhausted
from porthole.clock import iso
from porthole.config import Entity, Fleet
from porthole.security import ApiError

CATALOG_TTL_S = 300
PROBE_TTL_S = 60
LABEL_NAMES = set(promql.FLEET_LABELS) | set(promql.ENUM_LABELS) | {
    "__name__", "resource_region_slug", "filesystem_device", "filesystem_type", "host_id"}


def entity_for(labels: dict, fleet: Fleet) -> Entity | None:
    """The fleet member of a series, by resource_urn (fresh Droplets carry no resource_name, B-023). A name only
    identifies members that have no URN in the fleet description."""
    if e := fleet.by_urn(labels.get("resource_urn")):
        return e
    e = fleet.entity(labels["resource_name"]) if labels.get("resource_name") else None
    return e if e and not e.urn else None


def normalize(body: Any, region: str, fleet: Fleet) -> list[dict]:
    """Prometheus vector or matrix -> series tagged with entity, display name, region and color slot."""
    out = []
    for s in ((body or {}).get("data") or {}).get("result") or []:
        labels = {k: v for k, v in (s.get("metric") or {}).items() if k != "__name__"}
        e = entity_for(labels, fleet)
        raw = s.get("values") or ([s["value"]] if s.get("value") else [])
        points = []
        for t, v in raw:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(f):
                points.append([int(float(t)), f])
        text = ", ".join(f"{k}={v}" for k, v in sorted(labels.items())) or "value"
        out.append({"labels": labels, "entity": e.name if e else labels.get("resource_name"),
                    "display": e.display if e else (labels.get("resource_name") or text), "region": region,
                    "slot": e.slot if e and e.slot else None, "points": points})
    return out


def group_families(names: list[str]) -> dict[str, list[dict]]:
    families: dict[str, list[dict]] = {}
    for name in sorted(set(names)):
        family, dotted = promql.dotted(name)
        families.setdefault(family, []).append({"dotted": dotted, "underscored": name})
    return dict(sorted(families.items()))


def error_info(e: BaseException) -> dict:
    if isinstance(e, InsightsError):
        return {"status": e.status, "message": excerpt(e.body, 300)}
    if isinstance(e, BudgetExhausted):
        return {"status": 503, "message": f"upstream budget used up; retry in {math.ceil(e.retry_in)} s",
                "retry_in": math.ceil(e.retry_in)}
    return {"status": None, "message": f"{type(e).__name__}: {e}"}


def load_probe_metrics(watcher_dir: Path) -> dict[str, str]:
    try:
        data = json.loads((watcher_dir / "probe_metrics.json").read_text())
        return {kind: v["metric"] for kind, v in data["families"].items()}
    except (OSError, ValueError, KeyError):
        return {"tentacle": "do.droplets.cpu_utilization"}


class Panels:
    def __init__(self, deps: Any):
        self.deps = deps
        self.fleet: Fleet = deps.settings.fleet
        self.probe_mode = "selector"
        self.probe_metrics = load_probe_metrics(deps.watcher_dir)

    @property
    def ttl(self) -> float:
        return self.deps.settings.cache_ttl_s

    def regions(self, region: str | None, allow_both: bool = True) -> list[str]:
        region = region or (self.fleet.regions[0] if self.fleet.regions else "")
        if region == "both" and allow_both and self.fleet.regions:
            return list(self.fleet.regions)
        if region not in self.fleet.regions:
            allowed = list(self.fleet.regions) + (["both"] if allow_both else [])
            raise ApiError(400, "bad_region", f"region must be one of {allowed}", {"region": region})
        return [region]

    def insights(self) -> Any:
        if self.deps.insights is None:
            raise ApiError(503, "insights_not_configured",
                           "Insights not configured: DIGITALOCEAN_TOKEN is not set on this server")
        return self.deps.insights

    async def call(self, name: str, method: str, *args: Any, **kwargs: Any) -> tuple[Any, list[str], float]:
        """One harness call in a worker thread; returns (body, call ids, ms). Errors carry their call ids."""
        ins = self.insights()
        t0 = time.monotonic()
        with collect_calls() as calls, caller(name):
            try:
                body = await self.deps.run_sync(getattr(ins, method), *args, **kwargs)
            except Exception as e:
                e.porthole_calls = list(calls)  # type: ignore[attr-defined]
                raise
        return body, list(calls), round((time.monotonic() - t0) * 1000, 1)

    def window(self, range_s: int, step_s: int) -> tuple[int, int]:
        now = int(self.deps.clock.now().timestamp())
        end = now - now % step_s
        return end - range_s, end

    async def _range_one(self, region: str, query: str, start: int, end: int, step: int, name: str) -> dict:
        async def fetch() -> dict:
            body, calls, ms = await self.call(name, "query_range", query, start, end, f"{step}s", region=region)
            return {"series": normalize(body, region, self.fleet), "calls": calls, "ms": ms, "start": start, "end": end}

        try:
            res = await self.deps.cache.get(f"range:{region}:{step}:{end - start}:{query}", fetch, self.ttl)
        except (InsightsError, BudgetExhausted, httpx.HTTPError) as e:
            return {"error": error_info(e), "calls": getattr(e, "porthole_calls", []), "series": []}
        return {**res.value, "stale": res.stale, "retry_in": res.retry_in, "cached": res.cached,
                "fetched_at": res.fetched_at, "error": None}

    async def range_payload(self, regions: list[str], queries: dict[str, str], range_s: int, step: int, unit: str,
                            name: str = "panels.range") -> dict:
        start, end = self.window(range_s, step)
        results = await asyncio.gather(*(self._range_one(r, queries[r], start, end, step, name) for r in regions))
        out: dict[str, Any] = {"promql": queries[regions[0]], "start": start, "end": end, "step": step, "unit": unit,
                               "series": [], "regions": {}, "stale": False, "cached": True, "fetched_at": None}
        for r, res in zip(regions, results, strict=True):
            out["regions"][r] = {"calls": res.get("calls", []), "ms": res.get("ms"), "error": res["error"],
                                 "promql": queries[r]}
            if res["error"]:
                out["cached"] = False
                continue
            out["series"] += res["series"]
            out["start"], out["end"] = res["start"], res["end"]
            out["stale"] = out["stale"] or res["stale"]
            out["cached"] = out["cached"] and res["cached"]
            out["fetched_at"] = max(filter(None, [out["fetched_at"], res["fetched_at"]]), default=None)
            if res.get("retry_in"):
                out["retry_in"] = max(out.get("retry_in", 0), math.ceil(res["retry_in"]))
        return out

    async def range(self, region: str | None, metric: str, filters: Any, agg: str | None, range_name: str | None,
                    step: str | None) -> dict:
        regions = self.regions(region)
        try:
            range_s = promql.range_seconds(range_name)
            step_s = promql.step_seconds(step, range_s)
            parsed = promql.parse_filters(filters)
            queries = {r: promql.build(metric, agg, parsed, r, self.fleet) for r in regions}
        except promql.BuilderError as e:
            raise ApiError(400, "bad_query", str(e)) from None
        unit = promql.unit_for(f"rate({metric})" if agg == "rate" else metric)
        payload = await self.range_payload(regions, queries, range_s, step_s, unit)
        return {**payload, "metric": metric, "agg": agg, "filters": parsed}

    async def promql_raw(self, region: str | None, query: str, range_name: str | None, step: str | None) -> dict:
        regions = self.regions(region)
        try:
            q = promql.check_raw(query)
            range_s = promql.range_seconds(range_name, builder=False)
            step_s = promql.step_seconds(step, range_s)
        except promql.BuilderError as e:
            raise ApiError(400, "bad_query", str(e)) from None
        return await self.range_payload(regions, {r: q for r in regions}, range_s, step_s, promql.unit_for(q),
                                        "panels.promql")

    async def instant(self, region: str | None, metric: str, filters: Any, agg: str | None = "avg") -> dict:
        regions = self.regions(region)
        try:
            parsed = promql.parse_filters(filters)
            queries = {r: promql.build(metric, agg, parsed, r, self.fleet) for r in regions}
        except promql.BuilderError as e:
            raise ApiError(400, "bad_query", str(e)) from None
        out: dict[str, Any] = {"promql": queries[regions[0]], "unit": promql.unit_for(metric), "series": [],
                               "regions": {}, "stale": False}
        for r in regions:
            async def fetch(r: str = r) -> dict:
                body, calls, ms = await self.call("panels.query", "query", queries[r], region=r)
                return {"series": normalize(body, r, self.fleet), "calls": calls, "ms": ms}
            try:
                res = await self.deps.cache.get(f"query:{r}:{queries[r]}", fetch, self.ttl)
            except (InsightsError, BudgetExhausted, httpx.HTTPError) as e:
                out["regions"][r] = {"calls": getattr(e, "porthole_calls", []), "error": error_info(e)}
                continue
            out["series"] += res.value["series"]
            out["regions"][r] = {"calls": res.value["calls"], "ms": res.value["ms"], "error": None,
                                 "promql": queries[r]}
            out["stale"] = out["stale"] or res.stale
        return out

    async def catalog(self, region: str | None) -> dict:
        regions = self.regions(region)
        per: dict[str, dict] = {}
        for r in regions:
            async def fetch(r: str = r) -> dict:
                body, calls, _ = await self.call("panels.catalog", "label_values", "__name__", region=r)
                return {"names": list(body.get("data") or []), "calls": calls}
            try:
                res = await self.deps.cache.get(f"catalog:{r}", fetch, CATALOG_TTL_S)
            except (InsightsError, BudgetExhausted, httpx.HTTPError) as e:
                per[r] = {"families": {}, "count": 0, "error": error_info(e), "calls": getattr(e, "porthole_calls", [])}
                continue
            names = res.value["names"]
            per[r] = {"families": group_families(names), "count": len(names), "counted_at": res.fetched_at,
                      "stale": res.stale, "retry_in": res.retry_in, "cached": res.cached,
                      "calls": res.value["calls"], "error": None}
        if region == "both":
            return {"regions": per, "window": "30m"}
        return {**per[regions[0]], "region": regions[0], "window": "30m"}

    async def labels(self, region: str | None, name: str, match: str | None) -> dict:
        r = self.regions(region, allow_both=False)[0]
        if name not in LABEL_NAMES:
            raise ApiError(400, "bad_label", f"label {name!r} is not available; use one of {sorted(LABEL_NAMES)}")
        if name == "__name__" and not match:
            selectors = None
        else:
            if not match:
                raise ApiError(400, "match_required",
                               "match is required: the dotted metric whose label values you want")
            try:
                selectors = [promql.selector(promql.check_metric(match), {}, r, self.fleet)]
            except promql.BuilderError as e:
                raise ApiError(400, "bad_query", str(e)) from None

        async def fetch() -> dict:
            body, calls, _ = await self.call("panels.labels", "label_values", name, match=selectors, region=r)
            return {"values": list(body.get("data") or []), "calls": calls}

        res = await self.deps.cache.get(f"labels:{r}:{name}:{selectors}", fetch, CATALOG_TTL_S)
        return {"region": r, "name": name, "match": selectors, "values": res.value["values"],
                "calls": res.value["calls"], "fetched_at": res.fetched_at, "stale": res.stale}

    async def probes(self) -> dict:
        """Whether Insights has a sample for each fleet member in the last 5 minutes (instant queries, 60 s cache)."""
        entities = self.fleet.entities()
        if self.deps.insights is None:
            return {"mode": None, "checked_at": None, "errors": [],
                    "entities": {f"{e.kind}:{e.name}": {"seen": None, "reason": "Insights not configured"}
                                 for e in entities}}
        try:
            res = await self.deps.cache.get("probes", self._probe_fetch, PROBE_TTL_S)
        except BudgetExhausted as e:
            return {"mode": self.probe_mode, "checked_at": None, "errors": [error_info(e)], "entities": {}}
        return {**res.value, "stale": res.stale}

    async def _probe_fetch(self) -> dict:
        seen_urns: set[str] = set()
        seen_names: set[str] = set()
        errors: list[dict] = []
        # the regions are probed side by side (measured: 309 ms sequentially against 160 ms at 150 ms a call)
        await asyncio.gather(*(self._probe_region(r, seen_urns, seen_names, errors) for r in self.fleet.regions))
        out = {}
        for e in self.fleet.entities():
            key = f"{e.kind}:{e.name}"
            if not e.region:
                out[key] = {"seen": None, "reason": "no region in the fleet description"}
            elif self.probe_mode == "selector" and not e.urn:
                out[key] = {"seen": None, "reason": "no URN in the fleet description"}
            else:
                out[key] = {"seen": bool(e.urn and e.urn in seen_urns) or
                            (self.probe_mode == "family" and e.name in seen_names)}
        return {"mode": self.probe_mode, "checked_at": iso(self.deps.clock.now()), "entities": out, "errors": errors}

    async def _probe_region(self, r: str, seen_urns: set[str], seen_names: set[str], errors: list[dict]) -> None:
        members = [e for e in self.fleet.entities() if e.region == r]
        urns = [e.urn for e in members if e.urn]
        if self.probe_mode == "selector" and urns:
            q = f"count by (resource_urn) ({{resource_urn=~{promql.regex_alternation(urns)}}})"
            try:
                body, _, _ = await self.call("panels.probe", "query", q, region=r)
                seen_urns |= {s["metric"].get("resource_urn") for s in body["data"]["result"]}
            except InsightsError as e:
                if 400 <= e.status < 500 and e.status != 429:
                    self.probe_mode = "family"
                    self.deps.log.warn("Insights rejected the metric-less probe selector; using per-family "
                                       "probe metrics from watcher/probe_metrics.json", status=e.status,
                                       response=excerpt(e.body, 200))
                else:
                    errors.append({"region": r, **error_info(e)})
            except httpx.HTTPError as e:
                errors.append({"region": r, **error_info(e)})
        if self.probe_mode == "family":
            for kind in sorted({e.kind for e in members} & set(self.probe_metrics)):
                q = f"count by (resource_urn, resource_name) ({self.probe_metrics[kind]})"
                try:
                    body, _, _ = await self.call("panels.probe", "query", q, region=r)
                except (InsightsError, httpx.HTTPError) as e:
                    errors.append({"region": r, "kind": kind, **error_info(e)})
                    continue
                for s in body["data"]["result"]:
                    seen_urns.add(s["metric"].get("resource_urn", ""))
                    seen_names.add(s["metric"].get("resource_name", ""))

    def sidecar(self) -> dict | None:
        try:
            return json.loads((self.deps.watcher_dir / "dashboards" / "krakens-eye.queries.json").read_text())
        except (OSError, ValueError):
            return None

    async def dashboard_run(self, index: int, region: str | None, range_name: str | None) -> dict:
        """Run one chart of the committed dashboard sidecar; the queries come from the repo, not from visitors."""
        side = self.sidecar()
        if side is None:
            raise ApiError(404, "no_sidecar", "watcher/dashboards/krakens-eye.queries.json is not in this build")
        charts = side.get("charts") or []
        if not 0 <= index < len(charts) or not charts[index].get("promql"):
            raise ApiError(400, "no_query", "that chart has no PromQL to run")
        regions = self.regions(region)
        try:
            range_s = promql.range_seconds(range_name or "1h")
        except promql.BuilderError as e:
            raise ApiError(400, "bad_range", str(e)) from None
        template = charts[index]["promql"]
        queries = {}
        urns = {f"${kind}_urn": (s.urn or "none") for kind, s in self.fleet.sea.items()}
        urns["$head_urn"] = self.fleet.head.urn if self.fleet.head else "none"
        for r in regions:
            tentacles = [t.urn for t in self.fleet.tentacles if t.region == r and t.urn] or ["none"]
            q = template.replace("$tentacle", promql.regex_escape(tentacles))
            for var, urn in urns.items():
                q = q.replace(var, urn)
            queries[r] = q
        payload = await self.range_payload(regions, queries, range_s, promql.step_seconds(None, range_s),
                                           promql.unit_for(template), "panels.dashboard")
        return {**payload, "chart": {k: charts[index].get(k) for k in ("title", "type", "legend", "group")}}

    def caps(self) -> dict:
        return {"ranges": list(promql.RANGES), "aggs": list(promql.AGGS),
                "promql": {"max_chars": promql.MAX_QUERY_CHARS, "max_range_s": promql.MAX_RANGE_S,
                           "min_step_s": promql.MIN_STEP_S},
                "filter_labels": list(promql.FLEET_LABELS) + sorted(promql.ENUM_LABELS),
                "enum_values": {k: sorted(v) for k, v in promql.ENUM_LABELS.items()}}
