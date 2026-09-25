"""Issue #13: the ``rule`` request, its prompt slot and its acceptance gate.

A run that groups findings asks the model for a per-finding ``rule``: the
:data:`prxref.reviewer.RULE_REQUEST` block rides ``PromptContext.rule_request``
into the SYSTEM half after the ticket-scope block, and the ``## Output Format``
example finding gains a ``"rule"`` key through the ``{rule_example}`` slot that
follows ``{scope_example}`` in both packaged templates. What is pinned:

- with ``rule_request`` empty, every chunk and sweep prompt is byte-identical
  to the 0.15 build base (328d1c9), through the renderers and through a real
  ``orchestrate_review`` run;
- with it set, the SYSTEM half ends with the request after the ticket scope,
  and the example shows ``"rule"`` in both templates, with and without a
  ticket scope, while nothing else in the USER half moves;
- ``PromptContext.rule_active`` alone decides whether a model-supplied
  ``rule`` is kept, through the real reviewer and the orchestrator hops;
- an operator override without the optional ``{rule_example}`` slot loads
  with no error and no warning, still gets the request, and renders;
- the ``normalize_rule`` character ruling: control, surrogate, private-use
  and bidi embedding/override/isolate characters drop the label, while ZWNJ,
  ZWJ and every other format character are kept.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re

import pytest

from prxref import orchestrator
from prxref.llm import InvokeResult
from prxref.prompt_templates import OPTIONAL_PLACEHOLDERS, load_prompt_templates, placeholders, required_placeholders
from prxref.reviewer import (
    _CONTEXT_MARKER,
    _RULE_EXAMPLE,
    _SCOPE_EXAMPLE,
    NO_PROMPT_CONTEXT,
    RULE_REQUEST,
    PromptContext,
    _render_prompt,
    _render_systemic_prompt,
    load_prompt,
    review_chunk,
    review_systemic,
)
from prxref.triage import RULE_MAX_CHARS, normalize_rule, parse_unified_diff
from tests.test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff

MINI_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,3 @@\n"
    " import os\n"
    "+import sys\n"
    " print(os.name)\n"
)
DIGEST = "## src/app.py\n@@ -1,2 +1,3 @@\n+import sys"
RULES_WORKER = "## Team review rules\n\nWorker framing: never log tokens."
RULES_SWEEP = "## Team review rules\n\nSweep framing: never log tokens."
TICKET_SCOPE = "## Ticket scope\n\nAdd a \"scope\" key to every finding."
TICKET_BLOCK = "### Ticket context\n\n```text\nShip the header flag.\n```"
SPEC = "[spec:spec.md#L1] (MUST) tools MUST be named with the mcp prefix"

EXAMPLE_KEYS = ["file", "line", "severity", "confidence", "title", "body"]
RULE_HEADING = "## Rule names"
_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")


def _without_rule_slot(name: str) -> str:
    """The packaged template as an override exported before the slot existed."""
    head, marker, tail = load_prompt(name).partition(_CONTEXT_MARKER)
    return head + marker + tail.replace("{rule_example}", "")


def _override(name: str) -> str:
    head, marker, tail = _without_rule_slot(name).partition(_CONTEXT_MARKER)
    return "TEAM OVERRIDE: style-guide findings are welcome.\n\n" + head + marker + tail


CONTEXTS = {
    "off": NO_PROMPT_CONTEXT,
    "scope": PromptContext(ticket_scope=TICKET_SCOPE),
    "full": PromptContext(
        rules_worker=RULES_WORKER, rules_sweep=RULES_SWEEP, ticket_scope=TICKET_SCOPE,
        ticket_context=TICKET_BLOCK, spec_digest=SPEC,
    ),
    "override": PromptContext(
        ticket_scope=TICKET_SCOPE,
        worker_template=_override("worker.md"), systemic_template=_override("systemic.md"),
    ),
}

# sha256 of (system, user) for every prompt below, captured by rendering the
# same inputs at 328d1c9 before any #13 edit. The five worker USER hashes
# were re-derived when worker.md dropped its "roughly 30k tokens" promise
# (0.15): each is the earlier render with that one sentence
# replaced by "The diff below is the complete chunk.", and every system hash
# and every sweep hash is unchanged.
BASE_GOLDEN = {
    "worker/off": (
        "7484254cf7461fb7dab2aeee4599d9e4992af293f23e00d967801f8a48e99a7c",
        "4efbc87d908fc50b2ea9adb6dbc43b58e144a8c99734cef5790e425851b2f0f4",
    ),
    "sweep/off": (
        "1ff5d002746e76e39f456d76db1e935aaacad22296b5319ccda23e392bbd65b6",
        "577820a0c654d7ac50edab15990cf9d527c83317ad90aa16a6ded0f4dc8aa80f",
    ),
    "worker/scope": (
        "9dadd864c8c99ecd8937c7e111b8ae6cd26eb3e0cd7cebd09efd5e03cc5d4539",
        "5a6936e0eb45ca13d15ab7e70f7eb5879cc63ac3113f635b3294268f9c91e85f",
    ),
    "sweep/scope": (
        "a1c032c184f4dd82fdb986f7087a95971fb1a596aa6b49887aaa33687a27eb50",
        "971380d4218e5aaebd8afd76787b1e57ba08697d910d30547dd7ac219fb70103",
    ),
    "worker/full": (
        "19aea16e223c4c80c809728fde826c6bc7185f4173fece80f83b28e491f662a4",
        "118d48be8eeac397b8246b5c6bfc3afb1478100f678c2a540648e41a37f4b715",
    ),
    "sweep/full": (
        "82e4d0907a0f57da0f7f69ee8754b0bc3ba5ec9d22a77b78f172cea8b122c9a2",
        "3dcf3ce2490de05559933421db5beb8ab046a884f1d38d7470cf06921874732a",
    ),
    "worker/override": (
        "7518a47a993e55aceaf61f8141c3a6748ce73b63c87f460948fdaefd64aef69d",
        "5a6936e0eb45ca13d15ab7e70f7eb5879cc63ac3113f635b3294268f9c91e85f",
    ),
    "sweep/override": (
        "2395bb9b0607756b574773f6c49b4dbd89bac79dcbef6942b5bc732cad93aa72",
        "971380d4218e5aaebd8afd76787b1e57ba08697d910d30547dd7ac219fb70103",
    ),
    "orchestrator/0": (
        "1ff5d002746e76e39f456d76db1e935aaacad22296b5319ccda23e392bbd65b6",
        "69f464cf7517584bb8630547fde874febcff84f0498e95d7292a175952383c43",
    ),
    "orchestrator/1": (
        "7484254cf7461fb7dab2aeee4599d9e4992af293f23e00d967801f8a48e99a7c",
        "0113e78e0815af17dbbc6f2e642bfacc237f9464ead72197801a6c23799b2239",
    ),
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _worker(ctx: PromptContext = NO_PROMPT_CONTEXT) -> tuple[str, str]:
    return _render_prompt(
        parse_unified_diff(MINI_DIFF), "t", "d", "r", context_blocks="CTX", prompt_context=ctx,
    )


def _sweep(ctx: PromptContext = NO_PROMPT_CONTEXT) -> tuple[str, str]:
    return _render_systemic_prompt(DIGEST, "t", "d", "r", prompt_context=ctx)


RENDERERS = {"worker": (_worker, "worker.md"), "sweep": (_sweep, "systemic.md")}
RENDER_IDS = list(RENDERERS)


def _head(name: str) -> str:
    return load_prompt(name).partition(_CONTEXT_MARKER)[0].strip()


def _example(user: str) -> dict:
    """The ``## Output Format`` JSON example finding of a rendered user prompt."""
    section = user.rsplit("## Output Format", 1)[1]
    (finding,) = json.loads(section.split("```json\n", 1)[1].split("\n```", 1)[0])["findings"]
    return finding


