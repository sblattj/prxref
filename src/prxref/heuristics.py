"""Deterministic, non-LLM heuristics that manufacture Findings directly.

Every other Finding producer in the pipeline (a chunk worker, the systemic
sweep) is an LLM call whose output the quality passes filter or rewrite —
none of them originates a Finding on its own. A heuristic here is the
opposite: pure, computed once over the parsed diff's ``FileDiff`` list, no
model in the loop, so its output can be concatenated onto the LLM-sourced
findings list before the quality passes run and survive them the same way a
model finding would.
"""
from __future__ import annotations

from pathlib import PurePosixPath

from .triage import FileDiff, Finding

# Release-machinery basenames that are unambiguous on their own: version
# manifests, a bare VERSION file, Python's convention-named version modules,
# and the release-please manifest. Case-sensitive — real ecosystem tooling
# (npm, cargo, setuptools, release-please) always emits these exact names.
_MACHINERY_BASENAMES = frozenset({
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "setup.py",
    "setup.cfg",
    "VERSION",
    "version.py",
    "__version__.py",
    ".release-please-manifest.json",
})

# Lockfiles across ecosystems. This list is this module's own — it does not
# import ``systemic._LOCKFILE_BASENAMES``, which is npm-family only and
# private to that module — but is kept consistent with it on the basenames
# they share (package-lock.json, npm-shrinkwrap.json, pnpm-lock.yaml,
# yarn.lock, bun.lockb, bun.lock).
_LOCKFILE_BASENAMES = frozenset({
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "bun.lock",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "go.sum",
    "composer.lock",
})

# Case-insensitive basename prefixes: CHANGELOG.md, changelog.rst,
# HISTORY.txt, RELEASE_NOTES.md all match regardless of extension or case.
_PREFIX_BASENAMES = ("changelog", "history", "release_notes")

# The release-shaped ratio bar, 80%, held as an exact rational (4/5) rather
# than a float: the boundary is checked by cross-multiplication
# (``machinery * _RATIO_DEN >= total * _RATIO_NUM``) so no floating-point
# representation can nudge a file set across or off the line.
_RATIO_NUM = 4
_RATIO_DEN = 5

# A PR must touch at least this many files to be release-shaped at all — a
# single-file diff is never release-shaped regardless of ratio.
_MIN_FILES_FOR_SHAPE = 2

_BODY_SUFFIX = " (deterministic check, no model)"


def _is_release_machinery(path: str) -> bool:
    """True when ``path`` is release machinery under the frozen contract.

    Matching is basename-only, with one exception: any path with a
    ``.changeset`` directory component is machinery regardless of its own
    filename, since a changeset's filename is arbitrary (commonly a random
    two-word slug) and only its parent directory identifies it.
    """
    parts = PurePosixPath(path).parts
    if ".changeset" in parts[:-1]:
        return True
    basename = parts[-1] if parts else path
    if basename in _MACHINERY_BASENAMES or basename in _LOCKFILE_BASENAMES:
        return True
    if basename.endswith(".gemspec"):
        return True
    lower = basename.lower()
    return any(lower.startswith(prefix) for prefix in _PREFIX_BASENAMES)


def release_shape_findings(files: list[FileDiff]) -> list[Finding]:
    """Flag a release-shaped PR that also touches non-machinery source.

    A PR is release-shaped when it changes at least two files
    (:data:`_MIN_FILES_FOR_SHAPE`) and at least 80% of them
    (:data:`_RATIO_NUM` / :data:`_RATIO_DEN`, an exact rational comparison —
    no float) are release machinery: version manifests, changelogs,
    lockfiles, ``.changeset/`` entries, and the release-please manifest.
    Removed files count toward the total like any other changed file.

    When a release-shaped PR also changes one or more non-machinery files,
    returns exactly one file-level (``line=0``) warning naming every
    offending path — sorted, anchored on the first — with a body listing
    each offending path and ending with " (deterministic check, no
    model)". Otherwise returns ``[]``. Pure and deterministic: no LLM call,
    no I/O, no randomness.
    """
    total = len(files)
    if total < _MIN_FILES_FOR_SHAPE:
        return []

    offenders = sorted(f.path for f in files if not _is_release_machinery(f.path))
    machinery = total - len(offenders)

    if machinery * _RATIO_DEN < total * _RATIO_NUM:
        return []
    if not offenders:
        return []

    body = "\n".join(offenders) + _BODY_SUFFIX
    finding = Finding(
        file=offenders[0],
        line=0,
        severity="warning",
        confidence=1.0,
        title=f"Release-shaped PR touches source: {len(offenders)} non-release file(s)",
        body=body,
    )
    return [finding]
