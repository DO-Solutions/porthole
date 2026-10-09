"""Step 7 of design section 9.2: the managed Postgres kraken-pg with database "kraken" and user "tentacle".

It runs before the Droplets because their user_data carries the DSN, whose password is read from the API only
while that user_data is built and never reaches state.json or the screen."""
from __future__ import annotations

from functools import partial
from urllib.parse import quote

from doapi import is_placeholder
from steps.common import TAG, Context, find_named, note_urn

NAME, DB, USER, REGION = "kraken-pg", "kraken", "tentacle", "tor1"


def ensure_database(ctx: Context) -> None:
    vpc = ctx.require("vpc:tor1", "network")
    found = find_named(ctx.api.paginate("/v2/databases", "databases", {"tag_name": TAG}), NAME)
    ctx.hide(found, "the database cluster")
    body = {"name": NAME, "engine": "pg", "region": REGION, "size": "db-s-1vcpu-1gb", "num_nodes": 1, "tags": [TAG]}
    if not is_placeholder(vpc["id"]):
        body["private_network_uuid"] = vpc["id"]
    cluster, entry = ctx.resource("database", "database", NAME, found,
                                  partial(ctx.create, "/v2/databases", body, "database",
                                          f"create database cluster {NAME}"),
                                  region=REGION, engine="pg", db=DB, user=USER)
    ctx.hide(cluster, "the database cluster")
    if is_placeholder(cluster["id"]):
        ctx.say(f"would create database {DB} and user {USER} in {NAME} once it is online")
        return
    base = f"/v2/databases/{cluster['id']}"
    ctx.wait(partial(_online, ctx, base), ctx.timeouts.database, f"{NAME} to be online")
    _child(ctx, base, "dbs", "database", DB)
    # UNVERIFIED: that a user created through the API may CREATE TABLE in the public schema of "kraken"; on
    # Postgres 15 and later only the owner (doadmin) may, unless granted (BUGS.md B-018).
    _child(ctx, base, "users", "user", USER)
    note_urn(ctx, "database", REGION, f"do:dbaas:{cluster['id']}")
    if entry["created"]:
        ctx.assign(f"do:dbaas:{cluster['id']}")


def _online(ctx: Context, base: str) -> dict | None:
    cluster = ctx.api.get(base)["database"]
    ctx.hide(cluster, "the database cluster")
    return cluster if cluster.get("status") == "online" else None


def _child(ctx: Context, base: str, collection: str, label: str, name: str) -> None:
    """A database or a user inside the cluster; both go away with the cluster, so state does not track them."""
    answer = ctx.api.get(f"{base}/{collection}/{name}", missing_ok=True)
    ctx.hide(answer, f"the {label} {name}")
    if answer:
        ctx.say(f"exists {label} {name} in {NAME}")
        return
    created = ctx.api.post(f"{base}/{collection}", {"name": name}, note=f"create {label} {name} in {NAME}")
    ctx.hide(created, f"the {label} {name}")
    if created is not None:
        ctx.say(f"created {label} {name} in {NAME}")


def dsn(ctx: Context) -> str:
    """postgresql://tentacle:<password>@host:port/kraken?sslmode=require, read now and never stored or printed."""
    entry = ctx.require("database", "database")
    if is_placeholder(entry["id"]):
        return ""
    base = f"/v2/databases/{entry['id']}"
    cluster = ctx.api.get(base)["database"]
    ctx.hide(cluster, "the database cluster")
    user = ctx.api.get(f"{base}/users/{USER}")["user"]
    password = user.get("password") or ""
    ctx.state.add_secret("the database password", password)
    conn = cluster.get("connection") or {}
    url = f"postgresql://{USER}:{quote(password, safe='')}@{conn.get('host')}:{conn.get('port')}/{DB}?sslmode=require"
    ctx.state.add_secret("the database DSN", url)
    return url
