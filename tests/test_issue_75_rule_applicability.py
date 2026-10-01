"""Issue #75: findings must not cite rules that do not cover the defect.

Two halves, both pinned here:

- the prompt half — ``reviewer.RULE_REQUEST`` says a finding does not need
  a rule and must not name one whose stated scope sits elsewhere, and a
  rules body that declares ``scope:`` lines under ATX headings gets each
  scoped section's heading annotated ``(applies to: <tokens>)`` in the
  team-rules block, nothing removed;
- the deterministic half — ``quality.apply_rule_scope_check`` clears
  ``rule`` on a finding no scoped section covers and keeps the finding, so
  it groups and caps by normalized title like any ruleless one. The
  orchestrator runs it after the hedge gate and before grouping/cap on its
  own guard (any loaded rules file with a section scope, whatever the
  grouping and cap switches say), the run record's ``rule_scope_cleared``
  counts the cleared labels (``None`` when the check did not run), and one
  INFO line and one ``rulescope ok`` trace event report it.

Golden pins: a rules body with no ``scope:`` line parses to no section,
renders the pre-#75 team-rules block byte for byte, and leaves
``rule_scope_cleared`` ``None`` with no trace event — scope-less files
behave exactly as before. An unknown scope token is inert and never
filters.
"""
from __future__ import annotations

import json
import logging

import pytest

from prxref.orchestrator import orchestrate_review
from prxref.quality import (
    GROUPED_INTO_PREFIX,
    apply_rule_cap,
    apply_rule_grouping,
    apply_rule_scope_check,
    rule_cap_counts,
)
from prxref.reviewer import RULE_REQUEST
from prxref.rules import (
    _FRAMING,
    RULES_HEADING,
    ReviewRules,
    RuleSection,
    load_review_rules,
    load_scoped_rules,
    parse_rule_sections,
)
from prxref.text_inputs import cap_text
from prxref.triage import Finding
from tests.test_orchestrator import REF, FakeForge, _added_file_diff
from tests.test_orchestrator_grouping import _ScriptedLLM

APP_TS = "src/app.ts"
FOO_JAVA = "src/Foo.java"
ONE_TS_DIFF = _added_file_diff(APP_TS, 30)
FLOOR = 0.0

# One scoped java section, one scoped typescript section, one unscoped: the
# mixed shape the issue reports (a Java-only rule cited on a TypeScript file).
SCOPED_BODY = (
    "# Team rules\n"
    "\n"
    "## Java module boundaries\n"
    "\n"
    "scope: java\n"
    "\n"
    "- Controllers never call Controllers; the service layer owns the call.\n"
    "\n"
    "## TypeScript style\n"
    "\n"
    "scope: typescript\n"
    "\n"
    "- Never type boundary-crossing data as `any`.\n"
    "\n"
    "## General style\n"
    "\n"
    "- Name every data limit.\n"
)
SCOPELESS_BODY = SCOPED_BODY.replace("scope: java\n\n", "").replace(
    "scope: typescript\n\n", ""
)
SCOPED_SECTIONS = parse_rule_sections(SCOPED_BODY.strip())


def _f(line, *, file=APP_TS, severity="warning", confidence=0.9, title=None, body=None, rule=None, **kw):
    return Finding(
        file=file, line=line, severity=severity, confidence=confidence,
        title=title if title is not None else f"Data check {line}",
        body=body if body is not None else f"The data on line {line} is unchecked.",
        rule=rule, **kw,
    )


def _covered(token: str, path: str) -> bool:
    """Run one finding citing a single-token section through the check."""
    out, _cleared = apply_rule_scope_check(
        [_f(3, file=path, rule="The rule")],
        sections=(RuleSection(name="The rule", scopes=(token,)),),
    )
    return out[0].rule is not None


