"""An httpx.MockTransport emulation of the tentacle API (design section 3.4), the load balancer and the Function.

Runs finish after their seconds on the injected clock, so a test can sail a ten-minute voyage in milliseconds.
The same FakeFleet is the world the fake Insights reads: a cpu run here lifts cpu_utilization there."""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

# Query limits of tentacle.py: name -> {param: (default, low, high, type)}
LIMITS: dict[str, dict[str, tuple[Any, float, float, type]]] = {
    "cpu": {"seconds": (120, 1, 3600, int), "workers": (None, 1, 64, int)},
    "memory": {"seconds": (120, 1, 3600, int), "mb": (600, 1, 65536, int)},
    "disk": {"seconds": (120, 1, 3600, int), "mb": (1024, 1, 262144, int)},
    "network": {"seconds": (60, 1, 3600, int), "mbps": (50, 0.001, 2000, float)},
    "logs": {"seconds": (60, 1, 3600, int), "rate": (50, 1, 5000, int), "error_pct": (10, 0, 100, float)},
    "chain": {"count": (20, 1, 1000, int), "latency_ms": (0, 0, 30000, int), "error_pct": (0, 0, 100, float)},
    "pg": {"seconds": (60, 1, 3600, int), "clients": (4, 1, 32, int)},
}
MAX_RUNNING = 8


