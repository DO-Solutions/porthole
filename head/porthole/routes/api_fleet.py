"""The fleet snapshot (tentacle health, Insights dots, links) and the merged scenario listing of every target.

Both come from the poller, which asks each tentacle every 10 s, so many visitors cost the tentacles nothing extra."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/api/fleet")
async def fleet(request: Request) -> dict:
    return await request.app.state.deps.poller.latest()


@router.get("/api/fleet/scenarios")
async def fleet_scenarios(request: Request) -> dict:
    poller = request.app.state.deps.poller
    await poller.listings_fresh()
    return poller.merged()
