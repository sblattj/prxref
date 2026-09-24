"""Issue #67, the backend half: every call carries the dollar figure its provider REPORTED.

``OpenAICompatClient`` reads the body's ``usage.cost`` (OpenRouter sends it
unasked), else the ``x-litellm-response-cost`` header a LiteLLM gateway or
llm-ferry sets; ``LiteLLMClient`` reads ``_hidden_params["response_cost"]``.
No figure is ``None``, never ``0.0``, and a backend never estimates. Inside one
openai-compat call, every completion that came back was billed, so truncated
attempts the chain moved past are summed into the answer's figure, and one
unpriced attempt makes the whole call's figure unknown.

The reviewer copies the figure into each unit's meta before it parses the
answer, so an answer that arrived and then failed to parse still carries what
it cost. The last class drives the REAL client through the real reviewer and
the real orchestrator to the run record, with a fake HTTP session and no
network.
"""
from __future__ import annotations

import json
import logging
import sys
import types
from types import SimpleNamespace

import pytest
from requests.structures import CaseInsensitiveDict

from prxref import costs
from prxref.llm import InvokeResult
from prxref.llm_backends import LiteLLMClient, OpenAICompatClient
from prxref.orchestrator import orchestrate_review
from prxref.reviewer import review_chunk, review_systemic
from prxref.triage import parse_unified_diff
from tests.test_llm_backends import (
    _client,
    _client_capturing_payload,
    _resp,
    _ScriptedSession,
)
from tests.test_orchestrator import REF, FakeForge, _added_file_diff

_NO_COST = object()
HEADER = "X-LiteLLM-Response-Cost"
CLEAN = json.dumps({"findings": [], "escalations": []})
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


def _answer(
    cost=_NO_COST, *, header=None, headers=None, finish_reason="stop", text="ok",
    model="m1-resolved", prompt=11, completion=7,
):
    """One HTTP 200 chat completion; ``cost`` goes in ``usage.cost``, ``header`` in the cost header."""
    usage = {"prompt_tokens": prompt, "completion_tokens": completion}
    if cost is not _NO_COST:
        usage["cost"] = cost
    resp = _resp(
        model=model, usage=usage,
        choices=[{"message": {"role": "assistant", "content": text}, "finish_reason": finish_reason}],
    )
    if header is not None:
        resp.headers = CaseInsensitiveDict({HEADER: header})
    if headers is not None:
        resp.headers = headers
    return resp


def _litellm(monkeypatch, response_extra, *, module_extra=None):
    """Install a fake ``litellm`` whose completion returns one response carrying ``response_extra``."""
    captured: list[dict] = []

    def fake_completion(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="lit-ok"))],
            usage=SimpleNamespace(prompt_tokens=8, completion_tokens=4),
            model="openai/gpt-4o-mini",
            **response_extra,
        )

    module = types.SimpleNamespace(completion=fake_completion, **(module_extra or {}))
    monkeypatch.setitem(sys.modules, "litellm", module)
    return LiteLLMClient(models=["openrouter/openai/gpt-4o-mini"]), captured


