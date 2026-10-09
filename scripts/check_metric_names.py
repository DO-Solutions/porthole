"""Catalog check: every Insights metric name in the repo must be in a committed region catalog.

It finds dotted names (do.droplets.cpu_utilization, as queries and alert rules write them) and underscored names
(do_droplets_cpu_utilization, as results and the catalog list them) in code, JSON, READMEs, dashboards and alert
specs, and fails with a list of the ones no file in watcher/catalog/ carries. The Insights API takes a rule on a
name that does not exist and the rule never fires (finding A38, B-034), so a typo has to be caught here.

A dotted token is checked whatever its family, so a wrong family (do.load_balancer.*) is caught too; do.<kind>.id
is a log resource attribute, not a metric. An underscored token is checked when it starts with a known family, so
labels such as do_tags pass. A trailing _ or . marks a prefix (do_functions_*), which is not a name. A line that
names a metric on purpose because it does not exist (a test of the 404 path, a parse error) carries the marker
not-in-catalog. BUGS.md is the record of past wrong names and is not swept."""
from __future__ import annotations

import re
import sys
from pathlib import Path

from leak_sweep import files

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "watcher" / "catalog"
SKIP = {Path("BUGS.md"), Path("scripts") / Path(__file__).name}
SKIP_DIRS = {CATALOG.relative_to(ROOT), Path("head/static/vendor")}
SKIP_SUFFIXES = {".woff2", ".png", ".jpg", ".ico", ".gz", ".svg"}
MARKER = "not-in-catalog"
FAMILIES = ("apps", "batch_inference", "container_registry", "databases", "droplets", "functions", "gpu_droplets",
            "kubernetes", "load_balancers", "nat_gateways", "nfs", "serverless", "spaces", "vector_database",
            "vector_databases", "volumes")
DOTTED = re.compile(r"(?<![\w.])do\.([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)(?!\w)(?!\.\w)")
UNDERSCORED = re.compile(r"(?<![\w.])do_(?:" + "|".join(FAMILIES) + r")_[a-z0-9_]+(?!\w)(?!\.\w)")


def catalog(directory: Path = CATALOG) -> dict[str, set[str]]:
    """region -> the underscored names of watcher/catalog/metric-names-<region>.txt (# lines are comments)."""
    out = {}
    for path in sorted(directory.glob("metric-names-*.txt")):
        region = path.stem.removeprefix("metric-names-")
        out[region] = {line.strip() for line in path.read_text().splitlines()
                       if line.strip() and not line.startswith("#")}
    return out


def names(line: str) -> list[str]:
    """The metric names on one line, underscored (do.droplets.load_1 -> do_droplets_load_1)."""
    found = []
    for m in DOTTED.finditer(line):
        family, name = m.groups()
        if name == "id" or name.endswith("_"):
            continue
        found.append(f"do_{family}_{name}")
    for m in UNDERSCORED.finditer(line):
        if not m.group().endswith("_"):
            found.append(m.group())
    return found


def sweep(paths: list[Path], known: set[str]) -> list[tuple[str, int, str]]:
    findings = []
    for path in paths:
        rel = path.relative_to(ROOT)
        if (rel in SKIP or path.suffix in SKIP_SUFFIXES or any(d in rel.parents for d in SKIP_DIRS)
                or not path.is_file()):
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if MARKER in line:
                continue
            for name in names(line):
                if name not in known:
                    findings.append((str(rel), n, name))
    return findings


def main() -> int:
    regions = catalog()
    if not regions:
        print(f"metric names: no catalog files in {CATALOG.relative_to(ROOT)}")
        return 1
    known = set().union(*regions.values())
    findings = sweep(files(), known)
    for rel, n, name in findings:
        print(f"{rel}:{n}: {name} is in no catalog ({', '.join(sorted(regions))})")
    distinct = sorted({name for _, _, name in findings})
    print(f"metric names: {len(findings)} use(s) of {len(distinct)} name(s) not in the catalog")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
