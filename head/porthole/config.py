"""Settings for the head, read from environment variables only, and the fleet description it serves.

Settings.from_env() never raises on bad input: each problem becomes a line in Settings.problems and the head
starts in a degraded mode that explains itself, so the page can still say what is missing."""
from __future__ import annotations

import ipaddress
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from porthole import __version__


class FleetError(ValueError):
    """PORTHOLE_FLEET_JSON does not describe a usable fleet; the message names the field."""


@dataclass(frozen=True)
class Var:
    name: str
    kind: str  # SECRET or GENERAL
    default: str
    purpose: str


VARIABLES: tuple[Var, ...] = (
    Var("DIGITALOCEAN_TOKEN", "SECRET", "",
        "Insights token for the head (insights:read, plus insights:update for write mode)"),
    Var("PORTHOLE_CAPTAIN_KEY", "SECRET", "", "shared key for every mutating route, 24 characters or more"),
    Var("TENTACLE_KEY", "SECRET", "", "bearer the tentacles expect on their scenario endpoints"),
    Var("PORTHOLE_FLEET_JSON", "GENERAL", "{}",
        "the fleet description written by infra/fleet.py: ids, IPs, URNs, names, no secrets"),
    Var("PORTHOLE_HOOK_BEARER", "SECRET", "", "bearer configured on the Insights webhook channel"),
    Var("PORTHOLE_HOOK_BASIC", "SECRET", "", "user:password configured on the webhook channel, instead of the bearer"),
    Var("PORTHOLE_HOOK_SECRET", "SECRET", "", "signing secret configured on the channel; enables the signature checks"),
    Var("PORTHOLE_PUBLIC_URL", "GENERAL", "https://insights-demo.digitalocean.solutions",
        "public base URL, used in curl output and the webhook URL shown"),
    Var("PORTHOLE_INSIGHTS_WRITE", "GENERAL", "0",
        "1 shows Pause and Resume on the Alerts page and enables the rule status route"),
    Var("PORTHOLE_INSIGHTS_BASE_URL", "GENERAL", "https://api.digitalocean.com",
        "Insights API base URL; the local fake uses http://insights-fake:9000"),
    Var("PORTHOLE_TRUST_PROXY", "GENERAL", "1",
        "1 reads the client address from the first X-Forwarded-For hop (App Platform); 0 locally"),
    Var("PORTHOLE_DEEPLINKS_JSON", "GENERAL", "",
        "JSON object overriding control-panel link patterns once they are verified"),
    Var("PORTHOLE_UPSTREAM_BUDGET_PER_MIN", "GENERAL", "200",
        "Insights calls allowed per minute before panels serve cached data"),
    Var("PORTHOLE_CACHE_TTL_S", "GENERAL", "20", "panel cache lifetime in seconds"),
    Var("PORTHOLE_BRAIN", "GENERAL", "deckhand", "deckhand, harness-runtime or off"),
    Var("PORTHOLE_BRAIN_SESSION", "GENERAL", "", "phase 2: Harness Runtime session name"),
    Var("PORTHOLE_BRAIN_TOKEN", "SECRET", "", "phase 2: token of the session owner, used to answer approvals"),
    Var("PORTHOLE_GATEWAY_MCP_URL", "SECRET", "", "phase 2: the Action Gateway session's MCP URL"),
    Var("PORTHOLE_MCP_KEY", "SECRET", "", "phase 2: API key the head's own MCP server requires from Action Gateway"),
    Var("OTEL_EXPORTER_OTLP_ENDPOINT", "GENERAL", "",
        "OTLP/HTTP base URL; unset means nothing is exported, spans stay in memory"),
    Var("OTEL_EXPORTER_OTLP_HEADERS", "SECRET", "", "headers for the OTLP exporters, k=v pairs separated by commas"),
    Var("OTEL_SERVICE_NAME", "GENERAL", "porthole", "service.name on spans and log records"),
    Var("PORTHOLE_LOG_LEVEL", "GENERAL", "INFO", "DEBUG, INFO, WARN or ERROR"),
    Var("PORTHOLE_PORT", "GENERAL", "8080", "listen port when started with python -m porthole.main"),
    Var("PORTHOLE_VERSION", "GENERAL", "", "version shown in /healthz; the Dockerfile bakes the git SHA when given"),
)

