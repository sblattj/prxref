"""Issue #13 orchestrator wiring: finding grouping, the rule request and the new caps.

``orchestrate_review(..., group_findings=, max_warning_findings=,
max_outofscope_findings=)``:

- grouping on asks every unit for a ``rule`` (``reviewer.RULE_REQUEST``),
  keeps the answer (``_enforce_rule``), and runs
  ``quality.apply_rule_grouping`` after the hedge gate and before the quality
  gate, so every cap counts groups; one INFO line and one ``grouping ok``
  trace event report it;
- the representative's ``Finding.locations`` is exactly its ``Also at:``
  list and survives every later pass;
- both caps reach both ``apply_quality_gate`` sites, the summary-only exit's
  included;
- grouping on with a review override that has no ``{rule_example}`` slot
  logs one WARNING;
- everything off is byte-identical to BASE (``TestOffPathMatchesBase``).

No test here uses the ``contract_stubs`` fixture: the ones that need the real
prompts run the real reviewer against a scripted LLM, the rest install their
own ``review_chunk`` / ``review_systemic`` doubles.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import sys
import tempfile
import threading
import types
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from prxref import cli, orchestrator
from prxref.eval_metrics import credit_locations
from prxref.forges.base import ATTRIBUTION_MARKER, PRRef
from prxref.llm import InvokeResult
from prxref.orchestrator import _enforce_rule, _origin_key, _summary_only_run, orchestrate_review
from prxref.prompt_templates import CONTEXT_MARKER, PromptTemplates, TemplateFile
from prxref.quality import (
    CONTAINMENT_NOTE_SUFFIX,
    GROUPED_INTO_PREFIX,
    apply_containment_note,
    apply_quality_gate,
    apply_rule_grouping,
    apply_sweep_dedup,
    finding_rank_key,
    finding_sort_key,
)
from prxref.reviewer import RULE_REQUEST, load_prompt
from prxref.triage import Finding, parse_unified_diff
from tests.test_orchestrator import REF, FakeForge, _added_file_diff, make_pr

APP = "src/app.py"
OTHER = "src/other.py"
ONE_FILE_DIFF = _added_file_diff(APP, 40)
TWO_FILE_DIFF = _added_file_diff(APP, 40) + _added_file_diff(OTHER, 10)
RULE_HEADING = "## Rule names"
ALSO_AT_RE = re.compile(r"Also at: (.*)", re.DOTALL)
LOCATION_RE = re.compile(r"`([^`]+):(\d+)`")


def _binary(path: str) -> str:
    return f"diff --git a/{path} b/{path}\nindex 111..222 100644\nBinary files a/{path} and b/{path} differ\n"


RELEASE_BINARY_DIFF = "".join(_binary(p) for p in (
    "package.json", "pyproject.toml", "Cargo.toml", "setup.py", "setup.cfg",
    "VERSION", "CHANGELOG.md", "poetry.lock", "Cargo.lock", "src/logo.png",
))


def _f(line, *, severity="warning", confidence=0.9, title=None, rule=None, file=APP, body=None, **kw):
    return Finding(
        file=file, line=line, severity=severity, confidence=confidence,
        title=title if title is not None else f"Data check {line}",
        body=body if body is not None else f"The data on line {line} is unchecked.",
        rule=rule, **kw,
    )


def _meta(model="double-model"):
    return {"input_tokens": 10, "output_tokens": 5, "model": model, "elapsed_ms": 1, "error": ""}


def _install_doubles(monkeypatch, chunk=(), sweep=()):
    """Replace both review units; returns the prompt contexts each one saw."""
    seen: dict[str, list] = {"chunk": [], "sweep": []}

    def _review_chunk(llm, files, **kwargs):
        seen["chunk"].append(kwargs.get("prompt_context"))
        return [replace(f) for f in chunk], _meta()

    def _review_systemic(llm, digest, **kwargs):
        seen["sweep"].append(kwargs.get("prompt_context"))
        return [replace(f) for f in sweep], _meta("sweep-model")

    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _review_chunk)
    monkeypatch.setattr(orchestrator.reviewer, "review_systemic", _review_systemic)
    return seen


class _ScriptedLLM:
    """Answers chunk units with ``chunk`` and the sweep with ``sweep``; records every prompt."""

    def __init__(self, chunk=(), sweep=()):
        self.chunk_text = json.dumps({"findings": list(chunk)})
        self.sweep_text = json.dumps({"findings": list(sweep)})
        self.prompts: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        with self._lock:
            self.prompts.append((system, user))
        text = self.sweep_text if "systemic sweep" in system else self.chunk_text
        return InvokeResult(
            text=text, input_tokens=10, output_tokens=5,
            model="scripted-model-1", backend="fake", elapsed_ms=1,
        )


def _all(res):
    return [*res["findings_active"], *res["findings_dropped"]]


def _reasons(res):
    return sorted(f.drop_reason for f in res["findings_dropped"])


def _events(path, node=None):
    with open(path, encoding="utf-8") as fh:
        events = [json.loads(line) for line in fh]
    return [e for e in events if node is None or e["node"] == node]


def _also_at(body: str) -> tuple[tuple[str, int], ...]:
    match = ALSO_AT_RE.search(body or "")
    if match is None:
        return ()
    return tuple((path, int(line)) for path, line in LOCATION_RE.findall(match.group(1)))


BARE_EXCEPT_RAW = [
    {"file": APP, "line": 3, "severity": "warning", "confidence": 0.9,
     "title": "Bare except swallows data errors", "body": "The data loader catches every exception.",
     "rule": "no-bare-except"},
    {"file": APP, "line": 9, "severity": "error", "confidence": 0.8,
     "title": "Bare except hides data corruption", "body": "Another bare except wraps the data parsing.",
     "rule": "no-bare-except"},
    {"file": APP, "line": 15, "severity": "warning", "confidence": 0.7,
     "title": "Bare except in the data retry", "body": "A third bare except wraps the data retry.",
     "rule": "No-Bare-Except"},
]


class TestGroupingOnEndToEnd:
    """Scripted LLM, real reviewer, real passes: the map-13 T6 acceptance run."""

    def _run(self, *, group_findings=True, post=True, **kw):
        forge = FakeForge(diff=ONE_FILE_DIFF)
        llm = _ScriptedLLM(chunk=BARE_EXCEPT_RAW)
        res = orchestrate_review(
            forge, REF, llm, post=post, max_workers=1, group_findings=group_findings, **kw,
        )
        return res, forge, llm

    def test_three_same_rule_findings_fold_into_one_active_finding(self):
        res, _forge, _llm = self._run()
        (rep,) = res["findings_active"]
        assert (rep.file, rep.line, rep.rule) == (APP, 3, "no-bare-except")
        assert rep.severity == "error" and rep.confidence == 0.9
        assert rep.locations == ((APP, 9), (APP, 15))
        assert rep.body.endswith("Also at: `src/app.py:9`, `src/app.py:15`")
        assert _reasons(res) == [f"{GROUPED_INTO_PREFIX}{APP}:3"] * 2
        assert sorted(f.line for f in res["findings_dropped"]) == [9, 15]
        assert all(f.locations == () for f in res["findings_dropped"])
        assert res["verdict"] == "Request-Changes"

    def test_one_inline_comment_carries_also_at_with_the_footer_intact(self):
        _res, forge, _llm = self._run()
        (batch,) = forge.inline_batches
        (comment,) = batch
        assert (comment.path, comment.line) == (APP, 3)
        assert "Also at: `src/app.py:9`, `src/app.py:15`" in comment.body
        assert comment.body.index("Also at:") < comment.body.index(ATTRIBUTION_MARKER)
        assert comment.body.rstrip().endswith("model=scripted-model-1*")

    def test_the_summary_lists_one_finding(self):
        _res, forge, _llm = self._run()
        (summary,) = forge.summaries
        assert "Bare except swallows data errors" in summary
        assert "Bare except hides data corruption" not in summary

    def test_grouping_off_keeps_three_findings_and_no_rule(self):
        res, forge, _llm = self._run(group_findings=False)
        assert len(res["findings_active"]) == 3
        assert all(f.rule is None and f.locations == () for f in _all(res))
        assert all("Also at:" not in f.body for f in _all(res))
        assert len(forge.inline_batches[0]) == 3

    def test_the_credited_locations_match_for_eval(self):
        res, _forge, _llm = self._run()
        (rep,) = res["findings_active"]
        assert credit_locations(rep) == [(APP, 3), (APP, 9), (APP, 15)]


class TestTheRuleRequest:
    def test_every_unit_is_asked_for_a_rule_only_when_grouping_is_on(self):
        llm = _ScriptedLLM(chunk=BARE_EXCEPT_RAW)
        orchestrate_review(FakeForge(diff=TWO_FILE_DIFF), REF, llm, post=False, group_findings=True)
        assert len(llm.prompts) == 2
        for system, user in llm.prompts:
            assert RULE_REQUEST in system
            assert system.count(RULE_HEADING) == 1
            assert '"rule": "no-bare-except"' in user

    def test_off_no_unit_is_asked(self):
        llm = _ScriptedLLM(chunk=BARE_EXCEPT_RAW)
        orchestrate_review(FakeForge(diff=TWO_FILE_DIFF), REF, llm, post=False)
        assert len(llm.prompts) == 2
        for system, user in llm.prompts:
            assert RULE_HEADING not in system
            assert '"rule"' not in user

    @pytest.mark.parametrize("on", [True, False])
    def test_the_prompt_context_carries_the_request_exactly_when_on(self, monkeypatch, on):
        seen = _install_doubles(monkeypatch)
        orchestrate_review(FakeForge(diff=ONE_FILE_DIFF), REF, MagicMock(), post=False, group_findings=on)
        contexts = seen["chunk"] + seen["sweep"]
        assert len(contexts) == 2
        assert all(c.rule_request == (RULE_REQUEST if on else "") for c in contexts)
        assert all(c.rule_active is on for c in contexts)


class TestEnforceRule:
    def test_off_a_model_supplied_rule_is_reset_to_none(self, monkeypatch):
        _install_doubles(monkeypatch, chunk=[_f(3, rule="no-print")], sweep=[_f(20, rule="no-print")])
        res = orchestrate_review(FakeForge(diff=ONE_FILE_DIFF), REF, MagicMock(), post=False)
        assert [f.rule for f in _all(res)] == [None, None]

    def test_on_a_rule_is_normalized_and_kept(self, monkeypatch):
        _install_doubles(monkeypatch, chunk=[_f(3, rule="  no   print ")])
        res = orchestrate_review(
            FakeForge(diff=ONE_FILE_DIFF), REF, MagicMock(), post=False, group_findings=True,
        )
        assert [f.rule for f in _all(res)] == ["no print"]

    def test_on_an_unusable_rule_becomes_none(self, monkeypatch):
        _install_doubles(monkeypatch, chunk=[_f(3, rule="no\N{RIGHT-TO-LEFT OVERRIDE}print")])
        res = orchestrate_review(
            FakeForge(diff=ONE_FILE_DIFF), REF, MagicMock(), post=False, group_findings=True,
        )
        assert [f.rule for f in _all(res)] == [None]

    def test_the_helper_leaves_an_unchanged_finding_as_the_same_object(self):
        f = _f(3)
        assert _enforce_rule([f], False)[0] is f
        assert _enforce_rule([f], True)[0] is f


def _six_errors():
    """Two rules, three errors each; rule-a's anchor is the most confident."""
    return [
        _f(3, severity="error", confidence=0.95, title="Unchecked data read", rule="rule-a"),
        _f(5, severity="error", confidence=0.8, title="Unchecked data write", rule="rule-a"),
        _f(7, severity="error", confidence=0.7, title="Unchecked data copy", rule="rule-a"),
        _f(20, severity="error", confidence=0.9, title="Leaked data handle", rule="rule-b"),
        _f(22, severity="error", confidence=0.85, title="Leaked data socket", rule="rule-b"),
        _f(24, severity="error", confidence=0.75, title="Leaked data file", rule="rule-b"),
    ]


