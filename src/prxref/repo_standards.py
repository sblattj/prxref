"""In-repo standards documents as chunk context (#68).

A repository often carries its own rules: a web-security standard that pins
HSTS lifetimes, an ADR that settled how deployments are named, a CONTRIBUTING
section on error handling. A worker that contradicts one of them in a changed
line is wrong even when the diff itself is coherent, and the diff alone
cannot show it. This module slices those documents into Markdown sections
and picks the ones a chunk's own changes point at, so the worker can check
the change against the standard and cite it.

The flow mirrors :mod:`prxref.repo_contracts`: the orchestrator selects the
standards files once per run with the same generic glob selectors
(:func:`prxref.repo_contracts.select_contract_files` and
:func:`prxref.repo_contracts.literal_contract_paths`), and
:func:`standards_entries` turns one chunk into entries of kind
``"standards"`` and reason ``"standard"`` — :class:`prxref.repo_context.ContextEntry`
records whose ``text`` is one capped Markdown section.

What matches is what the chunk itself names. :func:`standards_triggers`
folds, through :func:`prxref.repo_contracts.normalize_name`, the path atoms
of every changed file (basename, stem, directory segments, extension), the
routes, tables, names and operation ids
:func:`prxref.repo_contracts.contract_triggers` finds on the added lines,
and the quoted string literals of those lines — a ``"max-age=15552000"``
inside ``add_header(...)`` is what points at the HSTS section, and the key
half of a ``key=value`` literal counts on its own, because the value is
exactly what the diff gets to disagree with. A trigger folded shorter than
:data:`MIN_TRIGGER_CHARS` matches nothing (``go``, ``py``, ``rs`` would
match almost any prose). A section qualifies when at least one trigger
appears in it; a heading hit outranks any number of body hits, and the rest
of the rank is ``(path, line)``, so the choice is deterministic. Sections
are admitted in that order while the sum of their rendered lengths stays
within ``max_chars``; admission stops at the first section that does not
fit, and a leftover count closes the last admitted entry.

When two admitted sections state different values for the same
``key=value``-shaped setting (two ``max-age=`` lifetimes, ``DENY`` against
``SAMEORIGIN``), the first entry carries a ``[note]`` line saying so, because
the worker must report the disagreement rather than silently pick a side.

A section of an ADR whose status is Superseded or Rejected is still
admitted — it is context the repository chose to keep — but its heading line
is annotated ``(status: Superseded)``, in the entry's text and its symbol,
so a worker cannot quote it as the current rule.

The module is pure. It is stdlib plus the pure :mod:`prxref.repo_contracts`
and :mod:`prxref.chunk_context`, and it performs no I/O: files are read only
through the ``read`` callable it is handed, a read that fails or is refused
gives None and the file is skipped silently. Everything degrades to ``[]``
rather than raising, because a review must never fail over missing context.
"""
from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from . import chunk_context
from .repo_context import ContextEntry
from .repo_contracts import contract_triggers, normalize_name

#: Characters one section's text may carry, before the ``path:line:`` prefix.
MAX_STANDARDS_SECTION_CHARS = 1600

#: A folded trigger shorter than this matches nothing: two- and three-letter
#: path atoms (``go``, ``py``, ``rs``, ``ts``) would match almost any prose.
MIN_TRIGGER_CHARS = 4

#: Standards files one chunk reads, at most. Priority (literal-glob) paths
#: first, then the rest in path order, mirroring the spec-file cap of
#: :mod:`prxref.repo_contracts` so a large ADR directory cannot spend a
#: chunk's whole read allowance.
MAX_STANDARDS_FILES = 6

#: Lines of a section (plus the file's front matter, for the first) the
#: Superseded/Rejected status is looked for in: the heading and the first
#: paragraph, by design — a status buried mid-document is not detected.
_STATUS_LINES = 8

