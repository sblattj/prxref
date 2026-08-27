"""LLM contract: protocol + fallback-chain factory.

Backends live in llm_backends.py (ferry / litellm / http). This module freezes
the interface the pipeline codes against. NO provider-specific keys are read
here; backend selection is PRXREF_LLM_BACKEND=ferry|litellm|http and the model
fallback chain is PRXREF_LLM_MODELS="model1,model2,..." (first that answers
within timeout wins; failures fail over fast).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ConfigError(ValueError):
    """Required configuration is missing, malformed, or out of range.

    A usage error, not a review failure: the CLI turns it into exit 2 while
    every review-time failure exits 0. It subclasses ``ValueError`` because a
    malformed value IS a bad value — callers that already catch ``ValueError``
    around config parsing keep working, and ``except ConfigError`` in
    :mod:`prxref.cli` now sees malformed values as well as missing ones.
    """


@dataclass
class InvokeResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    backend: str = ""
    elapsed_ms: int = 0


class LLMClient(Protocol):
    """One-shot invoke; the pipeline never holds conversations.

    ``timeout_s=None`` means "use the backend's configured default"
    (``PRXREF_LLM_TIMEOUT``); the protocol names no timeout of its own.
    """

    def invoke(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4096,
        json_mode: bool = False,
        timeout_s: float | None = None,
    ) -> InvokeResult:
        ...
