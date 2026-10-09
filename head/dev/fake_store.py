"""State behind the fake Insights: alert rules, instances with their state machine, channels, and logs search.

Shapes and error texts follow the facts pack: the rule list comes back empty while GET by id works (A2), channels
return *_status objects instead of secrets, logs need time_range and reject string instants (A15), and droplet
logs are not collected unless asked (A6b). Texts the facts pack does not give are marked invented."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fake_logs import generate, matches
from fake_promql import SeriesModel

WINDOW_S = {"EVALUATION_WINDOW_1M": 60, "EVALUATION_WINDOW_5M": 300, "EVALUATION_WINDOW_10M": 600,
            "EVALUATION_WINDOW_15M": 900, "EVALUATION_WINDOW_30M": 1800, "EVALUATION_WINDOW_1H": 3600}
OPS: dict[str, Callable[[float, float], bool]] = {
    "THRESHOLD_OPERATOR_GREATER_THAN": lambda v, x: v > x,
    "THRESHOLD_OPERATOR_GREATER_THAN_OR_EQUAL": lambda v, x: v >= x,
    "THRESHOLD_OPERATOR_LESS_THAN": lambda v, x: v < x,
    "THRESHOLD_OPERATOR_LESS_THAN_OR_EQUAL": lambda v, x: v <= x,
    "THRESHOLD_OPERATOR_EQUAL": lambda v, x: v == x,
    "THRESHOLD_OPERATOR_NOT_EQUAL": lambda v, x: v != x}
RE_ALERTS = {"RE_ALERT_DURATION_30M", "RE_ALERT_DURATION_1H", "RE_ALERT_DURATION_4H", "RE_ALERT_DURATION_NEVER"}
NOT_FOUND = {"id": "not_found", "message": "The resource you were accessing could not be found."}
ERR_TIME_RANGE = {"error": "time_range is required", "code": 3}
ERR_STRING_INSTANT = {"error": "json: cannot unmarshal string into Go value of type map[string]jsontext.Value",
                      "code": 3}
OWNER_ID = 1000001  # an obviously fake owner id
DEFAULT_RULES = Path(__file__).resolve().parents[2] / "watcher" / "alerts" / "round-trip.json"
# A39, 2026-10-09: the team's legacy Monitoring policies mirrored into Insights. GET by id answers, the list never
# shows them, they have no resource filter, so they fire on every Droplet of the team. Ids, names and thresholds
# are the live ones; the specs' other fields are a guess at what the mirror holds.
MIRRORS = (("25cd5489-13f6-430f-a9fd-a9e2676c1ce0", "CPU is running high", 70, "EVALUATION_WINDOW_5M"),
           ("d4bede3a-5365-4afb-83e7-c0bdfbefc149", "CPU Utilization Percent is running high", 50,
            "EVALUATION_WINDOW_30M"))
MIRRORED_AT = "2026-09-18T18:24:32Z"
OUTSIDE_URN = "do:droplet:999999901"  # a team member's Droplet that is not in the fleet


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _public(inst: dict) -> dict:
    return {k: v for k, v in inst.items() if not k.startswith("_")}


def _bad(message: str) -> tuple[int, dict]:
    return 400, {"id": "bad_request", "message": message}


class FakeStore:
    def __init__(self, fleet: Any, model: SeriesModel, now: Callable[[], datetime], public_url: str = "",
                 hook_bearer: str = "", hook_secret: str = "", world: Any = None, droplet_logs: bool = False,
                 head_logs: Callable[[], list[dict]] | None = None, rules_path: Path | None = None,
                 on_notify: Callable[[dict], None] | None = None):
        self.fleet, self.model, self.now, self.world = fleet, model, now, world
        self.public_url, self.hook_bearer, self.hook_secret = public_url.rstrip("/"), hook_bearer, hook_secret
        self.droplet_logs, self.head_logs, self.on_notify = droplet_logs, head_logs, on_notify
        self.rules: dict[str, dict] = {}
        self.instances: list[dict] = []
        self.channels: dict[str, dict] = {}
        self.notifications: list[dict] = []
        self.lock = threading.RLock()
        self._seed(rules_path or DEFAULT_RULES)

    # --- seeding ------------------------------------------------------------------------------

    def _seed(self, rules_path: Path) -> None:
        w = self.fleet.watcher
        created = _iso(self.now() - timedelta(days=2))
        webhook_id = w.channel_webhook_id or str(uuid.uuid4())
        email_id = w.channel_email_id or str(uuid.uuid4())
        status = {"is_set": True, "updated_at": created}  # invented field names; see BUGS.md
        self.channels[webhook_id] = {
            "id": webhook_id, "name": "kraken-head", "channel_type": "CHANNEL_TYPE_WEBHOOK", "created_at": created,
            "webhook": {"url": f"{self.public_url}/hooks/insights", "headers": {"X-Kraken": "1"},
                        "bearer_token_status": dict(status), "signature_status": dict(status)},
            "usage": {"rule_count": 0}}
        self.channels[email_id] = {"id": email_id, "name": "kraken-email", "channel_type": "CHANNEL_TYPE_EMAIL",
                                   "created_at": created, "email": {"to": "alerts@example.com"},
                                   "usage": {"rule_count": 0}}
        templates = json.loads(rules_path.read_text())["rules"] if rules_path.exists() else []
        ids = {r.purpose: r.id for r in w.rules}
        for tpl in templates:
            target = self.fleet.tentacle(tpl.get("target") or "") or (self.fleet.tentacles[0]
                                                                      if self.fleet.tentacles else None)
            if target is None:
                continue
            text = json.dumps(tpl["spec"]).replace("{{URN}}", target.urn).replace("{{CHANNEL}}", webhook_id)
            rid = ids.get(tpl["purpose"]) or str(uuid.uuid4())
            status = "ALERT_RULE_STATUS_" + tpl.get("status", "active").upper()
            self.rules[rid] = {"id": rid, "spec": json.loads(text), "owner_id": OWNER_ID, "created_at": created,
                               "updated_at": created, "status": status}
            if tpl["purpose"] == "round-trip":
                for days in (1, 2):
                    t0 = self.now() - timedelta(days=days, hours=3)
                    self.instances.append(self._instance(rid, target.urn, 71.4 + days, t0, t0 + timedelta(minutes=9)))
        self._seed_mirrors(email_id)
        self._count_usage()

    def _seed_mirrors(self, channel_id: str) -> None:
        for rid, name, critical, window in MIRRORS:
            spec = {"name": name, "query": {"metric": "do.droplets.cpu_utilization"},
                    "thresholds": {"operator": "THRESHOLD_OPERATOR_GREATER_THAN", "critical": critical},
                    "condition": {"window": window}, "re_alert_duration": "RE_ALERT_DURATION_4H",
                    "notification_channels": [{"notification_channel_id": channel_id,
                                               "notify_on": ["SEVERITY_CRITICAL"]}]}
            self.rules[rid] = {"id": rid, "spec": spec, "owner_id": OWNER_ID, "created_at": MIRRORED_AT,
                               "updated_at": MIRRORED_AT, "status": "ALERT_RULE_STATUS_ACTIVE"}
        t0 = self.now() - timedelta(days=1, hours=3)
        rid = MIRRORS[1][0]
        if self.fleet.tentacles:
            urn = self.fleet.tentacles[0].urn
            self.instances.append(self._instance(rid, urn, 52.3, t0, t0 + timedelta(minutes=31)))
        self.instances.append(self._instance(rid, OUTSIDE_URN, 88.0, t0 - timedelta(hours=5),
                                             t0 - timedelta(hours=4)))

    def _count_usage(self) -> None:
        for ch in self.channels.values():
            ch["usage"]["rule_count"] = sum(
                1 for r in self.rules.values()
                for b in r["spec"].get("notification_channels") or [] if b.get("notification_channel_id") == ch["id"])

    @staticmethod
    def _instance(rule_id: str, urn: str, value: float, start: datetime, end: datetime | None,
                  severity: str = "SEVERITY_CRITICAL") -> dict:
        return {"id": str(uuid.uuid4()), "rule_id": rule_id, "severity": severity,
                "status": "ALERT_INSTANCE_STATUS_RESOLVED" if end else "ALERT_INSTANCE_STATUS_ACTIVE",
                "resource_urn": urn, "value": round(value, 2), "triggered_at": _iso(start),
                "resolved_at": _iso(end) if end else None, "muted": False}

    # --- rules -------------------------------------------------------------------------------

    def _validate_spec(self, spec: Any, creating: bool) -> tuple[int, dict] | None:
        if not isinstance(spec, dict):
            return _bad("spec is required")
        for key in ("name", "query", "thresholds"):
            if not spec.get(key):
                return _bad(f"spec.{key} is required")
        metric = str((spec.get("query") or {}).get("metric") or "")
        if not metric:
            return _bad("spec.query.metric is required")
        if re.match(r"^do_", metric):  # the reference says 422; the live API returned 201 (BUGS.md)
            return 422, {"id": "unprocessable_entity",
                         "message": "metric must be a dotted OpenTelemetry name such as do.droplets.cpu_utilization"}
        th = spec["thresholds"]
        if th.get("operator") not in OPS or (th.get("warning") is None and th.get("critical") is None):
            return _bad("thresholds need an operator and at least one of warning or critical")
        window = (spec.get("condition") or {}).get("window", "EVALUATION_WINDOW_5M")
        if window not in WINDOW_S:
            return _bad(f"unknown evaluation window {window!r}")
        if spec.get("re_alert_duration", "RE_ALERT_DURATION_4H") not in RE_ALERTS:
            return _bad("unknown re_alert_duration")
        channels = spec.get("notification_channels")
        if channels is not None and len(channels) == 0:
            return _bad("notification_channels must not be empty; omit it to keep the bindings")
        if creating and not channels:
            return _bad("at least one notification channel binding is required")
        return None

    def create_rule(self, body: Any) -> tuple[int, dict]:
        spec = (body or {}).get("spec") if isinstance(body, dict) else None
        problem = self._validate_spec(spec, creating=True)
        if problem:
            return problem
        with self.lock:
            rid, now = str(uuid.uuid4()), _iso(self.now())
            spec.setdefault("condition", {"window": "EVALUATION_WINDOW_5M"})
            spec.setdefault("re_alert_duration", "RE_ALERT_DURATION_4H")
            self.rules[rid] = {"id": rid, "spec": spec, "owner_id": OWNER_ID, "created_at": now, "updated_at": now,
                               "status": body.get("status") or "ALERT_RULE_STATUS_ACTIVE"}
            self._count_usage()
            return 201, {"alert_rule": self.rules[rid]}

    def update_rule(self, rid: str, body: Any) -> tuple[int, dict]:
        with self.lock:
            rule = self.rules.get(rid)
            if rule is None:
                return 404, NOT_FOUND
            spec = (body or {}).get("spec") if isinstance(body, dict) else None
            problem = self._validate_spec(spec, creating=False)
            if problem:
                return problem
            if "notification_channels" not in spec:
                spec["notification_channels"] = rule["spec"].get("notification_channels")
            rule["spec"], rule["updated_at"] = spec, _iso(self.now())
            if body.get("status"):
                rule["status"] = body["status"]
                if rule["status"] == "ALERT_RULE_STATUS_PAUSED":
                    for inst in self._active(rid):
                        self._resolve(inst, rule)
            return 200, {"alert_rule": rule}

    # --- instances and the state machine ---------------------------------------------------------

    def _active(self, rid: str, urn: str | None = None) -> list[dict]:
        return [i for i in self.instances if i["rule_id"] == rid and i["status"].endswith("ACTIVE")
                and (urn is None or i["resource_urn"] == urn)]

    def list_instances(self, q: dict) -> dict:
        status = (q.get("status") or "").upper()
        with self.lock:
            items = [i for i in reversed(self.instances)
                     if (not status or i["status"].endswith(status.replace("ALERT_INSTANCE_STATUS_", "")))
                     and (not q.get("rule_id") or i["rule_id"] == q["rule_id"])
                     and (not q.get("resource_urn") or i["resource_urn"] == q["resource_urn"])]
        page, per = max(1, int(q.get("page") or 1)), max(1, min(200, int(q.get("per_page") or 20)))
        pages = max(1, -(-len(items) // per))
        return {"alert_instances": [_public(i) for i in items[(page - 1) * per: page * per]],
                "pagination": {"page": page, "pages": pages, "per_page": per, "total": len(items)}}

    def fire(self, rid: str, urn: str, value: float, severity: str = "SEVERITY_CRITICAL") -> dict:
        """Script a transition to ACTIVE (and notify), as the real rule engine would."""
        with self.lock:
            inst = {**self._instance(rid, urn, value, self.now(), None, severity), "_scripted": True}
            self.instances.append(inst)
            self._notify(self.rules[rid], inst, "ALERT_TRIGGERED")
            return _public(inst)

    def resolve(self, rid: str, urn: str) -> None:
        with self.lock:
            for inst in self._active(rid, urn):
                self._resolve(inst, self.rules[rid])

    def _resolve(self, inst: dict, rule: dict) -> None:
        inst["status"], inst["resolved_at"] = "ALERT_INSTANCE_STATUS_RESOLVED", _iso(self.now())
        self._notify(rule, inst, "ALERT_RESOLVED")

    def evaluate(self) -> None:
        """One pass of the rule engine over active rules, using the synthetic series."""
        t = self.now().timestamp()
        with self.lock:
            for rid, rule in self.rules.items():
                if rule["status"] != "ALERT_RULE_STATUS_ACTIVE":
                    continue
                spec, th = rule["spec"], rule["spec"]["thresholds"]
                name = spec["query"]["metric"].replace(".", "_")
                window = WINDOW_S.get((spec.get("condition") or {}).get("window"), 300)
                urns = spec["query"].get("resource_urns") or []
                for e in self.model.entities:
                    series = [s for s in self.model.series(e.region) if s.metric == name and s.entity is e
                              and (not urns or s.labels["resource_urn"] in urns)]
                    if not series:
                        continue
                    now_v, then_v = self.model.value(series[0], t), self.model.value(series[0], t - window)
                    urn = series[0].labels["resource_urn"]
                    sev = self._severity(th, now_v) if now_v is not None else None
                    held = sev and then_v is not None and self._severity(th, then_v)
                    active = [i for i in self._active(rid, urn) if not i.get("_scripted")]
                    if held and not self._active(rid, urn):
                        inst = self._instance(rid, urn, now_v, self.now(), None, sev)
                        self.instances.append(inst)
                        self._notify(rule, inst, "ALERT_TRIGGERED")
                    elif not sev and now_v is not None and active:
                        for inst in active:
                            self._resolve(inst, rule)
            del self.instances[:-500]

    @staticmethod
    def _severity(th: dict, v: float) -> str | None:
        op = OPS[th["operator"]]
        if th.get("critical") is not None and op(v, th["critical"]):
            return "SEVERITY_CRITICAL"
        if th.get("warning") is not None and op(v, th["warning"]):
            return "SEVERITY_WARNING"
        return None

    def _notify(self, rule: dict, inst: dict, kind: str) -> None:
        for binding in rule["spec"].get("notification_channels") or []:
            ch = self.channels.get(binding.get("notification_channel_id"))
            wanted = binding.get("notify_on") or []
            if ch is None or (wanted and inst["severity"] not in wanted):
                continue
            if ch["channel_type"] != "CHANNEL_TYPE_WEBHOOK":
                self.notifications.append({"channel": ch["id"], "kind": kind, "instance": dict(inst)})
                continue
            # invented payload and signature header: the real ones are undocumented (BUGS.md)
            body = json.dumps({"type": kind, "alert_rule": {"id": rule["id"], "name": rule["spec"]["name"]},
                               "alert_instance": _public(inst), "sent_at": _iso(self.now())}).encode()
            headers = {"content-type": "application/json", "user-agent": "porthole-fake-insights/1.0",
                       "x-porthole-fake": "1", **{k.lower(): v for k, v in ch["webhook"]["headers"].items()}}
            if self.hook_bearer:
                headers["authorization"] = f"Bearer {self.hook_bearer}"
            if self.hook_secret:
                headers["x-signature"] = hmac.new(self.hook_secret.encode(), body, hashlib.sha256).hexdigest()
            delivery = {"url": ch["webhook"]["url"], "headers": headers, "body": body, "kind": kind}
            self.notifications.append({"channel": ch["id"], "kind": kind, "instance": dict(inst)})
            if self.on_notify:
                self.on_notify(delivery)

    # --- logs ----------------------------------------------------------------------------------

    def instant(self, value: Any) -> float | tuple[int, dict]:
        if isinstance(value, str):
            return 400, ERR_STRING_INSTANT
        if not isinstance(value, dict) or len(value) != 1:  # invented text
            return 400, {"error": "a time instant needs exactly one of absolute, relative or unix_nano", "code": 3}
        kind, raw = next(iter(value.items()))
        now = self.now().timestamp()
        try:
            if kind == "unix_nano":
                return int(raw) / 1e9
            if kind == "absolute":
                if re.fullmatch(r"\d+(\.\d+)?", str(raw)):
                    n = float(raw)
                    return n / 1e9 if n > 1e12 else n
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
            if kind == "relative":
                m = re.fullmatch(r"(now)?([+-]?)(\d+)?([smhdw])?", str(raw))
                if not m or (m.group(3) is None and m.group(1) is None):
                    raise ValueError
                unit = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(4) or "s"]
                secs = int(m.group(3) or 0) * unit
                return now + secs if m.group(2) == "+" else now - secs
        except ValueError:
            pass
        return 400, {"error": f"invalid {kind} time instant {raw!r}", "code": 3}  # invented text

    def search_logs(self, region: str, body: Any) -> tuple[int, dict]:
        if not isinstance(body, dict) or not body.get("time_range"):
            return 400, ERR_TIME_RANGE
        tr = body["time_range"]
        if not isinstance(tr, dict):
            return 400, ERR_STRING_INSTANT
        bounds = [self.instant(tr.get("from")), self.instant(tr.get("to"))]
        for b in bounds:
            if isinstance(b, tuple):
                return b
        t0, t1 = bounds
        if t1 - t0 > 7 * 86400:
            return 400, {"error": "time_range must not exceed 7 days", "code": 3}  # invented text
        records =[r for r in generate(self, region, t0, t1) if matches(r, body.get("filter"))]
        order_by = body.get("order_by") or []
        by_ts = len(order_by) == 1 and (order_by[0].get("field") or {}).get("name") == "timestamp"
        desc = not by_ts or order_by[0].get("direction") != "SORT_DIRECTION_ASC"
        records.sort(key=lambda r: r["timestamp"], reverse=desc)
        pag = body.get("pagination") or {}
        limit = max(1, min(1000, int(pag.get("limit") or 100)))
        offset = int(base64.urlsafe_b64decode(pag["cursor"]).decode()) if pag.get("cursor") else 0
        page = records[offset: offset + limit]
        more = by_ts and offset + limit < len(records)
        out: dict[str, Any] = {"pagination": {"has_more": more}}
        if more:
            out["pagination"]["next_cursor"] = base64.urlsafe_b64encode(str(offset + limit).encode()).decode()
        if page:
            out["data"] = [{k: v for k, v in r.items() if not k.startswith("_")} for r in page]
        return 200, out
