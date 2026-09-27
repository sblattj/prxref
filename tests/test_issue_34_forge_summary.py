"""Forge reads and scoped prunes for incremental re-review (#34).

``get_summary`` must return the body of exactly the comment ``post_summary``
would overwrite, through the same lookup, and must never write.
``prune_inline_comments(paths=...)`` must delete only prxref's own comments
on the named files, and keep any comment whose file cannot be told.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import requests

from prxref.forges import azure_devops, bitbucket, bitbucket_server, github, gitlab
from prxref.forges.base import ATTRIBUTION_MARKER, SUMMARY_MARKER, FeedReadError

WRITE_VERBS = ("post", "put", "patch", "delete")
OURS = f"finding\n\n---\n*{ATTRIBUTION_MARKER} - model=m*"
HUMAN = "a human's comment, never a candidate"

_ENV = (
    "PRXREF_GITHUB_TOKEN",
    "PRXREF_GITHUB_ENTERPRISE_TOKEN",
    "PRXREF_GITLAB_TOKEN",
    "PRXREF_BITBUCKET_TOKEN",
    "PRXREF_BITBUCKET_USER",
    "PRXREF_BITBUCKET_APP_PASSWORD",
    "PRXREF_BITBUCKET_SERVER_TOKEN",
    "PRXREF_BITBUCKET_SERVER_USER",
    "PRXREF_BITBUCKET_SERVER_PASSWORD",
    "PRXREF_AZURE_DEVOPS_TOKEN",
    "SYSTEM_ACCESSTOKEN",
)


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def _resp(status_code=200, json_data=None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.headers = {"Content-Type": "application/json"}
    if json_data is not None:
        resp.json.return_value = json_data
        resp.text = json.dumps(json_data)
    else:
        resp.text = ""
        resp.json.side_effect = ValueError("No JSON")
    resp.content = resp.text.encode("utf-8")
    resp.raise_for_status.side_effect = None if resp.ok else requests.HTTPError(response=resp)
    return resp


def _session(get_response):
    session = MagicMock(spec=requests.Session)
    session.get.return_value = get_response
    for verb in WRITE_VERBS:
        getattr(session, verb).return_value = _resp(200, json_data={"id": 1})
    return session


def _assert_no_writes(session):
    for verb in WRITE_VERBS:
        getattr(session, verb).assert_not_called()


def _last_segment(url):
    return url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


class _GitHub:
    """Issue comments hold the summary; review comments carry a ``path``."""

    name = "github"
    kinds = ("top",)
    field = "body"

    @staticmethod
    def forge(session):
        return github.ForgeImpl(session=session)

    @staticmethod
    def ref():
        return github.ForgeImpl.parse_pr_url("https://github.com/acme/api/pull/42")

    @staticmethod
    def summary_feed(items):
        return _resp(json_data=[{"id": i, "body": body} for i, body, _ in items])

    @staticmethod
    def updated_id(session):
        if not session.patch.called:
            return None
        return int(_last_segment(session.patch.call_args[0][0]))

    @staticmethod
    def prune_feed(entries):
        comments = []
        for i, path, body in entries:
            comment = {"id": i, "body": body, "line": 1}
            if path is not None:
                comment["path"] = path
            comments.append(comment)
        return _resp(json_data=comments)


class _GitLab:
    """Notes hold the summary; a diff note's position names new and old paths."""

    name = "gitlab"
    kinds = ("top",)
    field = "body"

    @staticmethod
    def forge(session):
        return gitlab.ForgeImpl(session=session)

    @staticmethod
    def ref():
        return gitlab.ForgeImpl.parse_pr_url("https://gitlab.com/acme/api/-/merge_requests/5")

    @staticmethod
    def summary_feed(items):
        return _resp(json_data=[{"id": i, "body": body} for i, body, _ in items])

    @staticmethod
    def updated_id(session):
        if not session.put.called:
            return None
        return int(_last_segment(session.put.call_args[0][0]))

    @staticmethod
    def prune_feed(entries):
        discussions = []
        for i, path, body in entries:
            position = {"position_type": "text", "new_line": 1}
            if path is not None:
                position.update(new_path=path, old_path=path)
            discussions.append({"id": f"d{i}", "notes": [{"id": i, "body": body, "position": position}]})
        return _resp(json_data=discussions)


