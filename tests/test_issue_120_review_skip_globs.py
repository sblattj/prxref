"""Issue 120: generated files the gate should not send to the model.

The repro: a release PR carried a regenerated 3.5 MB vendored bundle. Its
chunk failed with an HTTP 400, the review came back ``Incomplete``, and the
PR showed only "changes requested - 0 inline finding(s)" with no cause.

Frozen contracts under test: ``PRXREF_REVIEW_SKIP_GLOBS`` drops matching
files before chunking (no chunk or sweep sees them), the run record lists
them in ``review_skipped``, the summary names them, the default is empty and
changes nothing, and the key is env-only (a ``.prxref.toml`` cannot set it,
since the gate reads that file from the PR head). A failed gate review names
its reason and each failed unit in the review body.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_orchestrator import (  # noqa: E402  (shared fixtures, read not guessed)
    REF,
    FakeForge,
    FakeLLM,
    _added_file_diff,
)

from prxref import config  # noqa: E402
from prxref.gate import checks  # noqa: E402
from prxref.llm import ConfigError  # noqa: E402
from prxref.orchestrator import orchestrate_review  # noqa: E402

VENDOR = "pkg/_vendor/bundle.mjs"
DIFF = _added_file_diff("src/app.py", 20) + _added_file_diff(VENDOR, 20)


def _finding(path):
    return {
        "file": path,
        "line": 3,
        "severity": "error",
        "confidence": 0.9,
        "title": "Null deref",
        "body": "data may be None when config is missing; data loss follows.",
    }


class _SpyLLM(FakeLLM):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.users: list[str] = []

    def invoke(self, system, user, **kw):
        self.users.append(user)
        return super().invoke(system, user, **kw)


def _review(globs=()):
    forge = FakeForge(diff=DIFF)
    llm = _SpyLLM(findings_by_path={VENDOR: [_finding(VENDOR)]})
    res = orchestrate_review(forge, REF, llm, review_skip_globs=globs)
    return forge, llm, res


@pytest.mark.usefixtures("contract_stubs")
class TestSkipGlobs:
    def test_a_matching_file_reaches_no_model_call(self):
        _, llm, res = _review(["pkg/_vendor/*"])
        assert not any(VENDOR in u for u in llm.users)
        assert res["verdict"] == "Approved"
        assert res["review_skipped"] == [VENDOR]

    def test_the_summary_names_the_skipped_file(self):
        forge, _, _ = _review(["pkg/_vendor/*"])
        assert "Not reviewed (1 file matched PRXREF_REVIEW_SKIP_GLOBS)" in forge.summaries[0]
        assert f"`{VENDOR}`" in forge.summaries[0]

    def test_no_globs_reviews_everything_and_records_nothing(self):
        forge, llm, res = _review()
        assert any(VENDOR in u for u in llm.users)
        assert res["verdict"] == "Request-Changes"
        assert "review_skipped" not in res
        assert "PRXREF_REVIEW_SKIP_GLOBS" not in forge.summaries[0]

    def test_a_glob_matching_nothing_records_an_empty_list(self):
        forge, _, res = _review(["nope/*"])
        assert res["review_skipped"] == []
        assert "PRXREF_REVIEW_SKIP_GLOBS" not in forge.summaries[0]


class TestConfig:
    def test_env_sets_a_list(self, monkeypatch):
        monkeypatch.setenv("PRXREF_REVIEW_SKIP_GLOBS", "a/*,b/*.mjs")
        assert config.load_config()["review_skip_globs"] == ["a/*", "b/*.mjs"]

    def test_default_is_empty(self, monkeypatch):
        monkeypatch.delenv("PRXREF_REVIEW_SKIP_GLOBS", raising=False)
        assert config.load_config()["review_skip_globs"] == []

    def test_env_only(self):
        assert "review_skip_globs" not in config.FILE_KEYS
        assert config._ENV_ONLY_REASONS["review_skip_globs"] == "gate"

    def test_a_config_file_cannot_set_it(self, tmp_path):
        path = tmp_path / config.CONFIG_FILE_NAME
        path.write_text('review_skip_globs = ["src/*"]\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="review_skip_globs"):
            config.read_config_file(path)


class TestFailureDetail:
    RESULT = {
        "verdict": "Incomplete",
        "chunks_failed": 1,
        "failed_chunks": [
            {"unit": 2, "kind": "chunk", "files": [VENDOR], "error": "glm: HTTP 400 token ghp_" + "a" * 36},
        ],
    }

    def test_names_the_reason_unit_file_and_redacted_error(self):
        text = checks.failure_detail(self.RESULT, "incomplete review: a partial review is not a review")
        assert text.startswith("\nReason: incomplete review")
        assert f"- failed chunk 2 (`{VENDOR}`): glm: HTTP 400 [redacted]" in text
        assert "ghp_" not in text

    def test_caps_units_and_files(self):
        units = [{"unit": i, "kind": "chunk", "files": [f"f{j}" for j in range(5)], "error": "x"} for i in range(5)]
        text = checks.failure_detail({"failed_chunks": units}, "r")
        assert text.count("- failed chunk") == 3
        assert "and 2 more`" not in text and "and 2 more" in text
        assert "and 2 more failed unit(s)" in text

    def test_no_units_is_just_the_reason(self):
        assert checks.failure_detail({"verdict": "Request-Changes"}, "error-severity findings") == (
            "\nReason: error-severity findings."
        )


@pytest.mark.usefixtures("contract_stubs")
def test_a_pr_of_only_skipped_files_is_not_reviewed_and_says_so():
    forge = FakeForge(diff=_added_file_diff(VENDOR, 20))
    llm = _SpyLLM(findings_by_path={VENDOR: [_finding(VENDOR)]})
    res = orchestrate_review(forge, REF, llm, review_skip_globs=["pkg/_vendor/*"])
    assert not any(VENDOR in u for u in llm.users)
    assert res["verdict"] == "Approved"
    assert res["review_skipped"] == [VENDOR]
    assert f"`{VENDOR}`" in forge.summaries[0]
