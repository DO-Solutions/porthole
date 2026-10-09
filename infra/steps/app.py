"""Step 13 of design section 9.2: the App Platform app from .do/app.yaml, with its SECRET values filled in.

SECRET values come from the environment and go to the API once, which stores them encrypted; later runs find the
app by its spec name and leave it alone, apart from the PORTHOLE_FLEET_JSON that step 15 (fleet.py) sets."""
from __future__ import annotations

from collections.abc import Iterator
from functools import partial

import yaml

from doapi import is_placeholder
from steps.common import REPO, Context, StepError

SPEC = REPO / ".do" / "app.yaml"
SECRET_SOURCES = {"DIGITALOCEAN_TOKEN": "HEAD_TOKEN", "PORTHOLE_CAPTAIN_KEY": "CAPTAIN_KEY",
                  "TENTACLE_KEY": "TENTACLE_KEY", "PORTHOLE_HOOK_BEARER": "HOOK_BEARER",
                  "PORTHOLE_HOOK_SECRET": "HOOK_SECRET"}
COMPONENTS = ("services", "workers", "jobs", "static_sites", "functions")
MIN_CAPTAIN_KEY = 24


def app_envs(spec: dict) -> Iterator[dict]:
    """Every env entry of an app spec: app level first, then each component's."""
    yield from spec.get("envs") or []
    for kind in COMPONENTS:
        for component in spec.get(kind) or []:
            yield from component.get("envs") or []


def load_spec(ctx: Context) -> dict:
    """.do/app.yaml with each SECRET value taken from its variable (DIGITALOCEAN_TOKEN gets HEAD_TOKEN, never the
    work token), PORTHOLE_PUBLIC_URL from PUBLIC_URL and PORTHOLE_DO_CONTEXT from DO_CONTEXT when they are set."""
    spec = yaml.safe_load(SPEC.read_text())
    for env in app_envs(spec):
        if env.get("type") == "SECRET":
            source = SECRET_SOURCES.get(env["key"])
            if source is None:
                raise StepError(f".do/app.yaml has the SECRET {env['key']}, and no variable is mapped to it")
            env["value"] = ctx.need(source)
        elif env.get("key") == "PORTHOLE_PUBLIC_URL" and ctx.opt("PUBLIC_URL"):
            env["value"] = ctx.opt("PUBLIC_URL").rstrip("/")
    if ctx.opt("DO_CONTEXT"):  # optional, so it is added only when set rather than kept empty in the spec
        spec["services"][0].setdefault("envs", []).append(
            {"key": "PORTHOLE_DO_CONTEXT", "scope": "RUN_TIME", "value": ctx.opt("DO_CONTEXT")})
    if len(ctx.need("CAPTAIN_KEY")) < MIN_CAPTAIN_KEY:
        raise StepError(f"CAPTAIN_KEY must be at least {MIN_CAPTAIN_KEY} characters; the head refuses shorter keys")
    return spec


def head_region(app: dict) -> str:
    """App Platform says "tor"; the fleet JSON wants a region slug like "tor1"."""
    slug = str((app.get("region") or {}).get("slug") or "tor1")
    return slug if slug[-1:].isdigit() else f"{slug}1"


def ensure_app(ctx: Context) -> None:
    spec = load_spec(ctx)
    name = spec["name"]
    found = next((a for a in ctx.api.paginate("/v2/apps", "apps") if (a.get("spec") or {}).get("name") == name), None)
    create = partial(ctx.create, "/v2/apps", {"spec": spec}, "app", f"create app {name} from .do/app.yaml")
    app, entry = ctx.resource("app", "app", name, found, create)
    if is_placeholder(app["id"]):
        return
    if not found:
        ctx.say(f"waiting for the first deployment of app {name}")
        app = wait_deployment(ctx, app["id"], None)
    elif not app.get("active_deployment"):
        ctx.say(f"app {name} has no active deployment yet; check it in the control panel")
    if entry["created"]:
        ctx.assign(f"do:app:{app['id']}")
    url = app.get("default_ingress") or app.get("live_url")
    ctx.state.update("app", url=url, live_url=app.get("live_url"), region=head_region(app))
    ctx.say(f"app {name} answers at {url}")


def latest_deployment(ctx: Context, app_id: str) -> dict | None:
    answer = ctx.api.get(f"/v2/apps/{app_id}/deployments", {"page": 1, "per_page": 5})
    deployments = answer.get("deployments") or []
    return deployments[0] if deployments else None


def wait_deployment(ctx: Context, app_id: str, previous: str | None) -> dict:
    """Wait until the newest deployment (other than previous) is ACTIVE; return the app as it is then."""
    def active() -> dict | None:
        latest = latest_deployment(ctx, app_id)
        if not latest or latest.get("id") == previous:
            return None
        if latest.get("phase") in ("ERROR", "CANCELED"):
            raise StepError(f"deployment {latest.get('id')} of app {app_id} ended in {latest['phase']}")
        return latest if latest.get("phase") == "ACTIVE" else None

    ctx.wait(active, ctx.timeouts.deploy, f"app {app_id} to finish deploying")
    return ctx.api.get(f"/v2/apps/{app_id}")["app"]
