# Alert rule templates

`round-trip.json` holds one alert rule per operator and evaluation window of the plan (feature A1). Each entry has
a `purpose`, the `target` tentacle, the `status` it is created with, and a `spec` in the Insights API shape with two
placeholders:

- `{{URN}}`: the URN of the target tentacle, `do:droplet:<id>`.
- `{{CHANNEL}}`: the id of the webhook channel `kraken-head`, which points at `/hooks/insights`.

`infra/provision.py` (step 14, insights) substitutes both, creates each rule with the work token, and records the
rule ids in `infra/out/state.json`; `infra/fleet.py` copies them into `PORTHOLE_FLEET_JSON` under `watcher.rules`.
The head reads rules only by id, because the list endpoint leaves out the rules mirrored from legacy Monitoring
policies (findings A2, A39).

| purpose | metric | operator | window | status |
|---|---|---|---|---|
| round-trip | `do.droplets.cpu_utilization` | `>=` 40 warning, 60 critical | 1m | active |
| operator-gt-5m | `do.droplets.load_avg_1m` | `>` 0.9 / 1.5 | 5m | paused |
| operator-lte-10m | `do.droplets.memory_available` | `<=` 250 / 150 MiB | 10m | paused |
| operator-lt-15m | `do.droplets.filesystem_free` | `<` 17 / 16 GiB | 15m | paused |
| operator-eq-30m | `do.droplets.load_avg_15m` | `=` 0 | 30m | paused |
| operator-ne-1h | `do.droplets.load_avg_1m` | `!=` 0 | 1h | paused |

Only the round-trip rule is active; the Alert round trip voyage burns CPU on its target and times every hop. The
paused rules cover the remaining operators and windows; resume one (Alerts page, write mode) before a scenario that
should trip it. The local fake Insights seeds itself from this same file, so tests and `run_local.py` use the rules
provisioning would create.
