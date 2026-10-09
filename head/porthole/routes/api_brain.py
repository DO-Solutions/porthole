"""Brain routes: what the Brain is and its tools, asking a question, a session's events, and approvals.

Asking is public with a tight rate limit while the scripted deckhand answers; approving any action always needs
the captain's key. A session's events stream as SSE and the stream ends with the session."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from porthole.brain.deckhand import SUGGESTED
from porthole.brain.harness_runtime import NotConfigured
from porthole.security import ApiError, captain
from porthole.sse import SSE_HEADERS, format_event

router = APIRouter()


def _brain(request: Request) -> Any:
    brain = request.app.state.deps.brain
    if brain is None:
        raise ApiError(503, "brain_off", "the Brain is off on this server (PORTHOLE_BRAIN=off)")
    return brain


@router.get("/api/brain/info")
async def info(request: Request) -> dict:
    deps = request.app.state.deps
    brain = deps.brain
    label = {"deckhand": "scripted deckhand (phase 1)",
             "harness-runtime": f"Harness Runtime session {deps.settings.brain_session or '(not set)'}"}
    return {"backend": brain.name if brain else "off", "label": label.get(brain.name, "off") if brain else "off",
            "tools": [t.view() for t in deps.brain_tools.values()], "suggested": list(SUGGESTED)}


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=300)


@router.post("/api/brain/sessions")
async def ask(request: Request, body: Question) -> JSONResponse:
    deps = request.app.state.deps
    brain = _brain(request)
    if brain.name == "harness-runtime":
        deps.guard.require_captain(request, "brain")
    else:
        deps.guard.limit(request, "brain")
    fleet = deps.settings.fleet
    ctx = {"fleet": {"tentacles": [t.name for t in fleet.tentacles], "regions": list(fleet.regions)},
           "region_default": fleet.regions[0] if fleet.regions else "tor1",
           "tools": list(deps.brain_tools.values()),
           "actor": "captain" if deps.guard.is_captain(request) else "visitor"}
    try:
        session = await brain.start(body.question, ctx)
    except NotConfigured as e:
        raise ApiError(503, "brain_not_built", str(e)) from None
    return JSONResponse({"id": session["id"]}, status_code=202)


@router.get("/api/brain/sessions/{session_id}")
async def session(request: Request, session_id: str) -> dict:
    return _brain(request).snapshot(session_id)


@router.get("/api/brain/sessions/{session_id}/events")
async def session_events(request: Request, session_id: str, after: str | None = None) -> Response:
    brain = _brain(request)
    snap = brain.snapshot(session_id)
    after = after or request.headers.get("last-event-id")
    done = snap["state"] in ("done", "failed")
    if done and (not snap["events"] or after == snap["events"][-1]["id"]):
        return Response(status_code=204)  # nothing left: tells EventSource to stop reconnecting

    async def stream() -> AsyncIterator[str]:
        yield "retry: 3000\n\n"
        async for ev in brain.events(session_id, after):
            yield format_event(ev["type"], ev, ev["id"])

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


class Decision(BaseModel):
    decision: Literal["approve", "deny"]


@router.post("/api/brain/sessions/{session_id}/approvals/{approval_id}", dependencies=[Depends(captain)])
async def approval(request: Request, session_id: str, approval_id: str, body: Decision) -> dict:
    await _brain(request).approve(session_id, approval_id, body.decision, "captain")
    return {"ok": True, "decision": body.decision}
