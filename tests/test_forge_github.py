"""Tests for the GitHub / GitHub Enterprise Server forge adapter.

The adapter had no test module before this one. Both comment reads went out
unparameterised — one default page of 30 — so a summary or a thread past that
window did not exist as far as the adapter was concerned.

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

from prxref.forges import github
from prxref.forges.base import FeedReadError, InlineComment, PRRef
from prxref.forges.github import ForgeImpl, _create_default_session

MARKER = "<!-- prxref-summary -->"
# The page size the adapter is expected to ask for; pinned by its own test
# below so every other test here reads as data rather than as a coupling.
PAGE_SIZE = 100


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


def _ref(url="https://github.com/acme/api/pull/42"):
    ref = ForgeImpl.parse_pr_url(url)
    assert ref is not None
    return ref


def _full_page(prefix="chatter"):
    """A page of exactly _PAGE_SIZE comments — the shape that means 'keep going'."""
    return _mock_response(
        json_data=[{"id": i, "body": f"{prefix} {i}"} for i in range(PAGE_SIZE)]
    )


# --- URL parsing ------------------------------------------------------------


def test_parse_pr_url_cloud_and_enterprise():
    cloud = ForgeImpl.parse_pr_url("https://github.com/acme/api/pull/42")
    assert cloud is not None
    assert (cloud.host, cloud.owner, cloud.repo, cloud.number) == (
        "github.com", "acme", "api", 42,
    )

    ghes = ForgeImpl.parse_pr_url("https://git.corp.example/acme/api/pull/7")
    assert ghes is not None
    assert ghes.host == "git.corp.example"


def test_parse_pr_url_rejects_other_forges():
    assert ForgeImpl.parse_pr_url("https://gitlab.com/g/r/-/merge_requests/1") is None
    assert ForgeImpl.parse_pr_url("https://bitbucket.org/o/r/pull-requests/1") is None


# --- summary dedup ----------------------------------------------------------


def test_post_summary_creates_when_no_previous_summary_exists():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(json_data=[])
    session.post.return_value = _mock_response(201, json_data={"id": 5})

    ForgeImpl(session=session).post_summary(_ref(), "review summary")

    session.patch.assert_not_called()
    session.post.assert_called_once()
    assert session.post.call_args[1]["json"] == {"body": f"{MARKER}\nreview summary"}


def test_post_summary_asks_for_a_full_page_rather_than_the_default_thirty():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(json_data=[])
    session.post.return_value = _mock_response(201, json_data={"id": 5})

    ForgeImpl(session=session).post_summary(_ref(), "body")

    params = session.get.call_args[1]["params"]
    assert params["per_page"] == PAGE_SIZE == github._PAGE_SIZE
    assert params["page"] == 1


def test_post_summary_updates_the_existing_summary_instead_of_duplicating():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(
        json_data=[
            {"id": 1, "body": "a human comment"},
            {"id": 77, "body": f"{MARKER}\nold summary"},
        ]
    )
    session.patch.return_value = _mock_response(200, json_data={"id": 77})

    ForgeImpl(session=session).post_summary(_ref(), "new summary")

    session.post.assert_not_called()
    session.patch.assert_called_once()
    assert session.patch.call_args[0][0].endswith("/issues/comments/77")
    assert session.patch.call_args[1]["json"] == {"body": f"{MARKER}\nnew summary"}


def test_post_summary_finds_a_summary_past_the_first_page():
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = [
        _full_page(),
        _mock_response(json_data=[{"id": 77, "body": f"{MARKER}\nold summary"}]),
    ]
    session.patch.return_value = _mock_response(200, json_data={"id": 77})

    ForgeImpl(session=session).post_summary(_ref(), "new summary")

    session.post.assert_not_called()
    session.patch.assert_called_once()
    assert session.patch.call_args[0][0].endswith("/issues/comments/77")
    assert session.get.call_args_list[1][1]["params"]["page"] == 2


def test_post_summary_stops_reading_as_soon_as_it_finds_the_marker():
    session = MagicMock(spec=requests.Session)
    hit = [{"id": i, "body": "chatter"} for i in range(PAGE_SIZE - 1)]
    hit.append({"id": 77, "body": MARKER})
    session.get.return_value = _mock_response(json_data=hit)
    session.patch.return_value = _mock_response(200, json_data={"id": 77})

    ForgeImpl(session=session).post_summary(_ref(), "new summary")

    # A full page normally means "fetch the next one"; finding the marker wins.
    assert session.get.call_count == 1


# --- summary: an unreadable feed must not become a duplicate ----------------


def test_post_summary_refuses_to_post_when_the_feed_returns_an_error_status():
    # This is the fall-through the old code had: `if list_resp.ok:` was false,
    # so the lookup was skipped and the POST ran as if no summary existed.
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(403, json_data={"message": "rate limited"})

    with pytest.raises(FeedReadError, match="403"):
        ForgeImpl(session=session).post_summary(_ref(), "body")

    session.post.assert_not_called()
    session.patch.assert_not_called()


def test_post_summary_refuses_to_post_when_the_feed_read_raises():
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = requests.ConnectionError("down")

    with pytest.raises(FeedReadError, match="comment feed"):
        ForgeImpl(session=session).post_summary(_ref(), "body")

    session.post.assert_not_called()


def test_post_summary_refuses_to_post_when_the_feed_outruns_the_page_budget():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _full_page()

    with pytest.raises(FeedReadError, match="page budget"):
        ForgeImpl(session=session).post_summary(_ref(), "body")

    assert session.get.call_count == github._MAX_PAGES
    session.post.assert_not_called()


def test_post_summary_refuses_to_post_when_the_feed_is_not_a_list():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(json_data={"message": "Not Found"})

    with pytest.raises(FeedReadError):
        ForgeImpl(session=session).post_summary(_ref(), "body")

    session.post.assert_not_called()


# --- threads ----------------------------------------------------------------


def test_list_threads_reads_review_comments():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(
        json_data=[
            {
                "id": 1,
                "path": "src/app.py",
                "line": 12,
                "user": {"login": "reviewer"},
                "body": "please fix",
            }
        ]
    )

    threads = ForgeImpl(session=session).list_threads(_ref())

    assert len(threads) == 1
    assert threads[0].path == "src/app.py"
    assert threads[0].line == 12
    assert threads[0].author == "reviewer"
    assert threads[0].body_snippet == "please fix"


def test_list_threads_pages_past_the_first_page():
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = [
        _full_page("early"),
        _mock_response(json_data=[{"id": 999, "body": "late", "path": "b.py", "line": 3}]),
    ]

    threads = ForgeImpl(session=session).list_threads(_ref())

    assert len(threads) == PAGE_SIZE + 1
    assert threads[-1].body_snippet == "late"


def test_list_threads_keeps_what_it_read_and_warns_when_the_feed_read_fails(caplog):
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = [
        _full_page("early"),
        requests.ConnectionError("down"),
    ]

    with caplog.at_level(logging.WARNING, logger="prxref.forges.github"):
        threads = ForgeImpl(session=session).list_threads(_ref())

    assert len(threads) == PAGE_SIZE
    assert "incomplete" in caplog.text.lower()


# --- inline comments (unchanged behavior, previously untested) --------------


def _session_for_inline(post_responses, head_sha="deadbeef"):
    """A session whose get() answers get_pr and whose post() replays statuses.

    post_inline_comments reads the head SHA through get_pr, so an inline-comment
    test has to satisfy that GET as well as the comment POSTs.
    """
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(
        200,
        json_data={
            "title": "t", "body": "", "user": {"login": "u"},
            "head": {"ref": "feature", "sha": head_sha},
            "base": {"ref": "main", "sha": "cafe"},
        },
    )
    session.post.side_effect = post_responses
    return session


def test_post_inline_comments_skips_422_and_keeps_going():
    session = _session_for_inline([
        _mock_response(422),
        _mock_response(201, json_data={"id": 2}),
    ])
    posted = ForgeImpl(session=session).post_inline_comments(
        _ref(),
        [
            InlineComment(path="a.py", line=1, body="x"),
            InlineComment(path="b.py", line=2, body="y"),
        ],
    )
    assert posted == 1


def test_post_inline_comments_sends_commit_id():
    """GitHub rejects the whole payload without commit_id, reporting `line`
    itself as an unpermitted key, so every comment 422s rather than only the
    out-of-diff ones. Assert on the payload: a call-count assertion passes
    against a body GitHub refuses."""
    session = _session_for_inline([_mock_response(201, json_data={"id": 1})], head_sha="abc123")
    posted = ForgeImpl(session=session).post_inline_comments(
        _ref(), [InlineComment(path="a.py", line=7, body="x")]
    )
    assert posted == 1
    payload = session.post.call_args.kwargs["json"]
    assert payload["commit_id"] == "abc123"
    assert payload["path"] == "a.py"
    assert payload["line"] == 7
    assert payload["side"] == "RIGHT"


def test_post_inline_comments_no_comments_makes_no_requests():
    """An empty list must not cost a get_pr round trip."""
    session = MagicMock(spec=requests.Session)
    assert ForgeImpl(session=session).post_inline_comments(_ref(), []) == 0
    session.get.assert_not_called()
    session.post.assert_not_called()


def test_post_inline_comments_logs_the_422_body(caplog):
    """A swallowed 422 used to leave nothing behind, so a payload-level
    rejection and a legitimately skipped line both read as \"0 posted\"."""
    session = _session_for_inline([_mock_response(422, text='{"message":"No subschema matched"}')])
    with caplog.at_level(logging.WARNING, logger="prxref.forges.github"):
        posted = ForgeImpl(session=session).post_inline_comments(
            _ref(), [InlineComment(path="a.py", line=1, body="x")]
        )
    assert posted == 0
    assert "No subschema matched" in caplog.text
    assert "a.py" in caplog.text


