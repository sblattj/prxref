"""Issue #26: the litellm backend forwards ``reasoning_effort``, and
``PRXREF_LLM_SEED=off`` sends no seed at all.

Effort: ``LiteLLMClient`` passes ``reasoning_effort=`` to ``litellm.completion``
when it is set, like ``OpenAICompatClient`` does, and the factory hands it the
configured ``PRXREF_LLM_REASONING_EFFORT``. Unset, the kwargs are exactly what
0.18.0 sent.

Seed: ``off`` (lowercase, matched like config's other word values) means no
configured seed and no once-per-process fallback, on both HTTP backends. The
client's ``seed`` is ``None``, so the run record reports ``null``. Unset keeps
the auto seed, an integer still wins, and any other word is still exit 2
naming PRXREF_LLM_SEED.
"""
from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import pytest

import prxref.llm_backends
import prxref.llm_cli_backends
from prxref import config
from prxref.config import load_config
from prxref.forges.base import PRRef
from prxref.llm import ConfigError
from prxref.llm_backends import (
    SEED_OFF,
    LiteLLMClient,
    OpenAICompatClient,
    create_llm_client,
)
from prxref.orchestrator import orchestrate_review
from tests.test_llm_backends import _resp, _ScriptedSession
from tests.test_orchestrator import FakeForge, make_pr

NO_FINDINGS = json.dumps({"findings": [], "escalations": []})

# The kwargs 0.18.0 sent to litellm.completion from a factory-built client
# with no effort configured: the fixed request, plus the resolved
# temperature and seed.
BASE_KWARGS = {"model", "messages", "max_tokens", "num_retries", "timeout", "fallbacks"}
FACTORY_KWARGS = BASE_KWARGS | {"temperature", "seed"}

REF = PRRef(
    forge="fake", host="fake.test", owner="acme", repo="widget",
    number=7, url="https://fake.test/acme/widget/pull/7",
)

DIFF = (
    "diff --git a/src/a.py b/src/a.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/a.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+data 1\n"
    "+data 2\n"
)


def _fake_litellm(monkeypatch, content: str = "ok") -> list[dict]:
    """Install a fake ``litellm`` module; returns the kwargs of every completion call."""
    captured: list[dict] = []

    def completion(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=None,
            model=kwargs["model"],
        )

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(completion=completion))
    return captured


def _litellm_env(monkeypatch) -> None:
    monkeypatch.setenv("PRXREF_LLM_BACKEND", "litellm")
    monkeypatch.setenv("PRXREF_LLM_MODELS", "m1,m2")


def _openai_env(monkeypatch) -> None:
    monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
    monkeypatch.setenv("PRXREF_LLM_MODELS", "m1")


