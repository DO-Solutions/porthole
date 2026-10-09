# Porthole

Porthole is a public web page onto DigitalOcean Insights. It runs a small fleet of demo resources (three
Droplets called tentacles, a load balancer, a managed Postgres, a one-node Kubernetes cluster, a Function, a Spaces
bucket, a registry and a Managed Agents session), makes that fleet do things on demand, and shows what Insights
records about it through the Insights API. Every API call the page makes is listed as it happens, and alert
webhooks come back to the page so an alert round trip can be watched end to end.

The head is a FastAPI app on App Platform, so it is itself a resource Insights watches. It uses the Insights
harness (`harness/`) as a library and drives the tentacles (`tentacle/`) over their HTTP API; both are described
under "Harness and tentacle" below. Provisioning and teardown live in `infra/`. Nothing in this repo is a secret.

## The pages

| path | what it shows |
|---|---|
| `/` Bridge | the fleet with a dot for each member Insights has data for, the head's own request rate, and "the water": CPU of every tentacle |
| `/stir` | scenarios (cpu, memory, disk, network, logs, chain, pg, fn, lb) and voyages, the one-click sequences with timed steps |
| `/metrics` | the metric catalog per region, builder-mode range charts, `both` regions overlaid, raw PromQL for the captain |
| `/dashboards` | the Kraken's Eye dashboard as a file to import, and each of its queries run here |
| `/alerts` | rules by id, instances, channels, and the webhook deliveries with their headers and signature verdict |
| `/logs` | what Insights returns, what the head emitted, and what the tentacles say they wrote during a log storm |
| `/traces` | chain runs with trace ids, the head's own spans, and where those spans were exported |
| `/api` | every upstream call with parameters, status, timing, "Copy as curl" and "Copy as BUGS.md entry" |
| `/brain` | the Kraken's Brain: a scripted deckhand that answers questions and asks before it changes anything |

## Run it locally

Without Docker, everything in one process against the fakes (fake Insights, fake tentacles, real head):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python head/dev/run_local.py            # http://127.0.0.1:8080, captain's key: dev-captain-key-local-only-0000
```

With Docker, the head, two real tentacles and the fake Insights:

```bash
cp head/.env.example head/.env
make dev                                 # docker compose up --build
```

Checks: `make test` (the four suites), `make lint` (ruff), `make smoke` (starts the local runner and fetches
every page and route), `make leak-sweep` (secret shapes, lab names and public IPs outside the documentation ranges),
`make audit` (pip-audit). CI runs the same on every push. `make vendor` downloads uPlot only if its files are
missing; they are committed with their hashes in `head/static/vendor/uplot/VENDOR.md`.

## Deploy

The app is defined in `.do/app.yaml` (region `tor`, one instance of `apps-s-1vcpu-0.5gb`, health check
`/healthz`). Create it once with `doctl apps create --spec .do/app.yaml` or through `infra/provision.py --only app`,
then set the SECRET variables in the control panel or through the provisioner. After that, a push to `main`
redeploys. Confirm a deploy with `curl https://insights-demo.digitalocean.solutions/healthz`, which reports the
version (the git SHA when the image was built with `--build-arg PORTHOLE_VERSION=$(git rev-parse --short HEAD)`).
Roll back with App Platform's previous deployment. The head must run as one instance with one worker: its
stores are in memory.

## Provision the fleet

`infra/` creates the fleet with the DigitalOcean API, idempotently, and writes ids, names, IPs and URNs to
`infra/out/state.json` (gitignored). It never writes a secret to disk. See [infra/README.md](infra/README.md)
for the variables to export and the order of steps.

```bash
python infra/provision.py --plan         # the step order, no API calls
python infra/provision.py                # create what is missing, record what exists
python infra/fleet.py                    # write infra/out/porthole.env with PORTHOLE_FLEET_JSON
python infra/teardown.py                 # list what would be deleted; add --yes to delete it
```

## Configuration

Environment variables only. `head/.env.example` lists each one with a comment.