class TestOpenAICompatReportsTheProviderFigure:
    def test_openai_compat_carries_usage_cost_into_the_result(self):
        r = _client(_ScriptedSession(_answer(0.0021))).invoke("s", "u")
        assert (r.cost_usd, r.cost_source) == (0.0021, "usage.cost")

    def test_openai_compat_reads_the_litellm_cost_header(self):
        r = _client(_ScriptedSession(_answer(header="0.0042"))).invoke("s", "u")
        assert (r.cost_usd, r.cost_source) == (0.0042, "x-litellm-response-cost")

    def test_the_header_is_found_in_any_case_in_a_plain_mapping(self):
        resp = _answer(headers={"content-type": "application/json", "x-LiteLLM-response-COST": "0.0042"})
        r = _client(_ScriptedSession(resp)).invoke("s", "u")
        assert (r.cost_usd, r.cost_source) == (0.0042, "x-litellm-response-cost")

    def test_body_cost_wins_over_the_header(self):
        r = _client(_ScriptedSession(_answer(0.0021, header="0.9"))).invoke("s", "u")
        assert (r.cost_usd, r.cost_source) == (0.0021, "usage.cost")

    def test_an_unusable_body_cost_falls_back_to_the_header(self):
        r = _client(_ScriptedSession(_answer(-1, header="0.0042"))).invoke("s", "u")
        assert (r.cost_usd, r.cost_source) == (0.0042, "x-litellm-response-cost")

    def test_no_cost_anywhere_is_none_not_zero(self):
        r = _client(_ScriptedSession(_answer())).invoke("s", "u")
        assert r.cost_usd is None
        assert r.cost_usd != 0
        assert r.cost_source == ""

    def test_a_reported_zero_is_a_real_zero(self):
        r = _client(_ScriptedSession(_answer(0))).invoke("s", "u")
        assert r.cost_usd == 0.0
        assert r.cost_usd is not None
        assert r.cost_source == "usage.cost"

    @pytest.mark.parametrize(
        "where, value",
        [
            ("body", "0.1"),
            ("body", -1),
            ("body", True),
            ("body", float("nan")),
            ("body", float("inf")),
            ("body", None),
            ("body", {"total": 0.1}),
            ("header", ""),
            ("header", "None"),
            ("header", "abc"),
            ("header", "nan"),
            ("header", "-0.5"),
        ],
    )
    def test_a_malformed_reported_cost_is_ignored(self, where, value):
        resp = _answer(value) if where == "body" else _answer(header=value)
        r = _client(_ScriptedSession(resp)).invoke("s", "u")
        assert (r.cost_usd, r.cost_source) == (None, "")
        assert r.text == "ok"

    def test_tokens_are_untouched_by_the_cost(self):
        r = _client(_ScriptedSession(_answer(0.0021, prompt=40, completion=9))).invoke("s", "u")
        assert (r.input_tokens, r.output_tokens) == (40, 9)


class TestEveryReceivedAttemptIsBilled:
    def test_truncated_attempt_costs_are_added_to_the_answering_call(self):
        session = _ScriptedSession(
            _answer(0.01, finish_reason="length", model="m1", completion=4096),
            _answer(0.02, model="m2", prompt=30, completion=5),
        )
        r = _client(session).invoke("s", "u")
        assert r.model == "m2"
        assert r.cost_usd == pytest.approx(0.03)
        assert r.cost_source == "usage.cost"
        assert (r.input_tokens, r.output_tokens) == (30, 5)

    def test_an_uncosted_attempt_makes_the_call_cost_unknown(self):
        session = _ScriptedSession(
            _answer(finish_reason="length", model="m1"),
            _answer(0.02, model="m2"),
        )
        r = _client(session).invoke("s", "u")
        assert r.model == "m2"
        assert (r.cost_usd, r.cost_source) == (None, "")

    def test_exhaustion_by_truncation_returns_the_summed_cost(self):
        session = _ScriptedSession(
            _answer(0.01, finish_reason="length", model="m1"),
            _answer(0.02, finish_reason="length", model="m2"),
        )
        r = _client(session).invoke("s", "u")
        assert r.finish_reason == "length"
        assert r.model == "m2"
        assert r.cost_usd == pytest.approx(0.03)
        assert r.cost_source == "usage.cost"

    def test_the_last_attempts_source_names_a_mixed_sum(self):
        session = _ScriptedSession(
            _answer(0.01, finish_reason="length", model="m1"),
            _answer(header="0.02", model="m2"),
        )
        r = _client(session).invoke("s", "u")
        assert r.cost_usd == pytest.approx(0.03)
        assert r.cost_source == "x-litellm-response-cost"

    def test_an_attempt_that_returned_no_completion_adds_nothing(self):
        """An HTTP error or a malformed body carries no figure to read, so only
        the answering attempt is priced; neither makes the call unknown."""
        session = _ScriptedSession(
            _resp(status_code=500),
            _resp(choices=[]),
            _answer(0.02),
        )
        r = _client(session, models=("m1", "m2", "m3")).invoke("s", "u")
        assert (r.cost_usd, r.cost_source) == (0.02, "usage.cost")

    def test_the_request_never_asks_for_usage_include(self):
        client, captured = _client_capturing_payload(models=("m1", "m2"), fail_first=True)
        client.invoke("s", "u", json_mode=True)
        assert len(captured) == 2
        for payload in captured:
            assert "usage" not in payload
            assert "stream_options" not in payload


