"""Tests for prxref.eval_metrics: the section 7.2 deterministic matcher and the
``eval score`` metrics (issue #14).

Human findings are small frozen-dataclass fakes and AI findings are dict rows
shaped like a run record's ``findings``. Nothing here imports the case loader:
the seam with ``eval_cases`` is exercised by duck typing only, and a grouped
finding (issue #13) is a fake carrying the optional ``locations`` attribute.
"""
from __future__ import annotations

import json
import math
import random
import re
from dataclasses import dataclass

import pytest

from prxref import quality
from prxref.eval_metrics import (
    FULL,
    JUDGE_ERROR,
    LOCATIONS_FIELD,
    MATCH_METHOD,
    MAX_CREDITS_PER_FINDING,
    MISSING_LABEL,
    NONE,
    PARTIAL,
    PARTIAL_CREDIT,
    GradedCase,
    agreement_severity,
    case_row,
    credit_locations,
    match_case,
    must_match_predicate,
    score_cases,
)

FILE = "src/app.py"


@dataclass(frozen=True)
class Human:
    """A stand-in for ``eval_cases.ExpectedFinding``."""

    id: str
    file: str = FILE
    line: int = 10
    severity: str = "error"
    category: str | None = None
    accepted: bool | None = None
    text: str | None = None
    must_match: str | None = None


@dataclass(frozen=True)
class GroupedFinding:
    """A stand-in for an issue #13 grouped finding with extra locations."""

    file: str
    line: int
    title: str
    body: str
    severity: str = "warning"
    drop_reason: str | None = None
    locations: tuple = ()


@dataclass(frozen=True)
class Loc:
    file: str
    line: int


def _ai(line: int = 10, body: str = "", *, file: str = FILE, title: str = "finding", severity: str = "error",
        drop_reason: str | None = None) -> dict:
    """One run-record finding row, shaped like the CLI's JSON finding row."""
    return {
        "file": file,
        "line": line,
        "severity": severity,
        "confidence": 0.9,
        "scope": "unknown",
        "title": title,
        "body": body,
        "drop_reason": drop_reason,
    }


def _grade(human_id: str, grade: str, ai_ref: int | None = None) -> dict:
    return {"human_id": human_id, "grade": grade, "ai_ref": ai_ref}


def _case(case_id: str = "case-1", expected=(), ai=(), grades=(), record=None, **extra) -> GradedCase:
    return GradedCase(
        case_id=case_id, expected=list(expected), ai_findings=list(ai), grades=list(grades), record=record, **extra
    )


def _by_id(grades: list[dict]) -> dict[str, str]:
    return {grade["human_id"]: grade["grade"] for grade in grades}


class TestMustMatchPredicate:
    def test_plain_substring_ignores_case_and_markup(self):
        assert must_match_predicate("clientInfo")("Missing `ClientInfo` in the handshake")

    def test_plain_substring_collapses_whitespace_and_emphasis(self):
        assert must_match_predicate("missing header")("a **missing**\n    header value")

    def test_plain_substring_rejects_other_text(self):
        assert not must_match_predicate("clientInfo")("the client sends no info")

    def test_regex_prefix_is_case_insensitive(self):
        raw = "(log|logger|debug|info).{0,80}token|token.{0,80}(log|logger|debug|info)"
        text = "Logging the session TOKEN leaks it"
        assert re.search(raw, text) is None
        assert must_match_predicate("re:" + raw)(text)

    def test_regex_runs_on_the_raw_text(self):
        assert must_match_predicate(r"re:headers\.get\(")("use `headers.get(name)` instead")

    def test_invalid_regex_raises_value_error(self):
        with pytest.raises(ValueError, match="must_match"):
            must_match_predicate("re:(unclosed")

    def test_pattern_that_normalizes_to_nothing_is_compared_as_written(self):
        test = must_match_predicate("...")
        assert test("then it stops...")
        assert not test("then it stops")


