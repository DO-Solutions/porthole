"""provision.py against the fake DigitalOcean API (plan, full run, second run, dry runs, --only) and the client.

Part of acceptance criterion 13: --plan prints the order of design section 9.2 without any API call, and a second
run against the fake creates nothing."""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

import provision
import steps
from doapi import APIError, DOClient, WaitTimeout
from state import check_text
from steps.app import load_spec
from steps.common import StepError
from steps.droplets import user_data
from steps.insights import templates

REPO = Path(__file__).resolve().parents[2]
REAL_ORDER = [1, 2, 3, 4, 7, 9, 5, 6, 8, 10, 11, 12, 13, 14, 15]
PURPOSES = [template["purpose"] for template in templates()]  # watcher/alerts/*.json


def resources(out_dir: Path) -> dict:
    return json.loads((out_dir / "state.json").read_text())["resources"]


def test_plan_prints_the_real_order_without_any_api_call(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def no_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("--plan must not create an API client")

    def no_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"--plan sent {request.method} {request.url}")

    monkeypatch.setattr(provision, "DOClient", no_client)
    assert provision.main(["--plan"], env={}, transport=httpx.MockTransport(no_request)) == 0
    rows = re.findall(r"^\s*(\d+)\s+(\d+)\s+(\w+)\s", capsys.readouterr().out, re.MULTILINE)
    assert [int(design) for _, design, _ in rows] == REAL_ORDER
    assert [int(order) for order, _, _ in rows] == list(range(1, 16))
    assert [name for _, _, name in rows][4:7] == ["database", "functions", "droplets"]


def test_a_full_run_creates_each_resource_once(run, world, out_dir: Path) -> None:
    result = run("provision")
    assert result.code == 0, result.err
    expected = {"/v2/projects": 1, "/v2/tags": 2, "/v2/vpcs": 1, "/v2/firewalls": 1, "/v2/reserved_ips": 2,
                "/v2/databases": 1, "/v2/functions/namespaces": 1, "/v2/droplets": 3, "/v2/load_balancers": 1,
                "/v2/kubernetes/clusters": 1, "/v2/spaces/keys": 1, "/v2/registry": 0, "/v2/apps": 1,
                "/v2/insights/notification-channels": 2, "/v2/insights/alert-rules": len(PURPOSES)}
    assert {path: world.count("POST", path) for path in expected} == expected
    assert len(world.buckets) == 1
    state = resources(out_dir)
    assert (state["vpc:tor1"]["created"], state["vpc:tor1"]["default"]) == (False, True)
    assert state["vpc:syd1"]["created"] is True and state["registry"]["created"] is False
    assert state["lb"]["urn_verified"] is True and state["database"]["urn_verified"] is False
    assert state["database"]["urn"] == f"do:dbaas:{state['database']['id']}"
    t1 = state["droplet:kraken-tentacle-1"]
    assert t1["urn"] == f"do:droplet:{t1['id']}" and t1["created"] is True
    for name in ("kraken-tentacle-1", "kraken-tentacle-2"):  # each reserved IP ends up on its own Droplet
        ip = state[f"reserved_ip:{name}"]["id"]
        assert world.items["reserved_ips"][ip]["droplet"]["id"] == state[f"droplet:{name}"]["id"]
    assert "reserved_ip:kraken-tentacle-3" not in state
    assert (state["functions"]["deploy"], state["doks"]["manifest"], state["agent"]["status"]) == \
        ("pending", "pending", "pending")
    project = world.project_urns[state["project"]["id"]]
    assert sorted(u.split(":")[1] for u in project) == ["app", "dbaas", "droplet", "droplet", "droplet",
                                                         "kubernetes", "loadbalancer", "space"]
    for line in ("created Droplet kraken-tentacle-3", "doctl serverless deploy infra/functions",
                 "kubectl apply -f infra/k8s/kraken.yaml", "doctl harness-runtime create --name kraken-brain"):
        assert line in result.out