| variable | type | default | purpose |
|---|---|---|---|
| `DIGITALOCEAN_TOKEN` | secret | | Insights token for the head: `insights:read`, plus `insights:update` for write mode |
| `PORTHOLE_CAPTAIN_KEY` | secret | | shared key for mutating routes, 24 characters or more; unset means they return 503 |
| `TENTACLE_KEY` | secret | | bearer the tentacles expect |
| `PORTHOLE_FLEET_JSON` | general | `{}` | the fleet description from `infra/fleet.py` |
| `PORTHOLE_HOOK_BEARER` | secret | | bearer configured on the Insights webhook channel |
| `PORTHOLE_HOOK_BASIC` | secret | | `user:password` configured on the channel, instead of the bearer |
| `PORTHOLE_HOOK_SECRET` | secret | | the channel's signing secret; enables the signature checks |
| `PORTHOLE_PUBLIC_URL` | general | `https://insights-demo.digitalocean.solutions` | used in curl output and the webhook URL shown |
| `PORTHOLE_INSIGHTS_WRITE` | general | `0` | `1` shows Pause and Resume on the Alerts page |
| `PORTHOLE_INSIGHTS_BASE_URL` | general | `https://api.digitalocean.com` | the fake uses `http://insights-fake:9000` |
| `PORTHOLE_TRUST_PROXY` | general | `1` | read the client address from the first `X-Forwarded-For` hop |
| `PORTHOLE_DEEPLINKS_JSON` | general | | overrides for control-panel link patterns, keyed as in `head/porthole/deeplinks.py` |
| `PORTHOLE_DO_CONTEXT` | general | | team context id, the `i=` parameter of the Insights tab links; empty leaves it out |
| `PORTHOLE_UPSTREAM_BUDGET_PER_MIN` | general | `200` | Insights calls per minute before panels serve cached data |
| `PORTHOLE_CACHE_TTL_S` | general | `20` | panel cache lifetime |
| `PORTHOLE_BRAIN` | general | `deckhand` | `deckhand`, `harness-runtime` or `off` |
| `PORTHOLE_BRAIN_SESSION` | general | | phase 2: Harness Runtime session name |
| `PORTHOLE_BRAIN_TOKEN` | secret | | phase 2: token of the session owner, for approvals |
| `PORTHOLE_GATEWAY_MCP_URL` | secret | | phase 2: the Action Gateway session's MCP URL |
| `PORTHOLE_MCP_KEY` | secret | | phase 2: key the head's MCP server requires |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | general | | OTLP/HTTP base; unset means spans stay in memory only |
| `OTEL_EXPORTER_OTLP_HEADERS` | secret | | headers for the OTLP exporters |
| `OTEL_SERVICE_NAME` | general | `porthole` | `service.name` on spans and logs |
| `PORTHOLE_LOG_LEVEL` | general | `INFO` | `DEBUG`, `INFO`, `WARN` or `ERROR` |
| `PORTHOLE_PORT` | general | `8080` | listen port for `python -m porthole.main`, run from `head/`; the container always listens on 8080 |
| `PORTHOLE_VERSION` | general | | version shown in `/healthz` |

Missing values never crash the head. It starts in a degraded mode, logs one line per problem, lists the problems
in `/healthz`, and the pages say what is missing.

## The captain's key

Reading is open to everyone. Starting scenarios and voyages, raw PromQL, free-form log filters, pausing rules and
approving the Brain's actions need the captain's key: a shared secret of at least 24 characters in
`PORTHOLE_CAPTAIN_KEY`. On the page, the key button stores it in `sessionStorage` for the tab and sends it as
`X-Captain-Key`. The server compares it in constant time, rate limits every attempt (12 a minute per address, 60
overall), and clamps every parameter to the public caps whatever the page sends. Rotate it by changing the
variable; the next deploy picks it up.

## When something is wrong

1. `curl .../healthz`: the version, whether Insights is configured, and the list of problems.
2. The API page: every Insights, tentacle, Function and load balancer call with status and timing. "Copy as
   curl" reproduces a call with your own token; "Copy as BUGS.md entry" fills the template.
