"""Deterministic, non-LLM PR-metadata checks (issue #70).

A companion to :mod:`prxref.heuristics` under the same doctrine: every
check here is pure — computed once from the PR metadata, the parsed diff
and an already-fetched commit list, with no model and no I/O in the loop.
Where the heuristics are always on, these checks are opt-in: they run
only when ``metadata_rules = "on"``, and each is configured by its own key
(``branch_patterns``, ``commit_reference``, ``area_globs`` with
``max_areas_per_pr``).

The settings come from one of two places. ``metadata_rules = "<path>"``
names a separate TOML rules file, read by :func:`load_metadata_rules`
before any network call: the same four settings, with the two pair lists
also writable as tables (``[branch_patterns]`` mapping a type to its
regex, ``[area_globs]`` mapping an area to one glob or a list of them),
validated by the config layer's own validators so a bad file exits 2.
``metadata_rules = "on"`` is the back-compat alias that reads the four
flat config keys instead.

Three checks, one per key, each answering with its notes plus a status
string — ``"pass"`` (configured, and the PR satisfies it), ``"fail"``
(configured, and it found violations) or ``"skipped: <reason>"`` (nothing
configured, or nothing to check it against). A skip is never a violation:
a PR without a resolvable type is not flagged for its branch name, and a
forge that cannot list commits does not fail the reference check. The
statuses, plus every violation as a ``{check, title, detail}`` row, are
the run record's ``metadata_rules`` stamp.

A violation is a :class:`MetadataNote`, never a
:class:`~prxref.triage.Finding` (owner decision OD8): it is about the PR
as a whole, not a line, so it never enters the finding pipeline. The
orchestrator renders the notes in a ``PR metadata`` section of the posted
summary. No quality pass, severity cap, stable id or inline batch sees
them, and they touch neither the verdict nor the exit code —
``PRXREF_FAIL_ON`` counts findings, and these are not findings.
"""
from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .config import (
    _DEFAULTS,
    _METADATA_FLAT_KEYS,
    _RANGES,
    METADATA_RULES_SWITCHES,
    _check_metadata_rules,
    _display_path,
    _file_value,
)
from .forges.base import CommitData, PRData
from .llm import ConfigError
from .rules import match_globs
from .triage import FileDiff

#: The largest rules file :func:`load_metadata_rules` reads, in characters.
#: A fixed cap rather than a config key: a rules file is a few dozen lines.
RULES_FILE_MAX_CHARS = 65536

#: The values of ``metadata_rules`` that are not a rules-file path.
SWITCH_VALUES = METADATA_RULES_SWITCHES

_RULES_FILE_KEYS = _METADATA_FLAT_KEYS

# A conventional-commit type: the word before the optional "(scope)" and
# the ":" that opens a title like "fix(scope): ...". Case-insensitive,
# lowercased for the lookup; a title with no such prefix resolves no type.
_TITLE_TYPE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)(?:\([^)]*\))?:")


@dataclass(frozen=True)
class MetadataNote:
    """One PR-metadata violation, reported in the summary, never inline.

    ``check`` names the check that made it (``branch_pattern``,
    ``commit_reference`` or ``area_globs``), ``title`` is the one-line
    statement the summary section bullets and ``detail`` the sentence
    explaining it.
    """

    check: str
    title: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        """The note as the run record's ``{check, title, detail}`` row."""
        return {"check": self.check, "title": self.title, "detail": self.detail}


@dataclass(frozen=True)
class MetadataRules:
    """The four check settings a metadata rules file supplied.

    The same shapes the flat config keys hold: ``TYPE=REGEX`` and
    ``NAME=GLOB`` strings, a regex string, and the area cap. A key the
    file leaves out keeps its config default (empty, so that check skips;
    a cap of 2).
    """

    branch_patterns: tuple[str, ...] = ()
    commit_reference: str = ""
    area_globs: tuple[str, ...] = ()
    max_areas_per_pr: int = _DEFAULTS["max_areas_per_pr"]


