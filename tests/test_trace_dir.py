"""Per-unit prompt/response trace files: ``PRXREF_TRACE_DIR`` and ``--trace-dir``.

The structural JSONL trace (``PRXREF_TRACE_FILE``) says a chunk started,
succeeded, or failed; it never says what the model was actually asked or what
it actually answered. ``trace_dir`` is the missing half: one directory, four
files per review unit — ``<unit>.system.md``, ``<unit>.user.md``,
``<unit>.response.json`` (raw model text), ``<unit>.meta.json`` (model, token
counts, elapsed, error) — labelled ``chunk0`` … and ``sweep``.

The contract under test here mirrors ``PRXREF_TRACE_FILE``'s: unset means
zero cost (no directory, no writes, no error paths), a write failure is a
logged warning and never a review failure, and the CLI flag wins over the
environment exactly the way every other knob does.
"""
from __future__ import annotations

import json
import logging
import sys
import types
from unittest.mock import MagicMock

import pytest

from prxref import config
from prxref.cli import main
from prxref.forges.base import PRRef
from prxref.llm import InvokeResult
from prxref.orchestrator import orchestrate_review
from prxref.reviewer import review_chunk, review_systemic
from prxref.triage import parse_unified_diff
from tests.test_orchestrator import (  # noqa: F401
    REF,
    FakeForge,
    FakeLLM,
    _added_file_diff,
)

MINI_DIFF = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 import os
+import sys
 def main():
     print("hi")
