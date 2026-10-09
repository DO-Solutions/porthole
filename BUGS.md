# BUGS.md

One entry per surprise, written when it happens, with the exact request and response. The API page's "Copy as
BUGS.md entry" fills the request and response lines from any call in the trace. Entries start "to verify" when the
design could not confirm a fact; they move to open once someone observes the behavior, then to reported, fixed or
wontfix. Never write an observation nobody made.

```
## B-000  Alert webhook carries no signature header     (open)
- when: 2026-10-12T14:03:11Z   where: head /hooks/insights   finding: A14 follow-up
- request we made / received: POST /hooks/insights, headers [user-agent, content-type, authorization(Bearer), x-kraken], 1,212 bytes
- response: 200
- expected: a signature header, per "Sign webhook payload" in manage-metrics-alerts (quote)
- observed: no header containing sign/signature/hmac/digest; body excerpt {...}
- reproduce: run voyage alert-round-trip; harness: `insights_harness.py channels get <id>`
- status: open -> reported -> fixed / wontfix
```

## B-001  Insights tab URLs are not documented     (verified)
- when: 2026-10-09 (build); URLs read 2026-10-09 ~00:20Z by Darian   where: head `porthole/deeplinks.py`, design Appendix A   finding: none
- request we made / received: none; the docs give only the menu path DATA & LEARNING > Insights > tab
- response: n/a
- expected: a stable URL per tab (Metrics, Dashboards, Alerts, Logs, Traces), with region and range in the query
- observed: `https://cloud.digitalocean.com/insights/<tab>?i=<context>&region=<region>&from=now-1h&to=now` for metrics, dashboards, alerts, logs and traces; the control panel also has `uptime/checks` and `settings` under Insights, which the docs do not list. `i=` is the team context, now `PORTHOLE_DO_CONTEXT`. A Droplet's own `/droplets/<id>/insights` tab is the legacy Monitoring graphs (finding A16)
- reproduce: open each tab with region tor1 and a 1-hour range and compare the address bar with `/api/config` links
- status: verified; the head marks the seven tab links verified

## B-002  URN formats for resources other than Droplets     (to verify)
- when: 2026-10-09 (build)   where: `infra/fleet.py`, head fleet dots, design Appendix D   finding: none yet
- request we made / received: none yet; only `do:droplet:<id>` appears in the facts pack
- response: n/a
- expected: one URN shape per family (load balancer, managed database, Kubernetes, Function, Spaces, registry, app)
- observed: not observed yet. The fixtures use `do:loadbalancer:<id>`, `do:dbaas:<id>`, `do:kubernetes:<id>`, `do:app:<id>` as guesses
- reproduce: once the fleet reports, `insights_harness.py prom values resource_urn --region tor1`; record what comes back
- status: to verify

## B-003  The metric-less selector for the fleet dots     (to verify)
- when: 2026-10-09 (build)   where: head `porthole/panels.py` probes   finding: design section 12 risk
- request we made / received: GET /v2/insights/query/tor1/prom/api/v1/query, query `count by (resource_urn) ({resource_urn=~"..."})`
- response: n/a
- expected: either a vector with one sample per fleet URN, or a 4xx that says a metric name is required
- observed: not observed yet. On a 4xx the head switches to one probe metric per family from `watcher/probe_metrics.json`, logs it once, and shows `probe_mode: family`; six of those eight probe metric names are guesses (marked `verified: false`)
- reproduce: open the Bridge with the real fleet, then `/api/fleet` and look at `probe_mode`
- status: to verify

## B-004  Webhook payload schema and signature header     (to verify)
- when: 2026-10-09 (build)   where: head `/hooks/insights`   finding: A14
- request we made / received: none yet; the docs say a webhook channel can sign payloads but name no header or algorithm
- response: n/a
- expected: a documented payload and a signature header with a named scheme
- observed: not observed yet. The receiver tries hex and base64 HMAC-SHA256, `sha256=` and `sha1=` prefixes, Stripe-style `t=,v1=`, Standard Webhooks, and HMAC-SHA512 on every header whose name contains signature, sign, hmac or digest, and records `fields_found` from the body
- reproduce: run the Alert round trip voyage; read the delivery on the Alerts page (headers, signature verdict, body)
- status: to verify

## B-005  How App Platform traces and logs reach Insights     (to verify)
- when: 2026-10-09 (build)   where: head `porthole/telemetry.py`, Traces and Logs pages   finding: A6
- request we made / received: none; nothing documents an OTLP endpoint for App Platform apps
- response: n/a
- expected: either automatic collection of stdout logs and spans, or a documented OTLP endpoint
- observed: not observed yet. The head writes JSON logs to stdout and exports spans only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set; the Logs page compares the head's own records with what Insights returns for `service.name = porthole`
- reproduce: deploy, then the Logs page, tabs "Head (as emitted)" and "Insights (API)" with service porthole
- status: to verify

