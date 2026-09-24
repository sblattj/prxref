"""The finding-grouping pass ``apply_rule_grouping`` (issue #13, T3).

Also covers the one ``apply_sweep_dedup`` change T3 makes: a chunk finding
the grouping pass folded away still adds its key to the sweep dedup, so a
sweep copy that restates it is dropped whichever pass runs first.
"""
from __future__ import annotations

import copy
import random
import re
from pathlib import Path

import pytest

from prxref.quality import (
    GROUPED_INTO_PREFIX,
    active,
    apply_quality_gate,
    apply_rule_grouping,
    apply_sweep_dedup,
)
from prxref.triage import Finding

REPO_ROOT = Path(__file__).resolve().parents[1]
FLOOR = 0.6


def _f(
    line: int = 10,
    *,
    file: str = "src/app.py",
    severity: str = "warning",
    confidence: float = 0.9,
    title: str = "Unvalidated input reaches the query",
    body: str = "The handler passes the raw value through.",
    rule: str | None = "input-validation",
    scope: str = "unknown",
    drop_reason: str | None = None,
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
    )


def _group(findings, *, sweep_start=None, floor=FLOOR):
    start = len(findings) if sweep_start is None else sweep_start
    return apply_rule_grouping(findings, confidence_floor=floor, sweep_start=start)


def _unchanged(before, after):
    return len(before) == len(after) and all(
        a is b for a, b in zip(before, after, strict=True)
    )


class TestGroupsTheSameRule:
    def test_the_same_rule_across_lines_groups(self):
        findings = [_f(30), _f(10), _f(20)]
        out = _group(findings)
        assert out[1].drop_reason is None
        assert out[1].body.endswith(
            "\n\nAlso at: `src/app.py:20`, `src/app.py:30`"
        )
        assert out[0].drop_reason == "grouped into src/app.py:10"
        assert out[2].drop_reason == "grouped into src/app.py:10"
        assert len(active(out)) == 1

    def test_the_same_rule_across_files_stays_separate(self):
        findings = [_f(10, file="src/a.py"), _f(20, file="src/b.py")]
        out = _group(findings)
        assert _unchanged(findings, out)

    def test_across_files_each_file_forms_its_own_group(self):
        findings = [
            _f(10, file="src/a.py"),
            _f(20, file="src/b.py"),
            _f(15, file="src/a.py"),
            _f(25, file="src/b.py"),
        ]
        out = _group(findings)
        assert out[0].body.endswith("Also at: `src/a.py:15`")
        assert out[1].body.endswith("Also at: `src/b.py:25`")
        assert out[2].drop_reason == "grouped into src/a.py:10"
        assert out[3].drop_reason == "grouped into src/b.py:20"

    def test_different_rules_in_one_file_stay_separate(self):
        findings = [_f(10, rule="input-validation"), _f(20, rule="no-magic-numbers")]
        assert _unchanged(findings, _group(findings))

    @pytest.mark.parametrize(
        "variants",
        [
            ("No-Magic-Numbers", "no-magic-numbers", "NO-MAGIC-NUMBERS"),
            ("Straße", "STRASSE", "strasse"),
            ("no  magic\tnumbers", "No Magic Numbers", " no magic numbers "),
        ],
    )
    def test_the_rule_key_is_casefolded_and_whitespace_collapsed(self, variants):
        findings = [
            _f(10 * (i + 1), rule=rule, title=f"Distinct title {i}")
            for i, rule in enumerate(variants)
        ]
        out = _group(findings)
        assert out[0].drop_reason is None
        assert [f.drop_reason for f in out[1:]] == [
            "grouped into src/app.py:10",
            "grouped into src/app.py:10",
        ]

    def test_the_representative_keeps_its_own_rule_spelling(self):
        out = _group([_f(10, rule="No-Magic-Numbers"), _f(20, rule="no-magic-numbers")])
        assert out[0].rule == "No-Magic-Numbers"

    def test_grouping_keys_on_rule_not_title(self):
        findings = [
            _f(10, title="Raw query parameter trusted"),
            _f(20, title="Request body is never checked"),
        ]
        out = _group(findings)
        assert out[1].drop_reason == "grouped into src/app.py:10"


