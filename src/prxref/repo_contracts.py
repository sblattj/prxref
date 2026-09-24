"""Contract excerpts for repository context: the OpenAPI, JSON Schema, SQL and
Liquibase slices that a changed route, operation, schema or table points at.

A diff that adds a route handler, a migration or a DTO often changes behavior
that a contract file elsewhere in the repository pins down: the OpenAPI
operation for the route, the schema a payload must satisfy, the unique index on
a table. The excerpters here cut the matching slice out of such a file so a
worker can check the change against it.

The module is pure and stdlib only. It performs no I/O: callers pass the file's
text. There is deliberately no YAML parser, because the core ships none, so
YAML is sliced by indentation, JSON goes through :mod:`json`, and SQL and XML
are scanned with regular expressions. Every excerpter degrades to ``[]`` on
text it cannot read rather than raising, because a review must never fail over
missing context. Each excerpt is capped at :data:`MAX_CONTRACT_LINES` lines and
:data:`MAX_CONTRACT_CHARS` characters.

Names compare through :func:`normalize_name`, so a table called
``idempotency_keys`` matches a schema or class called ``IdempotencyKey``.
Routes compare through :func:`route_key`, so the code route
``/connectors/:id`` matches the spec path ``/connectors/{connectorId}``.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

MAX_CONTRACT_LINES = 40
MAX_CONTRACT_CHARS = 2000

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
