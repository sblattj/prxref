"""Orchestrator tests: FakeForge + FakeLLM, no network, no reviewer-file dependency.

``src/prxref/reviewer.py`` is owned by a parallel seat; these tests pin the
orchestrator-side contract (``review_chunk(llm, files, pr)`` dict return,
``load_prompt`` template) with a stub installed only when the real module
is absent, and an autouse fixture that pins behavior either way.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import sys
import threading
import types

import pytest

import prxref
from prxref.forges.base import InlineComment, PRData, PRRef, Thread
from prxref.llm import InvokeResult
from prxref.triage import Finding

SUMMARY_TEMPLATE = (
    "🤖 **prxref review — {verdict}**\n\n"
    "PR: {title}\n\n"
    "Files reviewed: {file_count} · errors: {error_count} · "
    "warnings: {warning_count} · notes: {note_count}\n\n"
    "{findings}\n\n{attribution}"
)


def _contract_review_chunk(
    llm, files, *, pr_title="", pr_description="", repo_hint="", max_tokens=None
):
    result = llm.invoke(
        system="review the chunk",
        user=json.dumps([f.path for f in files]),
    )
    payload = json.loads(result.text)
    if isinstance(payload, dict):
        payload = payload.get("findings", [])
    findings = [
        Finding(
            file=item["file"],
            line=item["line"],
            severity=item["severity"],
            confidence=item["confidence"],
            title=item["title"],
            body=item["body"],
        )
        for item in payload
    ]
    return findings, {
        "escalations": [],
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "model": result.model,
        "elapsed_ms": 1,
        "error": "",
    }


def _contract_load_prompt(name):
    assert name == "summary"
    return SUMMARY_TEMPLATE


def _install_reviewer_stub_if_missing() -> None:
    if importlib.util.find_spec("prxref.reviewer") is not None:
        return
    stub = types.ModuleType("prxref.reviewer")
    stub.review_chunk = _contract_review_chunk
    stub.load_prompt = _contract_load_prompt
    sys.modules["prxref.reviewer"] = stub
    prxref.reviewer = stub


_install_reviewer_stub_if_missing()  # noqa: E402 (must run before orchestrator import)

from prxref import orchestrator  # noqa: E402
from prxref.orchestrator import orchestrate_review  # noqa: E402

REF = PRRef(
    forge="fake", host="fake.test", owner="acme", repo="widget",
    number=7, url="https://fake.test/acme/widget/pull/7",
)


def make_pr(title: str = "Add widget") -> PRData:
    return PRData(
        title=title, description="does things", author="alice",
        source_branch="feature/widget", target_branch="main",
        source_sha="a" * 40, target_sha="b" * 40, raw={},
    )


def _added_file_diff(path: str, n_lines: int) -> str:
    body = "\n".join(f"+line {i}" for i in range(1, n_lines + 1))
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{n_lines} @@\n"
        f"{body}\n"
    )


TWO_FILE_DIFF = _added_file_diff("src/one.py", 400) + _added_file_diff("other/two.py", 400)


class FakeLLM:
    def __init__(self, findings_by_path: dict | str | None = None, error: Exception | None = None):
        self._raw_text = findings_by_path if isinstance(findings_by_path, str) else None
        self.findings_by_path = findings_by_path if isinstance(findings_by_path, dict) else {}
        self.error = error
        self.calls = 0
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        with self._lock:
            self.calls += 1
        if self.error is not None:
            raise self.error
        if self._raw_text is not None:
            text = self._raw_text
        else:
            paths = json.loads(user)
            payload = [f for p in paths for f in self.findings_by_path.get(p, [])]
            text = json.dumps({"findings": payload})
        return InvokeResult(
            text=text,
            input_tokens=100,
            output_tokens=50,
            model="test-model-1",
            backend="fake",
            elapsed_ms=1,
        )


class FakeForge:
    name = "fake"

    def __init__(self, pr: PRData | None = None, diff: str = "", threads: list | None = None):
        self.pr = pr or make_pr()
        self.diff = diff
        self.threads = threads or []
        self.fail: set[str] = set()
        self.summaries: list[str] = []
        self.inline_batches: list[list[InlineComment]] = []

    @staticmethod
    def parse_pr_url(url: str) -> PRRef | None:
        return None

    def get_pr(self, ref: PRRef) -> PRData:
        if "get_pr" in self.fail:
            raise RuntimeError("boom get_pr")
        return self.pr

    def get_diff(self, ref: PRRef) -> str:
        if "get_diff" in self.fail:
            raise RuntimeError("boom get_diff")
        return self.diff

    def post_summary(self, ref: PRRef, body: str) -> None:
        if "post_summary" in self.fail:
            raise RuntimeError("boom post_summary")
        self.summaries.append(body)

    def post_inline_comments(self, ref, comments) -> int:
        if "post_inline" in self.fail:
            raise RuntimeError("boom post_inline")
        self.inline_batches.append(list(comments))
        return len(comments)

    def list_threads(self, ref: PRRef) -> list[Thread]:
        if "list_threads" in self.fail:
            raise RuntimeError("boom list_threads")
        return self.threads


@pytest.fixture(autouse=True)
def _contract_stubs(monkeypatch):
    """Pin the reviewer contract. Env clearing lives in tests/conftest.py."""
    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _contract_review_chunk)
    monkeypatch.setattr(orchestrator.reviewer, "load_prompt", _contract_load_prompt)


HAPPY_FINDINGS = {
    "src/app.py": [
        {"file": "src/app.py", "line": 3, "severity": "error", "confidence": 0.9,
         "title": "Null deref", "body": "x may be None when config is missing."},
        {"file": "src/app.py", "line": 7, "severity": "note", "confidence": 0.8,
         "title": "Typo", "body": "recieve -> receive."},
    ],
}


class TestHappyPath:
    def test_posts_summary_and_inline_comments(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        llm = FakeLLM(findings_by_path=HAPPY_FINDINGS)

        res = orchestrate_review(forge, REF, llm)

        assert set(res) == {
            "verdict", "findings_active", "findings_dropped", "chunk_count",
            "chunks_reviewed", "chunks_failed",
            "elapsed_ms", "input_tokens", "output_tokens", "posted",
        }
        assert res["verdict"] == "Request-Changes"
        assert len(res["findings_active"]) == 2
        assert res["findings_dropped"] == []
        assert res["chunk_count"] == 1
        assert res["input_tokens"] == 100
        assert res["output_tokens"] == 50
        assert res["elapsed_ms"] >= 0
        assert res["posted"] is True

        assert len(forge.summaries) == 1
        summary = forge.summaries[0]
        assert "Request-Changes" in summary
        assert "Add widget" in summary
        assert "Files reviewed: 1" in summary
        assert "errors: 1" in summary
        assert "Null deref" in summary
        assert "Reviewed by prxref · model=test-model-1 · 150 tok" in summary

        assert len(forge.inline_batches) == 1
        comments = forge.inline_batches[0]
        assert len(comments) == 2
        by_line = {c.line: c for c in comments}
        assert set(by_line) == {3, 7}
        assert by_line[3].path == "src/app.py"
        assert "🚨" in by_line[3].body
        assert "[ERROR] Null deref" in by_line[3].body
        assert "x may be None" in by_line[3].body
        assert "Reviewed by prxref · model=test-model-1" in by_line[3].body
        assert "📝" in by_line[7].body

    def test_inline_comments_capped_at_fifteen(self):
        findings = {
            "src/app.py": [
                {"file": "src/app.py", "line": i, "severity": "warning",
                 "confidence": 0.75, "title": f"warning {i}", "body": "b"}
                for i in range(1, 19)
            ],
        }
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=findings))

        assert res["verdict"] == "Approved"
        assert len(res["findings_active"]) == 18
        assert len(forge.inline_batches) == 1
        assert len(forge.inline_batches[0]) == 15
        assert res["posted"] is True


class TestParallelFanOut:
    def test_three_chunks_run_concurrently_and_all_findings_collected(self, monkeypatch):
        paths = ["src/big1.py", "src/big2.py", "src/big3.py"]
        diff = "\n".join(_added_file_diff(p, 650) for p in paths) + "\n"
        barrier = threading.Barrier(3, timeout=10)

        def barrier_review_chunk(
            llm, files, *, pr_title="", pr_description="", repo_hint="", max_tokens=None
        ):
            barrier.wait()
            return [Finding(
                    file=files[0].path, line=1, severity="warning",
                    confidence=0.9, title=f"finding in {files[0].path}", body="b",
                )], {
                "escalations": [], "input_tokens": 10, "output_tokens": 5,
                "model": "m1", "elapsed_ms": 1,
            }

        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", barrier_review_chunk)
        forge = FakeForge(diff=diff)
        res = orchestrate_review(forge, REF, FakeLLM())

        assert res["chunk_count"] == 3
        assert res["verdict"] == "Approved"
        assert len(res["findings_active"]) == 3
        assert {f.file for f in res["findings_active"]} == set(paths)
        assert res["input_tokens"] == 30
        assert res["output_tokens"] == 15


class TestDedup:
    def test_duplicate_of_existing_thread_dropped_and_retained(self):
        forge = FakeForge(
            diff=_added_file_diff("src/app.py", 20),
            threads=[Thread(
                path="src/app.py", line=3, resolved=False, author="alice",
                body_snippet="Null deref: x may be None when config is missing here.",
            )],
        )
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS))

        assert [f.title for f in res["findings_active"]] == ["Typo"]
        assert len(res["findings_dropped"]) == 1
        dropped = res["findings_dropped"][0]
        assert dropped.title == "Null deref"
        assert dropped.drop_reason == "duplicate of existing thread"
        assert res["verdict"] == "Approved"
        assert len(forge.inline_batches) == 1
        assert [c.line for c in forge.inline_batches[0]] == [7]
        assert len(forge.summaries) == 1
        assert res["posted"] is True

    def test_list_threads_failure_degrades_to_no_threads(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        forge.fail = {"list_threads"}
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS))

        assert res["verdict"] == "Request-Changes"
        assert len(res["findings_active"]) == 2
        assert len(forge.inline_batches) == 1


class TestErrorPaths:
    def test_total_llm_failure_error_verdict_and_notice_posted(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        llm = FakeLLM(error=RuntimeError("provider down"))

        res = orchestrate_review(forge, REF, llm)

        assert res["verdict"] == "Error"
        assert res["findings_active"] == []
        assert res["findings_dropped"] == []
        assert res["chunk_count"] == 1
        assert res["posted"] is True
        assert len(forge.summaries) == 1
        notice = forge.summaries[0]
        assert "Error" in notice
        assert "could not complete" in notice
        assert "provider down" in notice
        assert "Reviewed by prxref" in notice
        assert forge.inline_batches == []

    def test_get_pr_failure_degrades_to_error_run(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        forge.fail = {"get_pr"}
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS))

        assert res["verdict"] == "Error"
        assert res["chunk_count"] == 0
        assert res["posted"] is True
        assert "get_pr failed" in forge.summaries[0]

    def test_post_summary_failure_keeps_run_non_raising(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        forge.fail = {"post_summary"}
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS))

        assert res["verdict"] == "Request-Changes"
        assert res["posted"] is False
        assert forge.inline_batches == []


class TestPostDisabled:
    def test_post_false_posts_nothing(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS), post=False)

        assert res["verdict"] == "Request-Changes"
        assert len(res["findings_active"]) == 2
        assert res["posted"] is False
        assert forge.summaries == []
        assert forge.inline_batches == []


class TestDroppedFindings:
    def test_low_confidence_and_invalid_severity_retained_with_reason(self):
        findings = {
            "src/app.py": [
                {"file": "src/app.py", "line": 3, "severity": "error",
                 "confidence": 0.2, "title": "Shaky", "body": "low confidence"},
                {"file": "src/app.py", "line": 5, "severity": "critical",
                 "confidence": 0.9, "title": "Not a severity", "body": "bad vocab"},
                {"file": "src/app.py", "line": 7, "severity": "error",
                 "confidence": 0.9, "title": "Real bug", "body": "solid"},
            ],
        }
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=findings))

        assert len(res["findings_active"]) == 1
        assert res["findings_active"][0].title == "Real bug"
        reasons = {f.title: f.drop_reason for f in res["findings_dropped"]}
        assert "below floor" in reasons["Shaky"]
        assert "invalid severity" in reasons["Not a severity"]


class TestEmptyDiff:
    def test_empty_diff_summary_only_approved_run(self):
        forge = FakeForge(diff="")
        res = orchestrate_review(forge, REF, FakeLLM())

        assert res["verdict"] == "Approved"
        assert res["findings_active"] == []
        assert res["findings_dropped"] == []
        assert res["chunk_count"] == 0
        assert res["input_tokens"] == 0
        assert res["output_tokens"] == 0
        assert res["posted"] is True
        assert len(forge.summaries) == 1
        assert "Approved" in forge.summaries[0]
        assert "No findings — nice work." in forge.summaries[0]
        assert forge.inline_batches == []


class TestCoverageAwareVerdict:
    def test_all_workers_failing_yields_error_not_approved(self, monkeypatch):
        def _failing_review_chunk(llm, files, **kwargs):
            return [], {
                "escalations": [], "input_tokens": 0, "output_tokens": 0,
                "model": "", "elapsed_ms": 0, "error": "LLMError: all models failed",
            }

        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _failing_review_chunk)
        forge = FakeForge(diff=TWO_FILE_DIFF)
        result = orchestrate_review(forge, REF, FakeLLM("{}"), post=False)
        assert result["verdict"] == "Error"
        assert result["chunks_reviewed"] == 0

    def test_partial_failure_keeps_verdict_and_reports_coverage(self, monkeypatch):
        counter = itertools.count(1)

        def _flaky_review_chunk(llm, files, **kwargs):
            if next(counter) == 1:
                return [], {
                    "escalations": [], "input_tokens": 0, "output_tokens": 0,
                    "model": "", "elapsed_ms": 0, "error": "LLMError: timeout",
                }
            return [], {
                "escalations": [], "input_tokens": 5, "output_tokens": 5,
                "model": "m", "elapsed_ms": 1, "error": "",
            }

        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _flaky_review_chunk)
        forge = FakeForge(diff=TWO_FILE_DIFF)
        result = orchestrate_review(forge, REF, FakeLLM("{}"), post=False, max_chunks=2)
        assert result["verdict"] == "Approved"
        assert result["chunks_failed"] == 1
        assert result["chunks_reviewed"] == 1
        assert result["chunks_reviewed"] + result["chunks_failed"] == result["chunk_count"]

    def test_clean_run_reports_full_coverage(self):
        forge = FakeForge(diff=TWO_FILE_DIFF)
        result = orchestrate_review(forge, REF, FakeLLM('{"findings": []}'), post=False)
        assert result["verdict"] == "Approved"
        assert result["chunks_failed"] == 0
        assert result["chunks_reviewed"] == result["chunk_count"]

    def test_degraded_coverage_is_declared_in_posted_summary(self, monkeypatch):
        counter = itertools.count(1)

        def _flaky_review_chunk(llm, files, **kwargs):
            if next(counter) == 1:
                return [], {
                    "escalations": [], "input_tokens": 0, "output_tokens": 0,
                    "model": "", "elapsed_ms": 0, "error": "LLMError: timeout",
                }
            return [], {
                "escalations": [], "input_tokens": 5, "output_tokens": 5,
                "model": "m", "elapsed_ms": 1, "error": "",
            }

        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _flaky_review_chunk)
        forge = FakeForge(diff=TWO_FILE_DIFF)
        orchestrate_review(forge, REF, FakeLLM("{}"), post=True, max_chunks=2)
        assert "Partial review" in forge.summaries[0]
        assert "1 of 2" in forge.summaries[0]


class TestMaxTokensThreading:
    """The configured completion budget must reach every worker, and stay out of the result."""

    def _recording_review_chunk(self, seen):
        def _rc(llm, files, **kwargs):
            seen.append(kwargs.get("max_tokens"))
            return [], {
                "escalations": [], "input_tokens": 1, "output_tokens": 1,
                "model": "m", "elapsed_ms": 1, "error": "",
            }
        return _rc

    def test_configured_budget_reaches_every_chunk(self, monkeypatch):
        seen: list = []
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk", self._recording_review_chunk(seen)
        )
        forge = FakeForge(diff=TWO_FILE_DIFF)
        orchestrate_review(forge, REF, FakeLLM("{}"), post=False, max_chunks=2, max_tokens=9001)
        assert seen == [9001, 9001]

    def test_default_is_none_so_the_reviewer_default_wins(self, monkeypatch):
        seen: list = []
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk", self._recording_review_chunk(seen)
        )
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        orchestrate_review(forge, REF, FakeLLM("{}"), post=False)
        assert seen == [None]

    def test_result_key_set_is_unchanged_by_the_new_knob(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS), post=False, max_tokens=2048
        )
        assert set(res) == {
            "verdict", "findings_active", "findings_dropped", "chunk_count",
            "chunks_reviewed", "chunks_failed",
            "elapsed_ms", "input_tokens", "output_tokens", "posted",
        }


FOUR_FILE_DIFF = "".join(
    _added_file_diff(path, 400)
    for path in ("a/one.py", "b/two.py", "c/three.py", "d/four.py")
)


def _findings(*specs) -> dict:
    """Build a findings_by_path payload from (line, severity, confidence) triples."""
    return {
        "src/app.py": [
            {"file": "src/app.py", "line": line, "severity": severity,
             "confidence": confidence, "title": f"f{line}", "body": "b"}
            for line, severity, confidence in specs
        ],
    }


class TestChunkTokenBudget:
    """The chunk token budget is a knob, and a smaller one means more chunks."""

    def test_signature_defaults_are_the_triage_constant(self):
        """Pinned by identity, not by outcome.

        Comparing a default run to an explicit ``DEFAULT_TOKEN_BUDGET`` run is
        insensitive over a wide band: any default in roughly 16 001-32 000
        yields 4 chunks on this fixture, so that comparison would still pass if
        someone replaced the signature default with a nearby literal.
        """
        import inspect

        from prxref import triage
        from prxref.triage import build_chunks

        assert triage.DEFAULT_TOKEN_BUDGET == 25_000
        for fn in (orchestrate_review, build_chunks):
            default = inspect.signature(fn).parameters["token_budget"].default
            assert default is triage.DEFAULT_TOKEN_BUDGET, fn.__name__

    def test_the_default_run_actually_chunks_at_that_budget(self):
        """Boundary-sensitive companion: 35 000 gives a different answer on this
        fixture, so a silently widened default shows up as a failure here."""
        from prxref import triage

        at_default = orchestrate_review(
            FakeForge(diff=FOUR_FILE_DIFF), REF, FakeLLM("{}"), post=False,
        )
        at_constant = orchestrate_review(
            FakeForge(diff=FOUR_FILE_DIFF), REF, FakeLLM("{}"), post=False,
            token_budget=triage.DEFAULT_TOKEN_BUDGET,
        )
        at_wider = orchestrate_review(
            FakeForge(diff=FOUR_FILE_DIFF), REF, FakeLLM("{}"), post=False,
            token_budget=35_000,
        )
        assert at_default["chunk_count"] == at_constant["chunk_count"] == 4
        assert at_wider["chunk_count"] == 2

    @pytest.mark.parametrize("budget,expected_chunks", [
        (70_000, 1),
        (35_000, 2),
        (16_000, 4),
    ])
    def test_lower_budget_yields_more_chunks(self, budget, expected_chunks):
        forge = FakeForge(diff=FOUR_FILE_DIFF)
        res = orchestrate_review(
            forge, REF, FakeLLM("{}"), post=False, token_budget=budget,
        )
        assert res["chunk_count"] == expected_chunks
        assert res["chunks_reviewed"] == expected_chunks


class TestMaxWorkers:
    """The fan-out width is a knob; narrowing it must not narrow coverage."""

    def _recording_pool(self, monkeypatch, seen):
        real = orchestrator.ThreadPoolExecutor

        def _factory(max_workers=None, **kwargs):
            seen.append(max_workers)
            return real(max_workers=max_workers, **kwargs)

        monkeypatch.setattr(orchestrator, "ThreadPoolExecutor", _factory)

    def test_default_is_the_module_constant(self, monkeypatch):
        seen: list = []
        self._recording_pool(monkeypatch, seen)
        orchestrate_review(
            FakeForge(diff=FOUR_FILE_DIFF), REF, FakeLLM("{}"), post=False,
        )
        assert orchestrator.MAX_WORKERS == 4
        assert seen == [orchestrator.MAX_WORKERS]

    def test_configured_width_caps_the_pool(self, monkeypatch):
        seen: list = []
        self._recording_pool(monkeypatch, seen)
        orchestrate_review(
            FakeForge(diff=FOUR_FILE_DIFF), REF, FakeLLM("{}"), post=False,
            max_workers=2,
        )
        assert seen == [2]

    def test_one_worker_still_reviews_every_chunk(self, monkeypatch):
        seen: list = []
        self._recording_pool(monkeypatch, seen)
        res = orchestrate_review(
            FakeForge(diff=FOUR_FILE_DIFF), REF, FakeLLM('{"findings": []}'),
            post=False, max_workers=1,
        )
        assert seen == [1]
        assert res["chunk_count"] == 4
        assert res["chunks_reviewed"] == 4
        assert res["chunks_failed"] == 0

    def test_width_never_exceeds_the_chunk_count(self, monkeypatch):
        seen: list = []
        self._recording_pool(monkeypatch, seen)
        orchestrate_review(
            FakeForge(diff=_added_file_diff("src/app.py", 20)), REF, FakeLLM("{}"),
            post=False, max_workers=16,
        )
        assert seen == [1]


class TestMaxInlineComments:
    def test_configured_cap_limits_the_posted_batch(self):
        findings = {
            "src/app.py": [
                {"file": "src/app.py", "line": i, "severity": "warning",
                 "confidence": 0.75, "title": f"warning {i}", "body": "b"}
                for i in range(1, 19)
            ],
        }
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=findings), max_inline_comments=3,
        )
        assert len(res["findings_active"]) == 18
        assert len(forge.inline_batches[0]) == 3

    def test_default_is_the_module_constant(self):
        findings = {
            "src/app.py": [
                {"file": "src/app.py", "line": i, "severity": "warning",
                 "confidence": 0.75, "title": f"warning {i}", "body": "b"}
                for i in range(1, 19)
            ],
        }
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        orchestrate_review(forge, REF, FakeLLM(findings_by_path=findings))
        assert len(forge.inline_batches[0]) == orchestrator.MAX_INLINE_COMMENTS == 15


class TestQualityGateKnobsAreThreaded:
    """Regression: ``apply_quality_gate(findings)`` was called with no arguments.

    The gate then re-read ``PRXREF_CONFIDENCE_FLOOR`` / ``PRXREF_MAX_ERROR_FINDINGS``
    from the environment itself, so a value that ``load_config`` had resolved —
    including an explicit programmatic override — never reached it. The
    environment fallback stays in ``quality`` for library callers who never build
    a config; an explicit value must win over it.
    """

    def test_explicit_floor_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONFIDENCE_FLOOR", "0.1")
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=_findings((3, "warning", 0.5))),
            post=False, confidence_floor=0.99,
        )
        assert res["findings_active"] == []
        assert len(res["findings_dropped"]) == 1
        assert "below floor 0.99" in res["findings_dropped"][0].drop_reason

    def test_environment_still_applies_when_no_value_is_supplied(self, monkeypatch):
        """Library callers that never build a config keep the env fallback."""
        monkeypatch.setenv("PRXREF_CONFIDENCE_FLOOR", "0.1")
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=_findings((3, "warning", 0.5))),
            post=False,
        )
        assert len(res["findings_active"]) == 1
        assert res["findings_dropped"] == []

    def test_explicit_error_cap_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_ERROR_FINDINGS", "10")
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF,
            FakeLLM(findings_by_path=_findings(
                (3, "error", 0.9), (7, "error", 0.8), (11, "error", 0.7),
            )),
            post=False, max_errors=1,
        )
        assert len(res["findings_active"]) == 1
        assert res["findings_active"][0].confidence == 0.9
        assert len(res["findings_dropped"]) == 2
        assert all(
            "error cap exceeded (max 1)" in f.drop_reason
            for f in res["findings_dropped"]
        )

    def test_environment_error_cap_still_applies_when_unsupplied(self, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_ERROR_FINDINGS", "1")
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF,
            FakeLLM(findings_by_path=_findings((3, "error", 0.9), (7, "error", 0.8))),
            post=False,
        )
        assert len(res["findings_active"]) == 1
        assert len(res["findings_dropped"]) == 1

    def test_result_key_set_is_unchanged_by_the_new_knobs(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS), post=False,
            token_budget=30_000, max_workers=2, max_inline_comments=4,
            confidence_floor=0.5, max_errors=5,
        )
        assert set(res) == {
            "verdict", "findings_active", "findings_dropped", "chunk_count",
            "chunks_reviewed", "chunks_failed",
            "elapsed_ms", "input_tokens", "output_tokens", "posted",
        }


RESULT_KEYS = {
    "verdict", "findings_active", "findings_dropped", "chunk_count",
    "chunks_reviewed", "chunks_failed",
    "elapsed_ms", "input_tokens", "output_tokens", "posted",
}


class TestNoStageRaisesOutOfOrchestrateReview:
    """The module docstring's never-raise contract, made true.

    ``parse_unified_diff`` and ``build_chunks`` were the only two stages not
    wrapped: ``get_pr``, ``get_diff``, ``list_threads``, ``post_summary``,
    ``post_inline_comments`` and the worker pool all were. A library caller
    passing ``max_chunks=0`` therefore got ``ValueError: min() iterable
    argument is empty`` out of a function documented never to raise. The CLI is
    fenced off earlier by the config range check; this is the library route.
    """

    def test_zero_max_chunks_degrades_instead_of_raising(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS),
            post=False, max_chunks=0,
        )
        assert res["verdict"] == "Error"
        assert res["findings_active"] == []

    def test_the_result_is_still_a_well_formed_result(self):
        """A caller reading the dict must not have to special-case this path."""
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS),
            post=False, max_chunks=0,
        )
        assert set(res) == RESULT_KEYS

    def test_the_posted_notice_names_the_stage_that_failed(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS),
            post=True, max_chunks=0,
        )
        assert res["posted"] is True
        assert len(forge.summaries) == 1
        assert "build_chunks failed" in forge.summaries[0]

    def test_no_chunk_is_counted_as_reviewed_or_failed(self):
        """Nothing was chunked, so the partial-coverage banner has nothing to
        report — this is a total failure, not a 0-of-N partial one."""
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS),
            post=False, max_chunks=0,
        )
        assert res["chunk_count"] == 0
        assert res["chunks_reviewed"] == 0
        assert res["chunks_failed"] == 0

    def test_a_parser_explosion_degrades_too(self, monkeypatch):
        """The wrap covers the parse stage, not only the chunking one."""
        def _boom(_raw):
            raise ValueError("unparseable diff")

        monkeypatch.setattr(orchestrator, "parse_unified_diff", _boom)
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(forge, REF, FakeLLM("{}"), post=True)
        assert res["verdict"] == "Error"
        assert "parse_unified_diff failed: unparseable diff" in forge.summaries[0]

    def test_the_llm_is_never_called_when_chunking_fails(self):
        """Failing before the fan-out must not spend tokens on the way out."""
        llm = FakeLLM(findings_by_path=HAPPY_FINDINGS)
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        orchestrate_review(forge, REF, llm, post=False, max_chunks=0)
        assert llm.calls == 0

    def test_a_legal_max_chunks_is_untouched_by_the_wrap(self):
        """Control: the guard must not swallow a run that works."""
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS),
            post=False, max_chunks=1,
        )
        assert res["verdict"] == "Request-Changes"
        assert res["chunk_count"] == 1
