"""The repository-context read caps as config, and which of them refused a read (#61).

``repo_context_max_reads`` (``PRXREF_REPO_CONTEXT_MAX_READS``, default 200) and
``repo_context_max_chunk_reads`` (``PRXREF_REPO_CONTEXT_MAX_CHUNK_READS``,
default 16) restate :data:`prxref.repo_reader.MAX_RUN_READS` and
:data:`prxref.repo_reader.MAX_CHUNK_READS` as file keys, reach the run's one
``RepoReader`` as ``run_cap`` and ``chunk_cap``, and are recorded in the run
record's ``repo_context`` next to ``chunk_read_cap_hit`` and
``run_read_cap_hit``. The orchestrator tests run the real reviewer over
``tests/fixtures/issue17`` with the fixture forge of
``tests/test_orchestrator_repo_context.py``. No test touches the network.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from prxref import cli, config, evals, orchestrator
from prxref.config import CONFIG_FILE_NAME, FILE_KEYS, load_config
from prxref.llm import ConfigError
from prxref.repo_reader import MAX_CHUNK_READS, MAX_RUN_READS, RepoReader
from tests.test_eval_score import CASE_C, _record, _row, _write, _write_run
from tests.test_orchestrator_repo_context import GLOBS, _RepoForge, _review

KEYS = {
    "repo_context_max_reads": ("PRXREF_REPO_CONTEXT_MAX_READS", 200),
    "repo_context_max_chunk_reads": ("PRXREF_REPO_CONTEXT_MAX_CHUNK_READS", 16),
}
DIFF = Path(__file__).parent / "fixtures" / "issue17" / "pr.diff"


class _Fetch:
    """A ``fetch`` that returns each path upper-cased and records the calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, path: str) -> str:
        self.calls.append(path)
        return path.upper()


def _flags(reader: RepoReader) -> tuple[bool, bool, bool]:
    stats = reader.stats()
    return stats["chunk_read_cap_hit"], stats["run_read_cap_hit"], stats["read_cap_hit"]


# --------------------------------------------------------------------------- config


