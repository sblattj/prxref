"""The parse retry through the orchestrator and the CLI (issue #21).

``orchestrate_review(llm_parse_retries=N)`` hands N to every chunk worker and
to the sweep as ``parse_retries``; the library default is 0 and the CLI
passes ``PRXREF_LLM_PARSE_RETRIES`` (default 1). A unit whose reviewer meta
carries ``parse_retries`` and ``first_error`` keeps both in its worker
result, and the run record's ``parse_retries`` is ``None`` at N=0 and the
sum over every chunk and the sweep otherwise.

The retry itself is the real reviewer's (tests/test_parse_retry_reviewer.py
covers it unit by unit); these tests drive it through ``orchestrate_review``
with a scripted LLM, and read each unit's worker result where the run
prices it (``_stamp_run_cost``, wrapped, not replaced).
"""
from __future__ import annotations

import json

import pytest

from prxref import cli, orchestrator
from prxref.llm import InvokeResult
from prxref.orchestrator import orchestrate_review
from tests.test_integration import MockOpenAIServer, _CLIHarness, _completion, _good_content
from tests.test_orchestrator import REF, FakeForge, _added_file_diff, multi_chunk_diff
from tests.test_run_record import PATHS, SCOPED_PATHS, _run

VALID = json.dumps({"findings": []})
BAD = "not json at all"
LIST = "[]"
ONE_FINDING = json.dumps({"findings": [{
    "file": "src/app.py", "line": 3, "severity": "warning", "confidence": 0.9,
    "title": "Unchecked data write", "body": "The data line is written without validation.",
}]})
BAD_ERROR = "JSONDecodeError: Expecting value: line 1 column 1 (char 0)"
LIST_ERROR = "worker review JSON is not an object: list"
TIMEOUT = "LLMError: m1: timeout (ReadTimeout)"
ONE_CHUNK_DIFF = _added_file_diff("src/app.py", 20)


class ScriptedLLM:
    """Returns one scripted reply text per ``invoke`` call, in call order."""

    def __init__(self, texts: list[str]):
        self.texts = list(texts)
        self.calls = 0

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.calls += 1
        return InvokeResult(
            text=self.texts.pop(0), input_tokens=100, output_tokens=50,
            model="test-model-1", backend="fake", elapsed_ms=1, finish_reason="stop",
        )


@pytest.fixture
def units(monkeypatch):
    """Every review unit's worker result, as the run prices it (chunks in order, then the sweep)."""
    seen: list[dict] = []
    real = orchestrator._stamp_run_cost

    def spy(run_inputs, results, price_table):
        seen.extend(dict(unit) for unit in results)
        real(run_inputs, results, price_table)

    monkeypatch.setattr(orchestrator, "_stamp_run_cost", spy)
    return seen


def _review(texts, *, diff=ONE_CHUNK_DIFF, **kw):
    llm = ScriptedLLM(texts)
    res = orchestrate_review(FakeForge(diff=diff), REF, llm, post=False, max_workers=1, **kw)
    return res, llm


def _meta_double(calls: list, name: str, outcomes: list[dict] | None = None, *, raises=None):
    """A review_chunk / review_systemic double that records its ``parse_retries`` keyword.

    It takes ``**kwargs``, so a keyword the orchestrator stopped passing is
    recorded as ``"absent"`` rather than breaking the call. Each call pops
    one dict from ``outcomes`` into its meta when given.
    """
    def double(llm, target, **kwargs):
        calls.append((name, kwargs.get("parse_retries", "absent")))
        if raises is not None:
            raise raises
        meta = {
            "escalations": [], "input_tokens": 1, "output_tokens": 1,
            "model": "m", "elapsed_ms": 1, "error": "",
        }
        if outcomes:
            meta.update(outcomes.pop(0))
        return [], meta

    return double


