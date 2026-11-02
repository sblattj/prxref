"""Opt-in stability mode: run the chunk workers N times and report agreement (#143).

``PRXREF_STABILITY_PASSES`` above 1 runs every chunk that many times over the
same prompts, one pass after the other. This module holds the pure parts: the
per-pass client, the per-chunk merge and the run record. The orchestrator
decides where they run. With one pass nothing here is called.

A model sampled twice on one prompt can raise a different set of findings, so
a single pass is a sample. The merge turns N samples into one result per
chunk, before any quality pass, so everything downstream sees the shape it
always saw.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from prxref.costs import combine_reported, unit_cost
from prxref.quality import (
    normalize_title,
    resolve_diff_path,
    same_cause_in_file,
    severity_rank,
)
from prxref.triage import Finding

STABILITY_DROP_PREFIX = "stability: "
"""Start of the ``drop_reason`` of a finding too few passes raised."""


def pass_seeds(seed: object, passes: int) -> list[int | None]:
    """The seed each pass sends: ``seed + pass`` for an integer seed, else ``None`` for all.

    A ``bool`` or a non-integer (an ``off`` client carries ``None``) sends no
    seed, so a pass never invents one.
    """
    if isinstance(seed, int) and not isinstance(seed, bool):
        return [seed + p for p in range(passes)]
    return [None] * passes


def pass_client(llm: Any, seed: int | None, index: int) -> Any:
    """The client pass ``index`` reviews with.

    Pass 0, and any pass with no seed, uses ``llm`` itself. Otherwise a
    shallow copy carries the pass's seed, so the shared client is never
    mutated while another thread reads it; the copy shares the original's
    session and unavailable-model memory by reference.
    """
    if index == 0 or seed is None:
        return llm
    clone = copy.copy(llm)
    clone.seed = seed
    return clone


@dataclass(frozen=True)
class StabilityOutcome:
    """What :func:`merge_passes` returns.

    ``results`` is one merged worker result per chunk, ``dropped`` the
    findings too few passes raised (each with a ``stability`` drop reason),
    and ``record`` the run record's ``stability`` value.
    """

    results: list[dict]
    dropped: list[Finding]
    record: dict[str, Any]


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _same_text(a: Finding, b: Finding) -> bool:
    """Same file, same normalized title and same body: one claim whatever line it sits on."""
    title = normalize_title(a.title or "")
    return (
        a.file == b.file
        and bool(title)
        and title == normalize_title(b.title or "")
        and " ".join((a.body or "").split()) == " ".join((b.body or "").split())
        and bool((a.body or "").strip())
    )


def _same_cluster(a: Finding, b: Finding) -> bool:
    return _same_text(a, b) or same_cause_in_file(a, b) is not None


def _clusters(ok: Sequence[tuple[int, Mapping[str, Any]]]) -> list[list[tuple[int, int, Finding]]]:
    """Group the findings of the ok passes into same-cause clusters.

    Findings are walked in pass order, then in the pass's own order. One
    joins the first cluster whose first member shares its cause, unless that
    cluster already holds a finding of the same pass: two findings of one
    pass are two defects. A finding that joins none opens a cluster.
    """
    clusters: list[list[tuple[int, int, Finding]]] = []
    for p, res in ok:
        touched: set[int] = set()
        for j, f in enumerate(res["findings"]):
            for ci, cluster in enumerate(clusters):
                if ci not in touched and _same_cluster(cluster[0][2], f):
                    cluster.append((p, j, f))
                    touched.add(ci)
                    break
            else:
                clusters.append([(p, j, f)])
                touched.add(len(clusters) - 1)
    return clusters


def _representative(cluster: Sequence[tuple[int, int, Finding]]) -> Finding:
    """Highest severity, then highest confidence, then earliest pass, then earliest position."""
    def key(member: tuple[int, int, Finding]) -> tuple[int, float, int, int]:
        p, j, f = member
        return (severity_rank(f), -float(f.confidence or 0.0), p, j)

    return min(cluster, key=key)[2]


def _merged_cost(received: Sequence[Mapping[str, Any]]) -> tuple[float | None, str]:
    parts = [unit_cost(unit)[1:] for unit in received]
    return combine_reported(parts) if parts else (None, "")


def _answer_pairs(unit: Mapping[str, Any]) -> list[list[str]]:
    return unit.get("answered_by") or [
        [str(unit.get("requested_model") or ""), str(unit.get("model") or "")],
    ]


def _merge_result(passes: Sequence[Mapping[str, Any]], findings: list[Finding]) -> dict:
    """One chunk's merged worker result: summed usage, the merged findings, the error rule."""
    ok = [res for res in passes if not res["error"]]
    pool = ok or list(passes)
    received = [res for res in passes if unit_cost(res)[0]]
    cost_usd, cost_source = _merged_cost(received)
    merged: dict[str, Any] = {
        "findings": findings,
        "error": "" if ok else passes[-1]["error"],
        "input_tokens": sum(_count(res["input_tokens"]) for res in passes),
        "output_tokens": sum(_count(res["output_tokens"]) for res in passes),
        "model": next((res["model"] for res in reversed(pool) if res["model"]), ""),
        "elapsed_ms": sum(_count(res["elapsed_ms"]) for res in passes),
        "cost_usd": cost_usd,
        "cost_source": cost_source,
    }
    retries = [res for res in passes if "parse_retries" in res]
    if retries:
        merged["parse_retries"] = sum(_count(res["parse_retries"]) for res in retries)
        merged["first_error"] = next(
            (res.get("first_error", "") for res in retries if res.get("first_error")), "",
        )
    if any(res.get("requested_model") or res.get("answered_by") for res in ok):
        pairs: list[list[str]] = []
        for res in ok:
            for pair in _answer_pairs(res):
                if pair not in pairs:
                    pairs.append(pair)
        merged["answered_by"] = pairs
        requested = ok[-1].get("requested_model")
        if requested:
            merged["requested_model"] = requested
    return merged


