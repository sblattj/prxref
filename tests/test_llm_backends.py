"""Tests for prxref.llm_backends: fast fallback loop, request shape, factory, litellm guard."""
from __future__ import annotations

import inspect
import json as json_module
import logging
import pathlib
import sys
import time
import types
from types import SimpleNamespace

import pytest
import requests

import prxref.llm_backends
from prxref.llm import ConfigError
from prxref.llm_backends import (
    LiteLLMClient,
    LLMError,
    OpenAICompatClient,
    create_llm_client,
)


def _streamable(resp):
    """Attach the streaming interface a real ``requests.Response`` exposes.

    The client reads the body through ``iter_content()`` under a wall-clock
    deadline, so any HTTP-response fake has to answer it. Doing this at the
    fake-session boundary keeps every individual response fixture free of
    transport plumbing.
    """
    if hasattr(resp, "iter_content"):
        return resp
    try:
        body = json_module.dumps(resp.json()).encode()
    except Exception:
        body = getattr(resp, "text", "") or ""
        body = body.encode() if isinstance(body, str) else body
    resp.iter_content = lambda chunk_size=None: iter([body])
    resp.close = lambda: None
    return resp


def _resp(status_code=200, model="m1-resolved", prompt=11, completion=7, text="ok", **extra):
    payload = {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "model": model,
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }
    payload.update(extra)
    body = json_module.dumps(payload).encode()
    # The client streams the body under a wall-clock deadline, so a fake
    # response has to answer iter_content()/close() the way a real one does.
    return SimpleNamespace(
        status_code=status_code, payload=payload, json=lambda: payload,
        iter_content=lambda chunk_size=None: iter([body]),
        close=lambda: None,
    )