class TestConfigKeys:
    @pytest.mark.parametrize("key", sorted(KEYS))
    def test_the_default_restates_the_repo_reader_constant(self, key):
        env, default = KEYS[key]
        assert config._DEFAULTS[key] == default
        assert load_config()[key] == default
        assert (config._DEFAULTS["repo_context_max_reads"], config._DEFAULTS["repo_context_max_chunk_reads"]) == (
            MAX_RUN_READS, MAX_CHUNK_READS,
        )

    @pytest.mark.parametrize("key", sorted(KEYS))
    def test_an_int_open_at_zero_like_max_chunks_and_a_file_key(self, key):
        assert key in config._INT_KEYS
        assert config._RANGES[key] == config._RANGES["max_chunks"] == config._Range(0)
        assert config._RANGES[key].accepts(0) is False
        assert config._RANGES[key].accepts(1) is True
        assert key in FILE_KEYS
        assert key not in config.ENV_ONLY_KEYS

    @pytest.mark.parametrize("key", sorted(KEYS))
    def test_env_coerces_to_an_int(self, key, monkeypatch):
        env, _ = KEYS[key]
        monkeypatch.setenv(env, " 7 ")
        value = load_config()[key]
        assert value == 7 and isinstance(value, int)

    @pytest.mark.parametrize("key", sorted(KEYS))
    @pytest.mark.parametrize("raw", ["0", "-1"])
    def test_zero_and_negative_are_errors_naming_the_variable(self, key, raw, monkeypatch):
        env, _ = KEYS[key]
        monkeypatch.setenv(env, raw)
        with pytest.raises(ConfigError, match=rf"^{env}: must be a finite number greater than 0, got {raw}$"):
            load_config()

    @pytest.mark.parametrize("key", sorted(KEYS))
    def test_a_non_integer_is_an_error_naming_the_variable(self, key, monkeypatch):
        env, _ = KEYS[key]
        monkeypatch.setenv(env, "lots")
        with pytest.raises(ConfigError, match=rf"^{env}: "):
            load_config()

    @pytest.mark.parametrize("key", sorted(KEYS))
    def test_an_override_is_range_checked_and_named_as_itself(self, key):
        with pytest.raises(ConfigError, match=rf"^{key}: "):
            load_config(**{key: 0})

    @pytest.mark.parametrize("key", sorted(KEYS))
    def test_a_toml_file_sets_it_and_env_beats_the_file(self, key, tmp_path, monkeypatch):
        env, _ = KEYS[key]
        monkeypatch.chdir(tmp_path)
        path = tmp_path / CONFIG_FILE_NAME
        path.write_text(f"{key} = 5\n", encoding="utf-8")
        assert load_config(config_file=path)[key] == 5
        monkeypatch.setenv(env, "9")
        assert load_config(config_file=path)[key] == 9

    @pytest.mark.parametrize("key", sorted(KEYS))
    def test_a_bad_toml_value_names_the_file_and_the_key(self, key, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        path = tmp_path / CONFIG_FILE_NAME
        path.write_text(f"{key} = 0\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=rf"^\.prxref\.toml: {key}: must be"):
            load_config(config_file=path)

    @pytest.mark.parametrize("key", sorted(KEYS))
    @pytest.mark.parametrize("raw", ["0", "-2", "many"])
    def test_review_exits_2_naming_the_variable(self, key, raw, monkeypatch, capsys):
        env, _ = KEYS[key]
        monkeypatch.setenv(env, raw)
        monkeypatch.setenv(config.CONFIG_FILE_ENV, "off")
        assert cli.main(["review", "--diff-file", str(DIFF), "--no-post"]) == 2
        err = capsys.readouterr().err
        assert f"configuration error: {env}: " in err


# --------------------------------------------------------------------------- reader


class TestWhichCapBinds:
    def test_the_defaults_leave_only_the_chunk_cap_reachable(self):
        """With the defaults and the context follow-up off, each of at most
        ``max_chunks`` (8) chunks gets one chunk reader of 16 reads, so the run
        spends at most 8 x 16 = 128 reads, under the run cap of 200: only
        ``chunk_read_cap_hit`` can be set. #61's reasoning rests on this, and it
        breaks the moment either default moves."""
        assert 8 * MAX_CHUNK_READS < MAX_RUN_READS
        assert config._DEFAULTS["max_chunks"] * config._DEFAULTS["repo_context_max_chunk_reads"] < (
            config._DEFAULTS["repo_context_max_reads"]
        )

    def test_a_chunk_that_spends_its_own_cap_sets_only_the_chunk_flag(self):
        reader = RepoReader(_Fetch(), None, kind="forge", run_cap=100, chunk_cap=2)
        chunk = reader.chunk_reader()
        assert [chunk(p) for p in "ab"] == ["A", "B"]
        assert _flags(reader) == (False, False, False)
        assert chunk("c") is None
        assert _flags(reader) == (True, False, True)

    def test_the_run_cap_below_chunks_times_chunk_cap_sets_the_run_flag(self):
        """Three chunks x ``chunk_cap=2`` would be 6 reads; ``run_cap=3`` binds first."""
        fetch = _Fetch()
        reader = RepoReader(fetch, None, kind="repo-dir", run_cap=3, chunk_cap=2)
        first, second, third = (reader.chunk_reader() for _ in range(3))
        assert [first(p) for p in "ab"] == ["A", "B"]
        assert second("c") == "C"
        assert second("d") is None
        assert _flags(reader) == (False, True, True)
        assert third("e") is None
        assert _flags(reader) == (False, True, True)
        assert fetch.calls == ["a", "b", "c"]

    def test_both_caps_spent_at_once_set_both_flags(self):
        reader = RepoReader(_Fetch(), None, kind="forge", run_cap=2, chunk_cap=2)
        chunk = reader.chunk_reader()
        assert [chunk(p) for p in "ab"] == ["A", "B"]
        assert chunk("c") is None
        assert _flags(reader) == (True, True, True)

    def test_a_cached_path_past_both_caps_sets_nothing(self):
        reader = RepoReader(_Fetch(), None, kind="forge", run_cap=1, chunk_cap=1)
        reader.chunk_reader()("a")
        assert reader.chunk_reader()("a") == "A"
        assert _flags(reader) == (False, False, False)


# --------------------------------------------------------------------------- orchestrator


class TestOrchestratorThreading:
    def test_the_kwargs_default_to_the_repo_reader_constants(self):
        params = inspect.signature(orchestrator.orchestrate_review).parameters
        for name, default in (("repo_context_max_reads", MAX_RUN_READS),
                              ("repo_context_max_chunk_reads", MAX_CHUNK_READS)):
            assert params[name].default == default
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY

    def test_the_defaults_are_recorded_and_bind_nothing(self):
        forge = _RepoForge()
        result, _ = _review(forge, repo_context="repo", context_contract_globs=GLOBS, max_workers=1)
        record = result["repo_context"]
        assert (record["max_reads"], record["max_chunk_reads"]) == (200, 16)
        assert record["reads"] == 5
        assert (record["chunk_read_cap_hit"], record["run_read_cap_hit"], record["read_cap_hit"]) == (
            False, False, False,
        )

    def test_a_small_chunk_cap_binds_per_chunk(self):
        forge = _RepoForge()
        result, _ = _review(
            forge, repo_context="repo", context_contract_globs=GLOBS, max_workers=1,
            repo_context_max_chunk_reads=1,
        )
        record = result["repo_context"]
        assert (record["max_reads"], record["max_chunk_reads"]) == (200, 1)
        assert record["reads"] < 5
        assert (record["chunk_read_cap_hit"], record["run_read_cap_hit"], record["read_cap_hit"]) == (
            True, False, True,
        )

    def test_a_small_run_cap_binds_across_chunks(self, tmp_path):
        forge = _RepoForge()
        trace = tmp_path / "trace.jsonl"
        result, _ = _review(
            forge, repo_context="repo", context_contract_globs=GLOBS, max_workers=1,
            repo_context_max_reads=1, trace_file=str(trace),
        )
        record = result["repo_context"]
        assert (record["max_reads"], record["max_chunk_reads"]) == (1, 16)
        assert record["reads"] < 5
        assert record["run_read_cap_hit"] is True
        assert record["read_cap_hit"] is True
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
        (event,) = [e for e in events if e["node"] == "repo_context"]
        assert event["meta"]["run_read_cap_hit"] is True
        assert event["meta"]["chunk_read_cap_hit"] is record["chunk_read_cap_hit"]

    def test_the_pre_worker_record_carries_the_caps(self):
        forge = _RepoForge("")
        result, _ = _review(
            forge, repo_context="diff", repo_context_max_reads=7, repo_context_max_chunk_reads=3,
        )
        record = result["repo_context"]
        assert (record["max_reads"], record["max_chunk_reads"], record["reads"]) == (7, 3, 0)
        assert (record["chunk_read_cap_hit"], record["run_read_cap_hit"], record["read_cap_hit"]) == (
            False, False, False,
        )


# --------------------------------------------------------------------------- cli


class TestCliLine:
    BASE = {
        "mode": "repo", "max_chars": 12000, "max_reads": 200, "max_chunk_reads": 16,
        "reader": "forge", "listing": {"paths": 9, "complete": True}, "reads": 16, "units": None,
    }

    @pytest.mark.parametrize("chunk,run,shown", [
        (False, False, "no"), (True, False, "chunk"), (False, True, "run"), (True, True, "chunk+run"),
    ])
    def test_cap_hit_names_the_cap(self, chunk, run, shown):
        repo = {**self.BASE, "read_cap_hit": chunk or run, "chunk_read_cap_hit": chunk, "run_read_cap_hit": run}
        assert cli._repo_context_line(repo) == (
            f"repo context: mode=repo reader=forge listing=9 reads=16 max_reads=200 max_chunk_reads=16 cap_hit={shown} "
            "entries=0 omitted=0"
        )

    def test_the_configured_caps_reach_the_line_through_the_entry_point(self, monkeypatch, capsys):
        seen: dict = {}

        def fake(**kwargs):
            seen.update(kwargs)
            return {
                "verdict": "Approved", "findings_active": [], "findings_dropped": [],
                "repo_context": {
                    **self.BASE, "max_reads": kwargs["repo_context_max_reads"],
                    "max_chunk_reads": kwargs["repo_context_max_chunk_reads"], "reads": 3,
                    "read_cap_hit": True, "chunk_read_cap_hit": False, "run_read_cap_hit": True,
                },
            }

        monkeypatch.setattr("prxref.orchestrator.orchestrate_review", fake)
        monkeypatch.setattr("prxref.llm_backends.create_llm_client", lambda cfg: object())
        monkeypatch.setenv(config.CONFIG_FILE_ENV, "off")
        monkeypatch.setenv("PRXREF_REPO_CONTEXT_MAX_READS", "3")
        monkeypatch.setenv("PRXREF_REPO_CONTEXT_MAX_CHUNK_READS", "2")
        assert cli.main(["review", "--diff-file", str(DIFF), "--no-post", "-v"]) == 0
        assert (seen["repo_context_max_reads"], seen["repo_context_max_chunk_reads"]) == (3, 2)
        lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("repo context: ")]
        assert lines == [
            "repo context: mode=repo reader=forge listing=9 reads=3 max_reads=3 max_chunk_reads=2 "
            "cap_hit=run entries=0 omitted=0",
        ]


# --------------------------------------------------------------------------- evals


PRE_61_CONFIG_KEYS = tuple(k for k in evals.RUN_CONFIG_KEYS if k not in KEYS)


class TestEvalBaselines:
    def test_run_config_keys_record_both_caps(self):
        assert set(KEYS) <= set(evals.RUN_CONFIG_KEYS)
        assert len(PRE_61_CONFIG_KEYS) == len(evals.RUN_CONFIG_KEYS) - 2

    def _score(self, out: Path, label: str, run_config: dict) -> Path:
        record = _record([_row("src/c.py", 3, "Leak", severity="error")])
        run_dir = _write_run(out, [(CASE_C, record)], label=label)
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        run["config"] = run_config
        _write(run_dir / "run.json", run)
        args = cli._build_parser().parse_args(["eval", "score", f"--label={label}", "--out", str(out)])
        assert evals.eval_score(args) == 0
        return run_dir

    def test_a_baseline_without_the_keys_scores_and_compares(self, tmp_path, capsys):
        cfg = load_config()
        old = {key: cfg[key] for key in PRE_61_CONFIG_KEYS}
        new = {key: cfg[key] for key in evals.RUN_CONFIG_KEYS}
        assert not set(KEYS) & set(old)
        old_dir = self._score(tmp_path, "old", old)
        self._score(tmp_path, "new", new)
        score = json.loads((old_dir / "score.json").read_text(encoding="utf-8"))
        assert score["run"]["config"] == old
        capsys.readouterr()
        args = cli._build_parser().parse_args(["eval", "compare", "old", "new", "--out", str(tmp_path)])
        assert evals.eval_compare(args) == 0
        out = capsys.readouterr().out
        assert out.startswith("# prxref eval compare\n")
        assert "## Metrics" in out