def test_a_second_run_creates_nothing(provisioned, run, runner) -> None:
    result = run("provision")
    assert result.code == 0, result.err
    assert provisioned.mutations() == []
    assert [c for c in provisioned.web_calls if c[0] not in ("GET", "HEAD")] == []
    assert runner.calls == []
    assert "\ncreated " not in result.out
    assert "exists Droplet kraken-tentacle-1" in result.out
    assert "exists PORTHOLE_FLEET_JSON on app porthole (up to date)" in result.out
    rule_reads = [p for m, p in provisioned.calls if m == "GET" and p.startswith("/v2/insights/alert-rules/")]
    assert len(rule_reads) == len(PURPOSES) and ("GET", "/v2/insights/alert-rules") not in provisioned.calls


def test_a_rule_that_disappeared_is_found_by_id_and_created_again(provisioned, run, out_dir: Path) -> None:
    gone = resources(out_dir)["rule:operator-ne-1h"]["id"]
    del provisioned.items["insights/alert-rules"][gone]
    result = run("provision", "--only", "insights")
    assert result.code == 0, result.err
    assert provisioned.mutations() == [("POST", "/v2/insights/alert-rules")]
    assert resources(out_dir)["rule:operator-ne-1h"]["id"] != gone


def test_a_dry_run_on_an_empty_account_sends_no_mutation(run, world, runner, out_dir: Path) -> None:
    runner.tools = {"doctl", "kubectl"}
    result = run("provision", "--dry-run")
    assert result.code == 0, result.err
    assert world.mutations() == []
    assert [c for c in world.web_calls if c[0] not in ("GET", "HEAD")] == []
    assert runner.calls == []
    assert not out_dir.exists()
    for line in ("would create Droplet kraken-tentacle-1 in tor1 (POST /v2/droplets)",
                 "would create VPC kraken-syd1 (POST /v2/vpcs)", "would create alert rule kraken churn (active)",
                 "dry run finished: nothing was changed"):
        assert line in result.out


def test_a_dry_run_after_a_full_run_changes_nothing(provisioned, run, out_dir: Path) -> None:
    before = (out_dir / "state.json").read_text()
    result = run("provision", "--dry-run")
    assert result.code == 0, result.err
    assert provisioned.mutations() == []
    assert (out_dir / "state.json").read_text() == before


def test_only_runs_the_named_step(run, world) -> None:
    result = run("provision", "--only", "project")
    assert result.code == 0, result.err
    assert world.mutations() == [("POST", "/v2/projects"), ("POST", "/v2/tags"), ("POST", "/v2/tags")]
    assert "== project (design step 1, 2)" in result.out and "== network" not in result.out


def test_only_accepts_design_numbers_aliases_and_lists() -> None:
    assert [s.name for s in steps.select("5")] == ["droplets"]
    assert [s.name for s in steps.select("firewall, tag")] == ["project", "network"]
    with pytest.raises(ValueError, match="unknown step 'kraken'"):
        steps.select("kraken")


def test_only_with_an_unknown_step_is_a_usage_error(run, world) -> None:
    result = run("provision", "--only", "kraken")
    assert result.code == 2 and "unknown step 'kraken'" in result.err and world.calls == []


def test_a_step_without_its_prerequisites_names_the_step_to_run(run) -> None:
    result = run("provision", "--only", "droplets")
    assert result.code == 1
    assert "run the database step first" in result.err and "Traceback" not in result.err


@pytest.mark.parametrize("name", ["DIGITALOCEAN_TOKEN", "TENTACLE_KEY", "HEAD_TOKEN", "ALERT_EMAIL"])
def test_a_missing_variable_is_named_before_any_call(run, world, env: dict, name: str) -> None:
    del env[name]
    result = run("provision", env=env)
    assert result.code == 2
    assert f"{name} is not set" in result.err and "Traceback" not in result.err
    assert world.calls == []


def test_a_webhook_url_that_is_not_https_is_a_clear_error(run, env: dict) -> None:
    env["PUBLIC_URL"] = "http://porthole.example.test"
    result = run("provision", env=env)
    assert result.code == 1
    assert "error in step insights: Insights request not sent: webhook url must be https" in result.err
    assert "Traceback" not in result.err


