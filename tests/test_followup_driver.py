"""The context follow-up driver for one worker chunk (#22)."""
from __future__ import annotations

import copy
import logging
from pathlib import Path

import pytest

from prxref import followup
from prxref.followup import ROW_KEYS, SKIP_REASONS, run_chunk_followup, skipped_row
from prxref.followup_merge import UNCONFIRMED_PREFIX
from prxref.repo_context import exclude_predicate
from prxref.repo_followup import FOLLOWUP_HEADER
from prxref.triage import Finding, build_chunks, parse_unified_diff

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue22"
REPO = FIXTURE / "repo"

R2_BODY = (
    "`_ledger` puts a live `ProgressLedger` object into `run.root().data`. `Engine.run_turn` passes that "
    "same `run` to `self.store.save(run)` on handoff. `StateStore` is not shown here. Does it serialize "
    "frame data in a way that fails on arbitrary objects, e.g. JSON? The conftest turns the feature off for "
    "every existing test, so save/resume with the default-on setting looks untested."
)
FLOOR = 0.6


class RecordingTracer:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def event(self, node: str, phase: str, **meta) -> None:
        self.events.append((node, phase, meta))

    def phases(self) -> list[str]:
        return [phase for node, phase, _ in self.events if node == "followup"]


class FakeInvoke:
    def __init__(self, reply=None, *, raises: Exception | None = None) -> None:
        self.reply = reply
        self.raises = raises
        self.blocks: list[str] = []

    def __call__(self, block: str):
        self.blocks.append(block)
        if self.raises is not None:
            raise self.raises
        return self.reply


@pytest.fixture(scope="module")
def pr():
    files = parse_unified_diff((FIXTURE / "pr.patch").read_text())
    return files, build_chunks(files)


@pytest.fixture(scope="module")
def texts() -> dict[str, str]:
    return {p.relative_to(REPO).as_posix(): p.read_text() for p in REPO.rglob("*") if p.is_file()}


def _question() -> Finding:
    return Finding("assistant/progress.py", 47, "warning", 0.5, "Serialization of the ledger", R2_BODY)


def _kept() -> Finding:
    return Finding("assistant/engine.py", 10, "warning", 0.8, "Kept", "A finding above the floor.")


def _first(findings=None, **extra) -> dict:
    out = {
        "findings": [_kept(), _question()] if findings is None else findings,
        "error": "",
        "input_tokens": 100,
        "output_tokens": 20,
        "model": "first-model",
        "elapsed_ms": 1000,
        "cost_usd": 0.01,
        "cost_source": "reported",
        "parse_retries": 0,
        "first_error": "",
    }
    out.update(extra)
    return out


def _reply(findings=(), **extra) -> dict:
    out = {
        "findings": list(findings),
        "error": "",
        "input_tokens": 150,
        "output_tokens": 30,
        "model": "rerun-model",
        "elapsed_ms": 500,
        "cost_usd": 0.02,
        "cost_source": "reported",
        "parse_retries": 0,
        "first_error": "",
    }
    out.update(extra)
    return out


def _run(pr, texts, first, invoke, *, shown="", read=None, tracer=None):
    files, chunks = pr
    tracer = tracer if tracer is not None else RecordingTracer()
    result, row = run_chunk_followup(
        first,
        chunk=chunks[0],
        all_files=files,
        read=read if read is not None else texts.get,
        listing_paths=frozenset(texts),
        listing_complete=True,
        exclude=exclude_predicate([]),
        shown=shown,
        floor=FLOOR,
        invoke=invoke,
        index=1,
        total=2,
        tracer=tracer,
    )
    return result, row, tracer


def test_row_has_exactly_the_record_keys():
    row = skipped_row("timeout-retry", questions=2)
    assert tuple(row) == ROW_KEYS
    assert row["skipped"] == "timeout-retry" and row["questions"] == 2 and row["called"] is False
    assert set(SKIP_REASONS) == {"no-questions", "no-names", "no-excerpts", "chunk-error", "timeout-retry"}


def test_no_questions_makes_no_call_and_no_event(pr, texts):
    first = _first([_kept()])
    invoke = FakeInvoke(_reply())
    result, row, tracer = _run(pr, texts, first, invoke)
    assert invoke.blocks == []
    assert result == first
    assert row == skipped_row("no-questions")
    assert tracer.events == []


