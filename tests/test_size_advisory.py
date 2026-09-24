"""Issue #68: the PR-size advisory, end to end on the real body.

``tests/test_run_record.py`` proves the foundation hook is wired by
monkeypatching ``_size_advisory``, and ``tests/test_config.py`` owns the three
keys' parsing. This file drives the real counting and wording instead: the
public lockfile alias, the triage predicates, the ``_size_advisory`` body and
its message, the posted summary on every path that renders one, and the
``prxref review`` entry point from the environment to its JSON and text
output.
"""
from __future__ import annotations

import ast
import inspect
import json
import sys
import types

import pytest

from prxref import cli, heuristics, orchestrator, triage
from prxref.cli import _fail_on_exit, main
from prxref.orchestrator import orchestrate_review
from prxref.triage import (
    GENERATED_FILE_RE,
    count_size_relevant_changes,
    is_size_ignored,
    parse_unified_diff,
)
from tests.test_orchestrator import (
    HAPPY_FINDINGS,
    REF,
    FakeForge,
    FakeLLM,
    _added_file_diff,
)
from tests.test_run_record import TRIGGERED, UNTRIGGERED

pytestmark = pytest.mark.usefixtures("contract_stubs")

LOCKFILES = sorted(heuristics.LOCKFILE_BASENAMES)
ADVISORY_PREFIX = "> ⚠️ This PR changes"


def _removed_file_diff(path: str, n_lines: int) -> str:
    body = "\n".join(f"-gone {i}" for i in range(1, n_lines + 1))
    return (
        f"diff --git a/{path} b/{path}\n"
        "deleted file mode 100644\n"
        f"--- a/{path}\n"
        "+++ /dev/null\n"
        f"@@ -1,{n_lines} +0,0 @@\n"
        f"{body}\n"
    )


def _binary_diff(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        f"Binary files /dev/null and b/{path} differ\n"
    )


MODIFIED_DIFF = (
    "diff --git a/src/calc.py b/src/calc.py\n"
    "--- a/src/calc.py\n"
    "+++ b/src/calc.py\n"
    "@@ -1,5 +1,4 @@\n"
    " keep\n"
    "-old one\n"
    "-old two\n"
    "-old three\n"
    "+new one\n"
    "+new two\n"
    " tail\n"
)

RENAME_DIFF = (
    "diff --git a/src/old_name.py b/src/new_name.py\n"
    "similarity index 100%\n"
    "rename from src/old_name.py\n"
    "rename to src/new_name.py\n"
)

THREE_SMALL_FILES = "".join(_added_file_diff(f"src/m{i}.py", 2) for i in range(1, 4))


def _files(diff: str):
    return parse_unified_diff(diff)


def _stats(diff: str, **kw):
    kw.setdefault("lines_limit", None)
    kw.setdefault("files_limit", None)
    return orchestrator._size_advisory(_files(diff), **kw)


def _review(diff: str, *, findings=None, **kw):
    forge = FakeForge(diff=diff)
    llm = FakeLLM(findings_by_path=HAPPY_FINDINGS if findings is None else findings)
    kw.setdefault("post", True)
    res = orchestrate_review(forge, REF, llm, **kw)
    return res, forge


def _without_attribution(body: str) -> str:
    """The summary minus its last line, whose elapsed time varies per run."""
    return body.rsplit("\n", 1)[0]


class TestLockfileAlias:
    def test_the_public_alias_is_the_private_set(self):
        assert heuristics.LOCKFILE_BASENAMES is heuristics._LOCKFILE_BASENAMES

    def test_it_spans_more_than_the_npm_family(self):
        assert {"package-lock.json", "uv.lock", "Cargo.lock", "go.sum"} <= heuristics.LOCKFILE_BASENAMES

    def test_triage_never_imports_heuristics(self):
        tree = ast.parse(inspect.getsource(triage))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert not any("heuristics" in name for name in imported), imported


