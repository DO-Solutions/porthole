"""The API trace: every upstream call the head makes, kept in a 500-entry ring and pushed as api_call events.

TracedInsights subclasses the harness client and records each exchange from send(); traced_request() does the
same for tentacle, Function and load balancer calls. No record ever holds a token: curl output says
$DIGITALOCEAN_TOKEN, request bodies go through the harness's redact(), and configured secrets are scrubbed."""
from __future__ import annotations

import json
import re
import shlex
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from opentelemetry import trace as otel_trace

from insights_harness import Insights, excerpt, redact
from porthole.cache import Budget, BudgetExhausted
from porthole.clock import iso, new_id

_calls: ContextVar[list | None] = ContextVar("porthole_calls", default=None)
_caller: ContextVar[str] = ContextVar("porthole_caller", default="")
REGION_IN_PATH = re.compile(r"^/v2/insights/query/([a-z0-9]+)/")
HEAD_BYTES = 2048


@contextmanager
def collect_calls() -> Iterator[list[str]]:
    """Collects the ids of the calls made inside the block, including calls made in worker threads."""
    calls: list[str] = []
    token = _calls.set(calls)
    try:
        yield calls
    finally:
        _calls.reset(token)


@contextmanager
def caller(name: str) -> Iterator[None]:
    token = _caller.set(name)
    try:
        yield
    finally:
        _caller.reset(token)


def current_trace_id() -> str | None:
    ctx = otel_trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


@dataclass
class ApiCall:
    id: str
    t: str
    target: str
    method: str
    path: str
    params: Any
    body: Any
    status: int | None
    content_type: str | None
    ms: float | None
    excerpt: str
    error: str | None
    trace_id: str | None
    region: str | None
    caller: str
    entity: str | None = None
    url: str | None = None
    response_head: str = ""
    mono: float = 0.0

    def summary(self) -> dict:
        return {"id": self.id, "t": self.t, "target": self.target, "method": self.method, "path": self.path,
                "status": self.status, "ms": self.ms, "excerpt": self.excerpt[:200], "error": self.error,
                "region": self.region, "caller": self.caller, "entity": self.entity}

    def full(self) -> dict:
        out = asdict(self)
        out.pop("mono", None)
        return out


