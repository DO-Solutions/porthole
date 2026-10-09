"""Voyage routes: the catalog and recent runs (public), one run with its timeline, start and abort (captain).

Only one voyage sails at a time; a second start answers 409 with the id of the one sailing."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from porthole.security import captain

router = APIRouter()


@router.get("/api/voyages")
async def voyages(request: Request) -> dict:
    engine = request.app.state.deps.voyages
    return {"voyages": engine.catalog_view(), "runs": engine.list_runs(),
            "active_run_id": engine.active.id if engine.active else None}


@router.get("/api/voyages/{run_id}")
async def voyage(request: Request, run_id: str) -> dict:
    return request.app.state.deps.voyages.get(run_id).view()


class StartBody(BaseModel):
    voyage: str = Field(max_length=40)
    params: dict = Field(default_factory=dict)


@router.post("/api/voyages/start", dependencies=[Depends(captain)])
async def start(request: Request, body: StartBody) -> JSONResponse:
    run = await request.app.state.deps.voyages.start(body.voyage, body.params)
    return JSONResponse({"run_id": run.id}, status_code=202)


@router.post("/api/voyages/{run_id}/abort", dependencies=[Depends(captain)])
async def abort(request: Request, run_id: str) -> dict:
    run = await request.app.state.deps.voyages.abort(run_id)
    return run.view()
