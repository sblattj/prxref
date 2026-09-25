"""The CLI and eval wiring of repository context (issue #17).

``_run_review`` hands the four repository-context settings and a checked
``--repo-dir`` to ``orchestrate_review``; ``--format json`` carries the run
record's ``repo_context`` right after ``rule_counts``; ``-v`` prints one
``repo context:`` line; and ``prxref eval run`` threads each case's
``repo_dir`` into the review and records the four settings in ``run.json``.
Nothing here touches the network or a real LLM.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
from pathlib import Path

import pytest

from prxref import cli, config, evals
from prxref.eval_cases import EvalCase
from prxref.forges.repo_dir import RepoDir
from prxref.llm import InvokeResult

URL = "https://github.com/org/repo/pull/7"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue17"
REPO = FIXTURE / "repo"
DIFF = FIXTURE / "pr.diff"
CASE_ID = "issue17-repo-context"
FOUR = ("repo_context", "repo_context_max_chars", "context_contract_globs", "context_exclude_globs")

ENTRY = {"path": "api/openapi/connectors.yaml", "line": 6, "symbol": "IdempotencyKey",
         "kind": "contract", "reason": "contract", "chars": 120}
SAMPLE = {
    "mode": "repo", "max_chars": 12000, "contract_globs": ["**/openapi/**"], "exclude_globs": [],
    "reader": "repo-dir", "listing": {"paths": 9, "complete": True}, "reads": 4,
    "read_cap_hit": False, "units": None,
}
TWO_CHUNKS = {
    **SAMPLE,
    "reader": "forge",
    "listing": {"paths": 120, "complete": False},
    "reads": 16,
    "read_cap_hit": True,
    "units": {"chunks": [
        {"entries": [ENTRY, {**ENTRY, "line": 9}], "omitted": 1, "retry_dropped": False},
        {"entries": [{**ENTRY, "kind": "definition", "reason": "import"}], "omitted": 2,
         "retry_dropped": True},
    ]},
}
SPEC = {"sources": 1, "ok": 1, "failed": [], "constraints": 2, "digest_sha256": "ab" * 32}


class _NoFindingsLLM:
    """Answers every review unit with no findings, counting the calls."""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, system, user, *, max_tokens=4096, json_mode=False, timeout_s=60.0):
        self.calls += 1
        return InvokeResult(
            text=json.dumps({"findings": [], "escalations": []}), model="fake", backend="fake",
        )


def _no_network(*args, **kwargs):
    raise AssertionError("repository context wiring must not touch the network")


@pytest.fixture
def recorder(monkeypatch):
    """Replace ``orchestrate_review`` and the LLM factory; return the recorded kwargs."""
    calls: list[dict] = []

    def _orchestrate(**kwargs):
        calls.append(kwargs)
        return {"verdict": "Approved", "findings_active": [], "findings_dropped": []}

    monkeypatch.setattr("prxref.orchestrator.orchestrate_review", _orchestrate)
    monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: object())
    monkeypatch.setattr("requests.Session.request", _no_network)
    return calls


@pytest.fixture
def forge_guard(monkeypatch):
    """Record every ``make_forge`` call and refuse it, so a forge build is visible."""
    built: list = []

    def _make_forge(ref):
        built.append(ref)
        raise AssertionError("make_forge ran")

    monkeypatch.setattr(cli, "make_forge", _make_forge)
    monkeypatch.setattr("prxref.llm_backends.create_llm_client", _no_network)
    monkeypatch.setattr("requests.Session.request", _no_network)
    return built


@pytest.fixture
def stub_llm(monkeypatch):
    llm = _NoFindingsLLM()
    monkeypatch.setenv("PRXREF_LLM_MODELS", "fake")
    monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: llm)
    monkeypatch.setattr("requests.Session.request", _no_network)
    return llm


def _summary(result, *, verbose: bool = True) -> list[str]:
    buf = io.StringIO()
    cli._print_summary(result, 1.0, verbose=verbose, out=buf)
    return buf.getvalue().splitlines()


def _repo_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("repo context:")]


# --------------------------------------------------------------------------- _run_review


class TestTheFourSettingsReachOrchestrate:
    def test_the_defaults_arrive_when_nothing_is_set(self, recorder):
        assert cli.main(["review", "--diff-file", str(DIFF)]) == 0

        (kwargs,) = recorder
        assert kwargs["repo_context"] == "off"
        assert kwargs["repo_context_max_chars"] == 12000
        assert kwargs["context_contract_globs"] == config._DEFAULTS["context_contract_globs"]
        assert len(kwargs["context_contract_globs"]) == 8
        assert list(kwargs["context_exclude_globs"]) == []
        assert kwargs["repo_dir"] is None

    def test_the_environment_values_arrive(self, recorder, monkeypatch):
        monkeypatch.setenv("PRXREF_REPO_CONTEXT", "repo")
        monkeypatch.setenv("PRXREF_REPO_CONTEXT_MAX_CHARS", "5000")
        monkeypatch.setenv("PRXREF_CONTEXT_CONTRACT_GLOBS", "**/api/*.yaml,**/schema/**")
        monkeypatch.setenv("PRXREF_CONTEXT_EXCLUDE_GLOBS", "vendor/**")

        assert cli.main(["review", "--diff-file", str(DIFF)]) == 0

        (kwargs,) = recorder
        assert {key: kwargs[key] for key in FOUR} == {
            "repo_context": "repo",
            "repo_context_max_chars": 5000,
            "context_contract_globs": ["**/api/*.yaml", "**/schema/**"],
            "context_exclude_globs": ["vendor/**"],
        }

    def test_a_bad_level_exits_2_naming_the_variable_before_orchestrate(self, recorder, monkeypatch, capsys):
        monkeypatch.setenv("PRXREF_REPO_CONTEXT", "Repo")

        assert cli.main(["review", "--diff-file", str(DIFF)]) == 2

        assert recorder == []
        assert "PRXREF_REPO_CONTEXT" in capsys.readouterr().err

    def test_the_webhook_passes_no_repo_dir(self, recorder, monkeypatch):
        monkeypatch.setattr("prxref.cli.make_forge", lambda ref: object())

        cli._webhook_handler(URL)

        (kwargs,) = recorder
        assert kwargs["repo_dir"] is None
        assert kwargs["repo_context"] == "off"


class TestRepoDirFlag:
    def test_the_directory_arrives_as_a_repo_dir_at_its_realpath(self, recorder):
        assert cli.main(["review", "--diff-file", str(DIFF), "--repo-dir", str(REPO)]) == 0

        (kwargs,) = recorder
        assert isinstance(kwargs["repo_dir"], RepoDir)
        assert kwargs["repo_dir"].root == os.path.realpath(REPO)

    def test_a_relative_directory_resolves_against_the_working_directory(self, recorder, monkeypatch):
        monkeypatch.chdir(FIXTURE)

        assert cli.main(["review", "--diff-file", "pr.diff", "--repo-dir", "repo"]) == 0

        (kwargs,) = recorder
        assert kwargs["repo_dir"].root == os.path.realpath(REPO)

    def test_it_is_checked_and_passed_while_the_feature_is_off(self, recorder):
        assert cli.main(["review", "--diff-file", str(DIFF), "--repo-dir", str(REPO)]) == 0

        (kwargs,) = recorder
        assert kwargs["repo_context"] == "off"
        assert isinstance(kwargs["repo_dir"], RepoDir)

    @pytest.mark.parametrize("kind", ["missing", "file", "blank"])
    def test_a_path_that_is_not_a_directory_exits_2_before_any_forge(
        self, kind, forge_guard, tmp_path, capsys,
    ):
        path = {
            "missing": str(tmp_path / "nowhere"),
            "file": str(DIFF),
            "blank": "",
        }[kind]

        assert cli.main(["review", "--pr-url", URL, "--repo-dir", path]) == 2

        assert forge_guard == []
        err = capsys.readouterr().err
        assert f"configuration error: --repo-dir: no such directory {path!r}" in err

    def test_control_a_good_directory_gets_as_far_as_the_forge(self, forge_guard, capsys):
        """The guard is live: the same entry point with a real directory builds the forge."""
        assert cli.main(["review", "--pr-url", URL, "--repo-dir", str(REPO)]) == 0

        assert len(forge_guard) == 1
        assert forge_guard[0].url == URL
        assert "review failed: make_forge ran" in capsys.readouterr().err

    def test_it_is_checked_even_while_the_feature_is_off(self, forge_guard, tmp_path):
        assert cli.main(["review", "--pr-url", URL, "--repo-dir", str(tmp_path / "gone")]) == 2
        assert forge_guard == []

    def test_the_parser_documents_the_flag(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["review", "--help"])
        flat = " ".join(capsys.readouterr().out.split())
        assert "--repo-dir PATH" in flat
        assert (
            "a local checkout of the repository at the PR head; with PRXREF_REPO_CONTEXT=repo, "
            "repository context reads and lists files there instead of calling the forge"
        ) in flat


# --------------------------------------------------------------------------- --format json


class TestJsonKey:
    def test_repo_context_rides_right_after_rule_counts(self):
        keys = list(cli._build_json_result({"verdict": "Approved", "repo_context": SAMPLE,
                                            "sampling": {"seed": 1}, "replay": {}}))
        assert keys.index("repo_context") == keys.index("rule_counts") + 1
        assert keys[keys.index("repo_context") + 1:] == ["sampling", "replay"]

    @pytest.mark.parametrize("result", [{}, None, {"verdict": "Approved"}, {"repo_context": None}])
    def test_a_result_without_it_emits_null(self, result):
        payload = cli._build_json_result(result)
        assert "repo_context" in payload
        assert payload["repo_context"] is None

    def test_a_dict_passes_through_unchanged(self):
        payload = cli._build_json_result({"verdict": "Approved", "repo_context": TWO_CHUNKS})
        assert payload["repo_context"] == TWO_CHUNKS
        assert json.loads(json.dumps(payload))["repo_context"] == TWO_CHUNKS

    def test_through_the_entry_point(self, recorder, monkeypatch, capsys):
        monkeypatch.setattr(
            "prxref.orchestrator.orchestrate_review",
            lambda **kwargs: {"verdict": "Approved", "repo_context": SAMPLE},
        )

        assert cli.main(["review", "--diff-file", str(DIFF), "--format", "json"]) == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["repo_context"] == SAMPLE


# --------------------------------------------------------------------------- -v


class TestVerboseLine:
    def test_units_none_prints_zero_totals(self):
        assert _repo_lines(_summary({"verdict": "Approved", "repo_context": SAMPLE})) == [
            "repo context: mode=repo reader=repo-dir listing=9 reads=4 cap_hit=no entries=0 omitted=0",
        ]

    def test_two_chunk_rows_are_summed(self):
        assert _repo_lines(_summary({"verdict": "Approved", "repo_context": TWO_CHUNKS})) == [
            "repo context: mode=repo reader=forge listing=120(partial) reads=16 cap_hit=yes "
            "entries=3 omitted=3",
        ]

    def test_no_reader_and_no_listing_print_dashes(self):
        record = {**SAMPLE, "mode": "diff", "reader": None, "listing": None, "reads": 0}
        assert _repo_lines(_summary({"verdict": "Approved", "repo_context": record})) == [
            "repo context: mode=diff reader=- listing=- reads=0 cap_hit=no entries=0 omitted=0",
        ]

    def test_it_follows_the_spec_line(self):
        lines = _summary({"verdict": "Approved", "spec_grounding": SPEC, "repo_context": SAMPLE})
        spec = next(i for i, line in enumerate(lines) if line.startswith("spec: "))
        assert lines[spec + 1].startswith("repo context: ")
        assert lines[-1].startswith("repo context: ")

    @pytest.mark.parametrize("result", [{"verdict": "Approved", "repo_context": None}, {"verdict": "Approved"}])
    def test_off_prints_no_line(self, result):
        assert _repo_lines(_summary(result)) == []

    def test_without_verbose_there_is_no_line(self):
        assert _repo_lines(_summary({"verdict": "Approved", "repo_context": SAMPLE}, verbose=False)) == []

    def test_through_the_entry_point(self, recorder, monkeypatch, capsys):
        monkeypatch.setattr(
            "prxref.orchestrator.orchestrate_review",
            lambda **kwargs: {"verdict": "Approved", "findings_active": [], "findings_dropped": [],
                              "repo_context": TWO_CHUNKS},
        )

        assert cli.main(["review", "--diff-file", str(DIFF), "-v"]) == 0

        assert _repo_lines(capsys.readouterr().out.splitlines()) == [
            "repo context: mode=repo reader=forge listing=120(partial) reads=16 cap_hit=yes "
            "entries=3 omitted=3",
        ]


# --------------------------------------------------------------------------- eval


def _eval_args(**over) -> argparse.Namespace:
    values = {"resume": False, "rules_file": None, "scoped_rules": None, "prompts_dir": None}
    values.update(over)
    return argparse.Namespace(**values)


class TestEvalWiring:
    @pytest.mark.parametrize("repo_dir", [str(REPO), None])
    def test_run_case_passes_the_cases_repo_dir(self, tmp_path, repo_dir):
        calls: list[dict] = []

        def _review(url, **kwargs):
            calls.append(kwargs)
            return {"verdict": "Approved", "findings_active": [], "findings_dropped": []}

        case = EvalCase(id="a", expected=(), diff_file=str(DIFF), repo_dir=repo_dir)

        line = evals._run_case(
            case, tmp_path / "a", _eval_args(), run_review=_review, build_record=cli._build_json_result,
        )

        assert line == "a: Approved (0 active findings)"
        (kwargs,) = calls
        assert kwargs["repo_dir"] == repo_dir

    def test_a_vanished_repo_dir_is_a_failed_case(self, tmp_path, stub_llm):
        gone = str(tmp_path / "gone")
        case = EvalCase(id="a", expected=(), diff_file=str(DIFF), repo_dir=gone)

        line = evals._run_case(
            case, tmp_path / "a", _eval_args(), run_review=cli._run_review,
            build_record=cli._build_json_result,
        )

        error = f"ConfigError: --repo-dir: no such directory {gone!r}"
        assert line == f"a: failed: {error}"
        assert json.loads((tmp_path / "a" / "error.json").read_text(encoding="utf-8")) == {
            "case_id": "a", "error": error,
        }
        assert stub_llm.calls == 0

    def test_run_config_keys_end_with_the_four_settings(self):
        assert evals.RUN_CONFIG_KEYS[-4:] == FOUR
        assert evals.RUN_CONFIG_KEYS.index("repo_context") == len(evals.RUN_CONFIG_KEYS) - 4


class TestFixtureEvalEndToEnd:
    def _run(self, tmp_path: Path) -> tuple[int, Path]:
        out = tmp_path / "out"
        code = cli.main(["eval", "run", "--cases", str(FIXTURE / "cases.json"), "--label", "L",
                         "--out", str(out)])
        return code, out / "L"

    def test_repo_mode_reads_the_cases_repo_dir(self, tmp_path, stub_llm, monkeypatch, capsys, caplog):
        monkeypatch.setenv("PRXREF_REPO_CONTEXT", "repo")

        with caplog.at_level(logging.WARNING):
            code, run_dir = self._run(tmp_path)

        assert code == 0
        assert capsys.readouterr().out.splitlines()[0] == f"{CASE_ID}: Approved (0 active findings)"
        record = json.loads((run_dir / "cases" / CASE_ID / "record.json").read_text(encoding="utf-8"))
        repo = record["repo_context"]
        assert repo["mode"] == "repo"
        assert repo["reader"] == "repo-dir"
        assert repo["listing"]["complete"] is True and repo["listing"]["paths"] > 0
        entries = [entry for row in repo["units"]["chunks"] for entry in row["entries"]]
        assert [entry for entry in entries if entry["kind"] == "contract"] != []
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert list(run["config"])[-4:] == list(FOUR)
        assert run["config"]["repo_context"] == "repo"
        assert run["config"]["context_contract_globs"] == config._DEFAULTS["context_contract_globs"]
        assert stub_llm.calls >= 2

    def test_control_off_records_null(self, tmp_path, stub_llm, capsys):
        code, run_dir = self._run(tmp_path)

        assert code == 0
        record = json.loads((run_dir / "cases" / CASE_ID / "record.json").read_text(encoding="utf-8"))
        assert "repo_context" in record and record["repo_context"] is None
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert run["config"]["repo_context"] == "off"
