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
import logging
import sys
import threading
import types
from unittest.mock import MagicMock

import pytest

import prxref
from prxref.forges.base import InlineComment, PRData, PRRef, Thread
from prxref.llm import InvokeResult
from prxref.triage import DEFAULT_TOKEN_BUDGET, Finding, build_chunks, parse_unified_diff

SUMMARY_TEMPLATE = (
    "🤖 **prxref review — {verdict}**\n\n"
    "PR: {title}\n\n"
    "Files reviewed: {file_count} · 🟥 {error_count} error · "
    "🟧 {warning_count} warning · 🟦 {outofscope_count} outofscope\n\n"
    "{findings}\n\n{attribution}"
)


def _contract_review_chunk(
    llm, files, *, pr_title="", pr_description="", repo_hint="",
    max_tokens=None, context_lines=None,
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


def _contract_review_systemic(
    llm, digest, *, pr_title="", pr_description="", repo_hint="", max_tokens=None,
):
    return [], {
        "escalations": [], "input_tokens": 0, "output_tokens": 0,
        "model": "", "elapsed_ms": 0, "error": "",
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

# Captured at import, before the autouse ``_contract_stubs`` fixture can
# overwrite the module attributes: tests that need the REAL renderer end to
# end restore these instead of re-reading the (already patched) attributes.
REAL_REVIEW_CHUNK = orchestrator.reviewer.review_chunk
REAL_LOAD_PROMPT = orchestrator.reviewer.load_prompt

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
    # ``data`` is the hunk's one tokenizer-visible token: findings citing this
    # fixture corroborate only if their text mentions it, mirroring how the
    # content pass validates real anchors (issue #19).
    body = "\n".join(f"+data {i}" for i in range(1, n_lines + 1))
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{n_lines} @@\n"
        f"{body}\n"
    )


TWO_FILE_DIFF = _added_file_diff("src/one.py", 400) + _added_file_diff("other/two.py", 400)

# A modified file with six context lines on each side of one change — fatter
# context than the default knob, so PRXREF_CHUNK_CONTEXT_LINES trimming is
# observable in what the worker LLM is actually sent.
FAT_CONTEXT_DIFF = (
    "diff --git a/src/ctx.py b/src/ctx.py\n"
    "--- a/src/ctx.py\n"
    "+++ b/src/ctx.py\n"
    "@@ -1,13 +1,13 @@\n"
    + "".join(f" lead{i}\n" for i in range(1, 7))
    + "-changed\n"
    + "+fixed\n"
    + "".join(f" tail{i}\n" for i in range(1, 7))
)


def multi_chunk_diff(n_files: int = 3) -> str:
    """Build a diff guaranteed to chunk into exactly ``n_files`` pieces.

    Each added file alone exceeds the default token budget (est_tokens is 40
    per line), so ``build_chunks`` must open one chunk per file. The count is
    asserted here so a budget change fails loudly instead of silently
    producing a single chunk.
    """
    diff = (
        "\n".join(_added_file_diff(f"src/big{i}.py", 650) for i in range(1, n_files + 1))
        + "\n"
    )
    chunks = build_chunks(parse_unified_diff(diff), token_budget=DEFAULT_TOKEN_BUDGET)
    assert len(chunks) == n_files, f"expected {n_files} chunks, got {len(chunks)}"
    return diff


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
        # What get_pr raises when "get_pr" is in ``fail``. A test that cares
        # about the TEXT of a forge failure (a URL in a 404, say) supplies its
        # own; everything else keeps the generic boom.
        self.pr_error: Exception | None = None
        self.summaries: list[str] = []
        self.inline_batches: list[list[InlineComment]] = []

    @staticmethod
    def parse_pr_url(url: str) -> PRRef | None:
        return None

    def get_pr(self, ref: PRRef) -> PRData:
        if "get_pr" in self.fail:
            raise self.pr_error or RuntimeError("boom get_pr")
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
    """Pin the reviewer contract. Env clearing lives in tests/conftest.py.

    The systemic sweep is stubbed to a clean no-findings success so the
    sweep-specific classes below can monkeypatch their own doubles; the
    chunk-count assertions in the older classes include the sweep unit.
    """
    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _contract_review_chunk)
    monkeypatch.setattr(
        orchestrator.reviewer, "review_systemic", _contract_review_systemic,
    )
    monkeypatch.setattr(orchestrator.reviewer, "load_prompt", _contract_load_prompt)


HAPPY_FINDINGS = {
    "src/app.py": [
        {"file": "src/app.py", "line": 3, "severity": "error", "confidence": 0.9,
         "title": "Null deref", "body": "x may be None when config is missing; data loss follows."},
        {"file": "src/app.py", "line": 7, "severity": "outofscope", "confidence": 0.8,
         "title": "Typo", "body": "recieve -> receive in data text."},
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
            "sampling",
        }
        assert res["verdict"] == "Request-Changes"
        assert len(res["findings_active"]) == 2
        assert res["findings_dropped"] == []
        # 1 chunk + 1 systemic-sweep unit.
        assert res["chunk_count"] == 2
        assert res["input_tokens"] == 100
        assert res["output_tokens"] == 50
        assert res["elapsed_ms"] >= 0
        assert res["posted"] is True

        assert len(forge.summaries) == 1
        summary = forge.summaries[0]
        assert "Request-Changes" in summary
        assert "Add widget" in summary
        assert "Files reviewed: 1" in summary
        assert "🟥 1 error" in summary
        assert "Null deref" in summary
        assert "Reviewed by prxref · model=test-model-1 · 150 tok" in summary

        assert len(forge.inline_batches) == 1
        comments = forge.inline_batches[0]
        assert len(comments) == 2
        by_line = {c.line: c for c in comments}
        assert set(by_line) == {3, 7}
        assert by_line[3].path == "src/app.py"
        assert "🟥" in by_line[3].body
        assert "[ERROR] Null deref" in by_line[3].body
        assert "x may be None" in by_line[3].body
        assert "Reviewed by prxref · model=test-model-1" in by_line[3].body
        assert "🟦" in by_line[7].body

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

    def test_inline_batch_is_severity_ordered(self):
        findings = {
            "src/app.py": [
                {"file": "src/app.py", "line": 3, "severity": "warning",
                 "confidence": 0.9, "title": "warn high", "body": "b"},
                {"file": "src/app.py", "line": 5, "severity": "warning",
                 "confidence": 0.8, "title": "warn low", "body": "b"},
                {"file": "src/app.py", "line": 7, "severity": "error",
                 "confidence": 0.7, "title": "the error", "body": "b"},
            ],
        }
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        orchestrate_review(forge, REF, FakeLLM(findings_by_path=findings))

        bodies = [c.body for c in forge.inline_batches[0]]
        error_positions = [i for i, b in enumerate(bodies) if "[ERROR] the error" in b]
        assert error_positions == [0]

    def test_cap_shortfall_is_disclosed_in_the_summary(self):
        findings = {
            "src/app.py": [
                {"file": "src/app.py", "line": 3, "severity": "error",
                 "confidence": 0.9, "title": "kept", "body": "b"},
                {"file": "src/app.py", "line": 5, "severity": "warning",
                 "confidence": 0.9, "title": "capped out", "body": "b"},
            ],
        }
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=findings),
            max_inline_comments=1,
        )

        assert len(forge.summaries) == 2
        assert "Inline comments:" not in forge.summaries[0]
        assert "Inline comments: 1 of 2 findings (1 over the 1-comment cap)." in (
            forge.summaries[1]
        )

    def test_rejected_anchors_are_disclosed_in_the_summary(self):
        class RejectingForge(FakeForge):
            def post_inline_comments(self, ref, comments) -> int:
                self.inline_batches.append(list(comments))
                return max(0, len(comments) - 1)

        findings = {
            "src/app.py": [
                {"file": "src/app.py", "line": 3, "severity": "error",
                 "confidence": 0.9, "title": "posted", "body": "b"},
                {"file": "src/app.py", "line": 5, "severity": "warning",
                 "confidence": 0.9, "title": "rejected", "body": "b"},
            ],
        }
        forge = RejectingForge(diff=_added_file_diff("src/app.py", 20))
        orchestrate_review(forge, REF, FakeLLM(findings_by_path=findings))

        assert "Inline comments: 1 of 2 findings (1 anchor rejected by the forge)." in (
            forge.summaries[-1]
        )

    def test_failed_inline_batch_is_disclosed_in_the_summary(self):
        findings = {
            "src/app.py": [
                {"file": "src/app.py", "line": 3, "severity": "error",
                 "confidence": 0.9, "title": "lost", "body": "b"},
            ],
        }
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        forge.fail.add("post_inline")
        orchestrate_review(forge, REF, FakeLLM(findings_by_path=findings))

        assert (
            "Inline comments: posting failed — 0 of 1 findings have one."
            in forge.summaries[-1]
        )


