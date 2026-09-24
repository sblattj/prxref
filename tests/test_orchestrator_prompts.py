"""Prompt-template overrides (#11 T3): ``orchestrate_review(prompts=...)``.

The orchestrator takes one loaded
:class:`prxref.prompt_templates.PromptTemplates` and wires it three ways:
``record()`` becomes the ``prompt_templates`` run-record key (always present,
``None`` when unset) and the meta of one ``prompts ok`` trace event;
``override("worker")`` / ``override("systemic")`` fill the run's one
``PromptContext``; ``override("summary")`` is the template of every summary
render. These tests run the REAL reviewer and the real loader end to end and
capture every prompt with a recording LLM.

None of these tests uses the ``contract_stubs`` fixture: it replaces
``reviewer.load_prompt`` with a summary-only stub, and every test here renders
the real packaged templates. The loader confines the directory to the working
directory, so each test runs from a fresh directory under ``tmp_path``.
"""
from __future__ import annotations

import inspect
import json
import threading
from pathlib import Path

import pytest

from prxref import orchestrator, viz
from prxref.llm import InvokeResult
from prxref.prompt_templates import (
    TEMPLATE_NAMES,
    PromptTemplates,
    export_prompt_templates,
    load_prompt_templates,
)
from prxref.reviewer import _CONTEXT_MARKER, load_prompt
from tests.test_orchestrator import REF, FakeForge, _added_file_diff, multi_chunk_diff

STYLE_LINE = "no style-guide nits that change neither behavior nor risk"
WORKER_LINE = "ACME chunk policy: style-guide findings that enforce the team standard are welcome"
SYSTEMIC_LINE = "ACME sweep policy: flag cross-file naming drift even when behaviour is unchanged"
SUMMARY_HEAD = "## prxref automated review: "
SUMMARY_LINE = "## ACME review digest: "

NO_FINDINGS = '{"findings": []}'
ONE_FINDING = json.dumps({"findings": [{
    "file": "src/app.py", "line": 3, "severity": "warning", "confidence": 0.9,
    "title": "Leak", "body": "data 3 leaks",
}]})


