"""Tests for the paged ``list_paths`` listings (issue #17).

GitLab walks ``repository/tree?recursive=true`` page by page and stops when
``X-Next-Page`` is absent or empty. Bitbucket Server / Data Center walks the
documented ``/files?at=`` listing with ``start``/``limit`` and stops on
``isLastPage`` or a missing ``nextPageStart``; its shape is UNVERIFIED (no
public instance). Both cap the walk at ``MAX_LISTING_PAGES`` pages.
"""
from __future__ import annotations

import inspect
import json
import logging
from unittest.mock import MagicMock

import pytest
import requests
from requests.structures import CaseInsensitiveDict

from prxref.forges import base, bitbucket_server, gitlab
from prxref.forges.base import PathListing

SHA = "1234567890abcdef1234567890abcdef12345678"
REQUEST_TIMEOUT = (10.0, 30.0)

GL_PR_URL = "https://gitlab.example.com/acme/platform/api/-/merge_requests/7"
GL_TREE_URL = "https://gitlab.example.com/api/v4/projects/acme%2Fplatform%2Fapi/repository/tree"

BBS_PR_URL = "https://bitbucket.example.com/projects/PLAT/repos/api/pull-requests/42"
BBS_FILES_URL = "https://bitbucket.example.com/rest/api/1.0/projects/PLAT/repos/api/files"
BBS_PERSONAL_PR_URL = "https://bitbucket.example.com/users/jdoe/repos/scratch/pull-requests/7"
BBS_PERSONAL_FILES_URL = "https://bitbucket.example.com/rest/api/1.0/projects/~jdoe/repos/scratch/files"

TOKEN_VARS = (
    "PRXREF_GITLAB_TOKEN",
    "PRXREF_BITBUCKET_SERVER_TOKEN",
    "PRXREF_BITBUCKET_TOKEN",
    "PRXREF_BITBUCKET_SERVER_USER",
    "PRXREF_BITBUCKET_SERVER_PASSWORD",
)


@pytest.fixture(autouse=True)
def _no_ambient_tokens(monkeypatch):
    for name in TOKEN_VARS:
        monkeypatch.delenv(name, raising=False)


