"""Shared parts of the provisioning steps: the run context, the fleet's fixed names, errors and small helpers.

Each step gets one Context with the API client, the state, the environment, and the pieces the tests replace with
fakes: the plain web client (tentacles, the app, Spaces), the command runner, the SQL runner and the PATH lookup."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from doapi import BASE_URL, APIError, DOClient, is_placeholder
from state import State, now_iso

INFRA = Path(__file__).resolve().parents[1]
REPO = INFRA.parent
PROJECT = "insights-demo"
TAG = "insights-demo"
TENTACLE_TAG = "kraken-tentacle"
TENTACLE_PORT = 8800
SECRET_FIELDS = frozenset({"password", "secret_key", "key", "token", "uri", "private_uri"})
LABELS = {
    "project": "project", "tag": "tag", "vpc": "VPC", "firewall": "firewall", "reserved_ip": "reserved IP",
    "database": "database cluster", "functions_namespace": "functions namespace", "droplet": "Droplet",
    "load_balancer": "load balancer", "kubernetes": "Kubernetes cluster", "spaces_key": "Spaces key",
    "spaces_bucket": "Spaces bucket", "registry": "registry", "agent": "Harness Runtime session", "app": "app",
    "insights_channel": "Insights channel", "insights_rule": "alert rule",
}


@dataclass(frozen=True)
class Tentacle:
    name: str
    region: str
    peer: str | None
    slot: int


TENTACLES = (Tentacle("kraken-tentacle-1", "tor1", "kraken-tentacle-2", 1),
             Tentacle("kraken-tentacle-2", "tor1", "kraken-tentacle-1", 2),
             Tentacle("kraken-tentacle-3", "syd1", None, 3))


class StepError(Exception):
    """A step cannot go on. The message says what is missing and which step or variable provides it."""


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def run_command(cmd: list[str], env: Mapping[str, str] | None = None) -> RunResult:
    """Run doctl or kubectl and capture the output; a binary that cannot start gives exit code 127."""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, env=dict(env) if env else None, timeout=1800,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return RunResult(127, "", str(e))
    return RunResult(done.returncode, done.stdout, done.stderr)


class SqlError(Exception):
    """A statement sent with run_sql failed, or psycopg is not installed. The message is the server's."""


def run_sql(conninfo: Mapping[str, Any], statements: list[str]) -> None:
    """Run statements in one autocommit session; conninfo holds psycopg.connect's keyword arguments."""
    try:
        import psycopg
    except ImportError as e:
        raise SqlError(f"psycopg is not installed ({e}); pip install -r infra/requirements.txt") from None
    try:
        with psycopg.connect(**conninfo, connect_timeout=10, autocommit=True) as conn:
            for statement in statements:
                conn.execute(statement)
    except psycopg.Error as e:
        raise SqlError(f"{type(e).__name__}: {str(e).strip()}") from None


@dataclass
class Timeouts:
    """Seconds to wait for each kind of thing to become ready."""
    action: float = 600
    droplet: float = 600
    health: float = 900
    load_balancer: float = 900
    database: float = 1800
    kubernetes: float = 1800
    deploy: float = 1800
    check: float = 300
    drain: float = 900
    interval: float = 10


def find_named(items: Iterable[dict], name: str) -> dict | None:
    return next((item for item in items if item.get("name") == name), None)


def last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:200] if lines else "no output"


def rel(path: Path) -> str:
    """A path relative to the repo root when it is inside the repo."""
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


def insights_module() -> Any:
    """harness/insights_harness.py, imported from the repo and used as a library."""
    harness = str(REPO / "harness")
    if harness not in sys.path:
        sys.path.insert(0, harness)
    import insights_harness
    return insights_harness


