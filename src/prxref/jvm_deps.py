"""Dependency-version lines for one changed Java or Kotlin file (#20).

Java and Kotlin imports name packages, not artifacts, so the version a changed
file runs against is found in three steps:

**Imports.** The file's added lines are parsed with
:func:`prxref.jvm_lang.parse_imports`. Imports rooted at ``java``, ``jdk``,
``sun`` or ``kotlin`` (:data:`SKIPPED_ROOTS`) are skipped; ``javax`` is kept,
because part of it ships as separate artifacts. When nothing is left, no
manifest is read at all.

**Manifest.** The walk starts in the file's own directory and climbs to the
repository root ``""``, trying :data:`MANIFEST_NAMES` (``pom.xml``, then
``build.gradle.kts``, then ``build.gradle``) at each level. The first
non-empty read wins and ends the walk, even when that file turns out to be
unparseable, so a broken manifest contributes nothing rather than letting a
farther one speak for the module. A pom is resolved by
:func:`prxref.jvm_maven.resolve_pom` (properties, in-repository parents,
imported BOMs) and a Gradle build file by
:func:`prxref.jvm_gradle.gradle_build` (string notation, platforms, the
version catalog). Every read goes through one per-call cache, so no path is
read twice, and a read that raises or returns anything but non-empty text is a
miss.

**Matching.** Imports under the project's own group (the pom's ``groupId`` or
the Gradle ``group``) are skipped. A declared dependency is a candidate for an
import when its groupId has at least :data:`MIN_GROUP_PREFIX_SEGMENTS` segments
and is a leading segment prefix of the import, or when the two share a leading
prefix of at least :data:`MIN_SHARED_SEGMENTS` segments. Among the candidates,
the ones whose artifactId has the most tokens (split on ``-``, ``.`` and
``_``, ignoring tokens that are groupId segments) equal to an import segment
win, and a tie keeps every tied candidate: ``com.fasterxml.jackson.databind``
picks ``jackson-databind`` over ``jackson-core`` and ``jackson-annotations``.

**Lines.** A Maven dependency renders through
:meth:`prxref.jvm_maven.MavenDependency.line`, keeping the owner jvm_maven
chose. A Gradle dependency renders as ``g:a@v``; without a version but with at
least one platform it renders as ``g:a@(managed by bg:ba@bv)``, the owner being
the platform whose group shares the most leading segments with the
dependency's, the first declared on a tie. A winning dependency with neither a
version nor an owner renders no line.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from prxref import jvm_gradle, jvm_maven
from prxref.jvm_lang import jvm_language, parse_imports

MANIFEST_NAMES: tuple[str, ...] = ("pom.xml", *jvm_gradle.BUILD_FILE_NAMES)
CATALOG_FILE_NAME = jvm_gradle.CATALOG_FILE_NAME
SKIPPED_ROOTS = frozenset({"java", "jdk", "sun", "kotlin"})
MIN_GROUP_PREFIX_SEGMENTS = 2
MIN_SHARED_SEGMENTS = 3

_TOKEN_RE = re.compile(r"[-._]")

Reader = Callable[[str], str | None]


@dataclass(frozen=True)
class _Declared:
    group: str
    artifact: str
    line: str | None


def _cached(read: Reader) -> Reader:
    seen: dict[str, str | None] = {}

    def cached(path: str) -> str | None:
        if path not in seen:
            try:
                text = read(path)
            except Exception:  # noqa: BLE001 - a failed read is a read that returned None
                text = None
            seen[path] = text if isinstance(text, str) and text else None
        return seen[path]

    return cached


def _ancestor_dirs(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    dirs = []
    while parts:
        dirs.append("/".join(parts))
        parts = parts[:-1]
    dirs.append("")
    return dirs


def _nearest_manifest(path: str, read: Reader) -> tuple[str, str] | None:
    for directory in _ancestor_dirs(path):
        for name in MANIFEST_NAMES:
            candidate = f"{directory}/{name}" if directory else name
            text = read(candidate)
            if text:
                return candidate, text
    return None


def _shared(left: Sequence[str], right: Sequence[str]) -> int:
    count = 0
    for a, b in zip(left, right, strict=False):
        if a != b:
            break
        count += 1
    return count


def _under(group: str | None, segments: Sequence[str]) -> bool:
    if not group:
        return False
    group_segments = group.split(".")
    return _shared(group_segments, segments) == len(group_segments)


def _is_candidate(group: str, segments: Sequence[str]) -> bool:
    group_segments = group.split(".")
    shared = _shared(group_segments, segments)
    if len(group_segments) >= MIN_GROUP_PREFIX_SEGMENTS and shared == len(group_segments):
        return True
    return shared >= MIN_SHARED_SEGMENTS


def _score(declared: _Declared, segments: Sequence[str]) -> int:
    group_segments = set(declared.group.split("."))
    present = set(segments)
    return sum(
        1 for token in _TOKEN_RE.split(declared.artifact)
        if token not in group_segments and token in present
    )


def _matches(segments: Sequence[str], declared: Sequence[_Declared]) -> list[_Declared]:
    candidates = [dep for dep in declared if _is_candidate(dep.group, segments)]
    if not candidates:
        return []
    scores = [_score(dep, segments) for dep in candidates]
    best = max(scores)
    return [dep for dep, score in zip(candidates, scores, strict=True) if score == best]


def _maven(path: str, text: str, read: Reader) -> tuple[str | None, list[_Declared]]:
    project = jvm_maven.resolve_pom(path, read, text=text)
    if project is None:
        return None, []
    return project.group_id, [
        _Declared(dep.group_id, dep.artifact_id, dep.line()) for dep in project.dependencies
    ]


def _gradle_owner(
    dependency: jvm_gradle.GradleDependency,
    boms: Sequence[jvm_gradle.GradleDependency],
) -> jvm_maven.MavenCoordinate:
    segments = dependency.group.split(".")
    bom = max(boms, key=lambda candidate: _shared(candidate.group.split("."), segments))
    return jvm_maven.MavenCoordinate(bom.group, bom.artifact, bom.version)


def _gradle_line(
    dependency: jvm_gradle.GradleDependency,
    boms: Sequence[jvm_gradle.GradleDependency],
) -> str | None:
    owner = _gradle_owner(dependency, boms) if dependency.version is None and boms else None
    return jvm_maven.MavenDependency(dependency.group, dependency.artifact, dependency.version, owner).line()


def _gradle(path: str, text: str, read: Reader) -> tuple[str | None, list[_Declared]]:
    build = jvm_gradle.gradle_build(path, text, read, root="")
    return build.group, [
        _Declared(dep.group, dep.artifact, _gradle_line(dep, build.boms)) for dep in build.dependencies
    ]


def _dependency_lines(path: str, added: Sequence[str], read: Reader) -> list[str]:
    if not jvm_language(path):
        return []
    imports = [item for item in parse_imports(added) if item.segments[0] not in SKIPPED_ROOTS]
    if not imports:
        return []
    cached = _cached(read)
    found = _nearest_manifest(path, cached)
    if found is None:
        return []
    manifest_path, text = found
    if manifest_path.rsplit("/", 1)[-1] == "pom.xml":
        own_group, declared = _maven(manifest_path, text, cached)
    else:
        own_group, declared = _gradle(manifest_path, text, cached)
    lines: set[str] = set()
    for item in imports:
        if _under(own_group, item.segments):
            continue
        lines.update(dep.line for dep in _matches(item.segments, declared) if dep.line)
    return sorted(lines)


def dependency_lines(path: str, added: Sequence[str], read: Reader) -> list[str]:
    """Dependency-version lines for the imports on a Java or Kotlin file's added lines.

    ``path`` is the changed file's repository-relative path (``.java``, ``.kt``
    or ``.kts``; any other path yields ``[]``), ``added`` holds the text of its
    added lines, and ``read`` maps a repository-relative path to its text or
    ``None``. Returns ``g:a@v`` and ``g:a@(managed by ...)`` lines, sorted and
    deduplicated, the same shape
    :func:`prxref.chunk_context.dependency_versions` returns for other
    languages. ``read`` is never called when every import is skipped, and never
    twice for one path. Never raises: any failure yields ``[]``.
    """
    try:
        return _dependency_lines(path, added, read)
    except Exception:  # noqa: BLE001 - missing context is not a review failure
        return []