def _pair_entries(key: str, raw: object, label: str, *, many: bool) -> list[str]:
    """``raw`` as ``NAME=VALUE`` strings: a table is flattened, an array kept.

    A table maps each name to one string, or, when ``many`` is true (the
    area globs), to an array of strings that become one entry each. An
    array goes through the config file's own type check. A table name
    holding ``=`` could not round-trip through the ``NAME=VALUE`` form, so
    it is refused.
    """
    if not isinstance(raw, dict):
        return list(_file_value(key, raw, label))
    entries: list[str] = []
    expected = "a string or an array of strings" if many else "a string"
    for name, value in raw.items():
        if "=" in name:
            raise ConfigError(f"{label}: {key}: name {name!r} must not contain '='")
        values = value if many and isinstance(value, list) else [value]
        if not values or not all(isinstance(v, str) for v in values):
            raise ConfigError(
                f"{label}: {key}: {name!r} must map to {expected}, got {value!r}"
            )
        entries.extend(f"{name}={v}" for v in values)
    return entries


def load_metadata_rules(
    path: str | None, *, max_chars: int, source: str,
) -> MetadataRules | None:
    """Load the metadata rules file at ``path``, or ``None`` when it names no file.

    ``None`` and the switch values ``""``, ``off`` and ``on`` are not paths
    and load nothing. The file is UTF-8 TOML of at most ``max_chars``
    characters (the caller passes :data:`RULES_FILE_MAX_CHARS`) holding only
    ``branch_patterns``, ``commit_reference``, ``area_globs`` and
    ``max_areas_per_pr``. Each value is checked by the config layer's
    validators (the file value types, the pattern check behind the flat
    keys and the cap's range), so a file is refused exactly where the same
    value set as a flat key would be. Every failure is a
    :class:`~prxref.llm.ConfigError` starting ``<source>: <file>``, which
    the CLI turns into exit 2 before any network call.
    """
    if path is None or path.strip() in SWITCH_VALUES:
        return None
    display = _display_path(Path(path))
    label = f"{source}: {display}"
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise ConfigError(
            f"{source}: cannot read metadata rules file {path!r}: {exc.strerror}"
        ) from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{label}: not valid UTF-8 (byte {exc.start})") from exc
    if len(text) > max_chars:
        raise ConfigError(
            f"{label}: rules file too large ({len(text)} characters, the limit is {max_chars})"
        )
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{label}: invalid TOML: {exc}") from exc
    for key in parsed:
        if key not in _RULES_FILE_KEYS:
            allowed = ", ".join(_RULES_FILE_KEYS)
            raise ConfigError(f"{label}: unknown key {key!r}; a rules file holds {allowed}")
    cfg: dict[str, object] = {key: _DEFAULTS[key] for key in _RULES_FILE_KEYS}
    if "branch_patterns" in parsed:
        cfg["branch_patterns"] = _pair_entries(
            "branch_patterns", parsed["branch_patterns"], label, many=False,
        )
    if "area_globs" in parsed:
        cfg["area_globs"] = _pair_entries("area_globs", parsed["area_globs"], label, many=True)
    for key in ("commit_reference", "max_areas_per_pr"):
        if key in parsed:
            cfg[key] = _file_value(key, parsed[key], label)
    sources = {key: f"{label}: {key}" for key in _RULES_FILE_KEYS}
    cap = cfg["max_areas_per_pr"]
    cap_range = _RANGES["max_areas_per_pr"]
    if not cap_range.accepts(cap):
        raise ConfigError(f"{sources['max_areas_per_pr']}: {cap_range.describe()}, got {cap!r}")
    _check_metadata_rules(cfg, sources)
    return MetadataRules(
        branch_patterns=tuple(cfg["branch_patterns"]),
        commit_reference=str(cfg["commit_reference"]),
        area_globs=tuple(cfg["area_globs"]),
        max_areas_per_pr=int(cap),
    )