class TestCapsCountGroups:
    def test_the_error_cap_counts_groups_not_lines(self, monkeypatch):
        _install_doubles(monkeypatch, chunk=_six_errors())
        res = orchestrate_review(
            FakeForge(diff=ONE_FILE_DIFF), REF, MagicMock(), post=False,
            group_findings=True, max_errors=1,
        )
        (survivor,) = res["findings_active"]
        assert (survivor.line, survivor.severity, survivor.rule) == (3, "error", "rule-a")
        assert survivor.locations == ((APP, 5), (APP, 7))
        assert _reasons(res) == [
            "error cap exceeded (max 1)",
            f"{GROUPED_INTO_PREFIX}{APP}:20", f"{GROUPED_INTO_PREFIX}{APP}:20",
            f"{GROUPED_INTO_PREFIX}{APP}:3", f"{GROUPED_INTO_PREFIX}{APP}:3",
        ]
        (capped,) = [f for f in res["findings_dropped"] if f.drop_reason.startswith("error cap")]
        assert capped.line == 20 and capped.locations == ((APP, 22), (APP, 24))

    def test_the_control_without_grouping_caps_lines(self, monkeypatch):
        _install_doubles(monkeypatch, chunk=_six_errors())
        res = orchestrate_review(
            FakeForge(diff=ONE_FILE_DIFF), REF, MagicMock(), post=False, max_errors=1,
        )
        assert [f.line for f in res["findings_active"]] == [3]
        assert _reasons(res) == ["error cap exceeded (max 1)"] * 5

    def test_the_warning_cap_counts_a_group_once(self, monkeypatch):
        chunk = [
            _f(3, confidence=0.9, title="Unchecked data read", rule="rule-w"),
            _f(5, confidence=0.7, title="Unchecked data write", rule="rule-w"),
            _f(7, confidence=0.7, title="Unchecked data copy", rule="rule-w"),
            _f(30, confidence=0.8, title="Stale data cache"),
        ]
        _install_doubles(monkeypatch, chunk=chunk)
        res = orchestrate_review(
            FakeForge(diff=ONE_FILE_DIFF), REF, MagicMock(), post=False,
            group_findings=True, max_warning_findings=1,
        )
        assert [(f.line, f.locations) for f in res["findings_active"]] == [(3, ((APP, 5), (APP, 7)))]
        assert _reasons(res) == [
            f"{GROUPED_INTO_PREFIX}{APP}:3", f"{GROUPED_INTO_PREFIX}{APP}:3",
            "warning cap exceeded (max 1)",
        ]


