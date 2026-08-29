"""Tests for the GitHub forge adapter.

URL parsing is covered across the cross-forge matrix in ``test_integration.py``;
this module holds the adapter-level invariants that are cheaper to state
directly against the adapter itself.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prxref.forges.github import _create_default_session

# --- retry policy -----------------------------------------------------------


class _RetryProbe:
    """A localhost server that counts arriving requests and replays statuses.

    The retry policy lives in urllib3, underneath the ``requests`` adapter, so
    a ``MagicMock`` session cannot exercise it — a mock never re-sends anything,
    which is precisely the behaviour under test. This runs the real session
    against a real socket and counts what actually arrives.

    ``statuses`` is replayed one per request and its last entry repeats, so
    ``[502]`` fails every attempt and ``[502, 200]`` fails once then recovers.
    """

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.received: list[str] = []
        probe = self

        class _Handler(BaseHTTPRequestHandler):
            # HTTP/1.0 closes after each response, so every retry arrives on a
            # fresh connection and the count cannot be confused by keep-alive.
            protocol_version = "HTTP/1.0"

            def _reply(self):
                probe.received.append(self.command)
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                index = min(len(probe.received), len(probe.statuses)) - 1
                self.send_response(probe.statuses[index])
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = _reply
            do_POST = _reply
            do_PUT = _reply
            do_PATCH = _reply

            def log_message(self, *args):
                """Silence the per-request stderr line."""

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/"

    def __enter__(self):
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        return False


def test_a_lost_write_is_not_re_sent():
    """A comment POST is never replayed, whatever the status.

    502 stands in for every way a committed write can lose its response: the
    comment is created, the 2xx dies in front of it, and a retry would create
    the comment a second time on the PR.
    """
    session = _create_default_session()
    with _RetryProbe([502]) as probe:
        resp = session.post(probe.url, json={"body": "finding"}, timeout=(5.0, 5.0))

    assert probe.received == ["POST"]
    assert resp.status_code == 502


def test_a_lost_summary_update_is_not_re_sent():
    """The summary update is a write too, and PATCH is not exempt.

    PATCH is not idempotent by HTTP semantics at all, and this one
    edits an existing comment — exactly the request that must not be
    replayed blind.
    """
    session = _create_default_session()
    with _RetryProbe([502]) as probe:
        resp = session.patch(probe.url, json={"body": "summary"}, timeout=(5.0, 5.0))

    assert probe.received == ["PATCH"]
    assert resp.status_code == 502



def test_a_lost_read_is_still_re_sent():
    """Reads keep their retries: replaying a GET costs nothing but a request."""
    session = _create_default_session()
    with _RetryProbe([502, 200]) as probe:
        resp = session.get(probe.url, timeout=(5.0, 5.0))

    assert probe.received == ["GET", "GET"]
    assert resp.status_code == 200


def test_only_read_verbs_are_retryable():
    retry = _create_default_session().get_adapter("https://api.github.com").max_retries

    assert retry.allowed_methods == frozenset(["GET", "HEAD", "OPTIONS"])
    assert retry.is_retry("GET", 502) is True
    assert retry.is_retry("POST", 502) is False
    # 429 is the one status a write could safely be replayed after — the server
    # says it did not process the request — but urllib3 tests the method before
    # it consults the status list, so holding a POST back on 502 holds it back
    # on 429 too. That trade is deliberate; see the comment on the policy.
    assert retry.is_retry("POST", 429) is False
