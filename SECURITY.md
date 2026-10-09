# Security

Porthole is a public demo site that holds a DigitalOcean token and can put load on a few demo Droplets. This page
says how it protects both, and how to report a problem.

## Posture

- **The token stays on the server.** `DIGITALOCEAN_TOKEN` lives only in the App Platform app's encrypted secrets.
  The browser never sees it. Curl lines on the API page carry `$DIGITALOCEAN_TOKEN` in its place, request bodies are
  redacted before they are stored, and every configured secret is scrubbed from responses, the API trace and the
  logs. The head's token needs `insights:read` only; `insights:update` is needed only when write mode
  (`PORTHOLE_INSIGHTS_WRITE=1`) is switched on. Every id, name and URN comes from the fleet description, so the head
  needs no scope on other resources.
- **Reading is open; changing needs the captain's key.** Starting scenarios and voyages, raw PromQL, free-form log
  filters, pausing rules and approving the Brain's actions require `PORTHOLE_CAPTAIN_KEY` (24 characters or more),
  sent as `X-Captain-Key` and compared in constant time. With no key configured those routes answer 503. The page
  keeps the key in `sessionStorage` for the tab, never in the URL or a cookie.
- **Rate limits.** Mutating routes: 12 a minute per client address and 60 overall. Brain questions: 3 a minute per
  address and 10 overall. The webhook: 60 a minute per address and 600 overall. Insights calls: 200 a minute
  overall (the API allows 250); past that, panels serve cached data marked stale. Over a limit the answer is 429
  with `Retry-After`. Behind App Platform the client address is the first `X-Forwarded-For` hop; a hop that is
  not an IP address is ignored. A caller can forge that hop, so the overall limits are the ones that hold.
- **Caps apply on the server.** Every scenario parameter is clamped to the public caps (tighter than the tentacles'
  own limits) whatever the page sends. Visitors get builder-mode queries sent only to the fleet's regions (the region
  is the path of the call) and pinned to the fleet's resource URNs, which keeps discovery queries off the shared
  team account.
- **The tentacles.** The head calls them over plain HTTP on their public addresses with a bearer key, because App
  Platform's basic tier has no fixed egress address. The key can only start bounded load on demo boxes. A TLS
  reverse proxy per tentacle is the upgrade path; it is not in this build.
- **The webhook.** `POST /hooks/insights` only, 64 KiB at most, bearer or basic auth checked before a delivery
  reaches any timeline. Rejected deliveries are kept apart and answered 401; the page shows only when they came,
  how big they were and why they were refused, never their body or headers, so posting to the webhook puts no
  text on the site. The signature is checked against several schemes and reported, never required, because
  Insights does not document its scheme. Authorization headers are stored reduced to their scheme, and any
  configured secret that appears in a header or body is replaced with `***`.
- **The browser.** Every asset comes from the same origin, with `Content-Security-Policy: default-src 'self';
  img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff` and
  `Referrer-Policy: no-referrer`. No CORS headers, no inline scripts or styles, and data is inserted as text.
- **Logs.** JSON lines on stdout. A record that contains any configured secret is replaced by a notice before it is
  written. Client addresses appear only in rate-limit events; webhook bodies only as 200-character excerpts.
- **The repo.** No secrets, no lab hostnames, no internal names. `infra/` reads secrets from the environment and its
  state file never holds one (a test plants values to check). CI runs the tests, ruff, `pip-audit` on pinned
  dependencies and a leak sweep for secret shapes and public addresses. The container runs as a non-root user.

## Reporting a problem

Please report a vulnerability privately, not in a public issue: use "Report a vulnerability" on this repository's
Security tab, or contact the DO-Solutions maintainers directly. Include what you saw, where, and how to reproduce
it. If you think a key or token is exposed, say so in the first line so it can be rotated first.
