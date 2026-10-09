"""The metric and alert voyages of design Appendix C (churn, alert round trip, ballast, two seas) and helpers.

Each records the times the design names: burn to crossing, crossing to alert, alert to webhook, stop to
resolved, and when each region showed a burn. voyages_catalog.py declares their planned steps."""
from __future__ import annotations

import operator
import re
from datetime import timedelta
from typing import Any

from porthole import promql
from porthole.clock import hhmm, iso, parse_iso
from porthole.panels_alerts import rule_view
from porthole.scenarios import CATALOG as SCENARIOS
from porthole.security import ApiError
from porthole.voyages import VoyageFailed

WINDOW_S = {"1m": 60, "5m": 300, "10m": 600, "15m": 900, "30m": 1800, "1h": 3600}
OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "=": operator.eq, "!=": operator.ne}
CPU = "do.droplets.cpu_utilization"
MEMORY, FS_FREE, NET_TX = "do.droplets.memory_utilization", "do.droplets.filesystem_free", "do.droplets.network_tx"
# Ballast reads one device label from each family that carries it (A37): the filesystem series carry
# filesystem_mountpoint, the network series network_device. The match[] stays, because label values without one
# come from every product in the region (the managed databases' mountpoints too).
BALLAST_LABELS = (("filesystem_mountpoint", FS_FREE), ("network_device", NET_TX))
# Every metric name a voyage asks for by name; the head checks them against watcher/catalog at startup.
METRICS = (CPU, MEMORY, FS_FREE, NET_TX)
# Ballast asks each tentacle for up to 500 MB and leaves it 150 MB, 50 more than the tentacle's own 100 MB guard
# (a 1 GB Droplet has about 590 MB available, B-032). The floor is the memory scenario's minimum.
BALLAST_MEMORY_MB, BALLAST_SPARE_MB = 500, 150
MEMORY_FLOOR_MB = int(next(p.low for p in SCENARIOS["memory"].params if p.name == "mb"))
MEM_AVAILABLE = re.compile(r"MemAvailable (\d+) MB")


def pct(v: float | None) -> str:
    return "no data" if v is None else f"{v:.1f} %"


def tentacle(ctx: Any, name: str | None) -> Any:
    t = ctx.fleet.tentacle(name or "") or (ctx.fleet.tentacles[0] if ctx.fleet.tentacles else None)
    if t is None:
        raise VoyageFailed("the fleet description has no tentacles")
    return t


async def value(ctx: Any, metric: str, name: str, region: str, agg: str = "avg") -> float | None:
    """One fresh instant value for one fleet member, selected by its URN (no panel cache: voyages need each
    reading)."""
    member = ctx.fleet.entity(name)
    if member is None:
        raise VoyageFailed(f"{name} is not in the fleet description")
    q = promql.build(metric, agg, promql.member_filters(member), region, ctx.fleet)
    body = await ctx.insights("query", q, region=region)
    result = ((body or {}).get("data") or {}).get("result") or []
    return float(result[0]["value"][1]) if result else None


async def finished(ctx: Any, target: str, run_id: str) -> dict | None:
    view = await ctx.run_view(target, run_id)
    return view if view and view.get("status") != "running" else None


async def churn(ctx: Any) -> dict:
    t = tentacle(ctx, ctx.params.get("target"))
    last: dict[str, Any] = {}
    async with ctx.step("baseline") as s:
        base = await value(ctx, CPU, t.name, t.region)
        s.data["cpu"] = base
        s.note(f"cpu {pct(base)} on {t.display}")
    async with ctx.step("burn") as s:
        run = await ctx.start(t.name, "cpu", {"seconds": 600, "workers": 1})
        burn_at = ctx.now()
        s.artifact("chart", CPU, region=t.region)
        s.note(f"{run['id']} on {t.display} for 600 s")

    async def above() -> tuple | None:
        last["v"] = await value(ctx, CPU, t.name, t.region)
        return (last["v"],) if last["v"] is not None and last["v"] > 50 else None

    async with ctx.step("metric-appears") as s:
        (seen,) = await ctx.wait_for(above, 30, lambda _: f"waiting; last {pct(last.get('v'))} at {hhmm(ctx.now())}")
        appear_s = ctx.since(burn_at)
        s.data.update(value=seen, after_s=appear_s)
        s.note(f"{pct(seen)} at {hhmm(ctx.now())} (+{appear_s:.0f} s after the burn started)")
    peak = seen

    async def run_over() -> dict | None:
        nonlocal peak
        v = await value(ctx, CPU, t.name, t.region)
        peak = max(peak, v) if v is not None else peak
        return await finished(ctx, t.name, run["id"])

    async with ctx.step("ride-out") as s:
        ended = await ctx.wait_for(run_over, 30, lambda _: f"sampling every 30 s; peak {pct(peak)} so far")
        end_at = parse_iso(ended.get("ended_at")) or ctx.now()
        s.note(f"run {ended['status']} at {hhmm(end_at)}; peak {pct(peak)}")

    async def below() -> tuple | None:
        last["v"] = await value(ctx, CPU, t.name, t.region)
        return (last["v"],) if last["v"] is not None and last["v"] < 50 else None

    async with ctx.step("settle") as s:
        await ctx.wait_for(below, 30, lambda _: f"waiting; last {pct(last.get('v'))}")
        settle_s = round((ctx.now() - end_at).total_seconds(), 1)
        s.note(f"below 50 % {settle_s:.0f} s after the run ended")
    async with ctx.step("summary") as s:
        s.note(f"Insights showed the burn after {appear_s:.0f} s, peaked at {pct(peak)}, and settled {settle_s:.0f} s "
               "after the run ended")
    return {"burn_to_appear_s": appear_s, "peak_pct": round(peak, 1), "settle_s": settle_s}


