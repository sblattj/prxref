"""Tests for GitLab forge adapter."""
from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
import requests

from prxref.forges import gitlab
from prxref.forges.base import FeedReadError, InlineComment, PRRef
from prxref.forges.gitlab import ForgeImpl, _make_retry_session
from prxref.triage import parse_unified_diff


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


def test_parse_pr_url_gitlab_com():
    url = "https://gitlab.com/gitlab-org/gitlab/-/merge_requests/12345"
    ref = ForgeImpl.parse_pr_url(url)
    assert ref is not None
    assert ref.forge == "gitlab"
    assert ref.host == "gitlab.com"
    assert ref.owner == "gitlab-org"
    assert ref.repo == "gitlab"
    assert ref.number == 12345
    assert ref.url == "https://gitlab.com/gitlab-org/gitlab/-/merge_requests/12345"


def test_parse_pr_url_subgroups():
    url = "https://gitlab.com/group/subgroup/nested/my-repo/-/merge_requests/42"
    ref = ForgeImpl.parse_pr_url(url)
    assert ref is not None
    assert ref.forge == "gitlab"
    assert ref.host == "gitlab.com"
    assert ref.owner == "group"
    assert ref.repo == "my-repo"
    assert ref.number == 42
    assert ref.url == "https://gitlab.com/group/subgroup/nested/my-repo/-/merge_requests/42"


def test_parse_pr_url_self_hosted():
    url = "https://git.mycompany.internal/core/infra/deployment/-/merge_requests/99"
    ref = ForgeImpl.parse_pr_url(url)
    assert ref is not None
    assert ref.forge == "gitlab"
    assert ref.host == "git.mycompany.internal"
    assert ref.owner == "core"
    assert ref.repo == "deployment"
    assert ref.number == 99
    assert ref.url == "https://git.mycompany.internal/core/infra/deployment/-/merge_requests/99"


def test_parse_pr_url_rejects():
    assert ForgeImpl.parse_pr_url("https://github.com/owner/repo/pull/1") is None
    assert ForgeImpl.parse_pr_url("https://bitbucket.org/owner/repo/pull-requests/1") is None
    assert ForgeImpl.parse_pr_url("not-a-url") is None
    assert ForgeImpl.parse_pr_url("https://gitlab.com/group/repo/issues/1") is None


def test_get_pr_with_diff_refs(monkeypatch):
    monkeypatch.setenv("PRXREF_GITLAB_TOKEN", "test-token")
    session = MagicMock(spec=requests.Session)
    forge = ForgeImpl(session=session)

    mr_data = {
        "title": "Fix memory leak",
        "description": "Resolves issue #123",
        "author": {"username": "alice", "name": "Alice Developer"},
        "source_branch": "feature/leak-fix",
        "target_branch": "main",
        "sha": "source_sha_123",
        "diff_refs": {
            "base_sha": "target_base_sha_456",
            "head_sha": "source_sha_123",
            "start_sha": "target_base_sha_456",
        },
    }
    session.get.return_value = _mock_response(200, json_data=mr_data)

    ref = PRRef("gitlab", "gitlab.com", "mygroup", "myrepo", 10, "https://gitlab.com/mygroup/myrepo/-/merge_requests/10")
    pr = forge.get_pr(ref)

    assert pr.title == "Fix memory leak"
    assert pr.description == "Resolves issue #123"
    assert pr.author == "alice"
    assert pr.source_branch == "feature/leak-fix"
    assert pr.target_branch == "main"
    assert pr.source_sha == "source_sha_123"
    assert pr.target_sha == "target_base_sha_456"

    session.get.assert_called_once()
    call_url = session.get.call_args[0][0]
    assert call_url == "https://gitlab.com/api/v4/projects/mygroup%2Fmyrepo/merge_requests/10"
    call_headers = session.get.call_args[1]["headers"]
    assert call_headers == {"PRIVATE-TOKEN": "test-token"}


def test_get_pr_branch_sha_fallback(monkeypatch):
    monkeypatch.delenv("PRXREF_GITLAB_TOKEN", raising=False)
    session = MagicMock(spec=requests.Session)
    forge = ForgeImpl(session=session)

    mr_data = {
        "title": "Update README",
        "description": "",
        "author": {"name": "Bob"},
        "source_branch": "patch-1",
        "target_branch": "main",
        "sha": "source_sha_789",
    }
    branch_data = {
        "name": "main",
        "commit": {"id": "target_sha_fallback_999"},
    }

    session.get.side_effect = [
        _mock_response(200, json_data=mr_data),
        _mock_response(200, json_data=branch_data),
    ]

    ref = PRRef("gitlab", "gitlab.com", "mygroup", "myrepo", 11, "https://gitlab.com/mygroup/myrepo/-/merge_requests/11")
    pr = forge.get_pr(ref)

    assert pr.title == "Update README"
    assert pr.author == "Bob"
    assert pr.source_sha == "source_sha_789"
    assert pr.target_sha == "target_sha_fallback_999"
    assert session.get.call_count == 2


