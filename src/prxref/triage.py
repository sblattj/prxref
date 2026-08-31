"""Unified-diff parsing, file risk scoring, and chunking for wide PRs.

All three forges deliver the same artifact — a raw git-style unified diff —
so this module is the single parser everything downstream codes against.
No forge imports, no graph/embedding dependency: chunking groups by
directory proximity plus risk instead of call-graph connectivity.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

# Per-chunk token budget used when no caller supplies one. Named so config and
# the orchestrator can share this single literal instead of restating it;
# PRXREF_CHUNK_TOKEN_BUDGET overrides it.
DEFAULT_TOKEN_BUDGET: int = 25_000

# Cap on files placed in one review chunk, for the same shared-literal reason
# as the budget above; PRXREF_CHUNK_MAX_FILES overrides it. The max_chunks
# overflow branch may exceed the cap rather than drop a file from review.
DEFAULT_MAX_FILES_PER_CHUNK: int = 5

# Context lines kept around each change when a chunk's diff is re-rendered
# for the worker prompt; PRXREF_CHUNK_CONTEXT_LINES overrides it. The forge's
# diff is the only source of context: this trims what was received, never
# adds what it did not.
DEFAULT_CONTEXT_LINES: int = 3


@dataclass
class Finding:
    """One review finding, the unit every downstream seat shares.

    ``line`` semantics: 1-based new-file line number after
    ``quality.apply_line_align`` snapping; 0 means file-level.
    ``drop_reason`` is set by quality passes instead of the finding being
    silently discarded, so run records can explain every drop.
    """

    file: str
    line: int
    severity: str
    confidence: float
    title: str
    body: str
    drop_reason: str | None = None


@dataclass
class DiffLine:
    """One line of a hunk body."""

    kind: str  # "+" | "-" | " "
    text: str
    old_line: int | None = None
    new_line: int | None = None


@dataclass
class Hunk:
    """One @@ hunk with resolved old/new line numbering."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[DiffLine] = field(default_factory=list)


@dataclass
class FileDiff:
    """One file's parsed diff: identity, status, and hunks."""

    path: str
    old_path: str | None
    new_path: str | None
    status: str = "modified"  # added | modified | removed | renamed
    is_binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def lines_added(self) -> int:
        return sum(1 for h in self.hunks for ln in h.lines if ln.kind == "+")

    @property
    def lines_removed(self) -> int:
        return sum(1 for h in self.hunks for ln in h.lines if ln.kind == "-")

    @property
    def added_lines(self) -> set[int]:
        """New-file line numbers of every ``+`` line across all hunks."""
        return {
            ln.new_line
            for h in self.hunks
            for ln in h.lines
            if ln.kind == "+" and ln.new_line is not None
        }


# Bitbucket Server / Data Center spells its diff prefixes ``src://`` and
# ``dst://`` where git uses ``a/`` and ``b/``. Both spellings have to be
# understood in two places -- the ``diff --git`` header and the ``---``/``+++``
# operands -- or Server paths keep the prefix and every reported location reads
# ``dst://pkg/mod.py`` instead of ``pkg/mod.py``.
_DIFF_GIT_RES = (
    re.compile(r'^diff --git a/(.*?) b/(.*)$'),
    re.compile(r'^diff --git src://(.*?) dst://(.*)$'),
)
_PATH_PREFIXES = ("a/", "b/", "src://", "dst://")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_RENAME_FROM_RE = re.compile(r"^rename from (.+)$")
_RENAME_TO_RE = re.compile(r"^rename to (.+)$")


def _match_diff_git(line: str) -> re.Match | None:
    """Match a ``diff --git`` header in either the git or Bitbucket Server form."""
    for pattern in _DIFF_GIT_RES:
        m = pattern.match(line)
        if m:
            return m
    return None


def _clean_path(raw: str) -> str | None:
    """Normalize a ``---``/``+++`` path operand: strip quotes, timestamps,
    and the a//b/ or src:////dst:// prefix; map /dev/null to None."""
    rest = raw.split("\t")[0].rstrip("\r")
    if len(rest) >= 2 and rest.startswith('"') and rest.endswith('"'):
        rest = rest[1:-1]
    if rest == "/dev/null":
        return None
    for prefix in _PATH_PREFIXES:
        if rest.startswith(prefix):
            return rest[len(prefix):]
    return rest


