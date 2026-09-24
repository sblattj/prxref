"""Deterministic quality passes over worker findings.

Sixteen passes run before posting, in the order ``orchestrate_review``
applies them; pass 1 runs only when the team review rules declare a
severity map, pass 12 only when ``PRXREF_GROUP_FINDINGS`` turns
finding grouping on, and pass 13 only when a team rules file is loaded
and ``PRXREF_MAX_FINDINGS_PER_RULE`` is above 0. A seventeenth
deterministic check, the release-shaped-PR
heuristic, is not a pass at all: ``heuristics.release_shape_findings``
ADDS a finding before pass 1 and it then flows through every pass below
exactly like a model finding. Every ``drop_reason`` prefix these passes
emit is tabulated for operators in ``docs/quality.md``.

1. ``apply_severity_map``: when the team review rules declare a severity
   map, rewrite a team severity word (``blocker``) to the prxref tier the
   map gives it (``error``), compared after whitespace collapsing and
   ``casefold()``. It runs first because every later pass reads the
   severity. A dropped finding, an unmapped word and one of prxref's own
   severities pass through unchanged; it drops nothing, and without a map
   it is not called.
2. ``apply_spec_grounding``: on a run that injected no spec constraint
   (``specs.constraint_count`` of the digest is 0 — no sources, every
   source failed, or nothing kept), relabel every ``spec`` finding as
   ``warning``: the prompts showed the no-specs text, so the label has
   nothing to be grounded in. It runs right after the team severity
   map, so ``apply_severity_consistency`` never raises a same-title
   sibling to ``spec`` on the strength of an ungrounded label. It drops
   nothing.
3. ``apply_example_echo_check``: drop a finding whose normalized title
   equals the title of an example finding in the worker or sweep prompt
   template in force for the run, packaged or overridden
   (``echoes the prompt's example: "<title>"``): the model copied the
   output example rather than reporting a defect. It is the first pass that
   drops, so an echo never reaches a thread, consistency or grouping
   comparison, a cap, or sweep dedup, and its audit copy keeps the model's
   own anchor. The match is exact, so a title that only resembles an
   example stays.
4. ``apply_location_validation``: drop findings whose ``file`` names no
   path of the parsed diff — an empty, non-path, or invented location is
   retained with ``drop_reason`` for the audit instead of rendering a
   bullet anchored to nothing.
5. ``apply_manifest_claim_check``: for findings on a manifest or
   npm-family lockfile (``package.json``, ``bun.lock``, ...), drop a
   claim whose named dependency is not the key on the anchored line
   (``anchor mismatch:``) or sits under a different dependency section
   than the claim asserts (``section mismatch:``); when the anchor's
   own hunk holds no section header, the served full-file lines decide
   the enclosing section. It runs BEFORE ``apply_line_align`` so it
   reads the model's raw anchor.
6. ``apply_line_align``: a line explicitly cited in the finding's own
   title or body (``line 553``, ``at line 553``, an own-file
   ``path:line``) outranks a drifted ``line`` field whenever the cited
   line lands on an added line — or a context line within tolerance of
   one — of that file's diff and its hunk corroborates against the claim;
   a non-corroborating citation is ignored. Then snap each finding's
   cited line to the nearest actual ``+`` line in that file's diff
   (within tolerance, else line=0), then corroborate exact ``+``-line
   members against the file's hunks by
   content, so an anchor that is a valid added line of the WRONG hunk
   (issue #19's drift shape) is
   re-resolved or demoted to file-level
   instead of posting at a wrong position. Corroboration is line-level:
   an anchor survives only when it ties the file's best evidence match
   or sits within tolerance of it, and a blank or pure-punctuation
   anchor never survives while any token-bearing added line exists.
7. ``apply_thread_dedup``: drop findings that duplicate an already-open
   or existing thread on the PR (path + line-window + shared distinctive
   tokens), with ``drop_reason`` ``duplicate of existing thread``.
8. ``apply_settled_thread_suppression``: drop findings that re-litigate a
   subject an existing thread already argued out — same path plus shared
   distinctive tokens, with NO line test, because line alignment has already
   demoted a file-level finding to line 0 by this point
   (``settled in thread: <author>``).
9. ``apply_severity_consistency``: findings sharing one normalized title —
   within a file or across sibling files — are all raised to the group's
   maximum severity, so per-chunk workers cannot disagree about how
   serious the same pattern is. Findings phrased differently but bound
   by a shared rare code token, with a shared problem class or file,
   join the same group (issue #30).
10. ``apply_removal_claim_check``: drop findings whose removal verb governs
    a path — ``removed src/app.py``, ``src/app.py was removed`` — when every
    path the claim names is still present in the diff's post-image — the false positive a ``copy from``/``copy to``
    header produces when a worker reads a copy as a move (issue #03).
    Only a claim that NAMES a diff path is judged, so a finding about a
    removed guard or constant is untouched.
11. ``apply_hedge_gate``: drop findings whose title or body conditions the
    defect on a precondition the worker never established from the diff
    ("If X still leases a client", "unless the backfill already ran"),
    with ``drop_reason`` ``hedged: "<matched span>"``. A body's
    ``Spec: "..."`` quote is not read for the text it copies verbatim from
    the spec digest the workers were shown.
12. ``apply_rule_grouping``: fold chunk findings in one file that name the
    same ``rule`` (casefolded), or that name none and share a normalized
    title, into one finding at the group's smallest positive line, with
    the group's highest severity and highest confidence and an
    ``Also at: `<file>:<line>`, ...`` paragraph listing the other lines;
    the other members are dropped as ``grouped into <file>:<line>``. Sweep
    findings are never grouped. It runs before the gate, so the caps count
    groups rather than lines.
13. ``apply_rule_cap``: keep at most ``PRXREF_MAX_FINDINGS_PER_RULE``
    chunk findings per ``rule`` (casefolded), or per normalized title for
    findings that name none, across every file of the review. The kept
    findings are the most severe, then the most confident; the rest are
    folded onto the first of them, whose ``locations`` and ``Also at:``
    paragraph list theirs (at most five named, then ``(+N more)``), and
    are dropped as ``rule cap exceeded (max N): listed at <file>:<line>``.
    Sweep findings are never capped. It runs only when the caller turns
    it on: a team rules file is loaded and the cap is above 0. It runs
    after grouping, so a group counts once, and before the gate, so the
    severity caps count what it kept.
14. ``apply_quality_gate``: drop findings below the confidence floor
    (``confidence 0.40 below floor 0.60``), cap errors per review
    (``error cap exceeded (max N)``), optionally cap warnings and
    outofscope findings the same way (``warning cap exceeded (max N)``,
    ``outofscope cap exceeded (max N)``), and enforce the
    {error, warning, spec, outofscope} severity vocabulary
    (``invalid severity: '<value>'``). It RETURNS its findings sorted by
    ``finding_sort_key``, so the caller re-derives the chunk/sweep
    boundary from finding identity rather than carrying an index across it.
15. ``apply_sweep_dedup``: drop a sweep finding that restates a chunk
    finding which SURVIVED the gate, on file + normalized title
    (``duplicate of chunk finding``). It runs after the gate so a
    sub-floor chunk finding cannot suppress its higher-confidence sweep
    duplicate and then die at the gate itself. With a similarity
    threshold set, a second tier then drops reworded restatements: two
    active findings in the same file on the same line (line 0 is never
    compared) whose titles pass ``titles_similar``
    (``duplicate of chunk finding (reworded, similarity 0.57)``, or
    ``duplicate of sweep finding ...`` between two sweep findings).
    Across the boundary the chunk copy always survives and a sweep copy
    is dropped only when it is no more severe; on one side the more
    severe, then higher-confidence, copy is kept. Without a threshold
    the tier does not run.
16. ``apply_containment_note``: a finding that asserts a throw, panic,
    crash, or unhandled rejection and never names where it is caught or
    where it propagates to has its body suffixed with
    ``" [containment boundary not stated]"`` — a purely textual
    decoration, run on active and dropped findings alike, that never
    changes ``drop_reason`` or severity.

Every dropped finding retains its identity with ``drop_reason`` populated,
so review runstores and logs can explain every filter decision. Use
``active(findings)`` to obtain the subset that should actually post.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import PurePosixPath

from .forges.base import Thread
from .triage import DiffLine, FileDiff, Finding, Hunk

SEVERITIES: frozenset[str] = frozenset({"error", "warning", "spec", "outofscope"})

DEFAULT_CONFIDENCE_FLOOR: float = 0.6
DEFAULT_MAX_ERRORS: int = 10

# Positional snap radius for cited lines. Inline comment cards render only a
# handful of surrounding lines, so a nudge of up to 5 keeps the cited code
# visible in the posted comment; past that the comment separates from its
# evidence. 5 also sits below the smallest drift the issue #19 audit measured
# on real reviews (10 lines), so no observed-failure distance survives.
DEFAULT_LINE_TOLERANCE: int = 5

# A period that is not followed by whitespace is member access, a filename, or
# a version — not a sentence break — so the hedge rules may span it.
_NO_SENTENCE_BREAK = r"(?:[^.;]|\.(?!\s))"

# A comma ends the ``if`` clause, so the hedge word has to appear before it to
# be hedging the CONDITION rather than asserting a defect in the independent
# clause that follows ("If save() raises, the transaction remains open").
_NO_CLAUSE_BREAK = r"(?:[^.;,]|\.(?!\s))"

HEDGE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "if-still",
        re.compile(
            rf"\bif\b{_NO_CLAUSE_BREAK}{{0,80}}?\b(?:still|already|remains?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "modal-still",
        re.compile(r"\b(?:may|might|could|likely|probably)\s+still\b", re.IGNORECASE),
    ),
    (
        "assuming",
        re.compile(
            r"(?:^|[.;]\s+|,\s*)(?:assuming|presumably|apparently|seemingly)\b(?!-)",
            re.IGNORECASE,
        ),
    ),
    (
        "unless-already",
        re.compile(
            rf"\bunless\b{_NO_SENTENCE_BREAK}{{0,60}}?\balready\b", re.IGNORECASE
        ),
    ),
    (
        "not-verified",
        re.compile(
            r"\b(?:I (?:can(?:no|')?t|cannot) (?:verify|tell|confirm|determine)"
            r"|unable to (?:verify|tell|confirm|determine)"
            r"|not visible in the diff"
            r"|not shown in the diff"
            r"|cannot be confirmed from the diff)",
            re.IGNORECASE,
        ),
    ),
    (
        "only-caller",
        re.compile(
            r"\bif this is the only (?:caller|call site|usage|consumer)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "membership",
        re.compile(
            r"\bif (?:they|it|this|those|these) (?:are|is) (?:members?|part) of\b",
            re.IGNORECASE,
        ),
    ),
)

_HEDGE_SPAN_MAX: int = 80

# Where a finding quotes its constraint (``Spec: "..."``). Normative text is
# conditional by nature ("If a session already exists, the server MUST reuse
# it"), so the quoted text the digest really holds is the spec's precondition,
# not the model's hedge, and is removed before the hedge rules read the body.
_SPEC_QUOTE_OPEN_RE = re.compile(r"Spec:\s*[\"“‘']?")
_SPEC_QUOTE_CLOSERS: frozenset[str] = frozenset("\"”’'")


def active(findings: Sequence[Finding]) -> list[Finding]:
    """Return only the findings that survived every quality pass."""
    return [f for f in findings if f.drop_reason is None]


def apply_location_validation(
    findings: Sequence[Finding],
    diff_paths: Sequence[str],
) -> list[Finding]:
    """Drop findings whose ``file`` does not name a path of the diff.

    A worker that answers ``file: "package."`` — or invents a path the
    diff never touches — used to survive every pass and render as a
    summary bullet like ``- 🟧 `package.:—```. A finding is reviewable
    only at a location the diff actually contains, so the accepted set is
    exactly the diff's own paths: an empty ``file``, a non-path shape,
    and a plausible-but-absent path all fail the membership check and are
    retained with ``drop_reason="malformed location: '<file>'"`` for the
    dropped-findings audit. A file that IS in the diff is never dropped,
    and findings already carrying a ``drop_reason`` keep it.
    """
    known = set(diff_paths)
    result: list[Finding] = []
    for f in findings:
        if f.drop_reason is not None or f.file in known:
            result.append(f)
            continue
        result.append(replace(f, drop_reason=f"malformed location: {f.file!r}"))
    return result


# Manifests and npm-family lockfiles whose diff lines are dependency
# entries under a named section — the files the claim check can judge.
# Local by design: the lockfile sets in heuristics/systemic are broader
# (other ecosystems) or carry a different signal. bun.lockb is binary in
# a diff, so its findings never name a key and pass through leniently.
MANIFEST_BASENAMES: frozenset[str] = frozenset({
    "package.json",
    "bun.lock",
    "bun.lockb",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
})

DEPENDENCY_SECTIONS: tuple[str, ...] = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)

# Keys that name a manifest SECTION or the package itself rather than a
# dependency, so they can never be the "claimed package" of a finding.
_MANIFEST_NON_PACKAGE_KEYS: frozenset[str] = frozenset(
    [s.lower() for s in DEPENDENCY_SECTIONS]
    + ["packages", "scripts", "engines", "name", "version"]
)

# Evidence tokens a manifest section header contributes (including the
# camelCase split of the compound forms), which must not decide a
# manifest/lockfile realign — see ``_realign_member``.
_MANIFEST_SECTION_TOKENS: frozenset[str] = frozenset(
    {"dependencies", "devdependencies", "peerdependencies",
     "optionaldependencies", "packages", "peer", "optional"}
)

_NPM_NAME_RE = re.compile(
    r"(?:@[a-z0-9\-~][a-z0-9._~\-]*/)?[a-z0-9\-~][a-z0-9._~\-]*"
)

_JSON_KEY_RE = re.compile(r'"([^"\n]+)"\s*:')

# A section opener as a line-scan, not a parse: bun.lock is JSONC
# (trailing commas, optionally unquoted keys), so the quotes are optional
# and the key must not be the tail of a longer identifier.
_SECTION_OPEN_RE = re.compile(
    r'(?<!\w)"?(dependencies|devDependencies|peerDependencies'
    r'|optionalDependencies|packages)"?'
    r"\s*:\s*\{"
)

_CLAIMED_SECTION_PATTERNS: tuple[tuple[str, str], ...] = (
    ("devdependenc", "devDependencies"),
    ("dev dependenc", "devDependencies"),
    ("peerdependenc", "peerDependencies"),
    ("peer dependenc", "peerDependencies"),
    ("optionaldependenc", "optionalDependencies"),
    ("optional dependenc", "optionalDependencies"),
    ("runtime dependenc", "dependencies"),
    ("production dependenc", "dependencies"),
    ("under `dependencies`", "dependencies"),
    ("in dependencies", "dependencies"),
    ("to dependencies", "dependencies"),
)


def _manifest_keys(hunks: Sequence[Hunk]) -> set[str]:
    """Every JSON key present on a post-image line of the file's hunks."""
    keys: set[str] = set()
    for h in hunks:
        for ln in h.lines:
            if ln.kind == "-":
                continue
            keys.update(_JSON_KEY_RE.findall(ln.text))
    return keys


