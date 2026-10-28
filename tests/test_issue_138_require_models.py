"""Issue #138: PRXREF_LLM_REQUIRE_MODELS fails a review a disallowed model answered.

``PRXREF_LLM_MODELS`` is a fallback chain, so when the first model errors a
later one answers and the review still finished ``Approved``; the only trace
was ``sampling.models``, which names the configured chain, not who answered.
With ``PRXREF_LLM_REQUIRE_MODELS`` set, a review unit (chunk, systemic sweep,
error-verification call) answered by a model outside the list makes the
verdict ``Incomplete`` and ``degraded.models`` names the unit and the model.

The acceptance case runs the real reviewer over the real openai-compat
client with a stubbed transport: the first chain entry answers HTTP 500, the
second answers under a provider id that differs from its chain entry, so the
guard has to match the chain entry the client carried
(``InvokeResult.requested_model``) rather than the reported id.
"""
from __future__ import annotations

import itertools
import json as json_module
from types import SimpleNamespace

import pytest

from prxref import cli, config, followup, orchestrator, verify
from prxref.config import load_config
from prxref.llm import InvokeResult
from prxref.llm_backends import OpenAICompatClient, _litellm_requested_model
from prxref.llm_cli_backends import _Attempt, _CLIClient
from prxref.orchestrator import (
    _degraded_record,
    _model_violations,
    orchestrate_review,
)
from prxref.triage import Finding
from tests.test_cli_inputs import _review as _cli_review
from tests.test_cli_inputs import runtime  # noqa: F401 -- pytest fixture
from tests.test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff

PRIMARY = "vendor/primary"
FALLBACK = "vendor/fallback"
#: What the fallback's provider echoes as ``model``: deliberately not its
#: chain entry, which is the trap the guard must not fall into.
FALLBACK_REPORTED = "fallback-2026-01-01"
PRIMARY_REPORTED = "primary-2026-01-01"

TWO_FILE_DIFF = _added_file_diff("src/one.py", 30) + _added_file_diff("other/two.py", 30)
#: Large enough that ``max_chunks=2`` splits it into two chunks (issue #72's shape).
TWO_CHUNK_DIFF = _added_file_diff("src/one.py", 400) + _added_file_diff("other/two.py", 400)


