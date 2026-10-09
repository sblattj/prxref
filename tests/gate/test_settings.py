"""Gate config keys and GateSettings (#102)."""

import os
from pathlib import Path

import pytest

from prxref import config
from prxref.gate import settings
from prxref.llm import ConfigError

GATE_KEYS = {
    "rules_source", "gate_app_id", "gate_private_key_file", "gate_installation_ids",
    "gate_repos", "gate_webhook_secret", "gate_webhook_secret_previous",
    "gate_secret_grace_minutes", "gate_state_file", "gate_runs_dir",
    "gate_review_deadline", "gate_watchdog_seconds", "gate_reconcile_minutes",
}


def _key_file(tmp_path: Path, mode: int = 0o600) -> Path:
    path = tmp_path / "app.pem"
    path.write_text("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    path.chmod(mode)
    return path


def _env(monkeypatch, tmp_path, **extra):
    values = {
        "PRXREF_GATE_APP_ID": "12345",
        "PRXREF_GATE_INSTALLATION_IDS": "777,778",
        "PRXREF_GATE_REPOS": "SBlattJ/sdlc-sandbox",
        "PRXREF_GATE_WEBHOOK_SECRET": "s3cret",
        "PRXREF_GATE_STATE_FILE": str(tmp_path / "gate.sqlite3"),
        "PRXREF_GATE_RUNS_DIR": str(tmp_path / "runs"),
    }
    if "PRXREF_GATE_PRIVATE_KEY_FILE" not in extra:
        values["PRXREF_GATE_PRIVATE_KEY_FILE"] = str(_key_file(tmp_path))
    values.update(extra)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_every_gate_key_is_a_default_and_env_only():
    assert GATE_KEYS <= set(config._DEFAULTS)
    assert GATE_KEYS <= config.ENV_ONLY_KEYS
    assert not GATE_KEYS & set(config.FILE_KEYS)


def test_defaults():
    cfg = config.load_config()
    assert cfg["rules_source"] == "head"
    assert cfg["gate_secret_grace_minutes"] == 30
    assert cfg["gate_review_deadline"] == 900
    assert cfg["gate_watchdog_seconds"] == 60
    assert cfg["gate_reconcile_minutes"] == 10
    assert cfg["gate_installation_ids"] == []


def test_rules_source_rejects_a_value_outside_its_vocabulary(monkeypatch):
    monkeypatch.setenv("PRXREF_RULES_SOURCE", "merge")
    with pytest.raises(ConfigError, match="PRXREF_RULES_SOURCE"):
        config.load_config()


def test_review_deadline_must_be_positive(monkeypatch):
    monkeypatch.setenv("PRXREF_GATE_REVIEW_DEADLINE", "0")
    with pytest.raises(ConfigError, match="PRXREF_GATE_REVIEW_DEADLINE"):
        config.load_config()


def test_settings_from_a_complete_environment(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    s = settings.settings_from_config(config.load_config())
    assert s.app_id == 12345
    assert s.installation_ids == frozenset({777, 778})
    assert s.repos == frozenset({"sblattj/sdlc-sandbox"})
    assert s.review_deadline == 900
    assert s.state_file == tmp_path / "gate.sqlite3"


@pytest.mark.parametrize("name, value", [
    ("PRXREF_GATE_APP_ID", ""),
    ("PRXREF_GATE_APP_ID", "sdlc"),
    ("PRXREF_GATE_INSTALLATION_IDS", ""),
    ("PRXREF_GATE_INSTALLATION_IDS", "77x"),
    ("PRXREF_GATE_REPOS", ""),
    ("PRXREF_GATE_REPOS", "no-slash"),
    ("PRXREF_GATE_WEBHOOK_SECRET", ""),
    ("PRXREF_GATE_STATE_FILE", ""),
    ("PRXREF_GATE_RUNS_DIR", ""),
    ("PRXREF_GATE_PRIVATE_KEY_FILE", "/nonexistent/app.pem"),
])
def test_a_missing_or_malformed_value_names_its_variable(monkeypatch, tmp_path, name, value):
    _env(monkeypatch, tmp_path, **{name: value})
    with pytest.raises(ConfigError, match=name):
        settings.settings_from_config(config.load_config())


def test_a_group_or_world_readable_key_file_is_refused(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, PRXREF_GATE_PRIVATE_KEY_FILE=str(_key_file(tmp_path, 0o644)))
    with pytest.raises(ConfigError, match="0600"):
        settings.settings_from_config(config.load_config())


def test_env_file_is_parsed_and_mode_checked(tmp_path):
    env = tmp_path / "env"
    env.write_text(
        "# comment\n\nexport PRXREF_GATE_APP_ID=12345\nAWS_REGION_NAME='us-west-2'\n"
        'PRXREF_LLM_MODELS="bedrock/us.anthropic.claude-opus-4-6-v1"\n'
    )
    env.chmod(0o600)
    assert settings.load_env_file(env) == {
        "PRXREF_GATE_APP_ID": "12345",
        "AWS_REGION_NAME": "us-west-2",
        "PRXREF_LLM_MODELS": "bedrock/us.anthropic.claude-opus-4-6-v1",
    }
    env.chmod(0o640)
    with pytest.raises(ConfigError, match="0600"):
        settings.load_env_file(env)


def test_env_file_rejects_a_line_without_equals(tmp_path):
    env = tmp_path / "env"
    env.write_text("JUSTANAME\n")
    env.chmod(0o600)
    with pytest.raises(ConfigError, match="line 1"):
        settings.load_env_file(env)


def test_apply_env_file_returns_names_not_values(monkeypatch, tmp_path):
    env = tmp_path / "env"
    env.write_text("PRXREF_GATE_WEBHOOK_SECRET=topsecret\n")
    env.chmod(0o600)
    monkeypatch.delenv("PRXREF_GATE_WEBHOOK_SECRET", raising=False)
    names = settings.apply_env_file(env)
    assert names == ["PRXREF_GATE_WEBHOOK_SECRET"]
    assert os.environ["PRXREF_GATE_WEBHOOK_SECRET"] == "topsecret"
