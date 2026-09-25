"""Issue #16: a ``--pr-url`` replay shows the PR title and description in force at a cutoff.

``cli._replay_forge`` resolves the description eagerly, before the
orchestrator runs: ``--description-file`` and ``--no-description`` fix it
without reading any history; otherwise it reads the forge's optional
``get_pr_history`` once, picks the cutoff (``--as-of``, else the first human
review, else the head commit's date) and asks ``pin_status`` whether that
history pins it. Only a pinned result hands the history and the cutoff to
``ReplayForge``, whose ``get_pr`` then applies ``pin_pr_metadata`` with no
further network call. Every other outcome keeps the PR's current title and
description and logs a WARNING saying why, except an explicit ``--as-of`` on
a forge that cannot read history, which exits 2 naming ``--as-of``. The
returned forge's ``description_pin`` is what the run's stamp reads.

These tests pin ``pin_status`` against ``pin_pr_metadata``, the forge
overrides, every rung of the resolution through ``_replay_forge`` with fake
forges, and the acceptance case through ``main`` and the real orchestrator
and reviewer: the worker prompt carries the pre-cutoff text, and a forge
with no history carries the live text and warns.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import types
from datetime import datetime, timedelta

import pytest

from prxref import cli, orchestrator
from prxref.cli import main
from prxref.forges import replay as replay_module
from prxref.forges.base import DescriptionVersion, PRData, PRHistory, PRRef, TitleRename, detect_forge
from prxref.forges.replay import (
    DescriptionPin,
    LocalDiffForge,
    ReplayForge,
    pin_pr_metadata,
    pin_status,
)
from prxref.llm import ConfigError
from tests.test_issue_16_replay_description import (
    CREATED,
    EDIT_1,
    EDIT_2,
    HEAD_COMMIT,
    LIVE,
    LIVE_TITLE,
    MIDDLE,
    ORIGINAL,
    REVIEW,
    V0,
    V1,
    V2,
    _history,
)
from tests.test_orchestrator import FakeLLM, _added_file_diff, make_pr
from tests.test_replay import BASE, HEAD, RAW_OK, RecordingForge, _mail

URL = "https://github.com/acme/widget/pull/7"
REF = detect_forge(URL)
APP_DIFF = _added_file_diff("src/app.py", 12)
FILE_DIFF = _added_file_diff("src/from_file.py", 5)
AS_OF = CREATED + timedelta(hours=12)
CURRENT = "replay shows the PR's CURRENT title and description"
NO_HISTORY = f"{CURRENT}: the github forge cannot read a pull request's description history"
AS_OF_REFUSED = "--as-of: the github forge cannot read a pull request's description history"
LIVE_PR = dataclasses.replace(make_pr(LIVE_TITLE), description=LIVE)
PINNED_AT_REVIEW = ("parser: add it", MIDDLE)
DELETED_1 = DescriptionVersion(text=None, edited_at=EDIT_1)


class HistoryForge(RecordingForge):
    """``RecordingForge`` plus ``get_pr_history``: returns ``history`` or raises ``error``, every call recorded."""

    def __init__(self, *, history: PRHistory | None = None, error: Exception | None = None, **kw):
        super().__init__(**kw)
        self.history = history
        self.error = error
        self.history_calls: list[tuple[PRRef, str | None]] = []

    def get_pr_history(self, ref, *, head_sha=None):
        self.history_calls.append((ref, head_sha))
        if self.error is not None:
            raise self.error
        return self.history


def _pr_texts(forge) -> tuple[str, str]:
    pr = forge.get_pr(REF)
    return pr.title, pr.description


def _pin_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if CURRENT in r.getMessage()]


CASE_SHAPES = [
    pytest.param(_history(), REVIEW, "pinned", id="edits-before-and-after"),
    pytest.param(_history(), EDIT_1, "pinned", id="edit-exactly-at-the-cutoff"),
    pytest.param(_history(), EDIT_2 + timedelta(days=1), "pinned", id="after-every-edit"),
    pytest.param(_history(description_versions=()), REVIEW, "pinned", id="zero-edits"),
    pytest.param(_history(description_versions=(), complete=False), REVIEW, "live", id="zero-edits-incomplete"),
    pytest.param(_history(description_versions=(V0, DELETED_1, V2)), REVIEW, "live", id="deleted-in-force"),
    pytest.param(_history(description_versions=(V0, V1, DescriptionVersion(None, EDIT_2))), REVIEW, "pinned",
                 id="deleted-not-in-force"),
    pytest.param(_history(description_versions=(V2,), complete=False), REVIEW, "live", id="short"),
    pytest.param(_history(description_versions=(V1, V2), complete=False), REVIEW, "pinned", id="short-but-reaching"),
    pytest.param(_history(), CREATED + timedelta(hours=1), "pinned", id="rename-after-the-cutoff"),
    pytest.param(_history(), CREATED - timedelta(days=1), "pinned", id="cutoff-before-created-at"),
    pytest.param(_history(complete=False), CREATED - timedelta(days=1), "live",
                 id="incomplete-cutoff-before-created-at"),
]


class TestPinStatus:
    """``pin_status`` is ``pin_pr_metadata``'s status, computed without the live texts."""

    @pytest.mark.parametrize(("history", "cutoff", "expected"), CASE_SHAPES)
    def test_it_agrees_with_pin_pr_metadata_on_every_case_shape(self, history, cutoff, expected):
        pinned = pin_pr_metadata(history, live_title=LIVE_TITLE, live_description=LIVE, cutoff=cutoff)
        assert pin_status(history, cutoff) == pinned.status == expected

    @pytest.mark.parametrize(("history", "cutoff", "expected"), CASE_SHAPES)
    def test_the_live_texts_never_move_the_status(self, history, cutoff, expected):
        for title, description in [("", ""), ("other", "other body"), (LIVE_TITLE, ORIGINAL)]:
            pinned = pin_pr_metadata(history, live_title=title, live_description=description, cutoff=cutoff)
            assert pinned.status == expected

    def test_a_naive_cutoff_raises_in_both(self):
        naive = datetime(2026, 3, 2, 9, 0)
        with pytest.raises(ValueError, match="cutoff"):
            pin_status(_history(), naive)
        with pytest.raises(ValueError, match="cutoff"):
            pin_pr_metadata(_history(), live_title=LIVE_TITLE, live_description=LIVE, cutoff=naive)


