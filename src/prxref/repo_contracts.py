"""Contract excerpts for repository context: the OpenAPI, JSON Schema, SQL and
Liquibase slices that a changed route, operation, schema or table points at.

A diff that adds a route handler, a migration or a DTO often changes behavior
that a contract file elsewhere in the repository pins down: the OpenAPI
operation for the route, the schema a payload must satisfy, the unique index on
a table. The excerpters here cut the matching slice out of such a file so a
worker can check the change against it.

The module is pure. It is stdlib plus the pure :mod:`prxref.repo_context`,
:mod:`prxref.chunk_context` and :func:`prxref.rules.match_globs`, and it
performs no I/O: callers pass the file's text, and :func:`contract_entries`
reads only through the ``read`` callable it is handed. There is deliberately
no YAML parser, because the core ships none, so YAML is sliced by indentation,
JSON goes through :mod:`json`, and SQL and XML are scanned with regular
expressions. Every excerpter degrades to ``[]`` on text it cannot read rather
than raising, because a review must never fail over missing context. Each
excerpt is capped at :data:`MAX_CONTRACT_LINES` lines and
:data:`MAX_CONTRACT_CHARS` characters.

Names compare through :func:`normalize_name`, so a table called
``idempotency_keys`` matches a schema or class called ``IdempotencyKey``.
Routes compare through :func:`route_key`, so the code route
``/connectors/:id`` matches the spec path ``/connectors/{connectorId}``.

The second half of the module turns a worker chunk into contract entries.
:func:`contract_triggers` reads a changed file's added lines for the routes,
tables, names and operation ids a contract can match.
:func:`select_contract_files` picks the repository's contract files from the
configured globs once per run, and :func:`literal_contract_paths` names the
globs that are plain paths. :func:`earlier_migrations` finds the migrations a
changed one builds on, :func:`contract_excerpts` sends one contract file to
the excerpter its format needs, and :func:`contract_entries` ties them
together for one chunk as :class:`prxref.repo_context.ContextEntry` records.
Every regular expression here runs in linear time on a 512 KiB line.
"""
from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Callable, Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass

from . import chunk_context
from .repo_context import _JAVA_KEYWORDS, ContextEntry, language_of, referenced_names
from .rules import match_globs

MAX_CONTRACT_LINES = 40
MAX_CONTRACT_CHARS = 2000
MAX_EARLIER_MIGRATIONS = 4
MAX_SPEC_FILES = 6

_BOM = chr(0xFEFF)
_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
_TABLE_KEYS = frozenset({"tableName", "baseTableName", "referencedTableName"})

_BRACE_PARAM = re.compile(r"\{[^{}/]*\}")
_ANGLE_PARAM = re.compile(r"<[^<>/]*>")

_YAML_KEY = re.compile(
    r"""(?P<indent>\ *)(?P<dash>(?:-[ \t]+)*)"""
    r"""(?P<key>"(?:[^"\\]|\\.)*"[ \t]*|'(?:[^']|'')*'[ \t]*"""
    r"""|(?:[^\s#"'\-\[\]{},?&*!|>%@`]|-(?=\S))(?:[^#:]|:(?![ \t]|$))*):(?:[ \t]|$)"""
)
_YAML_QUOTED = re.compile(r""""(?:[^"\\]|\\.)*"|'(?:[^']|'')*'""")
_YAML_BLOCK_SCALAR = re.compile(r"(?:^[ \t]*(?:-[ \t]+)+|:[ \t]+)[|>][-+1-9]{0,2}[ \t]*(?:#.*)?$")

_SQL_TOKEN = re.compile(
    r"(?P<changeset>--[ \t]*changeset\b[^\n]*)"
    r"|(?P<comment>--[^\n]*|/\*.*?(?:\*/|\Z))"
    r"|(?P<quoted>'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`)"
    r"|(?P<dollar>\$(?P<tag>[A-Za-z_]\w*|)\$.*?\$(?P=tag)\$)"
    r"|(?P<go>^[ \t]*go[ \t\r]*$)"
    r"|(?P<end>;)",
    re.S | re.M | re.I,
)
_SQL_PART = r"""(?:"[^"]+"|`[^`]+`|\[[^\]]+\]|[\w$]+)"""
_SQL_NAME = rf"(?P<name>{_SQL_PART}(?:\s*\.\s*{_SQL_PART})*)"
_SQL_HEADS = (
    re.compile(
        r"create\s+(?:or\s+replace\s+)?(?:(?:global|local)\s+)?(?:(?:temp|temporary|unlogged)\s+)?"
        rf"table\s+(?:if\s+not\s+exists\s+)?{_SQL_NAME}",
        re.I,
    ),
    re.compile(rf"alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?{_SQL_NAME}", re.I),
    re.compile(
        r"create\s+(?:unique\s+)?(?:(?:clustered|nonclustered|bitmap|fulltext|spatial)\s+)?"
        rf"index\b[^;]*?\bon\s+(?:only\s+)?{_SQL_NAME}",
        re.I,
    ),
)
_SQL_NAME_PART = re.compile(_SQL_PART)
_NON_SPACE = re.compile(r"\S")

