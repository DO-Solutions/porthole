"""OpenTelemetry for the head, following the tentacle's pattern: tracer and logger providers, OTLP only by env.

With OTEL_EXPORTER_OTLP_ENDPOINT unset no exporter exists and nothing leaves the process; in every case a ring
exporter keeps the last 50 traces for the Traces page, which says what was exported and where."""
from __future__ import annotations

import logging
import socket
import threading
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, SimpleLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult

EXCLUDED_URLS = "healthz,/events,/static/,favicon"
KEPT_ATTRIBUTES = ("http.method", "http.request.method", "http.route", "http.status_code",
                   "http.response.status_code", "url.path", "url.full", "http.url", "http.target", "server.address",
                   "net.peer.name",
                   "porthole.target", "porthole.caller", "scenario.name", "scenario.id", "voyage.id", "error.type")


def parse_headers(raw: str) -> dict[str, str]:
    """OTEL_EXPORTER_OTLP_HEADERS format: k=v pairs separated by commas, values URL-encoded."""
    out = {}
    for pair in (raw or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip():
                out[k.strip()] = unquote(v.strip())
    return out


class RingSpanExporter(SpanExporter):
    """Keeps the most recent traces in memory, grouped by trace id."""

    def __init__(self, max_traces: int = 50, max_spans: int = 200, secrets: Iterable[str] = ()):
        self.max_traces, self.max_spans = max_traces, max_spans
        self.secrets = [s for s in secrets if s]
        self._traces: OrderedDict[str, list[dict]] = OrderedDict()
        self._lock = threading.Lock()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            for span in spans:
                ctx = span.get_span_context()
                if ctx is None:
                    continue
                tid = format(ctx.trace_id, "032x")
                bucket = self._traces.get(tid)
                if bucket is None:
                    bucket = self._traces[tid] = []
                    while len(self._traces) > self.max_traces:
                        self._traces.popitem(last=False)
                if len(bucket) < self.max_spans:
                    bucket.append(self._record(span))
        return SpanExportResult.SUCCESS

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            for s in self.secrets:
                value = value.replace(s, "***")
        return value

    def _record(self, span: ReadableSpan) -> dict:
        attrs = span.attributes or {}
        return {"name": span.name, "span_id": format(span.context.span_id, "016x"),
                "parent_id": format(span.parent.span_id, "016x") if span.parent else None,
                "start_ns": span.start_time or 0, "end_ns": span.end_time or span.start_time or 0,
                "status": span.status.status_code.name, "kind": span.kind.name.lower(),
                "attributes": {k: self._scrub(attrs[k]) for k in KEPT_ATTRIBUTES if k in attrs}}

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True

    def traces(self, limit: int = 50) -> list[dict]:
        """Newest first, each with its spans as offsets from the first span."""
        with self._lock:
            items = list(self._traces.items())[::-1][:max(0, limit)]
            return [self._view(tid, list(spans)) for tid, spans in items]

    def find(self, trace_id: str) -> dict | None:
        with self._lock:
            spans = self._traces.get(trace_id)
            return self._view(trace_id, list(spans)) if spans else None

    @staticmethod
    def _view(tid: str, spans: list[dict]) -> dict:
        start = min(s["start_ns"] for s in spans)
        end = max(s["end_ns"] for s in spans)
        ids = {s["span_id"] for s in spans}
        roots = [s for s in spans if not s["parent_id"] or s["parent_id"] not in ids]
        root = min(roots or spans, key=lambda s: s["start_ns"])
        status = "ERROR" if any(s["status"] == "ERROR" for s in spans) else root["status"]
        ordered = sorted(spans, key=lambda s: s["start_ns"])
        return {"trace_id": tid, "root": root["name"],
                "started_at": datetime.fromtimestamp(start / 1e9, timezone.utc).isoformat(
                    timespec="milliseconds").replace("+00:00", "Z"),
                "duration_ms": round((end - start) / 1e6, 2), "status": status, "span_count": len(spans),
                "spans": [{"name": s["name"], "span_id": s["span_id"], "parent_id": s["parent_id"],
                           "offset_ms": round((s["start_ns"] - start) / 1e6, 2),
                           "duration_ms": round((s["end_ns"] - s["start_ns"]) / 1e6, 2),
                           "status": s["status"], "kind": s["kind"], "attributes": s["attributes"]}
                          for s in ordered]}


@dataclass
class Telemetry:
    tracer_provider: TracerProvider
    logger_provider: LoggerProvider
    ring: RingSpanExporter
    endpoint: str

    @property
    def tracer(self) -> Any:
        return self.tracer_provider.get_tracer("porthole")

    def describe(self) -> dict:
        if self.endpoint:
            return {"exporting": True, "endpoint": self.endpoint, "text": f"OTLP to {self.endpoint}"}
        return {"exporting": False, "endpoint": None, "text": "not exported: no OTEL_EXPORTER_OTLP_ENDPOINT"}

    def shutdown(self) -> None:
        for provider in (self.tracer_provider, self.logger_provider):
            try:
                provider.shutdown()
            except Exception:
                pass


def setup_telemetry(service_name: str, region: str, endpoint: str = "", headers: str = "",
                    secrets: Iterable[str] = (), span_exporter: SpanExporter | None = None,
                    log_exporter: Any = None) -> Telemetry:
    """Providers with the resource attributes of design section 3.7. Exporter failures stay silent."""
    for noisy in ("opentelemetry.exporter", "opentelemetry.sdk", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)
    resource = Resource.create({"service.name": service_name, "service.instance.id": socket.gethostname(),
                                "deployment.region": region})
    ring = RingSpanExporter(secrets=secrets)
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(SimpleSpanProcessor(ring))
    lp = LoggerProvider(resource=resource)
    hdrs = parse_headers(headers)
    if span_exporter is not None:
        tp.add_span_processor(SimpleSpanProcessor(span_exporter))
    elif endpoint:
        tp.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=hdrs or None, timeout=5),
            max_queue_size=2048, export_timeout_millis=5000))
    if log_exporter is not None:
        lp.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    elif endpoint:
        lp.add_log_record_processor(BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=f"{endpoint}/v1/logs", headers=hdrs or None, timeout=5),
            max_queue_size=4096, export_timeout_millis=5000))
    return Telemetry(tracer_provider=tp, logger_provider=lp, ring=ring, endpoint=endpoint)


def instrument_app(app: Any, telemetry: Telemetry) -> None:
    FastAPIInstrumentor.instrument_app(app, tracer_provider=telemetry.tracer_provider,
                                       excluded_urls=EXCLUDED_URLS, exclude_spans=["receive", "send"])


def instrument_client(client: Any, telemetry: Telemetry) -> None:
    HTTPXClientInstrumentor.instrument_client(client, tracer_provider=telemetry.tracer_provider)
