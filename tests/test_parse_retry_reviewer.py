"""A reply that cannot be used as a review is asked for again (#21).

``parse_retries`` (N) is one budget shared by every kind of unusable reply:
empty, unparseable, not a JSON object, and, at N of 1 or more, an object
without a ``findings`` list. The same request is sent again while fewer than
N retries have run; an empty reply keeps its single retry at N=0. A reply the
provider stopped at the budget, and a call that raised, are never retried.
N=0, the library default, behaves exactly as 0.16.0 did.

Every behaviour is checked through both reviewers, ``review_chunk`` and
``review_systemic``, which share one parse path.
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from prxref.llm import InvokeResult
from prxref.reviewer import _write_trace_files, review_chunk, review_systemic
from prxref.triage import parse_unified_diff

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

GARBAGE = "I am not returning JSON."
GARBAGE_ERROR = "JSONDecodeError: Expecting value: line 1 column 1 (char 0)"
CUT_OFF = '{"findings": ['
CUT_OFF_ERROR = "JSONDecodeError: Expecting value: line 1 column 15 (char 14)"
STRAY_QUOTE = '{"findings":[{"file":"src/app.py","line":72","title":"x"}]}'
STRAY_QUOTE_ERROR = "JSONDecodeError: Expecting ',' delimiter: line 1 column 44 (char 43)"
EMPTY_ERROR = "JSONDecodeError: no parseable content: line 1 column 1 (char 0)"
NO_FINDINGS_ERROR = "worker review JSON has no findings list"
LIST_ERROR = "worker review JSON is not an object: list"
BUDGET_ERROR = (
    "response truncated at max_tokens=512 (finish_reason=length); "
    "raise PRXREF_LLM_MAX_TOKENS"
)

BASE_META_KEYS = [
    "escalations", "input_tokens", "output_tokens", "model", "elapsed_ms",
    "error", "cost_usd", "cost_source",
]
BASE_META_JSON_KEYS = [
    "unit", "model", "input_tokens", "output_tokens", "elapsed_ms",
    "error", "cost_usd", "cost_source",
]
RETRY_KEYS = ["parse_retries", "first_error"]


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
    Running past the script fails the test loudly instead of inventing a reply.
    """

    def __init__(self, *script):
        self._script = list(script)
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
        if isinstance(step, BaseException):
            raise step
        return step


@dataclass(frozen=True)
class Unit:
    """One reviewer entry point, with the names it goes by in traces and logs."""

    review: Callable[..., tuple]
    trace_label: str
    log_label: str


def _review_chunk(llm, **kwargs):
    return review_chunk(llm, parse_unified_diff(MINI_DIFF), **kwargs)


def _review_sweep(llm, **kwargs):
    return review_systemic(llm, "the whole-pr digest", **kwargs)


@pytest.fixture(params=[
    pytest.param(Unit(_review_chunk, "chunk0", "chunk of 1 files"), id="chunk"),
    pytest.param(Unit(_review_sweep, "sweep", "systemic sweep"), id="sweep"),
])
def unit(request) -> Unit:
    return request.param


def _traced(unit: Unit, llm, tmp_path, **kwargs):
    return unit.review(llm, trace_dir=str(tmp_path), trace_label=unit.trace_label, **kwargs)


def _files(tmp_path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir())


def _base_files(label: str) -> list[str]:
    return sorted(f"{label}{s}" for s in (".system.md", ".user.md", ".response.json", ".meta.json"))


def _response(tmp_path, name: str):
    return json.loads((tmp_path / name).read_text(encoding="utf-8"))


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