_XML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.S)
_XML_CHANGESET = re.compile(
    r"""<(?:[\w.-]+:)?changeSet\b(?:"[^"]*"|'[^']*'|[^'">/]|/(?!>))*(?:/>|>.*?</(?:[\w.-]+:)?changeSet\s*>)""",
    re.S,
)
_XML_TABLE_ATTR = re.compile(r"""\b(?:tableName|baseTableName|referencedTableName)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


@dataclass(frozen=True)
class Excerpt:
    """One slice of a contract file.

    ``line`` is the 1-based line the slice starts on (best effort for JSON, and
    1 when unknown), ``symbol`` is what matched, written as the file writes it,
    and ``text`` is the slice, already capped to :data:`MAX_CONTRACT_LINES` and
    :data:`MAX_CONTRACT_CHARS`.
    """

    line: int
    symbol: str
    text: str


def normalize_name(name: str) -> str:
    """Fold a type, schema or table name so its spellings compare equal.

    The name is lowercased, every ``_`` and ``-`` is dropped, and ONE trailing
    ``s`` is stripped, so ``idempotency_keys``, ``idempotency-key`` and
    ``IdempotencyKey`` all give ``idempotencykey``. Excerpters treat a name
    that folds to the empty string as matching nothing.
    """
    folded = name.lower().replace("_", "").replace("-", "")
    return folded[:-1] if folded.endswith("s") else folded


def route_key(route: str) -> str:
    """Normalize an HTTP route template so a code route and a spec path compare equal.

    Every parameter becomes ``{}``: an OpenAPI or Spring ``{id}`` or
    ``{connectorId}``, a Flask ``<id>`` or ``<int:id>``, and an Express
    segment that starts with ``:`` (``:id``, ``:id?``). A parameter inside a
    segment is replaced in place (``/files/{name}.json`` gives
    ``/files/{}.json``). Empty segments are dropped, which collapses duplicate
    slashes and strips a trailing slash, and the result always starts with
    ``/``. Case is kept. ``/`` gives ``/``; a blank route gives ``""``, which
    excerpters treat as matching nothing.
    """
    segments = [segment for segment in route.strip().split("/") if segment]
    if not segments:
        return "/" if route.strip() else ""
    keyed = [
        "{}" if segment.startswith(":") else _ANGLE_PARAM.sub("{}", _BRACE_PARAM.sub("{}", segment))
        for segment in segments
    ]
    return "/" + "/".join(keyed)


def openapi_yaml_excerpts(
    text: str,
    *,
    routes: Iterable[str] = (),
    operation_ids: Iterable[str] = (),
    schemas: Iterable[str] = (),
) -> list[Excerpt]:
    """Slice the matching parts out of an OpenAPI or Swagger document written in YAML.

    There is no YAML parser: each match is an indentation slice, the key line
    plus every following line indented deeper than it, with blank and comment
    lines inside kept and trailing ones trimmed. The slice is dedented by the
    key's indentation. Lines inside a ``|`` or ``>`` block scalar are text,
    never keys.

    - A route matches a key directly under the top-level ``paths:`` when the
      two :func:`route_key` values are equal, and yields that path item.
    - An operation id matches an ``operationId: <id>`` line, quoted or not,
      whose enclosing key is an HTTP method (``get:``, ``post:`` and so on),
      and yields that operation block. An ``operationId`` under ``links:`` is
      a reference, not an operation, and is ignored.
    - A schema name matches a key directly under ``components:`` then
      ``schemas:`` (or under the top-level ``definitions:`` of Swagger 2.0)
      when the :func:`normalize_name` values are equal, and yields that schema.

    ``line`` is the key line's 1-based number. ``symbol`` is the spec's own
    path key, operation id or schema key, unquoted. Results come in line order
    with no duplicates: an excerpt is dropped when another one already shows
    every line of it, so a path item found by route absorbs an operation found
    by id, unless the path item's caps cut that operation off. A text whose
    first character is ``{`` is JSON, which is also YAML, and goes through
    :func:`openapi_json_excerpts`.
    """
    text = text.removeprefix(_BOM)
    if text.lstrip().startswith("{"):
        return openapi_json_excerpts(text, routes=routes, operation_ids=operation_ids, schemas=schemas)
    route_keys = _keys(routes, route_key)
    op_ids = _keys(operation_ids, str.strip)
    schema_keys = _keys(schemas, normalize_name)
    if not (route_keys or op_ids or schema_keys):
        return []
    doc = _Yaml(text)
    top = doc.top_level()
    found: list[tuple[int, str]] = []
    if route_keys and "paths" in top:
        found += [(i, doc.key[i]) for i in doc.children(top["paths"]) if route_key(doc.key[i]) in route_keys]
    if op_ids:
        for i, key in enumerate(doc.key):
            if key != "operationId" or doc.value[i] not in op_ids:
                continue
            parent = doc.parent(i)
            if parent is not None and doc.key[parent].lower() in _HTTP_METHODS:
                found.append((parent, doc.value[i]))
    if schema_keys:
        containers: list[int] = []
        if "components" in top:
            containers += [i for i in doc.children(top["components"]) if doc.key[i] == "schemas"]
        if "definitions" in top:
            containers.append(top["definitions"])
        for container in containers:
            found += [(i, doc.key[i]) for i in doc.children(container) if normalize_name(doc.key[i]) in schema_keys]
    return _line_excerpts(doc.spans(found))


def openapi_json_excerpts(
    text: str,
    *,
    routes: Iterable[str] = (),
    operation_ids: Iterable[str] = (),
    schemas: Iterable[str] = (),
) -> list[Excerpt]:
    """Slice the matching parts out of an OpenAPI or Swagger document written in JSON.

    The semantics are :func:`openapi_yaml_excerpts`'s, through
    :func:`json.loads`: a route matches a key of ``paths``, an operation id an
    HTTP-method object whose ``operationId`` equals it, and a schema name a key
    of ``components.schemas`` or of the Swagger 2.0 ``definitions``.

    ``text`` is the matched member rendered as ``"<key>": `` followed by
    ``json.dumps(node, indent=2)``, capped the same way. The key is kept
    because a rendered context entry shows only ``path:line: text``, and a bare
    ``{`` would not say which path, operation or schema it is. ``line`` is best
    effort: the line of the matched key found by following its key path
    through the source (``paths``, then the path, then the method), else the
    line of the deepest ancestor found, else 1. Results come in line order
    with no duplicates; here a container absorbs what it contains only when it
    is shown whole. Invalid JSON, or a document that is not an object, gives
    ``[]``.
    """
    text = text.removeprefix(_BOM)
    route_keys = _keys(routes, route_key)
    op_ids = _keys(operation_ids, str.strip)
    schema_keys = _keys(schemas, normalize_name)
    if not (route_keys or op_ids or schema_keys):
        return []
    doc = _load_json(text)
    if not isinstance(doc, dict):
        return []
    hits: list[_Hit] = []
    paths = doc.get("paths")
    if route_keys and isinstance(paths, dict):
        hits += [
            _json_hit(text, ("paths", key), key, key, item)
            for key, item in paths.items()
            if route_key(key) in route_keys
        ]
    if op_ids:
        for pointer, node in _walk(doc):
            key = pointer[-1] if pointer else None
            if not (isinstance(key, str) and key.lower() in _HTTP_METHODS and isinstance(node, dict)):
                continue
            op_id = node.get("operationId")
            if isinstance(op_id, str) and op_id in op_ids:
                hits.append(_json_hit(text, pointer, op_id, key, node))
    if schema_keys:
        containers: list[tuple[tuple[str, ...], dict]] = []
        components = doc.get("components")
        if isinstance(components, dict) and isinstance(components.get("schemas"), dict):
            containers.append((("components", "schemas"), components["schemas"]))
        if isinstance(doc.get("definitions"), dict):
            containers.append((("definitions",), doc["definitions"]))
        for base, members in containers:
            hits += [
                _json_hit(text, (*base, name), name, name, node)
                for name, node in members.items()
                if normalize_name(name) in schema_keys
            ]
    return _json_excerpts(hits)


def json_schema_excerpts(text: str, *, names: Iterable[str] = ()) -> list[Excerpt]:
    """Slice the matching schemas out of a JSON Schema document.

    A name matches the document itself when it equals the root ``title`` by
    :func:`normalize_name`, with whitespace in the title dropped first, so
    ``transport_requests`` matches ``"title": "Transport Request"``; that
    excerpt is the whole document at line 1. A name also matches any member
    of a ``$defs`` or ``definitions`` object at any depth (not one that is a
    property called ``definitions``), rendered and located as in
    :func:`openapi_json_excerpts`. Results come in line order with no
    duplicates; the root, when shown whole, absorbs its own definitions.
    Invalid JSON, or a document that is not an object, gives ``[]``.
    """
    text = text.removeprefix(_BOM)
    keys = _keys(names, normalize_name)
    if not keys:
        return []
    doc = _load_json(text)
    if not isinstance(doc, dict):
        return []
    hits: list[_Hit] = []
    title = doc.get("title")
    if isinstance(title, str) and normalize_name("".join(title.split())) in keys:
        hits.append(_Hit((), title, None, doc, 1))
    for pointer, node in _walk(doc):
        if not pointer or pointer[-1] not in ("$defs", "definitions") or not isinstance(node, dict):
            continue
        if len(pointer) > 1 and pointer[-2] in ("properties", "patternProperties"):
            continue
        hits += [
            _json_hit(text, (*pointer, name), name, name, member)
            for name, member in node.items()
            if normalize_name(name) in keys
        ]
    return _json_excerpts(hits)


def sql_excerpts(text: str, *, tables: Iterable[str] = ()) -> list[Excerpt]:
    """Slice the statements that touch a table out of a SQL script or migration.

    Three statement kinds count, case-insensitively: ``CREATE TABLE``
    (``OR REPLACE``, ``TEMP``, ``UNLOGGED`` and ``IF NOT EXISTS`` allowed),
    ``ALTER TABLE`` (``IF EXISTS`` and ``ONLY`` allowed), and
    ``CREATE [UNIQUE] INDEX … ON <table>``. The table matches when its bare
    name (the last dot-separated part, with ``"``, backticks and ``[]``
    removed) equals a requested table by :func:`normalize_name`.

    A statement runs from its keyword to its terminating ``;``. A ``;`` inside
    a ``--`` or ``/* */`` comment, a quoted string or identifier, or a
    ``$$`` body does not end it. A Liquibase formatted-SQL ``--changeset`` line
    and a batch-separator line ``GO`` also end a statement, so a changeset's
    last statement may omit its ``;``; trailing comments such as
    ``--rollback`` stay outside it. Comment lines, including the
    ``--liquibase formatted sql`` header, never start one. ``line`` is the
    keyword's 1-based line and ``symbol`` the bare table name as written.
    Results come in file order.
    """
    text = text.removeprefix(_BOM)
    keys = _keys(tables, normalize_name)
    if not keys:
        return []
    out: list[Excerpt] = []
    for start, end, name in _sql_statements(text):
        bare = _bare_table(name)
        if normalize_name(bare) in keys:
            capped, _ = _cap(_slice_lines(text, start, end))
            out.append(Excerpt(text.count("\n", 0, start) + 1, bare, capped))
    return out


def liquibase_excerpts(text: str, *, tables: Iterable[str] = ()) -> list[Excerpt]:
    """Slice the changeSets that touch a table out of a Liquibase changelog.

    The format follows the first non-blank character: ``<`` is XML, ``{`` or
    ``[`` is JSON, anything else is YAML. A changeSet matches when it holds a
    ``tableName``, ``baseTableName`` or ``referencedTableName`` (an XML
    attribute, a YAML key or a JSON member) equal to a requested table by
    :func:`normalize_name`.

    - XML yields the ``<changeSet …>`` element through ``</changeSet>``;
      changeSets inside ``<!-- -->`` comments are ignored.
    - YAML yields the indentation slice of the ``changeSet:`` line (usually
      ``- changeSet:``), as :func:`openapi_yaml_excerpts` slices a key.
    - JSON yields ``"changeSet": `` plus the member's ``json.dumps(indent=2)``.

    ``line`` is the changeSet's 1-based line (for JSON, the line of its own
    ``"changeSet"`` key) and ``symbol`` the first matching table name as
    written. Formatted-SQL changelogs go through :func:`sql_excerpts`.
    Results come in file order; invalid JSON gives ``[]``.
    """
    text = text.removeprefix(_BOM)
    keys = _keys(tables, normalize_name)
    if not keys:
        return []
    head = text.lstrip()[:1]
    if head == "<":
        return _liquibase_xml(text, keys)
    if head in ("{", "["):
        return _liquibase_json(text, keys)
    doc = _Yaml(text)
    found: list[tuple[int, str]] = []
    for i, key in enumerate(doc.key):
        if key != "changeSet":
            continue
        for j in range(i + 1, doc.block_end(i) + 1):
            if doc.key[j] in _TABLE_KEYS and normalize_name(doc.value[j]) in keys:
                found.append((i, doc.value[j]))
                break
    return _line_excerpts(doc.spans(found))


def _keys(values: Iterable[str], fold: Callable[[str], str]) -> frozenset[str]:
    return frozenset(key for key in map(fold, values) if key)


def _cap(lines: list[str]) -> tuple[str, int]:
    """``(text, shown)``: the capped text and how many source lines it shows whole.

    A cut block keeps as many leading lines as fit and ends with an
    ``… N more lines`` line, which counts toward both caps. A first line too
    long to fit even alone is itself cut and ends with ``…``.
    """
    text = "\n".join(lines)
    if len(lines) <= MAX_CONTRACT_LINES and len(text) <= MAX_CONTRACT_CHARS:
        return text, len(lines)
    for keep in range(min(len(lines), MAX_CONTRACT_LINES) - 1, 0, -1):
        capped = "\n".join([*lines[:keep], f"… {len(lines) - keep} more lines"])
        if len(capped) <= MAX_CONTRACT_CHARS:
            return capped, keep
    if len(lines) == 1:
        return lines[0][: MAX_CONTRACT_CHARS - 1] + "…", 0
    marker = f"… {len(lines) - 1} more lines"
    return f"{lines[0][: MAX_CONTRACT_CHARS - len(marker) - 2]}…\n{marker}", 0


def _dedent(line: str, width: int) -> str:
    return line[width:] if not line[:width].strip() else line.lstrip()


def _slice_lines(text: str, start: int, end: int) -> list[str]:
    """The source between two offsets as lines, dedented by the start's column."""
    column = start - (text.rfind("\n", 0, start) + 1)
    lines = [line.rstrip() for line in text[start:end].split("\n")]
    return [lines[0], *(_dedent(line, column) for line in lines[1:])]


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    shown: int
    symbol: str
    text: str


def _covers(outer: _Span, inner: _Span) -> bool:
    """True when ``outer`` already shows every line ``inner`` shows."""
    if (outer.start, outer.end) == (inner.start, inner.end):
        return True
    return (
        outer.start <= inner.start
        and inner.end <= outer.end
        and inner.start + max(inner.shown, 1) <= outer.start + outer.shown
    )


def _line_excerpts(spans: list[_Span]) -> list[Excerpt]:
    kept: list[_Span] = []
    for span in sorted(spans, key=lambda s: (s.start, -s.end)):
        if not any(_covers(other, span) for other in kept):
            kept.append(span)
    return [Excerpt(span.start + 1, span.symbol, span.text) for span in kept]


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] == '"':
        try:
            value = json.loads(token)
        except ValueError:
            return token[1:-1]
        return value if isinstance(value, str) else token[1:-1]
    if len(token) >= 2 and token[0] == token[-1] == "'":
        return token[1:-1].replace("''", "'")
    return token


def _yaml_scalar(rest: str) -> str:
    rest = rest.strip()
    quoted = _YAML_QUOTED.match(rest)
    if quoted:
        return _unquote(quoted.group())
    return rest.split(" #", 1)[0].rstrip()


class _Yaml:
    """Per-line indentation facts about a YAML text, enough for indentation slices."""

    def __init__(self, text: str) -> None:
        self.lines = [line.rstrip() for line in text.split("\n")]
        self.indent: list[int] = []
        self.key: list[str | None] = []
        self.value: list[str] = []
        self.dash: list[bool] = []
        self.filler: list[bool] = []
        self.scalar: list[bool] = []
        scalar_floor: int | None = None
        for line in self.lines:
            stripped = line.lstrip(" ")
            indent = len(line) - len(stripped)
            in_scalar = scalar_floor is not None and (not stripped or indent > scalar_floor)
            if not in_scalar:
                scalar_floor = None
            filler = not stripped or (not in_scalar and stripped.startswith("#"))
            match = None if in_scalar or filler else _YAML_KEY.match(line)
            self.indent.append(indent)
            self.key.append(_unquote(match.group("key").rstrip()) if match else None)
            self.value.append(_yaml_scalar(line[match.end():]) if match else "")
            self.dash.append(bool(match and match.group("dash")))
            self.filler.append(filler)
            self.scalar.append(in_scalar)
            if not in_scalar and not filler and _YAML_BLOCK_SCALAR.search(line):
                scalar_floor = indent

    def _content(self, index: int) -> bool:
        return not self.filler[index] and not self.scalar[index]

    def top_level(self) -> dict[str, int]:
        """Each column-0 mapping key's first line."""
        top: dict[str, int] = {}
        for i, key in enumerate(self.key):
            if key is not None and self.indent[i] == 0 and not self.dash[i] and self._content(i):
                top.setdefault(key, i)
        return top

    def block_end(self, start: int) -> int:
        """The last content line of the block the key on ``start`` opens."""
        last = start
        for j in range(start + 1, len(self.lines)):
            if self.filler[j]:
                continue
            if not self.scalar[j] and self.indent[j] <= self.indent[start]:
                break
            last = j
        return last

    def children(self, parent: int) -> list[int]:
        """The mapping keys directly under the key on ``parent``."""
        out: list[int] = []
        child_indent: int | None = None
        for j in range(parent + 1, self.block_end(parent) + 1):
            if not self._content(j):
                continue
            if child_indent is None:
                child_indent = self.indent[j]
            if self.indent[j] == child_indent and self.key[j] is not None and not self.dash[j]:
                out.append(j)
        return out

    def parent(self, index: int) -> int | None:
        """The key line one level above ``index``, or None when that line holds no key."""
        for j in range(index - 1, -1, -1):
            if self._content(j) and self.indent[j] < self.indent[index]:
                return j if self.key[j] is not None else None
        return None

    def spans(self, found: Iterable[tuple[int, str]]) -> list[_Span]:
        out: list[_Span] = []
        for start, symbol in found:
            end = self.block_end(start)
            width = self.indent[start]
            text, shown = _cap([_dedent(line, width) for line in self.lines[start : end + 1]])
            out.append(_Span(start, end, shown, symbol, text))
        return out


def _load_json(text: str) -> object:
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _walk(root: object) -> Iterator[tuple[tuple[str | int, ...], object]]:
    """Every node under ``root`` with its key path, in document order."""
    stack: list[tuple[tuple[str | int, ...], object]] = [((), root)]
    while stack:
        pointer, node = stack.pop()
        yield pointer, node
        if isinstance(node, dict):
            stack.extend(((*pointer, key), value) for key, value in reversed(list(node.items())))
        elif isinstance(node, list):
            stack.extend(((*pointer, i), value) for i, value in reversed(list(enumerate(node))))


def _json_key_re(key: str) -> re.Pattern[str]:
    forms = dict.fromkeys((json.dumps(key, ensure_ascii=False), json.dumps(key)))
    return re.compile("(?:" + "|".join(map(re.escape, forms)) + r")\s*:")


def _json_line(text: str, pointer: Iterable[str | int]) -> int:
    """Best-effort line of a key path: each string key is searched after its parent."""
    position, found = 0, -1
    for key in pointer:
        if not isinstance(key, str):
            continue
        match = _json_key_re(key).search(text, position)
        if match is None:
            break
        found, position = match.start(), match.end()
    return text.count("\n", 0, found) + 1 if found >= 0 else 1


@dataclass(frozen=True)
class _Hit:
    pointer: tuple[str | int, ...]
    symbol: str
    label: str | None
    node: object
    line: int


def _json_hit(text: str, pointer: tuple[str | int, ...], symbol: str, label: str | None, node: object) -> _Hit:
    return _Hit(pointer, symbol, label, node, _json_line(text, pointer))


def _json_excerpts(hits: list[_Hit]) -> list[Excerpt]:
    rendered: list[tuple[_Hit, int, str, bool]] = []
    for order, hit in enumerate(hits):
        body = json.dumps(hit.node, indent=2, ensure_ascii=False)
        if hit.label is not None:
            body = f"{json.dumps(hit.label, ensure_ascii=False)}: {body}"
        lines = body.split("\n")
        text, shown = _cap(lines)
        rendered.append((hit, order, text, shown < len(lines)))
    kept: list[tuple[_Hit, int, str, bool]] = []
    for entry in sorted(rendered, key=lambda e: len(e[0].pointer)):
        pointer = entry[0].pointer
        if any(
            other[0].pointer == pointer or (not other[3] and pointer[: len(other[0].pointer)] == other[0].pointer)
            for other in kept
        ):
            continue
        kept.append(entry)
    kept.sort(key=lambda e: (e[0].line, e[1]))
    return [Excerpt(hit.line, hit.symbol, text) for hit, _, text, _ in kept]


def _blank(fragment: str) -> str:
    return re.sub(r"[^\n]", " ", fragment)


def _sql_statements(text: str) -> Iterator[tuple[int, int, str]]:
    """``(start, end, table)`` for each table statement: keyword to terminator."""
    pieces: list[str] = []
    bounds: list[tuple[int, int]] = []
    copied = begin = 0
    for match in _SQL_TOKEN.finditer(text):
        kind = match.lastgroup
        if kind == "comment":
            pieces += [text[copied : match.start()], _blank(match.group())]
            copied = match.end()
        elif kind == "end":
            bounds.append((begin, match.end()))
            begin = match.end()
        elif kind in ("changeset", "go"):
            bounds.append((begin, match.start()))
            begin = match.end()
    pieces.append(text[copied:])
    bounds.append((begin, len(text)))
    cleaned = "".join(pieces)
    for begin, end in bounds:
        first = _NON_SPACE.search(cleaned, begin, end)
        if first is None:
            continue
        for head in _SQL_HEADS:
            match = head.match(cleaned, first.start(), end)
            if match:
                stop = begin + len(cleaned[begin:end].rstrip())
                yield first.start(), stop, match.group("name")
                break


def _bare_table(name: str) -> str:
    parts = _SQL_NAME_PART.findall(name)
    return parts[-1].strip('"`[]') if parts else name


def _liquibase_xml(text: str, keys: frozenset[str]) -> list[Excerpt]:
    blanked = _XML_COMMENT.sub(lambda m: _blank(m.group()), text)
    out: list[Excerpt] = []
    for change_set in _XML_CHANGESET.finditer(blanked):
        for attr in _XML_TABLE_ATTR.finditer(change_set.group()):
            name = attr.group(1) if attr.group(1) is not None else attr.group(2)
            if normalize_name(name) in keys:
                capped, _ = _cap(_slice_lines(text, change_set.start(), change_set.end()))
                out.append(Excerpt(text.count("\n", 0, change_set.start()) + 1, name, capped))
                break
    return out


def _liquibase_json(text: str, keys: frozenset[str]) -> list[Excerpt]:
    doc = _load_json(text)
    if doc is None:
        return []
    starts = [text.count("\n", 0, m.start()) + 1 for m in _json_key_re("changeSet").finditer(text)]
    hits: list[_Hit] = []
    seen = 0
    for pointer, node in _walk(doc):
        if not pointer or pointer[-1] != "changeSet":
            continue
        line = starts[seen] if seen < len(starts) else 1
        seen += 1
        if not isinstance(node, dict):
            continue
        for inner_pointer, value in _walk(node):
            key = inner_pointer[-1] if inner_pointer else None
            if key in _TABLE_KEYS and isinstance(value, str) and normalize_name(value) in keys:
                hits.append(_Hit(pointer, value, "changeSet", node, line))
                break
    return _json_excerpts(hits)


_SPEC_SUFFIXES = (".yaml", ".yml", ".json")
_MIGRATION_SUFFIXES = (".sql", ".xml", ".yaml", ".yml", ".json")
_PREFIX_LOOKBACK_LINES = 40

_OPEN_CALL = re.compile(r"(?<![\w$])([A-Za-z_$][\w$]*+)\(")
_WORD = re.compile(r"\w")
_DIGITS = re.compile(r"(\d+)")

_SQL_VERB = re.compile(r"(?<![\w$])(?:create|alter)(?![\w$])", re.I)
_SQL_REFERENCES = re.compile(
    rf"(?<![\w$])references\s+{_SQL_NAME}"
    r"(?=\s*(?:[(,;)]|$)|\s+(?:on|match|deferrable|not|initially)\b)",
    re.I | re.M,
)
_LIQUIBASE_TABLE = re.compile(
    r"""(?<![\w$])(?:tableName|baseTableName|referencedTableName)["']?\s*[:=]\s*"""
    r"""(?:"([^"\n]*)"|'([^'\n]*)'|([\w$.]+))"""
)

_JAVA_STRING = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
_ANNOTATION_ARG = re.compile(r"""(?<![\w$])(?:([A-Za-z_$][\w$]*+)\s*=\s*)?(\{[^{}]*\}|"(?:[^"\\\n]|\\.)*")""")
_SPRING_MAPPING = re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(([^()]*)\)")
_JAXRS_PATH = re.compile(r"@Path\s*\(([^()]*)\)")
_PY_ROUTE = re.compile(
    r"""@[A-Za-z_]\w*+(?:\.[A-Za-z_]\w*+)*\.(?:get|post|put|delete|patch|route|api_route)\s*\(\s*"""
    r"""(?:(?:path|rule)\s*=\s*)?[rRbBuUfF]{0,2}(?:"([^"\\\n]*)"|'([^'\\\n]*)')"""
)
_EXPRESS_ROUTE = re.compile(
    r"""(?<![\w$])[A-Za-z_$][\w$]*+\s*\.\s*(?:get|post|put|delete|patch|all|use)\s*\(\s*"""
    r"""(?:"(/[^"\\\n]*)"|'(/[^'\\\n]*)'|`(/[^`\\\n]*)`)"""
)
_JAVA_TYPE_DECL = re.compile(
    r"[ \t]*(?:(?:@[\w$.]++(?:[ \t]*\([^()]*\))?|public|protected|private|abstract|final|static|sealed"
    r"|non-sealed|strictfp)[ \t]+)*(?P<kw>class|interface)[ \t]+[A-Za-z_$]"
)
_FLASK_PREFIX = re.compile(
    r"""(?<![\w$])Blueprint\s*\([^()]*?(?<![\w$])url_prefix\s*=\s*[rRbBuUfF]{0,2}"""
    r"""(?:"([^"\\\n]*)"|'([^'\\\n]*)')"""
)
_FASTAPI_PREFIX = re.compile(
    r"""(?<![\w$])APIRouter\s*\([^()]*?(?<![\w$])prefix\s*=\s*[rRbBuUfF]{0,2}"""
    r"""(?:"([^"\\\n]*)"|'([^'\\\n]*)')"""
)

_YAML_OPENAPI = re.compile(r"""^["']?(?:openapi|swagger)["']?[ \t]*:""", re.M)
_YAML_CHANGELOG = re.compile(r"""^["']?databaseChangeLog["']?[ \t]*:""", re.M)


@dataclass(frozen=True)
class ContractTriggers:
    """What a changed file's added lines can match in a contract file.

    ``routes`` are HTTP route templates, ``tables`` bare SQL table names,
    ``names`` referenced identifiers (candidate schema names) and
    ``operation_ids`` identifiers written directly before a ``(``. Each tuple
    is deduplicated and in first-appearance order.
    """

    routes: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    operation_ids: tuple[str, ...] = ()


def contract_triggers(path: str, added: Sequence[str], *, text: str | None = None) -> ContractTriggers:
    """The contract triggers on one changed file's added lines.

    ``added`` holds the file's ``+`` lines; they are scanned joined by
    newlines, so a statement or annotation that wraps across added lines
    still counts. ``text`` is the file's full content at the head, or None.

    - ``routes``: the path literals of route declarations. Spring
      ``@GetMapping``, ``@PostMapping``, ``@PutMapping``, ``@DeleteMapping``,
      ``@PatchMapping`` and ``@RequestMapping`` give their positional string,
      their ``value =`` or ``path =`` string, or every string of an array
      form ``{"/a", "/b"}``, never a ``produces``, ``consumes``, ``name``,
      ``headers`` or ``params`` string. JAX-RS ``@Path("...")`` counts, as do
      FastAPI and Flask ``@<ident>.get|post|put|delete|patch|route|api_route("...")``
      and Express ``<ident>.get|post|put|delete|patch|all|use('/...')`` when
      the literal starts with ``/``. Empty literals are dropped. When
      ``text`` is given and a route was found, class-level prefixes are read
      from it: a Spring ``@RequestMapping`` or JAX-RS ``@Path`` in the
      annotations directly above a class or interface declaration, a Flask
      ``Blueprint(..., url_prefix="...")`` and a FastAPI
      ``APIRouter(prefix="...")``, at most the first of each kind in the
      file. After the bare routes come, for each prefix, the prefix joined to
      every route with exactly one ``/`` between them, because Spring and
      Flask accept a segment written without its leading slash.
    - ``tables``: every ``CREATE TABLE``, ``ALTER TABLE`` and
      ``CREATE [UNIQUE] INDEX ... ON <table>`` head this module's
      :func:`sql_excerpts` recognizes (the table, never the index name), each
      ``REFERENCES <table>`` foreign-key target, and every Liquibase
      ``tableName``, ``baseTableName`` and ``referencedTableName`` value, in
      XML (``tableName="t"``), YAML (``tableName: t``) or JSON
      (``"tableName": "t"``). Each is reduced to its bare name. This applies
      to every language, because SQL also shows up in Python and Java
      migrations. An index head is read only up to the next ``CREATE`` or
      ``ALTER`` keyword, which keeps the scan linear.
    - ``names``: :func:`prxref.repo_context.referenced_names` of the added
      lines in the path's language.
    - ``operation_ids``: the identifiers written directly before a ``(``
      (declarations and calls alike), minus the language's keywords.
    """
    body = "\n".join(added)
    language = language_of(path)
    routes = _routes(body)
    if routes and text is not None:
        prefixes = _route_prefixes(text.removeprefix(_BOM))
        routes += [_join_route(prefix, route) for prefix in prefixes for route in routes]
    keywords = _JAVA_KEYWORDS if language == "java" else chunk_context._keywords(language)
    return ContractTriggers(
        routes=_unique(routes),
        tables=_unique(_tables(body)),
        names=_unique(referenced_names(added, language)),
        operation_ids=_unique(name for name in _OPEN_CALL.findall(body) if name not in keywords),
    )


def literal_contract_paths(globs: Sequence[str]) -> list[str]:
    """The contract globs that are plain paths, in glob order, deduplicated.

    A glob is literal when it holds no ``*``, ``?`` or ``[`` and does not
    start with ``!``. It names one repository-relative path, which is read
    directly even when no listing shows it: a miss costs one read. A literal
    that a ``!`` negation in the same list vetoes is left out, as
    :func:`prxref.rules.match_globs` would leave it out. Blank globs are
    ignored.
    """
    out: dict[str, None] = {}
    for glob in globs:
        if not glob.strip() or glob.startswith("!") or any(c in glob for c in "*?["):
            continue
        if match_globs(glob, globs):
            out.setdefault(glob)
    return list(out)


def select_contract_files(
    globs: Sequence[str],
    *,
    listing: Collection[str] | None,
    diff_paths: Sequence[str],
) -> list[str]:
    """The run's contract files: sorted, deduplicated repository-relative paths.

    A path from ``listing`` (the repository's file listing at the head, or
    None when there is none) or from ``diff_paths`` counts when
    :func:`prxref.rules.match_globs` selects it with ``globs``, and every
    :func:`literal_contract_paths` path counts even when absent from both.
    The caller passes the diff paths that were not removed; nothing is
    filtered by status here. The result depends on no chunk, so it is
    computed once per run.
    """
    selected = {path for path in (*(listing or ()), *diff_paths) if path and match_globs(path, globs)}
    selected.update(literal_contract_paths(globs))
    return sorted(selected)


def earlier_migrations(path: str, contract_paths: Sequence[str]) -> list[str]:
    """The contract files a migration at ``path`` builds on, in ascending order.

    A candidate sits in the same directory as ``path``, has an extension
    :func:`contract_excerpts` handles as a migration (``.sql``, ``.xml``,
    ``.yaml``, ``.yml`` or ``.json``, case-insensitive), and sorts before
    ``path`` under a natural sort of the basenames, where digit runs compare
    as numbers, so ``V9__a.sql`` sorts before ``V10__b.sql``. At most
    :data:`MAX_EARLIER_MIGRATIONS` are returned: the ones nearest to
    ``path``. ``path`` itself is never returned.
    """
    directory = posixpath.dirname(path)
    own = _natural_key(posixpath.basename(path))
    earlier = sorted(
        {
            candidate
            for candidate in contract_paths
            if candidate != path
            and posixpath.dirname(candidate) == directory
            and _suffix(candidate) in _MIGRATION_SUFFIXES
            and _natural_key(posixpath.basename(candidate)) < own
        },
        key=lambda candidate: _natural_key(posixpath.basename(candidate)),
    )
    return earlier[-MAX_EARLIER_MIGRATIONS:]


def contract_excerpts(path: str, text: str, triggers: ContractTriggers) -> list[Excerpt]:
    """The excerpts one contract file gives for ``triggers``, chosen by extension.

    The extension is compared case-insensitively. ``names + tables`` below is
    the two tuples concatenated and deduplicated.

    - ``.sql`` goes to :func:`sql_excerpts` with the tables.
    - ``.xml`` goes to :func:`liquibase_excerpts` with the tables.
    - ``.yaml`` and ``.yml``: a top-level ``openapi:`` or ``swagger:`` key (a
      line that starts in column 0, the key optionally quoted) goes to
      :func:`openapi_yaml_excerpts` with the routes, the operation ids and
      ``names + tables`` as schemas. Otherwise a top-level
      ``databaseChangeLog:`` goes to :func:`liquibase_excerpts`. Otherwise
      the file is a fragment, such as one schema of a split spec: when the
      :func:`normalize_name` of its stem equals that of a name or table, the
      whole file is one excerpt at line 1 whose symbol is the stem, capped as
      every excerpt is; otherwise it gives nothing. A text whose first
      non-blank character is ``{`` is JSON and takes the ``.json`` branch.
    - ``.json`` is parsed once to sniff its top-level keys: ``openapi`` or
      ``swagger`` goes to :func:`openapi_json_excerpts`,
      ``databaseChangeLog`` to :func:`liquibase_excerpts`, and anything else,
      invalid JSON included, to :func:`json_schema_excerpts` with
      ``names + tables``.
    - Any other extension gives ``[]``.

    The stem is the basename up to its first dot, so
    ``transport-config.schema.json`` has the stem ``transport-config``.
    """
    suffix = _suffix(path)
    text = text.removeprefix(_BOM)
    schemas = _unique((*triggers.names, *triggers.tables))
    if suffix == ".sql":
        return sql_excerpts(text, tables=triggers.tables)
    if suffix == ".xml":
        return liquibase_excerpts(text, tables=triggers.tables)
    if suffix not in _SPEC_SUFFIXES:
        return []
    if suffix != ".json" and not text.lstrip().startswith("{"):
        if _YAML_OPENAPI.search(text):
            return openapi_yaml_excerpts(
                text, routes=triggers.routes, operation_ids=triggers.operation_ids, schemas=schemas
            )
        if _YAML_CHANGELOG.search(text):
            return liquibase_excerpts(text, tables=triggers.tables)
        return _fragment_excerpts(path, text, schemas)
    doc = _load_json(text)
    keys = doc if isinstance(doc, dict) else {}
    if "openapi" in keys or "swagger" in keys:
        return openapi_json_excerpts(
            text, routes=triggers.routes, operation_ids=triggers.operation_ids, schemas=schemas
        )
    if "databaseChangeLog" in keys:
        return liquibase_excerpts(text, tables=triggers.tables)
    return json_schema_excerpts(text, names=schemas)


def contract_entries(
    chunk: Sequence[object],
    *,
    contract_paths: Sequence[str],
    read: Callable[[str], str | None] | None,
    priority: Sequence[str] = (),
) -> list[ContextEntry]:
    """The contract entries for one worker chunk, ordered by ``(path, line)``.

    ``chunk`` is the chunk's file diffs, duck-typed on ``path``, ``hunks`` and
    ``lines`` (``kind``, ``text``, ``new_line``) as
    :func:`prxref.chunk_context.chunk_files` reads them. ``contract_paths`` is
    :func:`select_contract_files`'s result. ``read`` is the chunk's capped
    reader: each call may cost a read, and a path it excludes or cannot read
    gives None, which is skipped silently. ``read`` None gives ``[]`` with no
    work. ``priority`` lists paths that rank first among the spec files,
    normally :func:`literal_contract_paths` of the globs.

    Each chunk file's triggers come from :func:`contract_triggers`. The file's
    own text is read only when its added lines hold a route, so its
    class-level prefix can be found. The triggers of all the chunk's files
    are merged in first-appearance order, and when every field is empty the
    result is ``[]`` with no read at all.

    The files then read, in this order and once each, are:

    1. when there are tables, the :func:`earlier_migrations` of each chunk
       file that is itself a contract path, at most
       :data:`MAX_EARLIER_MIGRATIONS` per file;
    2. at most :data:`MAX_SPEC_FILES` spec candidates, the contract paths
       ending in ``.yaml``, ``.yml`` or ``.json``, ranked: a path in
       ``priority``, then a path whose stem matches a name or table by
       :func:`normalize_name`, then a basename that starts with ``openapi``
       or ``swagger`` or ends with ``.schema.json`` (case-insensitive), then
       the rest, ties broken by path. The ranking is deterministic, so every
       chunk picks the same root spec.

    A contract path that is itself a file of this chunk is never read, and
    ``.sql`` and ``.xml`` contract files are read only as earlier
    migrations. Each file read goes through :func:`contract_excerpts`, and
    each :class:`Excerpt` becomes a ``ContextEntry`` of kind and reason
    ``"contract"``. Entries that share a ``(path, line)`` keep the first.
    No character budget applies here: the only caps are the per-excerpt
    ones, :data:`MAX_EARLIER_MIGRATIONS` and :data:`MAX_SPEC_FILES`.
    """
    if read is None:
        return []
    files = chunk_context.chunk_files(chunk)
    per_file: list[ContractTriggers] = []
    for changed in files:
        triggers = contract_triggers(changed.path, changed.added)
        if triggers.routes:
            text = read(changed.path)
            if text is not None:
                triggers = contract_triggers(changed.path, changed.added, text=text)
        per_file.append(triggers)
    merged = ContractTriggers(
        routes=_unique(route for t in per_file for route in t.routes),
        tables=_unique(table for t in per_file for table in t.tables),
        names=_unique(name for t in per_file for name in t.names),
        operation_ids=_unique(op for t in per_file for op in t.operation_ids),
    )
    if not (merged.routes or merged.tables or merged.names or merged.operation_ids):
        return []
    in_chunk = {changed.path for changed in files}
    queue: dict[str, None] = {}
    if merged.tables:
        contract_set = set(contract_paths)
        for changed in files:
            if changed.path in contract_set:
                earlier = earlier_migrations(changed.path, contract_paths)
                queue.update(dict.fromkeys(p for p in earlier if p not in in_chunk))
    wanted = _keys((*merged.names, *merged.tables), normalize_name)
    ranks = frozenset(priority)
    specs = sorted(
        {p for p in contract_paths if _suffix(p) in _SPEC_SUFFIXES and p not in in_chunk and p not in queue},
        key=lambda p: (_spec_rank(p, ranks, wanted), p),
    )
    queue.update(dict.fromkeys(specs[:MAX_SPEC_FILES]))
    entries: dict[tuple[str, int], ContextEntry] = {}
    for path in queue:
        text = read(path)
        if text is None:
            continue
        for excerpt in contract_excerpts(path, text, merged):
            entries.setdefault(
                (path, excerpt.line),
                ContextEntry(
                    path=path, line=excerpt.line, symbol=excerpt.symbol, kind="contract", reason="contract",
                    text=excerpt.text,
                ),
            )
    return [entries[key] for key in sorted(entries)]


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _suffix(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def _stem(path: str) -> str:
    return posixpath.basename(path).split(".", 1)[0]


def _natural_key(name: str) -> tuple[tuple[object, ...], str]:
    """A natural sort key: digit runs compare by value, without converting them to ``int``."""
    parts = _DIGITS.split(name)
    return (
        tuple((len(part.lstrip("0")), part.lstrip("0")) if i % 2 else part for i, part in enumerate(parts)),
        name,
    )


def _spec_rank(path: str, priority: frozenset[str], wanted: frozenset[str]) -> int:
    if path in priority:
        return 0
    if normalize_name(_stem(path)) in wanted:
        return 1
    base = posixpath.basename(path).lower()
    if base.startswith(("openapi", "swagger")) or base.endswith(".schema.json"):
        return 2
    return 3


def _fragment_excerpts(path: str, text: str, schemas: Iterable[str]) -> list[Excerpt]:
    stem = _stem(path)
    if normalize_name(stem) not in _keys(schemas, normalize_name):
        return []
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        return []
    capped, _ = _cap(lines)
    return [Excerpt(1, stem, capped)]


def _first_group(match: re.Match[str]) -> str:
    return next(group for group in match.groups() if group is not None)


def _tables(body: str) -> list[str]:
    """Bare table names in ``body``, in order of appearance."""
    found: list[tuple[int, str]] = []
    verbs = [match.start() for match in _SQL_VERB.finditer(body)]
    for start, end in zip(verbs, [*verbs[1:], len(body)], strict=False):
        for head in _SQL_HEADS:
            match = head.match(body, start, end)
            if match:
                found.append((start, match.group("name")))
                break
    found += [(match.start(), match.group("name")) for match in _SQL_REFERENCES.finditer(body)]
    found += [(match.start(), _first_group(match)) for match in _LIQUIBASE_TABLE.finditer(body)]
    bare = (_bare_table(name.strip()) for _, name in sorted(found))
    return [name for name in bare if _WORD.search(name)]


def _annotation_paths(args: str) -> list[str]:
    """The route strings of a Java annotation's arguments: positional, ``value =`` or ``path =``."""
    out: list[str] = []
    for match in _ANNOTATION_ARG.finditer(args):
        key, value = match.groups()
        if key is not None and key not in ("value", "path"):
            continue
        out += _JAVA_STRING.findall(value) if value.startswith("{") else [value[1:-1]]
    return out


def _routes(body: str) -> list[str]:
    found: list[tuple[int, int, str]] = []
    for match in _SPRING_MAPPING.finditer(body):
        found += [(match.start(), i, route) for i, route in enumerate(_annotation_paths(match.group(2)))]
    for match in _JAXRS_PATH.finditer(body):
        found += [(match.start(), i, route) for i, route in enumerate(_annotation_paths(match.group(1)))]
    for regex in (_PY_ROUTE, _EXPRESS_ROUTE):
        found += [(match.start(), 0, _first_group(match)) for match in regex.finditer(body)]
    return [route for _, _, route in sorted(found) if route]


def _annotation_block(lines: list[str], index: int) -> list[str]:
    """The annotation lines directly above line ``index``, a wrapped annotation's arguments included."""
    block: list[str] = []
    depth = 0
    for j in range(index - 1, max(index - 1 - _PREFIX_LOOKBACK_LINES, -1), -1):
        line = lines[j]
        depth += line.count(")") - line.count("(")
        if depth <= 0 and not line.lstrip().startswith("@"):
            break
        block.append(line)
    return block[::-1]


def _class_prefixes(text: str) -> tuple[str | None, str | None]:
    """The first class-level Spring ``@RequestMapping`` and JAX-RS ``@Path`` paths, or None."""
    lines = text.split("\n")
    spring: str | None = None
    jaxrs: str | None = None
    for index, line in enumerate(lines):
        if "class" not in line and "interface" not in line:
            continue
        decl = _JAVA_TYPE_DECL.match(line)
        if decl is None:
            continue
        head = "\n".join([*_annotation_block(lines, index), line[: decl.start("kw")]])
        if spring is None:
            mappings = (m for m in _SPRING_MAPPING.finditer(head) if m.group(1) == "Request")
            spring = next((p for m in mappings for p in _annotation_paths(m.group(2))), None)
        if jaxrs is None:
            jaxrs = next((p for m in _JAXRS_PATH.finditer(head) for p in _annotation_paths(m.group(1))), None)
        if spring is not None and jaxrs is not None:
            break
    return spring, jaxrs


def _route_prefixes(text: str) -> list[str]:
    prefixes = list(_class_prefixes(text))
    for regex in (_FLASK_PREFIX, _FASTAPI_PREFIX):
        match = regex.search(text)
        prefixes.append(_first_group(match) if match else None)
    return [prefix for prefix in prefixes if prefix]


def _join_route(prefix: str, route: str) -> str:
    return prefix.rstrip("/") + "/" + route.lstrip("/")