def test_an_api_error_stops_the_run_with_a_short_message(run, world) -> None:
    world.fail[("POST", "/v2/droplets")] = 422
    result = run("provision")
    assert result.code == 1
    assert "error in step droplets: POST /v2/droplets -> HTTP 422: planted failure" in result.err
    assert "Traceback" not in result.err


def test_a_failed_optional_step_is_skipped_and_the_run_goes_on(run, world, out_dir: Path) -> None:
    world.fail[("POST", "/v2/spaces/keys")] = 500
    result = run("provision")
    assert result.code == 0, result.err
    assert "warning: optional step spaces failed and was skipped: POST /v2/spaces/keys -> HTTP 500" in result.err
    state = resources(out_dir)
    assert "spaces_bucket" not in state and state["app"]["created"] is True
    assert (out_dir / "porthole.env").exists()


def test_user_data_carries_the_variables_right_after_the_shebang(run, world, out_dir: Path) -> None:
    assert run("provision", "--only", "project,network,database,functions,droplets").code == 0
    bodies = {b["name"]: b for b in world.body("POST", "/v2/droplets")}
    state = resources(out_dir)
    lines = bodies["kraken-tentacle-1"]["user_data"].splitlines()
    assert lines[:2] == ["#!/bin/bash", "export TENTACLE_NAME=kraken-tentacle-1"]
    assert f"export PEER_URL=http://{state['reserved_ip:kraken-tentacle-2']['id']}:8800" in lines
    dsn = next(line for line in lines if line.startswith("export PG_DSN="))
    assert dsn.startswith("export PG_DSN='postgresql://tentacle:") and dsn.endswith("/kraken?sslmode=require'")
    assert "export FN_URL=https://faas-tor1-00000000.doserverless.co/api/v1/web/" in "\n".join(lines)
    assert not any(line.startswith("export PEER_URL") for line in bodies["kraken-tentacle-3"]["user_data"].split("\n"))
    body = bodies["kraken-tentacle-1"]
    assert (body["size"], body["image"], body["monitoring"]) == ("s-1vcpu-1gb", "ubuntu-24-04-x64", True)
    assert body["tags"] == ["insights-demo", "kraken-tentacle"] and body["ssh_keys"][0] == 123456
    assert body["vpc_uuid"] == state["vpc:tor1"]["id"]
    assert state["droplet:kraken-tentacle-1"]["ip"] != state["reserved_ip:kraken-tentacle-1"]["id"]


def test_firewall_opens_22_only_with_an_allow_list() -> None:
    from steps.network import firewall_body
    closed = firewall_body("")
    assert [r["ports"] for r in closed["inbound_rules"]] == ["8800"]
    assert closed["inbound_rules"][0]["sources"] == {"addresses": ["0.0.0.0/0", "::/0"]}
    opened = firewall_body("192.0.2.0/24, 198.51.100.7/32")
    assert opened["inbound_rules"][0] == {"protocol": "tcp", "ports": "22",
                                          "sources": {"addresses": ["192.0.2.0/24", "198.51.100.7/32"]}}
    assert opened["tags"] == ["kraken-tentacle"] and len(opened["outbound_rules"]) == 3


def test_user_data_needs_a_shebang_and_quotes_values() -> None:
    script = user_data("#!/bin/bash\nset -e\n", {"A": "x y", "B": "", "C": "plain"})
    assert script == "#!/bin/bash\nexport A='x y'\nexport C=plain\nset -e\n"
    with pytest.raises(StepError, match="shebang"):
        user_data("set -e\n", {"A": "x"})


