"""Issue #16: the replay stamp's description keys and the ``replay:`` summary line.

``_ReplayRequest.stamp`` appends three keys to the four of 0.14:
``description`` (``pinned``, ``live``, ``file`` or ``none``), ``as_of`` (the
cutoff as a UTC ISO-8601 time ending in ``Z``, or null) and ``as_of_source``
(``flag``, ``first-review`` or ``head-commit``, or null). All seven are
present in every replay stamp (decisions.md D1). On the ``--pr-url`` path
they are copied from the replay forge's ``DescriptionPin``; without a forge,
or when the forge carries no ``DescriptionPin`` (a test recorder's
``MagicMock``), they are read from the flags. The text summary's ``replay:``
line ends in ``description=<status>`` and, when a cutoff was chosen,
`` as_of=<time> (<source>)``.
"""
from __future__ import annotations

import io
import json
import sys
import types
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from prxref import cli
from prxref.cli import main
from prxref.forges.base import detect_forge
from prxref.forges.replay import DescriptionPin, LocalDiffForge, ReplayForge
from tests.test_issue_16_replay_description import CREATED, HEAD_COMMIT, REVIEW, V2, _history
from tests.test_orchestrator import _added_file_diff
from tests.test_replay import BASE, HEAD, RecordingForge

URL = "https://github.com/acme/widget/pull/7"
APP_DIFF = _added_file_diff("src/app.py", 12)
FILE_DIFF = _added_file_diff("src/from_file.py", 5)
KEYS = ["base_sha", "head_sha", "threads", "diff_file", "description", "as_of", "as_of_source"]
PLUS_TWO = timezone(timedelta(hours=2))
MINUS_SEVEN = timezone(timedelta(hours=-7))
REVIEW_Z = "2026-03-03T09:00:00Z"
HEAD_COMMIT_Z = "2026-03-01T14:00:00Z"


class HistoryForge(RecordingForge):
    """``RecordingForge`` plus ``get_pr_history``, which returns ``history``."""

    def __init__(self, *, history, **kw):
        super().__init__(**kw)
        self.history = history

    def get_pr_history(self, ref, *, head_sha=None):
        return self.history


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    """Everything past config is a double: ``make_forge`` hands back
    ``rec.made``, and the orchestrator echoes the stamp into its result the
    way the real one does. The working directory is ``tmp_path``."""
    monkeypatch.chdir(tmp_path)
    rec = types.SimpleNamespace(orchestrate=[], made=RecordingForge(diff=APP_DIFF))

    def fake_orchestrate_review(**kwargs):
        rec.orchestrate.append(kwargs)
        result = {"verdict": "Approved", "findings_active": [], "findings_dropped": []}
        if kwargs["replay"] is not None:
            result["replay"] = dict(kwargs["replay"])
        return result

    llm = types.ModuleType("prxref.llm_backends")
    llm.create_llm_client = lambda cfg: MagicMock(name="LLMClient")
    orch = types.ModuleType("prxref.orchestrator")
    orch.orchestrate_review = fake_orchestrate_review
    monkeypatch.setitem(sys.modules, "prxref.llm_backends", llm)
    monkeypatch.setitem(sys.modules, "prxref.orchestrator", orch)
    monkeypatch.setattr("prxref.cli.make_forge", lambda ref: rec.made)
    return rec


@pytest.fixture
def diff_file(tmp_path) -> str:
    path = tmp_path / "case.patch"
    path.write_text(FILE_DIFF, encoding="utf-8")
    return "case.patch"


@pytest.fixture
def description_file(tmp_path) -> str:
    path = tmp_path / "description.md"
    path.write_text("The description as it was at review time.\n", encoding="utf-8")
    return "description.md"


def _stamp_of(runtime) -> dict:
    [call] = runtime.orchestrate
    return call["replay"]


def _line(stamp: dict) -> str:
    buf = io.StringIO()
    cli._print_summary({"verdict": "Approved", "replay": stamp}, 0.0, verbose=False, out=buf)
    return buf.getvalue().splitlines()[-1]


