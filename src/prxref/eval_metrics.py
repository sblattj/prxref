"""Deterministic grading and metrics for ``prxref eval score`` (issue #14).

Two pure layers. Neither does I/O and neither calls an LLM:

- :func:`match_case` grades every human finding that carries a
  ``must_match`` predicate with the deterministic rule of
  ``docs/spec-grounded-review.md`` section 7.2: an AI finding in the same
  ``file``, within ``quality.DEFAULT_LINE_TOLERANCE`` lines, whose title plus
  body satisfies the predicate (:func:`must_match_predicate`). A human finding
  with no ``must_match`` is not graded here; it goes to the judge.
- :func:`score_cases` turns graded cases into the JSON-safe metrics and the
  per-case rows of ``score.json``.

A grade is ``full`` (credit 1), ``partial`` (credit :data:`PARTIAL_CREDIT`) or
``none`` (credit 0). ``judge_error`` marks a human finding the judge failed
to grade: it is counted on its own and left out of every denominator, never
scored as 0. A case whose review itself failed is different: the author
received nothing, so its human findings are honestly ``none``.

One AI finding credits at most :data:`MAX_CREDITS_PER_FINDING` human
findings. A grouped finding (issue #13) that lists extra locations under
:data:`LOCATIONS_FIELD` is credited per location: each location is its own
credit unit with its own cap, exactly as the member findings it replaced
would have been, so a group is never capped as a whole.

Human findings are duck-typed (:class:`ExpectedLike`, as mappings or
attribute objects). AI findings are the run record's ``findings`` rows, the
CLI's JSON finding shape (``file``, ``line``, ``severity``, ``title``,
``body``, ``drop_reason``), as mappings or attribute objects. Only rows whose
``drop_reason`` is null are ever credited or counted: the eval scores what a
PR author receives, after every quality gate.
"""
from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple, Protocol

from .costs import valid_usd
from .quality import DEFAULT_LINE_TOLERANCE, _tokens, normalize_title

FULL = "full"
PARTIAL = "partial"
NONE = "none"
JUDGE_ERROR = "judge_error"
GRADES: tuple[str, ...] = (FULL, PARTIAL, NONE)
PARTIAL_CREDIT = 0.5
GRADE_CREDIT: dict[str, float] = {FULL: 1.0, PARTIAL: PARTIAL_CREDIT, NONE: 0.0}
MAX_CREDITS_PER_FINDING = 2
LOCATIONS_FIELD = "locations"
REGEX_PREFIX = "re:"
MATCH_METHOD = "must_match"
MISSING_LABEL = "(none)"

_CREDITED = frozenset({FULL, PARTIAL})
_ALL_GRADES = (*GRADES, JUDGE_ERROR)


class ExpectedLike(Protocol):
    """The fields of one human finding (``eval_cases.ExpectedFinding``) this module reads."""

    id: str
    file: str
    line: int
    severity: str
    category: str | None
    accepted: bool | None
    text: str | None
    must_match: str | None


@dataclass(frozen=True)
class GradedCase:
    """One case ready for :func:`score_cases`.

    ``expected`` holds the case's human findings. ``ai_findings`` is the run
    record's ``findings`` list, the sequence every grade's ``ai_ref`` indexes.
    ``grades`` holds exactly one grade mapping per human finding, from
    :func:`match_case` or the judge: ``human_id``, ``grade`` (one of
    ``full``, ``partial``, ``none``, ``judge_error``), and for a credited
    grade an ``ai_ref`` naming a row whose ``drop_reason`` is null; ``ai_line``
    and ``method`` are optional. ``record`` is the case's run record
    (``verdict``, ``chunks_failed``, ``elapsed_ms``, ``cost_usd``,
    ``cost_estimated``), or ``None`` when the run left none.
    ``judge_cost_usd`` is ``0.0`` when no judge call was made, and ``None``
    when a call was made that nothing could price.
    """

    case_id: str
    expected: Sequence[Any]
    ai_findings: Sequence[Any]
    grades: Sequence[Mapping[str, Any]]
    record: Mapping[str, Any] | None = None
    judge_cost_usd: float | None = 0.0
    judge_cost_estimated: bool = False


class _Unit(NamedTuple):
    ref: int
    file: str
    line: int | None
    order: int


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _count(value: Any) -> int | None:
    number = _int(value)
    return number if number is not None and number >= 0 else None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _label(value: Any) -> str:
    return value if isinstance(value, str) and value.strip() else MISSING_LABEL


