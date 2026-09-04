"""LLM backends: an OpenAI-compatible plain-HTTP client and an optional litellm wrapper.

The primary backend speaks plain HTTP to any OpenAI-compatible
``/chat/completions`` endpoint. There is no default endpoint and no default
model chain: ``PRXREF_LLM_BASE_URL`` and ``PRXREF_LLM_MODELS`` are required,
and an unset one raises ``ConfigError`` rather than guessing a host.

Fallback is a caller-side loop over the model chain: a model that answers
with HTTP >= 500, HTTP 429, a connection error, a timeout, a malformed
body, or a truncated completion (``finish_reason`` ``length``) is advanced
past immediately — no same-model retry, fast failover is the product
promise. A truncated answer arrives as HTTP 200, so it has to be caught
here or the chain never advances; if every model truncates, the last
truncated answer is returned rather than raised and the reviewer names the
token budget as the cause. The optional ``litellm`` extra wraps the
in-process SDK and delegates the chain to its native ``fallbacks=``
mechanism (which advances on errors only — a truncated litellm answer
still returns as success).

Tenet: no provider credential is ever read and no env name is
provider-specific — provider keys live behind the configured endpoint,
never here.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time

import requests

from .llm import ConfigError, InvokeResult, LLMClient

DEFAULT_BASE_URL = ""
DEFAULT_API_KEY = ""
DEFAULT_MODELS = ""
DEFAULT_TIMEOUT = 45.0
# Sent, not just a fallback: temperature 0 is the reproducibility default —
# identical diff, same model, same verdict — and it only works if the field
# actually reaches the wire. Resolved by create_llm_client when the operator
# left PRXREF_LLM_TEMPERATURE unset or empty.
DEFAULT_TEMPERATURE = 0.0
logger = logging.getLogger(__name__)
# Connecting is not generating: a reachable endpoint answers the TCP/TLS
# handshake in well under this, so a separate, much smaller connect budget
# fails a dead host fast instead of spending the whole generation deadline on
# it. Clamped to the deadline itself when that is smaller.
_CONNECT_TIMEOUT = 10.0
_READ_CHUNK_BYTES = 8192
# The truncation vocabulary, mirrored from reviewer.py's
# _TRUNCATION_FINISH_REASONS: gateways disagree on casing and spelling, and a
# truncated answer is a 200, so it must be caught HERE for the chain to
# advance — the reviewer only sees what survives the chain.
_TRUNCATION_FINISH_REASONS = frozenset({"length", "max_tokens"})
# A model that answers 4xx with one of these phrases is gone for the rest of
# the run, not merely rate-limited or transiently unhappy — deprovisioned,
# renamed, or never enabled for this integrator. Shared by both backends so a
# deprovisioned model reads identically whichever client hits it.
_UNAVAILABLE_PHRASES = (
    "not available",
    "not supported",
    "does not exist",
    "model_not_found",
    "unknown model",
    "no such model",
    "deprecated",
)


class LLMError(Exception):
    """Every model in the fallback chain failed; the message carries per-model reasons."""


def _looks_permanently_unavailable(text: str) -> bool:
    """Case-insensitive match of ``text`` against the unavailable-phrase vocabulary."""
    lowered = text.lower()
    return any(phrase in lowered for phrase in _UNAVAILABLE_PHRASES)


def _openai_error_message(resp: requests.Response) -> str:
    """Best-effort extraction of a 4xx body's error text for phrase matching.

    Prefers the OpenAI-style ``error.message``, falls back to a string
    ``error`` field, and finally the raw response text when the body does
    not parse as JSON or carries neither shape.
    """
    try:
        body = resp.json()
    except ValueError:
        return getattr(resp, "text", "") or ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
        elif isinstance(error, str):
            return error
    return getattr(resp, "text", "") or ""


def _mark_unavailable(model: str, unavailable: set[str], lock: threading.Lock) -> bool:
    """Add ``model`` to ``unavailable`` under ``lock``; ``True`` only for the adding thread.

    Guards the once-per-model "skipping for the rest of the run" WARNING
    against a race: both backends run on one client instance shared across a
    ``ThreadPoolExecutor``, so two chunk workers can observe the same
    not-yet-marked model at the same moment.
    """
    with lock:
        if model in unavailable:
            return False
        unavailable.add(model)
        return True


class OpenAICompatClient(LLMClient):
    """Plain-HTTP client for an OpenAI-compatible endpoint.

    Tries each model in ``models`` order (cheap first for speed). A model
    fails on HTTP >= 500, HTTP 429, any other HTTP error, a connection
    Tries each model in ``models`` order (cheap first for speed). A model
    fails on HTTP >= 500, HTTP 429, any other HTTP error, a connection
    error, a timeout, a malformed body, or a truncated completion
    (``finish_reason`` ``length``/``max_tokens``) — the next model is tried
    at once, and exhausting the chain raises :class:`LLMError` with
    per-model reasons. The one exception is exhaustion by truncation: a
    truncated answer is a real completion, so the last truncated result is
    returned instead of raised and the reviewer downstream names the token
    budget as the cause.
    ``temperature`` and ``seed`` are omitted from the payload entirely when
    ``None`` (like ``reasoning_effort``); the factory resolves temperature's
    configured default of 0.0 — sent, so reviews are reproducible by default —
    and passes a seed only when one is configured. The choice's
    ``finish_reason`` is carried through verbatim so the reviewer can name
    truncation as the cause of an unparseable response.
    A model whose 4xx body names it as permanently gone (deprovisioned,
    renamed, never enabled) is cached in-memory for the client's lifetime:
    every later ``invoke()`` skips it outright — no request, no log — and
    the one time it is marked, a single WARNING records why.
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
        seed: int | None = None,
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
        self.seed = seed
        # Run-lifetime memory of models a 4xx body named as permanently gone
        # (deprovisioned, renamed, never enabled) so a chunk fan-out backed by
        # one shared client stops re-trying and re-logging a dead model on
        # every chunk plus the sweep. The lock guards concurrent workers
        # racing to be the one that logs the once-per-model WARNING.
        self._unavailable: set[str] = set()
        self._unavailable_lock = threading.Lock()

    def _post_within_deadline(
        self,
        url: str,
        *,
        json: dict,
        headers: dict[str, str],
        deadline_s: float,
    ) -> requests.Response:
        """POST and read the body under a WALL-CLOCK deadline.

        ``requests`` treats a scalar ``timeout`` as connect-and-read, and its
        read timeout bounds the gap BETWEEN bytes, never the duration of the
        call. An endpoint that dribbles -- or a proxy holding the connection
        open -- therefore resets that clock indefinitely, and the request runs
        unbounded while every log stays silent. Measured against a real
        provider: 496s elapsed under a configured 240s.

        Streaming the body puts the deadline back in reach: the socket read is
        still bounded by ``read_timeout`` so a silent peer fails fast, and the
        elapsed check between chunks bounds a peer that trickles.
        """
        deadline = time.monotonic() + deadline_s
        connect_timeout = min(_CONNECT_TIMEOUT, deadline_s)
        resp = self.session.post(
            url,
            json=json,
            headers=headers,
            timeout=(connect_timeout, deadline_s),
            stream=True,
        )
        try:
            chunks: list[bytes] = []
            for chunk in resp.iter_content(chunk_size=_READ_CHUNK_BYTES):
                chunks.append(chunk)
                if time.monotonic() > deadline:
                    raise requests.Timeout(
                        f"exceeded the {deadline_s:.0f}s deadline while reading the "
                        f"response body ({sum(len(c) for c in chunks)} bytes read)"
                    )
            # _content/_content_consumed is how requests itself marks a streamed
            # body as fully read; setting them lets .json()/.text work normally
            # downstream instead of raising on an already-consumed stream.
            resp._content = b"".join(chunks)
            resp._content_consumed = True
            return resp
        finally:
            resp.close()

    def invoke(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4096,
        json_mode: bool = False,
        timeout_s: float | None = None,
    ) -> InvokeResult:
        """POST /chat/completions per model until one answers untruncated; fast-fail the rest."""
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
        if self.seed is not None:
            payload["seed"] = self.seed
        headers = {"Authorization": f"Bearer {self.api_key}"}

        failures: list[str] = []
        last_truncated: InvokeResult | None = None
        for attempt, model in enumerate(self.models, start=1):
            if model in self._unavailable:
                failures.append(f"{model}: skipped (unavailable)")
                continue
            t0 = time.perf_counter()
            logger.info(
                "llm attempt %d/%d: model=%s deadline=%.0fs",
                attempt, len(self.models), model, request_timeout,
            )
            try:
                resp = self._post_within_deadline(
                    f"{self.base_url}/chat/completions",
                    json={**payload, "model": model},
                    headers=headers,
                    deadline_s=request_timeout,
                )
            except requests.Timeout as exc:
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                logger.warning(
                    "llm attempt %d/%d failed: model=%s timeout after %dms (%s)",
                    attempt, len(self.models), model, elapsed_ms, exc.__class__.__name__,
                )
                failures.append(f"{model}: timeout ({exc.__class__.__name__})")
                continue
            except requests.RequestException as exc:
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                logger.warning(
                    "llm attempt %d/%d failed: model=%s %s after %dms",
                    attempt, len(self.models), model, exc.__class__.__name__, elapsed_ms,
                )
                failures.append(f"{model}: {exc.__class__.__name__}: {exc}")
                continue
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            if resp.status_code >= 400:
                logger.warning(
                    "llm attempt %d/%d failed: model=%s HTTP %d after %dms",
                    attempt, len(self.models), model, resp.status_code, elapsed_ms,
                )
                failures.append(f"{model}: HTTP {resp.status_code}")
                if 400 <= resp.status_code < 500:
                    message = _openai_error_message(resp)
                    if _looks_permanently_unavailable(message):
                        if _mark_unavailable(model, self._unavailable, self._unavailable_lock):
                            logger.warning(
                                "model=%s marked unavailable (%s), skipping for the rest of "
                                "the run",
                                model, message,
                            )
                continue
            try:
                body = resp.json()
                choice = body["choices"][0]
                message = choice["message"]
                if not isinstance(message, dict):
                    # A non-mapping message is a malformed body like any other,
                    # and the docstring promises the chain advances on one. Left
                    # to itself, ``"oops".get`` raises AttributeError — outside
                    # the tuple below — and escapes invoke(), losing the failover.
                    raise TypeError(
                        f"choices[0].message is {type(message).__name__}, expected an object"
                    )
                text = message.get("content") or ""
                # Read after ``choice["message"]`` has already proved ``choice``
                # is a mapping, so a malformed body still lands in the
                # advance-to-the-next-model branch below rather than raising.
                finish_reason = str(choice.get("finish_reason") or "")
                usage = body.get("usage") or {}
                resp_model = body.get("model") or model
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                logger.warning(
                    "llm attempt %d/%d failed: model=%s malformed response (%s) after %dms",
                    attempt, len(self.models), model, exc.__class__.__name__, elapsed_ms,
                )
                failures.append(f"{model}: malformed response ({exc.__class__.__name__})")
                continue
            if finish_reason.strip().lower() in _TRUNCATION_FINISH_REASONS:
                # A truncated completion is HTTP 200, so without this branch
                # it returned as success and PRXREF_LLM_MODELS never advanced.
                logger.warning(
                    "llm attempt %d/%d truncated: model=%s finish_reason=%s after %dms out=%s",
                    attempt, len(self.models), resp_model, finish_reason, elapsed_ms,
                    usage.get("completion_tokens") or 0,
                )
                failures.append(f"{model}: truncated (finish_reason={finish_reason})")
                last_truncated = InvokeResult(
                    text=text,
                    input_tokens=usage.get("prompt_tokens") or 0,
                    output_tokens=usage.get("completion_tokens") or 0,
                    model=resp_model,
                    backend="openai-compat",
                    elapsed_ms=elapsed_ms,
                    finish_reason=finish_reason,
                )
                continue
            logger.info(
                "llm attempt %d/%d ok: model=%s %dms in=%s out=%s finish=%s",
                attempt, len(self.models), resp_model, elapsed_ms,
                usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0,
                finish_reason or "-",
            )
            return InvokeResult(
                text=text,
                input_tokens=usage.get("prompt_tokens") or 0,
                output_tokens=usage.get("completion_tokens") or 0,
                model=resp_model,
                backend="openai-compat",
                elapsed_ms=elapsed_ms,
                finish_reason=finish_reason,
            )
        # Exhausting the chain on truncation alone is a last resort, not a
        # failure: the best answer anyone managed is still handed back, with
        # finish_reason intact so the reviewer blames the token budget.
        if last_truncated is not None:
            return last_truncated
        raise LLMError("all models failed: " + "; ".join(failures))


