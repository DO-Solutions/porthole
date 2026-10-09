"""The API trace routes: the ring of upstream calls, and one call with its curl line and BUGS.md entry.

Curl output carries $DIGITALOCEAN_TOKEN, never the token; bodies are the harness's redacted copies."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from starlette.exceptions import HTTPException

from porthole.apitrace import as_bugs_md, as_curl

router = APIRouter()


@router.get("/api/trace")
async def trace_list(request: Request, limit: int = Query(50, ge=1, le=500), target: str | None = None,
                     q: str | None = Query(None, max_length=200)) -> dict:
    trace = request.app.state.deps.trace
    return {"calls": trace.list(limit, target, q), "stats": trace.stats()}


@router.get("/api/trace/{call_id}")
async def trace_one(call_id: str, request: Request) -> dict:
    deps = request.app.state.deps
    call = deps.trace.get(call_id)
    if call is None:
        raise HTTPException(404, f"no call {call_id} in the ring (it keeps the last 500)")
    curl = as_curl(call, deps.settings.insights_base_url)
    return {**call.full(), "curl": curl, "bugs_md": as_bugs_md(call, curl)}