class TestTitleFallback:
    def test_findings_without_a_rule_group_on_normalized_title(self):
        findings = [
            _f(10, rule=None, title="Missing `await` on promise."),
            _f(20, rule=None, title="missing await on promise"),
        ]
        out = _group(findings)
        assert out[0].body.endswith("Also at: `src/app.py:20`")
        assert out[1].drop_reason == "grouped into src/app.py:10"

    def test_different_titles_without_a_rule_stay_separate(self):
        findings = [
            _f(10, rule=None, title="Missing await on promise"),
            _f(20, rule=None, title="Unbounded retry loop"),
        ]
        assert _unchanged(findings, _group(findings))

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_a_blank_rule_falls_back_to_the_title(self, blank):
        findings = [_f(10, rule=blank, title="Same title"), _f(20, rule=None, title="Same title")]
        out = _group(findings)
        assert out[1].drop_reason == "grouped into src/app.py:10"

    def test_a_ruled_finding_never_groups_with_an_unruled_one(self):
        findings = [
            _f(10, rule="Same title", title="Same title"),
            _f(20, rule=None, title="Same title"),
        ]
        assert _unchanged(findings, _group(findings))

    def test_a_rule_equal_to_another_findings_title_does_not_collide(self):
        findings = [
            _f(10, rule="missing input validation", title="Anything else"),
            _f(20, rule=None, title="Missing input validation"),
        ]
        assert _unchanged(findings, _group(findings))

    def test_an_empty_title_without_a_rule_is_not_grouped(self):
        findings = [_f(10, rule=None, title=""), _f(20, rule=None, title="  ")]
        assert _unchanged(findings, _group(findings))


class TestCandidates:
    def test_a_member_below_the_floor_is_neither_listed_nor_dropped(self):
        findings = [_f(10), _f(20), _f(30, confidence=0.3)]
        out = _group(findings)
        assert out[0].body.endswith("Also at: `src/app.py:20`")
        assert "src/app.py:30" not in out[0].body
        assert out[2] is findings[2]
        gated = apply_quality_gate(out, confidence_floor=FLOOR)
        low = [f for f in gated if f.line == 30]
        assert low[0].drop_reason == "confidence 0.30 below floor 0.60"

    def test_a_below_floor_finding_never_anchors(self):
        findings = [_f(5, confidence=0.3), _f(10), _f(20)]
        out = _group(findings)
        assert out[0] is findings[0]
        assert out[2].drop_reason == "grouped into src/app.py:10"

    def test_the_floor_is_resolved_like_the_gate(self, monkeypatch):
        findings = [_f(10), _f(20)]
        monkeypatch.setenv("PRXREF_CONFIDENCE_FLOOR", "0.95")
        assert _unchanged(findings, apply_rule_grouping(
            findings, confidence_floor=None, sweep_start=2,
        ))
        grouped = apply_rule_grouping(findings, confidence_floor=0.5, sweep_start=2)
        assert grouped[1].drop_reason == "grouped into src/app.py:10"
        monkeypatch.delenv("PRXREF_CONFIDENCE_FLOOR")
        default = apply_rule_grouping(findings, confidence_floor=None, sweep_start=2)
        assert default[1].drop_reason == "grouped into src/app.py:10"

    def test_a_finding_exactly_at_the_floor_is_a_candidate(self):
        out = _group([_f(10, confidence=FLOOR), _f(20, confidence=FLOOR)])
        assert out[1].drop_reason == "grouped into src/app.py:10"

    def test_an_already_dropped_member_is_ignored(self):
        findings = [
            _f(10, drop_reason='hedged: "if it still leases"'),
            _f(20),
            _f(30),
        ]
        out = _group(findings)
        assert out[0] is findings[0]
        assert out[1].body.endswith("Also at: `src/app.py:30`")
        assert "src/app.py:10" not in out[1].body
        assert out[2].drop_reason == "grouped into src/app.py:20"

    def test_an_invalid_severity_is_not_a_candidate(self):
        findings = [_f(10, severity="blocker"), _f(20)]
        assert _unchanged(findings, _group(findings))

    def test_severity_is_compared_trimmed_and_lower_cased(self):
        out = _group([_f(10, severity=" Error "), _f(20, severity="WARNING")])
        assert out[0].severity == "error"
        assert out[1].drop_reason == "grouped into src/app.py:10"


