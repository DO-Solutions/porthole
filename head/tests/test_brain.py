"""The Brain: the deckhand's event sequences for the three suggested questions, approvals, and the protocol."""
from __future__ import annotations

import inspect

from conftest import AppEnv, base_env

from porthole.brain.adapter import BrainAdapter
from porthole.brain.deckhand import Deckhand

ASK = ["status", "thinking", "tool_call", "tool_result", "tool_call", "tool_result", "tool_call", "tool_result",
       "message"]


async def ask(env, question: str, wait: bool = True):
    r = await env.client.post("/api/brain/sessions", json={"question": question})
    assert r.status_code == 202, r.text
    s = env.deps.brain.sessions[r.json()["id"]]
    if wait:
        await env.run_until(lambda: s.state in ("waiting_approval", "done", "failed"), 120)
    return s


def types(s) -> list[str]:
    return [e["type"] for e in s.events]


def approval_id(s) -> str:
    return next(e["data"]["approval_id"] for e in s.events if e["type"] == "approval_request")


async def decide(env, s, decision: str):
    r = await env.client.post(f"/api/brain/sessions/{s.id}/approvals/{approval_id(s)}", json={"decision": decision},
                              headers=env.captain())
    assert r.status_code == 200, r.text
    await env.run_until(lambda: s.state in ("done", "failed"), 120)


async def burning(env, target="tentacle-2"):
    r = await env.client.post("/api/scenarios/start", headers=env.captain(),
                              json={"target": target, "scenario": "cpu", "params": {"seconds": 300}})
    run = env.fleet.by_name(f"kraken-{target}").runs[r.json()["id"]]
    await env.advance(150)
    return run


async def test_info(env):
    info = (await env.client.get("/api/brain/info")).json()
    assert info["backend"] == "deckhand" and info["label"] == "scripted deckhand (phase 1)"
    assert [t["name"] for t in info["tools"] if t["needs_approval"]] == ["scenario_start", "scenario_stop",
                                                                         "voyage_start"]
    assert len(info["tools"]) == 8 and len(info["suggested"]) == 3


async def test_slow_tentacle_proposes_a_stop_and_waits(env):
    run = await burning(env)
    s = await ask(env, "why is tentacle-2 slow?")
    assert s.state == "waiting_approval" and types(s) == ASK + ["approval_request"]
    request = s.events[-1]["data"]
    assert request["tool"] == "scenario_stop" and request["args"] == {"target": "kraken-tentacle-2", "run_id": run.id}
    assert "above 90 % since" in s.events[8]["data"]["text"] and run.id in s.events[8]["data"]["text"]
    await env.advance(60)  # still blocked: nothing happens without the captain
    calls = [e["data"]["tool"] for e in s.events if e["type"] == "tool_call"]
    assert run.status == "running" and "scenario_stop" not in calls
    await decide(env, s, "approve")
    assert types(s)[10:] == ["approval_resolved", "tool_call", "tool_result", "message", "done"]
    assert s.events[10]["data"]["decision"] == "approve" and s.events[10]["data"]["actor"] == "captain"
    assert run.status == "stopped"
    assert all(e["data"]["trace_ids"] for e in s.events if e["type"] == "tool_result")


async def test_deny_skips_the_action(env):
    run = await burning(env)
    s = await ask(env, "why is tentacle-2 slow?")
    await decide(env, s, "deny")
    assert types(s)[10:] == ["approval_resolved", "message", "done"]
    assert run.status == "running" and "Left" in s.events[11]["data"]["text"]


async def test_unanswered_approval_expires(env):
    run = await burning(env)
    s = await ask(env, "why is tentacle-2 slow?")
    await env.advance(301)
    await env.run_until(lambda: s.state == "done", 60)
    assert s.events[10]["data"]["decision"] == "expired" and run.status != "stopped"  # it ran out on its own
    r = await env.client.post(f"/api/brain/sessions/{s.id}/approvals/{approval_id(s)}", json={"decision": "approve"},
                              headers=env.captain())
    assert r.status_code == 409


async def test_quiet_tentacle_needs_no_action(env):
    s = await ask(env, "why is tentacle-1 slow?")
    assert s.state == "done" and types(s) == ASK + ["done"]
    assert "Nothing is running on it" in s.events[8]["data"]["text"]


async def test_alerting_question(env):
    s = await ask(env, "is anything alerting right now?")
    assert types(s) == ["status", "tool_call", "tool_result", "message", "approval_request"]
    assert s.events[4]["data"]["tool"] == "voyage_start"
    await decide(env, s, "approve")
    assert types(s)[5:] == ["approval_resolved", "tool_call", "tool_result", "message", "done"]
    assert env.deps.voyages.active is not None and env.deps.voyages.active.voyage == "alert-round-trip"
    await env.client.post(f"/api/voyages/{env.deps.voyages.active.id}/abort", headers=env.captain())


async def test_alerting_question_with_an_active_alert(env):
    env.insights.store.fire("00000000-0000-0000-0000-0000000000a1", "do:droplet:600000001", 88.0)
    env.deps.cache.invalidate("alerts")
    s = await ask(env, "is anything alerting right now?")
    assert types(s) == ["status", "tool_call", "tool_result", "message", "done"]
    assert "kraken churn on tentacle-1: critical, value 88.0" in s.events[3]["data"]["text"]


