"""Issue #67: a review's dollar cost, from the review units to the run record.

The backends REPORT a figure per call (``InvokeResult.cost_usd``) and the
reviewer copies it into the unit's meta; both halves are owned elsewhere.
What is pinned here is the orchestrator's half: every review unit dict carries
``cost_usd`` / ``cost_source`` (``None`` / ``""`` whenever the call raised or
the reviewer's meta has no figure), and ``_stamp_run_cost`` totals them with
:func:`prxref.costs.run_cost` into the record's ``cost_usd`` and
``cost_estimated``: reported, else estimated from the price table, else
unknown (``None``, never ``0``).

``_cost_review_chunk`` / ``_cost_review_systemic`` stand in for the reviewer
contract that carries the invoke result's cost into the meta, so the tests
drive the whole orchestrator with a fake LLM that reports a cost. Every
expected figure is computed from what those doubles return: one chunk plus a
sweep that also calls the model, each 100 input and 50 output tokens.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re

import pytest

from prxref import costs, orchestrator
from prxref.cli import _build_json_result
from prxref.forges.base import ATTRIBUTION_MARKER
from prxref.orchestrator import orchestrate_review
from prxref.triage import Finding
from tests.test_orchestrator import (
    HAPPY_FINDINGS,
    REF,
    TRUNCATED_REASON,
    TWO_FILE_DIFF,
    FakeForge,
    FakeLLM,
    _added_file_diff,
    _contract_review_chunk,
)

pytestmark = pytest.mark.usefixtures("contract_stubs")

MODEL = "test-model-1"
SWEEP_SYSTEM = "systemic sweep"
ONE_FILE_DIFF = _added_file_diff("src/app.py", 20)
# 100 input and 50 output tokens per unit, at 1.0 / 2.0 USD per million.
TABLE = {MODEL: costs.ModelPrice(input=1.0, output=2.0)}
UNIT_ESTIMATE = (100 * 1.0 + 50 * 2.0) / 1_000_000
UNKNOWN_LINE = (
    "cost unknown: no reported cost and no usable PRXREF_PRICE_TABLE "
    "estimate for model(s) 'test-model-1'"
)
PLAIN_ATTRIBUTION = re.compile(
    rf"{re.escape(ATTRIBUTION_MARKER)} · model=\S+ · \d+ tok · \d+\.\ds"
)


class CostLLM(FakeLLM):
    """``FakeLLM`` whose results report a dollar cost, as a backend would.

    ``sweep_cost_usd`` prices the sweep's call separately; it defaults to the
    chunk figure. ``None`` reports nothing, which is never ``0``.
    """

    _UNSET = object()

    def __init__(self, findings_by_path=None, *, cost_usd=None,
                 cost_source="usage.cost", sweep_cost_usd=_UNSET, error=None):
        super().__init__(findings_by_path, error=error)
        self.cost_usd = cost_usd
        self.sweep_cost_usd = cost_usd if sweep_cost_usd is CostLLM._UNSET else sweep_cost_usd
        self.cost_source = cost_source

    def invoke(self, system, user, **kwargs):
        result = super().invoke(system, user, **kwargs)
        cost = self.sweep_cost_usd if system == SWEEP_SYSTEM else self.cost_usd
        return dataclasses.replace(
            result, cost_usd=cost, cost_source=self.cost_source if cost is not None else "",
        )


def _with_cost(meta: dict, result) -> dict:
    """The reviewer contract: the invoke result's reported cost rides the meta."""
    cost = costs.valid_usd(getattr(result, "cost_usd", None))
    out = dict(meta)
    out["cost_usd"] = cost
    out["cost_source"] = str(getattr(result, "cost_source", "") or "") if cost is not None else ""
    return out


def _cost_review_chunk(
    llm, files, *, pr_title="", pr_description="", repo_hint="",
    max_tokens=None, context_lines=None, context_blocks="", sibling_files=(),
    trace_label="", trace_dir="", prompt_context=None,
):
    seen = []

    class _Tap:
        def invoke(self, *args, **kwargs):
            seen.append(llm.invoke(*args, **kwargs))
            return seen[-1]

    findings, meta = _contract_review_chunk(
        _Tap(), files, pr_title=pr_title, pr_description=pr_description,
        repo_hint=repo_hint, max_tokens=max_tokens, context_lines=context_lines,
        context_blocks=context_blocks, sibling_files=sibling_files,
        trace_label=trace_label, trace_dir=trace_dir, prompt_context=prompt_context,
    )
    return findings, _with_cost(meta, seen[-1])


