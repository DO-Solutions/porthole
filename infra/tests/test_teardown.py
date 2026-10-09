"""teardown.py against the fake DigitalOcean API, after a full provisioning run.

Part of acceptance criterion 13: without --yes nothing is deleted, and with --yes only entries with created: true
are deleted, in reverse step order, with alert rules before the channels they notify."""
from __future__ import annotations

import json
from pathlib import Path

import steps
import teardown

DELETE = "DELETE"


def resources(out_dir: Path) -> dict:
    return json.loads((out_dir / "state.json").read_text())["resources"]


def expected_deletes(state: dict) -> list[str]:
    """The DELETE paths of a full teardown, in order, apart from the short-lived Spaces key."""
    ids = {key: entry["id"] for key, entry in state.items()}
    rules = [key for key in state if key.startswith("rule:")]
    return [*(f"/v2/insights/alert-rules/{ids[k]}" for k in reversed(rules)),
            f"/v2/insights/notification-channels/{ids['channel:kraken-email']}",
            f"/v2/insights/notification-channels/{ids['channel:kraken-head']}",
            f"/v2/apps/{ids['app']}",
            f"/v2/spaces/keys/{ids['spaces_key']}",
            f"/v2/kubernetes/clusters/{ids['doks']}",
            f"/v2/load_balancers/{ids['lb']}",
            *(f"/v2/droplets/{ids[f'droplet:kraken-tentacle-{n}']}" for n in (3, 2, 1)),
            f"/v2/functions/namespaces/{ids['functions']}",
            f"/v2/databases/{ids['database']}",
            f"/v2/reserved_ips/{ids['reserved_ip:kraken-tentacle-2']}",
            f"/v2/reserved_ips/{ids['reserved_ip:kraken-tentacle-1']}",
            f"/v2/firewalls/{ids['firewall']}",
            f"/v2/vpcs/{ids['vpc:syd1']}",
            "/v2/tags/kraken-tentacle", "/v2/tags/insights-demo",
            f"/v2/projects/{ids['project']}"]


def test_without_yes_nothing_is_deleted_and_no_call_is_made(provisioned, run, out_dir: Path) -> None:
    before = (out_dir / "state.json").read_text()
    created = [entry for entry in resources(out_dir).values() if entry["created"]]
    result = run("teardown")
    assert result.code == 0, result.err
    assert provisioned.calls == [] and provisioned.web_calls == []
    assert (out_dir / "state.json").read_text() == before
    listed = [line for line in result.out.splitlines() if line.startswith("would delete ")]
    assert len(listed) == len(created) == len(expected_deletes(resources(out_dir))) + 1  # + the bucket (S3)
    assert listed[0].startswith("would delete alert rule kraken load not zero")
    assert listed[-1].startswith("would delete project insights-demo")
    assert "would keep registry solutions-team: not created by provision.py" in result.out
    assert "would keep VPC default-tor1" in result.out
    assert "nothing was deleted" in result.out


def test_listing_needs_no_token_but_deleting_does(provisioned, run, env: dict) -> None:
    del env["DIGITALOCEAN_TOKEN"]
    assert run("teardown", env=env).code == 0
    result = run("teardown", "--yes", env=env)
    assert result.code == 2 and "DIGITALOCEAN_TOKEN is not set" in result.err
    assert provisioned.calls == []


def test_yes_deletes_only_created_entries_in_reverse_order(provisioned, run, out_dir: Path) -> None:
    state = resources(out_dir)
    result = run("teardown", "--yes")
    assert result.code == 0, result.err
    deletes = [path for method, path in provisioned.calls if method == DELETE]
    temporary = [p for p in deletes if p.startswith("/v2/spaces/keys/") and not p.endswith(state["spaces_key"]["id"])]
    assert len(temporary) == 1  # the short-lived key that signed the bucket DELETE
    assert [p for p in deletes if p not in temporary] == expected_deletes(state)
    bucket = state["spaces_bucket"]["id"]
    assert (DELETE, f"https://tor1.digitaloceanspaces.com/{bucket}") in provisioned.web_calls
    assert "/v2/registry" not in deletes and f"/v2/vpcs/{state['vpc:tor1']['id']}" not in deletes
    assert provisioned.registry is not None and provisioned.items["vpcs"][state["vpc:tor1"]["id"]]
    assert set(resources(out_dir)) == {"vpc:tor1", "registry", "agent"}
    assert all(not entry["created"] for entry in resources(out_dir).values())


def test_rules_go_before_the_channels_they_notify(provisioned, run) -> None:
    assert run("teardown", "--yes").code == 0
    deletes = [path for method, path in provisioned.calls if method == DELETE]
    last_rule = max(i for i, p in enumerate(deletes) if "/alert-rules/" in p)
    first_channel = min(i for i, p in enumerate(deletes) if "/notification-channels/" in p)
    assert last_rule < first_channel
    assert provisioned.items["insights/notification-channels"] == {}


def test_a_resource_already_gone_is_skipped_and_dropped(provisioned, run, out_dir: Path) -> None:
    gone = resources(out_dir)["droplet:kraken-tentacle-2"]["id"]
    provisioned.remove("droplets", provisioned.items["droplets"][str(gone)], str(gone))  # deleted by hand
    result = run("teardown", "--yes")
    assert result.code == 0, result.err
    assert f"already gone Droplet kraken-tentacle-2 (id {gone})" in result.out
    assert (DELETE, f"/v2/droplets/{gone}") not in provisioned.calls
    assert ("GET", f"/v2/droplets/{gone}") in provisioned.calls
    assert "droplet:kraken-tentacle-2" not in resources(out_dir)


def test_a_failed_delete_keeps_its_entry_and_exits_1(provisioned, run, out_dir: Path) -> None:
    database = resources(out_dir)["database"]["id"]
    provisioned.fail[(DELETE, f"/v2/databases/{database}")] = 409
    result = run("teardown", "--yes")
    assert result.code == 1
    assert f"failed to delete database cluster kraken-pg (id {database})" in result.err
    assert "Traceback" not in result.err
    left = resources(out_dir)
    assert "database" in left and "project" in left  # the project still holds the database, so it stays too
    assert "droplet:kraken-tentacle-1" not in left and "firewall" not in left


def test_the_delete_order_is_the_reverse_of_the_step_order(provisioned, out_dir: Path) -> None:
    order = [step.name for step in steps.STEPS]
    step_of = {entry["kind"]: entry["step"] for entry in resources(out_dir).values()}
    assert set(step_of) <= set(teardown.DELETE_ORDER)
    ranks = [order.index(step_of[kind]) for kind in teardown.DELETE_ORDER if kind in step_of]
    assert ranks == sorted(ranks, reverse=True)


def test_nothing_to_delete_without_a_state_file(run, world) -> None:
    result = run("teardown", "--yes")
    assert result.code == 0 and "nothing to delete" in result.out and world.calls == []
