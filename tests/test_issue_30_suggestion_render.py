"""Code suggestions rendered and posted per forge (#30).

A finding's ``suggestion`` arrives already validated. What is tested here is
how each forge renders it: GitHub's ``suggestion`` block with a multi-line
range, GitLab's ``suggestion:-0+K`` anchored at the first line, and a
copyable fallback everywhere else. A finding without a suggestion, or with
one the guards reject, must post exactly what it posted before suggestions
existed.
"""
from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import MagicMock

import pytest
import requests

from prxref import orchestrator
from prxref.forges import azure_devops, bitbucket, bitbucket_server, github, gitlab
from prxref.forges.base import InlineComment
from prxref.formatter import format_suggestion_block, suggestion_range
from prxref.markers import inline_header
from prxref.orchestrator import _format_finding, _inline_comment, orchestrate_review
from prxref.quality import apply_thread_dedup
from prxref.triage import Finding
from tests.test_orchestrator import REF, FakeForge, FakeLLM, _added_file_diff

MODEL = "m"


def _finding(line=10, suggestion=None, end=0, body="Use the helper instead."):
    return Finding(
        "src/app.py", line, "warning", 0.9, "Unbounded retry", body,
        suggestion=suggestion, suggestion_end_line=end,
    )


def _base_body(f: Finding) -> str:
    """The body every inline comment carried before suggestions existed."""
    return (
        f"{inline_header(f)}\n\n"
        f"{f.body}\n\n"
        f"---\n*Reviewed by prxref · model={MODEL}*"
    )


def _with_block(f: Finding, block: str) -> str:
    return (
        f"{inline_header(f)}\n\n"
        f"{f.body}\n\n"
        f"{block}\n\n"
        f"---\n*Reviewed by prxref · model={MODEL}*"
    )


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.headers = {}
    if json_data is not None:
        resp.json.return_value = json_data
        resp.text = json.dumps(json_data)
    else:
        resp.text = text
        resp.json.side_effect = ValueError("No JSON")
    resp.content = resp.text.encode("utf-8")
    resp.raise_for_status.side_effect = None if resp.ok else requests.HTTPError(response=resp)
    return resp


def _ref(forge_module, url):
    ref = forge_module.ForgeImpl.parse_pr_url(url)
    assert ref is not None
    return ref


GITHUB_URL = "https://github.com/acme/api/pull/42"
GITLAB_URL = "https://gitlab.com/acme/api/-/merge_requests/7"
BITBUCKET_URL = "https://bitbucket.org/acme/api/pull-requests/42"
BITBUCKET_SERVER_URL = "https://bitbucket.example.com/projects/PLAT/repos/api/pull-requests/42"
AZURE_URL = "https://dev.azure.com/acme/Web/_git/Web/pullrequest/551"

ALL_STYLES = [
    github.ForgeImpl.suggestion_style,
    gitlab.ForgeImpl.suggestion_style,
    getattr(bitbucket.ForgeImpl, "suggestion_style", None),
    getattr(bitbucket_server.ForgeImpl, "suggestion_style", None),
    getattr(azure_devops.ForgeImpl, "suggestion_style", None),
]


def _post_github(comments):
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(
        200,
        json_data={
            "title": "t", "body": "", "user": {"login": "u"},
            "head": {"ref": "feature", "sha": "abc123"},
            "base": {"ref": "main", "sha": "cafe"},
        },
    )
    session.post.return_value = _mock_response(201, json_data={"id": 1})
    github.ForgeImpl(session=session).post_inline_comments(_ref(github, GITHUB_URL), comments)
    return [c.kwargs["json"] for c in session.post.call_args_list]


def _post_gitlab(comments):
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(
        200,
        json_data={
            "title": "PR", "description": "", "sha": "sha_head",
            "diff_refs": {"base_sha": "sha_base", "start_sha": "sha_start", "head_sha": "sha_head"},
        },
    )
    session.post.return_value = _mock_response(201, json_data={"id": 1})
    gitlab.ForgeImpl(session=session).post_inline_comments(_ref(gitlab, GITLAB_URL), comments)
    return [c.kwargs["json"] for c in session.post.call_args_list]


