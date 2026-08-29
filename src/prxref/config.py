"""Configuration loading and forge-factory wiring.

Canonical environment-variable table (every name prefixed PRXREF_):

LLM / pipeline:
  PRXREF_LLM_BACKEND            LLM backend: openai-compat | ferry | http (aliases) | litellm
  PRXREF_LLM_BASE_URL           Base URL for the chosen backend (optional)
  PRXREF_LLM_API_KEY            API key for the chosen backend (optional)
  PRXREF_LLM_MODELS             Comma-separated model fallback chain, first
                                that answers wins; empty = backend default
  PRXREF_LLM_REASONING_EFFORT   Reasoning effort for models that cannot
                                disable reasoning; provider-specific string,
                                passed through unvalidated; empty = omit
  PRXREF_LLM_MAX_TOKENS         Completion-token budget per worker review
                                call; positive int (default 4096)
  PRXREF_LLM_TIMEOUT            Per-request LLM timeout in seconds; must be
                                greater than 0 (default 45.0)
  PRXREF_LLM_TEMPERATURE        Sampling temperature, e.g. "0.2"; finite and
                                >= 0, no upper bound (provider-specific);
                                empty = omit from the request
  PRXREF_CONFIDENCE_FLOOR       Findings below this confidence are dropped;
                                a probability in [0.0, 1.0] (default 0.6)
  PRXREF_MAX_ERROR_FINDINGS     Max error-severity findings reported per
                                review; >= 0, where 0 caps every error
                                (legacy alias: PRXREF_MAX_ERRORS)
  PRXREF_MAX_CHUNKS             Max diff chunks reviewed per PR; positive int
                                (default 8)
  PRXREF_CHUNK_TOKEN_BUDGET     Approximate token budget per diff chunk;
                                lowering it splits a PR into more, smaller
                                chunks (positive int, default 25000)
  PRXREF_CHUNK_MAX_FILES        Cap on files placed in one review chunk;
                                chunks stay under it while any chunk has
                                room, and the max_chunks overflow branch
                                may exceed it rather than drop a file
                                (positive int, default 5)
  PRXREF_CHUNK_CONTEXT_LINES    Context lines kept around each change when a
                                chunk's diff is rendered for the worker
                                prompt; 0 emits the changed lines only.
                                Trims the forge's diff, never adds
                                (int >= 0, default 3)
  PRXREF_MAX_WORKERS            Parallel chunk-review workers; positive int
                                (default 4)
  PRXREF_MAX_INLINE_COMMENTS    Max inline comments posted per review, after
                                the quality gate; positive int (default 15)
  PRXREF_DRY_RUN                literal "1" reviews without writing anything
                                to the forge — no summary, no inline comments
                                (default off). Applies to the webhook daemon
                                as well as the CLI; ``--no-post`` is the
                                per-invocation equivalent and still wins.
  PRXREF_FAIL_ON                Exit-code policy for ``prxref review``:
                                "never" (default) keeps the advisory
                                contract — the exit code never reflects
                                findings; "error" exits 1 when the
                                completed review carries an active
                                error-severity finding; "any" exits 1 on
                                any active finding. Under "error" and
                                "any", a review that fails to complete
                                also exits 1. The webhook daemon has no
                                exit code and is unaffected.
  PRXREF_POST_MODE              What gets posted to the forge:
                                "summary+inline" (default) | "summary" |
                                "inline". Any other value is a
                                configuration error. Superseded entirely by
                                PRXREF_DRY_RUN / ``--no-post``, which post
                                nothing in any mode.
  PRXREF_POST_VERDICT           literal "1" keeps the verdict stamp in the
                                posted summary; any other value renders the
                                summary without it (default on). The
                                total-failure notice always names its status.

Per-forge auth:
  PRXREF_BITBUCKET_TOKEN        Bitbucket Cloud bearer token
  PRXREF_BITBUCKET_USER         Bitbucket Cloud username (app-password pair)
  PRXREF_BITBUCKET_APP_PASSWORD Bitbucket Cloud app password
  PRXREF_BITBUCKET_SERVER_TOKEN Bitbucket Server/Data Center HTTP access token
                                (falls back to PRXREF_BITBUCKET_TOKEN)
  PRXREF_BITBUCKET_SERVER_USER  Bitbucket Server username (basic-auth pair)
  PRXREF_BITBUCKET_SERVER_PASSWORD Bitbucket Server password (basic-auth pair)
  PRXREF_GITHUB_TOKEN           GitHub token (github.com)
  PRXREF_GITHUB_ENTERPRISE_TOKEN GitHub Enterprise token (GHES hosts)
  PRXREF_GITLAB_TOKEN           GitLab token

Webhooks:
  PRXREF_BITBUCKET_WEBHOOK_SECRET HMAC secret for Bitbucket webhook payloads
  PRXREF_GITHUB_WEBHOOK_SECRET    HMAC secret for GitHub webhook payloads
  PRXREF_GITLAB_WEBHOOK_SECRET    HMAC secret for GitLab webhook payloads
  PRXREF_ALLOW_UNSIGNED           literal "1" accepts unsigned
                                  webhooks (default off; insecure)

Precedence: built-in defaults < environment < ``overrides`` kwargs.
An error names the source that actually supplied the offending value — the
environment variable that was read (including a legacy alias), or the caller's
own name for an override (``--max-chunks`` rather than ``PRXREF_MAX_CHUNKS``
when the flag is what the operator typed).
An empty or whitespace-only environment value reads as unset, so a stray
``PRXREF_LLM_TIMEOUT= `` in a .env file keeps the default instead of aborting.
``None``-valued overrides are ignored (callers may pass optional values).
Unknown override keys raise ``ValueError`` so typos surface immediately.
A malformed value, one out of its numeric range, or one outside its key's
allowed vocabulary (``PRXREF_FAIL_ON`` accepts only never | error | any)
raises :class:`~prxref.llm.ConfigError`, which the CLI reports as a
configuration error and exits 2 for — never as a review failure.
"""
from __future__ import annotations

