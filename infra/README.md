# infra: provisioning and teardown

`infra/` creates the kraken fleet that Porthole watches: three tentacle Droplets, a load balancer, a managed
Postgres, a one-node Kubernetes cluster, a Function, a Spaces bucket, a registry, a Harness Runtime session, the
App Platform app, and the Insights channels and alert rules. It calls the DigitalOcean API directly through
`doapi.py` (plain httpx, retries on 429 and 5xx). Every step looks for its resources by name and tag before it
creates anything, so a second run is safe, and `teardown.py` deletes only what `provision.py` created. It needs
Python 3.12 and `pip install -r infra/requirements.txt`; run the commands from the repo root.

## Variables

Export these before a run; the values never go in the repo. `provision.py` names any variable its steps need that
is missing, before it calls anything.

| variable | used by | what it is |
|---|---|---|
| `DIGITALOCEAN_TOKEN` | every step | the work token with write scopes; only these scripts use it, never the head |
| `HEAD_TOKEN` | app | the head's `insights:read` token, which becomes the app's `DIGITALOCEAN_TOKEN` |
| `TENTACLE_KEY` | droplets, app | the bearer the tentacles expect |
| `CAPTAIN_KEY` | app | the captain's key, 24 characters or more |
| `HOOK_BEARER` | app, insights | the bearer on the Insights webhook channel |
| `HOOK_SECRET` | app, insights | the signing secret on the webhook channel |
| `SSH_KEY_IDS` | droplets | comma separated SSH key ids or fingerprints for the tentacles |
| `SSH_ALLOW_CIDRS` | network (optional) | comma separated CIDRs allowed on port 22; unset keeps port 22 closed |
| `TENTACLE_TARBALL_URL` | droplets | a tarball with `tentacle/` in it, such as a release archive of this repo |
| `ALERT_EMAIL` | insights | the recipient of the email channel |
| `PUBLIC_URL` | app, insights (optional) | default `https://insights-demo.digitalocean.solutions` |
| `DO_CONTEXT` | app (optional) | the team context id (`i=` in control-panel URLs); becomes `PORTHOLE_DO_CONTEXT` |

## Steps

Design section 9.2 lists the steps by resource. They run in this order because the tentacles need the database DSN
and the Function URL at first boot. `--plan` prints the same list.

| order | design | `--only` | creates or finds |
|---|---|---|---|
| 1 | 1 | project | project `insights-demo`; every resource created later is assigned to it |
| 2 | 2 | project | tags `insights-demo` (every taggable resource) and `kraken-tentacle` (the firewall target) |
| 3 | 3 | network | per region: `kraken-<region>` if it exists, else the region's default VPC, else a new `kraken-<region>` |
| 4 | 4 | network | firewall `kraken-tentacles`, and a reserved IP for each of tentacle-1 and tentacle-2 |
| 5 | 7 | database | `kraken-pg` (Postgres, db-s-1vcpu-1gb, tor1) with database `kraken` and user `tentacle` |
| 6 | 9 | functions | namespace `kraken` in tor1 and the function `kraken/ping` |
| 7 | 5 | droplets | `kraken-tentacle-1` and `-2` (tor1), `-3` (syd1): s-1vcpu-1gb, Ubuntu 24.04, monitoring on |
| 8 | 6 | lb | `kraken-lb` (tor1): HTTP 80 to port 8800 on tentacle-1 and -2, health check `/health` |
| 9 | 8 | doks | `kraken-doks` (tor1, one s-2vcpu-2gb node, no HA control plane) and `infra/k8s/kraken.yaml` |
| 10 | 10 | spaces | Spaces key `kraken-spaces` and bucket `kraken-<6 hex>` in tor1 |
| 11 | 11 | registry | the team's registry when there is one (never deleted), else `kraken` on the starter tier |
| 12 | 12 | agent | Harness Runtime session `kraken-brain` (mars-1vcpu-1gb), created and paused with doctl |
| 13 | 13 | app | the app from `.do/app.yaml` with its SECRET values; waits for the first deployment |
| 14 | 14 | insights | webhook channel `kraken-head`, email channel `kraken-email`, one rule per template in `watcher/alerts/` |
| 15 | 15 | fleet | `infra/out/porthole.env`, then `PORTHOLE_FLEET_JSON` on the app; checks `/healthz` and `/api/fleet` |