# Slot N uses the skin's fleet color SLOT_PALETTE[N-1] (1-based). The file order failed the palette validator's
# adjacent-pair checks; this order passes them with zero hex changes (head/static/README.md). porthole.css
# carries the same map as --slot-N custom properties.
SLOT_PALETTE = (1, 6, 5, 8, 2, 4, 3, 7)
SEA_KINDS = ("load_balancer", "database", "kubernetes", "functions", "spaces", "registry", "agent")
DEFAULT_SLOTS = {"load_balancer": 4, "head": 5, "database": 6, "kubernetes": 7, "functions": 8}
REGION_RE = re.compile(r"^[a-z]{3}\d$")
MIN_CAPTAIN_KEY = 24


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

    def names_in_region(self, region: str) -> list[str]:
        return [e.name for e in self.entities() if e.region == region]

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
    return SeaSpec(kind=kind, name=_req(v, "name", where), region=v.get("region"), id=v.get("id"),
                   urn=v.get("urn") or None, slot=_slot(v.get("slot"), where, DEFAULT_SLOTS.get(kind)), extra=extra)


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


@dataclass(frozen=True)
class Settings:
    token: str = ""
    captain_key: str = ""
    tentacle_key: str = ""
    fleet: Fleet = Fleet()
    hook_bearer: str = ""
    hook_basic: str = ""
    hook_secret: str = ""
    public_url: str = "https://insights-demo.digitalocean.solutions"
    insights_write: bool = False
    insights_base_url: str = "https://api.digitalocean.com"
    trust_proxy: bool = True
    deeplink_overrides: dict = field(default_factory=dict)
    upstream_budget_per_min: int = 200
    cache_ttl_s: float = 20.0
    brain: str = "deckhand"
    brain_session: str = ""
    brain_token: str = ""
    gateway_mcp_url: str = ""
    mcp_key: str = ""
    otlp_endpoint: str = ""
    otlp_headers: str = ""
    service_name: str = "porthole"
    log_level: str = "INFO"
    port: int = 8080
    version: str = __version__
    problems: tuple[str, ...] = ()

    @property
    def captain_configured(self) -> bool:
        return len(self.captain_key) >= MIN_CAPTAIN_KEY

    @property
    def insights_configured(self) -> bool:
        return bool(self.token)

    def secret_values(self) -> list[str]:
        """Every configured secret, for the log filter and the API trace scrubber."""
        values = [self.token, self.captain_key, self.tentacle_key, self.hook_bearer, self.hook_basic,
                  self.hook_secret, self.brain_token, self.gateway_mcp_url, self.mcp_key, self.otlp_headers]
        if ":" in self.hook_basic:
            values.append(self.hook_basic.split(":", 1)[1])
        for pair in self.otlp_headers.split(","):
            if "=" in pair:
                values.append(pair.split("=", 1)[1].strip())
        return [v for v in values if v and len(v) >= 6]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        problems: list[str] = []

        def get(name: str) -> str:
            var = next(v for v in VARIABLES if v.name == name)
            value = env.get(name)
            return var.default if value is None else value.strip()

        def number(name: str, kind: type, lo: float) -> Any:
            raw, default = get(name), next(v.default for v in VARIABLES if v.name == name)
            try:
                value = kind(raw)
                if value < lo:
                    raise ValueError
                return value
            except ValueError:
                problems.append(f"{name}={raw!r} is not a valid number; using {default}")
                return kind(default)

        try:
            fleet = Fleet.from_json(get("PORTHOLE_FLEET_JSON"))
        except FleetError as e:
            problems.append(f"PORTHOLE_FLEET_JSON: {e}")
            fleet = Fleet()
        if fleet.empty and not problems:
            problems.append("PORTHOLE_FLEET_JSON describes no fleet yet: run infra/fleet.py and set it")
        overrides: dict = {}
        if get("PORTHOLE_DEEPLINKS_JSON"):
            try:
                overrides = json.loads(get("PORTHOLE_DEEPLINKS_JSON"))
                if not isinstance(overrides, dict):
                    raise ValueError
            except ValueError:
                problems.append("PORTHOLE_DEEPLINKS_JSON is not a JSON object; using the default links")
                overrides = {}
        brain = get("PORTHOLE_BRAIN").lower()
        if brain not in ("deckhand", "harness-runtime", "off"):
            problems.append(f"PORTHOLE_BRAIN={brain!r} is not deckhand, harness-runtime or off; using deckhand")
            brain = "deckhand"
        level = get("PORTHOLE_LOG_LEVEL").upper().replace("WARNING", "WARN")
        if level not in ("DEBUG", "INFO", "WARN", "ERROR"):
            problems.append(f"PORTHOLE_LOG_LEVEL={level!r} is unknown; using INFO")
            level = "INFO"
        s = cls(token=get("DIGITALOCEAN_TOKEN"), captain_key=get("PORTHOLE_CAPTAIN_KEY"),
                tentacle_key=get("TENTACLE_KEY"), fleet=fleet, hook_bearer=get("PORTHOLE_HOOK_BEARER"),
                hook_basic=get("PORTHOLE_HOOK_BASIC"), hook_secret=get("PORTHOLE_HOOK_SECRET"),
                public_url=get("PORTHOLE_PUBLIC_URL").rstrip("/"),
                insights_write=get("PORTHOLE_INSIGHTS_WRITE") == "1",
                insights_base_url=get("PORTHOLE_INSIGHTS_BASE_URL").rstrip("/"),
                trust_proxy=get("PORTHOLE_TRUST_PROXY") != "0", deeplink_overrides=overrides,
                upstream_budget_per_min=number("PORTHOLE_UPSTREAM_BUDGET_PER_MIN", int, 1),
                cache_ttl_s=number("PORTHOLE_CACHE_TTL_S", float, 0), brain=brain,
                brain_session=get("PORTHOLE_BRAIN_SESSION"), brain_token=get("PORTHOLE_BRAIN_TOKEN"),
                gateway_mcp_url=get("PORTHOLE_GATEWAY_MCP_URL"), mcp_key=get("PORTHOLE_MCP_KEY"),
                otlp_endpoint=get("OTEL_EXPORTER_OTLP_ENDPOINT").rstrip("/"),
                otlp_headers=get("OTEL_EXPORTER_OTLP_HEADERS"),
                service_name=get("OTEL_SERVICE_NAME") or "porthole", log_level=level,
                port=number("PORTHOLE_PORT", int, 1), version=get("PORTHOLE_VERSION") or __version__)
        return cls(**{**s.__dict__, "problems": tuple(problems + _secret_problems(s))})


