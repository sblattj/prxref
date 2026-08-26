"""Tests for prxref.config: env loading, type coercion, overrides, and forge factory."""
import pytest
import requests

from prxref.config import load_config, make_forge
from prxref.forges import bitbucket, github, gitlab
from prxref.forges.base import PRRef
from prxref.quality import DEFAULT_MAX_ERRORS

_ALL_ENV_KEYS = [
    "PRXREF_LLM_BACKEND",
    "PRXREF_LLM_BASE_URL",
    "PRXREF_LLM_API_KEY",
    "PRXREF_LLM_MODELS",
    "PRXREF_CONFIDENCE_FLOOR",
    "PRXREF_MAX_ERRORS",
    "PRXREF_MAX_ERROR_FINDINGS",
    "PRXREF_MAX_CHUNKS",
    "PRXREF_BITBUCKET_TOKEN",
    "PRXREF_BITBUCKET_USER",
    "PRXREF_BITBUCKET_APP_PASSWORD",
    "PRXREF_GITHUB_TOKEN",
    "PRXREF_GITHUB_ENTERPRISE_TOKEN",
    "PRXREF_GITLAB_TOKEN",
    "PRXREF_BITBUCKET_WEBHOOK_SECRET",
    "PRXREF_GITHUB_WEBHOOK_SECRET",
    "PRXREF_GITLAB_WEBHOOK_SECRET",
    "PRXREF_ALLOW_UNSIGNED",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ALL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


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

    def test_invalid_int_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "not-a-number")
        with pytest.raises(ValueError, match="PRXREF_MAX_CHUNKS"):
            load_config()

    def test_invalid_float_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("PRXREF_CONFIDENCE_FLOOR", "high")
        with pytest.raises(ValueError, match="PRXREF_CONFIDENCE_FLOOR"):
            load_config()


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

