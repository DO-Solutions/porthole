"""JSON logs: one object per line, trace ids inside a span, secrets refused, bounded ring, stdlib bridge."""
from __future__ import annotations

import io
import json
import logging

from opentelemetry.sdk.trace import TracerProvider

from porthole.jsonlog import WITHHELD, JsonLog, install_stdlib_bridge

KEYS = {"timestamp", "severity_text", "severity_number", "body", "service.name"}


def lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(x) for x in stream.getvalue().splitlines()]


def test_one_json_object_per_line_with_documented_keys():
    out = io.StringIO()
    log = JsonLog(stream=out)
    log.info("porthole up", version="0.1.0", fleet_tentacles=3)
    log.warn("slow upstream", ms=812.5)
    recs = lines(out)
    assert len(recs) == 2
    assert KEYS <= set(recs[0]) and recs[0]["severity_number"] == 9 and recs[1]["severity_text"] == "WARN"
    assert recs[0]["service.name"] == "porthole" and recs[0]["version"] == "0.1.0"
    assert recs[0]["timestamp"].endswith("Z")
    assert "trace_id" not in recs[0]


def test_trace_and_span_ids_inside_a_span():
    out = io.StringIO()
    log = JsonLog(stream=out)
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("work") as span:
        log.info("inside")
        ctx = span.get_span_context()
    rec = lines(out)[0]
    assert rec["trace_id"] == format(ctx.trace_id, "032x") and rec["span_id"] == format(ctx.span_id, "016x")


def test_a_record_with_a_secret_is_withheld():
    out = io.StringIO()
    log = JsonLog(stream=out, secrets=["sekret-value-000111"])
    log.error("calling upstream with sekret-value-000111")
    log.info("fine", detail={"nested": "also sekret-value-000111"})
    text = out.getvalue()
    assert "sekret-value-000111" not in text
    recs = lines(out)
    assert all(r["body"] == WITHHELD for r in recs)
    assert recs[0]["withheld.severity"] == "ERROR" and recs[1]["withheld.keys"] == ["detail"]
    assert all("sekret" not in json.dumps(r) for r in log.records())


def test_ring_is_bounded_and_newest_first():
    log = JsonLog(stream=io.StringIO(), ring_size=5)
    for i in range(8):
        log.info(f"line {i}")
    recs = log.records()
    assert len(recs) == 5 and recs[0]["body"] == "line 7" and recs[-1]["body"] == "line 3"
    assert len(log.records(2)) == 2


def test_level_filter():
    out = io.StringIO()
    log = JsonLog(stream=out, level="WARN")
    log.info("quiet")
    log.debug("quieter")
    log.error("loud")
    assert [r["body"] for r in lines(out)] == ["loud"]


def test_stdlib_bridge_keeps_one_format():
    out = io.StringIO()
    log = JsonLog(stream=out)
    install_stdlib_bridge(log)
    logging.getLogger("uvicorn.error").warning("Started server process")
    rec = lines(out)[-1]
    assert rec["body"] == "Started server process" and rec["severity_text"] == "WARN"
    assert rec["logger"] == "uvicorn.error"


def test_mirror_to_otel_logger():
    emitted = []

    class Fake:
        def emit(self, **kw):
            emitted.append(kw)

    log = JsonLog(stream=io.StringIO())
    log.otel_logger = Fake()
    log.info("mirrored", **{"http.route": "/api/config"})
    assert emitted and emitted[0]["body"] == "mirrored" and emitted[0]["attributes"] == {"http.route": "/api/config"}
