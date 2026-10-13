"""Issues 104 and 105: the CI wiring check's false negatives and positives.

#104: an Azure Pipelines definition kept in a folder (``azure-pipelines/ci.yml``)
was never found, so every check-shaped file got its own "no CI job runs it"
warning. #105: the check-shaped heuristic fired on docs, JSON, test files and
library modules, which are not checks.
"""
from __future__ import annotations

import pytest

from prxref import ci_wiring, config
from prxref.triage import parse_unified_diff


def _added(path: str, lines: list[str]) -> str:
    body = "\n".join(f"+{line}" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n"
        f"+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}\n"
    )


def _files(*diffs: str):
    return parse_unified_diff("".join(diffs))


def _candidates(*diffs: str):
    return ci_wiring.candidate_checks(_files(*diffs))


def _paths(*diffs: str) -> list[str]:
    return [c.path for c in _candidates(*diffs)]


# --- #104 ---------------------------------------------------------------------

AZURE_CI = "trigger:\n  - main\nsteps:\n  - script: npm test\n"
REPRO_104 = (
    _added("scripts/foo-check.js", ["console.log('check')", "if (require.main === module) run();"])
    + _added("tests/foo-check.test.js", ["test('x', () => {});"])
)


class TestIssue104:
    @pytest.mark.parametrize("path", [
        "azure-pipelines/ci.yml",
        "azure-pipelines/publish.yaml",
        ".azure-pipelines/ci.yml",
        ".azuredevops/ci.yml",
        ".azuredevops/pipelines/deep/ci.yml",
    ])
    def test_folder_pipelines_are_found(self, path):
        assert ci_wiring.ci_config_paths([path, "src/app.py"], list(ci_wiring.DEFAULT_CI_GLOBS)) == [path]

    def test_the_pipelines_folder_stays_out(self):
        assert ci_wiring.ci_config_paths([".pipelines/ci.yml"], list(ci_wiring.DEFAULT_CI_GLOBS)) == []

    def test_config_default_matches(self):
        assert config._DEFAULTS["ci_wiring_globs"] == list(ci_wiring.DEFAULT_CI_GLOBS)

    def test_the_issue_repro_finds_the_folder_file(self):
        files = _files(REPRO_104)
        listing = ["azure-pipelines/ci.yml", "scripts/foo-check.js"]
        findings, record = ci_wiring.ci_wiring_findings(
            files, read={"azure-pipelines/ci.yml": AZURE_CI}.get, listing=listing,
        )
        assert record["ci_files"] == ["azure-pipelines/ci.yml"]
        assert not any("no CI configuration file was found" in f.body for f in findings)

    def test_no_ci_file_gives_one_note_not_one_warning_per_file(self):
        files = _files(
            _added("scripts/a-check.sh", ["#!/bin/sh"]),
            _added("scripts/b-verify.sh", ["#!/bin/sh"]),
            _added("scripts/c-smoke.sh", ["#!/bin/sh"]),
        )
        findings, record = ci_wiring.ci_wiring_findings(
            files, read=lambda p: None, listing=["scripts/a-check.sh"],
        )
        assert len(findings) == 1
        assert "set ci_wiring_globs" in findings[0].title
        assert "no CI configuration file was found" in findings[0].body
        assert record["ci_files"] == []
        assert len(record["candidates"]) == 3

    def test_a_found_ci_file_still_flags_each_unwired_check(self):
        files = _files(
            _added("scripts/a-check.sh", ["#!/bin/sh"]),
            _added("scripts/b-verify.sh", ["#!/bin/sh"]),
        )
        findings, _ = ci_wiring.ci_wiring_findings(
            files, read={"azure-pipelines/ci.yml": AZURE_CI}.get,
            listing=["azure-pipelines/ci.yml"],
        )
        assert len(findings) == 2
        assert all("no CI job runs it" in f.title for f in findings)


# --- #105 ---------------------------------------------------------------------


