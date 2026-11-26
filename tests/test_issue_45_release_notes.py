"""Release notes from merged pull requests (#45).

Acceptance criteria:
1. A CLI command and an optional post-merge daemon hook that assemble notes
   from the executive summaries of the PRs merged since a tag.
2. Markdown output grouped by conventional-commit category, editable before
   commit (plain markdown, no prxref markers).

The summary fixtures under ``tests/fixtures/issue45/`` are real prxref
summary comments, copied byte for byte from public pull requests.
"""
from __future__ import annotations

import io
import json
import logging
import os
import queue
import subprocess
from pathlib import Path

import pytest

from prxref import cli, webhooks
from prxref import release_notes as rn
from prxref.forges.base import SUMMARY_MARKER
from prxref.llm import ConfigError

FIXTURES = Path(__file__).parent / "fixtures" / "issue45"
CLEAN = (FIXTURES / "summary_approved_clean.md").read_text(encoding="utf-8")
WITH_FINDINGS = (FIXTURES / "summary_with_findings.md").read_text(encoding="utf-8")
ERROR = (FIXTURES / "summary_error.md").read_text(encoding="utf-8")
BASE = "https://github.com/acme/widgets/pull/"


def _c(subject: str, body: str = "", sha: str = "s") -> rn.Commit:
    return rn.Commit(sha=sha, subject=subject, body=body)


class TestPrNumber:
    @pytest.mark.parametrize(
        ("subject", "body", "expected"),
        [
            ("feat: add x (#12)", "", (12, "feat: add x (#12)", False)),
            ("Merge pull request #7 from acme/topic", "fix: crash", (7, "fix: crash", True)),
            ("Merge pull request #8 in P/r from t", "", (8, "Merge pull request #8 in P/r from t", True)),
            ("Merge pull request 'feat: gitea thing' (#9) from t into main", "", (9, "feat: gitea thing", True)),
            ("Merge branch 't' into 'main'", "perf: fast\n\nSee merge request g/s/r!31", (31, "perf: fast", True)),
            ("Merged in topic (pull request #44)", "docs: the gate\n\nApproved-by: x", (44, "docs: the gate", True)),
            ("Merged PR 55: refactor: split the parser", "", (55, "refactor: split the parser", True)),
        ],
    )
    def test_each_forge_shape(self, subject, body, expected):
        assert rn.pr_number(_c(subject, body)) == expected

    @pytest.mark.parametrize(
        "subject",
        ["fix: handle null (Fixes #12)x", "fix: crash, fixes #12", "release: 0.44.0 (#32, #44, #40)", "chore: bump"],
    )
    def test_a_bare_or_listed_number_is_not_a_pull_request(self, subject):
        assert rn.pr_number(_c(subject, "Fixes #12\nSee #13")) is None


class TestCategorize:
    @pytest.mark.parametrize(
        ("title", "body", "expected"),
        [
            ("feat: add x (#12)", "", ("feat", "add x")),
            ("fix(gitlab): paging", "", ("fix", "paging")),
            ("Perf: faster", "", ("perf", "faster")),
            ("feat!: drop py3.11", "", ("breaking", "drop py3.11")),
            ("refactor(api)!: rename", "", ("breaking", "rename")),
            ("feat: new flag", "BREAKING CHANGE: the old flag is gone", ("breaking", "new flag")),
            ("release: 0.43.0, stuff (#146)", "", ("other", "release: 0.43.0, stuff")),
            ("Update README", "", ("other", "Update README")),
        ],
    )
    def test_category_and_text(self, title, body, expected):
        assert rn.categorize(title, body) == expected

    def test_every_documented_type_has_a_group(self):
        keys = [key for key, _ in rn.CATEGORIES]
        for kind in ("feat", "fix", "perf", "refactor", "docs", "test", "chore", "build", "ci", "revert"):
            assert kind in keys
        assert keys[0] == "breaking" and keys[-1] == "other"


class TestMergedPullRequests:
    def test_one_entry_per_number_and_a_typed_commit_beats_a_release_commit(self):
        prs, without = rn.merged_pull_requests([
            _c("feat: the default-branch check is on (#146)", sha="a"),
            _c("release: 0.43.0, the default-branch check (#146)", sha="b"),
            _c("chore: no pr here", sha="c"),
        ])
        assert [(p.number, p.title, p.sha) for p in prs] == [(146, "feat: the default-branch check is on (#146)", "a")]
        assert without == 1

    def test_a_merge_commit_wins_over_a_squash_reference(self):
        prs, _ = rn.merged_pull_requests([
            _c("wip (#5)", sha="a"),
            _c("Merge pull request #5 from acme/topic", "fix: the real title", sha="b"),
        ])
        assert [(p.title, p.sha) for p in prs] == [("fix: the real title", "b")]


