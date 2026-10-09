"""Phase 2 placeholder: a Kraken's Brain backed by a DigitalOcean Harness Runtime session. Not built in this job;
every method raises NotConfigured so PORTHOLE_BRAIN=harness-runtime fails loudly instead of pretending.

The plan, from design section 11.5:

1. The head exposes its tools (brain/tools.py) as an MCP server at /mcp (Streamable HTTP, the `mcp` package,
   mounted into the FastAPI app, protected by PORTHOLE_MCP_KEY as the API-key credential Action Gateway stores).
   Read tools are `allow`; scenario_start, scenario_stop and voyage_start are `ask`.
2. watcher/brain/env.yaml declares the environment: agent claude-code, size mars-1vcpu-1gb, the porthole tool
   provider, permissions with the rules above and `default: deny`, egress limited to the head's hostname, the model
   key under secrets.
3. start() creates or reuses the session named in PORTHOLE_BRAIN_SESSION and sends the question as the prompt.
   events() tails the session's event history through the API (the operation `doctl harness-runtime logs` wraps)
   and maps each native event to a BrainEvent. approve() posts the decision to
   `<gateway origin>/approvals/<approval id>` with `Authorization: Bearer PORTHOLE_BRAIN_TOKEN`, `X-Session-Id` and
   `{"decision": "approve"}` or `"deny"`. That token must belong to the session's owner, which is a wider blast
   radius than insights:read (design section 12).
4. Nothing above touches the routes, the Brain page or the deckhand.

The REST shapes for creating a session, sending a prompt and reading events are not in a public reference page
yet (BUGS.md, "to verify"); the CLI proves the operations exist."""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from porthole.brain.adapter import BrainContext, BrainEvent, BrainSession


class NotConfigured(Exception):
    def __init__(self) -> None:
        super().__init__("the Harness Runtime brain is phase 2 and not built yet; set PORTHOLE_BRAIN=deckhand")


class HarnessRuntimeBrain:
    name = "harness-runtime"

    def __init__(self, session: str = "", gateway_url: str = "", token: str = "", **_: Any):
        self.session, self.gateway_url, self.token = session, gateway_url, token

    async def start(self, question: str, ctx: BrainContext) -> BrainSession:
        raise NotConfigured()

    async def events(self, session_id: str, after: str | None = None) -> AsyncIterator[BrainEvent]:
        raise NotConfigured()
        yield  # pragma: no cover

    async def approve(self, session_id: str, approval_id: str, decision: str, actor: str) -> None:
        raise NotConfigured()

    async def cancel(self, session_id: str) -> None:
        raise NotConfigured()

    async def get(self, session_id: str) -> BrainSession:
        raise NotConfigured()