import math
import os
from typing import NamedTuple

from prxref.forges.base import Forge, PRRef

from .llm import ConfigError
from .quality import DEFAULT_CONFIDENCE_FLOOR, DEFAULT_MAX_ERRORS
from .triage import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_MAX_FILES_PER_CHUNK,
    DEFAULT_TOKEN_BUDGET,
)

_ENV_PREFIX = "PRXREF_"

_DEFAULTS: dict[str, object] = {
    "llm_backend": "openai-compat",
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_models": [],
    "llm_reasoning_effort": "",
    "llm_max_tokens": 4096,
    "llm_timeout": 45.0,
    "llm_temperature": "",
    "confidence_floor": DEFAULT_CONFIDENCE_FLOOR,
    "max_error_findings": DEFAULT_MAX_ERRORS,
    "max_chunks": 8,
    "chunk_token_budget": DEFAULT_TOKEN_BUDGET,
    "chunk_max_files": DEFAULT_MAX_FILES_PER_CHUNK,
    "chunk_context_lines": DEFAULT_CONTEXT_LINES,
    # Mirrors orchestrator.MAX_WORKERS / MAX_INLINE_COMMENTS. Restated rather
    # than imported: config is a leaf module and importing the orchestrator
    # here would pull the whole review pipeline into every config read.
    # TestChunkingAndFanoutKnobs pins the three literals together.
    "max_workers": 4,
    "max_inline_comments": 15,
    "fail_on": "never",
    "dry_run": False,
    "post_mode": "summary+inline",
    "post_verdict": True,
    "bitbucket_token": "",
    "bitbucket_user": "",
    "bitbucket_app_password": "",
    "bitbucket_server_token": "",
    "bitbucket_server_user": "",
    "bitbucket_server_password": "",
    "github_token": "",
    "github_enterprise_token": "",
    "gitlab_token": "",
    "bitbucket_webhook_secret": "",
    "github_webhook_secret": "",
    "gitlab_webhook_secret": "",
    "allow_unsigned": False,
}

_INT_KEYS = frozenset({
    "max_error_findings", "max_chunks", "llm_max_tokens",
    "chunk_token_budget", "chunk_max_files", "chunk_context_lines",
    "max_workers", "max_inline_comments",
})
_FLOAT_KEYS = frozenset({"confidence_floor", "llm_timeout"})
_BOOL_KEYS = frozenset({"allow_unsigned", "dry_run", "post_verdict"})
_LIST_KEYS = frozenset({"llm_models"})

# An enum-valued key has no numeric interval to check, so its legal vocabulary
# is declared here instead and enforced on the same pass as the ranges. A
# value outside the set is a ConfigError (exit 2) naming the legal values —
# never a silent fall-back to the default, which would turn a typo'd
# PRXREF_FAIL_ON=eror into an undetected "never".
_CHOICE_KEYS: dict[str, frozenset[str]] = {
    "fail_on": frozenset({"never", "error", "any"}),
}

