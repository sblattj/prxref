"""Tests for the Gitea / Forgejo forge adapter (#31).

Every request goes through a fake session that answers by method and URL, so
each test states the API it emulates as data. The response shapes follow a
Forgejo 11 (Gitea 1.22 API) instance: the issue-comment and review-comment
listings are not paged, the review listing is, ``new_position`` is a line of
the new file, and the compare endpoint answers JSON with no diff text.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import pytest
import requests

from prxref.config import make_forge
from prxref.forges import azure_devops, bitbucket, bitbucket_server, gitea, github, gitlab
from prxref.forges.base import (
    ATTRIBUTION_MARKER,
    MAX_LISTING_PAGES,
    SUMMARY_MARKER,
    FeedReadError,
    InlineComment,
    detect_forge,
)
from prxref.forges.gitea import ForgeImpl
from prxref.triage import parse_unified_diff

PR_URL = "https://codeberg.example.com/acme/api/pulls/42"
API = "https://codeberg.example.com/api/v1/repos/acme/api"
HEAD = "b" * 40
BASE = "a" * 40
MERGE_BASE = "c" * 40
MID = "d" * 40

GITEA_URLS = [
    "https://codeberg.example.com/acme/api/pulls/42",
    "https://codeberg.org/acme/api/pulls/7",
    "http://127.0.0.1:3000/acme/api/pulls/1",
    "https://example.com/git/acme/api/pulls/3",
    "https://git.example.com/acme/api/pulls/9/files",
    "https://git.example.com/acme/api/pulls/9#issuecomment-5",
]
OTHER_URLS = {
    "bitbucket": "https://bitbucket.org/acme/api/pull-requests/42",
    "bitbucket_server": "https://bitbucket.example.com/projects/PLAT/repos/api/pull-requests/42",
    "github": "https://github.com/acme/api/pull/42",
    "github_enterprise": "https://git.corp.example/acme/api/pull/7",
    "gitlab": "https://gitlab.com/acme/api/-/merge_requests/7",
    "gitlab_subgroup": "https://gitlab.example.com/group/sub/api/-/merge_requests/7",
    "azure_devops": "https://dev.azure.com/acme/example/_git/api/pullrequest/42",
    "azure_devops_server": "https://tfs.example.com/tfs/DefaultCollection/Proj/_git/Repo/pullrequest/7",
}
OLD_PARSERS = {
    "bitbucket": bitbucket.ForgeImpl.parse_pr_url,
    "bitbucket_server": bitbucket_server.ForgeImpl.parse_pr_url,
    "github": github.ForgeImpl.parse_pr_url,
    "gitlab": gitlab.ForgeImpl.parse_pr_url,
    "azure_devops": azure_devops.ForgeImpl.parse_pr_url,
}


class Resp:
    """A minimal ``requests.Response`` stand-in."""

    def __init__(self, status: int = 200, json_data: Any = None, text: str | None = None,
                 content: bytes | None = None) -> None:
        self.status_code = status
        self.ok = 200 <= status < 300
        self._json = json_data
        if text is None:
            text = json.dumps(json_data) if json_data is not None else ""
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no JSON")
        return self._json

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """Answers ``(method, url)`` from ``routes``; a value may be a callable of the params."""

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[dict] = []

    def _answer(self, method: str, url: str, **kwargs: Any) -> Resp:
        self.calls.append({"method": method, "url": url, **kwargs})
        route = self.routes.get((method, url))
        if route is None:
            return Resp(404, {"message": "not found"})
        if isinstance(route, Exception):
            raise route
        if callable(route):
            return route(kwargs.get("params") or {})
        return route

    def get(self, url, **kw):
        return self._answer("GET", url, **kw)

    def post(self, url, **kw):
        return self._answer("POST", url, **kw)

    def patch(self, url, **kw):
        return self._answer("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self._answer("DELETE", url, **kw)

    def sent(self, method: str) -> list[dict]:
        return [c for c in self.calls if c["method"] == method]


def _ref(url: str = PR_URL):
    ref = ForgeImpl.parse_pr_url(url)
    assert ref is not None
    return ref


def _forge(routes: dict | None = None) -> tuple[ForgeImpl, FakeSession]:
    session = FakeSession(routes)
    return ForgeImpl(session=session), session


PR_JSON = {
    "title": "Add refunds",
    "body": "the description",
    "user": {"login": "dev"},
    "head": {"ref": "feature", "sha": HEAD},
    "base": {"ref": "main", "sha": BASE},
    "merge_base": MERGE_BASE,
    "html_url": PR_URL,
}


@pytest.fixture(autouse=True)
def _no_token(monkeypatch):
    monkeypatch.delenv("PRXREF_GITEA_TOKEN", raising=False)


# --- URL parsing ------------------------------------------------------------


@pytest.mark.parametrize("url", GITEA_URLS)
def test_parse_pr_url_accepts_gitea_urls(url):
    ref = ForgeImpl.parse_pr_url(url)
    assert ref is not None
    assert (ref.forge, ref.owner, ref.repo) == ("gitea", "acme", "api")


def test_parse_pr_url_fields_and_normalized_url():
    ref = ForgeImpl.parse_pr_url("HTTP://Git.Example.com:3000/sub/acme/api/pulls/12/files?x=1#top")
    assert ref is not None
    assert (ref.host, ref.owner, ref.repo, ref.number) == ("Git.Example.com:3000", "acme", "api", 12)
    assert ref.url == "http://Git.Example.com:3000/sub/acme/api/pulls/12"
    assert ForgeImpl.parse_pr_url(ref.url) == ref


@pytest.mark.parametrize("url", [
    "https://codeberg.org/acme/api/pull/7",
    "https://codeberg.org/acme/api/pulls",
    "https://codeberg.org/acme/api/pulls/x",
    "https://codeberg.org/acme/pulls/7",
    "https://codeberg.org/acme/api/issues/7",
    "not-a-url",
    "https://api.github.com/repos/acme/api/pulls/42",
    "https://git.corp.example/api/v3/repos/acme/api/pulls/42",
    "https://codeberg.org/api/v1/repos/acme/api/pulls/7",
    "https://github.com/acme/api/pulls/42",
])
def test_parse_pr_url_rejects_non_gitea_urls(url):
    assert ForgeImpl.parse_pr_url(url) is None


@pytest.mark.parametrize("name", sorted(OTHER_URLS))
def test_gitea_parser_refuses_every_other_forges_urls(name):
    assert ForgeImpl.parse_pr_url(OTHER_URLS[name]) is None


@pytest.mark.parametrize("url", GITEA_URLS)
@pytest.mark.parametrize("parser", sorted(OLD_PARSERS))
def test_every_other_parser_refuses_gitea_urls(parser, url):
    assert OLD_PARSERS[parser](url) is None


@pytest.mark.parametrize("name", sorted(OTHER_URLS))
def test_detect_forge_still_routes_other_forges_as_before(name):
    ref = detect_forge(OTHER_URLS[name])
    assert ref is not None
    assert ref.forge != "gitea"


@pytest.mark.parametrize("url", GITEA_URLS)
def test_detect_forge_routes_gitea_urls(url):
    ref = detect_forge(url)
    assert ref is not None and ref.forge == "gitea"


def test_make_forge_resolves_gitea():
    session = FakeSession()
    forge = make_forge(_ref(), session=session)
    assert isinstance(forge, gitea.ForgeImpl)
    assert forge.name == "gitea"
    assert forge.session is session


def test_suggestion_style_is_the_plain_fenced_fallback():
    from prxref.formatter import SUGGESTION_STYLE_GITHUB, SUGGESTION_STYLE_GITLAB
    assert ForgeImpl.suggestion_style not in (SUGGESTION_STYLE_GITHUB, SUGGESTION_STYLE_GITLAB)


def test_no_pr_history_method():
    assert not hasattr(ForgeImpl, "get_pr_history")


# --- API base and auth ------------------------------------------------------


def test_api_base_keeps_scheme_and_sub_path():
    forge, session = _forge({("GET", "http://h.example:3000/git/api/v1/repos/acme/api/pulls/3"): Resp(200, PR_JSON)})
    forge.get_pr(_ref("http://h.example:3000/git/acme/api/pulls/3"))
    assert session.calls[0]["url"] == "http://h.example:3000/git/api/v1/repos/acme/api/pulls/3"


def test_no_token_sends_no_authorization():
    forge, session = _forge({("GET", f"{API}/pulls/42"): Resp(200, PR_JSON)})
    forge.get_pr(_ref())
    assert "Authorization" not in session.calls[0]["headers"]


def test_token_is_sent_as_token_scheme(monkeypatch):
    monkeypatch.setenv("PRXREF_GITEA_TOKEN", "t0k")
    forge, session = _forge({("GET", f"{API}/pulls/42"): Resp(200, PR_JSON)})
    forge.get_pr(_ref())
    assert session.calls[0]["headers"]["Authorization"] == "token t0k"


def test_every_request_carries_a_timeout(monkeypatch):
    forge, session = _forge({("GET", f"{API}/pulls/42"): Resp(200, PR_JSON),
                             ("GET", f"{API}/pulls/42.diff"): Resp(200, text="diff")})
    forge.get_pr(_ref())
    forge.get_diff(_ref())
    assert all(c.get("timeout") == gitea._REQUEST_TIMEOUT for c in session.calls)


# --- get_pr / get_diff ------------------------------------------------------


def test_get_pr_normalizes():
    forge, _ = _forge({("GET", f"{API}/pulls/42"): Resp(200, PR_JSON)})
    pr = forge.get_pr(_ref())
    assert (pr.title, pr.description, pr.author) == ("Add refunds", "the description", "dev")
    assert (pr.source_branch, pr.target_branch, pr.source_sha, pr.target_sha) == ("feature", "main", HEAD, BASE)
    assert pr.raw["merge_base"] == MERGE_BASE


def test_get_pr_raises_on_http_error():
    forge, _ = _forge({("GET", f"{API}/pulls/42"): Resp(404, {"message": "nope"})})
    with pytest.raises(requests.HTTPError):
        forge.get_pr(_ref())


def test_get_diff_returns_the_text_verbatim():
    text = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    forge, _ = _forge({("GET", f"{API}/pulls/42.diff"): Resp(200, text=text)})
    assert forge.get_diff(_ref()) == text


def test_get_diff_raises_on_http_error():
    forge, _ = _forge({("GET", f"{API}/pulls/42.diff"): Resp(500, text="boom")})
    with pytest.raises(requests.HTTPError):
        forge.get_diff(_ref())


# --- summary ----------------------------------------------------------------


def _comments(*bodies: str) -> Resp:
    return Resp(200, [{"id": 100 + i, "body": b} for i, b in enumerate(bodies)])


def test_post_summary_creates_when_none_exists():
    forge, session = _forge({
        ("GET", f"{API}/issues/42/comments"): _comments("hello"),
        ("POST", f"{API}/issues/42/comments"): Resp(201, {"id": 7}),
    })
    forge.post_summary(_ref(), "the summary")
    [post] = session.sent("POST")
    assert post["json"]["body"] == f"{SUMMARY_MARKER}\nthe summary"
    assert session.sent("PATCH") == []


def test_post_summary_updates_in_place():
    forge, session = _forge({
        ("GET", f"{API}/issues/42/comments"): _comments("hello", f"{SUMMARY_MARKER}\nold"),
        ("PATCH", f"{API}/issues/comments/101"): Resp(200, {"id": 101}),
    })
    forge.post_summary(_ref(), "new")
    [patch] = session.sent("PATCH")
    assert patch["json"]["body"] == f"{SUMMARY_MARKER}\nnew"
    assert session.sent("POST") == []


def test_summary_listing_is_read_once_without_paging():
    forge, session = _forge({("GET", f"{API}/issues/42/comments"): _comments(*["x"] * 120)})
    assert forge.get_summary(_ref()) is None
    [call] = session.sent("GET")
    assert not call.get("params")


def test_get_summary_returns_the_marked_body():
    forge, session = _forge({("GET", f"{API}/issues/42/comments"): _comments("a", f"{SUMMARY_MARKER}\nbody")})
    assert forge.get_summary(_ref()) == f"{SUMMARY_MARKER}\nbody"
    assert {c["method"] for c in session.calls} == {"GET"}


@pytest.mark.parametrize("answer", [
    Resp(500, text="boom"),
    Resp(200, text="not json"),
    Resp(200, {"not": "a list"}),
    requests.ConnectionError("down"),
])
def test_a_failed_summary_lookup_raises_and_never_posts(answer):
    forge, session = _forge({("GET", f"{API}/issues/42/comments"): answer})
    with pytest.raises(FeedReadError):
        forge.post_summary(_ref(), "s")
    assert session.sent("POST") == [] and session.sent("PATCH") == []
    with pytest.raises(FeedReadError):
        forge.get_summary(_ref())


def test_post_summary_raises_on_a_refused_write():
    forge, _ = _forge({
        ("GET", f"{API}/issues/42/comments"): _comments(),
        ("POST", f"{API}/issues/42/comments"): Resp(403, {"message": "forbidden"}),
    })
    with pytest.raises(requests.HTTPError):
        forge.post_summary(_ref(), "s")


# --- inline comments --------------------------------------------------------


def test_post_inline_comments_sends_one_review_with_new_positions():
    forge, session = _forge({
        ("GET", f"{API}/pulls/42"): Resp(200, PR_JSON),
        ("POST", f"{API}/pulls/42/reviews"): Resp(200, {"id": 5}),
    })
    posted = forge.post_inline_comments(_ref(), [
        InlineComment(path="app.py", line=21, body="one"),
        InlineComment(path="src/new.py", line=2, body="two", start_line=1),
        InlineComment(path="old.py", line=4, body="three", side="LEFT"),
    ])
    assert posted == 3
    [post] = session.sent("POST")
    assert post["json"]["event"] == "COMMENT"
    assert post["json"]["commit_id"] == HEAD
    assert post["json"]["body"] == ""
    assert post["json"]["comments"] == [
        {"path": "app.py", "body": "one", "new_position": 21, "old_position": 0},
        {"path": "src/new.py", "body": "two", "new_position": 2, "old_position": 0},
        {"path": "old.py", "body": "three", "new_position": 0, "old_position": 4},
    ]


def test_post_inline_comments_with_nothing_makes_no_request():
    forge, session = _forge()
    assert forge.post_inline_comments(_ref(), []) == 0
    assert session.calls == []


def test_post_inline_comments_logs_and_raises_a_refusal(caplog):
    forge, _ = _forge({
        ("GET", f"{API}/pulls/42"): Resp(200, PR_JSON),
        ("POST", f"{API}/pulls/42/reviews"): Resp(422, {"message": "bad review"}),
    })
    with caplog.at_level(logging.WARNING, logger="prxref.forges.gitea"):
        with pytest.raises(requests.HTTPError):
            forge.post_inline_comments(_ref(), [InlineComment(path="a", line=1, body="x")])
    assert "bad review" in caplog.text


# --- threads and prune ------------------------------------------------------


def _paged(pages: list[list[dict]]):
    def answer(params):
        page = int(params.get("page", 1))
        assert int(params.get("limit")) == gitea._PAGE_SIZE
        return Resp(200, pages[page - 1] if page <= len(pages) else [])
    return answer


def _review_routes(reviews: dict[int, tuple[str, list[dict]]], pages: list[list[int]] | None = None) -> dict:
    ids = list(reviews)
    pages = pages or [ids]
    routes: dict = {
        ("GET", f"{API}/pulls/42/reviews"): _paged(
            [[{"id": i, "body": reviews[i][0], "comments_count": len(reviews[i][1])} for i in page] for page in pages]
        ),
    }
    for rid, (_, comments) in reviews.items():
        routes[("GET", f"{API}/pulls/42/reviews/{rid}/comments")] = Resp(200, comments)
    return routes


def _c(cid: int, path: str, position: int, body: str, resolver: dict | None = None) -> dict:
    return {"id": cid, "path": path, "position": position, "original_position": 0, "body": body,
            "user": {"login": "prx"}, "resolver": resolver}


def test_list_threads_reads_every_review_comment():
    forge, _ = _forge(_review_routes({
        1: ("", [_c(10, "app.py", 21, "finding"), _c(11, "old.py", 0, "old side")]),
        2: ("looks good", []),
        3: ("", [_c(12, "b.py", 3, "resolved one", resolver={"login": "dev"})]),
    }))
    threads = forge.list_threads(_ref())
    assert [(t.path, t.line, t.resolved, t.author, t.body_snippet) for t in threads] == [
        ("app.py", 21, False, "prx", "finding"),
        ("old.py", None, False, "prx", "old side"),
        ("b.py", 3, True, "prx", "resolved one"),
    ]


_LONG_HUMAN_WONT_FIX = "Looks odd at first. " * 12 + "Won't fix: intentional."
_LONG_PRXREF_BY_DESIGN = (
    "By design. Drain duration should come from config. " + "y " * 150
    + f"\n\n*{ATTRIBUTION_MARKER} · model=m*"
)


@pytest.mark.parametrize(("body", "wont_fix"), [
    (_LONG_HUMAN_WONT_FIX, True),
    (_LONG_PRXREF_BY_DESIGN, False),
    ("finding", False),
], ids=["human-decline-past-the-cap", "prxref-body", "plain"])
def test_list_threads_reads_wont_fix_from_the_full_body(body, wont_fix):
    forge, _ = _forge(_review_routes({
        1: ("", [_c(10, "app.py", 21, body, resolver={"login": "dev"})]),
    }))

    [thread] = forge.list_threads(_ref())

    assert thread.wont_fix is wont_fix


def test_list_threads_skips_the_comment_read_of_an_empty_review():
    forge, session = _forge(_review_routes({2: ("body only", [])}))
    assert forge.list_threads(_ref()) == []
    assert all("/reviews/2/comments" not in c["url"] for c in session.calls)


def test_review_walk_pages_until_an_empty_page():
    reviews = {i: ("", [_c(100 + i, "a.py", i, "x")]) for i in range(1, 4)}
    forge, session = _forge(_review_routes(reviews, pages=[[1, 2], [3]]))
    assert len(forge.list_threads(_ref())) == 3
    pages = [c["params"]["page"] for c in session.calls if c["url"] == f"{API}/pulls/42/reviews"]
    assert pages == [1, 2, 3]


def test_review_walk_budget_is_a_ceiling(caplog):
    forge, session = _forge({
        ("GET", f"{API}/pulls/42/reviews"): lambda params: Resp(200, [{"id": 1, "comments_count": 0}]),
    })
    with caplog.at_level(logging.WARNING, logger="prxref.forges.gitea"):
        threads = forge.list_threads(_ref())
    assert threads == []
    assert len(session.calls) == gitea._MAX_PAGES
    assert "budget" in caplog.text


def test_list_threads_keeps_what_it_read_when_a_page_fails(caplog):
    def answer(params):
        if int(params["page"]) == 1:
            return Resp(200, [{"id": 1, "comments_count": 1}])
        return Resp(502, text="bad gateway")
    routes = {("GET", f"{API}/pulls/42/reviews"): answer,
              ("GET", f"{API}/pulls/42/reviews/1/comments"): Resp(200, [_c(10, "a.py", 1, "x")])}
    forge, _ = _forge(routes)
    with caplog.at_level(logging.WARNING, logger="prxref.forges.gitea"):
        threads = forge.list_threads(_ref())
    assert len(threads) == 1
    assert "incomplete" in caplog.text


MINE = f"finding\n\n{ATTRIBUTION_MARKER} model=m"


def test_prune_deletes_a_wholly_attributed_review_whole():
    routes = _review_routes({1: ("", [_c(10, "a.py", 1, MINE), _c(11, "b.py", 2, MINE)])})
    routes[("DELETE", f"{API}/pulls/42/reviews/1")] = Resp(204)
    forge, session = _forge(routes)
    assert forge.prune_inline_comments(_ref()) == 2
    assert [c["url"] for c in session.sent("DELETE")] == [f"{API}/pulls/42/reviews/1"]


def test_prune_never_deletes_a_review_holding_a_human_comment():
    routes = _review_routes({1: ("", [_c(10, "a.py", 1, MINE), _c(11, "a.py", 2, "a human note")])})
    routes[("DELETE", f"{API}/pulls/42/reviews/1/comments/10")] = Resp(204)
    forge, session = _forge(routes)
    assert forge.prune_inline_comments(_ref()) == 1
    assert [c["url"] for c in session.sent("DELETE")] == [f"{API}/pulls/42/reviews/1/comments/10"]


def test_prune_never_deletes_a_review_whose_body_is_a_humans():
    routes = _review_routes({1: ("please fix", [_c(10, "a.py", 1, MINE)])})
    routes[("DELETE", f"{API}/pulls/42/reviews/1/comments/10")] = Resp(204)
    forge, session = _forge(routes)
    assert forge.prune_inline_comments(_ref()) == 1
    assert [c["url"] for c in session.sent("DELETE")] == [f"{API}/pulls/42/reviews/1/comments/10"]


def test_prune_with_paths_keeps_other_paths():
    routes = _review_routes({1: ("", [_c(10, "a.py", 1, MINE), _c(11, "b.py", 2, MINE)])})
    routes[("DELETE", f"{API}/pulls/42/reviews/1/comments/11")] = Resp(204)
    forge, session = _forge(routes)
    assert forge.prune_inline_comments(_ref(), paths={"b.py"}) == 1
    assert [c["url"] for c in session.sent("DELETE")] == [f"{API}/pulls/42/reviews/1/comments/11"]


def test_prune_logs_a_refused_delete_and_carries_on(caplog):
    routes = _review_routes({
        1: ("", [_c(10, "a.py", 1, MINE), _c(11, "a.py", 2, "human")]),
        2: ("", [_c(20, "a.py", 3, MINE)]),
    })
    routes[("DELETE", f"{API}/pulls/42/reviews/1/comments/10")] = Resp(404, {"message": "no such route"})
    routes[("DELETE", f"{API}/pulls/42/reviews/2")] = Resp(204)
    forge, _ = _forge(routes)
    with caplog.at_level(logging.WARNING, logger="prxref.forges.gitea"):
        assert forge.prune_inline_comments(_ref()) == 1
    assert "could not prune inline comment 10" in caplog.text


def test_prune_reads_everything_before_deleting():
    routes = _review_routes({1: ("", [_c(10, "a.py", 1, MINE)]), 2: ("", [_c(20, "a.py", 2, MINE)])},
                            pages=[[1], [2]])
    routes[("DELETE", f"{API}/pulls/42/reviews/1")] = Resp(204)
    routes[("DELETE", f"{API}/pulls/42/reviews/2")] = Resp(204)
    forge, session = _forge(routes)
    assert forge.prune_inline_comments(_ref()) == 2
    methods = [c["method"] for c in session.calls]
    assert methods.index("DELETE") > max(i for i, m in enumerate(methods) if m == "GET")


def test_prune_on_an_unreadable_feed_deletes_nothing(caplog):
    forge, session = _forge({("GET", f"{API}/pulls/42/reviews"): Resp(500, text="x")})
    with caplog.at_level(logging.WARNING, logger="prxref.forges.gitea"):
        assert forge.prune_inline_comments(_ref()) == 0
    assert session.sent("DELETE") == []


# --- file content and paths -------------------------------------------------


def test_get_file_content_reads_raw_at_the_sha():
    forge, session = _forge({("GET", f"{API}/raw/src/a%20b.py"): Resp(200, text="x = 1\n")})
    assert forge.get_file_content(_ref(), "src/a b.py", sha=HEAD) == "x = 1\n"
    assert session.calls[0]["params"] == {"ref": HEAD}


@pytest.mark.parametrize("answer", [
    Resp(404, {"message": "not found"}),
    Resp(200, content=b"a\x00b", text=""),
    Resp(200, content=b"x" * (512 * 1024 + 1), text=""),
    requests.ConnectionError("down"),
])
def test_get_file_content_never_raises(answer):
    forge, _ = _forge({("GET", f"{API}/raw/a.py"): answer})
    assert forge.get_file_content(_ref(), "a.py", sha=HEAD) is None


def test_get_file_content_without_a_sha_makes_no_request():
    forge, session = _forge()
    assert forge.get_file_content(_ref(), "a.py", sha="") is None
    assert session.calls == []


def _tree_route(pages: list[tuple[list[dict], bool]]):
    def answer(params):
        assert params["recursive"] == "true"
        entries, truncated = pages[int(params["page"]) - 1]
        return Resp(200, {"tree": entries, "truncated": truncated, "page": int(params["page"])})
    return answer


def test_list_paths_pages_while_truncated_and_keeps_blobs():
    forge, session = _forge({("GET", f"{API}/git/trees/{HEAD}"): _tree_route([
        ([{"path": "b.py", "type": "blob"}, {"path": "src", "type": "tree"}], True),
        ([{"path": "src/a.py", "type": "blob"}, {"path": "b.py", "type": "blob"},
          {"path": "mod", "type": "commit"}], False),
    ])})
    listing = forge.list_paths(_ref(), sha=HEAD)
    assert listing.paths == ("b.py", "src/a.py")
    assert listing.complete is True
    assert [c["params"]["page"] for c in session.calls] == [1, 2]


def test_list_paths_stops_at_the_listing_ceiling_incomplete():
    forge, session = _forge({("GET", f"{API}/git/trees/{HEAD}"): lambda params: Resp(
        200, {"tree": [{"path": f"f{params['page']}", "type": "blob"}], "truncated": True})})
    listing = forge.list_paths(_ref(), sha=HEAD)
    assert listing.complete is False
    assert len(session.calls) == MAX_LISTING_PAGES


@pytest.mark.parametrize("answer", [
    Resp(404, {"message": "x"}), Resp(200, text="nope"), Resp(200, {"no": "tree"}), requests.ConnectionError("x"),
])
def test_list_paths_never_raises(answer):
    forge, _ = _forge({("GET", f"{API}/git/trees/{HEAD}"): answer})
    assert forge.list_paths(_ref(), sha=HEAD) is None


def test_list_paths_without_a_sha_makes_no_request():
    forge, session = _forge()
    assert forge.list_paths(_ref(), sha="") is None
    assert session.calls == []


# --- compare ----------------------------------------------------------------


OLD_APP = "".join(f"line {i}\n" for i in range(1, 41))
NEW_APP = OLD_APP.replace("line 20\n", "line 20 changed\nline 20b added\n")
COMPARE_URL = f"{API}/compare/{BASE}...{HEAD}"
TWO_COMMITS = [
    {"sha": HEAD, "parents": [{"sha": MID}]},
    {"sha": MID, "parents": [{"sha": MERGE_BASE}]},
]


def _compare_routes(files: list[dict], commits: list[dict] = TWO_COMMITS, blobs: dict | None = None) -> dict:
    blobs = blobs if blobs is not None else {
        ("app.py", MERGE_BASE): OLD_APP.encode(),
        ("app.py", HEAD): NEW_APP.encode(),
        ("src/new.py", HEAD): b"a = 1\n",
        ("old.py", MERGE_BASE): b"gone\n",
        ("docs/usage.md", MERGE_BASE): b"# Usage\n",
        ("docs/guide.md", HEAD): b"# Guide\n",
    }
    routes: dict = {("GET", COMPARE_URL): Resp(200, {"total_commits": len(commits), "commits": commits,
                                                    "files": files})}
    by_path: dict[str, dict[str, bytes]] = {}
    for (path, sha), data in blobs.items():
        by_path.setdefault(path, {})[sha] = data

    def raw(sides):
        return lambda params: Resp(200, content=sides[params["ref"]], text="") if params.get("ref") in sides \
            else Resp(404, {"message": "no"})
    for path, sides in by_path.items():
        routes[("GET", f"{API}/raw/{path}")] = raw(sides)
    return routes


def _shape(diff: str):
    return [(f.path, f.old_path, f.status, f.lines_added, f.lines_removed) for f in parse_unified_diff(diff)]


def test_compare_rebuilds_the_diff_from_the_merge_base():
    forge, session = _forge(_compare_routes([
        {"filename": "app.py", "status": "modified"},
        {"filename": "src/new.py", "status": "added"},
        {"filename": "old.py", "status": "removed"},
        {"filename": "docs/guide.md", "status": "renamed", "previous_filename": "docs/usage.md"},
    ]))
    diff = forge.get_compare_diff(_ref(), base_sha=BASE, head_sha=HEAD)
    assert _shape(diff) == [
        ("app.py", "app.py", "modified", 2, 1),
        ("src/new.py", None, "added", 1, 0),
        ("old.py", "old.py", "removed", 0, 1),
        ("docs/guide.md", "docs/usage.md", "renamed", 1, 1),
    ]
    raw_refs = {c["params"]["ref"] for c in session.calls if "/raw/" in c["url"]}
    assert raw_refs == {MERGE_BASE, HEAD}
    assert {c["method"] for c in session.calls} == {"GET"}


def test_compare_of_an_empty_range_is_empty():
    forge, _ = _forge({("GET", COMPARE_URL): Resp(200, {"total_commits": 0, "commits": [], "files": []})})
    assert forge.get_compare_diff(_ref(), base_sha=BASE, head_sha=HEAD) == ""


def test_compare_refuses_an_ambiguous_merge_base():
    commits = [{"sha": HEAD, "parents": [{"sha": MID}, {"sha": "e" * 40}]},
               {"sha": MID, "parents": [{"sha": MERGE_BASE}]}]
    forge, _ = _forge(_compare_routes([{"filename": "app.py", "status": "modified"}], commits=commits))
    with pytest.raises(ValueError, match="2 candidate merge bases"):
        forge.get_compare_diff(_ref(), base_sha=BASE, head_sha=HEAD)


def test_compare_refuses_a_partial_commit_listing():
    routes = _compare_routes([{"filename": "app.py", "status": "modified"}])
    routes[("GET", COMPARE_URL)] = Resp(200, {"total_commits": 5, "commits": TWO_COMMITS,
                                              "files": [{"filename": "app.py", "status": "modified"}]})
    forge, _ = _forge(routes)
    with pytest.raises(ValueError, match="2 of 5 commits"):
        forge.get_compare_diff(_ref(), base_sha=BASE, head_sha=HEAD)


def test_compare_renders_binary_and_oversize_files_header_only(caplog):
    blobs = {("img.png", MERGE_BASE): b"\x89PNG\x00", ("img.png", HEAD): b"\x89PNG\x00\x01",
             ("big.txt", MERGE_BASE): b"a\n", ("big.txt", HEAD): b"b\n" * (300 * 1024)}
    forge, _ = _forge(_compare_routes([{"filename": "img.png", "status": "modified"},
                                       {"filename": "big.txt", "status": "modified"}], blobs=blobs))
    with caplog.at_level(logging.WARNING, logger="prxref.forges.gitea"):
        diff = forge.get_compare_diff(_ref(), base_sha=BASE, head_sha=HEAD)
    assert _shape(diff) == [("img.png", "img.png", "modified", 0, 0), ("big.txt", "big.txt", "modified", 0, 0)]
    assert "2 file(s)" in caplog.text


def test_compare_raises_on_http_and_transport_errors():
    forge, _ = _forge({("GET", COMPARE_URL): Resp(404, {"message": "x"})})
    with pytest.raises(requests.HTTPError):
        forge.get_compare_diff(_ref(), base_sha=BASE, head_sha=HEAD)
    forge, _ = _forge({("GET", COMPARE_URL): requests.ConnectionError("down")})
    with pytest.raises(requests.ConnectionError):
        forge.get_compare_diff(_ref(), base_sha=BASE, head_sha=HEAD)
    routes = _compare_routes([{"filename": "app.py", "status": "modified"}], blobs={})
    forge, _ = _forge(routes)
    with pytest.raises(requests.HTTPError):
        forge.get_compare_diff(_ref(), base_sha=BASE, head_sha=HEAD)


# --- session ----------------------------------------------------------------


def test_default_session_retries_reads_only():
    session = gitea._create_default_session()
    retry = session.get_adapter("https://x").max_retries
    assert retry.allowed_methods == frozenset(["GET", "HEAD", "OPTIONS"])
    assert session.get_adapter("http://x").max_retries is retry
