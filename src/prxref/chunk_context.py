"""Extra prompt context for one worker chunk: dependency pins and definitions.

Two blocks answer the two questions a diff-only prompt cannot: which VERSION of
a third-party library the changed code runs against, and what a referenced
identifier actually IS when its definition sits outside the rendered hunk.

The module is pure. It performs no I/O of its own: every caller passes a
``read(path) -> str | None`` callable that resolves a repository-relative path
at the PR head, and a read that fails is simply a read that returned ``None``.
Everything here degrades to an empty block rather than raising, because a
review must never fail over missing context.
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

# Caps are constants, not configuration: they bound prompt growth, and an
# operator who wants a different prompt shape has no lever here worth exposing.
MAX_DEFINITION_ENTRIES = 40
MAX_LINES_PER_DEFINITION = 6
MAX_DEFINITION_CHARS = 8000
MAX_FILE_BYTES = 512 * 1024

DEPENDENCY_HEADER = "### Dependency versions"
DEFINITIONS_HEADER = "### Definitions referenced by this chunk"

_JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")

_JS_KEYWORDS = frozenset({
    "abstract", "any", "as", "async", "await", "boolean", "break", "case",
    "catch", "class", "const", "constructor", "continue", "debugger",
    "declare", "default", "delete", "do", "else", "enum", "export", "extends",
    "false", "finally", "for", "from", "function", "get", "if", "implements",
    "import", "in", "infer", "instanceof", "interface", "is", "keyof", "let",
    "n", "namespace", "never", "new", "null", "number", "object", "of",
    "package", "private", "protected", "public", "readonly", "require",
    "return", "satisfies", "set", "static", "string", "super", "switch",
    "symbol", "this", "throw", "true", "try", "type", "typeof", "undefined",
    "union", "unknown", "var", "void", "while", "with", "yield",
})

_PY_KEYWORDS = frozenset({
    "and", "as", "assert", "async", "await", "break", "class", "continue",
    "def", "del", "elif", "else", "except", "False", "finally", "for", "from",
    "global", "if", "import", "in", "is", "lambda", "None", "nonlocal", "not",
    "or", "pass", "raise", "return", "self", "True", "try", "while", "with",
    "yield", "int", "str", "float", "bool", "list", "dict", "set", "tuple",
    "print", "len", "range", "type", "object",
})

_IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")

_JS_FROM_RE = re.compile(r"""\bfrom\s+['"]([^'"]+)['"]""")
_JS_BARE_IMPORT_RE = re.compile(r"""^\s*import\s+['"]([^'"]+)['"]""")
_JS_REQUIRE_RE = re.compile(r"""\brequire\(\s*['"]([^'"]+)['"]\s*\)""")
_JS_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\(\s*['"]([^'"]+)['"]\s*\)""")

_PY_IMPORT_RE = re.compile(r"^\s*import\s+([A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*)")
_PY_FROM_RE = re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\b")

_RUST_USE_RE = re.compile(r"^\s*use\s+([A-Za-z_][A-Za-z0-9_]*)\s*::")
_RUST_INTERNAL = frozenset({"crate", "self", "super", "std", "core", "alloc"})

_JS_DEF_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?"
    r"(?:async\s+function|function|const|let|var|class|type|interface|enum)"
    r"\s+([A-Za-z_$][A-Za-z0-9_$]*)"
)
_PY_DEF_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")
_PY_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=(?!=)")

_MANIFESTS = {
    "js": "package.json",
    "python": "pyproject.toml",
    "go": "go.mod",
    "rust": "Cargo.toml",
}


@dataclass(frozen=True)
class ChunkFile:
    """One changed file reduced to what prompt context needs.

    ``added`` holds the TEXT of the chunk's ``+`` lines; ``hunk_lines`` holds
    every new-file line number the chunk's hunks cover, so a definition inside
    a rendered hunk is never repeated as out-of-hunk context.
    """

    path: str
    added: tuple[str, ...] = ()
    hunk_lines: frozenset[int] = frozenset()


def chunk_files(chunk: Iterable[object]) -> list[ChunkFile]:
    """Reduce a chunk of ``triage.FileDiff`` records to :class:`ChunkFile`.

    Duck-typed on ``path`` / ``hunks`` / ``lines`` so the pure functions below
    stay independent of the parser's dataclasses, and so unit tests can build
    :class:`ChunkFile` values directly.
    """
    out: list[ChunkFile] = []
    for f in chunk:
        path = getattr(f, "path", "") or ""
        if not path:
            continue
        added: list[str] = []
        covered: set[int] = set()
        for hunk in getattr(f, "hunks", None) or []:
            for line in getattr(hunk, "lines", None) or []:
                kind = getattr(line, "kind", " ")
                new_line = getattr(line, "new_line", None)
                if new_line is not None:
                    covered.add(new_line)
                if kind == "+":
                    added.append(getattr(line, "text", ""))
        out.append(ChunkFile(path=path, added=tuple(added), hunk_lines=frozenset(covered)))
    return out


