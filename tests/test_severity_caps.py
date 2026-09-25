"""Per-severity caps in ``apply_quality_gate`` (issue #13)."""
from __future__ import annotations

import itertools

import pytest

from prxref.cli import _fail_on_exit
from prxref.quality import active, apply_quality_gate
from prxref.triage import Finding


def _f(**kwargs) -> Finding:
    defaults = {
        "file": "src/app.py",
        "line": 10,
        "severity": "warning",
        "confidence": 0.8,
        "title": "Possible bug",
        "body": "Details about the finding.",
    }
    defaults.update(kwargs)
    return Finding(**defaults)


def _tied(severity: str) -> list[Finding]:
    return [
        _f(file="src/a.py", line=1, severity=severity, confidence=0.9, title="alpha"),
        _f(file="src/a.py", line=2, severity=severity, confidence=0.9, title="bravo"),
        _f(file="src/a.py", line=3, severity=severity, confidence=0.9, title="charlie"),
    ]


def _mixed() -> list[Finding]:
    return [
        _f(file="src/a.py", line=1, severity="error", confidence=0.9, title="e1"),
        _f(file="src/a.py", line=2, severity="error", confidence=0.7, title="e2"),
        _f(file="src/a.py", line=3, severity="error", confidence=0.8, title="e3"),
        _f(file="src/b.py", line=1, severity="warning", confidence=0.9, title="w1"),
        _f(file="src/b.py", line=2, severity="Warning", confidence=0.7, title="w2"),
        _f(file="src/b.py", line=3, severity="warning", confidence=0.4, title="w3"),
        _f(file="src/c.py", line=1, severity="outofscope", confidence=0.9, title="o1"),
        _f(file="src/c.py", line=2, severity="outofscope", confidence=0.8, title="o2"),
        _f(file="src/d.py", line=1, severity="spec", confidence=0.9, title="s1"),
        _f(file="src/d.py", line=2, severity="bogus", confidence=0.9, title="x1"),
        _f(
            file="src/e.py", line=1, severity="warning", confidence=0.9,
            title="pre", drop_reason="hedged: \"if\"",
        ),
    ]


def _cap_kwargs(severity: str, cap: int | None) -> dict[str, int | None]:
    return {
        "warning": {"max_warning_findings": cap},
        "outofscope": {"max_outofscope_findings": cap},
    }[severity]


def _by_title(findings: list[Finding]) -> dict[str, Finding]:
    return {f.title: f for f in findings}


CAPPED = ("warning", "outofscope")


class TestCapsHonoured:
    """Each new cap keeps the top N of its severity, ranked by content."""

    @pytest.mark.parametrize("severity", CAPPED)
    def test_survivors_do_not_depend_on_arrival_order(self, severity):
        seen = set()
        for perm in itertools.permutations(_tied(severity)):
            staged = apply_quality_gate(
                list(perm), confidence_floor=0.6, **_cap_kwargs(severity, 2)
            )
            seen.add(frozenset(f.title for f in staged if f.drop_reason is None))
        assert seen == {frozenset({"alpha", "bravo"})}

    @pytest.mark.parametrize("severity", CAPPED)
    def test_excess_is_marked_not_removed(self, severity):
        staged = apply_quality_gate(
            _tied(severity), confidence_floor=0.6, **_cap_kwargs(severity, 2)
        )
        assert len(staged) == 3
        assert _by_title(staged)["charlie"].drop_reason == (
            f"{severity} cap exceeded (max 2)"
        )

    @pytest.mark.parametrize("severity", CAPPED)
    def test_confidence_decides_before_content(self, severity):
        findings = [
            _f(file="src/a.py", line=1, severity=severity, confidence=0.7, title="aaa"),
            _f(file="src/z.py", line=9, severity=severity, confidence=0.95, title="zzz"),
        ]
        staged = _by_title(
            apply_quality_gate(
                findings, confidence_floor=0.6, **_cap_kwargs(severity, 1)
            )
        )
        assert staged["zzz"].drop_reason is None
        assert staged["aaa"].drop_reason == f"{severity} cap exceeded (max 1)"

    def test_a_cap_counts_only_its_own_severity(self):
        staged = _by_title(
            apply_quality_gate(
                _mixed(),
                confidence_floor=0.6,
                max_errors=10,
                max_warning_findings=1,
                max_outofscope_findings=2,
            )
        )
        assert [t for t in ("e1", "e2", "e3") if staged[t].drop_reason] == []
        assert staged["w1"].drop_reason is None
        assert staged["w2"].drop_reason == "warning cap exceeded (max 1)"
        assert staged["o1"].drop_reason is None
        assert staged["o2"].drop_reason is None

    def test_earlier_drops_keep_their_reason_and_take_no_slot(self):
        staged = _by_title(
            apply_quality_gate(
                _mixed(), confidence_floor=0.6, max_warning_findings=2,
            )
        )
        assert staged["w3"].drop_reason == "confidence 0.40 below floor 0.60"
        assert staged["pre"].drop_reason == 'hedged: "if"'
        assert staged["w1"].drop_reason is None
        assert staged["w2"].drop_reason is None

    def test_a_case_normalized_severity_counts_toward_its_cap(self):
        staged = _by_title(
            apply_quality_gate(
                _mixed(), confidence_floor=0.6, max_warning_findings=1,
            )
        )
        assert staged["w2"].severity == "warning"
        assert staged["w2"].drop_reason == "warning cap exceeded (max 1)"


