"""GET /healthz: shape, version, and degraded mode without a token or fleet."""
from __future__ import annotations

from conftest import AppEnv, base_env


async def test_healthz_shape(env):
    r = await env.client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "version", "uptime_s", "insights", "fleet", "brain", "problems"}
    assert body["status"] == "ok"
    assert body["insights"] == "configured"
    assert body["fleet"] == {"tentacles": 3, "regions": ["tor1", "syd1"]}
    assert body["brain"] == "deckhand"
    assert body["problems"] == []


async def test_healthz_reports_the_baked_version():
    async with AppEnv(base_env(PORTHOLE_VERSION="3f2a9c0")) as e:
        assert (await e.client.get("/healthz")).json()["version"] == "3f2a9c0"


async def test_uptime_follows_the_clock(env):
    await env.clock.advance(42)
    assert (await env.client.get("/healthz")).json()["uptime_s"] >= 42


async def test_degraded_mode_without_token_still_answers():
    async with AppEnv(base_env(DIGITALOCEAN_TOKEN=None, PORTHOLE_FLEET_JSON=None)) as e:
        r = await e.client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["insights"] == "missing"
        assert body["fleet"]["tentacles"] == 0
        assert any("DIGITALOCEAN_TOKEN is not set" in p for p in body["problems"])
        assert any("PORTHOLE_FLEET_JSON" in p for p in body["problems"])
        # the rest of the site works and explains itself
        assert (await e.client.get("/api/config")).status_code == 200
        lines = e.log_lines()
        assert any(line["body"].startswith("degraded: DIGITALOCEAN_TOKEN") for line in lines)


async def test_healthz_is_not_traced(env):
    await env.client.get("/healthz")
    names = [t["root"] for t in env.deps.telemetry.ring.traces()]
    assert not any("healthz" in n for n in names)
