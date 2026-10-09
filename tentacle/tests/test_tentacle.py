"""Tentacle tests: auth, health, every scenario starts and is listed, stop, JSON logs, chain spans."""
from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tentacle  # noqa: E402

KEY = "test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}
REQUIRED_LOG_KEYS = {"timestamp", "severity_text", "severity_number", "body", "service.name"}


class Env:
    def __init__(self, tmp_path: Path, **overrides):
        self.spans = InMemorySpanExporter()
        self.logs = InMemoryLogRecordExporter()
        self.stdout = io.StringIO()
        self.settings = tentacle.Settings(
            name="tentacle-a", key=KEY, port=8800, service_name="tentacle-a",
            peer_url="http://peer.test", log_file=str(tmp_path / "log" / "tentacle.jsonl"),
            data_dir=str(tmp_path / "data"), self_url="http://self.test", **overrides)
        # outgoing calls (to self, peer, sink) are routed back into this same app in-process
        self.app = tentacle.create_app(self.settings, span_exporter=self.spans, log_exporter=self.logs,
                                       http_client_factory=lambda app: TestClient(app), log_stream=self.stdout)
        self.client = TestClient(self.app)

    @property
    def t(self) -> tentacle.Tentacle:
        return self.app.state.tentacle

    def start(self, path: str, **params) -> dict:
        r = self.client.post(path, params=params, headers=AUTH)
        assert r.status_code == 202, r.text
        return r.json()

    def wait(self, run_id: str, timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            listing = self.client.get("/scenarios").json()
            for run in listing["finished"]:
                if run["id"] == run_id:
                    return run
            time.sleep(0.1)
        raise AssertionError(f"{run_id} did not finish in {timeout}s")

    def log_lines(self) -> list[dict]:
        return [json.loads(line) for line in Path(self.settings.log_file).read_text().splitlines()]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    with e.client:
        yield e


def test_health_is_open_and_shaped(env):
    r = env.client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"name", "uptime_s", "running", "load1", "mem_pct", "mem_avail_mb", "mem_total_mb"}
    assert body["name"] == "tentacle-a"
    assert isinstance(body["running"], list)
    assert isinstance(body["load1"], float)
    assert 0 < body["mem_pct"] < 100
    assert isinstance(body["mem_avail_mb"], int) and 0 < body["mem_avail_mb"] < body["mem_total_mb"]


def test_health_memory_on_a_1_gb_droplet(env, monkeypatch):
    """The tor1 tentacles on 2026-10-09: MemTotal 984556 kB, about 590 MB available with nothing running. The
    head sizes the ballast ask from mem_avail_mb and parses "MemAvailable N MB" out of the guard's 409."""
    meminfo = {"MemTotal": 984556 * 1024, "MemAvailable": 604160 * 1024}
    monkeypatch.setattr(tentacle, "_mem_info", lambda: dict(meminfo))
    monkeypatch.setattr(tentacle.Tentacle, "memory", lambda self, run: {"held_mb": run.params["mb"]})
    body = env.client.get("/health").json()
    assert (body["mem_avail_mb"], body["mem_total_mb"], body["mem_pct"]) == (590, 961, 38.6)
    r = env.client.post("/scenario/memory", params={"mb": 500, "seconds": 1}, headers=AUTH)
    assert r.status_code == 409
    assert r.json()["detail"] == "500 MB would leave less than 100 MB available (MemAvailable 590 MB)"
    assert env.client.post("/scenario/memory", params={"mb": 440, "seconds": 1}, headers=AUTH).status_code == 202


@pytest.mark.parametrize("method,path", [
    ("post", "/scenario/cpu"), ("post", "/scenario/memory"), ("post", "/scenario/disk"),
    ("post", "/scenario/network"), ("post", "/scenario/logs"), ("post", "/scenario/chain"),
    ("post", "/scenario/pg"), ("post", "/scenario/stop/x"), ("post", "/sink"), ("get", "/chain/hop")])
