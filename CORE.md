# Insights demo suite — core

Two self-contained Python 3.12 components for the DigitalOcean Insights demo (see `DEMO-PLAN.md`):

| path | what | dependencies |
|---|---|---|
| `harness/insights_harness.py` | library + CLI over the Insights API, plus `probe` subcommands that reproduce the findings in the facts pack | `httpx` |
| `tentacle/tentacle.py` | the scenario service on each demo Droplet: CPU, memory, disk, network, log storms, traced request chains, PG load | FastAPI, uvicorn, OpenTelemetry, psycopg |

```
out/
├── README.md
├── requirements-dev.txt          # both components + pytest + PyYAML (tests only)
├── harness/
│   ├── insights_harness.py
│   ├── requirements.txt
│   └── tests/
│       ├── test_harness.py
│       └── openapi/*.yml         # Insights models from digitalocean/openapi, for offline schema checks
└── tentacle/
    ├── tentacle.py
    ├── tentacle.service
    ├── install.sh
    ├── requirements.txt
    └── tests/test_tentacle.py
```

Tests (no network, no DO account):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r out/requirements-dev.txt
(cd out/harness && pytest -q)
(cd out/tentacle && pytest -q)
```

## Harness

```bash
export DIGITALOCEAN_TOKEN=dop_v1_...        # scopes insights:read (+ create/update/delete for writes)
python3 harness/insights_harness.py <group> <verb> [args] [--region R] [--json] [--trace]
```

The token comes only from `DIGITALOCEAN_TOKEN`. It is never printed or logged. `--trace` prints each request
(method, path, params, body) and each response (status, content type, time) to stderr for the demo page.
Write-only secrets in request bodies (`token`, `password`, `secret`, `webhook_url`) show as `***` in the trace.
`--region` defaults to `$INSIGHTS_REGION`. It is needed for `prom`/`logs`. Probes default to `nyc3`.

| group | verbs |
|---|---|
| `channels` | `list`, `get ID`, `create`, `update ID` (`--file body.json`, or `--name` + one of `--email TO` / `--slack-url U --slack-channel C` / `--webhook-url U [--bearer T \| --basic u:p] [--secret S] [--header K=V]`), `delete ID --yes` |
| `rules` | `list [--resource-urn]`, `get ID`, `create`, `update ID` (`--file spec.json`, or `--name --metric --op` `[--warning N] [--critical N] [--window 5m] [--urn U] [--tag T] [--filter host_id=1] [--channel ID[:warning,critical]] [--re-alert 4h] [--status paused]`), `delete ID --yes` |
| `instances` | `list [--status active\|resolved] [--rule-id] [--resource-urn]`, `get ID` |
| `prom` | `query Q [--time]`, `range Q [--start --end --step]`, `labels`, `values NAME`, `series --match SEL` (the last three always send a window, by default the last 30 min) |
| `logs` | `search` / `iter` with `--start now-1h --end now --severity ERROR --service S --text T --filter-json '{…}' --order -timestamp --limit 100 [--cursor C]` |
| `probe` | see below |

Library:

```python
from insights_harness import Insights, rule_spec, cond, and_, or_, not_, text, order
ins = Insights(token, region="tor1", trace=True)
ch = ins.create_channel(ins.webhook_channel("head", "https://insights-demo.digitalocean.solutions/hooks/insights",
                                            bearer=hook_token, secret=hook_secret, headers={"X-Kraken": "1"}))
ins.create_rule(rule_spec("Churn", "do.droplets.cpu_utilization", ">=", warning=60, critical=90, window="1m",
                          resource_urns=["do:droplet:123"],
                          channels=[(ch["notification_channel"]["id"], ["warning", "critical"])]))
ins.query("avg by (resource_name) (do.droplets.cpu_utilization)")
for rec in ins.iter_logs("now-1h", "now", filter=and_(cond("service.name", "=", "tentacle-a"),
                                                       cond("severity_number", ">=", 17))):
    print(rec["timestamp"], rec.get("body"))