class TestDescriptionPin:
    def test_it_is_a_frozen_three_field_record(self):
        pin = DescriptionPin("pinned", REVIEW, "first-review")
        assert [f.name for f in dataclasses.fields(DescriptionPin)] == ["status", "as_of", "as_of_source"]
        with pytest.raises(dataclasses.FrozenInstanceError):
            pin.status = "live"


class TestReplayForgeOverrides:
    def test_history_and_cutoff_pin_the_title_and_description(self):
        inner = HistoryForge(pr=LIVE_PR)
        forge = ReplayForge(inner, history=_history(), cutoff=REVIEW)
        assert _pr_texts(forge) == PINNED_AT_REVIEW
        assert forge.get_pr(REF) == dataclasses.replace(LIVE_PR, title="parser: add it", description=MIDDLE)
        assert inner.history_calls == []

    def test_the_pin_is_pin_pr_metadata_over_the_inner_pr(self):
        inner = HistoryForge(pr=LIVE_PR)
        for cutoff in (CREATED - timedelta(days=1), CREATED + timedelta(hours=1), REVIEW, EDIT_2):
            expected = pin_pr_metadata(_history(), live_title=LIVE_TITLE, live_description=LIVE, cutoff=cutoff)
            forge = ReplayForge(inner, history=_history(), cutoff=cutoff)
            assert _pr_texts(forge) == (expected.title, expected.description)

    def test_the_pin_composes_with_the_pinned_shas(self):
        inner = HistoryForge(pr=LIVE_PR)
        pr = ReplayForge(inner, base_sha=BASE, head_sha=HEAD, history=_history(), cutoff=REVIEW).get_pr(REF)
        assert (pr.source_sha, pr.target_sha) == (HEAD, BASE)
        assert (pr.title, pr.description) == PINNED_AT_REVIEW

    @pytest.mark.parametrize("text", ["The description from a file.\n", ""], ids=["text", "empty"])
    def test_a_fixed_description_replaces_only_the_description(self, text):
        inner = HistoryForge(pr=LIVE_PR)
        pr = ReplayForge(inner, description=text).get_pr(REF)
        assert pr == dataclasses.replace(LIVE_PR, description=text)

    def test_a_fixed_description_is_applied_after_a_pin(self):
        forge = ReplayForge(HistoryForge(pr=LIVE_PR), history=_history(), cutoff=REVIEW, description="fixed")
        assert _pr_texts(forge) == ("parser: add it", "fixed")

    def test_the_pin_record_is_kept_and_does_not_change_get_pr(self):
        pin = DescriptionPin("live", None, None)
        inner = HistoryForge(pr=LIVE_PR)
        forge = ReplayForge(inner, description_pin=pin)
        assert forge.description_pin is pin
        assert forge.get_pr(REF) == LIVE_PR
        assert ReplayForge(inner).description_pin is None

    def test_none_of_them_leaves_the_inner_pr(self):
        inner = HistoryForge(pr=LIVE_PR)
        assert ReplayForge(inner, hide_threads=True).get_pr(REF) == LIVE_PR

    @pytest.mark.parametrize("kwargs", [
        {"history": _history()}, {"cutoff": REVIEW},
    ], ids=["history-only", "cutoff-only"])
    def test_history_and_cutoff_must_be_given_together(self, kwargs):
        with pytest.raises(ValueError, match="history and cutoff must be given together"):
            ReplayForge(HistoryForge(), **kwargs)

    def test_a_naive_cutoff_is_refused(self):
        with pytest.raises(ValueError, match="cutoff"):
            ReplayForge(HistoryForge(), history=_history(), cutoff=datetime(2026, 3, 2))


