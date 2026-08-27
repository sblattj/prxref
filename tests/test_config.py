"""Tests for prxref.config: env loading, type coercion, overrides, and forge factory."""
import os

import pytest
import requests

from prxref import config, llm_backends, reviewer
from prxref.config import load_config, make_forge
from prxref.forges import bitbucket, github, gitlab
from prxref.forges.base import PRRef
from prxref.llm import ConfigError
from prxref.quality import DEFAULT_MAX_ERRORS
from tests.conftest import clear_prxref_env, prxref_env_names


def _make_ref(forge: str) -> PRRef:
    return PRRef(
        forge=forge,
        host="example.com",
        owner="owner",
        repo="repo",
        number=42,
        url="https://example.com/owner/repo/pull/42",
    )


class TestLoadConfigDefaults:
    def test_all_defaults_present(self):
        cfg = load_config()
        assert cfg["llm_backend"] == "openai-compat"
        assert cfg["llm_base_url"] == ""
        assert cfg["llm_api_key"] == ""
        assert cfg["llm_models"] == []
        assert cfg["confidence_floor"] == 0.6
        assert cfg["max_error_findings"] == DEFAULT_MAX_ERRORS
        assert cfg["max_chunks"] == 8
        assert cfg["llm_max_tokens"] == 4096
        assert cfg["llm_timeout"] == 45.0
        assert cfg["llm_temperature"] == ""
        assert cfg["bitbucket_token"] == ""
        assert cfg["github_token"] == ""
        assert cfg["gitlab_token"] == ""
        assert cfg["allow_unsigned"] is False


class TestLoadConfigEnv:
    def test_env_strings_and_coercions(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "ferry")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "http://localhost:11434")
        monkeypatch.setenv("PRXREF_LLM_API_KEY", "secret-key")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "claude-opus-4-7, gpt-5.2 ,")
        monkeypatch.setenv("PRXREF_CONFIDENCE_FLOOR", "0.85")
        monkeypatch.setenv("PRXREF_MAX_ERROR_FINDINGS", "5")
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "12")
        monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", "1")
        monkeypatch.setenv("PRXREF_BITBUCKET_TOKEN", "bb-token")
        monkeypatch.setenv("PRXREF_GITHUB_TOKEN", "gh-token")
        monkeypatch.setenv("PRXREF_GITLAB_TOKEN", "gl-token")

        cfg = load_config()
        assert cfg["llm_backend"] == "ferry"
        assert cfg["llm_base_url"] == "http://localhost:11434"
        assert cfg["llm_api_key"] == "secret-key"
        assert cfg["llm_models"] == ["claude-opus-4-7", "gpt-5.2"]
        assert cfg["confidence_floor"] == 0.85
        assert cfg["max_error_findings"] == 5
        assert cfg["max_chunks"] == 12
        assert cfg["allow_unsigned"] is True
        assert cfg["bitbucket_token"] == "bb-token"
        assert cfg["github_token"] == "gh-token"
        assert cfg["gitlab_token"] == "gl-token"

    @pytest.mark.parametrize("raw,expected", [
        ("1", True),
        (" 1 ", True),
        # Rejected on purpose: the webhook gate accepts only the literal "1",
        # so anything else must read as False here too, or config would promise
        # a bypass that never happens.
        ("true", False),
        ("TRUE", False),
        ("yes", False),
        ("on", False),
        ("01", False),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("", False),
    ])
    def test_bool_coercion_variants(self, monkeypatch, raw, expected):
        monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", raw)
        cfg = load_config()
        assert cfg["allow_unsigned"] is expected

    def test_invalid_int_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "not-a-number")
        with pytest.raises(ConfigError, match="PRXREF_MAX_CHUNKS"):
            load_config()

    def test_invalid_float_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONFIDENCE_FLOOR", "high")
        with pytest.raises(ConfigError, match="PRXREF_CONFIDENCE_FLOOR"):
            load_config()

    def test_config_error_is_still_a_value_error(self, monkeypatch):
        """Subclassing keeps every existing ``except ValueError`` caller working."""
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "not-a-number")
        with pytest.raises(ValueError):
            load_config()

    @pytest.mark.parametrize("name,key,expected", [
        ("PRXREF_LLM_TIMEOUT", "llm_timeout", 45.0),
        ("PRXREF_LLM_MAX_TOKENS", "llm_max_tokens", 4096),
        ("PRXREF_LLM_TEMPERATURE", "llm_temperature", ""),
        ("PRXREF_MAX_CHUNKS", "max_chunks", 8),
        ("PRXREF_LLM_BASE_URL", "llm_base_url", ""),
    ])
    def test_whitespace_only_env_reads_as_unset(self, monkeypatch, name, key, expected):
        """A .env line like ``PRXREF_LLM_TIMEOUT= `` must not abort the review."""
        monkeypatch.setenv(name, "   ")
        assert load_config()[key] == expected


