"""Spec grounding end to end: the ungrounded relabel, the prompt rules, the hedge call site.

A run is spec-grounded only when its digest holds at least one constraint
line (``specs.constraint_count(digest) > 0``). An ungrounded run injects no
digest, so every prompt shows ``(no specs provided for this review)``, and
any ``spec`` finding the model emits anyway is relabelled ``warning`` right
after the team severity map, before consistency can spread the label. A
grounded run hands the injected digest to the hedge gate, so a condition
inside a real ``Spec: "..."`` quote is the spec's, not the model's hedge.

Every orchestrator test here runs the REAL reviewer (``review_chunk``,
``review_systemic`` and the packaged prompts): this module never requests the
``contract_stubs`` fixture, which would swap the sweep out. A spec source is
a real file under ``tmp_path`` except for the Jira shape, which cannot be
fetched offline and is handed to the orchestrator as fetched.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import replace

import pytest

from prxref import orchestrator, specs
from prxref.llm import InvokeResult
from prxref.orchestrator import orchestrate_review
from prxref.quality import apply_spec_grounding
from prxref.reviewer import load_prompt
from prxref.specs import SpecSource
from prxref.triage import Finding, parse_unified_diff
from tests.test_orchestrator import REF, FakeForge, _added_file_diff

NO_SPECS = "(no specs provided for this review)"

DIFF = _added_file_diff("src/app.py", 20)

SPEC_DOC = (
    "# Session rules\n\n"
    "If a session already exists, the server MUST reuse it.\n\n"
    "Clients MAY still send the legacy header.\n\n"
    'Clients MUST set "mode" to strict if it is still unset.\n\n'
    "If the server is still initializing, the client MUST NOT send requests.\n\n"
    "Tools MUST keep the mcp prefix if they are already registered.\n\n"
    "Tokens MUST NOT be logged.\n"
)

SPEC_DOC_CONSTRAINTS = 6

PLAIN_SPEC_BODY = 'Spec: "Tokens MUST NOT be logged." The diff logs the data token.'

JIRA_ORIGIN = "https://jira.example.invalid/browse/PROJ-7"

JIRA_TEXT = (
    "Summary: Keep data tokens private\n"
    "Type: Story\n\n"
    "Tokens MUST NOT be logged.\n"
    "Sessions MUST be reused.\n"
)


def _finding(
    severity: str = "spec", *, line: int = 3, title: str = "Data token is logged",
    body: str = PLAIN_SPEC_BODY, confidence: float = 0.9,
) -> dict:
    return {
        "file": "src/app.py", "line": line, "severity": severity,
        "confidence": confidence, "title": title, "body": body,
    }


class RoutingLLM:
    """Answers chunk units and the sweep with separate findings; records every prompt."""

    def __init__(self, chunk=(), sweep=()):
        self.chunk = list(chunk)
        self.sweep = list(sweep)
        self.chunk_prompts: list[tuple[str, str]] = []
        self.sweep_prompts: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        is_sweep = "### Digest" in user
        with self._lock:
            (self.sweep_prompts if is_sweep else self.chunk_prompts).append((system, user))
        payload = self.sweep if is_sweep else self.chunk
        return InvokeResult(
            text=json.dumps({"findings": payload, "escalations": []}),
            input_tokens=10, output_tokens=5, model="test-model-1",
            backend="fake", elapsed_ms=1,
        )


def _spec_file(tmp_path, text: str = SPEC_DOC) -> str:
    path = tmp_path / "spec.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _review(llm, *, tmp_path, sources=(), **kw):
    forge = FakeForge(diff=DIFF)
    trace = tmp_path / "run.jsonl"
    res = orchestrate_review(
        forge, REF, llm, spec_sources=list(sources), trace_file=str(trace), **kw,
    )
    events = [json.loads(x) for x in trace.read_text().splitlines() if x.strip()]
    return forge, res, events


def _specs_events(events: list[dict]) -> list[tuple[str, dict]]:
    return [(e["phase"], e.get("meta", {})) for e in events if e["node"] == "specs"]


def _relabel_events(events: list[dict]) -> list[dict]:
    return [meta for phase, meta in _specs_events(events) if phase == "relabel"]


def _all_prompts(llm: RoutingLLM) -> list[tuple[str, str]]:
    return llm.chunk_prompts + llm.sweep_prompts


class TestUngroundedRunsRelabelSpec:
    """No constraint injected: every ``spec`` finding posts as a ``warning``."""

    def test_no_sources_relabels_a_worker_spec_finding(self, tmp_path):
        llm = RoutingLLM(chunk=[_finding()])
        forge, res, events = _review(llm, tmp_path=tmp_path)
        assert [(f.title, f.severity) for f in res["findings_active"]] == [
            ("Data token is logged", "warning"),
        ]
        summary = forge.summaries[0]
        assert "🔍 0 spec" in summary
        assert "🟧 1 warning" in summary
        assert "Spec-grounded" not in summary
        body = forge.inline_batches[0][0].body
        assert body.startswith("🤖 🟧 **[WARNING] Data token is logged**")
        assert all(NO_SPECS in user for _system, user in _all_prompts(llm))
        assert _relabel_events(events) == [{"findings": 1}]

    def test_a_sweep_spec_finding_is_relabelled_too(self, tmp_path):
        llm = RoutingLLM(sweep=[_finding(title="Sweep data token is logged")])
        _forge, res, events = _review(llm, tmp_path=tmp_path, post=False)
        assert len(llm.sweep_prompts) == 1
        assert NO_SPECS in llm.sweep_prompts[0][1]
        assert [(f.title, f.severity) for f in res["findings_active"]] == [
            ("Sweep data token is logged", "warning"),
        ]
        assert _relabel_events(events) == [{"findings": 1}]

    def test_every_source_failed_relabels(self, tmp_path):
        llm = RoutingLLM(chunk=[_finding()])
        forge, res, events = _review(
            llm, tmp_path=tmp_path, sources=[str(tmp_path / "missing.md")],
        )
        assert [f.severity for f in res["findings_active"]] == ["warning"]
        assert all(NO_SPECS in user for _system, user in _all_prompts(llm))
        summary = forge.summaries[0]
        assert "> ⚠️ Spec fetch failed for 1 source(s)" in summary
        assert "🔍 0 spec" in summary
        assert _relabel_events(events) == [{"findings": 1}]

    def test_a_digest_with_no_constraint_line_is_not_injected(self, tmp_path):
        """A budget too small for any unit leaves the intro and the truncation
        marker: a non-empty digest that grounds nothing."""
        path = _spec_file(tmp_path)
        fetched = specs.fetch_specs([path], max_chars=120000)
        digest = specs.build_spec_digest(fetched, parse_unified_diff(DIFF), 1)
        assert digest != ""
        assert specs.TRUNCATION_MARKER in digest
        assert specs.constraint_count(digest) == 0

        llm = RoutingLLM(chunk=[_finding()])
        _forge, res, events = _review(
            llm, tmp_path=tmp_path, sources=[path], spec_digest_tokens=1,
            post=False,
        )
        for _system, user in _all_prompts(llm):
            assert NO_SPECS in user
            assert "Spec constraints ranked for this diff" not in user
            assert specs.TRUNCATION_MARKER not in user
        assert [f.severity for f in res["findings_active"]] == ["warning"]
        assert _relabel_events(events) == [{"findings": 1}]

    @pytest.mark.parametrize("severity", ["SPEC", " Spec "])
    def test_the_label_is_compared_case_insensitively(self, severity, tmp_path):
        llm = RoutingLLM(chunk=[_finding(severity)])
        _forge, res, events = _review(llm, tmp_path=tmp_path, post=False)
        assert [f.severity for f in res["findings_active"]] == ["warning"]
        assert _relabel_events(events) == [{"findings": 1}]

    def test_a_same_title_outofscope_sibling_is_never_raised_to_spec(self, tmp_path):
        """Consistency still raises the sibling to its group's max, which is
        now ``warning``. Exactly one relabel proves the relabel ran BEFORE
        consistency: run after it, consistency would first raise the sibling
        to spec and the relabel would count two."""
        llm = RoutingLLM(chunk=[
            _finding("spec", line=3, title="Shared data pattern"),
            _finding(
                "outofscope", line=9, title="Shared data pattern",
                body="The data pattern repeats here.",
            ),
        ])
        _forge, res, events = _review(llm, tmp_path=tmp_path, post=False)
        active = {f.line: f.severity for f in res["findings_active"]}
        assert active == {3: "warning", 9: "warning"}
        assert _relabel_events(events) == [{"findings": 1}]

    def test_the_grounded_control_raises_that_sibling_to_spec(self, tmp_path):
        llm = RoutingLLM(chunk=[
            _finding("spec", line=3, title="Shared data pattern"),
            _finding(
                "outofscope", line=9, title="Shared data pattern",
                body="The data pattern repeats here.",
            ),
        ])
        _forge, res, events = _review(
            llm, tmp_path=tmp_path, sources=[_spec_file(tmp_path)], post=False,
        )
        active = {f.line: f.severity for f in res["findings_active"]}
        assert active == {3: "spec", 9: "spec"}
        assert _relabel_events(events) == []

    def test_other_severities_are_untouched(self, tmp_path):
        llm = RoutingLLM(chunk=[
            _finding("error", line=3, title="Data loss on retry",
                     body="The retry drops the data batch."),
            _finding("warning", line=5, title="Data lock held too long",
                     body="The data lock spans the network call."),
            _finding("outofscope", line=7, title="Data typo",
                     body="recieve -> receive in data text."),
        ])
        _forge, res, events = _review(llm, tmp_path=tmp_path, post=False)
        active = {f.line: f.severity for f in res["findings_active"]}
        assert active == {3: "error", 5: "warning", 7: "outofscope"}
        assert _specs_events(events) == []

    def test_the_relabel_is_logged_and_traced_once_with_the_count(
        self, tmp_path, caplog,
    ):
        caplog.set_level(logging.INFO, logger="prxref")
        llm = RoutingLLM(
            chunk=[_finding(title="Chunk data token is logged")],
            sweep=[_finding(line=5, title="Sweep data token is logged")],
        )
        _forge, res, events = _review(llm, tmp_path=tmp_path, post=False)
        assert sorted(f.severity for f in res["findings_active"]) == [
            "warning", "warning",
        ]
        assert _specs_events(events) == [("relabel", {"findings": 2})]
        lines = [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.INFO and "relabelled" in r.getMessage()
        ]
        assert lines == [
            "spec grounding: relabelled 2 spec finding(s) as warning "
            "(no spec constraint was injected)"
        ]


class TestGroundedRunsKeepSpec:
    """At least one constraint injected: ``spec`` findings keep their label."""

    def test_a_doc_source_keeps_spec(self, tmp_path):
        llm = RoutingLLM(chunk=[_finding()])
        forge, res, events = _review(
            llm, tmp_path=tmp_path, sources=[_spec_file(tmp_path)],
        )
        assert [f.severity for f in res["findings_active"]] == ["spec"]
        summary = forge.summaries[0]
        assert "🔍 1 spec" in summary
        assert (
            f"> 🔍 Spec-grounded: 1 source(s) · {SPEC_DOC_CONSTRAINTS} "
            "constraint(s) injected" in summary
        )
        for _system, user in _all_prompts(llm):
            assert NO_SPECS not in user
            assert "(MUST) Tokens MUST NOT be logged." in user
        assert _relabel_events(events) == []

    def test_a_jira_only_digest_keeps_spec(self, monkeypatch, tmp_path):
        """Ticket lines carry no strength label and still count as constraints."""
        fetched = [SpecSource(origin=JIRA_ORIGIN, kind="jira", text=JIRA_TEXT, error="")]
        monkeypatch.setattr(orchestrator.specs, "fetch_specs", lambda *a, **k: fetched)
        llm = RoutingLLM(chunk=[_finding()])
        forge, res, events = _review(llm, tmp_path=tmp_path, sources=[JIRA_ORIGIN])
        assert [f.severity for f in res["findings_active"]] == ["spec"]
        assert (
            "> 🔍 Spec-grounded: 1 source(s) · 4 constraint(s) injected"
            in forge.summaries[0]
        )
        for _system, user in _all_prompts(llm):
            assert "[ticket:PROJ-7] Tokens MUST NOT be logged." in user
            assert NO_SPECS not in user
        assert _relabel_events(events) == []

    def test_an_upper_case_label_on_a_grounded_run_stays_spec(self, tmp_path):
        llm = RoutingLLM(chunk=[_finding("SPEC")])
        _forge, res, events = _review(
            llm, tmp_path=tmp_path, sources=[_spec_file(tmp_path)], post=False,
        )
        assert [f.severity for f in res["findings_active"]] == ["spec"]
        assert _relabel_events(events) == []


class TestTheHedgeGateReadsTheInjectedDigest:
    """A condition inside a quote the injected digest holds is the spec's own.

    Each case runs a real file source, so the quote is checked against the
    digest the prompts actually carried; the controls prove the run was
    grounded and that the gate still reads the model's own text.
    """

    KEPT = {
        "conditional": (
            "spec", 'Spec: "If a session already exists, the server MUST reuse '
            'it." The diff opens a new data session.',
        ),
        "may-still": (
            "spec", 'Spec: "Clients MAY still send the legacy header." The diff '
            "rejects it on the data path.",
        ),
        "inner-quote": (
            "spec", 'Spec: "Clients MUST set "mode" to strict if it is still '
            'unset." The diff leaves the data mode lax.',
        ),
        "curly-quote": (
            "spec", "Spec: “If the server is still initializing, the client MUST "
            "NOT send requests.” The diff sends data requests during startup.",
        ),
        "case-differs": (
            "spec", 'Spec: "tools MUST keep the mcp prefix if they are already '
            'registered"; the diff drops it from the data tools.',
        ),
    }

    DROPPED = {
        "hedge-outside-the-quote": (
            'Spec: "Tokens MUST NOT be logged." If the logger is still at debug '
            "level, the data token leaks.",
            'hedged: "If the logger is still"',
        ),
        "fake-quote-not-in-the-digest": (
            'Spec: "If the cache is still warm, reads MUST bypass it." The diff '
            "reads data through it.",
            'hedged: "If the cache is still"',
        ),
    }

    def _grounded_review(self, tmp_path, chunk):
        llm = RoutingLLM(chunk=chunk)
        _forge, res, _events = _review(
            llm, tmp_path=tmp_path, sources=[_spec_file(tmp_path)], post=False,
        )
        assert llm.chunk_prompts
        for _system, user in _all_prompts(llm):
            assert NO_SPECS not in user
            assert "(MUST) If a session already exists, the server MUST reuse it." in user
        return res

    @pytest.mark.parametrize("case", sorted(KEPT))
    def test_a_quoted_spec_condition_keeps_the_finding_active(self, case, tmp_path):
        severity, body = self.KEPT[case]
        res = self._grounded_review(tmp_path, [_finding(severity, body=body)])
        assert res["findings_dropped"] == []
        assert [f.severity for f in res["findings_active"]] == ["spec"]

    def test_a_spec_finding_consistency_raised_to_warning_stays_active(self, tmp_path):
        quoted = self.KEPT["conditional"][1]
        res = self._grounded_review(tmp_path, [
            _finding("spec", line=3, title="Session data reuse broken", body=quoted),
            _finding(
                "warning", line=9, title="Session data reuse broken",
                body="The data session is recreated on every call.",
            ),
        ])
        assert res["findings_dropped"] == []
        active = {f.line: f.severity for f in res["findings_active"]}
        assert active == {3: "warning", 9: "warning"}

    @pytest.mark.parametrize("case", sorted(DROPPED))
    def test_the_controls_still_drop(self, case, tmp_path):
        body, reason = self.DROPPED[case]
        res = self._grounded_review(tmp_path, [_finding(body=body)])
        assert res["findings_active"] == []
        assert [f.drop_reason for f in res["findings_dropped"]] == [reason]

    def test_an_ungrounded_run_reads_every_quote(self, tmp_path):
        """No digest injected, nothing exempt: the same conditional quote the
        grounded run keeps is read as the model's own hedge."""
        llm = RoutingLLM(chunk=[_finding(body=self.KEPT["conditional"][1])])
        _forge, res, _events = _review(llm, tmp_path=tmp_path, post=False)
        assert res["findings_active"] == []
        assert [(f.severity, f.drop_reason) for f in res["findings_dropped"]] == [
            ("warning", 'hedged: "If a session already"'),
        ]


