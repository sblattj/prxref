"""The per-rule cap ``apply_rule_cap`` and its tally ``rule_cap_counts`` (issue #18).

Also covers the one ``apply_sweep_dedup`` change #18 makes: a chunk finding
the rule cap folded away still adds its key to the sweep dedup, so a sweep
copy that restates it is dropped.
"""
from __future__ import annotations

import itertools
import re

import pytest

from prxref.quality import (
    GROUPED_INTO_PREFIX,
    RULE_CAP_LISTED_LOCATIONS,
    RULE_CAP_PREFIX,
    active,
    apply_rule_cap,
    apply_rule_grouping,
    apply_sweep_dedup,
    rule_cap_counts,
)
from prxref.triage import Finding

FLOOR = 0.6
RULE = "input-validation"
TITLE = "Unvalidated input reaches the query"
BODY = "The handler passes the raw value through."


def _f(
    file: str = "src/a.py",
    line: int | None = 10,
    *,
    severity: str = "warning",
    confidence: float = 0.9,
    title: str = TITLE,
    body: str = BODY,
    rule: str | None = RULE,
    scope: str = "unknown",
    drop_reason: str | None = None,
    locations: tuple[tuple[str, int], ...] = (),
) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=severity,
        confidence=confidence,
        title=title,
        body=body,
        drop_reason=drop_reason,
        scope=scope,
        rule=rule,
        locations=locations,
    )


def _cap(findings, cap, *, sweep_start=None, floor=FLOOR):
    start = len(findings) if sweep_start is None else sweep_start
    return apply_rule_cap(findings, cap=cap, confidence_floor=floor, sweep_start=start)


def _counts(findings, cap, *, sweep_start=None, floor=FLOOR):
    start = len(findings) if sweep_start is None else sweep_start
    return rule_cap_counts(findings, cap=cap, confidence_floor=floor, sweep_start=start)


def _unchanged(before, after):
    return len(before) == len(after) and all(
        a is b for a, b in zip(before, after, strict=True)
    )


def _reason(cap, file, line):
    return f"rule cap exceeded (max {cap}): listed at {file}:{line}"


def _at(findings, file, line):
    [hit] = [f for f in findings if f.file == file and f.line == line]
    return hit


class TestAcceptance:
    """Five findings of one rule across four files, cap 2 (the issue's acceptance)."""

    def _five(self):
        return [
            _f("src/d.py", 40, confidence=0.7),
            _f("src/b.py", 20, confidence=0.9),
            _f("src/d.py", 50, confidence=0.7),
            _f("src/a.py", 10, confidence=0.95),
            _f("src/c.py", 30, confidence=0.8),
        ]

    def test_two_stay_active(self):
        out = _cap(self._five(), 2)
        assert len(out) == 5
        assert sorted((f.file, f.line) for f in active(out)) == [
            ("src/a.py", 10), ("src/b.py", 20),
        ]

    def test_the_best_lists_the_other_three_locations(self):
        out = _cap(self._five(), 2)
        best = out[3]
        assert best.locations == (
            ("src/c.py", 30), ("src/d.py", 40), ("src/d.py", 50),
        )
        assert best.body == (
            BODY + "\n\nAlso at: `src/c.py:30`, `src/d.py:40`, `src/d.py:50`"
        )

    def test_the_three_folded_carry_the_rule_cap_reason(self):
        out = _cap(self._five(), 2)
        folded = [out[0], out[2], out[4]]
        assert [f.drop_reason for f in folded] == [_reason(2, "src/a.py", 10)] * 3
        for f in folded:
            assert f.drop_reason.startswith(RULE_CAP_PREFIX)
            assert re.fullmatch(
                r"rule cap exceeded \(max 2\): listed at (?P<file>.+):(?P<line>\d+)",
                f.drop_reason,
            )

    def test_folded_findings_change_nothing_but_the_reason(self):
        findings = self._five()
        out = _cap(findings, 2)
        for i in (0, 2, 4):
            assert out[i] == Finding(
                **{**vars(findings[i]), "drop_reason": _reason(2, "src/a.py", 10)}
            )

    def test_the_second_kept_finding_is_the_same_object(self):
        findings = self._five()
        out = _cap(findings, 2)
        assert out[1] is findings[1]
        assert out[3] is not findings[3]

    def test_the_best_keeps_everything_but_body_and_locations(self):
        findings = self._five()
        best = _cap(findings, 2)[3]
        before = vars(findings[3])
        after = vars(best)
        for field in ("file", "line", "severity", "confidence", "title", "rule", "scope", "drop_reason"):
            assert after[field] == before[field], field


