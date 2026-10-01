"""Deterministic, non-LLM PR-metadata checks (issue #70).

A companion to :mod:`prxref.heuristics` under the same doctrine: every
check here is pure — computed once from the PR metadata, the parsed diff
and an already-fetched commit list, with no model and no I/O in the loop —
and every finding it makes ends its body with " (deterministic check, no
model)", so :func:`prxref.heuristics.is_deterministic` exempts it from
severity consistency exactly like a heuristic finding. Where the
heuristics are always on, these checks are opt-in: they run only when
``metadata_rules = "on"``, and each is configured by its own key
(``branch_patterns``, ``commit_reference``, ``area_globs`` with
``max_areas_per_pr``).

Three checks, one per key, each answering with its findings plus a status
string — ``"pass"`` (configured, and the PR satisfies it), ``"fail"``
(configured, and it found violations) or ``"skipped: <reason>"`` (nothing
configured, or nothing to check it against). A skip is never a violation:
a PR without a resolvable type is not flagged for its branch name, and a
forge that cannot list commits does not fail the reference check. The
statuses are the run record's ``metadata_rules`` stamp. Violations are
summary-only findings (the orchestrator keeps them out of the inline
batch by threading the finding objects), anchored file-level (``line=0``)
on the diff's first path so location validation keeps them; with no files
in the diff they anchor on ``""`` and the empty-diff exit, which validates
locations against nothing, leaves them standing.

None of this ever touches the verdict or the exit code: branch and area
violations are ``warning``, a missing commit reference is ``outofscope``,
and only an ``error`` finding makes a verdict Request-Changes.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace

from .forges.base import CommitData, PRData
from .rules import match_globs
from .triage import FileDiff, Finding

# The mark heuristics.is_deterministic looks for, restated here rather
# than imported: heuristics keeps its private copy deliberately (it does
# not import systemic._LOCKFILE_BASENAMES either), and tests pin the two
# literals together through is_deterministic itself.
_BODY_SUFFIX = " (deterministic check, no model)"

# A conventional-commit type: the word before the optional "(scope)" and
# the ":" that opens a title like "fix(scope): ...". Case-insensitive,
# lowercased for the lookup; a title with no such prefix resolves no type.
_TITLE_TYPE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)(?:\([^)]*\))?:")


def _anchor(files: Sequence[FileDiff]) -> str:
    """The diff's alphabetically first path — a metadata finding's technical anchor.

    A metadata violation is about the PR as a whole, not a line, but a
    finding needs a diff path to survive ``apply_location_validation``.
    The first path in sorted order is deterministic and stable however the
    diff was chunked. An empty diff anchors on ``""``; the empty-diff exit
    runs no location validation, so the finding stands.
    """
    return min((f.path for f in files), default="")


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
) -> tuple[list[Finding], str]:
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
    fails makes a finding, a file-level ``warning`` with confidence 1.0.
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
    finding = Finding(
        file="", line=0, severity="warning", confidence=1.0,
        title=f"Branch '{branch}' does not match the '{name}' pattern",
        body=(
            f"The PR's type resolves to '{name}' (from {source}), which requires a "
            f"source branch fully matching `{name}={pattern.pattern}`, but the source "
            f"branch is `{branch}`.{_BODY_SUFFIX}"
        ),
    )
    return [finding], "fail"


def commit_reference_check(
    commits: Sequence[CommitData] | None,
    pattern: str | re.Pattern[str],
    *,
    skip_reason: str = "no commit source",
) -> tuple[list[Finding], str]:
    """Check every non-merge commit subject for the reference pattern.

    ``pattern`` is matched with ``re.search`` against each commit's
    subject (the first line of its message): the reference must appear
    somewhere in the subject, not span the whole line. Merge commits (more
    than one parent) are exempt — their subject is the merge line, not the
    author's. ``commits=None`` means no source could list them (a forge
    without ``get_commits``, or a listing that failed) and skips with
    ``skip_reason``; an empty list is a real answer (a squash-merged PR,
    or a range of merges only) and passes vacuously. One ``outofscope``
    finding per offending commit, file-level, confidence 1.0. Pure and
    deterministic: no LLM call, no I/O, no randomness.
    """
    if not pattern:
        return [], "skipped: no commit reference pattern"
    if commits is None:
        return [], f"skipped: {skip_reason}"
    regex = pattern if isinstance(pattern, re.Pattern) else re.compile(pattern)
    findings: list[Finding] = []
    for commit in commits:
        if commit.parent_count > 1:
            continue
        if regex.search(commit.subject):
            continue
        findings.append(Finding(
            file="", line=0, severity="outofscope", confidence=1.0,
            title=f"Commit {commit.sha[:10]} subject has no '{regex.pattern}' reference",
            body=(
                f"Every non-merge commit subject must match `{regex.pattern}`, but "
                f"commit `{commit.sha[:10]}` reads: \"{commit.subject}\".{_BODY_SUFFIX}"
            ),
        ))
    if findings:
        return findings, "fail"
    return [], "pass"


def area_check(
    files: Sequence[FileDiff],
    globs: Sequence[str],
    max_areas: int,
) -> tuple[list[Finding], str]:
    """Check the diff's paths against named area globs and the area cap.

    ``globs`` are ``name=glob`` entries; entries sharing a name form one
    area (``backend=src/**`` and ``backend=lib/**`` are one area of two
    globs), matched with :func:`prxref.rules.match_globs` — case-sensitive
    ``fnmatch``, ``*`` crossing ``/``, a leading ``!`` negating, ``**/``
    matching zero directories. A path belongs to every area whose globs
    match it, so the result never depends on entry order; a path matching
    no area is ignored. Strictly more distinct areas than ``max_areas``
    makes exactly one file-level ``warning`` listing each area and its
    file count. Pure and deterministic: no LLM call, no I/O, no
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
    finding = Finding(
        file="", line=0, severity="warning", confidence=1.0,
        title=f"PR touches {len(counts)} areas (max {max_areas}): {listed}",
        body=(
            f"The diff's paths fall into {len(counts)} of the configured areas — "
            f"{listed} — more than the maximum of {max_areas}; consider splitting "
            f"the PR.{_BODY_SUFFIX}"
        ),
    )
    return [finding], "fail"


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
) -> tuple[list[Finding], dict[str, str]]:
    """Run the three checks once and anchor their findings on the diff.

    The ``type=regex`` / ``name=glob`` strings are the config values as
    loaded (the config layer already refused a malformed entry or a regex
    that does not compile); they are parsed here, and the findings the
    checks return with their technical ``file=""`` are re-anchored on
    :func:`_anchor` before they are handed to the pipeline. Returns every
    finding and the ``{check: status}`` stamp for the run record, with the
    checks in a fixed order so both are deterministic.
    """
    patterns: dict[str, str] = {}
    for entry in branch_patterns:
        name, _, pattern = entry.partition("=")
        if name.strip() and pattern.strip():
            patterns[name] = pattern
    branch_findings, branch_status = branch_pattern_check(pr, patterns)
    commit_findings, commit_status = commit_reference_check(
        commits, commit_reference, skip_reason=commit_skip_reason,
    )
    area_findings, area_status = area_check(files, area_globs, max_areas_per_pr)
    anchor = _anchor(files)
    findings = [
        f if f.file else replace(f, file=anchor)
        for f in [*branch_findings, *commit_findings, *area_findings]
    ]
    return findings, {
        "branch_pattern": branch_status,
        "commit_reference": commit_status,
        "area_globs": area_status,
    }
