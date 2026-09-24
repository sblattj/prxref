"""The LLM judge of ``prxref eval score``: prompt, response parser, grade cap, cache key.

A case whose human labels carry no ``must_match`` predicate is scored by one
single-shot judge call. This module holds the pure half of that call; the
client, the cache storage and the cost live with the caller.

1. :func:`assign_refs` numbers one run record's post-gate findings ``A1``,
   ``A2``, ... in record order, skipping any row with a ``drop_reason``.
2. :func:`build_judge_prompt` fills ``prompts/judge.md`` with the case's human
   findings and the AI findings in the same files; :func:`split_judge_prompt`
   cuts it into the ``system`` / ``user`` pair ``LLMClient.invoke`` takes.
3. :func:`parse_judge_response` turns the judge's JSON into exactly one
   :class:`Grade` per human finding, in label order. Code, not the judge,
   enforces the scoring rules: a credited ``ai_ref`` must exist and sit in the
   human finding's file, and one AI finding credits at most
   :data:`MAX_CREDITS_PER_AI_FINDING` human findings. A grade those rules
   override becomes ``none`` with a :attr:`Grade.reason` and a logged warning.
   Output that is not the promised shape raises :class:`JudgeParseError`,
   which the caller records as a judge error, never as a score of 0.
4. :func:`judge_cache_key` addresses a cached grading by the template sha256,
   the judge model, and the labels and AI findings exactly as the prompt shows
   them.

Cases and findings are read by duck typing: a mapping key or an attribute of
the same name, so a run record's JSON rows, :class:`prxref.triage.Finding`
objects and the eval case loader's records all work. A human label supplies
``id``, ``file``, ``line``, ``severity`` and optional ``text``; a case supplies
its labels as ``expected``. Human severity is passed through as given.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import reviewer
from .parser import loads_lenient

logger = logging.getLogger("prxref")

JUDGE_PROMPT_NAME = "judge"
JUDGE_PROMPT_VERSION = 1
JUDGE_SPLIT_MARKER = "## Case"

GRADE_FULL = "full"
GRADE_PARTIAL = "partial"
GRADE_NONE = "none"
GRADES = (GRADE_FULL, GRADE_PARTIAL, GRADE_NONE)

MAX_CREDITS_PER_AI_FINDING = 2
AI_REF_PREFIX = "A"

REASON_MISSING = "missing"
REASON_UNKNOWN_REF = "unknown_ref"
REASON_OTHER_FILE = "other_file"
REASON_OVER_CAP = "over_cap"

_GRADE_RANK = {GRADE_FULL: 0, GRADE_PARTIAL: 1}


class JudgeParseError(ValueError):
    """The judge's response is not a ``{"grades": [...]}`` object of well-formed rows.

    Raised for unparseable JSON, a non-object top level, a missing or
    non-list ``grades``, a row that is not an object, a row without a string
    ``human_id``, and a ``grade`` outside :data:`GRADES`. The caller counts
    the case as a judge error; it is never scored as 0.
    """


@dataclass(frozen=True)
class AIFinding:
    """One post-gate AI finding as the judge sees it, under its ``ref``."""

    ref: str
    file: str
    line: int | None
    severity: str
    title: str
    body: str


@dataclass(frozen=True)
class Grade:
    """The judge's verdict on one human finding, after the code-enforced rules.

    ``grade`` is one of :data:`GRADES`. ``ai_ref`` is the crediting AI
    finding's ref for ``full`` / ``partial`` and ``None`` for ``none``.
    ``reason`` is ``None`` when the judge's own grade stands, otherwise why
    the code made it ``none``: :data:`REASON_MISSING` (the judge skipped the
    finding), :data:`REASON_UNKNOWN_REF` (no such ref, or none cited),
    :data:`REASON_OTHER_FILE` (the ref is in another file) or
    :data:`REASON_OVER_CAP` (the ref already credits
    :data:`MAX_CREDITS_PER_AI_FINDING` findings).
    """

    human_id: str
    grade: str
    ai_ref: str | None
    reason: str | None = None


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _line(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ident(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _label_rows(case: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for label in _get(case, "expected") or ():
        human_id = _ident(_get(label, "id"))
        if human_id is None:
            raise ValueError(f"human finding without an id: {label!r}")
        if human_id in seen:
            raise ValueError(f"duplicate human finding id {human_id!r}")
        seen.add(human_id)
        rows.append({
            "id": human_id,
            "file": _text(_get(label, "file")),
            "line": _line(_get(label, "line")),
            "severity": _text(_get(label, "severity")),
            "text": _text(_get(label, "text")),
        })
    return rows


def _ai_row(finding: Any) -> dict[str, Any]:
    return {
        "ref": _text(_get(finding, "ref")),
        "file": _text(_get(finding, "file")),
        "line": _line(_get(finding, "line")),
        "severity": _text(_get(finding, "severity")),
        "title": _text(_get(finding, "title")),
        "body": _text(_get(finding, "body")),
    }


def _judge_inputs(case: Any, ai_findings: Iterable[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    labels = _label_rows(case)
    files = {row["file"] for row in labels}
    visible = [row for row in map(_ai_row, ai_findings) if row["file"] in files]
    return labels, visible


def assign_refs(findings: Iterable[Any]) -> list[AIFinding]:
    """Number one run record's post-gate findings ``A1``, ``A2``, ... in input order.

    Takes the record's ``findings`` rows (mappings) or finding objects. A row
    whose ``drop_reason`` is set was gated out and is skipped without using a
    number, because eval scores post-gate output only. A grouped finding that
    covers several locations should arrive as one row per location, so each
    location is its own ref under the per-ref cap.
    """
    kept = [f for f in findings if not _get(f, "drop_reason")]
    return [
        AIFinding(
            ref=f"{AI_REF_PREFIX}{index}",
            file=_text(_get(f, "file")),
            line=_line(_get(f, "line")),
            severity=_text(_get(f, "severity")),
            title=_text(_get(f, "title")),
            body=_text(_get(f, "body")),
        )
        for index, f in enumerate(kept, start=1)
    ]


def human_files(case: Any) -> dict[str, str]:
    """Map each of the case's human finding ids to its file, in label order.

    This is the ``human_ids`` argument of :func:`parse_judge_response`.
    Raises ``ValueError`` for a label without an id or a repeated id.
    """
    return {row["id"]: row["file"] for row in _label_rows(case)}


def ai_ref_files(ai_findings: Iterable[Any]) -> dict[str, str]:
    """Map each AI finding's ref to its file; the ``ai_refs`` of :func:`parse_judge_response`."""
    return {row["ref"]: row["file"] for row in map(_ai_row, ai_findings) if row["ref"]}


