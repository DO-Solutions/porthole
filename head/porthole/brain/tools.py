"""The Brain's eight tools (design section 11.2), each defined once with its schema and approval flag.

The same definitions serve the deckhand, the tool list on the Brain page and, in phase 2, the head's MCP server.
Read tools go through the panels and the traced harness client, so every call shows up in the API drawer."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from porthole.apitrace import caller, collect_calls
from porthole.security import ApiError

BRAIN_RANGES = ("5m", "15m", "30m", "1h", "3h", "6h")


class ToolResult(TypedDict):
    ok: bool
    summary: str
    data: Any
    trace_ids: list[str]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[[dict], Awaitable[ToolResult]]
    needs_approval: bool

    def view(self) -> dict:
        return {"name": self.name, "description": self.description, "parameters": self.parameters,
                "needs_approval": self.needs_approval}


def _schema(**props: dict) -> dict:
    return {"type": "object", "properties": props, "required": [k for k, v in props.items() if v.pop("req", False)]}


def build_tools(deps: Any) -> dict[str, Tool]:
    fleet = deps.settings.fleet

    async def run(name: str, body: Callable[[], Awaitable[tuple[str, Any]]]) -> ToolResult:
        with collect_calls() as calls, caller(f"brain.{name}"):
            try:
                summary, data = await body()
                return {"ok": True, "summary": summary, "data": data, "trace_ids": list(calls)}
            except ApiError as e:
                return {"ok": False, "summary": e.message, "data": e.detail, "trace_ids": list(calls)}

    async def fleet_describe(args: dict) -> ToolResult:
        async def body() -> tuple[str, Any]:
            snap = await deps.poller.latest()
            listings = await deps.poller.listings_fresh()
            out = []
            for t in snap["tentacles"]:
                recent = (listings.get(t["name"]) or {}).get("finished") or []
                out.append({**t, "recent": recent[:5]})
            names = ", ".join(f"{t['display']} ({t['region']})" for t in snap["tentacles"])
            return f"{len(out)} tentacles: {names}", {"tentacles": out, "head": snap["head"], "sea": snap["sea"]}
        return await run("fleet_describe", body)

    async def tentacle_status(args: dict) -> ToolResult:
        async def body() -> tuple[str, Any]:
            t = fleet.tentacle(str(args.get("name") or ""))
            if t is None:
                raise ApiError(400, "unknown_target", f"no tentacle called {args.get('name')!r}")
            health = await deps.poller.clients[t.name].health(record=True)
            running = [f"{r['name']} {r['id']}" for r in health.get("running") or []]
            return (f"{t.display}: load {health.get('load1')}, memory {health.get('mem_pct')} %, running "
                    f"{', '.join(running) or 'nothing'}"), health
        return await run("tentacle_status", body)

    async def insights_query_range(args: dict) -> ToolResult:
        async def body() -> tuple[str, Any]:
            rng = str(args.get("range") or "30m")
            if rng not in BRAIN_RANGES:
                raise ApiError(400, "bad_range", f"range must be one of {list(BRAIN_RANGES)} (at most 6 h)")
            p = await deps.panels.range(args.get("region"), str(args.get("metric") or ""), args.get("filters") or [],
                                        args.get("agg") or "avg", rng, None)
            parts = []
            for s in p["series"]:
                values = [v for _, v in s["points"]]
                if values:
                    parts.append(f"{s['display']} last {values[-1]:.1f}, max {max(values):.1f}")
            return "; ".join(parts) or "no series", p
        return await run("insights_query_range", body)

    async def insights_alert_instances(args: dict) -> ToolResult:
        async def body() -> tuple[str, Any]:
            overview = await deps.alert_panels.overview()
            status = str(args.get("status") or "").lower()
            items = [i for i in overview["instances"]
                     if (not status or i["status"] == status) and (not args.get("rule_id") or
                                                                   i["rule_id"] == args["rule_id"])]
            active = [i for i in items if i["status"] == "active"]
            return f"{len(active)} active, {len(items) - len(active)} resolved", items
        return await run("insights_alert_instances", body)

    async def insights_logs_search(args: dict) -> ToolResult:
        async def body() -> tuple[str, Any]:
            page = await deps.log_panels.page(args.get("region"), str(args.get("range") or "1h"),
                                              args.get("service") or None, args.get("severity") or None, None, 100)
            more = "+" if page["pagination"].get("has_more") else ""
            return f"Insights returned {page['count']}{more} records", page
        return await run("insights_logs_search", body)

    async def scenario_start(args: dict) -> ToolResult:
        async def body() -> tuple[str, Any]:
            view = await deps.scenarios.start(str(args.get("target") or ""), str(args.get("scenario") or ""),
                                              args.get("params") or {}, actor="brain")
            return f"started {view['id']} on {view['target']}", view
        return await run("scenario_start", body)

    async def scenario_stop(args: dict) -> ToolResult:
        async def body() -> tuple[str, Any]:
            view = await deps.scenarios.stop(str(args.get("target") or ""), str(args.get("run_id") or ""),
                                             actor="brain")
            return f"{view['id']} is {view['status']}", view
        return await run("scenario_stop", body)

    async def voyage_start(args: dict) -> ToolResult:
        async def body() -> tuple[str, Any]:
            voyage = await deps.voyages.start(str(args.get("name") or ""), args.get("params") or {}, actor="brain")
            return f"voyage {voyage.voyage} set sail as {voyage.id}", voyage.brief()
        return await run("voyage_start", body)

    s = {"type": "string"}
    tools = [
        Tool("fleet_describe", "the fleet summary: tentacles, their health and recent runs, the sea", _schema(),
             fleet_describe, False),
        Tool("tentacle_status", "/health and running scenarios of one tentacle", _schema(name={**s, "req": True}),
             tentacle_status, False),
        Tool("insights_query_range", "a builder-mode range query, at most 6 h; pick fleet members with filters "
             "like resource_urn=do:droplet:1 (a resource_name filter on a fleet member is turned into its URN)",
             _schema(region=dict(s), metric={**s, "req": True}, filters={"type": "array", "items": s},
                     agg=dict(s), range=dict(s)), insights_query_range, False),
        Tool("insights_alert_instances", "alert instances of the fleet's rules", _schema(status=dict(s),
                                                                                         rule_id=dict(s)),
             insights_alert_instances, False),
        Tool("insights_logs_search", "at most 100 log records for a fleet service",
             _schema(region=dict(s), service=dict(s), severity=dict(s), range=dict(s)), insights_logs_search, False),
        Tool("scenario_start", "start a scenario with the console's validation and caps",
             _schema(target={**s, "req": True}, scenario={**s, "req": True}, params={"type": "object"}),
             scenario_start, True),
        Tool("scenario_stop", "stop a running scenario",
             _schema(target={**s, "req": True}, run_id={**s, "req": True}), scenario_stop, True),
        Tool("voyage_start", "set a voyage sailing", _schema(name={**s, "req": True}, params={"type": "object"}),
             voyage_start, True),
    ]
    return {t.name: t for t in tools}
