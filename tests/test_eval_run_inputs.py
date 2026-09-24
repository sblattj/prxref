"""``prxref eval run --scoped-rules`` and ``--prompts-dir``: an arm's shared review inputs (#14 x #12 x #11).

``eval run`` hands ``--rules-file``, ``--scoped-rules`` and ``--prompts-dir``
to the review of every case exactly as given, so ``None`` leaves the
environment in force and ``""`` turns it off, as ``review`` takes them. All
three are loaded once before the first case, so an unusable one exits 2
naming the flag (or its variable) instead of failing every case into its
``error.json``. ``run.json`` records the scoped rules of the first reviewed
record right after ``review_rules``, ``score.json``'s ``run`` block copies
them, and ``eval compare`` reads such a ``score.json`` unchanged.

Most tests drive :func:`prxref.evals.eval_run` with a stub review runner
that records its keyword arguments. :class:`TestThroughTheRealReview` runs
``cli.main`` with the real ``_run_review``, a stub LLM and no network.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from prxref import cli, evals
from prxref.llm import ConfigError, InvokeResult
from prxref.prompt_templates import export_prompt_templates

DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,4 @@\n"
    " def add(a, b):\n"
    "-    return a + b\n"
    "+    total = a + b\n"
    "+    print(total)\n"
    "+    return total\n"
)
SAMPLING = {"temperature": None, "seed": 7, "models": ["model-x"]}
SCOPED_FILE = '---\napplies_to: ["**/*.py"]\n---\n- SCOPED-RULE-LINE: prefer early returns.\n'
OTHER_SCOPED_FILE = '---\napplies_to: ["**/*.py"]\n---\n- ENV-SCOPED-RULE-LINE: never used here.\n'
ALWAYS_FILE = "---\nseverity:\n  blocker: error\n---\n- blocker: a network call without a timeout.\n"
CONFLICT_FILE = '---\napplies_to: ["**/*.py"]\nseverity:\n  blocker: warning\n---\n- blocker: no changelog.\n'
SCOPED_RECORD = {
    "entries": ["rules/python.md"],
    "files": [{
        "path": "rules/python.md", "sha256": "1" * 64, "chars": 40, "max_chars": 12000,
        "truncated": False, "severity_map": {}, "applies_to": ["**/*.py"],
    }],
    "max_chars": 24000,
    "units": None,
}


def _dataset(tmp_path: Path) -> Path:
    """Write ``cases.json`` (cases ``a`` then ``b``, no labels) beside ``change.patch``."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "change.patch").write_text(DIFF, encoding="utf-8")
    path = data / "cases.json"
    path.write_text(json.dumps({"version": 1, "cases": [
        {"id": "a", "diff_file": "change.patch", "expected": []},
        {"id": "b", "diff_file": "change.patch", "expected": []},
    ]}), encoding="utf-8")
    return path


def _args(cases: Path, out: Path, *extra: str, label: str = "L"):
    return cli._build_parser().parse_args(
        ["eval", "run", "--cases", str(cases), f"--label={label}", "--out", str(out), *extra]
    )


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _prompts_dir(root: Path) -> str:
    """A prompts directory holding the packaged templates, which loads without a warning."""
    export_prompt_templates(root)
    return str(root)


def _result(verdict: str = "Approved", **extra: Any) -> dict:
    return {
        "verdict": verdict, "findings_active": [], "findings_dropped": [],
        "chunk_count": 1, "chunks_reviewed": 1, "chunks_failed": 0,
        "sampling": SAMPLING, "review_rules": None, **extra,
    }


