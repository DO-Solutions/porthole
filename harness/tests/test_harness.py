"""Offline tests for insights_harness: request building, OpenAPI shapes, errors, cursors."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import insights_harness as h  # noqa: E402

OPENAPI = Path(__file__).parent / "openapi"
TOKEN = "dop_v1_test_token_never_printed"


# --- a minimal validator for the OpenAPI subset the Insights models use -----------------------

def _load(name: str) -> dict:
    return yaml.safe_load((OPENAPI / name).read_text())


def _resolve(schema: dict) -> dict:
    while "$ref" in schema:
        schema = _load(schema["$ref"])
    return schema


def _props(schema: dict) -> set[str]:
    schema = _resolve(schema)
    keys = set(schema.get("properties", {}))
    for sub in schema.get("allOf", []):
        keys |= _props(sub)
    return keys


def validate(value, schema: dict, path: str = "$", strict: bool = True) -> None:
    schema = _resolve(schema)
    for sub in schema.get("allOf", []):
        validate(value, sub, path, strict=False)
    if "allOf" in schema and strict and isinstance(value, dict):
        unknown = set(value) - _props(schema)
        assert not unknown, f"{path}: unknown keys {unknown}"
    if "anyOf" in schema:
        assert any(_ok(value, s, path) for s in schema["anyOf"]), f"{path}: matches no anyOf branch"
    if "oneOf" in schema:
        n = sum(_ok(value, s, path) for s in schema["oneOf"])
        assert n == 1, f"{path}: matches {n} oneOf branches"
    t = schema.get("type")
    if t == "object" or "properties" in schema or "required" in schema:
        assert isinstance(value, dict), f"{path}: expected object, got {value!r}"
        for k in schema.get("required", []):
            assert k in value, f"{path}: missing required {k!r}"
        if "minProperties" in schema:
            assert len(value) >= schema["minProperties"], f"{path}: too few properties"
        if "maxProperties" in schema:
            assert len(value) <= schema["maxProperties"], f"{path}: too many properties"
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties")
        for k, v in value.items():
            if k in props:
                validate(v, props[k], f"{path}.{k}")
            elif isinstance(extra, dict):
                validate(v, extra, f"{path}.{k}")
            elif strict and props and "allOf" not in schema:
                raise AssertionError(f"{path}: unknown key {k!r}")
    elif t == "array":
        assert isinstance(value, list), f"{path}: expected array"
        assert len(value) >= schema.get("minItems", 0), f"{path}: too few items"
        for i, v in enumerate(value):
            validate(v, schema.get("items", {}), f"{path}[{i}]")
    elif t == "string":
        assert isinstance(value, str), f"{path}: expected string, got {value!r}"
    elif t == "integer":
        assert isinstance(value, int) and not isinstance(value, bool), f"{path}: expected integer"
    elif t == "number":
        assert isinstance(value, (int, float)) and not isinstance(value, bool), f"{path}: expected number"
    elif t == "boolean":
        assert isinstance(value, bool), f"{path}: expected boolean"
    if "enum" in schema:
        assert value in schema["enum"], f"{path}: {value!r} not in {schema['enum']}"
    if "minimum" in schema:
        assert value >= schema["minimum"], f"{path}: below minimum"
    if "maximum" in schema:
        assert value <= schema["maximum"], f"{path}: above maximum"


def _ok(value, schema, path) -> bool:
    try:
        validate(value, schema, path, strict=False)
        return True
    except AssertionError:
        return False


def test_validator_rejects_bad_shapes():
    with pytest.raises(AssertionError):
        validate({"condition": {}, "and": {"expressions": []}}, {"$ref": "logs_filter_expression.yml"})
    with pytest.raises(AssertionError):
        validate({"condition": {"field": {"name": "x"}, "operator": "EQ"}}, {"$ref": "logs_filter_expression.yml"})
    with pytest.raises(AssertionError):
        validate({"name": "x", "email": {"to": "a"}, "slack": {"channel": "c"}},
                 {"$ref": "notification_channel_request.yml"})


# --- fixtures ---------------------------------------------------------------------------------

class Recorder:
    def __init__(self, responder=None):
        self.requests: list[httpx.Request] = []
        self.responder = responder or (lambda req: httpx.Response(200, json={"ok": True}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responder(request)

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def make(responder=None, **kw):
    rec = Recorder(responder)
    ins = h.Insights(TOKEN, region="tor1", transport=httpx.MockTransport(rec), **kw)
    return ins, rec


def qs(req: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(req.url)).query)


def body(req: httpx.Request):
    return json.loads(req.content) if req.content else None


# --- request building -------------------------------------------------------------------------

CHANNEL_ID = "550e8400-e29b-41d4-a716-446655440000"
RULE_ID = "25cd5489-13f6-430f-a9fd-a9e2676c1ce0"

CASES = [
    (lambda i: i.list_channels(), "GET", "/v2/insights/notification-channels", {}),
    (lambda i: i.list_channels(2, 50), "GET", "/v2/insights/notification-channels",
     {"page": ["2"], "per_page": ["50"]}),
    (lambda i: i.get_channel(CHANNEL_ID), "GET", f"/v2/insights/notification-channels/{CHANNEL_ID}", {}),
    (lambda i: i.delete_channel(CHANNEL_ID), "DELETE", f"/v2/insights/notification-channels/{CHANNEL_ID}", {}),
    (lambda i: i.list_rules(1, 100, "do:droplet:1"), "GET", "/v2/insights/alert-rules",
     {"page": ["1"], "per_page": ["100"], "resource_urn": ["do:droplet:1"]}),
    (lambda i: i.get_rule(RULE_ID), "GET", f"/v2/insights/alert-rules/{RULE_ID}", {}),
    (lambda i: i.delete_rule(RULE_ID), "DELETE", f"/v2/insights/alert-rules/{RULE_ID}", {}),
    (lambda i: i.list_instances("resolved", RULE_ID, "do:droplet:1", 3, 20), "GET",
     "/v2/insights/alert-instances",
     {"status": ["resolved"], "rule_id": [RULE_ID], "resource_urn": ["do:droplet:1"],
      "page": ["3"], "per_page": ["20"]}),
    (lambda i: i.get_instance("abc"), "GET", "/v2/insights/alert-instances/abc", {}),
    (lambda i: i.query("do.droplets.cpu_utilization"), "GET", "/v2/insights/query/tor1/prom/api/v1/query",
     {"query": ["do.droplets.cpu_utilization"]}),
    (lambda i: i.query("up", time=1700000000), "GET", "/v2/insights/query/tor1/prom/api/v1/query",
     {"query": ["up"], "time": ["1700000000"]}),
    (lambda i: i.query_range("rate(x[5m])", 1700000000, 1700003600, "60s"), "GET",
     "/v2/insights/query/tor1/prom/api/v1/query_range",
     {"query": ["rate(x[5m])"], "start": ["1700000000"], "end": ["1700003600"], "step": ["60s"]}),
    (lambda i: i.labels(1, 2), "GET", "/v2/insights/query/tor1/prom/api/v1/labels", {"start": ["1"], "end": ["2"]}),
    (lambda i: i.label_values("__name__", 1, 2), "GET", "/v2/insights/query/tor1/prom/api/v1/label/__name__/values",
     {"start": ["1"], "end": ["2"]}),
    (lambda i: i.series(["a", "b"], 1, 2), "GET", "/v2/insights/query/tor1/prom/api/v1/series",
     {"match[]": ["a", "b"], "start": ["1"], "end": ["2"]}),
    (lambda i: i.query("up", region="syd1"), "GET", "/v2/insights/query/syd1/prom/api/v1/query", {"query": ["up"]}),
]


@pytest.mark.parametrize("call,method,path,params", CASES)
def test_request_building(call, method, path, params):
    ins, rec = make()
    assert call(ins) == {"ok": True}
    req = rec.last
    assert req.method == method
    assert req.url.path == path
    assert str(req.url).startswith("https://api.digitalocean.com/")
    assert qs(req) == params
    assert req.headers["authorization"] == f"Bearer {TOKEN}"


def test_discovery_calls_default_to_30_minute_window():
    ins, rec = make()
    for call in (lambda: ins.labels(), lambda: ins.label_values("service_name"), lambda: ins.series("up")):
        call()
        q = qs(rec.last)
        start = h.datetime.fromisoformat(q["start"][0].replace("Z", "+00:00"))
        end = h.datetime.fromisoformat(q["end"][0].replace("Z", "+00:00"))
        assert (end - start).total_seconds() == 1800


def test_regional_calls_need_a_region():
    ins = h.Insights(TOKEN, transport=httpx.MockTransport(Recorder()))
    with pytest.raises(ValueError):
        ins.query("up")
    with pytest.raises(ValueError):
        ins.search_logs("now-1h", "now")


def test_create_and_update_channel_bodies():
    ins, rec = make()
    spec = h.webhook_channel("hook", "https://head.example/hooks/insights", bearer="b", secret="s",
                             headers={"X-Kraken": "1"})
    ins.create_channel(spec)
    assert (rec.last.method, rec.last.url.path) == ("POST", "/v2/insights/notification-channels")
    assert body(rec.last) == spec
    ins.update_channel(CHANNEL_ID, h.email_channel("mail", ["a@x.com", "b@x.com"]))
    assert (rec.last.method, rec.last.url.path) == ("PUT", f"/v2/insights/notification-channels/{CHANNEL_ID}")
    assert body(rec.last) == {"name": "mail", "email": {"to": "a@x.com, b@x.com"}}


def test_create_and_update_rule_bodies():
    ins, rec = make()
    spec = h.rule_spec("High CPU", "do.droplets.cpu_utilization", ">", warning=80, critical=95,
                       resource_urns=["do:droplet:12345"], tags=["env:prod"],
                       channels=[(CHANNEL_ID, ["critical"])])
    ins.create_rule(spec)
    assert (rec.last.method, rec.last.url.path) == ("POST", "/v2/insights/alert-rules")
    sent = body(rec.last)
    assert sent == {"spec": spec}
    # the OpenAPI example body, field for field
    assert sent["spec"] == {
        "name": "High CPU",
        "query": {"metric": "do.droplets.cpu_utilization", "resource_urns": ["do:droplet:12345"],
                  "tags": ["env:prod"]},
        "condition": {"window": "EVALUATION_WINDOW_5M"},
        "thresholds": {"warning": 80, "critical": 95, "operator": "THRESHOLD_OPERATOR_GREATER_THAN"},
        "notification_channels": [{"notification_channel_id": CHANNEL_ID, "notify_on": ["SEVERITY_CRITICAL"]}],
        "re_alert_duration": "RE_ALERT_DURATION_4H"}
    validate(sent, {"$ref": "alert_rule_create_request.yml"})
    ins.update_rule(RULE_ID, h.rule_spec("x", "do.droplets.cpu_utilization", "<=", critical=1), "paused")
    assert (rec.last.method, rec.last.url.path) == ("PUT", f"/v2/insights/alert-rules/{RULE_ID}")
    sent = body(rec.last)
    assert sent["status"] == "ALERT_RULE_STATUS_PAUSED"
    assert "notification_channels" not in sent["spec"]  # omitted on PUT keeps bindings
    validate(sent, {"$ref": "alert_rule_request.yml"})


@pytest.mark.parametrize("op,enum", [(">=", "THRESHOLD_OPERATOR_GREATER_THAN_OR_EQUAL"),
                                     (">", "THRESHOLD_OPERATOR_GREATER_THAN"),
                                     ("<=", "THRESHOLD_OPERATOR_LESS_THAN_OR_EQUAL"),
                                     ("<", "THRESHOLD_OPERATOR_LESS_THAN"),
                                     ("=", "THRESHOLD_OPERATOR_EQUAL"),
                                     ("!=", "THRESHOLD_OPERATOR_NOT_EQUAL"),
                                     ("THRESHOLD_OPERATOR_EQUAL", "THRESHOLD_OPERATOR_EQUAL")])
def test_rule_spec_operators(op, enum):
    spec = h.rule_spec("r", "do.droplets.cpu_utilization", op, critical=1, channels=[CHANNEL_ID])
    assert spec["thresholds"]["operator"] == enum
    validate({"spec": spec}, {"$ref": "alert_rule_create_request.yml"})


def test_rule_spec_all_options_validate():
    for window in ("1m", "5m", "10m", "15m", "30m", "1h", "EVALUATION_WINDOW_1H"):
        for re_alert in ("30m", "1h", "4h", "never"):
            spec = h.rule_spec("r", "do.droplets.memory_utilization", ">=", warning=70, window=window,
                               filters=[("host_id", "=", 123), {"field": "x", "operator": "FILTER_OPERATOR_NOT_EQUAL",
                                                                 "value": "y"}],
                               channels=[(CHANNEL_ID, ["warning", "critical"]), (CHANNEL_ID,)],
                               re_alert=re_alert)
            validate({"spec": spec, "status": "ALERT_RULE_STATUS_ACTIVE"}, {"$ref": "alert_rule_create_request.yml"})
    assert spec["query"]["filters"][0] == {"field": "host_id", "operator": "FILTER_OPERATOR_EQUAL", "value": "123"}
    assert spec["notification_channels"][1] == {"notification_channel_id": CHANNEL_ID}


def test_rule_spec_rejects_bad_input():
    with pytest.raises(ValueError):
        h.rule_spec("r", "m", ">")  # no threshold
    with pytest.raises(ValueError):
        h.rule_spec("r", "m", "~", critical=1)
    with pytest.raises(ValueError):
        h.rule_spec("r", "m", ">", critical=1, window="2m")
    with pytest.raises(ValueError):
        h.rule_spec("r", "m", ">", critical=1, channels=[(CHANNEL_ID, ["info"])])


def test_channel_helpers_match_openapi():
    for spec in (h.email_channel("e", "a@x.com"),
                 h.slack_webhook_channel("s", "https://hooks.slack.com/services/T/B/X", "#alerts"),
                 h.webhook_channel("w", "https://x.example/h"),
                 h.webhook_channel("w", "https://x.example/h", bearer="t", secret="s", headers={"X-A": "1"}),
                 h.webhook_channel("w", "https://x.example/h", basic=("u", "p"))):
        validate(spec, {"$ref": "notification_channel_request.yml"})
    with pytest.raises(ValueError):
        h.webhook_channel("w", "http://x.example/h")
    with pytest.raises(ValueError):
        h.webhook_channel("w", "https://x.example/h", bearer="t", basic=("u", "p"))


# --- logs filter tree -------------------------------------------------------------------------

EXPR = {"$ref": "logs_filter_expression.yml"}


def test_filter_builders_exact_shapes():
    assert h.cond("severity_text", "=", "Error") == {"condition": {
        "field": {"name": "severity_text"}, "operator": "FILTER_OPERATOR_EQ", "value": {"string_value": "Error"}}}
    assert h.cond("severity_number", ">=", 17)["condition"]["value"] == {"number_value": 17}
    assert h.cond("x", "=", True)["condition"]["value"] == {"bool_value": True}
    assert h.cond("x", "in", ["a", "b"])["condition"]["value"] == {"string_array_value": {"values": ["a", "b"]}}
    assert h.cond("x", "in", [1, 2])["condition"]["value"] == {"number_array_value": {"values": [1, 2]}}
    assert h.cond("x", "in", "a")["condition"]["value"] == {"string_array_value": {"values": ["a"]}}
    assert h.cond("trace_id", "exists") == {"condition": {"field": {"name": "trace_id"},
                                                          "operator": "FILTER_OPERATOR_EXISTS"}}
    assert h.cond("k", "!=", "v", scope="attributes")["condition"]["field"] == {
        "name": "k", "scope": "FIELD_SCOPE_ATTRIBUTES"}
    assert h.text("timeout") == {"text_search": {"query": "timeout"}}
    a, b = h.cond("a", "=", "1"), h.cond("b", "=", "2")
    assert h.and_(a, b) == {"and": {"expressions": [a, b]}}
    assert h.or_([a, b]) == {"or": {"expressions": [a, b]}}
    assert h.not_(a) == {"not": a}


def test_filter_tree_validates_against_openapi():
    tree = h.and_(
        h.or_(h.cond("severity_text", "=", "ERROR"), h.cond("severity_number", ">=", 17)),
        h.not_(h.cond("service.name", "in", ["noise", "chatter"])),
        h.cond("LogAttributes['scenario']", "=", "logs"),
        h.cond("trace_id", "exists"),
        h.cond("resource.urn", "!=", "do:droplet:1", scope="resource"),
        h.cond("http.status_code", "<=", 499, scope="attributes"),
        h.cond("retry", "=", False),
        h.cond("code", "in", [500, 502]),
        h.text("kraken"))
    validate(tree, EXPR)
    for op in ("=", "!=", "in", ">=", "<=", "FILTER_OPERATOR_EQ"):
        validate(h.cond("f", op, "v" if op != "in" else ["v"]), EXPR)


def test_filter_builders_reject_bad_input():
    with pytest.raises(ValueError):
        h.cond("x", "like", "a")
    with pytest.raises(ValueError):
        h.cond("x", "exists", "a")
    with pytest.raises(ValueError):
        h.cond("x", "=")
    with pytest.raises(ValueError):
        h.cond("x", "in", [])
    with pytest.raises(ValueError):
        h.cond("x", "in", ["a", 1])
    with pytest.raises(ValueError):
        h.and_({"condition": {}, "text_search": {}})


def test_search_logs_body_matches_openapi():
    ins, rec = make(lambda r: httpx.Response(200, json={"data": [], "pagination": {"has_more": False}}))
    ins.search_logs("2026-09-30T00:00:00Z", "2026-09-30T01:00:00Z", filter=h.cond("severity_text", "=", "Error"),
                    order_by=[h.order("timestamp", "desc")], limit=100)
    req = rec.last
    assert (req.method, req.url.path) == ("POST", "/v2/insights/query/tor1/logs/search")
    # the OpenAPI curl example body, exactly
    assert body(req) == {
        "time_range": {"from": {"absolute": "2026-09-30T00:00:00Z"}, "to": {"absolute": "2026-09-30T01:00:00Z"}},
        "filter": {"condition": {"field": {"name": "severity_text"}, "operator": "FILTER_OPERATOR_EQ",
                                 "value": {"string_value": "Error"}}},
        "order_by": [{"field": {"name": "timestamp"}, "direction": "SORT_DIRECTION_DESC"}],
        "pagination": {"limit": 100}}
    validate(body(req), {"$ref": "logs_search_request.yml"})


def test_time_instants_and_order_shortcuts():
    assert h.time_instant("now-1h") == {"relative": "now-1h"}
    assert h.time_instant("15m") == {"relative": "15m"}
    assert h.time_instant("now") == {"relative": "now"}
    assert h.time_instant(1756684800) == {"unix_nano": "1756684800000000000"}
    assert h.time_instant(h.datetime(2026, 9, 1, tzinfo=h.timezone.utc)) == {"absolute": "2026-09-01T00:00:00Z"}
    b = h.Insights.logs_body("1h", "now", order_by="-timestamp", limit=5, cursor="abc")
    assert b["order_by"] == [{"field": {"name": "timestamp"}, "direction": "SORT_DIRECTION_DESC"}]
    assert b["pagination"] == {"limit": 5, "cursor": "abc"}
    validate(b, {"$ref": "logs_search_request.yml"})
    assert h.Insights.logs_body("1h", "now", order_by="severity_number")["order_by"][0]["direction"] == \
        "SORT_DIRECTION_ASC"
    with pytest.raises(ValueError):
        h.Insights.logs_body("1h", "now", limit=1001)


def test_iter_logs_follows_cursor():
    pages = {None: {"data": [{"timestamp": "t3"}, {"timestamp": "t2"}],
                    "pagination": {"has_more": True, "next_cursor": "c1"}},
             "c1": {"data": [{"timestamp": "t1"}], "pagination": {"has_more": True, "next_cursor": "c2"}},
             "c2": {"pagination": {"has_more": False}}}

    def responder(req):
        cursor = body(req).get("pagination", {}).get("cursor")
        return httpx.Response(200, json=pages[cursor])

    ins, rec = make(responder)
    out = list(ins.iter_logs("now-1h", "now", limit=2))
    assert [r["timestamp"] for r in out] == ["t3", "t2", "t1"]
    assert [body(r).get("pagination", {}).get("cursor") for r in rec.requests] == [None, "c1", "c2"]
    # timestamp ordering is the default (cursors require it)
    assert body(rec.requests[0])["order_by"] == [{"field": {"name": "timestamp"}, "direction": "SORT_DIRECTION_DESC"}]
    rec.requests.clear()
    assert len(list(ins.iter_logs("now-1h", "now", max_pages=1))) == 2
    assert len(rec.requests) == 1


# --- errors, rate limits, trace ---------------------------------------------------------------

def test_http_error_raises_insights_error():
    ins, _ = make(lambda r: httpx.Response(422, json={"status": "error", "errorType": "execution",
                                                     "error": "query processing exceeded the discovery time limit"}))
    with pytest.raises(h.InsightsError) as ei:
        ins.labels()
    e = ei.value
    assert e.status == 422
    assert e.body["errorType"] == "execution"
    assert e.request_summary.startswith("GET /v2/insights/query/tor1/prom/api/v1/labels?start=")
    assert TOKEN not in str(e)


def test_non_json_response_raises_with_html_excerpt():
    html = "<html><head><title>DigitalOcean - Maintenance</title></head><body>down</body></html>"
    ins, _ = make(lambda r: httpx.Response(404, text=html, headers={"content-type": "text/html"}))
    with pytest.raises(h.InsightsError) as ei:
        ins.query("up", region="mkc1")
    assert ei.value.status == 404
    assert "Maintenance" in str(ei.value)
    ins, _ = make(lambda r: httpx.Response(200, text="not json"))
    with pytest.raises(h.InsightsError):
        ins.list_rules()


def test_empty_body_returns_empty_dict():
    ins, _ = make(lambda r: httpx.Response(204))
    assert ins.delete_rule(RULE_ID) == {}


def test_429_sleeps_until_reset_and_retries_once():
    now = h.time.time()
    calls = []

    def responder(req):
        calls.append(req)
        return httpx.Response(429, headers={"ratelimit-reset": str(int(now + 5))}, json={"id": "too_many_requests"}) \
            if len(calls) == 1 else httpx.Response(200, json={"alert_rules": []})

    slept = []
    ins, _ = make(responder, sleep=slept.append)
    assert ins.list_rules() == {"alert_rules": []}
    assert len(calls) == 2
    assert 3 <= slept[0] <= 6

    ins, rec = make(lambda r: httpx.Response(429, headers={"ratelimit-reset": "2"}, json={}), sleep=slept.append)
    with pytest.raises(h.InsightsError) as ei:
        ins.list_rules()
    assert ei.value.status == 429
    assert len(rec.requests) == 2  # exactly one retry


def test_trace_prints_requests_and_redacts_secrets():
    buf = io.StringIO()
    ins, _ = make(lambda r: httpx.Response(201, json={}), trace=True, trace_stream=buf)
    ins.create_channel(h.webhook_channel("w", "https://x.example/h", bearer="sekrit-bearer", secret="sekrit-sig"))
    out = buf.getvalue()
    assert "→ POST /v2/insights/notification-channels" in out
    assert "← 201" in out and " ms" in out
    assert "sekrit" not in out and TOKEN not in out
    assert '"token": "***"' in out


# --- CLI --------------------------------------------------------------------------------------

def test_cli_write_probes_refuse_without_write(capsys):
    ins, rec = make()
    assert h.run(["probe", "naming", CHANNEL_ID], client=ins) == 2
    assert h.run(["probe", "tags", CHANNEL_ID], client=ins) == 2
    assert rec.requests == []
    assert "--write" in capsys.readouterr().err


def test_cli_delete_needs_yes():
    ins, rec = make()
    assert h.run(["rules", "delete", RULE_ID], client=ins) == 2
    assert rec.requests == []
    assert h.run(["rules", "delete", RULE_ID, "--yes"], client=ins) == 0
    assert rec.last.method == "DELETE"


def test_cli_table_and_json(capsys):
    rules = {"alert_rules": [{"id": RULE_ID, "status": "ALERT_RULE_STATUS_ACTIVE", "spec": h.rule_spec(
        "CPU is running high", "do.droplets.cpu_utilization", ">", critical=70)}],
        "pagination": {"page": 1, "pages": 1, "per_page": 20}}
    ins, _ = make(lambda r: httpx.Response(200, json=rules))
    assert h.run(["rules", "list"], client=ins) == 0
    out = capsys.readouterr().out
    assert "CPU is running high" in out and "GREATER_THAN" in out and "ACTIVE" in out
    assert h.run(["rules", "list", "--json"], client=ins) == 0
    assert json.loads(capsys.readouterr().out) == rules


def test_cli_rule_create_from_flags():
    ins, rec = make(lambda r: httpx.Response(201, json={"alert_rule": {"id": RULE_ID}}))
    assert h.run(["rules", "create", "--name", "Churn", "--metric", "do.droplets.cpu_utilization", "--op", ">=",
                  "--warning", "60", "--critical", "90", "--window", "1m", "--urn", "do:droplet:1",
                  "--channel", f"{CHANNEL_ID}:critical", "--filter", "host_id=1", "--status", "paused",
                  "--json"], client=ins) == 0
    sent = body(rec.last)
    validate(sent, {"$ref": "alert_rule_create_request.yml"})
    assert sent["status"] == "ALERT_RULE_STATUS_PAUSED"
    assert sent["spec"]["condition"]["window"] == "EVALUATION_WINDOW_1M"


def test_cli_logs_search_and_region_after_verb(capsys):
    ins, rec = make(lambda r: httpx.Response(200, json={"data": [{"timestamp": "t", "severity_text": "ERROR",
                                                                   "body": "boom"}]}))
    assert h.run(["logs", "search", "--severity", "ERROR", "--text", "boom", "--limit", "5"], client=ins) == 0
    sent = body(rec.last)
    assert set(sent["filter"]) == {"and"}
    validate(sent, {"$ref": "logs_search_request.yml"})
    assert "boom" in capsys.readouterr().out
    # --region after the verb is honoured when the client is built by the CLI
    args = h.build_parser().parse_args(["prom", "query", "up", "--region", "syd1", "--json"])
    assert args.region == "syd1" and args.json is True


def test_cli_http_error_exit_code(capsys):
    ins, _ = make(lambda r: httpx.Response(404, json={"id": "not_found", "message": "nope"}))
    assert h.run(["rules", "get", RULE_ID], client=ins) == 1
    assert "HTTP 404" in capsys.readouterr().err


# --- probes (mocked) --------------------------------------------------------------------------

def test_probe_naming_creates_and_always_deletes(capsys):
    created_id = "11111111-2222-3333-4444-555555555555"

    def responder(req):
        if req.method == "POST":
            metric = body(req)["spec"]["query"]["metric"]
            if "_" in metric.split(".")[0]:
                return httpx.Response(422, json={"id": "unprocessable_entity", "message": "dotted names only"})
            return httpx.Response(201, json={"alert_rule": {"id": created_id}})
        if req.method == "GET":
            raise httpx.ConnectError("boom")  # fails mid-probe: cleanup must still run
        return httpx.Response(204)

    ins, rec = make(responder)
    assert h.run(["probe", "naming", CHANNEL_ID, "--write"], client=ins) == 1
    deletes = [r for r in rec.requests if r.method == "DELETE"]
    assert [r.url.path for r in deletes] == [f"/v2/insights/alert-rules/{created_id}"]
    posts = [body(r) for r in rec.requests if r.method == "POST"]
    assert all(p["status"] == "ALERT_RULE_STATUS_PAUSED" for p in posts)
    captured = capsys.readouterr()
    assert "ConnectError" in captured.err
    out = captured.out
    assert "PASS naming: create rule metric=do_droplets_cpu_utilization" in out
    assert "PASS naming: cleanup" in out


def test_probe_tags_reports_and_deletes(capsys):
    rid = "99999999-2222-3333-4444-555555555555"

    def responder(req):
        if req.method == "POST":
            return httpx.Response(201, json={"alert_rule": {"id": rid}})
        if req.method == "GET":
            return httpx.Response(200, json={"alert_rule": {"id": rid, "spec": {"query": {"tags": ["insights-demo"]}}}})
        return httpx.Response(204)

    ins, rec = make(responder)
    assert h.run(["probe", "tags", CHANNEL_ID, "--write"], client=ins) == 0
    assert body(rec.requests[0])["spec"]["query"]["tags"] == ["insights-demo"]
    assert rec.requests[-1].method == "DELETE" and rec.requests[-1].url.path.endswith(rid)
    assert "round-trips" in capsys.readouterr().out


def test_probe_regions_flags_non_json(capsys):
    def responder(req):
        region = req.url.path.split("/")[4]
        if region == "mkc1":
            return httpx.Response(404, text="<title>DigitalOcean - Maintenance</title>",
                                  headers={"content-type": "text/html"})
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [
            {"metric": {}, "value": [1, "6"]}] if region == "nyc3" else []}})

    ins, _ = make(responder)
    assert h.run(["probe", "regions", "--only", "nyc3", "--only", "mem1", "--only", "mkc1"], client=ins) == 1
    out = capsys.readouterr().out
    assert "PASS regions: nyc3" in out and "= 6" in out
    assert "PASS regions: mem1" in out and "0 (no series)" in out
    assert "FAIL regions: mkc1 — non-JSON response: HTTP 404" in out and "Maintenance" in out


def test_probe_labels_window_and_endpoints(capsys):
    def responder(req):
        if req.url.path.endswith("/labels"):
            if "start" not in qs(req):
                return httpx.Response(422, json={"status": "error", "errorType": "execution"})
            return httpx.Response(200, json={"status": "success", "data": ["__name__", "resource_urn"]})
        if req.url.path.startswith("/v2/insights/query/"):
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})
        return httpx.Response(404, json={"id": "not_found", "message": "Your request could not be routed."})

    ins, _ = make(responder)
    assert h.run(["probe", "labels-window"], client=ins) == 0
    assert h.run(["probe", "endpoints"], client=ins) == 0
    out = capsys.readouterr().out
    assert "PASS labels-window: labels without window — HTTP 422" in out
    assert "2 labels" in out
    assert out.count("PASS endpoints") == 3


def test_probe_rules_visibility_detects_a2(capsys):
    def responder(req):
        p = req.url.path
        if p == "/v2/insights/alert-rules":
            return httpx.Response(200, json={"alert_rules": [], "pagination": {"page": 1, "pages": 1}})
        if p == "/v2/insights/notification-channels":
            return httpx.Response(200, json={"notification_channels": [{"id": "c", "name": "n",
                                                                         "usage": {"rule_count": 2}}]})
        if p == "/v2/insights/alert-instances":
            return httpx.Response(200, json={"alert_instances": [{"rule_id": RULE_ID}, {"rule_id": RULE_ID}]})
        return httpx.Response(200, json={"alert_rule": {"id": RULE_ID, "spec": {"name": "CPU is running high"}}})

    ins, _ = make(responder)
    assert h.run(["probe", "rules-visibility"], client=ins) == 1
    out = capsys.readouterr().out
    assert f"FAIL rules-visibility: rule {RULE_ID} — GET by id 200 ('CPU is running high') but missing" in out


def test_probe_logs_api(capsys):
    def responder(req):
        b = body(req)
        if not b:
            return httpx.Response(400, json={"error": "time_range is required", "code": 3})
        cursor = (b.get("pagination") or {}).get("cursor")
        data = [{"timestamp": "2026-10-08T22:00:0%dZ" % i, "severity_text": "ERROR"} for i in (5, 4)] if not cursor \
            else [{"timestamp": "2026-10-08T22:00:03Z", "severity_text": "ERROR"}]
        return httpx.Response(200, json={"data": data, "pagination": {"has_more": not cursor, "next_cursor": "c1"}})

    ins, rec = make(responder)
    assert h.run(["probe", "logs-api"], client=ins) == 0
    out = capsys.readouterr().out
    assert "PASS logs-api: empty body {}" in out
    assert "PASS logs-api: cursor ordering" in out
    for r in rec.requests[1:]:
        validate(body(r), {"$ref": "logs_search_request.yml"})