class TestParallelFanOut:
    def test_three_chunks_run_concurrently_and_all_findings_collected(self, monkeypatch):
        paths = [f"src/big{i}.py" for i in range(1, 4)]
        diff = multi_chunk_diff(3)
        barrier = threading.Barrier(3, timeout=10)

        def barrier_review_chunk(
            llm, files, *, pr_title="", pr_description="", repo_hint="",
            max_tokens=None, context_lines=None,
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

        # 3 chunks + 1 systemic-sweep unit.
        assert res["chunk_count"] == 4
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


class TestSeverityConsistency:
    def test_normalized_title_raises_warning_to_error_before_the_error_cap(self, monkeypatch):
        # Issue #18: identical pattern, sibling files, split severities. The
        # consistency pass must run BEFORE apply_quality_gate so the cap
        # counts the normalized error: with PRXREF_MAX_ERROR_FINDINGS=1 the
        # now-two errors cap to one. If the pass ran after the gate, the
        # warning would sail past the cap uncapped and both would survive.
        monkeypatch.setenv("PRXREF_MAX_ERROR_FINDINGS", "1")
        diff = _added_file_diff("functions/a.ts", 10) + _added_file_diff("functions/b.ts", 10)
        findings = {
            "functions/a.ts": [
                {"file": "functions/a.ts", "line": 3, "severity": "error",
                 "confidence": 0.9, "title": "Hardcoded secret in serverless handler",
                 "body": "VITE_ key committed."},
            ],
            "functions/b.ts": [
                {"file": "functions/b.ts", "line": 3, "severity": "warning",
                 "confidence": 0.8, "title": "hardcoded secret in serverless handler",
                 "body": "Same VITE_ pattern."},
            ],
        }
        forge = FakeForge(diff=diff)
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=findings), post=False)

        assert len(res["findings_active"]) == 1
        kept = res["findings_active"][0]
        assert kept.title == "Hardcoded secret in serverless handler"
        assert kept.severity == "error"
        assert kept.file == "functions/a.ts"
        assert len(res["findings_dropped"]) == 1
        dropped = res["findings_dropped"][0]
        assert dropped.file == "functions/b.ts"
        assert dropped.severity == "error"
        assert dropped.drop_reason == "error cap exceeded (max 1)"
        assert res["verdict"] == "Request-Changes"

    def test_shared_rare_code_token_raises_sibling_file_warning_to_error(self, caplog):
        # Issue #30 live shape end to end: per-chunk workers phrase the
        # same unescaped-interpolation bug differently, so normalized-title
        # equality (#18) never binds. The rare-token pass must raise both
        # to error before the gate, and log the binding token.
        diff = (
            _added_file_diff("src/airtable-video-processor.ts", 10)
            + _added_file_diff("src/get-video-feedbacks.ts", 10)
        )
        findings = {
            "src/airtable-video-processor.ts": [
                {"file": "src/airtable-video-processor.ts", "line": 3,
                 "severity": "error", "confidence": 0.9,
                 "title": "Airtable formula injection via unescaped vimeo_code in filterByFormula",
                 "body": "filterByFormula({vimeo_code}) allows filter manipulation."},
            ],
            "src/get-video-feedbacks.ts": [
                {"file": "src/get-video-feedbacks.ts", "line": 3,
                 "severity": "warning", "confidence": 0.8,
                 "title": "Formula interpolation of vimeo_code allows filter manipulation",
                 "body": "vimeo_code interpolated into filterByFormula."},
            ],
        }
        forge = FakeForge(diff=diff)
        with caplog.at_level(logging.INFO, logger="prxref.quality"):
            res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=findings), post=False)

        assert len(res["findings_active"]) == 2
        assert all(f.severity == "error" for f in res["findings_active"])
        assert res["verdict"] == "Request-Changes"
        token_lines = [
            r for r in caplog.records
            if r.name == "prxref.quality" and "shared rare code token" in r.getMessage()
        ]
        assert len(token_lines) == 1
        assert "vimeo_code" in token_lines[0].getMessage()


class TestErrorPaths:
    def test_total_llm_failure_error_verdict_and_notice_posted(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        llm = FakeLLM(error=RuntimeError("provider down"))

        res = orchestrate_review(forge, REF, llm)

        assert res["verdict"] == "Error"
        assert res["findings_active"] == []
        assert res["findings_dropped"] == []
        # 1 chunk + 1 systemic-sweep unit.
        assert res["chunk_count"] == 2
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
                    "model": "", "elapsed_ms": 0, "error": "LLMError: malformed response",
                }
            return [], {
                "escalations": [], "input_tokens": 5, "output_tokens": 5,
                "model": "m", "elapsed_ms": 1, "error": "",
            }

        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _flaky_review_chunk)
        forge = FakeForge(diff=TWO_FILE_DIFF)
        result = orchestrate_review(forge, REF, FakeLLM("{}"), post=False, max_chunks=2)
        assert result["verdict"] == "Approved"
        # The failed chunk counts as failed; the sweep unit reviewed.
        assert result["chunks_failed"] == 1
        assert result["chunks_reviewed"] == 2
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
                    "model": "", "elapsed_ms": 0, "error": "LLMError: malformed response",
                }
            return [], {
                "escalations": [], "input_tokens": 5, "output_tokens": 5,
                "model": "m", "elapsed_ms": 1, "error": "",
            }

        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _flaky_review_chunk)
        forge = FakeForge(diff=TWO_FILE_DIFF)
        orchestrate_review(forge, REF, FakeLLM("{}"), post=True, max_chunks=2)
        assert "Partial review" in forge.summaries[0]
        # 2 of 3 units reviewed: the good chunk plus the systemic sweep.
        assert "2 of 3" in forge.summaries[0]


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
            "sampling",
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
        # chunk_count includes the systemic-sweep unit (+1).
        assert at_default["chunk_count"] == at_constant["chunk_count"] == 5
        assert at_wider["chunk_count"] == 3

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
        assert res["chunk_count"] == expected_chunks + 1
        assert res["chunks_reviewed"] == expected_chunks + 1


class TestChunkFileCapAndContextKnobs:
    """The per-chunk file cap and the prompt context bound are knobs too, and
    both must survive the whole trip config → orchestrator → worker."""

    def test_signature_defaults_are_the_triage_constants(self):
        import inspect

        from prxref import triage

        assert triage.DEFAULT_MAX_FILES_PER_CHUNK == 5
        assert triage.DEFAULT_CONTEXT_LINES == 3
        for fn, param, constant in (
            (orchestrate_review, "max_files_per_chunk",
             triage.DEFAULT_MAX_FILES_PER_CHUNK),
            (orchestrate_review, "context_lines", triage.DEFAULT_CONTEXT_LINES),
            (build_chunks, "max_files_per_chunk",
             triage.DEFAULT_MAX_FILES_PER_CHUNK),
        ):
            default = inspect.signature(fn).parameters[param].default
            assert default is constant, (fn.__name__, param)

    def test_max_files_per_chunk_reaches_build_chunks(self, monkeypatch):
        spy = MagicMock(wraps=orchestrator.build_chunks)
        monkeypatch.setattr(orchestrator, "build_chunks", spy)
        orchestrate_review(
            FakeForge(diff=FOUR_FILE_DIFF), REF, FakeLLM("{}"), post=False,
            max_files_per_chunk=3,
        )
        assert spy.call_args.kwargs["max_files_per_chunk"] == 3

    def test_file_cap_splits_one_wide_chunk_end_to_end(self):
        """Six same-directory files fit one chunk when the cap is wide enough;
        the cap itself is what splits them, observable in the run result."""
        six = "".join(_added_file_diff(f"src/small{i}.py", 2) for i in range(6))
        wide = orchestrate_review(
            FakeForge(diff=six), REF, FakeLLM("{}"), post=False,
            max_files_per_chunk=6,
        )
        capped = orchestrate_review(
            FakeForge(diff=six), REF, FakeLLM("{}"), post=False,
            max_files_per_chunk=2,
        )
        assert wide["chunk_count"] == 2
        assert capped["chunk_count"] == 4
        assert capped["chunks_reviewed"] == 4

    def test_context_lines_reach_the_worker_prompt(self, monkeypatch):
        """The trim is observable in what the LLM is sent, the changed lines
        themselves are never trimmed away, and the chunk still reviews."""
        prompts: list[str] = []

        class RecordingLLM:
            def invoke(self, system, user, *, max_tokens=4096, json_mode=False,
                       timeout_s=60.0):
                prompts.append(user)
                return InvokeResult(
                    text="{}", input_tokens=1, output_tokens=1,
                    model="m", backend="b", elapsed_ms=1,
                )

        # Both REAL functions were captured at import, before the autouse
        # contract stub replaced the module attributes.
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk", REAL_REVIEW_CHUNK,
        )
        monkeypatch.setattr(
            orchestrator.reviewer, "load_prompt", REAL_LOAD_PROMPT,
        )
        res = orchestrate_review(
            FakeForge(diff=FAT_CONTEXT_DIFF), REF, RecordingLLM(), post=False,
            context_lines=1,
        )
        # The contract fixture stubs the sweep, so the only LLM prompt here is
        # the chunk's; the sweep prompt is covered by the systemic-sweep tests.
        assert res["chunks_reviewed"] == 2
        assert len(prompts) == 1
        assert " lead1" not in prompts[0]
        assert " lead6" in prompts[0]
        assert " tail1" in prompts[0]
        assert "-changed" in prompts[0]
        assert "+fixed" in prompts[0]


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
        assert res["chunk_count"] == 5
        assert res["chunks_reviewed"] == 5
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
            "sampling",
        }