class TestParseRuleSections:
    def test_only_sections_with_a_scope_line_parse(self):
        assert [(s.name, s.scopes) for s in SCOPED_SECTIONS] == [
            ("Java module boundaries", ("java",)),
            ("TypeScript style", ("typescript",)),
        ]

    def test_a_body_without_scope_lines_parses_to_nothing(self):
        assert parse_rule_sections(SCOPELESS_BODY.strip()) == ()

    def test_tokens_split_on_commas_and_whitespace_casefolded(self):
        (section,) = parse_rule_sections("## S\n\nScope: Java,  PYTHON\n\n- r.\n")
        assert section.scopes == ("java", "python")

    def test_the_scope_line_must_be_the_sections_first_non_blank_line(self):
        assert parse_rule_sections("## S\n\n- A rule.\n\nscope: java\n") == ()

    def test_a_scope_line_with_no_token_declares_nothing(self):
        assert parse_rule_sections("## S\n\nscope:\n\n- r.\n") == ()

    def test_heading_levels_one_to_four_count_and_five_does_not(self):
        body = (
            "# One\n\nscope: java\n\n- r\n\n"
            "#### Four\n\nscope: python\n\n- r\n\n"
            "##### Five\n\nscope: docs\n\n- r\n\n"
            "##NoSpace\n\nscope: comments\n\n- r\n"
        )
        assert [s.name for s in parse_rule_sections(body)] == ["One", "Four"]

    def test_two_scoped_headings_in_a_row_both_parse(self):
        body = "## A\n\nscope: java\n\n## B\n\nscope: python\n\n- r\n"
        assert [(s.name, s.scopes) for s in parse_rule_sections(body)] == [
            ("A", ("java",)), ("B", ("python",)),
        ]

    def test_a_section_the_character_cap_cut_off_is_not_parsed(self):
        capped = cap_text(SCOPED_BODY.strip(), 30)
        assert "scope:" not in capped.text
        assert parse_rule_sections(capped.text) == ()

    def test_load_review_rules_parses_the_capped_bodys_sections(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "rules.md").write_text(SCOPED_BODY, encoding="utf-8")
        rules = load_review_rules("rules.md", max_chars=24000, source="--rules-file")
        assert [s.name for s in rules.sections] == [
            "Java module boundaries", "TypeScript style",
        ]

    def test_load_scoped_rules_parses_each_files_sections(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "app.md").write_text(SCOPED_BODY, encoding="utf-8")
        scoped = load_scoped_rules(["app.md"], max_chars=24000, source="--scoped-rules")
        assert scoped is not None and len(scoped.files) == 1
        assert [s.scopes for s in scoped.files[0].sections] == [("java",), ("typescript",)]


class TestPromptAnnotation:
    def _rules(self, body: str) -> ReviewRules:
        return ReviewRules(
            path="team-rules.md",
            body=cap_text(body.strip(), 24000),
            severity_map={},
            sections=parse_rule_sections(body.strip()),
        )

    def test_a_scoped_heading_gains_the_applies_to_note(self):
        block = self._rules(SCOPED_BODY).prompt_block("worker")
        assert "## Java module boundaries (applies to: java)\n" in block
        assert "## TypeScript style (applies to: typescript)\n" in block
        assert "## General style\n" in block  # unscoped: annotated nowhere

    def test_the_annotation_adds_text_and_removes_none(self):
        body = SCOPED_BODY.strip()
        block = self._rules(SCOPED_BODY).prompt_block("worker")
        assert f"<team_rules>\n{body}\n</team_rules>".replace(
            "## Java module boundaries\n", "## Java module boundaries (applies to: java)\n"
        ).replace(
            "## TypeScript style\n", "## TypeScript style (applies to: typescript)\n"
        ) in block

    def test_a_scopeless_body_renders_the_pre75_block_byte_for_byte(self):
        body = SCOPELESS_BODY.strip()
        rules = self._rules(SCOPELESS_BODY)
        assert rules.sections == ()
        expected = "\n\n".join((
            RULES_HEADING,
            _FRAMING["worker"],
            f"<team_rules>\n{body}\n</team_rules>",
        ))
        assert rules.prompt_block("worker") == expected
        assert rules.prompt_block("sweep") == "\n\n".join((
            RULES_HEADING,
            _FRAMING["sweep"],
            f"<team_rules>\n{body}\n</team_rules>",
        ))


