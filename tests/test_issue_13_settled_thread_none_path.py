"""Issue 13: the settled-thread gate crashes on a path-less general PR comment.

``apply_settled_thread_suppression`` calls ``_normalised_path(t.path)`` for
every thread, and ``_normalised_path`` does ``path.startswith("./")`` — a
plain ``str`` method. ``Thread.path`` is typed ``str | None`` and IS None for
any general, unanchored PR comment (near-universal on Bitbucket Server), so
the pass raises ``AttributeError: 'NoneType' object has no attribute
'startswith'``. Because the quality-pass chain in ``orchestrate_review`` is
not wrapped, that exception escapes the whole review: nothing gets posted.

Frozen contract under test: a thread with ``path=None`` is skipped by this
pass (the finding survives untouched), while same-path suppression with
enough shared tokens is unaffected.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_orchestrator import (  # noqa: E402  (shared fixtures, read not guessed)
    REF,
    FakeForge,
    FakeLLM,
    _added_file_diff,
    _contract_review_chunk,
    _contract_review_systemic,
)

from prxref import orchestrator  # noqa: E402
from prxref.forges.base import Thread  # noqa: E402
from prxref.orchestrator import orchestrate_review  # noqa: E402
from prxref.quality import apply_settled_thread_suppression  # noqa: E402
from prxref.triage import Finding  # noqa: E402

# Shared title/body across every case below, and a thread snippet chosen so
# the shared-token count is exactly the SETTLED_MIN_SHARED_TOKENS floor (4:
# retry, wrapper, transient, failures) when the path matches — so case (b)
# is a genuine control on the SAME subject, not an easier one.
FINDING_TITLE = "retry wrapper removed"
FINDING_BODY = "transient failures now surface to callers"
THREAD_SNIPPET = "please keep the retry wrapper for transient failures here"
FINDING_FILE = "src/app/db.py"


def _finding() -> Finding:
    return Finding(
        file=FINDING_FILE, line=3, severity="warning", confidence=0.9,
        title=FINDING_TITLE, body=FINDING_BODY,
    )


@pytest.fixture
def contract_stubs(monkeypatch):
    """Chunk + sweep pinned to the orchestrator contract (no real reviewer)."""
    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _contract_review_chunk)
    monkeypatch.setattr(
        orchestrator.reviewer, "review_systemic", _contract_review_systemic,
    )


class TestA_DirectReproduction:
    def test_pathless_thread_leaves_the_finding_unchanged(self):
        """A general PR comment (Thread.path is None) cannot be "same path"
        as any finding, so it must not crash and must not suppress."""
        t = Thread(
            path=None, line=0, resolved=False, author="reviewer",
            body_snippet=THREAD_SNIPPET,
        )
        f = _finding()

        result = apply_settled_thread_suppression([f], [t])

        assert result == [f]
        assert result[0].drop_reason is None


class TestB_ControlSamePathStillSettles:
    def test_same_path_and_shared_tokens_still_drops(self):
        t = Thread(
            path=FINDING_FILE, line=0, resolved=False, author="reviewer",
            body_snippet=THREAD_SNIPPET,
        )
        f = _finding()

        result = apply_settled_thread_suppression([f], [t])

        assert len(result) == 1
        assert result[0].drop_reason == "settled in thread: reviewer"


class TestC_EndToEndThroughOrchestrateReview:
    def test_pathless_general_comment_does_not_lose_the_review(self, contract_stubs):
        general_comment = Thread(
            path=None, line=0, resolved=False, author="reviewer",
            body_snippet=THREAD_SNIPPET,
        )
        forge = FakeForge(
            diff=_added_file_diff(FINDING_FILE, 20),
            threads=[general_comment],
        )
        finding_payload = {
            "file": FINDING_FILE, "line": 3, "severity": "warning",
            "confidence": 0.9, "title": FINDING_TITLE, "body": FINDING_BODY,
        }
        llm = FakeLLM(json.dumps({"findings": [finding_payload]}))

        res = orchestrate_review(forge, REF, llm)

        assert res["verdict"] != "Error", (
            f"review failed instead of completing: {res}"
        )
        assert FINDING_TITLE in {f.title for f in res["findings_active"]}, (
            f"finding missing from findings_active: {res['findings_active']}"
        )
        assert res["posted"] is True
        assert len(forge.summaries) == 1


class TestControlUnrelatedPathStillActive:
    """A path-less thread on an unrelated subject must not accidentally
    suppress either — same guard, different shared-token shape."""

    def test_pathless_thread_with_no_shared_tokens_leaves_finding_active(self):
        t = Thread(
            path=None, line=0, resolved=False, author="reviewer",
            body_snippet="Bumping the cache eviction interval before release.",
        )
        f = _finding()

        result = apply_settled_thread_suppression([f], [t])

        assert result[0].drop_reason is None
