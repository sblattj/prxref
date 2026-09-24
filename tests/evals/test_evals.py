"""Golden offline eval dataset: loader, schema validation, structural scorer.

Each ``case-*`` directory under ``tests/evals/`` is a self-contained eval case
for the spec-grounded review pipeline (``--spec``): a Jira-style ticket
(``ticket.md``), a small normative spec corpus (``docs/``), a unified diff
implementing the ticket with at least one planted spec violation
(``diff.patch``), machine-checkable expected findings (``expected.json``), and
case metadata (``meta.json``). See ``README.md`` for the full case format.

This module deliberately runs NO LLM. It proves the dataset is well-formed and
self-consistent: every case parses with the production diff parser
(:func:`prxref.triage.parse_unified_diff`), both JSON schemas hold, every
planted violation in ``meta.json`` maps to exactly one ``spec``-sourced entry
in ``expected.json`` and vice versa, and every expected finding anchors on a
line the diff actually adds. Each case also runs through the real pipeline
with one replay-mode CLI call; ``test_eval_replay.py`` does that offline with
a stub LLM that finds nothing, so it proves the wiring, not the review.
Scoring a real run's findings against ``expected.json`` is still a manual,
offline step; ``must_match`` is the acceptance predicate for that step (plain
substring, or a regex when prefixed ``re:``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from prxref.triage import FileDiff, parse_unified_diff

EVALS_DIR = Path(__file__).resolve().parent
SEVERITY_VOCABULARY = frozenset({"error", "warning", "outofscope", "spec"})
SOURCE_VOCABULARY = frozenset({"spec", "generic"})
EXPECTED_KEYS = frozenset(
    {"id", "file", "line_hint", "severity", "must_match", "source"}
)
META_KEYS = frozenset({"id", "title", "source_prs", "planted_violations", "notes"})
META_REQUIRED_KEYS = frozenset({"id", "title", "planted_violations", "notes"})
VIOLATION_KEYS = frozenset({"id", "description", "expected_ref"})
CASE_DIRS = sorted(
    p for p in EVALS_DIR.iterdir() if p.is_dir() and p.name.startswith("case-")
)


@dataclass
class EvalCase:
    """One loaded eval case: metadata, expected findings, parsed diff."""

    directory: Path
    meta: dict
    expected: list[dict]
    files: list[FileDiff]

    @property
    def file_map(self) -> dict[str, FileDiff]:
        return {f.path: f for f in self.files}


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(params=CASE_DIRS, ids=[p.name for p in CASE_DIRS])
def case(request) -> EvalCase:
    directory = request.param
    files = parse_unified_diff((directory / "diff.patch").read_text(encoding="utf-8"))
    return EvalCase(
        directory=directory,
        meta=_read_json(directory / "meta.json"),
        expected=_read_json(directory / "expected.json"),
        files=files,
    )


def test_dataset_has_cases():
    assert len(CASE_DIRS) >= 3, "eval dataset needs at least three case-* directories"


def test_case_layout(case: EvalCase):
    assert (case.directory / "ticket.md").is_file()
    assert (case.directory / "diff.patch").is_file()
    docs = sorted((case.directory / "docs").glob("*.md"))
    assert docs, "docs corpus is empty"
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        assert "MUST" in text or "FORBIDDEN" in text, doc.name
    ticket = (case.directory / "ticket.md").read_text(encoding="utf-8")
    assert "Acceptance Criteria" in ticket
    assert len(ticket) > 200


def test_diff_parses_with_production_parser(case: EvalCase):
    assert case.files, "diff.patch produced no FileDiff records"
    assert any(f.hunks for f in case.files), "diff.patch contains no hunks"


def test_expected_json_schema(case: EvalCase):
    assert isinstance(case.expected, list) and case.expected
    seen_ids: set[str] = set()
    for entry in case.expected:
        assert set(entry) == EXPECTED_KEYS, entry
        assert isinstance(entry["id"], str) and entry["id"]
        assert isinstance(entry["file"], str) and entry["file"]
        assert isinstance(entry["line_hint"], int)
        assert not isinstance(entry["line_hint"], bool)
        assert entry["line_hint"] >= 1
        assert entry["severity"] in SEVERITY_VOCABULARY
        assert isinstance(entry["must_match"], str) and entry["must_match"]
        assert entry["source"] in SOURCE_VOCABULARY
        assert entry["id"] not in seen_ids, f"duplicate expected id {entry['id']}"
        seen_ids.add(entry["id"])


def test_meta_json_schema(case: EvalCase):
    meta = case.meta
    assert set(meta) <= META_KEYS
    assert META_REQUIRED_KEYS <= set(meta)
    assert meta["id"] == case.directory.name
    assert isinstance(meta["title"], str) and meta["title"]
    assert isinstance(meta["notes"], str) and meta["notes"]
    assert isinstance(meta.get("source_prs", []), list)
    violations = meta["planted_violations"]
    assert isinstance(violations, list) and violations
    violation_ids: set[str] = set()
    for violation in violations:
        assert set(violation) == VIOLATION_KEYS, violation
        assert isinstance(violation["id"], str) and violation["id"]
        assert isinstance(violation["description"], str) and violation["description"]
        assert isinstance(violation["expected_ref"], str) and violation["expected_ref"]
        assert violation["id"] not in violation_ids
        violation_ids.add(violation["id"])


def test_planted_violations_map_to_spec_entries(case: EvalCase):
    by_id = {entry["id"]: entry for entry in case.expected}
    for violation in case.meta["planted_violations"]:
        entry = by_id.get(violation["expected_ref"])
        assert entry is not None, f"unknown expected_ref {violation['expected_ref']}"
        assert entry["source"] == "spec", entry
    spec_ids = {entry["id"] for entry in case.expected if entry["source"] == "spec"}
    referenced = {v["expected_ref"] for v in case.meta["planted_violations"]}
    assert spec_ids, "case has no spec-sourced expected findings"
    assert referenced == spec_ids, "spec entries and planted violations diverge"


def test_expected_files_present_in_diff(case: EvalCase):
    file_map = case.file_map
    for entry in case.expected:
        assert entry["file"] in file_map, entry["file"]


def test_line_hints_anchor_added_lines(case: EvalCase):
    file_map = case.file_map
    for entry in case.expected:
        added = file_map[entry["file"]].added_lines
        assert entry["line_hint"] in added, entry


def test_must_match_patterns_compile(case: EvalCase):
    for entry in case.expected:
        pattern = entry["must_match"]
        if pattern.startswith("re:"):
            re.compile(pattern[3:])