def _line_json_key(ln: DiffLine) -> str | None:
    """The dependency key declared on a manifest line, if any.

    A section header or a bare brace declares no dependency, so both
    answer None and leave the anchor-key check with nothing to compare.
    """
    m = _JSON_KEY_RE.search(ln.text)
    if m is None or m.group(1).lower() in _MANIFEST_NON_PACKAGE_KEYS:
        return None
    return m.group(1)


def _claimed_package(text: str, keys: set[str]) -> str | None:
    """The first npm package name in ``text`` that is a key of the file."""
    for m in _NPM_NAME_RE.finditer(_CITATION_RE.sub(" ", text.lower())):
        name = m.group(0)
        if len(name) < 2 or name in _MANIFEST_NON_PACKAGE_KEYS:
            continue
        for key in keys:
            if key.lower() == name:
                return key
    return None


def _claimed_section(text: str) -> str | None:
    """The dependency section a claim asserts, by earliest phrasing."""
    lowered = text.lower()
    best: tuple[int, str] | None = None
    for needle, section in _CLAIMED_SECTION_PATTERNS:
        idx = lowered.find(needle)
        if idx >= 0 and (best is None or idx < best[0]):
            best = (idx, section)
    return best[1] if best else None


def _enclosing_section(
    hunks: Sequence[Hunk],
    line: int,
    full_lines: Sequence[str] | None = None,
) -> str | None:
    """The dependency section header above ``line`` in the post-image.

    The anchor's own hunk lines are scanned first; when they hold no
    header above the anchor — the common lockfile shape, where the
    ``devDependencies`` block opens far above the hunk — the served
    full-file lines decide it instead. ``None`` is returned, and the
    caller stays lenient, only when NO source names a section.
    """
    for h in hunks:
        body = [ln for ln in h.lines if ln.kind != "-"]
        for i, ln in enumerate(body):
            if ln.new_line != line:
                continue
            for prev in reversed(body[: i + 1]):
                m = _SECTION_OPEN_RE.search(prev.text)
                if m is not None:
                    return m.group(1)
            break
    if full_lines is not None and 0 < line <= len(full_lines):
        for text in reversed(full_lines[:line]):
            m = _SECTION_OPEN_RE.search(text)
            if m is not None:
                return m.group(1)
    return None


def _served_lines(
    path: str,
    cache: dict[str, list[str] | None],
    read: Callable[[str], str | None] | None,
) -> list[str] | None:
    """Post-image lines of ``path`` from the reader, cached; None without one."""
    if read is None:
        return None
    if path not in cache:
        content = read(path)
        cache[path] = content.splitlines() if content else None
    return cache[path]


def apply_manifest_claim_check(
    findings: Sequence[Finding],
    files: Sequence[FileDiff],
    read: Callable[[str], str | None] | None = None,
) -> list[Finding]:
    """Drop manifest/lockfile findings that misname key or section.

    A worker reading a manifest or lockfile diff can name a real
    dependency and then anchor the comment on an unrelated neighbouring
    entry, or read an entry as a runtime dependency when the enclosing
    block is ``devDependencies``. Both are checkable against the diff
    itself: the claim names a package, the anchored line declares a key,
    and the nearest section header above that line names the block it
    lives in — from the anchor's hunk, or, when that hunk holds no
    header, from the served full file (``read``, the same reader the
    chunk context uses).

    A finding is dropped with ``drop_reason="anchor mismatch: claims
    <claimed> but line <n> is <anchor>"`` when the anchored line declares
    a different dependency, and with ``drop_reason="section mismatch:
    claims <claimed> but <key> is under <actual>"`` when the anchor is
    right but the asserted section is not. Findings on other files, on a
    manifest whose claim names no key of the diff, and findings that
    already carry a ``drop_reason`` pass through untouched. Order is
    preserved.

    Run this BEFORE :func:`apply_line_align`: it must read the model's
    raw anchor, because realignment can move a correctly anchored claim
    onto the very line this pass exists to catch.
    """
    hunks_by_file = {
        f.path: f.hunks
        for f in files
        if PurePosixPath(f.path).name in MANIFEST_BASENAMES
    }
    full_lines: dict[str, list[str] | None] = {}
    result: list[Finding] = []
    for f in findings:
        hunks = hunks_by_file.get(f.file)
        if f.drop_reason is not None or not hunks:
            result.append(f)
            continue
        claim = f"{f.title}\n{f.body}"
        keys = _manifest_keys(hunks)
        claimed = _claimed_package(f.title, keys) or _claimed_package(f.body, keys)
        if claimed is None:
            result.append(f)
            continue
        anchor_ln = next(
            (ln for h in hunks for ln in h.lines
             if ln.kind != "-" and ln.new_line == f.line),
            None,
        )
        anchor_key = _line_json_key(anchor_ln) if anchor_ln is not None else None
        if anchor_key is not None and anchor_key != claimed:
            result.append(replace(
                f,
                drop_reason=(
                    f"anchor mismatch: claims {claimed} but "
                    f"line {f.line} is {anchor_key}"
                ),
            ))
            continue
        claimed_section = _claimed_section(claim)
        actual_section = _enclosing_section(
            hunks, f.line, _served_lines(f.file, full_lines, read)
        )
        if (
            claimed_section is not None
            and actual_section is not None
            and claimed_section != actual_section
        ):
            result.append(replace(
                f,
                drop_reason=(
                    f"section mismatch: claims {claimed_section} but "
                    f"{claimed} is under {actual_section}"
                ),
            ))
            continue
        result.append(f)
    return result


def snap_line(
    line: int, added: set[int], tolerance: int = DEFAULT_LINE_TOLERANCE
) -> int:
    """Snap a cited line to the nearest ``+`` line within tolerance, else 0.

    A returned 0 denotes a file-level anchor (no ``+`` line close enough,
    or the citation was already file-level: ``line <= 0`` never snatches a
    file-level finding onto an inline line).
    """
    if line <= 0:
        return 0
    if line in added:
        return line
    if not added:
        return 0
    nearest = min(added, key=lambda ln: (abs(ln - line), ln))
    if abs(nearest - line) <= tolerance:
        return nearest
    return 0


def _hunk_containing(hunks: Sequence[Hunk], line: int) -> Hunk | None:
    """The hunk whose new-file span holds ``line``, or None."""
    for h in hunks:
        if h.new_start <= line < h.new_start + h.new_count:
            return h
    return None


def _line_at(hunk: Hunk, line: int) -> DiffLine | None:
    """The hunk body line rendered at new-file ``line``, or None."""
    for ln in hunk.lines:
        if ln.kind != "-" and ln.new_line == line:
            return ln
    return None


def _line_tokens(ln: DiffLine) -> set[str]:
    return _tokens(ln.text, split_compounds=True)


_PUNCT_RE = re.compile(r"[A-Za-z0-9]")


def _is_blankish(ln: DiffLine) -> bool:
    """True for blank or pure-punctuation lines (no alphanumeric at all).

    Deliberately narrower than "no content tokens": a real code line whose
    identifiers are below the 4-char token floor (``$ttl = $this->ttl;``)
    is not a blank anchor and stays eligible for the tolerance reprieve.
    """
    return _PUNCT_RE.search(ln.text) is None


def _line_is_blankish(hunks: Sequence[Hunk], line: int) -> bool:
    for h in hunks:
        ln = _line_at(h, line)
        if ln is not None:
            return _is_blankish(ln)
    return False


def _file_has_token_bearing_add(hunks: Sequence[Hunk]) -> bool:
    return any(
        ln.kind == "+" and ln.new_line is not None and _line_tokens(ln)
        for h in hunks
        for ln in h.lines
    )


