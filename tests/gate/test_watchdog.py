"""Watchdog: expiry repair and reconciliation (#102)."""

import logging
import threading

import pytest

from prxref.gate import watchdog
from prxref.gate.checks import GitHubError, UncertainReply
from prxref.gate.store import Store

H1, H2, BASE = "1" * 40, "2" * 40, "b" * 40


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "gate.db")
    yield s
    s.close()


def _pull(n, head):
    return {"number": n, "head": {"sha": head}, "base": {"sha": BASE, "ref": "main"}}


class _Client:
    def __init__(self, by_repo):
        self.by_repo = by_repo
        self.calls = []

    def request(self, method, repo, path, **kwargs):
        self.calls.append((method, repo, path, kwargs.get("params")))
        answer = self.by_repo[repo]
        if isinstance(answer, Exception):
            raise answer
        return answer


class _PagedClient:
    """Serves ``pages[page - 1]``; the last page repeats when ``repeat_last`` is set."""

    def __init__(self, pages, repeat_last=False):
        self.pages = pages
        self.repeat_last = repeat_last
        self.calls = []

    def request(self, method, repo, path, **kwargs):
        params = kwargs.get("params")
        self.calls.append((method, repo, path, params))
        idx = params["page"] - 1
        if idx >= len(self.pages):
            if not self.repeat_last:
                return []
            idx = len(self.pages) - 1
        return self.pages[idx]


def test_reconcile_enqueues_only_uncovered_heads(store):
    store.enqueue("acme/a", 1, H1, BASE, "main", "opened")
    client = _Client({"acme/a": [_pull(1, H1), _pull(2, H2)]})
    made = watchdog.reconcile(client, store, ["acme/a"])
    assert [(g.pr_number, g.head_sha, g.reason) for g in made] == [(2, H2, "reconcile")]
    assert client.calls == [("GET", "acme/a", "/pulls", {"state": "open", "per_page": 100, "page": 1})]


def test_reconcile_survives_one_failing_repo(store):
    client = _Client({"acme/a": GitHubError("GET /pulls: HTTP 404", status=404), "acme/b": [_pull(3, H1)]})
    made = watchdog.reconcile(client, store, ["acme/a", "acme/b"])
    assert [(g.repo, g.pr_number) for g in made] == [("acme/b", 3)]


def test_reconcile_survives_uncertain_reply_and_logs_class_and_status_only(store, caplog):
    err = UncertainReply("GET /pulls: timeout SECRET-TOKEN-xyz", status=502)
    client = _Client({"acme/a": err, "acme/b": [_pull(3, H1)]})
    with caplog.at_level(logging.ERROR, logger="prxref.gate.watchdog"):
        made = watchdog.reconcile(client, store, ["acme/a", "acme/b"])
    assert [(g.repo, g.pr_number) for g in made] == [("acme/b", 3)]
    assert "UncertainReply" in caplog.text and "502" in caplog.text and "acme/a" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text


def test_reconcile_paginates_until_a_short_page(store):
    pages = [[_pull(n, f"{n:040x}") for n in range(1, 101)], [_pull(n, f"{n:040x}") for n in range(101, 104)]]
    client = _PagedClient(pages)
    made = watchdog.reconcile(client, store, ["acme/a"])
    assert len(made) == 103
    assert [c[3]["page"] for c in client.calls] == [1, 2]


def test_reconcile_caps_pages_and_warns(store, caplog):
    full = [_pull(n, f"{n:040x}") for n in range(1, 101)]
    client = _PagedClient([full], repeat_last=True)
    with caplog.at_level(logging.WARNING, logger="prxref.gate.watchdog"):
        watchdog.reconcile(client, store, ["acme/a"])
    assert [c[3]["page"] for c in client.calls] == list(range(1, 11))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "acme/a" in warnings[0].getMessage()


def test_reconcile_enqueues_pr_whose_delivery_was_recorded_without_a_generation(store):
    assert store.record_delivery("d-1", "pull_request", "opened", "acme/a", 7)
    assert store.latest_generation("acme/a", 7) is None
    client = _Client({"acme/a": [_pull(7, H1)]})
    made = watchdog.reconcile(client, store, ["acme/a"])
    assert [(g.pr_number, g.head_sha, g.reason) for g in made] == [(7, H1, "reconcile")]
    stored = store.latest_generation("acme/a", 7)
    assert stored is not None and stored.id == made[0].id and stored.state == "queued"


def test_reconcile_ignores_a_moved_base(store):
    gen = store.enqueue("acme/a", 1, H1, BASE, "main", "opened")
    token = store.acquire_lease("w", 60)
    store.start(gen.id, token)
    store.finish(gen.id, token, "completed")
    moved = {"number": 1, "head": {"sha": H1}, "base": {"sha": "c" * 40, "ref": "main"}}
    assert watchdog.reconcile(_Client({"acme/a": [moved]}), store, ["acme/a"]) == []


