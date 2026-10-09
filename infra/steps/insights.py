"""Step 14 of design section 9.2: the Insights channels and the alert rules of watcher/alerts/*.json, created
through the harness with the work token.

Channels are found by name, and rules by the ids kept in state.json, because the rule list leaves some rules out
(finding A2)."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from doapi import is_placeholder
from state import now_iso
from steps.common import REPO, Context, StepError, find_named, insights_module

WEBHOOK, EMAIL = "kraken-head", "kraken-email"
ALERTS = REPO / "watcher" / "alerts"
DEFAULT_PUBLIC_URL = "https://insights-demo.digitalocean.solutions"


def templates() -> list[dict]:
    """The rule templates of every file in watcher/alerts, in file order."""
    rules: list[dict] = []
    for path in sorted(ALERTS.glob("*.json")):
        rules.extend(json.loads(path.read_text()).get("rules") or [])
    return rules


def fill(value: Any, replacements: dict[str, str]) -> Any:
    """A copy of a template with each placeholder replaced in every string."""
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [fill(v, replacements) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, replacements) for k, v in value.items()}
    return value


def webhook_url(ctx: Context) -> str:
    return f"{ctx.opt('PUBLIC_URL', DEFAULT_PUBLIC_URL).rstrip('/')}/hooks/insights"


def ensure_insights(ctx: Context) -> None:
    harness = insights_module()
    ins = ctx.insights()
    try:
        channels = _channels(ins)
        hook_id = _channel(ctx, ins, channels, WEBHOOK, "webhook", lambda: harness.webhook_channel(
            WEBHOOK, webhook_url(ctx), bearer=ctx.need("HOOK_BEARER"), secret=ctx.need("HOOK_SECRET"),
            headers={"X-Kraken": "1"}))
        _channel(ctx, ins, channels, EMAIL, "email", lambda: harness.email_channel(EMAIL, ctx.need("ALERT_EMAIL")))
        for template in templates():
            _rule(ctx, ins, template, hook_id)
    except harness.InsightsError as e:
        detail = harness.excerpt(e.body, 200)
        raise StepError(f"Insights API: HTTP {e.status} from {e.request_summary}: {detail}") from None
    except httpx.HTTPError as e:
        raise StepError(f"Insights API: {type(e).__name__}: {e}") from None
    except ValueError as e:  # the harness checks channel and rule fields, such as an https webhook URL
        raise StepError(f"Insights request not sent: {e} (check PUBLIC_URL and watcher/alerts)") from None


def _channels(ins: Any) -> list[dict]:
    found: list[dict] = []
    for page in range(1, 21):
        body = ins.list_channels(page=page, per_page=100)
        found.extend(body.get("notification_channels") or [])
        if page >= int((body.get("pagination") or {}).get("pages") or 1):
            break
    return found


def _channel(ctx: Context, ins: Any, channels: list[dict], name: str, kind: str, spec: Callable[[], dict]) -> str:
    def create() -> dict:
        if ctx.dry_run:
            ctx.say(f"would create Insights {kind} channel {name} (POST /v2/insights/notification-channels)")
            return {"id": ctx.api.placeholder_id(), "dry_run": True}
        return ins.create_channel(spec())["notification_channel"]

    channel, _ = ctx.resource(f"channel:{name}", "insights_channel", name, find_named(channels, name), create,
                              channel_type=kind)
    return channel["id"]


def _rule(ctx: Context, ins: Any, template: dict, channel_id: str) -> None:
    purpose, target, status = template["purpose"], template["target"], template["status"]
    name = template["spec"]["name"]
    key = f"rule:{purpose}"
    old = ctx.state.get(key)
    if old and not is_placeholder(old["id"]):
        try:
            ins.get_rule(old["id"])
            ctx.say(f"exists alert rule {name} (id {old['id']})")
            return
        except insights_module().InsightsError as e:
            if e.status != 404:
                raise
            ctx.say(f"alert rule {name} (id {old['id']}) is gone; creating it again")
    droplet = ctx.require(f"droplet:{target}", "droplets")
    if ctx.dry_run or is_placeholder(channel_id) or is_placeholder(droplet["id"]):
        ctx.say(f"would create alert rule {name} ({status}) on {target} (POST /v2/insights/alert-rules)")
        return
    if not droplet.get("urn"):
        raise StepError(f"{target} has no URN in state.json yet; run the droplets step again")
    spec = fill(template["spec"], {"{{URN}}": droplet["urn"], "{{CHANNEL}}": channel_id})
    rule = ins.create_rule(spec, status=status)["alert_rule"]
    ctx.say(f"created alert rule {name} (id {rule['id']}, {status})")
    ctx.state.record(key, step=ctx.step, kind="insights_rule", ident=rule["id"], name=name, created=True,
                     created_at=now_iso(), purpose=purpose, target=target, status=status)
