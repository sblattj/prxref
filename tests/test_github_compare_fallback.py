"""GitHub's read of a pull request past its diff limits (issue #15).

GitHub refuses the PR diff past 20,000 lines or 300 files. On that 406
``too_large`` refusal ``get_diff`` reads the pull request once as JSON and
takes the compare diff of its ``base.sha...head.sha`` when that diff's file
count and line sums equal the PR's ``changed_files``, ``additions`` and
``deletions``. Every other outcome of the compare read degrades, with one
WARNING naming the reason, to the changed-file listing. The listing refuses to
return a diff when its line totals differ from the PR's; an entry whose
``patch`` GitHub withheld with its counts intact is reviewed header-only with
one WARNING naming it.

Every test drives the real adapter; only the HTTP session (and, through the
CLI, the model) is a double. The recorded fixtures under ``fixtures/github/``
come from this project's own public pull request #9 (the 0.14.0 release),
read live on 2026-09-24:

- ``pr-9.json``: the PR's base, head and counts (127 files, +32003/-612),
  trimmed to those keys.
- ``files-9-trimmed.json``: its ``/pulls/9/files`` listing at ``per_page=100``
  with every entry's ``additions``/``deletions``/``changes`` as GitHub sent
  them. Each ``patch`` is cut to its first hunk header, recounted, and two body
  lines; the URL fields are dropped. Entries 84 to 99 are the 16 ordinary text
  files GitHub sent without a ``patch`` and with zero changed lines; entry 47
  is a genuinely empty file.
- ``compare-9-subset.diff``: 8 whole files, byte for byte, of the recorded
  ``compare/{base.sha}...{head.sha}`` diff. The whole recorded diff (1.67 MB)
  parses to 127 files, +32003/-612, the PR's own counts; it is too large to
  commit, so the subset is held against GitHub's own per-file numbers.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from prxref import orchestrator as real_orchestrator
from prxref.cli import main
from prxref.forges import gitlab
from prxref.forges.github import ForgeImpl
from prxref.triage import parse_unified_diff
from tests.test_forge_github import REQUEST_TIMEOUT, _mock_response, _ref, _routed_session
from tests.test_orchestrator import FakeLLM

FIXTURES = Path(__file__).parent / "fixtures" / "github"
LOGGER = "prxref.forges.github"

API = "https://api.github.com/repos/acme/api"
PR_API_URL = f"{API}/pulls/42"
FILES_URL = f"{API}/pulls/42/files"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
COMPARE_URL = f"{API}/compare/{BASE_SHA}...{HEAD_SHA}"
DIFF_ACCEPT = "application/vnd.github.v3.diff, application/vnd.diff"
COMPARE_ACCEPT = "application/vnd.github.diff"
JSON_ACCEPT = "application/vnd.github+json"
TOO_LARGE = {
    "message": "Sorry, the diff exceeded the maximum number of lines (20000)",
    "errors": [{"resource": "PullRequest", "field": "diff", "code": "too_large"}],
}
TOO_MANY_FILES_MESSAGE = (
    "Sorry, the diff exceeded the maximum number of files (300). Consider using "
    "'List pull requests files' API or locally cloning the repository instead."
)
TOO_MANY_FILES = {
    "message": TOO_MANY_FILES_MESSAGE,
    "errors": [{"resource": "PullRequest", "field": "diff", "code": "too_large"}],
}


def _debug_406(message):
    return (
        f"diff for acme/api#42 was refused by GitHub as too large (406 too_large: "
        f"{message}); reading it from the compare endpoint"
    )


DEBUG_406 = _debug_406(TOO_LARGE["message"])
DEBUG_COMPARE_USED = (
    "diff for acme/api#42 read from compare aaaaaaaaaaaa...bbbbbbbbbbbb; "
    "its file and line counts match the PR's"
)


def _warning_text(reason):
    return (
        f"GitHub PR diff for acme/api#42: the compare diff was not used ({reason}); "
        "rebuilding it from the changed-file listing"
    )


@pytest.fixture(autouse=True)
def _no_github_token(monkeypatch):
    monkeypatch.delenv("PRXREF_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PRXREF_GITHUB_ENTERPRISE_TOKEN", raising=False)


def _file(name, status="modified", patch=None, additions=0, deletions=0, **extra):
    entry = {
        "filename": name, "status": status, "additions": additions,
        "deletions": deletions, "changes": additions + deletions, **extra,
    }
    if patch is not None:
        entry["patch"] = patch
    return entry


def _pr(changed_files, additions, deletions, *, base_sha=BASE_SHA, head_sha=HEAD_SHA):
    return {
        "title": "t", "body": "", "user": {"login": "dev"},
        "head": {"ref": "feat", "sha": head_sha},
        "base": {"ref": "main", "sha": base_sha},
        "changed_files": changed_files, "additions": additions, "deletions": deletions,
    }


APP = _file("src/app.py", patch="@@ -1 +1 @@\n-old\n+new", additions=1, deletions=1)
NEW = _file("src/new.py", "added", patch="@@ -0,0 +1,2 @@\n+a\n+b", additions=2)
LISTING = [APP, NEW]
PR = _pr(2, 3, 1)
APP_SECTION = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"
)
NEW_SECTION = (
    "diff --git a/src/new.py b/src/new.py\n"
    "new file mode 100644\n"
    "index 0000000..3333333\n"
    "--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1,2 @@\n+a\n+b\n"
)
COMPARE_TEXT = APP_SECTION + NEW_SECTION
LISTING_DIFF = gitlab._render_diff_entries([
    {"old_path": "src/app.py", "new_path": "src/app.py", "new_file": False,
     "deleted_file": False, "renamed_file": False, "diff": "@@ -1 +1 @@\n-old\n+new"},
    {"old_path": "src/new.py", "new_path": "src/new.py", "new_file": True,
     "deleted_file": False, "renamed_file": False, "diff": "@@ -0,0 +1,2 @@\n+a\n+b"},
])


def _session(*, pr, listing, compare, diff_status=406, diff_text="", refusal=TOO_LARGE):
    """A Session double for the whole past-the-limit read.

    ``compare`` answers the compare GET: a ``str`` is a 200 diff, an ``int`` an
    error status, an exception instance is raised. ``refusal`` is the body of
    the PR diff's non-200 answer. Routes on URL and Accept header, never on
    call order, so the order asserted is the adapter's.
    """

    def get(url, headers=None, params=None, **kwargs):
        accept = (headers or {}).get("Accept", "")
        if "/compare/" in url:
            if isinstance(compare, BaseException):
                raise compare
            if isinstance(compare, int):
                return _mock_response(compare, json_data={"message": "Not Found"})
            return _mock_response(text=compare)
        if url.endswith("/pulls/42") and "diff" in accept:
            if diff_status == 200:
                return _mock_response(text=diff_text)
            return _mock_response(diff_status, json_data=refusal)
        if url.endswith("/pulls/42/files"):
            start = (params["page"] - 1) * params["per_page"]
            return _mock_response(json_data=listing[start:start + params["per_page"]])
        if url.endswith("/pulls/42"):
            return _mock_response(json_data=pr)
        raise AssertionError(f"unrouted GET {url}")

    session = MagicMock(spec=requests.Session)
    session.get.side_effect = get
    return session


def _calls(session):
    return [
        (c.args[0], c.kwargs["headers"]["Accept"], (c.kwargs.get("params") or {}).get("page"))
        for c in session.get.call_args_list
    ]


def _ours(caplog, level=logging.DEBUG):
    return [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.name == LOGGER and r.levelno >= level
    ]


# --- (1) a 406 reads the PR once, then the compare diff, never the listing ------


@pytest.mark.parametrize(
    ("url", "token_env", "api"),
    [
        ("https://github.com/acme/api/pull/42", None, API),
        (
            "https://git.corp.example/acme/api/pull/42",
            ("PRXREF_GITHUB_ENTERPRISE_TOKEN", "placeholder-token"),
            "https://git.corp.example/api/v3/repos/acme/api",
        ),
    ],
    ids=["github.com-anonymous", "ghes-token"],
)
def test_a_406_reads_the_pr_then_the_compare_diff_and_returns_it_as_is(
    monkeypatch, caplog, url, token_env, api
):
    if token_env is not None:
        monkeypatch.setenv(*token_env)
    session = _session(pr=PR, listing=LISTING, compare=COMPARE_TEXT)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        diff = ForgeImpl(session=session).get_diff(_ref(url))

    assert diff == COMPARE_TEXT
    assert _calls(session) == [
        (f"{api}/pulls/42", DIFF_ACCEPT, None),
        (f"{api}/pulls/42", JSON_ACCEPT, None),
        (f"{api}/compare/{BASE_SHA}...{HEAD_SHA}", COMPARE_ACCEPT, None),
    ]
    for call in session.get.call_args_list:
        assert call.kwargs["timeout"] == REQUEST_TIMEOUT
        if token_env is None:
            assert "Authorization" not in call.kwargs["headers"]
        else:
            assert call.kwargs["headers"]["Authorization"] == "Bearer placeholder-token"
    assert _ours(caplog) == [(logging.DEBUG, DEBUG_406), (logging.DEBUG, DEBUG_COMPARE_USED)]


@pytest.mark.parametrize(
    ("refusal", "message"),
    [
        (TOO_MANY_FILES, TOO_MANY_FILES_MESSAGE),
        ({"errors": [{"code": "too_large"}]}, "no message"),
        ({"message": "  \n", "errors": [{"code": "too_large"}]}, "no message"),
    ],
    ids=["300-files", "no-message", "blank-message"],
)
def test_every_too_large_406_takes_the_same_compare_path_and_logs_githubs_message(
    caplog, refusal, message
):
    """GitHub also answers 406 ``too_large`` past 300 files, under the line
    limit: a public 438-file, 18,542-line PR drew this message, verbatim."""
    session = _session(pr=PR, listing=LISTING, compare=COMPARE_TEXT, refusal=refusal)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        diff = ForgeImpl(session=session).get_diff(_ref())

    assert diff == COMPARE_TEXT
    assert _calls(session) == [
        (PR_API_URL, DIFF_ACCEPT, None),
        (PR_API_URL, JSON_ACCEPT, None),
        (COMPARE_URL, COMPARE_ACCEPT, None),
    ]
    assert _ours(caplog) == [
        (logging.DEBUG, _debug_406(message)), (logging.DEBUG, DEBUG_COMPARE_USED),
    ]


# --- (2) every compare failure degrades to the listing with one WARNING -------------


@pytest.mark.parametrize(
    ("compare", "reason"),
    [
        (404, "it answered HTTP 404"),
        (406, "it answered HTTP 406"),
        (500, "it answered HTTP 500"),
        (
            requests.ReadTimeout("read timed out"),
            "it could not be read: ReadTimeout: read timed out",
        ),
        (
            requests.ConnectionError("connection reset"),
            "it could not be read: ConnectionError: connection reset",
        ),
        (APP_SECTION, "its file count 1 differs from the PR's changed_files 2"),
        ("", "its file count 0 differs from the PR's changed_files 2"),
        (
            APP_SECTION + NEW_SECTION.replace("+1,2 @@\n+a\n+b\n", "+1 @@\n+a\n"),
            "its line counts +2/-1 differ from the PR's additions/deletions +3/-1",
        ),
    ],
    ids=[
        "http-404", "http-406", "http-500", "timeout", "connection-error",
        "file-count-mismatch", "empty-range", "line-sum-mismatch",
    ],
)
def test_a_failed_compare_read_degrades_to_the_listing_with_one_warning(
    caplog, compare, reason
):
    session = _session(pr=PR, listing=LISTING, compare=compare)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        diff = ForgeImpl(session=session).get_diff(_ref())

    assert diff == LISTING_DIFF
    assert _ours(caplog, logging.WARNING) == [(logging.WARNING, _warning_text(reason))]
    assert _calls(session) == [
        (PR_API_URL, DIFF_ACCEPT, None),
        (PR_API_URL, JSON_ACCEPT, None),
        (COMPARE_URL, COMPARE_ACCEPT, None),
        (FILES_URL, JSON_ACCEPT, 1),
    ]


def test_pr_metadata_without_base_and_head_skips_the_compare_read(caplog):
    session = _session(pr=_pr(2, 3, 1, base_sha=None, head_sha=""), listing=LISTING,
                       compare=COMPARE_TEXT)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        diff = ForgeImpl(session=session).get_diff(_ref())

    assert diff == LISTING_DIFF
    assert _ours(caplog, logging.WARNING) == [
        (logging.WARNING, _warning_text("the PR metadata carries no base.sha and head.sha"))
    ]
    assert [url for url, _, _ in _calls(session)] == [PR_API_URL, PR_API_URL, FILES_URL]


def test_pr_metadata_without_integer_totals_degrades_and_the_listing_refuses(caplog):
    pr = _pr(2, 3, 1)
    del pr["additions"]
    session = _session(pr=pr, listing=LISTING, compare=COMPARE_TEXT)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        with pytest.raises(ValueError) as info:
            ForgeImpl(session=session).get_diff(_ref())

    assert _ours(caplog, logging.WARNING) == [
        (logging.WARNING, _warning_text(
            "the PR metadata carries no integer additions to check it against"
        ))
    ]
    assert str(info.value) == (
        "GitHub PR diff for acme/api#42 cannot be checked for completeness: the PR "
        "metadata carries no integer additions and deletions; refusing to review "
        "a diff that may be partial"
    )


def test_a_failed_pr_metadata_read_propagates_before_any_other_read():
    session = _session(pr=PR, listing=LISTING, compare=COMPARE_TEXT)
    routed = session.get.side_effect

    def get(url, headers=None, params=None, **kwargs):
        if url == PR_API_URL and "diff" not in (headers or {}).get("Accept", ""):
            return _mock_response(502, json_data={"message": "Bad Gateway"})
        return routed(url, headers=headers, params=params, **kwargs)

    session.get.side_effect = get

    with pytest.raises(requests.HTTPError):
        ForgeImpl(session=session).get_diff(_ref())

    assert [url for url, _, _ in _calls(session)] == [PR_API_URL, PR_API_URL]


# --- (3) a patch withheld with its counts intact: header-only, one WARNING each ------


def _withheld_warning(name, additions, deletions):
    return (
        f"GitHub PR diff for acme/api#42: GitHub withheld the patch of {name} "
        f"(+{additions}/-{deletions}); it is reviewed as header-only"
    )


@pytest.mark.parametrize(
    "withheld",
    [
        _file("src/big.py", additions=1200, deletions=3),
        _file("package-lock.json", "removed", deletions=40),
        _file("bun.lock", "added", additions=7),
    ],
    ids=["modified", "removed", "added"],
)
def test_a_patchless_entry_with_changed_lines_is_header_only_with_one_warning(
    caplog, withheld
):
    listing = [APP, withheld]
    pr = _pr(2, 1 + withheld["additions"], 1 + withheld["deletions"])
    session = _session(pr=pr, listing=listing, compare=404)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        diff = ForgeImpl(session=session).get_diff(_ref())

    files = parse_unified_diff(diff)
    assert [(f.path, len(f.hunks)) for f in files] == [
        ("src/app.py", 1), (withheld["filename"], 0),
    ]
    assert _ours(caplog, logging.WARNING) == [
        (logging.WARNING, _warning_text("it answered HTTP 404")),
        (logging.WARNING, _withheld_warning(
            withheld["filename"], withheld["additions"], withheld["deletions"],
        )),
    ]
    assert not any("no patch and no changed lines" in msg for _, msg in _ours(caplog))


def test_each_withheld_patch_gets_its_own_warning_in_listing_order(caplog):
    """The shape seen live on a 438-file PR that drew the 300-file 406: two
    whole-file lockfile changes listed with their true counts and no patch."""
    listing = [
        APP,
        _file("bun.lock", "added", additions=2128),
        _file("pnpm-lock.yaml", "removed", deletions=8700),
    ]
    session = _session(pr=_pr(3, 2129, 8701), listing=listing, compare=404)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        diff = ForgeImpl(session=session).get_diff(_ref())

    assert [(f.path, len(f.hunks)) for f in parse_unified_diff(diff)] == [
        ("src/app.py", 1), ("bun.lock", 0), ("pnpm-lock.yaml", 0),
    ]
    assert [msg for _, msg in _ours(caplog, logging.WARNING)] == [
        _warning_text("it answered HTTP 404"),
        _withheld_warning("bun.lock", 2128, 0),
        _withheld_warning("pnpm-lock.yaml", 0, 8700),
    ]


def test_listing_totals_that_differ_from_the_pr_raise_naming_both(caplog):
    session = _session(pr=_pr(2, 3, 2), listing=LISTING, compare=404)

    with pytest.raises(ValueError) as info:
        ForgeImpl(session=session).get_diff(_ref())

    assert str(info.value) == (
        "GitHub PR diff for acme/api#42 cannot be rebuilt whole: the changed-file "
        "listing totals +3/-1 lines but the PR has +3/-2; refusing to review a "
        "partial diff"
    )


# --- (4) the recorded sblattj/prxref#9 listing -----------------------------------------


def _recorded_pr():
    recorded = json.loads((FIXTURES / "pr-9.json").read_text(encoding="utf-8"))
    return {
        "title": "release", "body": "", "user": {"login": "dev"},
        **{key: recorded[key] for key in ("base", "head", "changed_files",
                                          "additions", "deletions")},
    }


def _recorded_listing():
    return json.loads((FIXTURES / "files-9-trimmed.json").read_text(encoding="utf-8"))


def test_the_recorded_listing_has_the_shape_the_live_check_saw():
    """The fixture is the defect: 17 entries without a patch, every one listed
    with zero changed lines, so only the totals can tell them from empty files."""
    listing, pr = _recorded_listing(), _recorded_pr()

    patchless = [i for i, f in enumerate(listing) if not f.get("patch")]
    assert len(listing) == pr["changed_files"] == 127
    assert patchless == [47, *range(84, 100)]
    assert all(listing[i]["additions"] == listing[i]["deletions"] == 0 for i in patchless)
    assert listing[47]["filename"] == "tests/evals/__init__.py"
    assert sum(f["additions"] for f in listing) == 25123
    assert sum(f["deletions"] for f in listing) == 597
    assert (pr["additions"], pr["deletions"]) == (32003, 612)


@pytest.mark.parametrize(
    "compare", [404, requests.ReadTimeout("read timed out")], ids=["http-404", "timeout"]
)
def test_the_recorded_listing_fails_on_its_totals_rather_than_going_partial(compare):
    session = _session(pr=_recorded_pr(), listing=_recorded_listing(), compare=compare)

    with pytest.raises(ValueError) as info:
        ForgeImpl(session=session).get_diff(_ref())

    assert str(info.value) == (
        "GitHub PR diff for acme/api#42 cannot be rebuilt whole: the changed-file "
        "listing totals +25123/-597 lines but the PR has +32003/-612; refusing to "
        "review a partial diff"
    )
    assert [page for url, _, page in _calls(session) if url == FILES_URL] == [1, 2]


def test_control_the_recorded_listing_renders_when_the_totals_agree(caplog):
    """Only the totals stop the recorded listing: given PR totals equal to its
    own sums, the same listing renders all 127 files, 17 of them header-only."""
    pr = _recorded_pr()
    pr["additions"], pr["deletions"] = 25123, 597
    session = _session(pr=pr, listing=_recorded_listing(), compare=404)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        diff = ForgeImpl(session=session).get_diff(_ref())

    files = parse_unified_diff(diff)
    assert len(files) == 127
    assert sum(1 for f in files if not f.hunks) == 17
    assert len(_ours(caplog, logging.WARNING)) == 1


# --- (5) the recorded compare diff, at the real entry point ------------------------


SUBSET_PATHS = [
    ".gitignore",
    "docs/live-instance-verification/followup-tasks-real-forge-fixtures.md",
    "docs/systemic-sweep.md",
    "pyproject.toml",
    "src/prxref/heuristics.py",
    "tests/evals/__init__.py",
    "tests/test_retry_logging.py",
    "uv.lock",
]


def test_the_recorded_compare_diff_matches_githubs_own_counts(caplog):
    """The expected counts are GitHub's per-file numbers from the recorded
    listing, never the parser's: the cross-check holds the parser to them."""
    subset = (FIXTURES / "compare-9-subset.diff").read_text(encoding="utf-8")
    by_path = {f["filename"]: f for f in _recorded_listing()}
    additions = sum(by_path[p]["additions"] for p in SUBSET_PATHS)
    deletions = sum(by_path[p]["deletions"] for p in SUBSET_PATHS)
    assert (additions, deletions) == (23, 13)
    recorded = _recorded_pr()
    pr = _pr(len(SUBSET_PATHS), additions, deletions,
             base_sha=recorded["base"]["sha"], head_sha=recorded["head"]["sha"])
    session = _session(pr=pr, listing=_recorded_listing(), compare=subset)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        diff = ForgeImpl(session=session).get_diff(_ref())

    assert diff == subset
    assert f"/compare/{recorded['base']['sha']}...{recorded['head']['sha']}" in (
        session.get.call_args_list[2].args[0]
    )
    assert FILES_URL not in [url for url, _, _ in _calls(session)]
    assert _ours(caplog, logging.WARNING) == []
    files = parse_unified_diff(diff)
    assert sorted(f.path for f in files) == SUBSET_PATHS
    (empty,) = [f for f in files if not f.hunks]
    assert (empty.path, empty.status) == ("tests/evals/__init__.py", "added")


