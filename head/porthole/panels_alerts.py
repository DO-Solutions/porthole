"""The alerts overview: rules fetched by id from the fleet description, their instances, and the channels.

Rules are never discovered by listing, because the list omits rules created in the control panel (finding A2).
Pausing and resuming needs write mode and works only on the fleet's own rules."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from insights_harness import (
    RE_ALERT,
    RULE_STATUSES,
    THRESHOLD_OPERATORS,
    WINDOWS,
    InsightsError,
)
from porthole.config import RuleRef
from porthole.panels import Panels, error_info
from porthole.security import ApiError

OPS = {v: k for k, v in THRESHOLD_OPERATORS.items() if not k.isalpha() and k != "=="}
WINDOW_SHORT = {v: k for k, v in WINDOWS.items()}
RE_ALERT_SHORT = {v: k for k, v in RE_ALERT.items()}
STATUS_SHORT = {v: k for k, v in RULE_STATUSES.items()}


def rule_view(rule: dict, ref: RuleRef) -> dict:
    spec = rule.get("spec") or {}
    query, th = spec.get("query") or {}, spec.get("thresholds") or {}
    return {"id": rule.get("id", ref.id), "name": spec.get("name") or ref.name, "purpose": ref.purpose,
            "target": ref.target, "metric": query.get("metric"), "resource_urns": query.get("resource_urns") or [],
            "tags": query.get("tags") or [], "operator": OPS.get(th.get("operator"), th.get("operator")),
            "warning": th.get("warning"), "critical": th.get("critical"),
            "window": WINDOW_SHORT.get((spec.get("condition") or {}).get("window"), "5m"),
            "re_alert": RE_ALERT_SHORT.get(spec.get("re_alert_duration"), spec.get("re_alert_duration")),
            "status": STATUS_SHORT.get(rule.get("status"), rule.get("status")),
            "channels": [{"id": b.get("notification_channel_id"),
                          "notify_on": [s.replace("SEVERITY_", "").lower() for s in b.get("notify_on") or []]}
                         for b in spec.get("notification_channels") or []],
            "error": None}


def instance_view(inst: dict, rules: dict[str, dict], fleet: Any) -> dict:
    urn = inst.get("resource_urn")
    who = next((e.display for e in fleet.entities() if urn and e.urn == urn), urn)
    return {"id": inst.get("id"), "rule_id": inst.get("rule_id"),
            "rule_name": (rules.get(inst.get("rule_id")) or {}).get("name"),
            "severity": str(inst.get("severity") or "").replace("SEVERITY_", "").lower(),
            "status": str(inst.get("status") or "").replace("ALERT_INSTANCE_STATUS_", "").lower(),
            "resource_urn": urn, "entity": who, "value": inst.get("value"),
            "triggered_at": inst.get("triggered_at"), "resolved_at": inst.get("resolved_at"),
            "muted": inst.get("muted")}


def channel_view(ch: dict, public_url: str) -> dict:
    kind = str(ch.get("channel_type") or "").replace("CHANNEL_TYPE_", "").lower() or next(
        (k for k in ("webhook", "email", "slack") if k in ch), "unknown")
    cfg = ch.get(kind) or {}
    target = cfg.get("url") or cfg.get("to") or cfg.get("channel") or ""
    return {"id": ch.get("id"), "name": ch.get("name"), "type": kind, "target": target,
            "statuses": {k: v for k, v in cfg.items() if k.endswith("_status")},
            "headers": cfg.get("headers") or {}, "rule_count": (ch.get("usage") or {}).get("rule_count"),
            "created_at": ch.get("created_at"),
            "points_here": bool(public_url and str(target).startswith(public_url))}


class AlertPanels:
    def __init__(self, panels: Panels):
        self.p = panels
        self.deps = panels.deps

    async def overview(self) -> dict:
        res = await self.deps.cache.get("alerts", self._fetch, self.p.ttl)
        return {**res.value, "fetched_at": res.fetched_at, "stale": res.stale, "cached": res.cached,
                "write": self.deps.settings.insights_write}

    async def _fetch(self) -> dict:
        """Rules by id side by side, then their instances and the channels side by side: two rounds of upstream
        latency for 13 calls instead of 13 (measured 2.0 s against 0.35 s at 150 ms a call)."""
        fleet = self.p.fleet
        errors: list[dict] = []

        async def rule(ref: RuleRef) -> dict:
            try:
                body, _, _ = await self.p.call("panels.alerts", "get_rule", ref.id)
                return rule_view(body.get("alert_rule") or {}, ref)
            except (InsightsError, httpx.HTTPError) as e:
                errors.append({"what": f"rule {ref.id}", **error_info(e)})
                return {"id": ref.id, "name": ref.name, "purpose": ref.purpose, "target": ref.target,
                        "status": "unknown", "error": error_info(e)}

        rules = list(await asyncio.gather(*(rule(ref) for ref in fleet.watcher.rules)))
        by_id = {r["id"]: r for r in rules}

        async def instances_of(r: dict) -> list[dict]:
            try:
                body, _, _ = await self.p.call("panels.alerts", "list_instances", rule_id=r["id"], per_page=100)
                return [instance_view(i, by_id, fleet) for i in body.get("alert_instances") or []]
            except (InsightsError, httpx.HTTPError) as e:
                errors.append({"what": f"instances of {r['id']}", **error_info(e)})
                return []

        async def channels() -> list[dict]:
            try:
                body, _, _ = await self.p.call("panels.alerts", "list_channels")
                public = self.deps.settings.public_url
                return [channel_view(c, public) for c in body.get("notification_channels") or []]
            except (InsightsError, httpx.HTTPError) as e:
                errors.append({"what": "channels", **error_info(e)})
                return []

        *per_rule, found = await asyncio.gather(*(instances_of(r) for r in rules if not r.get("error")), channels())
        instances = [i for batch in per_rule for i in batch]
        instances.sort(key=lambda i: i.get("triggered_at") or "", reverse=True)
        return {"rules": rules, "instances": instances, "channels": found, "errors": errors}

    async def set_status(self, rule_id: str, status: str) -> dict:
        """Pause or resume one of the fleet's rules: get it, then PUT its spec without the channel bindings."""
        if not self.deps.settings.insights_write:
            raise ApiError(403, "write_mode_off", "write mode is off on this server (PORTHOLE_INSIGHTS_WRITE=0)")
        ref = next((r for r in self.p.fleet.watcher.rules if r.id == rule_id), None)
        if ref is None:
            raise ApiError(403, "not_a_fleet_rule", "only the rules in the fleet description can be changed here")
        if status not in ("paused", "active"):
            raise ApiError(400, "bad_status", "status must be paused or active")
        body, calls, _ = await self.p.call("panels.rule_status", "get_rule", rule_id)
        spec = dict((body.get("alert_rule") or {}).get("spec") or {})
        spec.pop("notification_channels", None)  # omitted on PUT keeps the bindings
        updated, more, _ = await self.p.call("panels.rule_status", "update_rule", rule_id, spec, status=status)
        self.deps.cache.invalidate("alerts")
        self.deps.log.info(f"rule {rule_id} set to {status}", rule_id=rule_id)
        return {"rule": rule_view(updated.get("alert_rule") or {}, ref), "calls": calls + more}
