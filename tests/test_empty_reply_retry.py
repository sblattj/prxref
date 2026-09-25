"""An empty model reply is asked for once more, with the same prompt and budget.

0.15.0 and earlier failed the unit at parse time (``no parseable content``)
on a reply with no text and ``finish_reason=stop``, although the provider had
billed it. The reviewer now repeats that one call, once. It does not repeat a
reply the provider stopped at the budget, a non-empty reply that fails to
parse, or a call that raised.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from prxref.llm import InvokeResult
from prxref.orchestrator import orchestrate_review
from prxref.reviewer import review_chunk, review_systemic
from prxref.triage import parse_unified_diff
from tests.test_orchestrator import REF, FakeForge, _added_file_diff

MINI_DIFF = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 import os
+import sys
 def main():
     print("hi")
"""

VALID = json.dumps({
    "findings": [
        {
            "file": "src/app.py", "line": 2, "severity": "error",
            "confidence": 0.9, "title": "Unused import",
            "body": "sys is imported and never used.",
        },
    ],
    "escalations": [],
})

TODAYS_EMPTY_ERROR = "JSONDecodeError: no parseable content: line 1 column 1 (char 0)"
TODAYS_GARBAGE_ERROR = "JSONDecodeError: Expecting value: line 1 column 1 (char 0)"
RETRY_NOTE = "empty model reply"


def reply(text, *, finish_reason="stop", input_tokens=100, output_tokens=50,
          model="fake-model", cost_usd=None, cost_source=""):
    return InvokeResult(
        text=text, input_tokens=input_tokens, output_tokens=output_tokens,
        model=model, backend="fake", elapsed_ms=1, finish_reason=finish_reason,
        cost_usd=cost_usd, cost_source=cost_source,
    )


class ScriptedLLM:
    """Answers each ``invoke`` with the next scripted step and records the call.

    A step is an :class:`InvokeResult` to return or an exception to raise.
    ``sleep_s`` is spent inside every call, so elapsed time is observable.
    Running past the script fails the test loudly instead of inventing a reply.
    """

    def __init__(self, *script, sleep_s: float = 0.0):
        self._script = list(script)
        self._sleep_s = sleep_s
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=None):
        with self._lock:
            self.calls.append({
                "system": system, "user": user,
                "max_tokens": max_tokens, "json_mode": json_mode,
            })
            if not self._script:
                raise AssertionError(f"unscripted call #{len(self.calls)}")
            step = self._script.pop(0)
        if self._sleep_s:
            time.sleep(self._sleep_s)
        if isinstance(step, BaseException):
            raise step
        return step


def _chunk():
    return parse_unified_diff(MINI_DIFF)


def _retry_warnings(caplog):
    return [r for r in caplog.records if RETRY_NOTE in r.getMessage()]


class TestEmptyThenValid:
    def test_the_findings_of_the_second_reply_are_returned(self, caplog):
        llm = ScriptedLLM(
            reply("", input_tokens=100, output_tokens=7, model="first-model"),
            reply(VALID, input_tokens=30, output_tokens=50, model="second-model"),
        )
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = review_chunk(llm, _chunk(), max_tokens=900)
        assert [f.title for f in findings] == ["Unused import"]
        assert meta["error"] == ""
        assert len(llm.calls) == 2
        assert meta["input_tokens"] == 130
        assert meta["output_tokens"] == 57
        assert meta["model"] == "second-model"
        assert [r.getMessage() for r in _retry_warnings(caplog)] == [
            "chunk of 1 files: empty model reply (finish_reason=stop); retrying once",
        ]

    def test_the_retry_sends_the_same_prompt_and_the_same_budget(self):
        llm = ScriptedLLM(reply(""), reply(VALID))
        review_chunk(llm, _chunk(), max_tokens=900)
        first, second = llm.calls
        assert second == first
        assert first["max_tokens"] == 900
        assert first["json_mode"] is True

    def test_elapsed_ms_covers_both_calls(self):
        """Each call sleeps 25 ms; one call alone could not reach 45 ms."""
        llm = ScriptedLLM(reply(""), reply(VALID), sleep_s=0.025)
        _findings, meta = review_chunk(llm, _chunk())
        assert meta["elapsed_ms"] >= 45

    def test_an_unreported_finish_reason_is_retried_and_logged_as_a_dash(self, caplog):
        llm = ScriptedLLM(reply("", finish_reason=""), reply(VALID))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, _meta = review_chunk(llm, _chunk())
        assert len(findings) == 1
        assert len(llm.calls) == 2
        assert "(finish_reason=-)" in _retry_warnings(caplog)[0].getMessage()


