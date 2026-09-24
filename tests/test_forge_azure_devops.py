"""Tests for the Azure DevOps Services / Server forge adapter.

The recorded payloads under ``fixtures/azure_devops/`` are real public API
responses, trimmed to the keys the adapter reads and scrubbed to placeholder
names. Blob bodies are small synthetic bytes. Blob fetches run on a thread
pool, so the session double answers by URL, never from an ordered list.
"""
from __future__ import annotations

import base64
import inspect
import json
import logging
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import FunctionType
from typing import Any, NamedTuple
from unittest.mock import MagicMock

import pytest
import requests

from prxref.config import make_forge
from prxref.forges import azure_devops, bitbucket, bitbucket_server, github, gitlab
from prxref.forges.azure_devops import ForgeImpl, _make_retry_session
from prxref.forges.base import (
    ATTRIBUTION_MARKER,
    SUMMARY_MARKER,
    FeedReadError,
    InlineComment,
    PRRef,
    detect_forge,
)
from prxref.retry_logging import LoggingRetry
from prxref.triage import FileDiff, parse_unified_diff

FIXTURES = Path(__file__).parent / "fixtures" / "azure_devops"
LOGGER = "prxref.forges.azure_devops"

PR_URL = "https://dev.azure.com/acme/AcmeWeb/_git/AcmeWeb/pullrequest/551"
BASE = "https://dev.azure.com/acme/AcmeWeb/_apis/git/repositories/AcmeWeb"
PR_API = "/pullrequests/551"

PR9_URL = "https://dev.azure.com/acme/Acme%20Project/_git/Acme%20Project/pullrequest/9"
PR9_BASE = "https://dev.azure.com/acme/Acme%20Project/_apis/git/repositories/Acme%20Project"

SOURCE_551 = "41449294c4fc8ffdfb063a9176b560dc70ef7dd0"
TARGET_551 = "d82bd8eda4e7e90fd2dece6e67e13fc8c9d2dbe7"


@pytest.fixture(autouse=True)
def _no_ado_credentials(monkeypatch):
    """conftest clears PRXREF_* only; the pipeline token is not one of them."""
    monkeypatch.delenv("SYSTEM_ACCESSTOKEN", raising=False)
    monkeypatch.delenv("PRXREF_AZURE_DEVOPS_TOKEN", raising=False)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


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
    resp.raise_for_status.side_effect = None if resp.ok else requests.HTTPError(response=resp)
    return resp


def _json(data: Any, status: int = 200) -> MagicMock:
    return _mock_response(
        status, json_data=data, headers={"Content-Type": "application/json; charset=utf-8; api-version=7.1"}
    )


def _bytes(data: bytes, status: int = 200) -> MagicMock:
    resp = _mock_response(
        status,
        text=data.decode("utf-8", errors="replace"),
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )
    resp.iter_content.side_effect = lambda chunk_size=1, decode_unicode=False: iter(
        [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
    )
    return resp


def _status(status: int, text: str = "") -> MagicMock:
    return _mock_response(status, text=text, headers={"Content-Type": "application/json"})


class _Call(NamedTuple):
    verb: str
    url: str
    path: str
    params: dict
    headers: dict
    body: Any
    stream: bool


class _Ado:
    """A ``requests.Session`` double that answers by verb and URL path.

    Routes are keyed on the path below ``base`` (the repository API root). An
    unrouted request raises ``AssertionError``, so a call the adapter should
    not have made fails the test loudly, from whichever thread made it.
    """

    def __init__(self, base: str = BASE) -> None:
        self.base = base
        self.routes: dict[tuple[str, str], Any] = {}
        self.calls: list[_Call] = []
        self._lock = threading.Lock()
        self.session = MagicMock(spec=requests.Session)
        self.session.get.side_effect = lambda url, **kw: self._answer("GET", url, kw)
        self.session.post.side_effect = lambda url, **kw: self._answer("POST", url, kw)
        self.session.patch.side_effect = lambda url, **kw: self._answer("PATCH", url, kw)
        self.session.delete.side_effect = lambda url, **kw: self._answer("DELETE", url, kw)

    def on(self, verb: str, path: str, reply: Any) -> None:
        """Answer ``verb path`` with a response, or with ``reply(params, body)``."""
        self.routes[(verb, path)] = reply if isinstance(reply, FunctionType) else (lambda *_: reply)

    def _answer(self, verb: str, url: str, kw: dict) -> Any:
        assert url.startswith(self.base), url
        path = url[len(self.base):]
        params = dict(kw.get("params") or {})
        call = _Call(verb, url, path, params, dict(kw.get("headers") or {}), kw.get("json"), kw.get("stream", False))
        with self._lock:
            self.calls.append(call)
        handler = self.routes.get((verb, path))
        if handler is None:
            raise AssertionError(f"unrouted {verb} {path}")
        return handler(params, kw.get("json"))

    def of(self, verb: str, path: str | None = None) -> list[_Call]:
        return [c for c in self.calls if c.verb == verb and (path is None or c.path == path)]

    def blob_gets(self) -> list[str]:
        return [c.path[len("/blobs/"):] for c in self.calls if c.verb == "GET" and c.path.startswith("/blobs/")]


def _ref(url: str = PR_URL) -> PRRef:
    ref = ForgeImpl.parse_pr_url(url)
    assert ref is not None
    return ref


def _blob_change(change_type: str, path: str, *, new: str | None = None, old: str | None = None,
                 source: str | None = None, kind: str = "blob") -> dict:
    item: dict[str, Any] = {"gitObjectType": kind, "path": path}
    if new is not None:
        item["objectId"] = new
    if old is not None:
        item["originalObjectId"] = old
    change: dict[str, Any] = {"changeType": change_type, "item": item}
    if source is not None:
        change["sourceServerItem"] = source
    return change


def _synthetic_blobs(changes: list[dict]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for change in changes:
        item = change["item"]
        if item.get("gitObjectType") != "blob":
            continue
        for oid in (item.get("objectId"), item.get("originalObjectId")):
            if oid:
                out[oid] = f"shared context\n{oid[:12]}\n".encode()
    return out


def _serve_diff(ado: _Ado, changes: list[dict], blobs: dict[str, bytes], *, pr: dict | None = None,
                pr_path: str = PR_API) -> None:
    ado.on("GET", pr_path, _json(pr if pr is not None else _load("pr-551.json")))
    ado.on("GET", "/diffs/commits", _json({"allChangesIncluded": True, "changes": changes}))
    for oid, data in blobs.items():
        ado.on("GET", f"/blobs/{oid}", _bytes(data))


def _diff_of(changes: list[dict], blobs: dict[str, bytes]) -> tuple[str, list[FileDiff], _Ado]:
    ado = _Ado()
    _serve_diff(ado, changes, blobs)
    text = ForgeImpl(session=ado.session).get_diff(_ref())
    return text, parse_unified_diff(text), ado


def _assert_hunk_counts_consistent(files: list[FileDiff]) -> None:
    for f in files:
        for h in f.hunks:
            assert sum(1 for ln in h.lines if ln.kind in "- ") == h.old_count, f.path
            assert sum(1 for ln in h.lines if ln.kind in "+ ") == h.new_count, f.path


# --- URL parsing --------------------------------------------------------------


def test_parses_dev_azure_com_url():
    ref = ForgeImpl.parse_pr_url(PR_URL)

    assert ref == PRRef(
        forge="azure-devops", host="dev.azure.com", owner="AcmeWeb", repo="AcmeWeb", number=551, url=PR_URL
    )


def test_parses_short_form_with_project_equal_to_repo():
    ref = ForgeImpl.parse_pr_url("https://dev.azure.com/acme/_git/AcmeWeb/pullrequest/551")

    assert ref is not None
    assert (ref.owner, ref.repo, ref.number) == ("AcmeWeb", "AcmeWeb", 551)
    assert ref.url == PR_URL


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        (
            "https://acme.visualstudio.com/AcmeWeb/_git/AcmeWeb/pullrequest/551",
            "https://acme.visualstudio.com/AcmeWeb/_git/AcmeWeb/pullrequest/551",
        ),
        (
            "https://acme.visualstudio.com/DefaultCollection/AcmeWeb/_git/AcmeWeb/pullrequest/551",
            "https://acme.visualstudio.com/DefaultCollection/AcmeWeb/_git/AcmeWeb/pullrequest/551",
        ),
        (
            "https://acme.visualstudio.com/_git/AcmeWeb/pullrequest/551",
            "https://acme.visualstudio.com/AcmeWeb/_git/AcmeWeb/pullrequest/551",
        ),
    ],
)
def test_parses_visualstudio_com_with_and_without_defaultcollection(url, normalized):
    ref = ForgeImpl.parse_pr_url(url)

    assert ref is not None
    assert ref.host == "acme.visualstudio.com"
    assert (ref.owner, ref.repo, ref.number) == ("AcmeWeb", "AcmeWeb", 551)
    assert ref.url == normalized


