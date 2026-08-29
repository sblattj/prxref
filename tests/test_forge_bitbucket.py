"""Tests for the Bitbucket Cloud forge adapter.

The adapter had no test module at all before this one, which is how
``post_summary`` shipped without ever looking for its own previous summary:
nothing asserted that a second review updates the first one rather than
posting beside it.

URL parsing is also covered across the cross-forge matrix in
``test_integration.py``; what lives here are the adapter-level invariants that
are cheaper to state directly against the adapter itself.
"""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
import requests

from prxref.forges import bitbucket
from prxref.forges.base import FeedReadError, InlineComment, PRRef
from prxref.forges.bitbucket import ForgeImpl, _make_retry_session

MARKER = "<!-- prxref-summary -->"


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    if json_data is not None:
        resp.json.return_value = json_data
        resp.text = json.dumps(json_data)
    else:
        resp.text = text
        resp.json.side_effect = ValueError("No JSON")
    resp.raise_for_status.side_effect = (
        None if resp.ok else requests.HTTPError(response=resp)
    )
    return resp


def _ref(url="https://bitbucket.org/acme/api/pull-requests/42"):
    ref = ForgeImpl.parse_pr_url(url)
    assert ref is not None
    return ref


def _page(values, next_url=None):
    body = {"values": values, "pagelen": 100, "size": len(values)}
    if next_url:
        body["next"] = next_url
    return _mock_response(json_data=body)


# --- URL parsing ------------------------------------------------------------


def test_parse_pr_url_accepts_the_three_path_spellings():
    for path in ("pull-requests", "pullrequests", "pullrequest"):
        ref = ForgeImpl.parse_pr_url(f"https://bitbucket.org/acme/api/{path}/42")
        assert ref is not None
        assert ref.forge == "bitbucket"
        assert ref.owner == "acme"
        assert ref.repo == "api"
        assert ref.number == 42
        # every spelling normalizes to the canonical one
        assert ref.url == "https://bitbucket.org/acme/api/pull-requests/42"


def test_parse_pr_url_rejects_other_forges():
    assert ForgeImpl.parse_pr_url("https://github.com/o/r/pull/1") is None
    assert ForgeImpl.parse_pr_url("https://gitlab.com/g/r/-/merge_requests/1") is None
    assert ForgeImpl.parse_pr_url(
        "https://bitbucket.corp.example/projects/P/repos/r/pull-requests/1"
    ) is None


# --- summary dedup ----------------------------------------------------------


def test_post_summary_creates_when_no_previous_summary_exists():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _page([])
    session.post.return_value = _mock_response(201, json_data={"id": 5})

    ForgeImpl(session=session).post_summary(_ref(), "review summary")

    session.put.assert_not_called()
    session.post.assert_called_once()
    assert session.post.call_args[1]["json"] == {
        "content": {"raw": f"{MARKER}\nreview summary"}
    }


def test_post_summary_stamps_the_marker_so_the_next_run_can_find_it():
    # Nothing upstream of the adapter puts the marker in the body, so a summary
    # posted without one is invisible to every later lookup.
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _page([])
    session.post.return_value = _mock_response(201, json_data={"id": 5})

    ForgeImpl(session=session).post_summary(_ref(), "no marker here")

    posted = session.post.call_args[1]["json"]["content"]["raw"]
    assert MARKER in posted


def test_post_summary_leaves_a_body_that_already_carries_the_marker_alone():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _page([])
    session.post.return_value = _mock_response(201, json_data={"id": 5})

    ForgeImpl(session=session).post_summary(_ref(), f"{MARKER}\nalready stamped")

    posted = session.post.call_args[1]["json"]["content"]["raw"]
    assert posted == f"{MARKER}\nalready stamped"
    assert posted.count(MARKER) == 1


def test_post_summary_updates_the_existing_summary_instead_of_duplicating():
    # The re-review case: a second run must land on the first run's comment.
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _page(
        [
            {"id": 1, "content": {"raw": "a human comment"}},
            {"id": 77, "content": {"raw": f"{MARKER}\nold summary"}},
        ]
    )
    session.put.return_value = _mock_response(200, json_data={"id": 77})

    ForgeImpl(session=session).post_summary(_ref(), "new summary")

    session.post.assert_not_called()
    session.put.assert_called_once()
    assert session.put.call_args[0][0].endswith("/pullrequests/42/comments/77")
    assert session.put.call_args[1]["json"] == {
        "content": {"raw": f"{MARKER}\nnew summary"}
    }