class TestStampFromThePin:
    @pytest.mark.parametrize(("pin", "expected"), [
        (DescriptionPin("pinned", REVIEW, "first-review"), ("pinned", REVIEW_Z, "first-review")),
        (DescriptionPin("live", REVIEW, "first-review"), ("live", REVIEW_Z, "first-review")),
        (DescriptionPin("live", None, None), ("live", None, None)),
        (DescriptionPin("file", None, None), ("file", None, None)),
        (DescriptionPin("none", None, None), ("none", None, None)),
    ], ids=["pinned", "live-with-cutoff", "live", "file", "none"])
    def test_all_seven_keys_are_present_in_order(self, pin, expected):
        stamp = cli._ReplayRequest(base_sha=BASE, head_sha=HEAD).stamp(has_forge=True, pin=pin)
        assert list(stamp) == KEYS
        assert stamp == {
            "base_sha": BASE, "head_sha": HEAD, "threads": "shown", "diff_file": None,
            "description": expected[0], "as_of": expected[1], "as_of_source": expected[2],
        }

    @pytest.mark.parametrize("source", ["flag", "first-review", "head-commit"])
    def test_the_cutoff_source_is_copied(self, source):
        stamp = cli._ReplayRequest().stamp(has_forge=True, pin=DescriptionPin("pinned", REVIEW, source))
        assert stamp["as_of_source"] == source

    def test_a_non_utc_cutoff_is_written_in_utc_ending_in_z(self):
        cutoff = datetime(2026, 5, 1, 11, 30, tzinfo=PLUS_TWO)
        stamp = cli._ReplayRequest().stamp(has_forge=True, pin=DescriptionPin("pinned", cutoff, "flag"))
        assert stamp["as_of"] == "2026-05-01T09:30:00Z"

    def test_a_cutoff_west_of_utc_can_move_to_the_next_day(self):
        cutoff = datetime(2026, 5, 1, 20, 15, 5, tzinfo=MINUS_SEVEN)
        stamp = cli._ReplayRequest().stamp(has_forge=True, pin=DescriptionPin("pinned", cutoff, "head-commit"))
        assert stamp["as_of"] == "2026-05-02T03:15:05Z"

    def test_a_fraction_of_a_second_is_kept_not_floored(self):
        cutoff = datetime(2026, 5, 1, 11, 30, 0, 250000, tzinfo=PLUS_TWO)
        stamp = cli._ReplayRequest().stamp(has_forge=True, pin=DescriptionPin("pinned", cutoff, "first-review"))
        assert stamp["as_of"] == "2026-05-01T09:30:00.250000Z"

    @pytest.mark.parametrize("cutoff", [
        datetime(2026, 5, 1, 11, 30, tzinfo=PLUS_TWO),
        datetime(2026, 5, 1, 11, 30, 0, 250000, tzinfo=PLUS_TWO),
        datetime(2026, 5, 1, 9, 30, 0, 1, tzinfo=UTC),
    ], ids=["whole-seconds", "fraction", "one-microsecond"])
    def test_the_written_time_given_back_as_as_of_is_the_same_instant(self, cutoff):
        stamp = cli._ReplayRequest().stamp(has_forge=True, pin=DescriptionPin("pinned", cutoff, "first-review"))
        assert cli._parse_as_of(stamp["as_of"]) == cutoff


class TestStampFromTheFlags:
    """No ``DescriptionPin``: the local path always, and a ``--pr-url`` forge that carries none."""

    @pytest.mark.parametrize("pin", [None, MagicMock(), MagicMock().description_pin, "pinned"],
                             ids=["none", "magicmock", "magicmock-attribute", "a-string"])
    @pytest.mark.parametrize(("has_forge", "expected"), [(True, "live"), (False, "file")], ids=["forge", "local"])
    def test_anything_but_a_pin_is_treated_as_no_pin(self, pin, has_forge, expected):
        stamp = cli._ReplayRequest(diff_file="case.patch").stamp(has_forge=has_forge, pin=pin)
        assert list(stamp) == KEYS
        assert stamp["description"] == expected
        assert stamp["as_of"] is None
        assert stamp["as_of_source"] is None
        json.dumps(stamp)

    @pytest.mark.parametrize("has_forge", [True, False], ids=["forge", "local"])
    def test_description_file_is_file(self, has_forge):
        replay = cli._ReplayRequest(description_file="d.md", description_text="")
        assert replay.stamp(has_forge=has_forge)["description"] == "file"

    @pytest.mark.parametrize("has_forge", [True, False], ids=["forge", "local"])
    def test_no_description_is_none(self, has_forge):
        assert cli._ReplayRequest(no_description=True).stamp(has_forge=has_forge)["description"] == "none"

    def test_as_of_without_a_pin_is_not_stamped_as_a_cutoff(self):
        stamp = cli._ReplayRequest(as_of=REVIEW).stamp(has_forge=True)
        assert (stamp["description"], stamp["as_of"], stamp["as_of_source"]) == ("live", None, None)


