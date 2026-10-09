"""The fake Insights matches the facts pack: shapes, error texts, absent data key, enums, A1, A2, A3, A6b, A15."""
from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
from conftest import FakeClock, fleet_text

from fake_insights import FakeInsights
from fake_tentacles import FakeFleet
from insights_harness import Insights, InsightsError, cond, order
from porthole.config import Fleet

TOK = {"Authorization": "Bearer test-token-0000"}


@pytest.fixture
def world():
    clock = FakeClock()
    fleet = Fleet.from_json(fleet_text())
    tentacles = FakeFleet(fleet, key="k", clock=clock)
    sent: list[dict] = []
    fake = FakeInsights(fleet, tentacles, clock, hook_bearer="hb-0000000000", hook_secret="hs-0000000000",
                        on_notify=sent.append, public_url="https://porthole.example.test")
    client = httpx.Client(transport=fake.transport(), base_url="http://fake", headers=TOK)
    ins = Insights("test-token-0000", transport=fake.transport(), base_url="http://fake")
    return {"clock": clock, "fleet": fleet, "tentacles": tentacles, "fake": fake, "http": client, "ins": ins,
            "sent": sent}


def test_requires_a_bearer(world):
    r = httpx.Client(transport=world["fake"].transport(), base_url="http://fake").get(
        "/v2/insights/query/tor1/prom/api/v1/query", params={"query": "count(do.droplets.cpu_utilization)"})
    assert r.status_code == 401 and r.json() == {"id": "Unauthorized", "message": "Unable to authenticate you"}


def test_range_shape_with_underscored_names(world):
    now = world["clock"].now().timestamp()
    body = world["ins"].query_range("do.droplets.cpu_utilization", now - 600, now, "60s", region="tor1")
    assert body["status"] == "success" and body["data"]["resultType"] == "matrix"
    series = body["data"]["result"]
    assert {s["metric"]["resource_urn"] for s in series} == {"do:droplet:600000001", "do:droplet:600000002"}
    m = series[0]["metric"]
    assert set(m) == {"__name__", "do_tags", "resource_urn", "service_name"}  # finding A22, as observed
    assert m["__name__"] == "do_droplets_cpu_utilization" and m["service_name"] == "kraken-tentacle-1"
    t, v = series[0]["values"][0]
    assert isinstance(t, int) and isinstance(v, str) and float(v) >= 0


def test_droplet_names_behind_a_flag(world):
    """With droplet_names the Droplet series carry resource_name, as older Droplets do; other kinds always do. The
    CPU series never does (A22), so memory is asked."""
    q = "count by (resource_urn, resource_name) (do.droplets.memory_utilization)"
    named = FakeInsights(world["fleet"], world["tentacles"], world["clock"], droplet_names=True)
    ins = Insights("test-token-0000", transport=named.transport(), base_url="http://fake")
    assert {s["metric"].get("resource_name") for s in ins.query(q, region="tor1")["data"]["result"]} == {
        "kraken-tentacle-1", "kraken-tentacle-2"}
    assert {s["metric"].get("resource_name") for s in world["ins"].query(q, region="tor1")["data"]["result"]} == {None}
    lb = world["ins"].query("count by (resource_name) (do.load_balancers.requests_per_second)", region="tor1")
    assert [s["metric"] for s in lb["data"]["result"]] == [{"resource_name": "kraken-lb"}]


def test_region_label_varies_per_metric(world):
    """A22: the Droplet CPU series has no resource_region_slug while its memory series has one, so pinning the
    region by label empties the CPU query (B-026). The region is the path segment."""
    urn = 'resource_urn="do:droplet:600000001"'
    pinned = world["ins"].query(f'do.droplets.cpu_utilization{{resource_region_slug="tor1", {urn}}}', region="tor1")
    assert pinned["data"]["result"] == []
    assert len(world["ins"].query(f"do.droplets.cpu_utilization{{{urn}}}", region="tor1")["data"]["result"]) == 1
    memory = world["ins"].query(f'do.droplets.memory_utilization{{resource_region_slug="tor1", {urn}}}',
                                region="tor1")["data"]["result"]
    assert [s["metric"]["resource_region_slug"] for s in memory] == ["tor1"]