def _cost_review_systemic(
    llm, digest, *, pr_title="", pr_description="", repo_hint="", max_tokens=None,
    threads=(), trace_label="", trace_dir="", prompt_context=None,
):
    result = llm.invoke(system=SWEEP_SYSTEM, user="[]")
    return [], _with_cost({
        "escalations": [], "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens, "model": result.model,
        "elapsed_ms": 1, "error": "",
    }, result)


@pytest.fixture
def cost_reviewer(contract_stubs, monkeypatch):
    """Swap in reviewer doubles that carry the reported cost into the meta."""
    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _cost_review_chunk)
    monkeypatch.setattr(orchestrator.reviewer, "review_systemic", _cost_review_systemic)


def _spy_units(monkeypatch) -> list[list[dict]]:
    calls: list[list[dict]] = []
    real = orchestrator._stamp_run_cost

    def spy(run_inputs, units, price_table):
        calls.append([dict(u) for u in units])
        return real(run_inputs, units, price_table)

    monkeypatch.setattr(orchestrator, "_stamp_run_cost", spy)
    return calls


def _run(llm, *, diff=ONE_FILE_DIFF, tmp_path=None, **kw):
    forge = FakeForge(diff=diff)
    kw.setdefault("post", False)
    if tmp_path is not None:
        kw["trace_file"] = str(tmp_path / "run.jsonl")
    res = orchestrate_review(forge, REF, llm, **kw)
    return res, forge


def _events(tmp_path, node, phase):
    lines = (tmp_path / "run.jsonl").read_text().splitlines()
    return [
        e for e in (json.loads(x) for x in lines if x.strip())
        if e["node"] == node and e["phase"] == phase
    ]


def _last_line(body: str) -> str:
    return body.rstrip("\n").splitlines()[-1]


def _failing_chunks(outcomes: dict[str, tuple[str, float | None]]):
    """A ``review_chunk`` double: per first path, ``(error, cost)`` of a received answer.

    The response ARRIVED (model and tokens are set) whatever the error, the
    way a truncated or unparseable completion does.
    """
    def _rc(llm, files, **kwargs):
        error, cost = outcomes[files[0].path]
        findings = [] if error else [
            Finding(
                file=files[0].path, line=1, severity="outofscope", confidence=0.9,
                title="ok", body="b",
            )
        ]
        return findings, {
            "escalations": [], "input_tokens": 100, "output_tokens": 50,
            "model": MODEL, "elapsed_ms": 1, "error": error,
            "cost_usd": cost, "cost_source": "usage.cost" if cost is not None else "",
        }
    return _rc


@pytest.mark.usefixtures("cost_reviewer")
class TestEveryUnitCarriesItsCost:
    def test_chunk_and_sweep_units_carry_the_reported_cost_and_source(self, monkeypatch):
        calls = _spy_units(monkeypatch)
        _run(CostLLM(cost_usd=0.001, sweep_cost_usd=0.004))
        assert len(calls) == 1
        chunk, sweep = calls[0]
        assert (chunk["cost_usd"], chunk["cost_source"]) == (0.001, "usage.cost")
        assert (sweep["cost_usd"], sweep["cost_source"]) == (0.004, "usage.cost")

    def test_a_raised_chunk_and_a_raised_sweep_carry_none(self, monkeypatch):
        calls = _spy_units(monkeypatch)
        _run(CostLLM(cost_usd=0.001, error=RuntimeError("no model")))
        for unit in calls[0]:
            assert unit["error"]
            assert unit["cost_usd"] is None
            assert unit["cost_source"] == ""

    def test_a_crashed_worker_carries_none(self, monkeypatch):
        def crash(*args, **kwargs):
            raise RuntimeError("thread died")

        monkeypatch.setattr(orchestrator, "_run_worker", crash)
        calls = _spy_units(monkeypatch)
        _run(CostLLM(cost_usd=0.001))
        chunk, sweep = calls[0]
        assert chunk["error"].startswith("worker crashed")
        assert chunk["cost_usd"] is None
        assert chunk["cost_source"] == ""
        assert sweep["cost_usd"] == 0.001

    def test_the_timeout_retry_carries_the_retried_calls_cost(self, monkeypatch):
        attempts = []

        def rc(llm, files, **kwargs):
            attempts.append(kwargs.get("context_lines"))
            if len(attempts) == 1:
                return [], {
                    "escalations": [], "input_tokens": 0, "output_tokens": 0,
                    "model": "", "elapsed_ms": 1,
                    "error": "LLMError: m1: timeout (read timed out)",
                    "cost_usd": None, "cost_source": "",
                }
            return [], {
                "escalations": [], "input_tokens": 100, "output_tokens": 50,
                "model": MODEL, "elapsed_ms": 1, "error": "",
                "cost_usd": 0.002, "cost_source": "usage.cost",
            }

        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", rc)
        calls = _spy_units(monkeypatch)
        res, _ = _run(CostLLM(cost_usd=0.001))
        assert len(attempts) == 2 and attempts[1] == 0
        assert calls[0][0]["cost_usd"] == 0.002
        assert res["cost_usd"] == pytest.approx(0.003)


