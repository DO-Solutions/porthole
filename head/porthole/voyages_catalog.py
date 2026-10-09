"""The seven voyages of design Appendix C as specs: title, plan feature, story, parameters and planned steps.

The step lists are the plan the Stir page shows before a voyage sails; the run functions live in
voyages_metrics.py and voyages_sea.py, and each one walks exactly these steps in this order."""
from __future__ import annotations

from porthole.voyages import StepSpec, VoyageSpec
from porthole.voyages_metrics import alert_round_trip, ballast, churn, two_seas
from porthole.voyages_sea import chain, deep_water, log_storm

TARGET = {"target": {"type": "tentacle", "default": None}}

CATALOG: dict[str, VoyageSpec] = {v.name: v for v in (
    VoyageSpec(
        "churn", "Churn", "M1",
        "Burns CPU on one tentacle for ten minutes and times how long Insights takes to show it, then to show it "
        "settle after the burn stops.",
        (StepSpec("baseline", "read the CPU baseline", 60), StepSpec("burn", "start cpu for 600 s", 60),
         StepSpec("metric-appears", "CPU above 50 % in Insights", 600),
         StepSpec("ride-out", "sample every 30 s until the run ends", 720),
         StepSpec("settle", "CPU back below 50 %", 600), StepSpec("summary", "ramp and settle times", 30)),
        churn, TARGET, ("burn_to_appear_s", "peak_pct", "settle_s")),
    VoyageSpec(
        "alert-round-trip", "Alert round trip", "A1, A2",
        "Burns CPU until the round-trip rule fires, waits for the alert and its webhook to come back to this "
        "site, stops the burn, and waits for the alert to resolve. Every hop is timed.",
        (StepSpec("read-rule", "read the round-trip rule by id", 60), StepSpec("baseline", "read the CPU baseline", 60),
         StepSpec("burn", "start cpu for 3 windows plus 2 minutes", 60),
         StepSpec("metric-crosses", "CPU crosses the critical threshold", 600),
         StepSpec("alert-active", "an ACTIVE instance appears", 600),
         StepSpec("webhook-delivered", "the webhook reaches /hooks/insights", 600),
         StepSpec("stop-burn", "stop the cpu scenario", 60),
         StepSpec("metric-falls", "CPU falls below the threshold", 600),
         StepSpec("alert-resolved", "the instance turns RESOLVED", 1500),
         StepSpec("resolve-delivery", "the resolve webhook arrives", 600, optional=True),
         StepSpec("summary", "every latency", 30)),
        alert_round_trip, TARGET,
        ("burn_to_cross_s", "cross_to_active_s", "active_to_delivery_s", "stop_to_resolved_s")),
    VoyageSpec(
        "ballast", "Ballast", "M2",
        "Loads memory, disk and the private network on the two Toronto tentacles and checks which labels "
        "(filesystem_mountpoint, network_device) Insights attaches to those families.",
        (StepSpec("start-memory", "memory 500 MB for 300 s", 60), StepSpec("start-disk", "disk 2,048 MB for 300 s", 60),
         StepSpec("start-network", "network 50 mbps for 180 s", 60),
         StepSpec("watch", "sample the families for 6 minutes", 420),
         StepSpec("labels", "read filesystem_mountpoint and network_device", 120),
         StepSpec("summary", "which labels appeared", 30)),
        ballast, {}, ("memory_peak_pct", "labels")),
    VoyageSpec(
        "two-seas", "Two seas", "M3",
        "Burns CPU in Toronto and Sydney at once and charts both regions: each region answers separately and the "
        "series are overlaid, because Insights does not add regions together.",
        (StepSpec("burn", "cpu for 300 s on a tentacle in each region", 60),
         StepSpec("both-regions", "ask both regions", 60),
         StepSpec("series-appear", "each region shows its burn", 600), StepSpec("summary", "when each appeared", 30)),
        two_seas, {}, ()),
    VoyageSpec(
        "log-storm", "Log storm", "L1, L3",
        "Writes 3,000 log lines in a minute on one tentacle, then searches Insights for them every 20 s for six "
        "minutes and says whether they arrived.",
        (StepSpec("storm", "logs: 50 lines a second for 60 s, 10 % errors", 60),
         StepSpec("emitted", "read what the tentacle wrote", 180),
         StepSpec("search", "search Insights every 20 s for 6 minutes", 420),
         StepSpec("verdict", "collected or not collected (A6b)", 30)),
        log_storm, TARGET, ("emitted", "insights_count", "verdict")),
    VoyageSpec(
        "chain", "Chain", "T1",
        "Sends 20 traced requests through a tentacle and its peer with injected latency and errors, collects the "
        "trace ids, and shows the head's own span for the call that started them.",
        (StepSpec("start-chain", "chain: 20 requests, 250 ms, 20 % errors", 60),
         StepSpec("collect", "collect the trace ids", 300), StepSpec("own-span", "find the head's own span", 30),
         StepSpec("link", "where to search in Insights", 30)),
        chain, TARGET, ("ok", "failed", "first_trace_id")),
    VoyageSpec(
        "deep-water", "Deep water", "M4",
        "Loads the managed Postgres, the Function and the load balancer for a minute and checks which of their "
        "metric families Insights reports within five minutes, and which of them move with the load.",
        (StepSpec("start-pg", "pg: 4 clients for 60 s", 60), StepSpec("start-fn", "fn: 5 calls a second for 60 s", 60),
         StepSpec("start-lb", "lb: 20 calls a second for 60 s", 60),
         StepSpec("watch", "check each family for 5 minutes", 360), StepSpec("summary", "which families reported", 30)),
        deep_water, {}, ("reported_after_s", "moved_after_s", "missing")),
)}