class TestLocalDiffForgeOverride:
    def test_a_description_replaces_the_mail_body_and_keeps_its_title(self):
        forge = LocalDiffForge(_mail(), path="case.patch", description="From the file.")
        pr = forge.get_pr(LocalDiffForge.ref_for("case.patch"))
        assert (pr.title, pr.description, pr.author) == ("Fix the widget", "From the file.", "Alice Example")

    def test_an_empty_description_blanks_the_mail_body(self):
        pr = LocalDiffForge(_mail(), path="case.patch", description="").get_pr(REF)
        assert pr.description == ""

    def test_none_keeps_the_mail_body(self):
        default = LocalDiffForge(_mail(), path="case.patch").get_pr(REF)
        explicit = LocalDiffForge(_mail(), path="case.patch", description=None).get_pr(REF)
        assert default == explicit
        assert default.description == "Explain why the widget broke."

    def test_a_plain_diff_takes_the_description_too(self):
        pr = LocalDiffForge(FILE_DIFF, path="cases/pr.patch", description="Why.").get_pr(REF)
        assert (pr.title, pr.description) == ("Local diff pr.patch", "Why.")


def _history_forge(**history_overrides) -> HistoryForge:
    return HistoryForge(pr=LIVE_PR, history=_history(**history_overrides))