def _on(ctx: PromptContext = NO_PROMPT_CONTEXT) -> PromptContext:
    return dataclasses.replace(ctx, rule_request=RULE_REQUEST)


class _Recorder:
    """Records every (system, user) prompt and answers with one scripted text."""

    def __init__(self, text: str = '{"findings": [], "escalations": []}'):
        self.text = text
        self.prompts: list[tuple[str, str]] = []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.prompts.append((system, user))
        return InvokeResult(
            text=self.text, input_tokens=10, output_tokens=5,
            model="rec-model-1", backend="fake", elapsed_ms=1,
        )


def _ruled_reply(rule: object) -> str:
    return json.dumps({"findings": [{
        "file": "src/app.py", "line": 2, "severity": "warning", "confidence": 0.8,
        "title": "t", "body": "b", "scope": "out", "rule": rule,
    }]})


class TestFeatureOffMatchesBase:
    @pytest.mark.parametrize("unit", RENDER_IDS)
    @pytest.mark.parametrize("ctx", list(CONTEXTS))
    def test_every_prompt_is_byte_identical_to_the_base(self, unit, ctx):
        render, _name = RENDERERS[unit]
        system, user = render(CONTEXTS[ctx])
        assert (_sha(system), _sha(user)) == BASE_GOLDEN[f"{unit}/{ctx}"]

    def test_a_real_orchestrator_run_sends_the_base_prompts(self):
        llm = _Recorder()
        orchestrator.orchestrate_review(FakeForge(diff=_added_file_diff("src/app.py", 20)), REF, llm, post=False)
        prompts = sorted(llm.prompts)
        assert len(prompts) == 2, "one chunk unit and one sweep"
        got = {f"orchestrator/{i}": (_sha(s), _sha(u)) for i, (s, u) in enumerate(prompts)}
        assert got == {k: v for k, v in BASE_GOLDEN.items() if k.startswith("orchestrator/")}

    def test_the_request_is_empty_by_default(self):
        assert PromptContext().rule_request == ""
        assert NO_PROMPT_CONTEXT.rule_active is False

    @pytest.mark.parametrize("unit", RENDER_IDS)
    def test_an_empty_request_leaves_no_rule_trace_in_either_half(self, unit):
        render, _name = RENDERERS[unit]
        for ctx in CONTEXTS.values():
            system, user = render(ctx)
            assert RULE_HEADING not in system
            assert '"rule"' not in user
            assert _PLACEHOLDER.findall(user) == []


