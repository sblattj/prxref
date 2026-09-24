"""Replay mode through the CLI (issue #65): validation, wiring, and the stamp.

``--base-sha`` / ``--head-sha`` pin a commit range of the ``--pr-url``
repository, ``--no-threads`` hides the PR's threads, and ``--diff-file``
reviews a diff on disk, with or without ``--pr-url``. These tests pin what
``prxref.cli`` does with them: every validation message (D65 §3, verbatim)
exits 2 before the URL is parsed and before any forge or LLM client exists;
any replay flag turns posting off; the ``--pr-url`` forge is wrapped in a
``ReplayForge`` and a lone ``--diff-file`` gets a ``LocalDiffForge``; the
orchestrator receives the stamp; and a run without replay flags is exactly
what it was. The forges themselves are pinned in tests/test_replay.py, and
the record's ``replay`` key on every exit in tests/test_run_record.py.
"""
from __future__ import annotations

import json
import logging
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from prxref import cli
from prxref.cli import main
from prxref.forges.base import detect_forge
from prxref.forges.replay import LocalDiffForge, ReplayForge
from tests.test_orchestrator import FakeForge, FakeLLM, _added_file_diff
from tests.test_replay import APP_DIFF, BASE, HEAD, RAW_OK, THREAD, RecordingForge

URL = "https://github.com/acme/widget/pull/7"
COMPARE_DIFF = _added_file_diff("src/app.py", 12)
FILE_DIFF = _added_file_diff("src/from_file.py", 5)
POSTING_OFF = "replay run: posting to the forge is disabled"
SHOWN_THREADS = "replay at pinned SHAs still shows the PR's CURRENT threads"
STALE_HEAD = "--diff-file with --pr-url and no --head-sha"
DISCUSSION = "- src/app.py: reviewer-bot: rename data before merging"


def _install_fake_module(monkeypatch, fullname: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(fullname)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, fullname, mod)
    return mod


@pytest.fixture
def runtime(monkeypatch):
    """Doubles for everything past config. ``detect_forge`` is the real parser
    behind a call counter, ``make_forge`` hands back ``rec.made`` (a forge with
    ``get_compare_diff`` unless a test swaps it), and the orchestrator echoes
    the stamp into its result the way the real one does."""
    rec = types.SimpleNamespace(
        orchestrate=[], llm=[], forge=[], detect=[],
        made=RecordingForge(diff=APP_DIFF, compare=COMPARE_DIFF),
    )

    def fake_orchestrate_review(**kwargs):
        rec.orchestrate.append(kwargs)
        result = {"verdict": "Approved", "findings_active": [], "findings_dropped": []}
        if kwargs["replay"] is not None:
            result["replay"] = dict(kwargs["replay"])
        return result

    def fake_create_llm_client(cfg):
        rec.llm.append(cfg)
        return MagicMock(name="LLMClient")

    def spy_make_forge(ref):
        rec.forge.append(ref)
        return rec.made

    def spy_detect_forge(url):
        rec.detect.append(url)
        return detect_forge(url)

    monkeypatch.setattr("prxref.cli.make_forge", spy_make_forge)
    monkeypatch.setattr("prxref.cli.detect_forge", spy_detect_forge)
    _install_fake_module(
        monkeypatch, "prxref.llm_backends", create_llm_client=fake_create_llm_client,
    )
    _install_fake_module(
        monkeypatch, "prxref.orchestrator", orchestrate_review=fake_orchestrate_review,
    )
    return rec


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


def _stamp(base=None, head=None, threads="hidden", diff=None) -> dict:
    return {"base_sha": base, "head_sha": head, "threads": threads, "diff_file": diff}


