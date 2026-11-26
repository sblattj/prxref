"""Reconcile never re-reviews a head another instance of the same App already reviewed.

PR #207: the AWS gate reviewed head e9ece21 from the webhook and completed a
passing ``sdlc-review`` check at 20:51:00. A second, stale instance of the same
App, whose store had never seen that head, reconciled 44 s later, reviewed it
again and posted a failing check. GitHub counts the newest run per check name,
so the failure replaced the pass and blocked the merge. Reconcile now asks
GitHub for this App's completed ``sdlc-review`` run on the head before it
enqueues one of its own.
"""

import logging

import pytest

from prxref.gate import watchdog
from prxref.gate.checks import GitHubError
from prxref.gate.store import Store

APP = 5278363
HEAD, BASE = "e9ece211856a1b9bc9e835aad367364ce3065cfa", "d" * 40
REPO = "acme/prxref"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "gate.db")
    yield s
    s.close()


def _pull():
    return {"number": 207, "head": {"sha": HEAD}, "base": {"sha": BASE, "ref": "main"}}


class _Client:
    """Open PRs from ``/pulls``; ``runs`` (or an exception) from ``/check-runs``."""

    def __init__(self, runs):
        self.runs = runs
        self.calls = []

    def request(self, method, repo, path, **kwargs):
        self.calls.append((method, path, kwargs.get("params")))
        if path == "/pulls":
            return [_pull()]
        if isinstance(self.runs, Exception):
            raise self.runs
        return {"total_count": len(self.runs), "check_runs": self.runs}


def _run(status="completed", conclusion="success", ext="376"):
    return {"id": 140163068298, "status": status, "conclusion": conclusion, "external_id": ext}


def test_a_head_another_instance_completed_is_not_rereviewed(store, caplog):
    client = _Client([_run()])
    with caplog.at_level(logging.WARNING, logger="prxref.gate.watchdog"):
        made = watchdog.reconcile(client, store, [REPO], app_id=APP)
    assert made == []
    lookup = [c for c in client.calls if c[1] != "/pulls"]
    assert lookup == [
        ("GET", f"/commits/{HEAD}/check-runs", {"check_name": "sdlc-review", "app_id": APP, "filter": "all"})
    ]
    assert "another instance" in caplog.text and "376" in caplog.text


def test_a_completed_failure_also_covers_the_head(store):
    assert watchdog.reconcile(_Client([_run(conclusion="failure")]), store, [REPO], app_id=APP) == []


def test_a_head_with_no_run_is_still_reconciled(store):
    made = watchdog.reconcile(_Client([]), store, [REPO], app_id=APP)
    assert [(g.pr_number, g.head_sha, g.reason) for g in made] == [(207, HEAD, "reconcile")]


def test_a_run_still_in_progress_does_not_cover_the_head(store):
    made = watchdog.reconcile(_Client([_run(status="in_progress", conclusion=None)]), store, [REPO], app_id=APP)
    assert [g.pr_number for g in made] == [207]


def test_a_failed_lookup_falls_back_to_reconciling(store):
    made = watchdog.reconcile(_Client(GitHubError("GET check-runs: HTTP 502", status=502)), store, [REPO], app_id=APP)
    assert [g.pr_number for g in made] == [207]


def test_a_head_the_store_covers_asks_github_nothing(store):
    store.enqueue(REPO, 207, HEAD, BASE, "main", "opened")
    client = _Client([])
    assert watchdog.reconcile(client, store, [REPO], app_id=APP) == []
    assert [c[1] for c in client.calls] == ["/pulls"]


def test_the_watchdog_passes_its_app_id(store):
    client = _Client([_run()])
    dog = watchdog.Watchdog(store, client, [REPO], watchdog_s=60, reconcile_s=600, app_id=APP)
    assert dog.tick(0.0)["reconciled"] == []
