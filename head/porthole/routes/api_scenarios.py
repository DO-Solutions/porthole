"""Scenario routes: the catalog with its caps (public), and start and stop (captain's key).

Parameters are validated against the catalog and clamped to the public caps before any tentacle sees them."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from porthole.security import captain

router = APIRouter()


@router.get("/api/scenarios/catalog")
async def catalog(request: Request) -> dict:
    deps = request.app.state.deps
    targets = [t.name for t in deps.settings.fleet.tentacles] + ["head"]
    return {"scenarios": deps.scenarios.catalog(), "targets": targets}


class StartBody(BaseModel):
    target: str = Field(max_length=100)
    scenario: str = Field(max_length=40)
    params: dict = Field(default_factory=dict)


class StopBody(BaseModel):
    target: str = Field(max_length=100)
    run_id: str = Field(max_length=100)


@router.post("/api/scenarios/start", dependencies=[Depends(captain)])
async def start(request: Request, body: StartBody) -> JSONResponse:
    view = await request.app.state.deps.scenarios.start(body.target, body.scenario, body.params)
    return JSONResponse(view, status_code=202)


@router.post("/api/scenarios/stop", dependencies=[Depends(captain)])
async def stop(request: Request, body: StopBody) -> dict:
    return await request.app.state.deps.scenarios.stop(body.target, body.run_id)
