"""The run record: one choke point stamps every exit of ``orchestrate_review``.

``_run_record`` wraps all seven returns — the ``get_pr``, ``get_diff``,
``parse_unified_diff`` and ``build_chunks`` failures, the empty-diff
summary-only exit, the total LLM failure, and the completed review — so the
0.14 run-record keys (``cost_usd``, ``cost_estimated``, ``review_rules``,
``ticket_context``, ``spec_grounding``, ``size_advisory``) are present on every
one of them, null when their feature is off, and ``replay`` rides a replay run
only. Each exit is driven below, not just the happy one, the way
``TestRunTrace`` in tests/test_orchestrator.py walks them.

The cost and size hooks' bodies (``_stamp_run_cost``, ``_size_advisory``) are
driven for real in tests/test_issue_67_cost.py and tests/test_size_advisory.py,
so these tests prove the hooks are WIRED by monkeypatching them, and pin only
the facts that hold whatever the bodies compute.
"""
from __future__ import annotations

import ast
import inspect
import json
import logging
import re
import sys
import textwrap

import pytest

from prxref import costs, orchestrator
from prxref.cli import _build_json_result
from prxref.forges.base import ATTRIBUTION_MARKER
from prxref.orchestrator import orchestrate_review
from prxref.triage import parse_unified_diff
from tests.test_orchestrator import (
    HAPPY_FINDINGS,
    REF,
    FakeForge,
    FakeLLM,
    _added_file_diff,
)

pytestmark = pytest.mark.usefixtures("contract_stubs")

BASE_KEYS = {
    "verdict", "findings_active", "findings_dropped", "chunk_count",
    "chunks_reviewed", "chunks_failed", "elapsed_ms", "input_tokens",
    "output_tokens", "posted", "sampling",
}
RECORD_KEYS = {
    "cost_usd", "cost_estimated", "review_rules", "ticket_context",
    "spec_grounding", "size_advisory",
}
NULL_WHEN_OFF = ("review_rules", "ticket_context", "spec_grounding", "size_advisory", "prompt_templates")

REPLAY = {
    "base_sha": "b" * 40,
    "head_sha": "a" * 40,
    "threads": "hidden",
    "diff_file": None,
    "description": "pinned",
    "as_of": "2026-05-01T09:30:00Z",
    "as_of_source": "first-review",
}

PATHS = (
    "get_pr", "get_diff", "parse", "build_chunks",
    "empty_diff", "total_failure", "success",
)
BEFORE_ANY_REQUEST = ("get_pr", "get_diff", "parse", "build_chunks", "empty_diff")
AFTER_REQUESTS = ("total_failure", "success")
BEFORE_THE_PARSE = ("get_pr", "get_diff", "parse")
VERDICT = {
    "get_pr": "Error", "get_diff": "Error", "parse": "Error",
    "build_chunks": "Error", "empty_diff": "Approved",
    "total_failure": "Error", "success": "Request-Changes",
}

ADVISORY = (
    "This PR changes 20 lines in 1 file, above the team guideline of 5 lines. "
    "Consider splitting it."
)
TRIGGERED = {
    "changed_lines": 20, "changed_files": 1, "lines_limit": 5,
    "files_limit": None, "triggered": True, "message": ADVISORY,
}
UNTRIGGERED = {
    "changed_lines": 20, "changed_files": 1, "lines_limit": 50,
    "files_limit": None, "triggered": False, "message": None,
}
PLAIN_ATTRIBUTION = re.compile(
    rf"{re.escape(ATTRIBUTION_MARKER)} · model=\S+ · \d+ tok · \d+\.\ds"
)


def _boom(*args, **kwargs):
    raise ValueError("boom parse")