class TestMalformedThenValid:
    def test_two_calls_and_the_second_replys_findings(self, unit):
        llm = ScriptedLLM(reply(GARBAGE), reply(VALID))
        findings, meta = unit.review(llm, parse_retries=1)
        assert len(llm.calls) == 2
        assert [f.title for f in findings] == ["Unused import"]
        assert meta["error"] == ""
        assert meta["parse_retries"] == 1
        assert meta["first_error"] == GARBAGE_ERROR

    def test_the_retry_resends_the_same_request(self, unit):
        llm = ScriptedLLM(reply(GARBAGE), reply(VALID))
        unit.review(llm, parse_retries=1, max_tokens=900)
        first, second = llm.calls
        assert second == first
        assert first["max_tokens"] == 900
        assert first["json_mode"] is True

    def test_meta_gains_the_two_keys_after_the_eight_base_keys(self, unit):
        llm = ScriptedLLM(reply(GARBAGE), reply(VALID))
        _findings, meta = unit.review(llm, parse_retries=1)
        assert list(meta) == BASE_META_KEYS + RETRY_KEYS

    def test_the_trace_keeps_both_replies(self, unit, tmp_path):
        llm = ScriptedLLM(reply(GARBAGE), reply(VALID))
        _traced(unit, llm, tmp_path, parse_retries=1)
        label = unit.trace_label
        assert _files(tmp_path) == sorted(
            _base_files(label) + [f"{label}.attempt1.response.json"],
        )
        assert _response(tmp_path, f"{label}.response.json") == VALID
        assert _response(tmp_path, f"{label}.attempt1.response.json") == GARBAGE

    def test_meta_json_records_the_retry_after_its_eight_keys(self, unit, tmp_path):
        llm = ScriptedLLM(reply(GARBAGE), reply(VALID))
        _traced(unit, llm, tmp_path, parse_retries=1)
        traced = _response(tmp_path, f"{unit.trace_label}.meta.json")
        assert list(traced) == BASE_META_JSON_KEYS + RETRY_KEYS
        assert traced["parse_retries"] == 1
        assert traced["first_error"] == GARBAGE_ERROR
        assert traced["error"] == ""

    def test_one_warning_names_the_error_and_the_count(self, unit, caplog):
        llm = ScriptedLLM(reply(GARBAGE), reply(VALID))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            unit.review(llm, parse_retries=1)
        assert _warnings(caplog) == [
            f"{unit.log_label}: unusable model reply ({GARBAGE_ERROR}); parse retry 1 of 1",
        ]


class TestMalformedTwice:
    def test_two_calls_and_the_last_error(self, unit):
        assert CUT_OFF_ERROR != GARBAGE_ERROR
        llm = ScriptedLLM(reply(GARBAGE), reply(CUT_OFF))
        findings, meta = unit.review(llm, parse_retries=1)
        assert len(llm.calls) == 2
        assert findings == []
        assert meta["error"] == CUT_OFF_ERROR
        assert meta["first_error"] == GARBAGE_ERROR
        assert meta["parse_retries"] == 1

    def test_the_failure_is_logged_as_in_0_16(self, unit, caplog):
        llm = ScriptedLLM(reply(GARBAGE), reply(CUT_OFF))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            unit.review(llm, parse_retries=1)
        assert _warnings(caplog)[-1] == f"worker review failed for {unit.log_label}: {CUT_OFF_ERROR}"

    def test_the_trace_uses_the_last_reply_and_keeps_the_first(self, unit, tmp_path):
        llm = ScriptedLLM(reply(GARBAGE), reply(CUT_OFF))
        _traced(unit, llm, tmp_path, parse_retries=1)
        label = unit.trace_label
        assert _response(tmp_path, f"{label}.response.json") == CUT_OFF
        assert _response(tmp_path, f"{label}.attempt1.response.json") == GARBAGE
        traced = _response(tmp_path, f"{label}.meta.json")
        assert traced["error"] == CUT_OFF_ERROR
        assert traced["first_error"] == GARBAGE_ERROR
        assert traced["parse_retries"] == 1

    def test_a_stray_quote_inside_the_object_is_retried(self, unit):
        llm = ScriptedLLM(reply(STRAY_QUOTE), reply(VALID))
        findings, meta = unit.review(llm, parse_retries=1)
        assert len(llm.calls) == 2
        assert len(findings) == 1
        assert meta["first_error"] == STRAY_QUOTE_ERROR


