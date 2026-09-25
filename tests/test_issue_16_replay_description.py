"""Issue #16: pin a replay's PR title and description to a cutoff.

A replay of a PR must not show the reviewer a description the author edited
after review to list the fixes. These are the pure pieces, with no forge and
no network: the ``PRHistory`` a forge's optional ``get_pr_history`` returns,
``pin_pr_metadata`` (the title and description in force at a cutoff, or the
live ones) and ``choose_cutoff`` (``--as-of``, else the first human review,
else the head commit's date). The CLI wiring, the stamp and the forge
implementations are later tasks and are tested there.
"""
from __future__ import annotations

import dataclasses
import inspect
import itertools
import socket
from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest

from prxref.forges.base import DescriptionVersion, Forge, PRHistory, TitleRename
from prxref.forges.replay import PinnedMetadata, choose_cutoff, pin_pr_metadata
from tests.test_orchestrator import FakeForge

CREATED = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
EDIT_1 = CREATED + timedelta(days=1)
EDIT_2 = CREATED + timedelta(days=3)
REVIEW = CREATED + timedelta(days=2)
HEAD_COMMIT = CREATED + timedelta(hours=5)

ORIGINAL = "Adds the parser."
MIDDLE = "Adds the parser and its tests."
LIVE = "Adds the parser.\n\n| Review comment | Fix |\n| rename data | done |"
LIVE_TITLE = "parser: add, address review"

V0 = DescriptionVersion(text=ORIGINAL, edited_at=CREATED)
V1 = DescriptionVersion(text=MIDDLE, edited_at=EDIT_1)
V2 = DescriptionVersion(text=LIVE, edited_at=EDIT_2)
VERSIONS = (V0, V1, V2)

RENAME_1 = TitleRename(previous_title="parser: add", current_title="parser: add it", created_at=EDIT_1)
RENAME_2 = TitleRename(previous_title="parser: add it", current_title=LIVE_TITLE, created_at=EDIT_2)
RENAMES = (RENAME_1, RENAME_2)


def _history(**overrides) -> PRHistory:
    fields = {"created_at": CREATED, "description_versions": VERSIONS, "title_renames": RENAMES}
    fields.update(overrides)
    return PRHistory(**fields)


def _pin(history: PRHistory, cutoff: datetime) -> PinnedMetadata:
    return pin_pr_metadata(history, live_title=LIVE_TITLE, live_description=LIVE, cutoff=cutoff)


LIVE_RESULT = PinnedMetadata(title=LIVE_TITLE, description=LIVE, status="live")


class TestProtocolDeclaration:
    def test_the_protocol_declares_it_with_a_keyword_only_head_sha(self):
        signature = inspect.signature(Forge.get_pr_history)
        params = signature.parameters
        assert list(params) == ["self", "ref", "head_sha"]
        assert params["head_sha"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["head_sha"].default is None
        assert signature.return_annotation == "PRHistory"

    def test_the_docstring_says_it_is_optional_and_how_to_resolve_it(self):
        doc = inspect.getdoc(Forge.get_pr_history)
        assert 'getattr(forge, "get_pr_history", None)' in doc
        assert "not every forge implements it" in doc
        assert "complete=False" in doc

    def test_a_forge_without_it_resolves_to_none(self):
        assert getattr(FakeForge(), "get_pr_history", None) is None


class TestPRHistory:
    def test_defaults_are_an_unedited_complete_history(self):
        history = PRHistory(created_at=CREATED)
        assert history.description_versions == ()
        assert history.title_renames == ()
        assert history.first_review_at is None
        assert history.head_committed_at is None
        assert history.complete is True

    def test_lists_are_stored_as_tuples_so_a_history_is_hashable(self):
        history = PRHistory(created_at=CREATED, description_versions=[V0, V1], title_renames=[RENAME_1])
        assert history.description_versions == (V0, V1)
        assert history.title_renames == (RENAME_1,)
        assert hash(history) == hash(PRHistory(created_at=CREATED, description_versions=(V0, V1),
                                               title_renames=(RENAME_1,)))

    @pytest.mark.parametrize("instance, field", [
        (PRHistory(created_at=CREATED), "complete"),
        (V0, "text"),
        (RENAME_1, "previous_title"),
        (PinnedMetadata(title="t", description="d", status="pinned"), "status"),
    ])
    def test_every_type_is_frozen(self, instance, field):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, field, None)

    def test_a_version_may_have_no_text(self):
        assert DescriptionVersion(text=None, edited_at=EDIT_1).text is None

    @pytest.mark.parametrize("field", ["description_versions", "title_renames"])
    def test_a_foreign_element_is_refused_naming_the_field(self, field):
        with pytest.raises(ValueError, match=f"PRHistory.{field} must hold"):
            PRHistory(created_at=CREATED, **{field: [{"text": "x"}]})


NAIVE = datetime(2026, 3, 1, 9, 0)


