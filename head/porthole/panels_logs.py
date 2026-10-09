"""Logs panels: a page of Insights records, the captain's free-form search, expected versus observed, chain runs.

"Expected from tentacles" pairs every log storm run with the count Insights returned for that service in the same
window, and says plainly when that count is zero (finding A6b). Logs are one region at a time (finding A11)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from insights_harness import Insights, InsightsError, and_, cond, not_, or_, order, text
from porthole import promql
from porthole.cache import BudgetExhausted
from porthole.clock import hhmm, iso, parse_iso
from porthole.panels import Panels, error_info
from porthole.security import ApiError

SEVERITY_MIN = {"DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17}
COUNT_PAGES = 3
COUNT_LIMIT = 1000
COUNT_TTL_S = 60
MAX_SEARCH_S = 7 * 86400


def a6b_text(display: str, emitted: int, start: datetime | None, end: datetime | None, service: str,
             checked: datetime) -> str:
    """The banner of design section 2.7."""
    return (f"Not yet collected by DigitalOcean: {display} reports {emitted:,} lines between {hhmm(start)} and "
            f"{hhmm(end)}, Insights returned 0 for service {service} (finding A6b, checked {hhmm(checked)})")


def rebuild_filter(node: Any, depth: int = 0) -> dict:
    """Re-create a free-form filter tree with the harness builders, which validate every node."""
    if depth > 8:
        raise ValueError("the filter tree is nested too deep")
    if not isinstance(node, dict) or len(node) != 1:
        raise ValueError("each filter node must set exactly one of condition, and, or, not, text_search")
    kind, value = next(iter(node.items()))
    if kind in ("and", "or"):
        exprs = (value or {}).get("expressions") or []
        if not exprs or len(exprs) > 20:
            raise ValueError(f"{kind} needs 1 to 20 expressions")
        return (and_ if kind == "and" else or_)(*[rebuild_filter(e, depth + 1) for e in exprs])
    if kind == "not":
        return not_(rebuild_filter(value, depth + 1))
    if kind == "text_search":
        q = str((value or {}).get("query") or "")
        if not 1 <= len(q) <= 200:
            raise ValueError("text_search needs a query of 1 to 200 characters")
        return text(q)
    if kind == "condition":
        f = value.get("field") or {}
        v = value.get("value")
        raw = None if v is None else next(iter(v.values()), None) if isinstance(v, dict) else v
        if isinstance(raw, dict):
            raw = raw.get("values")
        return cond(f.get("name") if isinstance(f, dict) else f, value.get("operator", ""), raw,
                    f.get("scope") if isinstance(f, dict) else None)
    raise ValueError(f"unknown filter node {kind!r}")


class LogPanels:
    def __init__(self, panels: Panels):
        self.p = panels
        self.deps = panels.deps

    def _region(self, region: str | None) -> str:
        if region == "both":
            raise ApiError(400, "one_region", "logs show one region at a time during the preview (finding A11)")
        return self.p.regions(region, allow_both=False)[0]

    def now(self) -> datetime:
        return self.deps.clock.now().astimezone(timezone.utc)

    async def page(self, region: str | None, range_name: str | None, service: str | None, severity: str | None,
                   cursor: str | None, limit: int, start: int | None = None, end: int | None = None) -> dict:
        r = self._region(region)
        try:
            range_s = promql.range_seconds(range_name or "1h")
        except promql.BuilderError as e:
            raise ApiError(400, "bad_range", str(e)) from None
        if service and service not in self.p.fleet.service_names():
            raise ApiError(400, "bad_service", f"service must be one of {self.p.fleet.service_names()}")
        severity = (severity or "").upper()
        if severity and severity not in SEVERITY_MIN:
            raise ApiError(400, "bad_severity", f"severity must be one of {list(SEVERITY_MIN)}")
        if not 1 <= limit <= 100:
            raise ApiError(400, "bad_limit", "limit must be 1 to 100 (the captain's search allows 1,000)")
        if cursor and (start is not None or end is not None):
            # A cursor only means something inside the window of the page it came from, so the page sends that
            # window back; anything that is not a plausible window is a client error, not a server crash.
            latest = int(self.now().timestamp()) + 86400
            if start is None or end is None or not 0 <= start < end <= latest or end - start > promql.MAX_RANGE_S:
                raise ApiError(400, "bad_window", "start and end must be unix seconds of the page's own window, at "
                                                  "most 24 h apart and not in the future")
            t0, t1 = datetime.fromtimestamp(start, timezone.utc), datetime.fromtimestamp(end, timezone.utc)
        else:
            now = self.now().replace(microsecond=0)
            t1 = now - timedelta(seconds=now.second % 10)
            t0 = t1 - timedelta(seconds=range_s)
        conds = ([cond("service.name", "=", service)] if service else []) + (
            [cond("severity_number", ">=", SEVERITY_MIN[severity])] if severity else [])
        flt = conds[0] if len(conds) == 1 else and_(*conds) if conds else None
        sort = [order("timestamp", "desc")]
        body_sent = Insights.logs_body(t0, t1, flt, sort, limit, cursor)

        async def fetch() -> dict:
            body, calls, ms = await self.p.call("panels.logs", "search_logs", t0, t1, flt, sort, limit, cursor,
                                                region=r)
            return {"body": body, "calls": calls, "ms": ms}

        key = f"logs:{r}:{int(t0.timestamp())}:{int(t1.timestamp())}:{service}:{severity}:{cursor}:{limit}"
        res = await self.deps.cache.get(key, fetch, self.p.ttl)
        records = res.value["body"].get("data") or []
        return {"region": r, "records": records, "count": len(records),
                "pagination": res.value["body"].get("pagination") or {"has_more": False},
                "window": {"start": int(t0.timestamp()), "end": int(t1.timestamp())}, "promql_equivalent": None,
                "body_sent": body_sent, "calls": res.value["calls"], "fetched_at": res.fetched_at,
                "stale": res.stale, "summary": f"Insights returned {len(records):,} records"}

    async def search(self, region: str | None, range_name: str | None, filter_tree: Any, limit: int) -> dict:
        r = self._region(region)
        try:
            range_s = promql.seconds(range_name or "1h", "range")
            flt = rebuild_filter(filter_tree) if filter_tree else None
        except (promql.BuilderError, ValueError, TypeError) as e:
            raise ApiError(400, "bad_filter", str(e)) from None
        if not 60 <= range_s <= MAX_SEARCH_S:
            raise ApiError(400, "bad_range", "range must be between 1m and 7d")
        if not 1 <= limit <= 1000:
            raise ApiError(400, "bad_limit", "limit must be 1 to 1,000")
        t1 = self.now().replace(microsecond=0)
        t0 = t1 - timedelta(seconds=range_s)
        sort = [order("timestamp", "desc")]
        body, calls, _ = await self.p.call("panels.logs_search", "search_logs", t0, t1, flt, sort, limit, None,
                                           region=r)
        records = body.get("data") or []
        return {"region": r, "records": records, "count": len(records), "pagination": body.get("pagination") or {},
                "body_sent": Insights.logs_body(t0, t1, flt, sort, limit), "calls": calls,
                "summary": f"Insights returned {len(records):,} records"}

    async def count(self, region: str, service: str, t0: datetime, t1: datetime) -> dict:
        """Records Insights holds for one service in a window, following the cursor for up to 3,000."""
        flt = cond("service.name", "=", service)
        sort = [order("timestamp", "desc")]

        async def fetch() -> dict:
            total, cursor, calls, capped = 0, None, [], False
            for page in range(COUNT_PAGES):
                body, more, _ = await self.p.call("panels.logs_count", "search_logs", t0, t1, flt, sort, COUNT_LIMIT,
                                                  cursor, region=region)
                calls += more
                total += len(body.get("data") or [])
                pag = body.get("pagination") or {}
                cursor = pag.get("next_cursor")
                if not pag.get("has_more") or not cursor:
                    break
                capped = page == COUNT_PAGES - 1
            return {"count": total, "capped": capped, "calls": calls}

        key = f"logcount:{region}:{service}:{int(t0.timestamp())}:{int(t1.timestamp())}"
        return (await self.deps.cache.get(key, fetch, COUNT_TTL_S)).value

    async def expected(self, range_name: str | None) -> list[dict]:
        try:
            range_s = promql.range_seconds(range_name or "1h")
        except promql.BuilderError as e:
            raise ApiError(400, "bad_range", str(e)) from None
        now = self.now()
        since = now - timedelta(seconds=range_s)
        listings = await self.deps.poller.listings_fresh()
        rows = []
        for t in self.p.fleet.tentacles:
            listing = listings.get(t.name) or {}
            for run in (listing.get("running") or []) + (listing.get("finished") or []):
                if run.get("name") != "logs":
                    continue
                started, ended = parse_iso(run.get("started_at")), parse_iso(run.get("ended_at"))
                if started is None or (ended or now) < since:
                    continue
                rows.append(await self._expected_row(t, run, started, ended, now))
        rows.sort(key=lambda r: r["started_at"] or "", reverse=True)
        return rows

    async def _expected_row(self, t: Any, run: dict, started: datetime, ended: datetime | None,
                            now: datetime) -> dict:
        result, params = run.get("result") or {}, run.get("params") or {}
        estimated = "emitted" not in result
        emitted = int(result.get("emitted") if not estimated else
                      params.get("rate", 0) * max(0.0, ((ended or now) - started).total_seconds()))
        by_severity = {sev: result.get(f"count_{sev.lower()}") for sev in SEVERITY_MIN} if not estimated else None
        row = {"tentacle": t.name, "display": t.display, "region": t.region, "service": t.service_name,
               "run_id": run.get("id"), "status": run.get("status"), "started_at": iso(started),
               "ended_at": iso(ended), "emitted": emitted, "estimated": estimated, "by_severity": by_severity,
               "insights_count": None, "insights_count_capped": False, "checked_at": iso(now), "calls": []}
        window_end = min((ended or now) + timedelta(seconds=60), now)
        try:
            counted = await self.count(t.region, t.service_name, started, window_end)
        except (InsightsError, BudgetExhausted, httpx.HTTPError, ApiError) as e:
            info = error_info(e) if not isinstance(e, ApiError) else {"message": e.message}
            return {**row, "verdict": "unknown", "text": f"Could not ask Insights: {info['message']}"}
        n = counted["count"]
        row.update(insights_count=n, insights_count_capped=counted["capped"], calls=counted["calls"])
        if emitted == 0:
            return {**row, "verdict": "nothing emitted", "text": f"{t.display} reports no lines for this run yet"}
        if n == 0:
            return {**row, "verdict": "not collected (A6b)",
                    "text": a6b_text(t.display, emitted, started, ended or now, t.service_name, now)}
        more = "+" if counted["capped"] else ""
        return {**row, "verdict": "collected",
                "text": f"Collected: Insights returned {n:,}{more} of {emitted:,} lines for service "
                        f"{t.service_name} (checked {hhmm(now)})"}

    async def chains(self, range_name: str | None) -> dict:
        try:
            range_s = promql.range_seconds(range_name or "24h")
        except promql.BuilderError as e:
            raise ApiError(400, "bad_range", str(e)) from None
        since = self.now() - timedelta(seconds=range_s)
        listings = await self.deps.poller.listings_fresh()
        rows = []
        for t in self.p.fleet.tentacles:
            listing = listings.get(t.name) or {}
            for run in (listing.get("running") or []) + (listing.get("finished") or []):
                started = parse_iso(run.get("started_at"))
                if run.get("name") != "chain" or started is None or started < since:
                    continue
                res, params = run.get("result") or {}, run.get("params") or {}
                rows.append({"tentacle": t.name, "display": t.display, "region": t.region, "run_id": run.get("id"),
                             "status": run.get("status"), "started_at": run.get("started_at"),
                             "ended_at": run.get("ended_at"), "count": params.get("count"), "ok": res.get("ok"),
                             "failed": res.get("failed"), "first_trace_id": res.get("first_trace_id"),
                             "last_trace_id": res.get("last_trace_id"), "latency_ms": params.get("latency_ms"),
                             "error_pct": params.get("error_pct")})
        rows.sort(key=lambda r: r["started_at"] or "", reverse=True)
        return {"chains": rows, "traces_link": self.deps.links.link("insights.traces")}
