"""Issue #146 groundwork: ``get_default_branch`` and ``get_file_at_branch`` on every forge.

Both are optional capabilities nothing calls yet. They run through each real
adapter over a real ``requests.Session`` whose transport adapter is a
recorder, so URL quoting is what ``requests`` actually produces, and the
request counts prove that construction and ``get_pr`` never reach for them.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import HTTPAdapter

from prxref.forges import azure_devops, bitbucket, bitbucket_server, gitea, github, gitlab
from prxref.forges.base import PRRef, branch_file_result, detect_forge
from prxref.forges.replay import LocalDiffForge, ReplayForge

JSON = "application/json"
TEXT = "text/plain; charset=utf-8"
BRANCH = "feat/x y"
PATH = "docs/a b#c.md"
FILE_TEXT = "# hello\nbody\n"


class Recorder(HTTPAdapter):
    """A transport that records each prepared request and answers from a script."""

    def __init__(self, script):
        super().__init__()
        self.script = script
        self.requests: list[requests.PreparedRequest] = []

    def send(self, request, **kwargs):
        self.requests.append(request)
        answer = self.script(request)
        if isinstance(answer, Exception):
            raise answer
        status, body, ctype = answer
        resp = requests.Response()
        resp.status_code = status
        resp._content = body if isinstance(body, bytes) else body.encode()
        resp._content_consumed = True
        resp.headers["Content-Type"] = ctype
        resp.url = request.url
        resp.request = request
        return resp


def make_session(script):
    session = requests.Session()
    recorder = Recorder(script)
    session.mount("https://", recorder)
    session.mount("http://", recorder)
    return session, recorder


@dataclass(frozen=True)
class Case:
    name: str
    module: object
    pr_url: str
    default_ok: dict
    file_ctype: str
    default_ctype: str = JSON


CASES = [
    Case("github", github, "https://github.com/owner/repo/pull/1",
         {"default_branch": "main"}, "application/vnd.github.raw+json; charset=utf-8"),
    Case("gitlab", gitlab, "https://gitlab.example.com/owner/repo/-/merge_requests/1",
         {"default_branch": "main"}, TEXT),
    Case("gitea", gitea, "https://gitea.example.com/owner/repo/pulls/1",
         {"default_branch": "main"}, TEXT),
    Case("bitbucket", bitbucket, "https://bitbucket.org/owner/repo/pull-requests/1",
         {"mainbranch": {"name": "main", "type": "branch"}}, TEXT),
    Case("bitbucket-server", bitbucket_server,
         "https://bitbucket.example.com/projects/PROJ/repos/repo/pull-requests/1",
         {"id": "refs/heads/main", "displayId": "main"}, TEXT),
    Case("azure-devops", azure_devops, "https://dev.azure.com/org/proj/_git/repo/pullrequest/1",
         {"defaultBranch": "refs/heads/main"}, TEXT),
]
IDS = [c.name for c in CASES]


@pytest.fixture(params=CASES, ids=IDS)
def case(request):
    return request.param


def build(case: Case, script):
    session, recorder = make_session(script)
    forge = case.module.ForgeImpl(session=session)
    ref = forge.parse_pr_url(case.pr_url)
    assert ref is not None
    return forge, ref, recorder


def is_file_request(case: Case, request) -> bool:
    url = request.url
    return any(part in url for part in ("/contents/", "/files/", "/raw/", "/src/", "/items"))


def answer_default(case: Case, status: int, body, ctype: str | None = None):
    payload = body if isinstance(body, (str, bytes)) else json.dumps(body)
    return lambda request: (status, payload, ctype or case.default_ctype)


# --- get_default_branch ---------------------------------------------------------


def test_default_branch_success_is_the_bare_name(case):
    forge, ref, rec = build(case, answer_default(case, 200, case.default_ok))
    assert forge.get_default_branch(ref) == "main"
    assert len(rec.requests) == 1


@pytest.mark.parametrize("status", [404, 500])
def test_default_branch_http_failure_is_none_with_a_warning(case, status, caplog):
    forge, ref, rec = build(case, answer_default(case, status, {"message": "nope"}))
    before = dict(vars(forge))
    with caplog.at_level(logging.WARNING):
        assert forge.get_default_branch(ref) is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert vars(forge) == before


@pytest.mark.parametrize("body", ["not json {", "[]", "{}", '"main"'])
def test_default_branch_malformed_body_is_none_with_a_warning(case, body, caplog):
    forge, ref, _ = build(case, answer_default(case, 200, body))
    with caplog.at_level(logging.WARNING):
        assert forge.get_default_branch(ref) is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_default_branch_connection_error_is_none_with_a_warning(case, caplog):
    forge, ref, _ = build(case, lambda request: requests.ConnectionError("boom"))
    with caplog.at_level(logging.WARNING):
        assert forge.get_default_branch(ref) is None
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_default_branch_null_value_is_none(case):
    nulled = {k: None for k in case.default_ok}
    forge, ref, _ = build(case, answer_default(case, 200, nulled))
    assert forge.get_default_branch(ref) is None


def test_default_branch_keeps_slashes_in_the_name(case):
    body = json.loads(json.dumps(case.default_ok).replace('main"', 'release/1.x"'))
    forge, ref, _ = build(case, answer_default(case, 200, body))
    assert forge.get_default_branch(ref) == "release/1.x"


# --- get_file_at_branch ----------------------------------------------------------


def file_script(case: Case, status: int, body=FILE_TEXT):
    return lambda request: (status, body, case.file_ctype)


def test_file_200_is_found_with_text(case):
    forge, ref, rec = build(case, file_script(case, 200))
    assert forge.get_file_at_branch(ref, PATH, BRANCH) == (True, FILE_TEXT)
    assert len(rec.requests) == 1


def test_file_404_is_absent(case):
    forge, ref, _ = build(case, file_script(case, 404, "not found"))
    assert forge.get_file_at_branch(ref, PATH, BRANCH) == (False, None)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_file_other_statuses_are_unknown(case, status):
    forge, ref, _ = build(case, file_script(case, status, "bad"))
    assert forge.get_file_at_branch(ref, PATH, BRANCH) == (True, None)


def test_file_connection_error_is_unknown(case):
    forge, ref, _ = build(case, lambda request: requests.ConnectionError("boom"))
    assert forge.get_file_at_branch(ref, PATH, BRANCH) == (True, None)


def test_file_timeout_is_unknown(case):
    forge, ref, _ = build(case, lambda request: requests.Timeout("slow"))
    assert forge.get_file_at_branch(ref, PATH, BRANCH) == (True, None)


def test_file_binary_and_oversized_are_unknown(case):
    forge, ref, _ = build(case, file_script(case, 200, b"ab\x00cd"))
    assert forge.get_file_at_branch(ref, PATH, BRANCH) == (True, None)
    forge, ref, _ = build(case, file_script(case, 200, b"x" * (512 * 1024 + 1)))
    assert forge.get_file_at_branch(ref, PATH, BRANCH) == (True, None)


def test_file_empty_branch_or_path_is_unknown_with_no_request(case):
    forge, ref, rec = build(case, file_script(case, 200))
    assert forge.get_file_at_branch(ref, PATH, "") == (True, None)
    assert forge.get_file_at_branch(ref, "", BRANCH) == (True, None)
    assert rec.requests == []


def test_file_branch_with_slash_and_space_is_quoted(case):
    forge, ref, rec = build(case, file_script(case, 200))
    forge.get_file_at_branch(ref, PATH, BRANCH)
    (request,) = rec.requests
    parts = urlsplit(request.url)
    assert " " not in request.url and "#" not in request.url
    if case.name == "bitbucket":
        assert "/src/feat%2Fx%20y/docs/a%20b%23c.md" in parts.path
        return
    values = [v for vs in parse_qs(parts.query).values() for v in vs]
    assert BRANCH in values
    assert "feat/x" not in parts.query
    assert "%23" in request.url


# --- no request unless asked ------------------------------------------------------


def test_construction_makes_no_request(case):
    forge, ref, rec = build(case, answer_default(case, 200, {}))
    assert rec.requests == []


def test_get_pr_does_not_look_up_the_default_branch_or_a_file(case):
    forge, ref, rec = build(case, answer_default(case, 200, {}))
    try:
        forge.get_pr(ref)
    except Exception:  # noqa: BLE001 - an empty PR body may be refused; the requests still count
        pass
    assert not [r for r in rec.requests if is_file_request(case, r)]
    default_urls = [r.url for r in rec.requests if urlsplit(r.url).path.rstrip("/").endswith(
        ("/default-branch", "/branches/default")
    )]
    assert default_urls == []
    assert all("pull" in r.url.lower() or "merge_requests" in r.url for r in rec.requests)


def test_the_two_calls_are_the_only_new_requests(case):
    forge, ref, rec = build(case, answer_default(case, 200, case.default_ok))
    forge.get_default_branch(ref)
    assert len(rec.requests) == 1
    forge.get_file_at_branch(ref, PATH, BRANCH)
    assert len(rec.requests) == 2


def test_the_capabilities_are_resolved_through_getattr(case):
    forge, _, _ = build(case, answer_default(case, 200, {}))
    assert callable(getattr(forge, "get_default_branch", None))
    assert callable(getattr(forge, "get_file_at_branch", None))
    assert getattr(object(), "get_default_branch", None) is None


# --- adapter specifics -------------------------------------------------------------


def test_azure_strips_refs_heads_from_the_default_branch():
    case = CASES[-1]
    for raw, bare in (("refs/heads/main", "main"), ("refs/heads/feat/x", "feat/x"), ("trunk", "trunk")):
        forge, ref, _ = build(case, answer_default(case, 200, {"defaultBranch": raw}))
        assert forge.get_default_branch(ref) == bare


def test_azure_203_sign_in_page_is_none():
    case = CASES[-1]
    forge, ref, _ = build(case, answer_default(case, 203, "<html>sign in</html>", "text/html"))
    assert forge.get_default_branch(ref) is None


def test_azure_file_read_names_the_branch_version_type_and_strips_the_prefix():
    case = CASES[-1]
    forge, ref, rec = build(case, file_script(case, 200))
    forge.get_file_at_branch(ref, PATH, "refs/heads/feat/x y")
    query = parse_qs(urlsplit(rec.requests[0].url).query)
    assert query["versionDescriptor.versionType"] == ["branch"]
    assert query["versionDescriptor.version"] == ["feat/x y"]
    assert query["path"] == ["/docs/a b#c.md"]


def test_default_branch_urls_are_the_repository_resources():
    expected = {
        "github": "https://api.github.com/repos/owner/repo",
        "gitlab": "https://gitlab.example.com/api/v4/projects/owner%2Frepo",
        "gitea": "https://gitea.example.com/api/v1/repos/owner/repo",
        "bitbucket": "https://api.bitbucket.org/2.0/repositories/owner/repo",
        "bitbucket-server": "https://bitbucket.example.com/rest/api/1.0/projects/PROJ/repos/repo/default-branch",
        "azure-devops": "https://dev.azure.com/org/proj/_apis/git/repositories/repo",
    }
    for case in CASES:
        forge, ref, rec = build(case, answer_default(case, 200, case.default_ok))
        forge.get_default_branch(ref)
        assert rec.requests[0].url.split("?")[0] == expected[case.name], case.name


def test_bitbucket_server_falls_back_once_to_branches_default_on_404():
    case = CASES[4]
    seen = []

    def script(request):
        seen.append(urlsplit(request.url).path)
        if request.url.split("?")[0].endswith("/default-branch"):
            return 404, "{}", JSON
        return 200, json.dumps({"id": "refs/heads/dev", "displayId": "dev"}), JSON

    forge, ref, rec = build(case, script)
    assert forge.get_default_branch(ref) == "dev"
    assert [p.rsplit("/repos/repo", 1)[1] for p in seen] == ["/default-branch", "/branches/default"]


def test_bitbucket_server_404_on_both_endpoints_is_none():
    case = CASES[4]
    forge, ref, rec = build(case, lambda request: (404, "{}", JSON))
    assert forge.get_default_branch(ref) is None
    assert len(rec.requests) == 2


def test_bitbucket_server_500_does_not_fall_back():
    case = CASES[4]
    forge, ref, rec = build(case, lambda request: (500, "{}", JSON))
    assert forge.get_default_branch(ref) is None
    assert len(rec.requests) == 1


def test_github_directory_envelope_is_unknown_not_absent():
    case = CASES[0]
    forge, ref, _ = build(case, lambda request: (200, json.dumps([{"name": "a"}]), JSON))
    assert forge.get_file_at_branch(ref, "docs", "main") == (True, None)


def test_github_file_read_sends_the_branch_as_ref():
    case = CASES[0]
    forge, ref, rec = build(case, file_script(case, 200))
    forge.get_file_at_branch(ref, "a/b.py", "main")
    assert parse_qs(urlsplit(rec.requests[0].url).query) == {"ref": ["main"]}


# --- shared helper, replay, detection ----------------------------------------------


def test_branch_file_result_table(case):
    assert branch_file_result(200, b"hi", max_bytes=10) == (True, "hi")
    assert branch_file_result(404, b"hi", max_bytes=10) == (False, None)
    assert branch_file_result(500, b"hi", max_bytes=10) == (True, None)
    assert branch_file_result(302, b"hi", max_bytes=10) == (True, None)
    assert branch_file_result(200, b"h" * 11, max_bytes=10) == (True, None)
    assert branch_file_result(200, b"a\x00", max_bytes=10) == (True, None)


def test_replay_forges_answer_unknown(case):
    local = LocalDiffForge("diff", path="x.diff")
    ref = PRRef("local", "local", "o", "r", 1, "x.diff")
    assert local.get_default_branch(ref) is None
    assert local.get_file_at_branch(ref, "a", "main") == (True, None)
    inner, _, rec = build(case, answer_default(case, 200, case.default_ok))
    wrapped = ReplayForge(inner)
    real_ref = inner.parse_pr_url(case.pr_url)
    assert wrapped.get_default_branch(real_ref) is None
    assert wrapped.get_file_at_branch(real_ref, "a", "main") == (True, None)
    assert rec.requests == []


def test_detect_forge_still_routes_each_url(case):
    ref = detect_forge(case.pr_url)
    assert ref is not None and ref.forge == case.module.ForgeImpl.name
    assert detect_forge("https://example.com/owner/repo/default-branch") is None
