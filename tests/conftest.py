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