class TestSeverityRanksFirst:
    def _error_and_warnings(self, error_confidence):
        return [
            _f("src/w1.py", 1, confidence=0.9),
            _f("src/w2.py", 2, confidence=0.95),
            _f("src/e.py", 3, severity="error", confidence=error_confidence),
            _f("src/w3.py", 4, confidence=0.8),
        ]

    @pytest.mark.parametrize("error_confidence", [0.99, 0.65])
    def test_the_error_is_kept_at_cap_one(self, error_confidence):
        out = _cap(self._error_and_warnings(error_confidence), 1)
        [kept] = active(out)
        assert kept.file == "src/e.py"
        assert kept.severity == "error"
        assert kept.confidence == error_confidence
        for f in out:
            if f.file != "src/e.py":
                assert f.drop_reason == _reason(1, "src/e.py", 3)
                assert f.severity == "warning"

    def test_a_less_confident_error_still_outranks_every_warning(self):
        findings = self._error_and_warnings(0.65)
        assert all(
            f.confidence > 0.65 for f in findings if f.severity == "warning"
        )
        out = _cap(findings, 1)
        assert out[2].drop_reason is None
        assert out[2].locations == (("src/w1.py", 1), ("src/w2.py", 2), ("src/w3.py", 4))

    def test_kept_findings_are_not_promoted(self):
        findings = [
            _f("src/e.py", 1, severity="error", confidence=0.7),
            _f("src/w.py", 2, confidence=0.99),
            _f("src/s.py", 3, severity="spec", confidence=0.99),
        ]
        out = _cap(findings, 2)
        assert out[0].severity == "error" and out[0].confidence == 0.7
        assert out[1] is findings[1]
        assert out[2].drop_reason == _reason(2, "src/e.py", 1)

    def test_severity_is_compared_trimmed_and_lower_cased(self):
        findings = [
            _f("src/w.py", 1, confidence=0.99),
            _f("src/e.py", 2, severity=" Error ", confidence=0.7),
        ]
        out = _cap(findings, 1)
        assert out[1].drop_reason is None
        assert out[1].severity == " Error "
        assert out[0].drop_reason == _reason(1, "src/e.py", 2)


class TestTheKey:
    def test_rule_less_findings_with_different_titles_never_fold(self):
        findings = [
            _f("src/a.py", 1, rule=None, title="Unchecked write"),
            _f("src/b.py", 2, rule=None, title="Unchecked read"),
        ]
        assert _unchanged(findings, _cap(findings, 1))
        assert _counts(findings, 1) == []

    def test_rule_less_findings_with_one_normalized_title_fold_across_files(self):
        findings = [
            _f("src/a.py", 1, rule=None, title="`Unchecked` write.", confidence=0.9),
            _f("src/b.py", 2, rule=None, title="unchecked   write", confidence=0.8),
        ]
        out = _cap(findings, 1)
        assert out[0].drop_reason is None
        assert out[0].locations == (("src/b.py", 2),)
        assert out[1].drop_reason == _reason(1, "src/a.py", 1)

    def test_a_rule_named_and_a_rule_less_finding_never_fold(self):
        findings = [
            _f("src/a.py", 1, rule=RULE),
            _f("src/b.py", 2, rule=None),
        ]
        assert _unchanged(findings, _cap(findings, 1))
        assert _counts(findings, 1) == []

    def test_the_rule_is_compared_casefolded_and_whitespace_collapsed(self):
        findings = [
            _f("src/a.py", 1, rule="No-Bare-Except", title="One", confidence=0.9),
            _f("src/b.py", 2, rule="no-bare-except", title="Two", confidence=0.8),
            _f("src/c.py", 3, rule="NO-BARE-EXCEPT ", title="Three", confidence=0.7),
        ]
        out = _cap(findings, 1)
        assert len(active(out)) == 1
        assert out[0].locations == (("src/b.py", 2), ("src/c.py", 3))

    def test_the_same_rule_in_one_file_counts_every_line(self):
        findings = [_f("src/a.py", 10), _f("src/a.py", 20), _f("src/a.py", 30)]
        out = _cap(findings, 2)
        assert [f.drop_reason for f in out] == [None, None, _reason(2, "src/a.py", 10)]
        assert out[0].locations == (("src/a.py", 30),)

    def test_scope_is_not_part_of_the_key(self):
        findings = [
            _f("src/a.py", 1, scope="in", confidence=0.9),
            _f("src/b.py", 2, scope="out", confidence=0.8),
        ]
        out = _cap(findings, 1)
        assert out[1].drop_reason == _reason(1, "src/a.py", 1)


