"""End-to-end test of GitHub's 406 too_large fallback (issue #15).

``test_github_get_diff.py`` pins the 406 detection and hand-off
to ``_get_diff_from_files``, with that method doubled out on every test.
``test_github_files_fallback.py`` pins the listing rebuild by
calling ``_get_diff_from_files`` directly, with the diff GET never in the
picture. Each module's tests double away the other half, so neither drives the
real path: a real 406 too_large refusal, a real PR-metadata read, a compare
read that fails (HTTP 404 here; the compare-first path has its own module,
``test_github_compare_fallback.py``), a real paged ``/pulls/{n}/files`` walk,
and a real render into a unified diff that ``prxref.triage.parse_unified_diff``
can parse. This module drives that whole path in one call, with only the HTTP
session (and, in the CLI cases, the LLM) faked.
"""
from __future__ import annotations

import json
import logging
import sys
from unittest.mock import MagicMock

import pytest
import requests

from prxref import orchestrator as real_orchestrator
from prxref.cli import main
from prxref.forges.github import ForgeImpl
from prxref.triage import parse_unified_diff
from tests.test_forge_github import _mock_response, _ref, _routed_session
from tests.test_orchestrator import FakeLLM

API = "https://api.github.com/repos/acme/api"
PR_API_URL = f"{API}/pulls/42"
FILES_URL = f"{API}/pulls/42/files"
COMPARE_URL = f"{API}/compare/{'a' * 40}...{'b' * 40}"
COMPARE_WARNING = (
    "GitHub PR diff for acme/api#42: the compare diff was not used (it answered "
    "HTTP 404); rebuilding it from the changed-file listing"
)

# GitHub's verbatim 406 body for a diff past its 20,000-line limit.
TOO_LARGE = {
    "message": "Sorry, the diff exceeded the maximum number of lines (20000)",
    "errors": [{"resource": "PullRequest", "field": "diff", "code": "too_large"}],
}
CONTROL_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x\n+y\n"
)


@pytest.fixture(autouse=True)
def _no_github_token(monkeypatch):
    monkeypatch.delenv("PRXREF_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PRXREF_GITHUB_ENTERPRISE_TOKEN", raising=False)


def _file(name, status="modified", patch="@@ -1 +1 @@\n-old\n+new", **extra):
    entry = {"filename": name, "status": status, **extra}
    if patch is not None:
        entry["patch"] = patch
    return entry


def _pr_json(changed_files):
    """The listings here carry no line counts, so the PR's totals are zero."""
    return {
        "title": "t", "body": "", "user": {"login": "dev"},
        "head": {"ref": "feat", "sha": "b" * 40},
        "base": {"ref": "main", "sha": "a" * 40},
        "changed_files": changed_files,
        "additions": 0,
        "deletions": 0,
    }


def _session(listing, *, changed_files=None, diff_status=406, diff_text=""):
    """Route the diff GET, the compare GET (404), the paged files listing, and
    the PR-metadata GET from one session double -- the real end-to-end path,
    not a stand-in for any one leg of it.

    Routes on URL and the diff Accept header, never on call order, so the
    order asserted below is the adapter's, not the double's.
    """
    if changed_files is None:
        changed_files = len(listing)

    def get(url, headers=None, params=None, **kwargs):
        accept = (headers or {}).get("Accept", "")
        if url == COMPARE_URL:
            return _mock_response(404, json_data={"message": "Not Found"})
        if url == PR_API_URL and "diff" in accept:
            if diff_status == 200:
                return _mock_response(text=diff_text)
            return _mock_response(diff_status, json_data=TOO_LARGE)
        if url == FILES_URL:
            start = (params["page"] - 1) * params["per_page"]
            return _mock_response(json_data=listing[start:start + params["per_page"]])
        if url == PR_API_URL:
            return _mock_response(json_data=_pr_json(changed_files))
        raise AssertionError(f"unrouted GET {url}")

    session = MagicMock(spec=requests.Session)
    session.get.side_effect = get
    return session


# --- case 1: a real 406 -> a real two-page listing -> a diff the parser reads ---


def test_a_real_406_rebuilds_a_diff_the_parser_can_read(caplog):
    listing = [_file(f"src/mod_{i:03d}.py") for i in range(100)]
    listing += [_file(f"src/tail_{i}.py") for i in range(20)]
    session = _session(listing)
    forge = ForgeImpl(session=session)

    with caplog.at_level(logging.WARNING, logger="prxref.forges.github"):
        diff = forge.get_diff(_ref())

    files = parse_unified_diff(diff)
    assert [f.path for f in files] == [f["filename"] for f in listing]
    assert len(files) == 120
    assert [
        r.getMessage() for r in caplog.records if r.name == "prxref.forges.github"
    ] == [COMPARE_WARNING]

    calls = session.get.call_args_list
    assert [c[0][0] for c in calls] == [
        PR_API_URL, PR_API_URL, COMPARE_URL, FILES_URL, FILES_URL,
    ]
    assert "diff" in calls[0][1]["headers"]["Accept"]
    assert "diff" not in calls[1][1]["headers"]["Accept"]
    assert [c[1]["params"]["page"] for c in calls[3:5]] == [1, 2]