def test_ref_round_trips_through_the_protocol_dataclass():
    ref = _ref()
    assert isinstance(ref, PRRef)
    assert ForgeImpl.parse_pr_url(ref.url) == ref


# --- pruning stale inline comments -------------------------------------------


ATTRIBUTION_BODY = (
    "🤖 🟥 **[ERROR] x** (`a.py:1`)\n\nbody\n\n---\n"
    "*Reviewed by prxref · model=m*"
)


def test_prune_deletes_only_attributed_comments():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(
        200,
        json_data=[
            {"id": 11, "body": ATTRIBUTION_BODY},
            {"id": 12, "body": "a human's comment, never a candidate"},
            {"id": 13, "body": "*Reviewed by prxref · model=other*"},
        ],
    )
    session.delete.return_value = _mock_response(204)

    removed = ForgeImpl(session=session).prune_inline_comments(_ref())

    assert removed == 2
    deleted_urls = [c.args[0] for c in session.delete.call_args_list]
    # The delete route carries NO pull number: /pulls/comments/{id}. The
    # listing URL does, and reusing it 404s every delete.
    for url in deleted_urls:
        assert "/repos/acme/api/pulls/comments/" in url
        assert "/pulls/42/" not in url
    deleted_ids = {url.rsplit("/", 1)[-1] for url in deleted_urls}
    assert deleted_ids == {"11", "13"}


def test_prune_counts_past_a_delete_the_token_cannot_perform():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(
        200,
        json_data=[
            {"id": 21, "body": ATTRIBUTION_BODY},
            {"id": 22, "body": ATTRIBUTION_BODY},
        ],
    )
    session.delete.side_effect = [_mock_response(204), _mock_response(403)]

    removed = ForgeImpl(session=session).prune_inline_comments(_ref())

    assert removed == 1
    assert session.delete.call_count == 2


def test_prune_survives_an_unreadable_feed():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(500)

    removed = ForgeImpl(session=session).prune_inline_comments(_ref())

    assert removed == 0
    session.delete.assert_not_called()


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
