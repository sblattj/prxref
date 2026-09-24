"""Repository hygiene: local issue bundles never enter the tracked tree or the sdist.

``docs/issues/`` holds issue bundles written during local triage. Three guards
keep them out of anything published: ``.gitignore`` ignores the directory, the
sdist build config excludes it, and ``test_docs_issues_is_not_tracked`` fails if
a force-add tracked something there anyway.

The git checks skip where there is no checkout to inspect: no ``git``
executable, or a tree that is not a git checkout of this project (an unpacked
sdist, for example).
"""
from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ISSUES_DIR = "docs/issues"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True, text=True, check=False,
    )


@pytest.fixture
def checkout() -> Path:
    """The project root when it is a git checkout of this project; skips otherwise."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    top = _git("rev-parse", "--show-toplevel")
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != ROOT:
        pytest.skip("not a git checkout of this project")
    return ROOT


def test_docs_issues_is_not_tracked(checkout):
    docs = _git("ls-files", "--", "docs")
    assert docs.returncode == 0, docs.stderr
    assert docs.stdout.strip(), "git ls-files lists no tracked docs, so the check below proves nothing"

    listed = _git("ls-files", "--", ISSUES_DIR)
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout == ""


def test_docs_issues_is_gitignored(checkout):
    assert _git("check-ignore", "-q", "--", f"{ISSUES_DIR}/any-bundle.md").returncode == 0
    assert _git("check-ignore", "-q", "--", "docs/any-page.md").returncode == 1


def test_sdist_excludes_docs_issues():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    sdist = config["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert ISSUES_DIR in sdist["exclude"]
