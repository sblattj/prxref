"""Optional one-shot review notification to a webhook (issue #40).

``PRXREF_NOTIFY_URL`` (env-only, empty by default) names a webhook, typically
a Slack incoming webhook. When it is set and the review was allowed to post,
one small JSON document is POSTed to it after the review finishes::

    {"text": "prxref: Request-Changes on owner/repo#12 (2 errors, 1 warning)",
     "pr_url": ..., "forge": ..., "repo": "owner/repo", "number": 12,
     "verdict": ..., "findings": {"error": 2, "warning": 1},
     "models": [...], "prxref_version": ..., "timestamp": <ISO 8601 UTC>}

One attempt, a fixed :data:`NOTIFY_TIMEOUT`, no retry, no redirect following.
The notification is advisory: it never raises and never changes the verdict,
the posted comments or the exit code. The outcome lands on the result as
``result["notify"] = {"status", "http_status", "error"}`` where ``status`` is
``"sent"``, ``"failed"`` or ``"off"`` (no URL, or a dry run). The URL is a
credential: only its host is logged, and ``error`` is an exception class name
or an HTTP status, never an exception message, which may echo the URL.
"""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import requests

from prxref import __version__

logger = logging.getLogger(__name__)

NOTIFY_TIMEOUT = 10
"""Seconds one notification attempt may take."""


def _outcome(status: str, http_status: int | None = None, error: str | None = None) -> dict:
    return {"status": status, "http_status": http_status, "error": error}


def _counts(result: Mapping[str, Any]) -> dict[str, int]:
    active = result.get("findings_active")
    if not isinstance(active, list):
        return {}
    counts = Counter(str(getattr(f, "severity", "unknown")) for f in active)
    return dict(sorted(counts.items(), key=lambda kv: (kv[0] != "error", kv[0] != "warning", kv[0])))


def _summary(ref: Any, verdict: str, counts: Mapping[str, int]) -> str:
    parts = [f"{n} {sev}{'' if n == 1 or sev == 'info' else 's'}" for sev, n in counts.items()]
    return f"prxref: {verdict} on {ref.owner}/{ref.repo}#{ref.number} ({', '.join(parts) or 'no findings'})"


def build_payload(result: Mapping[str, Any], ref: Any) -> dict[str, Any]:
    """The JSON document posted for a finished review of ``ref``."""
    verdict = str(result.get("verdict") or "unknown")
    counts = _counts(result)
    sampling = result.get("sampling")
    models = sampling.get("models") if isinstance(sampling, Mapping) else None
    payload: dict[str, Any] = {
        "text": _summary(ref, verdict, counts),
        "pr_url": ref.url,
        "forge": ref.forge,
        "repo": f"{ref.owner}/{ref.repo}",
        "number": ref.number,
        "verdict": verdict,
        "findings": counts,
        "prxref_version": __version__,
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if models:
        payload["models"] = list(models)
    return payload


def notify(result: Any, cfg: Mapping[str, Any], *, ref: Any, post: bool) -> None:
    """POST the review summary to ``cfg["notify_url"]`` and record the outcome.

    ``post`` is the effective post flag, so a dry run, ``--no-post`` or a
    replay never notifies. Does nothing when ``result`` is not a dict.
    """
    if not isinstance(result, dict):
        return
    url = str(cfg.get("notify_url") or "")
    if not url or not post:
        result["notify"] = _outcome("off")
        return
    host = urlsplit(url).hostname or "unknown-host"
    try:
        resp = requests.post(
            url, json=build_payload(result, ref), timeout=NOTIFY_TIMEOUT,
            allow_redirects=False,
        )
        status = int(resp.status_code)
    except Exception as exc:  # noqa: BLE001 - a notification never raises
        logger.warning("notify to %s failed: %s", host, type(exc).__name__)
        result["notify"] = _outcome("failed", None, type(exc).__name__)
        return
    if 200 <= status < 300:
        logger.info("notify to %s: HTTP %d", host, status)
        result["notify"] = _outcome("sent", status)
        return
    logger.warning("notify to %s failed: HTTP %d", host, status)
    result["notify"] = _outcome("failed", status, f"HTTP {status}")