class TestThroughTheCli:
    """``main`` → ``_run_review`` → the stamp the orchestrator receives."""

    def test_the_first_review_rung_is_pinned(self, runtime):
        runtime.made = HistoryForge(diff=APP_DIFF, history=_history(first_review_at=REVIEW.astimezone(MINUS_SEVEN)))
        assert main(["review", "--pr-url", URL, "--no-threads"]) == 0
        stamp = _stamp_of(runtime)
        assert list(stamp) == KEYS
        assert (stamp["description"], stamp["as_of"], stamp["as_of_source"]) == ("pinned", REVIEW_Z, "first-review")

    def test_the_head_commit_rung_is_pinned(self, runtime):
        runtime.made = HistoryForge(diff=APP_DIFF, history=_history(head_committed_at=HEAD_COMMIT))
        assert main(["review", "--pr-url", URL, "--base-sha", BASE, "--head-sha", HEAD, "--no-threads"]) == 0
        stamp = _stamp_of(runtime)
        assert (stamp["description"], stamp["as_of"], stamp["as_of_source"]) == (
            "pinned", HEAD_COMMIT_Z, "head-commit",
        )

    def test_the_flag_wins_over_the_history_and_is_written_in_utc(self, runtime):
        runtime.made = HistoryForge(
            diff=APP_DIFF, history=_history(first_review_at=CREATED, head_committed_at=CREATED),
        )
        assert main(["review", "--pr-url", URL, "--as-of", "2026-03-03T11:00:00+02:00"]) == 0
        stamp = _stamp_of(runtime)
        assert (stamp["description"], stamp["as_of"], stamp["as_of_source"]) == ("pinned", REVIEW_Z, "flag")

    def test_a_history_that_does_not_reach_the_cutoff_is_live_with_it(self, runtime):
        history = _history(description_versions=(V2,), complete=False, first_review_at=REVIEW)
        runtime.made = HistoryForge(diff=APP_DIFF, history=history)
        assert main(["review", "--pr-url", URL, "--no-threads"]) == 0
        stamp = _stamp_of(runtime)
        assert (stamp["description"], stamp["as_of"], stamp["as_of_source"]) == ("live", REVIEW_Z, "first-review")

    def test_a_forge_without_history_is_live_without_a_cutoff(self, runtime):
        assert main(["review", "--pr-url", URL, "--no-threads"]) == 0
        stamp = _stamp_of(runtime)
        assert list(stamp) == KEYS
        assert (stamp["description"], stamp["as_of"], stamp["as_of_source"]) == ("live", None, None)

    def test_description_file_with_pr_url_is_file(self, runtime, description_file):
        assert main(["review", "--pr-url", URL, "--description-file", description_file]) == 0
        stamp = _stamp_of(runtime)
        assert isinstance(runtime.orchestrate[0]["forge"], ReplayForge)
        assert (stamp["description"], stamp["as_of"], stamp["as_of_source"]) == ("file", None, None)

    def test_no_description_with_pr_url_is_none(self, runtime):
        assert main(["review", "--pr-url", URL, "--no-description"]) == 0
        stamp = _stamp_of(runtime)
        assert (stamp["description"], stamp["as_of"], stamp["as_of_source"]) == ("none", None, None)

    @pytest.mark.parametrize(("extra", "expected"), [
        ([], "file"),
        (["--description-file", "description.md"], "file"),
        (["--no-description"], "none"),
    ], ids=["diff-file-alone", "description-file", "no-description"])
    def test_the_local_path_reads_the_flags(self, runtime, diff_file, description_file, extra, expected):
        assert main(["review", "--diff-file", diff_file, *extra]) == 0
        assert isinstance(runtime.orchestrate[0]["forge"], LocalDiffForge)
        stamp = _stamp_of(runtime)
        assert list(stamp) == KEYS
        assert stamp == {
            "base_sha": None, "head_sha": None, "threads": "hidden", "diff_file": diff_file,
            "description": expected, "as_of": None, "as_of_source": None,
        }

    def test_a_recorder_returning_a_magicmock_is_treated_as_no_pin(self, runtime, monkeypatch):
        monkeypatch.setattr(cli, "_replay_forge", lambda forge, ref, replay: MagicMock(name="ReplayForge"))
        assert main(["review", "--pr-url", URL, "--no-threads"]) == 0
        stamp = _stamp_of(runtime)
        assert stamp["description"] == "live"
        assert stamp["as_of"] is None
        assert stamp["as_of_source"] is None

    def test_a_normal_run_has_no_stamp(self, runtime):
        assert main(["review", "--pr-url", URL]) == 0
        assert runtime.orchestrate[0]["replay"] is None


