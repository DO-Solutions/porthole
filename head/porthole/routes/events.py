"""GET /events: the multiplexed Server-Sent Events stream every page opens once (design section 5.4).

Reconnects send Last-Event-ID and get the missed events replayed from the hub's ring."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from porthole.sse import SSE_HEADERS

router = APIRouter()


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    hub = request.app.state.deps.hub
    stream = hub.stream(request.headers.get("last-event-id"), is_disconnected=request.is_disconnected)
    return StreamingResponse(stream, media_type="text/event-stream", headers=SSE_HEADERS)
