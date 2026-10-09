"""Voyages: each one walks its planned steps in order, the round trip records every latency, abort and timeouts
clean up, and only one voyage sails at a time."""
from __future__ import annotations

import json

import pytest
from conftest import AppEnv, base_env, fleet_text

from porthole.voyages_catalog import CATALOG


async def sail(env, name, params=None):
    r = await env.client.post("/api/voyages/start", json={"voyage": name, "params": params or {}},
                              headers=env.captain())
    assert r.status_code == 202, r.text
    return env.deps.voyages.runs[r.json()["run_id"]]


async def test_catalog_route(env):
    body = (await env.client.get("/api/voyages")).json()
    assert [v["name"] for v in body["voyages"]] == ["churn", "alert-round-trip", "ballast", "two-seas", "log-storm",
                                                    "chain", "deep-water"]
    rt = next(v for v in body["voyages"] if v["name"] == "alert-round-trip")
    assert rt["feature"] == "A1, A2"
    assert rt["steps"][9] == {"name": "resolve-delivery", "title": "the resolve webhook arrives", "timeout_s": 600,
                              "optional": True}
    assert body["runs"] == [] and body["active_run_id"] is None


@pytest.mark.parametrize("name", list(CATALOG))
async def test_each_voyage_walks_its_planned_steps(name):
    async with AppEnv() as env:
        run = await sail(env, name)
        planned = [s.name for s in CATALOG[name].steps]
        assert [s.name for s in run.steps] == planned
        assert await env.run_until(lambda: run.status != "sailing", 3 * 3600)
        assert run.status == "done", run.error
        assert all(s.status in ("done", "skipped") for s in run.steps)
        stamps = [s.started_at for s in run.steps if s.started_at]
        assert stamps == sorted(stamps)
        events = [e.data for e in env.deps.hub.ring if e.event == "voyage" and e.data["run_id"] == run.id]
        stepped = list(dict.fromkeys(e["step"] for e in events if e["step"] and e["status"] == "running"))
        assert stepped == [s.name for s in run.steps if s.started_at]
        view = (await env.client.get(f"/api/voyages/{run.id}")).json()
        assert view["status"] == "done" and len(view["steps"]) == len(planned)


async def test_alert_round_trip_records_every_latency(env):
    run = await sail(env, "alert-round-trip")
    assert await env.run_until(lambda: run.status != "sailing")
    assert run.status == "done"
    for key in ("burn_to_cross_s", "cross_to_active_s", "active_to_delivery_s", "stop_to_resolved_s",
                "stop_to_fall_s", "resolved_to_delivery_s"):
        assert isinstance(run.summary[key], float) and run.summary[key] >= 0, key
    assert run.step("read-rule").text == "kraken churn: >= 60 critical, window 1m, re-alert 30m"
    first = run.step("webhook-delivered").artifacts[0]["ref"]
    assert env.deps.hooks.get(first)["matched_voyage"] == run.id
    cpu = env.fleet.by_name("kraken-tentacle-1").runs[run.scenarios_started[0]["run_id"]]
    assert cpu.status == "stopped" and cpu.params["seconds"] == 300  # 3 windows of 1 minute plus 120 s
    instances = env.insights.store.list_instances({"rule_id": "00000000-0000-0000-0000-0000000000a1"})
    assert instances["alert_instances"][0]["status"] == "ALERT_INSTANCE_STATUS_RESOLVED"


async def test_optional_step_timing_out_does_not_fail(env):
    async def drop_resolves():
        env.insights.tick()
        for d in [d for d in env.deliveries if d["kind"] == "ALERT_TRIGGERED"]:
            await env.client.post("/hooks/insights", content=d["body"], headers=d["headers"])
        env.deliveries.clear()

    env.pump = drop_resolves
    run = await sail(env, "alert-round-trip")
    assert await env.run_until(lambda: run.status != "sailing")
    assert run.status == "done" and run.step("resolve-delivery").status == "timed_out"
    assert run.step("summary").status == "done"


async def test_step_timeout_fails_the_voyage_and_cleans_up(env):
    async def no_webhooks():
        env.insights.tick()
        env.deliveries.clear()

    env.pump = no_webhooks
    run = await sail(env, "alert-round-trip")
    assert await env.run_until(lambda: run.status != "sailing")
    assert run.status == "failed" and "webhook-delivered timed out" in run.error
    assert run.step("webhook-delivered").status == "timed_out"
    assert [s.status for s in run.steps[6:]] == ["skipped"] * 5
    cpu = env.fleet.by_name("kraken-tentacle-1").runs[run.scenarios_started[0]["run_id"]]
    assert cpu.status in ("stopped", "finished")  # cleanup stops it if the burn is still going


async def test_abort_stops_what_the_voyage_started(env):
    run = await sail(env, "churn")
    assert await env.run_until(lambda: run.step("metric-appears").status == "running", 300)
    cpu = env.fleet.by_name("kraken-tentacle-1").runs[run.scenarios_started[0]["run_id"]]
    assert cpu.status == "running"
    r = await env.client.post(f"/api/voyages/{run.id}/abort", headers=env.captain())
    assert r.status_code == 200 and r.json()["status"] == "aborted"
    assert cpu.status == "stopped"
    assert run.step("metric-appears").status == "skipped" and run.step("summary").status == "skipped"
    assert env.deps.voyages.active is None


