"""``prxref eval score --precision``: verdicts, arithmetic, cache, failures, wiring (#81).

The judge is a stub client returned by a patched
``prxref.llm_backends.create_llm_client``, as in ``test_eval_score.py``, so
nothing reaches a network. A reply is chosen by the prompt: the precision
prompt carries the ``### AI findings to grade`` heading, the label judge's
does not.
"""
from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from prxref import cli, eval_precision, evals
from prxref.eval_cases import EvalCase
from prxref.eval_precision import parse_precision_response, rates
from prxref.judge import JudgeParseError
from tests.test_eval_score import StubJudge, _case, _label, _record, _row, _write_run

JUDGE = "judge-m"
PRECISION_MARK = "### AI findings to grade"

CASE_A = _case("a", _label("H1", "src/a.py", 10, "error", must_match="divide by zero"))
RECORD_A = _record([
    _row("src/a.py", 11, "Divide by zero", severity="error"),
    _row("src/a.py", 40, "Unused import"),
    _row("src/a.py", 55, "Rename variable"),
    _row("src/a.py", 70, "Dropped one", drop_reason="low_confidence"),
])
CASE_B = _case("b", _label("K1", "src/b.py", 5, "error", must_match="leak"))
RECORD_B = _record([
    _row("src/b.py", 5, "Leak of handle"),
    _row("src/b.py", 9, "Wrong claim"),
])


def _verdicts(*rows: tuple[str, str]) -> str:
    return json.dumps({"verdicts": [{"ai_ref": ref, "verdict": verdict, "reason": f"because {verdict}"}
                                    for ref, verdict in rows]})


def _args(out: Path, *extra: str):
    return cli._build_parser().parse_args(["eval", "score", "--label=L", "--out", str(out), *extra])


@pytest.fixture
def judge(monkeypatch):
    """Patch the client factory; ``state.precision(user)`` answers precision prompts."""
    state = SimpleNamespace(built=[], clients=[], precision=lambda user: _verdicts(("A1", "valid"), ("A2", "nit")),
                            label=lambda user: json.dumps({"grades": []}))

    def create(cfg=None, session=None):
        state.built.append(copy.deepcopy(cfg))
        client = StubJudge(cfg["llm_models"][0], lambda user: (
            state.precision(user) if PRECISION_MARK in user else state.label(user)
        ))
        state.clients.append(client)
        return client

    monkeypatch.setattr("prxref.llm_backends.create_llm_client", create)
    state.calls = lambda: [call for client in state.clients for call in client.calls]
    state.precision_calls = lambda: [call for call in state.calls() if PRECISION_MARK in call["user"]]
    return state


def _score(out: Path, *extra: str) -> tuple[dict, str]:
    assert evals.eval_score(_args(out, *extra)) == 0
    run_dir = out / "L"
    return (json.loads((run_dir / "score.json").read_text(encoding="utf-8")),
            (run_dir / "score.md").read_text(encoding="utf-8"))


def _row_for(score: dict, case_id: str) -> dict:
    return next(row for row in score["cases"] if row["case_id"] == case_id)


class TestVerdictParsing:
    @pytest.mark.parametrize("verdict", ["valid", "nit", "invalid", "duplicate", "unverifiable"])
    def test_each_of_the_five_verdicts_parses(self, verdict):
        parsed = parse_precision_response(_verdicts(("A1", verdict)), [4])
        assert [(v.ai_ref, v.verdict, v.reason) for v in parsed] == [(4, verdict, f"because {verdict}")]

    def test_a_verdict_is_trimmed_and_lowercased_and_a_reason_is_optional(self):
        reply = json.dumps({"verdicts": [{"ai_ref": " A1 ", "verdict": " NIT "}]})
        assert [(v.verdict, v.reason) for v in parse_precision_response(reply, [0])] == [("nit", None)]

    def test_the_verdicts_follow_the_unit_order_not_the_reply_order(self):
        parsed = parse_precision_response(_verdicts(("A2", "nit"), ("A1", "valid")), [3, 8])
        assert [(v.ai_ref, v.verdict) for v in parsed] == [(3, "valid"), (8, "nit")]

    @pytest.mark.parametrize("reply", [
        _verdicts(("A1", "great")),
        _verdicts(("A1", "judge_error")),
        _verdicts(("A1", "valid"), ("A3", "valid")),
        _verdicts(("A1", "valid")),
        _verdicts(("A1", "valid"), ("A1", "nit"), ("A2", "nit")),
        json.dumps({"verdicts": [{"ai_ref": "A1"}]}),
        json.dumps({"verdicts": ["A1"]}),
        json.dumps({"grades": []}),
        json.dumps([]),
        "not json",
        "",
    ])
    def test_a_reply_that_is_not_one_verdict_per_listed_finding_is_rejected(self, reply):
        with pytest.raises(JudgeParseError):
            parse_precision_response(reply, [0, 1])