class TestLibraryDefault:
    def test_the_record_is_null_and_a_bad_reply_is_not_retried(self, units):
        res, llm = _review([BAD, VALID])
        assert llm.calls == 2
        assert res["parse_retries"] is None
        assert res["chunks_failed"] == 1
        assert res["verdict"] == "Error"
        assert units[0]["error"] == BAD_ERROR
        assert all("parse_retries" not in unit and "first_error" not in unit for unit in units)

    def test_an_explicit_zero_is_the_default(self, units):
        res, llm = _review([BAD, VALID], llm_parse_retries=0)
        assert llm.calls == 2
        assert res["parse_retries"] is None
        assert all("parse_retries" not in unit for unit in units)

    def test_a_negative_budget_records_null_like_zero(self):
        res, llm = _review([BAD, VALID], llm_parse_retries=-1)
        assert llm.calls == 2
        assert res["parse_retries"] is None

    def test_a_non_int_budget_is_an_error_run_not_a_raise(self):
        """A degenerate library argument degrades the run, as ``max_chunks=0`` does."""
        res, _ = _review([VALID, VALID], llm_parse_retries=None)
        assert res["verdict"] == "Error"
        assert res["parse_retries"] is None


class TestOneRetry:
    def test_a_malformed_then_valid_chunk_records_one_and_keeps_its_meta(self, units):
        res, llm = _review([BAD, ONE_FINDING, VALID], llm_parse_retries=1)
        assert llm.calls == 3
        assert res["parse_retries"] == 1
        assert res["chunks_failed"] == 0
        chunk, sweep = units
        assert chunk["error"] == ""
        assert chunk["parse_retries"] == 1
        assert chunk["first_error"] == BAD_ERROR
        assert len(chunk["findings"]) == 1
        assert list(chunk)[-2:] == ["parse_retries", "first_error"]
        assert "parse_retries" not in sweep and "first_error" not in sweep

    def test_both_calls_count_toward_the_units_tokens(self, units):
        res, _ = _review([BAD, VALID, VALID], llm_parse_retries=1)
        assert units[0]["input_tokens"] == 200
        assert units[0]["output_tokens"] == 100
        assert res["input_tokens"] == 300
        assert res["output_tokens"] == 150

    def test_a_clean_run_records_zero(self, units):
        res, llm = _review([VALID, VALID], llm_parse_retries=1)
        assert llm.calls == 2
        assert res["parse_retries"] == 0
        assert all("parse_retries" not in unit for unit in units)

    def test_malformed_twice_fails_the_chunk_and_the_retry_still_counts(self, units):
        res, llm = _review([BAD, BAD, VALID], llm_parse_retries=1)
        assert llm.calls == 3
        assert res["verdict"] == "Error"
        assert res["chunks_failed"] == 1
        assert res["parse_retries"] == 1
        assert units[0]["error"] == BAD_ERROR
        assert units[0]["parse_retries"] == 1

    def test_the_sweep_keeps_its_meta_and_counts(self, units):
        res, llm = _review([VALID, LIST, VALID], llm_parse_retries=1)
        assert llm.calls == 3
        assert res["parse_retries"] == 1
        chunk, sweep = units
        assert "parse_retries" not in chunk
        assert sweep["parse_retries"] == 1
        assert sweep["first_error"] == LIST_ERROR
        assert sweep["error"] == ""


class TestTheSum:
    def test_chunks_and_the_sweep_add_up(self, units):
        texts = [BAD, VALID, VALID, LIST, VALID]
        res, llm = _review(texts, diff=multi_chunk_diff(2), llm_parse_retries=1)
        assert llm.calls == 5
        assert res["chunk_count"] == 3
        assert res["parse_retries"] == 2
        first, second, sweep = units
        assert (first["parse_retries"], first["first_error"]) == (1, BAD_ERROR)
        assert "parse_retries" not in second
        assert (sweep["parse_retries"], sweep["first_error"]) == (1, LIST_ERROR)

    def test_it_sums_retries_not_units(self, units):
        texts = [BAD, LIST, VALID, BAD, VALID]
        res, llm = _review(texts, llm_parse_retries=2)
        assert llm.calls == 5
        assert res["parse_retries"] == 3
        assert units[0]["parse_retries"] == 2
        assert units[0]["first_error"] == BAD_ERROR
        assert units[1]["parse_retries"] == 1

    def test_the_json_payload_carries_the_total(self):
        res, _ = _review([BAD, VALID, VALID], llm_parse_retries=1)
        payload = cli._build_json_result(res)
        assert payload["parse_retries"] == 1
        keys = list(payload)
        assert keys.index("parse_retries") == keys.index("repo_context") + 1


