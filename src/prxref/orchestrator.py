"""Review pipeline orchestrator: fetch diff, chunk, parallel workers, quality, post.

Stage order (v1 — no Jira, no graph, no learnings, no investigator):

1. ``forge.get_pr`` → PRData, ``forge.get_diff`` → raw diff,
   ``parse_unified_diff`` → files. An empty or unchunkable diff (every file
   binary, or no files at all) short-circuits to a summary-only run with
   verdict ``Approved`` — no chunk worker or sweep ever runs, but
   ``heuristics.release_shape_findings`` still does, gated the same as on
   the normal path, so a release-shaped diff with no reviewable text still
   gets its deterministic finding instead of a silent approval.
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
   unless a ticket is active (``_enforce_scope``), then gain
   ``heuristics.release_shape_findings(files)`` (a pure, no-LLM finding
   about a PR that is ≥80% release machinery yet also touches source),
   folded in BEFORE the passes so it is filtered like any other finding,
   and spliced in at the chunk/sweep boundary — before the sweep's own
   findings, never after — so it is always a CHUNK-side finding to
   ``apply_sweep_dedup``, whose exact tier drops only sweep findings, and
   being file-level (line 0) it is never compared by that pass's reworded
   tier either. It can never be dropped as a duplicate of a chunk worker's
   own restatement:

   ``apply_severity_map`` (only when the team review rules declare a
   severity map: a team word such as ``blocker`` becomes the prxref tier it
   maps to; drops nothing) → ``apply_spec_grounding`` (on an ungrounded
   run every ``spec`` finding, the sweep's included, is relabelled
   ``warning``, counted by a ``specs relabel`` trace event; drops nothing)
   → ``apply_location_validation`` (a ``file``
   naming no path of the parsed diff is dropped, not rendered) →
   ``apply_manifest_claim_check`` (a
   ``package.json`` claim whose dependency is not the key on the anchored
   line, or whose asserted section disagrees with the actual one; it must
   precede line align, which is what makes it read the model's RAW
   anchor) → ``apply_line_align`` → ``apply_thread_dedup`` (existing
   threads fetched best-effort BEFORE the workers run, and after the
   stale-inline prune; failure means no threads) →
   ``apply_settled_thread_suppression`` (a finding re-litigating a
   subject an existing thread already argued out, line-independently) →
   ``apply_severity_consistency`` (findings sharing a normalized title
   are raised to the group's max severity — the sweep's corroborating
   title counts toward its group) → ``apply_removal_claim_check`` (a
   claim that a NAMED path was removed when the post-image still carries
   it) → ``apply_hedge_gate`` (a finding whose own text conditions the
   defect on something the worker never established; a ``Spec:`` quote
   of the injected digest is not read as the finding's own text) →
   ``apply_quality_gate(confidence_floor=, max_errors=)``, which returns
   its findings in content order, so the chunk/sweep boundary is
   re-derived here from finding identity rather than carried across the
   gate as an index → ``apply_sweep_dedup`` (drops a sweep finding that
   restates a chunk finding that SURVIVED the gate, on file + normalized
   title; running it after the gate is what keeps a sub-floor chunk
   finding from suppressing its higher-confidence sweep duplicate and
   then dying at the gate itself. With ``dedup_similarity`` set, a
   reworded tier also compares findings in the same file on the same
   line: a sweep copy no more severe than a chunk copy is dropped, and of
   two copies on one side the less severe, then less confident, one is;
   a chunk copy is never dropped for a sweep copy, and line 0 is never
   compared) → ``apply_containment_note`` (a throw
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
   ``spec_grounding``, ``size_advisory``; ``replay`` on replays only, and
   ``cost_api_equivalent`` on claude-cli-priced runs only).
7. Verdict: ``"Error"`` when every CHUNK review failed (a sweep success
   on a dead worker pool cannot carry the run); ``"Request-Changes"``
   iff any active error-severity finding survives;
   else ``"Approved"``. A partial failure keeps the verdict but the summary
   declares reduced coverage AND itemizes each failed chunk with the files
   it took unreviewed plus its reason (capped, redacted, inside the same
   blockquote) — a partial review reads as a successful one, so a failure
   left only in the logs reaches nobody, and a file list left out of it
   leaves the operator guessing which files went unreviewed.
8. Post: summary rendered from ``reviewer.load_prompt("summary")``, or from
   the operator's ``summary.md`` override when ``prompts`` carries one, with
   placeholders ``{verdict} {title} {file_count} {error_count}
   {warning_count} {spec_count} {spec_note} {ticket_note}
   {outofscope_count} {findings} {attribution}`` filled, plus
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
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

from . import chunk_context, costs, heuristics, reviewer, specs, systemic
from .forges.base import (
    ATTRIBUTION_MARKER,
    Forge,
    InlineComment,
    PRData,
    PRRef,
    Thread,
)
from .llm import LLMClient
from .markers import OUT_OF_TICKET_MARKER, SEVERITY_MARKERS, inline_header, marker_for
from .prompt_templates import PromptTemplates
from .quality import (
    active,
    apply_containment_note,
    apply_hedge_gate,
    apply_line_align,
    apply_location_validation,
    apply_manifest_claim_check,
    apply_quality_gate,
    apply_removal_claim_check,
    apply_settled_thread_suppression,
    apply_severity_consistency,
    apply_severity_map,
    apply_spec_grounding,
    apply_sweep_dedup,
    apply_thread_dedup,
    finding_rank_key,
    finding_sort_key,
)
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
    build_chunks,
    count_size_relevant_changes,
    normalize_rule,
    normalize_scope,
    parse_unified_diff,
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
    "Files reviewed: {file_count} · 🟥 {error_count} error · "
    "🟧 {warning_count} warning · 🔍 {spec_count} spec · "
    "⬜ {outofscope_count} outofscope\n"
    "{spec_note}{ticket_note}\n"
    "{findings}\n\n{attribution}"
)

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
) -> dict:
    """Run one full review pass over a PR and optionally post results.

    Returns ``{verdict, findings_active, findings_dropped, chunk_count,
    chunks_reviewed, chunks_failed, elapsed_ms, input_tokens, output_tokens,
    posted, sampling, cost_usd, cost_estimated, review_rules, ticket_context,
    spec_grounding, size_advisory, prompt_templates, scoped_rules}``, plus
    ``replay`` on a replay run only.
    Every exit, error and empty-diff exits included, goes through
    :func:`_run_record`, so the last eight keys are always present and are
    ``None`` (``cost_usd``: ``0.0`` before any LLM request; ``cost_estimated``:
    ``False``) when their feature is off or the run never reached it.
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

    ``chunk_count`` counts the review units: ``len(chunks)`` plus one for
    the systemic sweep, which runs whenever at least one chunk exists (an
    empty diff returns before any review unit runs). ``chunks_reviewed`` +
    ``chunks_failed`` always equals it.

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

    ``replay`` is the evaluation-replay stamp built by the CLI
    (``{base_sha, head_sha, threads, diff_file}``). When given it is copied
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
    """
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

    try:
        with tracer.span("build_chunks") as sp:
            chunks = build_chunks(
                files, max_chunks=max_chunks, token_budget=token_budget,
                max_files_per_chunk=max_files_per_chunk,
            )
            sp["chunks"] = len(chunks)
    except Exception as e:  # noqa: BLE001
        logger.error("build_chunks failed: %s", e)
        tracer.event("run", "fail", **_cost_meta(run_inputs))
        return _run_record(_error_run(
            forge, ref, post, 0, f"build_chunks failed: {e}", t0,
            post_mode=post_mode, tracer=tracer, sampling=sampling,
            cost_label=_cost_label(run_inputs, post_cost),
        ), run_inputs)

    if not chunks:
        # No chunk survived build_chunks — an empty diff, or every file
        # binary — but the release-shape heuristic is pure and needs no
        # chunk to fire on: computed here so a release PR whose only
        # non-machinery file is binary still gets the deterministic finding
        # instead of a silent Approved (issue #29 residual, concern #2).
        release_shape = heuristics.release_shape_findings(files)
        tracer.event(
            "run", "ok", chunks_reviewed=0, findings=len(release_shape),
            **_cost_meta(run_inputs),
            **(_scope_counts(release_shape) if ticket_active else {}),
        )
        return _run_record(_summary_only_run(
            forge, ref, pr, files, post, t0,
            post_mode=post_mode, post_verdict=post_verdict, tracer=tracer,
            sampling=sampling, release_shape_findings=release_shape,
            confidence_floor=confidence_floor, max_errors=max_errors,
            ticket_note=ticket_note,
            cost_label=_cost_label(run_inputs, post_cost),
            size_advisory_line=size_advisory_line,
            summary_template=summary_template,
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
            ), run_inputs)
        run_inputs["scoped_rules"] = {
            **run_inputs["scoped_rules"],
            "units": {
                "chunks": [_scoped_rows(block) for block in scoped_blocks],
                "sweep": _scoped_rows(sweep_block),
            },
        }
        _warn_scoped_cap(scoped_rules, [*scoped_blocks, sweep_block], scoped_rules_max_chars)

    # Pruned BEFORE the threads are listed, and both before the review units
    # run. The prune-then-list order is load-bearing: reading threads first
    # would let this run's findings be suppressed as already-discussed against
    # prxref's OWN stale comments, which the prune then deletes. Both moved
    # ahead of the dispatch because the sweep needs the discussion in its
    # prompt, and a review that never starts has nothing to say either way.
    if post and post_mode in POST_INLINE_MODES:
        _prune_stale_inline_comments(forge, ref)

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

    prompt_context = PromptContext(
        rules_worker=rules.prompt_block("worker") if rules is not None else "",
        rules_sweep=rules.prompt_block("sweep") if rules is not None else "",
        ticket_scope=ticket.scope_block() if ticket_active else "",
        ticket_context=ticket.prompt_block() if ticket_active else "",
        spec_digest=injected,
        worker_template=prompts.override("worker") if prompts is not None else "",
        systemic_template=prompts.override("systemic") if prompts is not None else "",
    )
    reader = _make_file_reader(forge, ref, pr)
    results = _run_workers(
        llm, chunks, pr, max_tokens=max_tokens, max_workers=max_workers,
        context_lines=context_lines, tracer=tracer,
        reader=reader, all_files=files, trace_dir=trace_dir,
        prompt_context=prompt_context, scoped_blocks=scoped_blocks,
    )

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
        )
    )

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
    if all(r["error"] for r in results[:-1]):
        reason = f"all {len(chunks)} worker reviews failed ({results[0]['error']})"
        logger.error("Total LLM failure: %s", reason)
        tracer.event("run", "fail", **_cost_meta(run_inputs))
        return _run_record(_error_run(
            forge, ref, post, len(chunks) + 1, reason, t0, tracer=tracer,
            model=model, input_tokens=input_tokens, output_tokens=output_tokens,
            post_mode=post_mode, sampling=sampling, cost_label=cost_label,
            chunks_reviewed=sum(1 for r in results if not r["error"]),
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

    # Futures were submitted in chunk order, so results[i] is chunk[i]'s
    # outcome for i < len(chunks): the zip pairs each failed review with the
    # files it took down, which the partial banner names (issue #31). The
    # systemic sweep is results[-1] and names itself in its reason, so it is
    # not zipped against a chunk here.
    failed_chunks = [
        (r["error"], [f.path for f in chunk])
        for chunk, r in zip(chunks, results, strict=False)
        if r["error"]
    ]
    if results[-1]["error"]:
        failed_chunks.append((results[-1]["error"], []))

    # A deterministic, non-LLM finding folded in before the quality passes so
    # it flows through every one of them exactly like a model finding (issue
    # #10): file-level (line=0) survives apply_line_align untouched, and
    # warning/1.0 clears apply_quality_gate trivially. Folded in AT the
    # chunk/sweep boundary — before the sweep's own findings, not after —
    # and sweep_start moves with it: apply_sweep_dedup never drops a
    # CHUNK-side finding for a sweep-side one (its exact tier drops only
    # sweep findings; its reworded tier, on with dedup_similarity, can drop
    # one chunk copy for another on the same line, but never compares line
    # 0, which this finding always sits on). Appending this after the
    # sweep's findings would put it on the sweep side of that boundary,
    # where a chunk worker's own finding sharing its file and normalized
    # title could drop the deterministic finding as "duplicate of chunk
    # finding" and keep the model's restatement instead.
    release_shape = heuristics.release_shape_findings(files)
    findings = findings[:sweep_start] + release_shape + findings[sweep_start:]
    sweep_start += len(release_shape)

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

    findings = apply_location_validation(findings, [f.path for f in files])
    # BEFORE apply_line_align, deliberately: the manifest check compares the
    # model's raw anchor against the key and section it claims, and realignment
    # can move a correctly anchored claim onto a neighbouring entry first.
    # The same reader the chunk context uses serves the full-file lines that
    # name the section when the anchor's own hunk starts below its header.
    findings = apply_manifest_claim_check(findings, files, read=reader)
    findings = apply_line_align(findings, added_lines_by_file(files), files=files)
    findings = apply_thread_dedup(findings, threads)
    findings = apply_settled_thread_suppression(findings, threads)
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
    # The sweep boundary is positional, and the gate now returns its findings
    # in content order, so the boundary is re-derived from the identity of the
    # sweep's own findings rather than carried across the gate as an index.
    sweep_identities = Counter(
        _origin_key(f) for f in findings[sweep_start:]
    )
    findings = apply_quality_gate(
        findings, confidence_floor=confidence_floor, max_errors=max_errors,
    )
    chunk_part: list[Finding] = []
    sweep_part: list[Finding] = []
    for f in findings:
        key = _origin_key(f)
        if sweep_identities[key] > 0:
            sweep_identities[key] -= 1
            sweep_part.append(f)
        else:
            chunk_part.append(f)
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

    verdict = (
        "Request-Changes"
        if any(f.severity == "error" for f in findings_active)
        else "Approved"
    )

    elapsed_ms = _elapsed_ms(t0)
    posted = False
    inline_posted = 0
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
            cost_label=cost_label,
            size_advisory_line=size_advisory_line,
            summary_template=summary_template,
        )
        try:
            forge.post_summary(ref, summary)
            posted = True
        except Exception as e:  # noqa: BLE001
            logger.error("post_summary failed: %s", e)
    # A summary-mode run still requires the summary to have landed before the
    # inline batch rides on it; an inline-mode run has no summary to gate on.
    inline_attempted = 0
    inline_failed = False
    if post_inline_wanted and findings_active and (posted or not post_summary_wanted):
        ordered = sorted(
            findings_active,
            key=lambda f: (
                _SEVERITY_RANK.get(f.severity, 3),
                _SCOPE_RANK.get(f.scope, 0),
                *finding_rank_key(f),
            ),
        )
        comments = [
            InlineComment(
                path=f.file,
                line=f.line,
                body=_format_finding(f, model),
            )
            for f in ordered[:max_inline_comments]
        ]
        inline_attempted = len(comments)
        try:
            inline_posted = forge.post_inline_comments(ref, comments)
            posted = True
        except Exception as e:  # noqa: BLE001
            logger.error("post_inline_comments failed: %s", e)
            inline_failed = True

    # The summary itemizes every active finding, so when the inline pass left
    # some of them without an anchor the summary has to say so — otherwise it
    # promises a per-finding comment the PR never received. The counts only
    # exist after posting, so the disclosure rides a second post_summary call,
    # which the forges already implement as an update-in-place.
    if (
        post_summary_wanted and posted and post_inline_wanted
        and len(findings_active) > inline_posted
    ):
        refreshed = _render_summary(
            pr, files, verdict, findings_active, model,
            input_tokens, output_tokens, elapsed_ms,
            chunks_reviewed=chunks_reviewed, chunks_failed=chunks_failed,
            failed_chunks=failed_chunks,
            include_verdict=post_verdict,
            spec_note=spec_note,
            ticket_note=ticket_note,
            cost_label=cost_label,
            size_advisory_line=size_advisory_line,
            summary_template=summary_template,
            inline_accounting=_inline_accounting(
                len(findings_active), inline_attempted, inline_posted,
                failed=inline_failed, cap=max_inline_comments,
            ),
        )
        try:
            forge.post_summary(ref, refreshed)
        except Exception as e:  # noqa: BLE001
            logger.error("summary re-post with inline accounting failed: %s", e)

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
    return _run_record({
        "verdict": verdict,
        "findings_active": findings_active,
        "findings_dropped": findings_dropped,
        "chunk_count": len(chunks) + 1,
        "chunks_reviewed": chunks_reviewed,
        "chunks_failed": chunks_failed,
        "elapsed_ms": elapsed_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "posted": posted,
        "sampling": _sampling(llm),
    }, run_inputs)


def _origin_key(finding: Finding) -> tuple:
    """Identity used to re-derive the chunk/sweep boundary across the gate.

    ``severity`` and ``confidence`` are part of the key: without them a chunk
    finding and a sweep finding that agree on file, line, title, and body
    collide, ``finding_sort_key`` ties them, and the Counter walk hands the
    first survivor to the sweep side — dropping the higher-confidence chunk
    copy as a "duplicate of chunk finding". ``scope`` is in it for the same
    reason: with a ticket active the two copies can disagree on it, and a
    swap would put the sweep copy's scope in the chunk copy's slot. ``rule``
    is in it for that reason too, and sits before ``scope``, which stays the
    last element.
    """
    return (
        finding.file,
        finding.line,
        finding.title,
        finding.body,
        finding.severity,
        finding.confidence,
        finding.rule,
        finding.scope,
    )


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


def _prune_stale_inline_comments(forge: Forge, ref: PRRef) -> None:
    """Call the forge's optional stale-inline cleanup, before dedup reads threads.

    The order is load-bearing: pruning after ``list_threads`` would let the
    thread dedup suppress this run's findings as already-discussed and then
    delete the very comments it suppressed them against, removing the finding
    from the PR entirely. The capability is optional — forges without it and
    the duck-typed test fakes are skipped via getattr — and best-effort: a
    prune failure is logged, never raised, because cleanup must not abort the
    review that follows it.
    """
    prune = getattr(forge, "prune_inline_comments", None)
    if not callable(prune):
        return
    try:
        removed = prune(ref)
    except Exception as e:  # noqa: BLE001
        logger.warning("prune_inline_comments failed (best-effort): %s", e)
        return
    if removed:
        logger.info("pruned %d stale inline comment(s) before posting", removed)


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


HEARTBEAT_SECONDS = 30.0


def _make_file_reader(forge: Forge, ref: PRRef, pr: PRData):
    """A cached ``read(path) -> str | None`` over the forge's optional reader.

    Returns ``None`` when the forge has no ``get_file_content`` or the PR has
    no head sha, which is the signal to skip context injection entirely.
    Otherwise every path is fetched at most once per run at ``pr.source_sha``,
    and any exception from the adapter degrades to ``None``.
    """
    reader = getattr(forge, "get_file_content", None)
    sha = getattr(pr, "source_sha", "") or ""
    if reader is None or not sha:
        return None

    cache: dict[tuple[str, str], str | None] = {}
    lock = threading.Lock()

    def read(path: str) -> str | None:
        key = (path, sha)
        with lock:
            if key in cache:
                return cache[key]
        try:
            value = reader(ref, path, sha=sha)
        except Exception as e:  # noqa: BLE001 - context is never worth a failed review
            logger.debug("get_file_content(%s) failed: %s", path, e)
            value = None
        if not isinstance(value, str):
            value = None
        with lock:
            cache[key] = value
        return value

    return read


def _context_blocks(chunk, reader, *, include_definitions: bool) -> str:
    """Render the chunk's dependency and definition blocks; never raises."""
    if reader is None:
        return ""
    try:
        files = chunk_context.chunk_files(chunk)
        deps = chunk_context.dependency_versions(files, reader)
        defs = (
            chunk_context.referenced_definitions(files, reader)
            if include_definitions else []
        )
        return chunk_context.render_context_blocks(deps, defs)
    except Exception as e:  # noqa: BLE001
        logger.debug("chunk context unavailable: %s", e)
        return ""


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