```

- Each call returns parsed JSON. A non-2xx or non-JSON response raises `InsightsError(status, body, request_summary)`.
- On HTTP 429 the client sleeps until `ratelimit-reset` (at most 120 s), then retries once.
- Short aliases map to the exact enums in the OpenAPI spec: `>=` maps to `THRESHOLD_OPERATOR_GREATER_THAN_OR_EQUAL`, `5m` to
  `EVALUATION_WINDOW_5M`, `never` to `RE_ALERT_DURATION_NEVER`, `critical` to `SEVERITY_CRITICAL`, and `in` to `FILTER_OPERATOR_IN`.
  Full enum strings work too.
- Logs filter builders produce the `logs_filter_expression` tree exactly. `cond(field, op, value, scope=None)`
  picks `string_value` / `number_value` / `bool_value` / `string_array_value` / `number_array_value` from the
  Python type. `exists` takes no value. The tests validate the output against the vendored OpenAPI models.
- Time instants: `datetime` and RFC3339 strings become `absolute`, `now`, `now-1h` and `15m` become `relative`,
  and numbers (unix seconds) become `unix_nano`.
- `iter_logs` orders by `timestamp` desc by default, because cursors only work with timestamp ordering. It
  follows `pagination.next_cursor` while `has_more` is true.

### Probes

Every line is `PASS|FAIL|INFO <probe>: <check> — <status and body excerpt>`. PASS means the observed behavior matches the
expectation in the facts pack. FAIL means it differs. INFO is context. The exit code is 1 if any check FAILed. `--json` gives
a machine-readable list.

| probe | finding | what it does |
|---|---|---|
| `probe labels-window` | A1 | `labels` with no window (30 s timeout; expects an error or a timeout), then with a 30-min window (expects 200) |
| `probe rules-visibility` | A2 | lists rules and channels (`usage.rule_count`), collects every `rule_id` referenced by alert instances, and GETs each one. A rule that GET returns but the list omits is a FAIL (A2 reproduced) |
| `probe regions [--only R]` | A3 | `count(do.droplets.cpu_utilization)` in all 16 listed regions. Reports status, content type, and series count. A non-JSON body (the mkc1 maintenance page) is a FAIL |
| `probe naming CHANNEL_ID --write` | A4 | creates a rule with `do_droplets_cpu_utilization` (expects 422), then `do.droplets.cpu_utilization` (expects 201). Checks whether the new rule shows in the list (the A2 follow-up), then deletes it |
| `probe tags CHANNEL_ID --write [--tag T]` | A8 | creates a rule with `query.tags`, reports whether the API accepted or rejected it, reads it back to see if the tags round-trip, then deletes it |
| `probe endpoints` | A5 | `/v2/insights/prom/query` and `/v2/insights/metrics/query` (expect 404), then `/v2/insights/query/{region}/prom/api/v1/query` (expects 200) |
| `probe logs-api` | L3 | `{}` body (expects 400 `time_range is required`), a 1-h window with `limit=5`, `severity_number >= 17`, `severity_text IN [...]`, then timestamp ordering and one cursor page. Reports record keys, `has_more`, and timing |

Write-mode probes refuse to run without `--write`. The rules they create are **paused**, with an unreachable threshold
(critical > 100000), so they never notify. A `try/finally` deletes them even when a step fails. The harness deletes
only what it created. The `channels delete` and `rules delete` verbs act only on the id you pass, and only with `--yes`.

## Tentacle

```bash
pip install -r tentacle/requirements.txt
TENTACLE_NAME=tentacle-a TENTACLE_KEY=secret PEER_URL=http://10.0.0.3:8800 python3 tentacle/tentacle.py
```

| env | default | meaning |
|---|---|---|
| `TENTACLE_NAME` | hostname | name in logs, spans, `/health` |
| `TENTACLE_KEY` | — | bearer for every endpoint except `/health` and `GET /scenarios`. If unset, those endpoints return 503 |
| `TENTACLE_PORT` | `8800` | listen port (0.0.0.0) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://127.0.0.1:4318` | OTLP/HTTP base; traces go to `/v1/traces`, logs to `/v1/logs` |
| `OTEL_SERVICE_NAME` | `TENTACLE_NAME` | `service.name` resource attribute and log key |
| `PEER_URL` | — | another tentacle (network blast target, hop 2 of chains). The peer must share `TENTACLE_KEY` |
| `PG_DSN` | — | managed PostgreSQL (`/scenario/pg`, PG leg of chains) |
| `FN_URL` | — | Function URL (HTTP GET leg of chains) |
| `TENTACLE_LOG_FILE` | `/var/log/tentacle/tentacle.jsonl` | JSONL log file (rotated at 200 MB to `.jsonl.1`) |
| `TENTACLE_DATA_DIR` | `/var/tmp/tentacle` | disk-fill files |

