"""A 4xx failure keeps the provider's own error message, one bounded line.

PR #207's gate check said only ``glm-5.3: HTTP 400`` for every chunk. The
provider's body said why: ``This model always engages in thinking and cannot
be disabled; please use low, high, or max`` (the configured reasoning effort
was ``medium``). Behind it sat a 429 saying the plan had expired. Neither
reached the check or the log, so the cause took a live probe to find. The
failure text now carries the message after the status, with the API key
removed, newlines collapsed and the length capped. A 5xx is unchanged.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from prxref.llm_backends import LLMError, OpenAICompatClient
from prxref.orchestrator import redact_for_post

KEY = "sk-live-0123456789abcdef0123456789abcdef"
THINKING = "This model always engages in thinking and cannot be disabled; please use low, high, or max"
EXPIRED = "Your GLM Coding Plan package has expired and is temporarily unavailable."


def _err(status: int, message: str | None = None, *, raw: str | None = None):
    text = raw if raw is not None else json.dumps({"error": {"code": "1210", "message": message}})
    body = text.encode()

    def _json():
        return json.loads(text)

    return SimpleNamespace(
        status_code=status, text=text, json=_json,
        iter_content=lambda chunk_size=None: iter([body]), close=lambda: None,
    )


class _Session:
    def __init__(self, *script):
        self.script = list(script)

    def post(self, url, json=None, headers=None, timeout=None, stream=None):
        return self.script.pop(0)


def _fail(*responses, models=("glm-5.3",)) -> str:
    client = OpenAICompatClient(
        base_url="https://llm.test/v1/", api_key=KEY, models=list(models),
        session=_Session(*responses), default_timeout=45.0,
    )
    with pytest.raises(LLMError) as exc:
        client.invoke("sys", "usr")
    return str(exc.value)


def test_the_issue_400_names_the_provider_reason():
    text = _fail(_err(400, THINKING))
    assert f"glm-5.3: HTTP 400: {THINKING}" in text


def test_a_429_names_the_expired_plan():
    assert f"glm-5.3: HTTP 429: {EXPIRED}" in _fail(_err(429, EXPIRED))


def test_a_5xx_is_unchanged():
    text = _fail(_err(500, "upstream exploded"))
    assert text.endswith("glm-5.3: HTTP 500")


def test_the_api_key_is_never_echoed():
    text = _fail(_err(401, f"Incorrect API key provided: {KEY}"))
    assert KEY not in text
    assert "glm-5.3: HTTP 401: Incorrect API key provided:" in text


def test_the_message_is_one_bounded_line():
    text = _fail(_err(400, "line one\nline two\t" + "x" * 500))
    reason = text.split("glm-5.3: HTTP 400: ", 1)[1]
    assert "\n" not in reason and "\t" not in reason
    assert reason.startswith("line one line two x")
    assert len(reason) <= 161 and reason.endswith("…")


def test_a_non_json_body_is_used_as_text():
    assert "glm-5.3: HTTP 400: Bad Request" in _fail(_err(400, raw="Bad Request"))


def test_an_empty_body_adds_nothing():
    assert _fail(_err(400, raw="")).endswith("glm-5.3: HTTP 400")


def test_the_posted_text_keeps_the_reason_readable():
    posted = redact_for_post(_fail(_err(400, THINKING)))
    assert "please use low, high, or max" in posted


def test_the_operator_log_names_the_reason(caplog):
    with caplog.at_level("WARNING", logger="prxref.llm_backends"):
        _fail(_err(400, THINKING))
    assert "HTTP 400 after" in caplog.text and "please use low, high, or max" in caplog.text
