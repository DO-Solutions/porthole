"""The voyage engine: one-click sequences of scenarios and Insights checks, each step timed and published.

Only one voyage sails at a time. A failed, timed-out or aborted voyage still runs its cleanup and stops every
scenario it started. All waiting goes through the injected clock, so tests sail a twenty-minute voyage in
milliseconds. The seven voyages themselves are in voyages_catalog.py."""
from __future__ import annotations

import asyncio
import contextvars
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from opentelemetry import context as otel_context

from insights_harness import InsightsError
from porthole.apitrace import caller, collect_calls, current_trace_id
from porthole.cache import BudgetExhausted
from porthole.clock import iso, new_id
from porthole.security import ApiError
from porthole.tentacles import TentacleError

RUNS_KEPT = 50


class VoyageFailed(Exception):
    pass


class StepTimeout(Exception):
    pass


class StepSkipped(Exception):
    pass


def with_note(error: str, note: str) -> str:
    """A failed step's text: the error, then the note the step had written when it adds something."""
    return f"{error} ({note})" if note and note != error else error


@dataclass(frozen=True)
class StepSpec:
    name: str
    title: str
    timeout_s: float = 120.0
    optional: bool = False


@dataclass(frozen=True)
class VoyageSpec:
    name: str
    title: str
    feature: str
    story: str
    steps: tuple[StepSpec, ...]
    run: Callable[[Any], Awaitable[dict]]
    params: dict = field(default_factory=dict)
    summary_keys: tuple[str, ...] = ()

    def view(self) -> dict:
        return {"name": self.name, "title": self.title, "feature": self.feature, "story": self.story,
                "params": self.params, "steps": [{"name": s.name, "title": s.title, "timeout_s": s.timeout_s,
                                                  "optional": s.optional} for s in self.steps]}


@dataclass
class StepRecord:
    """One planned step of a run. `text` is the step's latest note while it runs. When the step fails or times
    out, `text` becomes the error, then the note it had written in parentheses when there was one and it says
    something else: "no tentacle accepted memory (started nothing; refused: ...)". A skipped step keeps the
    reason it was skipped."""
    name: str
    title: str
    timeout_s: float
    optional: bool
    status: str = "planned"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    text: str = ""
    data: dict = field(default_factory=dict)
    artifacts: list = field(default_factory=list)
    started_mono: float = 0.0

    def view(self) -> dict:
        return {"name": self.name, "title": self.title, "status": self.status, "started_at": iso(self.started_at),
                "ended_at": iso(self.ended_at), "text": self.text, "data": self.data, "artifacts": self.artifacts,
                "timeout_s": self.timeout_s, "optional": self.optional,
                "duration_s": round((self.ended_at - self.started_at).total_seconds(), 1)
                if self.started_at and self.ended_at else None}


@dataclass
class VoyageRun:
    id: str
    voyage: str
    title: str
    params: dict
    started_at: datetime
    steps: list[StepRecord]
    actor: str
    summary: dict = field(default_factory=dict)
    status: str = "sailing"
    ended_at: datetime | None = None
    error: str | None = None
    trace_id: str | None = None
    scenarios_started: list = field(default_factory=list)
    task: asyncio.Task | None = None

    def step(self, name: str) -> StepRecord:
        return next(s for s in self.steps if s.name == name)

    def view(self) -> dict:
        return {"id": self.id, "voyage": self.voyage, "title": self.title, "params": self.params,
                "status": self.status, "started_at": iso(self.started_at), "ended_at": iso(self.ended_at),
                "actor": self.actor, "error": self.error, "trace_id": self.trace_id,
                "steps": [s.view() for s in self.steps], "summary": self.summary,
                "scenarios_started": self.scenarios_started}

    def brief(self) -> dict:
        current = next((s.name for s in self.steps if s.status == "running"), None)
        return {"id": self.id, "voyage": self.voyage, "title": self.title, "status": self.status,
                "started_at": iso(self.started_at), "ended_at": iso(self.ended_at), "current_step": current,
                "params": self.params}


class Step:
    """What a voyage sees inside `async with ctx.step(name) as s`."""

    def __init__(self, ctx: VoyageContext, rec: StepRecord):
        self.ctx, self.rec = ctx, rec

    @property
    def data(self) -> dict:
        return self.rec.data

    def note(self, text: str) -> None:
        self.rec.text = text
        self.ctx.engine.publish(self.ctx.run, self.rec)

    def artifact(self, kind: str, ref: str, **extra: Any) -> None:
        self.rec.artifacts.append({"kind": kind, "ref": ref, **extra})

    def skip(self, reason: str) -> None:
        raise StepSkipped(reason)


