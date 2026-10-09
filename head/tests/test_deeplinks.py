"""Deep links: every fleet member gets one, overrides apply, the unverified flag travels with each link."""
from __future__ import annotations

from conftest import fleet_text

from porthole.config import Fleet
from porthole.deeplinks import DeepLinks

FLEET = Fleet.from_json(fleet_text())


def test_every_fleet_member_gets_a_link():
    links = DeepLinks(FLEET)
    for e in FLEET.entities():
        link = links.for_entity(e)
        assert link["url"] and link["url"].startswith("https://cloud.digitalocean.com/"), e.name
        assert link["verified"] is False  # patterns in common use, not from the docs
    t1 = links.for_entity(FLEET.entity("kraken-tentacle-1"))
    assert t1["url"] == "https://cloud.digitalocean.com/droplets/600000001/graphs"
    fn = next(e for e in FLEET.entities() if e.kind == "functions")
    assert links.for_entity(fn)["url"].endswith("/functions/fn-00000000-0000-0000-0000-000000000004")
    spaces = next(e for e in FLEET.entities() if e.kind == "spaces")
    assert links.for_entity(spaces)["url"].endswith("/spaces/kraken-a1b2c3")


def test_insights_tabs_are_unverified_and_docs_verified():
    links = DeepLinks(FLEET).public()
    for tab in ("metrics", "dashboards", "alerts", "logs", "traces"):
        assert links[f"insights.{tab}"]["verified"] is False
        assert links[f"insights.{tab}"]["note"] == "the docs give only the menu path"
    assert links["feature_preview"]["verified"] is True
    assert links["docs.limits"]["url"].startswith("https://docs.digitalocean.com/products/insights/")
    assert all(v["verified"] for k, v in links.items() if k.startswith("docs."))


def test_overrides_apply():
    links = DeepLinks(FLEET, {"insights.metrics": "https://cloud.digitalocean.com/insights/metrics?region=tor1",
                              "droplet": {"pattern": "https://cloud.digitalocean.com/droplets/{id}", "verified": True},
                              "insights.logs": {"note": "no pattern here, ignored"}})
    m = links.link("insights.metrics")
    assert m == {"url": "https://cloud.digitalocean.com/insights/metrics?region=tor1", "verified": True,
                 "note": "override"}
    assert links.for_entity(FLEET.entity("kraken-tentacle-2"))["url"] == \
        "https://cloud.digitalocean.com/droplets/600000002"
    assert links.link("insights.logs")["verified"] is False


def test_missing_ids_give_no_url():
    fleet = Fleet.from_dict({"tentacles": [{"name": "kraken-tentacle-1", "region": "tor1",
                                            "url": "http://192.0.2.1:8800"}]})
    assert DeepLinks(fleet).for_entity(fleet.entities()[0])["url"] is None
    assert DeepLinks(fleet).link("nothing-like-this")["url"] is None


async def test_config_carries_links_and_flags(env):
    cfg = (await env.client.get("/api/config")).json()
    assert all("link" in e and "link_verified" in e for e in cfg["entities"])
    assert cfg["links"]["insights.alerts"]["verified"] is False
    assert cfg["fleet"]["tentacles"][0]["link"].endswith("/droplets/600000001/graphs")
