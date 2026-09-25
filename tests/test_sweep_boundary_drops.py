"""The chunk/sweep boundary survives the quality gate (0.15.0).

``orchestrate_review`` hands :func:`quality.apply_sweep_dedup` the gate's
output split back into chunk findings and sweep findings. The gate returns
content order, so the split is re-derived after it by
``orchestrator._split_at_sweep`` from ``orchestrator._origin_key``. The walk
it replaced counted the SWEEP side's keys and filed the first copies of each
key as sweep findings, but a chunk copy always precedes its sweep twin in the
gate's output, so every twin pair was swapped. That was harmless only while
the two copies stayed identical in every field, and three things break that:

- a pass before the gate drops the chunk copy and not the sweep copy:
  finding grouping (``grouped into``) or the per-rule cap (``rule cap
  exceeded``). The active sweep copy landed on the chunk side, was never
  compared with it, and was posted as a second comment;
- the model writes a severity the gate rewrites (``Warning``). The key
  changed across the gate, both copies landed on the chunk side, and the
  sweep copy was posted as a second comment;
- a severity cap in the gate keeps the chunk copy and drops the sweep copy.
  The kept chunk copy landed on the sweep side, where a chunk finding that
  shares its file and title dropped it as a "duplicate of chunk finding":
  a finding the cap had kept was never posted.

The controls pin what must not change: a sweep finding the gate drops stays
on the sweep side, a sweep finding with no chunk twin stays active, and a run
with none of the three triggers gives BASE's findings in BASE's order.
"""
from __future__ import annotations

import random
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from prxref import orchestrator
from prxref.orchestrator import _origin_key, orchestrate_review
from prxref.quality import (
    GROUPED_INTO_PREFIX,
    RULE_CAP_PREFIX,
    apply_quality_gate,
    finding_sort_key,
)
from tests.test_orchestrator import REF, FakeForge, _added_file_diff
from tests.test_orchestrator_grouping import APP, _f, _install_doubles, _reasons, _ScriptedLLM
from tests.test_orchestrator_rule_cap import A_PY, B_PY, C_PY, FIVE_RAW, FOUR_FILE_DIFF, _rules

ONE_FILE_DIFF = _added_file_diff(APP, 40)
DUPLICATE = "duplicate of chunk finding"


def _record_splits(monkeypatch) -> list[tuple[list, list]]:
    """Wrap the orchestrator's ``apply_sweep_dedup`` to record each (chunk, sweep) split it is given."""
    splits: list[tuple[list, list]] = []
    real = orchestrator.apply_sweep_dedup

    def _recording(findings, sweep_start, **kwargs):
        splits.append((list(findings[:sweep_start]), list(findings[sweep_start:])))
        return real(findings, sweep_start, **kwargs)

    monkeypatch.setattr(orchestrator, "apply_sweep_dedup", _recording)
    return splits


def _run(monkeypatch, chunk=(), sweep=(), **kw):
    """Review ONE_FILE_DIFF with review-unit doubles; returns the result, the forge and the one split."""
    _install_doubles(monkeypatch, chunk=chunk, sweep=sweep)
    splits = _record_splits(monkeypatch)
    forge = FakeForge(diff=ONE_FILE_DIFF)
    res = orchestrate_review(forge, REF, MagicMock(), post=True, max_workers=1, **kw)
    (split,) = splits
    return res, forge, split


def _scripted(monkeypatch, *, chunk, sweep, diff=ONE_FILE_DIFF, **kw):
    """Review ``diff`` with the REAL reviewer parsing scripted chunk and sweep answers."""
    splits = _record_splits(monkeypatch)
    forge = FakeForge(diff=diff)
    res = orchestrate_review(forge, REF, _ScriptedLLM(chunk=chunk, sweep=sweep), post=True, max_workers=1, **kw)
    (split,) = splits
    return res, forge, split


def _inline(forge) -> list[tuple[str, int]]:
    return sorted((c.path, c.line) for batch in forge.inline_batches for c in batch)