def _realign_member(
    finding: Finding,
    hunks: Sequence[Hunk],
    tolerance: int = DEFAULT_LINE_TOLERANCE,
) -> int:
    """Re-resolve an exact ``+``-line member anchor by content, or keep it.

    Membership alone cannot catch drift: a model that adds the wrong
    hunk's ``@@`` start to an in-hunk offset emits a number that IS a
    valid added line, of the wrong hunk or of the wrong construct. The
    checkable property is line-level content correspondence. A blank or
    pure-punctuation anchor never survives while any token-bearing added
    line exists in the file. Any other anchor survives only when it ties
    the file's best evidence match — the non-removed line sharing the
    most claim evidence — or sits within ``tolerance`` of that line, so
    the cited code is still visible in the posted comment card.
    Otherwise the anchor is re-resolved to the best-matching added line,
    ranked by breadth of shared evidence tokens, then by the most
    specific (longest) shared token, then by proximity to the citation;
    the anchor's own hunk competes like any other. When no line matches
    the evidence anywhere, the finding drops to file-level 0.

    Evidence tokens come from title + body with backticked-style
    ``path:line`` citation strings stripped (so a ``file.js:31`` mention
    cannot collide with path words) and compound identifiers split into
    their snake/camel parts (so ``wp_ajax_nopriv_avatar_upload`` can
    answer a claim about "nopriv avatar upload").

    On a manifest or lockfile the manifest section words are dropped
    from the evidence, because every dependency claim mentions one and
    the long ``dependencies`` token would otherwise outrank the package
    name and resolve the anchor onto a section header instead of the
    entry.
    """
    ftoks = _evidence_tokens(f"{finding.title} {finding.body}")
    if PurePosixPath(finding.file).name in MANIFEST_BASENAMES:
        ftoks -= _MANIFEST_SECTION_TOKENS
    if not ftoks:
        return finding.line
    anchor = _hunk_containing(hunks, finding.line)
    anchor_ln = _line_at(anchor, finding.line) if anchor else None
    if (
        anchor_ln is not None
        and _is_blankish(anchor_ln)
        and not _file_has_token_bearing_add(hunks)
    ):
        return finding.line
    best = _best_candidate(hunks, ftoks, finding.line)
    if anchor_ln is not None and not _is_blankish(anchor_ln) and best is not None:
        anchor_shared = ftoks & _line_tokens(anchor_ln)
        anchor_key = (
            (len(anchor_shared), max(map(len, anchor_shared)))
            if anchor_shared
            else (0, 0)
        )
        if anchor_key >= best[0] or abs(
            (anchor_ln.new_line or 0) - (best[1].new_line or 0)
        ) <= tolerance:
            return finding.line
    if best is None:
        return 0
    target = best[1]
    if target.kind == "+":
        return target.new_line or 0
    adds = [
        ln for h in hunks for ln in h.lines
        if ln.kind == "+" and ln.new_line is not None
    ]
    if not adds:
        return 0
    return min(
        adds, key=lambda a: abs((a.new_line or 0) - (target.new_line or 0))
    ).new_line or 0


def _best_candidate(
    hunks: Sequence[Hunk], ftoks: set[str], citation: int
) -> tuple[tuple[int, int], DiffLine] | None:
    """The non-removed line sharing the most claim evidence.

    Ranked by shared-token breadth, then longest shared token, then
    proximity to the citation line. Context lines can win; callers move
    a context winner to its nearest added line before anchoring.
    """
    candidates: list[tuple[tuple[int, int], int, DiffLine]] = []
    for h in hunks:
        for ln in h.lines:
            if ln.kind == "-" or ln.new_line is None:
                continue
            shared = ftoks & _line_tokens(ln)
            if not shared:
                continue
            key = (len(shared), max(map(len, shared)))
            candidates.append((key, -abs(ln.new_line - citation), ln))
    if not candidates:
        return None
    top = max(candidates, key=lambda c: (c[0], c[1]))
    return top[0], top[2]


_BODY_LINE_CITE_RE = re.compile(
    r"(?P<path>[\w./\\-]+\.(?:php|phtml|inc|js|cjs|mjs|jsx|ts|tsx|py|pyi|rb|go"
    r"|rs|java|kt|kts|swift|c|h|cpp|hpp|cc|cs|fs|css|scss|less|sass|html?|htm"
    r"|vue|svelte|json|ya?ml|toml|ini|cfg|conf|md|markdown|txt|sh|bash|zsh"
    r"|fish|sql|xml|env|lock|csv|tsv|log)|\S+/\S+\.\w{1,8}):(?P<num>\d+)"
    r"|\bline\s+(?P<prose>\d+)\b",
    re.IGNORECASE,
)


def _is_own_file_citation(path: str, file: str) -> bool:
    """True when a ``path:line`` citation names the finding's own file.

    Exact, or a suffix at a directory separator in either direction, so
    ``sync.ts:553`` answers a finding filed against ``src/sync.ts`` while
    a sibling file's ``other.ts:12`` never does. A bare path with no
    ``:line`` never reaches this check.
    """
    cited = path.replace("\\", "/").lower()
    own = file.replace("\\", "/").lower()
    if cited.startswith("./"):
        cited = cited[2:]
    return cited == own or own.endswith("/" + cited) or cited.endswith("/" + own)


def _body_cited_lines(finding: Finding) -> list[int]:
    """Line numbers the finding's own title+body cite, in document order.

    Grammar: an own-file ``path:line`` citation (``sync.ts:553``,
    backticked or bare, any known extension or slash path) and prose
    ``line N`` (``at line 553``, ``Line 553.``). A bare path mention
    (``sync.ts``) cites no line, and another file's ``path:line`` is
    ignored.
    """
    text = f"{finding.title} {finding.body}"
    hits: list[tuple[int, int]] = []
    for m in _BODY_LINE_CITE_RE.finditer(text):
        if m.group("path") is not None:
            if _is_own_file_citation(m.group("path"), finding.file):
                hits.append((m.start(), int(m.group("num"))))
        else:
            hits.append((m.start(), int(m.group("prose"))))
    hits.sort()
    return [num for _, num in hits]


def _hunk_tokens(hunk: Hunk) -> set[str]:
    """Union of content tokens over the hunk's non-removed lines."""
    tokens: set[str] = set()
    for ln in hunk.lines:
        if ln.kind != "-" and ln.new_line is not None:
            tokens |= _line_tokens(ln)
    return tokens


def _resolve_body_cited_anchor(
    finding: Finding,
    hunks: Sequence[Hunk],
    added: set[int],
    tolerance: int = DEFAULT_LINE_TOLERANCE,
) -> int | None:
    """The anchor a corroborated in-body line citation demands, or None.

    Issue #28: the model's ``line`` field drifts while its own body still
    names the right line. The first body citation — ``line N`` or an
    own-file ``path:line`` — whose cited line sits on an added line (or a
    context line within ``tolerance`` of one) of this file AND whose
    containing hunk shares a non-generic evidence token with the claim
    wins; its snapped anchor outranks the ``line`` field. A citation that
    fails either check is skipped (later citations still try), and None
    sends the caller down the shipped resolution path unchanged. A
    citation never lands on a blank or pure-punctuation anchor while the
    file carries token-bearing added lines.
    """
    evidence = _evidence_tokens(f"{finding.title} {finding.body}")
    if not evidence:
        return None
    blank_guard = _file_has_token_bearing_add(hunks)
    for cited in _body_cited_lines(finding):
        anchor = snap_line(cited, added, tolerance=tolerance)
        if anchor <= 0:
            continue
        hunk = _hunk_containing(hunks, cited)
        if hunk is None or not (_hunk_tokens(hunk) & evidence):
            continue
        if blank_guard and _line_is_blankish(hunks, anchor):
            continue
        return anchor
    return None


def apply_line_align(
    findings: Sequence[Finding],
    added_lines_by_file: dict[str, set[int]] | None = None,
    tolerance: int = DEFAULT_LINE_TOLERANCE,
    files: Sequence[FileDiff] | None = None,
) -> list[Finding]:
    """Snap each finding's line to a defensible anchor in that file's diff.

    Body-citation precedence: when the parsed hunks are supplied, a line
    cited in the finding's own title or body for this file (``line 553``,
    ``at line 553``, ``sync.ts:553``) outranks the ``line`` field
    whenever the cited line lands on an added line — or a context line
    within ``tolerance`` of one — of this file's diff and the hunk
    containing the cited line shares a non-generic evidence token with
    the claim (:func:`_resolve_body_cited_anchor`). The first
    corroborating citation wins; a citation outside the diff, with no
    corroborating hunk, or landing on a blank anchor is ignored and the
    shipped rules below apply unchanged.

    Positional pass: within ``tolerance`` of the nearest ``+`` line the
    citation snaps there; beyond it the citation is not trusted and drops
    to file-level (0) — the summary still carries the finding, at an
    honest location. Content pass: an exact ``+``-line member is
    corroborated against the file's hunks when ``files`` is supplied
    (see :func:`_realign_member`) — that is the only way a wrong-hunk or
    wrong-construct citation becomes visible, and it is corrected or
    demoted instead of posted at a wrong position. A citation that snaps
    onto a blank or pure-punctuation added line is re-resolved the same
    way whenever the file also carries token-bearing added lines, so a
    blank line never outranks the line the evidence lives on.

    When ``added_lines_by_file`` is omitted or has no entry for a file,
    the finding's line is dropped to 0 (file-level). A file-level
    citation stays file-level.
    """
    by_file = added_lines_by_file or {}
    hunks_by_file = {f.path: f.hunks for f in files} if files else {}
    result: list[Finding] = []
    for f in findings:
        added = by_file.get(f.file, set())
        hunks = hunks_by_file.get(f.file)
        new_line = (
            _resolve_body_cited_anchor(f, hunks, added, tolerance=tolerance)
            if hunks
            else None
        )
        if new_line is None:
            if hunks and f.line > 0 and f.line in added:
                new_line = _realign_member(f, hunks, tolerance=tolerance)
            else:
                new_line = snap_line(f.line, added, tolerance=tolerance)
                if (
                    hunks
                    and new_line > 0
                    and _line_is_blankish(hunks, new_line)
                    and _file_has_token_bearing_add(hunks)
                ):
                    new_line = _realign_member(f, hunks, tolerance=tolerance)
        if new_line != f.line:
            result.append(replace(f, line=new_line))
        else:
            result.append(f)
    return result


_STOPWORDS: frozenset[str] = frozenset({
    "this", "that", "there", "here", "with", "which", "should", "would",
    "could", "when", "then", "have", "will", "into", "from", "what", "your",
    "note", "also", "some", "more", "than", "been", "does", "they", "them",
    "because", "while", "might", "being", "within",
    "maybe", "consider", "perhaps", "probably", "actually", "simply",
    "just", "definitely",
    "code", "line", "file", "case", "type", "need", "used", "using",
    "async", "await", "function", "method", "return", "throw", "catch",
    "throws", "import", "export", "module", "const", "static",
    "public", "private", "protected", "abstract",
    "extends", "implements", "override",
    "string", "number", "boolean", "array", "object", "void",
    "null", "undefined", "promise",
})

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{4,}")
_CAMEL_PART_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")

_CITATION_RE = re.compile(
    r"\b[\w./\\-]+\.(?:php|phtml|inc|js|cjs|mjs|jsx|ts|tsx|py|pyi|rb|go|rs"
    r"|java|kt|kts|swift|c|h|cpp|hpp|cc|cs|fs|css|scss|less|sass|html?|htm"
    r"|vue|svelte|json|ya?ml|toml|ini|cfg|conf|md|markdown|txt|sh|bash|zsh"
    r"|fish|sql|xml|env|lock|csv|tsv|log)(?::\d+)?"
    r"|\S+/\S+\.\w{1,8}",
    re.IGNORECASE,
)


def _tokens(text: str, *, split_compounds: bool = False) -> set[str]:
    """Lowercased content tokens, stopwords dropped.

    With ``split_compounds`` each token is accompanied by its snake_case
    and camelCase parts (each still length- and stopword-filtered), so
    ``wp_ajax_nopriv_avatar_upload`` also yields {ajax, nopriv, avatar,
    upload} and claims can match the constructs they name.
    """
    tokens: set[str] = set()
    for raw in _TOKEN_RE.findall(text):
        lowered = raw.lower()
        if lowered in _STOPWORDS:
            continue
        tokens.add(lowered)
        if not split_compounds:
            continue
        for part in raw.split("_"):
            for piece in _CAMEL_PART_RE.findall(part):
                piece = piece.lower()
                if len(piece) >= 4 and piece not in _STOPWORDS:
                    tokens.add(piece)
    return tokens


def _evidence_tokens(text: str) -> set[str]:
    """Content tokens for claim-to-code correspondence.

    Path-like citation strings (``functions.php:1520``, ``src/a/b.py``,
    ``assets/exercise-library-el.js:31``) are stripped first so a
    backticked file reference in the claim cannot collide with path or
    module words inside unrelated hunks.
    """
    return _tokens(_CITATION_RE.sub(" ", text), split_compounds=True)


