"""Runs the head with the fake Insights and fake tentacles in one process on port 8080; no Docker, no token.

The fakes follow each other: a scenario started on the page lifts the fake metrics a minute later, the
round-trip rule fires, and its webhook comes back to /hooks/insights. Usage: python head/dev/run_local.py [--port N]"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for extra in (HERE, HERE.parent, HERE.parents[1] / "harness"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from fake_insights import FakeInsights  # noqa: E402
from fake_tentacles import FakeFleet  # noqa: E402
from porthole.config import Settings  # noqa: E402
from porthole.main import create_app  # noqa: E402

FLEET = HERE.parent / "tests" / "fixtures" / "fleet.json"
DEV_ENV = {
    "DIGITALOCEAN_TOKEN": "dev-token-for-the-local-fake",
    "PORTHOLE_CAPTAIN_KEY": "dev-captain-key-local-only-0000",
    "TENTACLE_KEY": "dev-key",
    "PORTHOLE_HOOK_BEARER": "dev-hook-bearer-local-only",
    "PORTHOLE_HOOK_SECRET": "dev-hook-secret-local-only",
    "PORTHOLE_TRUST_PROXY": "0",
    "PORTHOLE_INSIGHTS_BASE_URL": "http://insights-fake.local",
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORTHOLE_PORT", "8080")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--droplet-logs", action="store_true", help="pretend Droplet logs reach Insights (A6b fixed)")
    a = ap.parse_args()
    env = {**DEV_ENV, "PORTHOLE_FLEET_JSON": FLEET.read_text(),
           "PORTHOLE_PUBLIC_URL": f"http://{a.host}:{a.port}", **{k: v for k, v in os.environ.items()
                                                                 if k.startswith(("PORTHOLE_", "OTEL_"))
                                                                 and k != "PORTHOLE_FLEET_JSON"}}
    settings = Settings.from_env(env)
    fleet = FakeFleet(settings.fleet, key=settings.tentacle_key)
    hook = f"http://{a.host}:{a.port}/hooks/insights"
    holder: dict = {}

    def deliver(d: dict) -> None:
        def post() -> None:
            try:
                httpx.post(hook, content=d["body"], headers=d["headers"], timeout=5)
            except httpx.HTTPError:
                pass
        threading.Thread(target=post, daemon=True).start()

    fake = FakeInsights(settings.fleet, fleet, droplet_logs=a.droplet_logs, public_url=settings.public_url,
                        hook_bearer=settings.hook_bearer, hook_secret=settings.hook_secret,
                        head_logs=lambda: holder["app"].state.deps.log.records(300) if "app" in holder else [],
                        on_notify=deliver)
    app = create_app(settings, insights_transport=fake.transport(), tentacle_transport=fleet.transport())
    holder["app"] = app

    def ticker() -> None:
        while True:
            time.sleep(10)
            try:
                fake.tick()
            except Exception as e:  # keep the demo alive
                print(f"fake insights tick failed: {e}", file=sys.stderr)

    threading.Thread(target=ticker, daemon=True).start()
    print(f"Porthole on http://{a.host}:{a.port}  captain's key: {settings.captain_key}", file=sys.stderr, flush=True)
    uvicorn.run(app, host=a.host, port=a.port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