def test_mutating_endpoints_need_the_bearer(env, method, path):
    call = getattr(env.client, method)
    assert call(path).status_code == 401
    assert call(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert call(path, headers={"Authorization": KEY}).status_code == 401
    assert env.client.get("/scenarios").json() == {"running": [], "finished": []}


def test_no_key_configured_disables_mutations(tmp_path):
    e = Env(tmp_path, )
    e.settings.key = ""
    with e.client:
        assert e.client.post("/scenario/logs", headers=AUTH).status_code == 503
        assert e.client.get("/health").status_code == 200


def test_cpu_scenario(env):
    run = env.start("/scenario/cpu", seconds=1, workers=1)
    assert run["name"] == "cpu" and run["status"] == "running"
    assert run["params"] == {"seconds": 1, "workers": 1}
    assert any(r["id"] == run["id"] for r in env.client.get("/health").json()["running"])
    done = env.wait(run["id"])
    assert done["status"] == "finished" and done["result"] == {"workers": 1}
    assert done["elapsed_s"] >= 1


def test_cpu_default_workers_is_cpu_count(env, monkeypatch):
    monkeypatch.setattr(tentacle.Tentacle, "cpu", lambda self, run: {"workers": run.params["workers"]})
    run = env.start("/scenario/cpu", seconds=1)
    assert run["params"]["workers"] == (tentacle.os.cpu_count() or 1)


def test_memory_scenario(env):
    run = env.start("/scenario/memory", seconds=1, mb=8)
    done = env.wait(run["id"])
    assert done["status"] == "finished" and done["result"] == {"held_mb": 8}


def test_memory_guard(env):
    r = env.client.post("/scenario/memory", params={"mb": 60000}, headers=AUTH)
    assert r.status_code == 409


def test_disk_scenario_writes_then_deletes(env):
    run = env.start("/scenario/disk", seconds=1, mb=3)
    path = Path(env.settings.data_dir) / f"{run['id']}.bin"
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists()
    done = env.wait(run["id"])
    assert done["status"] == "finished" and done["result"]["written_mb"] == 3
    assert not path.exists()


def test_network_scenario_streams_to_peer_sink(env):
    run = env.start("/scenario/network", seconds=1, mbps=2)
    done = env.wait(run["id"])
    assert done["status"] == "finished"
    assert done["result"]["errors"] == 0
    assert done["result"]["sent_mb"] > 0
    assert done["result"]["peer"] == "http://peer.test/sink"


def test_network_needs_peer(tmp_path):
    e = Env(tmp_path, )
    e.settings.peer_url = ""
    with e.client:
        assert e.client.post("/scenario/network", headers=AUTH).status_code == 400


def test_sink_discards_and_counts(env):
    r = env.client.post("/sink", content=b"x" * 12345, headers=AUTH)
    assert r.json() == {"bytes": 12345}


def test_logs_scenario_and_log_format(env):
    run = env.start("/scenario/logs", seconds=1, rate=40, error_pct=50)
    done = env.wait(run["id"])
    assert done["status"] == "finished"
    emitted = done["result"]["emitted"]
    assert emitted >= 40
    assert done["result"]["count_error"] > 0

    lines = env.log_lines()
    storm = [l for l in lines if l.get("scenario.id") == run["id"] and "log.seq" in l]
    assert len(storm) == emitted
    severities = {l["severity_text"] for l in lines}
    assert severities <= set(tentacle.SEVERITY)
    for l in lines:
        assert REQUIRED_LOG_KEYS <= set(l), l
        assert l["service.name"] == "tentacle-a"
        assert l["severity_number"] == tentacle.SEVERITY[l["severity_text"]]
        assert l["timestamp"].endswith("Z")
    for l in storm:  # emitted inside a span
        assert len(l["trace_id"]) == 32 and len(l["span_id"]) == 16

    # start and end lines with the parameters
    start = [l for l in lines if l["body"] == "scenario logs start"]
    end = [l for l in lines if l["body"] == "scenario logs end (finished)"]
    assert start and start[0]["scenario.rate"] == 40 and start[0]["scenario.error_pct"] == 50
    assert end and end[0]["result.emitted"] == emitted

    # stdout carries the same JSON lines
    out = [json.loads(x) for x in env.stdout.getvalue().splitlines()]
    assert len(out) == len(lines)

    # and the OTel log pipeline got them, with trace context on the storm records
    otel = env.logs.get_finished_logs()
    assert len(otel) >= emitted
    rec = [x.log_record for x in otel if (x.log_record.attributes or {}).get("scenario.id") == run["id"]
           and "log.seq" in (x.log_record.attributes or {})]
    assert rec and all(r.trace_id for r in rec)


def test_scenario_span_named_and_attributed(env):
    run = env.start("/scenario/memory", seconds=1, mb=1)
    env.wait(run["id"])
    spans = [s for s in env.spans.get_finished_spans() if s.name == "scenario.memory"]
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["scenario.id"] == run["id"] and attrs["scenario.mb"] == 1 and attrs["tentacle"] == "tentacle-a"


def test_stop_works(env):
    run = env.start("/scenario/logs", seconds=300, rate=5)
    time.sleep(0.3)
    t0 = time.monotonic()
    r = env.client.post(f"/scenario/stop/{run['id']}", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "stopped"
    assert time.monotonic() - t0 < 5
    listing = env.client.get("/scenarios").json()
    assert [x["id"] for x in listing["finished"]] == [run["id"]]
    assert listing["running"] == []
    assert env.client.post("/scenario/stop/nope", headers=AUTH).status_code == 404


def test_stop_cpu_terminates_workers(env):
    run = env.start("/scenario/cpu", seconds=300, workers=1)
    time.sleep(0.5)
    r = env.client.post(f"/scenario/stop/{run['id']}", headers=AUTH)
    assert r.json()["status"] == "stopped"


def test_chain_produces_one_trace_per_request(env):
    run = env.start("/scenario/chain", count=3, latency_ms=5)
    done = env.wait(run["id"])
    assert done["status"] == "finished"
    assert done["result"]["ok"] == 3 and done["result"]["failed"] == 0

    spans = env.spans.get_finished_spans()
    scenario = [s for s in spans if s.name == "scenario.chain"]
    requests = [s for s in spans if s.name == "chain.request"]
    hops = [s for s in spans if s.name == "chain.hop"]
    assert len(scenario) == 1 and len(requests) == 3
    assert len(hops) == 6  # this tentacle (hop 1) + the peer (hop 2) per request
    trace_ids = {r.context.trace_id for r in requests}
    assert len(trace_ids) == 3
    assert scenario[0].context.trace_id not in trace_ids
    for req in requests:
        assert req.attributes["hop"] == 0 and req.attributes["tentacle"] == "tentacle-a"
        assert req.links and req.links[0].context.span_id == scenario[0].context.span_id
        in_trace = [s for s in spans if s.context.trace_id == req.context.trace_id]
        hop_attrs = sorted(s.attributes["hop"] for s in in_trace if s.name == "chain.hop")
        assert hop_attrs == [1, 2]
        assert all(s.attributes["tentacle"] == "tentacle-a" for s in in_trace if s.name == "chain.hop")
        # httpx client spans + FastAPI request spans in the same trace. (In-process routing through
        # TestClient carries the caller's context, so the ASGI span is INTERNAL here, SERVER in prod.)
        assert sum(s.kind.name == "CLIENT" and s.name == "GET" for s in in_trace) == 2
        assert sum(s.name == "GET /chain/hop" for s in in_trace) == 2
        assert not any(s.name.endswith((" http send", " http receive")) for s in in_trace)
        assert any(s.name == "chain.latency" for s in in_trace)
    hop_logs = [l for l in env.log_lines() if l["body"].startswith("hop ")]
    assert hop_logs and all("trace_id" in l for l in hop_logs)


def test_chain_injected_errors(env):
    run = env.start("/scenario/chain", count=2, error_pct=100)
    done = env.wait(run["id"])
    assert done["result"]["failed"] == 2
    spans = env.spans.get_finished_spans()
    assert all(s.status.status_code == StatusCode.ERROR for s in spans if s.name == "chain.request")
    assert any(s.attributes.get("error.injected") for s in spans if s.name == "chain.hop")


def test_chain_legs_to_pg_and_function_are_spans(tmp_path):
    e = Env(tmp_path, pg_dsn="postgresql://nobody@127.0.0.1:1/none", fn_url="http://fn.test/health")
    with e.client:
        run = e.start("/scenario/chain", count=1)
        done = e.wait(run["id"])
        assert done["status"] == "finished"
        spans = e.spans.get_finished_spans()
        req = [s for s in spans if s.name == "chain.request"][0]
        names = [s.name for s in spans if s.context.trace_id == req.context.trace_id]
        assert names.count("pg.select_now") == 2 and names.count("fn.call") == 2
        pg = [s for s in spans if s.name == "pg.select_now"][0]
        assert pg.status.status_code == StatusCode.ERROR  # nothing listens on :1; the leg reports, not crashes


def test_pg_scenario_needs_dsn_and_reports_failure(env, tmp_path):
    assert env.client.post("/scenario/pg", headers=AUTH).status_code == 400
    e = Env(tmp_path / "pg", pg_dsn="postgresql://nobody@127.0.0.1:1/none")
    with e.client:
        run = e.start("/scenario/pg", seconds=1, clients=1)
        done = e.wait(run["id"])
        assert done["status"] == "failed" and "OperationalError" in done["error"]
        assert done["result"]["connections"] == 0 and done["result"]["error_texts"] == [done["error"]]
        assert any(l["severity_text"] == "ERROR" and l.get("scenario.id") == run["id"] for l in e.log_lines())


class FakePsycopg:
    """psycopg as the pg scenario uses it, over a database where `schema` is "writable", "read-only" or "missing"
    and a statement starting with a key of `refuse` raises that error. Every statement lands in `sent`."""

    class Error(Exception):
        pass

    class InsufficientPrivilege(Error):
        pass

    def __init__(self, schema: str, refuse: dict[str, str] | None = None):
        self.schema, self.refuse = schema, refuse or {}
        self.sent: list[str] = []
        self.connects = 0

    def connect(self, dsn, **kwargs):
        self.connects += 1
        return FakePgConn(self)


class FakePgConn:
    def __init__(self, pg: FakePsycopg):
        self.pg, self.row = pg, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=None):
        for prefix, text in self.pg.refuse.items():
            if sql.startswith(prefix):
                raise self.pg.InsufficientPrivilege(text)
        self.pg.sent.append(sql)
        self.row = {"writable": (True,), "read-only": (False,), "missing": None}[self.pg.schema] \
            if "pg_namespace" in sql else (1, 1.0)
        time.sleep(0.002)
        return self

    def fetchone(self):
        return self.row


def pg_run(tmp_path, monkeypatch, fake: FakePsycopg, clients: int = 3) -> tuple[dict, list[dict]]:
    monkeypatch.setitem(sys.modules, "psycopg", fake)
    e = Env(tmp_path, pg_dsn="postgresql://tentacle@db.test/kraken")
    with e.client:
        done = e.wait(e.start("/scenario/pg", seconds=1, clients=clients)["id"])
    return done, e.log_lines()


def test_pg_scenario_writes_to_the_schema_its_user_owns(tmp_path, monkeypatch):
    """B-037: PostgreSQL 16 lets only the owner of public create there, so the table is tentacle.load."""
    fake = FakePsycopg("writable")
    done, _ = pg_run(tmp_path, monkeypatch, fake)
    res = done["result"]
    assert done["status"] == "finished" and done["error"] is None
    assert res["table"] == "tentacle.load" and res["fallback"] is None
    assert res["connections"] == fake.connects == 4 and res["clients"] == 3  # the setup session and three clients
    assert res["statements"] == len(fake.sent) and res["rows_written"] > 0 and res["errors"] == 0
    assert sum(s.startswith("INSERT INTO tentacle.load ") for s in fake.sent) == res["rows_written"]
    assert any(s.startswith("CREATE TABLE IF NOT EXISTS tentacle.load (") for s in fake.sent)
    assert not any("tentacle_load" in s or "TEMP" in s for s in fake.sent)


@pytest.mark.parametrize("schema,why", [("missing", "is missing"), ("read-only", "is not writable")])
def test_pg_scenario_falls_back_to_a_temp_table_and_says_so(tmp_path, monkeypatch, schema, why):
    fake = FakePsycopg(schema)
    done, logs = pg_run(tmp_path, monkeypatch, fake)
    res = done["result"]
    assert done["status"] == "finished" and res["rows_written"] > 0
    assert res["table"] == "pg_temp.load"
    assert res["fallback"] == f"schema tentacle {why}; each connection wrote its own TEMP TABLE, dropped when it closed"
    assert fake.sent.count("CREATE TEMP TABLE IF NOT EXISTS pg_temp.load (id bigserial PRIMARY KEY, tentacle text, "
                           "payload text, n int, created_at timestamptz DEFAULT now())") == 3  # one per connection
    assert any(l["severity_text"] == "WARN" and l["body"] == f"pg: {res['fallback']}" for l in logs)


def test_a_pg_run_that_writes_nothing_fails_with_the_servers_text(tmp_path, monkeypatch):
    """v-670380 failed 0.4 s in; the run must say why, and what it had done by then."""
    text = 'permission denied to create temporary tables in database "kraken"'
    done, logs = pg_run(tmp_path, monkeypatch, FakePsycopg("missing", {"CREATE TEMP TABLE": text}), clients=2)
    res = done["result"]
    assert done["status"] == "failed" and done["reason"] == "error"
    assert done["error"] == f"RuntimeError: no row written in 1 s: InsufficientPrivilege: {text}"
    assert res["error_texts"] == [f"InsufficientPrivilege: {text}"] and res["errors"] == 2
    assert res["connections"] == 3 and res["rows_written"] == 0 and res["statements"] == 1
    end = next(l for l in logs if l["body"].startswith("scenario pg failed: "))
    assert end["result.rows_written"] == 0 and end["result.error_texts"] == res["error_texts"]


def test_finished_runs_say_how_they_ended_newest_first(tmp_path):
    """2026-10-09 ~06:25Z: GET /scenarios on a tentacle answered finished [] although runs had ended there."""
    e = Env(tmp_path, pg_dsn="postgresql://nobody@127.0.0.1:1/none")
    with e.client:
        done = e.wait(e.start("/scenario/memory", seconds=1, mb=1)["id"])
        failed = e.wait(e.start("/scenario/pg", seconds=1, clients=1)["id"])
        long = e.start("/scenario/logs", seconds=300, rate=1)
        stopped = e.client.post(f"/scenario/stop/{long['id']}", headers=AUTH).json()
        listing = e.client.get("/scenarios").json()
    assert listing["running"] == []
    assert [r["id"] for r in listing["finished"]] == [long["id"], failed["id"], done["id"]]
    assert [(r["status"], r["reason"]) for r in listing["finished"]] == [
        ("stopped", "stopped"), ("failed", "error"), ("finished", "completed")]
    assert stopped["reason"] == "stopped" and long["reason"] is None
    for r in listing["finished"]:
        assert r["started_at"] <= r["ended_at"] and r["elapsed_s"] >= 0 and isinstance(r["result"], dict)
    assert listing["finished"][2]["result"] == {"held_mb": 1}
    assert "OperationalError" in listing["finished"][1]["error"]
    assert e.client.post(f"/scenario/stop/{done['id']}", headers=AUTH).json()["reason"] == "completed"


def test_the_last_50_finished_runs_are_kept(env, monkeypatch):
    monkeypatch.setattr(tentacle.Tentacle, "memory", lambda self, run: {"held_mb": run.params["mb"]})
    ids = [env.wait(env.start("/scenario/memory", seconds=1, mb=1)["id"])["id"] for _ in range(53)]
    finished = env.client.get("/scenarios").json()["finished"]
    assert len(finished) == tentacle.KEEP_FINISHED == 50
    assert [r["id"] for r in finished] == ids[::-1][:50]


def test_finished_runs_survive_a_restart_and_a_cut_run_says_so(tmp_path, monkeypatch):
    """A redeploy or a crash restarts the service (Restart=always) and used to empty the listing."""
    monkeypatch.setattr(tentacle.Tentacle, "memory", lambda self, run: {"held_mb": run.params["mb"]})
    first = Env(tmp_path)
    with first.client:
        done = first.wait(first.start("/scenario/memory", seconds=1, mb=1)["id"])
        cut = first.start("/scenario/logs", seconds=300, rate=1)
        state = json.loads((Path(first.settings.data_dir) / tentacle.RUNS_FILE).read_text())
        assert [r["id"] for r in state["running"]] == [cut["id"]]
        assert [r["id"] for r in state["finished"]] == [done["id"]]
    # the first process's shutdown stopped the logs run; put back what a kill -9 would have left
    (Path(first.settings.data_dir) / tentacle.RUNS_FILE).write_text(json.dumps(state))
    second = Env(tmp_path)
    with second.client:
        listing = second.client.get("/scenarios").json()
    assert listing["running"] == []
    lost, kept = listing["finished"]
    assert kept == done
    assert lost["id"] == cut["id"] and (lost["status"], lost["reason"]) == ("failed", "error")
    assert lost["error"].startswith("the tentacle restarted at ") and lost["ended_at"] >= lost["started_at"]
    assert any("1 run(s) ended by a restart" in line for line in second.stdout.getvalue().splitlines())


def test_an_unwritable_runs_file_does_not_fail_runs(tmp_path):
    e = Env(tmp_path)
    Path(e.settings.data_dir).mkdir(parents=True)
    (Path(e.settings.data_dir) / tentacle.RUNS_FILE).mkdir()  # a directory where the file should be
    with e.client:
        done = e.wait(e.start("/scenario/memory", seconds=1, mb=1)["id"])
        assert done["reason"] == "completed"
    assert "not written" in e.stdout.getvalue()


def test_parameter_validation(env):
    assert env.client.post("/scenario/cpu", params={"seconds": 0}, headers=AUTH).status_code == 422
    assert env.client.post("/scenario/logs", params={"error_pct": 101}, headers=AUTH).status_code == 422


def test_runs_without_a_collector(tmp_path, capfd):
    """Default OTLP exporters pointed at a closed port: requests stay fast, nothing is raised."""
    settings = tentacle.Settings(name="lonely", key=KEY, service_name="lonely",
                                 otlp_endpoint="http://127.0.0.1:9", log_file=str(tmp_path / "t.jsonl"),
                                 data_dir=str(tmp_path / "d"), self_url="http://self.test")
    app = tentacle.create_app(settings, http_client_factory=lambda a: TestClient(a), log_stream=io.StringIO())
    with TestClient(app) as client:
        t0 = time.monotonic()
        for _ in range(20):
            assert client.get("/health").status_code == 200
        run = client.post("/scenario/chain", params={"count": 5}, headers=AUTH).json()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            fin = client.get("/scenarios").json()["finished"]
            if fin:
                break
            time.sleep(0.05)
        assert fin[0]["id"] == run["id"] and fin[0]["result"]["ok"] == 5
        assert time.monotonic() - t0 < 10
    assert "Traceback" not in capfd.readouterr().err


def test_settings_from_env():
    s = tentacle.Settings.from_env({"TENTACLE_NAME": "t1", "TENTACLE_KEY": "k", "PEER_URL": "http://p:8800/",
                                    "TENTACLE_PORT": "9000"})
    assert (s.name, s.key, s.port, s.peer_url) == ("t1", "k", 9000, "http://p:8800")
    assert s.service_name == "t1"
    assert s.otlp_endpoint == "http://127.0.0.1:4318"
    assert s.self_url == "http://127.0.0.1:9000"
    s = tentacle.Settings.from_env({"TENTACLE_NAME": "t1", "OTEL_SERVICE_NAME": "svc",
                                    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318/"})
    assert (s.service_name, s.otlp_endpoint, s.port) == ("svc", "http://c:4318", 8800)


# --- packaging ------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def test_install_script_is_user_data_ready():
    script = (ROOT / "install.sh").read_text()
    assert script.startswith("#!/bin/bash\n")
    import subprocess
    subprocess.run(["bash", "-n", str(ROOT / "install.sh")], check=True)


def test_embedded_unit_matches_tentacle_service():
    script = (ROOT / "install.sh").read_text()
    embedded = script.split("<<'UNIT_EOF'\n", 1)[1].split("UNIT_EOF\n", 1)[0]
    assert embedded == (ROOT / "tentacle.service").read_text()


def test_unit_essentials():
    unit = (ROOT / "tentacle.service").read_text()
    for line in ("User=tentacle", "Restart=always", "EnvironmentFile=/etc/tentacle/env",
                 "ExecStart=/opt/tentacle/venv/bin/python /opt/tentacle/tentacle.py"):
        assert line in unit.splitlines()
