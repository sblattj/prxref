"""Systemic-sweep digest: a compact, deterministic view of a whole PR diff.

Per-chunk workers each see one slice of the diff, so patterns that only look
wrong in aggregate — an unauthenticated handler, a secret in a client-exposed
constant, a migration with no policy — have no seat that sees enough to name
them. The systemic sweep spends ONE extra single-shot review call over a
digest of the whole PR, and this module builds that digest deterministically.
Grep-like on purpose — no model in the loop — so the same diff always digests
to the same text.

Three kinds of content reach the sweep, each closing a measured miss class:

- Pattern-matched lines (always): the added/removed lines that hit a curated
  high-signal pattern, per file. When a file exceeds the per-file cap, the
  security-critical classes (entry points, secrets, auth checks) are admitted
  FIRST so noisier matches — logging above all — cannot fill the cap and
  push a secret-bearing line into the omission note.
- Full added content: a file whose added content is short enough that absence
  is evidence — any file the migration-ddl pattern touches (an RLS-less
  ``CREATE TABLE`` is only visible when the whole migration is), and any file
  under :data:`FULL_CONTENT_MAX_ADDED_LINES` added lines (loop bodies, config
  keys, and store files match few or no patterns yet carry systemic bugs).
- Repo-config notes: a lockfile newly added while another lockfile or a
  ``packageManager`` pin also appears in the diff is stated as one synthetic
  ``! repo-config:`` line — a dual-lockfile adoption is invisible to any
  line pattern because the conflicting file is often not in the diff at all.

Two caps bound the output, both deterministic: the per-file matched-line cap
(pattern mode only) and the overall ``token_budget`` on a 4-chars-per-token
estimate, announced with :data:`TRUNCATION_MARKER`. Full content is charged
against a bounded share of that budget (:data:`FULL_CONTENT_BUDGET_SHARE`)
so a PR of many small files cannot crowd out the pattern coverage of the
rest; a file that does not fit degrades to pattern lines and says so.
"""
from __future__ import annotations

import re

from .triage import DiffLine, FileDiff

# At most this many matched lines from ONE file enter the digest in pattern
# mode, so a single noisy file (a minified bundle, a logging-heavy script)
# cannot eat the whole budget before the sweep has seen the rest of the PR.
# Per-file, not global: a file that trips the cap keeps its highest-priority
# hits and an explicit omission note, never a silent cut.
MAX_LINES_PER_FILE = 40

# A file with at most this many added lines is rendered in FULL — every added
# line, matched or not. Below this size, absence of a line is evidence: a 7-line
# migration with no RLS statement, a store file whose poll loop has no attempt
# cap, a package.json diff. Above it, pattern matching applies (a lockfile's
# 12k lines must never enter whole).
FULL_CONTENT_MAX_ADDED_LINES = 60

# Migration files get the same whole-file treatment up to a larger bound,
# because "this migration never enables RLS or creates a policy" is exactly
# the negative the sweep exists to assert and migrations are short. Past the
# bound the file degrades to pattern lines with an omission note.
MIGRATION_FULL_CONTENT_MAX_ADDED_LINES = 200

# The share of the overall digest budget reserved for full-content bodies of
# small (non-migration) files. A PR of many tiny files can therefore never
# crowd out pattern coverage of the big ones: once the share is spent, later
# eligible files degrade to pattern lines and the digest says so.
FULL_CONTENT_BUDGET_SHARE = 0.3

# The digest budget is an upper bound on prompt size, not a billing figure;
# the ~4-chars-per-token estimate is the same order-of-magnitude shortcut
# build_chunks applies (40 tokens per changed code line).
_CHARS_PER_TOKEN = 4

# Truncation is announced in the digest itself so the model knows the text
# ends early, and in the returned marker so tests can pin the behaviour.
TRUNCATION_MARKER = "[digest truncated: token budget reached]"

# A file that qualifies for full content but does not fit degrades to pattern
# lines and announces the omission rather than silently shrinking.
_FULL_CONTENT_NOTE = "[full content omitted: {reason}]"

