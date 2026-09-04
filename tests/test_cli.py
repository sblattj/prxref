"""Tests for prxref.cli: review subcommand, serve daemon, --version, and non-blocking exits."""
import json
import logging
import os
import subprocess
import sys
import types
from unittest.mock import MagicMock

import pytest

from prxref import __version__, cli
from prxref.cli import main
from prxref.forges.base import PRRef
from prxref.llm import ConfigError
from prxref.triage import Finding


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
        # The hint has to name both halves of every forge. Three of the four
        # adapters serve self-hosted deployments, so listing only the SaaS
        # hostnames would read as a restriction that no longer exists.
        for forge in ("Bitbucket", "GitHub", "GitLab"):
            assert forge in err
        for host in ("bitbucket.org", "github.com", "gitlab.com"):
            assert host in err
        assert "self-hosted" in err
        assert "Bitbucket Data Center" in err
        assert "GitHub Enterprise Server" in err

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

    def test_format_json_on_error_shaped_result_still_emits_one_object(
        self, fake_runtime, monkeypatch, capsys
    ):
        """``_run_review`` can return an incomplete/error-shaped result (as it
        does today for a partial-coverage run — see the sibling text-mode
        test above). ``--format json`` must still emit exactly one JSON
        object, with missing keys defaulting to ``null`` and findings to
        ``[]`` rather than raising (issue 08 FROZEN CLI CONTRACT)."""
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

        rc = main([
            "review", "--pr-url", "https://github.com/org/repo/pull/42",
            "--no-post", "--format", "json",
        ])

        assert rc == 0
        out, _ = capsys.readouterr()
        payload = json.loads(out.strip())
        assert payload["verdict"] == "Approved"
        assert payload["findings"] == []
        assert payload["chunks_reviewed"] == 1
        assert payload["chunks_failed"] == 1
        assert payload["chunk_count"] is None
        assert payload["input_tokens"] is None
        assert payload["output_tokens"] is None
        assert payload["posted"] is None
        assert "sampling" not in payload

    def test_no_post_text_mode_indents_every_line_of_a_multiline_body(
        self, fake_runtime, monkeypatch, capsys
    ):
        """A multi-line finding body must have EVERY line indented two
        spaces, not just the first (issue 08 FROZEN CLI CONTRACT)."""
        test_ref = PRRef(
            forge="github",
            host="github.com",
            owner="org",
            repo="repo",
            number=7,
            url="https://github.com/org/repo/pull/7",
        )
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: test_ref)

        multiline = Finding(
            file="src/foo.py",
            line=42,
            severity="error",
            confidence=0.92,
            title="Off-by-one in loop bound",
            body="first line\nsecond line\nthird line",
            drop_reason=None,
        )

        def _multiline_orchestrate(**kwargs):
            return {
                "verdict": "Request-Changes",
                "findings_active": [multiline],
                "findings_dropped": [],
                "chunk_count": 1,
                "chunks_reviewed": 1,
                "chunks_failed": 0,
                "elapsed_ms": 10,
                "input_tokens": 5,
                "output_tokens": 5,
                "posted": False,
            }

        fake_runtime["set_orchestrate_side_effect"](_multiline_orchestrate)

        rc = main(["review", "--pr-url", "https://github.com/org/repo/pull/7", "--no-post"])
        assert rc == 0
        out, _ = capsys.readouterr()
        assert "  first line\n  second line\n  third line" in out

    def test_format_json_with_verbose_emits_only_one_json_object(
        self, fake_runtime, monkeypatch, capsys
    ):
        """``--format json`` together with ``-v`` must still print exactly
        one JSON object and nothing else on stdout — ``-v`` only expands
        text-mode output, never json (issue 08 FROZEN CLI CONTRACT)."""
        test_ref = PRRef(
            forge="github",
            host="github.com",
            owner="org",
            repo="repo",
            number=7,
            url="https://github.com/org/repo/pull/7",
        )
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: test_ref)

        def _full_orchestrate(**kwargs):
            return {
                "verdict": "commented",
                "findings_active": [],
                "findings_dropped": [],
                "chunk_count": 2,
                "chunks_reviewed": 2,
                "chunks_failed": 0,
                "elapsed_ms": 500,
                "input_tokens": 100,
                "output_tokens": 50,
                "posted": False,
            }

        fake_runtime["set_orchestrate_side_effect"](_full_orchestrate)

        rc = main([
            "review", "--pr-url", "https://github.com/org/repo/pull/7",
            "--no-post", "--format", "json", "-v",
        ])

        assert rc == 0
        out, _ = capsys.readouterr()
        lines = out.strip().splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["verdict"] == "commented"
        assert payload["findings"] == []


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

    def test_chunk_shape_knobs_reach_the_orchestrator(
        self, fake_runtime, monkeypatch
    ):
        monkeypatch.setenv("PRXREF_CHUNK_MAX_FILES", "7")
        monkeypatch.setenv("PRXREF_CHUNK_CONTEXT_LINES", "1")
        assert self._review(monkeypatch) == 0
        call = fake_runtime["orchestrate_calls"][0]
        assert call["max_files_per_chunk"] == 7
        assert call["context_lines"] == 1

    def test_posting_knobs_reach_the_orchestrator(self, fake_runtime, monkeypatch):
        monkeypatch.setenv("PRXREF_POST_MODE", "summary")
        monkeypatch.setenv("PRXREF_POST_VERDICT", "0")
        assert self._review(monkeypatch) == 0
        call = fake_runtime["orchestrate_calls"][0]
        assert call["post_mode"] == "summary"
        assert call["post_verdict"] is False

    def test_defaults_are_todays_hardcoded_values(self, fake_runtime, monkeypatch):
        """Zero behaviour change when nothing is configured."""
        assert self._review(monkeypatch) == 0
        call = fake_runtime["orchestrate_calls"][0]
        assert call["max_chunks"] == 8
        assert call["token_budget"] == 25_000
        assert call["max_files_per_chunk"] == 5
        assert call["context_lines"] == 3
        assert call["max_workers"] == 4
        assert call["max_inline_comments"] == 15
        assert call["confidence_floor"] == 0.6
        assert call["max_errors"] == 10
        assert call["post_mode"] == "summary+inline"
        assert call["post_verdict"] is True

    @pytest.mark.parametrize("name,raw", [
        ("PRXREF_CHUNK_TOKEN_BUDGET", "lots"),
        ("PRXREF_CHUNK_TOKEN_BUDGET", "0"),
        ("PRXREF_CHUNK_TOKEN_BUDGET", "-1"),
        ("PRXREF_MAX_WORKERS", "many"),
        ("PRXREF_MAX_WORKERS", "0"),
        ("PRXREF_MAX_INLINE_COMMENTS", "all"),
        ("PRXREF_MAX_INLINE_COMMENTS", "0"),
        ("PRXREF_POST_MODE", "everything"),
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


class TestDegenerateValuesNeverReachTheOrchestrator:
    """End-to-end exit codes for the numeric keys that predate the range check.

    Asserting on the loader alone would not have caught these: the damage
    happened downstream of ``load_config``, inside the pipeline the CLI is
    supposed to protect. Every case here also asserts the orchestrator was
    never entered.
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

    @pytest.mark.parametrize("name,raw", [
        # Was: uncaught ValueError("min() iterable argument is empty") out of
        # orchestrate_review -> "review failed: ..." on stderr and exit 0, with
        # nothing reviewed and nothing posted. A CI job reads that as green.
        ("PRXREF_MAX_CHUNKS", "0"),
        ("PRXREF_MAX_CHUNKS", "-2"),
        # Was: every finding dropped and a confident "Approved" posted, or (nan)
        # the gate silently disabled. Both fail as success.
        ("PRXREF_CONFIDENCE_FLOOR", "1.5"),
        ("PRXREF_CONFIDENCE_FLOOR", "95"),
        ("PRXREF_CONFIDENCE_FLOOR", "-0.1"),
        ("PRXREF_CONFIDENCE_FLOOR", "nan"),
        ("PRXREF_CONFIDENCE_FLOOR", "inf"),
        # Was: a negative slice silently dropping the lowest-confidence errors.
        ("PRXREF_MAX_ERROR_FINDINGS", "-5"),
    ])
    def test_exits_2_and_names_the_variable(
        self, fake_runtime, monkeypatch, capsys, name, raw
    ):
        monkeypatch.setenv(name, raw)
        assert self._review(monkeypatch) == 2
        _, err = capsys.readouterr()
        assert "configuration error" in err
        assert name in err
        assert len(fake_runtime["orchestrate_calls"]) == 0

    @pytest.mark.parametrize("flag_value", ["0", "-2"])
    def test_degenerate_max_chunks_flag_exits_2(
        self, fake_runtime, monkeypatch, capsys, flag_value
    ):
        """The flag is applied as a ``load_config`` override, so it is checked
        on the same path as the environment variable — and reported as the flag.

        Naming ``PRXREF_MAX_CHUNKS`` here sent an operator who had typed
        ``--max-chunks 0`` hunting through an environment that never set it.
        """
        assert self._review(monkeypatch, ["--max-chunks", flag_value]) == 2
        _, err = capsys.readouterr()
        assert "configuration error" in err
        assert "--max-chunks" in err
        assert "PRXREF_MAX_CHUNKS" not in err
        assert len(fake_runtime["orchestrate_calls"]) == 0

    def test_the_environment_is_still_reported_as_the_environment(
        self, fake_runtime, monkeypatch, capsys
    ):
        """Control for the test above: the flag spelling appears only when the
        flag is what supplied the value."""
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "0")
        assert self._review(monkeypatch) == 2
        _, err = capsys.readouterr()
        assert "PRXREF_MAX_CHUNKS" in err
        assert "--max-chunks" not in err

    @pytest.mark.parametrize("name,raw,key", [
        ("PRXREF_MAX_CHUNKS", "1", "max_chunks"),
        ("PRXREF_CONFIDENCE_FLOOR", "0", "confidence_floor"),
        ("PRXREF_CONFIDENCE_FLOOR", "1", "confidence_floor"),
        ("PRXREF_MAX_ERROR_FINDINGS", "0", "max_error_findings"),
    ])
    def test_legal_edge_values_still_run(
        self, fake_runtime, monkeypatch, name, raw, key
    ):
        """The bounds reject only what is semantically impossible."""
        monkeypatch.setenv(name, raw)
        assert self._review(monkeypatch) == 0
        assert len(fake_runtime["orchestrate_calls"]) == 1


class TestDryRun:
    """PRXREF_DRY_RUN must reach BOTH review paths, daemon included.

    ``_webhook_handler`` hardcoded ``post=True``, so the one component that runs
    unattended against a live repo was the one component that could not be told
    to keep its hands off it.
    """

    REF = PRRef(
        forge="github",
        host="github.com",
        owner="org",
        repo="repo",
        number=7,
        url="https://github.com/org/repo/pull/7",
    )
    URL = "https://github.com/org/repo/pull/7"

    @pytest.fixture(autouse=True)
    def _detect(self, monkeypatch):
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: self.REF)

    def _review(self, argv_extra=()):
        return main(["review", "--pr-url", self.URL, *argv_extra])

    def test_cli_review_posts_by_default(self, fake_runtime):
        """Control: without the variable, today's behaviour is unchanged."""
        assert self._review() == 0
        assert fake_runtime["orchestrate_calls"][0]["post"] is True

    def test_cli_review_stops_posting_when_dry_run_is_set(
        self, fake_runtime, monkeypatch
    ):
        monkeypatch.setenv("PRXREF_DRY_RUN", "1")
        assert self._review() == 0
        assert fake_runtime["orchestrate_calls"][0]["post"] is False

    def test_the_review_still_runs_it_just_does_not_post(
        self, fake_runtime, monkeypatch
    ):
        """A dry run is a full review with the writes removed, not a no-op."""
        monkeypatch.setenv("PRXREF_DRY_RUN", "1")
        assert self._review() == 0
        assert len(fake_runtime["orchestrate_calls"]) == 1

    def test_no_post_still_wins_with_no_variable_set(self, fake_runtime):
        assert self._review(["--no-post"]) == 0
        assert fake_runtime["orchestrate_calls"][0]["post"] is False

    def test_no_post_and_dry_run_together_still_suppress(
        self, fake_runtime, monkeypatch
    ):
        monkeypatch.setenv("PRXREF_DRY_RUN", "1")
        assert self._review(["--no-post"]) == 0
        assert fake_runtime["orchestrate_calls"][0]["post"] is False

    def test_dry_run_wins_over_post_mode(self, fake_runtime, monkeypatch):
        """A dry run removes every write; post_mode only ever selects among
        writes, so the two compose by post_mode losing entirely."""
        monkeypatch.setenv("PRXREF_DRY_RUN", "1")
        monkeypatch.setenv("PRXREF_POST_MODE", "inline")
        assert self._review() == 0
        call = fake_runtime["orchestrate_calls"][0]
        assert call["post"] is False
        assert call["post_mode"] == "inline"

    @pytest.mark.parametrize("raw", ["true", "yes", "0"])
    def test_a_non_literal_one_does_not_silently_disable_posting(
        self, fake_runtime, monkeypatch, raw
    ):
        """Fail-safe direction: an unrecognised value leaves posting ON, so a
        typo cannot quietly turn the reviewer into a no-op nobody notices."""
        monkeypatch.setenv("PRXREF_DRY_RUN", raw)
        assert self._review() == 0
        assert fake_runtime["orchestrate_calls"][0]["post"] is True

    def test_the_webhook_daemon_posts_by_default(self, fake_runtime):
        """Control for the test below."""
        cli._webhook_handler(self.URL)
        assert fake_runtime["orchestrate_calls"][0]["post"] is True

    def test_the_webhook_daemon_honours_dry_run(self, fake_runtime, monkeypatch):
        monkeypatch.setenv("PRXREF_DRY_RUN", "1")
        cli._webhook_handler(self.URL)
        assert len(fake_runtime["orchestrate_calls"]) == 1
        assert fake_runtime["orchestrate_calls"][0]["post"] is False

    def test_a_dry_run_says_so_in_the_log(self, fake_runtime, monkeypatch, caplog):
        """Silence would be indistinguishable from a review that posted.

        Asserted through caplog rather than captured stderr: the handler is
        installed by ``logging.basicConfig`` inside ``main()``, which is a
        no-op once any earlier test has configured the root logger, so a
        stderr assertion would pass or fail on test ORDER rather than on
        behaviour. The stream itself is pinned by ``main()``'s
        ``stream=sys.stderr``.
        """
        monkeypatch.setenv("PRXREF_DRY_RUN", "1")
        with caplog.at_level(logging.INFO, logger="prxref"):
            assert self._review() == 0
        assert "PRXREF_DRY_RUN" in caplog.text


class TestFailOnExitPolicy:
    """PRXREF_FAIL_ON decides when a review outcome moves the exit code.

    The default ``never`` must leave every exit code in this file exactly as
    it was, so the first test pins that. ``error`` and ``any`` opt into a
    failing exit on findings AND on a review that fails to complete — a gate
    that silently passes on a broken run is worse than none. Severity is
    compared the way the orchestrator builds its verdict: exact ``error``,
    never a substring.
    """

    REF = PRRef(
        forge="github",
        host="github.com",
        owner="org",
        repo="repo",
        number=7,
        url="https://github.com/org/repo/pull/7",
    )
    URL = "https://github.com/org/repo/pull/7"

    class _F:
        def __init__(self, severity):
            self.severity = severity

    @pytest.fixture(autouse=True)
    def _detect(self, monkeypatch):
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: self.REF)

    def _review(self, argv_extra=()):
        return main(["review", "--pr-url", self.URL, *argv_extra])

    def _install_result(self, fake_runtime, severities):
        result = {
            "verdict": "Request-Changes" if "error" in severities else "Approved",
            "findings_active": [self._F(s) for s in severities],
            "findings_dropped": [],
        }

        def _orchestrate(**kwargs):
            fake_runtime["orchestrate_calls"].append(kwargs)
            return result

        fake_runtime["set_orchestrate_side_effect"](_orchestrate)
        return result

    def test_the_default_keeps_every_exit_code_where_it_was(
        self, fake_runtime, monkeypatch
    ):
        """Control: an error-severity review still exits 0 with no variable set."""
        self._install_result(fake_runtime, ["error", "warning"])
        assert self._review() == 0

    def test_error_policy_exits_1_on_an_error_finding(
        self, fake_runtime, monkeypatch, capsys
    ):
        monkeypatch.setenv("PRXREF_FAIL_ON", "error")
        self._install_result(fake_runtime, ["error", "outofscope"])
        code = self._review()
        assert code == 1
        out, err = capsys.readouterr()
        assert "verdict: Request-Changes" in out
        assert "PRXREF_FAIL_ON=error" in err
        assert "exiting 1" in err

    def test_error_policy_tolerates_warning_and_note_findings(
        self, fake_runtime, monkeypatch
    ):
        monkeypatch.setenv("PRXREF_FAIL_ON", "error")
        self._install_result(fake_runtime, ["warning", "outofscope"])
        assert self._review() == 0

    def test_error_policy_matches_severity_exactly(
        self, fake_runtime, monkeypatch
    ):
        """"critical" is not "error". The quality gate would have dropped such
        a finding as an invalid severity, so this pins the comparison against
        a substring or prefix accident rather than against real traffic."""
        monkeypatch.setenv("PRXREF_FAIL_ON", "error")
        self._install_result(fake_runtime, ["critical"])
        assert self._review() == 0

    def test_any_policy_exits_1_on_a_warning_only_review(
        self, fake_runtime, monkeypatch
    ):
        monkeypatch.setenv("PRXREF_FAIL_ON", "any")
        self._install_result(fake_runtime, ["warning"])
        assert self._review() == 1

    def test_any_policy_also_covers_error_findings(
        self, fake_runtime, monkeypatch
    ):
        monkeypatch.setenv("PRXREF_FAIL_ON", "any")
        self._install_result(fake_runtime, ["error"])
        assert self._review() == 1

    def test_any_policy_exits_0_when_nothing_was_found(
        self, fake_runtime, monkeypatch
    ):
        monkeypatch.setenv("PRXREF_FAIL_ON", "any")
        self._install_result(fake_runtime, [])
        assert self._review() == 0

    def test_never_policy_ignores_findings_when_set_explicitly(
        self, fake_runtime, monkeypatch
    ):
        monkeypatch.setenv("PRXREF_FAIL_ON", "never")
        self._install_result(fake_runtime, ["error", "error", "warning"])
        assert self._review() == 0

    def test_a_result_without_countable_findings_is_not_gated(
        self, fake_runtime, monkeypatch
    ):
        """A total-LLM-failure run degrades to verdict ``Error`` with no
        findings list; there is nothing countable, so nothing fires."""
        monkeypatch.setenv("PRXREF_FAIL_ON", "error")
        result = {"verdict": "Error", "chunks_failed": 3}
        fake_runtime["set_orchestrate_side_effect"](lambda **kwargs: result)
        assert self._review() == 0

    @pytest.mark.parametrize("policy", ["error", "any"])
    def test_a_failed_review_exits_1_when_gating(
        self, fake_runtime, monkeypatch, capsys, policy
    ):
        """The failure path is exactly the outcome a gating lane must not
        read as green, so the policy is resolved before the run — not from a
        config that a failed run never produced."""
        monkeypatch.setenv("PRXREF_FAIL_ON", policy)

        def _exploding(**kwargs):
            raise RuntimeError("Bedrock throttled: 429 Too Many Requests")

        fake_runtime["set_orchestrate_side_effect"](_exploding)
        assert self._review() == 1
        _, err = capsys.readouterr()
        assert "review failed: Bedrock throttled" in err
        assert f"PRXREF_FAIL_ON={policy}" in err

    def test_a_failed_review_still_exits_0_under_never(
        self, fake_runtime, monkeypatch
    ):
        def _exploding(**kwargs):
            raise ConnectionError("Bitbucket unreachable")

        fake_runtime["set_orchestrate_side_effect"](_exploding)
        assert self._review() == 0

    def test_an_invalid_policy_exits_2_before_anything_runs(
        self, fake_runtime, monkeypatch, capsys
    ):
        monkeypatch.setenv("PRXREF_FAIL_ON", "sometimes")
        assert self._review() == 2
        _, err = capsys.readouterr()
        assert "configuration error" in err
        assert "PRXREF_FAIL_ON" in err
        assert len(fake_runtime["orchestrate_calls"]) == 0

    def test_an_unrecognized_url_still_exits_0_under_gating(
        self, fake_runtime, monkeypatch
    ):
        """Nothing was reviewed, so there is no outcome to gate on."""
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: None)
        monkeypatch.setenv("PRXREF_FAIL_ON", "any")
        assert main(["review", "--pr-url", "https://example.com/not/a/pr"]) == 0
        assert len(fake_runtime["orchestrate_calls"]) == 0

    def test_the_webhook_daemon_is_unaffected_by_the_policy(
        self, fake_runtime, monkeypatch
    ):
        """The daemon has no exit code; the knob must not change what it does."""
        monkeypatch.setenv("PRXREF_FAIL_ON", "error")
        self._install_result(fake_runtime, ["error"])
        cli._webhook_handler(self.URL)
        assert len(fake_runtime["orchestrate_calls"]) == 1


