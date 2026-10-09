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
- when: 2026-10-09 (build); URLs read 2026-10-09 ~00:20Z by the maintainer   where: head `porthole/deeplinks.py`, design Appendix A   finding: none
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

## B-016  Spaces keys API, Spaces in tor1, and bucket probing     (verified 2026-10-09)
- when: 2026-10-09 (build)   where: `infra/steps/spaces.py`, `infra/teardown.py`   finding: none yet
- request we made / received: none; no token on the build box
- response: n/a
- expected: `POST /v2/spaces/keys` with `{"name", "grants": [{"bucket": "", "permission": "fullaccess"}]}` returns `{"key": {"access_key", "secret_key", ...}}`; Spaces is offered in tor1; an unsigned `HEAD https://tor1.digitaloceanspaces.com/<bucket>` answers 404 for a missing bucket and 403 for one that exists
- observed: not observed yet
- reproduce: `python infra/provision.py --only spaces`, then `python infra/teardown.py` to list it; if the step fails, skip it (Spaces is optional for the demo)
- status: verified 2026-10-09; the Spaces key and bucket kraken-5f014f were created in tor1 by the first real run

## B-017  Project resource URNs for clusters, apps and buckets     (verified 2026-10-09)
- when: 2026-10-09 (build)   where: `infra/steps/common.py` `Context.assign`   finding: none yet
- request we made / received: none yet
- response: n/a
- expected: `POST /v2/projects/<id>/resources` accepts `do:kubernetes:<id>`, `do:app:<id>` and `do:space:<bucket>` as well as the documented Droplet, load balancer, dbaas and reserved IP URNs
- observed: not observed yet. A refused assignment prints a warning and the run goes on; project membership only changes how the control panel groups resources
- reproduce: run `provision.py` and look for "could not assign" lines
- status: verified 2026-10-09; do:loadbalancer:<id>, do:dbaas:<id>, do:kubernetes:<id>, do:space:<name> were accepted by the project assign API and the LB, database and cluster report in Insights under those URNs

## B-018  The tentacle database user may lack CREATE on the public schema     (to verify)
- when: 2026-10-09 (build)   where: `infra/steps/database.py`, tentacle `pg` scenario   finding: none yet
- request we made / received: none yet
- response: n/a
- expected: the `pg` scenario's `CREATE TABLE IF NOT EXISTS tentacle_load` succeeds as user `tentacle` in database `kraken`
- observed: not observed yet. Postgres 15 and later revoke CREATE on `public` from ordinary users, and the API creates databases owned by doadmin. If the scenario fails with "permission denied for schema public", run `GRANT CREATE ON SCHEMA public TO tentacle;` as doadmin in database `kraken`
- reproduce: start the Deep water voyage, or `POST /scenario/pg` on tentacle-1, and read the run's error
- status: to verify

## B-019  Functions namespace in tor1 and the web function URL     (verified 2026-10-09)
- when: 2026-10-09 (build)   where: `infra/steps/functions.py`   finding: none yet
- request we made / received: none yet
- response: n/a
- expected: `POST /v2/functions/namespaces` with `{"region": "tor1", "label": "kraken"}` works, and the deployed function answers `{"pong": true}` at `<api_host>/api/v1/web/<namespace>/kraken/ping`
- observed: not observed yet
- reproduce: `python infra/provision.py --only functions`, then curl the `url` recorded in `infra/out/state.json`
- status: verified 2026-10-09; namespace fn-595a… created in tor1; kraken/ping deployed with doctl from a workstation (the provisioner's doctl serverless plugin crashed with Illegal instruction inside a python:3.12-slim container) and answers {"pong": true} with 200

## B-020  First boot on Ubuntu 24.04 failed at the venv step     (fixed)
- when: 2026-10-09T03:21Z   where: `tentacle/install.sh` on all three tentacles   finding: none (our bug)
- request we made / received: cloud-init ran the installer; `python3 -m venv --help` succeeded, so python3-venv was never installed, then `python3 -m venv` failed: "ensurepip is not available"
- response: cloud-init `status: error`, no tentacle service, port 8800 closed; provisioning gave up after 900 s
- expected: the installer installs python3-venv when it is missing
- observed: the check tested the venv module, which exists without ensurepip
- reproduce: a stock Ubuntu 24.04 Droplet with the installer as user_data
- status: fixed in 478b640 (test ensurepip, rebuild a pip-less venv); the three boxes were repaired by hand

## B-021  `GET /v2/registry` answers 412 on a team with several registries     (fixed)
- when: 2026-10-09T03:11Z   where: `infra/steps/registry.py`   finding: DigitalOcean API behavior
- request we made / received: `GET /v2/registry`
- response: `412 This API is not supported if you have created multiple registries. Please use /v2/registries/{registry_name} instead.`
- expected: the documented single-registry endpoint, or a listing
- observed: teams with more than one registry must use `/v2/registries`
- reproduce: any team with two registries
- status: fixed in d5b5a3b (list `/v2/registries` first, fall back to `/v2/registry`)

## B-022  `POST /v2/apps` with a GitHub source: "GitHub user not authenticated"     (open)
- when: 2026-10-09T03:59Z and 04:01Z   where: `infra/steps/app.py`   finding: App Platform API
- request we made / received: `POST /v2/apps` with the spec of `.do/app.yaml` (github source DO-Solutions/porthole, branch main)
- response: `400 {"id":"bad_request","message":"GitHub user not authenticated"}`
- expected: the app is created; the DigitalOcean GitHub app is installed on the org for all repositories, the control panel shows the integration as connected, and `POST /v2/apps/propose` with the same spec returns 200
- observed: the create is refused for a personal access token; nothing in the docs says the API path needs a separate per-user GitHub authorization
- reproduce: `POST /v2/apps` with any github source and a PAT
- status: open; worked around by deploying from a DOCR image (`poseidon-docr/porthole:<sha>`); deploy-on-push can be switched on in the control panel

## B-023  Fresh Droplet metric series carry no `resource_name` label     (open)
- when: 2026-10-09T03:48Z   where: Insights PromQL API, region tor1   finding: Insights
- request we made / received: `GET /v2/insights/query/tor1/prom/api/v1/query` with `{__name__=~"do_droplets_cpu.*",resource_urn=~"do:droplet:60748318.*"}`
- response: series with `resource_urn` set and no `resource_name`, 30 minutes after creation; older Droplets in nyc3 carry `resource_name`
- expected: the same labels on every Droplet series
- observed: a selector on `resource_name` matches nothing for a new Droplet; Porthole's builder filters the fleet by name
- reproduce: create a Droplet with monitoring on, query within the first hour
- status: open; re-check after two hours; if the label never arrives, the builder must select by `resource_urn`

## B-024  `kraken.yaml` creates its objects in `default`, not in a `kraken` namespace     (fixed)
- when: 2026-10-09T04:02Z   where: `infra/k8s/kraken.yaml`   finding: none (our manifest)
- request we made / received: `kubectl apply -f infra/k8s/kraken.yaml`
- response: `deployment.apps/kraken-echo created`, `service/kraken-echo created` in `default`
- expected: a `kraken` namespace, as the design's wording implies
- observed: the manifest has no namespace
- reproduce: apply and `kubectl get pods -n kraken`
- status: documented here; the pod runs in `default` (pod kraken-echo, service kraken-echo 80/TCP)

