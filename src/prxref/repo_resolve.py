"""Candidate files for the definitions a changed file references (repository context).

:func:`prxref.repo_context.referenced_names` says WHICH identifiers a chunk's
added lines use. This module says WHERE their definitions probably live: it
turns a referencing file plus those names into an ordered list of
:class:`Candidate` files. Reading the candidates and running
:func:`prxref.repo_context.find_definitions` over their text is the caller's
job, not this module's.

The module is pure. It is stdlib plus :mod:`prxref.repo_context`, and it
performs no I/O, no reads and no network. Every candidate costs the caller a
read, and a miss costs one too, so the rules stay narrow and the resolution is
best effort by design: no build file, ``tsconfig`` path alias, ``sys.path`` or
classpath is consulted.

Candidates come in three groups, highest rank first, each carrying a member of
:data:`prxref.repo_context.REASONS` as its ``reason``:

- ``"import"``: Java imports from the file's own organization under the source
  root its ``package`` line implies, Python ``from ... import`` statements,
  and TS/JS relative ``import ... from`` and ``export ... from`` specifiers.
- ``"path-convention"``: a Java type from the file's own package,
  ``<dir>/<Name>.java``.
- ``"name-search"``: listing files of the same language whose stem equals the
  name, when the caller has a repository listing.
"""
from __future__ import annotations

import posixpath
import re
import sys
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from .repo_context import definition_regexes, language_of

MAX_NAME_SEARCH_PER_NAME = 3

_JAVA_PACKAGE_RE = re.compile(r"^[ \t]*package[ \t]+([\w$.]+)[ \t]*;", re.M)
_JAVA_IMPORT_RE = re.compile(r"^[ \t]*import[ \t]+(static[ \t]+)?([\w$.]+?)(\.\*)?[ \t]*;", re.M)
_JAVA_EXTERNAL_ROOTS = frozenset({"java", "javax"})

_PY_FROM_RE = re.compile(
    r"^[ \t]*from[ \t]+(\.*)[ \t]*([A-Za-z_][\w.]*)?[ \t]+import[ \t]+(\([^)]*\)|\([^\n]*|[^\n]*)",
    re.M,
)
_PY_ITEM_RE = re.compile(r"^([A-Za-z_]\w*)(?:\s+as\s+([A-Za-z_]\w*))?$")
_PY_STDLIB = sys.stdlib_module_names

_JS_IDENT = r"[A-Za-z_$][\w$]*"
_JS_FROM_RE = re.compile(
    r"(?<![\w$.])(import|export)\s+(?:type\s+)?"
    rf"((?:{_JS_IDENT}\s*,\s*)?(?:\{{[^{{}}]*\}}|\*\s*as\s+{_JS_IDENT}|\*)|{_JS_IDENT})"
    r"""\s*from\s*(['"])([^'"\n]+)\3"""
)
_JS_ITEM_RE = re.compile(rf"^(?:type\s+)?({_JS_IDENT})(?:\s+as\s+({_JS_IDENT}))?$")
_JS_NAMESPACE_RE = re.compile(rf"^\*\s*as\s+({_JS_IDENT})$")
_JS_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_JS_PROBES = (".ts", ".tsx", ".d.ts", ".js", "/index.ts")
_JS_ESM_PROBES = (".ts", ".tsx", ".d.ts", ".js")
_JS_LITERAL_SUFFIXES = (".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
_JS_ASSET_SUFFIXES = (
    ".css", ".scss", ".sass", ".less", ".json", ".svg", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".ico", ".html", ".md", ".txt", ".vue", ".svelte",
    ".graphql", ".gql", ".wasm", ".yaml", ".yml",
)

_SNAKE_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

_Found = tuple[str, str, str]


@dataclass(frozen=True)
class Candidate:
    """One file that may define a referenced name.

    ``name`` is the identifier to look up in the candidate file: the
    referenced name itself, or, for an aliased import (``from m import C as
    D``, ``import {C as D} from './m'``), the imported name ``C``. ``path`` is
    the repository-relative POSIX path of the file, and ``reason`` is
    ``"import"``, ``"path-convention"`` or ``"name-search"``, all members of
    :data:`prxref.repo_context.REASONS`.
    """

    name: str
    path: str
    reason: str


def _normalize(path: str) -> str | None:
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts) or None


def _java_root(directory: str, package: str) -> str | None:
    if not package:
        return None
    package_dir = package.replace(".", "/")
    if directory == package_dir:
        return ""
    if directory.endswith("/" + package_dir):
        return directory[: -len(package_dir)]
    return None


def _java_type_index(parts: Sequence[str]) -> int | None:
    for index, part in enumerate(parts):
        if part[:1].isupper():
            return index
    return None