async def alert_round_trip(ctx: Any) -> dict:
    ref = ctx.fleet.rule("round-trip")
    summary: dict[str, Any] = {}
    async with ctx.step("read-rule") as s:
        if ref is None:
            raise VoyageFailed("no rule with purpose round-trip in PORTHOLE_FLEET_JSON")
        rule = rule_view((await ctx.insights("get_rule", ref.id)).get("alert_rule") or {}, ref)
        s.data["rule"] = {k: rule[k] for k in ("id", "name", "operator", "warning", "critical", "window", "re_alert")}
        s.note(f"{rule['name']}: {rule['operator']} {rule['critical']} critical, window {rule['window']}, "
               f"re-alert {rule['re_alert']}")
        if rule["status"] != "active":
            raise VoyageFailed(f"the rule is {rule['status']}; resume it before sailing")
    t = tentacle(ctx, ctx.params.get("target") or ref.target)
    urn = (rule["resource_urns"] or [t.urn])[0]
    crosses = OPS.get(rule["operator"], operator.ge)
    level = float(rule["critical"] if rule["critical"] is not None else rule["warning"])
    seconds = min(600, 3 * WINDOW_S.get(rule["window"], 300) + 120)
    last: dict[str, Any] = {}
    async with ctx.step("baseline") as s:
        s.data["cpu"] = await value(ctx, CPU, t.name, t.region)
        s.note(f"cpu {pct(s.data['cpu'])} on {t.display}")
    async with ctx.step("burn") as s:
        run = await ctx.start(t.name, "cpu", {"seconds": seconds, "workers": 1})
        burn_at = ctx.now()
        s.artifact("chart", CPU, region=t.region)
        s.note(f"{run['id']} on {t.display}, {seconds} s")

    async def crossed(want: bool) -> tuple | None:
        last["v"] = await value(ctx, CPU, t.name, t.region)
        return (last["v"],) if last["v"] is not None and crosses(last["v"], level) == want else None

    async with ctx.step("metric-crosses") as s:
        (v,) = await ctx.wait_for(lambda: crossed(True), 15, lambda _: f"waiting; last {pct(last.get('v'))}")
        cross_at = ctx.now()
        summary["burn_to_cross_s"] = ctx.since(burn_at) - ctx.since(cross_at)
        s.note(f"{pct(v)} at {hhmm(cross_at)} (+{summary['burn_to_cross_s']:.0f} s)")

    async def instance(status: str, instance_id: str | None = None) -> dict | None:
        body = await ctx.insights("list_instances", rule_id=ref.id, per_page=100)
        for i in body.get("alert_instances") or []:
            trig = parse_iso(i.get("triggered_at"))
            fresh = trig is None or trig >= burn_at - timedelta(seconds=120)
            if str(i.get("status", "")).endswith(status) and i.get("resource_urn") == urn and fresh and (
                    instance_id is None or i.get("id") == instance_id):
                return i
        return None

    async with ctx.step("alert-active") as s:
        inst = await ctx.wait_for(lambda: instance("ACTIVE"), 15, lambda _: "waiting for an ACTIVE instance")
        active_at = ctx.now()
        summary["cross_to_active_s"] = round((active_at - cross_at).total_seconds(), 1)
        s.data.update(instance_id=inst.get("id"), triggered_at=inst.get("triggered_at"), value=inst.get("value"))
        severity = str(inst.get("severity") or "").replace("SEVERITY_", "").lower()
        s.note(f"instance {inst.get('id')} {severity} value {inst.get('value')} "
               f"(+{summary['cross_to_active_s']:.0f} s after the crossing)")
    async with ctx.step("webhook-delivered") as s:
        async def delivered() -> dict | None:
            return ctx.deps.hooks.find(iso(burn_at), rule_id=ref.id, urn=urn)
        first = await ctx.wait_for(delivered, 5, lambda _: "waiting for a delivery to /hooks/insights")
        first["matched_voyage"] = ctx.run.id
        delivery_at = ctx.now()
        summary["active_to_delivery_s"] = round((delivery_at - active_at).total_seconds(), 1)
        s.artifact("delivery", first["id"])
        s.note(f"{first['id']}, signature {first['signature'].get('note') or 'verified'}")
    async with ctx.step("stop-burn") as s:
        await ctx.stop(t.name, run["id"])
        stop_at = ctx.now()
        s.note(f"stopped {run['id']} at {hhmm(stop_at)}")
    async with ctx.step("metric-falls") as s:
        (v,) = await ctx.wait_for(lambda: crossed(False), 15, lambda _: f"waiting; last {pct(last.get('v'))}")
        summary["stop_to_fall_s"] = round((ctx.now() - stop_at).total_seconds(), 1)
        s.note(f"{pct(v)} at {hhmm(ctx.now())}")
    async with ctx.step("alert-resolved") as s:
        await ctx.wait_for(lambda: instance("RESOLVED", inst.get("id")), 30, lambda _: "waiting for RESOLVED")
        resolved_at = ctx.now()
        summary["stop_to_resolved_s"] = round((resolved_at - stop_at).total_seconds(), 1)
        s.note(f"resolved at {hhmm(resolved_at)} (+{summary['stop_to_resolved_s']:.0f} s after the stop)")
    async with ctx.step("resolve-delivery") as s:
        async def resolve_hook() -> dict | None:
            return ctx.deps.hooks.find(iso(resolved_at - timedelta(seconds=30)), rule_id=ref.id, urn=urn,
                                       exclude=(first["id"],))
        second = await ctx.wait_for(resolve_hook, 5, lambda _: "waiting for the resolve delivery (optional)")
        second["matched_voyage"] = ctx.run.id
        summary["resolved_to_delivery_s"] = round((ctx.now() - resolved_at).total_seconds(), 1)
        s.artifact("delivery", second["id"])
        s.note(f"{second['id']}")
    async with ctx.step("summary") as s:
        s.note(", ".join(f"{k.replace('_s', '').replace('_', ' ')} {v:.0f} s" for k, v in summary.items()))
    return summary


