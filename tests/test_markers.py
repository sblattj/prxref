"""Tests for prxref.markers (the one glyph table) and the ticket-scope
vocabulary in prxref.triage that the out-of-ticket marker keys on.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

from prxref import formatter, markers, orchestrator, reviewer
from prxref.markers import (
    FALLBACK_MARKER,
    OUT_OF_TICKET_MARKER,
    SCOPE_LABELS,
    SEVERITY_MARKERS,
    marker_for,
    severity_marker,
)
from prxref.quality import SEVERITIES
from prxref.triage import SCOPE_IN, SCOPE_OUT, SCOPE_UNKNOWN, SCOPES, normalize_scope


class TestSeverityTable:
    def test_the_table_is_owner_decision_2(self):
        assert list(SEVERITY_MARKERS.items()) == [
            ("error", "🟥"),
            ("warning", "🟧"),
            ("spec", "🔍"),
            ("outofscope", "⬜"),
        ]

    def test_every_severity_the_gate_accepts_has_a_glyph(self):
        assert set(SEVERITY_MARKERS) == set(SEVERITIES)

    def test_glyphs_are_distinct(self):
        assert len(set(SEVERITY_MARKERS.values())) == len(SEVERITY_MARKERS)

    def test_the_out_of_ticket_marker_is_never_a_severity_glyph(self):
        assert OUT_OF_TICKET_MARKER == "🟦"
        assert OUT_OF_TICKET_MARKER not in SEVERITY_MARKERS.values()

    def test_an_unknown_severity_renders_as_the_minor_class(self):
        assert FALLBACK_MARKER == "⬜"
        assert FALLBACK_MARKER == SEVERITY_MARKERS["outofscope"]

    def test_the_tables_are_read_only(self):
        with pytest.raises(TypeError):
            SEVERITY_MARKERS["error"] = "X"  # type: ignore[index]
        with pytest.raises(TypeError):
            SCOPE_LABELS[SCOPE_OUT] = "X"  # type: ignore[index]

    def test_scope_labels(self):
        assert dict(SCOPE_LABELS) == {"out": "OUTSIDE TICKET"}


class TestSeverityMarker:
    @pytest.mark.parametrize(("severity", "glyph"), list(SEVERITY_MARKERS.items()))
    def test_known_severities(self, severity, glyph):
        assert severity_marker(severity) == glyph

    @pytest.mark.parametrize("severity", ["blocker", "", "ERROR", "minor"])
    def test_anything_else_falls_back(self, severity):
        assert severity_marker(severity) == FALLBACK_MARKER


_D64_TABLE = [
    ("error", "🟥"),
    ("warning", "🟧"),
    ("spec", "🔍"),
    ("outofscope", "⬜"),
    ("blocker", "⬜"),
]


class TestMarkerFor:
    """Design #64 §10.1: the scope prefix goes in front of the severity glyph."""

    @pytest.mark.parametrize(("severity", "glyph"), _D64_TABLE)
    def test_in_and_unknown_scope_add_nothing(self, severity, glyph):
        assert marker_for(severity, SCOPE_IN) == glyph
        assert marker_for(severity, SCOPE_UNKNOWN) == glyph

    @pytest.mark.parametrize(("severity", "glyph"), _D64_TABLE)
    def test_out_of_ticket_prefixes_the_severity_glyph(self, severity, glyph):
        assert marker_for(severity, SCOPE_OUT) == f"🟦 {glyph}"

    def test_scope_is_matched_exactly(self):
        assert marker_for("error", "OUT") == "🟥"


class TestScopeVocabulary:
    def test_constants(self):
        assert (SCOPE_IN, SCOPE_OUT, SCOPE_UNKNOWN) == ("in", "out", "unknown")
        assert SCOPES == ("in", "out", "unknown")
        assert isinstance(SCOPES, tuple)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("in", "in"),
            ("out", "out"),
            ("unknown", "unknown"),
            ("  OUT \n", "out"),
            ("In", "in"),
            ("UNKNOWN", "unknown"),
        ],
    )
    def test_the_three_words_survive_case_and_whitespace(self, raw, expected):
        assert normalize_scope(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["In scope", "yes", "no", "outside", "in-scope", "", "   ", None, True, False, 1, 0, ["out"], {"scope": "out"}],
    )
    def test_everything_else_is_unknown(self, raw):
        assert normalize_scope(raw) == SCOPE_UNKNOWN