class VoyageContext:
    def __init__(self, engine: VoyageEngine, run: VoyageRun):
        self.engine, self.run = engine, run
        self.deps = engine.deps
        self.clock = engine.deps.clock
        self.fleet = engine.deps.settings.fleet
        self.current: StepRecord | None = None

    @property
    def params(self) -> dict:
        return self.run.params

    def now(self) -> datetime:
        return self.clock.now()

    def since(self, t: datetime | None) -> float | None:
        return None if t is None else round((self.now() - t).total_seconds(), 1)

    @asynccontextmanager
    async def step(self, name: str) -> AsyncIterator[Step]:
        rec = self.run.step(name)
        rec.status, rec.started_at, rec.started_mono = "running", self.now(), self.clock.monotonic()
        self.current = rec
        self.engine.publish(self.run, rec)
        try:
            yield Step(self, rec)
        except StepSkipped as e:
            rec.status, rec.text = "skipped", str(e)
        except StepTimeout as e:
            rec.status, rec.text = "timed_out", with_note(str(e), rec.text)
            if not rec.optional:
                self._close(rec)
                raise VoyageFailed(f"{name} timed out after {rec.timeout_s:.0f} s") from None
        except (TentacleError, InsightsError, ApiError, BudgetExhausted, VoyageFailed) as e:
            rec.status = "failed"
            rec.text = with_note(getattr(e, "message", None) or str(e), rec.text)
            self._close(rec)
            raise VoyageFailed(f"{name}: {rec.text}") from None
        else:
            rec.status = "done"
        self._close(rec)

    def _close(self, rec: StepRecord) -> None:
        rec.ended_at = self.now()
        self.current = None
        self.engine.publish(self.run, rec)

    async def wait_for(self, check: Callable[[], Awaitable[Any]], every_s: float,
                       waiting: Callable[[Any], str] | None = None) -> Any:
        """Poll check() until it returns something truthy or the current step's timeout passes."""
        rec = self.current
        assert rec is not None, "wait_for runs inside a step"
        while True:
            value = await check()
            if value:
                return value
            if waiting is not None:
                rec.text = waiting(value)
                self.engine.publish(self.run, rec)
            if self.clock.monotonic() - rec.started_mono + every_s > rec.timeout_s:
                raise StepTimeout(f"still waiting after {rec.timeout_s:.0f} s")
            await self.clock.sleep(every_s)

    async def insights(self, method: str, *args: Any, **kwargs: Any) -> Any:
        body, _, _ = await self.deps.panels.call(f"voyage.{self.run.voyage}", method, *args, **kwargs)
        return body

    async def start(self, target: str, scenario: str, params: dict) -> dict:
        with collect_calls() as calls, caller(f"voyage.{self.run.voyage}"):
            view = await self.deps.scenarios.start(target, scenario, params, actor=f"voyage {self.run.id}")
        self.run.scenarios_started.append({"target": view["target"], "run_id": view["id"], "scenario": scenario})
        if self.current is not None:
            self.current.artifacts.append({"kind": "run", "ref": view["id"], "target": view["target"]})
            if calls:
                self.current.artifacts.append({"kind": "api_call", "ref": calls[-1]})
        return view

    async def stop(self, target: str, run_id: str) -> dict:
        with caller(f"voyage.{self.run.voyage}"):
            return await self.deps.scenarios.stop(target, run_id, actor=f"voyage {self.run.id}")

    async def run_view(self, target: str, run_id: str) -> dict | None:
        if target == "head":
            run = self.deps.scenarios.head_runs.get(run_id)
            return run.view(self.now()) if run else None
        listing = await self.deps.poller.clients[target].scenarios()
        return next((r for r in (listing.get("running") or []) + (listing.get("finished") or [])
                     if r.get("id") == run_id), None)

    async def cleanup(self) -> None:
        """Stop every scenario the voyage started. A cancellation that lands here (a second abort, or shutdown)
        is noted and the loop goes on, so one interrupted stop never leaves the others running."""
        for started in self.run.scenarios_started:
            try:
                view = await self.run_view(started["target"], started["run_id"])
                if view and view.get("status") == "running":
                    await self.stop(started["target"], started["run_id"])
            except asyncio.CancelledError:
                self.deps.log.warn(f"voyage cleanup interrupted while stopping {started['run_id']}; going on")
            except Exception as e:  # cleanup must reach every scenario
                self.deps.log.warn(f"voyage cleanup could not stop {started['run_id']}: {e}")