def _language(path: str) -> str:
    lower = path.lower()
    if lower.endswith(_JS_SUFFIXES):
        return "js"
    if lower.endswith((".py", ".pyi")):
        return "python"
    if lower.endswith(".go"):
        return "go"
    if lower.endswith(".rs"):
        return "rust"
    return ""


def _js_package(specifier: str) -> str:
    if not specifier or specifier.startswith((".", "/")) or ":" in specifier:
        return ""
    parts = specifier.split("/")
    if specifier.startswith("@"):
        return "/".join(parts[:2]) if len(parts) >= 2 else ""
    return parts[0]


def imported_packages(path: str, added: Sequence[str]) -> set[str]:
    """External package names imported by a file's added lines.

    Relative and ``node:`` specifiers, Python stdlib modules, and relative
    Python imports are excluded. Go and Rust are best effort: Go returns
    nothing (its import paths are module paths, not package names), Rust
    returns the crate root of an external ``use name::…``.
    """
    language = _language(path)
    names: set[str] = set()
    if language == "js":
        for text in added:
            for regex in (
                _JS_FROM_RE, _JS_BARE_IMPORT_RE, _JS_REQUIRE_RE, _JS_DYNAMIC_IMPORT_RE,
            ):
                for spec in regex.findall(text):
                    name = _js_package(spec)
                    if name:
                        names.add(name)
    elif language == "python":
        stdlib = getattr(sys, "stdlib_module_names", frozenset())
        for text in added:
            candidates: list[str] = []
            match = _PY_FROM_RE.match(text)
            if match:
                candidates.append(match.group(1))
            else:
                match = _PY_IMPORT_RE.match(text)
                if match:
                    candidates.extend(p.strip() for p in match.group(1).split(","))
            for candidate in candidates:
                top = candidate.split(".")[0]
                if top and top not in stdlib:
                    names.add(top)
    elif language == "rust":
        for text in added:
            match = _RUST_USE_RE.match(text)
            if match and match.group(1) not in _RUST_INTERNAL:
                names.add(match.group(1))
    return names


def _ancestor_dirs(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    dirs = []
    while parts:
        dirs.append("/".join(parts))
        parts = parts[:-1]
    dirs.append("")
    return dirs


def _nearest_manifest(path: str, manifest: str, read) -> str | None:
    for directory in _ancestor_dirs(path):
        candidate = f"{directory}/{manifest}" if directory else manifest
        text = read(candidate)
        if text:
            return text
    return None


def _parse_package_json(text: str) -> dict[str, str]:
    data = json.loads(text)
    out: dict[str, str] = {}
    for section in ("dependencies", "devDependencies"):
        block = data.get(section)
        if isinstance(block, dict):
            for name, version in block.items():
                if isinstance(name, str) and isinstance(version, str):
                    out.setdefault(name, version)
    return out


_PEP508_NAME_RE = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*(\[[^\]]*\])?\s*(.*)$")


def _parse_pyproject(text: str) -> dict[str, str]:
    data = tomllib.loads(text)
    out: dict[str, str] = {}
    project = data.get("project")
    if isinstance(project, dict):
        for spec in project.get("dependencies") or []:
            if not isinstance(spec, str):
                continue
            match = _PEP508_NAME_RE.match(spec.split(";")[0])
            if match:
                out.setdefault(match.group(1), (match.group(3) or "*").strip() or "*")
    poetry = (data.get("tool") or {}).get("poetry") if isinstance(data.get("tool"), dict) else None
    if isinstance(poetry, dict):
        block = poetry.get("dependencies")
        if isinstance(block, dict):
            for name, version in block.items():
                if not isinstance(name, str):
                    continue
                if isinstance(version, str):
                    out.setdefault(name, version)
                elif isinstance(version, dict) and isinstance(version.get("version"), str):
                    out.setdefault(name, version["version"])
    return out


_GO_REQUIRE_RE = re.compile(r"^\s*(?:require\s+)?([^\s]+)\s+(v[^\s/]+)")


