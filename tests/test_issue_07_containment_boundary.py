"""Issue #07 — 'throws' findings must name their containment boundary.

Self-contained: does not import tests/test_orchestrator.py or
tests/test_reviewer.py fixtures (that module patches
``orchestrator.reviewer.review_chunk`` via an autouse fixture scoped to its
own file only; importing it here would risk pulling in unrelated import-time
side effects). All fixtures below are the minimal shapes needed for this
issue, modeled on those two files.

Three things must exist for this suite to pass, none of which exist yet:
1. The rendered worker prompt states the containment-boundary rule.
2. A deterministic post-pass, ``prxref.quality.apply_containment_note``,
   suffixes an un-boundaried throw-class finding's body with
   " [containment boundary not stated]".
3. That post-pass is wired into ``orchestrate_review``'s pass order.

Tests A, B, C fail today (2026-09-04, prxref 0.11.1) and should pass once
the fix lands. The two "Controls" tests pass today and after — they pin
the no-false-positive behaviour using the identity fallback so they are not
themselves evidence the fix landed.
"""
from __future__ import annotations

import json

import pytest

from prxref import quality
from prxref.forges.base import PRData, PRRef
from prxref.llm import InvokeResult
from prxref.orchestrator import orchestrate_review
from prxref.reviewer import review_chunk
from prxref.triage import Finding, parse_unified_diff

# ---------------------------------------------------------------------------
# Test A: the rendered worker prompt states the rule.
# ---------------------------------------------------------------------------

MINI_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,3 @@\n"
    " import os\n"
    "+import sys\n"
    " def main():\n"
)


class _CapturingLLM:
    """Records the exact system/user text handed to invoke(); always returns
    a clean empty-findings response so review_chunk's parse never fails."""

    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.calls.append({"system": system, "user": user})
        return InvokeResult(
            text=self.text, input_tokens=10, output_tokens=5,
            model="capture-model", backend="fake", elapsed_ms=1,
        )


def test_worker_prompt_states_the_containment_boundary_rule():
    llm = _CapturingLLM('{"findings":[],"escalations":[]}')
    chunk = parse_unified_diff(MINI_DIFF)

    review_chunk(llm, chunk)

    assert len(llm.calls) == 1
    combined = llm.calls[0]["system"] + "\n" + llm.calls[0]["user"]
    assert "containment boundary" in combined, (
        "worker.md must state: a finding that asserts a throw, panic, crash, "
        "or unhandled rejection must name its containment boundary — the "
        "enclosing catch, or state that it is uncaught and name the caller "
        "it propagates to."
    )


# ---------------------------------------------------------------------------
# Test B: end to end through orchestrate_review — the fix must be WIRED IN,
# not just available as a function nobody calls.
# ---------------------------------------------------------------------------

# Mirrors issue 07's real-world shape: a throw with no catch anywhere in the
# diff, escaping into the caller. Old file: 5 lines: new file: 8 (3 added).
FIGMA_DIFF = (
    "diff --git a/src/mcp/figma.ts b/src/mcp/figma.ts\n"
    "--- a/src/mcp/figma.ts\n"
    "+++ b/src/mcp/figma.ts\n"
    "@@ -1,5 +1,8 @@\n"
    " export function register(server) {\n"
    "   for (const tool of tools) {\n"
    "+    if (typeof tool.inputSchema !== \"object\") {\n"
    "+      throw decodeServerJsonSchema(tool);\n"
    "+    }\n"
    "     server.addTool(tool);\n"
    "   }\n"
    " }\n"
)

# The issue's finding, verbatim (title + body text drawn from the issue
# file), file/line anchored on the added throw line. No boundary language
# anywhere in title or body — this is exactly the finding the issue says
# understates blast radius.
FIGMA_FINDING_JSON = json.dumps({
    "findings": [
        {
            "file": "src/mcp/figma.ts",
            "line": 4,
            "severity": "error",
            "confidence": 0.85,
            "title": (
                "decodeServerJsonSchema throws during register for "
                "non-object inputSchema"
            ),
            "body": (
                "any tool whose inputSchema is not an object throws "
                "synchronously, aborting registration of all remaining "
                "Figma tools instead of skipping that one tool."
            ),
        }
    ],
    "escalations": [],
})


class _VerbatimLLM:
    """Returns the SAME text for every invoke() call — worker chunk call
    and systemic-sweep call alike — matching FakeLLM's string-mode contract
    in tests/test_orchestrator.py."""

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.calls += 1
        return InvokeResult(
            text=self.text, input_tokens=10, output_tokens=5,
            model="figma-test-model", backend="fake", elapsed_ms=1,
        )


