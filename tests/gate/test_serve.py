"""prxref gate: wiring and operator commands (#102)."""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from prxref import cli
from prxref.gate import rulesets, serve, worker
from prxref.gate.store import Store


@pytest.fixture(autouse=True)
def _isolated_environ(monkeypatch):
    """apply_env_file writes os.environ; keep every test's writes inside the test."""
    monkeypatch.setattr(os, "environ", os.environ.copy())


@pytest.fixture(autouse=True)
def _no_leaked_log_filters():
    """serve_main filters the root handlers, including pytest's session-long ones."""
    import logging

    yield
    for handler in logging.getLogger().handlers:
        for f in [f for f in handler.filters if isinstance(f, serve._RedactingFilter)]:
            handler.removeFilter(f)


def _env_file(tmp_path, lines, mode=0o600):
    path = tmp_path / "env"
    path.write_text("\n".join(lines) + "\n")
    path.chmod(mode)
    return path


def _paused(state):
    store = Store(state)
    try:
        return store.is_paused()
    finally:
        store.close()


def test_gate_without_an_action_prints_help_and_exits_2(capsys):
    assert cli.main(["gate"]) == 2
    assert "usage:" in capsys.readouterr().err.lower()


def test_serve_names_the_missing_variable_and_exits_2(tmp_path, capsys):
    env = _env_file(tmp_path, ["PRXREF_GATE_STATE_FILE=" + str(tmp_path / "s.db")])
    assert cli.main(["gate", "serve", "--env-file", str(env)]) == 2
    assert "PRXREF_GATE_APP_ID" in capsys.readouterr().err


def test_serve_refuses_an_env_file_others_can_read(tmp_path, capsys):
    env = _env_file(tmp_path, ["PRXREF_GATE_APP_ID=1"], mode=0o644)
    assert cli.main(["gate", "serve", "--env-file", str(env)]) == 2
    assert "0600" in capsys.readouterr().err


def test_serve_without_the_extra_exits_2(tmp_path, monkeypatch, capsys):
    from prxref import gate

    def missing():
        raise serve.GateDependencyError(gate.EXTRA_HINT)

    monkeypatch.setattr(serve, "require_jwt", missing)
    env = _env_file(tmp_path, ["PRXREF_GATE_APP_ID=1"])
    assert cli.main(["gate", "serve", "--env-file", str(env)]) == 2
    assert "github-app" in capsys.readouterr().err


@pytest.mark.parametrize("line, expected", [(None, "base"), ("PRXREF_RULES_SOURCE=head", "head")])
def test_serve_defaults_rules_source_to_base(tmp_path, monkeypatch, line, expected):
    seen = {}
    monkeypatch.setattr(serve, "settings_from_config", lambda cfg: seen.setdefault("cfg", cfg))
    monkeypatch.setattr(serve, "build", lambda settings, cfg: "gate")
    monkeypatch.setattr(serve, "run_gate", lambda gate, host, port: 0)
    env = _env_file(tmp_path, [x for x in ["PRXREF_GATE_APP_ID=1", line] if x])
    assert serve.serve_main(env, "127.0.0.1", 8787) == 0
    assert seen["cfg"]["rules_source"] == expected


def test_pause_resume_and_status(tmp_path, capsys):
    state = tmp_path / "gate.db"
    env = _env_file(tmp_path, [f"PRXREF_GATE_STATE_FILE={state}"])
    seed = Store(state)
    seed.enqueue("acme/widget", 7, "h" * 40, "b" * 40, "main", "opened")
    seed.close()
    assert cli.main(["gate", "pause", "--env-file", str(env)]) == 0
    assert _paused(state)
    assert cli.main(["gate", "status", "--env-file", str(env)]) == 0
    out = capsys.readouterr().out
    assert "paused: yes" in out and "queue depth: 1" in out and "acme/widget#7" in out
    assert cli.main(["gate", "resume", "--env-file", str(env)]) == 0
    assert not _paused(state)


def test_status_without_a_state_file_exits_2(tmp_path, capsys):
    env = _env_file(tmp_path, ["PRXREF_GATE_APP_ID=1"])
    assert cli.main(["gate", "status", "--env-file", str(env)]) == 2
    assert "PRXREF_GATE_STATE_FILE" in capsys.readouterr().err