def _resp(status_code=200, *, model="", text=""):
    payload = (
        {"error": {"message": "upstream overloaded"}}
        if status_code >= 400
        else {
            "choices": [{"message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
            "model": model,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    )
    body = json_module.dumps(payload).encode()
    return SimpleNamespace(
        status_code=status_code, json=lambda: payload, text=body.decode(),
        headers={}, iter_content=lambda chunk_size=None: iter([body]), close=lambda: None,
    )


class _ChainSession:
    """Answers by the posted chain entry: ``down`` entries fail HTTP 500, the rest answer.

    Each answering entry replies ``{"findings": []}`` under its provider id,
    which is what the real reviewer parses for both a chunk and the sweep.
    """

    def __init__(self, reported: dict[str, str], down=()):
        self.reported = reported
        self.down = set(down)
        self.calls: list[str] = []

    def post(self, url, json=None, headers=None, timeout=None, stream=None):
        model = json["model"]
        self.calls.append(model)
        if model in self.down:
            return _resp(500)
        return _resp(model=self.reported[model], text=json_module.dumps({"findings": []}))


def _client(down=()):
    session = _ChainSession(
        {PRIMARY: PRIMARY_REPORTED, FALLBACK: FALLBACK_REPORTED}, down=down,
    )
    client = OpenAICompatClient(
        base_url="https://llm.test/v1", api_key="k", models=[PRIMARY, FALLBACK],
        session=session, default_timeout=30.0,
    )
    return client, session


def _run(llm, diff=TWO_FILE_DIFF, **kw):
    kw.setdefault("max_workers", 1)
    kw.setdefault("max_chunks", 2)
    kw.setdefault("post", False)
    return orchestrate_review(FakeForge(diff=diff), REF, llm, **kw)


class TestAcceptanceTwoModelChainFirstFails:
    """The issue's acceptance case, end to end through the real client and reviewer."""

    def test_guard_on_the_first_model_makes_the_run_incomplete(self):
        llm, session = _client(down={PRIMARY})
        res = _run(llm, llm_require_models=[PRIMARY])
        assert PRIMARY in session.calls and FALLBACK in session.calls
        assert res["chunks_failed"] == 0
        assert res["verdict"] == "Incomplete"
        degraded = res["degraded"]
        assert degraded["cause"] == "model"
        assert degraded["failed"] == ["model"]
        rows = degraded["models"]
        assert {row["unit"] for row in rows} == {"chunk", "sweep"}
        for row in rows:
            assert row["requested_model"] == FALLBACK
            assert row["model"] == FALLBACK_REPORTED
        chunk_rows = [row for row in rows if row["unit"] == "chunk"]
        assert sorted(f for row in chunk_rows for f in row["files"]) == [
            "other/two.py", "src/one.py",
        ]

    def test_without_the_guard_the_review_completes_as_today(self):
        llm, _ = _client(down={PRIMARY})
        res = _run(llm)
        assert res["verdict"] == "Approved"
        assert res["degraded"] is None

    def test_sampling_models_is_unchanged_by_the_guard(self):
        guarded, _ = _client(down={PRIMARY})
        plain, _ = _client(down={PRIMARY})
        with_guard = _run(guarded, llm_require_models=[PRIMARY])["sampling"]
        without = _run(plain)["sampling"]
        assert with_guard == without
        assert with_guard["models"] == [PRIMARY, FALLBACK]

    def test_the_first_model_answering_passes_although_it_reports_another_id(self):
        llm, _ = _client()
        res = _run(llm, llm_require_models=[PRIMARY])
        assert res["verdict"] == "Approved"
        assert res["degraded"] is None

    def test_listing_the_fallback_chain_entry_allows_it(self):
        llm, _ = _client(down={PRIMARY})
        res = _run(llm, llm_require_models=[PRIMARY, FALLBACK])
        assert res["verdict"] == "Approved"
        assert res["degraded"] is None

    def test_listing_the_reported_id_allows_it_too(self):
        llm, _ = _client(down={PRIMARY})
        res = _run(llm, llm_require_models=[FALLBACK_REPORTED])
        assert res["verdict"] == "Approved"

    def test_the_summary_names_the_units_and_the_model(self):
        llm, _ = _client(down={PRIMARY})
        forge = FakeForge(diff=TWO_FILE_DIFF)
        res = orchestrate_review(
            forge, REF, llm, post=True, max_workers=1, max_chunks=2,
            llm_require_models=[PRIMARY],
        )
        assert res["verdict"] == "Incomplete"
        body = forge.summaries[-1]
        assert "Not the required model" in body
        assert "PRXREF_LLM_REQUIRE_MODELS" in body
        assert f"{FALLBACK}, reported as {FALLBACK_REPORTED}" in body

    def test_no_banner_without_a_violation(self):
        llm, _ = _client()
        forge = FakeForge(diff=TWO_FILE_DIFF)
        orchestrate_review(
            forge, REF, llm, post=True, max_workers=1, max_chunks=2,
            llm_require_models=[PRIMARY],
        )
        assert "Not the required model" not in forge.summaries[-1]


class TestFailOnTreatsItAsIncomplete:
    def test_fail_on_error_exits_one(self):
        llm, _ = _client(down={PRIMARY})
        res = _run(llm, llm_require_models=[PRIMARY])
        code, note = cli._fail_on_exit(res, "error")
        assert code == 1 and "did not complete" in note

    def test_fail_on_never_exits_zero(self):
        llm, _ = _client(down={PRIMARY})
        res = _run(llm, llm_require_models=[PRIMARY])
        assert cli._fail_on_exit(res, "never")[0] == 0


ERROR_FINDING = {
    "file": "other/two.py", "line": 3, "severity": "error",
    "confidence": 0.9, "title": "Null deref", "body": "data",
}


def _stub_units(monkeypatch, *, findings=(), requested=FALLBACK, model=FALLBACK_REPORTED):
    """Chunk and sweep doubles whose meta names ``requested``/``model`` as the answerer."""

    def meta():
        out = {
            "escalations": [], "input_tokens": 5, "output_tokens": 5,
            "model": model, "elapsed_ms": 1, "error": "",
        }
        if requested:
            out["requested_model"] = requested
        return out

    def review_chunk(llm, files, **kwargs):
        paths = {f.path for f in files}
        return [dict(f) for f in findings if f["file"] in paths], meta()

    def review_systemic(llm, digest, **kwargs):
        return [], meta()

    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", review_chunk)
    monkeypatch.setattr(orchestrator.reviewer, "review_systemic", review_systemic)


@pytest.mark.usefixtures("contract_stubs")
class TestPrecedence:
    def test_an_error_finding_still_wins_over_the_violation(self, monkeypatch):
        _stub_units(monkeypatch, findings=[ERROR_FINDING])
        res = _run(FakeLLM("{}"), llm_require_models=[PRIMARY])
        assert res["verdict"] == "Request-Changes"
        assert res["degraded"]["cause"] == "model"
        assert res["degraded"]["models"]

    def test_a_failed_chunk_and_a_violation_both_land_in_degraded(self, monkeypatch):
        counter = itertools.count(1)

        def review_chunk(llm, files, **kwargs):
            if next(counter) == 1:
                return [], {
                    "escalations": [], "input_tokens": 0, "output_tokens": 0,
                    "model": "", "elapsed_ms": 0,
                    "error": "LLMError: all models failed: x: HTTP 500",
                }
            return [], {
                "escalations": [], "input_tokens": 5, "output_tokens": 5,
                "model": FALLBACK_REPORTED, "requested_model": FALLBACK,
                "elapsed_ms": 1, "error": "",
            }

        _stub_units(monkeypatch)
        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", review_chunk)
        res = _run(FakeLLM("{}"), diff=TWO_CHUNK_DIFF, llm_require_models=[PRIMARY])
        assert res["chunks_failed"] == 1 and res["chunk_count"] == 3
        assert res["verdict"] == "Incomplete"
        degraded = res["degraded"]
        assert degraded["failed"] == ["chunk", "model"]
        assert degraded["cause"] == "model"
        assert len(degraded["chunks"]) == 1
        # The failed chunk is not double-reported as a violation.
        assert [(row["unit"], row["index"]) for row in degraded["models"]] == [
            ("chunk", 2), ("sweep", 3),
        ]

    def test_the_all_failed_error_exit_is_untouched(self, monkeypatch):
        def review_chunk(llm, files, **kwargs):
            return [], {
                "escalations": [], "input_tokens": 0, "output_tokens": 0,
                "model": "", "elapsed_ms": 0, "error": "LLMError: all models failed",
            }

        _stub_units(monkeypatch)
        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", review_chunk)
        res = _run(FakeLLM("{}"), llm_require_models=[PRIMARY])
        assert res["verdict"] == "Error"

    def test_an_unidentified_answerer_fails_closed(self, monkeypatch):
        _stub_units(monkeypatch, requested="", model="")
        res = _run(FakeLLM("{}"), llm_require_models=[PRIMARY])
        assert res["verdict"] == "Incomplete"
        assert {row["model"] for row in res["degraded"]["models"]} == {""}

    def test_an_empty_list_checks_nothing(self, monkeypatch):
        _stub_units(monkeypatch, requested="", model="")
        res = _run(FakeLLM("{}"), llm_require_models=[])
        assert res["verdict"] == "Approved"
        assert res["degraded"] is None


@pytest.mark.usefixtures("contract_stubs")
class TestIncrementalMarker:
    """A violating run must not record the head as reviewed, so the next push re-reviews it."""

    HEAD = "a" * 40

    def _forge(self):
        import dataclasses

        from tests.test_orchestrator import make_pr

        class Forge(FakeForge):
            def get_summary(self, ref):
                return None

        return Forge(pr=dataclasses.replace(make_pr(), source_sha=self.HEAD), diff=TWO_FILE_DIFF)

    def test_a_violation_writes_no_marker(self, monkeypatch):
        _stub_units(monkeypatch)
        res = orchestrate_review(
            self._forge(), REF, FakeLLM("{}"), post=True, max_workers=1, max_chunks=2,
            incremental="on", llm_require_models=[PRIMARY],
        )
        assert res["verdict"] == "Incomplete"
        assert res["incremental"]["marker_sha"] is None

    def test_an_allowed_run_writes_the_marker(self, monkeypatch):
        _stub_units(monkeypatch)
        res = orchestrate_review(
            self._forge(), REF, FakeLLM("{}"), post=True, max_workers=1, max_chunks=2,
            incremental="on", llm_require_models=[PRIMARY, FALLBACK],
        )
        assert res["verdict"] == "Approved"
        assert res["incremental"]["marker_sha"] == self.HEAD


class TestBackendsCarryTheChainEntry:
    def test_openai_compat_sets_the_entry_that_answered(self):
        llm, _ = _client(down={PRIMARY})
        res = llm.invoke("s", "u")
        assert (res.requested_model, res.model) == (FALLBACK, FALLBACK_REPORTED)

    def test_openai_compat_primary(self):
        llm, _ = _client()
        res = llm.invoke("s", "u")
        assert (res.requested_model, res.model) == (PRIMARY, PRIMARY_REPORTED)

    def test_cli_backend_sets_the_entry_that_answered(self):
        class Fake(_CLIClient):
            backend_name = "fake-cli"

            def __init__(self):
                super().__init__(binary="fake", models=["sonnet", "opus"], default_timeout=5.0)

            def _attempt(self, model, sys_text, user, deadline):
                if model == "sonnet":
                    return _Attempt(None, "sonnet: rate limited")
                return _Attempt(InvokeResult(text="ok", model="claude-opus-x", finish_reason="end_turn"))

        res = Fake().invoke("s", "u")
        assert (res.requested_model, res.model) == ("opus", "claude-opus-x")

    @pytest.mark.parametrize(
        ("reported", "available", "expected"),
        [
            ("claude-x-2025", ["anthropic/claude-x"], "anthropic/claude-x"),
            ("b", ["a", "b"], "b"),
            ("b-2025", ["a", "b"], ""),
        ],
    )
    def test_litellm_names_the_entry_only_when_it_can(self, reported, available, expected):
        assert _litellm_requested_model(reported, available) == expected


class TestUnitPlumbing:
    def test_reviewer_meta_carries_the_entry_only_when_reported(self):
        from prxref.reviewer import _stamp_requested_model

        meta: dict = {}
        _stamp_requested_model(meta, InvokeResult(text="", requested_model="m"))
        assert meta == {"requested_model": "m"}
        _stamp_requested_model(meta, InvokeResult(text=""))
        assert meta == {}

    def test_followup_keeps_both_answerers(self):
        result = {
            "model": PRIMARY_REPORTED, "requested_model": PRIMARY,
            "input_tokens": 1, "output_tokens": 1, "elapsed_ms": 1,
            "cost_usd": None, "cost_source": "",
        }
        rerun = {
            "model": FALLBACK_REPORTED, "requested_model": FALLBACK,
            "input_tokens": 1, "output_tokens": 1, "elapsed_ms": 1,
        }
        followup._fold_usage(result, rerun, {})
        assert result["answered_by"] == [
            [PRIMARY, PRIMARY_REPORTED], [FALLBACK, FALLBACK_REPORTED],
        ]
        rows = _model_violations([[]], [{**result, "error": ""}], [], [PRIMARY])
        assert [(r["requested_model"], r["model"]) for r in rows] == [
            (FALLBACK, FALLBACK_REPORTED),
        ]

    def test_followup_without_entries_adds_no_key(self):
        result = {"model": "a", "input_tokens": 0, "output_tokens": 0, "elapsed_ms": 0}
        followup._fold_usage(result, {"model": "b"}, {})
        assert "answered_by" not in result and "requested_model" not in result

    def test_a_verification_unit_names_its_finding_and_answerer(self):
        finding = Finding(
            file="src/a.py", line=2, severity="error", confidence=0.9,
            title="Undefined name", body="`foo` is undefined",
        )

        class LLM:
            def invoke(self, system, user, **kw):
                return InvokeResult(
                    text=json_module.dumps({"verdict": "confirmed", "evidence": ""}),
                    model=FALLBACK_REPORTED, requested_model=FALLBACK,
                )

        outcome = verify.verify_one(LLM(), finding, read=lambda p: "x = 1\nfoo()\n")
        unit = outcome.unit
        assert (unit["file"], unit["line"], unit["requested_model"]) == ("src/a.py", 2, FALLBACK)
        rows = _model_violations([], [], [unit], [PRIMARY])
        assert rows == [{
            "unit": "verify", "index": 1, "files": ["src/a.py"], "line": 2,
            "model": FALLBACK_REPORTED, "requested_model": FALLBACK,
        }]

    def test_degraded_shape_without_violations_is_unchanged(self):
        assert _degraded_record([("summary", "error")]) == {
            "cause": "error", "failed": ["summary"], "fallback": [], "annotations": 0,
        }
        assert _degraded_record([]) is None

    def test_a_blocked_write_outranks_the_violation(self):
        row = {"unit": "sweep", "index": 2, "files": [], "model": "x", "requested_model": "x"}
        rec = _degraded_record([("summary", "permission")], (), [row])
        assert rec["cause"] == "permission"
        assert rec["failed"] == ["model", "summary"]
        assert rec["models"] == [row]


class TestConfig:
    def test_env_list(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_REQUIRE_MODELS", f"{PRIMARY}, {FALLBACK}")
        assert load_config()["llm_require_models"] == [PRIMARY, FALLBACK]

    def test_default_is_empty(self, monkeypatch):
        monkeypatch.delenv("PRXREF_LLM_REQUIRE_MODELS", raising=False)
        assert load_config()["llm_require_models"] == []

    def test_config_file_array(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PRXREF_LLM_REQUIRE_MODELS", raising=False)
        path = tmp_path / ".prxref.toml"
        path.write_text(f'llm_require_models = ["{PRIMARY}"]\n', encoding="utf-8")
        assert load_config(config_file=path)["llm_require_models"] == [PRIMARY]
        assert "llm_require_models" in config.FILE_KEYS

    def test_the_cli_passes_it_to_the_orchestrator(self, runtime, monkeypatch):  # noqa: F811
        monkeypatch.setenv("PRXREF_LLM_REQUIRE_MODELS", f"{PRIMARY} {FALLBACK}")
        assert _cli_review() == 0
        assert runtime.orchestrate[0]["llm_require_models"] == [PRIMARY, FALLBACK]

    def test_the_cli_default_passes_an_empty_list(self, runtime, monkeypatch):  # noqa: F811
        monkeypatch.delenv("PRXREF_LLM_REQUIRE_MODELS", raising=False)
        assert _cli_review() == 0
        assert runtime.orchestrate[0]["llm_require_models"] == []
