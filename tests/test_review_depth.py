"""``PRXREF_REVIEW_DEPTH``: the opt-in ``thorough`` worker prompt.

What is pinned:

- ``thorough_worker_text()`` equals the measured variant prompt
  (``tests/fixtures/review_depth/worker_thorough.md``) byte for byte, and a
  ``worker.md`` that loses one of the derivation's anchors raises loudly;
- ``standard`` (explicit or default) sends the prompts recorded before the
  key existed, and records ``review_depth`` in the run record and JSON;
- ``thorough`` changes the chunk worker's system prompt only: its user half
  and the sweep are those of a ``standard`` run;
- a custom ``worker.md`` wins over ``thorough`` and logs one WARNING naming
  ``PRXREF_REVIEW_DEPTH``;
- the config key: default, legal values, a bad value exiting 2 naming the
  variable, ``.prxref.toml`` loading, and the ``--review-depth`` flag.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pytest

from prxref import cli, config, evals, orchestrator
from prxref.cli import main
from prxref.config import load_config
from prxref.forges.base import PRRef
from prxref.llm import ConfigError
from prxref.prompt_templates import (
    CONTEXT_MARKER,
    REVIEW_DEPTHS,
    PromptTemplates,
    TemplateFile,
    derive_thorough_worker,
    packaged_text,
    thorough_worker_text,
)
from prxref.reviewer import load_prompt
from tests.test_cli import _install_fake_module
from tests.test_orchestrator import REF, FakeForge, _added_file_diff
from tests.test_rule_prompt_slot import BASE_GOLDEN, _Recorder

GOLDEN = Path(__file__).parent / "fixtures" / "review_depth" / "worker_thorough.md"
SECTION = "## Reviewer suggestions"
WORKER_OPENING = "You are a senior code reviewer. You review one chunk"
APP_DIFF = _added_file_diff("src/app.py", 20)
_REF_CLI = PRRef(
    forge="github", host="github.com", owner="acme", repo="widget", number=7,
    url="https://github.com/acme/widget/pull/7",
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run(**knobs) -> tuple[dict, _Recorder]:
    llm = _Recorder()
    res = orchestrator.orchestrate_review(FakeForge(diff=APP_DIFF), REF, llm, post=False, **knobs)
    return res, llm


def _split(prompts: list[tuple[str, str]]) -> tuple[tuple[str, str], list[tuple[str, str]]]:
    workers = [p for p in prompts if p[0].startswith(WORKER_OPENING)]
    assert len(workers) == 1, [p[0][:60] for p in prompts]
    return workers[0], sorted(p for p in prompts if not p[0].startswith(WORKER_OPENING))


def _templates(worker_text: str) -> PromptTemplates:
    file = TemplateFile(
        name="worker", path="team-prompts/worker.md", text=worker_text,
        sha256=_sha(worker_text), chars=len(worker_text),
    )
    return PromptTemplates(
        dir="team-prompts", worker=worker_text, systemic=load_prompt("systemic.md"),
        summary=load_prompt("summary.md"), overrides=(file,),
    )


class TestTheDerivedTemplate:
    def test_thorough_equals_the_measured_variant_byte_for_byte(self):
        assert thorough_worker_text() == GOLDEN.read_text(encoding="utf-8")

    def test_standard_is_the_packaged_worker(self):
        assert packaged_text("worker") == load_prompt("worker.md")
        assert SECTION not in packaged_text("worker")

    def test_the_section_is_added_once_above_the_context_marker(self):
        head, marker, _tail = thorough_worker_text().partition(CONTEXT_MARKER)
        assert marker
        assert head.count(SECTION) == 1

    @pytest.mark.parametrize("anchor", [
        "Prefer zero findings over one speculative finding.",
        "- `outofscope` — minor: misleading naming,",
        "## Spec-grounded rules",
        "no style-guide nits that change neither behavior nor risk",
    ])
    def test_a_missing_anchor_raises_naming_it(self, anchor):
        broken = packaged_text("worker").replace(anchor, "")
        with pytest.raises(RuntimeError, match="PRXREF_REVIEW_DEPTH=thorough") as exc:
            derive_thorough_worker(broken, "## Reviewer suggestions\n\nx\n")
        assert "exactly once, found 0" in str(exc.value)

    def test_a_repeated_anchor_raises(self):
        worker = packaged_text("worker")
        doubled = worker + "\n## Spec-grounded rules\n"
        with pytest.raises(RuntimeError, match="found 2"):
            derive_thorough_worker(doubled, "## Reviewer suggestions\n\nx\n")


class TestStandardIsUnchanged:
    @pytest.mark.parametrize("knobs", [{}, {"review_depth": "standard"}])
    def test_a_real_run_sends_the_base_prompts(self, knobs):
        res, llm = _run(**knobs)
        got = {f"orchestrator/{i}": (_sha(s), _sha(u)) for i, (s, u) in enumerate(sorted(llm.prompts))}
        assert got == {k: v for k, v in BASE_GOLDEN.items() if k.startswith("orchestrator/")}
        assert all(SECTION not in s for s, _u in llm.prompts)
        assert res["review_depth"] == "standard"
        assert cli._build_json_result(res)["review_depth"] == "standard"

    def test_an_unknown_depth_is_a_value_error(self):
        with pytest.raises(ValueError, match="review_depth"):
            _run(review_depth="deep")


class TestThorough:
    def test_only_the_worker_system_prompt_changes(self):
        _std_res, std = _run(review_depth="standard")
        res, thr = _run(review_depth="thorough")
        (std_sys, std_user), std_rest = _split(std.prompts)
        (thr_sys, thr_user), thr_rest = _split(thr.prompts)
        assert SECTION not in std_sys
        assert SECTION in thr_sys
        assert thr_sys == GOLDEN.read_text(encoding="utf-8").partition(CONTEXT_MARKER)[0].strip()
        assert thr_user == std_user
        assert thr_rest == std_rest
        assert all(SECTION not in s for s, _u in thr_rest)
        assert res["review_depth"] == "thorough"

    def test_a_custom_worker_wins_and_warns_once(self, caplog):
        custom = load_prompt("worker.md").replace(
            "You are a senior code reviewer.", "You are the team's reviewer.", 1,
        )
        llm = _Recorder()
        with caplog.at_level(logging.WARNING, logger="prxref"):
            res = orchestrator.orchestrate_review(
                FakeForge(diff=APP_DIFF), REF, llm, post=False,
                prompts=_templates(custom), review_depth="thorough",
            )
        warnings = [r.getMessage() for r in caplog.records
                    if r.levelno == logging.WARNING and "PRXREF_REVIEW_DEPTH" in r.getMessage()]
        assert len(warnings) == 1
        assert "custom worker.md" in warnings[0]
        systems = [s for s, _u in llm.prompts]
        assert custom.partition(CONTEXT_MARKER)[0].strip() in systems
        assert all(SECTION not in s for s in systems)
        assert res["review_depth"] == "thorough"

    def test_a_custom_worker_at_standard_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref"):
            orchestrator.orchestrate_review(
                FakeForge(diff=APP_DIFF), REF, _Recorder(), post=False,
                prompts=_templates(load_prompt("worker.md")), review_depth="standard",
            )
        assert not [r for r in caplog.records if "PRXREF_REVIEW_DEPTH" in r.getMessage()]


class TestConfig:
    def test_the_default_is_standard(self, monkeypatch):
        monkeypatch.delenv("PRXREF_REVIEW_DEPTH", raising=False)
        assert config._DEFAULTS["review_depth"] == "standard"
        assert load_config()["review_depth"] == "standard"

    def test_the_vocabulary_is_declared_in_the_choice_table(self):
        assert config._CHOICE_KEYS["review_depth"] == frozenset(REVIEW_DEPTHS)

    @pytest.mark.parametrize("raw", ["standard", "thorough"])
    def test_each_legal_value_loads(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_REVIEW_DEPTH", raw)
        assert load_config()["review_depth"] == raw

    @pytest.mark.parametrize("raw", ["deep", "Thorough", "on", "1"])
    def test_a_value_outside_the_vocabulary_is_a_config_error(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_REVIEW_DEPTH", raw)
        with pytest.raises(ConfigError, match="PRXREF_REVIEW_DEPTH") as exc:
            load_config()
        assert "'standard', 'thorough'" in str(exc.value)

    def test_bogus_exits_2_naming_the_variable(self, monkeypatch, capsys):
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: _REF_CLI)
        monkeypatch.setenv("PRXREF_REVIEW_DEPTH", "deep")
        assert main(["review", "--pr-url", _REF_CLI.url, "--no-post"]) == 2
        _, err = capsys.readouterr()
        assert "configuration error" in err
        assert "PRXREF_REVIEW_DEPTH" in err

    def test_it_is_a_file_key_and_loads_from_prxref_toml(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PRXREF_REVIEW_DEPTH", raising=False)
        assert "review_depth" in config.FILE_KEYS
        path = tmp_path / ".prxref.toml"
        path.write_text('review_depth = "thorough"\n', encoding="utf-8")
        assert load_config(config_file=path)["review_depth"] == "thorough"

    def test_a_bad_file_value_is_a_config_error(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PRXREF_REVIEW_DEPTH", raising=False)
        path = tmp_path / ".prxref.toml"
        path.write_text('review_depth = "deep"\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="review_depth"):
            load_config(config_file=path)

    def test_the_eval_run_config_records_it(self):
        assert "review_depth" in evals.RUN_CONFIG_KEYS


class TestTheCli:
    def _calls(self, monkeypatch) -> list[dict]:
        calls: list[dict] = []

        def fake_orchestrate_review(**kwargs):
            calls.append(kwargs)
            return {"verdict": "commented", "findings_active": [], "findings_dropped": []}

        _install_fake_module(monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: object())
        _install_fake_module(monkeypatch, "prxref.orchestrator", orchestrate_review=fake_orchestrate_review)
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: _REF_CLI)
        return calls

    @pytest.mark.parametrize("env, flag, want", [
        (None, None, "standard"),
        ("thorough", None, "thorough"),
        (None, "thorough", "thorough"),
        ("thorough", "standard", "standard"),
    ])
    def test_the_flag_wins_over_the_variable(self, monkeypatch, env, flag, want):
        calls = self._calls(monkeypatch)
        if env is None:
            monkeypatch.delenv("PRXREF_REVIEW_DEPTH", raising=False)
        else:
            monkeypatch.setenv("PRXREF_REVIEW_DEPTH", env)
        argv = ["review", "--pr-url", _REF_CLI.url, "--no-post"]
        if flag is not None:
            argv += ["--review-depth", flag]
        assert main(argv) == 0
        (call,) = calls
        assert call["review_depth"] == want

    def test_a_bad_flag_value_exits_2(self, monkeypatch, capsys):
        self._calls(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            main(["review", "--pr-url", _REF_CLI.url, "--no-post", "--review-depth", "deep"])
        assert exc.value.code == 2
        assert "--review-depth" in capsys.readouterr().err