def test_aggregation_keeps_only_the_grouping_label(world):
    body = world["ins"].query("avg by (resource_urn) (do.droplets.cpu_utilization)", region="tor1")
    assert body["data"]["resultType"] == "vector"
    assert all(set(s["metric"]) == {"resource_urn"} for s in body["data"]["result"])
    by_name = world["ins"].query("avg by (resource_name) (do.droplets.cpu_utilization)", region="tor1")
    assert [s["metric"] for s in by_name["data"]["result"]] == [{}]  # B-023: both tentacles fold into one series


def test_underscored_names_are_rejected_with_422(world):
    with pytest.raises(InsightsError) as err:
        world["ins"].query("avg(do_droplets_cpu_utilization)", region="tor1")
    assert err.value.status == 422 and err.value.body["status"] == "error"


def test_discovery_needs_a_window(world):  # finding A1
    r = world["http"].get("/v2/insights/query/tor1/prom/api/v1/labels")
    assert r.status_code == 422
    assert r.json() == {"status": "error", "errorType": "execution",
                        "error": "query processing exceeded the discovery time limit in query execution"}
    assert "resource_urn" in world["ins"].labels(region="tor1")["data"]


def test_catalog_is_per_region(world):
    tor = world["ins"].label_values("__name__", region="tor1")["data"]
    syd = world["ins"].label_values("__name__", region="syd1")["data"]
    assert "do_apps_app_requests_per_second" in tor and "do_load_balancers_requests_per_second" in tor
    assert all(n.startswith("do_droplets_") for n in syd)
    assert world["ins"].label_values("__name__", region="ams3")["data"] == []


def test_mkc1_serves_the_maintenance_page_and_mem1_is_empty(world):  # finding A3
    r = world["http"].get("/v2/insights/query/mkc1/prom/api/v1/query", params={"query": "1"})
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "DigitalOcean - Maintenance" in r.text
    assert world["ins"].query("count(do.droplets.cpu_utilization)", region="mem1")["data"]["result"] == []


def test_logs_errors_follow_the_facts_pack(world):  # finding A15
    r = world["http"].post("/v2/insights/query/tor1/logs/search", json={})
    assert r.status_code == 400 and r.json() == {"error": "time_range is required", "code": 3}
    r = world["http"].post("/v2/insights/query/tor1/logs/search",
                           json={"time_range": {"from": "2026-10-12T13:00:00Z", "to": "2026-10-12T14:00:00Z"}})
    assert r.status_code == 400
    assert r.json()["error"] == "json: cannot unmarshal string into Go value of type map[string]jsontext.Value"


def test_logs_data_key_absent_when_empty(world):  # finding A6b: droplet logs are not collected
    body = world["ins"].search_logs("now-1h", "now", cond("service.name", "=", "kraken-tentacle-1"), region="tor1")
    assert "data" not in body and body["pagination"]["has_more"] is False


def test_logs_with_droplet_logs_on_page_by_cursor(world):
    fake = FakeInsights(world["fleet"], world["tentacles"], world["clock"], droplet_logs=True)
    ins = Insights("test-token-0000", transport=fake.transport(), base_url="http://fake")
    t1 = world["tentacles"].by_name("kraken-tentacle-1")
    t1.start("logs", {"seconds": "60", "rate": "50", "error_pct": "10"}, world["clock"].now())
    world["clock"].t += 120
    flt = cond("service.name", "=", "kraken-tentacle-1")
    first = ins.search_logs("now-1h", "now", flt, [order("timestamp", "desc")], limit=1000, region="tor1")
    assert len(first["data"]) == 1000 and first["pagination"]["has_more"] is True
    rec = first["data"][0]
    assert {"timestamp", "severity_number", "severity_text", "body", "service_name", "resource",
            "attributes"} <= set(rec)
    assert rec["service_name"] == "kraken-tentacle-1" and rec["attributes"]["scenario.name"] == "logs"
    records = list(ins.iter_logs("now-1h", "now", flt, limit=1000, region="tor1"))
    assert len(records) == 3000
    errors = ins.search_logs("now-1h", "now", cond("severity_number", ">=", 17), limit=1000, region="tor1")
    assert all(r["severity_text"] == "ERROR" for r in errors["data"])
    unordered = ins.search_logs("now-1h", "now", flt, None, limit=10, region="tor1")
    assert unordered["pagination"]["has_more"] is False  # cursors only with timestamp ordering


