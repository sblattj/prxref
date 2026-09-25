"""Readers of the shared state a chunk's added lines write to (#22).

A PR can break code it never touches by writing something new into state that
unchanged code reads: an object put into a scratch map that is later
serialized, or a row appended to a table that another function reads with a
row limit. The definitions context cannot find that code, because the diff
never names it. This module finds it with a plain text search: it takes the
keys the added lines WRITE, then looks in other files of the same language for
lines that READ those keys, and returns short excerpts of the functions around
them as :class:`prxref.repo_context.ContextEntry` values of kind ``"reader"``
and reason ``"shared-state"``.

Writes, found on a chunk file's added lines by :func:`shared_state_keys`:

- a subscript store, ``<recv>[k] = v`` (also ``+=`` and the other augmented
  assignments, and chained subscripts): the key is ``recv``, the last
  identifier before the ``[``, so ``run.root().data[k] = v`` and
  ``self.cache[k] = v`` give ``data`` and ``cache``;
- a write call, ``<recv>.<verb>(`` with a verb from :data:`WRITE_VERBS`: the
  key is the last segment of the receiver, so ``self.table.append(m)`` gives
  ``table``; a receiver that is ``self``, ``this``, ``super`` or ``cls`` itself
  gives nothing.

A bare receiver the added lines bind to an attribute (``data =
run.root().data``) is an alias, and the key is the attribute it names. A key
the added lines assign a NEW value (``items = []``, ``self.cache = {}``,
``self._lines: list[str] = []``, ``List<X> xs = new ArrayList<>()``,
``ledger = Ledger(turn)``) is skipped: that is a new local or field, not state
that exists outside the hunk. An assignment whose right side is a plain
reference, a name or an attribute chain (``self.table = table``, ``data =
run.root().data``), binds existing state and does not skip the key.

Reads, found in a file's text by :func:`reader_matches`, on any line that is
not a comment or an import:

- ``.key`` used other than as a store;
- ``["key"]`` or ``['key']`` used other than as a store;
- ``key.<method>(`` where the method is not a write verb.

A store is not a read: ``.key = v``, ``.key[k] = v``, ``["key"] = v`` and
``.key.append(...)`` are other writers.

Each read becomes one excerpt running from the enclosing definition's line
(the nearest preceding definition line that is less indented than the read)
down to the read's line, at most :data:`MAX_READER_LINES` lines; a longer span
keeps the definition line, an ellipsis line, and the lines just above the read.
The definition lines are the language's
:func:`prxref.repo_context.definition_regexes` plus function and method
patterns for js, java, kotlin, go and rust.

:func:`reader_entries` runs the whole search for one chunk. The caps
:data:`MAX_READER_ENTRIES` and :data:`MAX_READER_SCAN` are module constants
read at call time, so patching ``MAX_READER_ENTRIES = 0`` turns readers off
with no read at all. There is no configuration key.

The module is pure: stdlib plus the repository-context modules, no I/O except
through the ``read`` callable the caller passes, and no network.
"""
from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Collection, Iterable, Sequence

from . import chunk_context, repo_resolve
from .repo_context import ContextEntry, definition_regexes, language_of

MAX_READER_ENTRIES = 6
MAX_READER_SCAN = 24
MAX_READER_LINES = 8

WRITE_VERBS = (
    "append", "add", "insert", "save", "put", "push", "store", "write", "extend", "update", "setdefault",
)

_IDENT = r"[A-Za-z_$][\w$]*"
_VERBS = "|".join(WRITE_VERBS)
_STORE = r"(?:\*\*|//|>>>|<<|>>|\?\?|\|\||&&|[-+*/%&|^])?=(?![=>])"
_SUBSCRIPT = r"\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]"
_MEMBER = r"(?:\?|!!)?\."

_SUBSCRIPT_STORE_RE = re.compile(rf"(?<![\w$])({_IDENT})(?:\s*{_SUBSCRIPT})+\s*{_STORE}")
_VERB_CALL_RE = re.compile(rf"(?<![\w$])({_IDENT})\s*{_MEMBER}\s*(?:{_VERBS})\s*\(")
_ASSIGN_RE = re.compile(
    rf"(?<![\w$])(?P<owner>(?:self|this)\s*\.\s*)?(?P<name>{_IDENT})\s*(?::[^=()]*)?(?<![!<>=])=(?![=>])"
)
_REFERENCE_RE = re.compile(rf"{_IDENT}(?:\s*(?:\(\s*\))?\s*{_MEMBER}\s*{_IDENT})*")
_IDENT_RE = re.compile(_IDENT)
_DECLARATION_TAIL_RE = re.compile(r"[\w$>\]?]$")
_TRAILING_COMMENT_RE = re.compile(r"\s+(?:#|//)")
_STORE_AFTER_RE = re.compile(
    rf"(?:\s*{_SUBSCRIPT})*(?:\s*:[^=()\[\]]*)?\s*(?:{_STORE}|{_MEMBER}\s*(?:{_VERBS})\s*\()"
)
_SKIPPED_LINE_RE = re.compile(r"^\s*(?:#|//|/\*|\*|(?:import|from|package)\b)")
_COMMENT_LINE_RE = re.compile(r"^\s*(?:#|//|/\*|\*)")

