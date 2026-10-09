"""The provisioning state in infra/out/state.json: what exists, what provision.py created, and nothing secret.

Every write serializes the state first and refuses to save it when the text holds a secret variable's value, a
secret seen during the run, or a string shaped like a token, so a bug cannot put a credential on disk."""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SECRET_ENV = ("DIGITALOCEAN_TOKEN", "HEAD_TOKEN", "TENTACLE_KEY", "CAPTAIN_KEY", "HOOK_BEARER", "HOOK_SECRET")
MIN_SECRET_LENGTH = 8
SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("a DigitalOcean token", re.compile(r"do[opr]_v1_[0-9a-f]{64}")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY")),
    ("a webhook signing secret", re.compile(r"whsec_[A-Za-z0-9+/=_-]{16,}")),
    ("a managed database password", re.compile(r"AVNS_[A-Za-z0-9_-]{8,}")),
    ("a URL with a password in it", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@\"]+:[^\s/@\"]+@")),
    ("a JSON web token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.")),
    ("a long hex string", re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{40,}(?![0-9A-Za-z])")),
    ("a long base64 string", re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}")),
)


class SecretLeak(ValueError):
    """A write was refused because the text holds a secret. The message names the source, never the value."""


def now_iso() -> str:
    """The current UTC time, for example 2026-10-09T12:00:00Z."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def env_secrets(env: Mapping[str, str]) -> dict[str, str]:
    """{value: variable name} for every secret variable that is set and at least 8 characters long."""
    return {env[name]: name for name in SECRET_ENV if len(env.get(name) or "") >= MIN_SECRET_LENGTH}


def check_text(text: str, secrets: Mapping[str, str]) -> None:
    """Raise SecretLeak when text contains one of the secret values (raw or JSON-escaped) or a token shape."""
    for value, label in secrets.items():
        if value in text or json.dumps(value)[1:-1] in text:
            raise SecretLeak(f"it contains the value of {label}")
    for label, pattern in SHAPES:
        if pattern.search(text):
            raise SecretLeak(f"it contains {label}")


def write_text(path: Path, text: str, secrets: Mapping[str, str], *, dry_run: bool = False) -> None:
    """Check text for secrets, then write it atomically. Nothing is written when the check fails or in a dry run."""
    try:
        check_text(text, secrets)
    except SecretLeak as e:
        raise SecretLeak(f"refusing to write {path.name}: {e}") from None
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class State:
    """The resources of state.json by key, in the order they were first recorded. readonly is for dry runs:
    changes stay in memory and every save still runs the secrets check."""

    def __init__(self, path: Path, data: dict | None = None, secrets: Mapping[str, str] | None = None,
                 readonly: bool = False):
        self.path = path
        self.data = data or {"version": 1, "resources": {}}
        self.secrets: dict[str, str] = dict(secrets or {})
        self.readonly = readonly

    @classmethod
    def load(cls, path: Path, env: Mapping[str, str] | None = None, readonly: bool = False) -> State:
        data = None
        if path.exists():
            data = json.loads(path.read_text())
            if not isinstance(data, dict) or not isinstance(data.get("resources"), dict):
                raise ValueError(f"{path} is not a provisioning state file: it has no resources object")
        return cls(path, data, env_secrets(env or {}), readonly)

    @property
    def resources(self) -> dict[str, dict]:
        return self.data["resources"]

    def get(self, key: str) -> dict | None:
        return self.resources.get(key)

    def add_secret(self, label: str, value: str | None) -> None:
        """Remember a secret seen during the run, such as a password the API returned, so no write contains it."""
        if value and len(value) >= MIN_SECRET_LENGTH:
            self.secrets.setdefault(value, label)

    def record(self, key: str, *, step: str, kind: str, ident: Any, name: str, created: bool,
               created_at: str | None = None, **facts: Any) -> dict:
        """Store one resource and save. When a later run finds the same id, the entry keeps created: true, its
        created_at and the facts that run does not repeat."""
        old = self.resources.get(key) or {}
        same = bool(old) and old.get("id") == ident
        base = {"step": step, "kind": kind, "id": ident, "name": name,
                "created": bool(created or (same and old.get("created"))),
                "created_at": (old.get("created_at") if same else None) or created_at}
        kept = {k: v for k, v in old.items() if k not in base} if same else {}
        entry = {**base, **kept, **facts}
        self._commit(key, entry)
        return entry

    def update(self, key: str, **facts: Any) -> dict:
        """Change facts of an existing entry, such as an IP or a deploy status, and save."""
        entry = {**self.resources[key], **facts}
        self._commit(key, entry)
        return entry

    def remove(self, key: str) -> None:
        """Drop an entry and save. teardown.py calls this once DigitalOcean confirmed the delete."""
        if key in self.resources:
            self._commit(key, None)

    def _commit(self, key: str, entry: dict | None) -> None:
        before = dict(self.resources)
        if entry is None:
            del self.resources[key]
        else:
            self.resources[key] = entry
        try:
            self.save()
        except Exception:
            self.data["resources"] = before
            raise

    def save(self) -> None:
        write_text(self.path, json.dumps(self.data, indent=2) + "\n", self.secrets, dry_run=self.readonly)