class TestWrongShape:
    @pytest.mark.parametrize("text,error", [
        pytest.param('[{"file": "src/app.py", "line": 2}]', LIST_ERROR, id="list"),
        pytest.param("null", "worker review JSON is not an object: NoneType", id="null"),
        pytest.param("{}", NO_FINDINGS_ERROR, id="empty-object"),
        pytest.param('{"escalations": []}', NO_FINDINGS_ERROR, id="no-findings-key"),
        pytest.param('{"findings": "none"}', NO_FINDINGS_ERROR, id="findings-not-a-list"),
        pytest.param('{"findings": null}', NO_FINDINGS_ERROR, id="findings-null"),
    ])
    def test_is_retried_like_a_parse_failure(self, unit, text, error):
        llm = ScriptedLLM(reply(text), reply(VALID))
        findings, meta = unit.review(llm, parse_retries=1)
        assert len(llm.calls) == 2
        assert [f.title for f in findings] == ["Unused import"]
        assert meta["error"] == ""
        assert meta["parse_retries"] == 1
        assert meta["first_error"] == error

    def test_an_object_without_findings_twice_fails_the_unit(self, unit, caplog):
        llm = ScriptedLLM(reply("{}"), reply('{"escalations": []}'))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = unit.review(llm, parse_retries=1)
        assert len(llm.calls) == 2
        assert findings == []
        assert meta["error"] == NO_FINDINGS_ERROR
        assert meta["first_error"] == NO_FINDINGS_ERROR
        assert _warnings(caplog)[-1] == NO_FINDINGS_ERROR

    def test_a_list_twice_fails_the_unit_with_the_shape_error(self, unit):
        llm = ScriptedLLM(reply("[]"), reply("[1, 2]"))
        findings, meta = unit.review(llm, parse_retries=1)
        assert len(llm.calls) == 2
        assert findings == []
        assert meta["error"] == LIST_ERROR

    def test_an_empty_findings_list_is_a_clean_review_in_one_call(self, unit):
        llm = ScriptedLLM(reply('{"findings": []}'))
        findings, meta = unit.review(llm, parse_retries=1)
        assert len(llm.calls) == 1
        assert findings == []
        assert list(meta) == BASE_META_KEYS
        assert meta["error"] == ""


class TestTheBudgetStopIsNeverRetried:
    @pytest.mark.parametrize("text", [
        pytest.param(CUT_OFF, id="unparseable"),
        pytest.param("", id="empty"),
        pytest.param("[1]", id="list"),
        pytest.param("{}", id="no-findings"),
    ])
    def test_one_call_and_the_budget_message(self, unit, text, tmp_path):
        llm = ScriptedLLM(reply(text, finish_reason="length"))
        findings, meta = _traced(unit, llm, tmp_path, parse_retries=3, max_tokens=512)
        assert len(llm.calls) == 1
        assert findings == []
        assert meta["error"] == BUDGET_ERROR
        assert list(meta) == BASE_META_KEYS
        assert _files(tmp_path) == _base_files(unit.trace_label)

    def test_a_retry_stopped_at_the_budget_ends_the_retries(self, unit):
        llm = ScriptedLLM(reply(GARBAGE), reply(CUT_OFF, finish_reason="length"))
        findings, meta = unit.review(llm, parse_retries=3, max_tokens=512)
        assert len(llm.calls) == 2
        assert findings == []
        assert meta["error"] == BUDGET_ERROR
        assert meta["parse_retries"] == 1
        assert meta["first_error"] == GARBAGE_ERROR

    def test_a_truncated_usable_reply_is_kept_without_a_retry(self, unit):
        llm = ScriptedLLM(reply(VALID, finish_reason="length"))
        findings, meta = unit.review(llm, parse_retries=1)
        assert len(llm.calls) == 1
        assert len(findings) == 1
        assert meta["error"] == ""


