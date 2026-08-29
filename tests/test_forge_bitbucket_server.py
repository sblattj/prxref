"""Tests for the Bitbucket Server / Data Center forge adapter."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests

from prxref.config import make_forge
from prxref.forges.base import InlineComment, PRRef, detect_forge
from prxref.forges.bitbucket_server import ForgeImpl
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


def _ref(url="https://bitbucket.corp.example/projects/PLAT/repos/api/pull-requests/42"):
    ref = ForgeImpl.parse_pr_url(url)
    assert ref is not None
    return ref


# --- URL parsing ------------------------------------------------------------


def test_parse_pr_url_project_repo():
    ref = ForgeImpl.parse_pr_url(
        "https://bitbucket.corp.example/projects/PLAT/repos/api/pull-requests/42"
    )
    assert ref is not None
    assert ref.forge == "bitbucket-server"
    assert ref.host == "bitbucket.corp.example"
    assert ref.owner == "PLAT"
    assert ref.repo == "api"
    assert ref.number == 42
    assert ref.url == (
        "https://bitbucket.corp.example/projects/PLAT/repos/api/pull-requests/42"
    )


def test_parse_pr_url_personal_repo_becomes_tilde_project_key():
    # A personal repo browses as /users/jdoe but is addressed as ~jdoe in the
    # API. Getting this wrong 404s every request for a personal-repo PR.
    ref = ForgeImpl.parse_pr_url(
        "https://bitbucket.corp.example/users/jdoe/repos/scratch/pull-requests/7"
    )
    assert ref is not None
    assert ref.owner == "~jdoe"
    assert ref.repo == "scratch"
    assert ref.url == (
        "https://bitbucket.corp.example/users/jdoe/repos/scratch/pull-requests/7"
    )


def test_parse_pr_url_with_deployment_context_path():
    # Data Center is commonly reverse-proxied under a context path.
    ref = ForgeImpl.parse_pr_url(
        "https://tools.corp.example/bitbucket/projects/PLAT/repos/api/pull-requests/9"
    )
    assert ref is not None
    assert ref.host == "tools.corp.example"
    assert ref.owner == "PLAT"
    assert ref.number == 9
    assert "/bitbucket/projects/PLAT/" in ref.url


def test_parse_pr_url_tolerates_trailing_route():
    ref = ForgeImpl.parse_pr_url(
        "https://bitbucket.corp.example/projects/PLAT/repos/api/pull-requests/42/overview"
    )
    assert ref is not None
    assert ref.number == 42
    assert ref.url.endswith("/pull-requests/42")


def test_parse_pr_url_preserves_a_plain_http_scheme():
    # Data Center's standalone install serves plain HTTP on :7990, so an
    # http:// PR URL is the out-of-the-box shape, not an edge case. The pattern
    # accepts the scheme, so normalization must keep it: rewriting it to https
    # points every later request at a TLS listener that is not there.
    ref = ForgeImpl.parse_pr_url(
        "http://bitbucket.internal:7990/projects/PLAT/repos/api/pull-requests/42"
    )
    assert ref is not None
    assert ref.host == "bitbucket.internal:7990"
    assert ref.url == (
        "http://bitbucket.internal:7990/projects/PLAT/repos/api/pull-requests/42"
    )
    # The normalized URL must parse back to itself, or the scheme is lost the
    # second time a PRRef is rebuilt from its own url.
    assert ForgeImpl.parse_pr_url(ref.url) == ref


def test_parse_pr_url_lowercases_an_uppercase_scheme():
    ref = ForgeImpl.parse_pr_url(
        "HTTP://bitbucket.corp.example/projects/PLAT/repos/api/pull-requests/42"
    )
    assert ref is not None
    assert ref.url.startswith("http://bitbucket.corp.example/")


def test_parse_pr_url_normalizes_a_rest_api_url_to_the_browse_url():
    # /rest/api/1.0 is a route, not a deployment context path. Captured as a
    # context it gets replayed into the API base, so every request path carries
    # /rest/api/1.0 twice and 404s.
    ref = ForgeImpl.parse_pr_url(
        "https://bitbucket.corp.example/rest/api/1.0/projects/PLAT/repos/api/pull-requests/42"
    )
    assert ref is not None
    assert ref.owner == "PLAT"
    assert ref.repo == "api"
    assert ref.number == 42
    assert ref.url == (
        "https://bitbucket.corp.example/projects/PLAT/repos/api/pull-requests/42"
    )


def test_parse_pr_url_keeps_the_context_when_a_rest_prefix_follows_it():
    # A reverse-proxied deployment's REST URL carries both: the context path is
    # real and must survive, the REST prefix is not and must not.
    ref = ForgeImpl.parse_pr_url(
        "https://tools.corp.example/bitbucket/rest/api/1.0/projects/PLAT/repos/api/pull-requests/9"
    )
    assert ref is not None
    assert ref.host == "tools.corp.example"
    assert ref.url == (
        "https://tools.corp.example/bitbucket/projects/PLAT/repos/api/pull-requests/9"
    )


@pytest.mark.parametrize("version", ["1.0", "latest", "LATEST", "2", "2.1"])
def test_parse_pr_url_strips_any_rest_api_version_alias(version):
    # Data Center serves the versioned path and the /latest alias alike, and the
    # surrounding pattern is case-insensitive.
    ref = ForgeImpl.parse_pr_url(
        f"https://bitbucket.corp.example/rest/api/{version}/projects/PLAT/repos/api/pull-requests/42"
    )
    assert ref is not None
    assert ref.url == (
        "https://bitbucket.corp.example/projects/PLAT/repos/api/pull-requests/42"
    )


def test_parse_pr_url_strips_a_rest_prefix_in_any_case():
    ref = ForgeImpl.parse_pr_url(
        "https://bitbucket.corp.example/REST/API/1.0/projects/PLAT/repos/api/pull-requests/42"
    )
    assert ref is not None
    assert ref.url == (
        "https://bitbucket.corp.example/projects/PLAT/repos/api/pull-requests/42"
    )


def test_parse_pr_url_rest_form_of_a_personal_repo_normalizes_to_the_users_route():
    # The API addresses a personal repo as the ~slug project key; it browses as
    # /users/slug. PRRef.url is the link a human clicks, so it gets the browse
    # form while owner keeps the API form.
    ref = ForgeImpl.parse_pr_url(
        "https://bitbucket.corp.example/rest/api/1.0/projects/~jdoe/repos/scratch/pull-requests/7"
    )
    assert ref is not None
    assert ref.owner == "~jdoe"
    assert ref.url == (
        "https://bitbucket.corp.example/users/jdoe/repos/scratch/pull-requests/7"
    )


def test_parse_pr_url_does_not_mistake_a_repo_named_rest_for_the_api_prefix():
    # Only a genuine /rest/api/<version> tail is stripped.
    ref = ForgeImpl.parse_pr_url(
        "https://bitbucket.corp.example/rest/projects/PLAT/repos/api/pull-requests/42"
    )
    assert ref is not None
    assert ref.url == (
        "https://bitbucket.corp.example/rest/projects/PLAT/repos/api/pull-requests/42"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://bitbucket.org/team/repo/pull-requests/1",  # Cloud, not Server
        "https://github.com/o/r/pull/1",
        "https://gitlab.com/g/p/-/merge_requests/1",
        "https://bitbucket.corp.example/projects/PLAT/repos/api/pull-requests/abc",
        "https://bitbucket.corp.example/projects/PLAT/repos/api",
        "not-a-url",
        "",
    ],
)
def test_parse_pr_url_rejects_foreign_urls(url):
    assert ForgeImpl.parse_pr_url(url) is None


def test_cloud_url_is_not_claimed_by_server():
    # Ordering guard: bitbucket.org must resolve to the Cloud adapter even
    # though the Server pattern matches any host.
    ref = detect_forge("https://bitbucket.org/team/repo/pull-requests/1")
    assert ref is not None
    assert ref.forge == "bitbucket"


def test_detect_forge_routes_server_urls_here():
    ref = detect_forge(
        "https://bitbucket.corp.example/projects/PLAT/repos/api/pull-requests/42"
    )
    assert ref is not None
    assert ref.forge == "bitbucket-server"


def test_make_forge_builds_the_server_adapter():
    forge = make_forge(_ref(), session=MagicMock())
    assert isinstance(forge, ForgeImpl)
    assert forge.name == "bitbucket-server"


# --- auth -------------------------------------------------------------------


def test_bearer_token_prefers_the_server_specific_variable(monkeypatch):
    monkeypatch.setenv("PRXREF_BITBUCKET_SERVER_TOKEN", "dc-token")
    monkeypatch.setenv("PRXREF_BITBUCKET_TOKEN", "cloud-token")
    headers, auth = ForgeImpl()._get_auth()
    assert headers == {"Authorization": "Bearer dc-token"}
    assert auth is None


def test_bearer_token_falls_back_to_the_cloud_variable(monkeypatch):
    monkeypatch.delenv("PRXREF_BITBUCKET_SERVER_TOKEN", raising=False)
    monkeypatch.setenv("PRXREF_BITBUCKET_TOKEN", "cloud-token")
    headers, auth = ForgeImpl()._get_auth()
    assert headers == {"Authorization": "Bearer cloud-token"}


def test_basic_auth_when_no_token(monkeypatch):
    for var in ("PRXREF_BITBUCKET_SERVER_TOKEN", "PRXREF_BITBUCKET_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PRXREF_BITBUCKET_SERVER_USER", "svc")
    monkeypatch.setenv("PRXREF_BITBUCKET_SERVER_PASSWORD", "pw")
    headers, auth = ForgeImpl()._get_auth()
    assert headers == {}
    assert auth == ("svc", "pw")


# --- API paths --------------------------------------------------------------


def test_api_path_uses_rest_v1_and_the_project_key():
    session = MagicMock()
    session.get.return_value = _mock_response(json_data={})
    ForgeImpl(session=session).get_pr(_ref())
    url = session.get.call_args[0][0]
    assert url == (
        "https://bitbucket.corp.example/rest/api/1.0"
        "/projects/PLAT/repos/api/pull-requests/42"
    )


def test_api_path_preserves_the_context_path():
    session = MagicMock()
    session.get.return_value = _mock_response(json_data={})
    ref = _ref("https://tools.corp.example/bitbucket/projects/PLAT/repos/api/pull-requests/9")
    ForgeImpl(session=session).get_pr(ref)
    assert session.get.call_args[0][0].startswith(
        "https://tools.corp.example/bitbucket/rest/api/1.0/"
    )


def test_api_path_preserves_a_plain_http_scheme():
    # The scheme parsed off the PR URL has to reach the API base. Hardcoding
    # https here sends every request to a TLS port an http-only deployment is
    # not listening on, and the failure names a URL the operator never typed.
    session = MagicMock()
    session.get.return_value = _mock_response(json_data={})
    ref = _ref("http://bitbucket.internal:7990/projects/PLAT/repos/api/pull-requests/42")
    ForgeImpl(session=session).get_pr(ref)
    assert session.get.call_args[0][0] == (
        "http://bitbucket.internal:7990/rest/api/1.0"
        "/projects/PLAT/repos/api/pull-requests/42"
    )


def test_api_path_over_http_keeps_the_context_path_and_the_rest_stripping():
    session = MagicMock()
    session.get.return_value = _mock_response(json_data={})
    ref = _ref(
        "http://tools.internal:7990/bitbucket/rest/api/latest"
        "/projects/PLAT/repos/api/pull-requests/9"
    )
    ForgeImpl(session=session).get_pr(ref)
    url = session.get.call_args[0][0]
    assert url == (
        "http://tools.internal:7990/bitbucket/rest/api/1.0"
        "/projects/PLAT/repos/api/pull-requests/9"
    )
    assert url.count("/rest/api/") == 1


def test_api_path_for_a_personal_repo():
    session = MagicMock()
    session.get.return_value = _mock_response(json_data={})
    ref = _ref("https://bitbucket.corp.example/users/jdoe/repos/scratch/pull-requests/7")
    ForgeImpl(session=session).get_pr(ref)
    assert "/projects/~jdoe/repos/scratch/" in session.get.call_args[0][0]


def test_api_path_is_not_doubled_for_a_rest_form_url():
    # Pasting a PR's REST URL into `prxref review` must not yield
    # /rest/api/1.0/rest/api/1.0/projects/...
    session = MagicMock()
    session.get.return_value = _mock_response(json_data={})
    ref = _ref(
        "https://bitbucket.corp.example/rest/api/1.0/projects/PLAT/repos/api/pull-requests/42"
    )
    ForgeImpl(session=session).get_pr(ref)
    url = session.get.call_args[0][0]
    assert url == (
        "https://bitbucket.corp.example/rest/api/1.0"
        "/projects/PLAT/repos/api/pull-requests/42"
    )
    assert url.count("/rest/api/") == 1


def test_api_path_from_a_rest_form_url_replays_only_the_deployment_context():
    session = MagicMock()
    session.get.return_value = _mock_response(json_data={})
    ref = _ref(
        "https://tools.corp.example/bitbucket/rest/api/latest/projects/PLAT/repos/api/pull-requests/9"
    )
    ForgeImpl(session=session).get_pr(ref)
    url = session.get.call_args[0][0]
    assert url == (
        "https://tools.corp.example/bitbucket/rest/api/1.0"
        "/projects/PLAT/repos/api/pull-requests/9"
    )
    assert url.count("/rest/api/") == 1


# --- get_pr -----------------------------------------------------------------


def test_get_pr_normalizes_data_center_fields():
    payload = {
        "title": "Add retry",
        "description": "why",
        "author": {"user": {"name": "jdoe", "displayName": "J Doe"}},
        "fromRef": {"displayId": "feature/x", "latestCommit": "aaa111"},
        "toRef": {"displayId": "main", "latestCommit": "bbb222"},
    }
    session = MagicMock()
    session.get.return_value = _mock_response(json_data=payload)

    data = ForgeImpl(session=session).get_pr(_ref())
    assert data.title == "Add retry"
    assert data.description == "why"
    assert data.author == "jdoe"
    assert data.source_branch == "feature/x"
    assert data.target_branch == "main"
    assert data.source_sha == "aaa111"
    assert data.target_sha == "bbb222"
    assert data.raw is payload


def test_get_pr_tolerates_a_sparse_payload():
    session = MagicMock()
    session.get.return_value = _mock_response(json_data={})
    data = ForgeImpl(session=session).get_pr(_ref())
    assert data.title == ""
    assert data.author == ""
    assert data.source_sha == ""


# --- get_diff ---------------------------------------------------------------


DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
 import os
+import sys

 def main():
"""


