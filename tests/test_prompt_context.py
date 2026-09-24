"""The prompt-context plumbing: ``fill_template``, ``PromptContext``, ``scope``.

One frozen :class:`prxref.reviewer.PromptContext` carries the run-wide injected
inputs (team rules, ticket scope, ticket context, spec digest) from
``orchestrate_review`` through every hop to both prompt renderers. These tests
pin the fixed assembly order, the byte-stability of an unset run, the
single-pass template fill, the ``scope`` gate, and the two reserved cost keys
in the reviewer meta.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from prxref import orchestrator
from prxref.chunk_context import sibling_summary_block
from prxref.llm import InvokeResult
from prxref.reviewer import (
    _CONTEXT_MARKER,
    _NO_SPECS_TEXT,
    NO_PROMPT_CONTEXT,
    PromptContext,
    _finding_from,
    _render_prompt,
    _render_systemic_prompt,
    fill_template,
    load_prompt,
    render_chunk,
    review_chunk,
    review_systemic,
)
from prxref.specs import SpecSource
from prxref.triage import SCOPE_UNKNOWN, Finding, build_chunks, parse_unified_diff
from tests.test_orchestrator import (
    REF,
    FakeForge,
    _added_file_diff,
    make_pr,
    multi_chunk_diff,
)

MINI_DIFF = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 import os
+import sys
 print(os.name)
"""

DIGEST = "## src/app.py\n@@ -1,2 +1,3 @@\n+import sys"

RULES_WORKER = "## Team review rules\n\nWorker framing: never log tokens."
RULES_SWEEP = "## Team review rules\n\nSweep framing: never log tokens."
TICKET_SCOPE = "## Ticket scope\n\nAdd a \"scope\" key to every finding."
TICKET_BLOCK = "### Ticket context\n\n```text\nShip the header flag.\n```"

FULL = PromptContext(
    rules_worker=RULES_WORKER,
    rules_sweep=RULES_SWEEP,
    ticket_scope=TICKET_SCOPE,
    ticket_context=TICKET_BLOCK,
    spec_digest="[spec:spec.md#L1] (MUST) tools MUST be named with the mcp prefix",
)


def _chunk():
    return parse_unified_diff(MINI_DIFF)


def _worker(ctx=NO_PROMPT_CONTEXT, *, description="d"):
    return _render_prompt(_chunk(), "t", description, "r", prompt_context=ctx)


def _sweep(ctx=NO_PROMPT_CONTEXT, *, description="d"):
    return _render_systemic_prompt(DIGEST, "t", description, "r", prompt_context=ctx)


def _head(name: str) -> str:
    return load_prompt(name).partition(_CONTEXT_MARKER)[0].strip()


