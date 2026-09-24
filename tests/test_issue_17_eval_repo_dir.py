"""The eval-case half of ``--repo-dir`` (issue #17, T15a, OQ4).

``repo_dir`` is an optional :class:`~prxref.eval_cases.EvalCase` field: a
directory holding the repository at the PR head. Repository context
(``PRXREF_REPO_CONTEXT=repo``) reads and lists files there instead of
calling a forge. This module only loads, validates, joins and round-trips
the field; the CLI flag, the threading into ``evals._run_case`` and the
orchestrator are T15b's job, and neither is exercised here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from prxref import cli
from prxref.eval_cases import (
    EvalCase,
    ExpectedFinding,
    case_from_json_record,
    case_to_json,
    load_cases,
)
from prxref.llm import ConfigError

DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,3 @@\n"
    " import os\n"
    "+import sys\n"
    " print(os.name)\n"
)
DROP = object()
FIXTURE = Path(__file__).parent / "fixtures" / "issue17"


def _finding(**over) -> dict:
    entry = {"id": "H1", "file": "src/app.py", "line": 2, "severity": "error"}
    entry.update(over)
    return {key: value for key, value in entry.items() if value is not DROP}


def _dir_finding(**over) -> dict:
    entry = {"id": "H1", "file": "src/app.py", "line_hint": 2, "severity": "error"}
    entry.update(over)
    return {key: value for key, value in entry.items() if value is not DROP}


def _case(**over) -> dict:
    entry = {"id": "case-a", "diff_file": "change.diff", "expected": [_finding()]}
    entry.update(over)
    return {key: value for key, value in entry.items() if value is not DROP}


def _dataset(tmp_path: Path, cases) -> Path:
    (tmp_path / "change.diff").write_text(DIFF, encoding="utf-8")
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"version": 1, "cases": cases}), encoding="utf-8")
    return path


def _refusal(path) -> str:
    with pytest.raises(ConfigError) as exc:
        load_cases(path)
    return str(exc.value)


class TestCasesJsonRepoDir:
    def test_a_relative_repo_dir_is_joined_onto_the_dataset_directory(self, tmp_path):
        (tmp_path / "repo").mkdir()
        path = _dataset(tmp_path, [_case(repo_dir="repo")])

        (case,) = load_cases(path)

        assert case.repo_dir == str(tmp_path / "repo")

    def test_an_absolute_repo_dir_is_kept_as_given(self, tmp_path):
        repo = tmp_path / "elsewhere" / "repo"
        repo.mkdir(parents=True)
        path = _dataset(tmp_path, [_case(repo_dir=str(repo))])

        (case,) = load_cases(path)

        assert case.repo_dir == str(repo)

    def test_a_missing_repo_dir_is_refused_naming_the_case_and_field(self, tmp_path):
        message = _refusal(_dataset(tmp_path, [_case(repo_dir="missing")]))

        assert message.startswith("--cases: case 'case-a': repo_dir: no such directory "), message

    def test_a_repo_dir_that_is_a_file_is_refused(self, tmp_path):
        (tmp_path / "repo-file").write_text("not a directory\n", encoding="utf-8")

        message = _refusal(_dataset(tmp_path, [_case(repo_dir="repo-file")]))

        assert message.startswith("--cases: case 'case-a': repo_dir: no such directory "), message

    def test_a_non_string_repo_dir_is_rejected_by_optional_text(self, tmp_path):
        message = _refusal(_dataset(tmp_path, [_case(repo_dir=3)]))

        assert message == (
            "--cases: case 'case-a': repo_dir: must be a non-empty string or null, "
            "got the number 3"
        )

    def test_a_null_repo_dir_counts_as_absent(self, tmp_path):
        path = _dataset(tmp_path, [_case(repo_dir=None)])

        (case,) = load_cases(path)

        assert case.repo_dir is None

    def test_repo_dir_may_sit_beside_context_file_and_diff_file(self, tmp_path):
        (tmp_path / "repo").mkdir()
        (tmp_path / "ticket.md").write_text("placeholder\n", encoding="utf-8")
        path = _dataset(tmp_path, [_case(repo_dir="repo", context_file="ticket.md")])

        (case,) = load_cases(path)

        assert case.repo_dir == str(tmp_path / "repo")
        assert case.context_file == str(tmp_path / "ticket.md")
        assert case.diff_file == str(tmp_path / "change.diff")

    def test_the_unknown_field_message_lists_repo_dir_in_its_position(self, tmp_path):
        message = _refusal(_dataset(tmp_path, [_case(bogus_field=1)]))

        assert (
            "allowed: id, pr_url, base_sha, head_sha, diff_file, context_file, repo_dir, "
            "spec, expected" in message
        )


class TestDirectoryFormRepoDir:
    def test_a_repo_subdirectory_becomes_repo_dir(self, tmp_path):
        case_dir = tmp_path / "case-a"
        case_dir.mkdir()
        (case_dir / "diff.patch").write_text(DIFF, encoding="utf-8")
        (case_dir / "expected.json").write_text(json.dumps([_dir_finding()]), encoding="utf-8")
        (case_dir / "repo").mkdir()

        (case,) = load_cases(tmp_path)

        assert case.repo_dir == str(case_dir / "repo")

    def test_a_case_directory_without_repo_gives_none(self, tmp_path):
        case_dir = tmp_path / "case-a"
        case_dir.mkdir()
        (case_dir / "diff.patch").write_text(DIFF, encoding="utf-8")
        (case_dir / "expected.json").write_text(json.dumps([_dir_finding()]), encoding="utf-8")

        (case,) = load_cases(tmp_path)

        assert case.repo_dir is None


class TestRepoDirRoundTrip:
    def _case_with_repo_dir(self) -> EvalCase:
        return EvalCase(
            id="case-a",
            expected=(ExpectedFinding(id="H1", file="src/app.py", line=2, severity="error"),),
            diff_file="/data/cases/change.diff",
            context_file="/data/cases/ticket.md",
            spec=("/data/cases/docs",),
            repo_dir="/data/cases/repo",
        )

    def test_repo_dir_round_trips_when_set(self):
        case = self._case_with_repo_dir()

        assert case_from_json_record(case_to_json(case)) == case

    def test_repo_dir_round_trips_when_unset(self):
        case = EvalCase(id="case-a", expected=(), diff_file="/data/cases/change.diff")
        assert case.repo_dir is None

        assert case_from_json_record(case_to_json(case)) == case

    def test_a_0_15_shaped_record_with_no_repo_dir_key_still_loads(self):
        record = {
            "id": "case-a",
            "pr_url": None,
            "base_sha": None,
            "head_sha": None,
            "diff_file": "/data/cases/change.diff",
            "context_file": None,
            "spec": [],
            "expected": [],
        }

        case = case_from_json_record(record)

        assert case.repo_dir is None
        assert case.diff_file == "/data/cases/change.diff"


class TestFixtureRepoDir:
    def test_the_issue17_fixture_case_gets_a_repo_dir_pointing_at_the_committed_repo(self):
        (case,) = load_cases(str(FIXTURE / "cases.json"))

        assert case.repo_dir is not None
        assert case.repo_dir.endswith(str(Path("tests") / "fixtures" / "issue17" / "repo"))
        assert Path(case.repo_dir).is_dir()


class TestCliExit2ThroughRealEntryPoint:
    def test_a_bad_repo_dir_exits_2_naming_the_field_without_monkeypatching(self, tmp_path, capsys):
        cases = _dataset(tmp_path, [_case(repo_dir="missing")])

        code = cli.main(["eval", "run", "--cases", str(cases), "--label", "L"])

        assert code == 2
        err = capsys.readouterr().err
        assert "configuration error:" in err
        assert "repo_dir" in err
