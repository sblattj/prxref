"""Issue 06: the sweep is blind to pure deletions, and re-raises settled threads.

Two halves, one issue. (1) ``build_digest`` only admits removed lines that hit
one of the eight shipped pattern classes, none of which describe a deleted
limit constant or a deleted validator — so guard removal, an advertised sweep
class, is structurally invisible. (2) Nothing suppresses a finding that
re-litigates a decision already argued out in a PR thread; the sweep prompt
never sees the discussion at all.

Frozen contracts under test: a ``guard-removal`` digest class applied to
REMOVED lines and admitted ahead of fill classes; a ``settled in thread``
drop_reason; and an ``### Existing discussion`` block in the sweep user prompt.
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_orchestrator import (  # noqa: E402  (shared fixtures, read not guessed)
    REF,
    FakeForge,
    FakeLLM,
    _contract_review_chunk,
    _contract_review_systemic,
)

from prxref import orchestrator  # noqa: E402
from prxref.forges.base import Thread  # noqa: E402
from prxref.llm import InvokeResult  # noqa: E402
from prxref.orchestrator import orchestrate_review  # noqa: E402
from prxref.quality import is_duplicate_of_existing  # noqa: E402
from prxref.systemic import build_digest, match_class  # noqa: E402
from prxref.triage import Finding, parse_unified_diff  # noqa: E402

# The PR from the issue: three input-size guards and their enforcing filter
# deleted outright, with two innocuous added lines. Nothing added here matches
# any shipped pattern class, so the file's only systemic signal is deletions.
REGISTRY_DIFF = (
    "diff --git a/src/tools/registry.ts b/src/tools/registry.ts\n"
    "--- a/src/tools/registry.ts\n"
    "+++ b/src/tools/registry.ts\n"
    "@@ -1,9 +1,5 @@\n"
    ' import { z } from "zod";\n'
    "-const MAX_TOOL_NAME_LENGTH = 128;\n"
    "-const MAX_TOOL_DESCRIPTION_LENGTH = 4_000;\n"
    "-const MAX_TOOL_SCHEMA_BYTES = 100_000;\n"
    "-function isSupported(tool) {\n"
    "-  return tool.name.length <= MAX_TOOL_NAME_LENGTH;\n"
    "-}\n"
    " export function listTools(raw) {\n"
    "+  const list = raw.slice();\n"
    "+  return list;\n"
    " }\n"
)

SETTLED_THREAD = Thread(
    path="src/tools/registry.ts",
    line=None,
    resolved=False,
    author="bob",
    body_snippet=(
        "This still feels too defensive, I don't think we need it imo "
        "— Removed the defensive tool metadata guard in commit 917d1f4"
    ),
)

GUARD_FINDING = {
    "file": "src/tools/registry.ts",
    "line": 3,
    "severity": "warning",
    "confidence": 0.8,
    "title": "Tool metadata guard removed: unbounded tool names from remote servers",
    "body": (
        "This change removes the tool metadata guard: MAX_TOOL_NAME_LENGTH and "
        "isSupported no longer bound names or schemas supplied by a remote MCP "
        "server."
    ),
}


def _cls(text: str, kind: str = "-") -> str | None:
    """``match_class`` for a line of a given diff kind.

    ASSUMPTION recorded for the fix seat: today ``match_class(text)`` takes only
    the text, so removal cannot be a matching condition. The recommended
    signature is ``match_class(text, kind="+"|"-"|" ")`` with ``kind``
    keyword-only and defaulted, keeping every existing call site valid. This
    helper calls the new form and falls back to the old one so the controls
    below pass under both.
    """
    try:
        return match_class(text, kind=kind)
    except TypeError:
        return match_class(text)


def _digest_of(diff: str, token_budget: int = 4000) -> str:
    return build_digest(parse_unified_diff(diff), token_budget)


@pytest.fixture
def contract_stubs(monkeypatch):
    """Chunk + sweep pinned to the orchestrator contract (no real reviewer)."""
    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _contract_review_chunk)
    monkeypatch.setattr(
        orchestrator.reviewer, "review_systemic", _contract_review_systemic,
    )


class TestA_DigestSeesDeletedGuards:
    def test_digest_carries_the_removed_limit_constants_and_validator(self):
        digest = _digest_of(REGISTRY_DIFF)

        assert "MAX_TOOL_NAME_LENGTH" in digest
        assert "MAX_TOOL_DESCRIPTION_LENGTH" in digest
        assert "MAX_TOOL_SCHEMA_BYTES" in digest
        assert "isSupported" in digest
        # Rendered as removals, so the model can tell deletion from addition.
        assert "-2| const MAX_TOOL_NAME_LENGTH = 128;" in digest

    def test_removed_limit_constant_is_classed_guard_removal(self):
        assert _cls("const MAX_TOOL_NAME_LENGTH = 128;") == "guard-removal"
        assert _cls("const MAX_TOOL_SCHEMA_BYTES = 100_000;") == "guard-removal"
        assert _cls("static final int REQUEST_TIMEOUT = 30;") == "guard-removal"

    def test_removed_validator_definition_is_classed_guard_removal(self):
        assert _cls("function isSupported(tool) {") == "guard-removal"
        assert _cls("function validateSchema(s) {") == "guard-removal"
        assert _cls("def sanitize_name(name):") == "guard-removal"

    def test_guard_removal_survives_the_per_file_cap_as_a_must_see_class(self):
        from prxref.systemic import _MUST_SEE_CLASSES

        assert "guard-removal" in _MUST_SEE_CLASSES


class TestB_SettledThreadSuppression:
    def test_finding_relitigating_a_settled_thread_is_dropped(self, contract_stubs):
        forge = FakeForge(diff=REGISTRY_DIFF, threads=[SETTLED_THREAD])
        llm = FakeLLM(json.dumps({"findings": [GUARD_FINDING]}))

        res = orchestrate_review(forge, REF, llm, post=False)

        titles = {f.title for f in res["findings_dropped"]}
        assert GUARD_FINDING["title"] in titles, (
            "guard-removal finding survived a settled thread on the same file; "
            f"active={[f.title for f in res['findings_active']]}"
        )
        dropped = next(
            f for f in res["findings_dropped"] if f.title == GUARD_FINDING["title"]
        )
        assert dropped.drop_reason.startswith("settled in thread")


class TestC_SweepPromptCarriesTheDiscussion:
    def test_sweep_user_prompt_lists_threads_for_digested_files(self, monkeypatch):
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk", _contract_review_chunk,
        )
        calls: list[tuple[str, str]] = []

        class CapturingLLM:
            def invoke(self, system, user, **kwargs):
                calls.append((system, user))
                return InvokeResult(
                    text='{"findings":[],"escalations":[]}',
                    input_tokens=10, output_tokens=5,
                    model="test-model-1", backend="fake", elapsed_ms=1,
                )

        forge = FakeForge(diff=REGISTRY_DIFF, threads=[SETTLED_THREAD])
        orchestrate_review(forge, REF, CapturingLLM(), post=False)

        sweep = [u for s, u in calls if "systemic sweep" in s.lower()]
        assert sweep, f"no sweep call captured; systems={[s[:60] for s, _ in calls]}"
        user = sweep[0]
        assert "### Existing discussion" in user
        assert "Removed the defensive tool metadata guard" in user


class TestControls:
    def test_removed_console_log_is_not_guard_removal(self):
        assert _cls('console.log("x")') != "guard-removal"
        assert _cls('console.log("x")') == "console-log"

    def test_added_limit_constant_is_not_guard_removal(self):
        assert _cls("const MAX_TOOL_NAME_LENGTH = 128;", kind="+") != "guard-removal"

    def test_unrelated_thread_does_not_suppress(self, contract_stubs):
        unrelated = Thread(
            path="src/other/cache.ts", line=None, resolved=False, author="carol",
            body_snippet="Bumping the cache eviction interval before release.",
        )
        forge = FakeForge(diff=REGISTRY_DIFF, threads=[unrelated])
        llm = FakeLLM(json.dumps({"findings": [GUARD_FINDING]}))

        res = orchestrate_review(forge, REF, llm, post=False)

        assert GUARD_FINDING["title"] in {f.title for f in res["findings_active"]}

    def test_resolved_thread_on_other_subject_does_not_suppress(self, contract_stubs):
        other = Thread(
            path="src/tools/registry.ts", line=None, resolved=True, author="dave",
            body_snippet="Please rename listTools to enumerateTools for consistency.",
        )
        forge = FakeForge(diff=REGISTRY_DIFF, threads=[other])
        llm = FakeLLM(json.dumps({"findings": [GUARD_FINDING]}))

        res = orchestrate_review(forge, REF, llm, post=False)

        assert GUARD_FINDING["title"] in {f.title for f in res["findings_active"]}


class TestAlreadyCoveredBoundary:
    """What today's thread dedup DOES catch, so the fix does not re-solve it."""

    def test_line_pinned_thread_already_matches_at_the_unit_level(self):
        """The one variant today's dedup catches — but only as a unit call.

        Same line (distance 0) needs 1 shared token and 4 are shared
        ({tool, metadata, guard, removed}). With ``line=None`` the distant
        threshold is ``max(4, 17 // 2) == 8`` and the same thread is missed.
        """
        finding = Finding(
            file=GUARD_FINDING["file"], line=3, severity="warning",
            confidence=0.8, title=GUARD_FINDING["title"], body=GUARD_FINDING["body"],
        )
        pinned = replace(SETTLED_THREAD, line=3)
        assert is_duplicate_of_existing(finding, [pinned]) is True

    def test_end_to_end_even_the_pinned_thread_misses_today(self, contract_stubs):
        """Measured: ``apply_line_align`` demotes the finding to line 0 before
        dedup runs, so the distance-0 tier above never applies in the real
        pipeline. The suppression must therefore be line-independent."""
        forge = FakeForge(diff=REGISTRY_DIFF, threads=[replace(SETTLED_THREAD, line=3)])
        llm = FakeLLM(json.dumps({"findings": [GUARD_FINDING]}))

        res = orchestrate_review(forge, REF, llm, post=False)
        landed = (res["findings_active"] + res["findings_dropped"])[0]
        assert landed.line == 0
