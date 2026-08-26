"""Tests for prxref.cli: review subcommand, serve daemon, --version, and non-blocking exits."""
import sys
import types
from unittest.mock import MagicMock

import pytest

from prxref import __version__
from prxref.cli import main
from prxref.forges.base import PRRef


def _install_fake_module(monkeypatch, fullname: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(fullname)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, fullname, mod)
    return mod


@pytest.fixture
def fake_runtime(monkeypatch):
    """Installs mock orchestrator and llm_backends modules in sys.modules."""
    recorded_orchestrate = []
    recorded_llm = []

    def fake_create_llm_client(cfg):
        recorded_llm.append(cfg)
        return MagicMock(name="LLMClient")

    def fake_orchestrate_review(**kwargs):
        recorded_orchestrate.append(kwargs)

        class F:
            def __init__(self, severity):
                self.severity = severity

        return {
            "verdict": "commented",
            "findings_active": [F("critical"), F("warning"), F("warning")],
            "findings_dropped": [{}, {}, {}],
            "input_tokens": 1200,
            "output_tokens": 350,
        }

    _install_fake_module(
        monkeypatch,
        "prxref.llm_backends",
        create_llm_client=fake_create_llm_client,
    )
    _install_fake_module(
        monkeypatch,
        "prxref.orchestrator",
        orchestrate_review=fake_orchestrate_review,
    )

    return {
        "orchestrate_calls": recorded_orchestrate,
        "llm_calls": recorded_llm,
        "set_orchestrate_side_effect": lambda fn: setattr(
            sys.modules["prxref.orchestrator"], "orchestrate_review", fn
        ),
    }


def test_version_flag(capsys):
    rc = main(["--version"])
    assert rc == 0
    out, _ = capsys.readouterr()
    assert out.strip() == __version__
    assert out.strip() == "0.1.0"


def test_no_subcommand_prints_help_and_exits_2(capsys):
    rc = main([])
    assert rc == 2
    out, err = capsys.readouterr()
    assert "usage:" in err.lower() or "usage:" in out.lower()


class TestReviewSubcommand:
    def test_happy_path_verbose(self, fake_runtime, monkeypatch, capsys):
        test_ref = PRRef(
            forge="bitbucket",
            host="bitbucket.org",
            owner="org",
            repo="repo",
            number=10,
            url="https://bitbucket.org/org/repo/pull-requests/10",
        )
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: test_ref)

        rc = main([
            "review",
            "--pr-url",
            "https://bitbucket.org/org/repo/pull-requests/10",
            "--no-post",
            "--max-chunks",
            "4",
            "-v",
        ])

        assert rc == 0
        calls = fake_runtime["orchestrate_calls"]
        assert len(calls) == 1
        call = calls[0]
        assert call["post"] is False
        assert call["max_chunks"] == 4
        assert call["ref"] is test_ref
        assert call["forge"].name == "bitbucket"

        out, _ = capsys.readouterr()
        assert "verdict: commented" in out
        assert "critical=1 warning=2" in out
        assert "dropped: 3" in out
        assert "tokens: 1200+350" in out
        assert "elapsed:" in out

    def test_default_post_is_true_without_no_post_flag(self, fake_runtime, monkeypatch):
        test_ref = PRRef(
            forge="github",
            host="github.com",
            owner="org",
            repo="repo",
            number=7,
            url="https://github.com/org/repo/pull/7",
        )
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: test_ref)

        rc = main(["review", "--pr-url", "https://github.com/org/repo/pull/7"])
        assert rc == 0
        calls = fake_runtime["orchestrate_calls"]
        assert len(calls) == 1
        assert calls[0]["post"] is True
        assert calls[0]["forge"].name == "github"

    def test_unknown_pr_url_exits_0_with_stderr_hint(self, fake_runtime, monkeypatch, capsys):
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: None)

        rc = main(["review", "--pr-url", "https://example.com/not/a/pr"])
        assert rc == 0
        assert len(fake_runtime["orchestrate_calls"]) == 0

        _, err = capsys.readouterr()
        assert "unrecognized PR URL" in err
        assert "bitbucket.org" in err

    def test_orchestration_exception_exits_0_non_blocking(
        self, fake_runtime, monkeypatch, capsys
    ):
        test_ref = PRRef(
            forge="gitlab",
            host="gitlab.com",
            owner="org",
            repo="repo",
            number=99,
            url="https://gitlab.com/org/repo/-/merge_requests/99",
        )
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: test_ref)

        def _exploding_orchestrate(**kwargs):
            raise RuntimeError("Bedrock throttled: 429 Too Many Requests")

        fake_runtime["set_orchestrate_side_effect"](_exploding_orchestrate)

        rc = main(["review", "--pr-url", "https://gitlab.com/org/repo/-/merge_requests/99"])
        assert rc == 0

        _, err = capsys.readouterr()
        assert "review failed: Bedrock throttled: 429 Too Many Requests" in err

    def test_partial_coverage_prints_without_verbose(self, fake_runtime, monkeypatch, capsys):
        test_ref = PRRef(
            forge="github",
            host="github.com",
            owner="org",
            repo="repo",
            number=42,
            url="https://github.com/org/repo/pull/42",
        )
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: test_ref)

        def _partial_orchestrate(**kwargs):
            return {"verdict": "Approved", "chunks_reviewed": 1, "chunks_failed": 1}

        fake_runtime["set_orchestrate_side_effect"](_partial_orchestrate)

        rc = main(["review", "--pr-url", "https://github.com/org/repo/pull/42", "--no-post"])

        assert rc == 0
        out, _ = capsys.readouterr()
        assert "verdict: Approved" in out
        assert "coverage: 1/2 chunks reviewed" in out
        assert "counts:" not in out


class TestServeSubcommand:
    def test_serve_wiring_and_handler_execution(self, fake_runtime, monkeypatch):
        recorded_serve = []

        def fake_serve(port: int, host: str, handler):
            recorded_serve.append({"port": port, "host": host, "handler": handler})

        _install_fake_module(monkeypatch, "prxref.webhooks", serve=fake_serve)

        rc = main(["serve", "--port", "9090", "--host", "127.0.0.1"])
        assert rc == 0
        assert len(recorded_serve) == 1
        info = recorded_serve[0]
        assert info["port"] == 9090
        assert info["host"] == "127.0.0.1"

        handler = info["handler"]
        test_ref = PRRef(
            forge="github",
            host="github.com",
            owner="org",
            repo="repo",
            number=1,
            url="https://github.com/org/repo/pull/1",
        )
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: test_ref)

        handler("https://github.com/org/repo/pull/1")
        calls = fake_runtime["orchestrate_calls"]
        assert len(calls) == 1
        assert calls[0]["post"] is True
        assert calls[0]["forge"].name == "github"

    def test_serve_handler_swallows_exception_safely(self, fake_runtime, monkeypatch):
        recorded_serve = []

        def fake_serve(port: int, host: str, handler):
            recorded_serve.append(handler)

        _install_fake_module(monkeypatch, "prxref.webhooks", serve=fake_serve)

        main(["serve"])
        handler = recorded_serve[0]

        test_ref = PRRef(
            forge="github",
            host="github.com",
            owner="org",
            repo="repo",
            number=1,
            url="https://github.com/org/repo/pull/1",
        )
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: test_ref)

        def _exploding_orchestrate(**kwargs):
            raise ConnectionError("Bitbucket unreachable")

        fake_runtime["set_orchestrate_side_effect"](_exploding_orchestrate)

        # Must not raise
        handler("https://github.com/org/repo/pull/1")
