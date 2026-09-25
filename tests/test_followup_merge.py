"""Merging a context follow-up re-run into a chunk's first review (#22)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from prxref.followup_merge import (
    UNCONFIRMED_PREFIX,
    MergeOutcome,
    confirms,
    merge_followup,
)
from prxref.quality import DEFAULT_LINE_TOLERANCE
from prxref.triage import Finding

FLOOR = 0.6


def _f(
    file: str = "acme/progress.py",
    line: int = 47,
    confidence: float | None = 0.5,
    title: str = "History window may drop the role",
    body: str = "Does `StateStore.save` serialize the run?",
    severity: str = "warning",
    drop_reason: str | None = None,
) -> Finding:
    return Finding(
        file=file,
        line=line,
        severity=severity,
        confidence=confidence,
        title=title,
        body=body,
        drop_reason=drop_reason,
    )


def _r(**kw) -> Finding:
    base = {
        "line": 200,
        "confidence": 0.9,
        "title": "Unrelated re-run title",
        "body": "Plain body with no symbol.",
        "severity": "error",
    }
    base.update(kw)
    return _f(**base)


def test_confirm_by_line_within_window():
    q = _f(line=47)
    assert confirms(q, _r(line=47 + DEFAULT_LINE_TOLERANCE), [], floor=FLOOR)
    assert confirms(q, _r(line=47 - DEFAULT_LINE_TOLERANCE), [], floor=FLOOR)
    assert not confirms(q, _r(line=47 + DEFAULT_LINE_TOLERANCE + 1), [], floor=FLOOR)


def test_line_window_is_configurable():
    q = _f(line=47)
    assert not confirms(q, _r(line=49), [], floor=FLOOR, line_window=1)
    assert confirms(q, _r(line=48), [], floor=FLOOR, line_window=1)


def test_line_match_needs_both_lines_positive():
    assert not confirms(_f(line=0), _r(line=3), [], floor=FLOOR)
    assert not confirms(_f(line=3), _r(line=0), [], floor=FLOOR)
    assert not confirms(_f(line=0), _r(line=0), [], floor=FLOOR)


def test_confirm_by_normalized_title():
    q = _f(line=0, title="`StateStore.save` drops the role")
    assert confirms(q, _r(title="statestore.save drops the role."), [], floor=FLOOR)


def test_empty_titles_do_not_match():
    q = _f(line=0, title="")
    assert not confirms(q, _r(title=""), [], floor=FLOOR)


def test_confirm_by_resolved_name_in_title_or_body():
    q = _f(line=0)
    assert confirms(q, _r(title="`StateStore.save` loses data"), ["StateStore"], floor=FLOOR)
    assert confirms(q, _r(body="calls json.dumps on the run"), ["dumps"], floor=FLOOR)
    assert confirms(q, _r(body="see acme.StateStore here"), ["StateStore"], floor=FLOOR)
    assert not confirms(q, _r(body="no name here"), ["StateStore"], floor=FLOOR)


def test_name_match_is_word_bounded():
    q = _f(line=0)
    assert not confirms(q, _r(body="MyStateStore is fine"), ["StateStore"], floor=FLOOR)
    assert not confirms(q, _r(body="StateStoreX is fine"), ["StateStore"], floor=FLOOR)
    assert not confirms(q, _r(body="$StateStore is fine"), ["StateStore"], floor=FLOOR)
    assert not confirms(q, _r(body="state_store_x"), ["state_store"], floor=FLOOR)


def test_name_with_dollar_is_escaped_and_bounded():
    q = _f(line=0)
    assert confirms(q, _r(body="uses $store now"), ["$store"], floor=FLOOR)
    assert not confirms(q, _r(body="uses $storeX now"), ["$store"], floor=FLOOR)
    assert not confirms(q, _r(body="uses Xstore now"), ["$store"], floor=FLOOR)


def test_empty_name_never_matches():
    q = _f(line=0)
    assert not confirms(q, _r(body="anything"), [""], floor=FLOOR)


def test_different_file_never_confirms():
    q = _f(line=47, title="Same title")
    r = _r(file="acme/state_store.py", line=47, title="Same title", body="`StateStore`")
    assert not confirms(q, r, ["StateStore"], floor=FLOOR)


def test_rerun_below_floor_never_confirms():
    q = _f(line=47)
    assert not confirms(q, _r(line=47, confidence=FLOOR - 0.001), [], floor=FLOOR)
    assert not confirms(q, _r(line=47, confidence=None), [], floor=FLOOR)


def test_rerun_exactly_at_floor_confirms():
    assert confirms(_f(line=47), _r(line=47, confidence=FLOOR), [], floor=FLOOR)


def test_merge_replaces_confirmed_question_in_place():
    above = _f(line=5, confidence=0.8, title="Kept")
    q = _f(line=47)
    r = _r(line=47)
    out = merge_followup([above, q], {1: ["StateStore"]}, [r], floor=FLOOR)
    assert out == MergeOutcome(findings=(above, r), confirmed=1, unconfirmed=0, discarded=0)
    assert out.findings[0] is above
    assert out.findings[1] is r


def test_unconfirmed_question_gets_exact_drop_reason():
    q = _f(line=47, confidence=0.5)
    out = merge_followup([q], {0: ["StateStore"]}, [], floor=FLOOR)
    reason = out.findings[0].drop_reason
    assert reason == "not confirmed by context follow-up (confidence 0.50 below floor 0.60)"
    assert reason.startswith(UNCONFIRMED_PREFIX)
    assert (out.confirmed, out.unconfirmed, out.discarded) == (0, 1, 0)
    assert q.drop_reason is None


def test_unconfirmed_missing_confidence_reads_as_zero():
    q = _f(confidence=None)
    out = merge_followup([q], {0: ["StateStore"]}, [], floor=FLOOR)
    assert out.findings[0].drop_reason == (
        "not confirmed by context follow-up (confidence 0.00 below floor 0.60)"
    )


def test_one_rerun_cannot_confirm_two_questions():
    q1 = _f(line=47)
    q2 = _f(line=48)
    r = _r(line=47)
    out = merge_followup([q1, q2], {0: ["StateStore"], 1: ["StateStore"]}, [r], floor=FLOOR)
    assert out.findings[0] is r
    assert out.findings[1].drop_reason.startswith(UNCONFIRMED_PREFIX)
    assert (out.confirmed, out.unconfirmed, out.discarded) == (1, 1, 0)


def test_highest_confidence_wins_and_lowest_index_breaks_ties():
    q = _f(line=47)
    low = _r(line=47, confidence=0.7, title="low")
    high = _r(line=47, confidence=0.95, title="high")
    tie = _r(line=47, confidence=0.95, title="tie")
    out = merge_followup([q], {0: ["StateStore"]}, [low, high, tie], floor=FLOOR)
    assert out.findings[0] is high
    assert (out.confirmed, out.discarded) == (1, 2)


def test_second_question_takes_the_next_best_rerun():
    q1 = _f(line=47)
    q2 = _f(line=50)
    a = _r(line=48, confidence=0.9, title="a")
    b = _r(line=49, confidence=0.8, title="b")
    out = merge_followup([q1, q2], {0: ["x_name"], 1: ["x_name"]}, [b, a], floor=FLOOR)
    assert out.findings == (a, b)
    assert (out.confirmed, out.unconfirmed, out.discarded) == (2, 0, 0)


def test_above_floor_and_nameless_questions_untouched():
    above = _f(line=47, confidence=FLOOR)
    nameless = _f(line=47, confidence=0.4)
    not_listed = _f(line=47, confidence=0.3)
    r = _r(line=47)
    out = merge_followup(
        [above, nameless, not_listed], {0: ["StateStore"], 1: []}, [r], floor=FLOOR
    )
    assert out.findings[0] is above
    assert out.findings[1] is nameless
    assert out.findings[2] is not_listed
    assert (out.confirmed, out.unconfirmed, out.discarded) == (0, 0, 1)


def test_already_dropped_question_untouched():
    q = _f(drop_reason="invalid severity: 'x'")
    out = merge_followup([q], {0: ["StateStore"]}, [_r(line=47)], floor=FLOOR)
    assert out.findings[0] is q
    assert (out.confirmed, out.unconfirmed, out.discarded) == (0, 0, 1)


def test_out_of_range_index_ignored():
    q = _f()
    out = merge_followup([q], {5: ["StateStore"], -1: ["StateStore"]}, [], floor=FLOOR)
    assert out.findings == (q,)
    assert (out.confirmed, out.unconfirmed, out.discarded) == (0, 0, 0)


def test_discarded_counts_every_unused_rerun_finding():
    q = _f(line=47)
    rerun = [
        _r(line=47),
        _r(file="acme/state_store.py", line=13),
        _r(line=300, title="another"),
        _r(line=47, confidence=0.2),
    ]
    out = merge_followup([q], {0: ["StateStore"]}, rerun, floor=FLOOR)
    assert out.confirmed == 1
    assert out.discarded == len(rerun) - out.confirmed == 3
    assert len(out.findings) == 1


def test_order_and_length_preserved():
    first = [
        _f(line=10, confidence=0.9, title="t0"),
        _f(line=47, title="t1"),
        _f(line=90, confidence=0.7, title="t2"),
        _f(line=120, title="t3"),
    ]
    r = _r(line=121)
    out = merge_followup(first, {3: ["StateStore"], 1: ["StateStore"]}, [r], floor=FLOOR)
    assert len(out.findings) == 4
    assert out.findings[0] is first[0]
    assert out.findings[1].title == "t1"
    assert out.findings[1].drop_reason.startswith(UNCONFIRMED_PREFIX)
    assert out.findings[2] is first[2]
    assert out.findings[3] is r


def test_questions_settle_in_index_order():
    q1 = _f(line=47)
    q2 = _f(line=48)
    r = _r(line=48)
    out = merge_followup([q1, q2], {1: ["StateStore"], 0: ["StateStore"]}, [r], floor=FLOOR)
    assert out.findings[0] is r
    assert out.findings[1].drop_reason.startswith(UNCONFIRMED_PREFIX)


def test_inputs_are_not_mutated():
    first = [_f(line=47)]
    rerun = [_r(line=900)]
    merge_followup(first, {0: ["StateStore"]}, rerun, floor=FLOOR)
    assert first[0].drop_reason is None
    assert rerun[0].drop_reason is None


def test_outcome_is_frozen():
    out = merge_followup([], {}, [], floor=FLOOR)
    assert out == MergeOutcome(findings=(), confirmed=0, unconfirmed=0, discarded=0)
    with pytest.raises(FrozenInstanceError):
        out.confirmed = 1