RESULT_KEYS = {
    "verdict", "findings_active", "findings_dropped", "chunk_count",
    "chunks_reviewed", "chunks_failed",
    "elapsed_ms", "input_tokens", "output_tokens", "posted", "sampling",
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
        assert res["chunk_count"] == 2


TRUNCATED_REASON = (
    "response truncated at max_tokens=256 (finish_reason=length); "
    "raise PRXREF_LLM_MAX_TOKENS"
)


def _review_chunk_failing(errors_by_path: dict[str, str], *, tokens: int = 0):
    """A ``review_chunk`` double whose outcome depends on the chunk's first file.

    Keyed by path rather than by call order, so the result is identical however
    the thread pool interleaves the chunks.
    """
    def _rc(llm, files, **kwargs):
        error = errors_by_path.get(files[0].path, "")
        findings = [] if error else [
            Finding(
                file=files[0].path, line=1, severity="outofscope", confidence=0.9,
                title="ok", body="b",
            )
        ]
        return findings, {
            "escalations": [],
            "input_tokens": tokens, "output_tokens": tokens,
            "model": "test-model-1", "elapsed_ms": 1, "error": error,
        }
    return _rc


class TestPartialBannerNamesTheReasons:
    """A partial review READS like a successful one, so a reason left in the
    logs reaches nobody.

    The total-failure notice has always interpolated its reason verbatim into
    the posted comment; the partial path staying silent was the inconsistency.
    It is also the path that needs it more — a total failure announces itself,
    while "Findings may be incomplete" looks like a footnote.
    """

    def _run(self, errors_by_path, *, diff=None, max_chunks=4):
        forge = FakeForge(diff=diff if diff is not None else FOUR_FILE_DIFF)
        res = orchestrate_review(
            forge, REF, FakeLLM("{}"), post=True,
            max_chunks=max_chunks, token_budget=1000,
        )
        return forge, res

    def _banner(self, forge):
        return forge.summaries[0].split("> ⚠️ Partial review:")[1]

    @pytest.fixture(autouse=True)
    def _stub(self, monkeypatch):
        self.monkeypatch = monkeypatch

    def _with(self, errors_by_path, **kwargs):
        self.monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _review_chunk_failing(errors_by_path),
        )
        return self._run(errors_by_path, **kwargs)

    def test_the_truncation_reason_reaches_the_posted_comment(self):
        forge, res = self._with({"a/one.py": TRUNCATED_REASON})
        assert res["chunks_failed"] == 1
        assert res["chunks_reviewed"] == 4
        assert TRUNCATED_REASON in forge.summaries[0]

    def test_it_stays_inside_the_existing_blockquote(self):
        """Subordinate to the findings, not a second block competing with them."""
        forge, _res = self._with({"a/one.py": TRUNCATED_REASON})
        tail = self._banner(forge)
        assert f"> - chunk of 1 file (a/one.py): {TRUNCATED_REASON}" in tail
        # Line 0 is the rest of the "Partial review:" sentence itself; every
        # line after it must still carry the blockquote marker, or the reasons
        # have escaped into a block of their own.
        assert [
            line for line in tail.splitlines()[1:]
            if line.strip() and not line.startswith(">")
        ] == []

    def test_the_existing_coverage_sentence_is_untouched(self):
        forge, _res = self._with({"a/one.py": TRUNCATED_REASON})
        assert (
            "> ⚠️ Partial review: 4 of 5 chunks were reviewed; 1 failed. "
            "Findings may be incomplete." in forge.summaries[0]
        )

    def test_identical_reasons_still_name_each_failed_chunks_files(self):
        """The reason is one fact; the file lists are not (issue #31).

        Three chunks starved by the same budget used to collapse into one
        reason line, leaving the operator to guess which files went
        unreviewed. Each failed chunk now names its own files, so the same
        reason appears once per chunk, each line carrying a different list.
        """
        forge, res = self._with({
            "a/one.py": TRUNCATED_REASON,
            "b/two.py": TRUNCATED_REASON,
            "c/three.py": TRUNCATED_REASON,
        })
        assert res["chunks_failed"] == 3
        tail = self._banner(forge)
        for path in ("a/one.py", "b/two.py", "c/three.py"):
            assert path in tail
        assert tail.count("\n> - ") == 3

    def test_distinct_reasons_are_all_reported(self):
        forge, _res = self._with({
            "a/one.py": TRUNCATED_REASON,
            "b/two.py": "RuntimeError: connection reset",
        })
        tail = self._banner(forge)
        assert TRUNCATED_REASON in tail
        assert "RuntimeError: connection reset" in tail

    def test_every_chunk_failing_is_a_total_failure_not_a_partial_one(self):
        """Boundary: the partial banner belongs to runs that produced something.
        With nothing reviewed the run takes the error path, whose notice has
        always carried the reason verbatim."""
        forge, res = self._with({
            "a/one.py": "reason A", "b/two.py": "reason B",
            "c/three.py": "reason C", "d/four.py": "reason D",
        })
        assert res["verdict"] == "Error"
        assert "Partial review" not in forge.summaries[0]
        assert "reason A" in forge.summaries[0]

    def test_the_overflow_is_counted_out_loud(self, monkeypatch):
        """Capped so a pathological run cannot bury the findings — and counted,
        because silently dropping the overflow would repeat the very failure
        this banner exists to fix."""
        five = "".join(
            _added_file_diff(path, 400)
            for path in ("a/1.py", "b/2.py", "c/3.py", "d/4.py", "e/5.py")
        )
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _review_chunk_failing({
                "a/1.py": "reason A", "b/2.py": "reason B",
                "c/3.py": "reason C", "d/4.py": "reason D",
            }),
        )
        forge = FakeForge(diff=five)
        res = orchestrate_review(
            forge, REF, FakeLLM("{}"), post=True, max_chunks=5, token_budget=1000,
        )
        assert res["chunks_failed"] == 4
        tail = self._banner(forge)
        assert "…and 1 more failed chunk (see logs)" in tail
        assert tail.count("\n> - ") == orchestrator.MAX_REPORTED_REASONS + 1

    def test_exactly_the_cap_reports_every_reason_and_no_overflow_line(self):
        """The no-overflow boundary, one below the case above.

        Three distinct reasons is exactly MAX_REPORTED_REASONS, so all three
        are named and the "…and N more" line must NOT appear. It has to stay a
        genuine PARTIAL run: with all four chunks failing this is a TOTAL
        failure that never reaches the banner at all.
        """
        forge, res = self._with({
            "a/one.py": "reason A",
            "b/two.py": "reason B",
            "c/three.py": "reason C",
        })
        assert res["chunks_failed"] == orchestrator.MAX_REPORTED_REASONS
        assert res["chunks_reviewed"] == 2
        tail = self._banner(forge)
        for reason in ("reason A", "reason B", "reason C"):
            assert reason in tail
        assert "…and" not in tail
        assert tail.count("\n> - ") == orchestrator.MAX_REPORTED_REASONS

    def test_a_clean_full_review_adds_nothing(self):
        """The discriminating control: no failures, no banner at all."""
        forge, res = self._with({})
        assert res["chunks_failed"] == 0
        assert "Partial review" not in forge.summaries[0]
        assert "…and" not in forge.summaries[0]

    def test_a_summary_only_run_adds_nothing(self):
        forge = FakeForge(diff="")
        orchestrate_review(forge, REF, FakeLLM("{}"), post=True)
        assert "Partial review" not in forge.summaries[0]


class TestFailedChunkTelemetryIsCounted:
    """A chunk whose response ARRIVED and then failed to parse still spent tokens.

    Telemetry is populated before the parse, so those tokens now reach the
    totals and the PR's cost line, and the attribution names the real model
    instead of "unknown". Pinned because it is a deliberate, visible change to
    the numbers anything downstream would do cost accounting on.
    """

    def test_the_tokens_of_a_failed_chunk_reach_the_totals(self, monkeypatch):
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _review_chunk_failing({"a/one.py": TRUNCATED_REASON}, tokens=100),
        )
        forge = FakeForge(diff=FOUR_FILE_DIFF)
        res = orchestrate_review(
            forge, REF, FakeLLM("{}"), post=True, max_chunks=4, token_budget=1000,
        )
        # 4 chunks x 100 in + 100 out, the failed one included.
        assert res["input_tokens"] == 400
        assert res["output_tokens"] == 400
        assert "800 tok" in forge.summaries[0]

    def test_the_attribution_names_the_model_that_answered(self, monkeypatch):
        """Even when every chunk failed, a model DID answer, and saying
        ``model=unknown`` there hid which one."""
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _review_chunk_failing(
                {"src/one.py": TRUNCATED_REASON, "other/two.py": TRUNCATED_REASON},
                tokens=100,
            ),
        )
        forge = FakeForge(diff=TWO_FILE_DIFF)
        res = orchestrate_review(
            forge, REF, FakeLLM("{}"), post=True, max_chunks=2, token_budget=1000,
        )
        assert res["verdict"] == "Error"
        assert "model=test-model-1" in forge.summaries[0]
        assert TRUNCATED_REASON in forge.summaries[0]


# A fabricated stand-in for what ``requests`` actually produces: the gateway
# host, the full request path, and a credential riding in the query string all
# arrive inside one ConnectionError string, which invoke() wraps verbatim.
LEAKY_HOST = "gateway.internal.example"
LEAKY_SECRET = "sk-AbCdEf0123456789AbCdEf0123456789"
LEAKY_REASON = (
    "LLMError: all models failed: m1: ConnectionError: "
    f"HTTPSConnectionPool(host='{LEAKY_HOST}', port=8443): "
    "Max retries exceeded with url: "
    f"/v1/chat/completions?api_key={LEAKY_SECRET} "
    f"(Caused by NameResolutionError(\"Failed to resolve '{LEAKY_HOST}'\"))"
)


