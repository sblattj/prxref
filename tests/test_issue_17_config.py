"""PRXREF_REPO_CONTEXT / _MAX_CHARS / CONTEXT_CONTRACT_GLOBS / _EXCLUDE_GLOBS.

The four 0.16.0 config keys (#17, seat T1). Nothing reads them yet -- wiring
is a later task -- so this module pins ``load_config`` behaviour only:
defaults, coercion, range/choice enforcement, and the OQ5 "a set value
REPLACES the built-in default, empty reads as unset" contract for
``PRXREF_CONTEXT_CONTRACT_GLOBS``.
"""
from __future__ import annotations

import pytest

from prxref import config
from prxref.config import load_config
from prxref.llm import ConfigError

CONTRACT_GLOBS_DEFAULT = [
    "**/openapi*.y*ml",
    "**/openapi*.json",
    "**/openapi/**",
    "**/swagger*",
    "**/*.schema.json",
    "**/db/changelog/**",
    "**/db/migration/**",
    "**/migrations/**",
]


class TestDefaults:
    def test_repo_context_defaults_to_off(self):
        assert config._DEFAULTS["repo_context"] == "off"
        assert load_config()["repo_context"] == "off"

    def test_repo_context_max_chars_defaults_to_12000(self):
        assert config._DEFAULTS["repo_context_max_chars"] == 12000
        value = load_config()["repo_context_max_chars"]
        assert value == 12000
        assert isinstance(value, int)

    def test_context_contract_globs_defaults_to_the_oq5_set(self):
        assert config._DEFAULTS["context_contract_globs"] == CONTRACT_GLOBS_DEFAULT
        assert load_config()["context_contract_globs"] == CONTRACT_GLOBS_DEFAULT

    def test_context_exclude_globs_defaults_to_empty(self):
        assert config._DEFAULTS["context_exclude_globs"] == []
        assert load_config()["context_exclude_globs"] == []

    def test_defaults_are_independent_objects_across_calls(self):
        """A caller mutating its own config dict must not corrupt the
        built-in default or a later ``load_config()`` call."""
        cfg = load_config()
        cfg["context_contract_globs"].append("**/mutated/**")
        assert load_config()["context_contract_globs"] == CONTRACT_GLOBS_DEFAULT
        assert config._DEFAULTS["context_contract_globs"] == CONTRACT_GLOBS_DEFAULT


class TestKeyDeclarations:
    def test_repo_context_is_a_choice_key(self):
        assert config._CHOICE_KEYS["repo_context"] == frozenset({"off", "diff", "repo"})
        assert "repo_context" not in config._INT_KEYS | config._FLOAT_KEYS
        assert "repo_context" not in config._RANGES

    def test_repo_context_max_chars_is_an_int_with_an_open_zero_bound(self):
        assert "repo_context_max_chars" in config._INT_KEYS
        assert config._RANGES["repo_context_max_chars"] == config._Range(0)
        assert config._RANGES["repo_context_max_chars"].low_inclusive is False

    def test_the_two_glob_keys_are_list_keys(self):
        assert "context_contract_globs" in config._LIST_KEYS
        assert "context_exclude_globs" in config._LIST_KEYS
        for key in ("context_contract_globs", "context_exclude_globs"):
            assert key not in config._RANGES
            assert key not in config._CHOICE_KEYS