3. The app's runtime logs in App Platform: one JSON object per line. The Logs page shows the last 300.
4. A tentacle: `curl http://<ip>:8800/health` and `/scenarios` are open; `journalctl -u tentacle` on the box.
5. Insights itself: [BUGS.md](BUGS.md) lists what is unverified or known to differ from the docs.
6. A chart or voyage reads "no data" while the resource is busy: drop matchers one at a time. Labels vary per
   metric, even on one Droplet: `do.droplets.cpu_utilization` carries only `__name__`, `do_tags`, `resource_urn`
   and `service_name`, while the same Droplet's other series also carry `resource_region_slug` (B-023, B-026).
   `GET .../prom/api/v1/series?match[]={resource_urn="do:droplet:<id>"}` with a `start` and `end` lists each
   series with its labels. Select by `resource_urn`; the region is the path segment of the call, not a label.

Security posture and how to report a problem: [SECURITY.md](SECURITY.md).

## Harness and tentacle

Both are carried over unchanged from the core build and work on their own.

The harness (`harness/insights_harness.py`, needs httpx) is a library and CLI over the Insights API. The token
comes only from `DIGITALOCEAN_TOKEN` and is never printed; `--trace` prints each request and response to stderr
with write-only secrets masked. A 429 waits for `ratelimit-reset` (at most 120 s) and retries once.

```bash
python3 harness/insights_harness.py <group> <verb> [args] [--region R] [--json] [--trace]
```

The groups are `channels` and `rules` (list, get, create, update, and delete with `--yes`), `instances`, `prom`
(query, range, labels, values, series; discovery calls always send a window), `logs` (search, iter) and `probe`.
Each probe reproduces a finding of the facts pack (`labels-window` A1, `rules-visibility` A2, `regions` A3,
`naming` A4, `endpoints` A5, `tags` A8, `logs-api` L3) and prints one PASS, FAIL or INFO line per check. Write
probes need `--write`, create paused rules with an unreachable threshold, and delete them even when a step fails.

The tentacle (`tentacle/tentacle.py`, FastAPI) is the scenario service on each Droplet:

| variable | default | meaning |
|---|---|---|
| `TENTACLE_NAME` | hostname | name in logs, spans and `/health` |
| `TENTACLE_KEY` | | bearer for every endpoint except `/health` and `GET /scenarios`; unset means they return 503 |
| `TENTACLE_PORT` | `8800` | listen port |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://127.0.0.1:4318` | OTLP/HTTP base for traces and logs |
| `OTEL_SERVICE_NAME` | `TENTACLE_NAME` | `service.name` on spans and log records |
| `PEER_URL` | | another tentacle: network target and hop 2 of chains; it must share `TENTACLE_KEY` |
| `PG_DSN`, `FN_URL` | | managed Postgres and the Function, for the `pg` scenario and the chain legs |
| `TENTACLE_LOG_FILE`, `TENTACLE_DATA_DIR` | `/var/log/tentacle/tentacle.jsonl`, `/var/tmp/tentacle` | JSON log file, disk-fill files |

`POST /scenario/{cpu,memory,disk,network,logs,chain,pg}` starts a bounded run, at most 8 at once; memory and disk
answer 409 when they would starve the machine. `POST /scenario/stop/{id}` stops one; `POST /sink` and
`GET /chain/hop` serve the network and chain scenarios. Each run logs its start and end and emits a
`scenario.<name>` span. Log lines are JSON on stdout and in the log file; traces and logs go out over OTLP/HTTP and
fail quietly when no collector listens. `tentacle/install.sh` is idempotent and works as `user_data` with
`export` lines right after the shebang (`infra/steps/droplets.py` writes them). It writes `/etc/tentacle/env`
once (`TENTACLE_ENV_OVERWRITE=1` rewrites it) and runs the service under systemd.

## Layout

```
head/       the app (porthole/), static pages, dev fakes and runner, smoke.sh, tests
harness/    Insights API library and CLI, carried over unchanged
tentacle/   the scenario service for the Droplets, carried over unchanged, plus Dockerfile.dev
infra/      provisioning and teardown through the DigitalOcean API
watcher/    alert rule templates, the dashboard file and its query list, probe metrics, the skin, the phase 2 Brain spec
scripts/    the leak sweep and the uPlot vendoring used by make and CI
.do/        the App Platform spec
```

MIT licensed, see [LICENSE](LICENSE).
