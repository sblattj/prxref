"""Repository context outside the diff: shared types and the definitions core.

``chunk_context`` answers what a referenced identifier IS only when its
definition sits in the SAME changed file. This module holds the pieces the
repository-context feature (``PRXREF_REPO_CONTEXT``) builds on: the
:class:`ContextEntry` record every context source emits, the admission ranks
in :data:`REASONS`, a language map and definition regexes that answer Java and
Kotlin themselves, the names an added line references, a definition scan over
the text of ANY file, and the exclude floor (:data:`EXCLUDE_FLOOR`,
:func:`exclude_predicate`) that keeps label files and secrets out of every read.

The module is pure. It is stdlib plus :mod:`prxref.chunk_context`,
:mod:`prxref.jvm_lang` and :func:`prxref.rules.match_globs`, performs no I/O
and no network, and callers hand it file text they already read. It
imports ``chunk_context``'s underscore helpers (``_language``,
``_definition_regexes``, ``_keywords``, ``_entry_text``, ``_IDENT_RE``) on
purpose, so both modules scan and render a definition the same way instead of
drifting apart.

``chunk_context`` handles Java and Kotlin itself for the same-file context,
with methods, fields, enum constants and more. This module keeps a TYPES-ONLY
view of both for repository context, and answers them before
``chunk_context`` is consulted: a Java ``class``, ``interface``, ``record``,
``enum`` or ``@interface`` declaration, and a Kotlin ``class`` of any flavour,
``interface``, ``object`` or ``typealias``. That keeps the names a repository
search looks for to type names, the names a JVM source file is called after,
and keeps their number small. The shared facts live in
:mod:`prxref.jvm_lang`: ``_JAVA_DEF_RE``, ``_JAVA_KEYWORDS`` and ``_JDK_NAMES``
here are the SAME objects as its ``JAVA_TYPE_RE``, ``JAVA_KEYWORDS`` and
``JDK_NAMES``, so there is one implementation.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from . import chunk_context, jvm_lang
from .rules import match_globs

REASONS = ("cross-chunk", "contract", "diff-file", "import", "path-convention", "name-search", "shared-state")
KINDS = ("definition", "contract", "reader")

_JAVA_DEF_RE = jvm_lang.JAVA_TYPE_RE

_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?((?:java|javax)\.[\w$.]*[\w$])(?:\.\*)?\s*;")

_JAVA_KEYWORDS = jvm_lang.JAVA_KEYWORDS

_JDK_NAMES = jvm_lang.JDK_NAMES

_KOTLIN_PLATFORM_ROOTS = frozenset({"kotlin", "java", "javax"})


@dataclass(frozen=True)
class ContextEntry:
    """One repository-context entry admitted into a worker chunk's prompt.

    ``path`` is repository-relative, ``line`` the 1-based start line in that
    file, ``symbol`` the type or definition name (or, for a contract, the
    operationId, schema name, route template or table name). ``kind`` is a
    member of :data:`KINDS`, ``reason`` a member of :data:`REASONS`, and
    ``text`` the entry body with per-entry caps already applied and no
    ``path:line:`` prefix.
    """

    path: str
    line: int
    symbol: str
    kind: str
    reason: str
    text: str

    def rendered(self) -> str:
        """The prompt line, ``path:line: text``, the shape ``referenced_definitions`` emits."""
        return f"{self.path}:{self.line}: {self.text}"

    def record(self) -> dict:
        """The run-record row: path, line, symbol, kind, reason, and the rendered length as ``chars``."""
        return {
            "path": self.path,
            "line": self.line,
            "symbol": self.symbol,
            "kind": self.kind,
            "reason": self.reason,
            "chars": len(self.rendered()),
        }


def language_of(path: str) -> str:
    """The definition language of ``path``: ``"java"``, ``"kotlin"``, else ``chunk_context``'s map.

    ``.java`` gives ``"java"`` and ``.kt`` or ``.kts`` gives ``"kotlin"``, in
    any case, before ``chunk_context`` is asked; a ``.kts`` build script is
    Kotlin too. Returns ``""`` for a path no language claims.
    """
    return jvm_lang.jvm_language(path) or chunk_context._language(path)


def definition_regexes(language: str) -> tuple[re.Pattern[str], ...]:
    """The definition regexes for ``language``; group 1 of a match is the defined name.

    Java and Kotlin get one regex each, for a type declaration only.
    ``"java"`` gets :data:`prxref.jvm_lang.JAVA_TYPE_RE` (``class``,
    ``interface``, ``record``, ``enum`` or ``@interface``, after optional
    modifiers and same-line annotations). ``"kotlin"`` gets
    :data:`prxref.jvm_lang.KOTLIN_TYPE_RE` (``class`` in every flavour, such as
    ``data class`` or ``enum class``, ``interface``, a named ``object`` and
    ``typealias``). Neither ever matches a method or function, a field or
    property, or a local variable. Every other language delegates to
    ``chunk_context``, which covers js and python and returns ``()``
    otherwise.
    """
    if language == "java":
        return (_JAVA_DEF_RE,)
    if language == "kotlin":
        return (jvm_lang.KOTLIN_TYPE_RE,)
    return chunk_context._definition_regexes(language)


def _jdk_imports(added: Iterable[str]) -> set[str]:
    names: set[str] = set()
    for text in added:
        match = _JAVA_IMPORT_RE.match(text)
        if match:
            names.update(match.group(1).split("."))
    return names


def _kotlin_platform_imports(added: Iterable[str]) -> set[str]:
    names: set[str] = set()
    for parsed in jvm_lang.parse_imports(added):
        if parsed.segments[0] in _KOTLIN_PLATFORM_ROOTS:
            names.update(parsed.segments)
            if parsed.alias:
                names.add(parsed.alias)
    return names


def _type_like(name: str) -> bool:
    return name[:1].isupper() and any(c.islower() for c in name)


def referenced_names(added: Iterable[str], language: str) -> list[str]:
    """Identifiers the added lines reference, in first-appearance order, deduplicated.

    Language keywords are removed, and so is every name a
    :func:`definition_regexes` match defines on the added lines themselves, as
    ``chunk_context.referenced_definitions`` does. Java and Kotlin, whose
    definition regexes here can only ever find a type, are filtered further:

    - keywords are :func:`prxref.jvm_lang.keywords` of the language;
    - JDK names are removed: the built-in set of common ``java.lang``,
      ``java.util``, ``java.time`` and related types
      (:data:`prxref.jvm_lang.JDK_NAMES`), plus every segment of a
      ``java.*`` or ``javax.*`` import on the added lines. For Kotlin that
      covers a ``kotlin.*`` import too, with or without a semicolon, and the
      ``as`` alias of any of the three; ``kotlinx.*`` is a library, not the
      platform, and is kept;
    - only type-like names are kept: an uppercase first letter and at least
      one lowercase letter. That drops constants, locals, methods and
      functions, properties and single-letter type parameters.
    """
    lines = list(added)
    language_is_jvm = language in ("java", "kotlin")
    keywords = jvm_lang.keywords(language) if language_is_jvm else chunk_context._keywords(language)
    defined: set[str] = set()
    for text in lines:
        for regex in definition_regexes(language):
            match = regex.match(text)
            if match:
                defined.add(match.group(1))
    dropped = keywords | defined
    if language == "java":
        dropped = dropped | _JDK_NAMES | _jdk_imports(lines)
    elif language == "kotlin":
        dropped = dropped | _JDK_NAMES | _kotlin_platform_imports(lines)
    out: dict[str, None] = {}
    for text in lines:
        for name in chunk_context._IDENT_RE.findall(text):
            if name in dropped or name in out:
                continue
            if language_is_jvm and not _type_like(name):
                continue
            out[name] = None
    return list(out)


def find_definitions(
    text: str,
    names: Iterable[str],
    *,
    language: str,
    skip_lines: frozenset[int] = frozenset(),
    max_lines: int = chunk_context.MAX_LINES_PER_DEFINITION,
) -> list[tuple[str, int, str]]:
    """``(symbol, line, entry_text)`` for the first definition of each wanted name.

    Scans ``text`` line by line with :func:`definition_regexes`, the loop
    ``chunk_context.referenced_definitions`` runs, generalized to any file:
    1-based lines in ``skip_lines`` are passed over, only the first regex that
    matches a line is consulted, and each name yields at most its FIRST
    definition. ``entry_text`` is the defining line plus continuation lines up
    to a balanced bracket or ``max_lines``. Results are in line order. Returns
    ``[]`` when ``text`` is empty or larger than ``chunk_context.MAX_FILE_BYTES``
    in UTF-8, when no name is wanted, or when the language has no regexes.
    """
    regexes = definition_regexes(language)
    wanted = set(names)
    if not regexes or not wanted or not text:
        return []
    if len(text.encode("utf-8", "ignore")) > chunk_context.MAX_FILE_BYTES:
        return []
    lines = text.splitlines()
    seen: set[str] = set()
    out: list[tuple[str, int, str]] = []
    for idx, line in enumerate(lines):
        number = idx + 1
        if number in skip_lines:
            continue
        for regex in regexes:
            match = regex.match(line)
            if not match:
                continue
            name = match.group(1)
            if name in wanted and name not in seen:
                seen.add(name)
                out.append((name, number, chunk_context._entry_text(lines, idx, max_lines)))
            break
    return out


EXCLUDE_FLOOR = (
    "**/expected.json",
    "**/cases.json",
    "**/case.json",
    "**/prxref-eval/**",
    "**/.env*",
    "**/*.pem",
    "**/*.key",
)


def exclude_predicate(extra_globs: Sequence[str] = ()) -> Callable[[str], bool]:
    """A ``path -> bool`` that is true for a path repository context must never read.

    A path is excluded when :func:`prxref.rules.match_globs` selects it with
    :data:`EXCLUDE_FLOOR` (eval labels, eval output, dotenv files, keys), or
    with ``extra_globs`` (``PRXREF_CONTEXT_EXCLUDE_GLOBS``) when that list is
    non-empty. The two lists are matched separately, so the extra globs only
    ever ADD exclusions: a ``!`` negation in ``extra_globs`` vetoes only the
    extra list's own positives and can never re-admit a floor path, which it
    would if both were one ``match_globs`` list.
    """
    extra = tuple(extra_globs)

    def excluded(path: str) -> bool:
        return match_globs(path, EXCLUDE_FLOOR) or bool(extra and match_globs(path, extra))

    return excluded
