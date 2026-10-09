"""GET /api/config, the first call of every page, and POST /api/captain/check for the key dialog.

The config carries the regions, every fleet member with its fixed color slot and control-panel link, the
feature flags, the deep links and the public caps, so pages never hard-code fleet facts."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from porthole.config import slot_palette
from porthole.security import captain

router = APIRouter()


def _entity(e: Any, links: Any) -> dict:
    link = links.for_entity(e)
    return {"name": e.name, "display": e.display, "kind": e.kind, "region": e.region, "urn": e.urn, "slot": e.slot,
            "service_name": e.service_name, "link": link["url"], "link_verified": link["verified"]}


def caps(deps: Any) -> dict:
    out: dict[str, Any] = {"max_charts": 6}
    for name in ("panels", "scenarios"):
        provider = getattr(deps, f"{name}_caps", None)
        if provider is not None:
            out.update(provider())
    return out


def build_config(deps: Any) -> dict:
    s, fleet, links = deps.settings, deps.settings.fleet, deps.links
    entities = [_entity(e, links) for e in fleet.entities()]
    by_key = {f"{e['kind']}:{e['name']}": e for e in entities}
    hook_auth = "+".join(k for k, v in (("bearer", s.hook_bearer), ("basic", s.hook_basic)) if v) or "none"
    return {
        "version": s.version,
        "regions": list(fleet.regions),
        "region_default": "both" if len(fleet.regions) > 1 else (fleet.regions[0] if fleet.regions else "tor1"),
        "fleet": {
            "project": fleet.project,
            "tentacles": [{**by_key[f"tentacle:{t.name}"], "peer": t.peer} for t in fleet.tentacles],
            "head": by_key.get(f"app:{fleet.head.name}") if fleet.head else None,
            "sea": {kind: {**by_key[f"{kind}:{spec.name}"],
                           **{k: v for k, v in spec.extra.items() if k in ("ip", "engine")}}
                    for kind, spec in fleet.sea.items()},
            "rules": [{"id": r.id, "purpose": r.purpose, "name": r.name, "target": r.target}
                      for r in fleet.watcher.rules],
            "dashboard": fleet.watcher.dashboard,
        },
        "entities": entities,
        "slots": {e.name: e.slot for e in fleet.entities() if e.slot},
        "palette": slot_palette(deps.watcher_dir),
        "features": {"write": s.insights_write, "brain": s.brain, "captain_configured": s.captain_configured,
                     "insights_configured": s.insights_configured, "hook_auth": hook_auth,
                     "hook_signature": bool(s.hook_secret), "otlp": deps.telemetry.describe()},
        "links": links.public(),
        "caps": caps(deps),
        "public_url": s.public_url,
        "hook_url": f"{s.public_url}/hooks/insights",
        "problems": list(s.problems),
    }


@router.get("/api/config")
async def config(request: Request) -> dict:
    return build_config(request.app.state.deps)


@router.post("/api/captain/check", status_code=204, dependencies=[Depends(captain)])
async def captain_check() -> Response:
    return Response(status_code=204)
