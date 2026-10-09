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
    assert run.step("webhook-delivered").text == "still waiting after 600 s (waiting for a delivery to /hooks/insights)"
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


def memory_runs(env, name):
    return [r for r in env.fleet.by_name(name).runs.values() if r.name == "memory"]


async def test_ballast_sizes_the_memory_ask_to_what_the_tentacle_has(env):
    """B-032, run v-09c93d: a 1 GB tentacle has about 590 MB available and refuses anything that leaves it under
    100 MB, so the old fixed 500 MB ask could never be accepted. 590 - 150 = 440."""
    run = await sail(env, "ballast")
    assert await env.run_until(lambda: run.status != "sailing")
    assert run.status == "done", run.error
    for name in ("kraken-tentacle-1", "kraken-tentacle-2"):
        assert [r.params["mb"] for r in memory_runs(env, name)] == [440]
        assert env.fleet.by_name(name).requests.count(("POST", "/scenario/memory")) == 1  # sized, never refused
    assert run.step("start-memory").text == ("tentacle-1: 440 MB (asked 500, 590 MB available), "
                                             "tentacle-2: 440 MB (asked 500, 590 MB available)")
    assert run.summary["memory_held_mb"] == {"tentacle-1": 440, "tentacle-2": 440}
    assert run.step("summary").text.startswith("memory held {'tentacle-1': 440, 'tentacle-2': 440} MB, peaks ")


async def test_ballast_falls_back_to_the_409_of_an_older_tentacle(env):
    """A tentacle from before mem_avail_mb: ask the full 500, read MemAvailable out of the 409, ask N - 150 once."""
    t2 = env.fleet.by_name("kraken-tentacle-2")
    t2.reports_mem_avail, t2.mem_base_mb = False, t2.mem_base_mb + 190  # 400 MB available
    await env.advance(11)  # the fleet view polls once more
    run = await sail(env, "ballast")
    assert await env.run_until(lambda: run.status != "sailing")
    assert run.status == "done", run.error
    assert t2.requests.count(("POST", "/scenario/memory")) == 2
    assert [r.params["mb"] for r in memory_runs(env, "kraken-tentacle-2")] == [250]
    assert run.step("start-memory").text.endswith("tentacle-2: 250 MB (asked 500, 400 MB available)")
    assert run.summary["memory_held_mb"] == {"tentacle-1": 440, "tentacle-2": 250}


@pytest.mark.parametrize("avail,ask", [(590, 440), (900, 500), (400, 250), (200, 64), (100, 64)])
def test_memory_ask_leaves_150_mb_and_never_drops_below_the_minimum(avail, ask):
    from porthole.voyages_metrics import memory_ask
    assert memory_ask(500, avail) == ask


async def test_a_failed_step_keeps_its_note_next_to_the_error(env):
    """v-09c93d kept only "no tentacle accepted memory"; the refusals the step had noted were overwritten."""
    for name in ("kraken-tentacle-1", "kraken-tentacle-2"):
        env.fleet.by_name(name).refuse_memory = True
    run = await sail(env, "ballast")
    assert await env.run_until(lambda: run.status != "sailing", 300)
    rec = run.step("start-memory")
    assert run.status == "failed" and rec.status == "failed"
    assert rec.text.startswith("no tentacle accepted memory (started nothing; refused: tentacle-1 memory: ")
    assert rec.text.count("less than 100 MB available (MemAvailable 590 MB)") == 2 and rec.text.endswith(")")
    assert run.error == f"start-memory: {rec.text}"
    assert (await env.client.get(f"/api/voyages/{run.id}")).json()["steps"][0]["text"] == rec.text