def test_post_summary_finds_a_summary_past_the_first_page():
    session = MagicMock(spec=requests.Session)
    next_url = "https://api.bitbucket.org/2.0/next-page"
    session.get.side_effect = [
        _page([{"id": i, "content": {"raw": f"chatter {i}"}} for i in range(100)], next_url),
        _page([{"id": 77, "content": {"raw": f"{MARKER}\nold summary"}}]),
    ]
    session.put.return_value = _mock_response(200, json_data={"id": 77})

    ForgeImpl(session=session).post_summary(_ref(), "new summary")

    session.post.assert_not_called()
    session.put.assert_called_once()
    assert session.put.call_args[0][0].endswith("/comments/77")
    assert session.get.call_args_list[1][0][0] == next_url


def test_post_summary_stops_reading_as_soon_as_it_finds_the_marker():
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = [
        _page([{"id": 77, "content": {"raw": MARKER}}], "https://api.bitbucket.org/2.0/next"),
        _page([]),
    ]
    session.put.return_value = _mock_response(200, json_data={"id": 77})

    ForgeImpl(session=session).post_summary(_ref(), "new summary")

    assert session.get.call_count == 1


def test_post_summary_ignores_an_inline_comment_carrying_the_marker():
    # A quoted marker inside a line comment is not the summary.
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _page(
        [
            {
                "id": 9,
                "content": {"raw": f"quoted {MARKER}"},
                "inline": {"path": "a.py", "to": 3},
            }
        ]
    )
    session.post.return_value = _mock_response(201, json_data={"id": 10})

    ForgeImpl(session=session).post_summary(_ref(), "body")

    session.put.assert_not_called()
    session.post.assert_called_once()


def test_post_summary_ignores_a_deleted_comment_carrying_the_marker():
    # Bitbucket keeps deleted comments in the feed with an empty body; updating
    # one puts the summary somewhere nobody can read it.
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _page(
        [{"id": 9, "content": {"raw": MARKER}, "deleted": True}]
    )
    session.post.return_value = _mock_response(201, json_data={"id": 10})

    ForgeImpl(session=session).post_summary(_ref(), "body")

    session.put.assert_not_called()
    session.post.assert_called_once()


# --- summary: an unreadable feed must not become a duplicate ----------------


def test_post_summary_refuses_to_post_when_the_feed_read_fails():
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = requests.ConnectionError("down")

    with pytest.raises(FeedReadError, match="comment feed"):
        ForgeImpl(session=session).post_summary(_ref(), "body")

    session.post.assert_not_called()
    session.put.assert_not_called()


def test_post_summary_refuses_to_post_when_the_feed_returns_an_error_status():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(500, json_data={"error": "boom"})

    with pytest.raises(FeedReadError, match="500"):
        ForgeImpl(session=session).post_summary(_ref(), "body")

    session.post.assert_not_called()


def test_post_summary_refuses_to_post_when_the_feed_outruns_the_page_budget():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _page(
        [{"id": 1, "content": {"raw": "chatter"}}],
        "https://api.bitbucket.org/2.0/forever",
    )

    with pytest.raises(FeedReadError, match="page budget"):
        ForgeImpl(session=session).post_summary(_ref(), "body")

    assert session.get.call_count == bitbucket._MAX_PAGES
    session.post.assert_not_called()


# --- threads ----------------------------------------------------------------


def test_list_threads_reads_inline_and_top_level_comments():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _page(
        [
            {
                "id": 1,
                "content": {"raw": "please fix"},
                "inline": {"path": "src/app.py", "to": 12},
                "user": {"nickname": "reviewer"},
            },
            {"id": 2, "content": {"raw": "looks fine"}, "user": {"nickname": "dev"}},
        ]
    )

    threads = ForgeImpl(session=session).list_threads(_ref())

    assert [t.path for t in threads] == ["src/app.py", None]
    assert [t.line for t in threads] == [12, None]
    assert [t.body_snippet for t in threads] == ["please fix", "looks fine"]


