"""Tests for GitHub's PR diff rebuilt from the ``/pulls/{n}/files`` listing.

``ForgeImpl._get_diff_from_files`` is the fallback for a PR whose diff GitHub
refuses to serve through the diff media type and whose compare diff could not
be used (tests/test_github_compare_fallback.py). It pages the changed-file
listing, maps each entry into the shared renderer's GitLab-shaped entry, and
refuses to return a diff the listing may have cut short.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock

import pytest
import requests

from prxref.forges import github, gitlab
from prxref.forges.base import FeedReadError
from prxref.forges.github import ForgeImpl
from prxref.triage import parse_unified_diff

PAGE_SIZE = 100
API = "https://api.github.com/repos/acme/api"
FILES_URL = f"{API}/pulls/42/files"
PR_URL = f"{API}/pulls/42"
REQUEST_TIMEOUT = (10.0, 30.0)


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.headers = {}
    if json_data is not None:
        resp.json.return_value = json_data
        resp.text = json.dumps(json_data)
    else:
        resp.text = text
        resp.json.side_effect = ValueError("No JSON")
    resp.raise_for_status.side_effect = (
        None if resp.ok else requests.HTTPError(response=resp)
    )
    return resp


def _ref(url="https://github.com/acme/api/pull/42"):
    ref = ForgeImpl.parse_pr_url(url)
    assert ref is not None
    return ref


# ``changed_files`` sentinels: the listing's own length, or no key at all.
_LISTED = object()
_ABSENT = object()


def _pr(changed_files, listing):
    """The PR metadata; its ``additions``/``deletions`` are the listing's own
    totals, as GitHub's are, so the totals check passes unless a test says so."""
    pr = {
        "title": "t", "body": "", "user": {"login": "dev"},
        "head": {"ref": "feat", "sha": "b" * 40},
        "base": {"ref": "main", "sha": "a" * 40},
        "additions": sum(f.get("additions", 0) for f in listing),
        "deletions": sum(f.get("deletions", 0) for f in listing),
    }
    if changed_files is not _ABSENT:
        pr["changed_files"] = changed_files
    return pr


def _session(listing, changed_files=_LISTED, *, listing_status=200, pr_status=200):
    """A Session double serving ``listing`` a page at a time, plus the PR itself.

    Routes on URL and the ``page`` parameter, never on call order, so the order
    of the two reads is not load-bearing. A page past the end of ``listing``
    comes back as ``[]``, which is how GitHub answers it.
    """
    if changed_files is _LISTED:
        changed_files = len(listing)

    def get(url, headers=None, params=None, **kwargs):
        if url.endswith("/pulls/42/files"):
            if listing_status != 200:
                return _mock_response(listing_status, json_data={"message": "boom"})
            start = (params["page"] - 1) * params["per_page"]
            return _mock_response(json_data=listing[start:start + params["per_page"]])
        if url.endswith("/pulls/42"):
            if pr_status != 200:
                return _mock_response(pr_status, json_data={"message": "Not Found"})
            return _mock_response(json_data=_pr(changed_files, listing))
        raise AssertionError(f"unrouted GET {url}")

    session = MagicMock(spec=requests.Session)
    session.get.side_effect = get
    return session


def _file(name, status="modified", patch="@@ -1 +1 @@\n-old\n+new", **extra):
    entry = {"filename": name, "status": status, **extra}
    if patch is not None:
        entry["patch"] = patch
    return entry


def _rebuild(session, ref=None):
    """Read the PR metadata, then rebuild from the listing, as ``get_diff`` does."""
    forge, ref = ForgeImpl(session=session), ref or _ref()
    return forge._get_diff_from_files(ref, forge.get_pr(ref))


def _gets_to(session, url):
    return [c for c in session.get.call_args_list if c[0][0] == url]


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING]


# --- paging -------------------------------------------------------------------


def test_reads_the_listing_to_its_last_page(monkeypatch):
    monkeypatch.setenv("PRXREF_GITHUB_TOKEN", "t0ken")
    listing = [_file(f"src/mod_{i:03d}.py") for i in range(130)]
    session = _session(listing)

    diff = _rebuild(session)

    listing_gets = _gets_to(session, FILES_URL)
    assert [c[1]["params"]["page"] for c in listing_gets] == [1, 2]
    assert all(c[1]["params"]["per_page"] == PAGE_SIZE for c in listing_gets)
    assert len(_gets_to(session, PR_URL)) == 1
    for call in session.get.call_args_list:
        assert call[1]["headers"]["Accept"] == "application/vnd.github+json"
        assert call[1]["headers"]["Authorization"] == "Bearer t0ken"
        assert call[1]["timeout"] == REQUEST_TIMEOUT == github._REQUEST_TIMEOUT

    files = parse_unified_diff(diff)
    assert [f.path for f in files] == [f"src/mod_{i:03d}.py" for i in range(130)]
    assert all(f.status == "modified" and f.added_lines == {1} for f in files)