class TestModuleIsALeaf:
    def test_markers_imports_only_the_standard_library_and_triage(self):
        tree = ast.parse(Path(markers.__file__).read_text(encoding="utf-8"))
        stdlib: set[str] = set()
        package: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                stdlib.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    package.add(node.module or "")
                else:
                    stdlib.add((node.module or "").split(".")[0])
        assert stdlib <= set(sys.stdlib_module_names) | {"__future__"}, stdlib
        assert package == {"triage"}


_COUNT_RE = re.compile(r"(\S+) \{(error|warning|spec|outofscope)_count\}")

_SUMMARY_TEMPLATES = {
    "prompts/summary.md": lambda: reviewer.load_prompt("summary"),
    "orchestrator._FALLBACK_SUMMARY_TEMPLATE": lambda: orchestrator._FALLBACK_SUMMARY_TEMPLATE,
    "formatter._DEFAULT_SUMMARY_TEMPLATE": lambda: formatter._DEFAULT_SUMMARY_TEMPLATE,
}


class TestSummaryTemplateParity:
    """The three summary templates spell no glyph (#59): each counts line
    draws every glyph from the table through its marker slot, so a template
    cannot drift from the table, default or operator-configured."""

    @pytest.mark.parametrize("name", list(_SUMMARY_TEMPLATES))
    def test_every_count_is_led_by_its_own_marker_slot(self, name):
        pairs = _COUNT_RE.findall(_SUMMARY_TEMPLATES[name]())
        assert sorted(sev for _lead, sev in pairs) == sorted(SEVERITY_MARKERS), pairs
        for lead, sev in pairs:
            assert lead == "{" + markers.MARKER_SLOTS[sev] + "}", (name, sev, lead)

    @pytest.mark.parametrize("name", list(_SUMMARY_TEMPLATES))
    def test_no_template_spells_a_glyph(self, name):
        text = _SUMMARY_TEMPLATES[name]()
        assert [g for g in _GLYPHS if g in text] == [], name

    @pytest.mark.parametrize("name", list(_SUMMARY_TEMPLATES))
    def test_minor_is_the_outofscope_slot_on_every_run(self, name):
        assert "{outofscope_marker} {outofscope_count} outofscope" in _SUMMARY_TEMPLATES[name]()


_PACKAGE = Path(markers.__file__).resolve().parent
_GLYPHS = frozenset(SEVERITY_MARKERS.values()) | {OUT_OF_TICKET_MARKER}


def _docstring_ids(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _glyph_literals(path: Path) -> list[tuple[int, list[str]]]:
    """Every non-docstring string constant (f-string parts included) in
    ``path`` that contains a marker glyph, as ``(lineno, glyphs)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _docstring_ids(tree)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            found = sorted(g for g in _GLYPHS if g in node.value)
            if found:
                hits.append((node.lineno, found))
    return hits


class TestGlyphsLiveInOnePlace:
    """Design #64 §9.2 and contract §6.2's end state, tightened by #59: code
    renders a glyph from the table and never re-types it, with no exemption
    for the summary templates (docstrings are documentation and are not
    scanned)."""

    def test_no_module_but_markers_spells_a_glyph(self):
        offenders = {
            path.relative_to(_PACKAGE).as_posix(): hits
            for path in sorted(_PACKAGE.rglob("*.py"))
            if path.name != "markers.py" and (hits := _glyph_literals(path))
        }
        assert offenders == {}

    def test_the_scan_sees_glyphs_where_they_are(self, tmp_path):
        assert _glyph_literals(_PACKAGE / "markers.py")
        spelled = tmp_path / "spelled.py"
        spelled.write_text('"""🟥 in a docstring."""\nX = f"{1} 🟧"\n', encoding="utf-8")
        assert _glyph_literals(spelled) == [(2, ["🟧"])]

    def test_the_out_of_ticket_glyph_appears_only_in_markers(self):
        holders = sorted(
            path.relative_to(_PACKAGE).as_posix()
            for path in _PACKAGE.rglob("*")
            if path.is_file() and path.suffix in {".py", ".md"}
            and OUT_OF_TICKET_MARKER in path.read_text(encoding="utf-8")
        )
        assert holders == ["markers.py"]