class TestModuleEntryPoint:
    """``python -m prxref.cli`` must actually run main().

    Without the ``__main__`` guard the module imported, executed nothing, and
    exited 0 — a silent success indistinguishable from a review that worked,
    and the exact opposite of the console script's behaviour on the same input.
    """

    def _run(self, *args, env_extra=None):
        env = dict(os.environ)
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, "-m", "prxref.cli", *args],
            capture_output=True, text=True, env=env,
        )

    def test_it_runs_and_prints_the_version(self):
        proc = self._run("--version")
        assert proc.returncode == 0
        assert proc.stdout.strip() == __version__

    def test_a_config_error_exits_2_through_the_module_path(self):
        """This invocation used to exit 0 with no output at all."""
        proc = self._run(
            "review", "--pr-url", "https://github.com/org/repo/pull/42", "--no-post"
        )
        assert proc.returncode == 2
        assert "configuration error" in proc.stderr
        assert "PRXREF_LLM_BASE_URL" in proc.stderr

    def test_no_subcommand_exits_2_with_usage(self):
        proc = self._run()
        assert proc.returncode == 2
        assert "usage:" in proc.stderr.lower()


class TestTraceSubcommand:
    """`trace render` turns a run trace into something a human can look at."""

    def _trace(self, tmp_path):
        f = tmp_path / "run.jsonl"
        f.write_text(
            '{"v":1,"seq":1,"t_ms":0,"node":"run","phase":"start"}\n'
            '{"v":1,"seq":2,"t_ms":9,"node":"forge.get_pr","phase":"start"}\n'
        )
        return f

    def test_it_writes_html_next_to_the_trace_by_default(self, tmp_path, capsys):
        src = self._trace(tmp_path)
        assert main(["trace", "render", str(src)]) == 0
        out = src.with_suffix(".html")
        assert out.is_file()
        assert out.read_text().lstrip().startswith("<!doctype html")
        assert capsys.readouterr().out.strip() == str(out)

    def test_the_output_path_is_overridable(self, tmp_path):
        src = self._trace(tmp_path)
        dest = tmp_path / "elsewhere" / "view.html"
        dest.parent.mkdir()
        assert main(["trace", "render", str(src), "-o", str(dest)]) == 0
        assert dest.is_file()

    def test_a_missing_trace_exits_2_and_names_the_path(self, tmp_path, caplog):
        """A path that is not there is an operator mistake, not a review
        outcome — same class as a malformed env var, so exit 2, not 0."""
        missing = tmp_path / "nope.jsonl"
        with caplog.at_level(logging.ERROR, logger="prxref"):
            assert main(["trace", "render", str(missing)]) == 2
        assert str(missing) in caplog.text
        assert not missing.with_suffix(".html").exists()

    def test_a_missing_output_directory_is_created(self, tmp_path):
        """The convenient half of the write path: -o names a tree, not a hole."""
        src = self._trace(tmp_path)
        dest = tmp_path / "fresh" / "nested" / "view.html"
        assert main(["trace", "render", str(src), "-o", str(dest)]) == 0
        assert dest.is_file()

    def test_an_unwritable_destination_exits_2(self, tmp_path, caplog):
        """A file standing where a directory must go: mkdir cannot rescue this,
        so the OSError branch is the one actually taken."""
        src = self._trace(tmp_path)
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        with caplog.at_level(logging.ERROR, logger="prxref"):
            assert main(["trace", "render", str(src), "-o", str(blocker / "d.html")]) == 2
        assert "could not write" in caplog.text

    def test_trace_without_a_subcommand_exits_2_with_usage(self, capsys):
        assert main(["trace"]) == 2
        assert "usage:" in capsys.readouterr().err.lower()