def _label_names(pr: PRData) -> list[str]:
    """The PR's label names, read from the forge-native payload by shape.

    ``PRData.raw`` is the payload the forge returned whole, and label
    shapes differ per forge: GitHub and Gitea carry a list of objects with
    a ``name``, GitLab a plain list of strings. Both shapes are read here
    without asking which forge it is — an adapter whose payload carries
    neither shape simply has no labels, and the branch check falls through
    to the title prefix. Anything that is neither a string nor an object
    with a string ``name`` is dropped, never guessed at.
    """
    raw = pr.raw if isinstance(pr.raw, dict) else {}
    labels = raw.get("labels")
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for label in labels:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict) and isinstance(label.get("name"), str):
            names.append(label["name"])
    return names


def _title_type(title: str) -> str | None:
    """The conventional-commit type of ``title``, lowercased, or ``None``.

    ``fix: x`` and ``feat(api): y`` resolve to ``fix`` and ``feat``; a
    title without the ``type:`` shape resolves nothing. The scope is
    stripped because a team's branch patterns key on the type alone.
    """
    match = _TITLE_TYPE_RE.match(title or "")
    return match.group(1).lower() if match else None


def branch_pattern_check(
    pr: PRData, patterns: Mapping[str, str | re.Pattern[str]],
) -> tuple[list[MetadataNote], str]:
    """Check the source branch against the pattern of the PR's type.

    ``patterns`` maps type → pattern (a string is compiled; the config
    layer already rejected one that does not compile). The type is
    resolved from the PR's labels first — the first label naming a
    configured type, compared case-insensitively — else from the
    conventional-commit prefix of the title. Matching uses
    ``re.fullmatch``, so a team pattern written with ``^``/``$`` anchors
    behaves the same as one without. A PR with no resolvable type, a type
    no entry covers, or no source branch (a ``--diff-file`` run) is
    skipped with the reason; only a resolved type whose pattern the branch
    fails makes a note.
    Pure and deterministic: no LLM call, no I/O, no randomness.
    """
    if not patterns:
        return [], "skipped: no branch patterns"
    branch = pr.source_branch or ""
    if not branch:
        return [], "skipped: no source branch"
    compiled = {
        name.casefold(): (name, pat if isinstance(pat, re.Pattern) else re.compile(pat))
        for name, pat in patterns.items()
    }
    resolved: str | None = None
    source = ""
    for label in _label_names(pr):
        if label.casefold() in compiled:
            resolved, source = label.casefold(), f"label {label!r}"
            break
    if resolved is None:
        title_type = _title_type(pr.title)
        if title_type is not None and title_type in compiled:
            resolved, source = title_type, "the title prefix"
        elif title_type is not None:
            return [], f"skipped: no pattern for type '{title_type}'"
    if resolved is None:
        return [], "skipped: no PR type"
    name, pattern = compiled[resolved]
    if pattern.fullmatch(branch):
        return [], "pass"
    note = MetadataNote(
        check="branch_pattern",
        title=f"Branch '{branch}' does not match the '{name}' pattern",
        detail=(
            f"The PR's type resolves to '{name}' (from {source}), which requires a "
            f"source branch fully matching `{name}={pattern.pattern}`, but the source "
            f"branch is `{branch}`."
        ),
    )
    return [note], "fail"


def commit_reference_check(
    commits: Sequence[CommitData] | None,
    pattern: str | re.Pattern[str],
    *,
    skip_reason: str = "no commit source",
) -> tuple[list[MetadataNote], str]:
    """Check every non-merge commit subject for the reference pattern.

    ``pattern`` is matched with ``re.search`` against each commit's
    subject (the first line of its message): the reference must appear
    somewhere in the subject, not span the whole line. Merge commits (more
    than one parent) are exempt — their subject is the merge line, not the
    author's. ``commits=None`` means no source could list them (a forge
    without ``get_commits``, or a listing that failed) and skips with
    ``skip_reason``; an empty list is a real answer (a squash-merged PR,
    or a range of merges only) and passes vacuously. One note per
    offending commit. Pure and deterministic: no LLM call, no I/O, no
    randomness.
    """
    if not pattern:
        return [], "skipped: no commit reference pattern"
    if commits is None:
        return [], f"skipped: {skip_reason}"
    regex = pattern if isinstance(pattern, re.Pattern) else re.compile(pattern)
    notes: list[MetadataNote] = []
    for commit in commits:
        if commit.parent_count > 1:
            continue
        if regex.search(commit.subject):
            continue
        notes.append(MetadataNote(
            check="commit_reference",
            title=f"Commit {commit.sha[:10]} subject has no '{regex.pattern}' reference",
            detail=(
                f"Every non-merge commit subject must match `{regex.pattern}`, but "
                f"commit `{commit.sha[:10]}` reads: \"{commit.subject}\"."
            ),
        ))
    if notes:
        return notes, "fail"
    return [], "pass"