class TestThreading:
    """Every worker and sweep call gets the budget as ``parse_retries=``, 0 included."""

    def _doubles(self, monkeypatch, chunk_outcomes=None, sweep_outcomes=None):
        calls: list = []
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk", _meta_double(calls, "chunk", chunk_outcomes),
        )
        monkeypatch.setattr(
            orchestrator.reviewer, "review_systemic", _meta_double(calls, "sweep", sweep_outcomes),
        )
        return calls

    def test_the_default_passes_zero_explicitly(self, monkeypatch):
        calls = self._doubles(monkeypatch)
        orchestrate_review(FakeForge(diff=multi_chunk_diff(2)), REF, ScriptedLLM([]), post=False)
        assert sorted(calls) == [("chunk", 0), ("chunk", 0), ("sweep", 0)]

    def test_the_budget_reaches_every_chunk_and_the_sweep(self, monkeypatch):
        calls = self._doubles(monkeypatch)
        orchestrate_review(
            FakeForge(diff=multi_chunk_diff(2)), REF, ScriptedLLM([]), post=False,
            llm_parse_retries=3,
        )
        assert sorted(calls) == [("chunk", 3), ("chunk", 3), ("sweep", 3)]

    def test_the_timeout_retry_gets_it_too(self, monkeypatch):
        calls = self._doubles(monkeypatch, chunk_outcomes=[{"error": TIMEOUT}, {}])
        res = orchestrate_review(
            FakeForge(diff=ONE_CHUNK_DIFF), REF, ScriptedLLM([]), post=False,
            llm_parse_retries=2,
        )
        assert calls == [("chunk", 2), ("chunk", 2), ("sweep", 2)]
        assert res["chunks_failed"] == 0
        assert res["parse_retries"] == 0


class TestWhatCarriesNeither:
    def test_a_timeout_retry_keeps_only_the_second_runs_count(self, monkeypatch, units):
        """The documented gap: the timeout retry replaces the first run's result, counts included."""
        calls: list = []
        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _meta_double(calls, "chunk", [
            {"error": TIMEOUT, "parse_retries": 1, "first_error": "first run"},
            {"parse_retries": 1, "first_error": "second run"},
        ]))
        monkeypatch.setattr(orchestrator.reviewer, "review_systemic", _meta_double(calls, "sweep"))
        res = orchestrate_review(
            FakeForge(diff=ONE_CHUNK_DIFF), REF, ScriptedLLM([]), post=False,
            llm_parse_retries=1,
        )
        assert res["parse_retries"] == 1
        assert units[0]["first_error"] == "second run"

    def test_a_raising_chunk_call_carries_neither(self, monkeypatch, units):
        calls: list = []
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _meta_double(calls, "chunk", raises=RuntimeError("chunk boom")),
        )
        monkeypatch.setattr(orchestrator.reviewer, "review_systemic", _meta_double(calls, "sweep", [
            {"parse_retries": 1, "first_error": LIST_ERROR},
        ]))
        res = orchestrate_review(
            FakeForge(diff=ONE_CHUNK_DIFF), REF, ScriptedLLM([]), post=False,
            llm_parse_retries=1,
        )
        assert res["verdict"] == "Error"
        assert res["parse_retries"] == 1
        assert units[0]["error"] == "chunk boom"
        assert "parse_retries" not in units[0] and "first_error" not in units[0]

    def test_a_raising_sweep_carries_neither(self, monkeypatch, units):
        calls: list = []
        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _meta_double(calls, "chunk", [
            {"parse_retries": 1, "first_error": BAD_ERROR},
        ]))
        monkeypatch.setattr(
            orchestrator.reviewer, "review_systemic",
            _meta_double(calls, "sweep", raises=RuntimeError("sweep boom")),
        )
        res = orchestrate_review(
            FakeForge(diff=ONE_CHUNK_DIFF), REF, ScriptedLLM([]), post=False,
            llm_parse_retries=1,
        )
        assert res["parse_retries"] == 1
        assert units[1]["error"] == "systemic sweep: sweep boom"
        assert "parse_retries" not in units[1] and "first_error" not in units[1]

    def test_a_crashed_worker_carries_neither(self, monkeypatch, units):
        def crash(*args, **kwargs):
            raise RuntimeError("worker boom")

        monkeypatch.setattr(orchestrator, "_run_worker", crash)
        res, _ = _review([VALID], llm_parse_retries=1)
        assert res["verdict"] == "Error"
        assert res["parse_retries"] == 0
        assert units[0]["error"] == "worker crashed: worker boom"
        assert "parse_retries" not in units[0] and "first_error" not in units[0]


