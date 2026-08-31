"""A urllib3 retry policy that says out loud that it retried.

Retries happen underneath the requests adapter, so a call that is transparently
re-sent three times looks from above like one slow call. A review that took
four minutes because GitHub 502'd twice is indistinguishable, in the logs, from
one that took four minutes for no reason at all -- and the second reading is
the one that gets debugged.

:class:`LoggingRetry` is a drop-in for :class:`urllib3.util.Retry` that emits
one WARNING per retry naming the verb, the URL, why, and how long the sleep
will be, plus one when the budget runs out. It changes no retry behaviour.
"""
from __future__ import annotations

import logging
from typing import Any

from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


def _redacted(url: str | None) -> str:
    """Return ``url`` without its query string.

    Credentials travel in headers on every forge here, but a query string is
    the one part of a URL that carries a token when one ever does, and a log
    line is the wrong place to find out.
    """
    if not url:
        return "<no url>"
    return url.split("?", 1)[0]


def _reason(response: Any, error: Exception | None) -> str:
    if error is not None:
        return f"{error.__class__.__name__}: {error}"
    status = getattr(response, "status", None)
    if status is not None:
        return f"HTTP {status}"
    return "unknown"


class LoggingRetry(Retry):
    """A :class:`Retry` that logs each retry it grants and the one it refuses.

    ``Retry.new`` rebuilds the policy with ``type(self)(**params)`` on every
    hop, so a subclass that adds no constructor arguments -- this one -- stays
    in force for the whole request rather than decaying back to the base class.
    """

    def increment(  # noqa: D102 - inherited contract, see class docstring
        self,
        method: str | None = None,
        url: str | None = None,
        response: Any = None,
        error: Exception | None = None,
        _pool: Any = None,
        _stacktrace: Any = None,
    ) -> Retry:
        try:
            nxt = super().increment(
                method=method,
                url=url,
                response=response,
                error=error,
                _pool=_pool,
                _stacktrace=_stacktrace,
            )
        except Exception:
            logger.warning(
                "retry budget exhausted: %s %s after %d retries (%s)",
                method or "?",
                _redacted(url),
                len(self.history or ()),
                _reason(response, error),
            )
            raise
        logger.warning(
            "retrying %s %s in %.1fs: %s (retry %d)",
            method or "?",
            _redacted(url),
            nxt.get_backoff_time(),
            _reason(response, error),
            len(nxt.history or ()),
        )
        return nxt
