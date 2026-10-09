"""GET /healthz: the App Platform health check and the one-curl deploy confirmation (version, uptime).

It answers 200 in degraded mode too, listing what is missing, so the page can still explain itself."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/healthz")
async def healthz(request: Request) -> dict:
    deps = request.app.state.deps
    s = deps.settings
    return {"status": "ok", "version": s.version, "uptime_s": round(deps.clock.monotonic() - deps.started, 1),
            "insights": "configured" if s.insights_configured else "missing",
            "fleet": {"tentacles": len(s.fleet.tentacles), "regions": list(s.fleet.regions)},
            "brain": s.brain, "problems": list(s.problems)}
