"""Precision grading for ``prxref eval score --precision``.

Recall asks whether the reviewer found what humans found. This module asks
the other half: of the active AI findings that no credited grade names (the
set :func:`prxref.eval_metrics.case_row` counts as ``unmatched_ai``), how many
were worth posting. A grouped finding is one unit.

One single-shot ``json_mode`` call per case with at least one such finding,
using the packaged ``prompts/precision.md``, returns one verdict per unit:
``valid``, ``nit``, ``invalid``, ``duplicate`` or ``unverifiable``. The judge
sees each finding, the case's ticket text when it has one, and the diff hunks
of the file each finding names, read from the case's ``diff_file`` or else
from ``<run>/cases/<id>/diff.patch``; with neither it is told no diff was
available. A reply the parser rejects is sent again with the same request up
to ``parse_retries`` times. A failed call, or a reply still rejected when the
retries run out, makes every unit of that case ``judge_error``: never cached,
never counted.

Replies are cached, one JSON file per cache key, under the run's
``judge-cache/``. The key hashes the precision prompt sha256, the judge
model, the findings, the ticket text and the diff text as rendered into the
prompt. A cache hit makes no call.

:func:`summarize` turns verdict counts into the ``score.json`` block and
:func:`rates` into the strict and lenient precision. This module never
imports :mod:`prxref.cli`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import costs, eval_metrics, reviewer
from .eval_judge import (
    JUDGE_CACHE_VERSION,
    _corrupt,
    _elapsed_ms,
    _live,
    _trace_meta,
    _write_entry,
)
from .judge import JudgeParseError
from .llm import LLMClient
from .parser import loads_lenient
from .reviewer import _fold_retry_usage, _write_trace_files

logger = logging.getLogger("prxref")

PRECISION_PROMPT_NAME = "precision"
PRECISION_SPLIT_MARKER = "## Case"
PRECISION_TRACE_LABEL = "precision"
PRECISION_FLAG = "--precision"
PRECISION_CACHE_KIND = "precision"

VALID = "valid"
NIT = "nit"
INVALID = "invalid"
DUPLICATE = "duplicate"
UNVERIFIABLE = "unverifiable"
JUDGE_ERROR = "judge_error"
VERDICTS = (VALID, NIT, INVALID, DUPLICATE, UNVERIFIABLE)
COUNT_KEYS = ("graded", "matched", *VERDICTS, JUDGE_ERROR)
DIFF_PATCH_NAME = "diff.patch"

MAX_TICKET_CHARS = 8000
MAX_DIFF_CHARS = 60000
NO_TICKET = "No ticket text."
NO_DIFF = "No diff was available for this case: grade `unverifiable` any claim the findings alone cannot settle."
NO_FILE_DIFF = "The diff has no hunks for this file."
_TRUNCATED = "\n[truncated]"
_AI_REF_PREFIX = "A"
_MATCHED_PREFIX = "M"


@dataclass(frozen=True)
class Verdict:
    """One AI finding's verdict: ``ai_ref`` is its index in the record's ``findings`` list."""

    ai_ref: int
    verdict: str
    reason: str | None = None


@dataclass(frozen=True)
class PrecisionOutcome:
    """The result of grading one case's unmatched AI findings.

    ``verdicts`` holds one :class:`Verdict` per unit, in record order, or is
    ``None`` exactly when ``error`` is set: a judge error carries no
    verdicts, so it is never read as ``invalid``. The cost, token and retry
    fields mean what they mean on :class:`prxref.eval_judge.JudgeOutcome`,
    and :func:`prxref.eval_judge.judge_cost` totals ``unit`` the same way.
    """

    case_id: str
    cache_key: str
    verdicts: tuple[Verdict, ...] | None
    error: str | None
    cached: bool
    llm_calls: int
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_ms: int = 0
    cost_usd: float | None = 0.0
    cost_estimated: bool = False
    cost_source: str = ""
    unit: dict[str, Any] | None = None
    parse_retries: int = 0

    @property
    def ok(self) -> bool:
        """Whether the case was graded; false for a judge error."""
        return self.error is None


def load_precision_template() -> str:
    """The packaged ``prompts/precision.md`` text."""
    return reviewer.load_prompt(PRECISION_PROMPT_NAME)


def precision_prompt_sha(template: str | None = None) -> str:
    """The sha256 hex digest of the precision template (the packaged one by default)."""
    text = load_precision_template() if template is None else template
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def credited_refs(grades: Iterable[Mapping[str, Any]]) -> set[int]:
    """The record indexes some ``full`` or ``partial`` grade names."""
    refs: set[int] = set()
    for grade in grades:
        ref = grade.get("ai_ref")
        if grade.get("grade") in (eval_metrics.FULL, eval_metrics.PARTIAL) and isinstance(ref, int):
            refs.add(ref)
    return refs


def unit_indexes(findings: Sequence[Any], credited: set[int]) -> list[int]:
    """The record indexes of the active findings no credited grade names, in record order."""
    return [
        index for index, row in enumerate(findings)
        if _get(row, "drop_reason") is None and index not in credited
    ]


def load_diff(diff_file: str | None, case_dir: Path | None) -> str | None:
    """The case's diff text, or ``None``: ``diff_file`` first, else ``<case_dir>/diff.patch``."""
    candidates = []
    if diff_file:
        candidates.append(Path(diff_file))
    if case_dir is not None:
        candidates.append(case_dir / DIFF_PATCH_NAME)
    for path in candidates:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return None