class _RecordingLLM:
    """Records every ``(system, user)`` prompt and answers with one scripted text."""

    def __init__(self, text: str = NO_FINDINGS, error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        with self._lock:
            self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        return InvokeResult(
            text=self.text, input_tokens=10, output_tokens=5,
            model="fake-model", backend="fake", elapsed_ms=1,
        )


@pytest.fixture
def work(tmp_path, monkeypatch) -> Path:
    """A fresh working directory, with the attribution's elapsed time pinned.

    The attribution line carries the run's wall clock, so two runs' summaries
    are comparable byte for byte only with ``_elapsed_ms`` held constant.
    """
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(orchestrator, "_elapsed_ms", lambda t0: 0)
    return root


def _packaged(name: str) -> str:
    return load_prompt(f"{name}.md")


def _edited(name: str, text: str) -> str:
    if name == "worker":
        assert STYLE_LINE in text
        return text.replace(STYLE_LINE, WORKER_LINE)
    if name == "systemic":
        assert STYLE_LINE in text
        return text.replace(STYLE_LINE, SYSTEMIC_LINE)
    assert text.count(SUMMARY_HEAD) == 1
    return text.replace(SUMMARY_HEAD, SUMMARY_LINE)


def _load(work: Path, *names: str, edit: bool = True) -> PromptTemplates:
    """Export the packaged templates, keep ``names`` (edited unless ``edit`` is false), load them."""
    d = work / "prompts"
    export_prompt_templates(d)
    for name in TEMPLATE_NAMES:
        path = d / f"{name}.md"
        if name not in names:
            path.unlink()
        elif edit:
            path.write_bytes(_edited(name, path.read_bytes().decode("utf-8")).encode("utf-8"))
    prompts = load_prompt_templates("prompts", source="--prompts-dir")
    assert prompts is not None
    assert prompts.overridden == tuple(n for n in TEMPLATE_NAMES if n in names)
    return prompts


def _review(diff: str, *, reply: str = NO_FINDINGS, error: Exception | None = None, **kwargs):
    forge = FakeForge(diff=diff)
    llm = _RecordingLLM(reply, error)
    res = orchestrator.orchestrate_review(forge, REF, llm, **kwargs)
    return res, forge, llm


def _split(llm: _RecordingLLM) -> tuple[list[tuple[str, str]], tuple[str, str]]:
    """The chunk prompts (sorted: the pool finishes in any order) and the sweep prompt, which runs last."""
    *chunks, sweep = llm.calls
    return sorted(chunks), sweep


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _head(name: str) -> str:
    return _packaged(name).partition(_CONTEXT_MARKER)[0].strip()


class TestTheParameter:
    def test_prompts_is_keyword_only_and_off_by_default(self):
        param = inspect.signature(orchestrator.orchestrate_review).parameters["prompts"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is None

    @pytest.mark.parametrize("func", [orchestrator._render_summary, orchestrator._summary_only_run])
    def test_summary_template_is_keyword_only_and_packaged_by_default(self, func):
        param = inspect.signature(func).parameters["summary_template"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default == ""


class TestUnsetRun:
    def test_the_record_key_is_present_and_null(self, work):
        res, _, _ = _review(multi_chunk_diff(2))
        assert "prompt_templates" in res
        assert res["prompt_templates"] is None

    def test_prompts_none_is_byte_identical_to_a_run_without_the_kwarg(self, work, tmp_path):
        base_trace, none_trace = tmp_path / "base.jsonl", tmp_path / "none.jsonl"
        base, base_forge, base_llm = _review(multi_chunk_diff(2), trace_file=str(base_trace))
        off, off_forge, off_llm = _review(
            multi_chunk_diff(2), trace_file=str(none_trace), prompts=None,
        )
        assert len(base_llm.calls) == len(off_llm.calls) == 3
        assert _split(off_llm) == _split(base_llm)
        assert off_forge.summaries == base_forge.summaries and len(base_forge.summaries) == 1
        assert off == base
        assert sorted((e["node"], e["phase"]) for e in _events(none_trace)) == sorted(
            (e["node"], e["phase"]) for e in _events(base_trace)
        )

    def test_an_unset_run_renders_the_packaged_templates(self, work):
        _, forge, llm = _review(multi_chunk_diff(2))
        chunks, sweep = _split(llm)
        assert [system for system, _ in chunks] == [_head("worker")] * 2
        assert sweep[0] == _head("systemic")
        assert forge.summaries[0].startswith(f"{SUMMARY_HEAD}Approved\n")

    def test_an_unset_run_emits_no_prompts_event(self, work, tmp_path):
        trace = tmp_path / "trace.jsonl"
        _review(multi_chunk_diff(2), trace_file=str(trace))
        events = _events(trace)
        assert events
        assert [e for e in events if e["node"] == "prompts"] == []

    def test_unedited_exported_overrides_render_byte_identically(self, work):
        prompts = _load(work, *TEMPLATE_NAMES, edit=False)
        base, base_forge, base_llm = _review(multi_chunk_diff(2))
        res, forge, llm = _review(multi_chunk_diff(2), prompts=prompts)
        assert _split(llm) == _split(base_llm)
        assert forge.summaries == base_forge.summaries
        assert res["prompt_templates"] == prompts.record()
        assert {k: v for k, v in res.items() if k != "prompt_templates"} == {
            k: v for k, v in base.items() if k != "prompt_templates"
        }


class TestTheRunRecord:
    def test_a_set_run_records_the_loaded_templates(self, work):
        prompts = _load(work, "worker", "summary")
        res, _, _ = _review(multi_chunk_diff(2), prompts=prompts)
        assert res["prompt_templates"] == prompts.record()
        assert set(res["prompt_templates"]["templates"]) == {"worker", "summary"}
        assert json.loads(json.dumps(res["prompt_templates"])) == prompts.record()

    @pytest.mark.parametrize("exit_path", ["get_pr", "empty_diff", "llm_total_failure", "normal"])
    def test_every_exit_carries_the_key(self, work, exit_path):
        prompts = _load(work, "summary")
        diff = "" if exit_path == "empty_diff" else multi_chunk_diff(2)
        error = RuntimeError("model offline") if exit_path == "llm_total_failure" else None
        for given, expected in ((None, None), (prompts, prompts.record())):
            forge = FakeForge(diff=diff)
            if exit_path == "get_pr":
                forge.fail.add("get_pr")
            res = orchestrator.orchestrate_review(
                forge, REF, _RecordingLLM(error=error), prompts=given,
            )
            assert res["prompt_templates"] == expected
            if exit_path in ("get_pr", "llm_total_failure"):
                assert res["verdict"] == "Error"


class TestReviewOverrides:
    def test_a_worker_override_reaches_every_chunk_and_only_the_chunks(self, work):
        prompts = _load(work, "worker")
        _, base_forge, base_llm = _review(multi_chunk_diff(2))
        _, forge, llm = _review(multi_chunk_diff(2), prompts=prompts)
        chunks, sweep = _split(llm)
        base_chunks, base_sweep = _split(base_llm)
        assert len(chunks) == 2
        for (system, user), (base_system, base_user) in zip(chunks, base_chunks, strict=True):
            assert system == base_system.replace(STYLE_LINE, WORKER_LINE)
            assert WORKER_LINE in system and STYLE_LINE not in system
            assert user == base_user
        assert sweep == base_sweep
        assert forge.summaries == base_forge.summaries

    def test_a_systemic_override_reaches_the_sweep_and_only_the_sweep(self, work):
        prompts = _load(work, "systemic")
        _, _, base_llm = _review(multi_chunk_diff(2))
        _, _, llm = _review(multi_chunk_diff(2), prompts=prompts)
        chunks, (system, user) = _split(llm)
        base_chunks, (base_system, base_user) = _split(base_llm)
        assert system == base_system.replace(STYLE_LINE, SYSTEMIC_LINE)
        assert SYSTEMIC_LINE in system and STYLE_LINE not in system
        assert user == base_user
        assert chunks == base_chunks

    def test_both_overrides_reach_their_own_units(self, work):
        prompts = _load(work, *TEMPLATE_NAMES)
        _, _, llm = _review(multi_chunk_diff(3), prompts=prompts)
        chunks, (sweep_system, _) = _split(llm)
        assert len(chunks) == 3
        assert all(WORKER_LINE in s and SYSTEMIC_LINE not in s for s, _ in chunks)
        assert SYSTEMIC_LINE in sweep_system and WORKER_LINE not in sweep_system


class TestSummaryOverride:
    def test_it_reaches_the_posted_summary_on_the_normal_path(self, work):
        prompts = _load(work, "summary")
        _, base_forge, base_llm = _review(multi_chunk_diff(2))
        _, forge, llm = _review(multi_chunk_diff(2), prompts=prompts)
        assert len(forge.summaries) == 1
        assert forge.summaries[0] == base_forge.summaries[0].replace(SUMMARY_HEAD, SUMMARY_LINE)
        assert forge.summaries[0].startswith(f"{SUMMARY_LINE}Approved\n")
        assert _split(llm) == _split(base_llm)

    def test_it_reaches_the_empty_diff_summary(self, work):
        prompts = _load(work, "summary")
        _, base_forge, base_llm = _review("")
        res, forge, llm = _review("", prompts=prompts)
        assert res["chunk_count"] == 0 and llm.calls == [] and base_llm.calls == []
        assert len(forge.summaries) == 1
        assert forge.summaries[0] == base_forge.summaries[0].replace(SUMMARY_HEAD, SUMMARY_LINE)
        assert forge.summaries[0].startswith(f"{SUMMARY_LINE}Approved\n")

    def test_it_reaches_the_inline_accounting_re_post(self, work):
        prompts = _load(work, "summary")
        diff = _added_file_diff("src/app.py", 20)
        base, base_forge, _ = _review(diff, reply=ONE_FINDING, max_inline_comments=0)
        res, forge, _ = _review(diff, reply=ONE_FINDING, max_inline_comments=0, prompts=prompts)
        assert len(base["findings_active"]) == len(res["findings_active"]) == 1
        assert len(base_forge.summaries) == len(forge.summaries) == 2
        for posted, before in zip(forge.summaries, base_forge.summaries, strict=True):
            assert posted == before.replace(SUMMARY_HEAD, SUMMARY_LINE)
            assert posted.startswith(SUMMARY_LINE)
        assert forge.summaries[1] != forge.summaries[0]

    def test_a_summary_override_never_reaches_the_error_notice(self, work):
        prompts = _load(work, "summary")
        forge = FakeForge(diff=multi_chunk_diff(2))
        forge.fail.add("get_pr")
        orchestrator.orchestrate_review(forge, REF, _RecordingLLM(), prompts=prompts)
        assert len(forge.summaries) == 1
        assert SUMMARY_LINE not in forge.summaries[0]
        assert "The review could not complete" in forge.summaries[0]

    def test_a_failing_packaged_read_is_not_consulted_for_an_override(self, work, monkeypatch):
        prompts = _load(work, "summary")

        def _boom(name):
            raise OSError(f"packaged {name} unreadable")

        monkeypatch.setattr(orchestrator.reviewer, "load_prompt", _boom)
        rendered = orchestrator._render_summary(
            FakeForge().pr, [], "Approved", [], "m", 0, 0, 0,
            summary_template=prompts.override("summary"),
        )
        assert rendered.startswith(f"{SUMMARY_LINE}Approved\n")
        fallback = orchestrator._render_summary(FakeForge().pr, [], "Approved", [], "m", 0, 0, 0)
        assert fallback.startswith("🤖 **prxref review — Approved**")


class TestTraceEvent:
    def test_a_set_run_emits_one_prompts_ok_event_with_the_record(self, work, tmp_path):
        prompts = _load(work, "worker", "systemic")
        trace = tmp_path / "trace.jsonl"
        _review(multi_chunk_diff(2), prompts=prompts, trace_file=str(trace))
        events = _events(trace)
        found = [e for e in events if e["node"] == "prompts"]
        assert len(found) == 1
        assert found[0]["phase"] == "ok"
        assert found[0]["meta"] == prompts.record()
        assert [(e["node"], e["phase"]) for e in events[:2]] == [("run", "start"), ("prompts", "ok")]

    def test_the_event_follows_the_rules_and_ticket_events(self, work, tmp_path):
        prompts = _load(work, "summary")
        trace = tmp_path / "trace.jsonl"

        class _Rules:
            severity_map = None

            def record(self):
                return {"path": "rules.md"}

            def prompt_block(self, unit):
                return ""

        class _Ticket:
            active = False

            def record(self):
                return {"source": "ticket.txt"}

            def note(self):
                return ""

        _review(
            multi_chunk_diff(2), prompts=prompts, trace_file=str(trace),
            rules=_Rules(), ticket=_Ticket(),
        )
        head = [(e["node"], e["phase"]) for e in _events(trace)[:4]]
        assert head == [("run", "start"), ("rules", "ok"), ("ticket", "ok"), ("prompts", "ok")]

    def test_the_viz_summary_buckets_the_event_as_a_closed_node(self, work, tmp_path):
        prompts = _load(work, "worker")
        trace = tmp_path / "trace.jsonl"
        _review(multi_chunk_diff(2), prompts=prompts, trace_file=str(trace))
        events = viz.load_events(trace)
        summary = viz.summarize(events)
        assert summary["nodes"]["prompts"]["ok"] == 1
        assert summary["nodes"]["prompts"]["starts"] == 0
        assert "prompts" not in summary["open_nodes"]
        assert "prompts" not in summary["failed"]
        assert summary["open_nodes"] == []
        assert "prompts" not in {n["id"] for n in viz.NODES}
        assert "\"prompts\"" in viz.render_html(events)
