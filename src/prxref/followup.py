"""Drive one worker chunk's context follow-up (#22).

With ``PRXREF_CONTEXT_FOLLOWUP=on`` a chunk whose first reply holds findings
below the confidence floor, asking about symbols the worker was not shown, is
re-sent once with those symbols' definitions appended. This module composes
the lookup half (:mod:`prxref.repo_followup`) and the merge half
(:mod:`prxref.followup_merge`) around one caller-supplied ``invoke``:
questions, then names, then excerpts, then the prompt block, then the single
re-run, then the merge.

The follow-up is advisory. It makes at most one ``invoke``, never triggers
another follow-up, and never raises: any failure keeps the first review's
findings, so the sub-floor questions die at the quality gate exactly as they
would without the follow-up. The re-run's tokens and reported cost are still
folded into the chunk result, because the call was billed.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any

from .costs import combine_reported
from .followup_merge import merge_followup
from .repo_followup import (
    diff_defined_names,
    finding_names,
    lookup_excerpts,
    lookup_names,
    name_tiers,
    question_indices,
    render_followup_block,
)
from .trace import Tracer, get_tracer
from .triage import Finding

logger = logging.getLogger("prxref")

SKIP_REASONS = ("no-questions", "no-names", "no-excerpts", "chunk-error", "timeout-retry")

ROW_KEYS = (
    "questions",
    "names",
    "excerpts",
    "called",
    "error",
    "confirmed",
    "unconfirmed",
    "discarded",
    "input_tokens",
    "output_tokens",
    "skipped",
)


def skipped_row(reason: str | None = None, *, questions: int = 0) -> dict:
    """A fresh run-record row for one chunk's follow-up.

    The row has every key of :data:`ROW_KEYS`: ``questions`` as given, empty
    ``names`` and ``excerpts``, ``called`` false, ``error`` ``""``, zero
    counts and tokens, and ``skipped`` set to ``reason`` (a member of
    :data:`SKIP_REASONS`, or None for a row that is still being filled). A
    caller that skips the follow-up before :func:`run_chunk_followup` runs,
    such as a chunk that took the timeout retry, records this row with
    ``reason="timeout-retry"``.
    """
    return {
        "questions": questions,
        "names": [],
        "excerpts": [],
        "called": False,
        "error": "",
        "confirmed": 0,
        "unconfirmed": 0,
        "discarded": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "skipped": reason,
    }


def _count(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fold_usage(result: dict, rerun: Mapping[str, Any], row: dict) -> None:
    extra_in = _count(rerun.get("input_tokens"))
    extra_out = _count(rerun.get("output_tokens"))
    row["input_tokens"] = extra_in
    row["output_tokens"] = extra_out
    result["input_tokens"] = _count(result.get("input_tokens")) + extra_in
    result["output_tokens"] = _count(result.get("output_tokens")) + extra_out
    model = rerun.get("model") or ""
    if model:
        result["model"] = model
    result["elapsed_ms"] = _count(result.get("elapsed_ms")) + _count(rerun.get("elapsed_ms"))
    result["cost_usd"], result["cost_source"] = combine_reported([
        (result.get("cost_usd"), str(result.get("cost_source") or "")),
        (rerun.get("cost_usd"), str(rerun.get("cost_source") or "")),
    ])


def _resolved_names(question: Finding, resolved: set[str]) -> list[str]:
    return [name for name in finding_names(question.title or "", question.body or "") if name in resolved]


def _skip(row: dict, reason: str, tracer: Tracer, index: int, total: int) -> None:
    row["skipped"] = reason
    if row["questions"] > 0:
        tracer.event(
            "followup", "skip", index=index, total=total,
            reason=reason, questions=row["questions"], names=list(row["names"]),
        )


def _fail(row: dict, error: str, tracer: Tracer, index: int, total: int) -> None:
    row["error"] = error
    logger.warning(
        "[chunk %d/%d] context follow-up failed (keeping the first review): %s",
        index, total, error,
    )
    tracer.event(
        "followup", "fail", index=index, total=total, error=error[:200],
        input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
    )


def run_chunk_followup(
    first: Mapping[str, Any],
    *,
    chunk: Sequence[object],
    all_files: Sequence[object],
    read: Callable[[str], str | None] | None,
    listing_paths: Collection[str] | None,
    listing_complete: bool,
    exclude: Callable[[str], bool] | None,
    shown: str,
    floor: float,
    invoke: Callable[[str], Mapping[str, Any]],
    index: int,
    total: int,
    tracer: Tracer | None,
) -> tuple[dict, dict]:
    """Run one chunk's context follow-up and return ``(result, row)``.

    ``first`` is the chunk's first worker result in the orchestrator's
    ``_invoke_chunk`` shape, and it is never mutated. ``chunk``, ``all_files``,
    ``read``, ``listing_paths``, ``listing_complete``, ``exclude`` and
    ``shown`` go to :func:`prxref.repo_followup.lookup_excerpts` (``read``
    also to :func:`prxref.repo_followup.diff_defined_names`). ``floor`` is the
    resolved confidence floor. ``invoke(block)`` re-sends the chunk with the
    follow-up block appended and returns a result of the same shape.

    Steps, each of which may end the follow-up with ``row["skipped"]`` set:
    a first result with an ``error`` is ``"chunk-error"``; no finding below
    ``floor`` (a finding that already has a ``drop_reason`` does not count)
    is ``"no-questions"``; no name left once the names the pull request
    defines are removed is ``"no-names"``; and no admitted excerpt is
    ``"no-excerpts"``. Otherwise ``invoke`` is called exactly once.

    ``result`` is a copy of ``first``. Its ``input_tokens``,
    ``output_tokens`` and ``elapsed_ms`` add the re-run's, its ``model``
    becomes the re-run's when that is not empty, and its ``cost_usd`` and
    ``cost_source`` fold through :func:`prxref.costs.combine_reported`, so a
    missing figure on either side gives ``None``. ``parse_retries`` and
    ``first_error`` stay the first call's. When the re-run succeeded, its
    findings go through :func:`prxref.followup_merge.merge_followup` with
    each question's resolved names: the question's own
    :func:`prxref.repo_followup.finding_names` that an admitted excerpt
    defines or covers. A re-run with an ``error``, an ``invoke`` that raises,
    or any other exception keeps the first findings unchanged and sets
    ``row["error"]``; tokens already received are still folded.

    ``row`` has the keys of :data:`ROW_KEYS`. ``index`` and ``total`` are the
    chunk's 1-based position and the chunk count, for the log lines and the
    ``followup`` trace events (``start``, ``ok``, ``fail``, and ``skip`` when
    there was at least one question).
    """
    tracer = tracer if tracer is not None else get_tracer()
    result = dict(first)
    row = skipped_row()
    try:
        if str(first.get("error") or ""):
            row["skipped"] = "chunk-error"
            return result, row
        findings = list(first.get("findings") or [])
        picked = [i for i in question_indices(findings, floor) if findings[i].drop_reason is None]
        row["questions"] = len(picked)
        if not picked:
            row["skipped"] = "no-questions"
            return result, row
        ranked = [name_tiers(findings[i].title or "", findings[i].body or "") for i in picked]
        defined = diff_defined_names(all_files, chunk, read)
        names = lookup_names(ranked, defined=defined)
        row["names"] = list(names)
        if not names:
            _skip(row, "no-names", tracer, index, total)
            return result, row
        excerpts = lookup_excerpts(
            chunk, all_files, names, read=read, listing_paths=listing_paths,
            listing_complete=listing_complete, exclude=exclude, shown=shown,
        )
        row["excerpts"] = [excerpt.record() for excerpt in excerpts]
        if not excerpts:
            _skip(row, "no-excerpts", tracer, index, total)
            return result, row
        block = render_followup_block(excerpts)
    except Exception as exc:  # noqa: BLE001
        row["skipped"] = None
        _fail(row, str(exc) or exc.__class__.__name__, tracer, index, total)
        return dict(first), row

    logger.info(
        "[chunk %d/%d] context follow-up: %d question(s), %d name(s), %d excerpt(s); re-running once",
        index, total, len(picked), len(names), len(excerpts),
    )
    tracer.event(
        "followup", "start", index=index, total=total, questions=len(picked),
        names=list(names), excerpts=len(excerpts), chars=len(block),
    )
    row["called"] = True
    try:
        rerun = invoke(block)
    except Exception as exc:  # noqa: BLE001
        _fail(row, str(exc) or exc.__class__.__name__, tracer, index, total)
        return result, row
    try:
        _fold_usage(result, rerun, row)
        error = str(rerun.get("error") or "")
        if error:
            _fail(row, error, tracer, index, total)
            return result, row
        resolved: set[str] = set()
        for excerpt in excerpts:
            resolved.add(excerpt.symbol)
            resolved.update(excerpt.covers)
        questions: dict[int, list[str]] = {}
        for i in picked:
            own = _resolved_names(findings[i], resolved)
            if own:
                questions[i] = own
        rerun_findings = [f for f in (rerun.get("findings") or []) if isinstance(f, Finding)]
        outcome = merge_followup(findings, questions, rerun_findings, floor=floor)
    except Exception as exc:  # noqa: BLE001
        _fail(row, str(exc) or exc.__class__.__name__, tracer, index, total)
        return result, row

    result["findings"] = list(outcome.findings)
    row["confirmed"] = outcome.confirmed
    row["unconfirmed"] = outcome.unconfirmed
    row["discarded"] = outcome.discarded
    logger.info(
        "[chunk %d/%d] context follow-up: %d confirmed, %d not confirmed, %d other finding(s) discarded",
        index, total, outcome.confirmed, outcome.unconfirmed, outcome.discarded,
    )
    tracer.event(
        "followup", "ok", index=index, total=total,
        confirmed=outcome.confirmed, unconfirmed=outcome.unconfirmed,
        discarded=outcome.discarded, model=result.get("model", ""),
        input_tokens=row["input_tokens"], output_tokens=row["output_tokens"],
    )
    return result, row
