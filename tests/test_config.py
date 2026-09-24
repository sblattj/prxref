"""Tests for prxref.config: env loading, type coercion, overrides, and forge factory."""
import os
import re

import pytest
import requests

from prxref import cli, config, costs, llm_backends, reviewer
from prxref.config import load_config, make_forge
from prxref.forges import bitbucket, github, gitlab
from prxref.forges.base import PRRef
from prxref.llm import ConfigError
from prxref.quality import DEFAULT_MAX_ERRORS
from tests.conftest import clear_prxref_env, prxref_env_names
from tests.test_docs_consistency import SURFACES


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
        assert cfg["llm_seed"] is None
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


class TestLLMSeedConfig:
    """PRXREF_LLM_SEED: an int key whose unset state is a first-class value.

    Unlike ``llm_temperature`` (whose "" unset marker survives to the backend
    that owns the wire decision), "no seed" is representable in the seed's
    own type — ``None`` — so the seed is coerced and range-checked here, and
    ``_check_ranges`` skips it only while it is unset.
    """

    def test_default_is_none_the_declared_unset(self):
        assert config._DEFAULTS["llm_seed"] is None
        assert load_config()["llm_seed"] is None

    def test_env_coercion_yields_an_int(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_SEED", "42")
        cfg = load_config()
        assert cfg["llm_seed"] == 42
        assert isinstance(cfg["llm_seed"], int)

    def test_zero_is_a_legal_seed(self, monkeypatch):
        """0 is a valid provider seed; the inclusive low bound exists for it."""
        monkeypatch.setenv("PRXREF_LLM_SEED", "0")
        assert load_config()["llm_seed"] == 0

    @pytest.mark.parametrize("raw", ["-1", "-42"])
    def test_negative_seed_rejected(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_LLM_SEED", raw)
        with pytest.raises(ConfigError, match="PRXREF_LLM_SEED"):
            load_config()

    @pytest.mark.parametrize("raw", ["deterministic", "7.5"])
    def test_malformed_seed_names_the_variable(self, monkeypatch, raw):
        """Non-int input is the exit-2 configuration error, not a crash —
        and ``7.5`` is rejected here rather than silently truncated."""
        monkeypatch.setenv("PRXREF_LLM_SEED", raw)
        with pytest.raises(ConfigError, match="PRXREF_LLM_SEED"):
            load_config()

    def test_whitespace_only_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_SEED", "   ")
        assert load_config()["llm_seed"] is None

    def test_no_upper_bound_is_invented(self, monkeypatch):
        """A ceiling would be provider-specific; a huge seed is simply a seed."""
        monkeypatch.setenv("PRXREF_LLM_SEED", "2147483647")
        assert load_config()["llm_seed"] == 2_147_483_647

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_SEED", "1")
        assert load_config(llm_seed=9)["llm_seed"] == 9

    def test_an_override_cannot_smuggle_a_negative_seed(self):
        """Still rejected, and reported as the override it came from."""
        with pytest.raises(ConfigError, match="llm_seed") as exc:
            load_config(llm_seed=-1)
        assert "PRXREF_LLM_SEED" not in str(exc.value)

    def test_the_env_name_is_derived_for_the_suite_wide_clear(self):
        assert "PRXREF_LLM_SEED" in prxref_env_names()


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
        """Rejected, and reported under the source that supplied it.

        An unlabelled override is the caller's own keyword, so the message
        names ``llm_max_tokens`` rather than sending a library caller off to
        look for an environment variable they never set.
        """
        with pytest.raises(ConfigError, match="llm_max_tokens") as exc:
            load_config(llm_max_tokens=value)
        assert "PRXREF_LLM_MAX_TOKENS" not in str(exc.value)

    def test_overrides_cannot_smuggle_a_degenerate_timeout(self):
        with pytest.raises(ConfigError, match="llm_timeout"):
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


class TestChunkingAndFanoutKnobs:
    """PRXREF_CHUNK_TOKEN_BUDGET / _MAX_WORKERS / _MAX_INLINE_COMMENTS."""

    def test_defaults_equal_todays_hardcoded_values(self):
        from prxref import orchestrator, triage

        cfg = load_config()
        assert cfg["chunk_token_budget"] == triage.DEFAULT_TOKEN_BUDGET == 25_000
        assert cfg["max_workers"] == orchestrator.MAX_WORKERS == 4
        assert cfg["max_inline_comments"] == orchestrator.MAX_INLINE_COMMENTS == 15

    def test_env_coercions(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CHUNK_TOKEN_BUDGET", "9000")
        monkeypatch.setenv("PRXREF_MAX_WORKERS", "2")
        monkeypatch.setenv("PRXREF_MAX_INLINE_COMMENTS", "5")
        cfg = load_config()
        assert cfg["chunk_token_budget"] == 9000
        assert isinstance(cfg["chunk_token_budget"], int)
        assert cfg["max_workers"] == 2
        assert cfg["max_inline_comments"] == 5

    @pytest.mark.parametrize("name", [
        "PRXREF_CHUNK_TOKEN_BUDGET",
        "PRXREF_MAX_WORKERS",
        "PRXREF_MAX_INLINE_COMMENTS",
    ])
    def test_malformed_value_names_the_variable(self, monkeypatch, name):
        monkeypatch.setenv(name, "plenty")
        with pytest.raises(ConfigError, match=name):
            load_config()

    @pytest.mark.parametrize("name", [
        "PRXREF_CHUNK_TOKEN_BUDGET",
        "PRXREF_MAX_WORKERS",
        "PRXREF_MAX_INLINE_COMMENTS",
    ])
    @pytest.mark.parametrize("raw", ["0", "-1"])
    def test_non_positive_value_rejected(self, monkeypatch, name, raw):
        monkeypatch.setenv(name, raw)
        with pytest.raises(ConfigError, match=name):
            load_config()

    @pytest.mark.parametrize("key,env", [
        ("chunk_token_budget", "PRXREF_CHUNK_TOKEN_BUDGET"),
        ("max_workers", "PRXREF_MAX_WORKERS"),
        ("max_inline_comments", "PRXREF_MAX_INLINE_COMMENTS"),
    ])
    def test_overrides_cannot_smuggle_a_degenerate_value(self, key, env):
        """Still rejected; now reported as the override it came from."""
        with pytest.raises(ConfigError, match=key) as exc:
            load_config(**{key: 0})
        assert env not in str(exc.value)

    def test_overrides_accept_the_new_keys(self):
        cfg = load_config(chunk_token_budget=1, max_workers=1, max_inline_comments=1)
        assert cfg["chunk_token_budget"] == 1
        assert cfg["max_workers"] == 1
        assert cfg["max_inline_comments"] == 1

    @pytest.mark.parametrize("name,key,expected", [
        ("PRXREF_CHUNK_TOKEN_BUDGET", "chunk_token_budget", 25_000),
        ("PRXREF_MAX_WORKERS", "max_workers", 4),
        ("PRXREF_MAX_INLINE_COMMENTS", "max_inline_comments", 15),
    ])
    def test_whitespace_only_env_reads_as_unset(self, monkeypatch, name, key, expected):
        monkeypatch.setenv(name, "   ")
        assert load_config()[key] == expected


class TestChunkFileCapAndContextConfig:
    """PRXREF_CHUNK_MAX_FILES / PRXREF_CHUNK_CONTEXT_LINES: the two knobs that
    shape what one review chunk contains."""

    def test_defaults_equal_the_triage_constants(self):
        from prxref import triage

        cfg = load_config()
        assert cfg["chunk_max_files"] == triage.DEFAULT_MAX_FILES_PER_CHUNK == 5
        assert cfg["chunk_context_lines"] == triage.DEFAULT_CONTEXT_LINES == 3

    def test_env_coercions(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CHUNK_MAX_FILES", "9")
        monkeypatch.setenv("PRXREF_CHUNK_CONTEXT_LINES", "1")
        cfg = load_config()
        assert cfg["chunk_max_files"] == 9
        assert isinstance(cfg["chunk_max_files"], int)
        assert cfg["chunk_context_lines"] == 1
        assert isinstance(cfg["chunk_context_lines"], int)

    @pytest.mark.parametrize("name", [
        "PRXREF_CHUNK_MAX_FILES",
        "PRXREF_CHUNK_CONTEXT_LINES",
    ])
    def test_malformed_value_names_the_variable(self, monkeypatch, name):
        monkeypatch.setenv(name, "several")
        with pytest.raises(ConfigError, match=name):
            load_config()

    @pytest.mark.parametrize("raw", ["0", "-1", "-5"])
    def test_non_positive_max_files_rejected(self, monkeypatch, raw):
        """A zero-file cap makes every chunk full from birth, so nothing
        could ever be placed — meaningless, unlike a zero context count."""
        monkeypatch.setenv("PRXREF_CHUNK_MAX_FILES", raw)
        with pytest.raises(ConfigError, match="PRXREF_CHUNK_MAX_FILES"):
            load_config()

    def test_zero_context_lines_is_legal(self, monkeypatch):
        """0 is the -U0 reading — emit the changed lines only."""
        monkeypatch.setenv("PRXREF_CHUNK_CONTEXT_LINES", "0")
        assert load_config()["chunk_context_lines"] == 0

    @pytest.mark.parametrize("raw", ["-1", "-3"])
    def test_negative_context_lines_rejected(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_CHUNK_CONTEXT_LINES", raw)
        with pytest.raises(ConfigError, match="PRXREF_CHUNK_CONTEXT_LINES"):
            load_config()

    @pytest.mark.parametrize("key,env", [
        ("chunk_max_files", "PRXREF_CHUNK_MAX_FILES"),
        ("chunk_context_lines", "PRXREF_CHUNK_CONTEXT_LINES"),
    ])
    def test_overrides_cannot_smuggle_a_degenerate_value(self, key, env):
        """Still rejected; reported as the override it came from."""
        with pytest.raises(ConfigError, match=key) as exc:
            load_config(**{key: -1})
        assert env not in str(exc.value)

    def test_overrides_accept_the_new_keys(self):
        cfg = load_config(chunk_max_files=1, chunk_context_lines=0)
        assert cfg["chunk_max_files"] == 1
        assert cfg["chunk_context_lines"] == 0

    @pytest.mark.parametrize("name,key,expected", [
        ("PRXREF_CHUNK_MAX_FILES", "chunk_max_files", 5),
        ("PRXREF_CHUNK_CONTEXT_LINES", "chunk_context_lines", 3),
    ])
    def test_whitespace_only_env_reads_as_unset(self, monkeypatch, name, key, expected):
        monkeypatch.setenv(name, "   ")
        assert load_config()[key] == expected

    def test_no_upper_bound_is_invented(self, monkeypatch):
        """A ceiling for either is deployment-specific; a huge context radius
        simply means keep whatever the forge sent."""
        monkeypatch.setenv("PRXREF_CHUNK_MAX_FILES", "100000")
        monkeypatch.setenv("PRXREF_CHUNK_CONTEXT_LINES", "100000")
        cfg = load_config()
        assert cfg["chunk_max_files"] == 100_000
        assert cfg["chunk_context_lines"] == 100_000


class TestSpecSourceConfig:
    """PRXREF_SPEC_SOURCES / PRXREF_SPEC_MAX_CHARS / PRXREF_SPEC_DIGEST_TOKENS and the
    three PRXREF_JIRA_* keys: the spec-grounded review's input surface."""

    def test_defaults_are_inert(self):
        cfg = load_config()
        assert cfg["spec_sources"] == []
        assert cfg["spec_max_chars"] == 120000
        assert isinstance(cfg["spec_max_chars"], int)
        assert cfg["spec_digest_tokens"] == 3000
        assert isinstance(cfg["spec_digest_tokens"], int)
        assert cfg["jira_base_url"] == ""
        assert cfg["jira_email"] == ""
        assert cfg["jira_api_token"] == ""

    def test_the_new_keys_are_declared_in_their_tables(self):
        """The four-surface rule starts here: list key in _LIST_KEYS, both ints
        in _INT_KEYS and _RANGES, the three strings in none of them."""
        assert "spec_sources" in config._LIST_KEYS
        assert {"spec_max_chars", "spec_digest_tokens"} <= config._INT_KEYS
        assert {"spec_max_chars", "spec_digest_tokens"} <= set(config._RANGES)
        for key in ("jira_base_url", "jira_email", "jira_api_token"):
            assert key not in (
                config._INT_KEYS | config._FLOAT_KEYS | config._BOOL_KEYS
                | config._LIST_KEYS
            )

    def test_env_values_coerce(self, monkeypatch):
        monkeypatch.setenv("PRXREF_SPEC_MAX_CHARS", "5000")
        monkeypatch.setenv("PRXREF_SPEC_DIGEST_TOKENS", "500")
        monkeypatch.setenv("PRXREF_JIRA_BASE_URL", "https://jira.example.com")
        monkeypatch.setenv("PRXREF_JIRA_EMAIL", "ops@example.com")
        monkeypatch.setenv("PRXREF_JIRA_API_TOKEN", "token")
        cfg = load_config()
        assert cfg["spec_max_chars"] == 5000
        assert isinstance(cfg["spec_max_chars"], int)
        assert cfg["spec_digest_tokens"] == 500
        assert isinstance(cfg["spec_digest_tokens"], int)
        assert cfg["jira_base_url"] == "https://jira.example.com"
        assert cfg["jira_email"] == "ops@example.com"
        assert cfg["jira_api_token"] == "token"

    @pytest.mark.parametrize("raw,expected", [
        ("a", ["a"]),
        ("a,b", ["a", "b"]),
        ("a, b", ["a", "b"]),
        ("a b", ["a", "b"]),
        ("a b,c  d", ["a", "b", "c", "d"]),
        ("  a  ,  b  ", ["a", "b"]),
        ("a,,b", ["a", "b"]),
        ("a,b,", ["a", "b"]),
    ])
    def test_spec_sources_split_on_comma_and_whitespace(
        self, monkeypatch, raw, expected
    ):
        """A PRXREF_SPEC_SOURCES value may separate sources with commas, whitespace,
        or both — URLs and paths are opaque strings, so the only split that
        cannot corrupt one is a run of separators."""
        monkeypatch.setenv("PRXREF_SPEC_SOURCES", raw)
        assert load_config()["spec_sources"] == expected

    @pytest.mark.parametrize("name,key,expected", [
        ("PRXREF_SPEC_SOURCES", "spec_sources", []),
        ("PRXREF_SPEC_MAX_CHARS", "spec_max_chars", 120000),
        ("PRXREF_SPEC_DIGEST_TOKENS", "spec_digest_tokens", 3000),
    ])
    def test_whitespace_only_env_reads_as_unset(self, monkeypatch, name, key, expected):
        monkeypatch.setenv(name, "   ")
        assert load_config()[key] == expected

    @pytest.mark.parametrize("name", [
        "PRXREF_SPEC_MAX_CHARS",
        "PRXREF_SPEC_DIGEST_TOKENS",
    ])
    def test_malformed_int_names_the_variable(self, monkeypatch, name):
        monkeypatch.setenv(name, "lots")
        with pytest.raises(ConfigError, match=name):
            load_config()

    @pytest.mark.parametrize("name", [
        "PRXREF_SPEC_MAX_CHARS",
        "PRXREF_SPEC_DIGEST_TOKENS",
    ])
    @pytest.mark.parametrize("raw", ["0", "-1"])
    def test_non_positive_spec_ints_rejected(self, monkeypatch, name, raw):
        """Same bound as every other size knob: positive, unbounded above."""
        monkeypatch.setenv(name, raw)
        with pytest.raises(ConfigError, match=name):
            load_config()

    @pytest.mark.parametrize("key,env", [
        ("spec_max_chars", "PRXREF_SPEC_MAX_CHARS"),
        ("spec_digest_tokens", "PRXREF_SPEC_DIGEST_TOKENS"),
    ])
    def test_overrides_cannot_smuggle_a_degenerate_value(self, key, env):
        """An override is still range-checked, and reported as the override."""
        with pytest.raises(ConfigError, match=key) as exc:
            load_config(**{key: 0})
        assert env not in str(exc.value)

    def test_no_upper_bound_is_invented(self, monkeypatch):
        """A ceiling would be corpus-size-specific; a huge cap is a cap."""
        monkeypatch.setenv("PRXREF_SPEC_MAX_CHARS", "10000000")
        monkeypatch.setenv("PRXREF_SPEC_DIGEST_TOKENS", "1000000")
        cfg = load_config()
        assert cfg["spec_max_chars"] == 10_000_000
        assert cfg["spec_digest_tokens"] == 1_000_000

    def test_overrides_accept_the_new_keys(self):
        cfg = load_config(
            spec_sources=["https://a/spec.md"],
            spec_max_chars=1,
            spec_digest_tokens=1,
            jira_base_url="https://jira.example.com",
            jira_email="ops@example.com",
            jira_api_token="token",
        )
        assert cfg["spec_sources"] == ["https://a/spec.md"]
        assert cfg["spec_max_chars"] == 1
        assert cfg["spec_digest_tokens"] == 1
        assert cfg["jira_base_url"] == "https://jira.example.com"
        assert cfg["jira_email"] == "ops@example.com"
        assert cfg["jira_api_token"] == "token"

    def test_an_override_replaces_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_SPEC_SOURCES", "env-a env-b")
        assert load_config(spec_sources=["flag-a"])["spec_sources"] == ["flag-a"]

    def test_a_none_override_is_ignored(self, monkeypatch):
        monkeypatch.setenv("PRXREF_SPEC_SOURCES", "env-a")
        assert load_config(spec_sources=None)["spec_sources"] == ["env-a"]

    def test_the_widened_list_split_is_a_noop_for_llm_models(self, monkeypatch):
        """The comma-or-whitespace split applies to every list key; model
        names can never contain spaces, so ``llm_models`` only gains slack."""
        monkeypatch.setenv("PRXREF_LLM_MODELS", "m1 m2,m3")
        assert load_config()["llm_models"] == ["m1", "m2", "m3"]


class TestPreExistingNumericRanges:
    """The three numeric keys that predate the range check are now covered too.

    Making a documented-but-dead key authoritative makes its missing range check
    reachable, so the config surface has to validate its whole numeric surface
    rather than only the keys most recently added.
    """

    def test_every_numeric_key_declares_a_range(self):
        """Drift guard: a new int/float key without a range is a silent gap."""
        numeric = config._INT_KEYS | config._FLOAT_KEYS
        assert numeric - set(config._RANGES) == set()

    def test_a_numeric_default_is_both_coerced_and_range_checked(self):
        """The wider drift guard, for the gap the one above cannot see.

        That guard starts from ``_INT_KEYS | _FLOAT_KEYS``, so it only ever
        looks at keys someone already remembered to classify. A numeric key
        added to ``_DEFAULTS`` ALONE walks straight past it and fails twice
        over: ``_coerce_env`` matches none of its branches, so the environment
        value stays a ``str`` and reaches the wire as ``"8"`` rather than ``8``;
        and ``_check_ranges`` iterates ``_RANGES``, so nothing bounds it either.
        Starting from the DEFAULT VALUE catches that, because the default is
        the one thing a new key cannot be added without.

        ``llm_temperature`` is the deliberate exception and is pinned
        separately below; it is invisible here because its default is a string.
        """
        numeric_defaults = {
            key for key, value in config._DEFAULTS.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        assert numeric_defaults - (config._INT_KEYS | config._FLOAT_KEYS) == set()
        assert numeric_defaults - set(config._RANGES) == set()

    def test_llm_temperature_is_a_string_by_decision_not_by_omission(self):
        """The one numeric-LOOKING key deliberately left out of the coercion
        sets, written down here so the next reader does not file it as an
        oversight and "fix" it.

        It is stored as a string because the config layer records only what
        the operator supplied: "" is the unset marker, and the wire decision
        belongs to ``llm_backends.create_llm_client``, which owns payload
        construction. Since the reproducibility default that client now
        resolves an unset value to ``DEFAULT_TEMPERATURE`` (0.0, SENT), the
        distinction the string preserves is "the operator said nothing" —
        which no float can encode, and which is what lets an explicit value
        win verbatim. Its parse and its bound are not skipped, only moved:
        ``llm_backends._float_setting`` performs both when the client is
        built, and raises the same ``ConfigError`` naming the same variable.
        """
        assert config._DEFAULTS["llm_temperature"] == ""
        assert "llm_temperature" not in config._INT_KEYS | config._FLOAT_KEYS
        assert "llm_temperature" not in config._RANGES
        # Evidence for the "only moved" claim, rather than a comment asserting it.
        with pytest.raises(ConfigError, match="PRXREF_LLM_TEMPERATURE"):
            llm_backends._float_setting("-1", "PRXREF_LLM_TEMPERATURE", minimum=0.0)

    def test_every_coercion_key_is_a_real_config_key(self):
        """The other direction: a typo in a coercion set is dead code, because
        ``_coerce_env`` is only ever asked about keys that exist in
        ``_DEFAULTS``. A misspelt entry would silently coerce nothing."""
        declared = (
            config._INT_KEYS | config._FLOAT_KEYS
            | config._BOOL_KEYS | config._LIST_KEYS
        )
        assert declared - set(config._DEFAULTS) == set()

    @pytest.mark.parametrize("raw", ["0", "-1", "-8"])
    def test_non_positive_max_chunks_rejected(self, monkeypatch, raw):
        """PRXREF_MAX_CHUNKS=0 used to reach build_chunks and raise
        ``ValueError: min() iterable argument is empty`` out of
        orchestrate_review, which the CLI reported as a review failure (exit 0)."""
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", raw)
        with pytest.raises(ConfigError, match="PRXREF_MAX_CHUNKS"):
            load_config()

    def test_max_chunks_override_cannot_smuggle_zero(self):
        """The --max-chunks flag arrives as an override, so it is checked too."""
        with pytest.raises(ConfigError, match="max_chunks"):
            load_config(max_chunks=0)

    def test_smallest_legal_max_chunks_accepted(self, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "1")
        assert load_config()["max_chunks"] == 1

    @pytest.mark.parametrize("raw", ["-1", "-5"])
    def test_negative_max_error_findings_rejected(self, monkeypatch, raw):
        """A negative cap negative-slices ``ranked[cap:]``, silently dropping the
        |cap| LOWEST-confidence errors under the reason "error cap exceeded"."""
        monkeypatch.setenv("PRXREF_MAX_ERROR_FINDINGS", raw)
        with pytest.raises(ConfigError, match="PRXREF_MAX_ERROR_FINDINGS"):
            load_config()

    def test_zero_max_error_findings_is_legal(self, monkeypatch):
        """0 means "cap every error", which is coherent; negative is not."""
        monkeypatch.setenv("PRXREF_MAX_ERROR_FINDINGS", "0")
        assert load_config()["max_error_findings"] == 0

    def test_legacy_alias_is_range_checked_too(self, monkeypatch):
        """Checked, and named as the variable that is actually set.

        Reporting the canonical ``PRXREF_MAX_ERROR_FINDINGS`` here would point
        an operator at a variable absent from their environment.
        """
        monkeypatch.setenv("PRXREF_MAX_ERRORS", "-5")
        with pytest.raises(ConfigError, match="PRXREF_MAX_ERRORS") as exc:
            load_config()
        assert "PRXREF_MAX_ERROR_FINDINGS" not in str(exc.value)

    def test_legacy_alias_is_named_when_its_value_is_malformed(self, monkeypatch):
        """The coercion error names the source too, not only the range error."""
        monkeypatch.setenv("PRXREF_MAX_ERRORS", "several")
        with pytest.raises(ConfigError, match="PRXREF_MAX_ERRORS") as exc:
            load_config()
        assert "PRXREF_MAX_ERROR_FINDINGS" not in str(exc.value)

    @pytest.mark.parametrize("raw", ["1.5", "95", "-0.1", "nan", "inf", "-inf"])
    def test_out_of_band_confidence_floor_rejected(self, monkeypatch, raw):
        """Confidence is a 0-1 probability everywhere in triage.Finding.

        A fat-fingered ``PRXREF_CONFIDENCE_FLOOR=95`` (meant as a percentage)
        drops every finding, and the run then posts a confident
        "Approved - No findings" on a PR full of real errors. ``nan`` is the
        mirror: every ``conf < nan`` is False, so the gate is silently disabled.
        Both fail AS SUCCESS, which is the worst mode for an advisory reviewer.
        """
        monkeypatch.setenv("PRXREF_CONFIDENCE_FLOOR", raw)
        with pytest.raises(ConfigError, match="PRXREF_CONFIDENCE_FLOOR"):
            load_config()

    @pytest.mark.parametrize("raw,expected", [
        ("0", 0.0),
        ("0.0", 0.0),
        ("0.6", 0.6),
        ("1", 1.0),
        ("1.0", 1.0),
    ])
    def test_both_endpoints_of_the_closed_interval_accepted(
        self, monkeypatch, raw, expected
    ):
        """0.0 (keep everything) and 1.0 (only certainty) are both legitimate."""
        monkeypatch.setenv("PRXREF_CONFIDENCE_FLOOR", raw)
        assert load_config()["confidence_floor"] == expected

    @pytest.mark.parametrize("value", [1.5, -0.1, float("nan"), float("inf")])
    def test_confidence_floor_override_cannot_smuggle_an_out_of_band_value(self, value):
        with pytest.raises(ConfigError, match="confidence_floor"):
            load_config(confidence_floor=value)

    def test_message_states_the_bound_it_broke(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONFIDENCE_FLOOR", "1.5")
        with pytest.raises(ConfigError) as exc:
            load_config()
        assert "0.0" in str(exc.value) and "1.0" in str(exc.value)

    def test_no_upper_bound_is_invented_for_the_open_numerics(self, monkeypatch):
        """Ceilings for these are provider- and machine-specific; an invented
        limit would be worse than none."""
        monkeypatch.setenv("PRXREF_LLM_MAX_TOKENS", "1000000")
        monkeypatch.setenv("PRXREF_MAX_WORKERS", "512")
        monkeypatch.setenv("PRXREF_CHUNK_TOKEN_BUDGET", "10000000")
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "9999")
        monkeypatch.setenv("PRXREF_LLM_TIMEOUT", "86400")
        cfg = load_config()
        assert cfg["llm_max_tokens"] == 1_000_000
        assert cfg["max_workers"] == 512
        assert cfg["chunk_token_budget"] == 10_000_000
        assert cfg["max_chunks"] == 9999
        assert cfg["llm_timeout"] == 86400.0


class TestDryRun:
    """PRXREF_DRY_RUN is a boolean, and ``_truthy`` is the only boolean parser."""

    def test_defaults_to_off_so_behaviour_is_unchanged(self):
        assert load_config()["dry_run"] is False

    def test_literal_one_enables_it(self, monkeypatch):
        monkeypatch.setenv("PRXREF_DRY_RUN", "1")
        assert load_config()["dry_run"] is True

    @pytest.mark.parametrize("raw", ["true", "True", "yes", "on", "0", "y"])
    def test_only_the_literal_one_enables_it(self, monkeypatch, raw):
        """Same parser as PRXREF_ALLOW_UNSIGNED: a second boolean dialect in the
        config surface is how "yes" ends up meaning False in one key and True in
        another."""
        monkeypatch.setenv("PRXREF_DRY_RUN", raw)
        assert load_config()["dry_run"] is False

    def test_it_uses_the_one_boolean_parser(self, monkeypatch):
        monkeypatch.setenv("PRXREF_DRY_RUN", " 1 ")
        assert load_config()["dry_run"] is config._truthy(" 1 ")

    def test_whitespace_only_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_DRY_RUN", "   ")
        assert load_config()["dry_run"] is False

    def test_it_is_a_bool_key_not_a_numeric_one(self):
        """A bool needs no range, and must not be swept into the numeric check
        (``_check_ranges`` would read ``True`` as the number 1)."""
        assert "dry_run" in config._BOOL_KEYS
        assert "dry_run" not in config._INT_KEYS | config._FLOAT_KEYS
        assert "dry_run" not in config._RANGES

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_DRY_RUN", "1")
        assert load_config(dry_run=False)["dry_run"] is False

    def test_the_env_name_is_derived_for_the_suite_wide_clear(self):
        assert "PRXREF_DRY_RUN" in prxref_env_names()


class TestPostMode:
    """PRXREF_POST_MODE picks what gets written; the vocabulary is validated."""

    def test_defaults_to_summary_plus_inline_so_behaviour_is_unchanged(self):
        assert load_config()["post_mode"] == "summary+inline"

    @pytest.mark.parametrize("raw,expected", [
        ("summary+inline", "summary+inline"),
        ("summary", "summary"),
        ("inline", "inline"),
    ])
    def test_each_documented_value_loads(self, monkeypatch, raw, expected):
        monkeypatch.setenv("PRXREF_POST_MODE", raw)
        assert load_config()["post_mode"] == expected

    @pytest.mark.parametrize("raw", [
        "Summary", "SUMMARY", "comments", "both", "neither", "inline-only",
        "summary ", " summary", "summary+inline ",
    ])
    def test_an_unknown_value_is_a_config_error_naming_the_variable(
        self, monkeypatch, raw
    ):
        """Values are matched exactly, whitespace included, so a misspelling
        cannot fall back to the default and post what nobody asked for."""
        monkeypatch.setenv("PRXREF_POST_MODE", raw)
        with pytest.raises(ConfigError, match="PRXREF_POST_MODE"):
            load_config()

    def test_an_override_is_validated_too_and_named_as_itself(self):
        with pytest.raises(ConfigError, match="post_mode") as exc:
            load_config(post_mode="everything")
        assert "PRXREF_POST_MODE" not in str(exc.value)

    def test_whitespace_only_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_POST_MODE", "   ")
        assert load_config()["post_mode"] == "summary+inline"

    def test_the_vocabulary_matches_the_orchestrator(self):
        """Config stays a leaf module, so the two lists are restated, not
        shared — this pin keeps a one-sided edit from splitting the contract."""
        from prxref import orchestrator

        assert set(config._POST_MODES) == set(orchestrator.POST_MODES)
        assert config._DEFAULTS["post_mode"] == "summary+inline"

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_POST_MODE", "summary")
        assert load_config(post_mode="inline")["post_mode"] == "inline"

    def test_the_env_name_is_derived_for_the_suite_wide_clear(self):
        assert "PRXREF_POST_MODE" in prxref_env_names()


class TestPostVerdict:
    """PRXREF_POST_VERDICT is a boolean, and ``_truthy`` is the only parser.

    Unlike the other bool keys it defaults ON: the verdict has always been
    posted, so the default must preserve that. The way to turn it off is any
    value other than the literal "1".
    """

    def test_defaults_to_on_so_behaviour_is_unchanged(self):
        assert load_config()["post_verdict"] is True

    def test_literal_one_enables_it(self, monkeypatch):
        monkeypatch.setenv("PRXREF_POST_VERDICT", "1")
        assert load_config()["post_verdict"] is True

    @pytest.mark.parametrize("raw", ["true", "True", "yes", "on", "0", "y"])
    def test_anything_but_the_literal_one_disables_it(self, monkeypatch, raw):
        """An empty value reads as UNSET and keeps the default; the dedicated
        whitespace test covers that direction."""
        monkeypatch.setenv("PRXREF_POST_VERDICT", raw)
        assert load_config()["post_verdict"] is False

    def test_it_uses_the_one_boolean_parser(self, monkeypatch):
        monkeypatch.setenv("PRXREF_POST_VERDICT", " 0 ")
        assert load_config()["post_verdict"] is config._truthy(" 0 ")

    def test_whitespace_only_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_POST_VERDICT", "   ")
        assert load_config()["post_verdict"] is True

    def test_it_is_a_bool_key_not_a_numeric_one(self):
        assert "post_verdict" in config._BOOL_KEYS
        assert "post_verdict" not in config._INT_KEYS | config._FLOAT_KEYS
        assert "post_verdict" not in config._RANGES

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_POST_VERDICT", "0")
        assert load_config(post_verdict=True)["post_verdict"] is True

    def test_the_env_name_is_derived_for_the_suite_wide_clear(self):
        assert "PRXREF_POST_VERDICT" in prxref_env_names()


class TestFailOnPolicy:
    """PRXREF_FAIL_ON: the review command's exit-code policy enum.

    The default is the standing advisory contract, so every value other than
    ``never`` is an opt-in — and a value outside the vocabulary must fail as a
    configuration error rather than fall back to the default, which would turn
    a typo into an undetected "never".
    """

    def test_defaults_to_never_so_behaviour_is_unchanged(self):
        assert load_config()["fail_on"] == "never"

    @pytest.mark.parametrize("raw", ["never", "error", "any"])
    def test_each_legal_value_loads(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_FAIL_ON", raw)
        assert load_config()["fail_on"] == raw

    @pytest.mark.parametrize("raw", [
        "sometimes", "ERROR", "Never", "ANY", "1", "on-error", "errors",
    ])
    def test_a_value_outside_the_vocabulary_is_a_config_error(
        self, monkeypatch, raw
    ):
        """Matching is exact, so it is case-sensitive too: an operator who
        wants the gate types it exactly as documented, and anything else is
        rejected rather than guessed at."""
        monkeypatch.setenv("PRXREF_FAIL_ON", raw)
        with pytest.raises(ConfigError, match="PRXREF_FAIL_ON"):
            load_config()

    def test_the_error_names_the_legal_values(self, monkeypatch):
        monkeypatch.setenv("PRXREF_FAIL_ON", "sometimes")
        with pytest.raises(ConfigError) as exc:
            load_config()
        for word in ("never", "error", "any"):
            assert word in str(exc.value)

    def test_an_empty_value_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_FAIL_ON", "")
        assert load_config()["fail_on"] == "never"

    def test_whitespace_only_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_FAIL_ON", "   ")
        assert load_config()["fail_on"] == "never"

    def test_it_is_a_string_key_outside_the_numeric_surface(self):
        """A choice key has no interval; it must not be swept into the range
        check, which would read any truthy string as a non-finite number."""
        assert config._DEFAULTS["fail_on"] == "never"
        assert "fail_on" not in config._INT_KEYS | config._FLOAT_KEYS
        assert "fail_on" not in config._RANGES

    def test_the_vocabulary_is_declared_in_the_choice_table(self):
        assert config._CHOICE_KEYS["fail_on"] == frozenset(
            {"never", "error", "any"}
        )

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_FAIL_ON", "never")
        assert load_config(fail_on="any")["fail_on"] == "any"

    def test_an_override_cannot_smuggle_a_value_outside_the_vocabulary(self):
        """Same rule as the ranges: the check runs after overrides too, and
        the message names the override's own key, not the environment."""
        with pytest.raises(ConfigError, match="fail_on") as exc:
            load_config(fail_on="sometimes")
        assert "PRXREF_FAIL_ON" not in str(exc.value)

    def test_every_choice_key_is_a_real_config_key(self):
        """Mirror of the coercion-set guard: a typo in ``_CHOICE_KEYS`` would
        validate a key nothing reads."""
        assert set(config._CHOICE_KEYS) - set(config._DEFAULTS) == set()


class TestErrorsNameTheirSource:
    """One rule: the message names whichever input supplied the bad value."""

    def test_the_environment_is_named_when_the_environment_supplied_it(
        self, monkeypatch
    ):
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "0")
        with pytest.raises(ConfigError, match="PRXREF_MAX_CHUNKS"):
            load_config()

    def test_a_caller_label_is_used_when_one_is_given(self):
        with pytest.raises(ConfigError, match="--max-chunks") as exc:
            load_config(max_chunks=0, source_labels={"max_chunks": "--max-chunks"})
        assert "PRXREF_MAX_CHUNKS" not in str(exc.value)

    def test_a_label_only_covers_the_key_it_names(self):
        """The label is per-key, so an unrelated override keeps its own name."""
        with pytest.raises(ConfigError, match="max_workers") as exc:
            load_config(max_workers=0, source_labels={"max_chunks": "--max-chunks"})
        assert "--max-chunks" not in str(exc.value)

    def test_a_label_for_a_key_that_was_not_overridden_is_inert(self, monkeypatch):
        """The env var supplied the value, so the env var is named — a label for
        an override that never happened must not hijack the message."""
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "0")
        with pytest.raises(ConfigError, match="PRXREF_MAX_CHUNKS") as exc:
            load_config(source_labels={"max_chunks": "--max-chunks"})
        assert "--max-chunks" not in str(exc.value)

    def test_labels_are_not_config_keys(self):
        """``source_labels`` is keyword-only and never lands in the result."""
        cfg = load_config(source_labels={"max_chunks": "--max-chunks"})
        assert "source_labels" not in cfg
        assert cfg["max_chunks"] == 8


_POSITIVE_INT_KNOBS = [
    ("llm_cli_concurrency", "PRXREF_LLM_CLI_CONCURRENCY", 2),
    ("review_rules_max_chars", "PRXREF_REVIEW_RULES_MAX_CHARS", 12000),
    ("ticket_context_max_chars", "PRXREF_TICKET_CONTEXT_MAX_CHARS", 6000),
]


class TestNewPositiveIntKnobs:
    """The three 0.14 caps that follow the size-knob rule: int, > 0, no ceiling."""

    @pytest.mark.parametrize("key,env,default", _POSITIVE_INT_KNOBS)
    def test_default(self, key, env, default):
        assert config._DEFAULTS[key] == default
        value = load_config()[key]
        assert value == default
        assert isinstance(value, int)

    @pytest.mark.parametrize("key,env,default", _POSITIVE_INT_KNOBS)
    def test_declared_in_the_int_and_range_tables(self, key, env, default):
        assert key in config._INT_KEYS
        assert config._RANGES[key] == config._Range(0)

    @pytest.mark.parametrize("key,env,default", _POSITIVE_INT_KNOBS)
    def test_env_coerces_to_an_int(self, monkeypatch, key, env, default):
        monkeypatch.setenv(env, " 7 ")
        value = load_config()[key]
        assert value == 7
        assert isinstance(value, int)

    @pytest.mark.parametrize("key,env,default", _POSITIVE_INT_KNOBS)
    def test_one_is_the_smallest_legal_value(self, monkeypatch, key, env, default):
        monkeypatch.setenv(env, "1")
        assert load_config()[key] == 1

    @pytest.mark.parametrize("key,env,default", _POSITIVE_INT_KNOBS)
    def test_whitespace_only_reads_as_unset(self, monkeypatch, key, env, default):
        monkeypatch.setenv(env, "   ")
        assert load_config()[key] == default

    @pytest.mark.parametrize("key,env,default", _POSITIVE_INT_KNOBS)
    @pytest.mark.parametrize("raw", ["lots", "2.5"])
    def test_malformed_value_names_the_variable(self, monkeypatch, key, env, default, raw):
        monkeypatch.setenv(env, raw)
        with pytest.raises(ConfigError, match=env):
            load_config()

    @pytest.mark.parametrize("key,env,default", _POSITIVE_INT_KNOBS)
    @pytest.mark.parametrize("raw", ["0", "-1"])
    def test_non_positive_value_rejected(self, monkeypatch, key, env, default, raw):
        monkeypatch.setenv(env, raw)
        with pytest.raises(ConfigError, match=env):
            load_config()

    @pytest.mark.parametrize("key,env,default", _POSITIVE_INT_KNOBS)
    def test_an_override_is_range_checked_and_named_as_itself(self, key, env, default):
        with pytest.raises(ConfigError, match=key) as exc:
            load_config(**{key: 0})
        assert env not in str(exc.value)

    @pytest.mark.parametrize("key,env,default", _POSITIVE_INT_KNOBS)
    def test_an_override_wins_over_the_environment(self, monkeypatch, key, env, default):
        monkeypatch.setenv(env, "3")
        assert load_config(**{key: 5})[key] == 5


_SIZE_THRESHOLDS = [
    ("size_warn_lines", "PRXREF_SIZE_WARN_LINES"),
    ("size_warn_files", "PRXREF_SIZE_WARN_FILES"),
]


class TestSizeWarnThresholds:
    """PRXREF_SIZE_WARN_LINES / _FILES: the second "None means off" class.

    Like ``llm_seed``, unset is ``None`` in the key's own type, and 0 is a legal
    value distinct from it (it flags any change at all), so the low bound is
    inclusive and ``_check_ranges`` skips the key only while it is unset.
    """

    @pytest.mark.parametrize("key,env", _SIZE_THRESHOLDS)
    def test_default_is_none_meaning_off(self, key, env):
        assert config._DEFAULTS[key] is None
        assert load_config()[key] is None

    @pytest.mark.parametrize("key,env", _SIZE_THRESHOLDS)
    def test_declared_in_the_int_and_range_tables(self, key, env):
        assert key in config._INT_KEYS
        assert config._RANGES[key] == config._Range(0, low_inclusive=True)

    @pytest.mark.parametrize("key,env", _SIZE_THRESHOLDS)
    @pytest.mark.parametrize("raw,expected", [("0", 0), ("250", 250), (" 40 ", 40)])
    def test_env_coerces_to_an_int(self, monkeypatch, key, env, raw, expected):
        monkeypatch.setenv(env, raw)
        value = load_config()[key]
        assert value == expected
        assert isinstance(value, int)

    @pytest.mark.parametrize("key,env", _SIZE_THRESHOLDS)
    def test_zero_is_distinct_from_unset(self, monkeypatch, key, env):
        monkeypatch.setenv(env, "0")
        assert load_config()[key] is not None

    @pytest.mark.parametrize("key,env", _SIZE_THRESHOLDS)
    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty_or_whitespace_reads_as_unset(self, monkeypatch, key, env, raw):
        monkeypatch.setenv(env, raw)
        assert load_config()[key] is None

    @pytest.mark.parametrize("key,env", _SIZE_THRESHOLDS)
    @pytest.mark.parametrize("raw", ["-1", "-500"])
    def test_negative_value_rejected(self, monkeypatch, key, env, raw):
        monkeypatch.setenv(env, raw)
        with pytest.raises(ConfigError, match=env):
            load_config()

    @pytest.mark.parametrize("key,env", _SIZE_THRESHOLDS)
    @pytest.mark.parametrize("raw", ["many", "1.5", "off"])
    def test_malformed_value_names_the_variable(self, monkeypatch, key, env, raw):
        monkeypatch.setenv(env, raw)
        with pytest.raises(ConfigError, match=env):
            load_config()

    @pytest.mark.parametrize("key,env", _SIZE_THRESHOLDS)
    def test_an_override_cannot_smuggle_a_negative_threshold(self, key, env):
        with pytest.raises(ConfigError, match=key) as exc:
            load_config(**{key: -1})
        assert env not in str(exc.value)

    @pytest.mark.parametrize("key,env", _SIZE_THRESHOLDS)
    def test_an_override_wins_over_the_environment(self, monkeypatch, key, env):
        monkeypatch.setenv(env, "100")
        assert load_config(**{key: 0})[key] == 0


class TestSizeIgnoreGlobs:
    """PRXREF_SIZE_IGNORE_GLOBS uses the one list grammar every list key shares."""

    def test_default_is_an_empty_list(self):
        assert config._DEFAULTS["size_ignore_globs"] == []
        assert load_config()["size_ignore_globs"] == []

    def test_declared_as_a_list_key(self):
        assert "size_ignore_globs" in config._LIST_KEYS
        assert "size_ignore_globs" not in config._RANGES

    @pytest.mark.parametrize("raw,expected", [
        ("*.snap", ["*.snap"]),
        ("*.snap,vendor/*", ["*.snap", "vendor/*"]),
        ("*.snap vendor/*", ["*.snap", "vendor/*"]),
        (" *.snap ,\n vendor/*\t*.pb.go ", ["*.snap", "vendor/*", "*.pb.go"]),
        ("docs/my?notes.md", ["docs/my?notes.md"]),
    ])
    def test_splits_on_commas_and_whitespace(self, monkeypatch, raw, expected):
        """A literal space in a glob is written ``?``, which survives the split."""
        monkeypatch.setenv("PRXREF_SIZE_IGNORE_GLOBS", raw)
        assert load_config()["size_ignore_globs"] == expected

    def test_whitespace_only_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_SIZE_IGNORE_GLOBS", "  \t ")
        assert load_config()["size_ignore_globs"] == []

    @pytest.mark.parametrize("key", sorted(config._LIST_KEYS))
    def test_a_default_list_is_not_shared_between_loads(self, key):
        """``load_config`` copies list defaults, so mutating one run's list
        cannot leak into ``_DEFAULTS`` and every later load."""
        before = list(config._DEFAULTS[key])
        load_config()[key].append("leak")
        assert config._DEFAULTS[key] == before
        assert load_config()[key] == before


class TestPostCost:
    """PRXREF_POST_COST is a boolean, and ``_truthy`` is the only boolean parser."""

    def test_defaults_to_off(self):
        assert config._DEFAULTS["post_cost"] is False
        assert load_config()["post_cost"] is False

    def test_literal_one_enables_it(self, monkeypatch):
        monkeypatch.setenv("PRXREF_POST_COST", "1")
        assert load_config()["post_cost"] is True

    @pytest.mark.parametrize("raw", ["true", "True", "yes", "on", "0", "y"])
    def test_only_the_literal_one_enables_it(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_POST_COST", raw)
        assert load_config()["post_cost"] is False

    def test_whitespace_only_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_POST_COST", "   ")
        assert load_config()["post_cost"] is False

    def test_it_is_a_bool_key_not_a_numeric_one(self):
        assert "post_cost" in config._BOOL_KEYS
        assert "post_cost" not in config._INT_KEYS | config._FLOAT_KEYS
        assert "post_cost" not in config._RANGES

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_POST_COST", "1")
        assert load_config(post_cost=False)["post_cost"] is False


_GPT_MINI = '{"openai/gpt-4o-mini": {"input": 0.15, "output": 0.60}}'


class TestPriceTable:
    """PRXREF_PRICE_TABLE: a string in, a validated ``dict[str, ModelPrice]`` out.

    ``_check_price_table`` runs inside ``load_config`` after every other check,
    so a malformed table is the exit-2 configuration error at load time, named
    after whichever input supplied it — never a mid-review surprise.
    """

    def test_default_is_no_table(self):
        assert config._DEFAULTS["price_table"] == ""
        assert load_config()["price_table"] == {}

    def test_it_is_in_no_coercion_table(self):
        assert "price_table" not in (
            config._INT_KEYS | config._FLOAT_KEYS | config._BOOL_KEYS | config._LIST_KEYS
        )
        assert "price_table" not in config._RANGES
        assert "price_table" not in config._CHOICE_KEYS

    def test_inline_json_is_parsed(self, monkeypatch):
        monkeypatch.setenv("PRXREF_PRICE_TABLE", _GPT_MINI)
        table = load_config()["price_table"]
        assert table == {"openai/gpt-4o-mini": costs.ModelPrice(0.15, 0.60)}
        assert isinstance(table["openai/gpt-4o-mini"], costs.ModelPrice)

    def test_inline_json_may_start_after_whitespace(self, monkeypatch):
        monkeypatch.setenv("PRXREF_PRICE_TABLE", "  " + _GPT_MINI)
        assert list(load_config()["price_table"]) == ["openai/gpt-4o-mini"]

    def test_a_file_path_is_read(self, monkeypatch, tmp_path):
        path = tmp_path / "prices.json"
        path.write_text('{"local/free": {"input": 0, "output": 0}}', encoding="utf-8")
        monkeypatch.setenv("PRXREF_PRICE_TABLE", str(path))
        assert load_config()["price_table"] == {"local/free": costs.ModelPrice(0.0, 0.0)}

    @pytest.mark.parametrize("raw", [
        "{not json",
        '{"m": {"input": 1}}',
        '{"m": {"input": 1, "ouput": 2}}',
        '{"m": {"input": -1, "output": 2}}',
        '{"m": {"input": true, "output": 2}}',
        '{"m": "cheap"}',
    ])
    def test_a_malformed_table_is_a_config_error_naming_the_variable(
        self, monkeypatch, raw
    ):
        monkeypatch.setenv("PRXREF_PRICE_TABLE", raw)
        with pytest.raises(ConfigError, match=r"^PRXREF_PRICE_TABLE: "):
            load_config()

    def test_a_missing_file_is_a_config_error_naming_the_variable(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("PRXREF_PRICE_TABLE", str(tmp_path / "absent.json"))
        with pytest.raises(ConfigError, match=r"^PRXREF_PRICE_TABLE: "):
            load_config()

    def test_whitespace_only_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_PRICE_TABLE", "   ")
        assert load_config()["price_table"] == {}

    def test_a_mapping_override_is_validated_like_json(self):
        cfg = load_config(price_table={"m": {"input": 1, "output": 2}})
        assert cfg["price_table"] == {"m": costs.ModelPrice(1.0, 2.0)}

    def test_a_bad_override_is_named_as_itself(self, monkeypatch):
        monkeypatch.setenv("PRXREF_PRICE_TABLE", _GPT_MINI)
        with pytest.raises(ConfigError, match=r"^price_table: ") as exc:
            load_config(price_table={"m": {"input": "cheap", "output": 2}})
        assert "PRXREF_PRICE_TABLE" not in str(exc.value)

    def test_a_caller_label_names_the_override(self):
        with pytest.raises(ConfigError, match=r"^caller-prices: "):
            load_config(
                price_table="{oops", source_labels={"price_table": "caller-prices"}
            )

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_PRICE_TABLE", _GPT_MINI)
        cfg = load_config(price_table='{"m": {"input": 3, "output": 4}}')
        assert cfg["price_table"] == {"m": costs.ModelPrice(3.0, 4.0)}

    def test_a_malformed_table_exits_2_before_the_review_runs(
        self, monkeypatch, capsys
    ):
        """Observed through the real entry point: ``prxref review`` resolves
        its config before orchestration, so a bad table is exit 2 with no run."""
        calls = []
        monkeypatch.setattr(cli, "_run_review", lambda *a, **k: calls.append(a))
        monkeypatch.setenv("PRXREF_PRICE_TABLE", "{not json")

        rc = cli.main(["review", "--pr-url", "https://github.com/org/repo/pull/7"])

        assert rc == 2
        _, err = capsys.readouterr()
        assert "configuration error" in err
        assert "PRXREF_PRICE_TABLE" in err
        assert calls == []

    def test_a_valid_table_reaches_the_review(self, monkeypatch):
        """Control for the exit-2 test: the same entry point with a good table
        gets past config and calls the review."""
        calls = []

        def _record(*args, **kwargs):
            calls.append(args)
            raise RuntimeError("stop after config")

        monkeypatch.setattr(cli, "_run_review", _record)
        monkeypatch.setenv("PRXREF_PRICE_TABLE", _GPT_MINI)

        cli.main(["review", "--pr-url", "https://github.com/org/repo/pull/7"])

        assert len(calls) == 1


_PLAIN_STRING_KEYS = [
    ("llm_cli_path", "PRXREF_LLM_CLI_PATH", "~/bin/claude"),
    ("review_rules", "PRXREF_REVIEW_RULES", ".prxref/rules.md"),
    ("ticket_context_file", "PRXREF_TICKET_CONTEXT_FILE", "ticket.md"),
    ("azure_devops_token", "PRXREF_AZURE_DEVOPS_TOKEN", "pat-value"),
    ("azure_devops_webhook_secret", "PRXREF_AZURE_DEVOPS_WEBHOOK_SECRET", "hook-secret"),
]


class TestNewStringKeys:
    """The 0.14 string keys: empty by default, passed through verbatim.

    Paths are only read later (``cli._run_review``, the backend factory), so
    ``load_config`` stays I/O-free for them and does no coercion.
    """

    @pytest.mark.parametrize("key,env,value", _PLAIN_STRING_KEYS)
    def test_default_is_empty(self, key, env, value):
        assert config._DEFAULTS[key] == ""
        assert load_config()[key] == ""

    @pytest.mark.parametrize("key,env,value", _PLAIN_STRING_KEYS)
    def test_in_no_coercion_table(self, key, env, value):
        assert key not in (
            config._INT_KEYS | config._FLOAT_KEYS | config._BOOL_KEYS | config._LIST_KEYS
        )
        assert key not in config._RANGES
        assert key not in config._CHOICE_KEYS

    @pytest.mark.parametrize("key,env,value", _PLAIN_STRING_KEYS)
    def test_env_value_passes_through(self, monkeypatch, key, env, value):
        monkeypatch.setenv(env, value)
        assert load_config()[key] == value

    @pytest.mark.parametrize("key,env,value", _PLAIN_STRING_KEYS)
    def test_whitespace_only_reads_as_unset(self, monkeypatch, key, env, value):
        monkeypatch.setenv(env, "   ")
        assert load_config()[key] == ""

    def test_a_missing_rules_file_is_not_read_at_load_time(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PRXREF_REVIEW_RULES", str(tmp_path / "absent.md"))
        monkeypatch.setenv("PRXREF_TICKET_CONTEXT_FILE", str(tmp_path / "absent.txt"))
        cfg = load_config()
        assert cfg["review_rules"].endswith("absent.md")
        assert cfg["ticket_context_file"].endswith("absent.txt")


class TestNewKeysAreClearedSuiteWide:
    @pytest.mark.parametrize("env", [
        "PRXREF_LLM_CLI_PATH", "PRXREF_LLM_CLI_CONCURRENCY", "PRXREF_PRICE_TABLE",
        "PRXREF_POST_COST", "PRXREF_SIZE_WARN_LINES", "PRXREF_SIZE_WARN_FILES",
        "PRXREF_SIZE_IGNORE_GLOBS", "PRXREF_REVIEW_RULES",
        "PRXREF_REVIEW_RULES_MAX_CHARS", "PRXREF_TICKET_CONTEXT_FILE",
        "PRXREF_TICKET_CONTEXT_MAX_CHARS", "PRXREF_AZURE_DEVOPS_TOKEN",
        "PRXREF_AZURE_DEVOPS_WEBHOOK_SECRET",
    ])
    def test_the_env_name_is_derived(self, env):
        assert env in prxref_env_names()


class TestLLMBackendIsNotAChoiceKey:
    def test_the_factory_owns_the_backend_vocabulary(self):
        """The factory lower-cases the value, so an exact-match choice table
        here would reject ``Claude-CLI`` that the factory accepts."""
        assert "llm_backend" not in config._CHOICE_KEYS

    def test_a_mixed_case_backend_loads(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "Claude-CLI")
        assert load_config()["llm_backend"] == "Claude-CLI"


_KEYS_0_15 = [
    ("dedup_similarity", "PRXREF_DEDUP_SIMILARITY"),
    ("prompts_dir", "PRXREF_PROMPTS_DIR"),
    ("scoped_rules", "PRXREF_SCOPED_RULES"),
    ("scoped_rules_max_chars", "PRXREF_SCOPED_RULES_MAX_CHARS"),
    ("group_findings", "PRXREF_GROUP_FINDINGS"),
    ("max_warning_findings", "PRXREF_MAX_WARNING_FINDINGS"),
    ("max_outofscope_findings", "PRXREF_MAX_OUTOFSCOPE_FINDINGS"),
    ("max_findings_per_rule", "PRXREF_MAX_FINDINGS_PER_RULE"),
]


class TestDedupSimilarity:
    """PRXREF_DEDUP_SIMILARITY (#10): a 0-1 Jaccard threshold, off when unset.

    ``None`` is the declared unset, like the size thresholds, so the
    reworded-duplicate pass runs only when an operator asks for it. The low
    bound is open: 0 would merge every pair of findings on a line.
    """

    def test_default_is_none_meaning_off(self):
        assert config._DEFAULTS["dedup_similarity"] is None
        assert load_config()["dedup_similarity"] is None

    def test_declared_as_a_float_key_with_an_open_low_bound(self):
        assert "dedup_similarity" in config._FLOAT_KEYS
        assert "dedup_similarity" not in config._INT_KEYS
        assert config._RANGES["dedup_similarity"] == config._Range(0.0, 1.0)
        assert config._RANGES["dedup_similarity"].low_inclusive is False

    @pytest.mark.parametrize("raw,expected", [
        ("0.5", 0.5), ("1", 1.0), ("1.0", 1.0), (" 0.75 ", 0.75), ("0.01", 0.01),
    ])
    def test_env_coerces_to_a_float(self, monkeypatch, raw, expected):
        monkeypatch.setenv("PRXREF_DEDUP_SIMILARITY", raw)
        value = load_config()["dedup_similarity"]
        assert value == expected
        assert isinstance(value, float)

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty_or_whitespace_reads_as_unset(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_DEDUP_SIMILARITY", raw)
        assert load_config()["dedup_similarity"] is None

    @pytest.mark.parametrize("raw", ["0", "0.0", "-0.1", "1.01", "2", "nan", "inf"])
    def test_out_of_band_value_rejected(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_DEDUP_SIMILARITY", raw)
        with pytest.raises(ConfigError, match=r"^PRXREF_DEDUP_SIMILARITY: "):
            load_config()

    @pytest.mark.parametrize("raw", ["high", "50%", "0,5"])
    def test_malformed_value_names_the_variable(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_DEDUP_SIMILARITY", raw)
        with pytest.raises(ConfigError, match=r"^PRXREF_DEDUP_SIMILARITY: "):
            load_config()

    def test_message_states_the_bound_it_broke(self, monkeypatch):
        monkeypatch.setenv("PRXREF_DEDUP_SIMILARITY", "0")
        with pytest.raises(ConfigError) as exc:
            load_config()
        assert "greater than 0.0" in str(exc.value)
        assert "at most 1.0" in str(exc.value)

    @pytest.mark.parametrize("value", [0, 0.0, 1.5, -1, float("nan"), True])
    def test_an_override_cannot_smuggle_an_out_of_band_value(self, value):
        with pytest.raises(ConfigError, match=r"^dedup_similarity: ") as exc:
            load_config(dedup_similarity=value)
        assert "PRXREF_DEDUP_SIMILARITY" not in str(exc.value)

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_DEDUP_SIMILARITY", "0.9")
        assert load_config(dedup_similarity=0.5)["dedup_similarity"] == 0.5


class TestPromptsDir:
    """PRXREF_PROMPTS_DIR (#11): a path string, ``None`` when unset.

    The directory is only read by the prompt-template loader, so
    ``load_config`` stays I/O-free for it and passes the value through.
    """

    def test_default_is_none(self):
        assert config._DEFAULTS["prompts_dir"] is None
        assert load_config()["prompts_dir"] is None

    def test_in_no_coercion_table(self):
        assert "prompts_dir" not in (
            config._INT_KEYS | config._FLOAT_KEYS | config._BOOL_KEYS | config._LIST_KEYS
        )
        assert "prompts_dir" not in config._RANGES
        assert "prompts_dir" not in config._CHOICE_KEYS

    @pytest.mark.parametrize("raw", [".prxref/prompts", "team prompts/v2", "~/prompts"])
    def test_env_value_passes_through_verbatim(self, monkeypatch, raw):
        """Not a list key: a path containing a space survives whole."""
        monkeypatch.setenv("PRXREF_PROMPTS_DIR", raw)
        assert load_config()["prompts_dir"] == raw

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty_or_whitespace_reads_as_unset(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_PROMPTS_DIR", raw)
        assert load_config()["prompts_dir"] is None

    def test_a_missing_dir_is_not_read_at_load_time(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PRXREF_PROMPTS_DIR", str(tmp_path / "absent"))
        assert load_config()["prompts_dir"].endswith("absent")

    def test_an_empty_string_override_arrives_as_the_empty_string(self, monkeypatch):
        """Only ``None`` overrides are ignored, so a ``--prompts-dir ""`` style
        override reaches the caller as ``""``: consumers treat both ``None`` and
        ``""`` as off."""
        monkeypatch.setenv("PRXREF_PROMPTS_DIR", ".prxref/prompts")
        assert load_config(prompts_dir="")["prompts_dir"] == ""

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_PROMPTS_DIR", "env-dir")
        assert load_config(prompts_dir="flag-dir")["prompts_dir"] == "flag-dir"


class TestScopedRules:
    """PRXREF_SCOPED_RULES (#12): the fourth list key, empty by default."""

    def test_default_is_an_empty_list(self):
        assert config._DEFAULTS["scoped_rules"] == []
        assert load_config()["scoped_rules"] == []

    def test_declared_as_a_list_key(self):
        assert "scoped_rules" in config._LIST_KEYS
        assert "scoped_rules" not in config._RANGES
        assert "scoped_rules" not in config._CHOICE_KEYS

    @pytest.mark.parametrize("raw,expected", [
        (".prxref/rules", [".prxref/rules"]),
        ("rules/java.md,rules/helm.md", ["rules/java.md", "rules/helm.md"]),
        ("rules/java.md rules/helm.md", ["rules/java.md", "rules/helm.md"]),
        (" rules/java.md ,\n .prxref/scoped\t", ["rules/java.md", ".prxref/scoped"]),
    ])
    def test_splits_on_commas_and_whitespace(self, monkeypatch, raw, expected):
        monkeypatch.setenv("PRXREF_SCOPED_RULES", raw)
        assert load_config()["scoped_rules"] == expected

    def test_whitespace_only_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_SCOPED_RULES", "  \t ")
        assert load_config()["scoped_rules"] == []

    @pytest.mark.parametrize("value", [[""], ["team rules/java.md"]])
    def test_an_override_list_is_taken_as_given(self, value):
        """No split and no blank-dropping on the override path: a flag can name
        a path with a space, and the loader drops blank entries itself."""
        assert load_config(scoped_rules=value)["scoped_rules"] == value

    def test_an_override_replaces_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_SCOPED_RULES", "a.md,b.md")
        assert load_config(scoped_rules=["c.md"])["scoped_rules"] == ["c.md"]


class TestScopedRulesMaxChars:
    """PRXREF_SCOPED_RULES_MAX_CHARS (#12): the per-unit total, int > 0."""

    def test_default(self):
        assert config._DEFAULTS["scoped_rules_max_chars"] == 24000
        value = load_config()["scoped_rules_max_chars"]
        assert value == 24000
        assert isinstance(value, int)

    def test_declared_in_the_int_and_range_tables(self):
        assert "scoped_rules_max_chars" in config._INT_KEYS
        assert config._RANGES["scoped_rules_max_chars"] == config._Range(0)

    @pytest.mark.parametrize("raw,expected", [(" 7 ", 7), ("1", 1), ("10000000", 10_000_000)])
    def test_env_coerces_to_an_int_with_no_invented_ceiling(
        self, monkeypatch, raw, expected
    ):
        monkeypatch.setenv("PRXREF_SCOPED_RULES_MAX_CHARS", raw)
        value = load_config()["scoped_rules_max_chars"]
        assert value == expected
        assert isinstance(value, int)

    def test_whitespace_only_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_SCOPED_RULES_MAX_CHARS", "   ")
        assert load_config()["scoped_rules_max_chars"] == 24000

    @pytest.mark.parametrize("raw", ["lots", "2.5", "0", "-1"])
    def test_malformed_or_non_positive_value_names_the_variable(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_SCOPED_RULES_MAX_CHARS", raw)
        with pytest.raises(ConfigError, match=r"^PRXREF_SCOPED_RULES_MAX_CHARS: "):
            load_config()

    def test_an_override_is_range_checked_and_named_as_itself(self):
        with pytest.raises(ConfigError, match=r"^scoped_rules_max_chars: ") as exc:
            load_config(scoped_rules_max_chars=0)
        assert "PRXREF_SCOPED_RULES_MAX_CHARS" not in str(exc.value)

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_SCOPED_RULES_MAX_CHARS", "3")
        assert load_config(scoped_rules_max_chars=5)["scoped_rules_max_chars"] == 5


class TestGroupFindings:
    """PRXREF_GROUP_FINDINGS (#13): explicit opt-in, parsed by ``_truthy``."""

    def test_defaults_to_off(self):
        assert config._DEFAULTS["group_findings"] is False
        assert load_config()["group_findings"] is False

    def test_literal_one_enables_it(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GROUP_FINDINGS", "1")
        assert load_config()["group_findings"] is True

    @pytest.mark.parametrize("raw", ["true", "True", "yes", "on", "0", "y"])
    def test_only_the_literal_one_enables_it(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_GROUP_FINDINGS", raw)
        assert load_config()["group_findings"] is False

    def test_whitespace_only_reads_as_unset(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GROUP_FINDINGS", "   ")
        assert load_config()["group_findings"] is False

    def test_it_is_a_bool_key_not_a_numeric_one(self):
        assert "group_findings" in config._BOOL_KEYS
        assert "group_findings" not in config._INT_KEYS | config._FLOAT_KEYS
        assert "group_findings" not in config._RANGES

    def test_an_override_wins_over_the_environment(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GROUP_FINDINGS", "1")
        assert load_config(group_findings=False)["group_findings"] is False


_SEVERITY_CAPS = [
    ("max_warning_findings", "PRXREF_MAX_WARNING_FINDINGS"),
    ("max_outofscope_findings", "PRXREF_MAX_OUTOFSCOPE_FINDINGS"),
]


class TestPerSeverityCaps:
    """PRXREF_MAX_WARNING_FINDINGS / _OUTOFSCOPE_FINDINGS (#13).

    ``None`` means unlimited, in the key's own type, because 0 is a legal cap
    that drops every finding of that severity. So the low bound is inclusive
    and ``_check_ranges`` skips the key only while it is unset.
    """

    @pytest.mark.parametrize("key,env", _SEVERITY_CAPS)
    def test_default_is_none_meaning_unlimited(self, key, env):
        assert config._DEFAULTS[key] is None
        assert load_config()[key] is None

    @pytest.mark.parametrize("key,env", _SEVERITY_CAPS)
    def test_declared_in_the_int_and_range_tables(self, key, env):
        assert key in config._INT_KEYS
        assert config._RANGES[key] == config._Range(0, low_inclusive=True)

    @pytest.mark.parametrize("key,env", _SEVERITY_CAPS)
    @pytest.mark.parametrize(
        "raw,expected", [("0", 0), ("3", 3), (" 12 ", 12), ("100000", 100_000)]
    )
    def test_env_coerces_to_an_int(self, monkeypatch, key, env, raw, expected):
        monkeypatch.setenv(env, raw)
        value = load_config()[key]
        assert value == expected
        assert isinstance(value, int)

    @pytest.mark.parametrize("key,env", _SEVERITY_CAPS)
    def test_zero_is_distinct_from_unset(self, monkeypatch, key, env):
        monkeypatch.setenv(env, "0")
        assert load_config()[key] == 0

    @pytest.mark.parametrize("key,env", _SEVERITY_CAPS)
    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty_or_whitespace_reads_as_unset(self, monkeypatch, key, env, raw):
        monkeypatch.setenv(env, raw)
        assert load_config()[key] is None

    @pytest.mark.parametrize("key,env", _SEVERITY_CAPS)
    @pytest.mark.parametrize("raw", ["-1", "-50"])
    def test_negative_value_rejected(self, monkeypatch, key, env, raw):
        monkeypatch.setenv(env, raw)
        with pytest.raises(ConfigError, match=rf"^{env}: "):
            load_config()

    @pytest.mark.parametrize("key,env", _SEVERITY_CAPS)
    @pytest.mark.parametrize("raw", ["many", "1.5", "off"])
    def test_malformed_value_names_the_variable(self, monkeypatch, key, env, raw):
        monkeypatch.setenv(env, raw)
        with pytest.raises(ConfigError, match=rf"^{env}: "):
            load_config()

    @pytest.mark.parametrize("key,env", _SEVERITY_CAPS)
    def test_an_override_cannot_smuggle_a_negative_cap(self, key, env):
        with pytest.raises(ConfigError, match=rf"^{key}: ") as exc:
            load_config(**{key: -1})
        assert env not in str(exc.value)

    @pytest.mark.parametrize("key,env", _SEVERITY_CAPS)
    def test_an_override_wins_over_the_environment(self, monkeypatch, key, env):
        monkeypatch.setenv(env, "10")
        assert load_config(**{key: 0})[key] == 0

    def test_the_caps_are_independent_of_each_other_and_of_the_error_cap(
        self, monkeypatch
    ):
        monkeypatch.setenv("PRXREF_MAX_WARNING_FINDINGS", "2")
        cfg = load_config()
        assert cfg["max_warning_findings"] == 2
        assert cfg["max_outofscope_findings"] is None
        assert cfg["max_error_findings"] == DEFAULT_MAX_ERRORS


class TestKeys015ReachTheEntryPoint:
    """Observed through ``prxref review``: a bad 0.15 value is exit 2 naming
    its variable, before the review runs; a good one gets past config."""

    @pytest.mark.parametrize("env,raw", [
        ("PRXREF_DEDUP_SIMILARITY", "0"),
        ("PRXREF_SCOPED_RULES_MAX_CHARS", "0"),
        ("PRXREF_MAX_WARNING_FINDINGS", "-1"),
        ("PRXREF_MAX_OUTOFSCOPE_FINDINGS", "few"),
        ("PRXREF_MAX_FINDINGS_PER_RULE", "-1"),
    ])
    def test_a_bad_value_exits_2_before_the_review_runs(
        self, monkeypatch, capsys, env, raw
    ):
        calls = []
        monkeypatch.setattr(cli, "_run_review", lambda *a, **k: calls.append(a))
        monkeypatch.setenv(env, raw)

        rc = cli.main(["review", "--pr-url", "https://github.com/org/repo/pull/7"])

        assert rc == 2
        _, err = capsys.readouterr()
        assert f"configuration error: {env}: " in err
        assert calls == []

    def test_good_values_reach_the_review(self, monkeypatch):
        calls = []

        def _record(*args, **kwargs):
            calls.append(args)
            raise RuntimeError("stop after config")

        monkeypatch.setattr(cli, "_run_review", _record)
        monkeypatch.setenv("PRXREF_DEDUP_SIMILARITY", "0.5")
        monkeypatch.setenv("PRXREF_SCOPED_RULES_MAX_CHARS", "100")
        monkeypatch.setenv("PRXREF_MAX_WARNING_FINDINGS", "0")
        monkeypatch.setenv("PRXREF_MAX_OUTOFSCOPE_FINDINGS", "3")
        monkeypatch.setenv("PRXREF_GROUP_FINDINGS", "1")
        monkeypatch.setenv("PRXREF_MAX_FINDINGS_PER_RULE", "0")

        cli.main(["review", "--pr-url", "https://github.com/org/repo/pull/7"])

        assert len(calls) == 1


def _doc_entry(surface: str, env: str) -> str:
    """The one entry that documents ``env`` on ``surface``, whitespace-folded."""
    text = SURFACES[surface]
    if surface == "docs/env-vars.md":
        entries = [ln for ln in text.splitlines() if ln.startswith(f"| `{env}` |")]
    elif surface == ".env.example":
        entries = [p for p in text.split("\n\n") if re.search(rf"^# {env}=", p, re.M)]
    else:
        entries = [
            m.group(0)
            for m in re.finditer(
                rf"^  {env}\b.*?(?=^  PRXREF_|^\S|\n\n)", text, re.M | re.S
            )
        ]
    assert len(entries) == 1, f"{surface}: expected one entry for {env}, got {len(entries)}"
    return " ".join(entries[0].split())


class TestKeys015AreDocumented:
    """The four-surface rule, tightened for the 0.15 keys.

    ``test_docs_consistency`` checks a SUBSTRING, so ``PRXREF_SCOPED_RULES``
    would pass on the strength of ``PRXREF_SCOPED_RULES_MAX_CHARS`` alone.
    """

    @pytest.mark.parametrize("key,env", _KEYS_0_15)
    def test_the_env_name_is_derived_for_the_suite_wide_clear(self, key, env):
        assert config._ENV_PREFIX + key.upper() == env
        assert env in prxref_env_names()

    @pytest.mark.parametrize("surface", sorted(SURFACES))
    @pytest.mark.parametrize("key,env", _KEYS_0_15)
    def test_each_surface_has_one_entry_that_names_the_release(self, key, env, surface):
        assert "0.15.0" in _doc_entry(surface, env)

    @pytest.mark.parametrize("surface", sorted(SURFACES))
    def test_the_outofscope_cap_says_it_is_not_ticket_scope_out(self, surface):
        entry = _doc_entry(surface, "PRXREF_MAX_OUTOFSCOPE_FINDINGS")
        assert re.search(r"\bnot\W+ticket scope\W+out\b", entry, re.I), entry