def _invoke_chunk(
    llm: LLMClient, chunk, pr: PRData,
    max_tokens: int | None, context_lines: int | None,
    reader=None, *, include_definitions: bool = True, all_files=None,
    trace_label: str = "", trace_dir: str | None = None,
    prompt_context: PromptContext = NO_PROMPT_CONTEXT,
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
    the context's ``rule_active`` is true.

    The shape carries the reviewer's reported ``cost_usd`` and
    ``cost_source`` beside the token counts; a call that raised, or a stub
    whose meta lacks them, gives ``None`` and ``""``. Pricing is left to
    :func:`_stamp_run_cost`, over the whole run.
    """
    blocks = _context_blocks(chunk, reader, include_definitions=include_definitions)
    try:
        res = reviewer.review_chunk(
            llm, chunk, pr_title=pr.title, pr_description=pr.description,
            max_tokens=max_tokens, context_lines=context_lines,
            context_blocks=blocks, sibling_files=all_files or (),
            trace_label=trace_label, trace_dir=trace_dir or "",
            prompt_context=prompt_context,
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
        }

    findings = []
    for item in res.get("findings") or []:
        finding = _coerce_finding(
            item, accept_scope=prompt_context.scope_active,
            accept_rule=getattr(prompt_context, "rule_active", False),
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
    }


def _run_worker(
    index: int, total: int, llm: LLMClient, chunk, pr: PRData,
    max_tokens: int | None = None, context_lines: int | None = None,
    tracer: Tracer | None = None, reader=None, all_files=None,
    trace_label: str = "", trace_dir: str | None = None,
    *, prompt_context: PromptContext = NO_PROMPT_CONTEXT,
    scoped_block: Any = None,
) -> dict:
    tracer = tracer if tracer is not None else get_tracer()
    t0 = time.perf_counter()
    # The chunk's own scoped rules replace the run-wide worker block; both
    # attempts below take this context, so the retry keeps the chunk's rules.
    unit_context = (
        prompt_context if scoped_block is None
        else replace(prompt_context, rules_worker=scoped_block.text)
    )
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
    res = _invoke_chunk(
        llm, chunk, pr, max_tokens, context_lines, reader, all_files=all_files,
        trace_label=trace_label, trace_dir=trace_dir, prompt_context=unit_context,
    )
    if (
        res["error"]
        and _is_timeout_error(res["error"])
        and context_lines != _TIMEOUT_RETRY_CONTEXT_LINES
    ):
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
        res = _invoke_chunk(
            llm, chunk, pr, max_tokens, _TIMEOUT_RETRY_CONTEXT_LINES, reader,
            include_definitions=False, all_files=all_files,
            trace_label=trace_label, trace_dir=trace_dir,
            prompt_context=unit_context,
        )

    error = res["error"]
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
) -> dict:
    """Run the whole-PR systemic sweep as one worker-style review unit.

    Builds the digest (:func:`prxref.systemic.build_digest`, capped inside
    ``token_budget``), makes ONE single-shot call through
    :func:`reviewer.review_systemic` — so ``PRXREF_LLM_MAX_TOKENS``, the
    timeout, and the model fallback chain all apply as to any chunk — and
    returns the same result shape a chunk worker does. ``prompt_context``
    rides along into the sweep prompt (sweep rules and ticket scope in the
    system half, ticket context and the spec digest in the user half), and a
    dict-shaped finding keeps its ``scope`` only when
    :attr:`reviewer.PromptContext.scope_active`, and its ``rule`` only when
    the context's ``rule_active`` is true. A failure is that
    shape with ``error`` set prefixed ``systemic sweep:``, so the
    partial-review banner names the unit that failed; it counts as one
    failed chunk in the caller's coverage accounting. ``scoped_block``, the
    sweep's path-scoped rules block, replaces ``rules_sweep`` in the context
    and its files ride the ``sweep start`` event as ``rules``; ``None`` (no
    scoped rules) leaves both exactly as they were.
    """
    tracer = tracer if tracer is not None else get_tracer()
    t0 = time.perf_counter()
    if scoped_block is not None:
        prompt_context = replace(prompt_context, rules_sweep=scoped_block.text)
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
            prompt_context=prompt_context,
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
            accept_rule=getattr(prompt_context, "rule_active", False),
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
    }


def _coerce_finding(
    item, *, accept_scope: bool = False, accept_rule: bool = False,
) -> Finding | None:
    if isinstance(item, Finding):
        return item
    if isinstance(item, dict):
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
    spec_note: str = "",
    ticket_note: str = "",
    cost_label: str = "",
    size_advisory_line: str = "",
    summary_template: str = "",
) -> str:
    """Render the PR summary comment body.

    The template is filled in ONE pass (:func:`reviewer.fill_template`), so
    a PR title, a note or a finding title containing ``{findings}``,
    ``{attribution}`` or any other placeholder renders literally instead of
    receiving that placeholder's value. ``spec_note`` and ``ticket_note``
    ride ``{spec_note}{ticket_note}`` on the line after the counts; each
    carries its own trailing newline when non-empty, so empty notes leave the
    summary byte-identical. ``{findings}`` lists the in-ticket and unjudged
    findings first; findings outside the ticket (scope ``"out"``) follow
    under a bold ``Outside the ticket (N)`` heading led by
    :data:`markers.OUT_OF_TICKET_MARKER`; when no other finding exists,
    ``No in-ticket findings.`` stands in for the first list. Without an
    active ticket every scope is ``"unknown"``, so the list stays flat.
    ``cost_label`` is the attribution's last field
    (:func:`_attribution`). ``size_advisory_line`` (``"> ⚠️ …\\n\\n"`` or
    ``""``) is prepended to the finished body, after the partial-review
    banner, so it is the first thing under the forge's summary marker.
    ``summary_template`` is an operator override of ``summary.md``
    (:meth:`prxref.prompt_templates.PromptTemplates.override`); ``""`` reads
    the packaged template through ``reviewer.load_prompt``, and only that
    read can fall back to the built-in template, so an override is always
    rendered as given.
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

    inside = [f for f in findings_active if f.scope != SCOPE_OUT]
    outside = [f for f in findings_active if f.scope == SCOPE_OUT]
    if inside:
        bullets = _summary_bullets(inside)
    elif outside:
        bullets = "No in-ticket findings."
    else:
        bullets = "No findings — nice work."
    if outside:
        bullets = (
            f"{bullets}\n\n**{OUT_OF_TICKET_MARKER} Outside the ticket ({len(outside)})**"
            f"\n\n{_summary_bullets(outside)}"
        )
    if inline_accounting:
        bullets = f"{bullets}\n\n{inline_accounting}"

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
        "findings": bullets,
        "attribution": attribution,
    })
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
    return f"{size_advisory_line}{rendered}"


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
            f"> {SEVERITY_MARKERS['spec']} Spec-grounded: {total} source(s) · "
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