def _run(monkeypatch, path, tmp_path, **kw):
    """Drive ``orchestrate_review`` out through one named exit.

    Returns ``(result, forge, trace_events)``. ``post`` defaults to False;
    a test that reads the posted bodies passes ``post=True``.
    """
    forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
    llm = FakeLLM(findings_by_path=HAPPY_FINDINGS)
    if path in ("get_pr", "get_diff"):
        forge.fail.add(path)
    elif path == "parse":
        monkeypatch.setattr(orchestrator, "parse_unified_diff", _boom)
    elif path == "build_chunks":
        kw.setdefault("max_chunks", 0)
    elif path == "empty_diff":
        forge.diff = ""
    elif path == "total_failure":
        llm = FakeLLM(error=RuntimeError("no model"))
    tmp_path.mkdir(parents=True, exist_ok=True)
    trace = tmp_path / "run.jsonl"
    kw.setdefault("post", False)
    res = orchestrate_review(forge, REF, llm, trace_file=str(trace), **kw)
    events = [json.loads(x) for x in trace.read_text().splitlines() if x.strip()]
    return res, forge, events


def _run_events(events, phase=None):
    return [
        e for e in events
        if e["node"] == "run" and (phase is None or e["phase"] == phase)
    ]


def _closing(events):
    closing = [e for e in _run_events(events) if e["phase"] in ("ok", "fail")]
    assert len(closing) == 1, closing
    return closing[0]


def _last_line(body: str) -> str:
    return body.rstrip("\n").splitlines()[-1]


class TestOneChokePoint:
    """Every return goes through ``_run_record``; a new exit cannot skip it."""

    def _returns(self):
        source = textwrap.dedent(inspect.getsource(orchestrator.orchestrate_review))
        fn = ast.parse(source).body[0]
        found = []

        def walk(node):
            for child in ast.iter_child_nodes(node):
                if isinstance(
                    child,
                    (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef),
                ):
                    continue
                if isinstance(child, ast.Return):
                    found.append(child)
                walk(child)

        walk(fn)
        return found

    def test_every_return_is_a_run_record_call(self):
        returns = self._returns()
        assert len(returns) == 7, (
            "orchestrate_review has a new exit: wrap it in _run_record and add "
            "it to PATHS in this module"
        )
        for ret in returns:
            call = ret.value
            assert isinstance(call, ast.Call), ast.dump(ret)
            assert isinstance(call.func, ast.Name) and call.func.id == "_run_record"
            assert isinstance(call.args[1], ast.Name) and call.args[1].id == "run_inputs"

    def test_the_seven_paths_here_reach_seven_distinct_returns(self, monkeypatch, tmp_path):
        """So the parametrised tests below really do cover every exit."""
        real = orchestrator._run_record
        callers: list[tuple[str, int]] = []

        def spy(result, run_inputs):
            callers.append(sys._getframe(1).f_code.co_name)
            callers.append(sys._getframe(1).f_lineno)
            return real(result, run_inputs)

        monkeypatch.setattr(orchestrator, "_run_record", spy)
        for name in PATHS:
            with monkeypatch.context() as m:
                _run(m, name, tmp_path / name)
        names, lines = callers[0::2], callers[1::2]
        assert set(names) == {"orchestrate_review"}
        assert len(lines) == 7
        assert len(set(lines)) == 7