class LiteLLMClient(LLMClient):
    """In-process litellm backend; first model primary, the rest native fallbacks.

    Requires the optional extra (``pip install 'prxref[litellm]'``).
    ``num_retries=0`` keeps failover fast; the chain itself is delegated to
    litellm via ``fallbacks=``. Usage and the choice's ``finish_reason`` are
    mapped into InvokeResult; a response carrying neither yields zeros and
    ``""``. ``temperature`` and ``seed`` are omitted entirely when ``None``,
    never defaulted; the factory resolves temperature's configured default
    of 0.0 before this client is built.
    A model litellm reports as permanently gone (a 4xx-shaped exception
    naming it deprovisioned, renamed, or never enabled) is cached in-memory
    for the client's lifetime, mirroring :class:`OpenAICompatClient`: it is
    filtered out of both ``model`` and ``fallbacks`` on every later
    ``invoke()``, and if every configured model is unavailable, ``invoke()``
    raises :class:`LLMError` without calling litellm at all.
    """

    def __init__(
        self,
        models: list[str],
        default_timeout: float = DEFAULT_TIMEOUT,
        temperature: float | None = None,
        seed: int | None = None,
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
        self.seed = seed
        self._completion = litellm.completion
        # Same run-lifetime memory as OpenAICompatClient (see its __init__),
        # keyed on litellm's own exception shape instead of a status code.
        self._unavailable: set[str] = set()
        self._unavailable_lock = threading.Lock()

    def invoke(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4096,
        json_mode: bool = False,
        timeout_s: float | None = None,
    ) -> InvokeResult:
        """One litellm.completion call with the native fallback chain attached.

        Models already marked unavailable are filtered out of both ``model``
        and ``fallbacks`` before the call; if none are left, ``invoke()``
        raises :class:`LLMError` without calling litellm.
        """
        request_timeout = self.default_timeout if timeout_s is None else timeout_s
        available = [m for m in self.models if m not in self._unavailable]
        if not available:
            raise LLMError(
                "all models failed: "
                + "; ".join(f"{m}: skipped (unavailable)" for m in self.models)
            )
        kwargs: dict = {
            "model": available[0],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "num_retries": 0,
            "timeout": request_timeout,
            "fallbacks": available[1:],
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.seed is not None:
            kwargs["seed"] = self.seed
        t0 = time.perf_counter()
        try:
            response = self._completion(**kwargs)
        except Exception as exc:
            self._maybe_mark_unavailable(exc, kwargs["model"], available)
            raise
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        choice = response.choices[0]
        text = choice.message.content or ""
        usage = getattr(response, "usage", None)
        return InvokeResult(
            text=text,
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            model=getattr(response, "model", "") or available[0],
            backend="litellm",
            elapsed_ms=elapsed_ms,
            # Absent on a provider that does not report one; never guessed.
            finish_reason=str(getattr(choice, "finish_reason", "") or ""),
        )

    def _maybe_mark_unavailable(
        self, exc: Exception, requested_model: str, available: list[str]
    ) -> None:
        """Cache ``requested_model`` (or the exception's own ``.model``) on a permanent 4xx.

        litellm's own ``BadRequestError``/``NotFoundError`` usually carry a
        ``.model`` naming which candidate in the internal fallback chain
        actually failed; when that is missing or not one of the models this
        call attempted, the model requested as primary is the best guess.
        """
        if not _litellm_error_signals_unavailable(exc):
            return
        failed_model = getattr(exc, "model", None)
        if not isinstance(failed_model, str) or failed_model not in available:
            failed_model = requested_model
        if _mark_unavailable(failed_model, self._unavailable, self._unavailable_lock):
            logger.warning(
                "model=%s marked unavailable, skipping for the rest of the run",
                failed_model,
            )


def _litellm_error_signals_unavailable(exc: Exception) -> bool:
    """True when a raised litellm exception names a model as permanently gone.

    Requires both a phrase match (the shared unavailable vocabulary, checked
    against ``str(exc)`` and a ``.message`` attribute when litellm sets one)
    AND a 4xx signal — litellm does not always set ``.status_code``, so the
    exception class naming ``BadRequest``/``NotFound`` is the fallback
    signal. A transient error (timeout, connection error, 5xx) must never be
    cached, so both checks are required, not either.
    """
    text = " ".join(filter(None, [str(exc), str(getattr(exc, "message", "") or "")]))
    if not _looks_permanently_unavailable(text):
        return False
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and 400 <= status_code < 500:
        return True
    class_name = exc.__class__.__name__
    return "BadRequest" in class_name or "NotFound" in class_name


def _float_setting(
    raw: str | None, env: str, *, minimum: float, exclusive: bool = False
) -> float | None:
    """Parse one numeric setting from cfg-or-env, or ``None`` when unset.

    ``None``, empty, or whitespace-only means "unset" and yields ``None`` — the
    same reading ``config.load_config`` gives those values — so the caller
    keeps its built-in default (the timeout) or resolves the reproducibility
    default (temperature → ``DEFAULT_TEMPERATURE``). A malformed,
    non-finite, or out-of-range value raises
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


def _int_setting(raw: str | None, env: str, *, minimum: int) -> int | None:
    """Parse one integer setting from cfg-or-env, or ``None`` when unset.

    Same unset/malformed/out-of-range contract as :func:`_float_setting`, but
    for a whole number: the OpenAI ``seed`` field is an integer, and a
    ``42.0`` on the wire is a provider-side type error waiting to happen.
    """
    if raw is None or not str(raw).strip():
        return None
    text = str(raw).strip()
    try:
        value = int(text)
    except ValueError as exc:
        raise ConfigError(f"{env}: {exc}") from exc
    if value < minimum:
        raise ConfigError(
            f"{env}: must be an integer at least {minimum}, got {text!r}"
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
    client's ``default_timeout``. PRXREF_LLM_TEMPERATURE is parsed to a
    float (finite, >= 0 — no upper bound, since the maximum is
    provider-specific); an unset or empty value resolves to
    ``DEFAULT_TEMPERATURE`` (0.0), which IS sent — temperature 0 keeps
    reviews reproducible by default, and an operator-set value wins.
    PRXREF_LLM_SEED (integer >= 0, where 0 is a valid seed) is passed to
    both backends as a top-level ``seed``; empty or unset omits it from
    the request entirely. A malformed or out-of-range value for any of
    these raises :class:`~prxref.llm.ConfigError` naming the variable, so
    the CLI exits 2 rather than degrading the review.
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
    if temperature is None:
        temperature = DEFAULT_TEMPERATURE
    seed = _int_setting(
        _get("LLM_SEED", "PRXREF_LLM_SEED"), "PRXREF_LLM_SEED", minimum=0
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
            seed=seed,
        )
    if backend == "litellm":
        return LiteLLMClient(
            models=models, default_timeout=timeout, temperature=temperature, seed=seed
        )
    raise LLMError(f"unknown PRXREF_LLM_BACKEND {backend!r}; expected openai-compat|ferry|http|litellm")
