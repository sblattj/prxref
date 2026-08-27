"""Tests for prxref.reviewer: template loading, chunk rendering, and review_chunk."""
from __future__ import annotations

import json
import logging

import pytest

from prxref.llm import InvokeResult
from prxref.reviewer import MAX_TOKENS, load_prompt, render_chunk, review_chunk
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
    def __init__(self, text: str, finish_reason: str = ""):
        self.text = text
        self.finish_reason = finish_reason
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
            finish_reason=self.finish_reason,
        )


class TestLoadPrompt:
    def test_loads_worker_template_via_resources(self):
        content = load_prompt("worker.md")
        assert "findings" in content
        assert "{diff}" in content
        assert "## Review Context" in content

    def test_loads_summary_template_via_resources(self):
        content = load_prompt("summary.md")
        for placeholder in ("{verdict}", "{attribution}", "{findings}", "{error_count}"):
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
        assert meta["error"]

    def test_non_object_json_returns_empty_tuple(self):
        llm = FakeLLM("[1, 2, 3]")
        findings, meta = review_chunk(llm, self._chunk())
        assert findings == []
        assert meta["escalations"] == []
        assert "not an object" in meta["error"]

    def test_llm_exception_returns_empty_tuple_never_raises(self):
        class ExplodingLLM:
            def invoke(self, *args, **kwargs):
                raise RuntimeError("upstream timeout")

        findings, meta = review_chunk(ExplodingLLM(), self._chunk())
        assert findings == []
        assert meta["escalations"] == []
        assert "upstream timeout" in meta["error"]

    def test_successful_review_reports_empty_error(self):
        llm = FakeLLM(CLEAN_RESPONSE)
        _findings, meta = review_chunk(llm, self._chunk())
        assert meta["error"] == ""

    def test_invoke_called_with_json_mode_and_token_cap(self):
        llm = FakeLLM(CLEAN_RESPONSE)
        review_chunk(llm, self._chunk())
        assert len(llm.calls) == 1
        assert llm.calls[0]["json_mode"] is True
        assert llm.calls[0]["max_tokens"] == 4096

    def test_explicit_max_tokens_reaches_the_payload(self):
        llm = FakeLLM(CLEAN_RESPONSE)
        review_chunk(llm, self._chunk(), max_tokens=12000)
        assert llm.calls[0]["max_tokens"] == 12000

    def test_none_max_tokens_falls_back_to_the_module_default(self):
        llm = FakeLLM(CLEAN_RESPONSE)
        review_chunk(llm, self._chunk(), max_tokens=None)
        assert llm.calls[0]["max_tokens"] == MAX_TOKENS == 4096

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


TRUNCATED_RESPONSE = '{"findings": [{"file": "src/app.py", "line": 3, "sev'


