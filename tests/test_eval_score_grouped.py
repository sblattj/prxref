"""``prxref eval score``: the judge tier credits a grouped finding per location (#13 x #14).

A grouped representative's JSON ``locations`` list the other members of its
group. The deterministic tier already credits each location as its own unit
(``eval_metrics.credit_locations``); these tests pin that the judge tier does
the same. Before the judge call each extra location becomes its own row, so
the judge numbers it as its own ``A<k>``; each judge credit is mapped back to
the representative's record index with the credited location's line as
``ai_line``, and the per-finding cap holds per location in both tiers. A
record with no grouped row reaches the judge exactly as written, so its
prompt and cache key do not change.

The run layout, the record and label builders and the stub judge client come
from :mod:`tests.test_eval_score`; nothing reaches a network.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from prxref import evals
from prxref.judge import assign_refs, build_judge_prompt, judge_cache_key, judge_prompt_sha, split_judge_prompt
from tests import test_eval_score as base
from tests.test_eval_score import JUDGE, _case, _finding, _label, _record, _reply, _row, _score, _write_run

judge = base.judge


def _grouped(file: str, line: int, title: str, *locations: tuple[str, int], **extra: Any) -> dict[str, Any]:
    return _row(file, line, title, locations=[{"file": f, "line": n} for f, n in locations], **extra)


def _credit(score: dict, case_id: str, human_id: str) -> tuple[Any, Any, Any]:
    entry = _finding(score, case_id, human_id)
    return entry["grade"], entry["ai_ref"], entry["ai_line"]


def _cache_names(out: Path) -> list[str]:
    return sorted(path.name for path in (out / "L/judge-cache").glob("*.json"))


def _as_written_key(case, record: dict) -> str:
    return judge_cache_key(judge_prompt_sha(), JUDGE, case, assign_refs(record["findings"]))


OTHER_FILE_CASE = _case("x", _label("J1", "src/b.py", 20, "warning", text="The same null check is missing here."))
OTHER_FILE_RECORD = _record([
    _grouped("src/a.py", 10, "Missing null check", ("src/b.py", 20)),
    _row("src/b.py", 60, "Vague name"),
])


class TestTheExpandedRecord:
    def test_each_extra_location_follows_its_row_as_a_copy_at_that_location(self):
        rep, other = OTHER_FILE_RECORD["findings"]

        record, origin = evals._judge_record(OTHER_FILE_RECORD)

        assert record["findings"] == [rep, {**rep, "file": "src/b.py", "line": 20, "locations": None}, other]
        assert origin == [0, 0, 1]
        assert {key: value for key, value in record.items() if key != "findings"} == {
            key: value for key, value in OTHER_FILE_RECORD.items() if key != "findings"
        }
        assert OTHER_FILE_RECORD["findings"] == [rep, other]
        assert rep["locations"] == [{"file": "src/b.py", "line": 20}]

    @pytest.mark.parametrize("extra", [
        {},
        {"locations": None},
        {"locations": []},
        {"locations": [{"file": "src/b.py", "line": 5}]},
    ], ids=["missing", "null", "empty", "own-location-only"])
    def test_a_record_with_nothing_to_expand_is_returned_itself(self, extra):
        record = _record([_row("src/b.py", 5, "Secret in log", **extra), _row("src/b.py", 40, "Other")])

        expanded, origin = evals._judge_record(record)

        assert expanded is record
        assert origin == [0, 1]

    def test_a_dropped_grouped_row_is_never_expanded(self):
        record = _record([
            _grouped("src/b.py", 4, "Gated out", ("src/b.py", 30), drop_reason="confidence"),
            _row("src/b.py", 9, "Kept"),
        ])

        expanded, origin = evals._judge_record(record)

        assert expanded is record
        assert origin == [0, 1]


class TestPerLocationCredit:
    def test_a_label_in_another_file_is_credited_through_the_expanded_ref(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [(OTHER_FILE_CASE, OTHER_FILE_RECORD)])
        judge.answer = lambda user: _reply(("J1", "full", "A2"))

        score, _ = _score(out, judge_model=JUDGE)

        assert _credit(score, "x", "J1") == ("full", 0, 20)
        entry = _finding(score, "x", "J1")
        assert (entry["credit"], entry["ai_severity"], entry["severity_agrees"]) == (1.0, "warning", True)
        assert score["cases"][0]["unmatched_ai"] == 1
        [call] = judge.calls()
        assert '"ref": "A2"' in call["user"] and '"ref": "A3"' in call["user"]
        assert '"ref": "A1"' not in call["user"] and '"src/a.py"' not in call["user"]

    def test_two_locations_and_the_representative_each_credit_one_label(self, tmp_path, judge, caplog):
        out = tmp_path / "out"
        case = _case(
            "g",
            _label("J1", "src/g.py", 40, "warning", text="Race on the counter."),
            _label("J2", "src/g.py", 80, "warning", text="Race on the cache."),
            _label("J3", "src/g.py", 10, "warning", text="Race on the queue."),
        )
        _write_run(out, [(case, _record([_grouped("src/g.py", 10, "Race condition", ("src/g.py", 40),
                                                  ("src/g.py", 80))]))])
        judge.answer = lambda user: _reply(("J1", "full", "A2"), ("J2", "full", "A3"), ("J3", "full", "A1"))

        with caplog.at_level(logging.WARNING):
            score, _ = _score(out, judge_model=JUDGE)

        assert [_credit(score, "g", h) for h in ("J1", "J2", "J3")] == [
            ("full", 0, 40), ("full", 0, 80), ("full", 0, 10),
        ]
        assert score["metrics"]["recall"]["recall"] == 1.0
        assert score["cases"][0]["unmatched_ai"] == 0
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_a_dropped_row_before_a_grouped_row_shifts_nothing(self, tmp_path, judge):
        out = tmp_path / "out"
        case = _case("d", _label("J1", "src/b.py", 30, "error", text="Null dereference."),
                     _label("J2", "src/b.py", 70, "warning", text="Name is vague."))
        _write_run(out, [(case, _record([
            _row("src/b.py", 4, "Gated out", drop_reason="confidence"),
            _grouped("src/a.py", 10, "Null dereference", ("src/b.py", 30), severity="error"),
            _row("src/b.py", 70, "Vague name"),
        ]))])
        judge.answer = lambda user: _reply(("J1", "full", "A2"), ("J2", "partial", "A3"))

        score, _ = _score(out, judge_model=JUDGE)

        assert [_credit(score, "d", h) for h in ("J1", "J2")] == [("full", 1, 30), ("partial", 2, 70)]
        assert _finding(score, "d", "J1")["ai_severity"] == "error"
        assert score["cases"][0]["unmatched_ai"] == 0


class TestTheCapPerLocation:
    def test_a_third_credit_at_one_location_is_demoted_across_the_tiers(self, tmp_path, judge, caplog):
        out = tmp_path / "out"
        case = _case(
            "k",
            _label("M1", "src/g.py", 50, must_match="race"),
            _label("M2", "src/g.py", 52, must_match="race"),
            _label("J1", "src/g.py", 50, text="Race on the counter."),
            _label("J2", "src/g.py", 10, text="Race on the queue."),
        )
        _write_run(out, [(case, _record([_grouped("src/g.py", 10, "Race condition", ("src/g.py", 50))]))])
        judge.answer = lambda user: _reply(("J1", "full", "A2"), ("J2", "full", "A1"))

        with caplog.at_level(logging.WARNING):
            score, _ = _score(out, judge_model=JUDGE)

        assert [_credit(score, "k", h) for h in ("M1", "M2", "J1", "J2")] == [
            ("full", 0, 50), ("full", 0, 50), ("none", None, None), ("full", 0, 10),
        ]
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert "already credits 2 labels" in warnings[0] and "'J1'" in warnings[0]

    def test_the_judge_caps_each_location_at_two_on_its_own(self, tmp_path, judge):
        out = tmp_path / "out"
        case = _case(
            "j",
            _label("J1", "src/g.py", 50, text="Race one."),
            _label("J2", "src/g.py", 51, text="Race two."),
            _label("J3", "src/g.py", 52, text="Race three."),
            _label("J4", "src/g.py", 10, text="Race four."),
        )
        _write_run(out, [(case, _record([_grouped("src/g.py", 10, "Race condition", ("src/g.py", 50))]))])
        judge.answer = lambda user: _reply(
            ("J1", "full", "A2"), ("J2", "full", "A2"), ("J3", "full", "A2"), ("J4", "full", "A1"),
        )

        score, _ = _score(out, judge_model=JUDGE)

        assert [_credit(score, "j", h) for h in ("J1", "J2", "J3", "J4")] == [
            ("full", 0, 50), ("full", 0, 50), ("none", None, None), ("full", 0, 10),
        ]


class TestTheJudgeCacheKey:
    @pytest.mark.parametrize("extra", [{}, {"locations": None}], ids=["missing", "null"])
    def test_an_ungrouped_record_keeps_the_prompt_and_the_cache_key_of_the_record_as_written(
        self, tmp_path, judge, extra
    ):
        out = tmp_path / "out"
        case = _case("u", _label("J1", "src/b.py", 5, "error", text="Token logged in clear."),
                     _label("J2", "src/b.py", 40, "warning", text="Name is vague."))
        record = _record([
            _row("src/b.py", 5, "Secret in log", severity="error", **extra),
            _row("src/a.py", 8, "Elsewhere", **extra),
            _row("src/b.py", 40, "Vague name", **extra),
        ])
        _write_run(out, [(case, record)])
        judge.answer = lambda user: _reply(("J1", "full", "A1"), ("J2", "partial", "A3"))

        score, _ = _score(out, judge_model=JUDGE)

        system, user = split_judge_prompt(build_judge_prompt(case, assign_refs(record["findings"])))
        [call] = judge.calls()
        assert (call["system"], call["user"]) == (system, user)
        assert _cache_names(out) == [f"{_as_written_key(case, record)}.json"]
        assert [_credit(score, "u", h) for h in ("J1", "J2")] == [("full", 0, 5), ("partial", 2, 40)]

    def test_a_grouped_record_is_keyed_on_its_expanded_rows(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [(OTHER_FILE_CASE, OTHER_FILE_RECORD)])
        judge.answer = lambda user: _reply(("J1", "full", "A2"))

        _score(out, judge_model=JUDGE)

        expanded, _ = evals._judge_record(OTHER_FILE_RECORD)
        assert _cache_names(out) == [f"{_as_written_key(OTHER_FILE_CASE, expanded)}.json"]
        assert _as_written_key(OTHER_FILE_CASE, expanded) != _as_written_key(OTHER_FILE_CASE, OTHER_FILE_RECORD)

    def test_a_rescore_of_a_grouped_record_is_served_from_the_cache(self, tmp_path, judge):
        out = tmp_path / "out"
        _write_run(out, [(OTHER_FILE_CASE, OTHER_FILE_RECORD)])
        judge.answer = lambda user: _reply(("J1", "full", "A2"))
        first, _ = _score(out, judge_model=JUDGE)

        second, _ = _score(out, judge_model=JUDGE)

        assert len(judge.calls()) == 1
        assert second["judge"]["cached"] == 1
        assert _credit(second, "x", "J1") == _credit(first, "x", "J1") == ("full", 0, 20)
