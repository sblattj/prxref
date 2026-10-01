"""LLM backends: OpenAI-compatible HTTP, optional litellm, and subscription CLIs (``llm_cli_backends``).

The primary backend speaks plain HTTP to any OpenAI-compatible
``/chat/completions`` endpoint. There is no default model chain on any
backend: ``PRXREF_LLM_MODELS`` is required, and an unset one raises
``ConfigError``. There is no default endpoint either:
``PRXREF_LLM_BASE_URL`` is required by the openai-compat backend (and its
``ferry``/``http`` aliases) and raises ``ConfigError`` when unset rather than
guessing a host. The other backends do not use it: litellm resolves each
model's own provider endpoint, and the CLI backends talk to their CLI. A set
value is ignored there with one INFO line, never forwarded, so a deployment
that set a placeholder URL to get past the old unconditional check keeps its
routing on upgrade.

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

The ``claude-cli`` and ``kiro-cli`` backends (``llm_cli_backends``) run the
user's own installed, logged-in CLI as a subprocess, one process per call,
with the same caller-side model chain. They take the model chain, the
timeout, a process-count cap (``PRXREF_LLM_CLI_CONCURRENCY``) and an
optional binary path (``PRXREF_LLM_CLI_PATH``); the base URL, the API key,
``max_tokens``, temperature and seed are not applied. The factory imports
that module lazily, so the HTTP backends never load it.

Cost: a backend reports the dollar figure its provider returned and never
estimates one. The openai-compat client reads the body's ``usage.cost``
first, then a LiteLLM-based gateway's ``x-litellm-response-cost`` response
header; the litellm client reads ``_hidden_params["response_cost"]``. No
figure leaves ``InvokeResult.cost_usd`` as ``None``, never ``0.0``. The
price-table estimate is made once per run, in :mod:`prxref.costs`.

Tenet: prxref never reads, stores, or forwards a provider credential, and
its own settings are provider-neutral ``PRXREF_*`` names. A provider key
lives behind the configured endpoint (openai-compat), in the provider SDK's
own environment (litellm), or inside the user's own logged-in CLI
(claude-cli, kiro-cli). The CLI backends remove a fixed list of
credential-routing variable NAMES from the child process environment so the
CLI falls back to its subscription login; the values are never read.
"""
from __future__ import annotations

import logging
import math
import os
import secrets
import threading
import time

import requests

from . import costs
from .llm import ConfigError, InvokeResult, LLMClient

DEFAULT_BASE_URL = ""
DEFAULT_API_KEY = ""
DEFAULT_MODELS = ""
DEFAULT_TIMEOUT = 45.0
# Deadline scaling (issue #72): with the timeout left at its default the
# openai-compat client sizes each request's deadline from the prompt instead
# of holding one fixed value that a large chunk cannot fit inside. The
# calibration point: a 22k-token prompt answered in ~55s, while the 45s
# default cut it off — prefill and decode both grow with the input.
SCALED_DEADLINE_BASE_S = 20.0
SCALED_DEADLINE_CAP_S = 900.0
DEFAULT_TIMEOUT_PER_1K = 1.6
# Sent, not just a fallback: temperature 0 is the reproducibility default —
# identical diff, same model, same verdict — and it only works if the field
# actually reaches the wire. Resolved by create_llm_client when the operator
# left PRXREF_LLM_TEMPERATURE unset or empty.
DEFAULT_TEMPERATURE = 0.0
DEFAULT_CLI_CONCURRENCY = 2
OPENAI_COMPAT_BACKENDS = ("openai-compat", "ferry", "http")
CLI_BACKENDS = ("claude-cli", "kiro-cli")
BACKENDS = (*OPENAI_COMPAT_BACKENDS, "litellm", *CLI_BACKENDS)
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


def scaled_deadline(input_tokens: int, per_1k: float = DEFAULT_TIMEOUT_PER_1K) -> float:
    """The prompt-scaled deadline for one request, in seconds (issue #72).

    ``min(SCALED_DEADLINE_CAP_S, SCALED_DEADLINE_BASE_S + per_1k * tokens /
    1000)``: a fixed base plus a per-1k-input-token term, because prefill and
    decode both grow with the prompt, and the calibration point (22k tokens
    answered in ~55s at 1.6s/1k) sits on that line. The cap keeps a
    pathological prompt from asking for an hour; 900s is longer than any
    useful review answer.
    """
    return min(
        SCALED_DEADLINE_CAP_S,
        SCALED_DEADLINE_BASE_S + per_1k * input_tokens / 1000.0,
    )


