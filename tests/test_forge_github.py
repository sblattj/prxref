"""Tests for the GitHub / GitHub Enterprise Server forge adapter.

The adapter had no test module before this one. Both comment reads went out
unparameterised — one default page of 30 — so a summary or a thread past that
window did not exist as far as the adapter was concerned.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest
import requests

from prxref.forges import github
from prxref.forges.base import FeedReadError, InlineComment, PRRef
from prxref.forges.github import ForgeImpl

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


def test_post_inline_comments_skips_422_and_keeps_going():
    session = MagicMock(spec=requests.Session)
    session.post.side_effect = [
        _mock_response(422),
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
