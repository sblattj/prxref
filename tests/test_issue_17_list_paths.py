"""Tests for the optional ``Forge.list_paths`` listing (issue #17, task T8).

``PathListing`` and the Protocol method live in ``forges/base.py``;
``ReplayForge`` delegates the listing to the forge it wraps, ``LocalDiffForge``
has none, and GitHub answers it from one Git Trees API request.
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import logging
from unittest.mock import MagicMock

import pytest
import requests

from prxref.forges import base
from prxref.forges.base import Forge, PathListing, PRRef
from prxref.forges.github import ForgeImpl
from prxref.forges.replay import LocalDiffForge, ReplayForge

SHA = "e13a2c97d926386a950ae1a559d2ee50b113ad2e"
OTHER_SHA = "b" * 40
REQUEST_TIMEOUT = (10.0, 30.0)
TREE = [
    {"mode": "100644", "path": "src/b.py", "sha": "1" * 40, "size": 10, "type": "blob"},
    {"mode": "040000", "path": "src", "sha": "2" * 40, "type": "tree"},
    {"mode": "160000", "path": "vendor/lib", "sha": "3" * 40, "type": "commit"},
    {"mode": "100644", "path": "README.md", "sha": "4" * 40, "size": 5, "type": "blob"},
    {"mode": "100644", "path": "src/a.py", "sha": "5" * 40, "size": 7, "type": "blob"},
    {"mode": "100644", "path": "src/a.py", "sha": "5" * 40, "size": 7, "type": "blob"},
]
BLOB_PATHS = ("README.md", "src/a.py", "src/b.py")


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


def _ref(url="https://github.com/acme/api/pull/42"):
    ref = ForgeImpl.parse_pr_url(url)
    assert ref is not None
    return ref


def _tree_body(tree=TREE, truncated=False):
    return {"sha": SHA, "url": "https://api.example.com/tree", "tree": tree, "truncated": truncated}


def _session(response):
    session = MagicMock(spec=requests.Session)
    session.get.return_value = response
    return session


# --- PathListing and the Protocol --------------------------------------------


class TestPathListing:
    def test_it_is_frozen(self):
        listing = PathListing(paths=("a.py",), complete=True)
        with pytest.raises(dataclasses.FrozenInstanceError):
            listing.complete = False  # type: ignore[misc]

    def test_it_is_hashable_and_compares_by_value(self):
        one = PathListing(paths=("a.py", "b.py"), complete=True)
        two = PathListing(paths=("a.py", "b.py"), complete=True)
        assert hash(one) == hash(two)
        assert one == two
        assert len({one, two, PathListing(paths=("a.py", "b.py"), complete=False)}) == 2

    def test_its_fields_are_exactly_paths_and_complete(self):
        assert [field.name for field in dataclasses.fields(PathListing)] == ["paths", "complete"]


class TestProtocolDeclaration:
    def test_the_page_cap_is_twenty(self):
        assert base.MAX_LISTING_PAGES == 20

    def test_the_protocol_declares_it_with_a_keyword_only_sha(self):
        signature = inspect.signature(Forge.list_paths)
        params = signature.parameters
        assert list(params) == ["self", "ref", "sha"]
        assert params["sha"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["sha"].default is inspect.Parameter.empty
        assert signature.return_annotation == "PathListing | None"

    def test_it_follows_get_pr_history_in_the_protocol(self):
        names = [name for name in vars(Forge) if not name.startswith("_")]
        assert names.index("list_paths") == names.index("get_pr_history") + 1

    def test_the_docstring_says_it_is_optional_and_never_raises(self):
        doc = inspect.getdoc(Forge.list_paths)
        assert 'getattr(forge, "list_paths", None)' in doc
        assert "complete=False" in doc
        assert "MAX_LISTING_PAGES" in doc
        assert "never raises" in doc


# --- GitHub -------------------------------------------------------------------


class TestGitHubListPaths:
    def test_it_keeps_only_blob_paths_sorted_and_deduplicated(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITHUB_TOKEN", "t0ken")
        session = _session(_mock_response(json_data=_tree_body()))

        listing = ForgeImpl(session=session).list_paths(_ref(), sha=SHA)

        assert listing == PathListing(paths=BLOB_PATHS, complete=True)
        session.get.assert_called_once()
        call = session.get.call_args
        assert call.args[0] == f"https://api.github.com/repos/acme/api/git/trees/{SHA}"
        assert call.kwargs["params"] == {"recursive": "1"}
        assert call.kwargs["timeout"] == REQUEST_TIMEOUT
        assert call.kwargs["headers"]["Accept"] == "application/vnd.github+json"
        assert call.kwargs["headers"]["Authorization"] == "Bearer t0ken"

    def test_the_live_probe_entry_shape_is_read(self):
        entry = {
            "mode": "100644", "path": ".dockerignore",
            "sha": "b1b58bb3cf967b160f341ebb00e6e8c99bee2955", "size": 116, "type": "blob",
            "url": "https://api.github.com/repos/acme/api/git/blobs/b1b58bb3cf967b160f341ebb00e6e8c99bee2955",
        }
        session = _session(_mock_response(json_data=_tree_body(tree=[entry])))

        listing = ForgeImpl(session=session).list_paths(_ref(), sha=SHA)

        assert listing == PathListing(paths=(".dockerignore",), complete=True)

    @pytest.mark.parametrize(("truncated", "complete"), [(False, True), (True, False)])
    def test_complete_mirrors_truncated(self, truncated, complete):
        session = _session(_mock_response(json_data=_tree_body(truncated=truncated)))

        listing = ForgeImpl(session=session).list_paths(_ref(), sha=SHA)

        assert listing is not None
        assert listing.complete is complete
        assert listing.paths == BLOB_PATHS

    def test_malformed_entries_are_skipped(self):
        tree = [
            "not-an-entry",
            {"type": "blob"},
            {"type": "blob", "path": ""},
            {"type": "blob", "path": 7},
            {"type": "blob", "path": "kept.py"},
        ]
        session = _session(_mock_response(json_data=_tree_body(tree=tree)))

        listing = ForgeImpl(session=session).list_paths(_ref(), sha=SHA)

        assert listing == PathListing(paths=("kept.py",), complete=True)

    def test_an_empty_tree_is_an_empty_complete_listing(self):
        session = _session(_mock_response(json_data=_tree_body(tree=[])))

        assert ForgeImpl(session=session).list_paths(_ref(), sha=SHA) == PathListing(
            paths=(), complete=True
        )

    def test_the_sha_is_quoted_into_the_url(self):
        session = _session(_mock_response(json_data=_tree_body()))

        ForgeImpl(session=session).list_paths(_ref(), sha="feat/x y")

        assert session.get.call_args.args[0] == (
            "https://api.github.com/repos/acme/api/git/trees/feat%2Fx%20y"
        )

    def test_an_enterprise_host_uses_its_own_api_base(self, monkeypatch):
        monkeypatch.setenv("PRXREF_GITHUB_ENTERPRISE_TOKEN", "ghes-t0ken")
        session = _session(_mock_response(json_data=_tree_body()))
        ref = _ref("https://git.corp.example/acme/api/pull/7")

        listing = ForgeImpl(session=session).list_paths(ref, sha=SHA)

        assert listing == PathListing(paths=BLOB_PATHS, complete=True)
        call = session.get.call_args
        assert call.args[0] == f"https://git.corp.example/api/v3/repos/acme/api/git/trees/{SHA}"
        assert call.kwargs["params"] == {"recursive": "1"}
        assert call.kwargs["headers"]["Authorization"] == "Bearer ghes-t0ken"

    @pytest.mark.parametrize("status", [404, 409, 500])
    def test_a_non_2xx_status_gives_none(self, status):
        session = _session(_mock_response(status, json_data={"message": "nope"}))

        assert ForgeImpl(session=session).list_paths(_ref(), sha=SHA) is None

    @pytest.mark.parametrize(
        "error", [requests.ConnectionError("down"), requests.Timeout("slow"), requests.RequestException("x")]
    )
    def test_a_request_exception_gives_none(self, error):
        session = MagicMock(spec=requests.Session)
        session.get.side_effect = error

        assert ForgeImpl(session=session).list_paths(_ref(), sha=SHA) is None

    def test_a_non_json_body_gives_none(self):
        session = _session(_mock_response(text="<html>gateway</html>"))

        assert ForgeImpl(session=session).list_paths(_ref(), sha=SHA) is None

    @pytest.mark.parametrize(
        "body",
        [
            {"sha": SHA, "truncated": False},
            {"sha": SHA, "truncated": False, "tree": None},
            {"sha": SHA, "truncated": False, "tree": {"path": "a.py", "type": "blob"}},
            [{"path": "a.py", "type": "blob"}],
        ],
        ids=["missing-tree", "null-tree", "tree-not-a-list", "body-not-an-object"],
    )
    def test_a_body_without_a_tree_list_gives_none(self, body):
        session = _session(_mock_response(json_data=body))

        assert ForgeImpl(session=session).list_paths(_ref(), sha=SHA) is None

    def test_an_empty_sha_gives_none_without_a_request(self):
        session = MagicMock(spec=requests.Session)

        assert ForgeImpl(session=session).list_paths(_ref(), sha="") is None
        session.get.assert_not_called()

    def test_failures_never_log_above_debug(self, caplog):
        session = MagicMock(spec=requests.Session)
        session.get.side_effect = [
            requests.ConnectionError("down"),
            _mock_response(500, json_data={"message": "boom"}),
            _mock_response(text="not json"),
            _mock_response(json_data={"sha": SHA}),
        ]
        forge = ForgeImpl(session=session)

        with caplog.at_level(logging.DEBUG, logger="prxref.forges.github"):
            results = [forge.list_paths(_ref(), sha=SHA) for _ in range(4)]

        assert results == [None, None, None, None]
        assert len(caplog.records) == 4
        assert all(record.levelno <= logging.DEBUG for record in caplog.records)


# --- ReplayForge and LocalDiffForge -------------------------------------------


class _ListingForge:
    """An inner forge with a listing that records every call."""

    name = "fake"

    def __init__(self, result=None, error=None):
        self.calls: list[tuple[PRRef, str]] = []
        self._result = result
        self._error = error

    def list_paths(self, ref, *, sha):
        self.calls.append((ref, sha))
        if self._error is not None:
            raise self._error
        return self._result


class _NoListingForge:
    """An inner forge with no ``list_paths`` at all."""

    name = "bare"


LISTING = PathListing(paths=BLOB_PATHS, complete=True)


class TestReplayForgeListPaths:
    def test_it_delegates_and_returns_the_inner_listing(self):
        inner = _ListingForge(result=LISTING)
        ref = _ref()

        result = ReplayForge(inner).list_paths(ref, sha=SHA)

        assert result is LISTING
        assert inner.calls == [(ref, SHA)]

    def test_it_passes_the_callers_sha_through_unchanged_under_a_pin(self):
        inner = _ListingForge(result=LISTING)
        ref = _ref()
        replay = ReplayForge(inner, base_sha="a" * 40, head_sha=SHA, diff_text="diff --git a/x b/x\n")

        replay.list_paths(ref, sha=OTHER_SHA)

        assert inner.calls == [(ref, OTHER_SHA)]

    def test_an_incomplete_or_absent_inner_listing_is_returned_as_is(self):
        partial = PathListing(paths=("a.py",), complete=False)
        assert ReplayForge(_ListingForge(result=partial)).list_paths(_ref(), sha=SHA) is partial
        assert ReplayForge(_ListingForge(result=None)).list_paths(_ref(), sha=SHA) is None

    def test_an_inner_forge_without_list_paths_gives_none(self):
        replay = ReplayForge(_NoListingForge())

        assert getattr(replay, "list_paths", None) is not None
        assert replay.list_paths(_ref(), sha=SHA) is None

    @pytest.mark.parametrize("error", [RuntimeError("boom"), requests.ConnectionError("down"), ValueError("bad")])
    def test_an_inner_forge_that_raises_gives_none(self, error):
        inner = _ListingForge(error=error)

        assert ReplayForge(inner).list_paths(_ref(), sha=SHA) is None
        assert len(inner.calls) == 1

    def test_it_delegates_to_the_github_adapter_end_to_end(self):
        session = _session(_mock_response(json_data=_tree_body()))

        listing = ReplayForge(ForgeImpl(session=session)).list_paths(_ref(), sha=SHA)

        assert listing == PathListing(paths=BLOB_PATHS, complete=True)
        assert session.get.call_args.args[0].endswith(f"/git/trees/{SHA}")


class TestLocalDiffForgeHasNoListing:
    def test_it_has_no_list_paths_attribute(self):
        forge = LocalDiffForge("diff --git a/x b/x\n", path="change.diff")

        assert getattr(forge, "list_paths", None) is None
        assert not hasattr(LocalDiffForge, "list_paths")
