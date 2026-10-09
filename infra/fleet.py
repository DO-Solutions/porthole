"""Builds PORTHOLE_FLEET_JSON (design Appendix D) from infra/out/state.json and writes infra/out/porthole.env.

Run on its own it only writes that file; as step 15 of provision.py it also sets the variable on the app through
the API, waits for the redeploy, and checks /healthz and /api/fleet on the live app."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

from doapi import is_placeholder
from state import State, write_text
from steps.app import app_envs, latest_deployment, wait_deployment
from steps.common import PROJECT, TENTACLE_PORT, TENTACLES, Context, StepError, note_urn, rel

OUT = Path(__file__).resolve().parent / "out"
ENV_FILE = "porthole.env"
REGIONS = ["tor1", "syd1"]
SEA = (("lb", "load_balancer", 4), ("database", "database", 6), ("doks", "kubernetes", 7))
URN_PATTERNS = {"lb": "do:loadbalancer:{}", "database": "do:dbaas:{}", "doks": "do:kubernetes:{}"}
FUNCTIONS_URN = "do:functions_namespace:{}"  # the only label on every do_functions_* series in tor1 (B-033)


def usable(entry: dict | None) -> bool:
    return bool(entry and entry.get("id") and not is_placeholder(entry["id"]))


def build_fleet(state: State) -> dict:
    """The fleet description the head reads: ids, IPs, URNs and names of what state.json holds, no secrets.
    Tentacle-1 and -2 are reached on their reserved IPs; tentacle-3 has no peer and uses its public IPv4."""
    listed = []
    for t in TENTACLES:
        droplet, reserved = state.get(f"droplet:{t.name}") or {}, state.get(f"reserved_ip:{t.name}")
        ip = reserved["id"] if usable(reserved) else droplet.get("ip")
        if usable(droplet) and ip:
            listed.append((t, droplet, ip))
    present = {t.name for t, _, _ in listed}  # a peer or rule target must be a listed tentacle
    tentacles = [{"name": t.name, "display": t.name.removeprefix("kraken-"), "region": t.region,
                  "url": f"http://{ip}:{TENTACLE_PORT}", "id": droplet["id"],
                  "urn": droplet.get("urn") or f"do:droplet:{droplet['id']}", "service_name": t.name,
                  "peer": t.peer if t.peer in present else None, "slot": t.slot} for t, droplet, ip in listed]
    fleet: dict = {"project": PROJECT, "regions": REGIONS, "tentacles": tentacles}
    app = state.get("app")
    if usable(app):
        fleet["head"] = {"app_id": app["id"], "urn": f"do:app:{app['id']}", "region": app.get("region") or "tor1",
                         "service_name": "porthole", "slot": 5}
    fleet["sea"] = _sea(state)
    rules = [{"id": e["id"], "purpose": e.get("purpose"), "name": e["name"], "target": e.get("target")}
             for e in state.resources.values() if e.get("kind") == "insights_rule" and usable(e)
             and e.get("target") in present]
    hook, email = state.get("channel:kraken-head"), state.get("channel:kraken-email")
    fleet["watcher"] = {"rules": rules, "channel_webhook_id": hook["id"] if usable(hook) else None,
                        "channel_email_id": email["id"] if usable(email) else None, "dashboard": "krakens-eye"}
    return fleet


def _sea(state: State) -> dict:
    sea: dict = {}
    for key, kind, slot in SEA:
        entry = state.get(key)
        if not usable(entry):
            continue
        sea[kind] = {"name": entry["name"], "id": entry["id"], "region": entry.get("region"),
                     "urn": entry.get("urn") or URN_PATTERNS[key].format(entry["id"]), "slot": slot}
        if entry.get("ip"):
            sea[kind]["ip"] = entry["ip"]
        if entry.get("engine"):
            sea[kind]["engine"] = entry["engine"]
    fn = state.get("functions")
    if usable(fn):
        sea["functions"] = {"name": fn["name"], "namespace_id": fn["id"], "region": fn.get("region"),
                            "urn": fn.get("urn") or FUNCTIONS_URN.format(fn["id"]), "slot": 8,
                            **({"url": fn["url"]} if fn.get("url") else {})}
    bucket = state.get("spaces_bucket")
    if usable(bucket):
        sea["spaces"] = {"name": bucket["id"], "region": bucket.get("region")}
    registry = state.get("registry")
    if usable(registry):
        sea["registry"] = {"name": registry["name"], "created": bool(registry.get("created"))}
    agent = state.get("agent") or {}
    if agent.get("status") in ("created", "paused"):
        sea["agent"] = {"kind": "harness-runtime", "name": agent["name"], "region": agent.get("region")}
    return sea


def env_text(fleet: dict) -> str:
    return ("# Written by infra/fleet.py from infra/out/state.json. Ids, IPs, URNs and names only.\n"
            f"PORTHOLE_FLEET_JSON={json.dumps(fleet, separators=(',', ':'))}\n")


def summary(fleet: dict) -> str:
    return (f"{len(fleet['tentacles'])} tentacles, {len(fleet['sea'])} sea members, "
            f"{len(fleet['watcher']['rules'])} alert rules")


def ensure_fleet(ctx: Context) -> None:
    for key, pattern in URN_PATTERNS.items():  # sea resources that had not reported yet may have by now
        entry = ctx.state.get(key)
        if usable(entry):
            note_urn(ctx, key, entry.get("region") or "tor1", pattern.format(entry["id"]))
    fleet = build_fleet(ctx.state)
    path = ctx.out_dir / ENV_FILE
    write_text(path, env_text(fleet), ctx.state.secrets, dry_run=ctx.dry_run)
    ctx.say(f"{'would write' if ctx.dry_run else 'wrote'} {rel(path)} ({summary(fleet)})")
    app = ctx.require("app", "app")
    if not usable(app):
        ctx.say("would set PORTHOLE_FLEET_JSON on the app once it exists")
        return
    current = ctx.api.get(f"/v2/apps/{app['id']}")["app"]
    spec = current["spec"]
    env = next((e for e in app_envs(spec) if e.get("key") == "PORTHOLE_FLEET_JSON"), None)
    if env is None:
        raise StepError(f"app {app['name']} has no PORTHOLE_FLEET_JSON variable in its spec")
    try:
        same = json.loads(env.get("value") or "{}") == fleet
    except ValueError:
        same = False
    if same:
        ctx.say(f"exists PORTHOLE_FLEET_JSON on app {app['name']} (up to date)")
    else:
        before = (latest_deployment(ctx, app["id"]) or {}).get("id")
        env.update(value=json.dumps(fleet, separators=(",", ":")), type="GENERAL")
        # The spec comes back with SECRET values encrypted; sending them back unchanged keeps them as they are.
        if ctx.api.put(f"/v2/apps/{app['id']}", {"spec": spec},
                       note=f"set PORTHOLE_FLEET_JSON on app {app['name']}") is None:
            return
        ctx.say(f"updated PORTHOLE_FLEET_JSON on app {app['name']}; waiting for the redeploy")
        wait_deployment(ctx, app["id"], before)
    if app.get("url"):
        verify(ctx, app["url"], fleet)


def verify(ctx: Context, url: str, fleet: dict) -> None:
    """GET /healthz and /api/fleet on the app and compare the tentacle count with the fleet JSON."""
    def fetch(path: str) -> dict | None:
        try:
            resp = ctx.web.get(f"{url}{path}", timeout=10)
            return resp.json() if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    health = ctx.wait(lambda: fetch("/healthz"), ctx.timeouts.check, f"{url}/healthz to answer 200")
    ctx.say(f"checked {url}/healthz: status {health.get('status')}, version {health.get('version')}")
    snapshot = ctx.wait(lambda: fetch("/api/fleet"), ctx.timeouts.check, f"{url}/api/fleet to answer 200")
    listed = snapshot.get("tentacles") or []
    reachable = sum(1 for t in listed if t.get("reachable"))
    ctx.say(f"checked {url}/api/fleet: {len(listed)} tentacles, {reachable} reachable")
    if len(listed) != len(fleet["tentacles"]):
        raise StepError(f"{url}/api/fleet lists {len(listed)} tentacles but the fleet JSON has "
                        f"{len(fleet['tentacles'])}; the new value is not live yet")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write infra/out/porthole.env with PORTHOLE_FLEET_JSON, built "
                                     "from infra/out/state.json. No token needed and no API call.")
    parser.add_argument("--print", action="store_true", help="also print the JSON")
    args = parser.parse_args(argv)
    state = State.load(OUT / "state.json", os.environ)
    if not state.resources:
        print("error: infra/out/state.json is missing or empty; run infra/provision.py first", file=sys.stderr)
        return 1
    fleet = build_fleet(state)
    write_text(OUT / ENV_FILE, env_text(fleet), state.secrets)
    print(f"wrote {rel(OUT / ENV_FILE)} ({summary(fleet)})")
    print("set it on the app with: python infra/provision.py --only fleet")
    if args.print:
        print(json.dumps(fleet, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