## B-006  The instance size `apps-s-1vcpu-0.5gb` in region `tor`     (to verify)
- when: 2026-10-09 (build)   where: `.do/app.yaml`   finding: design section 8.2
- request we made / received: none; the pricing page lists `basic-xxs` as legacy for apps created before 7 May 2024
- response: n/a
- expected: the app is created in `tor` with `apps-s-1vcpu-0.5gb`
- observed: not observed yet
- reproduce: `doctl apps create --spec .do/app.yaml` or `infra/provision.py --only app`
- status: to verify

## B-007  Harness Runtime REST shapes     (to verify)
- when: 2026-10-09 (build)   where: head `porthole/brain/harness_runtime.py`, `watcher/brain/env.yaml`   finding: design section 11.5
- request we made / received: none; `doctl harness-runtime` shows create, prompt, logs, cancel and approve exist
- response: n/a
- expected: public REST shapes for creating a session, sending a prompt and reading its events, and an environment schema
- observed: Phase 2 is a placeholder that raises NotConfigured; `env.yaml` field names are a plan. On the build box (2026-10-09), doctl 1.166.0 answers `unknown command "harness-runtime"`, so `infra/steps/agent.py` checks the help text and records the session as pending; its size slug and flags are unverified
- reproduce: read the API reference for `/v2/agents/*` once it is published
- status: to verify

## B-008  The alert-rules API accepts underscored metric names     (to verify)
- when: 2026-10-08T23:13Z (Atlas write probe)   where: harness, `POST /v2/insights/alert-rules`   finding: A4 reversed
- request we made / received: POST /v2/insights/alert-rules with `query.metric = "do_droplets_cpu_utilization"`
- response: 201, the underscored name stored verbatim (probe rule deleted afterwards)
- expected: 422 Unprocessable Entity, per the API reference ("Underscored Prometheus-style names are rejected")
- observed: 201 in that one probe; whether such a rule ever evaluates is untested. The local fake still answers 422, as the design asks
- reproduce: `insights_harness.py probe naming <channel id> --write`
- status: to verify

## B-009  The rule list omits rules made in the control panel     (to verify)
- when: 2026-10-08T23:13Z (Atlas write probe)   where: `GET /v2/insights/alert-rules`   finding: A2 sharpened
- request we made / received: GET /v2/insights/alert-rules?per_page=100 after creating two rules through the API
- response: 200 listing the two API-made rules, but not "CPU is running high", which the same owner made in the control panel and which GET by id returns
- expected: every rule of the team in the list
- observed: as in the response line, in one probe. The head never lists rules: it reads the ids recorded by provisioning; the fake keeps an empty list
- reproduce: `insights_harness.py probe rules-visibility`
- status: to verify

## B-010  Dotted names for the catalog are derived by a heuristic     (to verify)
- when: 2026-10-09 (build)   where: head `porthole/promql.py` `dotted()`, Metrics catalog   finding: A4
- request we made / received: GET .../label/__name__/values returns only underscored names
- response: n/a
- expected: the dotted name Insights expects for every catalog entry
- observed: not observed yet. The head splits after the known family (`do_apps_app_cpu_usage` becomes `do.apps.app_cpu_usage`); a family name with an unexpected shape would give a wrong dotted name and an empty chart
- reproduce: compare the Metrics catalog with the Explore tab's names in the control panel
- status: to verify

## B-011  The fields of a channel's `*_status` objects     (to verify)
- when: 2026-10-09 (build)   where: head Alerts page, fake Insights   finding: none yet
- request we made / received: GET /v2/insights/notification-channels
- response: n/a
- expected: the field names inside `bearer_token_status`, `signature_status`, `basic_auth_status`
- observed: not observed yet. The page prints whatever keys arrive; the fake invents `is_set` and `updated_at`
- reproduce: `insights_harness.py channels get <webhook channel id> --json`
- status: to verify

## B-012  The ACTIVE instance status and the `status` filter     (to verify)
- when: 2026-10-09 (build)   where: head alerts panels, Alert round trip voyage   finding: none yet
- request we made / received: none for an active instance; only `ALERT_INSTANCE_STATUS_RESOLVED` was seen
- response: n/a
- expected: `ALERT_INSTANCE_STATUS_ACTIVE` while firing, and a documented value for the `status` query filter
- observed: not observed yet. The head lists instances by rule id and filters by status suffix itself
- reproduce: run the Alert round trip voyage and read `/api/insights/alerts` while it fires
- status: to verify

