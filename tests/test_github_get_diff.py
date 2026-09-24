"""Direct tests of ``github.ForgeImpl.get_diff``: the diff request and its 406 fallback.

GitHub refuses the diff media type for a pull request whose diff runs past
20,000 lines, with HTTP 406 and an ``errors`` entry coded ``too_large``. Exactly
that answer hands the read to ``_get_diff_from_files``, which rebuilds the diff
from the changed-file listing; every other answer is the single GET it always
was. ``_get_diff_from_files`` is a double set on the instance in every test
here, so these pin the detection and the hand-off, not the listing itself.
"""
from __future__ import annotations

import json
import logging
import sys
import types
from unittest.mock import MagicMock

import pytest
import requests

from prxref import orchestrator as real_orchestrator
from prxref.cli import main
from prxref.forges.base import FeedReadError
from prxref.forges.github import ForgeImpl
from tests.test_forge_github import REQUEST_TIMEOUT, _mock_response, _ref, _routed_session
from tests.test_orchestrator import FakeLLM

DIFF_ACCEPT = "application/vnd.github.v3.diff, application/vnd.diff"
PR_API_URL = "https://api.github.com/repos/acme/api/pulls/42"
DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x\n+y\n"
)
# The body GitHub sent for the 0.14.0 release PR, verbatim.
TOO_LARGE = {
    "message": "Sorry, the diff exceeded the maximum number of lines (20000)",
    "errors": [{"resource": "PullRequest", "field": "diff", "code": "too_large"}],
}
DEBUG_LINE = (
    "diff for acme/api#42 exceeded GitHub's line limit (406 too_large); "
    "rebuilding it from the files listing"
)
CAP_ERROR = (
    "acme/api#42: changed_files=3200 but the files listing returned 3000 "
    "(GitHub caps it at 3,000)"
)


@pytest.fixture(autouse=True)
def _no_github_token(monkeypatch):
    monkeypatch.delenv("PRXREF_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PRXREF_GITHUB_ENTERPRISE_TOKEN", raising=False)


def _forge(resp):
    session = MagicMock(spec=requests.Session)
    session.get.return_value = resp
    return ForgeImpl(session=session), session


def _fallback(monkeypatch, forge, **kwargs):
    """Stand in for ``_get_diff_from_files``, which is another seat's method."""
    fallback = MagicMock(**kwargs)
    monkeypatch.setattr(forge, "_get_diff_from_files", fallback, raising=False)
    return fallback


# --- under the limit: the request as it always was -----------------------------


@pytest.mark.parametrize(
    ("url", "token_env", "expected_url", "expected_headers"),
    [
        (
            "https://github.com/acme/api/pull/42",
            None,
            PR_API_URL,
            {"Accept": DIFF_ACCEPT},
        ),
        (
            "https://git.corp.example/acme/api/pull/42",
            ("PRXREF_GITHUB_ENTERPRISE_TOKEN", "placeholder-token"),
            "https://git.corp.example/api/v3/repos/acme/api/pulls/42",
            {"Accept": DIFF_ACCEPT, "Authorization": "Bearer placeholder-token"},
        ),
    ],
    ids=["github.com-anonymous", "ghes-token"],
)
def test_under_the_limit_is_exactly_one_get_with_the_diff_accept_header(
    monkeypatch, caplog, url, token_env, expected_url, expected_headers
):
    """The issue's acceptance test: a pull request under the limit makes exactly
    the request it made before the fallback existed, and returns its body."""
    if token_env is not None:
        monkeypatch.setenv(*token_env)
    resp = _mock_response(text=DIFF)
    forge, session = _forge(resp)
    fallback = _fallback(monkeypatch, forge)

    with caplog.at_level(logging.DEBUG, logger="prxref.forges.github"):
        assert forge.get_diff(_ref(url)) == DIFF

    session.get.assert_called_once_with(
        expected_url, headers=expected_headers, timeout=REQUEST_TIMEOUT
    )
    resp.json.assert_not_called()
    fallback.assert_not_called()
    assert [r for r in caplog.records if r.name == "prxref.forges.github"] == []


