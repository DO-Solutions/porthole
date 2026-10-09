"""Telemetry: no exporter without an endpoint, the ring keeps request spans, log lines carry trace ids."""
from __future__ import annotations

import time

from conftest import TOKEN, AppEnv, base_env
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from porthole.telemetry import RingSpanExporter, parse_headers, setup_telemetry


def _processors(telemetry) -> tuple:
    return telemetry.tracer_provider._active_span_processor._span_processors


def test_no_endpoint_means_no_exporter():
    t = setup_telemetry("porthole", "tor1")
    assert t.describe() == {"exporting": False, "endpoint": None,
                            "text": "not exported: no OTEL_EXPORTER_OTLP_ENDPOINT"}
    assert not any(isinstance(p, BatchSpanProcessor) for p in _processors(t))
    assert len(_processors(t)) == 1  # the ring only


def test_endpoint_adds_batch_exporters():
    t = setup_telemetry("porthole", "tor1", endpoint="http://127.0.0.1:9", headers="api-key=abc")
    assert t.describe()["text"] == "OTLP to http://127.0.0.1:9"
    assert any(isinstance(p, BatchSpanProcessor) for p in _processors(t))
    t.shutdown()


async def test_ring_captures_the_request_span(env):
    await env.client.get("/api/config")
    traces = env.deps.telemetry.ring.traces()
    assert any(tr["root"].startswith("GET /api/config") for tr in traces)
    r = await env.client.get("/api/traces/own")
    body = r.json()
    assert body["export"]["exporting"] is False and body["traces"]
    span = body["traces"][0]["spans"][0]
    assert {"name", "span_id", "parent_id", "offset_ms", "duration_ms", "status", "kind"} <= set(span)


async def test_request_log_lines_carry_trace_ids(env):
    await env.client.get("/api/config")
    line = next(x for x in env.log_lines() if x["body"].startswith("GET /api/config 200"))
    assert len(line["trace_id"]) == 32 and len(line["span_id"]) == 16
    assert line["http.status_code"] == 200


async def test_no_errors_logged_without_a_collector():
    async with AppEnv(base_env(OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9")) as e:
        t0 = time.monotonic()
        for _ in range(3):
            assert (await e.client.get("/api/config")).status_code == 200
        assert time.monotonic() - t0 < 5
    severities = {line["severity_text"] for line in e.log_lines() if "degraded" not in line["body"]}
    assert "ERROR" not in severities


async def test_log_records_never_hold_the_token(env):
    env.deps.log.info(f"oops {TOKEN}")
    assert TOKEN not in env.stdout.getvalue()
    r = await env.client.get("/api/logs/own")
    assert TOKEN not in r.text and r.json()["records"]


def test_ring_groups_spans_by_trace_and_keeps_fifty():
    ring = RingSpanExporter(max_traces=50)
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(ring))
    tracer = tp.get_tracer("t")
    for i in range(60):
        with tracer.start_as_current_span(f"root-{i}"):
            with tracer.start_as_current_span("child"):
                pass
    traces = ring.traces(100)
    assert len(traces) == 50 and traces[0]["root"] == "root-59"
    assert traces[0]["span_count"] == 2 and ring.find(traces[0]["trace_id"])["root"] == "root-59"


def test_parse_headers():
    assert parse_headers("api-key=a%20b, x-team = kraken ,bad") == {"api-key": "a b", "x-team": "kraken"}
