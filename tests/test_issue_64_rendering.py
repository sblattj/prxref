"""Issue #64 rendering: the out-of-ticket marker on every output surface.

Design #64 §10 and contract §6.1. A finding whose ``scope`` is ``out`` keeps
its severity glyph and gains the 🟦 prefix in front of it:

- the summary lists it after the others, under ``**🟦 Outside the ticket (N)**``
  (``No in-ticket findings.`` stands in for an empty first list);
- the pipeline's inline header reads ``🤖 🟦 🟧 **[WARNING · OUTSIDE TICKET] …**``
  (``markers.inline_header``);
- the library formatter's table cell and inline comment carry the prefix;
- the CLI text line ends in `` [scope: out]`` (or `` [scope: in]``).

Scope ``in`` and ``unknown`` render byte-identically to a run without a
ticket, which is the feature-off guarantee. The ticket object is faked with
the duck-typed surface the orchestrator reads (``active``, ``record()``,
``note()``, ``scope_block()``, ``prompt_block()``); the real loader is W64A's.

The prompt side of the same field is pinned here too: the ``## Output Format``
JSON example that ends every worker and sweep USER prompt shows
``"scope": "in"`` on its finding only while a ticket is active, and with no
ticket both prompts are byte-identical to the pre-ticket ones.
"""
from __future__ import annotations

import io
import json
import re
import threading
from types import SimpleNamespace

import pytest

from prxref import orchestrator
from prxref.cli import _fmt_finding_line, _print_findings
from prxref.formatter import format_inline_comment
from prxref.llm import InvokeResult
from prxref.markers import (
    OUT_OF_TICKET_MARKER,
    SCOPE_LABELS,
    SEVERITY_MARKERS,
    inline_header,
    marker_for,
)
from prxref.orchestrator import _format_finding, _render_summary
from prxref.reviewer import (
    NO_PROMPT_CONTEXT,
    PromptContext,
    _render_prompt,
    _render_systemic_prompt,
)
from prxref.triage import (
    SCOPE_IN,
    SCOPE_OUT,
    SCOPE_UNKNOWN,
    SCOPES,
    Finding,
    parse_unified_diff,
)
from tests.test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff, make_pr

BLUE = "🟦"
ATTRIBUTION = "Reviewed by prxref · model=m · 150 tok · 1.2s"
NOTE_A = (
    "> ℹ️ No ticket context for this PR — findings were not checked against a "
    "ticket's scope.\n"
)
NOTE_B = (
    "> ℹ️ The ticket context has no acceptance criteria — scope was judged from "
    "its description alone.\n"
)

# Design #64 §10.1: every severity the renderers can meet, with its glyph.
# ``blocker`` stands for any unrecognised severity, which falls back to ⬜.
SEVERITY_ROWS = [
    ("error", "🟥"),
    ("warning", "🟧"),
    ("spec", "🔍"),
    ("outofscope", "⬜"),
    ("blocker", "⬜"),
]
STATES = ("NONE", "EMPTY", "NO_AC", "AC")
_ELAPSED = re.compile(r"\d+\.\ds\b")


def _finding(line=3, *, severity="warning", scope=SCOPE_UNKNOWN, title=None, file="a.py"):
    return Finding(
        file=file, line=line, severity=severity, confidence=0.9,
        title=title or f"Problem {line}", body=f"data {line} is wrong", scope=scope,
    )


def _render(findings, **kwargs) -> str:
    return _render_summary(
        make_pr(), ["a.py"], "Request-Changes", list(findings), "m", 100, 50, 1234,
        **kwargs,
    )


def _findings_block(rendered: str) -> str:
    """The ``{findings}`` value inside a summary rendered with empty notes."""
    after_counts = rendered.split(" outofscope\n", 1)[1]
    after_notes = after_counts.split("\n", 1)[1]
    return re.split(r"\n\n(?:---\n\n)?Reviewed by prxref", after_notes, maxsplit=1)[0]


