"""Unit tests for src/prxref/heuristics.py: the release-shaped-PR heuristic.

Most tests here call ``_is_release_machinery`` / ``release_shape_findings``
directly over hand-built ``FileDiff`` lists -- no orchestrator, no LLM, no
diff parsing. One integration test at the bottom confirms the finding
survives the real ``orchestrate_review`` quality-pass chain, reusing
test_orchestrator's shared fixtures the way tests/test_issue_10_release_shaped_pr.py
does.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_orchestrator import (  # noqa: E402  (shared fixtures, read not guessed)
    REF,
    FakeForge,
    FakeLLM,
    _contract_review_chunk,
    _contract_review_systemic,
)

from prxref import orchestrator  # noqa: E402
from prxref.heuristics import _is_release_machinery, release_shape_findings  # noqa: E402
from prxref.orchestrator import orchestrate_review  # noqa: E402
from prxref.triage import FileDiff  # noqa: E402


def _fd(path: str, status: str = "modified") -> FileDiff:
    """One minimal FileDiff for path-only matching -- hunks are irrelevant here."""
    old = None if status == "added" else path
    new = None if status == "removed" else path
    return FileDiff(path=path, old_path=old, new_path=new, status=status)


def _modified_file_diff(path: str) -> str:
    """One small modified-file diff section: one hunk, one changed line."""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,1 @@\n"
        "-old value\n"
        "+new value\n"
    )


class TestMachineryBasenames:
    """Every basename class named in the frozen contract, matched on its own."""

    @pytest.mark.parametrize("path", [
        "package.json",
        "pyproject.toml",
        "Cargo.toml",
        "setup.py",
        "setup.cfg",
        "VERSION",
        "version.py",
        "__version__.py",
        "widget.gemspec",
        "nested/dir/widget.gemspec",
        ".release-please-manifest.json",
    ])
    def test_exact_and_suffix_basenames_are_machinery(self, path):
        assert _is_release_machinery(path) is True

    @pytest.mark.parametrize("path", [
        "CHANGELOG.md", "changelog.md", "CHANGELOG", "CHANGELOG.rst",
        "HISTORY.txt", "history.rst", "History.md",
        "RELEASE_NOTES.md", "release_notes.txt", "Release_Notes.md",
    ])
    def test_changelog_history_release_notes_prefixes_case_insensitive(self, path):
        assert _is_release_machinery(path) is True

    @pytest.mark.parametrize("path", [
        "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml",
        "yarn.lock", "bun.lockb", "bun.lock", "uv.lock", "poetry.lock",
        "Pipfile.lock", "Cargo.lock", "Gemfile.lock", "go.sum", "composer.lock",
    ])
    def test_lockfile_basenames_are_machinery(self, path):
        assert _is_release_machinery(path) is True

    @pytest.mark.parametrize("path", [
        ".changeset/brave-foxes.md",
        ".changeset/README.md",
        "packages/cli/.changeset/some-slug.md",
    ])
    def test_changeset_directory_component_is_machinery_regardless_of_filename(self, path):
        assert _is_release_machinery(path) is True

    @pytest.mark.parametrize("path", [
        "src/app.py",
        "README.md",
        "notchangelog.md",  # does not start with the "changelog" prefix
        "package.json.bak",  # basename mismatch: not exactly "package.json"
        "changeset.md",  # ".changeset" is a filename here, not a directory
    ])
    def test_non_machinery_paths_are_not_machinery(self, path):
        assert _is_release_machinery(path) is False


class TestReleaseShapeRatio:
    """The >= 2 files / >= 80% machinery contract, exact-rational throughout."""

    def test_single_file_diff_never_release_shaped_even_if_pure_machinery(self):
        assert release_shape_findings([_fd("package.json")]) == []

    def test_ratio_exactly_80_percent_is_release_shaped(self):
        # 4 machinery + 1 source = 5 files; 4/5 = 80% exactly (boundary
        # inclusive per the contract's ">=").
        files = [
            _fd("package.json"), _fd("pyproject.toml"),
            _fd("Cargo.toml"), _fd("setup.py"),
            _fd("src/app.py"),
        ]
        findings = release_shape_findings(files)
        assert len(findings) == 1
        assert findings[0].file == "src/app.py"

    def test_ratio_just_under_80_percent_is_not_release_shaped(self):
        # 79/100: 79*5=395 < 100*4=400. Integers only, no float involved.
        machinery = [_fd(f"pkg{i}/package.json") for i in range(79)]
        source = [_fd(f"src/mod{i}.py") for i in range(21)]
        assert release_shape_findings(machinery + source) == []

    def test_no_float_drift_across_equivalent_80_percent_ratios(self):
        """4/5 and 12/15 are the same ratio (80%) but different integers.

        The contract requires ``machinery * 5 >= total * 4`` (exact
        rational, cross-multiplied) rather than a computed
        ``machinery / total >= 0.8`` float comparison, so both expressions
        of the same ratio must classify identically -- a naive float
        division is exactly the failure mode this guards against.
        """
        four_of_five = [
            _fd("package.json"), _fd("pyproject.toml"),
            _fd("Cargo.toml"), _fd("setup.py"),
            _fd("src/app.py"),
        ]
        twelve_of_fifteen = (
            [_fd(f"pkg{i}/package.json") for i in range(12)]
            + [_fd(f"src/mod{i}.py") for i in range(3)]
        )
        assert len(release_shape_findings(four_of_five)) == 1
        result = release_shape_findings(twelve_of_fifteen)
        assert len(result) == 1
        assert "3 non-release file" in result[0].title


class TestRemovedFilesAndOffenderSorting:
    def test_removed_non_machinery_file_counts_as_offender(self):
        files = [
            _fd("package.json"), _fd("pyproject.toml"), _fd("Cargo.toml"),
            _fd("setup.py"), _fd("src/old_module.py", status="removed"),
        ]
        findings = release_shape_findings(files)
        assert len(findings) == 1
        assert findings[0].file == "src/old_module.py"
        assert "1 non-release file" in findings[0].title

    def test_removed_machinery_file_counts_toward_total(self):
        # 4 machinery (one removed) / 5 total = 80%; exactly 1 offender.
        files = [
            _fd("package.json"), _fd("pyproject.toml"), _fd("Cargo.toml"),
            _fd(".changeset/brave-foxes.md", status="removed"),
            _fd("src/app.py"),
        ]
        findings = release_shape_findings(files)
        assert len(findings) == 1
        assert findings[0].file == "src/app.py"

    def test_offenders_sorted_first_offender_anchors_finding_and_body(self):
        files = [
            _fd("package.json"), _fd("pyproject.toml"), _fd("Cargo.toml"),
            _fd("setup.py"), _fd("setup.cfg"), _fd("VERSION"),
            _fd("version.py"), _fd("__version__.py"), _fd("widget.gemspec"),
            _fd("CHANGELOG.md"), _fd(".release-please-manifest.json"),
            _fd("Gemfile.lock"),
            _fd("src/z_module.py"), _fd("src/a_module.py"),
        ]
        # 12 machinery + 2 source = 14 files, 12/14 = 85.7% >= 80%.
        findings = release_shape_findings(files)
        assert len(findings) == 1
        f = findings[0]
        assert f.file == "src/a_module.py"
        assert "src/a_module.py" in f.body
        assert "src/z_module.py" in f.body
        assert f.body.index("src/a_module.py") < f.body.index("src/z_module.py")
        assert f.body.endswith(" (deterministic check, no model)")


class TestFindingShape:
    def test_finding_shape_matches_contract(self):
        files = [
            _fd("package.json"), _fd("pyproject.toml"), _fd("Cargo.toml"),
            _fd("setup.py"), _fd("src/app.py"),
        ]
        findings = release_shape_findings(files)
        assert len(findings) == 1
        f = findings[0]
        assert f.severity == "warning"
        assert f.confidence == 1.0
        assert f.line == 0
        assert f.drop_reason is None
        assert f.title == "Release-shaped PR touches source: 1 non-release file(s)"

    def test_pure_release_pr_yields_no_finding(self):
        files = [
            _fd("package.json"), _fd("pyproject.toml"),
            _fd("Cargo.toml"), _fd("setup.py"),
        ]
        assert release_shape_findings(files) == []

    def test_ordinary_feature_pr_with_incidental_manifest_bump_yields_no_finding(self):
        files = [_fd(f"src/mod{i}.py") for i in range(1, 6)] + [_fd("package.json")]
        assert release_shape_findings(files) == []


class TestOrchestratorIntegration:
    """The finding must survive the full quality-pass chain and post as a
    summary item under ``post=False`` -- not just exist as heuristics output."""

    def test_release_shape_finding_survives_full_pass_chain(self, monkeypatch):
        monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _contract_review_chunk)
        monkeypatch.setattr(
            orchestrator.reviewer, "review_systemic", _contract_review_systemic,
        )

        diff = "\n".join(
            _modified_file_diff(p)
            for p in [
                "package.json", "pyproject.toml", "Cargo.toml", "setup.py",
                "src/app.py",
            ]
        )
        forge = FakeForge(diff=diff)
        llm = FakeLLM('{"findings":[],"escalations":[]}')

        res = orchestrate_review(forge, REF, llm, post=False)

        active = res["findings_active"]
        release_findings = [
            f for f in active
            if f.title.startswith("Release-shaped PR touches source:")
        ]
        assert len(release_findings) == 1
        f = release_findings[0]
        assert f.file == "src/app.py"
        assert f.line == 0
        assert f.severity == "warning"
        assert f.confidence == 1.0
        assert f.drop_reason is None