class TestRuleRequestText:
    def test_it_is_one_headed_block(self):
        assert RULE_REQUEST.startswith(f"{RULE_HEADING}\n\n")
        assert RULE_REQUEST.count("## ") == 1
        assert RULE_REQUEST == RULE_REQUEST.strip()

    def test_it_asks_for_a_short_single_line_rule_name_or_nothing(self):
        text = " ".join(RULE_REQUEST.split())
        assert '"rule"' in text
        assert "team review rules" in text
        assert f"at most {RULE_MAX_CHARS} characters" in text
        assert "one line" in text
        assert 'leave "rule" out' in text

    def test_it_never_touches_severity_or_confidence(self):
        assert '"rule" never changes "severity" or "confidence".' in RULE_REQUEST

    def test_the_example_rule_is_a_label_normalize_rule_keeps(self):
        (value,) = json.loads("{" + _RULE_EXAMPLE.lstrip(",") + "}").values()
        assert normalize_rule(value) == value == "no-bare-except"

    def test_the_example_is_indented_like_the_scope_example(self):
        assert _RULE_EXAMPLE.startswith(',\n      "rule": ')
        assert _SCOPE_EXAMPLE.startswith(',\n      "scope": ')


class TestPackagedSlot:
    @pytest.mark.parametrize("name", ["worker.md", "systemic.md"])
    def test_the_slot_follows_the_scope_slot_once(self, name):
        template = load_prompt(name)
        assert template.count("{rule_example}") == 1
        assert template.count("{scope_example}{rule_example}\n    }") == 1

    @pytest.mark.parametrize("name", ["worker", "systemic"])
    def test_the_slot_is_below_the_marker_and_optional(self, name):
        _head_text, _marker, tail = load_prompt(f"{name}.md").partition(_CONTEXT_MARKER)
        assert "rule_example" in placeholders(tail)
        assert "rule_example" in OPTIONAL_PLACEHOLDERS
        assert "rule_example" not in required_placeholders(name)


