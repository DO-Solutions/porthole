"""Synthetic metric series for the fleet and a small PromQL evaluator, used by the fake Insights.

Series move with what the fleet is doing: a cpu scenario on a tentacle lifts its cpu_utilization a minute later,
a log storm or a memory balloon shows up the same way. Names follow the facts pack: queries take dotted names,
results come back underscored, and underscored names in a query are rejected with 422."""
from __future__ import annotations

import math
import re
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

LAG_S = 60.0  # ingestion plus one-minute resolution
# Labels differ per metric (finding A22): the Droplet CPU series carries only these, with no resource_region_slug,
# no resource_name and no host_id, while the Droplet's other series do carry resource_region_slug. A query that pins
# the region by label finds no CPU series (B-026).
DROPLET_BASE = ("__name__", "do_tags", "resource_urn", "service_name")
# A37, tor1 2026-10-09 06:08Z: on top of the CPU series' four, the memory series carry the region slug, the
# filesystem series the region slug and three filesystem_* labels, the network series the region slug and
# network_device. resource_name stays on the list for the droplet_names flag (older Droplets carry it).
FS_LABELS = ("resource_region_slug", "resource_name", "filesystem_device", "filesystem_mountpoint", "filesystem_type")
NET_LABELS = ("resource_region_slug", "resource_name", "network_device")
MEM_LABELS = ("resource_region_slug", "resource_name")
ONLY_LABELS = {"do_droplets_cpu_utilization": DROPLET_BASE,
               "do_droplets_memory_utilization": (*DROPLET_BASE, *MEM_LABELS),
               "do_droplets_memory_available": (*DROPLET_BASE, *MEM_LABELS),
               "do_droplets_filesystem_free": (*DROPLET_BASE, *FS_LABELS),
               "do_droplets_filesystem_size": (*DROPLET_BASE, *FS_LABELS),
               "do_droplets_network_rx": (*DROPLET_BASE, *NET_LABELS),
               "do_droplets_network_tx": (*DROPLET_BASE, *NET_LABELS),
               # B-033, tor1 2026-10-09: the load balancer's connection series carry these three, and every
               # do_functions_* series carries resource_urn and nothing else
               "do_load_balancers_connections_active": ("__name__", "do_tags", "resource_urn", "service_name"),
               "do_functions_activations": ("__name__", "resource_urn"),
               "do_functions_avg_duration": ("__name__", "resource_urn"),
               "do_functions_errors_total": ("__name__", "resource_urn")}
DROPLET_TAGS = '["insights-demo","kraken-tentacle"]'  # finding A23 (the real label also lists two project ids)
LB_TAGS = '["insights-demo"]'  # the label set is observed (B-033); this value is a stand-in
ROOT_FS = {"filesystem_mountpoint": "/", "filesystem_device": "/dev/vda1", "filesystem_type": "ext4"}
# A37: the managed database's volumes show up in the region's label values too, so a label/<name>/values call
# without match[] returns them next to the tentacles' "/". The service id here is a stand-in.
DB_FS = tuple({"filesystem_mountpoint": f"/srv/aiven-persistent/kraken-pg-{v}", "filesystem_type": "ext4",
               "filesystem_device": f"/dev/mapper/kraken-pg-{v}"} for v in ("service", "scratch"))
# Counters: the value only grows, and rate()/increase() over them are computed from two samples.
COUNTERS = {"do_load_balancers_http_responses_by_status"}
# The database series SeriesModel._database values on the two-minute cadence of A40
DB_TWO_MINUTE = {"do_databases_cpu_utilization", "do_databases_load_avg_1m", "do_databases_pg_connections_active"}

