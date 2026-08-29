"""Tests for the Bitbucket Cloud forge adapter.

The adapter had no test module at all before this one, which is how
``post_summary`` shipped without ever looking for its own previous summary:
nothing asserted that a second review updates the first one rather than
posting beside it.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest
import requests

from prxref.forges import bitbucket
from prxref.forges.base import FeedReadError, InlineComment, PRRef
from prxref.forges.bitbucket import ForgeImpl

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