class TestSweepSide:
    def test_sweep_findings_are_untouched(self):
        chunk = [_f(10)]
        sweep = [_f(20), _f(30), _f(5)]
        findings = chunk + sweep
        out = _group(findings, sweep_start=1)
        assert _unchanged(findings, out)

    def test_a_sweep_finding_never_anchors_or_joins_a_chunk_group(self):
        findings = [_f(20), _f(30), _f(5)]
        out = _group(findings, sweep_start=2)
        assert out[2] is findings[2]
        assert out[0].body.endswith("Also at: `src/app.py:30`")
        assert out[1].drop_reason == "grouped into src/app.py:20"

    def test_sweep_start_zero_or_below_groups_nothing(self):
        findings = [_f(10), _f(20)]
        assert _unchanged(findings, _group(findings, sweep_start=0))
        assert _unchanged(findings, _group(findings, sweep_start=-3))

    def test_sweep_start_past_the_end_treats_everything_as_chunk(self):
        out = _group([_f(10), _f(20)], sweep_start=99)
        assert out[1].drop_reason == "grouped into src/app.py:10"


class TestRepresentative:
    def test_the_smallest_positive_line_anchors_with_top_severity_and_confidence(self):
        findings = [
            _f(30, severity="outofscope", confidence=0.95, body="Third."),
            _f(12, severity="warning", confidence=0.7, body="First."),
            _f(20, severity="error", confidence=0.8, body="Second."),
        ]
        out = _group(findings)
        rep = out[1]
        assert rep.line == 12
        assert rep.severity == "error"
        assert rep.confidence == 0.95
        assert rep.body == "First.\n\nAlso at: `src/app.py:20`, `src/app.py:30`"
        assert rep.title == findings[1].title

    @pytest.mark.parametrize(
        ("severities", "top"),
        [
            (("outofscope", "spec"), "spec"),
            (("spec", "warning"), "warning"),
            (("warning", "error"), "error"),
            (("outofscope", "outofscope"), "outofscope"),
        ],
    )
    def test_the_group_takes_its_highest_severity(self, severities, top):
        findings = [_f(10 * (i + 1), severity=s) for i, s in enumerate(severities)]
        assert _group(findings)[0].severity == top

    def test_line_zero_never_anchors_when_a_positive_line_exists(self):
        findings = [_f(0, confidence=0.99), _f(30), _f(12)]
        out = _group(findings)
        assert out[2].drop_reason is None
        assert out[2].confidence == 0.99
        assert out[2].body.endswith("\n\nAlso at: `src/app.py:30`")
        assert out[0].drop_reason == "grouped into src/app.py:12"
        assert "src/app.py:0" not in out[2].body

    def test_an_all_file_level_group_anchors_the_top_ranked_member(self):
        findings = [
            _f(0, confidence=0.7, body="Lower."),
            _f(0, confidence=0.9, body="Higher."),
        ]
        out = _group(findings)
        assert out[1].drop_reason is None
        assert out[1].body == "Higher."
        assert out[0].drop_reason == "grouped into src/app.py:0"

    def test_a_tie_on_the_line_goes_to_the_more_confident_member(self):
        findings = [_f(10, confidence=0.7, body="Weaker."), _f(10, confidence=0.9, body="Stronger.")]
        out = _group(findings)
        assert out[1].drop_reason is None
        assert out[1].body == "Stronger."
        assert out[0].drop_reason == "grouped into src/app.py:10"

    def test_each_other_line_is_listed_once_in_line_order(self):
        findings = [_f(40), _f(10), _f(25), _f(40), _f(10)]
        out = _group(findings)
        assert out[1].body.endswith("Also at: `src/app.py:25`, `src/app.py:40`")
        assert out[1].body.count("Also at:") == 1

    def test_an_empty_body_gets_the_list_without_a_leading_blank_line(self):
        out = _group([_f(10, body=""), _f(20)])
        assert out[0].body == "Also at: `src/app.py:20`"

    def test_trailing_whitespace_is_trimmed_before_the_list(self):
        out = _group([_f(10, body="Body.\n"), _f(20)])
        assert out[0].body == "Body.\n\nAlso at: `src/app.py:20`"

    def test_scope_is_ignored_and_each_member_keeps_its_own(self):
        findings = [_f(10, scope="out"), _f(20, scope="in")]
        out = _group(findings)
        assert out[0].scope == "out"
        assert out[1].scope == "in"
        assert out[1].drop_reason == "grouped into src/app.py:10"

    def test_a_dropped_member_keeps_every_other_field(self):
        findings = [_f(10), _f(20, severity="error", confidence=0.95, body="B.")]
        member = _group(findings)[1]
        assert (member.line, member.severity, member.confidence, member.body) == (
            20, "error", 0.95, "B.",
        )

    def test_the_result_does_not_depend_on_input_order(self):
        findings = [
            _f(30, severity="error", confidence=0.7, body="C."),
            _f(12, confidence=0.8, body="A1."),
            _f(12, confidence=0.9, body="A2."),
            _f(0, confidence=0.95, body="Z."),
            _f(20, severity="spec", body="B."),
        ]
        expected = sorted(map(repr, _group(findings)))
        rng = random.Random(13)
        for _ in range(20):
            shuffled = findings[:]
            rng.shuffle(shuffled)
            assert sorted(map(repr, _group(shuffled))) == expected


