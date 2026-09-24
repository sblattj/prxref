"""Issue #13 T1: the optional per-finding ``rule`` and its parser gate.

``Finding.rule`` names the rule or standard the reviewer applied. It is read
from model output only when the run asked for it (``accept_rule``), through
:func:`prxref.triage.normalize_rule`; anything else leaves it ``None``. What
is pinned:

- ``normalize_rule``: strings only, whitespace collapsed, case kept; empty,
  whitespace-only and oversized values, and values holding a control,
  surrogate, private-use or bidi-control character, are dropped, never raised
  or truncated;
- the three parsers (``reviewer._finding_from``, ``reviewer._invoke_and_parse``
  and ``orchestrator._coerce_finding``) keep a rule only when it is accepted;
- ``orchestrator._enforce_rule`` holds rules to the run's state, like
  ``_enforce_scope`` does for scope;
- ``orchestrator._origin_key`` carries the rule, so a chunk copy and a sweep
  copy that differ only in rule keep their sides of the sweep boundary;
- with the rule not accepted, every parser returns exactly what it returned at
  the 0.15 build base (25ce3fa) for the same inputs, rule ``None`` included.
"""
from __future__ import annotations

import dataclasses
import json
from dataclasses import replace

import pytest

from prxref import orchestrator
from prxref.reviewer import (
    NO_PROMPT_CONTEXT,
    PromptContext,
    _finding_from,
    _invoke_and_parse,
    review_chunk,
    review_systemic,
)
from prxref.triage import RULE_MAX_CHARS, SCOPE_OUT, SCOPE_UNKNOWN, Finding, normalize_rule
from tests.test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff
from tests.test_prompt_context import _LLM, DIGEST, TICKET_SCOPE, _chunk
from tests.test_prompt_inputs import _finding, _review

BASE_FIELDS = ("file", "line", "severity", "confidence", "title", "body", "drop_reason", "scope")


@dataclasses.dataclass(frozen=True)
class _RuleContext(PromptContext):
    """A run that asked the model for ``rule``: the flag the parsers read."""

    @property
    def rule_active(self) -> bool:
        return True


RULE_ON = _RuleContext()


def _base_row(f: Finding | None):
    if f is None:
        return None
    return tuple(getattr(f, name) for name in BASE_FIELDS)


def _rule_reply(*rules: object) -> str:
    return json.dumps({"findings": [
        {"file": "src/app.py", "line": i + 1, "severity": "warning",
         "confidence": 0.8, "title": f"t{i}", "body": "b", "rule": rule}
        for i, rule in enumerate(rules)
    ]})


NOT_STRINGS = [None, 42, 4.2, True, False, ["no-print"], {"id": "no-print"}, b"no-print"]
BLANKS = ["", " ", "\n\t \r\n", "  "]
UNPRINTABLE = [
    "no\x00print", "no-print\x1b[31m", "no\x7fprint", "no\x9bprint",
    "no\ud800print", "no\ue000print",
    "no\u202aprint", "no\u202eprint", "no\u2066print", "no\u2069print",
]
OVERSIZED = ["r" * (RULE_MAX_CHARS + 1), "a " * RULE_MAX_CHARS]