# --- (6) under the limit, and the header-only entries that stay --------------------


def test_under_the_limit_is_still_exactly_one_get():
    session = _session(pr=PR, listing=LISTING, compare=COMPARE_TEXT,
                       diff_status=200, diff_text=COMPARE_TEXT)

    assert ForgeImpl(session=session).get_diff(_ref()) == COMPARE_TEXT

    assert _calls(session) == [(PR_API_URL, DIFF_ACCEPT, None)]


def test_patchless_entries_with_no_changed_lines_stay_header_only_at_debug(caplog):
    listing = [
        APP,
        _file("pkg/__init__.py", "added"),
        _file("src/renamed.py", "renamed", previous_filename="src/original.py"),
        _file("assets/logo.png"),
    ]
    session = _session(pr=_pr(4, 1, 1), listing=listing, compare=404)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        diff = ForgeImpl(session=session).get_diff(_ref())

    files = parse_unified_diff(diff)
    assert [(f.path, f.status, len(f.hunks)) for f in files] == [
        ("src/app.py", "modified", 1),
        ("pkg/__init__.py", "added", 0),
        ("src/renamed.py", "renamed", 0),
        ("assets/logo.png", "modified", 0),
    ]
    assert _ours(caplog, logging.WARNING) == [
        (logging.WARNING, _warning_text("it answered HTTP 404"))
    ]
    assert [msg for level, msg in _ours(caplog) if level == logging.DEBUG][-3:] == [
        f"GitHub PR diff: {name} is listed with no patch and no changed lines; "
        "it is reviewed as header-only"
        for name in ("pkg/__init__.py", "src/renamed.py", "assets/logo.png")
    ]


