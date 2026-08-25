"""Tests for prxref.reviewer: template loading, chunk rendering, and review_chunk."""
from __future__ import annotations

import json

from prxref.llm import InvokeResult
from prxref.reviewer import load_prompt, render_chunk, review_chunk
from prxref.triage import Finding, parse_unified_diff

MINI_DIFF = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,5 @@
 import os
+import sys
 def main():
-    print("hi")
+    print("hello")
+    return 0
"""

CLEAN_RESPONSE = json.dumps({
    "findings": [
        {
            "file": "src/app.py",
            "line": 3,
            "severity": "error",
            "confidence": 0.9,
            "title": "Unchecked import",
            "body": "sys is imported but unused in this file.",
        },
        {
            "file": "src/app.py",
            "line": 5,
            "severity": "Warning",
            "title": "Missing confidence",
            "body": "Should default to 0.5 confidence.",
        },
    ],
    "escalations": [
        {"file": "src/app.py", "line": 3, "concern": "Need caller context"}
    ],
})


class FakeLLM:
    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def invoke(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4096,
        json_mode: bool = False,
        timeout_s: float = 60.0,
    ) -> InvokeResult:
        self.calls.append({
            "system": system,
            "user": user,
            "max_tokens": max_tokens,
            "json_mode": json_mode,
            "timeout_s": timeout_s,
        })
        return InvokeResult(
            text=self.text,
            input_tokens=100,
            output_tokens=50,
            model="fake-model",
            backend="fake",
            elapsed_ms=12,
        )


class TestLoadPrompt:
    def test_loads_worker_template_via_resources(self):
        content = load_prompt("worker.md")
        assert "findings" in content
        assert "{diff}" in content
        assert "## Review Context" in content

    def test_loads_summary_template_via_resources(self):
        content = load_prompt("summary.md")
        for placeholder in ("{verdict}", "{attribution}", "{elapsed_ms}", "{findings_table}"):
            assert placeholder in content


class TestRenderChunk:
    def test_round_trip_preserves_path_and_added_lines(self):
        parsed = parse_unified_diff(MINI_DIFF)
        rendered = render_chunk(parsed)
        reparsed = parse_unified_diff(rendered)
        assert len(reparsed) == 1
        assert reparsed[0].path == "src/app.py"
        assert reparsed[0].added_lines == parsed[0].added_lines

    def test_added_file_renders_dev_null_old_path(self):
        diff = (
            "diff --git a/new.py b/new.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/new.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+a = 1\n"
            "+b = 2\n"
        )
        parsed = parse_unified_diff(diff)
        rendered = render_chunk(parsed)
        assert "--- /dev/null" in rendered
        assert "+++ b/new.py" in rendered
        reparsed = parse_unified_diff(rendered)
        assert reparsed[0].added_lines == {1, 2}

    def test_binary_file_renders_differ_marker(self):
        diff = (
            "diff --git a/logo.png b/logo.png\n"
            "Binary files a/logo.png and b/logo.png differ\n"
        )
        parsed = parse_unified_diff(diff)
        rendered = render_chunk(parsed)
        assert "Binary files a/logo.png and b/logo.png differ" in rendered


class TestReviewChunk:
    def _chunk(self):
        return parse_unified_diff(MINI_DIFF)

    def test_clean_json_maps_findings_and_escalations(self):
        llm = FakeLLM(CLEAN_RESPONSE)
        findings, meta = review_chunk(llm, self._chunk())
        assert len(findings) == 2
        assert findings[0] == Finding(
            file="src/app.py",
            line=3,
            severity="error",
            confidence=0.9,
            title="Unchecked import",
            body="sys is imported but unused in this file.",
        )
        assert findings[1].confidence == 0.5
        assert findings[1].severity == "Warning"
        assert meta["escalations"] == [{"file": "src/app.py", "line": 3, "concern": "Need caller context"}]

    def test_fenced_json_is_recovered(self):
        fenced = f"Here is the review:\n```json\n{CLEAN_RESPONSE}\n```\n"
        llm = FakeLLM(fenced)
        findings, _ = review_chunk(llm, self._chunk())
        assert len(findings) == 2

    def test_trailing_comma_json_is_recovered(self):
        broken = CLEAN_RESPONSE[:-1] + ",}"
        llm = FakeLLM(broken)
        findings, _ = review_chunk(llm, self._chunk())
        assert len(findings) == 2

    def test_broken_json_returns_empty_tuple_never_raises(self):
        llm = FakeLLM("I am not returning JSON.")
        findings, meta = review_chunk(llm, self._chunk())
        assert findings == []
        assert meta["escalations"] == []

    def test_non_object_json_returns_empty_tuple(self):
        llm = FakeLLM("[1, 2, 3]")
        findings, meta = review_chunk(llm, self._chunk())
        assert findings == []
        assert meta["escalations"] == []

    def test_llm_exception_returns_empty_tuple_never_raises(self):
        class ExplodingLLM:
            def invoke(self, *args, **kwargs):
                raise RuntimeError("upstream timeout")

        findings, meta = review_chunk(ExplodingLLM(), self._chunk())
        assert findings == []
        assert meta["escalations"] == []

    def test_invoke_called_with_json_mode_and_token_cap(self):
        llm = FakeLLM(CLEAN_RESPONSE)
        review_chunk(llm, self._chunk())
        assert len(llm.calls) == 1
        assert llm.calls[0]["json_mode"] is True
        assert llm.calls[0]["max_tokens"] == 4096

    def test_prompt_renders_diff_text_and_pr_context(self):
        llm = FakeLLM(CLEAN_RESPONSE)
        review_chunk(
            llm,
            self._chunk(),
            pr_title="Fix crash on start",
            pr_description="Replaces print with greeting",
            repo_hint="acme/service",
        )
        call = llm.calls[0]
        user = call["user"]
        system = call["system"]

        assert "src/app.py" in user
        assert '+    print("hello")' in user
        assert "Fix crash on start" in user
        assert "Replaces print with greeting" in user
        assert "acme/service" in user
        assert "{diff}" not in user
        assert "{pr_title}" not in user
        assert "## Review Context" in user
        assert "## Review Context" not in system
        assert len(system) > 0

    def test_skips_finding_entries_without_file(self):
        payload = json.dumps({
            "findings": [
                {"line": 1, "severity": "note", "title": "Missing file", "body": "no file key"},
                {"file": "src/app.py", "line": 2, "severity": "note", "title": "OK", "body": "has file"},
            ],
            "escalations": [],
        })
        llm = FakeLLM(payload)
        findings, _ = review_chunk(llm, self._chunk())
        assert len(findings) == 1
        assert findings[0].file == "src/app.py"

    def test_dirty_escalations_filtered_to_dicts_only(self):
        payload = json.dumps({
            "findings": [],
            "escalations": [{"concern": "valid"}, "not a dict", 42],
        })
        llm = FakeLLM(payload)
        _, meta = review_chunk(llm, self._chunk())
        assert meta["escalations"] == [{"concern": "valid"}]

    def test_missing_findings_key_returns_empty(self):
        payload = json.dumps({"escalations": []})
        llm = FakeLLM(payload)
        findings, meta = review_chunk(llm, self._chunk())
        assert findings == []
        assert meta["escalations"] == []
