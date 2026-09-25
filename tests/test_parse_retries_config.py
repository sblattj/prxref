"""Tests for the ``PRXREF_LLM_PARSE_RETRIES`` config key (#21).

Nothing reads this key yet -- it is wired to the reply-retry loop in a later
task. This file only pins the config surface: default, valid range, the exit-2
path shared with the other int keys, and that the docs surfaces agree with the
schema on the derived key/name counts.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from prxref import config
from prxref.config import load_config
from prxref.llm import ConfigError
from tests.conftest import prxref_env_names
from tests.test_docs_consistency import REPO_ROOT, SURFACES


class TestDefault:
    def test_unset_defaults_to_one(self):
        assert config._DEFAULTS["llm_parse_retries"] == 1
        assert load_config()["llm_parse_retries"] == 1

    def test_zero_is_valid(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_PARSE_RETRIES", "0")
        cfg = load_config()
        assert cfg["llm_parse_retries"] == 0

    def test_env_coercion_yields_an_int(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_PARSE_RETRIES", "3")
        cfg = load_config()
        assert cfg["llm_parse_retries"] == 3
        assert isinstance(cfg["llm_parse_retries"], int)

    def test_the_env_name_is_derived_for_the_suite_wide_clear(self):
        assert "PRXREF_LLM_PARSE_RETRIES" in prxref_env_names()


class TestRangeEnforcement:
    """Same exit-2 path every other int key uses (see TestLLMSeedConfig in
    tests/test_config.py for the pattern this mirrors)."""

    def test_negative_value_exits_2_naming_the_variable(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_PARSE_RETRIES", "-1")
        with pytest.raises(ConfigError, match="PRXREF_LLM_PARSE_RETRIES"):
            load_config()

    def test_non_numeric_value_exits_2_naming_the_variable(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_PARSE_RETRIES", "x")
        with pytest.raises(ConfigError, match="PRXREF_LLM_PARSE_RETRIES"):
            load_config()

    def test_an_override_cannot_smuggle_a_negative_value(self):
        """Still rejected, and reported as the override it came from."""
        with pytest.raises(ConfigError, match="llm_parse_retries") as exc:
            load_config(llm_parse_retries=-1)
        assert "PRXREF_LLM_PARSE_RETRIES" not in str(exc.value)


class TestDocsAgreeWithTheSchema:
    """The docs' stated counts must equal what an independent AST walk of
    ``_DEFAULTS`` (and ``_LEGACY_ENV_ALIASES``) finds -- never eyeballed."""

    def _ast_counts(self) -> tuple[int, int]:
        src = (REPO_ROOT / "src" / "prxref" / "config.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        defaults_count = None
        legacy_count = None
        for node in ast.walk(tree):
            targets = None
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and node.target is not None:
                targets = [node.target]
            if not targets:
                continue
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id == "_DEFAULTS":
                    assert isinstance(node.value, ast.Dict)
                    defaults_count = len(node.value.keys)
                elif target.id == "_LEGACY_ENV_ALIASES":
                    assert isinstance(node.value, ast.Dict)
                    legacy_count = len(node.value.keys)
        assert defaults_count is not None
        assert legacy_count is not None
        return defaults_count, legacy_count

    def test_key_count_matches_ast(self):
        defaults_count, _ = self._ast_counts()
        assert defaults_count == len(config._DEFAULTS)
        assert f"**{defaults_count}** configuration keys" in SURFACES["docs/env-vars.md"]

    def test_accepted_name_count_matches_ast(self):
        defaults_count, legacy_count = self._ast_counts()
        accepted = defaults_count + legacy_count
        assert accepted == len(config._DEFAULTS) + len(config._LEGACY_ENV_ALIASES)
        assert f"for {accepted} accepted variable names" in SURFACES["docs/env-vars.md"]

    def test_llm_parse_retries_row_exists_in_docs(self):
        text = SURFACES["docs/env-vars.md"]
        rows = [
            ln for ln in text.splitlines()
            if ln.startswith("| `PRXREF_LLM_PARSE_RETRIES` |")
        ]
        assert len(rows) == 1

    def test_llm_parse_retries_row_exists_in_env_example(self):
        text = Path(REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        assert "PRXREF_LLM_PARSE_RETRIES=1" in text

    def test_llm_parse_retries_documented_in_module_docstring(self):
        assert "PRXREF_LLM_PARSE_RETRIES" in (config.__doc__ or "")
