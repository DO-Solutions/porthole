"""Builder-mode PromQL: exact output per agg and filter set, rejections, caps, units, dotted names."""
from __future__ import annotations

import pytest
from conftest import fleet_text

from porthole import promql
from porthole.config import Fleet
from porthole.promql import BuilderError

FLEET = Fleet.from_json(fleet_text())
TOR1_PIN = 'resource_urn=~"do:droplet:600000001|do:droplet:600000002"'
CPU = "do.droplets.cpu_utilization"


@pytest.mark.parametrize("agg", ["avg", "sum", "max", "min"])
def test_aggregations(agg):
    q = promql.build(CPU, agg, {}, "tor1", FLEET)
    assert q == f'{agg} by (resource_urn) ({CPU}{{resource_region_slug="tor1", {TOR1_PIN}}})'


def test_rate_and_raw_selector():
    q = promql.build("do.droplets.network_receive_bytes", "rate", {}, "syd1", FLEET)
    assert q == ('sum by (resource_urn) (rate(do.droplets.network_receive_bytes{resource_region_slug="syd1", '
                 'resource_urn=~"do:droplet:600000003"}[5m]))')
    assert promql.build(CPU, None, {}, "syd1", FLEET) == \
        f'{CPU}{{resource_region_slug="syd1", resource_urn=~"do:droplet:600000003"}}'
    assert promql.build(CPU, "none", {}, "syd1", FLEET) == promql.build(CPU, None, {}, "syd1", FLEET)


def test_filters_replace_the_fleet_pin():
    """A resource_name chip on a fleet member, by name or display name, becomes that member's URN (B-023)."""
    one = promql.build(CPU, "avg", promql.parse_filters("resource_name=kraken-tentacle-1"), "tor1", FLEET)
    assert one == f'avg by (resource_urn) ({CPU}{{resource_region_slug="tor1", resource_urn="do:droplet:600000001"}})'
    assert promql.build(CPU, "avg", promql.parse_filters("resource_name=tentacle-1"), "tor1", FLEET) == one
    assert promql.build(CPU, "avg", promql.member_filters(FLEET.tentacle("tentacle-1")), "tor1", FLEET) == one
    two = promql.build(CPU, "max", promql.parse_filters(["resource_name=kraken-tentacle-1", "resource_name=tentacle-2",
                                                         "resource_urn=do:droplet:600000001"]), "tor1", FLEET)
    assert 'resource_urn=~"do:droplet:600000001|do:droplet:600000002"' in two
    assert two.count("resource_urn=") == 1 and "resource_name" not in two
    urn = promql.build("do.apps.app_requests_per_second", None,
                       promql.parse_filters("resource_urn=do:app:00000000-0000-0000-0000-000000000000"), "tor1", FLEET)
    assert urn == ('do.apps.app_requests_per_second{resource_region_slug="tor1", '
                   'resource_urn="do:app:00000000-0000-0000-0000-000000000000"}')


def test_the_pin_follows_the_metric_family():
    lb = promql.build("do.load_balancers.requests_per_second", "sum", {}, "tor1", FLEET)
    assert lb == ('sum by (resource_urn) (do.load_balancers.requests_per_second{resource_region_slug="tor1", '
                  'resource_urn=~"do:loadbalancer:00000000-0000-0000-0000-000000000001"})')
    other = promql.build("do.gpu_droplets.gpu_utilization", None, {}, "syd1", FLEET)
    assert other == 'do.gpu_droplets.gpu_utilization{resource_region_slug="syd1", resource_urn=~"do:droplet:600000003"}'


def test_members_without_a_urn_stay_selected_by_name():
    """The Function namespace has no URN in the fleet description, so its queries select and group by name."""
    fn = promql.build("do.serverless.invocations", "sum", {}, "tor1", FLEET)
    assert fn == ('sum by (resource_name) (do.serverless.invocations{resource_region_slug="tor1", '
                  'resource_name=~"kraken"})')
    assert promql.build("do.serverless.invocations", "sum", promql.parse_filters("resource_name=kraken"), "tor1",
                        FLEET) == fn.replace('=~"kraken"', '="kraken"')
    with pytest.raises(BuilderError, match="have no URN in the fleet description"):
        promql.build(CPU, "avg", promql.parse_filters("resource_name=kraken,resource_name=kraken-tentacle-1"), "tor1",
                     FLEET)


