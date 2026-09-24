"""Replay forges (issue #65): ``LocalDiffForge`` and ``ReplayForge``.

A replay reviews a fixed input — a commit range pinned by SHA, or a diff file
— and never writes to a forge. These tests pin the two forges on their own
(against recording fakes of the inner forge, never a real adapter) and
through a real ``orchestrate_review``: file reads land at the pinned head,
hidden threads never reach the sweep prompt, a blank replay diff is an
``Error`` run that still carries its stamp, and ``post=True`` writes nothing.

The run-record stamp itself (all seven exits, the ``run start`` event, the
JSON payload) is the foundation's and is pinned in tests/test_run_record.py;
the tests here pass a stamp only to show it survives a replay forge's error
exit.
"""
from __future__ import annotations

import dataclasses
import json
import logging

import pytest

from prxref import orchestrator
from prxref.forges.base import PRRef, Thread, detect_forge
from prxref.forges.replay import NEVER_POSTS, LocalDiffForge, ReplayForge, _patch_metadata
from prxref.orchestrator import orchestrate_review
from prxref.triage import parse_unified_diff
from tests.test_orchestrator import (
    HAPPY_FINDINGS,
    REF,
    FakeForge,
    FakeLLM,
    _added_file_diff,
)

BASE = "d" * 40
HEAD = "c" * 40
APP_DIFF = _added_file_diff("src/app.py", 20)
COMPARE_DIFF = _added_file_diff("src/pinned.py", 5)
FILE_DIFF = _added_file_diff("src/from_file.py", 5)
PINNED_STAMP = {
    "base_sha": BASE, "head_sha": HEAD, "threads": "hidden", "diff_file": None,
    "description": "pinned", "as_of": "2026-05-01T09:30:00Z", "as_of_source": "first-review",
}
LOCAL_PATH = "cases/empty.patch"
LOCAL_STAMP = {
    "base_sha": None, "head_sha": None, "threads": "hidden", "diff_file": LOCAL_PATH,
    "description": "file", "as_of": None, "as_of_source": None,
}
THREAD = Thread(
    path="src/app.py", line=3, resolved=False, author="reviewer-bot",
    body_snippet="rename data before merging",
)
RAW_OK = json.dumps({"findings": [], "escalations": []})

MODIFY_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,3 +1,3 @@\n"
    " import os\n"
    "-x = 1\n"
    "+x = 2\n"
    " print(x)\n"
)


def _mail(
    subject: str = "[PATCH] Fix the widget",
    sender: str = "Alice Example <alice@example.com>",
    body: str = "Explain why the widget broke.\n",
    headers: str = "",
    diff: str = MODIFY_DIFF,
) -> str:
    """One ``git format-patch`` mail: mbox line, headers, body, diffstat, diff, signature."""
    return (
        "From 0123456789abcdef0123456789abcdef01234567 Mon Sep 17 00:00:00 2001\n"
        f"From: {sender}\n"
        "Date: Tue, 1 Sep 2026 10:00:00 +0000\n"
        f"Subject: {subject}\n"
        f"{headers}"
        "\n"
        f"{body}"
        "---\n"
        " src/app.py | 2 +-\n"
        " 1 file changed, 1 insertion(+), 1 deletion(-)\n"
        "\n"
        f"{diff}"
        "-- \n"
        "2.43.0\n"
        "\n"
    )


class RecordingForge(FakeForge):
    """``FakeForge`` plus the optional reader, compare and prune, every call recorded."""

    def __init__(self, *, compare: str = "", files: dict[str, str] | None = None, **kw):
        super().__init__(**kw)
        self.compare = compare
        self.files = files or {}
        self.calls: list[str] = []
        self.compare_args: list[tuple[PRRef, str, str]] = []
        self.reads: list[tuple[str, str]] = []
        self.pruned = 0

    def get_diff(self, ref):
        self.calls.append("get_diff")
        return super().get_diff(ref)

    def list_threads(self, ref):
        self.calls.append("list_threads")
        return super().list_threads(ref)

    def get_compare_diff(self, ref, *, base_sha, head_sha):
        self.calls.append("get_compare_diff")
        self.compare_args.append((ref, base_sha, head_sha))
        return self.compare

    def get_file_content(self, ref, path, *, sha):
        self.reads.append((path, sha))
        return self.files.get(path)

    def prune_inline_comments(self, ref):
        self.pruned += 1
        return 0


