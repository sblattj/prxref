"""Systemic-sweep digest: a compact, deterministic view of a whole PR diff.

Per-chunk workers each see one slice of the diff, so patterns that only look
wrong in aggregate — an unauthenticated handler, a secret in a client-exposed
constant, a migration with no policy — have no seat that sees enough to name
them. The systemic sweep spends ONE extra single-shot review call over a
digest of the whole PR, and this module builds that digest deterministically:
the full file list with per-file hunk headers, plus the added/removed lines
that match a curated set of high-signal patterns, capped inside the same
per-chunk token budget the chunking uses. Grep-like on purpose — no model in
the loop — so the same diff always digests to the same text.
"""
from __future__ import annotations

import re

from .triage import FileDiff

# At most this many matched lines from ONE file enter the digest, so a single
# noisy file (a minified bundle, a logging-heavy script) cannot eat the whole
# budget before the sweep has seen the rest of the PR. Per-file, not global:
# a file that trips the cap keeps its first 40 hits and an explicit omission
# note, never a silent cut.
MAX_LINES_PER_FILE = 40

# The digest budget is an upper bound on prompt size, not a billing figure;
# the ~4-chars-per-token estimate is the same order-of-magnitude shortcut
# build_chunks applies (40 tokens per changed code line).
_CHARS_PER_TOKEN = 4

# Truncation is announced in the digest itself so the model knows the text
# ends early, and in the returned marker so tests can pin the behaviour.
TRUNCATION_MARKER = "[digest truncated: token budget reached]"

# One pattern class per systemic family the sweep prompt hunts for. Order is
# the report order of :func:`match_class` when a line matches several —
# entry-point first, because a line that both defines a handler and reads an
# env var is best explained as a handler.
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
)


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


def build_digest(files: list[FileDiff], token_budget: int) -> str:
    """Render the whole-PR digest for the systemic sweep.

    Every non-binary file contributes a ``## path`` header and its hunk
    headers verbatim; under them, only the ``+``/``-`` lines whose text
    matches a pattern in :data:`_DIGEST_PATTERNS`, rendered as
    ``+<new-line>| text`` (``-`` lines carry their old-file number). Context
    lines are never included. Two caps bound the output, both deterministic:
    :data:`MAX_LINES_PER_FILE` matched lines per file (further matches are
    counted in an omission note), and ``token_budget`` overall on a 4-chars-
    per-token estimate, at which point the digest ends with
    :data:`TRUNCATION_MARKER`. A ``token_budget`` below 1 degrades to that
    truncated one-line digest rather than raising — the sweep is best-effort
    by contract and never blocks a review.
    """
    budget = max(1, token_budget) * _CHARS_PER_TOKEN
    out: list[str] = [
        f"PR changes {len(files)} file(s); each file lists its hunk headers, "
        "then only its high-signal added/removed lines."
    ]
    used = len(out[0])
    for f in files:
        header = f"\n## {f.path}"
        if used + len(header) > budget:
            out.append(TRUNCATION_MARKER)
            return "\n".join(out)
        out.append(header)
        used += len(header)
        if f.is_binary or not f.hunks:
            continue
        matched = 0
        omitted = 0
        for h in f.hunks:
            hunk_header = (
                f"@@ -{h.old_start},{h.old_count} +{h.new_start},{h.new_count} @@"
            )
            if used + len(hunk_header) > budget:
                out.append(TRUNCATION_MARKER)
                return "\n".join(out)
            out.append(hunk_header)
            used += len(hunk_header)
            for ln in h.lines:
                if ln.kind == " ":
                    continue
                if match_class(ln.text) is None:
                    continue
                if matched >= MAX_LINES_PER_FILE:
                    omitted += 1
                    continue
                number = ln.new_line if ln.kind == "+" else ln.old_line
                rendered = f"{ln.kind}{number}| {ln.text.rstrip()}"
                if used + len(rendered) > budget:
                    out.append(TRUNCATION_MARKER)
                    return "\n".join(out)
                out.append(rendered)
                used += len(rendered)
                matched += 1
        if omitted:
            note = f"... {omitted} more matched line(s) in this file omitted"
            out.append(note)
            used += len(note)
    return "\n".join(out)