@dataclass
class Context:
    """Everything a step needs. provision.py sets step before running each one."""
    api: DOClient
    state: State
    env: Mapping[str, str]
    web: httpx.Client
    out_dir: Path
    dry_run: bool = False
    out: Callable[[str], None] = print
    which: Callable[[str], str | None] = shutil.which
    run: Callable[..., RunResult] = run_command
    sql: Callable[[Mapping[str, Any], list[str]], None] = run_sql
    insights_transport: httpx.BaseTransport | None = None
    base_url: str = BASE_URL
    timeouts: Timeouts = field(default_factory=Timeouts)
    step: str = ""
    _insights: Any = field(default=None, repr=False)
    _project_urns: set[str] | None = field(default=None, repr=False)

    def need(self, name: str) -> str:
        value = (self.env.get(name) or "").strip()
        if not value:
            raise StepError(f"{name} is not set; export it and run again")
        return value

    def opt(self, name: str, default: str = "") -> str:
        return (self.env.get(name) or "").strip() or default

    def say(self, line: str) -> None:
        self.out(line)

    def require(self, key: str, step: str) -> dict:
        entry = self.state.get(key)
        if not entry:
            raise StepError(f"state.json has no {key} yet; run the {step} step first (provision.py --only {step})")
        return entry

    def create(self, path: str, body: Mapping[str, Any], key: str, note: str, id_field: str = "id") -> dict:
        """POST a new resource and return the object under key (a placeholder in a dry run)."""
        return self.api.create(path, body, key, note=note, id_field=id_field)[key]

    def resource(self, key: str, kind: str, name: str, found: dict | None, create: Callable[[], dict], *,
                 id_field: str = "id", shown: str | None = None, **facts: Any) -> tuple[dict, dict]:
        """Use what was found by name, or create it; record it in state and print one line either way."""
        what = f"{LABELS[kind]} {shown or name}"
        if found:
            obj, created = found, False
            self.say(f"exists {what} (id {found.get(id_field)})")
        else:
            obj, created = create(), True
            if not obj.get("dry_run"):
                self.say(f"created {what} (id {obj.get(id_field)})")
        stamp = obj.get("created_at") or (now_iso() if created else None)
        entry = self.state.record(key, step=self.step, kind=kind, ident=obj.get(id_field), name=name,
                                  created=created, created_at=stamp, **facts)
        return obj, entry

    def hide(self, obj: Any, label: str) -> None:
        """Register the secret fields of an API answer (passwords, keys, DSNs) so no write can contain them."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in SECRET_FIELDS and isinstance(v, str):
                    self.state.add_secret(f"{label} {k}", v)
                else:
                    self.hide(v, label)
        elif isinstance(obj, list):
            for v in obj:
                self.hide(v, label)

    def wait(self, check: Callable[[], Any], timeout: float, what: str) -> Any:
        return self.api.wait_until(check, timeout, self.timeouts.interval, what)

    def insights(self) -> Any:
        """The harness's Insights client with the work token, created on first use."""
        if self._insights is None:
            self._insights = insights_module().Insights(self.need("DIGITALOCEAN_TOKEN"), base_url=self.base_url,
                                                        transport=self.insights_transport, sleep=self.api.sleep)
        return self._insights

    def doctl_env(self) -> dict[str, str]:
        """This process's environment plus the work token as DIGITALOCEAN_ACCESS_TOKEN, which doctl reads."""
        return {**os.environ, "DIGITALOCEAN_ACCESS_TOKEN": self.need("DIGITALOCEAN_TOKEN")}

    def show_commands(self, reason: str, commands: list[list[str]]) -> None:
        self.say(f"{reason}; run this by hand from the repo root:")
        for cmd in commands:
            self.say(f"  {shlex.join(cmd)}")

    # UNVERIFIED: the project resource URNs do:kubernetes:<id>, do:app:<id> and do:space:<name>; Droplet, load
    # balancer, dbaas and reserved IP URNs are documented (BUGS.md B-017).
    def assign(self, urn: str) -> None:
        """Put a resource this tool created into the project unless it is there already. A failure is only a
        warning: project membership changes how the control panel groups resources, nothing else."""
        project = self.state.get("project")
        if not project or is_placeholder(project["id"]) or is_placeholder(urn.rsplit(":", 1)[-1]):
            if self.dry_run:
                self.say(f"would assign {urn} to project {PROJECT}")
            return
        path = f"/v2/projects/{project['id']}/resources"
        if self._project_urns is None:
            self._project_urns = {r.get("urn") for r in self.api.paginate(path, "resources")}
        if urn in self._project_urns:
            return
        try:
            self.api.post(path, {"resources": [urn]}, note=f"assign {urn} to project {PROJECT}")
        except APIError as e:
            self.say(f"warning: could not assign {urn} to project {PROJECT}: {e}")
            return
        self._project_urns.add(urn)
        if not self.dry_run:
            self.say(f"assigned {urn} to project {PROJECT}")

    def close(self) -> None:
        self.web.close()
        self.api.close()
        if self._insights is not None:
            self._insights.close()


def verify_urn(ctx: Context, name: str, region: str, ident: Any, fallback: str) -> tuple[str, bool]:
    """The URN Insights reports for this resource name, or the fallback pattern marked unverified."""
    if is_placeholder(ident):
        return fallback, False
    harness = insights_module()
    try:
        body = ctx.insights().label_values("resource_urn", match=[f'{{resource_name="{name}"}}'], region=region)
    except (harness.InsightsError, httpx.HTTPError):
        return fallback, False
    values = [v for v in body.get("data") or [] if isinstance(v, str)]
    urn = next((v for v in values if str(ident) in v), values[0] if len(values) == 1 else None)
    return (urn, True) if urn else (fallback, False)


def note_urn(ctx: Context, key: str, region: str, fallback: str) -> None:
    """Record the URN of a sea resource with urn_verified, read from Insights once the resource reports there.
    Prints a line only when the recorded URN changes."""
    entry = ctx.state.get(key) or {}
    if entry.get("urn_verified") or is_placeholder(entry.get("id")):
        return
    urn, verified = verify_urn(ctx, entry["name"], region, entry["id"], fallback)
    if (entry.get("urn"), entry.get("urn_verified")) == (urn, verified):
        return
    ctx.state.update(key, urn=urn, urn_verified=verified)
    source = "reported by Insights" if verified else "not reported by Insights yet, so the documented pattern"
    ctx.say(f"urn of {entry['name']}: {urn} ({source})")
