"""Issue #13 output half: the ``rule`` and ``locations`` of every finding, in JSON and text.

- ``cli._finding_json`` always emits ``rule`` then ``locations``, right after
  ``scope``. ``rule`` is ``null``, never ``""``, when the finding names none.
  ``locations`` is ``null`` unless the finding is a grouped representative,
  and then lists its ``Also at:`` locations as ``{"file", "line"}`` objects in
  stored order.
- ``cli._fmt_finding_line`` appends `` [rule: <rule>]`` after any scope tag.
- A grouped run's JSON row round-trips into ``eval_metrics.credit_locations``
  as the representative plus its locations, and a member row credits only
  its own location while keeping its own rule.
- With grouping off the orchestrator resets every rule, so the text output is
  unchanged and every row's ``rule`` and ``locations`` are ``null``.
"""
from __future__ import annotations

import io
import json
import sys
import types
from types import SimpleNamespace

import pytest

from prxref import cli
from prxref.eval_metrics import credit_locations
from prxref.forges.base import PRRef
from prxref.orchestrator import orchestrate_review
from prxref.quality import GROUPED_INTO_PREFIX
from prxref.triage import SCOPE_IN, SCOPE_OUT, Finding
from tests.test_orchestrator import REF, FakeForge
from tests.test_orchestrator_grouping import APP, BARE_EXCEPT_RAW, ONE_FILE_DIFF, _ScriptedLLM

URL = "https://github.com/org/repo/pull/7"
CLI_REF = PRRef(forge="github", host="github.com", owner="org", repo="repo", number=7, url=URL)
ROW_KEYS = [
    "file", "line", "severity", "confidence", "scope", "rule", "locations", "title", "body", "drop_reason",
]
FROZEN = "warning a.py:5 T (confidence 0.90)"


def _f(**kw) -> Finding:
    fields = {"file": "a.py", "line": 5, "severity": "warning", "confidence": 0.9, "title": "T", "body": "B"}
    fields.update(kw)
    return Finding(**fields)


def _frozen_line(f) -> str:
    """The text line exactly as 0.14 rendered it, scope tag included."""
    line = f"{f.severity} {f.file}:{f.line} {f.title} (confidence {f.confidence:.2f})"
    return f"{line} [scope: {f.scope}]" if f.scope in (SCOPE_IN, SCOPE_OUT) else line


def _grouped_run(*, group_findings: bool = True) -> dict:
    return orchestrate_review(
        FakeForge(diff=ONE_FILE_DIFF), REF, _ScriptedLLM(chunk=BARE_EXCEPT_RAW),
        post=False, max_workers=1, group_findings=group_findings,
    )


def _round_trip(res: dict) -> list[dict]:
    return json.loads(json.dumps(cli._build_json_result(res)))["findings"]


def _printed(res: dict) -> str:
    buf = io.StringIO()
    cli._print_findings(res, out=buf)
    return buf.getvalue()


class TestTheRow:
    def test_rule_and_locations_follow_scope_in_this_order(self):
        assert list(cli._finding_json(_f(), drop_reason=None)) == ROW_KEYS

    def test_an_ungrouped_finding_without_a_rule_gives_two_nulls(self):
        row = cli._finding_json(_f(), drop_reason=None)
        assert row["rule"] is None
        assert row["locations"] is None

    def test_empty_locations_are_null_not_an_empty_list(self):
        row = cli._finding_json(_f(rule="no-bare-except", locations=()), drop_reason=None)
        assert row["locations"] is None
        assert json.dumps(row).count('"locations": null') == 1

    def test_an_empty_rule_is_emitted_as_null(self):
        assert cli._finding_json(_f(rule=""), drop_reason=None)["rule"] is None

    def test_a_rule_is_forwarded_as_given(self):
        assert cli._finding_json(_f(rule="No-Bare-Except"), drop_reason=None)["rule"] == "No-Bare-Except"

    def test_locations_become_file_line_objects_in_stored_order(self):
        row = cli._finding_json(_f(locations=(("a.py", 15), ("b.py", 9))), drop_reason=None)
        assert row["locations"] == [{"file": "a.py", "line": 15}, {"file": "b.py", "line": 9}]

    def test_a_finding_object_without_either_attribute_reports_null(self):
        legacy = SimpleNamespace(file="f.py", line=1, severity="error", confidence=0.9, title="t", body="b")
        row = cli._finding_json(legacy, drop_reason="x")
        assert list(row) == ROW_KEYS
        assert (row["rule"], row["locations"], row["drop_reason"]) == (None, None, "x")

    def test_a_dropped_member_row_keeps_its_own_rule(self):
        member = _f(line=9, rule="No-Bare-Except", drop_reason=f"{GROUPED_INTO_PREFIX}a.py:5")
        row = cli._finding_json(member, drop_reason=member.drop_reason)
        assert (row["rule"], row["locations"]) == ("No-Bare-Except", None)

    def test_active_and_dropped_rows_both_carry_the_keys(self):
        result = {
            "findings_active": [_f(rule="r", locations=(("a.py", 9),))],
            "findings_dropped": [_f(line=9, rule="r", drop_reason=f"{GROUPED_INTO_PREFIX}a.py:5")],
        }
        rows = cli._build_json_result(result)["findings"]
        assert [list(row) for row in rows] == [ROW_KEYS, ROW_KEYS]
        assert [(row["rule"], row["locations"]) for row in rows] == [
            ("r", [{"file": "a.py", "line": 9}]), ("r", None),
        ]


