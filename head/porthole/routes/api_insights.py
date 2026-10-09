"""Insights panel routes: catalog, labels, builder range and instant queries, alerts, logs, expected logs.

Reads are public and builder-only; raw PromQL, free-form log filters and rule pause/resume need the captain's key."""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from porthole.security import captain

router = APIRouter()


def _deps(request: Request) -> Any:
    return request.app.state.deps


@router.get("/api/insights/catalog")
async def catalog(request: Request, region: str | None = None) -> dict:
    return await _deps(request).panels.catalog(region)


@router.get("/api/insights/labels")
async def labels(request: Request, name: str, region: str | None = None, match: str | None = None) -> dict:
    return await _deps(request).panels.labels(region, name, match)


@router.get("/api/insights/range")
async def range_query(request: Request, metric: str, region: str | None = None,
                      filters: list[str] = Query(default=[]), agg: str | None = None,
                      range: str = "30m", step: str | None = None) -> dict:
    return await _deps(request).panels.range(region, metric, filters, agg, range, step)


@router.get("/api/insights/query")
async def instant_query(request: Request, metric: str, region: str | None = None,
                        filters: list[str] = Query(default=[]), agg: str | None = "avg") -> dict:
    return await _deps(request).panels.instant(region, metric, filters, agg)


@router.get("/api/insights/alerts")
async def alerts(request: Request) -> dict:
    return await _deps(request).alert_panels.overview()


@router.get("/api/insights/logs")
async def logs(request: Request, region: str | None = None, range: str = "1h", service: str | None = None,
               severity: str | None = None, cursor: str | None = Query(None, max_length=512),
               limit: int = 100, start: int | None = None, end: int | None = None) -> dict:
    return await _deps(request).log_panels.page(region, range, service, severity, cursor, limit, start, end)


@router.get("/api/insights/logs/expected")
async def logs_expected(request: Request, range: str = "1h") -> list[dict]:
    return await _deps(request).log_panels.expected(range)


@router.get("/api/dashboards/krakens-eye")
async def dashboard(request: Request) -> dict:
    deps = _deps(request)
    side = deps.panels.sidecar()
    path = deps.watcher_dir / "dashboards" / "krakens-eye.json"
    raw = path.read_text() if side is None and path.is_file() else None
    return {"sidecar": side, "file_url": "/watcher/dashboards/krakens-eye.json", "file_present": path.is_file(),
            "raw": raw, "dashboards_link": deps.links.link("insights.dashboards")}


@router.get("/api/dashboards/krakens-eye/run")
async def dashboard_run(request: Request, index: int, region: str | None = None, range: str = "1h") -> dict:
    return await _deps(request).panels.dashboard_run(index, region, range)


class PromqlBody(BaseModel):
    region: str | None = None
    query: str = Field(max_length=2000)
    range: str = "30m"
    step: str | None = None


@router.post("/api/insights/promql", dependencies=[Depends(captain)])
async def promql(request: Request, body: PromqlBody) -> dict:
    return await _deps(request).panels.promql_raw(body.region, body.query, body.range, body.step)


class LogSearchBody(BaseModel):
    region: str | None = None
    range: str = "1h"
    filter: dict | None = None
    limit: int = 100


@router.post("/api/insights/logs/search", dependencies=[Depends(captain)])
async def logs_search(request: Request, body: LogSearchBody) -> dict:
    return await _deps(request).log_panels.search(body.region, body.range, body.filter, body.limit)


class RuleStatusBody(BaseModel):
    status: Literal["paused", "active"]


@router.post("/api/insights/rules/{rule_id}/status", dependencies=[Depends(captain)])
async def rule_status(request: Request, rule_id: str, body: RuleStatusBody) -> dict:
    return await _deps(request).alert_panels.set_status(rule_id, body.status)
