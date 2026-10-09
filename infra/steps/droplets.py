"""Step 5 of design section 9.2: the three tentacles, Droplets running tentacle/install.sh as user_data.

The installer's variables are exported right after its shebang, PEER_URL pointing at the peer's reserved IP; once a
Droplet is active its reserved IP is assigned to it, and the step waits until every tentacle answers /health."""
from __future__ import annotations

import shlex
from collections.abc import Mapping
from functools import partial

import httpx

from doapi import is_placeholder
from steps.common import REPO, TAG, TENTACLE_PORT, TENTACLE_TAG, TENTACLES, Context, StepError, Tentacle
from steps.database import dsn

SIZE, IMAGE = "s-1vcpu-1gb", "ubuntu-24-04-x64"
INSTALL = REPO / "tentacle" / "install.sh"


def user_data(script: str, exports: Mapping[str, str]) -> str:
    """The installer with one `export NAME=value` line per variable right after the shebang; empty values are
    left out so the installer's own defaults apply."""
    first, _, rest = script.partition("\n")
    if not first.startswith("#!"):
        raise StepError("tentacle/install.sh must start with a shebang line to work as user_data")
    lines = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in exports.items() if value)
    return f"{first}\n{lines}{rest}"


def ssh_keys(text: str) -> list[int | str]:
    """SSH_KEY_IDS as the API wants it: numeric ids as integers, fingerprints as strings."""
    return [int(k) if k.isdigit() else k for k in (part.strip() for part in text.split(",")) if k]


def public_ip(droplet: dict, avoid: str | None = None) -> str | None:
    """The Droplet's public IPv4, preferring one that is not its reserved IP."""
    ips = [n.get("ip_address") for n in (droplet.get("networks") or {}).get("v4") or [] if n.get("type") == "public"]
    return next((ip for ip in ips if ip != avoid), ips[0] if ips else None)


def ensure_droplets(ctx: Context) -> None:
    existing = {d.get("name"): d for d in ctx.api.paginate("/v2/droplets", "droplets", {"tag_name": TAG})}
    boot: dict[str, str] = {}
    if any(t.name not in existing for t in TENTACLES):
        boot = {"TENTACLE_KEY": ctx.need("TENTACLE_KEY"), "PG_DSN": dsn(ctx),
                "FN_URL": ctx.require("functions", "functions").get("url") or "",
                "TENTACLE_TARBALL_URL": ctx.need("TENTACLE_TARBALL_URL")}
    actions: dict[str, list] = {}
    for t in TENTACLES:
        ctx.resource(f"droplet:{t.name}", "droplet", t.name, existing.get(t.name),
                     partial(_create, ctx, t, boot, actions), region=t.region)
    for t in TENTACLES:
        _settle(ctx, t, actions.get(t.name, []))
    for t in TENTACLES:
        _wait_healthy(ctx, t)


def _create(ctx: Context, t: Tentacle, boot: Mapping[str, str], actions: dict[str, list]) -> dict:
    vpc = ctx.require(f"vpc:{t.region}", "network")
    peer_url = ""
    if t.peer:
        peer_ip = ctx.require(f"reserved_ip:{t.peer}", "network")["id"]
        peer_url = f"http://{peer_ip}:{TENTACLE_PORT}"
    exports = {"TENTACLE_NAME": t.name, "TENTACLE_KEY": boot["TENTACLE_KEY"], "PEER_URL": peer_url,
               "PG_DSN": boot["PG_DSN"], "FN_URL": boot["FN_URL"],
               "TENTACLE_TARBALL_URL": boot["TENTACLE_TARBALL_URL"]}
    body = {"name": t.name, "region": t.region, "size": SIZE, "image": IMAGE, "monitoring": True, "ipv6": False,
            "ssh_keys": ssh_keys(ctx.need("SSH_KEY_IDS")), "tags": [TAG, TENTACLE_TAG],
            "user_data": user_data(INSTALL.read_text(), exports)}
    if not is_placeholder(vpc["id"]):
        body["vpc_uuid"] = vpc["id"]
    answer = ctx.api.create("/v2/droplets", body, "droplet", note=f"create Droplet {t.name} in {t.region}")
    actions[t.name] = [a.get("id") for a in (answer.get("links") or {}).get("actions") or []]
    return answer["droplet"]


def _settle(ctx: Context, t: Tentacle, action_ids: list) -> None:
    """Wait for the create action and the public IP, then attach the reserved IP and assign the project."""
    key = f"droplet:{t.name}"
    entry = ctx.state.get(key) or {}
    reserved = ctx.state.get(f"reserved_ip:{t.name}")
    if is_placeholder(entry.get("id")):
        if reserved:
            ctx.say(f"would assign the reserved IP for {t.name} once the Droplet is active")
        return
    for action_id in action_ids:
        ctx.api.wait_action(action_id, ctx.timeouts.action)
    droplet = ctx.wait(partial(_active, ctx, entry["id"]), ctx.timeouts.droplet, f"{t.name} to be active")
    ctx.state.update(key, ip=public_ip(droplet, reserved and reserved["id"]), urn=f"do:droplet:{entry['id']}")
    if reserved:
        _attach(ctx, t.name, entry["id"], reserved["id"])
    if entry.get("created"):
        ctx.assign(f"do:droplet:{entry['id']}")


def _active(ctx: Context, droplet_id: int) -> dict | None:
    droplet = ctx.api.get(f"/v2/droplets/{droplet_id}")["droplet"]
    return droplet if droplet.get("status") == "active" and public_ip(droplet) else None


def _attach(ctx: Context, name: str, droplet_id: int, ip: str) -> None:
    if is_placeholder(ip):
        ctx.say(f"would assign the reserved IP for {name} once it exists")
        return
    current = ctx.api.get(f"/v2/reserved_ips/{ip}")["reserved_ip"]
    if (current.get("droplet") or {}).get("id") == droplet_id:
        ctx.say(f"exists reserved IP {ip} on {name}")
        return
    answer = ctx.api.post(f"/v2/reserved_ips/{ip}/actions", {"type": "assign", "droplet_id": droplet_id},
                          note=f"assign reserved IP {ip} to {name}")
    if answer is not None:
        ctx.api.wait_action((answer.get("action") or {}).get("id"), ctx.timeouts.action)
        ctx.say(f"assigned reserved IP {ip} to {name}")


def _wait_healthy(ctx: Context, t: Tentacle) -> None:
    entry = ctx.state.get(f"droplet:{t.name}") or {}
    if is_placeholder(entry.get("id")):
        ctx.say(f"would wait for {t.name} to answer /health on port {TENTACLE_PORT}")
        return
    url = f"http://{entry['ip']}:{TENTACLE_PORT}/health"
    ctx.wait(partial(_healthy, ctx, url), ctx.timeouts.health, f"{t.name} to answer {url}")
    ctx.say(f"healthy {t.name} ({url})")


def _healthy(ctx: Context, url: str) -> bool:
    try:
        return ctx.web.get(url, timeout=5).status_code == 200
    except httpx.HTTPError:
        return False