class TestMatchCase:
    def test_tolerance_is_the_quality_default_of_five_lines(self):
        assert quality.DEFAULT_LINE_TOLERANCE == 5
        human = Human("H1", line=10, must_match="race")
        for line, grade in ((5, FULL), (15, FULL), (4, NONE), (16, NONE)):
            assert match_case([human], [_ai(line, "a race here")])[0]["grade"] == grade, line

    def test_full_grade_shape(self):
        grades = match_case([Human("H1", must_match="race")], [_ai(13, "a race here")])
        assert grades == [
            {"human_id": "H1", "grade": FULL, "ai_ref": 0, "ai_line": 13, "method": MATCH_METHOD},
        ]

    def test_none_grade_shape(self):
        grades = match_case([Human("H1", must_match="race")], [])
        assert grades == [
            {"human_id": "H1", "grade": NONE, "ai_ref": None, "ai_line": None, "method": MATCH_METHOD},
        ]

    def test_another_file_never_credits(self):
        grades = match_case([Human("H1", must_match="race")], [_ai(10, "a race here", file="src/other.py")])
        assert grades[0]["grade"] == NONE

    def test_the_predicate_must_hold(self):
        grades = match_case([Human("H1", must_match="race")], [_ai(10, "an off-by-one here")])
        assert grades[0]["grade"] == NONE

    def test_the_predicate_reads_the_title_too(self):
        grades = match_case([Human("H1", must_match="clientInfo")], [_ai(10, "", title="No clientInfo sent")])
        assert grades[0]["grade"] == FULL

    def test_a_dropped_finding_never_credits_and_refs_index_the_whole_list(self):
        ai = [_ai(10, "a race here", drop_reason="below confidence floor"), _ai(11, "a race here")]
        assert match_case([Human("H1", must_match="race")], ai)[0]["ai_ref"] == 1
        assert match_case([Human("H1", must_match="race")], ai[:1])[0]["grade"] == NONE

    def test_a_human_finding_without_must_match_is_left_to_the_judge(self):
        expected = [Human("H1", must_match="race"), Human("H2"), Human("H3", must_match="")]
        assert [grade["human_id"] for grade in match_case(expected, [_ai(10, "a race here")])] == ["H1"]

    def test_a_file_level_human_finding_matches_any_line_of_its_file(self):
        grades = match_case([Human("H1", line=0, must_match="race")], [_ai(300, "a race here")])
        assert grades[0]["grade"] == FULL

    def test_one_ai_finding_credits_at_most_two_human_findings(self):
        assert MAX_CREDITS_PER_FINDING == 2
        expected = [Human(f"H{n}", line=9 + n, must_match="race") for n in (1, 2, 3)]
        grades = match_case(expected, [_ai(11, "a race here")])
        assert _by_id(grades) == {"H1": FULL, "H2": FULL, "H3": NONE}
        assert sum(1 for grade in grades if grade["grade"] == FULL) == MAX_CREDITS_PER_FINDING

    def test_the_cap_reroutes_a_credit_rather_than_stranding_it(self):
        expected = [Human(f"H{n}", line=9 + n, must_match="race") for n in (1, 2, 3)]
        ai = [_ai(10, "a race here", title="A"), _ai(5, "a race here", title="B")]
        grades = match_case(expected, ai)
        assert _by_id(grades) == {"H1": FULL, "H2": FULL, "H3": FULL}
        assert [grade["ai_ref"] for grade in grades] == [1, 0, 0]

    def test_a_grouped_finding_is_credited_per_location_not_under_the_cap(self):
        expected = [Human(f"H{n}", line=line, must_match="unchecked") for n, line in enumerate((10, 11, 40, 70), 1)]
        grouped = GroupedFinding(
            FILE, 10, "Unchecked return value", "The return value is unchecked.",
            locations=({"file": FILE, "line": 40}, (FILE, 70)),
        )
        grades = match_case(expected, [grouped])
        assert _by_id(grades) == {"H1": FULL, "H2": FULL, "H3": FULL, "H4": FULL}
        assert [grade["ai_ref"] for grade in grades] == [0, 0, 0, 0]
        assert [grade["ai_line"] for grade in grades] == [10, 10, 40, 70]
        plain = _ai(10, "The return value is unchecked.", title="Unchecked return value")
        assert _by_id(match_case(expected, [plain])) == {"H1": FULL, "H2": FULL, "H3": NONE, "H4": NONE}

    def test_each_location_of_a_group_keeps_its_own_cap(self):
        expected = [Human(f"H{n}", line=39 + n, must_match="unchecked") for n in (1, 2, 3)]
        grouped = GroupedFinding(FILE, 10, "Unchecked", "unchecked", locations=({"line": 40},))
        assert _by_id(match_case(expected, [grouped])) == {"H1": FULL, "H2": FULL, "H3": NONE}

    def test_grouped_locations_accept_several_shapes_and_skip_bad_entries(self):
        finding = {
            "file": "a.py",
            "line": 3,
            LOCATIONS_FIELD: [
                {"line": 9},
                ("b.py", 4),
                Loc("a.py", 12),
                {"file": "a.py", "line": "7"},
                ("a.py", 3),
                {"file": "a.py", "line": True},
                "a.py:5",
            ],
        }
        assert credit_locations(finding) == [("a.py", 3), ("a.py", 9), ("b.py", 4), ("a.py", 12)]
        assert credit_locations({"file": "a.py", "line": 3}) == [("a.py", 3)]
        assert credit_locations({"file": "a.py", "line": 3, LOCATIONS_FIELD: "a.py:9"}) == [("a.py", 3)]

    def test_shared_tokens_break_a_tie_on_line_distance(self):
        human = Human("H1", line=10, text="session token logged in plain text", must_match="re:token")
        near_first = _ai(8, "The token value is never validated.", title="Unvalidated token")
        on_topic = _ai(12, "The session token is logged in plain text.", title="Secret leak")
        wanted = quality._tokens("session token logged in plain text\ntoken", split_compounds=True)
        shared = [
            len(wanted & quality._tokens(f"{row['title']}\n{row['body']}", split_compounds=True))
            for row in (near_first, on_topic)
        ]
        assert shared[1] > shared[0]
        assert match_case([human], [near_first, on_topic])[0]["ai_ref"] == 1

    def test_grades_do_not_depend_on_input_order(self):
        expected = [
            Human("H1", line=10, must_match="re:race|lock"),
            Human("H2", line=11, must_match="race"),
            Human("H3", line=12, must_match="race"),
            Human("H4", line=20, must_match="lock"),
            Human("H5", line=21, must_match="re:lock|deadlock"),
        ]
        ai = [
            _ai(11, "a race on the counter", title="A"),
            _ai(13, "another race", title="B"),
            _ai(20, "lock is never released", title="C"),
            _ai(5, "race in setup", title="D"),
            _ai(22, "deadlock on shutdown", title="E"),
        ]

        def outcome(humans, rows):
            return {
                grade["human_id"]: (
                    grade["grade"],
                    rows[grade["ai_ref"]]["title"] if grade["ai_ref"] is not None else None,
                    grade["ai_line"],
                )
                for grade in match_case(humans, rows)
            }

        baseline = outcome(expected, ai)
        assert sum(1 for grade, _, _ in baseline.values() if grade == FULL) == 5
        rng = random.Random(14)
        for _ in range(20):
            humans, rows = expected[:], ai[:]
            rng.shuffle(humans)
            rng.shuffle(rows)
            assert outcome(humans, rows) == baseline


