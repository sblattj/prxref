"""Review pipeline orchestrator: fetch diff, chunk, parallel workers, quality, post.

Stage order (v1 — no Jira, no graph, no learnings, no investigator):

1. ``forge.get_pr`` → PRData, ``forge.get_diff`` → raw diff,
   ``parse_unified_diff`` → files. An empty or unchunkable diff
   short-circuits to a summary-only run with verdict ``Approved``.
2. ``build_chunks`` risk-ranked chunking (≤ ``max_chunks``).
3. Parallel worker fan-out: one ``reviewer.review_chunk(llm, files, pr)``
   call per chunk on a ThreadPoolExecutor capped at 4 workers. The actual
   contract is ``(findings, meta) -> tuple[list[Finding | dict], dict]``,
   with ``meta["error"]`` the empty string on success and the failure
   reason otherwise; dict findings are coerced to ``triage.Finding``. A
   legacy dict-shaped stub (``{"findings": ..., "error": ...}``) is still
   accepted for test doubles.
4. Quality passes in order: ``apply_line_align`` → thread dedup
   (existing threads fetched best-effort; failure means no threads) →
   ``apply_quality_gate``. Dropped findings are retained in the result
   with ``drop_reason`` set, never silently discarded.
5. Verdict: ``"Error"`` when no chunk was reviewed successfully;
   ``"Request-Changes"`` iff any active error-severity finding survives;
   else ``"Approved"``. A partial failure keeps the verdict but the summary
   declares reduced coverage.
6. Post: summary rendered from ``reviewer.load_prompt("summary")`` with
   placeholders ``{verdict} {title} {file_count} {error_count}
   {warning_count} {note_count} {findings} {attribution}`` filled, plus
   inline comments for up to 15 active findings.

No stage failure raises out of ``orchestrate_review``: a forge failure or
a total LLM failure degrades to verdict ``"Error"`` with a posted notice
(when ``post`` is true). Exit-code posture lives in the CLI.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from . import reviewer
from .forges.base import Forge, InlineComment, PRData, PRRef
from .llm import LLMClient
from .quality import active, apply_line_align, apply_quality_gate, apply_thread_dedup
from .triage import Finding, added_lines_by_file, build_chunks, parse_unified_diff

logger = logging.getLogger("prxref")

MAX_WORKERS = 4
MAX_INLINE_COMMENTS = 15

_SEVERITY_EMOJI = {"error": "🚨", "warning": "⚠️", "note": "📝"}

_FALLBACK_SUMMARY_TEMPLATE = (
    "🤖 **prxref review — {verdict}**\n\n"
    "PR: {title}\n\n"
    "Files reviewed: {file_count} · errors: {error_count} · "
    "warnings: {warning_count} · notes: {note_count}\n\n"
    "{findings}\n\n{attribution}"
)


def orchestrate_review(
    forge: Forge,
    ref: PRRef,
    llm: LLMClient,
    *,
    post: bool = True,
    max_chunks: int = 8,
) -> dict:
    """Run one full review pass over a PR and optionally post results.

    Returns ``{verdict, findings_active, findings_dropped, chunk_count,
    chunks_reviewed, chunks_failed, elapsed_ms, input_tokens, output_tokens,
    posted}``. Never raises on forge or LLM stage failure — the run degrades
    to verdict ``"Error"`` with a posted notice when ``post`` is true.
    """
    t0 = time.perf_counter()

    try:
        pr = forge.get_pr(ref)
    except Exception as e:  # noqa: BLE001
        logger.error("get_pr failed: %s", e)
        return _error_run(forge, ref, post, 0, f"get_pr failed: {e}", t0)

    try:
        raw = forge.get_diff(ref)
    except Exception as e:  # noqa: BLE001
        logger.error("get_diff failed: %s", e)
        return _error_run(forge, ref, post, 0, f"get_diff failed: {e}", t0)

    files = parse_unified_diff(raw)
    chunks = build_chunks(files, max_chunks=max_chunks)

    if not chunks:
        return _summary_only_run(forge, ref, pr, files, post, t0)

    results = _run_workers(llm, chunks, pr)

    input_tokens = sum(r["input_tokens"] for r in results)
    output_tokens = sum(r["output_tokens"] for r in results)
    model = next((r["model"] for r in results if r["model"]), "unknown")

    if all(r["error"] for r in results):
        reason = f"all {len(results)} worker reviews failed ({results[0]['error']})"
        logger.error("Total LLM failure: %s", reason)
        return _error_run(
            forge, ref, post, len(chunks), reason, t0,
            model=model, input_tokens=input_tokens, output_tokens=output_tokens,
        )

    chunks_failed = sum(1 for r in results if r["error"])
    chunks_reviewed = len(results) - chunks_failed

    findings = [f for r in results if not r["error"] for f in r["findings"]]

    try:
        threads = forge.list_threads(ref)
    except Exception as e:  # noqa: BLE001
        logger.warning("list_threads failed (best-effort): %s", e)
        threads = []

    findings = apply_line_align(findings, added_lines_by_file(files))
    findings = apply_thread_dedup(findings, threads)
    findings = apply_quality_gate(findings)

    findings_active = active(findings)
    findings_dropped = [f for f in findings if f.drop_reason is not None]

    verdict = (
        "Request-Changes"
        if any(f.severity == "error" for f in findings_active)
        else "Approved"
    )

    elapsed_ms = _elapsed_ms(t0)
    posted = False
    if post:
        summary = _render_summary(
            pr, files, verdict, findings_active, model,
            input_tokens, output_tokens, elapsed_ms,
            chunks_reviewed=chunks_reviewed, chunks_failed=chunks_failed,
        )
        try:
            forge.post_summary(ref, summary)
            posted = True
        except Exception as e:  # noqa: BLE001
            logger.error("post_summary failed: %s", e)
        if posted and findings_active:
            comments = [
                InlineComment(
                    path=f.file,
                    line=f.line,
                    body=_format_finding(f, model),
                )
                for f in findings_active[:MAX_INLINE_COMMENTS]
            ]
            try:
                forge.post_inline_comments(ref, comments)
            except Exception as e:  # noqa: BLE001
                logger.error("post_inline_comments failed: %s", e)

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
    return f"Reviewed by prxref · model={model} · {tokens} tok · {elapsed_ms / 1000:.1f}s"


def _run_workers(llm: LLMClient, chunks, pr: PRData) -> list[dict]:
    workers = min(MAX_WORKERS, len(chunks))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [
            ex.submit(_run_worker, i + 1, len(chunks), llm, chunk, pr)
            for i, chunk in enumerate(chunks)
        ]
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


def _run_worker(index: int, total: int, llm: LLMClient, chunk, pr: PRData) -> dict:
    t0 = time.perf_counter()
    try:
        res = reviewer.review_chunk(
            llm, chunk, pr_title=pr.title, pr_description=pr.description
        )
    except Exception as e:  # noqa: BLE001
        logger.error("[chunk %d/%d] worker raised: %s", index, total, e)
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
    else:
        logger.info(
            "[chunk %d/%d] %d findings in %d ms",
            index, total, len(findings), _elapsed_ms(t0),
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
) -> str:
    try:
        template = reviewer.load_prompt("summary")
    except Exception as e:  # noqa: BLE001
        logger.warning("load_prompt('summary') failed, using fallback: %s", e)
        template = _FALLBACK_SUMMARY_TEMPLATE

    counts = {"error": 0, "warning": 0, "note": 0}
    for f in findings_active:
        counts[f.severity] = counts.get(f.severity, 0) + 1

    if findings_active:
        bullets = "\n".join(
            f"- {_SEVERITY_EMOJI.get(f.severity, '📝')} "
            f"`{f.file}:{f.line if f.line > 0 else '—'}` — {f.title}"
            for f in findings_active
        )
    else:
        bullets = "No findings — nice work."

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
        .replace("{note_count}", str(counts["note"]))
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
    return rendered


def _format_finding(f: Finding, model: str) -> str:
    emoji = _SEVERITY_EMOJI.get(f.severity, "📝")
    loc = f"{f.file}:{f.line}" if f.line > 0 else f.file
    return (
        f"🤖 {emoji} **[{f.severity.upper()}] {f.title}** (`{loc}`)\n\n"
        f"{f.body}\n\n"
        f"---\n*Reviewed by prxref · model={model}*"
    )


def _summary_only_run(forge: Forge, ref: PRRef, pr: PRData, files, post: bool, t0: float) -> dict:
    elapsed_ms = _elapsed_ms(t0)
    posted = False
    if post:
        summary = _render_summary(
            pr, files, "Approved", [], "unknown", 0, 0, elapsed_ms,
            chunks_reviewed=0, chunks_failed=0,
        )
        try:
            forge.post_summary(ref, summary)
            posted = True
        except Exception as e:  # noqa: BLE001
            logger.error("post_summary failed: %s", e)
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
) -> dict:
    elapsed_ms = _elapsed_ms(t0)
    posted = False
    if post:
        attribution = _attribution(
            model, input_tokens + output_tokens, elapsed_ms,
        )
        body = (
            "🤖 **prxref review — Error**\n\n"
            f"The review could not complete: {reason}\n\n"
            "No findings were produced.\n\n"
            f"{attribution}"
        )
        try:
            forge.post_summary(ref, body)
            posted = True
        except Exception as e:  # noqa: BLE001
            logger.error("post_summary (error notice) failed: %s", e)
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
