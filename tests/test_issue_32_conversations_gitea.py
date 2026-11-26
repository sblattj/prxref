"""Issue #32: Gitea/Forgejo ``list_conversations`` and ``post_reply``.

Only the PR's top-level stream is answered: ``GET /repos/{o}/{r}/issues/{n}/comments``
(unpaged) is read and ``POST /repos/{o}/{r}/issues/{n}/comments`` replies.
A review comment carries no field linking a reply to its root, and Forgejo
and Gitea up to 1.26 serve no reply route, so inline threads are left out
and a non-empty ``reply_to`` is refused.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_forge_gitea import API, Resp, _forge, _ref  # noqa: E402

from prxref.forges.base import (  # noqa: E402
    ATTRIBUTION_MARKER,
    Conversation,
    ConversationComment,
    FeedReadError,
)

SUMMARY_BODY = f"<!-- prxref-summary -->\n## Review\n\n*{ATTRIBUTION_MARKER} · model=m*"


def _issue_comment(cid, body, login, created):
    return {
        "id": cid,
        "html_url": f"https://codeberg.example.com/acme/api/pulls/42#issuecomment-{cid}",
        "pull_request_url": "https://codeberg.example.com/acme/api/pulls/42",
        "issue_url": "",
        "user": {"id": 7, "login": login, "full_name": login.title()},
        "original_author": "",
        "body": body,
        "assets": [],
        "created_at": created,
        "updated_at": created,
    }


COMMENTS = [
    _issue_comment(130, "@prxref is this safe?", "dev", "2026-11-01T13:00:00Z"),
    _issue_comment(100, SUMMARY_BODY, "prxref-bot", "2026-11-01T09:00:00Z"),
    _issue_comment(120, "thanks", "lead", "2026-11-01T12:00:00Z"),
]
REVIEW_COMMENT = {
    "id": 501, "body": f"**[WARNING] x**\n\n*{ATTRIBUTION_MARKER} · model=m*", "path": "src/app.py",
    "position": 25, "user": {"login": "prxref-bot"}, "pull_request_review_id": 9,
}


def _routes():
    return {
        ("GET", f"{API}/issues/42/comments"): Resp(200, COMMENTS),
        ("GET", f"{API}/pulls/42/reviews"): Resp(200, [{"id": 9, "comments_count": 1, "body": ""}]),
        ("GET", f"{API}/pulls/42/reviews/9/comments"): Resp(200, [REVIEW_COMMENT]),
        ("POST", f"{API}/issues/42/comments"): Resp(201, {"id": 140}),
    }


def test_top_level_comments_form_the_only_conversation_and_an_empty_reply_to_posts_a_comment():
    forge, session = _forge(_routes())

    conversations = forge.list_conversations(_ref())

    assert conversations == [Conversation(
        kind="pr",
        reply_to="",
        comments=(
            ConversationComment("prxref-bot", SUMMARY_BODY, True, "2026-11-01T09:00:00Z"),
            ConversationComment("lead", "thanks", False, "2026-11-01T12:00:00Z"),
            ConversationComment("dev", "@prxref is this safe?", False, "2026-11-01T13:00:00Z"),
        ),
    )]

    forge.post_reply(_ref(), "", "Yes: the lock is held by the caller.")

    [post] = session.sent("POST")
    assert post["url"] == f"{API}/issues/42/comments"
    assert post["json"] == {"body": "Yes: the lock is held by the caller."}
    assert session.sent("PATCH") == []


def test_prxref_review_comments_are_not_listed_as_inline_conversations():
    forge, session = _forge(_routes())

    conversations = forge.list_conversations(_ref())

    assert [c.kind for c in conversations] == ["pr"]
    assert [c["url"] for c in session.sent("GET")] == [f"{API}/issues/42/comments"]


def test_a_thread_reply_is_refused_without_a_request():
    forge, session = _forge(_routes())

    with pytest.raises(NotImplementedError, match="review"):
        forge.post_reply(_ref(), "501", "reply")

    assert session.calls == []


def test_the_comment_listing_is_read_once():
    forge, session = _forge(_routes())

    forge.list_conversations(_ref())

    assert len(session.sent("GET")) == 1


def test_no_top_level_comments_means_no_conversations():
    routes = _routes()
    routes[("GET", f"{API}/issues/42/comments")] = Resp(200, [])
    forge, _ = _forge(routes)

    assert forge.list_conversations(_ref()) == []


def test_a_listing_failure_raises():
    routes = _routes()
    routes[("GET", f"{API}/issues/42/comments")] = Resp(500, {"message": "boom"})
    forge, _ = _forge(routes)

    with pytest.raises(FeedReadError):
        forge.list_conversations(_ref())


def test_a_rejected_reply_raises():
    routes = _routes()
    routes[("POST", f"{API}/issues/42/comments")] = Resp(403, {"message": "forbidden"})
    forge, _ = _forge(routes)

    with pytest.raises(requests.HTTPError):
        forge.post_reply(_ref(), "", "reply")
