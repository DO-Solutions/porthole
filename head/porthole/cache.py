"""The panel cache and the upstream budget that protect the shared team account's Insights rate limit.

Every Insights call takes one unit from the budget (200 a minute by default). When it runs out, panels serve
their last good value marked stale with a retry_in; concurrent misses for one key share a single fetch."""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


class BudgetExhausted(Exception):
    def __init__(self, retry_in: float):
        super().__init__(f"upstream budget exhausted; retry in {retry_in:.0f} s")
        self.retry_in = retry_in


class Budget:
    """Sliding one-minute window of upstream calls; take() is called from the harness worker threads."""

    def __init__(self, per_minute: int, monotonic: Callable[[], float]):
        self.per_minute = per_minute
        self.monotonic = monotonic
        self.calls: deque[float] = deque()
        self._lock = threading.Lock()

    def _trim(self, now: float) -> None:
        while self.calls and now - self.calls[0] >= 60.0:
            self.calls.popleft()

    def take(self) -> tuple[bool, float]:
        with self._lock:
            now = self.monotonic()
            self._trim(now)
            if len(self.calls) >= self.per_minute:
                return False, max(1.0, 60.0 - (now - self.calls[0]))
            self.calls.append(now)
            return True, 0.0

    def used(self) -> int:
        with self._lock:
            self._trim(self.monotonic())
            return len(self.calls)

    def remaining(self) -> int:
        return max(0, self.per_minute - self.used())


@dataclass
class CacheResult[T]:
    value: T
    cached: bool
    stale: bool
    retry_in: float | None
    fetched_at: str


@dataclass
class _Entry:
    value: Any
    at: float
    fetched_at: str


class PanelCache:
    def __init__(self, monotonic: Callable[[], float], now_iso: Callable[[], str], max_entries: int = 500):
        self.monotonic, self.now_iso, self.max_entries = monotonic, now_iso, max_entries
        self._entries: dict[str, _Entry] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self.hits = self.misses = self.stale_served = 0

    def peek(self, key: str) -> Any:
        entry = self._entries.get(key)
        return entry.value if entry else None

    def invalidate(self, prefix: str = "") -> None:
        for key in [k for k in self._entries if k.startswith(prefix)]:
            del self._entries[key]

    async def get[T](self, key: str, fetch: Callable[[], Awaitable[T]], ttl: float) -> CacheResult[T]:
        entry = self._entries.get(key)
        now = self.monotonic()
        if entry is not None and now - entry.at < ttl:
            self.hits += 1
            return CacheResult(entry.value, True, False, None, entry.fetched_at)
        pending = self._inflight.get(key)
        if pending is not None:
            result: CacheResult[T] = await asyncio.shield(pending)
            return CacheResult(result.value, True, result.stale, result.retry_in, result.fetched_at)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            self.misses += 1
            try:
                value = await fetch()
            except BudgetExhausted as e:
                if entry is None:
                    raise
                self.stale_served += 1
                result = CacheResult(entry.value, True, True, e.retry_in, entry.fetched_at)
            else:
                fetched_at = self.now_iso()
                self._store(key, _Entry(value, self.monotonic(), fetched_at))
                result = CacheResult(value, False, False, None, fetched_at)
            future.set_result(result)
            return result
        except BaseException as e:
            future.set_exception(e)
            future.exception()  # mark retrieved so an unawaited future does not warn
            raise
        finally:
            self._inflight.pop(key, None)

    def _store(self, key: str, entry: _Entry) -> None:
        self._entries[key] = entry
        if len(self._entries) > self.max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k].at)
            del self._entries[oldest]