The firewall applies to the tag `kraken-tentacle`, so it covers the tentacles from first boot: 8800 from anywhere
(App Platform has no fixed egress IP), 22 only from `SSH_ALLOW_CIDRS`, and 80 from the load balancer, a rule the lb
step adds once the load balancer exists. A tentacle's user_data is `tentacle/install.sh` with `export` lines for
`TENTACLE_NAME`, `TENTACLE_KEY`, `PEER_URL`, `PG_DSN`, `FN_URL` and `TENTACLE_TARBALL_URL` right after the shebang.

Rule templates get `{{URN}}` (the target tentacle) and `{{CHANNEL}}` (the webhook channel) filled in and keep their
status. The rule list leaves rules out (finding A2), so rule ids live in state and a later run checks each by id.
For the load balancer, the database and the cluster, the URN comes from Insights (`label/resource_urn/values`),
or is the documented pattern with `urn_verified: false` until the resource reports there.

## PEER_URL: why the network step reserves two IPs

Tentacle-1 and tentacle-2 send traffic to each other, and the installer writes `PEER_URL` into `/etc/tentacle/env`
once, at first boot, before either Droplet's IP is known. So the network step reserves one IPv4 address in tor1
for each of them before any Droplet exists. The droplets step writes `PEER_URL=http://<peer's reserved IP>:8800`
into each user_data and assigns each reserved IP to its Droplet once the Droplet is active. The fleet JSON uses
the reserved IPs in their URLs; tentacle-3 has no peer and is reached on its public IPv4.

## Commands

```bash
python infra/provision.py --plan            # the step order; no token, no API call
python infra/provision.py --dry-run         # read the account, print what would be created, change nothing
python infra/provision.py                   # create what is missing
python infra/provision.py --only droplets   # one step; a design number (5) or a list (lb,doks) also works
python infra/fleet.py                       # rewrite infra/out/porthole.env from state.json; no API call
python infra/teardown.py                    # list what would be deleted; no API call
python infra/teardown.py --yes              # delete it
```

Each action prints one line (`exists ...`, `created ...`, or in a dry run `would create ... (POST /v2/...)`). An
API error stops the run with exit code 1 and a one-line message; a missing variable exits with code 2.

## Without doctl or kubectl

Three steps hand part of their work to a command line tool. When the tool is not on PATH, the step prints the
exact commands, marks the item pending in state.json and the run goes on; a later run picks up pending items.

- functions: `doctl serverless connect <namespace id>` and `doctl serverless deploy infra/functions`.
- doks: `doctl kubernetes cluster kubeconfig save kraken-doks` and `kubectl apply -f infra/k8s/kraken.yaml`. With
  kubectl on PATH, the kubeconfig goes from the API into a temporary file that is deleted right after the apply.
- agent: `doctl harness-runtime create ...`, run only when doctl has the command and its `create` lists the flags
  used here (no REST shapes are published, and only `create --prompt` is documented). doctl 1.166.0 has none.

## State and secrets

`infra/out/` is gitignored. `state.json` holds each resource's id, name, `created` flag and `created_at`, plus
facts such as region, IP and URN; a resource found by name is recorded with `created: false` unless state already
says `provision.py` created it. No secret is written to disk: before every write, `state.py` refuses to save when
the text contains a secret variable's value, a secret the API returned during the run (the database password, a
Spaces secret key, the functions namespace key, the kubeconfig), or a string shaped like a token. The database
password is read only while the tentacles' user_data is built. A Spaces key's secret is used in memory and dropped;
to sign a later S3 request, a run or teardown creates a short-lived key and deletes it after. Secrets leave the
machine in three places only: the tentacles' user_data, the Insights webhook channel, and the app's SECRET
variables, which App Platform stores encrypted.

## Teardown

`teardown.py` lists the entries with `created: true` in reverse step order and deletes nothing without `--yes`.
With `--yes` it re-reads each resource, skips what is already gone, and removes the entry from state.json only
after DigitalOcean confirms. Alert rules go before the channels (a channel in use cannot be deleted), and reserved
IPs go after the Droplets. Entries with `created: false` (the team's registry, a default VPC) are never touched. A
failed delete keeps its entry in state.json; run `teardown.py --yes` again.

## By hand

- Create `HEAD_TOKEN` in the control panel (`insights:read`); no API creates tokens.
- `ALERT_EMAIL` must already be a verified member of the team before the insights step runs.
- Import `watcher/dashboards/krakens-eye.json` in the control panel; dashboards have no API.
- App Platform needs access to the GitHub repo DO-Solutions/porthole before the app step can create the app.

`cd infra && python -m pytest -q` runs the tests offline against an in-memory fake of the DigitalOcean API.
