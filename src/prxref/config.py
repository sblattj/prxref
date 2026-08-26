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
  PRXREF_CONFIDENCE_FLOOR       Findings below this confidence are dropped
                                (default 0.6)
  PRXREF_MAX_ERROR_FINDINGS     Max error-severity findings reported per
                                review (legacy alias: PRXREF_MAX_ERRORS)
  PRXREF_MAX_CHUNKS             Max diff chunks reviewed per PR (default 8)

Per-forge auth:
  PRXREF_BITBUCKET_TOKEN        Bitbucket bearer token
  PRXREF_BITBUCKET_USER         Bitbucket username (app-password pair)
  PRXREF_BITBUCKET_APP_PASSWORD Bitbucket app password
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
``None``-valued overrides are ignored (callers may pass optional values).
Unknown override keys raise ``ValueError`` so typos surface immediately.
"""
from __future__ import annotations

import os

from prxref.forges.base import Forge, PRRef

from .quality import DEFAULT_MAX_ERRORS

_ENV_PREFIX = "PRXREF_"

_DEFAULTS: dict[str, object] = {
    "llm_backend": "openai-compat",
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_models": [],
    "llm_reasoning_effort": "",
    "confidence_floor": 0.6,
    "max_error_findings": DEFAULT_MAX_ERRORS,
    "max_chunks": 8,
    "bitbucket_token": "",
    "bitbucket_user": "",
    "bitbucket_app_password": "",
    "github_token": "",
    "github_enterprise_token": "",
    "gitlab_token": "",
    "bitbucket_webhook_secret": "",
    "github_webhook_secret": "",
    "gitlab_webhook_secret": "",
    "allow_unsigned": False,
}

_INT_KEYS = frozenset({"max_error_findings", "max_chunks"})
_FLOAT_KEYS = frozenset({"confidence_floor"})
_BOOL_KEYS = frozenset({"allow_unsigned"})
_LIST_KEYS = frozenset({"llm_models"})

_LEGACY_ENV_ALIASES: dict[str, str] = {
    "max_error_findings": _ENV_PREFIX + "MAX_ERRORS",
}


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _coerce_env(key: str, raw: str) -> object:
    try:
        if key in _INT_KEYS:
            return int(raw)
        if key in _FLOAT_KEYS:
            return float(raw)
        if key in _BOOL_KEYS:
            return _truthy(raw)
        if key in _LIST_KEYS:
            return [part.strip() for part in raw.split(",") if part.strip()]
        return raw
    except ValueError as exc:
        raise ValueError(f"{_ENV_PREFIX}{key.upper()}: {exc}") from exc


def load_config(**overrides: object) -> dict:
    """Build the runtime config dict from defaults, environment, then overrides.

    Keys mirror the env table above (lowercase, no prefix). Env values are
    type-coerced per key (int / float / bool / comma-list / str); a malformed
    env value raises ``ValueError`` naming the offending variable.
    """
    cfg: dict[str, object] = dict(_DEFAULTS)
    for key in _DEFAULTS:
        raw = os.environ.get(_ENV_PREFIX + key.upper())
        if raw is None or raw == "":
            legacy = _LEGACY_ENV_ALIASES.get(key)
            raw = os.environ.get(legacy) if legacy else None
        if raw is None or raw == "":
            continue
        cfg[key] = _coerce_env(key, raw)
    for key, value in overrides.items():
        if key not in _DEFAULTS:
            raise ValueError(f"unknown config key: {key!r}")
        if value is None:
            continue
        cfg[key] = value
    return cfg


def make_forge(ref: PRRef, session=None) -> Forge:
    """Instantiate the ForgeImpl matching ``ref.forge``.

    ``session`` optionally injects a custom ``requests.Session`` (tests,
    shared connection pools). Unknown forge names raise ``ValueError``.
    """
    from prxref.forges import bitbucket, github, gitlab

    impls = {
        "bitbucket": bitbucket.ForgeImpl,
        "github": github.ForgeImpl,
        "gitlab": gitlab.ForgeImpl,
    }
    impl = impls.get(ref.forge)
    if impl is None:
        raise ValueError(f"unknown forge: {ref.forge!r}")
    return impl(session=session)
