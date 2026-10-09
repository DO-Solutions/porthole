#!/usr/bin/env python3
"""DigitalOcean Insights API harness: a small library and CLI over the Insights API.

Library:  ins = Insights(os.environ["DIGITALOCEAN_TOKEN"], region="tor1", trace=True)
CLI:      python3 insights_harness.py <group> <verb> [args] [--region R] [--json] [--trace]

Shapes follow the public OpenAPI spec (digitalocean/openapi, specification/resources/insights)
and the verified facts pack (FACTS-2026-10-08.md). The token is read from DIGITALOCEAN_TOKEN and
is never printed or logged; --trace output redacts secret fields in request bodies.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator

import httpx

BASE_URL = "https://api.digitalocean.com"
DEFAULT_DISCOVERY_WINDOW = timedelta(minutes=30)  # facts A1: unwindowed discovery times out
MAX_RATE_LIMIT_SLEEP = 120.0

# Regions listed on the availability page (facts §1); mkc1/mem1 have empty feature cells (A3).
REGIONS = ["nyc1", "nyc2", "nyc3", "ams3", "sfo2", "sfo3", "sgp1", "lon1", "fra1", "tor1",
           "blr1", "syd1", "atl1", "ric1", "mkc1", "mem1"]

# --- enums (alert_rule_spec.yml, alert_condition.yml, alert_thresholds.yml, ...) -------------

WINDOWS = {"1m": "EVALUATION_WINDOW_1M", "5m": "EVALUATION_WINDOW_5M", "10m": "EVALUATION_WINDOW_10M",
           "15m": "EVALUATION_WINDOW_15M", "30m": "EVALUATION_WINDOW_30M", "1h": "EVALUATION_WINDOW_1H"}
THRESHOLD_OPERATORS = {
    ">": "THRESHOLD_OPERATOR_GREATER_THAN", ">=": "THRESHOLD_OPERATOR_GREATER_THAN_OR_EQUAL",
    "<": "THRESHOLD_OPERATOR_LESS_THAN", "<=": "THRESHOLD_OPERATOR_LESS_THAN_OR_EQUAL",
    "=": "THRESHOLD_OPERATOR_EQUAL", "==": "THRESHOLD_OPERATOR_EQUAL", "!=": "THRESHOLD_OPERATOR_NOT_EQUAL",
    "gt": "THRESHOLD_OPERATOR_GREATER_THAN", "gte": "THRESHOLD_OPERATOR_GREATER_THAN_OR_EQUAL",
    "lt": "THRESHOLD_OPERATOR_LESS_THAN", "lte": "THRESHOLD_OPERATOR_LESS_THAN_OR_EQUAL",
    "eq": "THRESHOLD_OPERATOR_EQUAL", "ne": "THRESHOLD_OPERATOR_NOT_EQUAL", "neq": "THRESHOLD_OPERATOR_NOT_EQUAL",
}
QUERY_FILTER_OPERATORS = {k: v.replace("THRESHOLD_OPERATOR_", "FILTER_OPERATOR_")
                          for k, v in THRESHOLD_OPERATORS.items()}
RE_ALERT = {"30m": "RE_ALERT_DURATION_30M", "1h": "RE_ALERT_DURATION_1H", "4h": "RE_ALERT_DURATION_4H",
            "never": "RE_ALERT_DURATION_NEVER"}
SEVERITIES = {"warning": "SEVERITY_WARNING", "critical": "SEVERITY_CRITICAL"}
RULE_STATUSES = {"active": "ALERT_RULE_STATUS_ACTIVE", "paused": "ALERT_RULE_STATUS_PAUSED"}

# logs_filter_condition.yml / logs_field_ref.yml / logs_order_by.yml
LOG_OPERATORS = {
    "=": "FILTER_OPERATOR_EQ", "==": "FILTER_OPERATOR_EQ", "eq": "FILTER_OPERATOR_EQ",
    "!=": "FILTER_OPERATOR_NEQ", "neq": "FILTER_OPERATOR_NEQ", "ne": "FILTER_OPERATOR_NEQ",
    "in": "FILTER_OPERATOR_IN", "exists": "FILTER_OPERATOR_EXISTS",
    ">=": "FILTER_OPERATOR_GTE", "gte": "FILTER_OPERATOR_GTE",
    "<=": "FILTER_OPERATOR_LTE", "lte": "FILTER_OPERATOR_LTE",
}
FIELD_SCOPES = {"resource": "FIELD_SCOPE_RESOURCE", "attributes": "FIELD_SCOPE_ATTRIBUTES"}
SORT_DIRECTIONS = {"asc": "SORT_DIRECTION_ASC", "desc": "SORT_DIRECTION_DESC"}

SECRET_KEYS = {"token", "password", "secret", "webhook_url", "authorization"}


def _enum(value: str, table: dict[str, str], what: str) -> str:
    """Map a short alias ('>=', '5m', 'critical') or the full enum string to the full enum."""
    if value in table.values():
        return value
    key = str(value).strip().lower()
    if key in table:
        return table[key]
    raise ValueError(f"unknown {what} {value!r}; use one of {sorted(set(table) | set(table.values()))}")


class InsightsError(Exception):
    """A non-2xx (or non-JSON) response from the Insights API."""

    def __init__(self, status: int, body: Any, request_summary: str):
        self.status = status
        self.body = body
        self.request_summary = request_summary
        super().__init__(f"{request_summary} -> HTTP {status}: {excerpt(body)}")


def excerpt(body: Any, limit: int = 300) -> str:
    if not isinstance(body, str):
        body = json.dumps(body, separators=(",", ":"))
    m = re.search(r"<title>(.*?)</title>", body, re.I | re.S)
    if m:  # HTML error pages (mkc1 maintenance page, A3): the title says it all
        body = f"[html] title={m.group(1).strip()!r} " + body
    body = " ".join(body.split())
    return body if len(body) <= limit else body[:limit] + "…"


def redact(obj: Any) -> Any:
    """Copy of a request body with write-only secrets masked, for --trace output."""
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in SECRET_KEYS and v else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def _ts(value: Any) -> str:
    """Prometheus time parameter: datetime -> RFC3339, numbers -> unix seconds, strings as-is."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, (int, float)):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _window(start: Any, end: Any) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    end = now if end is None else end
    if start is None:
        start = (end if isinstance(end, datetime) else now) - DEFAULT_DISCOVERY_WINDOW
    return _ts(start), _ts(end)


# --- logs filter-tree builders (logs_filter_expression.yml and friends) ----------------------

