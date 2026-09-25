"""The issue #20 acceptance harness: a read-and-list fake forge, a capturing LLM, one review.

It is version-neutral on purpose. ``make_golden.py`` runs it against the
RELEASED prxref 0.16.0 and ``tests/test_issue_20_acceptance.py`` runs it
against the tree, so it imports only API that exists in both:
``orchestrate_review``, ``PRRef``, ``PRData`` and ``InvokeResult``.
``FixtureForge.list_paths`` imports ``PathListing`` only when it is called.

The fixture is a small monorepo under ``repo/``. ``shop/`` is a Maven build: a
parent ``pom.xml`` with properties and a BOM import, a ``shop-api`` module
whose ``jackson-databind`` dependency has no version, and a ``shop-legacy``
module whose ``pom.xml`` is malformed. ``inventory/`` is a Gradle build with a
``libs.versions.toml`` catalog and a ``reports`` subproject whose
``build.gradle`` is garbage. ``tools/`` is a Python control. ``pr.diff``
changes seven source files, one per chunk.

The forge serves the diff over the tree plus an optional in-memory overlay and
records every ``get_file_content`` path and every ``list_paths`` call. The LLM
records every ``(system, user)`` prompt, worker and sweep alike, and returns no
findings.
"""
from __future__ import annotations

import difflib
import os
import threading
from pathlib import Path

from prxref.forges.base import PRData, PRRef
from prxref.llm import InvokeResult
from prxref.orchestrator import orchestrate_review

FIXTURE = Path(__file__).resolve().parent
REPO = FIXTURE / "repo"
DIFF = (FIXTURE / "pr.diff").read_text(encoding="utf-8")
HEAD_SHA = "5eed20c0ffee20c0ffee20c0ffee20c0ffee2020"
BASE_SHA = "ba5e20c0ffee20c0ffee20c0ffee20c0ffee2020"
NO_FINDINGS = '{"findings": []}'
MAX_CHUNKS = 32

REF = PRRef(
    forge="fake", host="example.com", owner="example", repo="shop",
    number=20, url="https://example.com/example/shop/pull/20",
)

ORDER_MAPPER = "shop/shop-api/src/main/java/com/example/shop/api/OrderMapper.java"
PRICE_CALCULATOR = "shop/shop-api/src/main/java/com/example/shop/pricing/PriceCalculator.java"
LEGACY_EXPORT = "shop/shop-legacy/src/main/java/com/example/shop/legacy/LegacyExport.java"
STOCK_CLIENT = "inventory/src/main/java/com/example/inventory/StockClient.java"
STOCK_REPORT = "inventory/src/main/kotlin/com/example/inventory/StockReport.kt"
REPORT_WRITER = "inventory/reports/src/main/java/com/example/reports/ReportWriter.java"
EXPORT_ORDERS = "tools/export_orders.py"
CHANGED = (
    ORDER_MAPPER, PRICE_CALCULATOR, LEGACY_EXPORT, STOCK_CLIENT, STOCK_REPORT, REPORT_WRITER, EXPORT_ORDERS,
)

DESIGN_POINTS = {
    "fixture": (DIFF, {}),
}

CAPS_PATH = "caps/src/main/java/com/example/caps/Limits.java"
CAPS_CONSTANTS = 45


def _caps_text(body: list[str]) -> str:
    lines = ["package com.example.caps;", "", "public class Limits {", ""]
    for n in range(1, CAPS_CONSTANTS + 1):
        lines += ["    @Deprecated", f"    public static final int LIMIT_{n:02d} = {n};", ""]
    lines += ["    /**", "     * The sum of every limit.", "     */", "    public int sum() {"]
    lines += body
    lines += ["    }", "}"]
    return "\n".join(lines) + "\n"


def caps_design() -> tuple[str, dict[str, str]]:
    """A one-file diff whose added lines reference ``CAPS_CONSTANTS`` annotated constants outside the hunk.

    Returns ``(diff, overlay)``: the overlay serves the new ``Limits.java``.
    """
    names = [f"LIMIT_{n:02d}" for n in range(1, CAPS_CONSTANTS + 1)]
    body = ["        int total = 0;"]
    for start in range(0, len(names), 5):
        body.append("        total += " + " + ".join(names[start:start + 5]) + ";")
    body.append("        return total;")
    base = _caps_text(["        return 0;"])
    head = _caps_text(body)
    lines = difflib.unified_diff(
        base.splitlines(keepends=True), head.splitlines(keepends=True),
        fromfile=f"a/{CAPS_PATH}", tofile=f"b/{CAPS_PATH}", n=3,
    )
    return f"diff --git a/{CAPS_PATH} b/{CAPS_PATH}\n" + "".join(lines), {CAPS_PATH: head}


def make_pr() -> PRData:
    """The fixture PR: fixed title, description, author, branches and 40-hex shas."""
    return PRData(
        title="Serialize orders and stock levels as JSON",
        description="Adds Jackson to the shop and inventory modules and tightens the price total.",
        author="example-dev",
        source_branch="feature/json-export",
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
        raise RuntimeError("the issue #20 harness never posts")

    def post_inline_comments(self, ref: PRRef, comments) -> int:
        """Always raises: the harness never posts."""
        raise RuntimeError("the issue #20 harness never posts")


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
    """One ``orchestrate_review`` with ``post=False``, one file per chunk and ``MAX_CHUNKS`` unless overridden.

    Returns ``(result, forge, llm)``.
    """
    forge = forge if forge is not None else FixtureForge()
    llm = llm if llm is not None else CapturingLLM()
    kwargs.setdefault("max_files_per_chunk", 1)
    kwargs.setdefault("max_chunks", MAX_CHUNKS)
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
