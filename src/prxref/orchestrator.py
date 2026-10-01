"""Review pipeline orchestrator: fetch diff, chunk, parallel workers, quality, post.

Stage order (v1 — no Jira, no graph, no learnings, no investigator):

1. ``forge.get_pr`` → PRData, ``forge.get_diff`` → raw diff,
   ``parse_unified_diff`` → files. An empty or unchunkable diff (every file
   binary, or no files at all) short-circuits to a summary-only run with
   verdict ``Approved`` — no chunk worker or sweep ever runs, but
   ``heuristics.release_shape_findings`` and
   ``heuristics.toggle_pinned_off_findings`` still do, gated the same as on
   the normal path, so a release-shaped diff with no reviewable text still
   gets its deterministic finding instead of a silent approval, and so does
   a toggle/pin pair on a diff that otherwise chunks to nothing — though a
   toggle needs an added line to match against, so it rarely fires here.
2. ``build_chunks`` risk-ranked chunking (≤ ``max_chunks``, each chunk
   sized to ``token_budget`` and capped at ``max_files_per_chunk`` files).
3. Parallel worker fan-out: one ``reviewer.review_chunk(llm, files, pr)``
   call per chunk on a ThreadPoolExecutor capped at ``max_workers``. The actual
   contract is ``(findings, meta) -> tuple[list[Finding | dict], dict]``,
   with ``meta["error"]`` the empty string on success and the failure
   reason otherwise; dict findings are coerced to ``triage.Finding``. A
   legacy dict-shaped stub (``{"findings": ..., "error": ...}``) is still
   accepted for test doubles. A chunk whose failure is an LLM deadline
   overrun (``timeout`` in the error) is retried ONCE with
   ``context_lines=0`` rendering — a strictly smaller prompt attacks the
   prefill-side share of the wall clock, and a truncated completion (the
   response-side budget) is not a timeout and never reaches this retry.
4. Spec grounding (best-effort, only when ``spec_sources`` is non-empty):
   ``specs.fetch_specs`` + ``specs.build_spec_digest`` run inside the same
   never-raise fence as every other stage, and the digest rides the
   existing chunk calls and the systemic sweep — no extra LLM unit. The
   run is grounded only when the digest holds at least one constraint
   (``specs.constraint_count`` above 0); otherwise no digest is injected
   and the prompts show their no-specs text. A run whose every source
   failed behaves exactly like a run with no specs, plus a grounding note
   in the summary.
5. Systemic sweep: after the chunk workers, ONE more worker-style
   single-shot call over the whole-PR digest built by
   ``systemic.build_digest`` (every file with hunk headers; short files and
   migrations render their full added content, the rest only the
   high-signal matched lines — all capped inside ``token_budget``). It
   hunts the cross-file classes no single chunk seat can see, joins the
   chunk results, and counts as one more review unit: ``chunk_count`` is
   ``len(chunks) + 1`` whenever the sweep ran, and a sweep failure is one
   failed chunk in the partial-review banner.
6. Deterministic checks and quality passes, in exactly this order — the
   raw chunk + sweep findings have their ``scope`` held to ``unknown``
   unless a ticket is active (``_enforce_scope``) and their ``rule`` held
   to ``None`` unless finding grouping or the per-rule cap is on
   (``_enforce_rule``), then gain
   ``heuristics.release_shape_findings(files)`` (a pure, no-LLM finding
   about a PR that is ≥80% release machinery yet also touches source) and
   ``heuristics.toggle_pinned_off_findings(files)`` (a pure, no-LLM finding
   about a toggle this PR adds with a default of on that this PR's own
   test setup pins off), both folded in BEFORE the passes so each is
   filtered like any other finding, and both spliced in at the chunk/sweep
   boundary — before the sweep's own findings, never after — so each is
   always a CHUNK-side finding to ``apply_sweep_dedup``, whose exact tier
   drops only sweep findings and never one chunk-side finding for another.
   Being file-level (line 0), a release-shape finding is also never
   compared by that pass's reworded tier, so it can never be dropped as a
   duplicate of a chunk worker's own restatement; a toggle finding instead
   sits on the toggle's own real line, so with ``dedup_similarity`` set it
   IS compared by that tier against any other chunk-side finding sharing
   its file and line — a chunk worker's own restatement of the same toggle
   included — and only the higher-ranked one of the two (severity, then
   confidence, then content) survives, same as any other same-side pair.
   The opt-in PR-metadata findings (#70, ``metadata_rules``) join that
   same splice with the same chunk-side standing and the same
   deterministic exemption, computed before any worker runs; they are
   summary-only, so the inline batch is selected from the findings that
   are not them (by object identity, never by a reserved rule name):

   ``apply_severity_map`` (only when the team review rules declare a
   severity map: a team word such as ``blocker`` becomes the prxref tier it
   maps to; drops nothing) → ``apply_spec_grounding`` (on an ungrounded
   run every ``spec`` finding, the sweep's included, is relabelled
   ``warning``, counted by a ``specs relabel`` trace event; drops nothing)
   → ``apply_example_echo_check`` (the first pass that drops: a finding
   whose normalized title equals an example finding's title in the worker
   or sweep template the run rendered, packaged or overridden, is dropped
   as ``echoes the prompt's example: "<title>"``, counted by a
   ``prompts echo`` trace event)
   → ``apply_location_validation`` (a ``file``
   naming no path of the parsed diff is dropped, not rendered) →
   ``apply_manifest_claim_check`` (a
   ``package.json`` claim whose dependency is not the key on the anchored
   line, or whose asserted section disagrees with the actual one; it must
   precede line align, which is what makes it read the model's RAW
   anchor) → ``apply_line_align`` → ``apply_anchor_snap`` (#74: the
   hunk-bounded align passes cannot reach a defect the model quoted
   outside every hunk, so the anchor is settled by the finding's own
   quoted code — backticked, double-quoted, or a ``catch (``/``if (``
   interior — against the head file the chunk-context reader serves,
   within 80 lines of the model's RAW line, which the same capture the
   suggestion pass reads supplies; a finding whose snippet the file does
   not hold, whose multi-match nothing breaks, or that sits file-level
   with no snippet at all is marked ``anchor_unverified`` and loses 0.1
   confidence, and without a reader the pass changes nothing) →
   ``apply_thread_dedup`` (existing
   threads fetched best-effort BEFORE the workers run, and after the
   stale-inline prune; failure means no threads; a resolved or outdated
   thread never suppresses, issue #73) →
   ``apply_settled_thread_suppression`` (a finding re-litigating a
   subject an existing open, current thread already argued out,
   line-independently; resolved or outdated threads skipped here too,
   and a surviving finding that matches one is stamped with a
   previously-raised note instead) →
   ``apply_severity_consistency`` (findings sharing a normalized title
   are raised to the group's max severity — the sweep's corroborating
   title counts toward its group) → ``apply_removal_claim_check`` (a
   claim that a NAMED path was removed when the post-image still carries
   it) → ``apply_hedge_gate`` (a finding whose own text conditions the
   defect on something the worker never established; a ``Spec:`` quote
   of the injected digest is not read as the finding's own text) →
   ``apply_rule_scope_check`` (#75, on its own guard: any loaded rules
   file whose body declares at least one ATX section scope, whatever the
   grouping and cap switches say; a ``rule`` label that names no scoped
   section, or whose section's ``scope:`` tokens do not cover the
   finding's path, is cleared to ``None`` and the finding kept, so it
   groups and caps by title like any ruleless one; one INFO line and one
   ``rulescope ok`` trace event count the cleared labels, and the run
   record's ``rule_scope_cleared`` carries the count, ``None`` when the
   check did not run) →
   ``apply_rule_grouping`` (only with ``group_findings`` on: chunk findings
   in one file that break one rule, or that name no rule and share a
   normalized title, fold into one representative that lists
   the other lines after ``Also at:``, the rest dropped as ``grouped into
   <file>:<line>``; sweep findings are never grouped, and running before
   the gate is what makes every cap count groups; one INFO line and one
   ``grouping ok`` trace event count the groups and the folded members) →
   ``apply_rule_cap`` (only with a team rules file loaded and
   ``max_findings_per_rule`` above 0, which also turns the rule request
   on: at most that many chunk findings per rule, across every file, stay
   active, a group counting once, and the rest fold onto the best one kept,
   which lists them after ``Also at:`` and in its ``locations``, the rest
   dropped as ``rule cap exceeded (max <n>): listed at <file>:<line>``;
   sweep findings are never capped; one INFO line and one ``rulecap ok``
   trace event count the folded findings and the rules over the cap) →
   ``apply_location_verification`` (#74, once ``locations`` is final:
   each ``Also at:`` site of a grouped or capped representative is
   re-checked against the head file for the representative's own quoted
   snippets within 80 lines of the site, and a site nothing corroborates
   is dropped from the list and the paragraph — an unreadable file keeps
   every site, folded members keep their drop reasons, and without a
   reader the pass changes nothing) →
   ``apply_quality_gate(confidence_floor=, max_errors=,
   max_warning_findings=, max_outofscope_findings=)``, which returns
   its findings in content order, so the chunk/sweep boundary is
   re-derived here from finding identity and the gate's stable sort
   (``_split_at_sweep``) rather than carried across the gate as an index
   → ``apply_sweep_dedup`` (drops a sweep finding that
   restates a chunk finding that SURVIVED the gate, on file + normalized
   title; running it after the gate is what keeps a sub-floor chunk
   finding from suppressing its higher-confidence sweep duplicate and
   then dying at the gate itself. With ``dedup_similarity`` set, a
   reworded tier also compares findings in the same file on the same
   line, or two file-level (line 0) findings of the same file (#74): a
   sweep copy no more severe than a chunk copy is dropped, and of
   two copies on one side the less severe, then less confident, one is;
   a chunk copy is never dropped for a sweep copy) → ``apply_containment_note`` (a throw
   / panic / crash / unhandled-rejection finding that never names its
   catch or its propagation target gets its body suffixed with
   ``" [containment boundary not stated]"``; textual only, runs last so
   it touches both the active and dropped copies of chunk and sweep
   findings alike). Dropped findings are retained in the result with
   ``drop_reason`` set, never silently discarded, and both lists come out
   sorted by ``finding_sort_key``. Every result — including an error or
   summary-only exit — carries a ``sampling`` record naming the
   temperature, seed, and model chain actually in force, and the run-record
   keys that :func:`_run_record` stamps on every exit (``cost_usd``,
   ``cost_estimated``, ``review_rules``, ``ticket_context``,
   ``spec_grounding``, ``size_advisory``, ``prompt_templates``,
   ``scoped_rules``, ``rule_counts``, ``rule_scope_cleared``,
   ``repo_context``, ``evidence``; ``replay`` on replays only, and
   ``cost_api_equivalent`` on claude-cli-priced runs only).
7. Verdict: ``"Error"`` when every CHUNK review failed (a sweep success
   on a dead worker pool cannot carry the run); ``"Request-Changes"``
   iff any active error-severity finding survives; else ``"Incomplete"``
   when any review unit failed (issue #72: a partial review used to read
   as ``"Approved"`` because the failed chunk simply contributed no
   findings); else ``"Approved"``. A partial failure also degrades the
   run record (``degraded.cause == "partial"``, one ``chunks`` row per
   failed unit) and the summary declares reduced coverage AND itemizes
   each failed chunk with the files it took unreviewed plus its reason
   (capped, redacted, inside the same blockquote) — a partial review
   reads as a successful one, so a failure left only in the logs reaches
   nobody, and a file list left out of it leaves the operator guessing
   which files went unreviewed.
8. Post: summary rendered from ``reviewer.load_prompt("summary")``, or from
   the operator's ``summary.md`` override when ``prompts`` carries one, with
   placeholders ``{verdict} {title} {file_count} {error_count}
   {warning_count} {spec_count} {spec_note} {ticket_note} {evidence_note}
   {outofscope_count} {findings} {attribution}`` filled, along with the
   marker slots and the optional slots of :func:`_render_summary`
   (per-severity finding groups, head SHA, chunk and token counts), plus
   inline comments for up to ``max_inline_comments`` active findings.
   ``post_mode`` narrows what is written: ``"summary+inline"`` (default) is
   that full behaviour, ``"summary"`` skips the inline batch, ``"inline"``
   skips every summary post including the error notices. ``post_verdict``
   renders the summary without the verdict stamp while keeping the rest of
   the template.

Every failure reason that reaches a POSTED comment goes through
:func:`redact_for_post` first — on both posting paths, the partial-review
banner and the total-failure notice. The logs keep the full text.

No stage failure raises out of ``orchestrate_review``: a forge failure, an
unparseable or unchunkable diff, or a total LLM failure degrades to verdict
``"Error"`` with a posted notice (when ``post`` is true). Exit-code posture
lives in the CLI. The guarantee holds for a library caller too, who has no
config-level range check in front of these arguments.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlparse

from . import (
    chunk_context,
    costs,
    followup,
    heuristics,
    repo_contracts,
    repo_reader,
    repo_unit,
    reviewer,
    specs,
    systemic,
)
from .ci_fallback import DEGRADED_SUMMARY_KEY
from .ci_wiring import ci_wiring_findings
from .forges.base import (
    ATTRIBUTION_MARKER,
    CommitData,
    Forge,
    InlineComment,
    PRData,
    PRRef,
    Thread,
)
from .forges.repo_dir import RepoDir
from .formatter import SUGGESTION_STYLE_GITHUB, format_suggestion_block, suggestion_range
from .llm import LLMClient
from .markers import (
    active_severity_markers,
    inline_header,
    marker_for,
    marker_slots,
    out_of_ticket_marker,
    severity_marker,
)
from .metadata_rules import run_metadata_checks
from .prompt_templates import (
    CONTEXT_MARKER,
    REVIEW_TEMPLATES,
    SUMMARY_GROUP_PLACEHOLDERS,
    PromptTemplates,
    packaged_text,
    placeholders,
    uncovered_summary_groups,
)
from .quality import (
    GROUPED_INTO_PREFIX,
    RULE_CAP_PREFIX,
    SUGGESTION_CLEAR_REASONS,
    _resolve_confidence_floor,
    active,
    apply_anchor_snap,
    apply_containment_note,
    apply_evidence_verdicts,
    apply_example_echo_check,
    apply_hedge_gate,
    apply_line_align,
    apply_location_validation,
    apply_location_verification,
    apply_manifest_claim_check,
    apply_quality_gate,
    apply_removal_claim_check,
    apply_rule_cap,
    apply_rule_grouping,
    apply_rule_scope_check,
    apply_settled_thread_suppression,
    apply_severity_consistency,
    apply_severity_map,
    apply_spec_grounding,
    apply_suggestion_validation,
    apply_sweep_dedup,
    apply_thread_dedup,
    finding_rank_key,
    finding_sort_key,
    previously_discussed_thread,
    prompt_example_titles,
    rule_cap_counts,
)
from .repo_context import exclude_predicate
from .reviewer import NO_PROMPT_CONTEXT, PromptContext, fill_template
from .trace import Tracer, get_tracer
from .triage import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_MAX_FILES_PER_CHUNK,
    DEFAULT_TOKEN_BUDGET,
    SCOPE_IN,
    SCOPE_OUT,
    SCOPE_UNKNOWN,
    Finding,
    added_lines_by_file,
    count_size_relevant_changes,
    normalize_evidence,
    normalize_rule,
    normalize_scope,
    parse_unified_diff,
    plan_chunks,
)

logger = logging.getLogger("prxref")

MAX_WORKERS = 4
MAX_INLINE_COMMENTS = 15

# The posting-behaviour vocabulary. Restated in config._POST_MODES (config is
# a leaf module and must not import this pipeline); the two are pinned
# together by TestPostMode::test_the_vocabulary_matches_the_orchestrator.
POST_MODES = frozenset({"summary+inline", "summary", "inline"})
POST_SUMMARY_MODES = frozenset({"summary+inline", "summary"})
POST_INLINE_MODES = frozenset({"summary+inline", "inline"})

#: ``PRXREF_INCREMENTAL``'s vocabulary (issue #34), restated in config._CHOICE_KEYS.
INCREMENTAL_MODES = frozenset({"off", "on"})

#: The reviewed-head marker an incremental-capable run stamps on its summary:
#: :func:`reviewed_head_line` writes it and :data:`REVIEWED_HEAD_RE` reads it
#: back, both from these two halves.
REVIEWED_HEAD_PREFIX = "<!-- prxref-reviewed-head: "
REVIEWED_HEAD_SUFFIX = " -->"
REVIEWED_HEAD_RE = re.compile(
    re.escape(REVIEWED_HEAD_PREFIX) + r"([0-9a-f]{7,64})" + re.escape(REVIEWED_HEAD_SUFFIX)
)

# How many failed chunks the partial-review banner itemizes — each with its
# file list and redacted reason — before it starts counting the rest. Three is
# enough to show a mixed failure (say, a starved budget plus a timeout) without
# letting a pathological run bury the findings under its own diagnostics.
MAX_REPORTED_REASONS = 3

# Inline-comment priority: the most severe findings get the anchor first, so
# a cap or a rejected anchor costs the run its least-important comments
# rather than whatever happened to sit at the tail of chunk order. spec sits
# below warning (a spec violation is an operator-requested contract breach,
# but not claimed to break at runtime) and above outofscope.
_SEVERITY_RANK = {"error": 0, "warning": 1, "spec": 2, "outofscope": 3}

# The tie-break after severity: within one severity, a finding outside the
# ticket yields the inline slots to in-ticket and unjudged ones. With no active
# ticket every scope is unknown, so the ordering is exactly the severity one.
_SCOPE_RANK = {SCOPE_IN: 0, SCOPE_UNKNOWN: 0, SCOPE_OUT: 1}

_REDACTED = "[redacted]"

# Key=value pairs whose VALUE is safe to post. An ALLOWLIST, deliberately: an
# unrecognised key's value is dropped, so a future exception string carrying
# ``gateway=``, ``session=`` or ``account=`` is covered without anyone having
# had to think of it first. Everything listed here is prxref's own vocabulary,
# emitted by prxref itself, and names a diagnostic the operator acts on —
# ``max_tokens`` and ``finish_reason`` are the truncation message, whose whole
# purpose is telling the operator which lever to pull, and ``model`` is already
# posted verbatim by _attribution. ``port`` is NOT here: with a host it is the
# endpoint's identity, and it diagnoses nothing on its own.
_POSTABLE_KV_KEYS = frozenset({
    "max_tokens", "finish_reason", "model", "status", "status_code", "code", "errno",
})

# Any scheme://rest-of-token. A URL is the single densest leak: it carries the
# host, the path, and whatever the operator put in the query string.
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://\S+")

# requests writes the request target as ``with url: /path?query`` — a bare path,
# so _URL_RE never sees it, and the query string is where an api_key rides.
_URL_FIELD_RE = re.compile(r"\burl\s*[:=]\s*\S+", re.IGNORECASE)

# ``Bearer <token>``, with or without the ``Authorization:`` label in front,
# then a labelled credential with no ``Bearer`` in it. Two patterns rather than
# one so the label and its value are consumed together instead of leaving the
# token behind as a bare word.
_BEARER_RE = re.compile(
    r"\b(?:authorization\s*[:=]?\s*)?bearer\b[\s:=]*\S*", re.IGNORECASE
)
_AUTH_FIELD_RE = re.compile(r"\bauthorization\b\s*[:=]\s*\S*", re.IGNORECASE)

_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

# A quoted single token containing a dot, colon, slash or at-sign: a hostname,
# an address, a path, an account. ``Failed to resolve 'host.example'`` is not a
# key=value pair and nothing else catches it. Quoted PROSE (anything with
# whitespace in it) is left alone and sanitised by the other rules instead, so
# a nested exception message keeps its diagnostic text.
_QUOTED_LOCATOR_RE = re.compile(r"(['\"])([^'\"\s]*[.:/@][^'\"\s]*)\1")

# The placeholder is listed as a value alternative FIRST so redaction is
# idempotent: without it ``url=[redacted]`` (written a moment earlier by
# _URL_FIELD_RE) re-matches with the value ``[redacted`` and leaves a stray
# bracket behind.
_KV_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_.\-]*)\s*=\s*"
    + r"(" + re.escape(_REDACTED) + r"|'[^']*'|\"[^\"]*\"|[^\s,;)\]}]+)"
)

# Known credential prefixes, for the ones too short to trip the length rule.
_KNOWN_SECRET_RE = re.compile(
    r"\b(?:sk|pk|rk|ghp|gho|ghu|ghs|glpat|xox[abprs])[-_][A-Za-z0-9_\-]{8,}\b"
)

# A long unbroken run of opaque characters is a credential shape (an API key, a
# JWT segment, a session id). PRXREF_* names are exempt: they are the operator's
# levers, prxref emits them itself, and PRXREF_GITHUB_ENTERPRISE_TOKEN is long
# enough to trip this. A dotted Python name is exempt for free — ``.`` is a word
# boundary, so ``requests.exceptions.ConnectionError`` is measured per segment.
_OPAQUE_RE = re.compile(r"\b(?!PRXREF_)[A-Za-z0-9_\-]{24,}\b")


def _redact_kv(match: re.Match) -> str:
    """Keep an allowlisted key's value; drop every other one."""
    key = match.group(1)
    if key.lower() in _POSTABLE_KV_KEYS:
        return match.group(0)
    return f"{key}={_REDACTED}"


def redact_for_post(reason: str) -> str:
    """Strip endpoint and credential detail out of a reason before POSTING it.

    prxref's entire job is writing comments onto pull requests, and a chunk
    failure reason is one of the things it writes. ``requests`` puts the
    gateway host, the request path and the query string into a
    ``ConnectionError``'s message; ``OpenAICompatClient.invoke`` wraps that
    verbatim; the reviewer stores it in ``meta["error"]``. Posted unedited on a
    public repository, that publishes the operator's endpoint and any
    credential riding in its URL.

    The approach is allowlist-flavoured rather than a catalogue of secret
    patterns: URLs, quoted network locators and every key=value pair whose key
    is not in :data:`_POSTABLE_KV_KEYS` lose their value, so a leak shape
    nobody anticipated is covered by default. What survives is the diagnostic
    SHAPE an author can act on — the exception class, ``HTTP 429``, a timeout,
    and the truncation message with its ``PRXREF_LLM_MAX_TOKENS`` hint, which
    is byte-for-byte untouched — naming that lever is the only reason the
    reason string is posted at all.

    Applies to the POSTED text only. stderr keeps the full reason: the logs are
    operator-only, and an operator debugging a dead gateway needs the host.
    """
    if not reason:
        return reason
    out = _URL_RE.sub(_REDACTED, reason)
    out = _URL_FIELD_RE.sub(f"url={_REDACTED}", out)
    out = _BEARER_RE.sub(_REDACTED, out)
    out = _AUTH_FIELD_RE.sub(_REDACTED, out)
    out = _IPV4_RE.sub(_REDACTED, out)
    out = _QUOTED_LOCATOR_RE.sub(rf"\1{_REDACTED}\1", out)
    out = _KV_RE.sub(_redact_kv, out)
    out = _KNOWN_SECRET_RE.sub(_REDACTED, out)
    return _OPAQUE_RE.sub(_REDACTED, out)

_FALLBACK_SUMMARY_TEMPLATE = (
    "🤖 **prxref review — {verdict}**\n\n"
    "PR: {title}\n\n"
    "Files reviewed: {file_count} · {error_marker} {error_count} error · "
    "{warning_marker} {warning_count} warning · {spec_marker} {spec_count} spec · "
    "{outofscope_marker} {outofscope_count} outofscope\n"
    "{spec_note}{ticket_note}{evidence_note}\n"
    "{findings}\n\n{attribution}"
)

SUMMARY_BULLET_SEPARATOR = " — "
_SUMMARY_GROUP_LABELS: dict[str, str] = {
    "error": "Errors", "warning": "Warnings", "spec": "Spec", "outofscope": "Minor",
}

# A {verdict} placeholder together with the separator joining it to the rest
# of its line — ": {verdict}", " — {verdict}", " - {verdict}" — so removing it
# leaves a clean header instead of a dangling colon or dash. A bare
# placeholder is removed too; whitespace runs around it are collapsed.
_VERDICT_STAMP_RE = re.compile(r"[ \t]*[:—–-]?[ \t]*\{verdict\}[ \t]*")


def _strip_verdict_stamp(template: str) -> str:
    """Remove the ``{verdict}`` stamp from a summary template.

    Applied to the template BEFORE the placeholders are filled, so the
    shipped templates render as ``**prxref review**`` and
    ``## prxref automated review`` with the rest of the comment untouched.
    """
    return _VERDICT_STAMP_RE.sub("", template)