def test_get_diff_uses_the_dot_diff_suffix_and_parses():
    session = MagicMock()
    session.get.return_value = _mock_response(text=DIFF)
    diff = ForgeImpl(session=session).get_diff(_ref())

    assert session.get.call_args[0][0].endswith("/pull-requests/42.diff")
    files = parse_unified_diff(diff)
    assert len(files) == 1
    assert files[0].new_path == "src/app.py"


def test_get_diff_rejects_an_empty_body():
    session = MagicMock()
    session.get.return_value = _mock_response(text="   ")
    with pytest.raises(ValueError, match="Empty or truncated diff"):
        ForgeImpl(session=session).get_diff(_ref())


# --- comments ---------------------------------------------------------------


def test_post_inline_comment_anchors_to_the_new_file():
    session = MagicMock()
    session.post.return_value = _mock_response(status_code=201, json_data={"id": 1})

    posted = ForgeImpl(session=session).post_inline_comments(
        _ref(), [InlineComment(path="src/app.py", line=12, body="nit")]
    )
    assert posted == 1
    payload = session.post.call_args.kwargs["json"]
    assert payload["text"] == "nit"
    assert payload["anchor"] == {
        "line": 12,
        "lineType": "ADDED",
        "fileType": "TO",
        "path": "src/app.py",
    }