class TestWhitespaceOnlyIsEmpty:
    @pytest.mark.parametrize("blank", [" ", "\n", "  \n\t  "])
    def test_a_whitespace_only_reply_is_retried(self, blank):
        llm = ScriptedLLM(reply(blank), reply(VALID))
        findings, meta = review_chunk(llm, _chunk())
        assert len(llm.calls) == 2
        assert len(findings) == 1
        assert meta["error"] == ""


class TestEmptyThenEmpty:
    def test_the_unit_fails_with_todays_error_and_both_calls_counted(self, caplog):
        llm = ScriptedLLM(
            reply("", input_tokens=100, output_tokens=7),
            reply("", input_tokens=30, output_tokens=5),
        )
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = review_chunk(llm, _chunk())
        assert findings == []
        assert meta["error"] == TODAYS_EMPTY_ERROR
        assert len(llm.calls) == 2
        assert meta["input_tokens"] == 130
        assert meta["output_tokens"] == 12
        assert len(_retry_warnings(caplog)) == 1


class TestTruncationIsNotRetried:
    @pytest.mark.parametrize("reason", ["length", "max_tokens", " LENGTH "])
    def test_an_empty_reply_at_the_budget_is_one_call_and_the_budget_error(self, reason, caplog):
        llm = ScriptedLLM(reply("", finish_reason=reason))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = review_chunk(llm, _chunk(), max_tokens=512)
        assert findings == []
        assert len(llm.calls) == 1
        assert meta["error"] == (
            f"response truncated at max_tokens=512 (finish_reason={reason.strip()}); "
            "raise PRXREF_LLM_MAX_TOKENS"
        )
        assert _retry_warnings(caplog) == []


class TestNonEmptyFailuresAreNotRetried:
    def test_non_empty_garbage_is_one_call_and_todays_parse_error(self, caplog):
        llm = ScriptedLLM(reply("I am not returning JSON."))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = review_chunk(llm, _chunk())
        assert findings == []
        assert len(llm.calls) == 1
        assert meta["error"] == TODAYS_GARBAGE_ERROR
        assert _retry_warnings(caplog) == []

    def test_a_raising_call_is_one_call(self):
        llm = ScriptedLLM(RuntimeError("upstream down"))
        findings, meta = review_chunk(llm, _chunk())
        assert findings == []
        assert len(llm.calls) == 1
        assert meta["error"] == "RuntimeError: upstream down"
        assert meta["input_tokens"] == 0
        assert meta["model"] == ""


class TestTheRetryRaising:
    def test_the_unit_fails_with_the_exception_and_keeps_the_first_calls_usage(self):
        llm = ScriptedLLM(
            reply("", input_tokens=100, output_tokens=7, model="first-model",
                  cost_usd=0.25, cost_source="usage.cost"),
            RuntimeError("m: timeout (ReadTimeout)"),
        )
        findings, meta = review_chunk(llm, _chunk())
        assert findings == []
        assert len(llm.calls) == 2
        assert meta["error"] == "RuntimeError: m: timeout (ReadTimeout)"
        assert meta["input_tokens"] == 100
        assert meta["output_tokens"] == 7
        assert meta["model"] == "first-model"
        assert meta["cost_usd"] == 0.25
        assert meta["cost_source"] == "usage.cost"

    def test_its_trace_records_no_response(self, tmp_path):
        llm = ScriptedLLM(reply(""), RuntimeError("endpoint down"))
        review_chunk(llm, _chunk(), trace_dir=str(tmp_path), trace_label="chunk0")
        assert json.loads((tmp_path / "chunk0.response.json").read_text()) is None
        traced = json.loads((tmp_path / "chunk0.meta.json").read_text())
        assert traced["error"] == "RuntimeError: endpoint down"
        assert traced["input_tokens"] == 100


