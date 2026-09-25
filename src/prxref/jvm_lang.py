"""Java and Kotlin language facts for JVM chunk context (#20).

A stdlib-only leaf module: it imports nothing from ``prxref``, so
``chunk_context`` and ``repo_context`` can both build on it without an import
cycle. It holds:

- :func:`jvm_language`, which maps ``.java`` to ``"java"`` and ``.kt`` and
  ``.kts`` to ``"kotlin"``;
- the definition regexes of each language, in the order a caller tries them
  (:data:`JAVA_DEFINITION_REGEXES`, :data:`KOTLIN_DEFINITION_REGEXES`). The
  first regex that matches a line decides it, and group 1 of a match is the
  defined name;
- the keyword sets (:data:`JAVA_KEYWORDS`, :data:`KOTLIN_KEYWORDS`) that keep
  ``public``, ``new``, ``return``, ``override`` and the like out of referenced
  names, and :data:`JDK_NAMES`, common ``java.*`` types a repository never
  defines;
- import parsing (:func:`parse_import`, :func:`parse_imports`) for plain,
  ``static``, wildcard and Kotlin ``as`` imports of ANY package, returned as
  :class:`JvmImport`;
- :func:`annotation_start`, the lookback that lets a definition entry begin at
  the ``@X`` lines directly above it.

:data:`JAVA_TYPE_RE` matches type declarations only (``class``, ``interface``,
``record``, ``enum``, ``@interface``). :data:`JAVA_METHOD_RE` matches a method
with a return type, so a constructor is left to its class's type declaration.
:data:`JAVA_FIELD_RE` matches a declaration ending in ``=`` or ``;``, which a
local variable also is; ``var`` locals are excluded because ``var`` cannot
declare a field. :data:`JAVA_ENUM_CONSTANT_RE` matches an upper-case name,
optionally with arguments or a body, followed by ``,``, ``;``, ``{`` or the end
of the line; an upper-case constant alone on an argument or array-initializer
line matches it too. The Kotlin regexes are led by ``class``, ``interface``,
``object``, ``typealias``, ``fun``, ``val`` or ``var``, so a nameless
``companion object`` matches none of them.

Every regex works on one line: a declaration split across lines is recognised
on its first line only, and a comment or string that spans lines is not known
to be one.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

MAX_ANNOTATION_LINES = 2

JAVA_TYPE_RE = re.compile(
    r"^\s*(?:(?:@(?!interface\b)[A-Za-z_$][\w$.]*(?:\((?:[^()]|\([^()]*\))*\))?"
    r"|public|protected|private|static|final|abstract|sealed|non-sealed|strictfp)\s+)*"
    r"(?:class|interface|record|enum|@interface)\s+([A-Za-z_$][A-Za-z0-9_$]*)"
)

JAVA_KEYWORDS = frozenset({
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

JDK_NAMES = frozenset({
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

KOTLIN_KEYWORDS = frozenset({
    "abstract", "actual", "annotation", "as", "break", "by", "catch", "class",
    "companion", "const", "constructor", "continue", "crossinline", "data",
    "delegate", "do", "dynamic", "else", "enum", "expect", "external", "false",
    "field", "file", "final", "finally", "for", "fun", "get", "if", "import",
    "in", "infix", "init", "inline", "inner", "interface", "internal", "is",
    "it", "lateinit", "noinline", "null", "object", "open", "operator", "out",
    "override", "package", "param", "private", "property", "protected",
    "public", "receiver", "reified", "return", "sealed", "set", "setparam",
    "super", "suspend", "tailrec", "this", "throw", "true", "try", "typealias",
    "typeof", "val", "value", "var", "vararg", "when", "where", "while",
})

_JAVA_PRIMITIVES = frozenset({"boolean", "byte", "char", "double", "float", "int", "long", "short", "void"})

_JAVA_RESERVED = frozenset({
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char",
    "class", "const", "continue", "default", "do", "double", "else", "enum",
    "extends", "false", "final", "finally", "float", "for", "goto", "if",
    "implements", "import", "instanceof", "int", "interface", "long", "native",
    "new", "null", "package", "private", "protected", "public", "return",
    "short", "static", "strictfp", "super", "switch", "synchronized", "this",
    "throw", "throws", "transient", "true", "try", "void", "volatile", "while",
})

_JAVA_MODIFIERS = (
    "public", "protected", "private", "static", "final", "abstract", "sealed", "non-sealed",
    "strictfp", "default", "synchronized", "native", "transient", "volatile",
)

_KOTLIN_MODIFIERS = (
    "public", "private", "protected", "internal", "open", "final", "abstract", "sealed",
    "override", "data", "enum", "annotation", "inner", "value", "companion", "const",
    "lateinit", "inline", "suspend", "operator", "infix", "tailrec", "external", "expect",
    "actual",
)


def _none_of(words: Iterable[str]) -> str:
    return r"(?!(?:" + "|".join(re.escape(w) for w in sorted(words)) + r")(?![\w$]))"


_GENERIC = r"<(?:[^<>;=(){}]|<(?:[^<>;=(){}]|<[^<>;=(){}]*>)*>)*>"
_PARENS = r"\((?:[^()]|\([^()]*\))*\)"

_JAVA_IDENT = r"[A-Za-z_$][\w$]*"
_JAVA_ANNOTATION = r"@(?!interface\b)[A-Za-z_$][\w$.]*(?:" + _PARENS + r")?"
_JAVA_PREFIX = r"^\s*(?:(?:" + _JAVA_ANNOTATION + "|" + "|".join(_JAVA_MODIFIERS) + r")\s+)*"
_JAVA_TYPE = (
    _JAVA_IDENT + r"(?:" + _GENERIC + r")?"
    r"(?:\." + _JAVA_IDENT + r"(?:" + _GENERIC + r")?)*"
    r"(?:\s*\[\s*\])*"
)
_JAVA_TYPED_NAME = (
    _none_of(JAVA_KEYWORDS - _JAVA_PRIMITIVES) + _JAVA_TYPE + r"\s+"
    + _none_of(_JAVA_RESERVED) + r"(" + _JAVA_IDENT + r")"
)

JAVA_METHOD_RE = re.compile(_JAVA_PREFIX + r"(?:" + _GENERIC + r"\s*)?" + _JAVA_TYPED_NAME + r"\s*\(")

JAVA_FIELD_RE = re.compile(_JAVA_PREFIX + _JAVA_TYPED_NAME + r"\s*(?:\[\s*\]\s*)*(?:=(?!=)|;)")

JAVA_ENUM_CONSTANT_RE = re.compile(
    r"^\s*(?:" + _JAVA_ANNOTATION + r"\s+)*([A-Z][A-Z0-9_]*)(?![\w$])\s*"
    r"(?:" + _PARENS + r"|\([^()]*$)?\s*(?:[,;{]|//|/\*|$)"
)

JAVA_DEFINITION_REGEXES = (JAVA_TYPE_RE, JAVA_METHOD_RE, JAVA_FIELD_RE, JAVA_ENUM_CONSTANT_RE)

_KOTLIN_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_KOTLIN_ANNOTATION = r"@(?:[A-Za-z]+:)?[A-Za-z_][\w.]*(?:" + _PARENS + r")?"
_KOTLIN_PREFIX = r"^\s*(?:(?:" + _KOTLIN_ANNOTATION + "|" + "|".join(_KOTLIN_MODIFIERS) + r")\s+)*"
_KOTLIN_RECEIVER = (
    r"(?:" + _KOTLIN_IDENT + r"(?:" + _GENERIC + r")?"
    r"(?:\." + _KOTLIN_IDENT + r"(?:" + _GENERIC + r")?)*\??\.)?"
)

KOTLIN_TYPE_RE = re.compile(
    _KOTLIN_PREFIX + r"(?:fun\s+)?(?:class|interface|object|typealias)\s+(" + _KOTLIN_IDENT + r")"
)

KOTLIN_FUN_RE = re.compile(
    _KOTLIN_PREFIX + r"fun\s+(?:" + _GENERIC + r"\s*)?" + _KOTLIN_RECEIVER
    + r"(" + _KOTLIN_IDENT + r")\s*\("
)

KOTLIN_PROPERTY_RE = re.compile(
    _KOTLIN_PREFIX + r"(?:val|var)\s+(?:" + _GENERIC + r"\s*)?" + _KOTLIN_RECEIVER
    + r"(" + _KOTLIN_IDENT + r")(?![\w.])"
)

KOTLIN_DEFINITION_REGEXES = (KOTLIN_TYPE_RE, KOTLIN_FUN_RE, KOTLIN_PROPERTY_RE)

_IMPORT_RE = re.compile(
    r"^\s*import\s+(static\s+)?(" + _JAVA_IDENT + r"(?:\." + _JAVA_IDENT + r")*)(\.\*)?"
    r"(?:\s+as\s+(" + _JAVA_IDENT + r"))?\s*;?\s*(?://.*|/\*.*)?$"
)

_ANNOTATION_LINE_RE = re.compile(
    r"^\s*(?:@(?!interface\b)(?:[A-Za-z]+:)?[A-Za-z_$][\w$.]*(?:" + _PARENS + r")?\s*)+"
    r"(?://.*|/\*.*)?$"
)


def jvm_language(path: str) -> str:
    """``"java"`` for a ``.java`` path, ``"kotlin"`` for ``.kt`` or ``.kts``, else ``""``.

    The suffix check ignores case, as ``repo_context.language_of`` does.
    """
    lower = path.lower()
    if lower.endswith(".java"):
        return "java"
    if lower.endswith((".kt", ".kts")):
        return "kotlin"
    return ""


def definition_regexes(language: str) -> tuple[re.Pattern[str], ...]:
    """The definition regexes of ``language`` in the order to try them; ``()`` for a non-JVM language.

    Java: types, methods, fields and constants, enum constants. Kotlin: types
    (``class`` in every flavour, ``interface``, ``object``, ``typealias``),
    ``fun``, then ``val``/``var``. Group 1 of a match is the defined name.
    """
    if language == "java":
        return JAVA_DEFINITION_REGEXES
    if language == "kotlin":
        return KOTLIN_DEFINITION_REGEXES
    return ()


def keywords(language: str) -> frozenset[str]:
    """The keyword set of ``language``; an empty set for a non-JVM language."""
    if language == "java":
        return JAVA_KEYWORDS
    if language == "kotlin":
        return KOTLIN_KEYWORDS
    return frozenset()


@dataclass(frozen=True)
class JvmImport:
    """One parsed ``import`` line of a Java or Kotlin file.

    ``name`` is the qualified name as written: a type (``a.b.C``), a static
    member (``a.b.C.m``), or, for a wildcard import, the package or type before
    ``.*``. ``alias`` is the Kotlin ``as`` name, or ``""``.
    """

    name: str
    alias: str = ""
    static: bool = False
    wildcard: bool = False

    @property
    def segments(self) -> tuple[str, ...]:
        """``name`` split on ``.``."""
        return tuple(self.name.split("."))


def parse_import(line: str) -> JvmImport | None:
    """Parse one Java or Kotlin ``import`` line; ``None`` when the line is not one.

    Accepts ``import a.b.C;``, ``import static a.b.C.m;``, ``import a.b.*;``,
    ``import static a.b.C.*;``, the Kotlin forms without the semicolon, and
    Kotlin ``import a.b.C as D``. Any package qualifies, not only the JDK's; a
    trailing comment is allowed.
    """
    match = _IMPORT_RE.match(line)
    if not match:
        return None
    return JvmImport(
        name=match.group(2),
        alias=match.group(4) or "",
        static=bool(match.group(1)),
        wildcard=bool(match.group(3)),
    )


def parse_imports(lines: Iterable[str]) -> list[JvmImport]:
    """Every import among ``lines``, in order, one :class:`JvmImport` per import line."""
    out: list[JvmImport] = []
    for line in lines:
        parsed = parse_import(line)
        if parsed is not None:
            out.append(parsed)
    return out


def annotation_start(lines: Sequence[str], index: int, *, max_annotations: int = MAX_ANNOTATION_LINES) -> int:
    """The index a definition entry starts at: its first directly preceding annotation line.

    Walks up from ``lines[index]`` over at most ``max_annotations`` contiguous
    lines that hold only annotations (``@X``, ``@X(...)``, a Kotlin use-site
    target such as ``@field:X``, several on one line, an optional trailing
    comment) and returns the index of the topmost one. A blank line, a comment
    or any other line stops the walk, and ``@interface`` is a declaration, not
    an annotation. Returns ``index`` when nothing qualifies.
    """
    start = index
    cursor = index - 1
    while 0 <= cursor < len(lines) and index - cursor <= max_annotations:
        if not _ANNOTATION_LINE_RE.match(lines[cursor]):
            break
        start = cursor
        cursor -= 1
    return start
