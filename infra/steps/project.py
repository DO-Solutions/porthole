"""Steps 1 and 2 of design section 9.2: the project "insights-demo", which later steps assign their resources to,
and two tags.

"insights-demo" goes on every taggable resource, and "kraken-tentacle" marks the three tentacles so the firewall
covers them from first boot."""
from __future__ import annotations

from functools import partial

from steps.common import PROJECT, TAG, TENTACLE_TAG, Context, find_named

PROJECT_BODY = {"name": PROJECT, "purpose": "Operational / Developer tooling", "environment": "Development",
                "description": "Porthole: the kraken fleet that DigitalOcean Insights watches"}


def ensure_project(ctx: Context) -> None:
    found = find_named(ctx.api.paginate("/v2/projects", "projects"), PROJECT)
    ctx.resource("project", "project", PROJECT, found,
                 partial(ctx.create, "/v2/projects", PROJECT_BODY, "project", f"create project {PROJECT}"))
    for tag in (TAG, TENTACLE_TAG):
        answer = ctx.api.get(f"/v2/tags/{tag}", missing_ok=True)
        ctx.resource(f"tag:{tag}", "tag", tag, answer and answer.get("tag"),
                     partial(ctx.create, "/v2/tags", {"name": tag}, "tag", f"create tag {tag}", "name"),
                     id_field="name")
