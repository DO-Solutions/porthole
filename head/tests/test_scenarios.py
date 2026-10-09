"""Scenarios: catalog caps, start and stop on the right tentacle with the bearer, tentacle errors, head-side runs."""
from __future__ import annotations

import json
from datetime import timedelta

from conftest import TENTACLE_KEY, AppEnv, base_env, fleet_text

from fake_tentacles import FakeRun


async def start(env, target, scenario, params=None):
    return await env.client.post("/api/scenarios/start", headers=env.captain(),
                                 json={"target": target, "scenario": scenario, "params": params or {}})


async def test_catalog_lists_caps(env):
    body = (await env.client.get("/api/scenarios/catalog")).json()
    by = {s["name"]: s for s in body["scenarios"]}
    assert set(by) == {"cpu", "memory", "disk", "network", "logs", "chain", "pg", "fn", "lb"}
    assert by["cpu"]["params"][1] == {"name": "workers", "min": 1, "max": 2, "default": 1, "type": "int"}
    assert by["fn"]["runs_on"] == "head" and by["network"]["requires"] == "peer"
    assert body["targets"][-1] == "head"
    caps = (await env.client.get("/api/config")).json()["caps"]["scenarios"]
    assert caps["memory"]["mb"] == [64, 700]


async def test_start_clamps_and_proxies_with_the_bearer(env):
    r = await start(env, "tentacle-1", "cpu", {"seconds": 5000, "workers": 9})
    assert r.status_code == 202, r.text
    run = r.json()
    assert run["target"] == "kraken-tentacle-1" and run["params"] == {"seconds": 600, "workers": 2}
    assert run["clamped"] == {"seconds": {"asked": 5000, "used": 600}, "workers": {"asked": 9, "used": 2}}
    fake = env.fleet.by_name("kraken-tentacle-1")
    assert ("POST", "/scenario/cpu") in fake.requests and fake.auth_seen[-1] == f"Bearer {TENTACLE_KEY}"
    assert env.fleet.by_name("kraken-tentacle-2").runs == {}
    call = next(c for c in reversed(env.deps.trace.ring) if c.path == "/scenario/cpu")
    assert call.target == "tentacle" and call.entity == "kraken-tentacle-1"
    assert TENTACLE_KEY not in json.dumps(call.full())


async def test_parameter_and_target_validation(env):
    cases = [("tentacle-1", "cpu", {"speed": 3}, "unknown_parameter"),
             ("tentacle-1", "cpu", {"seconds": "lots"}, "bad_parameter"),
             ("tentacle-1", "fly", {}, "unknown_scenario"), ("tentacle-9", "cpu", {}, "unknown_target"),
             ("tentacle-3", "network", {}, "no_peer"), ("tentacle-1", "fn", {}, "unknown_target"),
             ("head", "cpu", {}, "unknown_target")]
    for target, scenario, params, code in cases:
        r = await start(env, target, scenario, params)
        assert r.status_code == 400 and r.json()["error"]["code"] == code, (scenario, r.text)


async def test_one_run_per_name_per_target(env):
    first = (await start(env, "tentacle-1", "cpu")).json()
    r = await start(env, "tentacle-1", "cpu")
    assert r.status_code == 409 and r.json()["error"]["detail"] == {"run_id": first["id"]}
    assert (await start(env, "tentacle-2", "cpu")).status_code == 202
    assert (await start(env, "tentacle-1", "logs")).status_code == 202


async def test_tentacle_errors_surface_with_their_status(env):
    t1 = env.fleet.by_name("kraken-tentacle-1")
    t1.pg = False
    r = await start(env, "tentacle-1", "pg")
    assert r.status_code == 400 and "PG_DSN is not configured" in r.json()["error"]["message"]
    t1.refuse_memory = True
    r = await start(env, "tentacle-1", "memory", {"mb": 500})
    assert r.status_code == 409 and "less than 100 MB available" in r.json()["error"]["message"]
    for i in range(8):
        t1.runs[f"x-{i}"] = FakeRun(f"x-{i}", f"other{i}", {}, env.clock.now(), 3600)
    r = await start(env, "tentacle-1", "disk")
    assert r.status_code == 429 and r.json()["error"]["code"] == "tentacle_busy"
    env.fleet.by_name("kraken-tentacle-2").unreachable = True
    r = await start(env, "tentacle-2", "cpu")
    assert r.status_code == 502 and "unreachable" in r.json()["error"]["message"]


async def test_wrong_tentacle_key_is_a_server_problem():
    async with AppEnv(tentacle_key="the-tentacles-expect-another-key") as e:
        r = await start(e, "tentacle-1", "cpu")
        assert r.status_code == 502 and r.json()["error"]["code"] == "tentacle_auth"


