"""Acceptance tests for issue #41: a read-only run dashboard on 127.0.0.1."""
from __future__ import annotations

import http.client
import importlib
import json
import logging
import os
import socket
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from prxref import cli, config, dashboard
from prxref.triage import Finding

ROOT = Path(__file__).resolve().parent.parent
EVIL = '<script>alert("x")</script>'


@pytest.fixture(autouse=True)
def _fresh_ring():
    dashboard.reset()
    yield
    dashboard.reset()


@pytest.fixture(autouse=True)
def _isolated_environ(monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    monkeypatch.delenv("PRXREF_DASHBOARD_PORT", raising=False)


@pytest.fixture(autouse=True)
def _no_leaked_log_filters():
    from prxref.gate import serve

    yield
    for handler in logging.getLogger().handlers:
        for f in [f for f in handler.filters if isinstance(f, serve._RedactingFilter)]:
            handler.removeFilter(f)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served():
    servers = []

    def start(source, label="test"):
        server = dashboard.start_dashboard_listener(_free_port(), source, source_label=label)
        assert server is not None
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _get(server, path: str):
    host, port = server.server_address[:2]
    return urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=5)


def _request(server, method: str, path: str = "/"):
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request(method, path, body=b"x=1" if method != "GET" else None)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _finding(severity: str, drop_reason: str | None = None) -> Finding:
    return Finding(
        file="src/a.py", line=3, severity=severity, title="t", body="b",
        confidence=0.9, drop_reason=drop_reason,
    )


def _result(**over) -> dict:
    result = {
        "verdict": "Request-Changes",
        "findings_active": [_finding("error"), _finding("warning"), _finding("warning")],
        "findings_dropped": [_finding("error", "hedged: \"might\"")],
        "elapsed_ms": 2500,
        "input_tokens": 1000,
        "output_tokens": 200,
        "cost_usd": 0.0125,
        "cost_estimated": False,
        "cost_by_model": {"acme/model-a": 0.01, "acme/model-b": 0.0025},
        "sampling": {"temperature": 0, "seed": None, "models": ["acme/model-a", "acme/model-b"]},
    }
    result.update(over)
    return result


# Criterion 1: read-only UI on loopback with runs, verdicts, costs, finding counts.


def test_port_zero_starts_nothing():
    assert dashboard.start_dashboard_listener(0, dashboard.RingSource()) is None


def test_listener_binds_loopback_only(served):
    server = served(dashboard.RingSource())
    assert server.server_address[0] == "127.0.0.1"


def test_page_and_json_show_verdict_cost_counts_tokens_models_duration(served):
    dashboard.observe_result(_result(), pr_url="https://github.com/acme/widget/pull/7")
    server = served(dashboard.ring())
    with _get(server, "/runs.json") as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "application/json"
        rows = json.loads(resp.read())
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == set(dashboard.ROW_KEYS)
    assert row["repo"] == "acme/widget" and row["pr_number"] == 7
    assert row["pr_url"] == "https://github.com/acme/widget/pull/7"
    assert row["verdict"] == "Request-Changes"
    assert row["findings"] == {"error": 1, "warning": 2, "spec": 0, "outofscope": 0, "other": 0}
    assert row["cost_usd"] == pytest.approx(0.0125)
    assert (row["input_tokens"], row["output_tokens"]) == (1000, 200)
    assert row["models"] == ["acme/model-a", "acme/model-b"]
    assert row["elapsed_ms"] == 2500 and row["source"] == "memory"
    with _get(server, "/") as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/html; charset=utf-8"
        assert "script-src" not in resp.headers["Content-Security-Policy"]
        page = resp.read().decode()
    assert "<script" not in page.lower()
    for text in ("acme/widget#7", 'href="https://github.com/acme/widget/pull/7"',
                 "Request-Changes", "error 1, warning 2", "$0.0125",
                 "1000 in / 200 out", "acme/model-a, acme/model-b", "2.5 s"):
        assert text in page, text


def test_unknown_cost_renders_unknown_not_zero(served):
    dashboard.observe_result(_result(cost_usd=None), pr_url="https://github.com/acme/widget/pull/8")
    server = served(dashboard.ring())
    with _get(server, "/") as resp:
        page = resp.read().decode()
    assert ">unknown<" in page and "$0.0000" not in page
    with _get(server, "/runs.json") as resp:
        assert json.loads(resp.read())[0]["cost_usd"] is None