@pytest.fixture(params=["summary.md", "fallback"])
def template(request, monkeypatch):
    """Run once on the packaged template and once on the fallback template."""
    if request.param == "fallback":
        def _boom(name):
            raise RuntimeError("no prompts")

        monkeypatch.setattr(orchestrator.reviewer, "load_prompt", _boom)
    return request.param


class TestInlineHeader:
    """``markers.inline_header``: design #64 §10.1, the inline-header columns."""

    @pytest.mark.parametrize(("severity", "glyph"), SEVERITY_ROWS)
    @pytest.mark.parametrize("scope", [SCOPE_IN, SCOPE_UNKNOWN])
    def test_in_and_unknown_render_the_severity_header(self, severity, glyph, scope):
        f = Finding("src/f.py", 7, severity, 0.5, "Title", "b", scope=scope)
        assert inline_header(f) == f"🤖 {glyph} **[{severity.upper()}] Title** (`src/f.py:7`)"

    @pytest.mark.parametrize(("severity", "glyph"), SEVERITY_ROWS)
    def test_out_prefixes_the_glyph_and_labels_the_severity(self, severity, glyph):
        f = Finding("src/f.py", 7, severity, 0.5, "Title", "b", scope=SCOPE_OUT)
        assert inline_header(f) == (
            f"🤖 🟦 {glyph} **[{severity.upper()} · OUTSIDE TICKET] Title** (`src/f.py:7`)"
        )

    def test_a_file_level_finding_has_no_line(self):
        f = Finding("src/f.py", 0, "error", 0.5, "Title", "b", scope=SCOPE_OUT)
        assert inline_header(f).endswith("(`src/f.py`)")

    def test_the_label_comes_from_the_table(self):
        f = Finding("a.py", 1, "warning", 0.5, "T", "b", scope=SCOPE_OUT)
        assert f"[WARNING · {SCOPE_LABELS[SCOPE_OUT]}]" in inline_header(f)
        assert inline_header(f).startswith(f"🤖 {marker_for('warning', SCOPE_OUT)} ")


class TestFormatFinding:
    """``orchestrator._format_finding`` renders the header, then the unchanged
    body and attribution footer the prune pass matches."""

    @pytest.mark.parametrize("scope", [SCOPE_IN, SCOPE_UNKNOWN])
    def test_in_and_unknown_are_the_0_13_body(self, scope):
        body = _format_finding(Finding("a.py", 1, "error", 0.5, "x", "body", scope=scope), "m")
        assert body == "🤖 🟥 **[ERROR] x** (`a.py:1`)\n\nbody\n\n---\n*Reviewed by prxref · model=m*"

    def test_out_changes_only_the_header(self):
        body = _format_finding(Finding("a.py", 1, "error", 0.5, "x", "body", scope=SCOPE_OUT), "m")
        assert body == (
            "🤖 🟦 🟥 **[ERROR · OUTSIDE TICKET] x** (`a.py:1`)\n\n"
            "body\n\n---\n*Reviewed by prxref · model=m*"
        )

    @pytest.mark.parametrize("scope", SCOPES)
    def test_the_header_is_the_shared_helper(self, scope):
        f = Finding("a.py", 4, "spec", 0.5, "x", "body", scope=scope)
        assert _format_finding(f, "m").split("\n\n", 1)[0] == inline_header(f)