@pytest.mark.usefixtures("contract_stubs")
class TestEveryExit:
    """The key rides every exit through the run-record choke point."""

    @pytest.mark.parametrize("path", PATHS + SCOPED_PATHS)
    @pytest.mark.parametrize(("n", "expected"), [(0, None), (1, 0)])
    def test_every_exit_carries_the_key(self, monkeypatch, tmp_path, path, n, expected):
        res, _, _ = _run(monkeypatch, path, tmp_path, llm_parse_retries=n)
        assert "parse_retries" in res
        assert res["parse_retries"] == expected
        assert type(res["parse_retries"]) is type(expected)
        assert cli._build_json_result(res)["parse_retries"] == expected


class TestHelpers:
    def test_the_total_skips_units_without_an_int(self):
        units = [
            {"parse_retries": 2}, {}, {"parse_retries": True},
            {"parse_retries": "3"}, {"parse_retries": None}, {"parse_retries": 1},
        ]
        assert orchestrator._parse_retry_total(units) == 3

    def test_the_total_of_no_units_is_zero(self):
        assert orchestrator._parse_retry_total([]) == 0

    def test_retry_meta_is_empty_without_the_count(self):
        assert orchestrator._retry_meta({"error": "", "first_error": "stray"}) == {}

    def test_retry_meta_carries_both_keys(self):
        meta = {"parse_retries": 2, "first_error": LIST_ERROR, "error": ""}
        assert orchestrator._retry_meta(meta) == {"parse_retries": 2, "first_error": LIST_ERROR}

    def test_retry_meta_defaults_a_missing_first_error(self):
        assert orchestrator._retry_meta({"parse_retries": 1}) == {"parse_retries": 1, "first_error": ""}


GARBAGE = "not json at all"


def _counting_route(replies):
    """An OpenAI route answering the Kth request with ``replies[K-1]``, then the last one forever."""
    state = {"n": 0}

    def route(payload):
        state["n"] += 1
        content = replies[min(state["n"], len(replies)) - 1]
        return 200, _completion(content, "stop")

    return route


class TestTheCliPath:
    """``cli._run_review`` with the integration harness: config default 1, real client and reviewer."""

    def _review(self, monkeypatch, replies, env=None):
        server = MockOpenAIServer(routes={"fast": _counting_route(replies)})
        base_url = server.start()
        try:
            harness = _CLIHarness(monkeypatch, base_url, env or {})
            result = harness.review(post=False)
        finally:
            server.stop()
        return result, server

    def test_the_default_makes_two_calls_on_a_garbage_reply(self, monkeypatch):
        result, server = self._review(
            monkeypatch, [GARBAGE, _good_content("src/auth.py"), VALID],
        )
        assert len(server.requests) == 3
        assert result["chunks_failed"] == 0
        assert result["parse_retries"] == 1
        assert len(result["findings_active"]) == 1
        assert result["input_tokens"] == 600
        assert result["output_tokens"] == 240
        assert cli._build_json_result(result)["parse_retries"] == 1

    def test_garbage_twice_fails_the_chunk_after_two_calls(self, monkeypatch):
        result, server = self._review(monkeypatch, [GARBAGE, GARBAGE, VALID])
        assert len(server.requests) == 3
        assert result["verdict"] == "Error"
        assert result["parse_retries"] == 1

    def test_zero_from_the_environment_makes_one_call_and_records_null(self, monkeypatch):
        """Control: the same replies, the budget turned off."""
        result, server = self._review(
            monkeypatch, [GARBAGE, _good_content("src/auth.py"), VALID],
            env={"PRXREF_LLM_PARSE_RETRIES": "0"},
        )
        assert len(server.requests) == 2
        assert result["verdict"] == "Error"
        assert result["parse_retries"] is None
        assert cli._build_json_result(result)["parse_retries"] is None
