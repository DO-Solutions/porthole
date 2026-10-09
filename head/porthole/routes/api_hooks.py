"""Delivery routes: the last webhook deliveries (authenticated and rejected), and one delivery in full.

Headers come back with Authorization reduced to its scheme; bodies are shown as received."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from starlette.exceptions import HTTPException

router = APIRouter()


@router.get("/api/hooks/deliveries")
async def deliveries(request: Request, limit: int = Query(50, ge=1, le=250)) -> dict:
    deps = request.app.state.deps
    store = deps.hooks
    return {"deliveries": store.list(limit), "counts": {"authenticated": len(store.authenticated),
                                                        "rejected": len(store.rejected)},
            "hook_url": f"{deps.settings.public_url}/hooks/insights"}


@router.get("/api/hooks/deliveries/{delivery_id}")
async def delivery(request: Request, delivery_id: str) -> dict:
    rec = request.app.state.deps.hooks.get(delivery_id)
    if rec is None:
        raise HTTPException(404, f"no delivery {delivery_id} (the last 200 authenticated and 50 rejected are kept)")
    return rec
