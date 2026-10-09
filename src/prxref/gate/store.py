"""The gate's durable state: one SQLite file in WAL mode with ``synchronous=FULL`` (#102).

Every state transition is a method here. The worker lease is the ``worker``
row of ``leases``; its ``token`` is a fencing token, and every transition a
worker makes passes the token it holds. A transition from a holder whose
token is no longer current, or whose lease has expired, raises
:class:`LeaseLost` before anything is written, so a second ``prxref gate
serve`` on the same file can never publish.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

STATES = ("queued", "in_progress", "completed", "failed", "superseded", "stale", "cancelled")
FINAL_STATES = frozenset({"completed", "failed", "stale", "cancelled"})
AUTH_MARKERS = ("AuthenticationError", "UnrecognizedClientException", "ExpiredToken", "security token")
_COLUMNS = (
    "id, repo, pr_number, head_sha, base_sha, base_ref, gen, reason, state, created_at, "
    "started_at, finished_at, check_run_id, external_id, conclusion, error, attempts"
)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries(
  delivery_id TEXT PRIMARY KEY, received_at REAL NOT NULL, event TEXT NOT NULL,
  action TEXT, repo TEXT, pr_number INTEGER, handled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS generations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL, pr_number INTEGER NOT NULL,
  head_sha TEXT NOT NULL, base_sha TEXT NOT NULL, base_ref TEXT NOT NULL, gen INTEGER NOT NULL,
  reason TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, started_at REAL,
  finished_at REAL, check_run_id INTEGER, external_id TEXT NOT NULL, conclusion TEXT,
  error TEXT, attempts INTEGER NOT NULL DEFAULT 0, UNIQUE(repo, pr_number, gen));
CREATE TABLE IF NOT EXISTS leases(
  name TEXT PRIMARY KEY, holder TEXT, token INTEGER NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS publications(
  generation_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, github_id INTEGER NOT NULL,
  posted_at REAL NOT NULL);
"""


class LeaseLost(RuntimeError):
    """The caller's fencing token is not the current, live worker lease."""


class StateError(RuntimeError):
    """A transition from a state that does not allow it."""


@dataclass(frozen=True)
class Generation:
    """One review attempt for one PR at one (head, base) pair."""

    id: int
    repo: str
    pr_number: int
    head_sha: str
    base_sha: str
    base_ref: str
    gen: int
    reason: str
    state: str
    created_at: float
    started_at: float | None
    finished_at: float | None
    check_run_id: int | None
    external_id: str
    conclusion: str | None
    error: str | None
    attempts: int


