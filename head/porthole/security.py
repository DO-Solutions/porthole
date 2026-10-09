"""The public-site guards: the captain's key, rate limits, security headers, the body cap and client addresses.

Mutating routes depend on Guard.mutate(); the brain and webhook routes use their own limiters. Errors use the
head's JSON error shape, {"error": {"code", "message", "detail"}}, with a matching status."""
from __future__ import annotations

import hmac
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from starlette.exceptions import HTTPException

CSP = "default-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
SECURITY_HEADERS = (
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)
MAX_BODY = 64 * 1024

# per-IP and overall requests per minute (design section 6); None means no overall limit
LIMITS = {"mutate": (12, 60), "brain": (3, 10), "hook": (60, None)}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, detail: Any = None,
                 headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail
        self.headers = headers or {}

    def body(self) -> dict:
        return error_body(self.code, self.message, self.detail)


def error_body(code: str, message: str, detail: Any = None) -> dict:
    return {"error": {"code": code, "message": message, "detail": detail}}


class TokenBucket:
    """capacity tokens, refilled continuously over one minute."""

    def __init__(self, per_minute: int, now: Callable[[], float]):
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self.tokens = self.capacity
        self.now = now
        self.last = now()

    def take(self) -> float | None:
        """None when a token was taken, else seconds until one is available."""
        t = self.now()
        self.tokens = min(self.capacity, self.tokens + (t - self.last) * self.rate)
        self.last = t
        if self.tokens >= 1:
            self.tokens -= 1
            return None
        return (1 - self.tokens) / self.rate


class RateLimiter:
    """Token buckets per client address plus one overall bucket."""

    def __init__(self, per_ip: int, overall: int | None, now: Callable[[], float], max_ips: int = 5000):
        self.per_ip, self.now, self.max_ips = per_ip, now, max_ips
        self.overall = TokenBucket(overall, now) if overall else None
        self.buckets: dict[str, TokenBucket] = {}

    def check(self, ip: str) -> float | None:
        bucket = self.buckets.get(ip)
        if bucket is None:
            if len(self.buckets) >= self.max_ips:
                self.buckets.pop(next(iter(self.buckets)))
            bucket = self.buckets[ip] = TokenBucket(self.per_ip, self.now)
        wait = bucket.take()
        if wait is not None:
            return wait
        if self.overall is not None:
            wait = self.overall.take()
            if wait is not None:
                bucket.tokens = min(bucket.capacity, bucket.tokens + 1)  # refund the per-IP token
                return wait
        return None


def client_ip(request: Request, trust_proxy: bool) -> str:
    """The first X-Forwarded-For hop when the proxy is trusted (App Platform), else the socket peer."""
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def presented_key(headers: Any) -> str | None:
    key = headers.get("x-captain-key")
    if key:
        return key.strip()
    scheme, _, token = (headers.get("authorization") or "").partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


@dataclass
class Guard:
    """Captain's key plus the rate limiters, built once per app."""
    captain_key: str
    captain_configured: bool
    trust_proxy: bool
    limiters: dict[str, RateLimiter]
    on_limited: Callable[[str, str, float], None] | None = None

    @classmethod
    def build(cls, captain_key: str, captain_configured: bool, trust_proxy: bool, now: Callable[[], float],
              on_limited: Callable[[str, str, float], None] | None = None) -> Guard:
        limiters = {kind: RateLimiter(per_ip, overall, now) for kind, (per_ip, overall) in LIMITS.items()}
        return cls(captain_key, captain_configured, trust_proxy, limiters, on_limited)

    def is_captain(self, request: Request) -> bool:
        key = presented_key(request.headers)
        return bool(self.captain_configured and key and
                    hmac.compare_digest(key.encode(), self.captain_key.encode()))

    def limit(self, request: Request, kind: str) -> None:
        ip = client_ip(request, self.trust_proxy)
        wait = self.limiters[kind].check(ip)
        if wait is not None:
            retry = max(1, int(wait + 0.999))
            if self.on_limited:
                self.on_limited(kind, ip, retry)
            raise ApiError(429, "rate_limited", f"too many requests; try again in {retry} s",
                           {"retry_after": retry}, {"Retry-After": str(retry)})

    def require_captain(self, request: Request, kind: str = "mutate") -> None:
        if not self.captain_configured:
            raise ApiError(503, "captain_not_configured",
                           "the captain's key is not configured on this server; mutating routes are off")
        self.limit(request, kind)
        if not self.is_captain(request):
            raise ApiError(401, "captain_key_required", "needs the captain's key",
                           headers={"WWW-Authenticate": "Bearer"})


def guard_of(request: Request) -> Guard:
    return request.app.state.deps.guard


def captain(request: Request) -> None:
    """FastAPI dependency for every route of design section 4.2."""
    guard_of(request).require_captain(request)


class SecurityHeadersMiddleware:
    """Adds the CSP and friends to every HTTP response, streaming ones included."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                headers.extend((k, v) for k, v in SECURITY_HEADERS if k not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodyTooLarge(HTTPException):
    """An HTTPException so FastAPI's body parsing re-raises it instead of turning it into a 400."""

    def __init__(self) -> None:
        super().__init__(413, f"request bodies are limited to {MAX_BODY // 1024} KiB")


class BodyLimitMiddleware:
    """Rejects request bodies over MAX_BODY with 413, by Content-Length or while streaming."""

    def __init__(self, app: Any, limit: int = MAX_BODY):
        self.app, self.limit = app, limit

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers") or []:
            if name == b"content-length" and value.isdigit() and int(value) > self.limit:
                await self._reject(send)
                return
        seen = 0
        started = False

        async def counting_receive() -> dict:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    raise BodyTooLarge()
            return message

        async def tracking_send(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except BodyTooLarge:
            if not started:
                await self._reject(send)

    async def _reject(self, send: Callable) -> None:
        body = json.dumps(error_body("too_large", f"request bodies are limited to {self.limit // 1024} KiB")).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