class TestCapOff:
    @pytest.mark.parametrize("cap", [0, -1, True, False, 2.0, None, "2"])
    def test_an_inactive_cap_returns_every_finding_as_the_same_object(self, cap):
        findings = [_f("src/a.py", 1), _f("src/b.py", 2), _f("src/c.py", 3)]
        out = _cap(findings, cap)
        assert out == findings
        assert _unchanged(findings, out)
        assert out is not findings

    def test_keys_at_or_under_the_cap_change_nothing(self):
        findings = [
            _f("src/a.py", 1), _f("src/b.py", 2),
            _f("src/c.py", 3, rule="other"),
        ]
        assert _unchanged(findings, _cap(findings, 2))


class TestSweepSide:
    def test_sweep_findings_are_never_counted(self):
        chunk = [_f("src/a.py", 1), _f("src/b.py", 2)]
        sweep = [_f("src/c.py", 3, confidence=0.99), _f("src/d.py", 4, confidence=0.99)]
        findings = chunk + sweep
        assert _unchanged(findings, _cap(findings, 2, sweep_start=2))
        assert _counts(findings, 2, sweep_start=2) == [
            {"rule": RULE, "kind": "rule", "total": 2, "kept": 2},
        ]

    def test_sweep_findings_are_never_folded_nor_absorb_a_fold(self):
        chunk = [_f("src/a.py", 1, confidence=0.9), _f("src/b.py", 2, confidence=0.8)]
        sweep = [
            _f("src/c.py", 3, severity="error", confidence=0.99),
            _f("src/d.py", 4, confidence=0.99),
        ]
        findings = chunk + sweep
        out = _cap(findings, 1, sweep_start=2)
        assert out[2] is sweep[0]
        assert out[3] is sweep[1]
        assert out[0].locations == (("src/b.py", 2),)
        assert out[1].drop_reason == _reason(1, "src/a.py", 1)

    def test_a_negative_sweep_start_makes_everything_sweep(self):
        findings = [_f("src/a.py", 1), _f("src/b.py", 2)]
        assert _unchanged(findings, _cap(findings, 1, sweep_start=-1))
        assert _counts(findings, 1, sweep_start=-1) == []

    def test_a_sweep_start_past_the_end_makes_everything_chunk(self):
        findings = [_f("src/a.py", 1, confidence=0.9), _f("src/b.py", 2, confidence=0.8)]
        out = _cap(findings, 1, sweep_start=99)
        assert out[1].drop_reason == _reason(1, "src/a.py", 1)


