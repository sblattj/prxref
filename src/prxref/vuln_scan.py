"""Known vulnerabilities in the dependency pins a PR changes, from OSV.dev (#36).

Off by default (``PRXREF_VULN_SCAN=off``): when on, the names and versions of
the dependencies the PR changes are sent to a third party, the OSV.dev API at
``PRXREF_OSV_URL``. Nothing else of the PR leaves the runner.

**Manifests.** Matched on the file's basename: ``package.json`` (``npm``),
``requirements*.txt`` and ``pyproject.toml`` (``PyPI``), ``go.mod`` (``Go``),
``Cargo.toml`` (``crates.io``), and, through the JVM context machinery of
:mod:`prxref.jvm_maven` and :mod:`prxref.jvm_gradle` (#20), ``pom.xml``,
``build.gradle``, ``build.gradle.kts`` and ``libs.versions.toml``
(``Maven``, named ``groupId:artifactId``). A removed file is never scanned.

**Changed pins.** A dependency is changed when the PR adds it or changes its
declared version; an unchanged one is never queried. Two ways decide it:

- *Whole file*, when the head file is readable (the forge's head-sha reader,
  or ``--repo-dir``) and every hunk's new-side lines match it, or when the PR
  adds the file: the base file is rebuilt by undoing the hunks, both sides are
  parsed with the same whole-file parsers the chunk context uses
  (:mod:`prxref.chunk_context`, :func:`prxref.jvm_maven.resolve_pom` with
  properties and parents, :func:`prxref.jvm_gradle.gradle_build`,
  :func:`prxref.jvm_gradle.parse_catalog`), and a pin is changed when its
  head version differs from its base version or the base lacks it. A Maven
  property or a catalog ``[versions]`` bump therefore changes every
  dependency that resolves through it.
- *Hunk lines*, otherwise: each hunk's new side and old side are scanned line
  by line for declarations, each in its own section (a ``package.json`` line
  needs its ``"dependencies"`` or ``"devDependencies"`` opener in the hunk, a
  ``pyproject.toml`` string its ``dependencies = [`` array, a ``Cargo.toml``
  line its ``[dependencies]`` style header). A declaration on an added line
  is changed unless the same name and version sit on a removed line, so a
  re-emitted line (a trailing comma, a reorder, a reindent) is not. A Maven
  ``<dependency>`` block must sit whole inside the hunk and touch an added
  line; it is read by :func:`prxref.jvm_maven.resolve_pom` as a one-block
  pom, so a ``${property}`` version stays unresolved here.

Each finding anchors on an added line of the manifest: the first one naming
both the dependency and its version, else its version (a property line),
else its name, else the file's first added line.

**Versions.** A changed pin is queried only with a concrete version:

- npm: ``1.2.3``, ``=1.2.3``, ``v1.2.3``, ``^1.2.3`` or ``~1.2.3`` (a
  prerelease suffix allowed), queried as ``1.2.3``: the exact version, or the
  lowest version a caret or tilde range admits;
- PyPI: one ``==`` specifier, not a wildcard (``requirements*.txt``, and the
  ``[project] dependencies`` of ``pyproject.toml``);
- Go: a ``require`` version, queried without its leading ``v``;
- crates.io: ``1.2.3`` or ``=1.2.3``, as for npm the lowest version Cargo's
  default caret admits;
- Maven: a literal version, not a range and not an unresolved ``${...}``.

Anything else (a range, a tag, a URL, a missing version) is skipped and
counted as ``unpinned``.

**OSV.** One ``POST {osv_url}/v1/querybatch`` with every changed pin (at most
:data:`MAX_QUERIES`; a later page of a query is followed at most
:data:`MAX_PAGES` times), then one ``GET {osv_url}/v1/vulns/<id>`` per
returned id, cached for the run, at most :data:`MAX_DETAIL_FETCHES` of them,
:data:`DETAIL_WORKERS` at a time. Every call has a :data:`REQUEST_TIMEOUT`
second timeout and no retry, and the whole scan a :data:`SCAN_DEADLINE`
second deadline. Any failure (a connection error, a timeout, a non-2xx
status, a body that is not the JSON OSV sends) skips the whole scan: no
finding is made, the review goes on, and the run record says why.

**Advisories.** The ids of one pin that alias each other (a ``GHSA`` record
and the ``PYSEC`` or ``GO`` record for the same CVE) are one advisory. Its
id is the ``GHSA`` one when there is one; its severity is the highest any
member gives, from ``database_specific.severity`` or an ``affected`` entry's
``ecosystem_specific``/``database_specific`` severity. A CVSS vector alone
is not scored, so such an advisory is ``UNKNOWN``. Severity map:
``CRITICAL`` and ``HIGH`` post as ``error``, ``MODERATE``/``MEDIUM``, ``LOW``
and ``UNKNOWN`` as ``warning`` (prxref has no lower tier); the title carries
OSV's own word. The fixed versions are the ``fixed`` events of the ranges
naming this package (``ECOSYSTEM`` and ``SEMVER`` ranges only, so no commit
hash).

**Findings.** One per changed pin and advisory, confidence 1.0, its body
ending with :data:`prxref.heuristics._BODY_SUFFIX`, so
:func:`prxref.heuristics.is_deterministic` exempts it from every pass that
judges a model's claim. ``vuln_max`` (``PRXREF_VULN_MAX``) caps them per
PR: the most severe are kept, ties by manifest path, line, package and id,
and the rest are counted as ``capped``.
"""
from __future__ import annotations

