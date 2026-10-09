"""Control-panel and docs links (design Appendix A), each carrying whether its URL pattern is verified.

The Insights tab URLs are unverified: the docs give only the menu path. PORTHOLE_DEEPLINKS_JSON overrides any
entry once someone has opened the real page; an override given as a plain string counts as verified."""
from __future__ import annotations

from typing import Any

from porthole.config import Entity, Fleet

CP = "https://cloud.digitalocean.com"
DOCS = "https://docs.digitalocean.com/products/insights"

# key -> (pattern, verified, note)
DEFAULTS: dict[str, tuple[str, bool, str]] = {
    "insights.metrics": (f"{CP}/insights/metrics", False, "the docs give only the menu path"),
    "insights.dashboards": (f"{CP}/insights/dashboards", False, "the docs give only the menu path"),
    "insights.alerts": (f"{CP}/insights/alerts", False, "the docs give only the menu path"),
    "insights.logs": (f"{CP}/insights/logs", False, "the docs give only the menu path"),
    "insights.traces": (f"{CP}/insights/traces", False, "the docs give only the menu path"),
    "feature_preview": (f"{CP}/account/feature-preview", True, "docs"),
    "droplet": (f"{CP}/droplets/{{id}}/graphs", False, "pattern in common use"),
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


class DeepLinks:
    def __init__(self, fleet: Fleet, overrides: dict | None = None):
        self.fleet = fleet
        self.table: dict[str, dict] = {k: {"pattern": p, "verified": v, "note": n} for k, (p, v, n) in DEFAULTS.items()}
        for key, value in (overrides or {}).items():
            if isinstance(value, str):
                self.table[key] = {"pattern": value, "verified": True, "note": "override"}
            elif isinstance(value, dict) and value.get("pattern"):
                base = self.table.get(key, {"note": "override"})
                self.table[key] = {**base, **{k: value[k] for k in ("pattern", "verified", "note") if k in value}}
                self.table[key].setdefault("verified", True)

    def link(self, key: str, **ids: Any) -> dict:
        entry = self.table.get(key)
        if entry is None:
            return {"url": None, "verified": False, "note": f"no link for {key}"}
        try:
            url = entry["pattern"].format(**{k: v for k, v in ids.items() if v not in (None, "")})
        except KeyError:
            url = None  # an id the pattern needs is missing from the fleet description
        return {"url": url, "verified": bool(entry["verified"]), "note": entry.get("note", "")}

    def for_entity(self, entity: Entity) -> dict:
        key = KIND_TO_KEY.get(entity.kind)
        return self.link(key, **entity.ids) if key else {"url": None, "verified": False, "note": "no link"}

    def public(self) -> dict:
        """The non-resource links for /api/config: Insights tabs, the feature preview page and the docs."""
        return {k: self.link(k) for k in self.table if k.startswith(("insights.", "docs.", "feature_preview"))}

    def entities(self) -> dict:
        return {e.name: self.for_entity(e) for e in self.fleet.entities()}