def test_tools_on_path_deploy_apply_and_create_the_session(run, world, runner, out_dir: Path, planted) -> None:
    runner.tools = {"doctl", "kubectl"}
    result = run("provision")
    assert result.code == 0, result.err
    commands = [c[:3] for c in runner.calls]
    for cmd in (["doctl", "serverless", "connect"], ["doctl", "serverless", "deploy"],
                ["doctl", "harness-runtime", "create"], ["doctl", "harness-runtime", "pause"]):
        assert cmd in commands
    [(kubeconfig, text)] = runner.kubeconfigs
    assert "token:" in text and not kubeconfig.exists()
    state = resources(out_dir)
    assert (state["functions"]["deploy"], state["doks"]["manifest"]) == ("done", "applied")
    assert (state["agent"]["status"], state["agent"]["created"]) == ("paused", True)
    saved = (out_dir / "state.json").read_text()
    assert not [value for value in planted if value in saved]
    runner.calls.clear()
    world.calls.clear()
    assert run("provision").code == 0
    assert runner.calls == [] and world.mutations() == []


def test_the_fleet_json_has_the_shape_the_head_reads(provisioned, out_dir: Path) -> None:
    line = next(x for x in (out_dir / "porthole.env").read_text().splitlines() if x.startswith("PORTHOLE_FLEET_JSON="))
    fleet = json.loads(line.split("=", 1)[1])
    fixture = json.loads((REPO / "head" / "tests" / "fixtures" / "fleet.json").read_text())
    assert set(fleet) == set(fixture) and set(fleet["head"]) == set(fixture["head"])
    assert [set(t) for t in fleet["tentacles"]] == [set(t) for t in fixture["tentacles"]]
    assert set(fleet["sea"]) == set(fixture["sea"]) - {"agent"}  # the session is pending without doctl
    assert all(set(fleet["sea"][k]) == set(fixture["sea"][k]) for k in fleet["sea"])
    assert set(fleet["watcher"]) == set(fixture["watcher"])
    assert [r["purpose"] for r in fleet["watcher"]["rules"]] == PURPOSES
    state = resources(out_dir)
    t1, _, t3 = fleet["tentacles"]
    fn = fleet["sea"]["functions"]  # the label on every do_functions_* series (B-033)
    assert fn["namespace_id"] == state["functions"]["id"]
    assert fn["urn"] == f"do:functions_namespace:{fn['namespace_id']}"
    assert t1["url"] == f"http://{state['reserved_ip:kraken-tentacle-1']['id']}:8800"
    assert t3["url"] == f"http://{state['droplet:kraken-tentacle-3']['ip']}:8800" and t3["peer"] is None
    app = next(iter(provisioned.items["apps"].values()))
    value = next(e["value"] for e in app["spec"]["services"][0]["envs"] if e["key"] == "PORTHOLE_FLEET_JSON")
    assert json.loads(value) == fleet


def test_the_app_gets_the_head_token_never_the_work_token(provisioned, env: dict) -> None:
    app = next(iter(provisioned.items["apps"].values()))
    values = {e["key"]: e.get("value") for e in app["spec"]["services"][0]["envs"]}
    assert values["DIGITALOCEAN_TOKEN"] == env["HEAD_TOKEN"] != env["DIGITALOCEAN_TOKEN"]
    assert (values["PORTHOLE_CAPTAIN_KEY"], values["PORTHOLE_PUBLIC_URL"]) == (env["CAPTAIN_KEY"], env["PUBLIC_URL"])


def test_do_context_reaches_the_app_only_when_set(provisioned, env: dict) -> None:
    envs = next(iter(provisioned.items["apps"].values()))["spec"]["services"][0]["envs"]
    assert "PORTHOLE_DO_CONTEXT" not in {e["key"] for e in envs}

    class Ctx:  # load_spec reads variables through need() and opt() only
        def __init__(self, values: dict):
            self.values = values

        def need(self, name: str) -> str:
            return self.values[name]

        def opt(self, name: str, default: str = "") -> str:
            return self.values.get(name) or default

    spec = load_spec(Ctx({**env, "DO_CONTEXT": "00ab12"}))
    added = [e for e in spec["services"][0]["envs"] if e["key"] == "PORTHOLE_DO_CONTEXT"]
    assert added == [{"key": "PORTHOLE_DO_CONTEXT", "scope": "RUN_TIME", "value": "00ab12"}]


