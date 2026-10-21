"""``PRXREF_VERIFY_ERRORS`` and ``PRXREF_VERIFY_ERRORS_MAX`` (#126).

``verify_errors`` is a choice key (``off``, ``names``, ``llm``) whose default
is ``names``: the free, deterministic tier is on for everyone, the model tier
is opt-in. ``verify_errors_max`` is a non-negative int, default 5. Both are
repository-config keys, allowlisted into an eval run's ``config``, and the
CLI passes both to the orchestrator.
"""

from __future__ import annotations

import pytest

from prxref import config, evals
from prxref.config import load_config
from prxref.llm import ConfigError

ENV = "PRXREF_VERIFY_ERRORS"
ENV_MAX = "PRXREF_VERIFY_ERRORS_MAX"


class TestVerifyErrors:
    def test_unset_gives_names(self):
        assert load_config()["verify_errors"] == "names"

    @pytest.mark.parametrize("raw", ["off", "names", "llm"])
    def test_each_legal_value_loads(self, monkeypatch, raw):
        monkeypatch.setenv(ENV, raw)
        assert load_config()["verify_errors"] == raw

    @pytest.mark.parametrize("raw", ["on", "LLM", "true", "1", "name"])
    def test_a_value_outside_the_vocabulary_is_a_config_error_naming_the_legal_values(self, monkeypatch, raw):
        monkeypatch.setenv(ENV, raw)
        with pytest.raises(ConfigError, match=ENV) as exc:
            load_config()
        for word in ("off", "names", "llm"):
            assert word in str(exc.value)

    def test_the_vocabulary_is_declared_in_the_choice_table(self):
        assert config._CHOICE_KEYS["verify_errors"] == frozenset({"off", "names", "llm"})


class TestVerifyErrorsMax:
    def test_unset_gives_five(self):
        assert load_config()["verify_errors_max"] == 5

    @pytest.mark.parametrize("raw, expected", [("0", 0), ("12", 12)])
    def test_a_non_negative_int_loads(self, monkeypatch, raw, expected):
        monkeypatch.setenv(ENV_MAX, raw)
        assert load_config()["verify_errors_max"] == expected

    @pytest.mark.parametrize("raw", ["-1", "two", "1.5"])
    def test_anything_else_is_a_config_error(self, monkeypatch, raw):
        monkeypatch.setenv(ENV_MAX, raw)
        with pytest.raises(ConfigError, match=ENV_MAX):
            load_config()


class TestWiring:
    def test_both_are_repository_config_keys(self):
        assert {"verify_errors", "verify_errors_max"} <= set(config.FILE_KEYS)

    def test_both_are_recorded_in_an_eval_run(self):
        assert evals.RUN_CONFIG_KEYS[-2:] == ("verify_errors", "verify_errors_max")