# underscored name -> (unit, label variants). Every name is in watcher/catalog (scripts/check_metric_names.py
# checks); the values are synthetic.
FAMILIES: dict[str, dict[str, tuple[str, tuple[dict, ...]]]] = {
    "tentacle": {
        "do_droplets_cpu_utilization": ("percent", ({},)),
        "do_droplets_cpu_time": ("seconds", tuple({"cpu_mode": m} for m in ("user", "system", "idle", "iowait"))),
        "do_droplets_load_avg_1m": ("plain", ({},)), "do_droplets_load_avg_5m": ("plain", ({},)),
        "do_droplets_load_avg_15m": ("plain", ({},)),
        "do_droplets_memory_utilization": ("percent", ({},)),
        "do_droplets_memory_available": ("bytes", ({},)),
        "do_droplets_disk_write": ("bytes", ({"disk_device": "vda"},)),
        "do_droplets_disk_read": ("bytes", ({"disk_device": "vda"},)),
        "do_droplets_filesystem_free": ("bytes", (ROOT_FS,)),
        "do_droplets_filesystem_size": ("bytes", (ROOT_FS,)),
        "do_droplets_network_rx": ("bytes", ({"network_device": "eth0"}, {"network_device": "eth1"})),
        "do_droplets_network_tx": ("bytes", ({"network_device": "eth0"}, {"network_device": "eth1"})),
    },
    "app": {
        "do_apps_app_cpu_utilization": ("percent", ({},)), "do_apps_app_memory_utilization": ("percent", ({},)),
        "do_apps_app_requests_per_second": ("per_second", ({},)),
        "do_apps_app_request_duration_p95": ("seconds", ({},)), "do_apps_app_replicas_ready": ("plain", ({},)),
    },
    "load_balancer": {
        "do_load_balancers_http_responses_by_status": ("plain", ({},)),  # a counter; rps is rate() of it (A35)
        "do_load_balancers_connections_active": ("plain", ({},)),
        "do_load_balancers_http_error_count_5xx": ("plain", ({},)),
    },
    "database": {
        "do_databases_cpu_utilization": ("percent", ({},)), "do_databases_memory_utilization": ("percent", ({},)),
        "do_databases_load_avg_1m": ("plain", ({},)),
        "do_databases_pg_connections_active": ("plain", ({},)), "do_databases_filesystem_free": ("bytes", DB_FS),
    },
    "kubernetes": {
        "do_kubernetes_node_cpu_utilization": ("percent", ({},)),
        "do_kubernetes_node_memory_utilization": ("percent", ({},)),
    },
    "functions": {"do_functions_activations": ("plain", ({},)), "do_functions_avg_duration": ("ms", ({},)),
                  "do_functions_errors_total": ("plain", ({},))},
    "spaces": {"do_spaces_requests": ("plain", ({"spaces_operation": "GET"}, {"spaces_operation": "PUT"}))},
    "registry": {"do_container_registry_storage_used": ("bytes", ({},))},
}


class PromError(Exception):
    def __init__(self, status: int, error_type: str, message: str):
        super().__init__(message)
        self.status, self.error_type, self.message = status, error_type, message


@dataclass
class Series:
    labels: dict
    entity: Any  # porthole.config.Entity
    metric: str


def _noise(key: str, t: float) -> float:
    return zlib.crc32(f"{key}:{int(t // 60)}".encode()) / 2**32


def _wave(key: str, t: float, period: float) -> float:
    phase = (zlib.crc32(key.encode()) % 360) * math.pi / 180
    return math.sin(2 * math.pi * t / period + phase)