def _states(findings) -> list[tuple]:
    return [(f.line, f.confidence, f.drop_reason) for f in findings]


class TestAGroupedChunkFinding:
    """Grouping drops the chunk copy; its field-identical sweep copy must still meet it."""

    MEMBER = _f(5, title="Unchecked data write", rule="rule-a")
    CHUNK = (_f(3, title="Unchecked data read", rule="rule-a"), MEMBER)

    def test_the_sweep_copy_of_a_grouped_member_drops_as_a_duplicate(self, monkeypatch):
        res, forge, _split = _run(monkeypatch, chunk=self.CHUNK, sweep=[self.MEMBER], group_findings=True)
        assert [f.line for f in res["findings_active"]] == [3]
        assert _reasons(res) == [DUPLICATE, f"{GROUPED_INTO_PREFIX}{APP}:3"]
        assert _inline(forge) == [(APP, 3)]

    def test_the_grouped_member_stays_on_the_chunk_side(self, monkeypatch):
        _res, _forge, (chunk_part, sweep_part) = _run(
            monkeypatch, chunk=self.CHUNK, sweep=[self.MEMBER], group_findings=True,
        )
        assert _states(chunk_part) == [(3, 0.9, None), (5, 0.9, f"{GROUPED_INTO_PREFIX}{APP}:3")]
        assert _states(sweep_part) == [(5, 0.9, None)]

    def test_through_the_real_reviewer_one_comment_per_line(self, monkeypatch):
        raw = [
            {"file": APP, "line": line, "severity": "warning", "confidence": 0.9, "title": title,
             "body": f"The data on line {line} is unchecked.", "rule": "rule-a"}
            for line, title in ((3, "Unchecked data read"), (5, "Unchecked data write"))
        ]
        res, forge, _split = _scripted(monkeypatch, chunk=raw, sweep=raw[1:], group_findings=True)
        assert _inline(forge) == [(APP, 3)]
        assert _reasons(res) == [DUPLICATE, f"{GROUPED_INTO_PREFIX}{APP}:3"]


class TestBRuleCappedChunkFinding:
    """The per-rule cap folds the chunk copy; its field-identical sweep copy must still meet it."""

    FOLDED = _f(7, confidence=0.7, title="Unchecked data copy", rule="rule-a")
    CHUNK = (
        _f(3, confidence=0.9, title="Unchecked data read", rule="rule-a"),
        _f(5, confidence=0.8, title="Unchecked data write", rule="rule-a"),
        FOLDED,
    )
    CAP_REASON = f"{RULE_CAP_PREFIX}(max 2): listed at {APP}:3"

    def test_the_sweep_copy_of_a_folded_finding_drops_as_a_duplicate(self, monkeypatch):
        res, forge, _split = _run(monkeypatch, chunk=self.CHUNK, sweep=[self.FOLDED], rules=_rules())
        assert [f.line for f in res["findings_active"]] == [3, 5]
        assert _reasons(res) == [DUPLICATE, self.CAP_REASON]
        assert _inline(forge) == [(APP, 3), (APP, 5)]

    def test_the_folded_finding_stays_on_the_chunk_side(self, monkeypatch):
        _res, _forge, (chunk_part, sweep_part) = _run(
            monkeypatch, chunk=self.CHUNK, sweep=[self.FOLDED], rules=_rules(),
        )
        assert _states(chunk_part) == [(3, 0.9, None), (5, 0.8, None), (7, 0.7, self.CAP_REASON)]
        assert _states(sweep_part) == [(7, 0.7, None)]

    def test_through_the_real_reviewer_at_the_default_cap(self, monkeypatch):
        res, forge, _split = _scripted(
            monkeypatch, chunk=FIVE_RAW, sweep=[FIVE_RAW[2]], diff=FOUR_FILE_DIFF, rules=_rules(),
        )
        assert _inline(forge) == [(A_PY, 3), (B_PY, 4)]
        (dupe,) = [f for f in res["findings_dropped"] if f.drop_reason == DUPLICATE]
        assert (dupe.file, dupe.line, dupe.rule) == (C_PY, 5, "No-Bare-Except")


