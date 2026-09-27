"""Safe, stdlib-only reading of Maven ``pom.xml`` files for JVM chunk context (#20).

A Java or Kotlin chunk's dependency block needs the version of each declared
artifact, and in Maven that version is often not in the file that declares the
dependency: it comes from a ``<properties>`` entry, from a parent pom's
``<dependencyManagement>``, or from a BOM the build imports. This module
resolves one pom and its in-repository parents into plain coordinates.

**Safety.** A pom is untrusted input from the pull request. Any text holding a
``<!DOCTYPE`` or ``<!ENTITY`` declaration (any case) is refused before the XML
parser ever sees it, so entity expansion, external entities and the
billion-laughs payload are never evaluated. Text larger than
:data:`MAX_POM_BYTES` (UTF-8) is refused the same way, and a parse error, a
root other than ``<project>``, or a failing ``read`` contributes nothing.
:func:`resolve_pom` never raises.

**Parents.** ``<parent>`` is followed through ``<relativePath>``, which defaults
to ``../pom.xml`` and resolves against the child pom's directory; a path naming
a directory gets ``pom.xml`` appended. A parent is accepted only when its
``<artifactId>`` equals the child's ``<parent><artifactId>``. The walk stops at
an empty ``<relativePath/>``, a path outside the repository, an unreadable,
refused or mismatched pom, after :data:`MAX_POM_PARENTS` parents, or on
revisiting a path. The first parent it does not read (for any reason but a
revisit) is the *external parent*.

**Properties.** ``${name}`` resolves from ``<properties>`` merged over the whole
chain (a child overrides its parents), plus ``project.version``,
``project.groupId`` and ``project.parent.version`` of the resolved pom, in at
most :data:`MAX_PROPERTY_PASSES` passes. An unresolved placeholder stays
literal, and a pass that would grow a value past
:data:`MAX_INTERPOLATED_CHARS` characters is abandoned, keeping the value as
the previous pass left it.

**Versions and owners.** ``<dependencies>`` and
``<dependencyManagement><dependencies>`` are read in every pom of the chain,
the nearest pom winning per ``groupId:artifactId``. A managed entry with
``<type>pom</type>`` and ``<scope>import</scope>`` is a BOM owner, not a
dependency. A dependency without a version takes the nearest managed version;
failing that, its version is inherited from something this module cannot
read. Its candidate owners are the external parent, then the imported BOMs,
nearest pom first and in declaration order, and :func:`managed_dependency`
applies the owner rule (#25), which offline can only guess which candidate
manages an artifact:

- exactly one candidate is named: ``g:a@(managed by bg:ba@bv)``;
- among several, the one whose groupId shares the most leading segments with
  the dependency's, at least :data:`MIN_OWNER_SHARED_SEGMENTS` and strictly
  more than every other, is named as a guess:
  ``g:a@(likely managed by bg:ba@bv)``;
- otherwise none is named: ``g:a@(managed by one of 3 imported BOMs: ba1,
  ba2, ba3)``, or ``the parent or one of 2 imported BOMs: ...`` when the
  external parent is a candidate (it is listed first, and the count names the
  BOMs only), or ``one of 2 platforms: ...`` for Gradle. At most
  :data:`MAX_LISTED_OWNERS` artifactIds are listed, then ``+K more``.

A dependency with neither a version nor a candidate owner renders no line.

All I/O goes through a caller-supplied ``read(path) -> str | None`` over
repository-relative, ``/``-separated paths, so the caller owns fetching and
caching.
"""
from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass

MAX_POM_BYTES = 512 * 1024
MAX_POM_PARENTS = 5
MAX_PROPERTY_PASSES = 5
MAX_INTERPOLATED_CHARS = 1024
MIN_OWNER_SHARED_SEGMENTS = 2
MAX_LISTED_OWNERS = 3
OWNER_KINDS = ("bom", "parent", "platform")

_UNSAFE_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(r"\$\{([^${}]+)\}")
_DEFAULT_RELATIVE_PATH = "../pom.xml"

Reader = Callable[[str], str | None]


@dataclass(frozen=True)
class MavenCoordinate:
    """A ``groupId:artifactId`` pair with an optional version, such as a BOM."""

    group_id: str
    artifact_id: str
    version: str | None = None

    @property
    def name(self) -> str:
        """``groupId:artifactId``."""
        return f"{self.group_id}:{self.artifact_id}"

    def render(self) -> str:
        """``g:a@v``, or ``g:a`` when the version is unknown."""
        return f"{self.name}@{self.version}" if self.version else self.name


