"""Leak sweep for the public repo: secret shapes, lab names, private keys, and public IPv4 addresses.

It matches the shape of a secret, not a bare prefix: the carried-over harness tests and OpenAPI examples use the
words dop_v1_ and whsec on purpose. Documentation ranges (RFC 5737), private and loopback addresses pass."""
from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [
    ("DigitalOcean token", re.compile(r"do[por]_v1_[0-9a-f]{20,}")),
    ("webhook signing secret", re.compile(r"whsec_[A-Za-z0-9+/=]{16,}")),
    ("private key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("lab secret path", re.compile(r"secret/", re.IGNORECASE)),
    # Extra private names to sweep for come from the environment (comma separated), so the public
    # repo never has to list them: LEAK_SWEEP_NAMES="host-a,host-b" python3 scripts/leak_sweep.py
    *[
        ("private name", re.compile(re.escape(name.strip()), re.IGNORECASE))
        for name in os.environ.get("LEAK_SWEEP_NAMES", "").split(",")
        if name.strip()
    ],
    ("cloud access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack webhook", re.compile(r"hooks\.slack\.com/services/T[0-9A-Z]{6,}/B[0-9A-Z]{6,}/[0-9A-Za-z]{16,}")),
]
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
SKIP_SUFFIXES = {".woff2", ".png", ".jpg", ".ico", ".gz"}


def files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
        return [ROOT / f for f in out]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]


def sweep(paths: list[Path]) -> list[str]:
    findings = []
    for path in paths:
        if path.suffix in SKIP_SUFFIXES or not path.is_file() or path.name == Path(__file__).name:
            continue
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(ROOT)
        for n, line in enumerate(text.splitlines(), 1):
            for name, rx in PATTERNS:
                if rx.search(line):
                    findings.append(f"{rel}:{n}: {name}")
            for raw in IPV4.findall(line):
                try:
                    ip = ipaddress.ip_address(raw)
                except ValueError:
                    continue
                if ip.is_global:
                    findings.append(f"{rel}:{n}: public IPv4 {raw}")
    return findings


def main() -> int:
    findings = sweep(files())
    for f in findings:
        print(f)
    print(f"leak sweep: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
