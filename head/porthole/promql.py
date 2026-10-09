"""Builder-mode PromQL for visitors and the checks on raw PromQL for the captain, plus unit heuristics.

Every builder query is pinned to its region and to the fleet's resource names, so a visitor cannot send a
discovery query to the shared team account. Metric names are written dotted, as Insights expects (finding A4)."""
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
            "serverless", "nat_gateways", "nfs", "vector_databases", "volumes", "gpu_droplets")
DURATION = re.compile(r"^(\d+)([smhd]?)$")


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
    return {"resource_name": {e.name for e in entities}, "resource_urn": {e.urn for e in entities if e.urn},
            "service_name": set(fleet.service_names())}


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


def selector(metric: str, filters: dict[str, list[str]], region: str, fleet: Fleet) -> str:
    matchers = [f"resource_region_slug={quote(region)}"]
    if not any(label in filters for label in ("resource_name", "resource_urn")):
        names = sorted(set(fleet.names_in_region(region)) or {e.name for e in fleet.entities()})
        matchers.append(f"resource_name=~{regex_alternation(names)}")
    for label in sorted(filters):
        values = filters[label]
        matchers.append(f"{label}={quote(values[0])}" if len(values) == 1 else f"{label}=~{regex_alternation(values)}")
    return f"{metric}{{{', '.join(matchers)}}}"


def build(metric: str, agg: str | None, filters: dict[str, list[str]], region: str, fleet: Fleet,
          group_by: str = "resource_name") -> str:
    """The query the builder writes, e.g. avg by (resource_name) (do.droplets.cpu_utilization{...})."""
    metric = check_metric(metric)
    agg = (agg or "").strip().lower() or None
    if agg not in (None, "none", *AGGS):
        raise BuilderError(f"agg must be one of {list(AGGS)}")
    check_filters(filters, fleet)
    sel = selector(metric, filters, region, fleet)
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
    if "_bytes" in t:
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