_RECEIVER_KEYWORDS = frozenset({"self", "this", "super", "cls"})
_LITERALS = frozenset({"True", "False", "None", "true", "false", "null", "nil", "undefined", "NaN"})

_JS_DEFINITIONS = (
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\b"),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\b"),
    re.compile(
        rf"^\s*(?:export\s+)?(?:const|let|var)\s+{_IDENT}\s*(?::[^=]*)?=\s*(?:async\s+)?"
        rf"(?:function\b|(?:\([^)]*\)|{_IDENT})\s*(?::[^=]*)?=>)"
    ),
    re.compile(
        r"^\s*(?:(?:public|private|protected|static|async|readonly|override|abstract|get|set)\s+)*\*?\s*"
        r"(?!(?:if|for|while|switch|catch|with|return|function|new|typeof|await|yield|else|do|super|this)\b)"
        rf"{_IDENT}\s*(?:<[^>]*>)?\s*\([^)]*\)\s*(?::\s*[^={{;]+)?\{{\s*$"
    ),
)
_JAVA_METHOD = re.compile(
    r"^\s*(?:@[\w$.]+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|protected|private|static|final|abstract|synchronized|native|default|strictfp)\s+)*"
    r"(?:<[^>]*>\s+)?"
    r"(?!(?:return|new|throw|else|case|if|for|while|switch|catch|try|do|yield|assert)\b)"
    rf"[\w$.<>\[\],? ]+?\s+{_IDENT}\s*\([^;=]*$"
)
_KOTLIN_DEFINITION = re.compile(
    r"^\s*(?:@[\w$.]+(?:\([^)]*\))?\s+)*(?:[a-z]+\s+)*(?:fun|class|interface|object)\b"
)
_GO_DEFINITION = re.compile(r"^(?:func|type)\b")
_RUST_DEFINITION = re.compile(
    r'^\s*(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|const|unsafe|extern(?:\s+"[^"]*")?)\s+)*'
    r"(?:fn|struct|enum|trait|impl|mod)\b"
)
_EXTRA_DEFINITIONS: dict[str, tuple[re.Pattern[str], ...]] = {
    "js": _JS_DEFINITIONS,
    "java": (_JAVA_METHOD,),
    "kotlin": (_KOTLIN_DEFINITION,),
    "go": (_GO_DEFINITION,),
    "rust": (_RUST_DEFINITION,),
}


def _statement_start(prefix: str) -> bool:
    if not prefix.strip():
        return True
    return prefix[-1].isspace() and _DECLARATION_TAIL_RE.search(prefix.rstrip()) is not None


def _right_side(rest: str) -> str:
    value = _TRAILING_COMMENT_RE.split(rest.split(";", 1)[0], maxsplit=1)[0]
    return value.strip().rstrip(",").strip()


def _is_reference(value: str) -> bool:
    return value not in _LITERALS and _REFERENCE_RE.fullmatch(value) is not None


def _bindings(lines: Iterable[str]) -> tuple[set[str], dict[str, str]]:
    fresh: set[str] = set()
    aliases: dict[str, str] = {}
    for text in lines:
        for match in _ASSIGN_RE.finditer(text):
            if not _statement_start(text[: match.start()]):
                continue
            name = match.group("name")
            value = _right_side(text[match.end():])
            if not _is_reference(value):
                fresh.add(name)
            elif match.group("owner") is None:
                aliases.setdefault(name, _IDENT_RE.findall(value)[-1])
    return fresh, aliases


def _write_key(text: str, start: int, name: str, aliases: dict[str, str]) -> str | None:
    if name in _RECEIVER_KEYWORDS:
        return None
    before = text[:start].rstrip()
    if before.endswith("."):
        return name
    return aliases.get(name, name)