class TestPostedFailureReasonsAreSanitised:
    """A failure reason is POSTED onto a pull request, and it can carry secrets.

    ``requests`` writes the gateway host and the whole request URL — query
    string included — into a ConnectionError's string;
    ``OpenAICompatClient.invoke`` wraps that verbatim into ``LLMError``, the
    reviewer stores it in ``meta["error"]``, and both posting paths interpolate
    it into a comment. On a public repository that publishes the operator's
    endpoint and any credential riding in its query string.

    Redaction applies to what is POSTED only. stderr is operator-only, and an
    operator debugging a dead gateway needs the host, so the logs keep the full
    text.
    """

    def _partial(self, monkeypatch, reason):
        """One chunk of four fails: a genuine partial run, banner path."""
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _review_chunk_failing({"a/one.py": reason}),
        )
        forge = FakeForge(diff=FOUR_FILE_DIFF)
        res = orchestrate_review(
            forge, REF, FakeLLM("{}"), post=True, max_chunks=4, token_budget=1000,
        )
        return forge, res

    def _total(self, monkeypatch, reason):
        """Every chunk fails: the _error_run notice path."""
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _review_chunk_failing(dict.fromkeys(
                ("a/one.py", "b/two.py", "c/three.py", "d/four.py"), reason,
            )),
        )
        forge = FakeForge(diff=FOUR_FILE_DIFF)
        res = orchestrate_review(
            forge, REF, FakeLLM("{}"), post=True, max_chunks=4, token_budget=1000,
        )
        return forge, res

    def test_the_partial_banner_posts_neither_the_host_nor_the_credential(
        self, monkeypatch,
    ):
        forge, res = self._partial(monkeypatch, LEAKY_REASON)
        assert res["chunks_failed"] == 1 and res["chunks_reviewed"] == 4
        body = forge.summaries[0]
        assert "Partial review" in body
        assert LEAKY_SECRET not in body
        assert LEAKY_HOST not in body
        assert "[redacted]" in body

    def test_the_total_failure_notice_posts_neither_either(self, monkeypatch):
        forge, res = self._total(monkeypatch, LEAKY_REASON)
        assert res["verdict"] == "Error"
        body = forge.summaries[0]
        assert "could not complete" in body
        assert LEAKY_SECRET not in body
        assert LEAKY_HOST not in body
        assert "[redacted]" in body

    def test_a_forge_stage_failure_is_sanitised_on_the_same_path(self):
        """``get_pr failed: <exc>`` reaches the same notice, and a forge client
        puts its base URL in the exception just as readily."""
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        forge.fail = {"get_pr"}
        forge.pr_error = RuntimeError(
            f"404 Client Error for url: https://{LEAKY_HOST}/api/v1/pulls/1"
        )
        res = orchestrate_review(forge, REF, FakeLLM("{}"), post=True)
        assert res["verdict"] == "Error"
        body = forge.summaries[0]
        assert "get_pr failed" in body
        assert LEAKY_HOST not in body

    @pytest.mark.parametrize("path", ["partial", "total"])
    def test_the_truncation_message_and_its_lever_survive_intact(
        self, monkeypatch, path,
    ):
        """The whole point of naming the budget is that the operator can act on
        it, so redaction must not eat ``max_tokens=`` or the variable name."""
        run = self._partial if path == "partial" else self._total
        forge, _res = run(monkeypatch, TRUNCATED_REASON)
        assert TRUNCATED_REASON in forge.summaries[0]

    def test_the_diagnostic_shape_survives_on_both_paths(self, monkeypatch):
        """Redacted is not the same as useless: the exception class and the
        failure mode still reach the reader."""
        forge, _res = self._partial(monkeypatch, LEAKY_REASON)
        assert "ConnectionError" in forge.summaries[0]
        assert "LLMError" in forge.summaries[0]

    def test_the_unredacted_reason_still_reaches_the_log(self, monkeypatch, caplog):
        """The discriminating control: the operator's stderr is NOT degraded."""
        with caplog.at_level(logging.ERROR, logger="prxref"):
            forge, _res = self._partial(monkeypatch, LEAKY_REASON)
        assert LEAKY_SECRET in caplog.text
        assert LEAKY_HOST in caplog.text
        assert LEAKY_SECRET not in forge.summaries[0]

    def test_the_total_failure_log_keeps_the_full_reason_too(
        self, monkeypatch, caplog,
    ):
        with caplog.at_level(logging.ERROR, logger="prxref"):
            forge, _res = self._total(monkeypatch, LEAKY_REASON)
        assert LEAKY_SECRET in caplog.text
        assert LEAKY_SECRET not in forge.summaries[0]

    def test_a_clean_reason_is_posted_unchanged(self, monkeypatch):
        """Control: redaction must not fire on a reason carrying no secret."""
        forge, _res = self._partial(monkeypatch, "RuntimeError: connection reset")
        assert (
            "- chunk of 1 file (a/one.py): RuntimeError: connection reset"
            in forge.summaries[0]
        )
        assert "[redacted]" not in forge.summaries[0]


class TestRedactForPost:
    """Unit coverage for the single redaction both posting paths call."""

    @pytest.mark.parametrize("reason", [
        "ConnectionError: failed to reach https://gw.example.test/v1/chat?key=abc123",
        "InvalidURL: http://user:pw@gw.example.test:8443/v1",
    ])
    def test_a_url_is_replaced_wholesale(self, reason):
        out = orchestrator.redact_for_post(reason)
        assert "://" not in out
        assert "gw.example.test" not in out
        assert "[redacted]" in out

    def test_a_host_fragment_is_replaced_quoted_or_bare(self):
        quoted = orchestrator.redact_for_post("Pool(host='gw.example.test', port=443)")
        bare = orchestrator.redact_for_post("Pool(host=gw.example.test, port=443)")
        assert "gw.example.test" not in quoted
        assert "gw.example.test" not in bare

    def test_a_bare_quoted_hostname_is_replaced_too(self):
        """``Failed to resolve 'gw.example.test'`` is not a key=value pair."""
        out = orchestrator.redact_for_post(
            "NameResolutionError: Failed to resolve 'gw.example.test'"
        )
        assert "gw.example.test" not in out
        assert "NameResolutionError" in out

    @pytest.mark.parametrize("reason", [
        "auth failed: api_key=sk-AbCdEf0123456789AbCdEf0123456789",
        "auth failed: token=AbCdEf0123456789AbCdEf0123456789",
        "auth failed: key=AbCdEf0123456789AbCdEf0123456789",
        "auth failed: Authorization: Bearer AbCdEf0123456789AbCdEf",
    ])
    def test_credential_shapes_are_replaced(self, reason):
        out = orchestrator.redact_for_post(reason)
        assert "AbCdEf" not in out
        assert "[redacted]" in out

    def test_an_ip_address_is_replaced(self):
        out = orchestrator.redact_for_post("ConnectionError: no route to 10.11.12.13")
        assert "10.11.12.13" not in out

    def test_the_truncation_message_is_byte_identical(self):
        """``max_tokens=`` is a key=value pair and must NOT be mistaken for a
        credential; the env-var hint must survive too."""
        assert orchestrator.redact_for_post(TRUNCATED_REASON) == TRUNCATED_REASON

    @pytest.mark.parametrize("reason", [
        "LLMError: all models failed: m1: HTTP 429",
        "RuntimeError: timeout",
        "get_pr failed: boom get_pr",
        "parse_unified_diff failed: unparseable diff",
        "worker review JSON is not an object: list",
    ])
    def test_a_reason_carrying_nothing_sensitive_is_untouched(self, reason):
        assert orchestrator.redact_for_post(reason) == reason

    def test_redaction_is_idempotent(self):
        """Re-redacting must not chew on its own placeholder: the kv rule used
        to re-match ``url=[redacted]`` and leave a stray bracket behind."""
        once = orchestrator.redact_for_post(LEAKY_REASON)
        assert orchestrator.redact_for_post(once) == once
        assert "[redacted]]" not in once

    def test_the_model_name_is_allowed_through(self):
        """``_attribution`` already posts ``model=`` by design."""
        out = orchestrator.redact_for_post("failed on model=glm-5.3-flash")
        assert "model=glm-5.3-flash" in out


class TestMultiLineReasonsStayInTheBlockquote:
    """A reason with a newline used to break out of the ``> ⚠️`` blockquote.

    ``_failure_reason_lines`` prefixed ``> `` once per REASON, not once per
    LINE, so a provider that answers with a two-line message mangled the whole
    comment from that point on.
    """

    def _run(self, monkeypatch, reason):
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _review_chunk_failing({"a/one.py": reason}),
        )
        forge = FakeForge(diff=FOUR_FILE_DIFF)
        res = orchestrate_review(
            forge, REF, FakeLLM("{}"), post=True, max_chunks=4, token_budget=1000,
        )
        return forge, res

    MULTILINE = "RuntimeError: upstream refused the request\nsecond line of the reason"

    def test_every_line_carries_the_blockquote_marker(self, monkeypatch):
        forge, res = self._run(monkeypatch, self.MULTILINE)
        assert res["chunks_failed"] == 1
        tail = forge.summaries[0].split("> ⚠️ Partial review:")[1]
        assert [
            line for line in tail.splitlines()[1:]
            if line.strip() and not line.startswith(">")
        ] == []

    def test_the_continuation_text_is_still_reported(self, monkeypatch):
        forge, _res = self._run(monkeypatch, self.MULTILINE)
        assert "second line of the reason" in forge.summaries[0]


class TestChunkFilesLabel:
    """The per-chunk file list rendered into the banner's reason line."""

    def test_a_single_file_chunk_says_file(self):
        assert orchestrator._chunk_files_label(("a/one.py",)) == (
            "chunk of 1 file (a/one.py)"
        )

    def test_up_to_three_files_are_listed_in_full(self):
        assert orchestrator._chunk_files_label(("a.py", "b.py", "c.py")) == (
            "chunk of 3 files (a.py, b.py, c.py)"
        )

    def test_beyond_three_the_rest_are_counted(self):
        assert orchestrator._chunk_files_label(("a.py", "b.py", "c.py", "d.py", "e.py")) == (
            "chunk of 5 files (a.py, b.py, c.py, +2 more)"
        )


