"""The scenario catalog with its public caps, start and stop on the tentacles, and the head-side fn and lb runs.

The caps are tighter than the tentacles' own limits and apply server side whatever the page sends. The head
mirrors the tentacle's run view for its own runs, so the console shows one shape for both."""
from __future__ import annotations

import asyncio
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from opentelemetry import context as otel_context

from porthole.apitrace import caller, traced_request
from porthole.clock import iso, new_id
from porthole.security import ApiError
from porthole.tentacles import TentacleError

HEAD_RUNS_KEPT = 200
HEAD_CONCURRENCY = 8


@dataclass(frozen=True)
class Param:
    name: str
    low: float
    high: float
    default: float
    kind: type = int


@dataclass(frozen=True)
class Scenario:
    name: str
    runs_on: str  # tentacle or head
    params: tuple[Param, ...]
    story: str
    requires: str | None = None  # peer, pg, fn, lb

    def view(self) -> dict:
        return {"name": self.name, "runs_on": self.runs_on, "story": self.story, "requires": self.requires,
                "params": [{"name": p.name, "min": p.low, "max": p.high, "default": p.default,
                            "type": p.kind.__name__} for p in self.params]}


CATALOG: dict[str, Scenario] = {s.name: s for s in (
    Scenario("cpu", "tentacle", (Param("seconds", 10, 600, 120), Param("workers", 1, 2, 1)),
             "burns every core for N seconds"),
    Scenario("memory", "tentacle", (Param("seconds", 10, 600, 120), Param("mb", 64, 700, 400)),
             "holds N MB of memory, then frees it; the Droplets have 1 GB"),
    Scenario("disk", "tentacle", (Param("seconds", 10, 600, 120), Param("mb", 128, 4096, 1024)),
             "writes an N MB file with fsync, keeps it, then deletes it"),
    Scenario("network", "tentacle", (Param("seconds", 10, 300, 60), Param("mbps", 1, 200, 50, float)),
             "sends N megabits a second to the peer tentacle", "peer"),
    Scenario("logs", "tentacle", (Param("seconds", 10, 600, 60), Param("rate", 1, 500, 50),
                                  Param("error_pct", 0, 100, 10, float)),
             "writes N JSON log lines a second, some of them errors"),
    Scenario("chain", "tentacle", (Param("count", 1, 200, 20), Param("latency_ms", 0, 5000, 250),
                                   Param("error_pct", 0, 100, 0, float)),
             "sends N traced requests through the tentacle, its peer, the database and the Function"),
    Scenario("pg", "tentacle", (Param("seconds", 10, 600, 60), Param("clients", 1, 8, 4)),
             "runs inserts and reads against the managed Postgres", "pg"),
    Scenario("fn", "head", (Param("seconds", 10, 300, 60), Param("rps", 1, 20, 5)),
             "calls the Function N times a second from the head", "fn"),
    Scenario("lb", "head", (Param("seconds", 10, 300, 60), Param("rps", 1, 50, 20)),
             "calls the load balancer's /health N times a second from the head", "lb"),
)}


def clamp(scenario: Scenario, params: dict | None) -> tuple[dict, dict]:
    """Validated parameters clamped to the public caps, and what was clamped ({name: {asked, used}})."""
    params = dict(params or {})
    known = {p.name for p in scenario.params}
    unknown = sorted(set(params) - known)
    if unknown:
        raise ApiError(400, "unknown_parameter", f"{scenario.name} takes {sorted(known)}, not {unknown}")
    clean, clamped = {}, {}
    for p in scenario.params:
        raw = params.get(p.name, p.default)
        try:
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError
        except (TypeError, ValueError):
            raise ApiError(400, "bad_parameter", f"{p.name} must be a number, got {raw!r}") from None
        used = min(max(value, p.low), p.high)
        used = int(round(used)) if p.kind is int else round(used, 3)
        if used != value:
            clamped[p.name] = {"asked": raw, "used": used}
        clean[p.name] = used
    return clean, clamped


@dataclass
class HeadRun:
    id: str
    name: str
    params: dict
    started_at: datetime
    status: str = "running"
    ended_at: datetime | None = None
    result: dict = field(default_factory=dict)
    error: str | None = None
    task: asyncio.Task | None = None

    def view(self, now: datetime) -> dict:
        end = self.ended_at or now
        return {"id": self.id, "name": self.name, "status": self.status, "params": self.params,
                "started_at": iso(self.started_at, millis=True), "ended_at": iso(self.ended_at, millis=True),
                "elapsed_s": round((end - self.started_at).total_seconds(), 3), "result": self.result,
                "error": self.error, "target": "head"}


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 1)