def shared_state_keys(added: Sequence[str]) -> list[str]:
    """The shared-state keys the added lines write, in first-appearance order, deduplicated.

    ``added`` holds the text of one chunk file's ``+`` lines. A key comes from
    a subscript store (``recv[k] = v``, the key ``recv``) or a write call
    (``recv.<verb>(`` with a verb from :data:`WRITE_VERBS`, the key the last
    segment of ``recv``); a bare receiver the added lines alias to an
    attribute (``data = run.root().data``) gives that attribute's name.
    Comment and import lines are ignored, and so is a subscript on a type
    annotation (``x: list[str] = []``). A key the added lines assign a new
    value, anything but a plain name or attribute chain, is dropped.
    """
    lines = [text for text in added if not _SKIPPED_LINE_RE.match(text)]
    fresh, aliases = _bindings(lines)
    keys: dict[str, None] = {}
    for text in lines:
        found: list[tuple[int, str]] = []
        for match in _SUBSCRIPT_STORE_RE.finditer(text):
            if text[: match.start()].rstrip().endswith(":"):
                continue
            key = _write_key(text, match.start(), match.group(1), aliases)
            if key:
                found.append((match.start(), key))
        for match in _VERB_CALL_RE.finditer(text):
            key = _write_key(text, match.start(), match.group(1), aliases)
            if key:
                found.append((match.start(), key))
        for _, key in sorted(found):
            if key not in fresh:
                keys.setdefault(key, None)
    return list(keys)


def _read_patterns(key: str) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    escaped = re.escape(key)
    return (
        re.compile(rf"(?<!\.)\.\s*{escaped}(?![\w$])"),
        re.compile(rf"""\[\s*(['"]){escaped}\1\s*\]"""),
        re.compile(rf"(?<![\w$.]){escaped}\s*{_MEMBER}\s*({_IDENT})\s*\("),
    )


def _reads(text: str, patterns: tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]) -> bool:
    attribute, subscript, receiver = patterns
    for match in attribute.finditer(text):
        if not _STORE_AFTER_RE.match(text, match.end()):
            return True
    for match in subscript.finditer(text):
        if not _STORE_AFTER_RE.match(text, match.end()):
            return True
    return any(match.group(1) not in WRITE_VERBS for match in receiver.finditer(text))


def _indent(text: str) -> int:
    expanded = text.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def _is_definition(text: str, language: str) -> bool:
    if any(regex.match(text) for regex in _EXTRA_DEFINITIONS.get(language, ())):
        return True
    if language == "js" and text[:1].isspace():
        return False
    return any(regex.match(text) for regex in definition_regexes(language))


def _start_index(lines: Sequence[str], index: int, language: str) -> int:
    if _is_definition(lines[index], language):
        return index
    limit = _indent(lines[index])
    for cursor in range(index - 1, -1, -1):
        text = lines[cursor]
        stripped = text.strip()
        if not stripped or _COMMENT_LINE_RE.match(text):
            continue
        width = _indent(text)
        if width < limit and _is_definition(text, language):
            return cursor
        if width == 0 and not stripped.startswith((")", "]", "@")):
            return index
    return index


def _excerpt(lines: Sequence[str], start: int, index: int) -> str:
    cap = max(MAX_READER_LINES, 3)
    if index - start < cap:
        chosen = list(lines[start:index + 1])
    else:
        tail = list(lines[index - cap + 3:index + 1])
        indent = tail[0][: len(tail[0]) - len(tail[0].lstrip())]
        chosen = [lines[start], f"{indent}\N{HORIZONTAL ELLIPSIS}", *tail]
    return "\n".join(text.rstrip() for text in chosen)


def reader_matches(text: str, keys: Sequence[str], *, language: str) -> list[tuple[str, int, str]]:
    """``(key, line, excerpt)`` for each place ``text`` reads one of ``keys``.

    ``text`` is one file's content and ``language`` its
    :func:`prxref.repo_context.language_of`. A line reads a key through
    ``.key``, ``["key"]``/``['key']`` or ``key.<method>(`` when that use is not
    a store; comment and import lines never read. Each reading line gives an
    excerpt from its enclosing definition's line down to the reading line, at
    most :data:`MAX_READER_LINES` lines, and ``line`` is the excerpt's 1-based
    first line. When the span is longer, the excerpt keeps the definition
    line, then an indented horizontal ellipsis, then the lines ending at the
    read. With no enclosing definition the excerpt is the reading line alone.
    The first key found on a line names it; reads that share an excerpt start
    keep the first. Results are in reading-line order. Returns ``[]`` when
    ``text`` is empty or larger than ``chunk_context.MAX_FILE_BYTES`` in UTF-8,
    or when no key is given.
    """
    wanted = [key for key in dict.fromkeys(keys) if key]
    if not wanted or not text:
        return []
    if len(text.encode("utf-8", "ignore")) > chunk_context.MAX_FILE_BYTES:
        return []
    patterns = {key: _read_patterns(key) for key in wanted}
    lines = text.splitlines()
    starts: set[int] = set()
    out: list[tuple[str, int, str]] = []
    for index, line in enumerate(lines):
        if _SKIPPED_LINE_RE.match(line):
            continue
        key = next((key for key in wanted if key in line and _reads(line, patterns[key])), None)
        if key is None:
            continue
        start = _start_index(lines, index, language)
        if start in starts:
            continue
        starts.add(start)
        out.append((key, start + 1, _excerpt(lines, start, index)))
    return out


