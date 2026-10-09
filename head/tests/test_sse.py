"""The SSE hub: event format, order, Last-Event-ID replay, heartbeat, slow consumers, thread-safe publish."""
from __future__ import annotations

import asyncio
import json
import threading

from porthole.sse import Hub, format_event


def test_format_event_lines():
    text = format_event("scenario", {"target": "kraken-tentacle-1", "run": {"id": "cpu-1"}}, 7)
    assert text == 'id: 7\nevent: scenario\ndata: {"target":"kraken-tentacle-1","run":{"id":"cpu-1"}}\n\n'
    assert format_event("heartbeat", {"t": "x"}).startswith("event: heartbeat\n")


async def test_published_events_arrive_in_order_with_increasing_ids():
    hub = Hub()
    hub.bind(asyncio.get_running_loop())
    q = hub.subscribe()
    ids = [hub.publish("fleet", {"n": i}) for i in range(5)]
    assert ids == sorted(ids) and len(set(ids)) == 5
    got = [q.get_nowait() for _ in range(5)]
    assert [e.data["n"] for e in got] == [0, 1, 2, 3, 4]


async def test_last_event_id_replay_through_the_stream():
    hub = Hub()
    hub.bind(asyncio.get_running_loop())
    for i in range(6):
        hub.publish("voyage", {"step": i})
    hub.close()
    chunks = [c async for c in hub.stream(last_event_id="3")]
    events = [c for c in chunks if c.startswith("id:")]
    assert [json.loads(c.split("data: ")[1])["step"] for c in events] == [3, 4, 5]
    assert chunks[0] == "retry: 3000\n\n"


async def test_no_replay_without_last_event_id():
    hub = Hub()
    hub.bind(asyncio.get_running_loop())
    hub.publish("fleet", {})
    hub.close()
    assert [c for c in [c async for c in hub.stream()] if c.startswith("id:")] == []


async def test_heartbeat_when_idle():
    hub = Hub(now_iso=lambda: "2026-10-12T14:00:00Z")
    hub.bind(asyncio.get_running_loop())
    gen = hub.stream(heartbeat_s=0.02)
    assert await gen.__anext__() == "retry: 3000\n\n"
    beat = await asyncio.wait_for(gen.__anext__(), 2)
    assert beat.startswith(": heartbeat\n\n") and 'event: heartbeat\ndata: {"t":"2026-10-12T14:00:00Z"}' in beat
    await gen.aclose()
    assert hub.subscribers == 0


async def test_slow_consumer_loses_oldest():
    hub = Hub(queue_size=3)
    hub.bind(asyncio.get_running_loop())
    q = hub.subscribe()
    for i in range(5):
        hub.publish("api_call", {"n": i})
    assert [q.get_nowait().data["n"] for _ in range(3)] == [2, 3, 4]
    assert hub.dropped == 2


async def test_publish_from_a_worker_thread():
    hub = Hub()
    hub.bind(asyncio.get_running_loop())
    q = hub.subscribe()
    th = threading.Thread(target=lambda: hub.publish("api_call", {"from": "thread"}))
    th.start()
    th.join()
    ev = await asyncio.wait_for(q.get(), 2)
    assert ev.data == {"from": "thread"}


async def test_ring_holds_500():
    hub = Hub()
    for i in range(600):
        hub.publish("x", {"i": i})
    assert len(hub.ring) == 500 and hub.ring[0].data["i"] == 100


async def test_events_endpoint_headers_and_replay(env):
    hub = env.deps.hub
    first = hub.publish("delivery", {"id": "d-1"})
    hub.publish("delivery", {"id": "d-2"})
    hub.close()
    r = await env.client.get("/events", headers={"Last-Event-ID": str(first)})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    assert '"id":"d-2"' in r.text and '"id":"d-1"' not in r.text