class TestAReviewerWithoutCostKeys:
    """Plain contract stubs: their meta has no cost keys at all."""

    def test_every_unit_reads_none_and_empty(self, monkeypatch):
        calls = _spy_units(monkeypatch)
        _run(CostLLM(cost_usd=0.001))
        assert len(calls[0]) == 2
        for unit in calls[0]:
            assert "cost_usd" in unit and unit["cost_usd"] is None
            assert unit["cost_source"] == ""

    def test_a_legacy_dict_stub_reads_none_and_one_with_keys_passes_through(self, monkeypatch):
        replies = iter([
            {"findings": [], "error": "", "input_tokens": 100, "output_tokens": 50,
             "model": MODEL, "elapsed_ms": 1},
            {"findings": [], "error": "", "input_tokens": 100, "output_tokens": 50,
             "model": MODEL, "elapsed_ms": 1, "cost_usd": 0.007, "cost_source": "litellm"},
        ])
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk", lambda llm, files, **kw: next(replies),
        )
        calls = _spy_units(monkeypatch)
        _run(FakeLLM(), diff=TWO_FILE_DIFF, max_chunks=2, token_budget=1000, max_workers=1)
        first, second = calls[0][0], calls[0][1]
        assert (first["cost_usd"], first["cost_source"]) == (None, "")
        assert (second["cost_usd"], second["cost_source"]) == (0.007, "litellm")