class TestCapsReachTheMainGate:
    CHUNK = (
        _f(3, confidence=0.9, title="Unchecked data read"),
        _f(5, confidence=0.8, title="Unchecked data write"),
        _f(7, confidence=0.7, title="Unchecked data copy"),
        _f(10, severity="outofscope", confidence=0.9, title="Data typo"),
        _f(12, severity="outofscope", confidence=0.8, title="Data spacing"),
        _f(14, severity="error", confidence=0.9, title="Data loss"),
    )

    def _run(self, monkeypatch, **kw):
        _install_doubles(monkeypatch, chunk=self.CHUNK)
        return orchestrate_review(FakeForge(diff=ONE_FILE_DIFF), REF, MagicMock(), post=False, **kw)

    def test_the_warning_cap(self, monkeypatch):
        res = self._run(monkeypatch, max_warning_findings=1)
        assert [f.line for f in res["findings_active"] if f.severity == "warning"] == [3]
        assert _reasons(res) == ["warning cap exceeded (max 1)"] * 2

    def test_the_outofscope_cap(self, monkeypatch):
        res = self._run(monkeypatch, max_outofscope_findings=0)
        assert [f.severity for f in res["findings_active"]].count("outofscope") == 0
        assert _reasons(res) == ["outofscope cap exceeded (max 0)"] * 2

    def test_none_is_unlimited(self, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_WARNING_FINDINGS", "0")
        monkeypatch.setenv("PRXREF_MAX_OUTOFSCOPE_FINDINGS", "0")
        res = self._run(monkeypatch)
        assert len(res["findings_active"]) == len(self.CHUNK)
        assert res["findings_dropped"] == []


class TestCapsReachTheSummaryOnlyGate:
    OUTOFSCOPE = Finding("src/logo.png", 0, "outofscope", 1.0, "Logo data note", "The data in the logo.")

    def test_the_warning_cap_drops_the_release_shape_finding(self):
        res = orchestrate_review(
            FakeForge(diff=RELEASE_BINARY_DIFF), REF, MagicMock(), post=False, max_warning_findings=0,
        )
        assert res["chunk_count"] == 0
        assert res["findings_active"] == []
        assert _reasons(res) == ["warning cap exceeded (max 0)"]

    def test_the_control_keeps_it(self):
        res = orchestrate_review(FakeForge(diff=RELEASE_BINARY_DIFF), REF, MagicMock(), post=False)
        assert [f.severity for f in res["findings_active"]] == ["warning"]

    def test_the_outofscope_cap(self, monkeypatch):
        monkeypatch.setattr(
            orchestrator.heuristics, "release_shape_findings", lambda files: [replace(self.OUTOFSCOPE)],
        )
        res = orchestrate_review(
            FakeForge(diff=RELEASE_BINARY_DIFF), REF, MagicMock(), post=False, max_outofscope_findings=0,
        )
        assert res["findings_active"] == []
        assert _reasons(res) == ["outofscope cap exceeded (max 0)"]

    def test_the_helper_takes_both_caps(self):
        files = parse_unified_diff(RELEASE_BINARY_DIFF)
        release = [
            Finding("src/logo.png", 0, "warning", 1.0, "Release data shape", "The data is release shaped."),
            replace(self.OUTOFSCOPE),
        ]
        res = _summary_only_run(
            FakeForge(diff=RELEASE_BINARY_DIFF), REF, make_pr(), files, False, 0.0,
            release_shape_findings=release, max_warning_findings=0, max_outofscope_findings=0,
        )
        assert res["findings_active"] == []
        assert _reasons(res) == ["outofscope cap exceeded (max 0)", "warning cap exceeded (max 0)"]

    def test_no_grouping_event_on_the_summary_only_path(self, tmp_path):
        trace = tmp_path / "trace.jsonl"
        orchestrate_review(
            FakeForge(diff=RELEASE_BINARY_DIFF), REF, MagicMock(), post=False,
            group_findings=True, trace_file=str(trace),
        )
        assert _events(trace, "grouping") == []


class TestSweepIsNeverGrouped:
    def test_sweep_findings_under_one_rule_stay_apart(self, monkeypatch):
        sweep = [
            _f(3, title="Unchecked data read", rule="rule-s"),
            _f(5, title="Unchecked data write", rule="rule-s"),
            _f(7, title="Unchecked data copy", rule="rule-s"),
        ]
        _install_doubles(monkeypatch, sweep=sweep)
        res = orchestrate_review(
            FakeForge(diff=ONE_FILE_DIFF), REF, MagicMock(), post=False, group_findings=True,
        )
        assert [f.line for f in res["findings_active"]] == [3, 5, 7]
        assert res["findings_dropped"] == []
        assert all(f.locations == () and "Also at:" not in f.body for f in res["findings_active"])

    def test_a_chunk_finding_never_groups_with_a_sweep_one(self, monkeypatch):
        _install_doubles(
            monkeypatch,
            chunk=[_f(3, title="Unchecked data read", rule="rule-s")],
            sweep=[_f(5, title="Unchecked data write", rule="rule-s")],
        )
        res = orchestrate_review(
            FakeForge(diff=ONE_FILE_DIFF), REF, MagicMock(), post=False, group_findings=True,
        )
        assert [f.line for f in res["findings_active"]] == [3, 5]
        assert res["findings_dropped"] == []

    def test_a_sweep_restatement_of_a_grouped_member_is_still_deduped(self, monkeypatch):
        _install_doubles(
            monkeypatch,
            chunk=[
                _f(3, title="Unchecked data read", rule="rule-a"),
                _f(9, title="Unchecked data write", rule="rule-a"),
            ],
            sweep=[_f(9, title="Unchecked data write", body="The sweep saw the data write.")],
        )
        res = orchestrate_review(
            FakeForge(diff=ONE_FILE_DIFF), REF, MagicMock(), post=False, group_findings=True,
        )
        assert [f.line for f in res["findings_active"]] == [3]
        assert _reasons(res) == ["duplicate of chunk finding", f"{GROUPED_INTO_PREFIX}{APP}:3"]


class TestReportingTheGroupingPass:
    def _run(self, monkeypatch, tmp_path, caplog, chunk, **kw):
        _install_doubles(monkeypatch, chunk=chunk)
        trace = tmp_path / "trace.jsonl"
        with caplog.at_level(logging.INFO, logger="prxref"):
            orchestrate_review(
                FakeForge(diff=ONE_FILE_DIFF), REF, MagicMock(), post=False,
                trace_file=str(trace), **kw,
            )
        lines = [r.getMessage() for r in caplog.records if "finding grouping" in r.getMessage()]
        return _events(trace, "grouping"), lines

    def test_on_one_info_line_and_one_trace_event_with_the_counts(self, monkeypatch, tmp_path, caplog):
        chunk = [_f(3, rule="r"), _f(5, rule="r"), _f(7, rule="r"), _f(30, rule="other")]
        events, lines = self._run(monkeypatch, tmp_path, caplog, chunk, group_findings=True)
        assert [(e["phase"], e["meta"]) for e in events] == [("ok", {"groups": 1, "members": 2})]
        assert lines == ["finding grouping: formed 1 group(s), folding 2 finding(s) into them"]
        (record,) = [r for r in caplog.records if "finding grouping" in r.getMessage()]
        assert record.levelno == logging.INFO and record.name == "prxref"

    def test_on_with_no_group_still_reports_zeros(self, monkeypatch, tmp_path, caplog):
        events, lines = self._run(monkeypatch, tmp_path, caplog, [_f(3)], group_findings=True)
        assert [e["meta"] for e in events] == [{"groups": 0, "members": 0}]
        assert lines == ["finding grouping: formed 0 group(s), folding 0 finding(s) into them"]

    def test_two_groups_anchored_on_one_line_count_as_two(self, monkeypatch, tmp_path, caplog):
        chunk = [
            _f(10, title="Unchecked data read", rule="rule-a"),
            _f(12, title="Unchecked data write", rule="rule-a"),
            _f(10, title="Leaked data handle", rule="rule-b"),
            _f(14, title="Leaked data socket", rule="rule-b"),
        ]
        events, _lines = self._run(monkeypatch, tmp_path, caplog, chunk, group_findings=True)
        assert [e["meta"] for e in events] == [{"groups": 2, "members": 2}]

    def test_off_neither_appears(self, monkeypatch, tmp_path, caplog):
        chunk = [_f(3, rule="r"), _f(5, rule="r")]
        events, lines = self._run(monkeypatch, tmp_path, caplog, chunk)
        assert events == [] and lines == []

    def test_the_event_sits_between_the_units_and_the_run_end(self, monkeypatch, tmp_path, caplog):
        _install_doubles(monkeypatch, chunk=[_f(3, rule="r"), _f(5, rule="r")])
        trace = tmp_path / "trace.jsonl"
        orchestrate_review(
            FakeForge(diff=ONE_FILE_DIFF), REF, MagicMock(), post=False, max_workers=1,
            trace_file=str(trace), group_findings=True,
        )
        nodes = [f"{e['node']} {e['phase']}" for e in _events(trace)]
        assert nodes.index("sweep ok") < nodes.index("grouping ok") < nodes.index("run ok")


def _template_file(name: str, text: str) -> TemplateFile:
    return TemplateFile(
        name=name, path=f"team-prompts/{name}.md", text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), chars=len(text),
    )


