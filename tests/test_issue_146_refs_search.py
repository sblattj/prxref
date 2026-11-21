"""Issue #146, third part: search the whole default branch and release tags.

The owner left #146 open on three gaps: no repo-wide search, no release-tag
check, and a finding the check could not tie to a removed surface of its own
file. Each case below is the issue's failure shape on a synthetic repository:

* a route the PR removed lives in another file on the default branch (moved
  earlier in the stack): the old check lowered it, hiding a real break;
* a route gone from the default branch but still in the newest release tag:
  released clients can call it, so the finding must stay;
* the duplicate on the build file (the route string is only in the
  controller's removed lines): the old check left it an ``error``.

The git search runs only when ``--repo-dir`` is a git checkout that holds the
default branch ref; otherwise the forge path runs exactly as before.
"""
from __future__ import annotations

import subprocess

import pytest

from prxref import default_branch
from prxref.forges.repo_dir import RepoDir
from prxref.orchestrator import orchestrate_review
from tests.test_issue_146_default_branch_check import (
    API,
    NOTE_B,
    BranchForge,
    only,
    raw,
    route_diff,
    run,
)
from tests.test_orchestrator import REF, FakeLLM

ROUTE_LINE = '@route("/v1/widgets")\ndef widgets():\n    return data\n'
BUILD = "build.gradle"


def git(root, *args):
    subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
             "HOME": str(root), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
    )


