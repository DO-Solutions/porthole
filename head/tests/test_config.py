"""Settings.from_env and the fleet model: defaults, parsing, explicit validation errors, slots, .env.example."""
from __future__ import annotations

import json
import re

import pytest
from conftest import HEAD, REPO, AppEnv, base_env, fleet_text

from porthole.config import VARIABLES, Fleet, FleetError, Settings


def fleet_dict() -> dict:
    return json.loads(fleet_text())


def test_defaults_without_any_environment():
    s = Settings.from_env({})
    assert s.public_url == "https://insights-demo.digitalocean.solutions"
    assert s.insights_base_url == "https://api.digitalocean.com"
    assert s.trust_proxy is True and s.insights_write is False
    assert s.upstream_budget_per_min == 200 and s.cache_ttl_s == 20.0
    assert s.brain == "deckhand" and s.service_name == "porthole" and s.port == 8080
    assert s.fleet.empty
    joined = " | ".join(s.problems)
    for var in ("DIGITALOCEAN_TOKEN", "PORTHOLE_CAPTAIN_KEY", "TENTACLE_KEY", "PORTHOLE_FLEET_JSON"):
        assert var in joined


def test_env_parsing_and_bad_numbers():
    s = Settings.from_env(base_env(PORTHOLE_INSIGHTS_WRITE="1", PORTHOLE_TRUST_PROXY="0",
                                   PORTHOLE_CACHE_TTL_S="abc", PORTHOLE_UPSTREAM_BUDGET_PER_MIN="50",
                                   PORTHOLE_BRAIN="nonsense", PORTHOLE_LOG_LEVEL="warning"))
    assert s.insights_write is True and s.trust_proxy is False
    assert s.cache_ttl_s == 20.0 and s.upstream_budget_per_min == 50
    assert s.brain == "deckhand" and s.log_level == "WARN"
    assert any("PORTHOLE_CACHE_TTL_S='abc'" in p for p in s.problems)
    assert any("PORTHOLE_BRAIN" in p for p in s.problems)


def test_captain_key_must_be_long_enough():
    short = Settings.from_env(base_env(PORTHOLE_CAPTAIN_KEY="short-key"))
    assert not short.captain_configured
    assert any("shorter than 24" in p for p in short.problems)
    assert Settings.from_env(base_env()).captain_configured


def test_secret_values_include_derived_parts():
    s = Settings.from_env(base_env(PORTHOLE_HOOK_BASIC="hookuser:basic-password-123",
                                   OTEL_EXPORTER_OTLP_HEADERS="api-key=otlp-header-secret"))
    values = s.secret_values()
    assert "basic-password-123" in values and "otlp-header-secret" in values
    assert s.token in values and s.captain_key in values


def test_fixture_fleet_parses_with_slots_and_links():
    fleet = Fleet.from_json(fleet_text())
    assert fleet.regions == ("tor1", "syd1")
    slots = {e.name: e.slot for e in fleet.entities() if e.slot}
    assert slots == {"kraken-tentacle-1": 1, "kraken-tentacle-2": 2, "kraken-tentacle-3": 3, "kraken-lb": 4,
                     "porthole": 5, "kraken-pg": 6, "kraken-doks": 7, "kraken": 8}
    assert fleet.tentacle("tentacle-2").name == "kraken-tentacle-2"
    assert fleet.entity("kraken").kind == "functions"  # the slotted member wins over the registry
    assert fleet.rule("round-trip").target == "kraken-tentacle-1"
    assert fleet.service_names() == ["kraken-tentacle-1", "kraken-tentacle-2", "kraken-tentacle-3", "porthole"]


def test_regions_are_derived_when_absent():
    d = fleet_dict()
    del d["regions"]
    assert Fleet.from_dict(d).regions == ("tor1", "syd1")


def test_default_slots_and_display_names():
    d = {"tentacles": [{"name": "kraken-tentacle-9", "region": "tor1", "url": "http://192.0.2.1:8800", "id": 7}],
         "sea": {"load_balancer": {"name": "kraken-lb", "region": "tor1"}}}
    fleet = Fleet.from_dict(d)
    t = fleet.tentacles[0]
    assert (t.display, t.slot, t.urn, t.service_name) == ("tentacle-9", 1, "do:droplet:7", "kraken-tentacle-9")
    assert fleet.sea["load_balancer"].slot == 4


