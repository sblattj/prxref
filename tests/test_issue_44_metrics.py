"""Acceptance tests for issue #44: a Prometheus /metrics listener on 127.0.0.1."""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from prxref import metrics
from prxref.triage import Finding

ROOT = Path(__file__).resolve().parent.parent
LINE = re.compile(
    r"^(#.*|[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? -?[0-9.eE+\-]+|[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})? (NaN|\+Inf|-Inf))$"
)


@pytest.fixture(autouse=True)
def _fresh_registry():
    metrics.reset()
    yield
    metrics.reset()


@pytest.fixture(autouse=True)
def _isolated_environ(monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())


@pytest.fixture(autouse=True)
def _no_leaked_log_filters():
    from prxref.gate import serve

    yield
    for handler in logging.getLogger().handlers:
        for f in [f for f in handler.filters if isinstance(f, serve._RedactingFilter)]:
            handler.removeFilter(f)


@pytest.fixture
def listener():
    server = metrics.start_metrics_listener(_free_port())
    assert server is not None
    yield server
    server.shutdown()
    server.server_close()


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _finding(severity: str, drop_reason: str | None = None) -> Finding:
    return Finding(
        file="src/a.py", line=3, severity=severity, title="t", body="b",
        confidence=0.9, drop_reason=drop_reason,
    )


def _result(**over) -> dict:
    result = {
        "verdict": "Request-Changes",
        "findings_active": [_finding("error"), _finding("warning"), _finding("warning")],
        "findings_dropped": [
            _finding("warning", "hedged: \"might\""),
            _finding("warning", "confidence 0.30 below floor 0.50"),
            _finding("warning", "duplicate of existing thread"),
            _finding("warning", "something no table knows: https://example.com/acme/widget"),
        ],
        "elapsed_ms": 2500,
        "input_tokens": 1000,
        "output_tokens": 200,
        "cost_usd": 0.0125,
        "sampling": {"temperature": 0, "seed": None, "models": ["acme/model-a", "acme/model-b"]},
    }
    result.update(over)
    return result


def _get(server, path: str):
    host, port = server.server_address[:2]
    return urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=5)


def test_port_zero_starts_nothing():
    assert metrics.start_metrics_listener(0) is None


def test_listener_binds_loopback_and_serves_exposition(listener):
    assert listener.server_address[0] == "127.0.0.1"
    metrics.observe_result(_result())
    with _get(listener, "/metrics") as resp:
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "text/plain; version=0.0.4; charset=utf-8"
        body = resp.read().decode()
    lines = [x for x in body.splitlines() if x]
    assert lines and all(LINE.match(x) for x in lines), [x for x in lines if not LINE.match(x)]
    assert any(x.startswith("# HELP prxref_reviews_total ") for x in lines)
    assert any(x.startswith("# TYPE prxref_reviews_total counter") for x in lines)


@pytest.mark.parametrize("path", ["/", "/metric", "/metrics/x", "/healthz"])
def test_other_paths_are_404(listener, path):
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(listener, path)
    assert err.value.code == 404


def test_listener_host_is_not_configurable():
    import inspect

    assert list(inspect.signature(metrics.start_metrics_listener).parameters) == ["port"]


def test_observing_a_result_counts_verdict_severity_gate_and_duration():
    metrics.observe_result(_result())
    text = metrics.render()
    assert 'prxref_reviews_total{verdict="Request-Changes"} 1' in text
    assert 'prxref_findings_posted_total{severity="error"} 1' in text
    assert 'prxref_findings_posted_total{severity="warning"} 2' in text
    assert 'prxref_findings_dropped_total{gate="apply_hedge_gate"} 1' in text
    assert 'prxref_findings_dropped_total{gate="apply_quality_gate"} 1' in text
    assert 'prxref_findings_dropped_total{gate="apply_thread_dedup"} 1' in text
    assert 'prxref_findings_dropped_total{gate="other"} 1' in text
    assert "prxref_review_duration_seconds_count 1" in text
    assert "prxref_review_duration_seconds_sum 2.5" in text
    assert 'prxref_review_duration_seconds_bucket{le="+Inf"} 1' in text
    assert "prxref_review_errors_total 0" in text
    assert 'prxref_llm_cost_usd_total{model="acme/model-a,acme/model-b"} 0.0125' in text
    assert 'prxref_llm_tokens_total{direction="input"} 1000' in text