class TestExecutiveExcerpt:
    def test_real_clean_summary(self):
        assert rn.executive_excerpt(CLEAN) == "Approved: 🟥 0 error · 🟧 0 warning · 🔍 0 spec · ⬜ 0 outofscope"

    def test_real_summary_with_findings(self):
        expected = "Approved: 🟥 0 error · 🟧 1 warning · 🔍 0 spec · ⬜ 1 outofscope"
        assert rn.executive_excerpt(WITH_FINDINGS) == expected

    def test_real_error_summary_without_the_heading(self):
        excerpt = rn.executive_excerpt(ERROR)
        assert excerpt == (
            "Error: The review could not complete: all 4 worker reviews failed "
            "(LLMError: all models failed: glm-5.3: HTTP 400)"
        )

    def test_markers_banners_and_footer_never_leak(self):
        body = (
            f"{SUMMARY_MARKER}\n> ⚠️ Large PR: 900 lines\n\n{CLEAN}\n\n"
            "<!-- prxref:reviewed-head abc1234 -->"
        )
        excerpt = rn.executive_excerpt(body)
        assert excerpt == "Approved: 🟥 0 error · 🟧 0 warning · 🔍 0 spec · ⬜ 0 outofscope"

    def test_a_custom_template_falls_back_to_the_first_plain_paragraph(self):
        body = f"{SUMMARY_MARKER}\n### Review\n\nLooks fine overall.\nTwo nits.\n\n---\nReviewed by prxref · m"
        assert rn.executive_excerpt(body) == "Looks fine overall. Two nits."

    def test_capped(self):
        assert len(rn.executive_excerpt("word " * 200, max_chars=50)) == 50

    @pytest.mark.parametrize("body", [None, "", f"{SUMMARY_MARKER}\n---\nReviewed by prxref · m"])
    def test_nothing_left(self, body):
        assert rn.executive_excerpt(body) == ""


class TestRender:
    def test_groups_in_fixed_order_and_plain_markdown(self):
        entries = [
            rn.Entry(3, BASE + "3", "fix", "paging", ""),
            rn.Entry(1, BASE + "1", "other", "Update README", ""),
            rn.Entry(2, BASE + "2", "feat", "add x", "Approved: 0 error"),
            rn.Entry(4, BASE + "4", "breaking", "drop py3.11", ""),
        ]
        text = rn.render(entries)
        assert text == (
            "## Breaking changes\n\n- drop py3.11 ([#4](https://github.com/acme/widgets/pull/4))\n\n"
            "## Features\n\n- add x ([#2](https://github.com/acme/widgets/pull/2))\n"
            "  prxref review: Approved: 0 error\n\n"
            "## Fixes\n\n- paging ([#3](https://github.com/acme/widgets/pull/3))\n\n"
            "## Other\n\n- Update README ([#1](https://github.com/acme/widgets/pull/1))\n"
        )
        assert "<!--" not in text and "Reviewed by prxref" not in text


class TestPrUrlBase:
    @pytest.mark.parametrize(
        ("repo_url", "expected"),
        [
            ("https://github.com/acme/widgets", "https://github.com/acme/widgets/pull/"),
            ("https://github.com/acme/widgets.git/", "https://github.com/acme/widgets/pull/"),
            ("https://gitlab.com/g/sub/repo", "https://gitlab.com/g/sub/repo/-/merge_requests/"),
            ("https://bitbucket.org/ws/repo", "https://bitbucket.org/ws/repo/pull-requests/"),
            ("https://codeberg.org/o/r", "https://codeberg.org/o/r/pulls/"),
            ("https://bb.example.com/projects/P/repos/r", "https://bb.example.com/projects/P/repos/r/pull-requests/"),
            ("https://dev.azure.com/org/proj/_git/repo", "https://dev.azure.com/org/proj/_git/repo/pullrequest/"),
        ],
    )
    def test_derived(self, repo_url, expected):
        assert rn.pr_url_base(repo_url=repo_url) == expected

    def test_an_ambiguous_self_hosted_url_names_the_flag(self):
        with pytest.raises(ConfigError, match="--repo-url.*--pr-url-base"):
            rn.pr_url_base(repo_url="https://git.example.com/o/r")

    def test_a_base_without_trailing_slash_is_fixed_and_a_bad_one_refused(self):
        assert rn.pr_url_base(pr_url_base="https://git.example.com/o/r/pulls") == "https://git.example.com/o/r/pulls/"
        with pytest.raises(ConfigError, match="--pr-url-base"):
            rn.pr_url_base(pr_url_base="https://example.com/not/a/pr/shape/")