def _post_bitbucket(comments):
    session = MagicMock(spec=requests.Session)
    session.post.return_value = _mock_response(201, json_data={"id": 1})
    bitbucket.ForgeImpl(session=session).post_inline_comments(_ref(bitbucket, BITBUCKET_URL), comments)
    return [c.kwargs["json"] for c in session.post.call_args_list]


def _post_bitbucket_server(comments):
    session = MagicMock()
    session.post.return_value = _mock_response(201, json_data={"id": 1})
    bitbucket_server.ForgeImpl(session=session).post_inline_comments(
        _ref(bitbucket_server, BITBUCKET_SERVER_URL), comments
    )
    return [c.kwargs["json"] for c in session.post.call_args_list]


def _post_azure(comments, monkeypatch):
    session = MagicMock(spec=requests.Session)
    session.post.return_value = _mock_response(201, json_data={"id": 1})
    forge = azure_devops.ForgeImpl(session=session)
    monkeypatch.setattr(forge, "_change_tracking", lambda ref: {})
    forge.post_inline_comments(_ref(azure_devops, AZURE_URL), comments)
    return [c.kwargs["json"] for c in session.post.call_args_list]


# --- no suggestion: byte-identical to before ---------------------------------


@pytest.mark.parametrize("style", ALL_STYLES)
def test_a_finding_without_a_suggestion_renders_the_old_body_for_every_forge(style):
    f = _finding()
    comment = _inline_comment(f, MODEL, style)
    assert comment == InlineComment(path="src/app.py", line=10, body=_base_body(f))
    assert comment.start_line is None
    assert _format_finding(f, MODEL, style) == _format_finding(f, MODEL) == _base_body(f)


def test_other_adapters_carry_no_suggestion_style():
    assert ALL_STYLES == ["github", "gitlab", None, None, None]


def test_github_payload_without_a_suggestion_is_unchanged():
    f = _finding()
    (payload,) = _post_github([_inline_comment(f, MODEL, "github")])
    assert payload == {
        "body": _base_body(f), "path": "src/app.py", "line": 10,
        "side": "RIGHT", "commit_id": "abc123",
    }


def test_gitlab_payload_without_a_suggestion_is_unchanged():
    f = _finding()
    (payload,) = _post_gitlab([_inline_comment(f, MODEL, "gitlab")])
    assert payload == {
        "body": _base_body(f),
        "position": {
            "base_sha": "sha_base", "start_sha": "sha_start", "head_sha": "sha_head",
            "position_type": "text", "new_path": "src/app.py", "new_line": 10,
        },
    }


def test_bitbucket_payload_without_a_suggestion_is_unchanged():
    f = _finding()
    (payload,) = _post_bitbucket([_inline_comment(f, MODEL, None)])
    assert payload == {"content": {"raw": _base_body(f)}, "inline": {"path": "src/app.py", "to": 10}}


def test_bitbucket_server_payload_without_a_suggestion_is_unchanged():
    f = _finding()
    (payload,) = _post_bitbucket_server([_inline_comment(f, MODEL, None)])
    assert payload["text"] == _base_body(f)
    assert payload["anchor"] == {"line": 10, "lineType": "ADDED", "fileType": "TO", "path": "src/app.py"}


def test_azure_payload_without_a_suggestion_is_unchanged(monkeypatch):
    f = _finding()
    (payload,) = _post_azure([_inline_comment(f, MODEL, None)], monkeypatch)
    assert payload == {
        "comments": [{"parentCommentId": 0, "content": _base_body(f), "commentType": 1}],
        "status": "active",
        "threadContext": {
            "filePath": "/src/app.py",
            "rightFileStart": {"line": 10, "offset": 1},
            "rightFileEnd": {"line": 10, "offset": 1},
        },
    }


