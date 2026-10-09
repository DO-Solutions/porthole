#!/usr/bin/env python3
"""Tentacle: the scenario service that runs on each Insights demo Droplet.

Stirs the water on demand (CPU, memory, disk, network, log storms, traced request chains, PG load)
so DigitalOcean Insights has something to see. Logs go to stdout and a JSONL file as one JSON object
per line; traces and logs also go out over OTLP/HTTP to the local collector, which may be absent.

Run:  python3 tentacle.py            (uvicorn on 0.0.0.0:$TENTACLE_PORT)
"""
from __future__ import annotations

import hmac
import json
import logging
import multiprocessing
import os
import random
import shutil
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, SimpleLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.trace import Link, SpanKind, Status, StatusCode

SEVERITY = {"DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17}
MAX_RUNNING = 8
KEEP_FINISHED = 200
MiB = 1024 * 1024


# --- settings -------------------------------------------------------------------------------

@dataclass
class Settings:
    name: str = "tentacle"
    key: str = ""
    port: int = 8800
    otlp_endpoint: str = "http://127.0.0.1:4318"
    service_name: str = "tentacle"
    peer_url: str = ""
    pg_dsn: str = ""
    fn_url: str = ""
    log_file: str = "/var/log/tentacle/tentacle.jsonl"
    data_dir: str = "/var/tmp/tentacle"
    self_url: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        name = env.get("TENTACLE_NAME") or socket.gethostname()
        port = int(env.get("TENTACLE_PORT") or 8800)
        return cls(name=name, key=env.get("TENTACLE_KEY", ""), port=port,
                   otlp_endpoint=(env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "http://127.0.0.1:4318").rstrip("/"),
                   service_name=env.get("OTEL_SERVICE_NAME") or name,
                   peer_url=(env.get("PEER_URL") or "").rstrip("/"), pg_dsn=env.get("PG_DSN", ""),
                   fn_url=env.get("FN_URL", ""),
                   log_file=env.get("TENTACLE_LOG_FILE") or "/var/log/tentacle/tentacle.jsonl",
                   data_dir=env.get("TENTACLE_DATA_DIR") or "/var/tmp/tentacle",
                   self_url=f"http://127.0.0.1:{port}")


# --- structured log: stdout + JSONL file + OTLP logs ----------------------------------------

class JsonLog:
    """One JSON object per line: timestamp, severity_text, severity_number, body, service.name,
    trace_id/span_id inside a span, plus flat attributes. Mirrored to the OTel logs pipeline."""

    MAX_FILE_BYTES = 200 * MiB

    def __init__(self, service_name: str, path: str | None, otel_logger=None, stream=None):
        self.service_name = service_name
        self.stream = stream or sys.stdout
        self.otel_logger = otel_logger
        self._lock = threading.Lock()
        self._file = None
        self.path = Path(path) if path else None
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._file = open(self.path, "a", buffering=1)
            except OSError as e:
                print(json.dumps({"timestamp": _now_iso(), "severity_text": "WARN", "severity_number": 13,
                                  "body": f"log file disabled: {e}", "service.name": service_name}),
                      file=self.stream, flush=True)

    def __call__(self, severity: str, body: str, **attrs: Any) -> dict:
        record: dict[str, Any] = {"timestamp": _now_iso(), "severity_text": severity,
                                  "severity_number": SEVERITY[severity], "body": body,
                                  "service.name": self.service_name}
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            record["trace_id"] = format(ctx.trace_id, "032x")
            record["span_id"] = format(ctx.span_id, "016x")
        record.update({k: v for k, v in attrs.items() if v is not None})
        line = json.dumps(record, default=str)
        with self._lock:
            print(line, file=self.stream, flush=True)
            if self._file:
                try:
                    self._file.write(line + "\n")
                    if self._file.tell() > self.MAX_FILE_BYTES:
                        self._rotate()
                except OSError:
                    pass
        if self.otel_logger is not None:
            try:
                from opentelemetry._logs import SeverityNumber
                self.otel_logger.emit(
                    timestamp=time.time_ns(), context=otel_context.get_current(),
                    severity_number=SeverityNumber(SEVERITY[severity]), severity_text=severity, body=body,
                    attributes={k: (v if isinstance(v, (str, bool, int, float)) else json.dumps(v, default=str))
                                for k, v in attrs.items() if v is not None})
            except Exception:  # never let telemetry break the service
                pass
        return record

    def _rotate(self) -> None:
        self._file.close()
        os.replace(self.path, self.path.with_suffix(".jsonl.1"))
        self._file = open(self.path, "a", buffering=1)

    def debug(self, body, **a): return self("DEBUG", body, **a)
    def info(self, body, **a): return self("INFO", body, **a)
    def warn(self, body, **a): return self("WARN", body, **a)
    def error(self, body, **a): return self("ERROR", body, **a)

    def close(self) -> None:
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- telemetry setup ------------------------------------------------------------------------

def setup_telemetry(settings: Settings, span_exporter=None, log_exporter=None):
    """Tracer + logger providers. With no exporters given, OTLP/HTTP to the collector through
    batch processors: exports happen on a background thread, failures are dropped quietly."""
    for noisy in ("opentelemetry.exporter", "opentelemetry.sdk", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)
    resource = Resource.create({"service.name": settings.service_name, "service.instance.id": settings.name,
                                "tentacle.name": settings.name, "host.name": socket.gethostname()})
    tp = TracerProvider(resource=resource)
    lp = LoggerProvider(resource=resource)
    if span_exporter is not None:
        tp.add_span_processor(SimpleSpanProcessor(span_exporter))
    else:
        tp.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{settings.otlp_endpoint}/v1/traces", timeout=5),
            max_queue_size=4096, export_timeout_millis=5000))
    if log_exporter is not None:
        lp.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    else:
        lp.add_log_record_processor(BatchLogRecordProcessor(
            OTLPLogExporter(endpoint=f"{settings.otlp_endpoint}/v1/logs", timeout=5),
            max_queue_size=8192, export_timeout_millis=5000))
    return tp, lp


