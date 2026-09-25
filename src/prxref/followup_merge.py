"""Merge a chunk's context follow-up re-run into its first review (#22).

When ``PRXREF_CONTEXT_FOLLOWUP=on`` a chunk whose first reply holds
sub-floor questions about symbols it was not shown is re-sent once with
those symbols' definitions appended. This module decides, without any I/O,
what that re-run changes in the chunk's findings:

* A re-run finding **confirms** a question when it clears the confidence
  floor, sits in the same file, and either lands within
  :data:`prxref.quality.DEFAULT_LINE_TOLERANCE` lines of it, restates its
  title, or names one of the symbols the follow-up resolved for it.
* A confirmed question is replaced in place by its confirming finding,
  which then runs every quality pass like any other worker finding.
* A question with resolved names and no confirmation is marked with a
  ``drop_reason`` starting with :data:`UNCONFIRMED_PREFIX`.
* Every other re-run finding is discarded and only counted, so the
  follow-up never becomes a second full review of the chunk.
* First-pass findings at or above the floor, and questions for which no
  name was resolved, are returned untouched.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from .quality import DEFAULT_LINE_TOLERANCE, normalize_title
from .triage import Finding

UNCONFIRMED_PREFIX = "not confirmed by context follow-up"


@dataclass(frozen=True)
class MergeOutcome:
    """Result of :func:`merge_followup`.

    ``findings`` has the same length and order as the first review's
    findings. ``confirmed`` counts questions replaced by a re-run finding,
    ``unconfirmed`` counts questions marked with the
    :data:`UNCONFIRMED_PREFIX` drop reason, and ``discarded`` counts the
    re-run findings that confirmed nothing.
    """

    findings: tuple[Finding, ...]
    confirmed: int
    unconfirmed: int
    discarded: int


def _confidence(finding: Finding) -> float:
    return float(finding.confidence) if finding.confidence is not None else 0.0


def _names_in(text: str, names: Sequence[str]) -> bool:
    for name in names:
        if not name:
            continue
        if re.search(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", text):
            return True
    return False


def confirms(
    question: Finding,
    rerun: Finding,
    names: Sequence[str],
    *,
    floor: float,
    line_window: int = DEFAULT_LINE_TOLERANCE,
) -> bool:
    """Whether the re-run finding ``rerun`` confirms the sub-floor ``question``.

    ``rerun`` must reach ``floor`` (a missing confidence counts as 0.0, as
    in the quality gate) and name the same file as ``question``. It must
    then match on at least one of: a line within ``line_window`` of the
    question's line (both lines positive), an equal
    :func:`prxref.quality.normalize_title`, or a word-boundary mention of
    one of ``names`` (the symbols the follow-up resolved for the question)
    in its title or body.
    """
    if _confidence(rerun) < floor:
        return False
    if rerun.file != question.file:
        return False
    if question.line > 0 and rerun.line > 0 and abs(rerun.line - question.line) <= line_window:
        return True
    q_title = normalize_title(question.title or "")
    if q_title and normalize_title(rerun.title or "") == q_title:
        return True
    return _names_in(f"{rerun.title or ''}\n{rerun.body or ''}", names)


def merge_followup(
    first: Sequence[Finding],
    questions: Mapping[int, Sequence[str]],
    rerun: Sequence[Finding],
    *,
    floor: float,
) -> MergeOutcome:
    """Fold a follow-up re-run's findings into the first review's findings.

    ``questions`` maps an index into ``first`` to the names the follow-up
    resolved for that question. Questions are settled in index order; each
    takes the not-yet-used confirming re-run finding with the highest
    confidence, the lowest re-run index breaking ties, so one re-run
    finding confirms at most one question. An index outside ``first``, an
    empty name list, a finding at or above ``floor`` and a finding that
    already carries a ``drop_reason`` are all left untouched.
    """
    out = list(first)
    used: set[int] = set()
    confirmed = 0
    unconfirmed = 0
    for index in sorted(questions):
        names = tuple(questions[index])
        if not 0 <= index < len(first) or not names:
            continue
        question = first[index]
        conf = _confidence(question)
        if conf >= floor or question.drop_reason is not None:
            continue
        best: int | None = None
        for r_index, candidate in enumerate(rerun):
            if r_index in used or not confirms(question, candidate, names, floor=floor):
                continue
            if best is None or _confidence(candidate) > _confidence(rerun[best]):
                best = r_index
        if best is None:
            out[index] = replace(
                question,
                drop_reason=f"{UNCONFIRMED_PREFIX} (confidence {conf:.2f} below floor {floor:.2f})",
            )
            unconfirmed += 1
            continue
        used.add(best)
        out[index] = rerun[best]
        confirmed += 1
    return MergeOutcome(
        findings=tuple(out),
        confirmed=confirmed,
        unconfirmed=unconfirmed,
        discarded=len(rerun) - confirmed,
    )
