"""Finding grouping (#13) composed with path-scoped review rules (#12) in one run.

Two seats wired ``orchestrate_review`` separately: P12-E gives each chunk its
own copy of the prompt context (``dataclasses.replace(prompt_context,
rules_worker=...)``) and the sweep one over the union of the chunks' paths,
and Q13-E puts ``reviewer.RULE_REQUEST`` into the context when grouping is on.
With both set:

- each chunk's system prompt carries its OWN scoped block and then the rule
  request, on the first attempt and on the timeout retry, and its user prompt
  keeps the ``"rule"`` example key;
- the sweep's system prompt carries the union block and then the request;
- a scoped file's severity map, with no always-on rules file, remaps a
  finding before grouping, so that finding still groups with its same-rule
  sibling and lends the group its remapped severity.

The expected prompts are built from real runs with neither feature and with
grouping alone, so a byte that either feature adds or loses shows here. Every
test runs the real reviewer and the real loaders; none uses ``contract_stubs``.
"""
from __future__ import annotations

import json

import pytest

from prxref import orchestrator
from prxref.quality import GROUPED_INTO_PREFIX
from prxref.reviewer import RULE_REQUEST
from tests.test_orchestrator import REF, FakeForge, _added_file_diff
from tests.test_orchestrator_grouping import RULE_HEADING, _ScriptedLLM
from tests.test_orchestrator_scoped_rules import (
    BLOCKER_MD,
    DEFAULT_CAP,
    HELM_MD,
    HELM_PATHS,
    HELM_RULE,
    JAVA_MD,
    JAVA_PATHS,
    JAVA_RULE,
    SERVICE,
    _RecordingLLM,
    _review,
    _scoped,
    _stack,
    _units,
    _with_block,
    stack_diff,
)

RULE_EXAMPLE_KEY = '"rule": "no-bare-except"'


@pytest.fixture
def work(tmp_path, monkeypatch):
    """A fresh working directory (the loaders confine paths to it), with the elapsed time pinned."""
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.chdir(root)
    monkeypatch.setattr(orchestrator, "_elapsed_ms", lambda t0: 0)
    return root


def _both(system_without_either: str, block: str) -> str:
    """The system half with the unit's scoped block and then the rule request appended."""
    return _with_block(_with_block(system_without_either, block), RULE_REQUEST)


@pytest.fixture
def runs(work):
    """The stack diff reviewed three ways: neither feature, grouping only, and both."""
    scoped = _scoped(work, {"java.md": JAVA_MD, "helm.md": HELM_MD})
    _, _, neither = _review(stack_diff())
    _, _, grouping = _review(stack_diff(), group_findings=True)
    _, _, both = _review(stack_diff(), scoped_rules=scoped, group_findings=True)
    return scoped, _units(neither), _units(grouping), _units(both)


class TestEachChunkPrompt:
    @pytest.mark.parametrize(("stack", "paths", "own", "other"), [
        ("java", JAVA_PATHS, JAVA_RULE, HELM_RULE),
        ("helm", HELM_PATHS, HELM_RULE, JAVA_RULE),
    ])
    def test_it_carries_its_own_block_and_the_rule_request(self, runs, stack, paths, own, other):
        scoped, (neither, _), (grouping, _), (both, _) = runs
        [(system, user)] = both[stack]
        block = scoped.unit_block("worker", paths, None, max_chars=DEFAULT_CAP).text
        assert system == _both(neither[stack][0][0], block)
        assert own in system and other not in system
        assert system.count(RULE_HEADING) == 1
        assert user == grouping[stack][0][1]
        assert RULE_EXAMPLE_KEY in user


class TestTheSweepPrompt:
    def test_it_carries_the_union_block_and_the_rule_request(self, runs):
        scoped, (_, neither), (_, grouping), (_, both) = runs
        block = scoped.unit_block("sweep", JAVA_PATHS + HELM_PATHS, None, max_chars=DEFAULT_CAP).text
        assert both[0] == _both(neither[0], block)
        assert JAVA_RULE in both[0] and HELM_RULE in both[0]
        assert both[0].count(RULE_HEADING) == 1
        assert both[1] == grouping[1]
        assert RULE_EXAMPLE_KEY in both[1]