def memory_ask(requested: int, avail_mb: int) -> int:
    """What to ask a tentacle with avail_mb available: the request, cut to leave BALLAST_SPARE_MB, not below the
    scenario's minimum. The tentacle refuses anything that would leave it under 100 MB."""
    return max(MEMORY_FLOOR_MB, min(requested, avail_mb - BALLAST_SPARE_MB))


async def hold_memory(ctx: Any, t: Any, requested: int, seconds: int, avail_mb: int | None) -> tuple[str, int, str]:
    """Start the memory scenario on one tentacle, sized from the fleet view's mem_avail_mb. A tentacle that does
    not report it is asked for the full request; if it answers 409 with its MemAvailable, the ask is resized
    from that and tried once more. Returns the run id, the MB taken and the note part."""
    ask = requested if avail_mb is None else memory_ask(requested, avail_mb)
    try:
        run = await ctx.start(t.name, "memory", {"seconds": seconds, "mb": ask})
    except ApiError as e:
        m = MEM_AVAILABLE.search(e.message) if e.status == 409 else None
        retry = memory_ask(requested, int(m.group(1))) if m else ask
        if retry >= ask:
            raise
        avail_mb, ask = int(m.group(1)), retry
        run = await ctx.start(t.name, "memory", {"seconds": seconds, "mb": ask})
    seen = "" if avail_mb is None else f", {avail_mb} MB available"
    return run["id"], ask, f"{t.display}: {ask} MB (asked {requested}{seen})"