class TestNormalizeRule:
    def test_the_cap_is_120_characters(self):
        assert RULE_MAX_CHARS == 120

    @pytest.mark.parametrize("raw", NOT_STRINGS)
    def test_a_non_string_is_dropped(self, raw):
        assert normalize_rule(raw) is None

    @pytest.mark.parametrize("raw", BLANKS)
    def test_empty_and_whitespace_only_are_dropped(self, raw):
        assert normalize_rule(raw) is None

    @pytest.mark.parametrize("raw", OVERSIZED)
    def test_an_oversized_value_is_dropped_not_truncated(self, raw):
        assert normalize_rule(raw) is None

    @pytest.mark.parametrize("raw", UNPRINTABLE)
    def test_a_non_printable_character_drops_the_value(self, raw):
        assert normalize_rule(raw) is None

    @pytest.mark.parametrize(("raw", "expected"), [
        ("no-print", "no-print"),
        ("  no-print  ", "no-print"),
        ("Structured\n\tlog   keys", "Structured log keys"),
        ("a b", "a b"),
        ("No-Print", "No-Print"),
        ("règle 日本", "règle 日本"),
    ])
    def test_whitespace_collapses_and_case_is_kept(self, raw, expected):
        assert normalize_rule(raw) == expected

    def test_the_cap_is_measured_after_collapsing(self):
        exact = "r" * RULE_MAX_CHARS
        assert normalize_rule(exact) == exact
        assert normalize_rule(f"  {exact}\n") == exact
        padded = "a" + " " * 500 + "b"
        assert normalize_rule(padded) == "a b"

    def test_a_str_subclass_comes_back_as_a_plain_str(self):
        class Label(str):
            pass

        out = normalize_rule(Label(" no-print "))
        assert out == "no-print" and type(out) is str

    def test_an_object_whose_str_raises_is_dropped_without_raising(self):
        class Hostile:
            def __str__(self):
                raise RuntimeError("boom")

        assert normalize_rule(Hostile()) is None


class TestFindingRuleField:
    def test_rule_is_the_trailing_field_and_defaults_to_none(self):
        names = [f.name for f in dataclasses.fields(Finding)]
        assert names == [*BASE_FIELDS, "rule", "locations"]
        assert Finding("a.py", 1, "error", 0.9, "t", "b").rule is None

    def test_positional_construction_through_scope_still_works(self):
        f = Finding("a.py", 1, "error", 0.9, "t", "b", None, SCOPE_OUT)
        assert f.scope == SCOPE_OUT and f.rule is None

    def test_replace_carries_rule_through(self):
        f = Finding("a.py", 1, "error", 0.9, "t", "b", rule="no-print")
        assert replace(f, severity="warning").rule == "no-print"


class TestFindingFromRule:
    RAW = {"file": "src/a.py", "line": 3, "severity": "error", "confidence": 0.9,
           "title": "Leak", "body": "token logged", "rule": "  no   print "}

    def test_an_accepted_rule_is_normalized(self):
        assert _finding_from(dict(self.RAW), accept_rule=True).rule == "no print"

    def test_a_rule_is_ignored_unless_accepted(self):
        assert _finding_from(dict(self.RAW)).rule is None
        assert _finding_from(dict(self.RAW), accept_rule=False).rule is None

    @pytest.mark.parametrize("raw", NOT_STRINGS + BLANKS + UNPRINTABLE + OVERSIZED)
    def test_an_unusable_rule_is_none_and_the_finding_survives(self, raw):
        f = _finding_from(dict(self.RAW, rule=raw), accept_rule=True)
        assert f is not None and f.rule is None
        assert _base_row(f) == _base_row(_finding_from(dict(self.RAW)))

    def test_a_missing_rule_is_none(self):
        raw = {k: v for k, v in self.RAW.items() if k != "rule"}
        assert _finding_from(raw, accept_rule=True).rule is None

    def test_rule_and_scope_are_gated_independently(self):
        raw = dict(self.RAW, scope="out")
        only_rule = _finding_from(raw, accept_rule=True)
        assert (only_rule.rule, only_rule.scope) == ("no print", SCOPE_UNKNOWN)
        only_scope = _finding_from(raw, accept_scope=True)
        assert (only_scope.rule, only_scope.scope) == (None, SCOPE_OUT)


class TestInvokeAndParseRule:
    REPLY = _rule_reply(" no-print ", 7, "x" * (RULE_MAX_CHARS + 1), None)

    def _parse(self, **kw):
        findings, meta = _invoke_and_parse(
            _LLM(self.REPLY), "s", "u", budget=100, label="test", **kw,
        )
        assert meta["error"] == ""
        return [f.rule for f in findings]

    def test_rules_are_kept_only_when_accepted(self):
        assert self._parse(accept_rule=True) == ["no-print", None, None, None]
        assert self._parse() == [None] * 4
        assert self._parse(accept_scope=True) == [None] * 4

    @pytest.mark.parametrize("call", ["chunk", "sweep"])
    def test_the_reviewer_reads_rule_only_when_the_context_asks(self, call):
        def run(ctx):
            llm = _LLM(self.REPLY)
            if call == "chunk":
                findings, _ = review_chunk(llm, _chunk(), prompt_context=ctx)
            else:
                findings, _ = review_systemic(llm, DIGEST, prompt_context=ctx)
            return [f.rule for f in findings]

        assert run(RULE_ON) == ["no-print", None, None, None]
        assert run(NO_PROMPT_CONTEXT) == [None] * 4
        assert run(PromptContext(ticket_scope=TICKET_SCOPE)) == [None] * 4