@pytest.mark.usefixtures("cost_reviewer")
class TestTheRunTotal:
    def test_passthrough_reaches_the_run_record(self):
        res, _ = _run(CostLLM(cost_usd=0.001))
        assert res["cost_usd"] == pytest.approx(0.002)
        assert res["cost_estimated"] is False
        payload = _build_json_result(res)
        assert payload["cost_usd"] == pytest.approx(0.002)
        assert payload["cost_estimated"] is False

    def test_provider_cost_wins_over_a_table_entry(self):
        res, _ = _run(CostLLM(cost_usd=0.001), price_table=TABLE)
        assert res["cost_usd"] == pytest.approx(0.002)
        assert res["cost_estimated"] is False

    def test_estimate_reaches_the_run_record_and_is_flagged(self):
        res, _ = _run(CostLLM(cost_usd=None), price_table=TABLE)
        assert res["cost_usd"] == pytest.approx(2 * UNIT_ESTIMATE)
        assert res["cost_estimated"] is True

    def test_mixed_reported_and_estimated_is_estimated(self):
        res, _ = _run(CostLLM(cost_usd=0.001, sweep_cost_usd=None), price_table=TABLE)
        assert res["cost_usd"] == pytest.approx(0.001 + UNIT_ESTIMATE)
        assert res["cost_estimated"] is True

    def test_unknown_reaches_the_run_record_as_null(self, caplog):
        with caplog.at_level(logging.INFO, logger="prxref"):
            res, _ = _run(CostLLM(cost_usd=None))
        assert res["cost_usd"] is None
        assert res["cost_usd"] != 0
        assert res["cost_estimated"] is False
        assert UNKNOWN_LINE in caplog.text
        assert _build_json_result(res)["cost_usd"] is None

    def test_a_partial_sum_is_never_reported(self, caplog):
        with caplog.at_level(logging.INFO, logger="prxref"):
            res, _ = _run(CostLLM(cost_usd=0.001, sweep_cost_usd=None))
        assert res["cost_usd"] is None
        assert res["cost_estimated"] is False
        assert UNKNOWN_LINE in caplog.text

    def test_a_table_keyed_on_another_model_names_the_unpriced_one(self, caplog):
        other = {"test-model-2": costs.ModelPrice(input=1.0, output=2.0)}
        with caplog.at_level(logging.INFO, logger="prxref"):
            res, _ = _run(CostLLM(cost_usd=None), price_table=other)
        assert res["cost_usd"] is None
        assert UNKNOWN_LINE in caplog.text

    def test_a_priced_run_logs_no_unknown_line(self, caplog):
        with caplog.at_level(logging.INFO, logger="prxref"):
            _run(CostLLM(cost_usd=0.001))
        assert "cost unknown" not in caplog.text

    def test_reported_zero_is_a_real_zero(self):
        res, _ = _run(CostLLM(cost_usd=0.0))
        assert res["cost_usd"] == 0.0
        assert res["cost_usd"] is not None
        assert res["cost_estimated"] is False

    def test_empty_diff_costs_a_known_zero(self):
        llm = CostLLM(cost_usd=0.001)
        res, _ = _run(llm, diff="")
        assert llm.calls == 0
        assert res["cost_usd"] == 0.0
        assert res["cost_estimated"] is False

    def test_forge_failure_before_any_llm_call_costs_a_known_zero(self):
        llm = CostLLM(cost_usd=0.001)
        forge = FakeForge(diff=ONE_FILE_DIFF)
        forge.fail.add("get_diff")
        res = orchestrate_review(forge, REF, llm, post=False, price_table=TABLE)
        assert llm.calls == 0
        assert res["verdict"] == "Error"
        assert res["cost_usd"] == 0.0
        assert res["cost_estimated"] is False

    def test_total_llm_failure_with_no_completion_is_null(self):
        res, _ = _run(
            CostLLM(cost_usd=0.001, error=RuntimeError("no model")), price_table=TABLE,
        )
        assert res["verdict"] == "Error"
        assert res["cost_usd"] is None
        assert res["cost_estimated"] is False

    def test_parse_failed_units_still_count_their_cost(self, monkeypatch):
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _failing_chunks({
                "src/one.py": (TRUNCATED_REASON, 0.003),
                "other/two.py": ("", 0.001),
            }),
        )
        res, _ = _run(
            CostLLM(cost_usd=0.002), diff=TWO_FILE_DIFF, max_chunks=2, token_budget=1000,
        )
        assert res["chunks_failed"] == 1
        assert res["cost_usd"] == pytest.approx(0.003 + 0.001 + 0.002)
        assert res["cost_estimated"] is False

    def test_a_total_failure_after_answers_arrived_is_priced(self, monkeypatch):
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _failing_chunks({
                "src/one.py": (TRUNCATED_REASON, 0.003),
                "other/two.py": (TRUNCATED_REASON, 0.001),
            }),
        )
        res, _ = _run(
            CostLLM(cost_usd=0.002), diff=TWO_FILE_DIFF, max_chunks=2, token_budget=1000,
        )
        assert res["verdict"] == "Error"
        assert res["cost_usd"] == pytest.approx(0.006)

    def test_a_broken_price_table_object_cannot_fail_the_review(self, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref"):
            res, forge = _run(
                CostLLM(HAPPY_FINDINGS, cost_usd=None),
                diff=_added_file_diff("src/app.py", 20),
                price_table={MODEL: "garbage"}, post=True, post_cost=True,
            )
        assert res["verdict"] == "Request-Changes"
        assert res["cost_usd"] is None
        assert res["cost_estimated"] is False
        assert "cost accounting failed" in caplog.text
        assert _last_line(forge.summaries[0]).endswith("s · cost unknown")


@pytest.mark.usefixtures("cost_reviewer")
class TestPostedAttribution:
    def test_attribution_is_byte_identical_without_post_cost(self):
        assert (
            orchestrator._attribution("m", 150, 1200)
            == "Reviewed by prxref · model=m · 150 tok · 1.2s"
        )

    @pytest.mark.parametrize("cost_usd, table", [(0.001, None), (None, TABLE), (None, None)])
    def test_posted_bodies_carry_no_cost_without_post_cost(self, cost_usd, table):
        res, forge = _run(
            CostLLM(HAPPY_FINDINGS, cost_usd=cost_usd), post=True, price_table=table,
        )
        assert forge.summaries
        for body in forge.summaries:
            line = _last_line(body)
            assert PLAIN_ATTRIBUTION.fullmatch(line), line
        assert res["input_tokens"] + res["output_tokens"] == 300

    def test_post_cost_appends_after_the_existing_fields(self):
        _, forge = _run(CostLLM(cost_usd=0.001), post=True, post_cost=True)
        line = _last_line(forge.summaries[0])
        assert line.startswith(ATTRIBUTION_MARKER)
        assert line.endswith(" · $0.0020")
        assert PLAIN_ATTRIBUTION.fullmatch(line.removesuffix(" · $0.0020")), line
        assert line.index("model=") < line.index("$0.0020")

    def test_post_cost_renders_estimates_with_a_tilde(self):
        _, forge = _run(
            CostLLM(cost_usd=None), post=True, post_cost=True, price_table=TABLE,
        )
        assert _last_line(forge.summaries[0]).endswith(" · ~$0.0004 (est.)")

    def test_post_cost_renders_unknown_as_words(self):
        _, forge = _run(CostLLM(cost_usd=None), post=True, post_cost=True)
        assert _last_line(forge.summaries[0]).endswith(" · cost unknown")

    def test_post_cost_reaches_the_error_notice(self, monkeypatch):
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _failing_chunks({
                "src/one.py": (TRUNCATED_REASON, 0.003),
                "other/two.py": (TRUNCATED_REASON, 0.001),
            }),
        )
        res, forge = _run(
            CostLLM(cost_usd=0.002), diff=TWO_FILE_DIFF, max_chunks=2,
            token_budget=1000, post=True, post_cost=True,
        )
        assert res["verdict"] == "Error"
        assert len(forge.summaries) == 1
        assert _last_line(forge.summaries[0]).endswith(" · $0.0060")

    def test_inline_comments_never_carry_cost(self):
        _, forge = _run(
            CostLLM(HAPPY_FINDINGS, cost_usd=0.001), post=True, post_cost=True,
        )
        comments = [c for batch in forge.inline_batches for c in batch]
        assert comments
        label = costs.cost_label(0.002, False)
        assert _last_line(forge.summaries[0]).endswith(f" · {label}")
        for comment in comments:
            assert "$" not in comment.body
            assert "cost unknown" not in comment.body


