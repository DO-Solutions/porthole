"""The log, trace and managed-resource voyages of design Appendix C: log storm, chain and deep water.

The log storm compares what a tentacle wrote with what Insights returned and gives the A6b verdict when Insights
has nothing; deep water checks which managed families report within five minutes and which of them rise above
the level they had in the five minutes before the load."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from porthole.clock import hhmm, iso, parse_iso
from porthole.panels_logs import a6b_text
from porthole.voyages import VoyageFailed
from porthole.voyages_metrics import finished, tentacle, value, window_mean


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


# 4 clients for 60 s left a 1 vCPU managed Postgres between 15 and 20 % CPU (v-aba9cd); the load has to outlast the
# two-minute database series and Insights' lag inside the five-minute watch. 8 clients is the head's cap for pg.
PG_SECONDS, PG_CLIENTS = 180, 8
# Database metrics arrive every two minutes (A40), so the first sample after the load starts can predate it; the
# moved check compares with the mean over the five minutes before the load instead.
BASELINE_S = 300
PLAN = (("start-pg", "pg", "database"), ("start-fn", "fn", "functions"), ("start-lb", "lb", "load_balancer"))


def pg_tentacle(ctx: Any, region: str | None) -> Any:
    """Where the pg clients run: the voyage's target, else a tentacle in the database's region that the round-trip
    CPU rule does not watch, else the first tentacle. The clients load the tentacle they run on (69 % CPU with 8
    clients on a 1 vCPU Droplet, B-035), and on the rule's tentacle that fires the rule and spoils the next round
    trip's baseline."""
    if ctx.params.get("target"):
        return tentacle(ctx, ctx.params["target"])
    rule = ctx.fleet.rule("round-trip")
    watched = {rule.target} if rule and rule.target else set()
    same_region = [t for t in ctx.fleet.tentacles if t.region == region]
    return next((t for t in same_region + list(ctx.fleet.tentacles) if t.name not in watched), None) or tentacle(
        ctx, None)


def _missing(kind: str, spec: Any) -> bool:
    return spec is None or (kind == "functions" and not spec.extra.get("url")) or (
        kind == "load_balancer" and not spec.extra.get("ip"))


def _num(v: float | None) -> str:
    return "none" if v is None else format(v, ".3g")


async def deep_water(ctx: Any) -> dict:
    sea, panels = ctx.fleet.sea, ctx.deps.panels
    seen_by = {kind: panels.probe_metrics.get(kind) for _, _, kind in PLAN}
    moved_by = {kind: panels.moved_metrics.get(kind) or seen_by[kind] for _, _, kind in PLAN}
    usable = [kind for _, _, kind in PLAN if not _missing(kind, sea.get(kind))]
    if not usable:
        raise VoyageFailed("the fleet description has no managed Postgres, Function or load balancer")
    baseline: dict[str, float | None] = {}
    samples: dict[str, int] = {}
    async with ctx.step("baseline") as s:
        for kind in usable:
            spec = sea[kind]
            if moved_by[kind] and spec.region:
                baseline[kind], samples[kind] = await window_mean(ctx, moved_by[kind], spec.name, spec.region,
                                                                  BASELINE_S, "max")
        s.data.update(baseline=baseline, samples=samples)
        s.note(", ".join(f"{k} {moved_by[k]} {_num(baseline.get(k))} ({samples.get(k, 0)} samples)" for k in usable)
               + f" over the {BASELINE_S // 60} minutes before the load")
    watched: list[tuple[str, Any]] = []
    for step, scenario, kind in PLAN:
        async with ctx.step(step) as s:
            spec = sea.get(kind)
            if kind not in usable:
                s.skip(f"no {kind.replace('_', ' ')} in the fleet description")
            target = pg_tentacle(ctx, spec.region).name if scenario == "pg" else "head"
            params = {"pg": {"seconds": PG_SECONDS, "clients": PG_CLIENTS}, "fn": {"seconds": 60, "rps": 5},
                      "lb": {"seconds": 60, "rps": 20}}[scenario]
            run = await ctx.start(target, scenario, params)
            watched.append((kind, spec))
            s.note(f"{run['id']} on {target}")
    present: dict[str, float | None] = {kind: None for kind, _ in watched}
    moved: dict[str, float | None] = {kind: None for kind, _ in watched}
    last: dict[str, float] = {}
    async with ctx.step("watch") as s:
        begin = ctx.now()
        for kind, spec in watched:
            if moved_by[kind]:
                s.artifact("chart", moved_by[kind], region=spec.region)

        async def families() -> bool:
            for kind, spec in watched:
                if not moved_by[kind] or not spec.region or moved[kind] is not None:
                    continue
                v = await value(ctx, moved_by[kind], spec.name, spec.region, "max")
                if present[kind] is None and (v is not None or (seen_by[kind] not in (None, moved_by[kind]) and
                                                                await value(ctx, seen_by[kind], spec.name,
                                                                            spec.region, "max") is not None)):
                    present[kind] = ctx.since(begin)
                if v is None:
                    continue
                last[kind] = v
                if baseline.get(kind) is None:
                    baseline[kind] = v  # nothing before the load: the first sample stands in
                elif v > baseline[kind] * 1.1 + 0.5:
                    moved[kind] = ctx.since(begin)
            return all(v is not None for v in moved.values()) or (ctx.now() - begin).total_seconds() >= 300

        await ctx.wait_for(families, 30, lambda _: f"moved so far: {[k for k, v in moved.items() if v is not None]}")
        metrics = {kind: moved_by[kind] for kind in present}
        s.data.update(baseline=baseline, present_after_s=present, moved_after_s=moved, metrics=metrics,
                      seen_metrics={kind: seen_by[kind] for kind in present}, last=last)

        def said(k: str) -> str:
            name = metrics[k] or "no probe metric"
            if seen_by[k] and seen_by[k] != metrics[k]:
                name = f"moved by {name}, seen by {seen_by[k]}"
            if present[k] is None:
                return f"{k} ({name}): no data"
            values = f"{_num(baseline.get(k))} before, {_num(last.get(k))} last"
            if moved[k] is None:
                return f"{k} ({name}): data but no change ({values})"
            return f"{k} ({name}): moved after {moved[k]:.0f} s ({values})"

        s.note(", ".join(said(k) for k in present))
    async with ctx.step("summary") as s:
        missing = [k for k, v in present.items() if v is None]
        still = [k for k, v in moved.items() if v is None and present[k] is not None]
        s.note(f"{len(present) - len(missing)} of {len(present)} families reported within 5 minutes, "
               f"{len([v for v in moved.values() if v is not None])} moved with the load"
               + (f"; no data: {', '.join(missing)}" if missing else "")
               + (f"; no change: {', '.join(still)}" if still else ""))
    return {"reported_after_s": present, "moved_after_s": moved, "missing": missing}