# --- scenario runs --------------------------------------------------------------------------

@dataclass
class Run:
    id: str
    name: str
    params: dict
    started: float = field(default_factory=time.time)
    ended: float | None = None
    status: str = "running"  # running | finished | stopped | failed
    result: dict = field(default_factory=dict)
    error: str | None = None
    stop: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)

    def view(self) -> dict:
        end = self.ended or time.time()
        return {"id": self.id, "name": self.name, "status": self.status, "params": self.params,
                "started_at": datetime.fromtimestamp(self.started, timezone.utc).isoformat(),
                "ended_at": datetime.fromtimestamp(self.ended, timezone.utc).isoformat() if self.ended else None,
                "elapsed_s": round(end - self.started, 3), "result": self.result, "error": self.error}


class Scenarios:
    def __init__(self, tentacle: "Tentacle"):
        self.t = tentacle
        self.runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def running(self) -> list[Run]:
        return [r for r in self.runs.values() if r.status == "running"]

    def start(self, name: str, params: dict, fn: Callable[[Run], dict]) -> Run:
        with self._lock:
            if len(self.running()) >= MAX_RUNNING:
                raise HTTPException(429, f"{MAX_RUNNING} scenarios already running")
            run = Run(id=f"{name}-{uuid.uuid4().hex[:8]}", name=name, params=params)
            self.runs[run.id] = run
            finished = [r for r in self.runs.values() if r.status != "running"]
            for old in sorted(finished, key=lambda r: r.started)[:-KEEP_FINISHED]:
                self.runs.pop(old.id, None)
        run.thread = threading.Thread(target=self._execute, args=(run, fn), name=run.id, daemon=True)
        run.thread.start()
        return run

    def _execute(self, run: Run, fn: Callable[[Run], dict]) -> None:
        attrs = {"scenario.name": run.name, "scenario.id": run.id, "tentacle": self.t.settings.name,
                 **{f"scenario.{k}": v for k, v in run.params.items() if v is not None}}
        tracer = self.t.tracer
        with tracer.start_as_current_span(f"scenario.{run.name}", attributes=attrs) as span:
            self.t.log.info(f"scenario {run.name} start", **attrs)
            try:
                run.result = fn(run) or {}
                run.status = "stopped" if run.stop.is_set() else "finished"
                span.set_attributes({f"result.{k}": v for k, v in run.result.items()
                                     if isinstance(v, (str, bool, int, float))})
            except Exception as e:  # report, never crash the service
                run.status, run.error = "failed", f"{type(e).__name__}: {e}"
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, run.error))
            finally:
                run.ended = time.time()
                end_attrs = {**attrs, "scenario.status": run.status,
                             "scenario.elapsed_s": round(run.ended - run.started, 3),
                             **{f"result.{k}": v for k, v in run.result.items()}}
                if run.error:
                    self.t.log.error(f"scenario {run.name} failed: {run.error}", **end_attrs)
                else:
                    self.t.log.info(f"scenario {run.name} end ({run.status})", **end_attrs)

    def stop(self, run_id: str) -> Run:
        run = self.runs.get(run_id)
        if not run:
            raise HTTPException(404, f"no scenario {run_id}")
        run.stop.set()
        return run

    def stop_all(self, timeout: float = 10) -> None:
        for r in self.running():
            r.stop.set()
        for r in list(self.runs.values()):
            if r.thread and r.thread.is_alive():
                r.thread.join(timeout)