def field(name: str | dict, scope: str | None = None) -> dict:
    """logs_field_ref: {"name": ..., "scope"?: FIELD_SCOPE_*}."""
    if isinstance(name, dict):
        return name
    ref: dict[str, Any] = {"name": name}
    if scope:
        ref["scope"] = _enum(scope, FIELD_SCOPES, "field scope")
    return ref


def filter_value(value: Any) -> dict:
    """logs_filter_value: exactly one of string/number/bool/string_array/number_array."""
    if isinstance(value, dict):
        return value
    if isinstance(value, bool):
        return {"bool_value": value}
    if isinstance(value, (int, float)):
        return {"number_value": value}
    if isinstance(value, str):
        return {"string_value": value}
    if isinstance(value, (list, tuple, set)):
        values = list(value)
        if not values:
            raise ValueError("array filter values need at least one item")
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
            return {"number_array_value": {"values": values}}
        if all(isinstance(v, str) for v in values):
            return {"string_array_value": {"values": values}}
        raise ValueError("array filter values must be all strings or all numbers")
    raise TypeError(f"unsupported filter value type {type(value).__name__}")


def cond(name: str | dict, op: str, value: Any = None, scope: str | None = None) -> dict:
    """A condition node: cond("severity_text", "=", "Error"), cond("trace_id", "exists")."""
    operator = _enum(op, LOG_OPERATORS, "logs filter operator")
    condition: dict[str, Any] = {"field": field(name, scope), "operator": operator}
    if operator == "FILTER_OPERATOR_EXISTS":
        if value is not None:
            raise ValueError("FILTER_OPERATOR_EXISTS takes no value")
    else:
        if value is None:
            raise ValueError(f"{operator} needs a value")
        if operator == "FILTER_OPERATOR_IN" and not isinstance(value, (list, tuple, set, dict)):
            value = [value]
        condition["value"] = filter_value(value)
    return {"condition": condition}


def _expressions(exprs: tuple) -> list[dict]:
    if len(exprs) == 1 and isinstance(exprs[0], (list, tuple)):
        exprs = tuple(exprs[0])
    for e in exprs:
        if not (isinstance(e, dict) and len(e) == 1):
            raise ValueError("each filter expression must set exactly one node")
    return list(exprs)


def and_(*exprs: dict) -> dict:
    return {"and": {"expressions": _expressions(exprs)}}


def or_(*exprs: dict) -> dict:
    return {"or": {"expressions": _expressions(exprs)}}


def not_(expr: dict) -> dict:
    return {"not": _expressions((expr,))[0]}


def text(search: str) -> dict:
    """Substring search across body, service name and resource URN."""
    return {"text_search": {"query": search}}


def order(name: str = "timestamp", direction: str | None = "desc") -> dict:
    """logs_order_by: {"field": {"name": ...}, "direction"?: SORT_DIRECTION_*}."""
    clause: dict[str, Any] = {"field": field(name)}
    if direction:
        clause["direction"] = _enum(direction, SORT_DIRECTIONS, "sort direction")
    return clause


_RELATIVE = re.compile(r"^(now([+-]\d+[smhdw])?|\d+[smhdw])$")


def time_instant(value: Any) -> dict:
    """logs_time_instant: exactly one of absolute / relative / unix_nano."""
    if isinstance(value, dict):
        return value
    if isinstance(value, datetime):
        return {"absolute": _ts(value)}
    if isinstance(value, (int, float)):
        return {"unix_nano": str(int(value * 1e9)) if value < 1e12 else str(int(value))}
    value = str(value)
    if _RELATIVE.match(value):
        return {"relative": value}
    return {"absolute": value}


# --- channel and rule spec helpers ----------------------------------------------------------

def email_channel(name: str, to: str | Iterable[str]) -> dict:
    if not isinstance(to, str):
        to = ", ".join(to)
    return {"name": name, "email": {"to": to}}


def slack_webhook_channel(name: str, url: str, channel: str) -> dict:
    return {"name": name, "slack": {"webhook_url": url, "channel": channel}}


def webhook_channel(name: str, url: str, bearer: str | None = None, basic: tuple[str, str] | None = None,
                    secret: str | None = None, headers: dict[str, str] | None = None) -> dict:
    if not url.startswith("https://"):
        raise ValueError("webhook url must be https")
    if bearer and basic:
        raise ValueError("configure either basic auth or a bearer token, not both")
    hook: dict[str, Any] = {"url": url}
    if bearer:
        hook["bearer_token"] = {"token": bearer}
    if basic:
        hook["basic_auth"] = {"username": basic[0], "password": basic[1]}
    if headers:
        if len(headers) > 20:
            raise ValueError("at most 20 custom headers")
        hook["headers"] = dict(headers)
    if secret:
        hook["signature"] = {"secret": secret}
    return {"name": name, "webhook": hook}


def rule_spec(name: str, metric: str, operator: str, warning: float | None = None,
              critical: float | None = None, window: str = "EVALUATION_WINDOW_5M",
              resource_urns: list[str] | None = None, tags: list[str] | None = None,
              filters: list | None = None, channels: list | None = None,
              re_alert: str = "RE_ALERT_DURATION_4H") -> dict:
    """alert_rule_spec. channels = [(channel_id, ["critical", ...]), ...] or bare ids (all severities).
    filters = [(field, op, value), ...] or query_filter dicts. Omitting channels keeps bindings on PUT."""
    if warning is None and critical is None:
        raise ValueError("set warning and/or critical")
    thresholds: dict[str, Any] = {"operator": _enum(operator, THRESHOLD_OPERATORS, "threshold operator")}
    if warning is not None:
        thresholds["warning"] = warning
    if critical is not None:
        thresholds["critical"] = critical
    query: dict[str, Any] = {"metric": metric}
    if filters:
        query["filters"] = [f if isinstance(f, dict) else
                            {"field": f[0], "operator": _enum(f[1], QUERY_FILTER_OPERATORS, "filter operator"),
                             "value": str(f[2])} for f in filters]
    if resource_urns:
        query["resource_urns"] = list(resource_urns)
    if tags:
        query["tags"] = list(tags)
    spec: dict[str, Any] = {"name": name, "query": query,
                            "condition": {"window": _enum(window, WINDOWS, "evaluation window")},
                            "thresholds": thresholds}
    if channels:
        bindings = []
        for ch in channels:
            cid, sev = (ch, None) if isinstance(ch, str) else (ch[0], ch[1] if len(ch) > 1 else None)
            binding: dict[str, Any] = {"notification_channel_id": cid}
            if sev:
                binding["notify_on"] = [_enum(s, SEVERITIES, "severity") for s in sev]
            bindings.append(binding)
        spec["notification_channels"] = bindings
    spec["re_alert_duration"] = _enum(re_alert, RE_ALERT, "re-alert duration")
    return spec