def test_newest_first_and_capped_at_max_runs():
    for n in range(dashboard.MAX_RUNS + 5):
        dashboard.observe_result(_result(elapsed_ms=n), pr_url=f"https://github.com/acme/widget/pull/{n + 1}")
    rows = dashboard.ring().rows()
    assert len(rows) == dashboard.MAX_RUNS
    assert [r["pr_number"] for r in rows[:2]] == [dashboard.MAX_RUNS + 5, dashboard.MAX_RUNS + 4]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_no_write_surface_other_methods_are_405(served, method):
    server = served(dashboard.ring())
    status, headers, _ = _request(server, method, "/")
    assert status == 405
    assert headers["Allow"] == "GET, HEAD"
    assert dashboard.ring().rows() == []


@pytest.mark.parametrize("path", ["/metrics", "/runs", "/../etc/passwd", "/runs.json/x", "/favicon.ico"])
def test_other_paths_are_404(served, path):
    server = served(dashboard.ring())
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(server, path)
    assert err.value.code == 404


def test_page_has_no_form_and_no_remote_asset(served):
    dashboard.observe_result(_result(), pr_url="https://github.com/acme/widget/pull/7")
    server = served(dashboard.ring())
    with _get(server, "/") as resp:
        page = resp.read().decode().lower()
    assert "<form" not in page and "<input" not in page and "<button" not in page
    assert "src=" not in page and "<link" not in page and "@import" not in page


def test_script_in_any_field_renders_inert_from_memory(served):
    dashboard.observe_result(
        _result(
            verdict=EVIL,
            cost_by_model={EVIL: 0.1},
            findings_active=[_finding(EVIL)],
        ),
        pr_url='https://github.com/acme/widget/pull/9?"><script>alert(1)</script>',
    )
    server = served(dashboard.ring())
    with _get(server, "/") as resp:
        page = resp.read().decode()
    assert "<script" not in page.lower()
    assert "&lt;script&gt;" in page


def test_a_javascript_url_is_never_a_link(served):
    dashboard.ring().add(dashboard.summarize(_result(), pr_url="javascript:alert(1)"))
    server = served(dashboard.ring())
    with _get(server, "/") as resp:
        page = resp.read().decode()
    assert "href=" not in page
    assert "javascript:alert(1)" in page


def test_odd_types_never_raise():
    row = dashboard.summarize(
        {"verdict": {"x": 1}, "cost_usd": "lots", "findings": "nope", "input_tokens": True,
         "elapsed_ms": float("inf"), "cost_by_model": [1, 2], "sampling": "x"},
        pr_number="7", finished_at=float("nan"),
    )
    assert row["verdict"] is None and row["cost_usd"] is None and row["input_tokens"] is None
    assert row["elapsed_ms"] is None and row["finished_at"] is None and row["pr_number"] is None
    assert row["models"] == [] and sum(row["findings"].values()) == 0
    assert "<td>" in dashboard.render_page([row], source_label="x")


def test_a_failing_source_serves_an_empty_page_not_a_500(served):
    class Broken:
        def rows(self):
            raise RuntimeError("boom")

    server = served(Broken())
    with _get(server, "/runs.json") as resp:
        assert resp.status == 200 and json.loads(resp.read()) == []


def test_observe_never_raises_on_a_non_dict():
    dashboard.observe_result(None)
    dashboard.observe_result("x", pr_url=42)
    assert dashboard.ring().rows() == []


# The `prxref serve` seam: a finished `_run_review` feeds the ring.