def _templates(**texts: str) -> PromptTemplates:
    files = tuple(_template_file(name, texts[name]) for name in ("worker", "systemic", "summary") if name in texts)
    return PromptTemplates(
        dir="team-prompts",
        worker=texts.get("worker", load_prompt("worker.md")),
        systemic=texts.get("systemic", load_prompt("systemic.md")),
        summary=texts.get("summary", load_prompt("summary.md")),
        overrides=files,
    )


def _without_slot(name: str) -> str:
    head, marker, tail = load_prompt(f"{name}.md").partition(CONTEXT_MARKER)
    return "TEAM OVERRIDE: style-guide findings are welcome.\n\n" + head + marker + tail.replace("{rule_example}", "")


def _slot_above_marker(name: str) -> str:
    head, marker, tail = load_prompt(f"{name}.md").partition(CONTEXT_MARKER)
    return "{rule_example}\n\n" + head + marker + tail.replace("{rule_example}", "")


class TestOverrideWithoutTheRuleSlot:
    def _warnings(self, caplog, prompts, *, group_findings):
        llm = _ScriptedLLM(chunk=BARE_EXCEPT_RAW)
        with caplog.at_level(logging.INFO, logger="prxref"):
            orchestrate_review(
                FakeForge(diff=ONE_FILE_DIFF), REF, llm, post=False,
                prompts=prompts, group_findings=group_findings,
            )
        warnings = [
            r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "{rule_example}" in r.getMessage()
        ]
        return warnings, llm

    def test_on_one_warning_names_both_files_and_the_export_command(self, caplog):
        prompts = _templates(worker=_without_slot("worker"), systemic=_without_slot("systemic"))
        warnings, _llm = self._warnings(caplog, prompts, group_findings=True)
        assert len(warnings) == 1
        (text,) = warnings
        assert "team-prompts/worker.md, team-prompts/systemic.md" in text
        assert "prxref prompts export" in text and "finding grouping is on" in text

    def test_the_override_still_gets_the_request_without_the_example_key(self, caplog):
        prompts = _templates(worker=_without_slot("worker"), systemic=_without_slot("systemic"))
        _warnings, llm = self._warnings(caplog, prompts, group_findings=True)
        assert len(llm.prompts) == 2
        for system, user in llm.prompts:
            assert system.startswith("TEAM OVERRIDE") and RULE_REQUEST in system
            assert '"rule"' not in user

    def test_only_the_file_missing_the_slot_is_named(self, caplog):
        prompts = _templates(worker=load_prompt("worker.md"), systemic=_without_slot("systemic"))
        (text,), _llm = self._warnings(caplog, prompts, group_findings=True)
        assert "team-prompts/systemic.md" in text and "worker.md" not in text

    def test_a_slot_above_the_marker_does_not_count(self, caplog):
        prompts = _templates(worker=_slot_above_marker("worker"))
        (text,), _llm = self._warnings(caplog, prompts, group_findings=True)
        assert "team-prompts/worker.md" in text

    def test_overrides_with_the_slot_do_not_warn(self, caplog):
        prompts = _templates(worker=load_prompt("worker.md"), systemic=load_prompt("systemic.md"))
        warnings, _llm = self._warnings(caplog, prompts, group_findings=True)
        assert warnings == []

    def test_a_summary_only_override_does_not_warn(self, caplog):
        prompts = _templates(summary=load_prompt("summary.md"))
        warnings, _llm = self._warnings(caplog, prompts, group_findings=True)
        assert warnings == []

    def test_off_nothing_warns(self, caplog):
        prompts = _templates(worker=_without_slot("worker"), systemic=_without_slot("systemic"))
        warnings, _llm = self._warnings(caplog, prompts, group_findings=False)
        assert warnings == []