class TestArithmetic:
    COUNTS = {"matched": 2, "valid": 1, "nit": 1, "invalid": 1, "duplicate": 1, "unverifiable": 5, "judge_error": 3}

    def test_strict_and_lenient_exclude_unverifiable_and_judge_error(self):
        assert rates(self.COUNTS) == (3 / 6, 4 / 6)

    def test_a_zero_denominator_is_none(self):
        zero = {key: 0 for key in eval_precision.COUNT_KEYS}
        assert rates({**zero, "unverifiable": 2, "judge_error": 1}) == (None, None)
        assert eval_precision.summarize([zero])["strict"] is None

    def test_matched_only_is_perfect(self):
        assert rates({**{key: 0 for key in eval_precision.COUNT_KEYS}, "matched": 4}) == (1.0, 1.0)

    def test_summarize_sums_the_case_counts(self):
        one = {**{key: 0 for key in eval_precision.COUNT_KEYS}, "matched": 1, "valid": 1, "graded": 1}
        two = {**{key: 0 for key in eval_precision.COUNT_KEYS}, "matched": 1, "invalid": 2, "graded": 2}
        total = eval_precision.summarize([one, two])
        assert (total["matched"], total["valid"], total["invalid"], total["graded"]) == (2, 1, 2, 3)
        assert (total["strict"], total["lenient"]) == (3 / 5, 3 / 5)


