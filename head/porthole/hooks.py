"""The webhook receiver's checks and store: bearer or basic auth, every signature scheme of design Appendix B.

A signature is attempted, never required, because Insights does not document its scheme; each delivery records
which header and scheme matched, or says plainly that there was no signature header. Rejected deliveries are kept
in their own ring so probes and retries are visible without reaching the voyage timeline."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from porthole.clock import iso, new_id, parse_iso

CANDIDATE = re.compile(r"signature|sign|hmac|digest", re.I)
FIELDS = ("rule_id", "rule_name", "status", "state", "severity", "resource_urn", "value", "triggered_at",
          "resolved_at", "instance_id")
SECRET_HEADERS = ("cookie", "proxy-authorization")


def check_auth(headers: dict[str, str], bearer: str, basic: str) -> dict:
    """{"configured", "ok", "scheme_seen"}; with nothing configured every caller is accepted."""
    configured = "+".join(k for k, v in (("bearer", bearer), ("basic", basic)) if v) or "none"
    raw = headers.get("authorization", "")
    scheme, _, value = raw.partition(" ")
    seen = scheme.capitalize() if raw else None
    ok = configured == "none"
    if bearer and scheme.lower() == "bearer":
        ok = ok or hmac.compare_digest(value.strip().encode(), bearer.encode())
    if basic and scheme.lower() == "basic":
        expected = base64.b64encode(basic.encode()).decode()
        ok = ok or hmac.compare_digest(value.strip().encode(), expected.encode())
    return {"configured": configured, "ok": ok, "scheme_seen": seen}


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    out = {}
    for name, value in headers.items():
        lower = name.lower()
        if lower == "authorization":
            out[lower] = f"{value.split(' ', 1)[0]} ***" if value else ""
        elif lower in SECRET_HEADERS:
            out[lower] = "***"
        else:
            out[lower] = value
    return out


def _mac(key: bytes, msg: bytes, algo: Any) -> bytes:
    return hmac.new(key, msg, algo).digest()


def _same(given: str, expected: str, fold: bool = False) -> bool:
    given, expected = given.strip(), expected.strip()
    if fold:
        given, expected = given.lower(), expected.lower()
    return hmac.compare_digest(given.encode(), expected.encode())


def _schemes(name: str, value: str, body: bytes, secret: str, headers: dict[str, str]) -> list[tuple[str, bool]]:
    """Every (scheme, matched) pair that applies to one candidate header."""
    key = secret.encode()
    sha256, sha512, sha1 = (_mac(key, body, hashlib.sha256), _mac(key, body, hashlib.sha512),
                            _mac(key, body, hashlib.sha1))
    out = [("hmac-sha256-hex", _same(value, sha256.hex(), True)),
           ("hmac-sha256-base64", _same(value, base64.b64encode(sha256).decode()))]
    if value.lower().startswith("sha256="):
        out.append(("github-sha256", _same(value[7:], sha256.hex(), True)))
    if value.lower().startswith("sha1="):
        out.append(("github-sha1", _same(value[5:], sha1.hex(), True)))
    parts = dict(p.split("=", 1) for p in value.replace(" ", "").split(",") if "=" in p)
    if "t" in parts and "v1" in parts:
        v1s = [p.split("=", 1)[1] for p in value.replace(" ", "").split(",") if p.startswith("v1=")]
        expected = _mac(key, f"{parts['t']}.".encode() + body, hashlib.sha256).hex()
        out.append(("stripe-v1", any(_same(v, expected, True) for v in v1s)))
    if name == "webhook-signature" and headers.get("webhook-id") and headers.get("webhook-timestamp"):
        msg = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
        keys = [key]
        if secret.startswith("whsec_"):
            try:
                keys.append(base64.b64decode(secret[6:]))
            except ValueError:
                pass
        given = [s.split(",", 1)[1] for s in value.split() if s.startswith("v1,")]
        expected = [base64.b64encode(_mac(k, msg, hashlib.sha256)).decode() for k in keys]
        out.append(("standard-webhooks-v1", any(_same(g, e) for g in given for e in expected)))
    out += [("hmac-sha512-hex", _same(value, sha512.hex(), True)),
            ("hmac-sha512-base64", _same(value, base64.b64encode(sha512).decode()))]
    return out


def signature_attempts(headers: dict[str, str], body: bytes, secret: str) -> dict:
    """{headers_seen, verified, matched: {header, scheme} | None, tried, attempts, note}."""
    seen = sorted(k for k in headers if CANDIDATE.search(k))
    result: dict[str, Any] = {"headers_seen": seen, "verified": False, "matched": None, "tried": 0, "attempts": [],
                              "note": None}
    if not seen:
        result["note"] = "no signature header"
        return result
    if not secret:
        result["note"] = "signature header present but PORTHOLE_HOOK_SECRET is not set, so nothing was checked"
        return result
    for name in seen:
        for scheme, matched in _schemes(name, headers[name], body, secret, headers):
            result["tried"] += 1
            result["attempts"].append({"header": name, "scheme": scheme, "matched": matched})
            if matched and result["matched"] is None:
                result["matched"] = {"header": name, "scheme": scheme}
    result["verified"] = result["matched"] is not None
    if not result["verified"]:
        result["note"] = f"no scheme matched ({result['tried']} tries on {', '.join(seen)})"
    return result


def verdict(signature: dict) -> str:
    if signature.get("verified"):
        return f"verified: {signature['matched']['scheme']} in {signature['matched']['header']}"
    return signature.get("note") or "not verified"


def fields_found(body: Any) -> dict:
    """The first value of each interesting key at any depth, so the first real delivery documents the schema."""
    found: dict[str, Any] = {}

    def walk(node: Any, depth: int) -> None:
        if depth > 8 or len(found) == len(FIELDS):
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k in FIELDS and k not in found and not isinstance(v, (dict, list)):
                    found[k] = v
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node[:50]:
                walk(v, depth + 1)

    walk(body, 0)
    return found


def parse_body(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def excerpt(body: Any, limit: int = 200) -> str:
    text = body if isinstance(body, str) else json.dumps(body, separators=(",", ":"))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


REJECTED_NOTE = "rejected: the body and headers of unauthenticated deliveries are not shown"
PUBLIC_REJECTED_KEYS = ("id", "received_at", "size", "content_type", "auth", "elapsed_ms", "matched_voyage")


class DeliveryStore:
    def __init__(self, hub: Any, now: Callable[[], Any], size_auth: int = 200, size_unauth: int = 50,
                 secrets: Iterable[str] = ()):
        self.hub, self.now = hub, now
        self.secrets = [s for s in secrets if s and len(s) >= 6]
        self.authenticated: deque[dict] = deque(maxlen=size_auth)
        self.rejected: deque[dict] = deque(maxlen=size_unauth)

    def scrub(self, value: Any) -> Any:
        """Configured secrets replaced with *** wherever they appear in a stored header or body: a channel whose
        bearer was also put in a custom header, or a body that echoes a key, must not show it on the page."""
        if isinstance(value, str):
            for s in self.secrets:
                value = value.replace(s, "***")
            return value
        if isinstance(value, dict):
            return {k: self.scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(v) for v in value]
        return value

    def record(self, *, headers: dict[str, str], raw: bytes, auth: dict, signature: dict, remote_ip: str,
               elapsed_ms: float | None = None) -> dict:
        body = self.scrub(parse_body(raw))
        rec = {"id": new_id("d"), "received_at": iso(self.now(), millis=True), "remote_ip": remote_ip,
               "size": len(raw), "content_type": headers.get("content-type"), "auth": auth,
               "headers": self.scrub(redact_headers(headers)), "signature": signature, "body": body,
               "fields_found": fields_found(body), "matched_voyage": None, "excerpt": excerpt(body),
               "elapsed_ms": elapsed_ms}
        (self.authenticated if auth["ok"] else self.rejected).append(rec)
        if auth["ok"]:
            self.hub.publish("delivery", {"id": rec["id"], "t": rec["received_at"], "auth_ok": True,
                                          "signature": verdict(signature), "excerpt": rec["excerpt"]})
        return rec

    @staticmethod
    def summary(rec: dict) -> dict:
        """One row of the public list. A rejected delivery shows when, how big and why it was refused, never
        what an unauthenticated caller wrote, so nobody can put text on the page by posting to the webhook."""
        sig = {**rec["signature"], "verdict": verdict(rec["signature"]), "attempts": None}
        if not rec["auth"]["ok"]:
            return {**{k: rec[k] for k in PUBLIC_REJECTED_KEYS}, "excerpt": None, "fields_found": {},
                    "signature": {"headers_seen": sig["headers_seen"], "verified": False, "matched": None,
                                  "tried": 0, "attempts": None, "note": REJECTED_NOTE, "verdict": REJECTED_NOTE},
                    "note": REJECTED_NOTE}
        keys = ("id", "received_at", "size", "auth", "excerpt", "matched_voyage", "fields_found")
        return {k: rec[k] for k in keys} | {"signature": sig}

    @staticmethod
    def public(rec: dict) -> dict:
        """The full record of an authenticated delivery; the metadata only of a rejected one."""
        if rec["auth"]["ok"]:
            return rec
        return {**{k: rec[k] for k in PUBLIC_REJECTED_KEYS}, "note": REJECTED_NOTE,
                "signature": {"headers_seen": rec["signature"]["headers_seen"]}}

    def list(self, limit: int = 50) -> list[dict]:
        items = sorted(list(self.authenticated) + list(self.rejected), key=lambda r: r["received_at"], reverse=True)
        return [self.summary(r) for r in items[:max(0, limit)]]

    def get(self, delivery_id: str) -> dict | None:
        return next((r for r in list(self.authenticated) + list(self.rejected) if r["id"] == delivery_id), None)

    def find(self, after_iso: str, rule_id: str | None = None, urn: str | None = None,
             exclude: tuple[str, ...] = ()) -> dict | None:
        """An authenticated delivery at or after a time: one naming the rule or URN, else the first one."""
        after = parse_iso(after_iso)
        fresh = [r for r in self.authenticated if r["id"] not in exclude and r["matched_voyage"] is None
                 and (after is None or (parse_iso(r["received_at"]) or after) >= after)]
        for r in fresh:
            text = json.dumps(r["body"]) if not isinstance(r["body"], str) else r["body"]
            if (rule_id and rule_id in text) or (urn and urn in text):
                return r
        return fresh[0] if fresh else None
