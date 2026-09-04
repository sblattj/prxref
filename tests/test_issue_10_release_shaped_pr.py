"""Issue 10: flag a release-shaped PR that also touches source files.

prxref returned zero findings on a release PR whose branch quietly carried
the commits of a different, still-open PR — diff-visible without any repo
archaeology: every changed file except two was release machinery (version
bumps, CHANGELOG, lockfile, a consumed changeset), and the two remaining
files were source.

Frozen contract under test:

- Release machinery basenames: package.json, pyproject.toml, Cargo.toml,
  setup.py, setup.cfg, VERSION, version.py, __version__.py, *.gemspec,
  CHANGELOG*/HISTORY*/RELEASE_NOTES* (any case), lockfiles (package-lock.json,
  pnpm-lock.yaml, yarn.lock, bun.lockb, bun.lock, uv.lock, poetry.lock,
  Pipfile.lock, Cargo.lock, Gemfile.lock, go.sum, composer.lock), anything
  under a ``.changeset/`` directory, and ``.release-please-manifest.json``.
- A PR is release-shaped when it changes >= 2 files and >= 80% of them are
  release machinery.
- When a release-shaped PR ALSO changes any non-machinery file, the pipeline
  emits exactly ONE finding: severity "warning", confidence 1.0, file = the
  first offending file (sorted), line 0 (file-level — no single diff hunk
  owns a whole-PR-shape claim), title
  "Release-shaped PR touches source: <N> non-release file(s)", body listing
  every offending path on its own line and ending with
  " (deterministic check, no model)". It must survive the normal quality
  passes.
- Pure release PRs and ordinary feature PRs emit no such finding.

This is a deterministic (non-LLM) heuristic: today NOTHING in the pipeline
produces a finding without an LLM call, so every test below that expects a
finding fails until a ``heuristics`` module (or equivalent) is wired into
``orchestrate_review``. The FakeLLM below always answers with an empty
findings payload — including for the systemic sweep, which the shared
``_contract_review_systemic`` stub short-circuits to ``[]`` regardless of
what it is asked — so any finding that appears is not model output.
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
from prxref.orchestrator import orchestrate_review  # noqa: E402

# An empty-but-valid worker payload: the chunk contract parses this as zero
# findings, and the sweep is stubbed below to ignore its input entirely — so
# together the model contributes nothing to any test in this file.
EMPTY_LLM = FakeLLM('{"findings":[],"escalations":[]}')


@pytest.fixture
def contract_stubs(monkeypatch):
    """Chunk + sweep pinned to the orchestrator contract (no real reviewer)."""
    monkeypatch.setattr(orchestrator.reviewer, "review_chunk", _contract_review_chunk)
    monkeypatch.setattr(
        orchestrator.reviewer, "review_systemic", _contract_review_systemic,
    )


def _modified_file_diff(path: str, old: str = "old value", new: str = "new value") -> str:
    """One small modified-file diff section: one hunk, one changed line."""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,1 +1,1 @@\n"
        f"-{old}\n"
        f"+{new}\n"
    )


def _deleted_file_diff(path: str, line: str = "consumed changeset") -> str:
    """One deleted-file diff section (e.g. a changeset consumed by a release)."""
    return (
        f"diff --git a/{path} b/{path}\n"
        "deleted file mode 100644\n"
        f"--- a/{path}\n"
        "+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n"
        f"-{line}\n"
    )


# Nine distinct release-machinery basenames (one is a deleted changeset) so
# the ratio clears the >=80% bar with margin even after two source files are
# added: 9 / 11 = 81.8%. A literal 5-machinery/2-source split (the issue's
# own worked example) sits at 5/7 = 71.4%, BELOW the frozen 80% threshold —
# recomputed here rather than copied, see the verify report's Eval section.
MACHINERY_PATHS = [
    "package.json",
    "CHANGELOG.md",
    "pnpm-lock.yaml",
    "packages/cli/package.json",
    "VERSION",
    "Cargo.toml",
    "setup.py",
    "RELEASE_NOTES.md",
]
DELETED_CHANGESET = ".changeset/brave-foxes.md"

SOURCE_PATHS = [
    "src/apps/cli/src/config/json-file.ts",
    "src/apps/cli/src/config/json-file.test.ts",
]
# Sorted order matters: the contract anchors the finding on the first
# offending file, sorted.
SOURCE_PATHS_SORTED = sorted(SOURCE_PATHS)


def _release_shaped_diff(*, include_source: bool) -> str:
    parts = [_modified_file_diff(p) for p in MACHINERY_PATHS]
    parts.append(_deleted_file_diff(DELETED_CHANGESET))
    if include_source:
        parts.extend(_modified_file_diff(p) for p in SOURCE_PATHS)
    return "\n".join(parts)


def _feature_diff_with_one_manifest() -> str:
    """5 source files + 1 manifest bump: 1/6 = 16.7% machinery, not release-shaped."""
    parts = [_modified_file_diff(f"src/mod{i}.py") for i in range(1, 6)]
    parts.append(_modified_file_diff("package.json"))
    return "\n".join(parts)


def _single_manifest_diff() -> str:
    """One file changed: fails the >= 2 files half of the contract outright."""
    return _modified_file_diff("package.json")


class TestReleaseShapedPRTouchesSource:
    """Test A: must FAIL today (no deterministic finding exists yet)."""

    def test_release_shaped_pr_with_two_source_files_raises_one_finding(
        self, contract_stubs,
    ):
        forge = FakeForge(diff=_release_shaped_diff(include_source=True))
        res = orchestrate_review(forge, REF, EMPTY_LLM, post=False)

        active = res["findings_active"]
        assert len(active) == 1, (
            f"expected exactly one finding, got {len(active)}: {active}"
        )
        f = active[0]

        assert f.title.startswith("Release-shaped PR touches source:")
        assert "2 non-release file" in f.title
        assert f.severity == "warning"
        assert f.confidence == 1.0
        assert f.line == 0
        assert f.file == SOURCE_PATHS_SORTED[0]
        assert f.drop_reason is None

        # Both offending paths named in the body. Containment rather than
        # exact-own-line matching: the contract says each path is on its own
        # line AND the body ends with " (deterministic check, no model)" —
        # if that suffix is appended to the last path's line rather than a
        # line of its own, a strict per-line match would false-negative on
        # an otherwise-compliant implementation.
        for path in SOURCE_PATHS:
            assert path in f.body, f"{path!r} missing from body:\n{f.body}"

        assert f.body.endswith(" (deterministic check, no model)")


class TestControls:
    """Diffs that must never raise the release-shaped finding — now or after
    the fix lands."""

    def test_pure_release_pr_no_source_files_yields_no_finding(self, contract_stubs):
        forge = FakeForge(diff=_release_shaped_diff(include_source=False))
        res = orchestrate_review(forge, REF, EMPTY_LLM, post=False)
        assert res["findings_active"] == []

    def test_ordinary_feature_pr_with_incidental_manifest_bump_yields_no_finding(
        self, contract_stubs,
    ):
        forge = FakeForge(diff=_feature_diff_with_one_manifest())
        res = orchestrate_review(forge, REF, EMPTY_LLM, post=False)
        assert res["findings_active"] == []

    def test_single_manifest_file_diff_yields_no_finding(self, contract_stubs):
        forge = FakeForge(diff=_single_manifest_diff())
        res = orchestrate_review(forge, REF, EMPTY_LLM, post=False)
        assert res["findings_active"] == []