class TestSummaryFindingsList:
    """``_render_summary``'s ``{findings}``: design #64 §10.2."""

    def test_the_flat_list_golden_when_nothing_is_out(self, template):
        findings = [
            _finding(3, severity="error", scope=SCOPE_IN, title="Null deref"),
            _finding(0, severity="outofscope", file="b.py", title="Nit"),
        ]
        assert _findings_block(_render(findings)) == (
            "- 🟥 `a.py:3` — Null deref\n"
            "- ⬜ `b.py:—` — Nit"
        )

    @pytest.mark.parametrize("scope", [SCOPE_IN, SCOPE_UNKNOWN])
    def test_in_and_unknown_render_byte_identically(self, template, scope):
        rows = [(n, sev) for n, (sev, _glyph) in enumerate(SEVERITY_ROWS, 1)]
        scoped = [_finding(n, severity=sev, scope=scope) for n, sev in rows]
        unknown = [_finding(n, severity=sev) for n, sev in rows]
        assert _render(scoped) == _render(unknown)
        assert BLUE not in _render(scoped)

    def test_out_findings_are_grouped_after_the_rest_with_their_count(self, template):
        findings = [
            _finding(1, severity="error", scope=SCOPE_IN, title="A"),
            _finding(2, severity="warning", scope=SCOPE_OUT, title="B"),
            _finding(3, severity="outofscope", title="C"),
            _finding(4, severity="spec", scope=SCOPE_OUT, title="D"),
        ]
        assert _findings_block(_render(findings)) == (
            "- 🟥 `a.py:1` — A\n"
            "- ⬜ `a.py:3` — C\n\n"
            "**🟦 Outside the ticket (2)**\n\n"
            "- 🟦 🟧 `a.py:2` — B\n"
            "- 🟦 🔍 `a.py:4` — D"
        )

    def test_only_out_findings_render_no_in_ticket_findings_line(self, template):
        rendered = _render([_finding(1, severity="error", scope=SCOPE_OUT, title="A")])
        assert _findings_block(rendered) == (
            "No in-ticket findings.\n\n"
            "**🟦 Outside the ticket (1)**\n\n"
            "- 🟦 🟥 `a.py:1` — A"
        )
        assert "nice work" not in rendered

    def test_no_findings_is_unchanged(self, template):
        rendered = _render([])
        assert _findings_block(rendered) == "No findings — nice work."
        assert BLUE not in rendered

    def test_the_counts_line_still_counts_out_findings_by_severity(self):
        rendered = _render([
            _finding(1, severity="error", scope=SCOPE_OUT),
            _finding(2, severity="error"),
            _finding(3, severity="outofscope", scope=SCOPE_OUT),
        ])
        assert "🟥 2 error · 🟧 0 warning · 🔍 0 spec · ⬜ 1 outofscope\n" in rendered

    def test_inline_accounting_follows_the_group(self):
        rendered = _render(
            [_finding(1, scope=SCOPE_OUT, title="A")], inline_accounting="_(1 of 1 inline)_",
        )
        assert _findings_block(rendered) == (
            "No in-ticket findings.\n\n"
            "**🟦 Outside the ticket (1)**\n\n"
            "- 🟦 🟧 `a.py:1` — A\n\n"
            "_(1 of 1 inline)_"
        )

    def test_the_group_heading_is_built_from_the_marker_table(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "OUT_OF_TICKET_MARKER", "@@")
        rendered = _render([_finding(1, scope=SCOPE_OUT)])
        assert "**@@ Outside the ticket (1)**" in rendered
        assert OUT_OF_TICKET_MARKER == BLUE

    def test_a_title_holding_a_placeholder_renders_literally(self):
        rendered = _render([_finding(1, scope=SCOPE_OUT, title="{attribution} {findings}")])
        assert "- 🟦 🟧 `a.py:1` — {attribution} {findings}" in rendered