class TestARaisingCallIsNeverRetried:
    def test_a_first_call_that_raises_is_one_call(self, unit, tmp_path):
        llm = ScriptedLLM(RuntimeError("upstream down"))
        findings, meta = _traced(unit, llm, tmp_path, parse_retries=3)
        assert len(llm.calls) == 1
        assert findings == []
        assert meta["error"] == "RuntimeError: upstream down"
        assert list(meta) == BASE_META_KEYS
        assert _files(tmp_path) == _base_files(unit.trace_label)

    def test_a_retry_that_raises_fails_the_unit_and_keeps_the_first_reply(self, unit, tmp_path):
        llm = ScriptedLLM(
            reply(GARBAGE, input_tokens=100, output_tokens=7, model="first-model"),
            RuntimeError("m: timeout (ReadTimeout)"),
        )
        findings, meta = _traced(unit, llm, tmp_path, parse_retries=3)
        label = unit.trace_label
        assert len(llm.calls) == 2
        assert findings == []
        assert meta["error"] == "RuntimeError: m: timeout (ReadTimeout)"
        assert meta["input_tokens"] == 100
        assert meta["output_tokens"] == 7
        assert meta["model"] == "first-model"
        assert meta["parse_retries"] == 1
        assert meta["first_error"] == GARBAGE_ERROR
        assert _response(tmp_path, f"{label}.response.json") is None
        assert _response(tmp_path, f"{label}.attempt1.response.json") == GARBAGE


class TestOneSharedBudget:
    def test_an_empty_reply_draws_on_the_same_budget(self, unit, caplog):
        llm = ScriptedLLM(reply(""), reply(GARBAGE))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = unit.review(llm, parse_retries=1)
        assert len(llm.calls) == 2
        assert findings == []
        assert meta["error"] == GARBAGE_ERROR
        assert meta["parse_retries"] == 1
        assert meta["first_error"] == EMPTY_ERROR
        assert _warnings(caplog)[0] == (
            f"{unit.log_label}: empty model reply (finish_reason=stop); parse retry 1 of 1"
        )

    def test_empty_twice_is_two_calls_and_the_empty_error(self, unit, tmp_path):
        llm = ScriptedLLM(reply(""), reply(""))
        findings, meta = _traced(unit, llm, tmp_path, parse_retries=1)
        assert len(llm.calls) == 2
        assert findings == []
        assert meta["error"] == EMPTY_ERROR
        assert _response(tmp_path, f"{unit.trace_label}.attempt1.response.json") == ""

    def test_an_empty_reply_after_a_malformed_one_is_not_a_third_call(self, unit):
        llm = ScriptedLLM(reply(GARBAGE), reply(""))
        findings, meta = unit.review(llm, parse_retries=1)
        assert len(llm.calls) == 2
        assert meta["error"] == EMPTY_ERROR
        assert meta["first_error"] == GARBAGE_ERROR

    def test_n_of_two_allows_three_calls_and_keeps_every_discarded_reply(self, unit, tmp_path, caplog):
        llm = ScriptedLLM(reply(GARBAGE), reply("{}"), reply(CUT_OFF))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = _traced(unit, llm, tmp_path, parse_retries=2)
        label = unit.trace_label
        assert len(llm.calls) == 3
        assert findings == []
        assert meta["error"] == CUT_OFF_ERROR
        assert meta["first_error"] == GARBAGE_ERROR
        assert meta["parse_retries"] == 2
        assert _files(tmp_path) == sorted(_base_files(label) + [
            f"{label}.attempt1.response.json", f"{label}.attempt2.response.json",
        ])
        assert _response(tmp_path, f"{label}.attempt1.response.json") == GARBAGE
        assert _response(tmp_path, f"{label}.attempt2.response.json") == "{}"
        assert _response(tmp_path, f"{label}.response.json") == CUT_OFF
        retry_lines = [m for m in _warnings(caplog) if "parse retry" in m]
        assert retry_lines == [
            f"{unit.log_label}: unusable model reply ({GARBAGE_ERROR}); parse retry 1 of 2",
            f"{unit.log_label}: unusable model reply ({NO_FINDINGS_ERROR}); parse retry 2 of 2",
        ]

    def test_n_of_two_succeeds_on_the_third_call(self, unit):
        llm = ScriptedLLM(reply("[]"), reply(""), reply(VALID))
        findings, meta = unit.review(llm, parse_retries=2)
        assert len(llm.calls) == 3
        assert len(findings) == 1
        assert meta["error"] == ""
        assert meta["parse_retries"] == 2
        assert meta["first_error"] == LIST_ERROR


