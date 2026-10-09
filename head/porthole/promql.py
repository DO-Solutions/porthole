"""Builder-mode PromQL for visitors and the checks on raw PromQL for the captain, plus unit heuristics.

Every builder query is pinned to the fleet's resource URNs, so a visitor cannot send a discovery query to the
shared team account. The region is the path segment of the Insights call and never a label matcher: labels vary
per metric, and do.droplets.cpu_utilization carries no resource_region_slug (finding A22, B-026). Fleet members
are selected and grouped by resource_urn because fresh Droplets report no resource_name (B-023); a resource_name
filter on a fleet member becomes its URN, and only members without a URN in the fleet description are selected by
name. Metric names are written dotted, as Insights expects (finding A4)."""
from __future__ import annotations

import math
import re
from typing import Any

from porthole.config import Fleet

AGGS = ("avg", "sum", "max", "min", "rate")
RANGES = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "3h": 10800, "6h": 21600, "24h": 86400}
MIN_STEP_S = 60
MAX_RANGE_S = 86400
MAX_POINTS = 1440
MAX_QUERY_CHARS = 500
DOTTED = re.compile(r"^do(\.[a-z0-9_]+){2,}$")
UNDERSCORED = re.compile(r"^do_[a-z0-9_]+$")
FLEET_LABELS = ("resource_name", "resource_urn", "service_name")
ENUM_LABELS: dict[str, frozenset[str]] = {
    "cpu_mode": frozenset({"user", "system", "idle", "iowait", "nice", "irq", "softirq", "steal"}),
    "network_device": frozenset({"eth0", "eth1"}),
    "filesystem_mountpoint": frozenset({"/"}),
    "disk_device": frozenset({"vda", "vdb", "sda"}),
    "spaces_operation": frozenset({"GET", "PUT", "DELETE", "HEAD", "LIST"}),
}
FAMILIES = ("droplets", "load_balancers", "databases", "kubernetes", "apps", "container_registry", "spaces",
            "functions", "serverless", "nat_gateways", "nfs", "vector_databases", "volumes", "gpu_droplets")
DURATION = re.compile(r"^(\d+)([smhd]?)$")
# Catalog names that are bytes without saying so (watcher/catalog: filesystem_free, not filesystem_free_bytes)
BYTES = re.compile(r"[._](filesystem_(free|size)|memory_(available|free|cached|total|swap_\w+)|storage_used)\b")
FAMILY_KIND = {"droplets": "tentacle", "apps": "app", "load_balancers": "load_balancer", "databases": "database",
               "kubernetes": "kubernetes", "functions": "functions", "spaces": "spaces",
               "container_registry": "registry"}  # do.serverless is Serverless Inference, not Functions (B-033)


class BuilderError(ValueError):
    """A visitor's query part is outside what the builder allows; the message says which part."""


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def regex_escape(values: list[str]) -> str:
    """values joined as an RE2 alternation, each value escaped."""
    return "|".join(re.sub(r"([.+*?()|\[\]{}^$\\])", r"\\\1", v) for v in values)


def regex_alternation(values: list[str]) -> str:
    return quote(regex_escape(values))


def check_metric(metric: str) -> str:
    metric = (metric or "").strip()
    if UNDERSCORED.match(metric):
        raise BuilderError(f"use the dotted name {dotted(metric)[1]}: Insights takes dotted names in queries "
                           "and returns underscored ones (finding A4)")
    if not DOTTED.match(metric) or len(metric) > 120:
        raise BuilderError(f"{metric!r} is not a dotted metric name like do.droplets.cpu_utilization")
    return metric


def fleet_values(fleet: Fleet) -> dict[str, set[str]]:
    entities = fleet.entities()
    return {"resource_name": {e.name for e in entities} | {e.display for e in entities},
            "resource_urn": {e.urn for e in entities if e.urn}, "service_name": set(fleet.service_names())}


