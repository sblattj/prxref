"""Issue #32: Bitbucket Server/Data Center ``list_conversations`` and ``post_reply``.

Comments come from the COMMENTED entries of ``GET .../pull-requests/{id}/activities``
(start/limit paging, newest first), with replies nested under ``comments``.
Both a thread reply and a top-level reply are ``POST .../pull-requests/{id}/comments``;
a thread reply carries ``parent.id``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_forge_bitbucket_server import _activity_page, _mock_response, _ref  # noqa: E402

from prxref.forges.base import (  # noqa: E402
    ATTRIBUTION_MARKER,
    Conversation,
    ConversationComment,
    FeedReadError,
)
from prxref.forges.bitbucket_server import ForgeImpl  # noqa: E402

PR = "https://bitbucket.corp.example/rest/api/1.0/projects/PLAT/repos/api/pull-requests/42"
PRXREF_BODY = (
    "**[WARNING] Null check missing** (`src/app.py:25`)\n\nbody\n\n---\n"
    f"*{ATTRIBUTION_MARKER} · model=m*"
)
SUMMARY_BODY = f"<!-- prxref-summary -->\n## Review\n\n*{ATTRIBUTION_MARKER} · model=m*"
ANCHOR = {"line": 25, "lineType": "ADDED", "fileType": "TO", "path": "src/app.py", "diffType": "EFFECTIVE"}


def _user(name):
    return {"name": name, "slug": name, "displayName": name.title(), "type": "NORMAL"}


def _c(cid, text, who, created, *, anchor=None, replies=(), **extra):
    comment = {
        "id": cid, "version": 0, "text": text, "author": _user(who),
        "createdDate": created, "updatedDate": created,
        "comments": list(replies), "state": "OPEN", "threadResolved": False,
        **extra,
    }
    if anchor is not None:
        comment["anchor"] = anchor
    return comment


def _activity(aid, comment, action="ADDED"):
    return {
        "id": aid, "createdDate": comment["createdDate"], "user": comment["author"],
        "action": "COMMENTED", "commentAction": action, "comment": comment,
    }


NESTED = _c(13, "and the test covers it", "dev", 1762000300000)
REPLY = _c(12, "Why is this a warning?\nThe caller checks.", "dev", 1762000200000, replies=[NESTED])
ROOT = _c(10, PRXREF_BODY, "prxref-bot", 1762000000000, anchor=ANCHOR, replies=[REPLY])
HUMAN_ROOT = _c(
    20, "human root", "reviewer", 1762000050000,
    anchor={**ANCHOR, "path": "src/other.py", "line": 3},
    replies=[_c(21, f"quoted {ATTRIBUTION_MARKER}", "prxref-bot", 1762000060000)],
)
SUMMARY = _c(5, SUMMARY_BODY, "prxref-bot", 1761999000000, replies=[_c(7, "thanks", "lead", 1762000400000)])
QUESTION = _c(30, "@prxref is this safe?", "dev", 1762000500000)
APPROVED = {"id": 99, "createdDate": 1762000600000, "user": _user("lead"), "action": "APPROVED"}


def _feed():
    return [
        APPROVED,
        _activity(108, QUESTION),
        _activity(107, NESTED, "REPLIED"),
        _activity(106, REPLY, "REPLIED"),
        _activity(103, HUMAN_ROOT),
        _activity(102, ROOT),
        _activity(101, SUMMARY),
    ]


def _forge(*responses):
    session = MagicMock()
    session.get.side_effect = list(responses)
    session.post.return_value = _mock_response(201, json_data={"id": 900, "version": 0})
    return ForgeImpl(session=session), session


def test_prxref_rooted_thread_maps_to_one_inline_conversation_and_reply_targets_its_root():
    forge, session = _forge(_activity_page(_feed()))

    conversations = forge.list_conversations(_ref())

    inline = [c for c in conversations if c.kind == "inline"]
    assert inline == [Conversation(
        kind="inline",
        reply_to="10",
        comments=(
            ConversationComment("Prxref-Bot", PRXREF_BODY, True, "1762000000000"),
            ConversationComment("Dev", "Why is this a warning?\nThe caller checks.", False, "1762000200000"),
            ConversationComment("Dev", "and the test covers it", False, "1762000300000"),
        ),
        path="src/app.py",
        line=25,
        resolved=False,
    )]
    assert session.get.call_args_list[0].args[0] == f"{PR}/activities"

    forge.post_reply(_ref(), inline[0].reply_to, "It is reachable from the retry path.")

    [post] = session.post.call_args_list
    assert post.args[0] == f"{PR}/comments"
    assert post.kwargs["json"] == {"text": "It is reachable from the retry path.", "parent": {"id": 10}}


def test_a_resolved_root_reports_resolved():
    resolved = {**ROOT, "state": "RESOLVED"}
    forge, _ = _forge(_activity_page([_activity(102, resolved)]))

    [conversation] = forge.list_conversations(_ref())

    assert conversation.resolved is True


def test_a_thread_whose_root_is_human_is_left_out():
    forge, _ = _forge(_activity_page(_feed()))

    conversations = forge.list_conversations(_ref())

    assert [c.reply_to for c in conversations if c.kind == "inline"] == ["10"]
    assert all("human root" not in cm.body for c in conversations for cm in c.comments)
    assert all("quoted" not in cm.body for c in conversations for cm in c.comments)


def test_top_level_comments_form_one_pr_conversation_and_an_empty_reply_to_posts_a_comment():
    forge, session = _forge(_activity_page(_feed()))

    conversations = forge.list_conversations(_ref())

    [pr] = [c for c in conversations if c.kind == "pr"]
    assert pr == Conversation(
        kind="pr",
        reply_to="",
        comments=(
            ConversationComment("Prxref-Bot", SUMMARY_BODY, True, "1761999000000"),
            ConversationComment("Lead", "thanks", False, "1762000400000"),
            ConversationComment("Dev", "@prxref is this safe?", False, "1762000500000"),
        ),
    )

    forge.post_reply(_ref(), "", "Yes: the lock is held by the caller.")

    [post] = session.post.call_args_list
    assert post.args[0] == f"{PR}/comments"
    assert post.kwargs["json"] == {"text": "Yes: the lock is held by the caller."}


def test_the_activity_feed_is_read_past_the_first_page():
    forge, session = _forge(
        _activity_page([_activity(106, REPLY, "REPLIED")], last=False, next_start=100),
        _activity_page([_activity(102, ROOT)]),
    )

    [conversation] = forge.list_conversations(_ref())

    assert conversation.reply_to == "10"
    assert len(conversation.comments) == 3
    assert [call.kwargs["params"]["start"] for call in session.get.call_args_list] == [0, 100]


def test_a_feed_read_failure_raises():
    forge, _ = _forge(
        _activity_page([_activity(102, ROOT)], last=False, next_start=100),
        _mock_response(500, json_data={"errors": [{"message": "boom"}]}),
    )

    with pytest.raises(FeedReadError):
        forge.list_conversations(_ref())


def test_a_rejected_reply_raises():
    forge, session = _forge()
    session.post.return_value = _mock_response(409, json_data={"errors": [{"message": "conflict"}]})

    with pytest.raises(requests.HTTPError):
        forge.post_reply(_ref(), "10", "reply")
