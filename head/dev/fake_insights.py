"""The fake DigitalOcean Insights API: a FastAPI app serving the facts-pack shapes with moving synthetic series.

It backs the tests (mounted into the harness through an in-process transport) and runs as the insights-fake
service in docker-compose. Standalone: python head/dev/fake_insights.py, port 9000, fleet from
PORTHOLE_FLEET_JSON, FAKE_WATCH_TENTACLES=1 to follow real tentacles, FAKE_DROPLET_LOGS=1 to serve their logs,
FAKE_DROPLET_NAMES=1 to give Droplet series a resource_name (B-023: fresh Droplets have none)."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

HERE = Path(__file__).resolve().parent
for extra in (HERE, HERE.parent, HERE.parents[1] / "harness"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import httpx  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, Response  # noqa: E402

from fake_promql import PromError, SeriesModel, evaluate, parse, parse_duration, select  # noqa: E402
from fake_store import NOT_FOUND, FakeStore  # noqa: E402
from insights_harness import REGIONS  # noqa: E402
from porthole.clock import parse_iso  # noqa: E402
from porthole.config import Fleet  # noqa: E402

UNAUTHORIZED = {"id": "Unauthorized", "message": "Unable to authenticate you"}
DISCOVERY = "query processing exceeded the discovery time limit in query execution"
MAINTENANCE = ("<!DOCTYPE html><html><head><title>DigitalOcean - Maintenance</title></head>"
               "<body><h1>We'll be back soon</h1></body></html>")


def _fmt_t(t: float) -> float | int:
    return int(t) if t == int(t) else round(t, 3)


def _fmt_v(v: float) -> str:
    return "NaN" if v != v else format(v, ".10g")


def _time(raw: str | None, name: str, default: float | None = None) -> float:
    if raw in (None, ""):
        if default is None:
            raise PromError(400, "bad_data", f'invalid parameter "{name}": cannot parse "" to a valid timestamp')
        return default
    try:
        return float(raw)
    except ValueError:
        dt = parse_iso(raw)
        if dt is None:
            raise PromError(400, "bad_data",
                            f'invalid parameter "{name}": cannot parse "{raw}" to a valid timestamp') from None
        return dt.timestamp()


def in_process_transport(app: Any) -> httpx.MockTransport:
    """A sync transport that runs an ASGI app in process, so the synchronous harness can call the fake."""
    asgi = httpx.ASGITransport(app=app)

    async def call(request: httpx.Request) -> httpx.Response:
        clone = httpx.Request(request.method, request.url, headers=request.headers, content=request.content)
        resp = await asgi.handle_async_request(clone)
        body = await resp.aread()
        return httpx.Response(resp.status_code, headers=resp.headers, content=body)

    def handler(request: httpx.Request) -> httpx.Response:
        request.read()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(call(request))
        with concurrent.futures.ThreadPoolExecutor(1) as pool:  # called from inside a running loop
            return pool.submit(asyncio.run, call(request)).result()

    return httpx.MockTransport(handler)


class FakeInsights:
    def __init__(self, fleet: Fleet, world: Any = None, clock: Any = None, *, droplet_logs: bool = False,
                 droplet_names: bool = False, reject_metricless: bool = False,
                 public_url: str = "http://127.0.0.1:8080", hook_bearer: str = "", hook_secret: str = "",
                 head_logs: Any = None, on_notify: Any = None, rules_path: Path | None = None):
        self.fleet, self.clock, self.reject_metricless = fleet, clock, reject_metricless
        self.model = SeriesModel(fleet, world, now=lambda: self.now().timestamp(), droplet_names=droplet_names)
        self.store = FakeStore(fleet, self.model, self.now, public_url, hook_bearer, hook_secret, world,
                               droplet_logs, head_logs, rules_path, on_notify)
        self.requests: list[tuple[str, str]] = []
        self.app = self._build()

    def now(self) -> datetime:
        return self.clock.now() if self.clock is not None else datetime.now(timezone.utc)

    def tick(self) -> None:
        self.store.evaluate()

    def transport(self) -> httpx.MockTransport:
        return in_process_transport(self.app)

    def _parse(self, query: str | None) -> tuple:
        if not query:
            raise PromError(400, "bad_data", 'invalid parameter "query": parse error: no expression found in input')
        tree = parse(query)
        if self.reject_metricless and '{' in query and _has_bare_selector(tree):
            raise PromError(400, "bad_data", "vector selector must contain a metric name")  # invented text
        return tree

    def prom(self, region: str, endpoint: str, q: dict[str, list[str]]) -> dict:
        def one(key: str) -> str | None:
            return (q.get(key) or [None])[0]

        now = self.now().timestamp()
        if endpoint == "query":
            tree, t = self._parse(one("query")), _time(one("time"), "time", now)
            result = [{"metric": labels, "value": [_fmt_t(t), _fmt_v(v)]}
                      for labels, v in evaluate(self.model, region, tree, t)]
            return {"status": "success", "data": {"resultType": "vector", "result": result}}
        if endpoint == "query_range":
            tree = self._parse(one("query"))
            start, end = _time(one("start"), "start"), _time(one("end"), "end")
            step = parse_duration(one("step") or "")
            if step <= 0 or end < start:
                raise PromError(400, "bad_data", "invalid parameter: end must not be before start and step must be > 0")
            if (end - start) / step > 11_000:
                raise PromError(400, "bad_data", "exceeded maximum resolution of 11,000 points per timeseries. "
                                                 "Try decreasing the query resolution (?step=XX)")
            matrix: dict[tuple, dict] = {}
            t = start
            while t <= end + 1e-9:
                for labels, v in evaluate(self.model, region, tree, t):
                    key = tuple(sorted(labels.items()))
                    matrix.setdefault(key, {"metric": labels, "values": []})["values"].append([_fmt_t(t), _fmt_v(v)])
                t += step
            return {"status": "success", "data": {"resultType": "matrix", "result": list(matrix.values())}}
        if endpoint in ("labels", "series") or (endpoint.startswith("label/") and endpoint.endswith("/values")):
            if not one("start") or not one("end"):  # finding A1: discovery needs a window
                raise PromError(422, "execution", DISCOVERY)
            series = self.model.series(region)
            if q.get("match[]"):
                picked = {}
                for sel in q["match[]"]:
                    tree = self._parse(sel)
                    if tree[0] != "sel":
                        raise PromError(400, "bad_data", "match[] must be a series selector")
                    for s in select(self.model, region, tree[1], tree[2]):
                        picked[id(s)] = s
                series = list(picked.values())
            if endpoint == "labels":
                return {"status": "success", "data": sorted({k for s in series for k in s.labels})}
            if endpoint == "series":
                return {"status": "success", "data": [s.labels for s in series]}
            name = endpoint[len("label/"):-len("/values")]
            return {"status": "success", "data": sorted({s.labels[name] for s in series if name in s.labels})}
        raise PromError(404, "not_found", f"unknown endpoint {endpoint}")

    def _build(self) -> FastAPI:
        app = FastAPI(title="fake insights", docs_url=None, redoc_url=None, openapi_url=None)
        store = self.store

        def denied(request: Request) -> Response | None:
            self.requests.append((request.method, request.url.path))
            auth = request.headers.get("authorization", "")
            if not auth.lower().startswith("bearer ") or len(auth) < 12:
                return JSONResponse(UNAUTHORIZED, 401)
            return None

        def region_error(region: str) -> Response | None:
            if region == "mkc1":  # finding A3: the maintenance page, as HTML with 404
                return HTMLResponse(MAINTENANCE, 404)
            if region not in REGIONS:
                return JSONResponse(NOT_FOUND, 404)
            return None

        @app.api_route("/v2/insights/query/{region}/prom/api/v1/{endpoint:path}", methods=["GET", "POST"])
        async def prom(region: str, endpoint: str, request: Request) -> Response:
            if (err := denied(request) or region_error(region)) is not None:
                return err
            q = parse_qs(request.url.query, keep_blank_values=True)
            if request.method == "POST" and "form" in request.headers.get("content-type", ""):
                for k, v in parse_qs((await request.body()).decode(), keep_blank_values=True).items():
                    q.setdefault(k, []).extend(v)
            try:
                return JSONResponse(self.prom(region, endpoint, q))
            except PromError as e:
                return JSONResponse({"status": "error", "errorType": e.error_type, "error": e.message}, e.status)

        @app.post("/v2/insights/query/{region}/logs/search")
        async def logs(region: str, request: Request) -> Response:
            if (err := denied(request) or region_error(region)) is not None:
                return err
            raw = await request.body()
            try:
                body = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                return JSONResponse({"error": "invalid character in request body", "code": 3}, 400)
            status, payload = store.search_logs(region, body)
            return JSONResponse(payload, status)

        @app.get("/v2/insights/alert-rules")
        async def list_rules(request: Request) -> Response:
            if (err := denied(request)) is not None:
                return err
            per = int(request.query_params.get("per_page") or 20)
            return JSONResponse({"alert_rules": [], "pagination": {"page": 1, "pages": 1, "per_page": per}})

        @app.api_route("/v2/insights/alert-rules/{rid}", methods=["GET", "PUT", "DELETE"])
        async def rule(rid: str, request: Request) -> Response:
            if (err := denied(request)) is not None:
                return err
            if request.method == "PUT":
                status, payload = store.update_rule(rid, await _json_body(request))
                return JSONResponse(payload, status)
            with store.lock:
                found = store.rules.get(rid)
                if found and request.method == "DELETE":
                    del store.rules[rid]
                    return Response(status_code=204)
            return JSONResponse({"alert_rule": found} if found else NOT_FOUND, 200 if found else 404)

        @app.post("/v2/insights/alert-rules")
        async def create_rule(request: Request) -> Response:
            if (err := denied(request)) is not None:
                return err
            status, payload = store.create_rule(await _json_body(request))
            return JSONResponse(payload, status)

        @app.get("/v2/insights/alert-instances")
        async def instances(request: Request) -> Response:
            if (err := denied(request)) is not None:
                return err
            store.evaluate()
            return JSONResponse(store.list_instances(dict(request.query_params)))

        @app.get("/v2/insights/alert-instances/{iid}")
        async def instance(iid: str, request: Request) -> Response:
            if (err := denied(request)) is not None:
                return err
            found = next((i for i in store.instances if i["id"] == iid), None)
            if found is None:
                return JSONResponse(NOT_FOUND, 404)
            return JSONResponse({"alert_instance": {k: v for k, v in found.items() if not k.startswith("_")}})

        @app.api_route("/v2/insights/notification-channels", methods=["GET", "POST"])
        async def channels(request: Request) -> Response:
            if (err := denied(request)) is not None:
                return err
            if request.method == "POST":
                status, payload = _create_channel(store, await _json_body(request))
                return JSONResponse(payload, status)
            items = list(store.channels.values())
            return JSONResponse({"notification_channels": items,
                                 "pagination": {"page": 1, "pages": 1, "per_page": 20, "total": len(items)}})

        @app.api_route("/v2/insights/notification-channels/{cid}", methods=["GET", "DELETE"])
        async def channel(cid: str, request: Request) -> Response:
            if (err := denied(request)) is not None:
                return err
            found = store.channels.get(cid)
            if found and request.method == "DELETE":
                if found["usage"]["rule_count"]:  # status code invented; the docs say only "cannot delete"
                    return JSONResponse({"id": "conflict", "message": "You cannot delete a notification channel "
                                                                      "that is used by an alert rule."}, 409)
                del store.channels[cid]
                return Response(status_code=204)
            return JSONResponse({"notification_channel": found} if found else NOT_FOUND, 200 if found else 404)

        return app


def _has_bare_selector(tree: Any) -> bool:
    if isinstance(tree, tuple):
        if tree and tree[0] == "sel" and tree[1] is None:
            return True
        return any(_has_bare_selector(x) for x in tree)
    if isinstance(tree, list):
        return any(_has_bare_selector(x) for x in tree)
    return False


async def _json_body(request: Request) -> Any:
    try:
        return json.loads(await request.body() or b"{}")
    except json.JSONDecodeError:
        return None


def _create_channel(store: FakeStore, body: Any) -> tuple[int, dict]:
    if not isinstance(body, dict) or not body.get("name"):
        return 400, {"id": "bad_request", "message": "name is required"}
    kinds = [k for k in ("email", "slack", "webhook") if body.get(k)]
    if len(kinds) != 1:
        return 400, {"id": "bad_request", "message": "set exactly one of email, slack or webhook"}
    cfg = dict(body[kinds[0]])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    for secret_field, inner in (("bearer_token", "token"), ("basic_auth", "password"), ("signature", "secret")):
        if secret_field in cfg:
            kept = {k: v for k, v in cfg.pop(secret_field).items() if k != inner}
            cfg[f"{secret_field}_status"] = {**kept, "is_set": True, "updated_at": now}
    if "webhook_url" in cfg:
        cfg.pop("webhook_url")
        cfg["webhook_url_status"] = {"is_set": True, "updated_at": now}
    cid = str(uuid.uuid4())
    store.channels[cid] = {"id": cid, "name": body["name"], "channel_type": f"CHANNEL_TYPE_{kinds[0].upper()}",
                           "created_at": now, kinds[0]: cfg, "usage": {"rule_count": 0}}
    return 201, {"notification_channel": store.channels[cid]}


class TentacleWatcher:
    """A world built by polling real tentacles' open /scenarios endpoint (docker-compose mode)."""

    def __init__(self, fleet: Fleet):
        self.fleet = fleet
        self._runs: dict[str, list[dict]] = {}

    def poll(self) -> None:
        for t in self.fleet.tentacles:
            try:
                body = httpx.get(f"{t.url}/scenarios", timeout=3).json()
            except (httpx.HTTPError, ValueError):
                continue
            views = body.get("running", []) + body.get("finished", [])
            self._runs[t.name] = [{**r, "started_at": parse_iso(r["started_at"]),
                                   "ended_at": parse_iso(r.get("ended_at"))} for r in views]

    def runs(self, name: str) -> list[dict]:
        return list(self._runs.get(name, []))

    def runs_any(self, scenario: str) -> list[dict]:
        return [r for runs in self._runs.values() for r in runs if r["name"] == scenario]

    def requests(self, kind: str, t0: float, t1: float) -> int:
        return 0

    def head_rate(self, t: float) -> float:
        return 0.0