class ApiTrace:
    def __init__(self, hub: Any = None, size: int = 500, secrets: Iterable[str] = (),
                 now: Callable[[], Any] | None = None, monotonic: Callable[[], float] = time.monotonic):
        self.hub = hub
        self.ring: deque[ApiCall] = deque(maxlen=size)
        self.secrets = [s for s in secrets if s]
        self.now = now
        self.monotonic = monotonic
        self._lock = threading.Lock()

    def scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            for s in self.secrets:
                value = value.replace(s, "***")
            return value
        if isinstance(value, dict):
            return {k: self.scrub(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub(v) for v in value]
        return value

    def record(self, *, target: str, method: str, path: str, params: Any = None, body: Any = None,
               status: int | None = None, content_type: str | None = None, ms: float | None = None,
               text: str = "", error: str | None = None, region: str | None = None, url: str | None = None,
               entity: str | None = None, trace_id: str | None = None) -> ApiCall:
        from datetime import datetime, timezone
        t = self.now() if self.now else datetime.now(timezone.utc)
        if region is None:
            m = REGION_IN_PATH.match(path)
            region = m.group(1) if m else None
        call = ApiCall(id=new_id("c"), t=iso(t, millis=True), target=target, method=method,
                       path=self.scrub(path), params=self.scrub(redact(params) if params else None),
                       body=self.scrub(redact(body)) if body is not None else None, status=status,
                       content_type=content_type, ms=round(ms, 1) if ms is not None else None,
                       excerpt=self.scrub(excerpt(text)) if text else "", error=self.scrub(error),
                       trace_id=trace_id or current_trace_id(), region=region, caller=_caller.get(),
                       entity=entity, url=self.scrub(url), response_head=self.scrub(text[:HEAD_BYTES]),
                       mono=self.monotonic())
        with self._lock:
            self.ring.append(call)
        collected = _calls.get()
        if collected is not None:
            collected.append(call.id)
        if self.hub is not None:
            s = call.summary()
            self.hub.publish("api_call", {k: s[k] for k in ("id", "t", "target", "method", "path", "status", "ms",
                                                            "region", "entity")})
        return call

    def list(self, limit: int = 50, target: str | None = None, q: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self.ring)[::-1]
        if target:
            items = [c for c in items if c.target == target]
        if q:
            items = [c for c in items if q.lower() in c.path.lower()]
        return [c.summary() for c in items[:max(0, min(limit, 500))]]

    def get(self, call_id: str) -> ApiCall | None:
        with self._lock:
            return next((c for c in self.ring if c.id == call_id), None)

    def stats(self) -> dict:
        now = self.monotonic()
        with self._lock:
            recent = [c for c in self.ring if now - c.mono < 60]
            last = self.ring[-1].summary() if self.ring else None
        return {"last_minute": len(recent), "total": len(self.ring), "last": last}


def _capped_sleep(seconds: float) -> None:
    time.sleep(min(seconds, 5.0))  # a 429 retry must not hold a worker thread for minutes


class TracedInsights(Insights):
    """The harness client with every exchange recorded in the API trace and counted against the budget."""

    def __init__(self, token: str, *, trace: ApiTrace, budget: Budget | None = None,
                 base_url: str = "https://api.digitalocean.com", transport: httpx.BaseTransport | None = None,
                 timeout: float = 30, sleep: Callable[[float], None] = _capped_sleep):
        self._tl = threading.local()
        super().__init__(token, region=None, timeout=timeout, trace=False, base_url=base_url,
                         transport=transport, sleep=sleep)
        self.api_trace = trace
        self.budget = budget
        self.base_url = base_url

    # the harness writes last_elapsed_ms on the instance; worker threads share it, so keep it per thread
    @property
    def last_elapsed_ms(self) -> float | None:  # type: ignore[override]
        return getattr(self._tl, "ms", None)

    @last_elapsed_ms.setter
    def last_elapsed_ms(self, value: float | None) -> None:
        self._tl.ms = value

    def send(self, method: str, path: str, params: dict | None = None, json_body: Any = None,
             timeout: float | None = None, content: bytes | None = None) -> httpx.Response:
        if self.budget is not None:
            ok, retry_in = self.budget.take()
            if not ok:
                raise BudgetExhausted(retry_in)
        clean = {k: v for k, v in (params or {}).items() if v is not None} or None
        url = self.base_url + path + (("?" + urlencode(clean, doseq=True)) if clean else "")
        try:
            resp = super().send(method, path, params=params, json_body=json_body, timeout=timeout, content=content)
        except httpx.HTTPError as e:
            self.api_trace.record(target="insights", method=method, path=path, params=clean, body=json_body,
                                  ms=self.last_elapsed_ms, error=f"{type(e).__name__}: {e}", url=url)
            raise
        self.api_trace.record(target="insights", method=method, path=path, params=clean, body=json_body,
                              status=resp.status_code, content_type=resp.headers.get("content-type"),
                              ms=self.last_elapsed_ms, text=resp.text, url=url)
        return resp


async def traced_request(client: httpx.AsyncClient, api_trace: ApiTrace, *, target: str, method: str, url: str,
                         entity: str | None = None, record: bool = True, record_errors: bool = True,
                         **kwargs: Any) -> httpx.Response:
    """One async exchange recorded like an Insights call. Transport errors are recorded, then re-raised.
    record=False keeps routine successes out of the ring; record_errors=False does the same for failures."""
    t0 = time.monotonic()
    path = urlsplit(url).path or "/"
    params = kwargs.get("params")
    try:
        resp = await client.request(method, url, **kwargs)
    except httpx.HTTPError as e:
        if record or record_errors:
            api_trace.record(target=target, method=method, path=path, params=params, url=url, entity=entity,
                             ms=(time.monotonic() - t0) * 1000, error=f"{type(e).__name__}: {e}")
        raise
    if record or (resp.is_error and record_errors):
        api_trace.record(target=target, method=method, path=path, params=params, url=url, entity=entity,
                         status=resp.status_code, content_type=resp.headers.get("content-type"),
                         ms=(time.monotonic() - t0) * 1000, text=resp.text)
    return resp


AUTH_VARS = {"insights": "DIGITALOCEAN_TOKEN", "tentacle": "TENTACLE_KEY"}


def as_curl(call: ApiCall, base_url: str = "https://api.digitalocean.com") -> str:
    """A curl line with $DIGITALOCEAN_TOKEN (or $TENTACLE_KEY) in place of the real secret."""
    split = urlsplit(call.url or base_url + call.path)
    bare = f"{split.scheme}://{split.netloc}{split.path}"
    parts = ["curl", "-sS"]
    if call.method != "GET":
        parts += ["-X", call.method]
    var = AUTH_VARS.get(call.target)
    if var and (call.target == "insights" or call.method != "GET"):
        parts += ["-H", f'"Authorization: Bearer ${var}"']
    params = call.params or {}
    if call.method == "GET" and params:
        parts += ["-G", shlex.quote(bare)]
        for k, v in params.items():
            for item in (v if isinstance(v, list) else [v]):
                parts += ["--data-urlencode", shlex.quote(f"{k}={item}")]
    else:
        query = ("?" + urlencode(params, doseq=True)) if params else ""
        parts.append(shlex.quote(bare + query))
    if call.body is not None:
        parts += ["-H", shlex.quote("Content-Type: application/json"),
                  "-d", shlex.quote(json.dumps(call.body, separators=(",", ":")))]
    return " ".join(parts)


def as_bugs_md(call: ApiCall, curl: str) -> str:
    """The BUGS.md template of design section 8.5, filled with this call."""
    query = f" params {json.dumps(call.params, separators=(',', ':'))}" if call.params else ""
    body = f", body {json.dumps(call.body, separators=(',', ':'))}" if call.body is not None else ""
    status = call.status if call.status is not None else call.error
    lines = [
        f"## B-000  {call.method} {call.path} returned {status}     (open)",
        f"- when: {call.t}   where: head {call.caller or call.target}   finding: ",
        f"- request we made / received: {call.method} {call.path}{query}{body}",
        f"- response: {status} {call.content_type or ''}, {call.ms} ms, trace id {call.trace_id or '-'}",
        "- expected: ",
        f"- observed: {call.excerpt or call.error or ''}",
        f"- reproduce: `{curl}`",
        "- status: open -> reported -> fixed / wontfix",
    ]
    return "\n".join(lines) + "\n"
