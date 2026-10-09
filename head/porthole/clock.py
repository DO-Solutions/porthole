"""Time and id helpers shared by every module, kept apart from deps.py to avoid import cycles.

The voyage engine, caches, rate limiters and fakes take a Clock, so tests can run minutes of demo time in
milliseconds with a fake clock while production uses the system clock."""
from __future__ import annotations

import asyncio
import secrets
import time
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """time.monotonic, datetime.now(timezone.utc) and asyncio.sleep."""

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


def iso(dt: datetime | None, millis: bool = False) -> str | None:
    """ISO 8601 UTC with a Z suffix; None stays None."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    spec = "milliseconds" if millis else "seconds"
    return dt.astimezone(timezone.utc).isoformat(timespec=spec).replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    """Parse the ISO strings the tentacle and Insights return (Z or +00:00 offsets)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hhmm(dt: datetime | None) -> str:
    """'14:01Z' for banners and verdicts."""
    if dt is None:
        return "?"
    return dt.astimezone(timezone.utc).strftime("%H:%MZ")


def new_id(prefix: str, nbytes: int = 3) -> str:
    """Short random hex id with a prefix, e.g. c-7f3a1b."""
    return f"{prefix}-{secrets.token_hex(nbytes)}"