class TestApplyRuleScopeCheck:
    def test_a_java_rule_on_a_typescript_file_is_cleared_not_dropped(self):
        out, cleared = apply_rule_scope_check(
            [_f(3, rule="Java module boundaries")], sections=SCOPED_SECTIONS,
        )
        assert cleared == 1
        assert out[0].rule is None and out[0].drop_reason is None

    def test_the_same_rule_on_a_java_file_is_kept(self):
        out, cleared = apply_rule_scope_check(
            [_f(3, file=FOO_JAVA, rule="Java module boundaries")], sections=SCOPED_SECTIONS,
        )
        assert cleared == 0 and out[0].rule == "Java module boundaries"

    def test_a_matching_section_for_the_files_type_is_kept(self):
        out, cleared = apply_rule_scope_check(
            [_f(3, rule="TypeScript style")], sections=SCOPED_SECTIONS,
        )
        assert cleared == 0 and out[0].rule == "TypeScript style"

    def test_an_unknown_label_is_kept(self):
        out, cleared = apply_rule_scope_check(
            [_f(3, rule="made-up rule")], sections=SCOPED_SECTIONS,
        )
        assert cleared == 0 and out[0].rule == "made-up rule"

    def test_a_rule_from_an_unscoped_section_is_kept(self):
        out, cleared = apply_rule_scope_check(
            [_f(3, rule="General style"), _f(4, rule="Name every data limit")],
            sections=SCOPED_SECTIONS,
        )
        assert cleared == 0 and [f.rule for f in out] == ["General style", "Name every data limit"]

    def test_a_bullet_label_is_kept_on_a_covered_path_and_cleared_on_another(self):
        label = "Controllers never call Controllers"
        out, cleared = apply_rule_scope_check(
            [_f(3, file=FOO_JAVA, rule=label), _f(4, rule=label)],
            sections=SCOPED_SECTIONS,
        )
        assert cleared == 1 and [f.rule for f in out] == [label, None]

    def test_an_empty_path_keeps_its_label(self):
        out, cleared = apply_rule_scope_check(
            [_f(3, file="", rule="Java module boundaries")], sections=SCOPED_SECTIONS,
        )
        assert cleared == 0 and out[0].rule == "Java module boundaries"

    def test_the_label_match_collapses_case_and_whitespace(self):
        out, _cleared = apply_rule_scope_check(
            [_f(3, file=FOO_JAVA, rule="  JAVA   module BOUNDARIES ")],
            sections=SCOPED_SECTIONS,
        )
        assert out[0].rule == "  JAVA   module BOUNDARIES "

    def test_the_label_may_be_the_headings_leading_words(self):
        out, cleared = apply_rule_scope_check(
            [_f(3, file=FOO_JAVA, rule="Java module")], sections=SCOPED_SECTIONS,
        )
        assert cleared == 0

    def test_a_single_mid_heading_word_does_not_match(self):
        # "boundaries" sits inside the section name but is not its leading
        # words, so it clears (the label is too weak to key a group on).
        out, cleared = apply_rule_scope_check(
            [_f(3, file=FOO_JAVA, rule="boundaries")], sections=SCOPED_SECTIONS,
        )
        assert cleared == 0 and out[0].rule == "boundaries"

    def test_ruleless_and_dropped_findings_pass_through_untouched(self):
        ruleless = _f(3, rule=None)
        dropped = _f(4, rule="Java module boundaries", drop_reason='hedged: "if"')
        out, cleared = apply_rule_scope_check(
            [ruleless, dropped], sections=SCOPED_SECTIONS,
        )
        assert out[0] is ruleless and out[1] is dropped and cleared == 0

    def test_empty_sections_clear_nothing(self):
        findings = [_f(3, rule="anything")]
        out, cleared = apply_rule_scope_check(findings, sections=())
        assert out[0] is findings[0] and cleared == 0

    def test_every_token_of_a_multi_token_scope_must_cover_the_path(self):
        sections = parse_rule_sections("## S\n\nscope: java, comments\n\n- r.\n")
        out, _cleared = apply_rule_scope_check(
            [_f(3, file=FOO_JAVA, rule="S"), _f(3, rule="S")], sections=sections,
        )
        assert [f.rule is not None for f in out] == [True, False]

    def test_an_unknown_token_is_inert_on_any_path(self):
        sections = parse_rule_sections("## S\n\nscope: cobol\n\n- r.\n")
        out, cleared = apply_rule_scope_check(
            [_f(3, file=APP_TS, rule="S"), _f(4, file="README.md", rule="S")],
            sections=sections,
        )
        assert cleared == 0 and [f.rule for f in out] == ["S", "S"]

    @pytest.mark.parametrize(("token", "path", "expected"), [
        ("java", "src/Foo.java", True),
        ("java", "src/Main.kt", True),
        ("java", "pom.xml", True),
        ("java", "gradle/build.gradle.kts", True),
        ("java", "src/app.ts", False),
        ("jvm", "src/Foo.java", True),
        ("python", "src/app.py", True),
        ("python", "src/app.ts", False),
        ("typescript", "src/app.ts", True),
        ("typescript", "src/widget.tsx", True),
        ("javascript", "vendor/legacy.js", True),
        ("js", "src/page.jsx", True),
        ("ts", "src/mod.mjs", True),
        ("ts", "src/mod.cjs", True),
        ("docs", "README.md", True),
        ("markdown", "docs/guide.mdx", True),
        ("docs", "NOTICE.rst", True),
        ("docs", "notes.txt", True),
        ("docs", "src/app.py", False),
        ("openapi", "specs/openapi.yaml", True),
        ("openapi", "api/swagger.yml", True),
        ("specs", "openapi.json", True),
        ("specs", "config/runtime.yaml", False),
        ("specs", "package.json", False),
        ("comments", "src/app.py", True),
        ("comments", "README.md", True),
    ])
    def test_the_token_table(self, token, path, expected):
        assert _covered(token, path) is expected