class TestRepoContextChoice:
    @pytest.mark.parametrize("raw", ["off", "diff", "repo"])
    def test_each_legal_value_loads(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_REPO_CONTEXT", raw)
        assert load_config()["repo_context"] == raw

    def test_empty_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_REPO_CONTEXT", "")
        assert load_config()["repo_context"] == "off"

    def test_whitespace_only_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_REPO_CONTEXT", "   ")
        assert load_config()["repo_context"] == "off"

    @pytest.mark.parametrize("raw", ["Repo", "REPO", "Off", "DIFF", "bogus"])
    def test_a_value_outside_the_vocabulary_is_a_config_error(self, monkeypatch, raw):
        """Matching is exact and case-sensitive, mirroring PRXREF_FAIL_ON:
        ``Repo``/``REPO`` are rejected, not silently folded to ``repo``."""
        monkeypatch.setenv("PRXREF_REPO_CONTEXT", raw)
        with pytest.raises(ConfigError, match=r"^PRXREF_REPO_CONTEXT: "):
            load_config()

    def test_the_error_names_the_legal_values(self, monkeypatch):
        monkeypatch.setenv("PRXREF_REPO_CONTEXT", "bogus")
        with pytest.raises(ConfigError) as exc:
            load_config()
        for word in ("off", "diff", "repo"):
            assert word in str(exc.value)

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_REPO_CONTEXT", "off")
        assert load_config(repo_context="repo")["repo_context"] == "repo"

    def test_an_override_cannot_smuggle_a_value_outside_the_vocabulary(self):
        with pytest.raises(ConfigError, match=r"^repo_context: ") as exc:
            load_config(repo_context="bogus")
        assert "PRXREF_REPO_CONTEXT" not in str(exc.value)


class TestRepoContextMaxChars:
    @pytest.mark.parametrize("raw,expected", [("1", 1), (" 500 ", 500), ("100000", 100_000)])
    def test_env_coerces_to_an_int(self, monkeypatch, raw, expected):
        monkeypatch.setenv("PRXREF_REPO_CONTEXT_MAX_CHARS", raw)
        value = load_config()["repo_context_max_chars"]
        assert value == expected
        assert isinstance(value, int)

    def test_empty_or_whitespace_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_REPO_CONTEXT_MAX_CHARS", "   ")
        assert load_config()["repo_context_max_chars"] == 12000

    def test_negative_value_is_a_config_error_naming_the_variable(self, monkeypatch):
        monkeypatch.setenv("PRXREF_REPO_CONTEXT_MAX_CHARS", "-1")
        with pytest.raises(ConfigError, match=r"^PRXREF_REPO_CONTEXT_MAX_CHARS: ") as exc:
            load_config()
        assert "greater than 0" in str(exc.value)

    def test_zero_is_rejected_per_range_zero_semantics(self, monkeypatch):
        """``_Range(0)`` defaults ``low_inclusive`` to False, so the interval
        is open at zero -- the same contract as PRXREF_MAX_CHUNKS and
        PRXREF_SCOPED_RULES_MAX_CHARS. Zero is not a legal budget: it would
        ask for repository context and admit none of it."""
        assert config._RANGES["repo_context_max_chars"].accepts(0) is False
        monkeypatch.setenv("PRXREF_REPO_CONTEXT_MAX_CHARS", "0")
        with pytest.raises(ConfigError, match=r"^PRXREF_REPO_CONTEXT_MAX_CHARS: "):
            load_config()

    def test_malformed_value_names_the_variable(self, monkeypatch):
        monkeypatch.setenv("PRXREF_REPO_CONTEXT_MAX_CHARS", "lots")
        with pytest.raises(ConfigError, match=r"^PRXREF_REPO_CONTEXT_MAX_CHARS: "):
            load_config()

    def test_an_override_is_range_checked_and_named_as_itself(self):
        with pytest.raises(ConfigError, match=r"^repo_context_max_chars: ") as exc:
            load_config(repo_context_max_chars=0)
        assert "PRXREF_REPO_CONTEXT_MAX_CHARS" not in str(exc.value)

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_REPO_CONTEXT_MAX_CHARS", "3")
        assert load_config(repo_context_max_chars=5)["repo_context_max_chars"] == 5


class TestContextContractGlobs:
    def test_a_set_value_replaces_the_default(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONTEXT_CONTRACT_GLOBS", "custom/*.yaml")
        assert load_config()["context_contract_globs"] == ["custom/*.yaml"]

    def test_splits_on_commas_and_whitespace_like_the_other_list_keys(self, monkeypatch):
        monkeypatch.setenv(
            "PRXREF_CONTEXT_CONTRACT_GLOBS", "a/*.yaml,b/*.json c/*.sql"
        )
        assert load_config()["context_contract_globs"] == [
            "a/*.yaml", "b/*.json", "c/*.sql",
        ]

    def test_an_empty_value_gives_the_default(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONTEXT_CONTRACT_GLOBS", "")
        assert load_config()["context_contract_globs"] == CONTRACT_GLOBS_DEFAULT

    def test_a_whitespace_only_value_gives_the_default(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONTEXT_CONTRACT_GLOBS", "   ")
        assert load_config()["context_contract_globs"] == CONTRACT_GLOBS_DEFAULT

    def test_an_override_replaces_the_default_as_given(self):
        assert load_config(context_contract_globs=["one/*.yaml"])[
            "context_contract_globs"
        ] == ["one/*.yaml"]

    def test_an_override_of_the_empty_list_is_taken_literally(self):
        """Unlike the empty-string environment path (unset -> default), an
        override is used exactly as given: a caller that really wants zero
        contract globs passes ``[]`` as an override, not through the
        environment, where an empty string cannot spell it."""
        assert load_config(context_contract_globs=[])["context_contract_globs"] == []

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONTEXT_CONTRACT_GLOBS", "env/*.yaml")
        assert load_config(context_contract_globs=["flag/*.yaml"])[
            "context_contract_globs"
        ] == ["flag/*.yaml"]


class TestContextExcludeGlobs:
    def test_splits_on_commas_and_whitespace_like_the_other_list_keys(self, monkeypatch):
        monkeypatch.setenv(
            "PRXREF_CONTEXT_EXCLUDE_GLOBS", "**/secrets/**,**/*.pem\t**/vendor/**"
        )
        assert load_config()["context_exclude_globs"] == [
            "**/secrets/**", "**/*.pem", "**/vendor/**",
        ]

    def test_empty_or_whitespace_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONTEXT_EXCLUDE_GLOBS", "  \t ")
        assert load_config()["context_exclude_globs"] == []

    def test_an_override_replaces_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONTEXT_EXCLUDE_GLOBS", "a.md,b.md")
        assert load_config(context_exclude_globs=["c.md"])["context_exclude_globs"] == ["c.md"]

    def test_a_literal_space_is_written_as_a_question_mark(self, monkeypatch):
        """Same doctrine as PRXREF_SIZE_IGNORE_GLOBS: a comma-or-whitespace
        splitter cannot carry an item containing either character."""
        monkeypatch.setenv("PRXREF_CONTEXT_EXCLUDE_GLOBS", "path/with?space/**")
        assert load_config()["context_exclude_globs"] == ["path/with?space/**"]
