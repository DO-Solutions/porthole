"""Steps 3 and 4 of design section 9.2: a VPC per region, the tentacles' firewall and two reserved IPs.

A region uses kraken-<region> if it exists, else its default VPC (created: false), else a new kraken-<region>; the
reserved IPs exist before the Droplets so tentacle-1 and -2 can get each other's address at first boot (PEER_URL)."""
from __future__ import annotations

from functools import partial

from doapi import is_placeholder
from steps.common import TENTACLE_TAG, TENTACLES, Context, find_named

REGIONS = ("tor1", "syd1")
FIREWALL = "kraken-tentacles"
ANYWHERE = ["0.0.0.0/0", "::/0"]


def firewall_body(ssh_cidrs: str) -> dict:
    """Inbound 8800 from anywhere (App Platform has no fixed egress IP) and 22 only from SSH_ALLOW_CIDRS when it is
    set; outbound open. The lb step adds port 80 from the load balancer once that exists."""
    inbound = [{"protocol": "tcp", "ports": "8800", "sources": {"addresses": ANYWHERE}}]
    cidrs = [c.strip() for c in ssh_cidrs.split(",") if c.strip()]
    if cidrs:
        inbound.insert(0, {"protocol": "tcp", "ports": "22", "sources": {"addresses": cidrs}})
    outbound = [{"protocol": p, "ports": "1-65535", "destinations": {"addresses": ANYWHERE}} for p in ("tcp", "udp")]
    outbound.append({"protocol": "icmp", "destinations": {"addresses": ANYWHERE}})
    return {"name": FIREWALL, "inbound_rules": inbound, "outbound_rules": outbound, "tags": [TENTACLE_TAG]}


def ensure_network(ctx: Context) -> None:
    vpcs = ctx.api.paginate("/v2/vpcs", "vpcs")
    for region in REGIONS:
        name = f"kraken-{region}"
        here = [v for v in vpcs if v.get("region") == region]
        found = find_named(here, name) or next((v for v in here if v.get("default")), None)
        body = {"name": name, "region": region, "description": "Porthole kraken fleet"}
        ctx.resource(f"vpc:{region}", "vpc", found["name"] if found else name, found,
                     partial(ctx.create, "/v2/vpcs", body, "vpc", f"create VPC {name}"),
                     region=region, default=bool(found and found.get("default")))
    ctx.require(f"tag:{TENTACLE_TAG}", "project")  # the firewall targets this tag
    found = find_named(ctx.api.paginate("/v2/firewalls", "firewalls"), FIREWALL)
    body = firewall_body(ctx.opt("SSH_ALLOW_CIDRS"))
    ctx.resource("firewall", "firewall", FIREWALL, found,
                 partial(ctx.create, "/v2/firewalls", body, "firewall", f"create firewall {FIREWALL}"))
    for tentacle in TENTACLES:
        if tentacle.peer:
            _reserved_ip(ctx, tentacle.name, tentacle.region)


def _reserved_ip(ctx: Context, name: str, region: str) -> None:
    key = f"reserved_ip:{name}"
    old = ctx.state.get(key)
    found = None
    if old and not is_placeholder(old["id"]):
        answer = ctx.api.get(f"/v2/reserved_ips/{old['id']}", missing_ok=True)
        found = answer and answer.get("reserved_ip")
    if not found:  # no usable state: an IP already on the Droplet of that name is the one to keep
        found = next((r for r in ctx.api.paginate("/v2/reserved_ips", "reserved_ips")
                      if (r.get("droplet") or {}).get("name") == name), None)
    body = {"region": region}
    project = ctx.state.get("project")
    if project and not is_placeholder(project["id"]):
        body["project_id"] = project["id"]
    ctx.resource(key, "reserved_ip", name, found,
                 partial(ctx.create, "/v2/reserved_ips", body, "reserved_ip", f"create reserved IP for {name}", "ip"),
                 id_field="ip", shown=f"for {name}", region=region)