class TestTheRecordKeys:
    @pytest.mark.parametrize("path", PATHS)
    def test_every_exit_carries_the_always_present_keys(self, monkeypatch, tmp_path, path):
        res, _, _ = _run(monkeypatch, path, tmp_path)
        assert res["verdict"] == VERDICT[path]
        assert set(res) == BASE_KEYS | RECORD_KEYS | {"prompt_templates"}
        for key in NULL_WHEN_OFF:
            assert res[key] is None, key
        assert res["cost_estimated"] is False

    @pytest.mark.parametrize("path", BEFORE_ANY_REQUEST)
    def test_cost_is_zero_before_any_llm_request(self, monkeypatch, tmp_path, path):
        res, _, _ = _run(monkeypatch, path, tmp_path)
        assert res["cost_usd"] == 0.0
        assert type(res["cost_usd"]) is float

    @pytest.mark.parametrize("path", AFTER_REQUESTS)
    def test_cost_is_unknown_not_zero_once_requests_reported_none(
        self, monkeypatch, tmp_path, path
    ):
        """FakeLLM reports no cost and no price table is set: unknown, never 0."""
        res, _, _ = _run(monkeypatch, path, tmp_path)
        assert res["cost_usd"] is None
        assert res["cost_estimated"] is False

    @pytest.mark.parametrize("path", PATHS)
    def test_a_normal_run_has_no_replay_key(self, monkeypatch, tmp_path, path):
        res, _, _ = _run(monkeypatch, path, tmp_path)
        assert "replay" not in res

    @pytest.mark.parametrize("path", PATHS)
    def test_the_replay_stamp_rides_every_exit(self, monkeypatch, tmp_path, path):
        res, _, _ = _run(monkeypatch, path, tmp_path, replay=dict(REPLAY))
        assert set(res) == BASE_KEYS | RECORD_KEYS | {"prompt_templates", "replay"}
        assert res["replay"] == REPLAY
        assert list(res["replay"]) == [
            "base_sha", "head_sha", "threads", "diff_file", "description", "as_of", "as_of_source",
        ]

    def test_the_stamp_is_a_copy_of_the_callers_mapping(self, monkeypatch, tmp_path):
        stamp = dict(REPLAY)
        res, _, _ = _run(monkeypatch, "success", tmp_path, replay=stamp)
        assert res["replay"] is not stamp
        stamp["threads"] = "shown"
        assert res["replay"]["threads"] == "hidden"

    @pytest.mark.parametrize("path", ("get_diff", "empty_diff", "success"))
    def test_json_payload_is_normal_keys_plus_replay(self, monkeypatch, tmp_path, path):
        """Through the real consumer: ``--format json`` gains exactly ``replay``."""
        normal, _, _ = _run(monkeypatch, path, tmp_path / "normal")
        replayed, _, _ = _run(monkeypatch, path, tmp_path / "replay", replay=dict(REPLAY))
        normal_payload = _build_json_result(normal)
        replay_payload = _build_json_result(replayed)
        assert "replay" not in normal_payload
        assert set(replay_payload) == set(normal_payload) | {"replay"}
        assert replay_payload["replay"] == REPLAY
        for key in RECORD_KEYS:
            assert key in normal_payload, key
        assert normal_payload["size_advisory"] is None
        assert normal_payload["cost_estimated"] is False


class TestRunRecordHelper:
    def test_a_key_the_exit_already_carries_wins(self):
        result = {"verdict": "Approved", "cost_usd": 1.5}
        out = orchestrator._run_record(result, {"cost_usd": 0.0, "size_advisory": None})
        assert out is result
        assert out == {"verdict": "Approved", "cost_usd": 1.5, "size_advisory": None}

    def test_replay_none_is_never_written(self):
        out = orchestrator._run_record({}, {"replay": None, "review_rules": None})
        assert out == {"review_rules": None}

    def test_replay_is_written_as_a_copy(self):
        stamp = dict(REPLAY)
        out = orchestrator._run_record({}, {"replay": stamp})
        assert out["replay"] == REPLAY
        assert out["replay"] is not stamp


class TestTraceMeta:
    @pytest.mark.parametrize("path", PATHS)
    def test_the_closing_run_event_carries_the_cost(self, monkeypatch, tmp_path, path):
        res, _, events = _run(monkeypatch, path, tmp_path)
        meta = _closing(events)["meta"]
        assert meta["cost_usd"] == res["cost_usd"]
        assert meta["cost_estimated"] is False
        expected = 0.0 if path in BEFORE_ANY_REQUEST else None
        assert meta["cost_usd"] == expected

    @pytest.mark.parametrize("path", ("get_pr", "empty_diff", "success"))
    def test_run_start_carries_the_replay_stamp(self, monkeypatch, tmp_path, path):
        _, _, events = _run(monkeypatch, path, tmp_path, replay=dict(REPLAY))
        start = _run_events(events, "start")
        assert len(start) == 1
        assert start[0]["meta"]["replay"] == REPLAY

    @pytest.mark.parametrize("path", ("get_pr", "empty_diff", "success"))
    def test_run_start_has_no_replay_on_a_normal_run(self, monkeypatch, tmp_path, path):
        _, _, events = _run(monkeypatch, path, tmp_path)
        start = _run_events(events, "start")
        assert len(start) == 1
        assert "replay" not in start[0]["meta"]
        assert set(start[0]["meta"]) == {"forge", "url", "number", "sampling"}