def test_a_functions_entry_without_urn_gets_one_from_its_namespace_id():
    """B-033: the live app's description predates the functions urn; Insights labels the namespace's series
    do:functions_namespace:<namespace id>, so the head completes it."""
    d = fleet_dict()
    del d["sea"]["functions"]["urn"]
    fn = Fleet.from_dict(d).sea["functions"]
    assert fn.urn == "do:functions_namespace:fn-00000000-0000-0000-0000-000000000004"
    assert Fleet.from_dict(d).by_urn(fn.urn).name == "kraken"
    d["sea"]["functions"]["urn"] = "do:functions_namespace:fn-given"
    assert Fleet.from_dict(d).sea["functions"].urn == "do:functions_namespace:fn-given"  # a given urn is kept
    del d["sea"]["functions"]["urn"], d["sea"]["functions"]["namespace_id"]
    assert Fleet.from_dict(d).sea["functions"].urn is None


@pytest.mark.parametrize("mutate,message", [
    (lambda d: d["tentacles"][1].update(region="nyc9"), "tentacles[1].region 'nyc9' is not one of regions"),
    (lambda d: d["tentacles"][0].update(url="ftp://192.0.2.1"), "tentacles[0].url must be an http or https URL"),
    (lambda d: d["tentacles"][0].update(url="http://user:pw@192.0.2.1"), "tentacles[0].url must be"),
    (lambda d: d["tentacles"][2].update(slot=2), "slot 2 is given to both"),
    (lambda d: d["tentacles"][0].update(slot=9), "tentacles[0].slot must be an integer from 1 to 8"),
    (lambda d: d["tentacles"][0].update(peer="kraken-tentacle-7"), "peer 'kraken-tentacle-7' is not a tentacle"),
    (lambda d: d["tentacles"][0].pop("name"), "tentacles[0].name is required"),
    (lambda d: d["sea"].update(submarine={"name": "x"}), "sea.submarine is not a known resource kind"),
    (lambda d: d["sea"]["load_balancer"].update(ip="not-an-ip"), "sea.load_balancer.ip must be an IPv4 address"),
    (lambda d: d["watcher"]["rules"][0].update(target="kraken-tentacle-9"), "watcher.rules[0].target"),
    (lambda d: d.update(regions="tor1"), "regions must be a list of region slugs"),
    (lambda d: d["head"].update(region="ams3"), "head.region 'ams3' is not one of regions"),
])
def test_validation_errors_name_the_field(mutate, message):
    d = fleet_dict()
    mutate(d)
    with pytest.raises(FleetError) as err:
        Fleet.from_dict(d)
    assert message in str(err.value)


def test_bad_fleet_json_degrades_instead_of_crashing():
    s = Settings.from_env(base_env(PORTHOLE_FLEET_JSON="{not json"))
    assert s.fleet.empty
    assert any("PORTHOLE_FLEET_JSON: PORTHOLE_FLEET_JSON is not JSON" in p for p in s.problems)


def test_the_control_panel_context_is_checked():
    assert Settings.from_env(base_env(PORTHOLE_DO_CONTEXT="00ab12")).do_context == "00ab12"
    s = Settings.from_env(base_env(PORTHOLE_DO_CONTEXT="00ab12&evil=1"))
    assert s.do_context == ""
    assert any("PORTHOLE_DO_CONTEXT" in p for p in s.problems)


def test_deeplink_overrides_must_be_an_object():
    s = Settings.from_env(base_env(PORTHOLE_DEEPLINKS_JSON="[1,2]"))
    assert s.deeplink_overrides == {}
    assert any("PORTHOLE_DEEPLINKS_JSON" in p for p in s.problems)


async def test_insights_base_url_reaches_the_harness_and_the_curl_lines():
    async with AppEnv(base_env(PORTHOLE_INSIGHTS_BASE_URL="http://insights-fake:9000/")) as e:
        assert str(e.deps.insights._http.base_url).rstrip("/") == "http://insights-fake:9000"
        await e.deps.run_sync(e.deps.insights.query, "count(do.droplets.cpu_utilization)", region="tor1")
        call = e.deps.trace.ring[-1]
        curl = (await e.client.get(f"/api/trace/{call.id}")).json()["curl"]
        assert "http://insights-fake:9000/v2/insights/query/tor1/prom/api/v1/query" in curl


