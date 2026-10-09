"""The scripted deckhand, phase 1 of the Kraken's Brain: rule based, deterministic, kept in memory (20 sessions).

It reads the question for a tentacle, a region and a symptom, runs the matching read tools, explains what it
found with the numbers, and proposes at most one action as an approval request that waits for the captain."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from porthole.brain.adapter import BrainContext, BrainEvent, BrainSession
from porthole.brain.tools import Tool, ToolResult
from porthole.clock import hhmm, iso, new_id, parse_iso
from porthole.panels_logs import a6b_text
from porthole.security import ApiError

SUGGESTED = ("why is tentacle-2 slow?", "is anything alerting right now?", "did the log storm reach Insights?")
LOAD_SCENARIOS = ("cpu", "memory", "disk", "network", "pg")
TERMINAL = ("done", "failed")


@dataclass
class _Session:
    id: str
    question: str
    created_at: str
    actor: str
    state: str = "starting"
    events: list[BrainEvent] = field(default_factory=list)
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    task: asyncio.Task | None = None
    next_id: int = 1


def _series_text(res: ToolResult, label: str, unit: str = " %") -> str:
    if not res["ok"]:
        return f"{label}: not available ({res['summary']})"
    points = [p for s in (res["data"] or {}).get("series") or [] for p in s["points"]]
    if not points:
        return f"{label}: Insights has no samples in the last 30 minutes"
    values = [v for _, v in points]
    text = f"{label} is {values[-1]:.1f}{unit} now, peak {max(values):.1f}{unit} in the last 30 minutes"
    high = next((t for t, v in points if v >= 90), None)
    if high is not None and values[-1] >= 90:
        text += f", above 90{unit} since {hhmm(datetime.fromtimestamp(high, timezone.utc))}"
    return text


class Deckhand:
    name = "deckhand"

    def __init__(self, deps: Any, tools: dict[str, Tool], max_sessions: int = 20, approval_ttl_s: float = 300):
        self.deps, self.tools = deps, tools
        self.max_sessions, self.approval_ttl_s = max_sessions, approval_ttl_s
        self.sessions: OrderedDict[str, _Session] = OrderedDict()

    # --- the protocol ------------------------------------------------------------------------------

    def _session(self, session_id: str) -> _Session:
        s = self.sessions.get(session_id)
        if s is None:
            raise ApiError(404, "no_session", f"no Brain session {session_id} (the last {self.max_sessions} are kept)")
        return s

    @staticmethod
    def view(s: _Session) -> BrainSession:
        return {"id": s.id, "question": s.question, "created_at": s.created_at, "state": s.state,  # type: ignore
                "backend": "deckhand"}

    async def start(self, question: str, ctx: BrainContext) -> BrainSession:
        s = _Session(new_id("s"), question.strip(), iso(self.deps.clock.now()) or "", ctx["actor"])
        self.sessions[s.id] = s
        while len(self.sessions) > self.max_sessions:
            _, old = self.sessions.popitem(last=False)
            if old.task and not old.task.done():
                old.task.cancel()
        s.task = asyncio.get_running_loop().create_task(self._run(s), name=s.id)
        return self.view(s)

    async def events(self, session_id: str, after: str | None = None) -> AsyncIterator[BrainEvent]:
        s = self._session(session_id)
        idx = next((i + 1 for i, e in enumerate(s.events) if e["id"] == after), 0) if after else 0
        while True:
            async with s.cond:
                while idx >= len(s.events) and s.state not in TERMINAL:
                    await s.cond.wait()
                batch, idx, finished = s.events[idx:], len(s.events), s.state in TERMINAL
            for ev in batch:
                yield ev
            if finished and idx >= len(s.events):
                return

    async def approve(self, session_id: str, approval_id: str, decision: str, actor: str) -> None:
        s = self._session(session_id)
        fut = s.pending.get(approval_id)
        if fut is None or fut.done():
            raise ApiError(409, "no_approval", "that approval is not waiting (answered, expired or unknown)")
        fut.set_result((decision, actor))

    async def cancel(self, session_id: str) -> None:
        s = self._session(session_id)
        if s.task and not s.task.done():
            s.task.cancel()

    async def get(self, session_id: str) -> BrainSession:
        return self.view(self._session(session_id))

    async def close(self) -> None:
        """Cancel every unfinished session at shutdown, so their event streams end."""
        tasks = [s.task for s in self.sessions.values() if s.task and not s.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def snapshot(self, session_id: str) -> dict:
        s = self._session(session_id)
        return {**self.view(s), "events": list(s.events)}

    # --- plumbing -----------------------------------------------------------------------------------

    async def _emit(self, s: _Session, kind: str, data: dict) -> None:
        ev: BrainEvent = {"id": str(s.next_id), "t": iso(self.deps.clock.now(), millis=True) or "",
                          "type": kind, "data": data}  # type: ignore[typeddict-item]
        s.next_id += 1
        async with s.cond:
            s.events.append(ev)
            s.cond.notify_all()

    async def _state(self, s: _Session, state: str) -> None:
        async with s.cond:
            s.state = state
            s.cond.notify_all()
        self.deps.hub.publish("brain", {"session_id": s.id, "state": state})

    async def _tool(self, s: _Session, name: str, args: dict) -> ToolResult:
        call_id = new_id("t")
        await self._emit(s, "tool_call", {"call_id": call_id, "tool": name, "args": args})
        res = await self.tools[name].fn(args)
        await self._emit(s, "tool_result", {"call_id": call_id, "ok": res["ok"], "summary": res["summary"],
                                            "trace_ids": res["trace_ids"]})
        return res

    async def _ask(self, s: _Session, tool: str, args: dict, reason: str) -> ToolResult | str:
        """An approval request; the action runs only after the captain approves. Returns the tool result,
        'deny' or 'expired'."""
        approval_id = new_id("a")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        s.pending[approval_id] = fut
        expires = self.deps.clock.now() + timedelta(seconds=self.approval_ttl_s)
        await self._emit(s, "approval_request", {"approval_id": approval_id, "tool": tool, "args": args,
                                                 "reason": reason, "expires_at": iso(expires)})
        await self._state(s, "waiting_approval")
        timer = asyncio.get_running_loop().create_task(self.deps.clock.sleep(self.approval_ttl_s))
        try:
            await asyncio.wait({fut, timer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
        decision, actor = fut.result() if fut.done() else ("expired", "deckhand")
        if not fut.done():
            fut.cancel()
        s.pending.pop(approval_id, None)
        await self._emit(s, "approval_resolved", {"approval_id": approval_id, "decision": decision, "actor": actor,
                                                  "t": iso(self.deps.clock.now())})
        await self._state(s, "running")
        if decision == "approve":
            return await self._tool(s, tool, args)
        return decision

    async def _say(self, s: _Session, text: str) -> None:
        await self._emit(s, "message", {"text": text, "format": "markdown"})

    # --- the scripts --------------------------------------------------------------------------------

    async def _run(self, s: _Session) -> None:
        try:
            await self._state(s, "running")
            q = s.question.lower()
            fleet = self.deps.settings.fleet
            t = next((x for x in fleet.tentacles if x.display in q or x.name in q or
                      x.display.replace("-", " ") in q), None)
            if t is not None and any(w in q for w in ("slow", "cpu", "memory", "disk", "network", "busy", "load")):
                await self._slow(s, t)
            elif "alert" in q:
                await self._alerts(s)
            elif "log" in q:
                await self._logs(s)
            else:
                await self._unknown(s)
            await self._state(s, "done")
        except asyncio.CancelledError:
            await self._emit(s, "error", {"code": "cancelled", "message": "the session was cancelled"})
            await self._state(s, "failed")
        except Exception as e:  # a script bug ends the session, not the head
            self.deps.log.error(f"deckhand failed: {type(e).__name__}: {e}", session_id=s.id)
            await self._emit(s, "error", {"code": "internal", "message": f"{type(e).__name__}: {e}"})
            await self._state(s, "failed")

    async def _slow(self, s: _Session, t: Any) -> None:
        await self._emit(s, "status", {"text": f"looking at {t.display} in {t.region}"})
        await self._emit(s, "thinking", {"text": "plan: read its health and running scenarios, then CPU and memory "
                                                 "in Insights for the last 30 minutes"})
        status = await self._tool(s, "tentacle_status", {"name": t.name})
        base = {"region": t.region, "filters": [f"resource_name={t.name}"], "agg": "avg", "range": "30m"}
        cpu = await self._tool(s, "insights_query_range", {**base, "metric": "do.droplets.cpu_utilization"})
        mem = await self._tool(s, "insights_query_range", {**base, "metric": "do.droplets.memory_utilization"})
        running = [r for r in ((status["data"] or {}).get("running") or [] if status["ok"] else [])
                   if r.get("name") in LOAD_SCENARIOS]
        lines = [_series_text(cpu, f"{t.display} CPU") + ".", _series_text(mem, "Memory") + "."]
        if running:
            r = running[0]
            started = parse_iso(r.get("started_at"))
            seconds = (r.get("params") or {}).get("seconds")
            ends = f" and ends around {hhmm(started + timedelta(seconds=seconds))}" if started and seconds else ""
            lines.append(f"A {r['name']} scenario (`{r['id']}`) started at {hhmm(started)}{ends}; that explains it.")
        else:
            lines.append("Nothing is running on it, so these numbers are its normal load.")
        await self._say(s, "\n\n".join(lines))
        if running:
            r = running[0]
            outcome = await self._ask(s, "scenario_stop", {"target": t.name, "run_id": r["id"]},
                                      f"stop the {r['name']} scenario that explains the load on {t.display}")
            await self._say(s, self._outcome(outcome, f"Stopped `{r['id']}`. Insights should show the drop within "
                                                      "a minute or two.", f"Left `{r['id']}` running."))
        await self._emit(s, "done", {"summary": f"checked {t.display}" + (" and proposed a stop" if running else "")})

    @staticmethod
    def _outcome(outcome: ToolResult | str, approved: str, denied: str) -> str:
        if isinstance(outcome, dict):
            return approved if outcome["ok"] else f"I tried, and it failed: {outcome['summary']}"
        if outcome == "deny":
            return denied
        return "Nobody answered within 5 minutes, so I did nothing."

    async def _alerts(self, s: _Session) -> None:
        await self._emit(s, "status", {"text": "checking the instances of the fleet's alert rules"})
        res = await self._tool(s, "insights_alert_instances", {"status": ""})
        if not res["ok"]:
            await self._say(s, f"I could not read the alerts: {res['summary']}")
            await self._emit(s, "done", {"summary": "alerts unavailable"})
            return
        active = [i for i in res["data"] if i["status"] == "active"]
        if active:
            await self._say(s, "Yes:\n\n" + "\n".join(
                f"- {i['rule_name']} on {i['entity']}: {i['severity']}, value {i['value']} since "
                f"{hhmm(parse_iso(i['triggered_at']))}" for i in active))
            await self._emit(s, "done", {"summary": f"{len(active)} alerting"})
            return
        last = next((i for i in res["data"] if i["status"] == "resolved"), None)
        text = "Nothing is alerting right now."
        if last:
            text += (f" The last alert was {last['rule_name']} on {last['entity']}, resolved at "
                     f"{hhmm(parse_iso(last['resolved_at']))}.")
        await self._say(s, text)
        outcome = await self._ask(s, "voyage_start", {"name": "alert-round-trip"},
                                  "to watch an alert fire and resolve, start the Alert round trip voyage")
        await self._say(s, self._outcome(outcome, "The Alert round trip set sail; follow it on the Stir page.",
                                         "Fine, no voyage."))
        await self._emit(s, "done", {"summary": "nothing alerting"})

    async def _logs(self, s: _Session) -> None:
        fleet = self.deps.settings.fleet
        await self._emit(s, "status", {"text": "looking for log storms in the last hour"})
        described = await self._tool(s, "fleet_describe", {})
        now = self.deps.clock.now()
        storms = []
        for t in (described["data"] or {}).get("tentacles") or [] if described["ok"] else []:
            for r in t.get("recent") or []:
                started = parse_iso(r.get("started_at"))
                if r.get("name") == "logs" and started and now - started <= timedelta(hours=1):
                    storms.append((fleet.tentacle(t["name"]), r))
        if not storms:
            first = fleet.tentacles[0].name if fleet.tentacles else ""
            await self._say(s, "No log storm finished in the last hour, so there is nothing to look for yet.")
            outcome = await self._ask(s, "scenario_start", {"target": first, "scenario": "logs",
                                                            "params": {"seconds": 60, "rate": 50, "error_pct": 10}},
                                      "start a one-minute log storm so there is something to look for")
            await self._say(s, self._outcome(outcome, "Started a log storm. Ask me again in two minutes.",
                                             "Fine, no log storm."))
            await self._emit(s, "done", {"summary": "no log storm to check"})
            return
        lines = []
        for t, r in storms[:3]:
            res = await self._tool(s, "insights_logs_search", {"region": t.region, "service": t.service_name,
                                                               "range": "1h"})
            count = (res["data"] or {}).get("count", 0) if res["ok"] else None
            emitted = int((r.get("result") or {}).get("emitted") or 0)
            if count == 0:
                lines.append(a6b_text(t.display, emitted, parse_iso(r.get("started_at")), parse_iso(r.get("ended_at")),
                                      t.service_name, now))
            elif count is None:
                lines.append(f"{t.display}: could not ask Insights ({res['summary']})")
            else:
                more = "+" if (res["data"]["pagination"] or {}).get("has_more") else ""
                lines.append(f"Collected: Insights returned {count}{more} records for {t.service_name}, "
                             f"which wrote {emitted:,} lines.")
        await self._say(s, "\n\n".join(lines))
        await self._emit(s, "done", {"summary": f"checked {len(lines)} log storm(s)"})

    async def _unknown(self, s: _Session) -> None:
        await self._emit(s, "status", {"text": "I know three questions today"})
        described = await self._tool(s, "fleet_describe", {})
        known = "\n".join(f"- {q}" for q in SUGGESTED)
        await self._say(s, f"I only know these questions today:\n\n{known}\n\n"
                           f"Here is the fleet: {described['summary']}.")
        await self._emit(s, "done", {"summary": "answered with the fleet summary"})