def holder_alive(holder: str) -> bool:
    """False only for a ``host:pid`` holder on this host whose process is gone."""
    host, _, pid = holder.rpartition(":")
    if host != socket.gethostname() or not pid.isdigit():
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Store:
    """All reads and transitions over the gate's SQLite file."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _fence(self, c: sqlite3.Connection, token: int) -> None:
        row = c.execute("SELECT token, expires_at FROM leases WHERE name='worker'").fetchone()
        if row is None or row[0] != token or row[1] <= self._clock():
            raise LeaseLost(f"fencing token {token} is not the live worker lease")

    def _one(self, sql: str, args: tuple) -> Generation | None:
        with self._lock:
            row = self._conn.execute(f"SELECT {_COLUMNS} FROM generations {sql}", args).fetchone()
        return Generation(*row) if row else None

    def record_delivery(self, delivery_id, event, action, repo, pr_number) -> bool:
        """Insert one delivery row; False when the id was already recorded."""
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO deliveries(delivery_id, received_at, event, action, repo, pr_number) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (delivery_id, self._clock(), event, action, repo, pr_number),
            )
            return cur.rowcount == 1

    def mark_delivery_handled(self, delivery_id: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE deliveries SET handled=1 WHERE delivery_id=?", (delivery_id,))

    def enqueue(self, repo, pr_number, head_sha, base_sha, base_ref, reason) -> Generation:
        """Insert the PR's next generation; supersede its older queued ones in the same transaction."""
        now = self._clock()
        with self._tx() as c:
            gen = c.execute(
                "SELECT COALESCE(MAX(gen), 0) + 1 FROM generations WHERE repo=? AND pr_number=?",
                (repo, pr_number),
            ).fetchone()[0]
            c.execute(
                "UPDATE generations SET state='superseded', finished_at=? "
                "WHERE repo=? AND pr_number=? AND state='queued'",
                (now, repo, pr_number),
            )
            cur = c.execute(
                "INSERT INTO generations(repo, pr_number, head_sha, base_sha, base_ref, gen, reason, "
                "state, created_at, external_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, '')",
                (repo, pr_number, head_sha, base_sha, base_ref, gen, reason, now),
            )
            c.execute("UPDATE generations SET external_id=CAST(id AS TEXT) WHERE id=?", (cur.lastrowid,))
            new_id = cur.lastrowid
        return self.get(new_id)

    def cancel_pr(self, repo: str, pr_number: int) -> int:
        """Cancel queued generations of a closed PR that have no check run yet."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE generations SET state='cancelled', finished_at=? WHERE repo=? AND pr_number=? "
                "AND state='queued' AND check_run_id IS NULL",
                (self._clock(), repo, pr_number),
            )
            return cur.rowcount

    def next_queued(self) -> Generation | None:
        return self._one(
            "AS g WHERE state='queued' AND gen=(SELECT MAX(gen) FROM generations "
            "WHERE repo=g.repo AND pr_number=g.pr_number) ORDER BY id LIMIT 1",
            (),
        )

    def get(self, gen_id: int) -> Generation:
        found = self._one("WHERE id=?", (gen_id,))
        if found is None:
            raise StateError(f"no generation {gen_id}")
        return found

    def latest_generation(self, repo: str, pr_number: int) -> Generation | None:
        return self._one("WHERE repo=? AND pr_number=? ORDER BY gen DESC LIMIT 1", (repo, pr_number))

    def latest_generation_for_head(self, repo: str, head_sha: str) -> Generation | None:
        return self._one("WHERE repo=? AND head_sha=? ORDER BY id DESC LIMIT 1", (repo, head_sha))

    def has_live_generation(self, repo, pr_number, head_sha, base_sha) -> bool:
        return (
            self._one(
                "WHERE repo=? AND pr_number=? AND head_sha=? AND base_sha=? "
                "AND state IN ('queued','in_progress','completed','failed') LIMIT 1",
                (repo, pr_number, head_sha, base_sha),
            )
            is not None
        )

    def acquire_lease(self, holder, ttl_s, *, holder_alive=lambda h: True) -> int | None:
        """Take the worker lease and return its new fencing token, or None while another holds it."""
        now = self._clock()
        with self._tx() as c:
            row = c.execute("SELECT holder, token, expires_at FROM leases WHERE name='worker'").fetchone()
            if row is not None and row[0] != holder and row[2] > now and holder_alive(row[0]):
                return None
            token = (row[1] if row else 0) + 1
            c.execute(
                "INSERT INTO leases(name, holder, token, expires_at) VALUES ('worker', ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET holder=excluded.holder, token=excluded.token, "
                "expires_at=excluded.expires_at",
                (holder, token, now + ttl_s),
            )
            return token

    def renew_lease(self, holder: str, token: int, ttl_s: float) -> bool:
        now = self._clock()
        with self._tx() as c:
            cur = c.execute(
                "UPDATE leases SET expires_at=? WHERE name='worker' AND holder=? AND token=? AND expires_at>?",
                (now + ttl_s, holder, token, now),
            )
            return cur.rowcount == 1

    def release_lease(self, holder: str, token: int) -> None:
        """Expire the worker lease if ``holder`` still holds it under ``token`` (a clean stop).

        The row is expired, not deleted, so the next holder's fencing token
        still counts up from this one.
        """
        with self._tx() as c:
            c.execute(
                "UPDATE leases SET expires_at=0 WHERE name='worker' AND holder=? AND token=?",
                (holder, token),
            )

    def lease_info(self) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT holder, token, expires_at FROM leases WHERE name='worker'").fetchone()
        if row is None:
            return None
        return {"holder": row[0], "token": row[1], "expires_at": row[2], "live": row[2] > self._clock()}

    def start(self, gen_id: int, token: int) -> Generation:
        with self._tx() as c:
            self._fence(c, token)
            cur = c.execute(
                "UPDATE generations SET state='in_progress', started_at=?, attempts=attempts+1 "
                "WHERE id=? AND state='queued'",
                (self._clock(), gen_id),
            )
            if cur.rowcount != 1:
                raise StateError(f"generation {gen_id} is not queued")
        return self.get(gen_id)

    def set_check_run(self, gen_id: int, token: int, check_run_id: int) -> None:
        with self._tx() as c:
            self._fence(c, token)
            c.execute("UPDATE generations SET check_run_id=? WHERE id=?", (check_run_id, gen_id))

    def finish(self, gen_id, token, state, conclusion=None, error=None) -> None:
        if state not in FINAL_STATES:
            raise StateError(f"{state!r} is not a final state")
        with self._tx() as c:
            self._fence(c, token)
            cur = c.execute(
                "UPDATE generations SET state=?, conclusion=?, error=?, finished_at=? "
                "WHERE id=? AND state IN ('queued','in_progress')",
                (state, conclusion, error, self._clock(), gen_id),
            )
            if cur.rowcount != 1:
                raise StateError(f"generation {gen_id} is already final")

    def _requeue_in_progress(self, c: sqlite3.Connection) -> list[int]:
        """Recover every in-progress generation after its worker died; the caller holds the transaction.

        A row that is its PR's newest generation goes back to ``queued`` and its id is returned.
        A row a newer generation has overtaken would never be selected again, so it is finished
        instead: ``stale`` when it already owns a check run, else ``superseded``. Those ids are
        not returned.
        """
        c.execute(
            "UPDATE generations SET finished_at=?, "
            "state=CASE WHEN check_run_id IS NOT NULL THEN 'stale' ELSE 'superseded' END "
            "WHERE state='in_progress' AND gen < (SELECT MAX(g.gen) FROM generations AS g "
            "WHERE g.repo=generations.repo AND g.pr_number=generations.pr_number)",
            (self._clock(),),
        )
        ids = [r[0] for r in c.execute("SELECT id FROM generations WHERE state='in_progress' ORDER BY id")]
        c.execute("UPDATE generations SET state='queued' WHERE state='in_progress'")
        return ids

    def requeue_expired(self) -> list[int]:
        """Return in-progress generations to the queue when no live worker lease exists."""
        with self._tx() as c:
            row = c.execute("SELECT expires_at FROM leases WHERE name='worker'").fetchone()
            if row is not None and row[0] > self._clock():
                return []
            return self._requeue_in_progress(c)

    def adopt_orphans(self, token: int) -> list[int]:
        """Re-queue every in-progress generation; called once by a fresh lease holder."""
        with self._tx() as c:
            self._fence(c, token)
            return self._requeue_in_progress(c)

    def record_publication(self, gen_id: int, token: int, kind: str, github_id: int) -> None:
        with self._tx() as c:
            self._fence(c, token)
            c.execute(
                "INSERT OR IGNORE INTO publications(generation_id, kind, github_id, posted_at) VALUES (?, ?, ?, ?)",
                (gen_id, kind, github_id, self._clock()),
            )

    def publication(self, gen_id: int) -> tuple[str, int] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT kind, github_id FROM publications WHERE generation_id=?",
                (gen_id,),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def set_paused(self, paused: bool) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO leases(name, holder, token, expires_at) VALUES ('paused', 'operator', ?, 0) "
                "ON CONFLICT(name) DO UPDATE SET token=excluded.token",
                (1 if paused else 0,),
            )

    def is_paused(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT token FROM leases WHERE name='paused'").fetchone()
        return bool(row and row[0])

    def queue_depth(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM generations WHERE state='queued'").fetchone()[0]

    def recent(self, limit: int = 10) -> list[Generation]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM generations ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Generation(*r) for r in rows]

    def last_publish_time(self) -> float | None:
        with self._lock:
            return self._conn.execute(
                "SELECT MAX(finished_at) FROM generations WHERE conclusion IS NOT NULL",
            ).fetchone()[0]

    def last_auth_failure(self) -> str | None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT error FROM generations WHERE error IS NOT NULL ORDER BY id DESC LIMIT 50",
            ).fetchall()
        for (error,) in rows:
            if any(marker in error for marker in AUTH_MARKERS):
                return error
        return None