def orchestrate_review(
    forge: Forge,
    ref: PRRef,
    llm: LLMClient,
    *,
    post: bool = True,
    max_chunks: int = 8,
    max_tokens: int | None = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    max_files_per_chunk: int = DEFAULT_MAX_FILES_PER_CHUNK,
    context_lines: int = DEFAULT_CONTEXT_LINES,
    max_workers: int = MAX_WORKERS,
    max_inline_comments: int = MAX_INLINE_COMMENTS,
    confidence_floor: float | None = None,
    max_errors: int | None = None,
    dedup_similarity: float | None = None,
    post_mode: str = "summary+inline",
    post_verdict: bool = True,
    summary_bullet_separator: str = SUMMARY_BULLET_SEPARATOR,
    trace_file: str | None = None,
    trace_dir: str | None = None,
    spec_sources: Sequence[str] = (),
    spec_max_chars: int = 120000,
    spec_digest_tokens: int = 3000,
    jira_base_url: str = "",
    jira_email: str = "",
    jira_api_token: str = "",
    rules: Any = None,
    ticket: Any = None,
    price_table: Mapping[str, Any] | None = None,
    post_cost: bool = False,
    size_warn_lines: int | None = None,
    size_warn_files: int | None = None,
    size_ignore_globs: Sequence[str] = (),
    replay: Mapping[str, Any] | None = None,
    prompts: PromptTemplates | None = None,
    scoped_rules: Any = None,
    scoped_rules_max_chars: int = 24000,
    group_findings: bool = False,
    max_warning_findings: int | None = None,
    max_outofscope_findings: int | None = None,
    max_findings_per_rule: int = 2,
    repo_context: str = "off",
    repo_context_max_chars: int = 12000,
    context_contract_globs: Sequence[str] = (),
    context_exclude_globs: Sequence[str] = (),
    context_standards_globs: Sequence[str] = (),
    context_standards_max_chars: int = 4000,
    repo_dir: RepoDir | None = None,
    repo_context_max_reads: int = repo_reader.MAX_RUN_READS,
    repo_context_max_chunk_reads: int = repo_reader.MAX_CHUNK_READS,
    llm_parse_retries: int = 0,
    context_followup: str = "off",
    suggestions: str = "off",
    incremental: str = "off",
    full_review: bool = False,
    full_review_reason: str | None = None,
    metadata_rules: str = "off",
    branch_patterns: Sequence[str] = (),
    commit_reference: str = "",
    area_globs: Sequence[str] = (),
    max_areas_per_pr: int = 2,
    ci_wiring: str = "off",
    ci_wiring_globs: Sequence[str] = (),
    evidence: Any = None,
    evidence_max_chunk_chars: int = 4000,
) -> dict:
    """Run one full review pass over a PR and optionally post results.

    Returns ``{verdict, findings_active, findings_dropped, chunk_count,
    chunks_reviewed, chunks_failed, chunks_over_budget, largest_chunk_tokens,
    overflow_files, chunk_token_budget, elapsed_ms, input_tokens,
    output_tokens, posted, sampling, cost_usd, cost_estimated, review_rules, ticket_context,
    spec_grounding, size_advisory, prompt_templates, scoped_rules,
    rule_counts, repo_context, parse_retries, context_followup,
    suggestions, incremental, ci_wiring, evidence, degraded}``, plus
    ``replay`` on a replay run only. Every exit, error and empty-diff exits
    included, goes through
    :func:`_run_record`, so the last sixteen keys are always present and are
    ``None`` (``cost_usd``: ``0.0`` before any LLM request; ``cost_estimated``:
    ``False``; ``parse_retries``: ``0`` before any review unit when the
    parse retry is on) when their feature is off or the run never reached it.
    ``cost_usd`` is ``None`` when the cost is unknown, never ``0``.
    ``cost_api_equivalent`` (always ``True``) is added only when every
    reported unit cost came from claude-cli
    (:func:`prxref.costs.api_equivalent_run`), so the CLI's ``-v`` line can
    label the figure; ``--format json`` never emits it, because each unit's
    ``cost_source`` (in the ``PRXREF_TRACE_DIR`` meta files) is already the
    machine-readable label.
    Never raises on ANY stage failure — forge, diff parsing,
    chunking, or LLM — the run degrades to verdict ``"Error"`` with a posted
    notice when ``post`` is true. Degenerate arguments are part of that: a
    caller passing ``max_chunks=0`` gets an error run, not a ``ValueError``.
    The one exception is an unknown ``repo_context`` level, which raises
    ``ValueError`` before any forge call (see below).

    ``chunk_count`` counts the review units: ``len(chunks)`` plus one for
    the systemic sweep, which runs whenever at least one chunk exists, and on
    an incremental run whose delta chunks to nothing (an empty diff returns
    before any review unit runs). ``chunks_reviewed`` + ``chunks_failed``
    always equals it.

    ``chunks_over_budget``, ``largest_chunk_tokens``, ``overflow_files`` and
    ``chunk_token_budget`` (issue #61) describe the diff chunking, from the
    same :func:`prxref.triage.plan_chunks` pass that produced the chunks, and
    are stamped on every exit by :func:`_run_record`. ``chunk_token_budget``
    is ``token_budget``; ``chunks_over_budget`` counts the chunks whose
    :func:`prxref.triage.est_tokens` total exceeds it (a single file larger
    than the budget counts, even below ``max_chunks``);
    ``largest_chunk_tokens`` is the largest chunk's estimate; and
    ``overflow_files`` counts the files placed past the cap, appended to the
    smallest chunk because ``max_chunks`` chunks existed and none had room.
    The three counts are ``0`` on every exit taken before chunking ran or
    where it produced no chunk (an empty diff, every file binary); an exit
    after chunking (the scoped-rules error, a total failure) carries the
    real counts even when ``chunk_count`` is ``0``.

    ``max_tokens`` is the per-chunk completion budget handed to every worker;
    ``None`` leaves ``reviewer.MAX_TOKENS`` in charge. ``token_budget`` sizes
    each diff chunk, ``max_files_per_chunk`` caps the files placed in one,
    ``context_lines`` bounds the hunk context rendered into each worker
    prompt, ``max_workers`` the fan-out, ``max_inline_comments`` the posted
    batch. ``confidence_floor`` and ``max_errors`` are forwarded to
    ``apply_quality_gate``; ``None`` leaves that pass reading the environment
    itself, which is what a library caller with no config dict wants.
    ``dedup_similarity`` is forwarded to ``apply_sweep_dedup`` as its
    ``similarity``. ``None`` (the default) runs its exact-title tier alone
    and reads no environment variable, so the run is byte-identical to one
    without it; a threshold also drops a reworded restatement in the same
    file and on the same line (:func:`quality.titles_similar`).

    ``post_mode`` selects what is written to the forge: ``"summary+inline"``
    (default) keeps today's behaviour — the summary first, then the inline
    batch only if the summary landed — ``"summary"`` never calls
    ``post_inline_comments``, and ``"inline"`` never calls ``post_summary``,
    on any path, empty-diff and total-failure notices included, so its
    ``posted`` flag tracks the inline batch alone. ``post_verdict=False``
    renders the summary without the verdict stamp; the computed verdict in
    the result dict and the total-failure notice (whose job is to say the
    review failed) are unaffected. All of these are request knobs and are
    deliberately absent from the returned dict. The vocabulary is not
    re-validated here: ``load_config`` already gates it, and a library
    caller passing an unknown mode degrades to the plain no-op that mode's
    membership tests produce.

    ``summary_bullet_separator`` joins each summary bullet's ``file:line``
    to its title (``PRXREF_SUMMARY_BULLET_SEPARATOR``); the default,
    :data:`SUMMARY_BULLET_SEPARATOR` (``" — "``), renders the summary exactly
    as before. It reaches every summary render, the empty-diff summary and
    the inline-accounting re-post included, and is not re-validated here.

    ``trace_dir`` turns on the per-unit prompt/response dump: each review
    unit writes ``<label>.system.md``, ``<label>.user.md``,
    ``<label>.response.json`` (raw model text), and ``<label>.meta.json``
    (model, token counts, elapsed, error) under that directory, labelled
    ``chunk0`` … ``chunkN-1`` and ``sweep``. Empty (the default) traces
    nothing; a write failure is a logged warning, never a review failure.

    ``spec_sources`` grounds the review against written specs: each entry is
    fetched by :func:`prxref.specs.fetch_specs` and the pruned constraint
    digest (:func:`prxref.specs.build_spec_digest`) is injected into every
    worker prompt and the sweep prompt — no extra LLM unit. The fetch never
    raises and never fails the run: sources that fail become a grounding
    note in the summary (failure reasons pass through
    :func:`redact_for_post` before posting), and a run whose every source
    failed is exactly a run with no specs plus that note. A digest holding
    no constraint (:func:`prxref.specs.constraint_count` is 0) is not
    injected, and on such a run, with or without sources, every
    model-emitted ``spec`` finding is relabelled ``warning``
    (:func:`quality.apply_spec_grounding`); when any is, one INFO line and
    one ``specs relabel`` trace event count them. The remaining
    spec keywords mirror the config keys of the same names
    (``spec_max_chars``, ``spec_digest_tokens``, ``jira_base_url``,
    ``jira_email``, ``jira_api_token``); the defaults restate
    ``config._DEFAULTS`` the way ``MAX_WORKERS`` does. ``spec_sources`` is
    deliberately absent from the returned dict, like every other request
    knob.

    ``rules`` and ``ticket`` are the loaded review-rules and ticket-context
    objects (``rules.ReviewRules`` / ``ticket.TicketContext``), duck-typed so
    this module never imports theirs; ``None`` turns each off, and with both
    off the prompts, posts, record and trace are exactly a run without them.
    Their ``record()`` fills the ``review_rules`` / ``ticket_context`` keys on
    every exit, and is the meta of one ``rules ok`` / ``ticket ok`` trace
    event. The rules' ``prompt_block("worker")`` / ``("sweep")`` reach every
    chunk and the sweep through one :class:`reviewer.PromptContext`, and
    their ``severity_map`` goes to :func:`quality.apply_severity_map` ahead of
    every quality pass (a ``rules remap`` event counts the rewrites). An
    ACTIVE ticket (``ticket.active``) adds its ``scope_block()`` and
    ``prompt_block()`` to every unit, which is what lets a finding carry a
    ``scope`` of ``in`` or ``out``; otherwise every scope is forced to
    ``unknown`` (:func:`_enforce_scope`). A configured ticket's ``note()``
    rides the summary after the spec note, on the main and summary-only
    posts but never the error notice, and an active ticket's scope counts
    ride the ``run ok`` event.

    ``price_table`` is the parsed ``PRXREF_PRICE_TABLE``
    (:func:`prxref.costs.parse_price_table`); ``None`` or ``{}`` estimates
    nothing. It is not read from the environment here, because parsing can
    raise ``ConfigError`` and this function must not raise. ``post_cost``
    appends the run's cost label (:func:`prxref.costs.cost_label`) as the
    last field of the summary and error-notice attribution; off, both are
    byte-identical to a run without it.

    ``size_warn_lines`` / ``size_warn_files`` are the PR-size advisory
    thresholds (``None`` = off; ``0`` is a legal threshold) and
    ``size_ignore_globs`` the operator's extra ignore patterns. When either
    threshold is set the advisory's stats ride the result under
    ``size_advisory``, and a triggered advisory is prepended to every posted
    summary. It never touches the verdict.

    ``metadata_rules`` (issue #70) turns on the deterministic PR-metadata
    checks: ``branch_patterns`` (``type=regex``; the PR's type from its
    labels, else its title's conventional-commit prefix, must own a pattern
    the source branch fully matches), ``commit_reference`` (a regex every
    non-merge commit subject must contain; needs the forge's optional
    ``get_commits``, without which the check reports itself skipped) and
    ``area_globs`` with ``max_areas_per_pr`` (the diff's paths may not
    spread over more named areas than that). All three are pure, computed
    before any worker runs and making no LLM call of their own; each
    violation is a ``warning`` / ``outofscope`` finding with confidence 1.0,
    folded in beside the heuristics findings at the chunk/sweep boundary
    and SUMMARY-ONLY — the inline batch is selected from the findings that
    are not them, by object identity threaded from the check, never by a
    reserved rule name a model finding could match. With
    ``metadata_rules`` off (the default) nothing runs, no
    ``metadata_rules`` key is stamped on the run record, and the run is
    byte-identical to one without the feature; on, the record carries
    ``{branch_pattern, commit_reference, area_globs}`` each
    ``"pass"``/``"fail"``/``"skipped: <reason>"``, echoed by one
    ``metadata_rules ok`` trace event. None of it touches the verdict or
    the exit code.

    ``ci_wiring`` (issue #66) turns on the CI-wiring check:
    ``ci_wiring_globs`` (``PRXREF_CI_WIRING_GLOBS``; the default restates
    :data:`prxref.ci_wiring.DEFAULT_CI_GLOBS`) selects the CI
    configuration files, where a set value replaces the built-in set. The
    check flags a check-shaped file the PR adds — a script whose name or a
    ``--flag`` it gains says verify, smoke or check, a file that gains a
    shebang, or a new test file outside the runner's default include
    (:func:`prxref.ci_wiring.default_include`) — that no CI file invokes
    by full path or bare basename: one finding per unwired check,
    file-level on its own path, ``spec`` when the ticket text mentions
    regression checks, CI, pipelines or automated tests (relabelled
    ``warning`` by spec grounding on an ungrounded run) and ``warning``
    otherwise, an INLINE candidate like the heuristic findings rather
    than summary-only. It reads the repository through its own reader —
    the forge's head-sha reads or ``--repo-dir``, gated on the reader and
    NOT on ``repo_context`` — at most
    :data:`prxref.ci_wiring.MAX_CI_FILES` file reads, never the
    ``repo_context`` reader whose caps a CI file could starve on; with no
    reader the run logs one WARNING naming ``PRXREF_CI_WIRING`` and the
    record says why. Off (the default) nothing runs, nothing is read, and
    the record's ``ci_wiring`` key is ``None``; on, it carries
    ``{candidates, ci_files, picked_up_default, triggered}``, echoed by
    one ``ci_wiring ok`` trace event. Never changes the verdict or the
    exit code.

    ``evidence`` (issue #69) is the loaded execution evidence
    (:class:`prxref.evidence.EvidenceBundle`, as
    :func:`prxref.evidence.load_evidence` returns it, duck-typed like
    ``rules`` and ``ticket``: ``active``, ``record()``, ``block_for()``,
    ``global_block()`` and ``matched_for()``), and ``evidence_max_chunk_chars``
    (``PRXREF_EVIDENCE_MAX_CHUNK_CHARS``; the default restates
    ``config._DEFAULTS`` the way ``MAX_WORKERS`` does) is the per-unit
    character budget of its prompt blocks. ``None`` (an empty list
    configured) turns it off, and an unset run's prompts, posts, record
    and trace are exactly a run without it. When active, each chunk's
    prompt carries the evidence items its paths matched plus the global
    ones, and the sweep's prompt the global ones alone, each item fenced
    and labelled data-not-instructions under a must-not-contradict rule;
    the worker may answer with a per-finding ``"evidence": "contradicts"``
    label, read only on units whose prompt carried evidence, and
    :func:`quality.apply_evidence_verdicts` — right after spec grounding —
    RELABELS such a finding ``warning`` rather than dropping it, counted by
    one INFO line, one ``evidence downgrade`` trace event and the summary's
    evidence note (``{evidence_note}``, after the ticket note). The
    ``evidence`` key of every exit is ``{files, items, matched_chunks,
    max_chars}`` (paths as configured, never the evidence text), echoed by
    one ``evidence ok`` trace event, and an EMPTY bundle (files that held
    no item) is recorded like an empty ticket: noted, never an error.
    Never changes the verdict or the exit code.

    ``replay`` is the evaluation-replay stamp built by the CLI
    (``{base_sha, head_sha, threads, diff_file, description, as_of,
    as_of_source}``). When given it is copied
    into the returned dict under ``replay`` and into the ``run start`` trace
    event, the one request knob that is echoed back, so a replay can never
    be read as a live review. It changes nothing about how the review runs:
    pinning and thread hiding live in the forge the caller passes.

    ``prompts`` is the loaded prompt-template overrides
    (:class:`prxref.prompt_templates.PromptTemplates`); ``None`` turns them
    off, and an unset run's prompts, posts, record and trace are exactly a run
    without it. Its ``record()`` fills the ``prompt_templates`` key on every
    exit, and is the meta of one ``prompts ok`` trace event. Each template is
    taken through :meth:`~prxref.prompt_templates.PromptTemplates.override`,
    so one left packaged is still read by ``reviewer.load_prompt``: the
    ``worker`` and ``systemic`` overrides reach every chunk and the sweep
    through the one :class:`reviewer.PromptContext`, and the ``summary``
    override is the template of every summary render, the empty-diff summary
    and the inline-accounting re-post included, but never of the error notice.
    The example-finding titles of the worker and sweep templates the units
    rendered, overridden or packaged, are what
    :func:`quality.apply_example_echo_check` drops echoes of.

    ``scoped_rules`` is the loaded path-scoped review rules
    (:class:`prxref.rules.ScopedRules`, as
    :func:`prxref.rules.load_scoped_rules` returns them, taken duck-typed
    like ``rules``: ``record()``, ``files``, ``unit_block()`` and
    ``merged_severity_map()``), and
    ``scoped_rules_max_chars`` is the per-unit cap on their text
    (``PRXREF_SCOPED_RULES_MAX_CHARS``; the default restates
    ``config._DEFAULTS`` the way ``MAX_WORKERS`` does). ``None`` turns them
    off, and an unset run's prompts, posts, trace and logs are exactly a run
    without it; its record differs only by the ``scoped_rules`` key, which is
    ``None``. When set, each chunk's paths (every file's ``path``, plus its
    ``old_path`` on a rename) select the chunk's rules through
    :meth:`~prxref.rules.ScopedRules.unit_block`, with ``rules`` as the
    always-on file, and that block replaces ``rules_worker`` in the chunk's
    own copy of the :class:`reviewer.PromptContext`, on the first attempt and
    on the timeout retry alike. The sweep's block, selected by the union of
    the chunks' paths, replaces ``rules_sweep``. When the cap cuts or omits a
    file in any unit, one WARNING for the run names
    ``PRXREF_SCOPED_RULES_MAX_CHARS`` and every such file. The
    severity-remapping pass applies
    :meth:`~prxref.rules.ScopedRules.merged_severity_map` in place of the
    rules' own map, so a scoped file's map applies with no always-on file.
    The ``scoped_rules`` key of every exit is ``record()`` plus ``max_chars``
    (this per-unit cap; each ``files`` row keeps its own per-file
    ``max_chars``) and ``units``, which is ``{"chunks": [[<row>, ...], ...],
    "sweep": [<row>, ...]}`` with one list per chunk in chunk order and one
    ``{"path": ..., "chars": ...}`` row per scoped file the unit carries, in
    load order, ``chars`` being that file's ``chars`` in ``files``; ``units``
    is ``None`` on an exit before the units are planned (a forge, parse or
    chunking failure, or the empty diff). ``record()`` plus ``max_chars`` is
    the meta of one ``scoped_rules ok`` trace event, and the ``chunk start``
    and ``sweep start`` events carry their unit's rows as ``rules``. A cap
    below 1 is an error run, not a ``ValueError``, as ``max_chunks=0`` is.

    ``group_findings`` turns on finding grouping (``PRXREF_GROUP_FINDINGS``).
    Every chunk and the sweep are asked for a per-finding ``rule``
    (:data:`reviewer.RULE_REQUEST`, through the one
    :class:`reviewer.PromptContext`), and a model-supplied ``rule`` is kept
    only then, or while the per-rule cap below is active:
    :func:`_enforce_rule` resets it to ``None`` otherwise. After
    the hedge gate and before the quality gate,
    :func:`quality.apply_rule_grouping` folds the chunk findings that break
    one rule in one file (or, naming no rule, share a normalized title) into
    one representative, which lists the other lines after ``Also at:`` and
    in its ``locations``; the other members are kept, dropped as ``grouped
    into <file>:<line>``. Because grouping runs first, every cap counts
    groups rather than lines. Sweep findings are never grouped. One INFO line
    and one ``grouping ok`` trace event (``groups``, ``members``) report the
    pass on every run with it on, a run that formed no group included. A
    ``worker`` or ``systemic`` override in ``prompts`` with no
    ``{rule_example}`` slot after its ``## Review Context`` marker still gets
    the request, but its example finding shows no ``"rule"`` key, so one
    WARNING per run names those files and points at ``prxref prompts
    export``. Off (the default), the prompts, posts, record, trace and logs
    are exactly a run without it.

    ``max_findings_per_rule`` is the per-rule cap
    (``PRXREF_MAX_FINDINGS_PER_RULE``; the default restates
    ``config._DEFAULTS`` the way ``MAX_WORKERS`` does). It is active only
    when it is an ``int`` above 0 (a ``bool`` is not) AND a team rules file
    is loaded (``rules`` or ``scoped_rules`` is not ``None``); a cap with no
    rules file does nothing. Active, it asks every unit for a ``rule`` and
    keeps the answer exactly as ``group_findings`` does (the missing-slot
    WARNING included), with grouping on or off, and after the grouping pass
    and before the quality gate :func:`quality.apply_rule_cap` keeps at most
    that many chunk findings per rule across every file, folding the rest
    onto the best one kept (its ``locations`` and last ``Also at:``
    paragraph list them), so every severity cap counts what it kept. Folded
    findings are kept, dropped as ``rule cap exceeded (max <n>): listed at
    <file>:<line>``; a #13 group counts once; sweep findings are never
    counted or capped. The ``rule_counts`` key of every exit is
    :func:`quality.rule_cap_counts` of the pass input when the pass ran, and
    ``None`` otherwise (inactive, or an exit before the passes). One INFO
    line and one ``rulecap ok`` trace event (``cap``, ``rules``,
    ``folded``) report every pass, zeros included. Inactive, the prompts,
    posts, trace and logs are exactly a run without it, and the record
    differs only by ``rule_counts``, which is ``None``.

    ``max_warning_findings`` and ``max_outofscope_findings`` are forwarded to
    both ``apply_quality_gate`` calls, the summary-only exit's included, as
    its per-severity caps of the same names. ``None`` (the default) is
    unlimited and reads no environment variable; ``0`` drops every finding
    of that severity. ``outofscope`` is the minor severity, not the ticket
    scope ``out``.

    ``repo_context`` is the repository-context level
    (``PRXREF_REPO_CONTEXT``): ``"off"`` (the default), ``"diff"`` or
    ``"repo"`` (:data:`prxref.repo_unit.MODES`). Any other value raises
    ``ValueError`` before any forge call; config rejects it long before, so
    this guards a library caller only. ``repo_context_max_chars``
    (``PRXREF_REPO_CONTEXT_MAX_CHARS``) is each chunk's budget for it,
    ``context_contract_globs`` (``PRXREF_CONTEXT_CONTRACT_GLOBS``) selects
    the contract files, where ``()`` means none (the built-in set is
    config's default, which the CLI passes), and ``context_exclude_globs``
    (``PRXREF_CONTEXT_EXCLUDE_GLOBS``) adds to the exclude floor of
    :func:`prxref.repo_context.exclude_predicate`: an excluded path is
    never read, listed or shown. ``context_standards_globs``
    (``PRXREF_CONTEXT_STANDARDS_GLOBS``, #68) selects the repository's own
    standards documents the same way — ``()`` means none, the built-in set
    is config's default — and ``context_standards_max_chars``
    (``PRXREF_CONTEXT_STANDARDS_MAX_CHARS``, default 4000, the config
    default restated) is the per-chunk budget of
    :func:`prxref.repo_standards.standards_entries`. Standards sections are
    read ONLY at ``"repo"`` with a reader, exactly like contract files, so
    ``"off"`` and ``"diff"`` never read them. ``repo_dir`` is a
    :class:`prxref.forges.repo_dir.RepoDir` to read the repository from in
    place of the forge; this function does not validate it (``RepoDir``
    does, when it is built). ``repo_context_max_reads``
    (``PRXREF_REPO_CONTEXT_MAX_READS``, default
    :data:`prxref.repo_reader.MAX_RUN_READS`) caps the uncached reads of
    every chunk together, and ``repo_context_max_chunk_reads``
    (``PRXREF_REPO_CONTEXT_MAX_CHUNK_READS``, default
    :data:`prxref.repo_reader.MAX_CHUNK_READS`) caps each chunk's own; both
    go to the reader as ``run_cap`` and ``chunk_cap`` (#61). Off, nothing new is built or called: the
    prompts, posts, trace and logs are exactly a run without these
    arguments, and the record's ``repo_context`` key is ``None``.

    On, the run has ONE reader (:class:`prxref.repo_reader.RepoReader`):
    over ``repo_dir`` when given, else over the forge's optional
    ``get_file_content`` and ``list_paths`` at ``pr.source_sha``, and none
    when the forge cannot read or the PR has no head sha. At ``"repo"`` with
    a reader, the path listing is taken once and the contract files are
    selected once, before the chunk workers run. ``"repo"`` with no reader
    (only diff-only entries from hunk lines are left), or with a reader but
    no listing (no name search and no glob-matched contract files), logs one
    WARNING naming ``PRXREF_REPO_CONTEXT``; ``"diff"`` with no reader
    builds its entries from hunk lines and logs nothing. Each chunk's worker
    builds its context once, before its first attempt, with
    :func:`prxref.repo_unit.build_unit_context`. It reads a PR diff file
    through the reader's shared, uncapped ``read`` and every other path
    through a fresh ``chunk_reader()``, so the per-chunk read cap is spent
    on the paths outside the diff alone, and the entries do not depend on
    which chunk reads a shared diff file first. The context's definition
    lines extend the definitions block, its contract lines form a
    ``### Contract excerpts`` block, its reader lines a
    ``### Code elsewhere that reads state this chunk writes`` block and its
    standards lines (sections of the repository's own standards documents,
    #68) a last ``### In-repo standards for this chunk`` block that carries
    its own how-to-cite guidance, on the
    first attempt only: the timeout retry passes no unit, so it carries none
    of the four. A build that raises gives that chunk no context
    and one WARNING naming the chunk; the review goes on. The dependency and
    same-file definition blocks keep their own reader in every mode, so a
    diff file can be fetched once by each reader.

    The ``repo_context`` key of every exit, when on, is ``{"mode",
    "max_chars", "max_reads", "max_chunk_reads", "contract_globs",
    "exclude_globs", "standards_globs", "standards_max_chars", "reader",
    "listing", "reads", "read_cap_hit", "chunk_read_cap_hit",
    "run_read_cap_hit", "units"}``, where
    ``max_reads`` and ``max_chunk_reads`` are the two read caps. Until the
    chunk workers finish, ``reader`` and ``listing`` are ``None``, ``reads``
    is 0, the three cap flags are false and ``units`` is ``None``. After
    them, ``reader`` is the reader's ``kind`` (``"forge"`` or
    ``"repo-dir"``) or ``None``; ``listing`` (``{"paths", "complete"}`` or
    ``None``), ``reads`` and the cap flags come from one
    :meth:`~prxref.repo_reader.RepoReader.stats` snapshot, so ``reads``
    counts repository-context fetches only. ``chunk_read_cap_hit`` says a
    chunk's own cap refused a read, ``run_read_cap_hit`` says the run's cap
    did, and ``read_cap_hit`` is their OR; and ``units`` is ``{"chunks":
    [{"entries", "omitted", "retry_dropped"}, ...]}``, one row per chunk in
    chunk order, where ``retry_dropped`` is true when the timeout retry ran
    (and so ran without the chunk's repository context). The sweep has no
    row. One ``chunk context`` trace event per chunk (``index``, ``total``,
    ``entries``, ``omitted``, ``chars``) and one ``repo_context ok`` event
    per run carry the same figures, only when on.

    ``llm_parse_retries`` (``PRXREF_LLM_PARSE_RETRIES``, issue #21) is the
    parse-retry budget N handed to every chunk worker and to the sweep as
    ``parse_retries`` (see :func:`reviewer.review_chunk`). The library
    default is ``0``, which behaves exactly as 0.16.0; the CLI passes the
    config's default of ``1``. A unit whose reviewer meta carries
    ``parse_retries`` and ``first_error`` (a retry ran at N of 1 or more)
    keeps both in its worker result. The record's ``parse_retries`` is
    ``None`` unless N is an ``int`` of 1 or more, and otherwise the sum of
    those unit counts over every chunk and the sweep: ``0`` when nothing
    was retried, including an exit reached before any review unit ran.
    When the timeout retry re-runs a chunk, only the second run's count
    survives.

    ``context_followup`` (``PRXREF_CONTEXT_FOLLOWUP``, issue #22) is
    ``"off"`` (the default) or ``"on"``; any other value raises
    ``ValueError`` before any forge call, a guard for a library caller only.
    Off, nothing new is resolved, read, logged, traced or called, and the
    record's ``context_followup`` key is ``None``. On, the follow-up runs
    only at ``repo_context="repo"`` with a repository reader; otherwise the
    run logs one WARNING naming ``PRXREF_CONTEXT_FOLLOWUP`` and makes no
    follow-up call. With it active, the confidence floor is resolved once
    (``confidence_floor``, else ``PRXREF_CONFIDENCE_FLOOR``, else the
    default), and each chunk whose first attempt succeeded without the
    timeout retry goes through :func:`prxref.followup.run_chunk_followup`:
    a chunk with findings below the floor that name symbols it was not
    shown is re-sent once with their definitions appended as the last
    context block, over its own capped per-chunk reader. A chunk that took
    the timeout retry is skipped. The sweep never gets a follow-up.

    The ``context_followup`` key, when on, is ``{"active", "calls",
    "confirmed", "unconfirmed", "discarded", "input_tokens",
    "output_tokens", "chunks"}``. ``active`` is false, the counts are 0 and
    ``chunks`` is ``None`` when the gate above left it off, and on every
    exit reached before the chunk workers finish. Once they finish on an
    active run, the counts are the sums over the chunk rows and ``chunks``
    lists one row per chunk in chunk order (see
    :data:`prxref.followup.ROW_KEYS`), or ``None`` for a chunk whose worker
    left no row; one ``context_followup ok`` trace event carries the totals.

    ``suggestions`` (``PRXREF_SUGGESTIONS``, issue #30) is ``"off"`` (the
    default) or ``"on"``; any other value raises ``ValueError`` before any
    forge call, a guard for a library caller only. Off, every prompt, call,
    log line and trace event is exactly a run without it, every finding's
    ``suggestion`` is ``None`` (:func:`_enforce_suggestion`), and the
    record's ``suggestions`` key is ``None``. On, every chunk (never the
    sweep) is asked for an optional suggestion
    (:data:`reviewer.SUGGESTION_REQUEST`, through the one
    :class:`reviewer.PromptContext`, so the context follow-up's re-send
    carries it too); a ``worker`` override in ``prompts`` with no
    ``{suggestion_example}`` slot after its ``## Review Context`` marker
    still gets the request, and one WARNING per run names it. After the
    per-rule cap and before the quality gate,
    :func:`quality.apply_suggestion_validation` clears every suggestion
    that is not safe to render, against the line each finding had before
    :func:`quality.apply_line_align`, and keeps the finding. The
    ``suggestions`` key, when on, is ``{"kept": n, "cleared": {<reason>:
    n, ...}}``, one ``cleared`` entry per
    :data:`quality.SUGGESTION_CLEAR_REASONS` reason in that order, counted
    over the ACTIVE findings of the run (a dropped finding is not counted);
    it is all zeros on an exit reached before the passes.

    ``incremental`` (``PRXREF_INCREMENTAL``, issue #34) is ``"off"`` (the
    default) or ``"on"``; any other value raises ``ValueError`` before any
    forge call. Off, the run makes no extra forge read, every prompt, call,
    summary and prune is exactly a run without it, and the record's
    ``incremental`` key is ``None``. On, every summary body the run posts
    ends with :func:`reviewed_head_line` of the PR head when every review
    unit succeeded, else of the previous marker's SHA (no line when there is
    none), and with ``post_mode`` in :data:`POST_SUMMARY_MODES` the scope is
    resolved right after the diff is parsed (:func:`_resolve_incremental_scope`):
    the files of the PR's own diff touched since the previous summary's
    marker are chunked, get repository context and have their prxref inline
    comments pruned, while the systemic sweep, the size advisory and the
    deterministic checks still see every file. An empty delta runs the sweep
    alone, prunes nothing and posts the summary. An incremental run's summary
    carries one note line (:func:`_incremental_note`). The record's
    ``incremental`` key is ``{"mode", "reason", "since_sha", "files_total",
    "files_reviewed", "marker_sha"}``: ``mode`` ``"incremental"`` or
    ``"full"``, ``reason`` why a run is full (``None`` when incremental),
    ``since_sha`` the marker SHA an incremental run compared from,
    ``marker_sha`` the SHA the run's summary is stamped with (``None`` when
    none is, and whenever ``post_mode`` posts no summary).

    ``full_review`` forces a full review of an ``incremental="on"`` run
    (``prxref review --full-review``, or a ``PRXREF_FAIL_ON`` gate): scope
    resolution is skipped, so the previous summary is never read and no
    compare diff is fetched, every file is reviewed and ``prune(ref)`` runs
    as on any full run, and the summary is still stamped with the PR head
    by the rule above, so the next push is incremental again. The record's
    ``incremental`` is ``{"mode": "full", "reason": full_review_reason,
    ...}``, the reason defaulting to ``"full review requested"`` when
    ``full_review_reason`` is ``None``. Because a forced run never reads the
    previous marker, a forced run in which a review unit fails stamps no
    marker at all rather than carrying the previous head forward, so the
    next push is a full review. With ``incremental="off"`` both keywords are
    ignored.

    ``degraded`` (issue #48) records the posts that failed. It is ``None``
    when every attempted post succeeded or nothing was posted; otherwise it
    is ``{"cause", "failed", "fallback", "annotations"}``: ``failed`` lists
    ``"summary"`` and/or ``"inline"`` (a failed summary re-post counts as
    ``"summary"``), ``cause`` is ``"permission"`` when any failed post raised
    an exception whose ``response.status_code`` is 401 or 403 (the
    ``requests.HTTPError`` of a read-only token), else ``"error"``, and
    ``fallback`` is ``[]`` and ``annotations`` ``0`` here; the CLI fills
    those two after it emits the review through the CI fallback
    (:mod:`prxref.ci_fallback`). A degraded run's result also carries
    :data:`DEGRADED_SUMMARY_KEY`, the summary markdown the post would have
    carried (rendered for the fallback when the post mode posts no summary),
    which is not part of the ``--format json`` record. A run in which no post
    failed never carries that key.
    """
    if repo_context not in repo_unit.MODES:
        raise ValueError(
            f"repo_context must be one of {repo_unit.MODES}, got {repo_context!r}"
        )
    if context_followup not in FOLLOWUP_MODES:
        raise ValueError(
            f"context_followup must be one of {FOLLOWUP_MODES}, got {context_followup!r}"
        )
    if suggestions not in SUGGESTION_MODES:
        raise ValueError(
            f"suggestions must be one of {SUGGESTION_MODES}, got {suggestions!r}"
        )
    if incremental not in INCREMENTAL_MODES:
        raise ValueError(
            f"incremental must be one of {INCREMENTAL_MODES}, got {incremental!r}"
        )
    if ci_wiring not in CI_WIRING_MODES:
        raise ValueError(
            f"ci_wiring must be one of {CI_WIRING_MODES}, got {ci_wiring!r}"
        )
    t0 = time.perf_counter()
    tracer = get_tracer(trace_file)
    sampling = _sampling(llm)
    # The per-run record every exit is stamped with (_run_record). Built here
    # with every always-present key at its "off / not reached" value; each
    # stage assigns its own key as the run proceeds, so the value a return
    # carries is the one in force at that exit.
    run_inputs: dict[str, Any] = {
        "cost_usd": 0.0,
        "cost_estimated": False,
        "cost_api_equivalent": False,
        "review_rules": None,
        "ticket_context": None,
        "spec_grounding": None,
        "size_advisory": None,
        "replay": dict(replay) if replay is not None else None,
        "prompt_templates": None,
        "scoped_rules": None,
        "rule_counts": None,
        "rule_scope_cleared": None,
        "repo_context": None,
        "parse_retries": (
            0 if isinstance(llm_parse_retries, int) and llm_parse_retries >= 1 else None
        ),
        "context_followup": _followup_record() if context_followup == "on" else None,
        "suggestions": _suggestion_record() if suggestions == "on" else None,
        "incremental": _incremental_record(None, 0, None) if incremental == "on" else None,
        "ci_wiring": None,
        "evidence": None,
        "degraded": None,
        "chunks_over_budget": 0,
        "largest_chunk_tokens": 0,
        "overflow_files": 0,
        "chunk_token_budget": token_budget,
    }
    # Resolved once, before the first exit, so every exit records them and the
    # empty-diff summary gets the ticket note. An inactive (empty) ticket is
    # still recorded and still noted; it just asks the model for no scope.
    if rules is not None:
        run_inputs["review_rules"] = rules.record()
    if ticket is not None:
        run_inputs["ticket_context"] = ticket.record()
    if prompts is not None:
        run_inputs["prompt_templates"] = prompts.record()
    scoped_meta: dict[str, Any] | None = None
    if scoped_rules is not None:
        scoped_meta = {**scoped_rules.record(), "max_chars": scoped_rules_max_chars}
        run_inputs["scoped_rules"] = {**scoped_meta, "units": None}
    if repo_context != "off":
        run_inputs["repo_context"] = {
            "mode": repo_context,
            "max_chars": repo_context_max_chars,
            "max_reads": repo_context_max_reads,
            "max_chunk_reads": repo_context_max_chunk_reads,
            "contract_globs": list(context_contract_globs),
            "exclude_globs": list(context_exclude_globs),
            "standards_globs": list(context_standards_globs),
            "standards_max_chars": context_standards_max_chars,
            "reader": None,
            "listing": None,
            "reads": 0,
            "read_cap_hit": False,
            "chunk_read_cap_hit": False,
            "run_read_cap_hit": False,
            "units": None,
        }
    # Like the ticket: an EMPTY bundle (files that held no item) is still
    # recorded — the paths and the 0 items — it just reaches no prompt.
    evidence_active = evidence is not None and bool(evidence.active)
    if evidence is not None:
        run_inputs["evidence"] = {
            **evidence.record(),
            "matched_chunks": 0,
            "max_chars": evidence_max_chunk_chars,
        }
    summary_template = prompts.override("summary") if prompts is not None else ""
    ticket_active = ticket is not None and bool(ticket.active)
    ticket_note = ticket.note() if ticket is not None else ""
    if ticket_note and not ticket_note.endswith("\n"):
        ticket_note += "\n"
    tracer.event(
        "run", "start", forge=ref.forge, url=ref.url, number=ref.number,
        sampling=sampling,
        **({"replay": dict(replay)} if replay is not None else {}),
    )
    if run_inputs["review_rules"] is not None:
        tracer.event("rules", "ok", **run_inputs["review_rules"])
    if scoped_meta is not None:
        tracer.event("scoped_rules", "ok", **scoped_meta)
    if run_inputs["ticket_context"] is not None:
        tracer.event("ticket", "ok", **run_inputs["ticket_context"])
    if run_inputs["prompt_templates"] is not None:
        tracer.event("prompts", "ok", **run_inputs["prompt_templates"])

    try:
        with tracer.span("forge.get_pr"):
            pr = forge.get_pr(ref)
    except Exception as e:  # noqa: BLE001
        logger.error("get_pr failed: %s", e)
        tracer.event("run", "fail", **_cost_meta(run_inputs))
        return _run_record(_error_run(
            forge, ref, post, 0, f"get_pr failed: {e}", t0,
            post_mode=post_mode, tracer=tracer, sampling=sampling,
            cost_label=_cost_label(run_inputs, post_cost),
        ), run_inputs)

    try:
        with tracer.span("forge.get_diff") as sp:
            raw = forge.get_diff(ref)
            sp["bytes"] = len(raw.encode("utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.error("get_diff failed: %s", e)
        tracer.event("run", "fail", **_cost_meta(run_inputs))
        return _run_record(_error_run(
            forge, ref, post, 0, f"get_diff failed: {e}", t0,
            post_mode=post_mode, tracer=tracer, sampling=sampling,
            cost_label=_cost_label(run_inputs, post_cost),
        ), run_inputs)

    # Wrapped like every neighbouring stage. These two were the only ones that
    # could raise out of orchestrate_review, which made the never-raise contract
    # in the module docstring false: a library caller passing max_chunks=0 got
    # ``ValueError: min() iterable argument is empty`` instead of a review.
    # The CLI is fenced off earlier by config's range check; this closes the
    # library route and covers every other malformed-diff crash besides.
    try:
        with tracer.span("parse_diff") as sp:
            files = parse_unified_diff(raw)
            sp["files"] = len(files)
    except Exception as e:  # noqa: BLE001
        logger.error("parse_unified_diff failed: %s", e)
        tracer.event("run", "fail", **_cost_meta(run_inputs))
        return _run_record(_error_run(
            forge, ref, post, 0, f"parse_unified_diff failed: {e}", t0,
            post_mode=post_mode, tracer=tracer, sampling=sampling,
            cost_label=_cost_label(run_inputs, post_cost),
        ), run_inputs)

    scope: _IncrementalScope | None = None
    if incremental == "on":
        scope = (
            _full_scope(full_review_reason or "full review requested") if full_review
            else _resolve_incremental_scope(forge, ref, pr, files, post_mode=post_mode)
        )
        run_inputs["incremental"] = _incremental_record(scope, len(files), None)
    review_files = list(scope.delta) if scope is not None and scope.active else files
    sweep_alone = scope is not None and scope.active and bool(files)

    # Sized once, from the parsed files (never the raw diff), so every later
    # exit carries the same stats and the size line can reach all three
    # summary renders. Advisory only: a failure here is logged and the review
    # goes on without it.
    try:
        run_inputs["size_advisory"] = _size_advisory(
            files, lines_limit=size_warn_lines, files_limit=size_warn_files,
            ignore_globs=size_ignore_globs,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("size advisory failed (continuing without it): %s", e)
        run_inputs["size_advisory"] = None
    size_advisory_line = _size_advisory_line(run_inputs["size_advisory"])

    # Deterministic PR-metadata checks (#70), computed here — before any
    # worker dispatch — so the no-LLM guarantee is structural: nothing below
    # can schedule a model call on their behalf. Opt-in: with
    # metadata_rules off (the default) this block runs nothing, stamps no
    # run-record key and changes no byte of the review.
    metadata_findings: list[Finding] = []
    if metadata_rules == "on":
        commits, commit_skip = (
            _fetch_pr_commits(forge, ref, pr) if commit_reference else (None, "")
        )
        try:
            metadata_findings, stamp = run_metadata_checks(
                pr, files, commits,
                branch_patterns=branch_patterns, commit_reference=commit_reference,
                area_globs=area_globs, max_areas_per_pr=max_areas_per_pr,
                commit_skip_reason=commit_skip or "no commit source",
            )
            run_inputs["metadata_rules"] = stamp
            tracer.event("metadata_rules", "ok", **stamp)
        except Exception as e:  # noqa: BLE001
            logger.warning("metadata rules failed (continuing without them): %s", e)
            metadata_findings = []
            run_inputs["metadata_rules"] = {
                "checks": f"skipped: metadata stage failed: {e.__class__.__name__}"
            }
            tracer.event(
                "metadata_rules", "fail", reason=f"{e.__class__.__name__}: {e}"
            )

    # CI wiring (#66), computed on the same pre-dispatch doctrine: the
    # check makes no LLM call, so nothing below can schedule one on its
    # behalf. Opt-in like metadata_rules, but it READS the repository —
    # through its own reader, the forge's head-sha reads or --repo-dir,
    # never the repo_context reader (a CI file starved by that reader's
    # chunk caps would false-positive "no CI runs it") — with the module's
    # MAX_CI_FILES bound. Gated on the reader, not on repo_context: the
    # acceptance runs --repo-dir alone. A readerless run warns once and
    # records why; a crash in the stage disables it, never the review.
    ci_findings: list[Finding] = []
    if ci_wiring == "on":
        ci_reader = repo_reader.forge_reader(
            forge, ref, getattr(pr, "source_sha", "") or "",
        )
        if ci_reader is None and repo_dir is not None:
            ci_reader = repo_reader.repo_dir_reader(repo_dir)
        if ci_reader is None:
            logger.warning(CI_WIRING_INACTIVE_WARNING)
            run_inputs["ci_wiring"] = {"triggered": False, "reason": "no reader"}
            tracer.event("ci_wiring", "ok", triggered=False, reason="no reader")
        else:
            ci_listing = ci_reader.listing()
            try:
                ci_findings, ci_stamp = ci_wiring_findings(
                    files,
                    read=ci_reader.read,
                    listing=ci_listing.paths if ci_listing is not None else None,
                    globs=ci_wiring_globs,
                    ticket_text=ticket.text if ticket is not None else None,
                )
                run_inputs["ci_wiring"] = ci_stamp
                tracer.event("ci_wiring", "ok", **ci_stamp)
            except Exception as e:  # noqa: BLE001
                logger.warning("ci wiring failed (continuing without it): %s", e)
                ci_findings = []
                run_inputs["ci_wiring"] = {
                    "triggered": False,
                    "reason": f"skipped: ci wiring stage failed: {e.__class__.__name__}",
                }
                tracer.event(
                    "ci_wiring", "fail", reason=f"{e.__class__.__name__}: {e}"
                )

    try:
        with tracer.span("build_chunks") as sp:
            plan = plan_chunks(
                review_files, max_chunks=max_chunks, token_budget=token_budget,
                max_files_per_chunk=max_files_per_chunk,
            )
            chunks = plan.chunks
            sp["chunks"] = len(chunks)
        run_inputs["chunks_over_budget"] = plan.chunks_over_budget
        run_inputs["largest_chunk_tokens"] = plan.largest_chunk_tokens
        run_inputs["overflow_files"] = plan.overflow_files
    except Exception as e:  # noqa: BLE001
        logger.error("build_chunks failed: %s", e)
        tracer.event("run", "fail", **_cost_meta(run_inputs))
        return _run_record(_error_run(
            forge, ref, post, 0, f"build_chunks failed: {e}", t0,
            post_mode=post_mode, tracer=tracer, sampling=sampling,
            cost_label=_cost_label(run_inputs, post_cost),
            reviewed_head=_mark_reviewed_head(
                run_inputs, scope, pr, post_mode=post_mode, complete=False,
            ),
        ), run_inputs)

    if not chunks and not sweep_alone:
        # No chunk survived build_chunks — an empty diff, or every file
        # binary — but the release-shape and pinned-toggle heuristics are
        # pure and need no chunk to fire on: computed here so a release PR
        # whose only non-machinery file is binary still gets the
        # deterministic finding instead of a silent Approved (issue #29
        # residual, concern #2), and so does a toggle/pin pair on a diff
        # that otherwise chunks to nothing (issue #22) — though a toggle
        # needs an added, non-binary line, so it rarely fires on this path.
        release_shape = heuristics.release_shape_findings(files)
        toggle_findings = heuristics.toggle_pinned_off_findings(files)
        deterministic_findings = release_shape + toggle_findings + metadata_findings + ci_findings
        tracer.event(
            "run", "ok", chunks_reviewed=0, findings=len(deterministic_findings),
            **_cost_meta(run_inputs),
            **(_scope_counts(deterministic_findings) if ticket_active else {}),
        )
        return _run_record(_summary_only_run(
            forge, ref, pr, files, post, t0,
            post_mode=post_mode, post_verdict=post_verdict, tracer=tracer,
            sampling=sampling, release_shape_findings=release_shape,
            toggle_findings=toggle_findings,
            metadata_findings=metadata_findings,
            ci_findings=ci_findings,
            confidence_floor=confidence_floor, max_errors=max_errors,
            max_warning_findings=max_warning_findings,
            max_outofscope_findings=max_outofscope_findings,
            ticket_note=ticket_note,
            cost_label=_cost_label(run_inputs, post_cost),
            size_advisory_line=size_advisory_line,
            summary_template=summary_template,
            summary_bullet_separator=summary_bullet_separator,
            reviewed_head=_mark_reviewed_head(
                run_inputs, scope, pr, post_mode=post_mode, complete=True,
            ),
        ), run_inputs)

    # Planned once the chunks are final and before anything is written to the
    # forge, so a degenerate cap is an error run that pruned nothing.
    scoped_blocks: list[Any] | None = None
    sweep_block: Any = None
    if scoped_rules is not None:
        try:
            scoped_blocks, sweep_block = _scoped_unit_blocks(
                scoped_rules, chunks, rules, max_chars=scoped_rules_max_chars,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("scoped rules failed: %s", e)
            tracer.event("run", "fail", **_cost_meta(run_inputs))
            return _run_record(_error_run(
                forge, ref, post, 0, f"scoped rules failed: {e}", t0,
                post_mode=post_mode, tracer=tracer, sampling=sampling,
                cost_label=_cost_label(run_inputs, post_cost),
                reviewed_head=_mark_reviewed_head(
                    run_inputs, scope, pr, post_mode=post_mode, complete=False,
                ),
            ), run_inputs)
        run_inputs["scoped_rules"] = {
            **run_inputs["scoped_rules"],
            "units": {
                "chunks": [_scoped_rows(block) for block in scoped_blocks],
                "sweep": _scoped_rows(sweep_block),
            },
        }
        _warn_scoped_cap(scoped_rules, [*scoped_blocks, sweep_block], scoped_rules_max_chars)

    # Evidence blocks (#69), built before the fan-out like the scoped
    # blocks: each chunk's unit gets its matched items plus the global
    # ones inside its PromptContext copy, and the sweep's gets the global
    # ones alone (a chunk's matched items are that chunk's business, not
    # the sweep's), so its block is built against every chunk path at
    # once: an item matching ANY chunk is spent on that chunk. The ok
    # event fires here too, once matched_chunks is known — the record is
    # one claim, echoed whole. matched_chunks counts the chunks whose
    # paths matched at least one item.
    evidence_blocks: list[str] | None = None
    sweep_evidence_block = ""
    if evidence_active:
        chunk_paths = [
            [p for f in chunk for p in (f.path, f.old_path) if p] for chunk in chunks
        ]
        evidence_blocks = [
            evidence.block_for(paths, evidence_max_chunk_chars) for paths in chunk_paths
        ]
        sweep_evidence_block = evidence.global_block(
            [p for paths in chunk_paths for p in paths], evidence_max_chunk_chars,
        )
        run_inputs["evidence"]["matched_chunks"] = sum(
            1 for paths in chunk_paths if evidence.matched_for(paths)
        )
        tracer.event("evidence", "ok", **run_inputs["evidence"])
        logger.info(
            "evidence: %d item(s) from %d file(s); matched items reach %d of %d chunk(s)",
            len(evidence.items), len(evidence.files),
            run_inputs["evidence"]["matched_chunks"], len(chunks),
        )

    # Pruned BEFORE the threads are listed, and both before the review units
    # run. The prune-then-list order is load-bearing: reading threads first
    # would let this run's findings be suppressed as already-discussed against
    # prxref's OWN stale comments, which the prune then deletes. Both moved
    # ahead of the dispatch because the sweep needs the discussion in its
    # prompt, and a review that never starts has nothing to say either way.
    if post and post_mode in POST_INLINE_MODES:
        if scope is None or not scope.active:
            _prune_stale_inline_comments(forge, ref)
        elif scope.paths:
            _prune_stale_inline_comments(forge, ref, paths=scope.paths)

    # Fetched BEFORE the review units run, not after: the sweep needs the
    # existing discussion in its own prompt, and the same list serves the
    # post-hoc thread passes. Best-effort by contract — a forge that cannot
    # list threads still gets a review.
    try:
        threads = forge.list_threads(ref)
    except Exception as e:  # noqa: BLE001
        logger.warning("list_threads failed (best-effort): %s", e)
        threads = []

    # Best-effort, like the thread listing: a spec-fetch failure is data for
    # the grounding note, never a failed review. The digest is built once,
    # after parse_unified_diff (the files are the pruning input) and before
    # the worker fan-out, then rides the existing chunk + sweep calls.
    spec_digest = ""
    spec_note = ""
    fetched: list[specs.SpecSource] | None = None
    if spec_sources:
        try:
            fetched = specs.fetch_specs(
                list(spec_sources),
                max_chars=spec_max_chars,
                jira_base_url=jira_base_url,
                jira_email=jira_email,
                jira_api_token=jira_api_token,
            )
            # The note only reaches a POSTED summary, so a --no-post, dry-run,
            # or inline-only run would otherwise learn nothing about grounding.
            # Logged before the digest is built, so a crash there keeps them.
            for i, s in enumerate(fetched, start=1):
                if s.error:
                    logger.warning(
                        "spec source %d/%d (%s, %s) failed (best-effort): %s",
                        i, len(fetched), s.kind or "unknown",
                        _log_safe_origin(s.origin), redact_for_post(s.error),
                    )
            # Committed together at the end, so a crash leaves the run
            # ungrounded, which is what its record says.
            digest = specs.build_spec_digest(fetched, files, spec_digest_tokens)
            note = _spec_note(fetched, digest)
            spec_digest, spec_note = digest, note
        except Exception as e:  # noqa: BLE001
            logger.error("spec grounding failed (best-effort): %s", e)
            fetched = None
            run_inputs["spec_grounding"] = {
                "sources": len(spec_sources),
                "ok": 0,
                "failed": [f"spec stage crashed: {e.__class__.__name__}"],
                "constraints": 0,
                "digest_sha256": None,
            }
            tracer.event(
                "specs", "fail",
                sources=len(spec_sources), ok=0, constraints=0,
                reasons=[f"spec stage crashed: {e.__class__.__name__}: {e}"],
            )

    # Grounded means at least one constraint line reached the digest. A digest
    # without one (no sources, every source failed, nothing kept, or a budget
    # too small for any unit) is not injected, so every prompt shows its
    # no-specs text and forbids `spec`; apply_spec_grounding below relabels
    # any `spec` the model emits anyway.
    grounded = specs.constraint_count(spec_digest) > 0
    injected = spec_digest if grounded else ""

    # Recorded before the fan-out, so the total-failure exit carries it too.
    # The record mirrors the posted note (labels, redacted reasons); the
    # trace is operator-only and keeps the raw reasons. The hash encodes with
    # surrogatepass because a Jira body's JSON escapes can decode to a lone
    # surrogate, and this block sits outside the never-raise fence.
    if fetched is not None:
        ok = sum(1 for s in fetched if not s.error)
        constraints = specs.constraint_count(injected)
        failed = [
            (f"source {i}{f' ({s.kind})' if s.kind else ''}", s.error)
            for i, s in enumerate(fetched, start=1) if s.error
        ]
        run_inputs["spec_grounding"] = {
            "sources": len(fetched),
            "ok": ok,
            "failed": [f"{label}: {redact_for_post(error)}" for label, error in failed],
            "constraints": constraints,
            "digest_sha256": (
                hashlib.sha256(injected.encode("utf-8", "surrogatepass")).hexdigest()
                if injected else None
            ),
        }
        logger.info(
            "spec grounding: %d/%d source(s) ok, %d constraint(s) injected",
            ok, len(fetched), constraints,
        )
        tracer.event(
            "specs", "ok" if ok else "fail",
            sources=len(fetched), ok=ok, constraints=constraints,
            **({} if ok else {"reasons": [f"{label}: {error}" for label, error in failed]}),
        )

    rule_cap_active = (
        isinstance(max_findings_per_rule, int)
        and not isinstance(max_findings_per_rule, bool)
        and max_findings_per_rule > 0
        and (rules is not None or scoped_rules is not None)
    )
    rule_active = group_findings or rule_cap_active
    prompt_context = PromptContext(
        rules_worker=rules.prompt_block("worker") if rules is not None else "",
        rules_sweep=rules.prompt_block("sweep") if rules is not None else "",
        ticket_scope=ticket.scope_block() if ticket_active else "",
        ticket_context=ticket.prompt_block() if ticket_active else "",
        spec_digest=injected,
        worker_template=prompts.override("worker") if prompts is not None else "",
        systemic_template=prompts.override("systemic") if prompts is not None else "",
        rule_request=reviewer.RULE_REQUEST if rule_active else "",
        suggestion_request=reviewer.SUGGESTION_REQUEST if suggestions == "on" else "",
    )
    if rule_active and prompts is not None:
        _warn_missing_rule_slot(
            prompts, feature="finding grouping" if group_findings else "the per-rule cap",
        )
    if suggestions == "on" and prompts is not None:
        _warn_missing_suggestion_slot(prompts)
    reader = _make_file_reader(forge, ref, pr, repo_dir=repo_dir)
    repo_plan: _RepoPlan | None = None
    unit_records: list[dict[str, Any] | None] | None = None
    if repo_context != "off":
        repo_plan = _plan_repo_context(
            repo_context, forge, ref, pr, review_files, run_inputs["repo_context"],
            repo_dir=repo_dir,
        )
        unit_records = [None] * len(chunks)
    followup_floor: float | None = None
    followup_records: list[dict[str, Any] | None] | None = None
    if context_followup == "on":
        if repo_plan is not None and repo_plan.mode == "repo" and repo_plan.reader is not None:
            followup_floor = _resolve_confidence_floor(confidence_floor)
            followup_records = [None] * len(chunks)
        else:
            logger.warning(FOLLOWUP_INACTIVE_WARNING)
    results = _run_workers(
        llm, chunks, pr, max_tokens=max_tokens, max_workers=max_workers,
        context_lines=context_lines, tracer=tracer,
        reader=reader, all_files=files, trace_dir=trace_dir,
        prompt_context=prompt_context, scoped_blocks=scoped_blocks,
        repo_plan=repo_plan, unit_records=unit_records,
        parse_retries=llm_parse_retries,
        followup_floor=followup_floor, followup_records=followup_records,
        evidence_blocks=evidence_blocks,
    )
    if repo_plan is not None and unit_records is not None:
        run_inputs["repo_context"] = _repo_context_record(
            run_inputs["repo_context"], repo_plan, unit_records,
        )
        record = run_inputs["repo_context"]
        tracer.event(
            "repo_context", "ok", mode=record["mode"], reader=record["reader"],
            listing=record["listing"], reads=record["reads"],
            read_cap_hit=record["read_cap_hit"],
            chunk_read_cap_hit=record["chunk_read_cap_hit"],
            run_read_cap_hit=record["run_read_cap_hit"],
        )
    if followup_records is not None:
        run_inputs["context_followup"] = _followup_record(followup_records)
        totals = {k: v for k, v in run_inputs["context_followup"].items() if k != "chunks"}
        tracer.event("context_followup", "ok", **totals)

    # One more worker-style unit, not inside the pool: the sweep digests the
    # WHOLE diff, so it only has something to say once every chunk result —
    # including which files each chunk saw — is final. Appended to the same
    # results list, so coverage accounting, token sums, the all-failed
    # check, and the failure banner treat it exactly like a chunk.
    results.append(
        _run_sweep(
            llm, files, pr, max_tokens=max_tokens,
            token_budget=token_budget, tracer=tracer, threads=threads,
            trace_dir=trace_dir,
            prompt_context=prompt_context, scoped_block=sweep_block,
            parse_retries=llm_parse_retries,
            evidence_block=sweep_evidence_block,
        )
    )
    if run_inputs["parse_retries"] is not None:
        run_inputs["parse_retries"] = _parse_retry_total(results)

    # Priced once every review unit is final, and BEFORE the total-failure
    # exit below: requests went out, so that exit's record must say what they
    # cost rather than the pre-request 0.0. Cost accounting never fails a
    # review; a crash here leaves the cost unknown.
    try:
        _stamp_run_cost(
            run_inputs, results, {} if price_table is None else price_table,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("cost accounting failed (continuing): %s", e)
        run_inputs["cost_usd"] = None
        run_inputs["cost_estimated"] = False
        run_inputs["cost_api_equivalent"] = False
    cost_label = _cost_label(run_inputs, post_cost)

    input_tokens = sum(r["input_tokens"] for r in results)
    output_tokens = sum(r["output_tokens"] for r in results)
    model = next((r["model"] for r in results if r["model"]), "unknown")

    # Total failure is about the CHUNKS, deliberately: the sweep sees only a
    # pattern digest, so a sweep success on a dead worker pool is one unit of
    # pattern coverage over a review that never happened — it must not turn
    # that into an "Approved, no findings" run.
    if all(r["error"] for r in results[:-1]) if chunks else results[-1]["error"]:
        if chunks:
            reason = f"all {len(chunks)} worker reviews failed ({results[0]['error']})"
        else:
            reason = f"the only review unit failed ({results[-1]['error']})"
        logger.error("Total LLM failure: %s", reason)
        tracer.event("run", "fail", **_cost_meta(run_inputs))
        return _run_record(_error_run(
            forge, ref, post, len(chunks) + 1, reason, t0, tracer=tracer,
            model=model, input_tokens=input_tokens, output_tokens=output_tokens,
            post_mode=post_mode, sampling=sampling, cost_label=cost_label,
            chunks_reviewed=sum(1 for r in results if not r["error"]),
            reviewed_head=_mark_reviewed_head(
                run_inputs, scope, pr, post_mode=post_mode, complete=False,
            ),
        ), run_inputs)

    chunks_failed = sum(1 for r in results if r["error"])
    chunks_reviewed = len(results) - chunks_failed

    # Sweep findings trail the chunk findings by construction (results[:-1]
    # are the chunk workers), which is the boundary the sweep-dedup pass
    # needs after line alignment has settled every anchor.
    sweep_start = sum(
        len(r["findings"]) for r in results[:-1] if not r["error"]
    )
    findings = [f for r in results if not r["error"] for f in r["findings"]]
    findings = _enforce_scope(findings, ticket_active)
    findings = _enforce_rule(findings, rule_active)
    findings = _enforce_suggestion(findings, suggestions == "on", sweep_start=sweep_start)

    # Futures were submitted in chunk order, so results[i] is chunk[i]'s
    # outcome for i < len(chunks): the zip pairs each failed review with the
    # files it took down, which the partial banner names (issue #31). The
    # systemic sweep is results[-1] and names itself in its reason, so it is
    # not zipped against a chunk here. The rows mirror the pairs for the
    # #72 degraded record, with each unit's 1-based worker index and, for
    # the sweep, its results-list slot and an empty file list.
    failed_chunks: list[tuple[str, list[str]]] = []
    failed_chunk_rows: list[dict[str, object]] = []
    for i, (chunk, r) in enumerate(zip(chunks, results, strict=False), start=1):
        if not r["error"]:
            continue
        chunk_files = [f.path for f in chunk]
        failed_chunks.append((r["error"], chunk_files))
        failed_chunk_rows.append(
            {"index": i, "files": chunk_files, "error": r["error"]}
        )
    if results[-1]["error"]:
        failed_chunks.append((results[-1]["error"], []))
        failed_chunk_rows.append({
            "index": len(chunks) + 1, "files": [], "error": results[-1]["error"],
        })

    # Two deterministic, non-LLM findings folded in before the quality passes
    # so each flows through them like a model finding, except that
    # apply_severity_consistency leaves them out (heuristics.is_deterministic)
    # (issues #10 and #22): warning/1.0 clears apply_quality_gate trivially
    # for both. release_shape is file-level (line=0), so it survives
    # apply_line_align untouched; the toggle finding sits on the toggle's
    # own real line (> 0) instead, so apply_line_align's content pass
    # re-corroborates it like any model anchor — the body quotes the
    # toggle call itself, so the anchor holds. Both are folded in AT the
    # chunk/sweep boundary — before the sweep's own findings, not after —
    # and sweep_start moves with them: apply_sweep_dedup never drops a
    # CHUNK-side finding for a sweep-side one (its exact tier drops only
    # sweep findings). Its reworded tier, on with dedup_similarity, never
    # compares release_shape (line 0 is never compared), but DOES compare
    # the toggle finding against any other chunk-side finding sharing its
    # file and line — a chunk worker's own restatement of the same toggle
    # included — keeping only the higher-ranked one of the two (severity,
    # then confidence, then content). Appending either finding after the
    # sweep's findings would put it on the sweep side of that boundary,
    # where a chunk worker's own finding sharing its file and normalized
    # title could drop the deterministic finding as "duplicate of chunk
    # finding" and keep the model's restatement instead.
    release_shape = heuristics.release_shape_findings(files)
    toggle_findings = heuristics.toggle_pinned_off_findings(files)
    # Metadata findings (#70) join the same splice with the same
    # chunk-side standing; they are file-level (line 0) like
    # release_shape, so the reworded-dedup tier never compares them, and
    # severity consistency exempts them through the shared deterministic
    # body suffix. CI-wiring findings (#66) join them the same way —
    # file-level on the check's own path, and INLINE candidates rather
    # than summary-only: unlike the metadata findings they never thread
    # through the exclusion set below, so a genuinely unwired check
    # reaches the PR as a comment on the file that adds it.
    deterministic_findings = (
        release_shape + toggle_findings + metadata_findings + ci_findings
    )
    findings = (
        findings[:sweep_start] + deterministic_findings + findings[sweep_start:]
    )
    sweep_start += len(deterministic_findings)

    # FIRST among the passes: a team word the map knows ("blocker") would
    # otherwise die at the gate as an invalid severity, and consistency and
    # _origin_key both read the severity. 1:1 and order-preserving, so
    # sweep_start still marks the boundary. Scoped rules bring the run-wide
    # merged map, which applies with or without an always-on file.
    if scoped_rules is not None:
        severity_map = scoped_rules.merged_severity_map(rules)
    else:
        severity_map = rules.severity_map if rules is not None else None
    if severity_map:
        mapped = apply_severity_map(findings, severity_map)
        remapped = sum(
            1
            for before, after in zip(findings, mapped, strict=True)
            if before.severity != after.severity
        )
        if remapped:
            logger.info(
                "severity map: rewrote %d finding(s) from team severity words",
                remapped,
            )
            tracer.event("rules", "remap", findings=remapped)
        findings = mapped

    # Right after the map (whose tiers never include `spec`) and ahead of
    # consistency, so an ungrounded `spec` can never raise a same-title
    # sibling to spec. Covers the sweep's findings too; 1:1 and
    # order-preserving, so sweep_start still marks the boundary.
    graded = apply_spec_grounding(findings, grounded=grounded)
    relabelled = sum(
        1
        for before, after in zip(findings, graded, strict=True)
        if before.severity != after.severity
    )
    if relabelled:
        logger.info(
            "spec grounding: relabelled %d spec finding(s) as warning "
            "(no spec constraint was injected)",
            relabelled,
        )
        tracer.event("specs", "relabel", findings=relabelled)
    findings = graded

    # Evidence verdicts (#69), in spec grounding's slot: the only pass that
    # reads a finding's ``evidence`` label, and it RELABELS rather than
    # drops — the model judged the contradiction, this only enforces the
    # severity ceiling on it, so a wrong label costs severity, never the
    # finding. 1:1 and order-preserving, so sweep_start still marks the
    # boundary.
    evidence_downgraded = 0
    if evidence_active:
        downgraded = apply_evidence_verdicts(findings, evidence_active=True)
        evidence_downgraded = sum(
            1
            for before, after in zip(findings, downgraded, strict=True)
            if before.severity != after.severity
        )
        if evidence_downgraded:
            logger.info(
                "evidence: downgraded %d finding(s) the evidence contradicts to warning",
                evidence_downgraded,
            )
            tracer.event("evidence", "downgrade", findings=evidence_downgraded)
        findings = downgraded

    # The first pass that drops: an echo of the prompt's own example never
    # reaches the thread, consistency or grouping comparisons, a cap, or
    # sweep dedup, and its audit copy keeps the model's raw anchor. 1:1 and
    # order-preserving, so sweep_start still marks the boundary.
    checked = apply_example_echo_check(findings, _example_titles(prompt_context))
    echoes = sum(
        1
        for before, after in zip(findings, checked, strict=True)
        if before.drop_reason is None and after.drop_reason is not None
    )
    if echoes:
        logger.info(
            "example echo: dropped %d finding(s) titled like a prompt template's example finding",
            echoes,
        )
        tracer.event("prompts", "echo", findings=echoes)
    findings = checked

    findings = apply_location_validation(findings, [f.path for f in files])
    # BEFORE apply_line_align, deliberately: the manifest check compares the
    # model's raw anchor against the key and section it claims, and realignment
    # can move a correctly anchored claim onto a neighbouring entry first.
    # The same reader the chunk context uses serves the full-file lines that
    # name the section when the anchor's own hunk starts below its header.
    findings = apply_manifest_claim_check(findings, files, read=reader)
    model_lines = [f.line for f in findings]
    findings = apply_line_align(findings, added_lines_by_file(files), files=files)
    # AFTER line align, deliberately, on the RAW model anchor: the window is
    # centred where the model said the defect sits, not where the hunk-bounded
    # passes gave up on it (#74). A demoted (line 0) finding is exactly the
    # shape this pass exists to rescue, and re-running align after it would
    # re-demote any move beyond its 5-line tolerance.
    snapped = apply_anchor_snap(
        findings, files, read=reader, model_lines=model_lines,
    )
    anchor_moves = sum(
        1 for before, after in zip(findings, snapped, strict=True)
        if after.line != before.line
    )
    anchor_unverified = sum(
        1 for before, after in zip(findings, snapped, strict=True)
        if after.anchor_unverified and not before.anchor_unverified
    )
    if anchor_moves or anchor_unverified:
        logger.info(
            "anchor snap: moved %d anchor(s) to quoted evidence, "
            "marked %d unverified",
            anchor_moves, anchor_unverified,
        )
    findings = snapped
    before_threads = findings
    findings = apply_thread_dedup(findings, threads)
    findings = apply_settled_thread_suppression(findings, threads)
    # Both thread gates now skip resolved or outdated threads (issue #73), so
    # what they drop here was dropped against OPEN, CURRENT threads only, and
    # what they let through can still restate a subject a closed thread
    # raised — which earns the finding a previously-raised note, not a drop.
    thread_suppressed = sum(
        1
        for before, after in zip(before_threads, findings, strict=True)
        if before.drop_reason is None and after.drop_reason is not None
    )
    findings, thread_matched_resolved = _note_previously_raised(findings, threads)
    if threads:
        run_inputs["thread_dedup"] = {
            "suppressed": thread_suppressed,
            "matched_resolved": thread_matched_resolved,
            "threads_read": len(threads),
            # Threads that can no longer suppress anything: resolved OR
            # outdated, the same predicate both gates skip on.
            "threads_resolved": sum(1 for t in threads if t.resolved or t.outdated),
        }
    consistent = apply_severity_consistency(findings)
    rewrites = sum(
        1
        for before, after in zip(findings, consistent, strict=True)
        if before.severity != after.severity
    )
    if rewrites:
        logger.info(
            "severity consistency: raised %d finding(s) to their title group's max severity",
            rewrites,
        )
    findings = consistent
    findings = apply_removal_claim_check(findings, files)
    findings = apply_hedge_gate(findings, spec_digest=injected)
    # Its own guard (#75), independent of grouping and the cap: any loaded
    # rules file that declares a section scope turns the check on, so a
    # wrong label is cleared BEFORE either pass can key on it — including
    # when both are off and the label came from an override prompt.
    rule_sections = _rule_sections(rules, scoped_rules)
    if rule_sections:
        findings, cleared_labels = apply_rule_scope_check(findings, sections=rule_sections)
        logger.info(
            "rule scope: cleared %d rule label(s) that no section covers", cleared_labels,
        )
        tracer.event("rulescope", "ok", cleared=cleared_labels)
        run_inputs["rule_scope_cleared"] = cleared_labels
    if group_findings:
        findings = _group_findings(
            findings, confidence_floor=confidence_floor, sweep_start=sweep_start,
            tracer=tracer,
        )
    if rule_cap_active:
        findings, run_inputs["rule_counts"] = _cap_rules(
            findings, cap=max_findings_per_rule, confidence_floor=confidence_floor,
            sweep_start=sweep_start, tracer=tracer,
        )
    # AFTER grouping and the cap (#74): ``locations`` is final here, and the
    # verification shrinks only the representative's list — a folded member's
    # own drop reason names where it was folded, which stays true. BEFORE the
    # gate and the suggestion pass, so both read the verified locations.
    verified = apply_location_verification(findings, read=reader)
    unverified_sites = sum(
        1 for before, after in zip(findings, verified, strict=True)
        if after is not before
    )
    if unverified_sites:
        logger.info(
            "location verification: rewrote %d grouped finding(s) "
            "with an unverifiable Also-at site",
            unverified_sites,
        )
    findings = verified
    cleared_suggestions: list[tuple[Finding, str]] = []
    if suggestions == "on":
        findings, suggestion_reasons = apply_suggestion_validation(
            findings, files, model_lines=model_lines,
        )
        cleared_suggestions = [
            (f, reason)
            for f, reason in zip(findings, suggestion_reasons, strict=True)
            if reason is not None
        ]
    # The sweep boundary is positional, and the gate returns its findings in
    # content order, so _split_at_sweep re-derives the boundary from finding
    # identity and the gate's stable sort rather than carrying it across the
    # gate as an index.
    gated = apply_quality_gate(
        findings, confidence_floor=confidence_floor, max_errors=max_errors,
        max_warning_findings=max_warning_findings,
        max_outofscope_findings=max_outofscope_findings,
    )
    chunk_part, sweep_part = _split_at_sweep(gated, findings, sweep_start)
    # AFTER the gate, deliberately: the duplicate set is built from chunk
    # findings that survived it, so a sub-floor chunk finding cannot suppress
    # its higher-confidence sweep duplicate and then die at the gate itself —
    # that would lose the recall the sweep exists to add.
    findings = apply_sweep_dedup(
        chunk_part + sweep_part, sweep_start=len(chunk_part),
        similarity=dedup_similarity,
    )
    # Last, deliberately: it only decorates body text (never drop_reason or
    # severity), so it must run after every pass that keys off title/body
    # content, and running last means both the posted comment body and the
    # dropped-audit copy carry the same suffixed text.
    findings = apply_containment_note(findings)

    findings_active = sorted(active(findings), key=finding_sort_key)
    findings_dropped = sorted(
        (f for f in findings if f.drop_reason is not None), key=finding_sort_key
    )
    if suggestions == "on":
        run_inputs["suggestions"] = _suggestion_record(findings_active, cleared_suggestions)

    # Issue #72: a partial review is no longer "Approved". The failed units
    # contributed no findings, so the old expression read a review that did
    # not happen as one that found nothing. Precedence: an active
    # error-severity finding still wins over incompleteness (a real defect
    # beats a coverage gap), and the total-failure "Error" verdict left at
    # the all-failed exit above can never be shadowed from here.
    verdict = (
        "Request-Changes"
        if any(f.severity == "error" for f in findings_active)
        else "Incomplete"
        if chunks_failed
        else "Approved"
    )

    elapsed_ms = _elapsed_ms(t0)
    reviewed_head = _mark_reviewed_head(
        run_inputs, scope, pr, post_mode=post_mode, complete=chunks_failed == 0,
    )
    incremental_note = _incremental_note(scope, len(files))
    evidence_note = _evidence_note(run_inputs["evidence"], evidence_downgraded)
    posted = False
    inline_posted = 0
    post_failures: list[tuple[str, str]] = []
    fallback_summary: str | None = None
    post_summary_wanted = post and post_mode in POST_SUMMARY_MODES
    post_inline_wanted = post and post_mode in POST_INLINE_MODES
    if not post:
        # "Skipped" and "never reached" look identical in a graph that only
        # records what happened, and they mean opposite things: one is a
        # choice, the other is a failure upstream. Say which.
        tracer.event("post", "skip", reason="posting disabled (dry run or --no-post)")
    elif not (post_summary_wanted or post_inline_wanted):
        tracer.event("post", "skip", reason=f"post_mode={post_mode} posts nothing here")
    else:
        tracer.event("post", "start", mode=post_mode)
    if post_summary_wanted:
        summary = _render_summary(
            pr, files, verdict, findings_active, model,
            input_tokens, output_tokens, elapsed_ms,
            chunks_reviewed=chunks_reviewed, chunks_failed=chunks_failed,
            # Each failed chunk's reason AND file list reach the banner:
            # "findings may be incomplete" without which-files acts on nothing.
            failed_chunks=failed_chunks,
            include_verdict=post_verdict,
            spec_note=spec_note,
            ticket_note=ticket_note,
            evidence_note=evidence_note,
            cost_label=cost_label,
            size_advisory_line=size_advisory_line,
            summary_template=summary_template,
            summary_bullet_separator=summary_bullet_separator,
            thread_accounting=_thread_dedup_accounting(
                thread_suppressed, thread_matched_resolved,
            ),
            incremental_note=incremental_note,
        )
        fallback_summary = summary
        try:
            forge.post_summary(ref, _with_reviewed_head(summary, reviewed_head))
            posted = True
        except Exception as e:  # noqa: BLE001
            logger.error("post_summary failed: %s", e)
            post_failures.append(("summary", post_failure_cause(e)))
    # A summary-mode run still requires the summary to have landed before the
    # inline batch rides on it; an inline-mode run has no summary to gate on.
    inline_attempted = 0
    inline_failed = False
    # Metadata findings are summary-only by design (#70): they report on the
    # PR as a whole, not on a line, so the inline batch is selected from the
    # findings that are not them. The exclusion is an explicit identity set
    # threaded from the check that produced the findings — never a reserved
    # rule or title a model finding could accidentally match. The quality
    # passes above replace() a finding only when they change it, and none of
    # them changes one of these (file-level, deterministic-suffixed,
    # confidence 1.0); the worst case of a future pass that did is one
    # summary finding also posted inline — fail-open, never a silent loss.
    # With the feature off the set is empty and this is exactly findings_active.
    metadata_only = {id(f) for f in metadata_findings}
    inline_pool = [f for f in findings_active if id(f) not in metadata_only]
    if post_inline_wanted and inline_pool and (posted or not post_summary_wanted):
        ordered = sorted(
            inline_pool,
            key=lambda f: (
                _SEVERITY_RANK.get(f.severity, 3),
                _SCOPE_RANK.get(f.scope, 0),
                *finding_rank_key(f),
            ),
        )
        suggestion_style = getattr(forge, "suggestion_style", None)
        if not isinstance(suggestion_style, str):
            suggestion_style = None
        comments = [
            _inline_comment(f, model, suggestion_style)
            for f in ordered[:max_inline_comments]
        ]
        inline_attempted = len(comments)
        try:
            inline_posted = forge.post_inline_comments(ref, comments)
            posted = True
        except Exception as e:  # noqa: BLE001
            logger.error("post_inline_comments failed: %s", e)
            inline_failed = True
            post_failures.append(("inline", post_failure_cause(e)))

    # The summary itemizes every active finding, so when the inline pass left
    # some of them without an anchor the summary has to say so — otherwise it
    # promises a per-finding comment the PR never received. The counts only
    # exist after posting, so the disclosure rides a second post_summary call,
    # which the forges already implement as an update-in-place. Both sides
    # count the inline-eligible pool (#70: metadata findings are summary-only
    # by design, so they are neither promised nor accounted as missing).
    if (
        post_summary_wanted and posted and post_inline_wanted
        and len(inline_pool) > inline_posted
    ):
        refreshed = _render_summary(
            pr, files, verdict, findings_active, model,
            input_tokens, output_tokens, elapsed_ms,
            chunks_reviewed=chunks_reviewed, chunks_failed=chunks_failed,
            failed_chunks=failed_chunks,
            include_verdict=post_verdict,
            spec_note=spec_note,
            ticket_note=ticket_note,
            evidence_note=evidence_note,
            cost_label=cost_label,
            size_advisory_line=size_advisory_line,
            summary_template=summary_template,
            summary_bullet_separator=summary_bullet_separator,
            inline_accounting=_inline_accounting(
                len(inline_pool), inline_attempted, inline_posted,
                failed=inline_failed, cap=max_inline_comments,
            ),
            thread_accounting=_thread_dedup_accounting(
                thread_suppressed, thread_matched_resolved,
            ),
            incremental_note=incremental_note,
        )
        fallback_summary = refreshed
        try:
            forge.post_summary(ref, _with_reviewed_head(refreshed, reviewed_head))
        except Exception as e:  # noqa: BLE001
            logger.error("summary re-post with inline accounting failed: %s", e)
            post_failures.append(("summary", post_failure_cause(e)))

    degraded = _degraded_record(post_failures, failed_chunk_rows)
    if degraded is not None and fallback_summary is None:
        fallback_summary = _render_summary(
            pr, files, verdict, findings_active, model,
            input_tokens, output_tokens, elapsed_ms,
            chunks_reviewed=chunks_reviewed, chunks_failed=chunks_failed,
            failed_chunks=failed_chunks,
            include_verdict=post_verdict,
            spec_note=spec_note,
            ticket_note=ticket_note,
            evidence_note=evidence_note,
            cost_label=cost_label,
            size_advisory_line=size_advisory_line,
            summary_template=summary_template,
            summary_bullet_separator=summary_bullet_separator,
            thread_accounting=_thread_dedup_accounting(
                thread_suppressed, thread_matched_resolved,
            ),
            incremental_note=incremental_note,
        )

    if post and (post_summary_wanted or post_inline_wanted):
        tracer.event(
            "post", "ok" if posted else "fail",
            mode=post_mode, summary=post_summary_wanted and posted,
            inline=inline_posted,
        )
    tracer.event(
        "run", "ok", verdict=verdict,
        chunks_reviewed=chunks_reviewed, chunks_failed=chunks_failed,
        findings=len(findings_active),
        **_cost_meta(run_inputs),
        **(_scope_counts(findings_active) if ticket_active else {}),
    )
    return _run_record(_with_degraded({
        "verdict": verdict,
        "findings_active": findings_active,
        "findings_dropped": findings_dropped,
        "chunk_count": len(chunks) + 1,
        "chunks_reviewed": chunks_reviewed,
        "chunks_failed": chunks_failed,
        # Only on a partial run, like "degraded": a clean record must keep
        # the key set the golden tests pin (#72).
        **({"failed_chunks": failed_chunks} if failed_chunks else {}),
        "elapsed_ms": elapsed_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "posted": posted,
        "sampling": _sampling(llm),
    }, degraded, fallback_summary), run_inputs)


def _origin_key(finding: Finding) -> tuple:
    """Identity used to re-derive the chunk/sweep boundary across the gate.

    The file, line, title, body, severity, confidence, rule and scope, in
    that order: ``rule`` sits before ``scope``, which stays the last
    element. :func:`quality.apply_quality_gate` rewrites none of them but
    ``severity``, which it trims and lower-cases, so the key holds the
    severity in that form and a finding has the same key on both sides of
    the gate, which :func:`_split_at_sweep` relies on. A key built from the
    raw severity changed across the gate for a model that wrote
    ``Warning``, and the sweep's copy was posted as a second comment.

    ``severity`` and ``confidence`` are part of the key, so a chunk finding
    and a sweep finding that agree on file, line, title and body but not on
    those never share one and neither can take the other's slot, the
    higher-confidence chunk copy included. ``scope`` is in it for the same
    reason: with a ticket active the two copies can disagree on it. So is
    ``rule``. ``drop_reason`` and ``locations`` are not: they are state a
    pass sets on one copy and not the other, not identity.
    """
    return (
        finding.file,
        finding.line,
        finding.title,
        finding.body,
        (finding.severity or "").strip().lower(),
        finding.confidence,
        finding.rule,
        finding.scope,
    )


def _split_at_sweep(
    gated: Sequence[Finding], before: Sequence[Finding], sweep_start: int
) -> tuple[list[Finding], list[Finding]]:
    """Split the quality gate's output back into chunk findings and sweep findings.

    ``before`` is the list :func:`quality.apply_quality_gate` was given,
    whose first ``sweep_start`` findings came from the chunk workers, and
    ``gated`` is what it returned. Returns ``(chunk findings, sweep
    findings)``, each in ``gated`` order.

    The gate sorts by :func:`quality.finding_sort_key`, whose fields every
    :func:`_origin_key` holds, and the sort is stable, so the findings that
    share a key leave the gate in the order they entered it: every chunk
    copy of a key ahead of every sweep copy. The first copies of each key,
    as many as the chunk side had, are therefore the chunk findings, and
    the next, as many as the sweep side had, the sweep's. That holds
    whatever a pass set on one copy and not the other: grouping or the
    per-rule cap dropping the chunk copy before the gate, or a severity cap
    in the gate keeping the chunk copy and dropping the sweep copy. A
    finding whose key neither side had is filed with the chunk findings,
    so it is never dropped as a sweep duplicate.
    """
    chunk_left = Counter(_origin_key(f) for f in before[:sweep_start])
    sweep_left = Counter(_origin_key(f) for f in before[sweep_start:])
    chunk_part: list[Finding] = []
    sweep_part: list[Finding] = []
    for f in gated:
        key = _origin_key(f)
        if chunk_left[key] > 0:
            chunk_left[key] -= 1
            chunk_part.append(f)
        elif sweep_left[key] > 0:
            sweep_left[key] -= 1
            sweep_part.append(f)
        else:
            chunk_part.append(f)
    return chunk_part, sweep_part


def _enforce_scope(findings: Sequence[Finding], active: bool) -> list[Finding]:
    """Hold every finding's ``scope`` to what the run asked the model for.

    With no active ticket the prompts never asked for a scope, so any value
    other than ``unknown`` — from a test double, a library reviewer, or a
    future backend that bypasses the reviewer's own gate — is reset to
    ``unknown``. With one active, the value is normalized
    (:func:`triage.normalize_scope`), so an unrecognised one is ``unknown``
    too. Returns a new list in the same order; only a finding whose scope
    changes is replaced, with :func:`dataclasses.replace`.
    """
    out: list[Finding] = []
    for f in findings:
        scope = normalize_scope(f.scope) if active else SCOPE_UNKNOWN
        out.append(f if scope == f.scope else replace(f, scope=scope))
    return out


def _enforce_rule(findings: Sequence[Finding], active: bool) -> list[Finding]:
    """Hold every finding's ``rule`` to what the run asked the model for.

    The ``rule`` twin of :func:`_enforce_scope`. When the prompts never asked
    for a rule, any value (from a test double, a library reviewer, or a
    backend that bypasses the reviewer's own gate) is reset to ``None``. When
    they did, the value is normalized (:func:`triage.normalize_rule`), so an
    unusable one is ``None`` too. Returns a new list in the same order; only a
    finding whose rule changes is replaced, with :func:`dataclasses.replace`.
    """
    out: list[Finding] = []
    for f in findings:
        rule = normalize_rule(f.rule) if active else None
        out.append(f if rule == f.rule else replace(f, rule=rule))
    return out


def _example_titles(prompt_context: PromptContext) -> tuple[str, ...]:
    """The example-finding titles of the worker and sweep templates this run rendered.

    Each template is the one the review units rendered: the override text
    ``prompt_context`` carries (``worker_template`` / ``systemic_template``),
    else the packaged file, read through
    :func:`prompt_templates.packaged_text` rather than
    ``reviewer.load_prompt``, which the orchestrator asks for the summary
    template only. The titles come from :func:`quality.prompt_example_titles`
    and feed :func:`quality.apply_example_echo_check`. A packaged template
    that cannot be read contributes no title and logs one WARNING, so this
    never raises out of the review.
    """
    texts: list[str] = []
    for override, name in (
        (prompt_context.worker_template, "worker"),
        (prompt_context.systemic_template, "systemic"),
    ):
        if override:
            texts.append(override)
            continue
        try:
            texts.append(packaged_text(name))
        except (OSError, ValueError) as e:
            logger.warning("example echo check: cannot read packaged %s.md (continuing without it): %s", name, e)
    return prompt_example_titles(*texts)


def _rule_sections(rules: Any, scoped_rules: Any) -> tuple[Any, ...]:
    """Every scoped section of the loaded rules files, always-on first (#75).

    ``rules`` is the always-on ``PRXREF_REVIEW_RULES`` file and
    ``scoped_rules`` the path-scoped files, either possibly ``None``; the
    sections are read duck-typed off ``sections``, and an object without
    the attribute — a hand-constructed double or a pre-#75 stand-in —
    reads as none, so the applicability check never fires on it. The
    result is what guards the :func:`quality.apply_rule_scope_check` call:
    empty (check skipped, ``rule_scope_cleared`` stays ``None``) unless at
    least one loaded file declares a ``scope:`` line under an ATX heading.
    """
    sections: tuple[Any, ...] = ()
    if rules is not None:
        sections += getattr(rules, "sections", ()) or ()
    if scoped_rules is not None:
        for rules_file in getattr(scoped_rules, "files", ()) or ():
            sections += getattr(rules_file, "sections", ()) or ()
    return sections


def _group_findings(
    findings: Sequence[Finding],
    *,
    confidence_floor: float | None,
    sweep_start: int,
    tracer: Tracer,
) -> list[Finding]:
    """Run :func:`quality.apply_rule_grouping` and report what it folded.

    Called only with grouping on, after the hedge gate and before the quality
    gate, while the positional ``sweep_start`` is still valid (every pass
    before it is 1:1 and order-preserving), with the ``confidence_floor`` the
    gate gets. ``members`` counts the findings the pass dropped as
    ``grouped into <file>:<line>`` (:data:`quality.GROUPED_INTO_PREFIX`).
    ``groups`` counts representatives: the pass rewrites each one and passes
    every finding it does not fold through as the same object, so an active
    finding that is no longer the same object is one. Counting distinct drop
    reasons instead would merge two groups anchored on the same line. Both
    counts reach one INFO line and one ``grouping ok`` trace event, zeros
    included.
    """
    grouped = apply_rule_grouping(
        findings, confidence_floor=confidence_floor, sweep_start=sweep_start,
    )
    pairs = list(zip(findings, grouped, strict=True))
    groups = sum(
        1 for before, after in pairs
        if after is not before and after.drop_reason is None
    )
    members = sum(
        1 for before, after in pairs
        if before.drop_reason is None
        and isinstance(after.drop_reason, str)
        and after.drop_reason.startswith(GROUPED_INTO_PREFIX)
    )
    logger.info(
        "finding grouping: formed %d group(s), folding %d finding(s) into them",
        groups, members,
    )
    tracer.event("grouping", "ok", groups=groups, members=members)
    return grouped


def _cap_rules(
    findings: Sequence[Finding],
    *,
    cap: int,
    confidence_floor: float | None,
    sweep_start: int,
    tracer: Tracer,
) -> tuple[list[Finding], list[dict]]:
    """Run :func:`quality.apply_rule_cap` and report what it folded.

    Called only with the per-rule cap active, after the grouping pass and
    before the quality gate, while the positional ``sweep_start`` is still
    valid (every pass before it is 1:1 and order-preserving), with the
    ``confidence_floor`` the gate gets. Returns the capped findings and
    :func:`quality.rule_cap_counts` of the INPUT, the run record's
    ``rule_counts``. ``folded`` counts the findings the pass dropped as
    ``rule cap exceeded (max <n>): listed at <file>:<line>``
    (:data:`quality.RULE_CAP_PREFIX`); ``rules`` counts the rows of those
    counts whose ``total`` is over ``cap``. Both reach one INFO line and one
    ``rulecap ok`` trace event, with ``cap``, zeros included.
    """
    counts = rule_cap_counts(
        findings, cap=cap, confidence_floor=confidence_floor, sweep_start=sweep_start,
    )
    capped = apply_rule_cap(
        findings, cap=cap, confidence_floor=confidence_floor, sweep_start=sweep_start,
    )
    folded = sum(
        1 for before, after in zip(findings, capped, strict=True)
        if before.drop_reason is None
        and isinstance(after.drop_reason, str)
        and after.drop_reason.startswith(RULE_CAP_PREFIX)
    )
    rules = sum(1 for row in counts if row["total"] > cap)
    logger.info(
        "rule cap: folded %d finding(s) past %d per rule, across %d rule(s)",
        folded, cap, rules,
    )
    tracer.event("rulecap", "ok", cap=cap, rules=rules, folded=folded)
    return capped, counts


def _warn_missing_rule_slot(prompts: PromptTemplates, *, feature: str = "finding grouping") -> None:
    """Warn once when the rule request is on and a review override has no ``{rule_example}``.

    ``feature`` names what turned the request on (``finding grouping``, or
    ``the per-rule cap`` when only the cap did) and opens the message. Such
    an override still gets :data:`reviewer.RULE_REQUEST` and its
    ``rule`` answers are still kept, but its ``## Output Format`` example
    finding shows no ``"rule"`` key. Only the text after
    :data:`prompt_templates.CONTEXT_MARKER` is filled, so a slot above the
    marker does not count. One WARNING names every such ``worker`` or
    ``systemic`` file and points at ``prxref prompts export``; nothing is
    raised. :func:`prompt_templates.load_prompt_templates` stays silent about
    the slot, because it cannot know whether the request is on.
    """
    missing = [
        f.path
        for f in getattr(prompts, "overrides", ())
        if f.name in REVIEW_TEMPLATES
        and "rule_example" not in placeholders(f.text.partition(CONTEXT_MARKER)[2])
    ]
    if missing:
        logger.warning(
            "%s is on, but prompt template override(s) %s have no "
            "{rule_example} slot after %r, so their example finding shows no "
            "\"rule\" key; re-export with `prxref prompts export DIR --force` "
            "and re-apply your edits to pick the slot up",
            feature, ", ".join(missing), CONTEXT_MARKER,
        )


def _scope_counts(findings: Sequence[Finding]) -> dict[str, int]:
    """The ``run ok`` event's ``scope_in`` / ``scope_out`` / ``scope_unknown``."""
    counts = Counter(f.scope for f in findings)
    return {
        "scope_in": counts[SCOPE_IN],
        "scope_out": counts[SCOPE_OUT],
        "scope_unknown": counts[SCOPE_UNKNOWN],
    }


def _sampling(llm: object) -> dict:
    """Report the sampling knobs a client had in force, duck-typed.

    A client that exposes none of them still yields the same three keys, so a
    run record never has to be read as "absent means default".
    """
    return {
        "temperature": getattr(llm, "temperature", None),
        "seed": getattr(llm, "seed", None),
        "models": list(getattr(llm, "models", []) or []),
    }


def _elapsed_ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _run_record(result: dict, run_inputs: Mapping[str, Any]) -> dict:
    """Stamp one exit's result with the per-run record; the single choke point.

    Every return of :func:`orchestrate_review` goes through here, so a
    run-record key is added once instead of at each exit, and no exit can be
    missed. Each key of ``run_inputs`` is copied in with ``setdefault``
    semantics — a key the exit's own dict already carries wins — except
    ``replay``, which is written only when it is not ``None``: a normal run's
    record has no ``replay`` key at all, and a replay's is a copy of the
    stamp, never the caller's mapping. ``cost_api_equivalent`` is written
    only when it is ``True``, so a run not priced by claude-cli has the same
    record it had before the label existed. Returns ``result`` itself.
    """
    for key, value in run_inputs.items():
        if key == "replay":
            if value is not None:
                result.setdefault(key, dict(value))
        elif key == "cost_api_equivalent":
            if value is True:
                result.setdefault(key, True)
        else:
            result.setdefault(key, value)
    return result


PERMISSION_STATUSES = frozenset({401, 403})
"""HTTP statuses a failed post is classified as ``"permission"`` for."""


def post_failure_cause(exc: BaseException) -> str:
    """Classify one failed forge post: ``"permission"`` or ``"error"``.

    ``"permission"`` when the exception carries a ``response`` whose
    ``status_code`` is 401 or 403, which is how every forge adapter's
    ``raise_for_status`` reports a token that may read but not write;
    ``"error"`` for anything else, a transport failure or a 5xx included.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return "permission" if status in PERMISSION_STATUSES else "error"


def _degraded_record(
    failures: Sequence[tuple[str, str]],
    failed_chunk_rows: Sequence[Mapping[str, object]] = (),
) -> dict | None:
    """The run record's ``degraded`` value from failed posts and failed chunks.

    ``None`` when nothing failed. ``failed`` keeps each post kind once, in
    the order it first failed, after ``"chunk"`` when any review unit
    failed (the chunk fails before any post is attempted). ``cause`` is
    ``"permission"`` when any post failure was (a blocked write outranks
    everything: it is the one the operator must act on), else ``"partial"``
    when a chunk failed (issue #72: the review only partially happened),
    else ``"error"``. ``fallback`` and ``annotations`` start empty for the
    CLI to fill. ``chunks`` carries one ``{"index", "files", "error"}`` row
    per failed review unit, and appears only when at least one did, so the
    #48 post-failure shape is unchanged.
    """
    if not failures and not failed_chunk_rows:
        return None
    failed: list[str] = []
    if failed_chunk_rows:
        failed.append("chunk")
    for kind, _ in failures:
        if kind not in failed:
            failed.append(kind)
    if any(c == "permission" for _, c in failures):
        cause = "permission"
    elif failed_chunk_rows:
        cause = "partial"
    else:
        cause = "error"
    record: dict[str, object] = {
        "cause": cause, "failed": failed, "fallback": [], "annotations": 0,
    }
    if failed_chunk_rows:
        record["chunks"] = [dict(row) for row in failed_chunk_rows]
    return record


def _with_degraded(result: dict, degraded: dict | None, summary: str | None) -> dict:
    """Stamp a degraded exit with ``degraded`` and :data:`DEGRADED_SUMMARY_KEY`.

    A run with no failed post is returned untouched, so it takes the
    ``None`` default from :func:`_run_record`. Returns ``result`` itself.
    """
    if degraded is not None:
        result["degraded"] = degraded
        result[DEGRADED_SUMMARY_KEY] = summary or ""
    return result


def _cost_meta(run_inputs: Mapping[str, Any]) -> dict[str, Any]:
    """The cost keys every ``run ok`` / ``run fail`` trace event carries."""
    return {
        "cost_usd": run_inputs.get("cost_usd"),
        "cost_estimated": run_inputs.get("cost_estimated") is True,
    }


def _cost_label(run_inputs: Mapping[str, Any], post_cost: bool) -> str:
    """The attribution's cost field, or ``""`` when ``post_cost`` is off.

    ``""`` keeps every attribution byte-identical to a run without cost
    posting; otherwise it is :func:`prxref.costs.cost_label` of the cost in
    force at this exit (``$0.00`` before any LLM request, ``cost unknown``
    when the run's cost could not be established, and ``$0.0007
    (API-equivalent)`` when ``cost_api_equivalent`` is set).
    """
    if not post_cost:
        return ""
    return costs.cost_label(
        run_inputs.get("cost_usd"), run_inputs.get("cost_estimated") is True,
        api_equivalent=run_inputs.get("cost_api_equivalent") is True,
    )


def _stamp_run_cost(
    run_inputs: dict,
    units: Sequence[Mapping[str, Any]],
    price_table: Mapping[str, Any],
) -> None:
    """Set ``run_inputs["cost_usd"]``, ``["cost_estimated"]`` and ``["cost_api_equivalent"]``.

    Called once, after the sweep, with every review unit's result (the chunk
    workers plus the sweep) and the parsed price table (``{}`` when unset).
    The total is :func:`prxref.costs.run_cost`: each received unit's reported
    cost, else a price-table estimate for its exact model name when the unit
    counted input tokens (a unit reporting 0, as every kiro-cli unit does, is
    never estimated), else the whole run is unknown (``None``, never ``0``
    and never a partial sum). A run
    left unknown by models with neither figure logs one INFO line naming
    them, so a table keyed on the wrong model name diagnoses itself. A table
    that is not a valid parsed table raises, and the caller records the cost
    as unknown. ``cost_api_equivalent`` is
    :func:`prxref.costs.api_equivalent_run` over the same units, derived here
    once so the attribution and the CLI's ``-v`` line cannot disagree.
    """
    cost_usd, cost_estimated, unpriced = costs.run_cost(units, price_table)
    if unpriced:
        logger.info(
            "cost unknown: no reported cost and no usable PRXREF_PRICE_TABLE "
            "estimate for model(s) %s",
            ", ".join(repr(m) for m in unpriced),
        )
    run_inputs["cost_usd"] = cost_usd
    run_inputs["cost_estimated"] = cost_estimated
    run_inputs["cost_api_equivalent"] = costs.api_equivalent_run(units)


def _parse_retry_total(units: Sequence[Mapping[str, Any]]) -> int:
    """The run record's ``parse_retries``: the sum of every unit's own count (issue #21).

    Called once, after the sweep, with every review unit's result (the chunk
    workers plus the sweep). A unit without the key made no parse retry and
    adds 0, and so does a value that is not an ``int`` (a ``bool`` included),
    so a malformed test double can never raise out of the review.
    """
    total = 0
    for unit in units:
        value = unit.get("parse_retries", 0)
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


def _attribution(
    model: str, tokens: int, elapsed_ms: int, *, cost_label: str = "",
) -> str:
    """The attribution line every posted comment carries.

    ``cost_label`` (``"$0.0007"``, ``"$0.0007 (API-equivalent)"``,
    ``"~$0.0007 (est.)"``, ``"cost unknown"``)
    is appended as the LAST field, and only when non-empty: the existing
    fields keep their order, so a consumer that parses ``model=`` or the
    token count, and the prune pass that matches ``ATTRIBUTION_MARKER`` as a
    prefix, see the same line whether or not cost is posted.
    """
    line = f"{ATTRIBUTION_MARKER} · model={model} · {tokens} tok · {elapsed_ms / 1000:.1f}s"
    return f"{line} · {cost_label}" if cost_label else line


def _prune_stale_inline_comments(
    forge: Forge, ref: PRRef, *, paths: frozenset[str] | None = None,
) -> None:
    """Call the forge's optional stale-inline cleanup, before dedup reads threads.

    The order is load-bearing: pruning after ``list_threads`` would let the
    thread dedup suppress this run's findings as already-discussed and then
    delete the very comments it suppressed them against, removing the finding
    from the PR entirely. The capability is optional — forges without it and
    the duck-typed test fakes are skipped via getattr — and best-effort: a
    prune failure is logged, never raised, because cleanup must not abort the
    review that follows it. ``paths`` (an incremental run's re-reviewed files,
    issue #34) is passed as the keyword ``paths`` so only those files'
    comments go; ``None`` calls ``prune(ref)`` exactly as before.
    """
    prune = getattr(forge, "prune_inline_comments", None)
    if not callable(prune):
        return
    try:
        removed = prune(ref) if paths is None else prune(ref, paths=paths)
    except Exception as e:  # noqa: BLE001
        logger.warning("prune_inline_comments failed (best-effort): %s", e)
        return
    if removed:
        logger.info("pruned %d stale inline comment(s) before posting", removed)


@dataclass(frozen=True)
class _IncrementalScope:
    """What a ``PRXREF_INCREMENTAL=on`` run reviews (issue #34).

    ``mode`` is ``"incremental"`` or ``"full"``; ``reason`` says why a run is
    full and is ``None`` when it is incremental. ``since_sha`` is the marker
    SHA an incremental run compared from. ``previous_sha`` is the marker SHA
    read from the previous summary, whatever the mode, so a run whose review
    units did not all succeed can carry it forward. ``delta`` is the PR diff's
    files touched since the marker (empty on a full run) and ``paths`` their
    new and old paths.
    """

    mode: str
    reason: str | None = None
    since_sha: str | None = None
    previous_sha: str | None = None
    delta: tuple = ()
    paths: frozenset[str] = frozenset()

    @property
    def active(self) -> bool:
        """True when only ``delta`` is chunked."""
        return self.mode == "incremental"


def reviewed_head_line(sha: str) -> str:
    """The reviewed-head marker line for ``sha``, which :data:`REVIEWED_HEAD_RE` reads back."""
    return f"{REVIEWED_HEAD_PREFIX}{sha}{REVIEWED_HEAD_SUFFIX}"


def _with_reviewed_head(body: str, sha: str | None) -> str:
    """``body`` with :func:`reviewed_head_line` appended after a blank line, or unchanged for ``None``."""
    return body if sha is None else f"{body}\n\n{reviewed_head_line(sha)}"


def _full_scope(reason: str, previous_sha: str | None = None) -> _IncrementalScope:
    """A full-review scope, with its reason logged at INFO."""
    logger.info("PRXREF_INCREMENTAL=on: reviewing every file (%s)", reason)
    return _IncrementalScope("full", reason=reason, previous_sha=previous_sha)


def _resolve_incremental_scope(
    forge: Forge, ref: PRRef, pr: PRData, files: Sequence[Any], *, post_mode: str,
) -> _IncrementalScope:
    """Decide what a ``PRXREF_INCREMENTAL=on`` run reviews; never raises.

    Incremental only with ``post_mode`` in :data:`POST_SUMMARY_MODES` (an
    inline-only run never writes the marker). The previous summary is read
    through the forge's optional ``get_summary``; a forge without it, a read
    that raises (a WARNING naming the exception type), no summary ("first
    review") and a summary without a :data:`REVIEWED_HEAD_RE` marker each
    give a full review. A marker equal to the PR head gives an empty delta.
    Otherwise ``get_compare_diff(ref, base_sha=<marker>, head_sha=<head>)``
    supplies the set of new and old paths touched since the marker; a
    failure there (a force-push can make the old SHA unknown) gives a full
    review with a WARNING, and ``""`` an empty delta. The delta is the
    members of ``files`` (the PR's own diff, so inline positions stay right)
    whose path or old path is in that set, which keeps a merge from the base
    branch or a rebase from widening it.
    """
    if post_mode not in POST_SUMMARY_MODES:
        return _full_scope(f"PRXREF_POST_MODE={post_mode} never writes the reviewed-head marker")
    get_summary = getattr(forge, "get_summary", None)
    if not callable(get_summary):
        return _full_scope("forge cannot read its summary")
    try:
        body = get_summary(ref)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "PRXREF_INCREMENTAL=on: reading the previous summary failed (%s); "
            "reviewing every file", e.__class__.__name__,
        )
        return _IncrementalScope("full", reason="previous summary could not be read")
    if body is None:
        return _full_scope("first review")
    match = REVIEWED_HEAD_RE.search(body) if isinstance(body, str) else None
    if match is None:
        return _full_scope("previous summary has no reviewed-head marker")
    since = match.group(1)
    head = (getattr(pr, "source_sha", "") or "").lower()
    if not head:
        return _full_scope("PR head is unknown", since)
    if head == since or head.startswith(since):
        logger.info("PRXREF_INCREMENTAL=on: nothing changed since %s", since[:7])
        return _IncrementalScope("incremental", since_sha=since, previous_sha=since)
    compare = getattr(forge, "get_compare_diff", None)
    if not callable(compare):
        return _full_scope("forge cannot compare commits", since)
    try:
        raw = compare(ref, base_sha=since, head_sha=pr.source_sha)
        touched = parse_unified_diff(raw) if raw else []
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "PRXREF_INCREMENTAL=on: the compare diff since %s failed (%s: %s); "
            "reviewing every file", since[:7], e.__class__.__name__, e,
        )
        return _IncrementalScope("full", reason="compare diff failed", previous_sha=since)
    changed = {p for f in touched for p in (f.path, f.old_path) if p}
    delta = tuple(f for f in files if f.path in changed or (f.old_path or "") in changed)
    paths = frozenset(p for f in delta for p in (f.path, f.old_path) if p)
    logger.info(
        "PRXREF_INCREMENTAL=on: re-reviewing %d of %d changed file(s) since %s",
        len(delta), len(files), since[:7],
    )
    return _IncrementalScope(
        "incremental", since_sha=since, previous_sha=since, delta=delta, paths=paths,
    )


def _incremental_record(
    scope: _IncrementalScope | None, files_total: int, marker_sha: str | None,
) -> dict[str, Any]:
    """The run record's ``incremental`` value for a ``PRXREF_INCREMENTAL=on`` run.

    ``scope`` ``None`` is an exit taken before the scope was resolved: a full
    run of no files with the reason ``"review ended before scope
    resolution"``.
    """
    if scope is None:
        return {
            "mode": "full", "reason": "review ended before scope resolution",
            "since_sha": None, "files_total": 0, "files_reviewed": 0,
            "marker_sha": marker_sha,
        }
    return {
        "mode": scope.mode,
        "reason": scope.reason,
        "since_sha": scope.since_sha,
        "files_total": files_total,
        "files_reviewed": len(scope.delta) if scope.active else files_total,
        "marker_sha": marker_sha,
    }


def _mark_reviewed_head(
    run_inputs: dict[str, Any], scope: _IncrementalScope | None, pr: PRData,
    *, post_mode: str, complete: bool,
) -> str | None:
    """The SHA this exit's summary is stamped with, also written to the record.

    ``None`` whenever ``scope`` is ``None`` (``PRXREF_INCREMENTAL`` off, the
    record left untouched) or ``post_mode`` posts no summary. Otherwise the
    PR head, lowercased, when ``complete`` (every review unit of the run
    succeeded) and the head is a hex SHA :data:`REVIEWED_HEAD_RE` can read
    back; else the previous marker's SHA, so the files a failed unit covered
    are re-reviewed on the next push, or ``None`` when there was none.
    """
    if scope is None:
        return None
    sha: str | None = None
    if post_mode in POST_SUMMARY_MODES:
        head = (getattr(pr, "source_sha", "") or "").lower()
        if complete and REVIEWED_HEAD_RE.fullmatch(reviewed_head_line(head)):
            sha = head
        else:
            sha = scope.previous_sha
    record = run_inputs.get("incremental")
    if isinstance(record, dict):
        run_inputs["incremental"] = {**record, "marker_sha": sha}
    return sha


def _incremental_note(scope: _IncrementalScope | None, files_total: int) -> str:
    """The summary's one-line note on an incremental run, ``""`` on any other run."""
    if scope is None or not scope.active:
        return ""
    return (
        f"> Incremental review: {len(scope.delta)} of {files_total} changed "
        f"{_plural(files_total, 'file')} re-reviewed since `{(scope.since_sha or '')[:7]}`; "
        "the systemic sweep saw the whole PR, and earlier inline comments on the "
        "other files still stand.\n\n"
    )


def _inline_accounting(
    active: int, attempted: int, posted: int, *, failed: bool, cap: int
) -> str:
    """Render the claimed-vs-posted inline reconciliation line.

    The summary itemizes every active finding; when the inline pass leaves
    some of them without an anchor — the cap, a forge that rejected the
    position, or a failed batch — this line keeps the summary's promise
    honest instead of silently itemizing comments that never landed.
    """
    if failed:
        return (
            f"Inline comments: posting failed — 0 of {active} findings have one."
        )
    reasons: list[str] = []
    capped = max(0, active - attempted)
    rejected = max(0, attempted - posted)
    if capped:
        reasons.append(f"{capped} over the {cap}-comment cap")
    if rejected:
        plural = "s" if rejected != 1 else ""
        reasons.append(f"{rejected} anchor{plural} rejected by the forge")
    detail = " · ".join(reasons) if reasons else "unposted"
    return f"Inline comments: {posted} of {active} findings ({detail})."


def _thread_dedup_accounting(suppressed: int, matched_resolved: int) -> str:
    """Render the thread-dedup reconciliation line (issue #73).

    The summary used to be silent about both halves of the thread gates: a
    finding suppressed as a duplicate vanished without a trace, and a finding
    that matched a resolved thread was re-posted with nothing saying it had
    history. This line names both counts; ``""`` when neither happened, so a
    run without thread history keeps a byte-identical summary. "Resolved"
    here covers outdated threads too — anything the gates skip.
    """
    if not suppressed and not matched_resolved:
        return ""
    return (
        f"Thread dedup: {suppressed} suppressed as duplicates of open "
        f"threads; {matched_resolved} matched resolved threads and are "
        f"posted below."
    )


def _thread_reference(t: Thread) -> str:
    """How a previously-raised note names one thread: its URL when the forge
    reported one (GitHub's GraphQL read does), else ``thread by <author> at
    <path>:<line>`` with the pieces a forge without permalinks can still
    give."""
    if t.url:
        return t.url
    who = t.author or "unknown"
    if t.path:
        if isinstance(t.line, int) and t.line > 0:
            return f"thread by {who} at {t.path}:{t.line}"
        return f"thread by {who} at {t.path}"
    return f"thread by {who} on the PR"


def _note_previously_raised(
    findings: Sequence[Finding], threads: Sequence[Thread]
) -> tuple[list[Finding], int]:
    """Stamp surviving findings that restate a closed thread (issue #73).

    For every finding that survived both thread gates, the first resolved or
    outdated thread that matches it under either gate's rule earns the
    finding a ``previous_thread`` sentence — ``Previously raised in <ref>;
    still present at <file>:<line>.`` — which renders as a suffix on the
    posted inline body and the summary bullet. Pure, 1:1 and
    order-preserving; already-dropped findings pass through untouched.
    Returns the new list and the count of findings stamped.
    """
    if not any(t.resolved or t.outdated for t in threads):
        return list(findings), 0
    result: list[Finding] = []
    noted = 0
    for f in findings:
        if f.drop_reason is not None:
            result.append(f)
            continue
        t = previously_discussed_thread(f, threads)
        if t is None:
            result.append(f)
            continue
        where = f"{f.file}:{f.line if f.line > 0 else '—'}"
        result.append(replace(
            f,
            previous_thread=(
                f"Previously raised in {_thread_reference(t)}; "
                f"still present at {where}."
            ),
        ))
        noted += 1
    return result, noted


HEARTBEAT_SECONDS = 30.0


def _fetch_pr_commits(
    forge: Forge, ref: PRRef, pr: PRData,
) -> tuple[Sequence[CommitData] | None, str]:
    """The PR's commit list for the metadata rules, or why none came back.

    ``get_commits`` is an optional Forge method resolved with
    ``getattr``, like ``get_summary``: a forge without it — and every
    ``--diff-file`` run, whose local forge has no commits to list — gets
    ``(None, "no commit source")`` and the commit-reference check reports
    itself skipped rather than failed. Both PR shas are handed over as
    ``get_pr`` read them; a forge that needs no range ignores them. The
    call is best-effort like the thread listing: a transport failure is
    logged and also degrades to a skip with the reason, because the
    metadata checks are advisory and must never fail a review.
    """
    getter = getattr(forge, "get_commits", None)
    head = getattr(pr, "source_sha", "") or ""
    base = getattr(pr, "target_sha", "") or ""
    if getter is None or not head or not base:
        return None, "no commit source"
    try:
        return list(getter(ref, base_sha=base, head_sha=head)), ""
    except Exception as e:  # noqa: BLE001
        logger.warning("get_commits failed (best-effort): %s", e)
        return None, f"commit source failed: {e.__class__.__name__}"


def _make_file_reader(
    forge: Forge, ref: PRRef, pr: PRData, *, repo_dir: RepoDir | None = None,
):
    """A cached ``read(path) -> str | None`` over the forge's optional reader.

    The forge is used when it has ``get_file_content`` and the PR has a head
    sha, even when ``repo_dir`` is also given. Otherwise ``repo_dir`` (a
    :class:`prxref.forges.repo_dir.RepoDir`, the ``--repo-dir`` checkout) is
    the fallback, so a forge-less review such as ``--diff-file`` without
    ``--pr-url`` still gets chunk context (issue #29). With neither,
    ``None`` is returned, which is the signal to skip context injection
    entirely. Either way every path is fetched at most once per run, no
    exclusion is applied, and any exception from the source degrades to
    ``None``.
    """
    forge_reader = getattr(forge, "get_file_content", None)
    sha = getattr(pr, "source_sha", "") or ""
    if forge_reader is not None and sha:
        source = "get_file_content"

        def fetch(path: str):
            return forge_reader(ref, path, sha=sha)
    elif repo_dir is not None:
        source = "repo_dir.read"
        fetch = repo_dir.read
    else:
        return None

    cache: dict[tuple[str, str], str | None] = {}
    lock = threading.Lock()

    def read(path: str) -> str | None:
        key = (path, sha)
        with lock:
            if key in cache:
                return cache[key]
        try:
            value = fetch(path)
        except Exception as e:  # noqa: BLE001 - context is never worth a failed review
            logger.debug("%s(%s) failed: %s", source, path, e)
            value = None
        if not isinstance(value, str):
            value = None
        with lock:
            cache[key] = value
        return value

    return read


def _context_blocks(
    chunk, reader, *, include_definitions: bool, unit: repo_unit.UnitContext | None = None,
) -> str:
    """Render the chunk's dependency, definition, contract, reader and standards blocks; never raises.

    ``unit`` is the chunk's repository context. Its definition lines follow
    the same-file definitions under one header, its contract lines form the
    contracts block, its reader lines the readers block and its standards
    lines the last block; they render with no
    ``reader`` too, over empty dependency and same-file lists. ``None``, or a
    unit with no lines, is exactly the rendering without repository context.
    """
    extra = unit.definition_lines if unit is not None else ()
    contracts = unit.contract_lines if unit is not None else ()
    readers = unit.reader_lines if unit is not None else ()
    standards = unit.standards_lines if unit is not None else ()
    if reader is None and not (extra or contracts or readers or standards):
        return ""
    deps: list[str] = []
    defs: list[str] = []
    if reader is not None:
        try:
            files = chunk_context.chunk_files(chunk)
            deps = chunk_context.dependency_versions(files, reader)
            defs = (
                chunk_context.referenced_definitions(files, reader)
                if include_definitions else []
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("chunk context unavailable: %s", e)
            if not (extra or contracts or readers or standards):
                return ""
            deps, defs = [], []
    try:
        return chunk_context.render_context_blocks(
            deps, defs, extra_def_lines=extra, contract_lines=contracts,
            reader_lines=readers, standards_lines=standards,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("chunk context unavailable: %s", e)
        return ""


@dataclass(frozen=True)
class _RepoPlan:
    """One run's repository-context inputs, fixed before the chunk workers start.

    ``reader`` is the run's one :class:`prxref.repo_reader.RepoReader`, or
    ``None``. ``diff_paths`` holds every PR diff file's path: a read of one
    goes to the shared, uncapped ``reader.read``. The listing, contract and
    standards fields are the ``"repo"`` level's once-per-run inputs, and stay
    empty at ``"diff"`` or without a reader.
    """

    mode: str
    reader: repo_reader.RepoReader | None
    max_chars: int
    exclude: Callable[[str], bool]
    diff_paths: frozenset[str]
    listing_paths: frozenset[str] | None = None
    listing_complete: bool = False
    contract_paths: tuple[str, ...] = ()
    contract_priority: tuple[str, ...] = ()
    standards_paths: tuple[str, ...] = ()
    standards_priority: tuple[str, ...] = ()
    standards_max_chars: int = 4000


def _plan_repo_context(
    mode: str, forge: Forge, ref: PRRef, pr: PRData, files: Sequence[Any],
    initial: Mapping[str, Any], *, repo_dir: RepoDir | None,
) -> _RepoPlan:
    """Build the run's reader and its once-per-run inputs, for a level other than ``"off"``.

    ``initial`` is the run's initial ``repo_context`` record, whose
    ``max_chars``, read caps (``max_reads`` as the reader's ``run_cap``,
    ``max_chunk_reads`` as its ``chunk_cap``) and glob lists are the
    inputs. The reader reads
    ``repo_dir`` when it is given, else the forge at the PR's head sha. At
    ``"repo"`` with a reader, the listing is taken here, once, and the
    contract files and the standards files are selected here, once. At
    ``"repo"``, a missing reader
    or a missing listing logs the run's one WARNING naming
    ``PRXREF_REPO_CONTEXT``.
    """
    exclude = exclude_predicate(initial["exclude_globs"])
    caps = {"run_cap": initial["max_reads"], "chunk_cap": initial["max_chunk_reads"]}
    if repo_dir is not None:
        reader = repo_reader.repo_dir_reader(repo_dir, exclude=exclude, **caps)
    else:
        reader = repo_reader.forge_reader(
            forge, ref, getattr(pr, "source_sha", "") or "", exclude=exclude, **caps,
        )
    diff_paths = frozenset(f.path for f in files)
    if mode != "repo":
        return _RepoPlan(mode, reader, initial["max_chars"], exclude, diff_paths)
    if reader is None:
        logger.warning(
            "PRXREF_REPO_CONTEXT=repo, but there is no repository reader (the forge cannot "
            "read files at the PR head and no repository directory was given); repository "
            "context is limited to diff-only entries from hunk lines",
        )
        return _RepoPlan(mode, None, initial["max_chars"], exclude, diff_paths)
    listing = reader.listing()
    if listing is None:
        logger.warning(
            "PRXREF_REPO_CONTEXT=repo, but the repository path listing is unavailable; "
            "repository context runs with no name search and no glob-matched contract files "
            "outside the PR's own files",
        )
    globs = list(initial["contract_globs"])
    standards_globs = list(initial["standards_globs"])
    return _RepoPlan(
        mode, reader, initial["max_chars"], exclude, diff_paths,
        listing_paths=frozenset(listing.paths) if listing is not None else None,
        listing_complete=listing.complete if listing is not None else False,
        contract_paths=tuple(repo_contracts.select_contract_files(
            globs, listing=listing.paths if listing is not None else None,
            diff_paths=[f.path for f in files if f.status != "removed"],
        )),
        contract_priority=tuple(repo_contracts.literal_contract_paths(globs)),
        standards_paths=tuple(repo_contracts.select_contract_files(
            standards_globs, listing=listing.paths if listing is not None else None,
            diff_paths=[f.path for f in files if f.status != "removed"],
        )),
        standards_priority=tuple(repo_contracts.literal_contract_paths(standards_globs)),
        standards_max_chars=initial["standards_max_chars"],
    )


def _routed_read(reader: repo_reader.RepoReader, diff_paths: frozenset[str]) -> Callable[[str], str | None]:
    """One chunk's ``read``: a PR diff file through the shared ``reader.read``, any other path capped.

    The capped half is a fresh :meth:`~prxref.repo_reader.RepoReader.chunk_reader`,
    so the per-chunk cap is spent on paths outside the diff alone. Both
    halves refuse an excluded path.
    """
    capped = reader.chunk_reader()
    shared = reader.read

    def read(path: str) -> str | None:
        return shared(path) if path in diff_paths else capped(path)

    return read


def _chunk_unit(
    plan: _RepoPlan, chunk, all_files, *, index: int, total: int,
) -> repo_unit.UnitContext:
    """Build one chunk's repository context in its worker; never raises.

    A build that raises gives :data:`prxref.repo_unit.EMPTY_UNIT` and one
    WARNING naming the chunk.
    """
    read = _routed_read(plan.reader, plan.diff_paths) if plan.reader is not None else None
    try:
        return repo_unit.build_unit_context(
            chunk, all_files if all_files is not None else chunk,
            mode=plan.mode, read=read, max_chars=plan.max_chars,
            listing_paths=plan.listing_paths, listing_complete=plan.listing_complete,
            contract_paths=plan.contract_paths, contract_priority=plan.contract_priority,
            standards_paths=plan.standards_paths, standards_priority=plan.standards_priority,
            standards_max_chars=plan.standards_max_chars,
            exclude=plan.exclude,
        )
    except Exception as e:  # noqa: BLE001 - context is never worth a failed review
        logger.warning(
            "[chunk %d/%d] repository context failed (continuing without it): %s",
            index, total, e,
        )
        return repo_unit.EMPTY_UNIT


def _unit_row(unit: repo_unit.UnitContext, *, retry_dropped: bool = False) -> dict[str, Any]:
    """One chunk's ``repo_context`` units row: the unit's record plus ``retry_dropped``."""
    return {**unit.record(), "retry_dropped": retry_dropped}


def _repo_context_record(
    initial: Mapping[str, Any], plan: _RepoPlan, unit_records: Sequence[dict[str, Any] | None],
) -> dict[str, Any]:
    """The ``repo_context`` record once the chunk workers are done.

    ``reader`` is the reader's ``kind`` or ``None``; ``listing``, ``reads``
    and the three cap flags come from one ``stats()`` snapshot (``None``, 0
    and false without a reader); ``units`` lists the rows in chunk order,
    where a chunk whose worker left no row gets the empty unit's.
    """
    reader = plan.reader
    stats = reader.stats() if reader is not None else {
        "reads": 0, "read_cap_hit": False, "chunk_read_cap_hit": False,
        "run_read_cap_hit": False, "listing": None,
    }
    return {
        **initial,
        "reader": reader.kind if reader is not None else None,
        "listing": stats["listing"],
        "reads": stats["reads"],
        "read_cap_hit": stats["read_cap_hit"],
        "chunk_read_cap_hit": stats["chunk_read_cap_hit"],
        "run_read_cap_hit": stats["run_read_cap_hit"],
        "units": {
            "chunks": [
                row if row is not None else _unit_row(repo_unit.EMPTY_UNIT)
                for row in unit_records
            ],
        },
    }


SUGGESTION_MODES = ("off", "on")


def _suggestion_record(
    findings_active: Sequence[Finding] = (),
    cleared: Sequence[tuple[Finding, str]] = (),
) -> dict[str, Any]:
    """The ``suggestions`` record of a run with ``PRXREF_SUGGESTIONS=on`` (#30).

    ``{"kept": n, "cleared": {<reason>: n, ...}}``, with one ``cleared``
    entry per :data:`quality.SUGGESTION_CLEAR_REASONS` reason, in that
    order, zeros included. ``kept`` counts the active findings that still
    carry a suggestion. ``cleared`` pairs each finding
    :func:`quality.apply_suggestion_validation` cleared with its reason;
    the passes after it only drop and re-sort, and never touch ``file``,
    ``line`` or ``title``, so a pair counts once for one active finding
    without a suggestion on those three, and a pair whose finding was
    dropped later counts nowhere. No arguments gives the all-zero record.
    """
    counts = dict.fromkeys(SUGGESTION_CLEAR_REASONS, 0)
    pending: dict[tuple[str, int, str], list[str]] = {}
    for f, reason in cleared:
        pending.setdefault((f.file, f.line, f.title), []).append(reason)
    kept = 0
    for f in findings_active:
        if f.suggestion is not None:
            kept += 1
            continue
        reasons = pending.get((f.file, f.line, f.title))
        if reasons:
            counts[reasons.pop(0)] += 1
    return {"kept": kept, "cleared": counts}


def _enforce_suggestion(
    findings: Sequence[Finding], active: bool, *, sweep_start: int,
) -> list[Finding]:
    """Hold every finding's code suggestion to what the run asked the model for (#30).

    The ``suggestion`` twin of :func:`_enforce_rule`. Only chunk workers are
    asked, and only when ``active``, so a sweep finding (at or after
    ``sweep_start``) and, when not active, every finding has its
    ``suggestion`` reset to ``None`` and ``suggestion_end_line`` to 0,
    whatever a test double, a library reviewer or a backend that bypasses
    the reviewer's own gate supplied. Returns a new list in the same order;
    only a finding that changes is replaced, with
    :func:`dataclasses.replace`.
    """
    out: list[Finding] = []
    for index, f in enumerate(findings):
        keep = active and index < sweep_start
        if keep or (f.suggestion is None and f.suggestion_end_line == 0):
            out.append(f)
        else:
            out.append(replace(f, suggestion=None, suggestion_end_line=0))
    return out


def _warn_missing_suggestion_slot(prompts: PromptTemplates) -> None:
    """Warn once when suggestions are on and a worker override has no ``{suggestion_example}``.

    The ``suggestion`` twin of :func:`_warn_missing_rule_slot`, for the
    ``worker`` template only, because the sweep is never asked. Such an
    override still gets :data:`reviewer.SUGGESTION_REQUEST` and its answers
    are still read, but its ``## Output Format`` example finding shows no
    ``"suggestion"`` key. One WARNING names the file and points at
    ``prxref prompts export``; nothing is raised.
    """
    missing = [
        f.path
        for f in getattr(prompts, "overrides", ())
        if f.name == "worker"
        and "suggestion_example" not in placeholders(f.text.partition(CONTEXT_MARKER)[2])
    ]
    if missing:
        logger.warning(
            "PRXREF_SUGGESTIONS is on, but prompt template override(s) %s have no "
            "{suggestion_example} slot after %r, so their example finding shows no "
            "\"suggestion\" key; re-export with `prxref prompts export DIR --force` "
            "and re-apply your edits to pick the slot up",
            ", ".join(missing), CONTEXT_MARKER,
        )


FOLLOWUP_MODES = ("off", "on")

FOLLOWUP_INACTIVE_WARNING = (
    "PRXREF_CONTEXT_FOLLOWUP=on needs PRXREF_REPO_CONTEXT=repo and a repository reader; "
    "the context follow-up is off for this run"
)

CI_WIRING_MODES = ("off", "on")

CI_WIRING_INACTIVE_WARNING = (
    "PRXREF_CI_WIRING=on, but there is no repository reader (the forge cannot "
    "read files at the PR head and no repository directory was given); the CI "
    "wiring check is off for this run"
)

_FOLLOWUP_TOTALS = ("confirmed", "unconfirmed", "discarded", "input_tokens", "output_tokens")


def _followup_record(rows: Sequence[dict[str, Any] | None] | None = None) -> dict[str, Any]:
    """The ``context_followup`` record of a run with ``PRXREF_CONTEXT_FOLLOWUP=on``.

    ``rows`` is ``None`` for a run whose follow-up is not active (the level
    gate left it off, or the chunk workers have not finished): ``active`` is
    false, every count 0 and ``chunks`` ``None``. Otherwise ``rows`` holds one
    chunk row or ``None`` per chunk, in chunk order; ``calls`` counts the rows
    whose follow-up was sent and every other count is the sum over the rows.
    """
    present = [row for row in rows or () if row is not None]
    return {
        "active": rows is not None,
        "calls": sum(1 for row in present if row.get("called")),
        **{key: sum(int(row.get(key) or 0) for row in present) for key in _FOLLOWUP_TOTALS},
        "chunks": list(rows) if rows is not None else None,
    }


def _scoped_unit_blocks(
    scoped_rules: Any, chunks, always_on, *, max_chars: int,
) -> tuple[list[Any], Any]:
    """Build every review unit's scoped-rules block: one per chunk, in chunk order, then the sweep's.

    A chunk's paths are each file's ``path`` plus its ``old_path``, so a rules
    file scoped to a renamed file's old location still reaches it; the sweep's
    paths are the union of the chunks' paths, which selects the union of the
    chunks' files. ``always_on`` is the always-on rules file, or ``None``.
    Raises what :meth:`prxref.rules.ScopedRules.unit_block` raises (a
    ``max_chars`` below 1).
    """
    chunk_paths = [[p for f in chunk for p in (f.path, f.old_path) if p] for chunk in chunks]
    blocks = [
        scoped_rules.unit_block("worker", paths, always_on, max_chars=max_chars)
        for paths in chunk_paths
    ]
    sweep = scoped_rules.unit_block(
        "sweep", [p for paths in chunk_paths for p in paths], always_on, max_chars=max_chars,
    )
    return blocks, sweep


def _scoped_rows(block: Any) -> list[dict[str, Any]]:
    """One unit's scoped files as ``{"path", "chars"}`` rows, in load order.

    ``chars`` is the file's body length after its front matter, the same
    number as its ``chars`` in :meth:`prxref.rules.ScopedRules.record`, so a
    row joins its ``files`` row on ``path``. Shared by the run record and the
    ``chunk start`` / ``sweep start`` trace events.
    """
    return [{"path": f.path, "chars": f.body.chars} for f in block.files]


def _warn_scoped_cap(
    scoped_rules: Any, blocks: Sequence[Any], max_chars: int,
) -> None:
    """Log ONE warning for the run when the per-unit cap cut or omitted any scoped file.

    Names ``PRXREF_SCOPED_RULES_MAX_CHARS``, how many units it touched, and
    the distinct truncated and omitted paths in load order. Silent when
    every unit's scoped text fit.
    """
    cut = {block.truncated for block in blocks if block.truncated}
    left_out = {path for block in blocks for path in block.omitted}
    if not cut and not left_out:
        return
    order = [f.path for f in scoped_rules.files]
    logger.warning(
        "scoped rules exceed PRXREF_SCOPED_RULES_MAX_CHARS (%d characters per review unit) "
        "in %d of %d review unit(s); truncated: %s; omitted: %s",
        max_chars,
        sum(1 for block in blocks if block.truncated or block.omitted),
        len(blocks),
        ", ".join(p for p in order if p in cut) or "none",
        ", ".join(p for p in order if p in left_out) or "none",
    )


def _run_workers(
    llm: LLMClient, chunks, pr: PRData, *, max_tokens: int | None = None,
    max_workers: int = MAX_WORKERS, context_lines: int | None = None,
    tracer: Tracer | None = None, reader=None, all_files=None,
    trace_dir: str | None = None,
    prompt_context: PromptContext = NO_PROMPT_CONTEXT,
    scoped_blocks: Sequence[Any] | None = None,
    repo_plan: _RepoPlan | None = None,
    unit_records: list[dict[str, Any] | None] | None = None,
    parse_retries: int = 0,
    followup_floor: float | None = None,
    followup_records: list[dict[str, Any] | None] | None = None,
    evidence_blocks: Sequence[str] | None = None,
) -> list[dict]:
    # Never below 1: ThreadPoolExecutor rejects a zero-width pool, and a
    # library caller is not gated by config's range check.
    tracer = tracer if tracer is not None else get_tracer()
    workers = max(1, min(max_workers, len(chunks)))
    done = threading.Event()
    t_start = time.perf_counter()

    def _heartbeat() -> None:
        """Say the run is alive while nothing else is saying anything.

        Chunks log on completion, so a chunk that never completes produces
        silence indistinguishable from a wedged process -- the exact shape of
        the 496s hang this was written for. One line per interval turns that
        into a readable countdown.
        """
        while not done.wait(HEARTBEAT_SECONDS):
            waited = int(time.perf_counter() - t_start)
            pending = sum(1 for f in futures if not f.done())
            if pending:
                logger.info(
                    "still running: %d/%d chunks outstanding, %ds elapsed",
                    pending, len(chunks), waited,
                )
                tracer.event(
                    "heartbeat", "tick", pending=pending,
                    total=len(chunks), elapsed_s=waited,
                )

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [
            ex.submit(
                _run_worker, i + 1, len(chunks), llm, chunk, pr,
                max_tokens, context_lines, tracer, reader, all_files,
                trace_label=f"chunk{i}", trace_dir=trace_dir,
                prompt_context=prompt_context,
                scoped_block=scoped_blocks[i] if scoped_blocks is not None else None,
                repo_plan=repo_plan, unit_records=unit_records,
                parse_retries=parse_retries,
                followup_floor=followup_floor, followup_records=followup_records,
                evidence_block=(
                    evidence_blocks[i] if evidence_blocks is not None else ""
                ),
            )
            for i, chunk in enumerate(chunks)
        ]
        beat = threading.Thread(target=_heartbeat, name="prxref-heartbeat", daemon=True)
        beat.start()
        try:
            results = []
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as e:  # noqa: BLE001
                    results.append({
                        "findings": [], "error": f"worker crashed: {e}",
                        "input_tokens": 0, "output_tokens": 0,
                        "model": "", "elapsed_ms": 0,
                        "cost_usd": None, "cost_source": "",
                    })
            return results
        finally:
            done.set()


def _is_timeout_error(error: str) -> bool:
    """True when a worker's failure reason is an LLM deadline overrun.

    The OpenAI-compat chain spells every deadline failure ``<model>: timeout
    (<exc>)``, so this is a substring test against that vocabulary,
    case-insensitive. Deliberately NOT matched: truncation. A completion cut
    off by the response-side budget (``finish_reason=length``) is an HTTP 200,
    not a timeout — it degrades gracefully upstream and names
    ``PRXREF_LLM_MAX_TOKENS`` — so shrinking the prompt for it would be the
    wrong lever.
    """
    return "timeout" in (error or "").lower()


# The deadline (PRXREF_LLM_TIMEOUT) is wall clock over prefill AND decode, so
# a chunk can lose it to prompt size alone; rendering with zero context lines
# attacks exactly that share, keeping every changed line.
_TIMEOUT_RETRY_CONTEXT_LINES = 0


def _retry_meta(meta: Mapping[str, Any]) -> dict[str, Any]:
    """The parse-retry keys a worker result carries over from its reviewer meta (issue #21).

    ``{"parse_retries", "first_error"}`` when ``meta`` has ``parse_retries``,
    which the reviewer sets only when a parse retry ran at
    ``parse_retries`` of 1 or more, and ``{}`` otherwise, so a result
    without a retry keeps exactly its 0.16.0 keys.
    """
    if "parse_retries" not in meta:
        return {}
    return {
        "parse_retries": meta["parse_retries"],
        "first_error": meta.get("first_error", ""),
    }


def _invoke_chunk(
    llm: LLMClient, chunk, pr: PRData,
    max_tokens: int | None, context_lines: int | None,
    reader=None, *, include_definitions: bool = True, all_files=None,
    trace_label: str = "", trace_dir: str | None = None,
    prompt_context: PromptContext = NO_PROMPT_CONTEXT,
    unit: repo_unit.UnitContext | None = None,
    parse_retries: int = 0,
    extra_blocks: str = "",
) -> dict:
    """One normalized :func:`reviewer.review_chunk` call; never raises.

    Returns the worker result shape — findings coerced to ``Finding``,
    ``error`` always a string — for both the original attempt and the
    timeout retry in :func:`_run_worker`. Legacy dict-shaped stubs are
    accepted exactly as before.

    ``reader`` is the optional cached file reader from
    :func:`_make_file_reader`; when present the chunk's dependency and
    definition context blocks are built here, so both the original attempt and
    the retry carry them. ``include_definitions`` is false on the timeout
    retry, whose whole purpose is a smaller prompt. ``all_files`` is the PR's
    full parsed file list; the reviewer reduces it to the bounded sibling
    summary, which survives the retry because refuting evidence is not
    bulk context. ``prompt_context`` (rules, ticket, spec digest) is passed
    unchanged on both attempts: it is intent, not bulk context, and a
    dict-shaped finding keeps its ``scope`` only when
    :attr:`reviewer.PromptContext.scope_active`, and its ``rule`` only when
    :attr:`reviewer.PromptContext.rule_active`. ``unit`` is the chunk's
    repository context (:func:`_context_blocks`); :func:`_run_worker` passes
    it on the first attempt only, and ``None`` renders exactly as before.

    The shape carries the reviewer's reported ``cost_usd`` and
    ``cost_source`` beside the token counts; a call that raised, or a stub
    whose meta lacks them, gives ``None`` and ``""``. Pricing is left to
    :func:`_stamp_run_cost`, over the whole run.

    ``parse_retries`` is passed to the reviewer unchanged (issue #21). When
    its meta carries ``parse_retries`` and ``first_error``, the shape
    carries both after ``cost_source`` (:func:`_retry_meta`); otherwise,
    and always for a call that raised, it has neither.

    ``extra_blocks`` is a pre-rendered block appended after every other
    context block, separated by a blank line: the context follow-up's
    looked-up definitions (issue #22). ``""`` (the default) leaves the
    blocks exactly as they were.
    """
    blocks = _context_blocks(chunk, reader, include_definitions=include_definitions, unit=unit)
    if extra_blocks:
        blocks = "\n\n".join(part for part in (blocks.strip(), extra_blocks.strip()) if part)
    try:
        res = reviewer.review_chunk(
            llm, chunk, pr_title=pr.title, pr_description=pr.description,
            max_tokens=max_tokens, context_lines=context_lines,
            context_blocks=blocks, sibling_files=all_files or (),
            trace_label=trace_label, trace_dir=trace_dir or "",
            prompt_context=prompt_context, parse_retries=parse_retries,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "findings": [], "error": str(e),
            "input_tokens": 0, "output_tokens": 0, "model": "",
            "elapsed_ms": 0, "cost_usd": None, "cost_source": "",
        }

    # reviewer returns (findings, meta); legacy dict stubs still accepted.
    if isinstance(res, tuple):
        findings_raw, meta = res
        res = {
            "findings": findings_raw,
            "input_tokens": meta.get("input_tokens", 0),
            "output_tokens": meta.get("output_tokens", 0),
            "model": meta.get("model", ""),
            "elapsed_ms": meta.get("elapsed_ms", 0),
            "error": meta.get("error", ""),
            "cost_usd": meta.get("cost_usd"),
            "cost_source": meta.get("cost_source", ""),
            **_retry_meta(meta),
        }

    findings = []
    for item in res.get("findings") or []:
        finding = _coerce_finding(
            item, accept_scope=prompt_context.scope_active,
            accept_rule=prompt_context.rule_active,
            accept_suggestion=prompt_context.suggestion_active,
            accept_evidence=prompt_context.evidence_active,
        )
        if finding is not None:
            findings.append(finding)

    return {
        "findings": findings,
        "error": str(res.get("error") or ""),
        "input_tokens": res.get("input_tokens", 0),
        "output_tokens": res.get("output_tokens", 0),
        "model": res.get("model", ""),
        "elapsed_ms": res.get("elapsed_ms", 0),
        "cost_usd": res.get("cost_usd"),
        "cost_source": res.get("cost_source", ""),
        **_retry_meta(res),
    }


def _chunk_followup(
    first: dict, llm: LLMClient, chunk, pr: PRData,
    max_tokens: int | None, context_lines: int | None, reader, all_files, *,
    plan: _RepoPlan, unit: repo_unit.UnitContext | None, floor: float,
    prompt_context: PromptContext, trace_label: str, trace_dir: str | None,
    index: int, total: int, tracer: Tracer,
) -> tuple[dict, dict]:
    """Run one chunk's context follow-up (issue #22) after its first attempt; never raises.

    Returns ``(result, row)`` from :func:`prxref.followup.run_chunk_followup`.
    Its lookup reads through a fresh :func:`_routed_read`, so the follow-up
    spends its own per-chunk read cap. ``shown`` is the first attempt's
    context blocks plus the rendered chunk, so an excerpt the worker already
    saw is not sent again. The re-run is :func:`_invoke_chunk` with the same
    arguments as the first attempt, the follow-up block as ``extra_blocks``,
    no parse retry and the trace label ``<trace_label>.followup``. A failure
    outside the driver keeps ``first`` and records the error in the row.
    """
    def invoke(block: str) -> dict:
        return _invoke_chunk(
            llm, chunk, pr, max_tokens, context_lines, reader, all_files=all_files,
            trace_label=f"{trace_label}.followup", trace_dir=trace_dir,
            prompt_context=prompt_context, unit=unit, parse_retries=0,
            extra_blocks=block,
        )

    try:
        shown = (
            _context_blocks(chunk, reader, include_definitions=True, unit=unit)
            + reviewer.render_chunk(chunk, context_lines)
        )
        return followup.run_chunk_followup(
            first, chunk=chunk, all_files=all_files if all_files is not None else chunk,
            read=_routed_read(plan.reader, plan.diff_paths),
            listing_paths=plan.listing_paths, listing_complete=plan.listing_complete,
            exclude=plan.exclude, shown=shown, floor=floor, invoke=invoke,
            index=index, total=total, tracer=tracer,
        )
    except Exception as e:  # noqa: BLE001
        row = followup.skipped_row()
        row["error"] = str(e) or e.__class__.__name__
        logger.warning(
            "[chunk %d/%d] context follow-up failed (keeping the first review): %s",
            index, total, row["error"],
        )
        return first, row


def _run_worker(
    index: int, total: int, llm: LLMClient, chunk, pr: PRData,
    max_tokens: int | None = None, context_lines: int | None = None,
    tracer: Tracer | None = None, reader=None, all_files=None,
    trace_label: str = "", trace_dir: str | None = None,
    *, prompt_context: PromptContext = NO_PROMPT_CONTEXT,
    scoped_block: Any = None,
    repo_plan: _RepoPlan | None = None,
    unit_records: list[dict[str, Any] | None] | None = None,
    parse_retries: int = 0,
    followup_floor: float | None = None,
    followup_records: list[dict[str, Any] | None] | None = None,
    evidence_block: str = "",
) -> dict:
    tracer = tracer if tracer is not None else get_tracer()
    t0 = time.perf_counter()
    # The chunk's own scoped rules replace the run-wide worker block, and
    # its evidence block rides beside them (#69); both attempts below take
    # this context, so the retry keeps the chunk's rules and evidence.
    unit_context = prompt_context
    if scoped_block is not None:
        unit_context = replace(unit_context, rules_worker=scoped_block.text)
    if evidence_block:
        unit_context = replace(unit_context, evidence_block=evidence_block)
    # Logged on ENTRY, not only on completion. A chunk that never finishes
    # otherwise leaves no evidence it ever started, so a hang cannot be
    # attributed to a chunk, a file, or a model.
    # A chunk IS the list of FileDiffs, not an object wrapping one. A defensive
    # getattr(chunk, "files", []) here reported "0 files" for every chunk --
    # a log line that lies is worse than no log line, because it is believed.
    logger.info("[chunk %d/%d] start: %d files", index, total, len(chunk))
    tracer.event(
        "chunk", "start", index=index, total=total,
        files=[f.path for f in chunk],
        **({"rules": _scoped_rows(scoped_block)} if scoped_block is not None else {}),
    )
    unit: repo_unit.UnitContext | None = None
    if repo_plan is not None:
        unit = _chunk_unit(repo_plan, chunk, all_files, index=index, total=total)
        if unit_records is not None:
            unit_records[index - 1] = _unit_row(unit)
        tracer.event(
            "chunk", "context", index=index, total=total,
            entries=len(unit.entries), omitted=unit.omitted,
            chars=sum(len(entry.rendered()) for entry in unit.entries),
        )
    res = _invoke_chunk(
        llm, chunk, pr, max_tokens, context_lines, reader, all_files=all_files,
        trace_label=trace_label, trace_dir=trace_dir, prompt_context=unit_context,
        unit=unit, parse_retries=parse_retries,
    )
    retried = False
    if (
        res["error"]
        and _is_timeout_error(res["error"])
        and context_lines != _TIMEOUT_RETRY_CONTEXT_LINES
    ):
        retried = True
        # Issue #29's timeout half: a chunk that outruns the deadline took the
        # whole chunk's findings with it. One deterministic retry with the
        # context trimmed to the changed lines — same chunk, same budget,
        # strictly smaller prompt. The caller's own 0 skips it: an identical
        # prompt would meet an identical fate.
        logger.warning(
            "[chunk %d/%d] timed out; retrying once with context_lines=0",
            index, total,
        )
        tracer.event(
            "chunk", "retry", index=index, total=total, reason="timeout",
        )
        # The dependency block is a handful of tokens and survives; the
        # definitions block is the bulky one and is dropped, because shrinking
        # the prompt is the entire point of this retry.
        if unit is not None and unit_records is not None:
            unit_records[index - 1] = _unit_row(unit, retry_dropped=True)
        res = _invoke_chunk(
            llm, chunk, pr, max_tokens, _TIMEOUT_RETRY_CONTEXT_LINES, reader,
            include_definitions=False, all_files=all_files,
            trace_label=trace_label, trace_dir=trace_dir,
            prompt_context=unit_context, parse_retries=parse_retries,
        )

    if followup_floor is not None and repo_plan is not None and repo_plan.reader is not None:
        if retried:
            row = followup.skipped_row("timeout-retry")
        else:
            res, row = _chunk_followup(
                res, llm, chunk, pr, max_tokens, context_lines, reader, all_files,
                plan=repo_plan, unit=unit, floor=followup_floor, prompt_context=unit_context,
                trace_label=trace_label, trace_dir=trace_dir, index=index, total=total,
                tracer=tracer,
            )
        if followup_records is not None:
            followup_records[index - 1] = row

    error = res["error"]
    if error and _is_timeout_error(error):
        # Issue #72: the backend's timeout vocabulary names the model and
        # the exception class, neither of which tells an operator what to
        # do. The rewrite names the chunk and the wait, and points at the
        # lever that exists (--timeout, or leaving it alone so the prompt-
        # scaled deadline applies).
        error = (
            f"[chunk {index}/{total}] timed out after "
            f"{res.get('elapsed_ms', 0) / 1000:.0f}s; increase --timeout"
        )
    if error:
        logger.error("[chunk %d/%d] worker reported error: %s", index, total, error)
        tracer.event(
            "chunk", "fail", index=index, total=total,
            elapsed_ms=_elapsed_ms(t0), error=error[:200],
        )
    else:
        logger.info(
            "[chunk %d/%d] %d findings in %d ms",
            index, total, len(res["findings"]), _elapsed_ms(t0),
        )
        tracer.event(
            "chunk", "ok", index=index, total=total,
            elapsed_ms=_elapsed_ms(t0), findings=len(res["findings"]),
            model=res["model"],
            input_tokens=res["input_tokens"],
            output_tokens=res["output_tokens"],
            cost_usd=res["cost_usd"],
        )
    return {
        "findings": res["findings"],
        "error": error,
        "input_tokens": res["input_tokens"],
        "output_tokens": res["output_tokens"],
        "model": res["model"],
        "elapsed_ms": _elapsed_ms(t0),
        "cost_usd": res["cost_usd"],
        "cost_source": res["cost_source"],
        **_retry_meta(res),
    }


def _run_sweep(
    llm: LLMClient, files, pr: PRData, *,
    max_tokens: int | None = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    tracer: Tracer | None = None,
    threads: Sequence[Thread] = (),
    trace_dir: str | None = None,
    prompt_context: PromptContext = NO_PROMPT_CONTEXT,
    scoped_block: Any = None,
    parse_retries: int = 0,
    evidence_block: str = "",
) -> dict:
    """Run the whole-PR systemic sweep as one worker-style review unit.

    Builds the digest (:func:`prxref.systemic.build_digest`, capped inside
    ``token_budget``), makes ONE single-shot call through
    :func:`reviewer.review_systemic` — so ``PRXREF_LLM_MAX_TOKENS``, the
    timeout, and the model fallback chain all apply as to any chunk — and
    returns the same result shape a chunk worker does. ``prompt_context``
    rides along into the sweep prompt (sweep rules, ticket scope and the rule
    request in the system half, ticket context and the spec digest in the
    user half), and a dict-shaped finding keeps its ``scope`` only when
    :attr:`reviewer.PromptContext.scope_active`, and its ``rule`` only when
    :attr:`reviewer.PromptContext.rule_active`. A failure is that
    shape with ``error`` set prefixed ``systemic sweep:``, so the
    partial-review banner names the unit that failed; it counts as one
    failed chunk in the caller's coverage accounting. ``scoped_block``, the
    sweep's path-scoped rules block, replaces ``rules_sweep`` in the context
    and its files ride the ``sweep start`` event as ``rules``; ``None`` (no
    scoped rules) leaves both exactly as they were. ``evidence_block``
    (issue #69) is the sweep's GLOBAL evidence block — a chunk's matched
    items are that chunk's business — and fills ``evidence_block`` in the
    context the same way; ``""`` (the default) leaves the prompt as it was.
    ``parse_retries`` is
    passed to the reviewer unchanged (issue #21), and the result carries
    the meta's ``parse_retries`` and ``first_error`` exactly as a chunk's
    does (:func:`_retry_meta`); a sweep that raised carries neither.
    """
    tracer = tracer if tracer is not None else get_tracer()
    t0 = time.perf_counter()
    if scoped_block is not None:
        prompt_context = replace(prompt_context, rules_sweep=scoped_block.text)
    if evidence_block:
        prompt_context = replace(prompt_context, evidence_block=evidence_block)
    digest = systemic.build_digest(files, token_budget)
    digested = {f.path for f in files}
    discussion = [t for t in threads if t.path in digested]
    logger.info(
        "[sweep] start: %d files, digest %d chars, %d thread(s)",
        len(files), len(digest), len(discussion),
    )
    tracer.event(
        "sweep", "start", files=len(files), digest_chars=len(digest),
        threads=len(discussion),
        **({"rules": _scoped_rows(scoped_block)} if scoped_block is not None else {}),
    )
    try:
        findings_raw, meta = reviewer.review_systemic(
            llm, digest, pr_title=pr.title, pr_description=pr.description,
            max_tokens=max_tokens, threads=discussion,
            trace_label="sweep", trace_dir=trace_dir or "",
            prompt_context=prompt_context, parse_retries=parse_retries,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("[sweep] raised: %s", e)
        tracer.event(
            "sweep", "fail", elapsed_ms=_elapsed_ms(t0),
            error=e.__class__.__name__,
        )
        return {
            "findings": [], "error": f"systemic sweep: {e}",
            "input_tokens": 0, "output_tokens": 0, "model": "",
            "elapsed_ms": _elapsed_ms(t0),
            "cost_usd": None, "cost_source": "",
        }

    findings = []
    for item in findings_raw:
        finding = _coerce_finding(
            item, accept_scope=prompt_context.scope_active,
            accept_rule=prompt_context.rule_active,
            accept_evidence=prompt_context.evidence_active,
        )
        if finding is not None:
            findings.append(finding)

    error = str(meta.get("error") or "")
    if error:
        error = f"systemic sweep: {error}"
        logger.error("[sweep] failed: %s", error)
        tracer.event(
            "sweep", "fail", elapsed_ms=_elapsed_ms(t0), error=error[:200],
        )
    else:
        logger.info(
            "[sweep] %d findings in %d ms", len(findings), _elapsed_ms(t0),
        )
        tracer.event(
            "sweep", "ok", elapsed_ms=_elapsed_ms(t0), findings=len(findings),
            model=meta.get("model", ""),
            input_tokens=meta.get("input_tokens", 0),
            output_tokens=meta.get("output_tokens", 0),
            cost_usd=meta.get("cost_usd"),
        )
    return {
        "findings": findings,
        "error": error,
        "input_tokens": meta.get("input_tokens", 0),
        "output_tokens": meta.get("output_tokens", 0),
        "model": meta.get("model", ""),
        "elapsed_ms": _elapsed_ms(t0),
        "cost_usd": meta.get("cost_usd"),
        "cost_source": meta.get("cost_source", ""),
        **_retry_meta(meta),
    }


def _coerce_finding(
    item, *, accept_scope: bool = False, accept_rule: bool = False,
    accept_suggestion: bool = False, accept_evidence: bool = False,
) -> Finding | None:
    if isinstance(item, Finding):
        return item
    if isinstance(item, dict):
        suggestion, suggestion_end_line = (
            reviewer.parse_suggestion(item) if accept_suggestion else (None, 0)
        )
        try:
            return Finding(
                file=str(item["file"]),
                line=int(item.get("line") or 0),
                severity=str(item.get("severity") or ""),
                confidence=float(item.get("confidence") or 0.0),
                title=str(item.get("title") or ""),
                body=str(item.get("body") or ""),
                scope=(
                    normalize_scope(item.get("scope")) if accept_scope
                    else SCOPE_UNKNOWN
                ),
                rule=normalize_rule(item.get("rule")) if accept_rule else None,
                suggestion=suggestion,
                suggestion_end_line=suggestion_end_line,
                evidence=normalize_evidence(item.get("evidence")) if accept_evidence else None,
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("dropping malformed finding %r: %s", item, e)
            return None
    logger.warning("dropping malformed finding %r", item)
    return None


def _render_summary(
    pr: PRData,
    files,
    verdict: str,
    findings_active,
    model: str,
    input_tokens: int,
    output_tokens: int,
    elapsed_ms: int,
    *,
    chunks_reviewed: int = 0,
    chunks_failed: int = 0,
    failed_chunks: Sequence[tuple[str, Sequence[str]]] = (),
    include_verdict: bool = True,
    inline_accounting: str | None = None,
    thread_accounting: str = "",
    spec_note: str = "",
    ticket_note: str = "",
    evidence_note: str = "",
    cost_label: str = "",
    size_advisory_line: str = "",
    summary_template: str = "",
    incremental_note: str = "",
    summary_bullet_separator: str = SUMMARY_BULLET_SEPARATOR,
) -> str:
    """Render the PR summary comment body.

    The template is filled in ONE pass (:func:`reviewer.fill_template`), so
    a PR title, a note or a finding title containing ``{findings}``,
    ``{attribution}`` or any other placeholder renders literally instead of
    receiving that placeholder's value. The five marker slots
    (``{error_marker}`` ... ``{out_of_ticket_marker}``,
    :func:`markers.marker_slots`) are filled from the effective glyph table,
    as are the bullets and the outside-ticket heading. ``spec_note``,
    ``ticket_note`` and ``evidence_note`` ride
    ``{spec_note}{ticket_note}{evidence_note}`` on the line after the counts;
    each carries its own trailing newline when non-empty, so empty notes
    leave the summary byte-identical. ``{findings}`` lists the in-ticket and unjudged
    findings first; findings outside the ticket (scope ``"out"``) follow
    under a bold ``Outside the ticket (N)`` heading led by
    :func:`markers.out_of_ticket_marker`; when no other finding exists,
    ``No in-ticket findings.`` stands in for the first list. Without an
    active ticket every scope is ``"unknown"``, so the list stays flat.
    ``cost_label`` is the attribution's last field
    (:func:`_attribution`). ``size_advisory_line`` (``"> ⚠️ …\\n\\n"`` or
    ``""``) is prepended to the finished body, after the partial-review
    banner, so it is the first thing under the forge's summary marker.
    ``incremental_note`` (:func:`_incremental_note`, ``""`` on every run that
    is not incremental) follows it the same way.
    ``summary_template`` is an operator override of ``summary.md``
    (:meth:`prxref.prompt_templates.PromptTemplates.override`); ``""`` reads
    the packaged template through ``reviewer.load_prompt``, and only that
    read can fall back to the built-in template, so an override is always
    rendered as given.

    Optional slots, filled on every render and absent from the packaged
    template (:data:`prxref.prompt_templates.SUMMARY_OPTIONAL_PLACEHOLDERS`):
    ``{error_findings}``, ``{warning_findings}``, ``{spec_findings}`` and
    ``{outofscope_findings}`` are the bullets of that severity's findings
    not scoped ``"out"`` (a severity outside the four joins
    ``outofscope``), and ``{outside_ticket_findings}`` those of every
    ``"out"`` finding, each ``""`` when empty and in ``{findings}`` order.
    Each ``{<group>_section}`` is ``**<marker> <Label>**\\n\\n<bullets>\\n``
    (labels ``Errors``, ``Warnings``, ``Spec``, ``Minor``) or ``""``;
    ``{outside_ticket_section}`` is the ``Outside the ticket (N)`` block
    exactly as ``{findings}`` carries it, plus a trailing newline.
    ``{head_sha}`` is ``pr.source_sha`` (``""`` when unknown),
    ``{head_sha_short}`` its first 7 characters, ``{chunk_count}``
    ``chunks_reviewed + chunks_failed``, and ``{input_tokens}`` /
    ``{output_tokens}`` the token counts. ``summary_bullet_separator`` joins
    every bullet's location to its title.

    ``inline_accounting`` goes to the ``{inline_accounting}`` slot when the
    template has one; otherwise it rides the end of ``{findings}``, as it
    always has, and a template with neither slot gets it appended to the
    body. ``thread_accounting`` (:func:`_thread_dedup_accounting`, issue
    #73, ``""`` when neither thread count is non-zero) rides that exact same
    plumbing, joined after the inline line with a blank line between them.
    A template without ``{findings}`` whose finding group (see
    :func:`prxref.prompt_templates.uncovered_summary_groups`) has neither
    of its slots gets that group's findings appended as ``**Other findings
    (N)**`` with a WARNING, so no finding is silently dropped. Other
    findings, then appended inline accounting, go above the footer (the
    attribution and a ``---`` rule directly over it) when the body has one
    (:func:`_insert_before_footer`); a template that dropped
    ``{attribution}`` gets them at the end of the body and the attribution
    after them. The partial-review banner comes last.
    """
    try:
        template = summary_template or reviewer.load_prompt("summary")
    except Exception as e:  # noqa: BLE001
        logger.warning("load_prompt('summary') failed, using fallback: %s", e)
        template = _FALLBACK_SUMMARY_TEMPLATE
    if not include_verdict:
        template = _strip_verdict_stamp(template)

    counts = {"error": 0, "warning": 0, "spec": 0, "outofscope": 0}
    for f in findings_active:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    sep = summary_bullet_separator
    inside = [f for f in findings_active if f.scope != SCOPE_OUT]
    outside = [f for f in findings_active if f.scope == SCOPE_OUT]
    if inside:
        bullets = _summary_bullets(inside, separator=sep)
    elif outside:
        bullets = "No in-ticket findings."
    else:
        bullets = "No findings — nice work."
    outside_section = ""
    if outside:
        outside_section = (
            f"**{out_of_ticket_marker()} Outside the ticket ({len(outside)})**"
            f"\n\n{_summary_bullets(outside, separator=sep)}\n"
        )
        bullets = f"{bullets}\n\n{outside_section[:-1]}"

    found = placeholders(template)
    has_findings = "findings" in found
    # The inline and thread-dedup reconciliation lines ride the SAME slot
    # plumbing: joined with a blank line so either can be empty without
    # leaving a seam, and both reach {inline_accounting}, the end of
    # {findings}, or the appended extras exactly as the inline line alone
    # always has.
    accounting = "\n\n".join(
        line for line in (inline_accounting or "", thread_accounting) if line
    )
    if accounting and has_findings and "inline_accounting" not in found:
        bullets = f"{bullets}\n\n{accounting}"

    groups: dict[str, list[Finding]] = {g: [] for g in SUMMARY_GROUP_PLACEHOLDERS}
    for f in findings_active:
        groups[_summary_group_of(f)].append(f)
    group_slots: dict[str, str] = {}
    for group, (list_slot, section_slot) in SUMMARY_GROUP_PLACEHOLDERS.items():
        listed = _summary_bullets(groups[group], separator=sep)
        group_slots[list_slot] = listed
        if group == "outside_ticket":
            group_slots[section_slot] = outside_section
        elif listed:
            group_slots[section_slot] = (
                f"**{severity_marker(group)} {_SUMMARY_GROUP_LABELS[group]}**\n\n{listed}\n"
            )
        else:
            group_slots[section_slot] = ""

    head_sha = getattr(pr, "source_sha", "") or ""
    attribution = _attribution(
        model, input_tokens + output_tokens, elapsed_ms, cost_label=cost_label,
    )
    rendered = fill_template(template, {
        "verdict": verdict,
        "title": pr.title,
        "file_count": str(len(files)),
        "error_count": str(counts["error"]),
        "warning_count": str(counts["warning"]),
        "spec_count": str(counts["spec"]),
        "outofscope_count": str(counts["outofscope"]),
        "spec_note": spec_note,
        "ticket_note": ticket_note,
        "evidence_note": evidence_note,
        "findings": bullets,
        "attribution": attribution,
        "inline_accounting": accounting,
        "head_sha": head_sha,
        "head_sha_short": head_sha[:7],
        "chunk_count": str(chunks_reviewed + chunks_failed),
        "input_tokens": str(input_tokens),
        "output_tokens": str(output_tokens),
        **group_slots,
        **marker_slots(),
    })
    uncovered = uncovered_summary_groups(found)
    stray = [f for f in findings_active if _summary_group_of(f) in uncovered]
    extras: list[str] = []
    if stray:
        logger.warning(
            "summary template has no {findings} and no slot for the %s finding group(s); "
            "appending %d finding(s) under 'Other findings'",
            ", ".join(g for g in uncovered if groups[g]), len(stray),
        )
        extras.append(
            f"**Other findings ({len(stray)})**\n\n"
            f"{_summary_bullets(stray, separator=sep)}"
        )
    if accounting and not has_findings and "inline_accounting" not in found:
        extras.append(accounting)
    if extras:
        rendered = _insert_before_footer(rendered, "\n\n".join(extras), attribution)
    if attribution not in rendered:
        rendered = f"{rendered}\n\n{attribution}"
    if chunks_failed:
        total = chunks_reviewed + chunks_failed
        rendered = (
            f"{rendered}\n\n> ⚠️ Partial review: {chunks_reviewed} of {total} "
            f"chunks were reviewed; {chunks_failed} failed. Findings may be incomplete."
        )
        # Inside the same blockquote: subordinate to the findings, which is where
        # a PR author's eye skips unless they are troubleshooting — but present,
        # because a partial review looks like a successful one and nobody goes
        # looking. The total-failure notice has always posted its reason
        # verbatim; staying silent here was the inconsistency, not the safety.
        reason_lines = _failure_reason_lines(failed_chunks)
        if reason_lines:
            rendered += "\n>\n" + "\n".join(f"> {line}" for line in reason_lines)
    return f"{size_advisory_line}{incremental_note}{rendered}"


def _size_advisory(
    files,
    *,
    lines_limit: int | None,
    files_limit: int | None,
    ignore_globs: Sequence[str] = (),
) -> dict | None:
    """The PR-size advisory's stats, or ``None`` when both limits are unset.

    The stats are ``{changed_lines, changed_files, lines_limit, files_limit,
    triggered, message}``, computed whenever either limit is set, whether or
    not it is exceeded. The counts come from
    :func:`prxref.triage.count_size_relevant_changes`, which skips lockfiles
    (:data:`prxref.heuristics.LOCKFILE_BASENAMES`), generated files and
    ``ignore_globs``. A limit is exceeded strictly (``>``), so 0 is a real
    threshold rather than "off". ``message`` is ``None`` unless a limit is
    exceeded, and otherwise plain text naming only the exceeded limits, e.g.
    ``This PR changes 812 lines in 24 files, above the team guideline of 500
    lines and 20 files. Consider splitting it.`` The advisory never touches
    the findings, so it cannot move the verdict or the exit code.
    """
    if lines_limit is None and files_limit is None:
        return None
    changed_lines, changed_files = count_size_relevant_changes(
        files, lockfile_basenames=heuristics.LOCKFILE_BASENAMES, ignore_globs=ignore_globs,
    )
    exceeded = []
    if lines_limit is not None and changed_lines > lines_limit:
        exceeded.append(f"{lines_limit} {_plural(lines_limit, 'line')}")
    if files_limit is not None and changed_files > files_limit:
        exceeded.append(f"{files_limit} {_plural(files_limit, 'file')}")
    message = None
    if exceeded:
        message = (
            f"This PR changes {changed_lines} {_plural(changed_lines, 'line')} "
            f"in {changed_files} {_plural(changed_files, 'file')}, above the team "
            f"guideline of {' and '.join(exceeded)}. Consider splitting it."
        )
    return {
        "changed_lines": changed_lines,
        "changed_files": changed_files,
        "lines_limit": lines_limit,
        "files_limit": files_limit,
        "triggered": message is not None,
        "message": message,
    }


def _plural(n: int, unit: str) -> str:
    """``unit`` for exactly one, else ``unit + "s"`` (0 lines, 1 line, 2 lines)."""
    return unit if n == 1 else f"{unit}s"


def _size_advisory_line(stats: Mapping[str, Any] | None) -> str:
    """The blockquote a triggered size advisory prepends to the summary.

    ``"> ⚠️ {message}\\n\\n"`` when ``stats`` carries a message, else ``""``,
    which leaves the summary byte-identical to a run without the advisory.
    """
    message = stats.get("message") if stats else None
    return f"> ⚠️ {message}\n\n" if message else ""


def _spec_note(sources: Sequence[Any], digest: str) -> str:
    """Render the summary's grounding note, ``""`` when nothing was requested.

    One blockquote line counts what was injected
    (:func:`prxref.specs.constraint_count`); one lists every failed source.
    A failure is labelled by its 1-based position in the configured source
    list and its kind, ``source 2 (url)``, or ``source 2`` when the kind was
    never determined, never by its origin: a local path or a URL's query is
    not the PR audience's business, and the operator can map the ordinal
    back to the list. Each reason goes through :func:`redact_for_post`
    first, because this text is posted. A run whose every source failed
    renders ONLY the failure line — the review was un-grounded, and the note
    must not dress it up as grounded. The note rides the ``{spec_note}``
    placeholder on its own line between the counts and the findings, and a
    non-empty note carries its own trailing newline, so an empty return
    leaves the summary byte-identical to an ungrounded run's.
    """
    if not sources:
        return ""
    total = len(sources)
    failed = [(i, s) for i, s in enumerate(sources, start=1) if s.error]
    lines: list[str] = []
    if len(failed) < total:
        lines.append(
            f"> {active_severity_markers()['spec']} Spec-grounded: {total} source(s) · "
            f"{specs.constraint_count(digest)} constraint(s) injected"
        )
    if failed:
        reasons = "; ".join(
            f"source {i}{f' ({s.kind})' if s.kind else ''}: {redact_for_post(s.error)}"
            for i, s in failed
        )
        lines.append(
            f"> ⚠️ Spec fetch failed for {len(failed)} source(s): {reasons}"
        )
    return "\n".join(lines) + "\n"


def _evidence_note(record: Mapping[str, Any] | None, downgraded: int) -> str:
    """Render the summary's execution-evidence note (#69), ``""`` when none was supplied.

    One blockquote line says what was supplied (items, and the files they
    came from) and how far the matched items reached (how many chunk
    prompts), so a PR reader can tell an evidence-backed review from an
    unevidenced one; a second clause, only when any finding conceded a
    contradiction, says how many were downgraded to ``warning``. The note
    rides ``{evidence_note}`` after ``{ticket_note}``, and carries its own
    trailing newline, so an empty return leaves the summary byte-identical.
    """
    if record is None:
        return ""
    line = (
        f"> ℹ️ Execution evidence: {record.get('items', 0)} item(s) from "
        f"{len(record.get('files') or [])} file(s); matched items reached "
        f"{record.get('matched_chunks', 0)} chunk prompt(s)"
    )
    if downgraded:
        line += f"; {downgraded} contradicted finding(s) downgraded to warning"
    return line + "\n"


def _log_safe_origin(origin: str) -> str:
    """Name a spec source for the operator's log, never its credentials.

    A URL keeps ``scheme://host[:port]/path`` and loses its userinfo, query,
    fragment and ``;params``, because CI logs are read more widely than the
    operator's config. Anything without a scheme and a network location is
    a local path and is returned verbatim, since it tells the operator which
    file or directory to fix. A malformed URL is not echoed at all.
    """
    try:
        parsed = urlparse(origin.strip())
    except ValueError:
        return "[unparseable origin]"
    if not (parsed.scheme and parsed.netloc):
        return origin
    host = parsed.netloc.rpartition("@")[2]
    return f"{parsed.scheme}://{host}{parsed.path}"


def _chunk_files_label(files: Sequence[str]) -> str:
    """``chunk of 3 files (a.py, b.py, c.py)``, first three then a count."""
    listing = ", ".join(files[:3])
    if len(files) > 3:
        listing = f"{listing}, +{len(files) - 3} more"
    plural = "s" if len(files) != 1 else ""
    return f"chunk of {len(files)} file{plural} ({listing})"


def _failure_reason_lines(
    failed_chunks: Sequence[tuple[str, Sequence[str]]],
) -> list[str]:
    """Render each failed chunk as a blockquote-ready line naming its files.

    Each entry is one failed chunk: ``(reason, files)``. The reason is
    redacted first (:func:`redact_for_post`), because this text is posted
    onto a pull request; the file list is not, because paths are not
    secrets — and naming the files is the banner's whole point (issue
    #31): "7 of 8 chunks were reviewed; 1 failed" told the operator nothing
    about which files went unreviewed. An empty file list (the systemic
    sweep, which names itself in its reason) renders the reason alone.

    Identical chunk-and-reason pairs collapse to one line, and the list is
    capped at :data:`MAX_REPORTED_REASONS` chunks, because a pathological
    run must not flood the comment. The overflow is counted out loud rather
    than dropped — a silent truncation here would repeat the very failure
    this banner exists to fix.

    Returns one entry per RENDERED LINE, not one per chunk. The caller
    prefixes ``"> "`` per entry, so a reason containing a newline used to put
    every line after the first outside the blockquote and mangle the rest of
    the comment; continuation lines are indented under their bullet instead.
    """
    distinct: list[tuple[tuple[str, ...], str]] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    for reason, files in failed_chunks:
        if not reason:
            continue
        key = (tuple(files), redact_for_post(reason))
        if key not in seen:
            seen.add(key)
            distinct.append(key)
    lines: list[str] = []
    for files, reason in distinct[:MAX_REPORTED_REASONS]:
        first, *rest = reason.splitlines() or [""]
        label = f"{_chunk_files_label(files)}: " if files else ""
        lines.append(f"- {label}{first}")
        lines.extend(f"  {line}" for line in rest)
    hidden = max(0, len(distinct) - MAX_REPORTED_REASONS)
    if hidden:
        plural = "s" if hidden > 1 else ""
        lines.append(f"- …and {hidden} more failed chunk{plural} (see logs)")
    return lines



_FOOTER_RULE_RE = re.compile(
    r"(?:^|\n)[ \t]*(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$"
)


def _insert_before_footer(rendered: str, block: str, attribution: str) -> str:
    """Place ``block`` above the summary's footer, else append it to the body.

    The footer is the last occurrence of ``attribution`` together with a
    Markdown thematic break (three or more ``-``, ``*`` or ``_`` on their own
    line) directly above it, if there is one, so text a template
    leaves out still reads as part of the review and not as a postscript
    under the attribution. A body without the attribution gets ``block``
    appended after a blank line.
    """
    at = rendered.rfind(attribution)
    if at < 0:
        return f"{rendered}\n\n{block}"
    head = rendered[:at].rstrip()
    rule = _FOOTER_RULE_RE.search(head)
    if rule:
        head = head[:rule.start()].rstrip()
    cut = len(head)
    return f"{rendered[:cut]}\n\n{block}\n\n{rendered[cut:].lstrip(chr(10))}"


def _summary_bullets(
    findings: Sequence[Finding], *, separator: str = SUMMARY_BULLET_SEPARATOR,
) -> str:
    """One ``- <marker> `file:line`<separator>title`` summary bullet per finding, in order.

    ``separator`` defaults to :data:`SUMMARY_BULLET_SEPARATOR` (``" — "``);
    a file-level finding's line is always rendered ``—``, whatever the
    separator. ``""`` for no findings. A finding carrying a
    ``previous_thread`` note (issue #73) has it appended after its title,
    joined by the same separator, so the bullet says the finding was raised
    before in a since-resolved thread.
    """
    lines = []
    for f in findings:
        line = (
            f"- {marker_for(f.severity, f.scope)} "
            f"`{f.file}:{f.line if f.line > 0 else '—'}`{separator}{f.title}"
        )
        if f.previous_thread:
            line = f"{line}{separator}{f.previous_thread}"
        lines.append(line)
    return "\n".join(lines)


def _summary_group_of(f: Finding) -> str:
    """The summary group a finding renders in: ``outside_ticket`` for scope ``out``, else its severity.

    A severity outside the four known ones joins ``outofscope``, whose glyph
    :func:`markers.marker_for` already gives it.
    """
    if f.scope == SCOPE_OUT:
        return "outside_ticket"
    return f.severity if f.severity in _SUMMARY_GROUP_LABELS else "outofscope"


def _format_finding(f: Finding, model: str, suggestion_style: str | None = None) -> str:
    block = format_suggestion_block(f, suggestion_style)
    suggestion = f"{block}\n\n" if block else ""
    # The previously-raised note (issue #73) is a SUFFIX, after the footer:
    # the header, body, suggestion and attribution lines are pinned by frozen
    # tests that prefix-match or compare the leading body, and the note is
    # context for a re-review, not part of the finding itself.
    note = f"\n\n{f.previous_thread}" if f.previous_thread else ""
    return (
        f"{inline_header(f)}\n\n"
        f"{f.body}\n\n"
        f"{suggestion}"
        f"---\n*Reviewed by prxref · model={model}*{note}"
    )


def _inline_comment(f: Finding, model: str, suggestion_style: str | None) -> InlineComment:
    """Build one finding's inline comment for a forge's ``suggestion_style``.

    GitHub applies a suggestion to the comment's whole line range, so a
    multi-line suggestion there is anchored at its last line with
    ``start_line`` at its first. Every other style anchors at the finding's
    line, and a finding without a renderable suggestion is the exact comment
    it was before suggestions existed.
    """
    span = suggestion_range(f)
    body = _format_finding(f, model, suggestion_style)
    if suggestion_style == SUGGESTION_STYLE_GITHUB and span is not None and span[1] > span[0]:
        return InlineComment(path=f.file, line=span[1], body=body, start_line=span[0])
    return InlineComment(path=f.file, line=f.line, body=body)


def _trace_post_begin(
    tracer: Tracer, *, wanted: bool, reason: str, **meta: Any
) -> None:
    """Open the ``post`` node, or record that nothing asked it to open.

    A graph built only from what HAPPENED cannot express "nobody asked this to
    run", and that renders identically to "the run died before reaching it" --
    opposite findings. Every route out of a review calls this, including the
    two that return early and post their own notice.
    """
    if wanted:
        tracer.event("post", "start", **meta)
    else:
        tracer.event("post", "skip", reason=reason)


def _trace_post_end(
    tracer: Tracer, *, wanted: bool, posted: bool, **meta: Any
) -> None:
    """Close the ``post`` node opened by :func:`_trace_post_begin`."""
    if wanted:
        tracer.event("post", "ok" if posted else "fail", **meta)


def _summary_only_run(
    forge: Forge, ref: PRRef, pr: PRData, files, post: bool, t0: float,
    *, post_mode: str = "summary+inline", post_verdict: bool = True,
    tracer: Tracer | None = None, sampling: dict | None = None,
    release_shape_findings: list[Finding] | None = None,
    toggle_findings: list[Finding] | None = None,
    metadata_findings: list[Finding] | None = None,
    ci_findings: list[Finding] | None = None,
    confidence_floor: float | None = None, max_errors: int | None = None,
    max_warning_findings: int | None = None,
    max_outofscope_findings: int | None = None,
    ticket_note: str = "", cost_label: str = "", size_advisory_line: str = "",
    summary_template: str = "", reviewed_head: str | None = None,
    summary_bullet_separator: str = SUMMARY_BULLET_SEPARATOR,
) -> dict:
    """The no-chunk exit: an empty diff, or every file binary.

    No worker ever ran, but the release-shape and pinned-toggle heuristics
    (:func:`heuristics.release_shape_findings`,
    :func:`heuristics.toggle_pinned_off_findings`) are pure and need no
    chunk to fire on, so their findings — passed in by the caller, already
    computed over the full file list — are put through the same location
    and quality passes a chunk-sourced finding gets
    (:func:`apply_location_validation`, :func:`apply_quality_gate`) before
    they reach ``findings_active`` / ``verdict`` / the summary. The
    PR-metadata findings (#70, ``metadata_findings``) and the CI-wiring
    findings (#66, ``ci_findings``) join them the same
    way; no inline batch is ever posted from this exit, so the
    summary-only contract needs no enforcement here. Location validation
    runs only when the diff holds files: a metadata finding about a branch
    name exists even on an empty diff, where it anchors on ``""`` and
    there is no diff path set to validate against — the deterministic
    producers are trusted with their own anchors, exactly as they are on
    the empty-diff path that predates them (both heuristics yield ``[]``
    with no files, so the guard changes nothing when the feature is off).
    No
    :func:`apply_line_align` call here: both heuristics already anchor on a
    real diff line and there is no worker-supplied anchor to re-corroborate.
    An empty diff still yields ``release_shape_findings=[]`` and
    ``toggle_findings=[]`` (fewer than 2 files can never be release-shaped,
    and no file has an added line for a toggle or a pin to match), so this
    degrades to exactly the prior empty-diff behaviour: ``Approved``, no
    findings, no banner. ``confidence_floor``, ``max_errors``,
    ``max_warning_findings`` and ``max_outofscope_findings`` are that
    gate's knobs, threaded from :func:`orchestrate_review`. No grouping
    pass runs here: there is no chunk finding to group.

    ``ticket_note``, ``cost_label``, ``size_advisory_line`` and
    ``summary_template`` are handed to :func:`_render_summary` unchanged; all
    four default to ``""``, which renders the summary exactly as before. So
    is ``summary_bullet_separator``, whose default is
    :data:`SUMMARY_BULLET_SEPARATOR`.
    ``reviewed_head`` is stamped on the body by :func:`_with_reviewed_head`;
    ``None`` (the default) leaves it as before. The run-record keys are added
    by the caller's :func:`_run_record`, not here.
    """
    tracer = tracer if tracer is not None else get_tracer()
    elapsed_ms = _elapsed_ms(t0)
    posted = False
    degraded: dict | None = None
    summary: str | None = None

    findings = (
        list(release_shape_findings or []) + list(toggle_findings or [])
        + list(metadata_findings or []) + list(ci_findings or [])
    )
    if findings:
        if files:
            findings = apply_location_validation(findings, [f.path for f in files])
        findings = apply_quality_gate(
            findings, confidence_floor=confidence_floor, max_errors=max_errors,
            max_warning_findings=max_warning_findings,
            max_outofscope_findings=max_outofscope_findings,
        )
    findings_active = sorted(active(findings), key=finding_sort_key)
    findings_dropped = sorted(
        (f for f in findings if f.drop_reason is not None), key=finding_sort_key
    )
    verdict = (
        "Request-Changes"
        if any(f.severity == "error" for f in findings_active)
        else "Approved"
    )

    wanted = post and post_mode in POST_SUMMARY_MODES
    _trace_post_begin(
        tracer, wanted=wanted, mode=post_mode, kind="empty-diff summary",
        reason="posting disabled" if not post else f"post_mode={post_mode} posts no summary",
    )
    if wanted:
        summary = _render_summary(
            pr, files, verdict, findings_active, "unknown", 0, 0, elapsed_ms,
            chunks_reviewed=0, chunks_failed=0,
            include_verdict=post_verdict,
            ticket_note=ticket_note,
            cost_label=cost_label,
            size_advisory_line=size_advisory_line,
            summary_template=summary_template,
            summary_bullet_separator=summary_bullet_separator,
        )
        try:
            forge.post_summary(ref, _with_reviewed_head(summary, reviewed_head))
            posted = True
        except Exception as e:  # noqa: BLE001
            logger.error("post_summary failed: %s", e)
            degraded = _degraded_record([("summary", post_failure_cause(e))])
    _trace_post_end(tracer, wanted=wanted, posted=posted, mode=post_mode)
    return _with_degraded({
        "verdict": verdict,
        "findings_active": findings_active,
        "findings_dropped": findings_dropped,
        "chunk_count": 0,
        "chunks_reviewed": 0,
        "chunks_failed": 0,
        "elapsed_ms": elapsed_ms,
        "input_tokens": 0,
        "output_tokens": 0,
        "posted": posted,
        "sampling": sampling if sampling is not None else _sampling(None),
    }, degraded, summary)


def _error_run(
    forge: Forge,
    ref: PRRef,
    post: bool,
    chunk_count: int,
    reason: str,
    t0: float,
    model: str = "unknown",
    input_tokens: int = 0,
    output_tokens: int = 0,
    post_mode: str = "summary+inline",
    tracer: Tracer | None = None,
    sampling: dict | None = None,
    *,
    cost_label: str = "",
    chunks_reviewed: int = 0,
    reviewed_head: str | None = None,
) -> dict:
    """The error exit: post the failure notice when asked, return an Error run.

    ``cost_label`` becomes the notice attribution's last field
    (:func:`_attribution`); ``""`` leaves it as before. ``reviewed_head`` is
    stamped on the notice by :func:`_with_reviewed_head`; ``None`` (the
    default) leaves it as before. The notice never
    carries a ticket note or a size advisory, and the run-record keys are
    added by the caller's :func:`_run_record`, not here.

    ``chunks_reviewed`` is how many of the ``chunk_count`` review units
    succeeded; the rest are reported as failed. The default ``0`` fits every
    exit taken before a review unit ran. The total-failure exit passes the
    units that did succeed, so a sweep that answered over a dead worker pool
    is counted as reviewed while the verdict stays ``Error``.
    """
    tracer = tracer if tracer is not None else get_tracer()
    elapsed_ms = _elapsed_ms(t0)
    posted = False
    degraded: dict | None = None
    body: str | None = None
    wanted = post and post_mode in POST_SUMMARY_MODES
    _trace_post_begin(
        tracer, wanted=wanted, mode=post_mode, kind="error notice",
        reason="posting disabled" if not post else f"post_mode={post_mode} posts no summary",
    )
    if wanted:
        attribution = _attribution(
            model, input_tokens + output_tokens, elapsed_ms,
            cost_label=cost_label,
        )
        # The same redaction the partial banner uses: this notice interpolates
        # the reason into a public comment, and the caller has already logged
        # the unredacted text for the operator.
        body = (
            "🤖 **prxref review — Error**\n\n"
            f"The review could not complete: {redact_for_post(reason)}\n\n"
            "No findings were produced.\n\n"
            f"{attribution}"
        )
        try:
            forge.post_summary(ref, _with_reviewed_head(body, reviewed_head))
            posted = True
        except Exception as e:  # noqa: BLE001
            logger.error("post_summary (error notice) failed: %s", e)
            degraded = _degraded_record([("summary", post_failure_cause(e))])
    _trace_post_end(tracer, wanted=wanted, posted=posted, mode=post_mode)
    return _with_degraded({
        "verdict": "Error",
        "findings_active": [],
        "findings_dropped": [],
        "chunk_count": chunk_count,
        "chunks_reviewed": chunks_reviewed,
        "chunks_failed": chunk_count - chunks_reviewed,
        "elapsed_ms": elapsed_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "posted": posted,
        "sampling": sampling if sampling is not None else _sampling(None),
    }, degraded, body)