def test_uses_the_enterprise_api_base(monkeypatch):
    monkeypatch.setenv("PRXREF_GITHUB_ENTERPRISE_TOKEN", "ghes-t0ken")
    session = _session([_file("src/app.py")])

    _rebuild(session, _ref("https://git.corp.example/acme/api/pull/42"))

    urls = {c[0][0] for c in session.get.call_args_list}
    assert urls == {
        "https://git.corp.example/api/v3/repos/acme/api/pulls/42/files",
        "https://git.corp.example/api/v3/repos/acme/api/pulls/42",
    }
    assert all(
        c[1]["headers"]["Authorization"] == "Bearer ghes-t0ken"
        for c in session.get.call_args_list
    )


# --- header shapes, byte-for-byte against the GitLab renderer -------------------


def _gitlab_entry(old, new, *, new_file=False, deleted_file=False, renamed_file=False,
                  diff):
    return {
        "old_path": old, "new_path": new, "new_file": new_file,
        "deleted_file": deleted_file, "renamed_file": renamed_file, "diff": diff,
    }


HEADER_CASES = {
    "added": (
        _file("src/new.py", "added", "@@ -0,0 +1 @@\n+x"),
        _gitlab_entry("src/new.py", "src/new.py", new_file=True,
                      diff="@@ -0,0 +1 @@\n+x\n"),
        ["diff --git a/src/new.py b/src/new.py", "new file mode 100644",
         "--- /dev/null", "+++ b/src/new.py"],
        "added",
    ),
    "removed": (
        _file("src/gone.py", "removed", "@@ -1 +0,0 @@\n-x"),
        _gitlab_entry("src/gone.py", "src/gone.py", deleted_file=True,
                      diff="@@ -1 +0,0 @@\n-x\n"),
        ["diff --git a/src/gone.py b/src/gone.py", "deleted file mode 100644",
         "--- a/src/gone.py", "+++ /dev/null"],
        "removed",
    ),
    "renamed": (
        _file("src/new_name.py", "renamed", "@@ -1 +1 @@\n-a\n+b",
              previous_filename="src/old_name.py"),
        _gitlab_entry("src/old_name.py", "src/new_name.py", renamed_file=True,
                      diff="@@ -1 +1 @@\n-a\n+b\n"),
        ["diff --git a/src/old_name.py b/src/new_name.py",
         "rename from src/old_name.py", "rename to src/new_name.py",
         "--- a/src/old_name.py", "+++ b/src/new_name.py"],
        "renamed",
    ),
    "modified": (
        _file("src/app.py", "modified", "@@ -1 +1 @@\n-a\n+b"),
        _gitlab_entry("src/app.py", "src/app.py", diff="@@ -1 +1 @@\n-a\n+b\n"),
        ["diff --git a/src/app.py b/src/app.py", "--- a/src/app.py",
         "+++ b/src/app.py"],
        "modified",
    ),
    "changed": (
        _file("bin/run.sh", "changed", "@@ -1 +1 @@\n-a\n+b"),
        _gitlab_entry("bin/run.sh", "bin/run.sh", diff="@@ -1 +1 @@\n-a\n+b\n"),
        ["diff --git a/bin/run.sh b/bin/run.sh", "--- a/bin/run.sh",
         "+++ b/bin/run.sh"],
        "modified",
    ),
}


@pytest.mark.parametrize("status", sorted(HEADER_CASES))
def test_each_status_renders_the_gitlab_header_shape(status):
    github_entry, gitlab_entry, header, parsed_status = HEADER_CASES[status]

    diff = _rebuild(_session([github_entry]))

    assert diff == gitlab._render_diff_entries([gitlab_entry])
    assert diff.splitlines()[:len(header)] == header
    (parsed,) = parse_unified_diff(diff)
    assert parsed.status == parsed_status
    assert parsed.path == github_entry["filename"]


def test_a_copied_file_renders_plain_from_its_source():
    entry = _file("src/copy.py", "copied", "@@ -1 +1 @@\n-a\n+b",
                  previous_filename="src/orig.py")

    diff = _rebuild(_session([entry]))

    assert diff == gitlab._render_diff_entries([
        _gitlab_entry("src/orig.py", "src/copy.py", diff="@@ -1 +1 @@\n-a\n+b\n")
    ])
    assert diff.splitlines()[:3] == [
        "diff --git a/src/orig.py b/src/copy.py", "--- a/src/orig.py",
        "+++ b/src/copy.py",
    ]