class TestCSeverityTheGateRewrites:
    """The gate lower-cases a severity; the key must be the same on both sides of it."""

    RAW = {"file": APP, "line": 5, "severity": "Warning", "confidence": 0.9,
           "title": "Unchecked data write", "body": "The data write is unchecked."}

    @pytest.mark.parametrize("severity", ["Warning", "WARNING", " warning "])
    def test_a_twin_the_model_wrote_in_another_case_posts_once(self, monkeypatch, severity):
        raw = dict(self.RAW, severity=severity)
        res, forge, (chunk_part, sweep_part) = _scripted(monkeypatch, chunk=[raw], sweep=[raw])
        assert _inline(forge) == [(APP, 5)]
        assert [(f.severity, f.drop_reason) for f in res["findings_dropped"]] == [("warning", DUPLICATE)]
        assert (len(chunk_part), len(sweep_part)) == (1, 1)

    def test_the_key_takes_the_severity_the_gate_gives(self):
        f = _f(5)
        assert _origin_key(replace(f, severity=" Warning ")) == _origin_key(f)
        assert _origin_key(replace(f, severity="error")) != _origin_key(f)
        assert _origin_key(replace(f, severity="Blocker")) == _origin_key(replace(f, severity="blocker"))


class TestESeverityCapSplitsTwins:
    """A gate cap keeps the chunk copy and drops the sweep copy; the kept one must stay a chunk finding."""

    @pytest.mark.parametrize("severity, knob", [
        ("error", {"max_errors": 2}),
        ("warning", {"max_warning_findings": 2}),
    ])
    def test_the_kept_chunk_copy_is_posted(self, monkeypatch, severity, knob):
        twin = _f(5, severity=severity, confidence=0.9, title="Unchecked data write")
        other = _f(9, severity=severity, confidence=0.95, title="Unchecked data write",
                   body="The data write on line 9 is unchecked.")
        res, forge, (chunk_part, sweep_part) = _run(monkeypatch, chunk=[twin, other], sweep=[twin], **knob)
        assert [f.line for f in res["findings_active"]] == [5, 9]
        assert _reasons(res) == [f"{severity} cap exceeded (max 2)"]
        assert _inline(forge) == [(APP, 5), (APP, 9)]
        assert _states(chunk_part) == [(5, 0.9, None), (9, 0.95, None)]
        assert _states(sweep_part) == [(5, 0.9, f"{severity} cap exceeded (max 2)")]


class TestControls:
    def test_a_sweep_finding_the_gate_drops_stays_on_the_sweep_side(self, monkeypatch):
        res, _forge, (chunk_part, sweep_part) = _run(
            monkeypatch,
            chunk=[_f(3, title="Unchecked data read")],
            sweep=[_f(5, confidence=0.3, title="Unchecked data write")],
        )
        floor = "confidence 0.30 below floor 0.60"
        assert _states(chunk_part) == [(3, 0.9, None)]
        assert _states(sweep_part) == [(5, 0.3, floor)]
        assert _reasons(res) == [floor]

    def test_a_gate_dropped_twin_never_enters_the_chunk_keys(self, monkeypatch):
        low = _f(5, confidence=0.3, title="Unchecked data write")
        sweep_only = _f(9, title="Unchecked data write", body="The sweep saw the data write on line 9.")
        res, _forge, (chunk_part, sweep_part) = _run(monkeypatch, chunk=[low], sweep=[low, sweep_only])
        floor = "confidence 0.30 below floor 0.60"
        assert _states(chunk_part) == [(5, 0.3, floor)]
        assert _states(sweep_part) == [(5, 0.3, floor), (9, 0.9, None)]
        assert [f.line for f in res["findings_active"]] == [9]

    def test_a_sweep_finding_with_no_chunk_twin_stays_active(self, monkeypatch):
        res, forge, (chunk_part, sweep_part) = _run(
            monkeypatch,
            chunk=[_f(3, title="Unchecked data read")],
            sweep=[_f(17, title="Unchecked data flush")],
            group_findings=True, rules=_rules(),
        )
        assert [f.line for f in res["findings_active"]] == [3, 17]
        assert res["findings_dropped"] == []
        assert _inline(forge) == [(APP, 3), (APP, 17)]
        assert (_states(chunk_part), _states(sweep_part)) == ([(3, 0.9, None)], [(17, 0.9, None)])