def test_list_threads_pages_past_the_old_five_page_ceiling():
    # The cap used to be five pages, so comment 501 was invisible to dedup.
    session = MagicMock(spec=requests.Session)
    pages = [
        _page(
            [{"id": n, "content": {"raw": f"page {n}"}}],
            f"https://api.bitbucket.org/2.0/page/{n + 1}",
        )
        for n in range(8)
    ]
    pages.append(_page([{"id": 99, "content": {"raw": "last"}}]))
    session.get.side_effect = pages

    threads = ForgeImpl(session=session).list_threads(_ref())

    assert len(threads) == 9
    assert threads[-1].body_snippet == "last"


def test_list_threads_keeps_what_it_read_and_warns_when_the_feed_read_fails(caplog):
    # Dedup input is best-effort: a partial read still beats no read, but the
    # shortfall has to be visible rather than silent.
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = [
        _page([{"id": 1, "content": {"raw": "one"}}], "https://api.bitbucket.org/2.0/two"),
        requests.ConnectionError("down"),
    ]

    with caplog.at_level(logging.WARNING, logger="prxref.forges.bitbucket"):
        threads = ForgeImpl(session=session).list_threads(_ref())

    assert [t.body_snippet for t in threads] == ["one"]
    assert "incomplete" in caplog.text.lower()


def test_list_threads_returns_empty_on_an_immediate_transport_error(caplog):
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = requests.ConnectionError("down")

    with caplog.at_level(logging.WARNING, logger="prxref.forges.bitbucket"):
        assert ForgeImpl(session=session).list_threads(_ref()) == []

    assert caplog.records


# --- inline comments (unchanged behavior, previously untested) --------------


def test_post_inline_comments_skips_4xx_and_keeps_going():
    session = MagicMock(spec=requests.Session)
    session.post.side_effect = [
        _mock_response(400),
        _mock_response(201, json_data={"id": 2}),
    ]
    posted = ForgeImpl(session=session).post_inline_comments(
        _ref(),
        [
            InlineComment(path="a.py", line=1, body="x"),
            InlineComment(path="b.py", line=2, body="y"),
        ],
    )
    assert posted == 1


def test_ref_round_trips_through_the_protocol_dataclass():
    ref = _ref()
    assert isinstance(ref, PRRef)
    assert ForgeImpl.parse_pr_url(ref.url) == ref


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
    session = _make_retry_session()
    with _RetryProbe([502]) as probe:
        resp = session.post(probe.url, json={"content": {"raw": "finding"}}, timeout=(5.0, 5.0))

    assert probe.received == ["POST"]
    assert resp.status_code == 502


def test_a_lost_summary_update_is_not_re_sent():
    """The summary update is a write too, and PUT is not exempt.

    Cloud only grew an update path when post_summary learned to find its own
    previous summary; before that it always POSTed, so this verb had nothing
    to test. It is dropped from the retry policy like every other write: one
    policy shared by four adapters beats one exemption.
    """
    session = _make_retry_session()
    with _RetryProbe([502]) as probe:
        resp = session.put(probe.url, json={"content": {"raw": "summary"}}, timeout=(5.0, 5.0))

    assert probe.received == ["PUT"]
    assert resp.status_code == 502



def test_a_lost_read_is_still_re_sent():
    """Reads keep their retries: replaying a GET costs nothing but a request."""
    session = _make_retry_session()
    with _RetryProbe([502, 200]) as probe:
        resp = session.get(probe.url, timeout=(5.0, 5.0))

    assert probe.received == ["GET", "GET"]
    assert resp.status_code == 200


def test_only_read_verbs_are_retryable():
    retry = _make_retry_session().get_adapter("https://api.bitbucket.org").max_retries

    assert retry.allowed_methods == frozenset(["GET", "HEAD", "OPTIONS"])
    assert retry.is_retry("GET", 502) is True
    assert retry.is_retry("POST", 502) is False
    # 429 is the one status a write could safely be replayed after — the server
    # says it did not process the request — but urllib3 tests the method before
    # it consults the status list, so holding a POST back on 502 holds it back
    # on 429 too. That trade is deliberate; see the comment on the policy.
    assert retry.is_retry("POST", 429) is False
