"""Deterministic quality passes over worker findings.

Three passes run before posting:
1. ``apply_line_align``: snap each finding's cited line to the nearest
   actual ``+`` line in that file's diff (within tolerance, else line=0).
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
from .triage import Finding

SEVERITIES: frozenset[str] = frozenset({"error", "warning", "outofscope"})

DEFAULT_CONFIDENCE_FLOOR: float = 0.6
DEFAULT_MAX_ERRORS: int = 10


def active(findings: Sequence[Finding]) -> list[Finding]:
    """Return only the findings that survived every quality pass."""
    return [f for f in findings if f.drop_reason is None]


def snap_line(line: int, added: set[int], tolerance: int = 3) -> int:
    """Snap a cited line to the nearest ``+`` line within tolerance, else 0.

    A returned 0 denotes a file-level anchor (no ``+`` line close enough).
    """
    if line in added:
        return line
    if not added:
        return 0
    nearest = min(added, key=lambda ln: (abs(ln - line), ln))
    if abs(nearest - line) <= tolerance:
        return nearest
    return 0


def apply_line_align(
    findings: Sequence[Finding],
    added_lines_by_file: dict[str, set[int]] | None = None,
    tolerance: int = 3,
) -> list[Finding]:
    """Snap each finding's line number to the nearest ``+`` line in that file.

    When ``added_lines_by_file`` is omitted or has no entry for a file,
    the finding's line is dropped to 0 (file-level).
    """
    by_file = added_lines_by_file or {}
    result: list[Finding] = []
    for f in findings:
        added = by_file.get(f.file, set())
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