def area_check(
    files: Sequence[FileDiff],
    globs: Sequence[str],
    max_areas: int,
) -> tuple[list[MetadataNote], str]:
    """Check the diff's paths against named area globs and the area cap.

    ``globs`` are ``name=glob`` entries; entries sharing a name form one
    area (``backend=src/**`` and ``backend=lib/**`` are one area of two
    globs), matched with :func:`prxref.rules.match_globs` — case-sensitive
    ``fnmatch``, ``*`` crossing ``/``, a leading ``!`` negating, ``**/``
    matching zero directories. A path belongs to every area whose globs
    match it, so the result never depends on entry order; a path matching
    no area is ignored. Strictly more distinct areas than ``max_areas``
    makes exactly one note listing each area and its file count. Pure and deterministic: no LLM call, no I/O, no
    randomness.
    """
    if not globs:
        return [], "skipped: no area globs"
    areas: dict[str, list[str]] = {}
    for entry in globs:
        name, _, glob = entry.partition("=")
        if name.strip() and glob.strip():
            areas.setdefault(name, []).append(glob)
    if not areas:
        return [], "skipped: no area globs"
    counts: dict[str, int] = {}
    for file in files:
        for name, patterns in areas.items():
            if match_globs(file.path, patterns):
                counts[name] = counts.get(name, 0) + 1
    if len(counts) <= max_areas:
        return [], "pass"
    listed = ", ".join(f"{name} ({counts[name]} file(s))" for name in sorted(counts))
    note = MetadataNote(
        check="area_globs",
        title=f"PR touches {len(counts)} areas (max {max_areas}): {listed}",
        detail=(
            f"The diff's paths fall into {len(counts)} of the configured areas — "
            f"{listed} — more than the maximum of {max_areas}; consider splitting "
            f"the PR."
        ),
    )
    return [note], "fail"


def run_metadata_checks(
    pr: PRData,
    files: Sequence[FileDiff],
    commits: Sequence[CommitData] | None = None,
    *,
    branch_patterns: Sequence[str] = (),
    commit_reference: str = "",
    area_globs: Sequence[str] = (),
    max_areas_per_pr: int = 2,
    commit_skip_reason: str = "no commit source",
) -> tuple[list[MetadataNote], dict[str, str]]:
    """Run the three checks once and return their notes and statuses.

    The ``type=regex`` / ``name=glob`` strings are the config values as
    loaded (the config layer already refused a malformed entry or a regex
    that does not compile); they are parsed here. Returns every note and
    the ``{check: status}`` map, with the checks in a fixed order so both
    are deterministic.
    """
    patterns: dict[str, str] = {}
    for entry in branch_patterns:
        name, _, pattern = entry.partition("=")
        if name.strip() and pattern.strip():
            patterns[name] = pattern
    branch_notes, branch_status = branch_pattern_check(pr, patterns)
    commit_notes, commit_status = commit_reference_check(
        commits, commit_reference, skip_reason=commit_skip_reason,
    )
    area_notes, area_status = area_check(files, area_globs, max_areas_per_pr)
    return [*branch_notes, *commit_notes, *area_notes], {
        "branch_pattern": branch_status,
        "commit_reference": commit_status,
        "area_globs": area_status,
    }