class TestCostHook:
    """``_stamp_run_cost`` runs once, after the sweep, before the total-failure exit."""

    def _spy(self, monkeypatch, *, usd=None, estimated=False, raises=None):
        calls = []

        def stamp(run_inputs, units, price_table):
            calls.append({"units": list(units), "price_table": price_table})
            if raises is not None:
                raise raises
            run_inputs["cost_usd"] = usd
            run_inputs["cost_estimated"] = estimated

        monkeypatch.setattr(orchestrator, "_stamp_run_cost", stamp)
        return calls

    def test_called_once_with_every_unit_and_an_empty_table(self, monkeypatch, tmp_path):
        calls = self._spy(monkeypatch)
        _run(monkeypatch, "success", tmp_path)
        assert len(calls) == 1
        units = calls[0]["units"]
        assert len(units) == 2
        assert all("input_tokens" in u and "model" in u for u in units)
        assert calls[0]["price_table"] == {}

    def test_the_parsed_price_table_reaches_it_untouched(self, monkeypatch, tmp_path):
        calls = self._spy(monkeypatch)
        table = {"test-model-1": costs.ModelPrice(input=1.0, output=2.0)}
        _run(monkeypatch, "success", tmp_path, price_table=table)
        assert calls[0]["price_table"] is table

    @pytest.mark.parametrize("path", BEFORE_ANY_REQUEST)
    def test_not_called_when_no_request_went_out(self, monkeypatch, tmp_path, path):
        calls = self._spy(monkeypatch, usd=9.0)
        res, _, _ = _run(monkeypatch, path, tmp_path)
        assert calls == []
        assert res["cost_usd"] == 0.0

    @pytest.mark.parametrize("path", AFTER_REQUESTS)
    def test_its_value_reaches_the_record_and_the_trace(self, monkeypatch, tmp_path, path):
        self._spy(monkeypatch, usd=0.25, estimated=True)
        res, _, events = _run(monkeypatch, path, tmp_path)
        assert res["cost_usd"] == 0.25
        assert res["cost_estimated"] is True
        meta = _closing(events)["meta"]
        assert meta["cost_usd"] == 0.25
        assert meta["cost_estimated"] is True

    def test_its_label_ends_the_summary_and_its_inline_refresh(self, monkeypatch, tmp_path):
        """The refresh re-post is the render that is easy to miss."""
        self._spy(monkeypatch, usd=0.25, estimated=True)
        _, forge, _ = _run(
            monkeypatch, "success", tmp_path,
            post=True, post_cost=True, max_inline_comments=1,
        )
        assert len(forge.summaries) == 2
        label = costs.cost_label(0.25, True)
        assert label == "~$0.2500 (est.)"
        for body in forge.summaries:
            assert _last_line(body).endswith(f"s · {label}"), body

    def test_the_total_failure_notice_carries_the_stamped_cost(self, monkeypatch, tmp_path):
        self._spy(monkeypatch, usd=0.5)
        _, forge, _ = _run(monkeypatch, "total_failure", tmp_path, post=True, post_cost=True)
        assert len(forge.summaries) == 1
        assert _last_line(forge.summaries[0]).endswith("s · $0.5000")

    def test_a_crashing_stamp_never_fails_the_review(self, monkeypatch, tmp_path, caplog):
        self._spy(monkeypatch, raises=RuntimeError("bad table"))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            res, forge, events = _run(
                monkeypatch, "success", tmp_path, post=True, post_cost=True,
            )
        assert res["verdict"] == "Request-Changes"
        assert res["cost_usd"] is None
        assert res["cost_estimated"] is False
        assert "cost accounting failed" in caplog.text
        assert _last_line(forge.summaries[0]).endswith("s · cost unknown")
        assert _closing(events)["phase"] == "ok"


