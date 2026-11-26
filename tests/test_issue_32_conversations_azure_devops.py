"""Issue #32: Azure DevOps ``list_conversations`` and ``post_reply``.

Threads come from ``GET .../pullrequests/{id}/threads`` (one unpaged
response). A thread reply is ``POST .../threads/{threadId}/comments``; a
top-level reply is a new ``POST .../threads`` with no ``threadContext``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_forge_azure_devops import PR_API, _Ado, _json, _ref, _status  # noqa: E402

from prxref.forges.azure_devops import ForgeImpl  # noqa: E402
from prxref.forges.base import (  # noqa: E402
    ATTRIBUTION_MARKER,
    Conversation,
    ConversationComment,
    FeedReadError,
)

PRXREF_BODY = (
    "**[WARNING] Null check missing** (`src/app.py:25`)\n\nbody\n\n---\n"
    f"*{ATTRIBUTION_MARKER} · model=m*"
)
SUMMARY_BODY = f"<!-- prxref-summary -->\n## Review\n\n*{ATTRIBUTION_MARKER} · model=m*"
CONTEXT = {
    "filePath": "/src/app.py",
    "rightFileStart": {"line": 25, "offset": 1},
    "rightFileEnd": {"line": 25, "offset": 1},
}


def _comment(cid, content, who, published, *, parent=0, kind="text", deleted=False):
    return {
        "id": cid, "parentCommentId": parent, "content": content, "commentType": kind,
        "isDeleted": deleted, "publishedDate": published,
        "author": {"displayName": who, "uniqueName": f"{who.lower()}@example.com"},
    }


def _thread(tid, status, comments, context=None):
    thread = {
        "id": tid, "status": status, "isDeleted": False, "comments": comments,
        "threadContext": context,
        "pullRequestThreadContext": None if context is None else {
            "iterationContext": {"firstComparingIteration": 1, "secondComparingIteration": 1},
            "changeTrackingId": 2,
        },
    }
    return thread


def _threads():
    return {"value": [
        _thread(2631, "active", [
            _comment(1, PRXREF_BODY, "prxref", "2026-11-01T10:00:00.000Z"),
            _comment(2, "Why is this a warning?\nThe caller checks.", "Dev", "2026-11-01T11:00:00.000Z", parent=1),
            _comment(3, "gone", "Dev", "2026-11-01T11:10:00.000Z", parent=1, deleted=True),
            _comment(4, "Dev resolved the thread", "Dev", "2026-11-01T11:20:00.000Z", kind="system"),
        ], CONTEXT),
        _thread(2632, "active", [
            _comment(1, "human root", "Reviewer", "2026-11-01T10:05:00.000Z"),
            _comment(2, f"quoted {ATTRIBUTION_MARKER}", "prxref", "2026-11-01T10:06:00.000Z", parent=1),
        ], {**CONTEXT, "filePath": "/src/other.py"}),
        _thread(2640, "active", [
            _comment(1, "@prxref is this safe?", "Dev", "2026-11-01T13:00:00.000Z"),
        ]),
        _thread(2630, "closed", [
            _comment(1, SUMMARY_BODY, "prxref", "2026-11-01T09:00:00.000Z"),
            _comment(2, "thanks", "Lead", "2026-11-01T12:00:00.000Z", parent=1),
        ]),
        _thread(2641, "active", [
            _comment(1, "Dev voted 10", "Dev", "2026-11-01T14:00:00.000Z", kind="system"),
        ]),
    ]}


def _forge(payload=None):
    ado = _Ado()
    ado.on("GET", f"{PR_API}/threads", _json(_threads() if payload is None else payload))
    ado.on("POST", f"{PR_API}/threads/2631/comments", _json({"id": 5, "parentCommentId": 1}))
    ado.on("POST", f"{PR_API}/threads", _json({"id": 2700}))
    return ForgeImpl(session=ado.session), ado


def test_prxref_rooted_thread_maps_to_one_inline_conversation_and_reply_targets_it():
    forge, ado = _forge()

    conversations = forge.list_conversations(_ref())

    inline = [c for c in conversations if c.kind == "inline"]
    assert inline == [Conversation(
        kind="inline",
        reply_to="2631",
        comments=(
            ConversationComment("prxref", PRXREF_BODY, True, "2026-11-01T10:00:00.000Z"),
            ConversationComment(
                "Dev", "Why is this a warning?\nThe caller checks.", False, "2026-11-01T11:00:00.000Z",
            ),
        ),
        path="src/app.py",
        line=25,
        resolved=False,
    )]

    forge.post_reply(_ref(), inline[0].reply_to, "It is reachable from the retry path.")

    [post] = ado.of("POST")
    assert post.path == f"{PR_API}/threads/2631/comments"
    assert post.params == {"api-version": "7.1"}
    assert post.body == {
        "content": "It is reachable from the retry path.",
        "parentCommentId": 1,
        "commentType": 1,
    }


@pytest.mark.parametrize(("status", "resolved"), [("fixed", True), ("wontFix", True), ("active", False)])
def test_resolved_follows_the_thread_status(status, resolved):
    payload = _threads()
    payload["value"][0]["status"] = status
    forge, _ = _forge(payload)

    [conversation] = [c for c in forge.list_conversations(_ref()) if c.kind == "inline"]

    assert conversation.resolved is resolved


def test_a_thread_whose_root_is_human_is_left_out():
    forge, _ = _forge()

    conversations = forge.list_conversations(_ref())

    assert [c.reply_to for c in conversations if c.kind == "inline"] == ["2631"]
    assert all("human root" not in cm.body for c in conversations for cm in c.comments)
    assert all("quoted" not in cm.body for c in conversations for cm in c.comments)


def test_pr_level_threads_form_one_pr_conversation_and_an_empty_reply_to_opens_a_thread():
    forge, ado = _forge()

    conversations = forge.list_conversations(_ref())

    [pr] = [c for c in conversations if c.kind == "pr"]
    assert pr == Conversation(
        kind="pr",
        reply_to="",
        comments=(
            ConversationComment("prxref", SUMMARY_BODY, True, "2026-11-01T09:00:00.000Z"),
            ConversationComment("Lead", "thanks", False, "2026-11-01T12:00:00.000Z"),
            ConversationComment("Dev", "@prxref is this safe?", False, "2026-11-01T13:00:00.000Z"),
        ),
    )

    forge.post_reply(_ref(), "", "Yes: the lock is held by the caller.")

    [post] = ado.of("POST")
    assert post.path == f"{PR_API}/threads"
    assert post.params == {"api-version": "7.1"}
    assert post.body == {
        "comments": [{"parentCommentId": 0, "content": "Yes: the lock is held by the caller.", "commentType": 1}],
        "status": "closed",
    }
    assert "threadContext" not in post.body


def test_a_thread_read_failure_raises():
    ado = _Ado()
    ado.on("GET", f"{PR_API}/threads", _status(500, '{"message": "boom"}'))
    forge = ForgeImpl(session=ado.session)

    with pytest.raises(FeedReadError):
        forge.list_conversations(_ref())


def test_a_rejected_reply_raises():
    ado = _Ado()
    ado.on("POST", f"{PR_API}/threads/2631/comments", _status(403, '{"message": "denied"}'))
    forge = ForgeImpl(session=ado.session)

    with pytest.raises(requests.HTTPError):
        forge.post_reply(_ref(), "2631", "reply")