"""

RAW_RESPONSE = json.dumps({"findings": [], "escalations": []})

# test_orchestrator.FakeLLM in string mode answers every invoke with this
# verbatim — the right mode here, because this module runs the REAL reviewer,
# whose prompts are rendered markdown, not the contract stub's JSON.
RAW_OK = RAW_RESPONSE

UNIT_FILES = ("{u}.system.md", "{u}.user.md", "{u}.response.json", "{u}.meta.json")


class _LLM:
    """Local reviewer-level double: one canned result (or one raised error)."""

    def __init__(self, text: str = RAW_RESPONSE, error: Exception | None = None):
        self.text = text
        self.error = error

    def invoke(
        self, system: str, user: str, *, max_tokens: int = 4096,
        json_mode: bool = False, timeout_s: float = 60.0,
    ) -> InvokeResult:
        if self.error is not None:
            raise self.error
        return InvokeResult(
            text=self.text, input_tokens=100, output_tokens=50,
            model="fake-model", backend="fake", elapsed_ms=12,
        )


def _chunk():
    return parse_unified_diff(MINI_DIFF)


class TestReviewerWritesTraceFiles:
    def test_a_chunk_writes_its_four_files(self, tmp_path):
        findings, meta = review_chunk(
            _LLM(), _chunk(), trace_dir=str(tmp_path), trace_label="chunk0",
        )
        assert meta["error"] == ""
        for name in UNIT_FILES:
            assert (tmp_path / name.format(u="chunk0")).is_file()

    def test_the_sweep_writes_its_four_files(self, tmp_path):
        _, meta = review_systemic(
            _LLM(), "the whole-pr digest",
            trace_dir=str(tmp_path), trace_label="sweep",
        )
        assert meta["error"] == ""
        for name in UNIT_FILES:
            assert (tmp_path / name.format(u="sweep")).is_file()

    def test_system_and_user_files_hold_the_rendered_prompt(self, tmp_path):
        review_chunk(_LLM(), _chunk(), trace_dir=str(tmp_path), trace_label="chunk0")
        system = (tmp_path / "chunk0.system.md").read_text(encoding="utf-8")
        user = (tmp_path / "chunk0.user.md").read_text(encoding="utf-8")
        assert system.strip()
        assert "+import sys" in user
        assert "{diff}" not in user and "{pr_title}" not in user

    def test_response_json_round_trips_the_raw_text_verbatim(self, tmp_path):
        raw = 'not json at all, on purpose'
        review_chunk(
            _LLM(text=raw), _chunk(),
            trace_dir=str(tmp_path), trace_label="chunk0",
        )
        assert json.loads((tmp_path / "chunk0.response.json").read_text()) == raw

    def test_meta_json_carries_model_tokens_elapsed_and_error(self, tmp_path):
        review_chunk(_LLM(), _chunk(), trace_dir=str(tmp_path), trace_label="chunk0")
        meta = json.loads((tmp_path / "chunk0.meta.json").read_text())
        assert meta["unit"] == "chunk0"
        assert meta["model"] == "fake-model"
        assert meta["input_tokens"] == 100
        assert meta["output_tokens"] == 50
        assert meta["error"] == ""
        assert isinstance(meta["elapsed_ms"], int) and meta["elapsed_ms"] >= 0

    def test_a_failed_invoke_still_leaves_a_trace(self, tmp_path):
        findings, meta = review_chunk(
            _LLM(error=RuntimeError("endpoint down")), _chunk(),
            trace_dir=str(tmp_path), trace_label="chunk0",
        )
        assert findings == [] and "RuntimeError" in meta["error"]
        assert json.loads((tmp_path / "chunk0.response.json").read_text()) is None
        meta_json = json.loads((tmp_path / "chunk0.meta.json").read_text())
        assert "endpoint down" in meta_json["error"]
        assert meta_json["input_tokens"] == 0

    def test_an_unparseable_response_is_traced_as_the_raw_text(self, tmp_path):
        review_chunk(
            _LLM(text="I am not returning JSON."), _chunk(),
            trace_dir=str(tmp_path), trace_label="chunk0",
        )
        assert json.loads(
            (tmp_path / "chunk0.response.json").read_text()
        ) == "I am not returning JSON."
        assert json.loads((tmp_path / "chunk0.meta.json").read_text())["error"]


class TestNoCostWhenUnset:
    def test_an_unset_trace_dir_creates_nothing(self, tmp_path):
        findings, meta = review_chunk(_LLM(), _chunk())
        assert meta["error"] == ""
        assert findings == []
        assert list(tmp_path.iterdir()) == []

    def test_the_orchestrator_default_traces_nothing(self, tmp_path):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        orchestrate_review(forge, REF, FakeLLM(RAW_OK), post=False)
        assert list(tmp_path.iterdir()) == []


class TestOrchestratorLabelsEveryUnit:
    def test_chunks_and_the_sweep_each_get_a_set(self, tmp_path):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        res = orchestrate_review(
            forge, REF, FakeLLM(RAW_OK), post=False, trace_dir=str(tmp_path),
        )
        assert res["chunks_reviewed"] == 2  # one chunk + the sweep
        names = {p.name for p in tmp_path.iterdir()}
        assert names == {
            name.format(u=u)
            for u in ("chunk0", "sweep")
            for name in UNIT_FILES
        }

    def test_multi_chunk_runs_are_numbered_from_zero(self, tmp_path):
        diff = "".join(_added_file_diff(f"src/f{i}.py", 20) for i in range(3))
        forge = FakeForge(diff=diff)
        orchestrate_review(
            forge, REF, FakeLLM(), post=False,
            trace_dir=str(tmp_path), max_files_per_chunk=1,
        )
        units = {p.name.split(".")[0] for p in tmp_path.iterdir()}
        assert units == {"chunk0", "chunk1", "chunk2", "sweep"}

    def test_the_trace_directory_is_created_when_missing(self, tmp_path):
        target = tmp_path / "deep" / "nest"
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        orchestrate_review(
            forge, REF, FakeLLM(RAW_OK), post=False, trace_dir=str(target),
        )
        assert (target / "sweep.meta.json").is_file()


class TestWriteFailureNeverAborts:
    def test_an_unwritable_trace_dir_is_a_warning_not_a_failure(
        self, tmp_path, caplog
    ):
        # A FILE where the trace directory must be created: makedirs cannot
        # succeed there, which is the cheapest deterministic OSError.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        with caplog.at_level(logging.WARNING, logger="prxref"):
            findings, meta = review_chunk(
                _LLM(), _chunk(),
                trace_dir=str(blocker), trace_label="chunk0",
            )
        assert findings == [] and meta["error"] == ""
        assert any("trace write" in r.message for r in caplog.records)


class TestTraceDirConfig:
    def test_the_env_var_loads(self, monkeypatch):
        monkeypatch.setenv("PRXREF_TRACE_DIR", "/tmp/prxref-traces")
        assert config.load_config()["trace_dir"] == "/tmp/prxref-traces"

    def test_the_default_is_empty(self):
        assert config.load_config()["trace_dir"] == ""

    def test_an_empty_env_value_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_TRACE_DIR", "   ")
        assert config.load_config()["trace_dir"] == ""


def _install_fake_module(monkeypatch, fullname: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(fullname)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, fullname, mod)
    return mod


@pytest.fixture
def fake_runtime(monkeypatch):
    """Mock orchestrator/llm_backends modules; records ``orchestrate_review`` kwargs."""
    recorded: list[dict] = []

    def fake_orchestrate_review(**kwargs):
        recorded.append(kwargs)
        return {"verdict": "commented", "findings_active": [], "findings_dropped": []}

    _install_fake_module(
        monkeypatch, "prxref.llm_backends",
        create_llm_client=lambda cfg: MagicMock(name="LLMClient"),
    )
    _install_fake_module(
        monkeypatch, "prxref.orchestrator", orchestrate_review=fake_orchestrate_review,
    )
    return {"orchestrate_calls": recorded}


CLI_REF = PRRef(
    forge="github", host="github.com", owner="org", repo="repo", number=7,
    url="https://github.com/org/repo/pull/7",
)


class TestTraceDirCli:
    def _review(self, monkeypatch, fake_runtime, argv_extra=()):
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: CLI_REF)
        return main([
            "review", "--pr-url", CLI_REF.url, *argv_extra,
        ])

    def test_the_flag_reaches_the_orchestrator(self, fake_runtime, monkeypatch, tmp_path):
        assert self._review(
            monkeypatch, fake_runtime, ["--trace-dir", str(tmp_path)],
        ) == 0
        assert fake_runtime["orchestrate_calls"][0]["trace_dir"] == str(tmp_path)

    def test_the_env_var_reaches_the_orchestrator(self, fake_runtime, monkeypatch):
        monkeypatch.setenv("PRXREF_TRACE_DIR", "/from-env")
        assert self._review(monkeypatch, fake_runtime) == 0
        assert fake_runtime["orchestrate_calls"][0]["trace_dir"] == "/from-env"

    def test_the_flag_wins_over_the_environment(self, fake_runtime, monkeypatch, tmp_path):
        monkeypatch.setenv("PRXREF_TRACE_DIR", "/from-env")
        assert self._review(
            monkeypatch, fake_runtime, ["--trace-dir", "/from-flag"],
        ) == 0
        assert fake_runtime["orchestrate_calls"][0]["trace_dir"] == "/from-flag"

    def test_default_is_the_empty_string_when_neither_is_set(
        self, fake_runtime, monkeypatch
    ):
        assert self._review(monkeypatch, fake_runtime) == 0
        assert fake_runtime["orchestrate_calls"][0]["trace_dir"] == ""