def test_post_inline_comments_skips_4xx_and_keeps_going():
    session = MagicMock()
    session.post.side_effect = [
        _mock_response(status_code=400),
        _mock_response(status_code=201, json_data={"id": 2}),
    ]
    posted = ForgeImpl(session=session).post_inline_comments(
        _ref(),
        [
            InlineComment(path="a.py", line=1, body="x"),
            InlineComment(path="b.py", line=2, body="y"),
        ],
    )
    assert posted == 1


def test_post_inline_comments_no_op_on_empty_list():
    session = MagicMock()
    assert ForgeImpl(session=session).post_inline_comments(_ref(), []) == 0
    session.post.assert_not_called()


def test_post_summary_creates_when_absent():
    session = MagicMock()
    session.get.return_value = _mock_response(
        json_data={"values": [], "isLastPage": True}
    )
    session.post.return_value = _mock_response(status_code=201, json_data={"id": 5})

    ForgeImpl(session=session).post_summary(_ref(), "summary body")
    assert session.post.call_args.kwargs["json"] == {"text": "summary body"}
    session.put.assert_not_called()


def test_post_summary_updates_and_sends_the_version():
    # Data Center rejects a comment update that omits the current version.
    session = MagicMock()
    session.get.return_value = _mock_response(
        json_data={
            "values": [
                {
                    "action": "COMMENTED",
                    "comment": {
                        "id": 77,
                        "version": 3,
                        "text": "old <!-- prxref-summary -->",
                    },
                }
            ],
            "isLastPage": True,
        }
    )
    session.put.return_value = _mock_response(json_data={"id": 77})

    ForgeImpl(session=session).post_summary(_ref(), "new body")
    session.post.assert_not_called()
    assert session.put.call_args[0][0].endswith("/comments/77")
    assert session.put.call_args.kwargs["json"] == {"text": "new body", "version": 3}