def parse_filters(raw: Any) -> dict[str, list[str]]:
    """'label=value' pairs, given repeated or comma separated; values for one label are ORed."""
    items: list[str] = []
    for part in (raw if isinstance(raw, (list, tuple)) else [raw or ""]):
        items += [p.strip() for p in str(part).split(",") if p.strip()]
    out: dict[str, list[str]] = {}
    for item in items:
        label, sep, value = item.partition("=")
        if not sep or not label.strip() or not value.strip():
            raise BuilderError(f"filter {item!r} must look like label=value")
        values = out.setdefault(label.strip(), [])
        if value.strip() not in values:
            values.append(value.strip())
    return out


def check_filters(filters: dict[str, list[str]], fleet: Fleet) -> None:
    known = fleet_values(fleet)
    for label, values in filters.items():
        if label in known:
            outside = [v for v in values if v not in known[label]]
            if outside:
                raise BuilderError(f"{label} values {outside} are not in this fleet")
        elif label in ENUM_LABELS:
            outside = [v for v in values if v not in ENUM_LABELS[label]]
            if outside:
                raise BuilderError(f"{label} values {outside} are not allowed; use {sorted(ENUM_LABELS[label])}")
        else:
            raise BuilderError(f"label {label!r} is not available in the builder; use one of "
                               f"{list(FLEET_LABELS) + sorted(ENUM_LABELS)}")


def member_filters(entity: Any) -> dict[str, list[str]]:
    """The builder filter for one fleet member: its URN, or its name when the fleet description has no URN."""
    return {"resource_urn": [entity.urn]} if entity.urn else {"resource_name": [entity.name]}


def to_urns(filters: dict[str, list[str]], fleet: Fleet) -> dict[str, list[str]]:
    """resource_name filters on fleet members (by name or display name) as resource_urn filters."""
    if not filters.get("resource_name"):
        return filters
    out = {label: list(values) for label, values in filters.items() if label != "resource_name"}
    by_name = []
    for value in filters["resource_name"]:
        e = fleet.entity(value)
        if e and e.urn:
            urns = out.setdefault("resource_urn", [])
            if e.urn not in urns:
                urns.append(e.urn)
        elif (e.name if e else value) not in by_name:
            by_name.append(e.name if e else value)
    if by_name and out.get("resource_urn"):
        raise BuilderError(f"resource_name values {by_name} have no URN in the fleet description, so they cannot "
                           "share a chart with members selected by URN; chart them on their own")
    if by_name:
        out["resource_name"] = by_name
    return out


def pin(metric: str, region: str, fleet: Fleet) -> str:
    """The matcher that keeps a query on the fleet: the URNs of the region's members that report this metric
    family, or their names when none of them has a URN."""
    in_region = [e for e in fleet.entities() if e.region == region] or fleet.entities()
    parts = metric.split(".")
    kind = FAMILY_KIND.get(parts[1]) if len(parts) > 2 else None
    members = [e for e in in_region if e.kind == kind] or in_region
    urns = sorted({e.urn for e in members if e.urn})
    if urns:
        return f"resource_urn=~{regex_alternation(urns)}"
    return f"resource_name=~{regex_alternation(sorted({e.name for e in members}))}"


def _select(metric: str, filters: dict[str, list[str]], region: str, fleet: Fleet) -> tuple[str, str]:
    """(selector, the label it picks fleet members by)."""
    filters = to_urns(filters, fleet)
    matchers = []
    if not any(label in filters for label in ("resource_name", "resource_urn")):
        matchers.append(pin(metric, region, fleet))
    for label in sorted(filters):
        values = filters[label]
        matchers.append(f"{label}={quote(values[0])}" if len(values) == 1 else f"{label}=~{regex_alternation(values)}")
    by = "resource_name" if any(m.startswith("resource_name=") for m in matchers) else "resource_urn"
    return f"{metric}{{{', '.join(matchers)}}}", by


def selector(metric: str, filters: dict[str, list[str]], region: str, fleet: Fleet) -> str:
    return _select(metric, filters, region, fleet)[0]