import json
import re
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import requests

from . import jvm_gradle, jvm_maven
from .chunk_context import _parse_cargo_toml, _parse_go_mod, _parse_package_json
from .heuristics import _BODY_SUFFIX
from .triage import FileDiff, Finding, Hunk

VULN_SCAN_MODES: tuple[str, ...] = ("off", "on")
DEFAULT_OSV_URL = "https://api.osv.dev"
DEFAULT_VULN_MAX = 10
REQUEST_TIMEOUT = 10.0
SCAN_DEADLINE = 60.0
MAX_QUERIES = 1000
MAX_PAGES = 3
MAX_DETAIL_FETCHES = 200
DETAIL_WORKERS = 8
ADVISORY_URL = "https://osv.dev/vulnerability/"
SOURCE_LINE = "Source: the OSV.dev advisory database, matched on the exact pinned version."

#: OSV severity word -> rank (higher is worse) and the prxref severity it posts as.
SEVERITY_RANK: Mapping[str, int] = {"CRITICAL": 4, "HIGH": 3, "MODERATE": 2, "LOW": 1, "UNKNOWN": 0}
SEVERITY_MAP: Mapping[str, str] = {
    "CRITICAL": "error", "HIGH": "error", "MODERATE": "warning", "LOW": "warning", "UNKNOWN": "warning",
}
_SEVERITY_WORDS = {"CRITICAL": "CRITICAL", "HIGH": "HIGH", "MODERATE": "MODERATE", "MEDIUM": "MODERATE", "LOW": "LOW"}

Reader = Callable[[str], str | None]


@dataclass(frozen=True)
class Pin:
    """One changed dependency pin: where it is declared and what is queried."""

    ecosystem: str
    name: str
    version: str
    path: str
    line: int


@dataclass
class ScanResult:
    """What one scan made: its findings and the run record's ``vuln_scan`` value."""

    findings: list[Finding] = field(default_factory=list)
    record: dict[str, Any] = field(default_factory=dict)
    note: str = ""


class _Skip(Exception):
    """A network or protocol failure that skips the scan; ``args[0]`` is the reason."""


def off_record() -> dict[str, Any]:
    """The run record's ``vuln_scan`` value when the scan does not run."""
    return _record("off")


def _record(status: str, reason: str | None = None, **counts: int) -> dict[str, Any]:
    out: dict[str, Any] = {"status": status, "reason": reason}
    for key in ("manifests", "queried", "advisories", "posted", "capped", "unpinned"):
        out[key] = int(counts.get(key, 0))
    return out


# ---------------------------------------------------------------- manifests

def manifest_kind(path: str) -> str | None:
    """The manifest kind of ``path`` by basename, or ``None`` when it is not one."""
    name = PurePosixPath(path).name
    if name == "package.json":
        return "npm"
    if name.startswith("requirements") and name.endswith(".txt"):
        return "requirements"
    if name == "pyproject.toml":
        return "pyproject"
    if name == "go.mod":
        return "gomod"
    if name == "Cargo.toml":
        return "cargo"
    if name == "pom.xml":
        return "maven"
    if name in jvm_gradle.BUILD_FILE_NAMES:
        return "gradle"
    if name == jvm_gradle.CATALOG_FILE_NAME:
        return "catalog"
    return None


_ECOSYSTEM = {
    "npm": "npm", "requirements": "PyPI", "pyproject": "PyPI", "gomod": "Go",
    "cargo": "crates.io", "maven": "Maven", "gradle": "Maven", "catalog": "Maven",
}

