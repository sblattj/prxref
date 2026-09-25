"""A judge reply the parser rejects is asked for again (#21).

``judge_case(..., parse_retries=N)`` sends the same request again while
fewer than N retries have run, whenever :func:`prxref.judge.parse_judge_response`
raises :class:`~prxref.judge.JudgeParseError`, whatever the reason: a
superset of the issue's three cases. A call that raised and a cache hit are
never retried, so a case makes at most 1 + N calls. Only the graded reply is
cached, every attempt's usage counts toward the case's tokens and cost, and
N=0, the library default, behaves exactly as 0.16.0 did.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import threading
from pathlib import Path

import pytest

import prxref.judge
from prxref import eval_judge
from prxref.costs import parse_price_table
from prxref.eval_cases import EvalCase, ExpectedFinding
from prxref.eval_judge import JudgeOutcome, judge_case, judge_cost
from prxref.judge import (
    Grade,
    JudgeParseError,
    ai_ref_files,
    assign_refs,
    human_files,
    judge_cache_key,
    judge_prompt_sha,
    parse_judge_response,
)
from prxref.llm import InvokeResult

JUDGE = "judge-m"
FALLBACK = "judge-m-fallback"

CASE = EvalCase(
    id="case-1",
    expected=(
        ExpectedFinding("H1", "src/a.py", 10, "error", text="Divides by zero when size is 0."),
        ExpectedFinding("H2", "src/b.py", 5, "warning", text="Token logged in clear."),
    ),
    diff_file="case-1.patch",
)

RECORD = {
    "findings": [
        {"file": "src/a.py", "line": 10, "severity": "error", "title": "Divide by zero",
         "body": "size may be 0.", "drop_reason": None},
        {"file": "src/b.py", "line": 5, "severity": "error", "title": "Secret in log",
         "body": "token is logged.", "drop_reason": None},
    ],
}

GOOD_REPLY = json.dumps({"grades": [
    {"human_id": "H1", "grade": "full", "ai_ref": "A1"},
    {"human_id": "H2", "grade": "partial", "ai_ref": "A2"},
]})
GOOD_GRADES = (Grade("H1", "full", "A1"), Grade("H2", "partial", "A2"))

NOT_JSON = "I think H1 is matched."
NOT_JSON_ERROR = "judge response rejected: judge response is not JSON: Expecting value: line 1 column 1 (char 0)"
BAD_GRADE = '{"grades": [{"human_id": "H1", "grade": "match", "ai_ref": "A1"}]}'
BAD_GRADE_ERROR = "judge response rejected: grades[0] has grade 'match', not one of full, partial, none"

KINDS = [
    pytest.param("", "judge response is empty", id="empty"),
    pytest.param(NOT_JSON, "judge response is not JSON: Expecting value: line 1 column 1 (char 0)", id="not-json"),
    pytest.param("[]", "judge response is a JSON list, not an object", id="not-an-object"),
    pytest.param('{"grades": "none"}', "judge response has no 'grades' list", id="no-grades-list"),
    pytest.param('{"grades": [1]}', "grades[0] is not an object", id="row-not-an-object"),
    pytest.param('{"grades": [{"grade": "full", "ai_ref": "A1"}]}', "grades[0] has no human_id", id="no-human-id"),
    pytest.param(BAD_GRADE, "grades[0] has grade 'match', not one of full, partial, none", id="unknown-grade"),
]

BASE_META_JSON_KEYS = [
    "unit", "model", "input_tokens", "output_tokens", "elapsed_ms",
    "error", "cost_usd", "cost_source",
]
BASE_FILES = ["judge.meta.json", "judge.response.json", "judge.system.md", "judge.user.md"]


def reply(text: str, *, model: str = JUDGE, input_tokens: int = 1000, output_tokens: int = 500,
          cost_usd: float | None = None, cost_source: str = "") -> InvokeResult:
    return InvokeResult(
        text=text, model=model, backend="scripted",
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_usd=cost_usd, cost_source=cost_source,
    )


class ScriptedJudge:
    """Answers each ``invoke`` with the next scripted step and records the call.

    A step is an :class:`InvokeResult` to return or an exception to raise.
    Running past the script raises ``AssertionError``, which ``judge_case``
    reports as ``judge call failed: AssertionError: unscripted call #<n>``,
    so an extra call shows up in both ``calls`` and the outcome's error.
    """

    def __init__(self, *script) -> None:
        self.models = [JUDGE]
        self.temperature = 0.0
        self.seed = 7
        self._script = list(script)
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=None):
        with self._lock:
            self.calls.append({"system": system, "user": user, "max_tokens": max_tokens,
                               "json_mode": json_mode, "timeout_s": timeout_s})
            if not self._script:
                raise AssertionError(f"unscripted call #{len(self.calls)}")
            step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def _key() -> str:
    return judge_cache_key(judge_prompt_sha(), JUDGE, CASE, assign_refs(RECORD["findings"]))


def _cache_files(cache: Path) -> list[str]:
    return sorted(p.name for p in cache.iterdir()) if cache.exists() else []


def _entry(cache: Path) -> dict:
    return json.loads((cache / f"{_key()}.json").read_text(encoding="utf-8"))


def _files(trace: Path) -> list[str]:
    return sorted(p.name for p in trace.iterdir())


def _response(trace: Path, name: str):
    return json.loads((trace / name).read_text(encoding="utf-8"))


def _meta(trace: Path) -> dict:
    return json.loads((trace / "judge.meta.json").read_text(encoding="utf-8"))


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def _retry_warnings(caplog) -> list[str]:
    return [m for m in _warnings(caplog) if "parse retry" in m]


def _error_warnings(caplog) -> list[str]:
    return [m for m in _warnings(caplog) if m.startswith("judge error for case 'case-1'")]


@pytest.fixture
def cache_renames(monkeypatch):
    """Record each ``os.replace`` whose destination is a judge cache entry."""
    seen: list[str] = []
    real_replace = os.replace

    def spy(src, dst):
        if str(dst).endswith(f"{_key()}.json"):
            seen.append(str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(eval_judge.os, "replace", spy)
    return seen


class TestEveryParseErrorKindIsRetried:
    def test_the_kinds_cover_every_raise_site_in_the_parser(self):
        source = Path(prxref.judge.__file__).read_text(encoding="utf-8")
        assert len(re.findall(r"\braise JudgeParseError\(", source)) == len(KINDS)

    @pytest.mark.parametrize(("text", "error"), KINDS)
    def test_each_reply_reaches_its_own_raise_site(self, text, error):
        with pytest.raises(JudgeParseError) as caught:
            parse_judge_response(text, human_files(CASE), ai_ref_files(assign_refs(RECORD["findings"])))
        assert str(caught.value) == error

    @pytest.mark.parametrize(("text", "error"), KINDS)
    def test_malformed_then_valid_grades_the_case(self, tmp_path, caplog, cache_renames, text, error):
        cache = tmp_path / "cache"
        client = ScriptedJudge(reply(text), reply(GOOD_REPLY))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            outcome = judge_case(client, JUDGE, CASE, RECORD, cache_dir=cache, max_tokens=2048,
                                 timeout_s=9.0, parse_retries=1)
        assert len(client.calls) == 2
        assert client.calls[0] == client.calls[1]
        assert client.calls[0]["json_mode"] is True
        assert (client.calls[0]["max_tokens"], client.calls[0]["timeout_s"]) == (2048, 9.0)
        assert outcome.ok and outcome.error is None
        assert outcome.grades == GOOD_GRADES
        assert (outcome.llm_calls, outcome.parse_retries, outcome.cached) == (2, 1, False)
        assert _cache_files(cache) == [f"{_key()}.json"]
        assert _entry(cache)["response"] == GOOD_REPLY
        assert cache_renames == [str(cache / f"{_key()}.json")]
        assert _retry_warnings(caplog) == [
            f"judge for case 'case-1': unusable reply ({error}); parse retry 1 of 1",
        ]
        assert _error_warnings(caplog) == []


class TestMalformedEveryTime:
    def test_malformed_twice_at_one_fails_with_the_last_reason_and_caches_nothing(
        self, tmp_path, caplog, cache_renames,
    ):
        cache = tmp_path / "cache"
        client = ScriptedJudge(reply(NOT_JSON), reply(BAD_GRADE))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            outcome = judge_case(client, JUDGE, CASE, RECORD, cache_dir=cache, parse_retries=1)
        assert len(client.calls) == 2
        assert not outcome.ok and outcome.grades is None
        assert outcome.error == BAD_GRADE_ERROR
        assert (outcome.llm_calls, outcome.parse_retries, outcome.cached) == (2, 1, False)
        assert _cache_files(cache) == []
        assert cache_renames == []
        assert _error_warnings(caplog) == [f"judge error for case 'case-1': {BAD_GRADE_ERROR}"]
        assert len(_retry_warnings(caplog)) == 1

    def test_malformed_three_times_at_two_makes_three_calls(self, tmp_path, caplog):
        cache = tmp_path / "cache"
        client = ScriptedJudge(reply(""), reply("[]"), reply(NOT_JSON))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            outcome = judge_case(client, JUDGE, CASE, RECORD, cache_dir=cache, parse_retries=2)
        assert len(client.calls) == 3
        assert client.calls[0] == client.calls[1] == client.calls[2]
        assert outcome.error == NOT_JSON_ERROR
        assert (outcome.llm_calls, outcome.parse_retries) == (3, 2)
        assert _cache_files(cache) == []
        assert [m.rsplit("; ", 1)[1] for m in _retry_warnings(caplog)] == ["parse retry 1 of 2", "parse retry 2 of 2"]

    def test_a_valid_third_reply_at_two_grades_the_case(self, tmp_path):
        cache = tmp_path / "cache"
        client = ScriptedJudge(reply(""), reply(BAD_GRADE), reply(GOOD_REPLY))
        outcome = judge_case(client, JUDGE, CASE, RECORD, cache_dir=cache, parse_retries=2)
        assert len(client.calls) == 3
        assert outcome.ok and outcome.grades == GOOD_GRADES
        assert (outcome.llm_calls, outcome.parse_retries) == (3, 2)
        assert _entry(cache)["response"] == GOOD_REPLY

    def test_the_next_score_judges_the_uncached_case_again(self, tmp_path):
        cache = tmp_path / "cache"
        judge_case(ScriptedJudge(reply(NOT_JSON), reply(NOT_JSON)), JUDGE, CASE, RECORD,
                   cache_dir=cache, parse_retries=1)
        client = ScriptedJudge(reply(GOOD_REPLY))
        outcome = judge_case(client, JUDGE, CASE, RECORD, cache_dir=cache, parse_retries=1)
        assert len(client.calls) == 1 and outcome.ok and not outcome.cached


class TestAFirstReplyThatParses:
    def test_makes_one_call_and_no_retry_at_one(self, tmp_path, caplog):
        trace = tmp_path / "trace"
        client = ScriptedJudge(reply(GOOD_REPLY))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            outcome = judge_case(client, JUDGE, CASE, RECORD, trace_dir=str(trace), parse_retries=1)
        assert len(client.calls) == 1
        assert outcome.ok and (outcome.llm_calls, outcome.parse_retries) == (1, 0)
        assert _files(trace) == BASE_FILES
        assert list(_meta(trace)) == BASE_META_JSON_KEYS
        assert _warnings(caplog) == []


class TestZeroIsTheOldBehaviour:
    @pytest.mark.parametrize("kwargs", [pytest.param({}, id="default"), pytest.param({"parse_retries": 0}, id="zero")])
    def test_a_malformed_reply_is_one_call_and_todays_reason(self, tmp_path, caplog, kwargs):
        cache = tmp_path / "cache"
        client = ScriptedJudge(reply(NOT_JSON, cost_usd=0.003, cost_source="usage.cost"))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            outcome = judge_case(client, JUDGE, CASE, RECORD, cache_dir=cache, **kwargs)
        assert len(client.calls) == 1
        assert outcome.error == NOT_JSON_ERROR
        assert outcome.grades is None
        assert (outcome.llm_calls, outcome.parse_retries, outcome.cached) == (1, 0, False)
        assert (outcome.model, outcome.input_tokens, outcome.output_tokens) == (JUDGE, 1000, 500)
        assert (outcome.cost_usd, outcome.cost_estimated, outcome.cost_source) == (0.003, False, "usage.cost")
        assert outcome.unit == {"model": JUDGE, "input_tokens": 1000, "output_tokens": 500,
                                "cost_usd": 0.003, "cost_source": "usage.cost"}
        assert _warnings(caplog) == [f"judge error for case 'case-1': {NOT_JSON_ERROR}"]
        assert _cache_files(cache) == []

    @pytest.mark.parametrize("text", ["", "[]", BAD_GRADE])
    def test_no_kind_is_retried(self, text):
        client = ScriptedJudge(reply(text))
        outcome = judge_case(client, JUDGE, CASE, RECORD, parse_retries=0)
        assert len(client.calls) == 1
        assert outcome.error.startswith("judge response rejected: ")
        assert outcome.parse_retries == 0

    def test_a_negative_budget_retries_nothing(self):
        client = ScriptedJudge(reply(NOT_JSON))
        outcome = judge_case(client, JUDGE, CASE, RECORD, parse_retries=-1)
        assert len(client.calls) == 1 and outcome.error == NOT_JSON_ERROR and outcome.parse_retries == 0

    def test_the_trace_has_four_files_and_eight_meta_keys(self, tmp_path):
        trace = tmp_path / "trace"
        judge_case(ScriptedJudge(reply(NOT_JSON)), JUDGE, CASE, RECORD, trace_dir=str(trace))
        assert _files(trace) == BASE_FILES
        meta = _meta(trace)
        assert list(meta) == BASE_META_JSON_KEYS
        assert meta["error"] == NOT_JSON_ERROR
        assert _response(trace, "judge.response.json") == NOT_JSON

    def test_an_attempt_file_left_by_an_earlier_run_is_not_touched(self, tmp_path):
        trace = tmp_path / "trace"
        trace.mkdir()
        (trace / "judge.attempt1.response.json").write_text('"old"', encoding="utf-8")
        judge_case(ScriptedJudge(reply(GOOD_REPLY)), JUDGE, CASE, RECORD, trace_dir=str(trace))
        assert (trace / "judge.attempt1.response.json").read_text(encoding="utf-8") == '"old"'


class TestARaisingCallIsNeverRetried:
    def test_a_raising_first_call_is_one_call(self, tmp_path):
        cache = tmp_path / "cache"
        client = ScriptedJudge(TimeoutError("late"), reply(GOOD_REPLY))
        outcome = judge_case(client, JUDGE, CASE, RECORD, cache_dir=cache, parse_retries=1)
        assert len(client.calls) == 1
        assert outcome.error == "judge call failed: TimeoutError: late"
        assert (outcome.llm_calls, outcome.parse_retries) == (1, 0)
        assert (outcome.cost_usd, outcome.model, outcome.input_tokens) == (None, "", 0)
        assert _cache_files(cache) == []

    def test_a_raising_retry_fails_the_case_and_keeps_the_billed_attempt(self, tmp_path, caplog):
        cache = tmp_path / "cache"
        trace = tmp_path / "trace"
        client = ScriptedJudge(reply(NOT_JSON, cost_usd=0.001, cost_source="usage.cost"), TimeoutError("late"))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            outcome = judge_case(client, JUDGE, CASE, RECORD, cache_dir=cache, trace_dir=str(trace),
                                 parse_retries=2)
        assert len(client.calls) == 2
        assert outcome.error == "judge call failed: TimeoutError: late"
        assert outcome.grades is None
        assert (outcome.llm_calls, outcome.parse_retries) == (2, 1)
        assert (outcome.model, outcome.input_tokens, outcome.output_tokens) == (JUDGE, 1000, 500)
        assert outcome.cost_usd == 0.001
        assert judge_cost([outcome], {}) == (0.001, False)
        assert _cache_files(cache) == []
        assert _error_warnings(caplog) == ["judge error for case 'case-1': judge call failed: TimeoutError: late"]
        assert _response(trace, "judge.response.json") is None
        assert _response(trace, "judge.attempt1.response.json") == NOT_JSON
        meta = _meta(trace)
        assert (meta["parse_retries"], meta["first_error"]) == (1, NOT_JSON_ERROR)
        assert meta["error"] == "judge call failed: TimeoutError: late"


class TestUsageCoversEveryAttempt:
    def test_tokens_are_summed_and_the_model_is_the_last_calls(self):
        client = ScriptedJudge(
            reply(NOT_JSON, input_tokens=1000, output_tokens=500),
            reply(GOOD_REPLY, model=FALLBACK, input_tokens=1200, output_tokens=700),
        )
        outcome = judge_case(client, JUDGE, CASE, RECORD, parse_retries=1)
        assert (outcome.input_tokens, outcome.output_tokens, outcome.model) == (2200, 1200, FALLBACK)
        assert (outcome.unit["input_tokens"], outcome.unit["output_tokens"], outcome.unit["model"]) == (
            2200, 1200, FALLBACK)

    def test_reported_costs_are_summed(self):
        client = ScriptedJudge(
            reply(NOT_JSON, cost_usd=0.001, cost_source="usage.cost"),
            reply(GOOD_REPLY, cost_usd=0.002, cost_source="x-litellm-response-cost"),
        )
        outcome = judge_case(client, JUDGE, CASE, RECORD, parse_retries=1)
        assert outcome.cost_usd == pytest.approx(0.003)
        assert (outcome.cost_estimated, outcome.cost_source) == (False, "x-litellm-response-cost")
        assert judge_cost([outcome], {})[0] == pytest.approx(0.003)

    def test_a_failed_case_is_billed_for_every_attempt(self):
        client = ScriptedJudge(
            reply(NOT_JSON, cost_usd=0.001, cost_source="usage.cost"),
            reply(BAD_GRADE, cost_usd=0.002, cost_source="usage.cost"),
        )
        outcome = judge_case(client, JUDGE, CASE, RECORD, parse_retries=1)
        assert not outcome.ok
        assert outcome.cost_usd == pytest.approx(0.003)
        assert (outcome.input_tokens, outcome.output_tokens) == (2000, 1000)

    def test_one_unreported_attempt_makes_the_cost_unknown_without_a_table(self):
        client = ScriptedJudge(
            reply(NOT_JSON, cost_usd=0.001, cost_source="usage.cost"),
            reply(GOOD_REPLY, cost_usd=None),
        )
        outcome = judge_case(client, JUDGE, CASE, RECORD, price_table={}, parse_retries=1)
        assert (outcome.cost_usd, outcome.cost_estimated, outcome.cost_source) == (None, False, "")
        assert judge_cost([outcome], {}) == (None, False)

    def test_the_price_table_prices_the_summed_tokens(self):
        table = parse_price_table({JUDGE: {"input": 1.0, "output": 2.0}})
        client = ScriptedJudge(reply(NOT_JSON), reply(GOOD_REPLY))
        outcome = judge_case(client, JUDGE, CASE, RECORD, price_table=table, parse_retries=1)
        assert (outcome.cost_usd, outcome.cost_estimated, outcome.cost_source) == (0.004, True, "")
        assert judge_cost([outcome], table) == (0.004, True)


class TestACacheHitIsNeverRetried:
    def test_a_hit_makes_no_call_and_no_retry(self, tmp_path):
        cache = tmp_path / "cache"
        judge_case(ScriptedJudge(reply(NOT_JSON), reply(GOOD_REPLY)), JUDGE, CASE, RECORD,
                   cache_dir=cache, parse_retries=1)
        client = ScriptedJudge()
        outcome = judge_case(client, JUDGE, CASE, RECORD, cache_dir=cache, parse_retries=1)
        assert client.calls == []
        assert outcome.ok and outcome.cached and outcome.grades == GOOD_GRADES
        assert (outcome.llm_calls, outcome.parse_retries) == (0, 0)

    def test_a_case_without_labels_makes_no_call(self):
        client = ScriptedJudge()
        outcome = judge_case(client, JUDGE, EvalCase(id="empty", expected=()), RECORD, parse_retries=3)
        assert client.calls == []
        assert (outcome.llm_calls, outcome.parse_retries) == (0, 0)


class TestTheTraceKeepsEveryReply:
    def test_the_discarded_reply_is_an_attempt_file_and_the_base_files_show_the_used_one(self, tmp_path):
        trace = tmp_path / "trace"
        client = ScriptedJudge(reply(NOT_JSON, cost_usd=0.001, cost_source="usage.cost"),
                               reply(GOOD_REPLY, cost_usd=0.002, cost_source="usage.cost"))
        judge_case(client, JUDGE, CASE, RECORD, trace_dir=str(trace), parse_retries=1)
        assert _files(trace) == sorted([*BASE_FILES, "judge.attempt1.response.json"])
        assert _response(trace, "judge.attempt1.response.json") == NOT_JSON
        assert _response(trace, "judge.response.json") == GOOD_REPLY
        assert (trace / "judge.user.md").read_text(encoding="utf-8") == client.calls[1]["user"]
        meta = _meta(trace)
        assert list(meta) == [*BASE_META_JSON_KEYS, "parse_retries", "first_error"]
        assert (meta["parse_retries"], meta["first_error"], meta["error"]) == (1, NOT_JSON_ERROR, "")
        assert (meta["input_tokens"], meta["output_tokens"]) == (2000, 1000)
        assert meta["cost_usd"] == pytest.approx(0.003)

    def test_malformed_twice_keeps_both_replies(self, tmp_path):
        trace = tmp_path / "trace"
        judge_case(ScriptedJudge(reply(NOT_JSON), reply(BAD_GRADE)), JUDGE, CASE, RECORD,
                   trace_dir=str(trace), parse_retries=1)
        assert _response(trace, "judge.attempt1.response.json") == NOT_JSON
        assert _response(trace, "judge.response.json") == BAD_GRADE
        meta = _meta(trace)
        assert (meta["first_error"], meta["error"]) == (NOT_JSON_ERROR, BAD_GRADE_ERROR)

    def test_two_retries_write_two_attempt_files_in_call_order(self, tmp_path):
        trace = tmp_path / "trace"
        judge_case(ScriptedJudge(reply(""), reply("[]"), reply(GOOD_REPLY)), JUDGE, CASE, RECORD,
                   trace_dir=str(trace), parse_retries=2)
        assert _response(trace, "judge.attempt1.response.json") == ""
        assert _response(trace, "judge.attempt2.response.json") == "[]"
        assert _response(trace, "judge.response.json") == GOOD_REPLY
        assert _meta(trace)["parse_retries"] == 2

    def test_a_later_run_with_fewer_retries_removes_the_stale_attempt_files(self, tmp_path):
        trace = tmp_path / "trace"
        judge_case(ScriptedJudge(reply(""), reply("[]"), reply(GOOD_REPLY)), JUDGE, CASE, RECORD,
                   trace_dir=str(trace), parse_retries=2)
        judge_case(ScriptedJudge(reply(GOOD_REPLY)), JUDGE, CASE, RECORD, trace_dir=str(trace), parse_retries=2)
        assert _files(trace) == BASE_FILES
        assert list(_meta(trace)) == BASE_META_JSON_KEYS


class TestJudgeOutcomeField:
    def test_parse_retries_is_the_last_field_and_defaults_to_zero(self):
        assert dataclasses.fields(JudgeOutcome)[-1].name == "parse_retries"
        outcome = JudgeOutcome(case_id="c", cache_key="k", grades=(), error=None, cached=False, llm_calls=0,
                               ref_index={})
        assert outcome.parse_retries == 0