def test_adapters_that_anchor_one_line_ignore_start_line(monkeypatch):
    plain = InlineComment(path="src/app.py", line=12, body="x")
    ranged = InlineComment(path="src/app.py", line=12, body="x", start_line=10)
    assert _post_gitlab([ranged]) == _post_gitlab([plain])
    assert _post_bitbucket([ranged]) == _post_bitbucket([plain])
    assert _post_bitbucket_server([ranged]) == _post_bitbucket_server([plain])
    assert _post_azure([ranged], monkeypatch) == _post_azure([plain], monkeypatch)


# --- GitHub ------------------------------------------------------------------


def test_github_single_line_suggestion_changes_only_the_body():
    f = _finding(suggestion="retry(limit=3)")
    comment = _inline_comment(f, MODEL, "github")
    assert comment.line == 10
    assert comment.start_line is None
    assert comment.body == _with_block(f, "```suggestion\nretry(limit=3)\n```")
    (payload,) = _post_github([comment])
    assert "start_line" not in payload
    assert "start_side" not in payload
    assert payload["line"] == 10


def test_github_multi_line_suggestion_posts_a_range_ending_at_the_last_line():
    f = _finding(suggestion="a = 1\nb = 2", end=13)
    comment = _inline_comment(f, MODEL, "github")
    assert (comment.start_line, comment.line) == (10, 13)
    assert comment.body == _with_block(f, "```suggestion\na = 1\nb = 2\n```")
    (payload,) = _post_github([comment])
    assert payload["start_line"] == 10
    assert payload["start_side"] == "RIGHT"
    assert payload["line"] == 13
    assert payload["side"] == "RIGHT"
    assert payload["body"] == comment.body


def test_github_delete_suggestion_is_an_empty_block():
    f = _finding(suggestion="", end=11)
    comment = _inline_comment(f, MODEL, "github")
    assert comment.body == _with_block(f, "```suggestion\n```")
    assert (comment.start_line, comment.line) == (10, 11)


# --- GitLab ------------------------------------------------------------------


def test_gitlab_suggestion_is_anchored_at_the_first_line_with_its_offset():
    f = _finding(suggestion="a = 1\nb = 2\nc = 3", end=12)
    comment = _inline_comment(f, MODEL, "gitlab")
    assert comment.line == 10
    assert comment.start_line is None
    assert comment.body == _with_block(f, "```suggestion:-0+2\na = 1\nb = 2\nc = 3\n```")
    (payload,) = _post_gitlab([comment])
    assert payload["position"]["new_line"] == 10


def test_gitlab_single_line_suggestion_uses_a_zero_offset():
    f = _finding(suggestion="x")
    assert format_suggestion_block(f, "gitlab") == "```suggestion:-0+0\nx\n```"
    assert format_suggestion_block(_finding(suggestion="", end=10), "gitlab") == "```suggestion:-0+0\n```"


# --- fallback ----------------------------------------------------------------


def test_fallback_single_line_is_a_labelled_copyable_block(monkeypatch):
    f = _finding(suggestion="retry(limit=3)")
    block = "**Suggested change** (line 10)\n\n```\nretry(limit=3)\n```"
    comment = _inline_comment(f, MODEL, None)
    assert comment == InlineComment(path="src/app.py", line=10, body=_with_block(f, block))
    assert _post_bitbucket([comment])[0]["content"]["raw"] == comment.body
    assert _post_bitbucket_server([comment])[0]["text"] == comment.body
    assert _post_azure([comment], monkeypatch)[0]["comments"][0]["content"] == comment.body


def test_fallback_multi_line_names_the_range():
    f = _finding(suggestion="a\nb", end=11)
    assert format_suggestion_block(f, None) == "**Suggested change** (lines 10\N{EN DASH}11)\n\n```\na\nb\n```"
    assert _inline_comment(f, MODEL, None).line == 10