def test_parses_percent_encoded_names():
    ref = ForgeImpl.parse_pr_url(PR9_URL)

    assert ref is not None
    assert ref.owner == "Acme Project"
    assert ref.repo == "Acme Project"
    assert ref.url == PR9_URL


def test_ignores_query_and_fragment():
    ref = ForgeImpl.parse_pr_url(PR_URL + "?_a=files&path=/x#top")

    assert ref is not None
    assert ref.url == PR_URL


def test_parses_on_prem_collection_path_and_keeps_http_scheme():
    tfs = ForgeImpl.parse_pr_url("https://tfs.example.com/tfs/DefaultCollection/Proj/_git/Repo/pullrequest/7")
    plain = ForgeImpl.parse_pr_url("http://ado.example.com:8080/Coll/Proj/_git/Repo/pullrequest/7/")

    assert tfs is not None and plain is not None
    assert (tfs.host, tfs.owner, tfs.repo, tfs.number) == ("tfs.example.com", "Proj", "Repo", 7)
    assert tfs.url == "https://tfs.example.com/tfs/DefaultCollection/Proj/_git/Repo/pullrequest/7"
    assert plain.host == "ado.example.com:8080"
    assert plain.url == "http://ado.example.com:8080/Coll/Proj/_git/Repo/pullrequest/7"


def test_uppercase_scheme_and_route_segments_normalize():
    ref = ForgeImpl.parse_pr_url("HTTPS://dev.azure.com/acme/AcmeWeb/_GIT/AcmeWeb/PullRequest/551")

    assert ref is not None
    assert ref.url == PR_URL


def test_rejects_ambiguous_on_prem_short_form():
    assert ForgeImpl.parse_pr_url("https://tfs.example.com/Coll/_git/Repo/pullrequest/7") is None


def test_rejects_three_segments_on_dev_azure_com():
    assert ForgeImpl.parse_pr_url("https://dev.azure.com/org/a/b/_git/r/pullrequest/1") is None


def test_rejects_two_projects_on_visualstudio_com():
    assert ForgeImpl.parse_pr_url("https://acme.visualstudio.com/a/b/_git/r/pullrequest/1") is None


def test_rejects_a_host_that_will_not_parse():
    assert ForgeImpl.parse_pr_url("https://[bad/acme/AcmeWeb/_git/AcmeWeb/pullrequest/551") is None


_ADO_URLS = [
    PR_URL,
    PR_URL + "?_a=files&path=/x",
    PR9_URL,
    "https://dev.azure.com/acme/_git/AcmeWeb/pullrequest/551",
    "https://acme.visualstudio.com/AcmeWeb/_git/AcmeWeb/pullrequest/551",
    "https://acme.visualstudio.com/DefaultCollection/AcmeWeb/_git/AcmeWeb/pullrequest/551",
    "https://acme.visualstudio.com/_git/AcmeWeb/pullrequest/551",
    "https://tfs.example.com/tfs/DefaultCollection/Proj/_git/Repo/pullrequest/7",
    "http://ado.example.com:8080/Coll/Proj/_git/Repo/pullrequest/7/",
]
_NOT_ADO_URLS = [
    "https://dev.azure.com/org/a/b/_git/r/pullrequest/1",
    "https://tfs.example.com/Coll/_git/Repo/pullrequest/7",
    "https://dev.azure.com/org/proj/_git/repo/pullrequests",
    "https://dev.azure.com/org/proj/_git/repo/commit/abc",
]
_OTHER_FORGE_URLS = [
    ("https://github.com/o/r/pull/1", "github"),
    ("https://gitlab.com/g/_git/r/-/merge_requests/1", "gitlab"),
    ("https://bitbucket.org/w/r/pull-requests/1", "bitbucket"),
    ("https://bb.example.com/projects/K/repos/r/pull-requests/1", "bitbucket-server"),
]
_OTHER_PARSERS = [bitbucket.ForgeImpl, bitbucket_server.ForgeImpl, github.ForgeImpl, gitlab.ForgeImpl]


@pytest.mark.parametrize("url", _ADO_URLS)
def test_no_other_forge_claims_an_ado_url(url):
    assert ForgeImpl.parse_pr_url(url) is not None
    assert [impl.__module__ for impl in _OTHER_PARSERS if impl.parse_pr_url(url) is not None] == []


@pytest.mark.parametrize("url", _NOT_ADO_URLS + [u for u, _ in _OTHER_FORGE_URLS])
def test_the_ado_parser_claims_none_of_the_rest(url):
    assert ForgeImpl.parse_pr_url(url) is None


@pytest.mark.parametrize(("url", "forge"), _OTHER_FORGE_URLS)
def test_other_forge_urls_still_route_to_their_own_adapter(url, forge):
    ref = detect_forge(url)

    assert ref is not None
    assert ref.forge == forge


@pytest.mark.parametrize("url", _ADO_URLS)
def test_normalized_url_round_trips(url):
    ref = _ref(url)

    assert _ref(ref.url) == ref


def test_webhook_style_url_parses_from_a_recorded_web_url():
    """A service hook carries ``repository.webUrl``; the PR URL is that plus ``/pullrequest/N``."""
    pr = _load("pr-551.json")

    ref = detect_forge(f"{pr['repository']['webUrl']}/pullrequest/{pr['pullRequestId']}")

    assert ref is not None
    assert ref.forge == "azure-devops"
    assert ref.url == PR_URL


@pytest.mark.parametrize(
    ("web_url", "number", "owner", "repo", "normalized"),
    [
        (
            "https://dev.azure.com/acme/Platform/_git/api", 551, "Platform", "api",
            "https://dev.azure.com/acme/Platform/_git/api/pullrequest/551",
        ),
        (
            "https://dev.azure.com/acme/Acme%20Project/_git/Acme%20Repo", 9, "Acme Project", "Acme Repo",
            "https://dev.azure.com/acme/Acme%20Project/_git/Acme%20Repo/pullrequest/9",
        ),
        (
            "https://acme.visualstudio.com/DefaultCollection/_git/Repo", 3, "Repo", "Repo",
            "https://acme.visualstudio.com/DefaultCollection/Repo/_git/Repo/pullrequest/3",
        ),
        (
            "http://ado.example.com:8080/tfs/DefaultCollection/Project/_git/Repo", 12, "Project", "Repo",
            "http://ado.example.com:8080/tfs/DefaultCollection/Project/_git/Repo/pullrequest/12",
        ),
    ],
)
def test_webhook_style_url_parses(web_url, number, owner, repo, normalized):
    """Every URL shape the webhook queues (``webUrl + /pullrequest/{id}``) routes here."""
    ref = detect_forge(f"{web_url}/pullrequest/{number}")

    assert ref is not None
    assert (ref.forge, ref.owner, ref.repo, ref.number, ref.url) == ("azure-devops", owner, repo, number, normalized)
    assert _ref(ref.url) == ref