_NPM_VERSION_RE = re.compile(r"^(?:[\^~]|=|v)?\s*v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$")
_CARGO_VERSION_RE = re.compile(r"^(?:=\s*)?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$")
_PEP440_PIN_RE = re.compile(r"^==\s*([0-9][0-9A-Za-z.+!-]*)$")
_GO_VERSION_RE = re.compile(r"^v(\d+\.\d+\.\d+\S*)$")
_MAVEN_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")


def concrete_version(kind: str, raw: str | None) -> str | None:
    """The version queried for a declared ``raw`` version, or ``None`` when it is not pinned."""
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if kind == "npm":
        m = _NPM_VERSION_RE.match(raw)
        return m.group(1) if m else None
    if kind in ("requirements", "pyproject"):
        m = _PEP440_PIN_RE.match(raw)
        return m.group(1) if m and "*" not in raw else None
    if kind == "gomod":
        m = _GO_VERSION_RE.match(raw)
        return m.group(1) if m else None
    if kind == "cargo":
        m = _CARGO_VERSION_RE.match(raw)
        return m.group(1) if m else None
    if kind in ("maven", "gradle", "catalog"):
        if "${" in raw or "$" in raw or raw.startswith("latest.") or raw.endswith("+"):
            return None
        return raw if _MAVEN_VERSION_RE.match(raw) else None
    return None


def _norm_name(kind: str, name: str) -> str:
    if kind in ("requirements", "pyproject"):
        return re.sub(r"[-_.]+", "-", name).lower()
    return name


_REQ_LINE_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(.*)$")


def _requirement(spec: str) -> tuple[str, str] | None:
    """``(name, specifier)`` of one PEP 508 requirement string, or ``None``."""
    spec = spec.split(";", 1)[0].strip()
    if not spec or spec.startswith("-") or "://" in spec or " @ " in spec:
        return None
    spec = re.split(r"\s+--", spec, maxsplit=1)[0].strip()
    m = _REQ_LINE_RE.match(spec)
    if not m:
        return None
    return m.group(1), m.group(2).replace(" ", "")


def _requirements_line(text: str) -> tuple[str, str] | None:
    return _requirement(re.split(r"(?:^|\s)#", text, maxsplit=1)[0])


def _parse_requirements(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        found = _requirements_line(line)
        if found:
            out.setdefault(found[0], found[1])
    return out


def _parse_pyproject_pins(text: str) -> dict[str, str]:
    data = tomllib.loads(text)
    project = data.get("project")
    out: dict[str, str] = {}
    if isinstance(project, dict):
        for spec in project.get("dependencies") or []:
            if isinstance(spec, str) and (found := _requirement(spec)):
                out.setdefault(found[0], found[1])
    return out


def _maven_full(path: str, text: str, read: Reader) -> dict[str, str | None]:
    project = jvm_maven.resolve_pom(path, read, text=text)
    if project is None:
        return {}
    return {dep.name: dep.version for dep in project.dependencies}


def _gradle_full(path: str, text: str, read: Reader) -> dict[str, str | None]:
    build = jvm_gradle.gradle_build(path, text, read, root="")
    return {f"{dep.group}:{dep.artifact}": dep.version for dep in build.dependencies}


def _catalog_full(text: str) -> dict[str, str | None]:
    return {f"{dep.group}:{dep.artifact}": dep.version for dep in jvm_gradle.parse_catalog(text).values()}


def _whole_file(kind: str, path: str, text: str, read: Reader) -> dict[str, str | None]:
    """``{name: raw version}`` declared by one whole manifest; ``{}`` when it does not parse."""
    if not text.strip():
        return {}
    try:
        if kind == "npm":
            return dict(_parse_package_json(text))
        if kind == "requirements":
            return dict(_parse_requirements(text))
        if kind == "pyproject":
            return dict(_parse_pyproject_pins(text))
        if kind == "gomod":
            return dict(_parse_go_mod(text))
        if kind == "cargo":
            return dict(_parse_cargo_toml(text))
        if kind == "maven":
            return _maven_full(path, text, read)
        if kind == "gradle":
            return _gradle_full(path, text, read)
        if kind == "catalog":
            return _catalog_full(text)
    except Exception:  # noqa: BLE001 - an unparseable manifest declares nothing
        return {}
    return {}


def _undo_hunks(head: str, hunks: Sequence[Hunk]) -> str | None:
    """The base file rebuilt from ``head`` by undoing ``hunks``; ``None`` when they do not match it."""
    lines = head.splitlines()
    out: list[str] = []
    pos = 0
    for h in sorted(hunks, key=lambda h: h.new_start):
        start = h.new_start - 1 if h.new_count > 0 else h.new_start
        if start < pos or start > len(lines):
            return None
        out.extend(lines[pos:start])
        pos = start
        for ln in h.lines:
            if ln.kind == "-":
                out.append(ln.text)
                continue
            if pos >= len(lines) or lines[pos] != ln.text:
                return None
            if ln.kind == " ":
                out.append(ln.text)
            pos += 1
    out.extend(lines[pos:])
    return "\n".join(out) + ("\n" if out else "")


# ------------------------------------------------------- hunk-line scanning

_NPM_SECTION_RE = re.compile(r'"([^"]+)"\s*:\s*\{')
_NPM_DEP_RE = re.compile(r'^\s*"([^"]+)"\s*:\s*"([^"]*)"')
_TOML_HEADER_RE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
_TOML_STRING_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'')
_CARGO_DEP_RE = re.compile(
    r"""^\s*([A-Za-z0-9_-]+)\s*=\s*(?:"([^"]*)"|\{[^}]*?\bversion\s*=\s*"([^"]*)")"""
)
_CARGO_TABLE_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]*)"')
_GO_LINE_RE = re.compile(r"^\s*(?:require\s+)?([^\s()]+)\s+(v[^\s/]+)\s*(?://.*)?$")
_MAVEN_BLOCK_RE = re.compile(r"<dependency>.*?</dependency>", re.S)

