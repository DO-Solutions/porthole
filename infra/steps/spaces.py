"""Step 10 of design section 9.2: the Spaces key kraken-spaces and the bucket kraken-<6 hex> in tor1, created with
one S3 PUT signed with AWS Signature Version 4.

A key's secret is shown only when the key is created, so it is used in memory and dropped, and a later run or
teardown that has to sign a request creates a short-lived key and deletes it again."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlsplit

import httpx

from state import now_iso
from steps.common import Context, StepError, find_named

# UNVERIFIED: that Spaces is offered in tor1 (BUGS.md B-016).
KEY_NAME, TEMP_KEY, REGION = "kraken-spaces", "kraken-spaces-temp", "tor1"
ENDPOINT = f"https://{REGION}.digitaloceanspaces.com"


def key_body(name: str) -> dict:
    # UNVERIFIED: the /v2/spaces/keys body; an empty bucket with "fullaccess" is read as "every bucket" (B-016).
    return {"name": name, "grants": [{"bucket": "", "permission": "fullaccess"}]}


def sigv4_headers(method: str, url: str, region: str, access_key: str, secret_key: str, when: datetime,
                  payload: bytes = b"", service: str = "s3") -> dict[str, str]:
    """Headers that sign one request with AWS Signature Version 4, which the Spaces S3 API accepts."""
    parts = urlsplit(url)
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    body_hash = hashlib.sha256(payload).hexdigest()
    headers = {"host": parts.netloc, "x-amz-content-sha256": body_hash, "x-amz-date": stamp}
    names = ";".join(sorted(headers))
    query = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
                     for k, v in sorted(parse_qsl(parts.query, keep_blank_values=True)))
    canonical = "\n".join([method, quote(parts.path or "/", safe="/-_.~"), query,
                           "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)), names, body_hash])
    scope = f"{stamp[:8]}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = f"AWS4{secret_key}".encode()
    for part in (stamp[:8], region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={names}, "
                                f"Signature={signature}")
    return headers


def s3(ctx: Context, method: str, bucket: str, access_key: str, secret_key: str) -> int:
    """One signed S3 call on a bucket (path style); returns the HTTP status."""
    url = f"{ENDPOINT}/{bucket}"
    headers = sigv4_headers(method, url, REGION, access_key, secret_key, datetime.now(timezone.utc))
    try:
        return ctx.web.request(method, url, headers=headers, content=b"").status_code
    except httpx.HTTPError as e:
        raise StepError(f"{method} {url} failed: {type(e).__name__}") from None


def bucket_exists(ctx: Context, bucket: str) -> bool:
    """An unsigned HEAD answers 404 for a bucket that does not exist and 403 for one that does."""
    # UNVERIFIED: the 404 versus 403 split on an unsigned HEAD, as on S3 (B-016).
    try:
        return ctx.web.head(f"{ENDPOINT}/{bucket}", timeout=10).status_code != 404
    except httpx.HTTPError as e:
        raise StepError(f"HEAD {ENDPOINT}/{bucket} failed: {type(e).__name__}") from None


@contextmanager
def temporary_key(ctx: Context) -> Iterator[tuple[str, str]]:
    """A full-access Spaces key that exists only for the duration of the block."""
    key = ctx.create("/v2/spaces/keys", key_body(TEMP_KEY), "key", f"create Spaces key {TEMP_KEY}", "access_key")
    ctx.state.add_secret("a Spaces secret key", key.get("secret_key"))
    try:
        yield key["access_key"], key.get("secret_key") or ""
    finally:
        ctx.api.delete(f"/v2/spaces/keys/{key['access_key']}", note=f"delete Spaces key {TEMP_KEY}")


def ensure_spaces(ctx: Context) -> None:
    fresh: dict = {}

    def create_key() -> dict:
        key = ctx.create("/v2/spaces/keys", key_body(KEY_NAME), "key", f"create Spaces key {KEY_NAME}", "access_key")
        ctx.state.add_secret("the Spaces secret key", key.get("secret_key"))
        fresh.update(key)
        return key

    found = find_named(ctx.api.paginate("/v2/spaces/keys", "keys"), KEY_NAME)
    ctx.resource("spaces_key", "spaces_key", KEY_NAME, found, create_key, id_field="access_key")
    _bucket(ctx, fresh)


def _bucket(ctx: Context, fresh: dict) -> None:
    old = ctx.state.get("spaces_bucket")
    name = old["id"] if old else f"kraken-{secrets.token_hex(3)}"
    if old and bucket_exists(ctx, name):
        ctx.say(f"exists Spaces bucket {name} in {REGION}")
        if old.get("created"):
            ctx.assign(f"do:space:{name}")
        return
    if ctx.dry_run:
        ctx.say(f"would create Spaces bucket {name} in {REGION} (one signed S3 PUT)")
        return
    if fresh.get("secret_key"):
        status = s3(ctx, "PUT", name, fresh["access_key"], fresh["secret_key"])
    else:
        with temporary_key(ctx) as (access_key, secret_key):
            status = s3(ctx, "PUT", name, access_key, secret_key)
    if status not in (200, 204):
        raise StepError(f"creating Spaces bucket {name} failed: S3 PUT answered HTTP {status}")
    ctx.say(f"created Spaces bucket {name} in {REGION}")
    ctx.state.record("spaces_bucket", step=ctx.step, kind="spaces_bucket", ident=name, name=name, created=True,
                     created_at=now_iso(), region=REGION)
    ctx.assign(f"do:space:{name}")
