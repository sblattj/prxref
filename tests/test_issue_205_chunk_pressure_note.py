"""Issue 205: a review packed past the chunk limits says so in the posted body.

A purely additive PR of about 150 small files ran into the chunk cap: most
files were placed through the overflow branch of ``plan_chunks`` into chunks
far past the token budget, and the posted review was a plain ``Approved``
with nothing telling the reader that most of the diff was read in
overstuffed chunks. Every file still reaches a worker prompt (coverage is
the chunker's invariant), so the fix is the signal: the summary opens with a
reduced-depth note naming the files and lines placed past the limits and
the chunks over budget. The verdict, ``degraded`` and the exit code are
unchanged.
"""
from __future__ import annotations

import pytest

from prxref import orchestrator
from prxref.orchestrator import orchestrate_review
from prxref.reviewer import render_chunk
from prxref.triage import parse_unified_diff, plan_chunks
from tests.test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff

pytestmark = pytest.mark.usefixtures("contract_stubs")

ISSUE_DIFF = "".join(
    _added_file_diff(f"src/main/java/p{i % 7}/F{i}.java", 80) for i in range(150)
)
NOTE_START = "> ⚠️ Reduced review depth:"


def _post(diff: str, **kw):
    forge = FakeForge(diff=diff)
    res = orchestrate_review(forge, REF, FakeLLM('{"findings": []}'), post=True, **kw)
    return res, forge.summaries[-1]


def test_the_issue_case_posts_the_note():
    res, body = _post(ISSUE_DIFF)
    assert res["overflow_files"] > 0 and res["chunks_over_budget"] > 0
    first = body.splitlines()[0]
    assert first.startswith(NOTE_START), body[:300]
    n, over = res["overflow_files"], res["chunks_over_budget"]
    assert f"{n} of 150 files ({n * 80} changed lines)" in first
    assert f"{over} of " in first and "-token budget" in first
    for knob in ("PRXREF_MAX_CHUNKS", "PRXREF_CHUNK_MAX_FILES", "PRXREF_CHUNK_TOKEN_BUDGET"):
        assert knob in first


def test_the_verdict_and_degraded_are_unchanged():
    res, _ = _post(ISSUE_DIFF)
    assert res["verdict"] == "Approved"
    assert res["degraded"] is None


def test_every_file_still_reaches_a_worker_prompt(monkeypatch):
    seen: list[str] = []
    stub = orchestrator.reviewer.review_chunk

    def spy(llm, files, **kw):
        seen.append(render_chunk(files, kw.get("context_lines")))
        return stub(llm, files, **kw)

    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", spy)
    _post(ISSUE_DIFF)
    text = "\n".join(seen)
    assert [i for i in range(150) if f"/F{i}.java" not in text] == []


def test_a_run_without_pressure_has_no_note():
    _, body = _post(_added_file_diff("src/app.py", 20))
    assert "Reduced review depth" not in body


def test_a_lone_hunk_over_budget_has_no_note():
    # One single-hunk file over the budget opens its own chunk and is read
    # alone (#148), with nothing placed past the limits.
    res, body = _post(_added_file_diff("src/big.py", 400), token_budget=1000)
    assert (res["chunks_over_budget"], res["overflow_files"]) == (1, 0)
    assert "Reduced review depth" not in body


def test_file_cap_overflow_alone_names_no_budget():
    diff = "".join(_added_file_diff(f"src/f{i}.py", 5) for i in range(3))
    res, body = _post(diff, max_chunks=1, max_files_per_chunk=1)
    assert (res["overflow_files"], res["chunks_over_budget"]) == (2, 0)
    first = body.splitlines()[0]
    assert first.startswith(f"{NOTE_START} 2 of 3 files (10 changed lines) were placed past")
    assert "token budget" not in first


def test_the_plan_names_its_overflow_paths():
    plan = plan_chunks(parse_unified_diff(ISSUE_DIFF))
    assert len(plan.overflow_paths) == plan.overflow_files > 0
    assert len(set(plan.overflow_paths)) == len(plan.overflow_paths)