class TestUsageCoversEveryAttempt:
    def test_tokens_are_summed_and_the_model_is_the_last_calls(self, unit):
        llm = ScriptedLLM(
            reply(GARBAGE, input_tokens=100, output_tokens=7, model="first-model"),
            reply("{}", input_tokens=30, output_tokens=5, model="second-model"),
            reply(VALID, input_tokens=20, output_tokens=50, model="third-model"),
        )
        _findings, meta = unit.review(llm, parse_retries=2)
        assert meta["input_tokens"] == 150
        assert meta["output_tokens"] == 62
        assert meta["model"] == "third-model"

    def test_reported_costs_are_summed_and_the_last_source_kept(self, unit):
        llm = ScriptedLLM(
            reply(GARBAGE, cost_usd=0.25, cost_source="usage.cost"),
            reply(VALID, cost_usd=0.5, cost_source="x-litellm-response-cost"),
        )
        _findings, meta = unit.review(llm, parse_retries=1)
        assert meta["cost_usd"] == 0.75
        assert meta["cost_source"] == "x-litellm-response-cost"

    def test_one_unreported_cost_makes_the_unit_cost_unknown(self, unit):
        llm = ScriptedLLM(
            reply(GARBAGE, cost_usd=0.25, cost_source="usage.cost"),
            reply("{}"),
            reply(VALID, cost_usd=0.5, cost_source="usage.cost"),
        )
        _findings, meta = unit.review(llm, parse_retries=2)
        assert meta["cost_usd"] is None
        assert meta["cost_source"] == ""

    def test_the_trace_meta_carries_the_summed_tokens(self, unit, tmp_path):
        llm = ScriptedLLM(
            reply(GARBAGE, input_tokens=100, output_tokens=7),
            reply(VALID, input_tokens=30, output_tokens=50),
        )
        _traced(unit, llm, tmp_path, parse_retries=1)
        traced = _response(tmp_path, f"{unit.trace_label}.meta.json")
        assert traced["input_tokens"] == 130
        assert traced["output_tokens"] == 57


class TestZeroIsTheOldBehaviour:
    @pytest.fixture(params=[
        pytest.param({}, id="default"),
        pytest.param({"parse_retries": 0}, id="explicit-0"),
    ])
    def zero(self, request) -> dict:
        return request.param

    def test_garbage_is_one_call_and_the_parse_error(self, unit, zero, caplog):
        llm = ScriptedLLM(reply(GARBAGE))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = unit.review(llm, **zero)
        assert len(llm.calls) == 1
        assert findings == []
        assert meta["error"] == GARBAGE_ERROR
        assert list(meta) == BASE_META_KEYS
        assert _warnings(caplog) == [f"worker review failed for {unit.log_label}: {GARBAGE_ERROR}"]

    def test_an_object_without_findings_is_a_clean_review(self, unit, zero):
        llm = ScriptedLLM(reply("{}"))
        findings, meta = unit.review(llm, **zero)
        assert len(llm.calls) == 1
        assert findings == []
        assert meta["error"] == ""
        assert list(meta) == BASE_META_KEYS

    def test_a_list_is_one_call_and_the_shape_error(self, unit, zero):
        llm = ScriptedLLM(reply("[]"))
        findings, meta = unit.review(llm, **zero)
        assert len(llm.calls) == 1
        assert meta["error"] == LIST_ERROR

    def test_the_trace_is_four_files_with_eight_meta_keys(self, unit, zero, tmp_path):
        llm = ScriptedLLM(reply(GARBAGE))
        _traced(unit, llm, tmp_path, **zero)
        assert _files(tmp_path) == _base_files(unit.trace_label)
        traced = _response(tmp_path, f"{unit.trace_label}.meta.json")
        assert list(traced) == BASE_META_JSON_KEYS

    def test_the_empty_retry_keeps_its_one_call_log_and_writes_no_attempt(self, unit, zero, tmp_path, caplog):
        llm = ScriptedLLM(
            reply("", input_tokens=100, output_tokens=7),
            reply(VALID, input_tokens=30, output_tokens=50),
        )
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = _traced(unit, llm, tmp_path, **zero)
        assert len(llm.calls) == 2
        assert len(findings) == 1
        assert list(meta) == BASE_META_KEYS
        assert meta["input_tokens"] == 130
        assert _files(tmp_path) == _base_files(unit.trace_label)
        assert list(_response(tmp_path, f"{unit.trace_label}.meta.json")) == BASE_META_JSON_KEYS
        assert _warnings(caplog) == [
            f"{unit.log_label}: empty model reply (finish_reason=stop); retrying once",
        ]

    def test_empty_twice_is_still_two_calls_and_the_empty_error(self, unit, zero):
        llm = ScriptedLLM(reply(""), reply(""))
        findings, meta = unit.review(llm, **zero)
        assert len(llm.calls) == 2
        assert meta["error"] == EMPTY_ERROR
        assert list(meta) == BASE_META_KEYS

    def test_an_empty_reply_then_an_object_without_findings_is_clean(self, unit, zero):
        llm = ScriptedLLM(reply(""), reply("{}"))
        findings, meta = unit.review(llm, **zero)
        assert len(llm.calls) == 2
        assert findings == []
        assert meta["error"] == ""