class TestNaiveDatetimesAreRefused:
    @pytest.mark.parametrize("build, name", [
        (lambda: PRHistory(created_at=NAIVE), "PRHistory.created_at"),
        (lambda: PRHistory(created_at=CREATED, first_review_at=NAIVE), "PRHistory.first_review_at"),
        (lambda: PRHistory(created_at=CREATED, head_committed_at=NAIVE), "PRHistory.head_committed_at"),
        (lambda: DescriptionVersion(text="x", edited_at=NAIVE), "DescriptionVersion.edited_at"),
        (lambda: TitleRename(previous_title="a", current_title="b", created_at=NAIVE), "TitleRename.created_at"),
        (lambda: _pin(_history(), NAIVE), "cutoff"),
        (lambda: choose_cutoff(NAIVE, None), "as_of"),
    ])
    def test_naive_raises_value_error_naming_the_field(self, build, name):
        with pytest.raises(ValueError, match=rf"^{name} must be timezone-aware"):
            build()

    def test_a_non_datetime_is_refused_too(self):
        with pytest.raises(ValueError, match=r"^PRHistory.created_at must be a datetime, got str"):
            PRHistory(created_at="2026-03-01T09:00:00Z")

    def test_a_tzinfo_with_no_offset_counts_as_naive(self):
        class NoOffset(tzinfo):
            def utcoffset(self, dt):
                return None

        with pytest.raises(ValueError, match="must be timezone-aware"):
            PRHistory(created_at=datetime(2026, 3, 1, tzinfo=NoOffset()))


class TestDescriptionInForce:
    def test_edits_before_and_after_the_cutoff_give_the_pre_cutoff_text(self):
        result = _pin(_history(), REVIEW)
        assert result == PinnedMetadata(title="parser: add it", description=MIDDLE, status="pinned")

    @pytest.mark.parametrize("order", list(itertools.permutations(VERSIONS)))
    def test_versions_are_ordered_by_edited_at_not_by_position(self, order):
        assert _pin(_history(description_versions=order), REVIEW).description == MIDDLE

    def test_an_edit_exactly_at_the_cutoff_is_in_force(self):
        assert _pin(_history(), EDIT_1).description == MIDDLE

    def test_a_cutoff_after_every_edit_gives_the_live_version_pinned(self):
        result = _pin(_history(), EDIT_2 + timedelta(days=1))
        assert result == PinnedMetadata(title=LIVE_TITLE, description=LIVE, status="pinned")

    def test_a_cutoff_before_created_at_clamps_to_the_original(self):
        result = _pin(_history(), CREATED - timedelta(days=30))
        assert result == PinnedMetadata(title="parser: add", description=ORIGINAL, status="pinned")

    def test_a_rename_stamped_before_created_at_still_leaves_the_original_title(self):
        skewed = TitleRename(previous_title="parser: add", current_title=LIVE_TITLE,
                             created_at=CREATED - timedelta(seconds=10))
        result = _pin(_history(title_renames=(skewed,)), CREATED - timedelta(days=1))
        assert result == PinnedMetadata(title="parser: add", description=ORIGINAL, status="pinned")

    def test_a_complete_history_whose_oldest_version_is_after_the_cutoff_gives_the_oldest(self):
        late = (DescriptionVersion(text=MIDDLE, edited_at=EDIT_1), V2)
        result = _pin(_history(description_versions=late, title_renames=()), CREATED + timedelta(hours=1))
        assert result == PinnedMetadata(title=LIVE_TITLE, description=MIDDLE, status="pinned")


class TestZeroEdits:
    def test_zero_edits_gives_the_live_body_and_counts_as_pinned(self):
        result = _pin(_history(description_versions=(), title_renames=()), REVIEW)
        assert result == PinnedMetadata(title=LIVE_TITLE, description=LIVE, status="pinned")

    def test_zero_edits_still_pins_the_title(self):
        result = _pin(_history(description_versions=()), REVIEW)
        assert result == PinnedMetadata(title="parser: add it", description=LIVE, status="pinned")

    def test_zero_edits_in_an_incomplete_history_is_live(self):
        assert _pin(_history(description_versions=(), complete=False), REVIEW) == LIVE_RESULT


class TestDeletedVersion:
    def test_a_deleted_version_in_force_gives_live(self):
        deleted = (V0, DescriptionVersion(text=None, edited_at=EDIT_1), V2)
        assert _pin(_history(description_versions=deleted), REVIEW) == LIVE_RESULT

    def test_a_deleted_version_not_in_force_does_not_matter(self):
        deleted = (DescriptionVersion(text=None, edited_at=CREATED), V1, V2)
        result = _pin(_history(description_versions=deleted), REVIEW)
        assert result == PinnedMetadata(title="parser: add it", description=MIDDLE, status="pinned")


