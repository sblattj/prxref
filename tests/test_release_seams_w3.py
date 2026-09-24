"""Wave-3 release fix: a total LLM failure counts the review units truthfully.

When every chunk worker fails, ``orchestrate_review`` takes its total-failure
exit and returns verdict ``Error`` even if the systemic sweep answered: the
sweep sees only a pattern digest, so it must not turn a dead worker pool into
an approved review. That verdict, its reason and its posted notice are
unchanged. What changed is the count. The exit used to report every unit
failed, so a run whose sweep succeeded read ``chunks_reviewed 0,
chunks_failed 2``. It now counts the sweep that answered as reviewed:
``chunks_reviewed 1``, with ``chunks_reviewed + chunks_failed == chunk_count``
as on every other exit. A sweep that also failed still reports every unit
failed, which is the control.

The ``PRXREF_FAIL_ON`` gate keys on the verdict, not on the counts, so an
``Error`` run with a reviewed sweep still exits 1 when the lane opted in.

Only the forge, the model and the reviewer's chunk and sweep calls are
doubles (the ``contract_stubs`` fixture, as in tests/test_orchestrator.py);
the orchestrator and the CLI are the real ones.
"""
from __future__ import annotations

import inspect
import json
import sys
import time
import types

import pytest

from prxref import orchestrator as real_orchestrator
from prxref.cli import main
from prxref.forges.base import PRRef
from prxref.orchestrator import orchestrate_review
from tests.test_orchestrator import (
    REF,
    FakeForge,
    FakeLLM,
    _added_file_diff,
    _sweep_double,
    multi_chunk_diff,
)

CHUNK_ERROR = "JSONDecodeError: Expecting value: line 1 column 1 (char 0)"


def _every_chunk_fails(llm, files, **kwargs):
    return [], {
        "escalations": [], "input_tokens": 0, "output_tokens": 0,
        "model": "", "elapsed_ms": 0, "error": CHUNK_ERROR,
    }


def _notice_head(body: str) -> list[str]:
    """The notice's heading, reason and no-findings paragraphs, without the
    attribution line, whose token count and elapsed time vary by run."""
    return body.split("\n\n")[:3]