class TestNonCandidates:
    def _mixed(self):
        return [
            _f("src/a.py", 1, confidence=0.9),
            _f("src/b.py", 2, confidence=0.8),
            _f("src/c.py", 3, confidence=0.4),
            _f("src/d.py", 4, confidence=0.99, drop_reason='hedged: "if"'),
            _f("src/e.py", 5, confidence=0.99, severity="nit"),
        ]

    def test_sub_floor_and_dropped_findings_are_not_counted(self):
        findings = self._mixed()
        assert _unchanged(findings, _cap(findings, 2))
        assert _counts(findings, 2) == [
            {"rule": RULE, "kind": "rule", "total": 2, "kept": 2},
        ]

    def test_sub_floor_and_dropped_findings_are_not_folded_nor_listed(self):
        findings = self._mixed()
        out = _cap(findings, 1)
        assert out[0].locations == (("src/b.py", 2),)
        assert out[1].drop_reason == _reason(1, "src/a.py", 1)
        for i in (2, 3, 4):
            assert out[i] is findings[i]

    def test_the_floor_is_inclusive(self):
        findings = [_f("src/a.py", 1, confidence=0.9), _f("src/b.py", 2, confidence=FLOOR)]
        out = _cap(findings, 1)
        assert out[1].drop_reason == _reason(1, "src/a.py", 1)

    def test_a_none_floor_reads_the_default(self, monkeypatch):
        monkeypatch.delenv("PRXREF_CONFIDENCE_FLOOR", raising=False)
        findings = [
            _f("src/a.py", 1, confidence=0.9),
            _f("src/b.py", 2, confidence=0.65),
            _f("src/c.py", 3, confidence=0.55),
        ]
        out = apply_rule_cap(findings, cap=1, confidence_floor=None, sweep_start=3)
        assert out[1].drop_reason == _reason(1, "src/a.py", 1)
        assert out[2] is findings[2]
        counts = rule_cap_counts(findings, cap=1, confidence_floor=None, sweep_start=3)
        assert counts == [{"rule": RULE, "kind": "rule", "total": 2, "kept": 1}]

    def test_junk_fields_pass_through_without_raising(self):
        findings = [
            _f("src/a.py", 1, confidence=0.9, locations=None),
            _f("src/b.py", 2, confidence=0.8, locations=(("src/b.py", "x"), ("src/b.py", 7))),
            _f("", 3),
            Finding(**{**vars(_f("src/c.py", 4)), "confidence": "high"}),
            Finding(**{**vars(_f("src/d.py", 5)), "line": True}),
        ]
        out = _cap(findings, 1)
        assert out[0].locations == (("src/b.py", 2), ("src/b.py", 7))
        assert out[1].drop_reason == _reason(1, "src/a.py", 1)
        for i in (2, 3, 4):
            assert out[i] is findings[i]


class TestGroupRepresentatives:
    def test_a_folded_representative_hands_its_lines_to_the_best(self):
        findings = [
            _f("src/a.py", 10),
            _f("src/a.py", 20),
            _f("src/a.py", 30),
            _f("src/b.py", 5, severity="error"),
        ]
        grouped = apply_rule_grouping(findings, confidence_floor=FLOOR, sweep_start=4)
        assert grouped[0].locations == (("src/a.py", 20), ("src/a.py", 30))
        out = _cap(grouped, 1)
        best = out[3]
        assert best.locations == (
            ("src/a.py", 10), ("src/a.py", 20), ("src/a.py", 30),
        )
        assert best.body == (
            BODY + "\n\nAlso at: `src/a.py:10`, `src/a.py:20`, `src/a.py:30`"
        )
        assert out[0].drop_reason == _reason(1, "src/b.py", 5)
        assert out[1] is grouped[1]
        assert out[2] is grouped[2]
        assert out[1].drop_reason == f"{GROUPED_INTO_PREFIX}src/a.py:10"

    def test_a_group_counts_once(self):
        findings = [
            _f("src/a.py", 10), _f("src/a.py", 20), _f("src/a.py", 30),
            _f("src/b.py", 5),
        ]
        grouped = apply_rule_grouping(findings, confidence_floor=FLOOR, sweep_start=4)
        assert _unchanged(grouped, _cap(grouped, 2))
        assert _counts(grouped, 2) == [
            {"rule": RULE, "kind": "rule", "total": 2, "kept": 2},
        ]

    def test_a_best_representative_gets_one_also_at_paragraph(self):
        findings = [
            _f("src/a.py", 10, severity="error"),
            _f("src/a.py", 20, severity="error"),
            _f("src/b.py", 5),
        ]
        grouped = apply_rule_grouping(findings, confidence_floor=FLOOR, sweep_start=3)
        assert grouped[0].body == BODY + "\n\nAlso at: `src/a.py:20`"
        out = _cap(grouped, 1)
        best = out[0]
        assert best.locations == (("src/a.py", 20), ("src/b.py", 5))
        assert best.body == BODY + "\n\nAlso at: `src/a.py:20`, `src/b.py:5`"
        assert best.body.count("Also at:") == 1
        assert out[2].drop_reason == _reason(1, "src/a.py", 10)

    def test_a_best_representative_with_an_empty_body(self):
        findings = [
            _f("src/a.py", 10, severity="error", body=""),
            _f("src/a.py", 20, severity="error", body=""),
            _f("src/b.py", 5),
        ]
        grouped = apply_rule_grouping(findings, confidence_floor=FLOOR, sweep_start=3)
        assert grouped[0].body == "Also at: `src/a.py:20`"
        best = _cap(grouped, 1)[0]
        assert best.body == "Also at: `src/a.py:20`, `src/b.py:5`"

    def test_a_body_that_does_not_end_with_the_grouping_paragraph_keeps_it(self):
        best = _f(
            "src/a.py", 10, severity="error",
            body="Also at: `src/a.py:20`\n\nMore text.",
            locations=(("src/a.py", 20),),
        )
        out = _cap([best, _f("src/b.py", 5)], 1)
        assert out[0].body == (
            "Also at: `src/a.py:20`\n\nMore text.\n\nAlso at: `src/a.py:20`, `src/b.py:5`"
        )


