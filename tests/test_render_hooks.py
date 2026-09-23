"""The summary render hooks: one-pass template filling, the ``{ticket_note}``
slot, the cost label as the attribution's last field, the size-advisory
prepend, and the minor / unknown-severity glyph (owner decision 2).

These call ``orchestrator._render_summary`` and ``_attribution`` directly
against the REAL packaged ``summary.md`` and against the in-code fallback
template, so no reviewer stub is involved.
"""
from __future__ import annotations

import pytest

from prxref import orchestrator
from prxref.forges.base import ATTRIBUTION_MARKER, PRData
from prxref.orchestrator import _attribution, _format_finding, _render_summary
from prxref.triage import Finding


def _pr(title: str = "Add widget") -> PRData:
    return PRData(
        title=title, description="", author="a", source_branch="s",
        target_branch="t", source_sha="1", target_sha="2", raw={},
    )


_FINDINGS = [
    Finding("a.py", 3, "error", 0.9, "Null deref", "b"),
    Finding("b.py", 0, "outofscope", 0.5, "Nit", "b"),
]

_ATTRIBUTION = "Reviewed by prxref · model=m · 150 tok · 1.2s"


def _render(pr: PRData | None = None, findings=_FINDINGS, **kwargs) -> str:
    return _render_summary(
        pr or _pr(), ["a.py", "b.py"], "Request-Changes", list(findings),
        "m", 100, 50, 1234, **kwargs,
    )


@pytest.fixture(params=["summary.md", "fallback"])
def template(request, monkeypatch):
    """Run the test once on the packaged template and once on the fallback."""
    if request.param == "fallback":
        def _boom(name):
            raise RuntimeError("no prompts")

        monkeypatch.setattr(orchestrator.reviewer, "load_prompt", _boom)
    return request.param


class TestDefaultsKeepTheLayout:
    def test_packaged_template_golden(self):
        assert _render() == (
            "## prxref automated review: Request-Changes\n\n"
            "PR: Add widget · files reviewed: 2\n\n"
            "🟥 1 error · 🟧 0 warning · 🔍 0 spec · ⬜ 1 outofscope\n"
            "\n"
            "- 🟥 `a.py:3` — Null deref\n"
            "- ⬜ `b.py:—` — Nit\n\n"
            "---\n\n"
            f"{_ATTRIBUTION}\n"
        )

    def test_fallback_template_golden(self, monkeypatch):
        def _boom(name):
            raise RuntimeError("no prompts")

        monkeypatch.setattr(orchestrator.reviewer, "load_prompt", _boom)
        assert _render() == (
            "🤖 **prxref review — Request-Changes**\n\n"
            "PR: Add widget\n\n"
            "Files reviewed: 2 · 🟥 1 error · 🟧 0 warning · 🔍 0 spec · "
            "⬜ 1 outofscope\n"
            "\n"
            "- 🟥 `a.py:3` — Null deref\n"
            "- ⬜ `b.py:—` — Nit\n\n"
            f"{_ATTRIBUTION}"
        )

    def test_empty_hook_values_equal_omitted_ones(self, template):
        assert _render(
            spec_note="", ticket_note="", cost_label="", size_advisory_line="",
        ) == _render()


class TestOnePassFill:
    """A value is never re-scanned for placeholders (reviewer.fill_template)."""

    _HOSTILE = "Fix {findings} {attribution} {diff} {spec_note} {ticket_note} {verdict}"

    def test_placeholders_in_the_pr_title_render_literally(self, template):
        out = _render(_pr(self._HOSTILE))
        assert f"PR: {self._HOSTILE}" in out
        assert out.count("Null deref") == 1
        assert out.count(ATTRIBUTION_MARKER) == 1

    def test_placeholders_in_a_note_render_literally(self, template):
        out = _render(spec_note="> {findings}\n", ticket_note="> {attribution}\n")
        assert "> {findings}\n> {attribution}\n" in out
        assert out.count("Null deref") == 1
        assert out.count(ATTRIBUTION_MARKER) == 1

    def test_placeholders_in_a_finding_title_render_literally(self, template):
        findings = [Finding("a.py", 3, "error", 0.9, "Leaks {attribution}", "b")]
        out = _render(findings=findings)
        assert "— Leaks {attribution}" in out
        assert out.count(ATTRIBUTION_MARKER) == 1


class TestTicketNote:
    def test_rides_after_the_spec_note_on_the_counts_line_s_next_line(self, template):
        out = _render(spec_note="> S\n", ticket_note="> T\n")
        assert "⬜ 1 outofscope\n> S\n> T\n\n- 🟥 `a.py:3`" in out

    def test_renders_without_a_spec_note(self, template):
        out = _render(ticket_note="> T\n")
        assert "⬜ 1 outofscope\n> T\n\n- 🟥 `a.py:3`" in out

    def test_packaged_template_has_the_slot(self):
        assert "{spec_note}{ticket_note}\n" in orchestrator.reviewer.load_prompt("summary")
        assert "{spec_note}{ticket_note}\n" in orchestrator._FALLBACK_SUMMARY_TEMPLATE


class TestCostLabel:
    def test_attribution_without_a_label_is_unchanged(self):
        assert _attribution("m", 150, 1234) == _ATTRIBUTION

    @pytest.mark.parametrize("label", ["$0.0007", "~$0.0007 (est.)", "cost unknown"])
    def test_label_is_the_last_field(self, label):
        assert _attribution("m", 150, 1234, cost_label=label) == f"{_ATTRIBUTION} · {label}"

    def test_label_is_keyword_only(self):
        with pytest.raises(TypeError):
            _attribution("m", 150, 1234, "$0.0007")  # type: ignore[misc]

    def test_summary_attribution_carries_the_label(self, template):
        out = _render(cost_label="~$0.0007 (est.)")
        assert f"{_ATTRIBUTION} · ~$0.0007 (est.)" in out
        assert out.rstrip("\n").endswith(" · ~$0.0007 (est.)")


class TestSizeAdvisory:
    _ADVISORY = "> ⚠️ Large PR: 900 changed lines across 40 files\n\n"
    _PARTIAL = {
        "chunks_reviewed": 1, "chunks_failed": 1,
        "failed_chunks": [("timeout", ["a.py"])],
    }

    def test_prepended_to_the_finished_body(self, template):
        plain = _render(**self._PARTIAL)
        out = _render(size_advisory_line=self._ADVISORY, **self._PARTIAL)
        assert out == self._ADVISORY + plain
        assert out.count("⚠️ Partial review") == 1
        assert out.index("⚠️ Partial review") > out.index(ATTRIBUTION_MARKER)

    def test_empty_advisory_leaves_the_heading_first(self, template):
        first = _render().splitlines()[0]
        assert first.startswith(("## prxref automated review", "🤖 **prxref review"))


class TestMinorAndFallbackGlyph:
    """Owner decision 2: minor is ⬜ on every run, and so is an unknown severity."""

    def test_summary_bullets(self, template):
        findings = [
            Finding("b.py", 4, "outofscope", 0.5, "Nit", "b"),
            Finding("c.py", 5, "blocker", 0.5, "Odd", "b"),
        ]
        out = _render(findings=findings)
        assert "- ⬜ `b.py:4` — Nit" in out
        assert "- ⬜ `c.py:5` — Odd" in out
        assert "🟦" not in out

    @pytest.mark.parametrize("severity", ["outofscope", "blocker", ""])
    def test_inline_header(self, severity):
        body = _format_finding(Finding("a.py", 1, severity, 0.5, "T", "b"), "m")
        assert body.startswith(f"🤖 ⬜ **[{severity.upper()}] T**")
