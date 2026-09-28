"""The one table of finding glyphs every rendering surface draws from.

A severity maps to exactly one glyph, and that glyph is the same in the
summary counts line, the summary findings list, an inline comment header and
the CLI. A finding outside the ticket's scope keeps its severity glyph and
gains the out-of-ticket prefix in front of it; that prefix is never a severity
glyph itself, so scope and severity stay readable independently.

:data:`SEVERITY_MARKERS`, :data:`OUT_OF_TICKET_MARKER` and
:data:`FALLBACK_MARKER` are the immutable built-in defaults. An operator can
replace any of the five glyphs with ``PRXREF_SEVERITY_MARKERS`` (or the
``severity_markers`` config-file key, issue #59); the resulting *effective*
table is what every renderer reads:

- :func:`configure` installs the operator's overrides for the process (the
  CLI calls it once per loaded config); ``None`` or ``{}`` restores the
  defaults.
- :func:`active_severity_markers`, :func:`out_of_ticket_marker` and
  :func:`fallback_marker` read the effective table; :func:`severity_marker`,
  :func:`marker_for` and :func:`inline_header` go through them.
- :func:`marker_slots` fills the five summary-template slots named in
  :data:`MARKER_SLOTS` (``{error_marker}``, ``{warning_marker}``,
  ``{spec_marker}``, ``{outofscope_marker}``, ``{out_of_ticket_marker}``).
- :func:`overridden` is a context manager that installs overrides and
  restores the previous table on exit, for tests.
- :func:`parse_overrides` turns the ``name=glyph,...`` syntax (or a mapping)
  into a validated override dict, raising ``ValueError`` with an
  operator-facing message; config wraps it into a
  :class:`~prxref.llm.ConfigError` naming the input.

The summary templates (``prompts/summary.md`` and the two fallback templates)
spell no glyph: their counts line uses the slots, so a template cannot drift
from the table. Changing a default glyph or adding a severity is one edit
here.
"""
from __future__ import annotations

import difflib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from types import MappingProxyType

from .triage import SCOPE_OUT, Finding

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

#: The ``severity_markers`` name that overrides the out-of-ticket prefix.
OUT_OF_TICKET_NAME = "out_of_ticket"

#: Every name ``severity_markers`` accepts, in the order the docs list them.
MARKER_NAMES: tuple[str, ...] = (*SEVERITY_MARKERS, OUT_OF_TICKET_NAME)

#: Marker name to summary-template slot name (``error`` -> ``error_marker``).
MARKER_SLOTS: Mapping[str, str] = MappingProxyType(
    {name: f"{name}_marker" for name in MARKER_NAMES}
)

_DEFAULT_TABLE: Mapping[str, str] = MappingProxyType(
    {**SEVERITY_MARKERS, OUT_OF_TICKET_NAME: OUT_OF_TICKET_MARKER}
)
_active: Mapping[str, str] = _DEFAULT_TABLE


def _check_name(name: str) -> None:
    if name in MARKER_NAMES:
        return
    close = difflib.get_close_matches(name, MARKER_NAMES, n=1)
    hint = f"; did you mean {close[0]!r}?" if close else ""
    raise ValueError(
        f"unknown marker name {name!r}{hint} (known: {', '.join(MARKER_NAMES)})"
    )


def _check_glyph(name: str, glyph: object) -> None:
    if not isinstance(glyph, str):
        raise ValueError(f"marker {name!r} glyph must be a string, got {glyph!r}")
    if not glyph:
        raise ValueError(f"marker {name!r} has an empty glyph")
    if "," in glyph or any(ch.isspace() for ch in glyph):
        raise ValueError(
            f"marker {name!r} glyph {glyph!r} contains whitespace or a comma"
        )


def _merged(overrides: Mapping[str, str]) -> Mapping[str, str]:
    table = {**_DEFAULT_TABLE, **overrides}
    seen: dict[str, str] = {}
    for name in MARKER_NAMES:
        glyph = table[name]
        if glyph in seen:
            raise ValueError(
                f"markers {seen[glyph]!r} and {name!r} would both render "
                f"{glyph!r}; the five glyphs must be distinct"
            )
        seen[glyph] = name
    return MappingProxyType(table)


