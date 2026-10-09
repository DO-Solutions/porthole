"""A small DigitalOcean API client for the provisioning scripts, on plain httpx so every call stays visible.

It retries 429 and 5xx answers with backoff, follows pagination and polls actions, and in dry-run mode it prints
each POST, PUT, PATCH or DELETE instead of sending it and returns a placeholder."""
from __future__ import annotations

import itertools
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

BASE_URL = "https://api.digitalocean.com"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PLACEHOLDER = "dry-run-"
MAX_DELAY = 60.0


class APIError(Exception):
    """A call failed. The message names the call and quotes the API's own error text."""

    def __init__(self, status: int, method: str, path: str, message: str):
        self.status = status
        where = f"{method} {path}"
        super().__init__(f"{where} -> HTTP {status}: {message}" if status else f"{where}: {message}")


class WaitTimeout(Exception):
    """A resource did not reach the expected state before the timeout."""


def is_placeholder(value: Any) -> bool:
    """True for the ids dry-run mode invents for resources it did not create."""
    return isinstance(value, str) and value.startswith(PLACEHOLDER)


def error_text(resp: httpx.Response) -> str:
    """The API's message from an error answer, at most 200 characters."""
    try:
        body: Any = resp.json()
    except ValueError:
        body = " ".join(resp.text.split())
    if isinstance(body, dict):
        body = body.get("message") or body.get("error") or body.get("id") or ""
    return str(body)[:200] or resp.reason_phrase


class DOClient:
    """Bearer-token client for api.digitalocean.com. Tests pass transport= and a sleep that does not wait."""

    def __init__(self, token: str, *, base_url: str = BASE_URL, transport: httpx.BaseTransport | None = None,
                 dry_run: bool = False, sleep: Callable[[float], None] = time.sleep,
                 log: Callable[[str], None] = print, retries: int = 5, timeout: float = 30.0,
                 clock: Callable[[], float] = time.time):
        if not token:
            raise ValueError("DIGITALOCEAN_TOKEN is not set")
        self.dry_run = dry_run
        self.sleep = sleep
        self.log = log
        self.retries = retries
        self._clock = clock
        self._ids = itertools.count(1)
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=timeout, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "porthole-infra/1.0"})

    def close(self) -> None:
        self._http.close()

    def placeholder_id(self) -> str:
        return f"{PLACEHOLDER}{next(self._ids)}"

    def send(self, method: str, path: str, params: Mapping[str, Any] | None = None, json: Any = None) -> httpx.Response:
        """One call. 429, 5xx and refused connections are retried with exponential backoff; returns the last answer."""
        attempt, delay = 0, 1.0
        while True:
            last = attempt >= self.retries
            try:
                resp = self._http.request(method, path, params=params, json=json)
            except httpx.TransportError as e:
                # A connection that never opened is safe to retry for any method; other failures only for reads.
                if last or (method in MUTATING and not isinstance(e, httpx.ConnectError)):
                    raise APIError(0, method, path, f"{type(e).__name__}: {e}") from None
            else:
                if last or (resp.status_code != 429 and resp.status_code < 500):
                    return resp
                if resp.status_code == 429:
                    delay = max(delay, self._retry_hint(resp))
            self.sleep(min(delay, MAX_DELAY))
            attempt, delay = attempt + 1, delay * 2

    def _retry_hint(self, resp: httpx.Response) -> float:
        """Seconds from Retry-After (seconds) or ratelimit-reset (a unix time); 0 when neither is usable."""
        for name in ("retry-after", "ratelimit-reset"):
            try:
                value = float(resp.headers[name])
            except (KeyError, ValueError):
                continue
            return max(0.0, value - self._clock()) if value > 1e9 else value
        return 0.0

    def request(self, method: str, path: str, params: Mapping[str, Any] | None = None, json: Any = None, *,
                note: str = "", missing_ok: bool = False) -> Any:
        """Send and return the JSON body ({} when empty); missing_ok turns a 404 into None. In dry-run mode a
        mutating call is printed as "would <note> (METHOD path)" instead of sent, and returns None."""
        if self.dry_run and method in MUTATING:
            self.log(f"would {note} ({method} {path})" if note else f"would send {method} {path}")
            return None
        resp = self.send(method, path, params, json)
        if resp.status_code == 404 and missing_ok:
            return None
        if resp.is_error:
            raise APIError(resp.status_code, method, path, error_text(resp))
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            raise APIError(resp.status_code, method, path, "the answer is not JSON") from None

    def get(self, path: str, params: Mapping[str, Any] | None = None, *, missing_ok: bool = False) -> Any:
        return self.request("GET", path, params, missing_ok=missing_ok)

    def text(self, path: str) -> str:
        """GET an answer that is not JSON, such as a kubeconfig."""
        resp = self.send("GET", path)
        if resp.is_error:
            raise APIError(resp.status_code, "GET", path, error_text(resp))
        return resp.text

    def create(self, path: str, body: Mapping[str, Any], key: str, *, note: str, id_field: str = "id") -> dict:
        """POST a new resource and return the whole answer. In dry-run mode the answer is {key: placeholder},
        where the placeholder carries a dry-run id and "dry_run": true."""
        answer = self.request("POST", path, json=dict(body), note=note)
        if answer is None:
            fake = {k: body[k] for k in ("name", "region", "label") if k in body}
            return {key: {**fake, id_field: self.placeholder_id(), "dry_run": True}, "dry_run": True}
        return answer

    def post(self, path: str, body: Any = None, *, note: str) -> Any:
        return self.request("POST", path, json=body, note=note)

    def put(self, path: str, body: Any, *, note: str) -> Any:
        return self.request("PUT", path, json=body, note=note)

    def delete(self, path: str, *, note: str) -> bool:
        """True when DigitalOcean confirmed the delete (2xx), False when the resource was already gone (404)."""
        return self.request("DELETE", path, note=note, missing_ok=True) is not None

    def paginate(self, path: str, key: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        """Every item of a list endpoint: 200 per page, following links.pages.next, at most 100 pages."""
        items: list[dict] = []
        for page in range(1, 101):
            body = self.get(path, {**(params or {}), "page": page, "per_page": 200})
            items.extend(body.get(key) or [])
            if not ((body.get("links") or {}).get("pages") or {}).get("next"):
                break
        return items

    def wait_until(self, check: Callable[[], Any], timeout: float, interval: float, what: str) -> Any:
        """Call check() until it returns something truthy; raise WaitTimeout after timeout seconds of waiting."""
        waited = 0.0
        while True:
            result = check()
            if result:
                return result
            if waited >= timeout:
                raise WaitTimeout(f"gave up waiting for {what} after {timeout:.0f} s")
            self.sleep(interval)
            waited += interval

    def wait_action(self, action_id: Any, timeout: float = 600, interval: float = 5) -> dict:
        """Poll /v2/actions/{id} until the action completes; an errored action raises APIError."""
        if action_id is None or is_placeholder(action_id):
            return {}
        path = f"/v2/actions/{action_id}"

        def completed() -> dict | None:
            action = self.get(path).get("action") or {}
            if action.get("status") == "errored":
                raise APIError(0, "GET", path, f"action {action.get('type', '')} errored")
            return action if action.get("status") == "completed" else None

        return self.wait_until(completed, timeout, interval, f"action {action_id}")
