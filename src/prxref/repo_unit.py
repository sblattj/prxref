"""One worker chunk's repository context: sources, order, budget and exclusion (#17).

:func:`build_unit_context` combines the four repository-context sources for
one chunk into the lines its prompt carries:

1. :func:`prxref.repo_crosschunk.diff_definitions`: definitions from the PR's
   own diff files (``cross-chunk`` and ``diff-file``), at the ``diff`` and
   ``repo`` levels;
2. :func:`prxref.repo_contracts.contract_entries`: contract excerpts
   (``contract``), at the ``repo`` level only;
3. the resolver, :func:`prxref.repo_resolve.resolve_candidates` plus
   :func:`prxref.repo_context.find_definitions` over the candidate files
   (``import``, ``path-convention``, ``name-search``), at the ``repo`` level
   only;
4. :func:`prxref.repo_readers.reader_entries`: excerpts of code outside the
   diff that reads shared state the chunk's added lines write
   (``shared-state``, #22), at the ``repo`` level with a file listing only.

The sources are called in that order, which is their admission rank, so under
a capped reader the higher-ranked sources get their reads first. Every read
goes through one guard that refuses an excluded path without calling the
reader. The entries are then filtered by the exclude predicate, ordered by
``(REASONS rank, path, line)``, deduplicated on ``(path, line)`` and admitted
against one character budget, which stops at the first entry that does not
fit.

The module is pure: stdlib plus the pure repository-context modules and
:mod:`prxref.chunk_context`, with no I/O except through the ``read`` callable
the caller passes, nothing from :mod:`prxref.forges`, and no threads. The same
inputs and the same reader answers give the same result and the same read
order.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass

from . import repo_readers
from .chunk_context import chunk_files
from .repo_context import (
    REASONS,
    ContextEntry,
    definition_regexes,
    find_definitions,
    language_of,
    referenced_names,
)
from .repo_contracts import contract_entries
from .repo_crosschunk import diff_definitions
from .repo_resolve import resolve_candidates

MODES = ("off", "diff", "repo")


@dataclass(frozen=True)
class UnitContext:
    """The repository context admitted into one worker chunk's prompt.

    ``definition_lines``, ``contract_lines`` and ``reader_lines`` are the
    rendered admitted entries of kind ``definition``, ``contract`` and
    ``reader``, in admission order, ready for
    :func:`prxref.chunk_context.render_context_blocks` as ``extra_def_lines``,
    ``contract_lines`` and ``reader_lines``; when entries were left out, one
    omitted line (a horizontal ellipsis, then ``N more context entries
    omitted``) ends the list of the kind of the first entry left out.
    ``entries`` holds the admitted entries in admission order, and
    ``omitted`` counts the entries the budget left out. ``reader_lines`` comes
    last and defaults to empty, so a unit built without it is unchanged.
    """

    definition_lines: tuple[str, ...]
    contract_lines: tuple[str, ...]
    entries: tuple[ContextEntry, ...]
    omitted: int
    reader_lines: tuple[str, ...] = ()

    def record(self) -> dict:
        """The run-record row for this chunk: ``{"entries": [...], "omitted": N}``."""
        return {"entries": [entry.record() for entry in self.entries], "omitted": self.omitted}


EMPTY_UNIT = UnitContext((), (), (), 0)


def _excluded(exclude: Callable[[str], bool] | None, path: str) -> bool:
    if exclude is None:
        return False
    try:
        return bool(exclude(path))
    except Exception:  # noqa: BLE001
        return True


def _guard(
    read: Callable[[str], str | None] | None,
    exclude: Callable[[str], bool] | None,
) -> Callable[[str], str | None] | None:
    if read is None:
        return None

    def guarded(path: str) -> str | None:
        if _excluded(exclude, path):
            return None
        return read(path)

    return guarded


def _resolver_entries(
    chunk: Sequence[object],
    all_files: Sequence[object],
    read: Callable[[str], str | None],
    *,
    listing_paths: Collection[str] | None,
    listing_complete: bool,
    exclude: Callable[[str], bool] | None,
    found: Collection[str],
) -> list[ContextEntry]:
    known = set(found)
    removed = {getattr(f, "path", "") for f in chunk if getattr(f, "status", "") == "removed"}
    diff_paths = {getattr(f, "path", "") for f in all_files}
    out: list[ContextEntry] = []
    for changed in chunk_files(chunk):
        if changed.path in removed:
            continue
        language = language_of(changed.path)
        if not definition_regexes(language):
            continue
        names = [name for name in referenced_names(changed.added, language) if name not in known]
        if not names:
            continue
        text = read(changed.path)
        if not isinstance(text, str):
            text = "\n".join(changed.added)
        candidates = resolve_candidates(
            changed.path, text, names, listing=listing_paths, listing_complete=listing_complete
        )
        by_path: dict[str, dict[str, str]] = {}
        for candidate in candidates:
            by_path.setdefault(candidate.path, {}).setdefault(candidate.name, candidate.reason)
        for path, reasons in by_path.items():
            if path in diff_paths or _excluded(exclude, path):
                continue
            wanted = [name for name in reasons if name not in known]
            if not wanted:
                continue
            target = read(path)
            if not isinstance(target, str):
                continue
            for symbol, line, body in find_definitions(target, wanted, language=language_of(path)):
                out.append(ContextEntry(path, line, symbol, "definition", reasons[symbol], body))
                known.add(symbol)
    return out


def _merge(entries: list[ContextEntry], exclude: Callable[[str], bool] | None) -> list[ContextEntry]:
    kept = [entry for entry in entries if not _excluded(exclude, entry.path)]
    kept.sort(key=lambda entry: (REASONS.index(entry.reason), entry.path, entry.line))
    out: list[ContextEntry] = []
    seen: set[tuple[str, int]] = set()
    for entry in kept:
        key = (entry.path, entry.line)
        if key not in seen:
            seen.add(key)
            out.append(entry)
    return out


def _block(kind: str) -> str:
    return kind if kind in ("definition", "reader") else "contract"


def _admit(entries: list[ContextEntry], max_chars: int) -> UnitContext:
    admitted: list[ContextEntry] = []
    used = 0
    for entry in entries:
        size = len(entry.rendered())
        if used + size > max_chars:
            break
        admitted.append(entry)
        used += size
    omitted = len(entries) - len(admitted)
    lines: dict[str, list[str]] = {"definition": [], "contract": [], "reader": []}
    for entry in admitted:
        lines[_block(entry.kind)].append(entry.rendered())
    if omitted:
        marker = f"\N{HORIZONTAL ELLIPSIS} {omitted} more context entries omitted"
        lines[_block(entries[len(admitted)].kind)].append(marker)
    return UnitContext(
        tuple(lines["definition"]),
        tuple(lines["contract"]),
        tuple(admitted),
        omitted,
        tuple(lines["reader"]),
    )


def build_unit_context(
    chunk: Sequence[object],
    all_files: Sequence[object],
    *,
    mode: str,
    read: Callable[[str], str | None] | None,
    max_chars: int,
    listing_paths: Collection[str] | None = None,
    listing_complete: bool = False,
    contract_paths: Sequence[str] = (),
    contract_priority: Sequence[str] = (),
    exclude: Callable[[str], bool] | None = None,
) -> UnitContext:
    """The repository context for one worker chunk at level ``mode``.

    ``chunk`` holds the chunk's ``triage.FileDiff`` records and ``all_files``
    the whole PR's, duck-typed as :func:`prxref.repo_crosschunk.diff_definitions`
    reads them. ``mode`` is ``"off"``, ``"diff"`` or ``"repo"``; any other
    value raises ``ValueError``, and ``"off"`` returns :data:`EMPTY_UNIT`
    without calling anything. ``read`` is the chunk's capped reader, or None
    when there is none. ``listing_paths`` is the run's path listing as a set
    built once per run (or None), and ``listing_complete`` says it was not
    truncated. ``contract_paths`` and ``contract_priority`` are the run's
    :func:`prxref.repo_contracts.select_contract_files` and
    :func:`prxref.repo_contracts.literal_contract_paths` results.
    ``exclude(path)`` true marks a path repository context must never read or
    show, normally :func:`prxref.repo_context.exclude_predicate`; an
    ``exclude`` that raises counts as true.

    Every read goes through a guard that returns None for an excluded path
    without calling ``read``. The sources, called in this order:

    1. :func:`prxref.repo_crosschunk.diff_definitions` with the guarded
       reader, at both levels;
    2. at ``"repo"`` with a reader, :func:`prxref.repo_contracts.contract_entries`;
    3. at ``"repo"`` with a reader, the resolver. For each chunk file that is
       not removed and whose language has definition regexes, the names its
       added lines reference, less every symbol a definition entry at a
       non-excluded path has already found, go to
       :func:`prxref.repo_resolve.resolve_candidates` with the file's text
       (its added lines when the read gives None). The candidates' distinct
       paths are walked in first-appearance order; a path that is a PR diff
       file or excluded is skipped, and so is one whose candidate names are
       all found already, with no read. Each other path is read once, and
       :func:`prxref.repo_context.find_definitions` over it gives a
       ``definition`` entry whose reason is that name's candidate reason
       there; its symbol then counts as found;
    4. at ``"repo"`` with a reader and a ``listing_paths`` that is not None,
       :func:`prxref.repo_readers.reader_entries` over the chunk's files,
       with the same guarded reader, so it gets only the reads the chunk's
       cap has left after the resolver, every PR diff file's path as
       ``diff_paths``, so a diff file is never a candidate, and ``exclude``.
       It gives ``reader`` entries with reason ``shared-state``: excerpts of
       non-diff files that read state the chunk's added lines write. Its
       caps, :data:`prxref.repo_readers.MAX_READER_ENTRIES` and
       :data:`prxref.repo_readers.MAX_READER_SCAN`, are read when it runs.

    ``"repo"`` with ``read`` None is exactly ``"diff"`` with ``read`` None.

    Entries at excluded paths are dropped (this covers the hunk-derived
    entries of an excluded diff file), the rest are ordered by ``(REASONS
    rank, path, line)`` and deduplicated on ``(path, line)``, the first kept.
    They are admitted in that order while the sum of their rendered lengths
    stays within ``max_chars``; admission stops at the first entry that does
    not fit, even when a later one would. The omitted line does not count
    toward ``max_chars``.
    """
    if mode not in MODES:
        raise ValueError(f"repository context mode must be one of {MODES}, got {mode!r}")
    if mode == "off":
        return EMPTY_UNIT
    guarded = _guard(read, exclude)
    entries = list(diff_definitions(chunk, all_files, guarded))
    if mode == "repo" and guarded is not None:
        found = {
            entry.symbol
            for entry in entries
            if entry.kind == "definition" and not _excluded(exclude, entry.path)
        }
        entries.extend(
            contract_entries(chunk, contract_paths=contract_paths, read=guarded, priority=contract_priority)
        )
        entries.extend(
            _resolver_entries(
                chunk,
                all_files,
                guarded,
                listing_paths=listing_paths,
                listing_complete=listing_complete,
                exclude=exclude,
                found=found,
            )
        )
    if mode == "repo" and guarded is not None and listing_paths is not None:
        entries.extend(
            repo_readers.reader_entries(
                chunk_files(chunk),
                listing=listing_paths,
                read=guarded,
                diff_paths={getattr(f, "path", "") for f in all_files},
                exclude=exclude,
            )
        )
    return _admit(_merge(entries, exclude), max_chars)
