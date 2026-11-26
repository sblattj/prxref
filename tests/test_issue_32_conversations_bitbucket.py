"""Issue #32: Bitbucket Cloud ``list_conversations`` and ``post_reply``.

Comments come from ``GET .../pullrequests/{id}/comments`` (paged by
``next``) and join into threads along ``parent.id``. Both a thread reply and
a top-level reply are ``POST .../pullrequests/{id}/comments``; a thread reply
carries ``parent.id``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_forge_bitbucket import _mock_response, _page, _ref  # noqa: E402

from prxref.forges.base import (  # noqa: E402
    ATTRIBUTION_MARKER,
    Conversation,
    ConversationComment,
    FeedReadError,
)
from prxref.forges.bitbucket import ForgeImpl  # noqa: E402

COMMENTS_URL = "https://api.bitbucket.org/2.0/repositories/acme/api/pullrequests/42/comments"
PRXREF_BODY = (
    "**[WARNING] Null check missing** (`src/app.py:25`)\n\nbody\n\n---\n"
    f"*{ATTRIBUTION_MARKER} · model=m*"
)
SUMMARY_BODY = f"<!-- prxref-summary -->\n## Review\n\n*{ATTRIBUTION_MARKER} · model=m*"
HTML = "https://bitbucket.org/acme/api/pull-requests/42/_/diff#comment-"


def _comment(cid, raw, who, created, *, inline=None, parent=None, deleted=False):
    item = {
        "id": cid,
        "type": "pullrequest_comment",
        "content": {"raw": raw, "markup": "markdown", "html": f"<p>{raw}</p>"},
        "user": {"display_name": who.title(), "nickname": who, "uuid": "{" + who + "}"},
        "created_on": created,
        "deleted": deleted,
        "links": {"html": {"href": f"{HTML}{cid}"}},
    }
    if inline is not None:
        item["inline"] = inline
    if parent is not None:
        item["parent"] = {"id": parent}
    return item


ANCHOR = {"path": "src/app.py", "from": None, "to": 25}
ROOT = _comment(501, PRXREF_BODY, "prxref-bot", "2026-11-01T10:00:00+00:00", inline=ANCHOR)
REPLY = _comment(
    530, "Why is this a warning?\nThe caller checks.", "dev", "2026-11-01T11:00:00+00:00",
    inline=ANCHOR, parent=501,
)
NESTED = _comment(540, "and the test covers it", "dev", "2026-11-01T11:30:00+00:00", inline=ANCHOR, parent=530)
HUMAN_ROOT = _comment(
    502, "human root", "reviewer", "2026-11-01T10:10:00+00:00", inline={"path": "src/other.py", "to": 3},
)
HUMAN_ROOT_PRXREF_REPLY = _comment(
    503, f"quoted {ATTRIBUTION_MARKER}", "prxref-bot", "2026-11-01T10:20:00+00:00",
    inline={"path": "src/other.py", "to": 3}, parent=502,
)
SUMMARY = _comment(500, SUMMARY_BODY, "prxref-bot", "2026-11-01T09:00:00+00:00")
QUESTION = _comment(560, "@prxref is this safe?", "dev", "2026-11-01T13:00:00+00:00")
ANSWER_TO_SUMMARY = _comment(555, "thanks", "lead", "2026-11-01T12:00:00+00:00", parent=500)
GONE = _comment(570, "", "dev", "2026-11-01T14:00:00+00:00", deleted=True)


def _forge(*responses):
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = list(responses)
    session.post.return_value = _mock_response(201, json_data={"id": 900})
    return ForgeImpl(session=session), session


def test_prxref_rooted_thread_maps_to_one_inline_conversation_and_reply_targets_its_root():
    forge, session = _forge(_page([SUMMARY, ROOT, HUMAN_ROOT, HUMAN_ROOT_PRXREF_REPLY, REPLY, NESTED]))

    conversations = forge.list_conversations(_ref())

    inline = [c for c in conversations if c.kind == "inline"]
    assert inline == [Conversation(
        kind="inline",
        reply_to="501",
        comments=(
            ConversationComment("Prxref-Bot", PRXREF_BODY, True, "2026-11-01T10:00:00+00:00"),
            ConversationComment(
                "Dev", "Why is this a warning?\nThe caller checks.", False, "2026-11-01T11:00:00+00:00",
            ),
            ConversationComment("Dev", "and the test covers it", False, "2026-11-01T11:30:00+00:00"),
        ),
        path="src/app.py",
        line=25,
        resolved=False,
        url=f"{HTML}501",
    )]
    assert session.get.call_args_list[0].args[0] == COMMENTS_URL

    forge.post_reply(_ref(), inline[0].reply_to, "It is reachable from the retry path.")

    [post] = session.post.call_args_list
    assert post.args[0] == COMMENTS_URL
    assert post.kwargs["json"] == {
        "content": {"raw": "It is reachable from the retry path."},
        "parent": {"id": 501},
    }


def test_a_root_with_a_resolution_reports_resolved():
    resolved_root = {
        **ROOT,
        "resolution": {"type": "comment_resolution", "created_on": "2026-11-02T09:00:00+00:00"},
    }
    forge, _ = _forge(_page([resolved_root, REPLY]))

    [conversation] = forge.list_conversations(_ref())

    assert conversation.resolved is True


def test_a_thread_whose_root_is_human_is_left_out():
    forge, _ = _forge(_page([ROOT, HUMAN_ROOT, HUMAN_ROOT_PRXREF_REPLY]))

    conversations = forge.list_conversations(_ref())

    assert [c.reply_to for c in conversations if c.kind == "inline"] == ["501"]
    assert all("human root" not in cm.body for c in conversations for cm in c.comments)
    assert all("quoted" not in cm.body for c in conversations for cm in c.comments)


def test_top_level_comments_form_one_pr_conversation_and_an_empty_reply_to_posts_a_comment():
    forge, session = _forge(_page([QUESTION, ROOT, SUMMARY, ANSWER_TO_SUMMARY, GONE]))

    conversations = forge.list_conversations(_ref())

    [pr] = [c for c in conversations if c.kind == "pr"]
    assert pr == Conversation(
        kind="pr",
        reply_to="",
        comments=(
            ConversationComment("Prxref-Bot", SUMMARY_BODY, True, "2026-11-01T09:00:00+00:00"),
            ConversationComment("Lead", "thanks", False, "2026-11-01T12:00:00+00:00"),
            ConversationComment("Dev", "@prxref is this safe?", False, "2026-11-01T13:00:00+00:00"),
        ),
    )

    forge.post_reply(_ref(), "", "Yes: the lock is held by the caller.")

    [post] = session.post.call_args_list
    assert post.args[0] == COMMENTS_URL
    assert post.kwargs["json"] == {"content": {"raw": "Yes: the lock is held by the caller."}}


def test_a_reply_on_a_later_page_joins_its_root():
    forge, session = _forge(
        _page([ROOT], next_url=f"{COMMENTS_URL}?page=2&pagelen=100"),
        _page([REPLY]),
    )

    [conversation] = forge.list_conversations(_ref())

    assert [c.body for c in conversation.comments] == [PRXREF_BODY, REPLY["content"]["raw"]]
    assert session.get.call_args_list[1].args[0] == f"{COMMENTS_URL}?page=2&pagelen=100"


def test_a_feed_read_failure_raises():
    forge, _ = _forge(
        _page([ROOT], next_url=f"{COMMENTS_URL}?page=2"),
        _mock_response(500, json_data={"type": "error"}),
    )

    with pytest.raises(FeedReadError):
        forge.list_conversations(_ref())


def test_a_rejected_reply_raises():
    forge, session = _forge()
    session.post.return_value = _mock_response(403, json_data={"type": "error"})

    with pytest.raises(requests.HTTPError):
        forge.post_reply(_ref(), "501", "reply")