@dataclass(frozen=True)
class MavenDependency:
    """One declared or managed dependency after property resolution.

    ``version`` is ``None`` when no pom in the chain states it; ``managed_by``
    then names the owner whose management supplies it, a best guess when
    ``likely`` is true, or is ``None`` when no owner can be named. ``owners``
    then holds the candidates none of which could be named, in declared order,
    and ``owner_kind`` (one of :data:`OWNER_KINDS`) says what they are.
    """

    group_id: str
    artifact_id: str
    version: str | None = None
    managed_by: MavenCoordinate | None = None
    likely: bool = False
    owners: tuple[MavenCoordinate, ...] = ()
    owner_kind: str = "bom"

    @property
    def name(self) -> str:
        """``groupId:artifactId``."""
        return f"{self.group_id}:{self.artifact_id}"

    def line(self) -> str | None:
        """``g:a@v``, ``g:a@([likely ]managed by bg:ba@bv)``, ``g:a@(managed by one of ...)``, or ``None``."""
        if self.version:
            return f"{self.name}@{self.version}"
        if self.managed_by is not None:
            guess = "likely " if self.likely else ""
            return f"{self.name}@({guess}managed by {self.managed_by.render()})"
        if self.owners:
            return f"{self.name}@(managed by {_one_of(self.owners, self.owner_kind)})"
        return None


def _one_of(owners: tuple[MavenCoordinate, ...], kind: str) -> str:
    listed = [owner.artifact_id for owner in owners[:MAX_LISTED_OWNERS]]
    if len(owners) > MAX_LISTED_OWNERS:
        listed.append(f"+{len(owners) - MAX_LISTED_OWNERS} more")
    if kind == "parent":
        boms = len(owners) - 1
        head = "the parent or an imported BOM" if boms == 1 else f"the parent or one of {boms} imported BOMs"
    elif kind == "platform":
        head = f"one of {len(owners)} platforms"
    else:
        head = f"one of {len(owners)} imported BOMs"
    return f"{head}: {', '.join(listed)}"


def _leading_shared(left: list[str], right: list[str]) -> int:
    count = 0
    for a, b in zip(left, right, strict=False):
        if a != b:
            break
        count += 1
    return count


def managed_dependency(
    group_id: str,
    artifact_id: str,
    owners: tuple[MavenCoordinate, ...] | list[MavenCoordinate],
    kind: str = "bom",
) -> MavenDependency:
    """A version-less dependency whose version comes from one of ``owners`` (#25).

    ``owners`` are the candidate owners in declared order: the imported BOMs
    (``kind="bom"``), the external parent followed by the imported BOMs
    (``kind="parent"``), or the Gradle platforms (``kind="platform"``). With
    one candidate it is named. With several, the candidate whose groupId shares
    the most leading segments with ``group_id``, at least
    :data:`MIN_OWNER_SHARED_SEGMENTS` and strictly more than every other, is
    named as a likely owner; otherwise none is named and the line lists the
    candidates. With none, the dependency renders no line.
    """
    owners = tuple(owners)
    if len(owners) <= 1:
        return MavenDependency(group_id, artifact_id, None, owners[0] if owners else None)
    segments = group_id.split(".")
    scores = [_leading_shared(owner.group_id.split("."), segments) for owner in owners]
    best = max(scores)
    if best >= MIN_OWNER_SHARED_SEGMENTS and scores.count(best) == 1:
        return MavenDependency(group_id, artifact_id, None, owners[scores.index(best)], likely=True)
    return MavenDependency(group_id, artifact_id, None, None, owners=owners, owner_kind=kind)


@dataclass(frozen=True)
class MavenProject:
    """The resolved view of one pom and the in-repository parents it inherits.

    ``group_id``, ``artifact_id`` and ``version`` are the pom's own, falling
    back to ``<parent>`` for the group and version. ``dependencies`` holds the
    declared dependencies first, then the managed-only entries, each unique
    by ``groupId:artifactId``. ``pom_paths`` lists every pom read, the given
    one first.
    """

    path: str
    group_id: str | None
    artifact_id: str | None
    version: str | None
    dependencies: tuple[MavenDependency, ...] = ()
    boms: tuple[MavenCoordinate, ...] = ()
    external_parent: MavenCoordinate | None = None
    pom_paths: tuple[str, ...] = ()

    def lines(self) -> list[str]:
        """Every renderable dependency line, sorted and deduplicated."""
        return sorted({line for dep in self.dependencies if (line := dep.line())})