def is_duplicate_of_existing(
    finding: Finding,
    threads: Sequence[Thread],
    line_window: int = 30,
    min_shared_tokens: int = 2,
    min_shared_for_distant: int = 4,
) -> bool:
    """Return True if an existing thread on the same path overlaps in topic.

    Tiered threshold based on line distance:
    - Same line (distance 0): 1 distinctive shared token is enough.
    - Within line_window: min_shared_tokens (default 2).
    - Beyond window or unknown/file-level line: distant threshold (default 4+).
    """
    finding_tokens = _tokens(f"{finding.title} {finding.body}")
    if not finding_tokens:
        return False

    distant_required = max(min_shared_for_distant, len(finding_tokens) // 2)

    for t in threads:
        if t.path != finding.file:
            continue

        body_tokens = _tokens(t.body_snippet or "")
        if not body_tokens:
            continue

        f_line = finding.line if finding.line > 0 else None
        t_line = t.line if t.line is not None and t.line > 0 else None

        if f_line is not None and t_line is not None:
            distance = abs(t_line - f_line)
            if distance == 0:
                required = 1
            elif distance <= line_window:
                required = min_shared_tokens
            else:
                required = distant_required
        else:
            required = distant_required

        shared = finding_tokens & body_tokens
        if len(shared) >= required:
            return True

    return False


def apply_thread_dedup(
    findings: Sequence[Finding],
    threads: Sequence[Thread],
    line_window: int = 30,
    min_shared_tokens: int = 2,
    min_shared_for_distant: int = 4,
) -> list[Finding]:
    """Annotate findings that restate an existing discussion with drop_reason."""
    if not threads:
        return list(findings)

    result: list[Finding] = []
    for f in findings:
        if f.drop_reason is not None:
            result.append(f)
            continue
        if is_duplicate_of_existing(
            f,
            threads,
            line_window=line_window,
            min_shared_tokens=min_shared_tokens,
            min_shared_for_distant=min_shared_for_distant,
        ):
            result.append(replace(f, drop_reason="duplicate of existing thread"))
        else:
            result.append(f)
    return result


SETTLED_MIN_SHARED_TOKENS = 4


def _normalised_path(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def apply_settled_thread_suppression(
    findings: Sequence[Finding],
    threads: Sequence[Thread],
    min_shared_tokens: int = SETTLED_MIN_SHARED_TOKENS,
) -> list[Finding]:
    """Drop findings that re-litigate a subject already argued out in a thread.

    Line-INDEPENDENT, which is the whole point:
    :func:`apply_thread_dedup` needs the thread and the finding to sit near
    each other, and :func:`apply_line_align` has already demoted a
    non-anchorable finding to line 0 by the time either runs. A settled
    discussion is about a SUBJECT, not a line, so the test here is same path
    plus at least ``min_shared_tokens`` distinct shared content tokens between
    the finding's title+body and the thread's snippet.

    ``resolved`` does not gate it: a resolved thread is still a decision the
    reviewers made with more context than the review has. A thread with no
    path — a general, unanchored PR comment, near-universal on Bitbucket
    Server — cannot be "same path" as any finding and is skipped rather than
    compared. Order-preserving, pure, and already-dropped findings pass
    through untouched so the reason an earlier pass gave survives.
    """
    if not threads:
        return list(findings)

    result: list[Finding] = []
    for f in findings:
        if f.drop_reason is not None:
            result.append(f)
            continue
        finding_tokens = _tokens(f"{f.title} {f.body}")
        author = ""
        if finding_tokens:
            for t in threads:
                if t.path is None:
                    continue
                if _normalised_path(t.path) != _normalised_path(f.file):
                    continue
                body_tokens = _tokens(t.body_snippet or "")
                if len(finding_tokens & body_tokens) >= min_shared_tokens:
                    author = t.author or "unknown"
                    break
        if author:
            result.append(replace(f, drop_reason=f"settled in thread: {author}"))
        else:
            result.append(f)
    return result


_SEVERITY_RANK: dict[str, int] = {
    "error": 0, "warning": 1, "spec": 2, "outofscope": 3,
}

_TITLE_PUNCT_RE = re.compile(r"[`*\"'\u2018\u2019\u201c\u201d]")


def normalize_title(title: str) -> str:
    """Canonical form used to group findings that restate the same pattern.

    Lowercase; backticks, quotes, and emphasis marks removed; edge
    punctuation trimmed; whitespace runs collapsed to one space. Grouping
    is exact-match on this form, so a title that merely mentions another's
    words stays its own pattern.
    """
    lowered = _TITLE_PUNCT_RE.sub("", title.lower())
    trimmed = lowered.strip(" .:;,!?-")
    return " ".join(trimmed.split())


_TITLE_WORD_RE = re.compile(r"[a-z0-9]+")

_TITLE_TOKEN_MIN_LEN = 3

_TITLE_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "but", "for", "nor", "with", "without", "within", "into",
    "onto", "from", "via", "per", "instead", "rather", "than", "then",
    "that", "this", "these", "those", "its", "their", "when", "where",
    "which", "while", "can", "could", "should", "would", "must", "may",
    "might", "will", "are", "was", "were", "been", "being", "has", "have",
    "had", "does", "did",
    "use", "uses", "using", "replace", "add", "restore", "missing",
    "remove", "avoid", "ensure", "consider", "prefer", "fix", "make",
    "potential", "possible", "possibly", "likely",
})

TITLE_MIN_SHARED_TOKENS: int = 3
"""Fewest distinct title tokens two findings must share to count as similar.

Jaccard alone over-merges short titles: "SQL injection" against "SQL
injection in search query" scores exactly 0.5 while naming only two
words. Requiring three shared tokens keeps such a pair apart and still
admits every reworded duplicate measured for issue #10, which share
four or five.
"""


def _title_tokens(title: str) -> set[str]:
    """Content words of a finding title, for reworded-duplicate scoring.

    The ASCII ``[a-z0-9]+`` words of :func:`normalize_title`, at least
    three characters long, minus English function words and the generic
    remedy verbs and hedges (``use``, ``replace``, ``add``, ``restore``,
    ``missing``, ``potential``) that reworded titles swap freely. Hyphens
    and other punctuation split words, so ``string-literal`` and
    ``string literal`` yield the same tokens. Deliberately separate from
    :func:`_tokens`: its stopword list drops ``null`` and ``string`` and
    its four-character floor drops ``key``, which are exactly the words
    that tell two same-line titles apart.
    """
    return {
        word
        for word in _TITLE_WORD_RE.findall(normalize_title(title))
        if len(word) >= _TITLE_TOKEN_MIN_LEN and word not in _TITLE_STOPWORDS
    }


def title_similarity(a: str, b: str) -> tuple[float, int]:
    """Score how closely two finding titles restate one problem.

    Returns ``(jaccard, shared)``: the Jaccard index of the two titles'
    :func:`_title_tokens` sets and the number of distinct tokens they
    share. Two titles with no tokens between them score ``(0.0, 0)``.
    Symmetric in its arguments and a pure function of the two strings.
    """
    tokens_a = _title_tokens(a)
    tokens_b = _title_tokens(b)
    shared = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    if union == 0:
        return 0.0, 0
    return shared / union, shared


def titles_similar(a: str, b: str, threshold: float) -> bool:
    """Return True when two finding titles are reworded restatements.

    Similar means :func:`title_similarity` reaches ``threshold`` on the
    Jaccard index AND the titles share at least
    :data:`TITLE_MIN_SHARED_TOKENS` tokens. The token floor is what keeps
    a short title from merging into any longer one that contains it.
    """
    jaccard, shared = title_similarity(a, b)
    return jaccard >= threshold and shared >= TITLE_MIN_SHARED_TOKENS


def finding_sort_key(finding: Finding) -> tuple[str, int, str]:
    """Content-derived ordering key for a finding: ``(file, line, title)``.

    Every pass that emits or presents a list of findings sorts by this key so
    the output of a review depends on WHAT was found, never on the order the
    workers happened to return it in.
    """
    return (
        finding.file or "",
        finding.line if finding.line is not None else -1,
        finding.title or "",
    )


def finding_rank_key(finding: Finding) -> tuple[float, str, int, str]:
    """Tie-break key for caps: ``(-confidence, file, line, normalized title)``.

    Confidence decides first; everything after it exists only so that two
    equally confident findings are ranked by content rather than by arrival.
    """
    file, line, _ = finding_sort_key(finding)
    return (
        -float(finding.confidence or 0.0),
        file,
        line,
        normalize_title(finding.title or ""),
    )


logger = logging.getLogger(__name__)


def apply_sweep_dedup(
    findings: Sequence[Finding],
    sweep_start: int,
    *,
    similarity: float | None = None,
) -> list[Finding]:
    """Drop systemic-sweep findings that restate a chunk finding.

    ``findings[sweep_start:]`` came from the whole-PR systemic sweep; the
    entries before it came from the per-chunk workers. A sweep finding whose
    (file, :func:`normalize_title` title) pair matches a surviving chunk
    finding adds no recall — it is the same pattern the chunk seat already
    reported — and is dropped with ``drop_reason="duplicate of chunk
    finding"``, the same retained-not-silenced convention every other pass
    uses. A sweep finding never causes a chunk finding to be dropped, even
    when the sweep phrased the pattern first: the chunk seat cited the exact
    line.

    A chunk finding that :func:`apply_rule_grouping` folded into its group
    (``drop_reason`` ``grouped into <file>:<line>``) still adds its key: its
    location is listed on the group's representative, so a sweep copy that
    restates it adds no recall either, and the key set is the same whether
    grouping ran before this pass or not. A member adds its key even when a
    cap later drops its representative. So does a chunk finding
    :func:`apply_rule_cap` folded (``drop_reason`` ``rule cap exceeded (max
    <n>): listed at <file>:<line>``), whose location the best kept finding
    of its rule now lists.

    ``similarity`` switches on a second, reworded tier that runs after the
    exact tier; ``None`` (the default) skips it entirely, so the pass is
    the exact tier alone. The tier compares findings that are still active,
    in the same file and on the same line; a line-0 (file-level) finding is
    never compared. Two such findings are duplicates when
    :func:`titles_similar` holds at ``similarity``.

    - Across the chunk/sweep boundary the chunk copy always survives. The
      sweep copy is dropped only when its severity is no higher than the
      chunk copy's; a MORE severe sweep copy is kept alongside it, so the
      tier can never lower a review's worst severity. Confidence plays no
      part here.
    - Between two findings on the same side (chunk vs chunk, or sweep vs
      sweep), the more severe one is kept, then the one ranked first by
      :func:`finding_rank_key`: higher confidence, then content.

    Chunk findings are settled among themselves first; each sweep finding
    is then compared with the chunk copies kept on its line before the
    sweep copies kept there. A finding is only ever compared with copies
    already kept, in a fixed content order, so the result does not depend
    on input order and a dropped copy never drops a third. The dropped
    copy's ``drop_reason`` names the side of the copy it restates and the
    :func:`title_similarity` Jaccard score to two decimals:
    ``duplicate of chunk finding (reworded, similarity 0.57)`` or
    ``duplicate of sweep finding (reworded, similarity 0.57)``.

    Runs after :func:`apply_quality_gate`, so the duplicate set is built
    from chunk findings that SURVIVED it — a sub-floor chunk finding never
    suppresses its sweep duplicate — and after
    :func:`apply_severity_consistency`, so a restated pattern still agrees
    on severity with its group before any of it is judged. ``sweep_start``
    below zero is treated as zero (everything is sweep output); past the
    end, nothing is deduplicated.
    """
    start = max(0, sweep_start)
    chunk_keys = {
        (f.file, normalize_title(f.title))
        for f in findings[:start]
        if f.drop_reason is None
        or f.drop_reason.startswith(GROUPED_INTO_PREFIX)
        or f.drop_reason.startswith(RULE_CAP_PREFIX)
    }
    result: list[Finding] = []
    for i, f in enumerate(findings):
        if (
            i >= start
            and f.drop_reason is None
            and (f.file, normalize_title(f.title)) in chunk_keys
        ):
            result.append(replace(f, drop_reason="duplicate of chunk finding"))
        else:
            result.append(f)
    if similarity is None:
        return result
    return _dedup_reworded(result, start, similarity)