def build(metric: str, agg: str | None, filters: dict[str, list[str]], region: str, fleet: Fleet,
          group_by: str | None = None) -> str:
    """The query the builder writes, e.g. avg by (resource_urn) (do.droplets.cpu_utilization{...}). It groups by
    the label the fleet members were selected by unless group_by says otherwise."""
    metric = check_metric(metric)
    agg = (agg or "").strip().lower() or None
    if agg not in (None, "none", *AGGS):
        raise BuilderError(f"agg must be one of {list(AGGS)}")
    check_filters(filters, fleet)
    sel, by = _select(metric, filters, region, fleet)
    group_by = group_by or by
    if agg in (None, "none"):
        return sel
    if agg == "rate":
        return f"sum by ({group_by}) (rate({sel}[5m]))"
    return f"{agg} by ({group_by}) ({sel})"


def check_raw(query: str) -> str:
    """Raw PromQL from the captain: at most 500 characters, printable, brackets balanced."""
    query = (query or "").strip()
    if not query:
        raise BuilderError("the query is empty")
    if len(query) > MAX_QUERY_CHARS:
        raise BuilderError(f"the query is {len(query)} characters; the cap is {MAX_QUERY_CHARS}")
    if any(ord(c) < 32 and c not in "\n\t" for c in query) or any(ord(c) > 126 for c in query):
        raise BuilderError("the query contains characters outside printable ASCII")
    pairs = {")": "(", "}": "{", "]": "["}
    stack: list[str] = []
    in_string = False
    for i, c in enumerate(query):
        if c == '"' and (i == 0 or query[i - 1] != "\\"):
            in_string = not in_string
        elif not in_string and c in "({[":
            stack.append(c)
        elif not in_string and c in ")}]":
            if not stack or stack.pop() != pairs[c]:
                raise BuilderError(f"unbalanced {c!r} at position {i + 1}")
    if stack or in_string:
        raise BuilderError("unbalanced brackets or quotes")
    return query


def seconds(value: str | int | None, what: str) -> int:
    if isinstance(value, int):
        return value
    m = DURATION.match(str(value or "").strip())
    if not m:
        raise BuilderError(f"{what} {value!r} must look like 30m, 1h or 60s")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def range_seconds(value: str | None, builder: bool = True) -> int:
    value = value or "30m"
    if builder:
        if value not in RANGES:
            raise BuilderError(f"range must be one of {list(RANGES)}")
        return RANGES[value]
    s = seconds(value, "range")
    if not 60 <= s <= MAX_RANGE_S:
        raise BuilderError(f"range must be between 1m and 24h, got {value}")
    return s


def step_seconds(value: str | None, range_s: int) -> int:
    """At least 60 s, and coarse enough to keep at most 1,440 points."""
    step = MIN_STEP_S if value in (None, "") else seconds(value, "step")
    if step < MIN_STEP_S:
        raise BuilderError(f"step must be at least {MIN_STEP_S}s")
    return max(step, MIN_STEP_S * math.ceil(range_s / MAX_POINTS / MIN_STEP_S))


def unit_for(text: str) -> str:
    t = (text or "").lower()
    if "rate(" in t or "per_second" in t:
        return "per_second"
    if "_utilization" in t or "_pct" in t or "_percent" in t:
        return "percent"
    if "_bytes" in t or BYTES.search(t):
        return "bytes"
    if "_seconds" in t:
        return "seconds"
    if re.search(r"_ms\b", t):
        return "ms"
    return "plain"


def dotted(underscored: str) -> tuple[str, str]:
    """('do.droplets', 'do.droplets.cpu_utilization') for do_droplets_cpu_utilization. A heuristic: the catalog
    returns only the underscored form, so the family is matched against the known family names."""
    rest = underscored[3:] if underscored.startswith("do_") else underscored
    family = next((f for f in sorted(FAMILIES, key=len, reverse=True) if rest.startswith(f + "_")), None)
    if family is None:
        family = rest.split("_", 1)[0]
    tail = rest[len(family) + 1:]
    return f"do.{family}", f"do.{family}.{tail}" if tail else f"do.{family}"