class TestNoneAndZero:
    @pytest.mark.parametrize("severity", CAPPED)
    def test_none_is_unlimited(self, severity):
        many = [
            _f(file="src/a.py", line=i, severity=severity, confidence=0.9, title=f"t{i}")
            for i in range(1, 51)
        ]
        staged = apply_quality_gate(
            many, confidence_floor=0.6, **_cap_kwargs(severity, None)
        )
        assert len(active(staged)) == 50

    @pytest.mark.parametrize("severity", CAPPED)
    def test_none_reads_no_environment_variable(self, severity, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_WARNING_FINDINGS", "0")
        monkeypatch.setenv("PRXREF_MAX_OUTOFSCOPE_FINDINGS", "0")
        staged = apply_quality_gate(_tied(severity), confidence_floor=0.6)
        assert len(active(staged)) == 3

    @pytest.mark.parametrize("severity", CAPPED)
    def test_zero_drops_every_finding_of_that_severity(self, severity):
        staged = apply_quality_gate(
            _mixed(), confidence_floor=0.6, max_errors=10,
            **_cap_kwargs(severity, 0),
        )
        survivors = active(staged)
        assert [f.title for f in survivors if f.severity == severity] == []
        capped = [
            f for f in staged if f.drop_reason == f"{severity} cap exceeded (max 0)"
        ]
        assert capped and all(f.severity == severity for f in capped)
        other = {f.severity for f in survivors}
        assert other == {"error", "warning", "outofscope", "spec"} - {severity}


class TestErrorCapUnchanged:
    def test_error_reason_text_is_unchanged_beside_the_new_caps(self):
        staged = _by_title(
            apply_quality_gate(
                _mixed(),
                confidence_floor=0.6,
                max_errors=1,
                max_warning_findings=5,
                max_outofscope_findings=5,
            )
        )
        assert staged["e1"].drop_reason is None
        assert staged["e3"].drop_reason == "error cap exceeded (max 1)"
        assert staged["e2"].drop_reason == "error cap exceeded (max 1)"

    def test_the_error_cap_ignores_the_new_caps(self):
        base = apply_quality_gate(_mixed(), confidence_floor=0.6, max_errors=2)
        capped = apply_quality_gate(
            _mixed(), confidence_floor=0.6, max_errors=2,
            max_warning_findings=0, max_outofscope_findings=0,
        )
        errors = [(f.title, f.drop_reason) for f in base if f.severity == "error"]
        assert errors == [
            (f.title, f.drop_reason) for f in capped if f.severity == "error"
        ]


class TestSpecNeverCapped:
    def test_spec_survives_every_cap_at_zero(self):
        specs = [
            _f(file="src/s.py", line=i, severity="spec", confidence=0.9, title=f"s{i}")
            for i in range(1, 21)
        ]
        staged = apply_quality_gate(
            specs,
            confidence_floor=0.6,
            max_errors=0,
            max_warning_findings=0,
            max_outofscope_findings=0,
        )
        assert len(active(staged)) == 20

    def test_spec_takes_no_slot_from_a_capped_severity(self):
        findings = [
            _f(file="src/a.py", line=1, severity="spec", confidence=0.99, title="s"),
            _f(file="src/a.py", line=2, severity="warning", confidence=0.7, title="w"),
            _f(file="src/a.py", line=3, severity="outofscope", confidence=0.7, title="o"),
        ]
        staged = apply_quality_gate(
            findings,
            confidence_floor=0.6,
            max_warning_findings=1,
            max_outofscope_findings=1,
        )
        assert len(active(staged)) == 3


class TestDefaultsAreIdentical:
    def test_omitting_the_caps_equals_passing_none(self):
        omitted = apply_quality_gate(_mixed(), confidence_floor=0.6, max_errors=2)
        explicit = apply_quality_gate(
            _mixed(),
            confidence_floor=0.6,
            max_errors=2,
            max_warning_findings=None,
            max_outofscope_findings=None,
        )
        assert omitted == explicit
        assert repr(omitted) == repr(explicit)

    def test_defaults_leave_every_warning_and_outofscope_active(self):
        staged = _by_title(apply_quality_gate(_mixed(), confidence_floor=0.6))
        for title in ("w1", "w2", "o1", "o2"):
            assert staged[title].drop_reason is None


class TestFailOnInteraction:
    """Documented: under ``PRXREF_FAIL_ON=any`` a cap of 0 can turn 1 into 0."""

    def test_a_zero_cap_narrows_fail_on_any(self):
        warnings = _tied("warning")
        uncapped = apply_quality_gate(warnings, confidence_floor=0.6)
        capped = apply_quality_gate(
            warnings, confidence_floor=0.6, max_warning_findings=0
        )
        assert _fail_on_exit({"findings_active": active(uncapped)}, "any")[0] == 1
        assert _fail_on_exit({"findings_active": active(capped)}, "any") == (0, None)

    def test_the_docstring_names_the_interaction(self):
        assert "PRXREF_FAIL_ON" in (apply_quality_gate.__doc__ or "")