@pytest.mark.parametrize("url", _ADO_URLS)
def test_detect_forge_routes_ado_urls_here(url):
    ref = detect_forge(url)

    assert ref is not None
    assert ref.forge == "azure-devops"


def test_make_forge_builds_the_ado_adapter():
    session = MagicMock(spec=requests.Session)

    forge = make_forge(_ref(), session=session)

    assert isinstance(forge, azure_devops.ForgeImpl)
    assert forge.name == "azure-devops"
    assert forge._session is session


def test_adapter_implements_get_compare_diff():
    method = getattr(ForgeImpl(), "get_compare_diff", None)

    assert callable(method)
    params = inspect.signature(method).parameters
    assert params["base_sha"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["head_sha"].kind is inspect.Parameter.KEYWORD_ONLY


def test_adapter_implements_the_optional_protocol_methods():
    forge = ForgeImpl()

    assert callable(getattr(forge, "get_file_content", None))
    assert callable(getattr(forge, "prune_inline_comments", None))


# --- API base -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "base"),
    [
        (PR9_URL, PR9_BASE),
        (
            "https://acme.visualstudio.com/_git/AcmeWeb/pullrequest/9",
            "https://acme.visualstudio.com/AcmeWeb/_apis/git/repositories/AcmeWeb",
        ),
        (
            "https://acme.visualstudio.com/DefaultCollection/AcmeWeb/_git/AcmeWeb/pullrequest/9",
            "https://acme.visualstudio.com/DefaultCollection/AcmeWeb/_apis/git/repositories/AcmeWeb",
        ),
        (
            "http://ado.example.com:8080/tfs/Coll/Proj/_git/Repo/pullrequest/9",
            "http://ado.example.com:8080/tfs/Coll/Proj/_apis/git/repositories/Repo",
        ),
    ],
)
def test_api_base_is_project_scoped_and_requoted(url, base):
    ado = _Ado(base=base)
    ado.on("GET", "/pullrequests/9", _json(_load("pr-9.json")))

    ForgeImpl(session=ado.session).get_pr(_ref(url))

    (call,) = ado.calls
    assert call.url == f"{base}/pullrequests/9"
    assert call.params == {"api-version": "7.1"}


def test_visualstudio_api_base_has_no_org_segment():
    ado = _Ado(base="https://acme.visualstudio.com/AcmeWeb/_apis/git/repositories/AcmeWeb")
    ado.on("GET", "/pullrequests/551", _json(_load("pr-551.json")))

    ForgeImpl(session=ado.session).get_pr(_ref("https://acme.visualstudio.com/AcmeWeb/_git/AcmeWeb/pullrequest/551"))

    assert "/acme/" not in ado.calls[0].url


# --- auth ---------------------------------------------------------------------


def _headers_of_one_read() -> dict:
    ado = _Ado()
    ado.on("GET", PR_API, _json(_load("pr-551.json")))
    ForgeImpl(session=ado.session).get_pr(_ref())
    return ado.calls[0].headers


def test_pat_is_basic_with_empty_user(monkeypatch):
    monkeypatch.setenv("PRXREF_AZURE_DEVOPS_TOKEN", "not-a-real-pat")

    headers = _headers_of_one_read()

    assert headers["Authorization"] == "Basic " + base64.b64encode(b":not-a-real-pat").decode()


def test_system_accesstoken_is_bearer_when_no_pat(monkeypatch):
    monkeypatch.setenv("SYSTEM_ACCESSTOKEN", "not-a-real-pipeline-token")

    headers = _headers_of_one_read()

    assert headers["Authorization"] == "Bearer not-a-real-pipeline-token"


def test_pat_wins_over_system_accesstoken(monkeypatch):
    monkeypatch.setenv("PRXREF_AZURE_DEVOPS_TOKEN", "not-a-real-pat")
    monkeypatch.setenv("SYSTEM_ACCESSTOKEN", "not-a-real-pipeline-token")

    headers = _headers_of_one_read()

    assert headers["Authorization"].startswith("Basic ")


def test_anonymous_sends_no_authorization():
    assert "Authorization" not in _headers_of_one_read()


