"""Issue #75 follow-ups for heading-noun scope inference (0.30.1).

- A document title infers no scope: the first heading of a body that no
  later heading of its level or higher closes, when it is an H1 or holds
  sub-headings. Before, ``# Acme Java backend review rules`` scoped the
  whole file to java and a Markdown unit got an empty rules block.
- A heading naming several languages infers an ANY-of scope: the section
  covers a path any named language covers. An explicit ``scope:`` line
  keeps its every-token semantics.
"""
from __future__ import annotations

from prxref.quality import apply_rule_scope_check
from prxref.rules import (
    ReviewRules,
    RuleSection,
    filter_rule_sections,
    parse_rule_index,
    parse_rule_sections,
    scope_covers,
)
from prxref.text_inputs import cap_text
from prxref.triage import Finding

TITLE_BODY = "# Acme Java backend review rules\n\n## General style\n\n- Name every data limit.\n"
MULTI_BODY = "## Python and TypeScript conventions\n\n- Type every public function.\n"


def _rules(body: str) -> ReviewRules:
    return ReviewRules(
        path="r.md", body=cap_text(body, 24000), severity_map={}, sections=parse_rule_sections(body)
    )


def _finding(path: str, rule: str) -> Finding:
    return Finding(
        file=path, line=1, severity="warning", confidence=0.9, title="Untyped function", body="x", rule=rule
    )


class TestDocumentTitleInfersNothing:
    def test_an_h1_title_over_sub_sections_is_unscoped(self):
        assert parse_rule_sections(TITLE_BODY) == ()

    def test_the_title_file_keeps_every_rule_for_a_markdown_unit(self):
        block = _rules(TITLE_BODY).prompt_block("worker", paths=["docs/design.md"])
        assert "Name every data limit" in block
        assert "left out" not in block

    def test_a_bare_h1_title_is_unscoped(self):
        assert parse_rule_sections("# Java rules\n\n- x\n") == ()

    def test_an_h2_title_over_sub_sections_is_unscoped(self):
        body = "## Java team rules\n\n### General\n\n- g\n"
        assert parse_rule_sections(body) == ()

    def test_a_sub_section_under_the_title_still_infers(self):
        body = "# Acme Java rules\n\n## Java naming\n\n- n\n\n## General\n\n- g\n"
        assert [(s.name, s.scopes) for s in parse_rule_sections(body)] == [("Java naming", ("java",))]

    def test_a_single_h2_section_still_infers(self):
        (section,) = parse_rule_sections("## Java conventions\n- x\n")
        assert section.scopes == ("java",)

    def test_a_first_h1_closed_by_a_later_h1_still_infers(self):
        body = "# Java\n\n- j\n\n# Python\n\n- p\n"
        assert [s.scopes for s in parse_rule_sections(body)] == [("java",), ("python",)]

    def test_an_explicit_scope_line_on_a_title_still_scopes(self):
        body = "# Acme rules\nscope: java\n\n## General\n\n- g\n"
        (section,) = parse_rule_sections(body)
        assert section.scopes == ("java",)
        assert section.any_scope is False

    def test_the_index_leaves_the_title_unscoped(self):
        assert [(s.name, s.scopes) for s in parse_rule_index(TITLE_BODY)] == [
            ("Acme Java backend review rules", ()),
            ("General style", ()),
        ]


class TestMultiLanguageHeadingIsAnyOf:
    def test_the_inferred_section_is_any_of(self):
        (section,) = parse_rule_sections(MULTI_BODY)
        assert section.scopes == ("python", "typescript")
        assert section.any_scope is True

    def test_a_unit_with_both_languages_keeps_the_section(self):
        assert filter_rule_sections(MULTI_BODY, ["a.py", "b.ts"]) == (MULTI_BODY, ())

    def test_a_python_only_unit_keeps_the_section(self):
        assert filter_rule_sections(MULTI_BODY, ["a.py"]) == (MULTI_BODY, ())

    def test_a_markdown_unit_leaves_it_out(self):
        assert filter_rule_sections(MULTI_BODY, ["x.md"]) == ("", ("Python and TypeScript conventions",))

    def test_the_label_is_kept_on_either_language(self):
        sections = parse_rule_sections(MULTI_BODY)
        for path in ("a.py", "b.ts"):
            out, cleared = apply_rule_scope_check(
                [_finding(path, "Type every public function.")], sections=sections
            )
            assert cleared == 0 and out[0].rule == "Type every public function."

    def test_the_label_is_cleared_on_another_file_type(self):
        out, cleared = apply_rule_scope_check(
            [_finding("x.md", "Type every public function.")], sections=parse_rule_sections(MULTI_BODY)
        )
        assert cleared == 1 and out[0].rule is None

    def test_the_annotation_reads_or(self):
        block = _rules(MULTI_BODY).prompt_block("worker")
        assert "## Python and TypeScript conventions (applies to: python or typescript)" in block

    def test_the_index_carries_the_any_of_flag(self):
        (section,) = parse_rule_index(MULTI_BODY)
        assert section.scopes == ("python", "typescript") and section.any_scope is True


class TestExplicitScopeLinesKeepEveryTokenSemantics:
    BODY = "## Mixed\nscope: python, typescript\n\n- rule\n"

    def test_the_explicit_section_is_every_token(self):
        (section,) = parse_rule_sections(self.BODY)
        assert section.any_scope is False

    def test_no_single_path_satisfies_both_tokens(self):
        assert filter_rule_sections(self.BODY, ["a.py", "b.ts"]) == ("", ("Mixed",))

    def test_the_label_is_cleared_on_a_python_file(self):
        out, cleared = apply_rule_scope_check([_finding("a.py", "Mixed")], sections=parse_rule_sections(self.BODY))
        assert cleared == 1 and out[0].rule is None

    def test_the_annotation_keeps_commas(self):
        assert "## Mixed (applies to: python, typescript)" in _rules(self.BODY).prompt_block("worker")


class TestScopeCovers:
    def test_every_token_by_default(self):
        assert scope_covers(("java", "comments"), "A.java")
        assert not scope_covers(("python", "typescript"), "a.py")

    def test_any_token_when_asked(self):
        assert scope_covers(("python", "typescript"), "a.py", any_token=True)
        assert not scope_covers(("python", "typescript"), "x.md", any_token=True)

    def test_a_hand_built_section_defaults_to_every_token(self):
        assert RuleSection(name="n", scopes=("java",)).any_scope is False
