"""``docs/evals.md``, the ``prxref eval`` reference, pinned to the code it documents (#14 T10).

Every name list here is derived from the code that writes it, never copied
by hand: the ``run.json`` keys from :func:`prxref.evals._run_json`, the
``score.json`` keys from :func:`prxref.evals._score_json` and
:func:`prxref.evals._judge_block`, the metric keys from
:func:`prxref.eval_metrics.score_cases`, the ``case.json`` keys from
:func:`prxref.eval_cases.case_to_json`, the headings and metric rows from
the renderers, and the flags from the parser. A key is looked for as a
backticked code span, so an English word such as "version" or "cases" in
the prose can never stand in for it. The prose is compared with its
whitespace collapsed, because a code span may wrap across a line.

The two examples in the document are the golden outputs the renderer tests
pin, so a change to either renderer that leaves the document behind fails
here.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from prxref import cli, evals
from prxref.eval_cases import HUMAN_SEVERITIES, EvalCase, ExpectedFinding, case_to_json
from prxref.eval_judge import JudgeOutcome
from prxref.eval_metrics import MAX_CREDITS_PER_FINDING, PARTIAL_CREDIT, GradedCase, score_cases
from prxref.judge import JUDGE_PROMPT_VERSION
from prxref.quality import DEFAULT_LINE_TOLERANCE
from tests.test_docs_consistency import allowed_names
from tests.test_eval_compare import GOLDEN as COMPARE_GOLDEN
from tests.test_eval_score import GOLDEN as SCORE_GOLDEN

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC = (REPO_ROOT / "docs" / "evals.md").read_text(encoding="utf-8")
FLAT = " ".join(DOC.split())
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
SHA_PLACEHOLDER = "<sha256 of the packaged judge prompt>"

LABEL = ExpectedFinding("H1", "src/a.py", 10, "error", category="logic", accepted=True,
                        text="Divides by zero.", must_match="divide by zero")
ROW = {"file": "src/a.py", "line": 11, "severity": "error", "title": "Divide by zero", "body": "b.",
       "drop_reason": None}
RECORD = {"verdict": "Request-Changes", "findings": [ROW], "chunks_failed": 0, "elapsed_ms": 1000,
          "cost_usd": 0.01, "cost_estimated": False}
GRADED = GradedCase(
    case_id="c1", expected=(LABEL,), ai_findings=[ROW], record=RECORD,
    grades=[{"human_id": "H1", "grade": "full", "ai_ref": 0, "ai_line": 11, "method": "must_match"}],
)
CASE = EvalCase(id="c1", expected=(LABEL,), diff_file="c1.patch")
FAILED = evals._RunCase(EvalCase(id="c2", expected=(), diff_file="c2.patch"), Path("c2"), None, "RuntimeError: boom")
JUDGE_ERROR = JudgeOutcome(case_id="c1", cache_key="k", grades=None, error="judge call failed: x",
                           cached=False, llm_calls=1, ref_index={})
JUDGE = evals._judge_block(SimpleNamespace(models=["judge-m"], temperature=0.0, seed=1), "judge-m", False,
                           [JUDGE_ERROR], None)
SCORE = evals._score_json("L", {}, [FAILED], score_cases([GRADED]), JUDGE)


def _code(name: str) -> str:
    return f"`{name}`"


def _undocumented(names) -> list[str]:
    return sorted({name for name in names if _code(name) not in FLAT})


def _eval_parser() -> argparse.ArgumentParser:
    parser = cli._build_parser()
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return commands.choices["eval"]


def _eval_actions() -> dict[str, argparse.ArgumentParser]:
    actions = next(a for a in _eval_parser()._actions if isinstance(a, argparse._SubParsersAction))
    return dict(actions.choices)


class TestTheRunDirectoryIsDocumented:
    def test_every_run_json_key_is_named(self, tmp_path):
        args = SimpleNamespace(label="L", cases="cases.json")
        cfg = dict.fromkeys(evals.RUN_CONFIG_KEYS)
        run = evals._run_json(args, [CASE], tmp_path, cfg, created_at="2026-09-24T00:00:00Z")
        names = [*run, *run["prompts"], *run["prompts"]["sha256"], *run["config"]]
        assert list(run["config"]) == list(evals.RUN_CONFIG_KEYS)
        assert _undocumented(names) == []

    def test_every_case_json_key_is_named(self):
        written = case_to_json(CASE)
        assert _undocumented([*written, *written["expected"][0]]) == []

    def test_the_run_json_table_lists_its_keys_in_order(self, tmp_path):
        args = SimpleNamespace(label="L", cases="cases.json")
        run = evals._run_json(args, [CASE], tmp_path, dict.fromkeys(evals.RUN_CONFIG_KEYS), created_at="x")
        rows = [line.split("|")[1].strip() for line in DOC.split("`run.json` holds, in this order:")[1]
                .split("\n\n")[1].splitlines()[2:]]
        assert rows == [_code(key) for key in run]


class TestScoreJsonIsDocumented:
    def test_every_top_level_key_is_named_in_order(self):
        assert list(SCORE) == ["version", "label", "run", "judge", "failed", "metrics", "cases"]
        section = FLAT.split("### `score.json`")[1].split("### The metrics")[0]
        positions = [section.index(f"- {_code(key)}:") for key in SCORE]
        assert positions == sorted(positions)

    def test_every_run_and_judge_key_is_named(self):
        names = [*SCORE["run"], *SCORE["judge"], *SCORE["judge"]["sampling"], *SCORE["judge"]["errors"][0],
                 *SCORE["failed"][0]]
        assert list(SCORE["run"]) == list(evals.SCORE_RUN_KEYS)
        assert _undocumented(names) == []

    def test_every_metric_key_is_named(self):
        metrics = SCORE["metrics"]
        nested = ("recall", "unmatched_ai", "severity_agreement", "chunks_failed", "elapsed_ms", "review_cost",
                  "judge_cost")
        names = [*metrics, *(key for block in nested for key in metrics[block])]
        assert _undocumented(names) == []

    def test_the_metrics_are_documented_in_order(self):
        section = FLAT.split("### The metrics")[1].split("### `score.md`")[0]
        positions = [section.index(f"**{_code(key)}**") for key in SCORE["metrics"]]
        assert positions == sorted(positions)

    def test_every_case_row_and_label_entry_key_is_named(self):
        row = SCORE["cases"][0]
        assert _undocumented([*row, *row["findings"][0]]) == []

    def test_the_stated_constants_are_the_code_s(self):
        assert f"within {DEFAULT_LINE_TOLERANCE} lines" in FLAT
        assert f"at most {MAX_CREDITS_PER_FINDING} labels" in FLAT
        assert f"`partial` (credit {PARTIAL_CREDIT:g})" in FLAT
        assert f"`{evals.JUDGE_METHOD}`" in FLAT


class TestTheRenderedOutputIsDocumented:
    def test_every_score_md_heading_is_named(self):
        markdown = evals._score_markdown(SCORE)
        title = markdown.splitlines()[0].replace(": L", ": <label>")
        headings = [line for line in markdown.splitlines() if line.startswith("## ")]
        assert len(headings) == 11
        assert _undocumented([title, *headings]) == []

    def test_the_no_judge_lines_are_quoted(self):
        no_judge = evals._score_markdown({**SCORE, "judge": None})
        judge_lines = no_judge.split("## Judge\n\n")[1].splitlines()
        cost_line = no_judge.split("## Cost\n\n")[1].splitlines()[1]
        assert _undocumented([*judge_lines, cost_line]) == []

    def test_the_score_md_example_is_the_pinned_rendering(self):
        pinned = SCORE_GOLDEN.format(version=JUDGE_PROMPT_VERSION, sha=SHA_PLACEHOLDER)
        assert f"```markdown\n{pinned}```\n" in DOC

    def test_every_compare_heading_and_table_header_is_named(self):
        side = evals._ScoredRun("A", "a", Path("a"), SCORE)
        text = evals._compare_text(side, side)
        headings = [line for line in text.splitlines() if line.startswith("#")]
        headers = [line for line in text.splitlines() if line.startswith("| Metric |")]
        changed = {**SCORE["cases"][0], "findings": [{**SCORE["cases"][0]["findings"][0], "grade": "none",
                                                      "credit": 0.0}]}
        headers += evals._changed_labels(SCORE, {**SCORE, "cases": [changed]})[:1]
        assert headers == ["| Metric | A | B | Change |", "| Case | Label | Location | A | B |"]
        assert headings == ["# prxref eval compare", "## Metrics", "## Changed labels", "## Only in one run"]
        assert _undocumented([*headings, *headers]) == []

    def test_every_compare_metric_row_is_named(self):
        rows = evals._metric_table(SCORE, SCORE)[2:]
        names = {row.split(" | ")[0].removeprefix("| ") for row in rows}
        names = {re.sub(r"^(Recall, (?:severity|category)) .*$", r"\1", name) for name in names}
        assert {"Recall, severity", "Recall, category", "Cases", "Judge cost"} <= names
        assert _undocumented(names) == []

    def test_the_worked_example_is_the_pinned_comparison(self):
        assert f"```text\n{COMPARE_GOLDEN}```\n" in DOC


class TestTheInputsAreDocumented:
    def test_the_human_severities_are_listed_in_order(self):
        assert ", ".join(_code(severity) for severity in HUMAN_SEVERITIES) in FLAT

    @pytest.mark.parametrize("action", ["run", "score", "compare"])
    def test_every_action_has_a_section(self, action):
        assert f"\n## `prxref eval {action}`\n" in DOC

    @pytest.mark.parametrize(
        "flag",
        sorted({flag for sub in _eval_actions().values() for action in sub._actions
                for flag in action.option_strings if flag not in ("-h", "--help")}),
    )
    def test_every_flag_is_documented(self, flag):
        assert re.search(rf"`{re.escape(flag)}[` ]", DOC), f"docs/evals.md does not name {flag}"

    def test_the_default_out_and_its_gitignore_line_are_the_parser_s(self):
        default = cli._build_parser().parse_args(["eval", "score", "--label", "x"]).out
        assert _code(default) in FLAT
        assert f"```gitignore\n{default.removeprefix('./')}\n```" in DOC

    def test_every_prxref_variable_it_names_is_a_real_one(self):
        named = set(re.findall(r"\bPRXREF_[A-Z0-9_]+\b", DOC))
        assert "PRXREF_REVIEW_RULES" in named
        assert sorted(named - allowed_names()) == []


class TestTheDocumentIsReachable:
    def test_the_readme_section_links_it_between_replay_mode_and_exit_codes(self):
        start = README.index("\n## Evaluating prxref\n")
        end = README.index("\n## ", start + 1)
        assert README.index("\n## Replay Mode (Evaluation)\n") < start < README.index("\n## Exit Codes\n")
        assert "](docs/evals.md)" in README[start:end]

    @pytest.mark.parametrize("path", ["docs/env-vars.md", "docs/prompt-templates.md", "docs/spec-grounded-review.md"])
    def test_the_related_documents_link_it(self, path):
        assert "](evals.md" in (REPO_ROOT / path).read_text(encoding="utf-8")