class _BitbucketCloud:
    """One comment feed; the lookup skips ``inline`` and deleted comments."""

    name = "bitbucket"
    kinds = ("top", "inline", "deleted")
    field = "content.raw"

    @staticmethod
    def forge(session):
        return bitbucket.ForgeImpl(session=session)

    @staticmethod
    def ref():
        return bitbucket.ForgeImpl.parse_pr_url("https://bitbucket.org/acme/api/pull-requests/3")

    @staticmethod
    def summary_feed(items):
        values = []
        for i, body, kind in items:
            comment = {"id": i, "content": {"raw": body}}
            if kind == "inline":
                comment["inline"] = {"path": "src/a.py", "to": 1}
            if kind == "deleted":
                comment["deleted"] = True
            values.append(comment)
        return _resp(json_data={"values": values})

    @staticmethod
    def updated_id(session):
        if not session.put.called:
            return None
        return int(_last_segment(session.put.call_args[0][0]))

    @staticmethod
    def prune_feed(entries):
        values = []
        for i, path, body in entries:
            inline = {"to": 1}
            if path is not None:
                inline["path"] = path
            values.append({"id": i, "inline": inline, "content": {"raw": body}})
        return _resp(json_data={"values": values})


class _BitbucketServer:
    """COMMENTED activities; the lookup skips anchored comments."""

    name = "bitbucket_server"
    kinds = ("top", "inline")
    field = "text"

    @staticmethod
    def forge(session):
        return bitbucket_server.ForgeImpl(session=session)

    @staticmethod
    def ref():
        return bitbucket_server.ForgeImpl.parse_pr_url(
            "https://git.example.com/projects/ACME/repos/api/pull-requests/7"
        )

    @staticmethod
    def summary_feed(items):
        values = []
        for i, body, kind in items:
            comment = {"id": i, "version": 3, "text": body}
            if kind == "inline":
                comment["anchor"] = {"path": "src/a.py", "line": 1}
            values.append({"action": "COMMENTED", "comment": comment})
        return _resp(json_data={"values": values, "isLastPage": True})

    @staticmethod
    def updated_id(session):
        if not session.put.called:
            return None
        return int(_last_segment(session.put.call_args[0][0]))

    @staticmethod
    def prune_feed(entries):
        values = []
        for i, path, body in entries:
            anchor = {"line": 1, "lineType": "ADDED"}
            if path is not None:
                anchor["path"] = path
            comment = {"id": i, "version": 1, "text": body, "anchor": anchor}
            values.append({"action": "COMMENTED", "comment": comment})
        return _resp(json_data={"values": values, "isLastPage": True})


class _Azure:
    """Threads; the summary is a live PR-level thread's root comment."""

    name = "azure_devops"
    kinds = ("top", "inline", "deleted")
    field = "content"

    @staticmethod
    def forge(session):
        return azure_devops.ForgeImpl(session=session)

    @staticmethod
    def ref():
        return azure_devops.ForgeImpl.parse_pr_url(
            "https://dev.azure.com/acme/_git/AcmeWeb/pullrequest/551"
        )

    @staticmethod
    def summary_feed(items):
        threads = []
        for i, body, kind in items:
            thread = {"id": 100 + i, "comments": [{"id": i, "content": body, "commentType": 1}]}
            if kind == "inline":
                thread["threadContext"] = {"filePath": "/src/a.py"}
            if kind == "deleted":
                thread["isDeleted"] = True
            threads.append(thread)
        return _resp(json_data={"value": threads})

    @staticmethod
    def updated_id(session):
        if not session.patch.called:
            return None
        return int(_last_segment(session.patch.call_args[0][0]))

    @staticmethod
    def prune_feed(entries):
        threads = []
        for i, path, body in entries:
            context = {"rightFileStart": {"line": 1, "offset": 1}}
            if path is not None:
                context["filePath"] = "/" + path
            threads.append({
                "id": 100 + i,
                "threadContext": context,
                "comments": [{"id": i, "content": body, "commentType": 1}],
            })
        return _resp(json_data={"value": threads})