_Row = tuple[int, str, bool]


def _scan_npm(rows: Sequence[_Row]) -> Iterable[tuple[int, str, str]]:
    section: str | None = None
    for i, (_, text, _) in enumerate(rows):
        opened = _NPM_SECTION_RE.search(text)
        if opened is not None:
            section = opened.group(1)
            continue
        if text.strip().startswith("}"):
            section = None
            continue
        m = _NPM_DEP_RE.match(text)
        if m and section in ("dependencies", "devDependencies"):
            yield i, m.group(1), m.group(2)


def _scan_requirements(rows: Sequence[_Row]) -> Iterable[tuple[int, str, str]]:
    for i, (_, text, _) in enumerate(rows):
        found = _requirements_line(text)
        if found:
            yield i, found[0], found[1]


def _scan_pyproject(rows: Sequence[_Row]) -> Iterable[tuple[int, str, str]]:
    header: str | None = None
    in_array = False
    for i, (_, text, _) in enumerate(rows):
        h = _TOML_HEADER_RE.match(text)
        if h:
            header, in_array = h.group(1).strip(), False
            continue
        stripped = text.split("#", 1)[0]
        opens = re.match(r"^\s*dependencies\s*=\s*\[", stripped)
        if opens and header in (None, "project"):
            in_array = True
            stripped = stripped[opens.end():]
        if not in_array:
            continue
        for m in _TOML_STRING_RE.finditer(stripped):
            found = _requirement(m.group(1) if m.group(1) is not None else m.group(2))
            if found:
                yield i, found[0], found[1]
        if "]" in stripped:
            in_array = False


def _scan_gomod(rows: Sequence[_Row]) -> Iterable[tuple[int, str, str]]:
    block: str | None = None
    for i, (_, text, _) in enumerate(rows):
        line = text.split("//", 1)[0].strip()
        opened = re.match(r"^(require|replace|exclude|retract)\s*\($", line)
        if opened:
            block = opened.group(1)
            continue
        if line == ")":
            block = None
            continue
        if "=>" in line or line.startswith(("module ", "go ", "toolchain ", "replace ", "exclude ", "retract ")):
            continue
        if block not in (None, "require"):
            continue
        if block is None and not line.startswith("require ") and not text[:1].isspace():
            continue
        m = _GO_LINE_RE.match(line)
        if m:
            yield i, m.group(1), m.group(2)


def _scan_cargo(rows: Sequence[_Row]) -> Iterable[tuple[int, str, str]]:
    header: str | None = None
    for i, (_, text, _) in enumerate(rows):
        h = _TOML_HEADER_RE.match(text)
        if h:
            header = h.group(1).strip()
            continue
        if header is None:
            continue
        table = re.match(r"^(?:.*\.)?(?:dev-|build-)?dependencies\.([A-Za-z0-9_-]+)$", header)
        if table:
            m = _CARGO_TABLE_VERSION_RE.match(text)
            if m:
                yield i, table.group(1), m.group(1)
            continue
        if not re.match(r"^(?:.*\.)?(?:dev-|build-)?dependencies$", header):
            continue
        m = _CARGO_DEP_RE.match(text)
        if m:
            yield i, m.group(1), m.group(2) if m.group(2) is not None else m.group(3)


def _scan_gradle(rows: Sequence[_Row]) -> Iterable[tuple[int, str, str | None]]:
    for i, (_, text, _) in enumerate(rows):
        for dep in jvm_gradle.parse_build_file(text).dependencies:
            yield i, f"{dep.group}:{dep.artifact}", dep.version