def _wrote_nothing(inner: RecordingForge) -> bool:
    return inner.summaries == [] and inner.inline_batches == [] and inner.pruned == 0


class TestReplayForgePinning:
    def test_replay_get_pr_pins_source_and_target_sha(self):
        inner = RecordingForge()
        pr = ReplayForge(inner, base_sha=BASE, head_sha=HEAD).get_pr(REF)
        assert (pr.source_sha, pr.target_sha) == (HEAD, BASE)
        assert dataclasses.replace(pr, source_sha="a" * 40, target_sha="b" * 40) == inner.pr
        assert (inner.pr.source_sha, inner.pr.target_sha) == ("a" * 40, "b" * 40)

    def test_replay_get_pr_without_shas_is_the_inner_pr(self):
        inner = RecordingForge()
        assert ReplayForge(inner, hide_threads=True).get_pr(REF) == inner.pr

    def test_replay_diff_uses_compare_when_shas_given(self):
        inner = RecordingForge(diff=APP_DIFF, compare=COMPARE_DIFF)
        assert ReplayForge(inner, base_sha=BASE, head_sha=HEAD).get_diff(REF) == COMPARE_DIFF
        assert inner.compare_args == [(REF, BASE, HEAD)]
        assert "get_diff" not in inner.calls

    @pytest.mark.parametrize("pinned", [True, False], ids=["with-shas", "without-shas"])
    def test_replay_diff_file_wins_over_compare(self, pinned):
        inner = RecordingForge(diff=APP_DIFF, compare=COMPARE_DIFF)
        shas = {"base_sha": BASE, "head_sha": HEAD} if pinned else {}
        assert ReplayForge(inner, diff_text=FILE_DIFF, **shas).get_diff(REF) == FILE_DIFF
        assert inner.calls == []

    @pytest.mark.parametrize("live", [APP_DIFF, ""], ids=["diff", "empty"])
    def test_replay_live_diff_path_is_the_inner_get_diff(self, live):
        inner = RecordingForge(diff=live, compare=COMPARE_DIFF)
        assert ReplayForge(inner, hide_threads=True).get_diff(REF) == live
        assert inner.calls == ["get_diff"]

    @pytest.mark.parametrize("blank", ["", " \n\t\n"], ids=["empty", "whitespace"])
    def test_replay_blank_pinned_diff_raises_naming_the_range(self, blank):
        forge = ReplayForge(RecordingForge(compare=blank), base_sha=BASE, head_sha=HEAD)
        with pytest.raises(ValueError, match=rf"{BASE[:12]}\.\.\.{HEAD[:12]} is empty"):
            forge.get_diff(REF)

    @pytest.mark.parametrize("blank", ["", " \n\t\n"], ids=["empty", "whitespace"])
    def test_replay_blank_diff_text_raises(self, blank):
        forge = ReplayForge(RecordingForge(diff=APP_DIFF), diff_text=blank)
        with pytest.raises(ValueError, match="^replay diff from the --diff-file is empty$"):
            forge.get_diff(REF)

    @pytest.mark.parametrize(
        "shas", [{"base_sha": BASE}, {"head_sha": HEAD}], ids=["base-only", "head-only"],
    )
    def test_replay_rejects_a_lone_sha(self, shas):
        with pytest.raises(ValueError, match="given together"):
            ReplayForge(RecordingForge(), **shas)

    def test_replay_refuses_to_pin_a_forge_without_compare(self):
        with pytest.raises(ValueError, match="fake forge cannot fetch a pinned commit range"):
            ReplayForge(FakeForge(), base_sha=BASE, head_sha=HEAD)

    def test_replay_pins_a_forge_without_compare_when_the_diff_is_given(self):
        forge = ReplayForge(FakeForge(), base_sha=BASE, head_sha=HEAD, diff_text=FILE_DIFF)
        assert forge.get_diff(REF) == FILE_DIFF
        assert forge.get_pr(REF).source_sha == HEAD

    def test_neither_replay_forge_implements_compare(self):
        assert not hasattr(ReplayForge, "get_compare_diff")
        assert not hasattr(LocalDiffForge, "get_compare_diff")


class TestReplayForgeThreads:
    def test_replay_hide_threads_returns_empty_and_never_calls_inner(self):
        inner = RecordingForge(threads=[THREAD])
        assert ReplayForge(inner, hide_threads=True).list_threads(REF) == []
        assert inner.calls == []

    def test_replay_shown_threads_delegate_to_inner(self):
        inner = RecordingForge(threads=[THREAD])
        assert ReplayForge(inner, base_sha=BASE, head_sha=HEAD).list_threads(REF) == [THREAD]
        assert inner.calls == ["list_threads"]