# Pattern classes whose lines survive the per-file cap ahead of everything
# else. A secret or entry point buried at match #41 of a logging-heavy file
# is the miss that motivates this: without the buckets, the first 40
# console.log lines consume the cap and the credential line is counted as
# omitted. Within each bucket, diff order is preserved.
_MUST_SEE_CLASSES = frozenset({"entry-point", "secret", "auth-check"})

# Lockfile basenames whose coexistence in one diff signals repo-config drift.
_LOCKFILE_BASENAMES = frozenset({
    "package-lock.json",
    "npm-shrinkwrap.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "bun.lock",
})

_MANIFEST_BASENAME = "package.json"

# One pattern class per systemic family the sweep prompt hunts for. Order is
# the report order of :func:`match_class` when a line matches several —
# entry-point first, because a line that both defines a handler and reads an
# env var is best explained as a handler. repo-config and loop-timer sit
# last: they label lines no earlier class wants, and a line that matches both
# (a packageManager pin inside a polling setup) is more useful as repo-config.
_DIGEST_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "entry-point",
        re.compile(
            r"export\s+(?:default\s+)?(?:async\s+)?function"
            r"|\bdef\s+(?:handler|lambda_handler|view|on_request)\b"
            r"|@(?:\w+[._])?(?:route|get|post|put|patch|delete|use|api_view)\b"
            r"|\badd_action\s*\(\s*['\"]wp_ajax_"
            r"|\badd_action\b|\badd_filter\b"
            r"|\b(?:app|router)\.(?:get|post|put|patch|delete|all)\s*\("
        ),
    ),
    (
        "secret",
        re.compile(
            r"\bprocess\.env\b"
            r"|\bimport\.meta\.env\b"
            r"|\bVITE_[A-Z0-9_]+\b"
            r"|\bNEXT_PUBLIC_[A-Z0-9_]+\b"
            # The (?:_\w+)? tails keep compound names visible: \bSUPABASE\b
            # dies at the underscore of SUPABASE_KEY, the exact spelling the
            # sweep exists to catch.
            r"|\bSUPABASE(?:_\w+)?\b"
            r"|\bAPI_KEY(?:_\w+)?\b"
            r"|\bSECRET(?:_\w+)?\b"
            r"|\bTOKEN(?:_\w+)?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "auth-check",
        re.compile(
            r"check_ajax_referer"
            r"|wp_verify_nonce"
            r"|\bnonce\b"
            r"|\bjwt\b"
            r"|\bbearer\b"
            r"|verify_\w+"
            r"|\bverify\b"
            r"|\bauthenticate\b|\bisAuthenticated\b|\bisAuthorized\b"
            r"|\bauth\b",
            re.IGNORECASE,
        ),
    ),
    (
        "error-swallow",
        re.compile(
            r"\bcatch\s*(?:\(|\{)"
            r"|\bfinally\b"
            r"|\bexcept\b[^:\n]*:"
            r"|\.catch\s*\(\s*(?:\(\s*\)\s*=>|[A-Za-z_$][\w$]*\s*=>\s*\{?\s*\})"
            r"|\bexcept\s*:\s*pass\b",
        ),
    ),
    (
        "migration-ddl",
        re.compile(
            r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|INDEX|POLICY|SCHEMA|TYPE|VIEW|FUNCTION)\b"
            r"|\bALTER\s+TABLE\b"
            r"|\bDROP\s+(?:TABLE|COLUMN|POLICY|INDEX|SCHEMA)\b"
            r"|\b(?:ENABLE|FORCE|DISABLE)\s+ROW\s+LEVEL\s+SECURITY\b"
            r"|\bADD\s+COLUMN\b",
            re.IGNORECASE,
        ),
    ),
    (
        "console-log",
        re.compile(
            r"\bconsole\.\w+\s*\("
            r"|\blogger\.\w+\s*\("
            r"|\blogging\.(?:info|warning|error|debug|exception|critical)\s*\(",
        ),
    ),
    (
        "loop-timer",
        re.compile(
            r"\bsetInterval\s*\("
            r"|\bsetTimeout\s*\("
            r"|\bsetImmediate\s*\("
            r"|\brequestAnimationFrame\s*\("
            r"|\bwhile\s*\(\s*true\s*\)"
            r"|\bfor\s*\(\s*;\s*;\s*\)",
            re.IGNORECASE,
        ),
    ),
    (
        "repo-config",
        re.compile(r"\bpackageManager\b"),
    ),
)

