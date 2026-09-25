"""Gradle build files and the version catalog, reduced to declared dependencies (#20).

A leaf module for the JVM dependency-version context: it imports nothing from
prxref and performs no I/O of its own. Every read goes through an injected
``read(path) -> str | None`` callable resolving a repository-relative path, the
same contract :mod:`prxref.chunk_context` uses, so the caller owns caching and
the forge.

What is read:

- ``build.gradle`` and ``build.gradle.kts`` string notation, ``"g:a:v"`` or
  ``"g:a"`` in either quote style, wherever it appears outside a comment. That
  covers every configuration call (``implementation(...)``, ``api "..."``,
  ``testImplementation``, ``classpath`` and so on) and calls wrapped across
  lines. A classifier (``"g:a:v:jdk8"``) or an extension (``"g:a:v@aar"``) is
  dropped; an interpolated version (``"g:a:$ver"``) is kept literally.
- ``platform(...)``, ``enforcedPlatform(...)`` and the dependency-management
  plugin's ``mavenBom`` as BOM owners, either as a string or as a catalog
  accessor (``platform(libs.spring.boot.bom)``).
- ``group = "g"`` or ``group "g"`` on a line of its own: the project's own group.
- The version catalog (``gradle/libs.versions.toml``, or ``libs.versions.toml``
  beside the build file), probed only when the build file mentions ``libs.``.
  ``[libraries]`` in string, ``module`` and ``group``/``name`` form, with a
  version given as a string, ``version.ref`` or ``{ ref = ... }`` into
  ``[versions]``. Every catalog library counts as declared.

Not read: map notation (``group: 'g', name: 'a'``), catalogs under another name
or location (``versionCatalogs { create(...) }``), versions set through
variables or ``gradle.properties``, and ``[bundles]`` / ``[plugins]``.

Nothing here raises: an unparseable build file or broken TOML contributes
nothing.
"""
from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass

BUILD_FILE_NAMES = ("build.gradle.kts", "build.gradle")
CATALOG_FILE_NAME = "libs.versions.toml"

_COORDINATE = (
    r"(?P<group>[A-Za-z0-9_][A-Za-z0-9_.\-]*)"
    r":(?P<artifact>[A-Za-z0-9_][A-Za-z0-9_.\-]*)"
    r"(?::(?P<version>[^'\"\s:@]+))?"
    r"(?::[^'\"\s:@]+)?"
    r"(?:@[A-Za-z0-9_]+)?"
)
_COORDINATE_RE = re.compile(_COORDINATE)
_DECLARATION_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_$.])(?P<bom>platform|enforcedPlatform|mavenBom)\s*(?:\(\s*|[ \t]+))?"
    r"(?P<quote>['\"])" + _COORDINATE + r"(?P=quote)"
)
_PLATFORM_ACCESSOR_RE = re.compile(
    r"(?<![A-Za-z0-9_$.])(?:platform|enforcedPlatform)\s*\(\s*libs\.(?P<accessor>[A-Za-z0-9_.]+)\s*\)"
)
_CATALOG_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_$.])libs\.")
_GROUP_RE = re.compile(
    r"^[ \t]*(?:project\.)?group[ \t]*(?:=[ \t]*|[ \t]+)"
    r"(?P<quote>['\"])(?P<group>[A-Za-z0-9_][A-Za-z0-9_.\-]*)(?P=quote)[ \t]*;?[ \t]*$",
    re.M,
)
_LEXEME_RE = re.compile(
    r"\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|//[^\n]*|/\*.*?(?:\*/|\Z)",
    re.S,
)
_ALIAS_SEPARATOR_RE = re.compile(r"[-_]")
_RICH_VERSION_KEYS = ("strictly", "require", "prefer")


