"""Repository context outside the diff: shared types and the definitions core.

``chunk_context`` answers what a referenced identifier IS only when its
definition sits in the SAME changed file, and it knows no Java. This module
holds the pieces the repository-context feature (``PRXREF_REPO_CONTEXT``)
builds on: the :class:`ContextEntry` record every context source emits, the
admission ranks in :data:`REASONS`, a language map and definition regexes that
add Java, the names an added line references, a definition scan over the
text of ANY file, and the exclude floor (:data:`EXCLUDE_FLOOR`,
:func:`exclude_predicate`) that keeps label files and secrets out of every read.

The module is pure. It is stdlib plus :mod:`prxref.chunk_context` and
:func:`prxref.rules.match_globs`, performs no I/O and no network, and
callers hand it file text they already read. It
imports ``chunk_context``'s underscore helpers (``_language``,
``_definition_regexes``, ``_keywords``, ``_entry_text``, ``_IDENT_RE``) on
purpose, so both modules scan and render a definition the same way instead of
drifting apart. Java lives here rather than in
``chunk_context._definition_regexes`` because ``referenced_definitions`` runs
with the feature off, and feature-off output must stay byte-identical.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from . import chunk_context
from .rules import match_globs

REASONS = ("cross-chunk", "contract", "diff-file", "import", "path-convention", "name-search", "shared-state")
KINDS = ("definition", "contract", "reader")

_JAVA_DEF_RE = re.compile(
    r"^\s*(?:(?:@(?!interface\b)[A-Za-z_$][\w$.]*(?:\((?:[^()]|\([^()]*\))*\))?"
    r"|public|protected|private|static|final|abstract|sealed|non-sealed|strictfp)\s+)*"
    r"(?:class|interface|record|enum|@interface)\s+([A-Za-z_$][A-Za-z0-9_$]*)"
)

_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+(?:static\s+)?((?:java|javax)\.[\w$.]*[\w$])(?:\.\*)?\s*;")

_JAVA_KEYWORDS = frozenset({
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char",
    "class", "const", "continue", "default", "do", "double", "else", "enum",
    "exports", "extends", "false", "final", "finally", "float", "for", "goto",
    "if", "implements", "import", "instanceof", "int", "interface", "long",
    "module", "native", "new", "non", "null", "open", "opens", "package",
    "permits", "private", "protected", "provides", "public", "record",
    "requires", "return", "sealed", "short", "static", "strictfp", "super",
    "switch", "synchronized", "this", "throw", "throws", "to", "transient",
    "transitive", "true", "try", "uses", "var", "void", "volatile", "when",
    "while", "with", "yield",
})

_JDK_NAMES = frozenset({
    "AbstractMap", "ArithmeticException", "ArrayDeque", "ArrayIndexOutOfBoundsException",
    "ArrayList", "Arrays", "AssertionError", "AtomicBoolean", "AtomicInteger",
    "AtomicLong", "AtomicReference", "AutoCloseable", "Base64", "BigDecimal",
    "BigInteger", "BiConsumer", "BiFunction", "BinaryOperator", "BiPredicate",
    "Boolean", "Byte", "Callable", "CharSequence", "Character", "Charset",
    "ChronoUnit", "Class", "ClassCastException", "Clock", "Cloneable",
    "Collection", "Collections", "Collectors", "Comparable", "Comparator",
    "CompletableFuture", "CompletionStage", "ConcurrentHashMap", "ConcurrentMap",
    "Consumer", "CountDownLatch", "Date", "DateTimeFormatter", "Deprecated",
    "Deque", "Double", "Duration", "Enum", "Error", "Exception", "ExecutorService",
    "Executors", "File", "Files", "Float", "Function", "FunctionalInterface",
    "Future", "HashMap", "HashSet", "IllegalArgumentException",
    "IllegalStateException", "IndexOutOfBoundsException", "InputStream", "Instant",
    "IntStream", "Integer", "InterruptedException", "IOException", "Iterable",
    "Iterator", "LinkedHashMap", "LinkedHashSet", "LinkedList", "List",
    "LocalDate", "LocalDateTime", "LocalTime", "Locale", "Long", "LongStream",
    "Map", "Matcher", "Math", "NoSuchElementException", "NullPointerException",
    "Number", "NumberFormatException", "Object", "Objects", "OffsetDateTime",
    "Optional", "OptionalDouble", "OptionalInt", "OptionalLong", "OutputStream",
    "Override", "Path", "Paths", "Pattern", "Period", "Predicate", "PriorityQueue",
    "Queue", "Random", "Reader", "Record", "ReentrantLock", "Runnable", "Runtime",
    "RuntimeException", "SafeVarargs", "Set", "Short", "SortedMap", "SortedSet",
    "StandardCharsets", "Stream", "String", "StringBuilder", "StringJoiner",
    "Supplier", "SuppressWarnings", "System", "Thread", "ThreadLocal", "Throwable",
    "TimeUnit", "TreeMap", "TreeSet", "UUID", "UnaryOperator",
    "UncheckedIOException", "UnsupportedOperationException", "URI", "URL", "Void",
    "Writer", "ZoneId", "ZoneOffset", "ZonedDateTime",
})


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
    """The definition language of ``path``: ``chunk_context``'s map plus ``"java"`` for ``.java``.

    Returns ``""`` for a path no language claims.
    """
    if path.lower().endswith(".java"):
        return "java"
    return chunk_context._language(path)


def definition_regexes(language: str) -> tuple[re.Pattern[str], ...]:
    """The definition regexes for ``language``; group 1 of a match is the defined name.

    ``"java"`` gets one regex for a type declaration (``class``, ``interface``,
    ``record``, ``enum`` or ``@interface``, after optional modifiers and
    same-line annotations); it never matches a method, a field or a local
    variable. Every other language delegates to ``chunk_context``, which covers
    js and python and returns ``()`` otherwise.
    """
    if language == "java":
        return (_JAVA_DEF_RE,)
    return chunk_context._definition_regexes(language)


def _jdk_imports(added: Iterable[str]) -> set[str]:
    names: set[str] = set()
    for text in added:
        match = _JAVA_IMPORT_RE.match(text)
        if match:
            names.update(match.group(1).split("."))
    return names


def _type_like(name: str) -> bool:
    return name[:1].isupper() and any(c.islower() for c in name)


def referenced_names(added: Iterable[str], language: str) -> list[str]:
    """Identifiers the added lines reference, in first-appearance order, deduplicated.

    Language keywords are removed, and so is every name a
    :func:`definition_regexes` match defines on the added lines themselves, as
    ``chunk_context.referenced_definitions`` does. For Java, JDK names are also
    removed (every segment of a ``java.*`` or ``javax.*`` import on the added
    lines, plus a built-in set of common ``java.lang``, ``java.util``,
    ``java.time`` and related types), and only type-like names are kept: an
    uppercase first letter and at least one lowercase letter, since a Java
    definition regex can only ever find a type. That drops constants, locals,
    methods and single-letter type parameters.
    """
    lines = list(added)
    java = language == "java"
    keywords = _JAVA_KEYWORDS if java else chunk_context._keywords(language)
    defined: set[str] = set()
    for text in lines:
        for regex in definition_regexes(language):
            match = regex.match(text)
            if match:
                defined.add(match.group(1))
    dropped = keywords | defined
    if java:
        dropped = dropped | _JDK_NAMES | _jdk_imports(lines)
    out: dict[str, None] = {}
    for text in lines:
        for name in chunk_context._IDENT_RE.findall(text):
            if name in dropped or name in out:
                continue
            if java and not _type_like(name):
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