def make_repo(tmp_path, commits, *, tag_after=None):
    """A git repo on branch ``main``; ``commits`` is a list of {path: text|None}."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    for i, files in enumerate(commits):
        for path, text in files.items():
            p = root / path
            if text is None:
                git(root, "rm", "-q", path)
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            git(root, "add", path)
        git(root, "commit", "-q", "-m", f"c{i}")
        if tag_after == i:
            git(root, "tag", "-a", "v1.0.0", "-m", "release")
    return root


def two_file_diff():
    return route_diff(API) + (
        f"diff --git a/{BUILD} b/{BUILD}\n"
        f"--- a/{BUILD}\n"
        f"+++ b/{BUILD}\n"
        "@@ -1,3 +1,2 @@\n"
        " plugins { id 'java' }\n"
        "-sourceSets { main { java { srcDir 'widgets' } } }\n"
        " version = '1'\n"
    )


def build_raw():
    return raw(
        file=BUILD, line=2, title="Removed route without a new API version",
        body="Dropping this removes the `/v1/widgets` endpoint; keep it or version it.",
    )


def run_two(forge, findings_by_file, **kw):
    llm = FakeLLM(findings_by_file)
    return orchestrate_review(
        forge, REF, llm, post=True, max_workers=1, default_branch_check="on", **kw,
    )


@pytest.mark.usefixtures("contract_stubs")
class TestRepoWideSearch:
    def test_a_route_that_moved_files_on_default_keeps_the_error(self, tmp_path):
        root = make_repo(tmp_path, [{API: "import web\n", "src/legacy.py": ROUTE_LINE}])
        forge = BranchForge(diff=route_diff(API), files={API: (True, "import web\n")})
        res = run(forge, repo_dir=RepoDir(root))
        f = only(res)
        assert f.severity == "error"
        assert "Checked against main: `/v1/widgets` still present there." in f.body
        assert res["default_branch_check"]["refs"] == ["main"]

    def test_control_without_repo_dir_the_forge_path_still_lowers(self, tmp_path):
        forge = BranchForge(diff=route_diff(API), files={API: (True, "import web\n")})
        f = only(run(forge))
        assert f.severity == "outofscope"
        assert f.body.endswith(NOTE_B)

    def test_a_never_shipped_route_is_lowered_naming_the_refs(self, tmp_path):
        root = make_repo(tmp_path, [{API: "import web\n"}], tag_after=0)
        forge = BranchForge(diff=route_diff(API), files={API: (True, "import web\n")})
        f = only(run(forge, repo_dir=RepoDir(root)))
        assert f.severity == "outofscope"
        assert "Checked against main and v1.0.0 (whole tree)" in f.body
        assert "cannot tell" not in f.body


@pytest.mark.usefixtures("contract_stubs")
class TestReleaseTags:
    def test_a_route_only_in_the_newest_release_tag_keeps_the_error(self, tmp_path):
        root = make_repo(
            tmp_path, [{API: "import web\n" + ROUTE_LINE}, {API: "import web\n"}],
            tag_after=0,
        )
        forge = BranchForge(diff=route_diff(API), files={API: (True, "import web\n")})
        f = only(run(forge, repo_dir=RepoDir(root)))
        assert f.severity == "error"
        assert "Checked against v1.0.0: `/v1/widgets` still present there." in f.body


@pytest.mark.usefixtures("contract_stubs")
class TestBuildFileDuplicate:
    def test_the_build_file_duplicate_is_lowered_with_the_controller(self):
        forge = BranchForge(
            diff=two_file_diff(),
            files={API: (True, "import web\n"), BUILD: (True, "plugins { id 'java' }\n")},
        )
        res = run_two(forge, {API: [raw()], BUILD: [build_raw()]})
        sev = {f.file: f.severity for f in res["findings_active"]}
        assert sev.get(BUILD, "outofscope") == "outofscope"
        assert sev.get(API, "outofscope") == "outofscope"
        assert (API, "main") in forge.reads

    def test_control_the_build_file_duplicate_stays_when_the_route_ships(self):
        forge = BranchForge(
            diff=two_file_diff(),
            files={API: (True, "import web\n" + ROUTE_LINE),
                   BUILD: (True, "plugins { id 'java' }\n")},
        )
        res = run_two(forge, {API: [raw()], BUILD: [build_raw()]})
        assert all(f.severity == "error" for f in res["findings_active"])


class TestGitSearchFailsOpen:
    def test_a_directory_that_is_not_git_is_unknown(self, tmp_path):
        assert default_branch.git_search(str(tmp_path), "main", ["/v1/widgets"]) is None

    def test_a_missing_default_ref_is_unknown(self, tmp_path):
        root = make_repo(tmp_path, [{API: ROUTE_LINE}])
        assert default_branch.git_search(str(root), "trunk", ["/v1/widgets"]) is None

    def test_an_option_shaped_token_is_searched_as_text(self, tmp_path):
        root = make_repo(tmp_path, [{API: "--output-dir\n"}])
        res = default_branch.git_search(str(root), "main", ["--output-dir"])
        assert res is not None and res.present_in == ["main"]

    def test_an_option_shaped_branch_is_refused(self, tmp_path):
        root = make_repo(tmp_path, [{API: ROUTE_LINE}])
        assert default_branch.git_search(str(root), "--all", ["/v1/widgets"]) is None


@pytest.mark.usefixtures("contract_stubs")
class TestGenericTokens:
    def test_a_generic_removed_name_elsewhere_on_default_does_not_keep_a_never_shipped_route(
        self, tmp_path,
    ):
        diff = (
            f"diff --git a/{API} b/{API}\n--- a/{API}\n+++ b/{API}\n"
            "@@ -1,4 +1,3 @@\n import web\n"
            '-@route("/v1/widgets")\n-def widgets_handler():\n     return data\n'
        )
        root = make_repo(tmp_path, [{"src/other.py": "def widgets_handler():\n    pass\n"}])
        body = "`/v1/widgets` and `widgets_handler` were removed; keep it or version it."
        forge = BranchForge(diff=diff, files={API: (False, None)})
        f = only(run(forge, [raw(body=body, line=2)], repo_dir=RepoDir(root)))
        assert f.severity == "outofscope"
        assert "(whole tree)" in f.body


class TestNestedDirectory:
    def test_a_plain_directory_inside_another_checkout_is_unknown(self, tmp_path):
        root = make_repo(tmp_path, [{API: ROUTE_LINE}])
        nested = root / "fixtures"
        nested.mkdir()
        assert default_branch.git_search(str(nested), "main", ["/v1/widgets"]) is None

    def test_git_dir_in_the_environment_is_ignored(self, tmp_path, monkeypatch):
        other = make_repo(tmp_path, [{API: ROUTE_LINE}])
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.setenv("GIT_DIR", str(other / ".git"))
        assert default_branch.git_search(str(plain), "main", ["/v1/widgets"]) is None


@pytest.mark.usefixtures("contract_stubs")
class TestIncompleteTagSearch:
    def test_a_failed_tag_grep_keeps_the_finding_unchanged(self, tmp_path, monkeypatch):
        root = make_repo(
            tmp_path, [{API: "import web\n" + ROUTE_LINE}, {API: "import web\n"}],
            tag_after=0,
        )
        real = default_branch._grep

        def flaky(r, ref, tokens, timeout):
            return None if ref.startswith("refs/tags/") else real(r, ref, tokens, timeout)

        monkeypatch.setattr(default_branch, "_grep", flaky)
        forge = BranchForge(diff=route_diff(API), files={API: (True, "import web\n")})
        res = run(forge, repo_dir=RepoDir(root))
        f = only(res)
        assert f.severity == "error"
        assert "Checked against" not in f.body
        assert res["default_branch_check"]["unknown"] == 1
        assert forge.reads == []
