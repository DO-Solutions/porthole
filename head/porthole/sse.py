"""Server-Sent Events for the head: one hub, a 500-event replay ring, one bounded queue per subscriber.

publish() may be called from worker threads (the harness runs in threads); delivery to queues always happens
on the event loop. Slow consumers lose their oldest events instead of blocking anyone."""
from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

HEARTBEAT_S = 15.0


@dataclass(frozen=True)
class Event:
    id: int
    event: str
    data: Any


def format_event(event: str, data: Any, event_id: int | str | None = None) -> str:
    """id:, event: and data: lines; data is JSON on one line."""
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, separators=(",", ":"), default=str))
    return "\n".join(lines) + "\n\n"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Hub:
    def __init__(self, ring_size: int = 500, queue_size: int = 200, now_iso: Callable[[], str] | None = None):
        self.ring: deque[Event] = deque(maxlen=ring_size)
        self.queue_size = queue_size
        self.now_iso = now_iso or _now_iso
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._next_id = 1
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self.closed = False
        self.dropped = 0

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop, self._loop_thread = loop, threading.get_ident()
        self.closed = False

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def publish(self, event: str, data: Any) -> int:
        with self._lock:
            ev = Event(self._next_id, event, data)
            self._next_id += 1
            self.ring.append(ev)
        if self._loop is None or threading.get_ident() == self._loop_thread:
            self._deliver(ev)
        else:
            try:
                self._loop.call_soon_threadsafe(self._deliver, ev)
            except RuntimeError:  # loop closed during shutdown
                pass
        return ev.id

    def _deliver(self, ev: Event | None) -> None:
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(ev)

    def since(self, last_id: int | None) -> list[Event]:
        with self._lock:
            items = list(self.ring)
        if last_id is None:
            return []
        return [e for e in items if e.id > last_id]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def close(self) -> None:
        """End every open stream (used at shutdown so uvicorn need not wait for browsers)."""
        self.closed = True
        for q in list(self._subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                q.get_nowait()
                q.put_nowait(None)

    async def stream(self, last_event_id: str | None = None, heartbeat_s: float = HEARTBEAT_S,
                     is_disconnected: Callable[[], Awaitable[bool]] | None = None) -> AsyncIterator[str]:
        """Replay what the client missed, then live events and a heartbeat every heartbeat_s seconds."""
        q = self.subscribe()
        try:
            yield "retry: 3000\n\n"
            try:
                last = int(last_event_id) if last_event_id else None
            except ValueError:
                last = None
            for ev in self.since(last):
                yield format_event(ev.event, ev.data, ev.id)
            while not self.closed:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=heartbeat_s)
                except TimeoutError:
                    if is_disconnected is not None and await is_disconnected():
                        return
                    yield ": heartbeat\n\n" + format_event("heartbeat", {"t": self.now_iso()})
                    continue
                if ev is None:
                    return
                yield format_event(ev.event, ev.data, ev.id)
        finally:
            self.unsubscribe(q)


SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