def load_judge_template() -> str:
    """The packaged ``prompts/judge.md`` text, read through :func:`prxref.reviewer.load_prompt`."""
    return reviewer.load_prompt(JUDGE_PROMPT_NAME)


def judge_prompt_sha(template: str | None = None) -> str:
    """The sha256 hex digest of the judge template (the packaged one by default)."""
    text = load_judge_template() if template is None else template
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _render_rows(rows: Sequence[Mapping[str, Any]]) -> str:
    return json.dumps(list(rows), indent=2, ensure_ascii=False)


def build_judge_prompt(case: Any, ai_findings: Sequence[Any]) -> str:
    """Fill ``prompts/judge.md`` for one case.

    The ``{human_findings}`` slot gets the case's labels (``id``, ``file``,
    ``line``, ``severity``, ``text``) and the ``{ai_findings}`` slot gets the
    ref'd AI findings (``ref``, ``file``, ``line``, ``severity``, ``title``,
    ``body``) whose file holds at least one label, both as JSON arrays in
    input order. An AI finding in any other file could never be credited, so
    the judge does not see it. The template is filled in one pass by
    :func:`prxref.reviewer.fill_template`, so a finding that quotes a
    placeholder renders literally.
    """
    labels, visible = _judge_inputs(case, ai_findings)
    return reviewer.fill_template(load_judge_template(), {
        "human_findings": _render_rows(labels),
        "ai_findings": _render_rows(visible),
    })


def split_judge_prompt(prompt: str) -> tuple[str, str]:
    """Cut a filled judge prompt at :data:`JUDGE_SPLIT_MARKER` into ``(system, user)``.

    The system half is the fixed grading instructions; the user half starts
    at the marker and carries the case data and the output format. Raises
    ``ValueError`` when the marker is absent.
    """
    head, marker, tail = prompt.partition(JUDGE_SPLIT_MARKER)
    if not marker:
        raise ValueError(f"judge prompt is missing the {JUDGE_SPLIT_MARKER!r} split marker")
    return head.strip(), (marker + tail).strip()


