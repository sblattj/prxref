"""Deterministic quality passes over worker findings.

Three passes run before posting:
1. ``apply_line_align``: snap each finding's cited line to the nearest
   actual ``+`` line in that file's diff (within tolerance, else line=0),
   then corroborate exact ``+``-line members against the file's hunks by
   content, so an anchor that is a valid added line of the WRONG hunk
   (issue #19's drift shape) is re-resolved or demoted to file-level
   instead of posting at a wrong position.
2. ``apply_thread_dedup``: drop findings that duplicate an already-open
   or existing thread on the PR (path + line-window + shared distinctive tokens).
3. ``apply_quality_gate``: drop findings below the confidence floor, cap
   errors per review, and enforce the {error, warning, note} severity vocabulary.

Every dropped finding retains its identity with ``drop_reason`` populated,
so review runstores and logs can explain every filter decision. Use
``active(findings)`` to obtain the subset that should actually post.
"""
from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import replace

from .forges.base import Thread
from .triage import FileDiff, Finding, Hunk

SEVERITIES: frozenset[str] = frozenset({"error", "warning", "outofscope"})

DEFAULT_CONFIDENCE_FLOOR: float = 0.6
DEFAULT_MAX_ERRORS: int = 10

# Positional snap radius for cited lines. Inline comment cards render only a
# handful of surrounding lines, so a nudge of up to 5 keeps the cited code
# visible in the posted comment; past that the comment separates from its
# evidence. 5 also sits below the smallest drift the issue #19 audit measured
# on real reviews (10 lines), so no observed-failure distance survives.
DEFAULT_LINE_TOLERANCE: int = 5


def active(findings: Sequence[Finding]) -> list[Finding]:
    """Return only the findings that survived every quality pass."""
    return [f for f in findings if f.drop_reason is None]


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


def _hunk_text(hunk: Hunk) -> str:
    return "\n".join(ln.text for ln in hunk.lines)


def _hunk_containing(hunks: Sequence[Hunk], line: int) -> Hunk | None:
    """The hunk whose new-file span holds ``line``, or None."""
    for h in hunks:
        if h.new_start <= line < h.new_start + h.new_count:
            return h
    return None


def _realign_member(finding: Finding, hunks: Sequence[Hunk]) -> int:
    """Re-resolve an exact ``+``-line member anchor by content, or keep it.

    Membership alone cannot catch issue #19's drift: a model that adds the
    wrong hunk's ``@@`` start to an in-hunk offset emits a number that IS a
    valid added line, of the wrong hunk. The one checkable property left is
    content correspondence — the finding's own evidence tokens (title +
    body, minus stopwords) must occur in the anchor hunk's text. Zero
    overlap refutes the anchor: it re-anchors to the best token-matching
    added line elsewhere in the file (context lines score too; the target
    is the nearest ``+`` line to the best match), or drops to file-level 0
    when no line matches anywhere. Non-empty overlap corroborates the
    anchor and the citation stands.
    """
    ftoks = _tokens(f"{finding.title} {finding.body}")
    if not ftoks:
        return finding.line
    anchor = _hunk_containing(hunks, finding.line)
    if anchor is not None and ftoks & _tokens(_hunk_text(anchor)):
        return finding.line
    best_line, best_key = 0, (0, 0)
    for h in hunks:
        if h is anchor:
            continue
        scored = [
            (len(ftoks & _tokens(ln.text)), -abs((ln.new_line or 0) - finding.line), ln)
            for ln in h.lines
            if ln.kind != "-" and ln.new_line is not None
        ]
        scored = [s for s in scored if s[0] > 0]
        if not scored:
            continue
        _, _, best = max(scored, key=lambda s: (s[0], s[1]))
        if best.kind == "+":
            target = best
        else:
            adds = [
                ln for ln in h.lines
                if ln.kind == "+" and ln.new_line is not None
            ]
            if not adds:
                continue
            target = min(
                adds, key=lambda a: abs((a.new_line or 0) - (best.new_line or 0))
            )
        key = (
            len(ftoks & _tokens(best.text)),
            -abs((target.new_line or 0) - finding.line),
        )
        if key > best_key:
            best_key, best_line = key, target.new_line or 0
    return best_line


def apply_line_align(
    findings: Sequence[Finding],
    added_lines_by_file: dict[str, set[int]] | None = None,
    tolerance: int = DEFAULT_LINE_TOLERANCE,
    files: Sequence[FileDiff] | None = None,
) -> list[Finding]:
    """Snap each finding's line to a defensible anchor in that file's diff.

    Positional pass: within ``tolerance`` of the nearest ``+`` line the
    citation snaps there; beyond it the citation is not trusted and drops
    to file-level (0) — the summary still carries the finding, at an
    honest location. Content pass: an exact ``+``-line member is
    corroborated against the file's hunks when ``files`` is supplied
    (see :func:`_realign_member`) — that is the only way a wrong-hunk
    citation becomes visible, and it is corrected or demoted instead of
    posted at a wrong position.

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
        if hunks and f.line > 0 and f.line in added:
            new_line = _realign_member(f, hunks)
        else:
            new_line = snap_line(f.line, added, tolerance=tolerance)
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


def _tokens(text: str) -> set[str]:
    return {
        t.lower()
        for t in _TOKEN_RE.findall(text)
        if t.lower() not in _STOPWORDS
    }


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
    3. Error cap: among surviving errors, keep the top N by confidence and drop
       the rest.
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
            key=lambda idx: (float(staged[idx].confidence or 0.0)),
            reverse=True,
        )
        for dropped_idx in ranked[cap:]:
            staged[dropped_idx] = replace(
                staged[dropped_idx],
                drop_reason=f"error cap exceeded (max {cap})",
            )

    return staged