def diff_by_file(diff: str) -> dict[str, str]:
    """Split a unified diff into ``{path: that file's text}``, in diff order.

    A section starts at a ``diff --git`` line, or at a ``---`` line followed by
    a ``+++`` line when the diff has no git headers. The path is the ``+++``
    one without its ``a/`` / ``b/`` prefix, the ``---`` one for a deletion.
    """
    sections: list[list[str]] = []
    lines = diff.splitlines()
    git_style = any(line.startswith("diff --git ") for line in lines)
    for index, line in enumerate(lines):
        starts = line.startswith("diff --git ") if git_style else (
            line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ ")
        )
        if starts:
            sections.append([])
        if sections:
            sections[-1].append(line)
    files: dict[str, str] = {}
    for section in sections:
        path = _section_path(section)
        if path:
            files[path] = files.get(path, "") + "\n".join(section) + "\n"
    return files


def _section_path(section: Sequence[str]) -> str:
    old = new = ""
    for line in section:
        if line.startswith("--- ") and not old:
            old = _strip_prefix(line[4:])
        elif line.startswith("+++ ") and not new:
            new = _strip_prefix(line[4:])
        elif line.startswith("@@"):
            break
    if new and new != "/dev/null":
        return new
    if old and old != "/dev/null":
        return old
    match = re.match(r"diff --git a/(.+) b/(.+)$", section[0])
    return match.group(2) if match else ""


def _strip_prefix(operand: str) -> str:
    operand = operand.split("\t", 1)[0].strip().strip('"')
    if operand.startswith(("a/", "b/")):
        return operand[2:]
    return operand


def finding_files(row: Any) -> list[str]:
    """Every file one AI finding names: its own, then those of a grouped finding's locations."""
    files: list[str] = []
    for file, _line in eval_metrics.credit_locations(row):
        if file and file not in files:
            files.append(file)
    return files


def _finding_row(ref: str, row: Any) -> dict[str, Any]:
    return {
        "ai_ref": ref,
        "file": _text(_get(row, "file")),
        "line": _int(_get(row, "line")),
        "severity": _text(_get(row, "severity")),
        "title": _text(_get(row, "title")),
        "body": _text(_get(row, "body")),
    }


def _matched_row(ref: str, row: Any) -> dict[str, Any]:
    return {key: value for key, value in _finding_row(ref, row).items() if key != "body"} | {
        "body": _text(_get(row, "body"))[:400],
    }


def render_diff(diff: str | None, rows: Sequence[Any]) -> str:
    """The diff slot: the hunks of every file ``rows`` name, or the reason there are none."""
    if diff is None:
        return NO_DIFF
    by_file = diff_by_file(diff)
    parts: list[str] = []
    for row in rows:
        for file in finding_files(row):
            if file in by_file:
                parts.append(f"#### {file}\n\n```diff\n{by_file.pop(file).rstrip()}\n```")
            elif not any(part.startswith(f"#### {file}\n") for part in parts):
                parts.append(f"#### {file}\n\n{NO_FILE_DIFF}")
    text = "\n\n".join(parts) if parts else NO_FILE_DIFF
    return text if len(text) <= MAX_DIFF_CHARS else text[:MAX_DIFF_CHARS] + _TRUNCATED


