"""Eval for issue 09: a model whose 4xx body says it is permanently
unavailable must be cached for the lifetime of the OpenAICompatClient and
skipped on every later invoke() -- not retried on every chunk/sweep.

Mirrors the fake-session style in tests/test_llm_backends.py (a fake
``session.post`` returning objects with ``status_code``/``json()``/
``iter_content``/``close``) rather than inventing a new mocking approach.
These tests are expected to FAIL against today's llm_backends.py (no
per-run memory of a failed model exists yet) and PASS once the fix lands.
"""
from __future__ import annotations

import json as json_module
import logging
from types import SimpleNamespace

import pytest

from prxref.llm_backends import LLMError, OpenAICompatClient


def _resp(status_code=200, model="resolved", text="ok", error_message=None):
    """A fake ``requests.Response`` -- success body or an OpenAI-style error body."""
    if error_message is not None:
        payload = {"error": {"message": error_message}}
    else:
        payload = {
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "model": model,
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    body = json_module.dumps(payload).encode()
    return SimpleNamespace(
        status_code=status_code,
        json=lambda: payload,
        text=json_module.dumps(payload),
        iter_content=lambda chunk_size=None: iter([body]),
        close=lambda: None,
    )


class _ModelRoutedSession:
    """Queues responses per model (by the posted ``json["model"]``); records every call."""

    def __init__(self, responses: dict[str, list]):
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[str] = []

    def post(self, url, json=None, headers=None, timeout=None, stream=None):
        model = json["model"]
        self.calls.append(model)
        queue = self.responses.get(model, [])
        if not queue:
            raise AssertionError(f"unexpected extra request for model {model!r}")
        return queue.pop(0)

    def count(self, model: str) -> int:
        return self.calls.count(model)


def _client(session, models):
    return OpenAICompatClient(
        base_url="https://llm.test/v1", api_key="k", models=list(models), session=session,
    )


_UNAVAILABLE_MSG = (
    'The requested model is not available for integrator "opencode". '
    'Available models: ["claude-sonnet-5"]'
)


class TestSkipsPermanentlyUnavailableModel:
    def test_a_second_invoke_never_retries_the_unavailable_model(self, caplog):
        session = _ModelRoutedSession({
            "dead": [_resp(400, error_message=_UNAVAILABLE_MSG),
                     _resp(400, error_message=_UNAVAILABLE_MSG)],
            "alive": [_resp(text="ok"), _resp(text="ok")],
        })
        client = _client(session, ["dead", "alive"])
        with caplog.at_level(logging.WARNING, logger="prxref.llm_backends"):
            r1 = client.invoke("sys", "usr")
            r2 = client.invoke("sys", "usr")
        assert session.count("dead") == 1
        assert r1.text == "ok"
        assert r2.text == "ok"
        skip_msgs = [
            rec.getMessage() for rec in caplog.records
            if "skipping for the rest of the run" in rec.getMessage()
        ]
        assert len(skip_msgs) == 1, skip_msgs
        assert "dead" in skip_msgs[0]

    def test_b_all_unavailable_second_invoke_raises_without_a_request(self):
        session = _ModelRoutedSession({
            "dead1": [_resp(400, error_message="model_not_found: dead1"),
                      _resp(400, error_message="model_not_found: dead1")],
            "dead2": [_resp(400, error_message="model_not_found: dead2"),
                      _resp(400, error_message="model_not_found: dead2")],
        })
        client = _client(session, ["dead1", "dead2"])
        with pytest.raises(LLMError):
            client.invoke("sys", "usr")
        calls_after_first = len(session.calls)
        with pytest.raises(LLMError) as exc:
            client.invoke("sys", "usr")
        assert len(session.calls) == calls_after_first
        assert "skipped (unavailable)" in str(exc.value)


class TestControlsUnaffected:
    """Transient failures and non-matching 4xx bodies must keep retrying every invoke."""

    def test_transient_500_is_not_cached(self):
        session = _ModelRoutedSession({
            "dead": [_resp(500), _resp(500)],
            "alive": [_resp(text="ok1"), _resp(text="ok2")],
        })
        client = _client(session, ["dead", "alive"])
        r1 = client.invoke("sys", "usr")
        r2 = client.invoke("sys", "usr")
        assert session.count("dead") == 2
        assert r1.text == "ok1"
        assert r2.text == "ok2"

    def test_400_without_an_unavailable_phrase_is_not_cached(self):
        session = _ModelRoutedSession({
            "dead": [_resp(400, error_message="invalid request: messages must not be empty"),
                     _resp(400, error_message="invalid request: messages must not be empty")],
            "alive": [_resp(text="ok1"), _resp(text="ok2")],
        })
        client = _client(session, ["dead", "alive"])
        client.invoke("sys", "usr")
        client.invoke("sys", "usr")
        assert session.count("dead") == 2