async def test_a_second_abort_during_cleanup_does_not_lock_the_engine(env, monkeypatch):
    """Found by the wringer: a second abort while the first one's cleanup was still stopping the burn cancelled
    the cleanup itself, so the run never ended, the engine stayed on 'one at a time' and the burn kept going."""
    import asyncio

    from porthole.tentacles import TentacleClient
    run = await sail(env, "churn")
    assert await env.run_until(lambda: run.step("metric-appears").status == "running", 300)
    real_stop = TentacleClient.stop

    async def slow_stop(self, run_id):  # a real tentacle joins the scenario thread for up to 10 s
        await env.clock.sleep(5)
        return await real_stop(self, run_id)

    monkeypatch.setattr(TentacleClient, "stop", slow_stop)

    async def spin_until(done) -> None:
        for _ in range(2000):
            if done():
                return
            await asyncio.sleep(0)
        raise AssertionError("the abort request did not get through")

    first = asyncio.create_task(env.client.post(f"/api/voyages/{run.id}/abort", headers=env.captain()))
    await spin_until(lambda: run.status == "aborted")
    assert not run.task.done()  # cleanup is waiting on the slow stop
    second = asyncio.create_task(env.client.post(f"/api/voyages/{run.id}/abort", headers=env.captain()))
    await env.settle()
    await env.advance(10)
    assert (await first).status_code == 200 and (await second).status_code == 200
    assert run.task.done() and not run.task.cancelled() and run.ended_at is not None
    assert env.deps.voyages.active is None
    cpu = env.fleet.by_name("kraken-tentacle-1").runs[run.scenarios_started[0]["run_id"]]
    assert cpu.status == "stopped"
    assert (await env.client.post("/api/voyages/start", json={"voyage": "chain"},
                                  headers=env.captain())).status_code == 202


async def test_only_one_voyage_sails(env):
    run = await sail(env, "chain")
    r = await env.client.post("/api/voyages/start", json={"voyage": "churn"}, headers=env.captain())
    assert r.status_code == 409 and r.json()["error"]["detail"] == {"active_run_id": run.id}
    assert await env.run_until(lambda: run.status != "sailing")
    assert (await env.client.post("/api/voyages/start", json={"voyage": "log-storm"},
                                  headers=env.captain())).status_code == 202


async def test_voyage_validation(env):
    for body, code in (({"voyage": "atlantis"}, "unknown_voyage"),
                       ({"voyage": "churn", "params": {"target": "tentacle-9"}}, "unknown_target"),
                       ({"voyage": "churn", "params": {"speed": 1}}, "unknown_parameter")):
        r = await env.client.post("/api/voyages/start", json=body, headers=env.captain())
        assert r.status_code == 400 and r.json()["error"]["code"] == code
    assert (await env.client.get("/api/voyages/v-missing")).status_code == 404


async def test_round_trip_refuses_a_paused_rule(env):
    env.insights.store.rules["00000000-0000-0000-0000-0000000000a1"]["status"] = "ALERT_RULE_STATUS_PAUSED"
    run = await sail(env, "alert-round-trip")
    assert await env.run_until(lambda: run.status != "sailing", 300)
    assert run.status == "failed" and run.step("read-rule").status == "failed"
    assert "resume it" in run.step("read-rule").text and run.scenarios_started == []


async def test_log_storm_says_collected_when_droplet_logs_arrive():
    async with AppEnv(droplet_logs=True) as env:
        run = await sail(env, "log-storm", {"target": "tentacle-2"})
        assert await env.run_until(lambda: run.status != "sailing")
        assert run.summary["verdict"] == "collected" and run.summary["insights_count"] == 3000
        assert len(run.step("search").data["checks"]) == 1  # stopped early once all lines were there


async def test_log_storm_gives_the_a6b_verdict(env):
    run = await sail(env, "log-storm")
    assert await env.run_until(lambda: run.status != "sailing")
    assert run.summary["verdict"] == "not collected (A6b)"
    assert run.step("verdict").text.startswith("Not yet collected by DigitalOcean: tentacle-1 reports 3,000 lines")


async def test_two_seas_needs_two_regions():
    fleet = json.loads(fleet_text())
    fleet["tentacles"] = fleet["tentacles"][:2]
    fleet["regions"] = ["tor1"]
    fleet["watcher"]["rules"] = [r for r in fleet["watcher"]["rules"] if r["target"] != "kraken-tentacle-3"]
    async with AppEnv(base_env(PORTHOLE_FLEET_JSON=json.dumps(fleet))) as env:
        run = await sail(env, "two-seas")
        assert await env.run_until(lambda: run.status != "sailing", 300)
        assert run.status == "failed" and "two regions" in run.error


async def test_deep_water_skips_what_the_fleet_lacks():
    fleet = json.loads(fleet_text())
    del fleet["sea"]["database"]
    async with AppEnv(base_env(PORTHOLE_FLEET_JSON=json.dumps(fleet))) as env:
        run = await sail(env, "deep-water")
        assert await env.run_until(lambda: run.status != "sailing")
        assert run.status == "done" and run.step("start-pg").status == "skipped"
        assert set(run.summary["moved_after_s"]) == {"functions", "load_balancer"}


async def test_chain_points_at_the_heads_own_span(env):
    run = await sail(env, "chain")
    assert await env.run_until(lambda: run.status != "sailing")
    assert run.trace_id and run.summary["head_trace_id"] == run.trace_id
    assert run.step("own-span").data["span"] is not None
    assert run.step("link").artifacts[0]["verified"] is True