def render_ticket(ticket: str | None) -> str:
    """The ticket slot: the text capped at :data:`MAX_TICKET_CHARS`, or a note that there is none."""
    if not ticket or not ticket.strip():
        return NO_TICKET
    text = ticket.strip()
    return text if len(text) <= MAX_TICKET_CHARS else text[:MAX_TICKET_CHARS] + _TRUNCATED


def build_precision_prompt(
    findings: Sequence[Any], units: Sequence[int], credited: Iterable[int], *, ticket: str | None, diff: str | None,
) -> str:
    """Fill ``prompts/precision.md`` for one case.

    ``units`` are the record indexes to grade, numbered ``A1``, ``A2``, ...
    in order; ``credited`` are the active findings a credited grade names,
    shown as context under ``M1``, ``M2``, ... so the judge can call a unit a
    duplicate of one of them. The template is filled in one pass by
    :func:`prxref.reviewer.fill_template`.
    """
    rows = [findings[index] for index in units]
    matched = [findings[index] for index in sorted(credited) if _get(findings[index], "drop_reason") is None]
    return reviewer.fill_template(load_precision_template(), {
        "ticket": render_ticket(ticket),
        "matched": _json([_matched_row(f"{_MATCHED_PREFIX}{n}", row) for n, row in enumerate(matched, 1)]),
        "findings": _json([_finding_row(f"{_AI_REF_PREFIX}{n}", row) for n, row in enumerate(rows, 1)]),
        "diff": render_diff(diff, rows),
    })


def split_precision_prompt(prompt: str) -> tuple[str, str]:
    """Cut a filled prompt at :data:`PRECISION_SPLIT_MARKER` into ``(system, user)``."""
    head, marker, tail = prompt.partition(PRECISION_SPLIT_MARKER)
    if not marker:
        raise ValueError(f"precision prompt is missing the {PRECISION_SPLIT_MARKER!r} split marker")
    return head.strip(), (marker + tail).strip()


def precision_cache_key(prompt_sha: str, model: str, prompt: str) -> str:
    """The sha256 hex digest addressing one case's cached precision reply.

    Hashes the template sha256, the judge model and the filled prompt, which
    holds the findings, the ticket and the diff text exactly as the judge
    sees them.
    """
    payload = {"kind": PRECISION_CACHE_KIND, "prompt_sha256": prompt_sha, "model": model, "prompt": prompt}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def parse_precision_response(text: str, units: Sequence[int]) -> list[Verdict]:
    """Parse the reply into one :class:`Verdict` per unit, in ``units`` order.

    The reply is an object with a ``verdicts`` list of ``{"ai_ref",
    "verdict", "reason"}``; ``ai_ref`` is ``A1`` for ``units[0]``, and the
    verdict is matched after trimming and lowercasing. Raises
    :class:`~prxref.judge.JudgeParseError` for a reply that is not that shape,
    an unknown verdict, an unknown, repeated or missing ``ai_ref``.
    """
    if not isinstance(text, str) or not text.strip():
        raise JudgeParseError("precision response is empty")
    try:
        data = loads_lenient(text)
    except json.JSONDecodeError as exc:
        raise JudgeParseError(f"precision response is not JSON: {exc}") from exc
    rows = data.get("verdicts") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise JudgeParseError("precision response has no 'verdicts' list")
    index_of = {f"{_AI_REF_PREFIX}{n}": index for n, index in enumerate(units, 1)}
    found: dict[str, Verdict] = {}
    for position, row in enumerate(rows):
        if not isinstance(row, dict):
            raise JudgeParseError(f"verdicts[{position}] is not an object")
        ref = row.get("ai_ref")
        ref = ref.strip() if isinstance(ref, str) else ref
        if ref not in index_of:
            raise JudgeParseError(
                f"verdicts[{position}] has ai_ref {row.get('ai_ref')!r}, which is not a listed finding"
            )
        if ref in found:
            raise JudgeParseError(f"verdicts[{position}] repeats ai_ref {ref!r}")
        verdict = row.get("verdict")
        verdict = verdict.strip().lower() if isinstance(verdict, str) else verdict
        if verdict not in VERDICTS:
            raise JudgeParseError(
                f"verdicts[{position}] has verdict {row.get('verdict')!r}, not one of {', '.join(VERDICTS)}"
            )
        reason = row.get("reason")
        text = reason.strip() if isinstance(reason, str) else ""
        found[ref] = Verdict(index_of[ref], verdict, text or None)
    missing = [ref for ref in index_of if ref not in found]
    if missing:
        raise JudgeParseError(f"precision response has no verdict for {', '.join(missing)}")
    return [found[ref] for ref in index_of]


