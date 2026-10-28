"""``prxref review --eval-pins`` / ``PRXREF_EVAL_PINS=on`` (issue #137).

A team measures a rules pack with ``prxref eval run`` and then reviews in CI.
The eval harness pins three default-on features off (:data:`prxref.evals.EVAL_PINS`);
``--eval-pins`` applies the same mapping, from the same helper, at the same
layer, to a normal review. These tests drive ``cli.main`` with the
orchestrator, the LLM factory and the forge stubbed, so nothing touches the
network, and compare what reaches ``orchestrate_review`` on both paths.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from prxref import cli, config, evals

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue17"
DIFF = FIXTURE / "pr.diff"
PINNED = tuple(evals.EVAL_PINS)
STANDARDS = "docs/standards/*.md"

RESULT = {
    "verdict": "Approved", "findings_active": [], "findings_dropped": [],
    "chunk_count": 1, "chunks_reviewed": 1, "chunks_failed": 0, "elapsed_ms": 5,
    "input_tokens": 10, "output_tokens": 2, "posted": False,
}


@pytest.fixture
def recorder(monkeypatch):
    """Replace ``orchestrate_review``, the LLM factory and the forge; return the recorded kwargs."""
    calls: list[dict] = []

    def _orchestrate(**kwargs):
        calls.append(kwargs)
        return dict(RESULT)

    monkeypatch.setattr("prxref.orchestrator.orchestrate_review", _orchestrate)
    monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: object())
    monkeypatch.setattr(cli, "make_forge", lambda ref: object())
    return calls


@pytest.fixture
def resolved(monkeypatch):
    """Record every config ``_run_review`` resolves, through the real loader."""
    seen: list[dict] = []
    real = cli.load_config_with_sources

    def _load(**kwargs):
        cfg, layers = real(**kwargs)
        seen.append(cfg)
        return cfg, layers

    monkeypatch.setattr(cli, "load_config_with_sources", _load)
    return seen


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A working directory with a ``.prxref.toml`` and an env that turns every pinned feature on."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PRXREF_EVAL_PINS", raising=False)
    monkeypatch.delenv(config.CONFIG_FILE_ENV, raising=False)
    (tmp_path / config.CONFIG_FILE_NAME).write_text(
        'max_chunks = 3\nrouting_probe = "on"\nsuggestions = "on"\n', encoding="utf-8",
    )
    monkeypatch.setenv("PRXREF_CI_WIRING", "on")
    monkeypatch.setenv("PRXREF_ROUTING_PROBE", "on")
    monkeypatch.setenv("PRXREF_CONTEXT_STANDARDS_GLOBS", STANDARDS)
    return tmp_path


def _review(*extra: str) -> int:
    return cli.main(["review", "--diff-file", str(DIFF), "--no-post", *extra])


def _eval_run(root: Path) -> Path:
    out = root / "out"
    argv = ["eval", "run", "--cases", str(FIXTURE / "cases.json"), "--label", "L", "--out", str(out)]
    assert cli.main(argv) == 0
    return out / "L"


def _pinned(call: dict) -> dict:
    return {key: call[key] for key in PINNED}


class TestTheSameMapping:
    def test_review_with_the_flag_hands_orchestrate_review_what_eval_run_does(self, repo, recorder, capsys):
        _eval_run(repo)
        (eval_call,) = recorder
        recorder.clear()

        assert _review("--eval-pins") == 0
        (review_call,) = recorder

        assert _pinned(review_call) == _pinned(eval_call) == evals._pins()

    def test_the_pins_come_from_the_eval_helper_not_a_copy(self, repo, recorder, monkeypatch, capsys):
        swapped = {"ci_wiring": "on", "routing_probe": "off", "context_standards_globs": ["X.md"]}
        monkeypatch.setattr(evals, "EVAL_PINS", swapped)
        monkeypatch.setenv("PRXREF_CI_WIRING", "off")
        _eval_run(repo)
        (eval_call,) = recorder
        recorder.clear()

        assert _review("--eval-pins") == 0
        (review_call,) = recorder

        assert _pinned(review_call) == _pinned(eval_call) == swapped

    def test_without_the_flag_the_environment_decides(self, repo, recorder, capsys):
        assert _review() == 0
        (call,) = recorder
        assert _pinned(call) == {
            "ci_wiring": "on", "routing_probe": "on", "context_standards_globs": [STANDARDS],
        }
        assert _pinned(call) != evals._pins()

    def test_the_variable_is_the_flag(self, repo, recorder, monkeypatch, capsys):
        monkeypatch.setenv("PRXREF_EVAL_PINS", "on")
        assert _review() == 0
        (call,) = recorder
        assert _pinned(call) == evals._pins()

    def test_the_variable_off_pins_nothing(self, repo, recorder, monkeypatch, capsys):
        monkeypatch.setenv("PRXREF_EVAL_PINS", "off")
        assert _review() == 0
        (call,) = recorder
        assert call["ci_wiring"] == "on"


class TestTheResolvedConfig:
    def test_review_resolves_the_run_config_eval_run_records(self, repo, recorder, resolved, capsys):
        run_dir = _eval_run(repo)
        recorded = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["config"]
        resolved.clear()

        assert _review("--eval-pins") == 0
        (cfg,) = resolved

        keys = [k for k in evals.RUN_CONFIG_KEYS if k not in config.ENV_ONLY_KEYS]
        assert {k: cfg[k] for k in keys} == {k: recorded[k] for k in keys}
        assert recorded["max_chunks"] == 3 and recorded["suggestions"] == "on"

    def test_without_the_flag_it_differs(self, repo, recorder, resolved, capsys):
        run_dir = _eval_run(repo)
        recorded = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["config"]
        resolved.clear()

        assert _review() == 0
        (cfg,) = resolved

        assert {k: cfg[k] for k in PINNED} != {k: recorded[k] for k in PINNED}

    def test_the_allowlist_records_the_required_models(self, repo, recorder, monkeypatch, capsys):
        monkeypatch.setenv("PRXREF_LLM_REQUIRE_MODELS", "model-a,model-b")
        run_dir = _eval_run(repo)
        recorded = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["config"]
        assert recorded["llm_require_models"] == ["model-a", "model-b"]


class TestValidation:
    @pytest.mark.parametrize("value", ["yes", "ON", "1"])
    def test_a_bad_value_exits_2_naming_the_variable(self, repo, recorder, monkeypatch, capsys, value):
        monkeypatch.setenv("PRXREF_EVAL_PINS", value)
        assert _review() == 2
        assert recorder == []
        err = capsys.readouterr().err
        assert "configuration error" in err and "PRXREF_EVAL_PINS" in err

    def test_the_key_is_env_only(self, repo, recorder, capsys):
        (repo / config.CONFIG_FILE_NAME).write_text('eval_pins = "on"\n', encoding="utf-8")
        assert _review() == 2
        assert recorder == []
        err = capsys.readouterr().err
        assert "eval_pins" in err and "PRXREF_EVAL_PINS" in err
        assert "eval_pins" in config.ENV_ONLY_KEYS
        assert config._CHOICE_KEYS["eval_pins"] == frozenset({"off", "on"})
        assert config._DEFAULTS["eval_pins"] == "off"


class TestPrecedence:
    def test_a_pin_beats_a_flag_and_names_it(self, repo, recorder, caplog, capsys):
        with caplog.at_level(logging.WARNING, logger="prxref.cli"):
            assert _review("--eval-pins", "--ci-wiring", "on",
                           "--context-standards-globs", "STYLE.md") == 0
        (call,) = recorder
        assert _pinned(call) == evals._pins()
        text = caplog.text
        assert "--ci-wiring 'on' is overridden by the eval pin 'off'" in text
        assert "--context-standards-globs ['STYLE.md'] is overridden by the eval pin []" in text

    def test_a_flag_that_agrees_with_the_pin_is_not_warned_about(self, repo, recorder, caplog, capsys):
        with caplog.at_level(logging.WARNING, logger="prxref.cli"):
            assert _review("--eval-pins", "--ci-wiring", "off") == 0
        assert "overridden by the eval pin" not in caplog.text

    def test_without_the_pins_the_flag_wins(self, repo, recorder, capsys):
        assert _review("--ci-wiring", "off") == 0
        (call,) = recorder
        assert call["ci_wiring"] == "off"
        assert call["routing_probe"] == "on"


class TestTheRecord:
    def test_json_records_the_pins(self, repo, recorder, capsys):
        capsys.readouterr()
        assert _review("--eval-pins", "--format", "json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["eval_pins"] == evals._pins()

    def test_json_records_null_without_them(self, repo, recorder, capsys):
        capsys.readouterr()
        assert _review("--format", "json") == 0
        assert json.loads(capsys.readouterr().out)["eval_pins"] is None

    def test_an_eval_case_record_carries_the_pins(self, repo, recorder, capsys):
        run_dir = _eval_run(repo)
        record = json.loads(next(run_dir.glob("cases/*/record.json")).read_text(encoding="utf-8"))
        assert record["eval_pins"] == evals._pins()

    def test_the_flag_is_on_the_review_parser(self):
        assert cli._build_parser().parse_args(["review", "--eval-pins"]).eval_pins is True
        assert cli._build_parser().parse_args(["review"]).eval_pins is False
