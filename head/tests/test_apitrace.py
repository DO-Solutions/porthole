"""The API trace: TracedInsights records every exchange, the token is never in any field or curl line."""
from __future__ import annotations

import asyncio
import json
import threading

import httpx
import pytest
from conftest import TOKEN

from insights_harness import Insights, InsightsError, cond
from porthole.apitrace import ApiTrace, TracedInsights, as_bugs_md, as_curl, caller, collect_calls
from porthole.cache import Budget, BudgetExhausted
from porthole.sse import Hub


def _blob(env) -> str:
    return json.dumps([c.full() for c in env.deps.trace.ring])


async def test_traced_insights_records_each_exchange(env):
    ins = env.deps.insights
    with caller("panels.range"):
        await env.deps.run_sync(ins.query, "count(do.droplets.cpu_utilization)", region="tor1")
    call = env.deps.trace.ring[-1]
    assert call.target == "insights" and call.method == "GET"
    assert call.path == "/v2/insights/query/tor1/prom/api/v1/query"
    assert call.params == {"query": "count(do.droplets.cpu_utilization)"}
    assert call.status == 200 and call.content_type.startswith("application/json")
    assert call.ms is not None and call.ms >= 0
    assert call.excerpt.startswith('{"status":"success"')
    assert call.region == "tor1" and call.caller == "panels.range"
    assert call.trace_id is None or len(call.trace_id) == 32


async def test_request_bodies_are_redacted(env):
    ins = env.deps.insights
    spec = ins.webhook_channel("probe", "https://example.com/hook", bearer="hook-token-value-123",
                               secret="signing-secret-value-456")
    await env.deps.run_sync(ins.create_channel, spec)
    call = env.deps.trace.ring[-1]
    assert call.method == "POST" and call.status == 201
    assert call.body["webhook"]["bearer_token"]["token"] == "***"
    assert call.body["webhook"]["signature"]["secret"] == "***"
    assert "hook-token-value-123" not in _blob(env) and "signing-secret-value-456" not in _blob(env)


async def test_errors_are_recorded_with_status_and_body(env):
    ins = env.deps.insights
    with pytest.raises(InsightsError):
        await env.deps.run_sync(ins.request, "POST", "/v2/insights/query/tor1/logs/search", None, {})
    call = env.deps.trace.ring[-1]
    assert call.status == 400 and "time_range is required" in call.excerpt


async def test_token_never_present_anywhere(env):
    ins = env.deps.insights
    await env.deps.run_sync(ins.label_values, "__name__", region="tor1")
    await env.deps.run_sync(ins.search_logs, "now-1h", "now", cond("severity_number", ">=", 17), None, 5, None, "tor1")
    await env.deps.run_sync(ins.get_rule, "00000000-0000-0000-0000-0000000000a1")
    assert TOKEN not in _blob(env)
    for call in env.deps.trace.ring:
        r = await env.client.get(f"/api/trace/{call.id}")
        assert r.status_code == 200
        assert TOKEN not in r.text
        assert "$DIGITALOCEAN_TOKEN" in r.json()["curl"]


def test_curl_and_bugs_md_rendering():
    trace = ApiTrace()
    get = trace.record(target="insights", method="GET", path="/v2/insights/query/tor1/prom/api/v1/query_range",
                       params={"query": 'avg(do.droplets.cpu_utilization{resource_name="a b"})', "step": "60s"},
                       status=200, ms=12.3, text='{"status":"success"}',
                       url="https://api.digitalocean.com/v2/insights/query/tor1/prom/api/v1/query_range")
    curl = as_curl(get)
    assert curl.startswith('curl -sS -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" -G ')
    assert "--data-urlencode 'query=avg(do.droplets.cpu_utilization{resource_name=\"a b\"})'" in curl
    post = trace.record(target="insights", method="POST", path="/v2/insights/query/tor1/logs/search",
                        body={"time_range": {"from": {"relative": "1h"}, "to": {"relative": "now"}}}, status=200,
                        url="https://api.digitalocean.com/v2/insights/query/tor1/logs/search")
    curl = as_curl(post)
    assert "-X POST" in curl and "-d '{\"time_range\"" in curl and "Content-Type: application/json" in curl
    tentacle = trace.record(target="tentacle", method="POST", path="/scenario/cpu", params={"seconds": 120},
                            status=202, url="http://203.0.113.10:8800/scenario/cpu")
    assert '"Authorization: Bearer $TENTACLE_KEY"' in as_curl(tentacle)
    assert "'http://203.0.113.10:8800/scenario/cpu?seconds=120'" in as_curl(tentacle)
    md = as_bugs_md(get, as_curl(get))
    lines = md.splitlines()
    assert lines[0].startswith("## B-000  GET /v2/insights/query/tor1/prom/api/v1/query_range returned 200")
    for prefix in ("- when:", "- request we made / received:", "- response: 200", "- expected:", "- observed:",
                   "- reproduce: `curl", "- status: open -> reported -> fixed / wontfix"):
        assert any(line.startswith(prefix) for line in lines), prefix


