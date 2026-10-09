"""The tentacles as the head sees them: an HTTP client per tentacle and the fleet poller behind the Bridge.

The poller asks every tentacle for /health and /scenarios every 10 s, adds whether Insights has data for each fleet
member, publishes a fleet event, and a scenario event whenever a run finishes. Routine polls are kept out of the
API trace unless they fail, so the trace stays about Insights."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from porthole.apitrace import caller, traced_request
from porthole.clock import iso
from porthole.config import TentacleSpec

POLL_S = 10.0
ERROR_RECORD_EVERY_S = 60.0


class TentacleError(Exception):
    def __init__(self, status: int, message: str, detail: Any = None):
        super().__init__(message)
        self.status, self.message, self.detail = status, message, detail


class TentacleClient:
    def __init__(self, spec: TentacleSpec, http: httpx.AsyncClient, key: str, trace: Any):
        self.spec, self.http, self.key, self.trace = spec, http, key, trace

    async def _call(self, method: str, path: str, params: dict | None = None, record: bool = True,
                    timeout: float | None = None, record_errors: bool = True) -> Any:
        headers = {"Authorization": f"Bearer {self.key}"} if method != "GET" and self.key else {}
        kwargs: dict[str, Any] = {"headers": headers, "params": params}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            resp = await traced_request(self.http, self.trace, target="tentacle", method=method,
                                        url=f"{self.spec.url}{path}", entity=self.spec.name, record=record,
                                        record_errors=record_errors, **kwargs)
        except httpx.HTTPError as e:
            raise TentacleError(502, f"{self.spec.display} is unreachable: {type(e).__name__}") from e
        try:
            body = resp.json()
        except ValueError:
            body = {"detail": resp.text[:300]}
        if resp.is_error:
            detail = body.get("detail") if isinstance(body, dict) else body
            raise TentacleError(resp.status_code, detail if isinstance(detail, str) else f"HTTP {resp.status_code}",
                                detail)
        return body

    async def health(self, record: bool = False, record_errors: bool = True) -> dict:
        return await self._call("GET", "/health", record=record, timeout=5, record_errors=record_errors)

    async def scenarios(self, record: bool = False, record_errors: bool = True) -> dict:
        return await self._call("GET", "/scenarios", record=record, timeout=5, record_errors=record_errors)

    async def start(self, name: str, params: dict) -> dict:
        return await self._call("POST", f"/scenario/{name}", params=params)

    async def stop(self, run_id: str) -> dict:
        return await self._call("POST", f"/scenario/stop/{run_id}", timeout=15)


class FleetPoller:
    def __init__(self, deps: Any, interval: float = POLL_S):
        self.deps = deps
        self.interval = interval
        self.fleet = deps.settings.fleet
        self.clients = {t.name: TentacleClient(t, deps.http, deps.settings.tentacle_key, deps.trace)
                        for t in self.fleet.tentacles}
        self.snapshot: dict | None = None
        self.listings: dict[str, dict] = {}
        self.polled_at: float | None = None
        self._lock = asyncio.Lock()
        # when each unreachable tentacle last had a failed poll written to the API trace; the first failure is
        # recorded, then one a minute, so a tentacle that is down for an hour does not fill the ring by itself
        self._error_recorded_at: dict[str, float] = {}

    def client(self, name: str) -> TentacleClient | None:
        t = self.fleet.tentacle(name)
        return self.clients.get(t.name) if t else None

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # the poller must survive anything one cycle throws
                self.deps.log.warn(f"fleet poll failed: {type(e).__name__}: {e}")
            await self.deps.clock.sleep(self.interval)

    async def _poll_tentacle(self, t: TentacleSpec) -> dict:
        client = self.clients[t.name]
        now = self.deps.clock.monotonic()
        last = self._error_recorded_at.get(t.name)
        record_errors = last is None or now - last >= ERROR_RECORD_EVERY_S
        with caller("poller"):
            try:
                health, listing = await asyncio.gather(client.health(record_errors=record_errors),
                                                       client.scenarios(record_errors=False))
            except TentacleError as e:
                if record_errors:
                    self._error_recorded_at[t.name] = now
                return {"reachable": False, "error": e.message, "health": None, "listing": None}
        self._error_recorded_at.pop(t.name, None)
        return {"reachable": True, "error": None, "health": health, "listing": listing}

    async def poll_once(self) -> dict:
        async with self._lock:
            results = await asyncio.gather(*(self._poll_tentacle(t) for t in self.fleet.tentacles))
            checked = iso(self.deps.clock.now())
            for t, res in zip(self.fleet.tentacles, results, strict=True):
                if res["listing"] is not None:
                    self._announce_finished(t.name, res["listing"])
                    self.listings[t.name] = {**res["listing"], "checked_at": checked}
            probes = await self.deps.panels.probes() if getattr(self.deps, "panels", None) else {"entities": {}}
            self.snapshot = self._build(results, probes, checked)
            self.polled_at = self.deps.clock.monotonic()
            self.deps.hub.publish("fleet", self.snapshot)
            return self.snapshot

    def _announce_finished(self, name: str, listing: dict) -> None:
        before = {r["id"] for r in (self.listings.get(name) or {}).get("running") or []}
        for run in listing.get("finished") or []:
            if run.get("id") in before:
                self.deps.hub.publish("scenario", {"target": name, "run": run, "event": run.get("status")})

    def _build(self, results: list[dict], probes: dict, checked: str) -> dict:
        seen = probes.get("entities") or {}
        links = self.deps.links
        tentacles = []
        for t, res in zip(self.fleet.tentacles, results, strict=True):
            e = self.fleet.entity(t.name)
            probe = seen.get(f"tentacle:{t.name}", {})
            link = links.for_entity(e) if e else {"url": None, "verified": False}
            health = res["health"] or {}
            tentacles.append({
                "name": t.name, "display": t.display, "region": t.region, "slot": t.slot, "reachable": res["reachable"],
                "error": res["error"],
                "health": {k: health.get(k) for k in ("uptime_s", "load1", "mem_pct")} if res["health"] else None,
                "running": [r.get("id") for r in health.get("running") or []],
                "seen_in_insights": probe.get("seen"), "seen_reason": probe.get("reason"),
                "seen_checked_at": probes.get("checked_at"), "link": link["url"], "link_verified": link["verified"],
                "checked_at": checked})
        head = None
        if self.fleet.head:
            e = self.fleet.entity(self.fleet.head.name)
            link = links.for_entity(e) if e else {"url": None, "verified": False}
            probe = seen.get(f"app:{self.fleet.head.name}", {})
            head = {"name": self.fleet.head.name, "urn": self.fleet.head.urn, "region": self.fleet.head.region,
                    "seen_in_insights": probe.get("seen"), "seen_reason": probe.get("reason"),
                    "link": link["url"], "link_verified": link["verified"]}
        sea = {}
        for kind, spec in self.fleet.sea.items():
            e = next((x for x in self.fleet.entities() if x.kind == kind and x.name == spec.name), None)
            link = links.for_entity(e) if e else {"url": None, "verified": False}
            probe = seen.get(f"{kind}:{spec.name}", {})
            sea[kind] = {"name": spec.name, "region": spec.region, "slot": spec.slot,
                         "seen_in_insights": probe.get("seen"), "seen_reason": probe.get("reason"),
                         "link": link["url"], "link_verified": link["verified"]}
        head_runs = self.deps.scenarios.head_view() if getattr(self.deps, "scenarios", None) else []
        return {"checked_at": checked, "tentacles": tentacles, "head": head, "sea": sea, "head_runs": head_runs,
                "probe_mode": probes.get("mode"), "probe_errors": probes.get("errors") or []}

    async def latest(self) -> dict:
        if self.snapshot is None:
            return await self.poll_once()
        return self.snapshot

    async def listings_fresh(self, max_age: float = 15.0) -> dict[str, dict]:
        if self.polled_at is None or self.deps.clock.monotonic() - self.polled_at > max_age:
            await self.poll_once()
        return self.listings

    async def refresh(self, name: str) -> None:
        """Re-read one tentacle's listing right after a start or stop."""
        client = self.client(name)
        if client is None:
            return
        try:
            listing = await client.scenarios()
        except TentacleError:
            return
        self._announce_finished(client.spec.name, listing)
        self.listings[client.spec.name] = {**listing, "checked_at": iso(self.deps.clock.now())}

    def merged(self) -> dict:
        running, finished = [], []
        for name, listing in self.listings.items():
            running += [{**r, "target": name} for r in listing.get("running") or []]
            finished += [{**r, "target": name} for r in listing.get("finished") or []]
        if getattr(self.deps, "scenarios", None):
            for run in self.deps.scenarios.head_view():
                (running if run["status"] == "running" else finished).append(run)
        finished.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        running.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return {"running": running, "finished": finished[:50]}