def test_render_commands_print_the_golden_json(capsys):
    golden = Path(__file__).parent / "golden"
    assert cli.main(["gate", "render-manifest", "--webhook-url", "https://sdlc.example.dev/webhook"]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads((golden / "manifest.json").read_text())
    assert cli.main(["gate", "render-ruleset", "--integration-id", "123456"]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads((golden / "ruleset.json").read_text())


@pytest.mark.parametrize("diff, rc", [("", 0), ("--- rendered\n+++ live\n", 1)])
def test_apply_ruleset_diff_exits_1_on_drift(monkeypatch, capsys, diff, rc):
    monkeypatch.setattr(rulesets, "diff_ruleset", lambda repo, iid: diff)
    assert cli.main(["gate", "apply-ruleset", "--repo", "o/r", "--integration-id", "5", "--diff"]) == rc
    assert ("--- rendered" in capsys.readouterr().out) is bool(diff)


def test_apply_ruleset_prints_the_id(monkeypatch, capsys):
    monkeypatch.setattr(rulesets, "apply_ruleset", lambda repo, iid: 77)
    assert cli.main(["gate", "apply-ruleset", "--repo", "o/r", "--integration-id", "5"]) == 0
    assert "77" in capsys.readouterr().out


def test_make_review_reads_through_an_installation_token(tmp_path, monkeypatch):
    seen = {}

    def fake_review(gen, inner, *, rules_source, runs_dir, register=None):
        seen.update(inner=inner, rules_source=rules_source, runs_dir=runs_dir)
        register("capture")
        return "outcome"

    monkeypatch.setattr(worker, "review_with_prxref", fake_review)

    class _Auth:
        def token_for(self, repo):
            return f"tok-{repo}"

    settings = type("S", (), {"runs_dir": tmp_path / "runs"})()
    store = Store(tmp_path / "s.db")
    gen = store.enqueue("acme/widget", 7, "h" * 40, "b" * 40, "main", "opened")
    sink = []
    assert serve.make_review(_Auth(), settings, "base")(gen, register=sink.append) == "outcome"
    store.close()
    assert sink == ["capture"]
    assert isinstance(seen["inner"], worker.InstallationForge)
    assert seen["inner"]._headers("github.com")["Authorization"] == "Bearer tok-acme/widget"
    assert (seen["rules_source"], seen["runs_dir"]) == ("base", tmp_path / "runs")


@pytest.mark.parametrize("exc", [FileNotFoundError("gh"), ValueError("not json"), KeyError("id")])
def test_apply_ruleset_gh_failures_exit_1(monkeypatch, capsys, exc):
    def boom(repo, iid):
        raise exc

    monkeypatch.setattr(rulesets, "apply_ruleset", boom)
    assert cli.main(["gate", "apply-ruleset", "--repo", "o/r", "--integration-id", "5"]) == 1
    assert capsys.readouterr().err.startswith("prxref gate: ")


def test_operator_commands_close_the_store(tmp_path, monkeypatch):
    state = tmp_path / "gate.db"
    env = _env_file(tmp_path, [f"PRXREF_GATE_STATE_FILE={state}"])
    opened = []
    real_close = Store.close

    def tracking_close(self):
        opened.append(self)
        real_close(self)

    monkeypatch.setattr(Store, "close", tracking_close)
    for action in ("pause", "status", "resume"):
        assert cli.main(["gate", action, "--env-file", str(env)]) == 0
    assert len(opened) == 3


def test_run_gate_runs_every_thread_then_closes_the_store(tmp_path):
    import threading
    from types import SimpleNamespace

    ran = []

    class _Loop:
        def __init__(self, name):
            self.name = name

        def run(self, stop):
            ran.append(self.name)
            stop.wait()

    store = Store(tmp_path / "s.db")
    gate = SimpleNamespace(store=store, receiver=object(), worker=_Loop("worker"), watchdog=_Loop("watchdog"))
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    import signal

    before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    assert serve.run_gate(gate, "127.0.0.1", 0, stop=stop) == 0
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before
    assert sorted(ran) == ["watchdog", "worker"]
    with pytest.raises(sqlite3.ProgrammingError):
        store.queue_depth()


def test_installed_redaction_masks_messages_and_tracebacks_from_child_loggers():
    import io
    import logging

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        serve._install_redaction()
        logging.getLogger("prxref.orchestrator").error("get_pr failed: %s", "401 for token ghs_SECRETVALUE123")
        try:
            raise RuntimeError("401 for Bearer ghs_EXCSECRET2")
        except RuntimeError:
            logging.getLogger("prxref.orchestrator").exception("get_pr failed")
    finally:
        root.removeHandler(handler)
    out = buf.getvalue()
    assert "ghs_SECRETVALUE123" not in out and "ghs_EXCSECRET2" not in out
    assert "get_pr failed: 401 for [redacted]" in out
    assert "RuntimeError: 401 for [redacted]" in out


def test_serve_main_installs_the_filter_on_root_handlers(tmp_path, monkeypatch):
    import logging

    handler = logging.StreamHandler(open(os.devnull, "w"))
    root = logging.getLogger()
    root.addHandler(handler)
    monkeypatch.setattr(serve, "settings_from_config", lambda cfg: None)
    monkeypatch.setattr(serve, "build", lambda settings, cfg: "gate")
    monkeypatch.setattr(serve, "run_gate", lambda gate, host, port: 0)
    try:
        serve.serve_main(_env_file(tmp_path, ["PRXREF_GATE_APP_ID=1"]), "127.0.0.1", 0)
        assert any(isinstance(f, serve._RedactingFilter) for f in handler.filters)
    finally:
        root.removeHandler(handler)
        handler.stream.close()


def test_a_non_gh_error_in_status_is_not_reported_as_exit_1(tmp_path, monkeypatch):
    env = _env_file(tmp_path, [f"PRXREF_GATE_STATE_FILE={tmp_path / 'g.db'}"])

    def boom(store):
        raise KeyError("id")

    monkeypatch.setattr(serve, "status_text", boom)
    with pytest.raises(KeyError):
        cli.main(["gate", "status", "--env-file", str(env)])


def test_status_prints_lease_none_when_the_lease_has_expired(tmp_path):
    clock = [1000.0]
    store = Store(tmp_path / "g.db", clock=lambda: clock[0])
    try:
        assert store.acquire_lease("host:1", 60) is not None
        assert "lease: host:1 until" in serve.status_text(store)
        clock[0] += 61
        assert "lease: none" in serve.status_text(store)
    finally:
        store.close()


def test_a_gate_auth_error_from_build_exits_2(tmp_path, monkeypatch, capsys):
    from prxref.gate.app_auth import GateAuthError

    def boom(settings, cfg):
        raise GateAuthError("GitHub rejected the app JWT")

    monkeypatch.setattr(serve, "settings_from_config", lambda cfg: "settings")
    monkeypatch.setattr(serve, "build", boom)
    env = _env_file(tmp_path, ["PRXREF_GATE_APP_ID=1"])
    assert cli.main(["gate", "serve", "--env-file", str(env)]) == 2
    assert "prxref gate: GitHub rejected the app JWT" in capsys.readouterr().err


def test_build_registers_the_webhook_and_bedrock_secrets_for_redaction(tmp_path, monkeypatch):
    class FakeAuth:
        def __init__(self, *a, **k):
            pass

        def bot_login(self):
            return "sdlc[bot]"

    monkeypatch.setattr(serve, "AppAuth", FakeAuth)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-key-0123456789")
    key = tmp_path / "k.pem"
    key.write_text("x")
    cfg = {
        "gate_webhook_secret": "whsec-abcdef123456",
        "gate_webhook_secret_previous": "whprev-abcdef123456",
        "rules_source": "base",
        "llm_reasoning_effort": "",
    }
    gs = serve.GateSettings(
        app_id=1,
        private_key_file=key,
        installation_ids=frozenset({2}),
        repos=frozenset({"a/b"}),
        secret_grace_minutes=30,
        state_file=tmp_path / "s.db",
        runs_dir=tmp_path / "runs",
        review_deadline=900,
        watchdog_seconds=60,
        reconcile_minutes=10,
    )
    gate = serve.build(gs, cfg, session=object())
    try:
        for secret in ("whsec-abcdef123456", "whprev-abcdef123456", "bedrock-key-0123456789"):
            assert worker._redact(f"leaked {secret} here") == "leaked [redacted] here"
    finally:
        gate.store.close()