def _parse_go_mod(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//")[0].strip()
        if not line:
            continue
        if line.startswith("require (") or (in_block is False and line == "require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        if not in_block and not line.startswith("require "):
            continue
        match = _GO_REQUIRE_RE.match(line)
        if match:
            out.setdefault(match.group(1), match.group(2))
    return out


def _parse_cargo_toml(text: str) -> dict[str, str]:
    data = tomllib.loads(text)
    out: dict[str, str] = {}
    for section in ("dependencies", "dev-dependencies"):
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        for name, version in block.items():
            if not isinstance(name, str):
                continue
            if isinstance(version, str):
                out.setdefault(name, version)
            elif isinstance(version, dict) and isinstance(version.get("version"), str):
                out.setdefault(name, version["version"])
    return out


_PARSERS = {
    "js": _parse_package_json,
    "python": _parse_pyproject,
    "go": _parse_go_mod,
    "rust": _parse_cargo_toml,
}


def _normalized(name: str) -> str:
    return name.replace("-", "_").lower()


def dependency_versions(
    files: Sequence[ChunkFile],
    read: Callable[[str], str | None],
) -> list[str]:
    """``name@version`` lines for the third-party packages this chunk imports.

    For each changed file the nearest manifest — ``package.json``,
    ``pyproject.toml``, ``go.mod`` or ``Cargo.toml`` — is located by walking up
    from the file's directory to the repository root via ``read``. Only
    packages actually imported on the chunk's added lines are reported. The
    result is sorted and deduplicated; an unreadable or unparseable manifest
    contributes nothing.
    """
    lines: set[str] = set()
    for entry in files:
        language = _language(entry.path)
        manifest = _MANIFESTS.get(language)
        if not manifest:
            continue
        wanted = imported_packages(entry.path, entry.added)
        if not wanted:
            continue
        text = _nearest_manifest(entry.path, manifest, read)
        if not text:
            continue
        try:
            pins = _PARSERS[language](text)
        except Exception:  # noqa: BLE001 - a broken manifest is not a review failure
            continue
        lookup = {_normalized(name): (name, version) for name, version in pins.items()}
        for name in wanted:
            hit = lookup.get(_normalized(name))
            if hit:
                lines.add(f"{hit[0]}@{hit[1]}")
    return sorted(lines)


def _definition_regexes(language: str) -> tuple[re.Pattern[str], ...]:
    if language == "js":
        return (_JS_DEF_RE,)
    if language == "python":
        return (_PY_DEF_RE, _PY_ASSIGN_RE)
    return ()


def _keywords(language: str) -> frozenset[str]:
    if language == "python":
        return _PY_KEYWORDS
    return _JS_KEYWORDS


def _entry_text(lines: Sequence[str], index: int, max_lines: int) -> str:
    first = lines[index]
    depth = sum(first.count(c) for c in "([{") - sum(first.count(c) for c in ")]}")
    out = [first.rstrip()]
    cursor = index + 1
    while depth > 0 and len(out) < max_lines and cursor < len(lines):
        nxt = lines[cursor]
        depth += sum(nxt.count(c) for c in "([{") - sum(nxt.count(c) for c in ")]}")
        out.append(nxt.rstrip())
        cursor += 1
    return "\n".join(out)


def referenced_definitions(
    chunk_files: Sequence[ChunkFile],
    read: Callable[[str], str | None],
    *,
    max_entries: int = MAX_DEFINITION_ENTRIES,
    max_lines_per_entry: int = MAX_LINES_PER_DEFINITION,
    max_chars: int = MAX_DEFINITION_CHARS,
) -> list[str]:
    """``<path>:<line>: <definition>`` lines for symbols the chunk references.

    An identifier used on an added line, not defined inside the chunk's own
    hunks, and defined elsewhere in the SAME served file yields one entry: the
    defining line plus continuation lines up to a balanced bracket or
    ``max_lines_per_entry``. Order is deterministic (path, then line). Files
    larger than 512 KiB are skipped. When the entry or character cap trims the
    list, a final ``… N more definitions omitted`` line says so.
    """
    collected: list[tuple[str, int, str]] = []
    for entry in chunk_files:
        language = _language(entry.path)
        regexes = _definition_regexes(language)
        if not regexes:
            continue
        content = read(entry.path)
        if not content or len(content.encode("utf-8", "ignore")) > MAX_FILE_BYTES:
            continue
        keywords = _keywords(language)
        referenced = {
            name
            for text in entry.added
            for name in _IDENT_RE.findall(text)
            if name not in keywords
        }
        for text in entry.added:
            for regex in regexes:
                match = regex.match(text)
                if match:
                    referenced.discard(match.group(1))
        if not referenced:
            continue
        lines = content.splitlines()
        seen: set[str] = set()
        for idx, text in enumerate(lines):
            number = idx + 1
            if number in entry.hunk_lines:
                continue
            for regex in regexes:
                match = regex.match(text)
                if not match:
                    continue
                name = match.group(1)
                if name in referenced and name not in seen:
                    seen.add(name)
                    collected.append(
                        (entry.path, number, _entry_text(lines, idx, max_lines_per_entry))
                    )
                break

    collected.sort(key=lambda item: (item[0], item[1]))
    out: list[str] = []
    used = 0
    for path, number, text in collected:
        rendered = f"{path}:{number}: {text}"
        if len(out) >= max_entries or used + len(rendered) > max_chars:
            out.append(f"… {len(collected) - len(out)} more definitions omitted")
            break
        out.append(rendered)
        used += len(rendered)
    return out


def render_context_blocks(dep_lines: Sequence[str], def_lines: Sequence[str]) -> str:
    """Render the two prompt blocks, omitting each when it has no lines.

    Returns the empty string when both are empty, so the prompt slot leaves no
    stray header behind.
    """
    blocks: list[str] = []
    if dep_lines:
        blocks.append(DEPENDENCY_HEADER + "\n\n" + "\n".join(dep_lines))
    if def_lines:
        blocks.append(DEFINITIONS_HEADER + "\n\n" + "\n".join(def_lines))
    return "\n\n".join(blocks)
