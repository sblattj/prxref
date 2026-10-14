"""Forge write methods for PR auto-description and auto-labels (issue #37).

``update_description`` on every forge; ``list_labels`` / ``add_labels`` on
GitHub, GitLab, Gitea and Azure DevOps only. The session double answers by
method and URL, so a stray create call shows up in ``calls``.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests

from prxref.forges import (
    azure_devops,
    bitbucket,
    bitbucket_server,
    gitea,
    github,
    gitlab,
    replay,
)
from prxref.forges.base import FeedReadError


def _resp(status=200, data=None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.headers = {"Content-Type": "application/json"}
    resp.json.return_value = data if data is not None else {}
    resp.text = json.dumps(data if data is not None else {})
    resp.raise_for_status.side_effect = None if resp.ok else requests.HTTPError(response=resp)
    return resp


class Fake:
    """Routes ``(method, url-substring)`` to responses; records every call."""

    def __init__(self, routes):
        self.routes = routes  # {(method, substring): response | list[response] | callable}
        self.calls: list[tuple[str, str, dict]] = []

    def _do(self, method, url, **kw):
        self.calls.append((method, url, kw))
        best = None
        for (m, sub), answer in self.routes.items():
            if m == method and sub in url and (best is None or len(sub) > len(best[0])):
                best = (sub, answer)
        if best is None:
            raise AssertionError(f"unrouted {method} {url}")
        answer = best[1]
        if isinstance(answer, list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer

    def get(self, url, **kw):
        return self._do("GET", url, **kw)

    def post(self, url, **kw):
        return self._do("POST", url, **kw)

    def put(self, url, **kw):
        return self._do("PUT", url, **kw)

    def patch(self, url, **kw):
        return self._do("PATCH", url, **kw)

    def of(self, method):
        return [c for c in self.calls if c[0] == method]


@pytest.fixture(autouse=True)
def _no_creds(monkeypatch):
    for var in ("SYSTEM_ACCESSTOKEN", "PRXREF_AZURE_DEVOPS_TOKEN"):
        monkeypatch.delenv(var, raising=False)


GH_URL = "https://github.com/acme/api/pull/42"
GL_URL = "https://gitlab.com/acme/api/-/merge_requests/42"
GT_URL = "https://codeberg.example.com/acme/api/pulls/42"
AZ_URL = "https://dev.azure.com/acme/AcmeWeb/_git/AcmeWeb/pullrequest/551"
BB_URL = "https://bitbucket.org/acme/api/pull-requests/42"
BBS_URL = "https://bitbucket.corp.example/projects/PLAT/repos/api/pull-requests/42"


def _gh(fake):
    return github.ForgeImpl(session=fake), github.ForgeImpl.parse_pr_url(GH_URL)


def _gl(fake):
    return gitlab.ForgeImpl(session=fake), gitlab.ForgeImpl.parse_pr_url(GL_URL)


def _gt(fake):
    return gitea.ForgeImpl(session=fake), gitea.ForgeImpl.parse_pr_url(GT_URL)


def _az(fake):
    return azure_devops.ForgeImpl(session=fake), azure_devops.ForgeImpl.parse_pr_url(AZ_URL)


# --- update_description ------------------------------------------------------


def test_github_update_description_patches_the_pull_body():
    fake = Fake({("PATCH", "/repos/acme/api/pulls/42"): _resp(200, {})})
    forge, ref = _gh(fake)
    forge.update_description(ref, "new body")
    (_, url, kw), = fake.calls
    assert url == "https://api.github.com/repos/acme/api/pulls/42"
    assert kw["json"] == {"body": "new body"}


def test_gitlab_update_description_puts_the_mr_description():
    fake = Fake({("PUT", "/merge_requests/42"): _resp(200, {})})
    forge, ref = _gl(fake)
    forge.update_description(ref, "new body")
    (_, url, kw), = fake.calls
    assert url.endswith("/projects/acme%2Fapi/merge_requests/42")
    assert kw["json"] == {"description": "new body"}


def test_gitea_update_description_patches_the_pull_body():
    fake = Fake({("PATCH", "/repos/acme/api/pulls/42"): _resp(200, {})})
    forge, ref = _gt(fake)
    forge.update_description(ref, "new body")
    (_, url, kw), = fake.calls
    assert url == "https://codeberg.example.com/api/v1/repos/acme/api/pulls/42"
    assert kw["json"] == {"body": "new body"}


def test_azure_update_description_patches_the_pr_description():
    fake = Fake({("PATCH", "/pullrequests/551"): _resp(200, {})})
    forge, ref = _az(fake)
    forge.update_description(ref, "new body")
    (_, url, kw), = fake.calls
    assert url.endswith("/_apis/git/repositories/AcmeWeb/pullrequests/551")
    assert kw["json"] == {"description": "new body"}
    assert kw["params"] == {"api-version": "7.1"}


def test_bitbucket_cloud_update_description_keeps_the_title():
    fake = Fake({
        ("GET", "/pullrequests/42"): _resp(200, {"title": "Fix the thing"}),
        ("PUT", "/pullrequests/42"): _resp(200, {}),
    })
    ref = bitbucket.ForgeImpl.parse_pr_url(BB_URL)
    bitbucket.ForgeImpl(session=fake).update_description(ref, "new body")
    (_, url, kw), = fake.of("PUT")
    assert url == "https://api.bitbucket.org/2.0/repositories/acme/api/pullrequests/42"
    assert kw["json"] == {"title": "Fix the thing", "description": "new body"}


def test_bitbucket_server_update_description_sends_version_and_keeps_reviewers():
    fake = Fake({
        ("GET", "/pull-requests/42"): _resp(200, {
            "version": 7,
            "title": "Fix the thing",
            "reviewers": [{"user": {"name": "alice"}, "approved": True}, {"user": {}}],
        }),
        ("PUT", "/pull-requests/42"): _resp(200, {}),
    })
    ref = bitbucket_server.ForgeImpl.parse_pr_url(BBS_URL)
    bitbucket_server.ForgeImpl(session=fake).update_description(ref, "new body")
    (_, _, kw), = fake.of("PUT")
    assert kw["json"] == {
        "version": 7,
        "title": "Fix the thing",
        "description": "new body",
        "reviewers": [{"user": {"name": "alice"}}],
    }


@pytest.mark.parametrize("make,method", [
    (_gh, "PATCH"), (_gl, "PUT"), (_gt, "PATCH"), (_az, "PATCH"),
])
def test_update_description_raises_on_auth_failure(make, method):
    fake = Fake({(method, "/"): _resp(401, {})})
    forge, ref = make(fake)
    with pytest.raises(requests.HTTPError):
        forge.update_description(ref, "x")


def test_bitbucket_update_description_raises_on_auth_failure():
    fake = Fake({("GET", "/"): _resp(401, {})})
    ref = bitbucket.ForgeImpl.parse_pr_url(BB_URL)
    with pytest.raises(requests.HTTPError):
        bitbucket.ForgeImpl(session=fake).update_description(ref, "x")
    fake = Fake({("GET", "/"): _resp(401, {})})
    ref = bitbucket_server.ForgeImpl.parse_pr_url(BBS_URL)
    with pytest.raises(requests.HTTPError):
        bitbucket_server.ForgeImpl(session=fake).update_description(ref, "x")


# --- label scope: only four forges ------------------------------------------


@pytest.mark.parametrize("mod", [bitbucket, bitbucket_server, replay])
def test_forges_without_pr_labels_define_no_label_methods(mod):
    for cls in (getattr(mod, "ForgeImpl", None), getattr(mod, "LocalDiffForge", None),
                getattr(mod, "ReplayForge", None)):
        if cls is not None:
            assert not hasattr(cls, "list_labels")
            assert not hasattr(cls, "add_labels")
            # Bitbucket writes descriptions; a replay writes nothing.
            assert hasattr(cls, "update_description") == (mod is not replay)


# --- GitHub labels -----------------------------------------------------------


def _gh_routes(existing, extra=None):
    routes = {
        ("GET", "/repos/acme/api/labels"): _resp(200, [{"name": n} for n in existing]),
        ("POST", "/repos/acme/api/labels"): _resp(201, {}),
        ("POST", "/issues/42/labels"): _resp(200, []),
    }
    routes.update(extra or {})
    return routes


def test_github_list_labels_pages_to_the_end():
    full = _resp(200, [{"name": f"l{i}"} for i in range(github._PAGE_SIZE)])
    fake = Fake({("GET", "/labels"): [full, _resp(200, [{"name": "last"}])]})
    forge, ref = _gh(fake)
    names = forge.list_labels(ref)
    assert len(names) == github._PAGE_SIZE + 1 and names[-1] == "last"
    assert [c[2]["params"]["page"] for c in fake.of("GET")] == [1, 2]


def test_github_list_labels_raises_feed_read_error_on_failure():
    fake = Fake({("GET", "/labels"): _resp(500, {})})
    forge, ref = _gh(fake)
    with pytest.raises(FeedReadError):
        forge.list_labels(ref)


def test_github_add_labels_without_create_missing_never_creates():
    fake = Fake(_gh_routes(["Fix", "docs"]))
    forge, ref = _gh(fake)
    applied = forge.add_labels(ref, ["fix", "feat", "docs"], create_missing=False)
    assert applied == ["Fix", "docs"]
    assert fake.calls and all(
        not (m == "POST" and u.endswith("/repos/acme/api/labels")) for m, u, _ in fake.calls
    )
    (_, url, kw), = fake.of("POST")
    assert url.endswith("/issues/42/labels") and kw["json"] == {"labels": ["Fix", "docs"]}


def test_github_add_labels_with_create_missing_creates_then_applies():
    fake = Fake(_gh_routes(["docs"]))
    forge, ref = _gh(fake)
    applied = forge.add_labels(ref, ["feat", "docs"], create_missing=True)
    assert applied == ["feat", "docs"]
    posts = fake.of("POST")
    assert posts[0][1].endswith("/repos/acme/api/labels")
    assert posts[0][2]["json"] == {"name": "feat", "color": "ededed"}
    assert posts[1][1].endswith("/issues/42/labels")


def test_github_add_labels_applies_nothing_when_nothing_matches():
    fake = Fake(_gh_routes(["other"]))
    forge, ref = _gh(fake)
    assert forge.add_labels(ref, ["feat"]) == []
    assert fake.of("POST") == []


# --- GitLab labels -----------------------------------------------------------


def _gl_routes(existing):
    return {
        ("GET", "/labels"): _resp(200, [{"name": n} for n in existing]),
        ("POST", "/labels"): _resp(201, {}),
        ("PUT", "/merge_requests/42"): _resp(200, {}),
    }


def test_gitlab_list_labels_reads_project_and_group_labels():
    fake = Fake(_gl_routes(["feat", "scoped::x"]))
    forge, ref = _gl(fake)
    assert forge.list_labels(ref) == ["feat", "scoped::x"]
    assert fake.calls[0][2]["params"]["include_ancestor_groups"] == "true"


def test_gitlab_add_labels_without_create_missing_never_creates():
    fake = Fake(_gl_routes(["Fix"]))
    forge, ref = _gl(fake)
    assert forge.add_labels(ref, ["fix", "feat"]) == ["Fix"]
    assert fake.of("POST") == []
    (_, _, kw), = fake.of("PUT")
    assert kw["json"] == {"add_labels": "Fix"}


def test_gitlab_add_labels_with_create_missing_creates_first():
    fake = Fake(_gl_routes([]))
    forge, ref = _gl(fake)
    assert forge.add_labels(ref, ["feat"], create_missing=True) == ["feat"]
    assert fake.calls[1][0] == "POST" and fake.calls[1][2]["json"]["name"] == "feat"
    assert fake.calls[2][0] == "PUT"


# --- Gitea labels ------------------------------------------------------------


def _gt_routes(existing):
    return {
        ("GET", "/repos/acme/api/labels"): _resp(
            200, [{"name": n, "id": i + 1} for i, n in enumerate(existing)]
        ),
        ("POST", "/repos/acme/api/labels"): _resp(201, {"id": 99, "name": "feat"}),
        ("POST", "/issues/42/labels"): _resp(200, []),
    }


def test_gitea_list_labels():
    fake = Fake(_gt_routes(["feat", "fix"]))
    forge, ref = _gt(fake)
    assert forge.list_labels(ref) == ["feat", "fix"]


def test_gitea_add_labels_without_create_missing_never_creates():
    fake = Fake(_gt_routes(["Fix"]))
    forge, ref = _gt(fake)
    assert forge.add_labels(ref, ["fix", "feat"]) == ["Fix"]
    (_, url, kw), = fake.of("POST")
    assert url.endswith("/issues/42/labels") and kw["json"] == {"labels": [1]}


def test_gitea_add_labels_with_create_missing_applies_the_created_id():
    fake = Fake(_gt_routes(["fix"]))
    forge, ref = _gt(fake)
    assert forge.add_labels(ref, ["feat", "fix"], create_missing=True) == ["feat", "fix"]
    created, applied = fake.of("POST")
    assert created[1].endswith("/repos/acme/api/labels")
    assert applied[2]["json"] == {"labels": [99, 1]}


# --- Azure DevOps labels -----------------------------------------------------


def _az_routes(pr_labels, others):
    return {
        ("GET", "/pullrequests/551/labels"): _resp(
            200, {"value": [{"name": n, "active": True} for n in pr_labels]}
        ),
        ("GET", "/pullrequests"): _resp(
            200, {"value": [{"labels": [{"name": n, "active": True}]} for n in others]}
        ),
        ("POST", "/pullrequests/551/labels"): _resp(200, {}),
    }


def test_azure_list_labels_unions_this_pr_and_recent_prs():
    fake = Fake(_az_routes(["feat"], ["Fix", "feat"]))
    forge, ref = _az(fake)
    assert forge.list_labels(ref) == ["feat", "Fix"]


def test_azure_add_labels_without_create_missing_only_applies_known_tags():
    fake = Fake(_az_routes([], ["Fix"]))
    forge, ref = _az(fake)
    assert forge.add_labels(ref, ["fix", "feat"]) == ["Fix"]
    (_, _, kw), = fake.of("POST")
    assert kw["json"] == {"name": "Fix"}


def test_azure_add_labels_with_create_missing_posts_every_tag():
    fake = Fake(_az_routes([], ["Fix"]))
    forge, ref = _az(fake)
    assert forge.add_labels(ref, ["feat", "fix"], create_missing=True) == ["feat", "Fix"]
    assert [c[2]["json"] for c in fake.of("POST")] == [{"name": "feat"}, {"name": "Fix"}]


def test_azure_list_labels_raises_feed_read_error_on_http_failure():
    fake = Fake({("GET", "/pullrequests"): _resp(500, {})})
    forge, ref = _az(fake)
    with pytest.raises(FeedReadError):
        forge.list_labels(ref)
