"""Every curated rule pack under ``docs/rulepacks/`` loads through the real loaders.

A pack is an ordinary rules file, so the test feeds each one to the function
``--rules-file`` reaches (:func:`prxref.rules.load_review_rules`) and to the one
``--scoped-rules`` reaches (:func:`prxref.rules.load_scoped_rules`), with the
configured default caps, and renders the worker prompt block.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from prxref import config
from prxref.rules import RULES_HEADING, load_review_rules, load_scoped_rules

PACK_DIR = Path(__file__).resolve().parent.parent / "docs" / "rulepacks"
PACKS = sorted(p for p in PACK_DIR.glob("*.md") if p.name != "README.md")


def test_there_is_at_least_one_pack():
    assert PACKS, f"no packs found in {PACK_DIR}"


@pytest.mark.parametrize("pack", PACKS, ids=lambda p: p.name)
class TestPack:
    def test_loads_as_the_always_on_rules_file(self, pack, caplog):
        cap = config._DEFAULTS["review_rules_max_chars"]
        with caplog.at_level(logging.INFO, logger="prxref"):
            rules = load_review_rules(str(pack), max_chars=cap, source="--rules-file")
        assert rules is not None
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert not rules.body.truncated
        assert rules.body.chars < cap
        assert rules.applies_to is None

    def test_has_rules_and_no_accidental_section_scope(self, pack):
        rules = load_review_rules(str(pack), max_chars=config._DEFAULTS["review_rules_max_chars"], source="--rules-file")  # noqa: E501
        assert len(rules.index) >= 1
        assert rules.sections == (), "a heading names a language or artifact, which scopes the section"

    def test_every_rule_section_has_a_source_and_a_guard(self, pack):
        text = load_review_rules(
            str(pack), max_chars=config._DEFAULTS["review_rules_max_chars"], source="--rules-file"
        ).body.text
        sections = [s for s in text.split("\n## ")[1:]]
        assert sections
        for section in sections:
            assert "Source" in section.split("\n- ")[0], section.split("\n")[0]
            assert "Do not flag" in section, section.split("\n")[0]

    @pytest.mark.parametrize("unit", ["worker", "sweep"])
    def test_renders_into_a_prompt_block(self, pack, unit):
        rules = load_review_rules(str(pack), max_chars=config._DEFAULTS["review_rules_max_chars"], source="--rules-file")  # noqa: E501
        block = rules.prompt_block(unit)
        assert block.startswith(RULES_HEADING)
        assert "<team_rules>" in block and "</team_rules>" in block
        assert rules.body.text in block
        assert "truncated" not in block.lower().split("</team_rules>")[-1]

    def test_loads_as_a_scoped_rules_file_beside_an_always_on_file(self, pack, tmp_path, caplog):
        always = tmp_path / "team.md"
        always.write_text("- error: a TODO without a ticket id.\n", encoding="utf-8")
        cap = config._DEFAULTS["scoped_rules_max_chars"]
        team = load_review_rules(str(always), max_chars=cap, source="--rules-file")
        with caplog.at_level(logging.WARNING, logger="prxref"):
            scoped = load_scoped_rules([str(pack)], max_chars=cap, source="--scoped-rules", always_on=team)
        assert scoped is not None and len(scoped.files) == 1
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
        assert not scoped.files[0].body.truncated
        assert scoped.files[0].applies_to is None
