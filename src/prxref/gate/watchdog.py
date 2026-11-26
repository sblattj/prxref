"""Gate watchdog: expiry repair and reconciliation (#102).

GitHub never redelivers a webhook on its own, so a push made while the
Mac slept is found here: ``GET /pulls?state=open`` per allowlisted
repository per reconcile interval (paged, at most ``_MAX_PAGES`` pages of
100), and a ``reconcile`` generation for any open PR whose live head
has no generation.

A head this store has never seen may still have been reviewed by another
instance of the same App (a second gate left running against the same
installation). Before enqueuing, reconcile asks GitHub for this App's
completed ``sdlc-review`` run on the head and skips it when one exists, so a
second instance never posts a newer run over the first one's verdict.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from prxref.gate.checks import GitHubError
from prxref.gate.receiver import CHECK_NAME
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


def _completed_elsewhere(client: Any, repo: str, head: str, app_id: int) -> str | None:
    """The external id of a completed ``sdlc-review`` run by ``app_id`` on ``head``, or None.

    A run still queued or in progress does not count, so a run left behind by
    a dead instance is still repaired. A failed lookup returns None, which
    keeps the pre-check behavior.
    """
    try:
        data = client.request(
            "GET",
            repo,
            f"/commits/{head}/check-runs",
            params={"check_name": CHECK_NAME, "app_id": app_id, "filter": "all"},
        )
    except GitHubError as exc:
        logger.error("reconcile %s check lookup: %s status=%s", repo, type(exc).__name__, exc.status)
        return None
    for run in (data or {}).get("check_runs", []):
        if run.get("status") == "completed":
            return str(run.get("external_id") or run.get("id"))
    return None


def reconcile(
    client: Any, store: Store, repos: Sequence[str], *, app_id: int | None = None
) -> list[Generation]:
    """Enqueue every open PR whose live head has no covering generation in the store.

    With ``app_id``, a head the store does not cover is skipped when GitHub
    already shows a completed ``sdlc-review`` run by that App on it.

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
            if _covers(store.latest_generation_for_head(repo, head)):
                continue
            if app_id is not None:
                other = _completed_elsewhere(client, repo, head, app_id)
                if other is not None:
                    logger.warning(
                        "reconcile %s#%s: head %s already has a completed %s run (external_id %s) "
                        "from another instance of this App; not re-reviewed",
                        repo, number, head[:12], CHECK_NAME, other,
                    )
                    continue
            made.append(store.enqueue(repo, number, head, base, base_ref, "reconcile"))
    return made


class Watchdog:
    """Periodic expiry repair plus reconciliation on start and on its own interval."""

    def __init__(
        self,
        store: Store,
        client: Any,
        repos: Sequence[str],
        *,
        watchdog_s: float,
        reconcile_s: float,
        app_id: int | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.repos = list(repos)
        self.watchdog_s = watchdog_s
        self.reconcile_s = reconcile_s
        self.app_id = app_id
        self._last_reconcile: float | None = None

    def tick(self, now: float) -> dict:
        """One pass: re-queue expired work; reconcile if due."""
        requeued = self.store.requeue_expired()
        if requeued:
            logger.warning("watchdog re-queued expired generations: %s", requeued)
        reconciled = None
        if self._last_reconcile is None or now - self._last_reconcile >= self.reconcile_s:
            reconciled = reconcile(self.client, self.store, self.repos, app_id=self.app_id)
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
