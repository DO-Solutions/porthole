"""Step 12 of design section 9.2: the Harness Runtime session "kraken-brain", created with doctl and paused.

No REST shapes are published and only `create --prompt` is documented, so the step runs only when doctl has the
command and its help lists the flags used here; otherwise it prints the command and records the session as pending."""
from __future__ import annotations

import json
import re

from state import now_iso
from steps.common import Context, StepError, last_line

# UNVERIFIED: the size slug, the region and every flag of create_command(); doctl 1.166.0 has no harness-runtime
# command, so the step checks the help text before it runs anything (BUGS.md B-007).
NAME, SIZE, REGION = "kraken-brain", "mars-1vcpu-1gb", "tor1"
PROMPT = "You are the Kraken Brain of the Porthole demo. Wait for questions about the fleet."


def create_command() -> list[str]:
    return ["doctl", "harness-runtime", "create", "--name", NAME, "--size", SIZE, "--region", REGION,
            "--prompt", PROMPT, "--output", "json"]


def doctl_help(ctx: Context, *words: str) -> str | None:
    """The help text of `doctl harness-runtime <words>`, or None when doctl or the command is missing."""
    if not ctx.which("doctl"):
        return None
    result = ctx.run(["doctl", "harness-runtime", *words, "--help"])
    return result.stdout if result.returncode == 0 and result.stdout else None


def has_verb(help_text: str | None, verb: str) -> bool:
    return bool(help_text and re.search(rf"^\s+{verb}\b", help_text, re.MULTILINE))


def ensure_agent(ctx: Context) -> None:
    old = ctx.state.get("agent") or {}
    if old.get("status") in ("created", "paused"):
        ctx.say(f"exists Harness Runtime session {NAME} ({old['status']})")
        return
    if ctx.dry_run:
        ctx.say(f"would create Harness Runtime session {NAME} with doctl and pause it")
        return
    group = doctl_help(ctx)
    create = doctl_help(ctx, "create") if has_verb(group, "create") else None
    unknown = [flag for flag in create_command() if flag.startswith("--") and flag not in (create or "")]
    if create is None or unknown:
        why = (f"doctl harness-runtime create does not take {', '.join(unknown)}" if create
               else "this doctl has no harness-runtime command (or doctl is not on PATH)")
        ctx.show_commands(f"{why}, so session {NAME} is not created (pending)", [create_command()])
        ctx.state.record("agent", step=ctx.step, kind="agent", ident=None, name=NAME, created=False,
                         status="pending", region=REGION)
        return
    result = ctx.run(create_command(), ctx.doctl_env())
    if result.returncode:
        raise StepError(f"doctl harness-runtime create failed: {last_line(result.stderr)}")
    ident = session_id(result.stdout) or NAME
    ctx.state.record("agent", step=ctx.step, kind="agent", ident=ident, name=NAME, created=True,
                     created_at=now_iso(), status="created", region=REGION)
    ctx.say(f"created Harness Runtime session {NAME} (id {ident})")
    if not has_verb(group, "pause"):
        ctx.say("this doctl cannot pause a session, so it stays as created")
        return
    paused = ctx.run(["doctl", "harness-runtime", "pause", ident], ctx.doctl_env())
    if paused.returncode:
        ctx.say(f"warning: doctl harness-runtime pause failed ({last_line(paused.stderr)})")
        return
    ctx.state.update("agent", status="paused")
    ctx.say(f"paused Harness Runtime session {NAME}")


def session_id(stdout: str) -> str | None:
    """The id from doctl's JSON output, which may be one object or a list of them."""
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    if isinstance(data, list):
        data = data[0] if data else {}
    return str(data.get("id")) if isinstance(data, dict) and data.get("id") else None
