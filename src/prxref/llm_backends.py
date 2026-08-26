"""LLM backends: OpenAI-compatible plain-HTTP client (llm-ferry) and optional litellm.

The primary backend speaks plain HTTP to any OpenAI-compatible endpoint —
by default the llm-ferry daemon (http://<host>.local:8090/v1, api key
"local", lane names such as "flash"/"orch" as model strings, standard
OpenAI usage fields). Fallback is a caller-side loop over the model chain:
a model that answers with HTTP >= 500, HTTP 429, a connection error, or a
timeout is advanced past immediately — no same-model retry, fast failover
is the product promise. The optional ``litellm`` extra wraps the in-process
SDK and delegates the chain to its native ``fallbacks=`` mechanism.

Tenet: no provider credential is ever read and no env name is
provider-specific — provider keys live on the ferry host, never here.
"""
from __future__ import annotations

import os
import time

import requests

from .llm import InvokeResult, LLMClient

DEFAULT_BASE_URL = "http://127.0.0.1:8090/v1"
DEFAULT_API_KEY = "local"
DEFAULT_MODELS = "flash,orch"
DEFAULT_TIMEOUT = 45.0


class LLMError(Exception):
    """Every model in the fallback chain failed; the message carries per-model reasons."""


class OpenAICompatClient(LLMClient):
    """Plain-HTTP client for an OpenAI-compatible endpoint (llm-ferry lanes).

    Tries each model in ``models`` order (cheap first for speed). A model
    fails on HTTP >= 500, HTTP 429, any other HTTP error, a connection
    error, a timeout, or a malformed body — the next model is tried at once,
    and exhausting the chain raises :class:`LLMError` with per-model reasons.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        models: list[str],
        session: requests.Session | None = None,
        default_timeout: float = DEFAULT_TIMEOUT,
        reasoning_effort: str | None = None,
    ):
        if not models:
            raise ValueError("models must be a non-empty list")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.models = list(models)
        self.session = session if session is not None else requests.Session()
        self.default_timeout = default_timeout
        self.reasoning_effort = reasoning_effort or None

    def invoke(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4096,
        json_mode: bool = False,
        timeout_s: float | None = None,
    ) -> InvokeResult:
        """POST /chat/completions per model until one answers; fast-fail the rest."""
        request_timeout = self.default_timeout if timeout_s is None else timeout_s
        payload: dict = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        headers = {"Authorization": f"Bearer {self.api_key}"}

        failures: list[str] = []
        for model in self.models:
            t0 = time.perf_counter()
            try:
                resp = self.session.post(
                    f"{self.base_url}/chat/completions",
                    json={**payload, "model": model},
                    headers=headers,
                    timeout=request_timeout,
                )
            except requests.Timeout as exc:
                failures.append(f"{model}: timeout ({exc.__class__.__name__})")
                continue
            except requests.RequestException as exc:
                failures.append(f"{model}: {exc.__class__.__name__}: {exc}")
                continue
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            if resp.status_code >= 400:
                failures.append(f"{model}: HTTP {resp.status_code}")
                continue
            try:
                body = resp.json()
                text = body["choices"][0]["message"].get("content") or ""
                usage = body.get("usage") or {}
                resp_model = body.get("model") or model
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                failures.append(f"{model}: malformed response ({exc.__class__.__name__})")
                continue
            return InvokeResult(
                text=text,
                input_tokens=usage.get("prompt_tokens") or 0,
                output_tokens=usage.get("completion_tokens") or 0,
                model=resp_model,
                backend="openai-compat",
                elapsed_ms=elapsed_ms,
            )
        raise LLMError("all models failed: " + "; ".join(failures))


class LiteLLMClient(LLMClient):
    """In-process litellm backend; first model primary, the rest native fallbacks.

    Requires the optional extra (``pip install 'prxref[litellm]'``).
    ``num_retries=0`` keeps failover fast; the chain itself is delegated to
    litellm via ``fallbacks=``. Only usage is mapped into InvokeResult.
    """

    def __init__(self, models: list[str], default_timeout: float = DEFAULT_TIMEOUT):
        if not models:
            raise ValueError("models must be a non-empty list")
        try:
            import litellm
        except ImportError as exc:
            raise LLMError(
                "litellm backend selected but litellm is not installed; "
                "install the extra with: pip install 'prxref[litellm]'"
            ) from exc
        self.models = list(models)
        self.default_timeout = default_timeout
        self._completion = litellm.completion

    def invoke(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4096,
        json_mode: bool = False,
        timeout_s: float | None = None,
    ) -> InvokeResult:
        """One litellm.completion call with the native fallback chain attached."""
        request_timeout = self.default_timeout if timeout_s is None else timeout_s
        kwargs: dict = {
            "model": self.models[0],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "num_retries": 0,
            "timeout": request_timeout,
            "fallbacks": self.models[1:],
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        t0 = time.perf_counter()
        response = self._completion(**kwargs)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        text = response.choices[0].message.content or ""
        usage = getattr(response, "usage", None)
        return InvokeResult(
            text=text,
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            model=getattr(response, "model", "") or self.models[0],
            backend="litellm",
            elapsed_ms=elapsed_ms,
        )


def create_llm_client(
    cfg: dict | None = None, session: requests.Session | None = None
) -> LLMClient:
    """Build the configured client from ``cfg`` overrides then PRXREF_LLM_* env.

    ``cfg`` keys (LLM_BACKEND, LLM_BASE_URL, LLM_API_KEY, LLM_MODELS,
    LLM_REASONING_EFFORT) win over env; env never includes provider
    credentials. PRXREF_LLM_BACKEND selects ``openai-compat`` (default)
    with ``ferry`` as an alias, or ``litellm``. PRXREF_LLM_BASE_URL /
    PRXREF_LLM_API_KEY / PRXREF_LLM_MODELS feed the openai-compat client;
    PRXREF_LLM_MODELS (comma list, cheap first) feeds litellm too.
    PRXREF_LLM_REASONING_EFFORT is passed through unvalidated to the
    openai-compat client for models that cannot disable reasoning
    (e.g. GLM-5.3-Flash's ``low``/``high``/``max``); empty omits it.
    """
    cfg = cfg or {}

    def _get(key: str, env: str, default: str | None = None) -> str | None:
        for k in (key, key.lower()):
            v = cfg.get(k)
            if isinstance(v, (list, tuple)):
                v = ",".join(str(x) for x in v)
            if v not in (None, ""):
                return str(v)
        return os.environ.get(env, default)

    backend = (_get("LLM_BACKEND", "PRXREF_LLM_BACKEND", "openai-compat") or "").strip().lower() or "openai-compat"
    raw_models = _get("LLM_MODELS", "PRXREF_LLM_MODELS", DEFAULT_MODELS) or ""
    models = [m.strip() for m in raw_models.split(",") if m.strip()]
    if not models:
        raise LLMError("PRXREF_LLM_MODELS resolved to an empty model chain")
    if backend in ("openai-compat", "ferry", "http"):
        return OpenAICompatClient(
            base_url=_get("LLM_BASE_URL", "PRXREF_LLM_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL,
            api_key=_get("LLM_API_KEY", "PRXREF_LLM_API_KEY", DEFAULT_API_KEY) or DEFAULT_API_KEY,
            models=models,
            session=session,
            reasoning_effort=_get("LLM_REASONING_EFFORT", "PRXREF_LLM_REASONING_EFFORT"),
        )
    if backend == "litellm":
        return LiteLLMClient(models=models)
    raise LLMError(f"unknown PRXREF_LLM_BACKEND {backend!r}; expected openai-compat|ferry|http|litellm")