class TestReplayForgeFileReads:
    def test_replay_file_read_delegates_to_inner(self):
        inner = RecordingForge(files={"src/app.py": "x = 1\n"})
        forge = ReplayForge(inner, base_sha=BASE, head_sha=HEAD)
        assert forge.get_file_content(REF, "src/app.py", sha=HEAD) == "x = 1\n"
        assert inner.reads == [("src/app.py", HEAD)]

    def test_replay_without_inner_reader_skips_context(self):
        forge = ReplayForge(FakeForge(), hide_threads=True)
        assert forge.get_file_content(REF, "src/app.py", sha=HEAD) is None

    def test_replay_file_read_that_raises_is_none(self):
        class Raising(RecordingForge):
            def get_file_content(self, ref, path, *, sha):
                raise RuntimeError("boom read")

        forge = ReplayForge(Raising(), hide_threads=True)
        assert forge.get_file_content(REF, "src/app.py", sha=HEAD) is None


WRITES = [
    ("post_summary", (REF, "body")),
    ("post_inline_comments", (REF, [])),
    ("prune_inline_comments", (REF,)),
]


class TestNeverPosts:
    @pytest.mark.parametrize(("method", "args"), WRITES, ids=[w[0] for w in WRITES])
    def test_replay_forge_never_posts(self, method, args):
        inner = RecordingForge()
        forge = ReplayForge(inner, hide_threads=True)
        with pytest.raises(RuntimeError, match=f"^{NEVER_POSTS}$"):
            getattr(forge, method)(*args)
        assert _wrote_nothing(inner)

    @pytest.mark.parametrize(("method", "args"), WRITES, ids=[w[0] for w in WRITES])
    def test_local_forge_never_posts(self, method, args):
        with pytest.raises(RuntimeError, match=f"^{NEVER_POSTS}$"):
            getattr(LocalDiffForge(APP_DIFF, path=LOCAL_PATH), method)(*args)


@pytest.mark.usefixtures("contract_stubs")
class TestReplayThroughTheOrchestrator:
    def test_replay_file_reads_happen_at_head_sha(self):
        files = {"src/app.py": "def helper():\n    return 1\n"}
        inner = RecordingForge(diff=APP_DIFF, compare=APP_DIFF, files=files)
        forge = ReplayForge(inner, base_sha=BASE, head_sha=HEAD, hide_threads=True)
        orchestrate_review(forge, REF, FakeLLM(), post=False)
        assert {sha for _, sha in inner.reads} == {HEAD}

        control = RecordingForge(diff=APP_DIFF, files=files)
        orchestrate_review(control, REF, FakeLLM(), post=False)
        assert {sha for _, sha in control.reads} == {"a" * 40}

    def test_replay_empty_pinned_diff_is_an_error_run_with_stamp(self, caplog):
        forge = ReplayForge(RecordingForge(compare=""), base_sha=BASE, head_sha=HEAD)
        llm = FakeLLM()
        with caplog.at_level(logging.ERROR, logger="prxref.orchestrator"):
            res = orchestrate_review(forge, REF, llm, post=False, replay=dict(PINNED_STAMP))
        assert res["verdict"] == "Error"
        assert res["replay"] == PINNED_STAMP
        assert llm.calls == 0
        assert "get_diff failed: replay diff from" in caplog.text

    @pytest.mark.parametrize("blank", ["", "\n  \n"], ids=["empty", "whitespace"])
    @pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "wrapped"])
    def test_local_forge_blank_diff_is_an_error_run_with_stamp(self, blank, wrapped):
        forge = LocalDiffForge(blank, path=LOCAL_PATH)
        if wrapped:
            forge = ReplayForge(forge, hide_threads=True)
        llm = FakeLLM()
        res = orchestrate_review(
            forge, LocalDiffForge.ref_for(LOCAL_PATH), llm, post=False,
            replay=dict(LOCAL_STAMP),
        )
        assert res["verdict"] == "Error"
        assert res["replay"] == LOCAL_STAMP
        assert llm.calls == 0

    @pytest.mark.parametrize("post_mode", ["summary+inline", "summary", "inline"])
    @pytest.mark.parametrize("compare", [APP_DIFF, ""], ids=["review", "error-notice"])
    def test_orchestrate_with_post_true_over_replay_forge_writes_nothing(
        self, post_mode, compare, caplog,
    ):
        inner = RecordingForge(diff=APP_DIFF, compare=compare, threads=[THREAD])
        forge = ReplayForge(inner, base_sha=BASE, head_sha=HEAD, hide_threads=True)
        with caplog.at_level(logging.WARNING, logger="prxref.orchestrator"):
            res = orchestrate_review(
                forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS),
                post=True, post_mode=post_mode,
            )
        assert res["posted"] is False
        assert _wrote_nothing(inner)
        if compare:
            assert len(res["findings_active"]) == 2
            assert NEVER_POSTS in caplog.text
        else:
            assert res["verdict"] == "Error"
        if "summary" in post_mode:
            assert NEVER_POSTS in caplog.text

    def test_orchestrate_with_post_true_over_local_forge_writes_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger="prxref.orchestrator"):
            res = orchestrate_review(
                LocalDiffForge(APP_DIFF, path=LOCAL_PATH), LocalDiffForge.ref_for(LOCAL_PATH),
                FakeLLM(findings_by_path=HAPPY_FINDINGS), post=True,
            )
        assert res["posted"] is False
        assert len(res["findings_active"]) == 2
        assert NEVER_POSTS in caplog.text

    def test_local_forge_reviews_a_format_patch(self):
        text = _mail()
        assert [(f.path, f.lines_added, f.lines_removed) for f in parse_unified_diff(text)] == [
            ("src/app.py", 1, 1),
        ]
        res = orchestrate_review(
            LocalDiffForge(text, path="cases/fix.patch"), LocalDiffForge.ref_for("cases/fix.patch"),
            FakeLLM(), post=False,
        )
        assert res["verdict"] == "Approved"
        assert res["chunks_reviewed"] == res["chunk_count"] == 2
        assert res["chunks_failed"] == 0


