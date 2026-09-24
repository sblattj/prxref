"""Eval case loading (issue #14, T1): ``prxref.eval_cases.load_cases``.

``--cases`` is a ``cases.json`` file or a directory of ``case-*/`` dirs. Both
forms load into the same frozen records; the directory form maps
``line_hint`` onto ``line`` and ``source`` onto ``category``; the repo's own
``tests/evals`` dataset loads whole; and every bad case is a ``ConfigError``
whose message names ``--cases``, the case and the field, so the CLI can exit 2.
All identities are placeholders.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from prxref.eval_cases import (
    HUMAN_SEVERITIES,
    EvalCase,
    ExpectedFinding,
    check_anchors,
    load_cases,
)
from prxref.llm import ConfigError
from tests.evals.test_evals import CASE_DIRS, EVALS_DIR

DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,3 @@\n"
    " import os\n"
    "+import sys\n"
    " print(os.name)\n"
)
OTHER_DIFF = DIFF.replace("src/app.py", "src/other.py")
PR_URL = "https://github.com/acme/widgets/pull/7"
BASE = "a" * 40
HEAD = "b" * 40
DROP = object()


def _finding(**over) -> dict:
    entry = {"id": "H1", "file": "src/app.py", "line": 2, "severity": "error"}
    entry.update(over)
    return {key: value for key, value in entry.items() if value is not DROP}


def _case(**over) -> dict:
    entry = {"id": "case-a", "diff_file": "change.diff", "expected": [_finding()]}
    entry.update(over)
    return {key: value for key, value in entry.items() if value is not DROP}


def _dataset(tmp_path: Path, cases, *, version=1) -> Path:
    (tmp_path / "change.diff").write_text(DIFF, encoding="utf-8")
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"version": version, "cases": cases}), encoding="utf-8")
    return path


def _refusal(path, **kwargs) -> str:
    with pytest.raises(ConfigError) as exc:
        load_cases(path, **kwargs)
    return str(exc.value)


def _case_dir(root: Path, name: str, expected, *, diff: str = DIFF, ticket=True, docs=True) -> Path:
    case_dir = root / name
    case_dir.mkdir(parents=True)
    (case_dir / "diff.patch").write_text(diff, encoding="utf-8")
    (case_dir / "expected.json").write_text(json.dumps(expected), encoding="utf-8")
    if ticket:
        (case_dir / "ticket.md").write_text("## Summary\nplaceholder ticket\n", encoding="utf-8")
    if docs:
        (case_dir / "docs").mkdir()
        (case_dir / "docs" / "rules.md").write_text("Imports MUST be sorted.\n", encoding="utf-8")
    return case_dir


def _dir_finding(**over) -> dict:
    entry = {
        "id": "S1", "file": "src/app.py", "line_hint": 2, "severity": "spec",
        "must_match": "sys", "source": "spec",
    }
    entry.update(over)
    return {key: value for key, value in entry.items() if value is not DROP}


class TestCasesJson:
    def test_a_diff_file_case_loads_with_paths_joined_onto_the_dataset_dir(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "ticket.md").write_text("placeholder\n", encoding="utf-8")
        path = _dataset(tmp_path, [_case(
            context_file="ticket.md",
            spec=["docs", "https://docs.example.com/spec"],
            expected=[_finding(
                category="security", accepted=True, text="unused import", must_match="re:sys|unused",
            )],
        )])

        (case,) = load_cases(path)

        assert case == EvalCase(
            id="case-a",
            expected=(ExpectedFinding(
                id="H1", file="src/app.py", line=2, severity="error", category="security",
                accepted=True, text="unused import", must_match="re:sys|unused",
            ),),
            diff_file=str(tmp_path / "change.diff"),
            context_file=str(tmp_path / "ticket.md"),
            spec=(str(tmp_path / "docs"), "https://docs.example.com/spec"),
        )

    def test_a_pinned_pr_case_loads_with_lowercased_shas_and_no_diff(self, tmp_path):
        path = _dataset(tmp_path, [_case(
            diff_file=DROP, pr_url=PR_URL, base_sha=BASE.upper(), head_sha=HEAD,
            expected=[_finding(file="src/not-in-any-local-diff.py", line=40)],
        )])

        (case,) = load_cases(path)

        assert (case.pr_url, case.base_sha, case.head_sha, case.diff_file) == (PR_URL, BASE, HEAD, None)
        assert case.expected[0].line == 40

    def test_64_character_shas_are_accepted(self, tmp_path):
        path = _dataset(tmp_path, [_case(
            diff_file=DROP, pr_url=PR_URL, base_sha="c" * 64, head_sha="d" * 64,
        )])

        (case,) = load_cases(path)

        assert (case.base_sha, case.head_sha) == ("c" * 64, "d" * 64)

    def test_pr_url_beside_a_diff_file_needs_no_shas(self, tmp_path):
        (case,) = load_cases(_dataset(tmp_path, [_case(pr_url=PR_URL)]))

        assert (case.pr_url, case.base_sha, case.head_sha) == (PR_URL, None, None)
        assert case.diff_file == str(tmp_path / "change.diff")

    def test_null_optional_fields_count_as_absent(self, tmp_path):
        path = _dataset(tmp_path, [_case(
            pr_url=None, base_sha=None, head_sha=None, context_file=None, spec=None,
            expected=[_finding(category=None, accepted=None, text=None, must_match=None)],
        )])

        (case,) = load_cases(path)

        assert case == EvalCase(
            id="case-a",
            expected=(ExpectedFinding(id="H1", file="src/app.py", line=2, severity="error"),),
            diff_file=str(tmp_path / "change.diff"),
        )

    def test_a_single_spec_string_becomes_a_one_item_tuple(self, tmp_path):
        (tmp_path / "docs").mkdir()

        (case,) = load_cases(_dataset(tmp_path, [_case(spec="docs")]))

        assert case.spec == (str(tmp_path / "docs"),)

    def test_an_absolute_path_is_kept_as_given(self, tmp_path):
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "ticket.md").write_text("placeholder\n", encoding="utf-8")
        dataset = tmp_path / "dataset"
        dataset.mkdir()

        (case,) = load_cases(_dataset(dataset, [_case(context_file=str(outside / "ticket.md"))]))

        assert case.context_file == str(outside / "ticket.md")

    def test_a_relative_cases_path_keeps_the_case_paths_relative(self, tmp_path, monkeypatch):
        _dataset(tmp_path, [_case()])
        monkeypatch.chdir(tmp_path)

        (case,) = load_cases("cases.json")

        assert case.diff_file == "change.diff"

    def test_every_human_severity_is_stored_as_given(self, tmp_path):
        expected = [
            _finding(id=f"H{i}", severity=severity) for i, severity in enumerate(HUMAN_SEVERITIES)
        ]

        (case,) = load_cases(_dataset(tmp_path, [_case(expected=expected)]))

        assert [f.severity for f in case.expected] == ["error", "warning", "minor", "spec", "outofscope"]

    def test_an_empty_expected_list_is_a_clean_case(self, tmp_path):
        (case,) = load_cases(_dataset(tmp_path, [_case(expected=[])]))

        assert case.expected == ()

    def test_cases_keep_file_order(self, tmp_path):
        cases = [_case(id=name) for name in ("zeta", "alpha", "mid.1_x")]

        loaded = load_cases(_dataset(tmp_path, cases))

        assert [case.id for case in loaded] == ["zeta", "alpha", "mid.1_x"]

    def test_the_records_are_frozen(self, tmp_path):
        (case,) = load_cases(_dataset(tmp_path, [_case()]))

        with pytest.raises(dataclasses.FrozenInstanceError):
            case.id = "other"
        with pytest.raises(dataclasses.FrozenInstanceError):
            case.expected[0].line = 3
        assert isinstance(case.expected, tuple)
        assert isinstance(case.spec, tuple)


class TestDirectoryForm:
    def test_case_dirs_load_sorted_with_fields_mapped(self, tmp_path):
        _case_dir(tmp_path, "case-b", [_dir_finding()])
        _case_dir(tmp_path, "case-a", [_dir_finding(id="G1", severity="error", source="generic")],
                  ticket=False, docs=False)
        (tmp_path / "notes").mkdir()
        (tmp_path / "case-file.txt").write_text("not a case\n", encoding="utf-8")

        loaded = load_cases(tmp_path)

        assert loaded == [
            EvalCase(
                id="case-a",
                expected=(ExpectedFinding(
                    id="G1", file="src/app.py", line=2, severity="error", category="generic",
                    must_match="sys",
                ),),
                diff_file=str(tmp_path / "case-a" / "diff.patch"),
            ),
            EvalCase(
                id="case-b",
                expected=(ExpectedFinding(
                    id="S1", file="src/app.py", line=2, severity="spec", category="spec",
                    must_match="sys",
                ),),
                diff_file=str(tmp_path / "case-b" / "diff.patch"),
                context_file=str(tmp_path / "case-b" / "ticket.md"),
                spec=(str(tmp_path / "case-b" / "docs"),),
            ),
        ]

    def test_accepted_and_text_are_optional_in_expected_json(self, tmp_path):
        _case_dir(tmp_path, "case-a", [_dir_finding(accepted=False, text="placeholder", must_match=DROP, source=DROP)])

        (case,) = load_cases(str(tmp_path))

        assert case.expected == (ExpectedFinding(
            id="S1", file="src/app.py", line=2, severity="spec", accepted=False, text="placeholder",
        ),)

    def test_the_repo_eval_dataset_loads_whole(self):
        loaded = load_cases(EVALS_DIR)

        assert [case.id for case in loaded] == [p.name for p in CASE_DIRS]
        assert len(loaded) >= 3
        for case, case_dir in zip(loaded, CASE_DIRS, strict=True):
            labels = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
            assert case.diff_file == str(case_dir / "diff.patch")
            assert case.context_file == str(case_dir / "ticket.md")
            assert case.spec == (str(case_dir / "docs"),)
            assert (case.pr_url, case.base_sha, case.head_sha) == (None, None, None)
            assert [
                (f.id, f.file, f.line, f.severity, f.category, f.must_match) for f in case.expected
            ] == [
                (e["id"], e["file"], e["line_hint"], e["severity"], e["source"], e["must_match"])
                for e in labels
            ]
        assert any(f.severity == "spec" for case in loaded for f in case.expected)


class TestCasesJsonRefusals:
    @pytest.mark.parametrize(
        ("case", "field"),
        [
            (_case(diff_fle="change.diff"), "diff_fle"),
            (_case(diff_file=DROP), "pr_url/diff_file"),
            (_case(diff_file=DROP, pr_url=PR_URL), "base_sha/head_sha"),
            (_case(diff_file=DROP, pr_url=PR_URL, base_sha=BASE), "head_sha"),
            (_case(diff_file=DROP, pr_url=PR_URL, head_sha=HEAD), "base_sha"),
            (_case(diff_file=DROP, pr_url=PR_URL, base_sha="abc1234", head_sha=HEAD), "base_sha"),
            (_case(diff_file=DROP, pr_url=PR_URL, base_sha=BASE, head_sha="g" * 40), "head_sha"),
            (_case(diff_file=DROP, pr_url=PR_URL, base_sha=BASE, head_sha=BASE.upper()), "base_sha/head_sha"),
            (_case(base_sha=BASE, head_sha=HEAD), "pr_url"),
            (_case(pr_url="https://example.com/acme/widgets"), "pr_url"),
            (_case(pr_url=7), "pr_url"),
            (_case(pr_url=" "), "pr_url"),
            (_case(diff_file="missing.diff"), "diff_file"),
            (_case(diff_file=["change.diff"]), "diff_file"),
            (_case(context_file="missing.md"), "context_file"),
            (_case(spec="missing-docs"), "spec"),
            (_case(spec=3), "spec"),
            (_case(spec=["missing-docs"]), "spec[0]"),
            (_case(spec=["https://docs.example.com/spec", 4]), "spec[1]"),
            (_case(expected=DROP), "expected"),
            (_case(expected={"id": "H1"}), "expected"),
            (_case(expected=["H1"]), "expected[0]"),
            (_case(expected=[_finding(line_hint=2, line=DROP)]), "expected[0].line_hint"),
            (_case(expected=[_finding(id=DROP)]), "expected[0].id"),
            (_case(expected=[_finding(id="")]), "expected[0].id"),
            (_case(expected=[_finding(), _finding()]), "expected[1].id"),
            (_case(expected=[_finding(file=DROP)]), "expected[0].file"),
            (_case(expected=[_finding(file=3)]), "expected[0].file"),
            (_case(expected=[_finding(line=DROP)]), "expected[0].line"),
            (_case(expected=[_finding(line=0)]), "expected[0].line"),
            (_case(expected=[_finding(line="2")]), "expected[0].line"),
            (_case(expected=[_finding(line=True)]), "expected[0].line"),
            (_case(expected=[_finding(line=2.0)]), "expected[0].line"),
            (_case(expected=[_finding(severity=DROP)]), "expected[0].severity"),
            (_case(expected=[_finding(severity="critical")]), "expected[0].severity"),
            (_case(expected=[_finding(severity="Error")]), "expected[0].severity"),
            (_case(expected=[_finding(severity=None)]), "expected[0].severity"),
            (_case(expected=[_finding(category=3)]), "expected[0].category"),
            (_case(expected=[_finding(category="")]), "expected[0].category"),
            (_case(expected=[_finding(accepted="yes")]), "expected[0].accepted"),
            (_case(expected=[_finding(text=5)]), "expected[0].text"),
            (_case(expected=[_finding(must_match="")]), "expected[0].must_match"),
            (_case(expected=[_finding(must_match="re:(")]), "expected[0].must_match"),
            (_case(expected=[_finding(must_match="re:")]), "expected[0].must_match"),
            (_case(expected=[_finding(file="src/other.py")]), "expected[0].file"),
            (_case(expected=[_finding(line=1)]), "expected[0].line"),
            (_case(expected=[_finding(), _finding(id="H2", line=3)]), "expected[1].line"),
            (_case(expected=[_finding(line=99)]), "expected[0].line"),
        ],
    )
    def test_a_bad_case_names_the_flag_the_case_and_the_field(self, tmp_path, case, field):
        message = _refusal(_dataset(tmp_path, [case]))

        assert message.startswith(f"--cases: case 'case-a': {field}: "), message

    def test_an_empty_diff_file_is_refused(self, tmp_path):
        (tmp_path / "empty.diff").write_text("", encoding="utf-8")

        message = _refusal(_dataset(tmp_path, [_case(diff_file="empty.diff")]))

        assert message.startswith("--cases: case 'case-a': diff_file: "), message
        assert "holds no file diffs" in message

    def test_a_label_off_the_added_lines_says_which_line_and_file(self, tmp_path):
        message = _refusal(_dataset(tmp_path, [_case(expected=[_finding(line=3)])]))

        assert message == (
            "--cases: case 'case-a': expected[0].line: line 3 is not a line the diff adds to 'src/app.py'"
        )

    def test_the_second_bad_case_is_named_after_a_good_one(self, tmp_path):
        cases = [_case(), _case(id="case-b", expected=[_finding(severity="blocker")])]

        message = _refusal(_dataset(tmp_path, cases))

        assert message.startswith("--cases: case 'case-b': expected[0].severity: must be one of "), message
        assert "error, warning, minor, spec, outofscope" in message

    @pytest.mark.parametrize("bad_id", [DROP, "", "../escape", ".hidden", "a/b", "has space", 7, None])
    def test_a_bad_case_id_is_named_by_position(self, tmp_path, bad_id):
        message = _refusal(_dataset(tmp_path, [_case(), _case(id=bad_id)]))

        assert message.startswith("--cases: cases[1]: id: "), message

    def test_a_case_that_is_not_an_object_is_named_by_position(self, tmp_path):
        message = _refusal(_dataset(tmp_path, [_case(), "case-b"]))

        assert message == "--cases: cases[1]: must be an object, got the string 'case-b'"

    def test_a_duplicate_case_id_is_refused(self, tmp_path):
        message = _refusal(_dataset(tmp_path, [_case(), _case(id="case-b"), _case()]))

        assert message == "--cases: case 'case-a': id: duplicate of cases[0] (cases[2])"

    def test_the_source_label_leads_every_message(self, tmp_path):
        message = _refusal(_dataset(tmp_path, [_case(diff_file=DROP)]), source="--eval-cases")

        assert message.startswith("--eval-cases: case 'case-a': pr_url/diff_file: "), message


class TestCasesFileRefusals:
    def test_a_missing_path_is_refused(self, tmp_path):
        missing = tmp_path / "nope.json"

        message = _refusal(missing)

        assert message.startswith(f"--cases: cannot read {str(missing)!r}: "), message

    def test_invalid_json_is_refused_with_its_position(self, tmp_path):
        path = tmp_path / "cases.json"
        path.write_text('{"version": 1,\n "cases": [}', encoding="utf-8")

        message = _refusal(path)

        assert message.startswith(f"--cases: {str(path)!r} is not valid JSON ("), message
        assert "line 2" in message

    def test_non_utf8_is_refused(self, tmp_path):
        path = tmp_path / "cases.json"
        path.write_bytes(b'{"version": 1, "cases": ["\xff"]}')

        assert _refusal(path) == f"--cases: {str(path)!r} is not valid UTF-8"

    @pytest.mark.parametrize(
        ("document", "fragment"),
        [
            ([], 'must be an object like {"version": 1, "cases": [...]}, got an array'),
            ({"version": 1, "cases": [_case()], "extra": 1}, "extra: unknown field; expected version and cases"),
            ({"cases": [_case()]}, "version: must be 1, got null"),
            ({"version": 2, "cases": [_case()]}, "version: must be 1, got the number 2"),
            ({"version": True, "cases": [_case()]}, "version: must be 1, got the boolean true"),
            ({"version": 1.0, "cases": [_case()]}, "version: must be 1, got the number 1.0"),
            ({"version": "1", "cases": [_case()]}, "version: must be 1, got the string '1'"),
            ({"version": 1}, "cases: must be a non-empty array, got null"),
            ({"version": 1, "cases": []}, "cases: must be a non-empty array, got an array"),
            ({"version": 1, "cases": {"case-a": {}}}, "cases: must be a non-empty array, got an object"),
        ],
    )
    def test_a_bad_top_level_names_the_file_and_the_field(self, tmp_path, document, fragment):
        (tmp_path / "change.diff").write_text(DIFF, encoding="utf-8")
        path = tmp_path / "cases.json"
        path.write_text(json.dumps(document), encoding="utf-8")

        assert _refusal(path) == f"--cases: {str(path)!r}: {fragment}"


class TestDirectoryRefusals:
    def test_a_directory_without_case_dirs_is_refused(self, tmp_path):
        (tmp_path / "notes").mkdir()

        message = _refusal(tmp_path)

        assert message.startswith(f"--cases: {str(tmp_path)!r} has no case-*/ directories"), message

    def test_a_missing_diff_patch_is_refused(self, tmp_path):
        case_dir = _case_dir(tmp_path, "case-a", [_dir_finding()])
        (case_dir / "diff.patch").unlink()

        message = _refusal(tmp_path)

        assert message.startswith("--cases: case 'case-a': diff.patch: cannot read "), message

    def test_a_missing_expected_json_is_refused(self, tmp_path):
        case_dir = _case_dir(tmp_path, "case-a", [_dir_finding()])
        (case_dir / "expected.json").unlink()

        message = _refusal(tmp_path)

        assert message.startswith("--cases: case 'case-a': expected.json: cannot read "), message

    def test_invalid_expected_json_is_refused(self, tmp_path):
        case_dir = _case_dir(tmp_path, "case-a", [_dir_finding()])
        (case_dir / "expected.json").write_text("[{", encoding="utf-8")

        message = _refusal(tmp_path)

        assert message.startswith("--cases: case 'case-a': expected.json: "), message
        assert "is not valid JSON" in message

    @pytest.mark.parametrize(
        ("expected", "field"),
        [
            ({"id": "S1"}, "expected.json"),
            ([_dir_finding(line=2, line_hint=DROP)], "expected.json[0].line"),
            ([_dir_finding(category="spec", source=DROP)], "expected.json[0].category"),
            ([_dir_finding(line_hint=DROP)], "expected.json[0].line_hint"),
            ([_dir_finding(line_hint=0)], "expected.json[0].line_hint"),
            ([_dir_finding(source=3)], "expected.json[0].source"),
            ([_dir_finding(must_match="re:[")], "expected.json[0].must_match"),
            ([_dir_finding(), _dir_finding()], "expected.json[1].id"),
            ([_dir_finding(line_hint=1)], "expected.json[0].line_hint"),
            ([_dir_finding(file="src/other.py")], "expected.json[0].file"),
        ],
    )
    def test_a_bad_label_is_named_in_the_directory_spelling(self, tmp_path, expected, field):
        _case_dir(tmp_path, "case-a", expected)

        message = _refusal(tmp_path)

        assert message.startswith(f"--cases: case 'case-a': {field}: "), message

    def test_a_directory_name_that_is_not_a_safe_id_is_refused(self, tmp_path):
        _case_dir(tmp_path, "case-a b", [_dir_finding()])

        message = _refusal(tmp_path)

        assert message.startswith("--cases: case 'case-a b': id: "), message


class TestCheckAnchors:
    def test_a_pinned_case_can_be_checked_against_its_fetched_diff(self, tmp_path):
        (case,) = load_cases(_dataset(tmp_path, [_case(
            diff_file=DROP, pr_url=PR_URL, base_sha=BASE, head_sha=HEAD,
        )]))

        check_anchors(case, DIFF)

        with pytest.raises(ConfigError) as exc:
            check_anchors(case, OTHER_DIFF)
        assert str(exc.value) == (
            "--cases: case 'case-a': expected[0].file: 'src/app.py' is not a file the diff touches"
        )

    def test_a_label_on_a_context_line_fails_the_check(self):
        case = EvalCase(
            id="case-a",
            expected=(ExpectedFinding(id="H1", file="src/app.py", line=1, severity="minor"),),
        )

        with pytest.raises(ConfigError) as exc:
            check_anchors(case, DIFF, source="--label")
        assert str(exc.value).startswith("--label: case 'case-a': expected[0].line: line 1 "), str(exc.value)