class TestTheTextLine:
    def test_a_rule_tag_follows_the_frozen_prefix(self):
        assert cli._fmt_finding_line(_f(rule="no-bare-except")) == f"{FROZEN} [rule: no-bare-except]"

    @pytest.mark.parametrize("scope", [SCOPE_IN, SCOPE_OUT])
    def test_the_rule_tag_comes_after_the_scope_tag(self, scope):
        line = cli._fmt_finding_line(_f(scope=scope, rule="no-bare-except"))
        assert line == f"{FROZEN} [scope: {scope}] [rule: no-bare-except]"

    @pytest.mark.parametrize("rule", [None, ""])
    def test_no_rule_adds_nothing(self, rule):
        assert cli._fmt_finding_line(_f(rule=rule)) == FROZEN

    def test_an_object_without_the_attribute_adds_nothing(self):
        legacy = SimpleNamespace(file="a.py", line=5, severity="warning", confidence=0.9, title="T")
        assert cli._fmt_finding_line(legacy) == FROZEN


class TestAGroupedRunThroughJson:
    def test_the_representative_row_credits_every_location(self):
        res = _grouped_run()
        (rep,) = res["findings_active"]
        assert rep.locations == ((APP, 9), (APP, 15))
        rep_row = _round_trip(res)[0]
        assert rep_row["drop_reason"] is None
        assert rep_row["rule"] == "no-bare-except"
        assert rep_row["locations"] == [{"file": APP, "line": 9}, {"file": APP, "line": 15}]
        assert credit_locations(rep_row) == [(rep.file, rep.line), *rep.locations]
        assert credit_locations(rep_row) == credit_locations(rep)

    def test_each_member_row_has_null_locations_and_its_own_rule(self):
        members = _round_trip(_grouped_run())[1:]
        assert sorted((row["line"], row["rule"]) for row in members) == [
            (9, "no-bare-except"), (15, "No-Bare-Except"),
        ]
        for row in members:
            assert row["drop_reason"] == f"{GROUPED_INTO_PREFIX}{APP}:3"
            assert row["locations"] is None
            assert credit_locations(row) == [(APP, row["line"])]

    def test_the_cli_prints_the_same_rows(self, monkeypatch, capsys):
        res = _grouped_run()
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: CLI_REF)
        for name, attrs in (
            ("prxref.llm_backends", {"create_llm_client": lambda cfg: object()}),
            ("prxref.orchestrator", {"orchestrate_review": lambda **kwargs: res}),
        ):
            module = types.ModuleType(name)
            for key, value in attrs.items():
                setattr(module, key, value)
            monkeypatch.setitem(sys.modules, name, module)
        assert cli.main(["review", "--pr-url", URL, "--no-post", "--format", "json"]) == 0
        rows = json.loads(capsys.readouterr().out)["findings"]
        assert rows == _round_trip(res)
        assert [row["locations"] is None for row in rows] == [False, True, True]


class TestTheTextOutputOfARun:
    def test_grouping_on_tags_the_representative_and_lists_the_members(self):
        out = _printed(_grouped_run())
        (rep_line,) = [ln for ln in out.splitlines() if ln.startswith("error ")]
        assert rep_line == (
            "error src/app.py:3 Bare except swallows data errors (confidence 0.90) [rule: no-bare-except]"
        )
        assert out.count(f"-- {GROUPED_INTO_PREFIX}{APP}:3") == 2

    def test_grouping_off_prints_the_0_14_lines_although_the_model_named_rules(self):
        res = _grouped_run(group_findings=False)
        assert len(res["findings_active"]) == 3
        out = _printed(res)
        assert "[rule:" not in out
        lines = out.splitlines()
        for f in res["findings_active"]:
            assert _frozen_line(f) in lines
        assert all(row["rule"] is None and row["locations"] is None for row in _round_trip(res))