class TestIsSizeIgnored:
    @pytest.mark.parametrize("name", LOCKFILES)
    def test_a_lockfile_is_ignored_at_any_depth(self, name):
        for path in (name, f"services/api/{name}"):
            assert is_size_ignored(path, lockfile_basenames=heuristics.LOCKFILE_BASENAMES)

    @pytest.mark.parametrize("name", LOCKFILES)
    def test_lockfiles_come_only_from_the_callers_set(self, name):
        assert GENERATED_FILE_RE.search(name) is None
        assert not is_size_ignored(name)

    @pytest.mark.parametrize("path", [
        "yarn.lock.orig",
        "my-package-lock.json",
        "Uv.lock",
        "src/lockfile.py",
    ])
    def test_the_basename_match_is_exact_and_case_sensitive(self, path):
        assert not is_size_ignored(path, lockfile_basenames=heuristics.LOCKFILE_BASENAMES)

    @pytest.mark.parametrize("path", [
        "tests/__snapshots__/app.test.ts.snap",
        "ui/__snapshots__/button.txt",
        "a.snap",
        "static/vendor.min.js",
        "static/app.js.map",
        "api/client.generated.ts",
        "proto/types.auto.go",
    ])
    def test_generated_paths_are_ignored(self, path):
        assert is_size_ignored(path)

    @pytest.mark.parametrize("path", [
        "src/app.py",
        "src/map.py",
        "src/mapper.ts",
        "docs/snapshots.md",
        "static/app.js",
        "src/auto_save.py",
    ])
    def test_ordinary_paths_are_counted(self, path):
        assert not is_size_ignored(
            path, lockfile_basenames=heuristics.LOCKFILE_BASENAMES, ignore_globs=("vendor/*",),
        )

    def test_an_operator_glob_is_ignored(self):
        assert is_size_ignored("api/v1/service.pb.go", ignore_globs=("*.pb.go",))
        assert not is_size_ignored("api/v1/service.go", ignore_globs=("*.pb.go",))

    def test_a_glob_star_crosses_directories(self):
        assert is_size_ignored("dist/sub/deep/app.js", ignore_globs=("dist/*",))

    def test_a_glob_matches_the_full_path(self):
        assert not is_size_ignored("dist/app.js", ignore_globs=("dist",))
        assert not is_size_ignored("dist/app.js", ignore_globs=("app.js",))

    def test_a_glob_is_case_sensitive(self):
        assert not is_size_ignored("api/service.pb.go", ignore_globs=("*.PB.GO",))

    def test_globs_add_to_the_builtins_never_replace_them(self):
        globs = ("vendor/*",)
        kw = {"lockfile_basenames": heuristics.LOCKFILE_BASENAMES, "ignore_globs": globs}
        assert is_size_ignored("vendor/lib/x.js", **kw)
        assert is_size_ignored("uv.lock", **kw)
        assert is_size_ignored("ui/__snapshots__/x.snap", **kw)


class TestCountSizeRelevantChanges:
    def test_it_sums_added_and_removed_lines(self):
        assert count_size_relevant_changes(_files(MODIFIED_DIFF)) == (5, 1)

    def test_a_removed_file_counts_its_removed_lines(self):
        assert count_size_relevant_changes(_files(_removed_file_diff("src/old.py", 7))) == (7, 1)

    def test_a_binary_file_counts_as_one_file_and_zero_lines(self):
        files = _files(_binary_diff("assets/logo.png"))
        assert [f.is_binary for f in files] == [True]
        assert count_size_relevant_changes(files) == (0, 1)

    def test_a_pure_rename_counts_as_one_file_and_zero_lines(self):
        files = _files(RENAME_DIFF)
        assert [f.path for f in files] == ["src/new_name.py"]
        assert count_size_relevant_changes(files) == (0, 1)

    def test_an_ignored_file_leaves_both_counts(self):
        diff = (
            _added_file_diff("src/app.py", 4)
            + _added_file_diff("uv.lock", 900)
            + _removed_file_diff("web/yarn.lock", 300)
            + _added_file_diff("ui/__snapshots__/a.snap", 50)
            + _added_file_diff("gen/api.pb.go", 70)
        )
        counted = count_size_relevant_changes(
            _files(diff), lockfile_basenames=heuristics.LOCKFILE_BASENAMES,
            ignore_globs=("gen/*",),
        )
        assert counted == (4, 1)

    def test_nothing_to_count_is_zero_and_zero(self):
        assert count_size_relevant_changes([]) == (0, 0)


