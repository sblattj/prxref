"""Issue 01 — the worker prompt is blind to third-party library versions.

The chunk prompt carries only the diff, so a finding that turns on a
library's runtime semantics is answered from whichever major version
dominates training data. Tests A and B pin the frozen contract for the fix
(a ``### Dependency versions`` block resolved from the nearest manifest at
the PR head, plus a prompt rule about unversioned library claims); the
controls pin what must keep working when the forge offers no file reader.
"""
from __future__ import annotations

import threading

import pytest

from prxref.forges.base import PRRef
from prxref.llm import InvokeResult
from prxref.orchestrator import orchestrate_review
from tests.test_orchestrator import FakeForge, make_pr

REF = PRRef(
    forge="fake", host="fake.test", owner="acme", repo="widget",
    number=7, url="https://fake.test/acme/widget/pull/7",
)

PACKAGE_JSON = '{"name":"x","dependencies":{"effect":"4.0.0-rc.110"}}'

TS_DIFF = (
    "diff --git a/src/a.ts b/src/a.ts\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/a.ts\n"
    "@@ -0,0 +1,6 @@\n"
    "+import { Effect } from 'effect';\n"
    "+\n"
    "+export const run = (p: () => Promise<number>) =>\n"
    "+  Effect.runPromise(Effect.promise(p));\n"
    "+\n"
    "+export const label = 'data 1';\n"
)


class CapturingLLM:
    """Records every ``(system, user)`` pair; always returns no findings."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        with self._lock:
            self.calls.append((system, user))
        return InvokeResult(
            text='{"findings":[],"escalations":[]}',
            input_tokens=100,
            output_tokens=50,
            model="test-model-1",
            backend="fake",
            elapsed_ms=1,
        )

    @property
    def user_prompts(self) -> list[str]:
        return [u for _, u in self.calls]


class ContentForge(FakeForge):
    """FakeForge plus the frozen optional ``get_file_content`` contract."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.content_calls: list[tuple[str, str]] = []

    def get_file_content(self, ref: PRRef, path: str, *, sha: str) -> str | None:
        self.content_calls.append((path, sha))
        if path == "package.json" and sha == self.pr.source_sha:
            return PACKAGE_JSON
        return None


@pytest.fixture
def llm() -> CapturingLLM:
    return CapturingLLM()


def _run(forge, llm):
    return orchestrate_review(forge, REF, llm, post=False)


def test_a_dependency_versions_block_reaches_the_worker_prompt(llm):
    forge = ContentForge(pr=make_pr("Wire Effect"), diff=TS_DIFF)
    result = _run(forge, llm)

    assert result["chunk_count"] >= 1
    prompts = llm.user_prompts
    assert prompts, "no LLM call was made"
    assert any("### Dependency versions" in p for p in prompts), (
        "no worker prompt carried a '### Dependency versions' block; "
        "the chunk prompt is version-blind"
    )
    assert any("effect@4.0.0-rc.110" in p for p in prompts), (
        "the resolved pin 'effect@4.0.0-rc.110' never reached the prompt"
    )


def test_b_prompt_rules_cover_unversioned_library_claims(llm):
    forge = ContentForge(pr=make_pr("Wire Effect"), diff=TS_DIFF)
    _run(forge, llm)

    joined = "\n".join(s for s, _ in llm.calls) + "\n".join(llm.user_prompts)
    assert "library version" in joined, (
        "the worker prompt has no rule about findings that depend on a "
        "third-party library version not present in context"
    )


def test_control_plain_forge_reviews_without_dependency_block(llm):
    forge = FakeForge(pr=make_pr("Wire Effect"), diff=TS_DIFF)
    assert not hasattr(forge, "get_file_content")

    result = _run(forge, llm)

    assert result["verdict"]
    assert result["chunk_count"] >= 1
    assert result["findings_active"] == []
    prompts = llm.user_prompts
    assert prompts
    assert any("import { Effect } from 'effect';" in p for p in prompts), (
        "the diff itself never reached the worker prompt"
    )
    assert not any("### Dependency versions" in p for p in prompts), (
        "a forge with no get_file_content must not grow a dependency block"
    )


def test_control_file_content_is_asked_for_at_the_pr_head_sha(llm):
    """Whatever the fix reads, it must read it at the PR head, never blindly."""
    forge = ContentForge(pr=make_pr("Wire Effect"), diff=TS_DIFF)
    _run(forge, llm)

    assert all(sha == forge.pr.source_sha for _, sha in forge.content_calls), (
        f"get_file_content called at a non-head sha: {forge.content_calls}"
    )