class TestShape:
    @pytest.mark.parametrize(
        "findings",
        [
            [],
            [_f(10)],
            [_f(10), _f(20)],
            [_f(10), _f(20, file="src/b.py"), _f(30), _f(0), _f(5, rule=None)],
        ],
    )
    def test_the_length_and_order_are_unchanged(self, findings):
        out = _group(findings)
        assert len(out) == len(findings)
        assert [(f.file, f.line, f.title) for f in out] == [
            (f.file, f.line, f.title) for f in findings
        ]

    def test_no_group_of_one_changes_anything(self):
        findings = [
            _f(10, rule="a"),
            _f(20, rule="b"),
            _f(30, rule=None, title="Only one of these"),
            _f(40, file="src/b.py", rule="a"),
        ]
        assert _unchanged(findings, _group(findings))

    def test_the_input_is_not_mutated(self):
        findings = [_f(10), _f(20), _f(30, rule=None, title="x y z")]
        snapshot = copy.deepcopy(findings)
        _group(findings)
        assert findings == snapshot

    @pytest.mark.parametrize(
        "odd",
        [
            {"confidence": "high"},
            {"confidence": None},
            {"severity": None},
            {"severity": 3},
            {"title": 5, "rule": None},
            {"title": 5},
            {"rule": 7},
            {"line": "12"},
            {"line": True},
            {"file": None},
            {"file": ""},
            {"body": 3},
        ],
    )
    def test_a_finding_of_the_wrong_shape_passes_through_without_raising(self, odd):
        findings = [_f(10), _f(20)]
        weird = Finding(**{**vars(_f(30)), **odd})
        out = _group(findings + [weird])
        assert out[2] is weird
        assert out[1].drop_reason == "grouped into src/app.py:10"
        assert "src/app.py:30" not in out[0].body

    def test_a_none_line_counts_as_file_level(self):
        findings = [Finding(**{**vars(_f(0)), "line": None}), _f(20)]
        out = _group(findings)
        assert out[1].drop_reason is None
        assert out[0].drop_reason == "grouped into src/app.py:20"