class TestLocations:
    def test_it_is_the_trailing_field_and_defaults_to_empty(self):
        f = _f(3)
        assert f.locations == ()
        assert Finding("a.py", 1, "error", 0.9, "t", "b").locations == ()

    def test_it_is_not_part_of_any_identity_or_ordering_key(self):
        f = _f(3, rule="r")
        g = replace(f, locations=((APP, 9),))
        assert _origin_key(g) == _origin_key(f)
        assert finding_sort_key(g) == finding_sort_key(f)
        assert finding_rank_key(g) == finding_rank_key(f)

    def test_it_survives_the_gate_the_sweep_dedup_and_the_containment_note(self):
        rep = _f(3, severity="Error", title="Data parser throws on empty input",
                 body="The data parser throws.", locations=((APP, 9), (APP, 15)))
        (gated,) = apply_quality_gate([rep], max_errors=5)
        assert gated.severity == "error" and gated.locations == rep.locations
        (deduped,) = apply_sweep_dedup([gated], sweep_start=1)
        assert deduped.locations == rep.locations
        (noted,) = apply_containment_note([deduped])
        assert noted.body.endswith(CONTAINMENT_NOTE_SUFFIX)
        assert noted.locations == rep.locations
        capped = apply_quality_gate([replace(rep, severity="warning")], max_warning_findings=0)
        assert capped[0].drop_reason == "warning cap exceeded (max 0)"
        assert capped[0].locations == rep.locations

    def test_it_survives_the_whole_pipeline_with_a_containment_note(self, monkeypatch):
        chunk = [
            _f(3, severity="error", title="Data parser throws on empty input",
               body="The data parser throws.", rule="no-throw"),
            _f(9, severity="error", title="Data writer throws on a full disk",
               body="The data writer throws.", rule="no-throw"),
        ]
        _install_doubles(monkeypatch, chunk=chunk)
        res = orchestrate_review(
            FakeForge(diff=ONE_FILE_DIFF), REF, MagicMock(), post=False, group_findings=True,
        )
        (rep,) = res["findings_active"]
        assert rep.locations == ((APP, 9),)
        assert rep.body.endswith(CONTAINMENT_NOTE_SUFFIX)
        assert _also_at(rep.body) == rep.locations

    def _random_findings(self, rng: random.Random) -> list[Finding]:
        out = []
        for _ in range(rng.randint(0, 12)):
            out.append(Finding(
                file=rng.choice([APP, OTHER, "lib/a:b.py"]),
                line=rng.choice([0, 1, 2, 3, 3, 5, 8, 13, None]),
                severity=rng.choice(["error", "warning", "spec", "outofscope", "Warning ", "bogus"]),
                confidence=rng.choice([0.3, 0.6, 0.7, 0.9, 1.0]),
                title=rng.choice(["Data leak", "data  LEAK", "Stale data", ""]),
                body=rng.choice(["The data leaks.", "", "   ", "Data.\n\n"]),
                drop_reason=rng.choice([None, None, None, "hedged: \"x\""]),
                rule=rng.choice([None, "rule-a", "RULE-A ", "rule-b", ""]),
            ))
        return out

    def test_it_always_agrees_with_the_also_at_list(self):
        rng = random.Random(1313)
        folded = 0
        for _ in range(2000):
            findings = self._random_findings(rng)
            sweep_start = rng.randint(-1, len(findings) + 1)
            result = apply_rule_grouping(findings, confidence_floor=0.6, sweep_start=sweep_start)
            for before, after in zip(findings, result, strict=True):
                assert after.locations == _also_at(after.body)
                if after is before:
                    assert after.locations == ()
                if after.locations:
                    folded += 1
                    assert after.drop_reason is None
                    assert all(file == after.file for file, _line in after.locations)
                    assert list(after.locations) == sorted(set(after.locations))
                    assert (after.file, after.line) not in after.locations
        assert folded >= 100, "the generator produced too few groups to prove anything"