class TestTruncationIsNamedInsteadOfDecoded:
    """A starved completion budget must read as a budget problem, not a model one.

    Both failures arrive as the same ``JSONDecodeError``; only ``finish_reason``
    separates them, and only one of the two has a lever the operator can pull.
    The reported error has to name that lever, because ``JSONDecodeError: no
    parseable content`` sends an operator looking at prompts and models instead
    of at ``PRXREF_LLM_MAX_TOKENS``.
    """

    def _chunk(self):
        return parse_unified_diff(MINI_DIFF)

    def test_cut_off_json_names_the_budget_and_the_lever(self):
        llm = FakeLLM(TRUNCATED_RESPONSE, finish_reason="length")
        findings, meta = review_chunk(llm, self._chunk(), max_tokens=512)
        assert findings == []
        assert meta["error"] == (
            "response truncated at max_tokens=512 (finish_reason=length); "
            "raise PRXREF_LLM_MAX_TOKENS"
        )

    def test_empty_response_at_the_budget_is_truncation_too(self):
        """Reasoning models routinely spend the whole budget before the answer,
        which arrives as an EMPTY completion, not a partial one."""
        llm = FakeLLM("", finish_reason="length")
        findings, meta = review_chunk(llm, self._chunk(), max_tokens=4096)
        assert findings == []
        assert "max_tokens=4096" in meta["error"]
        assert "PRXREF_LLM_MAX_TOKENS" in meta["error"]

    def test_a_clean_empty_response_is_not_mislabelled_as_truncated(self):
        """The discriminating control: same empty text, different stop reason."""
        llm = FakeLLM("", finish_reason="stop")
        findings, meta = review_chunk(llm, self._chunk(), max_tokens=4096)
        assert findings == []
        assert "truncated" not in meta["error"]
        assert "PRXREF_LLM_MAX_TOKENS" not in meta["error"]
        assert "JSONDecodeError" in meta["error"]

    def test_an_unreported_stop_reason_is_not_treated_as_truncation(self):
        """A backend that reports nothing must not have truncation invented for it."""
        llm = FakeLLM("I am not returning JSON.")
        _findings, meta = review_chunk(llm, self._chunk())
        assert "truncated" not in meta["error"]
        assert "JSONDecodeError" in meta["error"]

    def test_the_budget_named_is_the_one_actually_sent(self):
        llm = FakeLLM("", finish_reason="length")
        _findings, meta = review_chunk(llm, self._chunk(), max_tokens=777)
        assert llm.calls[0]["max_tokens"] == 777
        assert "max_tokens=777" in meta["error"]

    def test_the_module_default_is_named_when_no_budget_was_passed(self):
        llm = FakeLLM("", finish_reason="length")
        _findings, meta = review_chunk(llm, self._chunk())
        assert f"max_tokens={MAX_TOKENS}" in meta["error"]

    @pytest.mark.parametrize("raw", ["length", "LENGTH", " Length "])
    def test_the_stop_reason_is_matched_case_and_space_insensitively(self, raw):
        """The string is the provider's, not ours; casing varies by gateway."""
        llm = FakeLLM("", finish_reason=raw)
        _findings, meta = review_chunk(llm, self._chunk())
        assert "truncated" in meta["error"]

    @pytest.mark.parametrize("raw", ["stop", "content_filter", "tool_calls", ""])
    def test_every_other_stop_reason_keeps_the_parse_error(self, raw):
        llm = FakeLLM("", finish_reason=raw)
        _findings, meta = review_chunk(llm, self._chunk())
        assert "truncated" not in meta["error"]

    def test_an_invoke_failure_still_reports_the_exception(self):
        """Control: not every empty-handed chunk is a truncation."""
        class ExplodingLLM:
            def invoke(self, *args, **kwargs):
                raise RuntimeError("upstream timeout")

        _findings, meta = review_chunk(ExplodingLLM(), self._chunk())
        assert meta["error"] == "RuntimeError: upstream timeout"

    def test_a_result_without_a_finish_reason_attribute_never_raises(self):
        """The never-raise contract covers a test double or backend that
        predates ``InvokeResult.finish_reason``."""
        class LegacyResult:
            text = "not json"
            input_tokens = 1
            output_tokens = 2
            model = "legacy"

        class LegacyLLM:
            def invoke(self, *args, **kwargs):
                return LegacyResult()

        findings, meta = review_chunk(LegacyLLM(), self._chunk())
        assert findings == []
        assert "JSONDecodeError" in meta["error"]

    def test_telemetry_survives_the_truncation_branch(self):
        """The response arrived, so its token counts are real and must be kept:
        they are how an operator sees the budget being consumed."""
        llm = FakeLLM("", finish_reason="length")
        _findings, meta = review_chunk(llm, self._chunk())
        assert meta["input_tokens"] == 100
        assert meta["output_tokens"] == 50
        assert meta["model"] == "fake-model"