class TestIssue105:
    def test_a_real_check_script_is_still_flagged(self):
        assert _paths(_added("scripts/verify.sh", ["#!/usr/bin/env bash", "echo ok"])) == [
            "scripts/verify.sh"
        ]

    def test_a_shebang_only_extensionless_script_is_still_flagged(self):
        assert _paths(_added("tools/migrate", ["#!/usr/bin/env python3"])) == ["tools/migrate"]

    def test_a_flag_defined_in_an_argument_parser_is_still_flagged(self):
        [c] = _candidates(_added("tools/run.py", ["p.add_argument('--check', action='store_true')"]))
        assert "--check" in c.reason

    @pytest.mark.parametrize("path", [
        "docs/CHANGELOG.md", "docs/feature.md", "docs/notes.rst", "docs/notes.txt",
        "cli/commands.json", "config/verify.yml", "data/check.csv", "README",
    ])
    def test_prose_and_data_never_gain_a_check_flag_or_name(self, path):
        text = ["`plan --check` exits 1 when the plan drifts", '"--check": "fail on drift"']
        assert _paths(_added(path, text)) == []

    def test_a_json_file_named_verify_is_not_a_check(self):
        assert _paths(_added("schemas/verify.json", ["{}"])) == []

    def test_a_help_string_that_mentions_a_flag_in_code_is_not_a_definition(self):
        lines = ['print("run `tool plan --check` to see drift")', "// handles --check later"]
        assert _paths(_added("src/help.js", lines)) == []

    @pytest.mark.parametrize("path", [
        "tests/foo-check.test.js", "tests/foo-verify.test.js", "test/verify_test.py",
        "src/__tests__/check.ts", "src/check.spec.ts", "pkg/verify_test.go",
    ])
    def test_runner_collected_tests_are_not_checks(self, path):
        assert _paths(_added(path, ["test('x', () => {})", "p.add_argument('--check')"])) == []

    def test_a_shell_script_under_tests_is_still_a_check(self):
        assert _paths(_added("tests/smoke.sh", ["#!/bin/sh"])) == ["tests/smoke.sh"]

    def test_a_test_file_outside_the_default_include_is_still_flagged(self):
        [c] = _candidates(_added("src/App.tests.tsx", ["x"]))
        assert "default include" in c.reason

    def test_a_markdown_file_named_like_a_test_is_not_flagged(self):
        assert _paths(_added("docs/test-plan.md", ["x"])) == []

    def test_the_issue_repro_library_tests_and_changelog(self):
        diffs = (
            _added("lib/verify.js", ["exports.verify = () => true;"]),
            _added("cli.js", ["const { verify } = require('./lib/verify');"]),
            _added("tests/verify.test.js", ["require('../lib/verify');"]),
            _added("CHANGELOG.md", ["- `tool verify --check` now exits 1"]),
        )
        assert _paths(*diffs) == []

    def test_a_module_with_an_entry_point_is_not_a_library(self):
        diffs = (
            _added("lib/verify.js", ["if (require.main === module) main();"]),
            _added("cli.js", ["const v = require('./lib/verify');"]),
        )
        assert _paths(*diffs) == ["lib/verify.js"]

    def test_an_unimported_module_without_entry_point_is_still_flagged_by_name(self):
        assert _paths(_added("lib/verify.js", ["exports.verify = () => true;"])) == ["lib/verify.js"]

    def test_a_python_library_imported_by_a_sibling_is_not_a_check(self):
        diffs = (
            _added("pkg/verify.py", ["def verify():", "    return True"]),
            _added("pkg/cli.py", ["from pkg.verify import verify"]),
        )
        assert _paths(*diffs) == []

    def test_a_shell_case_flag_is_a_definition(self):
        [c] = _candidates(_added("bin/run.sh", ["case $1 in", "  -c|--check) mode=check;;", "esac"]))
        assert "--check" in c.reason

    def test_a_docs_only_pr_end_to_end_raises_nothing(self):
        files = _files(
            _added("docs/CHANGELOG.md", ["tool verify --check"]),
            _added("cli/commands.json", ['{"help": "plan --check exits 1"}']),
        )
        findings, record = ci_wiring.ci_wiring_findings(files, read=lambda p: None, listing=None)
        assert findings == []
        assert record["candidates"] == []