class TestLiteLLMEffort:
    """``reasoning_effort`` reaches ``litellm.completion`` only when set."""

    def test_the_factory_forwards_the_configured_effort(self, monkeypatch):
        captured = _fake_litellm(monkeypatch)
        _litellm_env(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_REASONING_EFFORT", "medium")
        client = create_llm_client()
        assert isinstance(client, LiteLLMClient)
        assert client.reasoning_effort == "medium"
        client.invoke("sys", "usr")
        assert captured[0]["reasoning_effort"] == "medium"
        assert set(captured[0]) == FACTORY_KWARGS | {"reasoning_effort"}

    def test_a_cfg_effort_wins_over_the_environment(self, monkeypatch):
        captured = _fake_litellm(monkeypatch)
        _litellm_env(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_REASONING_EFFORT", "low")
        create_llm_client({"llm_reasoning_effort": "high"}).invoke("sys", "usr")
        assert captured[0]["reasoning_effort"] == "high"

    def test_the_value_is_forwarded_unvalidated(self, monkeypatch):
        """Provider vocabulary, like openai-compat: prxref does not check it."""
        captured = _fake_litellm(monkeypatch)
        LiteLLMClient(models=["p"], reasoning_effort="max").invoke("sys", "usr")
        assert captured[0]["reasoning_effort"] == "max"

    def test_unset_effort_leaves_the_kwargs_as_they_were(self, monkeypatch):
        captured = _fake_litellm(monkeypatch)
        _litellm_env(monkeypatch)
        client = create_llm_client()
        assert client.reasoning_effort is None
        client.invoke("sys", "usr")
        assert "reasoning_effort" not in captured[0]
        assert set(captured[0]) == FACTORY_KWARGS

    def test_empty_effort_is_omitted_like_openai_compat(self, monkeypatch):
        captured = _fake_litellm(monkeypatch)
        _litellm_env(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_REASONING_EFFORT", "")
        create_llm_client().invoke("sys", "usr")
        assert set(captured[0]) == FACTORY_KWARGS

    def test_a_directly_built_client_sends_the_bare_request(self, monkeypatch):
        """No temperature, seed or effort: only the fixed request keys."""
        captured = _fake_litellm(monkeypatch)
        client = LiteLLMClient(models=["p", "fb"], reasoning_effort="")
        assert client.reasoning_effort is None
        client.invoke("sys", "usr")
        assert set(captured[0]) == BASE_KWARGS

    def test_normalised_like_openai_compat(self, monkeypatch):
        _fake_litellm(monkeypatch)
        for raw in (None, ""):
            via_litellm = LiteLLMClient(models=["p"], reasoning_effort=raw)
            compat = OpenAICompatClient(
                base_url="https://llm.test/v1", api_key="k", models=["p"],
                reasoning_effort=raw,
            )
            assert via_litellm.reasoning_effort is compat.reasoning_effort is None


class TestSeedOffAtTheFactory:
    """``off`` sends no seed on either HTTP backend."""

    def test_off_on_litellm_sends_no_seed(self, monkeypatch):
        captured = _fake_litellm(monkeypatch)
        _litellm_env(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_SEED", "off")
        client = create_llm_client()
        assert client.seed is None
        client.invoke("sys", "usr")
        assert "seed" not in captured[0]
        assert set(captured[0]) == FACTORY_KWARGS - {"seed"}

    def test_off_on_openai_compat_sends_no_seed(self, monkeypatch):
        _openai_env(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_SEED", "off")
        s = _ScriptedSession(_resp())
        client = create_llm_client(session=s)
        assert isinstance(client, OpenAICompatClient)
        assert client.seed is None
        client.invoke("sys", "usr")
        assert "seed" not in s.calls[0]["json"]
        assert s.calls[0]["json"]["temperature"] == 0.0

    def test_off_does_not_touch_the_process_seed(self, monkeypatch):
        """No fallback is derived: the once-per-process seed is never asked for."""
        _openai_env(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_SEED", "off")

        def boom():
            raise AssertionError("_auto_run_seed called for PRXREF_LLM_SEED=off")

        monkeypatch.setattr(prxref.llm_backends, "_auto_run_seed", boom)
        assert create_llm_client().seed is None

    def test_off_through_the_cfg_override(self, monkeypatch):
        _openai_env(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_SEED", "5")
        assert create_llm_client({"LLM_SEED": "off"}).seed is None
        assert create_llm_client({"llm_seed": "off"}).seed is None

    def test_surrounding_whitespace_is_stripped(self, monkeypatch):
        _openai_env(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_SEED", "  off ")
        assert create_llm_client().seed is None

    def test_off_on_a_cli_backend_is_not_an_error(self, monkeypatch):
        """The CLI backends send no seed anyway; ``off`` must not exit 2 there."""
        built = []
        monkeypatch.setattr(
            prxref.llm_cli_backends, "build_cli_client",
            lambda *a, **kw: built.append(kw) or object(),
        )
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "claude-cli")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "m1")
        monkeypatch.setenv("PRXREF_LLM_SEED", "off")
        create_llm_client()
        assert len(built) == 1

    def test_unset_still_derives_the_auto_seed(self, monkeypatch):
        captured = _fake_litellm(monkeypatch)
        _litellm_env(monkeypatch)
        client = create_llm_client()
        assert isinstance(client.seed, int)
        assert client.seed == prxref.llm_backends._run_seed
        client.invoke("sys", "usr")
        assert captured[0]["seed"] == client.seed

    def test_an_integer_is_sent_verbatim(self, monkeypatch):
        captured = _fake_litellm(monkeypatch)
        _litellm_env(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_SEED", "7")
        client = create_llm_client()
        assert client.seed == 7
        client.invoke("sys", "usr")
        assert captured[0]["seed"] == 7

    @pytest.mark.parametrize("raw", ["bogus", "OFF", "Off", "none"])
    def test_any_other_word_names_the_variable(self, monkeypatch, raw):
        """Lowercase only: config's word values are matched exactly."""
        _openai_env(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_SEED", raw)
        with pytest.raises(ConfigError, match="PRXREF_LLM_SEED"):
            create_llm_client()


class TestSeedOffInTheConfigLoader:
    """``off`` is a new value of the existing ``llm_seed`` key, not a new key."""

    def test_off_from_the_environment_is_accepted(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_SEED", "off")
        assert load_config()["llm_seed"] == "off"

    def test_off_with_whitespace_is_accepted(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_SEED", " off ")
        assert load_config()["llm_seed"] == "off"

    def test_off_as_an_override_is_accepted(self):
        assert load_config(llm_seed="off")["llm_seed"] == "off"

    @pytest.mark.parametrize("raw", ["bogus", "OFF", "7.5"])
    def test_a_bad_env_value_is_still_exit_2(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_LLM_SEED", raw)
        with pytest.raises(ConfigError, match="PRXREF_LLM_SEED"):
            load_config()

    @pytest.mark.parametrize("value", ["OFF", "bogus", -1])
    def test_a_bad_override_is_reported_as_the_override(self, value):
        with pytest.raises(ConfigError, match="llm_seed") as exc:
            load_config(llm_seed=value)
        assert "PRXREF_LLM_SEED" not in str(exc.value)

    def test_integers_and_unset_are_unchanged(self, monkeypatch):
        assert load_config()["llm_seed"] is None
        monkeypatch.setenv("PRXREF_LLM_SEED", "7")
        assert load_config()["llm_seed"] == 7

    def test_no_key_was_added(self):
        assert "llm_seed" in config._INT_KEYS
        assert "llm_seed" in config._RANGES
        assert config._DEFAULTS["llm_seed"] is None

    def test_the_two_spellings_of_off_agree(self):
        """config restates the factory's word because it is a leaf module."""
        assert config._SEED_OFF == SEED_OFF == "off"

    def test_a_loaded_off_reaches_the_factory(self, monkeypatch):
        """The CLI's route: load_config, then create_llm_client(cfg)."""
        captured = _fake_litellm(monkeypatch)
        _litellm_env(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_SEED", "off")
        client = create_llm_client(load_config())
        assert client.seed is None
        client.invoke("sys", "usr")
        assert "seed" not in captured[0]


class TestSeedOffInTheRunRecord:
    """With ``off`` the run record's ``sampling.seed`` is null."""

    def test_litellm_run_records_a_null_seed(self, monkeypatch):
        _fake_litellm(monkeypatch, content=NO_FINDINGS)
        _litellm_env(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_SEED", "off")
        client = create_llm_client()
        result = orchestrate_review(FakeForge(pr=make_pr(), diff=DIFF), REF, client, post=False)
        assert not result.get("error")
        assert result["sampling"] == {"temperature": 0.0, "seed": None, "models": ["m1", "m2"]}

    def test_openai_compat_run_records_a_null_seed(self, monkeypatch):
        _openai_env(monkeypatch)
        monkeypatch.setenv("PRXREF_LLM_SEED", "off")
        s = _ScriptedSession(*[_resp(text=NO_FINDINGS) for _ in range(8)])
        client = create_llm_client(session=s)
        result = orchestrate_review(
            FakeForge(pr=make_pr(), diff=DIFF), REF, client, post=False, max_workers=1,
        )
        assert result["sampling"]["seed"] is None
        assert s.calls and all("seed" not in c["json"] for c in s.calls)

    def test_control_unset_records_the_auto_seed(self, monkeypatch):
        _fake_litellm(monkeypatch, content=NO_FINDINGS)
        _litellm_env(monkeypatch)
        client = create_llm_client()
        result = orchestrate_review(FakeForge(pr=make_pr(), diff=DIFF), REF, client, post=False)
        assert isinstance(result["sampling"]["seed"], int)
        assert result["sampling"]["seed"] == client.seed
