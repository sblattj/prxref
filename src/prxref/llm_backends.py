"""LLM backends: an OpenAI-compatible plain-HTTP client and an optional litellm wrapper.

The primary backend speaks plain HTTP to any OpenAI-compatible
``/chat/completions`` endpoint. There is no default endpoint and no default
model chain: ``PRXREF_LLM_BASE_URL`` and ``PRXREF_LLM_MODELS`` are required,
and an unset one raises ``ConfigError`` rather than guessing a host.

Fallback is a caller-side loop over the model chain: a model that answers
with HTTP >= 500, HTTP 429, a connection error, or a timeout is advanced
past immediately — no same-model retry, fast failover is the product
promise. The optional ``litellm`` extra wraps the in-process SDK and
delegates the chain to its native ``fallbacks=`` mechanism.

Tenet: no provider credential is ever read and no env name is
provider-specific — provider keys live behind the configured endpoint,
never here.
"""
from __future__ import annotations

import math
import os
import time

import requests

from .llm import ConfigError, InvokeResult, LLMClient

DEFAULT_BASE_URL = ""
DEFAULT_API_KEY = ""
DEFAULT_MODELS = ""
DEFAULT_TIMEOUT = 45.0


class LLMError(Exception):
    """Every model in the fallback chain failed; the message carries per-model reasons."""