def _burn_cpu(seconds: float) -> None:  # runs in a child process
    end = time.monotonic() + seconds
    x = 0
    while time.monotonic() < end:
        for i in range(10000):
            x = (x * 31 + i) % 1000003


def _mem_info() -> dict[str, int]:
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                out[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    return out


LOG_MESSAGES = {
    "DEBUG": ["cache lookup {key}", "pool stats idle={n}", "parsed request in {ms} ms"],
    "INFO": ["order {key} accepted", "user {n} signed in", "GET /api/items 200 in {ms} ms",
             "shipment {key} dispatched"],
    "WARN": ["slow query took {ms} ms", "retrying upstream call (attempt {n})", "queue depth {n} above soft limit"],
    "ERROR": ["payment provider timeout after {ms} ms", "failed to write order {key}: connection reset",
              "GET /api/items 500: kraken ate the response"],
}


class Tentacle:
    def __init__(self, settings: Settings, span_exporter=None, log_exporter=None,
                 http_client_factory: Callable[[FastAPI], httpx.Client] | None = None, log_stream=None):
        self.settings = settings
        self.started = time.time()
        self.tracer_provider, self.logger_provider = setup_telemetry(settings, span_exporter, log_exporter)
        self.tracer = self.tracer_provider.get_tracer("tentacle")
        self.log = JsonLog(settings.service_name, settings.log_file,
                           self.logger_provider.get_logger("tentacle"), stream=log_stream)
        self.scenarios = Scenarios(self)
        self.http_client_factory = http_client_factory or (lambda app: httpx.Client(timeout=30))
        self._http: httpx.Client | None = None
        self.app: FastAPI | None = None

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = self.http_client_factory(self.app)
            HTTPXClientInstrumentor.instrument_client(self._http, tracer_provider=self.tracer_provider)
        return self._http

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.key}"} if self.settings.key else {}

    def shutdown(self) -> None:
        self.scenarios.stop_all()
        for provider in (self.tracer_provider, self.logger_provider):
            try:
                provider.shutdown()
            except Exception:
                pass
        if self._http is not None:
            self._http.close()
        self.log.close()

    # scenario bodies (each runs in its own thread) ------------------------------------------

    def cpu(self, run: Run) -> dict:
        p = run.params
        ctx = multiprocessing.get_context("spawn")
        procs = [ctx.Process(target=_burn_cpu, args=(p["seconds"],), daemon=True) for _ in range(p["workers"])]
        for proc in procs:
            proc.start()
        run.stop.wait(p["seconds"])
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
        for proc in procs:
            proc.join(5)
        return {"workers": len(procs)}

    def memory(self, run: Run) -> dict:
        p = run.params
        chunks, held = [], 0
        try:
            while held < p["mb"] and not run.stop.is_set():
                n = min(16, p["mb"] - held)
                buf = bytearray(n * MiB)
                for i in range(0, len(buf), 4096):  # touch every page so it is resident
                    buf[i] = 1
                chunks.append(buf)
                held += n
            self.log.info(f"holding {held} MB", **{"scenario.id": run.id, "memory.held_mb": held})
            run.stop.wait(p["seconds"])
        finally:
            chunks.clear()
        return {"held_mb": held}

    def disk(self, run: Run) -> dict:
        p = run.params
        Path(self.settings.data_dir).mkdir(parents=True, exist_ok=True)
        path = Path(self.settings.data_dir) / f"{run.id}.bin"
        block = os.urandom(MiB)
        written = 0
        t0 = time.monotonic()
        try:
            with open(path, "wb") as f:
                while written < p["mb"] and not run.stop.is_set():
                    f.write(block)
                    written += 1
                    if written % 64 == 0:
                        f.flush()
                        os.fsync(f.fileno())
                f.flush()
                os.fsync(f.fileno())
            write_s = time.monotonic() - t0
            self.log.info(f"wrote {written} MB to {path}", **{"scenario.id": run.id, "disk.path": str(path),
                                                               "disk.written_mb": written})
            run.stop.wait(max(0.0, p["seconds"] - write_s))
        finally:
            path.unlink(missing_ok=True)
        return {"written_mb": written, "write_s": round(write_s, 3), "path": str(path)}

    def network(self, run: Run) -> dict:
        p = run.params
        per_tick = int(p["mbps"] * 1_000_000 / 8 / 4)  # four POSTs a second
        payload = os.urandom(min(per_tick, 4 * MiB))
        sent = errors = 0
        deadline = time.monotonic() + p["seconds"]
        url = f"{self.settings.peer_url}/sink"
        while time.monotonic() < deadline and not run.stop.is_set():
            tick = time.monotonic()
            remaining = per_tick
            try:
                while remaining > 0:
                    chunk = payload[:remaining]
                    self.http.post(url, content=chunk, headers=self.auth_headers()).raise_for_status()
                    sent += len(chunk)
                    remaining -= len(chunk)
            except httpx.HTTPError as e:
                errors += 1
                if errors in (1, 10, 100):
                    self.log.warn(f"network blast error: {e}", **{"scenario.id": run.id, "peer": url})
            run.stop.wait(max(0.0, 0.25 - (time.monotonic() - tick)))
        return {"sent_mb": round(sent / MiB, 2), "errors": errors, "peer": url}

    def logs(self, run: Run) -> dict:
        p = run.params
        rate, error_pct = p["rate"], p["error_pct"]
        rest = max(0.0, 100.0 - error_pct)
        weights = {"ERROR": error_pct, "WARN": rest * 0.15, "DEBUG": rest * 0.15, "INFO": rest * 0.70}
        counts = {k: 0 for k in SEVERITY}
        deadline = time.monotonic() + p["seconds"]
        n = 0
        while time.monotonic() < deadline and not run.stop.is_set():
            second = time.monotonic()
            # a span per second so some log lines carry trace/span ids
            with self.tracer.start_as_current_span("logs.batch", attributes={"scenario.id": run.id,
                                                                             "tentacle": self.settings.name}):
                for _ in range(rate):
                    sev = random.choices(list(weights), weights=list(weights.values()))[0]
                    msg = random.choice(LOG_MESSAGES[sev]).format(
                        key=uuid.uuid4().hex[:8], n=random.randint(1, 500), ms=random.randint(1, 4000))
                    self.log(sev, msg, **{"scenario.name": "logs", "scenario.id": run.id, "log.seq": n,
                                          "http.route": random.choice(["/api/items", "/api/orders", "/checkout"]),
                                          "user.id": random.randint(1, 50)})
                    counts[sev] += 1
                    n += 1
                    if time.monotonic() - deadline > 0 or run.stop.is_set():
                        break
            run.stop.wait(max(0.0, 1.0 - (time.monotonic() - second)))
        return {"emitted": n, **{f"count_{k.lower()}": v for k, v in counts.items()}}

    def chain(self, run: Run) -> dict:
        p = run.params
        scenario_ctx = trace.get_current_span().get_span_context()
        ok = failed = 0
        traces = []
        params = {"hop": 1, "latency_ms": p["latency_ms"], "error_pct": p["error_pct"]}
        for i in range(p["count"]):
            if run.stop.is_set():
                break
            # each request is its own trace, linked back to the scenario span
            with self.tracer.start_as_current_span(
                    "chain.request", context=otel_context.Context(), kind=SpanKind.CLIENT,
                    links=[Link(scenario_ctx)],
                    attributes={"hop": 0, "tentacle": self.settings.name, "chain.index": i,
                                "scenario.id": run.id}) as span:
                traces.append(format(span.get_span_context().trace_id, "032x"))
                try:
                    r = self.http.get(f"{self.settings.self_url}/chain/hop", params=params,
                                      headers=self.auth_headers())
                    span.set_attribute("http.status_code", r.status_code)
                    if r.is_success:
                        ok += 1
                    else:
                        failed += 1
                        span.set_status(Status(StatusCode.ERROR, f"hop 1 returned {r.status_code}"))
                except httpx.HTTPError as e:
                    failed += 1
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
        return {"ok": ok, "failed": failed, "first_trace_id": traces[0] if traces else None,
                "last_trace_id": traces[-1] if traces else None}

    def hop(self, hop: int, latency_ms: int, error_pct: float) -> dict:
        s = self.settings
        out: dict[str, Any] = {"tentacle": s.name, "hop": hop, "legs": []}
        with self.tracer.start_as_current_span("chain.hop", attributes={"hop": hop, "tentacle": s.name}) as span:
            if latency_ms:
                with self.tracer.start_as_current_span("chain.latency", attributes={"latency_ms": latency_ms}):
                    time.sleep(latency_ms / 1000)
            if hop == 1 and s.peer_url:
                r = self.http.get(f"{s.peer_url}/chain/hop",
                                  params={"hop": 2, "latency_ms": latency_ms, "error_pct": error_pct},
                                  headers=self.auth_headers())
                out["legs"].append({"peer": s.peer_url, "status": r.status_code})
                if r.is_error:
                    span.set_status(Status(StatusCode.ERROR, f"peer returned {r.status_code}"))
            if s.pg_dsn:
                out["legs"].append(self._pg_leg())
            if s.fn_url:
                out["legs"].append(self._fn_leg())
            if error_pct and random.uniform(0, 100) < error_pct:
                span.set_status(Status(StatusCode.ERROR, "injected error"))
                span.set_attribute("error.injected", True)
                self.log.error(f"hop {hop} injected error", hop=hop, tentacle=s.name)
                raise HTTPException(500, f"injected error at hop {hop} on {s.name}")
            self.log.info(f"hop {hop} ok", hop=hop, tentacle=s.name)
        return out

    def _pg_leg(self) -> dict:
        with self.tracer.start_as_current_span("pg.select_now", kind=SpanKind.CLIENT,
                                               attributes={"db.system": "postgresql",
                                                           "db.statement": "SELECT now()"}) as span:
            try:
                import psycopg
                with psycopg.connect(self.settings.pg_dsn, connect_timeout=5) as conn:
                    now = conn.execute("SELECT now()").fetchone()[0]
                return {"pg": str(now)}
            except Exception as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                return {"pg_error": f"{type(e).__name__}: {e}"}

    def _fn_leg(self) -> dict:
        with self.tracer.start_as_current_span("fn.call", attributes={"fn.url": self.settings.fn_url}) as span:
            try:
                r = self.http.get(self.settings.fn_url, timeout=15)
                if r.is_error:
                    span.set_status(Status(StatusCode.ERROR, f"function returned {r.status_code}"))
                return {"fn": r.status_code}
            except httpx.HTTPError as e:
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                return {"fn_error": f"{type(e).__name__}: {e}"}

    def pg(self, run: Run) -> dict:
        import psycopg
        p = run.params
        with psycopg.connect(self.settings.pg_dsn, connect_timeout=10, autocommit=True) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS tentacle_load (id bigserial PRIMARY KEY, tentacle text, "
                         "payload text, n int, created_at timestamptz DEFAULT now())")
        counts = {"ops": 0, "errors": 0}
        lock = threading.Lock()
        deadline = time.monotonic() + p["seconds"]

        def client(idx: int) -> None:
            try:
                with psycopg.connect(self.settings.pg_dsn, connect_timeout=10, autocommit=True) as conn:
                    while time.monotonic() < deadline and not run.stop.is_set():
                        try:
                            conn.execute("INSERT INTO tentacle_load (tentacle, payload, n) VALUES (%s, %s, %s)",
                                         (self.settings.name, uuid.uuid4().hex * 4, random.randint(0, 1000)))
                            conn.execute("SELECT count(*), avg(n) FROM (SELECT n FROM tentacle_load "
                                         "ORDER BY id DESC LIMIT 500) t").fetchone()
                            if random.random() < 0.1:
                                conn.execute("DELETE FROM tentacle_load WHERE id IN (SELECT id FROM tentacle_load "
                                             "ORDER BY id LIMIT 50) AND (SELECT count(*) FROM tentacle_load) > "
                                             "100000")
                            with lock:
                                counts["ops"] += 2
                        except psycopg.Error:
                            with lock:
                                counts["errors"] += 1
                            run.stop.wait(0.5)
            except Exception as e:
                with lock:
                    counts["errors"] += 1
                self.log.error(f"pg client {idx} failed: {e}", **{"scenario.id": run.id})

        threads = [threading.Thread(target=client, args=(i,), daemon=True) for i in range(p["clients"])]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return {**counts, "clients": p["clients"]}