class TestCoerceFindingRule:
    ITEM = {"file": "a.py", "line": 1, "severity": "error", "confidence": 0.9,
            "title": "t", "body": "b", "rule": " no-print "}

    def test_a_dict_rule_is_read_only_when_accepted(self):
        assert orchestrator._coerce_finding(dict(self.ITEM)).rule is None
        assert orchestrator._coerce_finding(dict(self.ITEM), accept_rule=True).rule == "no-print"

    @pytest.mark.parametrize("raw", NOT_STRINGS + BLANKS + UNPRINTABLE + OVERSIZED)
    def test_an_unusable_dict_rule_is_none_and_the_finding_survives(self, raw):
        f = orchestrator._coerce_finding(dict(self.ITEM, rule=raw), accept_rule=True)
        assert f is not None and f.rule is None

    def test_a_finding_object_passes_through_untouched(self):
        f = Finding("a.py", 1, "error", 0.9, "t", "b", rule="stray")
        assert orchestrator._coerce_finding(f) is f
        assert orchestrator._coerce_finding(f, accept_rule=True) is f

    def _chunk_double(self, items):
        def _rc(llm, files, **kw):
            return [dict(i) for i in items], {"error": "", "model": "m"}

        return _rc

    def test_the_chunk_hop_reads_rule_only_when_the_context_asks(self, monkeypatch):
        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", self._chunk_double([self.ITEM]))
        pr = FakeForge().get_pr(REF)

        def rules(ctx):
            res = orchestrator._invoke_chunk(FakeLLM(), [], pr, None, None, prompt_context=ctx)
            return [f.rule for f in res["findings"]]

        assert rules(RULE_ON) == ["no-print"]
        assert rules(NO_PROMPT_CONTEXT) == [None]

    def test_the_sweep_hop_reads_rule_only_when_the_context_asks(self, monkeypatch):
        monkeypatch.setattr(orchestrator.reviewer, "review_systemic", self._chunk_double([self.ITEM]))
        pr = FakeForge().get_pr(REF)

        def rules(ctx):
            res = orchestrator._run_sweep(FakeLLM(), [], pr, prompt_context=ctx)
            return [f.rule for f in res["findings"]]

        assert rules(RULE_ON) == ["no-print"]
        assert rules(NO_PROMPT_CONTEXT) == [None]


class TestEnforceRule:
    def test_inactive_resets_every_rule_to_none(self):
        findings = [
            _finding(1), replace(_finding(2), rule="no-print"),
            replace(_finding(3), rule=5), replace(_finding(4), rule="  "),
        ]
        out = orchestrator._enforce_rule(findings, False)
        assert [f.rule for f in out] == [None] * 4
        assert [f.line for f in out] == [1, 2, 3, 4]
        assert out[0] is findings[0]
        assert out[1] == replace(findings[1], rule=None)

    def test_active_normalizes_and_keeps_the_real_values(self):
        findings = [
            replace(_finding(1), rule="no-print"), replace(_finding(2), rule=" a \n b "),
            replace(_finding(3), rule=5), replace(_finding(4), rule="x" * (RULE_MAX_CHARS + 1)),
            _finding(5),
        ]
        out = orchestrator._enforce_rule(findings, True)
        assert [f.rule for f in out] == ["no-print", "a b", None, None, None]
        assert out[0] is findings[0] and out[4] is findings[4]
        assert out is not findings

    def test_only_the_rule_changes(self):
        f = replace(_finding(2, severity="error", scope=SCOPE_OUT), rule=" a  b ")
        out = orchestrator._enforce_rule([f], True)[0]
        assert out == replace(f, rule="a b")

    def test_empty_input(self):
        assert orchestrator._enforce_rule([], True) == []
        assert orchestrator._enforce_rule([], False) == []