# --- the client -----------------------------------------------------------------------------

class Insights:
    def __init__(self, token: str, region: str | None = None, timeout: float = 60, trace: bool = False,
                 base_url: str = BASE_URL, transport: httpx.BaseTransport | None = None,
                 trace_stream=None, sleep=time.sleep):
        if not token:
            raise ValueError("an API token is required (DIGITALOCEAN_TOKEN)")
        self.region = region
        self.trace = trace
        self.trace_stream = trace_stream or sys.stderr
        self.last_elapsed_ms: float | None = None
        self._sleep = sleep
        self._http = httpx.Client(base_url=base_url, timeout=timeout, transport=transport,
                                  headers={"Authorization": f"Bearer {token}",
                                           "Accept": "application/json",
                                           "User-Agent": "insights-harness/1.0"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # transport ----------------------------------------------------------------------------

    def _trace(self, line: str) -> None:
        if self.trace:
            print(line, file=self.trace_stream, flush=True)

    @staticmethod
    def _summary(method: str, path: str, params: Any) -> str:
        if params:
            items = params.items() if isinstance(params, dict) else params
            flat = [(k, x) for k, v in items for x in (v if isinstance(v, list) else [v]) if x is not None]
            if flat:
                return f"{method} {path}?" + "&".join(f"{k}={x}" for k, x in flat)
        return f"{method} {path}"

    def send(self, method: str, path: str, params: dict | None = None, json_body: Any = None,
             timeout: float | None = None, content: bytes | None = None) -> httpx.Response:
        """One HTTP exchange with tracing and a single 429 retry. Does not raise on HTTP status."""
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        kwargs: dict[str, Any] = {"params": params or None}
        if json_body is not None:
            kwargs["json"] = json_body
        if content is not None:
            kwargs["content"] = content
        if timeout is not None:
            kwargs["timeout"] = timeout
        for attempt in (1, 2):
            self._trace(f"→ {method} {path}" + (f" params={json.dumps(params)}" if params else "")
                        + (f" body={json.dumps(redact(json_body))}" if json_body is not None else ""))
            t0 = time.monotonic()
            try:
                resp = self._http.request(method, path, **kwargs)
            except httpx.HTTPError as e:
                self.last_elapsed_ms = (time.monotonic() - t0) * 1000
                self._trace(f"← {type(e).__name__} after {self.last_elapsed_ms:.0f} ms")
                raise
            self.last_elapsed_ms = (time.monotonic() - t0) * 1000
            self._trace(f"← {resp.status_code} {resp.headers.get('content-type', '')} "
                        f"in {self.last_elapsed_ms:.0f} ms")
            if resp.status_code == 429 and attempt == 1:
                delay = self._rate_limit_delay(resp)
                self._trace(f"  rate limited; sleeping {delay:.1f} s then retrying once")
                self._sleep(delay)
                continue
            return resp
        return resp  # pragma: no cover

    @staticmethod
    def _rate_limit_delay(resp: httpx.Response) -> float:
        # DO's ratelimit-reset is a unix epoch (seconds); tolerate a delta-seconds value too.
        raw = resp.headers.get("ratelimit-reset") or resp.headers.get("retry-after")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 1.0
        delay = value - time.time() if value > 1e9 else value
        return min(max(delay, 0.5), MAX_RATE_LIMIT_SLEEP)

    def request(self, method: str, path: str, params: dict | None = None, json_body: Any = None,
                timeout: float | None = None) -> Any:
        """Send and return parsed JSON; raise InsightsError on non-2xx or non-JSON bodies."""
        resp = self.send(method, path, params=params, json_body=json_body, timeout=timeout)
        summary = self._summary(method, path, params)
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            raise InsightsError(resp.status_code, resp.text, summary) from None
        if resp.is_error:
            raise InsightsError(resp.status_code, body, summary)
        return body

    # notification channels ------------------------------------------------------------------

    def list_channels(self, page: int | None = None, per_page: int | None = None) -> dict:
        return self.request("GET", "/v2/insights/notification-channels", {"page": page, "per_page": per_page})

    def get_channel(self, channel_id: str) -> dict:
        return self.request("GET", f"/v2/insights/notification-channels/{channel_id}")

    def create_channel(self, spec: dict) -> dict:
        return self.request("POST", "/v2/insights/notification-channels", json_body=spec)

    def update_channel(self, channel_id: str, spec: dict) -> dict:
        return self.request("PUT", f"/v2/insights/notification-channels/{channel_id}", json_body=spec)

    def delete_channel(self, channel_id: str) -> dict:
        return self.request("DELETE", f"/v2/insights/notification-channels/{channel_id}")

    email_channel = staticmethod(email_channel)
    slack_webhook_channel = staticmethod(slack_webhook_channel)
    webhook_channel = staticmethod(webhook_channel)

    # alert rules ----------------------------------------------------------------------------

    def list_rules(self, page: int | None = None, per_page: int | None = None,
                   resource_urn: str | None = None) -> dict:
        return self.request("GET", "/v2/insights/alert-rules",
                            {"page": page, "per_page": per_page, "resource_urn": resource_urn})

    def get_rule(self, rule_id: str) -> dict:
        return self.request("GET", f"/v2/insights/alert-rules/{rule_id}")

    @staticmethod
    def _rule_body(spec: dict, status: str | None) -> dict:
        body = dict(spec) if "spec" in spec else {"spec": spec}
        if status:
            body["status"] = _enum(status, RULE_STATUSES, "rule status")
        return body

    def create_rule(self, spec: dict, status: str | None = None) -> dict:
        return self.request("POST", "/v2/insights/alert-rules", json_body=self._rule_body(spec, status))

    def update_rule(self, rule_id: str, spec: dict, status: str | None = None) -> dict:
        return self.request("PUT", f"/v2/insights/alert-rules/{rule_id}", json_body=self._rule_body(spec, status))

    def delete_rule(self, rule_id: str) -> dict:
        return self.request("DELETE", f"/v2/insights/alert-rules/{rule_id}")

    rule_spec = staticmethod(rule_spec)

    def all_rules(self, per_page: int = 200, resource_urn: str | None = None) -> list[dict]:
        rules, page = [], 1
        while True:
            body = self.list_rules(page=page, per_page=per_page, resource_urn=resource_urn)
            rules.extend(body.get("alert_rules") or [])
            pages = (body.get("pagination") or {}).get("pages")
            more = page < pages if pages else bool((body.get("links") or {}).get("pages", {}).get("next"))
            if not more or page >= 100:
                return rules
            page += 1

    # alert instances ------------------------------------------------------------------------

    def list_instances(self, status: str | None = None, rule_id: str | None = None,
                       resource_urn: str | None = None, page: int | None = None,
                       per_page: int | None = None) -> dict:
        return self.request("GET", "/v2/insights/alert-instances",
                            {"status": status, "rule_id": rule_id, "resource_urn": resource_urn,
                             "page": page, "per_page": per_page})

    def get_instance(self, instance_id: str) -> dict:
        return self.request("GET", f"/v2/insights/alert-instances/{instance_id}")

    def all_instances(self, per_page: int = 200, **filters) -> list[dict]:
        out, page = [], 1
        while page <= 100:
            batch = self.list_instances(page=page, per_page=per_page, **filters).get("alert_instances") or []
            out.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return out

    # PromQL ---------------------------------------------------------------------------------

    def prom_path(self, endpoint: str, region: str | None = None) -> str:
        region = region or self.region
        if not region:
            raise ValueError("this call is regional: pass region= or set Insights(region=...)")
        return f"/v2/insights/query/{region}/prom/api/v1/{endpoint.lstrip('/')}"

    def query(self, q: str, time: Any = None, region: str | None = None) -> dict:
        return self.request("GET", self.prom_path("query", region),
                            {"query": q, "time": None if time is None else _ts(time)})

    def query_range(self, q: str, start: Any, end: Any, step: str = "60s", region: str | None = None) -> dict:
        return self.request("GET", self.prom_path("query_range", region),
                            {"query": q, "start": _ts(start), "end": _ts(end), "step": step})

    def labels(self, start: Any = None, end: Any = None, match: list[str] | None = None,
               region: str | None = None) -> dict:
        start, end = _window(start, end)
        return self.request("GET", self.prom_path("labels", region),
                            {"start": start, "end": end, "match[]": match or None})

    def label_values(self, name: str, start: Any = None, end: Any = None, match: list[str] | None = None,
                     region: str | None = None) -> dict:
        start, end = _window(start, end)
        return self.request("GET", self.prom_path(f"label/{name}/values", region),
                            {"start": start, "end": end, "match[]": match or None})

    def series(self, match: str | list[str], start: Any = None, end: Any = None,
               region: str | None = None) -> dict:
        start, end = _window(start, end)
        match = [match] if isinstance(match, str) else list(match)
        if not match:
            raise ValueError("series needs at least one match[] selector")
        return self.request("GET", self.prom_path("series", region),
                            {"match[]": match, "start": start, "end": end})

    # logs -----------------------------------------------------------------------------------

    @staticmethod
    def logs_body(start: Any, end: Any, filter: dict | None = None, order_by: Any = None,
                  limit: int | None = 100, cursor: str | None = None) -> dict:
        body: dict[str, Any] = {"time_range": {"from": time_instant(start), "to": time_instant(end)}}
        if filter:
            body["filter"] = filter
        if order_by:
            if isinstance(order_by, (str, dict)):
                order_by = [order_by]
            body["order_by"] = [o if isinstance(o, dict) else
                                (order(o[1:], "desc") if o.startswith("-") else order(o, "asc"))
                                for o in order_by]
        pagination: dict[str, Any] = {}
        if limit is not None:
            if not 1 <= int(limit) <= 1000:
                raise ValueError("limit must be between 1 and 1000")
            pagination["limit"] = int(limit)
        if cursor:
            pagination["cursor"] = cursor
        if pagination:
            body["pagination"] = pagination
        return body

    def search_logs(self, start: Any, end: Any, filter: dict | None = None, order_by: Any = None,
                    limit: int | None = 100, cursor: str | None = None, region: str | None = None) -> dict:
        region = region or self.region
        if not region:
            raise ValueError("logs search is regional: pass region= or set Insights(region=...)")
        return self.request("POST", f"/v2/insights/query/{region}/logs/search",
                            json_body=self.logs_body(start, end, filter, order_by, limit, cursor))

    def iter_logs(self, start: Any, end: Any, filter: dict | None = None, order_by: Any = None,
                  limit: int = 100, region: str | None = None, max_pages: int | None = None) -> Iterator[dict]:
        """Yield records across pages. Cursors only work when ordered by timestamp alone, so that
        is the default ordering here (newest first)."""
        order_by = order_by or [order("timestamp", "desc")]
        cursor, pages = None, 0
        while True:
            body = self.search_logs(start, end, filter, order_by, limit, cursor, region)
            yield from body.get("data") or []
            pages += 1
            pag = body.get("pagination") or {}
            cursor = pag.get("next_cursor")
            if not pag.get("has_more") or not cursor or (max_pages and pages >= max_pages):
                return


# --- probes ---------------------------------------------------------------------------------

class Probe:
    """Collects PASS/FAIL/INFO lines. PASS = the observed behavior matches the expectation stated
    for the probe; FAIL = it differs; INFO = context with no expectation attached."""

    def __init__(self, name: str, as_json: bool = False):
        self.name, self.as_json, self.results = name, as_json, []

    def add(self, verdict: str, check: str, detail: str, **data) -> None:
        self.results.append({"probe": self.name, "verdict": verdict, "check": check, "detail": detail, **data})
        if not self.as_json:
            print(f"{verdict:<4} {self.name}: {check} — {detail}", flush=True)

    @property
    def failed(self) -> bool:
        return any(r["verdict"] == "FAIL" for r in self.results)


def _status_line(resp: httpx.Response, ms: float | None) -> str:
    return f"HTTP {resp.status_code} {resp.headers.get('content-type', '-')} in {ms or 0:.0f} ms: {excerpt(resp.text, 200)}"


def _is_json(resp: httpx.Response) -> bool:
    try:
        resp.json()
        return True
    except ValueError:
        return False


def probe_labels_window(ins: Insights, p: Probe, region: str) -> None:
    path = ins.prom_path("labels", region)
    try:
        resp = ins.send("GET", path, timeout=30)
        if resp.is_success:
            n = len((resp.json() or {}).get("data") or []) if _is_json(resp) else "?"
            p.add("FAIL", "labels without window", f"expected error/timeout, got 200 with {n} labels "
                  f"(A1 not reproduced) in {ins.last_elapsed_ms:.0f} ms", status=resp.status_code)
        else:
            p.add("PASS", "labels without window", _status_line(resp, ins.last_elapsed_ms), status=resp.status_code)
    except httpx.TimeoutException:
        p.add("PASS", "labels without window", f"timed out after {ins.last_elapsed_ms / 1000:.1f} s", status=None)
    start, end = _window(None, None)
    resp = ins.send("GET", path, params={"start": start, "end": end}, timeout=60)
    if resp.status_code == 200 and _is_json(resp):
        p.add("PASS", "labels with 30-min window",
              f"HTTP 200 in {ins.last_elapsed_ms:.0f} ms, {len(resp.json().get('data') or [])} labels",
              status=200)
    else:
        p.add("FAIL", "labels with 30-min window", _status_line(resp, ins.last_elapsed_ms), status=resp.status_code)


def probe_rules_visibility(ins: Insights, p: Probe) -> None:
    listed = {r.get("id") for r in ins.all_rules()}
    p.add("INFO", "rules list", f"{len(listed)} rule(s) returned by GET /v2/insights/alert-rules", count=len(listed))
    for ch in ins.list_channels().get("notification_channels") or []:
        p.add("INFO", f"channel {ch.get('id')}", f"{ch.get('name')!r} usage.rule_count="
              f"{(ch.get('usage') or {}).get('rule_count')}")
    instances = ins.all_instances()
    referenced = sorted({i.get("rule_id") for i in instances if i.get("rule_id")})
    p.add("INFO", "alert instances", f"{len(instances)} instance(s) reference {len(referenced)} rule id(s)")
    for rid in referenced:
        resp = ins.send("GET", f"/v2/insights/alert-rules/{rid}")
        in_list = rid in listed
        if resp.status_code == 200 and in_list:
            p.add("PASS", f"rule {rid}", "visible in list and by id", get_status=200, in_list=True)
        elif resp.status_code == 200:
            name = ((resp.json().get("alert_rule") or {}).get("spec") or {}).get("name") if _is_json(resp) else None
            p.add("FAIL", f"rule {rid}", f"GET by id 200 ({name!r}) but missing from the list (A2)",
                  get_status=200, in_list=False)
        else:
            p.add("INFO", f"rule {rid}", f"GET by id {_status_line(resp, ins.last_elapsed_ms)}; in list={in_list}",
                  get_status=resp.status_code, in_list=in_list)


def probe_regions(ins: Insights, p: Probe, regions: list[str]) -> None:
    for region in regions:
        try:
            resp = ins.send("GET", ins.prom_path("query", region),
                            params={"query": "count(do.droplets.cpu_utilization)"}, timeout=30)
        except httpx.HTTPError as e:
            p.add("FAIL", region, f"{type(e).__name__}: {e}", region=region, status=None)
            continue
        if not _is_json(resp):
            p.add("FAIL", region, "non-JSON response: " + _status_line(resp, ins.last_elapsed_ms),
                  region=region, status=resp.status_code)
            continue
        body = resp.json()
        if resp.status_code == 200 and body.get("status") == "success":
            result = (body.get("data") or {}).get("result") or []
            count = result[0]["value"][1] if result else "0 (no series)"
            p.add("PASS", region, f"HTTP 200 success in {ins.last_elapsed_ms:.0f} ms, droplet series = {count}",
                  region=region, status=200, count=count)
        else:
            p.add("FAIL", region, _status_line(resp, ins.last_elapsed_ms), region=region, status=resp.status_code)


def _probe_name(kind: str) -> str:
    return f"insights-harness-probe-{kind}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"


def _created_id(resp: httpx.Response) -> str | None:
    if resp.is_success and _is_json(resp):
        return (resp.json().get("alert_rule") or {}).get("id")
    return None


def _delete_created(ins: Insights, p: Probe, created: list[str]) -> None:
    """Delete only the rule ids this probe run created."""
    for rid in created:
        try:
            resp = ins.send("DELETE", f"/v2/insights/alert-rules/{rid}")
            verdict = "PASS" if resp.is_success else "FAIL"
            p.add(verdict, "cleanup", f"DELETE rule {rid}: HTTP {resp.status_code}", status=resp.status_code)
        except httpx.HTTPError as e:
            p.add("FAIL", "cleanup", f"DELETE rule {rid}: {type(e).__name__} — delete it by hand")


def probe_naming(ins: Insights, p: Probe, channel_id: str) -> None:
    created: list[str] = []
    try:
        # Paused, unreachable threshold: the rule exists only to test validation and never notifies.
        for metric, expect in (("do_droplets_cpu_utilization", 422), ("do.droplets.cpu_utilization", 201)):
            spec = rule_spec(_probe_name("naming"), metric, ">", critical=100000,
                             channels=[(channel_id, ["critical"])])
            resp = ins.send("POST", "/v2/insights/alert-rules", json_body=Insights._rule_body(spec, "paused"))
            rid = _created_id(resp)
            if rid:
                created.append(rid)
            ok = resp.status_code == expect or (expect == 201 and resp.status_code == 200)
            p.add("PASS" if ok else "FAIL", f"create rule metric={metric}",
                  f"expected {expect}, got " + _status_line(resp, ins.last_elapsed_ms), status=resp.status_code)
        for rid in created:
            listed = {r.get("id") for r in ins.all_rules()}
            p.add("INFO", "list after create (A2)", f"rule {rid} {'IS' if rid in listed else 'is NOT'} in "
                  f"GET /v2/insights/alert-rules ({len(listed)} listed)", in_list=rid in listed)
    finally:
        _delete_created(ins, p, created)


def probe_tags(ins: Insights, p: Probe, channel_id: str, tag: str) -> None:
    created: list[str] = []
    try:
        spec = rule_spec(_probe_name("tags"), "do.droplets.cpu_utilization", ">", critical=100000,
                         tags=[tag], channels=[(channel_id, ["critical"])])
        resp = ins.send("POST", "/v2/insights/alert-rules", json_body=Insights._rule_body(spec, "paused"))
        rid = _created_id(resp)
        if not rid:
            p.add("INFO", "create rule with query.tags", "rejected: " + _status_line(resp, ins.last_elapsed_ms),
                  status=resp.status_code)
            return
        created.append(rid)
        p.add("INFO", "create rule with query.tags", f"accepted: HTTP {resp.status_code}, id {rid}",
              status=resp.status_code)
        got = ins.send("GET", f"/v2/insights/alert-rules/{rid}")
        tags = (((got.json().get("alert_rule") or {}).get("spec") or {}).get("query") or {}).get("tags") \
            if _is_json(got) else None
        p.add("INFO", "read back", f"HTTP {got.status_code}, query.tags = {tags!r} "
              f"({'round-trips' if tags == [tag] else 'does not round-trip'})", tags=tags)
    finally:
        _delete_created(ins, p, created)


def probe_endpoints(ins: Insights, p: Probe, region: str) -> None:
    q = {"query": "count(do.droplets.cpu_utilization)"}
    for path, expect in (("/v2/insights/prom/query", 404), ("/v2/insights/metrics/query", 404),
                         (ins.prom_path("query", region), 200)):
        resp = ins.send("GET", path, params=q, timeout=30)
        p.add("PASS" if resp.status_code == expect else "FAIL", f"GET {path}",
              f"expected {expect}, got " + _status_line(resp, ins.last_elapsed_ms), status=resp.status_code)


def probe_logs_api(ins: Insights, p: Probe, region: str) -> None:
    path = f"/v2/insights/query/{region}/logs/search"

    def run(check: str, body: dict, expect: int = 200) -> dict | None:
        resp = ins.send("POST", path, json_body=body, timeout=60)
        data = resp.json() if _is_json(resp) else None
        if resp.status_code != expect or data is None:
            p.add("FAIL", check, f"expected {expect}, got " + _status_line(resp, ins.last_elapsed_ms),
                  status=resp.status_code)
            return None
        if expect != 200:
            p.add("PASS", check, _status_line(resp, ins.last_elapsed_ms), status=resp.status_code)
            return data
        recs = data.get("data") or []
        pag = data.get("pagination") or {}
        keys = sorted(recs[0]) if recs else []
        p.add("PASS", check, f"HTTP 200 in {ins.last_elapsed_ms:.0f} ms, {len(recs)} record(s), "
              f"has_more={pag.get('has_more')}, cursor={'yes' if pag.get('next_cursor') else 'no'}, "
              f"record keys={keys}", status=200, records=len(recs))
        return data

    resp = ins.send("POST", path, json_body={}, timeout=60)
    ok = resp.status_code == 400 and "time_range" in resp.text
    p.add("PASS" if ok else "FAIL", "empty body {}", "expected 400 time_range is required, got "
          + _status_line(resp, ins.last_elapsed_ms), status=resp.status_code)

    window = {"start": "now-1h", "end": "now"}
    run("1h window, limit=5", Insights.logs_body(**window, limit=5))
    data = run("severity_number >= 17 (ERROR+)",
               Insights.logs_body(**window, filter=cond("severity_number", ">=", 17), limit=5))
    if data and data.get("data"):
        seen = sorted({r.get("severity_text") for r in data["data"]})
        p.add("INFO", "severity_text values seen", str(seen))
    run("severity_text IN [ERROR, Error, error]",
        Insights.logs_body(**window, filter=cond("severity_text", "in", ["ERROR", "Error", "error"]), limit=5))

    first = run("ordered by timestamp desc, limit=5",
                Insights.logs_body(**window, order_by=[order("timestamp", "desc")], limit=5))
    cursor = ((first or {}).get("pagination") or {}).get("next_cursor")
    if not cursor:
        p.add("INFO", "cursor page", "no next_cursor on the first page (not enough records?)")
        return
    second = run("cursor page 2", Insights.logs_body(**window, order_by=[order("timestamp", "desc")],
                                                    limit=5, cursor=cursor))
    if second and second.get("data") and first.get("data"):
        last1, first2 = first["data"][-1]["timestamp"], second["data"][0]["timestamp"]
        p.add("PASS" if first2 <= last1 else "FAIL", "cursor ordering",
              f"page 1 ends {last1}, page 2 starts {first2}")


# --- CLI ------------------------------------------------------------------------------------

def _table(rows: list[dict], cols: list[tuple[str, Any]]) -> str:
    if not rows:
        return "(none)"
    cells = [[str(get(r) if callable(get) else r.get(get, "")) for _, get in cols] for r in rows]
    widths = [max(len(h), *(len(c[i]) for c in cells)) for i, (h, _) in enumerate(cols)]
    widths = [min(w, 60) for w in widths]
    line = lambda vals: "  ".join(v[:w].ljust(w) for v, w in zip(vals, widths)).rstrip()
    return "\n".join([line([h for h, _ in cols]), line(["-" * w for w in widths]), *map(line, cells)])


def _dig(*keys):
    def get(row):
        for k in keys:
            row = (row or {}).get(k) if isinstance(row, dict) else None
        return "" if row is None else row
    return get


RULE_COLS = [("id", "id"), ("name", _dig("spec", "name")), ("metric", _dig("spec", "query", "metric")),
             ("op", lambda r: _dig("spec", "thresholds", "operator")(r).replace("THRESHOLD_OPERATOR_", "")),
             ("warn", _dig("spec", "thresholds", "warning")), ("crit", _dig("spec", "thresholds", "critical")),
             ("window", lambda r: _dig("spec", "condition", "window")(r).replace("EVALUATION_WINDOW_", "")),
             ("status", lambda r: str(r.get("status", "")).replace("ALERT_RULE_STATUS_", ""))]
CHANNEL_COLS = [("id", "id"), ("name", "name"), ("type", lambda r: str(r.get("channel_type", "")).replace(
    "CHANNEL_TYPE_", "")), ("target", lambda r: _dig("email", "to")(r) or _dig("slack", "channel")(r)
                            or _dig("webhook", "url")(r)), ("rules", _dig("usage", "rule_count")),
                ("created", "created_at")]
INSTANCE_COLS = [("id", "id"), ("rule_id", "rule_id"), ("severity", "severity"), ("status", "status"),
                 ("resource_urn", "resource_urn"), ("value", "value"), ("triggered_at", "triggered_at"),
                 ("resolved_at", "resolved_at")]
LOG_COLS = [("timestamp", "timestamp"), ("sev", "severity_text"), ("service", "service_name"),
            ("trace_id", "trace_id"), ("body", "body")]


def _prom_rows(body: dict) -> str:
    data = body.get("data")
    if isinstance(data, list):  # labels / label values / series
        return "\n".join(json.dumps(x) if isinstance(x, dict) else str(x) for x in data) or "(none)"
    result = (data or {}).get("result") or []
    lines = []
    for s in result:
        metric = s.get("metric") or {}
        name = metric.get("__name__", "")
        labels = ",".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__")
        if "value" in s:
            lines.append(f"{name}{{{labels}}} {s['value'][1]}")
        else:
            vals = s.get("values") or []
            lines.append(f"{name}{{{labels}}} {len(vals)} points, last={vals[-1][1] if vals else '-'}")
    return "\n".join(lines) or f"(no series; resultType={(data or {}).get('resultType')})"


def _load_json(path: str) -> dict:
    with (sys.stdin if path == "-" else open(path)) as f:
        return json.load(f)


def _channel_spec(a) -> dict:
    if a.file:
        return _load_json(a.file)
    if not a.name:
        raise SystemExit("--name is required without --file")
    if a.email:
        return email_channel(a.name, a.email)
    if a.slack_url:
        return slack_webhook_channel(a.name, a.slack_url, a.slack_channel or "")
    if a.webhook_url:
        basic = tuple(a.basic.split(":", 1)) if a.basic else None
        headers = dict(h.split("=", 1) for h in a.header or [])
        return webhook_channel(a.name, a.webhook_url, bearer=a.bearer, basic=basic, secret=a.secret,
                               headers=headers or None)
    raise SystemExit("one of --file, --email, --slack-url, --webhook-url is required")


def _rule_spec_from_args(a) -> dict:
    if a.file:
        return _load_json(a.file)
    if not (a.name and a.metric and a.op):
        raise SystemExit("--name, --metric and --op are required without --file")
    channels = []
    for c in a.channel or []:
        cid, _, sev = c.partition(":")
        channels.append((cid, sev.split(",") if sev else None))
    filters = [tuple(re.match(r"^([^!<>=]+)(!=|>=|<=|==|=|>|<)(.*)$", f).groups()) for f in a.filter or []]
    return rule_spec(a.name, a.metric, a.op, warning=a.warning, critical=a.critical, window=a.window,
                     resource_urns=a.urn, tags=a.tag, filters=filters, channels=channels, re_alert=a.re_alert)


def _out(a, body: Any, rows_key: str | None = None, cols=None, single_key: str | None = None) -> None:
    if a.json or cols is None:
        print(json.dumps(body, indent=2))
    elif rows_key:
        print(_table(body.get(rows_key) or [], cols))
        pag = body.get("pagination") or body.get("meta")
        if pag:
            print(f"\n{json.dumps(pag)}")
    else:
        print(_table([body.get(single_key) or body], cols))


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--region", default=argparse.SUPPRESS, help="region slug for query/logs calls")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print raw JSON")
    common.add_argument("--trace", action="store_true", default=argparse.SUPPRESS,
                        help="print each request/response line to stderr")
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS)

    ap = argparse.ArgumentParser(prog="insights_harness.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--region", default=os.environ.get("INSIGHTS_REGION"))
    ap.add_argument("--json", action="store_true", default=False)
    ap.add_argument("--trace", action="store_true", default=False)
    ap.add_argument("--timeout", type=float, default=60)
    groups = ap.add_subparsers(dest="group", required=True)

    def verbs(group: str, help_: str):
        g = groups.add_parser(group, help=help_)
        return g.add_subparsers(dest="verb", required=True)

    def leaf(sp, name: str, help_: str):
        return sp.add_parser(name, help=help_, parents=[common])

    # channels
    sp = verbs("channels", "notification channels")
    p = leaf(sp, "list", "list channels")
    p.add_argument("--page", type=int)
    p.add_argument("--per-page", type=int)
    leaf(sp, "get", "get a channel").add_argument("id")
    for verb in ("create", "update"):
        p = leaf(sp, verb, f"{verb} a channel")
        if verb == "update":
            p.add_argument("id")
        p.add_argument("--file", help="full request body as JSON ('-' for stdin)")
        p.add_argument("--name")
        p.add_argument("--email", help="recipients, comma separated (verified team members)")
        p.add_argument("--slack-url")
        p.add_argument("--slack-channel")
        p.add_argument("--webhook-url")
        p.add_argument("--bearer")
        p.add_argument("--basic", help="user:password")
        p.add_argument("--secret", help="payload signing secret")
        p.add_argument("--header", action="append", help="K=V (repeatable)")
    p = leaf(sp, "delete", "delete a channel (explicit id, needs --yes)")
    p.add_argument("id")
    p.add_argument("--yes", action="store_true")

    # rules
    sp = verbs("rules", "alert rules")
    p = leaf(sp, "list", "list rules")
    p.add_argument("--page", type=int)
    p.add_argument("--per-page", type=int)
    p.add_argument("--resource-urn")
    leaf(sp, "get", "get a rule").add_argument("id")
    for verb in ("create", "update"):
        p = leaf(sp, verb, f"{verb} a rule")
        if verb == "update":
            p.add_argument("id")
        p.add_argument("--file", help="spec (or full body with 'spec') as JSON ('-' for stdin)")
        p.add_argument("--name")
        p.add_argument("--metric", help="dotted name, e.g. do.droplets.cpu_utilization")
        p.add_argument("--op", help="one of > >= < <= = != (or the full enum)")
        p.add_argument("--warning", type=float)
        p.add_argument("--critical", type=float)
        p.add_argument("--window", default="5m", help="1m 5m 10m 15m 30m 1h")
        p.add_argument("--urn", action="append", help="resource URN (repeatable)")
        p.add_argument("--tag", action="append", help="resource tag (repeatable)")
        p.add_argument("--filter", action="append", help="label filter, e.g. host_id=123 (repeatable)")
        p.add_argument("--channel", action="append", help="CHANNEL_ID[:warning,critical] (repeatable)")
        p.add_argument("--re-alert", default="4h", help="30m 1h 4h never")
        p.add_argument("--status", choices=["active", "paused"])
    p = leaf(sp, "delete", "delete a rule (explicit id, needs --yes)")
    p.add_argument("id")
    p.add_argument("--yes", action="store_true")

    # instances
    sp = verbs("instances", "alert instances")
    p = leaf(sp, "list", "list alert instances")
    p.add_argument("--status", help="active | resolved")
    p.add_argument("--rule-id")
    p.add_argument("--resource-urn")
    p.add_argument("--page", type=int)
    p.add_argument("--per-page", type=int)
    leaf(sp, "get", "get an alert instance").add_argument("id")

    # prom
    sp = verbs("prom", "PromQL (regional)")
    p = leaf(sp, "query", "instant query")
    p.add_argument("q")
    p.add_argument("--time")
    p = leaf(sp, "range", "range query")
    p.add_argument("q")
    p.add_argument("--start", default=None, help="RFC3339 or unix (default: 1 h ago)")
    p.add_argument("--end", default=None, help="RFC3339 or unix (default: now)")
    p.add_argument("--step", default="60s")
    for verb, help_ in (("labels", "label names"), ("values", "label values"), ("series", "series")):
        p = leaf(sp, verb, help_ + " (windowed; default last 30 min)")
        if verb == "values":
            p.add_argument("name")
        p.add_argument("--match", action="append", required=verb == "series", help="match[] selector")
        p.add_argument("--start")
        p.add_argument("--end")

    # logs
    sp = verbs("logs", "logs search (regional)")
    for verb in ("search", "iter"):
        p = leaf(sp, verb, "one page" if verb == "search" else "follow the cursor")
        p.add_argument("--start", default="now-1h", help="RFC3339, 'now-1h', '15m', unix seconds")
        p.add_argument("--end", default="now")
        p.add_argument("--filter-json", help="filter expression as JSON")
        p.add_argument("--severity", help="severity_text equals (e.g. ERROR)")
        p.add_argument("--service", help="service.name equals")
        p.add_argument("--text", help="substring search")
        p.add_argument("--order", action="append", help="'timestamp' (asc) or '-timestamp' (desc)")
        p.add_argument("--limit", type=int, default=100)
        if verb == "search":
            p.add_argument("--cursor")
        else:
            p.add_argument("--max-pages", type=int, default=10)

    # probes
    sp = verbs("probe", "reproduce the facts-pack findings")
    leaf(sp, "labels-window", "A1: labels with vs without a time window")
    leaf(sp, "rules-visibility", "A2: rule list vs get-by-id")
    leaf(sp, "regions", "A3: count(do.droplets.cpu_utilization) in every region").add_argument(
        "--only", action="append", help="limit to these regions")
    p = leaf(sp, "naming", "A4 (write): underscored vs dotted metric name in a rule")
    p.add_argument("channel_id")
    p.add_argument("--write", action="store_true", help="required: this probe creates and deletes a rule")
    p = leaf(sp, "tags", "A8 (write): rule with query.tags")
    p.add_argument("channel_id")
    p.add_argument("--tag", default="insights-demo")
    p.add_argument("--write", action="store_true", help="required: this probe creates and deletes a rule")
    leaf(sp, "endpoints", "A5: documented wrong paths vs the real one")
    leaf(sp, "logs-api", "logs/search: errors, limit, filters, cursor")
    return ap