SPEC_RULES_SHARED = (
    "Emit `spec` only for a conflict between the diff and a constraint quoted "
    "in the Spec constraints block — never for a generic best practice not "
    "present in the block.",
    "When the only basis for a finding is a constraint quoted in the Spec "
    "constraints block, its severity is `spec`.",
    "When the block reads `(no specs provided for this review)`, `spec` is not "
    "a legal severity.",
)

BUILT_IN_CLASSES = "This prompt's built-in classes (RLS, secrets, …) are never spec constraints."

ERROR_BULLET = (
    "- `error` — the change will break at runtime or is a real bug: crash, wrong "
    "result, data loss, security hole, broken contract."
)


class TestSpecPromptRules:
    """The spec rules, checked on both prompts the real renderers send."""

    @pytest.fixture
    def systems(self, tmp_path):
        llm = RoutingLLM()
        _review(llm, tmp_path=tmp_path, post=False)
        assert len(llm.chunk_prompts) == 1
        assert len(llm.sweep_prompts) == 1
        return {"worker": llm.chunk_prompts[0][0], "sweep": llm.sweep_prompts[0][0]}

    @pytest.mark.parametrize("unit", ["worker", "sweep"])
    def test_both_prompts_carry_the_spec_rules(self, systems, unit):
        system = systems[unit]
        assert "## Spec-grounded rules" in system
        for sentence in SPEC_RULES_SHARED:
            assert sentence in system
        assert ERROR_BULLET in system

    @pytest.mark.parametrize("unit", ["worker", "sweep"])
    def test_the_rules_follow_the_vocabulary_and_precede_confidence(
        self, systems, unit,
    ):
        system = systems[unit]
        vocab = system.index("## Severity Vocabulary")
        rules = system.index("## Spec-grounded rules")
        override = system.index(SPEC_RULES_SHARED[1])
        confidence = system.index("## Confidence")
        assert vocab < rules < override < confidence

    def test_only_the_sweep_names_its_built_in_classes(self, systems):
        assert BUILT_IN_CLASSES in systems["sweep"]
        assert BUILT_IN_CLASSES not in systems["worker"]
        rules = systems["sweep"].index("## Spec-grounded rules")
        assert systems["sweep"].index(BUILT_IN_CLASSES) > rules

    def test_the_sweep_mission_makes_spec_constraints_conditional(self, systems):
        sweep = systems["sweep"]
        assert "with the whole-diff digest plus any spec constraints in view" in sweep
        assert "plus the spec constraints in view" not in sweep

    def test_the_sweep_cites_digest_lines(self, systems):
        assert "Cite the digest line that violates it" in systems["sweep"]
        assert "Cite the diff line that violates it" in systems["worker"]

    @pytest.mark.parametrize("name", ["worker", "systemic"])
    def test_the_rules_live_in_the_system_half(self, name):
        head, marker, _tail = load_prompt(name).partition("## Review Context")
        assert marker
        for sentence in SPEC_RULES_SHARED:
            assert sentence in head