# The posting-behaviour vocabulary, validated rather than trusted. Restated in
# prxref.orchestrator (config stays a leaf module); pinned together by
# TestPostMode::test_the_vocabulary_matches_the_orchestrator.
_POST_MODES = ("summary+inline", "summary", "inline")


class _Range(NamedTuple):
    """The legal interval for one numeric config key.

    ``high`` defaults to ``math.inf`` — unbounded above, deliberately. The
    ceiling for a token budget, a timeout or a worker count is provider- and
    machine-specific, and an invented limit would be worse than none. Only a
    semantically bounded quantity gets a real ``high``: today that is the
    confidence floor, which is a 0-1 probability everywhere in
    ``triage.Finding`` and in the prompts.

    ``low_inclusive`` distinguishes "must be positive" from "must not be
    negative". Zero is meaningless for a token budget (it asks the model for an
    empty completion), for a timeout (every request fails instantly), for a
    worker count (``ThreadPoolExecutor`` rejects it) and for a chunk count
    (``build_chunks`` raises on the overflow branch). Zero IS meaningful for the
    error cap, where it means "report no errors", and for the context-line
    count, where it means "emit the changed lines only".
    """

    low: float
    high: float = math.inf
    low_inclusive: bool = False

    def accepts(self, value: float) -> bool:
        """True if ``value`` lies inside the interval; assumes it is finite."""
        low_ok = value >= self.low if self.low_inclusive else value > self.low
        return low_ok and value <= self.high

    def describe(self) -> str:
        """The bound in words, for the error an operator has to act on."""
        low = (
            f"greater than or equal to {self.low}"
            if self.low_inclusive
            else f"greater than {self.low}"
        )
        if math.isinf(self.high):
            return f"must be a finite number {low}"
        return f"must be a finite number {low} and at most {self.high}"


# The whole numeric surface, in one place. Checked after environment AND
# overrides, so no path into the config can smuggle a degenerate value through.
# Every key in _INT_KEYS | _FLOAT_KEYS must appear here;
# TestPreExistingNumericRanges::test_every_numeric_key_declares_a_range fails
# if a future key is added without a bound.
_RANGES: dict[str, _Range] = {
    "llm_max_tokens": _Range(0),
    "llm_timeout": _Range(0),
    "chunk_token_budget": _Range(0),
    "max_workers": _Range(0),
    "max_inline_comments": _Range(0),
    "max_chunks": _Range(0),
    "chunk_max_files": _Range(0),
    "chunk_context_lines": _Range(0, low_inclusive=True),
    "max_error_findings": _Range(0, low_inclusive=True),
    "confidence_floor": _Range(0.0, 1.0, low_inclusive=True),
}

_LEGACY_ENV_ALIASES: dict[str, str] = {
    "max_error_findings": _ENV_PREFIX + "MAX_ERRORS",
}


def _truthy(raw: str) -> bool:
    """Parse a security-gating boolean; only the literal "1" enables it.

    Deliberately rejects "true"/"yes"/"on" so a typo or a shell quirk fails safe
    with verification left ON. Must stay identical to
    prxref.webhooks._allow_unsigned, which is the gate that actually runs;
    TestAllowUnsignedAgreesWithGate pins the two together.
    """
    return raw.strip() == "1"


def _coerce_env(key: str, raw: str, source: str) -> object:
    """Type-coerce one env value; a malformed one is a usage error, not a crash.

    ``source`` is the variable the value was actually read from, so a value set
    through a legacy alias is reported under the name the operator typed.
    """
    try:
        if key in _INT_KEYS:
            return int(raw.strip())
        if key in _FLOAT_KEYS:
            return float(raw.strip())
        if key in _BOOL_KEYS:
            return _truthy(raw)
        if key in _LIST_KEYS:
            return [part.strip() for part in raw.split(",") if part.strip()]
        return raw
    except ValueError as exc:
        raise ConfigError(f"{source}: {exc}") from exc


def _check_ranges(cfg: dict[str, object], sources: dict[str, str]) -> None:
    """Reject out-of-range numbers before they reach the wire.

    Runs after environment AND overrides, so every path into the config is
    covered. Failures are ``ConfigError`` (exit 2) rather than a mid-review
    error the operator has to decode from a provider's rejection — or, worse, a
    review that looks like it succeeded.

    The message names ``sources[key]``: whichever input actually supplied the
    offending value. Naming the environment variable unconditionally sent an
    operator who typed ``--max-chunks 0`` to hunt for a ``PRXREF_MAX_CHUNKS``
    they had never set.
    """
    for key, rng in sorted(_RANGES.items()):
        value = cfg[key]
        finite = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
        if not finite or not rng.accepts(value):
            raise ConfigError(f"{sources[key]}: {rng.describe()}, got {value!r}")


