"""Tests for prxref.llm_backends: fast fallback loop, request shape, factory, litellm guard."""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest
import requests

from prxref.llm_backends import (
    LiteLLMClient,
    LLMError,
    OpenAICompatClient,
    create_llm_client,
)


def _resp(status_code=200, model="m1-resolved", prompt=11, completion=7, text="ok", **extra):
    payload = {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "model": model,
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }
    payload.update(extra)
    return SimpleNamespace(status_code=status_code, payload=payload, json=lambda: payload)


class _ScriptedSession:
    """Returns queued responses/exceptions in order; records every request."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _client(session, models=("m1", "m2")):
    return OpenAICompatClient(
        base_url="http://ferry.local:8090/v1/",
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

    def post(self, url, json=None, headers=None, timeout=None):
        self.captured.append(json)
        if self.fail_first and len(self.captured) == 1:
            return _resp(status_code=500)
        return _resp()


def _client_capturing_payload(models=("m1",), reasoning_effort=None, fail_first=False):
    """Build an OpenAICompatClient whose session records posted payloads; returns (client, captured)."""
    captured: list[dict] = []
    client = OpenAICompatClient(
        base_url="http://ferry.local:8090/v1/",
        api_key="local",
        models=list(models),
        session=_PayloadCapturingSession(captured, fail_first),
        default_timeout=45.0,
        reasoning_effort=reasoning_effort,
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
        assert call["url"] == "http://ferry.local:8090/v1/chat/completions"
        assert call["headers"] == {"Authorization": "Bearer local"}
        assert call["timeout"] == 12
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
        assert s.calls[0]["timeout"] == 45.0


class TestUsageMapping:
    def test_usage_and_model_mapping(self):
        s = _ScriptedSession(_resp(model="ferry/gemini-flash", prompt=120, completion=45, text="body"))
        r = _client(s).invoke("sys", "usr")
        assert r.text == "body"
        assert r.input_tokens == 120
        assert r.output_tokens == 45
        assert r.model == "ferry/gemini-flash"
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


class TestCreateLLMClient:
    def test_defaults(self, monkeypatch):
        for var in (
            "PRXREF_LLM_BACKEND",
            "PRXREF_LLM_BASE_URL",
            "PRXREF_LLM_API_KEY",
            "PRXREF_LLM_MODELS",
        ):
            monkeypatch.delenv(var, raising=False)
        c = create_llm_client()
        assert isinstance(c, OpenAICompatClient)
        assert c.base_url == "http://127.0.0.1:8090/v1"
        assert c.api_key == "local"
        assert c.models == ["flash", "orch"]

    def test_ferry_alias_maps_to_openai_compat(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "ferry")
        assert isinstance(create_llm_client(), OpenAICompatClient)

    def test_litellm_selection(self, monkeypatch):
        fake = types.SimpleNamespace(completion=lambda **kw: None)
        monkeypatch.setitem(sys.modules, "litellm", fake)
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "litellm")
        assert isinstance(create_llm_client(), LiteLLMClient)

    def test_unknown_backend_raises(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_BACKEND", "skynet")
        with pytest.raises(LLMError):
            create_llm_client()

    def test_env_overrides(self, monkeypatch):
        monkeypatch.delenv("PRXREF_LLM_BACKEND", raising=False)
        monkeypatch.setenv("PRXREF_LLM_BASE_URL", "http://mini.local:8090/v1")
        monkeypatch.setenv("PRXREF_LLM_API_KEY", "sekrit")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "a, b ,c")
        c = create_llm_client()
        assert c.base_url == "http://mini.local:8090/v1"
        assert c.api_key == "sekrit"
        assert c.models == ["a", "b", "c"]

    def test_empty_models_env_raises(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_MODELS", " , ")
        with pytest.raises(LLMError):
            create_llm_client()

    def test_reasoning_effort_defaults_to_none(self, monkeypatch):
        monkeypatch.delenv("PRXREF_LLM_REASONING_EFFORT", raising=False)
        c = create_llm_client()
        assert c.reasoning_effort is None

    def test_reasoning_effort_env_var_passed_through(self, monkeypatch):
        monkeypatch.setenv("PRXREF_LLM_REASONING_EFFORT", "low")
        c = create_llm_client()
        assert c.reasoning_effort == "low"


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