| endpoint | params (defaults) | does |
|---|---|---|
| `GET /health` | | `{name, uptime_s, running, load1, mem_pct}` (open, excluded from tracing) |
| `GET /scenarios` | | `{running: [...], finished: [...]}` with params, start and end times, elapsed time, result, error |
| `POST /scenario/cpu` | `seconds=120 workers=<cpu count>` | one spawned busy-loop process per worker |
| `POST /scenario/memory` | `seconds=120 mb=600` | allocates and touches `mb` MiB, holds it, then frees it. Returns 409 if that would leave less than 100 MB available |
| `POST /scenario/disk` | `seconds=120 mb=1024` | writes `/var/tmp/tentacle/<id>.bin` with fsync, keeps it, then deletes it. Returns 409 if that would leave less than 1 GB free |
| `POST /scenario/network` | `seconds=60 mbps=50` | paced POSTs to `PEER_URL/sink` (4 per second) |
| `POST /sink` | | reads and discards the body, returns `{bytes}` |
| `POST /scenario/logs` | `seconds=60 rate=50 error_pct=10` | `rate` lines/s. Severity mix: `error_pct` ERROR; the rest is 70 % INFO, 15 % WARN, 15 % DEBUG. Lines are emitted inside a span per second, so they carry trace ids |
| `POST /scenario/chain` | `count=20 latency_ms=0 error_pct=0` | `count` requests. Each is its own trace: `chain.request` → this tentacle's `/chain/hop` (hop 1) → `PEER_URL/chain/hop` (hop 2), plus `pg.select_now` and `fn.call` legs per hop when configured. Injected latency and errors happen per hop. Each request links to the `scenario.chain` span |
| `GET /chain/hop` | `hop latency_ms error_pct` | one hop (bearer required: it can hit PG and the Function) |
| `POST /scenario/pg` | `seconds=60 clients=4` | creates `tentacle_load` once, then each client runs a loop of inserts and reads, with occasional trims |
| `POST /scenario/stop/{id}` | | stops a running scenario and waits up to 10 s for it to wind down |

Each scenario runs in a background thread, at most 8 at once. Each one logs its start and end with its parameters and emits a span named
`scenario.<name>`. Log lines are one JSON object per line, on stdout and in the JSONL file:
`timestamp`, `severity_text` (DEBUG/INFO/WARN/ERROR), `severity_number` (5/9/13/17), `body`, `service.name`,
`trace_id` and `span_id` inside a span, and flat scenario attributes (`scenario.name`, `scenario.id`,
`scenario.<param>`, `result.<key>`). The same records go to the OTel logs pipeline. Traces and logs are exported over
OTLP/HTTP through batch processors on background threads. When the collector is absent, exports fail quietly (exporter loggers are
silenced, queues are bounded and drop) and requests are never blocked.

### Install on a Droplet

`install.sh` is idempotent and works as DigitalOcean `user_data` (it starts with `#!/bin/bash`). It installs
`python3-venv`, creates the `tentacle` system user, `/opt/tentacle` (code and venv), `/etc/tentacle/env` (0640, written
once; `TENTACLE_ENV_OVERWRITE=1` rewrites it), `/var/log/tentacle` and `/var/tmp/tentacle`. It installs the pinned
`requirements.txt`, writes the systemd unit, and enables and restarts it.

- From a checkout: `sudo TENTACLE_KEY=… PEER_URL=http://<peer-private-ip>:8800 ./tentacle/install.sh`
- As user_data: put `export TENTACLE_KEY=… PEER_URL=… TENTACLE_TARBALL_URL=https://…/insights-demo.tar.gz` right
  after the shebang. The tarball must contain `tentacle/tentacle.py`, `tentacle/requirements.txt` and
  `tentacle/tentacle.service`.

Port 8800 must be reachable from the head, the LB, and the peer. Restrict it with a DO Cloud Firewall.