@pytest.fixture
def stubbed_review(monkeypatch, tmp_path):
    def _orchestrate(**kwargs):
        return _result(posted=False, chunk_count=1, chunks_reviewed=1, chunks_failed=0)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("prxref.orchestrator.orchestrate_review", _orchestrate)
    monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: object())
    monkeypatch.setattr(cli, "make_forge", lambda ref: object())
    for name in ("PRXREF_EVAL_PINS", config.CONFIG_FILE_ENV, "PRXREF_NOTIFY_URL"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_webhook_review_lands_in_the_served_ring(stubbed_review, served):
    cli._webhook_handler("https://github.com/acme/widget/pull/12")
    server = served(dashboard.ring())
    with _get(server, "/runs.json") as resp:
        rows = json.loads(resp.read())
    assert [(r["repo"], r["pr_number"], r["verdict"]) for r in rows] == [("acme/widget", 12, "Request-Changes")]
    assert rows[0]["head_sha"] is None
    assert sorted(p.name for p in stubbed_review.iterdir()) == []


# Criterion 2: the data source is the existing run records; no new persistence.


def _gate_fixture(tmp_path):
    from prxref.gate.store import Store

    store = Store(tmp_path / "state.sqlite")
    runs = tmp_path / "runs"
    runs.mkdir()
    a = store.enqueue("acme/widget", 3, "a" * 40, "b" * 40, "main", "opened")
    b = store.enqueue("acme/gadget", 4, "c" * 40, "d" * 40, "main", "opened")
    c = store.enqueue("acme/broken", 5, "e" * 40, "f" * 40, "main", "opened")
    record = cli._build_json_result(_result())
    run_record = json.loads(json.dumps(_result(), default=str))
    for i, gen in enumerate((a, b)):
        d = runs / str(gen.id)
        d.mkdir()
        (d / "result.json").write_text(json.dumps(record), encoding="utf-8")
        (d / "run_record.json").write_text(json.dumps(run_record), encoding="utf-8")
        os.utime(d / "result.json", (1_000_000 + i, 1_000_000 + i))
    (runs / str(c.id)).mkdir()
    (runs / str(c.id) / "result.json").write_text('{"verdict": "Appr', encoding="utf-8")
    (runs / "999").mkdir()
    (runs / "999" / "result.json").write_text(json.dumps({"verdict": "Approved"}), encoding="utf-8")
    (runs / "notes").mkdir()
    (runs / "notes" / "result.json").write_text(json.dumps({"verdict": "Approved"}), encoding="utf-8")
    (runs / "998").mkdir()
    return store, runs, a, b


def _tree(path: Path) -> dict[str, tuple[int, float]]:
    return {str(p.relative_to(path)): (p.stat().st_size, p.stat().st_mtime) for p in sorted(path.rglob("*"))}


def test_gate_rows_come_from_the_runs_dir_joined_to_the_store(tmp_path, served):
    store, runs, a, b = _gate_fixture(tmp_path)
    server = served(dashboard.RunsDirSource(runs, lookup=store.get), "the gate runs directory")
    with _get(server, "/runs.json") as resp:
        rows = json.loads(resp.read())
    by_repo = {r["repo"]: r for r in rows}
    assert set(by_repo) == {"acme/widget", "acme/gadget", None}
    assert len(rows) == 3, "the truncated record, the non-numeric dir and the empty dir are skipped"
    widget = by_repo["acme/widget"]
    assert widget["pr_url"] == "https://github.com/acme/widget/pull/3"
    assert widget["head_sha"] == "a" * 40
    assert widget["verdict"] == "Request-Changes"
    assert widget["findings"] == {"error": 1, "warning": 2, "spec": 0, "outofscope": 0, "other": 0}
    assert widget["cost_usd"] == pytest.approx(0.0125)
    assert widget["models"] == ["acme/model-a", "acme/model-b"]
    assert widget["source"] == "runs_dir"
    assert by_repo[None]["verdict"] == "Approved" and by_repo[None]["pr_url"] is None
    with _get(server, "/") as resp:
        page = resp.read().decode()
    assert "acme/widget#3" in page and "<code>aaaaaaa</code>" in page
    store.close()


def test_gate_rows_are_newest_first_by_mtime(tmp_path):
    store, runs, a, b = _gate_fixture(tmp_path)
    rows = dashboard.RunsDirSource(runs, lookup=lambda gid: (_ for _ in ()).throw(RuntimeError())).rows()
    assert [r["finished_at"] for r in rows if r["finished_at"] and r["finished_at"] < 2_000_000] == [
        1_000_001, 1_000_000,
    ]
    assert dashboard.RunsDirSource(runs, limit=1).rows()[0]["verdict"] == "Approved"
    store.close()


def test_reading_the_gate_dashboard_writes_nothing(tmp_path, served):
    store, runs, a, b = _gate_fixture(tmp_path)
    before = _tree(runs)
    server = served(dashboard.RunsDirSource(runs, lookup=store.get))
    for path in ("/", "/runs.json", "/", "/nope"):
        try:
            _get(server, path).read()
        except urllib.error.HTTPError:
            pass
    _request(server, "POST", "/")
    assert _tree(runs) == before
    store.close()


def test_script_in_a_gate_run_record_renders_inert(tmp_path, served):
    from prxref.gate.store import Store

    store = Store(tmp_path / "state.sqlite")
    gen = store.enqueue(f"acme/{EVIL}", 1, EVIL, "b" * 40, "main", "opened")
    runs = tmp_path / "runs"
    (runs / str(gen.id)).mkdir(parents=True)
    record = {"verdict": EVIL, "findings": [{"severity": EVIL, "drop_reason": None}], "cost_usd": None}
    (runs / str(gen.id) / "result.json").write_text(json.dumps(record), encoding="utf-8")
    (runs / str(gen.id) / "run_record.json").write_text(
        json.dumps({"cost_by_model": {EVIL: None}}), encoding="utf-8")
    server = served(dashboard.RunsDirSource(runs, lookup=store.get))
    with _get(server, "/") as resp:
        page = resp.read().decode()
    assert "<script" not in page.lower()
    assert page.count("&lt;script&gt;") >= 3
    with _get(server, "/runs.json") as resp:
        assert json.loads(resp.read())[0]["verdict"] == EVIL
    store.close()


def test_gate_models_are_the_ones_that_answered_not_the_chain(tmp_path):
    chain = {"temperature": 0, "seed": None, "models": ["acme/a", "acme/b", "acme/c"]}
    result = _result(cost_by_model={"acme/b": 0.01}, sampling=chain)
    d = tmp_path / "runs" / "1"
    d.mkdir(parents=True)
    (d / "result.json").write_text(json.dumps(cli._build_json_result(result)), encoding="utf-8")
    (d / "run_record.json").write_text(json.dumps(result, default=str), encoding="utf-8")
    assert dashboard.RunsDirSource(tmp_path / "runs").rows()[0]["models"] == ["acme/b"]
    (d / "run_record.json").unlink()
    assert dashboard.RunsDirSource(tmp_path / "runs").rows()[0]["models"] == ["acme/a", "acme/b", "acme/c"]


def test_a_missing_runs_dir_is_an_empty_list(tmp_path):
    assert dashboard.RunsDirSource(tmp_path / "absent").rows() == []


# Criterion 3: zero new services, disabled by default behind a flag.


def _fake_webhooks(monkeypatch):
    fake = types.ModuleType("prxref.webhooks")
    fake.serve = lambda port, host, handler, reply_handler=None: None
    monkeypatch.setitem(importlib.sys.modules, "prxref.webhooks", fake)


def test_the_flag_defaults_off():
    assert config._DEFAULTS["dashboard_port"] == 0
    assert config.load_config()["dashboard_port"] == 0
    assert "dashboard_port" in config.ENV_ONLY_KEYS and "dashboard_port" not in config.FILE_KEYS


def test_cmd_serve_starts_the_in_memory_dashboard_when_the_port_is_set(monkeypatch):
    started = []
    monkeypatch.setattr(
        dashboard, "start_dashboard_listener",
        lambda port, source, source_label="": started.append((port, source)),
    )
    monkeypatch.setenv("PRXREF_DASHBOARD_PORT", "9470")
    _fake_webhooks(monkeypatch)
    assert cli.main(["serve", "--port", "9090", "--host", "127.0.0.1"]) == 0
    assert started == [(9470, dashboard.ring())]


def test_cmd_serve_leaves_the_dashboard_off_by_default(monkeypatch):
    started = []
    monkeypatch.setattr(
        dashboard, "start_dashboard_listener",
        lambda port, source, source_label="": started.append(port),
    )
    _fake_webhooks(monkeypatch)
    assert cli.main(["serve"]) == 0
    assert started in ([], [0])


def test_a_bad_port_exits_2_from_serve(monkeypatch, capsys):
    monkeypatch.setenv("PRXREF_DASHBOARD_PORT", "70000")
    _fake_webhooks(monkeypatch)
    assert cli.main(["serve"]) == 2
    assert "PRXREF_DASHBOARD_PORT" in capsys.readouterr().err


def _gate_env(tmp_path, extra: str = "") -> Path:
    env = tmp_path / "gate.env"
    env.write_text("PRXREF_GATE_APP_ID=1\n" + extra, encoding="utf-8")
    env.chmod(0o600)
    return env


def test_gate_serve_starts_the_runs_dir_dashboard_when_the_port_is_set(tmp_path, monkeypatch):
    from prxref.gate import serve

    started = []
    fake_store = types.SimpleNamespace(get=lambda gid: None)
    monkeypatch.setattr(
        dashboard, "start_dashboard_listener",
        lambda port, source, source_label="": started.append((port, source)),
    )
    monkeypatch.setattr(serve, "settings_from_config", lambda cfg: types.SimpleNamespace(runs_dir=tmp_path))
    monkeypatch.setattr(serve, "build", lambda settings, cfg: types.SimpleNamespace(store=fake_store))
    monkeypatch.setattr(serve, "run_gate", lambda gate, host, port: 0)
    monkeypatch.setattr(serve, "require_jwt", lambda: None)
    assert serve.serve_main(_gate_env(tmp_path, "PRXREF_DASHBOARD_PORT=9471\n"), "127.0.0.1", 8787) == 0
    assert len(started) == 1
    port, source = started[0]
    assert port == 9471 and isinstance(source, dashboard.RunsDirSource)
    assert source.runs_dir == tmp_path and source.lookup == fake_store.get


def test_gate_serve_leaves_the_dashboard_off_by_default(tmp_path, monkeypatch):
    from prxref.gate import serve

    started = []
    monkeypatch.setattr(dashboard, "start_dashboard_listener", lambda *a, **k: started.append(a))
    monkeypatch.setattr(serve, "settings_from_config", lambda cfg: "settings")
    monkeypatch.setattr(serve, "build", lambda settings, cfg: "gate")
    monkeypatch.setattr(serve, "run_gate", lambda gate, host, port: 0)
    monkeypatch.setattr(serve, "require_jwt", lambda: None)
    assert serve.serve_main(_gate_env(tmp_path), "127.0.0.1", 8787) == 0
    assert started == []


def test_gate_serve_closes_the_dashboard_on_exit(tmp_path, monkeypatch):
    from prxref.gate import serve

    port = _free_port()
    monkeypatch.setattr(serve, "settings_from_config", lambda cfg: types.SimpleNamespace(runs_dir=tmp_path))
    monkeypatch.setattr(serve, "build", lambda s, c: types.SimpleNamespace(store=types.SimpleNamespace(get=None)))
    seen = {}

    def _run(gate, host, p):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/runs.json", timeout=5) as resp:
            seen["rows"] = json.loads(resp.read())
        return 0

    monkeypatch.setattr(serve, "run_gate", _run)
    monkeypatch.setattr(serve, "require_jwt", lambda: None)
    assert serve.serve_main(_gate_env(tmp_path, f"PRXREF_DASHBOARD_PORT={port}\n"), "127.0.0.1", 8787) == 0
    assert seen["rows"] == []
    with socket.socket() as s:
        s.settimeout(2)
        assert s.connect_ex(("127.0.0.1", port)) != 0


def test_no_new_dependency_or_service():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "dashboard" not in pyproject.lower()
    source = (ROOT / "src" / "prxref" / "dashboard.py").read_text(encoding="utf-8")
    imports = {line.split()[1].split(".")[0] for line in source.splitlines()
               if line.startswith(("import ", "from "))}
    assert imports <= {"__future__", "html", "json", "logging", "math", "threading", "time",
                       "collections", "http", "pathlib", "typing", "prxref"}


def test_the_doc_covers_enable_fields_sources_and_loopback():
    doc = (ROOT / "docs" / "dashboard.md").read_text(encoding="utf-8")
    assert "PRXREF_DASHBOARD_PORT" in doc and "127.0.0.1" in doc
    assert "PRXREF_GATE_RUNS_DIR" in doc and "restart" in doc and "405" in doc
    for key in dashboard.ROW_KEYS:
        assert f"`{key}`" in doc, key
    assert "docs/dashboard.md" in (ROOT / "README.md").read_text(encoding="utf-8")