def _check_choices(cfg: dict[str, object], sources: dict[str, str]) -> None:
    """Reject a value outside its key's allowed vocabulary.

    Same pass, same failure mode as :func:`_check_ranges`: it runs after
    environment AND overrides, and the message names ``sources[key]`` —
    whichever input actually supplied the offending value.
    """
    for key, choices in sorted(_CHOICE_KEYS.items()):
        value = cfg[key]
        if isinstance(value, str) and value in choices:
            continue
        allowed = ", ".join(repr(c) for c in sorted(choices))
        raise ConfigError(f"{sources[key]}: must be one of {allowed}, got {value!r}")


def _check_post_mode(cfg: dict[str, object], sources: dict[str, str]) -> None:
    """Reject a ``post_mode`` outside the documented vocabulary.

    Same doctrine as :func:`_check_ranges`: it runs after environment AND
    overrides, and a failure is a ``ConfigError`` naming whichever input
    supplied the value — exit 2 before anything is reviewed, never a silent
    fall-back to the default mode that would post findings nobody asked for.
    """
    value = cfg["post_mode"]
    if value not in _POST_MODES:
        raise ConfigError(
            f"{sources['post_mode']}: must be one of "
            f"{' | '.join(_POST_MODES)}, got {value!r}"
        )


def load_config(
    *, source_labels: dict[str, str] | None = None, **overrides: object
) -> dict:
    """Build the runtime config dict from defaults, environment, then overrides.

    Keys mirror the env table above (lowercase, no prefix). Env values are
    type-coerced per key (int / float / bool / comma-list / str); an empty or
    whitespace-only value reads as unset. A malformed value, one out of its
    numeric range, or one outside its key's allowed vocabulary raises
    :class:`~prxref.llm.ConfigError` naming the input that supplied it, which
    the CLI turns into exit 2.

    ``source_labels`` lets a caller say what its user calls an override — the
    CLI passes ``{"max_chunks": "--max-chunks"}`` so a bad flag is reported as
    the flag. It is used for error messages only, and only for keys the caller
    actually overrode; an unlabelled override is reported under its config key.
    Config itself knows no flag names: the caller that owns the surface names
    it.
    """
    cfg: dict[str, object] = dict(_DEFAULTS)
    # What supplied each value, for error messages. Defaults start out attributed
    # to their environment variable: that is the name an operator would set to
    # change one, and a built-in default is never out of range anyway.
    sources: dict[str, str] = {key: _ENV_PREFIX + key.upper() for key in _DEFAULTS}
    labels = source_labels or {}
    for key in _DEFAULTS:
        name = _ENV_PREFIX + key.upper()
        raw = os.environ.get(name)
        if raw is None or not raw.strip():
            legacy = _LEGACY_ENV_ALIASES.get(key)
            if legacy:
                raw = os.environ.get(legacy)
                name = legacy
        if raw is None or not raw.strip():
            continue
        cfg[key] = _coerce_env(key, raw, name)
        sources[key] = name
    for key, value in overrides.items():
        if key not in _DEFAULTS:
            raise ValueError(f"unknown config key: {key!r}")
        if value is None:
            continue
        cfg[key] = value
        sources[key] = labels.get(key, key)
    _check_ranges(cfg, sources)
    _check_choices(cfg, sources)
    _check_post_mode(cfg, sources)
    return cfg


def make_forge(ref: PRRef, session=None) -> Forge:
    """Instantiate the ForgeImpl matching ``ref.forge``.

    ``session`` optionally injects a custom ``requests.Session`` (tests,
    shared connection pools). Unknown forge names raise ``ValueError``.
    """
    from prxref.forges import bitbucket, bitbucket_server, github, gitlab

    impls = {
        "bitbucket": bitbucket.ForgeImpl,
        "bitbucket-server": bitbucket_server.ForgeImpl,
        "github": github.ForgeImpl,
        "gitlab": gitlab.ForgeImpl,
    }
    impl = impls.get(ref.forge)
    if impl is None:
        raise ValueError(f"unknown forge: {ref.forge!r}")
    return impl(session=session)