class TestRuleOn:
    @pytest.mark.parametrize("unit", RENDER_IDS)
    def test_the_system_half_ends_with_the_request(self, unit):
        render, name = RENDERERS[unit]
        system, user = render(_on())
        assert system == f"{_head(name)}\n\n{RULE_REQUEST}"
        assert RULE_REQUEST not in user

    @pytest.mark.parametrize("unit", RENDER_IDS)
    def test_the_request_follows_the_ticket_scope_block(self, unit):
        render, name = RENDERERS[unit]
        system, _user = render(_on(CONTEXTS["scope"]))
        assert system == f"{_head(name)}\n\n{TICKET_SCOPE}\n\n{RULE_REQUEST}"
        assert system.count(RULE_HEADING) == 1

    @pytest.mark.parametrize("unit", RENDER_IDS)
    def test_rules_then_scope_then_request(self, unit):
        render, name = RENDERERS[unit]
        rules = RULES_WORKER if unit == "worker" else RULES_SWEEP
        system, _user = render(_on(CONTEXTS["full"]))
        assert system == f"{_head(name)}\n\n{rules}\n\n{TICKET_SCOPE}\n\n{RULE_REQUEST}"

    @pytest.mark.parametrize("unit", RENDER_IDS)
    def test_the_example_finding_ends_in_rule(self, unit):
        render, _name = RENDERERS[unit]
        _system, user = render(_on())
        finding = _example(user)
        assert list(finding) == [*EXAMPLE_KEYS, "rule"]
        assert finding["rule"] == "no-bare-except"
        assert _PLACEHOLDER.findall(user) == []

    @pytest.mark.parametrize("unit", RENDER_IDS)
    def test_with_a_ticket_scope_the_example_ends_in_scope_then_rule(self, unit):
        render, _name = RENDERERS[unit]
        _system, user = render(_on(CONTEXTS["scope"]))
        finding = _example(user)
        assert list(finding) == [*EXAMPLE_KEYS, "scope", "rule"]
        assert (finding["scope"], finding["rule"]) == ("in", "no-bare-except")
        assert _PLACEHOLDER.findall(user) == []

    @pytest.mark.parametrize("ctx", ["off", "scope", "full"])
    @pytest.mark.parametrize("unit", RENDER_IDS)
    def test_the_rule_key_is_the_only_change_to_the_user_half(self, unit, ctx):
        render, _name = RENDERERS[unit]
        _system, user_off = render(CONTEXTS[ctx])
        _system, user_on = render(_on(CONTEXTS[ctx]))
        assert user_on.count(_RULE_EXAMPLE) == 1
        assert user_on.replace(_RULE_EXAMPLE, "", 1) == user_off

    @pytest.mark.parametrize("ctx", ["off", "scope", "full"])
    @pytest.mark.parametrize("unit", RENDER_IDS)
    def test_the_request_block_is_the_only_change_to_the_system_half(self, unit, ctx):
        render, _name = RENDERERS[unit]
        system_off, _user = render(CONTEXTS[ctx])
        system_on, _user = render(_on(CONTEXTS[ctx]))
        assert system_on == f"{system_off}\n\n{RULE_REQUEST}"

    def test_a_ticket_quoting_the_slot_renders_it_literally(self):
        ctx = _on(PromptContext(ticket_context="### Ticket context\n\nquote {rule_example} here"))
        _system, user = _worker(ctx)
        assert "quote {rule_example} here" in user
        assert user.count(_RULE_EXAMPLE) == 1