class _ScriptedSession:
    """Returns queued responses/exceptions in order; records every request."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None, stream=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout, "stream": stream})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _streamable(item)


def _client(session, models=("m1", "m2")):
    return OpenAICompatClient(
        base_url="https://llm.test/v1/",
        api_key="local",
        models=list(models),
        session=session,
        default_timeout=45.0,
    )


class _PayloadCapturingSession:
    """Appends every posted ``json=`` payload; fails the first call when ``fail_first``."""

    def __init__(self, captured, fail_first):
        self.captured = captured
        self.fail_first = fail_first

    def post(self, url, json=None, headers=None, timeout=None, stream=None):
        self.captured.append(json)
        if self.fail_first and len(self.captured) == 1:
            return _resp(status_code=500)
        return _resp()


def _client_capturing_payload(
    models=("m1",), reasoning_effort=None, fail_first=False, temperature=None,
    seed=None,
):
    """Build an OpenAICompatClient whose session records posted payloads; returns (client, captured)."""
    captured: list[dict] = []
    client = OpenAICompatClient(
        base_url="https://llm.test/v1/",
        api_key="local",
        models=list(models),
        session=_PayloadCapturingSession(captured, fail_first),
        default_timeout=45.0,
        reasoning_effort=reasoning_effort,
        temperature=temperature,
        seed=seed,
    )
    return client, captured


class TestFallbackLoop:
    def test_advances_on_500(self):
        s = _ScriptedSession(_resp(status_code=500), _resp())
        r = _client(s).invoke("sys", "usr")
        assert r.backend == "openai-compat"
        assert r.model == "m1-resolved"
        assert len(s.calls) == 2
        assert s.calls[0]["json"]["model"] == "m1"
        assert s.calls[1]["json"]["model"] == "m2"

    def test_advances_on_429(self):
        s = _ScriptedSession(_resp(status_code=429), _resp())
        assert _client(s).invoke("sys", "usr").text == "ok"
        assert len(s.calls) == 2

    def test_advances_on_timeout(self):
        s = _ScriptedSession(requests.exceptions.Timeout("t"), _resp())
        assert _client(s).invoke("sys", "usr").text == "ok"
        assert len(s.calls) == 2

    def test_advances_on_connection_error(self):
        s = _ScriptedSession(requests.exceptions.ConnectionError("c"), _resp())
        assert _client(s).invoke("sys", "usr").text == "ok"
        assert len(s.calls) == 2

    def test_all_models_fail_raises_with_reasons(self):
        s = _ScriptedSession(
            _resp(status_code=500),
            requests.exceptions.Timeout("t"),
        )
        with pytest.raises(LLMError) as exc:
            _client(s).invoke("sys", "usr")
        assert "all models failed" in str(exc.value)
        assert "m1: HTTP 500" in str(exc.value)
        assert "m2: timeout" in str(exc.value)

    def test_single_model_tried_once_no_retry(self):
        s = _ScriptedSession(_resp(status_code=503))
        with pytest.raises(LLMError):
            _client(s, models=("only",)).invoke("sys", "usr")
        assert len(s.calls) == 1

    def test_malformed_success_body_advances(self):
        bad = SimpleNamespace(status_code=200, json=lambda: {"nope": True})
        s = _ScriptedSession(bad, _resp())
        assert _client(s).invoke("sys", "usr").text == "ok"
        assert len(s.calls) == 2


class TestRequestShape:
    def test_posts_openai_shape(self):
        s = _ScriptedSession(_resp())
        _client(s).invoke("be system", "be user", max_tokens=99, timeout_s=12)
        call = s.calls[0]
        assert call["url"] == "https://llm.test/v1/chat/completions"
        assert call["headers"] == {"Authorization": "Bearer local"}
        # (connect, read): the configured value is the READ budget.
        assert call["timeout"][1] == 12
        assert call["timeout"][0] <= 12
        assert call["json"]["model"] == "m1"
        assert call["json"]["max_tokens"] == 99
        assert call["json"]["messages"] == [
            {"role": "system", "content": "be system"},
            {"role": "user", "content": "be user"},
        ]

    def test_json_mode_sets_response_format(self):
        s = _ScriptedSession(_resp())
        _client(s).invoke("sys", "usr", json_mode=True)
        assert s.calls[0]["json"]["response_format"] == {"type": "json_object"}

    def test_no_json_mode_omits_response_format(self):
        s = _ScriptedSession(_resp())
        _client(s).invoke("sys", "usr", json_mode=False)
        assert "response_format" not in s.calls[0]["json"]

    def test_uses_default_timeout_when_unset(self):
        s = _ScriptedSession(_resp())
        _client(s).invoke("sys", "usr")
        assert s.calls[0]["timeout"][1] == 45.0
        assert s.calls[0]["timeout"][0] <= 45.0


class TestUsageMapping:
    def test_usage_and_model_mapping(self):
        s = _ScriptedSession(_resp(model="vendor/gemini-flash", prompt=120, completion=45, text="body"))
        r = _client(s).invoke("sys", "usr")
        assert r.text == "body"
        assert r.input_tokens == 120
        assert r.output_tokens == 45
        assert r.model == "vendor/gemini-flash"
        assert r.backend == "openai-compat"
        assert r.elapsed_ms >= 0


class TestReasoningEffort:
    def test_absent_by_default(self):
        client, captured = _client_capturing_payload()
        client.invoke(system="s", user="u")
        assert "reasoning_effort" not in captured[0]

    def test_sent_when_configured(self):
        client, captured = _client_capturing_payload(reasoning_effort="low")
        client.invoke(system="s", user="u")
        assert captured[0]["reasoning_effort"] == "low"

    def test_applies_to_every_model_in_the_chain(self):
        client, captured = _client_capturing_payload(
            models=["a", "b"], reasoning_effort="low", fail_first=True
        )
        client.invoke(system="s", user="u")
        assert [c.get("reasoning_effort") for c in captured] == ["low", "low"]

    def test_empty_string_normalises_to_none(self):
        client, captured = _client_capturing_payload(reasoning_effort="")
        client.invoke(system="s", user="u")
        assert "reasoning_effort" not in captured[0]


class TestTemperature:
    """Unset temperature must leave the key out of the payload entirely."""

    def test_absent_by_default(self):
        client, captured = _client_capturing_payload()
        client.invoke(system="s", user="u")
        assert "temperature" not in captured[0]

    def test_sent_when_configured(self):
        client, captured = _client_capturing_payload(temperature=0.2)
        client.invoke(system="s", user="u")
        assert captured[0]["temperature"] == 0.2

    def test_zero_is_a_real_value_not_an_omission(self):
        client, captured = _client_capturing_payload(temperature=0.0)
        client.invoke(system="s", user="u")
        assert captured[0]["temperature"] == 0.0

    def test_applies_to_every_model_in_the_chain(self):
        client, captured = _client_capturing_payload(
            models=["a", "b"], temperature=0.7, fail_first=True
        )
        client.invoke(system="s", user="u")
        assert [c.get("temperature") for c in captured] == [0.7, 0.7]


class TestSeed:
    """Constructor-level seed handling mirrors temperature: None omits the key,
    and 0 is a real seed, never an omission."""

    def test_absent_by_default(self):
        client, captured = _client_capturing_payload()
        client.invoke(system="s", user="u")
        assert "seed" not in captured[0]

    def test_sent_when_configured(self):
        client, captured = _client_capturing_payload(seed=7)
        client.invoke(system="s", user="u")
        assert captured[0]["seed"] == 7

    def test_zero_is_a_real_value_not_an_omission(self):
        client, captured = _client_capturing_payload(seed=0)
        client.invoke(system="s", user="u")
        assert captured[0]["seed"] == 0

    def test_applies_to_every_model_in_the_chain(self):
        client, captured = _client_capturing_payload(
            models=["a", "b"], seed=7, fail_first=True
        )
        client.invoke(system="s", user="u")
        assert [c.get("seed") for c in captured] == [7, 7]


class TestNoShippedDefaults:
    def test_unset_endpoint_raises_config_error(self, monkeypatch):
        for v in ("PRXREF_LLM_BASE_URL", "PRXREF_LLM_MODELS", "PRXREF_LLM_API_KEY"):
            monkeypatch.delenv(v, raising=False)
        with pytest.raises(ConfigError) as exc:
            create_llm_client()
        assert "PRXREF_LLM_BASE_URL" in str(exc.value)

    def test_unset_models_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.delenv("PRXREF_LLM_MODELS", raising=False)
        with pytest.raises(ConfigError) as exc:
            create_llm_client()
        assert "PRXREF_LLM_MODELS" in str(exc.value)

    def test_no_private_host_or_lane_names_in_source(self):
        src = pathlib.Path(inspect.getfile(prxref.llm_backends)).read_text()
        for token in ("8090", "flash,orch", "llm-ferry"):
            assert token not in src, f"private default {token!r} still in source"

    def test_explicit_config_still_builds(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "a,b")
        c = create_llm_client()
        assert c.base_url == "https://llm.test/v1"
        assert c.models == ["a", "b"]

    def test_api_key_may_be_empty(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "a")
        monkeypatch.delenv("PRXREF_LLM_API_KEY", raising=False)
        assert create_llm_client().api_key == ""


class TestCreateLLMClient:
    def test_ferry_alias_maps_to_openai_compat(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "ferry")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "a,b")
        assert isinstance(create_llm_client(), OpenAICompatClient)

    def test_litellm_selection(self, monkeypatch):
        fake = types.SimpleNamespace(completion=lambda **kw: None)
        monkeypatch.setitem(sys.modules, "litellm", fake)
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "litellm")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "a,b")
        assert isinstance(create_llm_client(), LiteLLMClient)

    def test_unknown_backend_raises(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "skynet")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "a,b")
        with pytest.raises(LLMError):
            create_llm_client()

    def test_env_overrides(self, monkeypatch):
        monkeypatch.delenv("PRXREF_LLM_BACKEND", raising=False)
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://override.test/v1")
        monkeypatch.setenv("PRXREF_LLM_API_KEY", "sekrit")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "a, b ,c")
        c = create_llm_client()
        assert c.base_url == "https://override.test/v1"
        assert c.api_key == "sekrit"
        assert c.models == ["a", "b", "c"]

    def test_whitespace_only_models_env_raises(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.setenv("PRXREF_LLM_MODELS", " , ")
        with pytest.raises(ConfigError):
            create_llm_client()

    def test_reasoning_effort_defaults_to_none(self, monkeypatch):
        monkeypatch.delenv("PRXREF_LLM_REASONING_EFFORT", raising=False)
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "a,b")
        c = create_llm_client()
        assert c.reasoning_effort is None

    def test_reasoning_effort_env_var_passed_through(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_REASONING_EFFORT", "low")
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "a,b")
        c = create_llm_client()
        assert c.reasoning_effort == "low"


class TestBudgetKnobsFromConfig:
    """PRXREF_LLM_TIMEOUT / _TEMPERATURE reach the built client and the wire."""

    @pytest.fixture(autouse=True)
    def _endpoint(self, monkeypatch):
        """A reachable endpoint for every test here. Env clearing: tests/conftest.py."""
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "https://llm.test/v1")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "a")

    def test_timeout_defaults_to_45(self):
        assert create_llm_client().default_timeout == 45.0

    def test_timeout_from_env_reaches_session_post(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_TIMEOUT", "12.5")
        s = _ScriptedSession(_resp())
        create_llm_client(session=s).invoke("sys", "usr")
        assert s.calls[0]["timeout"][1] == 12.5

    def test_timeout_from_cfg_beats_env(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_TIMEOUT", "12.5")
        s = _ScriptedSession(_resp())
        create_llm_client({"llm_timeout": 3.0}, session=s).invoke("sys", "usr")
        assert s.calls[0]["timeout"][1] == 3.0
        # A deadline smaller than the connect budget clamps it.
        assert s.calls[0]["timeout"][0] <= 3.0

    def test_default_temperature_is_sent_as_zero(self):
        """The reproducibility default: an operator who configured nothing
        still gets temperature 0.0 on the wire. This is the fix for the
        vanished-finding report — identical diff, same model, same verdict —
        and it only holds if the field actually reaches the request."""
        s = _ScriptedSession(_resp())
        create_llm_client(session=s).invoke("sys", "usr")
        assert s.calls[0]["json"]["temperature"] == 0.0

    def test_the_factory_resolves_the_default_constant(self):
        client = create_llm_client()
        assert client.temperature == prxref.llm_backends.DEFAULT_TEMPERATURE == 0.0
        assert client.seed is None

    def test_temperature_from_env_reaches_the_payload(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_TEMPERATURE", "0.35")
        s = _ScriptedSession(_resp())
        create_llm_client(session=s).invoke("sys", "usr")
        assert s.calls[0]["json"]["temperature"] == 0.35

    def test_empty_temperature_env_reads_as_the_default(self, monkeypatch):
        """The same rule as every other env read: empty = unset, and unset now
        resolves to the 0.0 default rather than an omission."""
        monkeypatch.setenv("PRXREF_LLM_TEMPERATURE", "   ")
        s = _ScriptedSession(_resp())
        create_llm_client(session=s).invoke("sys", "usr")
        assert s.calls[0]["json"]["temperature"] == 0.0

    def test_seed_unset_is_omitted_from_the_payload(self):
        s = _ScriptedSession(_resp())
        create_llm_client(session=s).invoke("sys", "usr")
        assert "seed" not in s.calls[0]["json"]

    def test_seed_from_env_reaches_the_payload_as_an_int(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_SEED", "42")
        s = _ScriptedSession(_resp())
        create_llm_client(session=s).invoke("sys", "usr")
        assert s.calls[0]["json"]["seed"] == 42
        assert isinstance(s.calls[0]["json"]["seed"], int)

    def test_zero_seed_is_legal_and_reaches_the_payload(self, monkeypatch):
        """0 is a valid seed; only an absent value means "omit the key"."""
        monkeypatch.setenv("PRXREF_LLM_SEED", "0")
        s = _ScriptedSession(_resp())
        create_llm_client(session=s).invoke("sys", "usr")
        assert s.calls[0]["json"]["seed"] == 0

    def test_seed_from_cfg_beats_env(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_SEED", "3")
        s = _ScriptedSession(_resp())
        create_llm_client({"llm_seed": 9}, session=s).invoke("sys", "usr")
        assert s.calls[0]["json"]["seed"] == 9

    def test_malformed_seed_names_the_variable(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_SEED", "deterministic")
        with pytest.raises(ConfigError, match="PRXREF_LLM_SEED"):
            create_llm_client()

    def test_negative_seed_rejected_at_the_factory_too(self, monkeypatch):
        """Config already rejects it; the factory re-checks for standalone use."""
        monkeypatch.setenv("PRXREF_LLM_SEED", "-1")
        with pytest.raises(ConfigError, match="PRXREF_LLM_SEED"):
            create_llm_client()

    def test_malformed_temperature_names_the_variable(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_TEMPERATURE", "hot")
        with pytest.raises(ConfigError, match="PRXREF_LLM_TEMPERATURE"):
            create_llm_client()

    def test_malformed_timeout_names_the_variable(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_TIMEOUT", "soon")
        with pytest.raises(ConfigError, match="PRXREF_LLM_TIMEOUT"):
            create_llm_client()

    @pytest.mark.parametrize("raw", ["-0.1", "nan", "inf", "-inf"])
    def test_out_of_range_temperature_rejected(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_LLM_TEMPERATURE", raw)
        with pytest.raises(ConfigError, match="PRXREF_LLM_TEMPERATURE"):
            create_llm_client()

    @pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf"])
    def test_out_of_range_timeout_rejected(self, monkeypatch, raw):
        monkeypatch.setenv("PRXREF_LLM_TIMEOUT", raw)
        with pytest.raises(ConfigError, match="PRXREF_LLM_TIMEOUT"):
            create_llm_client()

    @pytest.mark.parametrize("raw", ["0", "0.0"])
    def test_zero_temperature_is_legal_and_reaches_the_payload(self, monkeypatch, raw):
        """0 is a real temperature; only an absent value means "omit the key"."""
        monkeypatch.setenv("PRXREF_LLM_TEMPERATURE", raw)
        s = _ScriptedSession(_resp())
        create_llm_client(session=s).invoke("sys", "usr")
        assert s.calls[0]["json"]["temperature"] == 0.0

    def test_litellm_call_kwargs_carry_the_configured_timeout_and_temperature(
        self, monkeypatch
    ):
        """Regression: create_llm_client built LiteLLMClient without default_timeout.

        Asserted at the wire (the kwargs litellm.completion actually receives),
        not at the constructor attribute, so a break anywhere between the two
        fails this test.
        """
        captured: list[dict] = []

        def fake_completion(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                usage=None,
                model="m",
            )

        monkeypatch.setenv("PRXREF_LLM_BACKEND", "litellm")
        monkeypatch.setenv("PRXREF_LLM_TIMEOUT", "7.5")
        monkeypatch.setenv("PRXREF_LLM_TEMPERATURE", "0.4")
        monkeypatch.setitem(
            sys.modules, "litellm", types.SimpleNamespace(completion=fake_completion)
        )
        create_llm_client().invoke("sys", "usr")
        assert captured[0]["timeout"] == 7.5
        assert captured[0]["temperature"] == 0.4

    def test_litellm_call_kwargs_carry_the_configured_seed_too(self, monkeypatch):
        """The seed shares the temperature's path: factory -> client -> kwargs."""
        captured: list[dict] = []

        def fake_completion(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                usage=None,
                model="m",
            )

        monkeypatch.setenv("PRXREF_LLM_BACKEND", "litellm")
        monkeypatch.setenv("PRXREF_LLM_SEED", "77")
        monkeypatch.setitem(
            sys.modules, "litellm", types.SimpleNamespace(completion=fake_completion)
        )
        create_llm_client().invoke("sys", "usr")
        assert captured[0]["seed"] == 77