def _scan_catalog(rows: Sequence[_Row]) -> Iterable[tuple[int, str, str | None]]:
    header: str | None = None
    for i, (_, text, _) in enumerate(rows):
        h = _TOML_HEADER_RE.match(text)
        if h:
            header = h.group(1).strip()
            continue
        if header != "libraries":
            continue
        for dep in jvm_gradle.parse_catalog("[libraries]\n" + text).values():
            yield i, f"{dep.group}:{dep.artifact}", dep.version


def _scan_maven(rows: Sequence[_Row]) -> Iterable[tuple[int, str, str | None]]:
    text = "\n".join(t for _, t, _ in rows)
    starts = [0]
    for _, t, _ in rows:
        starts.append(starts[-1] + len(t) + 1)
    for m in _MAVEN_BLOCK_RE.finditer(text):
        first = next(i for i in range(len(rows)) if starts[i + 1] > m.start())
        last = next(i for i in range(len(rows)) if starts[i + 1] > m.end() - 1)
        pom = f"<project><dependencies>{m.group(0)}</dependencies></project>"
        project = jvm_maven.resolve_pom("pom.xml", lambda _p: None, text=pom)
        if project is None:
            continue
        changed = [i for i in range(first, last + 1) if rows[i][2]]
        for dep in project.dependencies:
            for i in changed or [first]:
                yield i, dep.name, dep.version


_SCANNERS: Mapping[str, Callable[[Sequence[_Row]], Iterable[tuple[int, str, str | None]]]] = {
    "npm": _scan_npm, "requirements": _scan_requirements, "pyproject": _scan_pyproject,
    "gomod": _scan_gomod, "cargo": _scan_cargo, "gradle": _scan_gradle,
    "catalog": _scan_catalog, "maven": _scan_maven,
}


def _side(h: Hunk, *, new: bool) -> list[_Row]:
    rows: list[_Row] = []
    for ln in h.lines:
        if new and ln.kind != "-":
            rows.append((ln.new_line or 0, ln.text, ln.kind == "+"))
        elif not new and ln.kind != "+":
            rows.append((ln.old_line or 0, ln.text, ln.kind == "-"))
    return rows


def _declared_on_changed(kind: str, hunks: Sequence[Hunk], *, new: bool) -> dict[tuple[str, str | None], int]:
    """``{(name, raw version): line}`` for declarations on the changed lines of one side."""
    scan = _SCANNERS[kind]
    out: dict[tuple[str, str | None], int] = {}
    for h in hunks:
        rows = _side(h, new=new)
        try:
            for i, name, raw in scan(rows):
                if rows[i][2]:
                    out.setdefault((_norm_name(kind, name), raw), rows[i][0])
        except Exception:  # noqa: BLE001 - an unparseable hunk declares nothing
            continue
    return out


# ------------------------------------------------------------------ changes

def _added_lines(fd: FileDiff) -> list[tuple[int, str]]:
    return [(ln.new_line, ln.text) for h in fd.hunks for ln in h.lines if ln.kind == "+" and ln.new_line]


def _anchor(fd: FileDiff, kind: str, name: str, raw: str | None) -> int:
    added = _added_lines(fd)
    if not added:
        return 0
    token = name.rsplit(":", 1)[-1] if kind in ("maven", "gradle", "catalog") else name
    version = (raw or "").strip().lstrip("=^~v")

    def has_name(text: str) -> bool:
        if kind in ("requirements", "pyproject"):
            return _norm_name(kind, token) in _norm_name(kind, text)
        return token in text

    for test in (
        lambda t: has_name(t) and bool(version) and version in t,
        lambda t: bool(version) and version in t,
        has_name,
    ):
        for line, text in added:
            if test(text):
                return line
    return added[0][0]


def changed_pins(fd: FileDiff, read: Reader | None) -> tuple[list[Pin], int]:
    """The changed, pinned dependencies of one manifest diff, and how many changed ones were unpinned.

    ``read`` maps a repository-relative path to its head text, or is ``None``.
    Never raises: a file it cannot read or parse yields ``([], 0)``.
    """
    try:
        return _changed_pins(fd, read)
    except Exception:  # noqa: BLE001 - a broken manifest is not a review failure
        return [], 0