class _FakeForge:
    name = "fake"

    def __init__(self, pr: PRData, diff: str):
        self.pr = pr
        self.diff = diff
        self.summaries: list[str] = []
        self.inline_batches: list[list] = []

    @staticmethod
    def parse_pr_url(url: str):
        return None

    def get_pr(self, ref):
        return self.pr

    def get_diff(self, ref):
        return self.diff

    def post_summary(self, ref, body):
        self.summaries.append(body)

    def post_inline_comments(self, ref, comments):
        self.inline_batches.append(list(comments))
        return len(comments)

    def list_threads(self, ref):
        return []


def _make_pr() -> PRData:
    return PRData(
        title="Register Figma tools", description="adds tool registration",
        author="alice", source_branch="feature/figma", target_branch="main",
        source_sha="a" * 40, target_sha="b" * 40, raw={},
    )


REF = PRRef(
    forge="fake", host="fake.test", owner="acme", repo="widget",
    number=7, url="https://fake.test/acme/widget/pull/7",
)


def test_containment_note_applied_end_to_end_through_orchestrate_review():
    forge = _FakeForge(pr=_make_pr(), diff=FIGMA_DIFF)
    llm = _VerbatimLLM(FIGMA_FINDING_JSON)

    result = orchestrate_review(forge, REF, llm, post=False)

    findings = result["findings_active"]
    matches = [f for f in findings if "decodeServerJsonSchema" in f.title]
    assert matches, (
        f"expected the figma throw finding to survive the quality gates; "
        f"active findings were: {findings}"
    )
    finding = matches[0]
    assert finding.severity == "error"
    assert finding.body.endswith(" [containment boundary not stated]"), (
        f"finding body was not suffixed with the containment-boundary "
        f"warning; got: {finding.body!r}"
    )


# ---------------------------------------------------------------------------
# Test C: apply_containment_note directly, on 4 throw-class bodies with no
# boundary named. Explicit failure reason when the function does not exist.
# ---------------------------------------------------------------------------

def _require_apply_containment_note():
    fn = getattr(quality, "apply_containment_note", None)
    if fn is None:
        pytest.fail("apply_containment_note missing")
    return fn


def _finding(title: str, body: str) -> Finding:
    return Finding(
        file="src/mcp/figma.ts", line=4, severity="error", confidence=0.85,
        title=title, body=body,
    )


# Four throw-class phrasings (throws / panics / crashes / unhandled
# rejection), none naming a boundary.
THROW_CLASS_NO_BOUNDARY = [
    _finding(
        "Registration aborts on bad schema",
        "The handler throws when the payload is malformed, aborting the "
        "request entirely.",
    ),
    _finding(
        "Parser dies on empty input",
        "Parser panics on empty input, taking down the whole ingestion "
        "pipeline.",
    ),
    _finding(
        "Scheduler fails under contention",
        "The scheduler crashes when two jobs collide on the same worker "
        "slot.",
    ),
    _finding(
        "Async handler drops the promise",
        "An unhandled rejection escapes the async handler and terminates "
        "the process.",
    ),
]


class TestApplyContainmentNoteDirect:
    def test_four_throw_class_bodies_get_suffixed(self):
        apply_containment_note = _require_apply_containment_note()

        result = apply_containment_note(THROW_CLASS_NO_BOUNDARY)

        assert len(result) == len(THROW_CLASS_NO_BOUNDARY)
        assert [f.title for f in result] == [f.title for f in THROW_CLASS_NO_BOUNDARY], (
            "apply_containment_note must keep order"
        )
        for before, after in zip(THROW_CLASS_NO_BOUNDARY, result, strict=True):
            assert after.body == before.body + " [containment boundary not stated]", (
                f"not suffixed: {after.body!r}"
            )


# ---------------------------------------------------------------------------
# Controls: no false positives. Run against the real function when present,
# else an identity no-op — these must pass BOTH today and after the fix.
# ---------------------------------------------------------------------------

def _apply_containment_note_or_identity():
    return getattr(quality, "apply_containment_note", lambda fs: list(fs))


def test_control_boundary_named_finding_is_not_suffixed():
    apply_containment_note = _apply_containment_note_or_identity()
    finding = _finding(
        "decodeServerJsonSchema throws during register for non-object inputSchema",
        "The exception is uncaught here, but propagates to serverFactory "
        "and fails MCP handler construction, so all tool families go down "
        "together.",
    )

    result = apply_containment_note([finding])

    assert result[0].body == finding.body


def test_control_non_throw_finding_is_untouched():
    apply_containment_note = _apply_containment_note_or_identity()
    finding = _finding(
        "computeTotal returns the wrong total for empty carts",
        "The reduce accumulator starts at undefined instead of 0, so an "
        "empty cart returns NaN instead of 0 as the total.",
    )

    result = apply_containment_note([finding])

    assert result[0].body == finding.body