class TestCostLabelOnPostedBodies:
    @pytest.mark.parametrize("path", ("get_pr", "get_diff", "parse", "build_chunks"))
    def test_a_pre_request_error_notice_says_zero(self, monkeypatch, tmp_path, path):
        _, forge, _ = _run(monkeypatch, path, tmp_path, post=True, post_cost=True)
        assert len(forge.summaries) == 1
        assert _last_line(forge.summaries[0]).endswith("s · $0.00")

    def test_the_empty_diff_summary_says_zero(self, monkeypatch, tmp_path):
        _, forge, _ = _run(monkeypatch, "empty_diff", tmp_path, post=True, post_cost=True)
        assert _last_line(forge.summaries[0]).endswith("s · $0.00")

    @pytest.mark.parametrize("path", AFTER_REQUESTS)
    def test_an_unknown_cost_says_so(self, monkeypatch, tmp_path, path):
        _, forge, _ = _run(monkeypatch, path, tmp_path, post=True, post_cost=True)
        assert _last_line(forge.summaries[0]).endswith("s · cost unknown")

    @pytest.mark.parametrize("path", ("get_diff", "build_chunks", "empty_diff", "total_failure", "success"))
    def test_post_cost_off_leaves_every_attribution_as_it_was(self, monkeypatch, tmp_path, path):
        _, forge, _ = _run(
            monkeypatch, path, tmp_path / "off", post=True, max_inline_comments=1,
        )
        assert forge.summaries
        for body in forge.summaries:
            assert PLAIN_ATTRIBUTION.fullmatch(_last_line(body)), body
        _, forge_on, _ = _run(
            monkeypatch, path, tmp_path / "on", post=True, post_cost=True,
            max_inline_comments=1,
        )
        for body in forge_on.summaries:
            assert not PLAIN_ATTRIBUTION.fullmatch(_last_line(body)), body


class TestSizeHook:
    def _patch(self, monkeypatch, value=None, raises=None):
        calls = []

        def advisory(files, *, lines_limit, files_limit, ignore_globs=()):
            calls.append({
                "paths": [f.path for f in files], "lines_limit": lines_limit,
                "files_limit": files_limit, "ignore_globs": ignore_globs,
            })
            if raises is not None:
                raise raises
            return value

        monkeypatch.setattr(orchestrator, "_size_advisory", advisory)
        return calls

    def test_the_hook_sees_the_parsed_files_and_the_knobs(self, monkeypatch, tmp_path):
        calls = self._patch(monkeypatch)
        _run(
            monkeypatch, "success", tmp_path,
            size_warn_lines=5, size_ignore_globs=("docs/*",),
        )
        assert calls == [{
            "paths": ["src/app.py"], "lines_limit": 5, "files_limit": None,
            "ignore_globs": ("docs/*",),
        }]

    @pytest.mark.parametrize("path", BEFORE_THE_PARSE)
    def test_an_exit_before_the_parse_carries_null(self, monkeypatch, tmp_path, path):
        calls = self._patch(monkeypatch, TRIGGERED)
        res, _, _ = _run(monkeypatch, path, tmp_path, size_warn_lines=5)
        assert calls == []
        assert res["size_advisory"] is None

    @pytest.mark.parametrize("path", ("build_chunks", "empty_diff", "total_failure", "success"))
    def test_every_exit_after_the_parse_carries_the_stats(self, monkeypatch, tmp_path, path):
        self._patch(monkeypatch, TRIGGERED)
        res, _, _ = _run(monkeypatch, path, tmp_path, size_warn_lines=5)
        assert res["size_advisory"] == TRIGGERED

    def test_a_triggered_advisory_heads_the_summary_and_its_refresh(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, TRIGGERED)
        _, forge, _ = _run(
            monkeypatch, "success", tmp_path,
            post=True, size_warn_lines=5, max_inline_comments=1,
        )
        assert len(forge.summaries) == 2
        for body in forge.summaries:
            head, rest = body.split("\n\n", 1)
            assert head == f"> ⚠️ {ADVISORY}"
            assert rest.startswith("🤖 **prxref review"), rest

    def test_a_triggered_advisory_heads_the_empty_diff_summary(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, TRIGGERED)
        _, forge, _ = _run(
            monkeypatch, "empty_diff", tmp_path, post=True, size_warn_lines=5,
        )
        assert forge.summaries[0].startswith(f"> ⚠️ {ADVISORY}\n\n🤖 ")

    @pytest.mark.parametrize("path", ("build_chunks", "total_failure"))
    def test_an_error_notice_never_carries_it(self, monkeypatch, tmp_path, path):
        self._patch(monkeypatch, TRIGGERED)
        res, forge, _ = _run(monkeypatch, path, tmp_path, post=True, size_warn_lines=5)
        assert res["size_advisory"] == TRIGGERED
        assert len(forge.summaries) == 1
        assert ADVISORY not in forge.summaries[0]
        assert forge.summaries[0].startswith("🤖 **prxref review — Error**")

    def test_an_untriggered_advisory_adds_no_line(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, UNTRIGGERED)
        res, forge, _ = _run(
            monkeypatch, "success", tmp_path, post=True, size_warn_lines=50,
        )
        assert res["size_advisory"] == UNTRIGGERED
        assert forge.summaries[0].startswith("🤖 **prxref review")

    def test_unset_thresholds_leave_the_record_null_and_the_summary_alone(
        self, monkeypatch, tmp_path
    ):
        res, forge, _ = _run(monkeypatch, "success", tmp_path, post=True)
        assert res["size_advisory"] is None
        assert forge.summaries[0].startswith("🤖 **prxref review")

    def test_a_crashing_hook_never_fails_the_review(self, monkeypatch, tmp_path, caplog):
        self._patch(monkeypatch, raises=RuntimeError("bad glob"))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            res, forge, _ = _run(
                monkeypatch, "success", tmp_path, post=True, size_warn_lines=5,
            )
        assert res["verdict"] == "Request-Changes"
        assert res["size_advisory"] is None
        assert "size advisory failed" in caplog.text
        assert forge.summaries[0].startswith("🤖 **prxref review")