class TestMarkersAgreeAcrossSurfaces:
    """One marker per (severity, scope), on the summary bullet, the pipeline
    inline header, the formatter's inline comment and the CLI line."""

    @pytest.mark.parametrize(("severity", "glyph"), SEVERITY_ROWS)
    @pytest.mark.parametrize("scope", SCOPES)
    def test_every_surface(self, severity, glyph, scope):
        f = _finding(5, severity=severity, scope=scope, title="T")
        marker = f"🟦 {glyph}" if scope == SCOPE_OUT else glyph
        assert marker_for(severity, scope) == marker
        assert f"- {marker} `a.py:5` — T" in _render([f])
        assert inline_header(f).startswith(f"🤖 {marker} **[")
        assert format_inline_comment(f, "attr").startswith(f"{marker} **")
        suffix = "" if scope == SCOPE_UNKNOWN else f" [scope: {scope}]"
        assert _fmt_finding_line(f) == f"{severity} a.py:5 T (confidence 0.90){suffix}"

    def test_spec_finding_out_of_ticket_renders_blue_and_magnifier(self):
        f = _finding(9, severity="spec", scope=SCOPE_OUT, title="Header sent")
        assert "- 🟦 🔍 `a.py:9` — Header sent" in _render([f])
        assert inline_header(f) == "🤖 🟦 🔍 **[SPEC · OUTSIDE TICKET] Header sent** (`a.py:9`)"
        assert format_inline_comment(f, "a").startswith("🟦 🔍 **[OUTSIDE TICKET] Header sent**")
        assert _fmt_finding_line(f).endswith("(confidence 0.90) [scope: out]")

    @pytest.mark.parametrize("severity", ["blocker", "", "outofscope"])
    def test_fallback_glyph_is_grey_not_blue(self, severity):
        f = _finding(2, severity=severity, title="T")
        assert SEVERITY_MARKERS["outofscope"] == "⬜"
        rendered = _render([f])
        assert "- ⬜ `a.py:2` — T" in rendered
        assert inline_header(f).startswith("🤖 ⬜ **[")
        assert format_inline_comment(f, "a").startswith("⬜ **T**")
        for text in (rendered, inline_header(f), format_inline_comment(f, "a")):
            assert BLUE not in text


class TestCliScopeSuffix:
    """Design #64 §10.5: the suffix goes after the frozen issue-08 prefix."""

    FROZEN = "error src/foo.py:42 Off-by-one in loop bound (confidence 0.92)"

    def _f(self, **kw):
        return Finding("src/foo.py", 42, "error", 0.92, "Off-by-one in loop bound", "b", **kw)

    def test_cli_text_scope_suffix_after_frozen_prefix(self):
        assert _fmt_finding_line(self._f()) == self.FROZEN
        assert _fmt_finding_line(self._f(scope=SCOPE_IN)) == f"{self.FROZEN} [scope: in]"
        assert _fmt_finding_line(self._f(scope=SCOPE_OUT)) == f"{self.FROZEN} [scope: out]"

    @pytest.mark.parametrize("scope", ["maybe", "", None, "OUT"])
    def test_anything_but_in_or_out_adds_nothing(self, scope):
        f = SimpleNamespace(
            severity="error", file="src/foo.py", line=42,
            title="Off-by-one in loop bound", confidence=0.92, scope=scope,
        )
        assert _fmt_finding_line(f) == self.FROZEN

    def test_an_object_without_scope_adds_nothing(self):
        f = SimpleNamespace(
            severity="error", file="src/foo.py", line=42,
            title="Off-by-one in loop bound", confidence=0.92,
        )
        assert _fmt_finding_line(f) == self.FROZEN

    def test_print_findings_carries_the_suffix(self):
        buf = io.StringIO()
        _print_findings({"findings_active": [self._f(scope=SCOPE_OUT)]}, out=buf)
        assert buf.getvalue().splitlines()[0] == f"{self.FROZEN} [scope: out]"


class FakeTicket:
    """The duck-typed ``ticket.TicketContext`` surface, in one configured state."""

    NOTES = {"EMPTY": NOTE_A, "NO_AC": NOTE_B, "AC": ""}

    def __init__(self, state: str):
        self.state = state
        self.active = state != "EMPTY"

    def record(self) -> dict:
        return {
            "path": "ticket.md", "sha256": "c" * 64,
            "chars": 0 if self.state == "EMPTY" else 30, "max_chars": 6000,
            "truncated": False, "has_acceptance_criteria": self.state == "AC",
            "empty": self.state == "EMPTY",
        }

    def note(self) -> str:
        return self.NOTES[self.state]

    def scope_block(self) -> str:
        return "## Ticket scope\n\nMark each finding in, out or unknown."

    def prompt_block(self) -> str:
        return "### Ticket context\n\n```text\nShip the widget.\n```"


def _meta():
    return {
        "escalations": [], "input_tokens": 1, "output_tokens": 1,
        "model": "m", "elapsed_ms": 1, "error": "",
    }