@dataclass(frozen=True)
class _Entry:
    group_id: str
    artifact_id: str
    version: str
    type: str
    scope: str


@dataclass(frozen=True)
class _ParentRef:
    group_id: str
    artifact_id: str
    version: str
    relative_path: str | None


@dataclass(frozen=True)
class _Pom:
    group_id: str
    artifact_id: str
    version: str
    parent: _ParentRef | None
    properties: tuple[tuple[str, str], ...]
    dependencies: tuple[_Entry, ...]
    managed: tuple[_Entry, ...]


class _TooLong(Exception):
    pass


def _parse(text: str) -> ET.Element:
    return ET.fromstring(text)


def _safe_root(text: object) -> ET.Element | None:
    if not isinstance(text, str) or not text.strip():
        return None
    if len(text) > MAX_POM_BYTES or len(text.encode("utf-8", "ignore")) > MAX_POM_BYTES:
        return None
    if _UNSAFE_RE.search(text):
        return None
    try:
        root = _parse(text)
    except Exception:  # noqa: BLE001 - a broken pom is not a review failure
        return None
    for element in root.iter():
        if isinstance(element.tag, str) and element.tag.startswith("{"):
            element.tag = element.tag.split("}", 1)[1]
    return root if root.tag == "project" else None


def _text(element: ET.Element, tag: str) -> str:
    child = element.find(tag)
    if child is None:
        return ""
    return (child.text or "").strip()


def _entries(container: ET.Element | None) -> tuple[_Entry, ...]:
    if container is None:
        return ()
    out: list[_Entry] = []
    for dep in container.findall("dependency"):
        group_id = _text(dep, "groupId")
        artifact_id = _text(dep, "artifactId")
        if not group_id or not artifact_id:
            continue
        out.append(_Entry(
            group_id=group_id,
            artifact_id=artifact_id,
            version=_text(dep, "version"),
            type=_text(dep, "type") or "jar",
            scope=_text(dep, "scope"),
        ))
    return tuple(out)


def _read_pom(text: object) -> _Pom | None:
    root = _safe_root(text)
    if root is None:
        return None
    parent: _ParentRef | None = None
    parent_el = root.find("parent")
    if parent_el is not None and _text(parent_el, "artifactId"):
        relative_el = parent_el.find("relativePath")
        parent = _ParentRef(
            group_id=_text(parent_el, "groupId"),
            artifact_id=_text(parent_el, "artifactId"),
            version=_text(parent_el, "version"),
            relative_path=None if relative_el is None else (relative_el.text or "").strip(),
        )
    properties: list[tuple[str, str]] = []
    properties_el = root.find("properties")
    if properties_el is not None:
        for prop in properties_el:
            if isinstance(prop.tag, str):
                properties.append((prop.tag, (prop.text or "").strip()))
    return _Pom(
        group_id=_text(root, "groupId"),
        artifact_id=_text(root, "artifactId"),
        version=_text(root, "version"),
        parent=parent,
        properties=tuple(properties),
        dependencies=_entries(root.find("dependencies")),
        managed=_entries(root.find("dependencyManagement/dependencies")),
    )


def _safe_read(read: Reader, path: str) -> str | None:
    try:
        text = read(path)
    except Exception:  # noqa: BLE001 - an unreadable pom is not a review failure
        return None
    return text if isinstance(text, str) else None


def _parent_path(child_path: str, relative_path: str | None) -> str | None:
    if relative_path == "":
        return None
    relative = (relative_path or _DEFAULT_RELATIVE_PATH).replace("\\", "/")
    if relative.startswith("/"):
        return None
    joined = posixpath.join(posixpath.dirname(child_path), relative)
    if not joined.endswith(".xml"):
        joined = posixpath.join(joined, "pom.xml")
    target = posixpath.normpath(joined)
    if target == ".." or target.startswith("../"):
        return None
    return target


def _expand_once(value: str, properties: Mapping[str, str]) -> str:
    size = 0

    def swap(match: re.Match[str]) -> str:
        nonlocal size
        replacement = properties.get(match.group(1), match.group(0))
        size += len(replacement) - len(match.group(0))
        if len(value) + size > MAX_INTERPOLATED_CHARS:
            raise _TooLong
        return replacement

    return _PLACEHOLDER_RE.sub(swap, value)