def must_match_predicate(pattern: str) -> Callable[[str], bool]:
    """Compile one ``must_match`` value into a test over a finding's text.

    A value prefixed ``re:`` is a regular expression searched anywhere in the
    text, case-insensitively. Any other value is a plain substring, compared
    after both sides pass through ``quality.normalize_title``, so case,
    backticks, quotes, emphasis marks and whitespace runs never decide a
    match. A plain value that normalizes to nothing is compared lowercased as
    written. An invalid regular expression raises ``ValueError``.
    """
    if pattern.startswith(REGEX_PREFIX):
        try:
            compiled = re.compile(pattern[len(REGEX_PREFIX):], re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"must_match {pattern!r} is not a valid regular expression: {exc}") from exc
        return lambda text: compiled.search(text) is not None
    needle = normalize_title(pattern)
    if not needle:
        lowered = pattern.lower()
        return lambda text: lowered in text.lower()
    return lambda text: needle in normalize_title(text)


def _location(entry: Any, default_file: str) -> tuple[str, int] | None:
    if isinstance(entry, Mapping):
        file, line = entry.get("file"), entry.get("line")
    elif isinstance(entry, tuple | list) and len(entry) == 2:
        file, line = entry
    elif hasattr(entry, "line"):
        file, line = getattr(entry, "file", None), entry.line
    else:
        return None
    number = _int(line)
    if number is None:
        return None
    return (file if isinstance(file, str) and file else default_file, number)


def credit_locations(finding: Any) -> list[tuple[str, int | None]]:
    """Every ``(file, line)`` one AI finding is credited at, deduplicated in order.

    The finding's own ``file`` and ``line`` come first. A grouped finding
    adds the entries of its optional :data:`LOCATIONS_FIELD` attribute or
    key, each a ``{"file", "line"}`` mapping, a ``(file, line)`` pair, or an
    object with those attributes; an entry without a file inherits the
    finding's, and an entry without an integer line is skipped.
    """
    file = _str(_get(finding, "file"))
    locations: list[tuple[str, int | None]] = [(file, _int(_get(finding, "line")))]
    extra = _get(finding, LOCATIONS_FIELD)
    if isinstance(extra, Iterable) and not isinstance(extra, str | bytes | Mapping):
        for entry in extra:
            parsed = _location(entry, file)
            if parsed is not None and parsed not in locations:
                locations.append(parsed)
    return locations


def _units(ai_findings: Sequence[Any]) -> list[_Unit]:
    units: list[_Unit] = []
    for ref, finding in enumerate(ai_findings):
        if _get(finding, "drop_reason") is not None:
            continue
        for order, (file, line) in enumerate(credit_locations(finding)):
            units.append(_Unit(ref, file, line, order))
    return units


def _distance(human_line: int | None, ai_line: int | None, tolerance: int) -> int | None:
    if not human_line:
        return 0
    if ai_line is None:
        return None
    gap = abs(ai_line - human_line)
    return gap if gap <= tolerance else None


def _finding_text(finding: Any) -> str:
    return f"{_str(_get(finding, 'title'))}\n{_str(_get(finding, 'body'))}"


def _human_text(human: Any) -> str:
    pattern = _str(_get(human, "must_match")).removeprefix(REGEX_PREFIX)
    return f"{_str(_get(human, 'text'))}\n{pattern}"


def _human_key(human: Any) -> tuple[str, int, str]:
    return (_str(_get(human, "file")), _int(_get(human, "line")) or 0, str(_get(human, "id")))


def _assign(order: Sequence[int], prefs: Mapping[int, Sequence[int]], unit_count: int) -> dict[int, int]:
    slots: list[list[int]] = [[] for _ in range(unit_count)]

    def augment(human: int, seen: set[int]) -> bool:
        for unit in prefs[human]:
            if unit in seen:
                continue
            seen.add(unit)
            if len(slots[unit]) < MAX_CREDITS_PER_FINDING:
                slots[unit].append(human)
                return True
            for holder in list(slots[unit]):
                if augment(holder, seen):
                    slots[unit].remove(holder)
                    slots[unit].append(human)
                    return True
        return False

    for human in order:
        augment(human, set())
    return {human: unit for unit, holders in enumerate(slots) for human in holders}


