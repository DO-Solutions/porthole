"""POST /hooks/insights: the receiver for the Insights webhook channel; any other method answers 405.

It reads at most 64 KiB, checks the configured auth, tries the signature schemes, stores the delivery and answers
at once; the Alert round trip voyage picks the delivery up from the store."""
from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from porthole import hooks as checks
from porthole.security import MAX_BODY, BodyTooLarge, client_ip, error_body

router = APIRouter()


async def read_capped(request: Request, limit: int = MAX_BODY) -> bytes:
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise BodyTooLarge()
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise BodyTooLarge()
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/hooks/insights")
async def receive(request: Request) -> JSONResponse:
    t0 = time.perf_counter()
    deps = request.app.state.deps
    deps.guard.limit(request, "hook")
    raw = await read_capped(request)
    headers = {k.lower(): v for k, v in request.headers.items()}
    s = deps.settings
    auth = checks.check_auth(headers, s.hook_bearer, s.hook_basic)
    signature = checks.signature_attempts(headers, raw, s.hook_secret)
    rec = deps.hooks.record(headers=headers, raw=raw, auth=auth, signature=signature,
                            remote_ip=client_ip(request, s.trust_proxy),
                            elapsed_ms=round((time.perf_counter() - t0) * 1000, 2))
    if not auth["ok"]:
        deps.log.warn("webhook delivery rejected: authentication did not match", delivery_id=rec["id"],
                      scheme_seen=auth["scheme_seen"])
        return JSONResponse({"ok": False, "id": rec["id"], "error": "authentication required"}, status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})
    deps.log.info("webhook delivery", delivery_id=rec["id"], size=rec["size"],
                  signature=checks.verdict(signature), excerpt=rec["excerpt"])
    return JSONResponse({"ok": True, "id": rec["id"]})


@router.api_route("/hooks/insights", methods=["GET", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def wrong_method() -> JSONResponse:
    return JSONResponse(error_body("method_not_allowed", "POST only"), status_code=405, headers={"Allow": "POST"})
