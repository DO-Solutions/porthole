"""Fixtures for the infra tests: the planted environment, the fake DigitalOcean API, a fake doctl and kubectl,
and a run() helper that calls provision.py or teardown.py against them.

Everything runs offline; the secret variables carry planted values so the tests can show none of them is written."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from fake_do import DB_PASSWORD, FN_KEY, KUBE_TOKEN, SPACES_SECRET, FakeDO, uuid

import provision
import teardown
from steps.common import RunResult

# Planted secrets. Token shapes are built from parts so the repo holds no token-shaped string.
WORK_TOKEN = "dop" + "_v1_" + "0123456789abcdef" * 4
HEAD_TOKEN = "dop" + "_v1_" + "fedcba9876543210" * 4
SECRET_ENV = {"DIGITALOCEAN_TOKEN": WORK_TOKEN, "HEAD_TOKEN": HEAD_TOKEN, "TENTACLE_KEY": "planted-tentacle-key-01",
              "CAPTAIN_KEY": "planted-captain-key-0000000001", "HOOK_BEARER": "planted-hook-bearer-01",
              "HOOK_SECRET": "planted-hook-secret-01"}
ENV = {**SECRET_ENV, "SSH_KEY_IDS": "123456, 00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff",
       "SSH_ALLOW_CIDRS": "192.0.2.0/24", "ALERT_EMAIL": "kraken@example.com",
       "TENTACLE_TARBALL_URL": "https://github.com/DO-Solutions/porthole/archive/refs/tags/v0.1.0.tar.gz",
       "PUBLIC_URL": "https://porthole.example.test"}
PLANTED = [*SECRET_ENV.values(), DB_PASSWORD, SPACES_SECRET, FN_KEY, KUBE_TOKEN]
HELP = "Available Commands:\n  create      Create a session\n  delete      Delete a session\n  pause       Pause\n"
CREATE_HELP = "Flags:\n      --name\n      --size\n      --region\n      --prompt\nGlobal Flags:\n  -o, --output\n"


class FakeRunner:
    """doctl and kubectl as recorded calls. tools names the commands that are "on PATH"."""

    def __init__(self, world: FakeDO, tools: tuple[str, ...] = ()):
        self.world, self.tools = world, set(tools)
        self.calls: list[list[str]] = []
        self.kubeconfigs: list[tuple[Path, str]] = []

    def which(self, name: str) -> str | None:
        return f"/usr/local/bin/{name}" if name in self.tools else None

    def __call__(self, cmd: list[str], env: Any = None) -> RunResult:
        self.calls.append(list(cmd))
        if cmd[0] == "kubectl":
            path = Path(cmd[cmd.index("--kubeconfig") + 1])
            self.kubeconfigs.append((path, path.read_text()))
            return RunResult(0, "deployment.apps/kraken-echo created\n")
        if cmd[1:3] == ["serverless", "deploy"]:
            self.world.fn_deployed = True
        if cmd[1] == "harness-runtime" and cmd[-1] == "--help":
            return RunResult(0, CREATE_HELP if cmd[2] == "create" else HELP)
        if cmd[1:3] == ["harness-runtime", "create"]:
            return RunResult(0, json.dumps({"id": uuid(777), "name": "kraken-brain"}))
        return RunResult(0, "")


class Result(NamedTuple):
    code: int
    out: str
    err: str


@pytest.fixture
def world() -> FakeDO:
    return FakeDO()


@pytest.fixture
def runner(world: FakeDO) -> FakeRunner:
    return FakeRunner(world)


@pytest.fixture
def env() -> dict[str, str]:
    return dict(ENV)


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "out"


@pytest.fixture
def planted() -> list[str]:
    """Every planted secret: the secret variables and what the fake API echoes."""
    return list(PLANTED)


@pytest.fixture
def run(world: FakeDO, runner: FakeRunner, env: dict, out_dir: Path, capsys: pytest.CaptureFixture) -> Callable:
    """run("provision" or "teardown", *argv, env=...) against the fakes; returns code, stdout and stderr."""
    base_env = env

    def go(tool: str, *argv: str, env: dict | None = None) -> Result:
        module = provision if tool == "provision" else teardown
        extra = {"sql": world.sql} if tool == "provision" else {}
        code = module.main(list(argv), env=base_env if env is None else env, transport=world.transport(),
                           web_transport=world.web_transport(), runner=runner, which=runner.which,
                           sleep=lambda _s: None, out_dir=out_dir, **extra)
        captured = capsys.readouterr()
        return Result(code, captured.out, captured.err)

    return go


@pytest.fixture
def provisioned(run: Callable, world: FakeDO) -> FakeDO:
    """A world after one full provisioning run, with the request logs cleared."""
    result = run("provision")
    assert result.code == 0, result.out + result.err
    world.calls.clear()
    world.bodies.clear()
    world.web_calls.clear()
    world.sql_calls.clear()
    return world