async def test_log_storm_question_without_a_storm(env):
    s = await ask(env, "did the log storm reach Insights?")
    assert types(s) == ["status", "tool_call", "tool_result", "message", "approval_request"]
    assert s.events[4]["data"]["args"]["scenario"] == "logs"
    await decide(env, s, "approve")
    assert types(s)[-2:] == ["message", "done"]
    assert any(r.name == "logs" for r in env.fleet.by_name("kraken-tentacle-1").runs.values())


async def test_log_storm_question_after_a_storm(env):
    env.fleet.by_name("kraken-tentacle-3").start("logs", {"seconds": "60", "rate": "50", "error_pct": "10"},
                                                 env.clock.now())
    await env.advance(120)
    s = await ask(env, "did the log storm reach Insights?")
    assert types(s) == ["status", "tool_call", "tool_result", "tool_call", "tool_result", "message", "done"]
    assert s.events[3]["data"]["args"] == {"region": "syd1", "service": "kraken-tentacle-3", "range": "1h"}
    assert s.events[5]["data"]["text"].startswith("Not yet collected by DigitalOcean: tentacle-3 reports 3,000 lines")


async def test_unknown_questions_get_the_fleet(env):
    s = await ask(env, "what lives under the sea?")
    assert types(s) == ["status", "tool_call", "tool_result", "message", "done"]
    assert s.events[3]["data"]["text"].startswith("I only know these questions today")


async def test_approval_route_needs_the_key():
    async with AppEnv() as env:
        await burning(env)
        s = await ask(env, "why is tentacle-2 slow?")
        url = f"/api/brain/sessions/{s.id}/approvals/{approval_id(s)}"
        assert (await env.client.post(url, json={"decision": "approve"})).status_code == 401
        assert (await env.client.post(url, json={"decision": "maybe"}, headers=env.captain())).status_code == 400
        assert (await env.client.post(f"/api/brain/sessions/{s.id}/approvals/a-nope", json={"decision": "approve"},
                                      headers=env.captain())).status_code == 409
    async with AppEnv(base_env(PORTHOLE_CAPTAIN_KEY=None)) as env:
        r = await env.client.post("/api/brain/sessions/s-x/approvals/a-x", json={"decision": "approve"})
        assert r.status_code == 503


async def test_events_stream_and_snapshot(env):
    s = await ask(env, "what lives under the sea?")
    r = await env.client.get(f"/api/brain/sessions/{s.id}/events")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "event: status" in r.text and "event: done" in r.text and "id: 5" in r.text
    r = await env.client.get(f"/api/brain/sessions/{s.id}/events", params={"after": "3"})
    assert "event: status" not in r.text and "event: message" in r.text
    assert (await env.client.get(f"/api/brain/sessions/{s.id}/events", params={"after": "5"})).status_code == 204
    snap = (await env.client.get(f"/api/brain/sessions/{s.id}")).json()
    assert snap["state"] == "done" and snap["backend"] == "deckhand" and len(snap["events"]) == 5
    states = [e.data["state"] for e in env.deps.hub.ring if e.event == "brain" and e.data["session_id"] == s.id]
    assert states == ["running", "done"]


async def test_questions_are_rate_limited_and_validated(env):
    for _ in range(3):
        await ask(env, "what lives under the sea?", wait=False)
    r = await env.client.post("/api/brain/sessions", json={"question": "and now?"})
    assert r.status_code == 429 and "Retry-After" in r.headers
    await env.advance(60)
    for q in ("", "x" * 301):
        assert (await env.client.post("/api/brain/sessions", json={"question": q})).status_code == 400


async def test_backend_switch():
    async with AppEnv(base_env(PORTHOLE_BRAIN="off")) as env:
        assert (await env.client.get("/api/brain/info")).json()["backend"] == "off"
        assert (await env.client.post("/api/brain/sessions", json={"question": "hi"})).status_code == 503
    async with AppEnv(base_env(PORTHOLE_BRAIN="harness-runtime", PORTHOLE_BRAIN_SESSION="kraken-brain")) as env:
        info = (await env.client.get("/api/brain/info")).json()
        assert info["label"] == "Harness Runtime session kraken-brain"
        assert (await env.client.post("/api/brain/sessions", json={"question": "hi"})).status_code == 401
        r = await env.client.post("/api/brain/sessions", json={"question": "hi"}, headers=env.captain())
        assert r.status_code == 503 and r.json()["error"]["code"] == "brain_not_built"


def test_deckhand_follows_the_protocol():
    members = {n for n in dir(BrainAdapter) if not n.startswith("_")}
    assert members == {"start", "events", "approve", "cancel", "get"}
    for name in ("start", "approve", "cancel", "get"):
        assert inspect.iscoroutinefunction(getattr(Deckhand, name)), name
        assert list(inspect.signature(getattr(Deckhand, name)).parameters) == \
            list(inspect.signature(getattr(BrainAdapter, name)).parameters), name
    assert inspect.isasyncgenfunction(Deckhand.events) and Deckhand.name == "deckhand"


async def test_twenty_sessions_are_kept(env):
    ctx = {"fleet": {}, "region_default": "tor1", "tools": [], "actor": "visitor"}
    ids = [(await env.deps.brain.start("what lives under the sea?", ctx))["id"] for _ in range(21)]
    await env.settle()
    assert len(env.deps.brain.sessions) == 20 and ids[0] not in env.deps.brain.sessions
