"""Issue #110: a chunk and a sweep finding on one file, line and rule id.

Run through ``apply_sweep_dedup`` with the review pipeline's defaults
(``similarity=None``, ``code_overlap=True``: ``orchestrator`` passes
``dedup_similarity=None`` and ``dedup_code_overlap=True``). The sweep copy
is dropped when it restates the chunk finding's rule on the same anchored
line at no higher severity; every control below must stay two findings.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from prxref.quality import apply_sweep_dedup
from prxref.triage import Finding

PATH = "lib/review-config.js"
RULE = "BASE-ERR-01"

CHUNK = Finding(
    file=PATH,
    line=869,
    severity="warning",
    confidence=0.9,
    title="Do not fall back to the ignored legacy config on a bad config file",
    body=(
        "When loadConfig() throws on a malformed review config, the catch block "
        "returns the legacy settings object, so the bad file is silently ignored."
    ),
    rule=RULE,
)
SWEEP = Finding(
    file=PATH,
    line=869,
    severity="warning",
    confidence=0.8,
    title="Do not silently fall back to legacy config when the config file fails to parse",
    body="A parse failure should surface to the caller instead of using old defaults.",
    rule=RULE,
)
DISJOINT = replace(
    SWEEP,
    title="Cache eviction ordering is nondeterministic",
    body="Entries expire in insertion order rather than by age, which surprises users.",
)


def _run(chunk: Finding, sweep: Finding) -> list[Finding]:
    out = apply_sweep_dedup([chunk, sweep], 1, similarity=None, code_overlap=True)
    assert len(out) == 2
    return out


def _active(out: list[Finding]) -> int:
    return sum(1 for f in out if f.drop_reason is None)


def test_issue_table_pair_collapses_to_one():
    out = _run(CHUNK, SWEEP)
    assert out[0].drop_reason is None
    assert out[1].drop_reason is not None
    assert out[1].drop_reason.startswith("duplicate of chunk finding")


def test_disjoint_titles_and_bodies_same_rule_line_collapse_to_one():
    out = _run(CHUNK, DISJOINT)
    assert _active(out) == 1
    assert out[0].drop_reason is None
    assert out[1].drop_reason.startswith("duplicate of chunk finding")


def test_lower_severity_sweep_copy_is_dropped():
    out = _run(CHUNK, replace(DISJOINT, severity="outofscope"))
    assert _active(out) == 1


@pytest.mark.parametrize(
    "name,sweep",
    [
        ("different_rule", replace(DISJOINT, rule="BASE-ERR-02")),
        ("different_line", replace(DISJOINT, line=870)),
        ("sweep_more_severe", replace(DISJOINT, severity="error")),
        ("sweep_rule_null", replace(DISJOINT, rule=None)),
        ("line_zero", replace(DISJOINT, line=0)),
    ],
)
def test_controls_stay_two(name, sweep):
    assert _active(_run(CHUNK, sweep)) == 2, name


def test_chunk_rule_null_stays_two():
    assert _active(_run(replace(CHUNK, rule=None), DISJOINT)) == 2


def test_both_rules_null_stays_two():
    assert _active(_run(replace(CHUNK, rule=None), replace(DISJOINT, rule=None))) == 2


def test_line_zero_on_both_sides_stays_two():
    out = _run(replace(CHUNK, line=0), replace(DISJOINT, line=0))
    assert _active(out) == 2


def test_different_file_stays_two():
    assert _active(_run(CHUNK, replace(DISJOINT, file="lib/other.js"))) == 2
