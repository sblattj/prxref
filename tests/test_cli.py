"""Tests for prxref.cli: review subcommand, serve daemon, --version, and non-blocking exits."""
import sys
import types
from unittest.mock import MagicMock

import pytest

from prxref import __version__
from prxref.cli import main
from prxref.forges.base import PRRef
from prxref.llm import ConfigError


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


def test_version_matches_packaging_metadata():
    """pyproject.toml and prxref.__version__ declare the version separately, and
    a release is precisely when they drift.

    Pinned to the installed metadata rather than to a literal: a literal has to
    be hand-edited on every bump, and the one that used to live in
    test_version_flag would have passed a release with a stale __version__ as
    long as someone remembered to edit the test too.
    """
    from importlib.metadata import version as installed_version

    assert installed_version("prxref") == __version__


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

    def test_configured_max_tokens_reaches_the_orchestrator(
        self, fake_runtime, monkeypatch
    ):
        test_ref = PRRef(
            forge="github",
            host="github.com",
            owner="org",
            repo="repo",
            number=7,
            url="https://github.com/org/repo/pull/7",
        )
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: test_ref)
        monkeypatch.setenv("PRXREF_LLM_MAX_TOKENS", "8192")

        assert main(["review", "--pr-url", "https://github.com/org/repo/pull/7"]) == 0
        assert fake_runtime["orchestrate_calls"][0]["max_tokens"] == 8192

    def test_default_max_tokens_is_todays_hardcoded_budget(
        self, fake_runtime, monkeypatch
    ):
        test_ref = PRRef(
            forge="github",
            host="github.com",
            owner="org",
            repo="repo",
            number=7,
            url="https://github.com/org/repo/pull/7",
        )
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: test_ref)
        monkeypatch.delenv("PRXREF_LLM_MAX_TOKENS", raising=False)

        assert main(["review", "--pr-url", "https://github.com/org/repo/pull/7"]) == 0
        assert fake_runtime["orchestrate_calls"][0]["max_tokens"] == 4096

    @pytest.mark.parametrize("name,raw", [
        ("PRXREF_LLM_MAX_TOKENS", "lots"),   # malformed int
        ("PRXREF_LLM_MAX_TOKENS", "0"),      # out of range
        ("PRXREF_LLM_TIMEOUT", "soon"),      # malformed float
        ("PRXREF_LLM_TIMEOUT", "0"),         # out of range
        ("PRXREF_MAX_CHUNKS", "lots"),       # pre-existing knob, same contract
        ("PRXREF_CONFIDENCE_FLOOR", "high"),
    ])
    def test_bad_config_value_exits_2_and_names_the_variable(
        self, fake_runtime, monkeypatch, capsys, name, raw
    ):
        """A malformed value is a usage error (exit 2), like a missing one — not
        a review outcome (exit 0). cli.py's module docstring promises this."""
        test_ref = PRRef(
            forge="github",
            host="github.com",
            owner="org",
            repo="repo",
            number=7,
            url="https://github.com/org/repo/pull/7",
        )
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: test_ref)
        monkeypatch.setenv(name, raw)

        rc = main(["review", "--pr-url", "https://github.com/org/repo/pull/7"])

        assert rc == 2
        _, err = capsys.readouterr()
        assert "configuration error" in err
        assert name in err
        assert len(fake_runtime["orchestrate_calls"]) == 0

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

    def test_config_error_exits_2(self, fake_runtime, monkeypatch, capsys):
        test_ref = PRRef(
            forge="github",
            host="github.com",
            owner="org",
            repo="repo",
            number=5,
            url="https://github.com/org/repo/pull/5",
        )
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: test_ref)

        def _unconfigured(**kwargs):
            raise ConfigError("no LLM endpoint configured. Set PRXREF_LLM_BASE_URL to ...")

        fake_runtime["set_orchestrate_side_effect"](_unconfigured)

        rc = main(["review", "--pr-url", "https://github.com/org/repo/pull/5"])
        assert rc == 2

        _, err = capsys.readouterr()
        assert "configuration error: no LLM endpoint configured" in err
        assert "PRXREF_LLM_BASE_URL" in err

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