class TestFeatureOff:
    """No sources and no ``spec`` finding: nothing about the run changes."""

    def test_no_spec_stage_event_and_the_no_specs_text(self, tmp_path):
        llm = RoutingLLM(chunk=[
            _finding("warning", title="Data lock held too long",
                     body="The data lock spans the network call."),
        ])
        forge, res, events = _review(llm, tmp_path=tmp_path)
        assert _specs_events(events) == []
        assert [f.severity for f in res["findings_active"]] == ["warning"]
        assert all(NO_SPECS in user for _system, user in _all_prompts(llm))
        assert "Spec-grounded" not in forge.summaries[0]


def _f(**overrides) -> Finding:
    base = {
        "file": "src/app.py", "line": 3, "severity": "spec", "confidence": 0.9,
        "title": "t", "body": "b",
    }
    base.update(overrides)
    return Finding(**base)


class TestApplySpecGrounding:
    """The pure pass: relabel on ungrounded runs, identity on grounded ones."""

    def test_ungrounded_relabels_spec_to_warning(self):
        out = apply_spec_grounding([_f()], grounded=False)
        assert [f.severity for f in out] == ["warning"]

    @pytest.mark.parametrize("severity", ["SPEC", "Spec", " spec ", "\tSPEC\n"])
    def test_the_comparison_strips_and_ignores_case(self, severity):
        out = apply_spec_grounding([_f(severity=severity)], grounded=False)
        assert out[0].severity == "warning"

    def test_grounded_is_identity(self):
        findings = [_f(), _f(severity="SPEC"), _f(severity="error")]
        out = apply_spec_grounding(findings, grounded=True)
        assert out == findings
        assert out is not findings

    @pytest.mark.parametrize("severity", ["error", "warning", "outofscope", "blocker", ""])
    def test_no_other_severity_is_touched(self, severity):
        f = _f(severity=severity)
        assert apply_spec_grounding([f], grounded=False) == [f]

    def test_a_dropped_finding_passes_through_untouched(self):
        dropped = _f(drop_reason="invalid location: ''")
        out = apply_spec_grounding([dropped], grounded=False)
        assert out[0] is dropped
        assert out[0].severity == "spec"

    def test_order_length_and_every_other_field_are_kept(self):
        findings = [
            _f(title="a", severity="error"),
            _f(title="b", scope="out", confidence=0.7, body="kept body"),
            _f(title="c", severity="outofscope"),
        ]
        out = apply_spec_grounding(findings, grounded=False)
        assert [f.title for f in out] == ["a", "b", "c"]
        assert out[1] == replace(findings[1], severity="warning")
        assert out[0] is findings[0]
        assert out[2] is findings[2]

    def test_the_input_is_not_mutated(self):
        findings = [_f()]
        apply_spec_grounding(findings, grounded=False)
        assert findings[0].severity == "spec"

    def test_empty_input(self):
        assert apply_spec_grounding([], grounded=False) == []
