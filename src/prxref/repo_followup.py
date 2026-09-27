"""Definition lookup for the context follow-up of a worker chunk (#22).

A worker that is not shown the definition of a symbol a finding turns on
phrases that finding as a question and caps its confidence below the floor,
so the quality gate drops it. With ``PRXREF_CONTEXT_FOLLOWUP=on`` the chunk is
re-sent once with those definitions appended. This module is the lookup half:
it picks the questions (:func:`question_indices`), the symbol names they ask
about (:func:`finding_names`, :func:`lookup_names`), reads the definitions at
the pull request head (:func:`lookup_excerpts`) and renders the prompt block
(:func:`render_followup_block`).

Names come from structure, never from phrase lists: the backtick spans of a
finding's title and body first, then the identifiers of its plain text whose
shape marks them as code (a dotted chain, a call, a ``_``, a type-like word;
see :func:`name_tiers`). A Python builtin class such as ``TypeError`` never
counts, nor does a bare lowercase builtin such as ``len(``, since neither
has a definition in the repository; after a dot (``store.filter(``) a
lowercase builtin name is a repository attribute and counts. A name the PR
defines itself, on a hunk line of any file or anywhere in the head text of
the chunk's own files (:func:`diff_defined_names`), is not looked up, since
the worker was shown it. Every question gets one name before any question
gets a second (:func:`lookup_names`), so one question's names cannot fill
the cap alone.

The module is pure: stdlib plus :mod:`prxref.repo_context`,
:mod:`prxref.repo_resolve`, :mod:`prxref.repo_readers`,
:mod:`prxref.chunk_context`, :mod:`prxref.jvm_lang` and
:mod:`prxref.triage`, with no I/O except through the ``read`` callable the
caller passes. Every cap is a module constant read when the function runs, so
a test can patch it.
"""
from __future__ import annotations

import builtins
import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass

from . import chunk_context, jvm_lang, repo_readers
from .repo_context import _type_like, definition_regexes, find_definitions, language_of
from .repo_resolve import resolve_candidates
from .triage import Finding

FOLLOWUP_HEADER = "### Definitions referenced by this chunk, looked up for its open questions"
FOLLOWUP_NOTE = (
    "These definitions were read at the pull request head for symbols your first reading of this chunk "
    "could not see. Treat them as shown, and report any finding at its line in the diff above."
)

MAX_FOLLOWUP_NAMES = 3
MAX_FOLLOWUP_READS = 8
MAX_FOLLOWUP_EXCERPTS = 4
MAX_FOLLOWUP_EXCERPT_LINES = 30
MAX_FOLLOWUP_CHARS = 4000

SOURCES = ("import", "path-convention", "name-search", "definition-scan")

