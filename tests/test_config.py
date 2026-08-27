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

        It is stored as a string because "unset" and "0.0" are different
        requests: an empty value omits ``temperature`` from the payload
        entirely — some endpoints reject it alongside reasoning parameters —
        and no float can encode "omit me". Coercing it would force a numeric
        default and destroy that distinction. Its parse and its bound are not
        skipped, only moved: ``llm_backends._float_setting`` performs both when
        the client is built, and raises the same ``ConfigError`` naming the
        same variable.
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
