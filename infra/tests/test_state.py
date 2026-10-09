"""state.json: the round trip, the created flag, and the guard that refuses to write anything secret.

Part of acceptance criterion 13: state.json never contains a secret, tested with planted values in the
environment, in the fake API's answers, and written directly."""
from __future__ import annotations

from pathlib import Path

import pytest

from state import SecretLeak, State, check_text

UUID = "00000000-0000-0000-0000-000000000001"


def record(state: State, key: str = "vpc:syd1", ident: str = UUID, created: bool = True, **facts: object) -> dict:
    return state.record(key, step="network", kind="vpc", ident=ident, name="kraken-syd1", created=created,
                        created_at="2026-10-09T00:00:00Z" if created else None, **facts)


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "out" / "state.json"
    record(State.load(path), region="syd1")
    assert State.load(path).get("vpc:syd1") == {
        "step": "network", "kind": "vpc", "id": UUID, "name": "kraken-syd1", "created": True,
        "created_at": "2026-10-09T00:00:00Z", "region": "syd1"}


def test_an_entry_found_again_keeps_created_true_and_its_facts(tmp_path: Path) -> None:
    state = State.load(tmp_path / "state.json")
    record(state, urn="do:vpc:1")
    again = record(state, created=False)
    assert again["created"] is True and again["created_at"] == "2026-10-09T00:00:00Z"
    assert again["urn"] == "do:vpc:1"
    other = record(state, ident="00000000-0000-0000-0000-000000000002", created=False)
    assert other["created"] is False and "urn" not in other


def test_update_and_remove(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = State.load(path)
    record(state)
    state.update("vpc:syd1", default=False)
    assert State.load(path).get("vpc:syd1")["default"] is False
    state.remove("vpc:syd1")
    assert State.load(path).resources == {}


def test_the_guard_refuses_a_planted_variable_and_writes_nothing(tmp_path: Path, env: dict) -> None:
    path = tmp_path / "state.json"
    state = State.load(path, env)
    record(state)
    before = path.read_text()
    with pytest.raises(SecretLeak, match="TENTACLE_KEY") as raised:
        record(state, key="vpc:tor1", note=f"key is {env['TENTACLE_KEY']}")
    assert env["TENTACLE_KEY"] not in str(raised.value)
    assert path.read_text() == before and state.get("vpc:tor1") is None


@pytest.mark.parametrize("shaped", [
    "dop" + "_v1_" + "ab" * 32,
    "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
    "AVNS_" + "plantedPassword9",
    "postgresql://doadmin:" + "planted-pw" + "@db.example.test:25060/defaultdb",
    "a1B2c3D4" * 6,
    "deadbeef" * 6,
])
def test_the_guard_refuses_token_shapes(tmp_path: Path, shaped: str) -> None:
    state = State.load(tmp_path / "state.json")
    with pytest.raises(SecretLeak, match="refusing to write state.json"):
        record(state, note=shaped)
    assert not (tmp_path / "state.json").exists()


def test_the_guard_refuses_a_secret_seen_during_the_run(tmp_path: Path) -> None:
    state = State.load(tmp_path / "state.json")
    state.add_secret("the database password", "pw-from-api")
    with pytest.raises(SecretLeak, match="the database password"):
        record(state, note="pw-from-api")


def test_the_guard_lets_ordinary_state_through() -> None:
    check_text('{"id": 600000001, "ip": "203.0.113.10", "urn": "do:dbaas:00000000-0000-0000-0000-000000000002", '
               '"url": "https://faas-tor1-00000000.doserverless.co/api/v1/web/fn-00000000-0000-0000-0000-0000000'
               '00004/kraken/ping", "access": "DO00FAKEACCESS000018", "manifest_digest": "0123456789ab"}', {})


def test_a_readonly_state_never_writes(tmp_path: Path) -> None:
    state = State.load(tmp_path / "state.json", readonly=True)
    record(state)
    assert state.get("vpc:syd1") and not (tmp_path / "state.json").exists()
    with pytest.raises(SecretLeak):
        record(state, note="dop" + "_v1_" + "cd" * 32)


def test_a_full_run_writes_no_secret(provisioned, out_dir: Path, planted: list[str]) -> None:
    for name in ("state.json", "porthole.env"):
        text = (out_dir / name).read_text()
        assert [value for value in planted if value in text] == []
        check_text(text, {})


def test_a_dry_run_after_a_full_run_still_writes_no_secret(provisioned, run, out_dir: Path, planted) -> None:
    assert run("provision", "--dry-run").code == 0
    text = (out_dir / "state.json").read_text()
    assert [value for value in planted if value in text] == []