@dataclass(frozen=True)
class GradleDependency:
    """One declared coordinate: ``group:artifact`` and its version.

    ``version`` is ``None`` when the declaration names none, for example
    ``"com.fasterxml.jackson.core:jackson-databind"`` under a platform BOM, or a
    catalog ``version.ref`` naming a missing ``[versions]`` key.
    """

    group: str
    artifact: str
    version: str | None = None


@dataclass(frozen=True)
class GradleBuild:
    """What one Gradle build file, plus its catalog, declares.

    ``group`` is the project's own group, or ``None`` when the file sets none.
    ``dependencies`` holds ordinary declarations and every catalog library that
    is not used as a platform; ``boms`` holds the ``platform``,
    ``enforcedPlatform`` and ``mavenBom`` owners. Both are deduplicated and
    keep declaration order, build file first. Which BOM manages a versionless
    dependency is left to the caller. ``catalog_path`` names the catalog that
    was read, or is ``None`` when none was probed or found.
    """

    group: str | None = None
    dependencies: tuple[GradleDependency, ...] = ()
    boms: tuple[GradleDependency, ...] = ()
    catalog_path: str | None = None


@dataclass(frozen=True)
class _Scan:
    group: str | None
    dependencies: tuple[GradleDependency, ...]
    boms: tuple[GradleDependency, ...]
    platform_accessors: frozenset[str]
    mentions_catalog: bool


def _strip_comment(match: re.Match[str]) -> str:
    token = match.group(0)
    if token.startswith("/"):
        return "\n" * token.count("\n")
    return token


def _unique(items: Iterable[GradleDependency]) -> tuple[GradleDependency, ...]:
    return tuple(dict.fromkeys(items))


def _scan(text: str) -> _Scan:
    if not isinstance(text, str):
        return _Scan(None, (), (), frozenset(), False)
    code = _LEXEME_RE.sub(_strip_comment, text.replace("\r\n", "\n"))
    dependencies: list[GradleDependency] = []
    boms: list[GradleDependency] = []
    for match in _DECLARATION_RE.finditer(code):
        dependency = GradleDependency(match["group"], match["artifact"], match["version"])
        (boms if match["bom"] else dependencies).append(dependency)
    group_match = _GROUP_RE.search(code)
    return _Scan(
        group=group_match["group"] if group_match else None,
        dependencies=_unique(dependencies),
        boms=_unique(boms),
        platform_accessors=frozenset(m["accessor"] for m in _PLATFORM_ACCESSOR_RE.finditer(code)),
        mentions_catalog=_CATALOG_MENTION_RE.search(code) is not None,
    )


def _assemble(scan: _Scan, catalog: dict[str, GradleDependency], catalog_path: str | None) -> GradleBuild:
    dependencies = list(scan.dependencies)
    boms = list(scan.boms)
    for alias, dependency in catalog.items():
        accessor = _ALIAS_SEPARATOR_RE.sub(".", alias)
        (boms if accessor in scan.platform_accessors else dependencies).append(dependency)
    return GradleBuild(
        group=scan.group,
        dependencies=_unique(dependencies),
        boms=_unique(boms),
        catalog_path=catalog_path,
    )


def parse_build_file(text: str) -> GradleBuild:
    """Parse one ``build.gradle`` or ``build.gradle.kts`` text, without the catalog.

    Pure and total: a platform given as a catalog accessor stays unresolved,
    and text that cannot be parsed yields an empty :class:`GradleBuild`.
    """
    try:
        return _assemble(_scan(text), {}, None)
    except Exception:  # noqa: BLE001 - a broken build file is not a review failure
        return GradleBuild()


