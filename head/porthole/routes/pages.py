"""The nine static pages at clean paths, the favicon, and the committed dashboard file.

Pages are plain HTML files from head/static; the browser fetches data from the JSON API afterwards."""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response
from starlette.exceptions import HTTPException

PAGES = {"/": "index.html", "/stir": "stir.html", "/metrics": "metrics.html", "/dashboards": "dashboards.html",
         "/alerts": "alerts.html", "/logs": "logs.html", "/traces": "traces.html", "/api": "api.html",
         "/brain": "brain.html"}

router = APIRouter()


def _page(filename: str) -> Callable[[Request], Awaitable[Response]]:
    async def serve(request: Request) -> Response:
        path = request.app.state.deps.static_dir / filename
        if not path.is_file():
            raise HTTPException(404, f"{filename} is not built")
        return FileResponse(path, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})
    serve.__name__ = f"page_{filename.split('.')[0]}"
    return serve


for _route, _file in PAGES.items():
    router.add_api_route(_route, _page(_file), methods=["GET"], include_in_schema=False)


@router.get("/favicon.ico", include_in_schema=False)
async def favicon(request: Request) -> Response:
    return FileResponse(request.app.state.deps.static_dir / "img" / "favicon.svg", media_type="image/svg+xml")


@router.get("/watcher/dashboards/krakens-eye.json", include_in_schema=False)
async def dashboard_file(request: Request) -> Response:
    path = request.app.state.deps.watcher_dir / "dashboards" / "krakens-eye.json"
    if not path.is_file():
        raise HTTPException(404, "the dashboard file is not in this build")
    return FileResponse(path, media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="krakens-eye.json"'})