def main() -> None:
    import uvicorn
    fleet = Fleet.from_json(os.environ.get("PORTHOLE_FLEET_JSON"))
    world = TentacleWatcher(fleet) if os.environ.get("FAKE_WATCH_TENTACLES") == "1" else None
    hook_url = os.environ.get("FAKE_HOOK_URL", "")

    def deliver(d: dict) -> None:
        threading.Thread(target=lambda: _post(hook_url or d["url"], d), daemon=True).start()

    fake = FakeInsights(fleet, world, droplet_logs=os.environ.get("FAKE_DROPLET_LOGS") == "1",
                        droplet_names=os.environ.get("FAKE_DROPLET_NAMES") == "1",
                        public_url=os.environ.get("PORTHOLE_PUBLIC_URL", "http://127.0.0.1:8080"),
                        hook_bearer=os.environ.get("PORTHOLE_HOOK_BEARER", ""),
                        hook_secret=os.environ.get("PORTHOLE_HOOK_SECRET", ""), on_notify=deliver)

    def ticker() -> None:
        while True:
            if world is not None:
                world.poll()
            fake.tick()
            time.sleep(10)

    threading.Thread(target=ticker, daemon=True).start()
    uvicorn.run(fake.app, host="0.0.0.0", port=int(os.environ.get("FAKE_PORT", "9000")), log_level="warning")


def _post(url: str, delivery: dict) -> None:
    try:
        httpx.post(url, content=delivery["body"], headers=delivery["headers"], timeout=5)
    except httpx.HTTPError:
        pass


if __name__ == "__main__":
    main()