@dataclass
class SeriesModel:
    """All series of the fleet, valued at any unix time from the world's scenario runs."""
    fleet: Any
    world: Any = None
    now: Callable[[], float] | None = None
    droplet_names: bool = False  # fresh Droplets report no resource_name (B-023); True gives them one
    entities: list = field(default_factory=list)
    _by_region: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.entities = [e for e in self.fleet.entities() if e.kind in FAMILIES and e.region]
        self._by_region: dict[str, list[Series]] = {}

    def series(self, region: str) -> list[Series]:
        if region not in self._by_region:
            self._by_region[region] = self._build(region)
        return self._by_region[region]

    def _build(self, region: str) -> list[Series]:
        out = []
        for e in self.entities:
            if e.region != region:
                continue
            for metric, (_unit, variants) in FAMILIES[e.kind].items():
                for extra in variants:
                    labels = {"__name__": metric, "resource_name": e.name,
                              "resource_urn": e.urn or f"do:{e.kind}:{e.ids.get('id') or e.name}",
                              "resource_region_slug": region, **extra}
                    if e.kind == "tentacle" and not self.droplet_names:
                        del labels["resource_name"]
                    if e.kind == "tentacle" and e.ids.get("id"):
                        labels["host_id"] = str(e.ids["id"])
                    if e.kind == "tentacle":
                        labels |= {"do_tags": DROPLET_TAGS, "service_name": e.service_name or e.name}
                    if e.kind == "load_balancer":
                        labels |= {"do_tags": LB_TAGS, "service_name": e.name}
                    if metric in ONLY_LABELS:
                        labels = {k: v for k, v in labels.items() if k in ONLY_LABELS[metric]}
                    out.append(Series(labels, e, metric))
        return out

    def catalog(self, region: str) -> list[str]:
        return sorted({s.metric for s in self.series(region)})

    def _active(self, entity: Any, scenario: str, t: float) -> list[dict]:
        if self.world is None:
            return []
        at = t - LAG_S
        if entity.kind == "tentacle":
            runs = self.world.runs(entity.name)
        elif entity.kind == "database" and scenario == "pg":
            runs = self.world.runs_any("pg")  # any tentacle's pg run loads the one database
        else:
            return []
        out = []
        for r in runs:
            if r["name"] != scenario:
                continue
            start = r["started_at"].timestamp()
            end = r["ended_at"].timestamp() if r.get("ended_at") else (self.now() if self.now else at + 1)
            if start <= at < end:
                out.append(r)
        return out

    def value(self, s: Series, t: float) -> float | None:
        e, m, key = s.entity, s.metric, f"{s.metric}:{sorted(s.labels.items())}"
        if e.slot == 3 and int(t // 60) % 23 == 0:
            return None  # one tentacle drops a sample now and then, so gaps show as gaps
        if m in DB_TWO_MINUTE:
            return self._database(s, t)
        n, w = _noise(key, t), _wave(key, t, 1800)
        if m.endswith("cpu_utilization"):
            v = 2.5 + 1.5 * n + w
            if e.kind == "tentacle" and self._active(e, "cpu", t):
                v = 95.5 + 3 * n
            return round(min(100.0, max(0.0, v)), 3)
        if m.startswith("do_droplets_load_"):
            runs = self._active(e, "cpu", t)
            return round(0.05 + 0.1 * n + sum(r["params"].get("workers", 1) for r in runs), 3)
        if m.endswith("memory_utilization"):
            v = 28 + 4 * n + 2 * w
            for r in self._active(e, "memory", t):
                v += r["params"].get("mb", 0) / 1024 * 100
            return round(min(100.0, v), 3)
        if m == "do_droplets_memory_available":
            held = sum(r["params"].get("mb", 0) for r in self._active(e, "memory", t))
            return float((700 - held + 20 * n) * 2**20)
        if m.startswith("do_droplets_filesystem_free"):
            used = sum(r["params"].get("mb", 0) for r in self._active(e, "disk", t))
            return float((18_000 - used) * 2**20)
        if m.startswith("do_droplets_filesystem_size"):
            return float(25_000 * 2**20)
        if m.startswith("do_droplets_disk_"):
            busy = self._active(e, "disk", t) if "write" in m else []
            return float((40_000 + 10_000 * n) + sum(r["params"].get("mb", 0) * 2**20 / 60 for r in busy))
        if m.startswith("do_droplets_network_"):
            v = 2_000 + 1_000 * n
            if s.labels.get("network_device") == "eth1":
                for r in self._active(e, "network", t):
                    v += r["params"].get("mbps", 0) * 125_000
            return float(v)
        if m == "do_droplets_cpu_time":
            mode = s.labels.get("cpu_mode")
            busy = 95.0 if self._active(e, "cpu", t) else 3.0
            share = {"user": busy * 0.8, "system": busy * 0.2, "idle": 100 - busy, "iowait": 0.3}.get(mode, 0)
            return round(t / 100 * share / 100, 3)
        if m == "do_apps_app_requests_per_second":
            extra = self.world.head_rate(t - LAG_S) if self.world is not None else 0.0
            return round(0.8 + 0.6 * n + extra, 3)
        if m == "do_apps_app_request_duration_p95":
            return round(0.04 + 0.03 * n, 4)
        if m == "do_apps_app_replicas_ready":
            return 1.0
        if m == "do_load_balancers_http_responses_by_status":
            return float(int(t * 0.05) + self._load(t, "lb", since=0))  # a trickle of 0.05 rps plus the lb load
        if m == "do_load_balancers_connections_active":
            return float(round(self._load(t, "lb") / 400))  # 0 at rest, 3 under 20 rps (B-033)
        if m == "do_functions_activations":
            return float(self._load(t, "fn"))
        if m == "do_functions_avg_duration":
            return round(12 + 6 * n, 2)
        if m == "do_functions_errors_total":
            return 0.0
        return round(10 + 5 * n + 3 * w, 3)

    def _database(self, s: Series, t: float) -> float | None:
        """A40, tor1 2026-10-09: database metrics arrive every two minutes. The CPU series stores NaN in the minute
        between, and the load average carries the two-minute value forward, so its values come in pairs. Deep water
        v-670380 under 8 pg clients for 180 s: CPU 15 to 21 %, load average 0.16 to 0.97, and
        pg_connections_active 0 at every sample."""
        m, minute = s.metric, int(t // 60)
        key = f"{m}:{sorted(s.labels.items())}"
        t2 = (minute - minute % 2) * 60.0
        n, w = _noise(key, t2), _wave(key, t2, 1800)
        clients = sum(r["params"].get("clients", 0) for r in self._active(s.entity, "pg", t2))
        if m == "do_databases_cpu_utilization":
            if minute % 2:
                return math.nan
            return round(15 + 2 * n + w + (6 if clients else 0), 3)
        if m == "do_databases_load_avg_1m":
            return round(0.16 + 0.1 * n + 0.1 * clients, 3)
        return 0.0  # do_databases_pg_connections_active

    def _load(self, t: float, kind: str, since: float | None = None) -> int:
        """Requests of this kind in the minute before t less the lag, or from since to then."""
        if self.world is None:
            return 0
        end = t - LAG_S
        return self.world.requests(kind, end - 60 if since is None else since, end)


# --- the evaluator ------------------------------------------------------------------------------

TOKEN = re.compile(r'\s*(?:(?P<str>"(?:[^"\\]|\\.)*")|(?P<num>\d+(?:\.\d+)?(?:[smhdw](?![a-zA-Z_]))?)'
                   r"|(?P<op>=~|!~|!=|==|[=(){}\[\],*/+-])|(?P<id>[A-Za-z_][A-Za-z0-9_.:]*))")
AGGS = {"sum", "avg", "max", "min", "count"}
RANGE_FUNCS = {"rate", "irate", "increase", "avg_over_time", "max_over_time", "min_over_time", "last_over_time"}


def tokenize(q: str) -> list[tuple[str, str]]:
    pos, out = 0, []
    q = q.strip()
    while pos < len(q):
        m = TOKEN.match(q, pos)
        if not m or m.end() == pos:
            raise PromError(400, "bad_data", f"1:{pos + 1}: parse error: unexpected character {q[pos]!r}")
        kind = m.lastgroup or "op"
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]):
        self.t, self.i = tokens, 0

    def peek(self, value: str | None = None) -> tuple[str, str] | None:
        tok = self.t[self.i] if self.i < len(self.t) else None
        if value is not None and (tok is None or tok[1] != value):
            return None
        return tok

    def take(self, value: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if tok is None or (value is not None and tok[1] != value):
            raise PromError(400, "bad_data", f"parse error: expected {value or 'more input'}, got "
                                             f"{tok[1] if tok else 'end of input'}")
        self.i += 1
        return tok

    def expr(self) -> tuple:
        left = self.term()
        while self.peek() and self.peek()[1] in "+-*/" and self.peek()[0] == "op":
            op = self.take()[1]
            left = ("bin", op, left, self.term())
        return left

    def term(self) -> tuple:
        kind, val = self.take()
        if kind == "num":
            return ("num", float(val))
        if val == "(":
            inner = self.expr()
            self.take(")")
            return inner
        if val == "{":
            return self.selector(None)
        if kind != "id":
            raise PromError(400, "bad_data", f"parse error: unexpected {val!r}")
        if val in AGGS:
            by = self.grouping()
            self.take("(")
            arg = self.expr()
            self.take(")")
            return ("agg", val, by if by is not None else self.grouping(), arg)
        if val in RANGE_FUNCS:
            self.take("(")
            arg = self.expr()
            self.take(")")
            if arg[0] != "sel" or not arg[3]:
                raise PromError(400, "bad_data", f"parse error: {val} needs a range vector like x[5m]")
            return ("func", val, arg)
        if self.peek("("):
            raise PromError(400, "bad_data", f"parse error: function {val!r} is not supported by the fake")
        if self.peek("{"):
            self.take("{")
            return self.selector(val)
        return self.range_suffix(("sel", val, [], 0))

    def grouping(self) -> list[str] | None:
        if self.peek("by") or self.peek("without"):
            if self.take()[1] == "without":
                raise PromError(400, "bad_data", "parse error: 'without' is not supported by the fake")
            self.take("(")
            labels = []
            while not self.peek(")"):
                labels.append(self.take()[1])
                if self.peek(","):
                    self.take(",")
            self.take(")")
            return labels
        return None

    def selector(self, metric: str | None) -> tuple:
        matchers = []
        while not self.peek("}"):
            label = self.take()[1]
            op = self.take()[1]
            if op not in ("=", "!=", "=~", "!~"):
                raise PromError(400, "bad_data", f"parse error: unexpected matcher operator {op!r}")
            kind, raw = self.take()
            if kind != "str":
                raise PromError(400, "bad_data", "parse error: label values must be quoted strings")
            matchers.append((label, op, bytes(raw[1:-1], "utf-8").decode("unicode_escape")))
            if self.peek(","):
                self.take(",")
        self.take("}")
        if metric is None and not any(op in ("=", "=~") and v for _, op, v in matchers):
            raise PromError(400, "bad_data", "vector selector must contain at least one non-empty matcher")
        return self.range_suffix(("sel", metric, matchers, 0))

    def range_suffix(self, sel: tuple) -> tuple:
        if self.peek("["):
            self.take("[")
            kind, val = self.take()
            self.take("]")
            sel = (sel[0], sel[1], sel[2], parse_duration(val))
        return sel


def parse_duration(text: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhdw]?)", str(text).strip())
    if not m:
        raise PromError(400, "bad_data", f"invalid duration {text!r}")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]