def _reworded_severity_rank(finding: Finding) -> int:
    severity = (finding.severity or "").strip().lower()
    return _SEVERITY_RANK.get(severity, len(_SEVERITY_RANK))


def _reworded_keep_key(
    finding: Finding,
) -> tuple[int, tuple[float, str, int, str], str]:
    return (
        _reworded_severity_rank(finding),
        finding_rank_key(finding),
        repr(finding),
    )


def _first_reworded_match(
    findings: Sequence[Finding],
    candidate: int,
    kept: Sequence[int],
    threshold: float,
) -> float | None:
    title = findings[candidate].title or ""
    for k in kept:
        kept_title = findings[k].title or ""
        if titles_similar(title, kept_title, threshold):
            return title_similarity(title, kept_title)[0]
    return None


def _reworded_drop(finding: Finding, side: str, jaccard: float) -> Finding:
    return replace(
        finding,
        drop_reason=f"duplicate of {side} finding (reworded, similarity {jaccard:.2f})",
    )


def _dedup_reworded(
    findings: list[Finding], start: int, threshold: float
) -> list[Finding]:
    lines: dict[tuple[str, int], list[int]] = {}
    for i, f in enumerate(findings):
        if f.drop_reason is None and (f.line or 0) > 0:
            lines.setdefault((f.file, f.line), []).append(i)
    result = list(findings)
    for indices in lines.values():
        if len(indices) < 2:
            continue
        chunk_side = sorted(
            (i for i in indices if i < start),
            key=lambda i: _reworded_keep_key(findings[i]),
        )
        sweep_side = sorted(
            (i for i in indices if i >= start),
            key=lambda i: _reworded_keep_key(findings[i]),
        )
        kept_chunk: list[int] = []
        for i in chunk_side:
            jaccard = _first_reworded_match(findings, i, kept_chunk, threshold)
            if jaccard is None:
                kept_chunk.append(i)
            else:
                result[i] = _reworded_drop(findings[i], "chunk", jaccard)
        kept_sweep: list[int] = []
        for i in sweep_side:
            rank = _reworded_severity_rank(findings[i])
            at_least_as_severe = [
                k for k in kept_chunk if _reworded_severity_rank(findings[k]) <= rank
            ]
            jaccard = _first_reworded_match(findings, i, at_least_as_severe, threshold)
            if jaccard is not None:
                result[i] = _reworded_drop(findings[i], "chunk", jaccard)
                continue
            jaccard = _first_reworded_match(findings, i, kept_sweep, threshold)
            if jaccard is None:
                kept_sweep.append(i)
            else:
                result[i] = _reworded_drop(findings[i], "sweep", jaccard)
    return result


_THROW_CLASS_RE = re.compile(
    r"\bthrows?\b|\bthrowing\b|\bpanics?\b|\bcrash(es|ed)?\b"
    r"|unhandled rejection|uncaught exception|\braises?\b",
    re.IGNORECASE,
)

_CONTAINMENT_BOUNDARY_RE = re.compile(
    r"\bcaught\b|\buncaught\b|propagat|catch block|catch\s*\(|try/catch"
    r"|try-catch|\bswallow|handled by|bubbles? up"
    r"|rejects the promise returned to",
    re.IGNORECASE,
)

CONTAINMENT_NOTE_SUFFIX: str = " [containment boundary not stated]"


def apply_containment_note(findings: Sequence[Finding]) -> list[Finding]:
    """Suffix an un-boundaried throw-class finding's body with a note.

    A finding whose title or body asserts a throw, panic, crash, or
    unhandled rejection (:data:`_THROW_CLASS_RE`) but whose body never
    names where that exception is caught or where it propagates to
    (:data:`_CONTAINMENT_BOUNDARY_RE`) has its body suffixed with
    :data:`CONTAINMENT_NOTE_SUFFIX`, once — a body already carrying the
    suffix is left unchanged, so the pass is idempotent under repeated
    application. Purely textual: order-preserving, runs on active and
    dropped findings alike (it only decorates the text every downstream
    consumer, including the dropped-findings audit, already carries),
    and never touches ``drop_reason`` or ``severity``. Findings that do
    not match, or already name a boundary, pass through as the same
    instance.
    """
    result: list[Finding] = []
    for f in findings:
        claim = f"{f.title} {f.body}"
        if (
            f.body.endswith(CONTAINMENT_NOTE_SUFFIX)
            or not _THROW_CLASS_RE.search(claim)
            or _CONTAINMENT_BOUNDARY_RE.search(f.body)
        ):
            result.append(f)
        else:
            result.append(replace(f, body=f.body + CONTAINMENT_NOTE_SUFFIX))
    return result


def _problem_classes(title: str) -> set[str]:
    """Canonical problem classes a title names, e.g. ``{"injection"}``.

    Two titles share a verdict class when their sets intersect. Several
    phrasings map to one class on purpose: the live issue #30 family says
    "injection", "interpolation", "unescaped", and "filter manipulation"
    for the same bug, so all four sit in ``injection`` — a guard keyed on
    literal keyword equality would never bind them, which is the failure
    this pass fixes.
    """
    return {name for name, pattern in _PROBLEM_CLASSES if pattern.search(title)}


def _code_tokens(claim: str) -> set[str]:
    """Code-identifier tokens in a finding's title+body claim.

    Three shapes qualify: backticked identifier pieces (floor of 3
    chars, since backticks already mark them as code), bare compound
    identifiers — snake_case, UPPER_SNAKE_CASE, camelCase — at 6+ chars,
    and dotted paths as whole lowercase paths. File-path citations are
    stripped first (a path names a location, not a shared construct),
    and generic vocabulary is excluded via :data:`_CODE_STOPWORDS`.

    Whole identifiers only: unlike the evidence tokens above, compounds
    are never split, because grouping binds on the construct itself —
    ``vimeo_code`` must not match a claim that merely says "code".
    """
    text = _CITATION_RE.sub(" ", claim)
    tokens: set[str] = set()
    for span in _BACKTICK_RE.findall(text):
        for piece in _CODE_PIECE_RE.findall(span):
            tok = piece.lower().strip("._-")
            if len(tok) >= 3 and tok not in _CODE_STOPWORDS:
                tokens.add(tok)
    text = _BACKTICK_RE.sub(" ", text)
    for pattern in (_SNAKE_RE, _UPPER_SNAKE_RE, _CAMEL_RE, _DOTTED_RE):
        for raw in pattern.findall(text):
            if len(raw) >= 6:
                tokens.add(raw.lower())
    return tokens


CODE_TOKEN_RARITY_MAX: int = 2
"""Largest claim count a code token may have and still bind a group.

A token appearing in the claims of at most 2 findings names a construct
specific to one bug family; the same name in 3+ claims is systemic
vocabulary (an API used everywhere, a framework type), and binding on it
would chain unrelated findings into one severity. Tuned so the live
issue #30 pair (each token in exactly 2 claims) binds while the
3-claim sweep in the tests does not.
"""

_CODE_STOPWORDS: frozenset[str] = frozenset({
    "api", "app", "args", "attr", "attrs", "body", "callback", "case",
    "check", "class", "code", "component", "config", "const", "constant",
    "content", "context", "count", "create", "ctx", "data", "db",
    "default", "delete", "element", "err", "error", "example", "field",
    "file", "find", "func", "function", "get", "global", "handler",
    "handling", "helper", "id", "impl", "index", "init", "input",
    "instance", "issue", "item", "key", "length", "line", "list", "load",
    "local", "log", "logic", "manager", "message", "meta", "method",
    "mode", "model", "module", "msg", "name", "node", "null", "num",
    "number", "obj", "object", "option", "options", "output", "param",
    "params", "pattern", "process", "prop", "property", "record",
    "req", "request", "res", "response", "result", "return", "review",
    "route", "schema", "service", "set", "settings", "spec", "state",
    "store", "str", "string", "temp", "test", "text", "tmp", "type",
    "update", "util", "utils", "val", "value", "values", "var",
    "variable", "warning", "worker",
})

_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_CODE_PIECE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
_SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_UPPER_SNAKE_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_CAMEL_RE = re.compile(r"\b[a-z]+(?:[A-Z][a-z0-9]+)+\b")
_DOTTED_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_\-]*(?:\.[A-Za-z_][A-Za-z0-9_\-]+)+\b")

_PROBLEM_CLASSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("injection", re.compile(
        r"inject|interpolat|escap|sanitiz|manipulat", re.IGNORECASE)),
    ("secret", re.compile(
        r"secret|credential|password|\bapi[ -]?key|\btoken\b|hard[ -]?cod",
        re.IGNORECASE)),
    ("auth", re.compile(
        r"\bauth|unauthori|permission|privilege|access control", re.IGNORECASE)),
    ("race", re.compile(
        r"\brace\b|non-?atomic|deadlock|concurren", re.IGNORECASE)),
    ("leak", re.compile(r"\bleak|expos|disclos", re.IGNORECASE)),
    ("truncation", re.compile(r"truncat|overflow", re.IGNORECASE)),
    ("null", re.compile(r"\bnull\b|\bnil\b|nonetype|undefined", re.IGNORECASE)),
    ("duplicate", re.compile(r"duplicat|redundan", re.IGNORECASE)),
    ("timeout", re.compile(r"timeout|unbounded|infinite", re.IGNORECASE)),
    ("ratelimit", re.compile(r"rate[ -]?limit|throttl|\b429\b", re.IGNORECASE)),
    ("validation", re.compile(r"validat|malformed|invalid", re.IGNORECASE)),
)


def apply_severity_consistency(findings: Sequence[Finding]) -> list[Finding]:
    """Raise every finding in a severity group to the group's max severity.

    Two grouping mechanisms union into one component graph:

    1. Normalized-title equality (issue #18): findings sharing one
       :func:`normalize_title` form restate the same pattern and always
       merge — the strictly stronger rule.
    2. Shared rare code token (issue #30): the same construct phrased
       differently across claims — ``vimeo_code`` interpolated into
       ``filterByFormula``, reported once as injection and once as
       manipulation — merges only when both a rarity rule and a
       verdict-class guard hold. Rarity: the token appears in the claims
       of at most :data:`CODE_TOKEN_RARITY_MAX` findings in this review
       (see that constant). Verdict-class guard: the pair sits in the
       same file, or both titles name a common problem class (injection,
       secret, auth, race, leak, truncation, null, duplicate, timeout,
       rate limit, validation — see :func:`_problem_classes`). The guard
       is the over-merging brake: two findings sharing a token but
       describing different problems stay apart.

    Components bind transitively (A shares a token with B, B with C, so
    all three group). Each component is rewritten to its highest severity
    (error > warning > spec > outofscope). A rewritten finding keeps
    its own file, line, body, and confidence; only severity changes.
    Findings carrying a ``drop_reason`` or a severity outside the
    vocabulary pass through untouched. One summary line is logged when
    token-driven rewrites happen, naming the count and the binding
    tokens; silent otherwise.
    """
    idx = [
        i for i, f in enumerate(findings)
        if f.drop_reason is None
        and (f.severity or "").strip().lower() in _SEVERITY_RANK
    ]
    if not idx:
        return list(findings)

    parent = {i: i for i in idx}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_title: dict[str, int] = {}
    for i in idx:
        key = normalize_title(findings[i].title)
        seen = by_title.get(key)
        if seen is not None:
            union(seen, i)
        else:
            by_title[key] = i

    claims = [f"{findings[i].title} {findings[i].body}" for i in idx]
    tok_sets = [_code_tokens(claim) for claim in claims]
    counts: Counter[str] = Counter()
    for tokens in tok_sets:
        counts.update(tokens)
    rare = {tok for tok, n in counts.items() if n <= CODE_TOKEN_RARITY_MAX}
    classes = [_problem_classes(findings[i].title) for i in idx]

    token_edges: dict[tuple[int, int], set[str]] = {}
    for a in range(len(idx)):
        for b in range(a + 1, len(idx)):
            shared = tok_sets[a] & tok_sets[b] & rare
            if not shared:
                continue
            if findings[idx[a]].file == findings[idx[b]].file or classes[a] & classes[b]:
                union(idx[a], idx[b])
                token_edges[(idx[a], idx[b])] = shared

    components: dict[int, list[int]] = {}
    for i in idx:
        components.setdefault(find(i), []).append(i)

    target_of: dict[int, str] = {}
    token_rewrites = 0
    binding: set[str] = set()
    for members in components.values():
        sevs = {(findings[i].severity or "").strip().lower() for i in members}
        if len(sevs) < 2:
            continue
        top = min(sevs, key=lambda s: _SEVERITY_RANK[s])
        member = set(members)
        edges_here = [
            tokens for (a, b), tokens in token_edges.items()
            if a in member and b in member
        ]
        rewritten_here = 0
        for i in members:
            target_of[i] = top
            if (findings[i].severity or "").strip().lower() != top:
                rewritten_here += 1
        if edges_here and rewritten_here:
            token_rewrites += rewritten_here
            for tokens in edges_here:
                binding |= tokens

    if token_rewrites and binding:
        logger.info(
            "severity consistency: raised %d finding(s) via shared rare code token(s): %s",
            token_rewrites, ", ".join(sorted(binding)),
        )

    result: list[Finding] = []
    for pos, f in enumerate(findings):
        severity = (f.severity or "").strip().lower()
        if f.drop_reason is None and severity in _SEVERITY_RANK:
            target = target_of.get(pos, severity)
            if severity != target:
                result.append(replace(f, severity=target))
                continue
        result.append(f)
    return result