class OpenAICompatClient(LLMClient):
    """Plain-HTTP client for an OpenAI-compatible endpoint.

    Tries each model in ``models`` order (cheap first for speed). A model
    fails on HTTP >= 500, HTTP 429, any other HTTP error, a connection
    error, a timeout, or a malformed body — the next model is tried at once,
    and exhausting the chain raises :class:`LLMError` with per-model reasons.
    ``temperature`` is omitted from the payload entirely when unset (like
    ``reasoning_effort``), never sent as a numeric default: some endpoints
    reject it alongside reasoning parameters. The choice's ``finish_reason``
    is carried through verbatim so the reviewer can name truncation as the
    cause of an unparseable response.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        models: list[str],
        session: requests.Session | None = None,
        default_timeout: float = DEFAULT_TIMEOUT,
        reasoning_effort: str | None = None,
        temperature: float | None = None,
    ):
        if not models:
            raise ValueError("models must be a non-empty list")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.models = list(models)
        self.session = session if session is not None else requests.Session()
        self.default_timeout = default_timeout
        self.reasoning_effort = reasoning_effort or None
        self.temperature = temperature

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
        if self.temperature is not None:
            payload["temperature"] = self.temperature
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
                choice = body["choices"][0]
                text = choice["message"].get("content") or ""
                # Read after ``choice["message"]`` has already proved ``choice``
                # is a mapping, so a malformed body still lands in the
                # advance-to-the-next-model branch below rather than raising.
                finish_reason = str(choice.get("finish_reason") or "")
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
                finish_reason=finish_reason,
            )
        raise LLMError("all models failed: " + "; ".join(failures))


class LiteLLMClient(LLMClient):
    """In-process litellm backend; first model primary, the rest native fallbacks.

    Requires the optional extra (``pip install 'prxref[litellm]'``).
    ``num_retries=0`` keeps failover fast; the chain itself is delegated to
    litellm via ``fallbacks=``. Usage and the choice's ``finish_reason`` are
    mapped into InvokeResult; a response carrying neither yields zeros and
    ``""``. ``temperature`` is omitted entirely when unset, never defaulted.
    """

    def __init__(
        self,
        models: list[str],
        default_timeout: float = DEFAULT_TIMEOUT,
        temperature: float | None = None,
    ):
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
        self.temperature = temperature
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
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        t0 = time.perf_counter()
        response = self._completion(**kwargs)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        choice = response.choices[0]
        text = choice.message.content or ""
        usage = getattr(response, "usage", None)
        return InvokeResult(
            text=text,
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            model=getattr(response, "model", "") or self.models[0],
            backend="litellm",
            elapsed_ms=elapsed_ms,
            # Absent on a provider that does not report one; never guessed.
            finish_reason=str(getattr(choice, "finish_reason", "") or ""),
        )


def _float_setting(
    raw: str | None, env: str, *, minimum: float, exclusive: bool = False
) -> float | None:
    """Parse one numeric setting from cfg-or-env, or ``None`` when unset.

    ``None``, empty, or whitespace-only means "unset" and yields ``None`` — the
    same reading ``config.load_config`` gives those values — so the caller keeps
    its built-in default (timeout) or omits the field from the payload entirely
    (temperature). A malformed, non-finite, or out-of-range value raises
    :class:`~prxref.llm.ConfigError` naming the variable, so the CLI reports it
    as a configuration error (exit 2) instead of a mid-review failure.
    """
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    try:
        value = float(text)
    except ValueError as exc:
        raise ConfigError(f"{env}: {exc}") from exc
    floor_ok = value > minimum if exclusive else value >= minimum
    if not math.isfinite(value) or not floor_ok:
        bound = "greater than" if exclusive else "at least"
        raise ConfigError(
            f"{env}: must be a finite number {bound} {minimum}, got {text!r}"
        )
    return value


def create_llm_client(
    cfg: dict | None = None, session: requests.Session | None = None
) -> LLMClient:
    """Build the configured client from ``cfg`` overrides then PRXREF_LLM_* env.

    ``cfg`` keys (LLM_BACKEND, LLM_BASE_URL, LLM_API_KEY, LLM_MODELS,
    LLM_REASONING_EFFORT) win over env; env never includes provider
    credentials. PRXREF_LLM_BACKEND selects ``openai-compat`` (default)
    with ``ferry`` as an alias, or ``litellm``. PRXREF_LLM_BASE_URL and
    PRXREF_LLM_MODELS are required and have no defaults — an unset one
    raises :class:`~prxref.llm.ConfigError`. PRXREF_LLM_API_KEY is
    optional and may be empty for a local no-auth server.
    PRXREF_LLM_MODELS (comma list, cheap first) feeds litellm too.
    PRXREF_LLM_REASONING_EFFORT is passed through unvalidated to the
    openai-compat client for models that cannot disable reasoning
    (e.g. GLM-5.3-Flash's ``low``/``high``/``max``); empty omits it.
    PRXREF_LLM_TIMEOUT (seconds, default 45.0, must be > 0) becomes the
    client's ``default_timeout``; PRXREF_LLM_TEMPERATURE is parsed to a float
    (finite, >= 0 — no upper bound, since the maximum is provider-specific)
    and omitted from the request entirely when unset, which keeps 0.0 usable
    as a real temperature. A malformed or out-of-range value for either
    raises :class:`~prxref.llm.ConfigError` naming the variable, so the CLI
    exits 2 rather than degrading the review.
    ``PRXREF_LLM_MAX_TOKENS`` is deliberately NOT read here: it is a
    per-call budget threaded cfg -> orchestrator -> reviewer -> ``invoke``,
    so a client-level copy could never win and would be dead config.
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
    base_url = _get("LLM_BASE_URL", "PRXREF_LLM_BASE_URL", DEFAULT_BASE_URL) or ""
    if not base_url.strip():
        raise ConfigError(
            "no LLM endpoint configured. Set PRXREF_LLM_BASE_URL to an "
            "OpenAI-compatible /chat/completions endpoint "
            "(see README > LLM Configuration)."
        )
    if not models:
        raise ConfigError(
            "no LLM model chain configured. Set PRXREF_LLM_MODELS to a "
            "comma-separated list, cheapest first "
            "(see README > LLM Configuration)."
        )
    timeout = _float_setting(
        _get("LLM_TIMEOUT", "PRXREF_LLM_TIMEOUT"),
        "PRXREF_LLM_TIMEOUT",
        minimum=0.0,
        exclusive=True,
    )
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    temperature = _float_setting(
        _get("LLM_TEMPERATURE", "PRXREF_LLM_TEMPERATURE"),
        "PRXREF_LLM_TEMPERATURE",
        minimum=0.0,
    )
    if backend in ("openai-compat", "ferry", "http"):
        return OpenAICompatClient(
            base_url=base_url,
            api_key=_get("LLM_API_KEY", "PRXREF_LLM_API_KEY", DEFAULT_API_KEY) or DEFAULT_API_KEY,
            models=models,
            session=session,
            default_timeout=timeout,
            reasoning_effort=_get("LLM_REASONING_EFFORT", "PRXREF_LLM_REASONING_EFFORT"),
            temperature=temperature,
        )
    if backend == "litellm":
        return LiteLLMClient(models=models, default_timeout=timeout, temperature=temperature)
    raise LLMError(f"unknown PRXREF_LLM_BACKEND {backend!r}; expected openai-compat|ferry|http|litellm")
