"""Control-panel and docs links (design Appendix A), each carrying whether its URL pattern is verified.

The seven Insights tab URLs were verified in the control panel on 2026-10-09 and take the team context, the region
and a relative range; PORTHOLE_DEEPLINKS_JSON overrides any entry, and a plain string override counts as verified."""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from porthole.config import Entity, Fleet

CP = "https://cloud.digitalocean.com"
DOCS = "https://docs.digitalocean.com/products/insights"
# Every Insights tab takes the same query: i= is the team context (left out when PORTHOLE_DO_CONTEXT is empty),
# region= the region slug, and from/to a Grafana-style relative range.
TAB_QUERY = "?i={context}&region={region}&from=now-1h&to=now"
TAB_NOTE = "verified in the control panel, 2026-10-09"
TABS = {"metrics": "metrics", "dashboards": "dashboards", "alerts": "alerts", "logs": "logs", "traces": "traces",
        "uptime": "uptime/checks", "settings": "settings"}

# key -> (pattern, verified, note)
DEFAULTS: dict[str, tuple[str, bool, str]] = {
    **{f"insights.{key}": (f"{CP}/insights/{path}{TAB_QUERY}", True, TAB_NOTE) for key, path in TABS.items()},
    "feature_preview": (f"{CP}/account/feature-preview", True, "docs"),
    "droplet": (f"{CP}/droplets/{{id}}/graphs", False, "pattern in common use"),
    # A Droplet's own "Insights" tab shows the legacy Monitoring graphs, not the Insights product (finding A16).
    "droplet.legacy_insights": (f"{CP}/droplets/{{id}}/insights", True,
                                "legacy Monitoring graphs, not the Insights product (finding A16)"),
    "app": (f"{CP}/apps/{{app_id}}", False, "docs link /apps, then the app, then its Insights tab"),
    "load_balancer": (f"{CP}/networking/load_balancers/{{id}}", False, "pattern in common use"),
    "database": (f"{CP}/databases/{{id}}", False, "pattern in common use"),
    "kubernetes": (f"{CP}/kubernetes/clusters/{{id}}", False, "pattern in common use"),
    "functions": (f"{CP}/functions/{{namespace_id}}", False, "pattern in common use"),
    "spaces": (f"{CP}/spaces/{{bucket}}", False, "pattern in common use"),
    "registry": (f"{CP}/registry", False, "pattern in common use"),
    "harness_runtime": (f"{CP}/managed-agents/harness-runtime", False, "not in the docs"),
    "docs.insights": (f"{DOCS}/", True, "docs"),
    "docs.quickstart": (f"{DOCS}/getting-started/quickstart/", True, "docs"),
    "docs.metrics": (f"{DOCS}/how-to/explore-query-metrics/", True, "docs"),
    "docs.alerts": (f"{DOCS}/how-to/manage-metrics-alerts/", True, "docs"),
    "docs.dashboards": (f"{DOCS}/how-to/manage-custom-dashboards/", True, "docs"),
    "docs.customize": (f"{DOCS}/how-to/customize-dashboards/", True, "docs"),
    "docs.traces": (f"{DOCS}/how-to/view-traces/", True, "docs"),
    "docs.logs": (f"{DOCS}/how-to/view-logs/", True, "docs"),
    "docs.api": (f"{DOCS}/reference/api/", True, "docs"),
    "docs.limits": (f"{DOCS}/details/limits/", True, "docs"),
    "docs.availability": (f"{DOCS}/details/availability/", True, "docs"),
}
KIND_TO_KEY = {"tentacle": "droplet", "app": "app", "load_balancer": "load_balancer", "database": "database",
               "kubernetes": "kubernetes", "functions": "functions", "spaces": "spaces", "registry": "registry",
               "agent": "harness_runtime"}


def drop_empty_params(url: str) -> str:
    """The URL without query parameters whose value is empty, such as i= when no team context is configured."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if v]
    return urlunsplit(parts._replace(query=urlencode(kept, safe="-")))


class DeepLinks:
    def __init__(self, fleet: Fleet, overrides: dict | None = None, context: str = ""):
        self.fleet = fleet
        self.context = context
        self.table: dict[str, dict] = {k: {"pattern": p, "verified": v, "note": n} for k, (p, v, n) in DEFAULTS.items()}
        for key, value in (overrides or {}).items():
            if isinstance(value, str):
                self.table[key] = {"pattern": value, "verified": True, "note": "override"}
            elif isinstance(value, dict) and value.get("pattern"):
                base = self.table.get(key, {"note": "override"})
                self.table[key] = {**base, **{k: value[k] for k in ("pattern", "verified", "note") if k in value}}
                self.table[key].setdefault("verified", True)

    def default_region(self) -> str:
        return self.fleet.regions[0] if self.fleet.regions else "tor1"

    def link(self, key: str, region: str | None = None, **ids: Any) -> dict:
        """The link for key with ids filled in. {context} and {region} come from the settings and the fleet's
        first region unless given; a query parameter left empty is dropped."""
        entry = self.table.get(key)
        if entry is None:
            return {"url": None, "verified": False, "note": f"no link for {key}"}
        values = {"context": self.context, "region": region or self.default_region()}
        values.update({k: v for k, v in ids.items() if v not in (None, "")})
        try:
            url = drop_empty_params(entry["pattern"].format(**values))
        except KeyError:
            url = None  # an id the pattern needs is missing from the fleet description
        return {"url": url, "verified": bool(entry["verified"]), "note": entry.get("note", "")}

    def for_entity(self, entity: Entity) -> dict:
        key = KIND_TO_KEY.get(entity.kind)
        return self.link(key, **entity.ids) if key else {"url": None, "verified": False, "note": "no link"}

    def public(self) -> dict:
        """The non-resource links for /api/config: Insights tabs, the feature preview page and the docs. Each
        Insights tab also carries by_region, one URL per fleet region, for the pages' region selector."""
        out = {}
        regions = list(self.fleet.regions) or [self.default_region()]
        for key in self.table:
            if key.startswith(("insights.", "docs.", "feature_preview")):
                out[key] = self.link(key)
                if key.startswith("insights."):
                    out[key]["by_region"] = {r: self.link(key, region=r)["url"] for r in regions}
        return out

    def entities(self) -> dict:
        return {e.name: self.for_entity(e) for e in self.fleet.entities()}