def _parse_hunk_body(diff_lines: list[str], i: int, hunk: Hunk) -> int:
    """Consume exactly old_count+new_count body lines starting at index i.

    Counting by the @@ header's own numbers (not by guessing prefixes)
    disambiguates body lines that literally start with ``---``/``+++``/``@@``.
    Returns the index of the first line after the hunk.
    """
    remaining_old, remaining_new = hunk.old_count, hunk.new_count
    old_no, new_no = hunk.old_start, hunk.new_start

    while (remaining_old > 0 or remaining_new > 0) and i < len(diff_lines):
        line = diff_lines[i]
        if line.startswith("\\"):
            i += 1
            continue
        first = line[0] if line else " "
        if first == "+" and remaining_new > 0:
            hunk.lines.append(DiffLine("+", line[1:], new_line=new_no))
            new_no += 1
            remaining_new -= 1
        elif first == "-" and remaining_old > 0:
            hunk.lines.append(DiffLine("-", line[1:], old_line=old_no))
            old_no += 1
            remaining_old -= 1
        else:
            hunk.lines.append(
                DiffLine(" ", line[1:] if first == " " else line,
                         old_line=old_no, new_line=new_no)
            )
            old_no += 1
            new_no += 1
            remaining_old -= 1
            remaining_new -= 1
        i += 1

    while i < len(diff_lines) and diff_lines[i].startswith("\\"):
        i += 1

    return i


def parse_unified_diff(diff: str) -> list[FileDiff]:
    """Parse a raw unified diff into per-file FileDiff records.

    Handles added/deleted/renamed files (via /dev/null operands and git
    extended headers), binary files (marked ``is_binary``, no hunks), and
    hunks whose body lines themselves begin with ``+``/``-``/``@@``.
    Non-git fragments lacking a ``---``/``+++`` header are ignored.
    """
    files: list[FileDiff] = []
    lines = diff.splitlines()

    pending_old: str | None = None
    have_pending_old = False
    cur: FileDiff | None = None
    rename_hint = False
    added_hint = False
    removed_hint = False

    def close_current() -> None:
        nonlocal cur
        if cur is None:
            return
        if rename_hint:
            cur.status = "renamed"
        elif added_hint:
            cur.status = "added"
        elif removed_hint:
            cur.status = "removed"
        elif cur.old_path is None:
            cur.status = "added"
        elif cur.new_path is None:
            cur.status = "removed"
        elif cur.old_path != cur.new_path:
            cur.status = "renamed"
        else:
            cur.status = "modified"
        files.append(cur)
        cur = None

    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("diff --git "):
            close_current()
            m = _match_diff_git(line)
            old = m.group(1) if m else None
            new = m.group(2) if m else None
            cur = FileDiff(path=new or old or "", old_path=old, new_path=new)
            rename_hint = added_hint = removed_hint = False
            i += 1
            continue

        if line.startswith("--- "):
            pending_old = _clean_path(line[4:])
            have_pending_old = True
            i += 1
            continue

        if line.startswith("+++ "):
            new_path = _clean_path(line[4:])
            if cur is None:
                cur = FileDiff(
                    path=new_path or pending_old or "",
                    old_path=pending_old,
                    new_path=new_path,
                )
            else:
                if have_pending_old:
                    cur.old_path = pending_old
                cur.new_path = new_path
                cur.path = new_path or cur.old_path or cur.path
            pending_old = None
            have_pending_old = False
            i += 1
            continue

        m = _HUNK_RE.match(line)
        if m and cur is not None and not cur.is_binary:
            hunk = Hunk(
                old_start=int(m.group(1)),
                old_count=int(m.group(2)) if m.group(2) is not None else 1,
                new_start=int(m.group(3)),
                new_count=int(m.group(4)) if m.group(4) is not None else 1,
            )
            i = _parse_hunk_body(lines, i + 1, hunk)
            cur.hunks.append(hunk)
            continue

        if line.startswith("Binary files ") or line == "GIT binary patch":
            if cur is not None:
                cur.is_binary = True
                cur.hunks = []
            i += 1
            continue

        if line.startswith("rename from ") and cur is not None:
            cur.old_path = line[len("rename from "):]
            rename_hint = True
            i += 1
            continue
        if line.startswith("rename to ") and cur is not None:
            cur.new_path = line[len("rename to "):]
            cur.path = cur.new_path
            rename_hint = True
            i += 1
            continue
        if line.startswith("new file mode"):
            added_hint = True
            i += 1
            continue
        if line.startswith("deleted file mode"):
            removed_hint = True
            i += 1
            continue

        i += 1

    close_current()
    return files


def added_lines_by_file(files: list[FileDiff]) -> dict[str, set[int]]:
    """Map each file path to its set of ``+`` new-file line numbers."""
    return {f.path: f.added_lines for f in files if f.added_lines}


FILE_PENALTIES: list[tuple[str, int]] = [
    (r"(\.lock|-lock\.(json|yaml|yml)|\.snap|\.min\.js|\.map)$", -65),
    (r"(^|/)tests?/", -10),
    (r"\.(md|txt|rst|yml|yaml|toml|json|cfg|ini)$", -15),
    (r"__snapshots__/", -50),
    (r"\.(generated|auto)\.", -50),
    (r"\.(idea|vscode)/", -50),
]