ADAPTERS = [_GitHub, _GitLab, _BitbucketCloud, _BitbucketServer, _Azure]
IDS = [a.name for a in ADAPTERS]


def _deleted_ids(session):
    return sorted(int(_last_segment(c.args[0])) for c in session.delete.call_args_list)


# --- get_summary --------------------------------------------------------------


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_get_summary_returns_the_marked_body(adapter):
    stored = f"{SUMMARY_MARKER}\nold summary"
    session = _session(adapter.summary_feed([(1, HUMAN, "top"), (7, stored, "top")]))

    assert adapter.forge(session).get_summary(adapter.ref()) == stored
    _assert_no_writes(session)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_get_summary_returns_none_without_a_summary(adapter):
    session = _session(adapter.summary_feed([(1, HUMAN, "top")]))

    assert adapter.forge(session).get_summary(adapter.ref()) is None
    _assert_no_writes(session)


@pytest.mark.parametrize(
    ("adapter", "kind"),
    [(a, k) for a in ADAPTERS for k in a.kinds if k != "top"],
    ids=[f"{a.name}-{k}" for a in ADAPTERS for k in a.kinds if k != "top"],
)
def test_get_summary_ignores_a_marked_comment_the_lookup_skips(adapter, kind):
    session = _session(adapter.summary_feed([(3, f"{SUMMARY_MARKER}\nnot the summary", kind)]))

    assert adapter.forge(session).get_summary(adapter.ref()) is None
    _assert_no_writes(session)


@pytest.mark.parametrize(
    ("adapter", "kind"),
    [(a, k) for a in ADAPTERS for k in a.kinds if k != "top"],
    ids=[f"{a.name}-{k}" for a in ADAPTERS for k in a.kinds if k != "top"],
)
def test_get_summary_skips_to_the_real_summary_past_a_skipped_one(adapter, kind):
    real = f"{SUMMARY_MARKER}\nreal"
    session = _session(adapter.summary_feed([
        (3, f"{SUMMARY_MARKER}\nnot the summary", kind),
        (4, real, "top"),
    ]))

    assert adapter.forge(session).get_summary(adapter.ref()) == real
    _assert_no_writes(session)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_get_summary_raises_on_an_unreadable_feed(adapter):
    session = _session(_resp(500, json_data={"message": "boom"}))

    with pytest.raises(FeedReadError):
        adapter.forge(session).get_summary(adapter.ref())
    _assert_no_writes(session)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_post_summary_also_raises_on_that_unreadable_feed(adapter):
    session = _session(_resp(500, json_data={"message": "boom"}))

    with pytest.raises(FeedReadError):
        adapter.forge(session).post_summary(adapter.ref(), "body")
    _assert_no_writes(session)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_get_summary_picks_the_comment_post_summary_updates(adapter):
    items = [
        (5, f"{SUMMARY_MARKER}\nfirst", "top"),
        (6, f"{SUMMARY_MARKER}\nsecond", "top"),
    ]
    writer = _session(adapter.summary_feed(items))
    adapter.forge(writer).post_summary(adapter.ref(), "new")
    updated = adapter.updated_id(writer)
    assert updated is not None

    reader = _session(adapter.summary_feed(items))
    body = adapter.forge(reader).get_summary(adapter.ref())

    assert body == dict((i, b) for i, b, _ in items)[updated]
    _assert_no_writes(reader)


def test_get_summary_returns_the_bitbucket_cloud_raw_markdown_field():
    session = _session(_resp(json_data={"values": [
        {"id": 2, "content": {"raw": f"{SUMMARY_MARKER}\n**raw**", "html": "<p>html</p>"}},
    ]}))

    assert bitbucket.ForgeImpl(session=session).get_summary(_BitbucketCloud.ref()) == (
        f"{SUMMARY_MARKER}\n**raw**"
    )


def test_get_summary_ignores_an_azure_thread_whose_root_is_deleted():
    session = _session(_resp(json_data={"value": [
        {"id": 101, "comments": [{"id": 1, "content": f"{SUMMARY_MARKER}\nx", "isDeleted": True}]},
    ]}))

    assert azure_devops.ForgeImpl(session=session).get_summary(_Azure.ref()) is None