class TestRuleActiveDrivesAcceptance:
    def test_rule_active_follows_the_request(self):
        assert PromptContext(rule_request=RULE_REQUEST).rule_active is True
        assert PromptContext(ticket_scope=TICKET_SCOPE).rule_active is False
        assert PromptContext(rules_worker=RULES_WORKER).rule_active is False

    def test_rule_active_is_a_property_not_a_field(self):
        assert isinstance(PromptContext.__dict__["rule_active"], property)
        assert "rule_active" not in {f.name for f in dataclasses.fields(PromptContext)}
        assert dataclasses.fields(PromptContext)[7].name == "rule_request"

    @pytest.mark.parametrize("unit", ["chunk", "sweep"])
    def test_the_reviewer_keeps_rule_only_when_asked(self, unit):
        def run(ctx):
            llm = _Recorder(_ruled_reply(" no-print "))
            if unit == "chunk":
                findings, meta = review_chunk(llm, parse_unified_diff(MINI_DIFF), prompt_context=ctx)
            else:
                findings, meta = review_systemic(llm, DIGEST, prompt_context=ctx)
            assert meta["error"] == ""
            ((system, _user),) = llm.prompts
            return [f.rule for f in findings], system.endswith(RULE_REQUEST)

        assert run(_on()) == (["no-print"], True)
        assert run(NO_PROMPT_CONTEXT) == ([None], False)
        assert run(CONTEXTS["scope"]) == ([None], False)
        assert run(_on(CONTEXTS["scope"])) == (["no-print"], True)

    @pytest.mark.parametrize("unit", ["chunk", "sweep"])
    def test_an_unusable_rule_is_none_even_when_asked(self, unit):
        llm = _Recorder(_ruled_reply("no\u202eprint"))
        if unit == "chunk":
            findings, _meta = review_chunk(llm, parse_unified_diff(MINI_DIFF), prompt_context=_on())
        else:
            findings, _meta = review_systemic(llm, DIGEST, prompt_context=_on())
        assert [f.rule for f in findings] == [None]

    ITEM = {"file": "a.py", "line": 1, "severity": "error", "confidence": 0.9,
            "title": "t", "body": "b", "rule": " no-print "}

    def _double(self, llm, files, **kw):
        return [dict(self.ITEM)], {"error": "", "model": "m"}

    def test_the_orchestrator_chunk_hop_reads_the_property(self, monkeypatch):
        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", self._double)
        pr = FakeForge().get_pr(REF)

        def rules(ctx):
            res = orchestrator._invoke_chunk(FakeLLM(), [], pr, None, None, prompt_context=ctx)
            return [f.rule for f in res["findings"]]

        assert rules(_on()) == ["no-print"]
        assert rules(NO_PROMPT_CONTEXT) == [None]

    def test_the_orchestrator_sweep_hop_reads_the_property(self, monkeypatch):
        monkeypatch.setattr(orchestrator.reviewer, "review_systemic", self._double)
        pr = FakeForge().get_pr(REF)

        def rules(ctx):
            res = orchestrator._run_sweep(FakeLLM(), [], pr, prompt_context=ctx)
            return [f.rule for f in res["findings"]]

        assert rules(_on()) == ["no-print"]
        assert rules(NO_PROMPT_CONTEXT) == [None]


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_prompts(work, files: dict[str, str]) -> None:
    d = work / "prompts"
    d.mkdir()
    for fname, text in files.items():
        (d / fname).write_text(text, encoding="utf-8")