_REMOVAL_CLAIM_RE = re.compile(
    r"\b(?:removed|deleted|dropped the file|no longer exists|was removed"
    r"|is removed|has been removed|deletion of)\b",
    re.IGNORECASE,
)


def _post_image_paths(files: Sequence[FileDiff]) -> set[str]:
    """Every path the diff leaves present after the PR lands.

    A file section with any status other than ``removed`` leaves its path
    in place, and a ``copied`` section leaves its SOURCE in place too — a
    copy reads the original without touching it, so only a separate
    ``removed`` section can take that source away.
    """
    present: set[str] = set()
    for f in files:
        if f.status != "removed":
            present.add(f.path)
        if f.status == "copied" and f.old_path:
            present.add(f.old_path)
    return present


def _diff_path_candidates(files: Sequence[FileDiff]) -> list[str]:
    """Every path a finding could name: each section's path plus copy/rename sources."""
    seen: list[str] = []
    for f in files:
        for path in (f.path, f.old_path if f.status in {"copied", "renamed"} else None):
            if path and path not in seen:
                seen.append(path)
    return seen


def _tail(path: str, parts: int) -> str:
    return "/".join(path.replace("\\", "/").split("/")[-parts:])


def _claimed_paths(finding: Finding, candidates: Sequence[str]) -> list[str]:
    """Diff paths the finding's title+body name, in order of first mention.

    A path counts as named by its full form or by its
    basename-with-parent-directory (``alpha/package.json``), which is
    how a worker usually refers to a file inside a monorepo package.
    """
    text = f"{finding.title} {finding.body}".replace("\\", "/").lower()
    hits: list[tuple[int, str]] = []
    for path in candidates:
        norm = path.replace("\\", "/").lower()
        at = text.find(norm)
        if at < 0 and "/" in norm:
            at = text.find(_tail(norm, 2))
        if at >= 0:
            hits.append((at, path))
    hits.sort()
    return [path for _, path in hits]


# Words that end a removal verb's object: a preposition or a conjunction after
# the verb means the following path is the place something was removed FROM, or
# a new clause, not the thing removed ("the removed null check in src/app.py").
_OBJECT_STOP = (
    r"in|from|by|to|at|on|of|for|with|into|onto|via|and|or|but|so"
    r"|because|that|which|when|while|after|before|since"
)
# Up to three determiner/qualifier tokens may sit between the verb and its
# object ("removed the Alpha package.json"), none of them a stop word.
_OBJECT_FILLER = rf"(?:(?!(?:{_OBJECT_STOP})\b)[\w.'-]+\s+){{0,3}}"
_REMOVAL_VERB_PRE = r"(?:removed|deleted|deletion of|removal of)"
_REMOVAL_VERB_POST = (
    r"(?:(?:was|were|has been|have been|is|are|gets|got)\s+(?:removed|deleted)"
    r"|no longer exists?)"
)


def _path_forms(path: str) -> list[str]:
    """How a body may spell one diff path: full, parent/basename, basename."""
    norm = path.replace("\\", "/").lower()
    forms = [norm]
    if "/" in norm:
        forms.append(_tail(norm, 2))
        forms.append(_tail(norm, 1))
    return forms


def _governed_claimed_paths(
    finding: Finding, candidates: Sequence[str]
) -> list[str]:
    """Diff paths a removal verb GOVERNS in the finding's own text.

    A bare ``removed`` somewhere in a body and a path somewhere else in it
    are not a removal claim: "the removed null check in ``src/app.py``"
    says the check went, not the file. A path counts only when the verb
    takes it as its object (``removed the file src/app.py``) or when the
    path is the subject of the removal (``src/app.py was removed``, ``…no
    longer exists``), with no sentence break between the two.
    """
    text = f"{finding.title} {finding.body}".replace("\\", "/").lower()
    hits: list[tuple[int, str]] = []
    for path in candidates:
        alternation = "|".join(re.escape(form) for form in _path_forms(path))
        anchored = rf"(?<![\w/-])`?(?:{alternation})`?(?![\w/-])"
        pattern = re.compile(
            rf"{_REMOVAL_VERB_PRE}\s+(?:the\s+)?(?:file\s+)?"
            rf"{_OBJECT_FILLER}{anchored}"
            rf"|{anchored}\s+{_REMOVAL_VERB_POST}"
        )
        m = pattern.search(text)
        if m is not None:
            hits.append((m.start(), path))
    hits.sort()
    return [path for _, path in hits]


def apply_removal_claim_check(
    findings: Sequence[Finding], files: Sequence[FileDiff]
) -> list[Finding]:
    """Drop removal claims the diff's post-image contradicts.

    A finding whose title or body asserts a file was removed, deleted, or
    no longer exists is dropped when every path it names is still present
    after the PR lands. The removal verb has to GOVERN one of those paths:
    "the removed null check in ``src/app.py``" removes a check, not the
    file, so a bare removal word plus an unrelated path mention is left
    active. A copy's source is present unless a separate
    section removes it, so a ``copy from``/``copy to`` header read as a
    move produces exactly this false positive. When any named path really
    is absent from the post-image, the claim is left active — the pass
    never blanket-drops removal language.

    Input order is preserved and findings already carrying a
    ``drop_reason`` pass through untouched.
    """
    present = _post_image_paths(files)
    candidates = _diff_path_candidates(files)
    out: list[Finding] = []
    for f in findings:
        if f.drop_reason is not None:
            out.append(f)
            continue
        if not _REMOVAL_CLAIM_RE.search(f"{f.title} {f.body}"):
            out.append(f)
            continue
        # Only a claim whose removal verb GOVERNS a path is judged. Falling
        # back to the finding's anchor file, or to any path merely mentioned
        # somewhere in the body, would drop every finding that says something
        # was "removed" — a guard, a constant, a validator — on a file the PR
        # still leaves in place, which is precisely the guard-removal class
        # the systemic sweep exists to report.
        governed = _governed_claimed_paths(f, candidates)
        named = _claimed_paths(f, candidates)
        if governed and all(path in present for path in named):
            out.append(
                replace(
                    f,
                    drop_reason=(
                        "claims removal of a path present in the "
                        f"post-image: {governed[0]}"
                    ),
                )
            )
            continue
        out.append(f)
    return out


def _resolve_confidence_floor(explicit: float | None) -> float:
    if explicit is not None:
        return explicit
    raw = os.environ.get("PRXREF_CONFIDENCE_FLOOR")
    if raw is not None:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_CONFIDENCE_FLOOR