# --- the CLI, end to end over a real git repository ------------------------

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env={**os.environ, **_GIT_ENV})


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key, value in _GIT_ENV.items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "commit", "-q", "--allow-empty", "-m", "chore: start")
    _git(path, "tag", "v1.0.0")
    for message in (
        "feat: add widgets (#10)",
        "fix(api): null owner (#11)",
        "Merge pull request #12 from acme/topic\n\nperf: faster chunker",
        "docs: no pull request here",
        "feat!: drop the old flag (#13)",
        "release: 1.1.0 (#10)",
    ):
        _git(path, "commit", "-q", "--allow-empty", "-m", message)
    _git(path, "tag", "v1.1.0")
    return path


class FakeForge:
    def __init__(self, summaries, fail=()):
        self.summaries, self.fail, self.calls, self.writes = summaries, set(fail), [], []

    def get_summary(self, ref):
        self.calls.append(ref.number)
        if ref.number in self.fail:
            raise RuntimeError("HTTP 502")
        return self.summaries.get(ref.number)

    def post_summary(self, ref, body):  # pragma: no cover - must never be called
        self.writes.append(ref.number)


@pytest.fixture
def forge(monkeypatch):
    fake = FakeForge({10: CLEAN, 12: WITH_FINDINGS}, fail={13})
    monkeypatch.setattr(cli, "make_forge", lambda ref: fake)
    return fake


def _run(argv, capsys):
    rc = cli.main(argv)
    out, err = capsys.readouterr()
    return rc, out, err


class TestCli:
    def test_notes_since_a_tag_grouped_with_summary_excerpts(self, repo, forge, capsys):
        rc, out, err = _run(
            ["release-notes", "--since", "v1.0.0", "--until", "v1.1.0", "--repo", str(repo),
             "--repo-url", "https://github.com/acme/widgets"],
            capsys,
        )
        assert rc == 0
        assert out == (
            "## Breaking changes\n\n- drop the old flag ([#13](https://github.com/acme/widgets/pull/13))\n\n"
            "## Features\n\n- add widgets ([#10](https://github.com/acme/widgets/pull/10))\n"
            "  prxref review: Approved: 🟥 0 error · 🟧 0 warning · 🔍 0 spec · ⬜ 0 outofscope\n\n"
            "## Fixes\n\n- null owner ([#11](https://github.com/acme/widgets/pull/11))\n\n"
            "## Performance\n\n- faster chunker ([#12](https://github.com/acme/widgets/pull/12))\n"
            "  prxref review: Approved: 🟥 0 error · 🟧 1 warning · 🔍 0 spec · ⬜ 1 outofscope\n"
        )
        assert sorted(forge.calls) == [10, 11, 12, 13] and forge.writes == []
        assert "#11: fallback to the commit subject (no prxref summary)" in err
        assert "#13: fallback to the commit subject (summary read failed: RuntimeError: HTTP 502)" in err
        assert "4 pull requests, 2 prxref summaries, 2 fallbacks, 1 commits naming no pull request" in err

    def test_until_defaults_to_head_and_out_writes_an_editable_file(self, repo, forge, tmp_path, capsys):
        target = tmp_path / "notes.md"
        rc, out, _ = _run(
            ["release-notes", "--since", "v1.0.0", "--repo", str(repo), "--pr-url-base", BASE, "--out", str(target)],
            capsys,
        )
        assert rc == 0 and out == ""
        text = target.read_text(encoding="utf-8")
        assert text.startswith("## Breaking changes\n") and "<!--" not in text

    @pytest.mark.parametrize(
        ("extra", "flag"),
        [
            (["--since", "v9.9.9"], "--since"),
            (["--since", "v1.0.0", "--until", "nope"], "--until"),
            (["--since=--output=/tmp/x"], "--since"),
        ],
    )
    def test_an_unknown_revision_exits_2_naming_the_flag(self, repo, forge, capsys, extra, flag):
        rc, out, err = _run(["release-notes", *extra, "--repo", str(repo), "--pr-url-base", BASE], capsys)
        assert rc == 2 and out == ""
        assert err.startswith(f"configuration error: {flag}:")

    def test_not_a_git_repository_exits_2_naming_repo(self, tmp_path, forge, capsys):
        rc, _, err = _run(["release-notes", "--since", "v1", "--repo", str(tmp_path), "--pr-url-base", BASE], capsys)
        assert rc == 2 and "configuration error: --repo:" in err

    def test_a_bad_url_exits_2_naming_the_flag(self, repo, forge, capsys):
        rc, _, err = _run(
            ["release-notes", "--since", "v1.0.0", "--repo", str(repo), "--repo-url", "https://git.example.com/o/r"],
            capsys,
        )
        assert rc == 2 and "configuration error: --repo-url:" in err

    def test_an_unwritable_out_exits_2(self, repo, forge, tmp_path, capsys):
        rc, _, err = _run(
            ["release-notes", "--since", "v1.0.0", "--repo", str(repo), "--pr-url-base", BASE,
             "--out", str(tmp_path / "missing" / "notes.md")],
            capsys,
        )
        assert rc == 2 and "configuration error: --out:" in err

    def test_an_empty_range(self, repo, forge, capsys):
        rc, out, _ = _run(
            ["release-notes", "--since", "v1.1.0", "--repo", str(repo), "--pr-url-base", BASE], capsys,
        )
        assert rc == 0 and out == "No merged pull requests in this range.\n"