class TestAgreementSeverity:
    def test_human_minor_reads_as_warning(self):
        assert agreement_severity("minor") == "warning"
        assert agreement_severity(" Minor ") == "warning"

    def test_other_severities_are_lowercased_only(self):
        assert agreement_severity("Error") == "error"
        assert agreement_severity("outofscope") == "outofscope"
        assert agreement_severity(None) == ""


class TestRecall:
    def test_partial_credit_is_half(self):
        assert PARTIAL_CREDIT == 0.5
        case = _case(
            expected=[Human("H1"), Human("H2"), Human("H3")],
            ai=[_ai(10), _ai(11)],
            grades=[_grade("H1", FULL, 0), _grade("H2", PARTIAL, 1), _grade("H3", NONE)],
        )
        recall = score_cases([case])["metrics"]["recall"]
        assert recall == {
            "recall": 0.5, "credit": 1.5, "scored": 3, "full": 1, "partial": 1, "none": 1, "judge_error": 0,
        }

    def test_a_judge_error_is_excluded_not_scored_as_zero(self):
        case = _case(
            expected=[Human("H1", accepted=True), Human("H2", accepted=True)],
            ai=[_ai(10)],
            grades=[_grade("H1", FULL, 0), _grade("H2", JUDGE_ERROR)],
        )
        result = score_cases([case])
        metrics = result["metrics"]
        assert metrics["recall"]["recall"] == 1.0
        assert metrics["recall"]["scored"] == 1
        assert metrics["recall"]["judge_error"] == 1
        assert metrics["recall_by_severity"]["error"]["recall"] == 1.0
        assert metrics["recall_accepted"]["recall"] == 1.0
        assert result["cases"][0]["findings"][1]["credit"] is None
        zeroed = _case(
            expected=case.expected, ai=case.ai_findings, grades=[_grade("H1", FULL, 0), _grade("H2", NONE)]
        )
        assert score_cases([zeroed])["metrics"]["recall"]["recall"] == 0.5

    def test_only_judge_errors_leave_recall_unknown_not_zero(self):
        case = _case(expected=[Human("H1")], grades=[_grade("H1", JUDGE_ERROR)])
        recall = score_cases([case])["metrics"]["recall"]
        assert recall["recall"] is None
        assert recall["scored"] == 0

    def test_the_headline_is_micro_over_human_findings(self):
        one = _case("case-1", [Human("H1")], [_ai(10)], [_grade("H1", FULL, 0)])
        three = _case("case-2", [Human(f"H{n}") for n in (1, 2, 3)], [], [_grade(f"H{n}", NONE) for n in (1, 2, 3)])
        result = score_cases([one, three])
        assert result["metrics"]["recall"]["recall"] == 0.25
        assert [row["recall"] for row in result["cases"]] == [1.0, 0.0]

    def test_breakdowns_by_severity_category_and_accepted(self):
        expected = [
            Human("H1", severity="error", category="security", accepted=True),
            Human("H2", severity="minor", category="style", accepted=False),
            Human("H3", severity="warning", accepted=None),
            Human("H4", severity="error", category="security", accepted=True),
        ]
        grades = [_grade("H1", FULL, 0), _grade("H2", PARTIAL, 0), _grade("H3", NONE), _grade("H4", NONE)]
        metrics = score_cases([_case(expected=expected, ai=[_ai(10)], grades=grades)])["metrics"]
        assert list(metrics["recall_by_severity"]) == ["error", "minor", "warning"]
        assert metrics["recall_by_severity"]["error"]["recall"] == 0.5
        assert metrics["recall_by_severity"]["minor"]["recall"] == 0.5
        assert list(metrics["recall_by_category"]) == [MISSING_LABEL, "security", "style"]
        assert metrics["recall_by_category"]["security"]["credit"] == 1.0
        assert metrics["recall_accepted"]["scored"] == 2
        assert metrics["recall_accepted"]["recall"] == 0.5
        for partition in ("recall_by_severity", "recall_by_category"):
            blocks = metrics[partition].values()
            assert sum(block["scored"] for block in blocks) == metrics["recall"]["scored"], partition
            assert sum(block["credit"] for block in blocks) == metrics["recall"]["credit"], partition