## B-013  Absolute instants in a logs search     (to verify)
- when: 2026-10-09 (build)   where: head `porthole/panels_logs.py`   finding: A15
- request we made / received: the head sends `time_range.from/to` as `{"absolute": "<RFC 3339>"}`; the facts pack verified `relative` only
- response: n/a
- expected: 200 with records in the window
- observed: not observed yet
- reproduce: the Logs page, "request body sent to Insights", against the live API
- status: to verify

## B-014  `query.tags` on alert rules     (to verify)
- when: 2026-10-08T23:13Z (Atlas write probe)   where: `POST /v2/insights/alert-rules`   finding: A8
- request we made / received: a rule with `query.tags: ["porthole-probe"]` and no `resource_urns`
- response: 201, tags echoed back
- expected: the limits page says alert rules cannot select resources by tag
- observed: accepted in one probe; whether a tagged resource is matched is untested
- reproduce: tag the tentacles, `insights_harness.py probe tags <channel id> --tag porthole --write`
- status: to verify

## B-015  Which service name a tentacle's logs would carry     (to verify)
- when: 2026-10-09 (build)   where: head Logs page, "Expected from tentacles"   finding: A6b
- request we made / received: none; Droplet logs did not arrive at all on 2026-10-08
- response: n/a
- expected: records with `service.name` equal to `TENTACLE_NAME` (the Droplet name), as the tentacle sets on its OTel resource and log lines
- observed: not observed yet. The expected-versus-observed counts search by that service name, so a different name would read as A6b
- reproduce: run the Log storm voyage, then search the Logs tab for the Droplet's name in body or resource
- status: to verify

## B-016  Spaces keys API, Spaces in tor1, and bucket probing     (to verify)
- when: 2026-10-09 (build)   where: `infra/steps/spaces.py`, `infra/teardown.py`   finding: none yet
- request we made / received: none; no token on the build box
- response: n/a
- expected: `POST /v2/spaces/keys` with `{"name", "grants": [{"bucket": "", "permission": "fullaccess"}]}` returns `{"key": {"access_key", "secret_key", ...}}`; Spaces is offered in tor1; an unsigned `HEAD https://tor1.digitaloceanspaces.com/<bucket>` answers 404 for a missing bucket and 403 for one that exists
- observed: not observed yet
- reproduce: `python infra/provision.py --only spaces`, then `python infra/teardown.py` to list it; if the step fails, skip it (Spaces is optional for the demo)
- status: to verify

## B-017  Project resource URNs for clusters, apps and buckets     (to verify)
- when: 2026-10-09 (build)   where: `infra/steps/common.py` `Context.assign`   finding: none yet
- request we made / received: none yet
- response: n/a
- expected: `POST /v2/projects/<id>/resources` accepts `do:kubernetes:<id>`, `do:app:<id>` and `do:space:<bucket>` as well as the documented Droplet, load balancer, dbaas and reserved IP URNs
- observed: not observed yet. A refused assignment prints a warning and the run goes on; project membership only changes how the control panel groups resources
- reproduce: run `provision.py` and look for "could not assign" lines
- status: to verify

## B-018  The tentacle database user may lack CREATE on the public schema     (to verify)
- when: 2026-10-09 (build)   where: `infra/steps/database.py`, tentacle `pg` scenario   finding: none yet
- request we made / received: none yet
- response: n/a
- expected: the `pg` scenario's `CREATE TABLE IF NOT EXISTS tentacle_load` succeeds as user `tentacle` in database `kraken`
- observed: not observed yet. Postgres 15 and later revoke CREATE on `public` from ordinary users, and the API creates databases owned by doadmin. If the scenario fails with "permission denied for schema public", run `GRANT CREATE ON SCHEMA public TO tentacle;` as doadmin in database `kraken`
- reproduce: start the Deep water voyage, or `POST /scenario/pg` on tentacle-1, and read the run's error
- status: to verify

## B-019  Functions namespace in tor1 and the web function URL     (to verify)
- when: 2026-10-09 (build)   where: `infra/steps/functions.py`   finding: none yet
- request we made / received: none yet
- response: n/a
- expected: `POST /v2/functions/namespaces` with `{"region": "tor1", "label": "kraken"}` works, and the deployed function answers `{"pong": true}` at `<api_host>/api/v1/web/<namespace>/kraken/ping`
- observed: not observed yet
- reproduce: `python infra/provision.py --only functions`, then curl the `url` recorded in `infra/out/state.json`
- status: to verify
