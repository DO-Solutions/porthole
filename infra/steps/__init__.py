"""The provisioning steps in the order they run, with the step numbers of design section 9.2.

provision.py runs them, teardown.py deletes their resources in the reverse order, and --plan prints this list
without creating a client or calling anything."""
from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Step:
    name: str                          # the --only name, and ensure_<name> in the module
    module: str
    design: tuple[tuple[int, str], ...]  # (design step number, what it creates), one per design step
    requires: tuple[str, ...] = ()


STEPS: tuple[Step, ...] = (
    Step("project", "steps.project", (
        (1, 'project "insights-demo"; every resource created below is assigned to it'),
        (2, 'tags "insights-demo" (on every taggable resource) and "kraken-tentacle" (the firewall target)'))),
    Step("network", "steps.network", (
        (3, "a VPC per region: kraken-<region> if it exists, else the region's default, else a new kraken-<region>"),
        (4, "firewall kraken-tentacles (22 from SSH_ALLOW_CIDRS, 8800 from anywhere, 80 from the LB); "
            "reserved IPs for tentacle-1 and -2"))),
    Step("database", "steps.database", (
        (7, 'kraken-pg: managed Postgres, db-s-1vcpu-1gb in tor1, database "kraken", user "tentacle"'),)),
    Step("functions", "steps.functions", (
        (9, 'functions namespace "kraken" in tor1 and function kraken/ping (doctl serverless deploy)'),)),
    Step("droplets", "steps.droplets", (
        (5, "kraken-tentacle-1 and -2 (tor1), -3 (syd1): s-1vcpu-1gb, Ubuntu 24.04, monitoring on, "
            "tentacle/install.sh as user_data; waits for /health"),),
         ("TENTACLE_KEY", "SSH_KEY_IDS", "TENTACLE_TARBALL_URL")),
    Step("lb", "steps.lb", (
        (6, "kraken-lb in tor1: HTTP 80 to 8800 on tentacle-1 and -2, health check /health"),)),
    Step("doks", "steps.doks", (
        (8, "kraken-doks in tor1 (one s-2vcpu-2gb node) and infra/k8s/kraken.yaml applied with kubectl"),)),
    Step("spaces", "steps.spaces", (
        (10, "Spaces key kraken-spaces and bucket kraken-<6 hex> in tor1 (one signed S3 PUT)"),)),
    Step("registry", "steps.registry", (
        (11, 'the team\'s container registry when there is one, else "kraken" on the starter tier'),)),
    Step("agent", "steps.agent", (
        (12, 'Harness Runtime session "kraken-brain", created with doctl and paused'),)),
    Step("app", "steps.app", (
        (13, "App Platform app from .do/app.yaml with its SECRET values; waits for the first deployment"),),
         ("HEAD_TOKEN", "CAPTAIN_KEY", "TENTACLE_KEY", "HOOK_BEARER", "HOOK_SECRET")),
    Step("insights", "steps.insights", (
        (14, "Insights webhook channel kraken-head, email channel kraken-email, alert rules from watcher/alerts"),),
         ("HOOK_BEARER", "HOOK_SECRET", "ALERT_EMAIL")),
    Step("fleet", "fleet", (
        (15, "infra/out/porthole.env and PORTHOLE_FLEET_JSON on the app; checks /healthz and /api/fleet"),)),
)
ALIASES = {"tag": "project", "vpc": "network", "firewall": "network"}


def plan_lines() -> list[str]:
    lines = ["Steps in the order provision.py runs them (design section 9.2 numbers):",
             "order  design  --only     creates"]
    order = 0
    for step in STEPS:
        for number, text in step.design:
            order += 1
            lines.append(f"{order:>5}  {number:>6}  {step.name:<9}  {text}")
    return lines


def select(only: str | None) -> list[Step]:
    """All steps, or the ones named in a comma separated list of step names, aliases or design numbers."""
    if not only:
        return list(STEPS)
    by_name = {s.name: s for s in STEPS}
    by_number = {str(n): s for s in STEPS for n, _ in s.design}
    wanted = set()
    for word in (w.strip().lower() for w in only.split(",") if w.strip()):
        step = by_name.get(ALIASES.get(word, word)) or by_number.get(word)
        if step is None:
            raise ValueError(f"unknown step {word!r}; use one of {', '.join(by_name)} or a design number 1 to 15")
        wanted.add(step.name)
    return [s for s in STEPS if s.name in wanted]


def missing_env(selected: list[Step], env: Mapping[str, str]) -> list[str]:
    """One line per required variable that is not set, naming the steps that need it."""
    need: dict[str, list[str]] = {"DIGITALOCEAN_TOKEN": ["every step"]}
    for step in selected:
        for name in step.requires:
            need.setdefault(name, []).append(step.name)
    return [f"{name} is not set (needed by {', '.join(users)})" for name, users in need.items()
            if not (env.get(name) or "").strip()]


def run(step: Step, ctx: Any) -> None:
    getattr(importlib.import_module(step.module), f"ensure_{step.name}")(ctx)