class TestSizeAdvisoryLine:
    @pytest.mark.parametrize("stats", [None, {}, UNTRIGGERED, {"message": ""}])
    def test_no_message_is_no_line(self, stats):
        assert orchestrator._size_advisory_line(stats) == ""

    def test_a_message_is_one_blockquote_and_a_blank_line(self):
        assert orchestrator._size_advisory_line(TRIGGERED) == f"> ⚠️ {ADVISORY}\n\n"


class TestCostLabel:
    @pytest.mark.parametrize("usd, estimated", [(0.0, False), (None, False), (0.5, True)])
    def test_off_is_always_empty(self, usd, estimated):
        run_inputs = {"cost_usd": usd, "cost_estimated": estimated}
        assert orchestrator._cost_label(run_inputs, False) == ""

    @pytest.mark.parametrize("usd, estimated, label", [
        (0.0, False, "$0.00"),
        (None, False, "cost unknown"),
        (0.0007, False, "$0.0007"),
        (0.0007, True, "~$0.0007 (est.)"),
    ])
    def test_on_is_the_costs_label(self, usd, estimated, label):
        run_inputs = {"cost_usd": usd, "cost_estimated": estimated}
        assert orchestrator._cost_label(run_inputs, True) == label


class TestHooksAreOffByDefault:
    """The facts that hold for the inert stubs AND for the feature bodies."""

    def test_no_size_stats_when_both_limits_are_unset(self):
        files = parse_unified_diff(_added_file_diff("src/app.py", 20))
        assert orchestrator._size_advisory(files, lines_limit=None, files_limit=None) is None

    def test_units_that_report_nothing_and_no_table_leave_the_cost_unknown(self):
        run_inputs = {"cost_usd": 0.0, "cost_estimated": False}
        units = [
            {"findings": [], "error": "", "input_tokens": 100, "output_tokens": 50,
             "model": "test-model-1", "elapsed_ms": 1},
            {"findings": [], "error": "", "input_tokens": 0, "output_tokens": 0,
             "model": "", "elapsed_ms": 0},
        ]
        orchestrator._stamp_run_cost(run_inputs, units, {})
        assert run_inputs["cost_usd"] is None
        assert run_inputs["cost_estimated"] is False
