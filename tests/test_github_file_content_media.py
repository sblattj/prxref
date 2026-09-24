"""GitHub labels a raw file ``application/vnd.github.raw+json``.

``get_file_content`` asks the contents API for the raw media type. Live,
github.com answers a regular file with HTTP 200, the file's own bytes, and
``Content-Type: application/vnd.github.raw+json; charset=utf-8``; a directory
answers ``application/json; charset=utf-8`` with a JSON listing. The adapter
used to reject any Content-Type containing ``json``, so it dropped every
regular file it read and GitHub reviews ran with no full-file context
(dependency versions, symbol definitions). The other GitHub tests mocked the
raw body as ``text/plain``, which the live API does not send.

These tests pin the media-type rule through the real ``get_file_content``
with a mocked session, and drive it once through ``orchestrate_review`` to
the worker prompt.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest
import requests

from prxref.forges import github
from prxref.forges.base import PRRef
from prxref.forges.github import ForgeImpl
from prxref.orchestrator import orchestrate_review
from tests.test_forge_github import _mock_response, _ref
from tests.test_issue_01_library_version_context import (
    PACKAGE_JSON,
    REF,
    TS_DIFF,
    CapturingLLM,
)
from tests.test_orchestrator import FakeForge, make_pr

LIVE_RAW = "application/vnd.github.raw+json; charset=utf-8"
DIRECTORY = "application/json; charset=utf-8"
SHA = "deadbeef"
PY_SOURCE = (
    "import os\n"
    "\n"
    "\n"
    "def main() -> None:\n"
    "    print(os.getcwd())\n"
)
LISTING = [
    {"name": "app.py", "path": "src/app.py", "type": "file"},
    {"name": "util", "path": "src/util", "type": "dir"},
]
ENVELOPE = {
    "type": "file",
    "path": "src/app.py",
    "encoding": "base64",
    "content": "aW1wb3J0IG9zCg==",
}


def _read(content_type, *, text=PY_SOURCE, content=None, json_data=None, path="src/app.py"):
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(
        200,
        json_data=json_data,
        text=text,
        content=content,
        headers={"Content-Type": content_type},
    )
    return ForgeImpl(session=session).get_file_content(_ref(), path, sha=SHA)


# --- raw bodies are the file ---------------------------------------------------


def test_the_live_raw_json_media_type_returns_the_file_text():
    assert _read(LIVE_RAW) == PY_SOURCE


def test_a_raw_json_file_whose_own_body_is_json_returns_the_text():
    body = '{"name": "x", "dependencies": {"effect": "4.0.0"}}'

    assert _read(LIVE_RAW, text=body, path="package.json") == body


@pytest.mark.parametrize(
    "content_type",
    [
        "application/vnd.github.raw",
        "application/vnd.github.v3.raw",
        "application/vnd.github.v3.raw+json",
        "text/plain; charset=utf-8",
        "text/x-python",
    ],
)
def test_the_other_raw_variants_and_text_return_the_file_text(content_type):
    assert _read(content_type) == PY_SOURCE


# --- JSON envelopes are not ------------------------------------------------------


def test_a_directory_listing_returns_none():
    assert _read(DIRECTORY, json_data=LISTING, path="src") is None


@pytest.mark.parametrize(
    "content_type",
    [
        "application/vnd.github+json",
        "application/vnd.github.object+json",
        "application/vnd.github.v3+json; charset=utf-8",
    ],
)
def test_a_non_raw_json_envelope_returns_none(content_type):
    assert _read(content_type, json_data=ENVELOPE) is None


# --- media-type parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "content_type",
    [
        "APPLICATION/VND.GITHUB.RAW+JSON; CHARSET=UTF-8",
        "Application/Vnd.GitHub.Raw+Json;charset=utf-8",
        "  application/vnd.github.raw+json ; charset=utf-8",
        "\tapplication/vnd.github.raw+json\t",
        "TEXT/PLAIN ; Charset=UTF-8",
    ],
)
def test_a_raw_media_type_is_read_case_insensitively_past_parameters_and_whitespace(
    content_type,
):
    assert _read(content_type) == PY_SOURCE


@pytest.mark.parametrize(
    "content_type",
    [
        "APPLICATION/JSON",
        " application/json ;charset=utf-8",
        "Application/Vnd.GitHub.Object+JSON; charset=utf-8",
    ],
)
def test_an_envelope_media_type_is_read_case_insensitively_past_parameters_and_whitespace(
    content_type,
):
    assert _read(content_type, json_data=LISTING) is None


@pytest.mark.parametrize(
    ("content_type", "envelope"),
    [
        ("application/vnd.github.raw+json; charset=utf-8", False),
        ("application/vnd.github.raw", False),
        ("application/vnd.github.v3.raw", False),
        ("application/vnd.github.v3.raw+json", False),
        ("text/plain; charset=utf-8", False),
        ("application/octet-stream", False),
        ("", False),
        ("application/json; charset=utf-8", True),
        ("application/vnd.github+json", True),
        ("application/vnd.github.object+json", True),
        (" APPLICATION/JSON ; charset=utf-8", True),
    ],
)
def test_is_json_envelope_decides_on_the_media_type(content_type, envelope):
    assert github._is_json_envelope(content_type) is envelope


# --- the size and binary checks still run after the media-type rule ------------


def test_a_raw_json_body_at_the_size_limit_is_returned():
    body = b"a" * github._MAX_FILE_CONTENT_BYTES

    assert _read(LIVE_RAW, content=body) == body.decode("ascii")


def test_the_size_check_still_drops_a_raw_json_body_over_the_limit():
    body = b"a" * (github._MAX_FILE_CONTENT_BYTES + 1)

    assert _read(LIVE_RAW, content=body) is None


def test_the_binary_check_still_drops_a_raw_json_body_with_a_nul_byte():
    assert _read(LIVE_RAW, content=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR") is None


# --- through the orchestrator to the worker prompt -----------------------------


class GitHubBackedForge(FakeForge):
    """FakeForge whose ``get_file_content`` is the real GitHub adapter.

    The adapter's session is a mock that serves ``package.json`` at the PR
    head under ``manifest_type`` and answers every other path with a 404.
    """

    def __init__(self, *args, manifest_type: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.manifest_type = manifest_type
        self.content_urls: list[str] = []
        self._lock = threading.Lock()
        session = MagicMock(spec=requests.Session)
        session.get.side_effect = self._get
        self._github = ForgeImpl(session=session)
        self._github_ref = _ref("https://github.com/acme/widget/pull/7")

    def _get(self, url, headers=None, params=None, **kwargs):
        with self._lock:
            self.content_urls.append(url)
        if url.endswith("/contents/package.json") and (params or {}).get(
            "ref"
        ) == self.pr.source_sha:
            return _mock_response(
                200, text=PACKAGE_JSON, headers={"Content-Type": self.manifest_type}
            )
        return _mock_response(404, json_data={"message": "Not Found"})

    def get_file_content(self, ref: PRRef, path: str, *, sha: str) -> str | None:
        return self._github.get_file_content(self._github_ref, path, sha=sha)


MANIFEST_URL = "https://api.github.com/repos/acme/widget/contents/package.json"


@pytest.fixture
def llm() -> CapturingLLM:
    return CapturingLLM()


def test_a_raw_json_manifest_reaches_the_worker_prompt_through_the_github_adapter(
    llm, monkeypatch
):
    monkeypatch.delenv("PRXREF_GITHUB_TOKEN", raising=False)
    forge = GitHubBackedForge(pr=make_pr("Wire Effect"), diff=TS_DIFF, manifest_type=LIVE_RAW)

    result = orchestrate_review(forge, REF, llm, post=False)

    assert result["chunk_count"] >= 1
    assert MANIFEST_URL in forge.content_urls
    prompts = llm.user_prompts
    assert prompts, "no LLM call was made"
    assert any("### Dependency versions" in p for p in prompts), (
        "the GitHub adapter dropped package.json, so no worker prompt carried "
        "a dependency block"
    )
    assert any("effect@4.0.0-rc.110" in p for p in prompts)


def test_control_a_json_envelope_manifest_gives_no_dependency_block(llm, monkeypatch):
    monkeypatch.delenv("PRXREF_GITHUB_TOKEN", raising=False)
    forge = GitHubBackedForge(pr=make_pr("Wire Effect"), diff=TS_DIFF, manifest_type=DIRECTORY)

    result = orchestrate_review(forge, REF, llm, post=False)

    assert result["chunk_count"] >= 1
    assert MANIFEST_URL in forge.content_urls
    prompts = llm.user_prompts
    assert prompts, "no LLM call was made"
    assert any("import { Effect } from 'effect';" in p for p in prompts)
    assert not any("### Dependency versions" in p for p in prompts)