# --- case 2: a listed file with no patch and no changed lines is header-only ------


def test_a_patchless_listed_file_is_header_only_with_no_warning_of_its_own(caplog):
    listing = [_file("src/app.py"), _file("assets/logo.png", patch=None)]
    session = _session(listing)
    forge = ForgeImpl(session=session)

    with caplog.at_level(logging.WARNING, logger="prxref.forges.github"):
        diff = forge.get_diff(_ref())

    files = parse_unified_diff(diff)
    assert [f.path for f in files] == ["src/app.py", "assets/logo.png"]
    logo = files[1]
    assert logo.hunks == []

    warnings = [
        r.getMessage() for r in caplog.records
        if r.name == "prxref.forges.github" and r.levelno == logging.WARNING
    ]
    assert warnings == [COMPARE_WARNING]


# --- case 3: a short listing raises, and returns nothing -------------------------


def test_a_short_listing_raises_value_error_with_no_partial_diff():
    listing = [_file(f"src/mod_{i}.py") for i in range(4)]
    session = _session(listing, changed_files=7)
    forge = ForgeImpl(session=session)

    with pytest.raises(ValueError, match="changed_files=7 but the changed-file listing returned 4"):
        forge.get_diff(_ref())


# --- case 4: control, a 200 diff never touches the files listing -----------------


def test_a_200_diff_makes_exactly_one_request_and_never_touches_files():
    session = _session([], diff_status=200, diff_text=CONTROL_DIFF)
    forge = ForgeImpl(session=session)

    assert forge.get_diff(_ref()) == CONTROL_DIFF

    assert session.get.call_count == 1
    assert session.get.call_args[0][0] == PR_API_URL
    assert "diff" in session.get.call_args[1]["headers"]["Accept"]


# --- case 5: through the CLI, with the real orchestrator and a fake LLM ----------


def _cli_session(listing, *, changed_files=None):
    """``_routed_session`` (list_threads, get_file_content, ...) plus a real
    406 diff refusal, a compare 404 and a real files listing, so ``get_diff``
    runs for real inside a full ``prxref review`` invocation.
    """
    base = _routed_session(summary_feed=[])
    routed = base.get.side_effect
    if changed_files is None:
        changed_files = len(listing)

    def get(url, headers=None, params=None, **kwargs):
        accept = (headers or {}).get("Accept", "")
        if url == COMPARE_URL:
            return _mock_response(404, json_data={"message": "Not Found"})
        if url == PR_API_URL and "diff" in accept:
            return _mock_response(406, json_data=TOO_LARGE)
        if url == FILES_URL:
            start = (params["page"] - 1) * params["per_page"]
            return _mock_response(json_data=listing[start:start + params["per_page"]])
        if url == PR_API_URL:
            return _mock_response(json_data=_pr_json(changed_files))
        return routed(url, headers=headers, params=params, **kwargs)

    base.get.side_effect = get
    return base


class TestThroughTheCli:
    """``prxref review`` over the real orchestrator and the real GitHub
    adapter, with a real 406 -> listing -> rebuild. Only the HTTP session and
    the model are doubles.
    """

    @staticmethod
    def _review(ref):
        return main(["review", "--pr-url", ref.url, "--no-post", "--format", "json"])

    def test_the_406_path_yields_a_reviewed_verdict(self, monkeypatch, capsys):
        assert sys.modules["prxref.orchestrator"] is real_orchestrator
        listing = [_file(f"src/mod_{i}.py") for i in range(5)]
        forge = ForgeImpl(session=_cli_session(listing))
        llm = FakeLLM('{"findings": []}')
        ref = _ref()
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: ref)
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: forge)
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: llm)

        assert self._review(ref) == 0

        out, _ = capsys.readouterr()
        assert json.loads(out)["verdict"] != "Error"
        assert llm.calls >= 1

    def test_a_short_listing_ends_as_an_error_run_that_exits_0(self, monkeypatch, capsys):
        listing = [_file(f"src/mod_{i}.py") for i in range(4)]
        forge = ForgeImpl(session=_cli_session(listing, changed_files=7))
        llm = FakeLLM('{"findings": []}')
        ref = _ref()
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: ref)
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: forge)
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: llm)

        assert self._review(ref) == 0

        out, _ = capsys.readouterr()
        assert json.loads(out)["verdict"] == "Error"
        assert llm.calls == 0