def test_an_error_result_counts_an_error():
    metrics.observe_result(_result(verdict="Error", findings_active=[], findings_dropped=[]))
    text = metrics.render()
    assert "prxref_review_errors_total 1" in text
    assert 'prxref_reviews_total{verdict="Error"} 1' in text


def test_an_unknown_cost_is_counted_not_zeroed():
    metrics.observe_result(_result(cost_usd=None))
    text = metrics.render()
    assert "prxref_llm_cost_unknown_reviews_total 1" in text
    assert "prxref_llm_cost_usd_total{" not in text


def test_a_non_dict_or_unknown_verdict_is_bounded():
    metrics.observe_result(None)
    metrics.observe_result(_result(verdict="<script>"))
    text = metrics.render()
    assert 'verdict="other"' in text
    assert "<script>" not in text


def test_no_label_value_carries_a_url_or_repo_slug():
    metrics.observe_result(_result())
    for line in metrics.render().splitlines():
        for value in re.findall(r'="([^"]*)"', line):
            if "," in value:
                continue
            assert "://" not in value and "example.com" not in value and "acme/widget" not in value, line


def test_cmd_serve_starts_the_listener_when_the_port_is_set(monkeypatch):
    from prxref import cli

    started = []
    monkeypatch.setattr(metrics, "start_metrics_listener", lambda port: started.append(port))
    monkeypatch.setenv("PRXREF_METRICS_PORT", "9464")
    import importlib
    import types

    fake = types.ModuleType("prxref.webhooks")
    fake.serve = lambda port, host, handler: None
    monkeypatch.setitem(importlib.sys.modules, "prxref.webhooks", fake)
    assert cli.main(["serve", "--port", "9090", "--host", "127.0.0.1"]) == 0
    assert started == [9464]


def test_cmd_serve_leaves_the_listener_off_by_default(monkeypatch):
    from prxref import cli

    started = []
    monkeypatch.setattr(metrics, "start_metrics_listener", lambda port: started.append(port))
    monkeypatch.delenv("PRXREF_METRICS_PORT", raising=False)
    import importlib
    import types

    fake = types.ModuleType("prxref.webhooks")
    fake.serve = lambda port, host, handler: None
    monkeypatch.setitem(importlib.sys.modules, "prxref.webhooks", fake)
    assert cli.main(["serve"]) == 0
    assert started in ([], [0])


def test_gate_serve_starts_the_listener_when_the_port_is_set(tmp_path, monkeypatch):
    from prxref.gate import serve

    started = []
    monkeypatch.setattr(metrics, "start_metrics_listener", lambda port: started.append(port))
    monkeypatch.setattr(serve, "settings_from_config", lambda cfg: "settings")
    monkeypatch.setattr(serve, "build", lambda settings, cfg: "gate")
    monkeypatch.setattr(serve, "run_gate", lambda gate, host, port: 0)
    monkeypatch.setattr(serve, "require_jwt", lambda: None)
    env = tmp_path / "gate.env"
    env.write_text("PRXREF_GATE_APP_ID=1\nPRXREF_METRICS_PORT=9465\n", encoding="utf-8")
    env.chmod(0o600)
    assert serve.serve_main(env, "127.0.0.1", 8787) == 0
    assert started == [9465]


def test_a_bad_port_is_a_configuration_error(monkeypatch):
    from prxref.config import load_config
    from prxref.llm import ConfigError

    monkeypatch.setenv("PRXREF_METRICS_PORT", "70000")
    with pytest.raises(ConfigError):
        load_config()


def test_the_doc_names_every_registered_metric_and_the_grafana_json_parses():
    doc = (ROOT / "docs" / "metrics.md").read_text(encoding="utf-8")
    for name in metrics.metric_names():
        assert f"`{name}`" in doc, name
    block = re.search(r"```json\n(.*?)\n```", doc, re.S)
    assert block, "docs/metrics.md has no json block"
    panel = json.loads(block.group(1))
    assert panel["targets"][0]["expr"]
    assert "AWS Lambda" in doc and "127.0.0.1" in doc
