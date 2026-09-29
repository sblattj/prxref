"""GitHub issue #63 (2026-09-29): the team-rules cap default, raised to 24000.

``PRXREF_REVIEW_RULES_MAX_CHARS`` defaulted to 12000 while
``PRXREF_SCOPED_RULES_MAX_CHARS`` defaulted to 24000. Team rules files
mined from reviewer comments routinely run 18k to 22k characters, so the
old default silently cut their tail — usually the last groups in the
file (tests, observability, process) — behind one WARNING most CI runs
never surface. The default now matches the scoped cap at 24000, and the
old ceiling stays reachable through the environment variable.

(The sibling ``test_issue_63_review_rules.py`` pins issue #63 of the
project's previous issue tracker, in use before 0.14.0.)
"""

from __future__ import annotations

import hashlib
import logging

import pytest

from prxref import config
from prxref.config import load_config
from prxref.rules import load_review_rules

TAIL = "ISSUE-63-LAST-RULE-IN-FILE"
BODY = "x" * (20295 - len(TAIL)) + TAIL  # 20295 chars, like the issue's rules file


@pytest.fixture(autouse=True)
def _no_cap_in_the_environment(monkeypatch):
    monkeypatch.delenv("PRXREF_REVIEW_RULES_MAX_CHARS", raising=False)


def _rules_file(tmp_path):
    path = tmp_path / "rules.md"
    path.write_text(BODY, encoding="utf-8")
    return str(path)


def _load_with_the_configured_cap(path):
    return load_review_rules(path, max_chars=load_config()["review_rules_max_chars"], source="PRXREF_REVIEW_RULES")


class TestTheDefault:
    def test_it_is_24000_and_matches_the_scoped_cap(self):
        assert config._DEFAULTS["review_rules_max_chars"] == 24000
        assert config._DEFAULTS["review_rules_max_chars"] == config._DEFAULTS["scoped_rules_max_chars"]

    def test_it_flows_through_load_config(self):
        assert load_config()["review_rules_max_chars"] == 24000


class TestTheAcceptance:
    def test_a_20k_char_file_passes_whole_with_no_warning(self, tmp_path, caplog):
        path = _rules_file(tmp_path)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            rules = _load_with_the_configured_cap(path)
        assert rules.body.text == BODY
        assert TAIL in rules.prompt_block("worker")
        assert rules.body.truncated is False
        assert rules.record() == {
            "path": path,
            "sha256": hashlib.sha256(BODY.encode("utf-8")).hexdigest(),
            "chars": 20295,
            "max_chars": 24000,
            "truncated": False,
            "severity_map": {},
        }
        assert [r for r in caplog.records if r.name == "prxref.rules"] == []

    def test_the_old_12000_ceiling_is_still_reachable(self, tmp_path, monkeypatch, caplog):
        path = _rules_file(tmp_path)
        monkeypatch.setenv("PRXREF_REVIEW_RULES_MAX_CHARS", "12000")
        assert load_config()["review_rules_max_chars"] == 12000
        with caplog.at_level(logging.WARNING, logger="prxref"):
            rules = _load_with_the_configured_cap(path)
        assert rules.body.text == BODY[:12000]
        assert TAIL not in rules.body.text
        assert rules.record()["max_chars"] == 12000
        assert rules.record()["truncated"] is True
        warnings = [r.getMessage() for r in caplog.records if r.name == "prxref.rules"]
        assert len(warnings) == 1
        assert warnings[0].endswith("raise PRXREF_REVIEW_RULES_MAX_CHARS")
