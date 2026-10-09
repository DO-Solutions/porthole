#!/usr/bin/env bash
# Starts head/dev/run_local.py, fetches every page and GET route, checks status codes, the security headers and
# that pages reference only local assets, exercises one mutating call with the dev captain's key, then stops it.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${PYTHON:-python3}"
PORT="${SMOKE_PORT:-8090}"
BASE="http://127.0.0.1:$PORT"
KEY="dev-captain-key-local-only-0000"
TOKEN="dev-token-for-the-local-fake"
LOG="$(mktemp)"
BODIES="$(mktemp)"
"$PY" "$ROOT/head/dev/run_local.py" --port "$PORT" >"$LOG" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null; wait $PID 2>/dev/null; rm -f "$LOG" "$BODIES"' EXIT
for _ in $(seq 1 100); do curl -fsS "$BASE/healthz" >/dev/null 2>&1 && break; sleep 0.1; done
fail=0
pass=0

check() {  # check METHOD PATH STATUS [curl args...]
  local method=$1 path=$2 want=$3 headers status body
  shift 3
  body="$(mktemp)"
  headers=$(curl -sS -o "$body" -D - -X "$method" "$@" "$BASE$path" | tr -d '\r')
  cat "$body" >>"$BODIES"
  rm -f "$body"
  status=$(printf '%s\n' "$headers" | head -1 | awk '{print $2}')
  if [ "$status" != "$want" ]; then echo "FAIL $method $path: HTTP $status, expected $want"; fail=1; return; fi
  for h in "content-security-policy: default-src 'self'" "x-content-type-options: nosniff" "referrer-policy: no-referrer"; do
    if ! printf '%s\n' "$headers" | grep -qi "^$h"; then echo "FAIL $method $path: missing header ${h%%:*}"; fail=1; return; fi
  done
  pass=$((pass + 1))
  [ "${QUIET:-}" = 1 ] || echo "ok   $status $method $path"
}

for page in / /stir /metrics /dashboards /alerts /logs /traces /api /brain; do
  check GET "$page" 200
  html=$(curl -sS "$BASE$page")
  if printf '%s' "$html" | grep -Eqi '(src|href)="(https?:)?//'; then echo "FAIL $page references an external URL"; fail=1; fi
  if printf '%s' "$html" | grep -Eqi ' style="|<script>|<style'; then echo "FAIL $page has inline style or script"; fail=1; fi
  for asset in $(printf '%s' "$html" | grep -Eo '(src|href)="/static/[^"]+"' | sed -E 's/^(src|href)="//; s/"$//'); do
    QUIET=1 check GET "$asset" 200
  done
done

for path in /healthz /api/config /api/fleet /api/fleet/scenarios /api/scenarios/catalog /api/voyages \
    "/api/insights/catalog?region=tor1" "/api/insights/catalog?region=both" \
    "/api/insights/labels?region=tor1&name=__name__" \
    "/api/insights/range?region=both&metric=do.droplets.cpu_utilization&agg=avg&range=30m" \
    "/api/insights/query?region=tor1&metric=do.droplets.cpu_utilization" /api/insights/alerts \
    "/api/insights/logs?region=tor1&range=1h" "/api/insights/logs/expected?range=1h" /api/trace \
    /api/hooks/deliveries /api/traces/own /api/traces/chains /api/logs/own /api/brain/info \
    /api/dashboards/krakens-eye "/api/dashboards/krakens-eye/run?index=0&region=tor1" \
    /watcher/dashboards/krakens-eye.json /favicon.ico; do
  check GET "$path" 200
done
CALL=$(curl -sS "$BASE/api/trace?limit=1" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["calls"][0]["id"])')
check GET "/api/trace/$CALL" 200
check GET /api/voyages/v-000000 404
check GET /api/brain/sessions/s-000000 404
events=$(curl -sS -N --max-time 2 -D - -o /dev/null "$BASE/events" 2>/dev/null | tr -d '\r')
if printf '%s\n' "$events" | grep -qi '^content-type: text/event-stream'; then pass=$((pass + 1)); echo "ok   200 GET /events (stream)"; else echo "FAIL GET /events"; fail=1; fi

START='{"target": "tentacle-1", "scenario": "cpu", "params": {"seconds": 10}}'
check POST /api/scenarios/start 401 -H 'Content-Type: application/json' -d "$START"
check POST /api/scenarios/start 202 -H "X-Captain-Key: $KEY" -H 'Content-Type: application/json' -d "$START"
check GET /hooks/insights 405
check POST /hooks/insights 401 -H 'Content-Type: application/json' -d '{}'
check POST /hooks/insights 200 -H 'Authorization: Bearer dev-hook-bearer-local-only' -H 'Content-Type: application/json' -d '{"smoke": true}'

if grep -q "$TOKEN" "$BODIES"; then echo "FAIL the DigitalOcean token appeared in a response"; fail=1; fi
if grep -q "$TOKEN" "$LOG"; then echo "FAIL the DigitalOcean token appeared in the log"; fail=1; fi
echo "smoke: $pass checks passed, $([ $fail = 0 ] && echo 'no failures' || echo 'FAILURES above')"
exit $fail