def _secret_problems(s: Settings) -> list[str]:
    out = []
    if not s.token:
        out.append("DIGITALOCEAN_TOKEN is not set: Insights panels say 'Insights not configured'")
    if not s.captain_key:
        out.append("PORTHOLE_CAPTAIN_KEY is not set: mutating routes return 503")
    elif not s.captain_configured:
        out.append(f"PORTHOLE_CAPTAIN_KEY is shorter than {MIN_CAPTAIN_KEY} characters: mutating routes return 503")
    if not s.tentacle_key:
        out.append("TENTACLE_KEY is not set: the tentacles will refuse to start scenarios")
    if not (s.hook_bearer or s.hook_basic):
        out.append("neither PORTHOLE_HOOK_BEARER nor PORTHOLE_HOOK_BASIC is set: /hooks/insights accepts any caller")
    if s.hook_basic and ":" not in s.hook_basic:
        out.append("PORTHOLE_HOOK_BASIC must be user:password")
    return out


def slot_palette(watcher_dir: Path) -> dict:
    """{"1": "#30cbd9", ...} from watcher/skin/kraken.skin.json in the validated slot order."""
    try:
        fleet = json.loads((watcher_dir / "skin" / "kraken.skin.json").read_text())["fleet"]
        return {str(slot): fleet[idx - 1] for slot, idx in enumerate(SLOT_PALETTE, start=1)}
    except (OSError, ValueError, KeyError, IndexError):
        return {}