# --- the app --------------------------------------------------------------------------------

def create_app(settings: Settings | None = None, span_exporter=None, log_exporter=None,
               http_client_factory: Callable[[FastAPI], httpx.Client] | None = None, log_stream=None) -> FastAPI:
    settings = settings or Settings.from_env()
    t = Tentacle(settings, span_exporter, log_exporter, http_client_factory, log_stream)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        t.log.info("tentacle up", tentacle=settings.name, port=settings.port, peer=settings.peer_url or None,
                   pg=bool(settings.pg_dsn), fn=bool(settings.fn_url))
        yield
        t.log.info("tentacle down", tentacle=settings.name)
        t.shutdown()

    app = FastAPI(title=f"tentacle {settings.name}", lifespan=lifespan)
    app.state.tentacle = t
    t.app = app

    def require_key(request: Request) -> None:
        if not settings.key:
            raise HTTPException(503, "TENTACLE_KEY is not configured; mutating endpoints are disabled")
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), settings.key.encode()):
            raise HTTPException(401, "bearer token required", headers={"WWW-Authenticate": "Bearer"})

    auth = [Depends(require_key)]

    def started(run: Run) -> JSONResponse:
        return JSONResponse(run.view(), status_code=202)

    @app.get("/health")
    def health() -> dict:
        mem = _mem_info()
        total, avail = mem.get("MemTotal"), mem.get("MemAvailable")
        return {"name": settings.name, "uptime_s": round(time.time() - t.started, 1),
                "running": [r.view() for r in t.scenarios.running()],
                "load1": round(os.getloadavg()[0], 2),
                "mem_pct": round(100 * (total - avail) / total, 1) if total and avail is not None else None}

    @app.get("/scenarios")
    def scenarios() -> dict:
        runs = sorted(t.scenarios.runs.values(), key=lambda r: r.started, reverse=True)
        return {"running": [r.view() for r in runs if r.status == "running"],
                "finished": [r.view() for r in runs if r.status != "running"]}

    @app.post("/scenario/cpu", dependencies=auth, status_code=202)
    def scenario_cpu(seconds: int = Query(120, ge=1, le=3600),
                     workers: int | None = Query(None, ge=1, le=64)):
        return started(t.scenarios.start("cpu", {"seconds": seconds, "workers": workers or os.cpu_count() or 1},
                                         t.cpu))

    @app.post("/scenario/memory", dependencies=auth, status_code=202)
    def scenario_memory(seconds: int = Query(120, ge=1, le=3600), mb: int = Query(600, ge=1, le=65536)):
        avail = _mem_info().get("MemAvailable")
        if avail is not None and mb * MiB > avail - 100 * MiB:
            raise HTTPException(409, f"{mb} MB would leave less than 100 MB available "
                                     f"(MemAvailable {avail // MiB} MB)")
        return started(t.scenarios.start("memory", {"seconds": seconds, "mb": mb}, t.memory))

    @app.post("/scenario/disk", dependencies=auth, status_code=202)
    def scenario_disk(seconds: int = Query(120, ge=1, le=3600), mb: int = Query(1024, ge=1, le=262144)):
        Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(settings.data_dir).free
        if mb * MiB > free - 1024 * MiB:
            raise HTTPException(409, f"{mb} MB would leave less than 1 GB free ({free // MiB} MB free)")
        return started(t.scenarios.start("disk", {"seconds": seconds, "mb": mb}, t.disk))

    @app.post("/scenario/network", dependencies=auth, status_code=202)
    def scenario_network(seconds: int = Query(60, ge=1, le=3600), mbps: float = Query(50, gt=0, le=2000)):
        if not settings.peer_url:
            raise HTTPException(400, "PEER_URL is not configured")
        return started(t.scenarios.start("network", {"seconds": seconds, "mbps": mbps}, t.network))

    @app.post("/sink", dependencies=auth)
    async def sink(request: Request) -> dict:
        n = 0
        async for chunk in request.stream():
            n += len(chunk)
        return {"bytes": n}

    @app.post("/scenario/logs", dependencies=auth, status_code=202)
    def scenario_logs(seconds: int = Query(60, ge=1, le=3600), rate: int = Query(50, ge=1, le=5000),
                      error_pct: float = Query(10, ge=0, le=100)):
        return started(t.scenarios.start("logs", {"seconds": seconds, "rate": rate, "error_pct": error_pct}, t.logs))

    @app.post("/scenario/chain", dependencies=auth, status_code=202)
    def scenario_chain(count: int = Query(20, ge=1, le=1000), latency_ms: int = Query(0, ge=0, le=30000),
                       error_pct: float = Query(0, ge=0, le=100)):
        return started(t.scenarios.start("chain", {"count": count, "latency_ms": latency_ms,
                                                   "error_pct": error_pct, "peer": settings.peer_url or None,
                                                   "pg": bool(settings.pg_dsn), "fn": bool(settings.fn_url)},
                                         t.chain))

    @app.get("/chain/hop", dependencies=auth)
    def chain_hop(hop: int = Query(1, ge=1, le=2), latency_ms: int = Query(0, ge=0, le=30000),
                  error_pct: float = Query(0, ge=0, le=100)) -> dict:
        return t.hop(hop, latency_ms, error_pct)

    @app.post("/scenario/pg", dependencies=auth, status_code=202)
    def scenario_pg(seconds: int = Query(60, ge=1, le=3600), clients: int = Query(4, ge=1, le=32)):
        if not settings.pg_dsn:
            raise HTTPException(400, "PG_DSN is not configured")
        return started(t.scenarios.start("pg", {"seconds": seconds, "clients": clients}, t.pg))

    @app.post("/scenario/stop/{run_id}", dependencies=auth)
    def scenario_stop(run_id: str) -> dict:
        run = t.scenarios.stop(run_id)
        if run.thread:
            run.thread.join(10)
        return run.view()

    FastAPIInstrumentor.instrument_app(app, tracer_provider=t.tracer_provider, excluded_urls="/health",
                                       exclude_spans=["receive", "send"])
    return app


def main() -> None:
    import uvicorn
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, access_log=False,
                log_level="warning")


if __name__ == "__main__":
    main()
