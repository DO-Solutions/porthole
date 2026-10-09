"""The kraken/ping Function: answers every call with a small JSON body and the time it ran.

The tentacles call it as one hop of a request chain and the head calls it for the fn scenario, so its
invocations show up in the do.serverless metrics."""
from __future__ import annotations

from datetime import datetime, timezone


def main(args: dict) -> dict:
    return {"body": {"pong": True, "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                     "method": str(args.get("__ow_method") or "get")}}