def _changed_pins(fd: FileDiff, read: Reader | None) -> tuple[list[Pin], int]:
    kind = manifest_kind(fd.path)
    if kind is None or fd.status == "removed" or fd.is_binary or not _added_lines(fd):
        return [], 0
    safe_read: Reader = _safe(read) if read is not None else (lambda _p: None)
    changes: list[tuple[str, str | None, int]] = []
    head = safe_read(fd.path) if read is not None else None
    base: str | None = None
    if fd.status == "added":
        head = head or _new_side_text(fd)
        base = ""
    elif head is not None:
        base = _undo_hunks(head, fd.hunks)
    if head is not None and base is not None:
        before = {_norm_name(kind, k): v for k, v in _whole_file(kind, fd.path, base, safe_read).items()}
        after = _whole_file(kind, fd.path, head, safe_read)
        for name, raw in after.items():
            if name.startswith("$") or ":$" in name:
                continue
            key = _norm_name(kind, name)
            if key in before and before[key] == raw:
                continue
            changes.append((name, raw, _anchor(fd, kind, name, raw)))
    else:
        removed = _declared_on_changed(kind, fd.hunks, new=False)
        for (name, raw), line in _declared_on_changed(kind, fd.hunks, new=True).items():
            if (name, raw) in removed:
                continue
            changes.append((name, raw, line))
    pins: list[Pin] = []
    unpinned = 0
    seen: set[tuple[str, str]] = set()
    for name, raw, line in changes:
        version = concrete_version(kind, raw)
        if version is None:
            unpinned += 1
            continue
        key = (_norm_name(kind, name), version)
        if key in seen:
            continue
        seen.add(key)
        pins.append(Pin(_ECOSYSTEM[kind], name, version, fd.path, line))
    return pins, unpinned


def _new_side_text(fd: FileDiff) -> str:
    return "\n".join(ln.text for h in fd.hunks for ln in h.lines if ln.kind != "-") + "\n"


def _safe(read: Reader) -> Reader:
    cache: dict[str, str | None] = {}

    def cached(path: str) -> str | None:
        if path not in cache:
            try:
                text = read(path)
            except Exception:  # noqa: BLE001 - a failed read is a read that returned None
                text = None
            cache[path] = text if isinstance(text, str) and text else None
        return cache[path]

    return cached


# --------------------------------------------------------------------- OSV

class _Client:
    """One run's OSV calls: a deadline, no retries, and a per-run detail cache."""

    def __init__(self, base_url: str, *, post: Callable[..., Any], get: Callable[..., Any],
                 deadline: float, clock: Callable[[], float]) -> None:
        self.base = base_url.rstrip("/")
        self._post = post
        self._get = get
        self._deadline = deadline
        self._clock = clock
        self.details: dict[str, dict] = {}

    def _timeout(self) -> float:
        left = self._deadline - self._clock()
        if left <= 0:
            raise _Skip("deadline")
        return min(REQUEST_TIMEOUT, left)

    def _json(self, call: Callable[..., Any], url: str, **kwargs: Any) -> Any:
        try:
            resp = call(url, timeout=self._timeout(), **kwargs)
        except _Skip:
            raise
        except Exception as e:  # noqa: BLE001 - any transport failure skips the scan
            raise _Skip(e.__class__.__name__) from e
        status = getattr(resp, "status_code", None)
        if not isinstance(status, int) or not 200 <= status < 300:
            raise _Skip(f"HTTP {status}")
        try:
            return resp.json()
        except Exception as e:  # noqa: BLE001 - a body OSV would not send
            raise _Skip(e.__class__.__name__) from e

    def query(self, pins: Sequence[Pin]) -> list[list[str]]:
        """The advisory ids of each pin, in pin order."""
        queries = [
            {"package": {"name": p.name, "ecosystem": p.ecosystem}, "version": p.version} for p in pins
        ]
        ids: list[list[str]] = [[] for _ in pins]
        pending = list(range(len(pins)))
        tokens: dict[int, str] = {}
        for _ in range(1 + MAX_PAGES):
            if not pending:
                break
            batch = [dict(queries[i], **({"page_token": tokens[i]} if i in tokens else {})) for i in pending]
            data = self._json(self._post, f"{self.base}/v1/querybatch", json={"queries": batch})
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list) or len(results) != len(batch):
                raise _Skip("bad response")
            following: list[int] = []
            for i, result in zip(pending, results, strict=True):
                if not isinstance(result, dict):
                    raise _Skip("bad response")
                for vuln in result.get("vulns") or []:
                    vid = vuln.get("id") if isinstance(vuln, dict) else None
                    if isinstance(vid, str) and vid and vid not in ids[i]:
                        ids[i].append(vid)
                token = result.get("next_page_token")
                if isinstance(token, str) and token:
                    tokens[i] = token
                    following.append(i)
            pending = following
        return ids

    def fetch(self, ids: Iterable[str]) -> None:
        """Fetch every id's record not yet cached, at most :data:`MAX_DETAIL_FETCHES` in all."""
        wanted = [vid for vid in dict.fromkeys(ids) if vid not in self.details]
        room = max(0, MAX_DETAIL_FETCHES - len(self.details))
        wanted = sorted(wanted, key=lambda v: (not v.startswith("GHSA-"), v))[:room]
        if not wanted:
            return
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
            records = list(pool.map(lambda vid: self._json(self._get, f"{self.base}/v1/vulns/{vid}"), wanted))
        for vid, record in zip(wanted, records, strict=True):
            if not isinstance(record, dict):
                raise _Skip("bad response")
            self.details[vid] = record