@pytest.mark.usefixtures("cost_reviewer")
class TestTraceEvents:
    def test_run_trace_events_carry_cost(self, tmp_path):
        _run(CostLLM(cost_usd=None), tmp_path=tmp_path, price_table=TABLE)
        (ok,) = _events(tmp_path, "run", "ok")
        assert ok["meta"]["cost_usd"] == pytest.approx(2 * UNIT_ESTIMATE)
        assert ok["meta"]["cost_estimated"] is True

    def test_chunk_and_sweep_ok_events_carry_the_reported_figure(self, tmp_path):
        _run(CostLLM(cost_usd=0.001, sweep_cost_usd=0.004), tmp_path=tmp_path)
        (chunk,) = _events(tmp_path, "chunk", "ok")
        (sweep,) = _events(tmp_path, "sweep", "ok")
        assert chunk["meta"]["cost_usd"] == 0.001
        assert sweep["meta"]["cost_usd"] == 0.004

    def test_a_unit_estimate_is_never_traced_as_reported(self, tmp_path):
        _run(CostLLM(cost_usd=None), tmp_path=tmp_path, price_table=TABLE)
        (chunk,) = _events(tmp_path, "chunk", "ok")
        (sweep,) = _events(tmp_path, "sweep", "ok")
        assert chunk["meta"]["cost_usd"] is None
        assert sweep["meta"]["cost_usd"] is None