class TestLiteLLMClient:
    def test_import_error_points_at_extra(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "litellm", None)
        with pytest.raises(LLMError, match=r"prxref\[litellm\]"):
            LiteLLMClient(models=["m"])

    def _installed(self, monkeypatch):
        captured = []

        def fake_completion(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="lit-ok"))],
                usage=SimpleNamespace(prompt_tokens=8, completion_tokens=4),
                model="bedrock/big",
            )

        monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(completion=fake_completion))
        return LiteLLMClient(models=["primary", "fb1", "fb2"]), captured

    def test_invoke_passes_native_fallbacks(self, monkeypatch):
        client, captured = self._installed(monkeypatch)
        client.invoke("sys", "usr", json_mode=True, timeout_s=9)
        kw = captured[0]
        assert kw["model"] == "primary"
        assert kw["fallbacks"] == ["fb1", "fb2"]
        assert kw["num_retries"] == 0
        assert kw["timeout"] == 9
        assert kw["response_format"] == {"type": "json_object"}
        assert kw["max_tokens"] == 4096
        assert kw["messages"][0]["role"] == "system"

    def test_invoke_maps_usage(self, monkeypatch):
        client, _ = self._installed(monkeypatch)
        r = client.invoke("sys", "usr")
        assert r.text == "lit-ok"
        assert r.input_tokens == 8
        assert r.output_tokens == 4
        assert r.model == "bedrock/big"
        assert r.backend == "litellm"
        assert r.elapsed_ms >= 0

    def test_default_timeout_used_when_unset(self, monkeypatch):
        client, captured = self._installed(monkeypatch)
        client.invoke("sys", "usr")
        assert captured[0]["timeout"] == 45.0

    def test_temperature_omitted_when_unset(self, monkeypatch):
        client, captured = self._installed(monkeypatch)
        client.invoke("sys", "usr")
        assert "temperature" not in captured[0]

    def test_temperature_sent_when_configured(self, monkeypatch):
        captured = []

        def fake_completion(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="lit-ok"))],
                usage=None,
                model="m",
            )

        monkeypatch.setitem(
            sys.modules, "litellm", types.SimpleNamespace(completion=fake_completion)
        )
        LiteLLMClient(models=["p"], temperature=0.25).invoke("sys", "usr")
        assert captured[0]["temperature"] == 0.25

    def test_seed_sent_when_configured(self, monkeypatch):
        captured = []

        def fake_completion(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="lit-ok"))],
                usage=None,
                model="m",
            )

        monkeypatch.setitem(
            sys.modules, "litellm", types.SimpleNamespace(completion=fake_completion)
        )
        LiteLLMClient(models=["p"], seed=11).invoke("sys", "usr")
        assert captured[0]["seed"] == 11


