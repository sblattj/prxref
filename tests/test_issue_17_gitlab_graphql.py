"""Tests for GitLab's GraphQL-first ``list_paths`` (issue #17).

The REST ``repository/tree?recursive=true`` listing returns every directory
before any file, across the whole recursive walk, so under the page cap a large
project loses files: a live check against gitlab.com saw gitlab-org/gitlab's
first 21 pages come back 100% ``tree`` and ``list_paths`` return zero paths.
The adapter now walks GraphQL's files-only ``tree.blobs`` connection first and
falls back to the unchanged REST walk when the first GraphQL page is unusable.
The response shapes below are the ones that live check against gitlab.com
observed.
"""
from __future__ import annotations

import copy
import json
import logging
import re

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from prxref.forges import gitlab
from prxref.forges.base import PathListing

SHA = "1234567890abcdef1234567890abcdef12345678"
REQUEST_TIMEOUT = (10.0, 30.0)

GL_PR_URL = "https://gitlab.example.com/acme/platform/api/-/merge_requests/7"
PROJECT = "acme/platform/api"
GRAPHQL_URL = "https://gitlab.example.com/api/graphql"
TREE_URL = "https://gitlab.example.com/api/v4/projects/acme%2Fplatform%2Fapi/repository/tree"

LIVE_QUERY = (
    "query($p: ID!, $ref: String!, $after: String) { project(fullPath: $p) { repository { "
    "tree(ref: $ref, recursive: true) { blobs(first: 100, after: $after) { "
    "pageInfo { hasNextPage endCursor } nodes { path type mode } } } } } }"
)
LIVE_EMPTY_PAGE = {
    "data": {"project": {"repository": {"tree": {"blobs": {
        "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [],
    }}}}},
}
LIVE_UNKNOWN_PROJECT = {"data": {"project": None}}

_NOT_JSON = object()


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch):
    monkeypatch.delenv("PRXREF_GITLAB_TOKEN", raising=False)


