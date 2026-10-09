"""The webhook receiver: auth, every signature scheme of Appendix B, header detection, redaction, rings, caps."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from conftest import HOOK_BEARER, HOOK_SECRET, AppEnv, base_env

from porthole.hooks import fields_found, redact_headers, signature_attempts

BODY = json.dumps({"type": "ALERT_TRIGGERED", "alert_instance": {"rule_id": "r-1", "status": "ACTIVE",
                                                                 "resource_urn": "do:droplet:600000001"}}).encode()
AUTH = {"Authorization": f"Bearer {HOOK_BEARER}", "Content-Type": "application/json"}


def mac(key: bytes, msg: bytes, algo=hashlib.sha256) -> bytes:
    return hmac.new(key, msg, algo).digest()


def signed(scheme: str, body: bytes = BODY, secret: str = HOOK_SECRET) -> dict:
    key = secret.encode()
    if scheme == "hmac-sha256-hex":
        return {"X-Signature": mac(key, body).hex()}
    if scheme == "hmac-sha256-base64":
        return {"X-Insights-Signature": base64.b64encode(mac(key, body)).decode()}
    if scheme == "github-sha256":
        return {"X-Hub-Signature-256": "sha256=" + mac(key, body).hex()}
    if scheme == "github-sha1":
        return {"X-Hub-Signature": "sha1=" + mac(key, body, hashlib.sha1).hex()}
    if scheme == "stripe-v1":
        return {"Stripe-Signature": f"t=1760277131,v1={mac(key, b'1760277131.' + body).hex()}"}
    if scheme == "standard-webhooks-v1":
        raw = base64.b64decode(secret[6:]) if secret.startswith("whsec_") else key
        sig = base64.b64encode(mac(raw, b"msg_1.1760277131." + body)).decode()
        return {"webhook-id": "msg_1", "webhook-timestamp": "1760277131", "webhook-signature": f"v1,{sig}"}
    if scheme == "hmac-sha512-hex":
        return {"X-Payload-Digest": mac(key, body, hashlib.sha512).hex()}
    if scheme == "hmac-sha512-base64":
        return {"X-Payload-Digest": base64.b64encode(mac(key, body, hashlib.sha512)).decode()}
    raise ValueError(scheme)


SCHEMES = ["hmac-sha256-hex", "hmac-sha256-base64", "github-sha256", "github-sha1", "stripe-v1",
           "standard-webhooks-v1", "hmac-sha512-hex", "hmac-sha512-base64"]


async def post(env, headers, body=BODY):
    return await env.client.post("/hooks/insights", content=body, headers=headers)


async def test_accepted_with_the_bearer_in_under_50_ms(env):
    await post(env, AUTH)  # the app's first request pays one-off warm-up costs; measure a steady-state delivery
    t0 = time.perf_counter()
    r = await post(env, AUTH)
    elapsed = (time.perf_counter() - t0) * 1000
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["id"].startswith("d-")
    assert elapsed < 50, elapsed
    rec = env.deps.hooks.get(r.json()["id"])
    assert rec["elapsed_ms"] < 50 and rec["auth"] == {"configured": "bearer", "ok": True, "scheme_seen": "Bearer"}
    assert rec["headers"]["authorization"] == "Bearer ***" and HOOK_BEARER not in json.dumps(rec)
    assert rec["fields_found"] == {"rule_id": "r-1", "status": "ACTIVE", "resource_urn": "do:droplet:600000001"}
    assert any(e.event == "delivery" and e.data["id"] == rec["id"] for e in env.deps.hub.ring)


async def test_wrong_or_missing_auth_is_rejected_and_kept_apart(env):
    for headers in ({"Authorization": "Bearer nope"}, {}, {"Authorization": f"Basic {HOOK_BEARER}"}):
        r = await post(env, headers)
        assert r.status_code == 401 and r.json()["ok"] is False
    assert len(env.deps.hooks.rejected) == 3 and len(env.deps.hooks.authenticated) == 0
    assert not any(e.event == "delivery" for e in env.deps.hub.ring)
    listed = (await env.client.get("/api/hooks/deliveries")).json()
    assert listed["counts"] == {"authenticated": 0, "rejected": 3}
    assert all(d["auth"]["ok"] is False for d in listed["deliveries"])
    assert env.deps.hooks.find("2000-01-01T00:00:00Z") is None


async def test_basic_auth_and_no_auth():
    async with AppEnv(base_env(PORTHOLE_HOOK_BEARER=None, PORTHOLE_HOOK_BASIC="kraken:basic-pass-123")) as e:
        good = "Basic " + base64.b64encode(b"kraken:basic-pass-123").decode()
        assert (await post(e, {"Authorization": good})).status_code == 200
        bad = "Basic " + base64.b64encode(b"kraken:wrong").decode()
        assert (await post(e, {"Authorization": bad})).status_code == 401
    async with AppEnv(base_env(PORTHOLE_HOOK_BEARER=None)) as e:
        r = await post(e, {})
        assert r.status_code == 200
        assert e.deps.hooks.get(r.json()["id"])["auth"]["configured"] == "none"


@pytest.mark.parametrize("scheme", SCHEMES)
async def test_each_signature_scheme_verifies(env, scheme):
    headers = signed(scheme)
    r = await post(env, {**AUTH, **headers})
    rec = env.deps.hooks.get(r.json()["id"])
    sig = rec["signature"]
    assert sig["verified"] is True, sig
    assert sig["matched"]["scheme"] == scheme
    assert sig["matched"]["header"] in {k.lower() for k in headers}
    assert sig["tried"] >= 1 and sig["note"] is None


def test_standard_webhooks_with_a_prefixed_secret():
    secret = "whsec_" + base64.b64encode(b"sixteen byte key").decode()
    headers = {k.lower(): v for k, v in signed("standard-webhooks-v1", secret=secret).items()}
    result = signature_attempts(headers, BODY, secret)
    assert result["matched"] == {"header": "webhook-signature", "scheme": "standard-webhooks-v1"}


@pytest.mark.parametrize("scheme", SCHEMES)
def test_a_wrong_signature_is_reported_not_required(scheme):
    headers = {k.lower(): v for k, v in signed(scheme, secret="a-different-secret-0000").items()}
    result = signature_attempts(headers, BODY, HOOK_SECRET)
    assert result["verified"] is False and result["tried"] >= 2
    assert result["note"].startswith("no scheme matched")


async def test_no_signature_header_says_so(env):
    rec = env.deps.hooks.get((await post(env, {**AUTH, "X-Kraken": "1"})).json()["id"])
    assert rec["signature"] == {"headers_seen": [], "verified": False, "matched": None, "tried": 0, "attempts": [],
                                "note": "no signature header"}
    listed = (await env.client.get("/api/hooks/deliveries")).json()["deliveries"][0]
    assert listed["signature"]["verdict"] == "no signature header"


def test_header_detection_list():
    headers = {"x-kraken-digest": "a", "content-hmac": "b", "x-sign": "c", "x-kraken": "1", "user-agent": "u"}
    result = signature_attempts(headers, BODY, HOOK_SECRET)
    assert result["headers_seen"] == ["content-hmac", "x-kraken-digest", "x-sign"]
    no_secret = signature_attempts(headers, BODY, "")
    assert no_secret["tried"] == 0 and "PORTHOLE_HOOK_SECRET" in no_secret["note"]


def test_redaction_and_fields():
    assert redact_headers({"Authorization": "Basic abc", "Cookie": "s=1", "X-Kraken": "1"}) == {
        "authorization": "Basic ***", "cookie": "***", "x-kraken": "1"}
    deep = {"a": [{"b": {"rule_name": "kraken churn", "value": 97.5, "triggered_at": "t"}}], "severity": "critical"}
    assert fields_found(deep) == {"severity": "critical", "rule_name": "kraken churn", "value": 97.5,
                                  "triggered_at": "t"}


async def test_non_json_bodies_are_kept_as_text(env):
    rec = env.deps.hooks.get((await post(env, AUTH, b"plain text alert")).json()["id"])
    assert rec["body"] == "plain text alert" and rec["fields_found"] == {}


async def test_other_methods_get_405(env):
    for method in ("GET", "PUT", "DELETE"):
        r = await env.client.request(method, "/hooks/insights")
        assert r.status_code == 405 and r.headers["allow"] == "POST"
        assert r.json()["error"]["code"] == "method_not_allowed"


async def test_body_cap(env):
    r = await post(env, AUTH, b"x" * (64 * 1024 + 1))
    assert r.status_code == 413

    async def chunks():
        for _ in range(70):
            yield b"y" * 1024

    r = await env.client.post("/hooks/insights", content=chunks(), headers=AUTH)
    assert r.status_code == 413
    assert len(env.deps.hooks.authenticated) == 0


async def test_webhook_rate_limit(env):
    codes = [(await post(env, AUTH)).status_code for _ in range(61)]
    assert codes[:60] == [200] * 60 and codes[60] == 429


async def test_delivery_detail_route(env):
    rid = (await post(env, {**AUTH, **signed("github-sha256")})).json()["id"]
    d = (await env.client.get(f"/api/hooks/deliveries/{rid}")).json()
    assert d["signature"]["attempts"] and d["body"]["type"] == "ALERT_TRIGGERED"
    assert (await env.client.get("/api/hooks/deliveries/d-missing")).status_code == 404


async def test_the_sample_delivery(env):
    from conftest import fixture
    body = json.dumps(fixture("delivery.json")).encode()
    rec = env.deps.hooks.get((await post(env, AUTH, body)).json()["id"])
    assert rec["fields_found"]["rule_id"] == "00000000-0000-0000-0000-0000000000a1"
    assert rec["fields_found"]["status"] == "ALERT_INSTANCE_STATUS_ACTIVE"
    assert rec["fields_found"]["resource_urn"] == "do:droplet:600000001"
    found = env.deps.hooks.find("2026-10-12T00:00:00Z", rule_id="00000000-0000-0000-0000-0000000000a1")
    assert found["id"] == rec["id"]