class TestTheAttemptLogLine:
    def test_the_ok_line_names_the_reported_figure(self, caplog):
        caplog.set_level(logging.INFO, logger="prxref.llm_backends")
        _client(_ScriptedSession(_answer(0.0021))).invoke("s", "u")
        ok = [r.getMessage() for r in caplog.records if " ok: " in r.getMessage()]
        assert len(ok) == 1
        assert ok[0].endswith(" finish=stop cost=0.0021")

    def test_the_ok_line_says_dash_when_nothing_was_reported(self, caplog):
        caplog.set_level(logging.INFO, logger="prxref.llm_backends")
        _client(_ScriptedSession(_answer())).invoke("s", "u")
        ok = [r.getMessage() for r in caplog.records if " ok: " in r.getMessage()]
        assert len(ok) == 1
        assert ok[0].endswith(" cost=-")


class TestLiteLLMReportsResponseCost:
    def test_litellm_response_cost_is_carried(self, monkeypatch):
        client, _ = _litellm(monkeypatch, {"_hidden_params": {"response_cost": 1.35e-05}})
        r = client.invoke("s", "u")
        assert (r.cost_usd, r.cost_source) == (1.35e-05, "litellm")
        assert (r.input_tokens, r.output_tokens) == (8, 4)

    def test_hidden_params_read_as_attributes_are_carried_too(self, monkeypatch):
        client, _ = _litellm(monkeypatch, {"_hidden_params": SimpleNamespace(response_cost=0.5)})
        r = client.invoke("s", "u")
        assert (r.cost_usd, r.cost_source) == (0.5, "litellm")

    def test_a_reported_zero_is_a_real_zero(self, monkeypatch):
        client, _ = _litellm(monkeypatch, {"_hidden_params": {"response_cost": 0.0}})
        r = client.invoke("s", "u")
        assert (r.cost_usd, r.cost_source) == (0.0, "litellm")

    @pytest.mark.parametrize(
        "extra",
        [
            {},
            {"_hidden_params": None},
            {"_hidden_params": {}},
            {"_hidden_params": {"response_cost": None}},
            {"_hidden_params": {"response_cost": "0.1"}},
            {"_hidden_params": {"response_cost": -1}},
            {"_hidden_params": {"response_cost": float("nan")}},
            {"_hidden_params": {"response_cost": True}},
        ],
    )
    def test_litellm_missing_hidden_params_or_none_cost_is_none(self, monkeypatch, extra):
        client, _ = _litellm(monkeypatch, extra)
        r = client.invoke("s", "u")
        assert (r.cost_usd, r.cost_source) == (None, "")
        assert r.text == "lit-ok"

    def test_litellm_completion_cost_is_never_called(self, monkeypatch):
        calls: list[tuple] = []

        def completion_cost(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("completion_cost must never be called")

        client, _ = _litellm(
            monkeypatch, {"_hidden_params": {}}, module_extra={"completion_cost": completion_cost},
        )
        r = client.invoke("s", "u")
        assert calls == []
        assert r.cost_usd is None

    def test_the_litellm_request_is_unchanged(self, monkeypatch):
        client, captured = _litellm(monkeypatch, {"_hidden_params": {"response_cost": 0.1}})
        client.invoke("s", "u")
        assert "usage" not in captured[0]
        assert "stream_options" not in captured[0]


class _CostLLM:
    """Reviewer-level double: one result, optionally carrying a reported cost."""

    def __init__(self, text=CLEAN, *, cost_usd=None, cost_source="", error=None):
        self.text = text
        self.cost_usd = cost_usd
        self.cost_source = cost_source
        self.error = error

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=None):
        if self.error is not None:
            raise self.error
        return InvokeResult(
            text=self.text, input_tokens=100, output_tokens=50, model="fake-model",
            backend="fake", elapsed_ms=3, cost_usd=self.cost_usd, cost_source=self.cost_source,
        )


