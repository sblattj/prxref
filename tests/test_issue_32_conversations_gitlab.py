"""Issue #32: GitLab ``list_conversations`` and ``post_reply``.

Discussions come from ``GET .../merge_requests/:iid/discussions``; a reply in
a thread goes to ``POST .../discussions/:discussion_id/notes`` and a
top-level reply to ``POST .../merge_requests/:iid/notes``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_forge_gitlab import NOTE_PAGE_SIZE, _gl_ref, _mock_response  # noqa: E402

from prxref.forges.base import (  # noqa: E402
    ATTRIBUTION_MARKER,
    Conversation,
    ConversationComment,
    FeedReadError,
)
from prxref.forges.gitlab import ForgeImpl  # noqa: E402
from prxref.forges.replay import LocalDiffForge, ReplayForge  # noqa: E402

API = "https://gitlab.com/api/v4/projects/group%2Frepo/merge_requests/7"
PRXREF_BODY = (
    "**[WARNING] Null check missing** (`src/app.py:25`)\n\nbody\n\n---\n"
    f"*{ATTRIBUTION_MARKER} · model=m*"
)
SUMMARY_BODY = f"<!-- prxref-summary -->\n## Review\n\n*{ATTRIBUTION_MARKER} · model=m*"


def _discussions():
    return [
        {
            "id": "6a9c1750b37d513a43987b574953fceb50b03ce7",
            "individual_note": False,
            "notes": [
                {
                    "id": 1126, "type": "DiffNote", "body": PRXREF_BODY,
                    "author": {"username": "prxref-bot", "name": "prxref"},
                    "created_at": "2026-11-01T10:00:00.000Z", "system": False,
                    "position": {"new_path": "src/app.py", "new_line": 25, "old_path": "src/app.py"},
                    "resolvable": True, "resolved": False,
                },
                {
                    "id": 1127, "type": "DiffNote", "body": "Why is this a warning?\nThe caller checks.",
                    "author": {"username": "dev", "name": "Dev"},
                    "created_at": "2026-11-01T11:00:00.000Z", "system": False,
                    "position": {"new_path": "src/app.py", "new_line": 25, "old_path": "src/app.py"},
                    "resolvable": True, "resolved": False,
                },
                {
                    "id": 1128, "type": None, "body": "changed this line in version 2 of the diff",
                    "author": {"username": "dev"}, "created_at": "2026-11-01T12:00:00.000Z",
                    "system": True,
                },
            ],
        },
        {
            "id": "87805b7c09016a7058e91bdbe7b29d1f284a39e6",
            "individual_note": False,
            "notes": [
                {
                    "id": 1200, "type": "DiffNote", "body": "human root",
                    "author": {"username": "reviewer"}, "created_at": "2026-11-01T10:30:00.000Z",
                    "system": False, "position": {"new_path": "src/other.py", "new_line": 3},
                    "resolved": False,
                },
                {
                    "id": 1201, "type": "DiffNote", "body": f"quoted {ATTRIBUTION_MARKER}",
                    "author": {"username": "prxref-bot"}, "created_at": "2026-11-01T10:40:00.000Z",
                    "system": False, "position": {"new_path": "src/other.py", "new_line": 3},
                },
            ],
        },
        {
            "id": "aa01", "individual_note": True,
            "notes": [{
                "id": 1300, "type": None, "body": "@prxref-bot is this safe?",
                "author": {"username": "dev"}, "created_at": "2026-11-01T13:00:00.000Z",
                "system": False,
            }],
        },
        {
            "id": "aa00", "individual_note": True,
            "notes": [{
                "id": 1100, "type": None, "body": SUMMARY_BODY,
                "author": {"username": "prxref-bot"}, "created_at": "2026-11-01T09:00:00.000Z",
                "system": False,
            }],
        },
        {
            "id": "aa02", "individual_note": True,
            "notes": [{
                "id": 1301, "type": None, "body": "approved this merge request",
                "author": {"username": "lead"}, "created_at": "2026-11-01T14:00:00.000Z",
                "system": True,
            }],
        },
    ]


def _forge(*responses):
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = list(responses)
    session.post.return_value = _mock_response(201, json_data={"id": 9})
    return ForgeImpl(session=session), session


def test_prxref_rooted_thread_maps_to_one_inline_conversation_and_reply_targets_it():
    forge, session = _forge(_mock_response(200, json_data=_discussions()))

    conversations = forge.list_conversations(_gl_ref())

    inline = [c for c in conversations if c.kind == "inline"]
    assert inline == [Conversation(
        kind="inline",
        reply_to="6a9c1750b37d513a43987b574953fceb50b03ce7",
        comments=(
            ConversationComment("prxref-bot", PRXREF_BODY, True, "2026-11-01T10:00:00.000Z"),
            ConversationComment(
                "dev", "Why is this a warning?\nThe caller checks.", False, "2026-11-01T11:00:00.000Z",
            ),
        ),
        path="src/app.py",
        line=25,
        resolved=False,
    )]
    [get] = session.get.call_args_list
    assert get.args[0] == f"{API}/discussions"

    forge.post_reply(_gl_ref(), inline[0].reply_to, "It is reachable from the retry path.")

    [post] = session.post.call_args_list
    assert post.args[0] == f"{API}/discussions/6a9c1750b37d513a43987b574953fceb50b03ce7/notes"
    assert post.kwargs["json"] == {"body": "It is reachable from the retry path."}


def test_a_resolved_discussion_reports_resolved():
    discussions = _discussions()[:1]
    discussions[0]["resolved"] = True
    forge, _ = _forge(_mock_response(200, json_data=discussions))

    [conversation] = forge.list_conversations(_gl_ref())

    assert conversation.resolved is True


def test_a_thread_whose_root_is_human_is_left_out():
    forge, _ = _forge(_mock_response(200, json_data=_discussions()))

    conversations = forge.list_conversations(_gl_ref())

    assert [c.reply_to for c in conversations if c.kind == "inline"] == [
        "6a9c1750b37d513a43987b574953fceb50b03ce7",
    ]
    assert all("human root" not in cm.body for c in conversations for cm in c.comments)


def test_top_level_notes_form_one_pr_conversation_and_an_empty_reply_to_posts_a_note():
    forge, session = _forge(_mock_response(200, json_data=_discussions()))

    conversations = forge.list_conversations(_gl_ref())

    [pr] = [c for c in conversations if c.kind == "pr"]
    assert pr == Conversation(
        kind="pr",
        reply_to="",
        comments=(
            ConversationComment("prxref-bot", SUMMARY_BODY, True, "2026-11-01T09:00:00.000Z"),
            ConversationComment("dev", "@prxref-bot is this safe?", False, "2026-11-01T13:00:00.000Z"),
        ),
    )

    forge.post_reply(_gl_ref(), "", "Yes: the lock is held by the caller.")

    [post] = session.post.call_args_list
    assert post.args[0] == f"{API}/notes"
    assert post.kwargs["json"] == {"body": "Yes: the lock is held by the caller."}


def test_discussions_are_read_past_the_first_page():
    first = [
        {"id": f"d{i}", "notes": [{"id": i, "body": f"early {i}", "author": {"username": "dev"}}]}
        for i in range(NOTE_PAGE_SIZE)
    ]
    late = _discussions()[:1]
    forge, session = _forge(_mock_response(200, json_data=first), _mock_response(200, json_data=late))

    conversations = forge.list_conversations(_gl_ref())

    assert [c.reply_to for c in conversations if c.kind == "inline"] == [
        "6a9c1750b37d513a43987b574953fceb50b03ce7",
    ]
    assert [call.kwargs["params"]["page"] for call in session.get.call_args_list] == [1, 2]


def test_a_feed_read_failure_raises():
    first = [{"id": f"d{i}", "notes": [{"id": i, "body": "x"}]} for i in range(NOTE_PAGE_SIZE)]
    forge, _ = _forge(_mock_response(200, json_data=first), _mock_response(500, json_data={"message": "boom"}))

    with pytest.raises(FeedReadError):
        forge.list_conversations(_gl_ref())


def test_a_rejected_reply_raises():
    forge, session = _forge()
    session.post.return_value = _mock_response(403, json_data={"message": "403 Forbidden"})

    with pytest.raises(requests.HTTPError):
        forge.post_reply(_gl_ref(), "6a9c1750b37d513a43987b574953fceb50b03ce7", "reply")


def test_replay_and_local_diff_forges_expose_no_reply_methods():
    replay = ReplayForge(ForgeImpl(session=MagicMock(spec=requests.Session)))
    local = LocalDiffForge("diff --git a/x b/x\n", path="x.diff")

    for forge in (replay, local):
        assert getattr(forge, "list_conversations", None) is None
        assert getattr(forge, "post_reply", None) is None