# --- 406 too_large: rebuild from the listing -----------------------------------


@pytest.mark.parametrize(
    "body",
    [
        TOO_LARGE,
        {"errors": [{"code": "custom"}, {"field": "diff", "code": "too_large"}]},
    ],
    ids=["github-body", "too_large-among-other-errors"],
)
def test_406_too_large_rebuilds_the_diff_from_the_files_listing(monkeypatch, body):
    resp = _mock_response(406, json_data=body)
    forge, session = _forge(resp)
    fallback = _fallback(monkeypatch, forge, return_value=DIFF)
    ref = _ref()

    assert forge.get_diff(ref) == DIFF

    fallback.assert_called_once_with(ref)
    assert session.get.call_count == 1
    resp.raise_for_status.assert_not_called()


def test_the_fallback_logs_one_debug_line_and_nothing_louder(monkeypatch, caplog):
    """A successful fallback is DEBUG only (decisions #15): the rebuilt diff is
    the same content, so it earns no warning and no summary line."""
    forge, _ = _forge(_mock_response(406, json_data=TOO_LARGE))
    _fallback(monkeypatch, forge, return_value=DIFF)

    with caplog.at_level(logging.DEBUG, logger="prxref.forges.github"):
        forge.get_diff(_ref())

    ours = [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.name == "prxref.forges.github"
    ]
    assert ours == [(logging.DEBUG, DEBUG_LINE)]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


@pytest.mark.parametrize(
    "exc",
    [
        ValueError(CAP_ERROR),
        FeedReadError("pull request files for acme/api#42 returned HTTP 502 at page 7"),
    ],
    ids=["past-the-file-cap", "listing-read-failed"],
)
def test_a_failed_rebuild_propagates(monkeypatch, exc):
    """A listing that cannot be read whole fails the read; it never returns short."""
    forge, _ = _forge(_mock_response(406, json_data=TOO_LARGE))
    _fallback(monkeypatch, forge, side_effect=exc)

    with pytest.raises(type(exc)) as info:
        forge.get_diff(_ref())

    assert info.value is exc


# --- every other refusal raises as before --------------------------------------


@pytest.mark.parametrize(
    "resp_kwargs",
    [
        {"json_data": {"message": "Unsupported 'Accept' header"}},
        {"json_data": {"message": "Not Acceptable", "errors": []}},
        {"json_data": {"errors": [{"field": "diff", "code": "invalid"}]}},
        {"json_data": {"message": TOO_LARGE["message"]}},
        {"json_data": {"errors": {"code": "too_large"}}},
        {"json_data": {"errors": ["too_large"]}},
        {"json_data": [{"code": "too_large"}]},
        {"text": "Not Acceptable"},
        {"text": ""},
    ],
    ids=[
        "no-errors-key",
        "empty-errors",
        "another-code",
        "too-large-only-in-the-message",
        "errors-not-a-list",
        "error-entries-not-objects",
        "body-is-a-list",
        "body-not-json",
        "empty-body",
    ],
)
def test_a_406_without_too_large_still_raises(monkeypatch, resp_kwargs):
    """A bare 406 (a wrong media type, say) is a real misconfiguration, not a
    large pull request, so the fallback must not mask it."""
    resp = _mock_response(406, **resp_kwargs)
    forge, session = _forge(resp)
    fallback = _fallback(monkeypatch, forge)

    with pytest.raises(requests.HTTPError):
        forge.get_diff(_ref())

    fallback.assert_not_called()
    assert session.get.call_count == 1


def _real_406(content: bytes) -> requests.Response:
    resp = requests.Response()
    resp.status_code = 406
    resp.reason = "Not Acceptable"
    resp.url = PR_API_URL
    resp.encoding = "utf-8"
    resp._content = content
    return resp