class TestScoring:
    def test_the_unmatched_active_findings_are_graded_and_the_rates_follow(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [(CASE_A, RECORD_A)])

        score, _ = _score(out, "--judge-model", JUDGE, "--precision")

        block = score["metrics"]["precision"]
        assert list(block) == ["graded", "matched", "valid", "nit", "invalid", "duplicate", "unverifiable",
                               "judge_error", "strict", "lenient"]
        assert (block["graded"], block["matched"], block["valid"], block["nit"]) == (2, 1, 1, 1)
        assert block["strict"] == 2 / 3 and block["lenient"] == 1.0
        row = _row_for(score, "a")["precision"]
        assert [(v["ai_ref"], v["verdict"], v["reason"]) for v in row["verdicts"]] == [
            (1, "valid", "because valid"), (2, "nit", "because nit"),
        ]
        assert list(score["metrics"])[-1] == "precision"
        assert _row_for(score, "a")["unmatched_ai"] == 2
        assert len(judge.precision_calls()) == 1
        assert judge.precision_calls()[0]["json_mode"] is True

    def test_a_credited_and_a_dropped_finding_are_not_graded(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [(CASE_A, RECORD_A)])

        _score(out, "--judge-model", JUDGE, "--precision")

        user = judge.precision_calls()[0]["user"]
        assert "Unused import" in user and "Rename variable" in user
        assert "Dropped one" not in user
        graded_part = user.split(PRECISION_MARK)[1]
        assert "Divide by zero" not in graded_part
        assert "Divide by zero" in user.split(PRECISION_MARK)[0]

    def test_a_case_with_nothing_unmatched_makes_no_call(self, tmp_path, judge):
        out = tmp_path / "out"
        record = _record([_row("src/a.py", 11, "Divide by zero", severity="error")])
        _write_run(out, [(CASE_A, record)])

        score, _ = _score(out, "--judge-model", JUDGE, "--precision")

        assert judge.calls() == []
        assert score["metrics"]["precision"]["matched"] == 1
        assert score["metrics"]["precision"]["strict"] == 1.0
        assert _row_for(score, "a")["precision"]["verdicts"] == []

    def test_a_failed_case_has_zero_counts(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [(CASE_A, "RuntimeError: boom")])

        score, _ = _score(out, "--judge-model", JUDGE, "--precision")

        assert judge.calls() == []
        assert score["metrics"]["precision"]["graded"] == 0
        assert score["metrics"]["precision"]["strict"] is None

    def test_a_grouped_finding_is_graded_once(self, tmp_path, judge):
        out = tmp_path / "out"
        grouped = _row("src/g.py", 10, "Race condition", locations=[{"file": "src/g.py", "line": 50},
                                                                      {"file": "src/h.py", "line": 3}])
        _write_run(out, [(_case("g", _label("G1", "src/x.py", 1, "error", must_match="zzz")),
                          _record([grouped]))])
        judge.precision = lambda user: _verdicts(("A1", "valid"))

        score, _ = _score(out, "--judge-model", JUDGE, "--precision")

        user = judge.precision_calls()[0]["user"]
        listed = user.split(PRECISION_MARK)[1].split("### Diff")[0]
        assert listed.count('"ai_ref": "A') == 1
        assert listed.count("Race condition") == 2
        assert score["metrics"]["precision"]["graded"] == 1
        assert _row_for(score, "g")["unmatched_ai"] == 1

    def test_a_judge_failure_is_judge_error_and_is_not_counted(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [(CASE_A, RECORD_A), (CASE_B, RECORD_B)])

        def answer(user):
            if "Leak of handle" in user or "Wrong claim" in user:
                raise RuntimeError("provider down")
            return _verdicts(("A1", "invalid"), ("A2", "valid"))

        judge.precision = answer

        score, md = _score(out, "--judge-model", JUDGE, "--precision")

        block = score["metrics"]["precision"]
        assert (block["judge_error"], block["graded"], block["invalid"], block["valid"]) == (1, 2, 1, 1)
        assert block["matched"] == 2
        assert block["strict"] == 3 / 4
        failed = _row_for(score, "b")["precision"]
        assert [v["verdict"] for v in failed["verdicts"]] == ["judge_error"]
        assert "provider down" in failed["verdicts"][0]["reason"]
        assert failed["graded"] == 0 and failed["judge_error"] == 1
        assert not list((out / "L" / "judge-cache").glob("*.json")) or all(
            "provider down" not in path.read_text() for path in (out / "L" / "judge-cache").glob("*.json")
        )
        assert "Judge error (not counted): 1" in md

    def test_an_unparseable_reply_is_a_judge_error_after_the_retries(self, tmp_path, judge, monkeypatch):
        out = tmp_path / "out"
        _write_run(out, [(CASE_A, RECORD_A)])
        monkeypatch.setenv("PRXREF_LLM_PARSE_RETRIES", "2")
        judge.precision = lambda user: _verdicts(("A1", "valid"))

        score, _ = _score(out, "--judge-model", JUDGE, "--precision")

        assert len(judge.precision_calls()) == 3
        assert score["metrics"]["precision"]["judge_error"] == 2
        assert score["judge"]["parse_retries"] == 2
        assert not (out / "L" / "judge-cache").exists() or not list((out / "L" / "judge-cache").glob("*.json"))

    def test_a_cache_hit_makes_no_call(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [(CASE_A, RECORD_A)])
        first, _ = _score(out, "--judge-model", JUDGE, "--precision")
        assert len(judge.precision_calls()) == 1
        before = len(judge.calls())

        second, _ = _score(out, "--judge-model", JUDGE, "--precision")

        assert len(judge.calls()) == before
        assert second["metrics"]["precision"] == first["metrics"]["precision"]
        assert second["judge"]["cached"] == 1 and second["judge"]["llm_calls"] == 0

    def test_the_cache_key_changes_with_the_diff_and_the_model(self, tmp_path, judge):
        diff = tmp_path / "a.patch"
        diff.write_text("diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+y\n")
        case = replace(CASE_A, diff_file=str(diff))
        out = tmp_path / "out"
        _write_run(out, [(case, RECORD_A)])
        _score(out, "--judge-model", JUDGE, "--precision")
        assert len(judge.precision_calls()) == 1

        diff.write_text(diff.read_text() + "+z\n")
        _score(out, "--judge-model", JUDGE, "--precision")
        assert len(judge.precision_calls()) == 2

        _score(out, "--judge-model", "other-m", "--precision")
        assert len(judge.precision_calls()) == 3

    def test_precision_cost_and_calls_fold_into_judge_and_the_case(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [(CASE_A, RECORD_A)])

        score, _ = _score(out, "--judge-model", JUDGE, "--precision")

        assert score["judge"]["llm_calls"] == 1
        assert score["judge"]["cost_usd"] == pytest.approx(0.002)
        assert _row_for(score, "a")["judge_cost_usd"] == pytest.approx(0.002)
        assert score["judge"]["errors"] == []


class TestWithoutTheFlag:
    def test_precision_is_null_and_no_precision_call_is_made(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [(CASE_A, RECORD_A)])

        score, md = _score(out, "--judge-model", JUDGE)

        assert score["metrics"]["precision"] is None
        assert list(score["metrics"])[-1] == "precision"
        assert all(row["precision"] is None for row in score["cases"])
        assert judge.precision_calls() == []
        assert judge.calls() == []
        assert "## Precision\n\nNot graded" in md

    def test_the_flag_without_a_judge_model_exits_2_naming_it_before_any_call(self, tmp_path, judge, capsys):
        out = tmp_path / "out"
        _write_run(out, [(CASE_A, RECORD_A)])

        code = cli.main(["eval", "score", "--label", "L", "--out", str(out), "--precision"])

        assert code == 2
        err = capsys.readouterr().err
        assert err.startswith("configuration error: --precision:")
        assert judge.built == [] and judge.calls() == []
        assert not (out / "L" / "score.json").exists()

    def test_the_flag_builds_the_judge_even_when_every_label_has_must_match(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [(CASE_A, RECORD_A)])

        score, _ = _score(out, "--judge-model", JUDGE, "--precision")

        assert len(judge.built) == 1
        assert score["judge"]["model"] == JUDGE