class TestLLMBudgetKnobs:
    """PRXREF_LLM_MAX_TOKENS / _TIMEOUT / _TEMPERATURE: defaults, coercion, errors."""

    def test_defaults_equal_todays_hardcoded_values(self):
        cfg = load_config()
        assert cfg["llm_max_tokens"] == reviewer.MAX_TOKENS == 4096
        assert cfg["llm_timeout"] == llm_backends.DEFAULT_TIMEOUT == 45.0
        assert cfg["llm_temperature"] == ""

    def test_env_coercions(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_MAX_TOKENS", "8192")
        monkeypatch.setenv("PRXREF_LLM_TIMEOUT", "90")
        monkeypatch.setenv("PRXREF_LLM_TEMPERATURE", "0.2")
        cfg = load_config()
        assert cfg["llm_max_tokens"] == 8192
        assert isinstance(cfg["llm_max_tokens"], int)
        assert cfg["llm_timeout"] == 90.0
        assert isinstance(cfg["llm_timeout"], float)
        # temperature stays a string here: "" must survive as "omit it".
        assert cfg["llm_temperature"] == "0.2"

    def test_invalid_max_tokens_names_the_variable(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_MAX_TOKENS", "lots")
        with pytest.raises(ValueError, match="PRXREF_LLM_MAX_TOKENS"):
            load_config()

    def test_invalid_timeout_names_the_variable(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_TIMEOUT", "soon")
        with pytest.raises(ValueError, match="PRXREF_LLM_TIMEOUT"):
            load_config()

    def test_overrides_accept_the_new_keys(self):
        cfg = load_config(llm_max_tokens=1024, llm_timeout=5.0, llm_temperature="0.9")
        assert cfg["llm_max_tokens"] == 1024
        assert cfg["llm_timeout"] == 5.0
        assert cfg["llm_temperature"] == "0.9"


class TestBudgetKnobRanges:
    """Degenerate numbers are a usage error, not something that reaches the wire."""

    @pytest.mark.parametrize("raw", ["0", "-1", "-4096"])
    def test_non_positive_max_tokens_rejected(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_LLM_MAX_TOKENS", raw)
        with pytest.raises(ConfigError, match="PRXREF_LLM_MAX_TOKENS"):
            load_config()

    @pytest.mark.parametrize("raw", ["0", "0.0", "-0.5", "nan", "inf", "-inf"])
    def test_non_positive_or_non_finite_timeout_rejected(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_LLM_TIMEOUT", raw)
        with pytest.raises(ConfigError, match="PRXREF_LLM_TIMEOUT"):
            load_config()

    @pytest.mark.parametrize("value", [0, -1])
    def test_overrides_cannot_smuggle_a_degenerate_budget(self, value):
        with pytest.raises(ConfigError, match="PRXREF_LLM_MAX_TOKENS"):
            load_config(llm_max_tokens=value)

    def test_overrides_cannot_smuggle_a_degenerate_timeout(self):
        with pytest.raises(ConfigError, match="PRXREF_LLM_TIMEOUT"):
            load_config(llm_timeout=0.0)

    def test_smallest_legal_values_are_accepted(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_MAX_TOKENS", "1")
        monkeypatch.setenv("PRXREF_LLM_TIMEOUT", "0.001")
        cfg = load_config()
        assert cfg["llm_max_tokens"] == 1
        assert cfg["llm_timeout"] == 0.001


class TestEnvHygiene:
    """The suite's env-clear surface is derived from the schema, never hand-listed."""

    def test_every_defaults_key_yields_an_env_name(self):
        names = set(prxref_env_names())
        missing = [k for k in config._DEFAULTS if f"PRXREF_{k.upper()}" not in names]
        assert missing == []

    def test_legacy_aliases_are_covered(self):
        assert "PRXREF_MAX_ERRORS" in prxref_env_names()

    def test_a_new_defaults_key_is_cleared_without_touching_any_list(self, monkeypatch):
        """Adding a config key must not require editing a test-side list."""
        monkeypatch.setitem(config._DEFAULTS, "brand_new_knob", "")
        monkeypatch.setenv("PRXREF_BRAND_NEW_KNOB", "leaked")
        assert "PRXREF_BRAND_NEW_KNOB" in prxref_env_names()

        clear_prxref_env(monkeypatch)
        assert "PRXREF_BRAND_NEW_KNOB" not in os.environ

    def test_no_ambient_prxref_value_survives_into_a_test_body(self):
        assert [n for n in prxref_env_names() if n in os.environ] == []


class TestMaxErrorFindingsConfig:
    def test_default_matches_quality_default(self, monkeypatch):
        monkeypatch.delenv("PRXREF_MAX_ERROR_FINDINGS", raising=False)
        monkeypatch.delenv("PRXREF_MAX_ERRORS", raising=False)
        assert load_config()["max_error_findings"] == DEFAULT_MAX_ERRORS

    def test_new_env_name_populates_config(self, monkeypatch):
        monkeypatch.delenv("PRXREF_MAX_ERRORS", raising=False)
        monkeypatch.setenv("PRXREF_MAX_ERROR_FINDINGS", "6")
        assert load_config()["max_error_findings"] == 6

    def test_legacy_env_name_still_populates_config(self, monkeypatch):
        monkeypatch.delenv("PRXREF_MAX_ERROR_FINDINGS", raising=False)
        monkeypatch.setenv("PRXREF_MAX_ERRORS", "5")
        assert load_config()["max_error_findings"] == 5


class TestLoadConfigOverrides:
    def test_overrides_beat_env_and_defaults(self, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "4")
        cfg = load_config(max_chunks=16, llm_backend="litellm")
        assert cfg["max_chunks"] == 16
        assert cfg["llm_backend"] == "litellm"

    def test_none_override_is_ignored(self, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "4")
        cfg = load_config(max_chunks=None)
        assert cfg["max_chunks"] == 4

    def test_unknown_override_key_raises(self):
        with pytest.raises(ValueError, match="unknown config key"):
            load_config(non_existent_key="value")


class TestMakeForge:
    def test_bitbucket_instantiation(self):
        forge = make_forge(_make_ref("bitbucket"))
        assert isinstance(forge, bitbucket.ForgeImpl)
        assert forge.name == "bitbucket"

    def test_github_instantiation(self):
        forge = make_forge(_make_ref("github"))
        assert isinstance(forge, github.ForgeImpl)
        assert forge.name == "github"

    def test_gitlab_instantiation(self):
        forge = make_forge(_make_ref("gitlab"))
        assert isinstance(forge, gitlab.ForgeImpl)
        assert forge.name == "gitlab"

    def test_custom_session_injected(self):
        custom = requests.Session()
        forge = make_forge(_make_ref("bitbucket"), session=custom)
        assert getattr(forge, "_session", None) is custom

    def test_unknown_forge_raises(self):
        with pytest.raises(ValueError, match="unknown forge: 'gitea'"):
            make_forge(_make_ref("gitea"))


class TestAllowUnsignedAgreesWithGate:
    """config's parse and the gate that actually runs must never disagree.

    Before this test, config.py accepted "1"/"true"/"yes"/"on" while
    webhooks._allow_unsigned accepted only "1". Reading config.py would tell you
    PRXREF_ALLOW_UNSIGNED=true disabled signature verification; it never did.
    Two tests pinned the opposing behaviours, so neither could be changed
    without appearing to break the other.
    """

    @pytest.mark.parametrize("raw", [
        "1", " 1 ", "01", "1 1",
        "true", "TRUE", "True", "yes", "on",
        "0", "false", "no", "off", "", "   ",
    ])
    def test_config_matches_webhook_gate(self, monkeypatch, raw):
        from prxref import webhooks

        monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", raw)
        assert load_config()["allow_unsigned"] is webhooks._allow_unsigned(), (
            f"config and the webhook gate disagree for {raw!r}"
        )

