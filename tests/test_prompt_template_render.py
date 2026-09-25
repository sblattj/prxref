"""Prompt-template overrides (#11): the two renderers honour ``PromptContext`` templates.

``PromptContext.worker_template`` and ``systemic_template`` carry an
operator's override of ``worker.md`` / ``systemic.md`` into
``_render_prompt`` and ``_render_systemic_prompt``. These tests pin that an
override is split at the ``## Review Context`` marker exactly like the
packaged text (head to the system half with the rules and ticket-scope blocks
appended, tail filled by the one-pass keyed ``fill_template``), that each
field reaches only its own renderer, that override text is never run through
``str.format``, and that an empty field is the packaged render byte for byte.

None of these tests uses the ``contract_stubs`` fixture: it replaces
``reviewer.load_prompt`` with a summary-only stub, and every test here renders
the real review templates.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from prxref import reviewer
from prxref.chunk_context import sibling_summary_block
from prxref.forges.base import Thread
from prxref.prompt_templates import load_prompt_templates
from prxref.reviewer import (
    _CONTEXT_MARKER,
    _NO_SPECS_TEXT,
    _SCOPE_EXAMPLE,
    NO_PROMPT_CONTEXT,
    PromptContext,
    _render_prompt,
    _render_systemic_prompt,
    load_prompt,
    render_chunk,
)
from prxref.triage import parse_unified_diff

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
SPEC = "[spec:spec.md#L1] (MUST) tools MUST be named with the mcp prefix"

FULL = PromptContext(
    rules_worker=RULES_WORKER,
    rules_sweep=RULES_SWEEP,
    ticket_scope=TICKET_SCOPE,
    ticket_context=TICKET_BLOCK,
    spec_digest=SPEC,
)

WORKER_HEAD = "You are the ACME chunk reviewer.\n\nStyle-guide findings are welcome."
SYSTEMIC_HEAD = "You are the ACME sweep reviewer.\n\nFlag cross-file drift only."

WORKER_OVERRIDE = (
    f"{WORKER_HEAD}\n\n"
    f"{_CONTEXT_MARKER}\n\n"
    "TITLE={pr_title}\nDESC={pr_description}\nREPO={repo_hint}\n"
    "{ticket_context}SPEC={spec_digest}\n"
    "DIFF:\n{diff}\nCTX:\n{context_blocks}\n"
    'EXAMPLE={"title": "x"{scope_example}}\n'
)
SYSTEMIC_OVERRIDE = (
    f"{SYSTEMIC_HEAD}\n\n"
    f"{_CONTEXT_MARKER}\n\n"
    "TITLE={pr_title}\nDESC={pr_description}\nREPO={repo_hint}\n"
    "{ticket_context}SPEC={spec_digest}\n"
    "DIGEST:\n{digest}\n"
    'EXAMPLE={"title": "x"{scope_example}}\n'
)

STYLE_LINE = "no style-guide nits that change neither behavior nor risk"
STYLE_RELAXED = "style-guide findings that enforce the team standard are welcome"

PROBES = ("{ stray", "{0}", "{diff.__class__}", "{pr_title!r}", "{pr_title:>9}", r"\g<0> \1")


def _chunk():
    return parse_unified_diff(MINI_DIFF)


def _worker(ctx=NO_PROMPT_CONTEXT, *, title="t", description="d", context_blocks="CTX"):
    return _render_prompt(
        _chunk(), title, description, "r", context_blocks=context_blocks, prompt_context=ctx,
    )


def _sweep(ctx=NO_PROMPT_CONTEXT, *, title="t", description="d", threads=()):
    return _render_systemic_prompt(DIGEST, title, description, "r", threads, prompt_context=ctx)


def _blocks(context_blocks="CTX") -> str:
    return "\n\n".join(b for b in (sibling_summary_block(_chunk(), ()), context_blocks) if b)


def _packaged(name: str) -> str:
    return load_prompt(f"{name}.md")


def _with(ctx: PromptContext, **fields: str) -> PromptContext:
    return dataclasses.replace(ctx, **fields)


class TestWorkerOverride:
    def test_the_override_head_is_the_system_half(self):
        system, _ = _worker(PromptContext(worker_template=WORKER_OVERRIDE))
        assert system == WORKER_HEAD

    def test_the_override_tail_is_filled_by_every_keyed_slot(self):
        _, user = _worker(PromptContext(worker_template=WORKER_OVERRIDE))
        assert user == (
            f"{_CONTEXT_MARKER}\n\n"
            f"TITLE=t\nDESC=d\nREPO=r\nSPEC={_NO_SPECS_TEXT}\n"
            f"DIFF:\n{render_chunk(_chunk())}\nCTX:\n{_blocks()}\n"
            'EXAMPLE={"title": "x"}'
        )

    def test_an_active_ticket_fills_the_override_tail_slots(self):
        _, user = _worker(_with(FULL, worker_template=WORKER_OVERRIDE))
        assert f"REPO=r\n{TICKET_BLOCK}\n\nSPEC={SPEC}\n" in user
        assert user.endswith('EXAMPLE={"title": "x"' + _SCOPE_EXAMPLE + "}")

    def test_rules_then_ticket_scope_are_appended_to_the_override_head(self):
        system, user = _worker(_with(FULL, worker_template=WORKER_OVERRIDE))
        assert system == f"{WORKER_HEAD}\n\n{RULES_WORKER}\n\n{TICKET_SCOPE}"
        assert RULES_SWEEP not in system
        assert RULES_WORKER not in user and TICKET_SCOPE not in user

    def test_nothing_of_the_packaged_template_survives(self):
        system, user = _worker(PromptContext(worker_template=WORKER_OVERRIDE))
        packaged_head = _packaged("worker").partition(_CONTEXT_MARKER)[0].strip()
        assert packaged_head not in system and STYLE_LINE not in system + user
        assert "## Output Format" not in user

    def test_an_override_without_the_marker_raises_naming_the_marker(self):
        with pytest.raises(ValueError, match=_CONTEXT_MARKER):
            _worker(PromptContext(worker_template="no marker here {diff}"))


class TestSystemicOverride:
    def test_the_override_head_is_the_system_half(self):
        system, _ = _sweep(PromptContext(systemic_template=SYSTEMIC_OVERRIDE))
        assert system == SYSTEMIC_HEAD

    def test_the_override_tail_is_filled_by_every_keyed_slot(self):
        _, user = _sweep(PromptContext(systemic_template=SYSTEMIC_OVERRIDE))
        assert user == (
            f"{_CONTEXT_MARKER}\n\n"
            f"TITLE=t\nDESC=d\nREPO=r\nSPEC={_NO_SPECS_TEXT}\n"
            f"DIGEST:\n{DIGEST}\n"
            'EXAMPLE={"title": "x"}'
        )

    def test_an_active_ticket_fills_the_override_tail_slots(self):
        _, user = _sweep(_with(FULL, systemic_template=SYSTEMIC_OVERRIDE))
        assert f"REPO=r\n{TICKET_BLOCK}\n\nSPEC={SPEC}\n" in user
        assert user.endswith('EXAMPLE={"title": "x"' + _SCOPE_EXAMPLE + "}")

    def test_rules_then_ticket_scope_are_appended_to_the_override_head(self):
        system, user = _sweep(_with(FULL, systemic_template=SYSTEMIC_OVERRIDE))
        assert system == f"{SYSTEMIC_HEAD}\n\n{RULES_SWEEP}\n\n{TICKET_SCOPE}"
        assert RULES_WORKER not in system
        assert RULES_SWEEP not in user and TICKET_SCOPE not in user

    def test_the_discussion_block_still_follows_the_override_tail(self):
        thread = Thread(path="src/app.py", line=None, resolved=False, author="ana", body_snippet="why sys?")
        ctx = PromptContext(systemic_template=SYSTEMIC_OVERRIDE)
        _, user = _sweep(ctx, threads=[thread])
        _, bare = _sweep(ctx)
        assert user == f"{bare}\n\n### Existing discussion\n\n- src/app.py: ana: why sys?"

    def test_an_override_without_the_marker_raises_naming_the_marker(self):
        with pytest.raises(ValueError, match=_CONTEXT_MARKER):
            _sweep(PromptContext(systemic_template="no marker here {digest}"))


class TestEachFieldReachesOnlyItsRenderer:
    @pytest.mark.parametrize("base", [NO_PROMPT_CONTEXT, FULL], ids=["unset", "full"])
    def test_a_worker_override_leaves_the_systemic_render_unchanged(self, base):
        assert _sweep(_with(base, worker_template=WORKER_OVERRIDE)) == _sweep(base)

    @pytest.mark.parametrize("base", [NO_PROMPT_CONTEXT, FULL], ids=["unset", "full"])
    def test_a_systemic_override_leaves_the_worker_render_unchanged(self, base):
        assert _worker(_with(base, systemic_template=SYSTEMIC_OVERRIDE)) == _worker(base)

    def test_both_overrides_together_each_reach_their_own_renderer(self):
        ctx = PromptContext(worker_template=WORKER_OVERRIDE, systemic_template=SYSTEMIC_OVERRIDE)
        assert _worker(ctx) == _worker(PromptContext(worker_template=WORKER_OVERRIDE))
        assert _sweep(ctx) == _sweep(PromptContext(systemic_template=SYSTEMIC_OVERRIDE))


class TestLoadPromptIsReadOnlyForAnEmptyField:
    @pytest.fixture
    def calls(self, monkeypatch) -> list[str]:
        seen: list[str] = []
        real = reviewer.load_prompt

        def spy(name: str) -> str:
            seen.append(name)
            return real(name)

        monkeypatch.setattr(reviewer, "load_prompt", spy)
        return seen

    def test_an_empty_worker_field_reads_the_packaged_worker_template(self, calls):
        _worker(PromptContext(systemic_template=SYSTEMIC_OVERRIDE))
        assert calls == ["worker.md"]

    def test_a_worker_override_never_reads_the_packaged_template(self, calls):
        _worker(PromptContext(worker_template=WORKER_OVERRIDE))
        assert calls == []

    def test_an_empty_systemic_field_reads_the_packaged_systemic_template(self, calls):
        _sweep(PromptContext(worker_template=WORKER_OVERRIDE))
        assert calls == ["systemic.md"]

    def test_a_systemic_override_never_reads_the_packaged_template(self, calls):
        _sweep(PromptContext(systemic_template=SYSTEMIC_OVERRIDE))
        assert calls == []


def _probe_override(slots: str) -> str:
    probes = " | ".join(PROBES)
    return (
        f"HEAD {probes} {{pr_title}}\n\n{_CONTEXT_MARKER}\n\n"
        f"TAIL {probes}\nTITLE={{pr_title}}\nDESC={{pr_description}}\nREPO={{repo_hint}}\n{slots}\n"
    )


WORKER_PROBE = _probe_override("{ticket_context}{spec_digest}\n{diff}\n{context_blocks}{scope_example}")
SYSTEMIC_PROBE = _probe_override("{ticket_context}{spec_digest}\n{digest}{scope_example}")

RENDERS = [
    pytest.param(lambda ctx, **kw: _worker(ctx, **kw), "worker_template", WORKER_PROBE, id="worker"),
    pytest.param(lambda ctx, **kw: _sweep(ctx, **kw), "systemic_template", SYSTEMIC_PROBE, id="systemic"),
]


class TestOverrideTextIsNeverFormatted:
    @pytest.mark.parametrize(("render", "field", "override"), RENDERS)
    def test_stray_and_foreign_braces_render_literally_in_both_halves(self, render, field, override):
        system, user = render(PromptContext(**{field: override}))
        for probe in PROBES:
            assert probe in system
            assert probe in user
        assert "TITLE=t\n" in user and "TITLE={pr_title}" not in user

    @pytest.mark.parametrize(("render", "field", "override"), RENDERS)
    def test_the_head_is_never_filled_so_pr_data_stays_out_of_the_system_half(self, render, field, override):
        system, _ = render(PromptContext(**{field: override}), title="SECRET-TITLE")
        assert system.endswith("{pr_title}")
        assert "SECRET-TITLE" not in system

    def test_a_pr_title_naming_the_diff_slot_renders_literally(self):
        diff = render_chunk(_chunk())
        _, user = _worker(PromptContext(worker_template=WORKER_OVERRIDE), title="{diff} {0}")
        assert "TITLE={diff} {0}\n" in user
        assert user.count(diff) == 1

    def test_a_pr_title_naming_the_digest_slot_renders_literally(self):
        _, user = _sweep(PromptContext(systemic_template=SYSTEMIC_OVERRIDE), title="{digest} {0}")
        assert "TITLE={digest} {0}\n" in user
        assert user.count(DIGEST) == 1

    @pytest.mark.parametrize(("render", "field", "override"), RENDERS)
    def test_a_description_full_of_probes_renders_literally(self, render, field, override):
        description = " ".join(PROBES) + " {pr_title} {spec_digest}"
        _, user = render(PromptContext(**{field: override}), description=description)
        assert f"TITLE=t\nDESC={description}\nREPO=r\n" in user


class TestAnEmptyFieldIsThePackagedRender:
    def test_the_empty_fields_are_the_shared_default(self):
        assert PromptContext(worker_template="", systemic_template="") == NO_PROMPT_CONTEXT

    @pytest.mark.parametrize("base", [NO_PROMPT_CONTEXT, FULL], ids=["unset", "full"])
    def test_the_packaged_text_as_a_worker_override_renders_byte_identically(self, base):
        assert _worker(base) == _worker(_with(base, worker_template=_packaged("worker")))

    @pytest.mark.parametrize("base", [NO_PROMPT_CONTEXT, FULL], ids=["unset", "full"])
    def test_the_packaged_text_as_a_systemic_override_renders_byte_identically(self, base):
        assert _sweep(base) == _sweep(_with(base, systemic_template=_packaged("systemic")))

    @pytest.mark.parametrize("name", ["worker", "systemic"])
    def test_an_empty_field_renders_the_packaged_head_and_tail(self, name):
        render = _worker if name == "worker" else _sweep
        system, user = render(PromptContext())
        head, marker, _ = _packaged(name).partition(_CONTEXT_MARKER)
        assert system == head.strip()
        assert user.startswith(marker) and "{" + "pr_title" + "}" not in user


@pytest.fixture
def work(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    return root


def _relaxed(name: str) -> str:
    text = _packaged(name)
    head, marker, tail = text.partition(_CONTEXT_MARKER)
    assert STYLE_LINE in head
    return head.replace(STYLE_LINE, STYLE_RELAXED) + marker + tail


def _write(work: Path, files: dict[str, bytes]) -> str:
    d = work / "prompts"
    d.mkdir()
    for fname, data in files.items():
        (d / fname).write_bytes(data)
    return str(d)


class TestEndToEndWithTheRealLoader:
    def test_a_worker_only_override_relaxes_the_worker_head_and_leaves_the_sweep_packaged(self, work):
        prompts = load_prompt_templates(
            _write(work, {"worker.md": _relaxed("worker").encode()}), source="--prompts-dir",
        )
        assert prompts is not None and prompts.overridden == ("worker",)
        ctx = _with(
            FULL,
            worker_template=prompts.override("worker"),
            systemic_template=prompts.override("systemic"),
        )
        assert ctx.systemic_template == ""
        system, user = _worker(ctx)
        packaged_system, packaged_user = _worker(FULL)
        assert system == packaged_system.replace(STYLE_LINE, STYLE_RELAXED)
        assert STYLE_LINE not in system and system.endswith(f"\n\n{RULES_WORKER}\n\n{TICKET_SCOPE}")
        assert user == packaged_user
        assert _sweep(ctx) == _sweep(FULL)

    def test_both_overrides_reach_their_renderers(self, work):
        prompts = load_prompt_templates(
            _write(work, {
                "worker.md": _relaxed("worker").encode(),
                "systemic.md": _relaxed("systemic").encode(),
            }),
            source="PRXREF_PROMPTS_DIR",
        )
        assert prompts is not None and prompts.overridden == ("worker", "systemic")
        ctx = PromptContext(
            worker_template=prompts.override("worker"),
            systemic_template=prompts.override("systemic"),
        )
        for render in (_worker, _sweep):
            system, user = render(ctx)
            packaged_system, packaged_user = render()
            assert system == packaged_system.replace(STYLE_LINE, STYLE_RELAXED) != packaged_system
            assert user == packaged_user

    def test_a_crlf_override_renders_as_its_lf_twin(self, work):
        crlf = _relaxed("worker").replace("\n", "\r\n").encode()
        prompts = load_prompt_templates(_write(work, {"worker.md": crlf}), source="--prompts-dir")
        assert prompts is not None
        system, user = _worker(PromptContext(worker_template=prompts.override("worker")))
        assert "\r" not in system + user
        assert (system, user) == _worker(PromptContext(worker_template=_relaxed("worker")))