class FakeReview:
    """Stands in for ``cli._run_review``: records every call, answers per case id."""

    def __init__(self, outcomes: dict[str, Any] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        outcome = self.outcomes.get(Path(kwargs["trace_dir"]).parent.name, _result())
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _run(args, review: FakeReview) -> int:
    return evals.eval_run(args, run_review=review, build_record=cli._build_json_result)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return action.choices[name]


class TestFlags:
    def test_both_flags_default_to_none(self, tmp_path):
        args = _args(tmp_path / "c.json", tmp_path / "out")
        assert (args.scoped_rules, args.prompts_dir) == (None, None)

    def test_scoped_rules_repeats_and_prompts_dir_takes_one_directory(self, tmp_path):
        args = _args(tmp_path / "c.json", tmp_path / "out",
                     "--scoped-rules", "a.md", "--scoped-rules", "rules/", "--prompts-dir", "prompts")
        assert (args.scoped_rules, args.prompts_dir) == (["a.md", "rules/"], "prompts")

    def test_an_empty_value_is_kept_so_it_can_turn_the_variable_off(self, tmp_path):
        args = _args(tmp_path / "c.json", tmp_path / "out", "--scoped-rules", "", "--prompts-dir", "")
        assert (args.scoped_rules, args.prompts_dir) == ([""], "")

    def test_the_flags_come_right_after_rules_file_with_review_metavars(self):
        run = _subparser(_subparser(cli._build_parser(), "eval"), "run")
        options = [a.option_strings[0] for a in run._actions if a.option_strings and a.dest != "help"]
        assert options == ["--cases", "--label", "--out", "--rules-file", "--scoped-rules", "--prompts-dir", "--resume"]
        by_flag = {a.option_strings[0]: a for a in run._actions if a.option_strings}
        assert (by_flag["--scoped-rules"].metavar, by_flag["--prompts-dir"].metavar) == ("PATH", "DIR")


class TestPassThrough:
    def test_both_flags_reach_the_review_of_every_case(self, tmp_path):
        first = _write(tmp_path / "rules" / "python.md", SCOPED_FILE)
        second = _write(tmp_path / "more" / "extra.md", SCOPED_FILE)
        prompts = _prompts_dir(tmp_path / "prompts")
        review = FakeReview()

        assert _run(_args(_dataset(tmp_path), tmp_path / "out", "--scoped-rules", first,
                          "--scoped-rules", second, "--prompts-dir", prompts), review) == 0

        assert len(review.calls) == 2
        assert [(c["scoped_rules"], c["prompts_dir"]) for c in review.calls] == [
            ([first, second], prompts), ([first, second], prompts),
        ]

    def test_an_empty_value_is_passed_through_to_turn_the_variable_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PRXREF_SCOPED_RULES", str(tmp_path / "missing.md"))
        monkeypatch.setenv("PRXREF_PROMPTS_DIR", str(tmp_path / "missing-prompts"))
        review = FakeReview()

        assert _run(_args(_dataset(tmp_path), tmp_path / "out",
                          "--scoped-rules", "", "--prompts-dir", ""), review) == 0

        assert [(c["scoped_rules"], c["prompts_dir"]) for c in review.calls] == [([""], ""), ([""], "")]

    def test_unset_flags_pass_none_so_the_environment_decides(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PRXREF_SCOPED_RULES", _write(tmp_path / "env.md", SCOPED_FILE))
        monkeypatch.setenv("PRXREF_PROMPTS_DIR", _prompts_dir(tmp_path / "env-prompts"))
        review = FakeReview()

        assert _run(_args(_dataset(tmp_path), tmp_path / "out"), review) == 0

        assert [(c["scoped_rules"], c["prompts_dir"]) for c in review.calls] == [(None, None), (None, None)]


class TestUpFrontErrors:
    @pytest.mark.parametrize("flag", ["--scoped-rules", "--prompts-dir", "--rules-file"])
    def test_a_missing_path_exits_2_naming_the_flag_before_any_case_runs(self, tmp_path, flag):
        review = FakeReview()
        with pytest.raises(ConfigError, match=rf"^{flag}: "):
            _run(_args(_dataset(tmp_path), tmp_path / "out", flag, str(tmp_path / "missing")), review)
        assert review.calls == []
        assert not (tmp_path / "out").exists()

    def test_through_the_cli_a_missing_scoped_rules_path_exits_2_and_writes_no_case(self, tmp_path, capsys):
        missing = tmp_path / "missing.md"
        code = cli.main(["eval", "run", "--cases", str(_dataset(tmp_path)), "--label", "L",
                         "--out", str(tmp_path / "out"), "--scoped-rules", str(missing)])
        assert code == 2
        captured = capsys.readouterr()
        assert captured.err.startswith("configuration error: --scoped-rules: ")
        assert str(missing) in captured.err
        assert captured.out == ""
        assert not (tmp_path / "out").exists()

    @pytest.mark.parametrize(
        "variable", ["PRXREF_SCOPED_RULES", "PRXREF_PROMPTS_DIR", "PRXREF_REVIEW_RULES"],
    )
    def test_an_unusable_variable_exits_2_naming_it_when_no_flag_is_given(self, tmp_path, monkeypatch, variable):
        monkeypatch.setenv(variable, str(tmp_path / "missing"))
        review = FakeReview()
        with pytest.raises(ConfigError, match=rf"^{variable}: "):
            _run(_args(_dataset(tmp_path), tmp_path / "out"), review)
        assert review.calls == []
        assert not (tmp_path / "out").exists()

    def test_a_template_that_fails_its_checks_exits_2_naming_prompts_dir(self, tmp_path):
        prompts = tmp_path / "prompts"
        _write(prompts / "summary.md", "A summary template without its findings placeholder.\n")
        review = FakeReview()
        with pytest.raises(ConfigError, match=r"^--prompts-dir: "):
            _run(_args(_dataset(tmp_path), tmp_path / "out", "--prompts-dir", str(prompts)), review)
        assert review.calls == []

    def test_a_tier_conflict_with_the_rules_file_exits_2_naming_scoped_rules(self, tmp_path):
        always = _write(tmp_path / "RULES.md", ALWAYS_FILE)
        scoped = _write(tmp_path / "rules" / "python.md", CONFLICT_FILE)
        review = FakeReview()
        with pytest.raises(ConfigError, match=r"^--scoped-rules: .*'blocker' is mapped to warning here"):
            _run(_args(_dataset(tmp_path), tmp_path / "out", "--rules-file", always,
                       "--scoped-rules", scoped), review)
        assert review.calls == []

    def test_a_resumed_run_checks_the_inputs_before_it_skips_anything(self, tmp_path, capsys):
        cases = _dataset(tmp_path)
        _run(_args(cases, tmp_path / "out"), FakeReview())
        before = sorted(p.name for p in (tmp_path / "out" / "L").rglob("*"))
        review = FakeReview()
        with pytest.raises(ConfigError, match=r"^--scoped-rules: "):
            _run(_args(cases, tmp_path / "out", "--resume", "--scoped-rules", str(tmp_path / "gone.md")), review)
        assert review.calls == []
        assert sorted(p.name for p in (tmp_path / "out" / "L").rglob("*")) == before


class TestRunJson:
    def test_scoped_rules_follows_review_rules_and_is_null_when_off(self, tmp_path):
        _run(_args(_dataset(tmp_path), tmp_path / "out"), FakeReview())

        run = _read(tmp_path / "out" / "L" / "run.json")
        keys = list(run)
        assert keys.index("scoped_rules") == keys.index("review_rules") + 1
        assert keys[-1] == "config"
        assert run["scoped_rules"] is None

    def test_scoped_rules_is_the_first_reviewed_records_dict_when_on(self, tmp_path):
        review = FakeReview({
            "a": _result("Error", scoped_rules={"entries": ["from-an-error-verdict"]}),
            "b": _result(scoped_rules=SCOPED_RECORD),
        })
        _run(_args(_dataset(tmp_path), tmp_path / "out"), review)

        assert _read(tmp_path / "out/L/cases/b/record.json")["scoped_rules"] == SCOPED_RECORD
        assert _read(tmp_path / "out/L/run.json")["scoped_rules"] == SCOPED_RECORD

    def test_the_run_and_score_versions_do_not_move(self):
        assert (evals.RUN_VERSION, evals.SCORE_VERSION) == (1, 1)


def _score(out: Path, label: str) -> dict[str, Any]:
    args = cli._build_parser().parse_args(["eval", "score", "--label", label, "--out", str(out)])
    assert evals.eval_score(args) == 0
    return _read(out / label / "score.json")


class TestScoreJson:
    def test_score_run_keys_carry_scoped_rules_after_review_rules(self):
        assert evals.SCORE_RUN_KEYS == ("prompts", "sampling", "review_rules", "scoped_rules", "config")

    @pytest.mark.parametrize("scoped", [None, SCOPED_RECORD], ids=["off", "on"])
    def test_the_run_block_copies_scoped_rules_from_run_json(self, tmp_path, scoped, capsys):
        _run(_args(_dataset(tmp_path), tmp_path / "out"), FakeReview({"a": _result(scoped_rules=scoped)}))

        score = _score(tmp_path / "out", "L")

        assert list(score["run"]) == list(evals.SCORE_RUN_KEYS)
        assert score["run"]["scoped_rules"] == scoped
        assert score["run"] == {
            key: _read(tmp_path / "out/L/run.json")[key] for key in evals.SCORE_RUN_KEYS
        }


class TestCompare:
    def test_two_runs_that_differ_only_in_scoped_rules_compare_and_exit_0(self, tmp_path, capsys, caplog):
        out = tmp_path / "out"
        cases = _dataset(tmp_path)
        _run(_args(cases, out, label="A"), FakeReview())
        _run(_args(cases, out, label="B"), FakeReview({
            "a": _result(scoped_rules=SCOPED_RECORD), "b": _result(scoped_rules=SCOPED_RECORD),
        }))
        score_a, score_b = _score(out, "A"), _score(out, "B")
        assert (score_a["run"]["scoped_rules"], score_b["run"]["scoped_rules"]) == (None, SCOPED_RECORD)
        capsys.readouterr()

        with caplog.at_level(logging.WARNING):
            code = cli.main(["eval", "compare", "A", "B", "--out", str(out)])

        assert code == 0
        text = capsys.readouterr().out
        assert text.startswith("# prxref eval compare\n")
        assert "## Metrics" in text
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


class _NoFindingsLLM:
    """Answers every review unit with no findings."""

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        return InvokeResult(text=json.dumps({"findings": [], "escalations": []}), model="fake", backend="fake")


def _no_network(*args, **kwargs):
    raise AssertionError("prxref eval run must not touch the network")


class TestThroughTheRealReview:
    @pytest.fixture
    def stub_llm(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fake")
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: _NoFindingsLLM())
        monkeypatch.setattr("requests.Session.request", _no_network)

    def test_the_flags_replace_the_variables_in_every_case_and_run_json_records_them(
        self, tmp_path, stub_llm, monkeypatch, capsys,
    ):
        monkeypatch.setenv("PRXREF_SCOPED_RULES", _write(tmp_path / "env-rules" / "env.md", OTHER_SCOPED_FILE))
        scoped = _write(tmp_path / "rules" / "python.md", SCOPED_FILE)
        prompts = _prompts_dir(tmp_path / "prompts")

        code = cli.main(["eval", "run", "--cases", str(_dataset(tmp_path)), "--label", "arm",
                         "--out", str(tmp_path / "runs"), "--scoped-rules", scoped, "--prompts-dir", prompts])

        assert code == 0
        run_dir = tmp_path / "runs" / "arm"
        for case_id in ("a", "b"):
            case_dir = run_dir / "cases" / case_id
            assert not (case_dir / "error.json").exists()
            record = _read(case_dir / "record.json")
            assert record["scoped_rules"]["entries"] == [scoped]
            assert [f["path"] for f in record["scoped_rules"]["files"]] == [scoped]
            assert record["prompt_templates"] is not None
            prompts_text = "".join(p.read_text(encoding="utf-8") for p in (case_dir / "trace").glob("*.md"))
            assert "SCOPED-RULE-LINE" in prompts_text
            assert "ENV-SCOPED-RULE-LINE" not in prompts_text
        run = _read(run_dir / "run.json")
        assert run["scoped_rules"] == _read(run_dir / "cases/a/record.json")["scoped_rules"]
        assert run["prompts"]["prompt_templates"] == _read(run_dir / "cases/a/record.json")["prompt_templates"]

    def test_with_no_flag_and_no_variable_the_record_and_run_json_hold_null(self, tmp_path, stub_llm, capsys):
        code = cli.main(["eval", "run", "--cases", str(_dataset(tmp_path)), "--label", "base",
                         "--out", str(tmp_path / "runs")])

        assert code == 0
        run_dir = tmp_path / "runs" / "base"
        assert _read(run_dir / "cases/a/record.json")["scoped_rules"] is None
        assert _read(run_dir / "run.json")["scoped_rules"] is None