class VoyageEngine:
    def __init__(self, deps: Any, catalog: dict[str, VoyageSpec]):
        self.deps, self.catalog = deps, catalog
        self.runs: OrderedDict[str, VoyageRun] = OrderedDict()
        self.active: VoyageRun | None = None

    def catalog_view(self) -> list[dict]:
        return [spec.view() for spec in self.catalog.values()]

    def list_runs(self) -> list[dict]:
        return [r.brief() for r in reversed(self.runs.values())]

    def get(self, run_id: str) -> VoyageRun:
        run = self.runs.get(run_id)
        if run is None:
            raise ApiError(404, "no_voyage", f"no voyage run {run_id} (the last {RUNS_KEPT} are kept)")
        return run

    def publish(self, run: VoyageRun, rec: StepRecord | None = None) -> None:
        self.deps.hub.publish("voyage", {"run_id": run.id, "voyage": run.voyage,
                                         "step": rec.name if rec else None,
                                         "status": rec.status if rec else run.status,
                                         "t": iso(self.deps.clock.now()), "text": rec.text if rec else run.error,
                                         "data": rec.data if rec else run.summary})

    async def start(self, name: str, params: dict | None, actor: str = "captain") -> VoyageRun:
        spec = self.catalog.get(name)
        if spec is None:
            raise ApiError(400, "unknown_voyage", f"voyage must be one of {list(self.catalog)}")
        if self.active is not None:
            raise ApiError(409, "voyage_sailing", f"voyage {self.active.id} is sailing; one at a time",
                           {"active_run_id": self.active.id})
        clean = self._params(spec, params or {})
        run = VoyageRun(id=new_id("v"), voyage=name, title=spec.title, params=clean, started_at=self.deps.clock.now(),
                        actor=actor, summary=dict.fromkeys(spec.summary_keys),
                        steps=[StepRecord(s.name, s.title, s.timeout_s, s.optional) for s in spec.steps])
        self.runs[run.id] = run
        while len(self.runs) > RUNS_KEPT:
            self.runs.popitem(last=False)
        self.active = run
        ctx = contextvars.copy_context()
        ctx.run(otel_context.attach, otel_context.Context())  # each voyage gets its own trace
        run.task = asyncio.get_running_loop().create_task(self._sail(run, spec), name=run.id, context=ctx)
        self.deps.log.info(f"voyage {name} set sail", run_id=run.id, actor=actor, params=clean)
        self.publish(run)
        return run

    def _params(self, spec: VoyageSpec, params: dict) -> dict:
        unknown = sorted(set(params) - set(spec.params))
        if unknown:
            raise ApiError(400, "unknown_parameter", f"{spec.name} takes {sorted(spec.params)}, not {unknown}")
        clean = {}
        for key, schema in spec.params.items():
            value = params.get(key, schema.get("default"))
            if schema.get("type") == "tentacle" and value not in (None, ""):
                t = self.deps.settings.fleet.tentacle(str(value or ""))
                if t is None:
                    raise ApiError(400, "unknown_target", f"{key} must be a tentacle of this fleet")
                value = t.name
            clean[key] = value
        return clean

    async def _sail(self, run: VoyageRun, spec: VoyageSpec) -> None:
        ctx = VoyageContext(self, run)
        tracer = self.deps.telemetry.tracer
        with tracer.start_as_current_span(f"voyage.{run.voyage}", attributes={"voyage.id": run.id}):
            run.trace_id = current_trace_id()
            try:
                run.summary.update(await spec.run(ctx) or {})
                run.status = "done"
            except VoyageFailed as e:
                run.status, run.error = "failed", str(e)
            except asyncio.CancelledError:
                run.status, run.error = "aborted", run.error or "aborted by the captain"
            except Exception as e:  # a bug in a voyage must not take the head down
                run.status, run.error = "failed", f"{type(e).__name__}: {e}"
                self.deps.log.error(f"voyage {run.voyage} crashed: {run.error}", run_id=run.id)
            finally:
                # From here on the run is ending: abort() and close() no longer cancel it, and a cancellation
                # that still arrives (one was already in flight) must not skip the bookkeeping below, or the
                # engine would stay locked on a run that never ends.
                for rec in run.steps:
                    if rec.status == "running":
                        rec.status, rec.ended_at = ("failed" if run.status != "aborted" else "skipped"), ctx.now()
                    if rec.status == "planned":
                        rec.status, rec.text = "skipped", rec.text or "not reached"
                try:
                    await ctx.cleanup()
                except asyncio.CancelledError:
                    self.deps.log.warn(f"voyage {run.voyage} cleanup was cancelled", run_id=run.id)
                run.ended_at = ctx.now()
                self.active = None
                self.deps.log.info(f"voyage {run.voyage} ended: {run.status}", run_id=run.id)
                self.publish(run)

    async def abort(self, run_id: str, actor: str = "captain") -> VoyageRun:
        """Cancel a sailing voyage and wait for its cleanup. A run that is already ending (a second abort while
        the first one's cleanup is still stopping scenarios) is only waited for, never cancelled again."""
        run = self.get(run_id)
        if run.task is not None and not run.task.done():
            if run.status == "sailing":
                run.error = f"aborted by {actor}"
                run.task.cancel()
            await asyncio.gather(run.task, return_exceptions=True)
        return run

    async def close(self) -> None:
        run = self.active
        if run is not None and run.task is not None and not run.task.done():
            if run.status == "sailing":
                run.task.cancel()
            await asyncio.gather(run.task, return_exceptions=True)
