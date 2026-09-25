"""Bitbucket Cloud ``get_pr_history``: description versions and cutoff inputs from ``/activity`` (#16).

Fixture provenance, stated plainly. The ``/activity`` entries follow the shape
map-16 records as verified anonymously on a public pull request:
``update.changes.description.{old,new}`` carries the full texts, dated
``update.date``, and the newest ``new`` equals the live description.
Everything else here is built by analogy and is not a capture:
``changes.title``, the ``update.title``/``update.description`` snapshots, the
``approval``/``changes_requested``/``comment`` entries, the ``next`` link
spelling and ``/commit/{sha}``'s ``date``. Identities are placeholders.

The fake session answers only the four URLs the adapter documents and hands
anything else a 404, so a request to the wrong endpoint fails the test instead
of being answered.
"""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
import requests

from prxref.forges import bitbucket
from prxref.forges.base import ATTRIBUTION_MARKER, SUMMARY_MARKER, DescriptionVersion, PRHistory, TitleRename
from prxref.forges.bitbucket import ForgeImpl
from prxref.forges.replay import choose_cutoff, pin_pr_metadata

REPO_API = "https://api.bitbucket.org/2.0/repositories/acme/widgets"
PR_API = f"{REPO_API}/pullrequests/7"
ACTIVITY = f"{PR_API}/activity"
PR_URL = "https://bitbucket.org/acme/widgets/pull-requests/7"
PR_HEAD = "c" * 12
HEAD_SHA = "b" * 40

TITLE = "Add widget cache"
ORIGINAL = "Adds a cache in front of the widget store."
CREATED = "2026-01-05T09:00:00.000000+00:00"
COMMIT_DATE = "2026-01-05T08:30:00+00:00"

ALICE = {"type": "user", "uuid": "{a1a1a1a1-0000-4000-8000-000000000001}", "nickname": "alice",
         "display_name": "Alice Example"}
BOB = {"type": "user", "uuid": "{b0b0b0b0-0000-4000-8000-000000000002}", "nickname": "bob",
       "display_name": "Bob Example"}
CAROL = {"type": "user", "uuid": "{ca401ca4-0000-4000-8000-000000000003}", "nickname": "carol",
         "display_name": "Carol Example"}
REVIEW_BOT = {"type": "app_user", "uuid": "{b07b07b0-0000-4000-8000-000000000004}", "nickname": "acme-bot",
              "display_name": "Acme Bot"}
PR_STUB = {"type": "pullrequest", "id": 7, "title": TITLE}