class TestFailureReasonLinesCarryTheChunk:
    """Unit coverage for the per-chunk shape of the banner's reason lines."""

    def test_the_reason_is_redacted_but_the_files_are_not(self):
        lines = orchestrator._failure_reason_lines([(LEAKY_REASON, ["a/one.py"])])
        assert len(lines) == 1
        assert lines[0].startswith("- chunk of 1 file (a/one.py): ")
        assert LEAKY_SECRET not in lines[0]
        assert "ConnectionError" in lines[0]

    def test_multiline_reasons_keep_their_continuation_indented(self):
        lines = orchestrator._failure_reason_lines([
            ("boom\nsecond line", ["a/one.py"]),
        ])
        assert lines == [
            "- chunk of 1 file (a/one.py): boom",
            "  second line",
        ]

    def test_the_cap_counts_chunk_lines_and_the_overflow_counts_chunks(self):
        failed = [(f"reason {n}", [f"{n}.py"]) for n in ("a", "b", "c", "d")]
        lines = orchestrator._failure_reason_lines(failed)
        bullets = [line for line in lines if line.startswith("- ")]
        assert len(bullets) == orchestrator.MAX_REPORTED_REASONS + 1
        assert bullets[-1] == "- …and 1 more failed chunk (see logs)"

    def test_identical_chunk_and_reason_pairs_collapse(self):
        pair = ("same reason", ["a/one.py"])
        assert orchestrator._failure_reason_lines([pair, pair]) == [
            "- chunk of 1 file (a/one.py): same reason",
        ]

    def test_empty_reasons_are_skipped(self):
        assert orchestrator._failure_reason_lines([("", ["a/one.py"])]) == []


class TestPartialBannerNamesTheFailedChunksFiles:
    """Issue #31: "7 of 8 chunks were reviewed; 1 failed" never said WHICH,
    so the operator could not tell which files went unreviewed. The
    orchestrator knows each chunk's file list at failure time; the banner
    now carries it on the chunk's reason line."""

    def _run(self, monkeypatch, errors_by_path, *, diff, **kwargs):
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _review_chunk_failing(errors_by_path),
        )
        forge = FakeForge(diff=diff)
        res = orchestrate_review(forge, REF, FakeLLM("{}"), post=True, **kwargs)
        return forge, res

    def _banner(self, forge):
        return forge.summaries[0].split("> ⚠️ Partial review:")[1]

    def test_the_banner_line_carries_the_failed_chunks_files(self, monkeypatch):
        forge, _res = self._run(
            monkeypatch,
            {"a/one.py": "LLMError: timeout"},
            diff=FOUR_FILE_DIFF, max_chunks=4, token_budget=1000,
        )
        assert (
            "> - chunk of 1 file (a/one.py): LLMError: timeout"
            in self._banner(forge)
        )

    def test_a_multi_file_chunk_lists_three_then_counts_the_rest(self, monkeypatch):
        five = "".join(
            _added_file_diff(path, 3)
            for path in ("a/1.py", "b/2.py", "c/3.py", "d/4.py", "e/5.py")
        )
        forge, res = self._run(
            monkeypatch,
            {"a/1.py": "LLMError: timeout"},
            diff=five, max_chunks=5, token_budget=100_000, max_files_per_chunk=4,
        )
        assert res["chunks_failed"] == 1
        # 2 review units: the failing chunk and the systemic sweep, which
        # always runs and counts as one more unit.
        assert res["chunks_reviewed"] == 2
        assert (
            "> - chunk of 4 files (a/1.py, b/2.py, c/3.py, +1 more): "
            "LLMError: timeout" in self._banner(forge)
        )

    def test_the_unreviewed_files_are_not_redacted(self, monkeypatch):
        """Paths are not secrets: the file list renders verbatim even while
        the reason beside it is redacted."""
        forge, _res = self._run(
            monkeypatch,
            {"a/one.py": LEAKY_REASON},
            diff=FOUR_FILE_DIFF, max_chunks=4, token_budget=1000,
        )
        body = forge.summaries[0]
        assert "a/one.py" in body
        assert LEAKY_SECRET not in body
        assert LEAKY_HOST not in body


class TestMalformedLocationsAreDropped:
    """Issue #32: a worker answering ``file: "package."`` used to render as
    ``- 🟧 `package.:—```. A finding whose file names no path of the diff is
    dropped into the audit record with a reason, and the summary only shows
    findings anchored to files the diff actually touches."""

    PAYLOAD = [
        {"file": "package.", "line": 0, "severity": "warning",
         "confidence": 0.9, "title": "Bad location", "body": "data"},
        {"file": "src/app.py", "line": 3, "severity": "warning",
         "confidence": 0.9, "title": "Good location", "body": "data"},
    ]

    def _run(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF,
            FakeLLM(findings_by_path={"src/app.py": self.PAYLOAD}),
        )
        return forge, res

    def test_a_file_outside_the_diff_is_dropped_with_a_reason(self):
        _forge, res = self._run()
        assert [f.file for f in res["findings_active"]] == ["src/app.py"]
        assert len(res["findings_dropped"]) == 1
        dropped = res["findings_dropped"][0]
        assert dropped.file == "package."
        assert dropped.drop_reason == "malformed location: 'package.'"

    def test_the_malformed_bullet_never_reaches_the_summary(self):
        forge, _res = self._run()
        assert "package." not in forge.summaries[0]
        assert "Good location" in forge.summaries[0]

    def test_the_drop_reaches_the_dropped_findings_audit_section(self, monkeypatch):
        """End to end into the formatter: the dropped section tallies the
        reason and tables the finding the summary bullets omit. The formatter's
        own default template carries the dropped section; the reviewer stub
        this module installs only knows the orchestrator's template name."""
        from prxref import formatter
        from prxref.formatter import format_summary

        monkeypatch.setattr(
            formatter, "_load_summary_template",
            lambda: formatter._DEFAULT_SUMMARY_TEMPLATE,
        )
        _forge, res = self._run()
        rendered = format_summary(
            res["verdict"], res["findings_active"], res["findings_dropped"],
            chunk_count=res["chunk_count"], elapsed_ms=res["elapsed_ms"],
            input_tokens=res["input_tokens"], output_tokens=res["output_tokens"],
            model="test-model-1",
        )
        assert "- 1 × malformed location: 'package.'" in rendered
        assert "Bad location" in rendered

    def test_an_empty_file_field_is_dropped_too(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF,
            FakeLLM(findings_by_path={"src/app.py": [
                {"file": "", "line": 3, "severity": "warning",
                 "confidence": 0.9, "title": "No location", "body": "data"},
            ]}),
        )
        assert res["findings_active"] == []
        assert res["findings_dropped"][0].drop_reason == "malformed location: ''"


class TestStaleInlinePrune:
    """A re-review clears its own prior inline comments before it reads threads.

    Order is the whole point: prune must precede ``list_threads`` or the dedup
    would suppress this run's findings as already-discussed and then delete the
    comments it suppressed them against. And a clean re-review (zero findings)
    still prunes, so an Approved summary cannot keep standing above the stale
    ERROR comments of an earlier, nondeterministic run.
    """

    class PruningForge(FakeForge):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls: list[str] = []
            self.pruned = 0

        def prune_inline_comments(self, ref) -> int:
            self.calls.append("prune")
            self.pruned += 3
            return 3

        def list_threads(self, ref):
            self.calls.append("list_threads")
            return super().list_threads(ref)

    def test_prune_precedes_thread_listing(self):
        forge = self.PruningForge(diff=_added_file_diff("src/app.py", 20))
        orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS))
        assert forge.calls.index("prune") < forge.calls.index("list_threads")

    def test_a_clean_rerun_still_prunes(self):
        forge = self.PruningForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path={}))
        assert res["verdict"] == "Approved"
        assert forge.calls[0] == "prune"
        assert forge.inline_batches == []

    def test_a_prune_failure_never_aborts_the_review(self):
        class ExplodingPrune(self.PruningForge):
            def prune_inline_comments(self, ref) -> int:
                self.calls.append("prune")
                raise RuntimeError("boom prune")

        forge = ExplodingPrune(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS))
        assert res["posted"] is True
        assert len(forge.inline_batches) == 1

    def test_forges_without_the_capability_are_skipped(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS))
        assert res["posted"] is True
        assert len(forge.summaries) == 1


