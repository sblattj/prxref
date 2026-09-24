"""``prxref eval run``: the per-case replay loop, its run directory and its fences (#14 T3).

Most tests call :func:`prxref.evals.eval_run` directly with a fake review
runner, so a case can be made to crash, return nothing or end in an ``Error``
verdict on demand. The tests in :class:`TestThroughTheCli` go through
``cli.main``, the console-script entry point: one proves ``_cmd_eval`` hands
the CLI's own runner and record builder in, and the others run the REAL
``_run_review`` over ``--diff-file``-style cases with a stub LLM and no
network, as ``tests/evals/test_eval_replay.py`` does for ``review``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from prxref import cli, config, evals
from prxref.eval_cases import (
    EvalCase,
    ExpectedFinding,
    case_from_json_record,
    case_to_json,
    is_safe_id,
    load_cases,
)
from prxref.llm import ConfigError, InvokeResult

DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,2 +1,4 @@\n"
    " def add(a, b):\n"
    "-    return a + b\n"
    "+    total = a + b\n"
    "+    print(total)\n"
    "+    return total\n"
)
PR_URL = "https://github.com/acme/widgets/pull/7"
BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
SAMPLING = {"temperature": None, "seed": 7, "models": ["model-x"]}
ISO_Z = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
SECRET_NAME = re.compile(r"key|token|secret|password|auth", re.IGNORECASE)
TOKEN_COUNTS = ("llm_max_tokens", "chunk_token_budget")


def _dataset(tmp_path: Path, cases: list[dict[str, Any]] | None = None) -> Path:
    """Write a ``cases.json`` beside ``change.patch``; the default is cases ``a`` then ``b``."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    (data / "change.patch").write_text(DIFF, encoding="utf-8")
    if cases is None:
        cases = [
            {"id": "a", "diff_file": "change.patch", "expected": []},
            {"id": "b", "diff_file": "change.patch", "expected": []},
        ]
    path = data / "cases.json"
    path.write_text(json.dumps({"version": 1, "cases": cases}), encoding="utf-8")
    return path


def _args(cases: Path | str, out: Path, *extra: str, label: str = "L"):
    return cli._build_parser().parse_args(
        ["eval", "run", "--cases", str(cases), f"--label={label}", "--out", str(out), *extra]
    )


def _finding(title: str, *, drop_reason: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        file="src/app.py", line=3, severity="warning", confidence=0.9, scope="unknown",
        title=title, body=f"{title} body", drop_reason=drop_reason,
    )


def _result(verdict: str = "Approved", *, active=(), dropped=(), **extra: Any) -> dict:
    return {
        "verdict": verdict,
        "findings_active": list(active),
        "findings_dropped": list(dropped),
        "chunk_count": 1,
        "chunks_reviewed": 1,
        "chunks_failed": 0,
        "sampling": SAMPLING,
        "review_rules": None,
        **extra,
    }


