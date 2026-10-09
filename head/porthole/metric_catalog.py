"""The committed metric catalog, watcher/catalog/metric-names-<region>.txt, and the startup check against it.

Insights answers a query on a name it does not have with an empty result, and accepts an alert rule on one that
then never fires (finding A38, B-034). At startup the head looks up every name it asks for by name (the family
probes of watcher/probe_metrics.json and the voyages' metrics) in the catalog of each configured region where
that family has a fleet member, and logs one warning per name that is missing. It starts either way."""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from porthole.config import Fleet
from porthole.promql import FAMILY_KIND


def load(watcher_dir: Path) -> dict[str, set[str]]:
    """region -> underscored names; lines starting with # are the header."""
    out = {}
    for path in sorted((watcher_dir / "catalog").glob("metric-names-*.txt")):
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        out[path.stem.removeprefix("metric-names-")] = {s.strip() for s in lines if s.strip() and s[0] != "#"}
    return out


def missing(names: Iterable[str], fleet: Fleet, catalog: dict[str, set[str]]) -> dict[str, list[str]]:
    """Dotted name -> the configured regions with a catalog that lack it. A name is checked in the regions of the
    fleet members of its family (do.load_balancers in the load balancer's region); a family with no regional
    member (the registry) only has to be in one of the fleet's catalogs. Regions without a file are skipped."""
    regions = [r for r in fleet.regions if r in catalog]
    out = {}
    for name in dict.fromkeys(names):
        parts = name.split(".")
        kind = FAMILY_KIND.get(parts[1]) if len(parts) > 2 else None
        flat = name.replace(".", "_")
        home = [r for r in regions if any(e.kind == kind and e.region == r for e in fleet.entities())]
        lacking = [r for r in home if flat not in catalog[r]]
        if not home and regions and not any(flat in catalog[r] for r in regions):
            lacking = regions
        if lacking:
            out[name] = lacking
    return out