class TestTruncatedButParseableStillWarns:
    """A response that hit the cap and still parsed is a success with a hole in it."""

    def _chunk(self):
        return parse_unified_diff(MINI_DIFF)

    def test_findings_are_kept_and_the_chunk_counts_as_reviewed(self):
        llm = FakeLLM(CLEAN_RESPONSE, finish_reason="length")
        findings, meta = review_chunk(llm, self._chunk())
        assert len(findings) == 2
        assert meta["error"] == ""

    def test_the_budget_and_the_lever_are_logged(self, caplog):
        llm = FakeLLM(CLEAN_RESPONSE, finish_reason="length")
        with caplog.at_level(logging.WARNING, logger="prxref"):
            review_chunk(llm, self._chunk(), max_tokens=8192)
        assert "max_tokens=8192" in caplog.text
        assert "PRXREF_LLM_MAX_TOKENS" in caplog.text

    def test_a_clean_response_logs_no_such_warning(self, caplog):
        """Control: the warning must not fire on every successful review."""
        llm = FakeLLM(CLEAN_RESPONSE, finish_reason="stop")
        with caplog.at_level(logging.WARNING, logger="prxref"):
            review_chunk(llm, self._chunk(), max_tokens=8192)
        assert "PRXREF_LLM_MAX_TOKENS" not in caplog.text


class TestOtherProviderTruncationSpellings:
    """A plain OpenAI-compatible proxy may pass an upstream provider's own word
    through untouched; litellm normalises, a bare gateway need not."""

    def _chunk(self):
        return parse_unified_diff(MINI_DIFF)

    @pytest.mark.parametrize("raw", ["max_tokens", "MAX_TOKENS", " Max_Tokens "])
    def test_the_native_budget_spellings_are_recognised(self, raw):
        llm = FakeLLM("", finish_reason=raw)
        _findings, meta = review_chunk(llm, self._chunk(), max_tokens=64)
        assert "response truncated at max_tokens=64" in meta["error"]
        assert "PRXREF_LLM_MAX_TOKENS" in meta["error"]

    def test_the_message_quotes_what_the_provider_actually_said(self):
        """Reporting ``finish_reason=length`` for a provider that said
        ``max_tokens`` would send someone grepping for a string not in the log."""
        llm = FakeLLM("", finish_reason="max_tokens")
        _findings, meta = review_chunk(llm, self._chunk())
        assert "(finish_reason=max_tokens)" in meta["error"]

    @pytest.mark.parametrize("raw", ["LENGTH", " Length ", "Max_Tokens"])
    def test_the_reason_is_quoted_as_the_provider_spelled_it(self, raw):
        """Matching is casefolded; the message is not. Reporting a normalised
        ``length`` for a gateway that logged ``LENGTH`` sends an operator
        grepping for a string that is not in their log."""
        llm = FakeLLM("", finish_reason=raw)
        _findings, meta = review_chunk(llm, self._chunk())
        assert f"(finish_reason={raw.strip()})" in meta["error"]

    @pytest.mark.parametrize("raw", ["length_finish", "max_tokens_reached", "lengthy"])
    def test_a_neighbouring_spelling_is_not_a_false_positive(self, raw):
        """Matching is exact against the accepted set, never a substring test."""
        llm = FakeLLM("", finish_reason=raw)
        _findings, meta = review_chunk(llm, self._chunk())
        assert "truncated" not in meta["error"]
        assert "JSONDecodeError" in meta["error"]


class TestTruncationSurvivesAWrongShapedResponse:
    """Valid JSON of the wrong shape is as unusable as none, and the budget can
    be why it came out that way."""

    def _chunk(self):
        return parse_unified_diff(MINI_DIFF)

    def test_a_truncated_non_object_still_names_the_budget(self):
        llm = FakeLLM("[1, 2, 3]", finish_reason="length")
        findings, meta = review_chunk(llm, self._chunk(), max_tokens=128)
        assert findings == []
        assert "response truncated at max_tokens=128" in meta["error"]

    def test_a_clean_non_object_keeps_the_shape_message(self):
        """The discriminating control: same bytes, honest stop reason."""
        llm = FakeLLM("[1, 2, 3]", finish_reason="stop")
        _findings, meta = review_chunk(llm, self._chunk())
        assert meta["error"] == "worker review JSON is not an object: list"

    def test_the_budget_warning_does_not_fire_for_an_unusable_response(self, caplog):
        """"findings may be incomplete" would be wrong here — there are none at
        all, and the error already says why."""
        llm = FakeLLM("[1, 2, 3]", finish_reason="length")
        with caplog.at_level(logging.WARNING, logger="prxref"):
            review_chunk(llm, self._chunk())
        assert "findings may be incomplete" not in caplog.text
