"""Creates the kraken fleet through the DigitalOcean API, one idempotent step at a time (design section 9.2).

Each step looks for its resources by name and tag, creates only what is missing, and records ids, names, IPs and
URNs in infra/out/state.json; --plan prints the order without a token or any API call."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import httpx

import steps
from doapi import APIError, DOClient, WaitTimeout
from state import SecretLeak, State
from steps.common import Context, RunResult, StepError, Timeouts, run_command, run_sql

OUT = Path(__file__).resolve().parent / "out"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provision.py", description="Create what is missing of the kraken fleet and record it in "
        "infra/out/state.json. Run it again at any time: existing resources are found and left alone.")
    parser.add_argument("--only", metavar="STEP", help="run only these steps: a name such as droplets, a design "
                        "step number such as 5, or a comma separated list")
    parser.add_argument("--dry-run", action="store_true", help="read the account and print what would be "
                        "created; send no POST, PUT, PATCH or DELETE and write no files")
    parser.add_argument("--plan", action="store_true", help="print the step order and exit; no token, no API call")
    return parser


def main(argv: list[str] | None = None, *, env: Mapping[str, str] | None = None,
         transport: httpx.BaseTransport | None = None, web_transport: httpx.BaseTransport | None = None,
         runner: Callable[..., RunResult] = run_command, which: Callable[[str], str | None] = shutil.which,
         sleep: Callable[[float], None] = time.sleep, out_dir: Path = OUT, timeouts: Timeouts | None = None,
         sql: Callable[..., None] = run_sql) -> int:
    """The CLI. The keyword arguments are for tests: fake transports, a fake runner, a fake SQL runner and a sleep
    that returns."""
    args = build_parser().parse_args(argv)
    if args.plan:
        print("\n".join(steps.plan_lines()))
        return 0
    env = os.environ if env is None else env
    try:
        selected = steps.select(args.only)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    missing = steps.missing_env(selected, env)
    if missing:
        print("error: " + "\n       ".join(missing), file=sys.stderr)
        return 2
    try:
        state = State.load(out_dir / "state.json", env, readonly=args.dry_run)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    api = DOClient(env["DIGITALOCEAN_TOKEN"].strip(), transport=transport, dry_run=args.dry_run, sleep=sleep)
    ctx = Context(api=api, state=state, env=env, web=httpx.Client(transport=web_transport, timeout=10),
                  out_dir=out_dir, dry_run=args.dry_run, which=which, run=runner, sql=sql, insights_transport=transport,
                  timeouts=timeouts or Timeouts())
    try:
        for step in selected:
            ctx.step = step.name
            numbers = ", ".join(str(n) for n, _ in step.design)
            print(f"== {step.name} (design step {numbers})")
            try:
                steps.run(step, ctx)
            except (APIError, StepError, WaitTimeout) as e:
                if not step.optional:
                    raise
                print(f"warning: optional step {step.name} failed and was skipped: {e}", file=sys.stderr)
    except (APIError, StepError, WaitTimeout, SecretLeak) as e:
        print(f"error in step {ctx.step}: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(f"\ninterrupted in step {ctx.step}; state.json holds what was recorded so far", file=sys.stderr)
        return 130
    finally:
        ctx.close()
    print("dry run finished: nothing was changed" if args.dry_run else "done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