class FakeReview:
    """Stands in for ``cli._run_review``: records every call, answers per case id.

    The case id is read off the ``trace_dir`` it is handed
    (``<out>/<label>/cases/<id>/trace``). An outcome that is an exception is
    raised; anything else is returned, and a case with no outcome gets an
    ``Approved`` result with no findings.
    """

    def __init__(self, outcomes: dict[str, Any] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        outcome = self.outcomes.get(Path(kwargs["trace_dir"]).parent.name, _result())
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @property
    def case_ids(self) -> list[str]:
        return [Path(call["trace_dir"]).parent.name for call in self.calls]


def _run(args, review: FakeReview, build_record=cli._build_json_result) -> int:
    return evals.eval_run(args, run_review=review, build_record=build_record)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


class TestLayout:
    def test_a_run_writes_run_json_and_each_cases_case_record_and_trace(self, tmp_path, capsys):
        review = FakeReview({"a": _result(active=[_finding("t1")])})
        assert _run(_args(_dataset(tmp_path), tmp_path / "out"), review) == 0

        run_dir = tmp_path / "out" / "L"
        assert _tree(run_dir) == [
            "cases",
            "cases/a", "cases/a/case.json", "cases/a/record.json", "cases/a/trace",
            "cases/b", "cases/b/case.json", "cases/b/record.json", "cases/b/trace",
            "run.json",
        ]
        assert _read(run_dir / "cases/a/record.json") == cli._build_json_result(
            _result(active=[_finding("t1")])
        )
        assert capsys.readouterr().out.splitlines() == [
            "a: Approved (1 active finding)",
            "b: Approved (0 active findings)",
            f"run directory: {run_dir}",
        ]

    def test_the_record_is_what_build_record_returns(self, tmp_path):
        seen = []

        def build_record(result):
            seen.append(result)
            return {"verdict": "Approved", "findings": [], "marker": len(seen)}

        _run(_args(_dataset(tmp_path), tmp_path / "out"), FakeReview(), build_record)

        assert len(seen) == 2
        assert _read(tmp_path / "out/L/cases/b/record.json") == {
            "verdict": "Approved", "findings": [], "marker": 2,
        }

    def test_each_case_is_replayed_blind_with_its_own_inputs_only(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        (data / "ticket.md").write_text("Ticket: add a total.\n", encoding="utf-8")
        (data / "docs").mkdir()
        (data / "docs" / "rules.md").write_text("Functions MUST return.\n", encoding="utf-8")
        cases = _dataset(tmp_path, [
            {"id": "plain", "diff_file": "change.patch", "expected": []},
            {
                "id": "rich", "diff_file": "change.patch", "context_file": "ticket.md",
                "spec": ["docs", "https://example.com/spec.md"], "expected": [],
            },
            {
                "id": "pinned", "pr_url": PR_URL, "base_sha": BASE_SHA.upper(),
                "head_sha": HEAD_SHA, "expected": [],
            },
        ])
        rules_file = str(data / "RULES.md")
        (data / "RULES.md").write_text("Prefer early returns.\n", encoding="utf-8")
        review = FakeReview()

        _run(_args(cases, tmp_path / "out", "--rules-file", rules_file), review)

        cases_dir = tmp_path / "out" / "L" / "cases"
        common = {
            "post": False, "no_threads": True, "rules_file": rules_file,
            "scoped_rules": None, "prompts_dir": None,
        }
        assert review.calls == [
            {
                "url": None, **common, "diff_file": str(data / "change.patch"),
                "base_sha": None, "head_sha": None, "context_file": "", "spec_sources": [],
                "trace_dir": str(cases_dir / "plain" / "trace"),
            },
            {
                "url": None, **common, "diff_file": str(data / "change.patch"),
                "base_sha": None, "head_sha": None, "context_file": str(data / "ticket.md"),
                "spec_sources": [str(data / "docs"), "https://example.com/spec.md"],
                "trace_dir": str(cases_dir / "rich" / "trace"),
            },
            {
                "url": PR_URL, **common, "diff_file": None,
                "base_sha": BASE_SHA, "head_sha": HEAD_SHA, "context_file": "",
                "spec_sources": [], "trace_dir": str(cases_dir / "pinned" / "trace"),
            },
        ]

    @pytest.mark.parametrize("extra, expected", [((), None), (("--rules-file", ""), "")])
    def test_rules_file_is_passed_through_as_given(self, tmp_path, extra, expected):
        review = FakeReview()
        _run(_args(_dataset(tmp_path), tmp_path / "out", *extra), review)
        assert [call["rules_file"] for call in review.calls] == [expected, expected]

    def test_the_default_out_is_relative_to_the_working_directory(self, tmp_path, monkeypatch):
        cases = _dataset(tmp_path)
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        args = cli._build_parser().parse_args(["eval", "run", "--cases", str(cases), "--label", "L"])

        _run(args, FakeReview())

        assert (work / "prxref-eval" / "L" / "run.json").is_file()


class TestFencing:
    def test_a_raising_case_is_recorded_and_the_next_case_still_runs(self, tmp_path, capsys):
        review = FakeReview({"a": RuntimeError("boom")})

        assert _run(_args(_dataset(tmp_path), tmp_path / "out"), review) == 0

        cases_dir = tmp_path / "out" / "L" / "cases"
        assert review.case_ids == ["a", "b"]
        assert _read(cases_dir / "a" / "error.json") == {"case_id": "a", "error": "RuntimeError: boom"}
        assert not (cases_dir / "a" / "record.json").exists()
        assert (cases_dir / "a" / "case.json").is_file()
        assert (cases_dir / "b" / "record.json").is_file()
        assert not (cases_dir / "b" / "error.json").exists()
        assert capsys.readouterr().out.splitlines()[:2] == [
            "a: failed: RuntimeError: boom",
            "b: Approved (0 active findings)",
        ]

    def test_a_config_error_from_one_case_is_a_failed_case_not_exit_2(self, tmp_path):
        review = FakeReview({"a": ConfigError("--diff-file: cannot read 'gone.patch': No such file")})

        assert _run(_args(_dataset(tmp_path), tmp_path / "out"), review) == 0

        assert _read(tmp_path / "out/L/cases/a/error.json")["error"] == (
            "ConfigError: --diff-file: cannot read 'gone.patch': No such file"
        )
        assert (tmp_path / "out/L/cases/b/record.json").is_file()

    def test_a_none_result_is_recorded_as_an_unrecognised_url(self, tmp_path, capsys):
        cases = _dataset(tmp_path, [
            {"id": "a", "pr_url": PR_URL, "base_sha": BASE_SHA, "head_sha": HEAD_SHA, "expected": []},
        ])
        _run(_args(cases, tmp_path / "out"), FakeReview({"a": None}))

        error = f"RuntimeError: unrecognized PR URL {PR_URL!r}"
        assert _read(tmp_path / "out/L/cases/a/error.json") == {"case_id": "a", "error": error}
        assert capsys.readouterr().out.splitlines()[0] == f"a: failed: {error}"

    def test_a_build_record_crash_is_recorded_for_that_case_only(self, tmp_path):
        calls = []

        def build_record(result):
            calls.append(result)
            if len(calls) == 1:
                raise KeyError("verdict")
            return cli._build_json_result(result)

        _run(_args(_dataset(tmp_path), tmp_path / "out"), FakeReview(), build_record)

        assert _read(tmp_path / "out/L/cases/a/error.json")["error"] == "KeyError: 'verdict'"
        assert _read(tmp_path / "out/L/cases/b/record.json")["verdict"] == "Approved"

    def test_an_error_verdict_is_recorded_as_a_record_with_that_verdict(self, tmp_path, capsys):
        review = FakeReview({"a": _result("Error", chunks_failed=1, chunks_reviewed=0)})

        assert _run(_args(_dataset(tmp_path), tmp_path / "out"), review) == 0

        record = _read(tmp_path / "out/L/cases/a/record.json")
        assert record["verdict"] == "Error"
        assert record["chunks_failed"] == 1
        assert not (tmp_path / "out/L/cases/a/error.json").exists()
        assert capsys.readouterr().out.splitlines()[0] == "a: Error (0 active findings)"

    def test_dropped_rows_are_not_counted_as_active(self, tmp_path, capsys):
        review = FakeReview({"a": _result(
            "Request-Changes",
            active=[_finding("kept")],
            dropped=[_finding("d1", drop_reason="low confidence"), _finding("d2", drop_reason="dup")],
        )})

        _run(_args(_dataset(tmp_path), tmp_path / "out"), review)

        assert len(_read(tmp_path / "out/L/cases/a/record.json")["findings"]) == 3
        assert capsys.readouterr().out.splitlines()[0] == "a: Request-Changes (1 active finding)"

    def test_the_run_exits_0_through_the_cli_when_every_case_fails(self, tmp_path, monkeypatch, capsys):
        review = FakeReview({"a": RuntimeError("one"), "b": ConfigError("--rules-file: gone")})
        monkeypatch.setattr(cli, "_run_review", review)

        code = cli.main(["eval", "run", "--cases", str(_dataset(tmp_path)), "--label", "L",
                         "--out", str(tmp_path / "out")])

        assert code == 0
        assert review.case_ids == ["a", "b"]
        out = capsys.readouterr().out.splitlines()
        assert out[:2] == ["a: failed: RuntimeError: one", "b: failed: ConfigError: --rules-file: gone"]
        assert (tmp_path / "out/L/run.json").is_file()

    def test_a_normal_case_logs_nothing_at_warning_or_above(self, tmp_path, caplog):
        with caplog.at_level(logging.DEBUG):
            _run(_args(_dataset(tmp_path), tmp_path / "out"), FakeReview())
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


class TestFlagErrors:
    @pytest.mark.parametrize("label", ["", ".", "..", "a/b", "a\\b", "/abs", "-dash", "sp ace"])
    def test_a_label_that_is_not_one_safe_path_segment_is_refused(self, tmp_path, label):
        review = FakeReview()
        with pytest.raises(ConfigError, match=r"^--label: must be one directory name"):
            _run(_args(_dataset(tmp_path), tmp_path / "out", label=label), review)
        assert review.calls == []
        assert not (tmp_path / "out").exists()

    def test_a_bad_cases_path_is_refused_naming_cases(self, tmp_path):
        review = FakeReview()
        with pytest.raises(ConfigError, match=r"^--cases: "):
            _run(_args(tmp_path / "missing.json", tmp_path / "out"), review)
        assert review.calls == []
        assert not (tmp_path / "out").exists()

    def test_a_bad_case_is_refused_naming_cases_the_case_and_the_field(self, tmp_path):
        cases = _dataset(tmp_path, [
            {"id": "a", "diff_file": "change.patch", "expected": []},
            {"id": "b", "diff_file": "change.patch", "expected": [
                {"id": "h1", "file": "src/app.py", "line": 3, "severity": "fatal"},
            ]},
        ])
        review = FakeReview()
        with pytest.raises(ConfigError, match=r"^--cases: case 'b': expected\[0\]\.severity: "):
            _run(_args(cases, tmp_path / "out"), review)
        assert review.calls == []
        assert not (tmp_path / "out").exists()

    def test_an_existing_run_without_resume_is_refused_naming_label(self, tmp_path):
        run_dir = tmp_path / "out" / "L"
        run_dir.mkdir(parents=True)
        (run_dir / "keep.txt").write_text("x", encoding="utf-8")
        review = FakeReview()

        with pytest.raises(ConfigError, match=r"^--label: the run .* already exists; pass --resume"):
            _run(_args(_dataset(tmp_path), tmp_path / "out"), review)

        assert review.calls == []
        assert _tree(run_dir) == ["keep.txt"]

    def test_an_out_that_cannot_hold_the_run_is_refused_naming_out(self, tmp_path):
        blocker = tmp_path / "out"
        blocker.write_text("a file, not a directory", encoding="utf-8")
        review = FakeReview()
        with pytest.raises(ConfigError, match=r"^--out: cannot create the run directory "):
            _run(_args(_dataset(tmp_path), blocker), review)
        assert review.calls == []

    def test_a_malformed_environment_is_refused_before_any_case_runs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "many")
        review = FakeReview()
        with pytest.raises(ConfigError, match="PRXREF_MAX_CHUNKS"):
            _run(_args(_dataset(tmp_path), tmp_path / "out"), review)
        assert review.calls == []
        assert not (tmp_path / "out").exists()

    @pytest.mark.parametrize("flag", ["--label", "--cases"])
    def test_through_the_cli_a_flag_error_exits_2_naming_the_flag(self, tmp_path, flag, capsys):
        cases = tmp_path / "missing.json" if flag == "--cases" else _dataset(tmp_path)
        label = ".." if flag == "--label" else "L"
        code = cli.main(["eval", "run", "--cases", str(cases), "--label", label,
                         "--out", str(tmp_path / "out")])
        assert code == 2
        assert f"configuration error: {flag}: " in capsys.readouterr().err
        assert not (tmp_path / "out").exists()

    def test_through_the_cli_an_existing_run_exits_2(self, tmp_path, capsys):
        (tmp_path / "out" / "L").mkdir(parents=True)
        code = cli.main(["eval", "run", "--cases", str(_dataset(tmp_path)), "--label", "L",
                         "--out", str(tmp_path / "out")])
        assert code == 2
        assert "configuration error: --label: the run " in capsys.readouterr().err


class TestResume:
    def test_resume_skips_every_recorded_case_and_runs_the_rest(self, tmp_path, capsys):
        cases = _dataset(tmp_path, [
            {"id": c, "diff_file": "change.patch", "expected": []} for c in ("a", "b", "c")
        ])
        _run(_args(cases, tmp_path / "out"), FakeReview({"b": RuntimeError("flaky")}))
        cases_dir = tmp_path / "out" / "L" / "cases"
        (cases_dir / "c" / "record.json").unlink()
        capsys.readouterr()

        review = FakeReview()
        assert _run(_args(cases, tmp_path / "out", "--resume"), review) == 0

        assert review.case_ids == ["c"]
        assert _read(cases_dir / "b" / "error.json")["error"] == "RuntimeError: flaky"
        assert capsys.readouterr().out.splitlines() == [
            "a: skipped (already recorded)",
            "b: skipped (already recorded)",
            "c: Approved (0 active findings)",
            f"run directory: {tmp_path / 'out' / 'L'}",
        ]

    def test_resume_rewrites_run_json_from_the_records_on_disk(self, tmp_path):
        cases = _dataset(tmp_path)
        _run(_args(cases, tmp_path / "out"), FakeReview())
        run_json = tmp_path / "out" / "L" / "run.json"
        run_json.unlink()

        review = FakeReview()
        _run(_args(cases, tmp_path / "out", "--resume"), review)

        assert review.calls == []
        run = _read(run_json)
        assert run["case_ids"] == ["a", "b"]
        assert run["sampling"] == SAMPLING

    def test_resume_of_a_run_that_does_not_exist_starts_it(self, tmp_path):
        review = FakeReview()
        assert _run(_args(_dataset(tmp_path), tmp_path / "out", "--resume"), review) == 0
        assert review.case_ids == ["a", "b"]
        assert (tmp_path / "out" / "L" / "run.json").is_file()

    def test_a_case_with_only_its_case_json_is_run_again(self, tmp_path):
        cases = _dataset(tmp_path)
        _run(_args(cases, tmp_path / "out"), FakeReview())
        (tmp_path / "out/L/cases/a/record.json").unlink()

        review = FakeReview()
        _run(_args(cases, tmp_path / "out", "--resume"), review)

        assert review.case_ids == ["a"]
        assert (tmp_path / "out/L/cases/a/record.json").is_file()


class TestRunJson:
    def test_run_json_has_every_key_in_order(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "3")
        monkeypatch.setenv("PRXREF_LLM_MODELS", "model-x,model-y")
        cases = _dataset(tmp_path)

        _run(_args(cases, tmp_path / "out"), FakeReview())

        run = _read(tmp_path / "out" / "L" / "run.json")
        assert list(run) == [
            "version", "label", "cases_path", "created_at", "case_ids", "prompts",
            "sampling", "review_rules", "scoped_rules", "config",
        ]
        assert run["version"] == evals.RUN_VERSION == 1
        assert run["label"] == "L"
        assert run["cases_path"] == str(cases)
        assert ISO_Z.fullmatch(run["created_at"])
        assert run["case_ids"] == ["a", "b"]
        assert run["sampling"] == SAMPLING
        assert run["review_rules"] is None
        assert list(run["config"]) == list(evals.RUN_CONFIG_KEYS)
        assert run["config"]["max_chunks"] == 3
        assert run["config"]["llm_models"] == ["model-x", "model-y"]
        cfg = config.load_config()
        assert run["config"] == {key: cfg[key] for key in evals.RUN_CONFIG_KEYS}

    def test_cases_path_is_kept_exactly_as_given(self, tmp_path, monkeypatch):
        _dataset(tmp_path)
        monkeypatch.chdir(tmp_path)
        _run(_args("data/cases.json", tmp_path / "out"), FakeReview())
        assert _read(tmp_path / "out/L/run.json")["cases_path"] == "data/cases.json"

    def test_the_prompt_hashes_are_those_of_the_packaged_templates(self, tmp_path):
        _run(_args(_dataset(tmp_path), tmp_path / "out"), FakeReview())

        prompts = _read(tmp_path / "out/L/run.json")["prompts"]
        packaged = Path(evals.__file__).parent / "prompts"
        assert prompts == {
            "sha256": {
                name: hashlib.sha256((packaged / f"{name}.md").read_bytes()).hexdigest()
                for name in ("worker", "systemic", "summary")
            },
            "prompt_templates": None,
        }

    def test_the_stamps_come_from_the_first_case_that_did_not_error(self, tmp_path):
        cases = _dataset(tmp_path, [
            {"id": c, "diff_file": "change.patch", "expected": []} for c in ("a", "b", "c", "d")
        ])
        overrides = {"dir": "prompts", "templates": {"worker": {"sha256": "0" * 64}}}
        rules = {"path": "RULES.md", "sha256": "1" * 64}
        review = FakeReview({
            "a": RuntimeError("down"),
            "b": _result("Error", sampling={"temperature": None, "seed": None, "models": []}),
            "c": _result(sampling={"models": ["from-c"]}, review_rules=rules, prompt_templates=overrides),
            "d": _result(sampling={"models": ["from-d"]}),
        })

        def build_record(result):
            record = cli._build_json_result(result)
            if "prompt_templates" in result:
                record["prompt_templates"] = result["prompt_templates"]
            return record

        _run(_args(cases, tmp_path / "out"), review, build_record)

        run = _read(tmp_path / "out/L/run.json")
        assert run["sampling"] == {"models": ["from-c"]}
        assert run["review_rules"] == rules
        assert run["prompts"]["prompt_templates"] == overrides

    def test_the_stamps_are_null_when_no_case_was_reviewed(self, tmp_path):
        review = FakeReview({"a": RuntimeError("x"), "b": _result("Error")})
        _run(_args(_dataset(tmp_path), tmp_path / "out"), review)

        run = _read(tmp_path / "out/L/run.json")
        assert (run["sampling"], run["review_rules"], run["prompts"]["prompt_templates"]) == (
            None, None, None,
        )

    def test_every_allowlisted_key_is_a_config_key_and_none_is_secret(self):
        assert set(evals.RUN_CONFIG_KEYS) <= set(config._DEFAULTS)
        assert len(set(evals.RUN_CONFIG_KEYS)) == len(evals.RUN_CONFIG_KEYS)
        named_like_a_secret = [key for key in evals.RUN_CONFIG_KEYS if SECRET_NAME.search(key)]
        assert named_like_a_secret == list(TOKEN_COUNTS)
        assert all(key in config._INT_KEYS for key in TOKEN_COUNTS)

    def test_the_secret_name_check_catches_every_credential_key(self):
        credentials = {
            key for key in config._DEFAULTS
            if SECRET_NAME.search(key) and isinstance(config._DEFAULTS[key], str)
        }
        assert {
            "llm_api_key", "github_token", "jira_api_token", "bitbucket_app_password",
            "bitbucket_server_password", "github_webhook_secret", "azure_devops_token",
        } <= credentials
        assert credentials.isdisjoint(evals.RUN_CONFIG_KEYS)
        assert _secret_named_keys({"config": {
            "llm_max_tokens": 4096, "chunk_token_budget": "4096", "github_token": "",
        }}) == [".config.chunk_token_budget", ".config.github_token"]

    def test_no_credential_reaches_the_run_directory(self, tmp_path, monkeypatch):
        secret_keys = sorted(
            key for key in config._DEFAULTS
            if SECRET_NAME.search(key) and isinstance(config._DEFAULTS[key], str)
        )
        sentinels = {key: f"SENTINEL-{i:02d}-do-not-write" for i, key in enumerate(secret_keys)}
        for key, value in sentinels.items():
            monkeypatch.setenv(f"PRXREF_{key.upper()}", value)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fake")
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: _NoFindingsLLM())
        monkeypatch.setattr("requests.Session.request", _no_network)
        cases = _dataset(tmp_path)

        assert evals.eval_run(
            _args(cases, tmp_path / "out"),
            run_review=cli._run_review, build_record=cli._build_json_result,
        ) == 0

        run_dir = tmp_path / "out" / "L"
        assert _read(run_dir / "cases/a/record.json")["verdict"] == "Approved"
        written = "\n".join(
            p.read_text(encoding="utf-8") for p in sorted(run_dir.rglob("*")) if p.is_file()
        )
        assert [key for key, value in sentinels.items() if value in written] == []
        assert _secret_named_keys(_read(run_dir / "run.json")) == []