class TestSeverityAgreement:
    def test_human_minor_agrees_with_warning_only_for_agreement(self):
        expected = [Human("H1", severity="minor"), Human("H2", severity="minor"), Human("H3", severity="error")]
        ai = [_ai(10, severity="warning"), _ai(11, severity="outofscope"), _ai(12, severity="error")]
        grades = [_grade("H1", FULL, 0), _grade("H2", FULL, 1), _grade("H3", PARTIAL, 2)]
        result = score_cases([_case(expected=expected, ai=ai, grades=grades)])
        agreement = result["metrics"]["severity_agreement"]
        assert agreement == {
            "compared": 3,
            "agreed": 2,
            "rate": 2 / 3,
            "confusion": {"error": {"error": 1}, "warning": {"outofscope": 1, "warning": 1}},
        }
        assert [entry["severity_agrees"] for entry in result["cases"][0]["findings"]] == [True, False, True]
        assert list(result["metrics"]["recall_by_severity"]) == ["error", "minor"]
        assert result["cases"][0]["findings"][0]["severity"] == "minor"

    def test_uncredited_findings_are_not_compared(self):
        expected = [Human("H1", severity="error"), Human("H2", severity="error")]
        grades = [_grade("H1", NONE), _grade("H2", JUDGE_ERROR)]
        agreement = score_cases([_case(expected=expected, ai=[_ai(10)], grades=grades)])["metrics"][
            "severity_agreement"
        ]
        assert agreement == {"compared": 0, "agreed": 0, "rate": None, "confusion": {}}


