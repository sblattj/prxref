"""Gate watchdog: expiry repair and reconciliation (#102).

GitHub never redelivers a webhook on its own, so a push made while the
Mac slept is found here: ``GET /pulls?state=open`` per allowlisted
repository per reconcile interval (paged, at most ``_MAX_PAGES`` pages of
100), and a ``reconcile`` generation for any open PR whose live head
has no generation.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from prxref.gate.checks import GitHubError
from prxref.gate.store import Generation, Store

logger = logging.getLogger(__name__)

_PER_PAGE = 100
_MAX_PAGES = 10


def _list_open(client: Any, repo: str) -> list[dict]:
    """Every open PR of ``repo``, paged until a short page or the page cap.

    A GitHub error on a later page keeps the pages already fetched.
    """
    pulls: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        try:
            batch = (
                client.request("GET", repo, "/pulls", params={"state": "open", "per_page": _PER_PAGE, "page": page})
                or []
            )
        except GitHubError as exc:
            logger.error("reconcile %s page %d: %s status=%s", repo, page, type(exc).__name__, exc.status)
            return pulls
        pulls.extend(batch)
        if len(batch) < _PER_PAGE:
            return pulls
    logger.warning("reconcile %s: more than %d open PRs; the rest are not reconciled", repo, _MAX_PAGES * _PER_PAGE)
    return pulls


def _covers(row: Generation | None) -> bool:
    """True when ``row`` means the head has been (or will be) reviewed.

    A ``failed`` row with no check run never published anything, and a
    ``cancelled`` row was dropped by a close; neither covers its head.
    """
    if row is None:
        return False
    if row.state == "cancelled":
        return False
    return not (row.state == "failed" and row.check_run_id is None)


def reconcile(client: Any, store: Store, repos: Sequence[str]) -> list[Generation]:
    """Enqueue every open PR whose live head has no covering generation in the store.

    Heads only: a moved base is not noticed here (spec "watchdog.py").
    """
    made: list[Generation] = []
    for repo in repos:
        for pull in _list_open(client, repo):
            try:
                number, head, base, base_ref = (
                    pull["number"],
                    pull["head"]["sha"],
                    pull["base"]["sha"],
                    pull["base"]["ref"],
                )
            except (KeyError, TypeError) as exc:
                logger.error("reconcile %s: malformed pull (%s), skipped", repo, type(exc).__name__)
                continue
            if not _covers(store.latest_generation_for_head(repo, head)):
                made.append(store.enqueue(repo, number, head, base, base_ref, "reconcile"))
    return made


class Watchdog:
    """Periodic expiry repair plus reconciliation on start and on its own interval."""

    def __init__(
        self, store: Store, client: Any, repos: Sequence[str], *, watchdog_s: float, reconcile_s: float
    ) -> None:
        self.store = store
        self.client = client
        self.repos = list(repos)
        self.watchdog_s = watchdog_s
        self.reconcile_s = reconcile_s
        self._last_reconcile: float | None = None

    def tick(self, now: float) -> dict:
        """One pass: re-queue expired work; reconcile if due."""
        requeued = self.store.requeue_expired()
        if requeued:
            logger.warning("watchdog re-queued expired generations: %s", requeued)
        reconciled = None
        if self._last_reconcile is None or now - self._last_reconcile >= self.reconcile_s:
            reconciled = reconcile(self.client, self.store, self.repos)
            self._last_reconcile = now
        return {"requeued": requeued, "reconciled": reconciled}

    def run(self, stop: threading.Event, *, clock: Callable[[], float] = time.monotonic) -> None:
        """Tick every ``watchdog_s`` until ``stop`` is set."""
        while not stop.is_set():
            try:
                self.tick(clock())
            except Exception:  # noqa: BLE001
                logger.exception("watchdog tick failed")
            stop.wait(self.watchdog_s)