def test_get_pr_branch_sha_fallback_failure(monkeypatch):
    session = MagicMock(spec=requests.Session)
    forge = ForgeImpl(session=session)

    mr_data = {
        "title": "Update README",
        "description": "",
        "source_branch": "patch-1",
        "target_branch": "main",
        "sha": "source_sha_789",
    }

    session.get.side_effect = [
        _mock_response(200, json_data=mr_data),
        _mock_response(404, text="Branch not found"),
    ]

    ref = PRRef("gitlab", "gitlab.com", "mygroup", "myrepo", 11, "https://gitlab.com/mygroup/myrepo/-/merge_requests/11")
    pr = forge.get_pr(ref)

    assert pr.source_sha == "source_sha_789"
    assert pr.target_sha == ""


def test_get_diff_header_reconstruction():
    session = MagicMock(spec=requests.Session)
    forge = ForgeImpl(session=session)

    diff_data = [
        {
            "old_path": "src/old.py",
            "new_path": "src/modified.py",
            "new_file": False,
            "deleted_file": False,
            "renamed_file": True,
            "diff": "@@ -1 +1 @@\n-hello\n+world\n",
        },
        {
            "old_path": "new_file.txt",
            "new_path": "new_file.txt",
            "new_file": True,
            "deleted_file": False,
            "renamed_file": False,
            "diff": "@@ -0,0 +1,2 @@\n+line1\n+line2\n",
        },
        {
            "old_path": "deleted.txt",
            "new_path": "deleted.txt",
            "new_file": False,
            "deleted_file": True,
            "renamed_file": False,
            "diff": "@@ -1,2 +0,0 @@\n-line1\n-line2\n",
        },
    ]

    session.get.return_value = _mock_response(200, json_data=diff_data)
    ref = PRRef("gitlab", "gitlab.com", "group", "repo", 5, "https://gitlab.com/group/repo/-/merge_requests/5")

    diff_text = forge.get_diff(ref)

    # Verify triage parser parses it without errors
    files = parse_unified_diff(diff_text)
    assert len(files) == 3
    assert files[0].path == "src/modified.py"
    assert files[0].status == "renamed"
    assert files[1].path == "new_file.txt"
    assert files[1].status == "added"
    assert files[2].path == "deleted.txt"
    assert files[2].status == "removed"


def test_get_diff_empty_raises():
    session = MagicMock(spec=requests.Session)
    forge = ForgeImpl(session=session)
    session.get.return_value = _mock_response(200, json_data=[])
    ref = PRRef("gitlab", "gitlab.com", "group", "repo", 5, "https://gitlab.com/group/repo/-/merge_requests/5")

    with pytest.raises(ValueError, match="Empty diff received"):
        forge.get_diff(ref)


def test_post_summary_create_new():
    session = MagicMock(spec=requests.Session)
    forge = ForgeImpl(session=session)

    # list notes returns empty list
    session.get.return_value = _mock_response(200, json_data=[])
    session.post.return_value = _mock_response(201, json_data={"id": 101})

    ref = PRRef("gitlab", "gitlab.com", "group", "repo", 7, "https://gitlab.com/group/repo/-/merge_requests/7")
    forge.post_summary(ref, "<!-- prxref-summary -->\nLGTM!")

    session.post.assert_called_once()
    assert session.put.call_count == 0
    post_url = session.post.call_args[0][0]
    assert post_url == "https://gitlab.com/api/v4/projects/group%2Frepo/merge_requests/7/notes"
    assert session.post.call_args[1]["json"] == {"body": "<!-- prxref-summary -->\nLGTM!"}


def test_post_summary_update_existing():
    session = MagicMock(spec=requests.Session)
    forge = ForgeImpl(session=session)

    # list notes returns one existing note with the marker
    existing_notes = [
        {"id": 44, "body": "Regular comment"},
        {"id": 88, "body": "Summary:\n<!-- prxref-summary -->\nOld summary"},
    ]
    session.get.return_value = _mock_response(200, json_data=existing_notes)
    session.put.return_value = _mock_response(200, json_data={"id": 88})

    ref = PRRef("gitlab", "gitlab.com", "group", "repo", 7, "https://gitlab.com/group/repo/-/merge_requests/7")
    forge.post_summary(ref, "<!-- prxref-summary -->\nNew summary")

    session.put.assert_called_once()
    assert session.post.call_count == 0
    put_url = session.put.call_args[0][0]
    assert put_url == "https://gitlab.com/api/v4/projects/group%2Frepo/merge_requests/7/notes/88"
    assert session.put.call_args[1]["json"] == {"body": "<!-- prxref-summary -->\nNew summary"}


