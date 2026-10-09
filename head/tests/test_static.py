"""The static front end: clean paths, local assets only, the skin equals the skin file, explainers verbatim."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from conftest import HEAD, REPO

from porthole.routes.pages import PAGES

STATIC = HEAD / "static"

# Appendix E of the design, verbatim
EXPLAINERS = {
    "index.html": "Porthole is a window onto DigitalOcean Insights. The kraken outside is a small fleet we run for "
    "this demo: three Droplets called tentacles (two in Toronto, one in Sydney), a load balancer, a managed Postgres, "
    "a one-node Kubernetes cluster, a Function, a Spaces bucket, a container registry and a Managed Agents session. "
    "You can make the fleet do things, then see what Insights records about it through the same API you would use. "
    "Every number on this page came from an Insights API call; the calls are listed in the drawer at the bottom.",
    "stir.html": "Scenarios run on the tentacles for a bounded time and stop on their own. Starting one needs the "
    "captain's key; watching does not. Voyages chain scenarios and Insights checks into one story and record every "
    "step with a timestamp.",
    "metrics.html": "Insights stores metrics at one-minute resolution and serves them through a Prometheus-compatible "
    "API, one region at a time. Names are written with dots when you ask (do.droplets.cpu_utilization) and come back "
    "with underscores in the results. The chart below is a range query for the region you picked. Choose 'both' to "
    "see Toronto and Sydney side by side; Porthole asks each region separately because the API does not add regions "
    "together.",
    "dashboards.html": "Dashboards have no API yet, so this page cannot draw yours. It shows the dashboard we built "
    "for the fleet as a file you can import in the control panel, and runs each of its queries here so you can check "
    "what the chart would show. Variables, thresholds and chart types are listed from the file.",
    "alerts.html": "An alert rule watches one metric over a window and notifies channels when the threshold is "
    "crossed. Porthole reads the rules we created for the fleet, their instances (active and resolved) and the "
    "channels, including the webhook channel that points back at this site. Deliveries to that webhook appear below "
    "with their headers, so the undocumented signature scheme can be seen. Run the 'Alert round trip' voyage to "
    "watch the whole loop with timings.",
    "logs.html": "The Logs API searches one region for up to seven days with a filter tree and cursor paging. For "
    "Droplets, logs are supposed to arrive through the Observability agent; on 8 October 2026 the agent installed by "
    "the tick box shipped metrics only (finding A6b). This page shows three things side by side: what Insights "
    "returns, what this App Platform app emitted, and what the tentacles say they wrote during a log storm, so the "
    "gap is visible instead of hidden.",
    "traces.html": "Insights shows traces in the control panel but has no traces API, so Porthole cannot fetch them. "
    "It shows the trace ids produced by the tentacle request chains and its own spans, and links to the Traces tab "
    "where you can search for an id. How an application's traces reach Insights is not documented yet; this page "
    "says what was exported and where.",
    "api.html": "Every request Porthole makes to the DigitalOcean API is listed here: method, path, parameters, "
    "status, time and the start of the response. Secrets are replaced with ***. Copy a call as curl to run it "
    "yourself with your own token.",
    "brain.html": "The Kraken's Brain answers questions about the fleet using the same read-only Insights calls as "
    "the panels. Today it is a scripted deckhand: it recognises a tentacle name and a symptom, runs the checks, and "
    "explains what it found. Anything that would change the fleet shows up as an approval card and waits for the "
    "captain. The Managed Agents version plugs into the same panel.",
}
SKIN_MAP = {"--bg": "ui.bg", "--card": "ui.card", "--line": "ui.line", "--ink": "ui.ink", "--dim": "ui.dim",
            "--accent": "ui.accent", "--gold": "ships.laden", "--grid-minor": "sea.gridMinor",
            "--grid-major": "sea.gridMajor", "--axis-label": "sea.label", "--font": "ui.font",
            **{f"--fleet-{i}": f"fleet.{i - 1}" for i in range(1, 9)}}


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[tuple[str, str, str]] = []
        self.scripts: list[dict] = []
        self.inline_styles: list[str] = []
        self.explainer: list[str] = []
        self._in_explainer = 0
        self._in_script = False
        self.script_text = ""

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if "style" in a:
            self.inline_styles.append(tag)
        for key in ("src", "href"):
            if a.get(key) is not None:
                self.refs.append((tag, key, a[key]))
        if tag == "script":
            self.scripts.append(a)
            self._in_script = True
        if "explainer" in (a.get("class") or "").split():
            self._in_explainer += 1
        elif self._in_explainer:
            self._in_explainer += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False
        if self._in_explainer:
            self._in_explainer -= 1

    def handle_data(self, data: str) -> None:
        if self._in_explainer:
            self.explainer.append(data)
        if self._in_script:
            self.script_text += data.strip()


def parse(name: str) -> Page:
    p = Page()
    p.feed((STATIC / name).read_text())
    return p


def built_pages() -> list[str]:
    return [f for f in PAGES.values() if (STATIC / f).exists()]


@pytest.mark.parametrize("route,filename", list(PAGES.items()))
async def test_pages_served_at_clean_paths(env, route, filename):
    if not (STATIC / filename).exists():
        pytest.skip(f"{filename} arrives in a later milestone")
    r = await env.client.get(route)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("filename", built_pages())
def test_assets_are_local_and_exist(filename):
    page = parse(filename)
    for tag, key, ref in page.refs:
        assert not re.match(r"^(https?:)?//", ref), f"{filename}: external {tag} {key}={ref}"
        if tag in ("link", "script", "img"):
            assert ref.startswith("/static/"), f"{filename}: {ref}"
            assert (STATIC / ref.removeprefix("/static/")).is_file(), f"{filename}: missing {ref}"


@pytest.mark.parametrize("filename", built_pages())
def test_no_inline_style_or_script(filename):
    page = parse(filename)
    assert page.inline_styles == [], f"{filename}: style= on {page.inline_styles}"
    assert page.script_text == "", f"{filename}: inline script"
    assert "<style" not in (STATIC / filename).read_text()
    modules = [s for s in page.scripts if s.get("type") == "module"]
    assert len(modules) == 1 and modules[0]["src"].startswith("/static/js/pages/")
    assert all(s.get("src") for s in page.scripts)


@pytest.mark.parametrize("filename", built_pages())
def test_explainer_verbatim(filename):
    text = " ".join("".join(parse(filename).explainer).split())
    assert text == EXPLAINERS[filename]


@pytest.mark.parametrize("filename", built_pages())
def test_page_files_stay_small(filename):
    assert len((STATIC / filename).read_text().splitlines()) < 120


def test_skin_css_equals_the_skin_file():
    skin = json.loads((REPO / "watcher" / "skin" / "kraken.skin.json").read_text())
    css = (STATIC / "css" / "skin.css").read_text()
    declared = dict(re.findall(r"(--[a-z0-9-]+):\s*([^;]+);", css))
    for var, path in SKIN_MAP.items():
        value: object = skin
        for part in path.split("."):
            value = value[int(part)] if isinstance(value, list) else value[part]
        assert declared[var].strip() == value, var


def test_fonts_present_and_declared():
    css = (STATIC / "css" / "skin.css").read_text()
    for weight in (400, 500):
        font = STATIC / "fonts" / f"SpecialGothic-{weight}.woff2"
        assert font.is_file() and font.read_bytes()[:4] == b"wOF2"
        assert f"/static/fonts/SpecialGothic-{weight}.woff2" in css


def test_uplot_vendored_or_pending():
    vendor = STATIC / "vendor" / "uplot"
    if (vendor / ".pending").exists():
        assert "uplot@1.6.32" in (vendor / ".pending").read_text()
        return
    for f in ("uPlot.iife.min.js", "uPlot.min.css", "LICENSE", "VENDOR.md"):
        assert (vendor / f).is_file(), f
    import hashlib
    listed = (vendor / "VENDOR.md").read_text()
    for f in ("uPlot.iife.min.js", "uPlot.min.css", "LICENSE"):
        assert hashlib.sha256((vendor / f).read_bytes()).hexdigest() in listed, f


@pytest.mark.parametrize("path", sorted(str(p.relative_to(STATIC)) for p in STATIC.rglob("*.js")
                                        if "vendor" not in p.parts))
def test_scripts_never_build_html_from_strings(path):
    text = (STATIC / path).read_text()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert banned not in text, f"{path}: {banned}"
    assert not re.search(r"https?://", text), f"{path}: external URL"
    assert "setAttribute(\"style\"" not in text and "setAttribute('style'" not in text


def test_css_has_no_external_urls():
    for css in (STATIC / "css").glob("*.css"):
        assert not re.search(r"url\(\s*['\"]?(https?:)?//", css.read_text()), css.name
        assert "@import" not in css.read_text(), css.name


def test_page_files_reference_porthole_js_through_their_module():
    for f in built_pages():
        module = parse(f).scripts[-1]["src"]
        js = (STATIC / module.removeprefix("/static/")).read_text()
        assert 'from "../porthole.js"' in js, module
        assert Path(module).stem in ("bridge", "stir", "metrics", "dashboards", "alerts", "logs", "traces", "api",
                                     "brain")


def test_slot_layer_matches_the_config_palette():
    from porthole.config import SLOT_PALETTE
    css = (STATIC / "css" / "porthole.css").read_text()
    mapped = dict(re.findall(r"--slot-(\d)\s*:\s*var\(--fleet-(\d)\)", css))
    assert [int(mapped[str(i)]) for i in range(1, 9)] == list(SLOT_PALETTE)
    assert sorted(SLOT_PALETTE) == list(range(1, 9))  # a permutation: zero hex changes


async def test_config_palette_follows_the_slot_order(env):
    from porthole.config import SLOT_PALETTE
    skin = json.loads((REPO / "watcher" / "skin" / "kraken.skin.json").read_text())["fleet"]
    palette = (await env.client.get("/api/config")).json()["palette"]
    assert palette == {str(i + 1): skin[idx - 1] for i, idx in enumerate(SLOT_PALETTE)}


def test_static_readme_records_the_validator_run():
    text = (STATIC / "README.md").read_text()
    assert "## Palette validator" in text and "#081f1e" in text and "## Slot order" in text


async def test_favicon_and_missing_pages(env):
    r = await env.client.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert r.content.lstrip().startswith(b"<svg") or b"<svg" in r.content[:300]
    assert (await env.client.get("/nowhere")).status_code == 404
    assert (await env.client.get("/static/js/nope.js")).status_code == 404


def test_all_nine_pages_exist():
    assert len(PAGES) == 9 and built_pages() == list(PAGES.values())


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed here; CI runs these with Node 22")
def test_charts_js_unit_tests():
    r = subprocess.run(["node", "--test", str(HEAD / "tests" / "js" / "*.test.mjs")], capture_output=True, text=True,
                       timeout=120, cwd=HEAD)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
