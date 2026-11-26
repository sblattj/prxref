"""Issue #40: an env-only PRXREF_NOTIFY_URL posts a one-line review summary."""
from __future__ import annotations

import json
import logging
import sys
import types
from unittest.mock import MagicMock

import pytest
import requests

from prxref import __version__, config
from prxref.cli import main
from prxref.config import ENV_ONLY_KEYS, FILE_KEYS, load_config
from prxref.forges.base import PRRef

SECRET_PATH = "/services/T0FAKE/B0FAKE/s3cr3tT0kenValue"
URL = "https://hooks.example.test" + SECRET_PATH
REF = PRRef(
    forge="github", host="github.com", owner="org", repo="repo", number=12,
    url="https://github.com/org/repo/pull/12",
)


class _F:
    def __init__(self, severity):
        self.severity = severity


def _result(verdict="Request-Changes", severities=("error", "error", "warning")):
    return {
        "verdict": verdict,
        "findings_active": [_F(s) for s in severities],
        "findings_dropped": [],
        "sampling": {"temperature": 0, "seed": None, "models": ["m-one", "m-two"]},
        "posted": True,
    }


class _Resp:
    def __init__(self, status):
        self.status_code = status


@pytest.fixture
def posts(monkeypatch):
    """Record every ``requests.post``; ``behavior`` decides the outcome."""
    calls: list[dict] = []
    state = {"behavior": lambda: _Resp(200)}

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return state["behavior"]()

    monkeypatch.setattr(requests, "post", fake_post)
    return types.SimpleNamespace(calls=calls, state=state)


@pytest.fixture
def runtime(monkeypatch):
    holder = {"result": _result()}

    def fake_orchestrate_review(**kwargs):
        return dict(holder["result"])

    for name, attrs in (
        ("prxref.llm_backends", {"create_llm_client": lambda cfg: MagicMock()}),
        ("prxref.orchestrator", {"orchestrate_review": fake_orchestrate_review}),
    ):
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr("prxref.cli.detect_forge", lambda url: REF)
    return holder


def _review(*extra):
    return main(["review", "--pr-url", REF.url, *extra])


def test_config_key_is_env_only_and_off_by_default():
    assert config._DEFAULTS["notify_url"] == ""
    assert "notify_url" in ENV_ONLY_KEYS
    assert "notify_url" not in FILE_KEYS


def test_env_var_reaches_config(monkeypatch):
    monkeypatch.setenv("PRXREF_NOTIFY_URL", URL)
    assert load_config()["notify_url"] == URL


def test_finished_review_posts_once_with_documented_fields(runtime, posts, monkeypatch):
    monkeypatch.setenv("PRXREF_NOTIFY_URL", URL)
    assert _review() == 0
    assert len(posts.calls) == 1
    call = posts.calls[0]
    assert call["url"] == URL
    assert call["timeout"] == 10
    body = call["json"]
    assert body["text"] == "prxref: Request-Changes on org/repo#12 (2 errors, 1 warning)"
    assert body["pr_url"] == REF.url
    assert body["forge"] == "github"
    assert body["repo"] == "org/repo"
    assert body["verdict"] == "Request-Changes"
    assert body["findings"] == {"error": 2, "warning": 1}
    assert body["models"] == ["m-one", "m-two"]
    assert body["prxref_version"] == __version__
    assert body["timestamp"].endswith("+00:00")


def test_approved_text_line_has_no_findings(runtime, posts, monkeypatch):
    runtime["result"] = _result("Approved", ())
    monkeypatch.setenv("PRXREF_NOTIFY_URL", URL)
    assert _review() == 0
    assert posts.calls[0]["json"]["text"] == "prxref: Approved on org/repo#12 (no findings)"


def test_sent_outcome_is_recorded_without_the_url(runtime, posts, monkeypatch):
    monkeypatch.setenv("PRXREF_NOTIFY_URL", URL)
    from prxref import cli
    result = cli._run_review(REF.url, post=True)
    assert result["notify"] == {"status": "sent", "http_status": 200, "error": None}


@pytest.mark.parametrize("kind", ["http500", "timeout", "connection"])
def test_failure_never_changes_verdict_or_exit_code(runtime, posts, monkeypatch, kind):
    from prxref import cli
    monkeypatch.setenv("PRXREF_FAIL_ON", "error")
    monkeypatch.setenv("PRXREF_NOTIFY_URL", URL)

    def boom():
        if kind == "http500":
            return _Resp(500)
        if kind == "timeout":
            raise requests.exceptions.Timeout(f"timed out calling {URL}")
        raise requests.exceptions.ConnectionError(f"refused by {URL}")

    posts.state["behavior"] = boom
    rc_failed = _review()
    result = cli._run_review(REF.url, post=True)

    monkeypatch.delenv("PRXREF_NOTIFY_URL")
    rc_off = _review()

    assert rc_failed == rc_off == 1
    assert result["verdict"] == "Request-Changes"
    assert result["posted"] is True
    assert result["notify"]["status"] == "failed"
    assert result["notify"]["http_status"] == (500 if kind == "http500" else None)
    assert result["notify"]["error"]


def test_url_never_leaks(runtime, posts, monkeypatch, caplog):
    from prxref import cli
    monkeypatch.setenv("PRXREF_NOTIFY_URL", URL)
    caplog.set_level(logging.DEBUG)
    leaks = []
    for kind in ("ok", "http500", "timeout", "connection"):
        def behavior(kind=kind):
            if kind == "ok":
                return _Resp(200)
            if kind == "http500":
                return _Resp(500)
            if kind == "timeout":
                raise requests.exceptions.Timeout(f"read timed out: {URL}")
            raise requests.exceptions.ConnectionError(
                f"HTTPSConnectionPool(host='hooks.example.test'): max retries {URL}"
            )
        posts.state["behavior"] = behavior
        result = cli._run_review(REF.url, post=True)
        dumped = json.dumps(result, default=str)
        leaks.append((kind, "s3cr3tT0kenValue" in dumped, URL in dumped))
        assert "s3cr3tT0kenValue" not in (result["notify"]["error"] or "")
    assert all(not tok and not full for _, tok, full in leaks), leaks
    assert URL not in caplog.text
    assert "s3cr3tT0kenValue" not in caplog.text
    assert "hooks.example.test" in caplog.text


def test_unset_attempts_no_post(runtime, posts):
    from prxref import cli
    result = cli._run_review(REF.url, post=True)
    assert posts.calls == []
    assert result["notify"] == {"status": "off", "http_status": None, "error": None}


def test_env_dry_run_does_not_notify(runtime, posts, monkeypatch):
    from prxref import cli
    monkeypatch.setenv("PRXREF_NOTIFY_URL", URL)
    monkeypatch.setenv("PRXREF_DRY_RUN", "1")
    result = cli._run_review(REF.url, post=True)
    assert posts.calls == []
    assert result["notify"]["status"] == "off"


def test_no_post_flag_does_not_notify(runtime, posts, monkeypatch):
    monkeypatch.setenv("PRXREF_NOTIFY_URL", URL)
    assert _review("--no-post") == 0
    assert posts.calls == []