class TestPostModeKnobs:
    """post_mode narrows which forge writes happen; the two lists of calls are
    exclusive by construction, not by accident.

    ``summary`` must never reach ``post_inline_comments`` and ``inline`` must
    never reach ``post_summary`` — on any path, the empty-diff and error
    notices included, or a mode meant to quiet the reviewer would still shout
    on the days it fails.
    """

    def _forge(self) -> FakeForge:
        return FakeForge(diff=_added_file_diff("src/app.py", 20))

    def test_the_default_posts_summary_then_inline(self):
        forge = self._forge()
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS))
        assert len(forge.summaries) == 1
        assert len(forge.inline_batches) == 1
        assert res["posted"] is True

    def test_summary_mode_never_calls_post_inline_comments(self):
        forge = self._forge()
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS), post_mode="summary",
        )
        assert len(forge.summaries) == 1
        assert forge.inline_batches == []
        assert res["posted"] is True

    def test_inline_mode_never_calls_post_summary(self):
        forge = self._forge()
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS), post_mode="inline",
        )
        assert forge.summaries == []
        assert len(forge.inline_batches) == 1
        assert [c.line for c in forge.inline_batches[0]] == [3, 7]
        assert res["posted"] is True

    def test_inline_mode_does_not_gate_on_a_summary_that_never_happens(self):
        """Combined mode lets the inline batch ride on the summary landing;
        inline mode has no summary, so nothing may gate the batch away."""
        forge = self._forge()
        forge.fail = {"post_summary"}
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS),
            post_mode="summary+inline",
        )
        assert res["posted"] is False
        assert forge.inline_batches == []
        forge = self._forge()
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS), post_mode="inline",
        )
        assert res["posted"] is True

    def test_inline_mode_with_no_surviving_findings_posts_nothing(self):
        forge = self._forge()
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=_findings((3, "error", 0.1))),
            post_mode="inline",
        )
        assert res["findings_active"] == []
        assert forge.summaries == []
        assert forge.inline_batches == []
        assert res["posted"] is False

    def test_summary_mode_on_an_empty_diff_still_posts_the_summary(self):
        forge = FakeForge(diff="")
        res = orchestrate_review(forge, REF, FakeLLM(), post_mode="summary")
        assert len(forge.summaries) == 1
        assert forge.inline_batches == []
        assert res["posted"] is True

    def test_inline_mode_posts_nothing_on_an_empty_diff(self):
        forge = FakeForge(diff="")
        res = orchestrate_review(forge, REF, FakeLLM(), post_mode="inline")
        assert forge.summaries == []
        assert forge.inline_batches == []
        assert res["posted"] is False

    def test_inline_mode_skips_the_total_failure_notice(self, monkeypatch):
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _review_chunk_failing({"src/one.py": "reason A", "other/two.py": "reason B"}),
        )
        forge = FakeForge(diff=TWO_FILE_DIFF)
        res = orchestrate_review(
            forge, REF, FakeLLM("{}"), max_chunks=2, token_budget=1000,
            post_mode="inline",
        )
        assert res["verdict"] == "Error"
        assert forge.summaries == []
        assert res["posted"] is False

    def test_summary_mode_keeps_the_total_failure_notice(self, monkeypatch):
        """The notice is a summary post; a summary-only operator still gets it."""
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _review_chunk_failing({"src/one.py": "reason A", "other/two.py": "reason B"}),
        )
        forge = FakeForge(diff=TWO_FILE_DIFF)
        res = orchestrate_review(
            forge, REF, FakeLLM("{}"), max_chunks=2, token_budget=1000,
            post_mode="summary",
        )
        assert res["verdict"] == "Error"
        assert len(forge.summaries) == 1
        assert "could not complete" in forge.summaries[0]

    def test_inline_mode_skips_the_stage_failure_notice(self):
        forge = self._forge()
        forge.fail = {"get_pr"}
        res = orchestrate_review(forge, REF, FakeLLM(), post_mode="inline")
        assert res["verdict"] == "Error"
        assert forge.summaries == []
        assert res["posted"] is False

    def test_an_inline_post_failure_keeps_posted_false(self):
        forge = self._forge()
        forge.fail = {"post_inline"}
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS), post_mode="inline",
        )
        assert res["posted"] is False

    def test_post_false_beats_any_post_mode(self):
        """``post`` is the master switch; a mode selects among writes that
        ``post=False`` has already removed."""
        forge = self._forge()
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS),
            post=False, post_mode="summary",
        )
        assert forge.summaries == []
        assert forge.inline_batches == []
        assert res["posted"] is False

    def test_an_unknown_library_mode_posts_nothing(self):
        """``load_config`` rejects the vocabulary with exit 2; a library caller
        bypassing it gets the plain no-op the membership tests produce."""
        forge = self._forge()
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS), post_mode="everything",
        )
        assert forge.summaries == []
        assert forge.inline_batches == []
        assert res["posted"] is False

    def test_result_key_set_is_unchanged_by_the_new_knobs(self):
        forge = self._forge()
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS),
            post_mode="inline", post_verdict=False,
        )
        assert set(res) == RESULT_KEYS


class TestPostVerdictKnobs:
    """post_verdict=False renders the summary without the verdict stamp,
    keeping the findings, the counts, and the attribution."""

    def test_the_default_keeps_the_verdict_in_the_posted_summary(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS))
        assert res["verdict"] == "Request-Changes"
        assert "Request-Changes" in forge.summaries[0]

    def test_post_verdict_false_omits_the_verdict_and_keeps_the_rest(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS), post_verdict=False,
        )
        summary = forge.summaries[0]
        assert "Request-Changes" not in summary
        assert "**prxref review**" in summary
        assert "Add widget" in summary
        assert "Files reviewed: 1" in summary
        assert "🟥 1 error" in summary
        assert "Null deref" in summary
        assert "Reviewed by prxref · model=test-model-1 · 150 tok" in summary

    def test_the_computed_verdict_is_still_returned(self):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS), post_verdict=False,
        )
        assert res["verdict"] == "Request-Changes"

    def test_the_empty_diff_summary_omits_approved_too(self):
        forge = FakeForge(diff="")
        orchestrate_review(forge, REF, FakeLLM(), post_verdict=False)
        summary = forge.summaries[0]
        assert "Approved" not in summary
        assert "No findings — nice work." in summary

    def test_the_total_failure_notice_still_names_its_status(self):
        """The notice's job is to say the review failed; the knob governs the
        review summary's verdict stamp, not the failure announcement."""
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        forge.fail = {"get_pr"}
        res = orchestrate_review(forge, REF, FakeLLM(), post_verdict=False)
        assert res["verdict"] == "Error"
        assert "Error" in forge.summaries[0]

    def test_the_fallback_template_renders_a_clean_header(self, monkeypatch):
        def _boom(name):
            raise RuntimeError("no prompts")

        monkeypatch.setattr(orchestrator.reviewer, "load_prompt", _boom)
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS), post_verdict=False,
        )
        assert forge.summaries[0].splitlines()[0] == "🤖 **prxref review**"

    def test_the_shipped_template_style_strips_to_a_clean_header(self):
        out = orchestrator._strip_verdict_stamp("## prxref automated review: {verdict}\n")
        assert out == "## prxref automated review\n"


