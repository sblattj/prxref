"""Issue #148: one file over the token budget became one over-budget chunk.

``plan_chunks`` placed whole files, so a single file whose diff exceeded
``PRXREF_CHUNK_TOKEN_BUDGET`` opened a chunk of its own that was sent whole
and, when the provider refused it, took the review to Incomplete. Now such a
file is split by hunk into chunks that fit; a single hunk that is still over
budget goes alone and still counts in ``chunks_over_budget``.
"""
from __future__ import annotations

import pytest

from prxref import orchestrator
from prxref.orchestrator import orchestrate_review
from prxref.triage import (
    DEFAULT_TOKEN_BUDGET,
    est_tokens,
    parse_unified_diff,
    plan_chunks,
)
from tests.test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff

HUNKS = 12
LINES_PER_HUNK = 100
BIG = "src/big.py"


def _hunk_start(k: int) -> int:
    return 1000 * k + (k - 1) * LINES_PER_HUNK


def _many_hunk_file(path: str = BIG, hunks: int = HUNKS) -> str:
    out = [f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}"]
    for k in range(1, hunks + 1):
        old = 1000 * k
        new = _hunk_start(k)
        out.append(f"@@ -{old},1 +{new},{LINES_PER_HUNK + 1} @@")
        out.append(" ctx")
        out.extend(f"+data {k}.{i}" for i in range(LINES_PER_HUNK))
    return "\n".join(out) + "\n"


def _small_files(n: int = 4) -> str:
    return "".join(_added_file_diff(f"pkg/small{i}.py", 20) for i in range(n))


ISSUE_DIFF = _small_files() + _many_hunk_file()


def _plan(diff: str = ISSUE_DIFF, **kw):
    return plan_chunks(parse_unified_diff(diff), **kw)


def _starts(chunks, path: str) -> list[int]:
    return [h.new_start for c in chunks for f in c if f.path == path for h in f.hunks]


def test_the_fixture_is_over_budget_as_one_file():
    big = next(f for f in parse_unified_diff(ISSUE_DIFF) if f.path == BIG)
    assert est_tokens(big) > DEFAULT_TOKEN_BUDGET


def test_an_over_budget_file_is_split_into_chunks_that_all_fit():
    plan = _plan()
    assert plan.chunks_over_budget == 0
    assert all(t <= DEFAULT_TOKEN_BUDGET for t in plan.chunk_tokens)
    assert len([c for c in plan.chunks if any(f.path == BIG for f in c)]) >= 2


def test_every_hunk_appears_exactly_once_and_in_order():
    plan = _plan()
    starts = _starts(plan.chunks, BIG)
    assert sorted(starts) == [_hunk_start(k) for k in range(1, HUNKS + 1)]
    for c in plan.chunks:
        in_chunk = _starts([c], BIG)
        assert in_chunk == sorted(in_chunk)


def test_a_split_piece_keeps_its_file_identity_and_line_numbers():
    plan = _plan()
    whole = next(f for f in parse_unified_diff(ISSUE_DIFF) if f.path == BIG)
    pieces = [f for c in plan.chunks for f in c if f.path == BIG]
    assert all(
        (p.old_path, p.new_path, p.status) == (whole.old_path, whole.new_path, whole.status)
        for p in pieces
    )
    assert [h for p in pieces for h in p.hunks] == whole.hunks
    assert sum(p.lines_added for p in pieces) == whole.lines_added


def test_one_giant_hunk_goes_alone_and_counts_over_budget():
    giant = _added_file_diff("src/giant.py", 650)
    plan = _plan(_small_files() + giant)
    assert plan.chunks_over_budget == 1
    holders = [c for c in plan.chunks if any(f.path == "src/giant.py" for f in c)]
    assert len(holders) == 1 and [f.path for f in holders[0]] == ["src/giant.py"]
    others = [c for c in plan.chunks if c is not holders[0]]
    assert others and all(
        t <= DEFAULT_TOKEN_BUDGET
        for c, t in zip(plan.chunks, plan.chunk_tokens, strict=True) if c is not holders[0]
    )


def test_a_giant_hunk_beside_small_ones_is_isolated_not_the_whole_file():
    out = [f"diff --git a/{BIG} b/{BIG}", f"--- a/{BIG}", f"+++ b/{BIG}"]
    out += ["@@ -1,1 +1,11 @@", " ctx", *[f"+small {i}" for i in range(10)]]
    out += ["@@ -500,1 +510,701 @@", " ctx", *[f"+huge {i}" for i in range(700)]]
    out += ["@@ -900,1 +1610,11 @@", " ctx", *[f"+tail {i}" for i in range(10)]]
    plan = _plan("\n".join(out) + "\n")
    assert plan.chunks_over_budget == 1
    assert sorted(_starts(plan.chunks, BIG)) == [1, 510, 1610]
    for c in plan.chunks:
        starts = _starts([c], BIG)
        assert starts == sorted(starts)


def test_a_diff_with_no_over_budget_file_plans_exactly_as_before():
    diff = _small_files(6) + _added_file_diff("lib/mid.py", 300)
    files = parse_unified_diff(diff)
    plan = plan_chunks(files)
    assert plan.chunks_over_budget == 0
    ids = {id(f) for f in files}
    assert all(id(f) in ids for c in plan.chunks for f in c)


_OK = {
    "escalations": [], "input_tokens": 5, "output_tokens": 5,
    "model": "m", "elapsed_ms": 1, "error": "",
}


def _finding_for_hunk(files, k: int, line: int):
    from prxref.triage import Finding

    has = any(h.new_start == _hunk_start(k) for f in files if f.path == BIG for h in f.hunks)
    if not has:
        return []
    return [Finding(file=BIG, line=line, severity="warning", confidence=0.9,
                    title="Bad data", body="data is wrong here")]


@pytest.mark.usefixtures("contract_stubs")
def test_a_finding_in_the_split_file_keeps_its_real_line_through_a_run(monkeypatch):
    line = _hunk_start(HUNKS) + 2

    def _rc(llm, files, **kwargs):
        return _finding_for_hunk(files, HUNKS, line), _OK

    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _rc)
    res = orchestrate_review(
        FakeForge(diff=ISSUE_DIFF), REF, FakeLLM("{}"), post=False, max_chunks=8,
    )
    assert res["chunks_over_budget"] == 0
    assert [f.line for f in res["findings_active"] if f.file == BIG] == [line]
    assert res["chunks_failed"] == 0


@pytest.mark.usefixtures("contract_stubs")
def test_a_failed_piece_keeps_the_other_findings_and_names_the_skipped_file(monkeypatch):
    first = _hunk_start(1) + 2

    def _rc(llm, files, **kwargs):
        if _finding_for_hunk(files, HUNKS, 0):
            return [], {**_OK, "error": "LLMError: all models failed: m1: HTTP 413"}
        return _finding_for_hunk(files, 1, first), _OK

    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _rc)
    res = orchestrate_review(
        FakeForge(diff=ISSUE_DIFF), REF, FakeLLM("{}"), post=False, max_chunks=8,
    )
    assert res["verdict"] == "Incomplete"
    assert res["chunks_failed"] == 1
    assert [f.line for f in res["findings_active"] if f.file == BIG] == [first]
    assert BIG in res["failed_chunks"][0]["files"]
