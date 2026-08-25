"""Tests for prxref.formatter: inline comments, summary, attribution."""
from __future__ import annotations

from prxref.formatter import (
    _DEFAULT_SUMMARY_TEMPLATE,
    build_attribution,
    format_inline_comment,
    format_summary,
)
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


def _summary(**overrides):
    kwargs = {
        "verdict": "request_changes",
        "findings_active": [_f()],
        "findings_dropped": [
            _f(
                file="src/old.py",
                line=3,
                title="Already discussed",
                drop_reason="duplicate of existing thread",
            )
        ],
        "chunk_count": 4,
        "elapsed_ms": 2500,
        "input_tokens": 12000,
        "output_tokens": 340,
        "model": "test-model",
    }
    kwargs.update(overrides)
    return format_summary(**kwargs)


class TestBuildAttribution:
    def test_exact_string(self):
        assert (
            build_attribution("m1", 2500, 1234)
            == "Reviewed by prxref · model=m1 · 1234 tok · 2.5s"
        )

    def test_integer_seconds_trim_trailing_zero(self):
        assert build_attribution("m1", 4000, 10).endswith("4s")

    def test_sub_second(self):
        assert build_attribution("m1", 250, 10).endswith("0.2s")


class TestFormatInlineComment:
    def test_error_marker_bold_title_body_attribution(self):
        text = format_inline_comment(
            _f(severity="error", title="T", body="B"), "attr-here"
        )
        assert text == "🟥 **T**\n\nB\n\n*attr-here*"

    def test_warning_and_note_markers(self):
        assert format_inline_comment(_f(severity="warning"), "a").startswith("🟧 **")
        assert format_inline_comment(_f(severity="note"), "a").startswith("🟦 **")

    def test_unknown_severity_defaults_to_note(self):
        assert format_inline_comment(_f(severity=""), "a").startswith("🟦 **")

    def test_pipe_in_title_is_not_part_of_inline_output_structure(self):
        text = format_inline_comment(_f(title="a|b", body="x|y"), "a")
        assert "a|b" in text and "x|y" in text


class TestFormatSummaryVerdict:
    def test_request_changes_banner(self):
        assert "🛑 Request-Changes" in _summary()

    def test_approved_banner(self):
        assert "✅ Approved" in _summary(verdict="approved")

    def test_unknown_verdict_defaults_to_request_changes(self):
        assert "🛑 Request-Changes" in _summary(verdict="bogus")


class TestFormatSummaryCounts:
    def test_severity_counts_present(self):
        text = _summary(
            findings_active=[
                _f(severity="error", file="a.py"),
                _f(severity="error", file="b.py"),
                _f(severity="note"),
            ]
        )
        assert "🟥 2 error" in text
        assert "🟧 0 warning" in text
        assert "🟦 1 note" in text

    def test_active_of_total_counts(self):
        assert "1 active of 2 raw" in _summary()

    def test_empty_findings_approved_zero_counts(self):
        text = _summary(
            verdict="approved",
            findings_active=[],
            findings_dropped=[],
        )
        assert "✅ Approved" in text
        assert "🟥 0 error · 🟧 0 warning · 🟦 0 note" in text
        assert "0 active of 0 raw" in text
        assert "No findings survived the quality passes." in text


class TestFormatSummaryTables:
    def test_table_rows_sorted_error_first(self):
        text = _summary(
            findings_active=[
                _f(severity="note", file="z.py", line=1),
                _f(severity="error", file="z.py", line=2, title="Boom"),
            ]
        )
        header = "| Severity | Location | Title |"
        assert header in text
        assert text.index("z.py:2") < text.index("z.py:1")
        assert "| 🟥 | z.py:2 | Boom |" in text

    def test_file_level_finding_omits_line_zero(self):
        text = _summary(findings_active=[_f(line=0)])
        assert "src/app.py:" not in text
        assert "| src/app.py |" in text

    def test_pipe_in_title_escaped_in_table(self):
        text = _summary(findings_active=[_f(title="a|b")])
        assert "a\\|b" in text


class TestFormatSummaryDropped:
    def test_dropped_disclosure_with_tally(self):
        text = _summary()
        assert "<details>" in text
        assert "Dropped findings: 1" in text
        assert "1 × duplicate of existing thread" in text

    def test_tally_groups_repeated_reasons(self):
        text = _summary(
            findings_dropped=[
                _f(title="d1", drop_reason="low confidence"),
                _f(title="d2", drop_reason="low confidence"),
                _f(title="d3", drop_reason="other"),
            ]
        )
        assert "2 × low confidence" in text
        assert "1 × other" in text

    def test_no_dropped_section_when_none(self):
        text = _summary(findings_dropped=[])
        assert "Dropped findings" not in text


class TestFormatSummaryCost:
    def test_cost_line(self):
        text = _summary()
        assert (
            "*chunks 4 · 12000 in / 340 out tokens · 2.5s · model test-model*"
            in text
        )

    def test_attribution_line(self):
        text = _summary()
        assert (
            "*Reviewed by prxref · model=test-model · 12340 tok · 2.5s*" in text
        )


class TestTemplateFallback:
    def test_template_with_unknown_placeholder_falls_back(self, monkeypatch, tmp_path):
        import prxref.formatter as fmt

        bad = tmp_path / "summary.md"
        bad.write_text("## {verdict_banner} {nope}", encoding="utf-8")
        monkeypatch.setattr(fmt, "_reviewer_load_prompt", None)
        monkeypatch.setattr(fmt.Path, "resolve", lambda self: tmp_path / "x.py")

        text = _summary()
        assert "🛑 Request-Changes" in text
        assert "{nope}" not in text

    def test_default_template_placeholders_all_filled(self):
        text = _summary()
        for key in (
            "verdict_banner",
            "error_count",
            "findings_table",
            "dropped_section",
            "attribution",
        ):
            assert "{" + key + "}" not in text
        assert _DEFAULT_SUMMARY_TEMPLATE  # inline fallback stays non-empty
