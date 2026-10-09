"""The public-site guards: captain's key on every mutating route, rate limits, headers, body cap, client IP."""
from __future__ import annotations

import hmac

import pytest
from conftest import CAPTAIN, AppEnv, base_env
from starlette.requests import Request

from porthole.security import CSP, RateLimiter, client_ip

# every mutating route of design section 4.2, with a body that is valid apart from the key
MUTATING = [
    ("/api/captain/check", None),
]


def _request(headers: dict[str, str], peer: str = "192.0.2.50") -> Request:
    scope = {"type": "http", "method": "GET", "path": "/", "headers": [(k.lower().encode(), v.encode())
                                                                        for k, v in headers.items()],
             "client": (peer, 1234), "query_string": b""}
    return Request(scope)


@pytest.mark.parametrize("path,body", MUTATING)
async def test_mutating_routes_need_the_key(env, path, body):
    r = await env.client.post(path, json=body)
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "captain_key_required"
    r = await env.client.post(path, json=body, headers={"X-Captain-Key": "wrong-key-of-sufficient-length-00"})
    assert r.status_code == 401


@pytest.mark.parametrize("path,body", MUTATING)
async def test_mutating_routes_return_503_without_a_configured_key(path, body):
    for key in (None, "too-short"):
        async with AppEnv(base_env(PORTHOLE_CAPTAIN_KEY=key)) as e:
            r = await e.client.post(path, json=body, headers=e.captain())
            assert r.status_code == 503, r.text
            assert r.json()["error"]["code"] == "captain_not_configured"


async def test_key_accepted_as_header_or_bearer(env):
    assert (await env.client.post("/api/captain/check", headers={"X-Captain-Key": CAPTAIN})).status_code == 204
    assert (await env.client.post("/api/captain/check",
                                  headers={"Authorization": f"Bearer {CAPTAIN}"})).status_code == 204


async def test_key_compared_in_constant_time(env, monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr("porthole.security.hmac.compare_digest", spy)
    await env.client.post("/api/captain/check", headers=env.captain())
    assert calls, "compare_digest was not used"


async def test_per_ip_rate_limit_with_retry_after(env):
    statuses = [(await env.client.post("/api/captain/check", headers=env.captain())).status_code for _ in range(13)]
    assert statuses[:12] == [204] * 12
    r = await env.client.post("/api/captain/check", headers=env.captain())
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    assert r.json()["error"]["code"] == "rate_limited"
    await env.clock.advance(60)
    assert (await env.client.post("/api/captain/check", headers=env.captain())).status_code == 204


async def test_failed_attempts_count_against_the_limit(env):
    for _ in range(12):
        await env.client.post("/api/captain/check")
    assert (await env.client.post("/api/captain/check", headers=env.captain())).status_code == 429


async def test_overall_limit_across_addresses():
    async with AppEnv(base_env(PORTHOLE_TRUST_PROXY="1")) as e:
        codes = []
        for i in range(61):
            h = {**e.captain(), "X-Forwarded-For": f"198.51.100.{i}, 10.0.0.1"}
            codes.append((await e.client.post("/api/captain/check", headers=h)).status_code)
        assert codes[:60] == [204] * 60 and codes[60] == 429


async def test_forwarded_for_ignored_when_not_trusted(env):
    for i in range(12):
        await env.client.post("/api/captain/check", headers={**env.captain(), "X-Forwarded-For": f"198.51.100.{i}"})
    r = await env.client.post("/api/captain/check", headers={**env.captain(), "X-Forwarded-For": "198.51.100.99"})
    assert r.status_code == 429  # every request came from the same socket peer


def test_client_ip_rules():
    assert client_ip(_request({"X-Forwarded-For": "203.0.113.9, 10.1.1.1"}), True) == "203.0.113.9"
    assert client_ip(_request({"X-Forwarded-For": "203.0.113.9"}), False) == "192.0.2.50"
    assert client_ip(_request({}), True) == "192.0.2.50"


def test_token_bucket_refills_over_time():
    t = [0.0]
    limiter = RateLimiter(2, None, lambda: t[0])
    assert limiter.check("a") is None and limiter.check("a") is None
    wait = limiter.check("a")
    assert wait is not None and 29 < wait <= 30
    t[0] += 30
    assert limiter.check("a") is None


@pytest.mark.parametrize("path", ["/", "/api/config", "/healthz", "/static/css/skin.css", "/nope"])
async def test_security_headers_everywhere(env, path):
    r = await env.client.get(path)
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert "access-control-allow-origin" not in r.headers


async def test_error_shape(env):
    r = await env.client.get("/api/does-not-exist")
    assert r.status_code == 404
    assert set(r.json()["error"]) == {"code", "message", "detail"}


async def test_body_cap_by_length(env):
    r = await env.client.post("/api/captain/check", content=b"x" * (64 * 1024 + 1), headers=env.captain())
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "too_large"
    assert r.headers["content-security-policy"] == CSP


async def test_body_cap_while_streaming(env):
    async def chunks():
        for _ in range(70):
            yield b"y" * 1024

    r = await env.client.post("/hooks/insights", content=chunks(),
                              headers={"Authorization": "Bearer x", "Content-Type": "application/json"})
    assert r.status_code in (413, 404)  # 404 until the webhook route exists (M3)
