"""The Brain adapter protocol and its record types, exactly as design section 11.1 defines them.

The routes talk only to BrainAdapter; PORTHOLE_BRAIN picks the implementation at startup."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, TypedDict

EventType = Literal["status", "thinking", "message", "tool_call", "tool_result",
                    "approval_request", "approval_resolved", "error", "done"]
SessionState = Literal["starting", "running", "waiting_approval", "done", "failed"]
EVENT_TYPES: tuple[str, ...] = ("status", "thinking", "message", "tool_call", "tool_result", "approval_request",
                                "approval_resolved", "error", "done")


class BrainEvent(TypedDict):
    id: str  # monotonic within the session
    t: str  # ISO 8601 UTC
    type: EventType
    data: dict


class BrainSession(TypedDict):
    id: str
    question: str
    created_at: str
    state: SessionState
    backend: str  # "deckhand" or "harness-runtime"


class BrainContext(TypedDict):
    fleet: dict  # the fleet summary
    region_default: str
    tools: list[Any]  # the Tool definitions of brain/tools.py
    actor: str  # "visitor" or "captain"


class BrainAdapter(Protocol):
    name: str

    async def start(self, question: str, ctx: BrainContext) -> BrainSession: ...

    def events(self, session_id: str, after: str | None = None) -> AsyncIterator[BrainEvent]: ...

    async def approve(self, session_id: str, approval_id: str, decision: Literal["approve", "deny"],
                      actor: str) -> None: ...

    async def cancel(self, session_id: str) -> None: ...

    async def get(self, session_id: str) -> BrainSession: ...