class TestCliWiring:
    REF = PRRef(
        forge="github", host="github.com", owner="acme", repo="widget",
        number=7, url="https://github.com/acme/widget/pull/7",
    )

    def _calls(self, monkeypatch):
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
        assert len(calls) == 1
        return calls[0]

    def test_the_configured_values_reach_orchestrate(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GROUP_FINDINGS", "1")
        monkeypatch.setenv("PRXREF_MAX_WARNING_FINDINGS", "4")
        monkeypatch.setenv("PRXREF_MAX_OUTOFSCOPE_FINDINGS", "0")
        call = self._calls(monkeypatch)
        assert call["group_findings"] is True
        assert call["max_warning_findings"] == 4
        assert call["max_outofscope_findings"] == 0

    def test_the_defaults_are_off_and_unlimited(self, monkeypatch):
        call = self._calls(monkeypatch)
        assert call["group_findings"] is False
        assert call["max_warning_findings"] is None
        assert call["max_outofscope_findings"] is None


# ---------------------------------------------------------------------------
# Off-path identity. The capture below is the seat's BASE probe
# (build-0.15/seats/Q13-E/capture_off.py) ported verbatim; the goldens are the
# sha256 of its output at BASE cdb3e9e, before any #13 T6 edit. Record and JSON
# keys are projected onto the BASE key sets, so a key another 0.15 feature adds
# (always present, null when off) does not read as an off-path change here.
# ---------------------------------------------------------------------------

BASE_GOLDEN = {
    "main": "157b58c765faec04c40c5959acbb2cf64a3f65610279979edb55dd45f70ac53b",
    "override": "64ce8289a50334bcfbd39013978b058f2bdc340d0eb3f3d6ce4f11c08ab19dd9",
    "summary_only": "1539f1e036492cf3437e08b9263413c6cf9f1cb4f5378753dcc602fff62715a5",
}

BASE_FIELDS = ("file", "line", "severity", "confidence", "title", "body", "drop_reason", "scope", "rule")
BASE_RECORD_KEYS = (
    "verdict", "findings_active", "findings_dropped", "chunk_count", "chunks_reviewed",
    "chunks_failed", "elapsed_ms", "input_tokens", "output_tokens", "posted", "sampling",
    "cost_usd", "cost_estimated", "review_rules", "ticket_context", "spec_grounding",
    "size_advisory", "prompt_templates",
)
BASE_JSON_KEYS = (
    "verdict", "findings", "chunk_count", "chunks_reviewed", "chunks_failed", "elapsed_ms",
    "input_tokens", "output_tokens", "cost_usd", "cost_estimated", "posted", "review_rules",
    "ticket_context", "spec_grounding", "size_advisory", "sampling",
)
BASE_ROW_KEYS = ("file", "line", "severity", "confidence", "scope", "title", "body", "drop_reason")

MODEL_FINDINGS = [
    {"file": "src/app.py", "line": 3, "severity": "warning", "confidence": 0.9,
     "title": "Bare except swallows data errors", "body": "The data loader catches every exception.",
     "rule": "no-bare-except"},
    {"file": "src/app.py", "line": 9, "severity": "error", "confidence": 0.8,
     "title": "Bare except hides data corruption", "body": "Another bare except around data parsing.",
     "rule": "no-bare-except"},
    {"file": "src/app.py", "line": 15, "severity": "warning", "confidence": 0.7,
     "title": "Bare except in data retry", "body": "A third bare except around the data retry.",
     "rule": "No-Bare-Except"},
    {"file": "src/app.py", "line": 20, "severity": "warning", "confidence": 0.9,
     "title": "Magic number in data limit", "body": "The data limit 42 is unexplained."},
    {"file": "src/app.py", "line": 22, "severity": "warning", "confidence": 0.9,
     "title": "Magic number in data limit", "body": "The data limit 43 is unexplained."},
    {"file": "src/app.py", "line": 25, "severity": "outofscope", "confidence": 0.8,
     "title": "Typo in data comment", "body": "recieve in the data text."},
    {"file": "src/app.py", "line": 26, "severity": "outofscope", "confidence": 0.7,
     "title": "Trailing space in data line", "body": "The data line has a trailing space."},
    {"file": "src/other.py", "line": 2, "severity": "warning", "confidence": 0.9,
     "title": "Bare except swallows data errors", "body": "The data reader catches every exception.",
     "rule": "no-bare-except"},
]

MAIN_DIFF = _added_file_diff("src/app.py", 30) + _added_file_diff("src/other.py", 10)


def _override_text(name: str) -> str:
    head, marker, tail = load_prompt(name).partition(CONTEXT_MARKER)
    return "TEAM OVERRIDE: style-guide findings are welcome.\n\n" + head + marker + tail.replace("{rule_example}", "")


def _override_templates() -> PromptTemplates:
    files = tuple(
        TemplateFile(
            name=name, path=f"team-prompts/{name}.md", text=_override_text(f"{name}.md"),
            sha256=hashlib.sha256(_override_text(f"{name}.md").encode("utf-8")).hexdigest(),
            chars=len(_override_text(f"{name}.md")),
        )
        for name in ("worker", "systemic")
    )
    return PromptTemplates(
        dir="team-prompts", worker=files[0].text, systemic=files[1].text,
        summary=load_prompt("summary.md"), overrides=files,
    )


class _Recorder:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[tuple[str, str]] = []

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.prompts.append((system, user))
        return InvokeResult(
            text=self.text, input_tokens=10, output_tokens=5,
            model="rec-model-1", backend="fake", elapsed_ms=1,
        )


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{record.name} {record.levelname} {record.getMessage()}")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _finding(f) -> dict:
    return {name: getattr(f, name) for name in BASE_FIELDS}


def _record(res: dict) -> dict:
    out = {}
    for key in BASE_RECORD_KEYS:
        value = res[key]
        if key in ("findings_active", "findings_dropped"):
            out[key] = [_finding(f) for f in value]
        else:
            out[key] = value
    return out


def _json_payload(res: dict) -> dict:
    payload = cli._build_json_result(res)
    out = {key: payload[key] for key in BASE_JSON_KEYS}
    out["findings"] = [{key: row[key] for key in BASE_ROW_KEYS} for row in payload["findings"]]
    return out


def _trace_events(path: str) -> list[dict]:
    events = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            event = json.loads(line)
            event.pop("t_ms", None)
            if isinstance(event.get("meta"), dict):
                event["meta"].pop("elapsed_ms", None)
            events.append(event)
    return events


def _units(trace_dir: str) -> dict:
    out = {}
    if not os.path.isdir(trace_dir):
        return out
    for name in sorted(os.listdir(trace_dir)):
        with open(os.path.join(trace_dir, name), encoding="utf-8") as fh:
            text = fh.read()
        if name.endswith(".meta.json"):
            meta = json.loads(text)
            meta.pop("elapsed_ms", None)
            out[name] = meta
        else:
            out[name] = _sha(text)
    return out


SCENARIOS = {
    "main": (MAIN_DIFF, lambda: {}),
    "override": (MAIN_DIFF, lambda: {"prompts": _override_templates()}),
    "summary_only": (RELEASE_BINARY_DIFF, lambda: {}),
}


def capture(name: str, **knobs) -> tuple[str, dict]:
    diff, extra = SCENARIOS[name]
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

    @pytest.mark.parametrize("name", list(SCENARIOS))
    def test_the_default_run_is_byte_identical_to_base(self, name):
        text, res = capture(name)
        assert _sha(text) == BASE_GOLDEN[name]
        assert all(f.locations == () for f in _all(res))

    @pytest.mark.parametrize("name", list(SCENARIOS))
    def test_explicit_off_values_are_the_same_run(self, name):
        text, _res = capture(
            name, group_findings=False, max_warning_findings=None, max_outofscope_findings=None,
        )
        assert _sha(text) == BASE_GOLDEN[name]

    def test_the_golden_is_sensitive_to_grouping(self):
        text, _res = capture("main", group_findings=True)
        assert _sha(text) != BASE_GOLDEN["main"]

    def test_the_golden_is_sensitive_to_a_cap(self):
        text, _res = capture("main", max_outofscope_findings=0)
        assert _sha(text) != BASE_GOLDEN["main"]