_ATX_RE = re.compile(r" {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_STATUS_RE = re.compile(r"(?i)\bstatus\b\s*[:=]\s*[`*_\[\s]*(superseded|rejected)\b")
_QUOTED_RE = re.compile(r'"([^"\n]{4,})"|\'([^\'\n]{4,})\'')
_PAIR_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]{1,30})\s*[=:]\s*([A-Za-z0-9_.@/+-]{1,40})")


@dataclass(frozen=True)
class Section:
    """One heading-delimited slice of a standards document.

    ``line`` is the 1-based line of the heading, ``heading`` its text without
    the ``#`` markers (annotated with ``(status: ...)`` for a Superseded or
    Rejected ADR), and ``text`` the capped slice, heading line included.
    """

    heading: str
    line: int
    text: str
    path: str


def _front_matter(lines: list[str]) -> str:
    """The leading ``---``-fenced block, or ``""``; not parsed, only found."""
    if not lines or lines[0].strip() != "---":
        return ""
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            return "\n".join(lines[1:idx])
    return ""


def _cap_section(lines: list[str]) -> str:
    """The section text capped at :data:`MAX_STANDARDS_SECTION_CHARS` characters.

    A cut section keeps as many leading lines as fit and ends with an
    ``… N more lines`` line; a heading line too long to fit even alone is
    itself cut and ends with ``…``.
    """
    text = "\n".join(lines)
    if len(text) <= MAX_STANDARDS_SECTION_CHARS:
        return text
    for keep in range(len(lines) - 1, 0, -1):
        capped = "\n".join([*lines[:keep], f"… {len(lines) - keep} more lines"])
        if len(capped) <= MAX_STANDARDS_SECTION_CHARS:
            return capped
    return lines[0][: MAX_STANDARDS_SECTION_CHARS - 1] + "…"


def _status_of(lines: list[str], front: str) -> str:
    """``"Superseded"``/``"Rejected"`` when the heading or first paragraph states it, else ``""``."""
    scan = "\n".join([front, *lines[:_STATUS_LINES]]) if front else "\n".join(lines[:_STATUS_LINES])
    match = _STATUS_RE.search(scan)
    return match.group(1).capitalize() if match else ""


def _section(path: str, lines: list[str], start: int, heading: str, end: int, front: str) -> Section:
    slice_ = lines[start:end]
    status = _status_of(slice_, front)
    if status:
        slice_ = [f"{lines[start]} (status: {status})", *slice_[1:]]
        heading = f"{heading} (status: {status})"
    return Section(heading, start + 1, _cap_section(slice_), path)


def split_sections(path: str, text: str) -> list[Section]:
    """Slice ``text`` at ATX headings; a section runs to the next heading of its level or shallower.

    ``#{1,6}`` heading lines open sections; a heading of level ``N`` closes
    every open section of level ``N`` or greater, so a ``##`` section owns
    its ``###`` children — their lines are part of its text — and ends at
    the next ``##`` or ``#``, while the ``###`` child is a section of its
    own inside it. Text before the first heading (front matter included) is
    not a section; a leading front-matter block is only consulted for the
    first section's status. Fenced code blocks are skipped, so a
    ``# comment`` inside one opens nothing. Sections are returned in line
    order.
    """
    lines = text.splitlines()
    front = _front_matter(lines)
    sections: list[Section] = []
    fence = ""
    stack: list[tuple[int, int, str]] = []
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if fence:
            if stripped.startswith(fence):
                fence = ""
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            continue
        match = _ATX_RE.match(line)
        if match is None:
            continue
        level = len(match.group(1))
        while stack and stack[-1][1] >= level:
            start, _lvl, heading = stack.pop()
            sections.append(_section(path, lines, start, heading, idx, front if not sections else ""))
        stack.append((idx, level, match.group(2)))
    for start, _lvl, heading in reversed(stack):
        sections.append(_section(path, lines, start, heading, len(lines), front if not sections else ""))
    sections.sort(key=lambda section: section.line)
    return sections


def _path_atoms(path: str) -> list[str]:
    base = posixpath.basename(path)
    stem, suffix = posixpath.splitext(base)
    return [base, stem, suffix.lstrip("."), *path.split("/")]


def _quoted_literals(added: Iterable[str]) -> list[str]:
    out: list[str] = []
    for line in added:
        for match in _QUOTED_RE.finditer(line):
            out.extend(value for value in match.groups() if value)
    return out


def _fold_trigger(folded: dict[str, None], raw: str) -> None:
    value = normalize_name(raw.strip())
    if len(value) >= MIN_TRIGGER_CHARS:
        folded.setdefault(value)


def standards_triggers(chunk: Sequence[object]) -> tuple[str, ...]:
    """The folded names a chunk's own changes point at, in first-appearance order.

    For every changed file: its path atoms; the routes, tables, names (the
    :func:`prxref.repo_context.referenced_names` of its added lines, which
    ``contract_triggers`` already includes) and operation ids; and the
    double- or single-quoted string literals of at least 4 characters on its
    added lines, each whole and, when it is ``key=value``-shaped with both
    halves non-empty, its key half too. Every value is folded through
    :func:`prxref.repo_contracts.normalize_name`; one folded shorter than
    :data:`MIN_TRIGGER_CHARS` is dropped. Empty when the chunk names nothing.
    """
    folded: dict[str, None] = {}
    for changed in chunk_context.chunk_files(chunk):
        for atom in _path_atoms(changed.path):
            _fold_trigger(folded, atom)
        triggers = contract_triggers(changed.path, changed.added)
        for value in (*triggers.routes, *triggers.tables, *triggers.names, *triggers.operation_ids):
            _fold_trigger(folded, value)
        for literal in _quoted_literals(changed.added):
            _fold_trigger(folded, literal)
            key, sep, rest = literal.partition("=")
            if sep and key.strip() and rest.strip():
                _fold_trigger(folded, key)
    return tuple(folded)


def _hits(section: Section, triggers: Sequence[str]) -> tuple[int, int]:
    """``(heading_hits, body_hits)``: how many triggers the heading, and the whole section, contain."""
    heading = normalize_name(section.heading)
    body = normalize_name(section.text)
    return (
        sum(1 for trigger in triggers if trigger in heading),
        sum(1 for trigger in triggers if trigger in body),
    )


def _file_order(paths: Sequence[str], priority: Sequence[str]) -> list[str]:
    """``priority`` paths first (in their own order), then the rest by path, capped."""
    ranked = frozenset(priority)
    head = [path for path in priority if path in set(paths)]
    tail = sorted(path for path in paths if path not in ranked)
    return [*head, *tail][:MAX_STANDARDS_FILES]


def _disagreement(sections: Sequence[Section]) -> str | None:
    """The ``[note]`` line for the first setting two admitted sections state differently, or None.

    A ``key: value`` or ``key=value`` pair inside one section is that
    section's claim; the same folded key with a different value in a LATER
    section is the disagreement. Two values inside one section are one
    section showing options, not two standards disagreeing.
    """
    seen: dict[str, tuple[int, str, str]] = {}
    for index, section in enumerate(sections):
        for key, value in _PAIR_RE.findall(section.text):
            folded = normalize_name(key)
            first = seen.get(folded)
            if first is None:
                seen[folded] = (index, key, value)
            elif first[0] != index and value != first[2]:
                return (
                    f"[note] two standards in this repo disagree on {first[1]}: "
                    f"{first[2]} vs {value}"
                )
    return None


def standards_entries(
    chunk: Sequence[object],
    *,
    standards_paths: Sequence[str],
    read: Callable[[str], str | None] | None,
    priority: Sequence[str] = (),
    max_chars: int = 6000,
) -> list[ContextEntry]:
    """The standards entries for one worker chunk, in rank order.

    ``chunk`` is the chunk's file diffs, duck-typed as
    :func:`prxref.chunk_context.chunk_files` reads them.
    ``standards_paths`` is :func:`prxref.repo_contracts.select_contract_files`
    over the standards globs, ``priority`` its
    :func:`prxref.repo_contracts.literal_contract_paths`. ``read`` is the
    chunk's capped reader: at most :data:`MAX_STANDARDS_FILES` files are
    read, priority paths first, and a path the reader refuses or cannot read
    is skipped silently. ``read`` None, no paths, no trigger (the chunk
    names nothing any section could match) or a non-positive ``max_chars``
    give ``[]`` with no read at all.

    Each read file is sliced by :func:`split_sections`; a section qualifies
    when at least one of :func:`standards_triggers`' values appears in it,
    and candidates rank by ``(-heading hits, -body hits, path, line)``. They
    are admitted in that order while the sum of the rendered
    (``path:line: text``) lengths stays within ``max_chars``; admission stops
    at the first section that does not fit, and the last admitted entry then
    ends with an ``… N more standards sections omitted`` line. When two
    admitted sections disagree on one setting
    (:func:`_disagreement`), the first entry's text is preceded by the
    ``[note]`` line. Sections a path shares a ``(path, line)`` with keep the
    first; there is no other dedup here, and no read of a file the chunk
    itself changes.
    """
    if read is None or max_chars <= 0 or not standards_paths:
        return []
    triggers = standards_triggers(chunk)
    if not triggers:
        return []
    matched: list[tuple[int, int, Section]] = []
    for path in _file_order(standards_paths, priority):
        text = read(path)
        if not isinstance(text, str):
            continue
        for section in split_sections(path, text):
            heading_hits, body_hits = _hits(section, triggers)
            if heading_hits or body_hits:
                matched.append((heading_hits, body_hits, section))
    matched.sort(key=lambda item: (-item[0], -item[1], item[2].path, item[2].line))
    admitted: list[Section] = []
    used = 0
    for _heading_hits, _body_hits, section in matched:
        size = len(f"{section.path}:{section.line}: {section.text}")
        if used + size > max_chars:
            break
        admitted.append(section)
        used += size
    entries = [
        ContextEntry(
            path=section.path, line=section.line, symbol=section.heading,
            kind="standards", reason="standard", text=section.text,
        )
        for section in admitted
    ]
    if not entries:
        return []
    if len(admitted) < len(matched):
        last = admitted[-1]
        marker = f"… {len(matched) - len(admitted)} more standards sections omitted"
        entries[-1] = ContextEntry(
            last.path, last.line, last.heading, "standards", "standard",
            f"{last.text}\n{marker}",
        )
    note = _disagreement(admitted)
    if note:
        first = admitted[0]
        entries[0] = ContextEntry(
            first.path, first.line, first.heading, "standards", "standard",
            f"{note}\n{first.text}",
        )
    return entries