class TestReplayThreadsReachThePrompt:
    """The REAL reviewer renders the sweep prompt, so no contract stubs here."""

    @pytest.mark.parametrize("hide", [True, False], ids=["hidden", "shown"])
    def test_replay_hidden_threads_remove_the_existing_discussion_block(self, tmp_path, hide):
        inner = RecordingForge(diff=APP_DIFF, threads=[THREAD])
        forge = ReplayForge(inner, hide_threads=hide)
        res = orchestrate_review(forge, REF, FakeLLM(RAW_OK), post=False, trace_dir=str(tmp_path))
        assert res["chunks_failed"] == 0
        sweep = (tmp_path / "sweep.user.md").read_text(encoding="utf-8")
        line = "- src/app.py: reviewer-bot: rename data before merging"
        assert ("### Existing discussion" in sweep) is not hide
        assert (line in sweep) is not hide


class TestLocalDiffForge:
    def test_local_forge_has_no_threads_and_no_file_reader(self):
        forge = LocalDiffForge(APP_DIFF, path=LOCAL_PATH)
        ref = LocalDiffForge.ref_for(LOCAL_PATH)
        pr = forge.get_pr(ref)
        assert not hasattr(forge, "get_file_content")
        assert (pr.source_sha, pr.target_sha) == ("", "")
        assert forge.list_threads(ref) == []
        assert orchestrator._make_file_reader(forge, ref, pr) is None

    def test_local_forge_get_diff_is_the_text_unmodified(self):
        text = _mail()
        assert LocalDiffForge(text, path=LOCAL_PATH).get_diff(REF) == text

    @pytest.mark.parametrize("blank", ["", " \n\t\n"], ids=["empty", "whitespace"])
    def test_local_forge_blank_diff_raises(self, blank):
        with pytest.raises(ValueError, match="^replay diff from the --diff-file is empty$"):
            LocalDiffForge(blank, path=LOCAL_PATH).get_diff(REF)

    def test_local_forge_ref_for_is_the_resolved_file_uri(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ref = LocalDiffForge.ref_for("cases/fix.patch")
        assert (ref.forge, ref.host, ref.owner, ref.repo, ref.number) == ("local", "", "", "", 0)
        assert ref.url == (tmp_path / "cases" / "fix.patch").resolve().as_uri()
        assert LocalDiffForge.parse_pr_url(ref.url) is None
        assert detect_forge(ref.url) is None

    def test_local_forge_raw_keeps_the_path_as_typed(self):
        pr = LocalDiffForge(APP_DIFF, path="cases/../cases/fix.patch").get_pr(REF)
        assert pr.raw == {"diff_file": "cases/../cases/fix.patch"}
        assert (pr.source_branch, pr.target_branch) == ("", "")


class TestPatchMetadata:
    @pytest.mark.parametrize(
        ("subject", "title"),
        [
            ("[PATCH] Fix the widget", "Fix the widget"),
            ("[PATCH v2 3/7] Fix the widget", "Fix the widget"),
            ("[RFC PATCH 1/2] Fix the widget", "Fix the widget"),
            ("Fix the widget", "Fix the widget"),
            ("[docs] Fix the widget", "[docs] Fix the widget"),
        ],
    )
    def test_local_forge_title_from_format_patch_subject_strips_patch_prefix(self, subject, title):
        pr = LocalDiffForge(_mail(subject=subject), path=LOCAL_PATH).get_pr(REF)
        assert pr.title == title

    def test_local_forge_title_decodes_rfc2047_subject(self):
        subject = "[PATCH] =?UTF-8?q?Fix=20the=20w=C3=AFdget?= and a\n folded tail"
        title, _, _ = _patch_metadata(_mail(subject=subject), LOCAL_PATH)
        assert title == "Fix the wïdget and a folded tail"

    def test_local_forge_description_stops_at_diffstat_separator(self):
        body = "First paragraph.\n\n---- not the separator\nSecond paragraph.\n"
        _, description, _ = _patch_metadata(_mail(body=body), LOCAL_PATH)
        assert description == "First paragraph.\n\n---- not the separator\nSecond paragraph."

    def test_local_forge_description_keeps_an_8bit_body(self):
        headers = (
            "MIME-Version: 1.0\n"
            "Content-Type: text/plain; charset=UTF-8\n"
            "Content-Transfer-Encoding: 8bit\n"
        )
        _, description, _ = _patch_metadata(
            _mail(body="Handles the ümlaut.\n", headers=headers), LOCAL_PATH,
        )
        assert description == "Handles the ümlaut."

    def test_local_forge_description_decodes_quoted_printable(self):
        headers = (
            "MIME-Version: 1.0\n"
            "Content-Type: text/plain; charset=UTF-8\n"
            "Content-Transfer-Encoding: quoted-printable\n"
        )
        _, description, _ = _patch_metadata(
            _mail(body="Caf=C3=A9 fix.\n", headers=headers), LOCAL_PATH,
        )
        assert description == "Café fix."

    @pytest.mark.parametrize(
        ("sender", "author"),
        [
            ("Alice Example <alice@example.com>", "Alice Example"),
            ("alice@example.com", "alice@example.com"),
            ("=?UTF-8?q?J=C3=B6rg=20Example?= <jorg@example.com>", "Jörg Example"),
        ],
    )
    def test_local_forge_author_is_the_display_name_else_the_address(self, sender, author):
        assert LocalDiffForge(_mail(sender=sender), path=LOCAL_PATH).get_pr(REF).author == author

    def test_local_forge_series_takes_the_first_patch_metadata(self):
        series = _mail(subject="[PATCH 1/2] First fix") + _mail(subject="[PATCH 2/2] Second fix")
        assert _patch_metadata(series, LOCAL_PATH)[0] == "First fix"

    @pytest.mark.parametrize(
        "text",
        [
            MODIFY_DIFF,
            "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
            "commit 0123456789abcdef\nAuthor: Alice Example <alice@example.com>\n\n    Fix\n\n" + MODIFY_DIFF,
            "From the vendor, no subject header\n\n" + MODIFY_DIFF,
        ],
        ids=["git-diff", "no-diff-git-line", "git-show", "from-without-subject"],
    )
    def test_local_forge_plain_diff_title_falls_back_to_filename(self, text):
        forge = LocalDiffForge(text, path="cases/fix-widget.patch")
        pr = forge.get_pr(REF)
        assert (pr.title, pr.description, pr.author) == ("Local diff fix-widget.patch", "", "")

    def test_local_forge_unparseable_mail_falls_back_to_filename(self, monkeypatch):
        def choke(*args, **kwargs):
            raise ValueError("malformed mail")

        monkeypatch.setattr("prxref.forges.replay.email.message_from_string", choke)
        assert _patch_metadata(_mail(), "cases/fix.patch") == ("Local diff fix.patch", "", "")