class TestCostAccounting:
    def test_two_reported_figures_are_summed_and_the_second_source_kept(self):
        llm = ScriptedLLM(
            reply("", cost_usd=0.25, cost_source="usage.cost"),
            reply(VALID, cost_usd=0.5, cost_source="x-litellm-response-cost"),
        )
        _findings, meta = review_chunk(llm, _chunk())
        assert meta["cost_usd"] == 0.75
        assert meta["cost_source"] == "x-litellm-response-cost"

    @pytest.mark.parametrize("first,second", [(None, 0.5), (0.25, None), (None, None)])
    def test_one_unreported_figure_makes_the_unit_cost_unknown(self, first, second):
        llm = ScriptedLLM(
            reply("", cost_usd=first, cost_source="usage.cost" if first is not None else ""),
            reply(VALID, cost_usd=second, cost_source="usage.cost" if second is not None else ""),
        )
        _findings, meta = review_chunk(llm, _chunk())
        assert meta["cost_usd"] is None
        assert meta["cost_source"] == ""

    def test_a_single_call_reports_its_cost_as_before(self):
        llm = ScriptedLLM(reply(VALID, cost_usd=0.5, cost_source="usage.cost"))
        _findings, meta = review_chunk(llm, _chunk())
        assert meta["cost_usd"] == 0.5
        assert meta["cost_source"] == "usage.cost"


class TestTheTraceDescribesTheFinalAttempt:
    def test_four_files_holding_the_second_reply_and_both_calls_tokens(self, tmp_path):
        llm = ScriptedLLM(
            reply("", input_tokens=100, output_tokens=7),
            reply(VALID, input_tokens=30, output_tokens=50),
        )
        review_chunk(llm, _chunk(), trace_dir=str(tmp_path), trace_label="chunk0")
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "chunk0.meta.json", "chunk0.response.json", "chunk0.system.md", "chunk0.user.md",
        ]
        assert json.loads((tmp_path / "chunk0.response.json").read_text()) == VALID
        traced = json.loads((tmp_path / "chunk0.meta.json").read_text())
        assert traced["input_tokens"] == 130
        assert traced["output_tokens"] == 57
        assert traced["error"] == ""


class TestTheSweep:
    def test_review_systemic_retries_an_empty_reply_and_gets_its_findings(self, caplog):
        llm = ScriptedLLM(reply(""), reply(VALID))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = review_systemic(llm, "the whole-pr digest")
        assert [f.title for f in findings] == ["Unused import"]
        assert meta["error"] == ""
        assert len(llm.calls) == 2
        assert meta["input_tokens"] == 200
        assert [r.getMessage() for r in _retry_warnings(caplog)] == [
            "systemic sweep: empty model reply (finish_reason=stop); retrying once",
        ]


CHUNK_FINDING = json.dumps({
    "findings": [
        {
            "file": "src/app.py", "line": 3, "severity": "error", "confidence": 0.9,
            "title": "Null deref",
            "body": "x may be None when config is missing; data loss follows.",
        },
    ],
})


class TestThroughTheOrchestrator:
    def test_a_chunk_whose_first_reply_is_empty_keeps_its_findings_and_both_calls_tokens(self):
        """One chunk, one worker: the calls are the chunk, its retry, then the sweep."""
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        llm = ScriptedLLM(
            reply(""),
            reply(CHUNK_FINDING),
            reply(json.dumps({"findings": []})),
        )
        res = orchestrate_review(forge, REF, llm, post=False, max_workers=1)
        assert len(llm.calls) == 3
        assert llm.calls[0] == llm.calls[1]
        assert llm.calls[2]["system"] != llm.calls[0]["system"]
        assert [f.title for f in res["findings_active"]] == ["Null deref"]
        assert res["chunks_failed"] == 0
        assert res["input_tokens"] == 300
        assert res["output_tokens"] == 150