# --- the post-merge daemon hook --------------------------------------------

def _closed(merged: bool, number: int = 21, title: str = "feat: draft hook (#21)") -> bytes:
    return json.dumps({
        "action": "closed",
        "pull_request": {"html_url": f"{BASE}{number}", "merged": merged, "title": title},
    }).encode()


_HEADERS = {"X-GitHub-Event": "pull_request"}


@pytest.fixture
def unsigned(monkeypatch):
    monkeypatch.setenv("PRXREF_ALLOW_UNSIGNED", "1")
    monkeypatch.delenv("PRXREF_GITHUB_WEBHOOK_SECRET", raising=False)


class TestIntake:
    def test_off_by_default_a_merged_close_is_ignored_as_before(self, unsigned, monkeypatch):
        monkeypatch.delenv("PRXREF_RELEASE_NOTES_FILE", raising=False)
        assert webhooks.verify_signature(_closed(True), _HEADERS) == (
            False, "ignored: github pull_request action 'closed' is not reviewable",
        )

    def test_a_merged_close_queues_a_release_note_job(self, unsigned, monkeypatch, tmp_path):
        monkeypatch.setenv("PRXREF_RELEASE_NOTES_FILE", str(tmp_path / "draft.md"))
        review_queue: queue.Queue = queue.Queue()
        handler_cls = webhooks.make_webhook_handler(review_queue)
        body = _closed(True)
        handler = handler_cls.__new__(handler_cls)
        handler.headers = {**_HEADERS, "Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        sent = []
        handler._respond = lambda status, payload, extra=None: sent.append(status)
        handler._handle_webhook()
        assert sent == [202]
        assert review_queue.get_nowait() == webhooks.ReleaseNoteJob(f"{BASE}21", "feat: draft hook (#21)")

    def test_an_unmerged_close_is_ignored(self, unsigned, monkeypatch, tmp_path):
        monkeypatch.setenv("PRXREF_RELEASE_NOTES_FILE", str(tmp_path / "draft.md"))
        assert webhooks.verify_signature(_closed(False), _HEADERS) == (
            False, "ignored: github pull_request closed without merging",
        )

    def test_the_worker_routes_the_job_to_the_release_note_handler(self):
        review_queue: queue.Queue = queue.Queue()
        seen = []
        review_queue.put(webhooks.ReleaseNoteJob(f"{BASE}21", "t"))
        review_queue.put(None)
        webhooks._worker_loop(
            lambda url: seen.append(("review", url)), review_queue, None,
            lambda url, title: seen.append(("note", url, title)),
        )
        assert seen == [("note", f"{BASE}21", "t")]


class TestServeWiring:
    def _capture(self, monkeypatch):
        captured = {}

        def fake_serve(port, host, handler, reply_handler=None, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr("prxref.webhooks.serve", fake_serve)
        return captured

    def test_no_release_note_handler_by_default(self, monkeypatch):
        monkeypatch.delenv("PRXREF_RELEASE_NOTES_FILE", raising=False)
        captured = self._capture(monkeypatch)
        assert cli.main(["serve"]) == 0
        assert captured == {}

    def test_the_handler_is_wired_when_the_file_is_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PRXREF_RELEASE_NOTES_FILE", str(tmp_path / "draft.md"))
        captured = self._capture(monkeypatch)
        assert cli.main(["serve"]) == 0
        assert captured == {"release_note_handler": cli._webhook_release_note_handler}


class TestDraftHook:
    @pytest.fixture
    def draft(self, monkeypatch, tmp_path):
        path = tmp_path / "draft.md"
        monkeypatch.setenv("PRXREF_RELEASE_NOTES_FILE", str(path))
        return path

    def test_appends_under_its_category_in_order_and_dedups(self, draft, monkeypatch):
        fake = FakeForge({21: CLEAN})
        monkeypatch.setattr(cli, "make_forge", lambda ref: fake)
        cli._webhook_release_note_handler(f"{BASE}21", "fix: one (#21)")
        cli._webhook_release_note_handler(f"{BASE}22", "feat: two")
        cli._webhook_release_note_handler(f"{BASE}23", "fix: three")
        cli._webhook_release_note_handler(f"{BASE}21", "fix: one again (#21)")
        assert draft.read_text(encoding="utf-8") == (
            "# Unreleased\n\n"
            "## Features\n\n- two ([#22](https://github.com/acme/widgets/pull/22))\n\n"
            "## Fixes\n\n- one ([#21](https://github.com/acme/widgets/pull/21))\n"
            "  prxref review: Approved: 🟥 0 error · 🟧 0 warning · 🔍 0 spec · ⬜ 0 outofscope\n"
            "- three ([#23](https://github.com/acme/widgets/pull/23))\n"
        )
        assert fake.writes == []

    def test_a_human_edited_draft_keeps_its_text(self, draft, monkeypatch):
        draft.write_text(
            "# Unreleased\n\nIntro a human wrote.\n\n## Fixes\n\n- edited by hand ([#5](x))\n", encoding="utf-8",
        )
        monkeypatch.setattr(cli, "make_forge", lambda ref: FakeForge({}))
        cli._webhook_release_note_handler(f"{BASE}6", "fix: six")
        cli._webhook_release_note_handler(f"{BASE}5", "fix: five")
        assert draft.read_text(encoding="utf-8") == (
            "# Unreleased\n\nIntro a human wrote.\n\n## Fixes\n\n- edited by hand ([#5](x))\n"
            "- six ([#6](https://github.com/acme/widgets/pull/6))\n"
        )

    def test_a_summary_read_failure_still_records_the_title(self, draft, monkeypatch):
        monkeypatch.setattr(cli, "make_forge", lambda ref: FakeForge({}, fail={7}))
        cli._webhook_release_note_handler(f"{BASE}7", "perf: seven")
        assert "- seven ([#7](" in draft.read_text(encoding="utf-8")

    def test_a_write_failure_is_logged_not_raised(self, monkeypatch, tmp_path, caplog):
        blocked = tmp_path / "is_a_dir"
        blocked.mkdir()
        monkeypatch.setenv("PRXREF_RELEASE_NOTES_FILE", str(blocked))
        monkeypatch.setattr(cli, "make_forge", lambda ref: FakeForge({}))
        with caplog.at_level(logging.ERROR, logger="prxref"):
            cli._webhook_release_note_handler(f"{BASE}8", "fix: eight")
        assert any("release notes: could not record" in r.getMessage() for r in caplog.records)

    def test_off_by_default_the_handler_writes_nothing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("PRXREF_RELEASE_NOTES_FILE", raising=False)
        monkeypatch.chdir(tmp_path)
        called = []
        monkeypatch.setattr(cli, "make_forge", lambda ref: called.append(ref))
        cli._webhook_release_note_handler(f"{BASE}9", "fix: nine")
        assert called == [] and list(tmp_path.iterdir()) == []