class TestValidation:
    """Every D65 §3 message, verbatim, each exit 2 with nothing built."""

    NEED_URL = "--pr-url: required unless --diff-file is given"
    NOT_FULL = "must be a full 40- or 64-character hex commit SHA, got {!r} (resolve it with git rev-parse)"

    @pytest.mark.parametrize(("args", "message"), [
        pytest.param([], NEED_URL, id="no-input"),
        pytest.param(["--no-threads"], NEED_URL, id="no-threads-alone"),
        pytest.param(["--base-sha", BASE, "--head-sha", HEAD], NEED_URL, id="shas-alone"),
        pytest.param(
            ["--pr-url", URL, "--base-sha", BASE],
            "--base-sha/--head-sha: must be given together (got only --base-sha)",
            id="base-only",
        ),
        pytest.param(
            ["--pr-url", URL, "--head-sha", HEAD],
            "--base-sha/--head-sha: must be given together (got only --head-sha)",
            id="head-only",
        ),
        pytest.param(
            ["--pr-url", URL, "--base-sha", "abc123", "--head-sha", HEAD],
            "--base-sha: " + NOT_FULL.format("abc123"), id="abbreviated-base",
        ),
        pytest.param(
            ["--pr-url", URL, "--base-sha", BASE, "--head-sha", "c" * 39],
            "--head-sha: " + NOT_FULL.format("c" * 39), id="39-hex-head",
        ),
        pytest.param(
            ["--pr-url", URL, "--base-sha", "d" * 41, "--head-sha", HEAD],
            "--base-sha: " + NOT_FULL.format("d" * 41), id="41-hex-base",
        ),
        pytest.param(
            ["--pr-url", URL, "--base-sha", BASE, "--head-sha", "g" * 40],
            "--head-sha: " + NOT_FULL.format("g" * 40), id="non-hex-head",
        ),
        pytest.param(
            ["--pr-url", URL, "--base-sha", BASE + "\n", "--head-sha", HEAD],
            "--base-sha: " + NOT_FULL.format(BASE + "\n"), id="trailing-newline",
        ),
        pytest.param(
            ["--pr-url", URL, "--base-sha", "", "--head-sha", ""],
            "--base-sha: " + NOT_FULL.format(""), id="empty-shas",
        ),
        pytest.param(
            ["--pr-url", URL, "--base-sha", BASE, "--head-sha", BASE.upper()],
            "--base-sha/--head-sha: must name two different commits", id="equal-ignoring-case",
        ),
    ])
    def test_a_bad_flag_set_exits_2_naming_the_flag(self, runtime, capsys, args, message):
        assert main(["review", *args]) == 2
        assert capsys.readouterr().err == f"configuration error: {message}\n"
        _assert_nothing_ran(runtime)

    def test_neither_pr_url_nor_diff_file_exits_2_naming_both(self, runtime, capsys):
        assert main(["review", "--no-post"]) == 2
        err = capsys.readouterr().err
        assert "--pr-url" in err and "--diff-file" in err
        _assert_nothing_ran(runtime)

    def test_shas_without_pr_url_exit_2(self, runtime, capsys, diff_file):
        assert main(["review", "--diff-file", diff_file, "--base-sha", BASE, "--head-sha", HEAD]) == 2
        assert capsys.readouterr().err == (
            "configuration error: --base-sha/--head-sha: need --pr-url "
            "(the range is resolved in that PR's repository)\n"
        )
        _assert_nothing_ran(runtime)

    def test_a_malformed_sha_is_reported_before_a_missing_pr_url(self, runtime, capsys, diff_file):
        assert main(["review", "--diff-file", diff_file, "--base-sha", "abc", "--head-sha", "def"]) == 2
        assert capsys.readouterr().err == f"configuration error: --base-sha: {self.NOT_FULL.format('abc')}\n"
        _assert_nothing_ran(runtime)

    def test_missing_diff_file_exits_2_naming_the_flag(self, runtime, capsys, tmp_path):
        missing = str(tmp_path / "absent.patch")
        assert main(["review", "--diff-file", missing]) == 2
        assert capsys.readouterr().err == (
            f"configuration error: --diff-file: cannot read {missing!r}: No such file or directory\n"
        )
        _assert_nothing_ran(runtime)

    def test_directory_diff_file_exits_2(self, runtime, capsys, tmp_path):
        assert main(["review", "--pr-url", URL, "--diff-file", str(tmp_path)]) == 2
        assert capsys.readouterr().err == (
            f"configuration error: --diff-file: cannot read {str(tmp_path)!r}: Is a directory\n"
        )
        _assert_nothing_ran(runtime)

    def test_a_bad_flag_set_beats_an_unrecognized_url(self, runtime, capsys):
        url = "https://example.com/not/a/pr"
        assert detect_forge(url) is None
        assert main(["review", "--pr-url", url, "--base-sha", BASE, "--head-sha", BASE]) == 2
        assert "must name two different commits" in capsys.readouterr().err
        _assert_nothing_ran(runtime)

    def test_forge_without_get_compare_diff_exits_2(self, runtime, capsys):
        runtime.made = FakeForge(diff=APP_DIFF)
        assert not hasattr(runtime.made, "get_compare_diff")
        assert main(["review", "--pr-url", URL, "--base-sha", BASE, "--head-sha", HEAD]) == 2
        assert capsys.readouterr().err == (
            "configuration error: --base-sha/--head-sha: the github forge cannot fetch "
            "a pinned commit range\n"
        )
        assert len(runtime.forge) == 1
        assert runtime.llm == []
        assert runtime.orchestrate == []

    def test_a_diff_file_needs_no_compare_even_with_shas(self, runtime, diff_file):
        runtime.made = FakeForge(diff=APP_DIFF)
        assert main([
            "review", "--pr-url", URL, "--diff-file", diff_file,
            "--base-sha", BASE, "--head-sha", HEAD, "--no-threads",
        ]) == 0
        assert isinstance(runtime.orchestrate[0]["forge"], ReplayForge)

    def test_full_shas_are_accepted_and_lowercased(self, runtime):
        base, head = "E" * 64, "f" * 40
        assert main(["review", "--pr-url", URL, "--base-sha", base, "--head-sha", head.upper()]) == 0
        assert runtime.orchestrate[0]["replay"] == _stamp("e" * 64, head, "shown")
        runtime.orchestrate[0]["forge"].get_diff(detect_forge(URL))
        assert [(b, h) for _ref, b, h in runtime.made.compare_args] == [("e" * 64, head)]

    def test_the_resolver_is_pure(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        req = cli._resolve_replay(None, diff_file="nowhere.patch")
        assert req == cli._ReplayRequest(diff_file="nowhere.patch")
        assert req.diff_text is None
        assert list(tmp_path.iterdir()) == []


class TestWiring:
    """Which forge the orchestrator gets, what it is told, and when it may post."""

    def test_normal_run_passes_replay_none(self, runtime, caplog):
        with caplog.at_level(logging.INFO, logger="prxref"):
            assert main(["review", "--pr-url", URL]) == 0
        call = runtime.orchestrate[0]
        assert call["replay"] is None
        assert call["post"] is True
        assert call["forge"] is runtime.made
        assert POSTING_OFF not in caplog.text

    @pytest.mark.parametrize("args", [
        ["--no-threads"],
        ["--base-sha", BASE, "--head-sha", HEAD],
        ["--diff-file", "{diff}"],
    ], ids=["no-threads", "pinned", "diff-file"])
    def test_replay_forces_post_false_without_no_post(self, runtime, caplog, diff_file, args):
        args = [diff_file if a == "{diff}" else a for a in args]
        with caplog.at_level(logging.INFO, logger="prxref"):
            assert main(["review", "--pr-url", URL, *args]) == 0
        assert runtime.orchestrate[0]["post"] is False
        assert POSTING_OFF in caplog.text

    def test_replay_passes_stamp_to_orchestrate(self, runtime):
        assert main([
            "review", "--pr-url", URL, "--base-sha", BASE, "--head-sha", HEAD, "--no-threads",
        ]) == 0
        call = runtime.orchestrate[0]
        assert call["replay"] == _stamp(BASE, HEAD, "hidden")
        assert list(call["replay"]) == ["base_sha", "head_sha", "threads", "diff_file"]
        assert isinstance(call["forge"], ReplayForge)
        assert call["ref"] == detect_forge(URL)

    def test_pr_url_optional_with_diff_file(self, runtime, diff_file):
        assert main(["review", "--diff-file", diff_file]) == 0
        call = runtime.orchestrate[0]
        assert runtime.detect == []
        assert runtime.forge == []
        assert isinstance(call["forge"], LocalDiffForge)
        assert call["forge"].get_diff(call["ref"]) == FILE_DIFF
        assert call["ref"] == LocalDiffForge.ref_for(diff_file)
        assert call["replay"] == _stamp(diff=diff_file)
        assert call["post"] is False

    def test_the_diff_file_is_stamped_as_typed(self, runtime, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        Path("cases").mkdir()
        Path("cases/pr.patch").write_text(FILE_DIFF, encoding="utf-8")
        assert main(["review", "--diff-file", "cases/pr.patch"]) == 0
        call = runtime.orchestrate[0]
        assert call["replay"]["diff_file"] == "cases/pr.patch"
        assert call["ref"].url == (tmp_path / "cases/pr.patch").resolve().as_uri()

    @pytest.mark.parametrize(("args", "threads"), [
        (["--no-threads"], "hidden"),
        (["--base-sha", BASE, "--head-sha", HEAD], "shown"),
        (["--diff-file", "{diff}"], "shown"),
    ], ids=["no-threads", "pinned", "diff-file"])
    def test_a_pr_url_replay_reports_whether_threads_were_shown(
        self, runtime, diff_file, args, threads,
    ):
        args = [diff_file if a == "{diff}" else a for a in args]
        assert main(["review", "--pr-url", URL, *args]) == 0
        assert runtime.orchestrate[0]["replay"]["threads"] == threads

    def test_unrecognized_pr_url_with_diff_file_keeps_hint_and_exit_0(
        self, runtime, capsys, diff_file,
    ):
        url = "https://example.com/not/a/pr"
        assert main(["review", "--pr-url", url, "--diff-file", diff_file]) == 0
        assert f"unrecognized PR URL {url!r}" in capsys.readouterr().err
        assert runtime.detect == [url]
        assert runtime.orchestrate == []

    def test_pinned_replay_without_no_threads_warns(self, runtime, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref"):
            assert main(["review", "--pr-url", URL, "--base-sha", BASE, "--head-sha", HEAD]) == 0
        assert SHOWN_THREADS in caplog.text
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="prxref"):
            assert main([
                "review", "--pr-url", URL, "--base-sha", BASE, "--head-sha", HEAD, "--no-threads",
            ]) == 0
        assert caplog.text == ""

    def test_diff_file_with_pr_url_and_no_head_sha_warns(self, runtime, caplog, diff_file):
        with caplog.at_level(logging.WARNING, logger="prxref"):
            assert main(["review", "--pr-url", URL, "--diff-file", diff_file, "--no-threads"]) == 0
        assert STALE_HEAD in caplog.text
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="prxref"):
            assert main([
                "review", "--pr-url", URL, "--diff-file", diff_file,
                "--base-sha", BASE, "--head-sha", HEAD, "--no-threads",
            ]) == 0
        assert caplog.text == ""

    def test_replay_composes_with_spec_rules_and_context(self, runtime, tmp_path, diff_file):
        rules = tmp_path / "rules.md"
        rules.write_text("Every public function needs a docstring.\n", encoding="utf-8")
        ticket = tmp_path / "ticket.md"
        ticket.write_text("## Summary\nAdd the widget.\n", encoding="utf-8")
        spec = tmp_path / "spec.md"
        spec.write_text("The widget MUST validate its input.\n", encoding="utf-8")
        assert main([
            "review", "--diff-file", diff_file, "--spec", str(spec),
            "--rules-file", str(rules), "--context-file", str(ticket),
        ]) == 0
        call = runtime.orchestrate[0]
        assert call["replay"] == _stamp(diff=diff_file)
        assert call["spec_sources"] == [str(spec)]
        assert call["rules"] is not None and call["rules"].record()["path"] == str(rules)
        assert call["ticket"] is not None and call["ticket"].record()["path"] == str(ticket)

    def test_webhook_handler_run_is_never_a_replay(self, runtime):
        cli._webhook_handler(URL)
        call = runtime.orchestrate[0]
        assert call["replay"] is None
        assert call["post"] is True
        assert call["forge"] is runtime.made


class TestThroughTheRealOrchestrator:
    """The real orchestrator and reviewer behind the CLI; only the forge and
    the LLM are doubles."""

    @pytest.fixture
    def rig(self, monkeypatch):
        rig = types.SimpleNamespace(
            inner=RecordingForge(
                diff=APP_DIFF, compare=COMPARE_DIFF, threads=[THREAD],
                files={"src/app.py": "def helper():\n    return 1\n"},
            ),
            llm=FakeLLM(RAW_OK),
        )
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: rig.inner)
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: rig.llm)
        return rig

    def _json(self, capsys) -> dict:
        return json.loads(capsys.readouterr().out)

    def test_blind_pinned_replay_reads_at_head_hides_threads_and_writes_nothing(
        self, rig, capsys, tmp_path,
    ):
        assert main([
            "review", "--pr-url", URL, "--base-sha", BASE, "--head-sha", HEAD,
            "--no-threads", "--format", "json", "--trace-dir", str(tmp_path),
        ]) == 0
        payload = self._json(capsys)
        assert payload["replay"] == _stamp(BASE, HEAD, "hidden")
        assert payload["posted"] is False
        assert payload["verdict"] == "Approved"
        assert [(b, h) for _ref, b, h in rig.inner.compare_args] == [(BASE, HEAD)]
        assert "get_diff" not in rig.inner.calls
        assert "list_threads" not in rig.inner.calls
        assert {sha for _path, sha in rig.inner.reads} == {HEAD}
        assert rig.inner.summaries == [] and rig.inner.inline_batches == []
        assert rig.inner.pruned == 0
        assert "### Existing discussion" not in (tmp_path / "sweep.user.md").read_text()

    def test_pinned_replay_with_threads_shows_them(self, rig, capsys, tmp_path):
        assert main([
            "review", "--pr-url", URL, "--base-sha", BASE, "--head-sha", HEAD,
            "--format", "json", "--trace-dir", str(tmp_path),
        ]) == 0
        assert self._json(capsys)["replay"]["threads"] == "shown"
        assert "list_threads" in rig.inner.calls
        assert DISCUSSION in (tmp_path / "sweep.user.md").read_text()

    def test_json_payload_forwards_replay(self, rig, capsys, diff_file):
        assert main(["review", "--diff-file", diff_file, "--format", "json"]) == 0
        payload = self._json(capsys)
        assert payload["replay"] == _stamp(diff=diff_file)
        assert payload["chunks_reviewed"] >= 1 and payload["chunks_failed"] == 0
        assert rig.inner.calls == []

    def test_json_payload_omits_replay_on_normal_run(self, rig, capsys, diff_file):
        assert main(["review", "--pr-url", URL, "--no-post", "--format", "json"]) == 0
        normal = self._json(capsys)
        assert "replay" not in normal
        assert main(["review", "--pr-url", URL, "--no-threads", "--format", "json"]) == 0
        replayed = self._json(capsys)
        assert set(replayed) == set(normal) | {"replay"}

    def test_text_summary_prints_replay_line(self, rig, capsys, diff_file):
        assert main(["review", "--diff-file", diff_file]) == 0
        out = capsys.readouterr().out
        assert f"replay: base=- head=- threads=hidden diff_file={diff_file}\n" in out
        assert main(["review", "--pr-url", URL, "--no-post"]) == 0
        assert "replay:" not in capsys.readouterr().out

    @pytest.mark.parametrize("content", ["", "\n  \n"], ids=["empty", "whitespace"])
    def test_blank_diff_file_is_an_error_run_exit_0_with_stamp(self, rig, capsys, tmp_path, content):
        blank = tmp_path / "blank.patch"
        blank.write_text(content, encoding="utf-8")
        assert main(["review", "--diff-file", str(blank), "--format", "json"]) == 0
        payload = self._json(capsys)
        assert payload["verdict"] == "Error"
        assert payload["replay"] == _stamp(diff=str(blank))
        assert rig.llm.calls == 0

    def test_empty_pinned_range_is_an_error_run_exit_0_with_stamp(self, rig, capsys):
        rig.inner.compare = ""
        assert main([
            "review", "--pr-url", URL, "--base-sha", BASE, "--head-sha", HEAD,
            "--no-threads", "--format", "json",
        ]) == 0
        payload = self._json(capsys)
        assert payload["verdict"] == "Error"
        assert payload["replay"] == _stamp(BASE, HEAD, "hidden")
        assert rig.inner.summaries == []