def grade_case(
    client: LLMClient,
    judge_model: str,
    case_id: str,
    findings: Sequence[Any],
    credited: set[int],
    *,
    ticket: str | None = None,
    diff: str | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    price_table: Mapping[str, Any] | None = None,
    max_tokens: int = 4096,
    timeout_s: float | None = None,
    trace_dir: str = "",
    parse_retries: int = 0,
) -> PrecisionOutcome | None:
    """Grade one case's unmatched active AI findings; ``None`` when it has none.

    ``findings`` is the record's ``findings`` list and ``credited`` the
    record indexes some credited grade names (:func:`credited_refs`). A case
    with no unit makes no call and returns ``None``. A valid cache entry is
    used without a call (a corrupt one is a miss with a WARNING). Otherwise
    ``client.invoke(system, user, json_mode=True)`` is called, sent again
    unchanged while :func:`parse_precision_response` rejects the reply and
    fewer than ``parse_retries`` retries ran. An exception the client raises
    is never retried. A call that fails or a reply still rejected when the
    retries run out is a judge error with a WARNING, never cached. A live
    call's prompt and reply are traced as ``precision.*`` in ``trace_dir``,
    and its cost is priced as :func:`prxref.eval_judge.judge_case` prices
    its own.
    """
    units = unit_indexes(findings, credited)
    if not units:
        return None
    judge_model = judge_model.strip()
    prompt = build_precision_prompt(findings, units, credited, ticket=ticket, diff=diff)
    prompt_sha = precision_prompt_sha()
    key = precision_cache_key(prompt_sha, judge_model, prompt)
    base = {"case_id": case_id, "cache_key": key}
    entry_path = Path(cache_dir) / f"{key}.json" if cache_dir is not None else None
    if entry_path is not None:
        hit = _cached_verdicts(entry_path, key, judge_model, prompt_sha, units)
        if hit is not None:
            verdicts, stored_model = hit
            return PrecisionOutcome(**base, verdicts=tuple(verdicts), error=None, cached=True, llm_calls=0,
                                    model=stored_model)
    system, user = split_precision_prompt(prompt)
    t0 = time.perf_counter()
    unit: dict[str, Any] = {"model": "", "input_tokens": 0, "output_tokens": 0, "cost_usd": None, "cost_source": ""}
    attempts: list[str | None] | None = [] if parse_retries >= 1 else None
    retries = 0
    first_error = ""
    while True:
        try:
            result = client.invoke(system, user, max_tokens=max_tokens, json_mode=True, timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001 - a failed judge call is a judge error, never a failed run
            elapsed = _elapsed_ms(t0)
            reason = f"precision call failed: {type(exc).__name__}: {exc}"
            logger.warning("precision error for case %r: %s", case_id, reason)
            meta = {**_trace_meta(unit, elapsed, retries, first_error), "error": reason}
            _write_trace_files(trace_dir, PRECISION_TRACE_LABEL, system, user, None, meta, attempts=attempts)
            return PrecisionOutcome(**_live(base, unit, elapsed, retries, price_table), verdicts=None, error=reason)
        elapsed = _elapsed_ms(t0)
        if retries:
            _fold_retry_usage(unit, result)
        else:
            unit = {
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": result.cost_usd,
                "cost_source": result.cost_source,
            }
        try:
            verdicts = parse_precision_response(result.text, units)
        except JudgeParseError as exc:
            reason = f"precision response rejected: {exc}"
            if attempts is not None and retries < parse_retries:
                retries += 1
                attempts.append(result.text)
                first_error = first_error or reason
                logger.warning(
                    "precision for case %r: unusable reply (%s); parse retry %d of %d",
                    case_id, exc, retries, parse_retries,
                )
                continue
            logger.warning("precision error for case %r: %s", case_id, reason)
            meta = {**_trace_meta(unit, elapsed, retries, first_error), "error": reason}
            _write_trace_files(trace_dir, PRECISION_TRACE_LABEL, system, user, result.text, meta, attempts=attempts)
            return PrecisionOutcome(**_live(base, unit, elapsed, retries, price_table), verdicts=None, error=reason)
        break
    live = _live(base, unit, elapsed, retries, price_table)
    meta = _trace_meta(unit, elapsed, retries, first_error)
    _write_trace_files(trace_dir, PRECISION_TRACE_LABEL, system, user, result.text, meta, attempts=attempts)
    if entry_path is not None:
        _write_entry(entry_path, {
            "version": JUDGE_CACHE_VERSION,
            "kind": PRECISION_CACHE_KIND,
            "key": key,
            "model": judge_model,
            "prompt_sha256": prompt_sha,
            "case_id": case_id,
            "response_model": result.model,
            "response": result.text,
        })
    return PrecisionOutcome(**live, verdicts=tuple(verdicts), error=None)


def _cached_verdicts(
    path: Path, key: str, judge_model: str, prompt_sha: str, units: Sequence[int],
) -> tuple[list[Verdict], str] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        return _corrupt(path, f"unreadable ({exc})")
    try:
        entry = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _corrupt(path, f"not JSON ({exc.msg})")
    if not isinstance(entry, dict):
        return _corrupt(path, f"a JSON {type(entry).__name__}, not an object")
    for field, want in (("version", JUDGE_CACHE_VERSION), ("kind", PRECISION_CACHE_KIND), ("key", key),
                        ("model", judge_model), ("prompt_sha256", prompt_sha)):
        if entry.get(field) != want:
            return _corrupt(path, f"its {field} {entry.get(field)!r} is not {want!r}")
    if not isinstance(entry.get("response"), str):
        return _corrupt(path, "it has no response text")
    try:
        verdicts = parse_precision_response(entry["response"], units)
    except JudgeParseError as exc:
        return _corrupt(path, f"its response no longer parses ({exc})")
    stored = entry.get("response_model")
    return verdicts, stored if isinstance(stored, str) else ""


def case_block(
    outcome: PrecisionOutcome | None, units: Sequence[int], matched: int,
) -> dict[str, Any]:
    """One case's ``precision`` row: the verdicts and the counts.

    ``units`` are the record indexes that were to be graded and ``matched``
    the number of active findings a credited grade names. ``outcome`` is
    ``None`` for a case with no unit. A judge error gives every unit the
    verdict ``judge_error`` (with the error as reason) and counts it in
    ``judge_error``, not in ``graded``.
    """
    if outcome is None or outcome.verdicts is None:
        reason = outcome.error if outcome is not None else None
        verdicts = [{"ai_ref": index, "verdict": JUDGE_ERROR, "reason": reason} for index in units]
    else:
        verdicts = [{"ai_ref": v.ai_ref, "verdict": v.verdict, "reason": v.reason} for v in outcome.verdicts]
    counts = {key: 0 for key in COUNT_KEYS}
    counts["matched"] = matched
    for entry in verdicts:
        counts[entry["verdict"]] += 1
    counts["graded"] = sum(counts[name] for name in VERDICTS)
    return {"verdicts": verdicts, **counts}


def rates(counts: Mapping[str, int]) -> tuple[float | None, float | None]:
    """``(strict, lenient)`` precision from verdict counts; ``None`` when the denominator is 0.

    The denominator is ``matched + valid + nit + invalid + duplicate``;
    ``unverifiable`` and ``judge_error`` are outside it. Strict counts
    ``matched + valid``; lenient adds ``nit``.
    """
    denominator = counts["matched"] + counts[VALID] + counts[NIT] + counts[INVALID] + counts[DUPLICATE]
    if denominator == 0:
        return None, None
    strict = (counts["matched"] + counts[VALID]) / denominator
    lenient = (counts["matched"] + counts[VALID] + counts[NIT]) / denominator
    return strict, lenient


def summarize(blocks: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The ``metrics.precision`` block: the summed counts, then ``strict`` and ``lenient``."""
    totals = {key: 0 for key in COUNT_KEYS}
    for block in blocks:
        for key in COUNT_KEYS:
            totals[key] += block[key]
    strict, lenient = rates(totals)
    return {**totals, "strict": strict, "lenient": lenient}


def add_cost(cost: float | None, estimated: bool, outcome: PrecisionOutcome | None) -> tuple[float | None, bool]:
    """Fold a precision outcome's cost into a case's judge cost; unknown stays ``None``, never summed."""
    if outcome is None:
        return cost, estimated
    if cost is None or outcome.cost_usd is None or costs.valid_usd(outcome.cost_usd) is None:
        return None, estimated or outcome.cost_estimated
    return cost + outcome.cost_usd, estimated or outcome.cost_estimated


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _json(rows: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(list(rows), indent=2, ensure_ascii=False)