def _secret_named_keys(value: Any, path: str = "") -> list[str]:
    """Every key under ``value`` named like a secret, except an int-valued token count."""
    if isinstance(value, dict):
        found = [
            f"{path}.{key}" for key, child in value.items()
            if SECRET_NAME.search(str(key)) and not (key in TOKEN_COUNTS and type(child) is int)
        ]
        for key, child in value.items():
            found.extend(_secret_named_keys(child, f"{path}.{key}"))
        return found
    if isinstance(value, list):
        return [hit for i, child in enumerate(value) for hit in _secret_named_keys(child, f"{path}[{i}]")]
    return []


def _full_case() -> EvalCase:
    return EvalCase(
        id="case-01.x_y",
        expected=(
            ExpectedFinding(
                id="H1", file="src/app.py", line=3, severity="minor", category="generic",
                accepted=True, text="prints in library code", must_match="re:print\\(",
            ),
            ExpectedFinding(id="H2", file="src/app.py", line=4, severity="spec"),
        ),
        pr_url=PR_URL,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        diff_file="/data/cases/change.patch",
        context_file="/data/cases/ticket.md",
        spec=("/data/cases/docs", "https://example.com/spec.md"),
    )


class TestCaseJsonRoundTrip:
    def test_a_case_with_every_field_round_trips_through_json_text(self):
        case = _full_case()
        text = json.dumps(case_to_json(case))
        assert case_from_json_record(json.loads(text)) == case

    def test_a_minimal_case_round_trips(self):
        case = EvalCase(id="c1", expected=(), diff_file="change.patch")
        assert case_from_json_record(case_to_json(case)) == case

    def test_the_keys_come_in_the_cases_json_spelling_and_a_fixed_order(self):
        record = case_to_json(_full_case())
        assert list(record) == [
            "id", "pr_url", "base_sha", "head_sha", "diff_file", "context_file", "spec", "expected",
        ]
        assert list(record["expected"][0]) == [
            "id", "file", "line", "severity", "category", "accepted", "text", "must_match",
        ]
        assert record["spec"] == ["/data/cases/docs", "https://example.com/spec.md"]
        assert record["expected"][1] == {
            "id": "H2", "file": "src/app.py", "line": 4, "severity": "spec",
            "category": None, "accepted": None, "text": None, "must_match": None,
        }

    def test_both_dataset_forms_round_trip(self, tmp_path):
        case_dir = tmp_path / "dir-form" / "case-001"
        case_dir.mkdir(parents=True)
        (case_dir / "diff.patch").write_text(DIFF, encoding="utf-8")
        (case_dir / "expected.json").write_text(json.dumps([
            {"id": "G1", "file": "src/app.py", "line_hint": 3, "severity": "warning", "source": "generic"},
        ]), encoding="utf-8")
        (case_dir / "ticket.md").write_text("Ticket.\n", encoding="utf-8")
        loaded = load_cases(tmp_path / "dir-form") + load_cases(_dataset(tmp_path))
        assert len(loaded) == 3
        for case in loaded:
            assert case_from_json_record(json.loads(json.dumps(case_to_json(case)))) == case

    def test_eval_run_writes_the_case_json_that_reads_back_as_the_loaded_case(self, tmp_path):
        cases = _dataset(tmp_path, [
            {"id": "a", "diff_file": "change.patch", "expected": [
                {"id": "H1", "file": "src/app.py", "line": 3, "severity": "minor", "text": "print"},
            ]},
        ])
        _run(_args(cases, tmp_path / "out"), FakeReview())

        stored = _read(tmp_path / "out/L/cases/a/case.json")
        (loaded,) = load_cases(cases)
        assert stored == case_to_json(loaded)
        assert case_from_json_record(stored, source="case.json") == loaded

    @pytest.mark.parametrize(
        "mutate, message",
        [
            (lambda r: [r], r"^case\.json: must be an object"),
            (lambda r: {**r, "id": "../up"}, r"^case\.json: id: "),
            (lambda r: {**r, "extra": 1}, r"^case\.json: case 'case-01\.x_y': extra: unknown field"),
            (lambda r: {k: v for k, v in r.items() if k != "expected"},
             r"^case\.json: case 'case-01\.x_y': expected: required"),
            (lambda r: {**r, "pr_url": ""}, r"^case\.json: case 'case-01\.x_y': pr_url: "),
            (lambda r: {**r, "spec": "docs"}, r"^case\.json: case 'case-01\.x_y': spec: "),
            (lambda r: {**r, "spec": ["ok", 3]}, r"^case\.json: case 'case-01\.x_y': spec\[1\]: "),
            (lambda r: {**r, "expected": [{**r["expected"][0], "severity": "fatal"}]},
             r"^case\.json: case 'case-01\.x_y': expected\[0\]\.severity: "),
        ],
    )
    def test_a_malformed_record_is_refused_naming_the_field(self, mutate, message):
        with pytest.raises(ConfigError, match=message):
            case_from_json_record(mutate(case_to_json(_full_case())))

    def test_the_source_names_the_file_in_every_message(self):
        with pytest.raises(ConfigError, match=r"^runs/L/cases/x/case\.json: must be an object"):
            case_from_json_record(None, source="runs/L/cases/x/case.json")

    def test_a_null_spec_reads_as_no_spec(self):
        record = {**case_to_json(_full_case()), "spec": None}
        assert case_from_json_record(record).spec == ()

    @pytest.mark.parametrize("name, ok", [
        ("baseline", True), ("rules-v2.1_b", True), ("0", True),
        ("", False), (".", False), ("..", False), (".hidden", False), ("a/b", False),
        ("a\\b", False), ("-x", False), ("a b", False), (None, False), (3, False),
    ])
    def test_is_safe_id_is_the_case_id_rule(self, name, ok):
        assert is_safe_id(name) is ok


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
    raise AssertionError("prxref eval run must not touch the network")