def _resolve_max_errors(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    for name in ("PRXREF_MAX_ERROR_FINDINGS", "PRXREF_MAX_ERRORS"):
        raw = os.environ.get(name)
        if raw is not None:
            try:
                return int(raw)
            except ValueError:
                pass
    return DEFAULT_MAX_ERRORS


def _apply_severity_cap(staged: list[Finding], severity: str, cap: int) -> None:
    active_indices: list[int] = [
        i for i, f in enumerate(staged)
        if f.drop_reason is None and f.severity == severity
    ]

    if len(active_indices) > cap:
        ranked = sorted(
            active_indices,
            key=lambda idx: finding_rank_key(staged[idx]),
        )
        for dropped_idx in ranked[cap:]:
            staged[dropped_idx] = replace(
                staged[dropped_idx],
                drop_reason=f"{severity} cap exceeded (max {cap})",
            )


def _hedge_span(text: str) -> str | None:
    for _name, pattern in HEDGE_RULES:
        m = pattern.search(text)
        if m is None:
            continue
        span = m.group(0).strip(" \t\n,;.")
        return span[:_HEDGE_SPAN_MAX]
    return None


def _spec_quote_len(rest: str, digest_lower: str) -> int:
    lo, hi = 0, min(len(rest), len(digest_lower))
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if rest[:mid].lower() in digest_lower:
            lo = mid
        else:
            hi = mid - 1
    for k in range(min(lo, len(rest) - 1), 0, -1):
        if rest[k] in _SPEC_QUOTE_CLOSERS:
            return k
    return 0


def _blank_spec_quotes(body: str, spec_digest: str) -> str:
    if not spec_digest:
        return body
    digest_lower = spec_digest.lower()
    parts: list[str] = []
    pos = 0
    for m in _SPEC_QUOTE_OPEN_RE.finditer(body):
        if m.start() < pos:
            continue
        k = _spec_quote_len(body[m.end():], digest_lower)
        if k:
            parts.append(body[pos:m.end()])
            pos = m.end() + k
    parts.append(body[pos:])
    return "".join(parts)


def apply_hedge_gate(
    findings: Sequence[Finding], *, spec_digest: str = ""
) -> list[Finding]:
    """Drop findings whose own text conditions the defect on an unverified fact.

    A hedged finding ("If toolProxy.prepare still leases a client", "If they
    are members of the root workspaces globs") states a precondition the
    worker had the whole diff to check and did not. Each rule in
    ``HEDGE_RULES`` is anchored on an epistemic marker rather than a bare
    ``if``/``may``/``unless``, which appear throughout legitimate findings.

    Pure and order-preserving: already-dropped findings pass through
    untouched, and a match sets ``drop_reason`` to ``hedged: "<span>"``
    naming the matched text so the drop is auditable in the run record.

    ``spec_digest`` is the spec constraints block the workers were shown.
    After each ``Spec:`` marker in the body (with or without an opening
    quote), the longest following text that appears verbatim in the digest,
    compared case-insensitively, is cut back to end just before a closing
    quote and removed before the rules read the body; when no closing quote
    follows any part of it, nothing is removed. A condition inside a real
    constraint belongs to the spec, not to the model's reasoning, and this
    holds for every severity. Text the digest does not hold, and every quote
    when the digest is empty, is read like the rest of the body, so a model
    cannot hide its own hedge inside a fabricated ``Spec: "..."``. The title
    is always read as written.
    """
    out: list[Finding] = []
    for f in findings:
        if f.drop_reason is not None:
            out.append(f)
            continue
        body = _blank_spec_quotes(f.body or "", spec_digest)
        span = _hedge_span(f.title or "") or _hedge_span(body)
        if span is None:
            out.append(f)
            continue
        out.append(replace(f, drop_reason=f'hedged: "{span}"'))
    return out


GROUPED_INTO_PREFIX: str = "grouped into "
"""``drop_reason`` prefix of a finding :func:`apply_rule_grouping` folded away.

The full reason is ``grouped into <file>:<line>``, naming the location of
the representative that now lists the finding's own location.
"""

RULE_CAP_PREFIX: str = "rule cap exceeded "
"""``drop_reason`` prefix of a finding :func:`apply_rule_cap` folded away.

The full reason is ``rule cap exceeded (max <n>): listed at <file>:<line>``,
naming the per-rule cap and the location of the best kept finding of the
rule, whose ``locations`` and ``Also at:`` paragraph now list the folded
finding's own location.
"""

RULE_CAP_LISTED_LOCATIONS: int = 5
"""Most locations the ``Also at:`` paragraph of :func:`apply_rule_cap` names.

Any further ones are counted in a `` (+<k> more)`` suffix; the kept
finding's ``locations`` carries every one of them.
"""


def _grouping_line(finding: Finding) -> int | None:
    line = finding.line
    if line is None:
        return 0
    if isinstance(line, bool) or not isinstance(line, int):
        return None
    return line if line > 0 else 0


def _grouping_confidence(finding: Finding) -> float | None:
    raw = finding.confidence
    if raw is None:
        return 0.0
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return float(raw)


def _grouping_key(finding: Finding) -> tuple[str, str, str] | None:
    title = finding.title
    if title is not None and not isinstance(title, str):
        return None
    rule = finding.rule
    if rule is not None and not isinstance(rule, str):
        return None
    label = " ".join(rule.split()).casefold() if rule is not None else ""
    if label:
        return (finding.file, "rule", label)
    normalized = normalize_title(title or "")
    if not normalized:
        return None
    return (finding.file, "title", normalized)


def _grouping_candidate_key(
    finding: Finding, floor: float
) -> tuple[str, str, str] | None:
    if finding.drop_reason is not None:
        return None
    if not isinstance(finding.file, str) or not finding.file:
        return None
    if finding.body is not None and not isinstance(finding.body, str):
        return None
    severity = finding.severity
    if not isinstance(severity, str) or severity.strip().lower() not in SEVERITIES:
        return None
    confidence = _grouping_confidence(finding)
    if confidence is None or not confidence >= floor:
        return None
    if _grouping_line(finding) is None:
        return None
    return _grouping_key(finding)


def _group_anchor_key(
    findings: Sequence[Finding], index: int
) -> tuple[int, int, tuple[float, str, int, str], int]:
    finding = findings[index]
    line = _grouping_line(finding) or 0
    return (0 if line > 0 else 1, line, finding_rank_key(finding), index)


def _fold_group(
    findings: Sequence[Finding], members: Sequence[int], result: list[Finding]
) -> None:
    anchor = min(members, key=lambda i: _group_anchor_key(findings, i))
    representative = findings[anchor]
    anchor_line = _grouping_line(representative) or 0
    top_severity = min(
        (findings[i].severity.strip().lower() for i in members),
        key=lambda s: _SEVERITY_RANK[s],
    )
    most_confident = max(
        members,
        key=lambda i: (_grouping_confidence(findings[i]) or 0.0, -i),
    )
    other_lines = sorted({
        line
        for i in members
        if (line := _grouping_line(findings[i]) or 0) > 0 and line != anchor_line
    })
    body = representative.body or ""
    if other_lines:
        locations = ", ".join(
            f"`{representative.file}:{line}`" for line in other_lines
        )
        also_at = f"Also at: {locations}"
        body = f"{body.rstrip()}\n\n{also_at}" if body.strip() else also_at
    result[anchor] = replace(
        representative,
        severity=top_severity,
        confidence=findings[most_confident].confidence,
        body=body,
        locations=tuple((representative.file, line) for line in other_lines),
    )
    reason = f"{GROUPED_INTO_PREFIX}{representative.file}:{anchor_line}"
    for i in members:
        if i != anchor:
            result[i] = replace(findings[i], drop_reason=reason)


def apply_rule_grouping(
    findings: Sequence[Finding],
    *,
    confidence_floor: float | None,
    sweep_start: int,
) -> list[Finding]:
    """Fold chunk findings that break one rule in one file into one finding.

    Opt-in with ``PRXREF_GROUP_FINDINGS``: the caller runs this pass only
    when grouping is on, so a run without it never reaches this code.

    A finding is a candidate when it has no ``drop_reason``, its severity is
    in :data:`SEVERITIES` after trimming and lower-casing, its confidence is
    at or above the floor :func:`apply_quality_gate` would apply
    (``confidence_floor``, else ``PRXREF_CONFIDENCE_FLOOR``, else
    :data:`DEFAULT_CONFIDENCE_FLOOR`), and it sits on the chunk side of the
    list, before ``sweep_start``. Whole-PR sweep findings are never grouped
    and never anchor a group. ``sweep_start`` below zero is treated as zero
    (everything is sweep output); past the end, every finding is on the
    chunk side.

    Candidates group on their file plus their ``rule``, compared after
    whitespace collapsing and ``casefold()``. A finding without a rule
    groups on its file plus its :func:`normalize_title` title instead, and
    never with a finding that has one. The file is part of the key, so the
    same rule in two files forms two groups. Scope is not part of the key.

    Only a group of two or more changes anything. Its representative is the
    member on the smallest positive line; a file-level (line 0) member
    anchors only when no member has a positive line. A tie on the line goes
    to the member :func:`finding_rank_key` ranks first, then to the earlier
    one. The representative keeps its own position, title and scope, takes
    the group's highest severity (``error`` > ``warning`` > ``spec`` >
    ``outofscope``) and, independently, its highest confidence. Its body
    gains a last paragraph, after a blank line, that reads ``Also at:``
    followed by every other positive line of the group as a backticked
    ``<file>:<line>``, comma-separated, once each, in line order. A line
    the representative sits on, and a file-level member, add no location;
    when nothing is left the body is unchanged. The representative's
    ``locations`` is set to exactly the ``(file, line)`` pairs that paragraph
    lists, in the same order, and to ``()`` when it lists none.
    Every other member keeps its identity and gains ``drop_reason``
    ``grouped into <file>:<line>``, naming the representative's location.

    Runs after the thread, removal and hedge passes, so each member is judged
    at its own line and a dropped member is never listed, and before
    :func:`apply_quality_gate`, so the caps count groups rather than lines.
    Pure apart from reading ``PRXREF_CONFIDENCE_FLOOR`` when
    ``confidence_floor`` is ``None``; the result has the input's length and
    order, and a finding whose fields are not the documented types is
    passed through untouched rather than raising.
    """
    floor = _resolve_confidence_floor(confidence_floor)
    start = max(0, sweep_start)
    groups: dict[tuple[str, str, str], list[int]] = {}
    for i, f in enumerate(findings[:start]):
        key = _grouping_candidate_key(f, floor)
        if key is not None:
            groups.setdefault(key, []).append(i)
    result = list(findings)
    for members in groups.values():
        if len(members) >= 2:
            _fold_group(findings, members, result)
    return result


def _rule_cap_active(cap: object) -> bool:
    return isinstance(cap, int) and not isinstance(cap, bool) and cap >= 1


def _rule_cap_rank_key(
    findings: Sequence[Finding], index: int
) -> tuple[int, tuple[float, str, int, str], int]:
    finding = findings[index]
    return (
        _SEVERITY_RANK[finding.severity.strip().lower()],
        finding_rank_key(finding),
        index,
    )


def _rule_cap_groups(
    findings: Sequence[Finding], floor: float, sweep_start: int
) -> dict[tuple[str, str], list[int]]:
    groups: dict[tuple[str, str], list[int]] = {}
    for i, f in enumerate(findings[:max(0, sweep_start)]):
        key = _grouping_candidate_key(f, floor)
        if key is not None:
            groups.setdefault((key[1], key[2]), []).append(i)
    for members in groups.values():
        members.sort(key=lambda i: _rule_cap_rank_key(findings, i))
    return groups


def _own_locations(finding: Finding) -> list[tuple[str, int]]:
    raw = finding.locations
    if not isinstance(raw, tuple | list):
        return []
    return [
        (entry[0], entry[1])
        for entry in raw
        if isinstance(entry, tuple | list)
        and len(entry) == 2
        and isinstance(entry[0], str)
        and isinstance(entry[1], int)
        and not isinstance(entry[1], bool)
    ]


def _rule_cap_body(
    best: Finding, merged: Sequence[tuple[str, int]]
) -> str:
    body = best.body or ""
    own = _own_locations(best)
    if own:
        grouped = "Also at: " + ", ".join(f"`{file}:{line}`" for file, line in own)
        if body == grouped:
            body = ""
        elif body.endswith("\n\n" + grouped):
            body = body[: -len("\n\n" + grouped)]
    listed = ", ".join(
        f"`{file}`" if line == 0 else f"`{file}:{line}`"
        for file, line in merged[:RULE_CAP_LISTED_LOCATIONS]
    )
    unlisted = len(merged) - RULE_CAP_LISTED_LOCATIONS
    also_at = f"Also at: {listed}" + (f" (+{unlisted} more)" if unlisted > 0 else "")
    return f"{body.rstrip()}\n\n{also_at}" if body.strip() else also_at


def _fold_over_cap(
    findings: Sequence[Finding],
    members: Sequence[int],
    cap: int,
    result: list[Finding],
) -> None:
    best = findings[members[0]]
    best_line = _grouping_line(best) or 0
    folded = members[cap:]
    merged = set(_own_locations(best))
    for i in folded:
        member = findings[i]
        merged.add((member.file, _grouping_line(member) or 0))
        merged.update(_own_locations(member))
    merged.discard((best.file, best_line))
    ordered = tuple(sorted(merged))
    body = _rule_cap_body(best, ordered) if ordered else best.body
    result[members[0]] = replace(best, locations=ordered, body=body)
    reason = f"{RULE_CAP_PREFIX}(max {cap}): listed at {best.file}:{best_line}"
    for i in folded:
        result[i] = replace(findings[i], drop_reason=reason)


def apply_rule_cap(
    findings: Sequence[Finding],
    *,
    cap: int,
    confidence_floor: float | None,
    sweep_start: int,
) -> list[Finding]:
    """Fold the findings of one rule beyond ``cap`` onto the best one kept.

    Issue #18. The caller runs this pass only when a team rules file is
    loaded and ``PRXREF_MAX_FINDINGS_PER_RULE`` is above 0. A ``cap`` below
    1, or one that is not an ``int`` (a ``bool`` included), returns
    ``list(findings)`` unchanged without reading the environment.

    A finding is a candidate on exactly the terms of
    :func:`apply_rule_grouping`: it has no ``drop_reason``, its severity is
    in :data:`SEVERITIES` after trimming and lower-casing, its confidence is
    at or above the floor :func:`apply_quality_gate` would apply
    (``confidence_floor``, else ``PRXREF_CONFIDENCE_FLOOR``, else
    :data:`DEFAULT_CONFIDENCE_FLOOR`), and it sits on the chunk side of the
    list, before ``sweep_start``. Whole-PR sweep findings are never counted,
    capped or folded, and never absorb a folded finding. ``sweep_start``
    below zero is treated as zero (everything is sweep output); past the
    end, every finding is on the chunk side.

    Candidates count together when they name the same ``rule``, compared
    after whitespace collapsing and ``casefold()``, in ANY file: unlike
    :func:`apply_rule_grouping`, the file is not part of the key. A finding
    without a rule counts on its :func:`normalize_title` title instead, and
    never with a finding that has one. Scope is not part of the key.

    Only a key with more than ``cap`` candidates changes anything. Its
    candidates are ranked by severity (``error`` > ``warning`` > ``spec`` >
    ``outofscope``), then by :func:`finding_rank_key` (higher confidence,
    then file, line and normalized title), then by position. The first
    ``cap`` are kept with their own severity, confidence, title, rule and
    scope: nothing is promoted, and because severity ranks first an error
    is never folded under a warning. The rest are folded onto the
    first-ranked finding, the best, which gains:

    - ``locations``: its own ``locations``, plus each folded finding's
      ``(file, line)`` (``(file, 0)`` for a file-level one) and every entry
      of that finding's own ``locations``, once each, without the best's
      own ``(file, line)``, sorted by file then line.
    - a last body paragraph, after a blank line, that reads ``Also at:``
      followed by the first :data:`RULE_CAP_LISTED_LOCATIONS` of those
      locations as a backticked ``<file>:<line>`` (``<file>`` alone for
      line 0), comma-separated, then `` (+<k> more)`` when ``k`` of them
      are not named. The ``Also at:`` paragraph
      :func:`apply_rule_grouping` wrote for the best's own ``locations`` is
      replaced by it, never repeated. When no location is left, the body
      is unchanged.

    Every folded finding keeps its identity and gains ``drop_reason``
    ``rule cap exceeded (max <cap>): listed at <file>:<line>``
    (:data:`RULE_CAP_PREFIX`), naming the best's location. A folded group
    representative hands its group's lines to the best; its own
    ``grouped into <file>:<line>`` members keep their reason.

    Runs after :func:`apply_rule_grouping`, so a within-file group counts
    once, and before :func:`apply_quality_gate`, so the severity caps count
    what this cap kept. Pure apart from reading ``PRXREF_CONFIDENCE_FLOOR``
    when ``confidence_floor`` is ``None``; the result has the input's
    length and order, and a finding whose fields are not the documented
    types is passed through untouched rather than raising. The best of
    every key over the cap is a new object, even when no location was left
    to list; every other finding the pass does not fold is returned as the
    SAME object, so a caller can count rewrites by identity.
    """
    if not _rule_cap_active(cap):
        return list(findings)
    floor = _resolve_confidence_floor(confidence_floor)
    result = list(findings)
    for members in _rule_cap_groups(findings, floor, sweep_start).values():
        if len(members) > cap:
            _fold_over_cap(findings, members, cap, result)
    return result


def rule_cap_counts(
    findings: Sequence[Finding],
    *,
    cap: int,
    confidence_floor: float | None,
    sweep_start: int,
) -> list[dict]:
    """Tally, per cap key, how many findings :func:`apply_rule_cap` counts and keeps.

    Computed on the findings BEFORE the pass, over exactly the candidates
    and keys :func:`apply_rule_cap` uses (the same private helper groups and
    ranks both), so the two never disagree. One dict per key with at least
    two candidates, with keys in this order: ``rule`` (the first-ranked
    candidate's ``rule`` with its whitespace collapsed and its case kept,
    or, for a title key, its title with the whitespace collapsed), ``kind``
    (``"rule"`` or ``"title"``), ``total`` (the candidates) and ``kept``
    (``min(total, cap)``, or ``total`` when ``cap`` is below 1 or not an
    ``int``). Sorted by ``total`` descending, then ``kind`` (``"rule"``
    first), then the casefolded name, then the name. ``[]`` when no key
    reaches two. Pure apart from reading ``PRXREF_CONFIDENCE_FLOOR`` when
    ``confidence_floor`` is ``None``.
    """
    floor = _resolve_confidence_floor(confidence_floor)
    limited = _rule_cap_active(cap)
    rows: list[dict] = []
    for (kind, _label), members in _rule_cap_groups(findings, floor, sweep_start).items():
        total = len(members)
        if total < 2:
            continue
        first = findings[members[0]]
        name = " ".join((first.rule if kind == "rule" else first.title).split())
        rows.append({
            "rule": name,
            "kind": kind,
            "total": total,
            "kept": min(total, cap) if limited else total,
        })
    rows.sort(key=lambda row: (-row["total"], row["kind"], row["rule"].casefold(), row["rule"]))
    return rows


def apply_quality_gate(
    findings: Sequence[Finding],
    *,
    confidence_floor: float | None = None,
    max_errors: int | None = None,
    max_warning_findings: int | None = None,
    max_outofscope_findings: int | None = None,
) -> list[Finding]:
    """Filter findings through vocabulary, confidence, and per-severity caps.

    Order:
    1. Severity vocabulary: non-empty lowercase must be in
       {error, warning, spec, outofscope}; case-mismatches are normalized;
       invalid severities are dropped.
    2. Confidence floor: drop findings below the threshold (default 0.6).
    3. Error cap: among surviving errors, keep the top N ranked by
       :func:`finding_rank_key` and drop the rest
       (``error cap exceeded (max N)``), so ties are broken by content
       rather than by arrival order. ``max_errors`` falls back to
       ``PRXREF_MAX_ERROR_FINDINGS``, then to its legacy alias
       ``PRXREF_MAX_ERRORS``, and then to :data:`DEFAULT_MAX_ERRORS`.
    4. Warning and outofscope caps: ``max_warning_findings`` and
       ``max_outofscope_findings`` apply the same ranking to the surviving
       findings of their own severity, dropping the excess as
       ``warning cap exceeded (max N)`` and
       ``outofscope cap exceeded (max N)``. ``None`` (the default) means
       unlimited and reads no environment variable, so a call that omits
       both is identical to one without these caps; ``0`` drops every
       finding of that severity.

    Each cap counts only its own severity, and only findings that are still
    active after steps 1 and 2. ``spec`` findings are never capped and never
    count toward a cap: a spec-heavy review is neither crowded out by one
    nor crowding one out. A capped finding is kept with its ``drop_reason``
    set, never removed.

    A cap narrows ``PRXREF_FAIL_ON`` and never widens it: under ``any``, a
    cap of ``0`` removes that severity from the active findings, so a run
    whose only findings were of that severity exits 0 instead of 1.

    The returned list is sorted by :func:`finding_sort_key`.
    """
    floor = _resolve_confidence_floor(confidence_floor)
    cap = _resolve_max_errors(max_errors)

    staged: list[Finding] = []
    for f in findings:
        if f.drop_reason is not None:
            staged.append(f)
            continue

        raw_sev = (f.severity or "").strip().lower()
        if raw_sev not in SEVERITIES:
            staged.append(
                replace(f, drop_reason=f"invalid severity: {f.severity!r}")
            )
            continue

        norm = replace(f, severity=raw_sev) if raw_sev != f.severity else f

        conf = float(norm.confidence) if norm.confidence is not None else 0.0
        if conf < floor:
            staged.append(
                replace(
                    norm,
                    drop_reason=f"confidence {conf:.2f} below floor {floor:.2f}",
                )
            )
            continue

        staged.append(norm)

    _apply_severity_cap(staged, "error", cap)
    if max_warning_findings is not None:
        _apply_severity_cap(staged, "warning", max_warning_findings)
    if max_outofscope_findings is not None:
        _apply_severity_cap(staged, "outofscope", max_outofscope_findings)

    return sorted(staged, key=finding_sort_key)


def apply_severity_map(
    findings: Sequence[Finding], severity_map: Mapping[str, str],
) -> list[Finding]:
    """Rewrite a team severity word to the prxref severity it maps to.

    ``severity_map`` is the review rules' front-matter map, team word to
    prxref tier (``{"blocker": "error"}``). ``orchestrate_review`` runs this
    before every other pass, so a mapped word reaches the gate as its tier
    and an unmapped one still dies there as ``invalid severity``. Returns a
    new list of the same length and order, rewritten findings being
    :func:`dataclasses.replace` copies (every other field, ``scope``
    included, is kept); it drops nothing.

    A finding's severity matches a map word after ``strip()``, whitespace
    collapsing and ``casefold()`` on both sides, so ``" Must  FIX "`` meets
    ``must fix``. A finding that already carries a ``drop_reason``, one whose
    word is not in the map, and one that already names one of prxref's own
    :data:`SEVERITIES` pass through as the same object: the map translates
    team words only.
    """
    if not severity_map:
        return list(findings)
    table = {_severity_word(word): tier for word, tier in severity_map.items()}
    out: list[Finding] = []
    for f in findings:
        word = _severity_word(f.severity)
        tier = table.get(word)
        if f.drop_reason is not None or tier is None or word in SEVERITIES:
            out.append(f)
        else:
            out.append(replace(f, severity=tier))
    return out


def _severity_word(severity: object) -> str:
    return " ".join(severity.split()).casefold() if isinstance(severity, str) else ""


def apply_spec_grounding(
    findings: Sequence[Finding], *, grounded: bool,
) -> list[Finding]:
    """Relabel ``spec`` findings as ``warning`` on a run with no spec grounding.

    ``grounded`` says whether the run injected at least one spec constraint
    into the prompts (:func:`prxref.specs.constraint_count` of the digest is
    above 0). A run that injected none showed every review unit the no-specs
    text, so a ``spec`` finding there has no quoted constraint behind it: it
    is kept as a ``warning`` rather than posted under a label it has not
    earned or dropped along with whatever it found. The severity is compared
    after ``.strip().lower()``, so ``"SPEC"`` is relabelled too; every other
    severity is left exactly as written, so this pass never raises a finding
    to ``spec``. ``orchestrate_review`` runs it right after
    :func:`apply_severity_map` and before every other pass.

    Pure and order-preserving: returns a new list of the same length,
    rewritten findings being :func:`dataclasses.replace` copies, and
    already-dropped findings pass through untouched. Identity when
    ``grounded`` is true.
    """
    if grounded:
        return list(findings)
    return [
        replace(f, severity="warning")
        if f.drop_reason is None and (f.severity or "").strip().lower() == "spec"
        else f
        for f in findings
    ]


EXAMPLE_ECHO_PREFIX: str = "echoes the prompt's example: "

_EXAMPLE_FENCE_INFO: frozenset[str] = frozenset({"json", ""})
_EXAMPLE_TITLE_RE = re.compile(r'"title"\s*:\s*"((?:[^"\\\n]|\\.)*)"')


def prompt_example_titles(*templates: str) -> tuple[str, ...]:
    """The example-finding titles written into prompt templates, in first-seen order.

    Reads each template's fenced code blocks whose info string is ``json``
    (any case) or empty, and takes every ``"title": "<text>"`` string pair
    in them, JSON escapes decoded; a block of any other language (the
    worker's ``diff`` block) is never read. It scans rather than parses,
    because a packaged example is not valid JSON before rendering: the
    ``{scope_example}{rule_example}`` slots follow its last value. An empty
    title and a repeat are skipped. Pure; reads no file.
    """
    titles: list[str] = []
    for template in templates:
        for block in _fenced_blocks(template if isinstance(template, str) else ""):
            for raw in _EXAMPLE_TITLE_RE.findall(block):
                title = _json_string(raw)
                if title.strip() and title not in titles:
                    titles.append(title)
    return tuple(titles)


def _fenced_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    info: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        fence = line.strip().startswith("```")
        if info is None:
            if fence:
                info = line.strip()[3:].strip().casefold()
                body = []
        elif fence:
            if info in _EXAMPLE_FENCE_INFO:
                blocks.append("\n".join(body))
            info = None
        else:
            body.append(line)
    return blocks


def _json_string(raw: str) -> str:
    try:
        value = json.loads(f'"{raw}"')
    except ValueError:
        return raw
    return value if isinstance(value, str) else raw


def apply_example_echo_check(
    findings: Sequence[Finding], example_titles: Iterable[str],
) -> list[Finding]:
    """Drop a finding whose title repeats a prompt template's example finding.

    ``example_titles`` are the titles of the example findings in the
    templates the run's review units were shown (:func:`prompt_example_titles` of
    the worker and sweep templates in force, packaged or overridden). A
    finding whose :func:`normalize_title` equals one of theirs copied the
    output example instead of reporting a defect, and gains ``drop_reason``
    ``echoes the prompt's example: "<title>"``, naming the example's title
    as the template writes it. The match is exact after normalization, so
    a title that only resembles an example stays. Chunk and sweep findings
    are treated alike, against every title given.

    Pure and order-preserving: returns a new list of the same length,
    already-dropped findings and findings without a string title pass
    through untouched, and with no usable example title it drops nothing.
    """
    examples: dict[str, str] = {}
    for title in example_titles:
        key = normalize_title(title) if isinstance(title, str) else ""
        if key:
            examples.setdefault(key, title)
    out: list[Finding] = []
    for f in findings:
        example = (
            examples.get(normalize_title(f.title))
            if examples and f.drop_reason is None and isinstance(f.title, str)
            else None
        )
        out.append(
            f if example is None
            else replace(f, drop_reason=f'{EXAMPLE_ECHO_PREFIX}"{example}"')
        )
    return out
