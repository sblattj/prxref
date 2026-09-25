"""Cross-chunk and diff-file definitions for one worker chunk.

``chunk_context.referenced_definitions`` shows a worker the definition of a
name its added lines reference only when that definition sits in the SAME
changed file, and it knows no Java. When a PR changes a type in one chunk and
another chunk calls it, the calling chunk's worker never sees the new
invariant. :func:`diff_definitions` closes that gap for the ``diff`` level of
``PRXREF_REPO_CONTEXT``: it searches every file of the PR, not just the
chunk's own, and for a type changed in another chunk it also shows the changed
lines inside that type.

File text comes from the ``read(path) -> str | None`` callable at the PR head.
With no reader, or when a read returns ``None``, the only text known is the
file's hunks (its context and ``+`` lines), and entries are built from those
alone, so a forge without file reads still gets hunk-based entries. The module
is pure: stdlib plus :mod:`prxref.repo_context` and :mod:`prxref.chunk_context`,
with no I/O except through ``read``. It applies no budget; per-entry caps are
the only limits here.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from . import chunk_context
from .repo_context import (
    REASONS,
    ContextEntry,
    definition_regexes,
    find_definitions,
    language_of,
    referenced_names,
)

MAX_CHANGE_LINES = 12

_SAME_FILE_LANGUAGES = frozenset({"js", "python"})


def _diff_lines(f: object) -> list[object]:
    return [
        line
        for hunk in getattr(f, "hunks", None) or []
        for line in getattr(hunk, "lines", None) or []
    ]


def _hunk_text(f: object) -> dict[int, str]:
    known: dict[int, str] = {}
    for line in _diff_lines(f):
        new_line = getattr(line, "new_line", None)
        if getattr(line, "kind", " ") in (" ", "+") and isinstance(new_line, int):
            known.setdefault(new_line, getattr(line, "text", ""))
    return known


def _added_text(f: object) -> dict[int, str]:
    added: dict[int, str] = {}
    for line in _diff_lines(f):
        new_line = getattr(line, "new_line", None)
        if getattr(line, "kind", " ") == "+" and isinstance(new_line, int):
            added.setdefault(new_line, getattr(line, "text", ""))
    return added


def _runs(numbers: Sequence[int]) -> list[list[int]]:
    runs: list[list[int]] = []
    for number in sorted(numbers):
        if runs and number == runs[-1][-1] + 1:
            runs[-1].append(number)
        else:
            runs.append([number])
    return runs


def _hunk_definitions(
    known: dict[int, str],
    names: Sequence[str],
    language: str,
    skip: frozenset[int],
) -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    seen: set[str] = set()
    for run in _runs(list(known)):
        wanted = [name for name in names if name not in seen]
        if not wanted:
            break
        offset = run[0] - 1
        local_skip = frozenset(number - offset for number in run if number in skip)
        text = "\n".join(known[number] for number in run)
        for symbol, line, body in find_definitions(
            text, wanted, language=language, skip_lines=local_skip
        ):
            seen.add(symbol)
            found.append((symbol, line + offset, body))
    return found


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip())


def _window_end(known: dict[int, str], line: int, language: str) -> int | None:
    regexes = definition_regexes(language)
    depth = _indent(known.get(line, ""))
    for number in sorted(known):
        if number <= line:
            continue
        text = known[number]
        if _indent(text) <= depth and any(regex.match(text) for regex in regexes):
            return number
    return None


def _change_text(lines: Sequence[str]) -> str:
    shown = [text.rstrip() for text in lines[:MAX_CHANGE_LINES]]
    hidden = len(lines) - len(shown)
    if hidden > 0:
        shown.append(f"… {hidden} more changed lines")
    return "\n".join(shown)


def _change_entries(
    path: str,
    symbol: str,
    line: int,
    end: int | None,
    added: dict[int, str],
) -> list[ContextEntry]:
    inside = [n for n in added if n > line and (end is None or n < end)]
    return [
        ContextEntry(
            path=path,
            line=run[0],
            symbol=symbol,
            kind="definition",
            reason="cross-chunk",
            text=_change_text([added[n] for n in run]),
        )
        for run in _runs(inside)
    ]


def diff_definitions(
    chunk: Sequence[object],
    all_files: Sequence[object],
    read: Callable[[str], str | None] | None,
) -> list[ContextEntry]:
    """Definitions from the PR's diff files for the names one chunk's added lines reference.

    ``chunk`` holds the worker's ``triage.FileDiff`` records and ``all_files``
    the whole PR's, both duck-typed on ``path``, ``status`` and ``hunks``
    (whose ``lines`` carry ``kind``, ``text`` and ``new_line``). The wanted
    names are the union, in first-appearance order, of
    :func:`~prxref.repo_context.referenced_names` over each chunk file's added
    lines in that file's language.

    Every file in ``all_files`` with definition regexes is searched with
    :func:`~prxref.repo_context.find_definitions`, reading its text with
    ``read`` or, when ``read`` is ``None`` or returns ``None``, taking it from
    the file's hunks: each contiguous run of known new-file lines is scanned
    on its own, so an entry carries true line numbers and never shows a line
    the hunks lack. A removed file is skipped, and so are the lines of this
    chunk's own hunks. A js or python file in the chunk is skipped entirely
    when ``read`` is given, because ``referenced_definitions`` already covers
    it; a Java file in the chunk is searched outside its hunks.

    A hit in a file outside the chunk that has ``+`` lines anywhere in the PR
    has reason ``"cross-chunk"``, and every other hit ``"diff-file"``. A
    cross-chunk hit also yields one entry per contiguous run of ``+`` lines
    after the definition line and before the next definition that is not
    indented deeper than it (or the end of the known text): same symbol and
    reason, ``line`` the run's first new-file line, and the run's text capped
    at :data:`MAX_CHANGE_LINES` lines plus a ``… N more changed lines`` line.
    Every entry's kind is ``"definition"``.

    Entries are ordered by ``(reason rank in REASONS, path, line)`` and
    deduplicated on ``(path, line)``, the first kept. No budget is applied.
    """
    own_files = chunk_context.chunk_files(chunk)
    own = {entry.path: entry for entry in reversed(own_files)}
    names: dict[str, None] = {}
    for entry in own_files:
        for name in referenced_names(entry.added, language_of(entry.path)):
            names.setdefault(name, None)
    if not names:
        return []
    wanted = list(names)
    changed = {
        getattr(f, "path", "")
        for f in all_files
        if any(getattr(line, "kind", " ") == "+" for line in _diff_lines(f))
    }

    collected: list[ContextEntry] = []
    searched: set[str] = set()
    for f in all_files:
        path = getattr(f, "path", "") or ""
        if not path or path in searched:
            continue
        searched.add(path)
        if getattr(f, "status", "") == "removed":
            continue
        language = language_of(path)
        if not definition_regexes(language):
            continue
        mine = own.get(path)
        if mine is not None and read is not None and language in _SAME_FILE_LANGUAGES:
            continue
        skip = mine.hunk_lines if mine is not None else frozenset()
        text = read(path) if read is not None else None
        if isinstance(text, str):
            known = dict(enumerate(text.splitlines(), start=1))
            found = find_definitions(text, wanted, language=language, skip_lines=skip)
        else:
            known = _hunk_text(f)
            found = _hunk_definitions(known, wanted, language, skip)
        reason = "cross-chunk" if mine is None and path in changed else "diff-file"
        for symbol, line, body in found:
            collected.append(ContextEntry(path, line, symbol, "definition", reason, body))
        if reason == "cross-chunk":
            added = _added_text(f)
            for symbol, line, _ in found:
                end = _window_end(known, line, language)
                collected.extend(_change_entries(path, symbol, line, end, added))

    collected.sort(key=lambda e: (REASONS.index(e.reason), e.path, e.line))
    out: list[ContextEntry] = []
    kept: set[tuple[str, int]] = set()
    for entry in collected:
        key = (entry.path, entry.line)
        if key not in kept:
            kept.add(key)
            out.append(entry)
    return out
