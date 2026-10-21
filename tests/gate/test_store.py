"""Gate durable state: generations, supersede, lease fencing, restart recovery (#102).

The contract runs against both backends: the SQLite :class:`Store` and the
DynamoDB :class:`DynamoStore` (moto).
"""

import os
import socket
import sqlite3

import boto3
import pytest
from moto import mock_aws

from prxref.gate import store as store_mod
from prxref.gate.dynamo_store import DynamoStore, create_table
from prxref.gate.store import LeaseLost, Store


@pytest.fixture
def clock():
    now = [1000.0]
    return now


@pytest.fixture(params=["sqlite", "dynamo"])
def make_store(request, tmp_path, clock, monkeypatch):
    """A factory of stores on one backing state: one SQLite file, or one DynamoDB table."""
    opened = []

    def sqlite_store():
        opened.append(Store(tmp_path / "gate.sqlite3", clock=lambda: clock[0]))
        return opened[-1]

    if request.param == "sqlite":
        yield sqlite_store
    else:
        for name, value in (
            ("AWS_ACCESS_KEY_ID", "testing"),
            ("AWS_SECRET_ACCESS_KEY", "testing"),
            ("AWS_SESSION_TOKEN", "testing"),
            ("AWS_DEFAULT_REGION", "us-east-1"),
        ):
            monkeypatch.setenv(name, value)
        with mock_aws():
            client = boto3.client("dynamodb", region_name="us-east-1")
            create_table(client, "gate")
            yield lambda: DynamoStore("gate", client, clock=lambda: clock[0])
    for store in opened:
        store.close()


@pytest.fixture
def db(make_store):
    return make_store()


def _enqueue(db, head="h1", pr=7, base="b1", reason="opened"):
    return db.enqueue("acme/widget", pr, head, base, "main", reason)


# SQLite-only: WAL and synchronous=FULL are properties of the SQLite file.
def test_wal_and_synchronous_full(tmp_path, clock):
    db = Store(tmp_path / "gate.sqlite3", clock=lambda: clock[0])
    conn = sqlite3.connect(tmp_path / "gate.sqlite3")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()
    assert db._conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    db.close()


def test_duplicate_delivery_is_refused(db):
    assert db.record_delivery("d1", "pull_request", "opened", "acme/widget", 7) is True
    assert db.record_delivery("d1", "pull_request", "opened", "acme/widget", 7) is False


def test_gen_is_monotonic_and_a_new_push_supersedes_queued(db):
    g1 = _enqueue(db, "h1")
    g2 = _enqueue(db, "h2", reason="synchronize")
    assert (g1.gen, g2.gen) == (1, 2)
    assert g2.external_id == str(g2.id)
    assert db.get(g1.id).state == "superseded"
    assert db.next_queued().id == g2.id


def test_next_queued_is_oldest_across_prs(db):
    a = _enqueue(db, "h1", pr=7)
    _enqueue(db, "h9", pr=8)
    assert db.next_queued().id == a.id


def test_lease_fencing_refuses_a_stale_token(db, clock):
    gen = _enqueue(db)
    token = db.acquire_lease("hostA:1", 60)
    db.start(gen.id, token)
    clock[0] += 61
    other = db.acquire_lease("hostB:2", 60)
    assert other == token + 1
    with pytest.raises(LeaseLost):
        db.finish(gen.id, token, "completed", "success")
    with pytest.raises(LeaseLost):
        db.record_publication(gen.id, token, "review", 55)


def test_second_holder_cannot_take_a_live_lease(db):
    assert db.acquire_lease("hostA:1", 60) == 1
    assert db.acquire_lease("hostB:2", 60) is None


def test_a_dead_local_holder_is_broken_immediately(db):
    db.acquire_lease("hostA:1", 600)
    assert db.acquire_lease("hostB:2", 60, holder_alive=lambda h: False) == 2


# SQLite-only by nature: pid liveness is for a local ``host:pid`` holder; a Lambda holder just expires.
def test_holder_alive_checks_pids_on_this_host():
    host = socket.gethostname()
    assert store_mod.holder_alive(f"{host}:{os.getpid()}") is True
    assert store_mod.holder_alive(f"{host}:999999999") is False
    assert store_mod.holder_alive("some-other-host:1") is True


def test_restart_requeues_in_progress_only_when_the_lease_is_dead(db, clock):
    gen = _enqueue(db)
    token = db.acquire_lease("hostA:1", 60)
    db.start(gen.id, token)
    db.set_check_run(gen.id, token, 4242)
    assert db.requeue_expired() == []
    clock[0] += 61
    assert db.requeue_expired() == [gen.id]
    again = db.get(gen.id)
    assert (again.state, again.check_run_id, again.attempts) == ("queued", 4242, 1)


def test_a_fresh_lease_adopts_in_progress_orphans(db):
    gen = _enqueue(db)
    old = db.acquire_lease("hostA:1", 600)
    db.start(gen.id, old)
    new = db.acquire_lease("hostB:2", 600, holder_alive=lambda h: False)
    with pytest.raises(LeaseLost):
        db.adopt_orphans(old)
    assert db.adopt_orphans(new) == [gen.id]
    assert db.get(gen.id).state == "queued"


def test_finish_refuses_a_non_final_state(db):
    gen = _enqueue(db)
    token = db.acquire_lease("hostA:1", 60)
    db.start(gen.id, token)
    with pytest.raises(store_mod.StateError):
        db.finish(gen.id, token, "queued")