class TestSizeAdvisoryBody:
    def test_the_lines_only_variant_matches_the_foundation_fixture(self):
        assert _stats(_added_file_diff("src/app.py", 20), lines_limit=5) == TRIGGERED
        assert _stats(_added_file_diff("src/app.py", 20), lines_limit=50) == UNTRIGGERED

    def test_the_files_only_variant(self):
        stats = _stats(THREE_SMALL_FILES, files_limit=2)
        assert stats == {
            "changed_lines": 6, "changed_files": 3, "lines_limit": None,
            "files_limit": 2, "triggered": True,
            "message": (
                "This PR changes 6 lines in 3 files, above the team guideline of 2 files. "
                "Consider splitting it."
            ),
        }

    def test_both_exceeded_are_joined_with_and(self):
        stats = _stats(THREE_SMALL_FILES, lines_limit=5, files_limit=2)
        assert stats["message"] == (
            "This PR changes 6 lines in 3 files, above the team guideline of 5 lines and 2 files. "
            "Consider splitting it."
        )

    def test_only_the_exceeded_limit_is_named(self):
        stats = _stats(THREE_SMALL_FILES, lines_limit=100, files_limit=1)
        assert stats["lines_limit"] == 100
        assert stats["message"] == (
            "This PR changes 6 lines in 3 files, above the team guideline of 1 file. "
            "Consider splitting it."
        )

    def test_singular_counts_and_a_zero_limit(self):
        stats = _stats(_added_file_diff("src/one.py", 1), lines_limit=0, files_limit=0)
        assert stats["message"] == (
            "This PR changes 1 line in 1 file, above the team guideline of 0 lines and 0 files. "
            "Consider splitting it."
        )

    def test_a_count_equal_to_the_limit_does_not_trigger(self):
        stats = _stats(_added_file_diff("src/app.py", 20), lines_limit=20, files_limit=1)
        assert stats == {
            "changed_lines": 20, "changed_files": 1, "lines_limit": 20,
            "files_limit": 1, "triggered": False, "message": None,
        }

    @pytest.mark.parametrize("name", LOCKFILES)
    def test_every_lockfile_is_left_out(self, name):
        diff = _added_file_diff(f"deps/{name}", 500) + _added_file_diff("src/app.py", 3)
        stats = _stats(diff, lines_limit=10, files_limit=1)
        assert (stats["changed_lines"], stats["changed_files"]) == (3, 1)
        assert stats["triggered"] is False

    def test_the_operator_globs_reach_the_count(self):
        diff = _added_file_diff("docs/guide/intro.md", 40) + _added_file_diff("src/app.py", 3)
        stats = _stats(diff, lines_limit=10, ignore_globs=("docs/*",))
        assert (stats["changed_lines"], stats["changed_files"]) == (3, 1)
        assert stats["triggered"] is False

    @pytest.mark.parametrize("n, word", [(0, "lines"), (1, "line"), (2, "lines"), (500, "lines")])
    def test_plural(self, n, word):
        assert orchestrator._plural(n, "line") == word


