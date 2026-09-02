"""Deterministic quality passes over worker findings.

Four passes run before posting:
1. ``apply_line_align``: snap each finding's cited line to the nearest
   actual ``+`` line in that file's diff (within tolerance, else line=0),
   then corroborate exact ``+``-line members against the file's hunks by
   content, so an anchor that is a valid added line of the WRONG hunk
   (issue #19's drift shape) is re-resolved or demoted to file-level
   instead of posting at a wrong position. Corroboration is line-level:
   an anchor survives only when it ties the file's best evidence match
   or sits within tolerance of it, and a blank or pure-punctuation
   anchor never survives while any token-bearing added line exists.
2. ``apply_thread_dedup``: drop findings that duplicate an already-open
   or existing thread on the PR (path + line-window + shared distinctive tokens).
3. ``apply_severity_consistency``: findings sharing one normalized title —
   within a file or across sibling files — are all raised to the group's
   maximum severity, so per-chunk workers cannot disagree about how
   serious the same pattern is.
4. ``apply_quality_gate``: drop findings below the confidence floor, cap
   errors per review, and enforce the {error, warning, outofscope} severity
   vocabulary.
5. ``apply_sweep_dedup`` (LAST, after the gate): drop systemic-sweep
   findings that restate a chunk finding that SURVIVED the gate — same
   file, same normalized title (the #18 grouping). Running the pass after
   the gate is what keeps a sub-floor chunk finding from suppressing its
   higher-confidence sweep duplicate and then dying at the gate itself,
   which would lose exactly the recall the sweep adds.

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
    """
    ftoks = _evidence_tokens(f"{finding.title} {finding.body}")
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


def apply_severity_consistency(findings: Sequence[Finding]) -> list[Finding]:
    """Raise every finding in a normalized-title group to the group's max severity.

    Per-chunk workers decide severity in isolation, so the same pattern can
    surface as an error in one file and a warning in a sibling, or as a
    warning at one anchor and a note at another in the same file. Findings
    sharing one :func:`normalize_title` form — within a file or across
    files — are all rewritten to the group's highest severity
    (error > warning > outofscope). A rewritten finding keeps its own file,
    line, body, and confidence; only severity changes. Findings carrying a
    ``drop_reason`` or a severity outside the vocabulary pass through
    untouched.
    """
    group_max: dict[str, str] = {}
    for f in findings:
        if f.drop_reason is not None:
            continue
        severity = (f.severity or "").strip().lower()
        if severity not in _SEVERITY_RANK:
            continue
        key = normalize_title(f.title)
        current = group_max.get(key)
        if current is None or _SEVERITY_RANK[severity] < _SEVERITY_RANK[current]:
            group_max[key] = severity

    result: list[Finding] = []
    for f in findings:
        severity = (f.severity or "").strip().lower()
        if f.drop_reason is None and severity in _SEVERITY_RANK:
            target = group_max.get(normalize_title(f.title), severity)
            if severity != target:
                result.append(replace(f, severity=target))
                continue
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