class TestRunTrace:
    """Every exit closes the ``run`` node, and says which kind of exit it was.

    The pipeline view reads open-vs-closed to answer "did this finish?". A path
    that returns without closing is reported as a run still in flight forever,
    which is the one answer worse than no answer — so each of the seven ways
    out of ``orchestrate_review`` is walked here, not just the happy one.
    """

    def _run(self, tmp_path, forge, llm, **kw):
        tmp_path.mkdir(parents=True, exist_ok=True)
        target = tmp_path / "run.jsonl"
        res = orchestrate_review(forge, REF, llm, trace_file=str(target), **kw)
        events = [json.loads(x) for x in target.read_text().splitlines() if x.strip()]
        return res, events

    def _run_phases(self, events):
        return [e["phase"] for e in events if e["node"] == "run"]

    def test_a_completed_review_opens_and_closes_the_run(self, tmp_path):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        _, events = self._run(tmp_path, forge, FakeLLM(findings_by_path=HAPPY_FINDINGS))
        assert self._run_phases(events) == ["start", "ok"]
        closing = [e for e in events if e["node"] == "run" and e["phase"] == "ok"][0]
        assert closing["meta"]["chunks_reviewed"] == 2
        assert closing["meta"]["findings"] == 2

    def test_the_stages_of_a_completed_review_are_all_there(self, tmp_path):
        """The view draws fixed stages; a missing one reads as "never ran"."""
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        _, events = self._run(tmp_path, forge, FakeLLM(findings_by_path=HAPPY_FINDINGS))
        opened = {e["node"] for e in events if e["phase"] == "start"}
        assert opened == {
            "run", "forge.get_pr", "forge.get_diff", "parse_diff", "build_chunks",
            "chunk", "sweep", "post",
        }
        assert {e["node"] for e in events if e["phase"] == "ok"} >= {
            "forge.get_pr", "forge.get_diff", "parse_diff", "build_chunks",
            "chunk", "sweep", "post", "run",
        }

    def test_posting_that_was_turned_off_says_so_rather_than_looking_unreached(
        self, tmp_path
    ):
        """A stage that was skipped and one that was never reached look
        identical in a graph built only from what happened, and they mean
        opposite things: a choice versus a failure upstream."""
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        _, events = self._run(
            tmp_path, forge, FakeLLM(findings_by_path=HAPPY_FINDINGS), post=False
        )
        post = [e for e in events if e["node"] == "post"]
        assert [e["phase"] for e in post] == ["skip"]
        assert "disabled" in post[0]["meta"]["reason"]
        assert forge.summaries == [] and forge.inline_batches == []

    def test_a_post_mode_that_posts_nothing_is_also_a_skip(self, tmp_path):
        """Control: the skip reason must distinguish WHY, not just that."""
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        _, events = self._run(
            tmp_path, forge, FakeLLM(findings_by_path=HAPPY_FINDINGS),
            post=True, post_mode="inline",
        )
        post = [e for e in events if e["node"] == "post"]
        assert [e["phase"] for e in post] == ["start", "ok"]
        assert post[-1]["meta"]["inline"] == 2

    def test_a_failed_post_closes_the_post_node_as_failed(self, tmp_path):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        forge.fail.update({"post_summary", "post_inline"})
        _, events = self._run(tmp_path, forge, FakeLLM(findings_by_path=HAPPY_FINDINGS))
        assert [e["phase"] for e in events if e["node"] == "post"] == ["start", "fail"]

    @pytest.mark.parametrize("failing", ["get_pr", "get_diff"])
    def test_a_forge_failure_closes_the_run_as_failed(self, tmp_path, failing):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        forge.fail.add(failing)
        _, events = self._run(tmp_path, forge, FakeLLM())
        assert self._run_phases(events) == ["start", "fail"]
        stage = "forge." + failing
        assert [e["phase"] for e in events if e["node"] == stage] == ["start", "fail"]

    @pytest.mark.parametrize("stage", ["parse_unified_diff", "build_chunks"])
    def test_a_pipeline_stage_crash_closes_the_run_as_failed(
        self, tmp_path, monkeypatch, stage
    ):
        def boom(*a, **kw):
            raise ValueError("boom")

        monkeypatch.setattr(prxref.orchestrator, stage, boom)
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        _, events = self._run(tmp_path, forge, FakeLLM())
        assert self._run_phases(events) == ["start", "fail"]

    def test_an_empty_diff_closes_the_run_as_ok(self, tmp_path):
        """Nothing to review is a completed review, not a failure — the
        exit-code doctrine says so, and the trace must agree with it."""
        _, events = self._run(tmp_path, FakeForge(diff=""), FakeLLM())
        assert self._run_phases(events) == ["start", "ok"]

    def test_total_llm_failure_closes_the_run_as_failed(self, tmp_path):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        _, events = self._run(tmp_path, forge, FakeLLM(error=RuntimeError("no model")))
        assert self._run_phases(events) == ["start", "fail"]

    def test_a_failed_chunk_closes_its_own_node_too(self, tmp_path):
        """A worker catches and returns rather than raising, so the span never
        unwinds; without an explicit close the chunk reads as still running."""
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        _, events = self._run(tmp_path, forge, FakeLLM(error=RuntimeError("no model")))
        assert [e["phase"] for e in events if e["node"] == "chunk"] == ["start", "fail"]

    def test_a_chunk_that_REPORTS_an_error_also_closes_as_failed(
        self, tmp_path, monkeypatch
    ):
        """The other half of the failed-chunk path, and the quieter one.

        A worker that raises is caught by the except branch; a worker that
        hands back {"error": ...} — a truncated completion, a malformed body —
        returns normally, so nothing unwinds and the close has to be explicit.
        """
        def reports_error(llm, files, **kw):
            return [], {
                "escalations": [], "input_tokens": 1, "output_tokens": 1,
                "model": "test-model-1", "elapsed_ms": 1,
                "error": "truncated: finish_reason=length",
            }

        monkeypatch.setattr(prxref.orchestrator.reviewer, "review_chunk", reports_error)
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        _, events = self._run(tmp_path, forge, FakeLLM())
        chunk = [e for e in events if e["node"] == "chunk"]
        assert [e["phase"] for e in chunk] == ["start", "fail"]
        assert "truncated" in chunk[-1]["meta"]["error"]

    def test_the_error_path_records_its_own_posting_too(self, tmp_path):
        """The gap the happy-path fix left, found by running it for real.

        A total-LLM-failure run returns through `_error_run`, which posts an
        error notice of its own and never reached the instrumented block — so
        the picture showed `post` grey, "not reached", for a run that had just
        posted. Same lie, different route.
        """
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        _, events = self._run(tmp_path, forge, FakeLLM(error=RuntimeError("no model")))
        assert [e["phase"] for e in events if e["node"] == "post"] == ["start", "ok"]
        assert len(forge.summaries) == 1
        assert "could not complete" in forge.summaries[0]

    def test_the_error_path_reports_a_failed_post_as_failed(self, tmp_path):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        forge.fail.add("post_summary")
        _, events = self._run(tmp_path, forge, FakeLLM(error=RuntimeError("no model")))
        assert [e["phase"] for e in events if e["node"] == "post"] == ["start", "fail"]

    def test_the_error_path_says_skipped_when_posting_is_off(self, tmp_path):
        """Control: the error route must distinguish the same two states the
        happy route does, or the fix only moved the lie."""
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        _, events = self._run(
            tmp_path, forge, FakeLLM(error=RuntimeError("no model")), post=False
        )
        post = [e for e in events if e["node"] == "post"]
        assert [e["phase"] for e in post] == ["skip"]
        assert forge.summaries == []

    def test_an_empty_diff_records_its_summary_post(self, tmp_path):
        """The third early return: nothing to review still posts a summary."""
        forge = FakeForge(diff="")
        _, events = self._run(tmp_path, forge, FakeLLM())
        assert [e["phase"] for e in events if e["node"] == "post"] == ["start", "ok"]
        assert len(forge.summaries) == 1

    @pytest.mark.parametrize("failing", ["get_pr", "get_diff"])
    def test_a_forge_failure_still_records_its_error_notice(self, tmp_path, failing):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        forge.fail.add(failing)
        _, events = self._run(tmp_path, forge, FakeLLM())
        assert [e["phase"] for e in events if e["node"] == "post"] == ["start", "ok"]

    def test_every_route_out_of_a_review_touches_the_post_node(self, tmp_path):
        """The property, stated once, rather than one route at a time.

        Whatever happens, the picture must be able to say what posting did —
        ran, failed, or was never asked. A route that emits nothing renders
        identically to a run that died upstream.
        """
        cases = [
            ("get_pr fails", lambda f: f.fail.add("get_pr"), FakeLLM(), {}),
            ("get_diff fails", lambda f: f.fail.add("get_diff"), FakeLLM(), {}),
            ("empty diff", lambda f: setattr(f, "diff", ""), FakeLLM(), {}),
            ("llm dies", lambda f: None, FakeLLM(error=RuntimeError("x")), {}),
            ("happy", lambda f: None, FakeLLM(findings_by_path=HAPPY_FINDINGS), {}),
            ("inline only", lambda f: None,
             FakeLLM(findings_by_path=HAPPY_FINDINGS), {"post_mode": "inline"}),
            ("no post", lambda f: None,
             FakeLLM(findings_by_path=HAPPY_FINDINGS), {"post": False}),
        ]
        for i, (name, mutate, llm, kw) in enumerate(cases):
            forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
            mutate(forge)
            _, events = self._run(tmp_path / str(i), forge, llm, **kw)
            phases = [e["phase"] for e in events if e["node"] == "post"]
            assert phases in (["start", "ok"], ["start", "fail"], ["skip"]), \
                f"{name}: post node phases were {phases}"

    def test_tracing_is_off_when_no_file_is_named(self, tmp_path, monkeypatch):
        """Control: the events above are produced by the trace file, not by
        something that would have written them regardless."""
        monkeypatch.delenv("PRXREF_TRACE_FILE", raising=False)
        monkeypatch.chdir(tmp_path)
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        orchestrate_review(forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS))
        assert list(tmp_path.iterdir()) == []


SWEEP_SIGNAL_DIFF = (
    "diff --git a/src/supabase.ts b/src/supabase.ts\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/supabase.ts\n"
    "@@ -0,0 +1,3 @@\n"
    "+import { createClient } from '@supabase/supabase-js';\n"
    "+const supabase = createClient(process.env.SUPABASE_URL, 'service-key');\n"
    "+export default supabase;\n"
    + _added_file_diff("src/plain.ts", 3)
)


def _sweep_double(results: list, **meta_overrides):
    """A review_systemic double that pops one scripted result per call.

    Each entry is either ``("findings", [payload dicts])`` or
    ``("error", "reason")``; telemetry defaults apply unless overridden.
    """
    calls: list[dict] = []

    def _review_systemic(
        llm, digest, *, pr_title="", pr_description="", repo_hint="",
        max_tokens=None,
    ):
        calls.append({"digest": digest, "max_tokens": max_tokens})
        kind, payload = results.pop(0)
        meta = {
            "escalations": [], "input_tokens": 7, "output_tokens": 3,
            "model": "sweep-model", "elapsed_ms": 1, "error": "",
        }
        meta.update(meta_overrides)
        if kind == "error":
            meta["error"] = payload
            return [], meta
        return [Finding(
            file=item["file"], line=item["line"], severity=item["severity"],
            confidence=item["confidence"], title=item["title"],
            body=item["body"],
        ) for item in payload], meta

    return _review_systemic, calls