class TestStaleAttemptFiles:
    def test_a_later_write_of_the_same_label_removes_attempts_it_did_not_make(self, unit, tmp_path):
        label = unit.trace_label
        _traced(unit, ScriptedLLM(reply(GARBAGE), reply("{}"), reply(VALID)), tmp_path, parse_retries=2)
        assert f"{label}.attempt2.response.json" in _files(tmp_path)
        _traced(unit, ScriptedLLM(reply(VALID)), tmp_path, parse_retries=2)
        assert _files(tmp_path) == _base_files(label)
        assert "parse_retries" not in _response(tmp_path, f"{label}.meta.json")

    def test_only_the_attempts_beyond_the_new_count_are_removed(self, unit, tmp_path):
        label = unit.trace_label
        _traced(unit, ScriptedLLM(reply(GARBAGE), reply("{}"), reply(VALID)), tmp_path, parse_retries=2)
        _traced(unit, ScriptedLLM(reply(CUT_OFF), reply(VALID)), tmp_path, parse_retries=2)
        assert _files(tmp_path) == sorted(_base_files(label) + [f"{label}.attempt1.response.json"])
        assert _response(tmp_path, f"{label}.attempt1.response.json") == CUT_OFF


class TestWriteTraceFilesKeepsItsSignature:
    def test_the_positional_call_writes_four_files_and_eight_meta_keys(self, tmp_path):
        meta = {"model": "m", "input_tokens": 1, "output_tokens": 2, "elapsed_ms": 3, "error": ""}
        _write_trace_files(str(tmp_path), "judge", "sys", "usr", "raw", meta)
        assert _files(tmp_path) == _base_files("judge")
        assert list(_response(tmp_path, "judge.meta.json")) == BASE_META_JSON_KEYS

    def test_retry_keys_in_meta_and_attempts_are_written(self, tmp_path):
        meta = {"model": "m", "error": "", "parse_retries": 2, "first_error": "boom"}
        _write_trace_files(str(tmp_path), "judge", "sys", "usr", "raw", meta, attempts=["a", None])
        assert _files(tmp_path) == sorted(_base_files("judge") + [
            "judge.attempt1.response.json", "judge.attempt2.response.json",
        ])
        traced = _response(tmp_path, "judge.meta.json")
        assert list(traced) == BASE_META_JSON_KEYS + RETRY_KEYS
        assert traced["parse_retries"] == 2 and traced["first_error"] == "boom"
        assert _response(tmp_path, "judge.attempt1.response.json") == "a"
        assert _response(tmp_path, "judge.attempt2.response.json") is None

    def test_an_empty_trace_dir_still_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_trace_files("", "judge", "sys", "usr", "raw", {}, attempts=["a"])
        assert _files(tmp_path) == []
