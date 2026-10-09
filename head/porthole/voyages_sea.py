"""The log, trace and managed-resource voyages of design Appendix C: log storm, chain and deep water.

The log storm compares what a tentacle wrote with what Insights returned and gives the A6b verdict when Insights
has nothing; deep water checks which managed families report within five minutes."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from porthole.clock import hhmm, iso, parse_iso
from porthole.panels_logs import a6b_text
from porthole.voyages import VoyageFailed
from porthole.voyages_metrics import finished, tentacle, value


async def log_storm(ctx: Any) -> dict:
    t = tentacle(ctx, ctx.params.get("target"))
    async with ctx.step("storm") as s:
        run = await ctx.start(t.name, "logs", {"seconds": 60, "rate": 50, "error_pct": 10})
        s.note(f"{run['id']} on {t.display}: 50 lines a second for 60 s, 10 % errors")
    async with ctx.step("emitted") as s:
        done = await ctx.wait_for(lambda: finished(ctx, t.name, run["id"]), 10, lambda _: "the storm is running")
        result = done.get("result") or {}
        emitted = int(result.get("emitted") or 0)
        started, ended = parse_iso(done.get("started_at")), parse_iso(done.get("ended_at")) or ctx.now()
        by = {k: result.get(f"count_{k.lower()}") for k in ("ERROR", "WARN", "INFO", "DEBUG")}
        s.data.update(emitted=emitted, by_severity=by)
        s.note(f"{t.display} reports {emitted:,} lines: " + ", ".join(f"{k} {v}" for k, v in by.items()))
    checks: list[dict] = []
    async with ctx.step("search") as s:
        begin = ctx.now()

        async def counted() -> bool:
            window_end = min(ended + timedelta(seconds=60), ctx.now())
            got = await ctx.deps.log_panels.count(t.region, t.service_name, started or begin, window_end)
            checks.append({"t": iso(ctx.now()), "count": got["count"]})
            s.data["checks"] = checks
            return got["count"] >= emitted or (ctx.now() - begin).total_seconds() >= 360

        await ctx.wait_for(counted, 20, lambda _: f"Insights returned {checks[-1]['count'] if checks else 0} so far")
        s.note(f"{len(checks)} searches; the last returned {checks[-1]['count']:,}")
    count = checks[-1]["count"] if checks else 0
    async with ctx.step("verdict") as s:
        if count == 0 and emitted:
            verdict, text = "not collected (A6b)", a6b_text(t.display, emitted, started, ended, t.service_name,
                                                            ctx.now())
        else:
            verdict, text = "collected", (f"Collected: Insights returned {count:,} of {emitted:,} lines for service "
                                          f"{t.service_name} (checked {hhmm(ctx.now())})")
        s.data["verdict"] = verdict
        s.note(text)
    return {"emitted": emitted, "insights_count": count, "verdict": verdict, "checked_at": iso(ctx.now())}


async def chain(ctx: Any) -> dict:
    t = tentacle(ctx, ctx.params.get("target"))
    async with ctx.step("start-chain") as s:
        run = await ctx.start(t.name, "chain", {"count": 20, "latency_ms": 250, "error_pct": 20})
        s.note(f"{run['id']} on {t.display}: 20 requests, 250 ms per hop, 20 % injected errors")
    async with ctx.step("collect") as s:
        done = await ctx.wait_for(lambda: finished(ctx, t.name, run["id"]), 5, lambda _: "the chain is running")
        res = done.get("result") or {}
        s.data.update({k: res.get(k) for k in ("ok", "failed", "first_trace_id", "last_trace_id")})
        s.note(f"{res.get('ok')} ok, {res.get('failed')} failed; first trace {res.get('first_trace_id')}")
    async with ctx.step("own-span") as s:
        trace_id = ctx.run.trace_id
        trace = ctx.deps.telemetry.ring.find(trace_id) if trace_id else None
        start_span = None
        if trace:
            start_span = next((x for x in trace["spans"] if "/scenario/chain" in str(x["attributes"]) or
                               x["name"].startswith("POST")), None)
        s.data.update(trace_id=trace_id, span=start_span["name"] if start_span else None)
        s.note(f"the head's start call is in trace {trace_id}" + (f" as span {start_span['name']}" if start_span else
                                                                  " (span not in the ring any more)"))
    async with ctx.step("link") as s:
        link = ctx.deps.links.link("insights.traces")
        if link["url"]:
            s.artifact("link", link["url"], verified=link["verified"])
        s.note(f"search the Traces tab for {res.get('first_trace_id')}" +
               ("" if link["verified"] else " (the tab URL is not verified yet)"))
    return {"ok": res.get("ok"), "failed": res.get("failed"), "first_trace_id": res.get("first_trace_id"),
            "head_trace_id": ctx.run.trace_id}


async def deep_water(ctx: Any) -> dict:
    sea = ctx.fleet.sea
    plan = [("start-pg", "pg", "database"), ("start-fn", "fn", "functions"), ("start-lb", "lb", "load_balancer")]
    watched: list[tuple[str, Any]] = []
    for step, scenario, kind in plan:
        async with ctx.step(step) as s:
            spec = sea.get(kind)
            if spec is None or (kind == "functions" and not spec.extra.get("url")) or (
                    kind == "load_balancer" and not spec.extra.get("ip")):
                s.skip(f"no {kind.replace('_', ' ')} in the fleet description")
            target = tentacle(ctx, None).name if scenario == "pg" else "head"
            params = {"pg": {"seconds": 60, "clients": 4}, "fn": {"seconds": 60, "rps": 5},
                      "lb": {"seconds": 60, "rps": 20}}[scenario]
            run = await ctx.start(target, scenario, params)
            watched.append((kind, spec))
            s.note(f"{run['id']} on {target}")
    if not watched:
        raise VoyageFailed("the fleet description has no managed Postgres, Function or load balancer")
    present: dict[str, float | None] = {kind: None for kind, _ in watched}
    moved: dict[str, float | None] = {kind: None for kind, _ in watched}
    baseline: dict[str, float | None] = {}
    async with ctx.step("watch") as s:
        begin = ctx.now()
        for kind, spec in watched:
            metric = ctx.deps.panels.probe_metrics.get(kind)
            if metric:
                s.artifact("chart", metric, region=spec.region)

        async def families() -> bool:
            for kind, spec in watched:
                metric = ctx.deps.panels.probe_metrics.get(kind)
                if not metric or not spec.region or moved[kind] is not None:
                    continue
                v = await value(ctx, metric, spec.name, spec.region, "max")
                if v is None:
                    continue
                if present[kind] is None:
                    present[kind], baseline[kind] = ctx.since(begin), v  # first sample: Insights lags the load
                elif v > baseline[kind] * 1.1 + 0.5:
                    moved[kind] = ctx.since(begin)
            return all(v is not None for v in moved.values()) or (ctx.now() - begin).total_seconds() >= 300

        await ctx.wait_for(families, 30, lambda _: f"moved so far: {[k for k, v in moved.items() if v is not None]}")
        s.data.update(baseline=baseline, present_after_s=present, moved_after_s=moved)
        s.note(", ".join(f"{k}: " + ("no data" if present[k] is None else "moved after " + format(moved[k], ".0f")
                                     + " s" if moved[k] is not None else "data but no change")
                         for k in present))
    async with ctx.step("summary") as s:
        missing = [k for k, v in present.items() if v is None]
        still = [k for k, v in moved.items() if v is None and present[k] is not None]
        s.note(f"{len(present) - len(missing)} of {len(present)} families reported within 5 minutes, "
               f"{len([v for v in moved.values() if v is not None])} moved with the load"
               + (f"; no data: {', '.join(missing)}" if missing else "")
               + (f"; no change: {', '.join(still)}" if still else ""))
    return {"reported_after_s": present, "moved_after_s": moved, "missing": missing}
