"""Shared test fixtures: the fixture fleet, a fake clock, and an app wired to the fake Insights and tentacles.

Everything runs offline and in process. The fake clock wakes sleepers in time order, so a voyage that takes
twenty minutes of demo time finishes in a fraction of a second."""
from __future__ import annotations

import asyncio
import heapq
import io
import itertools
import json
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

HEAD = Path(__file__).resolve().parents[1]
REPO = HEAD.parent
for _p in (HEAD, HEAD / "dev", REPO / "harness"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fake_insights import FakeInsights  # noqa: E402
from fake_tentacles import FakeFleet  # noqa: E402
from porthole.config import Settings  # noqa: E402
from porthole.main import create_app  # noqa: E402

FIXTURES = HEAD / "tests" / "fixtures"
TOKEN = "porthole-test-token-" + "f" * 44
CAPTAIN = "captain-key-for-tests-0000000000"
TENTACLE_KEY = "tentacle-key-for-tests-0000"
HOOK_BEARER = "hook-bearer-for-tests-0000"
HOOK_SECRET = "hook-secret-for-tests-0000"
SECRETS = (TOKEN, CAPTAIN, TENTACLE_KEY, HOOK_BEARER, HOOK_SECRET)


def fleet_text() -> str:
    return (FIXTURES / "fleet.json").read_text()


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def base_env(**overrides: str | None) -> dict[str, str]:
    env: dict[str, str | None] = {
        "DIGITALOCEAN_TOKEN": TOKEN, "PORTHOLE_CAPTAIN_KEY": CAPTAIN, "TENTACLE_KEY": TENTACLE_KEY,
        "PORTHOLE_FLEET_JSON": fleet_text(), "PORTHOLE_HOOK_BEARER": HOOK_BEARER, "PORTHOLE_HOOK_SECRET": HOOK_SECRET,
        "PORTHOLE_TRUST_PROXY": "0", "PORTHOLE_PUBLIC_URL": "https://porthole.example.test"}
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


class FakeClock:
    """monotonic(), now() and sleep() on a virtual timeline that tests advance explicitly."""

    def __init__(self, start: datetime | None = None):
        self.start_wall = start or datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)
        self.t = 0.0
        self._sleepers: list[tuple[float, int, asyncio.Future]] = []
        self._seq = itertools.count()

    def monotonic(self) -> float:
        return 1000.0 + self.t

    def now(self) -> datetime:
        return self.start_wall + timedelta(seconds=self.t)

    async def sleep(self, seconds: float) -> None:
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.t + max(0.0, seconds), next(self._seq), fut))
        await fut

    def next_wake(self) -> float | None:
        while self._sleepers and self._sleepers[0][2].done():
            heapq.heappop(self._sleepers)
        return self._sleepers[0][0] if self._sleepers else None

    def wake_next(self) -> bool:
        wake = self.next_wake()
        if wake is None:
            return False
        self.t = max(self.t, wake)
        while self._sleepers and self._sleepers[0][0] <= self.t:
            _, _, fut = heapq.heappop(self._sleepers)
            if not fut.done():
                fut.set_result(None)
        return True

    async def settle(self, busy: Callable[[], int] = lambda: 0) -> None:
        deadline = time.monotonic() + 15
        quiet = 0
        while quiet < 2:
            for _ in range(25):
                await asyncio.sleep(0)
            if busy() == 0:
                quiet += 1
                continue
            quiet = 0
            if time.monotonic() > deadline:
                raise AssertionError("worker threads did not settle")
            await asyncio.sleep(0.002)

    async def run_until(self, predicate: Callable[[], bool], limit_s: float = 4 * 3600,
                        busy: Callable[[], int] = lambda: 0,
                        between: Callable[[], Awaitable[None]] | None = None) -> bool:
        end = self.t + limit_s
        real_end = time.monotonic() + 60
        while True:
            await self.settle(busy)
            if between is not None:
                await between()
                await self.settle(busy)
            if predicate():
                return True
            if self.t >= end:
                return False
            if time.monotonic() > real_end:
                raise AssertionError(f"run_until gave up after 60 real seconds at t={self.t}")
            wake = self.next_wake()
            if wake is None or wake > end:
                self.t = end  # everything is settled and nobody wakes before the end: jump
            else:
                self.wake_next()

    async def advance(self, seconds: float, busy: Callable[[], int] = lambda: 0,
                      between: Callable[[], Awaitable[None]] | None = None) -> None:
        target = self.t + seconds
        await self.run_until(lambda: False, seconds, busy, between)
        self.t = max(self.t, target)
        await self.settle(busy)


class AppEnv:
    """The head on a fake clock with the fake Insights and the fake fleet behind it."""

    def __init__(self, env: dict[str, str] | None = None, *, droplet_logs: bool = False,
                 reject_metricless: bool = False, tentacle_key: str = TENTACLE_KEY, span_exporter: Any = None):
        self.clock = FakeClock()
        self.settings = Settings.from_env(base_env() if env is None else env)
        fleet = self.settings.fleet
        self.fleet = FakeFleet(fleet, key=tentacle_key, clock=self.clock)
        self.deliveries: list[dict] = []
        self.insights = FakeInsights(fleet, self.fleet, self.clock, droplet_logs=droplet_logs,
                                     reject_metricless=reject_metricless, public_url=self.settings.public_url,
                                     hook_bearer=self.settings.hook_bearer, hook_secret=self.settings.hook_secret,
                                     on_notify=self.deliveries.append)
        self.stdout = io.StringIO()
        self.app = create_app(self.settings, insights_transport=self.insights.transport(),
                              tentacle_transport=self.fleet.transport(), clock=self.clock, log_stream=self.stdout,
                              span_exporter=span_exporter)
        self.deps = self.app.state.deps
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://porthole.test")
        self._lifespan: Any = None

    async def __aenter__(self) -> AppEnv:
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        await self.settle()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.client.aclose()
        await self._lifespan.__aexit__(None, None, None)

    @staticmethod
    def captain() -> dict[str, str]:
        return {"X-Captain-Key": CAPTAIN}

    async def settle(self) -> None:
        await self.clock.settle(lambda: self.deps.inflight)

    async def pump(self) -> None:
        """Deliver the webhooks the fake Insights has queued, as Insights would."""
        self.insights.tick()
        while self.deliveries:
            d = self.deliveries.pop(0)
            await self.client.post("/hooks/insights", content=d["body"], headers=d["headers"])

    async def run_until(self, predicate: Callable[[], bool], limit_s: float = 4 * 3600) -> bool:
        return await self.clock.run_until(predicate, limit_s, busy=lambda: self.deps.inflight, between=self.pump)

    async def advance(self, seconds: float) -> None:
        await self.clock.advance(seconds, busy=lambda: self.deps.inflight, between=self.pump)

    def log_lines(self) -> list[dict]:
        return [json.loads(line) for line in self.stdout.getvalue().splitlines() if line.strip()]


@pytest.fixture
async def env() -> Any:
    async with AppEnv() as e:
        yield e


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
