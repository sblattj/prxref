"""Subscription CLI backends: ``claude-cli`` and ``kiro-cli``.

These backends run the user's own installed, logged-in CLI as a subprocess
(argv list, never a shell; the user message on stdin; a fresh temporary
working directory) and walk ``PRXREF_LLM_MODELS`` as a caller-side chain,
exactly like the HTTP backends. The module is stdlib-only and is imported
lazily by :func:`prxref.llm_backends.create_llm_client`, so the HTTP
backends never load it. The backend names live in
``prxref.llm_backends.CLI_BACKENDS``.

This build does not wire either backend yet: both entry points fail closed
with a :class:`~prxref.llm.ConfigError` naming ``PRXREF_LLM_BACKEND``, which
``prxref review`` reports as a configuration error (exit 2) before any forge
or model call, rather than silently reviewing with nothing.
"""
from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence

from .llm import ConfigError, LLMClient


def _not_wired(backend: str) -> ConfigError:
    """The fail-closed error for a CLI backend this build cannot run."""
    return ConfigError(f"PRXREF_LLM_BACKEND: {backend} is not wired in this build")


def resolve_cli_binary(backend: str, cli_path: str, *, which=shutil.which) -> str:
    """Resolve the CLI binary for ``backend`` to an executable path.

    ``cli_path`` is ``PRXREF_LLM_CLI_PATH``: empty means the backend's default
    binary name on ``PATH``, otherwise a ``~``-expanded path or name looked up
    through ``which``. A binary that cannot be found raises
    :class:`~prxref.llm.ConfigError` naming ``PRXREF_LLM_CLI_PATH`` (or
    ``PRXREF_LLM_BACKEND`` when no override was given), so a missing CLI
    exits 2 before any network call.

    In this build every call raises the fail-closed ``ConfigError``.
    """
    raise _not_wired(backend)


def build_cli_client(
    backend: str,
    *,
    models: Sequence[str],
    default_timeout: float,
    reasoning_effort: str | None,
    cli_path: str,
    concurrency: int,
    which=shutil.which,
    runner=subprocess.Popen,
) -> LLMClient:
    """Build the client for ``backend`` (one of ``llm_backends.CLI_BACKENDS``).

    ``models`` is the chain walked in order; ``default_timeout`` is the
    per-model deadline in seconds; ``reasoning_effort`` feeds claude's effort
    setting and is ignored by kiro; ``cli_path`` is passed to
    :func:`resolve_cli_binary`; ``concurrency`` caps the CLI processes this
    client runs at once. ``which`` and ``runner`` are the binary lookup and
    the process launcher, injectable for tests.

    In this build every call raises the fail-closed ``ConfigError``.
    """
    raise _not_wired(backend)
