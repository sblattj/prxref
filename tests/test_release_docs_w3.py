"""The ``forge.get_diff`` trace span counts UTF-8 bytes, not characters.

``orchestrate_review`` records the fetched diff's size as ``bytes`` in the
closing event of its ``forge.get_diff`` span. The diff is a ``str``, so its
``len`` is a count of code points: a diff with a non-ASCII character or a
byte-order mark reads short. A live Azure DevOps PR measured 15647 from
``len`` against 15651 UTF-8 bytes. The harness is ``TestRunTrace._run`` of
tests/test_orchestrator.py: a real ``orchestrate_review`` run with a trace
file, whose JSONL is parsed back.
"""
from __future__ import annotations

import json

import pytest

from prxref.orchestrator import orchestrate_review
from tests.test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff


def _run_trace(tmp_path, diff: str) -> list[dict]:
    target = tmp_path / "run.jsonl"
    orchestrate_review(
        FakeForge(diff=diff), REF, FakeLLM(findings_by_path={}),
        trace_file=str(target), post=False,
    )
    return [json.loads(x) for x in target.read_text(encoding="utf-8").splitlines() if x.strip()]


def _get_diff_bytes(events: list[dict]) -> int:
    closing = [e for e in events if e["node"] == "forge.get_diff" and e["phase"] == "ok"]
    assert len(closing) == 1, closing
    return closing[0]["meta"]["bytes"]


@pytest.mark.usefixtures("contract_stubs")
class TestGetDiffSpanCountsBytes:
    def test_a_non_ascii_diff_is_counted_in_utf8_bytes(self, tmp_path):
        diff = _added_file_diff("src/app.py", 5).replace("+data 1\n", "+data 1 café\n", 1)
        assert "é" in diff
        assert len(diff.encode("utf-8")) == len(diff) + 1
        recorded = _get_diff_bytes(_run_trace(tmp_path, diff))
        assert recorded == len(diff.encode("utf-8"))
        assert recorded != len(diff)

    def test_a_byte_order_mark_is_counted_as_its_three_bytes(self, tmp_path):
        diff = _added_file_diff("src/app.py", 5).replace("+data 1\n", "+﻿data 1\n", 1)
        recorded = _get_diff_bytes(_run_trace(tmp_path, diff))
        assert recorded == len(diff.encode("utf-8")) == len(diff) + 2

    def test_an_ascii_diff_counts_the_same_either_way(self, tmp_path):
        """Control: for pure ASCII the two counts agree, so the fix changes nothing there."""
        diff = _added_file_diff("src/app.py", 5)
        recorded = _get_diff_bytes(_run_trace(tmp_path, diff))
        assert recorded == len(diff) == len(diff.encode("utf-8"))