class TestNoTriggerMatchesBase:
    """No pre-gate drop, lower-case severities, no cap splitting a twin: BASE's output, field for field."""

    CHUNK = (
        _f(3, title="Unchecked data read"),
        _f(5, confidence=0.95, title="Unchecked data write"),
        _f(11, confidence=0.4, title="Unchecked data size"),
        _f(13, title="Unchecked data mode"),
        _f(20, severity="error", title="Leaked data handle"),
    )
    SWEEP = (
        _f(3, title="Unchecked data read"),
        _f(5, confidence=0.7, title="Unchecked data write"),
        _f(11, confidence=0.4, title="Unchecked data size"),
        _f(17, title="Unchecked data flush"),
        _f(9, title="Unchecked data read", body="The sweep saw the data read on line 9."),
    )
    FLOOR = "confidence 0.40 below floor 0.60"
    BASE_ACTIVE = [
        (3, "warning", 0.9, "Unchecked data read", "The data on line 3 is unchecked.", None),
        (5, "warning", 0.95, "Unchecked data write", "The data on line 5 is unchecked.", None),
        (13, "warning", 0.9, "Unchecked data mode", "The data on line 13 is unchecked.", None),
        (17, "warning", 0.9, "Unchecked data flush", "The data on line 17 is unchecked.", None),
        (20, "error", 0.9, "Leaked data handle", "The data on line 20 is unchecked.", None),
    ]
    BASE_DROPPED = [
        (3, "warning", 0.9, "Unchecked data read", "The data on line 3 is unchecked.", DUPLICATE),
        (5, "warning", 0.7, "Unchecked data write", "The data on line 5 is unchecked.", DUPLICATE),
        (9, "warning", 0.9, "Unchecked data read", "The sweep saw the data read on line 9.", DUPLICATE),
        (11, "warning", 0.4, "Unchecked data size", "The data on line 11 is unchecked.", FLOOR),
        (11, "warning", 0.4, "Unchecked data size", "The data on line 11 is unchecked.", FLOOR),
    ]

    def test_the_findings_and_their_order_are_base(self, monkeypatch):
        res, forge, _split = _run(monkeypatch, chunk=self.CHUNK, sweep=self.SWEEP)
        rows = {
            side: [(f.line, f.severity, f.confidence, f.title, f.body, f.drop_reason) for f in res[side]]
            for side in ("findings_active", "findings_dropped")
        }
        assert rows == {"findings_active": self.BASE_ACTIVE, "findings_dropped": self.BASE_DROPPED}
        assert [c.line for batch in forge.inline_batches for c in batch] == [20, 5, 3, 13, 17]


def _base_split(gated, before, sweep_start):
    """The walk this fix replaced, verbatim: the first copies of a SWEEP key go to the sweep side."""
    sweep_left: dict = {}
    for f in before[sweep_start:]:
        key = _origin_key(f)
        sweep_left[key] = sweep_left.get(key, 0) + 1
    chunk_part, sweep_part = [], []
    for f in gated:
        key = _origin_key(f)
        if sweep_left.get(key, 0) > 0:
            sweep_left[key] -= 1
            sweep_part.append(f)
        else:
            chunk_part.append(f)
    return chunk_part, sweep_part


PRE_GATE_REASONS = (
    f"{GROUPED_INTO_PREFIX}{APP}:3",
    f"{RULE_CAP_PREFIX}(max 2): listed at {APP}:3",
    'hedged: "might"',
)