def test_rule_list_is_empty_while_get_by_id_works(world):  # finding A2
    assert world["ins"].list_rules(per_page=100)["alert_rules"] == []
    rule = world["ins"].get_rule("00000000-0000-0000-0000-0000000000a1")["alert_rule"]
    assert rule["status"] == "ALERT_RULE_STATUS_ACTIVE" and rule["owner_id"]
    assert rule["spec"]["query"]["metric"] == "do.droplets.cpu_utilization"
    assert rule["spec"]["query"]["resource_urns"] == ["do:droplet:600000001"]
    assert rule["spec"]["condition"]["window"] == "EVALUATION_WINDOW_1M"
    assert rule["spec"]["thresholds"]["operator"] == "THRESHOLD_OPERATOR_GREATER_THAN_OR_EQUAL"
    paused = world["ins"].get_rule("00000000-0000-0000-0000-0000000000a2")["alert_rule"]
    assert paused["status"] == "ALERT_RULE_STATUS_PAUSED"
    with pytest.raises(InsightsError) as err:
        world["ins"].get_rule("00000000-0000-0000-0000-00000000ffff")
    assert err.value.status == 404


def test_rule_create_and_update_rules(world):
    ins = world["ins"]
    ch = "00000000-0000-0000-0000-0000000000c1"
    with pytest.raises(InsightsError) as err:
        ins.create_rule(ins.rule_spec("u", "do_droplets_cpu_utilization", ">", critical=1, channels=[ch]))
    assert err.value.status == 422  # the reference says 422; the live API returned 201 (see BUGS.md)
    created = ins.create_rule(ins.rule_spec("d", "do.droplets.cpu_utilization", ">", critical=99, channels=[ch]),
                              status="paused")["alert_rule"]
    assert created["status"] == "ALERT_RULE_STATUS_PAUSED"
    spec = ins.rule_spec("d", "do.droplets.cpu_utilization", ">", critical=98)
    updated = ins.update_rule(created["id"], spec, status="active")["alert_rule"]
    assert updated["spec"]["notification_channels"][0]["notification_channel_id"] == ch
    with pytest.raises(InsightsError) as err:
        ins.update_rule(created["id"], {**spec, "notification_channels": []})
    assert err.value.status == 400


def test_channels_return_status_objects_not_secrets(world):
    ins = world["ins"]
    channels = ins.list_channels()["notification_channels"]
    hook = next(c for c in channels if c["channel_type"] == "CHANNEL_TYPE_WEBHOOK")
    assert hook["webhook"]["url"] == "https://porthole.example.test/hooks/insights"
    assert set(hook["webhook"]) >= {"bearer_token_status", "signature_status"}
    made = ins.create_channel(ins.webhook_channel("x", "https://example.com/h", bearer="tok-123456789",
                                                  secret="sig-123456789"))["notification_channel"]
    text = str(made)
    assert "tok-123456789" not in text and "sig-123456789" not in text
    assert made["webhook"]["bearer_token_status"]["is_set"] is True


