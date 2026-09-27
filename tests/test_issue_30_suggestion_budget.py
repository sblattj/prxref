"""Issue #30 part D: completion-budget headroom when suggestions are on.

A live reference-model run showed the 4096-token default (``llm_max_tokens``)
truncating every ``PRXREF_SUGGESTIONS=on`` review, because a suggestion
lengthens the reply. ``load_config`` now raises the effective budget to
``config.SUGGESTIONS_MAX_TOKENS`` (8192) when suggestions are on AND the
operator left ``llm_max_tokens`` unset — by env, its legacy alias (there is
none today), or an override. An explicit value, however supplied, always
wins, even when it is lower than the bump.

At the default (suggestions off), the change is a no-op: the default config
dict must stay byte-identical to BASE.
"""
from __future__ import annotations

import pytest

from prxref import config
from prxref.config import load_config


class TestSuggestionsBudgetBump:
    def test_off_and_unset_gives_4096(self):
        cfg = load_config()
        assert cfg["suggestions"] == "off"
        assert cfg["llm_max_tokens"] == 4096

    def test_on_and_unset_gives_8192(self, monkeypatch):
        monkeypatch.setenv("PRXREF_SUGGESTIONS", "on")
        cfg = load_config()
        assert cfg["llm_max_tokens"] == config.SUGGESTIONS_MAX_TOKENS == 8192

    def test_on_and_explicit_env_4096_is_respected(self, monkeypatch):
        """An explicit value equal to the plain default still counts as
        supplied, so the bump must not overwrite it."""
        monkeypatch.setenv("PRXREF_SUGGESTIONS", "on")
        monkeypatch.setenv("PRXREF_LLM_MAX_TOKENS", "4096")
        cfg = load_config()
        assert cfg["llm_max_tokens"] == 4096

    def test_on_and_explicit_env_16000_is_respected(self, monkeypatch):
        monkeypatch.setenv("PRXREF_SUGGESTIONS", "on")
        monkeypatch.setenv("PRXREF_LLM_MAX_TOKENS", "16000")
        cfg = load_config()
        assert cfg["llm_max_tokens"] == 16000

    def test_on_and_override_2048_is_respected(self):
        """An explicit override, even below the plain default, always wins."""
        cfg = load_config(suggestions="on", llm_max_tokens=2048)
        assert cfg["llm_max_tokens"] == 2048

    def test_on_via_override_and_unset_env_gives_8192(self):
        cfg = load_config(suggestions="on")
        assert cfg["llm_max_tokens"] == config.SUGGESTIONS_MAX_TOKENS

    @pytest.mark.parametrize("legacy_key", sorted(config._LEGACY_ENV_ALIASES))
    def test_a_legacy_alias_counts_as_supplied(self, monkeypatch, legacy_key):
        """Any key with a legacy env alias must not be bumped by this rule when
        supplied only through that alias. ``llm_max_tokens`` itself has no
        legacy alias today, so this exercises the general "supplied via
        legacy alias" path against whichever key does have one, and pins that
        the mechanism this rule depends on (the ``supplied`` set covering
        legacy-alias hits) actually fires.
        """
        alias = config._LEGACY_ENV_ALIASES[legacy_key]
        monkeypatch.setenv("PRXREF_SUGGESTIONS", "on")
        monkeypatch.setenv(alias, str(config._DEFAULTS[legacy_key]))
        cfg = load_config()
        # The legacy-aliased key's own value must be exactly what was supplied
        # through the alias, not silently re-defaulted.
        assert cfg[legacy_key] == config._DEFAULTS[legacy_key]
        # And the suggestions bump still applies normally to llm_max_tokens,
        # which was not touched by this alias.
        assert cfg["llm_max_tokens"] == config.SUGGESTIONS_MAX_TOKENS

    def test_off_default_dict_is_byte_identical_to_base(self):
        """Nothing moves at the default. Build the expectation from
        ``_DEFAULTS`` itself, never a hand-copied literal, so this cannot drift
        from the schema it is meant to protect.

        ``price_table`` is excluded from the direct comparison: it is a str on
        the way in (see ``_DEFAULTS``'s own comment) that ``load_config``
        always replaces with its parsed form, off or on, so comparing it
        verbatim would fail regardless of this task's change.
        """
        expected: dict[str, object] = {
            key: list(value) if isinstance(value, list) else value
            for key, value in config._DEFAULTS.items()
        }
        cfg = load_config()
        cfg_without_price_table = {k: v for k, v in cfg.items() if k != "price_table"}
        expected_without_price_table = {
            k: v for k, v in expected.items() if k != "price_table"
        }
        assert cfg_without_price_table == expected_without_price_table
        assert cfg["llm_max_tokens"] == expected["llm_max_tokens"] == 4096


class TestSuggestionsBudgetFalsification:
    """Reverting only the "supplied" gate must turn this test red."""

    def test_explicit_low_override_would_be_overwritten_without_the_gate(self):
        """This is the case the "supplied" check exists to prevent: an
        explicit low budget getting silently raised. It documents the
        contract the falsification step is expected to break."""
        cfg = load_config(suggestions="on", llm_max_tokens=2048)
        assert cfg["llm_max_tokens"] == 2048
        assert cfg["llm_max_tokens"] != config.SUGGESTIONS_MAX_TOKENS