def test_chunk_error_is_skipped_before_anything_else(pr, texts):
    first = _first(error="model-a: timeout (read timed out)")
    invoke = FakeInvoke(_reply())
    result, row, tracer = _run(pr, texts, first, invoke)
    assert invoke.blocks == []
    assert result == first
    assert row["skipped"] == "chunk-error" and row["called"] is False
    assert tracer.events == []


def test_no_names_skips_with_an_event(pr, texts):
    question = Finding("assistant/progress.py", 47, "warning", 0.5, "Question", "Is `ProgressLedger` safe here?")
    invoke = FakeInvoke(_reply())
    result, row, tracer = _run(pr, texts, _first([question]), invoke)
    assert invoke.blocks == []
    assert row["skipped"] == "no-names" and row["questions"] == 1 and row["names"] == []
    assert tracer.phases() == ["skip"]
    assert tracer.events[0][2]["reason"] == "no-names"


def test_no_excerpts_makes_no_call(pr, texts):
    own = {f.path for f in pr[1][0]}
    invoke = FakeInvoke(_reply())
    result, row, tracer = _run(pr, texts, _first(), invoke, read=lambda p: texts.get(p) if p in own else None)
    assert invoke.blocks == []
    assert row["skipped"] == "no-excerpts"
    assert row["names"][0] == "StateStore"
    assert row["excerpts"] == []
    assert tracer.phases() == ["skip"]
    assert result["findings"] == _first()["findings"]


def test_confirmation_replaces_the_question(pr, texts, caplog):
    confirm = Finding(
        "assistant/progress.py", 47, "error", 0.9, "Ledger breaks save",
        "`StateStore.save` calls `json.dumps(asdict(run))`, which fails on the live ledger.",
    )
    other = Finding("assistant/engine.py", 30, "warning", 0.9, "Unrelated", "Something else entirely.")
    invoke = FakeInvoke(_reply([confirm, other]))
    first = _first()
    before = copy.deepcopy(first)
    with caplog.at_level(logging.INFO, logger="prxref"):
        result, row, tracer = _run(pr, texts, first, invoke)
    assert first == before
    assert len(invoke.blocks) == 1
    block = invoke.blocks[0]
    assert block.startswith(FOLLOWUP_HEADER)
    assert "assistant/state_store.py:8: class StateStore:" in block
    assert "json.dumps(asdict(run))" in block
    assert result["findings"] == [_kept(), confirm]
    assert (row["called"], row["skipped"], row["error"]) == (True, None, "")
    assert (row["confirmed"], row["unconfirmed"], row["discarded"]) == (1, 0, 1)
    assert row["names"][0] == "StateStore"
    assert row["excerpts"][0]["path"] == "assistant/state_store.py"
    assert row["excerpts"][0]["line"] == 8
    assert (row["input_tokens"], row["output_tokens"]) == (150, 30)
    assert (result["input_tokens"], result["output_tokens"]) == (250, 50)
    assert result["model"] == "rerun-model"
    assert result["cost_usd"] == pytest.approx(0.03)
    assert result["cost_source"] == "reported"
    assert (result["parse_retries"], result["first_error"]) == (0, "")
    assert tracer.phases() == ["start", "ok"]
    messages = [r.getMessage() for r in caplog.records]
    assert any("context follow-up: 1 question(s)" in m and "re-running once" in m for m in messages)
    assert "[chunk 1/2] context follow-up: 1 confirmed, 0 not confirmed, 1 other finding(s) discarded" in messages


def test_confirmation_by_a_covered_name(pr, texts):
    confirm = Finding(
        "assistant/progress.py", 90, "error", 0.9, "Different title",
        "Calling save on this run serializes the ledger: `save` fails.",
    )
    invoke = FakeInvoke(_reply([confirm]))
    result, row, _ = _run(pr, texts, _first(), invoke)
    assert result["findings"][1] is confirm
    assert row["confirmed"] == 1