def _mock_response(status_code=200, json_data=None, text="", content=None, headers=None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.headers = headers or {}
    if json_data is not None:
        resp.json.return_value = json_data
        resp.text = json.dumps(json_data)
    else:
        resp.text = text
        resp.json.side_effect = ValueError("No JSON")
    resp.content = content if content is not None else resp.text.encode("utf-8")
    resp.raise_for_status.side_effect = (
        None if resp.ok else requests.HTTPError(response=resp)
    )
    return resp


def _session(*responses):
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = list(responses)
    return session


def _gl_ref(url=GL_PR_URL):
    ref = gitlab.ForgeImpl.parse_pr_url(url)
    assert ref is not None
    return ref


def _bbs_ref(url=BBS_PR_URL):
    ref = bitbucket_server.ForgeImpl.parse_pr_url(url)
    assert ref is not None
    return ref


def _entry(path, kind="blob"):
    mode = {"blob": "100644", "tree": "040000", "commit": "160000"}[kind]
    return {"id": "0" * 40, "name": path.rsplit("/", 1)[-1], "type": kind, "path": path, "mode": mode}


def _gl_page(entries, next_page=""):
    return _mock_response(200, json_data=entries, headers=CaseInsensitiveDict({"x-next-page": next_page}))


def _bbs_page(values, *, start=0, last=True, next_start=None):
    body = {"values": values, "size": len(values), "isLastPage": last, "start": start, "limit": 100}
    if next_start is not None:
        body["nextPageStart"] = next_start
    return _mock_response(200, json_data=body)


def _sent(session, key):
    return [c.kwargs["params"][key] for c in session.get.call_args_list]


def _failure(kind):
    if kind == "http-404":
        return _mock_response(404, json_data={"message": "404 Tree Not Found"})
    if kind == "http-500":
        return _mock_response(500, text="internal error")
    if kind == "transport":
        return requests.ConnectionError("connection refused")
    return _mock_response(200, text="<html>not json</html>")


FAILURE_KINDS = ["http-404", "http-500", "transport", "non-json"]


# --- shared ---------------------------------------------------------------------


class TestSharedCap:
    @pytest.mark.parametrize("module", [gitlab, bitbucket_server])
    def test_each_adapter_reads_the_cap_from_base(self, module):
        assert module.MAX_LISTING_PAGES is base.MAX_LISTING_PAGES
        assert not [name for name in vars(module) if "LISTING" in name and name != "MAX_LISTING_PAGES"]

    @pytest.mark.parametrize("forge_cls", [gitlab.ForgeImpl, bitbucket_server.ForgeImpl])
    def test_the_method_has_the_protocol_signature_and_a_docstring(self, forge_cls):
        signature = inspect.signature(forge_cls.list_paths)
        params = signature.parameters
        assert list(params) == ["self", "ref", "sha"]
        assert params["sha"].kind is inspect.Parameter.KEYWORD_ONLY
        assert signature.return_annotation == "PathListing | None"
        doc = inspect.getdoc(forge_cls.list_paths)
        assert "Never raises" in doc
        assert "MAX_LISTING_PAGES" in doc


# --- GitLab -------------------------------------------------------------------


class TestGitLabListPaths:
    def test_one_page_keeps_file_paths_sorted_and_deduplicated(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITLAB_TOKEN", "t0ken")
        session = _session(_gl_page([
            _entry("src/b.py"),
            _entry("README.md"),
            _entry("src/a.py"),
            _entry("src/a.py"),
        ]))

        listing = gitlab.ForgeImpl(session=session).list_paths(_gl_ref(), sha=SHA)

        assert listing == PathListing(paths=("README.md", "src/a.py", "src/b.py"), complete=True)
        assert session.get.call_count == 1
        call = session.get.call_args
        assert call.args[0] == GL_TREE_URL
        assert call.kwargs["params"] == {"recursive": "true", "per_page": 100, "page": 1, "ref": SHA}
        assert call.kwargs["headers"] == {"PRIVATE-TOKEN": "t0ken"}
        assert call.kwargs["timeout"] == REQUEST_TIMEOUT

    def test_no_token_sends_no_auth_header(self):
        session = _session(_gl_page([_entry("a.py")]))

        gitlab.ForgeImpl(session=session).list_paths(_gl_ref(), sha=SHA)

        assert session.get.call_args.kwargs["headers"] == {}

    def test_the_page_size_is_the_module_page_size(self):
        session = _session(_gl_page([_entry("a.py")]))

        gitlab.ForgeImpl(session=session).list_paths(_gl_ref(), sha=SHA)

        assert session.get.call_args.kwargs["params"]["per_page"] == gitlab._PAGE_SIZE

    def test_three_pages_are_walked_in_order(self):
        session = _session(
            _gl_page([_entry("z/last.py"), _entry("a.py")], next_page="2"),
            _gl_page([_entry("m/mid.py"), _entry("a.py")], next_page="3"),
            _gl_page([_entry("b.py")], next_page=""),
        )

        listing = gitlab.ForgeImpl(session=session).list_paths(_gl_ref(), sha=SHA)

        assert listing == PathListing(paths=("a.py", "b.py", "m/mid.py", "z/last.py"), complete=True)
        assert _sent(session, "page") == [1, 2, 3]
        assert {c.args[0] for c in session.get.call_args_list} == {GL_TREE_URL}
        assert {c.kwargs["params"]["ref"] for c in session.get.call_args_list} == {SHA}
        assert {c.kwargs["timeout"] for c in session.get.call_args_list} == {REQUEST_TIMEOUT}

    def test_a_short_page_that_names_a_next_page_is_still_followed(self):
        short = [_entry("a.py")]
        assert len(short) < gitlab._PAGE_SIZE
        session = _session(
            _gl_page([_entry("first.py")], next_page="2"),
            _gl_page(short, next_page="3"),
            _gl_page([_entry("third.py")], next_page=""),
        )

        listing = gitlab.ForgeImpl(session=session).list_paths(_gl_ref(), sha=SHA)

        assert session.get.call_count == 3
        assert listing == PathListing(paths=("a.py", "first.py", "third.py"), complete=True)

    def test_the_page_cap_stops_the_walk_and_marks_the_listing_partial(self, monkeypatch):
        monkeypatch.setattr(gitlab, "MAX_LISTING_PAGES", 2)
        session = _session(
            _gl_page([_entry("one.py")], next_page="2"),
            _gl_page([_entry("two.py")], next_page="3"),
            _gl_page([_entry("three.py")], next_page=""),
        )

        listing = gitlab.ForgeImpl(session=session).list_paths(_gl_ref(), sha=SHA)

        assert session.get.call_count == 2
        assert listing == PathListing(paths=("one.py", "two.py"), complete=False)

    def test_a_walk_that_ends_exactly_at_the_cap_is_whole(self, monkeypatch):
        monkeypatch.setattr(gitlab, "MAX_LISTING_PAGES", 2)
        session = _session(
            _gl_page([_entry("one.py")], next_page="2"),
            _gl_page([_entry("two.py")], next_page=""),
        )

        listing = gitlab.ForgeImpl(session=session).list_paths(_gl_ref(), sha=SHA)

        assert listing == PathListing(paths=("one.py", "two.py"), complete=True)

    @pytest.mark.parametrize("kind", FAILURE_KINDS)
    def test_a_first_page_failure_gives_none(self, kind, caplog):
        session = _session(_failure(kind))

        with caplog.at_level(logging.DEBUG, logger="prxref.forges.gitlab"):
            listing = gitlab.ForgeImpl(session=session).list_paths(_gl_ref(), sha=SHA)

        assert listing is None
        records = [r for r in caplog.records if "list_paths" in r.getMessage()]
        assert records and {r.levelno for r in records} == {logging.DEBUG}

    def test_a_wrong_shape_first_page_gives_none(self):
        session = _session(_mock_response(200, json_data={"message": "not a list"}))

        assert gitlab.ForgeImpl(session=session).list_paths(_gl_ref(), sha=SHA) is None

    @pytest.mark.parametrize("kind", [*FAILURE_KINDS, "wrong-shape"])
    def test_a_second_page_failure_keeps_the_first_page_as_partial(self, kind, caplog):
        second = (
            _mock_response(200, json_data={"message": "not a list"})
            if kind == "wrong-shape" else _failure(kind)
        )
        session = _session(_gl_page([_entry("b.py"), _entry("a.py")], next_page="2"), second)

        with caplog.at_level(logging.DEBUG, logger="prxref.forges.gitlab"):
            listing = gitlab.ForgeImpl(session=session).list_paths(_gl_ref(), sha=SHA)

        assert listing == PathListing(paths=("a.py", "b.py"), complete=False)
        assert session.get.call_count == 2
        records = [r for r in caplog.records if "list_paths" in r.getMessage()]
        assert records and {r.levelno for r in records} == {logging.DEBUG}

    def test_an_empty_sha_makes_no_request(self):
        session = _session()

        assert gitlab.ForgeImpl(session=session).list_paths(_gl_ref(), sha="") is None
        session.get.assert_not_called()

    def test_directories_submodules_and_malformed_entries_are_dropped(self):
        session = _session(_gl_page([
            _entry("src", kind="tree"),
            _entry("vendor/lib", kind="commit"),
            _entry("src/app.py"),
            {"type": "blob", "path": ""},
            {"type": "blob", "path": None},
            {"type": "blob"},
            "not-a-dict",
        ]))

        listing = gitlab.ForgeImpl(session=session).list_paths(_gl_ref(), sha=SHA)

        assert listing == PathListing(paths=("src/app.py",), complete=True)

    @pytest.mark.parametrize(
        "headers",
        [None, CaseInsensitiveDict({"x-next-page": ""}), CaseInsensitiveDict({"X-Next-Page": "  "})],
        ids=["header-absent", "header-empty", "header-blank"],
    )
    def test_an_absent_or_empty_next_page_header_ends_the_walk(self, headers):
        session = _session(
            _mock_response(200, json_data=[_entry("a.py")] * 100, headers=headers),
            _gl_page([_entry("never.py")]),
        )

        listing = gitlab.ForgeImpl(session=session).list_paths(_gl_ref(), sha=SHA)

        assert session.get.call_count == 1
        assert listing == PathListing(paths=("a.py",), complete=True)

    def test_the_next_page_header_is_read_case_insensitively(self):
        session = _session(
            _mock_response(200, json_data=[_entry("a.py")], headers=CaseInsensitiveDict({"X-NEXT-PAGE": "2"})),
            _gl_page([_entry("b.py")]),
        )

        listing = gitlab.ForgeImpl(session=session).list_paths(_gl_ref(), sha=SHA)

        assert session.get.call_count == 2
        assert listing == PathListing(paths=("a.py", "b.py"), complete=True)


# --- Bitbucket Server / Data Center ---------------------------------------------


class TestBitbucketServerListPaths:
    def test_one_page_keeps_file_paths_sorted_and_deduplicated(self, monkeypatch):
        monkeypatch.setenv("PRXREF_BITBUCKET_SERVER_TOKEN", "t0ken")
        session = _session(_bbs_page(["src/b.java", "README.md", "src/a.java", "src/a.java"]))

        listing = bitbucket_server.ForgeImpl(session=session).list_paths(_bbs_ref(), sha=SHA)

        assert listing == PathListing(paths=("README.md", "src/a.java", "src/b.java"), complete=True)
        assert session.get.call_count == 1
        call = session.get.call_args
        assert call.args[0] == BBS_FILES_URL
        assert call.kwargs["params"] == {"at": SHA, "start": 0, "limit": 100}
        assert call.kwargs["headers"] == {"Authorization": "Bearer t0ken"}
        assert call.kwargs["auth"] is None
        assert call.kwargs["timeout"] == REQUEST_TIMEOUT

    def test_basic_auth_is_sent_as_the_auth_pair(self, monkeypatch):
        monkeypatch.setenv("PRXREF_BITBUCKET_SERVER_USER", "svc")
        monkeypatch.setenv("PRXREF_BITBUCKET_SERVER_PASSWORD", "pw")
        session = _session(_bbs_page(["a.py"]))

        bitbucket_server.ForgeImpl(session=session).list_paths(_bbs_ref(), sha=SHA)

        assert session.get.call_args.kwargs["headers"] == {}
        assert session.get.call_args.kwargs["auth"] == ("svc", "pw")

    def test_the_page_limit_is_the_module_page_limit(self):
        session = _session(_bbs_page(["a.py"]))

        bitbucket_server.ForgeImpl(session=session).list_paths(_bbs_ref(), sha=SHA)

        assert session.get.call_args.kwargs["params"]["limit"] == bitbucket_server._PAGE_LIMIT

    def test_three_pages_are_walked_in_order(self):
        session = _session(
            _bbs_page(["z/last.py", "a.py"], start=0, last=False, next_start=100),
            _bbs_page(["m/mid.py", "a.py"], start=100, last=False, next_start=200),
            _bbs_page(["b.py"], start=200, last=True),
        )

        listing = bitbucket_server.ForgeImpl(session=session).list_paths(_bbs_ref(), sha=SHA)

        assert listing == PathListing(paths=("a.py", "b.py", "m/mid.py", "z/last.py"), complete=True)
        assert _sent(session, "start") == [0, 100, 200]
        assert {c.args[0] for c in session.get.call_args_list} == {BBS_FILES_URL}
        assert {c.kwargs["params"]["at"] for c in session.get.call_args_list} == {SHA}
        assert {c.kwargs["timeout"] for c in session.get.call_args_list} == {REQUEST_TIMEOUT}

    def test_the_next_start_comes_from_the_page_not_a_fixed_stride(self):
        session = _session(
            _bbs_page(["a.py"], start=0, last=False, next_start=37),
            _bbs_page(["b.py"], start=37, last=True),
        )

        bitbucket_server.ForgeImpl(session=session).list_paths(_bbs_ref(), sha=SHA)

        assert _sent(session, "start") == [0, 37]

    def test_the_page_cap_stops_the_walk_and_marks_the_listing_partial(self, monkeypatch):
        monkeypatch.setattr(bitbucket_server, "MAX_LISTING_PAGES", 2)
        session = _session(
            _bbs_page(["one.py"], start=0, last=False, next_start=100),
            _bbs_page(["two.py"], start=100, last=False, next_start=200),
            _bbs_page(["three.py"], start=200, last=True),
        )

        listing = bitbucket_server.ForgeImpl(session=session).list_paths(_bbs_ref(), sha=SHA)

        assert session.get.call_count == 2
        assert listing == PathListing(paths=("one.py", "two.py"), complete=False)

    def test_a_walk_that_ends_exactly_at_the_cap_is_whole(self, monkeypatch):
        monkeypatch.setattr(bitbucket_server, "MAX_LISTING_PAGES", 2)
        session = _session(
            _bbs_page(["one.py"], start=0, last=False, next_start=100),
            _bbs_page(["two.py"], start=100, last=True),
        )

        listing = bitbucket_server.ForgeImpl(session=session).list_paths(_bbs_ref(), sha=SHA)

        assert listing == PathListing(paths=("one.py", "two.py"), complete=True)

    @pytest.mark.parametrize("kind", FAILURE_KINDS)
    def test_a_first_page_failure_gives_none(self, kind, caplog):
        session = _session(_failure(kind))

        with caplog.at_level(logging.DEBUG, logger="prxref.forges.bitbucket_server"):
            listing = bitbucket_server.ForgeImpl(session=session).list_paths(_bbs_ref(), sha=SHA)

        assert listing is None
        records = [r for r in caplog.records if "list_paths" in r.getMessage()]
        assert records and {r.levelno for r in records} == {logging.DEBUG}

    @pytest.mark.parametrize(
        "body",
        [["a.py"], {"size": 0, "isLastPage": True}, {"values": "a.py", "isLastPage": True}],
        ids=["list-body", "no-values", "values-not-a-list"],
    )
    def test_a_wrong_shape_first_page_gives_none(self, body):
        session = _session(_mock_response(200, json_data=body))

        assert bitbucket_server.ForgeImpl(session=session).list_paths(_bbs_ref(), sha=SHA) is None

    @pytest.mark.parametrize("kind", [*FAILURE_KINDS, "wrong-shape"])
    def test_a_second_page_failure_keeps_the_first_page_as_partial(self, kind, caplog):
        second = (
            _mock_response(200, json_data={"errors": [{"message": "nope"}]})
            if kind == "wrong-shape" else _failure(kind)
        )
        session = _session(_bbs_page(["b.py", "a.py"], start=0, last=False, next_start=100), second)

        with caplog.at_level(logging.DEBUG, logger="prxref.forges.bitbucket_server"):
            listing = bitbucket_server.ForgeImpl(session=session).list_paths(_bbs_ref(), sha=SHA)

        assert listing == PathListing(paths=("a.py", "b.py"), complete=False)
        assert session.get.call_count == 2
        records = [r for r in caplog.records if "list_paths" in r.getMessage()]
        assert records and {r.levelno for r in records} == {logging.DEBUG}

    def test_an_empty_sha_makes_no_request(self):
        session = _session()

        assert bitbucket_server.ForgeImpl(session=session).list_paths(_bbs_ref(), sha="") is None
        session.get.assert_not_called()

    def test_a_personal_repository_hits_the_tilde_url(self):
        session = _session(_bbs_page(["notes.md"]))
        ref = _bbs_ref(BBS_PERSONAL_PR_URL)
        assert ref.owner == "~jdoe"

        listing = bitbucket_server.ForgeImpl(session=session).list_paths(ref, sha=SHA)

        assert listing == PathListing(paths=("notes.md",), complete=True)
        assert session.get.call_args.args[0] == BBS_PERSONAL_FILES_URL

    def test_empty_and_non_string_values_are_dropped(self):
        session = _session(_bbs_page(["src/app.py", "", None, 7, {"path": "x.py"}]))

        listing = bitbucket_server.ForgeImpl(session=session).list_paths(_bbs_ref(), sha=SHA)

        assert listing == PathListing(paths=("src/app.py",), complete=True)

    def test_a_page_without_is_last_page_ends_the_walk_whole(self):
        body = {"values": ["a.py"], "size": 1, "start": 0, "limit": 100, "nextPageStart": 100}
        session = _session(_mock_response(200, json_data=body), _bbs_page(["never.py"]))

        listing = bitbucket_server.ForgeImpl(session=session).list_paths(_bbs_ref(), sha=SHA)

        assert session.get.call_count == 1
        assert listing == PathListing(paths=("a.py",), complete=True)

    def test_a_non_last_page_with_no_next_start_ends_the_walk_partial(self):
        session = _session(
            _bbs_page(["a.py"], start=0, last=False, next_start=None),
            _bbs_page(["never.py"]),
        )

        listing = bitbucket_server.ForgeImpl(session=session).list_paths(_bbs_ref(), sha=SHA)

        assert session.get.call_count == 1
        assert listing == PathListing(paths=("a.py",), complete=False)

    def test_an_empty_listing_is_whole_and_empty(self):
        session = _session(_bbs_page([]))

        listing = bitbucket_server.ForgeImpl(session=session).list_paths(_bbs_ref(), sha=SHA)

        assert listing == PathListing(paths=(), complete=True)
