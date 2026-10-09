"""Step 11 of design section 9.2: the container registry, of which a team has at most one.

An existing registry is reused and recorded with created: false so teardown leaves it alone; only when there is
none does this step create "kraken" on the starter tier."""
from __future__ import annotations

from functools import partial

from steps.common import Context

NAME = "kraken"


def _find_registry(ctx: Context) -> dict | None:
    """A team may hold several registries now; the single-registry endpoint then answers 412.

    Prefer the multi-registry listing and pick the one named "kraken" if it exists, else the first; fall back to the
    legacy endpoint for teams that still have at most one."""
    listing = ctx.api.get("/v2/registries", missing_ok=True)
    registries = (listing or {}).get("registries") or []
    if registries:
        preferred = [r for r in registries if r.get("name") == NAME] or [r for r in registries if "poseidon" in r.get("name", "")]
        return preferred[0] if preferred else registries[0]
    answer = ctx.api.get("/v2/registry", missing_ok=True)
    return (answer or {}).get("registry")


def ensure_registry(ctx: Context) -> None:
    found = _find_registry(ctx)
    body = {"name": NAME, "subscription_tier_slug": "starter"}
    registry, entry = ctx.resource("registry", "registry", found.get("name") if found else NAME, found,
                                   partial(ctx.create, "/v2/registry", body, "registry",
                                           f"create registry {NAME} (starter tier)", "name"),
                                   id_field="name", region=(found or {}).get("region"))
    if not entry["created"]:
        ctx.say(f"registry {registry.get('name')} belongs to the team; teardown leaves it alone")
