"""Suite-wide fixtures.

``_clear_prxref_env`` below is the ONLY env-clear surface in this suite. It
derives the variable names from ``config._DEFAULTS`` and
``config._LEGACY_ENV_ALIASES`` instead of listing them, so a key added to the
config schema is cleared without anyone remembering to append it to a
hand-maintained list.

That per-task tax is not hypothetical: hand-maintained lists in four test
modules had already drifted apart, and the first env var whose parse could fail
(``PRXREF_LLM_TEMPERATURE``) turned an ambient typo in a developer's shell into
eight red tests in code they had never touched. Derivation makes that
structurally impossible.

Tests that need a variable set still set it themselves with
``monkeypatch.setenv``; the autouse clear runs first, so their value wins.
"""
from __future__ import annotations

import pytest

from prxref import config


def prxref_env_names() -> list[str]:
    """Every PRXREF_* name the config schema can read, derived from the schema."""
    names = {config._ENV_PREFIX + key.upper() for key in config._DEFAULTS}
    names.update(config._LEGACY_ENV_ALIASES.values())
    return sorted(names)


def clear_prxref_env(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Delete every schema-derived PRXREF_* name; returns the names cleared."""
    names = prxref_env_names()
    for name in names:
        monkeypatch.delenv(name, raising=False)
    return names


@pytest.fixture(autouse=True)
def _clear_prxref_env(monkeypatch):
    """No ambient PRXREF_* reaches any test; every test sets what it needs."""
    clear_prxref_env(monkeypatch)


@pytest.fixture
def contract_stubs(monkeypatch):
    """Pin the reviewer contract: chunk, sweep and summary-prompt stubs. Opt-in.

    ``tests/test_orchestrator.py`` requests it from an autouse wrapper, so every
    test there runs against the stubs. Any other module opts in with
    ``@pytest.mark.usefixtures("contract_stubs")``; a test that must prove the
    real prompts end to end leaves it off and runs the real reviewer.

    The systemic sweep is stubbed to a clean no-findings success so
    sweep-specific tests can monkeypatch their own doubles; chunk-count
    assertions include the sweep unit.

    The stubs live in ``tests/test_orchestrator.py`` and are imported here, at
    call time, so loading this conftest never imports a test module.
    """
    from prxref import orchestrator
    from tests.test_orchestrator import (
        _contract_load_prompt,
        _contract_review_chunk,
        _contract_review_systemic,
    )

    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _contract_review_chunk)
    monkeypatch.setattr(
        orchestrator.reviewer, "review_systemic", _contract_review_systemic,
    )
    monkeypatch.setattr(orchestrator.reviewer, "load_prompt", _contract_load_prompt)
