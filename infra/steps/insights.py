"""Step 14 of design section 9.2: the Insights channels and the alert rules of watcher/alerts/*.json, created
through the harness with the work token.

Channels are found by name, and rules by the ids kept in state.json, because the rule list leaves some rules out
(finding A2). A rule that exists but whose spec differs from its template, such as the five created on metric
names Insights does not have (B-034), is corrected in place with PUT; its status is left as it is, since rules are
paused and resumed from the Alerts page."""
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


def differences(want: Any, have: Any, path: str = "") -> list[str]:
    """Where the live spec differs from the template, as "path old -> new". Only the template's keys count: the
    API may add defaults, and those are no reason to send a PUT."""
    if isinstance(want, dict) and isinstance(have, dict):
        return [d for k, v in want.items() for d in differences(v, have.get(k), f"{path}.{k}" if path else k)]
    if isinstance(want, list) and isinstance(have, list) and len(want) == len(have):
        return [d for i, (w, h) in enumerate(zip(want, have, strict=True)) for d in differences(w, h, f"{path}[{i}]")]
    return [] if want == have else [f"{path} {_shown(have)} -> {_shown(want)}"]


def _shown(value: Any) -> str:
    return "unset" if value is None else value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))


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
    live = None
    if old and not is_placeholder(old["id"]):
        try:
            live = ins.get_rule(old["id"]).get("alert_rule") or {}
        except insights_module().InsightsError as e:
            if e.status != 404:
                raise
            ctx.say(f"alert rule {name} (id {old['id']}) is gone; creating it again")
    droplet = ctx.require(f"droplet:{target}", "droplets")
    planned = ctx.dry_run or is_placeholder(channel_id) or is_placeholder(droplet["id"])
    if live is None and planned:
        ctx.say(f"would create alert rule {name} ({status}) on {target} (POST /v2/insights/alert-rules)")
        return
    if not droplet.get("urn"):
        raise StepError(f"{target} has no URN in state.json yet; run the droplets step again")
    spec = fill(template["spec"], {"{{URN}}": droplet["urn"], "{{CHANNEL}}": channel_id})
    if live is not None:
        _correct(ctx, ins, key, old["id"], spec, live.get("spec") or {}, planned)
        return
    rule = ins.create_rule(spec, status=status)["alert_rule"]
    ctx.say(f"created alert rule {name} (id {rule['id']}, {status})")
    ctx.state.record(key, step=ctx.step, kind="insights_rule", ident=rule["id"], name=name, created=True,
                     created_at=now_iso(), purpose=purpose, target=target, status=status)


def _correct(ctx: Context, ins: Any, key: str, rule_id: str, spec: dict, live: dict, planned: bool) -> None:
    """Leave a rule that matches its template; PUT the template's spec over one that does not. The PUT carries no
    status, so the API keeps the rule's own (a paused rule stays paused)."""
    name = spec["name"]
    changes = differences(spec, live)
    if not changes:
        ctx.say(f"exists alert rule {name} (id {rule_id})")
        return
    what = "; ".join(changes)
    if planned:
        ctx.say(f"would update alert rule {name} (id {rule_id}): {what} (PUT /v2/insights/alert-rules/{rule_id})")
        return
    ins.update_rule(rule_id, spec)
    ctx.say(f"updated alert rule {name} (id {rule_id}): {what}")
    ctx.state.update(key, name=name, updated_at=now_iso())
