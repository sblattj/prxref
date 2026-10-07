"""The command-line wiring of the repository config file (issue #38).

``review`` and ``eval run`` take ``--config PATH`` / ``--no-config`` and
otherwise read ``PRXREF_CONFIG_FILE`` or ``.prxref.toml`` in the working
directory; ``serve`` reads only a file it is told about; ``config check``
prints every setting with its source; and the run record carries
``config_file``. Nothing here touches the network or a real LLM.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest

from prxref import cli, config

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "issue17"
DIFF = FIXTURE / "pr.diff"
URL = "https://github.com/org/repo/pull/7"
DEFAULT_CHUNKS = config._DEFAULTS["max_chunks"]
FAKE_SECRET = "ghp_FAKE0SECRET0VALUE0FOR0REDACTION0TEST"

RESULT = {
    "verdict": "Approved", "findings_active": [], "findings_dropped": [],
    "chunk_count": 1, "chunks_reviewed": 1, "chunks_failed": 0, "elapsed_ms": 5,
    "input_tokens": 10, "output_tokens": 2, "posted": False,
}

BASE_TEXT = "verdict: Approved\n"
BASE_TEXT_VERBOSE = (
    "verdict: Approved\ncounts: 0 (dropped: 0)\nelapsed: 0.0s tokens: 10+2 cost: -\n"
)
BASE_JSON = (
    '{"verdict": "Approved", "findings": [], "chunk_count": 1, "chunks_reviewed": 1, '
    '"chunks_failed": 0, "chunks_over_budget": null, "largest_chunk_tokens": null, '
    '"overflow_files": null, "chunk_token_budget": null, "elapsed_ms": 5, "input_tokens": 10, "output_tokens": 2, '
    '"cost_usd": null, "cost_estimated": null, "posted": false, "review_rules": null, '
    '"ticket_context": null, "spec_grounding": null, "size_advisory": null, '
    '"prompt_templates": null, "scoped_rules": null, "rule_counts": null, '
    '"rule_scope_cleared": null, '
    '"repo_context": null, "parse_retries": null, "context_followup": null, '
    '"suggestions": null, "incremental": null, "degraded": null}\n'
)


@pytest.fixture
def recorder(monkeypatch):
    """Replace ``orchestrate_review``, the LLM factory and the forge; return the recorded kwargs."""
    calls: list[dict] = []

    def _orchestrate(**kwargs):
        calls.append(kwargs)
        return dict(RESULT)

    monkeypatch.setattr("prxref.orchestrator.orchestrate_review", _orchestrate)
    monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: object())
    monkeypatch.setattr(cli, "make_forge", lambda ref: object())
    return calls


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A working directory holding ``.prxref.toml`` with ``max_chunks = 3``."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / config.CONFIG_FILE_NAME).write_text("max_chunks = 3\n", encoding="utf-8")
    return tmp_path


def _review(*extra: str) -> int:
    return cli.main(["review", "--diff-file", str(DIFF), "--no-post", *extra])


def _check(capsys, *extra: str) -> tuple[int, str, str]:
    code = cli.main(["config", "check", *extra])
    out, err = capsys.readouterr()
    return code, out, err


# --------------------------------------------------------------------------- review


class TestReviewPrecedence:
    def test_the_discovered_file_value_is_used(self, repo, recorder):
        assert _review() == 0
        assert recorder[0]["max_chunks"] == 3

    def test_env_beats_the_file(self, repo, recorder, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "5")
        assert _review() == 0
        assert recorder[0]["max_chunks"] == 5

    def test_a_flag_beats_env(self, repo, recorder, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "5")
        assert _review("--max-chunks", "7") == 0
        assert recorder[0]["max_chunks"] == 7

    def test_no_config_ignores_the_file(self, repo, recorder):
        assert _review("--no-config") == 0
        assert recorder[0]["max_chunks"] == DEFAULT_CHUNKS

    def test_no_config_beats_the_env_variable(self, repo, recorder, monkeypatch):
        (repo / "other.toml").write_text("max_chunks = 4\n", encoding="utf-8")
        monkeypatch.setenv(config.CONFIG_FILE_ENV, "other.toml")
        assert _review("--no-config") == 0
        assert recorder[0]["max_chunks"] == DEFAULT_CHUNKS

    def test_config_points_elsewhere(self, repo, recorder):
        (repo / "other.toml").write_text("max_chunks = 4\n", encoding="utf-8")
        assert _review("--config", "other.toml") == 0
        assert recorder[0]["max_chunks"] == 4

    def test_config_off_reads_none(self, repo, recorder):
        assert _review("--config", "off") == 0
        assert recorder[0]["max_chunks"] == DEFAULT_CHUNKS

    def test_the_env_variable_names_a_file(self, repo, recorder, monkeypatch):
        (repo / "other.toml").write_text("max_chunks = 4\n", encoding="utf-8")
        monkeypatch.setenv(config.CONFIG_FILE_ENV, "other.toml")
        assert _review() == 0
        assert recorder[0]["max_chunks"] == 4

    def test_a_missing_config_exits_2_naming_the_flag(self, repo, recorder, capsys):
        assert _review("--config", "nope.toml") == 2
        assert recorder == []
        err = capsys.readouterr().err
        assert "configuration error: --config: config file not found: nope.toml" in err

    def test_an_invalid_file_exits_2_before_the_review(self, repo, recorder, capsys):
        (repo / config.CONFIG_FILE_NAME).write_text("max_chunkz = 3\n", encoding="utf-8")
        assert _review() == 2
        assert recorder == []
        err = capsys.readouterr().err
        assert err.startswith("configuration error: .prxref.toml: unknown key 'max_chunkz';")

    def test_config_and_no_config_are_mutually_exclusive(self, repo, capsys):
        with pytest.raises(SystemExit) as excinfo:
            _review("--config", "x.toml", "--no-config")
        assert excinfo.value.code == 2
        assert "not allowed with argument" in capsys.readouterr().err

    def test_the_policy_keys_are_env_only(self):
        assert {"fail_on", "fallback"} <= config.ENV_ONLY_KEYS


# --------------------------------------------------------------------------- run record


class TestRecordKey:
    def test_json_carries_path_sha256_and_keys(self, repo, recorder, capsys, monkeypatch):
        text = 'max_chunks = 3\npost_mode = "summary"\n'
        (repo / config.CONFIG_FILE_NAME).write_text(text, encoding="utf-8")
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "5")
        assert _review("--format", "json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["config_file"] == {
            "path": ".prxref.toml",
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "keys": ["max_chunks", "post_mode"],
        }
        keys = list(payload)
        assert keys[keys.index("degraded") + 1] == "metadata_rules"
        assert keys[keys.index("metadata_rules") + 1] == "config_file"

    def test_a_file_outside_the_working_directory_is_named_by_its_path(
        self, tmp_path, recorder, capsys, monkeypatch,
    ):
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        other = tmp_path / "shared.toml"
        other.write_text("max_chunks = 2\n", encoding="utf-8")
        assert _review("--config", str(other), "--format", "json") == 0
        assert json.loads(capsys.readouterr().out)["config_file"]["path"] == str(other)

    def test_null_without_a_file(self, tmp_path, recorder, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert _review("--format", "json") == 0
        payload = json.loads(capsys.readouterr().out)
        assert "config_file" in payload and payload["config_file"] is None

    def test_the_payload_defaults_to_null(self):
        assert cli._build_json_result({})["config_file"] is None

    def test_verbose_logs_one_config_line(self, repo, recorder, caplog):
        with caplog.at_level(logging.INFO, logger="prxref"):
            assert _review("-v") == 0
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("config:")]
        assert lines == ["config: .prxref.toml (1 keys)"]

    def test_without_verbose_nothing_new_is_logged(self, repo, recorder, caplog):
        with caplog.at_level(logging.DEBUG, logger="prxref"):
            assert _review() == 0
        assert [r for r in caplog.records if r.getMessage().startswith("config:")] == []


class TestNoFileInvariant:
    """With no file, stdout is 0.24.0's byte for byte, apart from the new null key."""

    @pytest.fixture(autouse=True)
    def _pinned(self, tmp_path, recorder, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PRXREF_LLM_MODELS", "fake")
        monkeypatch.setattr(cli.time, "perf_counter", lambda: 0.0)

    def test_text(self, capsys):
        assert _review() == 0
        assert capsys.readouterr().out == BASE_TEXT

    def test_text_verbose(self, capsys):
        assert _review("-v") == 0
        assert capsys.readouterr().out == BASE_TEXT_VERBOSE

    def test_json(self, capsys):
        assert _review("--format", "json") == 0
        expected = BASE_JSON.replace(
            '"chunks_failed": 0,',
            '"chunks_failed": 0, "failed_chunks": null,',
        ).replace(
            '"incremental": null, "degraded": null}',
            '"incremental": null, "ci_wiring": null, "evidence": null, '
            '"stable_ids": null, "degraded": null, "metadata_rules": null, '
            '"config_file": null, "review_depth": null, "learnings": null}',
        )
        assert capsys.readouterr().out == expected


# --------------------------------------------------------------------------- eval run


class TestEvalRun:
    def _run(self, tmp_path: Path, *extra: str) -> Path:
        out = tmp_path / "out"
        argv = ["eval", "run", "--cases", str(FIXTURE / "cases.json"), "--label", "L",
                "--out", str(out), *extra]
        assert cli.main(argv) == 0
        return out / "L"

    def test_eval_reviews_with_the_discovered_file(self, repo, recorder, capsys):
        run_dir = self._run(repo)
        assert [call["max_chunks"] for call in recorder] == [3]
        record = json.loads(next(run_dir.glob("cases/*/record.json")).read_text(encoding="utf-8"))
        assert record["config_file"]["keys"] == ["max_chunks"]
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert run["config"]["max_chunks"] == 3

    def test_eval_no_config_ignores_it(self, repo, recorder, capsys):
        run_dir = self._run(repo, "--no-config")
        assert [call["max_chunks"] for call in recorder] == [DEFAULT_CHUNKS]
        record = json.loads(next(run_dir.glob("cases/*/record.json")).read_text(encoding="utf-8"))
        assert record["config_file"] is None

    def test_eval_missing_config_exits_2(self, repo, recorder, capsys):
        argv = ["eval", "run", "--cases", str(FIXTURE / "cases.json"), "--label", "L",
                "--out", str(repo / "out"), "--config", "nope.toml"]
        assert cli.main(argv) == 2
        assert recorder == []
        assert "configuration error: --config: config file not found: nope.toml" in capsys.readouterr().err


# --------------------------------------------------------------------------- serve


class TestServe:
    @pytest.fixture
    def served(self, monkeypatch):
        handlers: list = []
        monkeypatch.setattr(
            "prxref.webhooks.serve", lambda port, host, handler: handlers.append(handler),
        )
        return handlers

    def test_serve_does_not_auto_discover(self, repo, served, recorder):
        assert cli.main(["serve"]) == 0
        (handler,) = served
        assert handler is cli._webhook_handler
        handler(URL)
        assert recorder[0]["max_chunks"] == DEFAULT_CHUNKS

    def test_serve_config_uses_the_file(self, repo, served, recorder):
        assert cli.main(["serve", "--config", ".prxref.toml"]) == 0
        (handler,) = served
        handler(URL)
        assert recorder[0]["max_chunks"] == 3

    def test_serve_reads_the_env_variable(self, repo, served, recorder, monkeypatch):
        monkeypatch.setenv(config.CONFIG_FILE_ENV, ".prxref.toml")
        assert cli.main(["serve"]) == 0
        served[0](URL)
        assert recorder[0]["max_chunks"] == 3

    def test_serve_config_off_reads_none(self, repo, served, recorder):
        assert cli.main(["serve", "--config", "off"]) == 0
        assert served == [cli._webhook_handler]

    def test_serve_missing_config_exits_2_before_listening(self, repo, served, capsys):
        assert cli.main(["serve", "--config", "nope.toml"]) == 2
        assert served == []
        assert "configuration error: --config: config file not found: nope.toml" in capsys.readouterr().err

    def test_serve_invalid_config_exits_2_before_listening(self, repo, served, capsys):
        (repo / "bad.toml").write_text("github_token = \"x\"\n", encoding="utf-8")
        assert cli.main(["serve", "--config", "bad.toml"]) == 2
        assert served == []
        assert "'github_token' cannot be set in a repository config file (credential)" in (
            capsys.readouterr().err
        )


# --------------------------------------------------------------------------- config check


class TestConfigCheck:
    def test_text_lists_every_key_with_its_source(self, repo, capsys, monkeypatch):
        monkeypatch.setenv("PRXREF_POST_MODE", "summary")
        code, out, err = _check(capsys)
        assert code == 0
        lines = out.splitlines()
        assert lines[0] == "config file: .prxref.toml"
        assert lines[-1] == "ok"
        body = lines[1:-1]
        assert [line.split(" = ", 1)[0] for line in body] == sorted(config._DEFAULTS)
        assert "max_chunks = 3  (file)" in body
        assert "post_mode = summary  (env PRXREF_POST_MODE)" in body
        assert f"max_workers = {config._DEFAULTS['max_workers']}  (default)" in body
        assert f"llm_models = {json.dumps(config._DEFAULTS['llm_models'])}  (default)" in body

    def test_text_without_a_file(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        code, out, _ = _check(capsys)
        assert code == 0
        assert out.splitlines()[0] == "config file: none"
        assert f"max_chunks = {DEFAULT_CHUNKS}  (default)" in out.splitlines()

    def test_no_config_ignores_the_file(self, repo, capsys):
        code, out, _ = _check(capsys, "--no-config")
        assert code == 0
        assert out.splitlines()[0] == "config file: none"
        assert f"max_chunks = {DEFAULT_CHUNKS}  (default)" in out.splitlines()

    def test_json(self, repo, capsys, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_ERRORS", "2")
        code, out, _ = _check(capsys, "--format", "json")
        assert code == 0
        payload = json.loads(out)
        assert list(payload) == ["config_file", "values"]
        assert payload["config_file"] == ".prxref.toml"
        assert list(payload["values"]) == sorted(config._DEFAULTS)
        assert payload["values"]["max_chunks"] == {"value": 3, "source": "file"}
        assert payload["values"]["max_error_findings"] == {"value": 2, "source": "env PRXREF_MAX_ERRORS"}
        assert payload["values"]["post_mode"]["source"] == "default"
        assert payload["values"]["github_token"] == {"value": "<unset>", "source": "default"}

    def test_json_without_a_file(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        code, out, _ = _check(capsys, "--format", "json")
        assert code == 0
        assert json.loads(out)["config_file"] is None

    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_an_unknown_key_exits_2(self, repo, capsys, fmt):
        (repo / config.CONFIG_FILE_NAME).write_text("max_chunkz = 3\n", encoding="utf-8")
        code, out, err = _check(capsys, "--format", fmt)
        assert code == 2
        assert out == ""
        assert err == (
            "configuration error: .prxref.toml: unknown key 'max_chunkz'; did you mean "
            f"'max_chunks'? see {config.CONFIG_DOCS_URL}\n"
        )

    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_an_env_only_key_exits_2_with_the_file_layer_message(self, repo, capsys, fmt):
        (repo / config.CONFIG_FILE_NAME).write_text('llm_base_url = "https://example.com"\n', encoding="utf-8")
        code, out, err = _check(capsys, "--format", fmt)
        assert code == 2
        assert out == ""
        assert err == (
            "configuration error: .prxref.toml: 'llm_base_url' cannot be set in a repository "
            "config file (endpoint); set PRXREF_LLM_BASE_URL in the pipeline instead; see "
            f"{config.CONFIG_DOCS_URL}\n"
        )

    def test_a_bad_env_value_exits_2(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PRXREF_MAX_CHUNKS", "many")
        code, out, err = _check(capsys)
        assert code == 2
        assert out == ""
        assert err.startswith("configuration error: PRXREF_MAX_CHUNKS: ")

    def test_a_missing_config_exits_2(self, repo, capsys):
        code, out, err = _check(capsys, "--config", "nope.toml")
        assert code == 2
        assert out == ""
        assert err == "configuration error: --config: config file not found: nope.toml\n"

    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_a_credential_is_never_printed(self, repo, capsys, monkeypatch, fmt):
        monkeypatch.setenv("PRXREF_GITHUB_TOKEN", FAKE_SECRET)
        monkeypatch.setenv("PRXREF_GITHUB_WEBHOOK_SECRET", FAKE_SECRET + "2")
        code, out, err = _check(capsys, "--format", fmt)
        assert code == 0
        assert FAKE_SECRET not in out and FAKE_SECRET not in err
        assert FAKE_SECRET[4:20] not in out + err
        if fmt == "json":
            values = json.loads(out)["values"]
            assert values["github_token"] == {"value": "<set>", "source": "env PRXREF_GITHUB_TOKEN"}
            assert values["gitlab_token"]["value"] == "<unset>"
        else:
            assert "github_token = <set>  (env PRXREF_GITHUB_TOKEN)" in out.splitlines()
            assert "gitlab_token = <unset>  (default)" in out.splitlines()

    def test_every_credential_class_key_is_redacted(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        creds = sorted(k for k, why in config._ENV_ONLY_REASONS.items() if why == "credential")
        assert "llm_api_key" in creds and "jira_api_token" in creds
        for key in creds:
            monkeypatch.setenv(config._ENV_PREFIX + key.upper(), f"{FAKE_SECRET}-{key}")
        code, out, err = _check(capsys, "--format", "json")
        assert code == 0
        assert FAKE_SECRET not in out + err
        values = json.loads(out)["values"]
        assert {key: values[key]["value"] for key in creds} == dict.fromkeys(creds, "<set>")

    def test_config_without_an_action_exits_2(self, capsys):
        assert cli.main(["config"]) == 2
        capsys.readouterr()


# --------------------------------------------------------------------------- path inputs


class TestPathInputsAreOpened:
    """``config check`` opens the files path keys name, as ``review`` does, and names their source."""

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        for name in ("PRXREF_REVIEW_RULES", "PRXREF_SCOPED_RULES",
                     "PRXREF_TICKET_CONTEXT_FILE", "PRXREF_PROMPTS_DIR"):
            monkeypatch.delenv(name, raising=False)

    def _write(self, repo: Path, text: str) -> None:
        (repo / config.CONFIG_FILE_NAME).write_text(text, encoding="utf-8")

    def _missing_rules_message(self, repo: Path, source: str) -> str:
        missing = repo.resolve() / "missing.md"
        return (
            f"configuration error: {source}: cannot read rules file "
            f"'{missing}': No such file or directory\n"
        )

    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_a_missing_rules_file_named_by_the_file_exits_2(self, repo, capsys, fmt):
        self._write(repo, 'review_rules = "missing.md"\n')
        code, out, err = _check(capsys, "--format", fmt)
        assert code == 2
        assert out == ""
        assert err == self._missing_rules_message(repo, ".prxref.toml: review_rules")

    @pytest.mark.parametrize("fmt", ["text", "json"])
    def test_an_unusable_prompts_dir_named_by_the_file_exits_2(self, repo, capsys, fmt):
        (repo / "prompts").mkdir()
        (repo / "prompts" / "worker.md").write_text("no marker here\n", encoding="utf-8")
        self._write(repo, 'prompts_dir = "prompts"\n')
        code, out, err = _check(capsys, "--format", fmt)
        assert code == 2
        assert out == ""
        assert err.startswith("configuration error: .prxref.toml: prompts_dir: prompt template ")

    def test_the_same_file_set_through_the_variable_names_the_variable(
        self, repo, capsys, monkeypatch,
    ):
        monkeypatch.setenv("PRXREF_REVIEW_RULES", str(repo.resolve() / "missing.md"))
        code, out, err = _check(capsys, "--format", "json")
        assert code == 2
        assert out == ""
        assert err == self._missing_rules_message(repo, "PRXREF_REVIEW_RULES")

    def test_the_variable_beats_the_file_and_is_named(self, repo, capsys, monkeypatch):
        self._write(repo, 'review_rules = "other.md"\n')
        monkeypatch.setenv("PRXREF_REVIEW_RULES", str(repo.resolve() / "missing.md"))
        code, _out, err = _check(capsys)
        assert code == 2
        assert err == self._missing_rules_message(repo, "PRXREF_REVIEW_RULES")

    def test_usable_files_still_print_ok(self, repo, capsys):
        (repo / "rules.md").write_text("Prefer small functions.\n", encoding="utf-8")
        self._write(repo, 'review_rules = "rules.md"\n')
        code, out, err = _check(capsys)
        assert code == 0
        assert out.splitlines()[-1] == "ok"
        assert err == ""

    def test_review_names_the_file_key(self, repo, recorder, capsys):
        self._write(repo, 'review_rules = "missing.md"\n')
        assert _review() == 2
        assert recorder == []
        assert capsys.readouterr().err.endswith(
            self._missing_rules_message(repo, ".prxref.toml: review_rules")
        )

    def test_review_and_config_check_print_the_same_line(self, repo, recorder, capsys):
        self._write(repo, 'scoped_rules = ["missing.md"]\n')
        assert _review() == 2
        review_line = capsys.readouterr().err.strip().splitlines()[-1]
        code, _out, err = _check(capsys)
        assert code == 2
        assert err.strip() == review_line
        assert review_line.startswith("configuration error: .prxref.toml: scoped_rules: ")

    def test_a_flag_is_still_named_over_the_file(self, repo, recorder, capsys):
        self._write(repo, 'review_rules = "rules.md"\n')
        assert _review("--rules-file", "missing.md") == 2
        assert "configuration error: --rules-file: " in capsys.readouterr().err

    def test_eval_run_names_the_file_key(self, repo, recorder, capsys):
        self._write(repo, 'review_rules = "missing.md"\n')
        argv = ["eval", "run", "--cases", str(FIXTURE / "cases.json"), "--label", "L",
                "--out", str(repo / "out")]
        assert cli.main(argv) == 2
        assert recorder == []
        assert capsys.readouterr().err.endswith(
            self._missing_rules_message(repo, ".prxref.toml: review_rules")
        )


# --------------------------------------------------------------------------- symlinked paths


class TestSymlinkedDisplay:
    """A config file named through a symlinked directory displays as its resolved spelling."""

    @pytest.fixture
    def linked(self, tmp_path, monkeypatch):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        monkeypatch.chdir(real)
        return real, link

    def test_display_path_resolves_the_directory(self, linked):
        real, link = linked
        assert config._display_path(link / config.CONFIG_FILE_NAME) == config.CONFIG_FILE_NAME
        assert config._display_path(real / config.CONFIG_FILE_NAME) == config.CONFIG_FILE_NAME
        assert config._display_path(link / "sub" / "x.toml") == str(Path("sub") / "x.toml")

    def test_relative_stays_relative_and_outside_stays_as_given(self, linked, tmp_path):
        assert config._display_path(Path("sub/x.toml")) == str(Path("sub") / "x.toml")
        outside = tmp_path / "shared.toml"
        assert config._display_path(outside) == str(outside)

    def test_a_symlinked_file_keeps_its_own_name(self, linked, tmp_path):
        real, _link = linked
        target = tmp_path / "elsewhere.toml"
        target.write_text("max_chunks = 2\n", encoding="utf-8")
        (real / config.CONFIG_FILE_NAME).symlink_to(target)
        assert config._display_path(real / config.CONFIG_FILE_NAME) == config.CONFIG_FILE_NAME

    def test_errors_config_check_and_the_record_agree(self, linked, recorder, capsys):
        real, link = linked
        named = str(link / config.CONFIG_FILE_NAME)
        (real / config.CONFIG_FILE_NAME).write_text("max_chunks = 3\n", encoding="utf-8")
        code, out, _err = _check(capsys, "--config", named)
        assert code == 0
        assert out.splitlines()[0] == "config file: .prxref.toml"
        code, out, _err = _check(capsys, "--config", named, "--format", "json")
        assert json.loads(out)["config_file"] == ".prxref.toml"
        assert _review("--config", named, "--format", "json") == 0
        assert json.loads(capsys.readouterr().out)["config_file"]["path"] == ".prxref.toml"
        (real / config.CONFIG_FILE_NAME).write_text("max_chunks = 0\n", encoding="utf-8")
        code, out, err = _check(capsys, "--config", named)
        assert code == 2
        assert out == ""
        assert err.startswith("configuration error: .prxref.toml: max_chunks: ")


# --------------------------------------------------------------------------- sources helper


class TestLoadConfigWithSources:
    def test_same_config_as_load_config(self, repo, monkeypatch):
        monkeypatch.setenv("PRXREF_POST_MODE", "summary")
        path = repo / config.CONFIG_FILE_NAME
        cfg, layers = config.load_config_with_sources(config_file=path, max_workers=2)
        assert cfg == config.load_config(config_file=path, max_workers=2)
        assert layers["max_chunks"] == "file"
        assert layers["post_mode"] == "env PRXREF_POST_MODE"
        assert layers["max_workers"] == "override"
        assert layers["llm_models"] == "default"
        assert set(layers) == set(config._DEFAULTS)

    def test_a_legacy_alias_is_named_as_read(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PRXREF_MAX_ERRORS", "1")
        _, layers = config.load_config_with_sources()
        assert layers["max_error_findings"] == "env PRXREF_MAX_ERRORS"


# --------------------------------------------------------------------------- llm_temperature


class TestTemperatureInTheFile:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("llm_temperature = 0.2", "0.2"),
            ("llm_temperature = 1", "1"),
            ("llm_temperature = 1.0", "1.0"),
            ('llm_temperature = "0.2"', "0.2"),
        ],
    )
    def test_a_number_reads_as_the_env_string(self, tmp_path, text, expected):
        path = tmp_path / config.CONFIG_FILE_NAME
        path.write_text(text + "\n", encoding="utf-8")
        assert config.read_config_file(path) == {"llm_temperature": expected}

    def test_it_matches_the_env_path(self, tmp_path, monkeypatch):
        path = tmp_path / config.CONFIG_FILE_NAME
        path.write_text("llm_temperature = 0.2\n", encoding="utf-8")
        from_file = config.load_config(config_file=path)["llm_temperature"]
        monkeypatch.setenv("PRXREF_LLM_TEMPERATURE", "0.2")
        assert from_file == config.load_config()["llm_temperature"] == "0.2"

    @pytest.mark.parametrize(("text", "got"), [("llm_temperature = true", "a boolean"),
                                               ("llm_temperature = [0.2]", "an array")])
    def test_other_types_are_rejected(self, tmp_path, text, got):
        path = tmp_path / config.CONFIG_FILE_NAME
        path.write_text(text + "\n", encoding="utf-8")
        with pytest.raises(config.ConfigError) as excinfo:
            config.read_config_file(path)
        assert str(excinfo.value) == (
            f"{path}: 'llm_temperature' must be a number or a string, got {got}; "
            f"see {config.CONFIG_DOCS_URL}"
        )


# --------------------------------------------------------------------------- help


class TestHelp:
    @pytest.mark.parametrize("argv", [["review", "--help"], ["eval", "run", "--help"],
                                      ["config", "check", "--help"]])
    def test_the_flags_name_the_file_and_the_variable(self, argv, capsys):
        with pytest.raises(SystemExit):
            cli.main(argv)
        flat = " ".join(capsys.readouterr().out.split())
        assert "--config PATH" in flat and "--no-config" in flat
        assert ".prxref.toml" in flat and "PRXREF_CONFIG_FILE" in flat

    def test_serve_help(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["serve", "--help"])
        flat = " ".join(capsys.readouterr().out.split())
        assert "--config PATH" in flat and "--no-config" not in flat
        assert ".prxref.toml" in flat and "PRXREF_CONFIG_FILE" in flat

    def test_the_top_level_help_lists_config(self, capsys):
        with pytest.raises(SystemExit):
            cli.main(["--help"])
        assert "config" in capsys.readouterr().out.split("{", 1)[1].split("}", 1)[0].split(",")