def score_file(file: FileDiff, churn: int = 0) -> float:
    """Score a changed file by review risk (0-100).

    Weights: lines_changed=70%, churn=30%, then file-type penalties.
    (pr-sentinel split weight across graph impact-radius and caller-count;
    prxref has no graph, so line and churn weight is redistributed.)
    """
    lines = file.lines_added + file.lines_removed
    lines_score = min(lines / 5.0, 100)
    churn_score = min(churn * 2, 100)
    raw = lines_score * 0.70 + churn_score * 0.30

    penalty = 0
    for pattern, pen in FILE_PENALTIES:
        if re.search(pattern, file.path):
            penalty += pen

    return max(0, min(100, raw + penalty))


def _shared_dir_depth(p1: str, p2: str) -> int:
    """Count leading directory components two paths have in common."""
    d1 = PurePosixPath(p1).parts[:-1]
    d2 = PurePosixPath(p2).parts[:-1]
    depth = 0
    for a, b in zip(d1, d2, strict=False):
        if a != b:
            break
        depth += 1
    return depth


def trim_hunk_context(hunk: Hunk, context_lines: int) -> Hunk:
    """Rebuild ``hunk`` keeping at most ``context_lines`` around each change.

    A context line survives iff it lies within ``context_lines`` of a ``+``
    or ``-`` line — the same rule ``git diff -U<n>`` applies — so a short run
    of context between two changes stays whole and a long one is cut back to
    N on each side. ``0`` emits the changed lines only; a hunk with no
    change lines is returned unchanged.

    The result is a new Hunk whose ``old_start``/``new_start`` and counts
    describe the kept lines, so rendering it yields a self-consistent
    unified diff. The input is never mutated: chunking, quality, and line
    alignment all keep working from the full parse.

    Context can only be trimmed, never added — the forge's diff is the only
    source — so a forge that sent -U3 through ``context_lines=3`` renders
    byte-identical.
    """
    radius = max(0, context_lines)
    change_at = [i for i, ln in enumerate(hunk.lines) if ln.kind != " "]
    if not change_at:
        return hunk
    kept = [
        (i, ln)
        for i, ln in enumerate(hunk.lines)
        if ln.kind != " " or any(abs(i - j) <= radius for j in change_at)
    ]
    cut = kept[0][0]
    lines = [ln for _, ln in kept]
    return Hunk(
        old_start=hunk.old_start + sum(
            1 for ln in hunk.lines[:cut] if ln.kind != "+"
        ),
        new_start=hunk.new_start + sum(
            1 for ln in hunk.lines[:cut] if ln.kind != "-"
        ),
        old_count=sum(1 for ln in lines if ln.kind != "+"),
        new_count=sum(1 for ln in lines if ln.kind != "-"),
        lines=lines,
    )


def build_chunks(
    files: list[FileDiff],
    max_chunks: int = 8,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    churn_by_path: dict[str, int] | None = None,
    max_files_per_chunk: int = DEFAULT_MAX_FILES_PER_CHUNK,
) -> list[list[FileDiff]]:
    """Group files into review chunks (~token_budget tokens, ≤max_chunks).

    Files are placed risk-first (score_file, descending) into the chunk
    with the highest directory-proximity affinity that still fits the
    budget and holds fewer than max_files_per_chunk files; otherwise a new
    chunk opens, and once max_chunks is reached overflow goes to the
    smallest chunk. Binary files are skipped — there is no reviewable text.

    The file cap shapes placement like the budget does. Once max_chunks is
    reached and every chunk is at the cap, an overflow file joins the
    smallest chunk past the cap rather than being dropped: review coverage
    is the invariant, both caps are preferences.
    """
    candidates = [f for f in files if not f.is_binary]
    if not candidates:
        return []

    churn = churn_by_path or {}
    sorted_files = sorted(
        candidates, key=lambda f: score_file(f, churn.get(f.path, 0)), reverse=True
    )

    def est_tokens(f: FileDiff) -> int:
        return (f.lines_added + f.lines_removed) * 40

    chunks: list[list[FileDiff]] = []
    chunk_tokens: list[int] = []

    for f in sorted_files:
        ftokens = est_tokens(f)
        best_chunk = -1
        best_affinity = -1

        for idx, chunk in enumerate(chunks):
            if chunk_tokens[idx] + ftokens > token_budget:
                continue
            if len(chunk) >= max_files_per_chunk:
                continue
            affinity = max(_shared_dir_depth(f.path, cf.path) for cf in chunk)
            if affinity > best_affinity:
                best_affinity = affinity
                best_chunk = idx

        if best_chunk >= 0:
            chunks[best_chunk].append(f)
            chunk_tokens[best_chunk] += ftokens
        elif len(chunks) < max_chunks:
            chunks.append([f])
            chunk_tokens.append(ftokens)
        else:
            smallest = min(range(len(chunks)), key=lambda k: chunk_tokens[k])
            chunks[smallest].append(f)
            chunk_tokens[smallest] += ftokens

    return chunks