def match_case(
    expected: Iterable[Any], ai_findings: Sequence[Any], *, tolerance: int = DEFAULT_LINE_TOLERANCE
) -> list[dict[str, Any]]:
    """Grade every human finding that has a ``must_match``, deterministically.

    A human finding is credited ``full`` by an AI finding whose
    ``drop_reason`` is null, in the same ``file`` (exact), within
    ``tolerance`` lines (``quality.DEFAULT_LINE_TOLERANCE``, 5), and whose
    title plus body passes :func:`must_match_predicate`. A human ``line`` of
    ``0`` is file-level and matches any line of the file. Anything else is
    ``none``: this tier never gives partial credit.

    Credit is assigned as a maximum matching under the cap: each credit unit
    (an AI finding, or one location of a grouped finding, see
    :func:`credit_locations`) credits at most :data:`MAX_CREDITS_PER_FINDING`
    human findings, and a human finding that another could only share with
    it is re-routed rather than left uncredited. Among a human finding's
    candidates the nearest line wins, then the most content tokens shared
    with its ``text`` and ``must_match`` (``quality._tokens``), then content
    order, so the grades never depend on input order.

    Returns one grade per human finding with a ``must_match``, in input
    order: ``{"human_id", "grade", "ai_ref", "ai_line", "method"}``, where
    ``ai_ref`` indexes ``ai_findings`` and ``ai_line`` is the credited
    location's line (both ``None`` for ``none``) and ``method`` is
    :data:`MATCH_METHOD`. An invalid ``re:`` pattern raises ``ValueError``.
    """
    humans = [human for human in expected if _str(_get(human, "must_match"))]
    order = sorted(range(len(humans)), key=lambda index: _human_key(humans[index]))
    units = _units(ai_findings)
    texts = {unit.ref: _finding_text(ai_findings[unit.ref]) for unit in units}
    tokens = {ref: _tokens(text, split_compounds=True) for ref, text in texts.items()}
    prefs: dict[int, list[int]] = {}
    for index in order:
        human = humans[index]
        test = must_match_predicate(_str(_get(human, "must_match")))
        wanted = _tokens(_human_text(human), split_compounds=True)
        file = _str(_get(human, "file"))
        line = _int(_get(human, "line"))
        hits: dict[int, bool] = {}
        ranked = []
        for position, unit in enumerate(units):
            if unit.file != file:
                continue
            gap = _distance(line, unit.line, tolerance)
            if gap is None:
                continue
            if unit.ref not in hits:
                hits[unit.ref] = test(texts[unit.ref])
            if not hits[unit.ref]:
                continue
            finding = ai_findings[unit.ref]
            content = (
                unit.line if unit.line is not None else -1,
                _str(_get(finding, "title")),
                _str(_get(finding, "body")),
                unit.ref,
                unit.order,
            )
            ranked.append((gap, -len(wanted & tokens[unit.ref]), content, position))
        prefs[index] = [entry[-1] for entry in sorted(ranked)]
    assigned = _assign(order, prefs, len(units))
    grades: list[dict[str, Any]] = []
    for index, human in enumerate(humans):
        unit = units[assigned[index]] if index in assigned else None
        grades.append({
            "human_id": _get(human, "id"),
            "grade": FULL if unit is not None else NONE,
            "ai_ref": unit.ref if unit is not None else None,
            "ai_line": unit.line if unit is not None else None,
            "method": MATCH_METHOD,
        })
    return grades


def agreement_severity(severity: object) -> str:
    """The severity compared for agreement: lowercased, with ``minor`` read as ``warning``.

    Human labels use ``error|warning|minor`` and keep that value everywhere
    else (``recall_by_severity`` is keyed by it); only the agreement check
    reads a human ``minor`` as prxref's ``warning``.
    """
    value = severity.strip().lower() if isinstance(severity, str) else ""
    return "warning" if value == "minor" else value


def _check_ref(case: GradedCase, human_id: Any, ref: Any) -> None:
    number = _int(ref)
    if number is None or not 0 <= number < len(case.ai_findings):
        raise ValueError(
            f"case {case.case_id!r}: grade for {human_id!r} credits ai_ref {ref!r}, "
            f"not an index into its {len(case.ai_findings)} AI findings"
        )
    if _get(case.ai_findings[number], "drop_reason") is not None:
        raise ValueError(f"case {case.case_id!r}: grade for {human_id!r} credits dropped AI finding {number}")


def _grades_by_human(case: GradedCase) -> dict[Any, Mapping[str, Any]]:
    ids = [_get(human, "id") for human in case.expected]
    if len(set(ids)) != len(ids):
        raise ValueError(f"case {case.case_id!r}: expected finding ids are not unique")
    by_id: dict[Any, Mapping[str, Any]] = {}
    for grade in case.grades:
        human_id = grade.get("human_id")
        if human_id not in ids:
            raise ValueError(f"case {case.case_id!r}: grade for unknown expected finding {human_id!r}")
        if human_id in by_id:
            raise ValueError(f"case {case.case_id!r}: more than one grade for {human_id!r}")
        value = grade.get("grade")
        if value not in _ALL_GRADES:
            raise ValueError(
                f"case {case.case_id!r}: grade {value!r} for {human_id!r} is not one of {', '.join(_ALL_GRADES)}"
            )
        if value in _CREDITED:
            _check_ref(case, human_id, grade.get("ai_ref"))
        by_id[human_id] = grade
    missing = sorted(str(human_id) for human_id in ids if human_id not in by_id)
    if missing:
        raise ValueError(f"case {case.case_id!r}: no grade for expected finding(s) {', '.join(missing)}")
    return by_id