class TestConfiguredKnobsReachTheOrchestrator:
    """Every configured knob must arrive as an ``orchestrate_review`` kwarg.

    Two of these are regressions for keys that were documented but dead:
    ``PRXREF_MAX_CHUNKS`` (read under an UPPERCASE key that ``load_config``
    never returns) and the quality-gate pair (never passed at all, so the gate
    re-read the environment and any programmatic override was silently lost).
    """

    REF = PRRef(
        forge="github",
        host="github.com",
        owner="org",
        repo="repo",
        number=7,
        url="https://github.com/org/repo/pull/7",
    )

    def _review(self, monkeypatch, argv_extra=()):
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: self.REF)
        return main([
            "review", "--pr-url", "https://github.com/org/repo/pull/7", *argv_extra,
        ])

    def test_max_chunks_env_reaches_the_orchestrator(self, fake_runtime, monkeypatch):
        """Regression: cli read ``cfg.get("MAX_CHUNKS", 8)``; config keys are lowercase."""
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "3")
        assert self._review(monkeypatch) == 0
        assert fake_runtime["orchestrate_calls"][0]["max_chunks"] == 3

    def test_cli_flag_still_beats_the_environment(self, fake_runtime, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "3")
        assert self._review(monkeypatch, ["--max-chunks", "5"]) == 0
        assert fake_runtime["orchestrate_calls"][0]["max_chunks"] == 5

    def test_quality_gate_knobs_reach_the_orchestrator(self, fake_runtime, monkeypatch):
        """Regression: apply_quality_gate() was called with no arguments."""
        monkeypatch.setenv("PRXREF_CONFIDENCE_FLOOR", "0.85")
        monkeypatch.setenv("PRXREF_MAX_ERROR_FINDINGS", "3")
        assert self._review(monkeypatch) == 0
        call = fake_runtime["orchestrate_calls"][0]
        assert call["confidence_floor"] == 0.85
        assert call["max_errors"] == 3

    def test_chunking_and_fanout_knobs_reach_the_orchestrator(
        self, fake_runtime, monkeypatch
    ):
        monkeypatch.setenv("PRXREF_CHUNK_TOKEN_BUDGET", "9000")
        monkeypatch.setenv("PRXREF_MAX_WORKERS", "2")
        monkeypatch.setenv("PRXREF_MAX_INLINE_COMMENTS", "5")
        assert self._review(monkeypatch) == 0
        call = fake_runtime["orchestrate_calls"][0]
        assert call["token_budget"] == 9000
        assert call["max_workers"] == 2
        assert call["max_inline_comments"] == 5

    def test_defaults_are_todays_hardcoded_values(self, fake_runtime, monkeypatch):
        """Zero behaviour change when nothing is configured."""
        assert self._review(monkeypatch) == 0
        call = fake_runtime["orchestrate_calls"][0]
        assert call["max_chunks"] == 8
        assert call["token_budget"] == 25_000
        assert call["max_workers"] == 4
        assert call["max_inline_comments"] == 15
        assert call["confidence_floor"] == 0.6
        assert call["max_errors"] == 10

    @pytest.mark.parametrize("name,raw", [
        ("PRXREF_CHUNK_TOKEN_BUDGET", "lots"),
        ("PRXREF_CHUNK_TOKEN_BUDGET", "0"),
        ("PRXREF_CHUNK_TOKEN_BUDGET", "-1"),
        ("PRXREF_MAX_WORKERS", "many"),
        ("PRXREF_MAX_WORKERS", "0"),
        ("PRXREF_MAX_INLINE_COMMENTS", "all"),
        ("PRXREF_MAX_INLINE_COMMENTS", "0"),
    ])
    def test_bad_value_exits_2_and_names_the_variable(
        self, fake_runtime, monkeypatch, capsys, name, raw
    ):
        monkeypatch.setenv(name, raw)
        assert self._review(monkeypatch) == 2
        _, err = capsys.readouterr()
        assert "configuration error" in err
        assert name in err
        assert len(fake_runtime["orchestrate_calls"]) == 0