async def test_stop(env):
    run = (await start(env, "tentacle-2", "logs", {"seconds": 300})).json()
    r = await env.client.post("/api/scenarios/stop", headers=env.captain(),
                              json={"target": "kraken-tentacle-2", "run_id": run["id"]})
    assert r.status_code == 200 and r.json()["status"] == "stopped" and r.json()["target"] == "kraken-tentacle-2"
    r = await env.client.post("/api/scenarios/stop", headers=env.captain(),
                              json={"target": "tentacle-2", "run_id": "nope"})
    assert r.status_code == 404


async def test_head_side_fn_run_with_stats(env):
    r = await start(env, "head", "fn", {"seconds": 10, "rps": 5})
    assert r.status_code == 202 and r.json()["target"] == "head" and r.json()["status"] == "running"
    run_id = r.json()["id"]
    run = env.deps.scenarios.head_runs[run_id]
    assert await env.run_until(lambda: run.status != "running", 60)
    assert run.status == "finished"
    res = run.result
    assert res["requests"] == 50 and res["ok"] == 50 and res["errors"] == 0
    assert res["p50_ms"] is not None and res["p95_ms"] >= res["p50_ms"]
    assert env.fleet.requests("fn", 0, 2e9) == 50
    sampled = [c for c in env.deps.trace.ring if c.target == "function"]
    assert 1 <= len(sampled) <= 2  # one request in ten seconds reaches the API trace
    merged = (await env.client.get("/api/fleet/scenarios")).json()
    assert any(x["id"] == run_id and x["target"] == "head" for x in merged["finished"])
    events = [e.data for e in env.deps.hub.ring if e.event == "scenario" and e.data["run"]["id"] == run_id]
    assert [e["event"] for e in events] == ["started", "finished"]


async def test_head_side_lb_run_counts_errors_and_stops(env):
    for t in env.fleet.tentacles.values():
        t.unreachable = True  # the LB answers 503 with no healthy backend
    run_id = (await start(env, "head", "lb", {"seconds": 120, "rps": 20})).json()["id"]
    run = env.deps.scenarios.head_runs[run_id]
    await env.advance(5)
    r = await env.client.post("/api/scenarios/stop", headers=env.captain(), json={"target": "head", "run_id": run_id})
    assert r.status_code == 200 and r.json()["status"] == "stopped"
    assert run.result["errors"] == run.result["requests"] > 0 and run.result["ok"] == 0
    failures = [c for c in env.deps.trace.ring if c.target == "lb" and c.status == 503]
    assert len(failures) <= 6  # at most one failure a second is recorded
    assert (await start(env, "head", "lb")).status_code == 202


async def test_head_runs_need_the_resources():
    fleet = json.loads(fleet_text())
    del fleet["sea"]["functions"], fleet["sea"]["load_balancer"]
    async with AppEnv(base_env(PORTHOLE_FLEET_JSON=json.dumps(fleet))) as e:
        assert (await start(e, "head", "fn")).json()["error"]["code"] == "no_function"
        assert (await start(e, "head", "lb")).json()["error"]["code"] == "no_load_balancer"


async def test_runs_finish_on_the_clock_and_announce_it(env):
    run_id = (await start(env, "tentacle-1", "cpu", {"seconds": 30})).json()["id"]
    await env.advance(45)
    events = [e.data for e in env.deps.hub.ring if e.event == "scenario" and e.data["run"]["id"] == run_id]
    assert [e["event"] for e in events] == ["started", "finished"]
    finished = (await env.client.get("/api/fleet/scenarios")).json()["finished"]
    assert any(r["id"] == run_id and r["target"] == "kraken-tentacle-1" for r in finished)
    assert env.clock.now() - env.clock.start_wall >= timedelta(seconds=45)


async def test_the_merged_listing_says_how_each_run_ended_newest_first(env):
    """The Stir page's finished table: tentacle and head runs together, the last to end first, each with its
    started and ended times and the reason it ended (completed, stopped, error)."""
    long = (await start(env, "tentacle-2", "cpu", {"seconds": 300})).json()["id"]
    short = (await start(env, "tentacle-1", "cpu", {"seconds": 30})).json()["id"]
    await env.advance(45)
    r = await env.client.post("/api/scenarios/stop", headers=env.captain(), json={"target": "tentacle-2",
                                                                                  "run_id": long})
    assert r.json()["reason"] == "stopped"
    head = (await start(env, "head", "fn", {"seconds": 10, "rps": 1})).json()["id"]
    await env.advance(15)
    finished = (await env.client.get("/api/fleet/scenarios")).json()["finished"]
    assert [(r["id"], r["reason"]) for r in finished[:3]] == [(head, "completed"), (long, "stopped"),
                                                              (short, "completed")]
    assert all(r["started_at"] and r["ended_at"] and r["elapsed_s"] >= 0 for r in finished[:3])