async def ballast(ctx: Any) -> dict:
    targets = [t for t in ctx.fleet.tentacles if t.peer][:2] or list(ctx.fleet.tentacles[:2])
    if not targets:
        raise VoyageFailed("the fleet description has no tentacles")
    refused: list[str] = []
    held: dict[str, int] = {}
    async with ctx.step("start-memory") as s:
        snap = ctx.deps.poller.snapshot or {}  # the fleet view from the last poll
        health = {x["name"]: x.get("health") or {} for x in snap.get("tentacles") or []}
        started, taken = [], []
        for t in targets:
            try:
                run_id, mb, part = await hold_memory(ctx, t, BALLAST_MEMORY_MB, 300,
                                                     health.get(t.name, {}).get("mem_avail_mb"))
            except ApiError as e:
                refused.append(f"{t.display} memory: {e.message}")
                continue
            started.append(run_id)
            held[t.display] = mb
            taken.append(part)
        refusals = f"; refused: {'; '.join(refused)}" if refused else ""
        s.data["held_mb"] = held
        s.note(f"{', '.join(taken) or 'started nothing'}{refusals}")
        if not started:
            raise VoyageFailed("no tentacle accepted memory")
    plan = (("start-disk", "disk", {"seconds": 300, "mb": 2048}),
            ("start-network", "network", {"seconds": 180, "mbps": 50}))
    for step, scenario, params in plan:
        async with ctx.step(step) as s:
            started = []
            for t in targets:
                try:
                    started.append((await ctx.start(t.name, scenario, params))["id"])
                except ApiError as e:
                    refused.append(f"{t.display} {scenario}: {e.message}")
            refusals = f"; refused: {'; '.join(refused)}" if refused else ""
            s.note(f"started {', '.join(started) or 'nothing'}{refusals}")
            if not started:
                raise VoyageFailed(f"no tentacle accepted {scenario}")
    peaks: dict[str, float] = {}
    async with ctx.step("watch") as s:
        start = ctx.now()
        for metric in (MEMORY, FS_FREE, NET_TX):
            s.artifact("chart", metric, region=targets[0].region)

        async def sampled() -> bool:
            for t in targets:
                v = await value(ctx, MEMORY, t.name, t.region, "max")
                if v is not None:
                    peaks[t.display] = round(max(v, peaks.get(t.display, 0.0)), 1)
            return (ctx.now() - start).total_seconds() >= 360
        await ctx.wait_for(sampled, 60, lambda _: f"memory peaks so far {peaks}")
        s.note(f"memory peaks {peaks}")
    found: dict[str, list] = {}
    async with ctx.step("labels") as s:
        region = targets[0].region
        for label, metric in BALLAST_LABELS:
            match = [promql.selector(metric, {}, region, ctx.fleet)]
            body = await ctx.insights("label_values", label, match=match, region=region)
            found[label] = list(body.get("data") or [])
        labels = "; ".join(f"{k}: {', '.join(v) or 'none'}" for k, v in found.items())
        s.note(labels)
    async with ctx.step("summary") as s:
        s.note(f"memory held {held} MB, peaks {peaks}; {labels}" + (f"; refused {len(refused)}" if refused else ""))
    return {"memory_held_mb": held, "memory_peak_pct": peaks, "labels": found, "refused": refused}


async def two_seas(ctx: Any) -> dict:
    a = tentacle(ctx, None)
    b = next((t for t in ctx.fleet.tentacles if t.region != a.region), None)
    if b is None:
        raise VoyageFailed("two seas needs tentacles in two regions")
    appeared: dict[str, float] = {}
    async with ctx.step("burn") as s:
        for t in (a, b):
            await ctx.start(t.name, "cpu", {"seconds": 300, "workers": 1})
        burn_at = ctx.now()
        s.note(f"cpu for 300 s on {a.display} ({a.region}) and {b.display} ({b.region})")
    async with ctx.step("both-regions") as s:
        payload = await ctx.deps.panels.range("both", CPU, [], "avg", "15m", None)
        calls = {r: len(info["calls"]) for r, info in payload["regions"].items()}
        s.data["calls"] = calls
        s.artifact("chart", CPU, region="both")
        s.note("each region answered separately: " + ", ".join(f"{r} {n} call" for r, n in calls.items())
               + "; the series are overlaid, never summed")

    async def both_up() -> bool:
        for t in (a, b):
            if t.region not in appeared:
                v = await value(ctx, CPU, t.name, t.region)
                if v is not None and v > 50:
                    appeared[t.region] = ctx.since(burn_at)
        return len(appeared) == 2

    async with ctx.step("series-appear") as s:
        await ctx.wait_for(both_up, 30, lambda _: f"appeared so far: {appeared or 'none'}")
        s.note(", ".join(f"{r} after {v:.0f} s" for r, v in appeared.items()))
    async with ctx.step("summary") as s:
        s.note(f"{a.region} showed the burn after {appeared[a.region]:.0f} s, "
               f"{b.region} after {appeared[b.region]:.0f} s")
    return {f"{r}_appear_s": v for r, v in appeared.items()} | {"calls_per_region": calls}
