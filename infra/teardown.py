"""Deletes what provision.py created, in reverse step order, and nothing else (design section 9.3).

Without --yes it lists what it would delete and makes no API call; with --yes it re-reads each resource, deletes it
and drops its entry from state.json once DigitalOcean confirms, and it never touches entries with created: false."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx

from doapi import APIError, DOClient, WaitTimeout
from state import SecretLeak, State
from steps import agent, spaces
from steps.common import LABELS, Context, RunResult, StepError, Timeouts, insights_module, last_line, rel, run_command

OUT = Path(__file__).resolve().parent / "out"
# Reverse step order (steps/__init__.py), and inside a step the order that dependencies need: rules before the
# channels they notify, the bucket before its key, reserved IPs and the firewall before the VPC, tags before the
# project, which must be empty when it is deleted.
DELETE_ORDER = ("insights_rule", "insights_channel", "app", "agent", "registry", "spaces_bucket", "spaces_key",
                "kubernetes", "load_balancer", "droplet", "functions_namespace", "database", "reserved_ip",
                "firewall", "vpc", "tag", "project")
PATHS = {"app": "/v2/apps/{id}", "registry": "/v2/registry", "spaces_key": "/v2/spaces/keys/{id}",
         "kubernetes": "/v2/kubernetes/clusters/{id}", "load_balancer": "/v2/load_balancers/{id}",
         "droplet": "/v2/droplets/{id}", "functions_namespace": "/v2/functions/namespaces/{id}",
         "database": "/v2/databases/{id}", "reserved_ip": "/v2/reserved_ips/{id}", "firewall": "/v2/firewalls/{id}",
         "vpc": "/v2/vpcs/{id}", "tag": "/v2/tags/{id}", "project": "/v2/projects/{id}"}


def plan(state: State) -> list[tuple[str, dict]]:
    """The entries with created: true in the order they must be deleted; among entries of the same kind, the one
    recorded last goes first."""
    rank = {kind: i for i, kind in enumerate(DELETE_ORDER)}
    created = [(i, key, e) for i, (key, e) in enumerate(state.resources.items()) if e.get("created")]
    created.sort(key=lambda item: (rank.get(item[2].get("kind"), -1), -item[0]))
    return [(key, e) for _, key, e in created]


def describe(entry: dict) -> str:
    kind, name, ident = entry.get("kind"), entry.get("name"), entry.get("id")
    text = f"{LABELS.get(kind, kind)} {'for ' if kind == 'reserved_ip' else ''}{name}"
    return f"{text} (id {ident})" if ident is not None and str(ident) != name else text


def delete_one(ctx: Context, entry: dict) -> bool:
    """Re-read the resource and delete it. True when DigitalOcean confirmed, False when it was already gone."""
    kind, ident = entry.get("kind"), entry.get("id")
    if kind in ("insights_rule", "insights_channel"):
        return _delete_insights(ctx, entry)
    if kind == "spaces_bucket":
        return _delete_bucket(ctx, ident)
    if kind == "agent":
        return _delete_agent(ctx, entry)
    if kind not in PATHS:
        raise StepError(f"teardown does not know how to delete a {kind}")
    path = PATHS[kind].format(id=ident)
    current = ctx.api.get(path, missing_ok=True)
    if current is None or (kind == "registry" and (current.get("registry") or {}).get("name") != ident):
        return False
    if kind == "reserved_ip" and (current.get("reserved_ip") or {}).get("droplet"):
        answer = ctx.api.post(f"{path}/actions", {"type": "unassign"}, note=f"unassign reserved IP {ident}")
        ctx.api.wait_action((answer.get("action") or {}).get("id"), ctx.timeouts.action)
    elif kind in ("vpc", "project"):
        sub, what = ("members", "to have no members") if kind == "vpc" else ("resources", "to be empty")
        ctx.wait(lambda: not ctx.api.paginate(f"{path}/{sub}", sub), ctx.timeouts.drain, f"{describe(entry)} {what}")
    return ctx.api.delete(path, note=f"delete {describe(entry)}")


def _delete_insights(ctx: Context, entry: dict) -> bool:
    harness = insights_module()
    ins = ctx.insights()
    rule = entry["kind"] == "insights_rule"
    read, delete = (ins.get_rule, ins.delete_rule) if rule else (ins.get_channel, ins.delete_channel)
    try:
        read(entry["id"])
        delete(entry["id"])
    except harness.InsightsError as e:
        if e.status == 404:
            return False
        raise StepError(f"HTTP {e.status} from {e.request_summary}: {harness.excerpt(e.body, 200)}") from None
    except httpx.HTTPError as e:
        raise StepError(f"{type(e).__name__}: {e}") from None
    return True


def _delete_bucket(ctx: Context, bucket: str) -> bool:
    """The S3 API needs a signature, and no key secret is ever kept, so a short-lived key signs the DELETE."""
    if not spaces.bucket_exists(ctx, bucket):
        return False
    with spaces.temporary_key(ctx) as (access_key, secret_key):
        status = spaces.s3(ctx, "DELETE", bucket, access_key, secret_key)
    if status not in (200, 204, 404):
        raise StepError(f"S3 DELETE answered HTTP {status}; a bucket must be empty before it can be deleted")
    return status != 404


def _delete_agent(ctx: Context, entry: dict) -> bool:
    group = agent.doctl_help(ctx)
    verb = next((v for v in ("delete", "cancel") if agent.has_verb(group, v)), None)
    if verb is None:
        raise StepError("doctl here cannot delete Harness Runtime sessions; delete kraken-brain in the control "
                        "panel, then remove the agent entry from state.json")
    result = ctx.run(["doctl", "harness-runtime", verb, str(entry["id"])], ctx.doctl_env())
    if result.returncode:
        raise StepError(f"doctl harness-runtime {verb} failed: {last_line(result.stderr)}")
    return True


def kept_reason(entry: dict) -> str:
    return "never created (pending)" if entry.get("status") == "pending" else "not created by provision.py"


def main(argv: list[str] | None = None, *, env: Mapping[str, str] | None = None,
         transport: httpx.BaseTransport | None = None, web_transport: httpx.BaseTransport | None = None,
         runner: Callable[..., RunResult] = run_command, which: Callable[[str], str | None] = shutil.which,
         sleep: Callable[[float], None] = time.sleep, out_dir: Path = OUT, timeouts: Timeouts | None = None) -> int:
    """The CLI. The keyword arguments are for tests: fake transports, a fake runner and a sleep that returns."""
    parser = argparse.ArgumentParser(prog="teardown.py", description="Delete what provision.py created, in "
                                     "reverse order. Without --yes nothing is deleted and no API call is made.")
    parser.add_argument("--yes", action="store_true", help="delete the resources listed; without it they are "
                        "only printed")
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    path = out_dir / "state.json"
    if not path.exists():
        print(f"nothing to delete: {rel(path)} does not exist")
        return 0
    state = State.load(path, env)
    victims = plan(state)
    kept = [e for e in state.resources.values() if not e.get("created")]
    if not args.yes:
        for _, entry in victims:
            print(f"would delete {describe(entry)}")
        for entry in kept:
            print(f"would keep {describe(entry)}: {kept_reason(entry)}")
        print(f"nothing was deleted; run teardown.py --yes to delete the {len(victims)} resources above"
              if victims else "nothing to delete")
        return 0
    token = (env.get("DIGITALOCEAN_TOKEN") or "").strip()
    if not token:
        print("error: DIGITALOCEAN_TOKEN is not set (needed by teardown.py --yes)", file=sys.stderr)
        return 2
    ctx = Context(api=DOClient(token, transport=transport, sleep=sleep), state=state, env=env,
                  web=httpx.Client(transport=web_transport, timeout=10), out_dir=out_dir, which=which, run=runner,
                  insights_transport=transport, timeouts=timeouts or Timeouts(), step="teardown")
    failed = 0
    try:
        for key, entry in victims:
            try:
                deleted = delete_one(ctx, entry)
                state.remove(key)
            except (APIError, StepError, WaitTimeout, SecretLeak) as e:
                failed += 1
                print(f"failed to delete {describe(entry)}: {e}", file=sys.stderr)
                continue
            print(f"{'deleted' if deleted else 'already gone'} {describe(entry)}")
    finally:
        ctx.close()
    for entry in kept:
        print(f"kept {describe(entry)}: {kept_reason(entry)}")
    if failed:
        print(f"{failed} resources were not deleted and stay in state.json; run teardown.py --yes again",
              file=sys.stderr)
        return 1
    print("teardown finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())