class TestPostedSummary:
    def test_unset_yields_null_and_no_line(self):
        res, forge = _review(_added_file_diff("src/app.py", 20))
        assert res["size_advisory"] is None
        assert "This PR changes" not in forge.summaries[0]

    def test_the_advisory_is_the_first_line_of_the_summary(self):
        res, forge = _review(_added_file_diff("src/app.py", 20), size_warn_lines=5)
        assert res["size_advisory"] == TRIGGERED
        head, rest = forge.summaries[0].split("\n\n", 1)
        assert head == f"> ⚠️ {TRIGGERED['message']}"
        assert rest.startswith("🤖 **prxref review — Request-Changes**")

    def test_the_advisory_survives_the_inline_accounting_refresh(self):
        _, forge = _review(
            _added_file_diff("src/app.py", 20), size_warn_lines=5, max_inline_comments=1,
        )
        assert len(forge.summaries) == 2
        for body in (forge.summaries[0], forge.summaries[-1]):
            assert body.startswith(ADVISORY_PREFIX), body

    def test_configured_but_not_exceeded_matches_the_feature_off_summary(self):
        diff = _added_file_diff("src/app.py", 20)
        res_on, forge_on = _review(diff, size_warn_lines=500, size_warn_files=10)
        res_off, forge_off = _review(diff)
        assert res_on["size_advisory"]["triggered"] is False
        assert res_on["size_advisory"]["message"] is None
        assert res_off["size_advisory"] is None
        assert [_without_attribution(b) for b in forge_on.summaries] == [
            _without_attribution(b) for b in forge_off.summaries
        ]

    def test_zero_is_a_legal_threshold_distinct_from_unset(self):
        diff = _added_file_diff("src/one.py", 1)
        res_zero, forge_zero = _review(diff, size_warn_lines=0)
        res_unset, forge_unset = _review(diff, size_warn_lines=None)
        assert res_zero["size_advisory"]["triggered"] is True
        assert forge_zero.summaries[0].startswith(ADVISORY_PREFIX)
        assert res_unset["size_advisory"] is None
        assert not forge_unset.summaries[0].startswith(ADVISORY_PREFIX)

    def test_a_lockfile_only_bulk_does_not_trigger(self):
        diff = _added_file_diff("uv.lock", 2000) + _added_file_diff("src/app.py", 20)
        res, forge = _review(diff, size_warn_lines=100, size_warn_files=1)
        assert res["size_advisory"]["changed_lines"] == 20
        assert res["size_advisory"]["changed_files"] == 1
        assert res["size_advisory"]["triggered"] is False
        assert not forge.summaries[0].startswith(ADVISORY_PREFIX)

    def test_the_verdict_and_the_fail_on_gate_are_unaffected(self):
        diff = _added_file_diff("src/app.py", 20)
        res, _ = _review(diff, findings={}, size_warn_lines=0, size_warn_files=0)
        assert res["size_advisory"]["triggered"] is True
        assert res["verdict"] == "Approved"
        assert res["findings_active"] == []
        for policy in ("error", "any"):
            assert _fail_on_exit(res, policy) == (0, None)
        res_off, _ = _review(diff, size_warn_lines=None)
        res_on, _ = _review(diff, size_warn_lines=0)
        assert res_on["verdict"] == res_off["verdict"] == "Request-Changes"
        assert [f.title for f in res_on["findings_active"]] == [f.title for f in res_off["findings_active"]]

    def test_an_empty_diff_never_triggers_even_at_zero(self):
        res, forge = _review("", size_warn_lines=0, size_warn_files=0)
        assert res["size_advisory"] == {
            "changed_lines": 0, "changed_files": 0, "lines_limit": 0,
            "files_limit": 0, "triggered": False, "message": None,
        }
        assert not forge.summaries[0].startswith(ADVISORY_PREFIX)

    def test_an_all_binary_pr_can_trigger_the_files_limit(self):
        diff = _binary_diff("assets/logo.png") + _binary_diff("assets/hero.jpg")
        res, forge = _review(diff, size_warn_lines=0, size_warn_files=1)
        assert res["chunk_count"] == 0
        assert res["size_advisory"]["message"] == (
            "This PR changes 0 lines in 2 files, above the team guideline of 1 file. "
            "Consider splitting it."
        )
        assert forge.summaries[0].startswith(f"> ⚠️ {res['size_advisory']['message']}\n\n")

    @pytest.mark.parametrize("stage", ["get_pr", "get_diff"])
    def test_an_exit_before_the_parse_carries_null(self, stage):
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        forge.fail.add(stage)
        res = orchestrate_review(
            forge, REF, FakeLLM(findings_by_path=HAPPY_FINDINGS), post=True,
            size_warn_lines=0, size_warn_files=0,
        )
        assert res["verdict"] == "Error"
        assert res["size_advisory"] is None
        assert all("This PR changes" not in body for body in forge.summaries)