# --- through the CLI and the real orchestrator ----------------------------------------


def _cli_forge(monkeypatch, *, pr, listing, compare):
    """``_routed_session`` for every other route, plus this module's routes."""
    base = _routed_session(summary_feed=[])
    routed = base.get.side_effect
    ours = _session(pr=pr, listing=listing, compare=compare).get.side_effect

    def get(url, headers=None, params=None, **kwargs):
        if "/compare/" in url or url in (PR_API_URL, FILES_URL):
            return ours(url, headers=headers, params=params, **kwargs)
        return routed(url, headers=headers, params=params, **kwargs)

    base.get.side_effect = get
    forge = ForgeImpl(session=base)
    llm = FakeLLM('{"findings": []}')
    ref = _ref()
    monkeypatch.setattr("prxref.cli.detect_forge", lambda url: ref)
    monkeypatch.setattr("prxref.cli.make_forge", lambda ref: forge)
    monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: llm)
    return ref, llm


def _review(ref):
    return main(["review", "--pr-url", ref.url, "--no-post", "--format", "json"])


class TestThroughTheCli:
    """``prxref review --no-post`` over the real orchestrator and adapter."""

    def test_the_recorded_withheld_listing_ends_as_an_error_run_that_exits_0(
        self, monkeypatch, capsys, caplog
    ):
        assert sys.modules["prxref.orchestrator"] is real_orchestrator
        ref, llm = _cli_forge(
            monkeypatch, pr=_recorded_pr(), listing=_recorded_listing(), compare=404,
        )

        with caplog.at_level(logging.ERROR, logger="prxref"):
            assert _review(ref) == 0

        out, _ = capsys.readouterr()
        assert json.loads(out)["verdict"] == "Error"
        assert llm.calls == 0
        assert (
            "get_diff failed: GitHub PR diff for acme/api#42 cannot be rebuilt whole: "
            "the changed-file listing totals +25123/-597 lines but the PR has "
            "+32003/-612"
        ) in caplog.text

    def test_control_a_matching_compare_diff_is_reviewed(self, monkeypatch, capsys):
        ref, llm = _cli_forge(monkeypatch, pr=PR, listing=LISTING, compare=COMPARE_TEXT)

        assert _review(ref) == 0

        out, _ = capsys.readouterr()
        assert json.loads(out)["verdict"] != "Error"
        assert llm.calls >= 1