def _interpolate(value: str, properties: Mapping[str, str]) -> str:
    for _ in range(MAX_PROPERTY_PASSES):
        try:
            expanded = _expand_once(value, properties)
        except _TooLong:
            return value
        if expanded == value:
            return value
        value = expanded
    return value


def _chain(path: str, read: Reader, text: str | None) -> tuple[list[_Pom], list[str], _ParentRef | None] | None:
    child = _read_pom(text if text is not None else _safe_read(read, path))
    if child is None:
        return None
    chain = [child]
    paths = [path]
    current = posixpath.normpath(path)
    visited = {current}
    pom = child
    while pom.parent is not None:
        ref = pom.parent
        if len(chain) - 1 >= MAX_POM_PARENTS:
            return chain, paths, ref
        target = _parent_path(current, ref.relative_path)
        if target is None:
            return chain, paths, ref
        if target in visited:
            return chain, paths, None
        visited.add(target)
        parent = _read_pom(_safe_read(read, target))
        if parent is None or parent.artifact_id != ref.artifact_id:
            return chain, paths, ref
        chain.append(parent)
        paths.append(target)
        current = target
        pom = parent
    return chain, paths, None


def _resolve(path: str, read: Reader, text: str | None) -> MavenProject | None:
    walked = _chain(path, read, text)
    if walked is None:
        return None
    chain, paths, external_ref = walked
    child = chain[0]
    own_parent = child.parent
    group_id = child.group_id or (own_parent.group_id if own_parent else "")
    version = child.version or (own_parent.version if own_parent else "")

    properties: dict[str, str] = {}
    for pom in reversed(chain):
        properties.update(pom.properties)
    if version:
        properties["project.version"] = version
    if group_id:
        properties["project.groupId"] = group_id
    if own_parent is not None and own_parent.version:
        properties["project.parent.version"] = own_parent.version

    def resolve(value: str) -> str:
        return _interpolate(value, properties) if value else value

    external = None
    if external_ref is not None:
        external = MavenCoordinate(
            resolve(external_ref.group_id),
            resolve(external_ref.artifact_id),
            resolve(external_ref.version) or None,
        )

    boms: dict[tuple[str, str], MavenCoordinate] = {}
    managed: dict[tuple[str, str], str] = {}
    for pom in chain:
        for entry in pom.managed:
            key = (resolve(entry.group_id), resolve(entry.artifact_id))
            if resolve(entry.scope) == "import" and resolve(entry.type) == "pom":
                boms.setdefault(key, MavenCoordinate(key[0], key[1], resolve(entry.version) or None))
                continue
            if not managed.get(key):
                managed[key] = resolve(entry.version)

    owners = ([external] if external is not None else []) + list(boms.values())
    kind = "parent" if external is not None else "bom"

    def dependency(key: tuple[str, str], stated: str) -> MavenDependency:
        resolved = stated or managed.get(key, "")
        if resolved:
            return MavenDependency(key[0], key[1], resolved)
        return managed_dependency(key[0], key[1], owners, kind)

    dependencies: dict[tuple[str, str], MavenDependency] = {}
    for pom in chain:
        for entry in pom.dependencies:
            key = (resolve(entry.group_id), resolve(entry.artifact_id))
            if key not in dependencies and key not in boms:
                dependencies[key] = dependency(key, resolve(entry.version))
    for key in managed:
        if key not in dependencies and key not in boms:
            dependencies[key] = dependency(key, "")

    return MavenProject(
        path=path,
        group_id=resolve(group_id) or None,
        artifact_id=resolve(child.artifact_id) or None,
        version=resolve(version) or None,
        dependencies=tuple(dependencies.values()),
        boms=tuple(boms.values()),
        external_parent=external,
        pom_paths=tuple(paths),
    )


def resolve_pom(path: str, read: Reader, text: str | None = None) -> MavenProject | None:
    """Resolve the pom at ``path`` and its in-repository parents.

    ``read`` maps a repository-relative path to its text, or ``None``. When
    the caller already holds the pom's text it may pass it as ``text``, and
    ``path`` is then not read again (parents still are). Returns ``None`` when
    the pom itself is unreadable, refused, malformed or not a ``<project>``;
    never raises.
    """
    try:
        return _resolve(path, read, text)
    except Exception:  # noqa: BLE001 - a broken pom is not a review failure
        return None
