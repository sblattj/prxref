"""A partial review names what it did not review (issue #72).

A run where some review units fail still completes, but its record carries
``failed_chunks``: one ``{unit, kind, files, error}`` per failed unit, in
review order, so a consumer reading the verdict can also see which files got
no review. ``--format json`` always has the key, and the text summary prints
the files after the ``coverage:`` line, plus a deadline hint when a unit
timed out. The default per-model deadline is 120 s.
"""
from __future__ import annotations

import io
import json

from prxref import cli, config
from prxref.llm_backends import DEFAULT_TIMEOUT
from prxref.orchestrator import _failed_chunks, orchestrate_review
from tests.test_orchestrator import REF, FakeForge, multi_chunk_diff
from tests.test_parse_retry_orchestrator import BAD, VALID, ScriptedLLM


def _review(texts, n_files=3):
    return orchestrate_review(
        FakeForge(diff=multi_chunk_diff(n_files)), REF, ScriptedLLM(texts), post=False, max_workers=1,
    )


class TestRecord:
    def test_a_clean_run_has_no_failed_units(self):
        res = _review([VALID, VALID, VALID, VALID])
        assert res["chunks_failed"] == 0
        assert res["failed_chunks"] == []

    def test_a_failed_chunk_is_named_with_its_files(self):
        res = _review([VALID, BAD, VALID, VALID])
        assert res["chunks_failed"] == 1
        assert res["verdict"] != "Error"
        [unit] = res["failed_chunks"]
        assert unit["unit"] == 2
        assert unit["kind"] == "chunk"
        assert unit["files"] == ["src/big2.py"]
        assert "JSONDecodeError" in unit["error"]

    def test_a_failed_sweep_is_a_sweep_unit_with_no_files(self):
        res = _review([VALID, VALID, VALID, BAD])
        assert res["failed_chunks"] == [
            {"unit": 4, "kind": "sweep", "files": [], "error": res["failed_chunks"][0]["error"]},
        ]

    def test_a_total_failure_names_every_chunk(self):
        res = _review([BAD, BAD, VALID], n_files=2)
        assert res["verdict"] == "Error"
        assert [u["files"] for u in res["failed_chunks"]] == [["src/big1.py"], ["src/big2.py"]]


class TestHelper:
    def test_files_follow_chunk_order_and_plain_strings_pass_through(self):
        results = [{"error": "x"}, {"error": ""}, {"error": "timeout"}]
        assert _failed_chunks([["a", "b"], ["c"]], results) == [
            {"unit": 1, "kind": "chunk", "files": ["a", "b"], "error": "x"},
            {"unit": 3, "kind": "sweep", "files": [], "error": "timeout"},
        ]


class TestOutput:
    def test_json_carries_the_key(self):
        payload = cli._build_json_result(_review([VALID, BAD, VALID, VALID]))
        assert json.loads(json.dumps(payload))["failed_chunks"][0]["files"] == ["src/big2.py"]

    def test_text_names_the_files_and_the_sweep(self):
        out = io.StringIO()
        cli._print_summary({
            "verdict": "Approved", "chunks_reviewed": 2, "chunks_failed": 2,
            "failed_chunks": [
                {"unit": 1, "kind": "chunk", "files": ["a.py", "b.py"], "error": "boom"},
                {"unit": 4, "kind": "sweep", "files": [], "error": "boom"},
            ],
        }, 1.0, verbose=False, out=out)
        lines = out.getvalue().splitlines()
        assert lines[:4] == [
            "verdict: Approved", "coverage: 2/4 chunks reviewed",
            "not reviewed: a.py, b.py", "not reviewed: cross-file sweep",
        ]
        assert not any(line.startswith("hint:") for line in lines)

    def test_a_deadline_failure_prints_the_timeout_hint(self):
        out = io.StringIO()
        cli._print_summary({
            "verdict": "Approved", "chunks_reviewed": 2, "chunks_failed": 1,
            "failed_chunks": [{"unit": 1, "kind": "chunk", "files": ["a.py"], "error": "m1: timeout (ReadTimeout)"}],
        }, 1.0, verbose=False, out=out)
        assert "hint: a review unit hit the model deadline; raise --timeout or PRXREF_LLM_TIMEOUT" in out.getvalue()


class TestDefaultDeadline:
    def test_the_default_is_120_seconds(self):
        assert config._DEFAULTS["llm_timeout"] == 120.0
        assert DEFAULT_TIMEOUT == 120.0
