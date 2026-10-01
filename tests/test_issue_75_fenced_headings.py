"""Issue #75 follow-ups (0.30.1 review): fences and filtered-body annotation.

- A ``#`` line inside a fenced code block (``` or ~~~) is code, not a
  heading, wherever scope inference reads headings: the document title,
  the scoped-section walk, the end of a left-out section and the rule index.
  Before, ``# run the linter first`` in a shell sample was a peer H1, so the
  real title stopped being a title and scoped the whole file to java again.
- The ``(applies to: ...)`` annotation is decided on the ORIGINAL body, so
  a section that survives filtering keeps it even when the left-out peer
  was what made it a section rather than the document title.
"""
from __future__ import annotations

from prxref.rules import (
    ReviewRules,
    filter_rule_sections,
    parse_rule_index,
    parse_rule_sections,
)
from prxref.text_inputs import cap_text

FENCED_TITLE_BODY = (
    "# Acme Java backend review rules\n\n## General style\n\n- Name every data limit.\n\n"
    "```sh\n# run the linter first\nmake lint\n```\n"
)
PEER_BODY = "## Python conventions\n\n### Naming\n\n- Use snake_case.\n\n## Java conventions\n\n- Use camelCase.\n"


def _rules(body: str) -> ReviewRules:
    return ReviewRules(
        path="r.md", body=cap_text(body, 24000), severity_map={}, sections=parse_rule_sections(body)
    )


def test_fenced_comment_does_not_unseat_document_title():
    assert parse_rule_sections(FENCED_TITLE_BODY) == ()
    block = _rules(FENCED_TITLE_BODY).prompt_block("worker", paths=["docs/design.md"])
    assert "Name every data limit" in block
    assert "# Acme Java backend review rules\n" in block
    assert "left out" not in block


def test_tilde_fence_comment_does_not_unseat_document_title():
    body = FENCED_TITLE_BODY.replace("```sh", "~~~sh").replace("```\n", "~~~\n")
    assert parse_rule_sections(body) == ()
    assert filter_rule_sections(body, ["a.md"]) == (body, ())


def test_fenced_heading_noun_opens_no_scoped_section():
    body = "## General\n\n- Keep it short.\n\n```python\n# Java interop notes\nx = 1\n```\n"
    assert parse_rule_sections(body) == ()
    assert filter_rule_sections(body, ["a.py"]) == (body, ())


def test_fenced_comment_does_not_end_a_left_out_section():
    body = (
        "## Java conventions\n\n- Use camelCase.\n\n```sh\n# build it\nmvn -q package\n```\n\n"
        "## General\n\n- Keep it short.\n"
    )
    text, left_out = filter_rule_sections(body, ["a.py"])
    assert left_out == ("Java conventions",)
    assert text == "## General\n\n- Keep it short."


def test_shorter_or_other_marker_does_not_close_fence():
    body = (
        "# Rules\n\n## General\n\n- One.\n\n````md\n```\n~~~\n# Java\n````\n\n## Java conventions\n\n- Two.\n"
    )
    assert [section.name for section in parse_rule_sections(body)] == ["Java conventions"]


def test_unclosed_fence_runs_to_end_of_body():
    body = "## General\n\n- One.\n\n```sh\n# Java conventions\n- Two.\n"
    assert parse_rule_sections(body) == ()


def test_rule_index_skips_fenced_headings():
    index = parse_rule_index(FENCED_TITLE_BODY)
    assert [section.name for section in index] == ["Acme Java backend review rules", "General style"]


def test_unfenced_heading_behaviour_unchanged():
    body = "# Rules\n\n## Java conventions\n\n- Use camelCase.\n"
    assert [section.name for section in parse_rule_sections(body)] == ["Java conventions"]


def test_surviving_section_keeps_annotation_after_filtering():
    rules = _rules(PEER_BODY)
    alone = rules.prompt_block("worker", paths=["a.py"])
    both = rules.prompt_block("worker", paths=["a.py", "B.java"])
    assert "## Python conventions (applies to: python)\n\n### Naming" in alone
    assert "## Python conventions (applies to: python)\n\n### Naming" in both
    assert "Java conventions" not in alone.split("</team_rules>")[0]
    assert alone.endswith("[rules for other languages/file types left out: Java conventions]")


def test_unfiltered_block_annotation_unchanged():
    rules = _rules(PEER_BODY)
    assert rules.prompt_block("worker") == rules.prompt_block("worker", paths=["a.py", "B.java"])
