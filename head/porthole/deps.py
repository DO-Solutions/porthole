"""The dependency container: every store, client and background task of one app, built once in create_app().

Routes reach it as request.app.state.deps. Tests build it with fake transports and a fake clock; production
uses the system clock and real HTTP."""
from __future__ import annotations

import asyncio
import contextvars
import functools
import sys
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

import httpx

from porthole.apitrace import ApiTrace, TracedInsights
from porthole.cache import Budget, PanelCache
from porthole.clock import Clock, SystemClock, iso
from porthole.config import Settings
from porthole.jsonlog import JsonLog
from porthole.security import Guard
from porthole.sse import Hub
from porthole.telemetry import Telemetry, instrument_client, setup_telemetry

T = TypeVar("T")
# Harness calls wait on the network, not the CPU. The loop's default executor gives a 1 vCPU instance five
# threads, which turns a six-chart page in both mode (12 calls) into three rounds of Insights latency; measured
# at 550 ms against 310 ms with sixteen threads on a 150 ms fake. The upstream budget still caps the call rate.
HARNESS_THREADS = 16
HEAD_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = HEAD_DIR.parent
STATIC_DIR = HEAD_DIR / "static"
WATCHER_DIR = REPO_DIR / "watcher"


class Deps:
    def __init__(self, settings: Settings, clock: Clock | None = None, *,
                 insights_transport: httpx.BaseTransport | None = None,
                 tentacle_transport: httpx.AsyncBaseTransport | None = None,
                 span_exporter: Any = None, log_stream: Any = None):
        self.settings = settings
        self.clock: Clock = clock or SystemClock()
        self.started = self.clock.monotonic()
        self.static_dir, self.watcher_dir = STATIC_DIR, WATCHER_DIR
        secrets = settings.secret_values()
        self.log = JsonLog(settings.service_name, secrets, stream=log_stream or sys.stdout, level=settings.log_level,
                           now=lambda: iso(self.clock.now(), millis=True) or "")
        region = settings.fleet.head.region if settings.fleet.head else (settings.fleet.regions or ("tor1",))[0]
        self.telemetry: Telemetry = setup_telemetry(settings.service_name, region, settings.otlp_endpoint,
                                                    settings.otlp_headers, secrets, span_exporter=span_exporter)
        self.log.otel_logger = self.telemetry.logger_provider.get_logger("porthole")
        self.hub = Hub(now_iso=lambda: iso(self.clock.now()) or "")
        self.trace = ApiTrace(self.hub, secrets=secrets, now=self.clock.now, monotonic=self.clock.monotonic)
        self.budget = Budget(settings.upstream_budget_per_min, self.clock.monotonic)
        self.cache = PanelCache(self.clock.monotonic, lambda: iso(self.clock.now()) or "")
        self.guard = Guard.build(settings.captain_key, settings.captain_configured, settings.trust_proxy,
                                 self.clock.monotonic, on_limited=self._limited)
        self.insights: TracedInsights | None = None
        if settings.token:
            self.insights = TracedInsights(settings.token, trace=self.trace, budget=self.budget,
                                           base_url=settings.insights_base_url, transport=insights_transport)
            instrument_client(self.insights._http, self.telemetry)
        self.tentacle_transport = tentacle_transport
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), transport=tentacle_transport,
                                      headers={"User-Agent": f"porthole/{settings.version}"})
        instrument_client(self.http, self.telemetry)
        self.inflight = 0
        self.executor = ThreadPoolExecutor(max_workers=HARNESS_THREADS, thread_name_prefix="harness")
        self._tasks: set[asyncio.Task] = set()
        self._starters: list[Callable[[], None]] = []
        self._stoppers: list[Callable[[], Coroutine[Any, Any, None]]] = []

    def _limited(self, kind: str, ip: str, retry: float) -> None:
        self.log.warn(f"rate limited ({kind})", **{"client.address": ip, "retry_after_s": retry})

    async def run_sync(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run a blocking call (the harness) in the harness thread pool; contextvars travel with it."""
        self.inflight += 1
        try:
            call = functools.partial(contextvars.copy_context().run, fn, *args, **kwargs)
            return await asyncio.get_running_loop().run_in_executor(self.executor, call)
        finally:
            self.inflight -= 1

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def on_start(self, fn: Callable[[], None]) -> None:
        self._starters.append(fn)

    def on_stop(self, fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
        self._stoppers.append(fn)

    async def start(self) -> None:
        self.hub.bind(asyncio.get_running_loop())
        for problem in self.settings.problems:
            self.log.warn(f"degraded: {problem}")
        self.log.info("porthole up", version=self.settings.version,
                      fleet_tentacles=len(self.settings.fleet.tentacles),
                      insights="configured" if self.insights else "missing",
                      otlp=self.telemetry.describe()["text"])
        for fn in self._starters:
            fn()

    async def stop(self) -> None:
        self.hub.close()
        for fn in self._stoppers:
            try:
                await fn()
            except Exception as e:  # shutdown must finish
                self.log.warn(f"shutdown step failed: {type(e).__name__}: {e}")
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for _ in range(200):  # let harness calls already in worker threads finish before closing the client
            if self.inflight <= 0:
                break
            await asyncio.sleep(0.01)
        await self.http.aclose()
        if self.insights is not None:
            self.insights.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.log.info("porthole down")
        self.telemetry.shutdown()


def build_services(deps: Deps) -> None:
    """Attach the services of later milestones. Imports are local so modules can import deps types freely."""
    from porthole.brain.deckhand import Deckhand
    from porthole.brain.harness_runtime import HarnessRuntimeBrain
    from porthole.brain.tools import build_tools
    from porthole.deeplinks import DeepLinks
    from porthole.hooks import DeliveryStore
    from porthole.panels import Panels
    from porthole.panels_alerts import AlertPanels
    from porthole.panels_logs import LogPanels
    from porthole.scenarios import ScenarioService
    from porthole.tentacles import FleetPoller
    from porthole.voyages import VoyageEngine
    from porthole.voyages_catalog import CATALOG

    deps.links = DeepLinks(deps.settings.fleet, deps.settings.deeplink_overrides, deps.settings.do_context)
    deps.panels = Panels(deps)
    deps.alert_panels = AlertPanels(deps.panels)
    deps.log_panels = LogPanels(deps.panels)
    deps.panels_caps = deps.panels.caps
    deps.poller = FleetPoller(deps)
    deps.on_start(lambda: deps.spawn(deps.poller.run(), "fleet-poller"))
    deps.on_start(lambda: warn_unknown_metrics(deps))
    deps.scenarios = ScenarioService(deps)
    deps.scenarios_caps = deps.scenarios.caps
    deps.hooks = DeliveryStore(deps.hub, deps.clock.now, secrets=deps.settings.secret_values())
    deps.voyages = VoyageEngine(deps, CATALOG)
    deps.on_stop(deps.voyages.close)
    deps.on_stop(deps.scenarios.close)
    deps.brain_tools = build_tools(deps)
    deps.brain = None
    if deps.settings.brain == "deckhand":
        deps.brain = Deckhand(deps, deps.brain_tools)
        deps.on_stop(deps.brain.close)
    elif deps.settings.brain == "harness-runtime":
        s = deps.settings
        deps.brain = HarnessRuntimeBrain(session=s.brain_session, gateway_url=s.gateway_mcp_url, token=s.brain_token)


def warn_unknown_metrics(deps: Deps) -> None:
    """One warning per probe or voyage metric that a configured region's committed catalog lacks (B-034)."""
    from porthole import metric_catalog, voyages_metrics

    names = [*deps.panels.probe_metrics.values(), *voyages_metrics.METRICS]
    catalog = metric_catalog.load(deps.watcher_dir)
    for name, regions in metric_catalog.missing(names, deps.settings.fleet, catalog).items():
        deps.log.warn(f"metric {name} is not in the committed catalog for {', '.join(regions)} (watcher/catalog): "
                      "Insights answers queries on it with no data and a rule on it never fires",
                      **{"metric.name": name, "regions": regions})