# --------------------------------------------------------------- advisories

@dataclass(frozen=True)
class Advisory:
    """One advisory for one pin: aliased OSV records collapsed."""

    id: str
    aliases: tuple[str, ...]
    severity: str
    summary: str
    fixed: tuple[str, ...]


def _record_severity(record: Mapping[str, Any]) -> str:
    words: list[str] = []
    db = record.get("database_specific")
    if isinstance(db, dict):
        words.append(str(db.get("severity") or ""))
    for affected in record.get("affected") or []:
        if not isinstance(affected, dict):
            continue
        for key in ("ecosystem_specific", "database_specific"):
            block = affected.get(key)
            if isinstance(block, dict):
                words.append(str(block.get("severity") or ""))
    found = [_SEVERITY_WORDS[w.strip().upper()] for w in words if w.strip().upper() in _SEVERITY_WORDS]
    return max(found, key=SEVERITY_RANK.__getitem__) if found else "UNKNOWN"


def _fixed(record: Mapping[str, Any], pin: Pin) -> list[str]:
    out: list[str] = []
    want = _norm_name("requirements", pin.name) if pin.ecosystem == "PyPI" else pin.name
    for affected in record.get("affected") or []:
        if not isinstance(affected, dict):
            continue
        package = affected.get("package") if isinstance(affected.get("package"), dict) else {}
        name = str(package.get("name") or "")
        if package.get("ecosystem") not in (None, pin.ecosystem):
            continue
        if name and (_norm_name("requirements", name) if pin.ecosystem == "PyPI" else name) != want:
            continue
        for rng in affected.get("ranges") or []:
            if not isinstance(rng, dict) or rng.get("type") not in ("ECOSYSTEM", "SEMVER"):
                continue
            for event in rng.get("events") or []:
                if isinstance(event, dict) and isinstance(event.get("fixed"), str):
                    out.append(event["fixed"])
    return out


