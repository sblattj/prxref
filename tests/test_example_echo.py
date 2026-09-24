"""The example-echo quality pass and the worker prompt's size promise (0.15, seat Q-ECHO).

Live gpt-4o-mini sweeps returned the title of systemic.md's own output
example, "Paid API handler has no auth check", pinned to an unrelated line,
and every other pass kept it. ``quality.apply_example_echo_check`` drops a
finding whose normalized title equals the title of an example finding in the
worker or sweep template in force for the run, packaged or overridden through
``--prompts-dir``; ``quality.prompt_example_titles`` harvests those titles.
What is pinned:

- the harvest reads fenced ``json`` (or untagged) blocks only, and the
  packaged pair yields exactly the two packaged example titles;
- an exact echo is dropped with ``echoes the prompt's example: "<title>"``,
  from a chunk and from the sweep, and a case, whitespace or punctuation
  variant is dropped too, while a near miss is kept;
- an override's example title replaces the packaged one;
- the pass runs first among the dropping passes: an echo never becomes a
  grouping representative, never takes an error-cap slot, and carries the
  echo reason even when its location is also invalid;
- worker.md no longer promises an input size.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace

import pytest

from prxref import orchestrator
from prxref.orchestrator import orchestrate_review
from prxref.prompt_templates import (
    CONTEXT_MARKER,
    load_prompt_templates,
    packaged_text,
    required_placeholders,
)
from prxref.quality import (
    EXAMPLE_ECHO_PREFIX,
    GROUPED_INTO_PREFIX,
    apply_example_echo_check,
    normalize_title,
    prompt_example_titles,
)
from prxref.reviewer import load_prompt
from prxref.triage import Finding
from tests.test_orchestrator import REF, FakeForge, _added_file_diff

WORKER_EXAMPLE = "Divide by zero when size is unset"
SWEEP_EXAMPLE = "Paid API handler has no auth check"
PACKAGED = (WORKER_EXAMPLE, SWEEP_EXAMPLE)
APP = "src/app.py"
DIFF = _added_file_diff(APP, 40)


def _reason(title: str) -> str:
    return f'{EXAMPLE_ECHO_PREFIX}"{title}"'


def _f(line=3, *, title="Data check", severity="warning", confidence=0.9, file=APP, rule=None, body=None):
    return Finding(
        file=file, line=line, severity=severity, confidence=confidence, title=title,
        body=body if body is not None else f"The data on line {line} is unchecked.", rule=rule,
    )


def _meta(model="double-model"):
    return {"input_tokens": 10, "output_tokens": 5, "model": model, "elapsed_ms": 1, "error": ""}


def _install_doubles(monkeypatch, chunk=(), sweep=()):
    def _review_chunk(llm, files, **kwargs):
        return [replace(f) for f in chunk], _meta()

    def _review_systemic(llm, digest, **kwargs):
        return [replace(f) for f in sweep], _meta("sweep-model")

    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _review_chunk)
    monkeypatch.setattr(orchestrator.reviewer, "review_systemic", _review_systemic)


def _run(monkeypatch, chunk=(), sweep=(), **kw):
    _install_doubles(monkeypatch, chunk=chunk, sweep=sweep)
    return orchestrate_review(FakeForge(diff=DIFF), REF, object(), post=False, max_workers=1, **kw)


def _all(res):
    return [*res["findings_active"], *res["findings_dropped"]]


def _by_title(res, title):
    (found,) = [f for f in _all(res) if f.title == title]
    return found


def _events(path, node=None, phase=None):
    with open(path, encoding="utf-8") as fh:
        events = [json.loads(line) for line in fh]
    return [
        e for e in events
        if (node is None or e["node"] == node) and (phase is None or e["phase"] == phase)
    ]


@pytest.fixture
def work(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


class TestHarvest:
    def test_the_packaged_templates_yield_exactly_their_two_example_titles(self):
        assert prompt_example_titles(load_prompt("worker.md"), load_prompt("systemic.md")) == PACKAGED
        assert prompt_example_titles(packaged_text("worker"), packaged_text("systemic")) == PACKAGED

    @pytest.mark.parametrize("name", ["worker.md", "systemic.md"])
    def test_a_packaged_example_block_is_not_json_before_rendering(self, name):
        block = load_prompt(name).split("```json\n", 1)[1].split("\n```", 1)[0]
        assert "{scope_example}{rule_example}" in block
        with pytest.raises(ValueError):
            json.loads(block)

    def test_only_json_and_untagged_fences_are_read(self):
        text = (
            'Prose "title": "Prose title" stays unread.\n'
            "```diff\n"
            '"title": "Diff title"\n'
            "```\n"
            "```json\n"
            '{"title": "Json title"}\n'
            "```\n"
            "```\n"
            '{"title": "Untagged title"}\n'
            "```\n"
            "```text\n"
            '{"title": "Text title"}\n'
            "```\n"
        )
        assert prompt_example_titles(text) == ("Json title", "Untagged title")

    def test_a_diff_fence_does_not_swallow_the_json_block_after_it(self):
        text = "```diff\n{diff}\n```\n\nstuff\n\n```json\n{\"title\": \"After the diff\"}\n```\n"
        assert prompt_example_titles(text) == ("After the diff",)

    def test_escapes_are_decoded_and_empties_and_repeats_skipped(self):
        text = (
            "```JSON\n"
            '{"title": "Quote \\"here\\" and \\u0041"},\n'
            '{"title": ""}, {"title": "   "},\n'
            '{"title": "Quote \\"here\\" and \\u0041"}\n'
            "```\n"
        )
        assert prompt_example_titles(text) == ('Quote "here" and A',)

    def test_a_template_with_no_example_yields_nothing(self):
        assert prompt_example_titles("no fences at all", "") == ()


class TestPass:
    def test_an_exact_echo_is_dropped_naming_the_example(self):
        out = apply_example_echo_check([_f(title=SWEEP_EXAMPLE)], PACKAGED)
        assert out[0].drop_reason == 'echoes the prompt\'s example: "Paid API handler has no auth check"'

    @pytest.mark.parametrize("variant", [
        "paid api handler has no auth check",
        "PAID API HANDLER HAS NO AUTH CHECK",
        "  Paid   API handler\thas no auth check  ",
        "Paid API handler has no auth check.",
        "`Paid` API handler has no **auth** check!",
        "\"Paid API handler has no auth check\"",
    ])
    def test_a_case_whitespace_or_punctuation_variant_is_dropped(self, variant):
        assert normalize_title(variant) == normalize_title(SWEEP_EXAMPLE)
        out = apply_example_echo_check([_f(title=variant)], PACKAGED)
        assert out[0].drop_reason == _reason(SWEEP_EXAMPLE)

    @pytest.mark.parametrize("near_miss", [
        "Paid API handler has no auth check on refunds",
        "Paid API handler lacks an auth check",
        "Divide by zero when size is zero",
        "Refund API handler has no auth check",
    ])
    def test_a_near_miss_is_kept(self, near_miss):
        findings = [_f(title=near_miss)]
        assert apply_example_echo_check(findings, PACKAGED) == findings

    def test_it_is_one_to_one_and_order_preserving(self):
        findings = [_f(1, title="Real one"), _f(2, title=WORKER_EXAMPLE), _f(3, title="Real two")]
        out = apply_example_echo_check(findings, PACKAGED)
        assert [f.line for f in out] == [1, 2, 3]
        assert out[0] is findings[0] and out[2] is findings[2]
        assert out[1] == replace(findings[1], drop_reason=_reason(WORKER_EXAMPLE))

    def test_an_already_dropped_finding_keeps_its_reason(self):
        dropped = replace(_f(title=WORKER_EXAMPLE), drop_reason="malformed location: 'x'")
        assert apply_example_echo_check([dropped], PACKAGED) == [dropped]

    @pytest.mark.parametrize("titles", [(), ("",), ("  ", "..."), (None,)])
    def test_no_usable_example_drops_nothing(self, titles):
        findings = [_f(title=""), _f(title="..."), _f(title=WORKER_EXAMPLE)]
        assert apply_example_echo_check(findings, titles) == findings

    def test_a_non_string_title_passes_through(self):
        odd = replace(_f(), title=None)
        assert apply_example_echo_check([odd], PACKAGED) == [odd]


class TestOrchestrator:
    def test_a_chunk_echo_and_a_sweep_echo_are_both_dropped(self, monkeypatch, tmp_path, caplog):
        trace = tmp_path / "trace.jsonl"
        caplog.set_level(logging.INFO, logger="prxref")
        res = _run(
            monkeypatch,
            chunk=[_f(3, title=WORKER_EXAMPLE), _f(5, title="Unchecked data length")],
            sweep=[_f(0, title=SWEEP_EXAMPLE.upper() + ".", confidence=0.7)],
            trace_file=str(trace),
        )
        assert _by_title(res, WORKER_EXAMPLE).drop_reason == _reason(WORKER_EXAMPLE)
        assert _by_title(res, SWEEP_EXAMPLE.upper() + ".").drop_reason == _reason(SWEEP_EXAMPLE)
        assert [f.title for f in res["findings_active"]] == ["Unchecked data length"]
        assert [e["meta"] for e in _events(trace, "prompts", "echo")] == [{"findings": 2}]
        assert any("example echo: dropped 2 finding(s)" in r.getMessage() for r in caplog.records)

    def test_either_template_example_is_an_echo_from_either_unit(self, monkeypatch):
        res = _run(monkeypatch, chunk=[_f(3, title=SWEEP_EXAMPLE)], sweep=[_f(0, title=WORKER_EXAMPLE)])
        assert res["findings_active"] == []
        assert sorted(f.drop_reason for f in res["findings_dropped"]) == sorted(_reason(t) for t in PACKAGED)

    def test_a_run_without_an_echo_emits_no_event_and_no_line(self, monkeypatch, tmp_path, caplog):
        trace = tmp_path / "trace.jsonl"
        caplog.set_level(logging.INFO, logger="prxref")
        res = _run(monkeypatch, chunk=[_f(3, title="Divide by zero when size is zero")], trace_file=str(trace))
        assert [f.title for f in res["findings_active"]] == ["Divide by zero when size is zero"]
        assert _events(trace, "prompts", "echo") == []
        assert not any("example echo" in r.getMessage() for r in caplog.records)

    def test_an_unreadable_packaged_template_costs_only_its_own_titles(self, monkeypatch, caplog):
        real = orchestrator.packaged_text

        def _half_broken(name):
            if name == "systemic":
                raise OSError("gone")
            return real(name)

        monkeypatch.setattr(orchestrator, "packaged_text", _half_broken)
        caplog.set_level(logging.WARNING, logger="prxref")
        res = _run(monkeypatch, chunk=[_f(3, title=WORKER_EXAMPLE), _f(5, title=SWEEP_EXAMPLE)])
        assert _by_title(res, WORKER_EXAMPLE).drop_reason == _reason(WORKER_EXAMPLE)
        assert _by_title(res, SWEEP_EXAMPLE).drop_reason is None
        assert any("cannot read packaged systemic.md" in r.getMessage() for r in caplog.records)


class TestOverriddenTemplates:
    OVERRIDE_TITLE = "Retry counter never resets after success"

    def _prompts_dir(self, work):
        prompts = work / "team-prompts"
        prompts.mkdir()
        text = load_prompt("worker.md").replace(WORKER_EXAMPLE, self.OVERRIDE_TITLE)
        assert text != load_prompt("worker.md")
        (prompts / "worker.md").write_text(text, encoding="utf-8")
        return load_prompt_templates("team-prompts", source="--prompts-dir")

    def test_the_override_title_is_harvested_in_place_of_the_packaged_one(self, work):
        loaded = self._prompts_dir(work)
        assert loaded.overridden == ("worker",)
        assert prompt_example_titles(loaded.worker, loaded.systemic) == (self.OVERRIDE_TITLE, SWEEP_EXAMPLE)

    def test_the_override_title_drops_and_the_packaged_worker_title_no_longer_does(self, work, monkeypatch):
        loaded = self._prompts_dir(work)
        res = _run(
            monkeypatch,
            chunk=[
                _f(3, title=self.OVERRIDE_TITLE.lower()),
                _f(5, title=WORKER_EXAMPLE),
                _f(7, title=SWEEP_EXAMPLE),
            ],
            prompts=loaded,
        )
        assert _by_title(res, self.OVERRIDE_TITLE.lower()).drop_reason == _reason(self.OVERRIDE_TITLE)
        assert _by_title(res, WORKER_EXAMPLE).drop_reason is None
        assert _by_title(res, SWEEP_EXAMPLE).drop_reason == _reason(SWEEP_EXAMPLE)

    def test_without_the_override_the_packaged_worker_title_drops(self, work, monkeypatch):
        res = _run(monkeypatch, chunk=[_f(3, title=self.OVERRIDE_TITLE), _f(5, title=WORKER_EXAMPLE)])
        assert _by_title(res, self.OVERRIDE_TITLE).drop_reason is None
        assert _by_title(res, WORKER_EXAMPLE).drop_reason == _reason(WORKER_EXAMPLE)


class TestPassOrder:
    def test_an_echo_never_becomes_a_group_representative(self, monkeypatch):
        chunk = [
            _f(3, title=WORKER_EXAMPLE, rule="no-unchecked-data", severity="error"),
            _f(9, title="Unchecked data read", rule="no-unchecked-data"),
            _f(15, title="Unchecked data write", rule="no-unchecked-data"),
        ]
        res = _run(monkeypatch, chunk=chunk, group_findings=True)
        (rep,) = res["findings_active"]
        assert (rep.line, rep.title, rep.severity) == (9, "Unchecked data read", "warning")
        assert rep.body.endswith("Also at: `src/app.py:15`")
        assert "src/app.py:3" not in rep.body
        assert rep.locations == ((APP, 15),)
        assert _by_title(res, WORKER_EXAMPLE).drop_reason == _reason(WORKER_EXAMPLE)
        assert _by_title(res, "Unchecked data write").drop_reason == f"{GROUPED_INTO_PREFIX}{APP}:9"

    def test_with_the_pass_disabled_the_echo_would_have_been_the_representative(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "apply_example_echo_check", lambda findings, titles: list(findings))
        chunk = [
            _f(3, title=WORKER_EXAMPLE, rule="no-unchecked-data"),
            _f(9, title="Unchecked data read", rule="no-unchecked-data"),
        ]
        res = _run(monkeypatch, chunk=chunk, group_findings=True)
        (rep,) = res["findings_active"]
        assert (rep.line, rep.title) == (3, WORKER_EXAMPLE)

    def test_an_echo_takes_no_error_cap_slot(self, monkeypatch):
        chunk = [
            _f(3, title=WORKER_EXAMPLE, severity="error", confidence=1.0),
            _f(9, title="Unchecked data read", severity="error", confidence=0.7),
        ]
        res = _run(monkeypatch, chunk=chunk, max_errors=1)
        assert [f.title for f in res["findings_active"]] == ["Unchecked data read"]
        assert _by_title(res, WORKER_EXAMPLE).drop_reason == _reason(WORKER_EXAMPLE)

    def test_the_echo_reason_wins_over_an_invalid_location(self, monkeypatch):
        res = _run(monkeypatch, sweep=[_f(0, title=SWEEP_EXAMPLE, file="src/not-in-the-diff.py")])
        assert _by_title(res, SWEEP_EXAMPLE).drop_reason == _reason(SWEEP_EXAMPLE)


class TestWorkerPromptSizeClaim:
    def test_the_packaged_worker_prompt_promises_no_input_size(self):
        text = load_prompt("worker.md")
        assert "30k" not in text
        assert "tokens" not in text.partition(CONTEXT_MARKER)[2].split("### Diff", 1)[0]

    def test_it_still_says_the_diff_is_the_complete_chunk_right_above_it(self):
        user = load_prompt("worker.md").partition(CONTEXT_MARKER)[2]
        assert "{spec_digest}\n\nThe diff below is the complete chunk.\n\n### Diff\n\n```diff\n{diff}\n```" in user

    def test_the_loader_contract_is_unchanged(self):
        text = load_prompt("worker.md")
        assert text.count(CONTEXT_MARKER) == 1
        assert required_placeholders("worker") == frozenset({
            "pr_title", "pr_description", "repo_hint", "ticket_context", "spec_digest", "diff",
            "context_blocks",
        })