def _json(status: int, body: Any, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


@dataclass
class FakeRun:
    id: str
    name: str
    params: dict
    started_at: datetime
    duration_s: float
    status: str = "running"
    ended_at: datetime | None = None
    result: dict = field(default_factory=dict)
    error: str | None = None

    def view(self, now: datetime) -> dict:
        end = self.ended_at or now
        return {"id": self.id, "name": self.name, "status": self.status, "params": self.params,
                "started_at": self.started_at.isoformat(),
                "ended_at": self.ended_at.isoformat() if self.ended_at else None,
                "elapsed_s": round((end - self.started_at).total_seconds(), 3), "result": self.result,
                "error": self.error}

    def as_world(self) -> dict:
        return {"name": self.name, "params": self.params, "started_at": self.started_at,
                "ended_at": self.ended_at, "status": self.status, "result": self.result, "id": self.id}


def _result(run: FakeRun, fraction: float) -> dict:
    p = run.params
    if run.name == "cpu":
        return {"workers": p["workers"]}
    if run.name == "memory":
        return {"held_mb": p["mb"]}
    if run.name == "disk":
        return {"written_mb": p["mb"], "write_s": round(p["mb"] / 400, 3), "path": f"/var/tmp/tentacle/{run.id}.bin"}
    if run.name == "network":
        return {"sent_mb": round(p["mbps"] * p["seconds"] * fraction / 8, 2), "errors": 0, "peer": "peer/sink"}
    if run.name == "logs":
        n = int(p["rate"] * p["seconds"] * fraction)
        err = round(n * p["error_pct"] / 100)
        rest = n - err
        warn = debug = round(rest * 0.15)
        return {"emitted": n, "count_debug": debug, "count_info": rest - warn - debug, "count_warn": warn,
                "count_error": err}
    if run.name == "chain":
        done = max(1, int(p["count"] * fraction))
        failed = round(done * p["error_pct"] / 100)
        ids = [hashlib.md5(f"{run.id}:{i}".encode()).hexdigest() for i in (0, done - 1)]
        return {"ok": done - failed, "failed": failed, "first_trace_id": ids[0], "last_trace_id": ids[1]}
    if run.name == "pg":
        return {"ops": int(p["clients"] * p["seconds"] * fraction * 40), "errors": 0, "clients": p["clients"]}
    return {}


class FakeTentacle:
    def __init__(self, name: str, url: str, peer_url: str = "", pg: bool = False, key: str = "dev-key",
                 started_at: datetime | None = None):
        self.name, self.url, self.peer_url, self.pg, self.key = name, url, peer_url, pg, key
        self.runs: dict[str, FakeRun] = {}
        self.started_at = started_at or datetime.now(timezone.utc)
        self.mem_total_mb, self.mem_base_mb, self.disk_free_mb = 1024, 300, 20_000
        self.unreachable = self.refuse_memory = self.refuse_disk = False
        self.requests: list[tuple[str, str]] = []
        self.auth_seen: list[str | None] = []

    def refresh(self, now: datetime) -> None:
        for run in self.runs.values():
            if run.status == "running" and now >= run.started_at + timedelta(seconds=run.duration_s):
                run.status, run.ended_at = "finished", run.started_at + timedelta(seconds=run.duration_s)
                run.result = _result(run, 1.0)

    def running(self) -> list[FakeRun]:
        return [r for r in self.runs.values() if r.status == "running"]

    def handle(self, request: httpx.Request, now: datetime) -> httpx.Response:
        self.refresh(now)
        path = request.url.path
        self.requests.append((request.method, path))
        if request.method == "GET" and path == "/health":
            held = sum(r.params["mb"] for r in self.running() if r.name == "memory")
            cpu = sum(r.params["workers"] for r in self.running() if r.name == "cpu")
            return _json(200, {"name": self.name, "uptime_s": round((now - self.started_at).total_seconds(), 1),
                               "running": [r.view(now) for r in self.running()], "load1": round(0.04 + cpu, 2),
                               "mem_pct": round(100 * (self.mem_base_mb + held) / self.mem_total_mb, 1)})
        if request.method == "GET" and path == "/scenarios":
            runs = sorted(self.runs.values(), key=lambda r: r.started_at, reverse=True)
            return _json(200, {"running": [r.view(now) for r in runs if r.status == "running"],
                               "finished": [r.view(now) for r in runs if r.status != "running"]})
        auth = request.headers.get("authorization")
        self.auth_seen.append(auth)
        if not self.key:
            return _json(503, {"detail": "TENTACLE_KEY is not configured; mutating endpoints are disabled"})
        if auth != f"Bearer {self.key}":
            return _json(401, {"detail": "bearer token required"}, {"WWW-Authenticate": "Bearer"})
        if request.method == "POST" and path.startswith("/scenario/stop/"):
            run = self.runs.get(path.rsplit("/", 1)[1])
            if run is None:
                return _json(404, {"detail": f"no scenario {path.rsplit('/', 1)[1]}"})
            if run.status == "running":
                elapsed = (now - run.started_at).total_seconds()
                run.status, run.ended_at = "stopped", now
                run.result = _result(run, min(1.0, elapsed / max(run.duration_s, 0.001)))
            return _json(200, run.view(now))
        if request.method == "POST" and path.startswith("/scenario/"):
            return self.start(path.rsplit("/", 1)[1], dict(request.url.params), now)
        return _json(404, {"detail": "Not Found"})

    def start(self, name: str, query: dict, now: datetime) -> httpx.Response:
        if name not in LIMITS:
            return _json(404, {"detail": "Not Found"})
        params: dict[str, Any] = {}
        for key, (default, low, high, kind) in LIMITS[name].items():
            raw = query.get(key)
            if raw is None:
                params[key] = default
                continue
            try:
                value = kind(raw)
            except ValueError:
                return _json(422, {"detail": [{"type": f"{kind.__name__}_parsing", "loc": ["query", key],
                                               "msg": f"Input should be a valid {kind.__name__}", "input": raw}]})
            if not low <= value <= high:
                return _json(422, {"detail": [{"type": "range", "loc": ["query", key],
                                               "msg": f"Input should be between {low} and {high}", "input": raw}]})
            params[key] = value
        if name == "cpu":
            params["workers"] = params["workers"] or 1
        if name == "network" and not self.peer_url:
            return _json(400, {"detail": "PEER_URL is not configured"})
        if name == "pg" and not self.pg:
            return _json(400, {"detail": "PG_DSN is not configured"})
        if name == "memory":
            held = sum(r.params["mb"] for r in self.running() if r.name == "memory")
            avail = self.mem_total_mb - self.mem_base_mb - held
            if self.refuse_memory or params["mb"] > avail - 100:
                return _json(409, {"detail": f"{params['mb']} MB would leave less than 100 MB available "
                                             f"(MemAvailable {avail} MB)"})
        if name == "disk" and (self.refuse_disk or params["mb"] > self.disk_free_mb - 1024):
            return _json(409, {"detail": f"{params['mb']} MB would leave less than 1 GB free "
                                         f"({self.disk_free_mb} MB free)"})
        if len(self.running()) >= MAX_RUNNING:
            return _json(429, {"detail": f"{MAX_RUNNING} scenarios already running"})
        if name == "chain":
            params.update({"peer": self.peer_url or None, "pg": self.pg, "fn": False})
            duration = params["count"] * (2 * params["latency_ms"] / 1000 + 0.02)
        else:
            duration = float(params["seconds"])
        run = FakeRun(id=f"{name}-{uuid.uuid4().hex[:8]}", name=name, params=params, started_at=now,
                      duration_s=duration)
        self.runs[run.id] = run
        return _json(202, run.view(now))


class FakeFleet:
    """Every tentacle of a fleet, its LB and Function, behind one MockTransport; also the fake Insights' world."""

    def __init__(self, fleet: Any, key: str = "dev-key", clock: Any = None):
        self.clock = clock
        self.tentacles: dict[str, FakeTentacle] = {}
        urls = {t.name: t.url for t in fleet.tentacles}
        for t in fleet.tentacles:
            self.tentacles[urlsplit(t.url).netloc] = FakeTentacle(
                t.name, t.url, urls.get(t.peer or "", ""), pg="database" in fleet.sea, key=key,
                started_at=self.now() - timedelta(hours=6))
        lb = fleet.sea.get("load_balancer")
        self.lb_host = str(lb.extra.get("ip")) if lb and lb.extra.get("ip") else None
        fn = fleet.sea.get("functions")
        self.fn_host = urlsplit(fn.extra["url"]).netloc if fn and fn.extra.get("url") else None
        self.load: deque[tuple[str, float]] = deque(maxlen=200_000)
        self._lock = threading.Lock()
        self.lb_turn = 0

    def now(self) -> datetime:
        return self.clock.now() if self.clock is not None else datetime.now(timezone.utc)

    def by_name(self, name: str) -> FakeTentacle:
        return next(t for t in self.tentacles.values() if t.name == name)

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.netloc.decode() if isinstance(request.url.netloc, bytes) else request.url.netloc
        now = self.now()
        with self._lock:
            tentacle = self.tentacles.get(host)
            if tentacle is not None:
                if tentacle.unreachable:
                    raise httpx.ConnectError("connection refused", request=request)
                return tentacle.handle(request, now)
            if self.lb_host and request.url.host == self.lb_host:
                self.load.append(("lb", now.timestamp()))
                backends = [t for t in self.tentacles.values() if not t.unreachable][:2]
                if not backends:
                    return _json(503, {"detail": "no healthy backend"})
                self.lb_turn += 1
                return backends[self.lb_turn % len(backends)].handle(httpx.Request("GET", "http://lb/health"), now)
            if self.fn_host and host == self.fn_host:
                self.load.append(("fn", now.timestamp()))
                return httpx.Response(200, text=json.dumps({"body": "pong"}),
                                      headers={"content-type": "application/json"})
        raise httpx.ConnectError(f"no route to {host}", request=request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # the world protocol read by dev/fake_promql.py and dev/fake_insights.py ------------------------

    def runs(self, name: str) -> list[dict]:
        now = self.now()
        with self._lock:
            for t in self.tentacles.values():
                if t.name == name:
                    t.refresh(now)
                    return [r.as_world() for r in t.runs.values()]
        return []

    def runs_any(self, scenario: str) -> list[dict]:
        now = self.now()
        out = []
        with self._lock:
            for t in self.tentacles.values():
                t.refresh(now)
                out += [{**r.as_world(), "tentacle": t.name} for r in t.runs.values() if r.name == scenario]
        return out

    def requests(self, kind: str, t0: float, t1: float) -> int:
        with self._lock:
            return sum(1 for k, t in self.load if k == kind and t0 <= t < t1)

    def head_rate(self, t: float) -> float:
        return 0.0
