"""The fleet description the head serves: tentacles, the head, the sea, the watcher, and their validation.

Fleet.from_json() turns PORTHOLE_FLEET_JSON (design Appendix D) into frozen dataclasses and raises FleetError with
the JSON path of the first problem, so a bad fleet description says exactly what to fix."""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Any


class FleetError(ValueError):
    """PORTHOLE_FLEET_JSON does not describe a usable fleet; the message names the field."""


SEA_KINDS = ("load_balancer", "database", "kubernetes", "functions", "spaces", "registry", "agent")
DEFAULT_SLOTS = {"load_balancer": 4, "head": 5, "database": 6, "kubernetes": 7, "functions": 8}
REGION_RE = re.compile(r"^[a-z]{3}\d$")


@dataclass(frozen=True)
class TentacleSpec:
    name: str
    display: str
    region: str
    url: str
    id: Any
    urn: str
    service_name: str
    peer: str | None
    slot: int


@dataclass(frozen=True)
class HeadSpec:
    name: str
    app_id: str
    urn: str
    region: str
    service_name: str
    slot: int


@dataclass(frozen=True)
class SeaSpec:
    kind: str
    name: str
    region: str | None
    id: Any
    urn: str | None
    slot: int | None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RuleRef:
    id: str
    purpose: str
    name: str
    target: str | None


@dataclass(frozen=True)
class WatcherSpec:
    rules: tuple[RuleRef, ...] = ()
    channel_webhook_id: str | None = None
    channel_email_id: str | None = None
    dashboard: str | None = None


@dataclass(frozen=True)
class Entity:
    """One fleet member as the charts and links see it."""
    name: str
    display: str
    kind: str
    region: str | None
    urn: str | None
    slot: int | None
    service_name: str | None = None
    ids: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Fleet:
    project: str = ""
    regions: tuple[str, ...] = ()
    tentacles: tuple[TentacleSpec, ...] = ()
    head: HeadSpec | None = None
    sea: dict = field(default_factory=dict)
    watcher: WatcherSpec = WatcherSpec()

    @property
    def empty(self) -> bool:
        return not self.tentacles and self.head is None and not self.sea

    def tentacle(self, name: str) -> TentacleSpec | None:
        for t in self.tentacles:
            if name in (t.name, t.display):
                return t
        return None

    def entities(self) -> list[Entity]:
        out = [Entity(t.name, t.display, "tentacle", t.region, t.urn, t.slot, t.service_name, {"id": t.id})
               for t in self.tentacles]
        if self.head:
            h = self.head
            out.append(Entity(h.name, "head", "app", h.region, h.urn, h.slot, h.service_name, {"app_id": h.app_id}))
        for s in self.sea.values():
            ids = {"id": s.id, **{k: v for k, v in s.extra.items() if k in ("namespace_id", "bucket")}}
            out.append(Entity(s.name, s.name, s.kind, s.region, s.urn, s.slot, None, ids))
        return out

    def entity(self, name: str) -> Entity | None:
        """By name, display or service name; members with a color slot win when names repeat."""
        found = [e for e in self.entities()
                 if name in (e.name, e.display) or (e.service_name and name == e.service_name)]
        return min(found, key=lambda e: 0 if e.slot else 1) if found else None

    def by_urn(self, urn: str | None) -> Entity | None:
        return next((e for e in self.entities() if urn and e.urn == urn), None)

    def service_names(self) -> list[str]:
        names = [t.service_name for t in self.tentacles]
        if self.head:
            names.append(self.head.service_name)
        return names

    def rule(self, purpose: str) -> RuleRef | None:
        return next((r for r in self.watcher.rules if r.purpose == purpose), None)

    @classmethod
    def from_json(cls, text: str | None) -> Fleet:
        text = (text or "").strip() or "{}"
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise FleetError(f"PORTHOLE_FLEET_JSON is not JSON: {e.msg} at line {e.lineno} column {e.colno}") from None
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Any) -> Fleet:
        if not isinstance(data, dict):
            raise FleetError("the fleet must be a JSON object")
        tentacles = tuple(_tentacle(t, i) for i, t in enumerate(_list(data, "tentacles")))
        head = _head(data["head"]) if data.get("head") else None
        sea_raw = data.get("sea") or {}
        if not isinstance(sea_raw, dict):
            raise FleetError("sea must be an object keyed by resource kind")
        sea = {kind: _sea(kind, v) for kind, v in sea_raw.items()}
        regions = data.get("regions")
        if regions is None:
            seen = [t.region for t in tentacles] + ([head.region] if head else [])
            regions = list(dict.fromkeys(seen))
        if not isinstance(regions, list) or not all(isinstance(r, str) and REGION_RE.match(r) for r in regions):
            raise FleetError(f"regions must be a list of region slugs like 'tor1', got {regions!r}")
        fleet = cls(project=str(data.get("project") or ""), regions=tuple(regions), tentacles=tentacles,
                    head=head, sea=sea, watcher=_watcher(data.get("watcher") or {}))
        _check(fleet)
        return fleet


def _list(data: dict, key: str) -> list:
    value = data.get(key) or []
    if not isinstance(value, list):
        raise FleetError(f"{key} must be a list")
    return value


