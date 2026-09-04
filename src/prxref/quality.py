"""Deterministic quality passes over worker findings.

Six passes run before posting:
1. ``apply_location_validation``: drop findings whose ``file`` names no
   path of the parsed diff — an empty, non-path, or invented location is
   retained with ``drop_reason`` for the audit instead of rendering a
   bullet anchored to nothing.
2. ``apply_manifest_claim_check``: for findings on a ``package.json``,
   drop a claim whose named dependency is not the key on the anchored
   line (``anchor mismatch:``) or sits under a different dependency
   section than the claim asserts (``section mismatch:``). It runs
   BEFORE ``apply_line_align`` so it reads the model's raw anchor.
3. ``apply_line_align``: a line explicitly cited in the finding's own
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
4. ``apply_thread_dedup``: drop findings that duplicate an already-open
   or existing thread on the PR (path + line-window + shared distinctive tokens).
4. ``apply_settled_thread_suppression``: drop findings that re-litigate a
   subject an existing thread already argued out — same path plus shared
   distinctive tokens, with NO line test, because line alignment has already
   demoted a file-level finding to line 0 by this point.

5. ``apply_severity_consistency``: findings sharing one normalized title —
   within a file or across sibling files — are all raised to the group's
   maximum severity, so per-chunk workers cannot disagree about how
   serious the same pattern is. Findings phrased differently but bound
   by a shared rare code token, with a shared problem class or file,
   join the same group (issue #30).

5. ``apply_removal_claim_check``: drop findings claiming a path was
   removed or deleted when every path the claim names is still present in
   the diff's post-image — the false positive a ``copy from``/``copy to``
   header produces when a worker reads a copy as a move (issue #03).

5. ``apply_hedge_gate``: drop findings whose title or body conditions the
   defect on a precondition the worker never established from the diff
   ("If X still leases a client", "unless the backfill already ran"),
   with ``drop_reason`` ``hedged: "<matched span>"``.
6. ``apply_quality_gate``: drop findings below the confidence floor, cap
   errors per review, and enforce the {error, warning, outofscope} severity
   vocabulary.
6. ``apply_containment_note``: a finding that asserts a throw, panic,
   crash, or unhandled rejection and never names where it is caught or
   where it propagates to has its body suffixed with
   ``" [containment boundary not stated]"`` — a purely textual decoration,
   run on active and dropped findings alike, that never changes
   ``drop_reason`` or severity.

Every dropped finding retains its identity with ``drop_reason`` populated,
so review runstores and logs can explain every filter decision. Use
``active(findings)`` to obtain the subset that should actually post.
"""
from __future__ import annotations

import logging
import os
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from pathlib import PurePosixPath

from .forges.base import Thread
from .triage import DiffLine, FileDiff, Finding, Hunk

SEVERITIES: frozenset[str] = frozenset({"error", "warning", "outofscope"})

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

HEDGE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "if-still",
        re.compile(
            rf"\bif\b{_NO_SENTENCE_BREAK}{{0,80}}?\b(?:still|already|remains?)\b",
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


MANIFEST_BASENAME: str = "package.json"

DEPENDENCY_SECTIONS: tuple[str, ...] = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)

# Keys that name a manifest SECTION or the package itself rather than a
# dependency, so they can never be the "claimed package" of a finding.
_MANIFEST_NON_PACKAGE_KEYS: frozenset[str] = frozenset(
    [s.lower() for s in DEPENDENCY_SECTIONS] + ["scripts", "engines", "name", "version"]
)

# Evidence tokens a manifest section header contributes (including the
# camelCase split of the compound forms), which must not decide a
# package.json realign — see ``_realign_member``.
_MANIFEST_SECTION_TOKENS: frozenset[str] = frozenset(
    {"dependencies", "devdependencies", "peerdependencies",
     "optionaldependencies", "peer", "optional"}
)

_NPM_NAME_RE = re.compile(
    r"(?:@[a-z0-9\-~][a-z0-9._~\-]*/)?[a-z0-9\-~][a-z0-9._~\-]*"
)

_JSON_KEY_RE = re.compile(r'"([^"\n]+)"\s*:')

