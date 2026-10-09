"""Step 6 of design section 9.2: the load balancer kraken-lb in tor1, HTTP 80 to port 8800 on tentacle-1 and -2.

Once it exists, the tentacles' firewall gets its port 80 rule with the load balancer as the source."""
from __future__ import annotations

from functools import partial

from doapi import is_placeholder
from steps.common import Context, find_named, note_urn
from steps.network import FIREWALL

NAME, REGION = "kraken-lb", "tor1"
BACKENDS = ("kraken-tentacle-1", "kraken-tentacle-2")


def lb_body(droplet_ids: list, vpc_id: str) -> dict:
    body = {"name": NAME, "region": REGION, "size_unit": 1, "droplet_ids": droplet_ids,
            "forwarding_rules": [{"entry_protocol": "http", "entry_port": 80,
                                  "target_protocol": "http", "target_port": 8800}],
            "health_check": {"protocol": "http", "port": 8800, "path": "/health", "check_interval_seconds": 10,
                             "response_timeout_seconds": 5, "healthy_threshold": 3, "unhealthy_threshold": 3}}
    if not is_placeholder(vpc_id):
        body["vpc_uuid"] = vpc_id
    return body


def ensure_lb(ctx: Context) -> None:
    ids = [ctx.require(f"droplet:{name}", "droplets")["id"] for name in BACKENDS]
    vpc = ctx.require("vpc:tor1", "network")
    found = find_named(ctx.api.paginate("/v2/load_balancers", "load_balancers"), NAME)
    lb, entry = ctx.resource("lb", "load_balancer", NAME, found,
                             partial(ctx.create, "/v2/load_balancers", lb_body(ids, vpc["id"]), "load_balancer",
                                     f"create load balancer {NAME}"), region=REGION)
    if is_placeholder(lb["id"]):
        ctx.say(f"would add a firewall rule to {FIREWALL}: port 80 from {NAME}")
        return
    lb = ctx.wait(partial(_active, ctx, lb["id"]), ctx.timeouts.load_balancer, f"{NAME} to be active")
    ctx.state.update("lb", ip=lb.get("ip"))
    missing = [i for i in ids if i not in (lb.get("droplet_ids") or []) and not is_placeholder(i)]
    if missing and ctx.api.post(f"/v2/load_balancers/{lb['id']}/droplets", {"droplet_ids": missing},
                                note=f"add Droplets {missing} to {NAME}") is not None:
        ctx.say(f"added Droplets {missing} to {NAME}")
    note_urn(ctx, "lb", REGION, f"do:loadbalancer:{lb['id']}")
    if entry["created"]:
        ctx.assign(f"do:loadbalancer:{lb['id']}")
    _firewall_rule(ctx, lb["id"])


def _active(ctx: Context, lb_id: str) -> dict | None:
    lb = ctx.api.get(f"/v2/load_balancers/{lb_id}")["load_balancer"]
    return lb if lb.get("status") == "active" and lb.get("ip") else None


def _firewall_rule(ctx: Context, lb_id: str) -> None:
    # Design section 9.2 asks for "80 from the LB". The load balancer forwards to port 8800, which is already open
    # to anyone, so this rule carries no traffic today; it stays because the design lists it.
    firewall = ctx.require("firewall", "network")
    if is_placeholder(firewall["id"]):
        ctx.say(f"would add a firewall rule to {FIREWALL}: port 80 from {NAME}")
        return
    current = ctx.api.get(f"/v2/firewalls/{firewall['id']}")["firewall"]
    for rule in current.get("inbound_rules") or []:
        if rule.get("ports") == "80" and lb_id in ((rule.get("sources") or {}).get("load_balancer_uids") or []):
            ctx.say(f"exists firewall rule on {FIREWALL}: port 80 from {NAME}")
            return
    rule = {"protocol": "tcp", "ports": "80", "sources": {"load_balancer_uids": [lb_id]}}
    answer = ctx.api.post(f"/v2/firewalls/{firewall['id']}/rules", {"inbound_rules": [rule]},
                          note=f"add a firewall rule to {FIREWALL}: port 80 from {NAME}")
    if answer is not None:
        ctx.say(f"added firewall rule on {FIREWALL}: port 80 from {NAME}")