def _java(
    directory: str, text: str, names: Sequence[str]
) -> tuple[list[_Found], list[_Found], set[str]]:
    wanted = set(names)
    package_match = _JAVA_PACKAGE_RE.search(text)
    package = package_match.group(1) if package_match else ""
    root = _java_root(directory, package)
    org = package.split(".")[:2] if package else []
    statements = [
        (bool(m.group(1)), m.group(2).split("."), bool(m.group(3))) for m in _JAVA_IMPORT_RE.finditer(text)
    ]
    claimed = {parts[-1] for _, parts, wildcard in statements if not wildcard}
    external: set[str] = set()
    imports: list[_Found] = []
    for static, parts, wildcard in statements:
        third_party = parts[0] in _JAVA_EXTERNAL_ROOTS or bool(org and parts[: len(org)] != org)
        if third_party and not wildcard:
            external.add(parts[-1])
        if static or third_party or root is None:
            continue
        index = _java_type_index(parts)
        if wildcard:
            for name in names:
                if name in claimed:
                    continue
                file_parts = parts[: index + 1] if index is not None else [*parts, name]
                imports.append((name, name, root + "/".join(file_parts) + ".java"))
            continue
        name = parts[-1]
        if name in wanted:
            file_parts = parts[: index + 1] if index is not None else parts
            imports.append((name, name, root + "/".join(file_parts) + ".java"))
    conventions = [
        (name, name, f"{directory}/{name}.java" if directory else f"{name}.java")
        for name in names
        if name not in claimed
    ]
    return imports, conventions, external


def _py_items(clause: str) -> list[tuple[str, str]]:
    body = re.sub(r"#[^\n]*", "", clause).strip()
    if body.startswith("("):
        body = body[1:].split(")", 1)[0]
    else:
        body = body.split(";", 1)[0]
    items: list[tuple[str, str]] = []
    for raw in body.split(","):
        item = " ".join(raw.split())
        if item == "*":
            items.append(("*", "*"))
            continue
        match = _PY_ITEM_RE.match(item)
        if match:
            items.append((match.group(1), match.group(2) or match.group(1)))
    return items


def _py_module_files(directory: str, dots: str, module: str) -> list[str]:
    relative = module.replace(".", "/")
    if dots:
        base = "/".join([directory, *[".."] * (len(dots) - 1)])
        if relative:
            probes = [f"{base}/{relative}.py", f"{base}/{relative}/__init__.py"]
        else:
            probes = [f"{base}/__init__.py"]
    else:
        probes = [
            f"{relative}.py",
            f"{relative}/__init__.py",
            f"src/{relative}.py",
            f"src/{relative}/__init__.py",
        ]
    return [p for p in map(_normalize, probes) if p]


def _python(directory: str, text: str, names: Sequence[str]) -> tuple[list[_Found], set[str]]:
    wanted = set(names)
    imports: list[_Found] = []
    external: set[str] = set()
    bound: set[str] = set()
    stars: list[list[str]] = []
    for match in _PY_FROM_RE.finditer(text.replace("\\\n", " ")):
        dots, module, clause = match.group(1), match.group(2) or "", match.group(3)
        if not dots and not module:
            continue
        items = _py_items(clause)
        bound.update(local for _, local in items if local != "*")
        if not dots and module.split(".")[0] in _PY_STDLIB:
            external.update(local for _, local in items if local != "*")
            continue
        files = _py_module_files(directory, dots, module)
        for original, local in items:
            if original == "*":
                stars.append(files)
            elif local in wanted:
                imports.extend((local, original, f) for f in files)
    for files in stars:
        for name in names:
            if name not in bound:
                imports.extend((name, name, f) for f in files)
    return imports, external


def _js_module_files(directory: str, specifier: str) -> list[str]:
    if not (specifier in (".", "..") or specifier.startswith(("./", "../"))):
        return []
    base = f"{directory}/{specifier}" if directory else specifier
    lower = specifier.lower()
    if specifier in (".", "..") or specifier.endswith("/"):
        probes = [base.rstrip("/") + "/index.ts"]
    elif lower.endswith(_JS_ASSET_SUFFIXES):
        return []
    elif lower.endswith(".js"):
        probes = [base[:-3] + suffix for suffix in _JS_ESM_PROBES]
    elif lower.endswith(_JS_LITERAL_SUFFIXES):
        probes = [base]
    else:
        probes = [base + suffix for suffix in _JS_PROBES]
    return [p for p in map(_normalize, probes) if p]


