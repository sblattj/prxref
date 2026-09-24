"""Issue #10 T4: ``PRXREF_DEDUP_SIMILARITY`` reaches ``apply_sweep_dedup``.

The reworded tier itself is covered in tests/test_sweep_dedup_similarity.py.
This file covers only the wiring: the ``orchestrate_review`` keyword, its
call site, and ``_run_review``. It uses the real orchestrator with scripted
chunk and sweep findings (the ``contract_stubs`` pattern of
tests/test_prompt_inputs.py), and the real CLI entry point for the config
hop. P1 is the reworded same-line pair from issue #10; its Jaccard score is
0.57 with four shared title tokens.
"""
from __future__ import annotations

import inspect
import json
import types

import pytest

from prxref import config, orchestrator, quality
from prxref.cli import _build_json_result, main
from prxref.forges.base import PRRef
from prxref.triage import Finding
from tests.test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff

P1_CHUNK = "Replace manual null check with Optional in resolve()"
P1_SWEEP = "Use Optional instead of manual null check in adapter lookup"
REASON = "duplicate of chunk finding (reworded, similarity 0.57)"
PATH = "src/app.py"
LINE = 5

CLI_REF = PRRef(
    forge="github", host="github.com", owner="org", repo="repo",
    number=7, url="https://github.com/org/repo/pull/7",
)


def _finding(title: str, *, severity: str = "warning", confidence: float = 0.9) -> Finding:
    """A finding on ``PATH``:``LINE`` that survives every pass on ``_added_file_diff``."""
    return Finding(
        file=PATH, line=LINE, severity=severity, confidence=confidence,
        title=title, body=f"data {LINE} is wrong",
    )


def _meta() -> dict:
    return {
        "escalations": [], "input_tokens": 1, "output_tokens": 1,
        "model": "m", "elapsed_ms": 1, "error": "",
    }


def _install_doubles(monkeypatch, chunk, sweep) -> None:
    def _review_chunk(llm, files, **kwargs):
        return list(chunk), _meta()

    def _review_systemic(llm, digest, **kwargs):
        return list(sweep), _meta()

    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _review_chunk)
    monkeypatch.setattr(orchestrator.reviewer, "review_systemic", _review_systemic)


def _review(monkeypatch, *, chunk, sweep, trace_file=None, post=False, **kw):
    """One real orchestrator run; returns (result, forge)."""
    _install_doubles(monkeypatch, chunk, sweep)
    forge = FakeForge(diff=_added_file_diff(PATH, 20))
    res = orchestrator.orchestrate_review(
        forge, REF, FakeLLM(), post=post, trace_file=trace_file, **kw,
    )
    return res, forge


def _p1_pair(*, sweep_severity: str = "warning", sweep_confidence: float = 0.85):
    return (
        [_finding(P1_CHUNK)],
        [_finding(P1_SWEEP, severity=sweep_severity, confidence=sweep_confidence)],
    )


def _strip_timing(value):
    if isinstance(value, dict):
        return {
            k: _strip_timing(v) for k, v in value.items()
            if k not in {"t_ms", "elapsed_ms"}
        }
    if isinstance(value, list):
        return [_strip_timing(v) for v in value]
    return value


class TestSignature:
    def test_the_keyword_is_named_after_the_config_key_and_is_off_by_default(self):
        param = inspect.signature(orchestrator.orchestrate_review).parameters[
            "dedup_similarity"
        ]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is None
        assert "dedup_similarity" in config._DEFAULTS
        assert config._DEFAULTS["dedup_similarity"] is None


@pytest.mark.usefixtures("contract_stubs")
class TestOrchestratorForwardsTheThreshold:
    def test_unset_keeps_both_reworded_copies_on_their_line(self, monkeypatch):
        chunk, sweep = _p1_pair()
        res, _ = _review(monkeypatch, chunk=chunk, sweep=sweep)

        assert sorted((f.title, f.line) for f in res["findings_active"]) == sorted(
            [(P1_CHUNK, LINE), (P1_SWEEP, LINE)]
        )
        assert res["findings_dropped"] == []

    def test_a_threshold_drops_the_reworded_sweep_copy(self, monkeypatch):
        chunk, sweep = _p1_pair()
        res, _ = _review(monkeypatch, chunk=chunk, sweep=sweep, dedup_similarity=0.5)

        assert [f.title for f in res["findings_active"]] == [P1_CHUNK]
        assert [(f.title, f.drop_reason) for f in res["findings_dropped"]] == [
            (P1_SWEEP, REASON),
        ]

    def test_the_value_is_forwarded_not_just_switched_on(self, monkeypatch):
        """0.57 clears 0.5 and misses 0.6: the number itself reaches the pass."""
        chunk, sweep = _p1_pair()
        res, _ = _review(monkeypatch, chunk=chunk, sweep=sweep, dedup_similarity=0.6)

        assert len(res["findings_active"]) == 2
        assert res["findings_dropped"] == []

    def test_the_call_site_hands_the_keyword_to_apply_sweep_dedup(self, monkeypatch):
        seen: list[float | None] = []
        real = quality.apply_sweep_dedup

        def _spy(findings, sweep_start, *, similarity=None):
            seen.append(similarity)
            return real(findings, sweep_start, similarity=similarity)

        monkeypatch.setattr(orchestrator, "apply_sweep_dedup", _spy)
        chunk, sweep = _p1_pair()
        _review(monkeypatch, chunk=chunk, sweep=sweep, dedup_similarity=0.42)
        _review(monkeypatch, chunk=chunk, sweep=sweep)

        assert seen == [0.42, None]

    def test_a_more_severe_sweep_copy_is_kept_alongside_the_chunk_copy(self, monkeypatch):
        chunk, sweep = _p1_pair(sweep_severity="error")
        res, _ = _review(monkeypatch, chunk=chunk, sweep=sweep, dedup_similarity=0.5)

        assert sorted(f.title for f in res["findings_active"]) == sorted(
            [P1_CHUNK, P1_SWEEP]
        )
        assert res["findings_dropped"] == []
        assert res["verdict"] == "Request-Changes"

    def test_the_json_record_shows_the_reworded_drop_reason(self, monkeypatch):
        chunk, sweep = _p1_pair()
        res, _ = _review(monkeypatch, chunk=chunk, sweep=sweep, dedup_similarity=0.5)

        rows = _build_json_result(res)["findings"]
        assert [(r["title"], r["drop_reason"]) for r in rows] == [
            (P1_CHUNK, None),
            (P1_SWEEP, REASON),
        ]
        assert rows[1]["file"] == PATH
        assert rows[1]["line"] == LINE

    def test_only_the_kept_copy_posts_inline(self, monkeypatch):
        chunk, sweep = _p1_pair()
        _, forge = _review(
            monkeypatch, chunk=chunk, sweep=sweep, dedup_similarity=0.5, post=True,
        )

        (batch,) = forge.inline_batches
        assert len(batch) == 1
        assert P1_CHUNK in batch[0].body
        assert P1_SWEEP not in batch[0].body