def _req(obj: dict, key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FleetError(f"{where}.{key} is required and must be a non-empty string")
    return value.strip()


def _slot(value: Any, where: str, default: int | None) -> int | None:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 8:
        raise FleetError(f"{where}.slot must be an integer from 1 to 8, got {value!r}")
    return value


def _url(value: str, where: str, schemes: tuple[str, ...]) -> str:
    m = re.match(r"^([a-z]+)://([^/@]+)(/.*)?$", value)
    if not m or m.group(1) not in schemes:
        raise FleetError(f"{where} must be an {' or '.join(schemes)} URL without credentials, got {value!r}")
    return value.rstrip("/")


def _tentacle(t: Any, i: int) -> TentacleSpec:
    where = f"tentacles[{i}]"
    if not isinstance(t, dict):
        raise FleetError(f"{where} must be an object")
    name = _req(t, "name", where)
    tid = t.get("id")
    return TentacleSpec(
        name=name, display=str(t.get("display") or re.sub(r"^kraken-", "", name)),
        region=_req(t, "region", where), url=_url(_req(t, "url", where), f"{where}.url", ("http", "https")),
        id=tid, urn=str(t.get("urn") or (f"do:droplet:{tid}" if tid else "")),
        service_name=str(t.get("service_name") or name), peer=t.get("peer") or None,
        slot=_slot(t.get("slot"), where, i + 1 if i < 3 else None) or 0)


def _head(h: Any) -> HeadSpec:
    if not isinstance(h, dict):
        raise FleetError("head must be an object")
    app_id = _req(h, "app_id", "head")
    return HeadSpec(name=str(h.get("name") or "porthole"), app_id=app_id,
                    urn=str(h.get("urn") or f"do:app:{app_id}"), region=_req(h, "region", "head"),
                    service_name=str(h.get("service_name") or "porthole"),
                    slot=_slot(h.get("slot"), "head", DEFAULT_SLOTS["head"]) or 0)


def _sea(kind: str, v: Any) -> SeaSpec:
    where = f"sea.{kind}"
    if kind not in SEA_KINDS:
        raise FleetError(f"{where} is not a known resource kind; use one of {list(SEA_KINDS)}")
    if not isinstance(v, dict):
        raise FleetError(f"{where} must be an object")
    extra = {k: val for k, val in v.items() if k not in ("name", "id", "region", "urn", "slot")}
    if kind == "load_balancer" and extra.get("ip"):
        try:
            ipaddress.IPv4Address(str(extra["ip"]))
        except ValueError:
            raise FleetError(f"{where}.ip must be an IPv4 address, got {extra['ip']!r}") from None
    if kind == "functions" and extra.get("url"):
        extra["url"] = _url(str(extra["url"]), f"{where}.url", ("https", "http"))
    if kind == "spaces":
        extra.setdefault("bucket", v.get("name"))
    urn = v.get("urn") or None
    if kind == "functions" and not urn and extra.get("namespace_id"):
        # descriptions written before B-033 have no urn; Insights labels the namespace's series with this one
        urn = f"do:functions_namespace:{extra['namespace_id']}"
    return SeaSpec(kind=kind, name=_req(v, "name", where), region=v.get("region"), id=v.get("id"),
                   urn=urn, slot=_slot(v.get("slot"), where, DEFAULT_SLOTS.get(kind)), extra=extra)


def _watcher(w: Any) -> WatcherSpec:
    if not isinstance(w, dict):
        raise FleetError("watcher must be an object")
    rules = []
    for i, r in enumerate(_list(w, "rules")):
        if not isinstance(r, dict):
            raise FleetError(f"watcher.rules[{i}] must be an object")
        rules.append(RuleRef(id=_req(r, "id", f"watcher.rules[{i}]"), purpose=str(r.get("purpose") or ""),
                             name=str(r.get("name") or ""), target=r.get("target") or None))
    return WatcherSpec(rules=tuple(rules), channel_webhook_id=w.get("channel_webhook_id") or None,
                       channel_email_id=w.get("channel_email_id") or None, dashboard=w.get("dashboard") or None)


def _check(fleet: Fleet) -> None:
    for i, t in enumerate(fleet.tentacles):
        if t.region not in fleet.regions:
            raise FleetError(f"tentacles[{i}].region {t.region!r} is not one of regions {list(fleet.regions)}")
        if t.peer and not fleet.tentacle(t.peer):
            raise FleetError(f"tentacles[{i}].peer {t.peer!r} is not a tentacle in this fleet")
    if fleet.head and fleet.head.region not in fleet.regions:
        raise FleetError(f"head.region {fleet.head.region!r} is not one of regions {list(fleet.regions)}")
    names: set[str] = set()
    slots: dict[int, str] = {}
    for e in fleet.entities():
        if not e.slot:
            continue  # unslotted members may share a name (Appendix D names both the Function and registry kraken)
        if e.name in names:
            raise FleetError(f"the name {e.name!r} is used by two charted fleet members")
        names.add(e.name)
        if e.slot:
            if e.slot in slots:
                raise FleetError(f"slot {e.slot} is given to both {slots[e.slot]!r} and {e.name!r}")
            slots[e.slot] = e.name
    for i, r in enumerate(fleet.watcher.rules):
        if r.target and not fleet.tentacle(r.target):
            raise FleetError(f"watcher.rules[{i}].target {r.target!r} is not a tentacle in this fleet")