def _js_clause(clause: str) -> tuple[list[tuple[str, str]], list[str]]:
    pairs: list[tuple[str, str]] = []
    namespaces: list[str] = []
    brace = clause.find("{")
    head = clause if brace == -1 else clause[:brace]
    body = "" if brace == -1 else clause[brace + 1 : clause.rfind("}")]
    for raw in head.split(","):
        part = " ".join(raw.split())
        namespace = _JS_NAMESPACE_RE.match(part)
        if namespace:
            namespaces.append(namespace.group(1))
        elif part and part != "*" and re.fullmatch(_JS_IDENT, part):
            pairs.append(("default", part))
    for raw in _JS_COMMENT_RE.sub("", body).split(","):
        match = _JS_ITEM_RE.match(" ".join(raw.split()))
        if match:
            pairs.append((match.group(1), match.group(2) or match.group(1)))
    return pairs, namespaces


def _js(directory: str, text: str, names: Sequence[str]) -> tuple[list[_Found], set[str]]:
    wanted = set(names)
    imports: list[_Found] = []
    external: set[str] = set()
    for match in _JS_FROM_RE.finditer(text):
        keyword, clause, specifier = match.group(1), match.group(2), match.group(4)
        pairs, namespaces = _js_clause(clause)
        files = _js_module_files(directory, specifier)
        if not files:
            if keyword == "import" and not specifier.startswith((".", "/")):
                external.update(local for _, local in pairs)
            continue
        for original, local in pairs:
            keys = (local,) if keyword == "import" else (original, local)
            key = next((k for k in keys if k in wanted), None)
            if key is None:
                continue
            target = local if original == "default" else original
            imports.extend((key, target, f) for f in files)
        if keyword != "import":
            continue
        for namespace in namespaces:
            member_re = re.compile(rf"(?<![\w$]){re.escape(namespace)}\s*\.\s*({_JS_IDENT})")
            members = dict.fromkeys(m.group(1) for m in member_re.finditer(text))
            for member in members:
                if member in wanted:
                    imports.extend((member, member, f) for f in files)
    return imports, external


def _stem(base: str) -> str:
    dot = base.rfind(".")
    stem = base[:dot] if dot > 0 else base
    return stem[:-2] if stem.endswith(".d") else stem


def _snake_case(name: str) -> str:
    return _SNAKE_RE.sub("_", name).lower()


def _shared_depth(left: str, right: str) -> int:
    depth = 0
    for a, b in zip(left.split("/") if left else [], right.split("/") if right else [], strict=False):
        if a != b:
            break
        depth += 1
    return depth


def _name_search(
    source: str, language: str, names: Sequence[str], listing: Collection[str], skip: set[str]
) -> list[_Found]:
    python = language == "python"
    searched = [n for n in names if n not in skip and not (n.startswith("__") and n.endswith("__"))]
    targets = {n.lower() for n in searched}
    if python:
        targets |= {_snake_case(n) for n in searched}
    by_lower: dict[str, list[tuple[str, str]]] = {}
    for raw in listing:
        stem = _stem(raw.rsplit("/", 1)[-1])
        folded = stem.lower()
        if folded not in targets or language_of(raw) != language:
            continue
        path = _normalize(raw)
        if path is None or path == source:
            continue
        by_lower.setdefault(folded, []).append((stem, path))
    directory = posixpath.dirname(source)

    def ordered(paths: list[str]) -> list[str]:
        return sorted(paths, key=lambda p: (-_shared_depth(posixpath.dirname(p), directory), p))

    found: list[_Found] = []
    for name in searched:
        folded = name.lower()
        hits = by_lower.get(folded, [])
        tiers = [
            [p for stem, p in hits if stem == name],
            [p for stem, p in hits if stem != name],
        ]
        snake = _snake_case(name)
        if python and snake != folded:
            tiers.append([p for _, p in by_lower.get(snake, [])])
        picked: list[str] = []
        for tier in tiers:
            for path in ordered(tier):
                if path not in picked:
                    picked.append(path)
        found.extend((name, name, p) for p in picked[:MAX_NAME_SEARCH_PER_NAME])
    return found


