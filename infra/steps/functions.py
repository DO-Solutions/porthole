"""Step 9 of design section 9.2: the functions namespace "kraken" in tor1, whose kraken/ping URL becomes FN_URL.

The code is deployed with doctl serverless deploy when doctl is on PATH; otherwise the commands are printed and the
deploy is marked pending."""
from __future__ import annotations

from functools import partial

import httpx

from doapi import is_placeholder
from steps.common import INFRA, Context, last_line, rel

# UNVERIFIED: that tor1 hosts functions namespaces and that a web function answers at
# <api_host>/api/v1/web/<namespace>/<package>/<function> (BUGS.md B-019).
LABEL, REGION, FUNCTION = "kraken", "tor1", "kraken/ping"
PROJECT_DIR = INFRA / "functions"


def ensure_functions(ctx: Context) -> None:
    namespaces = ctx.api.paginate("/v2/functions/namespaces", "namespaces")
    ctx.hide(namespaces, "a functions namespace")
    found = next((n for n in namespaces if n.get("label") == LABEL and n.get("region") == REGION), None)
    body = {"region": REGION, "label": LABEL}
    ns, _ = ctx.resource("functions", "functions_namespace", LABEL, found,
                         partial(ctx.create, "/v2/functions/namespaces", body, "namespace",
                                 f"create functions namespace {LABEL} in {REGION}", "namespace"),
                         id_field="namespace", region=REGION)
    ctx.hide(ns, "the functions namespace")
    host = str(ns.get("api_host") or "").rstrip("/")
    url = f"{host}/api/v1/web/{ns['namespace']}/{FUNCTION}" if host else ""
    ctx.state.update("functions", api_host=host, url=url)
    ctx.state.update("functions", deploy=_deploy(ctx, ns["namespace"], url))


def _deploy(ctx: Context, namespace: str, url: str) -> str:
    if url and answers(ctx, url):
        ctx.say(f"exists function {FUNCTION} (answers at {url})")
        return "done"
    hint = "<namespace id>" if is_placeholder(namespace) else namespace
    shown = [["doctl", "serverless", "connect", hint], ["doctl", "serverless", "deploy", rel(PROJECT_DIR)]]
    if ctx.dry_run:
        ctx.say(f"would deploy function {FUNCTION} with: {' && '.join(' '.join(c) for c in shown)}")
        return "pending"
    if not ctx.which("doctl"):
        ctx.show_commands(f"doctl is not on PATH, so function {FUNCTION} is not deployed (pending)", shown)
        return "pending"
    for cmd in (shown[0], ["doctl", "serverless", "deploy", str(PROJECT_DIR)]):
        result = ctx.run(cmd, ctx.doctl_env())
        if result.returncode:
            ctx.say(f"warning: {' '.join(cmd[:3])} failed ({last_line(result.stderr)}); the deploy stays pending")
            return "pending"
    ctx.say(f"deployed function {FUNCTION} with doctl")
    return "done"


def answers(ctx: Context, url: str) -> bool:
    """True when the function URL returns {"pong": true}."""
    try:
        resp = ctx.web.get(url, timeout=10)
        return resp.status_code == 200 and resp.json().get("pong") is True
    except (httpx.HTTPError, ValueError, AttributeError):
        return False