class _FakeBadRequestError(Exception):
    """Mimics litellm's own exception shape: a ``.message`` and a ``.model``.

    Real litellm ``BadRequestError``/``NotFoundError`` instances carry both,
    which is how the fix identifies which model in the chain actually failed
    without depending on a reliable ``.status_code``.
    """

    def __init__(self, message: str, model: str | None = None):
        super().__init__(message)
        self.message = message
        self.model = model


class _FakeHttpError(Exception):
    """A litellm-shaped exception carrying only ``.status_code`` (no ``.model``,
    and a class name that does not itself say BadRequest/NotFound)."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class TestLiteLLMClientSkipsPermanentlyUnavailableModel:
    """Mirrors OpenAICompatClient's run-lifetime unavailable-model cache
    (issue 09), keyed on litellm's exception shape instead of a status code.
    """

    def test_second_invoke_filters_the_unavailable_primary(self, monkeypatch, caplog):
        calls: list[dict] = []

        def fake_completion(**kwargs):
            calls.append(kwargs)
            if kwargs["model"] == "primary":
                raise _FakeBadRequestError(
                    "The requested model is not available for this integrator",
                    model="primary",
                )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="lit-ok"))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
                model="fb1-resolved",
            )

        monkeypatch.setitem(
            sys.modules, "litellm", types.SimpleNamespace(completion=fake_completion)
        )
        client = LiteLLMClient(models=["primary", "fb1"])
        with caplog.at_level(logging.WARNING, logger="prxref.llm_backends"):
            with pytest.raises(_FakeBadRequestError):
                client.invoke("sys", "usr")
            r2 = client.invoke("sys", "usr")
        assert r2.text == "lit-ok"
        assert calls[0]["model"] == "primary"
        assert calls[0]["fallbacks"] == ["fb1"]
        assert calls[1]["model"] == "fb1"
        assert calls[1]["fallbacks"] == []
        skip_msgs = [
            rec.getMessage() for rec in caplog.records
            if "skipping for the rest of the run" in rec.getMessage()
        ]
        assert len(skip_msgs) == 1, skip_msgs
        assert "primary" in skip_msgs[0]

    def test_all_unavailable_raises_without_calling_litellm(self, monkeypatch):
        calls: list[dict] = []

        def fake_completion(**kwargs):
            calls.append(kwargs)
            raise _FakeBadRequestError("model_not_found: dead", model="dead")

        monkeypatch.setitem(
            sys.modules, "litellm", types.SimpleNamespace(completion=fake_completion)
        )
        client = LiteLLMClient(models=["dead"])
        with pytest.raises(_FakeBadRequestError):
            client.invoke("sys", "usr")
        with pytest.raises(LLMError) as exc:
            client.invoke("sys", "usr")
        assert len(calls) == 1
        assert "skipped (unavailable)" in str(exc.value)
        assert "dead" in str(exc.value)

    def test_status_code_alone_signals_unavailable(self, monkeypatch):
        """No ``BadRequest``/``NotFound`` in the class name; a 4xx
        ``.status_code`` plus a phrase match is sufficient on its own."""
        def fake_completion(**kwargs):
            raise _FakeHttpError("no such model: primary", status_code=404)

        monkeypatch.setitem(
            sys.modules, "litellm", types.SimpleNamespace(completion=fake_completion)
        )
        client = LiteLLMClient(models=["primary", "fb1"])
        with pytest.raises(_FakeHttpError):
            client.invoke("sys", "usr")
        assert client._unavailable == {"primary"}


class TestLiteLLMClientControlsUnaffected:
    """Transient/ambiguous exceptions must keep retrying every invoke."""

    def test_a_generic_exception_does_not_filter(self, monkeypatch):
        calls: list[dict] = []

        def fake_completion(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("connection reset by peer")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="lit-ok"))],
                usage=None,
                model="m",
            )

        monkeypatch.setitem(
            sys.modules, "litellm", types.SimpleNamespace(completion=fake_completion)
        )
        client = LiteLLMClient(models=["primary", "fb1"])
        with pytest.raises(RuntimeError):
            client.invoke("sys", "usr")
        client.invoke("sys", "usr")
        assert calls[1]["model"] == "primary"
        assert calls[1]["fallbacks"] == ["fb1"]

    def test_5xx_status_code_with_a_matching_phrase_is_not_cached(self, monkeypatch):
        """Only a 4xx signal qualifies -- mirrors OpenAICompatClient's rule
        that a 5xx is never cached even when its body happens to use one of
        the unavailable-phrase words."""
        def fake_completion(**kwargs):
            raise _FakeHttpError("model deprecated, retry later", status_code=503)

        monkeypatch.setitem(
            sys.modules, "litellm", types.SimpleNamespace(completion=fake_completion)
        )
        client = LiteLLMClient(models=["primary"])
        with pytest.raises(_FakeHttpError):
            client.invoke("sys", "usr")
        assert client._unavailable == set()


_ABSENT = object()


def _resp_with_finish(finish_reason, text="ok", status_code=200, model="m1-resolved"):
    """A well-formed success body whose choice carries ``finish_reason``.

    ``_resp`` puts its ``**extra`` at the top level of the body; the stop
    reason lives on the CHOICE, so it needs its own builder.
    """
    choice: dict = {"message": {"role": "assistant", "content": text}}
    if finish_reason is not _ABSENT:
        choice["finish_reason"] = finish_reason
    payload = {
        "choices": [choice],
        "model": model,
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    return SimpleNamespace(status_code=status_code, payload=payload, json=lambda: payload)


class TestFinishReasonOpenAICompat:
    """The provider's stop reason is the only signal that a chunk was truncated.

    Without it, a response cut off mid-JSON and a model that simply declined to
    emit JSON are the same ``JSONDecodeError`` to everyone downstream.
    """

    def test_length_is_carried_through(self):
        """A lone model that truncates exhausts the chain into the last-resort
        return, which is exactly where the carried-through reason is asserted."""
        s = _ScriptedSession(_resp_with_finish("length"))
        r = _client(s, models=("m1",)).invoke("sys", "usr")
        assert r.finish_reason == "length"

    def test_stop_is_carried_through_not_normalised_away(self):
        """Control for the test above: a clean stop must NOT read as 'length'."""
        s = _ScriptedSession(_resp_with_finish("stop"))
        assert _client(s).invoke("sys", "usr").finish_reason == "stop"

    def test_absent_finish_reason_is_empty_string(self):
        s = _ScriptedSession(_resp_with_finish(_ABSENT))
        assert _client(s).invoke("sys", "usr").finish_reason == ""

    def test_null_finish_reason_is_empty_string(self):
        """A streamed-then-collapsed body can carry an explicit null."""
        s = _ScriptedSession(_resp_with_finish(None))
        assert _client(s).invoke("sys", "usr").finish_reason == ""

    def test_non_string_finish_reason_is_stringified_not_crashed(self):
        s = _ScriptedSession(_resp_with_finish(7))
        assert _client(s).invoke("sys", "usr").finish_reason == "7"

    def test_a_choice_that_is_not_a_mapping_still_advances_the_chain(self):
        """Regression guard: reading the choice's finish_reason must not turn a
        malformed body into an uncaught AttributeError instead of a failover."""
        bad = SimpleNamespace(status_code=200, json=lambda: {"choices": [["nope"]]})
        s = _ScriptedSession(bad, _resp())
        assert _client(s).invoke("sys", "usr").text == "ok"
        assert len(s.calls) == 2

    def test_the_pre_existing_response_shape_still_reports_nothing(self):
        """``_resp`` has no finish_reason; the field must default, not guess."""
        s = _ScriptedSession(_resp())
        assert _client(s).invoke("sys", "usr").finish_reason == ""


class TestTruncationAdvancesTheChain:
    """A truncated completion is HTTP 200, so it used to return as success and
    PRXREF_LLM_MODELS never advanced (issue #10). The reviewer's truncation
    handling only engages AFTER the chain has run, so the chain itself has to
    refuse a ``length`` stop as a final answer.
    """

    def test_a_truncated_model_advances_to_the_next_model(self, caplog):
        s = _ScriptedSession(
            _resp_with_finish("length", text='{"findings": [{"ti'),
            _resp_with_finish("stop", text="ok", model="m2-resolved"),
        )
        with caplog.at_level(logging.WARNING, logger="prxref.llm_backends"):
            r = _client(s).invoke("sys", "usr")
        assert r.text == "ok"
        assert r.model == "m2-resolved"
        assert r.finish_reason == "stop"
        assert len(s.calls) == 2
        assert s.calls[0]["json"]["model"] == "m1"
        assert s.calls[1]["json"]["model"] == "m2"
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("truncated" in m and "m1" in m for m in messages)

    def test_all_models_truncated_returns_the_last_truncated_result(self):
        """Last resort: exhaustion by truncation hands back the truncated
        answer with its reason intact — never an exception — so the reviewer
        downstream can still name the token budget as the cause."""
        s = _ScriptedSession(
            _resp_with_finish("length", text='{"a"'),
            _resp_with_finish("length", text='{"b"', model="m2-resolved"),
        )
        r = _client(s).invoke("sys", "usr")
        assert r.text == '{"b"'
        assert r.model == "m2-resolved"
        assert r.finish_reason == "length"
        assert len(s.calls) == 2

    def test_a_truncation_is_returned_even_when_a_later_model_hard_fails(self):
        s = _ScriptedSession(_resp_with_finish("length", text='{"a"'), _resp(status_code=503))
        r = _client(s).invoke("sys", "usr")
        assert r.text == '{"a"'
        assert r.finish_reason == "length"

    def test_truncation_then_a_hard_failure_still_reaches_the_healthy_model(self):
        s = _ScriptedSession(
            _resp_with_finish("length"),
            _resp(status_code=500),
            _resp(model="m3-resolved"),
        )
        r = _client(s, models=("a", "b", "c")).invoke("sys", "usr")
        assert r.text == "ok"
        assert r.model == "m3-resolved"

    @pytest.mark.parametrize("raw", ["length", "LENGTH", "max_tokens", "MAX_TOKENS", " Length "])
    def test_every_truncation_spelling_the_reviewer_knows_advances(self, raw):
        """Same casefolded vocabulary as reviewer._budget_stop_reason: a
        gateway that logs MAX_TOKENS must fail over exactly like ``length``."""
        s = _ScriptedSession(_resp_with_finish(raw), _resp(model="m2-resolved"))
        assert _client(s).invoke("sys", "usr").model == "m2-resolved"

    def test_a_clean_stop_still_returns_on_the_first_model(self):
        """Control: a healthy answer must not be pushed off its model."""
        s = _ScriptedSession(_resp_with_finish("stop", model="only-resolved"))
        r = _client(s, models=("only",)).invoke("sys", "usr")
        assert r.text == "ok"
        assert len(s.calls) == 1

    def test_a_non_truncation_stop_reason_still_returns_as_success(self):
        """Control: not every unusual stop is a budget stop; unknown reasons
        keep today's behaviour."""
        s = _ScriptedSession(_resp_with_finish("content_filter", model="only-resolved"))
        r = _client(s, models=("only",)).invoke("sys", "usr")
        assert r.finish_reason == "content_filter"
        assert len(s.calls) == 1


class TestMalformedBodiesAdvanceTheChain:
    """The class docstring promises "a malformed body — the next model is tried
    at once". Two shapes used to break that promise by raising out of invoke().
    """

    @pytest.mark.parametrize("message", ["oops", ["a"], 7, None])
    def test_a_non_mapping_message_advances_instead_of_raising(self, message):
        """``"oops".get(...)`` raises AttributeError, which is outside the
        caught tuple, so it escaped invoke() and killed the failover."""
        bad = SimpleNamespace(
            status_code=200, json=lambda: {"choices": [{"message": message}]}
        )
        s = _ScriptedSession(bad, _resp())
        assert _client(s).invoke("sys", "usr").text == "ok"
        assert len(s.calls) == 2

    def test_it_is_reported_as_a_malformed_response_not_a_new_failure_class(self):
        bad = SimpleNamespace(
            status_code=200, json=lambda: {"choices": [{"message": "oops"}]}
        )
        s = _ScriptedSession(bad)
        with pytest.raises(LLMError, match="malformed response"):
            _client(s, models=("only",)).invoke("sys", "usr")

    def test_a_mapping_message_with_no_content_is_still_a_success(self):
        """Control: an EMPTY message is a valid answer of nothing, and must not
        be swept into the malformed branch by the new check."""
        ok = SimpleNamespace(
            status_code=200,
            json=lambda: {"choices": [{"message": {"role": "assistant"}}]},
        )
        s = _ScriptedSession(ok)
        result = _client(s, models=("only",)).invoke("sys", "usr")
        assert result.text == ""
        assert len(s.calls) == 1


class TestFinishReasonLiteLLM:
    def _client_with(self, monkeypatch, choice):
        def fake_completion(**kwargs):
            return SimpleNamespace(choices=[choice], usage=None, model="m")

        monkeypatch.setitem(
            sys.modules, "litellm", types.SimpleNamespace(completion=fake_completion)
        )
        return LiteLLMClient(models=["p"])

    def test_length_is_carried_through(self, monkeypatch):
        choice = SimpleNamespace(
            message=SimpleNamespace(content="{"), finish_reason="length"
        )
        assert self._client_with(monkeypatch, choice).invoke("s", "u").finish_reason == "length"

    def test_stop_is_carried_through(self, monkeypatch):
        choice = SimpleNamespace(
            message=SimpleNamespace(content="ok"), finish_reason="stop"
        )
        assert self._client_with(monkeypatch, choice).invoke("s", "u").finish_reason == "stop"

    def test_a_response_object_without_the_attribute_reports_nothing(self, monkeypatch):
        choice = SimpleNamespace(message=SimpleNamespace(content="ok"))
        assert self._client_with(monkeypatch, choice).invoke("s", "u").finish_reason == ""

    def test_none_finish_reason_reports_nothing(self, monkeypatch):
        choice = SimpleNamespace(
            message=SimpleNamespace(content="ok"), finish_reason=None
        )
        assert self._client_with(monkeypatch, choice).invoke("s", "u").finish_reason == ""


class TestWallClockDeadline:
    """The configured timeout must bound TOTAL elapsed, not the gap between bytes.

    ``requests`` treats a scalar ``timeout`` as connect-and-read, and its read
    timeout measures the interval BETWEEN chunks. A peer that trickles resets
    that clock on every chunk and runs unbounded. Measured against a real
    provider before this was fixed: 496s elapsed under a configured 240s, with
    the socket still ESTABLISHED and nothing in any log.
    """

    def _dribbling(self, chunk_delay, chunks=40):
        def iter_content(chunk_size=None):
            for _ in range(chunks):
                time.sleep(chunk_delay)
                yield b'{"x":1}'
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"choices": [{"message": {"content": "ok"}}]},
            iter_content=iter_content,
            close=lambda: None,
        )

    def test_a_trickling_body_is_cut_off_at_the_deadline(self):
        s = _ScriptedSession(self._dribbling(chunk_delay=0.02))
        client = OpenAICompatClient(
            base_url="https://llm.test/v1", api_key="local",
            models=["only"], session=s, default_timeout=0.05,
        )
        t0 = time.monotonic()
        with pytest.raises(LLMError) as exc:
            client.invoke("sys", "usr")
        elapsed = time.monotonic() - t0
        assert "timeout" in str(exc.value).lower()
        # The control that makes this falsifiable: 40 chunks x 20ms is 800ms of
        # work, so finishing well under that proves it was CUT OFF rather than
        # merely allowed to run to completion.
        assert elapsed < 0.5, f"ran {elapsed:.2f}s; deadline did not cut it off"

    def test_a_body_that_arrives_in_time_still_succeeds(self):
        """Control: the deadline must not truncate a healthy response."""
        s = _ScriptedSession(self._dribbling(chunk_delay=0.0, chunks=2))
        client = OpenAICompatClient(
            base_url="https://llm.test/v1", api_key="local",
            models=["only"], session=s, default_timeout=5.0,
        )
        assert client.invoke("sys", "usr").text == "ok"

    def test_the_deadline_advances_the_chain_to_the_next_model(self):
        """A stuck model must not strand the review: failover still applies."""
        s = _ScriptedSession(self._dribbling(chunk_delay=0.02), _resp(text="second"))
        client = OpenAICompatClient(
            base_url="https://llm.test/v1", api_key="local",
            models=["stuck", "healthy"], session=s, default_timeout=0.05,
        )
        assert client.invoke("sys", "usr").text == "second"