class TestResolutionLadder:
    """Each rung of the cutoff ladder through ``_replay_forge``, and how a pin reaches ``get_pr``."""

    def test_the_flag_rung(self, caplog):
        inner = _history_forge(first_review_at=EDIT_2, head_committed_at=EDIT_2)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            forge = cli._replay_forge(inner, REF, cli._ReplayRequest(as_of=REVIEW))
        assert forge.description_pin == DescriptionPin("pinned", REVIEW, "flag")
        assert _pr_texts(forge) == PINNED_AT_REVIEW
        assert inner.history_calls == [(REF, None)]
        assert _pin_warnings(caplog) == []

    def test_the_first_review_rung(self, caplog):
        inner = _history_forge(first_review_at=REVIEW, head_committed_at=HEAD_COMMIT)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            forge = cli._replay_forge(inner, REF, cli._ReplayRequest(no_threads=True))
        assert forge.description_pin == DescriptionPin("pinned", REVIEW, "first-review")
        assert _pr_texts(forge) == PINNED_AT_REVIEW
        assert _pin_warnings(caplog) == []

    def test_the_head_commit_rung(self, caplog):
        inner = _history_forge(head_committed_at=HEAD_COMMIT)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            forge = cli._replay_forge(inner, REF, cli._ReplayRequest(no_threads=True))
        assert forge.description_pin == DescriptionPin("pinned", HEAD_COMMIT, "head-commit")
        assert _pr_texts(forge) == ("parser: add", ORIGINAL)
        assert _pin_warnings(caplog) == []

    def test_no_rung_is_live_and_warns(self, caplog):
        inner = _history_forge()
        with caplog.at_level(logging.WARNING, logger="prxref"):
            forge = cli._replay_forge(inner, REF, cli._ReplayRequest(no_threads=True))
        assert forge.description_pin == DescriptionPin("live", None, None)
        assert _pr_texts(forge) == (LIVE_TITLE, LIVE)
        assert _pin_warnings(caplog) == [
            f"{CURRENT}: its history has no first human review and no head commit date "
            "to pin them to (give --as-of to choose the time)"
        ]

    def test_the_pinned_head_is_the_head_the_history_is_read_at(self):
        inner = _history_forge(head_committed_at=HEAD_COMMIT)
        replay = cli._ReplayRequest(base_sha=BASE, head_sha=HEAD, no_threads=True)
        cli._replay_forge(inner, REF, replay)
        assert inner.history_calls == [(REF, HEAD)]

    @pytest.mark.parametrize(("overrides", "cutoff_name"), [
        ({"description_versions": (V2,), "complete": False}, "short"),
        ({"description_versions": (V0, DELETED_1, V2)}, "deleted"),
    ], ids=["short", "deleted"])
    def test_a_history_that_does_not_pin_is_live_with_its_cutoff_and_warns(
        self, caplog, monkeypatch, overrides, cutoff_name,
    ):
        pins: list[object] = []
        real = replay_module.pin_pr_metadata
        monkeypatch.setattr(replay_module, "pin_pr_metadata", lambda *a, **k: pins.append(1) or real(*a, **k))
        inner = _history_forge(first_review_at=REVIEW, **overrides)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            forge = cli._replay_forge(inner, REF, cli._ReplayRequest(no_threads=True))
        assert forge.description_pin == DescriptionPin("live", REVIEW, "first-review")
        assert forge.get_pr(REF) == LIVE_PR
        assert pins == []
        assert _pin_warnings(caplog) == [
            f"{CURRENT}: its description history does not reach the first-review cutoff "
            f"{REVIEW.isoformat()} (it is incomplete, or the version then in force was deleted)"
        ]

    def test_a_pinned_resolution_does_pin_through_get_pr(self, monkeypatch):
        pins: list[object] = []
        real = replay_module.pin_pr_metadata
        monkeypatch.setattr(replay_module, "pin_pr_metadata", lambda *a, **k: pins.append(1) or real(*a, **k))
        forge = cli._replay_forge(_history_forge(first_review_at=REVIEW), REF, cli._ReplayRequest(no_threads=True))
        assert _pr_texts(forge) == PINNED_AT_REVIEW
        assert pins == [1]


class TestResolutionFallbacks:
    def test_no_getter_with_as_of_is_a_config_error_before_any_warning(self, caplog):
        inner = RecordingForge(pr=LIVE_PR)
        replay = cli._ReplayRequest(base_sha=BASE, head_sha=HEAD, as_of=AS_OF)
        with caplog.at_level(logging.WARNING, logger="prxref"), pytest.raises(ConfigError) as exc:
            cli._replay_forge(inner, REF, replay)
        assert str(exc.value) == AS_OF_REFUSED
        assert caplog.records == []
        assert inner.calls == []

    def test_no_getter_with_as_of_exits_2_before_the_llm_client(self, monkeypatch, capsys):
        built: list[object] = []
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: RecordingForge(pr=LIVE_PR))
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: built.append(cfg))
        assert main(["review", "--pr-url", URL, "--as-of", "2026-03-01T21:00:00Z"]) == 2
        assert capsys.readouterr().err == f"configuration error: {AS_OF_REFUSED}\n"
        assert built == []

    def test_no_getter_without_as_of_is_live_and_warns(self, caplog):
        inner = RecordingForge(pr=LIVE_PR)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            forge = cli._replay_forge(inner, REF, cli._ReplayRequest(no_threads=True))
        assert forge.description_pin == DescriptionPin("live", None, None)
        assert forge.get_pr(REF) == LIVE_PR
        assert _pin_warnings(caplog) == [NO_HISTORY]

    @pytest.mark.parametrize(("as_of", "pin"), [
        (None, DescriptionPin("live", None, None)),
        (AS_OF, DescriptionPin("live", AS_OF, "flag")),
    ], ids=["default-cutoff", "as-of"])
    def test_a_getter_that_raises_is_live_and_warns_with_the_error(self, caplog, as_of, pin):
        inner = HistoryForge(pr=LIVE_PR, error=PermissionError("403 Forbidden for the GraphQL endpoint"))
        with caplog.at_level(logging.WARNING, logger="prxref"):
            forge = cli._replay_forge(inner, REF, cli._ReplayRequest(as_of=as_of, no_threads=True))
        assert forge.description_pin == pin
        assert forge.get_pr(REF) == LIVE_PR
        assert inner.history_calls == [(REF, None)]
        assert _pin_warnings(caplog) == [
            f"{CURRENT}: reading its description history failed "
            "(PermissionError: 403 Forbidden for the GraphQL endpoint)"
        ]