@pytest.mark.parametrize("env", [{}, {"PRXREF_AZURE_DEVOPS_TOKEN": "p"}, {"SYSTEM_ACCESSTOKEN": "s"}])
def test_fedauth_suppress_header_always_sent(monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    headers = _headers_of_one_read()

    assert headers["X-TFS-FedAuthRedirect"] == "Suppress"
    assert headers["Accept"] == "application/json"


def test_credentials_are_read_at_call_time(monkeypatch):
    forge = ForgeImpl(session=MagicMock(spec=requests.Session))
    monkeypatch.setenv("PRXREF_AZURE_DEVOPS_TOKEN", "late")

    assert forge._headers()["Authorization"] == "Basic " + base64.b64encode(b":late").decode()


@pytest.mark.parametrize(
    "reply",
    [
        _mock_response(203, text="<html>sign in</html>", headers={"Content-Type": "text/html"}),
        _mock_response(200, text="<html>sign in</html>", headers={"Content-Type": "text/html; charset=utf-8"}),
    ],
)
def test_non_json_203_is_refused_with_token_hint(reply):
    ado = _Ado()
    ado.on("GET", PR_API, reply)

    with pytest.raises(ValueError, match="PRXREF_AZURE_DEVOPS_TOKEN"):
        ForgeImpl(session=ado.session).get_pr(_ref())


def test_a_json_array_is_refused():
    ado = _Ado()
    ado.on("GET", PR_API, _json([1, 2]))

    with pytest.raises(ValueError, match="list"):
        ForgeImpl(session=ado.session).get_pr(_ref())


def test_anonymous_401_raises():
    ado = _Ado()
    ado.on("GET", PR_API, _status(401, '{"message": "TF400813"}'))

    with pytest.raises(requests.HTTPError):
        ForgeImpl(session=ado.session).get_pr(_ref())


# --- get_pr -------------------------------------------------------------------


def test_get_pr_maps_fields_from_recorded_551():
    ado = _Ado()
    ado.on("GET", PR_API, _json(_load("pr-551.json")))

    pr = ForgeImpl(session=ado.session).get_pr(_ref())

    assert pr.title == "Added keyvault cleanup task"
    assert pr.description == "Added keyvault cleanup task"
    assert pr.author == "Example User"
    assert pr.source_branch == "feature/keyvaultcleanup"
    assert pr.target_branch == "master"
    assert pr.source_sha == SOURCE_551
    assert pr.target_sha == TARGET_551
    assert pr.raw["pullRequestId"] == 551


def test_get_pr_missing_description_is_empty():
    ado = _Ado(base=PR9_BASE)
    ado.on("GET", "/pullrequests/9", _json(_load("pr-9.json")))

    pr = ForgeImpl(session=ado.session).get_pr(_ref(PR9_URL))

    assert "description" not in pr.raw
    assert pr.description == ""
    assert pr.title == "pr6"
    assert (pr.source_branch, pr.target_branch) == ("feature-branch9", "feature-branch3")


# --- diff reconstruction ------------------------------------------------------


def test_diff_add_edit_delete_from_recorded_551():
    changes = _load("diffs-551.json")["changes"]

    text, files, ado = _diff_of(changes, _synthetic_blobs(changes))

    assert Counter(f.status for f in files) == Counter({"modified": 5, "added": 1, "removed": 1})
    (removed,) = [f for f in files if f.status == "removed"]
    assert removed.path == "AcmeWeb/AcmeWeb.Environment.ARMTemplate/PowerShell/KeyVault - Copy.ps1"
    assert removed.new_path is None
    assert removed.lines_removed == 2
    assert "+++ /dev/null" in text
    (added,) = [f for f in files if f.status == "added"]
    assert added.path == "AcmeWeb/AcmeWeb.Environment.ARMTemplate/PowerShell/KeyVaultCleanup.ps1"
    assert added.old_path is None
    assert added.lines_added == 2
    for f in files:
        assert not f.path.startswith("/")
        if f.status == "modified":
            assert (f.lines_added, f.lines_removed) == (1, 1)
    _assert_hunk_counts_consistent(files)
    shared = "87d2fbfd"
    assert sum(1 for oid in ado.blob_gets() if oid.startswith(shared)) == 1


def test_blob_fetch_asks_for_raw_octets():
    changes = [_blob_change("edit", "/a.txt", new="n1", old="o1")]

    _, _, ado = _diff_of(changes, {"n1": b"x\n", "o1": b"y\n"})

    for call in ado.of("GET"):
        if call.path.startswith("/blobs/"):
            assert call.params == {"api-version": "7.1", "$format": "octetstream"}
            assert call.headers["Accept"] == "application/octet-stream"
            assert call.stream is True


def test_pure_rename_emits_similarity_and_fetches_nothing():
    changes = _load("diffs-482-trimmed.json")["changes"]
    renamed_oids = {
        c["item"]["objectId"] for c in changes if c["changeType"] == "rename"
    }

    text, files, ado = _diff_of(changes, _synthetic_blobs(changes))

    assert text.count("similarity index 100%") == 2
    renames = {f.old_path: f.new_path for f in files if f.status == "renamed"}
    assert renames == {
        "AcmeWeb/AcmeWeb2.Web/appsettings.Development.json": "AcmeWeb/AcmeWeb.Service2/appsettings.Development.json",
        "AcmeWeb/AcmeWeb2.Web/ApplicationInsightsFilter.cs": "AcmeWeb/AcmeWeb.Web2/ApplicationInsightsFilter.cs",
    }
    assert all(not f.hunks for f in files if f.status == "renamed")
    assert renamed_oids.isdisjoint(ado.blob_gets())


def test_source_rename_half_is_dropped():
    changes = _load("diffs-482-trimmed.json")["changes"]

    _, files, _ = _diff_of(changes, _synthetic_blobs(changes))

    assert [f.status for f in files] == ["renamed", "added", "renamed", "modified"]
    assert "removed" not in {f.status for f in files}


def test_rename_with_edit_has_hunks_and_rename_lines():
    changes = [_blob_change("edit, rename", "/src/new.py", new="n1", old="o1", source="/src/old.py")]

    text, files, _ = _diff_of(changes, {"o1": b"a\nb\nc\n", "n1": b"a\nB\nc\n"})

    assert "rename from src/old.py\nrename to src/new.py\n" in text
    assert "similarity index" not in text
    (f,) = files
    assert (f.status, f.old_path, f.new_path) == ("renamed", "src/old.py", "src/new.py")
    assert (f.lines_added, f.lines_removed) == (1, 1)


def test_rename_source_falls_back_to_original_path():
    change = _blob_change("rename", "/src/new.py", new="n1", old="n1")
    change["originalPath"] = "/src/old.py"

    _, files, _ = _diff_of([change], {})

    assert (files[0].old_path, files[0].new_path) == ("src/old.py", "src/new.py")


def test_undelete_and_branch_count_as_added():
    changes = [
        _blob_change("undelete", "/back.txt", new="n1"),
        _blob_change("branch", "/branched.txt", new="n2"),
    ]

    _, files, _ = _diff_of(changes, {"n1": b"x\n", "n2": b"y\n"})

    assert [f.status for f in files] == ["added", "added"]


def test_binary_by_extension_is_not_downloaded():
    changes = _load("diffs-9.json")["changes"]
    binary = {
        c["item"]["objectId"] for c in changes if c["item"]["path"].endswith((".png", ".mov"))
    }
    ado = _Ado(base=PR9_BASE)
    blobs = {oid: data for oid, data in _synthetic_blobs(changes).items() if oid not in binary}
    _serve_diff(ado, changes, blobs, pr=_load("pr-9.json"), pr_path="/pullrequests/9")

    text = ForgeImpl(session=ado.session).get_diff(_ref(PR9_URL))
    files = parse_unified_diff(text)

    assert len(binary) == 2
    assert binary.isdisjoint(ado.blob_gets())
    flagged = {f.path for f in files if f.is_binary}
    assert flagged == {"Screen Recording 2022-09-28 at 2.55.55 PM.mov", "Test/Screenshot 2022-09-28 at 1.08.37 PM.png"}
    assert "Binary files /dev/null and b/Test/Screenshot 2022-09-28 at 1.08.37 PM.png differ" in text
    assert len(files) == 9
    assert all(f.status == "added" for f in files)


def test_binary_by_nul_sniff():
    changes = [_blob_change("edit", "/data.bin2", new="n1", old="o1")]

    text, files, _ = _diff_of(changes, {"o1": b"abc\n", "n1": b"ab\x00c\n"})

    assert "Binary files a/data.bin2 and b/data.bin2 differ" in text
    assert files[0].is_binary
    assert files[0].hunks == []


def test_paths_with_spaces_and_b_slash_parse_exactly():
    changes = [_blob_change("edit", "/docs/a b/c.txt", new="n1", old="o1")]

    _, files, _ = _diff_of(changes, {"o1": b"one\n", "n1": b"two\n"})

    (f,) = files
    assert (f.path, f.old_path, f.new_path) == ("docs/a b/c.txt", "docs/a b/c.txt", "docs/a b/c.txt")


def test_no_newline_at_eof_marker_and_trailing_newline_change_is_a_hunk():
    changes = [_blob_change("edit", "/eof.txt", new="n1", old="o1")]

    text, files, _ = _diff_of(changes, {"o1": b"a\nb", "n1": b"a\nb\n"})

    assert "-b\n\\ No newline at end of file\n+b\n" in text
    (f,) = files
    assert (f.lines_added, f.lines_removed) == (1, 1)
    _assert_hunk_counts_consistent(files)


def test_crlf_content_diffs_without_carriage_returns():
    changes = [_blob_change("edit", "/win.txt", new="n1", old="o1")]

    text, files, _ = _diff_of(changes, {"o1": b"x\r\ny\r\n", "n1": b"x\r\nz\r\n"})

    assert "\r" not in text
    assert [(ln.kind, ln.text) for ln in files[0].hunks[0].lines] == [(" ", "x"), ("-", "y"), ("+", "z")]


def test_formfeed_line_keeps_hunk_counts_consistent():
    changes = [_blob_change("edit", "/ff.txt", new="n1", old="o1")]

    _, files, _ = _diff_of(changes, {"o1": b"a\x0cb\nc\n", "n1": b"a\x0cb\nd\n"})

    _assert_hunk_counts_consistent(files)
    (added,) = [ln for ln in files[0].hunks[0].lines if ln.kind == "+"]
    assert (added.text, added.new_line) == ("d", 3)


def test_bom_is_preserved_on_both_sides():
    changes = [_blob_change("edit", "/bom.txt", new="n1", old="o1")]

    _, files, _ = _diff_of(changes, {"o1": b"\xef\xbb\xbfa\nb\n", "n1": b"\xef\xbb\xbfa\nc\n"})

    first = files[0].hunks[0].lines[0]
    assert (first.kind, first.text) == (" ", "﻿a")


def test_blob_over_cap_is_header_only_with_warning(monkeypatch, caplog):
    monkeypatch.setattr(azure_devops, "_MAX_BLOB_BYTES", 10)
    changes = [_blob_change("edit", "/big.txt", new="n1", old="o1")]

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        text, files, _ = _diff_of(changes, {"o1": b"x" * 50 + b"\n", "n1": b"y\n"})

    assert "--- a/big.txt\n+++ b/big.txt\n" in text
    assert files[0].hunks == []
    assert not files[0].is_binary
    assert "big.txt" in caplog.text


@pytest.mark.parametrize(("knob", "value"), [("_MAX_CONTENT_FILES", 8), ("_MAX_TOTAL_BYTES", 1)])
def test_file_budget_makes_the_rest_header_only_with_one_warning(monkeypatch, caplog, knob, value):
    monkeypatch.setattr(azure_devops, knob, value)
    changes = [_blob_change("edit", f"/f{i:02}.txt", new=f"n{i:02}", old=f"o{i:02}") for i in range(20)]

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _, files, ado = _diff_of(changes, _synthetic_blobs(changes))

    assert [bool(f.hunks) for f in files] == [True] * 8 + [False] * 12
    assert len(ado.blob_gets()) == 16
    budget_lines = [r.getMessage() for r in caplog.records if "content budget" in r.getMessage()]
    assert budget_lines == ["Azure DevOps diff: 12 file(s) past the content budget are header-only"]


def test_missing_object_id_is_header_only_with_warning(caplog):
    changes = [_blob_change("edit", "/half.txt", new="n1")]

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _, files, ado = _diff_of(changes, {})

    assert files[0].hunks == []
    assert ado.blob_gets() == []
    assert "half.txt" in caplog.text


@pytest.mark.parametrize("status", [404, 410])
def test_blob_404_is_header_only(caplog, status):
    ado = _Ado()
    changes = [_blob_change("edit", "/gone.txt", new="n1", old="o1")]
    _serve_diff(ado, changes, {"o1": b"a\n"})
    ado.on("GET", "/blobs/n1", _status(status))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        files = parse_unified_diff(ForgeImpl(session=ado.session).get_diff(_ref()))

    assert files[0].path == "gone.txt"
    assert files[0].hunks == []
    assert "gone.txt" in caplog.text


@pytest.mark.parametrize("status", [401, 500])
def test_blob_401_raises(status):
    """A failed blob read never degrades into an empty, approving review."""
    ado = _Ado()
    changes = [_blob_change("edit", "/a.txt", new="n1", old="o1")]
    _serve_diff(ado, changes, {"o1": b"a\n"})
    ado.on("GET", "/blobs/n1", _status(status))

    with pytest.raises(requests.HTTPError):
        ForgeImpl(session=ado.session).get_diff(_ref())


def test_listing_pages_until_all_changes_included():
    pages = {
        0: [_blob_change("add", "/p0a.txt", new="a0"), _blob_change("add", "/p0b.txt", new="b0")],
        2: [_blob_change("add", "/p1a.txt", new="a1"), _blob_change("add", "/p1b.txt", new="b1")],
        4: [_blob_change("add", "/p2a.txt", new="a2")],
    }
    ado = _Ado()
    ado.on("GET", PR_API, _json(_load("pr-551.json")))
    ado.on(
        "GET",
        "/diffs/commits",
        lambda params, _body: _json({"allChangesIncluded": params["$skip"] == 4, "changes": pages[params["$skip"]]}),
    )
    for oid in ("a0", "b0", "a1", "b1", "a2"):
        ado.on("GET", f"/blobs/{oid}", _bytes(b"x\n"))

    files = parse_unified_diff(ForgeImpl(session=ado.session).get_diff(_ref()))

    listing = ado.of("GET", "/diffs/commits")
    assert [c.params["$skip"] for c in listing] == [0, 2, 4]
    assert {c.params["$top"] for c in listing} == {1000}
    assert [f.path for f in files] == ["p0a.txt", "p0b.txt", "p1a.txt", "p1b.txt", "p2a.txt"]


def test_listing_stops_on_an_empty_page():
    first = [_blob_change("add", "/only.txt", new="a0")]
    ado = _Ado()
    ado.on("GET", PR_API, _json(_load("pr-551.json")))
    ado.on("GET", "/diffs/commits", lambda params, _body: _json({"changes": first if params["$skip"] == 0 else []}))
    ado.on("GET", "/blobs/a0", _bytes(b"x\n"))

    files = parse_unified_diff(ForgeImpl(session=ado.session).get_diff(_ref()))

    assert [c.params["$skip"] for c in ado.of("GET", "/diffs/commits")] == [0, 1]
    assert [f.path for f in files] == ["only.txt"]


def test_listing_page_budget_exhaustion_raises(monkeypatch):
    monkeypatch.setattr(azure_devops, "_MAX_PAGES", 3)
    ado = _Ado()
    ado.on("GET", PR_API, _json(_load("pr-551.json")))
    ado.on(
        "GET",
        "/diffs/commits",
        lambda params, _body: _json({"changes": [_blob_change("add", f"/f{params['$skip']}.txt", new="a")]}),
    )

    with pytest.raises(ValueError, match="exceeded 3 pages"):
        ForgeImpl(session=ado.session).get_diff(_ref())

    assert len(ado.of("GET", "/diffs/commits")) == 3
    assert ado.blob_gets() == []


def test_tree_and_submodule_entries_are_skipped():
    changes = [
        _blob_change("edit", "/src", new="t1", old="t0", kind="tree"),
        _blob_change("edit", "/vendor/lib", new="c1", old="c0", kind="commit"),
        _blob_change("edit", "/src/a.txt", new="n1", old="o1"),
    ]

    _, files, ado = _diff_of(changes, {"o1": b"a\n", "n1": b"b\n"})

    assert [f.path for f in files] == ["src/a.txt"]
    assert sorted(ado.blob_gets()) == ["n1", "o1"]


def test_path_with_a_tab_is_skipped_with_a_warning(caplog):
    changes = [
        _blob_change("add", "/bad\tname.txt", new="n1"),
        _blob_change("add", "/good.txt", new="n2"),
    ]

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _, files, _ = _diff_of(changes, {"n1": b"x\n", "n2": b"y\n"})

    assert [f.path for f in files] == ["good.txt"]
    assert "cannot be expressed" in caplog.text


def test_empty_diff_raises_value_error():
    changes = [_blob_change("edit", "/src", new="t1", old="t0", kind="tree")]
    ado = _Ado()
    _serve_diff(ado, changes, {})

    with pytest.raises(ValueError, match="empty diff"):
        ForgeImpl(session=ado.session).get_diff(_ref())


def test_missing_source_commit_raises():
    pr = _load("pr-551.json")
    del pr["lastMergeSourceCommit"]
    ado = _Ado()
    ado.on("GET", PR_API, _json(pr))

    with pytest.raises(ValueError, match="no source commit"):
        ForgeImpl(session=ado.session).get_diff(_ref())


def test_missing_target_commit_falls_back_to_branch_base():
    pr = _load("pr-551.json")
    del pr["lastMergeTargetCommit"]
    ado = _Ado()
    _serve_diff(ado, [_blob_change("add", "/a.txt", new="n1")], {"n1": b"x\n"}, pr=pr)

    ForgeImpl(session=ado.session).get_diff(_ref())

    (listing,) = ado.of("GET", "/diffs/commits")
    assert (listing.params["baseVersion"], listing.params["baseVersionType"]) == ("master", "branch")
    assert listing.params["targetVersion"] == SOURCE_551


def test_uses_diff_common_commit_true():
    ado = _Ado()
    _serve_diff(ado, [_blob_change("add", "/a.txt", new="n1")], {"n1": b"x\n"})

    ForgeImpl(session=ado.session).get_diff(_ref())

    (listing,) = ado.of("GET", "/diffs/commits")
    assert listing.params == {
        "api-version": "7.1",
        "baseVersion": TARGET_551,
        "baseVersionType": "commit",
        "targetVersion": SOURCE_551,
        "targetVersionType": "commit",
        "diffCommonCommit": "true",
        "$top": 1000,
        "$skip": 0,
    }


# --- compare ------------------------------------------------------------------


def test_get_compare_diff_uses_given_shas_and_returns_empty_for_empty_range():
    base_sha, head_sha = "b" * 40, "h" * 40
    ado = _Ado()
    ado.on("GET", "/diffs/commits", _json({"allChangesIncluded": True, "changes": []}))

    assert ForgeImpl(session=ado.session).get_compare_diff(_ref(), base_sha=base_sha, head_sha=head_sha) == ""

    (listing,) = ado.calls
    assert (listing.params["baseVersion"], listing.params["baseVersionType"]) == (base_sha, "commit")
    assert (listing.params["targetVersion"], listing.params["targetVersionType"]) == (head_sha, "commit")
    assert listing.params["diffCommonCommit"] == "true"


def test_get_compare_diff_renders_the_range():
    ado = _Ado()
    ado.on("GET", "/diffs/commits", _json({"allChangesIncluded": True,
                                          "changes": [_blob_change("edit", "/a.txt", new="n1", old="o1")]}))
    ado.on("GET", "/blobs/o1", _bytes(b"one\n"))
    ado.on("GET", "/blobs/n1", _bytes(b"two\n"))

    text = ForgeImpl(session=ado.session).get_compare_diff(_ref(), base_sha="b" * 40, head_sha="h" * 40)

    assert text == "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n"
    assert not ado.of("GET", PR_API)


def test_get_compare_diff_raises_on_http_failure():
    ado = _Ado()
    ado.on("GET", "/diffs/commits", _status(404))

    with pytest.raises(requests.HTTPError):
        ForgeImpl(session=ado.session).get_compare_diff(_ref(), base_sha="b" * 40, head_sha="h" * 40)


# --- get_file_content ---------------------------------------------------------


def test_get_file_content_reads_items_at_sha_raw_bytes():
    ado = _Ado()
    ado.on("GET", "/items", _bytes(b"\xef\xbb\xbfprint('hi')\n"))

    text = ForgeImpl(session=ado.session).get_file_content(_ref(), "src/app.py", sha=SOURCE_551)

    assert text == "﻿print('hi')\n"
    (call,) = ado.calls
    assert call.params == {
        "api-version": "7.1",
        "path": "/src/app.py",
        "versionDescriptor.version": SOURCE_551,
        "versionDescriptor.versionType": "commit",
        "download": "true",
    }
    assert call.headers["Accept"] == "application/octet-stream"
    assert call.stream is True


@pytest.mark.parametrize("status", [404, 401, 500])
def test_get_file_content_404_is_none(status):
    ado = _Ado()
    ado.on("GET", "/items", _status(status, '{"message": "TF401174"}'))

    assert ForgeImpl(session=ado.session).get_file_content(_ref(), "missing.py", sha=SOURCE_551) is None


def test_get_file_content_binary_is_none():
    ado = _Ado()
    ado.on("GET", "/items", _bytes(b"PK\x03\x04\x00\x00"))

    assert ForgeImpl(session=ado.session).get_file_content(_ref(), "a.zipx", sha=SOURCE_551) is None


def test_get_file_content_over_cap_is_none(monkeypatch):
    monkeypatch.setattr(azure_devops, "_MAX_FILE_CONTENT_BYTES", 8)
    ado = _Ado()
    ado.on("GET", "/items", _bytes(b"0123456789\n"))

    assert ForgeImpl(session=ado.session).get_file_content(_ref(), "big.txt", sha=SOURCE_551) is None


def test_get_file_content_transport_error_is_none():
    def boom(_params, _body):
        raise requests.ConnectionError("reset")

    ado = _Ado()
    ado.on("GET", "/items", boom)

    assert ForgeImpl(session=ado.session).get_file_content(_ref(), "a.py", sha=SOURCE_551) is None


def test_get_file_content_empty_sha_is_none():
    ado = _Ado()

    assert ForgeImpl(session=ado.session).get_file_content(_ref(), "a.py", sha="") is None
    assert ado.calls == []


def test_get_file_content_foreign_ref_is_none_not_raised():
    ref = PRRef(forge="azure-devops", host="github.com", owner="o", repo="r", number=1,
                url="https://github.com/o/r/pull/1")

    assert ForgeImpl(session=MagicMock(spec=requests.Session)).get_file_content(ref, "a.py", sha="abc") is None


# --- list_threads -------------------------------------------------------------


def _threads_forge(payload: dict) -> tuple[ForgeImpl, _Ado]:
    ado = _Ado()
    ado.on("GET", f"{PR_API}/threads", _json(payload))
    return ForgeImpl(session=ado.session), ado


def test_list_threads_skips_system_and_deleted():
    payload = _load("threads-551.json")
    payload["value"].append({
        "id": 1, "isDeleted": True, "status": "active",
        "comments": [{"id": 1, "content": "gone", "commentType": "text"}],
    })
    payload["value"].append({"id": 2, "status": "active", "comments": []})
    forge, _ = _threads_forge(payload)

    assert forge.list_threads(_ref()) == []


def test_list_threads_maps_inline_from_recorded_468():
    forge, ado = _threads_forge(_load("threads-468.json"))

    threads = forge.list_threads(_ref())

    assert [(t.path, t.line, t.resolved, t.author) for t in threads] == [
        ("AcmeWeb/AcmeWeb.Environment.ARMTemplate/Templates/FrontDoor.json", 85, True, "Example User"),
        ("AcmeWeb/AcmeWeb.sln", 19, True, "Example User"),
    ]
    assert threads[0].body_snippet.startswith("This will vary depending on region")
    assert ado.calls[0].params == {"api-version": "7.1"}


def test_list_threads_file_level_has_no_line():
    forge, _ = _threads_forge(_load("threads-476.json"))

    (thread,) = forge.list_threads(_ref())

    assert (thread.path, thread.line, thread.resolved) == (
        "AcmeWeb/AcmeWeb.Environment.ARMTemplate/Templates/FrontDoor.json", None, True,
    )


def test_list_threads_pr_level_active_is_unresolved():
    forge, _ = _threads_forge(_load("threads-463.json"))

    (thread,) = forge.list_threads(_ref())

    assert (thread.path, thread.line, thread.resolved) == (None, None, False)
    assert thread.body_snippet == "NOTE: Timing doesn't work. Abandoning"


@pytest.mark.parametrize("status", ["fixed", "wontFix", "closed", "byDesign"])
def test_list_threads_resolved_statuses(status):
    forge, _ = _threads_forge({"value": [
        {"id": 1, "status": status, "comments": [{"id": 1, "content": "x", "commentType": "text"}]},
    ]})

    assert forge.list_threads(_ref())[0].resolved is True


def test_list_threads_root_skips_a_deleted_first_comment():
    forge, _ = _threads_forge({"value": [{"id": 1, "status": "active", "comments": [
        {"id": 1, "content": "", "commentType": "text", "isDeleted": True},
        {"id": 2, "content": "the reply", "commentType": "text", "author": {"displayName": "Replier"}},
    ]}]})

    (thread,) = forge.list_threads(_ref())

    assert (thread.body_snippet, thread.author) == ("the reply", "Replier")


def test_list_threads_feed_failure_returns_empty_and_logs(caplog):
    ado = _Ado()
    ado.on("GET", f"{PR_API}/threads", _status(500, "upstream exploded"))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        threads = ForgeImpl(session=ado.session).list_threads(_ref())

    assert threads == []
    assert "upstream exploded" in caplog.text


def test_list_threads_transport_failure_returns_empty():
    def boom(_params, _body):
        raise requests.ConnectionError("reset")

    ado = _Ado()
    ado.on("GET", f"{PR_API}/threads", boom)

    assert ForgeImpl(session=ado.session).list_threads(_ref()) == []


# --- post_summary -------------------------------------------------------------


def _summary_ado(threads: dict) -> _Ado:
    ado = _Ado()
    ado.on("GET", f"{PR_API}/threads", _json(threads))
    ado.on("POST", f"{PR_API}/threads", _json({"id": 900}))
    return ado


def test_post_summary_creates_closed_thread_with_marker():
    ado = _summary_ado(_load("threads-551.json"))

    ForgeImpl(session=ado.session).post_summary(_ref(), "## Review\nAll good.")

    (post,) = ado.of("POST")
    assert post.params == {"api-version": "7.1"}
    assert post.body == {
        "comments": [{"parentCommentId": 0, "content": f"{SUMMARY_MARKER}\n## Review\nAll good.", "commentType": 1}],
        "status": "closed",
    }
    assert ado.of("PATCH") == []


def test_post_summary_patches_existing_marker_comment():
    threads = _load("threads-463.json")
    threads["value"].append({
        "id": 77, "status": "closed",
        "comments": [{"id": 3, "content": f"{SUMMARY_MARKER}\nold", "commentType": "text"}],
    })
    ado = _summary_ado(threads)
    ado.on("PATCH", f"{PR_API}/threads/77/comments/3", _json({"id": 3}))

    ForgeImpl(session=ado.session).post_summary(_ref(), "new body")

    (patch,) = ado.of("PATCH")
    assert patch.body == {"content": f"{SUMMARY_MARKER}\nnew body"}
    assert patch.params == {"api-version": "7.1"}
    assert ado.of("POST") == []


def test_post_summary_ignores_marker_in_inline_thread():
    ado = _summary_ado({"value": [{
        "id": 5, "status": "active",
        "threadContext": {"filePath": "/a.py", "rightFileStart": {"line": 1, "offset": 1}},
        "comments": [{"id": 1, "content": f"quoting {SUMMARY_MARKER}", "commentType": "text"}],
    }]})

    ForgeImpl(session=ado.session).post_summary(_ref(), "body")

    assert len(ado.of("POST")) == 1
    assert ado.of("PATCH") == []


@pytest.mark.parametrize("status", [401, 500])
def test_post_summary_feed_failure_propagates_and_posts_nothing(status):
    ado = _Ado()
    ado.on("GET", f"{PR_API}/threads", _status(status))

    with pytest.raises(FeedReadError):
        ForgeImpl(session=ado.session).post_summary(_ref(), "body")

    assert ado.of("POST") == [] and ado.of("PATCH") == []


def test_post_summary_non_json_feed_is_a_feed_read_error():
    ado = _Ado()
    ado.on("GET", f"{PR_API}/threads", _mock_response(203, text="<html/>", headers={"Content-Type": "text/html"}))

    with pytest.raises(FeedReadError, match="PRXREF_AZURE_DEVOPS_TOKEN"):
        ForgeImpl(session=ado.session).post_summary(_ref(), "body")

    assert ado.of("POST") == []


def test_post_summary_feed_without_value_array_is_a_feed_read_error():
    ado = _Ado()
    ado.on("GET", f"{PR_API}/threads", _json({"count": 0}))

    with pytest.raises(FeedReadError):
        ForgeImpl(session=ado.session).post_summary(_ref(), "body")


def test_post_summary_rejected_post_raises():
    ado = _Ado()
    ado.on("GET", f"{PR_API}/threads", _json({"value": []}))
    ado.on("POST", f"{PR_API}/threads", _status(403))

    with pytest.raises(requests.HTTPError):
        ForgeImpl(session=ado.session).post_summary(_ref(), "body")


# --- post_inline_comments -----------------------------------------------------


def _inline_ado(*, iterations: Any = None) -> _Ado:
    ado = _Ado()
    ado.on("GET", f"{PR_API}/iterations", iterations if iterations is not None else _status(401))
    ado.on("POST", f"{PR_API}/threads", _json({"id": 1}))
    return ado


def test_post_inline_builds_thread_context_with_leading_slash():
    ado = _inline_ado()

    posted = ForgeImpl(session=ado.session).post_inline_comments(
        _ref(), [InlineComment(path="src/app.py", line=12, body="finding")]
    )

    assert posted == 1
    (post,) = ado.of("POST")
    assert post.body == {
        "comments": [{"parentCommentId": 0, "content": "finding", "commentType": 1}],
        "status": "active",
        "threadContext": {
            "filePath": "/src/app.py",
            "rightFileStart": {"line": 12, "offset": 1},
            "rightFileEnd": {"line": 12, "offset": 1},
        },
    }


def test_post_inline_includes_change_tracking_when_iterations_readable():
    ado = _inline_ado(iterations=_json({"value": [{"id": 1}, {"id": 7}, {"id": 3}]}))
    ado.on("GET", f"{PR_API}/iterations/7/changes", _json(_load("iter-changes-551.json")))
    comments = [
        InlineComment(path="AcmeWeb/AcmeWeb.Environment.ARMTemplate/PowerShell/KeyVault.ps1", line=3, body="a"),
        InlineComment(path="AcmeWeb/AcmeWeb.Environment.ARMTemplate/PowerShell/KeyVault - Copy.ps1", line=1, body="b"),
        InlineComment(path="not/in/the/pr.py", line=1, body="c"),
    ]

    assert ForgeImpl(session=ado.session).post_inline_comments(_ref(), comments) == 3

    bodies = [p.body for p in ado.of("POST")]
    assert bodies[0]["pullRequestThreadContext"] == {
        "changeTrackingId": 6,
        "iterationContext": {"firstComparingIteration": 7, "secondComparingIteration": 7},
    }
    assert bodies[1]["pullRequestThreadContext"]["changeTrackingId"] == 1
    assert "pullRequestThreadContext" not in bodies[2]
    (changes_call,) = ado.of("GET", f"{PR_API}/iterations/7/changes")
    assert (changes_call.params["$top"], changes_call.params["$skip"]) == (2000, 0)


def test_post_inline_follows_iteration_change_paging():
    ado = _inline_ado(iterations=_json({"value": [{"id": 2}]}))
    pages = {
        0: {"changeEntries": [{"changeTrackingId": 1, "item": {"path": "/a.py"}}], "nextSkip": 1, "nextTop": 2000},
        1: {"changeEntries": [{"changeTrackingId": 9, "item": {"path": "/b.py"}}]},
    }
    ado.on("GET", f"{PR_API}/iterations/2/changes", lambda params, _body: _json(pages[params["$skip"]]))

    ForgeImpl(session=ado.session).post_inline_comments(_ref(), [InlineComment(path="b.py", line=1, body="x")])

    assert ado.of("POST")[0].body["pullRequestThreadContext"]["changeTrackingId"] == 9


def test_post_inline_omits_pr_thread_context_when_iterations_401():
    ado = _inline_ado(iterations=_status(401))

    ForgeImpl(session=ado.session).post_inline_comments(_ref(), [InlineComment(path="a.py", line=1, body="x")])

    assert "pullRequestThreadContext" not in ado.of("POST")[0].body


@pytest.mark.parametrize("iterations", [{"value": []}, {"value": [{"no_id": 1}]}, {"value": "junk"}])
def test_post_inline_survives_malformed_iterations(iterations):
    ado = _inline_ado(iterations=_json(iterations))

    posted = ForgeImpl(session=ado.session).post_inline_comments(
        _ref(), [InlineComment(path="a.py", line=1, body="x")]
    )

    assert posted == 1
    assert "pullRequestThreadContext" not in ado.of("POST")[0].body


def test_post_inline_counts_only_2xx_and_skips_4xx_5xx_and_transport(caplog):
    def by_line(_params, body):
        line = body["threadContext"]["rightFileStart"]["line"]
        if line == 4:
            raise requests.ConnectionError("reset")
        return {1: _json({"id": 1}), 2: _status(400, "line outside the diff"), 3: _status(503)}[line]

    ado = _inline_ado()
    ado.on("POST", f"{PR_API}/threads", by_line)
    comments = [InlineComment(path="a.py", line=n, body="x") for n in (1, 2, 3, 4)]

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        posted = ForgeImpl(session=ado.session).post_inline_comments(_ref(), comments)

    assert posted == 1
    assert len(ado.of("POST")) == 4
    assert "line outside the diff" in caplog.text


def test_post_inline_empty_is_zero_and_no_requests():
    ado = _Ado()

    assert ForgeImpl(session=ado.session).post_inline_comments(_ref(), []) == 0
    assert ado.calls == []


# --- prune ----------------------------------------------------------------------


_INLINE_CONTEXT = {"filePath": "/a.py", "rightFileStart": {"line": 1, "offset": 1}}


def _prune_threads() -> dict:
    attributed = f"finding\n\n{ATTRIBUTION_MARKER} · model=m · 1 tok · 1s"
    return {"value": [
        {"id": 10, "status": "active", "threadContext": _INLINE_CONTEXT,
         "comments": [{"id": 1, "content": attributed, "commentType": "text"}]},
        {"id": 11, "status": "active", "threadContext": _INLINE_CONTEXT,
         "comments": [{"id": 1, "content": "a human note", "commentType": "text"},
                      {"id": 2, "content": attributed, "commentType": "text"}]},
        {"id": 12, "status": "closed",
         "comments": [{"id": 1, "content": f"{SUMMARY_MARKER}\nsummary\n{attributed}", "commentType": "text"}]},
        {"id": 13, "isDeleted": True, "threadContext": _INLINE_CONTEXT,
         "comments": [{"id": 1, "content": attributed, "commentType": "text"}]},
        {"id": 14, "comments": [{"id": 1, "content": attributed, "commentType": "system"}]},
    ]}


def test_prune_deletes_only_attributed_inline_roots():
    ado = _Ado()
    ado.on("GET", f"{PR_API}/threads", _json(_prune_threads()))
    ado.on("DELETE", f"{PR_API}/threads/10/comments/1", _status(200))

    removed = ForgeImpl(session=ado.session).prune_inline_comments(_ref())

    assert removed == 1
    assert [c.path for c in ado.of("DELETE")] == [f"{PR_API}/threads/10/comments/1"]
    assert ado.of("DELETE")[0].params == {"api-version": "7.1"}


def test_prune_never_touches_summary():
    threads = {"value": [_prune_threads()["value"][2]]}
    ado = _Ado()
    ado.on("GET", f"{PR_API}/threads", _json(threads))

    assert ForgeImpl(session=ado.session).prune_inline_comments(_ref()) == 0
    assert ado.of("DELETE") == []


def test_prune_logs_403_and_continues(caplog):
    first = _prune_threads()["value"][0]
    second = {**first, "id": 20}
    ado = _Ado()
    ado.on("GET", f"{PR_API}/threads", _json({"value": [first, second]}))
    ado.on("DELETE", f"{PR_API}/threads/10/comments/1", _status(403, "not your comment"))
    ado.on("DELETE", f"{PR_API}/threads/20/comments/1", _status(204))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        removed = ForgeImpl(session=ado.session).prune_inline_comments(_ref())

    assert removed == 1
    assert len(ado.of("DELETE")) == 2
    assert "403" in caplog.text and "not your comment" in caplog.text


def test_prune_transport_error_is_skipped():
    def boom(_params, _body):
        raise requests.ConnectionError("reset")

    ado = _Ado()
    ado.on("GET", f"{PR_API}/threads", _json({"value": [_prune_threads()["value"][0]]}))
    ado.on("DELETE", f"{PR_API}/threads/10/comments/1", boom)

    assert ForgeImpl(session=ado.session).prune_inline_comments(_ref()) == 0


def test_prune_feed_failure_returns_zero(caplog):
    ado = _Ado()
    ado.on("GET", f"{PR_API}/threads", _status(500))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert ForgeImpl(session=ado.session).prune_inline_comments(_ref()) == 0

    assert ado.of("DELETE") == []
    assert "skipped" in caplog.text


# --- retry policy ---------------------------------------------------------------


class _RetryProbe:
    """A localhost server that counts arriving requests and replays statuses.

    The retry policy lives in urllib3, underneath the ``requests`` adapter, so
    a ``MagicMock`` session cannot exercise it. This runs the real session
    against a real socket and counts what actually arrives. ``statuses`` is
    replayed one per request and its last entry repeats.
    """

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.received: list[str] = []
        probe = self

        class _Handler(BaseHTTPRequestHandler):
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
            do_PATCH = _reply
            do_DELETE = _reply

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


@pytest.mark.parametrize("verb", ["post", "patch", "delete"])
def test_a_lost_write_is_not_re_sent(verb):
    """A thread POST, a summary PATCH and a prune DELETE are never replayed."""
    session = _make_retry_session()
    with _RetryProbe([502]) as probe:
        resp = getattr(session, verb)(probe.url, timeout=(5.0, 5.0))

    assert probe.received == [verb.upper()]
    assert resp.status_code == 502


def test_a_lost_read_is_still_re_sent():
    session = _make_retry_session()
    with _RetryProbe([502, 200]) as probe:
        resp = session.get(probe.url, timeout=(5.0, 5.0))

    assert probe.received == ["GET", "GET"]
    assert resp.status_code == 200


def test_only_read_verbs_are_retryable():
    retry = _make_retry_session().get_adapter("https://dev.azure.com").max_retries

    assert isinstance(retry, LoggingRetry)
    assert retry.allowed_methods == frozenset(["GET", "HEAD", "OPTIONS"])
    assert retry.is_retry("GET", 502) is True
    assert retry.is_retry("POST", 502) is False
    assert retry.is_retry("PATCH", 429) is False
    assert retry.is_retry("DELETE", 503) is False
    assert retry.respect_retry_after_header is True
