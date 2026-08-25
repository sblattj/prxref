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


@dataclass
class InvokeResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    backend: str = ""
    elapsed_ms: int = 0


class LLMClient(Protocol):
    """One-shot invoke; the pipeline never holds conversations."""

    def invoke(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4096,
        json_mode: bool = False,
        timeout_s: float = 60.0,
    ) -> InvokeResult:
        ...