def _chunk():
    return parse_unified_diff(MINI_DIFF)


class TestTheReviewerMetaCarriesTheFigure:
    def test_meta_carries_cost_from_the_invoke_result(self):
        _, meta = review_chunk(_CostLLM(cost_usd=0.0021, cost_source="usage.cost"), _chunk())
        assert meta["error"] == ""
        assert (meta["cost_usd"], meta["cost_source"]) == (0.0021, "usage.cost")

    def test_the_sweep_meta_carries_it_too(self):
        _, meta = review_systemic(_CostLLM(cost_usd=0.004, cost_source="litellm"), "the digest")
        assert (meta["cost_usd"], meta["cost_source"]) == (0.004, "litellm")

    def test_an_answer_that_fails_to_parse_still_carries_its_cost(self):
        """It arrived and was billed; the figure is read before the parse."""
        llm = _CostLLM("I am not returning JSON.", cost_usd=0.0021, cost_source="usage.cost")
        findings, meta = review_chunk(llm, _chunk())
        assert findings == []
        assert "JSONDecodeError" in meta["error"]
        assert (meta["cost_usd"], meta["cost_source"]) == (0.0021, "usage.cost")

    def test_meta_cost_is_none_when_the_result_reports_none(self):
        _, meta = review_chunk(_CostLLM(), _chunk())
        assert (meta["cost_usd"], meta["cost_source"]) == (None, "")

    def test_meta_cost_is_none_when_the_result_has_no_cost_fields(self):
        class LegacyResult:
            text = CLEAN
            input_tokens = 1
            output_tokens = 2
            model = "legacy"

        class LegacyLLM:
            def invoke(self, *args, **kwargs):
                return LegacyResult()

        _, meta = review_chunk(LegacyLLM(), _chunk())
        assert meta["error"] == ""
        assert (meta["cost_usd"], meta["cost_source"]) == (None, "")

    def test_an_unusable_figure_is_dropped_with_its_source(self):
        _, meta = review_chunk(_CostLLM(cost_usd=-1.0, cost_source="usage.cost"), _chunk())
        assert (meta["cost_usd"], meta["cost_source"]) == (None, "")

    def test_meta_cost_is_none_when_invoke_raises(self):
        _, meta = review_chunk(_CostLLM(error=RuntimeError("endpoint down")), _chunk())
        assert "endpoint down" in meta["error"]
        assert (meta["cost_usd"], meta["cost_source"]) == (None, "")

    def test_meta_json_carries_cost_usd_and_source(self, tmp_path):
        review_chunk(
            _CostLLM(cost_usd=0.0021, cost_source="usage.cost"), _chunk(),
            trace_dir=str(tmp_path), trace_label="chunk0",
        )
        meta = json.loads((tmp_path / "chunk0.meta.json").read_text())
        assert (meta["cost_usd"], meta["cost_source"]) == (0.0021, "usage.cost")

    def test_meta_json_cost_is_null_on_failure(self, tmp_path):
        review_chunk(
            _CostLLM(error=RuntimeError("endpoint down")), _chunk(),
            trace_dir=str(tmp_path), trace_label="chunk0",
        )
        meta = json.loads((tmp_path / "chunk0.meta.json").read_text())
        assert meta["cost_usd"] is None
        assert meta["cost_source"] == ""


class _EveryCallSession:
    """An HTTP session answering EVERY post with a fresh response from ``make``."""

    def __init__(self, make):
        self.make = make
        self.posts = 0

    def post(self, url, json=None, headers=None, timeout=None, stream=None):
        self.posts += 1
        return self.make()


