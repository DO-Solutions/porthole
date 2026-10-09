"""Step 11 of design section 9.2: the container registry, of which a team has at most one.

An existing registry is reused and recorded with created: false so teardown leaves it alone; only when there is
none does this step create "kraken" on the starter tier."""
from __future__ import annotations

from functools import partial

from steps.common import Context

NAME = "kraken"


def ensure_registry(ctx: Context) -> None:
    answer = ctx.api.get("/v2/registry", missing_ok=True)
    found = (answer or {}).get("registry")
    body = {"name": NAME, "subscription_tier_slug": "starter"}
    registry, entry = ctx.resource("registry", "registry", found.get("name") if found else NAME, found,
                                   partial(ctx.create, "/v2/registry", body, "registry",
                                           f"create registry {NAME} (starter tier)", "name"),
                                   id_field="name", region=(found or {}).get("region"))
    if not entry["created"]:
        ctx.say(f"registry {registry.get('name')} belongs to the team; teardown leaves it alone")