class TestUnmatchedAi:
    def test_unmatched_ai_findings_per_pr(self):
        first = _case(
            "case-1",
            [Human("H1")],
            [_ai(10, title="credited"), _ai(20, title="noise"), _ai(30, title="gated", drop_reason="hedge")],
            [_grade("H1", FULL, 0)],
        )
        second = _case("case-2", [Human("H1")], [_ai(10, title="noise")], [_grade("H1", NONE)])
        result = score_cases([first, second])
        assert result["metrics"]["unmatched_ai"] == {"total": 2, "ai_findings": 3, "per_pr": 1.0}
        assert [(row["ai_findings"], row["unmatched_ai"]) for row in result["cases"]] == [(2, 1), (1, 1)]

    def test_a_credited_grouped_finding_counts_once(self):
        grouped = GroupedFinding(FILE, 10, "Unchecked", "unchecked", locations=({"line": 40},))
        expected = [Human("H1", line=10, must_match="unchecked"), Human("H2", line=40, must_match="unchecked")]
        grades = match_case(expected, [grouped, _ai(90, "noise")])
        row = case_row(_case(expected=expected, ai=[grouped, _ai(90, "noise")], grades=grades))
        assert (row["full"], row["ai_findings"], row["unmatched_ai"]) == (2, 2, 1)


class TestRunTotals:
    def test_chunks_failed_and_elapsed_totals_skip_missing_values(self):
        cases = [
            _case("case-1", record={"chunks_failed": 1, "elapsed_ms": 1000}),
            _case("case-2", record={"chunks_failed": 0, "elapsed_ms": 2500}),
            _case("case-3", record=None),
            _case("case-4", record={"chunks_failed": True, "elapsed_ms": -5}),
        ]
        metrics = score_cases(cases)["metrics"]
        assert metrics["chunks_failed"] == {"total": 1, "missing": 2}
        assert metrics["elapsed_ms"] == {"total": 3500, "missing": 2}

    def test_a_none_review_cost_is_never_summed(self):
        priced = _case("case-1", record={"cost_usd": 0.25})
        unknown = _case("case-2", record={"cost_usd": None})
        cost = score_cases([priced, unknown])["metrics"]["review_cost"]
        assert cost == {"total_usd": None, "per_pr_usd": None, "priced": 1, "unpriced": 1, "estimated": 0}
        both = score_cases([priced, _case("case-2", record={"cost_usd": "0.5", "cost_estimated": True})])
        assert both["metrics"]["review_cost"] == {
            "total_usd": 0.75, "per_pr_usd": 0.375, "priced": 2, "unpriced": 0, "estimated": 1,
        }

    def test_unusable_review_costs_count_as_unpriced(self):
        bad = [math.nan, math.inf, -1.0, True, "abc", ""]
        cases = [_case(f"case-{n}", record={"cost_usd": value}) for n, value in enumerate(bad)]
        cases.append(_case("case-none", record=None))
        cost = score_cases(cases)["metrics"]["review_cost"]
        assert (cost["total_usd"], cost["priced"], cost["unpriced"]) == (None, 0, len(bad) + 1)

    def test_judge_cost_defaults_to_a_known_zero(self):
        cases = [_case("case-1"), _case("case-2", judge_cost_usd=0.02, judge_cost_estimated=True)]
        cost = score_cases(cases)["metrics"]["judge_cost"]
        assert cost == {"total_usd": 0.02, "per_pr_usd": 0.01, "priced": 2, "unpriced": 0, "estimated": 1}
        cases.append(_case("case-3", judge_cost_usd=None))
        cost = score_cases(cases)["metrics"]["judge_cost"]
        assert (cost["total_usd"], cost["per_pr_usd"], cost["unpriced"]) == (None, None, 1)