def reader_candidates(
    changed: Sequence[str],
    listing: Iterable[str],
    *,
    diff_paths: Collection[str] = (),
) -> list[str]:
    """The listing files that may read state the ``changed`` files write, best first.

    A candidate has the :func:`prxref.repo_context.language_of` of at least
    one changed file (never ``""``), and is neither a changed file nor in
    ``diff_paths``. Candidates are ordered by how many leading directories
    they share with the nearest changed file of their language, most first,
    then by path, the order the repository resolver's name search uses.
    """
    by_language: dict[str, list[str]] = {}
    for path in changed:
        language = language_of(path)
        if language:
            by_language.setdefault(language, []).append(posixpath.dirname(path))
    skipped = set(changed) | set(diff_paths)
    ranked: dict[str, int] = {}
    for path in listing:
        directories = by_language.get(language_of(path))
        if not directories or path in skipped or path in ranked:
            continue
        home = posixpath.dirname(path)
        ranked[path] = max(repo_resolve._shared_depth(home, directory) for directory in directories)
    return sorted(ranked, key=lambda path: (-ranked[path], path))


def _excluded(exclude: Callable[[str], bool] | None, path: str) -> bool:
    if exclude is None:
        return False
    try:
        return bool(exclude(path))
    except Exception:  # noqa: BLE001
        return True


def reader_entries(
    files: Sequence[object],
    *,
    listing: Collection[str] | None,
    read: Callable[[str], str | None] | None,
    diff_paths: Collection[str] = (),
    exclude: Callable[[str], bool] | None = None,
) -> list[ContextEntry]:
    """The ``reader`` / ``shared-state`` entries for one worker chunk.

    ``files`` holds the chunk's files, duck-typed on ``path`` and ``added``
    (the text of its ``+`` lines) like :class:`prxref.chunk_context.ChunkFile`.
    ``listing`` is the repository's file paths, ``read`` the chunk's reader
    (each call may cost a read; None or a non-string answer is skipped),
    ``diff_paths`` every path the PR changes, and ``exclude(path)`` true marks
    a path that must never be read (an ``exclude`` that raises counts as true).

    Each file's :func:`shared_state_keys` are gathered per language. The
    :func:`reader_candidates` of the files that have keys are walked in order;
    an excluded candidate is skipped without a read, and every other one is
    read once, at most :data:`MAX_READER_SCAN` reads in all. Each text read
    goes through :func:`reader_matches` with the keys of its language, and
    every match becomes a ``ContextEntry`` whose ``symbol`` is the key read,
    ``kind`` ``"reader"`` and ``reason`` ``"shared-state"``. The walk stops
    once :data:`MAX_READER_ENTRIES` entries are kept. Both caps are read when
    the function runs; with either at zero, ``read`` None, no listing or no
    key, the result is ``[]`` and nothing is read.
    """
    max_entries = MAX_READER_ENTRIES
    max_scan = MAX_READER_SCAN
    if read is None or not listing or max_entries <= 0 or max_scan <= 0:
        return []
    keys: dict[str, dict[str, None]] = {}
    writers: list[str] = []
    for changed in files:
        path = getattr(changed, "path", "") or ""
        language = language_of(path)
        if not language:
            continue
        found = shared_state_keys(tuple(getattr(changed, "added", ()) or ()))
        if found:
            keys.setdefault(language, {}).update(dict.fromkeys(found))
            writers.append(path)
    if not writers:
        return []
    entries: list[ContextEntry] = []
    scanned = 0
    for path in reader_candidates(writers, listing, diff_paths=diff_paths):
        if scanned >= max_scan or len(entries) >= max_entries:
            break
        if _excluded(exclude, path):
            continue
        scanned += 1
        text = read(path)
        if not isinstance(text, str):
            continue
        language = language_of(path)
        for key, line, body in reader_matches(text, list(keys[language]), language=language):
            entries.append(ContextEntry(path, line, key, "reader", "shared-state", body))
            if len(entries) >= max_entries:
                break
    return entries