def test_post_inline_comments_and_retry_as_note():
    session = MagicMock(spec=requests.Session)
    forge = ForgeImpl(session=session)

    # Mock get_pr call inside post_inline_comments
    mr_data = {
        "title": "PR",
        "description": "",
        "sha": "sha_head",
        "diff_refs": {
            "base_sha": "sha_base",
            "start_sha": "sha_start",
            "head_sha": "sha_head",
        },
    }
    session.get.return_value = _mock_response(200, json_data=mr_data)

    # Mock 1st comment 201 (success), 2nd comment 400 (fail -> fallback note 201)
    session.post.side_effect = [
        _mock_response(201, json_data={"id": 1}),  # discussion 1
        _mock_response(400, text="Line not in diff"),  # discussion 2 fail
        _mock_response(201, json_data={"id": 2}),  # fallback note 2
    ]

    comments = [
        InlineComment(path="src/main.py", line=10, body="Looks good"),
        InlineComment(path="src/other.py", line=99, body="Bad line"),
    ]
    ref = PRRef("gitlab", "gitlab.com", "group", "repo", 1, "https://gitlab.com/group/repo/-/merge_requests/1")
    count = forge.post_inline_comments(ref, comments)

    assert count == 2
    assert session.post.call_count == 3


def test_list_threads_flatten():
    session = MagicMock(spec=requests.Session)
    forge = ForgeImpl(session=session)

    discussions = [
        {
            "id": "disc1",
            "resolved": False,
            "notes": [
                {
                    "id": 1,
                    "author": {"username": "reviewer1"},
                    "body": "First line comment",
                    "position": {
                        "new_path": "src/app.py",
                        "new_line": 25,
                    },
                    "resolved": False,
                },
                {
                    "id": 2,
                    "author": {"username": "dev"},
                    "body": "Fixed in latest commit",
                    "resolved": False,
                },
            ],
        },
        {
            "id": "disc2",
            "resolved": True,
            "notes": [
                {
                    "id": 3,
                    "author": {"username": "reviewer2"},
                    "body": "General comment without position",
                }
            ],
        },
    ]

    session.get.return_value = _mock_response(200, json_data=discussions)
    ref = PRRef("gitlab", "gitlab.com", "group", "repo", 3, "https://gitlab.com/group/repo/-/merge_requests/3")

    threads = forge.list_threads(ref)
    assert len(threads) == 3

    assert threads[0].path == "src/app.py"
    assert threads[0].line == 25
    assert threads[0].author == "reviewer1"
    assert threads[0].body_snippet == "First line comment"
    assert threads[0].resolved is False

    assert threads[1].path == "src/app.py"
    assert threads[1].line == 25
    assert threads[1].author == "dev"
    assert threads[1].body_snippet == "Fixed in latest commit"
    assert threads[1].resolved is False

    assert threads[2].path is None
    assert threads[2].line is None
    assert threads[2].author == "reviewer2"
    assert threads[2].resolved is True


# --- summary dedup: paging and unreadable feeds ------------------------------

# The note page size the adapter is expected to ask for, pinned by its own test
# below so the rest of these read as data rather than as a coupling.
NOTE_PAGE_SIZE = 100
MARKER = "<!-- prxref-summary -->"


def _gl_ref():
    return PRRef(
        "gitlab", "gitlab.com", "group", "repo", 7,
        "https://gitlab.com/group/repo/-/merge_requests/7",
    )


def _full_note_page(prefix="chatter"):
    """A page of exactly per_page notes — the shape that means 'keep going'."""
    return _mock_response(
        200, json_data=[{"id": i, "body": f"{prefix} {i}"} for i in range(NOTE_PAGE_SIZE)]
    )


def test_post_summary_asks_for_a_full_page_of_notes():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(200, json_data=[])
    session.post.return_value = _mock_response(201, json_data={"id": 1})

    ForgeImpl(session=session).post_summary(_gl_ref(), "body")

    params = session.get.call_args[1]["params"]
    assert params["per_page"] == NOTE_PAGE_SIZE == gitlab._PAGE_SIZE
    assert params["page"] == 1
    # Oldest-first, so a note created during the walk lands at the END and
    # cannot shift a later page's window back over a note already passed.
    assert params["sort"] == "asc"
    assert params["order_by"] == "created_at"


def test_post_summary_finds_a_summary_past_the_first_page():
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = [
        _full_note_page(),
        _mock_response(200, json_data=[{"id": 88, "body": f"{MARKER}\nold summary"}]),
    ]
    session.put.return_value = _mock_response(200, json_data={"id": 88})

    ForgeImpl(session=session).post_summary(_gl_ref(), "new summary")

    session.post.assert_not_called()
    session.put.assert_called_once()
    assert session.put.call_args[0][0].endswith("/notes/88")
    assert session.get.call_args_list[1][1]["params"]["page"] == 2


