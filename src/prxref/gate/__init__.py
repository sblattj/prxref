"""The sdlc review gate (#102): a local GitHub App whose check run gates merges.

``prxref gate serve`` runs the receiver, the worker and the watchdog over one
SQLite file. Nothing in this package imports PyJWT at module level; the app's
JWT is built through :func:`require_jwt`, so ``prxref`` without the
``github-app`` extra still imports, and the gate names the extra it lacks.
"""

from __future__ import annotations

import types

EXTRA_HINT = "install the github-app extra: pip install 'prxref[github-app]'"


class GateDependencyError(ImportError):
    """PyJWT (with its crypto backend) is not installed."""


def require_jwt() -> types.ModuleType:
    """Return the ``jwt`` module, or raise :class:`GateDependencyError` naming the extra."""
    try:
        import jwt
    except ImportError as e:
        raise GateDependencyError(f"prxref gate needs PyJWT with RS256 support; {EXTRA_HINT}") from e
    return jwt
