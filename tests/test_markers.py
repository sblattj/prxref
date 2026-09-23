"""Tests for prxref.markers (the one glyph table) and the ticket-scope
vocabulary in prxref.triage that the out-of-ticket marker keys on.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from prxref import markers
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
