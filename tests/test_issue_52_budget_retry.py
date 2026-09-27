"""A reply stopped at the completion budget before it was usable is retried once (#52).

A reasoning model can spend the whole ``max_tokens`` budget on hidden
reasoning and return an empty or cut-off reply with ``finish_reason=length``.
The reviewer now sends that request once more at double the budget, capped at
:data:`prxref.reviewer.TRUNCATION_RETRY_MAX_TOKENS`. A usable truncated reply
is kept without a retry, a budget already at the cap is not retried, and a
normal reply makes exactly one call with the meta keys it always had.

Every behaviour is checked through both callers of the shared parse path,
``review_chunk`` and ``review_systemic``.
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from prxref.llm import InvokeResult
from prxref.reviewer import TRUNCATION_RETRY_MAX_TOKENS, review_chunk, review_systemic
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
CUT_OFF = '{"findings": [{"file": "src/app.py", "line": 2, "sev'

BASE_META_KEYS = [
    "escalations", "input_tokens", "output_tokens", "model", "elapsed_ms",
    "error", "cost_usd", "cost_source",
]


def _budget_error(budget: int, reason: str = "length") -> str:
    return (
        f"response truncated at max_tokens={budget} (finish_reason={reason}); "
        "raise PRXREF_LLM_MAX_TOKENS"
    )


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

    @property
    def budgets(self) -> list[int]:
        return [c["max_tokens"] for c in self.calls]


@dataclass(frozen=True)
class Unit:
    """One reviewer entry point, with the name it goes by in log lines."""

    review: Callable[..., tuple]
    log_label: str


def _review_chunk(llm, **kwargs):
    return review_chunk(llm, parse_unified_diff(MINI_DIFF), **kwargs)


def _review_sweep(llm, **kwargs):
    return review_systemic(llm, "the whole-pr digest", **kwargs)


@pytest.fixture(params=[
    pytest.param(Unit(_review_chunk, "chunk of 1 files"), id="chunk"),
    pytest.param(Unit(_review_sweep, "systemic sweep"), id="sweep"),
])
def unit(request) -> Unit:
    return request.param


def _budget_warnings(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING and "retrying once at max_tokens=" in r.getMessage()
    ]


def _retry_note(label: str, budget: int, larger: int, reason: str = "length") -> str:
    return (
        f"{label}: reply stopped at the completion budget (max_tokens={budget}, "
        f"finish_reason={reason}) before it was usable; retrying once at max_tokens={larger}"
    )


class TestTheCeiling:
    def test_the_ceiling_is_16384(self):
        assert TRUNCATION_RETRY_MAX_TOKENS == 16384


class TestTruncatedThenGood:
    @pytest.mark.parametrize("first", [
        pytest.param("", id="empty"),
        pytest.param(CUT_OFF, id="unparseable"),
    ])
    def test_the_retry_at_double_the_budget_keeps_the_findings(self, unit, first, caplog):
        llm = ScriptedLLM(
            reply(first, finish_reason="length", input_tokens=100, output_tokens=4096,
                  model="first-model", cost_usd=0.25, cost_source="usage.cost"),
            reply(VALID, input_tokens=100, output_tokens=900, model="second-model",
                  cost_usd=0.5, cost_source="usage.cost"),
        )
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = unit.review(llm, max_tokens=4096)
        assert llm.budgets == [4096, 8192]
        assert [f.title for f in findings] == ["Unused import"]
        assert meta["error"] == ""
        assert meta["budget_retry"] == 8192
        assert list(meta) == [*BASE_META_KEYS, "budget_retry"]
        assert meta["input_tokens"] == 200
        assert meta["output_tokens"] == 4996
        assert meta["model"] == "second-model"
        assert meta["cost_usd"] == pytest.approx(0.75)
        assert meta["cost_source"] == "usage.cost"
        assert _budget_warnings(caplog) == [_retry_note(unit.log_label, 4096, 8192)]

    def test_the_retry_resends_the_same_prompt(self, unit):
        llm = ScriptedLLM(reply("", finish_reason="length"), reply(VALID))
        unit.review(llm, max_tokens=4096)
        first, second = llm.calls
        assert second["system"] == first["system"]
        assert second["user"] == first["user"]
        assert second["json_mode"] is True

    def test_the_reason_is_quoted_as_the_provider_spelled_it(self, unit, caplog):
        llm = ScriptedLLM(reply("", finish_reason=" MAX_TOKENS "), reply(VALID))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            unit.review(llm, max_tokens=1000)
        assert _budget_warnings(caplog) == [
            _retry_note(unit.log_label, 1000, 2000, reason="MAX_TOKENS"),
        ]

    def test_a_wrong_shape_at_the_budget_is_retried_too(self, unit):
        llm = ScriptedLLM(reply("[1]", finish_reason="length"), reply(VALID))
        findings, meta = unit.review(llm, max_tokens=4096)
        assert llm.budgets == [4096, 8192]
        assert len(findings) == 1
        assert meta["budget_retry"] == 8192


class TestUsableButTruncated:
    def test_no_retry_and_the_warning_is_unchanged(self, unit, caplog):
        llm = ScriptedLLM(reply(VALID, finish_reason="length"))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = unit.review(llm, max_tokens=4096)
        assert llm.budgets == [4096]
        assert len(findings) == 1
        assert meta["error"] == ""
        assert list(meta) == BASE_META_KEYS
        assert _budget_warnings(caplog) == []
        assert [r.getMessage() for r in caplog.records] == [
            f"worker review for {unit.log_label} hit the completion budget "
            "(max_tokens=4096, finish_reason=length); findings may be incomplete — "
            "raise PRXREF_LLM_MAX_TOKENS",
        ]


class TestTruncatedTwice:
    def test_two_calls_and_the_error_names_the_larger_budget(self, unit, caplog):
        llm = ScriptedLLM(
            reply("", finish_reason="length"), reply(CUT_OFF, finish_reason="length"),
        )
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = unit.review(llm, max_tokens=4096)
        assert llm.budgets == [4096, 8192]
        assert findings == []
        assert meta["error"] == _budget_error(8192)
        assert meta["budget_retry"] == 8192
        assert meta["input_tokens"] == 200
        assert len(_budget_warnings(caplog)) == 1

    def test_a_usable_but_truncated_retry_warns_with_the_larger_budget(self, unit, caplog):
        llm = ScriptedLLM(reply("", finish_reason="length"), reply(VALID, finish_reason="length"))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = unit.review(llm, max_tokens=4096)
        assert len(findings) == 1
        assert meta["error"] == ""
        assert "(max_tokens=8192, finish_reason=length); findings may be incomplete" in caplog.text


class TestTheRetryRaising:
    def test_the_error_keeps_the_original_budget_and_names_the_exception_type(self, unit, caplog):
        llm = ScriptedLLM(
            reply("", finish_reason="length", input_tokens=100, output_tokens=4096),
            RuntimeError("HTTP 400: max_tokens too large for this model"),
        )
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = unit.review(llm, max_tokens=4096)
        assert llm.budgets == [4096, 8192]
        assert findings == []
        assert meta["error"].startswith(_budget_error(4096))
        assert meta["error"] == (
            _budget_error(4096) + " (a retry at max_tokens=8192 failed: RuntimeError)"
        )
        assert "HTTP 400" not in meta["error"]
        assert meta["input_tokens"] == 100
        assert meta["output_tokens"] == 4096
        assert meta["budget_retry"] == 8192
        failed = [m for m in (r.getMessage() for r in caplog.records) if "failed" in m]
        assert failed == [
            f"worker review failed for {unit.log_label}: the retry at max_tokens=8192 "
            "raised RuntimeError: HTTP 400: max_tokens too large for this model",
        ]

    def test_a_later_parse_retry_that_raises_keeps_the_plain_exception(self, unit):
        llm = ScriptedLLM(
            reply("", finish_reason="length"),
            reply("not json"),
            RuntimeError("upstream down"),
        )
        findings, meta = unit.review(llm, max_tokens=4096, parse_retries=1)
        assert llm.budgets == [4096, 8192, 8192]
        assert findings == []
        assert meta["error"] == "RuntimeError: upstream down"


class TestTheCap:
    def test_a_budget_already_at_the_ceiling_is_not_retried(self, unit, caplog):
        llm = ScriptedLLM(reply("", finish_reason="length"))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = unit.review(llm, max_tokens=TRUNCATION_RETRY_MAX_TOKENS)
        assert llm.budgets == [TRUNCATION_RETRY_MAX_TOKENS]
        assert findings == []
        assert meta["error"] == _budget_error(TRUNCATION_RETRY_MAX_TOKENS)
        assert list(meta) == BASE_META_KEYS
        assert _budget_warnings(caplog) == []

    def test_a_budget_above_the_ceiling_is_not_retried(self, unit):
        llm = ScriptedLLM(reply("", finish_reason="length"))
        _findings, meta = unit.review(llm, max_tokens=32768)
        assert llm.budgets == [32768]
        assert meta["error"] == _budget_error(32768)

    def test_a_retry_that_would_pass_the_ceiling_goes_to_the_ceiling(self, unit, caplog):
        llm = ScriptedLLM(reply("", finish_reason="length"), reply(VALID))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            _findings, meta = unit.review(llm, max_tokens=12000)
        assert llm.budgets == [12000, TRUNCATION_RETRY_MAX_TOKENS]
        assert meta["budget_retry"] == TRUNCATION_RETRY_MAX_TOKENS
        assert _budget_warnings(caplog) == [
            _retry_note(unit.log_label, 12000, TRUNCATION_RETRY_MAX_TOKENS),
        ]


class TestParseRetriesAfterTheBudgetRetry:
    def test_the_worst_case_is_two_plus_the_parse_retries(self, unit):
        """Empty, then truncated, then unusable: one empty retry, one budget
        retry and, at N=2, a second parse retry: ``2 + max(N, 1)`` calls."""
        llm = ScriptedLLM(
            reply(""),
            reply("", finish_reason="length"),
            reply("not json"),
            reply("still not json"),
        )
        findings, meta = unit.review(llm, max_tokens=4096, parse_retries=2)
        assert llm.budgets == [4096, 4096, 8192, 8192]
        assert findings == []
        assert meta["parse_retries"] == 2
        assert meta["budget_retry"] == 8192
        assert meta["error"].startswith("JSONDecodeError")

    def test_the_discarded_reply_is_traced_as_an_attempt(self, unit, tmp_path):
        llm = ScriptedLLM(reply(CUT_OFF, finish_reason="length"), reply(VALID))
        label = "unit0"
        unit.review(
            llm, max_tokens=4096, parse_retries=1,
            trace_dir=str(tmp_path), trace_label=label,
        )
        attempt = tmp_path / f"{label}.attempt1.response.json"
        assert json.loads(attempt.read_text(encoding="utf-8")) == CUT_OFF
        assert json.loads((tmp_path / f"{label}.response.json").read_text(encoding="utf-8")) == VALID

    def test_at_zero_parse_retries_no_attempt_file_is_written(self, unit, tmp_path):
        llm = ScriptedLLM(reply(CUT_OFF, finish_reason="length"), reply(VALID))
        unit.review(
            llm, max_tokens=4096, parse_retries=0,
            trace_dir=str(tmp_path), trace_label="unit0",
        )
        assert not [p for p in tmp_path.iterdir() if ".attempt" in p.name]


class TestIdentity:
    @pytest.mark.parametrize("parse_retries", [0, 1])
    def test_a_normal_reply_is_one_call_with_the_base_meta_keys(self, unit, parse_retries, caplog):
        llm = ScriptedLLM(reply(VALID))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = unit.review(llm, max_tokens=4096, parse_retries=parse_retries)
        assert llm.budgets == [4096]
        assert len(findings) == 1
        assert list(meta) == BASE_META_KEYS
        assert "budget_retry" not in meta
        assert caplog.records == []

    def test_an_unusable_reply_that_is_not_truncated_keeps_the_same_budget(self, unit):
        llm = ScriptedLLM(reply("not json"), reply(VALID))
        _findings, meta = unit.review(llm, max_tokens=4096, parse_retries=1)
        assert llm.budgets == [4096, 4096]
        assert "budget_retry" not in meta