def test_scripted_instance_state_machine_and_webhook(world):
    fake, ins = world["fake"], world["ins"]
    rid, urn = "00000000-0000-0000-0000-0000000000a1", "do:droplet:600000001"
    history = ins.list_instances(rule_id=rid)["alert_instances"]
    assert history and all(i["status"] == "ALERT_INSTANCE_STATUS_RESOLVED" for i in history)
    fake.store.fire(rid, urn, 97.5)
    active = ins.list_instances(rule_id=rid, status="active")["alert_instances"]
    assert len(active) == 1 and active[0]["severity"] == "SEVERITY_CRITICAL" and active[0]["resolved_at"] is None
    assert set(active[0]) == {"id", "rule_id", "severity", "status", "resource_urn", "value", "triggered_at",
                              "resolved_at", "muted"}
    fake.store.resolve(rid, urn)
    assert ins.list_instances(rule_id=rid, status="active")["alert_instances"] == []
    kinds = [d["kind"] for d in world["sent"]]
    assert kinds == ["ALERT_TRIGGERED", "ALERT_RESOLVED"]
    d = world["sent"][0]
    assert d["headers"]["authorization"] == "Bearer hb-0000000000" and d["headers"]["x-kraken"] == "1"
    assert len(d["headers"]["x-signature"]) == 64


def test_metric_driven_alert_follows_a_cpu_run(world):
    clock, fake = world["clock"], world["fake"]
    rid = "00000000-0000-0000-0000-0000000000a1"
    world["tentacles"].by_name("kraken-tentacle-1").start("cpu", {"seconds": "300", "workers": "1"}, clock.now())
    clock.t += 30
    fake.tick()
    assert not fake.store._active(rid)  # the metric lags a minute behind the burn
    clock.t += 120
    fake.tick()
    assert fake.store._active(rid)
    clock.t += 400
    fake.tick()
    assert not fake.store._active(rid)
    assert [d["kind"] for d in world["sent"]] == ["ALERT_TRIGGERED", "ALERT_RESOLVED"]
    assert clock.now() - world["clock"].start_wall == timedelta(seconds=550)


def same_shape(sample, actual, path="$"):
    """Every key of the sample exists in the fake's answer with a value of the same kind (None matches anything)."""
    if sample is None or actual is None:
        return
    if isinstance(sample, dict):
        assert isinstance(actual, dict), path
        for k, v in sample.items():
            if not k.startswith("_"):
                assert k in actual, f"{path}.{k} missing"
                same_shape(v, actual[k], f"{path}.{k}")
    elif isinstance(sample, list):
        assert isinstance(actual, list), path
        if sample and actual:
            same_shape(sample[0], actual[0], f"{path}[0]")
    else:
        kinds = (int, float) if isinstance(sample, (int, float)) and not isinstance(sample, bool) else type(sample)
        assert isinstance(actual, kinds), f"{path}: {type(actual).__name__} is not {type(sample).__name__}"


def test_the_fake_matches_the_sample_shapes(world):
    from conftest import fixture
    ins, now = world["ins"], world["clock"].now().timestamp()
    same_shape(fixture("prom_matrix.json"), ins.query_range("do.droplets.cpu_utilization", now - 300, now, "60s",
                                                            region="tor1"))
    same_shape(fixture("label_values.json"), ins.label_values("__name__", region="tor1"))
    same_shape(fixture("alert_rule.json"), ins.get_rule("00000000-0000-0000-0000-0000000000a1"))
    same_shape(fixture("alert_instances.json"), ins.list_instances(rule_id="00000000-0000-0000-0000-0000000000a1"))
    same_shape(fixture("channels.json"), ins.list_channels())
    fake = FakeInsights(world["fleet"], world["tentacles"], world["clock"], droplet_logs=True)
    logs = Insights("test-token-0000", transport=fake.transport(), base_url="http://fake")
    world["tentacles"].by_name("kraken-tentacle-1").start("logs", {"seconds": "60", "rate": "10"}, world["clock"].now())
    world["clock"].t += 90
    page = logs.search_logs("now-1h", "now", cond("service.name", "=", "kraken-tentacle-1"),
                            [order("timestamp", "desc")], limit=5, region="tor1")
    same_shape(fixture("logs_page.json"), page)