class TestAlsoAtParagraph:
    def _spread(self, n):
        return [_f("src/best.py", 1, severity="error")] + [
            _f(f"src/m{i}.py", i + 1) for i in range(n)
        ]

    def test_five_locations_have_no_suffix(self):
        out = _cap(self._spread(5), 1)
        assert len(out[0].locations) == RULE_CAP_LISTED_LOCATIONS == 5
        assert out[0].body.endswith(
            "Also at: `src/m0.py:1`, `src/m1.py:2`, `src/m2.py:3`, `src/m3.py:4`, `src/m4.py:5`"
        )
        assert "more)" not in out[0].body

    def test_six_locations_list_five_and_count_one_more(self):
        out = _cap(self._spread(6), 1)
        assert len(out[0].locations) == 6
        assert out[0].locations[-1] == ("src/m5.py", 6)
        assert out[0].body.endswith(
            "Also at: `src/m0.py:1`, `src/m1.py:2`, `src/m2.py:3`, `src/m3.py:4`, `src/m4.py:5` (+1 more)"
        )
        assert "src/m5.py" not in out[0].body

    def test_nine_locations_count_four_more(self):
        out = _cap(self._spread(9), 1)
        assert out[0].body.endswith(" (+4 more)")

    def test_a_file_level_location_is_the_bare_path(self):
        findings = [
            _f("src/a.py", 10, severity="error"),
            _f("src/z.py", 0),
            _f("src/y.py", None),
        ]
        out = _cap(findings, 1)
        assert out[0].locations == (("src/y.py", 0), ("src/z.py", 0))
        assert out[0].body.endswith("\n\nAlso at: `src/y.py`, `src/z.py`")
        assert out[1].drop_reason == _reason(1, "src/a.py", 10)

    def test_a_file_level_best_is_named_at_line_zero(self):
        findings = [
            _f("src/a.py", 0, severity="error"),
            _f("src/b.py", 7),
        ]
        out = _cap(findings, 1)
        assert out[1].drop_reason == _reason(1, "src/a.py", 0)
        assert out[0].body.endswith("\n\nAlso at: `src/b.py:7`")

    def test_duplicate_and_own_locations_are_listed_once_or_not_at_all(self):
        findings = [
            _f("src/a.py", 10, severity="error"),
            _f("src/a.py", 10),
            _f("src/b.py", 5, locations=(("src/b.py", 6), ("src/a.py", 10))),
            _f("src/b.py", 6),
        ]
        out = _cap(findings, 1)
        assert out[0].locations == (("src/b.py", 5), ("src/b.py", 6))
        assert out[0].body.endswith("\n\nAlso at: `src/b.py:5`, `src/b.py:6`")

    def test_nothing_left_to_list_keeps_the_body_but_rewrites_the_best(self):
        findings = [
            _f("src/a.py", 10, severity="error"),
            _f("src/a.py", 10, title="Another wording"),
        ]
        out = _cap(findings, 1)
        assert out[0] is not findings[0]
        assert out[0] == findings[0]
        assert out[0].body == BODY
        assert out[0].locations == ()
        assert out[1].drop_reason == _reason(1, "src/a.py", 10)

    def test_a_none_body_gains_the_paragraph_alone(self):
        findings = [_f("src/a.py", 1, severity="error", body=None), _f("src/b.py", 2)]
        assert _cap(findings, 1)[0].body == "Also at: `src/b.py:2`"