def _normalized(
    ok: Sequence[tuple[int, Mapping[str, Any]]], diff_paths: Sequence[str] | None,
) -> list[tuple[int, Mapping[str, Any]]]:
    """``ok`` with every finding's file resolved to a diff path of the chunk (#143).

    Uses :func:`prxref.quality.resolve_diff_path`, the resolver location repair
    uses, so ``big1.py`` and ``src/big1.py`` cluster as one file. A path that
    resolves to nothing is left as the model wrote it and so clusters only with
    the same spelling; location validation still drops it later.
    """
    if not diff_paths:
        return list(ok)
    out: list[tuple[int, Mapping[str, Any]]] = []
    for p, res in ok:
        fixed = []
        for f in res["findings"]:
            path = resolve_diff_path(f.file, diff_paths)
            fixed.append(f if path is None or path == f.file else replace(f, file=path))
        out.append((p, {**res, "findings": fixed}))
    return out


def merge_chunk(
    passes: Sequence[Mapping[str, Any]], *, min_agree: int,
    diff_paths: Sequence[str] | None = None,
) -> tuple[dict, list[Finding], list[dict[str, Any]]]:
    """Merge one chunk's pass results into ``(result, dropped, dropped_rows)``.

    A chunk fails only when every pass failed; the merged result then keeps
    the last pass's error and no findings. Otherwise findings of the ok
    passes are clustered by cause (:func:`prxref.quality.same_cause_in_file`),
    each cluster keeps one representative stamped with ``agreement`` (the
    passes that raised it) and ``passes_ok`` (the passes that did not fail),
    and a cluster seen in fewer than ``min(min_agree, passes_ok)`` passes is
    dropped with a ``stability`` reason: a failed pass shrinks the bar, so an
    outage never erases a finding. ``dropped_rows`` are the audit rows for
    the run record.
    """
    ok = [(p, res) for p, res in enumerate(passes) if not res["error"]]
    if not ok:
        return _merge_result(passes, []), [], []
    ok = _normalized(ok, diff_paths)
    passes_ok = len(ok)
    need = min(min_agree, passes_ok)
    kept: list[Finding] = []
    dropped: list[Finding] = []
    rows: list[dict[str, Any]] = []
    for cluster in _clusters(ok):
        agreement = len(cluster)
        rep = replace(_representative(cluster), agreement=agreement, passes_ok=passes_ok)
        if agreement >= need:
            kept.append(rep)
            continue
        gone = replace(
            rep,
            drop_reason=(
                f"{STABILITY_DROP_PREFIX}seen in {agreement} of {passes_ok} "
                f"passes, needs {need}"
            ),
        )
        dropped.append(gone)
        rows.append({
            "file": rep.file, "line": rep.line, "severity": rep.severity,
            "title": rep.title, "agreement": agreement, "passes_ok": passes_ok,
            "needed": need,
        })
    return _merge_result(passes, kept), dropped, rows


def merge_passes(
    pass_results: Sequence[Sequence[Mapping[str, Any]]],
    *, min_agree: int, seeds: Sequence[int | None],
    chunk_paths: Sequence[Sequence[str]] | None = None,
) -> StabilityOutcome:
    """Merge ``pass_results[p][i]`` (pass ``p``, chunk ``i``) into one result per chunk.

    The record is ``{"passes", "min_agree", "seeds", "findings_per_pass",
    "dropped"}``: ``findings_per_pass[p][i]`` is the finding count of chunk
    ``i`` in pass ``p`` (``null`` when that chunk failed in that pass), and
    ``dropped`` the audit rows of every finding dropped for want of
    agreement, each tagged with its 1-based ``chunk``.
    """
    chunks = len(pass_results[0]) if pass_results else 0
    results: list[dict] = []
    dropped: list[Finding] = []
    rows: list[dict[str, Any]] = []
    for i in range(chunks):
        merged, gone, gone_rows = merge_chunk(
            [p[i] for p in pass_results], min_agree=min_agree,
            diff_paths=chunk_paths[i] if chunk_paths is not None else None,
        )
        results.append(merged)
        dropped.extend(gone)
        rows.extend({"chunk": i + 1, **row} for row in gone_rows)
    record = {
        "passes": len(pass_results),
        "min_agree": min_agree,
        "seeds": list(seeds),
        "findings_per_pass": [
            [None if res["error"] else len(res["findings"]) for res in p]
            for p in pass_results
        ],
        "dropped": rows,
    }
    return StabilityOutcome(results=results, dropped=dropped, record=record)


def seen_line(finding: Finding) -> str:
    """``Seen in 2 of 3 passes.`` for a stability-mode finding, ``""`` when the mode is off."""
    if finding.agreement <= 0 or finding.passes_ok <= 0:
        return ""
    return f"Seen in {finding.agreement} of {finding.passes_ok} passes."


def summary_line(passes: int, min_agree: int) -> str:
    """The summary's one stability line, naming the passes and the bar."""
    bar = (
        "every finding any pass raised is listed"
        if min_agree <= 1
        else f"a finding needs {min_agree} of them to be listed"
    )
    return f"Stability mode: {passes} review passes; {bar}."