def test_get_summary_ignores_an_azure_system_thread():
    session = _session(_resp(json_data={"value": [
        {"id": 101, "comments": [{"id": 1, "content": f"{SUMMARY_MARKER}\nx", "commentType": "system"}]},
    ]}))

    assert azure_devops.ForgeImpl(session=session).get_summary(_Azure.ref()) is None


def test_get_summary_is_not_offered_by_the_replay_forges():
    from prxref.forges import replay

    offered = [
        name for name, cls in vars(replay).items()
        if isinstance(cls, type) and hasattr(cls, "prune_inline_comments")
    ]
    assert offered
    for name in offered:
        assert not hasattr(getattr(replay, name), "get_summary"), name


# --- prune_inline_comments(paths=...) -----------------------------------------


PRUNE_ENTRIES = [
    (11, "src/a.py", OURS),
    (12, "src/a.py", HUMAN),
    (13, "src/b.py", OURS),
    (14, None, OURS),
]


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_prune_with_paths_deletes_only_prxref_comments_on_those_paths(adapter):
    session = _session(adapter.prune_feed(PRUNE_ENTRIES))
    session.delete.return_value = _resp(204)

    removed = adapter.forge(session).prune_inline_comments(adapter.ref(), paths={"src/a.py"})

    assert removed == 1
    assert _deleted_ids(session) == [11]


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_prune_with_an_empty_paths_collection_deletes_nothing(adapter):
    session = _session(adapter.prune_feed(PRUNE_ENTRIES))

    assert adapter.forge(session).prune_inline_comments(adapter.ref(), paths=()) == 0
    session.delete.assert_not_called()


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_prune_without_paths_deletes_every_prxref_comment_as_before(adapter):
    session = _session(adapter.prune_feed(PRUNE_ENTRIES))
    session.delete.return_value = _resp(204)

    removed = adapter.forge(session).prune_inline_comments(adapter.ref())

    assert removed == 3
    assert _deleted_ids(session) == [11, 13, 14]


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_prune_with_paths_none_matches_the_default(adapter):
    session = _session(adapter.prune_feed(PRUNE_ENTRIES))
    session.delete.return_value = _resp(204)

    removed = adapter.forge(session).prune_inline_comments(adapter.ref(), paths=None)

    assert removed == 3
    assert _deleted_ids(session) == [11, 13, 14]


@pytest.mark.parametrize("adapter", ADAPTERS, ids=IDS)
def test_prune_accepts_any_collection_of_paths(adapter):
    session = _session(adapter.prune_feed(PRUNE_ENTRIES))
    session.delete.return_value = _resp(204)

    removed = adapter.forge(session).prune_inline_comments(
        adapter.ref(), paths=["src/b.py", "src/other.py"],
    )

    assert removed == 1
    assert _deleted_ids(session) == [13]


@pytest.mark.parametrize("old_or_new", ["old.py", "new.py"])
def test_gitlab_prune_matches_a_renamed_file_under_either_name(old_or_new):
    session = _session(_resp(json_data=[{
        "id": "d1",
        "notes": [{
            "id": 21, "body": OURS,
            "position": {"new_path": "new.py", "old_path": "old.py", "new_line": 1},
        }],
    }]))
    session.delete.return_value = _resp(204)

    removed = gitlab.ForgeImpl(session=session).prune_inline_comments(
        _GitLab.ref(), paths={old_or_new},
    )

    assert removed == 1
    assert _deleted_ids(session) == [21]


def test_azure_prune_strips_the_leading_slash_like_list_threads():
    feed = _Azure.prune_feed([(31, "src/deep/a.py", OURS)])
    threads = azure_devops.ForgeImpl(session=_session(feed)).list_threads(_Azure.ref())
    assert threads[0].path == "src/deep/a.py"

    session = _session(_Azure.prune_feed([(31, "src/deep/a.py", OURS)]))
    session.delete.return_value = _resp(204)

    removed = azure_devops.ForgeImpl(session=session).prune_inline_comments(
        _Azure.ref(), paths={threads[0].path},
    )

    assert removed == 1
    assert _deleted_ids(session) == [31]
