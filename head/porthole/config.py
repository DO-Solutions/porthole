"""Settings for the head, read from environment variables only; the fleet model lives in fleet_model.py.

Settings.from_env() never raises on bad input: each problem becomes a line in Settings.problems and the head
starts in a degraded mode that explains itself, so the page can still say what is missing."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from porthole import __version__
from porthole.fleet_model import (  # noqa: F401 (re-exported: config is where callers look for the fleet)
    Entity,
    Fleet,
    FleetError,
    HeadSpec,
    RuleRef,
    SeaSpec,
    TentacleSpec,
    WatcherSpec,
)


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
    Var("PORTHOLE_DO_CONTEXT", "GENERAL", "",
        "team context id, the i= parameter of control-panel Insights links; empty leaves it out"),
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
    Var("PORTHOLE_PORT", "GENERAL", "8080",
        "listen port when started with python -m porthole.main from head/; the container always listens on 8080"),
    Var("PORTHOLE_VERSION", "GENERAL", "", "version shown in /healthz; the Dockerfile bakes the git SHA when given"),
)

# Slot N uses the skin's fleet color SLOT_PALETTE[N-1] (1-based). The file order failed the palette validator's
# adjacent-pair checks; this order passes them with zero hex changes (head/static/README.md). porthole.css
# carries the same map as --slot-N custom properties.
SLOT_PALETTE = (1, 6, 5, 8, 2, 4, 3, 7)
MIN_CAPTAIN_KEY = 24


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
    do_context: str = ""
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
        do_context = get("PORTHOLE_DO_CONTEXT")
        if do_context and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", do_context):
            problems.append("PORTHOLE_DO_CONTEXT may hold only letters, digits, _ and -; Insights links leave it out")
            do_context = ""
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
                do_context=do_context,
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
