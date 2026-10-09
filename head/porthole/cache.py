"""The panel cache and the upstream budget that protect the shared team account's Insights rate limit.

Every Insights call takes one unit from the budget (200 a minute by default). When it runs out, panels serve
their last good value marked stale with a retry_in; concurrent misses for one key share a single fetch."""
from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


class BudgetExhausted(Exception):
    def __init__(self, retry_in: float):
        super().__init__(f"upstream budget exhausted; retry in {retry_in:.0f} s")
        self.retry_in = retry_in


class _LeaderCancelled(Exception):
    """The caller that was fetching a key got cancelled; the callers waiting on it fetch for themselves."""


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
    weight: int = 0


# Measured on the fixture fleet: a 24 h range entry is 55 KB on the wire and about 390 KB resident for two series,
# 278 KB and 1.5 MB for eight. Resident memory is roughly six times the JSON size, so 4 MB of JSON is about 25 MB
# of cache on the 512 MiB instance, which holds every chart the pages draw with room to spare.
MAX_CACHE_BYTES = 4 * 2**20


class PanelCache:
    def __init__(self, monotonic: Callable[[], float], now_iso: Callable[[], str], max_entries: int = 500,
                 max_bytes: int = MAX_CACHE_BYTES):
        self.monotonic, self.now_iso, self.max_entries, self.max_bytes = monotonic, now_iso, max_entries, max_bytes
        self._entries: dict[str, _Entry] = {}  # insertion order is age order: _store always appends
        self._inflight: dict[str, asyncio.Future] = {}
        self.hits = self.misses = self.stale_served = 0
        self.bytes = 0

    @staticmethod
    def weight(value: Any) -> int:
        """The JSON size of a value, the cheap proxy for what it costs to keep."""
        try:
            return len(json.dumps(value, separators=(",", ":"), default=str))
        except (TypeError, ValueError):
            return 1024

    def peek(self, key: str) -> Any:
        entry = self._entries.get(key)
        return entry.value if entry else None

    def invalidate(self, prefix: str = "") -> None:
        for key in [k for k in self._entries if k.startswith(prefix)]:
            self.bytes -= self._entries.pop(key).weight

    async def get[T](self, key: str, fetch: Callable[[], Awaitable[T]], ttl: float) -> CacheResult[T]:
        entry = self._entries.get(key)
        now = self.monotonic()
        if entry is not None and now - entry.at < ttl:
            self.hits += 1
            return CacheResult(entry.value, True, False, None, entry.fetched_at)
        pending = self._inflight.get(key)
        if pending is not None:
            try:
                result: CacheResult[T] = await asyncio.shield(pending)
            except _LeaderCancelled:
                # The first caller was cancelled (its request or task went away), not us: fetch on our own.
                return await self.get(key, fetch, ttl)
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
            # Our own cancellation must not cancel the callers sharing this fetch; they retry for themselves.
            future.set_exception(_LeaderCancelled() if isinstance(e, asyncio.CancelledError) else e)
            future.exception()  # mark retrieved so an unawaited future does not warn
            raise
        finally:
            self._inflight.pop(key, None)

    def _store(self, key: str, entry: _Entry) -> None:
        old = self._entries.pop(key, None)
        if old is not None:
            self.bytes -= old.weight
        entry.weight = self.weight(entry.value)
        self._entries[key] = entry
        self.bytes += entry.weight
        while self._entries and (len(self._entries) > self.max_entries or self.bytes > self.max_bytes):
            oldest = next(iter(self._entries))  # a value larger than the whole budget evicts itself at once
            self.bytes -= self._entries.pop(oldest).weight
