"""Issue #18 orchestrator wiring: the per-rule cap in the review, the run record and the JSON output.

``orchestrate_review(..., max_findings_per_rule=)`` (``PRXREF_MAX_FINDINGS_PER_RULE``,
default 2):

- the cap is active only when it is above 0 AND a team rules file is loaded
  (``rules`` or ``scoped_rules``); active, it asks every unit for a ``rule``
  (``reviewer.RULE_REQUEST``) with grouping off, keeps the answer
  (``_enforce_rule``), and runs ``quality.apply_rule_cap`` after the grouping
  pass and before the quality gate, so the severity caps count what it kept;
- the run record's ``rule_counts`` is ``quality.rule_cap_counts`` of the
  pass input, and ``None`` on every exit where the pass did not run;
- one INFO line and one ``rulecap ok`` trace event report the pass;
- a review override without ``{rule_example}`` gets the missing-slot WARNING
  under the cap alone, naming the cap rather than grouping;
- ``--format json`` carries ``rule_counts`` after ``scoped_rules`` and the
  best finding's ``locations`` across files;
- cap 0, or no rules file, is byte-identical to BASE (``TestOffPathMatchesBase``).

No test here uses the ``contract_stubs`` fixture: the ones that need the real
prompts run the real reviewer against a scripted LLM, the rest install their
own ``review_chunk`` / ``review_systemic`` doubles.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import types
from unittest.mock import MagicMock

import pytest

from prxref import cli, orchestrator
from prxref.forges.base import ATTRIBUTION_MARKER, PRRef
from prxref.orchestrator import orchestrate_review
from prxref.quality import GROUPED_INTO_PREFIX, RULE_CAP_PREFIX, rule_cap_counts
from prxref.reviewer import RULE_REQUEST
from prxref.rules import ReviewRules, ScopedRules
from prxref.text_inputs import cap_text
from prxref.trace import get_tracer
from tests.test_orchestrator import REF, FakeForge, _added_file_diff
from tests.test_orchestrator_grouping import (
    APP,
    BASE_GOLDEN,
    MAIN_DIFF,
    MODEL_FINDINGS,
    OTHER,
    RELEASE_BINARY_DIFF,
    TWO_FILE_DIFF,
    _all,
    _events,
    _f,
    _install_doubles,
    _ListHandler,
    _reasons,
    _Recorder,
    _ScriptedLLM,
    _sha,
    _templates,
    _trace_events,
    _units,
    _without_slot,
)
from tests.test_orchestrator_grouping import capture as grouping_capture

TEAM_RULES = (
    "# Team rules\n"
    "\n"
    "- no-bare-except: never catch every exception around data handling.\n"
    "- magic-number: name every data limit.\n"
)
APP_RULES = "# App rules\n\n- no-bare-except: never catch every exception around data in src.\n"

A_PY, B_PY, C_PY, D_PY = "src/a.py", "src/b.py", "src/c.py", "src/d.py"
FOUR_FILE_DIFF = "".join(_added_file_diff(path, 12) for path in (A_PY, B_PY, C_PY, D_PY))

FIVE_RAW = [
    {"file": A_PY, "line": 3, "severity": "error", "confidence": 0.9,
     "title": "Bare except swallows the data parse error",
     "body": "The data parser catches every exception.", "rule": "no-bare-except"},
    {"file": B_PY, "line": 4, "severity": "warning", "confidence": 0.95,
     "title": "Bare except hides the data write failure",
     "body": "The data writer catches every exception.", "rule": "no-bare-except"},
    {"file": C_PY, "line": 5, "severity": "warning", "confidence": 0.8,
     "title": "Bare except around the data cache refresh",
     "body": "The data cache catches every exception.", "rule": "No-Bare-Except"},
    {"file": D_PY, "line": 6, "severity": "warning", "confidence": 0.7,
     "title": "Bare except wraps the data socket close",
     "body": "The data socket catches every exception.", "rule": "no-bare-except"},
    {"file": A_PY, "line": 9, "severity": "warning", "confidence": 0.6,
     "title": "Bare except in the data retry loop",
     "body": "The data retry catches every exception.", "rule": "no-bare-except"},
]
CAP_REASON = f"{RULE_CAP_PREFIX}(max 2): listed at {A_PY}:3"
BEST_LOCATIONS = ((A_PY, 9), (C_PY, 5), (D_PY, 6))
BEST_ALSO_AT = "Also at: `src/a.py:9`, `src/c.py:5`, `src/d.py:6`"


def _rules() -> ReviewRules:
    """An always-on team rules file, as ``load_review_rules`` would return it."""
    return ReviewRules(path="team-rules.md", body=cap_text(TEAM_RULES, 12000), severity_map={})


def _scoped() -> ScopedRules:
    """One path-scoped rules file reaching ``src/**``, as ``load_scoped_rules`` would return it."""
    app = ReviewRules(
        path="rules/app.md", body=cap_text(APP_RULES, 12000), severity_map={}, applies_to=("src/**",),
    )
    return ScopedRules(entries=("rules",), files=(app,), severity_map={})


def _fields(res: dict) -> list[tuple]:
    return [
        (f.file, f.line, f.severity, f.confidence, f.title, f.body, f.drop_reason, f.scope, f.rule, f.locations)
        for f in _all(res)
    ]


def _scripted_run(*, chunk=FIVE_RAW, diff=FOUR_FILE_DIFF, post=True, **kw):
    forge = FakeForge(diff=diff)
    llm = _ScriptedLLM(chunk=chunk)
    res = orchestrate_review(forge, REF, llm, post=post, max_workers=1, **kw)
    return res, forge, llm


class TestDefaultCapEndToEnd:
    """Scripted LLM, real reviewer, real passes: the issue's acceptance run."""

    def test_five_findings_of_one_rule_in_four_files_leave_two_active(self):
        res, _forge, llm = _scripted_run(rules=_rules())
        assert len(llm.prompts) == 2
        assert [(f.file, f.line) for f in res["findings_active"]] == [(A_PY, 3), (B_PY, 4)]
        best = next(f for f in res["findings_active"] if f.file == A_PY)
        assert (best.severity, best.confidence, best.rule) == ("error", 0.9, "no-bare-except")
        assert best.locations == BEST_LOCATIONS
        assert best.body == f"The data parser catches every exception.\n\n{BEST_ALSO_AT}"
        kept = next(f for f in res["findings_active"] if f.file == B_PY)
        assert (kept.severity, kept.confidence, kept.locations) == ("warning", 0.95, ())
        assert "Also at:" not in kept.body
        assert _reasons(res) == [CAP_REASON] * 3
        assert sorted((f.file, f.line) for f in res["findings_dropped"]) == [(A_PY, 9), (C_PY, 5), (D_PY, 6)]
        assert all(f.locations == () for f in res["findings_dropped"])
        assert res["rule_counts"] == [{"rule": "no-bare-except", "kind": "rule", "total": 5, "kept": 2}]
        assert res["verdict"] == "Request-Changes"

    def test_every_unit_is_asked_for_a_rule_with_grouping_off(self, tmp_path):
        trace = tmp_path / "trace.jsonl"
        _res, _forge, llm = _scripted_run(rules=_rules(), trace_file=str(trace))
        assert len(llm.prompts) == 2
        for system, user in llm.prompts:
            assert RULE_REQUEST in system
            assert '"rule": "no-bare-except"' in user
        assert _events(trace, "grouping") == []

    def test_two_inline_comments_and_the_best_carries_also_at(self):
        _res, forge, _llm = _scripted_run(rules=_rules())
        (batch,) = forge.inline_batches
        assert [(c.path, c.line) for c in batch] == [(A_PY, 3), (B_PY, 4)]
        best = batch[0]
        assert BEST_ALSO_AT in best.body
        assert best.body.index("Also at:") < best.body.index(ATTRIBUTION_MARKER)
        assert "Also at:" not in batch[1].body

    def test_the_summary_lists_the_two_kept_findings_only(self):
        _res, forge, _llm = _scripted_run(rules=_rules())
        summary = forge.summaries[0]
        assert "Bare except swallows the data parse error" in summary
        assert "Bare except hides the data write failure" in summary
        for folded in ("data cache refresh", "data socket close", "data retry loop"):
            assert folded not in summary


class TestActivation:
    def test_no_rules_file_is_a_no_op_equal_to_cap_zero(self):
        res, forge, llm = _scripted_run()
        off, off_forge, off_llm = _scripted_run(max_findings_per_rule=0)
        assert all(RULE_REQUEST not in system for system, _user in llm.prompts)
        assert all('"rule"' not in user for _system, user in llm.prompts)
        assert all(f.rule is None for f in _all(res))
        assert res["findings_dropped"] == [] and len(res["findings_active"]) == 5
        assert res["rule_counts"] is None and off["rule_counts"] is None
        assert _fields(res) == _fields(off)
        assert llm.prompts == off_llm.prompts
        assert forge.summaries == off_forge.summaries
        assert [[c.body for c in b] for b in forge.inline_batches] == [
            [c.body for c in b] for b in off_forge.inline_batches
        ]

    def test_an_explicit_cap_without_rules_is_the_base_run(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "_elapsed_ms", lambda t0: 0)
        text, res = grouping_capture("main", max_findings_per_rule=5)
        assert _sha(text) == BASE_GOLDEN["main"]
        assert res["rule_counts"] is None

    def test_scoped_rules_alone_turn_the_cap_on(self):
        res, _forge, llm = _scripted_run(scoped_rules=_scoped())
        assert all(RULE_REQUEST in system for system, _user in llm.prompts)
        assert [(f.file, f.line) for f in res["findings_active"]] == [(A_PY, 3), (B_PY, 4)]
        assert _reasons(res) == [CAP_REASON] * 3
        assert res["rule_counts"] == [{"rule": "no-bare-except", "kind": "rule", "total": 5, "kept": 2}]

    @pytest.mark.parametrize("cap", [0, -1, True, False, "2", 2.0, None])
    def test_a_cap_that_is_not_a_positive_int_is_off(self, cap):
        res, _forge, llm = _scripted_run(rules=_rules(), max_findings_per_rule=cap)
        assert all(RULE_REQUEST not in system for system, _user in llm.prompts)
        assert all(f.rule is None for f in _all(res))
        assert len(res["findings_active"]) == 5
        assert res["rule_counts"] is None

    def test_a_higher_cap_keeps_more(self):
        res, _forge, _llm = _scripted_run(rules=_rules(), max_findings_per_rule=4)
        assert len(res["findings_active"]) == 4
        assert _reasons(res) == [f"{RULE_CAP_PREFIX}(max 4): listed at {A_PY}:3"]
        assert res["rule_counts"] == [{"rule": "no-bare-except", "kind": "rule", "total": 5, "kept": 4}]

    def test_the_prompt_context_carries_the_request_exactly_when_active(self, monkeypatch):
        seen = _install_doubles(monkeypatch)
        orchestrate_review(FakeForge(diff=TWO_FILE_DIFF), REF, MagicMock(), post=False, rules=_rules())
        contexts = seen["chunk"] + seen["sweep"]
        assert len(contexts) == 2
        assert all(c.rule_request == RULE_REQUEST and c.rule_active for c in contexts)
        seen_off = _install_doubles(monkeypatch)
        orchestrate_review(
            FakeForge(diff=TWO_FILE_DIFF), REF, MagicMock(), post=False, rules=_rules(),
            max_findings_per_rule=0,
        )
        assert all(c.rule_request == "" for c in seen_off["chunk"] + seen_off["sweep"])


class TestOverrideWithoutTheRuleSlotCapOnly:
    def _warnings(self, caplog, **kw):
        prompts = _templates(worker=_without_slot("worker"), systemic=_without_slot("systemic"))
        with caplog.at_level(logging.INFO, logger="prxref"):
            _scripted_run(prompts=prompts, rules=_rules(), post=False, **kw)
        return [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "{rule_example}" in r.getMessage()
        ]

    def test_the_cap_alone_warns_once_naming_itself(self, caplog):
        (text,) = self._warnings(caplog)
        assert text.startswith("the per-rule cap is on, but prompt template override(s) ")
        assert "team-prompts/worker.md, team-prompts/systemic.md" in text
        assert "prxref prompts export" in text

    def test_grouping_on_keeps_the_grouping_wording(self, caplog):
        (text,) = self._warnings(caplog, group_findings=True)
        assert text.startswith("finding grouping is on, but ")

    def test_cap_zero_does_not_warn(self, caplog):
        assert self._warnings(caplog, max_findings_per_rule=0) == []


class TestGroupingAndTheCap:
    CHUNK = (
        _f(3, confidence=0.8, title="Unchecked data read", rule="rule-r"),
        _f(5, confidence=0.7, title="Unchecked data write", rule="rule-r"),
        _f(7, confidence=0.7, title="Unchecked data copy", rule="rule-r"),
        _f(2, confidence=0.9, title="Unchecked data parse", rule="rule-r", file=OTHER),
    )

    def _run(self, monkeypatch, **kw):
        _install_doubles(monkeypatch, chunk=self.CHUNK)
        return orchestrate_review(
            FakeForge(diff=TWO_FILE_DIFF), REF, MagicMock(), post=False,
            group_findings=True, rules=_rules(), **kw,
        )

    def test_a_group_counts_once_toward_the_cap(self, monkeypatch):
        res = self._run(monkeypatch)
        assert res["rule_counts"] == [{"rule": "rule-r", "kind": "rule", "total": 2, "kept": 2}]
        assert [(f.file, f.line) for f in res["findings_active"]] == [(APP, 3), (OTHER, 2)]
        assert _reasons(res) == [f"{GROUPED_INTO_PREFIX}{APP}:3"] * 2

    def test_a_folded_representative_hands_its_lines_to_the_best(self, monkeypatch):
        res = self._run(monkeypatch, max_findings_per_rule=1)
        (best,) = res["findings_active"]
        assert (best.file, best.line) == (OTHER, 2)
        assert best.locations == ((APP, 3), (APP, 5), (APP, 7))
        assert best.body.endswith("Also at: `src/app.py:3`, `src/app.py:5`, `src/app.py:7`")
        assert _reasons(res) == [
            f"{GROUPED_INTO_PREFIX}{APP}:3", f"{GROUPED_INTO_PREFIX}{APP}:3",
            f"{RULE_CAP_PREFIX}(max 1): listed at {OTHER}:2",
        ]
        (rep,) = [f for f in res["findings_dropped"] if f.drop_reason.startswith(RULE_CAP_PREFIX)]
        assert (rep.line, rep.locations) == (3, ((APP, 5), (APP, 7)))
        assert res["rule_counts"] == [{"rule": "rule-r", "kind": "rule", "total": 2, "kept": 1}]


class TestTheSeverityCapsCountWhatTheRuleCapKept:
    CHUNK = (
        _f(3, severity="error", confidence=0.95, title="Unchecked data read", rule="rule-a"),
        _f(5, severity="error", confidence=0.8, title="Unchecked data write", rule="rule-a"),
        _f(20, severity="error", confidence=0.9, title="Leaked data handle", rule="rule-b"),
        _f(22, severity="error", confidence=0.85, title="Leaked data socket", rule="rule-b"),
    )

    def _run(self, monkeypatch, **kw):
        _install_doubles(monkeypatch, chunk=self.CHUNK)
        return orchestrate_review(
            FakeForge(diff=_added_file_diff(APP, 40)), REF, MagicMock(), post=False, max_errors=1, **kw,
        )

    def test_the_error_cap_sees_two_errors_and_keeps_one(self, monkeypatch):
        res = self._run(monkeypatch, rules=_rules(), max_findings_per_rule=1)
        (survivor,) = res["findings_active"]
        assert (survivor.line, survivor.rule, survivor.locations) == (3, "rule-a", ((APP, 5),))
        assert _reasons(res) == [
            "error cap exceeded (max 1)",
            f"{RULE_CAP_PREFIX}(max 1): listed at {APP}:20",
            f"{RULE_CAP_PREFIX}(max 1): listed at {APP}:3",
        ]
        (capped,) = [f for f in res["findings_dropped"] if f.drop_reason.startswith("error cap")]
        assert (capped.line, capped.locations) == (20, ((APP, 22),))

    def test_the_control_without_rules_caps_every_line(self, monkeypatch):
        res = self._run(monkeypatch, max_findings_per_rule=1)
        assert [f.line for f in res["findings_active"]] == [3]
        assert _reasons(res) == ["error cap exceeded (max 1)"] * 3


class TestTheSweep:
    def test_a_sweep_restatement_of_a_folded_finding_is_deduped(self, monkeypatch):
        _install_doubles(
            monkeypatch,
            chunk=[
                _f(3, confidence=0.9, title="Unchecked data read", rule="rule-a"),
                _f(5, confidence=0.8, title="Unchecked data write", rule="rule-a"),
                _f(7, confidence=0.7, title="Unchecked data copy", rule="rule-a"),
            ],
            sweep=[_f(7, confidence=0.9, title="Unchecked data copy", body="The sweep saw the data copy.")],
        )
        res = orchestrate_review(
            FakeForge(diff=_added_file_diff(APP, 40)), REF, MagicMock(), post=False, rules=_rules(),
        )
        assert [f.line for f in res["findings_active"]] == [3, 5]
        assert _reasons(res) == ["duplicate of chunk finding", f"{RULE_CAP_PREFIX}(max 2): listed at {APP}:3"]

    def test_sweep_findings_are_never_counted_or_capped(self, monkeypatch):
        sweep = [_f(line, title=f"Unchecked data step {line}", rule="rule-s") for line in (3, 5, 7)]
        _install_doubles(monkeypatch, sweep=sweep)
        res = orchestrate_review(
            FakeForge(diff=_added_file_diff(APP, 40)), REF, MagicMock(), post=False, rules=_rules(),
            max_findings_per_rule=1,
        )
        assert [f.line for f in res["findings_active"]] == [3, 5, 7]
        assert res["findings_dropped"] == []
        assert res["rule_counts"] == []


class TestReportingThePass:
    def _run(self, monkeypatch, tmp_path, caplog, chunk, **kw):
        _install_doubles(monkeypatch, chunk=chunk)
        trace = tmp_path / "trace.jsonl"
        with caplog.at_level(logging.INFO, logger="prxref"):
            res = orchestrate_review(
                FakeForge(diff=_added_file_diff(APP, 40)), REF, MagicMock(), post=False,
                max_workers=1, trace_file=str(trace), **kw,
            )
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("rule cap:")]
        return res, _events(trace, "rulecap"), lines

    def test_one_info_line_and_one_trace_event_with_the_counts(self, monkeypatch, tmp_path, caplog):
        chunk = [
            _f(3, title="Unchecked data read", rule="r"), _f(5, title="Unchecked data write", rule="r"),
            _f(7, title="Unchecked data copy", rule="r"), _f(9, title="Unchecked data move", rule="r"),
            _f(20, title="Leaked data handle", rule="other"), _f(22, title="Leaked data socket", rule="other"),
        ]
        _res, events, lines = self._run(monkeypatch, tmp_path, caplog, chunk, rules=_rules())
        assert [(e["phase"], e["meta"]) for e in events] == [("ok", {"cap": 2, "rules": 1, "folded": 2})]
        assert lines == ["rule cap: folded 2 finding(s) past 2 per rule, across 1 rule(s)"]
        (record,) = [r for r in caplog.records if r.getMessage().startswith("rule cap:")]
        assert record.levelno == logging.INFO and record.name == "prxref"

    def test_zeros_are_reported(self, monkeypatch, tmp_path, caplog):
        res, events, lines = self._run(monkeypatch, tmp_path, caplog, [_f(3)], rules=_rules())
        assert [e["meta"] for e in events] == [{"cap": 2, "rules": 0, "folded": 0}]
        assert lines == ["rule cap: folded 0 finding(s) past 2 per rule, across 0 rule(s)"]
        assert res["rule_counts"] == []

    @pytest.mark.parametrize("kw", [{}, {"rules": "cap0"}], ids=["no-rules", "cap-0"])
    def test_off_neither_appears(self, monkeypatch, tmp_path, caplog, kw):
        if kw:
            kw = {"rules": _rules(), "max_findings_per_rule": 0}
        chunk = [_f(3, rule="r"), _f(5, rule="r"), _f(7, rule="r")]
        res, events, lines = self._run(monkeypatch, tmp_path, caplog, chunk, **kw)
        assert events == [] and lines == []
        assert res["rule_counts"] is None

    def test_the_event_follows_grouping_and_precedes_the_run_end(self, monkeypatch, tmp_path, caplog):
        chunk = [_f(3, rule="r"), _f(5, rule="r")]
        _install_doubles(monkeypatch, chunk=chunk)
        trace = tmp_path / "trace.jsonl"
        orchestrate_review(
            FakeForge(diff=_added_file_diff(APP, 40)), REF, MagicMock(), post=False, max_workers=1,
            trace_file=str(trace), group_findings=True, rules=_rules(),
        )
        nodes = [f"{e['node']} {e['phase']}" for e in _events(trace)]
        assert nodes.index("sweep ok") < nodes.index("grouping ok") < nodes.index("rulecap ok") < nodes.index("run ok")

    def test_the_helper_returns_the_pass_and_the_input_counts(self):
        findings = [_f(3, rule="r"), _f(5, rule="r"), _f(7, rule="r"), _f(9, title="Lone data note")]
        capped, counts = orchestrator._cap_rules(
            findings, cap=1, confidence_floor=None, sweep_start=len(findings), tracer=get_tracer(None),
        )
        assert counts == rule_cap_counts(findings, cap=1, confidence_floor=None, sweep_start=len(findings))
        assert [f.drop_reason for f in capped] == [None, *[f"{RULE_CAP_PREFIX}(max 1): listed at {APP}:3"] * 2, None]
        assert capped[3] is findings[3]


class TestTheRunRecord:
    def test_the_key_follows_scoped_rules(self):
        res, _forge, _llm = _scripted_run(rules=_rules(), post=False)
        keys = list(res)
        assert keys.index("rule_counts") == keys.index("scoped_rules") + 1

    @pytest.mark.parametrize("path", ["get_diff", "empty_diff", "total_failure"])
    def test_an_exit_before_the_pass_carries_null_with_rules_loaded(self, path):
        forge = FakeForge(diff=FOUR_FILE_DIFF)
        llm = _ScriptedLLM(chunk=FIVE_RAW)
        if path == "get_diff":
            forge.fail.add("get_diff")
        elif path == "empty_diff":
            forge.diff = RELEASE_BINARY_DIFF
        else:
            llm = MagicMock()
            llm.invoke.side_effect = RuntimeError("no model")
        res = orchestrate_review(forge, REF, llm, post=False, rules=_rules())
        assert "rule_counts" in res and res["rule_counts"] is None

    def test_the_counts_are_json_native(self):
        res, _forge, _llm = _scripted_run(rules=_rules(), post=False)
        assert json.loads(json.dumps(res["rule_counts"])) == res["rule_counts"]


class TestJsonOutput:
    def test_rule_counts_rides_after_scoped_rules(self):
        res, _forge, _llm = _scripted_run(rules=_rules(), post=False)
        payload = cli._build_json_result(res)
        keys = list(payload)
        assert keys.index("rule_counts") == keys.index("scoped_rules") + 1
        assert keys[keys.index("rule_counts") + 1] == "sampling"
        assert payload["rule_counts"] == [{"rule": "no-bare-except", "kind": "rule", "total": 5, "kept": 2}]

    def test_the_best_row_carries_locations_across_files(self):
        res, _forge, _llm = _scripted_run(rules=_rules(), post=False)
        rows = cli._build_json_result(res)["findings"]
        (best,) = [r for r in rows if (r["file"], r["line"]) == (A_PY, 3)]
        assert best["locations"] == [
            {"file": A_PY, "line": 9}, {"file": C_PY, "line": 5}, {"file": D_PY, "line": 6},
        ]
        assert len({loc["file"] for loc in best["locations"]}) == 3
        assert best["rule"] == "no-bare-except" and best["drop_reason"] is None
        folded = [r for r in rows if r["drop_reason"] is not None]
        assert [r["drop_reason"] for r in folded] == [CAP_REASON] * 3
        assert all(r["locations"] is None for r in folded)
        assert [r["rule"] for r in folded] == ["no-bare-except", "No-Bare-Except", "no-bare-except"]

    def test_off_and_partial_results_emit_null(self):
        res, _forge, _llm = _scripted_run(post=False)
        assert cli._build_json_result(res)["rule_counts"] is None
        assert cli._build_json_result({})["rule_counts"] is None
        assert "rule_counts" in cli._build_json_result(None)


class TestCliWiring:
    REF = PRRef(
        forge="github", host="github.com", owner="acme", repo="widget",
        number=7, url="https://github.com/acme/widget/pull/7",
    )

    def _call(self, monkeypatch):
        calls = []

        def fake_orchestrate(**kwargs):
            calls.append(kwargs)
            return {"verdict": "Approved", "findings_active": [], "findings_dropped": [],
                    "input_tokens": 0, "output_tokens": 0}

        monkeypatch.setitem(
            sys.modules, "prxref.orchestrator",
            types.SimpleNamespace(orchestrate_review=fake_orchestrate),
        )
        monkeypatch.setitem(
            sys.modules, "prxref.llm_backends",
            types.SimpleNamespace(create_llm_client=lambda cfg: MagicMock(name="LLMClient")),
        )
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: self.REF)
        assert cli.main(["review", "--pr-url", self.REF.url, "--no-post"]) == 0
        (call,) = calls
        return call

    def test_the_default_reaches_orchestrate(self, monkeypatch):
        assert self._call(monkeypatch)["max_findings_per_rule"] == 2

    @pytest.mark.parametrize("value, expected", [("0", 0), ("5", 5)])
    def test_the_configured_value_reaches_orchestrate(self, monkeypatch, value, expected):
        monkeypatch.setenv("PRXREF_MAX_FINDINGS_PER_RULE", value)
        assert self._call(monkeypatch)["max_findings_per_rule"] == expected


# ---------------------------------------------------------------------------
# Off-path identity with a rules file loaded. The capture below mirrors
# tests/test_orchestrator_grouping.py's; the goldens are the sha256 of its
# output at BASE a81fb53 (seat R18-C, before any #18 wiring edit), where the
# orchestrator had no max_findings_per_rule parameter. Record and JSON keys are
# projected onto the key sets at that BASE, so the only difference a run at cap
# 0 may show is the new rule_counts key, which the tests pin to null.
# "rules_grouping" was re-derived when the chunk/sweep split after the gate
# stopped swapping twins (0.15, seat R18-D, tests/test_sweep_boundary_drops.py):
# the sweep's copies of the grouped members at src/app.py:9, :15 and :22 were
# posted as second comments and now drop as "duplicate of chunk finding". Only
# those three findings moved: the record and JSON rows, the three inline
# comments, the summary's counts and list, and the post and run trace counts.
# ---------------------------------------------------------------------------

RULES_GOLDEN = {
    "rules": "029d065d6734661346e48bad7acd3c1b9b282350406c957cc27d434ed1324a27",
    "scoped": "65406082cb53caa153bce81ffeecd1e4437935ff12e908bc67dd17293a501c23",
    "rules_grouping": "72733b90eb69c7edc14a8a3393245f44eade9c5f392b506b286461ace6d7dd30",
    "rules_summary_only": "356a9e6c3d91d4ddb6f728c55589c431d39cf0a217de0e6822a8a84770036196",
}

FIELDS = ("file", "line", "severity", "confidence", "title", "body", "drop_reason", "scope", "rule", "locations")
A81_RECORD_KEYS = (
    "verdict", "findings_active", "findings_dropped", "chunk_count", "chunks_reviewed",
    "chunks_failed", "elapsed_ms", "input_tokens", "output_tokens", "posted", "sampling",
    "cost_usd", "cost_estimated", "review_rules", "ticket_context", "spec_grounding",
    "size_advisory", "prompt_templates", "scoped_rules",
)
A81_JSON_KEYS = (
    "verdict", "findings", "chunk_count", "chunks_reviewed", "chunks_failed", "elapsed_ms",
    "input_tokens", "output_tokens", "cost_usd", "cost_estimated", "posted", "review_rules",
    "ticket_context", "spec_grounding", "size_advisory", "prompt_templates", "scoped_rules",
    "sampling",
)

RULES_SCENARIOS = {
    "rules": (MAIN_DIFF, lambda: {"rules": _rules()}),
    "scoped": (MAIN_DIFF, lambda: {"scoped_rules": _scoped()}),
    "rules_grouping": (MAIN_DIFF, lambda: {"rules": _rules(), "group_findings": True}),
    "rules_summary_only": (RELEASE_BINARY_DIFF, lambda: {"rules": _rules()}),
}


def _record(res: dict) -> dict:
    out = {}
    for key in A81_RECORD_KEYS:
        value = res[key]
        if key in ("findings_active", "findings_dropped"):
            out[key] = [{name: getattr(f, name) for name in FIELDS} for f in value]
        else:
            out[key] = value
    return out


def _json_payload(res: dict) -> dict:
    payload = cli._build_json_result(res)
    return {key: payload[key] for key in A81_JSON_KEYS}


def rules_capture(name: str, **knobs) -> tuple[str, dict]:
    diff, extra = RULES_SCENARIOS[name]
    forge = FakeForge(diff=diff)
    llm = _Recorder(json.dumps({"findings": MODEL_FINDINGS}))
    handler = _ListHandler()
    logger = logging.getLogger("prxref")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            trace_file = os.path.join(tmp, "trace.jsonl")
            trace_dir = os.path.join(tmp, "units")
            res = orchestrator.orchestrate_review(
                forge, REF, llm, post=True, max_workers=1,
                trace_file=trace_file, trace_dir=trace_dir, **extra(), **knobs,
            )
            payload = {
                "prompts": [[_sha(s), _sha(u)] for s, u in sorted(llm.prompts)],
                "trace": _trace_events(trace_file),
                "units": _units(trace_dir),
                "record": _record(res),
                "json": _json_payload(res),
                "summaries": list(forge.summaries),
                "inline": [[c.path, c.line, c.body] for batch in forge.inline_batches for c in batch],
                "logs": list(handler.lines),
            }
            text = json.dumps(payload, indent=1, ensure_ascii=True, default=repr)
            return text.replace(tmp, "<tmp>"), res
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


class TestOffPathMatchesBase:
    @pytest.fixture(autouse=True)
    def _pinned_clock(self, monkeypatch):
        monkeypatch.setattr(orchestrator, "_elapsed_ms", lambda t0: 0)

    @pytest.mark.parametrize("name", list(RULES_SCENARIOS))
    def test_cap_zero_with_rules_is_byte_identical_to_base(self, name):
        text, res = rules_capture(name, max_findings_per_rule=0)
        assert _sha(text) == RULES_GOLDEN[name]
        assert set(res) == set(A81_RECORD_KEYS) | {"rule_counts", "repo_context"}
        assert res["rule_counts"] is None
        assert res["repo_context"] is None
        payload = list(cli._build_json_result(res))
        assert payload == [*A81_JSON_KEYS[:-1], "rule_counts", "sampling"]

    @pytest.mark.parametrize("name", ["rules", "scoped", "rules_grouping"])
    def test_the_golden_is_sensitive_to_the_default_cap(self, name):
        text, res = rules_capture(name)
        assert _sha(text) != RULES_GOLDEN[name]
        assert res["rule_counts"] is not None

    def test_the_summary_only_exit_never_runs_the_pass(self):
        text, res = rules_capture("rules_summary_only")
        assert _sha(text) == RULES_GOLDEN["rules_summary_only"]
        assert res["rule_counts"] is None