class _LLM:
    """Records every call and answers with one scripted text."""

    def __init__(self, text: str = '{"findings": []}', error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls: list[dict] = []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.calls.append({"system": system, "user": user})
        if self.error is not None:
            raise self.error
        return InvokeResult(
            text=self.text, input_tokens=10, output_tokens=5,
            model="fake-model", backend="fake", elapsed_ms=1,
        )


def _scoped_response(scope: object) -> str:
    return json.dumps({"findings": [{
        "file": "src/app.py", "line": 2, "severity": "warning",
        "confidence": 0.8, "title": "t", "body": "b", "scope": scope,
    }]})


class TestFillTemplate:
    def test_named_placeholders_are_replaced(self):
        assert fill_template("a {x} b {y}", {"x": "1", "y": "2"}) == "a 1 b 2"

    def test_unknown_braces_and_json_stay_literal(self):
        template = 'keep {foo} and {"findings": []} but fill {x}'
        assert fill_template(template, {"x": "X"}) == (
            'keep {foo} and {"findings": []} but fill X'
        )

    def test_substituted_text_is_never_scanned_again(self):
        out = fill_template("{a}|{b}", {"a": "{b}", "b": "B"})
        assert out == "{b}|B"

    def test_empty_values_return_the_template_unchanged(self):
        assert fill_template("x {} {y}", {}) == "x {} {y}"

    def test_backslashes_in_a_value_are_inserted_verbatim(self):
        value = r"C:\new\1 \g<0>"
        assert fill_template("[{v}]", {"v": value}) == f"[{value}]"

    def test_key_metacharacters_are_escaped(self):
        assert fill_template("{a.b} {aXb}", {"a.b": "hit"}) == "hit {aXb}"

    def test_every_occurrence_of_a_key_is_filled(self):
        assert fill_template("{x}-{x}", {"x": "7"}) == "7-7"


class TestPromptContext:
    def test_defaults_are_empty_and_equal_to_the_shared_instance(self):
        ctx = PromptContext()
        assert ctx == NO_PROMPT_CONTEXT
        assert all(getattr(ctx, f.name) == "" for f in dataclasses.fields(ctx))

    def test_the_field_set_is_the_contract(self):
        assert [f.name for f in dataclasses.fields(PromptContext)] == [
            "rules_worker", "rules_sweep", "ticket_scope", "ticket_context",
            "spec_digest",
            "worker_template", "systemic_template",
        ]

    def test_it_is_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            NO_PROMPT_CONTEXT.spec_digest = "x"  # type: ignore[misc]

    def test_scope_is_active_only_with_the_scope_instructions(self):
        assert PromptContext(ticket_scope="s").scope_active is True
        assert PromptContext(ticket_context="ticket text").scope_active is False
        assert NO_PROMPT_CONTEXT.scope_active is False


class TestUnsetRunIsByteStable:
    """With nothing injected, both prompts render exactly as the 0.13 chain did."""

    @staticmethod
    def _old_user(name: str, values: list[tuple[str, str]]) -> str:
        template = load_prompt(name).replace("{ticket_context}", "", 1)
        template = template.replace("{scope_example}", "", 1)
        _, marker, tail = template.partition(_CONTEXT_MARKER)
        user = marker + tail
        for key, value in values:
            user = user.replace("{" + key + "}", value)
        return user.strip()

    def test_worker_system_is_the_template_head(self):
        system, _ = _worker()
        assert system == _head("worker.md")

    def test_sweep_system_is_the_template_head(self):
        system, _ = _sweep()
        assert system == _head("systemic.md")

    def test_worker_user_matches_the_chained_replace_output(self):
        chunk = _chunk()
        _, user = _render_prompt(chunk, "t", "d", "r", context_blocks="CTX")
        blocks = "\n\n".join(b for b in (sibling_summary_block(chunk, ()), "CTX") if b)
        assert user == self._old_user("worker.md", [
            ("pr_title", "t"), ("pr_description", "d"), ("repo_hint", "r"),
            ("context_blocks", blocks), ("spec_digest", _NO_SPECS_TEXT),
            ("diff", render_chunk(chunk)),
        ])

    def test_sweep_user_matches_the_chained_replace_output(self):
        _, user = _sweep()
        assert user == self._old_user("systemic.md", [
            ("pr_title", "t"), ("pr_description", "d"), ("repo_hint", "r"),
            ("spec_digest", _NO_SPECS_TEXT), ("digest", DIGEST),
        ])

    @pytest.mark.parametrize("render", [_worker, _sweep])
    def test_no_placeholder_survives_and_repo_meets_the_spec_block(self, render):
        _, user = render()
        assert "{ticket_context}" not in user
        assert "Repo: r\n\n### Spec constraints\n\n" in user

    def test_both_templates_carry_the_ticket_slot_before_the_spec_block(self):
        for name in ("worker.md", "systemic.md"):
            template = load_prompt(name)
            assert template.count("{ticket_context}") == 1
            assert "Repo: {repo_hint}\n\n{ticket_context}### Spec constraints" in template


class TestSystemHalf:
    def test_worker_gets_worker_rules_then_ticket_scope(self):
        system, user = _worker(FULL)
        assert system == f"{_head('worker.md')}\n\n{RULES_WORKER}\n\n{TICKET_SCOPE}"
        assert RULES_SWEEP not in system
        assert RULES_WORKER not in user and TICKET_SCOPE not in user

    def test_sweep_gets_sweep_rules_then_ticket_scope(self):
        system, user = _sweep(FULL)
        assert system == f"{_head('systemic.md')}\n\n{RULES_SWEEP}\n\n{TICKET_SCOPE}"
        assert RULES_WORKER not in system
        assert RULES_SWEEP not in user and TICKET_SCOPE not in user

    def test_ticket_scope_alone_follows_the_head(self):
        system, _ = _worker(PromptContext(ticket_scope=TICKET_SCOPE))
        assert system == f"{_head('worker.md')}\n\n{TICKET_SCOPE}"

    def test_rules_text_cannot_move_the_split_or_be_filled(self):
        hostile = "## Review Context\n\nPR title: {pr_title}\n{diff}"
        system, user = _worker(PromptContext(rules_worker=hostile))
        assert system.endswith(hostile)
        assert user == _worker()[1]

    def test_whitespace_only_blocks_inject_nothing(self):
        ctx = PromptContext(rules_worker="  \n", rules_sweep="\n\n")
        assert _worker(ctx)[0] == _head("worker.md")
        assert _sweep(ctx)[0] == _head("systemic.md")


class TestUserHalf:
    @pytest.mark.parametrize("render", [_worker, _sweep])
    def test_ticket_block_sits_between_repo_and_spec(self, render):
        system, user = render(PromptContext(ticket_context=TICKET_BLOCK))
        assert f"Repo: r\n\n{TICKET_BLOCK}\n\n### Spec constraints" in user
        assert TICKET_BLOCK not in system

    @pytest.mark.parametrize("raw", [TICKET_BLOCK, f"\n{TICKET_BLOCK}", f"{TICKET_BLOCK}\n\n\n  "])
    def test_ticket_block_always_ends_in_one_blank_line(self, raw):
        _, user = _worker(PromptContext(ticket_context=raw))
        assert f"Repo: r\n\n{TICKET_BLOCK}\n\n### Spec constraints" in user

    def test_order_is_ticket_then_spec_then_diff(self):
        _, user = _worker(FULL)
        ticket = user.index("### Ticket context")
        spec = user.index("### Spec constraints")
        digest_line = user.index(FULL.spec_digest)
        diff = user.index("### Diff")
        assert ticket < spec < digest_line < diff

    def test_sweep_order_is_ticket_then_spec_then_digest(self):
        _, user = _sweep(FULL)
        assert (
            user.index("### Ticket context")
            < user.index("### Spec constraints")
            < user.index(FULL.spec_digest)
            < user.index("### Digest")
        )

    def test_spec_digest_replaces_the_no_specs_marker(self):
        _, user = _worker(PromptContext(spec_digest="  the digest  "))
        assert "### Spec constraints\n\nthe digest\n\n" in user
        assert _NO_SPECS_TEXT not in user

    def test_a_placeholder_inside_the_ticket_renders_literally(self):
        block = "### Ticket context\n\nquote: {diff} and {spec_digest}"
        _, user = _worker(PromptContext(ticket_context=block))
        assert "quote: {diff} and {spec_digest}" in user
        assert user.count("+import sys") == 1

    def test_a_description_quoting_diff_no_longer_receives_the_diff(self):
        _, user = _worker(description="see {diff} here")
        assert "see {diff} here" in user
        assert user.count("+import sys") == 1

    def test_a_description_quoting_digest_no_longer_receives_the_digest(self):
        _, user = _sweep(description="see {digest} here")
        assert "see {digest} here" in user
        assert user.count("+import sys") == 1


class TestScopeParsing:
    @pytest.mark.parametrize(("raw", "expected"), [
        ("in", "in"), ("out", "out"), ("unknown", "unknown"),
        (" OUT ", "out"), ("In scope", "unknown"), ("yes", "unknown"),
        (True, "unknown"), (None, "unknown"),
    ])
    def test_scope_is_normalized_when_accepted(self, raw, expected):
        f = _finding_from({"file": "a.py", "scope": raw}, accept_scope=True)
        assert f is not None and f.scope == expected

    def test_scope_is_ignored_unless_accepted(self):
        assert _finding_from({"file": "a.py", "scope": "in"}).scope == SCOPE_UNKNOWN
        f = _finding_from({"file": "a.py", "scope": "out"}, accept_scope=False)
        assert f.scope == SCOPE_UNKNOWN

    def test_a_missing_scope_is_unknown(self):
        assert _finding_from({"file": "a.py"}, accept_scope=True).scope == SCOPE_UNKNOWN

    @pytest.mark.parametrize("call", ["chunk", "sweep"])
    def test_the_reviewer_keeps_scope_only_when_the_prompt_asked(self, call):
        def run(ctx):
            llm = _LLM(_scoped_response("out"))
            if call == "chunk":
                findings, _ = review_chunk(llm, _chunk(), prompt_context=ctx)
            else:
                findings, _ = review_systemic(llm, DIGEST, prompt_context=ctx)
            return [f.scope for f in findings]

        assert run(PromptContext(ticket_scope=TICKET_SCOPE)) == ["out"]
        assert run(PromptContext(ticket_context=TICKET_BLOCK)) == ["unknown"]
        assert run(NO_PROMPT_CONTEXT) == ["unknown"]


class TestFindingScopeField:
    def test_scope_keeps_its_positional_slot_and_defaults_to_unknown(self):
        assert dataclasses.fields(Finding)[7].name == "scope"
        assert Finding("a.py", 1, "error", 0.9, "t", "b").scope == "unknown"

    def test_positional_construction_with_drop_reason_still_works(self):
        f = Finding("a.py", 1, "error", 0.9, "t", "b", "dropped")
        assert f.drop_reason == "dropped" and f.scope == "unknown"

    def test_replace_carries_scope_through(self):
        f = Finding("a.py", 1, "error", 0.9, "t", "b", scope="out")
        assert dataclasses.replace(f, severity="warning").scope == "out"


class TestReviewerMetaCostKeys:
    def test_meta_reserves_both_cost_keys_on_success(self):
        _, meta = review_chunk(_LLM(), _chunk())
        assert meta["cost_usd"] is None
        assert meta["cost_source"] == ""

    def test_meta_reserves_both_cost_keys_on_failure(self):
        _, meta = review_systemic(_LLM(error=RuntimeError("down")), DIGEST)
        assert meta["error"]
        assert meta["cost_usd"] is None
        assert meta["cost_source"] == ""

    def test_meta_json_has_the_eight_base_keys_in_order(self, tmp_path):
        review_chunk(_LLM(), _chunk(), trace_dir=str(tmp_path), trace_label="chunk0")
        meta = json.loads((tmp_path / "chunk0.meta.json").read_text())
        assert list(meta) == [
            "unit", "model", "input_tokens", "output_tokens", "elapsed_ms",
            "error", "cost_usd", "cost_source",
        ]
        assert meta["cost_usd"] is None and meta["cost_source"] == ""

    def test_the_system_trace_shows_the_injected_rules(self, tmp_path):
        review_chunk(
            _LLM(), _chunk(), trace_dir=str(tmp_path), trace_label="chunk0",
            prompt_context=PromptContext(rules_worker=RULES_WORKER),
        )
        assert RULES_WORKER in (tmp_path / "chunk0.system.md").read_text()
        assert RULES_WORKER not in (tmp_path / "chunk0.user.md").read_text()


class TestCoerceFinding:
    ITEM = {
        "file": "a.py", "line": 1, "severity": "error", "confidence": 0.9,
        "title": "t", "body": "b", "scope": "out",
    }

    def test_dict_scope_is_read_only_when_accepted(self):
        assert orchestrator._coerce_finding(dict(self.ITEM)).scope == SCOPE_UNKNOWN
        kept = orchestrator._coerce_finding(dict(self.ITEM), accept_scope=True)
        assert kept.scope == "out"

    def test_dict_scope_is_normalized(self):
        item = dict(self.ITEM, scope="Out of scope")
        assert orchestrator._coerce_finding(item, accept_scope=True).scope == SCOPE_UNKNOWN

    def test_a_finding_passes_through_untouched(self):
        f = Finding("a.py", 1, "error", 0.9, "t", "b", scope="in")
        assert orchestrator._coerce_finding(f) is f


def _recording_chunk_double(calls: list, *, outcomes: list[str] | None = None, item=None):
    def _rc(llm, files, *, pr_title="", pr_description="", repo_hint="",
            max_tokens=None, context_lines=None, context_blocks="",
            sibling_files=(), trace_label="", trace_dir="", prompt_context=None):
        calls.append({"prompt_context": prompt_context, "context_lines": context_lines})
        error = outcomes.pop(0) if outcomes else ""
        findings = [dict(item, file=files[0].path)] if item else []
        return findings, {
            "escalations": [], "input_tokens": 1, "output_tokens": 1,
            "model": "m", "elapsed_ms": 1, "error": error,
        }

    return _rc


def _recording_sweep_double(calls: list, *, item=None):
    def _rs(llm, digest, *, pr_title="", pr_description="", repo_hint="",
            max_tokens=None, threads=(), trace_label="", trace_dir="",
            prompt_context=None):
        calls.append({"prompt_context": prompt_context})
        return ([dict(item)] if item else []), {
            "escalations": [], "input_tokens": 1, "output_tokens": 1,
            "model": "m", "elapsed_ms": 1, "error": "",
        }

    return _rs


@pytest.mark.usefixtures("contract_stubs")
class TestThreading:
    """The one PromptContext reaches every chunk, the timeout retry, and the sweep."""

    SCOPED_ITEM = {
        "file": "src/app.py", "line": 1, "severity": "warning", "confidence": 0.9,
        "title": "t", "body": "b", "scope": "out",
    }

    def test_every_chunk_and_the_sweep_get_the_same_context(self, monkeypatch):
        chunk_calls: list = []
        sweep_calls: list = []
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk", _recording_chunk_double(chunk_calls),
        )
        monkeypatch.setattr(
            orchestrator.reviewer, "review_systemic", _recording_sweep_double(sweep_calls),
        )
        monkeypatch.setattr(
            orchestrator.specs, "fetch_specs",
            lambda *a, **k: [SpecSource(
                origin="docs/spec.md", kind="file",
                text="## Rules\n\nTools MUST be named with an mcp prefix.\n", error="",
            )],
        )
        orchestrator.orchestrate_review(
            FakeForge(diff=multi_chunk_diff(3)), REF, _LLM(), post=False,
            spec_sources=["docs/spec.md"],
        )
        contexts = [c["prompt_context"] for c in chunk_calls + sweep_calls]
        assert len(chunk_calls) == 3 and len(sweep_calls) == 1
        assert all(isinstance(c, PromptContext) for c in contexts)
        assert len({id(c) for c in contexts}) == 1
        assert "mcp prefix" in contexts[0].spec_digest

    def test_an_unset_run_passes_the_empty_context(self, monkeypatch):
        chunk_calls: list = []
        sweep_calls: list = []
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk", _recording_chunk_double(chunk_calls),
        )
        monkeypatch.setattr(
            orchestrator.reviewer, "review_systemic", _recording_sweep_double(sweep_calls),
        )
        orchestrator.orchestrate_review(
            FakeForge(diff=_added_file_diff("src/app.py", 20)), REF, _LLM(), post=False,
        )
        contexts = [c["prompt_context"] for c in chunk_calls + sweep_calls]
        assert contexts and all(c == NO_PROMPT_CONTEXT for c in contexts)

    def test_run_workers_forwards_the_context_to_each_chunk(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _recording_chunk_double(calls))
        chunks = build_chunks(parse_unified_diff(multi_chunk_diff(2)))
        orchestrator._run_workers(_LLM(), chunks, make_pr(), prompt_context=FULL)
        assert [c["prompt_context"] for c in calls] == [FULL, FULL]

    def test_the_timeout_retry_keeps_the_context(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _recording_chunk_double(calls, outcomes=["LLMError: m1: timeout (ReadTimeout)", ""]),
        )
        chunk = parse_unified_diff(_added_file_diff("src/app.py", 20))
        res = orchestrator._run_worker(
            1, 1, _LLM(), chunk, make_pr(), None, 3, prompt_context=FULL,
        )
        assert res["error"] == ""
        assert [c["context_lines"] for c in calls] == [3, 0]
        assert [c["prompt_context"] for c in calls] == [FULL, FULL]

    @pytest.mark.parametrize(("ctx", "expected"), [
        (PromptContext(ticket_scope=TICKET_SCOPE), "out"),
        (PromptContext(ticket_context=TICKET_BLOCK), SCOPE_UNKNOWN),
        (NO_PROMPT_CONTEXT, SCOPE_UNKNOWN),
    ])
    def test_chunk_dict_findings_keep_scope_only_when_active(self, monkeypatch, ctx, expected):
        calls: list = []
        monkeypatch.setattr(
            orchestrator.reviewer, "review_chunk",
            _recording_chunk_double(calls, item=self.SCOPED_ITEM),
        )
        chunk = parse_unified_diff(_added_file_diff("src/app.py", 20))
        res = orchestrator._invoke_chunk(_LLM(), chunk, make_pr(), None, None, prompt_context=ctx)
        assert [f.scope for f in res["findings"]] == [expected]

    @pytest.mark.parametrize(("ctx", "expected"), [
        (PromptContext(ticket_scope=TICKET_SCOPE), "out"),
        (NO_PROMPT_CONTEXT, SCOPE_UNKNOWN),
    ])
    def test_sweep_dict_findings_keep_scope_only_when_active(self, monkeypatch, ctx, expected):
        calls: list = []
        monkeypatch.setattr(
            orchestrator.reviewer, "review_systemic",
            _recording_sweep_double(calls, item=self.SCOPED_ITEM),
        )
        files = parse_unified_diff(_added_file_diff("src/app.py", 20))
        res = orchestrator._run_sweep(_LLM(), files, make_pr(), prompt_context=ctx)
        assert calls[0]["prompt_context"] is ctx
        assert [f.scope for f in res["findings"]] == [expected]


class TestRealReviewerEndToEnd:
    """No stubs: the real renderers through ``orchestrate_review``."""

    def test_an_unset_run_sends_no_injected_text_to_any_unit(self):
        llm = _LLM()
        orchestrator.orchestrate_review(
            FakeForge(diff=multi_chunk_diff(2)), REF, llm, post=False,
        )
        assert len(llm.calls) == 3
        heads = {_head("worker.md"), _head("systemic.md")}
        for call in llm.calls:
            assert call["system"] in heads
            assert "{ticket_context}" not in call["user"]
            assert "### Ticket context" not in call["user"]
            assert "\n\n### Spec constraints\n\n" in call["user"]