class TestClearedFindingsReuseTheRulelessPaths:
    """A cleared label is just ``rule=None``: grouping and the cap key on title."""

    def _cleared(self):
        findings = [
            _f(10, rule="Java module boundaries", title="Missing await on promise."),
            _f(20, rule="Java module boundaries", title="missing await on promise"),
        ]
        out, cleared = apply_rule_scope_check(findings, sections=SCOPED_SECTIONS)
        assert cleared == 2 and all(f.rule is None for f in out)
        return out

    def test_cleared_findings_group_on_normalized_title(self):
        grouped = apply_rule_grouping(
            self._cleared(), confidence_floor=FLOOR, sweep_start=2,
        )
        assert grouped[1].drop_reason == f"{GROUPED_INTO_PREFIX}{APP_TS}:10"
        assert grouped[0].rule is None

    def test_cleared_findings_cap_by_title(self):
        cleared = self._cleared()
        capped = apply_rule_cap(cleared, cap=1, confidence_floor=FLOOR, sweep_start=2)
        assert capped[1].drop_reason is not None
        assert capped[1].drop_reason.startswith("rule cap exceeded")
        rows = rule_cap_counts(cleared, cap=1, confidence_floor=FLOOR, sweep_start=2)
        assert [row["kind"] for row in rows] == ["title"]