class TestIncompleteHistory:
    def test_an_incomplete_history_that_does_not_reach_the_cutoff_gives_live(self):
        newest_only = (V2,)
        assert _pin(_history(description_versions=newest_only, complete=False), REVIEW) == LIVE_RESULT

    def test_an_incomplete_history_that_reaches_the_cutoff_is_pinned(self):
        newest_two = (V2, V1)
        result = _pin(_history(description_versions=newest_two, complete=False), REVIEW)
        assert result == PinnedMetadata(title="parser: add it", description=MIDDLE, status="pinned")

    def test_an_incomplete_history_cannot_clamp_to_an_original_it_does_not_hold(self):
        newest_two = (V2, V1)
        assert _pin(_history(description_versions=newest_two, complete=False), CREATED - timedelta(days=1)) \
            == LIVE_RESULT


class TestTitle:
    def test_the_title_is_the_previous_title_of_the_first_rename_after_the_cutoff(self):
        assert _pin(_history(), REVIEW).title == "parser: add it"

    @pytest.mark.parametrize("order", [RENAMES, tuple(reversed(RENAMES))])
    def test_renames_are_ordered_by_created_at_not_by_position(self, order):
        assert _pin(_history(title_renames=order), CREATED + timedelta(hours=1)).title == "parser: add"

    def test_no_rename_after_the_cutoff_gives_the_live_title(self):
        assert _pin(_history(), EDIT_2 + timedelta(seconds=1)).title == LIVE_TITLE

    def test_a_rename_exactly_at_the_cutoff_is_not_after_it(self):
        assert _pin(_history(), EDIT_1).title == "parser: add it"
        assert _pin(_history(), EDIT_2).title == LIVE_TITLE

    def test_the_title_is_live_whenever_the_description_is(self):
        deleted = (V0, DescriptionVersion(text=None, edited_at=EDIT_1), V2)
        assert _pin(_history(description_versions=VERSIONS), REVIEW).title == "parser: add it"
        result = _pin(_history(description_versions=deleted), REVIEW)
        assert result.status == "live"
        assert result.title == LIVE_TITLE


class TestTimezones:
    def test_offsets_are_compared_as_instants_not_wall_clocks(self):
        pacific = timezone(timedelta(hours=-8))
        india = timezone(timedelta(hours=5, minutes=30))
        created = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
        early = DescriptionVersion(text="early", edited_at=datetime(2026, 3, 1, 20, 0, tzinfo=india))
        late = DescriptionVersion(text="late", edited_at=datetime(2026, 3, 1, 10, 0, tzinfo=pacific))
        history = PRHistory(created_at=created, description_versions=(late, early))
        cutoff = datetime(2026, 3, 1, 16, 0, tzinfo=UTC)
        assert early.edited_at < cutoff < late.edited_at
        assert late.edited_at.replace(tzinfo=None) < cutoff.replace(tzinfo=None)
        result = pin_pr_metadata(history, live_title="t", live_description="late", cutoff=cutoff)
        assert result == PinnedMetadata(title="t", description="early", status="pinned")

    def test_a_cutoff_in_another_offset_names_the_same_instant(self):
        plus_two = timezone(timedelta(hours=2))
        assert _pin(_history(), EDIT_1.astimezone(plus_two)).description == MIDDLE
        assert _pin(_history(), (EDIT_1 - timedelta(seconds=1)).astimezone(plus_two)).description == ORIGINAL


class TestChooseCutoff:
    def test_the_flag_wins_over_everything(self):
        history = _history(first_review_at=REVIEW, head_committed_at=HEAD_COMMIT)
        as_of = EDIT_1 + timedelta(minutes=7)
        assert choose_cutoff(as_of, history) == (as_of, "flag")

    def test_the_flag_needs_no_history(self):
        assert choose_cutoff(REVIEW, None) == (REVIEW, "flag")

    def test_the_first_review_comes_next(self):
        history = _history(first_review_at=REVIEW, head_committed_at=HEAD_COMMIT)
        assert choose_cutoff(None, history) == (REVIEW, "first-review")

    def test_the_head_commit_comes_last(self):
        assert choose_cutoff(None, _history(head_committed_at=HEAD_COMMIT)) == (HEAD_COMMIT, "head-commit")

    def test_nothing_available_gives_none(self):
        assert choose_cutoff(None, _history()) is None

    def test_no_flag_and_no_history_gives_none(self):
        assert choose_cutoff(None, None) is None

    def test_the_cutoff_is_returned_unclamped(self):
        early = CREATED - timedelta(days=5)
        assert choose_cutoff(early, _history()) == (early, "flag")

    def test_a_head_commit_before_the_pr_was_opened_pins_the_original(self):
        commit = CREATED - timedelta(days=2)
        history = _history(head_committed_at=commit)
        cutoff, source = choose_cutoff(None, history)
        assert (cutoff, source) == (commit, "head-commit")
        assert _pin(history, cutoff) == PinnedMetadata(title="parser: add", description=ORIGINAL, status="pinned")


class TestPureLogic:
    def test_pinning_and_choosing_never_touch_the_network(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError("network used")

        monkeypatch.setattr(socket, "socket", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)
        history = _history(first_review_at=REVIEW)
        cutoff, _source = choose_cutoff(None, history)
        assert _pin(history, cutoff).description == MIDDLE
