"""The head's own telemetry: its last 50 traces from the ring exporter and its last 300 log records.

The Traces page also lists chain runs across tentacles here, with their trace ids, for searching in Insights."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

router = APIRouter()


@router.get("/api/traces/own")
async def own_traces(request: Request, limit: int = Query(50, ge=1, le=50)) -> dict:
    deps = request.app.state.deps
    return {"traces": deps.telemetry.ring.traces(limit), "export": deps.telemetry.describe(),
            "service_name": deps.settings.service_name}


@router.get("/api/logs/own")
async def own_logs(request: Request, limit: int = Query(300, ge=1, le=300)) -> dict:
    deps = request.app.state.deps
    return {"records": deps.log.records(limit), "service_name": deps.settings.service_name}