class ScenarioService:
    def __init__(self, deps: Any):
        self.deps = deps
        self.fleet = deps.settings.fleet
        self.head_runs: OrderedDict[str, HeadRun] = OrderedDict()
        self.load_http = httpx.AsyncClient(transport=deps.tentacle_transport, timeout=httpx.Timeout(10.0, connect=5.0),
                                           limits=httpx.Limits(max_connections=HEAD_CONCURRENCY),
                                           headers={"User-Agent": f"porthole/{deps.settings.version}"})
        self.sem = asyncio.Semaphore(HEAD_CONCURRENCY)

    def catalog(self) -> list[dict]:
        return [s.view() for s in CATALOG.values()]

    def caps(self) -> dict:
        return {"scenarios": {s.name: {p.name: [p.low, p.high] for p in s.params} for s in CATALOG.values()}}

    def head_view(self) -> list[dict]:
        now = self.deps.clock.now()
        return [r.view(now) for r in reversed(self.head_runs.values())]

    def _scenario(self, name: str) -> Scenario:
        if name not in CATALOG:
            raise ApiError(400, "unknown_scenario", f"scenario must be one of {list(CATALOG)}")
        return CATALOG[name]

    async def start(self, target: str, name: str, params: dict | None, actor: str = "captain") -> dict:
        scenario = self._scenario(name)
        clean, clamped = clamp(scenario, params)
        if scenario.runs_on == "head":
            view = self._start_head(target, scenario, clean)
        else:
            view = await self._start_tentacle(target, scenario, clean)
        self.deps.hub.publish("scenario", {"target": view["target"], "run": view, "event": "started"})
        self.deps.log.info(f"scenario {name} started on {view['target']}", actor=actor, run_id=view["id"],
                           params=clean)
        return {**view, "clamped": clamped}

    async def _start_tentacle(self, target: str, scenario: Scenario, clean: dict) -> dict:
        spec = self.fleet.tentacle(target)
        if spec is None:
            raise ApiError(400, "unknown_target", f"{scenario.name} runs on a tentacle; target must be one of "
                                                  f"{[t.name for t in self.fleet.tentacles]}")
        if scenario.requires == "peer" and not spec.peer:
            raise ApiError(400, "no_peer", f"{spec.display} has no peer tentacle, so network cannot run there")
        if scenario.requires == "pg" and "database" not in self.fleet.sea:
            raise ApiError(400, "no_database", "no managed Postgres in the fleet description")
        client = self.deps.poller.clients[spec.name]
        with caller("scenarios.start"):
            try:
                listing = await client.scenarios()
                busy = next((r for r in listing.get("running") or [] if r.get("name") == scenario.name), None)
                if busy:
                    raise ApiError(409, "already_running", f"{scenario.name} is already running on {spec.display} "
                                                           f"({busy.get('id')})", {"run_id": busy.get("id")})
                view = await client.start(scenario.name, clean)
            except TentacleError as e:
                raise self._tentacle_error(spec.display, e) from None
        await self.deps.poller.refresh(spec.name)
        return {**view, "target": spec.name}

    @staticmethod
    def _tentacle_error(display: str, e: TentacleError) -> ApiError:
        if e.status in (400, 409, 429):
            code = {400: "tentacle_refused", 409: "tentacle_refused", 429: "tentacle_busy"}[e.status]
            return ApiError(e.status, code, f"{display}: {e.message}", e.detail)
        if e.status == 401:
            return ApiError(502, "tentacle_auth", f"{display} rejected the head's TENTACLE_KEY")
        if e.status == 503:
            return ApiError(502, "tentacle_not_configured", f"{display} has no TENTACLE_KEY configured")
        return ApiError(502, "tentacle_error", e.message, e.detail)

    def _start_head(self, target: str, scenario: Scenario, clean: dict) -> dict:
        if target != "head":
            raise ApiError(400, "unknown_target", f"{scenario.name} runs on the head; use target head")
        url = self._head_url(scenario.name)
        busy = next((r for r in self.head_runs.values() if r.name == scenario.name and r.status == "running"), None)
        if busy:
            raise ApiError(409, "already_running", f"{scenario.name} is already running on the head ({busy.id})",
                           {"run_id": busy.id})
        run = HeadRun(id=f"{scenario.name}-{new_id('x', 4)[2:]}", name=scenario.name, params=clean,
                      started_at=self.deps.clock.now())
        self.head_runs[run.id] = run
        while len(self.head_runs) > HEAD_RUNS_KEPT:
            self.head_runs.popitem(last=False)
        run.task = asyncio.get_running_loop().create_task(self._drive(run, url), context=_fresh_context())
        return run.view(self.deps.clock.now())

    def _head_url(self, name: str) -> str:
        if name == "fn":
            fn = self.fleet.sea.get("functions")
            if not fn or not fn.extra.get("url"):
                raise ApiError(400, "no_function", "no Function URL in the fleet description")
            return str(fn.extra["url"])
        lb = self.fleet.sea.get("load_balancer")
        if not lb or not lb.extra.get("ip"):
            raise ApiError(400, "no_load_balancer", "no load balancer IP in the fleet description")
        return f"http://{lb.extra['ip']}/health"

    async def _drive(self, run: HeadRun, url: str) -> None:
        kind = "function" if run.name == "fn" else "lb"
        entity = (self.fleet.sea.get("functions" if run.name == "fn" else "load_balancer")).name
        clock, seconds, rps = self.deps.clock, run.params["seconds"], run.params["rps"]
        latencies: list[float] = []
        counts = {"requests": 0, "ok": 0, "errors": 0}

        async def one(record: bool, record_errors: bool) -> None:
            async with self.sem:
                t0 = time.monotonic()
                try:
                    resp = await traced_request(self.load_http, self.deps.trace, target=kind, method="GET", url=url,
                                                entity=entity, record=record, record_errors=record_errors)
                    good = resp.status_code < 400
                except httpx.HTTPError:
                    good = False
                latencies.append((time.monotonic() - t0) * 1000)
                counts["requests"] += 1
                counts["ok" if good else "errors"] += 1

        tracer = self.deps.telemetry.tracer
        with tracer.start_as_current_span(f"scenario.{run.name}", attributes={"scenario.id": run.id,
                                                                            "scenario.name": run.name}):
            try:
                with caller(f"scenarios.{run.name}"):
                    for second in range(int(seconds)):
                        tick = clock.monotonic()
                        # one request in ten seconds goes to the API trace, and at most one failure a second
                        await asyncio.gather(*(one(second % 10 == 0 and i == 0, i == 0) for i in range(int(rps))))
                        run.result = self._result(counts, latencies)
                        await clock.sleep(max(0.0, 1.0 - (clock.monotonic() - tick)))
                run.status = "finished"
            except asyncio.CancelledError:
                run.status = "stopped"
            except Exception as e:  # report, never crash the head
                run.status, run.error = "failed", f"{type(e).__name__}: {e}"
            finally:
                run.ended_at = clock.now()
                run.result = self._result(counts, latencies)
                self.deps.hub.publish("scenario", {"target": "head", "run": run.view(run.ended_at),
                                                   "event": run.status})

    @staticmethod
    def _result(counts: dict, latencies: list[float]) -> dict:
        return {**counts, "p50_ms": _percentile(latencies, 0.5), "p95_ms": _percentile(latencies, 0.95)}

    async def stop(self, target: str, run_id: str, actor: str = "captain") -> dict:
        if target == "head":
            run = self.head_runs.get(run_id)
            if run is None:
                raise ApiError(404, "no_run", f"no head run {run_id}")
            if run.task and not run.task.done():
                run.task.cancel()
                await asyncio.gather(run.task, return_exceptions=True)
            view = run.view(self.deps.clock.now())
        else:
            spec = self.fleet.tentacle(target)
            if spec is None:
                raise ApiError(400, "unknown_target", f"no tentacle {target}")
            with caller("scenarios.stop"):
                try:
                    view = {**await self.deps.poller.clients[spec.name].stop(run_id), "target": spec.name}
                except TentacleError as e:
                    if e.status == 404:
                        raise ApiError(404, "no_run", f"{spec.display} has no run {run_id}") from None
                    raise self._tentacle_error(spec.display, e) from None
            await self.deps.poller.refresh(spec.name)
            self.deps.hub.publish("scenario", {"target": spec.name, "run": view, "event": "stopped"})
        self.deps.log.info(f"scenario {run_id} stopped on {target}", actor=actor)
        return view

    async def close(self) -> None:
        for run in self.head_runs.values():
            if run.task and not run.task.done():
                run.task.cancel()
        await asyncio.gather(*(r.task for r in self.head_runs.values() if r.task), return_exceptions=True)
        await self.load_http.aclose()


def _fresh_context() -> Any:
    """A context without the request's span, so a head run gets its own trace."""
    import contextvars
    ctx = contextvars.copy_context()
    ctx.run(otel_context.attach, otel_context.Context())
    return ctx
