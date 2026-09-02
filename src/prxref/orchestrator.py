"""Review pipeline orchestrator: fetch diff, chunk, parallel workers, quality, post.

Stage order (v1 — no Jira, no graph, no learnings, no investigator):

1. ``forge.get_pr`` → PRData, ``forge.get_diff`` → raw diff,
   ``parse_unified_diff`` → files. An empty or unchunkable diff
   short-circuits to a summary-only run with verdict ``Approved``.
2. ``build_chunks`` risk-ranked chunking (≤ ``max_chunks``, each chunk
   sized to ``token_budget`` and capped at ``max_files_per_chunk`` files).
3. Parallel worker fan-out: one ``reviewer.review_chunk(llm, files, pr)``
   call per chunk on a ThreadPoolExecutor capped at ``max_workers``. The actual
   contract is ``(findings, meta) -> tuple[list[Finding | dict], dict]``,
   with ``meta["error"]`` the empty string on success and the failure
   reason otherwise; dict findings are coerced to ``triage.Finding``. A
   legacy dict-shaped stub (``{"findings": ..., "error": ...}``) is still
   accepted for test doubles.
4. Quality passes in order: location validation (a ``file`` naming no
   path of the parsed diff is dropped, not rendered) →
   ``apply_line_align`` → thread dedup
   (existing threads fetched best-effort; failure means no threads) →
   severity consistency (findings sharing a normalized title are raised
   to the group's max severity) →
   ``apply_quality_gate(confidence_floor=, max_errors=)``. Dropped findings
   are retained in the result with ``drop_reason`` set, never silently
   discarded.
5. Verdict: ``"Error"`` when no chunk was reviewed successfully;
   ``"Request-Changes"`` iff any active error-severity finding survives;
   else ``"Approved"``. A partial failure keeps the verdict but the summary
   declares reduced coverage AND itemizes each failed chunk with the files
   it took unreviewed plus its reason (capped, redacted, inside the same
   blockquote) — a partial review reads as a successful one, so a failure
   left only in the logs reaches nobody, and a file list left out of it
   leaves the operator guessing which files went unreviewed.
6. Post: summary rendered from ``reviewer.load_prompt("summary")`` with
   placeholders ``{verdict} {title} {file_count} {error_count}
   {warning_count} {outofscope_count} {findings} {attribution}`` filled, plus
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

import logging
import re
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import reviewer
from .forges.base import ATTRIBUTION_MARKER, Forge, InlineComment, PRData, PRRef
from .llm import LLMClient
from .quality import (
    active,
    apply_line_align,
    apply_location_validation,
    apply_quality_gate,
    apply_severity_consistency,
    apply_thread_dedup,
)
from .trace import Tracer, get_tracer
from .triage import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_MAX_FILES_PER_CHUNK,
    DEFAULT_TOKEN_BUDGET,
    Finding,
    added_lines_by_file,
    build_chunks,
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

_SEVERITY_MARKERS = {"error": "🟥", "warning": "🟧", "outofscope": "🟦"}

# Inline-comment priority: the most severe findings get the anchor first, so
# a cap or a rejected anchor costs the run its least-important comments
# rather than whatever happened to sit at the tail of chunk order.
_SEVERITY_RANK = {"error": 0, "warning": 1, "outofscope": 2}

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
    "🟧 {warning_count} warning · 🟦 {outofscope_count} outofscope\n\n"
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
    post_mode: str = "summary+inline",
    post_verdict: bool = True,
    trace_file: str | None = None,
) -> dict:
    """Run one full review pass over a PR and optionally post results.

    Returns ``{verdict, findings_active, findings_dropped, chunk_count,
    chunks_reviewed, chunks_failed, elapsed_ms, input_tokens, output_tokens,
    posted}``. Never raises on ANY stage failure — forge, diff parsing,
    chunking, or LLM — the run degrades to verdict ``"Error"`` with a posted
    notice when ``post`` is true. Degenerate arguments are part of that: a
    caller passing ``max_chunks=0`` gets an error run, not a ``ValueError``.

    ``max_tokens`` is the per-chunk completion budget handed to every worker;
    ``None`` leaves ``reviewer.MAX_TOKENS`` in charge. ``token_budget`` sizes
    each diff chunk, ``max_files_per_chunk`` caps the files placed in one,
    ``context_lines`` bounds the hunk context rendered into each worker
    prompt, ``max_workers`` the fan-out, ``max_inline_comments`` the posted
    batch. ``confidence_floor`` and ``max_errors`` are forwarded to
    ``apply_quality_gate``; ``None`` leaves that pass reading the environment
    itself, which is what a library caller with no config dict wants.

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
    """
    t0 = time.perf_counter()
    tracer = get_tracer(trace_file)
    tracer.event("run", "start", forge=ref.forge, url=ref.url, number=ref.number)

    try:
        with tracer.span("forge.get_pr"):
            pr = forge.get_pr(ref)
    except Exception as e:  # noqa: BLE001
        logger.error("get_pr failed: %s", e)
        tracer.event("run", "fail")
        return _error_run(
            forge, ref, post, 0, f"get_pr failed: {e}", t0,
            post_mode=post_mode, tracer=tracer,
        )

    try:
        with tracer.span("forge.get_diff") as sp:
            raw = forge.get_diff(ref)
            sp["bytes"] = len(raw)
    except Exception as e:  # noqa: BLE001
        logger.error("get_diff failed: %s", e)
        tracer.event("run", "fail")
        return _error_run(
            forge, ref, post, 0, f"get_diff failed: {e}", t0,
            post_mode=post_mode, tracer=tracer,
        )

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
        tracer.event("run", "fail")
        return _error_run(
            forge, ref, post, 0, f"parse_unified_diff failed: {e}", t0,
            post_mode=post_mode, tracer=tracer,
        )

    try:
        with tracer.span("build_chunks") as sp:
            chunks = build_chunks(
                files, max_chunks=max_chunks, token_budget=token_budget,
                max_files_per_chunk=max_files_per_chunk,
            )
            sp["chunks"] = len(chunks)
    except Exception as e:  # noqa: BLE001
        logger.error("build_chunks failed: %s", e)
        tracer.event("run", "fail")
        return _error_run(
            forge, ref, post, 0, f"build_chunks failed: {e}", t0,
            post_mode=post_mode, tracer=tracer,
        )

    if not chunks:
        tracer.event("run", "ok", chunks_reviewed=0, findings=0)
        return _summary_only_run(
            forge, ref, pr, files, post, t0,
            post_mode=post_mode, post_verdict=post_verdict, tracer=tracer,
        )

    results = _run_workers(
        llm, chunks, pr, max_tokens=max_tokens, max_workers=max_workers,
        context_lines=context_lines, tracer=tracer,
    )

    input_tokens = sum(r["input_tokens"] for r in results)
    output_tokens = sum(r["output_tokens"] for r in results)
    model = next((r["model"] for r in results if r["model"]), "unknown")

    if all(r["error"] for r in results):
        reason = f"all {len(results)} worker reviews failed ({results[0]['error']})"
        logger.error("Total LLM failure: %s", reason)
        tracer.event("run", "fail")
        return _error_run(
            forge, ref, post, len(chunks), reason, t0, tracer=tracer,
            model=model, input_tokens=input_tokens, output_tokens=output_tokens,
            post_mode=post_mode,
        )

    chunks_failed = sum(1 for r in results if r["error"])
    chunks_reviewed = len(results) - chunks_failed

    findings = [f for r in results if not r["error"] for f in r["findings"]]

    # Futures were submitted in chunk order, so results[i] is chunk[i]'s
    # outcome: the zip is what pairs each failed review with the files it
    # took down, which the partial banner now names (issue #31).
    failed_chunks = [
        (r["error"], [f.path for f in chunk])
        for chunk, r in zip(chunks, results, strict=True)
        if r["error"]
    ]

    if post and post_mode in POST_INLINE_MODES:
        _prune_stale_inline_comments(forge, ref)

    try:
        threads = forge.list_threads(ref)
    except Exception as e:  # noqa: BLE001
        logger.warning("list_threads failed (best-effort): %s", e)
        threads = []

    findings = apply_location_validation(findings, [f.path for f in files])
    findings = apply_line_align(findings, added_lines_by_file(files), files=files)
    findings = apply_thread_dedup(findings, threads)
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
    findings = apply_quality_gate(
        findings, confidence_floor=confidence_floor, max_errors=max_errors,
    )

    findings_active = active(findings)
    findings_dropped = [f for f in findings if f.drop_reason is not None]

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
            key=lambda f: (_SEVERITY_RANK.get(f.severity, 3), -f.confidence),
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
    )
    return {
        "verdict": verdict,
        "findings_active": findings_active,
        "findings_dropped": findings_dropped,
        "chunk_count": len(chunks),
        "chunks_reviewed": chunks_reviewed,
        "chunks_failed": chunks_failed,
        "elapsed_ms": elapsed_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "posted": posted,
    }


def _elapsed_ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def _attribution(model: str, tokens: int, elapsed_ms: int) -> str:
    return f"{ATTRIBUTION_MARKER} · model={model} · {tokens} tok · {elapsed_ms / 1000:.1f}s"


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


def _run_workers(
    llm: LLMClient, chunks, pr: PRData, *, max_tokens: int | None = None,
    max_workers: int = MAX_WORKERS, context_lines: int | None = None,
    tracer: Tracer | None = None,
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
                max_tokens, context_lines, tracer,
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
                    })
            return results
        finally:
            done.set()


def _run_worker(
    index: int, total: int, llm: LLMClient, chunk, pr: PRData,
    max_tokens: int | None = None, context_lines: int | None = None,
    tracer: Tracer | None = None,
) -> dict:
    tracer = tracer if tracer is not None else get_tracer()
    t0 = time.perf_counter()
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
    )
    try:
        res = reviewer.review_chunk(
            llm, chunk, pr_title=pr.title, pr_description=pr.description,
            max_tokens=max_tokens, context_lines=context_lines,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("[chunk %d/%d] worker raised: %s", index, total, e)
        tracer.event(
            "chunk", "fail", index=index, total=total,
            elapsed_ms=_elapsed_ms(t0), error=e.__class__.__name__,
        )
        return {
            "findings": [], "error": str(e),
            "input_tokens": 0, "output_tokens": 0, "model": "",
            "elapsed_ms": _elapsed_ms(t0),
        }

    # reviewer returns (findings, meta); legacy dict stubs still accepted.
    if isinstance(res, tuple):
        findings_raw, meta = res
        res = {
            "findings": findings_raw,
            "input_tokens": meta.get("input_tokens", 0),
            "output_tokens": meta.get("output_tokens", 0),
            "model": meta.get("model", ""),
            "elapsed_ms": meta.get("elapsed_ms", _elapsed_ms(t0)),
            "error": meta.get("error", ""),
        }

    findings = []
    for item in res.get("findings") or []:
        finding = _coerce_finding(item)
        if finding is not None:
            findings.append(finding)

    error = res.get("error")
    if error:
        logger.error("[chunk %d/%d] worker reported error: %s", index, total, error)
        tracer.event(
            "chunk", "fail", index=index, total=total,
            elapsed_ms=_elapsed_ms(t0), error=str(error)[:200],
        )
    else:
        logger.info(
            "[chunk %d/%d] %d findings in %d ms",
            index, total, len(findings), _elapsed_ms(t0),
        )
        tracer.event(
            "chunk", "ok", index=index, total=total,
            elapsed_ms=_elapsed_ms(t0), findings=len(findings),
            model=res.get("model", ""),
            input_tokens=res.get("input_tokens", 0),
            output_tokens=res.get("output_tokens", 0),
        )
    return {
        "findings": findings,
        "error": error,
        "input_tokens": res.get("input_tokens", 0),
        "output_tokens": res.get("output_tokens", 0),
        "model": res.get("model", ""),
        "elapsed_ms": _elapsed_ms(t0),
    }


def _coerce_finding(item) -> Finding | None:
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
) -> str:
    try:
        template = reviewer.load_prompt("summary")
    except Exception as e:  # noqa: BLE001
        logger.warning("load_prompt('summary') failed, using fallback: %s", e)
        template = _FALLBACK_SUMMARY_TEMPLATE
    if not include_verdict:
        template = _strip_verdict_stamp(template)

    counts = {"error": 0, "warning": 0, "outofscope": 0}
    for f in findings_active:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    if findings_active:
        bullets = "\n".join(
            f"- {_SEVERITY_MARKERS.get(f.severity, '🟦')} "
            f"`{f.file}:{f.line if f.line > 0 else '—'}` — {f.title}"
            for f in findings_active
        )
    else:
        bullets = "No findings — nice work."
    if inline_accounting:
        bullets = f"{bullets}\n\n{inline_accounting}"

    attribution = _attribution(
        model, input_tokens + output_tokens, elapsed_ms,
    )
    rendered = (
        template
        .replace("{verdict}", verdict)
        .replace("{title}", pr.title)
        .replace("{file_count}", str(len(files)))
        .replace("{error_count}", str(counts["error"]))
        .replace("{warning_count}", str(counts["warning"]))
        .replace("{outofscope_count}", str(counts["outofscope"]))
        .replace("{findings}", bullets)
        .replace("{attribution}", attribution)
    )
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
    return rendered


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
    about which files went unreviewed.

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
        lines.append(f"- {_chunk_files_label(files)}: {first}")
        lines.extend(f"  {line}" for line in rest)
    hidden = max(0, len(distinct) - MAX_REPORTED_REASONS)
    if hidden:
        plural = "s" if hidden > 1 else ""
        lines.append(f"- …and {hidden} more failed chunk{plural} (see logs)")
    return lines


def _format_finding(f: Finding, model: str) -> str:
    marker = _SEVERITY_MARKERS.get(f.severity, "🟦")
    loc = f"{f.file}:{f.line}" if f.line > 0 else f.file
    return (
        f"🤖 {marker} **[{f.severity.upper()}] {f.title}** (`{loc}`)\n\n"
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
    tracer: Tracer | None = None,
) -> dict:
    tracer = tracer if tracer is not None else get_tracer()
    elapsed_ms = _elapsed_ms(t0)
    posted = False
    wanted = post and post_mode in POST_SUMMARY_MODES
    _trace_post_begin(
        tracer, wanted=wanted, mode=post_mode, kind="empty-diff summary",
        reason="posting disabled" if not post else f"post_mode={post_mode} posts no summary",
    )
    if wanted:
        summary = _render_summary(
            pr, files, "Approved", [], "unknown", 0, 0, elapsed_ms,
            chunks_reviewed=0, chunks_failed=0,
            include_verdict=post_verdict,
        )
        try:
            forge.post_summary(ref, summary)
            posted = True
        except Exception as e:  # noqa: BLE001
            logger.error("post_summary failed: %s", e)
    _trace_post_end(tracer, wanted=wanted, posted=posted, mode=post_mode)
    return {
        "verdict": "Approved",
        "findings_active": [],
        "findings_dropped": [],
        "chunk_count": 0,
        "chunks_reviewed": 0,
        "chunks_failed": 0,
        "elapsed_ms": elapsed_ms,
        "input_tokens": 0,
        "output_tokens": 0,
        "posted": posted,
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
) -> dict:
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
        "chunks_reviewed": 0,
        "chunks_failed": chunk_count,
        "elapsed_ms": elapsed_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "posted": posted,
    }
