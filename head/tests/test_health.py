"""GET /healthz: shape, version, and degraded mode without a token or fleet."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from conftest import HEAD, AppEnv, base_env


async def test_healthz_shape(env):
    r = await env.client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"status", "version", "uptime_s", "insights", "fleet", "brain", "problems"}
    assert body["status"] == "ok"
    assert body["insights"] == "configured"
    assert body["fleet"] == {"tentacles": 3, "regions": ["tor1", "syd1"]}
    assert body["brain"] == "deckhand"
    assert body["problems"] == []


async def test_healthz_reports_the_baked_version():
    async with AppEnv(base_env(PORTHOLE_VERSION="3f2a9c0")) as e:
        assert (await e.client.get("/healthz")).json()["version"] == "3f2a9c0"


async def test_uptime_follows_the_clock(env):
    await env.clock.advance(42)
    assert (await env.client.get("/healthz")).json()["uptime_s"] >= 42


async def test_degraded_mode_without_token_still_answers():
    async with AppEnv(base_env(DIGITALOCEAN_TOKEN=None, PORTHOLE_FLEET_JSON=None)) as e:
        r = await e.client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["insights"] == "missing"
        assert body["fleet"]["tentacles"] == 0
        assert any("DIGITALOCEAN_TOKEN is not set" in p for p in body["problems"])
        assert any("PORTHOLE_FLEET_JSON" in p for p in body["problems"])
        # the rest of the site works and explains itself
        assert (await e.client.get("/api/config")).status_code == 200
        lines = e.log_lines()
        assert any(line["body"].startswith("degraded: DIGITALOCEAN_TOKEN") for line in lines)


def test_the_module_entry_point_imports_from_a_checkout():
    """The README and .env.example name `python -m porthole.main`; found by the wringer failing outside the
    container, where the harness is not on the path."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    r = subprocess.run([sys.executable, "-c", "import porthole.main"], cwd=HEAD, env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-800:]


async def test_healthz_is_not_traced(env):
    await env.client.get("/healthz")
    names = [t["root"] for t in env.deps.telemetry.ring.traces()]
    assert not any("healthz" in n for n in names)


async def test_a_metric_outside_the_catalog_is_one_warning_and_the_head_still_starts(monkeypatch, tmp_path):
    """B-034: a probe or voyage metric that a configured region's committed catalog lacks gets one WARN line per
    name at startup, however often it is used; the voyages' own names give none."""
    import shutil

    from conftest import REPO

    import porthole.deps
    from porthole import voyages_metrics
    shutil.copytree(REPO / "watcher" / "catalog", tmp_path / "catalog")
    probes = {"tentacle": {"metric": "do.droplets.cpu_utilization"},
              "registry": {"metric": "do.container_registry.storage_used_bytes"}}  # not-in-catalog
    (tmp_path / "probe_metrics.json").write_text(json.dumps({"families": probes}))
    monkeypatch.setattr(porthole.deps, "WATCHER_DIR", tmp_path)
    old = ("do.droplets.load_1", "do.droplets.load_1")  # not-in-catalog: the name round-trip.json had
    monkeypatch.setattr(voyages_metrics, "METRICS", (*voyages_metrics.METRICS, *old))
    async with AppEnv() as e:
        assert (await e.client.get("/healthz")).json()["status"] == "ok"
        warned = [(line["severity_text"], line["metric.name"], line["regions"]) for line in e.log_lines()
                  if "committed catalog" in line["body"]]
    assert warned == [("WARN", "do.container_registry.storage_used_bytes", ["tor1", "syd1"]),  # not-in-catalog
                      ("WARN", "do.droplets.load_1", ["tor1", "syd1"])]  # not-in-catalog