def test_fallback_delete_wording_has_no_block():
    assert format_suggestion_block(_finding(suggestion=""), None) == "**Suggested change:** delete line 10"
    assert (
        format_suggestion_block(_finding(suggestion="", end=14), None)
        == "**Suggested change:** delete lines 10\N{EN DASH}14"
    )


# --- guards ------------------------------------------------------------------


@pytest.mark.parametrize(
    "f",
    [
        _finding(suggestion="x = 1\n```\ny = 2"),
        _finding(suggestion="x = 1", end=9),
        replace(_finding(suggestion="x = 1"), line=0),
    ],
    ids=["fence-in-text", "inverted-range", "file-level"],
)
@pytest.mark.parametrize("style", ["github", "gitlab", None])
def test_an_unrenderable_suggestion_posts_as_if_absent(f, style):
    assert suggestion_range(f) is None
    assert format_suggestion_block(f, style) == ""
    bare = replace(f, suggestion=None, suggestion_end_line=0)
    assert _inline_comment(f, MODEL, style) == _inline_comment(bare, MODEL, style)


# --- dedup after a multi-line suggestion ---------------------------------------


def _github_threads(items):
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(json_data=items)
    return github.ForgeImpl(session=session).list_threads(_ref(github, GITHUB_URL))


def test_github_thread_on_a_range_is_read_at_its_first_line():
    threads = _github_threads([
        {"id": 1, "path": "src/app.py", "start_line": 10, "line": 13,
         "user": {"login": "reviewer"}, "body": "unbounded"},
        {"id": 2, "path": "src/app.py", "start_line": None, "line": 20,
         "user": {"login": "reviewer"}, "body": "single"},
    ])
    assert [t.line for t in threads] == [10, 20]


def test_a_re_review_recognises_its_own_multi_line_suggestion_thread():
    f = _finding(suggestion="a\nb\nc\nd", end=13, body="Loop never stops.")
    comment = _inline_comment(f, MODEL, "github")
    posted_as = {
        "id": 1, "path": comment.path, "start_line": comment.start_line,
        "line": comment.line, "user": {"login": "prxref-bot"}, "body": "Unbounded",
    }
    threads = _github_threads([posted_as])
    (result,) = apply_thread_dedup([replace(f, suggestion=None, suggestion_end_line=0)], threads)
    assert result.drop_reason == "duplicate of existing thread"


# --- the review path wires the forge's style through --------------------------


class _StyledForge(FakeForge):
    suggestion_style = "github"


def _inject_suggestion(monkeypatch):
    real = orchestrator._inline_comment

    def spy(f, model, style):
        return real(replace(f, suggestion="fixed()", suggestion_end_line=f.line + 1), model, style)

    monkeypatch.setattr(orchestrator, "_inline_comment", spy)


FINDINGS = {
    "src/app.py": [
        {"file": "src/app.py", "line": 3, "severity": "error", "confidence": 0.9,
         "title": "Null deref", "body": "x may be None when config is missing; data loss follows."},
    ],
}


@pytest.mark.usefixtures("contract_stubs")
def test_review_uses_the_forge_suggestion_style(monkeypatch):
    _inject_suggestion(monkeypatch)
    forge = _StyledForge(diff=_added_file_diff("src/app.py", 20))
    orchestrate_review(forge, REF, FakeLLM(findings_by_path=FINDINGS))
    (comment,) = forge.inline_batches[0]
    assert (comment.start_line, comment.line) == (3, 4)
    assert "```suggestion\nfixed()\n```" in comment.body


@pytest.mark.usefixtures("contract_stubs")
def test_review_on_a_forge_without_a_style_uses_the_fallback(monkeypatch):
    _inject_suggestion(monkeypatch)
    forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
    orchestrate_review(forge, REF, FakeLLM(findings_by_path=FINDINGS))
    (comment,) = forge.inline_batches[0]
    assert comment.start_line is None
    assert comment.line == 3
    assert "**Suggested change** (lines 3\N{EN DASH}4)" in comment.body