_PATTERN_BY_NAME = dict(_DIGEST_PATTERNS)


def match_class(text: str) -> str | None:
    """The first pattern class ``text`` matches, or ``None``.

    Applied to one diff body line with its ``+``/``-``/space prefix stripped.
    Classes are mutually exclusive per line by report order only; a line that
    matches several is reported under the first, which keeps the digest one
    line per diff line.
    """
    for name, pattern in _DIGEST_PATTERNS:
        if pattern.search(text):
            return name
    return None


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _matched_lines(f: FileDiff) -> list[DiffLine]:
    """Every added/removed line of ``f`` that matches any pattern, in diff
    order, security-critical classes moved ahead of the rest."""
    must: list[DiffLine] = []
    rest: list[DiffLine] = []
    for h in f.hunks:
        for ln in h.lines:
            cls = match_class(ln.text)
            if ln.kind == " " or cls is None:
                continue
            if cls in _MUST_SEE_CLASSES:
                must.append(ln)
            else:
                rest.append(ln)
    return must + rest


def _matches_ddl(f: FileDiff) -> bool:
    pattern = _PATTERN_BY_NAME["migration-ddl"]
    return any(
        pattern.search(ln.text)
        for h in f.hunks
        for ln in h.lines
        if ln.kind != " "
    )


def _rendered(ln: DiffLine) -> str:
    number = ln.new_line if ln.kind == "+" else ln.old_line
    return f"{ln.kind}{number}| {ln.text.rstrip()}"


def _full_content_lines(f: FileDiff) -> list[DiffLine]:
    """Every added line of ``f`` plus its pattern-matched removed lines, in
    diff order. Context lines never appear, in this mode like any other."""
    out: list[DiffLine] = []
    for h in f.hunks:
        for ln in h.lines:
            if ln.kind == " ":
                continue
            if ln.kind == "-" and match_class(ln.text) is None:
                continue
            out.append(ln)
    return out


def _lockfile_notes(files: list[FileDiff]) -> dict[str, str]:
    """One synthetic note per newly added lockfile whose adoption collides
    with another lockfile or a ``packageManager`` pin elsewhere in the diff."""
    basenames = {_basename(f.path) for f in files}
    pin = any(
        _basename(f.path) == _MANIFEST_BASENAME
        and any(
            ln.kind == "+" and _PATTERN_BY_NAME["repo-config"].search(ln.text)
            for h in f.hunks
            for ln in h.lines
        )
        for f in files
    )
    present = sorted(basenames & _LOCKFILE_BASENAMES)
    notes: dict[str, str] = {}
    for f in files:
        base = _basename(f.path)
        if f.status != "added" or base not in _LOCKFILE_BASENAMES:
            continue
        reasons = []
        others = [name for name in present if name != base]
        if others:
            reasons.append("the " + " and the ".join(f"{name} lockfile" for name in others))
        if pin:
            reasons.append('a "packageManager" pin in package.json')
        if reasons:
            notes[f.path] = (
                f"! repo-config: {base} is newly added alongside "
                + " and ".join(reasons)
                + " — possible dual lockfiles for one package root"
            )
    return notes


def _full_content_plan(
    f: FileDiff, share_left: int, global_left: int
) -> tuple[list[DiffLine], str | None, int]:
    """Decide how ``f`` renders: its full-content lines with ``None`` (and the
    small-file share charge), or pattern lines with a degrade note (and a
    zero charge). ``global_left`` is the budget remaining after the file's
    fixed skeleton, so a body that cannot fit degrades instead of eating the
    budget the rest of the PR still needs."""
    migration = _matches_ddl(f)
    added = f.lines_added
    if migration and added > MIGRATION_FULL_CONTENT_MAX_ADDED_LINES:
        return [], _FULL_CONTENT_NOTE.format(reason="migration exceeds the full-content cap"), 0
    small = not migration and added <= FULL_CONTENT_MAX_ADDED_LINES
    if migration or small:
        body = _full_content_lines(f)
        total = sum(len(_rendered(ln)) for ln in body)
        share_charge = sum(len(_rendered(ln)) for ln in body if ln.kind == "+")
        if total > global_left:
            return [], _FULL_CONTENT_NOTE.format(reason="token budget"), 0
        if small and share_charge > share_left:
            return (
                [],
                _FULL_CONTENT_NOTE.format(reason="small-file budget share exhausted"),
                0,
            )
        return body, None, share_charge
    return [], None, 0