def test_post_summary_ignores_an_inline_comment_carrying_the_marker():
    session = MagicMock()
    session.get.return_value = _mock_response(
        json_data={
            "values": [
                {
                    "action": "COMMENTED",
                    "comment": {
                        "id": 9,
                        "version": 1,
                        "text": "quoted <!-- prxref-summary -->",
                        "anchor": {"path": "a.py", "line": 3},
                    },
                }
            ],
            "isLastPage": True,
        }
    )
    session.post.return_value = _mock_response(status_code=201, json_data={"id": 10})

    ForgeImpl(session=session).post_summary(_ref(), "body")
    session.put.assert_not_called()
    session.post.assert_called_once()


# --- threads ----------------------------------------------------------------


def test_list_threads_reads_the_activity_feed():
    session = MagicMock()
    session.get.return_value = _mock_response(
        json_data={
            "values": [
                {
                    "action": "COMMENTED",
                    "comment": {
                        "text": "please fix",
                        "author": {"name": "reviewer"},
                        "anchor": {"path": "src/app.py", "line": 12},
                        "state": "OPEN",
                    },
                },
                {"action": "APPROVED", "user": {"name": "someone"}},
                {
                    "action": "COMMENTED",
                    "comment": {
                        "text": "done",
                        "author": {"name": "author"},
                        "anchor": {"path": "src/app.py", "line": 30},
                        "state": "RESOLVED",
                    },
                },
            ],
            "isLastPage": True,
        }
    )

    threads = ForgeImpl(session=session).list_threads(_ref())
    assert len(threads) == 2  # the APPROVED activity is not a thread
    assert threads[0].path == "src/app.py"
    assert threads[0].line == 12
    assert threads[0].author == "reviewer"
    assert threads[0].resolved is False
    assert threads[1].resolved is True