def parse_overrides(value: str | Mapping[str, str] | None) -> dict[str, str]:
    """Parse and validate a ``severity_markers`` value into ``{name: glyph}``.

    ``value`` is a comma-separated ``name=glyph`` pair list, with whitespace
    around pairs, names and glyphs stripped and empty pairs skipped; or a
    mapping from a library caller; ``None`` or an empty string means no
    override. Names are :data:`MARKER_NAMES`. Raises ``ValueError`` for an
    unknown name (with a did-you-mean hint), a pair without ``=``, an empty
    name or glyph, a name given twice, a glyph containing whitespace or a
    comma, and a merged table in which two of the five glyphs are equal.
    Only the names given come back; the rest keep their defaults.
    """
    if value is None:
        return {}
    result: dict[str, str] = {}
    if isinstance(value, Mapping):
        for name, glyph in value.items():
            _check_name(name)
            _check_glyph(name, glyph)
            result[name] = glyph
    elif isinstance(value, str):
        for pair in value.split(","):
            pair = pair.strip()
            if not pair:
                continue
            name, eq, glyph = pair.partition("=")
            if not eq:
                raise ValueError(f"pair {pair!r} has no '='; expected name=glyph")
            name, glyph = name.strip(), glyph.strip()
            if not name:
                raise ValueError(f"pair {pair!r} has an empty name; expected name=glyph")
            _check_name(name)
            if name in result:
                raise ValueError(f"marker {name!r} is given twice")
            _check_glyph(name, glyph)
            result[name] = glyph
    else:
        raise ValueError(f"must be name=glyph pairs, got {value!r}")
    _merged(result)
    return result


def configure(overrides: str | Mapping[str, str] | None) -> None:
    """Install ``overrides`` as the effective table for the whole process.

    ``overrides`` is anything :func:`parse_overrides` accepts; ``None``,
    ``""`` or ``{}`` restores the defaults. An invalid value raises
    ``ValueError`` and leaves the current table in place.
    """
    global _active
    _active = _merged(parse_overrides(overrides))


@contextmanager
def overridden(overrides: str | Mapping[str, str] | None) -> Iterator[None]:
    """Install ``overrides`` for the ``with`` block, then restore the table
    that was active before it, even on error."""
    global _active
    previous = _active
    configure(overrides)
    try:
        yield
    finally:
        _active = previous


def active_severity_markers() -> Mapping[str, str]:
    """The effective ``{severity: glyph}`` table, read-only."""
    return MappingProxyType({name: _active[name] for name in SEVERITY_MARKERS})


def out_of_ticket_marker() -> str:
    """The effective out-of-ticket prefix."""
    return _active[OUT_OF_TICKET_NAME]


def fallback_marker() -> str:
    """The effective glyph for an unrecognised severity: the ``outofscope`` one."""
    return _active["outofscope"]


def marker_slots() -> dict[str, str]:
    """``{slot: glyph}`` for every :data:`MARKER_SLOTS` slot, from the effective table."""
    return {slot: _active[name] for name, slot in MARKER_SLOTS.items()}


def severity_marker(severity: str) -> str:
    """Return the effective glyph for ``severity``, or :func:`fallback_marker`
    if unknown.

    The lookup is exact: severities are normalised by the quality gate before
    anything renders them.
    """
    if severity in SEVERITY_MARKERS:
        return _active[severity]
    return fallback_marker()


def marker_for(severity: str, scope: str) -> str:
    """Return the full marker for a finding: its severity glyph, prefixed by
    :func:`out_of_ticket_marker` and a space when ``scope`` is ``"out"``.

    Scope ``"in"`` and ``"unknown"`` add nothing, so a run without a ticket
    renders exactly the severity glyph.
    """
    marker = severity_marker(severity)
    return f"{out_of_ticket_marker()} {marker}" if scope == SCOPE_OUT else marker


def inline_header(f: Finding) -> str:
    """Return the first line of the pipeline's inline comment for ``f``.

    The shape is ``🤖 <marker> **[<SEVERITY>] <title>** (`<file>:<line>`)``,
    with the marker from :func:`marker_for` and ``:<line>`` omitted for a
    file-level finding. A finding outside the ticket also carries its
    :data:`SCOPE_LABELS` entry inside the brackets, as
    ``[WARNING · OUTSIDE TICKET]``; scope ``"in"`` and ``"unknown"`` render
    exactly the severity header.
    """
    label = f.severity.upper()
    scope_label = SCOPE_LABELS.get(f.scope)
    if scope_label:
        label = f"{label} · {scope_label}"
    loc = f"{f.file}:{f.line}" if f.line > 0 else f.file
    return f"🤖 {marker_for(f.severity, f.scope)} **[{label}] {f.title}** (`{loc}`)"