def _real_client(make) -> tuple[OpenAICompatClient, _EveryCallSession]:
    session = _EveryCallSession(make)
    client = OpenAICompatClient(
        base_url="https://llm.test/v1/", api_key="local", models=["m1"],
        session=session, default_timeout=45.0,
    )
    return client, session


def _review(client, tmp_path, **kw):
    """A real run on a one-file diff: one chunk plus the sweep, two calls."""
    forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
    return orchestrate_review(forge, REF, client, post=False, trace_dir=str(tmp_path), **kw)


def _unit_metas(tmp_path) -> dict[str, dict]:
    return {u: json.loads((tmp_path / f"{u}.meta.json").read_text()) for u in ("chunk0", "sweep")}


class TestTheFigureReachesTheRunRecord:
    """The real client, the real reviewer and the real orchestrator, end to end."""

    def test_a_body_cost_reaches_the_run_record(self, tmp_path):
        client, session = _real_client(lambda: _answer(0.0021, text=CLEAN))
        res = _review(client, tmp_path)
        assert session.posts == 2
        assert res["chunks_failed"] == 0
        assert res["cost_usd"] == pytest.approx(2 * 0.0021)
        assert res["cost_estimated"] is False
        for unit, meta in _unit_metas(tmp_path).items():
            assert (meta["cost_usd"], meta["cost_source"]) == (0.0021, "usage.cost"), unit

    def test_a_gateway_header_cost_reaches_the_run_record(self, tmp_path):
        client, _ = _real_client(lambda: _answer(header="0.0042", text=CLEAN))
        res = _review(client, tmp_path)
        assert res["cost_usd"] == pytest.approx(2 * 0.0042)
        assert res["cost_estimated"] is False
        for unit, meta in _unit_metas(tmp_path).items():
            assert meta["cost_source"] == "x-litellm-response-cost", unit

    def test_a_reported_figure_wins_over_a_table_entry(self, tmp_path):
        client, _ = _real_client(lambda: _answer(0.0021, text=CLEAN))
        table = {"m1-resolved": costs.ModelPrice(input=1.0, output=2.0)}
        res = _review(client, tmp_path, price_table=table)
        assert res["cost_usd"] == pytest.approx(2 * 0.0021)
        assert res["cost_estimated"] is False

    def test_an_answer_that_fails_to_parse_still_counts(self, tmp_path):
        client, _ = _real_client(lambda: _answer(0.0021, text="I am not returning JSON."))
        res = _review(client, tmp_path)
        assert res["cost_usd"] == pytest.approx(2 * 0.0021)
        chunk = _unit_metas(tmp_path)["chunk0"]
        assert "JSONDecodeError" in chunk["error"]
        assert chunk["cost_usd"] == 0.0021

    def test_no_reported_figure_is_unknown_never_zero(self, tmp_path):
        """Control: the same run with nothing reported and no table."""
        client, _ = _real_client(lambda: _answer(text=CLEAN))
        res = _review(client, tmp_path)
        assert res["cost_usd"] is None
        assert res["cost_estimated"] is False
        for unit, meta in _unit_metas(tmp_path).items():
            assert (meta["cost_usd"], meta["cost_source"]) == (None, ""), unit

    def test_no_reported_figure_with_a_table_is_estimated(self, tmp_path):
        """Control: only the table differs from the unknown run above."""
        client, _ = _real_client(lambda: _answer(text=CLEAN, prompt=11, completion=7))
        table = {"m1-resolved": costs.ModelPrice(input=1.0, output=2.0)}
        res = _review(client, tmp_path, price_table=table)
        assert res["cost_usd"] == pytest.approx(2 * (11 * 1.0 + 7 * 2.0) / 1_000_000)
        assert res["cost_estimated"] is True

    def test_a_failed_call_reaches_the_run_record_as_null(self, tmp_path):
        client, _ = _real_client(lambda: _resp(status_code=500))
        res = _review(client, tmp_path)
        assert res["cost_usd"] is None
        for unit, meta in _unit_metas(tmp_path).items():
            assert meta["cost_usd"] is None, unit