TS_RAW = [
    {"file": APP_TS, "line": 3, "severity": "warning", "confidence": 0.9,
     "title": "Boundary bypass in the data loader",
     "body": "The data loader crosses the module boundary.", "rule": "Java module boundaries"},
    {"file": APP_TS, "line": 9, "severity": "warning", "confidence": 0.8,
     "title": "Boundary bypass in the data store",
     "body": "The data store crosses the module boundary.", "rule": "Java module boundaries"},
    {"file": APP_TS, "line": 15, "severity": "warning", "confidence": 0.7,
     "title": "Invented label on the data cache",
     "body": "The data cache cites a rule that does not exist.", "rule": "made-up rule"},
    {"file": APP_TS, "line": 21, "severity": "warning", "confidence": 0.9,
     "title": "Any-typed boundary data",
     "body": "The boundary data crosses as `any`.", "rule": "TypeScript style"},
]


class TestOrchestratorWiring:
    def _run(self, *, body=SCOPED_BODY, chunk=TS_RAW, trace=None, **kw):
        rules = ReviewRules(
            path="team-rules.md",
            body=cap_text(body.strip(), 24000),
            severity_map={},
            sections=parse_rule_sections(body.strip()),
        )
        forge = FakeForge(diff=ONE_TS_DIFF)
        llm = _ScriptedLLM(chunk=chunk)
        res = orchestrate_review(
            forge, REF, llm, post=False, max_workers=1, rules=rules,
            trace_file=str(trace) if trace else None, **kw,
        )
        return res, llm

    def test_wrong_scope_labels_are_cleared_and_unknown_ones_kept(self):
        res, _llm = self._run(group_findings=True)
        assert res["rule_scope_cleared"] == 2
        rules = sorted(f.rule is None for f in res["findings_active"])
        assert rules == [False, False, True, True]
        kept = sorted(f.rule for f in res["findings_active"] if f.rule is not None)
        assert kept == ["TypeScript style", "made-up rule"]

    def test_the_cleared_labels_leave_only_title_rows_in_rule_counts(self):
        # Grouping off, the default cap on: two same-title cleared findings
        # form one TITLE row where the rule row used to be, while the two
        # findings citing the file-covering TypeScript section keep theirs.
        chunk = [
            {"file": APP_TS, "line": 3, "severity": "warning", "confidence": 0.9,
             "title": "Missing await on the data promise",
             "body": "The data promise is not awaited.", "rule": "Java module boundaries"},
            {"file": APP_TS, "line": 9, "severity": "warning", "confidence": 0.8,
             "title": "missing await on the DATA promise",
             "body": "The data promise is not awaited here either.", "rule": "Java module boundaries"},
            {"file": APP_TS, "line": 15, "severity": "warning", "confidence": 0.9,
             "title": "Any-typed widget data",
             "body": "The widget data crosses as `any`.", "rule": "TypeScript style"},
            {"file": APP_TS, "line": 21, "severity": "warning", "confidence": 0.85,
             "title": "Any-typed store data",
             "body": "The store data crosses as `any`.", "rule": "TypeScript style"},
        ]
        res, _llm = self._run(chunk=chunk)
        assert res["rule_scope_cleared"] == 2
        assert res["rule_counts"] == [
            {"rule": "TypeScript style", "kind": "rule", "total": 2, "kept": 2},
            {"rule": "Missing await on the data promise", "kind": "title", "total": 2, "kept": 2},
        ]
        assert not any("boundaries" in row["rule"] for row in res["rule_counts"])

    def test_one_info_line_and_trace_event_report_the_check(self, tmp_path):
        trace = tmp_path / "run.jsonl"
        handler = _ListHandler()
        logger = logging.getLogger("prxref")
        old_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            res, _llm = self._run(group_findings=True, trace=trace)
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        assert any(
            "rule scope: cleared 2 rule label(s) whose scoped section does not cover the file" in line
            for line in handler.lines
        )
        events = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
        matches = [e for e in events if e.get("node") == "rulescope"]
        assert [(e["node"], e["phase"], e["meta"]) for e in matches] == [
            ("rulescope", "ok", {"cleared": 2}),
        ]

    def test_the_check_runs_on_its_own_guard_with_grouping_and_cap_off(self):
        # The ruling's own guard: any loaded rules file with a section scope,
        # independent of group_findings and rule_cap_active. With both off no
        # finding can carry a rule (the run never asked for one), so the
        # count is 0 — an int, not the skipped None.
        res, _llm = self._run(group_findings=False, max_findings_per_rule=0)
        assert res["rule_scope_cleared"] == 0

    def test_the_worker_system_prompt_carries_the_annotations_and_the_request(self):
        res, llm = self._run(group_findings=True)
        (system, _user) = llm.prompts[0]
        assert "## Java module boundaries (applies to: java)" in system
        assert "## TypeScript style (applies to: typescript)" in system
        assert system.endswith(RULE_REQUEST)
        assert res["rule_scope_cleared"] == 2

    def test_cleared_labels_group_by_title_through_the_whole_run(self):
        chunk = [
            {"file": APP_TS, "line": 3, "severity": "warning", "confidence": 0.9,
             "title": "Missing await on the data promise",
             "body": "The data promise is not awaited.", "rule": "Java module boundaries"},
            {"file": APP_TS, "line": 9, "severity": "warning", "confidence": 0.8,
             "title": "missing await on the data promise",
             "body": "The data promise is not awaited here either.", "rule": "Java module boundaries"},
        ]
        res, _llm = self._run(chunk=chunk, group_findings=True)
        assert res["rule_scope_cleared"] == 2
        (rep,) = res["findings_active"]
        assert rep.rule is None and rep.line == 3
        assert rep.body.endswith(f"Also at: `{APP_TS}:9`")
        (dropped,) = res["findings_dropped"]
        assert dropped.drop_reason == f"{GROUPED_INTO_PREFIX}{APP_TS}:3"

    def test_a_scopeless_rules_file_leaves_the_run_record_key_null(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "rules.md").write_text(SCOPELESS_BODY, encoding="utf-8")
        rules = load_review_rules("rules.md", max_chars=24000, source="--rules-file")
        trace = tmp_path / "run.jsonl"
        forge = FakeForge(diff=ONE_TS_DIFF)
        llm = _ScriptedLLM(chunk=TS_RAW)
        res = orchestrate_review(
            forge, REF, llm, post=False, max_workers=1, rules=rules,
            group_findings=True, trace_file=str(trace),
        )
        assert rules.sections == ()
        assert res["rule_scope_cleared"] is None
        assert res["rule_counts"] is not None  # the cap still ran
        events = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
        assert not [e for e in events if e.get("node") == "rulescope"]
        assert "(applies to:" not in llm.prompts[0][0]
        assert all(f.rule == "Java module boundaries" or f.rule == "made-up rule"
                   or f.rule == "TypeScript style" or f.rule is None
                   for f in [*res["findings_active"], *res["findings_dropped"]])
        # No section is scoped, so nothing is cleared: every label survives.
        assert [f.rule for f in res["findings_active"]].count(None) == 0


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(f"{record.name} {record.levelname} {record.getMessage()}")


class TestRuleRequestWording:
    def test_the_request_says_a_finding_does_not_need_a_rule(self):
        assert "A finding does not need a rule" in RULE_REQUEST

    def test_the_request_names_the_out_of_scope_case(self):
        assert (
            "the rule you would name is scoped to another language, file type, or "
            "artifact (for example a Java-only rule on a TypeScript file)"
        ) in RULE_REQUEST

    def test_the_request_forbids_uncovered_and_invented_names(self):
        assert "Never name a rule whose stated scope does not cover this finding" in RULE_REQUEST
        assert "never make a name up" in RULE_REQUEST

    def test_the_request_keeps_the_rule_names_heading(self):
        assert RULE_REQUEST.startswith("## Rule names\n\n")