def _clean(text: str, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    text = re.sub(r"@(?=\w)", "@​", text).replace("<", "&lt;").replace(">", "&gt;")
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def advisories(pin: Pin, ids: Sequence[str], details: Mapping[str, Mapping[str, Any]]) -> list[Advisory]:
    """The advisories of one pin: ``ids`` grouped where they alias each other."""
    parent = {vid: vid for vid in ids}

    def root(vid: str) -> str:
        while parent[vid] != vid:
            parent[vid] = parent[parent[vid]]
            vid = parent[vid]
        return vid

    names: dict[str, set[str]] = {}
    for vid in ids:
        record = details.get(vid) or {}
        names[vid] = {vid, *[a for a in record.get("aliases") or [] if isinstance(a, str)]}
    owner: dict[str, str] = {}
    for vid in ids:
        for alias in names[vid]:
            if alias in owner:
                parent[root(vid)] = root(owner[alias])
            else:
                owner[alias] = vid
    groups: dict[str, list[str]] = {}
    for vid in ids:
        groups.setdefault(root(vid), []).append(vid)
    out: list[Advisory] = []
    for members in groups.values():
        severities = [_record_severity(details.get(m) or {}) for m in members]
        primary = sorted(
            members,
            key=lambda m: (not m.startswith("GHSA-"), _record_severity(details.get(m) or {}) == "UNKNOWN", m),
        )[0]
        summary = ""
        for m in [primary, *sorted(members)]:
            record = details.get(m) or {}
            summary = str(record.get("summary") or "").strip() or str(record.get("details") or "").strip()
            if summary:
                break
        fixed: list[str] = []
        for m in [primary, *sorted(members)]:
            fixed.extend(_fixed(details.get(m) or {}, pin))
        every = set().union(*(names[m] for m in members)) - {primary}
        aliases = sorted(every, key=lambda a: (not a.startswith("CVE-"), a))
        out.append(Advisory(
            id=primary,
            aliases=tuple(aliases),
            severity=max(severities, key=SEVERITY_RANK.__getitem__),
            summary=_clean(summary.splitlines()[0] if summary else ""),
            fixed=tuple(dict.fromkeys(fixed))[:3],
        ))
    return out


def advisory_finding(pin: Pin, advisory: Advisory) -> Finding:
    """The finding one advisory makes on the manifest line that changed the pin."""
    also = f" ({', '.join(advisory.aliases)})" if advisory.aliases else ""
    summary = advisory.summary or "no summary given"
    fixed = (
        f"Fixed in {', '.join(advisory.fixed)}; move to a fixed version."
        if advisory.fixed else "OSV names no fixed version."
    )
    body = (
        f"`{pin.name}` {pin.version} ({pin.ecosystem}), changed in this diff, is affected by "
        f"{advisory.id}{also}, severity {advisory.severity}: {summary}. {fixed} "
        f"Details: {ADVISORY_URL}{advisory.id}. {SOURCE_LINE}{_BODY_SUFFIX}"
    )
    return Finding(
        file=pin.path,
        line=pin.line,
        severity=SEVERITY_MAP[advisory.severity],
        confidence=1.0,
        title=f"[{advisory.severity}] {pin.name} {pin.version}: {advisory.id} {summary}",
        body=body,
    )


def is_vuln_finding(finding: Finding) -> bool:
    """True when ``finding`` was made by :func:`advisory_finding`."""
    body = finding.body
    return isinstance(body, str) and body.endswith(_BODY_SUFFIX) and SOURCE_LINE in body


def overflow_note(capped: int, cap: int) -> str:
    """The summary's top-slot line for advisories over the cap; ``""`` when none were capped."""
    if capped <= 0:
        return ""
    plural = "advisories" if capped != 1 else "advisory"
    return (
        f"> ⚠️ Dependency scan: {capped} more known-vulnerability {plural} matched this "
        f"PR's changed dependencies but were not posted (PRXREF_VULN_MAX={cap}); the run record's "
        "vuln_scan lists the counts.\n\n"
    )


# --------------------------------------------------------------------- scan

def scan(
    files: Sequence[FileDiff],
    *,
    read: Reader | None = None,
    osv_url: str = DEFAULT_OSV_URL,
    vuln_max: int = DEFAULT_VULN_MAX,
    post: Callable[..., Any] | None = None,
    get: Callable[..., Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> ScanResult:
    """Scan the changed dependency pins of ``files`` against OSV.

    ``read`` is the head-sha reader, or ``None``. ``post`` and ``get`` default
    to :func:`requests.post` and :func:`requests.get`. Never raises: any
    failure yields no findings and a ``skipped`` record naming it.
    """
    try:
        return _scan(files, read, osv_url, vuln_max, post or requests.post, get or requests.get, clock)
    except Exception as e:  # noqa: BLE001 - the scan is advisory and never fails a review
        return ScanResult(record=_record("skipped", e.__class__.__name__))


def _scan(files, read, osv_url, vuln_max, post, get, clock) -> ScanResult:
    manifests = [fd for fd in files if manifest_kind(fd.path) is not None and fd.status != "removed"]
    pins: list[Pin] = []
    unpinned = 0
    for fd in manifests:
        found, skipped = changed_pins(fd, read)
        pins.extend(found)
        unpinned += skipped
    pins = pins[:MAX_QUERIES]
    counts = {"manifests": len(manifests), "queried": len(pins), "unpinned": unpinned}
    if not pins:
        return ScanResult(record=_record("ok", **counts))
    client = _Client(osv_url, post=post, get=get, deadline=clock() + SCAN_DEADLINE, clock=clock)
    try:
        ids = client.query(pins)
        client.fetch(vid for group in ids for vid in group)
    except _Skip as e:
        return ScanResult(record=_record("skipped", str(e.args[0]), **counts))
    made: list[tuple[Pin, Advisory]] = []
    for pin, group in zip(pins, ids, strict=True):
        made.extend((pin, adv) for adv in advisories(pin, group, client.details))
    made.sort(key=lambda pa: (-SEVERITY_RANK[pa[1].severity], pa[0].path, pa[0].line, pa[0].name, pa[1].id))
    kept, capped = made[:vuln_max], len(made) - min(len(made), vuln_max)
    findings = [advisory_finding(pin, adv) for pin, adv in kept]
    record = _record("ok", advisories=len(made), posted=len(findings), capped=capped, **counts)
    return ScanResult(findings=findings, record=record, note=overflow_note(capped, vuln_max))


def as_json(record: Mapping[str, Any]) -> str:
    """``record`` as one compact JSON line, for a trace or a log."""
    return json.dumps(dict(record), sort_keys=True)
