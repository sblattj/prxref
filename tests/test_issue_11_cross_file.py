"""Issue 11: a worker concludes from one file when a sibling refutes it.

Chunks are built risk-first and capped, so the file that refutes a claim
regularly lands in ANOTHER chunk (issue #57's reporter examples: a build-time
``define`` flagged against docs in the same diff, a "chmod never happens /
--force is unsupported" finding refuted by install.sh in the same diff). The
worker saw only its chunk, so an absence claim was unfalsifiable by
construction.

Frozen contract under test:

- ``review_chunk`` takes ``sibling_files`` (the PR's full parsed list; the
  caller passes it untouched) and renders every file NOT in the chunk under a
  ``### Other files changed in this PR`` header after the diff: path, status,
  +/- counts, and a bounded excerpt of the file's added/context hunk lines.
- A file already in the chunk is never restated as its own sibling.
- The excerpt is bounded per file (``MAX_SIBLING_LINES_PER_FILE``) with an
  omitted-count line; a chunk holding the whole PR renders no header at all.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_reviewer import FakeLLM  # noqa: E402  (shared double, read not guessed)

from prxref.chunk_context import (  # noqa: E402
    MAX_SIBLING_LINES_PER_FILE,
    SIBLING_HEADER,
)
from prxref.reviewer import review_chunk  # noqa: E402
from prxref.triage import parse_unified_diff  # noqa: E402

EMPTY_RESPONSE = '{"findings":[],"escalations":[]}'

TEST_FILE_DIFF = (
    "diff --git a/tests/cli.test.ts b/tests/cli.test.ts\n"
    "--- a/tests/cli.test.ts\n"
    "+++ b/tests/cli.test.ts\n"
    "@@ -1,1 +1,2 @@\n"
    " import { test } from 'vitest';\n"
    "+expect(execMode).toBe('no chmod is performed');\n"
)

INSTALL_SH_DIFF = (
    "diff --git a/install.sh b/install.sh\n"
    "--- a/install.sh\n"
    "+++ b/install.sh\n"
    "@@ -1,1 +1,2 @@\n"
    " #!/bin/sh\n"
    "+chmod +x bin/tool\n"
)


def _rendered_user_prompt(chunk_diff: str, sibling_diff: str) -> str:
    """Run the public render path: chunk = first diff, siblings = both."""
    llm = FakeLLM(EMPTY_RESPONSE)
    review_chunk(
        llm, parse_unified_diff(chunk_diff),
        sibling_files=parse_unified_diff(chunk_diff + sibling_diff),
    )
    return llm.calls[0]["user"]


def _sibling_part(user: str) -> str:
    """Text from the sibling header onward — the summary, never the diff."""
    return user.split(SIBLING_HEADER, 1)[1]


class TestSiblingEvidenceReachesThePrompt:
    """Test A: the refuting install.sh line must reach the worker prompt."""

    def test_refuting_sibling_line_is_summarized_below_the_diff(self):
        user = _rendered_user_prompt(TEST_FILE_DIFF, INSTALL_SH_DIFF)
        assert SIBLING_HEADER in user
        # The summary sits after the chunk's diff, where worker.md points at it.
        assert "diff --git a/tests/cli.test.ts" in user.split(SIBLING_HEADER, 1)[0]
        part = _sibling_part(user)
        assert "- install.sh (modified, +1/-0):" in part
        assert "+chmod +x bin/tool" in part


class TestNoDuplication:
    """Test B: a chunk's own files are not restated as their own siblings."""

    def test_a_file_in_the_chunk_is_not_restated_as_its_own_sibling(self):
        user = _rendered_user_prompt(TEST_FILE_DIFF, INSTALL_SH_DIFF)
        part = _sibling_part(user)
        assert "tests/cli.test.ts" not in part
        assert SIBLING_HEADER not in part


class TestTheSummaryIsBounded:
    """Test C: a huge sibling file is truncated, never rendered in full."""

    def test_a_huge_sibling_file_is_truncated_with_an_omitted_count(self):
        body = "\n".join(f"+pad line {i}" for i in range(1, 101))
        big = (
            "diff --git a/generated.ts b/generated.ts\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/generated.ts\n"
            "@@ -0,0 +1,100 @@\n"
            f"{body}\n"
        )
        part = _sibling_part(_rendered_user_prompt(TEST_FILE_DIFF, big))
        assert f"+pad line {MAX_SIBLING_LINES_PER_FILE}" in part
        assert f"+pad line {MAX_SIBLING_LINES_PER_FILE + 1}" not in part
        hidden = 100 - MAX_SIBLING_LINES_PER_FILE
        assert f"… {hidden} more lines not shown" in part


class TestControls:
    """Prompts that must carry no sibling block at all."""

    def test_no_sibling_files_renders_no_header(self):
        llm = FakeLLM(EMPTY_RESPONSE)
        review_chunk(llm, parse_unified_diff(TEST_FILE_DIFF))
        assert SIBLING_HEADER not in llm.calls[0]["user"]

    def test_a_chunk_holding_the_whole_pr_renders_no_header(self):
        llm = FakeLLM(EMPTY_RESPONSE)
        files = parse_unified_diff(TEST_FILE_DIFF + INSTALL_SH_DIFF)
        review_chunk(llm, files, sibling_files=files)
        assert SIBLING_HEADER not in llm.calls[0]["user"]