def build_digest(files: list[FileDiff], token_budget: int) -> str:
    """Render the whole-PR digest for the systemic sweep.

    Every non-binary file contributes a ``## path`` header, any repo-config
    note, and its hunk headers verbatim. Under them the file renders EITHER
    its full added content — when the migration-ddl pattern touches it (up to
    :data:`MIGRATION_FULL_CONTENT_MAX_ADDED_LINES` added lines) or when it
    adds at most :data:`FULL_CONTENT_MAX_ADDED_LINES` lines — OR the
    added/removed lines matching a pattern in :data:`_DIGEST_PATTERNS`, with
    security-critical classes admitted ahead of the rest inside
    :data:`MAX_LINES_PER_FILE`. Lines render as ``+<new-line>| text``
    (``-`` lines carry their old-file number); context lines never appear.

    Full-content bodies of small files are charged against
    :data:`FULL_CONTENT_BUDGET_SHARE` of the budget, migrations against the
    global budget only; a body that does not fit degrades that file to
    pattern lines with an explicit ``[full content omitted: ...]`` note. The
    remaining cap is ``token_budget`` overall on a 4-chars-per-token
    estimate, at which point the digest ends with
    :data:`TRUNCATION_MARKER`. A ``token_budget`` below 1 degrades to that
    truncated one-line digest rather than raising — the sweep is best-effort
    by contract and never blocks a review.
    """
    budget = max(1, token_budget) * _CHARS_PER_TOKEN
    share_cap = int(budget * FULL_CONTENT_BUDGET_SHARE)
    share_used = 0
    notes = _lockfile_notes(files)
    out: list[str] = [
        f"PR changes {len(files)} file(s); each file lists its hunk headers, "
        "then its full added content when the file is short or a migration, "
        "otherwise only its high-signal added/removed lines."
    ]
    used = len(out[0])
    for f in files:
        header = f"\n## {f.path}"
        if used + len(header) > budget:
            out.append(TRUNCATION_MARKER)
            return "\n".join(out)
        out.append(header)
        used += len(header)
        note = notes.get(f.path)
        if note is not None:
            if used + len(note) + 1 > budget:
                out.append(TRUNCATION_MARKER)
                return "\n".join(out)
            out.append(note)
            used += len(note) + 1
        if f.is_binary or not f.hunks:
            continue
        for h in f.hunks:
            hunk_header = (
                f"@@ -{h.old_start},{h.old_count} +{h.new_start},{h.new_count} @@"
            )
            if used + len(hunk_header) > budget:
                out.append(TRUNCATION_MARKER)
                return "\n".join(out)
            out.append(hunk_header)
            used += len(hunk_header)
        body, degrade, share_charge = _full_content_plan(
            f, share_cap - share_used, budget - used
        )
        full_content = degrade is None and bool(body)
        if degrade is not None:
            if used + len(degrade) + 1 > budget:
                out.append(TRUNCATION_MARKER)
                return "\n".join(out)
            out.append(degrade)
            used += len(degrade) + 1
        if not body:
            body = _matched_lines(f)
        matched = 0
        omitted = 0
        for ln in body:
            if not full_content and matched >= MAX_LINES_PER_FILE:
                omitted += 1
                continue
            rendered = _rendered(ln)
            if used + len(rendered) > budget:
                out.append(TRUNCATION_MARKER)
                return "\n".join(out)
            out.append(rendered)
            used += len(rendered)
            matched += 1
        share_used += share_charge
        if omitted:
            note = f"... {omitted} more matched line(s) in this file omitted"
            out.append(note)
            used += len(note)
    return "\n".join(out)