def resolve_candidates(
    path: str,
    text: str | None,
    names: Sequence[str],
    *,
    listing: Collection[str] | None,
    listing_complete: bool = False,
) -> list[Candidate]:
    """Ordered candidate files that may define ``names``, as referenced from ``path``.

    ``path`` is the referencing file, repository-relative. ``text`` is its
    content at the head, or the chunk's added lines joined with newlines when
    there is no reader, so no ``package`` line or import is assumed present;
    ``None`` means no imports are known. ``names`` comes from
    :func:`prxref.repo_context.referenced_names`. ``listing`` holds the
    repository's file paths, or is ``None``; ``listing_complete`` says the
    listing was not truncated.

    Rules, by :func:`prxref.repo_context.language_of`:

    - Java. The source root is ``path``'s directory minus the package
      directory of its first ``package`` line (``package com.acme.connectors;``
      in ``src/main/java/com/acme/connectors/X.java`` gives ``src/main/java/``);
      with no package line, or a directory that does not end with the package
      path, the root is unknown and no import resolves. Only an import sharing
      the first two package segments with the file's own package counts:
      ``import com.acme.x.T;`` gives ``<root>com/acme/x/T.java`` when ``T`` is
      referenced, and a wildcard ``import com.acme.x.*;`` gives
      ``<root>com/acme/x/<Name>.java`` for each referenced name no
      single-name import binds. ``import static``, ``java.*``, ``javax.*`` and
      other organizations' imports give nothing. A nested type is best effort:
      the file is the first segment with an uppercase initial, so
      ``a.b.Outer.Inner`` maps to ``a/b/Outer.java``. Then, as the
      ``"path-convention"`` group, ``<dir>/<Name>.java`` for every name no
      single-name import binds; a single-name import is any non-wildcard
      import, static or not, of any organization, resolved or not.
    - Python. ``from a.b import C`` (also parenthesized, aliased or ``*``)
      gives ``a/b.py``, ``a/b/__init__.py``, ``src/a/b.py`` and
      ``src/a/b/__init__.py``, in that order. Relative dots count from
      ``path``'s directory (``.`` is that directory, each further dot one level
      up), give ``<base>/x.py`` and ``<base>/x/__init__.py``, and for a bare
      ``from . import C`` just ``<base>/__init__.py``. ``import a.b`` gives
      nothing (its names are attributes), and neither does a standard-library
      module.
    - TS/JS (``language_of`` reports ``"js"``). A relative specifier of
      ``import {C} from``, ``import C from``, ``import * as N from`` (for each
      referenced ``N.<Name>``) or ``export {C} from`` tries ``x.ts``,
      ``x.tsx``, ``x.d.ts``, ``x.js`` and ``x/index.ts`` against ``path``'s
      directory. A ``.js`` specifier (TypeScript ESM) tries the same stem as
      ``.ts``, ``.tsx``, ``.d.ts`` and ``.js``; another script extension is
      tried literally; an asset (``.css``, ``.json``, ``.svg`` and similar)
      gives nothing. Bare package specifiers give nothing.
    - Name search, every language, only when ``listing`` is not ``None``:
      listing files of the same language whose stem (the basename less its
      last extension and a ``.d`` before it) equals the name case-sensitively,
      then case-insensitively, then, for Python, equals the snake_case form of
      a CamelCase name. Within each tier the deepest directory shared with
      ``path`` comes first, then the path. At most
      :data:`MAX_NAME_SEARCH_PER_NAME` per name. Names bound by an import
      judged external (another organization's Java import, a
      standard-library Python module, a bare TS/JS package) and dunder names
      are not searched.

    Candidates are ordered by group (imports, path conventions, name search),
    then by the name's position in ``names``; within one name, imports keep
    statement order and each rule's probe order above. Duplicates by
    ``(name, path)`` keep the first, highest-ranked reason. ``path`` itself is
    never a candidate. With a listing and ``listing_complete``, an import or
    path-convention candidate the listing lacks is dropped. Paths are
    normalized POSIX, and a candidate that would escape the repository root is
    dropped. A language without definition regexes
    (:func:`prxref.repo_context.definition_regexes`) returns ``[]``, since no
    candidate could yield a definition. There is no error path: an
    unresolvable name is simply absent.
    """
    source = _normalize(path)
    if source is None:
        return []
    language = language_of(source)
    if not definition_regexes(language):
        return []
    ordered_names = list(dict.fromkeys(names))
    rank = {name: index for index, name in enumerate(ordered_names)}
    directory = posixpath.dirname(source)
    body = text or ""
    conventions: list[_Found] = []
    if language == "java":
        imports, conventions, external = _java(directory, body, ordered_names)
    elif language == "python":
        imports, external = _python(directory, body, ordered_names)
    elif language == "js":
        imports, external = _js(directory, body, ordered_names)
    else:
        imports, external = [], set()
    listed: Collection[str] | None = None
    if listing is not None and listing_complete:
        listed = listing if isinstance(listing, (set, frozenset)) else frozenset(listing)
    out: list[Candidate] = []
    seen: set[tuple[str, str]] = set()

    def admit(reason: str, found: list[_Found], *, filtered: bool) -> None:
        for _, name, raw in sorted(found, key=lambda entry: rank[entry[0]]):
            candidate = _normalize(raw)
            if candidate is None or candidate == source:
                continue
            if filtered and listed is not None and candidate not in listed:
                continue
            if (name, candidate) in seen:
                continue
            seen.add((name, candidate))
            out.append(Candidate(name=name, path=candidate, reason=reason))

    admit("import", imports, filtered=True)
    admit("path-convention", conventions, filtered=True)
    if listing is not None:
        admit("name-search", _name_search(source, language, ordered_names, listing, external), filtered=False)
    return out
