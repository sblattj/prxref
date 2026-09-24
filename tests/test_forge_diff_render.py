"""Tests for the shared diff renderer and GitHub's generalized page walker.

``render_diff_entries`` moved out of the GitLab adapter so GitHub can rebuild
a diff from its ``/pulls/{n}/files`` listing with the same header logic.
``github.ForgeImpl._iter_pages`` is the comment-feed walker generalized to
any listing, named by ``what`` in every error it raises.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests

from prxref.forges import _diff_render, github, gitlab
from prxref.forges._diff_render import render_diff_entries
from prxref.forges.base import FeedReadError
from prxref.forges.github import ForgeImpl

PAGE_SIZE = 100
URL = "https://api.github.com/repos/acme/api/pulls/42/files"
HEADERS = {"Accept": "application/vnd.github+json"}


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
    return resp


def _page(count, start=0):
    return _mock_response(json_data=[{"id": start + i} for i in range(count)])


def _ref():
    ref = ForgeImpl.parse_pr_url("https://github.com/acme/api/pull/42")
    assert ref is not None
    return ref


def _walk(session, what="PR files listing", extra_params=None):
    return list(
        ForgeImpl(session=session)._iter_pages(
            _ref(), URL, HEADERS, what=what, extra_params=extra_params
        )
    )


# --- render_diff_entries ------------------------------------------------------


def test_render_new_file():
    entry = {"old_path": "src/new.py", "new_path": "src/new.py", "new_file": True, "diff": "@@ -0,0 +1 @@\n+x\n"}
    assert render_diff_entries([entry]) == (
        "diff --git a/src/new.py b/src/new.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/new.py\n"
        "@@ -0,0 +1 @@\n+x\n"
    )


def test_render_deleted_file():
    entry = {"old_path": "src/old.py", "new_path": "src/old.py", "deleted_file": True, "diff": "@@ -1 +0,0 @@\n-y\n"}
    assert render_diff_entries([entry]) == (
        "diff --git a/src/old.py b/src/old.py\n"
        "deleted file mode 100644\n"
        "--- a/src/old.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-y\n"
    )


def test_render_renamed_file_with_hunks():
    entry = {
        "old_path": "docs/a.md",
        "new_path": "docs/b.md",
        "renamed_file": True,
        "diff": "@@ -1 +1 @@\n-old\n+new\n",
    }
    assert render_diff_entries([entry]) == (
        "diff --git a/docs/a.md b/docs/b.md\n"
        "rename from docs/a.md\n"
        "rename to docs/b.md\n"
        "--- a/docs/a.md\n"
        "+++ b/docs/b.md\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )


def test_render_modified_file():
    entry = {"old_path": "src/a.py", "new_path": "src/a.py", "diff": "@@ -1 +1 @@\n-a\n+b\n"}
    assert render_diff_entries([entry]) == (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"
    )


@pytest.mark.parametrize(
    "entry",
    [
        {"old_path": "img/logo.png", "new_path": "img/logo.png"},
        {"old_path": "img/logo.png", "new_path": "img/logo.png", "diff": None},
        {"old_path": "img/logo.png", "new_path": "img/logo.png", "diff": ""},
    ],
    ids=["missing", "none", "empty"],
)
def test_render_entry_without_a_diff_is_header_only(entry):
    assert render_diff_entries([entry]) == (
        "diff --git a/img/logo.png b/img/logo.png\n"
        "--- a/img/logo.png\n"
        "+++ b/img/logo.png\n"
    )


def test_render_new_file_without_a_diff_keeps_its_new_file_header():
    entry = {"old_path": "bin/tool", "new_path": "bin/tool", "new_file": True}
    assert render_diff_entries([entry]) == (
        "diff --git a/bin/tool b/bin/tool\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/bin/tool\n"
    )


def test_render_normalizes_the_newlines_around_a_hunk_body():
    unterminated = {"old_path": "a", "new_path": "a", "diff": "@@ -1 +1 @@\n-a\n+b"}
    led_by_newline = {"old_path": "b", "new_path": "b", "diff": "\n@@ -1 +1 @@\n-a\n+b\n"}
    assert render_diff_entries([unterminated, led_by_newline]) == (
        "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/b b/b\n--- a/b\n+++ b/b\n@@ -1 +1 @@\n-a\n+b\n"
    )


def test_render_concatenates_entries_in_order_and_empty_renders_empty():
    first = {"old_path": "a", "new_path": "a", "diff": "@@ -1 +1 @@\n-a\n+b\n"}
    second = {"old_path": "b", "new_path": "b"}
    assert render_diff_entries([first, second]) == (
        render_diff_entries([first]) + render_diff_entries([second])
    )
    assert render_diff_entries([]) == ""


def test_gitlab_alias_is_the_shared_renderer():
    assert gitlab._render_diff_entries is render_diff_entries
    assert gitlab._render_diff_entries is _diff_render.render_diff_entries


# --- github._iter_pages -------------------------------------------------------


def test_iter_pages_reads_to_a_short_last_page():
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = [_page(PAGE_SIZE), _page(30, start=PAGE_SIZE)]

    pages = _walk(session)

    assert [len(p) for p in pages] == [PAGE_SIZE, 30]
    assert [item["id"] for p in pages for item in p] == list(range(PAGE_SIZE + 30))
    assert session.get.call_count == 2
    assert [c.kwargs["params"]["page"] for c in session.get.call_args_list] == [1, 2]
    assert all(c.args[0] == URL for c in session.get.call_args_list)


def test_iter_pages_stops_after_one_short_page():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _page(0)

    assert _walk(session) == [[]]
    assert session.get.call_count == 1


def test_iter_pages_sends_the_page_size_the_headers_and_extra_params():
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = [_page(PAGE_SIZE), _page(1)]

    _walk(session, extra_params={"state": "all"})

    sent = [c.kwargs["params"] for c in session.get.call_args_list]
    assert sent == [
        {"per_page": PAGE_SIZE, "page": 1, "state": "all"},
        {"per_page": PAGE_SIZE, "page": 2, "state": "all"},
    ]
    assert github._PAGE_SIZE == PAGE_SIZE
    assert all(c.kwargs["headers"] == HEADERS for c in session.get.call_args_list)
    assert all(c.kwargs["timeout"] == github._REQUEST_TIMEOUT for c in session.get.call_args_list)


def test_iter_pages_without_extra_params_sends_only_paging():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _page(3)

    _walk(session)

    assert session.get.call_args.kwargs["params"] == {"per_page": PAGE_SIZE, "page": 1}


def test_iter_pages_skips_non_dict_items_but_counts_them_toward_a_full_page():
    session = MagicMock(spec=requests.Session)
    full = [{"id": i} for i in range(PAGE_SIZE - 1)] + ["not a dict"]
    session.get.side_effect = [_mock_response(json_data=full), _page(0)]

    pages = _walk(session)

    assert [len(p) for p in pages] == [PAGE_SIZE - 1, 0]
    assert session.get.call_count == 2


def test_iter_pages_raises_naming_what_on_a_non_ok_page():
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = [_page(PAGE_SIZE), _mock_response(500, json_data={"message": "boom"})]

    with pytest.raises(FeedReadError) as excinfo:
        _walk(session, what="PR files listing")

    message = str(excinfo.value)
    assert message == "PR files listing for acme/api#42 returned HTTP 500 at page 2"
    assert "comment feed" not in message


def test_iter_pages_raises_naming_what_on_a_transport_failure():
    session = MagicMock(spec=requests.Session)
    session.get.side_effect = requests.ConnectionError("down")

    with pytest.raises(FeedReadError, match=r"^PR files listing for acme/api#42 could not be read at page 1: down$"):
        _walk(session, what="PR files listing")


def test_iter_pages_raises_naming_what_on_an_unreadable_body():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(text="<html>")

    with pytest.raises(FeedReadError, match=r"^PR files listing for acme/api#42 returned an unreadable body at page 1"):
        _walk(session, what="PR files listing")


def test_iter_pages_raises_naming_what_on_a_body_that_is_not_a_list():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(json_data={"message": "Not Found"})

    with pytest.raises(FeedReadError, match=r"^PR files listing for acme/api#42 returned dict, not a list$"):
        _walk(session, what="PR files listing")


def test_iter_pages_raises_when_the_page_budget_runs_out():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _page(PAGE_SIZE)

    with pytest.raises(FeedReadError, match="page budget") as excinfo:
        _walk(session, what="PR files listing")

    assert session.get.call_count == github._MAX_PAGES == 50
    assert str(excinfo.value) == (
        "PR files listing for acme/api#42 outran the 50-page budget "
        "(5000 entries) without reaching the end"
    )


def test_iter_pages_keeps_the_comment_feed_wording_for_comment_callers():
    session = MagicMock(spec=requests.Session)
    session.get.return_value = _mock_response(403, json_data={"message": "rate limited"})

    with pytest.raises(FeedReadError) as excinfo:
        ForgeImpl(session=session).post_summary(_ref(), "body")

    assert str(excinfo.value) == "comment feed for acme/api#42 returned HTTP 403 at page 1"
    session.post.assert_not_called()


def test_iter_pages_is_what_every_comment_reader_walks():
    for method in ("post_summary", "list_threads", "prune_inline_comments"):
        session = MagicMock(spec=requests.Session)
        session.get.side_effect = requests.ConnectionError("down")
        forge = ForgeImpl(session=session)
        seen: list[str] = []
        real = forge._iter_pages

        def spy(*args, _real=real, _seen=seen, **kwargs):
            _seen.append(kwargs["what"])
            return _real(*args, **kwargs)

        forge._iter_pages = spy
        args = (_ref(), "body") if method == "post_summary" else (_ref(),)
        try:
            getattr(forge, method)(*args)
        except FeedReadError:
            pass
        assert seen == ["comment feed"], method