def _estimate_prompt_tokens(system: str, user: str) -> int:
    """The pre-request input-token estimate the deadline scales on.

    Usage is only REPORTED by the endpoint, after the request has already run
    under whatever deadline was picked, so the only token count visible
    before the wire is the prompt itself. Roughly four characters per token
    is the standard English-and-code approximation, and an estimate is all
    the deadline needs: the applied deadline never drops below the
    configured default, so over-estimating costs a little patience and
    under-estimating costs nothing until the prompt is large.
    """
    return (len(system) + len(user)) // 4


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


def _header(headers: object, name: str) -> object:
    """Case-insensitive lookup of one response header; ``None`` when absent.

    A real response carries a ``requests.structures.CaseInsensitiveDict``,
    whose ``get`` already ignores case. A plain mapping (a test double, or
    anything else a session hands back) gets an exact ``get`` first and then
    a casefolded scan of its items, so ``X-LiteLLM-Response-Cost`` and
    ``x-litellm-response-cost`` read the same everywhere.
    """
    if not headers:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is not None:
            return value
    items = getattr(headers, "items", None)
    if not callable(items):
        return None
    wanted = name.casefold()
    for key, value in items():
        if isinstance(key, str) and key.casefold() == wanted:
            return value
    return None


def _reported_cost(usage: object, resp: object) -> tuple[float | None, str]:
    """The dollar figure the provider reported for one completion, and its source.

    The body's ``usage.cost`` (OpenRouter sends it unasked) wins; it must be
    a JSON number, so a string there is no figure. Otherwise the
    ``x-litellm-response-cost`` header that a LiteLLM-based gateway sets, a
    string by nature. Anything :func:`prxref.costs.valid_usd` rejects
    (negative, ``NaN``, ``""``, ``"None"``) is no figure, and no figure is
    ``(None, "")``: never ``0.0``.
    """
    if isinstance(usage, dict):
        body_cost = usage.get("cost")
        if not isinstance(body_cost, str):
            cost = costs.valid_usd(body_cost)
            if cost is not None:
                return cost, "usage.cost"
    cost = costs.valid_usd(_header(getattr(resp, "headers", None), "x-litellm-response-cost"))
    if cost is not None:
        return cost, "x-litellm-response-cost"
    return None, ""


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
    and the seed too: configured, else the once-per-process
    :func:`_auto_run_seed` shared by every client in the run. The choice's
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
        timeout_per_1k: float | None = None,
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
        # None (the default, and what the factory passes whenever an explicit
        # PRXREF_LLM_TIMEOUT or --timeout was given) disables deadline
        # scaling: an operator who set a deadline has replaced the model of
        # latency, and silently re-scaling it would override them.
        self.timeout_per_1k = timeout_per_1k
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
        if timeout_s is not None:
            request_timeout = timeout_s
        elif self.timeout_per_1k is not None:
            # Issue #72: the deadline scales with the prompt, never below the
            # configured default — scaling is what buys a large chunk room,
            # so a small one must not pay for it by losing deadline it had.
            request_timeout = max(
                self.default_timeout,
                scaled_deadline(
                    _estimate_prompt_tokens(system, user), self.timeout_per_1k,
                ),
            )
        else:
            request_timeout = self.default_timeout
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
        received: list[tuple[float | None, str]] = []
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
            # Every completion that came back was billed, a truncated one the
            # chain moves past included, so the call's figure sums them all.
            attempt_cost, attempt_source = _reported_cost(usage, resp)
            received.append((attempt_cost, attempt_source))
            cost_usd, cost_source = costs.combine_reported(received)
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
                    cost_usd=cost_usd,
                    cost_source=cost_source,
                )
                continue
            logger.info(
                "llm attempt %d/%d ok: model=%s %dms in=%s out=%s finish=%s cost=%s",
                attempt, len(self.models), resp_model, elapsed_ms,
                usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0,
                finish_reason or "-", "-" if attempt_cost is None else attempt_cost,
            )
            return InvokeResult(
                text=text,
                input_tokens=usage.get("prompt_tokens") or 0,
                output_tokens=usage.get("completion_tokens") or 0,
                model=resp_model,
                backend="openai-compat",
                elapsed_ms=elapsed_ms,
                finish_reason=finish_reason,
                cost_usd=cost_usd,
                cost_source=cost_source,
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
    ``""``.     ``temperature`` and ``seed`` are omitted entirely when ``None``,
    never defaulted here; the factory resolves both before this client is
    built — temperature's configured default of 0.0 and, when no seed is
    configured, the once-per-process :func:`_auto_run_seed` (a seed of
    ``off`` builds the client with ``seed=None``, so none is sent).
    ``reasoning_effort`` is forwarded as ``reasoning_effort=`` when set and
    omitted when empty or ``None``, like :class:`OpenAICompatClient`;
    litellm maps it to each provider's own effort parameter.
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
        reasoning_effort: str | None = None,
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
        self.reasoning_effort = reasoning_effort or None
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
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
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
        # litellm prices the call from its own bundled map and leaves the
        # figure here; completion_cost() is never called, because it raises on
        # a model the map does not know. A string is not a figure.
        hidden = getattr(response, "_hidden_params", None)
        raw_cost = hidden.get("response_cost") if isinstance(hidden, dict) else getattr(hidden, "response_cost", None)
        cost_usd = None if isinstance(raw_cost, str) else costs.valid_usd(raw_cost)
        return InvokeResult(
            text=text,
            input_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            model=getattr(response, "model", "") or available[0],
            backend="litellm",
            elapsed_ms=elapsed_ms,
            # Absent on a provider that does not report one; never guessed.
            finish_reason=str(getattr(choice, "finish_reason", "") or ""),
            cost_usd=cost_usd,
            cost_source="litellm" if cost_usd is not None else "",
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


# Temperature 0 does not pin hosted inference by itself: unseeded, the
# provider's sampler is free to vary token choice run to run, and a posting
# run diverged from its dry run over the same PR (issue #56). The factory
# has no PR identity to derive the seed from, so the fallback is ONE random
# seed per process, shared by every client the run builds — every invoke in
# the run pins the same sampling state, and ``orchestrator._sampling``
# records it via the client's ``seed`` attribute. 31 bits keeps the value a
# non-negative int inside every OpenAI-compatible provider's accepted range.
_run_seed: int | None = None
_run_seed_lock = threading.Lock()

# The PRXREF_LLM_SEED value that sends no seed at all (issue #26: Bedrock
# rejects the parameter). Lowercase only, like config's other word values;
# restated in prxref.config, which is a leaf module.
SEED_OFF = "off"


def _auto_run_seed() -> int:
    """The once-per-process fallback seed, shared by every client built without PRXREF_LLM_SEED."""
    global _run_seed
    with _run_seed_lock:
        if _run_seed is None:
            _run_seed = secrets.randbits(31)
        return _run_seed


def create_llm_client(
    cfg: dict | None = None, session: requests.Session | None = None
) -> LLMClient:
    """Build the configured client from ``cfg`` overrides then PRXREF_LLM_* env.

    ``cfg`` keys (LLM_BACKEND, LLM_BASE_URL, LLM_API_KEY, LLM_MODELS,
    LLM_REASONING_EFFORT, LLM_TIMEOUT, LLM_TIMEOUT_PER_1K, LLM_TEMPERATURE,
    LLM_SEED, LLM_CLI_PATH, LLM_CLI_CONCURRENCY, in either case) win over
    env; env
    never includes provider credentials. PRXREF_LLM_BACKEND is read
    case-insensitively and selects ``openai-compat`` (the default, with
    ``ferry`` and ``http`` as aliases), ``litellm``, ``claude-cli`` or
    ``kiro-cli``. Any other value raises :class:`~prxref.llm.ConfigError`
    naming PRXREF_LLM_BACKEND, before any other setting is looked at, so a
    typo is reported as itself (exit 2) rather than as a missing endpoint or
    a failed review.
    PRXREF_LLM_MODELS (comma list, cheap first) is required by every
    backend and has no default. PRXREF_LLM_BASE_URL has no default and is
    required by the openai-compat family only; it is checked before the
    models, so a run with both unset still names the endpoint first. The
    other backends do not use it: when it is set anyway it is ignored with
    one INFO line and never forwarded (litellm resolves each model's own
    provider endpoint; a LiteLLM proxy is OpenAI-compatible and belongs on
    ``openai-compat``). PRXREF_LLM_API_KEY is openai-compat only, optional,
    and may be empty for a local no-auth server.
    PRXREF_LLM_REASONING_EFFORT is passed through unvalidated to the
    openai-compat client for models that cannot disable reasoning
    (e.g. GLM-5.3-Flash's ``low``/``high``/``max``), to the litellm client
    as ``reasoning_effort=`` (litellm maps it per provider), and to
    claude-cli as its effort setting; empty omits it, and kiro-cli ignores
    it.
    PRXREF_LLM_TIMEOUT (seconds, default 45.0, must be > 0) becomes the
    client's ``default_timeout``. While it is left at that default the
    openai-compat client also scales each request's deadline from the
    prompt (issue #72): ``max(45, min(900, 20 + per_1k * estimated_input_
    tokens / 1000))``, with the estimate taken from the prompt text because
    usage is only reported after the request. PRXREF_LLM_TIMEOUT_PER_1K
    (float, must be > 0, default 1.6) tunes the per-1k coefficient for slow
    endpoints; it applies to the openai-compat family only. An explicit
    PRXREF_LLM_TIMEOUT or ``--timeout`` disables scaling entirely: the value
    is then used as-is for every request. A cfg that was resolved through
    :func:`prxref.config.load_config` always carries an llm_timeout value,
    so the CLI — the one caller that knows which layer supplied it — passes
    the boolean key ``LLM_TIMEOUT_IS_DEFAULT`` (or lowercase) alongside the
    resolved settings; without it the factory infers "default" as "no
    PRXREF_LLM_TIMEOUT in the environment and the resolved value equals
    the default". PRXREF_LLM_TEMPERATURE is
    parsed to a
    float (finite, >= 0 — no upper bound, since the maximum is
    provider-specific); an unset or empty value resolves to
    ``DEFAULT_TEMPERATURE`` (0.0), which IS sent — temperature 0 keeps
    reviews reproducible by default, and an operator-set value wins.
    PRXREF_LLM_SEED (integer >= 0, where 0 is a valid seed) is passed to
    the openai-compat and litellm backends as a top-level ``seed`` and
    always wins when set. Unset
    or empty does NOT omit the field: temperature 0 alone cannot pin hosted
    inference, so the factory derives ONE random seed per process
    (:func:`_auto_run_seed`) and stamps it on every client it builds —
    all LLM calls within a run share one seed, and the client's ``seed``
    attribute carries it into the run record's ``sampling``. The value
    ``off`` (:data:`SEED_OFF`, lowercase only) sends no seed at all: no
    configured seed and no fallback, for providers that reject the
    parameter, and the client's ``seed`` is ``None``. A malformed
    or out-of-range value for any of
    these raises :class:`~prxref.llm.ConfigError` naming the variable, so
    the CLI exits 2 rather than degrading the review.
    ``PRXREF_LLM_MAX_TOKENS`` is deliberately NOT read here: it is a
    per-call budget threaded cfg -> orchestrator -> reviewer -> ``invoke``,
    so a client-level copy could never win and would be dead config.

    The CLI backends (``claude-cli``, ``kiro-cli``) are built by
    :func:`prxref.llm_cli_backends.build_cli_client`, imported lazily.
    PRXREF_LLM_CLI_PATH overrides the binary (empty = ``claude`` or
    ``kiro-cli`` on ``PATH``; one that cannot be found is a ConfigError
    naming it). PRXREF_LLM_CLI_CONCURRENCY caps the CLI processes one client
    runs at once (integer >= 1, default ``DEFAULT_CLI_CONCURRENCY``), and is
    re-checked here for callers that bypass ``config.load_config``. Neither
    CLI has a temperature or seed option, so both are still parsed (a
    malformed value still exits 2) but are not applied, and an explicitly
    set one logs one WARNING saying so; the client's ``temperature`` and
    ``seed`` attributes are ``None``, which the run record's ``sampling``
    reports truthfully.
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
    if backend not in BACKENDS:
        raise ConfigError(
            f"PRXREF_LLM_BACKEND: must be one of {', '.join(BACKENDS)} "
            f"(case-insensitive), got {backend!r}"
        )
    raw_models = _get("LLM_MODELS", "PRXREF_LLM_MODELS", DEFAULT_MODELS) or ""
    models = [m.strip() for m in raw_models.split(",") if m.strip()]
    base_url = _get("LLM_BASE_URL", "PRXREF_LLM_BASE_URL", DEFAULT_BASE_URL) or ""
    if backend in OPENAI_COMPAT_BACKENDS and not base_url.strip():
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
    # The CLI — the caller whose cfg came from load_config — says which
    # layer supplied the deadline; every other caller falls back to the
    # inference below, where the environment is the only source that can
    # still bypass cfg and a resolved value equal to the default is the
    # default for every purpose that remains distinguishable.
    layer_flag = None
    for flag_key in ("LLM_TIMEOUT_IS_DEFAULT", "llm_timeout_is_default"):
        flag = (cfg or {}).get(flag_key)
        if isinstance(flag, bool):
            layer_flag = flag
            break
    timeout_is_default = layer_flag if layer_flag is not None else (
        not (os.environ.get("PRXREF_LLM_TIMEOUT") or "").strip()
        and timeout == DEFAULT_TIMEOUT
    )
    # Read (and validated) even for the backends that will not apply it, so a
    # malformed value still exits 2 rather than degrading the review.
    timeout_per_1k = _float_setting(
        _get("LLM_TIMEOUT_PER_1K", "PRXREF_LLM_TIMEOUT_PER_1K"),
        "PRXREF_LLM_TIMEOUT_PER_1K",
        minimum=0.0,
        exclusive=True,
    )
    if timeout_per_1k is None:
        timeout_per_1k = DEFAULT_TIMEOUT_PER_1K
    temperature = _float_setting(
        _get("LLM_TEMPERATURE", "PRXREF_LLM_TEMPERATURE"),
        "PRXREF_LLM_TEMPERATURE",
        minimum=0.0,
    )
    if temperature is None:
        temperature = DEFAULT_TEMPERATURE
    raw_seed = _get("LLM_SEED", "PRXREF_LLM_SEED")
    if raw_seed is not None and str(raw_seed).strip() == SEED_OFF:
        seed = None
    else:
        seed = _int_setting(raw_seed, "PRXREF_LLM_SEED", minimum=0)
        if seed is None:
            seed = _auto_run_seed()
    if backend in OPENAI_COMPAT_BACKENDS:
        return OpenAICompatClient(
            base_url=base_url,
            api_key=_get("LLM_API_KEY", "PRXREF_LLM_API_KEY", DEFAULT_API_KEY) or DEFAULT_API_KEY,
            models=models,
            session=session,
            default_timeout=timeout,
            reasoning_effort=_get("LLM_REASONING_EFFORT", "PRXREF_LLM_REASONING_EFFORT"),
            temperature=temperature,
            seed=seed,
            timeout_per_1k=timeout_per_1k if timeout_is_default else None,
        )
    if base_url.strip():
        logger.info(
            "PRXREF_LLM_BASE_URL is set but not used by the %s backend; ignoring it",
            backend,
        )
    if backend == "litellm":
        return LiteLLMClient(
            models=models,
            default_timeout=timeout,
            temperature=temperature,
            seed=seed,
            reasoning_effort=_get("LLM_REASONING_EFFORT", "PRXREF_LLM_REASONING_EFFORT"),
        )
    unapplied = [
        env
        for key, env in (
            ("LLM_TEMPERATURE", "PRXREF_LLM_TEMPERATURE"),
            ("LLM_SEED", "PRXREF_LLM_SEED"),
        )
        if (_get(key, env) or "").strip()
    ]
    if unapplied:
        logger.warning(
            "%s %s not applied by %s (the CLI has no such option)",
            " / ".join(unapplied),
            "is" if len(unapplied) == 1 else "are",
            backend,
        )
    concurrency = _int_setting(
        _get("LLM_CLI_CONCURRENCY", "PRXREF_LLM_CLI_CONCURRENCY"),
        "PRXREF_LLM_CLI_CONCURRENCY",
        minimum=1,
    )
    from .llm_cli_backends import build_cli_client

    return build_cli_client(
        backend,
        models=models,
        default_timeout=timeout,
        reasoning_effort=_get("LLM_REASONING_EFFORT", "PRXREF_LLM_REASONING_EFFORT") or None,
        cli_path=_get("LLM_CLI_PATH", "PRXREF_LLM_CLI_PATH") or "",
        concurrency=DEFAULT_CLI_CONCURRENCY if concurrency is None else concurrency,
    )