def test_mixed_statuses_render_in_listing_order():
    listing = [HEADER_CASES[s][0] for s in ("added", "removed", "renamed", "modified")]
    expected = [HEADER_CASES[s][1] for s in ("added", "removed", "renamed", "modified")]

    diff = _rebuild(_session(listing))

    assert diff == gitlab._render_diff_entries(expected)


# --- a file listed without a patch ------------------------------------------------


@pytest.mark.parametrize("patch_form", ["absent", "none", "empty"])
def test_a_patchless_file_with_no_changed_lines_is_header_only_at_debug(caplog, patch_form):
    binary = {"filename": "assets/logo.png", "status": "modified"}
    if patch_form == "none":
        binary["patch"] = None
    elif patch_form == "empty":
        binary["patch"] = ""
    listing = [binary, _file("src/app.py")]

    with caplog.at_level(logging.DEBUG, logger="prxref.forges.github"):
        diff = _rebuild(_session(listing))

    header_only = (
        "diff --git a/assets/logo.png b/assets/logo.png\n"
        "--- a/assets/logo.png\n+++ b/assets/logo.png\n"
    )
    assert diff.startswith(header_only)
    assert diff == gitlab._render_diff_entries([
        _gitlab_entry("assets/logo.png", "assets/logo.png", diff=""),
        _gitlab_entry("src/app.py", "src/app.py", diff="@@ -1 +1 @@\n-old\n+new\n"),
    ])
    assert _warnings(caplog) == []
    assert [r.getMessage() for r in caplog.records if "header-only" in r.getMessage()] == [
        "GitHub PR diff: assets/logo.png is listed with no patch and no changed "
        "lines; it is reviewed as header-only"
    ]
    assert [f.path for f in parse_unified_diff(diff)] == ["assets/logo.png", "src/app.py"]


def test_a_complete_rebuild_logs_nothing_at_info_or_above(caplog):
    listing = [_file(f"src/mod_{i}.py") for i in range(3)]

    with caplog.at_level(logging.DEBUG, logger="prxref.forges.github"):
        _rebuild(_session(listing))

    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []


# --- completeness is asserted, never assumed -----------------------------------------


def test_a_listing_shorter_than_changed_files_raises_with_the_counts(caplog):
    listing = [_file(f"src/mod_{i}.py") for i in range(4)]
    listing.append({"filename": "assets/logo.png", "status": "modified"})

    with caplog.at_level(logging.WARNING, logger="prxref.forges.github"):
        with pytest.raises(ValueError) as exc:
            _rebuild(_session(listing, changed_files=7))

    message = str(exc.value)
    assert "acme/api#42" in message
    assert "changed_files=7" in message
    assert "returned 5" in message
    assert "3,000" in message
    assert _warnings(caplog) == []


def test_a_pr_past_the_three_thousand_file_cap_fails_rather_than_going_partial():
    listing = [_file(f"src/mod_{i:04d}.py") for i in range(3000)]
    session = _session(listing, changed_files=3001)

    with pytest.raises(
        ValueError,
        match=r"changed_files=3001 but the changed-file listing returned 3000 "
              r"\(GitHub caps the listing at 3,000 files\)",
    ):
        _rebuild(session)

    pages = [c[1]["params"]["page"] for c in _gets_to(session, FILES_URL)]
    assert pages == list(range(1, 32))
    assert len(_gets_to(session, PR_URL)) == 1


def test_a_listing_matching_changed_files_at_the_cap_is_whole():
    listing = [_file(f"src/mod_{i:04d}.py") for i in range(3000)]

    diff = _rebuild(_session(listing, changed_files=3000))

    assert len(parse_unified_diff(diff)) == 3000


@pytest.mark.parametrize(
    "changed_files", [_ABSENT, None, "1", True], ids=["absent", "null", "str", "bool"]
)
def test_pr_metadata_without_an_integer_changed_files_raises(changed_files):
    with pytest.raises(ValueError, match="no integer changed_files"):
        _rebuild(_session([_file("src/app.py")], changed_files=changed_files))


# --- failures ------------------------------------------------------------------------


def test_a_non_ok_listing_raises_feed_read_error():
    with pytest.raises(FeedReadError, match=r"changed-file listing for acme/api#42 .*500"):
        _rebuild(_session([_file("src/app.py")], listing_status=500))


def test_a_failed_pr_metadata_read_raises_http_error():
    with pytest.raises(requests.HTTPError):
        _rebuild(_session([_file("src/app.py")], pr_status=404))