class TestFixedDescriptions:
    """``--description-file`` and ``--no-description`` never read history."""

    @pytest.mark.parametrize(("replay", "description", "pin"), [
        (cli._ReplayRequest(description_file="d.md", description_text="From the file.\n"),
         "From the file.\n", DescriptionPin("file", None, None)),
        (cli._ReplayRequest(description_file="blank.md", description_text=""),
         "", DescriptionPin("file", None, None)),
        (cli._ReplayRequest(no_description=True), "", DescriptionPin("none", None, None)),
    ], ids=["file", "blank-file", "no-description"])
    def test_the_getter_is_never_called(self, caplog, replay, description, pin):
        inner = _history_forge(first_review_at=REVIEW)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            forge = cli._replay_forge(inner, REF, replay)
        assert inner.history_calls == []
        assert forge.description_pin == pin
        assert _pr_texts(forge) == (LIVE_TITLE, description)
        assert _pin_warnings(caplog) == []

    def test_a_forge_without_history_does_not_warn_either(self, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref"):
            forge = cli._replay_forge(RecordingForge(pr=LIVE_PR), REF, cli._ReplayRequest(no_description=True))
        assert forge.description_pin == DescriptionPin("none", None, None)
        assert caplog.records == []


OLD_TITLE = "Draft: widget config loader"
NEW_TITLE = "Widget loader, every review comment addressed"
OLD_DESC = "Loads the widget configuration from disk."
NEW_DESC = "Review fixes applied: renamed data, added the missing tests."
REVIEWED_AT = CREATED + timedelta(days=2)
EDITED_AT = CREATED + timedelta(days=3)
ACCEPTANCE_HISTORY = PRHistory(
    created_at=CREATED,
    description_versions=(
        DescriptionVersion(text=OLD_DESC, edited_at=CREATED),
        DescriptionVersion(text=NEW_DESC, edited_at=EDITED_AT),
    ),
    title_renames=(TitleRename(previous_title=OLD_TITLE, current_title=NEW_TITLE, created_at=EDITED_AT),),
    first_review_at=REVIEWED_AT,
)
NEW_PR = PRData(
    title=NEW_TITLE, description=NEW_DESC, author="alice", source_branch="feature/widget",
    target_branch="main", source_sha="a" * 40, target_sha="b" * 40, raw={},
)


class _PromptLLM(FakeLLM):
    """``FakeLLM`` that keeps every prompt it is sent."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.prompts: list[str] = []

    def invoke(self, system, user, **kwargs):
        with self._lock:
            self.prompts.append(f"{system}\n{user}")
        return super().invoke(system, user, **kwargs)


class TestAcceptanceThroughTheRealOrchestrator:
    """``main`` with the real orchestrator and reviewer; only the forge and the LLM are doubles."""

    @pytest.fixture
    def rig(self, monkeypatch):
        rig = types.SimpleNamespace(inner=None, llm=_PromptLLM(RAW_OK), forges=[])
        real = orchestrator.orchestrate_review

        def spy(**kwargs):
            rig.forges.append(kwargs["forge"])
            return real(**kwargs)

        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: rig.inner)
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: rig.llm)
        monkeypatch.setattr(orchestrator, "orchestrate_review", spy)
        return rig

    def _described_prompts(self, rig) -> list[str]:
        prompts = [p for p in rig.llm.prompts if "PR title:" in p]
        assert prompts, "no prompt carried the PR title and description"
        return prompts

    @pytest.mark.parametrize("args", [
        [], ["--as-of", "2026-03-02T00:00:00Z"],
    ], ids=["first-review", "as-of"])
    def test_the_prompt_carries_the_description_in_force_at_the_cutoff(self, rig, caplog, capsys, args):
        rig.inner = HistoryForge(pr=NEW_PR, diff=APP_DIFF, history=ACCEPTANCE_HISTORY)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            assert main(["review", "--pr-url", URL, "--no-threads", "--format", "json", *args]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "Approved"
        for prompt in self._described_prompts(rig):
            assert _context(OLD_TITLE, OLD_DESC) in prompt
            assert NEW_DESC not in prompt and NEW_TITLE not in prompt
        [forge] = rig.forges
        assert forge.description_pin.status == "pinned"
        assert forge.description_pin.as_of_source == ("flag" if args else "first-review")
        assert rig.inner.history_calls == [(REF, None)]
        assert _pin_warnings(caplog) == []

    def test_control_a_forge_without_history_shows_the_live_text_and_warns(self, rig, caplog, capsys):
        rig.inner = RecordingForge(pr=NEW_PR, diff=APP_DIFF)
        with caplog.at_level(logging.WARNING, logger="prxref"):
            assert main(["review", "--pr-url", URL, "--no-threads", "--format", "json"]) == 0
        assert json.loads(capsys.readouterr().out)["verdict"] == "Approved"
        for prompt in self._described_prompts(rig):
            assert _context(NEW_TITLE, NEW_DESC) in prompt
            assert OLD_DESC not in prompt and OLD_TITLE not in prompt
        [forge] = rig.forges
        assert forge.description_pin == DescriptionPin("live", None, None)
        assert _pin_warnings(caplog) == [NO_HISTORY]

    def test_a_description_file_replaces_the_live_text_without_reading_history(
        self, rig, capsys, tmp_path,
    ):
        rig.inner = HistoryForge(pr=NEW_PR, diff=APP_DIFF, history=ACCEPTANCE_HISTORY)
        path = tmp_path / "pr.md"
        path.write_text("Written by the evaluator.\n", encoding="utf-8")
        assert main(["review", "--pr-url", URL, "--description-file", str(path), "--format", "json"]) == 0
        capsys.readouterr()
        for prompt in self._described_prompts(rig):
            assert _context(NEW_TITLE, "Written by the evaluator.") in prompt
            assert NEW_DESC not in prompt and OLD_DESC not in prompt
        assert rig.inner.history_calls == []
        assert rig.forges[0].description_pin == DescriptionPin("file", None, None)

    @pytest.mark.parametrize(("args", "shown", "hidden"), [
        (["--description-file", "{desc}"], "Written by the evaluator.", "Explain why the widget broke."),
        (["--no-description"], "(none)", "Explain why the widget broke."),
        ([], "Explain why the widget broke.", "Written by the evaluator."),
    ], ids=["description-file", "no-description", "neither"])
    def test_a_lone_diff_file_takes_the_description_flags(self, rig, capsys, tmp_path, args, shown, hidden):
        patch = tmp_path / "case.patch"
        patch.write_text(_mail(diff=APP_DIFF), encoding="utf-8")
        desc = tmp_path / "pr.md"
        desc.write_text("Written by the evaluator.\n", encoding="utf-8")
        args = [str(desc) if a == "{desc}" else a for a in args]
        assert main(["review", "--diff-file", str(patch), "--format", "json", *args]) == 0
        capsys.readouterr()
        for prompt in self._described_prompts(rig):
            assert _context("Fix the widget", shown) in prompt
            assert hidden not in prompt
        assert isinstance(rig.forges[0], LocalDiffForge)


def _context(title: str, description: str) -> str:
    """The ``## Review Context`` lines worker.md and systemic.md render for a PR."""
    return f"PR title: {title}\n\nPR description:\n{description}\n\nRepo:"