class TestTies:
    def _tied(self):
        return [
            _f("src/b.py", 1, title="Gamma"),
            _f("src/a.py", 9, title="Delta"),
            _f("src/a.py", 5, title="Beta"),
            _f("src/a.py", 5, title="alpha"),
            _f("src/c.py", 1, title="Epsilon"),
        ]

    def _kept(self, out):
        return sorted((f.file, f.line, f.title) for f in active(out))

    def test_ties_rank_by_file_line_then_normalized_title(self):
        out = _cap(self._tied(), 2)
        assert self._kept(out) == [("src/a.py", 5, "Beta"), ("src/a.py", 5, "alpha")]
        best = next(f for f in out if f.title == "alpha")
        assert best.locations == (("src/a.py", 9), ("src/b.py", 1), ("src/c.py", 1))

    def test_input_order_does_not_change_the_result(self):
        expected = None
        for perm in itertools.permutations(self._tied()):
            out = _cap(list(perm), 2)
            got = (
                self._kept(out),
                sorted((f.file, f.line, f.title, f.locations, f.body) for f in active(out)),
                sorted((f.file, f.line, f.title, f.drop_reason) for f in out),
            )
            if expected is None:
                expected = got
            assert got == expected


class TestRuleCapCounts:
    def _mixed(self):
        return [
            _f("src/a.py", 1, rule="Zeta", title="z1"),
            _f("src/b.py", 2, rule="Zeta", title="z2"),
            _f("src/c.py", 3, rule="Zeta", title="z3"),
            _f("src/a.py", 4, rule="alpha", title="a1", severity="error", confidence=0.7),
            _f("src/b.py", 5, rule="ALPHA", title="a2"),
            _f("src/c.py", 6, rule="Alpha", title="a3"),
            _f("src/a.py", 7, rule=None, title="Alpha  pattern"),
            _f("src/b.py", 8, rule=None, title="alpha pattern."),
            _f("src/c.py", 9, rule=None, title="`alpha` pattern"),
            _f("src/a.py", 10, rule="Beta", title="b1"),
            _f("src/b.py", 11, rule="beta", title="b2"),
            _f("src/a.py", 12, rule="solo", title="s1"),
        ]

    def test_shape_key_order_and_sort(self):
        rows = _counts(self._mixed(), 2)
        assert rows == [
            {"rule": "alpha", "kind": "rule", "total": 3, "kept": 2},
            {"rule": "Zeta", "kind": "rule", "total": 3, "kept": 2},
            {"rule": "Alpha pattern", "kind": "title", "total": 3, "kept": 2},
            {"rule": "Beta", "kind": "rule", "total": 2, "kept": 2},
        ]
        for row in rows:
            assert list(row) == ["rule", "kind", "total", "kept"]

    def test_a_key_with_one_finding_is_absent(self):
        rows = _counts(self._mixed(), 2)
        assert all(row["rule"] != "solo" for row in rows)

    @pytest.mark.parametrize(
        ("cap", "kept"),
        [(1, [1, 1, 1, 1]), (2, [2, 2, 2, 2]), (3, [3, 3, 3, 2]), (9, [3, 3, 3, 2]),
         (0, [3, 3, 3, 2]), (-1, [3, 3, 3, 2])],
    )
    def test_kept_is_the_minimum_of_total_and_cap(self, cap, kept):
        assert [row["kept"] for row in _counts(self._mixed(), cap)] == kept

    def test_the_name_is_the_first_ranked_rule_whitespace_collapsed(self):
        findings = [
            _f("src/a.py", 1, rule="no-bare-except", confidence=0.99),
            _f("src/b.py", 2, rule="No  Bare\nExcept", severity="error", confidence=0.7),
            _f("src/c.py", 3, rule="no bare except"),
        ]
        assert _counts(findings, 1) == [
            {"rule": "No Bare Except", "kind": "rule", "total": 2, "kept": 1},
        ]

    def test_the_name_is_the_first_ranked_title_whitespace_collapsed(self):
        findings = [
            _f("src/a.py", 1, rule=None, title="unchecked write.", confidence=0.7),
            _f("src/b.py", 2, rule=None, title="Unchecked   write", confidence=0.9),
        ]
        assert _counts(findings, 1) == [
            {"rule": "Unchecked write", "kind": "title", "total": 2, "kept": 1},
        ]

    def test_equal_casefolded_names_fall_back_to_the_name(self):
        sharp = "Stra\N{LATIN SMALL LETTER SHARP S}e"
        findings = [
            _f("src/a.py", 1, rule=None, title=sharp),
            _f("src/b.py", 2, rule=None, title=sharp),
            _f("src/a.py", 3, rule=None, title="STRASSE"),
            _f("src/b.py", 4, rule=None, title="STRASSE"),
        ]
        rows = _counts(findings, 1)
        assert [row["rule"] for row in rows] == ["STRASSE", sharp]
        assert rows[0]["rule"].casefold() == rows[1]["rule"].casefold()

    def test_no_key_reaching_two_is_an_empty_list(self):
        assert _counts([_f("src/a.py", 1)], 2) == []
        assert _counts([], 2) == []

    def test_the_counts_agree_with_the_pass(self):
        findings = self._mixed()
        out = _cap(findings, 2)
        folded = sum(
            1 for f in out
            if f.drop_reason is not None and f.drop_reason.startswith(RULE_CAP_PREFIX)
        )
        rows = _counts(findings, 2)
        assert folded == sum(row["total"] - row["kept"] for row in rows) == 3
        rewritten = sum(
            1 for before, after in zip(findings, out, strict=True)
            if after is not before and after.drop_reason is None
        )
        assert rewritten == sum(1 for row in rows if row["total"] > row["kept"]) == 3


