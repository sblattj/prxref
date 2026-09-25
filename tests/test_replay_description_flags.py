"""The replay description flags through the CLI (issue #16): parser, request, validation.

``--as-of TIME``, ``--description-file PATH`` and ``--no-description`` choose
which PR description a replay shows. These tests pin what ``prxref.cli`` does
with them before any history is read: each flag alone makes the run a replay
whose ``_ReplayRequest`` carries it; they are mutually exclusive; every bad
value exits 2 naming its flag before the URL is parsed and before any forge
or LLM client exists; and a run without them is exactly what it was. How the
request is resolved into a pinned title and description (``_replay_forge``)
and how it is stamped belong to later tasks, so ``_replay_forge`` is replaced
by a recorder here and the stamp is not asserted.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from prxref import cli
from prxref.cli import main
from prxref.forges.base import detect_forge
from prxref.forges.replay import LocalDiffForge
from prxref.llm import ConfigError
from tests.test_orchestrator import FakeForge, _added_file_diff

URL = "https://github.com/acme/widget/pull/7"
BASE = "d" * 40
HEAD = "c" * 40
APP_DIFF = _added_file_diff("src/app.py", 12)
FILE_DIFF = _added_file_diff("src/from_file.py", 5)
POSTING_OFF = "replay run: posting to the forge is disabled"
AS_OF = "2026-05-01T09:30:00Z"
AS_OF_UTC = datetime(2026, 5, 1, 9, 30, tzinfo=UTC)
EXCLUSIVE = "cannot be combined (give at most one of --as-of, --description-file and --no-description)"
NOT_ISO = "--as-of: must be an ISO-8601 time with a UTC offset, such as '2026-05-01T09:30:00Z', got {!r}"


def _install_fake_module(monkeypatch, fullname: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(fullname)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, fullname, mod)
    return mod


@pytest.fixture
def runtime(monkeypatch):
    """Doubles for everything past config. ``detect_forge`` is the real parser
    behind a call counter, ``make_forge`` hands back ``rec.made``, and
    ``_replay_forge`` records the ``_ReplayRequest`` it is handed and returns
    ``rec.wrapped``, so these tests observe the request exactly as the
    resolution step will receive it."""
    rec = types.SimpleNamespace(
        orchestrate=[], llm=[], forge=[], detect=[], replayed=[],
        made=FakeForge(diff=APP_DIFF), wrapped=MagicMock(name="ReplayForge"),
    )

    def fake_orchestrate_review(**kwargs):
        rec.orchestrate.append(kwargs)
        return {"verdict": "Approved", "findings_active": [], "findings_dropped": []}

    def fake_create_llm_client(cfg):
        rec.llm.append(cfg)
        return MagicMock(name="LLMClient")

    def spy_make_forge(ref):
        rec.forge.append(ref)
        return rec.made

    def spy_detect_forge(url):
        rec.detect.append(url)
        return detect_forge(url)

    def spy_replay_forge(forge, ref, replay):
        rec.replayed.append(replay)
        return rec.wrapped

    monkeypatch.setattr("prxref.cli.make_forge", spy_make_forge)
    monkeypatch.setattr("prxref.cli.detect_forge", spy_detect_forge)
    monkeypatch.setattr("prxref.cli._replay_forge", spy_replay_forge)
    _install_fake_module(
        monkeypatch, "prxref.llm_backends", create_llm_client=fake_create_llm_client,
    )
    _install_fake_module(
        monkeypatch, "prxref.orchestrator", orchestrate_review=fake_orchestrate_review,
    )
    return rec


@pytest.fixture
def desc_file(tmp_path) -> str:
    path = tmp_path / "description.md"
    path.write_text("The original description.\n", encoding="utf-8")
    return str(path)


@pytest.fixture
def diff_file(tmp_path) -> str:
    path = tmp_path / "case.patch"
    path.write_text(FILE_DIFF, encoding="utf-8")
    return str(path)


def _assert_nothing_ran(rec) -> None:
    """A configuration error exits before the URL is parsed and before the
    forge or the LLM client exists."""
    assert rec.detect == []
    assert rec.forge == []
    assert rec.llm == []
    assert rec.orchestrate == []


def _exit_2(runtime, capsys, args: list[str]) -> str:
    assert main(["review", *args]) == 2
    err = capsys.readouterr().err
    _assert_nothing_ran(runtime)
    return err


class TestParser:
    def test_the_flags_default_to_off(self):
        args = cli._build_parser().parse_args(["review", "--pr-url", URL])
        assert args.as_of is None
        assert args.description_file is None
        assert args.no_description is False

    def test_the_flags_parse_as_typed(self):
        args = cli._build_parser().parse_args([
            "review", "--as-of", AS_OF, "--description-file", "d.md", "--no-description",
        ])
        assert (args.as_of, args.description_file, args.no_description) == (AS_OF, "d.md", True)


class TestEachFlagMakesAReplay:
    """One flag alone is a replay, and the request carries exactly that flag."""

    @pytest.mark.parametrize(("kwargs", "expected"), [
        pytest.param({"as_of": AS_OF}, cli._ReplayRequest(as_of=AS_OF_UTC), id="as-of"),
        pytest.param(
            {"description_file": "d.md"}, cli._ReplayRequest(description_file="d.md"),
            id="description-file",
        ),
        pytest.param({"no_description": True}, cli._ReplayRequest(no_description=True), id="no-description"),
    ])
    def test_the_resolver_returns_a_request_carrying_the_flag(self, kwargs, expected):
        req = cli._resolve_replay(URL, **kwargs)
        assert req == expected
        assert req.description_text is None

    @pytest.mark.parametrize("kwargs", [
        {"description_file": "d.md"}, {"no_description": True},
    ], ids=["description-file", "no-description"])
    def test_description_flags_need_no_pr_url_with_a_diff_file(self, kwargs):
        req = cli._resolve_replay(None, diff_file="case.patch", **kwargs)
        assert req == cli._ReplayRequest(diff_file="case.patch", **kwargs)

    def test_the_resolver_stays_pure(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        req = cli._resolve_replay(URL, description_file="nowhere.md")
        assert req == cli._ReplayRequest(description_file="nowhere.md")
        assert list(tmp_path.iterdir()) == []

    def test_as_of_rides_along_with_the_other_replay_flags(self):
        req = cli._resolve_replay(
            URL, base_sha=BASE, head_sha=HEAD, no_threads=True, as_of=AS_OF,
        )
        assert req == cli._ReplayRequest(
            base_sha=BASE, head_sha=HEAD, no_threads=True, as_of=AS_OF_UTC,
        )

    @pytest.mark.parametrize("args", [
        ["--as-of", AS_OF], ["--description-file", "{desc}"], ["--no-description"],
    ], ids=["as-of", "description-file", "no-description"])
    def test_each_flag_alone_turns_posting_off_and_wraps_the_forge(
        self, runtime, caplog, desc_file, args,
    ):
        args = [desc_file if a == "{desc}" else a for a in args]
        with caplog.at_level(logging.INFO, logger="prxref"):
            assert main(["review", "--pr-url", URL, *args]) == 0
        call = runtime.orchestrate[0]
        assert call["post"] is False
        assert POSTING_OFF in caplog.text
        assert call["forge"] is runtime.wrapped
        assert call["replay"] is not None
        assert len(runtime.replayed) == 1

    def test_the_as_of_request_reaches_the_resolution_step(self, runtime):
        assert main(["review", "--pr-url", URL, "--as-of", "2026-05-01T11:30:00+02:00"]) == 0
        (req,) = runtime.replayed
        assert req == cli._ReplayRequest(as_of=AS_OF_UTC)

    def test_the_no_description_request_reaches_the_resolution_step(self, runtime):
        assert main(["review", "--pr-url", URL, "--no-description"]) == 0
        (req,) = runtime.replayed
        assert req == cli._ReplayRequest(no_description=True)

    def test_the_description_file_is_read_into_the_request(self, runtime, tmp_path):
        path = tmp_path / "pr.md"
        path.write_bytes(b"\xef\xbb\xbfFixes the parser.\r\n\r\nSee #12.\r")
        assert main(["review", "--pr-url", URL, "--description-file", str(path)]) == 0
        (req,) = runtime.replayed
        assert req == cli._ReplayRequest(
            description_file=str(path), description_text="Fixes the parser.\n\nSee #12.\n",
        )

    def test_the_description_file_is_kept_as_typed(self, runtime, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        Path("cases").mkdir()
        Path("cases/pr.md").write_text("text\n", encoding="utf-8")
        assert main(["review", "--pr-url", URL, "--description-file", "cases/pr.md"]) == 0
        assert runtime.replayed[0].description_file == "cases/pr.md"

    def test_a_blank_description_file_is_an_empty_description(self, runtime, tmp_path):
        path = tmp_path / "blank.md"
        path.write_bytes(b"")
        assert main(["review", "--pr-url", URL, "--description-file", str(path)]) == 0
        assert runtime.replayed[0].description_text == ""

    @pytest.mark.parametrize("args", [
        ["--description-file", "{desc}"], ["--no-description"],
    ], ids=["description-file", "no-description"])
    def test_a_lone_diff_file_accepts_the_description_flags(
        self, runtime, diff_file, desc_file, args,
    ):
        args = [desc_file if a == "{desc}" else a for a in args]
        assert main(["review", "--diff-file", diff_file, *args]) == 0
        call = runtime.orchestrate[0]
        assert isinstance(call["forge"], LocalDiffForge)
        assert call["post"] is False
        assert call["replay"] is not None
        assert runtime.detect == [] and runtime.forge == [] and runtime.replayed == []


class TestMutualExclusion:
    """Two or more of the three exit 2 naming every one given, whatever the argv order."""

    @pytest.mark.parametrize(("args", "named"), [
        pytest.param(["--as-of", AS_OF, "--description-file", "{desc}"], "--as-of/--description-file",
                     id="as-of+description-file"),
        pytest.param(["--as-of", AS_OF, "--no-description"], "--as-of/--no-description",
                     id="as-of+no-description"),
        pytest.param(["--description-file", "{desc}", "--no-description"], "--description-file/--no-description",
                     id="description-file+no-description"),
        pytest.param(["--no-description", "--description-file", "{desc}"], "--description-file/--no-description",
                     id="reversed-argv"),
        pytest.param(["--no-description", "--description-file", "{desc}", "--as-of", AS_OF],
                     "--as-of/--description-file/--no-description", id="all-three"),
    ])
    def test_a_pair_exits_2_naming_both(self, runtime, capsys, desc_file, args, named):
        args = [desc_file if a == "{desc}" else a for a in args]
        err = _exit_2(runtime, capsys, ["--pr-url", URL, *args])
        assert err == f"configuration error: {named}: {EXCLUSIVE}\n"

    def test_the_conflict_is_reported_before_a_bad_as_of(self, runtime, capsys):
        err = _exit_2(runtime, capsys, ["--pr-url", URL, "--as-of", "yesterday", "--no-description"])
        assert err == f"configuration error: --as-of/--no-description: {EXCLUSIVE}\n"

    def test_the_conflict_is_reported_before_the_file_is_read(self, runtime, capsys, tmp_path):
        missing = str(tmp_path / "absent.md")
        err = _exit_2(runtime, capsys, ["--pr-url", URL, "--description-file", missing, "--no-description"])
        assert err == f"configuration error: --description-file/--no-description: {EXCLUSIVE}\n"

    def test_the_resolver_refuses_a_pair_for_direct_callers(self):
        with pytest.raises(ConfigError, match="^--as-of/--no-description: cannot be combined"):
            cli._resolve_replay(URL, as_of=AS_OF, no_description=True)

    def test_an_empty_description_file_value_still_counts_as_given(self, runtime, capsys):
        err = _exit_2(runtime, capsys, ["--pr-url", URL, "--description-file", "", "--no-description"])
        assert err == f"configuration error: --description-file/--no-description: {EXCLUSIVE}\n"


class TestAsOf:
    """``--as-of`` must be an ISO-8601 time WITH a UTC offset; it is stored in UTC.

    A date alone and a time with no offset are refused (map-16: "a naive time
    or bad ISO exits 2"), never read in the host's time zone or at an assumed
    hour, so one replay command gives one cutoff on every machine.
    """

    @pytest.mark.parametrize("value", [
        "yesterday", "", "2026-05", "2026-05-01T24:00:00Z", "2026-05-01T09:30:00z",
        " 2026-05-01T09:30:00Z", "2026-13-01T00:00:00Z",
    ])
    def test_a_value_that_is_not_iso_8601_exits_2(self, runtime, capsys, value):
        err = _exit_2(runtime, capsys, ["--pr-url", URL, "--as-of", value])
        assert err == f"configuration error: {NOT_ISO.format(value)}\n"

    @pytest.mark.parametrize(("value", "day"), [
        ("2026-05-01", "2026-05-01"), ("20260501", "2026-05-01"), ("2026-W18-5", "2026-05-01"),
    ])
    def test_a_date_alone_exits_2(self, runtime, capsys, value, day):
        err = _exit_2(runtime, capsys, ["--pr-url", URL, "--as-of", value])
        assert err == (
            f"configuration error: --as-of: {value!r} is a date without a time; give a time "
            f"with a UTC offset, such as '{day}T00:00:00Z'\n"
        )

    @pytest.mark.parametrize("value", [
        "2026-05-01T09:30:00", "2026-05-01 09:30", "2026-05-01T09:30:00.250",
    ])
    def test_a_time_without_an_offset_exits_2(self, runtime, capsys, value):
        err = _exit_2(runtime, capsys, ["--pr-url", URL, "--as-of", value])
        assert err == (
            f"configuration error: --as-of: {value!r} has no UTC offset; add one, such as "
            "'Z' for UTC or '+02:00'\n"
        )

    @pytest.mark.parametrize("value", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
    def test_a_time_outside_the_utc_range_exits_2(self, runtime, capsys, value):
        err = _exit_2(runtime, capsys, ["--pr-url", URL, "--as-of", value])
        assert err == f"configuration error: --as-of: {value!r} is out of range in UTC\n"

    @pytest.mark.parametrize(("value", "expected"), [
        (AS_OF, AS_OF_UTC),
        ("2026-05-01T09:30Z", AS_OF_UTC),
        ("2026-05-01T11:30:00+02:00", AS_OF_UTC),
        ("2026-05-01T04:30:00-05:00", AS_OF_UTC),
        ("2026-05-01 09:30:00+00:00", AS_OF_UTC),
        ("2026-05-01T09:30:00.123456Z", AS_OF_UTC.replace(microsecond=123456)),
    ])
    def test_an_offset_time_is_accepted_and_normalised_to_utc(self, value, expected):
        req = cli._resolve_replay(URL, as_of=value)
        assert req.as_of == expected
        assert req.as_of.tzinfo is UTC
        assert req.as_of.utcoffset() == timedelta(0)

    def test_the_parsed_time_is_the_same_instant_as_typed(self):
        typed = datetime.fromisoformat("2026-05-01T23:30:00-03:00")
        assert cli._resolve_replay(URL, as_of="2026-05-01T23:30:00-03:00").as_of == typed
        assert cli._resolve_replay(URL, as_of="2026-05-01T23:30:00-03:00").as_of.day == 2

    def test_as_of_needs_a_pr_url(self, runtime, capsys, diff_file):
        err = _exit_2(runtime, capsys, ["--diff-file", diff_file, "--as-of", AS_OF])
        assert err == (
            "configuration error: --as-of: needs --pr-url (the description history is "
            "read from that PR)\n"
        )

    def test_a_bad_value_is_reported_before_a_missing_pr_url(self, runtime, capsys, diff_file):
        err = _exit_2(runtime, capsys, ["--diff-file", diff_file, "--as-of", "2026-05-01"])
        assert "is a date without a time" in err

    def test_a_bad_as_of_beats_an_unrecognized_url(self, runtime, capsys):
        url = "https://example.com/not/a/pr"
        assert detect_forge(url) is None
        err = _exit_2(runtime, capsys, ["--pr-url", url, "--as-of", "2026-05-01T09:30:00"])
        assert "--as-of:" in err and "has no UTC offset" in err

    def test_the_sha_checks_still_come_first(self, runtime, capsys):
        err = _exit_2(runtime, capsys, ["--pr-url", URL, "--base-sha", BASE, "--as-of", "nope"])
        assert err == (
            "configuration error: --base-sha/--head-sha: must be given together "
            "(got only --base-sha)\n"
        )


class TestDescriptionFile:
    """The file is read like the rules file; every failure exits 2 naming the flag."""

    def test_a_missing_file_exits_2(self, runtime, capsys, tmp_path):
        missing = str(tmp_path / "absent.md")
        err = _exit_2(runtime, capsys, ["--pr-url", URL, "--description-file", missing])
        assert err == (
            f"configuration error: --description-file: cannot read {missing!r}: "
            "No such file or directory\n"
        )

    def test_an_empty_path_exits_2(self, runtime, capsys):
        err = _exit_2(runtime, capsys, ["--pr-url", URL, "--description-file", ""])
        assert err == "configuration error: --description-file: cannot read '': No such file or directory\n"

    def test_a_directory_exits_2(self, runtime, capsys, tmp_path):
        err = _exit_2(runtime, capsys, ["--pr-url", URL, "--description-file", str(tmp_path)])
        assert err == f"configuration error: --description-file: cannot read {str(tmp_path)!r}: Is a directory\n"

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")
    def test_a_fifo_is_refused_without_blocking(self, runtime, capsys, tmp_path):
        fifo = tmp_path / "pipe.md"
        os.mkfifo(fifo)
        err = _exit_2(runtime, capsys, ["--pr-url", URL, "--description-file", str(fifo)])
        assert err == f"configuration error: --description-file: cannot read {str(fifo)!r}: not a regular file\n"

    @pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions as non-root")
    def test_an_unreadable_file_exits_2(self, runtime, capsys, tmp_path):
        path = tmp_path / "locked.md"
        path.write_text("secret\n", encoding="utf-8")
        path.chmod(0)
        try:
            err = _exit_2(runtime, capsys, ["--pr-url", URL, "--description-file", str(path)])
        finally:
            path.chmod(0o600)
        assert err == f"configuration error: --description-file: cannot read {str(path)!r}: Permission denied\n"

    @pytest.mark.parametrize(("raw", "byte"), [
        (b"caf\xe9 au lait\n", 3),
        (b"\xef\xbb\xbfcaf\xe9 au lait\n", 6),
    ], ids=["latin-1", "latin-1-after-bom"])
    def test_a_file_that_is_not_utf_8_exits_2(self, runtime, capsys, tmp_path, raw, byte):
        path = tmp_path / "latin1.md"
        path.write_bytes(raw)
        err = _exit_2(runtime, capsys, ["--pr-url", URL, "--description-file", str(path)])
        assert err == (
            f"configuration error: --description-file: {str(path)!r} is not UTF-8 text "
            f"(invalid continuation byte at byte {byte})\n"
        )

    def test_a_file_with_nul_bytes_exits_2(self, runtime, capsys, tmp_path):
        path = tmp_path / "binary.md"
        path.write_bytes(b"PNG\x00\x00\x01 header\n")
        err = _exit_2(runtime, capsys, ["--pr-url", URL, "--description-file", str(path)])
        assert err == (
            f"configuration error: --description-file: {str(path)!r} contains NUL bytes; "
            "expected Markdown or plain text\n"
        )

    def test_a_symlink_out_of_the_working_directory_exits_2(self, runtime, capsys, tmp_path, monkeypatch):
        work = tmp_path / "checkout"
        work.mkdir()
        outside = tmp_path / "private.md"
        outside.write_text("not for the prompt\n", encoding="utf-8")
        (work / "PR.md").symlink_to(outside)
        monkeypatch.chdir(work)
        err = _exit_2(runtime, capsys, ["--pr-url", URL, "--description-file", "PR.md"])
        assert err == (
            "configuration error: --description-file: cannot read 'PR.md': "
            "resolves outside the working directory\n"
        )

    def test_a_symlink_inside_the_working_directory_is_read(self, runtime, tmp_path, monkeypatch):
        work = tmp_path / "checkout"
        (work / "docs").mkdir(parents=True)
        (work / "docs" / "real.md").write_text("inside\n", encoding="utf-8")
        (work / "PR.md").symlink_to(work / "docs" / "real.md")
        monkeypatch.chdir(work)
        assert main(["review", "--pr-url", URL, "--description-file", "PR.md"]) == 0
        assert runtime.replayed[0].description_text == "inside\n"

    def test_an_absolute_path_outside_the_working_directory_is_read(
        self, runtime, tmp_path, monkeypatch,
    ):
        work = tmp_path / "checkout"
        work.mkdir()
        outside = tmp_path / "ci-temp" / "description.md"
        outside.parent.mkdir()
        outside.write_text("from the CI step\n", encoding="utf-8")
        monkeypatch.chdir(work)
        assert main(["review", "--pr-url", URL, "--description-file", str(outside)]) == 0
        assert runtime.replayed[0].description_text == "from the CI step\n"

    def test_the_file_is_read_before_the_url_is_parsed(self, runtime, capsys, tmp_path):
        url = "https://example.com/not/a/pr"
        assert detect_forge(url) is None
        err = _exit_2(runtime, capsys, ["--pr-url", url, "--description-file", str(tmp_path / "absent.md")])
        assert err.startswith("configuration error: --description-file: cannot read ")

    def test_the_diff_file_is_reported_first(self, runtime, capsys, tmp_path):
        err = _exit_2(runtime, capsys, [
            "--diff-file", str(tmp_path / "absent.patch"),
            "--description-file", str(tmp_path / "absent.md"),
        ])
        assert err.startswith("configuration error: --diff-file: cannot read ")

    def test_a_lone_diff_file_run_reads_it_too(self, runtime, capsys, diff_file, tmp_path):
        err = _exit_2(runtime, capsys, [
            "--diff-file", diff_file, "--description-file", str(tmp_path / "absent.md"),
        ])
        assert err.startswith("configuration error: --description-file: cannot read ")

    def test_the_reader_returns_the_decoded_text(self, tmp_path):
        path = tmp_path / "pr.md"
        path.write_bytes(b"line one\r\nline two\rline three\n")
        assert cli._read_description_file(str(path)) == "line one\nline two\nline three\n"


class TestNonReplayRunsAreUnchanged:
    """Without the new flags, the resolver returns what it returned in 0.14."""

    OLD_FIELDS = ["base_sha", "head_sha", "no_threads", "diff_file", "diff_text"]

    def test_no_replay_flag_is_still_none(self):
        assert cli._resolve_replay(URL) is None
        assert cli._resolve_replay(
            URL, as_of=None, description_file=None, no_description=False,
        ) is None

    @pytest.mark.parametrize(("kwargs", "expected"), [
        ({"no_threads": True}, cli._ReplayRequest(None, None, True, None)),
        ({"base_sha": BASE, "head_sha": HEAD}, cli._ReplayRequest(BASE, HEAD, False, None)),
        ({"diff_file": "case.patch"}, cli._ReplayRequest(None, None, False, "case.patch")),
    ], ids=["no-threads", "pinned", "diff-file"])
    def test_an_old_replay_request_is_the_same_request(self, kwargs, expected):
        req = cli._resolve_replay(URL, **kwargs)
        assert req == expected
        assert (req.as_of, req.description_file, req.description_text, req.no_description) == (
            None, None, None, False,
        )

    def test_the_old_fields_keep_their_positions(self):
        names = [f.name for f in dataclasses.fields(cli._ReplayRequest)]
        assert names[: len(self.OLD_FIELDS)] == self.OLD_FIELDS

    def test_a_normal_run_passes_replay_none_and_posts(self, runtime, caplog):
        with caplog.at_level(logging.INFO, logger="prxref"):
            assert main(["review", "--pr-url", URL]) == 0
        call = runtime.orchestrate[0]
        assert call["replay"] is None
        assert call["post"] is True
        assert call["forge"] is runtime.made
        assert runtime.replayed == []
        assert POSTING_OFF not in caplog.text

    def test_a_caller_that_passes_no_new_kwarg_is_not_a_replay(self, runtime):
        cli._run_review(URL, post=False)
        assert runtime.orchestrate[0]["replay"] is None
        assert runtime.replayed == []