def test_post_summary_stamps_the_marker_when_the_body_lacks_one():
    # Nothing upstream of the adapter puts the marker in the body, so a summary
    # posted without one is invisible to every later lookup.
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(200, json_data=[])
    session.post.return_value = _mock_response(201, json_data={"id": 1})

    ForgeImpl(session=session).post_summary(_gl_ref(), "no marker here")

    assert session.post.call_args[1]["json"] == {"body": f"{MARKER}\nno marker here"}


def test_post_summary_refuses_to_post_when_the_note_read_fails():
    # The old code swallowed the transport error and fell through to the POST,
    # which is a SECOND summary on a PR that already had one.
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = requests.ConnectionError("down")

    with pytest.raises(FeedReadError, match="note feed"):
        ForgeImpl(session=session).post_summary(_gl_ref(), "body")

    session.post.assert_not_called()
    session.put.assert_not_called()


def test_post_summary_refuses_to_post_when_the_note_read_returns_an_error_status():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(500, json_data={"message": "boom"})

    with pytest.raises(FeedReadError, match="500"):
        ForgeImpl(session=session).post_summary(_gl_ref(), "body")

    session.post.assert_not_called()


def test_post_summary_refuses_to_post_when_the_notes_outrun_the_page_budget():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _full_note_page()

    with pytest.raises(FeedReadError, match="page budget"):
        ForgeImpl(session=session).post_summary(_gl_ref(), "body")

    assert session.get.call_count == gitlab._MAX_PAGES
    session.post.assert_not_called()


def test_post_summary_stops_reading_as_soon_as_it_finds_the_marker():
    session = MagicMock(spec=requests.Session)
    hit = [{"id": i, "body": "chatter"} for i in range(NOTE_PAGE_SIZE - 1)]
    hit.append({"id": 88, "body": MARKER})
    session.get.return_value = _mock_response(200, json_data=hit)
    session.put.return_value = _mock_response(200, json_data={"id": 88})

    ForgeImpl(session=session).post_summary(_gl_ref(), "new summary")

    assert session.get.call_count == 1


def test_list_threads_pages_past_the_first_page():
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = [
        _mock_response(
            200,
            json_data=[
                {"id": f"d{i}", "notes": [{"id": i, "body": f"early {i}"}]}
                for i in range(NOTE_PAGE_SIZE)
            ],
        ),
        _mock_response(200, json_data=[{"id": "last", "notes": [{"id": 9, "body": "late"}]}]),
    ]

    threads = ForgeImpl(session=session).list_threads(_gl_ref())

    assert len(threads) == NOTE_PAGE_SIZE + 1
    assert threads[-1].body_snippet == "late"


def test_list_threads_keeps_what_it_read_and_warns_when_the_feed_read_fails(caplog):
    # Dedup input is best-effort: a partial read beats no read, but the
    # shortfall has to be visible rather than silent.
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = [
        _mock_response(
            200,
            json_data=[
                {"id": f"d{i}", "notes": [{"id": i, "body": f"early {i}"}]}
                for i in range(NOTE_PAGE_SIZE)
            ],
        ),
        requests.ConnectionError("down"),
    ]

    with caplog.at_level(logging.WARNING, logger="prxref.forges.gitlab"):
        threads = ForgeImpl(session=session).list_threads(_gl_ref())

    assert len(threads) == NOTE_PAGE_SIZE
    assert "incomplete" in caplog.text.lower()


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
        resp = session.post(probe.url, json={"body": "finding"}, timeout=(5.0, 5.0))

    assert probe.received == ["POST"]
    assert resp.status_code == 502


def test_a_lost_summary_update_is_not_re_sent():
    """The summary update is a write too, and PUT is not exempt.

    The note update overwrites the body, so a replay would write the
    same text twice and settle. It is dropped anyway: one policy
    shared by four adapters is worth more than one exemption.
    """
    session = _make_retry_session()
    with _RetryProbe([502]) as probe:
        resp = session.put(probe.url, json={"body": "summary"}, timeout=(5.0, 5.0))

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
    retry = _make_retry_session().get_adapter("https://gitlab.com").max_retries

    assert retry.allowed_methods == frozenset(["GET", "HEAD", "OPTIONS"])
    assert retry.is_retry("GET", 502) is True
    assert retry.is_retry("POST", 502) is False
    # 429 is the one status a write could safely be replayed after — the server
    # says it did not process the request — but urllib3 tests the method before
    # it consults the status list, so holding a POST back on 502 holds it back
    # on 429 too. That trade is deliberate; see the comment on the policy.
    assert retry.is_retry("POST", 429) is False