class TestOutputShape:
    ROW_KEYS = [
        "case_id", "verdict", "recall", "credit", "scored", "full", "partial", "none", "judge_error",
        "ai_findings", "unmatched_ai", "severity_compared", "severity_agreed", "chunks_failed", "elapsed_ms",
        "review_cost_usd", "review_cost_estimated", "judge_cost_usd", "judge_cost_estimated", "findings",
    ]
    ENTRY_KEYS = [
        "human_id", "file", "line", "severity", "category", "accepted", "grade", "credit", "ai_ref", "ai_line",
        "ai_severity", "severity_agrees", "method",
    ]
    METRIC_KEYS = [
        "case_count", "recall", "recall_by_severity", "recall_by_category", "recall_accepted", "unmatched_ai",
        "severity_agreement", "chunks_failed", "elapsed_ms", "review_cost", "judge_cost",
    ]

    def _result(self):
        late = _case(
            "case-b",
            [Human("H2", severity="minor", must_match="race"), Human("H1", must_match="lock")],
            [_ai(10, "a race here", severity="warning"), _ai(40, "noise")],
            record={"verdict": "Comment", "chunks_failed": 0, "elapsed_ms": 900, "cost_usd": 0.1},
        )
        late = _case(
            late.case_id, late.expected, late.ai_findings, match_case(late.expected, late.ai_findings), late.record
        )
        early = _case("case-a", [Human("H1")], [], [_grade("H1", JUDGE_ERROR)], judge_cost_usd=None)
        return score_cases([late, early])

    def test_keys_and_ordering(self):
        result = self._result()
        assert list(result) == ["metrics", "cases"]
        assert list(result["metrics"]) == self.METRIC_KEYS
        assert [row["case_id"] for row in result["cases"]] == ["case-a", "case-b"]
        for row in result["cases"]:
            assert list(row) == self.ROW_KEYS
            assert [list(entry) for entry in row["findings"]] == [self.ENTRY_KEYS] * len(row["findings"])
        assert [entry["human_id"] for entry in result["cases"][1]["findings"]] == ["H1", "H2"]

    def test_the_matcher_feeds_the_metrics(self):
        row = self._result()["cases"][1]
        assert (row["verdict"], row["full"], row["none"], row["unmatched_ai"]) == ("Comment", 1, 1, 1)
        credited = row["findings"][1]
        assert (credited["grade"], credited["ai_ref"], credited["ai_line"], credited["method"]) == (
            FULL, 0, 10, MATCH_METHOD,
        )
        assert (credited["ai_severity"], credited["severity_agrees"]) == ("warning", True)

    def test_the_result_survives_strict_json(self):
        result = self._result()
        assert json.loads(json.dumps(result, allow_nan=False)) == result

    def test_no_cases(self):
        metrics = score_cases([])["metrics"]
        assert metrics["case_count"] == 0
        assert metrics["recall"]["recall"] is None
        assert metrics["unmatched_ai"]["per_pr"] is None
        assert metrics["review_cost"]["total_usd"] is None


class TestGradeValidation:
    @pytest.mark.parametrize(
        ("grades", "message"),
        [
            ([_grade("H1", "match", 0)], "'match' for 'H1' is not one of full, partial, none, judge_error"),
            ([], "no grade for expected finding"),
            ([_grade("H1", FULL, 0), _grade("H1", NONE)], "more than one grade"),
            ([_grade("H1", NONE), _grade("H9", NONE)], "unknown expected finding 'H9'"),
            ([_grade("H1", FULL)], "credits ai_ref None"),
            ([_grade("H1", PARTIAL, 5)], "credits ai_ref 5"),
            ([_grade("H1", FULL, True)], "credits ai_ref True"),
            ([_grade("H1", FULL, 1)], "credits dropped AI finding 1"),
        ],
    )
    def test_bad_grades_raise(self, grades, message):
        case = _case(expected=[Human("H1")], ai=[_ai(10), _ai(11, drop_reason="hedge")], grades=grades)
        with pytest.raises(ValueError, match=re.escape(message)):
            score_cases([case])

    def test_duplicate_case_ids_raise(self):
        with pytest.raises(ValueError, match="case ids are not unique"):
            score_cases([_case("case-1"), _case("case-1")])

    def test_duplicate_expected_ids_raise(self):
        case = _case(expected=[Human("H1"), Human("H1")], grades=[_grade("H1", NONE)])
        with pytest.raises(ValueError, match="not unique"):
            case_row(case)