class TestTextLine:
    BASE_LINE = f"replay: base={BASE[:12]} head={HEAD[:12]} threads=hidden diff_file=-"

    def _stamp(self, description, as_of=None, source=None) -> dict:
        return {
            "base_sha": BASE, "head_sha": HEAD, "threads": "hidden", "diff_file": None,
            "description": description, "as_of": as_of, "as_of_source": source,
        }

    def test_a_pinned_line_carries_the_cutoff_and_its_source(self):
        assert _line(self._stamp("pinned", REVIEW_Z, "first-review")) == (
            f"{self.BASE_LINE} description=pinned as_of={REVIEW_Z} (first-review)"
        )

    def test_a_live_line_with_a_cutoff_carries_it(self):
        assert _line(self._stamp("live", REVIEW_Z, "flag")) == (
            f"{self.BASE_LINE} description=live as_of={REVIEW_Z} (flag)"
        )

    @pytest.mark.parametrize("description", ["live", "file", "none"])
    def test_without_a_cutoff_the_line_ends_at_the_status(self, description):
        assert _line(self._stamp(description)) == f"{self.BASE_LINE} description={description}"

    def test_a_stamp_without_the_description_keys_prints_a_dash(self):
        stamp = {"base_sha": BASE, "head_sha": HEAD, "threads": "hidden", "diff_file": None}
        assert _line(stamp) == f"{self.BASE_LINE} description=-"

    def test_through_the_cli(self, runtime, capsys):
        runtime.made = HistoryForge(diff=APP_DIFF, history=_history(first_review_at=REVIEW))
        assert main(["review", "--pr-url", URL, "--no-threads"]) == 0
        out = capsys.readouterr().out
        assert f"threads=hidden diff_file=- description=pinned as_of={REVIEW_Z} (first-review)\n" in out


class TestJsonStamp:
    def test_build_json_result_forwards_all_seven_keys_in_order(self):
        stamp = cli._ReplayRequest(base_sha=BASE, head_sha=HEAD, no_threads=True).stamp(
            has_forge=True, pin=DescriptionPin("pinned", REVIEW, "first-review"),
        )
        payload = cli._build_json_result({"verdict": "Approved", "replay": stamp})
        assert list(payload["replay"]) == KEYS
        assert json.loads(json.dumps(payload))["replay"] == {
            "base_sha": BASE, "head_sha": HEAD, "threads": "hidden", "diff_file": None,
            "description": "pinned", "as_of": REVIEW_Z, "as_of_source": "first-review",
        }

    def test_nulls_are_json_null(self):
        stamp = cli._ReplayRequest(diff_file="case.patch").stamp(has_forge=False)
        text = json.dumps(cli._build_json_result({"verdict": "Approved", "replay": stamp})["replay"])
        assert text.endswith('"description": "file", "as_of": null, "as_of_source": null}')

    def test_through_the_cli(self, runtime, capsys):
        runtime.made = HistoryForge(diff=APP_DIFF, history=_history(head_committed_at=HEAD_COMMIT))
        assert main(["review", "--pr-url", URL, "--no-threads", "--format", "json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert list(payload["replay"]) == KEYS
        assert payload["replay"]["description"] == "pinned"
        assert payload["replay"]["as_of"] == HEAD_COMMIT_Z
        assert payload["replay"]["as_of_source"] == "head-commit"
        assert detect_forge(URL) == runtime.orchestrate[0]["ref"]