class TestSystemicSweep:
    """The whole-PR sweep: one extra worker-style unit per review."""

    SWEEP_FINDING = [{
        "file": "src/supabase.ts", "line": 2, "severity": "warning",
        "confidence": 0.85, "title": "Service key in client bundle",
        "body": "The client is built from a literal service key; the token data leaks.",
    }]

    def test_the_sweep_runs_once_and_receives_the_whole_pr_digest(self, monkeypatch):
        double, calls = _sweep_double([("findings", [])])
        monkeypatch.setattr(orchestrator.reviewer, "review_systemic", double)
        forge = FakeForge(diff=SWEEP_SIGNAL_DIFF)
        res = orchestrate_review(forge, REF, FakeLLM('{"findings": []}'), post=False)

        assert len(calls) == 1
        digest = calls[0]["digest"]
        assert "## src/supabase.ts" in digest
        assert "## src/plain.ts" in digest
        assert "+2| const supabase = createClient(" in digest
        # plain.ts matches no pattern, but at 3 added lines it is a small
        # file: the digest renders its full added content.
        assert "+1| data 1" in digest
        # Both files fit one chunk; +1 for the sweep unit.
        assert res["chunk_count"] == 2
        assert res["chunks_reviewed"] == 2

    def test_sweep_findings_flow_to_active_inline_and_summary(self, monkeypatch):
        double, _calls = _sweep_double([("findings", self.SWEEP_FINDING)])
        monkeypatch.setattr(orchestrator.reviewer, "review_systemic", double)
        forge = FakeForge(diff=SWEEP_SIGNAL_DIFF)
        res = orchestrate_review(forge, REF, FakeLLM('{"findings": []}'))

        assert [f.title for f in res["findings_active"]] == [
            "Service key in client bundle",
        ]
        # A warning-severity finding approves; it still posts inline.
        assert res["verdict"] == "Approved"
        comments = forge.inline_batches[0]
        assert [c.line for c in comments] == [2]
        assert "Service key in client bundle" in comments[0].body
        assert "Service key in client bundle" in forge.summaries[0]

    def test_sweep_tokens_and_model_reach_the_totals(self, monkeypatch):
        double, _calls = _sweep_double([("findings", [])])
        monkeypatch.setattr(orchestrator.reviewer, "review_systemic", double)
        forge = FakeForge(diff=SWEEP_SIGNAL_DIFF)
        res = orchestrate_review(forge, REF, FakeLLM("{}"), post=False)
        # 1 chunk x (100+50) from FakeLLM + the sweep's (7+3).
        assert res["input_tokens"] == 107
        assert res["output_tokens"] == 53

    def test_a_max_tokens_override_reaches_the_sweep(self, monkeypatch):
        double, calls = _sweep_double([("findings", [])])
        monkeypatch.setattr(orchestrator.reviewer, "review_systemic", double)
        orchestrate_review(
            FakeForge(diff=SWEEP_SIGNAL_DIFF), REF, FakeLLM("{}"),
            post=False, max_tokens=9001,
        )
        assert calls[0]["max_tokens"] == 9001

    def test_a_sweep_finding_restating_a_surviving_chunk_finding_is_dropped(
        self, monkeypatch,
    ):
        double, _calls = _sweep_double([("findings", [dict(self.SWEEP_FINDING[0])])])
        monkeypatch.setattr(orchestrator.reviewer, "review_systemic", double)
        forge = FakeForge(diff=SWEEP_SIGNAL_DIFF)
        res = orchestrate_review(forge, REF, FakeLLM(
            '{"findings": [{"file": "src/supabase.ts", "line": 2, '
            '"severity": "warning", "confidence": 0.9, '
            '"title": "Service key in client bundle", "body": "data token leaks"}]}'
        ), post=False)
        assert len(res["findings_active"]) == 1
        assert res["findings_active"][0].confidence == 0.9
        dupes = [f for f in res["findings_dropped"]
                 if f.drop_reason == "duplicate of chunk finding"]
        assert len(dupes) == 1
        assert dupes[0].file == "src/supabase.ts"

    def test_a_dying_chunk_finding_cannot_suppress_its_sweep_duplicate(
        self, monkeypatch,
    ):
        """The dedup pass runs AFTER the quality gate on purpose: a chunk
        finding below the floor must not suppress the sweep's higher-
        confidence restatement and then die at the gate itself."""
        double, _calls = _sweep_double([("findings", [dict(
            self.SWEEP_FINDING[0], confidence=0.9,
        )])])
        monkeypatch.setattr(orchestrator.reviewer, "review_systemic", double)
        res = orchestrate_review(
            FakeForge(diff=SWEEP_SIGNAL_DIFF), REF, FakeLLM(
                '{"findings": [{"file": "src/supabase.ts", "line": 2, '
                '"severity": "warning", "confidence": 0.3, '
                '"title": "Service key in client bundle", "body": "data token leaks"}]}'
            ),
            post=False, confidence_floor=0.6,
        )
        assert len(res["findings_active"]) == 1
        assert res["findings_active"][0].confidence == 0.9

    def test_a_sweep_failure_counts_as_one_failed_chunk(self, monkeypatch):
        double, _calls = _sweep_double([("error", "LLMError: all models failed")])
        monkeypatch.setattr(orchestrator.reviewer, "review_systemic", double)
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(forge, REF, FakeLLM('{"findings": []}'))

        assert res["chunk_count"] == 2
        assert res["chunks_reviewed"] == 1
        assert res["chunks_failed"] == 1
        assert res["verdict"] == "Approved"
        summary = forge.summaries[0]
        assert "Partial review: 1 of 2 chunks were reviewed; 1 failed." in summary
        assert "systemic sweep" in summary

    def test_all_chunks_failing_is_still_a_total_failure_even_if_the_sweep_answers(
        self, monkeypatch,
    ):
        """The sweep sees only a pattern digest; it cannot carry a review
        whose every chunk died into an "Approved, no findings" verdict."""
        double, _calls = _sweep_double([("findings", self.SWEEP_FINDING)])
        monkeypatch.setattr(orchestrator.reviewer, "review_systemic", double)

        def _all_fail(llm, files, **kwargs):
            return [], {
                "escalations": [], "input_tokens": 0, "output_tokens": 0,
                "model": "", "elapsed_ms": 0, "error": "LLMError: provider down",
            }

        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _all_fail)
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(forge, REF, FakeLLM("{}"), post=True)
        assert res["verdict"] == "Error"
        assert res["chunks_failed"] == 2
        assert "Partial review" not in forge.summaries[0]

    def test_an_empty_diff_runs_no_sweep(self, monkeypatch):
        double, calls = _sweep_double([("findings", [])])
        monkeypatch.setattr(orchestrator.reviewer, "review_systemic", double)
        res = orchestrate_review(FakeForge(diff=""), REF, FakeLLM("{}"), post=False)
        assert calls == []
        assert res["chunk_count"] == 0


class TestChunkTimeoutRetry:
    """A chunk that outruns the LLM deadline gets ONE smaller-prompt retry."""

    TIMEOUT = "LLMError: m1: timeout (ReadTimeout)"

    @staticmethod
    def _scripted_double(outcomes: list[dict]):
        """A review_chunk double popping one outcome dict per call."""
        calls: list[dict] = []

        def _rc(llm, files, *, pr_title="", pr_description="", repo_hint="",
                max_tokens=None, context_lines=None):
            calls.append({"context_lines": context_lines, "path": files[0].path})
            outcome = outcomes.pop(0)
            return [Finding(
                file=files[0].path, line=1, severity="outofscope",
                confidence=0.9, title="ok", body="b",
            )] if outcome.get("findings") else [], {
                "escalations": [], "input_tokens": 5, "output_tokens": 5,
                "model": "m", "elapsed_ms": 1,
                "error": outcome.get("error", ""),
            }

        return _rc, calls

    def test_a_timed_out_chunk_is_retried_once_with_zero_context(self, monkeypatch):
        double, calls = self._scripted_double([
            {"error": self.TIMEOUT},
            {"findings": True},
        ])
        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", double)
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(forge, REF, FakeLLM("{}"), post=False, context_lines=3)

        assert [(c["context_lines"]) for c in calls] == [3, 0]
        assert res["chunks_failed"] == 0
        assert res["chunks_reviewed"] == 2

    def test_a_persistent_timeout_still_fails_the_chunk(self, monkeypatch):
        double, calls = self._scripted_double([
            {"error": self.TIMEOUT}, {"error": self.TIMEOUT},
        ])
        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", double)
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF, FakeLLM("{}"), post=True, context_lines=3,
        )
        assert len(calls) == 2
        # The only chunk failed, so this is the total-failure notice path
        # (both units counted failed), and the notice names the timeout.
        assert res["verdict"] == "Error"
        assert res["chunks_failed"] == 2
        assert res["chunks_reviewed"] == 0
        assert "timeout" in forge.summaries[0]

    def test_a_non_timeout_error_is_not_retried(self, monkeypatch):
        double, calls = self._scripted_double([
            {"error": "LLMError: m1: HTTP 500"}, {"findings": True},
        ])
        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", double)
        res = orchestrate_review(
            FakeForge(diff=_added_file_diff("src/app.py", 20)), REF, FakeLLM("{}"),
            post=False, context_lines=3,
        )
        assert len(calls) == 1
        assert res["verdict"] == "Error"
        assert res["chunks_failed"] == 2

    def test_a_timeout_at_zero_context_is_not_retried(self, monkeypatch):
        """context_lines=0 is already the smallest rendering; an identical
        prompt would meet an identical fate."""
        double, calls = self._scripted_double([
            {"error": self.TIMEOUT}, {"findings": True},
        ])
        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", double)
        res = orchestrate_review(
            FakeForge(diff=_added_file_diff("src/app.py", 20)), REF, FakeLLM("{}"),
            post=False, context_lines=0,
        )
        assert len(calls) == 1
        assert res["verdict"] == "Error"
        assert res["chunks_failed"] == 2

    def test_truncation_is_never_retried(self, monkeypatch):
        """finish_reason=length is the RESPONSE-side budget, not the deadline;
        shrinking the prompt is not its lever."""
        double, calls = self._scripted_double([
            {"error": TRUNCATED_REASON}, {"findings": True},
        ])
        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", double)
        res = orchestrate_review(
            FakeForge(diff=_added_file_diff("src/app.py", 20)), REF, FakeLLM("{}"),
            post=False, context_lines=3,
        )
        assert len(calls) == 1
        assert res["verdict"] == "Error"
        assert res["chunks_failed"] == 2

    def test_the_retry_predicate_is_the_backend_timeout_vocabulary(self):
        assert orchestrator._is_timeout_error(
            "LLMError: m1: timeout (ReadTimeout)")
        assert orchestrator._is_timeout_error("LLMError: m2: Timeout (read)")
        assert not orchestrator._is_timeout_error("LLMError: m1: HTTP 500")
        assert not orchestrator._is_timeout_error("JSONDecodeError: bad json")
        assert not orchestrator._is_timeout_error("")


class TestSamplingRecord:
    """``sampling`` rides EVERY exit of ``orchestrate_review``.

    The three exits are the normal return, ``_summary_only_run`` (empty
    diff), and ``_error_run`` (five call sites); each is driven below.
    """

    class _Sampled(FakeLLM):
        temperature = 0.0
        seed = 11
        models = ["fast", "slow"]

    def test_normal_return_carries_the_client_knobs(self):
        forge = FakeForge(diff=TWO_FILE_DIFF)
        res = orchestrate_review(forge, REF, self._Sampled(), post=False)
        assert res["sampling"] == {
            "temperature": 0.0, "seed": 11, "models": ["fast", "slow"],
        }

    def test_empty_diff_exit_carries_the_client_knobs(self):
        forge = FakeForge(diff="")
        res = orchestrate_review(forge, REF, self._Sampled(), post=False)
        assert res["sampling"]["seed"] == 11

    def test_error_exit_carries_the_client_knobs(self):
        forge = FakeForge(diff=TWO_FILE_DIFF)
        forge.fail.add("get_diff")
        res = orchestrate_review(forge, REF, self._Sampled(), post=False)
        assert res["verdict"] == "Error"
        assert res["sampling"]["models"] == ["fast", "slow"]

    def test_a_silent_client_still_gets_all_three_keys(self):
        forge = FakeForge(diff=TWO_FILE_DIFF)
        res = orchestrate_review(forge, REF, FakeLLM(), post=False)
        assert res["sampling"] == {
            "temperature": None, "seed": None, "models": [],
        }
