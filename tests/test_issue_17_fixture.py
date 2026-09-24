"""Structural checks for the issue #17 acceptance fixture (T16).

``tests/fixtures/issue17/`` reproduces the two repository-context misses from
issue #17: a contract file outside the diff (the OpenAPI idempotency-key
uniqueness contract) and a type changed in another diff chunk (the
``TransportConfig`` mutual-exclusion constructor). These tests validate the
fixture itself, not any resolver: ``cases.json`` loads and its labels anchor
on lines the diff adds, the diff chunks the two Java files apart, the OpenAPI
spec and the earlier migrations sit outside the diff, every added diff line
matches the head content committed under ``repo/``, and the fixture text
carries only placeholder identities.
"""
from __future__ import annotations

import re
from pathlib import Path

from prxref import eval_cases
from prxref.triage import build_chunks, parse_unified_diff

FIXTURE = Path(__file__).parent / "fixtures" / "issue17"
REPO = FIXTURE / "repo"

TRANSPORT_CONFIG_PATH = "src/main/java/com/acme/connectors/TransportConfig.java"
CONNECTOR_SERVICE_PATH = "src/main/java/com/acme/connectors/ConnectorService.java"
MIGRATION_003_PATH = "db/changelog/003-idempotency-unique.sql"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+)")
_DOMAIN_RE = re.compile(r"\b[a-zA-Z0-9-]+\.(?:com|org|net|io|dev|co)\b")


def _diff_files():
    diff_text = (FIXTURE / "pr.diff").read_text(encoding="utf-8")
    return parse_unified_diff(diff_text)


def test_cases_json_loads_and_holds_two_error_labels():
    cases = eval_cases.load_cases(str(FIXTURE / "cases.json"))
    assert len(cases) == 1
    case = cases[0]
    assert case.id == "issue17-repo-context"
    assert len(case.expected) == 2
    assert {finding.severity for finding in case.expected} == {"error"}
    assert all(finding.must_match and finding.must_match.startswith(eval_cases.MUST_MATCH_REGEX_PREFIX)
               for finding in case.expected)


def test_labels_anchor_on_the_migration_and_the_transport_config_call():
    cases = eval_cases.load_cases(str(FIXTURE / "cases.json"))
    anchors = {(finding.file, finding.line) for finding in cases[0].expected}
    assert (MIGRATION_003_PATH, 4) in anchors
    assert (CONNECTOR_SERVICE_PATH, 23) in anchors


def test_diff_touches_exactly_the_three_changed_files():
    files = _diff_files()
    assert {f.path for f in files} == {
        MIGRATION_003_PATH,
        TRANSPORT_CONFIG_PATH,
        CONNECTOR_SERVICE_PATH,
    }


def test_chunking_puts_transport_config_and_connector_service_in_different_chunks():
    files = _diff_files()
    chunks = build_chunks(files, max_files_per_chunk=1)
    assert len(chunks) >= 2
    chunk_of = {f.path: index for index, chunk in enumerate(chunks) for f in chunk}
    assert chunk_of[TRANSPORT_CONFIG_PATH] != chunk_of[CONNECTOR_SERVICE_PATH]


def test_openapi_spec_and_earlier_migrations_exist_but_sit_outside_the_diff():
    diff_paths = {f.path for f in _diff_files()}
    outside_the_diff = {
        "api/openapi/connectors.yaml",
        "db/changelog/001-create-connectors.sql",
        "db/changelog/002-create-idempotency-keys.sql",
    }
    assert diff_paths.isdisjoint(outside_the_diff)
    for relative_path in outside_the_diff:
        assert (REPO / relative_path).is_file()


def test_every_added_line_matches_the_repo_head_content():
    for file_diff in _diff_files():
        repo_lines = (REPO / file_diff.path).read_text(encoding="utf-8").splitlines()
        for hunk in file_diff.hunks:
            for line in hunk.lines:
                if line.kind != "+" or line.new_line is None:
                    continue
                index = line.new_line - 1
                assert 0 <= index < len(repo_lines), (
                    f"{file_diff.path}:{line.new_line} is past the end of repo/{file_diff.path}"
                )
                assert repo_lines[index] == line.text, (
                    f"{file_diff.path}:{line.new_line} disagrees with repo/{file_diff.path}"
                )


def test_fixture_text_uses_only_placeholder_identities():
    paths = [FIXTURE / "pr.diff", FIXTURE / "cases.json"]
    paths.extend(sorted(p for p in REPO.rglob("*") if p.is_file()))
    assert len(paths) >= 8
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for match in _EMAIL_RE.finditer(text):
            assert match.group(1) == "example.com", (
                f"{path}: email domain {match.group(1)!r} is not the example.com placeholder"
            )
        for match in _DOMAIN_RE.finditer(text):
            domain = match.group().lower()
            assert domain == "example.com", (
                f"{path}: domain {domain!r} is not the example.com placeholder"
            )
