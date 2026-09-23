"""The one table of finding glyphs every rendering surface draws from.

A severity maps to exactly one glyph, and that glyph is the same in the
summary counts line, the summary findings list, an inline comment header and
the CLI. A finding outside the ticket's scope keeps its severity glyph and
gains the out-of-ticket prefix in front of it; that prefix is never a severity
glyph itself, so scope and severity stay readable independently.

The summary templates (``prompts/summary.md`` and the two fallback templates)
keep their glyphs as literals because they are the readable source of the
layout; a parity test holds each of them to this table. Changing a glyph or
adding a severity is one edit here.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from .triage import SCOPE_OUT

SEVERITY_MARKERS: Mapping[str, str] = MappingProxyType({
    "error": "🟥",
    "warning": "🟧",
    "spec": "🔍",
    "outofscope": "⬜",
})

# An unrecognised severity renders as the minor class, matching the way the
# library formatter folds an unknown severity into ``outofscope``.
FALLBACK_MARKER: str = SEVERITY_MARKERS["outofscope"]

OUT_OF_TICKET_MARKER: str = "🟦"

SCOPE_LABELS: Mapping[str, str] = MappingProxyType({SCOPE_OUT: "OUTSIDE TICKET"})


def severity_marker(severity: str) -> str:
    """Return the glyph for ``severity``, or :data:`FALLBACK_MARKER` if unknown.

    The lookup is exact: severities are normalised by the quality gate before
    anything renders them.
    """
    return SEVERITY_MARKERS.get(severity, FALLBACK_MARKER)


def marker_for(severity: str, scope: str) -> str:
    """Return the full marker for a finding: its severity glyph, prefixed by
    :data:`OUT_OF_TICKET_MARKER` and a space when ``scope`` is ``"out"``.

    Scope ``"in"`` and ``"unknown"`` add nothing, so a run without a ticket
    renders exactly the severity glyph.
    """
    marker = severity_marker(severity)
    return f"{OUT_OF_TICKET_MARKER} {marker}" if scope == SCOPE_OUT else marker
