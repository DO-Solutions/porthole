"""Structured logs for the head: one JSON object per line on stdout, mirrored to OpenTelemetry, last 300 kept.

The Logs page compares this ring ("Head (as emitted)") with what Insights returns for service.name=porthole.
A record that contains any configured secret is never written; a short notice takes its place."""
from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any, TextIO

from opentelemetry import context as otel_context
from opentelemetry import trace

SEVERITY = {"DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17}
WITHHELD = "log record withheld: it contained a configured secret"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonLog:
    def __init__(self, service_name: str = "porthole", secrets: Iterable[str] = (), stream: TextIO | None = None,
                 level: str = "INFO", ring_size: int = 300, now: Callable[[], str] | None = None):
        self.service_name = service_name
        self.stream = stream or sys.stdout
        self.level = SEVERITY.get(level, 9)
        self.ring: deque[dict] = deque(maxlen=ring_size)
        self.otel_logger: Any = None
        self._secrets = [s for s in secrets if s and len(s) >= 6]
        self._now = now or _now_iso
        self._lock = threading.Lock()

    def __call__(self, severity: str, body: str, **attrs: Any) -> dict | None:
        number = SEVERITY[severity]
        if number < self.level:
            return None
        record: dict[str, Any] = {"timestamp": self._now(), "severity_text": severity, "severity_number": number,
                                  "body": body, "service.name": self.service_name}
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            record["trace_id"] = format(ctx.trace_id, "032x")
            record["span_id"] = format(ctx.span_id, "016x")
        record.update({k: v for k, v in attrs.items() if v is not None})
        line = json.dumps(record, default=str)
        if any(s in line for s in self._secrets):
            record = {"timestamp": record["timestamp"], "severity_text": "WARN", "severity_number": 13,
                      "body": WITHHELD, "service.name": self.service_name,
                      "withheld.severity": severity, "withheld.keys": sorted(attrs)}
            line = json.dumps(record)
        with self._lock:
            print(line, file=self.stream, flush=True)
            self.ring.append(record)
        self._mirror(record)
        return record

    def _mirror(self, record: dict) -> None:
        if self.otel_logger is None:
            return
        try:
            from opentelemetry._logs import SeverityNumber
            attrs = {k: (v if isinstance(v, (str, bool, int, float)) else json.dumps(v, default=str))
                     for k, v in record.items()
                     if k not in ("timestamp", "severity_text", "severity_number", "body", "service.name",
                                  "trace_id", "span_id")}
            self.otel_logger.emit(timestamp=time.time_ns(), context=otel_context.get_current(),
                                  severity_number=SeverityNumber(record["severity_number"]),
                                  severity_text=record["severity_text"], body=record["body"], attributes=attrs)
        except Exception:  # telemetry must never break a request
            pass

    def debug(self, body: str, **a: Any) -> dict | None:
        return self("DEBUG", body, **a)

    def info(self, body: str, **a: Any) -> dict | None:
        return self("INFO", body, **a)

    def warn(self, body: str, **a: Any) -> dict | None:
        return self("WARN", body, **a)

    def error(self, body: str, **a: Any) -> dict | None:
        return self("ERROR", body, **a)

    def records(self, limit: int = 300) -> list[dict]:
        """Newest first."""
        with self._lock:
            items = list(self.ring)
        return items[::-1][:max(0, limit)]


_LEVELS = {logging.DEBUG: "DEBUG", logging.INFO: "INFO", logging.WARNING: "WARN", logging.ERROR: "ERROR",
           logging.CRITICAL: "ERROR"}


class StdlibBridge(logging.Handler):
    """Sends records from Python logging (uvicorn, asyncio) through JsonLog so stdout stays one format."""

    def __init__(self, log: JsonLog):
        super().__init__()
        self.log = log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            severity = _LEVELS.get(record.levelno, "INFO")
            attrs: dict[str, Any] = {"logger": record.name}
            if record.exc_info and record.exc_info[1] is not None:
                attrs["exception"] = f"{type(record.exc_info[1]).__name__}: {record.exc_info[1]}"
            self.log(severity, record.getMessage(), **attrs)
        except Exception:
            pass


def install_stdlib_bridge(log: JsonLog) -> StdlibBridge:
    """Route uvicorn's and asyncio's loggers into JsonLog; exporter loggers stay silent."""
    bridge = StdlibBridge(log)
    for name in ("uvicorn", "uvicorn.error", "asyncio", "porthole"):
        lg = logging.getLogger(name)
        lg.handlers = [h for h in lg.handlers if not isinstance(h, StdlibBridge)] + [bridge]
        lg.propagate = False
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("opentelemetry.exporter", "opentelemetry.sdk", "opentelemetry", "urllib3", "httpx"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)
    return bridge