def test_reconcile_skips_a_malformed_pull(store, caplog):
    pulls = [_pull(1, H1), {"number": 2, "head": None}, _pull(3, H2)]
    with caplog.at_level(logging.ERROR, logger="prxref.gate.watchdog"):
        made = watchdog.reconcile(_Client({"acme/a": pulls}), store, ["acme/a"])
    assert [g.pr_number for g in made] == [1, 3]
    assert "TypeError" in caplog.text


def test_reconcile_keeps_pages_fetched_before_an_error(store):
    class _C:
        def request(self, method, repo, path, **kwargs):
            if kwargs["params"]["page"] == 1:
                return [_pull(n, f"{n:040x}") for n in range(1, 101)]
            raise GitHubError("boom", status=500)

    assert len(watchdog.reconcile(_C(), store, ["acme/a"])) == 100


def test_tick_does_not_mark_reconciled_when_reconcile_raises(store, monkeypatch):
    dog = watchdog.Watchdog(store, _Client({}), [], watchdog_s=60, reconcile_s=600)
    monkeypatch.setattr(watchdog, "reconcile", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        dog.tick(1000.0)
    monkeypatch.setattr(watchdog, "reconcile", lambda *a: [])
    assert dog.tick(1001.0)["reconciled"] == []


def test_run_ticks_repeatedly_survives_errors_and_stops(store, monkeypatch, caplog):
    stop = threading.Event()
    seen = []

    def fake_tick(now):
        seen.append(now)
        if len(seen) == 1:
            raise RuntimeError("tick boom")
        if len(seen) >= 3:
            stop.set()
        return {}

    dog = watchdog.Watchdog(store, _Client({}), [], watchdog_s=0.001, reconcile_s=600)
    monkeypatch.setattr(dog, "tick", fake_tick)
    ticks = iter(range(100))
    with caplog.at_level(logging.ERROR, logger="prxref.gate.watchdog"):
        dog.run(stop, clock=lambda: float(next(ticks)))
    assert seen == [0.0, 1.0, 2.0]
    assert "watchdog tick failed" in caplog.text


def _finished(store, state, *, check_run_id):
    gen = store.enqueue("acme/a", 1, H1, BASE, "main", "opened")
    token = store.acquire_lease("w", 60)
    store.start(gen.id, token)
    if check_run_id is not None:
        store.set_check_run(gen.id, token, check_run_id)
    store.finish(gen.id, token, state)
    assert store.get(gen.id).state == state
    return gen


@pytest.mark.parametrize("state", ["failed", "completed"])
def test_reconcile_does_not_reenqueue_a_finished_head_with_a_check_run(store, state):
    _finished(store, state, check_run_id=99)
    client = _Client({"acme/a": [_pull(1, H1)]})
    assert watchdog.reconcile(client, store, ["acme/a"]) == []


def test_reconcile_requeues_a_failed_head_whose_check_never_published(store):
    gen = _finished(store, "failed", check_run_id=None)
    assert store.get(gen.id).check_run_id is None
    made = watchdog.reconcile(_Client({"acme/a": [_pull(1, H1)]}), store, ["acme/a"])
    assert [(g.pr_number, g.head_sha, g.reason, g.state) for g in made] == [(1, H1, "reconcile", "queued")]


def test_reconcile_requeues_a_cancelled_head(store):
    gen = store.enqueue("acme/a", 1, H1, BASE, "main", "opened")
    assert store.cancel_pr("acme/a", 1) == 1
    assert store.get(gen.id).state == "cancelled"
    made = watchdog.reconcile(_Client({"acme/a": [_pull(1, H1)]}), store, ["acme/a"])
    assert [(g.pr_number, g.head_sha, g.state) for g in made] == [(1, H1, "queued")]


def test_tick_reconciles_at_start_then_on_the_interval(store):
    client = _Client({"acme/a": []})
    dog = watchdog.Watchdog(store, client, ["acme/a"], watchdog_s=60, reconcile_s=600)
    assert dog.tick(1000.0)["reconciled"] == []
    assert dog.tick(1060.0)["reconciled"] is None
    assert dog.tick(1600.0)["reconciled"] == []
    assert len(client.calls) == 2


def test_tick_requeues_expired_in_progress(store, monkeypatch):
    calls = []
    monkeypatch.setattr(store, "requeue_expired", lambda: calls.append(1) or [42])
    dog = watchdog.Watchdog(store, _Client({}), [], watchdog_s=60, reconcile_s=600)
    assert dog.tick(0.0)["requeued"] == [42]
    assert calls == [1]


def test_run_returns_when_stopped(store):
    stop = threading.Event()
    stop.set()
    watchdog.Watchdog(store, _Client({}), [], watchdog_s=60, reconcile_s=600).run(stop)
