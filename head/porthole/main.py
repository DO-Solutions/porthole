"""The FastAPI app factory for the head, and the module-level `app` that uvicorn serves with one worker.

create_app() takes optional transports and a clock so tests and the local runner can wire in the fakes;
`app` is created on first access so importing this module has no side effects."""
from __future__ import annotations

import sys
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

try:
    from insights_harness import InsightsError, excerpt
except ModuleNotFoundError:  # a checkout rather than the container: the harness sits next to head/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "harness"))
    from insights_harness import InsightsError, excerpt
from porthole import routes
from porthole.cache import BudgetExhausted
from porthole.clock import Clock
from porthole.config import Settings
from porthole.deps import Deps, build_services
from porthole.jsonlog import JsonLog, install_stdlib_bridge
from porthole.security import ApiError, BodyLimitMiddleware, SecurityHeadersMiddleware, error_body
from porthole.telemetry import instrument_app

CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
         409: "conflict", 413: "too_large", 429: "rate_limited", 502: "bad_gateway", 503: "unavailable"}


class RequestLogMiddleware:
    """One log line per API or webhook request; pages, assets, health and event streams stay quiet."""

    def __init__(self, app: Any, log: JsonLog):
        self.app, self.log = app, log

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(("/api/", "/hooks/")) or path.endswith("/events"):
            await self.app(scope, receive, send)
            return
        t0, status = time.monotonic(), 500

        async def capture(message: dict) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            ms = round((time.monotonic() - t0) * 1000, 1)
            severity = "WARN" if status >= 500 else "INFO"
            self.log(severity, f"{scope.get('method')} {path} {status} in {ms} ms",
                     **{"http.method": scope.get("method"), "http.route": path, "http.status_code": status,
                        "duration_ms": ms})


def _handlers(app: FastAPI, deps: Deps) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.body(), exc.status, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        detail = [{"loc": [str(x) for x in e.get("loc", [])], "msg": e.get("msg")} for e in exc.errors()]
        return JSONResponse(error_body("bad_request", "the request is not valid", detail), 400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(error_body(CODES.get(exc.status_code, "error"), str(exc.detail)), exc.status_code,
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(BudgetExhausted)
    async def budget(request: Request, exc: BudgetExhausted) -> JSONResponse:
        retry = max(1, int(exc.retry_in + 0.999))
        return JSONResponse(error_body("upstream_budget", "the Insights call budget for this minute is used up",
                                       {"retry_in": retry}), 503, headers={"Retry-After": str(retry)})

    @app.exception_handler(InsightsError)
    async def insights_error(request: Request, exc: InsightsError) -> JSONResponse:
        detail = {"status": exc.status, "body": excerpt(exc.body), "request": exc.request_summary}
        return JSONResponse(error_body("insights_error", f"Insights returned HTTP {exc.status}", detail), 502)

    @app.exception_handler(httpx.HTTPError)
    async def upstream_down(request: Request, exc: httpx.HTTPError) -> JSONResponse:
        return JSONResponse(error_body("upstream_unreachable", f"{type(exc).__name__}: {exc}"), 502)

    @app.exception_handler(Exception)
    async def crash(request: Request, exc: Exception) -> JSONResponse:
        deps.log.error(f"unhandled {type(exc).__name__}: {exc}", **{"http.route": request.url.path})
        return JSONResponse(error_body("internal_error", "something went wrong; the log has the details"), 500)


def create_app(settings: Settings | None = None, insights_transport: httpx.BaseTransport | None = None,
               tentacle_transport: httpx.AsyncBaseTransport | None = None, clock: Clock | None = None, *,
               span_exporter: Any = None, log_stream: Any = None) -> FastAPI:
    settings = settings or Settings.from_env()
    deps = Deps(settings, clock, insights_transport=insights_transport, tentacle_transport=tentacle_transport,
                span_exporter=span_exporter, log_stream=log_stream)
    install_stdlib_bridge(deps.log)
    build_services(deps)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await deps.start()
        try:
            yield
        finally:
            await deps.stop()

    app = FastAPI(title="Porthole", version=settings.version, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.deps = deps
    _handlers(app, deps)
    for router in routes.ROUTERS:
        app.include_router(router)
    app.mount("/static", StaticFiles(directory=deps.static_dir), name="static")
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(RequestLogMiddleware, log=deps.log)
    app.add_middleware(SecurityHeadersMiddleware)
    instrument_app(app, deps.telemetry)
    return app


_app: FastAPI | None = None


def __getattr__(name: str) -> Any:
    """`porthole.main:app` for uvicorn, built from the environment on first access."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)


def main() -> None:
    import uvicorn
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, workers=1, access_log=False,
                log_config=None)


if __name__ == "__main__":
    main()