@pytest.mark.parametrize(
    ("content", "falls_back"),
    [
        (json.dumps(TOO_LARGE).encode(), True),
        (b"Not Acceptable", False),
        (b"", False),
    ],
    ids=["too_large", "not-json", "empty"],
)
def test_a_real_406_response_is_read_the_same_way(monkeypatch, content, falls_back):
    """A real ``requests.Response``, not a double: its JSON decode error is the
    one the detection must survive."""
    forge, _ = _forge(_real_406(content))
    fallback = _fallback(monkeypatch, forge, return_value=DIFF)

    if falls_back:
        assert forge.get_diff(_ref()) == DIFF
    else:
        with pytest.raises(requests.HTTPError, match="406"):
            forge.get_diff(_ref())

    assert fallback.call_count == (1 if falls_back else 0)


@pytest.mark.parametrize("status", [401, 404, 422, 500])
def test_every_other_status_raises_as_before_even_with_a_too_large_body(
    monkeypatch, status
):
    resp = _mock_response(status, json_data=TOO_LARGE)
    forge, session = _forge(resp)
    fallback = _fallback(monkeypatch, forge)

    with pytest.raises(requests.HTTPError):
        forge.get_diff(_ref())

    resp.json.assert_not_called()
    fallback.assert_not_called()
    assert session.get.call_count == 1


# --- through the CLI and the real orchestrator ---------------------------------


def _session_refusing_the_diff():
    """The adapter's routed session double, with the diff answering 406 too_large."""
    session = _routed_session(summary_feed=[])
    routed = session.get.side_effect

    def get(url, headers=None, params=None, **kwargs):
        if url == PR_API_URL and "diff" in (headers or {}).get("Accept", ""):
            return _mock_response(406, json_data=TOO_LARGE)
        return routed(url, headers=headers, params=params, **kwargs)

    session.get.side_effect = get
    return session


class TestThroughTheCli:
    """``prxref review`` over the real orchestrator and the real GitHub adapter.

    Only the HTTP session, the model and ``_get_diff_from_files`` are doubles.
    A rebuild that raises is a failed ``get_diff``: verdict ``Error`` and exit
    0, never a partial review. The control lets the same rebuild return a
    diff, and the same rig then reviews it.
    """

    @pytest.fixture
    def rig(self, monkeypatch):
        assert sys.modules["prxref.orchestrator"] is real_orchestrator
        forge = ForgeImpl(session=_session_refusing_the_diff())
        fallback = _fallback(monkeypatch, forge)
        llm = FakeLLM('{"findings": []}')
        ref = _ref()
        monkeypatch.setattr("prxref.cli.detect_forge", lambda url: ref)
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: forge)
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: llm)
        return types.SimpleNamespace(fallback=fallback, llm=llm, ref=ref)

    @staticmethod
    def _review(ref):
        return main(["review", "--pr-url", ref.url, "--no-post", "--format", "json"])

    def test_a_listing_past_the_file_cap_ends_as_an_error_run_that_exits_0(
        self, rig, capsys, caplog
    ):
        rig.fallback.side_effect = ValueError(CAP_ERROR)

        with caplog.at_level(logging.ERROR, logger="prxref"):
            assert self._review(rig.ref) == 0

        out, _ = capsys.readouterr()
        assert json.loads(out)["verdict"] == "Error"
        rig.fallback.assert_called_once_with(rig.ref)
        assert rig.llm.calls == 0
        assert f"get_diff failed: {CAP_ERROR}" in caplog.text

    def test_control_a_rebuilt_diff_is_reviewed(self, rig, capsys):
        rig.fallback.return_value = DIFF

        assert self._review(rig.ref) == 0

        out, _ = capsys.readouterr()
        assert json.loads(out)["verdict"] != "Error"
        rig.fallback.assert_called_once_with(rig.ref)
        assert rig.llm.calls >= 1