def test_ring_is_bounded_and_newest_first():
    trace = ApiTrace(size=5)
    for i in range(8):
        trace.record(target="insights", method="GET", path=f"/p/{i}", status=200)
    assert len(trace.ring) == 5
    listed = trace.list(limit=10)
    assert [c["path"] for c in listed] == ["/p/7", "/p/6", "/p/5", "/p/4", "/p/3"]
    assert trace.list(target="tentacle") == []
    assert [c["path"] for c in trace.list(q="/p/6")] == ["/p/6"]


def test_configured_secrets_are_scrubbed_from_excerpts():
    trace = ApiTrace(secrets=["very-secret-value-0001"])
    call = trace.record(target="tentacle", method="GET", path="/health", status=200,
                        text='{"echo": "very-secret-value-0001"}')
    assert "very-secret-value-0001" not in json.dumps(call.full())
    assert "***" in call.excerpt


async def test_api_call_events_are_published():
    hub = Hub()
    hub.bind(asyncio.get_running_loop())
    q = hub.subscribe()
    trace = ApiTrace(hub)
    trace.record(target="insights", method="GET", path="/v2/insights/alert-rules/x", status=404)
    ev = q.get_nowait()
    assert ev.event == "api_call"
    assert set(ev.data) == {"id", "t", "target", "method", "path", "status", "ms", "region", "entity"}


async def test_harness_calls_run_on_their_own_thread_pool(env):
    """Pass 2 of the wringer: the loop's default executor gives a 1 vCPU instance five threads, so twelve
    concurrent Insights calls took three rounds of upstream latency. The harness pool runs them side by side."""
    barrier = threading.Barrier(12, timeout=5)  # fails unless all twelve calls are in flight at once

    def wait_then(n: int) -> int:
        barrier.wait()
        return n

    results = await asyncio.gather(*(env.deps.run_sync(wait_then, i) for i in range(12)))
    assert results == list(range(12))
    assert all(t.name.startswith("harness") for t in threading.enumerate() if t.name.startswith("harness"))


async def test_collect_calls_crosses_worker_threads(env):
    ins = env.deps.insights
    with collect_calls() as calls:
        await env.deps.run_sync(ins.query, "count(do.droplets.cpu_utilization)", region="tor1")
        await env.deps.run_sync(ins.query, "count(do.droplets.cpu_utilization)", region="syd1")
    assert len(calls) == 2 and calls == [c.id for c in list(env.deps.trace.ring)[-2:]]


def test_budget_is_checked_before_any_request():
    sent = []
    transport = httpx.MockTransport(lambda req: sent.append(req) or httpx.Response(200, json={"data": []}))
    t = [0.0]
    ins = TracedInsights("x" * 20, trace=ApiTrace(), budget=Budget(2, lambda: t[0]), transport=transport)
    ins.labels(region="tor1")
    ins.labels(region="tor1")
    with pytest.raises(BudgetExhausted) as err:
        ins.labels(region="tor1")
    assert len(sent) == 2 and 0 < err.value.retry_in <= 60


def test_elapsed_time_is_per_thread():
    ins = TracedInsights("x" * 20, trace=ApiTrace(),
                         transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})))
    ins.last_elapsed_ms = 5.0
    seen = []
    th = threading.Thread(target=lambda: seen.append(ins.last_elapsed_ms))
    th.start()
    th.join()
    assert seen == [None] and ins.last_elapsed_ms == 5.0
    assert isinstance(ins, Insights)


async def test_trace_routes(env):
    await env.deps.run_sync(env.deps.insights.query, "count(do.droplets.cpu_utilization)", region="tor1")
    r = await env.client.get("/api/trace", params={"limit": 5, "target": "insights"})
    body = r.json()
    assert body["calls"] and body["stats"]["last_minute"] >= 1
    one = (await env.client.get(f"/api/trace/{body['calls'][0]['id']}")).json()
    assert {"curl", "bugs_md", "trace_id", "params", "body", "response_head"} <= set(one)
    assert (await env.client.get("/api/trace/c-missing")).status_code == 404
