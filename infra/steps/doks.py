"""Step 8 of design section 9.2: the Kubernetes cluster kraken-doks in tor1, one s-2vcpu-2gb node, newest version.

infra/k8s/kraken.yaml is applied with kubectl through a kubeconfig in a temporary file that is deleted right after,
and without kubectl on PATH the commands are printed and the manifest is marked pending."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from functools import partial

from doapi import is_placeholder
from steps.common import INFRA, TAG, Context, StepError, find_named, last_line, note_urn, rel

NAME, REGION, SIZE = "kraken-doks", "tor1", "s-2vcpu-2gb"
MANIFEST = INFRA / "k8s" / "kraken.yaml"


def latest_version(versions: list[dict]) -> str:
    """The slug of the highest kubernetes_version in /v2/kubernetes/options."""
    def number(v: dict) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", str(v.get("kubernetes_version") or v.get("slug")))[:3])
    if not versions:
        raise StepError("/v2/kubernetes/options lists no Kubernetes versions")
    return max(versions, key=number)["slug"]


def ensure_doks(ctx: Context) -> None:
    vpc = ctx.require("vpc:tor1", "network")
    found = find_named(ctx.api.paginate("/v2/kubernetes/clusters", "kubernetes_clusters"), NAME)
    cluster, entry = ctx.resource("doks", "kubernetes", NAME, found, partial(_create, ctx, vpc["id"]), region=REGION)
    if is_placeholder(cluster["id"]):
        ctx.say(f"would apply {rel(MANIFEST)} to {NAME} once it is running")
        return
    cluster = ctx.wait(partial(_running, ctx, cluster["id"]), ctx.timeouts.kubernetes, f"{NAME} to be running")
    ctx.state.update("doks", version=cluster.get("version"))
    note_urn(ctx, "doks", REGION, f"do:kubernetes:{cluster['id']}")
    if entry["created"]:
        ctx.assign(f"do:kubernetes:{cluster['id']}")
    _apply(ctx, cluster["id"])


def _create(ctx: Context, vpc_id: str) -> dict:
    version = latest_version(ctx.api.get("/v2/kubernetes/options")["options"].get("versions") or [])
    # ha is set to false on purpose: newer versions default to a highly available control plane, which costs extra.
    body = {"name": NAME, "region": REGION, "version": version, "ha": False, "tags": [TAG],
            "node_pools": [{"name": "kraken-pool", "size": SIZE, "count": 1}]}
    if not is_placeholder(vpc_id):
        body["vpc_uuid"] = vpc_id
    return ctx.create("/v2/kubernetes/clusters", body, "kubernetes_cluster",
                      f"create Kubernetes cluster {NAME} (version {version})")


def _running(ctx: Context, cluster_id: str) -> dict | None:
    cluster = ctx.api.get(f"/v2/kubernetes/clusters/{cluster_id}")["kubernetes_cluster"]
    return cluster if (cluster.get("status") or {}).get("state") == "running" else None


def _apply(ctx: Context, cluster_id: str) -> None:
    digest = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()[:12]
    entry = ctx.state.get("doks") or {}
    if entry.get("manifest") == "applied" and entry.get("manifest_digest") == digest:
        ctx.say(f"exists {rel(MANIFEST)} on {NAME} (applied, unchanged since)")
        return
    if ctx.dry_run:
        ctx.say(f"would apply {rel(MANIFEST)} to {NAME} with kubectl")
        return
    if not ctx.which("kubectl"):
        ctx.show_commands(f"kubectl is not on PATH, so {rel(MANIFEST)} is not applied (pending)",
                          [["doctl", "kubernetes", "cluster", "kubeconfig", "save", NAME],
                           ["kubectl", "apply", "-f", rel(MANIFEST)]])
        ctx.state.update("doks", manifest="pending")
        return
    kubeconfig = ctx.api.text(f"/v2/kubernetes/clusters/{cluster_id}/kubeconfig")
    ctx.state.add_secret("the kubeconfig", kubeconfig)
    for token in re.findall(r"(?:token|client-key-data|client-certificate-data):\s*(\S+)", kubeconfig):
        ctx.state.add_secret("a kubeconfig credential", token)
    fd, path = tempfile.mkstemp(prefix="kraken-kubeconfig-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(kubeconfig)
        result = ctx.run(["kubectl", "--kubeconfig", path, "apply", "-f", str(MANIFEST)])
    finally:
        os.unlink(path)
    if result.returncode:
        ctx.say(f"warning: kubectl apply failed ({last_line(result.stderr)}); the manifest stays pending")
        ctx.state.update("doks", manifest="pending")
        return
    ctx.say(f"applied {rel(MANIFEST)} to {NAME}")
    ctx.state.update("doks", manifest="applied", manifest_digest=digest)
