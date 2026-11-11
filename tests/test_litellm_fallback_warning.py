"""The litellm chain logs when its first model did not answer.

litellm walks ``fallbacks`` inside one call and swallows the primary's error, so
a primary that failed on every call (a Bedrock model with no accepted
Marketplace agreement) left no trace: the gate reviewed on the second entry for
a whole day with nothing in its logs.
"""

import logging
import sys
import types
from types import SimpleNamespace

CHAIN = ["bedrock/us.anthropic.claude-opus-5-5", "bedrock/us.anthropic.claude-opus-4-6-v1"]
MSG = "model chain fell back"


def _client(monkeypatch, reported, attempted, chain=CHAIN):
    from prxref.llm_backends import LiteLLMClient

    headers = {} if attempted is None else {"x-litellm-attempted-fallbacks": attempted}

    def fake_completion(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok"), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=8, completion_tokens=4),
            model=reported,
            _hidden_params={"additional_headers": headers},
        )

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(completion=fake_completion))
    return LiteLLMClient(models=chain)


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and MSG in r.getMessage()]


def test_primary_answering_logs_nothing(monkeypatch, caplog):
    c = _client(monkeypatch, "us.anthropic.claude-opus-5-5", 0)
    with caplog.at_level(logging.WARNING, logger="prxref.llm_backends"):
        c.invoke("s", "u")
    assert _warnings(caplog) == []


def test_fallback_warns_once_per_run_naming_both_models(monkeypatch, caplog):
    c = _client(monkeypatch, "us.anthropic.claude-opus-4-6-v1", 1)
    with caplog.at_level(logging.WARNING, logger="prxref.llm_backends"):
        c.invoke("s", "u")
        c.invoke("s", "u")
    got = _warnings(caplog)
    assert len(got) == 1
    assert CHAIN[0] in got[0] and CHAIN[1] in got[0]


def test_fallback_without_a_count_still_warns_on_the_reported_id(monkeypatch, caplog):
    c = _client(monkeypatch, "us.anthropic.claude-opus-4-6-v1", None)
    with caplog.at_level(logging.WARNING, logger="prxref.llm_backends"):
        c.invoke("s", "u")
    got = _warnings(caplog)
    assert len(got) == 1 and "us.anthropic.claude-opus-4-6-v1 (reported id)" in got[0]


def test_primary_reported_without_prefix_and_no_count_logs_nothing(monkeypatch, caplog):
    c = _client(monkeypatch, "us.anthropic.claude-opus-5-5", None)
    with caplog.at_level(logging.WARNING, logger="prxref.llm_backends"):
        c.invoke("s", "u")
    assert _warnings(caplog) == []


def test_a_single_model_chain_never_warns(monkeypatch, caplog):
    c = _client(monkeypatch, "something-else", None, chain=[CHAIN[1]])
    with caplog.at_level(logging.WARNING, logger="prxref.llm_backends"):
        c.invoke("s", "u")
    assert _warnings(caplog) == []
