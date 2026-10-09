"""Panels against the fake Insights: normalization, both regions, catalog cache, budget, alerts, logs, A6b."""
from __future__ import annotations

import asyncio
import json

from conftest import CAPTAIN, TOKEN, AppEnv, base_env


def range_params(**kw):
    return {"region": "tor1", "metric": "do.droplets.cpu_utilization", "agg": "avg", "range": "30m", **kw}


def upstream(env, part: str = "") -> int:
    return len([r for r in env.insights.requests if part in r[1]])


async def test_range_normalization_gaps_and_step(env):
    body = (await env.client.get("/api/insights/range", params=range_params(region="syd1", range="1h"))).json()
    assert body["unit"] == "percent" and body["step"] == 60 and body["end"] - body["start"] == 3600
    (series,) = body["series"]
    assert (series["entity"], series["display"], series["region"], series["slot"]) == \
        ("kraken-tentacle-3", "tentacle-3", "syd1", 3)
    stamps = [p[0] for p in series["points"]]
    assert all(t % 60 == 0 and body["start"] <= t <= body["end"] for t in stamps)
    gaps = [t for t in range(body["start"], body["end"] + 1, 60) if t not in stamps]
    assert gaps and all((t // 60) % 23 == 0 for t in gaps)  # the fake drops these samples; no zeros filled in
    coarse = (await env.client.get("/api/insights/range", params=range_params(step="120s"))).json()
    assert coarse["step"] == 120
    assert all(b[0] - a[0] == 120 for s in coarse["series"] for a, b in zip(s["points"], s["points"][1:], strict=False))


async def test_both_regions_are_asked_separately_and_never_summed(env):
    body = (await env.client.get("/api/insights/range", params=range_params(region="both"))).json()
    assert set(body["regions"]) == {"tor1", "syd1"}
    assert all(len(r["calls"]) == 1 and r["error"] is None for r in body["regions"].values())
    assert body["regions"]["syd1"]["promql"] == \
        'avg by (resource_urn) (do.droplets.cpu_utilization{resource_urn=~"do:droplet:600000003"})'
    assert all("resource_region_slug" not in r["promql"] for r in body["regions"].values())  # B-026
    by = {(s["entity"], s["region"]) for s in body["series"]}
    assert by == {("kraken-tentacle-1", "tor1"), ("kraken-tentacle-2", "tor1"), ("kraken-tentacle-3", "syd1")}
    urns = {t.name: t.urn for t in env.deps.settings.fleet.tentacles}
    assert all(s["labels"] == {"resource_urn": urns[s["entity"]]} for s in body["series"])
    assert {s["display"] for s in body["series"]} == {"tentacle-1", "tentacle-2", "tentacle-3"}


async def test_bad_region_and_builder_rejections(env):
    r = await env.client.get("/api/insights/range", params=range_params(region="ams3"))
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_region"
    r = await env.client.get("/api/insights/range", params=range_params(filters="resource_name=not-ours"))
    assert r.status_code == 400 and "not in this fleet" in r.json()["error"]["message"]
    r = await env.client.get("/api/insights/range", params=range_params(metric="do_droplets_cpu_utilization"))
    assert r.status_code == 400 and "dotted" in r.json()["error"]["message"]


async def test_catalog_grouped_and_cached_for_five_minutes(env):
    first = (await env.client.get("/api/insights/catalog", params={"region": "tor1"})).json()
    fam = first["families"]["do.droplets"]
    assert {"dotted": "do.droplets.cpu_utilization", "underscored": "do_droplets_cpu_utilization"} in fam
    assert "do.load_balancers" in first["families"] and first["window"] == "30m" and first["cached"] is False
    n = upstream(env, "label/__name__/values")
    await env.clock.advance(299)
    again = (await env.client.get("/api/insights/catalog", params={"region": "tor1"})).json()
    assert again["cached"] is True and upstream(env, "label/__name__/values") == n
    await env.clock.advance(2)
    assert (await env.client.get("/api/insights/catalog", params={"region": "tor1"})).json()["cached"] is False
    both = (await env.client.get("/api/insights/catalog", params={"region": "both"})).json()
    assert set(both["regions"]) == {"tor1", "syd1"}


async def test_single_flight_for_concurrent_misses(env):
    n = upstream(env, "query_range")
    results = await asyncio.gather(*(env.client.get("/api/insights/range", params=range_params(range="3h"))
                                     for _ in range(5)))
    assert all(r.status_code == 200 for r in results)
    assert upstream(env, "query_range") == n + 1


async def test_cache_is_bounded_by_bytes_not_only_entries():
    """Found by the wringer (pass 2): 294 distinct 24 h range entries held 53 MB; a visitor could fill the
    500-entry cap with heavy metrics and take the 512 MiB instance down. The cache now evicts by JSON size."""
    from porthole.cache import PanelCache
    cache = PanelCache(lambda: 0.0, lambda: "now", max_entries=500, max_bytes=10_000)
    big = {"series": [{"points": [[1760000000 + 60 * i, 1.0] for i in range(150)]}]}  # about 3 KB of JSON
    assert 2_500 < PanelCache.weight(big) < 3_500

    async def fetch(v: dict = big) -> dict:
        return v

    for i in range(5):
        await cache.get(f"range:{i}", fetch, 20)
    assert cache.bytes <= 10_000 and len(cache._entries) == 3
    assert cache.peek("range:0") is None and cache.peek("range:1") is None and cache.peek("range:4") is not None
    await cache.get("huge", lambda: fetch({"series": [{"points": [[i, 1.0] for i in range(2000)]}]}), 20)
    assert cache.peek("huge") is None and cache.bytes <= 10_000  # a value over the whole budget is not kept
    cache.invalidate("range:")
    assert cache.bytes == 0 and cache._entries == {}


async def test_cache_waiters_survive_the_first_callers_cancellation():
    """Found by the wringer: cancelling the request that started a fetch cancelled every request sharing it."""
    from porthole.cache import PanelCache
    cache = PanelCache(lambda: 0.0, lambda: "now")
    gate = asyncio.Event()
    fetches = 0

    async def fetch() -> dict:
        nonlocal fetches
        fetches += 1
        await gate.wait()
        return {"v": fetches}

    leader = asyncio.create_task(cache.get("k", fetch, 20))
    await asyncio.sleep(0)
    follower = asyncio.create_task(cache.get("k", fetch, 20))
    await asyncio.sleep(0)
    leader.cancel()
    await asyncio.sleep(0)
    gate.set()
    result = await asyncio.wait_for(follower, 2)
    assert result.value == {"v": 2} and result.cached is False and fetches == 2
    assert leader.cancelled()
    assert (await cache.get("k", fetch, 20)).cached is True  # the follower's fetch filled the cache


async def test_budget_exhaustion_serves_stale_with_retry_in():
    async with AppEnv(base_env(PORTHOLE_UPSTREAM_BUDGET_PER_MIN="6")) as e:
        fresh = (await e.client.get("/api/insights/range", params=range_params(range="1h"))).json()
        assert fresh["stale"] is False
        while e.deps.budget.remaining():
            e.deps.budget.take()
        await e.clock.advance(25)  # past the 20 s TTL, still inside the budget minute
        stale = (await e.client.get("/api/insights/range", params=range_params(range="1h"))).json()
        assert stale["stale"] is True and stale["retry_in"] >= 1
        assert stale["series"] == fresh["series"]
        cold = (await e.client.get("/api/insights/range", params=range_params(range="6h"))).json()
        assert cold["series"] == [] and cold["regions"]["tor1"]["error"]["status"] == 503
        assert "retry in" in cold["regions"]["tor1"]["error"]["message"]


async def test_captain_promql_within_caps(env):
    h = env.captain()
    q = {"region": "both", "query": "max by (resource_name) (do.droplets.load_1)", "range": "2h", "step": "60s"}
    r = await env.client.post("/api/insights/promql", json=q, headers=h)
    assert r.status_code == 200 and set(r.json()["regions"]) == {"tor1", "syd1"} and r.json()["series"]
    for bad in ({**q, "query": "x" * 501}, {**q, "range": "25h"}, {**q, "step": "30s"}):
        r = await env.client.post("/api/insights/promql", json=bad, headers=h)
        assert r.status_code == 400, bad
    assert (await env.client.post("/api/insights/promql", json=q)).status_code == 401


async def test_instant_query(env):
    body = (await env.client.get("/api/insights/query", params={"region": "tor1",
                                                                 "metric": "do.droplets.cpu_utilization"})).json()
    assert {s["entity"] for s in body["series"]} == {"kraken-tentacle-1", "kraken-tentacle-2"}
    assert all(len(s["points"]) == 1 for s in body["series"])


async def test_labels_need_a_metric(env):
    r = await env.client.get("/api/insights/labels", params={"region": "tor1", "name": "filesystem_mountpoint"})
    assert r.status_code == 400
    r = await env.client.get("/api/insights/labels", params={"region": "tor1", "name": "filesystem_mountpoint",
                                                              "match": "do.droplets.filesystem_free_bytes"})
    assert r.json()["values"] == ["/"]


async def test_alerts_overview(env):
    body = (await env.client.get("/api/insights/alerts")).json()
    rt = next(r for r in body["rules"] if r["purpose"] == "round-trip")
    assert (rt["operator"], rt["critical"], rt["window"], rt["re_alert"], rt["status"]) == (">=", 60, "1m", "30m",
                                                                                           "active")
    assert len(body["rules"]) == 6 and body["errors"] == []
    assert all(i["rule_name"] == "kraken churn" and i["status"] == "resolved" for i in body["instances"])
    hook = next(c for c in body["channels"] if c["type"] == "webhook")
    assert hook["points_here"] and set(hook["statuses"]) == {"bearer_token_status", "signature_status"}
    assert body["write"] is False


async def test_rule_status_needs_write_mode_and_a_fleet_rule():
    rid = "00000000-0000-0000-0000-0000000000a2"
    async with AppEnv() as e:
        r = await e.client.post(f"/api/insights/rules/{rid}/status", json={"status": "active"}, headers=e.captain())
        assert r.status_code == 403 and r.json()["error"]["code"] == "write_mode_off"
    async with AppEnv(base_env(PORTHOLE_INSIGHTS_WRITE="1")) as e:
        r = await e.client.post("/api/insights/rules/not-ours/status", json={"status": "paused"}, headers=e.captain())
        assert r.status_code == 403
        r = await e.client.post(f"/api/insights/rules/{rid}/status", json={"status": "active"}, headers=e.captain())
        assert r.status_code == 200 and r.json()["rule"]["status"] == "active"
        put = next(c for c in reversed(e.deps.trace.ring) if c.method == "PUT")
        assert "notification_channels" not in put.body["spec"] and put.body["status"] == "ALERT_RULE_STATUS_ACTIVE"
        assert e.insights.store.rules[rid]["spec"]["notification_channels"]  # bindings kept


async def test_logs_page_and_rules(env):
    body = (await env.client.get("/api/insights/logs", params={"region": "tor1", "service": "porthole",
                                                               "severity": "INFO", "limit": 5})).json()
    assert body["count"] == 5 and body["summary"] == "Insights returned 5 records"
    sent = body["body_sent"]
    assert set(sent["time_range"]["from"]) == {"absolute"} and sent["pagination"]["limit"] == 5
    assert sent["order_by"][0]["direction"] == "SORT_DIRECTION_DESC"
    more = (await env.client.get("/api/insights/logs", params={
        "region": "tor1", "service": "porthole", "limit": 5, "cursor": body["pagination"]["next_cursor"],
        "start": body["window"]["start"], "end": body["window"]["end"]})).json()
    assert more["records"][0]["timestamp"] <= body["records"][-1]["timestamp"]
    for params, code in (({"region": "both"}, "one_region"), ({"service": "someone-else"}, "bad_service"),
                         ({"severity": "LOUD"}, "bad_severity"), ({"limit": 101}, "bad_limit"),
                         # found by the wringer: these answered 500 (ValueError from fromtimestamp)
                         ({"cursor": "x", "start": 99999999999990, "end": 99999999999999}, "bad_window"),
                         ({"cursor": "x", "start": -5, "end": 50}, "bad_window"),
                         ({"cursor": "x", "start": body["window"]["start"]}, "bad_window"),
                         ({"cursor": "x", "start": 0, "end": 90000}, "bad_window")):
        r = await env.client.get("/api/insights/logs", params={"region": "tor1", **params})
        assert r.status_code == 400 and r.json()["error"]["code"] == code, params


async def test_expected_logs_say_a6b_when_insights_has_none(env):
    env.fleet.by_name("kraken-tentacle-1").start("logs", {"seconds": "60", "rate": "50", "error_pct": "10"},
                                                 env.clock.now())
    await env.advance(180)
    (row,) = (await env.client.get("/api/insights/logs/expected", params={"range": "1h"})).json()
    assert row["emitted"] == 3000 and row["insights_count"] == 0 and row["verdict"] == "not collected (A6b)"
    assert row["by_severity"]["ERROR"] == 300
    assert row["text"] == ("Not yet collected by DigitalOcean: tentacle-1 reports 3,000 lines between 14:00Z and "
                           "14:01Z, Insights returned 0 for service kraken-tentacle-1 (finding A6b, checked 14:03Z)")


async def test_expected_logs_collected_when_droplet_logs_arrive():
    async with AppEnv(droplet_logs=True) as e:
        e.fleet.by_name("kraken-tentacle-1").start("logs", {"seconds": "60", "rate": "40", "error_pct": "5"},
                                                   e.clock.now())
        await e.advance(120)
        (row,) = (await e.client.get("/api/insights/logs/expected")).json()
        assert row["verdict"] == "collected" and row["insights_count"] == 2400
        assert row["text"].startswith("Collected: Insights returned 2,400 of 2,400 lines")


async def test_chain_runs_listed_with_trace_ids(env):
    env.fleet.by_name("kraken-tentacle-1").start("chain", {"count": "20", "latency_ms": "250", "error_pct": "20"},
                                                 env.clock.now())
    await env.advance(30)
    body = (await env.client.get("/api/traces/chains")).json()
    (chain,) = body["chains"]
    assert chain["ok"] + chain["failed"] == 20 and len(chain["first_trace_id"]) == 32
    assert body["traces_link"]["verified"] is True


async def test_dashboard_routes(env):
    """The committed dashboard: the sidecar listing, one chart run through the fleet-pinned template, the file."""
    body = (await env.client.get("/api/dashboards/krakens-eye")).json()
    assert body["file_present"] is True and body["raw"] is None
    assert body["file_url"] == "/watcher/dashboards/krakens-eye.json"
    charts = body["sidecar"]["charts"]
    assert charts[0]["title"] == "Tentacle CPU" and body["sidecar"]["variables"][0]["name"] == "tentacle"
    assert body["dashboards_link"]["verified"] is True
    run = (await env.client.get("/api/dashboards/krakens-eye/run", params={"index": 0, "region": "tor1"})).json()
    assert run["chart"]["title"] == "Tentacle CPU" and run["unit"] == "percent"
    assert "resource_region_slug" not in run["promql"] and "$region" not in run["promql"]  # B-026
    assert 'resource_urn=~"do:droplet:600000001|do:droplet:600000002"' in run["promql"]
    assert {s["entity"] for s in run["series"]} == {"kraken-tentacle-1", "kraken-tentacle-2"}
    both = (await env.client.get("/api/dashboards/krakens-eye/run", params={"index": 0, "region": "both"})).json()
    assert set(both["regions"]) == {"tor1", "syd1"} and "do:droplet:600000003" in both["regions"]["syd1"]["promql"]
    lb = next(i for i, c in enumerate(charts) if c["title"] == "Load balancer requests")
    lb_run = (await env.client.get("/api/dashboards/krakens-eye/run", params={"index": lb, "region": "tor1"})).json()
    assert 'resource_urn="do:loadbalancer:00000000-0000-0000-0000-000000000001"' in lb_run["promql"]
    assert [s["entity"] for s in lb_run["series"]] == ["kraken-lb"]
    logs_chart = next(i for i, c in enumerate(charts) if c["promql"] is None)
    for params, code in (({"index": logs_chart}, "no_query"), ({"index": 99}, "no_query"),
                         ({"index": 0, "range": "2h"}, "bad_range"), ({"index": 0, "region": "ams3"}, "bad_region")):
        r = await env.client.get("/api/dashboards/krakens-eye/run", params=params)
        assert r.status_code == 400 and r.json()["error"]["code"] == code, params
    assert (await env.client.get("/api/dashboards/krakens-eye/run")).status_code == 400  # index is required
    f = await env.client.get("/watcher/dashboards/krakens-eye.json")
    assert f.status_code == 200 and f.headers["content-type"].startswith("application/json")
    assert f.headers["content-disposition"] == 'attachment; filename="krakens-eye.json"'
    assert f.json()["name"] == "Kraken's Eye"


async def test_dashboard_page_without_the_sidecar(env, tmp_path):
    (tmp_path / "dashboards").mkdir()
    (tmp_path / "dashboards" / "krakens-eye.json").write_text('{"name": "raw only"}')
    env.deps.watcher_dir = tmp_path
    body = (await env.client.get("/api/dashboards/krakens-eye")).json()
    assert body["sidecar"] is None and json.loads(body["raw"]) == {"name": "raw only"} and body["file_present"]
    r = await env.client.get("/api/dashboards/krakens-eye/run", params={"index": 0})
    assert r.status_code == 404 and r.json()["error"]["code"] == "no_sidecar"
    (tmp_path / "dashboards" / "krakens-eye.json").unlink()
    assert (await env.client.get("/watcher/dashboards/krakens-eye.json")).status_code == 404


async def test_probes_fall_back_to_family_metrics():
    async with AppEnv(reject_metricless=True) as e:
        snap = (await e.client.get("/api/fleet")).json()
        assert snap["probe_mode"] == "family"
        assert all(t["seen_in_insights"] for t in snap["tentacles"])
        # the probe names of watcher/probe_metrics.json for these two are the ones the tor1 catalog has (B-033)
        assert snap["sea"]["functions"]["seen_in_insights"] is True
        assert snap["sea"]["load_balancer"]["seen_in_insights"] is True
        assert any("per-family probe metrics" in line["body"] for line in e.log_lines())


async def test_fleet_snapshot_shape(env):
    snap = (await env.client.get("/api/fleet")).json()
    t1 = snap["tentacles"][0]
    assert t1["reachable"] and t1["health"]["mem_pct"] > 0 and t1["seen_in_insights"] is True
    assert (t1["health"]["mem_avail_mb"], t1["health"]["mem_total_mb"]) == (590, 961)
    t3 = snap["tentacles"][2]  # not redeployed since mem_avail_mb: the fields are there, empty
    assert t3["health"]["mem_avail_mb"] is None and t3["health"]["mem_total_mb"] is None and t3["health"]["mem_pct"]
    assert snap["head"]["seen_in_insights"] is True and snap["probe_mode"] == "selector"
    assert snap["sea"]["functions"]["seen_in_insights"] is True  # by do:functions_namespace:<id> (B-033)
    assert snap["sea"]["load_balancer"]["seen_in_insights"] is True
    env.fleet.by_name("kraken-tentacle-2").unreachable = True
    await env.advance(11)
    snap = (await env.client.get("/api/fleet")).json()
    t2 = snap["tentacles"][1]
    assert t2["reachable"] is False and "unreachable" in t2["error"]


async def test_a_dead_tentacle_does_not_flood_the_api_trace(env):
    """Found by the wringer: every 10 s poll of an unreachable tentacle wrote two error records, 120 in ten
    minutes, so one dead box filled the 500-entry ring and every browser's drawer with tentacle noise."""
    env.fleet.by_name("kraken-tentacle-2").unreachable = True
    await env.advance(600)
    errors = [c for c in env.deps.trace.ring if c.target == "tentacle" and c.error]
    assert 1 <= len(errors) <= 11  # the first failure, then at most one a minute
    assert all(c.entity == "kraken-tentacle-2" and c.path == "/health" for c in errors)
    snap = (await env.client.get("/api/fleet")).json()
    assert snap["tentacles"][1]["reachable"] is False  # the snapshot still says so every cycle
    env.fleet.by_name("kraken-tentacle-2").unreachable = False
    await env.advance(20)
    assert (await env.client.get("/api/fleet")).json()["tentacles"][1]["reachable"] is True
    env.fleet.by_name("kraken-tentacle-2").unreachable = True
    await env.advance(15)
    assert len([c for c in env.deps.trace.ring if c.target == "tentacle" and c.error]) == len(errors) + 1  # anew


async def test_insights_not_configured():
    async with AppEnv(base_env(DIGITALOCEAN_TOKEN=None)) as e:
        r = await e.client.get("/api/insights/range", params=range_params())
        assert r.status_code == 503 and r.json()["error"]["code"] == "insights_not_configured"
        snap = (await e.client.get("/api/fleet")).json()
        assert snap["tentacles"][0]["seen_reason"] == "Insights not configured"


async def test_token_never_leaks_after_every_panel(env):
    reads = [("/api/insights/range", range_params(region="both")), ("/api/insights/query", range_params()),
             ("/api/insights/catalog", {"region": "both"}), ("/api/insights/alerts", None),
             ("/api/insights/labels", {"region": "tor1", "name": "__name__"}),
             ("/api/insights/logs", {"region": "tor1"}), ("/api/insights/logs/expected", None), ("/api/fleet", None),
             ("/api/traces/chains", None), ("/api/config", None), ("/healthz", None), ("/api/traces/own", None),
             ("/api/logs/own", None)]
    texts = []
    for path, params in reads:
        r = await env.client.get(path, params=params)
        assert r.status_code == 200, path
        texts.append(r.text)
    r = await env.client.post("/api/insights/promql", json={"region": "tor1", "query": "sum(do.droplets.load_1)"},
                              headers=env.captain())
    texts.append(r.text)
    r = await env.client.post("/api/insights/logs/search", headers=env.captain(), json={
        "region": "tor1", "filter": {"text_search": {"query": "GET"}}, "limit": 10})
    assert r.status_code == 200
    texts.append(r.text)
    trace = (await env.client.get("/api/trace", params={"limit": 500})).json()["calls"]
    assert len(trace) > 10
    for call in trace:
        texts.append((await env.client.get(f"/api/trace/{call['id']}")).text)
    texts += [env.stdout.getvalue(), json.dumps([c.full() for c in env.deps.trace.ring]),
              json.dumps(env.deps.telemetry.ring.traces())]
    for text in texts:
        assert TOKEN not in text and CAPTAIN not in text


def test_normalize_the_sample_matrix():
    from conftest import fixture, fleet_text

    from porthole.config import Fleet
    from porthole.panels import group_families, normalize
    fleet = Fleet.from_json(fleet_text())
    ours, theirs = normalize(fixture("prom_matrix.json"), "tor1", fleet)
    assert (ours["entity"], ours["display"], ours["slot"], ours["region"]) == \
        ("kraken-tentacle-1", "tentacle-1", 1, "tor1")
    assert ours["points"] == [[1760277120, 2.1], [1760277180, 2.3], [1760277300, 61.4]]  # gap and NaN stay missing
    assert theirs["slot"] is None and theirs["display"] == "someone-elses-droplet"
    # a name alone attributes a series only to members without a URN in the fleet description
    stray = {"resource_name": "kraken-tentacle-1", "resource_urn": "do:droplet:1"}
    fn = {"resource_name": "kraken", "resource_urn": "do:functions:kraken"}
    body = {"data": {"result": [{"metric": stray, "value": [1, "1"]}, {"metric": fn, "value": [1, "1"]}]}}
    unnamed = json.loads(fleet_text())
    for key in ("urn", "namespace_id"):
        del unnamed["sea"]["functions"][key]
    assert [(s["entity"], s["slot"]) for s in normalize(body, "tor1", Fleet.from_dict(unnamed))] == [
        ("kraken-tentacle-1", None), ("kraken", 8)]
    assert [s["slot"] for s in normalize(body, "tor1", fleet)] == [None, None]  # the namespace has a URN now
    real = {"resource_urn": "do:functions_namespace:fn-00000000-0000-0000-0000-000000000004"}  # B-033
    assert [(s["entity"], s["slot"]) for s in normalize({"data": {"result": [{"metric": real, "value": [1, "1"]}]}},
                                                        "tor1", fleet)] == [("kraken", 8)]
    families = group_families(fixture("label_values.json")["data"])
    assert list(families) == ["do.apps", "do.container_registry", "do.droplets", "do.load_balancers"]
    assert families["do.container_registry"][0]["dotted"] == "do.container_registry.storage_used_bytes"


def test_alert_views_from_the_samples():
    from conftest import fixture, fleet_text

    from porthole.config import Fleet
    from porthole.panels_alerts import channel_view, instance_view, rule_view
    fleet = Fleet.from_json(fleet_text())
    rule = rule_view(fixture("alert_rule.json")["alert_rule"], fleet.rule("round-trip"))
    assert (rule["operator"], rule["warning"], rule["critical"], rule["window"], rule["re_alert"], rule["status"]) == \
        (">=", 40, 60, "1m", "30m", "active")
    assert rule["channels"] == [{"id": "00000000-0000-0000-0000-0000000000c1", "notify_on": ["warning", "critical"]}]
    inst = instance_view(fixture("alert_instances.json")["alert_instances"][0], {rule["id"]: rule}, fleet)
    assert (inst["status"], inst["severity"], inst["entity"], inst["rule_name"]) == \
        ("resolved", "critical", "tentacle-1", "kraken churn")
    channels = fixture("channels.json")["notification_channels"]
    hook, email = (channel_view(c, "https://porthole.example.test") for c in channels)
    assert hook["type"] == "webhook" and hook["points_here"] and set(hook["statuses"]) == {"bearer_token_status",
                                                                                         "signature_status"}
    assert email["type"] == "email" and email["target"] == "alerts@example.com" and not email["points_here"]