_SECTION_OPEN_RE = re.compile(
    r'"(dependencies|devDependencies|peerDependencies|optionalDependencies)"'
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


def _enclosing_section(hunks: Sequence[Hunk], line: int) -> str | None:
    """The dependency section header above ``line`` in the post-image."""
    for h in hunks:
        body = [ln for ln in h.lines if ln.kind != "-"]
        for i, ln in enumerate(body):
            if ln.new_line != line:
                continue
            for prev in reversed(body[: i + 1]):
                m = _SECTION_OPEN_RE.search(prev.text)
                if m is not None:
                    return m.group(1)
            return None
    return None


def apply_manifest_claim_check(
    findings: Sequence[Finding],
    files: Sequence[FileDiff],
) -> list[Finding]:
    """Drop package.json findings that misname their key or their section.

    A worker reading a manifest diff can name a real dependency and then
    anchor the comment on an unrelated neighbouring entry, or read an
    entry as a runtime dependency when the enclosing block is
    ``devDependencies``. Both are checkable against the diff itself: the
    claim names a package, the anchored line declares a key, and the
    nearest section header above that line names the block it lives in.

    A finding is dropped with ``drop_reason="anchor mismatch: claims
    <claimed> but line <n> is <anchor>"`` when the anchored line declares
    a different dependency, and with ``drop_reason="section mismatch:
    claims <claimed> but <key> is under <actual>"`` when the anchor is
    right but the asserted section is not. Findings on other files, on a
    package.json whose claim names no key of the diff, and findings that
    already carry a ``drop_reason`` pass through untouched. Order is
    preserved.

    Run this BEFORE :func:`apply_line_align`: it must read the model's
    raw anchor, because realignment can move a correctly anchored claim
    onto the very line this pass exists to catch.
    """
    hunks_by_file = {
        f.path: f.hunks
        for f in files
        if PurePosixPath(f.path).name == MANIFEST_BASENAME
    }
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
        actual_section = _enclosing_section(hunks, f.line)
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

    On a ``package.json`` the manifest section words are dropped from the
    evidence, because every dependency claim mentions one and the long
    ``dependencies`` token would otherwise outrank the package name and
    resolve the anchor onto a section header instead of the entry.
    """
    ftoks = _evidence_tokens(f"{finding.title} {finding.body}")
    if PurePosixPath(finding.file).name == MANIFEST_BASENAME:
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
    reviewers made with more context than the review has. Order-preserving,
    pure, and already-dropped findings pass through untouched so the reason
    an earlier pass gave survives.
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


_SEVERITY_RANK: dict[str, int] = {"error": 0, "warning": 1, "outofscope": 2}

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
    findings: Sequence[Finding], sweep_start: int
) -> list[Finding]:
    """Drop systemic-sweep findings that restate a chunk finding.

    ``findings[sweep_start:]`` came from the whole-PR systemic sweep; the
    entries before it came from the per-chunk workers. A sweep finding whose
    (file, :func:`normalize_title` title) pair matches a surviving chunk
    finding adds no recall — it is the same pattern the chunk seat already
    reported — and is dropped with ``drop_reason="duplicate of chunk
    finding"``, the same retained-not-silenced convention every other pass
    uses. Chunk findings are never dropped by this pass, even when the sweep
    phrased the pattern first: the chunk seat cited the exact line.

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
    all three group). Each component is rewritten to its highest
    severity (error > warning > outofscope). A rewritten finding keeps
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
    basename-with-parent-directory (``servicenow/package.json``), which is
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


def apply_removal_claim_check(
    findings: Sequence[Finding], files: Sequence[FileDiff]
) -> list[Finding]:
    """Drop removal claims the diff's post-image contradicts.

    A finding whose title or body asserts a file was removed, deleted, or
    no longer exists is dropped when every path it names is still present
    after the PR lands. A copy's source is present unless a separate
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
        # Only a claim that NAMES a path is judged. Falling back to the
        # finding's anchor file would drop every finding whose body merely
        # says something was "removed" — a guard, a constant, a validator —
        # on a file the PR still leaves in place, which is precisely the
        # guard-removal class the systemic sweep exists to report.
        named = _claimed_paths(f, candidates)
        if named and all(path in present for path in named):
            out.append(
                replace(
                    f,
                    drop_reason=(
                        "claims removal of a path present in the "
                        f"post-image: {named[0]}"
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


def _hedge_span(text: str) -> str | None:
    for _name, pattern in HEDGE_RULES:
        m = pattern.search(text)
        if m is None:
            continue
        span = m.group(0).strip(" \t\n,;.")
        return span[:_HEDGE_SPAN_MAX]
    return None


def apply_hedge_gate(findings: Sequence[Finding]) -> list[Finding]:
    """Drop findings whose own text conditions the defect on an unverified fact.

    A hedged finding ("If figmaProxy.prepare still leases a client", "If they
    are members of the root workspaces globs") states a precondition the
    worker had the whole diff to check and did not. Each rule in
    ``HEDGE_RULES`` is anchored on an epistemic marker rather than a bare
    ``if``/``may``/``unless``, which appear throughout legitimate findings.

    Pure and order-preserving: already-dropped findings pass through
    untouched, and a match sets ``drop_reason`` to ``hedged: "<span>"``
    naming the matched text so the drop is auditable in the run record.
    """
    out: list[Finding] = []
    for f in findings:
        if f.drop_reason is not None:
            out.append(f)
            continue
        span = _hedge_span(f.title or "") or _hedge_span(f.body or "")
        if span is None:
            out.append(f)
            continue
        out.append(replace(f, drop_reason=f'hedged: "{span}"'))
    return out


def apply_quality_gate(
    findings: Sequence[Finding],
    *,
    confidence_floor: float | None = None,
    max_errors: int | None = None,
) -> list[Finding]:
    """Filter findings through vocabulary, confidence, and per-review error caps.

    Order:
    1. Severity vocabulary: non-empty lowercase must be in {error, warning, note};
       case-mismatches are normalized; invalid severities are dropped.
    2. Confidence floor: drop findings below the threshold (default 0.6).
    3. Error cap: among surviving errors, keep the top N ranked by
       :func:`finding_rank_key` and drop the rest, so ties are broken by
       content rather than by arrival order.

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

    active_error_indices: list[int] = [
        i for i, f in enumerate(staged)
        if f.drop_reason is None and f.severity == "error"
    ]

    if len(active_error_indices) > cap:
        ranked = sorted(
            active_error_indices,
            key=lambda idx: finding_rank_key(staged[idx]),
        )
        for dropped_idx in ranked[cap:]:
            staged[dropped_idx] = replace(
                staged[dropped_idx],
                drop_reason=f"error cap exceeded (max {cap})",
            )

    return sorted(staged, key=finding_sort_key)