def _random_split_case(rng: random.Random, *, triggers: bool):
    """A random pre-gate list full of cross-side twins, each finding tagged with its index.

    The tag rides in ``locations``, which no pass reads for ordering or identity
    (tests/test_orchestrator_grouping.py ``TestLocations``) and the gate keeps,
    so it names every gated finding's true side. ``triggers`` adds pre-gate
    drops, mixed-case severities and caps; without it the error cap is the
    list's length, since the default of 10 can split twins too.
    """
    severities = ("warning", "error", "Warning", "ERROR ") if triggers else ("warning", "error")
    pool = [
        _f(line, severity=rng.choice(severities), confidence=rng.choice((0.4, 0.8, 0.9)), title=title)
        for line in (3, 5) for title in ("Unchecked data read", "Unchecked data write")
    ]
    chunk = [rng.choice(pool) for _ in range(rng.randint(0, 6))]
    sweep = [rng.choice(pool) for _ in range(rng.randint(0, 6))]
    before = []
    for i, f in enumerate(chunk + sweep):
        f = replace(f, locations=(("tag", i),))
        if triggers and rng.random() < 0.3:
            f = replace(f, drop_reason=rng.choice(PRE_GATE_REASONS))
        before.append(f)
    caps = {"max_errors": len(before)}
    if triggers:
        caps = {"max_errors": rng.choice((0, 1, 2, 10)),
                "max_warning_findings": rng.choice((None, 0, 1, 2))}
    gated = apply_quality_gate(before, confidence_floor=0.6, **caps)
    return before, len(chunk), gated


def _untagged(findings):
    return [replace(f, locations=()) for f in findings]


def _split_at_sweep(gated, before, sweep_start):
    return orchestrator._split_at_sweep(gated, before, sweep_start)


SEEDS = range(500)


class TestTheSplitHelper:
    def test_every_finding_lands_on_its_own_side(self):
        for seed in SEEDS:
            before, sweep_start, gated = _random_split_case(random.Random(seed), triggers=True)
            chunk_part, sweep_part = _split_at_sweep(gated, before, sweep_start)
            truth = (
                [f for f in gated if f.locations[0][1] < sweep_start],
                [f for f in gated if f.locations[0][1] >= sweep_start],
            )
            assert (chunk_part, sweep_part) == truth, f"seed {seed}"

    def test_without_a_trigger_the_sides_hold_what_base_filed(self):
        for seed in SEEDS:
            before, sweep_start, gated = _random_split_case(random.Random(seed), triggers=False)
            new = _split_at_sweep(gated, before, sweep_start)
            base = _base_split(gated, before, sweep_start)
            assert [_untagged(part) for part in new] == [_untagged(part) for part in base], f"seed {seed}"

    def test_a_key_neither_side_had_is_filed_as_a_chunk_finding(self):
        before = [_f(3), _f(5)]
        stranger = _f(7)
        assert _split_at_sweep([*before, stranger], before, 1) == ([before[0], stranger], [before[1]])

    def test_the_base_walk_swaps_a_twin_the_new_one_does_not(self):
        chunk_copy = replace(_f(5), drop_reason=f"{GROUPED_INTO_PREFIX}{APP}:3", locations=(("tag", 0),))
        sweep_copy = replace(_f(5), locations=(("tag", 1),))
        before = [chunk_copy, sweep_copy]
        gated = apply_quality_gate(before, confidence_floor=0.6, max_errors=10)
        assert _split_at_sweep(gated, before, 1) == ([chunk_copy], [sweep_copy])
        assert _base_split(gated, before, 1) == ([sweep_copy], [chunk_copy])


class TestTheGateKeepsTiedFindingsInInputOrder:
    """The contract ``_split_at_sweep`` leans on: findings tied on ``finding_sort_key`` keep their input order."""

    def test_ties_stay_in_input_order_whatever_the_gate_drops(self):
        for seed in SEEDS:
            before, _sweep_start, gated = _random_split_case(random.Random(seed), triggers=True)
            assert len(gated) == len(before), f"seed {seed}"
            ties: dict = {}
            for f in gated:
                ties.setdefault(finding_sort_key(f), []).append(f.locations[0][1])
            assert all(tags == sorted(tags) for tags in ties.values()), f"seed {seed}"