def _recall_block(entries: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    counts = dict.fromkeys(_ALL_GRADES, 0)
    credit = 0.0
    for entry in entries:
        counts[entry["grade"]] += 1
        if entry["credit"] is not None:
            credit += entry["credit"]
    scored = counts[FULL] + counts[PARTIAL] + counts[NONE]
    return {"recall": credit / scored if scored else None, "credit": credit, "scored": scored, **counts}


def case_row(case: GradedCase) -> dict[str, Any]:
    """One JSON-safe per-case row of ``score.json``.

    Keys: ``case_id``, ``verdict``, the recall fields (``recall``, ``credit``,
    ``scored``, ``full``, ``partial``, ``none``, ``judge_error``),
    ``ai_findings`` (rows whose ``drop_reason`` is null), ``unmatched_ai``
    (those no credited grade names; a grouped finding counts once),
    ``severity_compared``, ``severity_agreed``, ``chunks_failed``,
    ``elapsed_ms``, ``review_cost_usd``, ``review_cost_estimated``,
    ``judge_cost_usd``, ``judge_cost_estimated``, and ``findings``: one entry
    per human finding, sorted by id, ``{"human_id", "file", "line",
    "severity", "category", "accepted", "grade", "credit", "ai_ref",
    "ai_line", "ai_severity", "severity_agrees", "method"}``. ``credit`` is
    ``None`` for ``judge_error``; the ``ai_*`` fields and
    ``severity_agrees`` are ``None`` unless the grade credits.

    Raises ``ValueError`` when the grades do not cover every human finding
    exactly once, name an unknown grade, or credit an ``ai_ref`` that is out
    of range or dropped.
    """
    grades = _grades_by_human(case)
    record = case.record if isinstance(case.record, Mapping) else {}
    findings: list[dict[str, Any]] = []
    credited_refs: set[int] = set()
    for human in sorted(case.expected, key=lambda item: str(_get(item, "id"))):
        human_id = _get(human, "id")
        grade = grades[human_id]
        value = grade["grade"]
        ref = _int(grade.get("ai_ref")) if value in _CREDITED else None
        ai_severity = None
        agrees = None
        if ref is not None:
            credited_refs.add(ref)
            ai_severity = _optional_str(_get(case.ai_findings[ref], "severity"))
            agrees = agreement_severity(ai_severity) == agreement_severity(_get(human, "severity"))
        accepted = _get(human, "accepted")
        findings.append({
            "human_id": human_id,
            "file": _optional_str(_get(human, "file")),
            "line": _int(_get(human, "line")),
            "severity": _optional_str(_get(human, "severity")),
            "category": _optional_str(_get(human, "category")),
            "accepted": accepted if isinstance(accepted, bool) else None,
            "grade": value,
            "credit": GRADE_CREDIT.get(value),
            "ai_ref": ref,
            "ai_line": _int(grade.get("ai_line")) if ref is not None else None,
            "ai_severity": ai_severity,
            "severity_agrees": agrees,
            "method": _optional_str(grade.get("method")),
        })
    active = [ref for ref, finding in enumerate(case.ai_findings) if _get(finding, "drop_reason") is None]
    compared = [entry["severity_agrees"] for entry in findings if entry["severity_agrees"] is not None]
    return {
        "case_id": str(case.case_id),
        "verdict": _optional_str(record.get("verdict")),
        **_recall_block(findings),
        "ai_findings": len(active),
        "unmatched_ai": sum(1 for ref in active if ref not in credited_refs),
        "severity_compared": len(compared),
        "severity_agreed": sum(1 for agrees in compared if agrees),
        "chunks_failed": _count(record.get("chunks_failed")),
        "elapsed_ms": _count(record.get("elapsed_ms")),
        "review_cost_usd": valid_usd(record.get("cost_usd")) if case.record is not None else None,
        "review_cost_estimated": record.get("cost_estimated") is True,
        "judge_cost_usd": valid_usd(case.judge_cost_usd),
        "judge_cost_estimated": case.judge_cost_estimated is True,
        "findings": findings,
    }


def _grouped(entries: Sequence[Mapping[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    labels = sorted({_label(entry[key]) for entry in entries})
    return {label: _recall_block(e for e in entries if _label(e[key]) == label) for label in labels}


def _agreement_block(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    confusion: dict[str, dict[str, int]] = {}
    compared = agreed = 0
    for entry in entries:
        if entry["severity_agrees"] is None:
            continue
        compared += 1
        if entry["severity_agrees"]:
            agreed += 1
        human = agreement_severity(entry["severity"]) or MISSING_LABEL
        ai = agreement_severity(entry["ai_severity"]) or MISSING_LABEL
        row = confusion.setdefault(human, {})
        row[ai] = row.get(ai, 0) + 1
    return {
        "compared": compared,
        "agreed": agreed,
        "rate": agreed / compared if compared else None,
        "confusion": {human: dict(sorted(confusion[human].items())) for human in sorted(confusion)},
    }


def _total_block(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, int]:
    values = [row[key] for row in rows]
    return {
        "total": sum(value for value in values if value is not None),
        "missing": sum(1 for value in values if value is None),
    }


def _cost_block(rows: Sequence[Mapping[str, Any]], key: str, estimated_key: str) -> dict[str, Any]:
    priced = [row[key] for row in rows if row[key] is not None]
    total = math.fsum(priced) if rows and len(priced) == len(rows) else None
    return {
        "total_usd": total,
        "per_pr_usd": total / len(rows) if total is not None else None,
        "priced": len(priced),
        "unpriced": len(rows) - len(priced),
        "estimated": sum(1 for row in rows if row[key] is not None and row[estimated_key]),
    }


def score_cases(cases: Iterable[GradedCase]) -> dict[str, Any]:
    """Score graded cases into ``{"metrics": {...}, "cases": [row, ...]}``, JSON-safe.

    ``cases`` holds one :func:`case_row` per case, sorted by ``case_id``.
    ``metrics`` holds:

    - ``case_count``.
    - ``recall``: the headline, micro over every human finding:
      ``{"recall", "credit", "scored", "full", "partial", "none",
      "judge_error"}``. ``recall`` is ``credit / scored`` and ``None`` when
      nothing was scored; ``judge_error`` findings are outside ``scored``.
    - ``recall_by_severity`` and ``recall_by_category``: the same block per
      human label as given, keys sorted, a missing label under
      :data:`MISSING_LABEL`; the blocks of each partition sum to the
      headline.
    - ``recall_accepted``: the same block over human findings whose
      ``accepted`` is ``True``.
    - ``unmatched_ai``: ``{"total", "ai_findings", "per_pr"}``, where
      ``per_pr`` is ``total / case_count``.
    - ``severity_agreement``: ``{"compared", "agreed", "rate",
      "confusion"}`` over credited grades, compared through
      :func:`agreement_severity`; ``confusion`` maps human severity to AI
      severity to a count.
    - ``chunks_failed`` and ``elapsed_ms``: ``{"total", "missing"}``; a case
      with no usable value is counted as missing, not as 0.
    - ``review_cost`` and ``judge_cost``: ``{"total_usd", "per_pr_usd",
      "priced", "unpriced", "estimated"}``. A ``None`` cost is never summed:
      one unpriced case makes ``total_usd`` and ``per_pr_usd`` ``None``, as a
      run's own cost is (``costs`` module), and ``unpriced`` says how many.

    Raises ``ValueError`` on a duplicate ``case_id`` or on grades
    :func:`case_row` rejects.
    """
    rows = sorted((case_row(case) for case in cases), key=lambda row: row["case_id"])
    ids = [row["case_id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("case ids are not unique")
    entries = [entry for row in rows for entry in row["findings"]]
    unmatched = sum(row["unmatched_ai"] for row in rows)
    metrics = {
        "case_count": len(rows),
        "recall": _recall_block(entries),
        "recall_by_severity": _grouped(entries, "severity"),
        "recall_by_category": _grouped(entries, "category"),
        "recall_accepted": _recall_block(entry for entry in entries if entry["accepted"] is True),
        "unmatched_ai": {
            "total": unmatched,
            "ai_findings": sum(row["ai_findings"] for row in rows),
            "per_pr": unmatched / len(rows) if rows else None,
        },
        "severity_agreement": _agreement_block(entries),
        "chunks_failed": _total_block(rows, "chunks_failed"),
        "elapsed_ms": _total_block(rows, "elapsed_ms"),
        "review_cost": _cost_block(rows, "review_cost_usd", "review_cost_estimated"),
        "judge_cost": _cost_block(rows, "judge_cost_usd", "judge_cost_estimated"),
    }
    return {"metrics": metrics, "cases": rows}