class TestOverrideWithoutTheSlot:
    """An operator override written before 0.15 has no ``{rule_example}``."""

    def _load(self, work, caplog):
        _write_prompts(work, {
            "worker.md": _without_rule_slot("worker.md"),
            "systemic.md": _without_rule_slot("systemic.md"),
        })
        with caplog.at_level(logging.WARNING, logger="prxref"):
            loaded = load_prompt_templates("prompts", source="PRXREF_PROMPTS_DIR")
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
        return loaded

    def _ctx(self, loaded, **kw):
        return PromptContext(
            worker_template=loaded.override("worker"),
            systemic_template=loaded.override("systemic"),
            rule_request=RULE_REQUEST, **kw,
        )

    def test_it_loads_with_no_error_and_no_warning(self, work, caplog):
        loaded = self._load(work, caplog)
        for name in ("worker", "systemic"):
            assert "{rule_example}" not in loaded.override(name)
            assert "{scope_example}" in loaded.override(name)

    @pytest.mark.parametrize("unit", RENDER_IDS)
    def test_it_still_gets_the_request_but_no_example_key(self, work, caplog, unit):
        render, name = RENDERERS[unit]
        loaded = self._load(work, caplog)
        system, user = render(self._ctx(loaded))
        assert system == f"{_head(name)}\n\n{RULE_REQUEST}"
        assert list(_example(user)) == EXAMPLE_KEYS
        assert '"rule"' not in user
        assert _PLACEHOLDER.findall(user) == []

    @pytest.mark.parametrize("unit", RENDER_IDS)
    def test_with_a_ticket_scope_it_renders_the_scope_key_only(self, work, caplog, unit):
        render, _name = RENDERERS[unit]
        loaded = self._load(work, caplog)
        _system, user = render(self._ctx(loaded, ticket_scope=TICKET_SCOPE))
        assert list(_example(user)) == [*EXAMPLE_KEYS, "scope"]

    def test_the_rule_is_still_kept_when_asked(self, work, caplog):
        loaded = self._load(work, caplog)
        llm = _Recorder(_ruled_reply("no-print"))
        findings, _meta = review_chunk(llm, parse_unified_diff(MINI_DIFF), prompt_context=self._ctx(loaded))
        assert [f.rule for f in findings] == ["no-print"]

    def test_an_exported_override_keeps_the_slot_and_shows_the_key(self, work, caplog):
        _write_prompts(work, {"worker.md": load_prompt("worker.md")})
        with caplog.at_level(logging.WARNING, logger="prxref"):
            loaded = load_prompt_templates("prompts", source="PRXREF_PROMPTS_DIR")
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
        _system, user = _worker(PromptContext(worker_template=loaded.override("worker"), rule_request=RULE_REQUEST))
        assert list(_example(user)) == [*EXAMPLE_KEYS, "rule"]


ZWNJ, ZWJ = "\u200c", "\u200d"


class TestNormalizeRuleCharacterRuling:
    @pytest.mark.parametrize("raw", [
        f"نیم{ZWNJ}فاصله",
        f"قاعده{ZWNJ}ها",
        f"\U0001f469{ZWJ}\U0001f4bb review",
        f"\U0001f3f3\ufe0f{ZWJ}\U0001f308",
        "no\u200bprint",
        "no\u200eprint",
        "no\u2060print",
        "règle 日本",
    ])
    def test_zwnj_zwj_and_other_format_characters_survive(self, raw):
        assert normalize_rule(raw) == raw

    @pytest.mark.parametrize("raw", [
        "no\x00print", "no\x07print", "no\x1bprint", "no\x7fprint", "no\x9bprint",
        "no\ud800print", "no\udfffprint",
        "no\ue000print", "no\U000f0000print",
        "no\u202aprint", "no\u202bprint", "no\u202cprint", "no\u202dprint", "no\u202eprint",
        "no\u2066print", "no\u2067print", "no\u2068print", "no\u2069print",
    ])
    def test_control_surrogate_private_use_and_bidi_controls_drop_the_label(self, raw):
        assert normalize_rule(raw) is None

    @pytest.mark.parametrize("raw", ["no\x1cprint", "no\x1fprint", "no\x85print", "no\u2028print"])
    def test_whitespace_class_controls_collapse_to_a_space_before_the_check(self, raw):
        assert normalize_rule(raw) == "no print"

    def test_the_neighbours_of_the_bidi_ranges_are_kept(self):
        for ch in ("\u2029", "\u202f", "\u2065", "\u206a"):
            raw = f"a{ch}b"
            expected = " ".join(raw.split())
            assert normalize_rule(raw) == expected

    def test_a_persian_label_survives_a_real_reply(self):
        label = f"نام{ZWNJ}گذاری متغیرها"
        llm = _Recorder(_ruled_reply(label))
        findings, _meta = review_chunk(llm, parse_unified_diff(MINI_DIFF), prompt_context=_on())
        assert [f.rule for f in findings] == [label]