def _post(monkeypatch, findings, *, state="NONE", diff=None):
    """One posting run whose chunk returns ``findings``; returns the forge."""
    def _chunk(llm, files, **kw):
        return list(findings), _meta()

    def _sweep(llm, digest, **kw):
        return [], _meta()

    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _chunk)
    monkeypatch.setattr(orchestrator.reviewer, "review_systemic", _sweep)
    forge = FakeForge(diff=_added_file_diff("src/app.py", 20) if diff is None else diff)
    ticket = None if state == "NONE" else FakeTicket(state)
    orchestrator.orchestrate_review(forge, REF, FakeLLM(), post=True, ticket=ticket)
    return forge


def _inline_bodies(forge) -> list[str]:
    return [c.body for batch in forge.inline_batches for c in batch]


class TestRenderMatrix:
    """Design #64 §14 item 33, end to end through ``orchestrate_review``.

    Severities are the three a ticket-only run can post: an ungrounded
    ``spec`` finding is relabelled ``warning`` and an unrecognised severity
    dies at the quality gate before rendering (both are covered above, where
    the renderers are called directly). NONE and EMPTY hold every scope to
    ``unknown``, so neither may ever show 🟦.
    """

    @pytest.mark.parametrize("state", STATES)
    @pytest.mark.parametrize("scope", SCOPES)
    @pytest.mark.parametrize(("severity", "glyph"), [
        ("error", "🟥"), ("warning", "🟧"), ("outofscope", "⬜"),
    ])
    def test_render_matrix(self, monkeypatch, state, scope, severity, glyph):
        f = Finding(
            "src/app.py", 3, severity, 0.9, "Problem 3", "data 3 is wrong", scope=scope,
        )
        forge = _post(monkeypatch, [f], state=state)
        (summary,) = forge.summaries
        (body,) = _inline_bodies(forge)

        effective = scope if state in ("NO_AC", "AC") else SCOPE_UNKNOWN
        if effective == SCOPE_OUT:
            assert (
                "No in-ticket findings.\n\n**🟦 Outside the ticket (1)**\n\n"
                f"- 🟦 {glyph} `src/app.py:3` — Problem 3"
            ) in summary
            assert body.startswith(
                f"🤖 🟦 {glyph} **[{severity.upper()} · OUTSIDE TICKET] Problem 3** (`src/app.py:3`)"
            )
        else:
            assert f"\n- {glyph} `src/app.py:3` — Problem 3" in summary
            assert body.startswith(f"🤖 {glyph} **[{severity.upper()}] Problem 3** (`src/app.py:3`)")
            assert BLUE not in summary and BLUE not in body

        expected_note = {"NONE": "", "EMPTY": NOTE_A, "NO_AC": NOTE_B, "AC": ""}[state]
        if expected_note:
            assert expected_note in summary
        else:
            assert "ℹ️" not in summary
        if state in ("NONE", "EMPTY"):
            assert BLUE not in summary and BLUE not in body

    def test_mixed_scopes_post_the_group_and_labelled_headers(self, monkeypatch):
        findings = [
            Finding("src/app.py", 3, "error", 0.9, "Problem 3", "data 3 is wrong", scope=SCOPE_IN),
            Finding("src/app.py", 5, "warning", 0.9, "Problem 5", "data 5 is wrong", scope=SCOPE_OUT),
            Finding("src/app.py", 7, "warning", 0.9, "Problem 7", "data 7 is wrong"),
        ]
        forge = _post(monkeypatch, findings, state="AC")
        (summary,) = forge.summaries
        assert (
            "- 🟥 `src/app.py:3` — Problem 3\n"
            "- 🟧 `src/app.py:7` — Problem 7\n\n"
            "**🟦 Outside the ticket (1)**\n\n"
            "- 🟦 🟧 `src/app.py:5` — Problem 5"
        ) in summary
        headers = sorted(b.split("\n", 1)[0] for b in _inline_bodies(forge))
        assert headers == sorted([
            "🤖 🟥 **[ERROR] Problem 3** (`src/app.py:3`)",
            "🤖 🟦 🟧 **[WARNING · OUTSIDE TICKET] Problem 5** (`src/app.py:5`)",
            "🤖 🟧 **[WARNING] Problem 7** (`src/app.py:7`)",
        ])