def test_channels_and_rules_follow_the_templates(run, world, env: dict, out_dir: Path) -> None:
    assert run("provision").code == 0
    hook, email = world.body("POST", "/v2/insights/notification-channels")
    assert hook["webhook"]["url"] == "https://porthole.example.test/hooks/insights"
    assert hook["webhook"]["bearer_token"] == {"token": env["HOOK_BEARER"]}
    assert hook["webhook"]["signature"] == {"secret": env["HOOK_SECRET"]}
    assert hook["webhook"]["headers"] == {"X-Kraken": "1"}
    assert email == {"name": "kraken-email", "email": {"to": "kraken@example.com"}}
    state = resources(out_dir)
    rules = world.body("POST", "/v2/insights/alert-rules")
    assert [r["status"] for r in rules].count("ALERT_RULE_STATUS_ACTIVE") == 1
    churn = rules[0]["spec"]
    assert churn["query"]["resource_urns"] == [state["droplet:kraken-tentacle-1"]["urn"]]
    assert churn["notification_channels"][0]["notification_channel_id"] == state["channel:kraken-head"]["id"]


def test_client_retries_429_and_5xx_with_backoff() -> None:
    answers = [httpx.Response(429, headers={"ratelimit-reset": "1760000009"}), httpx.Response(503),
               httpx.Response(429, headers={"retry-after": "40"}), httpx.Response(200, json={"ok": 1})]
    slept: list[float] = []
    client = DOClient("x" * 12, transport=httpx.MockTransport(lambda r: answers.pop(0)), sleep=slept.append,
                      clock=lambda: 1760000000.0)
    assert client.get("/v2/account") == {"ok": 1}
    assert slept == [9.0, 18.0, 40.0]  # ratelimit-reset, then doubling, then Retry-After when it is longer


def test_client_gives_up_with_a_short_error() -> None:
    error = httpx.Response(500, json={"id": "server_error", "message": "boom"})
    client = DOClient("x" * 12, transport=httpx.MockTransport(lambda r: error), sleep=lambda s: None, retries=2)
    with pytest.raises(APIError, match=r"^GET /v2/droplets -> HTTP 500: boom$"):
        client.get("/v2/droplets")


def test_paginate_follows_the_next_links() -> None:
    def page(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params["page"])
        links = {"pages": {"next": "https://api.digitalocean.com/v2/droplets?page=2"}} if n == 1 else {}
        return httpx.Response(200, json={"droplets": [{"id": n}], "links": links})

    assert DOClient("x" * 12, transport=httpx.MockTransport(page)).paginate("/v2/droplets", "droplets") == \
        [{"id": 1}, {"id": 2}]


def test_dry_run_client_prints_mutations_and_returns_placeholders(capsys) -> None:
    sent: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request.method)
        return httpx.Response(200, json={"vpcs": []})

    client = DOClient("x" * 12, transport=httpx.MockTransport(answer), dry_run=True)
    assert client.get("/v2/vpcs") == {"vpcs": []}
    vpc = client.create("/v2/vpcs", {"name": "kraken-syd1", "region": "syd1"}, "vpc", note="create VPC kraken-syd1")
    assert vpc["vpc"]["id"].startswith("dry-run-") and vpc["vpc"]["name"] == "kraken-syd1"
    assert client.delete("/v2/vpcs/x", note="delete VPC x") is False
    assert sent == ["GET"]
    assert "would create VPC kraken-syd1 (POST /v2/vpcs)" in capsys.readouterr().out


def test_wait_until_gives_up_after_the_timeout() -> None:
    slept: list[float] = []
    client = DOClient("x" * 12, transport=httpx.MockTransport(lambda r: httpx.Response(200)), sleep=slept.append)
    with pytest.raises(WaitTimeout, match="kraken-lb to be active after 30 s"):
        client.wait_until(lambda: None, timeout=30, interval=10, what="kraken-lb to be active")
    assert slept == [10, 10, 10]


def test_porthole_env_passes_the_secret_check(provisioned, out_dir: Path) -> None:
    check_text((out_dir / "porthole.env").read_text(), {})
