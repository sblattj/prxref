"""Issue #68 follow-up: the standards feature's prose surfaces and keyword
defaults agree with the shipped behaviour (every ``repo_context`` level, 6000
characters, TOML ``[]`` or ``"off"`` turns it off)."""
from __future__ import annotations

import inspect
import re
import tomllib
from pathlib import Path

from prxref import config, repo_standards, repo_unit

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "docs" / "examples" / "prxref.toml"


def _standards_docstring_entry() -> str:
    doc = config.__doc__ or ""
    start = doc.index("PRXREF_CONTEXT_STANDARDS_GLOBS\n")
    end = doc.index("PRXREF_CONTEXT_STANDARDS_MAX_CHARS\n")
    return re.sub(r"\s+", " ", doc[start:end])


def test_config_docstring_does_not_call_standards_repo_only():
    entry = _standards_docstring_entry()
    assert 'only' not in entry.split("excerpted", 1)[1].split("ranked", 1)[0]
    assert "every PRXREF_REPO_CONTEXT level" in entry


def test_example_toml_comment_matches_the_behaviour():
    text = EXAMPLE.read_text(encoding="utf-8")
    assert 'at repo_context = "repo"' not in text
    assert "set only through" not in text
    assert "every repo_context level" in text
    assert '[] or "off"' in text


def test_example_toml_budget_is_the_default():
    parsed = tomllib.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert parsed["context_standards_max_chars"] == config._DEFAULTS["context_standards_max_chars"]


def test_keyword_defaults_restate_the_config_default():
    default = config._DEFAULTS["context_standards_max_chars"]
    unit = inspect.signature(repo_unit.build_unit_context).parameters["standards_max_chars"]
    entries = inspect.signature(repo_standards.standards_entries).parameters["max_chars"]
    assert unit.default == default
    assert entries.default == default
    assert f"config default {default} restated" in (repo_unit.build_unit_context.__doc__ or "")