def test_refutation_marks_the_question_unconfirmed(pr, texts):
    invoke = FakeInvoke(_reply([]))
    result, row, tracer = _run(pr, texts, _first(), invoke)
    assert len(invoke.blocks) == 1
    kept, question = result["findings"]
    assert kept == _kept()
    assert question.drop_reason == f"{UNCONFIRMED_PREFIX} (confidence 0.50 below floor 0.60)"
    assert (row["confirmed"], row["unconfirmed"], row["discarded"]) == (0, 1, 0)
    assert tracer.phases() == ["start", "ok"]


def test_rerun_error_keeps_the_first_findings_and_counts_tokens(pr, texts, caplog):
    invoke = FakeInvoke(_reply([Finding("assistant/progress.py", 47, "error", 0.9, "x", "y")],
                               error="response truncated at max_tokens=10 (finish_reason=length)"))
    first = _first()
    with caplog.at_level(logging.WARNING, logger="prxref"):
        result, row, tracer = _run(pr, texts, first, invoke)
    assert result["findings"] == first["findings"]
    assert row["called"] is True
    assert row["error"].startswith("response truncated")
    assert (row["confirmed"], row["unconfirmed"], row["discarded"]) == (0, 0, 0)
    assert (result["input_tokens"], result["output_tokens"]) == (250, 50)
    assert tracer.phases() == ["start", "fail"]
    assert any(
        "context follow-up failed (keeping the first review): response truncated" in r.getMessage()
        for r in caplog.records
    )


def test_invoke_that_raises_keeps_the_first_result(pr, texts):
    invoke = FakeInvoke(raises=RuntimeError("socket closed"))
    first = _first()
    result, row, tracer = _run(pr, texts, first, invoke)
    assert result == first
    assert row["called"] is True and row["error"] == "socket closed"
    assert (row["input_tokens"], row["output_tokens"]) == (0, 0)
    assert tracer.phases() == ["start", "fail"]


def test_failure_inside_the_merge_still_folds_tokens(pr, texts, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("merge broke")

    monkeypatch.setattr(followup, "merge_followup", boom)
    first = _first()
    result, row, _ = _run(pr, texts, first, FakeInvoke(_reply([])))
    assert result["findings"] == first["findings"]
    assert row["error"] == "merge broke"
    assert result["input_tokens"] == 250


def test_failure_before_the_call_never_raises(pr, texts, monkeypatch):
    def boom(*args, **kwargs):
        raise KeyError("lookup")

    monkeypatch.setattr(followup, "lookup_names", boom)
    invoke = FakeInvoke(_reply())
    first = _first()
    result, row, tracer = _run(pr, texts, first, invoke)
    assert result == first
    assert invoke.blocks == []
    assert row["called"] is False and row["error"]
    assert tracer.phases() == ["fail"]


def test_cost_fold_with_an_unreported_side_is_unknown(pr, texts):
    first = _first(cost_usd=None, cost_source="")
    result, _, _ = _run(pr, texts, first, FakeInvoke(_reply([])))
    assert (result["cost_usd"], result["cost_source"]) == (None, "")
    result, _, _ = _run(pr, texts, _first(), FakeInvoke(_reply([], cost_usd=None, cost_source="")))
    assert (result["cost_usd"], result["cost_source"]) == (None, "")


def test_model_stays_the_first_when_the_rerun_names_none(pr, texts):
    result, _, _ = _run(pr, texts, _first(), FakeInvoke(_reply([], model="")))
    assert result["model"] == "first-model"


def test_question_with_a_drop_reason_is_not_a_question(pr, texts):
    dropped = Finding("assistant/progress.py", 47, "warning", 0.5, "q", R2_BODY, drop_reason="earlier pass")
    invoke = FakeInvoke(_reply())
    _, row, _ = _run(pr, texts, _first([dropped]), invoke)
    assert invoke.blocks == []
    assert row["skipped"] == "no-questions"


def test_none_tracer_uses_the_default(pr, texts):
    files, chunks = pr
    result, row = run_chunk_followup(
        _first([_kept()]), chunk=chunks[0], all_files=files, read=texts.get,
        listing_paths=frozenset(texts), listing_complete=True, exclude=None, shown="",
        floor=FLOOR, invoke=FakeInvoke(_reply()), index=1, total=1, tracer=None,
    )
    assert row["skipped"] == "no-questions"