class TestTheTimeoutRetry:
    def test_each_retry_keeps_its_own_block_and_the_rule_request(self, work, tmp_path):
        """With one worker the calls run in order, and each chunk's first attempt times out."""
        scoped = _scoped(work, {"java.md": JAVA_MD, "helm.md": HELM_MD})
        _, _, neither = _review(stack_diff())
        (base, _) = _units(neither)
        trace = tmp_path / "trace.jsonl"
        _, _, llm = _review(
            stack_diff(), _RecordingLLM(fail_calls={0, 2}),
            scoped_rules=scoped, group_findings=True, max_workers=1, trace_file=str(trace),
        )
        assert len(llm.calls) == 5
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        assert [(e["node"], e["phase"]) for e in events].count(("chunk", "retry")) == 2
        for (first, retry), stack, paths in (
            (llm.calls[0:2], "java", JAVA_PATHS), (llm.calls[2:4], "helm", HELM_PATHS),
        ):
            assert _stack(first[1]) == _stack(retry[1]) == stack
            block = scoped.unit_block("worker", paths, None, max_chars=DEFAULT_CAP).text
            assert retry[0] == first[0] == _both(base[stack][0][0], block)
            assert RULE_EXAMPLE_KEY in retry[1]


def _raw(line: int, severity: str, title: str) -> dict:
    return {
        "file": SERVICE, "line": line, "severity": severity, "confidence": 0.9, "title": title,
        "body": f"data {line} reaches the store before it is checked.", "rule": "validate-inputs",
    }


REMAP_CHUNK = [_raw(3, "warning", "Unchecked input in save"), _raw(9, "blocker", "Unchecked input in load")]


class TestTheScopedSeverityMapBeforeGrouping:
    def _run(self, work, *, scoped: bool, group_findings: bool):
        rules = _scoped(work, {"blocker.md": BLOCKER_MD}) if scoped else None
        return orchestrator.orchestrate_review(
            FakeForge(diff=_added_file_diff(SERVICE, 20)), REF, _ScriptedLLM(chunk=REMAP_CHUNK),
            post=False, max_workers=1, scoped_rules=rules, group_findings=group_findings,
        )

    def test_a_remapped_finding_still_groups_with_its_same_rule_sibling(self, work):
        """blocker.md reaches no unit (docs/**) and no always-on file is set, yet its map is run-wide."""
        res = self._run(work, scoped=True, group_findings=True)
        (rep,) = res["findings_active"]
        assert (rep.file, rep.line, rep.rule, rep.severity) == (SERVICE, 3, "validate-inputs", "error")
        assert rep.locations == ((SERVICE, 9),)
        assert rep.body.endswith(f"Also at: `{SERVICE}:9`")
        (member,) = res["findings_dropped"]
        assert (member.line, member.severity, member.rule) == (9, "error", "validate-inputs")
        assert member.drop_reason == f"{GROUPED_INTO_PREFIX}{SERVICE}:3"
        assert res["verdict"] == "Request-Changes"

    def test_without_the_map_the_blocker_dies_and_nothing_groups(self, work):
        res = self._run(work, scoped=False, group_findings=True)
        (rep,) = res["findings_active"]
        assert (rep.line, rep.severity, rep.locations) == (3, "warning", ())
        assert [f.drop_reason for f in res["findings_dropped"]] == ["invalid severity: 'blocker'"]

    def test_without_grouping_the_remapped_pair_stays_apart(self, work):
        res = self._run(work, scoped=True, group_findings=False)
        assert sorted((f.line, f.severity, f.rule) for f in res["findings_active"]) == [
            (3, "warning", "validate-inputs"), (9, "error", "validate-inputs"),
        ]
        assert res["findings_dropped"] == []
        assert res["rule_counts"] == [{"rule": "validate-inputs", "kind": "rule", "total": 2, "kept": 2}]
