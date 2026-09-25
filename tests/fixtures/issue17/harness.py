"""The issue #17 acceptance harness: a read-and-list fake forge, a capturing LLM, one review.

It is version-neutral on purpose. ``make_golden.py`` runs it against the
RELEASED prxref 0.15.0 and ``tests/test_issue_17_acceptance.py`` runs it
against the tree, so it imports only API that exists in both:
``orchestrate_review``, ``PRRef``, ``PRData`` and ``InvokeResult``.
``FixtureForge.list_paths`` imports ``PathListing`` only when it is called,
and 0.15.0 never calls it.

The forge serves ``pr.diff`` (or any diff) over a directory tree plus an
optional in-memory overlay, and records every ``get_file_content`` path and
every ``list_paths`` call. The LLM records every ``(system, user)`` prompt,
worker and sweep alike, and returns no findings.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from prxref.forges.base import PRData, PRRef
from prxref.llm import InvokeResult
from prxref.orchestrator import orchestrate_review

FIXTURE = Path(__file__).resolve().parent
REPO = FIXTURE / "repo"
DIFF = (FIXTURE / "pr.diff").read_text(encoding="utf-8")
HEAD_SHA = "5eed17c0ffee17c0ffee17c0ffee17c0ffee1717"
BASE_SHA = "ba5e17c0ffee17c0ffee17c0ffee17c0ffee1717"
NO_FINDINGS = '{"findings": []}'

REF = PRRef(
    forge="fake", host="example.com", owner="acme", repo="connectors",
    number=17, url="https://example.com/acme/connectors/pull/17",
)

PY_TREE = {
    "tools/sync.py": (
        '"""Sync helpers."""\n'
        "import requests\n"
        "\n"
        "\n"
        "def load_config(path):\n"
        '    """Read the sync settings."""\n'
        '    return {"path": path}\n'
        "\n"
        "\n"
        "def unrelated():\n"
        "    return None\n"
        "\n"
        "\n"
        "def sync(path):\n"
        "    settings = load_config(path)\n"
        '    return requests.get(settings["path"], timeout=5)\n'
    ),
    "pyproject.toml": (
        "[project]\n"
        'name = "acme-connectors-tools"\n'
        'version = "0.1.0"\n'
        'dependencies = ["requests==2.32.3"]\n'
    ),
}

PY_DIFF = (
    "diff --git a/tools/sync.py b/tools/sync.py\n"
    "--- a/tools/sync.py\n"
    "+++ b/tools/sync.py\n"
    "@@ -1,2 +1,3 @@\n"
    ' """Sync helpers."""\n'
    "+import requests\n"
    " \n"
    "@@ -13,3 +14,3 @@\n"
    " def sync(path):\n"
    '-    settings = {"path": path}\n'
    "-    return settings\n"
    "+    settings = load_config(path)\n"
    '+    return requests.get(settings["path"], timeout=5)\n'
)

DESIGN_POINTS = {
    "fixture": (DIFF, {}),
    "fixture+python": (DIFF + PY_DIFF, PY_TREE),
}


def make_pr() -> PRData:
    """The fixture PR: fixed title, description, author, branches and 40-hex shas."""
    return PRData(
        title="Create connector transports",
        description="Adds POST /connectors/{connectorId}/transports and a unique index on idempotency keys.",
        author="acme-dev",
        source_branch="feature/transports",
        target_branch="main",
        source_sha=HEAD_SHA,
        target_sha=BASE_SHA,
        raw={},
    )


class FixtureForge:
    """A read-and-list forge over ``root`` plus ``overlay`` (path to text), recording every read and listing."""

    name = "fake"

    def __init__(
        self, diff: str = DIFF, root: str | os.PathLike[str] = REPO, overlay: dict[str, str] | None = None,
    ):
        self.diff = diff
        self.root = Path(root)
        self.overlay = dict(overlay or {})
        self.content_calls: list[str] = []
        self.content_shas: set[str] = set()
        self.list_calls = 0
        self._lock = threading.Lock()

    @staticmethod
    def parse_pr_url(url: str) -> PRRef | None:
        """Never recognizes a URL."""
        return None

    def get_pr(self, ref: PRRef) -> PRData:
        """The fixture PR."""
        return make_pr()

    def get_diff(self, ref: PRRef) -> str:
        """The diff text, unmodified."""
        return self.diff

    def get_file_content(self, ref: PRRef, path: str, *, sha: str) -> str | None:
        """The overlay's text for ``path``, else the tree's, else None; every call is recorded."""
        with self._lock:
            self.content_calls.append(path)
            self.content_shas.add(sha)
        if path in self.overlay:
            return self.overlay[path]
        target = self.root / path
        return target.read_text(encoding="utf-8") if target.is_file() else None

    def all_paths(self) -> tuple[str, ...]:
        """Every file under ``root`` (dot files included) plus the overlay's paths, sorted."""
        found = set(self.overlay)
        for folder, _dirs, names in os.walk(self.root):
            for name in names:
                found.add(Path(folder, name).relative_to(self.root).as_posix())
        return tuple(sorted(found))

    def list_paths(self, ref: PRRef, *, sha: str):
        """A complete ``PathListing`` of :meth:`all_paths`; every call is counted."""
        from prxref.forges.base import PathListing

        with self._lock:
            self.list_calls += 1
        return PathListing(paths=self.all_paths(), complete=True)

    def list_threads(self, ref: PRRef) -> list:
        """Always empty."""
        return []

    def post_summary(self, ref: PRRef, body: str) -> None:
        """Always raises: the harness never posts."""
        raise RuntimeError("the issue #17 harness never posts")

    def post_inline_comments(self, ref: PRRef, comments) -> int:
        """Always raises: the harness never posts."""
        raise RuntimeError("the issue #17 harness never posts")


class CapturingLLM:
    """Records every ``(system, user)`` prompt, worker and sweep, and returns no findings."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=None):
        """Record the prompt and answer ``{"findings": []}``."""
        with self._lock:
            self.calls.append((system, user))
        return InvokeResult(
            text=NO_FINDINGS, input_tokens=10, output_tokens=5,
            model="fake-model", backend="fake", elapsed_ms=1,
        )


def review(forge=None, llm=None, *, ref: PRRef = REF, **kwargs):
    """One ``orchestrate_review`` with ``post=False`` and ``max_files_per_chunk=1`` unless overridden.

    Returns ``(result, forge, llm)``.
    """
    forge = forge if forge is not None else FixtureForge()
    llm = llm if llm is not None else CapturingLLM()
    kwargs.setdefault("max_files_per_chunk", 1)
    result = orchestrate_review(forge, ref, llm, post=False, **kwargs)
    return result, forge, llm


def golden_run(point: str, **kwargs) -> dict:
    """One design point's ordered prompts and forge traffic, with ``max_workers=1``.

    ``{"prompts": [{"system", "user"}, ...], "content_calls": [...], "list_calls": int}``
    """
    diff, overlay = DESIGN_POINTS[point]
    forge = FixtureForge(diff, overlay=overlay)
    _result, forge, llm = review(forge, max_workers=1, **kwargs)
    return {
        "prompts": [{"system": system, "user": user} for system, user in llm.calls],
        "content_calls": list(forge.content_calls),
        "list_calls": forge.list_calls,
    }