@pytest.mark.usefixtures("contract_stubs")
class TestTotalFailureCounts:
    @pytest.mark.parametrize("n_chunks", [1, 3])
    def test_a_sweep_that_answers_is_counted_as_reviewed(self, monkeypatch, n_chunks):
        monkeypatch.setattr(real_orchestrator.reviewer, "review_chunk", _every_chunk_fails)
        forge = FakeForge(diff=multi_chunk_diff(n_chunks))

        res = orchestrate_review(forge, REF, FakeLLM("{}"), post=True, max_chunks=n_chunks)

        assert res["verdict"] == "Error"
        assert res["chunk_count"] == n_chunks + 1
        assert res["chunks_reviewed"] == 1
        assert res["chunks_failed"] == n_chunks
        assert res["chunks_reviewed"] + res["chunks_failed"] == res["chunk_count"]
        assert res["findings_active"] == []
        assert len(forge.summaries) == 1
        notice = forge.summaries[0]
        assert f"all {n_chunks} worker reviews failed ({CHUNK_ERROR})" in notice
        assert "No findings were produced." in notice
        assert "Partial review" not in notice
        assert forge.inline_batches == []

    def test_a_sweep_that_also_fails_counts_every_unit_failed(self, monkeypatch):
        monkeypatch.setattr(real_orchestrator.reviewer, "review_chunk", _every_chunk_fails)
        double, calls = _sweep_double([("error", "LLMError: all models failed")])
        monkeypatch.setattr(real_orchestrator.reviewer, "review_systemic", double)
        forge = FakeForge(diff=multi_chunk_diff(3))

        res = orchestrate_review(forge, REF, FakeLLM("{}"), post=True, max_chunks=3)

        assert len(calls) == 1
        assert res["verdict"] == "Error"
        assert res["chunk_count"] == 4
        assert res["chunks_reviewed"] == 0
        assert res["chunks_failed"] == res["chunk_count"]

    def test_the_verdict_and_notice_do_not_depend_on_the_sweep(self, monkeypatch):
        """The count is the only thing the sweep's outcome moves on this exit."""
        monkeypatch.setattr(real_orchestrator.reviewer, "review_chunk", _every_chunk_fails)
        answered = FakeForge(diff=multi_chunk_diff(2))
        res_answered = orchestrate_review(
            answered, REF, FakeLLM("{}"), post=True, max_chunks=2,
        )

        double, _calls = _sweep_double([("error", "LLMError: all models failed")])
        monkeypatch.setattr(real_orchestrator.reviewer, "review_systemic", double)
        failed = FakeForge(diff=multi_chunk_diff(2))
        res_failed = orchestrate_review(failed, REF, FakeLLM("{}"), post=True, max_chunks=2)

        assert res_answered["verdict"] == res_failed["verdict"] == "Error"
        assert _notice_head(answered.summaries[0]) == _notice_head(failed.summaries[0])
        assert (res_answered["chunks_reviewed"], res_answered["chunks_failed"]) == (1, 2)
        assert (res_failed["chunks_reviewed"], res_failed["chunks_failed"]) == (0, 3)

    def test_the_earlier_error_exits_keep_their_counts(self):
        """get_pr, get_diff, parse and build_chunks failures run no review
        unit; the new parameter's default leaves their counts as they were."""
        param = inspect.signature(real_orchestrator._error_run).parameters["chunks_reviewed"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default == 0

        run = real_orchestrator._error_run(
            FakeForge(), REF, False, 3, "boom", time.perf_counter(),
        )
        assert (run["chunk_count"], run["chunks_reviewed"], run["chunks_failed"]) == (3, 0, 3)

        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        forge.fail.add("get_diff")
        res = orchestrate_review(forge, REF, FakeLLM("{}"), post=False)
        assert res["verdict"] == "Error"
        assert (res["chunk_count"], res["chunks_reviewed"], res["chunks_failed"]) == (0, 0, 0)


@pytest.mark.usefixtures("contract_stubs")
class TestFailOnGatesTheVerdictNotTheCounts:
    """An ``Error`` run with a reviewed sweep, through ``main`` and the real
    orchestrator: the chunk worker's model call raises, the stubbed sweep
    answers."""

    REF = PRRef(
        forge="github",
        host="github.com",
        owner="org",
        repo="repo",
        number=7,
        url="https://github.com/org/repo/pull/7",
    )

    @pytest.fixture
    def rig(self, monkeypatch):
        assert sys.modules["prxref.orchestrator"] is real_orchestrator
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        llm = FakeLLM(error=RuntimeError("provider down"))
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: self.REF)
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: forge)
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: llm)
        return types.SimpleNamespace(forge=forge, llm=llm)

    @pytest.mark.parametrize(("policy", "expected"), [("error", 1), ("any", 1), ("never", 0)])
    def test_an_error_run_with_a_reviewed_sweep_is_gated_when_opted_in(
        self, rig, monkeypatch, capsys, policy, expected
    ):
        monkeypatch.setenv("PRXREF_FAIL_ON", policy)
        argv = ["review", "--pr-url", self.REF.url, "--no-post", "--format", "json"]

        assert main(argv) == expected

        out, err = capsys.readouterr()
        payload = json.loads(out)
        assert payload["verdict"] == "Error"
        assert payload["chunk_count"] == 2
        assert payload["chunks_reviewed"] == 1
        assert payload["chunks_failed"] == 1
        assert rig.llm.calls == 1
        assert rig.forge.summaries == []
        note = f"PRXREF_FAIL_ON={policy}: review did not complete (verdict Error); exiting 1"
        assert (note in err) is (policy != "never")

    def test_the_text_summary_shows_the_reviewed_sweep(self, rig, monkeypatch, capsys):
        monkeypatch.setenv("PRXREF_FAIL_ON", "error")
        argv = ["review", "--pr-url", self.REF.url, "--no-post"]

        assert main(argv) == 1

        out, _err = capsys.readouterr()
        lines = out.splitlines()
        assert "verdict: Error" in lines
        assert "coverage: 1/2 chunks reviewed" in lines
