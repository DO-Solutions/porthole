# Backlog

Things the wringer found or thought of but did not change, each with the reason. Decisions go to Darian; the
rest waits for a build that touches the same code. Bugs observed against the live API go in `BUGS.md`, not here.

## Decisions

- **Which `X-Forwarded-For` hop to trust.** With `PORTHOLE_TRUST_PROXY=1` the head takes the first hop, as the
  design says. App Platform appends the real address to a header the caller may already have sent, so a caller can
  forge the first hop and dodge the per-address limits. The overall limits (60 mutations, 10 Brain questions, 600
  webhook posts a minute) still hold, and a hop that is not an IP address is ignored. The fix is to know how many
  trusted proxies sit in front of the app and take the hop before them, or to read the header App Platform
  documents for the client address. Needs one deploy and one look at a real request's headers.
- **A cap on open connections.** Every page holds one `/events` stream and each stream holds a queue of up to 200
  events. Nothing caps how many streams a visitor may open. uvicorn's `--limit-concurrency` would answer 503 past a
  number of connections; the design pins the container command, so the number and whether to add it are a decision
  for the deploy.

## Later

- **The harness prints an em dash.** `harness/insights_harness.py` writes probe lines as `PASS name: check — detail`
  and its test checks that format. The harness is carried over unchanged, so the copy pass left it. Change it when
  the harness is next revised.
- **The fleet probe assumes the Prometheus answer shape.** `Panels._probe_fetch` indexes `body["data"]["result"]`
  of an instant query; a 200 without that shape would raise, the poller would log one warning for that cycle and
  `/api/fleet` would answer 500 until the first good poll. No such answer exists in the facts pack; fix when a real
  one shows up.
- **One trace entry for the harness's 429 retry.** The harness retries a 429 once inside `send()`, so
  `TracedInsights` records one exchange with the last attempt's status and timing. Recording both attempts means
  a hook in the harness, which is used unchanged.
- **Chart payload bytes.** A 24 hour range with eight series is 278 KB of JSON (1,440 points a series, full
  timestamps, full floats). Rounding values to six significant digits would cut it by about a third at the cost of
  the table view's digits; the design's budget allows the current size, so it was left.