class TestFeatureOffIsByteIdentical:
    """Design #64 §14 item 34: with no ticket, whatever scope the model claims,
    the posts equal those of a run whose findings are all ``unknown``."""

    def _findings(self, scopes):
        return [
            Finding("src/app.py", n, sev, 0.9, f"Problem {n}", f"data {n} is wrong", scope=s)
            for (n, sev), s in zip(
                [(3, "error"), (5, "warning"), (7, "outofscope")], scopes, strict=True,
            )
        ]

    def test_summary_without_ticket_equals_previous_output_except_minor_glyph(self, monkeypatch):
        claimed = _post(monkeypatch, self._findings([SCOPE_OUT, SCOPE_IN, SCOPE_OUT]))
        plain = _post(monkeypatch, self._findings([SCOPE_UNKNOWN] * 3))
        assert [_ELAPSED.sub("N", s) for s in claimed.summaries] == [
            _ELAPSED.sub("N", s) for s in plain.summaries
        ]
        assert _inline_bodies(claimed) == _inline_bodies(plain)
        (summary,) = claimed.summaries
        assert (
            "\n- 🟥 `src/app.py:3` — Problem 3\n"
            "- 🟧 `src/app.py:5` — Problem 5\n"
            "- ⬜ `src/app.py:7` — Problem 7\n"
        ) in summary
        assert BLUE not in summary
        assert all(BLUE not in b for b in _inline_bodies(claimed))


class TestSummaryOnlyAndErrorRuns:
    """Design #64 §14 item 36: the summary-only run carries the note (its
    release-shape findings are never judged, so no group), the error notice
    carries neither."""

    def test_summary_only_run_carries_the_note_and_no_group(self, monkeypatch):
        forge = _post(monkeypatch, [], state="NO_AC", diff="")
        (summary,) = forge.summaries
        assert NOTE_B in summary
        assert "No findings — nice work." in summary
        assert BLUE not in summary

    def test_error_run_carries_neither(self, monkeypatch):
        monkeypatch.setattr(FakeForge, "get_diff", lambda self, ref: 1 / 0)
        forge = _post(monkeypatch, [], state="NO_AC")
        (notice,) = forge.summaries
        assert notice.startswith("🤖 **prxref review — Error**")
        assert "ℹ️" not in notice and BLUE not in notice


PROMPT_DIFF = """\
diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 import os
+import sys
 print(os.name)
"""
PROMPT_DIGEST = "## src/app.py\n@@ -1,2 +1,3 @@\n+import sys"
EXAMPLE_KEYS = ["file", "line", "severity", "confidence", "title", "body"]
SCOPE_LINE = ',\n      "scope": "in"'
_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")


def _worker_prompt(ctx: PromptContext = NO_PROMPT_CONTEXT) -> tuple[str, str]:
    return _render_prompt(parse_unified_diff(PROMPT_DIFF), "t", "d", "r", prompt_context=ctx)


def _sweep_prompt(ctx: PromptContext = NO_PROMPT_CONTEXT) -> tuple[str, str]:
    return _render_systemic_prompt(PROMPT_DIGEST, "t", "d", "r", prompt_context=ctx)


RENDERERS = [pytest.param(_worker_prompt, id="worker"), pytest.param(_sweep_prompt, id="sweep")]


def _active(ticket_context: str | None = None) -> PromptContext:
    """The prompt context the orchestrator builds for an active ticket."""
    ticket = FakeTicket("AC")
    return PromptContext(
        ticket_scope=ticket.scope_block(),
        ticket_context=ticket.prompt_block() if ticket_context is None else ticket_context,
    )


def _example(user: str) -> dict:
    """The ``## Output Format`` JSON example of a rendered user prompt, parsed."""
    section = user.rsplit("## Output Format", 1)[1]
    return json.loads(section.split("```json\n", 1)[1].split("\n```", 1)[0])