def _rich_version(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in _RICH_VERSION_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _catalog_version(value: object, versions: dict) -> str | None:
    if isinstance(value, dict) and "ref" in value:
        ref = value["ref"]
        return _rich_version(versions.get(ref)) if isinstance(ref, str) else None
    return _rich_version(value)


def _catalog_library(spec: object, versions: dict) -> GradleDependency | None:
    if isinstance(spec, str):
        match = _COORDINATE_RE.fullmatch(spec.strip())
        if not match:
            return None
        return GradleDependency(match["group"], match["artifact"], match["version"])
    if not isinstance(spec, dict):
        return None
    module = spec.get("module")
    if isinstance(module, str):
        group, _, artifact = module.strip().partition(":")
        if not group or not artifact or ":" in artifact:
            return None
    elif isinstance(spec.get("group"), str) and isinstance(spec.get("name"), str):
        group, artifact = spec["group"].strip(), spec["name"].strip()
        if not group or not artifact:
            return None
    else:
        return None
    return GradleDependency(group, artifact, _catalog_version(spec.get("version"), versions))


def parse_catalog(text: str) -> dict[str, GradleDependency]:
    """Map each ``[libraries]`` alias of a version catalog to its coordinate.

    Accepts the string form ``"g:a:v"``, ``{ module = "g:a", ... }`` and
    ``{ group = "g", name = "a", ... }``. A version is a string, a rich version
    (``strictly``, then ``require``, then ``prefer``), or a ``version.ref`` /
    ``{ ref = ... }`` into ``[versions]``; a ref to a missing key gives
    ``None``. Entries keep catalog order. Broken TOML, or a catalog of the
    wrong shape, yields ``{}``.
    """
    try:
        data = tomllib.loads(text)
        versions = data.get("versions")
        if not isinstance(versions, dict):
            versions = {}
        libraries = data.get("libraries")
        if not isinstance(libraries, dict):
            return {}
        out: dict[str, GradleDependency] = {}
        for alias, spec in libraries.items():
            dependency = _catalog_library(spec, versions)
            if dependency is not None:
                out[alias] = dependency
        return out
    except Exception:  # noqa: BLE001 - a broken catalog is not a review failure
        return {}


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


def catalog_paths(build_path: str, root: str = "") -> tuple[str, ...]:
    """The catalog paths probed for ``build_path``, in probe order.

    First the build file's own directory (``gradle/libs.versions.toml``, then
    ``libs.versions.toml``), then ``gradle/libs.versions.toml`` under ``root``.
    ``root`` is a repository-relative directory; the default ``""`` is the
    repository root, and a caller that knows where ``settings.gradle(.kts)``
    lives may pass that directory instead. Duplicates are dropped, so a build
    file at the root yields two paths.
    """
    directory = build_path.rsplit("/", 1)[0] if "/" in build_path else ""
    base = root.strip("/")
    candidates = (
        _join(directory, f"gradle/{CATALOG_FILE_NAME}"),
        _join(directory, CATALOG_FILE_NAME),
        _join(base, f"gradle/{CATALOG_FILE_NAME}"),
    )
    return tuple(dict.fromkeys(candidates))


def _safe_read(read: Callable[[str], str | None], path: str) -> str | None:
    try:
        text = read(path)
    except Exception:  # noqa: BLE001 - a failed read is a read that returned None
        return None
    return text if isinstance(text, str) and text else None


def gradle_build(
    path: str,
    text: str,
    read: Callable[[str], str | None],
    root: str = "",
) -> GradleBuild:
    """Parse the build file ``text`` found at ``path``, merging its version catalog.

    The catalog is probed through ``read`` only when ``text`` mentions
    ``libs.`` outside a comment, at :func:`catalog_paths` ``(path, root)`` in
    order; the first path that reads non-empty is the catalog, even if its TOML
    turns out to be broken. The build file itself is never read here: the
    caller already holds ``text``. Never raises; a failure contributes nothing.
    """
    try:
        scan = _scan(text)
        catalog: dict[str, GradleDependency] = {}
        found: str | None = None
        if scan.mentions_catalog:
            for candidate in catalog_paths(path, root):
                content = _safe_read(read, candidate)
                if content is not None:
                    found = candidate
                    catalog = parse_catalog(content)
                    break
        return _assemble(scan, catalog, found)
    except Exception:  # noqa: BLE001 - a broken build file is not a review failure
        return GradleBuild()