def test_cancel_pr_cancels_queued_rows_without_a_check(db, clock):
    g1 = _enqueue(db, "h1")
    assert db.cancel_pr("acme/widget", 7) == 1
    assert db.get(g1.id).state == "cancelled"


def test_has_live_generation_ignores_superseded_and_stale(db):
    _enqueue(db, "h1")
    _enqueue(db, "h2")
    assert db.has_live_generation("acme/widget", 7, "h2", "b1") is True
    assert db.has_live_generation("acme/widget", 7, "h1", "b1") is False


def test_latest_generation_for_head_finds_the_pr(db):
    _enqueue(db, "fork-head", pr=12)
    assert db.latest_generation_for_head("acme/widget", "fork-head").pr_number == 12
    assert db.latest_generation_for_head("acme/widget", "nope") is None


def test_pause_flag_round_trips(db):
    assert db.is_paused() is False
    db.set_paused(True)
    assert db.is_paused() is True
    db.set_paused(False)
    assert db.is_paused() is False


def test_last_auth_failure_reports_the_newest_auth_error(db):
    gen = _enqueue(db)
    token = db.acquire_lease("hostA:1", 60)
    db.start(gen.id, token)
    db.finish(gen.id, token, "failed", "failure", "review error: litellm.AuthenticationError: expired")
    assert "AuthenticationError" in db.last_auth_failure()


def test_state_survives_reopen(make_store):
    first = make_store()
    g = first.enqueue("acme/widget", 7, "h1", "b1", "main", "opened")
    second = make_store()
    try:
        assert second.get(g.id).head_sha == "h1"
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("recover", ["requeue_expired", "adopt_orphans"])
@pytest.mark.parametrize(("check_run", "final"), [(99, "stale"), (None, "superseded")])
def test_overtaken_in_progress_row_is_finished_not_stranded(make_store, clock, recover, check_run, final):
    a = make_store()
    b = make_store()
    try:
        g2 = _enqueue(a, "h2")
        token = a.acquire_lease("hostA:1", 60)
        a.start(g2.id, token)
        if check_run:
            a.set_check_run(g2.id, token, check_run)
        g3 = _enqueue(a, "h3", reason="synchronize")
        assert a.get(g2.id).state == "in_progress"
        clock[0] += 61
        if recover == "requeue_expired":
            requeued = b.requeue_expired()
            new = b.acquire_lease("hostB:2", 60)
        else:
            new = b.acquire_lease("hostB:2", 60)
            requeued = b.adopt_orphans(new)
        assert requeued == []
        done = b.get(g2.id)
        assert (done.state, done.finished_at) == (final, clock[0])
        assert b.has_live_generation("acme/widget", 7, "h2", "b1") is False
        assert b.queue_depth() == 1
        assert b.next_queued().id == g3.id
        b.start(g3.id, new)
        b.finish(g3.id, new, "completed", "success")
        assert b.next_queued() is None
        assert b.queue_depth() == 0
    finally:
        a.close()
        b.close()


def test_two_store_objects_fence_each_other_on_one_file(make_store, clock):
    a = make_store()
    b = make_store()
    try:
        gen = _enqueue(a)
        old = a.acquire_lease("hostA:1", 60)
        a.start(gen.id, old)
        clock[0] += 61
        new = b.acquire_lease("hostB:2", 60)
        assert new == old + 1
        with pytest.raises(LeaseLost):
            a.finish(gen.id, old, "completed", "success")
        assert b.adopt_orphans(new) == [gen.id]
        assert a.get(gen.id).state == "queued"
    finally:
        a.close()
        b.close()


def test_start_and_set_check_run_refuse_a_stale_token(db, clock):
    gen = _enqueue(db)
    old = db.acquire_lease("hostA:1", 60)
    clock[0] += 61
    assert db.acquire_lease("hostB:2", 60) == old + 1
    with pytest.raises(LeaseLost):
        db.start(gen.id, old)
    with pytest.raises(LeaseLost):
        db.set_check_run(gen.id, old, 5)
    row = db.get(gen.id)
    assert (row.state, row.check_run_id, row.attempts) == ("queued", None, 0)


def test_release_lease_expires_only_the_matching_holder_and_token(db):
    token = db.acquire_lease("hostA:1", 60)
    db.release_lease("hostA:1", token + 1)
    db.release_lease("hostB:1", token)
    assert db.lease_info()["live"] is True
    db.release_lease("hostA:1", token)
    assert db.lease_info()["live"] is False
    assert db.acquire_lease("hostB:1", 60) == token + 1


def test_queued_lists_every_queued_row_oldest_first(db):
    a = _enqueue(db, "h1", pr=7)
    b = _enqueue(db, "h9", pr=8)
    token = db.acquire_lease("hostA:1", 60)
    db.start(a.id, token)
    assert [g.id for g in db.queued()] == [b.id]
    assert db.queue_depth() == 1


def test_recent_publication_and_last_publish(db, clock):
    gen = _enqueue(db)
    token = db.acquire_lease("hostA:1", 60)
    assert db.last_publish_time() is None
    db.start(gen.id, token)
    db.record_publication(gen.id, token, "APPROVE", 55)
    db.record_publication(gen.id, token, "REQUEST_CHANGES", 56)
    assert db.publication(gen.id) == ("REQUEST_CHANGES", 56)
    clock[0] += 5
    db.finish(gen.id, token, "completed", "success")
    assert db.last_publish_time() == clock[0]
    newer = _enqueue(db, "h2")
    assert [g.id for g in db.recent(1)] == [newer.id]
    assert db.get(gen.id).finished_at == clock[0]