class TestDiffContext:
    DIFF = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old_a\n+new_a\n"
        "diff --git a/src/other.py b/src/other.py\n--- a/src/other.py\n+++ b/src/other.py\n"
        "@@ -1 +1 @@\n-old_o\n+new_o\n"
    )

    def _user(self, tmp_path, judge, case: EvalCase, *, patch: str | None = None) -> str:
        out = tmp_path / "out"
        run_dir = _write_run(out, [(case, RECORD_A)])
        if patch is not None:
            (run_dir / "cases" / case.id / "diff.patch").write_text(patch)
        _score(out, "--judge-model", JUDGE, "--precision")
        return judge.precision_calls()[0]["user"]

    def test_only_the_hunks_of_the_findings_files_are_shown(self, tmp_path, judge):
        diff = tmp_path / "a.patch"
        diff.write_text(self.DIFF)

        user = self._user(tmp_path, judge, replace(CASE_A, diff_file=str(diff)))

        assert "new_a" in user and "new_o" not in user

    def test_diff_patch_in_the_case_directory_is_the_fallback(self, tmp_path, judge):
        user = self._user(tmp_path, judge, CASE_A, patch=self.DIFF)
        assert "new_a" in user and "new_o" not in user

    def test_the_diff_file_wins_over_diff_patch(self, tmp_path, judge):
        diff = tmp_path / "a.patch"
        diff.write_text(self.DIFF)
        case = replace(CASE_A, diff_file=str(diff))
        user = self._user(tmp_path, judge, case, patch=self.DIFF.replace("new_a", "zzz"))
        assert "new_a" in user and "zzz" not in user

    def test_no_diff_at_all_is_said_so(self, tmp_path, judge):
        user = self._user(tmp_path, judge, CASE_A)
        assert eval_precision.NO_DIFF in user

    def test_a_file_the_diff_does_not_touch_is_said_so(self, tmp_path, judge):
        diff = tmp_path / "a.patch"
        diff.write_text(self.DIFF.replace("src/a.py", "src/zz.py"))
        user = self._user(tmp_path, judge, replace(CASE_A, diff_file=str(diff)))
        assert eval_precision.NO_FILE_DIFF in user

    def test_the_ticket_text_is_given(self, tmp_path, judge):
        ticket = tmp_path / "ticket.md"
        ticket.write_text("TICKET-123 must never divide.")
        user = self._user(tmp_path, judge, replace(CASE_A, context_file=str(ticket)))
        assert "TICKET-123 must never divide." in user

    def test_diff_by_file_reads_a_diff_without_git_headers(self):
        text = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n--- a/y.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-c\n"
        assert list(eval_precision.diff_by_file(text)) == ["x.py", "y.py"]


class TestReports:
    def test_score_md_has_a_precision_section_after_unmatched(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [(CASE_A, RECORD_A)])

        _, md = _score(out, "--judge-model", JUDGE, "--precision")

        assert md.index("## Unmatched AI findings") < md.index("## Precision") < md.index("## Severity agreement")
        assert "Strict precision: 66.7%. Lenient precision: 100.0%." in md

    def test_compare_shows_the_two_rows_and_n_a_when_a_side_lacks_it(self):
        base = {"metrics": {"precision": {"strict": 0.5, "lenient": 0.75, "matched": 1, "valid": 1, "nit": 1,
                                          "invalid": 1, "duplicate": 0}}, "judge": None}
        bare = {"metrics": {"precision": None}, "judge": None}
        better = {"metrics": {"precision": {"strict": 0.75, "lenient": 0.75}}, "judge": None}
        rows: Callable[[dict, dict], list[str]] = lambda a, b: [  # noqa: E731
            line for line in evals._metric_table(a, b) if "precision" in line
        ]

        assert rows(base, better) == [
            "| Strict precision | 50.0% | 75.0% | +25.0 pp |",
            "| Lenient precision | 75.0% | 75.0% | 0.0 pp |",
        ]
        assert rows(base, bare) == [
            "| Strict precision | 50.0% | n/a | unknown |",
            "| Lenient precision | 75.0% | n/a | unknown |",
        ]

    def test_the_parser_has_the_flag(self):
        args = cli._build_parser().parse_args(["eval", "score", "--label", "L", "--precision"])
        assert args.precision is True
        assert cli._build_parser().parse_args(["eval", "score", "--label", "L"]).precision is False
