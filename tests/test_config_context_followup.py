"""``PRXREF_CONTEXT_FOLLOWUP`` (0.18.0): the context follow-up opt-in.

A choice key, like ``PRXREF_REPO_CONTEXT`` and ``PRXREF_FAIL_ON``: only
``off`` (the default) and ``on`` are legal, and anything else — including the
truthy-looking ``ON``, ``true`` and ``1`` — is a ``ConfigError`` naming this
variable, exit 2. It is deliberately not a ``_BOOL_KEYS`` entry: that table's
``_truthy`` only recognises the literal ``"1"``, which would turn the typo
``PRXREF_CONTEXT_FOLLOWUP=true`` into a silent no-op for an opt-in feature
(map.md D1).

``eval run`` allowlists the key into ``run.json``'s ``config``, immediately
before ``repo_context`` (map.md D3), so the "allowlist position" and "run.json
config" cases live here rather than duplicating the eval-run harness of
``tests/test_eval_parse_retries.py``.
"""
from __future__ import annotations

import pytest

from prxref import config, evals
from prxref.config import load_config
from prxref.llm import ConfigError
from tests import test_eval_run as run_helpers

ENV = "PRXREF_CONTEXT_FOLLOWUP"
KEY = "context_followup"


class TestDefaultAndLegalValues:
    def test_unset_gives_off(self):
        assert load_config()["context_followup"] == "off"

    @pytest.mark.parametrize("raw", ["off", "on"])
    def test_each_legal_value_loads(self, monkeypatch, raw):
        monkeypatch.setenv(ENV, raw)
        assert load_config()["context_followup"] == raw

    def test_an_empty_value_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv(ENV, "")
        assert load_config()["context_followup"] == "off"

    def test_whitespace_only_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv(ENV, "   ")
        assert load_config()["context_followup"] == "off"


class TestOutsideTheVocabularyIsAConfigError:
    @pytest.mark.parametrize("raw", ["ON", "true", "1", "On", "yes", "off "])
    def test_a_value_outside_the_vocabulary_is_a_config_error(self, monkeypatch, raw):
        """Matching is exact and case-sensitive, like PRXREF_FAIL_ON and
        PRXREF_REPO_CONTEXT: a typo is rejected rather than guessed at, and
        the truthy-looking spellings 1/true/ON must not be silently coerced
        to "on" the way a _BOOL_KEYS entry would."""
        monkeypatch.setenv(ENV, raw)
        with pytest.raises(ConfigError, match="PRXREF_CONTEXT_FOLLOWUP"):
            load_config()

    def test_the_error_names_the_legal_values(self, monkeypatch):
        monkeypatch.setenv(ENV, "true")
        with pytest.raises(ConfigError) as exc:
            load_config()
        for word in ("off", "on"):
            assert word in str(exc.value)


class TestSchemaShape:
    def test_it_is_a_string_key_outside_the_numeric_surface(self):
        assert config._DEFAULTS["context_followup"] == "off"
        assert "context_followup" not in config._INT_KEYS | config._FLOAT_KEYS
        assert "context_followup" not in config._RANGES
        assert "context_followup" not in config._BOOL_KEYS

    def test_the_vocabulary_is_declared_in_the_choice_table(self):
        assert config._CHOICE_KEYS["context_followup"] == frozenset({"off", "on"})

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv(ENV, "off")
        assert load_config(context_followup="on")["context_followup"] == "on"

    def test_an_override_cannot_smuggle_a_value_outside_the_vocabulary(self):
        with pytest.raises(ConfigError, match="context_followup") as exc:
            load_config(context_followup="true")
        assert "PRXREF_CONTEXT_FOLLOWUP" not in str(exc.value)


class TestRunConfigAllowlist:
    def test_the_allowlist_holds_the_key_immediately_before_repo_context(self):
        keys = list(evals.RUN_CONFIG_KEYS)
        assert KEY in keys
        assert keys.index(KEY) == keys.index("repo_context") - 1

    def test_run_json_config_records_the_loaded_value(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV, "on")
        cases = run_helpers._dataset(tmp_path)

        run_helpers._run(run_helpers._args(cases, tmp_path / "out"), run_helpers.FakeReview())

        run = run_helpers._read(tmp_path / "out" / "L" / "run.json")
        assert run["config"][KEY] == "on"
        assert list(run["config"]) == list(evals.RUN_CONFIG_KEYS)

    def test_run_json_config_records_the_default(self, tmp_path):
        cases = run_helpers._dataset(tmp_path)

        run_helpers._run(run_helpers._args(cases, tmp_path / "out"), run_helpers.FakeReview())

        run = run_helpers._read(tmp_path / "out" / "L" / "run.json")
        assert run["config"][KEY] == "off"
