"""Tests for ``list_paths`` on Bitbucket Cloud and Azure DevOps (issue #17, task T10).

Bitbucket Cloud walks the paged ``/src/{sha}/`` listing, following ``next``
verbatim; Azure DevOps answers from one ``items?recursionLevel=Full`` request.
The entry shapes are the ones the orchestrator's read-only probes observed.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest
import requests

from prxref.forges import base, bitbucket
from prxref.forges.azure_devops import ForgeImpl as AzureForge
from prxref.forges.base import PathListing, PRRef
from prxref.forges.bitbucket import ForgeImpl as BitbucketForge

SHA = "0123456789abcdef0123456789abcdef01234567"
REQUEST_TIMEOUT = (10.0, 30.0)

BB_PR_URL = "https://bitbucket.org/acme/api/pull-requests/42"
BB_SRC = f"https://api.bitbucket.org/2.0/repositories/acme/api/src/{SHA}/"

ADO_PR_URL = "https://dev.azure.com/acme/AcmeWeb/_git/AcmeWeb/pullrequest/551"
ADO_ITEMS = "https://dev.azure.com/acme/AcmeWeb/_apis/git/repositories/AcmeWeb/items"
ADO_JSON = "application/json; charset=utf-8; api-version=7.1"


@pytest.fixture(autouse=True)
def _no_forge_credentials(monkeypatch):
    """conftest clears PRXREF_* only; the Azure pipeline token is not one of them."""
    monkeypatch.delenv("SYSTEM_ACCESSTOKEN", raising=False)
    monkeypatch.delenv("PRXREF_AZURE_DEVOPS_TOKEN", raising=False)
    monkeypatch.delenv("PRXREF_BITBUCKET_TOKEN", raising=False)
    monkeypatch.delenv("PRXREF_BITBUCKET_USER", raising=False)
    monkeypatch.delenv("PRXREF_BITBUCKET_APP_PASSWORD", raising=False)


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


# --- Bitbucket Cloud -----------------------------------------------------------


def _bb_ref(url=BB_PR_URL):
    ref = BitbucketForge.parse_pr_url(url)
    assert ref is not None
    return ref


def _bb_file(path):
    return {
        "path": path,
        "commit": {"hash": SHA, "type": "commit"},
        "type": "commit_file",
        "attributes": [],
        "escaped_path": path,
        "size": 10,
        "mimetype": None,
        "links": {"self": {"href": f"{BB_SRC}{path}"}},
    }


def _bb_dir(path):
    return {
        "path": path,
        "commit": {"hash": SHA, "type": "commit"},
        "type": "commit_directory",
        "links": {"self": {"href": f"{BB_SRC}{path}/"}},
    }


def _bb_page(values, next_url=None, page=1):
    body = {"page": page, "pagelen": 100, "values": values}
    if next_url:
        body["next"] = next_url
    return _mock_response(json_data=body)


def _bb_next(token):
    return f"{BB_SRC}?max_depth=64&pagelen=100&page={token}"


class TestBitbucketCloudListPaths:
    def test_one_page_keeps_only_commit_file_paths_sorted_and_deduplicated(self, monkeypatch):
        monkeypatch.setenv("PRXREF_BITBUCKET_TOKEN", "t0ken")
        values = [
            _bb_file("src/b.py"),
            _bb_dir("src"),
            {"path": "vendor/lib", "type": "commit_submodule"},
            _bb_file("README.md"),
            _bb_file("src/a.py"),
            _bb_file("src/a.py"),
        ]
        session = _session(_bb_page(values))

        listing = BitbucketForge(session=session).list_paths(_bb_ref(), sha=SHA)

        assert listing == PathListing(paths=("README.md", "src/a.py", "src/b.py"), complete=True)
        session.get.assert_called_once()
        call = session.get.call_args
        assert call.args[0] == BB_SRC
        assert call.kwargs["params"] == {"max_depth": 64, "pagelen": 100}
        assert call.kwargs["timeout"] == REQUEST_TIMEOUT
        assert call.kwargs["headers"] == {"Authorization": "Bearer t0ken"}
        assert call.kwargs["auth"] is None

    def test_app_password_credentials_go_out_as_basic_auth(self, monkeypatch):
        monkeypatch.setenv("PRXREF_BITBUCKET_USER", "bot")
        monkeypatch.setenv("PRXREF_BITBUCKET_APP_PASSWORD", "app-pw")
        session = _session(_bb_page([_bb_file("a.py")]))

        BitbucketForge(session=session).list_paths(_bb_ref(), sha=SHA)

        call = session.get.call_args
        assert call.kwargs["headers"] == {}
        assert call.kwargs["auth"] == ("bot", "app-pw")

    def test_three_pages_follow_next_verbatim_and_union_the_paths(self, monkeypatch):
        monkeypatch.setenv("PRXREF_BITBUCKET_TOKEN", "t0ken")
        second, third = _bb_next("rXtr"), _bb_next("sYus")
        session = _session(
            _bb_page([_bb_file("c.py"), _bb_file("a.py")], next_url=second, page=1),
            _bb_page([_bb_dir("pkg"), _bb_file("pkg/b.py")], next_url=third, page=2),
            _bb_page([_bb_file("a.py"), _bb_file("pkg/d.py")], page=3),
        )

        listing = BitbucketForge(session=session).list_paths(_bb_ref(), sha=SHA)

        assert listing == PathListing(paths=("a.py", "c.py", "pkg/b.py", "pkg/d.py"), complete=True)
        calls = session.get.call_args_list
        assert [c.args[0] for c in calls] == [BB_SRC, second, third]
        assert calls[0].kwargs["params"] == {"max_depth": 64, "pagelen": 100}
        for follow_up in calls[1:]:
            assert not follow_up.kwargs.get("params")
            assert follow_up.kwargs["headers"] == {"Authorization": "Bearer t0ken"}
            assert follow_up.kwargs["timeout"] == REQUEST_TIMEOUT

    def test_the_depth_limit_is_sixty_four(self):
        assert bitbucket._LISTING_MAX_DEPTH == 64

    @pytest.mark.parametrize(
        ("directory", "complete"),
        [("a/b", False), ("a/b/", False), ("a", True)],
        ids=["at-the-limit", "at-the-limit-trailing-slash", "one-below-the-limit"],
    )
    def test_a_directory_at_the_depth_limit_marks_the_listing_incomplete(
        self, monkeypatch, directory, complete
    ):
        monkeypatch.setattr(bitbucket, "_LISTING_MAX_DEPTH", 2)
        session = _session(_bb_page([_bb_file("a/x.py"), _bb_dir(directory)]))

        listing = BitbucketForge(session=session).list_paths(_bb_ref(), sha=SHA)

        assert listing == PathListing(paths=("a/x.py",), complete=complete)
        assert session.get.call_args.kwargs["params"]["max_depth"] == 2

    def test_a_directory_at_the_default_depth_limit_marks_the_listing_incomplete(self):
        deep = "/".join(f"d{i}" for i in range(64))
        shallow = "/".join(f"d{i}" for i in range(63))
        assert deep.count("/") == 63
        session = _session(_bb_page([_bb_dir(shallow)]), _bb_page([_bb_dir(deep)]))
        forge = BitbucketForge(session=session)

        assert forge.list_paths(_bb_ref(), sha=SHA) == PathListing(paths=(), complete=True)
        assert forge.list_paths(_bb_ref(), sha=SHA) == PathListing(paths=(), complete=False)

    def test_a_directory_at_the_limit_on_an_earlier_page_still_counts(self, monkeypatch):
        monkeypatch.setattr(bitbucket, "_LISTING_MAX_DEPTH", 2)
        session = _session(
            _bb_page([_bb_dir("a/b"), _bb_file("a/x.py")], next_url=_bb_next("p2")),
            _bb_page([_bb_file("z.py")], page=2),
        )

        listing = BitbucketForge(session=session).list_paths(_bb_ref(), sha=SHA)

        assert listing == PathListing(paths=("a/x.py", "z.py"), complete=False)

    def test_the_page_cap_is_the_shared_constant(self):
        assert bitbucket.MAX_LISTING_PAGES is base.MAX_LISTING_PAGES

    def test_the_page_cap_stops_the_walk_and_marks_it_incomplete(self, monkeypatch):
        monkeypatch.setattr(bitbucket, "MAX_LISTING_PAGES", 2)
        session = _session(
            _bb_page([_bb_file("a.py")], next_url=_bb_next("p2"), page=1),
            _bb_page([_bb_file("b.py")], next_url=_bb_next("p3"), page=2),
            _bb_page([_bb_file("c.py")], page=3),
        )

        listing = BitbucketForge(session=session).list_paths(_bb_ref(), sha=SHA)

        assert session.get.call_count == 2
        assert listing == PathListing(paths=("a.py", "b.py"), complete=False)

    def test_a_walk_that_ends_on_the_last_allowed_page_is_complete(self, monkeypatch):
        monkeypatch.setattr(bitbucket, "MAX_LISTING_PAGES", 2)
        session = _session(
            _bb_page([_bb_file("a.py")], next_url=_bb_next("p2"), page=1),
            _bb_page([_bb_file("b.py")], page=2),
        )

        listing = BitbucketForge(session=session).list_paths(_bb_ref(), sha=SHA)

        assert session.get.call_count == 2
        assert listing == PathListing(paths=("a.py", "b.py"), complete=True)

    def test_malformed_entries_are_skipped(self):
        values = [
            "not-an-entry",
            {"type": "commit_file"},
            {"type": "commit_file", "path": ""},
            {"type": "commit_file", "path": 7},
            {"type": "commit_directory"},
            _bb_file("kept.py"),
        ]
        session = _session(_bb_page(values))

        listing = BitbucketForge(session=session).list_paths(_bb_ref(), sha=SHA)

        assert listing == PathListing(paths=("kept.py",), complete=True)

    def test_an_empty_listing_is_an_empty_complete_listing(self):
        session = _session(_bb_page([]))

        assert BitbucketForge(session=session).list_paths(_bb_ref(), sha=SHA) == PathListing(
            paths=(), complete=True
        )

    def test_the_sha_is_quoted_into_the_url(self):
        session = _session(_bb_page([]))

        BitbucketForge(session=session).list_paths(_bb_ref(), sha="feat/x y")

        assert session.get.call_args.args[0] == (
            "https://api.bitbucket.org/2.0/repositories/acme/api/src/feat%2Fx%20y/"
        )

    @pytest.mark.parametrize(
        "failure",
        [
            _mock_response(404, json_data={"type": "error", "error": {"message": "Commit not found"}}),
            _mock_response(500, text="boom"),
            requests.ConnectionError("down"),
            requests.Timeout("slow"),
            _mock_response(text="<html>gateway</html>"),
            _mock_response(json_data={"page": 1, "pagelen": 100}),
            _mock_response(json_data={"page": 1, "pagelen": 100, "values": None}),
            _mock_response(json_data={"page": 1, "pagelen": 100, "values": {"path": "a.py"}}),
            _mock_response(json_data=[_bb_file("a.py")]),
            _mock_response(json_data={"values": [_bb_file("a.py")], "next": 7}),
        ],
        ids=[
            "http-404", "http-500", "connection-error", "timeout", "non-json",
            "missing-values", "null-values", "values-not-a-list", "body-not-an-object", "next-not-a-string",
        ],
    )
    def test_a_first_page_failure_gives_none(self, failure):
        session = _session(failure)

        assert BitbucketForge(session=session).list_paths(_bb_ref(), sha=SHA) is None
        session.get.assert_called_once()

    @pytest.mark.parametrize(
        "failure",
        [
            _mock_response(500, text="boom"),
            requests.ConnectionError("down"),
            _mock_response(text="<html>gateway</html>"),
            _mock_response(json_data={"page": 2, "pagelen": 100}),
        ],
        ids=["http-500", "connection-error", "non-json", "missing-values"],
    )
    def test_a_later_page_failure_keeps_the_paths_so_far_as_incomplete(self, failure):
        session = _session(
            _bb_page([_bb_file("b.py"), _bb_file("a.py")], next_url=_bb_next("p2")),
            failure,
        )

        listing = BitbucketForge(session=session).list_paths(_bb_ref(), sha=SHA)

        assert listing == PathListing(paths=("a.py", "b.py"), complete=False)
        assert session.get.call_count == 2

    def test_an_empty_sha_gives_none_without_a_request(self):
        session = MagicMock(spec=requests.Session)

        assert BitbucketForge(session=session).list_paths(_bb_ref(), sha="") is None
        session.get.assert_not_called()

    def test_failures_never_log_above_debug(self, caplog):
        session = _session(
            requests.ConnectionError("down"),
            _mock_response(500, text="boom"),
            _mock_response(text="not json"),
            _mock_response(json_data={"page": 1}),
            _bb_page([_bb_file("a.py")], next_url=_bb_next("p2")),
            requests.ConnectionError("down"),
        )
        forge = BitbucketForge(session=session)

        with caplog.at_level(logging.DEBUG, logger="prxref.forges.bitbucket"):
            results = [forge.list_paths(_bb_ref(), sha=SHA) for _ in range(5)]

        assert results == [None, None, None, None, PathListing(paths=("a.py",), complete=False)]
        assert len(caplog.records) == 5
        assert all(record.levelno <= logging.DEBUG for record in caplog.records)


# --- Azure DevOps --------------------------------------------------------------


def _ado_ref(url=ADO_PR_URL):
    ref = AzureForge.parse_pr_url(url)
    assert ref is not None
    return ref


def _ado_entry(path, git_object_type="blob", is_folder=None):
    entry = {
        "objectId": "1" * 40,
        "gitObjectType": git_object_type,
        "commitId": SHA,
        "path": path,
        "url": f"{ADO_ITEMS}?path={path}",
    }
    if is_folder is not None:
        entry["isFolder"] = is_folder
    return entry


def _ado_listing(values, headers=None):
    return _mock_response(
        json_data={"count": len(values), "value": values},
        headers={"Content-Type": ADO_JSON, **(headers or {})},
    )


ADO_VALUES = [
    _ado_entry("/", "tree", is_folder=True),
    _ado_entry("/src/b.py"),
    _ado_entry("/src", "tree", is_folder=True),
    _ado_entry("/.order"),
    _ado_entry("/vendor/lib", "commit"),
    _ado_entry("/src/a.py"),
    _ado_entry("/src/a.py"),
    _ado_entry("/odd", "blob", is_folder=True),
]
ADO_PATHS = (".order", "src/a.py", "src/b.py")


class TestAzureDevOpsListPaths:
    def test_it_keeps_blob_paths_without_the_leading_slash_sorted_and_deduplicated(self):
        session = _session(_ado_listing(ADO_VALUES))

        listing = AzureForge(session=session).list_paths(_ado_ref(), sha=SHA)

        assert listing == PathListing(paths=ADO_PATHS, complete=True)
        session.get.assert_called_once()
        call = session.get.call_args
        assert call.args[0] == ADO_ITEMS
        assert call.kwargs["params"] == {
            "api-version": "7.1",
            "recursionLevel": "Full",
            "versionDescriptor.version": SHA,
            "versionDescriptor.versionType": "commit",
        }
        assert call.kwargs["timeout"] == REQUEST_TIMEOUT
        assert call.kwargs["headers"]["Accept"] == "application/json"
        assert call.kwargs["headers"]["X-TFS-FedAuthRedirect"] == "Suppress"
        assert "Authorization" not in call.kwargs["headers"]

    def test_the_personal_access_token_goes_out_as_basic_auth(self, monkeypatch):
        monkeypatch.setenv("PRXREF_AZURE_DEVOPS_TOKEN", "p4t")
        session = _session(_ado_listing(ADO_VALUES))

        AzureForge(session=session).list_paths(_ado_ref(), sha=SHA)

        assert session.get.call_args.kwargs["headers"]["Authorization"] == "Basic OnA0dA=="

    def test_the_live_probe_entry_shapes_are_read(self):
        values = [
            {"objectId": "2" * 40, "gitObjectType": "tree", "commitId": SHA, "path": "/",
             "isFolder": True, "url": ADO_ITEMS},
            {"objectId": "3" * 40, "gitObjectType": "blob", "commitId": SHA, "path": "/.order",
             "url": ADO_ITEMS},
        ]
        session = _session(_ado_listing(values))

        listing = AzureForge(session=session).list_paths(_ado_ref(), sha=SHA)

        assert listing == PathListing(paths=(".order",), complete=True)

    def test_a_continuation_token_marks_the_listing_incomplete_without_following_it(self):
        session = _session(
            _ado_listing(ADO_VALUES, headers={"x-ms-continuationtoken": "next-page"}),
            _ado_listing([_ado_entry("/late.py")]),
        )

        listing = AzureForge(session=session).list_paths(_ado_ref(), sha=SHA)

        assert listing == PathListing(paths=ADO_PATHS, complete=False)
        session.get.assert_called_once()

    def test_malformed_entries_are_skipped(self):
        values = [
            "not-an-entry",
            {"gitObjectType": "blob"},
            {"gitObjectType": "blob", "path": ""},
            {"gitObjectType": "blob", "path": "/"},
            {"gitObjectType": "blob", "path": 7},
            _ado_entry("/kept.py"),
        ]
        session = _session(_ado_listing(values))

        listing = AzureForge(session=session).list_paths(_ado_ref(), sha=SHA)

        assert listing == PathListing(paths=("kept.py",), complete=True)

    def test_an_empty_value_list_is_an_empty_complete_listing(self):
        session = _session(_ado_listing([]))

        assert AzureForge(session=session).list_paths(_ado_ref(), sha=SHA) == PathListing(
            paths=(), complete=True
        )

    @pytest.mark.parametrize(
        "failure",
        [
            _mock_response(404, json_data={"message": "TF401175"}, headers={"Content-Type": ADO_JSON}),
            _mock_response(401, text="", headers={"Content-Type": ADO_JSON}),
            _mock_response(500, text="boom", headers={"Content-Type": "text/plain"}),
            _mock_response(203, text="<html>sign in</html>", headers={"Content-Type": "text/html"}),
            _mock_response(200, text="<html>sign in</html>", headers={"Content-Type": "text/html; charset=utf-8"}),
            requests.ConnectionError("reset"),
            requests.Timeout("slow"),
            _mock_response(200, text="{not json", headers={"Content-Type": ADO_JSON}),
            _mock_response(json_data={"count": 0}, headers={"Content-Type": ADO_JSON}),
            _mock_response(json_data={"count": 0, "value": None}, headers={"Content-Type": ADO_JSON}),
            _mock_response(json_data={"count": 1, "value": {"path": "/a.py"}}, headers={"Content-Type": ADO_JSON}),
            _mock_response(json_data=[_ado_entry("/a.py")], headers={"Content-Type": ADO_JSON}),
        ],
        ids=[
            "http-404", "http-401", "http-500", "http-203-sign-in", "http-200-html", "connection-error",
            "timeout", "non-json", "missing-value", "null-value", "value-not-a-list", "body-not-an-object",
        ],
    )
    def test_a_failure_gives_none(self, failure):
        session = _session(failure)

        assert AzureForge(session=session).list_paths(_ado_ref(), sha=SHA) is None
        session.get.assert_called_once()

    def test_an_empty_sha_gives_none_without_a_request(self):
        session = MagicMock(spec=requests.Session)

        assert AzureForge(session=session).list_paths(_ado_ref(), sha="") is None
        session.get.assert_not_called()

    def test_a_foreign_ref_gives_none_without_a_request(self):
        ref = PRRef(forge="azure-devops", host="github.com", owner="o", repo="r", number=1,
                    url="https://github.com/o/r/pull/1")
        session = MagicMock(spec=requests.Session)

        assert AzureForge(session=session).list_paths(ref, sha=SHA) is None
        session.get.assert_not_called()

    def test_failures_never_log_above_debug(self, caplog):
        session = _session(
            requests.ConnectionError("reset"),
            _mock_response(404, json_data={"message": "TF401175"}, headers={"Content-Type": ADO_JSON}),
            _mock_response(203, text="<html>sign in</html>", headers={"Content-Type": "text/html"}),
            _mock_response(json_data={"count": 0}, headers={"Content-Type": ADO_JSON}),
            _ado_listing(ADO_VALUES, headers={"x-ms-continuationtoken": "next-page"}),
        )
        forge = AzureForge(session=session)

        with caplog.at_level(logging.DEBUG, logger="prxref.forges.azure_devops"):
            results = [forge.list_paths(_ado_ref(), sha=SHA) for _ in range(5)]

        assert results == [None, None, None, None, PathListing(paths=ADO_PATHS, complete=False)]
        assert len(caplog.records) == 5
        assert all(record.levelno <= logging.DEBUG for record in caplog.records)