class TestOriginKeyRule:
    def test_rule_is_in_the_key_before_scope(self):
        f = replace(_finding(3, scope=SCOPE_OUT), rule="no-print")
        key = orchestrator._origin_key(f)
        assert key[-2:] == ("no-print", SCOPE_OUT)
        assert key != orchestrator._origin_key(replace(f, rule="no-log"))
        assert key != orchestrator._origin_key(replace(f, rule=None))

    @pytest.mark.usefixtures("contract_stubs")
    def test_chunk_and_sweep_copies_that_differ_only_in_rule_keep_their_sides(
        self, monkeypatch, tmp_path,
    ):
        """Without rule in the key the walk swaps the two copies.

        The gate ties them on ``finding_sort_key`` and keeps arrival order, so
        the chunk copy is met first. Keyed without rule it would be taken for
        the sweep's, dropped as a duplicate, and the sweep copy's rule would
        survive in the chunk slot. ``_enforce_rule`` is held to a pass-through
        so the rules reach the gate whatever the run's grouping state.
        """
        monkeypatch.setattr(orchestrator, "_enforce_rule", lambda findings, active: list(findings))
        res, _, _ = _review(
            monkeypatch, tmp_path,
            chunk=[replace(_finding(3, title="Leak"), rule="chunk-rule")],
            sweep=[replace(_finding(3, title="Leak"), rule="sweep-rule")],
        )
        assert [f.rule for f in res["findings_active"]] == ["chunk-rule"]
        dupes = [f for f in res["findings_dropped"] if f.drop_reason == "duplicate of chunk finding"]
        assert [f.rule for f in dupes] == ["sweep-rule"]


FINDING_FROM_CASES = [
    ({"file": "src/a.py", "line": 3, "severity": "error", "confidence": 0.9,
      "title": " Leak ", "body": " token logged ", "scope": "out"}, False),
    ({"file": "src/a.py", "line": 3, "severity": "error", "confidence": 0.9,
      "title": " Leak ", "body": " token logged ", "scope": "out"}, True),
    ({"path": "src/b.py", "line": "7", "severity": None, "confidence": "high",
      "title": None, "body": 5}, False),
    ({"file": "src/c.py", "line": None, "confidence": None, "rule": "no-print"}, False),
    ({"file": "src/c.py", "rule": 42, "scope": " IN "}, True),
    ({"file": "src/d.py", "line": 2.9, "confidence": 1, "rule": ["x"]}, False),
    ({"file": "src/e.py", "severity": "warning", "rule": "  spaced   rule  "}, True),
    ({"file": "  ", "rule": "r"}, False),
    ("not a dict", False),
]

COERCE_CASES = [
    ({"file": "a.py", "line": 1, "severity": "error", "confidence": 0.9,
      "title": "t", "body": "b", "scope": "out", "rule": "r"}, False),
    ({"file": "a.py", "line": 1, "severity": "error", "confidence": 0.9,
      "title": "t", "body": "b", "scope": "out", "rule": "r"}, True),
    ({"file": "a.py"}, False),
    ({"file": 5, "line": "4", "confidence": "0.25", "scope": "IN", "rule": None}, True),
    ({"line": 1, "rule": "r"}, False),
    ({"file": "a.py", "line": "abc"}, False),
    ({"file": "a.py", "confidence": "x"}, False),
    ("str", False),
]

INVOKE_TEXT = json.dumps({"findings": [
    {"file": "src/app.py", "line": 2, "severity": "warning", "confidence": 0.8,
     "title": " t ", "body": "b", "scope": "out", "rule": "no-print"},
    {"file": "src/app.py", "line": "3", "severity": "error", "title": "u",
     "body": "c", "rule": 7},
    {"path": "src/b.py", "confidence": "bad", "scope": "IN", "rule": "  a   b  "},
    "junk",
    {"line": 4},
], "escalations": []})