def _summary_bullets(findings: Sequence[Finding]) -> str:
    """One ``- <marker> `file:line` — title`` summary bullet per finding, in order."""
    return "\n".join(
        f"- {marker_for(f.severity, f.scope)} "
        f"`{f.file}:{f.line if f.line > 0 else '—'}` — {f.title}"
        for f in findings
    )


def _format_finding(f: Finding, model: str) -> str:
    return (
        f"{inline_header(f)}\n\n"
        f"{f.body}\n\n"
        f"---\n*Reviewed by prxref · model={model}*"
    )


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
    confidence_floor: float | None = None, max_errors: int | None = None,
    ticket_note: str = "", cost_label: str = "", size_advisory_line: str = "",
    summary_template: str = "",
) -> dict:
    """The no-chunk exit: an empty diff, or every file binary.

    No worker ever ran, but the release-shape heuristic
    (:func:`heuristics.release_shape_findings`) is pure and needs no chunk
    to fire on, so its findings — passed in by the caller, already computed
    over the full file list — are put through the same location and
    quality passes a chunk-sourced finding gets (:func:`apply_location_validation`,
    :func:`apply_quality_gate`) before they reach ``findings_active`` /
    ``verdict`` / the summary. An empty diff still yields
    ``release_shape_findings=[]`` (fewer than 2 files can never be
    release-shaped), so this degrades to exactly the prior empty-diff
    behaviour: ``Approved``, no findings, no banner.

    ``ticket_note``, ``cost_label``, ``size_advisory_line`` and
    ``summary_template`` are handed to :func:`_render_summary` unchanged; all
    four default to ``""``, which renders the summary exactly as before. The
    run-record keys are added by the caller's :func:`_run_record`, not here.
    """
    tracer = tracer if tracer is not None else get_tracer()
    elapsed_ms = _elapsed_ms(t0)
    posted = False

    findings = list(release_shape_findings or [])
    if findings:
        findings = apply_location_validation(findings, [f.path for f in files])
        findings = apply_quality_gate(
            findings, confidence_floor=confidence_floor, max_errors=max_errors,
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
        )
        try:
            forge.post_summary(ref, summary)
            posted = True
        except Exception as e:  # noqa: BLE001
            logger.error("post_summary failed: %s", e)
    _trace_post_end(tracer, wanted=wanted, posted=posted, mode=post_mode)
    return {
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
    }


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
) -> dict:
    """The error exit: post the failure notice when asked, return an Error run.

    ``cost_label`` becomes the notice attribution's last field
    (:func:`_attribution`); ``""`` leaves it as before. The notice never
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
            forge.post_summary(ref, body)
            posted = True
        except Exception as e:  # noqa: BLE001
            logger.error("post_summary (error notice) failed: %s", e)
    _trace_post_end(tracer, wanted=wanted, posted=posted, mode=post_mode)
    return {
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
    }