_SPAN_RE = re.compile(r"`([^`\n]{1,120})`")
_NAME_RE = re.compile(r"[A-Za-z_$][\w$]*")
_CHAIN_RE = re.compile(r"(?<![\w$.])[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
_PATH_SEPARATORS = ("/", "\\")
_RECEIVERS = frozenset({"self", "this", "cls", "super"})
_LITERALS = frozenset({"None", "True", "False", "null", "true", "false", "undefined", "nil", "NaN"})
_PY_BUILTIN_CLASSES = frozenset(name for name in dir(builtins) if name[:1].isupper())
_PY_BARE_BUILTINS = frozenset(dir(builtins)) - _PY_BUILTIN_CLASSES
_DROPPED = (
    _RECEIVERS
    | _LITERALS
    | _PY_BUILTIN_CLASSES
    | chunk_context._PY_KEYWORDS
    | chunk_context._JS_KEYWORDS
    | jvm_lang.JAVA_KEYWORDS
    | jvm_lang.KOTLIN_KEYWORDS
)
_SENTENCE_START_RE = re.compile(r"(?:^|[.!?:;]\s+|\n\s*)$")
_BRACE_LANGUAGES_OPEN = "([{"
_BRACE_LANGUAGES_CLOSE = ")]}"


@dataclass(frozen=True)
class FollowupExcerpt:
    """One definition looked up for a chunk's open questions.

    ``path`` is repository-relative and ``line`` the 1-based definition line.
    ``symbol`` is the looked-up name the definition defines, and ``covers``
    every other name a definition regex matches inside the excerpt (dunder
    names and names under three characters left out), such as the methods of
    a class; a wanted name in ``covers`` counts as resolved by this excerpt.
    ``source`` is a member of :data:`SOURCES`: the resolver reason of the
    candidate file, or ``"definition-scan"``. ``text`` is the
    :func:`definition_body`.
    """

    path: str
    line: int
    symbol: str
    covers: tuple[str, ...]
    source: str
    text: str

    def rendered(self) -> str:
        """The prompt line, ``path:line: text``."""
        return f"{self.path}:{self.line}: {self.text}"

    def record(self) -> dict:
        """The run-record row: path, line, symbol, source, and the rendered length as ``chars``."""
        return {
            "path": self.path,
            "line": self.line,
            "symbol": self.symbol,
            "source": self.source,
            "chars": len(self.rendered()),
        }


def question_indices(findings: Sequence[Finding], floor: float) -> list[int]:
    """Indices of the findings whose ``confidence`` is below ``floor``, best first.

    The comparison is the quality gate's own, ``confidence < floor``. The
    indices are ordered by confidence, highest first, then by index.
    """
    picked = [index for index, finding in enumerate(findings) if finding.confidence < floor]
    return sorted(picked, key=lambda index: (-findings[index].confidence, index))


def _kept(name: str, dotted: bool) -> bool:
    return len(name) >= 3 and name not in _DROPPED and (dotted or name not in _PY_BARE_BUILTINS)


def _sentence_start(text: str, start: int) -> bool:
    return bool(_SENTENCE_START_RE.search(text[:start]))


def _snake(name: str) -> bool:
    return "_" in name and bool(name.strip("_"))


def _plain_tier(name: str, parts: int, position: int, called: bool, sentence_start: bool) -> int | None:
    if _type_like(name):
        structural = parts > 1 or called or _snake(name)
        if not structural and sentence_start and not any(
            c.isupper() or c in "_$" or c.isdigit() for c in name[1:]
        ):
            return None
        return 0
    if position > 0:
        return 1
    if parts > 1 or called or _snake(name):
        return 2
    return None


def _plain_names(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    seen: set[str] = set()
    for match in _CHAIN_RE.finditer(text):
        start, end = match.span()
        if text[start - 1 : start] in _PATH_SEPARATORS or text[end : end + 1] in _PATH_SEPARATORS:
            continue
        parts = match.group(0).split(".")
        if len(parts) > 1 and language_of(match.group(0)):
            continue
        called = text[end : end + 1] == "("
        at_start = _sentence_start(text, start)
        for position, name in enumerate(parts):
            if name in seen or not _kept(name, position > 0):
                continue
            tier = _plain_tier(name, len(parts), position, called and position == len(parts) - 1, at_start)
            if tier is None:
                continue
            seen.add(name)
            out.append((tier, name))
    return out


def name_tiers(title: str, body: str) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """The names one finding asks about, in three ranked tiers.

    The identifiers inside the backtick spans of ``title`` and ``body`` (a
    span is one line of at most 120 characters) are kept when they have at
    least three characters and are not a receiver (``self``, ``this``,
    ``cls``, ``super``), a literal, a Python, JS/TS, Java or Kotlin keyword,
    or a capitalised name of Python's :mod:`builtins` (``TypeError``,
    ``Exception``), which cannot resolve to a repository definition. A
    lowercase builtin (``len``, ``dict``, ``open``, ``filter``) is dropped as
    a bare name or call, but kept when it directly follows a ``.``
    (``store.filter(``), where it names a repository attribute. Tier 0 holds
    the type-like names (an uppercase first letter and a lowercase letter),
    tier 1 the other names that directly follow a ``.``,
    tier 2 the rest; each name appears once, in its first tier and position.

    The plain text outside the spans gives names too, under the same filters.
    An identifier there counts only through its shape: a dotted chain
    ``a.b`` or ``a.b(`` gives ``b`` (any name after a ``.``) and ``a``, a
    call ``name(`` gives ``name``, and an identifier holding ``_`` that is
    not only underscores (``model_history``, ``HISTORY_WINDOW``) counts
    alone, as does a type-like word, less one that starts a sentence unless
    it has an uppercase letter, digit, ``_`` or ``$`` after its first
    character, so ``StateStore is ...`` counts and ``The store ...`` does
    not. A plain English word therefore never counts. A chain whose whole
    text is a file name of a known source language (``history.py``,
    ``Foo.java``), or that touches a ``/`` or ``\\``, gives nothing. The
    plain names are tiered like span names: type-like, then after a ``.``,
    then the rest, each tier in order of first appearance. With a backtick
    span present, they follow every span name, all in tier 2 in that order,
    so a plain name never outranks a span name.
    """
    text = f"{title}\n{body}"
    spans = _SPAN_RE.findall(text)
    plain = sorted(_plain_names(_SPAN_RE.sub(" ", text)), key=lambda entry: entry[0])
    if not spans:
        by_tier: tuple[list[str], list[str], list[str]] = ([], [], [])
        for tier, name in plain:
            by_tier[tier].append(name)
        return tuple(by_tier[0]), tuple(by_tier[1]), tuple(by_tier[2])
    tiers: tuple[list[str], list[str], list[str]] = ([], [], [])
    seen: set[str] = set()
    for span in spans:
        for match in _NAME_RE.finditer(span):
            name = match.group(0)
            dotted = match.start() > 0 and span[match.start() - 1] == "."
            if name in seen or not _kept(name, dotted):
                continue
            seen.add(name)
            if _type_like(name):
                tiers[0].append(name)
            elif dotted:
                tiers[1].append(name)
            else:
                tiers[2].append(name)
    tiers[2].extend(name for _, name in plain if name not in seen)
    return tuple(tiers[0]), tuple(tiers[1]), tuple(tiers[2])


def finding_names(title: str, body: str) -> list[str]:
    """The :func:`name_tiers` of one finding, flattened in tier order."""
    return [name for tier in name_tiers(title, body) for name in tier]


def _tiers_of(entry: Sequence[object]) -> tuple[Sequence[str], ...]:
    if all(isinstance(item, str) for item in entry):
        names = [str(item) for item in entry]
        return [n for n in names if _type_like(n)], [n for n in names if not _type_like(n)]
    return tuple(tier for tier in entry if not isinstance(tier, str))


def lookup_names(
    ranked: Sequence[Sequence[object]],
    *,
    defined: Collection[str],
    max_names: int | None = None,
) -> list[str]:
    """The chunk's lookup list: one name per question first, then the rest tier by tier.

    ``ranked`` holds one entry per question, highest confidence first. An
    entry is either a :func:`name_tiers` result or a flat
    :func:`finding_names` list, whose type-like names then form its first
    tier and the rest its second. Names in ``defined`` (normally
    :func:`diff_defined_names`) are never taken, and the list holds at most
    ``max_names``, which defaults to :data:`MAX_FOLLOWUP_NAMES` read when the
    function runs.

    Every question gets a slot before any question gets a second one: the
    list first takes each entry's best name, its first name in tier order
    not in ``defined``, in entry order, and a question whose best name an
    earlier question already took counts as served. With more questions than
    slots, the first entries win. The remaining slots take the first tier of
    every entry in order, then the second, then the third, deduplicated,
    keeping the first.
    """
    limit = MAX_FOLLOWUP_NAMES if max_names is None else max_names
    if limit <= 0:
        return []
    tiered = [_tiers_of(entry) for entry in ranked]
    depth = max((len(tiers) for tiers in tiered), default=0)
    skip = set(defined)
    out: list[str] = []
    for tiers in tiered:
        best = next((name for tier in tiers for name in tier if name not in skip), None)
        if best is None or best in out:
            continue
        out.append(best)
        if len(out) >= limit:
            return out
    for level in range(depth):
        for tiers in tiered:
            if level >= len(tiers):
                continue
            for name in tiers[level]:
                if name in skip or name in out:
                    continue
                out.append(name)
                if len(out) >= limit:
                    return out
    return out


def _diff_lines(f: object) -> list[object]:
    return [line for hunk in getattr(f, "hunks", None) or [] for line in getattr(hunk, "lines", None) or []]


def _safe_read(read: Callable[[str], str | None] | None, path: str) -> str | None:
    if read is None:
        return None
    try:
        text = read(path)
    except Exception:  # noqa: BLE001
        return None
    return text if isinstance(text, str) else None


def _defined_on(lines: Sequence[str], language: str) -> list[str]:
    regexes = definition_regexes(language)
    out: list[str] = []
    for text in lines:
        for regex in regexes:
            match = regex.match(text)
            if match:
                out.append(match.group(1))
                break
    return out


def diff_defined_names(
    all_files: Sequence[object],
    chunk: Sequence[object],
    read: Callable[[str], str | None] | None,
) -> frozenset[str]:
    """Names the pull request defines where the worker could see them.

    A name counts when a definition regex of its file's language
    (:func:`prxref.repo_context.definition_regexes`) matches a hunk line that
    is not a ``-`` line in any file of ``all_files``, or any line of the head
    text of a file of ``chunk`` that is not removed. Head text comes from
    ``read(path)``; a ``None`` answer, a raise or a ``read`` of None leaves
    only that file's hunks. Files are duck-typed like
    :class:`prxref.triage.FileDiff`.
    """
    names: set[str] = set()
    for f in all_files:
        path = getattr(f, "path", "") or ""
        language = language_of(path)
        if not path or not definition_regexes(language):
            continue
        hunk_text = [getattr(line, "text", "") for line in _diff_lines(f) if getattr(line, "kind", " ") != "-"]
        names.update(_defined_on(hunk_text, language))
    for f in chunk:
        path = getattr(f, "path", "") or ""
        language = language_of(path)
        if not path or getattr(f, "status", "") == "removed" or not definition_regexes(language):
            continue
        text = _safe_read(read, path)
        if text is not None:
            names.update(_defined_on(text.splitlines(), language))
    return frozenset(names)


def _indent(text: str) -> int:
    expanded = text.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def _depth_change(text: str) -> int:
    return sum(text.count(c) for c in _BRACE_LANGUAGES_OPEN) - sum(text.count(c) for c in _BRACE_LANGUAGES_CLOSE)


def _python_span(lines: Sequence[str], index: int) -> list[str]:
    base = _indent(lines[index])
    cursor = index
    depth = 0
    while cursor < len(lines):
        depth += _depth_change(lines[cursor])
        cursor += 1
        if depth <= 0:
            break
    chosen = list(lines[index:cursor])
    while cursor < len(lines):
        text = lines[cursor]
        if text.strip() and _indent(text) <= base:
            break
        chosen.append(text)
        cursor += 1
    while len(chosen) > 1 and not chosen[-1].strip():
        chosen.pop()
    return chosen


def _next_opens(lines: Sequence[str], cursor: int) -> bool:
    while cursor < len(lines) and not lines[cursor].strip():
        cursor += 1
    return cursor < len(lines) and lines[cursor].lstrip().startswith("{")


def _brace_span(lines: Sequence[str], index: int) -> list[str]:
    depth = _depth_change(lines[index])
    opened = "{" in lines[index]
    chosen = [lines[index]]
    cursor = index + 1
    while cursor < len(lines):
        if depth <= 0 and (opened or not _next_opens(lines, cursor)):
            break
        text = lines[cursor]
        depth += _depth_change(text)
        opened = opened or "{" in text
        chosen.append(text)
        cursor += 1
    return chosen


def definition_body(
    lines: Sequence[str],
    index: int,
    language: str,
    max_lines: int | None = None,
) -> str:
    """The full definition starting at ``lines[index]``, capped at ``max_lines`` lines.

    Python: the definition line and any continuation lines up to a balanced
    bracket, then every following line that is blank or indented deeper than
    the definition line, less trailing blank lines. Every other language: the
    definition line and the following lines up to a balanced bracket, where a
    definition whose opening ``{`` sits on a later line (after blank lines
    only) continues to that brace. Lines keep their indentation and lose
    trailing whitespace. When the body is longer than ``max_lines`` (default
    :data:`MAX_FOLLOWUP_EXCERPT_LINES`, read when the function runs), it keeps
    ``max_lines - 1`` lines and ends with a horizontal ellipsis line
    ``N more lines`` indented like the body's first continuation line.
    Returns ``""`` for an index outside ``lines``.
    """
    limit = MAX_FOLLOWUP_EXCERPT_LINES if max_lines is None else max_lines
    if index < 0 or index >= len(lines) or limit <= 0:
        return ""
    span = _python_span(lines, index) if language == "python" else _brace_span(lines, index)
    span = [text.rstrip() for text in span]
    if len(span) > limit:
        kept = span[: max(limit - 1, 1)]
        inner = [text for text in span[1:] if text.strip()]
        if inner:
            indent = inner[0][: len(inner[0]) - len(inner[0].lstrip())]
        else:
            indent = span[0][: len(span[0]) - len(span[0].lstrip())] + "    "
        kept.append(f"{indent}\N{HORIZONTAL ELLIPSIS} {len(span) - len(kept)} more lines")
        span = kept
    return "\n".join(span)


def _excluded(exclude: Callable[[str], bool] | None, path: str) -> bool:
    if exclude is None:
        return False
    try:
        return bool(exclude(path))
    except Exception:  # noqa: BLE001
        return True


def _covers(body: str, language: str, symbol: str) -> tuple[str, ...]:
    out: list[str] = []
    for name in _defined_on(body.splitlines()[1:], language):
        if name == symbol or name in out or len(name) < 3 or (name.startswith("__") and name.endswith("__")):
            continue
        out.append(name)
    return tuple(out)


def _candidate_paths(
    chunk: Sequence[object],
    names: Sequence[str],
    read: Callable[[str], str | None],
    listing_paths: Collection[str] | None,
    listing_complete: bool,
    diff_paths: set[str],
    exclude: Callable[[str], bool] | None,
) -> list[tuple[str, str]]:
    removed = {getattr(f, "path", "") for f in chunk if getattr(f, "status", "") == "removed"}
    files = [changed for changed in chunk_context.chunk_files(chunk) if changed.path not in removed]
    ordered: dict[str, str] = {}
    for changed in files:
        if not definition_regexes(language_of(changed.path)):
            continue
        text = _safe_read(read, changed.path)
        if text is None:
            text = "\n".join(changed.added)
        for candidate in resolve_candidates(
            changed.path, text, names, listing=listing_paths, listing_complete=listing_complete
        ):
            ordered.setdefault(candidate.path, candidate.reason)
    if listing_paths is not None:
        for path in repo_readers.reader_candidates(
            [changed.path for changed in files], listing_paths, diff_paths=diff_paths
        ):
            ordered.setdefault(path, "definition-scan")
    return [
        (path, source)
        for path, source in ordered.items()
        if path not in diff_paths and not _excluded(exclude, path)
    ]


def _lookup(
    chunk: Sequence[object],
    all_files: Sequence[object],
    names: Sequence[str],
    read: Callable[[str], str | None],
    listing_paths: Collection[str] | None,
    listing_complete: bool,
    exclude: Callable[[str], bool] | None,
    shown: str,
    admitted: list[FollowupExcerpt],
) -> None:
    max_reads = MAX_FOLLOWUP_READS
    max_excerpts = MAX_FOLLOWUP_EXCERPTS
    max_chars = MAX_FOLLOWUP_CHARS
    wanted = list(dict.fromkeys(name for name in names if name))
    if not wanted or max_reads <= 0 or max_excerpts <= 0 or max_chars <= 0:
        return
    diff_paths = {getattr(f, "path", "") for f in all_files} | {getattr(f, "path", "") for f in chunk}
    candidates = _candidate_paths(chunk, wanted, read, listing_paths, listing_complete, diff_paths, exclude)
    unresolved = list(wanted)
    reads = 0
    used = 0
    for path, source in candidates:
        if not unresolved or reads >= max_reads:
            return
        reads += 1
        text = _safe_read(read, path)
        if text is None:
            continue
        language = language_of(path)
        lines = text.splitlines()
        for symbol, line, _ in find_definitions(text, unresolved, language=language):
            if symbol not in unresolved:
                continue
            body = definition_body(lines, line - 1, language)
            covers = _covers(body, language, symbol)
            unresolved = [name for name in unresolved if name != symbol and name not in covers]
            if body in shown:
                continue
            excerpt = FollowupExcerpt(path, line, symbol, covers, source, body)
            size = len(excerpt.rendered())
            if len(admitted) >= max_excerpts or used + size > max_chars:
                return
            admitted.append(excerpt)
            used += size


def lookup_excerpts(
    chunk: Sequence[object],
    all_files: Sequence[object],
    names: Sequence[str],
    *,
    read: Callable[[str], str | None] | None,
    listing_paths: Collection[str] | None,
    listing_complete: bool,
    exclude: Callable[[str], bool] | None,
    shown: str,
) -> list[FollowupExcerpt]:
    """The definitions of ``names`` outside the pull request, in admission order.

    ``chunk`` and ``all_files`` are the chunk's and the whole PR's
    ``triage.FileDiff`` records, ``names`` the :func:`lookup_names` list,
    ``read`` the chunk's reader, ``listing_paths`` the run's path listing (or
    None) and ``listing_complete`` whether it was not truncated. ``exclude``
    marks a path that must never be read (one that raises counts as true), and
    ``shown`` is the text the first call already showed: its context blocks
    plus the rendered chunk.

    Candidate files, in order: for each chunk file that is not removed and
    whose language has definition regexes,
    :func:`prxref.repo_resolve.resolve_candidates` over its head text (its
    added lines when the read gives nothing) and ``names``; then, with a
    listing, :func:`prxref.repo_readers.reader_candidates` of the chunk's
    files, whose source is ``"definition-scan"``. A path keeps its first
    source. A PR diff path or an excluded path is never read. The chunk's own
    head-text reads are diff paths, which production serves from the shared
    uncapped reader, and do not count toward :data:`MAX_FOLLOWUP_READS`.

    The candidates are read in order, one read each, until every name is
    resolved or :data:`MAX_FOLLOWUP_READS` reads were made, cached or not.
    In each file :func:`prxref.repo_context.find_definitions` locates the
    first definition of every unresolved name, and the excerpt is its
    :func:`definition_body`. The name, and every name in the excerpt's
    ``covers``, is then resolved. An excerpt whose text is a substring of
    ``shown`` is not admitted. The others are admitted in order while their
    rendered lengths sum to at most :data:`MAX_FOLLOWUP_CHARS` and their count
    stays within :data:`MAX_FOLLOWUP_EXCERPTS`; the lookup stops at the first
    one that does not fit. Every cap is read when the function runs. With
    ``read`` None or no name the result is ``[]`` and nothing is read. The
    function never raises: a read that raises counts as None, and any other
    failure returns the excerpts admitted so far.
    """
    admitted: list[FollowupExcerpt] = []
    if read is None or not names:
        return admitted
    try:
        _lookup(chunk, all_files, names, read, listing_paths, listing_complete, exclude, shown or "", admitted)
    except Exception:  # noqa: BLE001
        return admitted
    return admitted


def render_followup_block(excerpts: Sequence[FollowupExcerpt]) -> str:
    """The follow-up prompt block, or ``""`` when ``excerpts`` is empty.

    :data:`FOLLOWUP_HEADER`, a blank line, :data:`FOLLOWUP_NOTE`, a blank
    line, then each excerpt's ``rendered()`` line, joined with newlines. The
    header starts with :data:`prxref.chunk_context.DEFINITIONS_HEADER`, so the
    worker prompt's rule about definitions shown under that heading covers
    the looked-up symbols.
    """
    if not excerpts:
        return ""
    return FOLLOWUP_HEADER + "\n\n" + FOLLOWUP_NOTE + "\n\n" + "\n".join(e.rendered() for e in excerpts)