def test_enum_labels_keep_the_pin():
    q = promql.build("do.droplets.network_transmit_bytes", "sum", promql.parse_filters("network_device=eth1"),
                     "tor1", FLEET)
    assert TOR1_PIN in q and 'network_device="eth1"' in q


@pytest.mark.parametrize("metric,filters,agg,message", [
    ("do_droplets_cpu_utilization", {}, "avg", "use the dotted name do.droplets.cpu_utilization"),
    ("cpu_utilization", {}, "avg", "is not a dotted metric name"),
    ("do.droplets.cpu_utilization{x=\"1\"}", {}, "avg", "is not a dotted metric name"),
    (CPU, {"host_id": ["1"]}, "avg", "label 'host_id' is not available"),
    (CPU, {"resource_name": ["someone-elses-droplet"]}, "avg", "are not in this fleet"),
    (CPU, {"resource_urn": ["do:droplet:1"]}, "avg", "are not in this fleet"),
    (CPU, {"network_device": ["wlan0"]}, "avg", "are not allowed"),
    (CPU, {}, "topk", "agg must be one of"),
])
def test_rejections(metric, filters, agg, message):
    with pytest.raises(BuilderError) as err:
        promql.build(metric, agg, filters, "tor1", FLEET)
    assert message in str(err.value)


def test_filter_syntax():
    assert promql.parse_filters("resource_name=a,resource_name=b, network_device=eth0") == {
        "resource_name": ["a", "b"], "network_device": ["eth0"]}
    with pytest.raises(BuilderError):
        promql.parse_filters("resource_name")


def test_raw_promql_caps():
    assert promql.check_raw(" sum(rate(do.droplets.cpu_time[5m])) ") == "sum(rate(do.droplets.cpu_time[5m]))"
    with pytest.raises(BuilderError, match="the cap is 500"):
        promql.check_raw("x" * 501)
    for bad in ("sum(do.x.y", "do.x.y{a=\"1\"", "rate(do.x.y[5m)", "café", ""):
        with pytest.raises(BuilderError):
            promql.check_raw(bad)
    assert promql.check_raw('count({resource_name="a)"})')  # brackets inside strings do not count


def test_range_and_step_caps():
    assert promql.range_seconds("30m") == 1800 and promql.range_seconds("24h") == 86400
    with pytest.raises(BuilderError):
        promql.range_seconds("2h")
    assert promql.range_seconds("2h", builder=False) == 7200
    with pytest.raises(BuilderError, match="between 1m and 24h"):
        promql.range_seconds("25h", builder=False)
    assert promql.step_seconds(None, 1800) == 60 and promql.step_seconds("120s", 1800) == 120
    assert promql.step_seconds("1m", 86400) == 60
    with pytest.raises(BuilderError, match="at least 60s"):
        promql.step_seconds("30s", 1800)


@pytest.mark.parametrize("text,unit", [
    ("do.droplets.cpu_utilization", "percent"), ("do.apps.app_memory_pct", "percent"),
    ("do.droplets.filesystem_free_bytes", "bytes"), ("do.apps.app_request_duration_seconds", "seconds"),
    ("do.serverless.duration_ms", "ms"), ("do.apps.app_requests_per_second", "per_second"),
    ("sum(rate(do.droplets.network_receive_bytes[5m]))", "per_second"), ("do.droplets.load_1", "plain"),
])
def test_unit_heuristics(text, unit):
    assert promql.unit_for(text) == unit


@pytest.mark.parametrize("name,family,dotted", [
    ("do_droplets_cpu_utilization", "do.droplets", "do.droplets.cpu_utilization"),
    ("do_load_balancers_requests_per_second", "do.load_balancers", "do.load_balancers.requests_per_second"),
    ("do_apps_app_cpu_usage", "do.apps", "do.apps.app_cpu_usage"),
    ("do_container_registry_storage_used_bytes", "do.container_registry", "do.container_registry.storage_used_bytes"),
    ("do_mystery_thing_count", "do.mystery", "do.mystery.thing_count"),
])
def test_dotted_names_from_the_catalog(name, family, dotted):
    assert promql.dotted(name) == (family, dotted)


def test_regex_alternation_escapes():
    assert promql.regex_alternation(["a.b", "c|d"]) == '"a\\\\.b|c\\\\|d"'
    assert promql.quote('say "hi"') == '"say \\"hi\\""'