class FakeResponse:
    """The slice of ``requests.Response`` the adapter reads."""

    def __init__(self, status_code=200, *, json_data=_NOT_JSON, text="", headers=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.headers = headers if headers is not None else CaseInsensitiveDict()
        self._json = json_data
        self.text = text if json_data is _NOT_JSON else json.dumps(json_data)

    def json(self):
        if self._json is _NOT_JSON:
            raise requests.exceptions.JSONDecodeError("Expecting value", self.text, 0)
        return self._json


class FakeSession:
    """Records every request; answers POSTs and GETs from their own queues, in order."""

    def __init__(self, graphql=(), rest=()):
        self._queues = {"POST": list(graphql), "GET": list(rest)}
        self.calls: list[tuple[str, str, dict]] = []

    def _answer(self, verb, url, kwargs):
        self.calls.append((verb, url, copy.deepcopy(kwargs)))
        queue = self._queues[verb]
        if not queue:
            raise AssertionError(f"unexpected {verb} {url}")
        item = queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def post(self, url, **kwargs):
        return self._answer("POST", url, kwargs)

    def get(self, url, **kwargs):
        return self._answer("GET", url, kwargs)

    def verbs(self):
        return [verb for verb, _, _ in self.calls]

    def of(self, verb):
        return [(url, kwargs) for v, url, kwargs in self.calls if v == verb]


def _ref(url=GL_PR_URL):
    ref = gitlab.ForgeImpl.parse_pr_url(url)
    assert ref is not None
    return ref


def _list(session, ref=None, sha=SHA):
    return gitlab.ForgeImpl(session=session).list_paths(ref or _ref(), sha=sha)


def _blobs_body(paths, *, has_next=False, cursor=None, nodes=None):
    if nodes is None:
        nodes = [{"path": p, "type": "blob", "mode": "100644"} for p in paths]
    return {"data": {"project": {"repository": {"tree": {"blobs": {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": nodes,
    }}}}}}


def _gql(paths, *, has_next=False, cursor=None):
    return FakeResponse(200, json_data=_blobs_body(paths, has_next=has_next, cursor=cursor))


def _entry(path, kind="blob"):
    mode = {"blob": "100644", "tree": "040000", "commit": "160000"}[kind]
    return {"id": "0" * 40, "name": path.rsplit("/", 1)[-1], "type": kind, "path": path, "mode": mode}


def _rest_page(entries, next_page=""):
    return FakeResponse(200, json_data=entries, headers=CaseInsensitiveDict({"x-next-page": next_page}))


def _rest_params(page):
    return {"recursive": "true", "per_page": 100, "page": page, "ref": SHA}


def _debug_lines(caplog):
    records = [r for r in caplog.records if "list_paths" in r.getMessage()]
    assert records, "no list_paths log line"
    assert {r.levelno for r in records} == {logging.DEBUG}
    return [r.getMessage() for r in records]


# --- the GraphQL walk ------------------------------------------------------------


class TestGraphQLWalk:
    def test_one_page_is_one_post_of_the_query_and_no_rest_call(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITLAB_TOKEN", "t0ken")
        session = FakeSession(graphql=[
            _gql(["src/b.py", "README.md", "src/a.py", "src/a.py"], has_next=False, cursor="NDA"),
        ])

        listing = _list(session)

        assert listing == PathListing(paths=("README.md", "src/a.py", "src/b.py"), complete=True)
        assert session.verbs() == ["POST"]
        url, kwargs = session.of("POST")[0]
        assert url == GRAPHQL_URL
        assert set(kwargs) == {"json", "headers", "timeout"}
        assert set(kwargs["json"]) == {"query", "variables"}
        assert kwargs["json"]["query"] == LIVE_QUERY
        assert kwargs["json"]["variables"] == {"p": PROJECT, "ref": SHA, "after": None}
        assert kwargs["headers"] == {"PRIVATE-TOKEN": "t0ken"}
        assert kwargs["timeout"] == REQUEST_TIMEOUT

    def test_the_query_is_a_read_whose_first_word_is_query(self):
        session = FakeSession(graphql=[_gql(["a.py"])])

        _list(session)

        sent = session.of("POST")[0][1]["json"]["query"]
        assert re.match(r"query\b", sent)
        assert sent.split("(", 1)[0] == "query"
        assert "mutation" not in sent
        assert gitlab._TREE_BLOBS_QUERY == LIVE_QUERY

    def test_no_token_sends_no_auth_header(self):
        session = FakeSession(graphql=[_gql(["a.py"])])

        _list(session)

        assert session.of("POST")[0][1]["headers"] == {}

    def test_pages_are_followed_by_threading_each_end_cursor_into_after(self):
        session = FakeSession(graphql=[
            _gql(["z/last.py", "a.py"], has_next=True, cursor="MTAw"),
            _gql(["m/mid.py", "a.py"], has_next=True, cursor="MjAw"),
            _gql(["b.py"], has_next=False, cursor="MjUw"),
        ])

        listing = _list(session)

        assert listing == PathListing(paths=("a.py", "b.py", "m/mid.py", "z/last.py"), complete=True)
        posts = session.of("POST")
        assert session.verbs() == ["POST", "POST", "POST"]
        assert [kwargs["json"]["variables"]["after"] for _, kwargs in posts] == [None, "MTAw", "MjAw"]
        assert {url for url, _ in posts} == {GRAPHQL_URL}
        assert {kwargs["json"]["variables"]["p"] for _, kwargs in posts} == {PROJECT}
        assert {kwargs["json"]["variables"]["ref"] for _, kwargs in posts} == {SHA}
        assert {kwargs["json"]["query"] for _, kwargs in posts} == {LIVE_QUERY}
        assert {kwargs["timeout"] for _, kwargs in posts} == {REQUEST_TIMEOUT}

    def test_two_pages_via_the_cursor(self):
        session = FakeSession(graphql=[
            _gql(["one.py"], has_next=True, cursor="MTAw"),
            _gql(["two.py"], has_next=False, cursor=None),
        ])

        listing = _list(session)

        assert listing == PathListing(paths=("one.py", "two.py"), complete=True)
        assert [kw["json"]["variables"] for _, kw in session.of("POST")] == [
            {"p": PROJECT, "ref": SHA, "after": None},
            {"p": PROJECT, "ref": SHA, "after": "MTAw"},
        ]
        assert session.of("GET") == []

    def test_nodes_whose_path_is_not_a_non_empty_string_are_dropped(self):
        nodes = [
            {"path": ".gitattributes", "type": "blob", "mode": "100644"},
            {"path": "bin/run", "type": "blob", "mode": "100755"},
            {"path": "", "type": "blob", "mode": "100644"},
            {"path": None, "type": "blob", "mode": "100644"},
            {"type": "blob", "mode": "100644"},
            {"path": 7},
            "not-a-dict",
            None,
        ]
        session = FakeSession(graphql=[FakeResponse(200, json_data=_blobs_body([], nodes=nodes))])

        listing = _list(session)

        assert listing == PathListing(paths=(".gitattributes", "bin/run"), complete=True)
        assert session.verbs() == ["POST"]

    def test_a_later_empty_page_ends_the_walk_whole(self):
        session = FakeSession(graphql=[
            _gql(["a.py"], has_next=True, cursor="MQ"),
            FakeResponse(200, json_data=LIVE_EMPTY_PAGE),
        ])

        listing = _list(session)

        assert listing == PathListing(paths=("a.py",), complete=True)
        assert session.verbs() == ["POST", "POST"]

    def test_the_post_goes_to_the_pr_host(self):
        ref = _ref("https://gitlab.com/acme/tools/sub/-/merge_requests/3")
        session = FakeSession(graphql=[_gql(["a.py"])])

        _list(session, ref=ref)

        url, kwargs = session.of("POST")[0]
        assert url == "https://gitlab.com/api/graphql"
        assert kwargs["json"]["variables"]["p"] == "acme/tools/sub"

    def test_an_empty_errors_array_is_not_a_failure(self):
        body = {"errors": [], **_blobs_body(["a.py"])}
        session = FakeSession(graphql=[FakeResponse(200, json_data=body)])

        assert _list(session) == PathListing(paths=("a.py",), complete=True)
        assert session.verbs() == ["POST"]

    def test_an_empty_sha_makes_no_request(self):
        session = FakeSession()

        assert _list(session, sha="") is None
        assert session.calls == []


# --- the page cap ------------------------------------------------------------------


class TestGraphQLCap:
    def test_the_cap_stops_the_walk_partial_with_no_rest_call(self, caplog):
        cap = gitlab.MAX_LISTING_PAGES
        pages = [_gql([f"p{n:02d}.py"], has_next=True, cursor=f"c{n}") for n in range(cap + 1)]
        session = FakeSession(graphql=pages)

        with caplog.at_level(logging.DEBUG, logger="prxref.forges.gitlab"):
            listing = _list(session)

        assert listing == PathListing(paths=tuple(f"p{n:02d}.py" for n in range(cap)), complete=False)
        assert session.verbs() == ["POST"] * cap
        assert [kw["json"]["variables"]["after"] for _, kw in session.of("POST")] == [
            None, *(f"c{n}" for n in range(cap - 1)),
        ]
        assert any("cap" in line and "GraphQL" in line for line in _debug_lines(caplog))

    def test_a_walk_that_ends_exactly_at_the_cap_is_whole(self):
        cap = gitlab.MAX_LISTING_PAGES
        pages = [_gql([f"p{n:02d}.py"], has_next=n < cap - 1, cursor=f"c{n}") for n in range(cap)]
        session = FakeSession(graphql=pages)

        listing = _list(session)

        assert listing == PathListing(paths=tuple(f"p{n:02d}.py" for n in range(cap)), complete=True)
        assert session.verbs() == ["POST"] * cap

    def test_the_cap_is_read_from_the_module(self, monkeypatch):
        monkeypatch.setattr(gitlab, "MAX_LISTING_PAGES", 2)
        session = FakeSession(graphql=[
            _gql(["one.py"], has_next=True, cursor="c1"),
            _gql(["two.py"], has_next=True, cursor="c2"),
            _gql(["three.py"]),
        ])

        assert _list(session) == PathListing(paths=("one.py", "two.py"), complete=False)
        assert session.verbs() == ["POST", "POST"]


# --- a failure after the first page --------------------------------------------------


def _unusable(kind):
    if kind == "transport":
        return requests.ConnectionError("connection refused")
    if kind == "timeout":
        return requests.Timeout("read timed out")
    if kind == "http-401":
        return FakeResponse(401, json_data={"message": "401 Unauthorized"})
    if kind == "http-404":
        return FakeResponse(404, json_data={"message": "404 Not Found"})
    if kind == "http-500":
        return FakeResponse(500, text="internal error")
    if kind == "non-json":
        return FakeResponse(200, text="<html>not json</html>")
    bodies = {
        "list-body": [],
        "null-body": None,
        "errors": {"errors": [{"message": "Field 'blobs' doesn't exist on type 'Tree'"}]},
        "errors-with-data": {"errors": [{"message": "partial"}], **_blobs_body(["x.py"])},
        "project-null": LIVE_UNKNOWN_PROJECT,
        "data-null": {"data": None},
        "no-data": {},
        "repository-null": {"data": {"project": {"repository": None}}},
        "tree-null": {"data": {"project": {"repository": {"tree": None}}}},
        "blobs-missing": {"data": {"project": {"repository": {"tree": {}}}}},
        "nodes-not-a-list": {"data": {"project": {"repository": {"tree": {"blobs": {
            "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": {"path": "a.py"},
        }}}}}},
        "page-info-missing": {"data": {"project": {"repository": {"tree": {"blobs": {
            "nodes": [{"path": "a.py"}],
        }}}}}},
        "has-next-not-a-bool": _blobs_body(["a.py"], has_next="false", cursor=None),
        "has-next-without-cursor": _blobs_body(["a.py"], has_next=True, cursor=None),
        "has-next-with-empty-cursor": _blobs_body(["a.py"], has_next=True, cursor=""),
    }
    return FakeResponse(200, json_data=bodies[kind])


LATER_PAGE_FAILURES = [
    "transport", "timeout", "http-401", "http-500", "non-json", "list-body", "errors",
    "project-null", "no-data", "nodes-not-a-list", "has-next-not-a-bool", "has-next-without-cursor",
]


class TestGraphQLLaterPageFailure:
    @pytest.mark.parametrize("kind", LATER_PAGE_FAILURES)
    def test_a_later_page_failure_keeps_the_paths_read_so_far(self, kind, caplog):
        session = FakeSession(graphql=[
            _gql(["b.py", "a.py"], has_next=True, cursor="MTAw"),
            _unusable(kind),
        ])

        with caplog.at_level(logging.DEBUG, logger="prxref.forges.gitlab"):
            listing = _list(session)

        assert listing == PathListing(paths=("a.py", "b.py"), complete=False)
        assert session.verbs() == ["POST", "POST"]
        lines = _debug_lines(caplog)
        assert any("page 2" in line and "GraphQL" in line for line in lines)

    def test_a_third_page_failure_keeps_both_pages(self):
        session = FakeSession(graphql=[
            _gql(["a.py"], has_next=True, cursor="c1"),
            _gql(["b.py"], has_next=True, cursor="c2"),
            requests.ConnectionError("reset"),
        ])

        assert _list(session) == PathListing(paths=("a.py", "b.py"), complete=False)
        assert session.verbs() == ["POST", "POST", "POST"]


# --- falling back to the REST walk ---------------------------------------------------


FIRST_PAGE_FALLBACKS = [
    "transport", "timeout", "http-401", "http-404", "http-500", "non-json",
    "list-body", "null-body", "errors", "errors-with-data", "project-null", "data-null",
    "no-data", "repository-null", "tree-null", "blobs-missing", "nodes-not-a-list",
    "page-info-missing", "has-next-not-a-bool", "has-next-without-cursor",
    "has-next-with-empty-cursor",
]


class TestFallbackToRest:
    @pytest.mark.parametrize("kind", FIRST_PAGE_FALLBACKS)
    def test_an_unusable_first_page_runs_the_rest_walk(self, kind, caplog):
        session = FakeSession(
            graphql=[_unusable(kind)],
            rest=[_rest_page([_entry("src", kind="tree"), _entry("src/app.py")])],
        )

        with caplog.at_level(logging.DEBUG, logger="prxref.forges.gitlab"):
            listing = _list(session)

        assert listing == PathListing(paths=("src/app.py",), complete=True)
        assert session.verbs() == ["POST", "GET"]
        url, kwargs = session.of("GET")[0]
        assert url == TREE_URL
        assert kwargs["params"] == _rest_params(1)
        assert any("falls back to the REST tree walk" in line for line in _debug_lines(caplog))

    @pytest.mark.parametrize(
        "first",
        [
            pytest.param(LIVE_EMPTY_PAGE, id="live-empty-page"),
            pytest.param(_blobs_body([], cursor="NDA"), id="empty-page-with-a-cursor"),
            pytest.param(
                _blobs_body([], nodes=[{"path": ""}, {"type": "blob"}, "x"]),
                id="no-usable-path",
            ),
        ],
    )
    def test_an_empty_first_page_runs_the_rest_walk(self, first):
        session = FakeSession(
            graphql=[FakeResponse(200, json_data=first)],
            rest=[_rest_page([_entry("a.py")])],
        )

        assert _list(session) == PathListing(paths=("a.py",), complete=True)
        assert session.verbs() == ["POST", "GET"]

    def test_a_bad_sha_empty_first_page_with_a_rest_404_gives_none(self):
        session = FakeSession(
            graphql=[FakeResponse(200, json_data=LIVE_EMPTY_PAGE)],
            rest=[FakeResponse(404, json_data={"message": "404 Tree Not Found"})],
        )

        assert _list(session) is None
        assert session.verbs() == ["POST", "GET"]

    def test_an_empty_tree_gives_an_empty_complete_listing(self):
        session = FakeSession(
            graphql=[FakeResponse(200, json_data=LIVE_EMPTY_PAGE)],
            rest=[_rest_page([])],
        )

        assert _list(session) == PathListing(paths=(), complete=True)
        assert session.verbs() == ["POST", "GET"]

    def test_an_unknown_project_with_a_rest_404_gives_none(self):
        session = FakeSession(
            graphql=[FakeResponse(200, json_data=LIVE_UNKNOWN_PROJECT)],
            rest=[FakeResponse(404, json_data={"message": "404 Project Not Found"})],
        )

        assert _list(session) is None
        assert session.verbs() == ["POST", "GET"]

    def test_the_fallback_walk_pages_and_authenticates_as_before(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITLAB_TOKEN", "t0ken")
        session = FakeSession(
            graphql=[FakeResponse(401, json_data={"message": "401 Unauthorized"})],
            rest=[
                _rest_page([_entry("b.py"), _entry("lib", kind="tree")], next_page="2"),
                _rest_page([_entry("a.py"), _entry("vendor/x", kind="commit")], next_page=""),
            ],
        )

        listing = _list(session)

        assert listing == PathListing(paths=("a.py", "b.py"), complete=True)
        assert session.verbs() == ["POST", "GET", "GET"]
        assert session.of("POST")[0][1]["headers"] == {"PRIVATE-TOKEN": "t0ken"}
        gets = session.of("GET")
        assert [kw["params"] for _, kw in gets] == [_rest_params(1), _rest_params(2)]
        assert {kw["headers"]["PRIVATE-TOKEN"] for _, kw in gets} == {"t0ken"}
        assert {kw["timeout"] for _, kw in gets} == {REQUEST_TIMEOUT}

    def test_a_rest_first_page_failure_after_the_fallback_gives_none(self):
        session = FakeSession(
            graphql=[requests.ConnectionError("refused")],
            rest=[requests.ConnectionError("refused")],
        )

        assert _list(session) is None
        assert session.verbs() == ["POST", "GET"]


# --- the defect a live check against gitlab.com observed ---------------------------


def _directories_first_rest_listing():
    """REST as the live gitlab.com check saw it: every page up to the cap is directories, the files come after."""
    cap = gitlab.MAX_LISTING_PAGES
    pages = [
        _rest_page(
            [_entry(f"dir{page:02d}/sub{n:02d}", kind="tree") for n in range(100)],
            next_page=str(page + 2),
        )
        for page in range(cap)
    ]
    pages.append(_rest_page([_entry("README.md"), _entry("src/app.py")], next_page=""))
    return pages


BLOBS = [f"dir{n:02d}/file{m}.py" for n in range(30) for m in range(10)]


class TestDirectoriesBeforeFiles:
    def test_graphql_lists_the_files_the_rest_walk_never_reaches(self):
        session = FakeSession(
            graphql=[
                _gql(BLOBS[:100], has_next=True, cursor="MTAw"),
                _gql(BLOBS[100:200], has_next=True, cursor="MjAw"),
                _gql(BLOBS[200:], has_next=False, cursor="MzAw"),
            ],
            rest=_directories_first_rest_listing(),
        )

        listing = _list(session)

        assert listing == PathListing(paths=tuple(sorted(BLOBS)), complete=True)
        assert len(listing.paths) == 300
        assert session.verbs() == ["POST"] * 3

    def test_control_without_graphql_the_rest_walk_finds_no_file(self):
        session = FakeSession(
            graphql=[requests.ConnectionError("refused")],
            rest=_directories_first_rest_listing(),
        )

        listing = _list(session)

        assert listing == PathListing(paths=(), complete=False)
        assert session.verbs() == ["POST"] + ["GET"] * gitlab.MAX_LISTING_PAGES


# --- the documented contract -------------------------------------------------------


class TestDocstring:
    def test_the_docstring_names_both_walks_and_why_graphql_comes_first(self):
        doc = " ".join((gitlab.ForgeImpl.list_paths.__doc__ or "").split())
        assert "tree.blobs" in doc
        assert "every directory before any file" in doc
        assert "repository/tree?recursive=true" in doc
        assert "REST walk answers instead when the FIRST GraphQL page is unusable" in doc
        assert "Never raises" in doc
        assert "MAX_LISTING_PAGES" in doc