class TestDocumentedStrings:
    """FND documented these strings before the pass existed (seams.md)."""

    def test_the_drop_reason_matches_the_documented_template(self):
        env_vars = (REPO_ROOT / "docs" / "env-vars.md").read_text(encoding="utf-8")
        quality_md = (REPO_ROOT / "docs" / "quality.md").read_text(encoding="utf-8")
        assert "`grouped into <file>:<line>`" in env_vars
        assert "| `grouped into <file>:<line>` | `apply_rule_grouping` |" in quality_md
        reason = _group([_f(10, file="pkg/mod.py"), _f(20, file="pkg/mod.py")])[1].drop_reason
        assert reason == "grouped into pkg/mod.py:10"
        assert GROUPED_INTO_PREFIX == "grouped into "
        assert re.fullmatch(r"grouped into (?P<file>.+):(?P<line>\d+)", reason)

    def test_the_body_suffix_uses_the_documented_prefix(self):
        env_vars = (REPO_ROOT / "docs" / "env-vars.md").read_text(encoding="utf-8")
        env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        documented = re.search(r"`(Also at: )…`", env_vars)
        assert documented is not None
        assert '("Also at: ...")' in env_example
        body = _group([_f(10, body="B."), _f(20), _f(30)])[0].body
        assert body == (
            "B.\n\n" + documented.group(1) + "`src/app.py:20`, `src/app.py:30`"
        )


class TestSweepDedupKeys:
    def _grouped_chunk_and_sweep_copy(self):
        chunk = [
            _f(10, title="Raw query parameter trusted"),
            _f(20, title="Request body is never checked"),
        ]
        sweep = [_f(20, rule=None, title="Request body is never checked", confidence=0.8)]
        return chunk + sweep

    def test_a_sweep_copy_of_a_grouped_member_is_deduped(self):
        findings = self._grouped_chunk_and_sweep_copy()
        grouped = _group(findings, sweep_start=2)
        assert grouped[1].drop_reason == "grouped into src/app.py:10"
        assert grouped[2].drop_reason is None
        deduped = apply_sweep_dedup(grouped, sweep_start=2)
        assert deduped[2].drop_reason == "duplicate of chunk finding"
        assert deduped[:2] == grouped[:2]

    def test_the_same_copy_is_deduped_when_sweep_dedup_runs_first(self):
        findings = self._grouped_chunk_and_sweep_copy()
        deduped_first = apply_sweep_dedup(findings, sweep_start=2)
        grouped_after = _group(deduped_first, sweep_start=2)
        assert grouped_after[2].drop_reason == "duplicate of chunk finding"
        assert grouped_after[1].drop_reason == "grouped into src/app.py:10"

    def test_a_member_dropped_for_another_reason_still_adds_no_key(self):
        findings = [
            _f(20, title="Request body is never checked", drop_reason='hedged: "if it"'),
            _f(20, rule=None, title="Request body is never checked"),
        ]
        deduped = apply_sweep_dedup(findings, sweep_start=1)
        assert deduped[1].drop_reason is None

    def test_a_grouped_member_adds_its_key_with_the_reworded_tier_on(self):
        grouped = _group(self._grouped_chunk_and_sweep_copy(), sweep_start=2)
        deduped = apply_sweep_dedup(grouped, sweep_start=2, similarity=0.5)
        assert deduped[2].drop_reason == "duplicate of chunk finding"


class TestCapsCountGroups:
    def test_the_error_cap_counts_groups_not_lines(self):
        findings = [
            _f(10, severity="error"),
            _f(20, severity="error"),
            _f(30, severity="error"),
            _f(40, severity="error", rule="no-magic-numbers"),
        ]
        ungrouped = apply_quality_gate(findings, confidence_floor=FLOOR, max_errors=2)
        assert len(active(ungrouped)) == 2
        grouped = apply_quality_gate(
            _group(findings), confidence_floor=FLOOR, max_errors=2,
        )
        survivors = active(grouped)
        assert [(f.line, f.rule) for f in survivors] == [
            (10, "input-validation"), (40, "no-magic-numbers"),
        ]
        assert survivors[0].body.endswith("Also at: `src/app.py:20`, `src/app.py:30`")
        assert not any("cap exceeded" in (f.drop_reason or "") for f in grouped)