def parse(q: str) -> tuple:
    if re.search(r"(?<![\w.\"])do_[a-z0-9_]+", re.sub(r'"(?:[^"\\]|\\.)*"', '""', q)):
        raise PromError(422, "bad_data", "metric names must use the dotted OpenTelemetry form, "
                                         "for example do.droplets.cpu_utilization")
    p = _Parser(tokenize(q))
    tree = p.expr()
    if p.peek():
        raise PromError(400, "bad_data", f"parse error: unexpected {p.peek()[1]!r}")
    return tree


def _match(labels: dict, matchers: list) -> bool:
    for label, op, value in matchers:
        have = labels.get(label, "")
        if op == "=" and have != value or op == "!=" and have == value:
            return False
        if op == "=~" and not re.fullmatch(value, have) or op == "!~" and re.fullmatch(value, have):
            return False
    return True


def select(model: SeriesModel, region: str, metric: str | None, matchers: list) -> list[Series]:
    name = metric.replace(".", "_") if metric else None
    return [s for s in model.series(region) if (name is None or s.metric == name) and _match(s.labels, matchers)]


def evaluate(model: SeriesModel, region: str, tree: tuple, t: float) -> list[tuple[dict, float]]:
    """Instant vector at time t: [(labels, value)]."""
    kind = tree[0]
    if kind == "num":
        return [({}, tree[1])]
    if kind == "sel":
        out = []
        for s in select(model, region, tree[1], tree[2]):
            v = model.value(s, t)
            if v is not None:
                out.append((dict(s.labels), v))
        return out
    if kind == "func":
        _, fn, (_, metric, matchers, window) = tree
        out = []
        for s in select(model, region, metric, matchers):
            v = model.value(s, t)
            if v is not None and s.metric in COUNTERS and fn in ("rate", "irate", "increase"):
                before = model.value(s, t - window)
                v = None if before is None else max(0.0, v - before) / (1 if fn == "increase" else window)
            if v is not None:
                out.append(({k: x for k, x in s.labels.items() if k != "__name__"}, v))
        return out
    if kind == "agg":
        _, op, by, arg = tree
        groups: dict[tuple, list[float]] = {}
        keys: dict[tuple, dict] = {}
        for labels, v in evaluate(model, region, arg, t):
            g = {k: labels[k] for k in (by or []) if k in labels}
            gk = tuple(sorted(g.items()))
            groups.setdefault(gk, []).append(v)
            keys[gk] = g
        fn = {"sum": sum, "max": max, "min": min, "count": len, "avg": lambda xs: sum(xs) / len(xs)}[op]
        return [(keys[k], float(fn(vs))) for k, vs in groups.items()]
    if kind == "bin":
        _, op, a, b = tree
        left, right = evaluate(model, region, a, t), evaluate(model, region, b, t)
        if a[0] == "num":
            left, right, swap = right, left, True
        else:
            swap = False
        if not right or right[0][0]:
            raise PromError(400, "bad_data", "the fake only supports arithmetic between a vector and a number")
        k = right[0][1]
        ops = {"+": lambda x, y: x + y, "-": lambda x, y: x - y, "*": lambda x, y: x * y,
               "/": lambda x, y: x / y if y else math.nan}
        return [({n: v for n, v in lbl.items() if n != "__name__"},
                 ops[op](k, val) if swap else ops[op](val, k)) for lbl, val in left]
    raise PromError(400, "bad_data", "parse error")