async def test_service_name_is_on_logs_spans_and_the_traces_page():
    async with AppEnv(base_env(OTEL_SERVICE_NAME="porthole-staging")) as e:
        await e.client.get("/api/config")
        assert all(line["service.name"] == "porthole-staging" for line in e.log_lines())
        assert (await e.client.get("/api/traces/own")).json()["service_name"] == "porthole-staging"
        assert (await e.client.get("/api/logs/own")).json()["service_name"] == "porthole-staging"


async def test_cache_ttl_is_honored():
    params = {"region": "tor1", "metric": "do.droplets.cpu_utilization", "agg": "avg", "range": "30m"}
    async with AppEnv(base_env(PORTHOLE_CACHE_TTL_S="5")) as e:
        assert (await e.client.get("/api/insights/range", params=params)).json()["cached"] is False
        await e.clock.advance(4)
        assert (await e.client.get("/api/insights/range", params=params)).json()["cached"] is True
        await e.clock.advance(2)
        assert (await e.client.get("/api/insights/range", params=params)).json()["cached"] is False


async def test_log_level_filters_the_request_lines():
    async with AppEnv(base_env(PORTHOLE_LOG_LEVEL="ERROR")) as e:
        await e.client.get("/api/config")
        assert not [line for line in e.log_lines() if line["severity_text"] in ("INFO", "WARN")]
        assert (await e.client.get("/api/logs/own")).json()["records"] == []


async def test_public_url_context_and_link_overrides_reach_the_config():
    env = base_env(PORTHOLE_PUBLIC_URL="https://demo.example.test/", PORTHOLE_DO_CONTEXT="00ab12",
                   PORTHOLE_DEEPLINKS_JSON=json.dumps({"droplet": "https://cloud.digitalocean.com/droplets/{id}"}))
    async with AppEnv(env) as e:
        cfg = (await e.client.get("/api/config")).json()
        assert cfg["public_url"] == "https://demo.example.test"
        assert cfg["hook_url"] == "https://demo.example.test/hooks/insights"
        assert cfg["links"]["insights.metrics"]["url"].startswith("https://cloud.digitalocean.com/insights/metrics?i=00ab12&")
        t1 = cfg["fleet"]["tentacles"][0]
        assert t1["link"] == "https://cloud.digitalocean.com/droplets/600000001" and t1["link_verified"] is True
        channels = (await e.client.get("/api/insights/alerts")).json()["channels"]
        hook = next(c for c in channels if c["type"] == "webhook")
        assert hook["points_here"] is True  # the fake's channel URL follows the public URL


async def test_phase_two_secrets_are_scrubbed_everywhere():
    secrets = {"PORTHOLE_BRAIN_TOKEN": "brain-token-value-0001", "PORTHOLE_MCP_KEY": "mcp-key-value-0001",
               "PORTHOLE_GATEWAY_MCP_URL": "https://gateway.example/mcp/s-0001"}
    async with AppEnv(base_env(**secrets, OTEL_EXPORTER_OTLP_HEADERS="api-key=otlp-secret-0001")) as e:
        for secret in (*secrets.values(), "otlp-secret-0001"):
            e.deps.log.info(f"leak attempt {secret}")
            e.deps.trace.record(target="tentacle", method="GET", path="/health", status=200,
                                text=f'{{"echo": "{secret}"}}')
            assert secret not in e.stdout.getvalue()
            assert secret not in (await e.client.get("/api/trace")).text
            assert secret not in (await e.client.get("/api/logs/own")).text


def test_env_example_lists_every_variable():
    text = (HEAD / ".env.example").read_text()
    listed = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.M))
    names = {v.name for v in VARIABLES}
    assert names <= listed, f"missing from .env.example: {sorted(names - listed)}"
    assert listed <= names, f"unknown in .env.example: {sorted(listed - names)}"
    for v in VARIABLES:  # every variable carries a comment line right above it
        assert re.search(rf"^# .+\n#?\s*{v.name}=", text, re.M), v.name


def test_readme_configuration_table_lists_every_variable():
    readme = (REPO / "README.md").read_text()
    for v in VARIABLES:
        assert f"`{v.name}`" in readme, v.name