class TestOutputFormatExampleScope:
    """The example ends the USER prompt and a model copies the example it read
    last, so the ``scope`` ask in the SYSTEM prompt alone went unanswered by
    gpt-4.1-mini. The example finding carries ``"scope": "in"`` exactly while
    the system prompt asks for scope, and nothing else in either prompt moves."""

    @pytest.mark.parametrize("render", RENDERERS)
    def test_no_ticket_user_prompt_never_mentions_scope(self, render):
        _system, user = render()
        assert '"scope"' not in user
        assert _PLACEHOLDER.findall(user) == []
        (finding,) = _example(user)["findings"]
        assert list(finding) == EXAMPLE_KEYS

    @pytest.mark.parametrize("render", RENDERERS)
    def test_active_ticket_example_finding_ends_in_scope_in(self, render):
        _system, user = render(_active())
        (finding,) = _example(user)["findings"]
        assert list(finding) == [*EXAMPLE_KEYS, "scope"]
        assert finding["scope"] == SCOPE_IN
        assert _PLACEHOLDER.findall(user) == []

    @pytest.mark.parametrize("render", RENDERERS)
    def test_the_scope_key_is_the_only_change_to_the_user_prompt(self, render):
        _system, user_on = render(PromptContext(ticket_scope=FakeTicket("AC").scope_block()))
        _system, user_off = render()
        assert user_on.count(SCOPE_LINE) == 1
        assert user_on.replace(SCOPE_LINE, "", 1) == user_off

    @pytest.mark.parametrize("render", RENDERERS)
    def test_the_system_prompt_keeps_exactly_one_ticket_scope_block(self, render):
        system_on, _user = render(_active())
        system_off, _user = render()
        assert system_on == f"{system_off}\n\n{FakeTicket('AC').scope_block()}"
        assert system_on.count("## Ticket scope") == 1
        assert SCOPE_LINE not in system_on

    @pytest.mark.parametrize("render", RENDERERS)
    def test_ticket_text_without_the_scope_ask_adds_no_scope_key(self, render):
        _system, user = render(PromptContext(ticket_context=FakeTicket("AC").prompt_block()))
        assert '"scope"' not in user
        (finding,) = _example(user)["findings"]
        assert list(finding) == EXAMPLE_KEYS

    def test_a_ticket_quoting_the_slot_renders_it_literally(self):
        _system, user = _worker_prompt(_active("### Ticket context\n\nquote {scope_example} here"))
        assert "quote {scope_example} here" in user
        assert user.count(SCOPE_LINE) == 1


class _PromptRecorder:
    """Records every (system, user) prompt and answers with no findings."""

    def __init__(self):
        self.prompts: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        with self._lock:
            self.prompts.append((system, user))
        return InvokeResult(
            text='{"findings": [], "escalations": []}', input_tokens=10, output_tokens=5,
            model="rec-model-1", backend="fake", elapsed_ms=1,
        )


def _live_prompts(state: str) -> list[tuple[str, str]]:
    """Every prompt a real orchestrator run sends, for one ticket state."""
    llm = _PromptRecorder()
    ticket = None if state == "NONE" else FakeTicket(state)
    forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
    orchestrator.orchestrate_review(forge, REF, llm, post=False, ticket=ticket)
    return sorted(llm.prompts)


class TestOutputFormatExampleThroughTheOrchestrator:
    """The real orchestrator and reviewer with the packaged templates."""

    @pytest.mark.parametrize("state", STATES)
    def test_every_prompt_shows_scope_only_while_the_ticket_is_active(self, state):
        prompts = _live_prompts(state)
        assert len(prompts) == 2, "one chunk unit and one sweep"
        active = state in ("NO_AC", "AC")
        for _system, user in prompts:
            assert user.count(SCOPE_LINE) == (1 if active else 0)
            assert ('"scope"' in user) is active
            assert _PLACEHOLDER.findall(user) == []

    def test_an_empty_ticket_sends_the_no_ticket_prompts(self):
        assert _live_prompts("EMPTY") == _live_prompts("NONE")