@pytest.mark.usefixtures("contract_stubs")
class TestUnsetIsByteIdentical:
    """``dedup_similarity=None`` equals a run that never passes the keyword."""

    def _observe(self, monkeypatch, tmp_path, name, **kw):
        trace = tmp_path / f"{name}.jsonl"
        chunk, sweep = _p1_pair()
        res, forge = _review(
            monkeypatch, chunk=chunk, sweep=sweep, trace_file=str(trace), post=True, **kw,
        )
        events = [json.loads(x) for x in trace.read_text().splitlines() if x.strip()]
        return {
            "record": json.dumps(_strip_timing(_build_json_result(res)), sort_keys=True),
            "summaries": list(forge.summaries),
            "inline": [repr(batch) for batch in forge.inline_batches],
            "trace": json.dumps(_strip_timing(events), sort_keys=True),
        }

    def test_none_matches_omitting_the_keyword(self, monkeypatch, tmp_path):
        omitted = self._observe(monkeypatch, tmp_path, "omitted")
        explicit = self._observe(monkeypatch, tmp_path, "none", dedup_similarity=None)

        assert explicit == omitted
        assert omitted["summaries"]
        assert omitted["inline"]

    def test_control_a_threshold_changes_the_same_observation(self, monkeypatch, tmp_path):
        omitted = self._observe(monkeypatch, tmp_path, "omitted")
        on = self._observe(monkeypatch, tmp_path, "on", dedup_similarity=0.5)

        assert on["record"] != omitted["record"]
        assert on["inline"] != omitted["inline"]


class TestCliPassesTheConfiguredValue:
    @pytest.fixture
    def calls(self, monkeypatch):
        recorded: list[dict] = []

        def _orchestrate(**kwargs):
            recorded.append(kwargs)
            return {"verdict": "Approved", "findings_active": [], "findings_dropped": []}

        monkeypatch.setattr(orchestrator, "orchestrate_review", _orchestrate)
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: object())
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: CLI_REF)
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: FakeForge())
        return recorded

    def test_the_environment_value_reaches_orchestrate_review(self, calls, monkeypatch):
        monkeypatch.setenv("PRXREF_DEDUP_SIMILARITY", "0.5")
        assert main(["review", "--pr-url", CLI_REF.url, "--no-post"]) == 0
        assert calls[0]["dedup_similarity"] == 0.5

    def test_unset_passes_none(self, calls):
        assert main(["review", "--pr-url", CLI_REF.url, "--no-post"]) == 0
        assert "dedup_similarity" in calls[0]
        assert calls[0]["dedup_similarity"] is None


@pytest.mark.usefixtures("contract_stubs")
class TestThroughTheRealCliAndOrchestrator:
    """Only the forge, the model and the two reviewer units are doubles."""

    @pytest.fixture
    def rig(self, monkeypatch):
        chunk, sweep = _p1_pair()
        _install_doubles(monkeypatch, chunk, sweep)
        forge = FakeForge(diff=_added_file_diff(PATH, 20))
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: CLI_REF)
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: forge)
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: FakeLLM())
        return types.SimpleNamespace(forge=forge)

    def _drop_reasons(self, capsys) -> list:
        argv = ["review", "--pr-url", CLI_REF.url, "--no-post", "--format", "json"]
        assert main(argv) == 0
        out, _ = capsys.readouterr()
        return [row["drop_reason"] for row in json.loads(out)["findings"]]

    def test_the_env_var_drops_the_reworded_duplicate(self, rig, monkeypatch, capsys):
        monkeypatch.setenv("PRXREF_DEDUP_SIMILARITY", "0.5")
        assert self._drop_reasons(capsys) == [None, REASON]

    def test_without_the_env_var_both_copies_stay(self, rig, capsys):
        assert self._drop_reasons(capsys) == [None, None]
