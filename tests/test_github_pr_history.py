"""Tests for the GitHub adapter's ``get_pr_history`` (issue #16).

The adapter reads a PR's description versions, title renames, first human
review and head commit date from GitHub's GraphQL API, one POST per page. The
fake session below answers that query from per-connection page lists, keyed by
the cursors the adapter sends, so every test states its data as pages and the
paging itself is exercised rather than assumed.

Fixtures are shaped like the responses verified live in map-16 but carry
placeholder data only: acme/widgets, users alice (the author) and bob.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import requests

from prxref.forges import github
from prxref.forges.base import (
    ATTRIBUTION_MARKER,
    SUMMARY_MARKER,
    DescriptionVersion,
    FeedReadError,
    PRHistory,
    PRRef,
    TitleRename,
)
from prxref.forges.github import ForgeImpl
from prxref.forges.replay import choose_cutoff, pin_pr_metadata

CONNECTIONS = (
    ("userContentEdits", "withEdits", "editsAfter"),
    ("timelineItems", "withRenames", "renamesAfter"),
    ("reviews", "withReviews", "reviewsAfter"),
    ("comments", "withComments", "commentsAfter"),
)
CREATED = "2026-01-05T09:00:00Z"
LAST_COMMIT_DATE = "2026-01-06T18:00:00Z"
HEAD_SHA = "c" * 40
PINNED_HEAD_DATE = "2026-01-05T20:00:00Z"


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _minute(n: int) -> str:
    return (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _ref(url="https://github.com/acme/widgets/pull/7") -> PRRef:
    ref = ForgeImpl.parse_pr_url(url)
    assert ref is not None
    return ref


def _edit(text, edited_at, *, deleted_at=None):
    return {"editedAt": edited_at, "deletedAt": deleted_at, "diff": None if deleted_at else text}


def _rename(previous, current, created_at):
    return {"createdAt": created_at, "previousTitle": previous, "currentTitle": current}


def _user(login):
    return {"__typename": "User", "login": login}


def _review(login, submitted_at, *, body="", inline=(), typename="User"):
    return {
        "submittedAt": submitted_at,
        "body": body,
        "author": {"__typename": typename, "login": login},
        "comments": {"nodes": [{"body": b} for b in inline]},
    }


def _comment(login, created_at, *, body="a comment", typename="User"):
    return {"createdAt": created_at, "body": body, "author": {"__typename": typename, "login": login}}


class FakeGraphQL:
    """A ``session.post`` side effect that answers the history query page by page.

    ``pages`` maps a connection field to its list of pages, each a list of
    nodes. A page's ``endCursor`` is ``"<field>-<next index>"``, so the next
    request's cursor says which page to serve. ``filtered_count`` overrides the
    renames' ``filteredCount``, which otherwise equals the renames served.
    """

    def __init__(
        self,
        pages=None,
        *,
        author="alice",
        filtered_count=None,
        objects=None,
        last_commit_date=LAST_COMMIT_DATE,
    ):
        self.pages = pages or {}
        self.author = author
        self.filtered_count = filtered_count
        self.objects = objects or {}
        self.last_commit_date = last_commit_date
        self.requests: list[dict] = []

    def __call__(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        variables = kwargs["json"]["variables"]
        pr: dict = {"createdAt": CREATED, "author": _user(self.author)}
        for field, include_name, after_name in CONNECTIONS:
            if not variables[include_name]:
                continue
            field_pages = self.pages.get(field) or [[]]
            cursor = variables[after_name]
            index = 0 if cursor is None else int(cursor.rsplit("-", 1)[1])
            has_next = index + 1 < len(field_pages)
            connection = {
                "pageInfo": {"hasNextPage": has_next, "endCursor": f"{field}-{index + 1}" if has_next else None},
                "nodes": field_pages[index],
            }
            if field == "timelineItems":
                served = sum(len(page) for page in field_pages)
                connection["filteredCount"] = served if self.filtered_count is None else self.filtered_count
            pr[field] = connection
        if variables["byLastCommit"]:
            pr["commits"] = {"nodes": [{"commit": {"oid": "d" * 40, "committedDate": self.last_commit_date}}]}
        repository: dict = {"pullRequest": pr}
        if variables["byHead"]:
            repository["object"] = self.objects.get(variables["head"])
        return _mock_response(json_data={"data": {"repository": repository}})


def _forge(fake, monkeypatch, *, token_env="PRXREF_GITHUB_TOKEN"):
    monkeypatch.setenv(token_env, "t0ken")
    session = MagicMock(spec=requests.Session)
    session.post.side_effect = fake
    return ForgeImpl(session=session), session


# --- request shape ------------------------------------------------------------


def test_one_post_to_the_github_com_graphql_endpoint_with_the_rest_token(monkeypatch):
    fake = FakeGraphQL()
    forge, session = _forge(fake, monkeypatch)

    forge.get_pr_history(_ref())

    assert session.post.call_count == 1
    session.get.assert_not_called()
    request = fake.requests[0]
    assert request["url"] == "https://api.github.com/graphql"
    assert request["headers"]["Authorization"] == "Bearer t0ken"
    assert request["timeout"] == github._REQUEST_TIMEOUT
    query = request["json"]["query"]
    for fragment in ("userContentEdits", "RENAMED_TITLE_EVENT", "filteredCount", "reviews", "comments"):
        assert fragment in query
    variables = request["json"]["variables"]
    assert (variables["owner"], variables["repo"], variables["number"]) == ("acme", "widgets", 7)
    assert variables["pageSize"] == github._PAGE_SIZE == 100
    assert all(variables[include_name] for _, include_name, _ in CONNECTIONS)
    assert all(variables[after_name] is None for _, _, after_name in CONNECTIONS)


def test_enterprise_host_posts_to_its_own_api_graphql_endpoint(monkeypatch):
    fake = FakeGraphQL()
    forge, session = _forge(fake, monkeypatch, token_env="PRXREF_GITHUB_ENTERPRISE_TOKEN")

    forge.get_pr_history(_ref("https://github.example.com/acme/widgets/pull/7"))

    assert fake.requests[0]["url"] == "https://github.example.com/api/graphql"
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer t0ken"


# --- description versions -----------------------------------------------------


def test_no_edits_and_no_renames_is_a_complete_empty_history(monkeypatch):
    forge, _ = _forge(FakeGraphQL(), monkeypatch)

    history = forge.get_pr_history(_ref())

    assert history == PRHistory(
        created_at=_at(CREATED),
        head_committed_at=_at(LAST_COMMIT_DATE),
    )
    assert history.created_at.tzinfo is not None
    pinned = pin_pr_metadata(
        history, live_title="Add widget", live_description="The only body", cutoff=_at("2026-01-06T00:00:00Z"),
    )
    assert (pinned.description, pinned.status) == ("The only body", "pinned")


def test_several_edits_are_read_newest_first_and_pinned_by_edited_at(monkeypatch):
    nodes = [
        _edit("third", "2026-01-08T12:00:00Z"),
        _edit("second", "2026-01-06T10:00:00Z"),
        _edit("original", CREATED),
    ]
    forge, session = _forge(FakeGraphQL({"userContentEdits": [nodes]}), monkeypatch)

    history = forge.get_pr_history(_ref())

    assert session.post.call_count == 1
    assert history.complete is True
    assert [v.text for v in history.description_versions] == ["third", "second", "original"]
    assert all(v.edited_at.tzinfo is not None for v in history.description_versions)
    for cutoff, expected in (
        ("2026-01-05T08:00:00Z", "original"),
        ("2026-01-06T10:00:00Z", "second"),
        ("2026-01-07T00:00:00Z", "second"),
        ("2026-01-09T00:00:00Z", "third"),
    ):
        pinned = pin_pr_metadata(history, live_title="t", live_description="third", cutoff=_at(cutoff))
        assert pinned.description == expected, cutoff


def test_a_deleted_edit_is_held_with_no_text(monkeypatch):
    nodes = [
        _edit("live", "2026-01-08T12:00:00Z"),
        _edit(None, "2026-01-06T10:00:00Z", deleted_at="2026-01-07T00:00:00Z"),
        _edit("original", CREATED),
    ]
    forge, _ = _forge(FakeGraphQL({"userContentEdits": [nodes]}), monkeypatch)

    history = forge.get_pr_history(_ref())

    assert history.description_versions[1] == DescriptionVersion(text=None, edited_at=_at("2026-01-06T10:00:00Z"))


def test_edit_pagination_follows_the_cursor_across_two_pages(monkeypatch):
    page_one = [_edit(f"v{n}", f"2026-01-{n:02d}T12:00:00Z") for n in range(20, 10, -1)]
    page_two = [_edit(f"v{n}", f"2026-01-{n:02d}T12:00:00Z") for n in range(10, 5, -1)]
    fake = FakeGraphQL({"userContentEdits": [page_one, page_two]})
    forge, session = _forge(fake, monkeypatch)

    history = forge.get_pr_history(_ref())

    assert session.post.call_count == 2
    second = fake.requests[1]["json"]["variables"]
    assert second["editsAfter"] == "userContentEdits-1"
    assert second["withEdits"] is True
    assert (second["withRenames"], second["withReviews"], second["withComments"]) == (False, False, False)
    assert (second["byHead"], second["byLastCommit"]) == (False, False)
    assert [v.text for v in history.description_versions] == [f"v{n}" for n in range(20, 5, -1)]
    assert history.complete is True
    assert history.head_committed_at == _at(LAST_COMMIT_DATE)


def test_an_exhausted_edit_budget_returns_the_newest_versions_incomplete(monkeypatch):
    budget = github._MAX_PAGES
    pages = [[_edit(f"v{n}", _minute(n))] for n in range(budget + 5, 0, -1)]
    fake = FakeGraphQL({"userContentEdits": pages})
    forge, session = _forge(fake, monkeypatch)

    history = forge.get_pr_history(_ref())

    assert session.post.call_count == budget
    assert history.complete is False
    assert [v.text for v in history.description_versions] == [f"v{n}" for n in range(budget + 5, 5, -1)]
    newest_first = sorted(history.description_versions, key=lambda v: v.edited_at, reverse=True)
    assert newest_first[0].text == f"v{budget + 5}"
    before_held = pin_pr_metadata(history, live_title="t", live_description="live", cutoff=_at(_minute(1)))
    assert before_held.status == "live"
    within_held = pin_pr_metadata(history, live_title="t", live_description="live", cutoff=_at(_minute(budget)))
    assert (within_held.description, within_held.status) == (f"v{budget}", "pinned")


# --- title renames ------------------------------------------------------------


def test_every_title_rename_is_read_and_pins_the_title(monkeypatch):
    renames = [
        _rename("Add widget", "Add widget v2", "2026-01-06T09:00:00Z"),
        _rename("Add widget v2", "Add widget (reviewed)", "2026-01-08T09:00:00Z"),
    ]
    forge, _ = _forge(FakeGraphQL({"timelineItems": [renames]}), monkeypatch)

    history = forge.get_pr_history(_ref())

    assert history.complete is True
    assert history.title_renames == (
        TitleRename("Add widget", "Add widget v2", _at("2026-01-06T09:00:00Z")),
        TitleRename("Add widget v2", "Add widget (reviewed)", _at("2026-01-08T09:00:00Z")),
    )
    pinned = pin_pr_metadata(
        history, live_title="Add widget (reviewed)", live_description="body", cutoff=_at("2026-01-07T00:00:00Z"),
    )
    assert (pinned.title, pinned.status) == ("Add widget v2", "pinned")


def test_renames_across_two_pages_are_all_read(monkeypatch):
    page_one = [_rename("a", "b", "2026-01-06T09:00:00Z")]
    page_two = [_rename("b", "c", "2026-01-07T09:00:00Z")]
    fake = FakeGraphQL({"timelineItems": [page_one, page_two]})
    forge, session = _forge(fake, monkeypatch)

    history = forge.get_pr_history(_ref())

    assert session.post.call_count == 2
    assert fake.requests[1]["json"]["variables"]["renamesAfter"] == "timelineItems-1"
    assert [r.current_title for r in history.title_renames] == ["b", "c"]
    assert history.complete is True


def test_an_exhausted_rename_budget_pins_nothing(monkeypatch):
    budget = github._MAX_PAGES
    pages = [[_rename(f"t{n}", f"t{n + 1}", "2026-01-06T09:00:00Z")] for n in range(budget + 1)]
    edits = [[_edit("live", "2026-01-08T12:00:00Z"), _edit("original", CREATED)]]
    forge, session = _forge(FakeGraphQL({"timelineItems": pages, "userContentEdits": edits}), monkeypatch)

    history = forge.get_pr_history(_ref())

    assert session.post.call_count == budget
    assert history.complete is False
    assert history.description_versions == ()
    pinned = pin_pr_metadata(history, live_title="t", live_description="live", cutoff=_at("2026-01-06T00:00:00Z"))
    assert pinned.status == "live"


def test_fewer_renames_than_filtered_count_pins_nothing(monkeypatch):
    renames = [[_rename("a", "b", "2026-01-06T09:00:00Z")]]
    edits = [[_edit("live", "2026-01-08T12:00:00Z"), _edit("original", CREATED)]]
    fake = FakeGraphQL({"timelineItems": renames, "userContentEdits": edits}, filtered_count=2)
    forge, _ = _forge(fake, monkeypatch)

    history = forge.get_pr_history(_ref())

    assert history.complete is False
    assert history.description_versions == ()


def test_an_unreadable_rename_pins_nothing(monkeypatch):
    renames = [[{"createdAt": "2026-01-06T09:00:00Z", "previousTitle": None, "currentTitle": "b"}]]
    edits = [[_edit("live", "2026-01-08T12:00:00Z"), _edit("original", CREATED)]]
    forge, _ = _forge(FakeGraphQL({"timelineItems": renames, "userContentEdits": edits}), monkeypatch)

    history = forge.get_pr_history(_ref())

    assert history.complete is False
    assert history.description_versions == ()


# --- first human review ---------------------------------------------------------


def test_first_review_excludes_the_author_bots_pending_and_prxref_posts(monkeypatch):
    reviews = [
        _review("alice", "2026-01-05T10:00:00Z", body="self-review"),
        _review("bob", "2026-01-05T11:00:00Z", inline=[f"🔴 bug\n\n{ATTRIBUTION_MARKER} · model=m · 1 tok · 1.0s"]),
        _review("bob", "2026-01-05T11:30:00Z", body=f"{ATTRIBUTION_MARKER} · model=m"),
        _review("bob", None, body="pending draft"),
        _review("acme-ci", "2026-01-05T12:00:00Z", body="bot review", typename="Bot"),
        _review("bob", "2026-01-07T08:00:00Z", body="looks good"),
    ]
    comments = [
        _comment("alice", "2026-01-05T09:30:00Z", body="author note"),
        _comment("bob", "2026-01-05T13:00:00Z", body=f"{SUMMARY_MARKER}\nsummary"),
        _comment("acme-ci", "2026-01-05T14:00:00Z", typename="Bot"),
        _comment("bob", "2026-01-07T09:00:00Z", body="a later question"),
    ]
    fake = FakeGraphQL({"reviews": [reviews], "comments": [comments]})
    forge, _ = _forge(fake, monkeypatch)

    history = forge.get_pr_history(_ref())

    assert history.first_review_at == _at("2026-01-07T08:00:00Z")
    assert history.first_review_at.tzinfo is not None


def test_first_review_takes_an_earlier_human_comment_over_a_review(monkeypatch):
    reviews = [_review("bob", "2026-01-07T08:00:00Z", body="approve")]
    comments = [_comment("bob", "2026-01-06T15:00:00Z", body="question first")]
    forge, _ = _forge(FakeGraphQL({"reviews": [reviews], "comments": [comments]}), monkeypatch)

    history = forge.get_pr_history(_ref())

    assert history.first_review_at == _at("2026-01-06T15:00:00Z")


def test_first_review_is_none_when_only_the_author_and_prxref_spoke(monkeypatch):
    reviews = [_review("alice", "2026-01-06T08:00:00Z", body="self")]
    comments = [_comment("bob", "2026-01-06T09:00:00Z", body=f"{SUMMARY_MARKER}\nsummary")]
    forge, _ = _forge(FakeGraphQL({"reviews": [reviews], "comments": [comments]}), monkeypatch)

    history = forge.get_pr_history(_ref())

    assert history.first_review_at is None
    assert choose_cutoff(None, history) == (_at(LAST_COMMIT_DATE), "head-commit")


def test_an_exhausted_review_budget_pins_nothing_and_leaves_the_review_unknown(monkeypatch):
    budget = github._MAX_PAGES
    pages = [[_review("bob", "2026-01-07T08:00:00Z", body="ok")] for _ in range(budget + 1)]
    edits = [[_edit("live", "2026-01-08T12:00:00Z"), _edit("original", CREATED)]]
    forge, session = _forge(FakeGraphQL({"reviews": pages, "userContentEdits": edits}), monkeypatch)

    history = forge.get_pr_history(_ref())

    assert session.post.call_count == budget
    assert history.complete is False
    assert history.description_versions == ()
    assert history.first_review_at is None


# --- head commit date -----------------------------------------------------------


def test_head_sha_reads_that_commit_date(monkeypatch):
    fake = FakeGraphQL(objects={HEAD_SHA: {"committedDate": PINNED_HEAD_DATE}})
    forge, _ = _forge(fake, monkeypatch)

    history = forge.get_pr_history(_ref(), head_sha=HEAD_SHA)

    variables = fake.requests[0]["json"]["variables"]
    assert (variables["head"], variables["byHead"], variables["byLastCommit"]) == (HEAD_SHA, True, False)
    assert history.head_committed_at == _at(PINNED_HEAD_DATE)


def test_no_head_sha_reads_the_last_commit_date(monkeypatch):
    fake = FakeGraphQL()
    forge, _ = _forge(fake, monkeypatch)

    history = forge.get_pr_history(_ref())

    variables = fake.requests[0]["json"]["variables"]
    assert (variables["head"], variables["byHead"], variables["byLastCommit"]) == (None, False, True)
    assert history.head_committed_at == _at(LAST_COMMIT_DATE)


def test_an_unknown_head_sha_leaves_the_head_date_unknown(monkeypatch):
    forge, _ = _forge(FakeGraphQL(), monkeypatch)

    history = forge.get_pr_history(_ref(), head_sha=HEAD_SHA)

    assert history.head_committed_at is None


# --- failures -------------------------------------------------------------------


def test_no_token_raises_before_any_http_call(monkeypatch):
    monkeypatch.delenv("PRXREF_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PRXREF_GITHUB_ENTERPRISE_TOKEN", raising=False)
    session = MagicMock(spec=requests.Session)

    with pytest.raises(FeedReadError, match="needs a token.*PRXREF_GITHUB_TOKEN"):
        ForgeImpl(session=session).get_pr_history(_ref())

    assert session.method_calls == []


def test_no_token_on_an_enterprise_host_names_the_enterprise_variable(monkeypatch):
    monkeypatch.delenv("PRXREF_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PRXREF_GITHUB_ENTERPRISE_TOKEN", raising=False)
    session = MagicMock(spec=requests.Session)

    with pytest.raises(FeedReadError, match="PRXREF_GITHUB_ENTERPRISE_TOKEN"):
        ForgeImpl(session=session).get_pr_history(_ref("https://github.example.com/acme/widgets/pull/7"))

    assert session.method_calls == []


@pytest.mark.parametrize("status", [401, 403, 502])
def test_a_non_ok_status_raises(monkeypatch, status):
    monkeypatch.setenv("PRXREF_GITHUB_TOKEN", "t0ken")
    session = MagicMock(spec=requests.Session)
    session.post.return_value = _mock_response(status, json_data={"message": "Bad credentials"})

    with pytest.raises(FeedReadError, match=f"HTTP {status}"):
        ForgeImpl(session=session).get_pr_history(_ref())

    assert session.post.call_count == 1


def test_a_graphql_errors_array_on_a_200_raises(monkeypatch):
    monkeypatch.setenv("PRXREF_GITHUB_TOKEN", "t0ken")
    session = MagicMock(spec=requests.Session)
    session.post.return_value = _mock_response(
        200,
        json_data={
            "data": {"repository": None},
            "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}],
        },
    )

    with pytest.raises(FeedReadError, match="GraphQL errors.*NOT_FOUND"):
        ForgeImpl(session=session).get_pr_history(_ref())


def test_a_transport_error_raises_feed_read_error(monkeypatch):
    monkeypatch.setenv("PRXREF_GITHUB_TOKEN", "t0ken")
    session = MagicMock(spec=requests.Session)
    session.post.side_effect = requests.ConnectionError("connection reset")

    with pytest.raises(FeedReadError, match="could not be read") as raised:
        ForgeImpl(session=session).get_pr_history(_ref())

    assert isinstance(raised.value.__cause__, requests.ConnectionError)


def test_an_unreadable_body_raises(monkeypatch):
    monkeypatch.setenv("PRXREF_GITHUB_TOKEN", "t0ken")
    session = MagicMock(spec=requests.Session)
    session.post.return_value = _mock_response(200, text="<html>proxy</html>")

    with pytest.raises(FeedReadError, match="unreadable body"):
        ForgeImpl(session=session).get_pr_history(_ref())


def test_a_missing_pull_request_raises(monkeypatch):
    monkeypatch.setenv("PRXREF_GITHUB_TOKEN", "t0ken")
    session = MagicMock(spec=requests.Session)
    session.post.return_value = _mock_response(200, json_data={"data": {"repository": {"pullRequest": None}}})

    with pytest.raises(FeedReadError, match="no pull request"):
        ForgeImpl(session=session).get_pr_history(_ref())


def test_a_naive_timestamp_raises_rather_than_assuming_utc(monkeypatch):
    nodes = [_edit("live", "2026-01-08T12:00:00")]
    forge, _ = _forge(FakeGraphQL({"userContentEdits": [nodes]}), monkeypatch)

    with pytest.raises(FeedReadError, match="naive"):
        forge.get_pr_history(_ref())


# --- round trip -----------------------------------------------------------------


def test_round_trip_pins_the_text_in_force_at_the_first_human_review(monkeypatch):
    edits = [
        _edit("Live body, rewritten after review", "2026-01-08T12:00:00Z"),
        _edit("Second body", "2026-01-06T10:00:00Z"),
        _edit("Original body", CREATED),
    ]
    renames = [_rename("Add widget", "Add widget (reviewed)", "2026-01-07T09:00:00Z")]
    reviews = [_review("bob", "2026-01-07T08:00:00Z", body="please split this")]
    fake = FakeGraphQL({"userContentEdits": [edits], "timelineItems": [renames], "reviews": [reviews]})
    forge, _ = _forge(fake, monkeypatch)

    history = forge.get_pr_history(_ref(), head_sha=HEAD_SHA)
    cutoff = choose_cutoff(None, history)
    assert cutoff == (datetime(2026, 1, 7, 8, 0, tzinfo=UTC), "first-review")
    pinned = pin_pr_metadata(
        history,
        live_title="Add widget (reviewed)",
        live_description="Live body, rewritten after review",
        cutoff=cutoff[0],
    )

    assert (pinned.title, pinned.description, pinned.status) == ("Add widget", "Second body", "pinned")