def _install_fake_module(monkeypatch, fullname: str, **attrs) -> None:
    mod = types.ModuleType(fullname)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, fullname, mod)


class TestEntryPoint:
    """``prxref review`` from the environment to its output, on the real
    config loader, the real ``_run_review`` and the real orchestrator; only
    the forge and the LLM client are doubles."""

    @pytest.fixture
    def forge(self, monkeypatch):
        assert sys.modules["prxref.orchestrator"] is orchestrator
        forge = FakeForge(diff=_added_file_diff("src/app.py", 20))
        llm = FakeLLM(findings_by_path={})
        monkeypatch.setattr(cli, "detect_forge", lambda url: REF)
        monkeypatch.setattr(cli, "make_forge", lambda ref: forge)
        _install_fake_module(monkeypatch, "prxref.llm_backends", create_llm_client=lambda cfg: llm)
        return forge

    def _json(self, capsys) -> dict:
        assert main(["review", "--pr-url", REF.url, "--no-post", "--format", "json"]) == 0
        return json.loads(capsys.readouterr().out)

    def _text(self, capsys) -> list[str]:
        assert main(["review", "--pr-url", REF.url, "--no-post"]) == 0
        return capsys.readouterr().out.splitlines()

    def test_json_is_null_when_unset(self, forge, capsys):
        assert self._json(capsys)["size_advisory"] is None

    def test_json_carries_the_stats_from_the_environment(self, forge, capsys, monkeypatch):
        monkeypatch.setenv("PRXREF_SIZE_WARN_LINES", "5")
        assert self._json(capsys)["size_advisory"] == TRIGGERED
        assert forge.summaries == []

    def test_the_glob_variable_reaches_the_count(self, forge, capsys, monkeypatch):
        monkeypatch.setenv("PRXREF_SIZE_WARN_FILES", "0")
        monkeypatch.setenv("PRXREF_SIZE_IGNORE_GLOBS", "docs/*, src/*")
        advisory = self._json(capsys)["size_advisory"]
        assert (advisory["changed_files"], advisory["triggered"]) == (0, False)

    def test_text_prints_the_advisory_line(self, forge, capsys, monkeypatch):
        monkeypatch.setenv("PRXREF_SIZE_WARN_LINES", "5")
        lines = self._text(capsys)
        assert lines[:2] == ["verdict: Approved", f"size advisory: {TRIGGERED['message']}"]

    @pytest.mark.parametrize("env", [{}, {"PRXREF_SIZE_WARN_LINES": "500"}])
    def test_text_is_silent_when_not_triggered(self, forge, capsys, monkeypatch, env):
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        assert not any(line.startswith("size advisory:") for line in self._text(capsys))

    def test_fail_on_any_still_exits_zero_on_a_triggered_advisory(self, forge, capsys, monkeypatch):
        monkeypatch.setenv("PRXREF_SIZE_WARN_FILES", "0")
        monkeypatch.setenv("PRXREF_FAIL_ON", "any")
        assert self._json(capsys)["size_advisory"]["triggered"] is True