def test_list_threads_follows_start_limit_pagination():
    session = MagicMock()
    session.get.side_effect = [
        _mock_response(
            json_data={
                "values": [{"action": "COMMENTED", "comment": {"text": "one"}}],
                "isLastPage": False,
                "nextPageStart": 100,
            }
        ),
        _mock_response(
            json_data={
                "values": [{"action": "COMMENTED", "comment": {"text": "two"}}],
                "isLastPage": True,
            }
        ),
    ]

    threads = ForgeImpl(session=session).list_threads(_ref())
    assert [t.body_snippet for t in threads] == ["one", "two"]
    assert session.get.call_args_list[1].kwargs["params"]["start"] == 100


def test_list_threads_returns_empty_on_transport_error():
    session = MagicMock()
    session.get.side_effect = requests.ConnectionError("down")
    assert ForgeImpl(session=session).list_threads(_ref()) == []


@pytest.mark.parametrize(
    "comment,expected",
    [
        ({"state": "RESOLVED"}, True),
        ({"threadResolved": True}, True),
        ({"resolvedDate": 1700000000}, True),
        ({"state": "OPEN"}, False),
        ({}, False),
    ],
)
def test_resolution_is_read_from_any_of_the_three_shapes(comment, expected):
    session = MagicMock()
    session.get.return_value = _mock_response(
        json_data={
            "values": [{"action": "COMMENTED", "comment": {"text": "x", **comment}}],
            "isLastPage": True,
        }
    )
    threads = ForgeImpl(session=session).list_threads(_ref())
    assert threads[0].resolved is expected


def test_ref_round_trips_through_the_protocol_dataclass():
    ref = _ref()
    assert isinstance(ref, PRRef)
    assert ForgeImpl.parse_pr_url(ref.url) == ref