def judge_cache_key(prompt_sha: str, model: str, case: Any, ai_findings: Sequence[Any]) -> str:
    """The sha256 hex digest that addresses one case's cached judge grading.

    Hashes canonical JSON (sorted keys, no whitespace) of the template
    sha256, the judge model, and the case's labels and AI findings projected
    exactly as :func:`build_judge_prompt` renders them. A mapping's key order
    never changes the key; a change the judge would see (a label's text, a
    finding's body, the finding order that decides its refs) always does,
    and a field the prompt omits (confidence, category, acceptance) never does.
    """
    labels, visible = _judge_inputs(case, ai_findings)
    payload = {
        "prompt_sha256": prompt_sha,
        "model": model,
        "labels": labels,
        "ai_findings": visible,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _read_rows(text: str) -> list[Any]:
    if not isinstance(text, str) or not text.strip():
        raise JudgeParseError("judge response is empty")
    try:
        data = loads_lenient(text)
    except json.JSONDecodeError as exc:
        raise JudgeParseError(f"judge response is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise JudgeParseError(f"judge response is a JSON {type(data).__name__}, not an object")
    rows = data.get("grades")
    if not isinstance(rows, list):
        raise JudgeParseError("judge response has no 'grades' list")
    return rows


def _answers(rows: list[Any], human_ids: Mapping[str, str]) -> dict[str, tuple[str, str | None]]:
    answers: dict[str, tuple[str, str | None]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise JudgeParseError(f"grades[{index}] is not an object")
        human_id = _ident(row.get("human_id"))
        if human_id is None:
            raise JudgeParseError(f"grades[{index}] has no human_id")
        grade = row.get("grade")
        grade = grade.strip().lower() if isinstance(grade, str) else grade
        if grade not in GRADES:
            raise JudgeParseError(f"grades[{index}] has grade {row.get('grade')!r}, not one of {', '.join(GRADES)}")
        if human_id not in human_ids:
            logger.warning("judge graded unknown human finding %r; dropped", human_id)
            continue
        if human_id in answers:
            logger.warning("judge graded human finding %r more than once; kept the first", human_id)
            continue
        answers[human_id] = (grade, _ident(row.get("ai_ref")))
    return answers


def _validated(human_id: str, file: str, answer: tuple[str, str | None] | None, ai_refs: Mapping[str, str]) -> Grade:
    if answer is None:
        logger.warning("judge returned no grade for human finding %r; graded none", human_id)
        return Grade(human_id, GRADE_NONE, None, REASON_MISSING)
    grade, ref = answer
    if grade == GRADE_NONE:
        return Grade(human_id, GRADE_NONE, None)
    if ref is None or ref not in ai_refs:
        logger.warning("judge credited human finding %r to unknown AI finding %r; graded none", human_id, ref)
        return Grade(human_id, GRADE_NONE, None, REASON_UNKNOWN_REF)
    if ai_refs[ref] != file:
        logger.warning(
            "judge credited human finding %r (%s) to AI finding %r in another file (%s); graded none",
            human_id, file, ref, ai_refs[ref],
        )
        return Grade(human_id, GRADE_NONE, None, REASON_OTHER_FILE)
    return Grade(human_id, grade, ref)


def _capped(grades: list[Grade]) -> list[Grade]:
    by_ref: dict[str, list[int]] = {}
    for index, grade in enumerate(grades):
        if grade.ai_ref is not None:
            by_ref.setdefault(grade.ai_ref, []).append(index)
    capped = list(grades)
    for ref, indexes in by_ref.items():
        if len(indexes) <= MAX_CREDITS_PER_AI_FINDING:
            continue
        ranked = sorted(indexes, key=lambda i: (_GRADE_RANK[grades[i].grade], i))
        kept = sorted(ranked[:MAX_CREDITS_PER_AI_FINDING])
        dropped = sorted(ranked[MAX_CREDITS_PER_AI_FINDING:])
        for i in dropped:
            capped[i] = Grade(grades[i].human_id, GRADE_NONE, None, REASON_OVER_CAP)
        logger.warning(
            "judge credited AI finding %r to %d human findings; kept %s, graded none %s (cap %d)",
            ref, len(indexes),
            ", ".join(grades[i].human_id for i in kept),
            ", ".join(grades[i].human_id for i in dropped),
            MAX_CREDITS_PER_AI_FINDING,
        )
    return capped


def parse_judge_response(text: str, human_ids: Mapping[str, str], ai_refs: Mapping[str, str]) -> list[Grade]:
    """Parse the judge's reply into one :class:`Grade` per human finding, in ``human_ids`` order.

    ``human_ids`` maps each human finding id to its file (:func:`human_files`)
    and ``ai_refs`` maps each AI ref to its file (:func:`ai_ref_files`). The
    reply is parsed with :func:`prxref.parser.loads_lenient`; ``grade`` is
    matched after trimming and lowercasing, and an integer id is read as its
    decimal string.

    - An id not in ``human_ids`` is dropped with a warning; a repeated id
      keeps its first row, with a warning.
    - An id the reply omits is graded ``none`` (:data:`REASON_MISSING`).
    - A ``full`` / ``partial`` grade whose ``ai_ref`` is null or not in
      ``ai_refs`` becomes ``none`` (:data:`REASON_UNKNOWN_REF`); one whose ref
      sits in a different file than the human finding becomes ``none``
      (:data:`REASON_OTHER_FILE`). A ``none`` grade never keeps a ref.
    - One ref credits at most :data:`MAX_CREDITS_PER_AI_FINDING` human
      findings. Deterministically, ``full`` outranks ``partial`` and then
      ``human_ids`` order decides; each excess grade becomes ``none``
      (:data:`REASON_OVER_CAP`) and the drop is logged as a warning.

    Raises :class:`JudgeParseError` when the reply is not the promised shape.
    """
    answers = _answers(_read_rows(text), human_ids)
    graded = [_validated(hid, file, answers.get(hid), ai_refs) for hid, file in human_ids.items()]
    return _capped(graded)