class TestSweepDedupKeys:
    def _chunk(self, second_reason=None):
        chunk = [
            _f("src/a.py", 10, title="Raw query parameter trusted", confidence=0.9),
            _f("src/b.py", 20, title="Raw query parameter trusted", confidence=0.8),
        ]
        if second_reason is None:
            return _cap(chunk, 1)
        return [chunk[0], _f(
            "src/b.py", 20, title="Raw query parameter trusted",
            confidence=0.8, drop_reason=second_reason,
        )]

    def _sweep(self):
        return _f("src/b.py", 44, title="raw query parameter trusted.", rule=None, confidence=0.99)

    def test_a_sweep_copy_of_a_rule_capped_chunk_finding_is_dropped(self):
        chunk = self._chunk()
        assert chunk[1].drop_reason == _reason(1, "src/a.py", 10)
        out = apply_sweep_dedup(chunk + [self._sweep()], sweep_start=2)
        assert out[2].drop_reason == "duplicate of chunk finding"
        assert out[1].drop_reason == _reason(1, "src/a.py", 10)

    def test_a_sweep_copy_of_a_chunk_finding_dropped_for_another_reason_is_kept(self):
        chunk = self._chunk(second_reason='hedged: "x"')
        sweep = self._sweep()
        out = apply_sweep_dedup(chunk + [sweep], sweep_start=2)
        assert out[2] is sweep
        assert out[2].drop_reason is None


class TestDocumentedStrings:
    def test_the_constants(self):
        assert RULE_CAP_PREFIX == "rule cap exceeded "
        assert RULE_CAP_LISTED_LOCATIONS == 5