def _time_or_none(v: str | None) -> Any:
    if v is None:
        return None
    return float(v) if re.fullmatch(r"\d+(\.\d+)?", v) else v


def run(argv: list[str] | None = None, client: Insights | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.group == "probe" and a.verb in ("naming", "tags") and not a.write:
        print(f"probe {a.verb} creates and deletes an alert rule; re-run with --write", file=sys.stderr)
        return 2
    if a.verb == "delete" and not a.yes:
        print(f"refusing to delete {a.group[:-1]} {a.id} without --yes", file=sys.stderr)
        return 2
    if client is None:
        token = os.environ.get("DIGITALOCEAN_TOKEN")
        if not token:
            print("DIGITALOCEAN_TOKEN is not set", file=sys.stderr)
            return 2
        client = Insights(token, region=a.region, timeout=a.timeout, trace=a.trace)
    ins = client
    try:
        return _dispatch(a, ins)
    except InsightsError as e:
        print(f"error: HTTP {e.status} from {e.request_summary}: {excerpt(e.body, 1000)}", file=sys.stderr)
        return 1
    except httpx.HTTPError as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _dispatch(a, ins: Insights) -> int:
    g, v = a.group, a.verb
    if g == "channels":
        if v == "list":
            _out(a, ins.list_channels(a.page, a.per_page), "notification_channels", CHANNEL_COLS)
        elif v == "get":
            _out(a, ins.get_channel(a.id), cols=CHANNEL_COLS, single_key="notification_channel")
        elif v == "create":
            _out(a, ins.create_channel(_channel_spec(a)), cols=CHANNEL_COLS, single_key="notification_channel")
        elif v == "update":
            _out(a, ins.update_channel(a.id, _channel_spec(a)), cols=CHANNEL_COLS, single_key="notification_channel")
        elif v == "delete":
            ins.delete_channel(a.id)
            print(f"deleted channel {a.id}")
    elif g == "rules":
        if v == "list":
            _out(a, ins.list_rules(a.page, a.per_page, a.resource_urn), "alert_rules", RULE_COLS)
        elif v == "get":
            _out(a, ins.get_rule(a.id), cols=RULE_COLS, single_key="alert_rule")
        elif v == "create":
            _out(a, ins.create_rule(_rule_spec_from_args(a), a.status), cols=RULE_COLS, single_key="alert_rule")
        elif v == "update":
            _out(a, ins.update_rule(a.id, _rule_spec_from_args(a), a.status), cols=RULE_COLS,
                 single_key="alert_rule")
        elif v == "delete":
            ins.delete_rule(a.id)
            print(f"deleted rule {a.id}")
    elif g == "instances":
        if v == "list":
            _out(a, ins.list_instances(a.status, a.rule_id, a.resource_urn, a.page, a.per_page),
                 "alert_instances", INSTANCE_COLS)
        else:
            _out(a, ins.get_instance(a.id), cols=INSTANCE_COLS, single_key="alert_instance")
    elif g == "prom":
        if v == "query":
            body = ins.query(a.q, _time_or_none(a.time))
        elif v == "range":
            end = _time_or_none(a.end) or datetime.now(timezone.utc)
            start = _time_or_none(a.start) or datetime.now(timezone.utc) - timedelta(hours=1)
            body = ins.query_range(a.q, start, end, a.step)
        elif v == "labels":
            body = ins.labels(_time_or_none(a.start), _time_or_none(a.end), a.match)
        elif v == "values":
            body = ins.label_values(a.name, _time_or_none(a.start), _time_or_none(a.end), a.match)
        else:
            body = ins.series(a.match, _time_or_none(a.start), _time_or_none(a.end))
        print(json.dumps(body, indent=2) if a.json else _prom_rows(body))
    elif g == "logs":
        conds = []
        if a.filter_json:
            conds.append(json.loads(a.filter_json))
        if a.severity:
            conds.append(cond("severity_text", "=", a.severity))
        if a.service:
            conds.append(cond("service.name", "=", a.service))
        if a.text:
            conds.append(text(a.text))
        flt = conds[0] if len(conds) == 1 else (and_(*conds) if conds else None)
        start, end = _time_or_none(a.start), _time_or_none(a.end)
        if v == "search":
            body = ins.search_logs(start, end, flt, a.order, a.limit, a.cursor)
            _out(a, body, "data", LOG_COLS)
        else:
            records = list(ins.iter_logs(start, end, flt, a.order, a.limit, max_pages=a.max_pages))
            print(json.dumps(records, indent=2) if a.json else _table(records, LOG_COLS))
            if not a.json:
                print(f"\n{len(records)} record(s)")
    elif g == "probe":
        p = Probe(v, as_json=a.json)
        region = a.region or "nyc3"
        if v == "labels-window":
            probe_labels_window(ins, p, region)
        elif v == "rules-visibility":
            probe_rules_visibility(ins, p)
        elif v == "regions":
            probe_regions(ins, p, a.only or REGIONS)
        elif v == "naming":
            probe_naming(ins, p, a.channel_id)
        elif v == "tags":
            probe_tags(ins, p, a.channel_id, a.tag)
        elif v == "endpoints":
            probe_endpoints(ins, p, region)
        elif v == "logs-api":
            probe_logs_api(ins, p, region)
        if a.json:
            print(json.dumps(p.results, indent=2))
        return 1 if p.failed else 0
    return 0


if __name__ == "__main__":
    sys.exit(run())