def _at(day: int, hour: int = 12) -> str:
    return f"2026-01-{day:02d}T{hour:02d}:00:00.000000+00:00"


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _mock_response(status_code=200, json_data=None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.headers = {}
    resp.json.return_value = json_data
    resp.text = json.dumps(json_data)
    resp.raise_for_status.side_effect = None if resp.ok else requests.HTTPError(
        f"{status_code} Client Error", response=resp,
    )
    return resp


def _pr(*, title=TITLE, description=ORIGINAL, author=ALICE, head=PR_HEAD):
    return {
        "type": "pullrequest",
        "id": 7,
        "title": title,
        "description": description,
        "summary": {"raw": description, "markup": "markdown"},
        "state": "OPEN",
        "created_on": CREATED,
        "updated_on": _at(20),
        "author": author,
        "source": {"branch": {"name": "feature/cache"}, "commit": {"hash": head}},
        "destination": {"branch": {"name": "main"}, "commit": {"hash": "d" * 12}},
    }


def _update(date, *, title=TITLE, description=ORIGINAL, changes=None, author=ALICE):
    update = {
        "state": "OPEN",
        "author": author,
        "date": date,
        "title": title,
        "description": description,
        "reason": "",
    }
    if changes is not None:
        update["changes"] = changes
    return {"pull_request": PR_STUB, "update": update}


def _edit(date, old, new, *, title=TITLE):
    return _update(date, title=title, description=new, changes={"description": {"old": old, "new": new}})


def _rename(date, old, new, *, description=ORIGINAL):
    return _update(date, title=new, description=description, changes={"title": {"old": old, "new": new}})


def _approval(date, user):
    return {"pull_request": PR_STUB, "approval": {"date": date, "user": user, "pullrequest": PR_STUB}}


def _changes_requested(date, user):
    return {"pull_request": PR_STUB, "changes_requested": {"date": date, "user": user, "pullrequest": PR_STUB}}


_COMMENT_IDS = itertools.count(1000)


def _comment(date, user, raw, **extra):
    comment = {
        "id": next(_COMMENT_IDS),
        "type": "pullrequest_comment",
        "created_on": date,
        "updated_on": date,
        "user": user,
        "content": {"raw": raw, "markup": "markdown", "html": f"<p>{raw}</p>"},
        "deleted": False,
    }
    comment.update(extra)
    return {"pull_request": PR_STUB, "comment": comment}


def _next(index: int) -> str:
    return f"{ACTIVITY}?ctx=page-{index}"


class _FakeBitbucket:
    """A session that serves one PR, its activity pages, and any commit, by URL."""

    def __init__(self, *, pr=None, pages=((),), commit_date=COMMIT_DATE, statuses=None, endless=False):
        self.pr = _pr() if pr is None else pr
        self.pages = [list(page) for page in pages]
        self.commit_date = commit_date
        self.statuses = statuses or {}
        self.endless = endless
        self.session = MagicMock(spec=requests.Session)
        self.session.get.side_effect = self._get

    def _page(self, index):
        entries = self.pages[min(index, len(self.pages) - 1)] if self.endless else self.pages[index]
        body = {"values": entries, "pagelen": 50}
        if self.endless or index + 1 < len(self.pages):
            body["next"] = _next(index + 1)
        return _mock_response(json_data=body)

    def _get(self, url, params=None, **kwargs):
        for prefix, status in self.statuses.items():
            if url.startswith(prefix):
                if isinstance(status, Exception):
                    raise status
                return _mock_response(status, json_data={"type": "error", "error": {"message": "nope"}})
        if url == PR_API:
            return _mock_response(json_data=self.pr)
        if url == ACTIVITY:
            return self._page(0)
        if url.startswith(f"{ACTIVITY}?ctx=page-"):
            return self._page(int(url.rsplit("-", 1)[1]))
        if url.startswith(f"{REPO_API}/commit/"):
            return _mock_response(json_data={"type": "commit", "hash": url.rsplit("/", 1)[1], "date": self.commit_date})
        return _mock_response(404, json_data={"type": "error"})

    def urls(self):
        return [call.args[0] for call in self.session.get.call_args_list]

    def history(self, **kwargs) -> PRHistory:
        return ForgeImpl(session=self.session).get_pr_history(ForgeImpl.parse_pr_url(PR_URL), **kwargs)


def _by_time(history: PRHistory) -> list[tuple[str | None, datetime]]:
    return [(v.text, v.edited_at) for v in sorted(history.description_versions, key=lambda v: v.edited_at)]


def _assert_all_aware(history: PRHistory) -> None:
    stamps = [history.created_at, history.first_review_at, history.head_committed_at]
    stamps += [v.edited_at for v in history.description_versions]
    stamps += [r.created_at for r in history.title_renames]
    for stamp in stamps:
        if stamp is not None:
            assert stamp.utcoffset() is not None


# --- no edits -----------------------------------------------------------------


def test_a_pr_never_edited_is_complete_with_no_versions_and_pins_the_live_text():
    fake = _FakeBitbucket(pages=[[_approval(_at(7), BOB), _update(CREATED)]])

    history = fake.history()

    assert history == PRHistory(
        created_at=_dt(CREATED),
        first_review_at=_dt(_at(7)),
        head_committed_at=_dt(COMMIT_DATE),
    )
    assert history.complete is True
    pinned = pin_pr_metadata(history, live_title=TITLE, live_description=ORIGINAL, cutoff=_dt(_at(6)))
    assert (pinned.title, pinned.description, pinned.status) == (TITLE, ORIGINAL, "pinned")


def test_an_empty_activity_feed_is_a_complete_history():
    history = _FakeBitbucket(pages=[[]]).history()

    assert history.complete is True
    assert history.description_versions == ()
    assert history.title_renames == ()
    assert history.first_review_at is None


def test_the_reads_use_the_adapters_credentials_and_timeout(monkeypatch):
    monkeypatch.setenv("PRXREF_BITBUCKET_TOKEN", "t0ken")
    fake = _FakeBitbucket(pages=[[_update(CREATED)]])

    fake.history()

    calls = fake.session.get.call_args_list
    assert [call.args[0] for call in calls] == [PR_API, ACTIVITY, f"{REPO_API}/commit/{PR_HEAD}"]
    for call in calls:
        assert call.kwargs["headers"] == {"Authorization": "Bearer t0ken"}
        assert call.kwargs["auth"] is None
        assert call.kwargs["timeout"] == bitbucket._REQUEST_TIMEOUT
    fake.session.post.assert_not_called()
    fake.session.put.assert_not_called()
    fake.session.delete.assert_not_called()


# --- description edits --------------------------------------------------------


V1 = "Adds a cache in front of the widget store. Invalidation is TTL based."
V2 = "Adds a cache in front of the widget store. Invalidation is event based."
V3 = "Adds a cache. Reviewer feedback resolution: switched to event invalidation, added the eviction test."


def _three_edits_shuffled():
    return [
        _edit(_at(8), V1, V2),
        _comment(_at(7), BOB, "TTL invalidation will serve stale widgets."),
        _edit(_at(6), ORIGINAL, V1),
        _edit(_at(10), V2, V3),
        _update(CREATED),
    ]


def test_several_edits_out_of_order_become_versions_dated_by_update_date():
    fake = _FakeBitbucket(pr=_pr(description=V3), pages=[_three_edits_shuffled()])

    history = fake.history()

    assert history.complete is True
    assert _by_time(history) == [
        (ORIGINAL, _dt(CREATED)),
        (V1, _dt(_at(6))),
        (V2, _dt(_at(8))),
        (V3, _dt(_at(10))),
    ]
    assert all(isinstance(v, DescriptionVersion) for v in history.description_versions)
    _assert_all_aware(history)


@pytest.mark.parametrize(
    ("cutoff", "expected"),
    [
        (_at(5, 10), ORIGINAL),
        (_at(6), V1),
        (_at(7), V1),
        (_at(9), V2),
        (_at(11), V3),
        ("2026-01-01T00:00:00+00:00", ORIGINAL),
    ],
)
def test_the_version_in_force_at_a_cutoff_is_the_last_edit_at_or_before_it(cutoff, expected):
    history = _FakeBitbucket(pr=_pr(description=V3), pages=[_three_edits_shuffled()]).history()

    pinned = pin_pr_metadata(history, live_title=TITLE, live_description=V3, cutoff=_dt(cutoff))

    assert (pinned.description, pinned.status) == (expected, "pinned")


def test_a_null_description_reads_as_empty_text():
    fake = _FakeBitbucket(
        pr=_pr(description=V1),
        pages=[[_edit(_at(6), None, V1), _update(CREATED, description=None)]],
    )

    history = fake.history()

    assert _by_time(history) == [("", _dt(CREATED)), (V1, _dt(_at(6)))]


def test_an_edit_stamped_before_created_on_keeps_the_original_first():
    fake = _FakeBitbucket(pr=_pr(description=V1), pages=[[_edit("2026-01-05T08:59:00+00:00", ORIGINAL, V1)]])

    history = fake.history()

    assert _by_time(history)[0] == (ORIGINAL, _dt("2026-01-05T08:59:00+00:00"))
    assert _by_time(history)[1][0] == V1


# --- pagination ---------------------------------------------------------------


def test_the_feed_is_followed_through_next_links_to_its_end():
    fake = _FakeBitbucket(
        pr=_pr(description=V3),
        pages=[
            [_edit(_at(10), V2, V3), _comment(_at(9), BOB, "Looks right now.")],
            [_edit(_at(8), V1, V2)],
            [_edit(_at(6), ORIGINAL, V1), _update(CREATED)],
        ],
    )

    history = fake.history()

    activity_calls = [c for c in fake.session.get.call_args_list if c.args[0].startswith(ACTIVITY)]
    assert [c.args[0] for c in activity_calls] == [ACTIVITY, _next(1), _next(2)]
    assert activity_calls[0].kwargs["params"] == {"pagelen": 50}
    assert [c.kwargs["params"] for c in activity_calls[1:]] == [None, None]
    assert history.complete is True
    assert [text for text, _ in _by_time(history)] == [ORIGINAL, V1, V2, V3]
    assert history.first_review_at == _dt(_at(9))


def test_a_feed_that_outruns_the_page_budget_is_incomplete_and_pins_nothing(monkeypatch):
    monkeypatch.setattr(bitbucket, "_MAX_PAGES", 3)
    fake = _FakeBitbucket(
        pr=_pr(description=V3),
        pages=[[_edit(_at(10), V2, V3), _approval(_at(9), BOB)], [_edit(_at(8), V1, V2)]],
        endless=True,
    )

    history = fake.history()

    assert [u for u in fake.urls() if u.startswith(ACTIVITY)] == [ACTIVITY, _next(1), _next(2)]
    assert history.complete is False
    assert history.description_versions == ()
    assert history.title_renames == ()
    assert history.first_review_at is None
    assert history.created_at == _dt(CREATED)
    assert history.head_committed_at == _dt(COMMIT_DATE)
    pinned = pin_pr_metadata(history, live_title=TITLE, live_description=V3, cutoff=_dt(_at(9)))
    assert (pinned.description, pinned.status) == (V3, "live")


def test_the_default_budget_is_the_adapters_page_cap():
    fake = _FakeBitbucket(pages=[[_update(CREATED)]], endless=True)

    history = fake.history()

    assert len([u for u in fake.urls() if u.startswith(ACTIVITY)]) == bitbucket._MAX_PAGES
    assert history.complete is False


def test_a_feed_that_ends_exactly_on_the_last_budgeted_page_is_complete(monkeypatch):
    monkeypatch.setattr(bitbucket, "_MAX_PAGES", 2)
    fake = _FakeBitbucket(pr=_pr(description=V1), pages=[[_edit(_at(6), ORIGINAL, V1)], [_update(CREATED)]])

    history = fake.history()

    assert history.complete is True
    assert [text for text, _ in _by_time(history)] == [ORIGINAL, V1]


# --- title renames ------------------------------------------------------------


def test_a_title_change_becomes_a_rename_and_pins_the_earlier_title():
    fake = _FakeBitbucket(
        pages=[[_rename(_at(7), "WIP cache", TITLE), _update(CREATED, title="WIP cache")]],
    )

    history = fake.history()

    assert history.complete is True
    assert history.title_renames == (
        TitleRename(previous_title="WIP cache", current_title=TITLE, created_at=_dt(_at(7))),
    )
    before = pin_pr_metadata(history, live_title=TITLE, live_description=ORIGINAL, cutoff=_dt(_at(6)))
    after = pin_pr_metadata(history, live_title=TITLE, live_description=ORIGINAL, cutoff=_dt(_at(8)))
    assert (before.title, before.status) == ("WIP cache", "pinned")
    assert (after.title, after.status) == (TITLE, "pinned")


def test_two_renames_chain_to_the_live_title():
    fake = _FakeBitbucket(
        pages=[[
            _rename(_at(9), "Widget cache", TITLE),
            _rename(_at(6), "WIP cache", "Widget cache"),
            _update(CREATED, title="WIP cache"),
        ]],
    )

    history = fake.history()

    assert history.complete is True
    assert sorted((r.created_at, r.previous_title) for r in history.title_renames) == [
        (_dt(_at(6)), "WIP cache"),
        (_dt(_at(9)), "Widget cache"),
    ]


@pytest.mark.parametrize(
    "title_change",
    [
        {"old": "WIP cache"},
        {"new": TITLE},
        {"old": 3, "new": TITLE},
        "WIP cache -> Add widget cache",
    ],
    ids=["no-new", "no-old", "non-text", "not-an-object"],
)
def test_a_title_change_that_cannot_be_read_pins_nothing(title_change):
    entry = _update(_at(7), changes={"title": title_change})
    fake = _FakeBitbucket(
        pr=_pr(description=V1),
        pages=[[_edit(_at(8), ORIGINAL, V1), entry, _approval(_at(6), BOB)]],
    )

    history = fake.history()

    assert history.complete is False
    assert history.description_versions == ()
    assert history.title_renames == ()
    assert history.first_review_at == _dt(_at(6))


def test_a_rename_that_does_not_end_at_the_live_title_pins_nothing():
    fake = _FakeBitbucket(pages=[[_rename(_at(7), "WIP cache", "Widget cache"), _update(CREATED, title="WIP cache")]])

    history = fake.history()

    assert history.complete is False
    assert history.description_versions == ()


def test_a_rename_missing_from_changes_but_visible_in_the_snapshots_pins_nothing():
    fake = _FakeBitbucket(
        pr=_pr(description=V1),
        pages=[[_edit(_at(8), ORIGINAL, V1), _update(_at(6), title="WIP cache"), _update(CREATED, title="WIP cache")]],
    )

    history = fake.history()

    assert history.complete is False
    assert history.description_versions == ()


# --- consistency of the description chain -------------------------------------


def test_edits_that_do_not_end_at_the_live_description_pin_nothing():
    fake = _FakeBitbucket(pr=_pr(description=V3), pages=[[_edit(_at(8), V1, V2), _edit(_at(6), ORIGINAL, V1)]])

    history = fake.history()

    assert history.complete is False
    assert history.description_versions == ()


def test_edits_with_a_gap_between_them_pin_nothing():
    fake = _FakeBitbucket(pr=_pr(description=V3), pages=[[_edit(_at(10), V2, V3), _edit(_at(6), ORIGINAL, V1)]])

    history = fake.history()

    assert history.complete is False
    assert history.description_versions == ()


def test_an_edit_missing_from_changes_but_visible_in_the_snapshots_pins_nothing():
    fake = _FakeBitbucket(
        pr=_pr(description=V2),
        pages=[[_edit(_at(8), V1, V2), _update(_at(7), description=V1), _update(CREATED, description=ORIGINAL)]],
    )

    history = fake.history()

    assert history.complete is False
    assert history.description_versions == ()


def test_a_snapshot_taken_before_its_own_edit_is_accepted():
    before_state = _update(_at(6), description=ORIGINAL, changes={"description": {"old": ORIGINAL, "new": V1}})
    fake = _FakeBitbucket(pr=_pr(description=V1), pages=[[before_state, _update(CREATED)]])

    history = fake.history()

    assert history.complete is True
    assert [text for text, _ in _by_time(history)] == [ORIGINAL, V1]


def test_snapshots_that_show_the_current_state_never_veto_the_recorded_changes():
    def current_state(entry):
        entry["update"]["title"] = TITLE
        entry["update"]["description"] = V2
        return entry

    fake = _FakeBitbucket(
        pr=_pr(title=TITLE, description=V2),
        pages=[[
            current_state(_edit(_at(8), V1, V2)),
            current_state(_rename(_at(7), "WIP cache", TITLE)),
            current_state(_edit(_at(6), ORIGINAL, V1)),
            current_state(_update(CREATED)),
        ]],
    )

    history = fake.history()

    assert history.complete is True
    assert _by_time(history) == [(ORIGINAL, _dt(CREATED)), (V1, _dt(_at(6))), (V2, _dt(_at(8)))]
    assert history.title_renames == (
        TitleRename(previous_title="WIP cache", current_title=TITLE, created_at=_dt(_at(7))),
    )


# --- first human review -------------------------------------------------------


def test_the_first_review_skips_the_author_bots_deleted_comments_and_prxrefs_posts():
    fake = _FakeBitbucket(
        pages=[[
            _comment(_at(11), BOB, "One more nit."),
            _approval(_at(9), BOB),
            _comment(_at(8, 9), CAROL, f"Consider a lock here.\n\n---\n*{ATTRIBUTION_MARKER} · model=m*",
                     inline={"path": "src/cache.py", "to": 12}),
            _comment(_at(8, 8), CAROL, f"{SUMMARY_MARKER}\n## Review summary"),
            _comment(_at(7, 9), BOB, "", deleted=True),
            _comment(_at(7, 8), REVIEW_BOT, "Build passed."),
            _approval(_at(6, 9), ALICE),
            _comment(_at(6, 8), ALICE, "Ready for review."),
            _comment(_at(6, 7), {"type": "user"}, "An account with no identity."),
            _update(CREATED),
        ]],
    )

    history = fake.history()

    assert history.first_review_at == _dt(_at(9))


def test_a_human_comment_counts_as_the_first_review():
    fake = _FakeBitbucket(pages=[[_approval(_at(9), BOB), _comment(_at(8), CAROL, "Why a dict here?")]])

    assert fake.history().first_review_at == _dt(_at(8))


def test_a_request_for_changes_counts_as_the_first_review():
    fake = _FakeBitbucket(pages=[[_comment(_at(9), BOB, "Fixed?"), _changes_requested(_at(8), BOB)]])

    assert fake.history().first_review_at == _dt(_at(8))


def test_the_author_is_matched_on_the_same_identity_list_threads_records():
    other_alice_object = {"type": "user", "uuid": ALICE["uuid"], "display_name": "Alice (renamed)"}
    fake = _FakeBitbucket(pages=[[_approval(_at(9), BOB), _comment(_at(8), other_alice_object, "Updated.")]])

    assert fake.history().first_review_at == _dt(_at(9))


def test_no_identifiable_author_leaves_the_first_review_unknown():
    fake = _FakeBitbucket(pr=_pr(author={}), pages=[[_approval(_at(9), BOB)]])

    assert fake.history().first_review_at is None


# --- head commit date ---------------------------------------------------------


def test_head_sha_is_the_commit_whose_date_is_read():
    fake = _FakeBitbucket(pages=[[]], commit_date="2026-01-04T17:45:00+02:00")

    history = fake.history(head_sha=HEAD_SHA)

    assert fake.urls()[-1] == f"{REPO_API}/commit/{HEAD_SHA}"
    assert f"{REPO_API}/commit/{PR_HEAD}" not in fake.urls()
    assert history.head_committed_at == _dt("2026-01-04T15:45:00+00:00")
    assert history.head_committed_at.utcoffset() == timedelta(hours=2)


def test_without_head_sha_the_prs_current_head_is_read():
    fake = _FakeBitbucket(pages=[[]])

    history = fake.history()

    assert fake.urls()[-1] == f"{REPO_API}/commit/{PR_HEAD}"
    assert history.head_committed_at == _dt(COMMIT_DATE)


def test_a_pr_with_no_head_hash_reads_no_commit():
    pr = _pr()
    pr["source"] = {"branch": {"name": "feature/cache"}}
    fake = _FakeBitbucket(pr=pr, pages=[[]])

    history = fake.history()

    assert not [u for u in fake.urls() if "/commit/" in u]
    assert history.head_committed_at is None


# --- errors -------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403, 404, 500])
@pytest.mark.parametrize("failing", [PR_API, ACTIVITY, f"{REPO_API}/commit/"], ids=["pr", "activity", "commit"])
def test_a_non_ok_response_raises(failing, status):
    fake = _FakeBitbucket(pages=[[]], statuses={failing: status})

    with pytest.raises(requests.HTTPError) as caught:
        fake.history()

    assert caught.value.response.status_code == status


def test_a_401_on_a_later_activity_page_raises_rather_than_returning_short():
    fake = _FakeBitbucket(pages=[[_edit(_at(6), ORIGINAL, V1)], [_update(CREATED)]], statuses={_next(1): 401})

    with pytest.raises(requests.HTTPError):
        fake.history()


def test_a_transport_failure_raises():
    fake = _FakeBitbucket(pages=[[]], statuses={ACTIVITY: requests.ConnectionError("down")})

    with pytest.raises(requests.ConnectionError):
        fake.history()


def test_a_body_that_is_not_an_object_raises():
    fake = _FakeBitbucket(pr=["not", "a", "pull", "request"], pages=[[]])

    with pytest.raises(ValueError, match="list, not an object"):
        fake.history()


@pytest.mark.parametrize(
    ("created_on", "problem"),
    [(None, "missing"), ("yesterday", "not an ISO 8601 time"), ("2026-01-05T09:00:00", "timezone-aware")],
)
def test_a_created_on_that_is_not_an_aware_time_raises(created_on, problem):
    pr = _pr()
    pr["created_on"] = created_on
    fake = _FakeBitbucket(pr=pr, pages=[[]])

    with pytest.raises(ValueError, match=problem):
        fake.history()


def test_a_naive_edit_date_raises():
    fake = _FakeBitbucket(pr=_pr(description=V1), pages=[[_edit("2026-01-06T12:00:00", ORIGINAL, V1)]])

    with pytest.raises(ValueError, match="activity update date must be timezone-aware"):
        fake.history()


# --- the round trip through pin_pr_metadata -----------------------------------


def test_the_history_pins_the_text_in_force_at_the_first_review():
    after_review = "Adds a cache. Reviewer feedback resolution: every comment addressed, see the table."
    fake = _FakeBitbucket(
        pr=_pr(title=TITLE, description=after_review),
        pages=[
            [
                _edit(_at(12), V1, after_review),
                _rename(_at(11), "WIP cache", TITLE, description=V1),
                _comment(_at(10), CAROL, f"{SUMMARY_MARKER}\nprxref summary"),
            ],
            [
                _comment(_at(9), BOB, "TTL invalidation will serve stale widgets."),
                _edit(_at(7), ORIGINAL, V1, title="WIP cache"),
                _update(CREATED, title="WIP cache"),
            ],
        ],
    )

    history = fake.history()
    cutoff, source = choose_cutoff(None, history)
    pinned = pin_pr_metadata(history, live_title=TITLE, live_description=after_review, cutoff=cutoff)

    assert (cutoff, source) == (_dt(_at(9)), "first-review")
    assert pinned.status == "pinned"
    assert pinned.description == V1
    assert pinned.title == "WIP cache"
    _assert_all_aware(history)


def test_an_explicit_cutoff_before_any_edit_pins_the_original():
    fake = _FakeBitbucket(pr=_pr(description=V3), pages=[_three_edits_shuffled()])

    history = fake.history()
    cutoff, source = choose_cutoff(_dt("2026-01-05T10:00:00+00:00"), history)
    pinned = pin_pr_metadata(history, live_title=TITLE, live_description=V3, cutoff=cutoff)

    assert source == "flag"
    assert (pinned.description, pinned.status) == (ORIGINAL, "pinned")