BASE_FINDING_FROM = [
    ("src/a.py", 3, "error", 0.9, "Leak", "token logged", None, "unknown"),
    ("src/a.py", 3, "error", 0.9, "Leak", "token logged", None, "out"),
    ("src/b.py", 7, "", 0.5, "", "5", None, "unknown"),
    ("src/c.py", 0, "", 0.5, "", "", None, "unknown"),
    ("src/c.py", 0, "", 0.5, "", "", None, "in"),
    ("src/d.py", 2, "", 1.0, "", "", None, "unknown"),
    ("src/e.py", 0, "warning", 0.5, "", "", None, "unknown"),
    None,
    None,
]

BASE_COERCE = [
    ("a.py", 1, "error", 0.9, "t", "b", None, "unknown"),
    ("a.py", 1, "error", 0.9, "t", "b", None, "out"),
    ("a.py", 0, "", 0.0, "", "", None, "unknown"),
    ("5", 4, "", 0.25, "", "", None, "in"),
    None,
    None,
    None,
    None,
]

BASE_UNIT = {
    "off": [
        ("src/app.py", 2, "warning", 0.8, "t", "b", None, "unknown"),
        ("src/app.py", 3, "error", 0.5, "u", "c", None, "unknown"),
        ("src/b.py", 0, "", 0.5, "", "", None, "unknown"),
    ],
    "scope": [
        ("src/app.py", 2, "warning", 0.8, "t", "b", None, "out"),
        ("src/app.py", 3, "error", 0.5, "u", "c", None, "unknown"),
        ("src/b.py", 0, "", 0.5, "", "", None, "in"),
    ],
}

CONTEXTS = {"off": NO_PROMPT_CONTEXT, "scope": PromptContext(ticket_scope=TICKET_SCOPE)}


class TestFeatureOffMatchesBase:
    """Captured by running these inputs through the parsers at 25ce3fa.

    Every case carries a ``rule`` key or none; with the rule not accepted the
    Finding is field-for-field what the base produced, plus ``rule=None``.
    """

    @pytest.mark.parametrize(("case", "expected"), list(zip(FINDING_FROM_CASES, BASE_FINDING_FROM, strict=True)))
    def test_finding_from(self, case, expected):
        raw, accept_scope = case
        f = _finding_from(raw, accept_scope=accept_scope)
        assert _base_row(f) == expected
        assert f is None or f.rule is None

    @pytest.mark.parametrize(("case", "expected"), list(zip(COERCE_CASES, BASE_COERCE, strict=True)))
    def test_coerce_finding(self, case, expected):
        item, accept_scope = case
        f = orchestrator._coerce_finding(item, accept_scope=accept_scope)
        assert _base_row(f) == expected
        assert f is None or f.rule is None

    @pytest.mark.parametrize("ctx", sorted(CONTEXTS))
    @pytest.mark.parametrize("call", ["chunk", "sweep"])
    def test_invoke_and_parse_through_both_reviewers(self, call, ctx):
        llm = _LLM(INVOKE_TEXT)
        if call == "chunk":
            findings, _ = review_chunk(llm, _chunk(), prompt_context=CONTEXTS[ctx])
        else:
            findings, _ = review_systemic(llm, DIGEST, prompt_context=CONTEXTS[ctx])
        assert [_base_row(f) for f in findings] == BASE_UNIT[ctx]
        assert [f.rule for f in findings] == [None] * len(findings)

    def test_enforce_rule_off_returns_the_same_objects(self):
        findings = [_finding(1), _finding(2, scope=SCOPE_OUT)]
        out = orchestrator._enforce_rule(findings, False)
        assert all(a is b for a, b in zip(out, findings, strict=True))

    def test_a_real_review_run_ignores_a_stray_rule(self):
        reply = json.dumps({"findings": [{
            "file": "src/app.py", "line": 3, "severity": "warning", "confidence": 0.9,
            "title": "Problem 3", "body": "data 3 is wrong", "rule": "no-print",
        }]})
        res = orchestrator.orchestrate_review(
            FakeForge(diff=_added_file_diff("src/app.py", 20)), REF, FakeLLM(reply), post=False,
        )
        every = res["findings_active"] + res["findings_dropped"]
        assert res["findings_active"]
        assert [f.rule for f in every] == [None] * len(every)
