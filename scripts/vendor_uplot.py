"""Vendors uPlot 1.6.32 into head/static/vendor/uplot when its files are missing and records their SHA-256.

With the files present it only checks the hashes against VENDOR.md. Without network it leaves .pending with the
exact URLs, and charts.js falls back to its table view."""
from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

DIR = Path(__file__).resolve().parents[1] / "head" / "static" / "vendor" / "uplot"
BASE = "https://cdn.jsdelivr.net/npm/uplot@1.6.32"
FILES = {"uPlot.iife.min.js": f"{BASE}/dist/uPlot.iife.min.js", "uPlot.min.css": f"{BASE}/dist/uPlot.min.css",
         "LICENSE": f"{BASE}/LICENSE"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_vendor_md() -> None:
    rows = "\n".join(f"| `{name}` | `{sha(DIR / name)}` |" for name in FILES)
    urls = "\n".join(f"- {url}" for url in FILES.values())
    (DIR / "VENDOR.md").write_text(
        "# Vendored: uPlot 1.6.32\n\nuPlot is MIT licensed (see LICENSE in this directory). The two files below are "
        "the release's\n`dist/uPlot.iife.min.js` and `dist/uPlot.min.css`, downloaded by `make vendor`.\n\n"
        f"| file | sha256 |\n|---|---|\n{rows}\n\nSource URLs:\n\n{urls}\n")


def main() -> int:
    DIR.mkdir(parents=True, exist_ok=True)
    missing = [name for name in FILES if not (DIR / name).is_file()]
    if not missing:
        listed = (DIR / "VENDOR.md").read_text() if (DIR / "VENDOR.md").is_file() else ""
        bad = [name for name in FILES if sha(DIR / name) not in listed]
        print("uPlot is vendored" + (f"; hashes not in VENDOR.md: {bad}" if bad else "; hashes match VENDOR.md"))
        return 1 if bad else 0
    try:
        for name in missing:
            with urllib.request.urlopen(FILES[name], timeout=30) as resp:  # noqa: S310 (fixed https URL)
                (DIR / name).write_bytes(resp.read())
    except OSError as e:
        (DIR / ".pending").write_text("uPlot could not be downloaded; fetch these into this directory:\n"
                                      + "\n".join(FILES.values()) + "\n")
        print(f"download failed ({e}); wrote .pending, charts fall back to tables")
        return 0
    (DIR / ".pending").unlink(missing_ok=True)
    write_vendor_md()
    print(f"downloaded {missing} and wrote VENDOR.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
