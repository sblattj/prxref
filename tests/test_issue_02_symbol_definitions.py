"""Issue 02: the worker reasons from an identifier's NAME when its definition
sits in the same file, outside the rendered hunk.

The reviewer renders only the hunk (``reviewer.render_chunk`` →
``triage.trim_hunk_context``), so a constant defined 76 lines above the change
is never shown to the model. The fix adds an OPTIONAL forge method::

    def get_file_content(self, ref: PRRef, path: str, *, sha: str) -> str | None

resolved via ``getattr(forge, "get_file_content", None)``, and injects a
``### Definitions referenced by this chunk`` block into the worker prompt for
identifiers the chunk's ADDED lines reference but that are defined elsewhere in
the same file.

Tests A and B fail on 0.11.1; the two controls pass both before and after.
"""
from __future__ import annotations

from prxref.forges.base import PRRef
from prxref.llm import InvokeResult
from tests.test_orchestrator import REF, FakeForge, FakeLLM, make_pr  # noqa: F401

SCHEMA_PATH = "src/packages/bitbucket/src/schema.ts"
CTRL_PATH = "src/packages/bitbucket/src/ctrl.ts"

CLEAN_JSON = '{"findings": [], "escalations": []}'


class CapturingLLM:
    """Records every (system, user) pair; always returns a clean review."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.calls.append((system, user))
        return InvokeResult(
            text=CLEAN_JSON, input_tokens=1, output_tokens=1,
            model="m", backend="b", elapsed_ms=1,
        )

    @property
    def prompts(self) -> list[str]:
        return [f"{s}\n{u}" for s, u in self.calls]


def schema_post_image() -> list[str]:
    """The POST-image of schema.ts: 100 lines, PositiveInt defined at line 18."""
    lines = [f"// filler {i}" for i in range(1, 18)]           # 1..17
    lines += [                                                  # 18..21
        "const PositiveInt = Schema.Number.check(",
        "  Schema.isInt(),",
        "  Schema.isBetween({ minimum: 0, maximum: Number.MAX_SAFE_INTEGER }),",
        ");",
    ]
    lines += [f"// filler {i}" for i in range(22, 91)]           # 22..90
    lines += [                                                   # 91..97
        "export const PageParams = Schema.Struct({",
        "  limit: PositiveInt,",
        "  filter: Schema.String,",
        "  start: PositiveInt,",
        "  order: Schema.String,",
        "});",
        "// tail",
    ]
    lines += [f"// filler {i}" for i in range(98, 101)]          # 98..100
    assert len(lines) == 100
    assert lines[17].startswith("const PositiveInt")
    assert lines[93] == "  start: PositiveInt,"
    return lines


# One added line at post-image line 94, three context lines on each side.
# Old side: post lines 91,92,93,95,96,97 -> @@ -91,6 +91,7 @@.
def schema_diff() -> str:
    post = schema_post_image()
    body = [
        " " + post[90], " " + post[91], " " + post[92],
        "+" + post[93],
        " " + post[94], " " + post[95], " " + post[96],
    ]
    return (
        f"diff --git a/{SCHEMA_PATH} b/{SCHEMA_PATH}\n"
        f"--- a/{SCHEMA_PATH}\n"
        f"+++ b/{SCHEMA_PATH}\n"
        "@@ -91,6 +91,7 @@\n"
        + "\n".join(body) + "\n"
    )


def ctrl_post_image() -> list[str]:
    """Control post-image: ``Limit`` is defined INSIDE the added block."""
    lines = [f"// head {i}" for i in range(1, 5)]                # 1..4
    lines += [                                                   # 5..8
        "const Limit = 5;",
        "export const Opts = Schema.Struct({",
        "  x: Limit,",
        "});",
    ]
    lines += [f"// tail {i}" for i in range(9, 13)]              # 9..12
    return lines


def ctrl_diff() -> str:
    post = ctrl_post_image()
    body = [
        " " + post[1], " " + post[2], " " + post[3],
        "+" + post[4], "+" + post[5], "+" + post[6], "+" + post[7],
        " " + post[8], " " + post[9], " " + post[10],
    ]
    return (
        f"diff --git a/{CTRL_PATH} b/{CTRL_PATH}\n"
        f"--- a/{CTRL_PATH}\n"
        f"+++ b/{CTRL_PATH}\n"
        "@@ -2,6 +2,10 @@\n"
        + "\n".join(body) + "\n"
    )


class SourceForge(FakeForge):
    """A forge that can serve post-image file content, the frozen contract."""

    def __init__(self, files: dict[str, list[str]], **kwargs):
        super().__init__(**kwargs)
        self._files = files
        self.content_calls: list[tuple[str, str]] = []

    def get_file_content(self, ref: PRRef, path: str, *, sha: str) -> str | None:
        self.content_calls.append((path, sha))
        if sha != self.pr.source_sha:
            return None
        body = self._files.get(path)
        return "\n".join(body) + "\n" if body is not None else None


DEFS_HEADER = "### Definitions referenced by this chunk"


def _run(forge, llm):
    from prxref.orchestrator import orchestrate_review

    return orchestrate_review(forge, REF, llm, post=False)


class TestDefinitionInjection:
    def test_out_of_hunk_definition_reaches_the_worker_prompt(self):
        """Test A — the PositiveInt definition at schema.ts:18 must be shown."""
        forge = SourceForge({SCHEMA_PATH: schema_post_image()}, diff=schema_diff())
        llm = CapturingLLM()
        res = _run(forge, llm)

        assert res["chunks_reviewed"] >= 1
        assert llm.calls, "the worker never ran"
        joined = "\n\n".join(llm.prompts)
        assert DEFS_HEADER in joined, (
            "worker prompt carries no definitions block; the model only ever "
            "sees the identifier NAME"
        )
        assert "schema.ts:18: const PositiveInt" in joined

    def test_prompt_caps_confidence_on_unresolved_symbols(self):
        """Test B — the prompt must carry the unshown-symbol rule."""
        forge = SourceForge({SCHEMA_PATH: schema_post_image()}, diff=schema_diff())
        llm = CapturingLLM()
        _run(forge, llm)

        assert llm.calls, "the worker never ran"
        assert any("definition is not shown" in p for p in llm.prompts)


class TestControls:
    def test_forge_without_get_file_content_still_reviews(self):
        """Control — the method is OPTIONAL; a plain forge must not break."""
        forge = FakeForge(diff=schema_diff())
        assert not hasattr(forge, "get_file_content")
        llm = CapturingLLM()
        res = _run(forge, llm)

        assert res["verdict"] == "Approved"
        assert res["chunks_failed"] == 0
        assert res["chunks_reviewed"] >= 1
        assert all(DEFS_HEADER not in p for p in llm.prompts)

    def test_definition_inside_the_hunk_is_not_re_injected(self):
        """Control — ``Limit`` is defined on an added line, so the fix must not
        repeat it under the definitions header."""
        forge = SourceForge({CTRL_PATH: ctrl_post_image()}, diff=ctrl_diff())
        llm = CapturingLLM()
        res = _run(forge, llm)

        assert res["chunks_reviewed"] >= 1
        assert "+const Limit = 5;" in "\n\n".join(llm.prompts)
        assert all("ctrl.ts:5: const Limit" not in p for p in llm.prompts)