class TestThroughTheCli:
    def test_main_hands_eval_run_the_clis_runner_and_record_builder(self, tmp_path, monkeypatch):
        seen = []

        def _record(args, **kwargs):
            seen.append((args, kwargs))
            return 0

        monkeypatch.setattr(evals, "eval_run", _record)
        assert cli.main(["eval", "run", "--cases", "c.json", "--label", "L", "--out", str(tmp_path)]) == 0
        ((args, kwargs),) = seen
        assert args.label == "L"
        assert kwargs == {"run_review": cli._run_review, "build_record": cli._build_json_result}

    @pytest.fixture
    def stub_llm(self, monkeypatch):
        llm = _NoFindingsLLM()
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fake")
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: llm)
        monkeypatch.setattr("requests.Session.request", _no_network)
        return llm

    def _directory_dataset(self, tmp_path: Path) -> Path:
        root = tmp_path / "dataset"
        for name in ("case-001", "case-002"):
            case_dir = root / name
            case_dir.mkdir(parents=True)
            (case_dir / "diff.patch").write_text(DIFF, encoding="utf-8")
            (case_dir / "expected.json").write_text(json.dumps([
                {"id": "G1", "file": "src/app.py", "line_hint": 3, "severity": "warning",
                 "source": "generic", "must_match": "print"},
            ]), encoding="utf-8")
        (root / "case-001" / "ticket.md").write_text("Ticket: CASE-TICKET-LINE\n", encoding="utf-8")
        return root

    def test_a_real_review_of_each_case_writes_the_whole_run(self, tmp_path, stub_llm, monkeypatch, capsys, caplog):
        leaked = tmp_path / "env-ticket.md"
        leaked.write_text("ENV-TICKET-MUST-NOT-LEAK\n", encoding="utf-8")
        monkeypatch.setenv("PRXREF_TICKET_CONTEXT_FILE", str(leaked))
        dataset = self._directory_dataset(tmp_path)

        code = cli.main(["eval", "run", "--cases", str(dataset), "--label", "base",
                         "--out", str(tmp_path / "runs")])

        assert code == 0
        run_dir = tmp_path / "runs" / "base"
        assert capsys.readouterr().out.splitlines() == [
            "case-001: Approved (0 active findings)",
            "case-002: Approved (0 active findings)",
            f"run directory: {run_dir}",
        ]
        assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []
        for case_id in ("case-001", "case-002"):
            case_dir = run_dir / "cases" / case_id
            record = _read(case_dir / "record.json")
            assert record["verdict"] == "Approved"
            assert record["posted"] is False
            assert record["replay"] == {
                "base_sha": None, "head_sha": None, "threads": "hidden",
                "diff_file": str(dataset / case_id / "diff.patch"),
                "description": "file", "as_of": None, "as_of_source": None,
            }
            assert {"chunk0.user.md", "sweep.user.md"} <= {p.name for p in (case_dir / "trace").iterdir()}
            assert _read(case_dir / "case.json")["expected"][0]["must_match"] == "print"
            assert not (case_dir / "error.json").exists()
            prompts = "".join(p.read_text(encoding="utf-8") for p in (case_dir / "trace").glob("*.md"))
            assert "ENV-TICKET-MUST-NOT-LEAK" not in prompts
        case_one = run_dir / "cases" / "case-001"
        assert _read(case_one / "record.json")["ticket_context"]["path"] == str(dataset / "case-001" / "ticket.md")
        assert "CASE-TICKET-LINE" in (case_one / "trace" / "chunk0.user.md").read_text(encoding="utf-8")
        assert _read(run_dir / "cases/case-002/record.json")["ticket_context"] is None
        run = _read(run_dir / "run.json")
        assert run["case_ids"] == ["case-001", "case-002"]
        assert run["sampling"] is not None
        assert stub_llm.calls >= 4

    def test_a_case_whose_diff_vanished_fails_alone_and_the_run_exits_0(
        self, tmp_path, stub_llm, monkeypatch, capsys,
    ):
        data = tmp_path / "data"
        data.mkdir()
        (data / "first.patch").write_text(DIFF, encoding="utf-8")
        (data / "second.patch").write_text(DIFF, encoding="utf-8")
        cases = _dataset(tmp_path, [
            {"id": "a", "diff_file": "first.patch", "expected": []},
            {"id": "b", "diff_file": "second.patch", "expected": []},
        ])
        real_run_review = cli._run_review

        def _vanish_then_review(url, **kwargs):
            if kwargs["diff_file"].endswith("first.patch"):
                Path(kwargs["diff_file"]).unlink()
            return real_run_review(url, **kwargs)

        monkeypatch.setattr(cli, "_run_review", _vanish_then_review)
        code = cli.main(["eval", "run", "--cases", str(cases), "--label", "L",
                         "--out", str(tmp_path / "out")])

        assert code == 0
        error = _read(tmp_path / "out/L/cases/a/error.json")["error"]
        assert error.startswith("ConfigError: --diff-file: cannot read ")
        assert _read(tmp_path / "out/L/cases/b/record.json")["verdict"] == "Approved"
        assert capsys.readouterr().out.splitlines()[:2] == [
            f"a: failed: {error}", "b: Approved (0 active findings)",
        ]
